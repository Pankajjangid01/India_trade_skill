"""in-timemachine in the signal flow: a rolling 60-trading-day window of 5-minute index bars, and the
replay + 10,000-sample resample (seed 19) of the strategy that produced a signal, run before the paper trade.

The window lives in trader/state/timemachine/window.sqlite, one row per (symbol, token, day). Each run
keeps exactly the last N closed trading days (by calendar.json) ending at the run's end date: days older
than the window are dropped, and only days missing from it (the new day, or a gap) are fetched, through the
skill pack's shared candle cache and rate limiter (smart.candles). Today is never stored: only closed days.

The replay:
  ORB           in-timemachine's own replay_day: the pack's opening-range rule with this strategy's stop
                rule and target R, the session's entry window and flat-by (expiry-day times on expiry days).
  EMA_PULLBACK  the engine's own strategy function on each day (seeded with the day before), every signal
                in the entry window walked bar by bar with the same rule: the stop before the target inside a
                bar, else out at the flat-by close. One position at a time, max_trades_per_day per day.
Then in-timemachine's stats_of and bootstrap: win rate, expectancy in R, worst drawdown, losing streak.
Underlying only, no charges (the option is not picked yet). A replay of the past, never a forecast.

Each verdict is written to trader/state/timemachine/<SYMBOL>_<STRATEGY>.json as {"ok", "reason", "data"}.
"""

import datetime
import json
import logging
import os
import sqlite3
import threading
from decimal import Decimal

from app import strategies as strat
from app.sp import smart, tm

log = logging.getLogger("timemachine")
DEFAULTS = {"enabled": True, "days": 60, "min_expectancy_r": 0.0, "required": False}
INTERVAL = "FIVE_MINUTE"


def settings(cfg):
    return {**DEFAULTS, **((cfg.get("timemachine") or {}))}


def folder(trader_dir):
    path = os.path.join(trader_dir, "state", "timemachine")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- the rolling window

class Window:
    """Date-indexed store: one row per (symbol, token, day), so a day is appended or dropped on its own."""

    def __init__(self, trader_dir):
        self.path = os.path.join(folder(trader_dir), "window.sqlite")
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.executescript("CREATE TABLE IF NOT EXISTS days (symbol TEXT NOT NULL, token TEXT NOT NULL, "
                              "day TEXT NOT NULL, bars TEXT NOT NULL, fetched_at TEXT, "
                              "PRIMARY KEY (symbol, token, day))")

    def close(self):
        self.db.close()

    def days(self, symbol, token):
        with self.lock:
            return {d: json.loads(b) for d, b in self.db.execute(
                "SELECT day, bars FROM days WHERE symbol = ? AND token = ? ORDER BY day", (symbol, str(token)))}

    def put(self, symbol, token, day, rows, now):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO days VALUES (?, ?, ?, ?, ?)",
                            (symbol, str(token), day, json.dumps(rows), now.isoformat()))

    def drop_before(self, symbol, token, first_day):
        with self.lock, self.db:
            return self.db.execute("DELETE FROM days WHERE symbol = ? AND token = ? AND day < ?",
                                   (symbol, str(token), first_day)).rowcount


def window_dates(end, n, cal):
    """The last n trading days ending at `end` (inclusive, a trading day or the one before it), oldest first."""
    if not smart.trading_day(end, cal):
        end = smart.previous_trading_day(end, cal)
    out = [end]
    while len(out) < n:
        out.append(smart.previous_trading_day(out[-1], cal))
    return out[::-1]


def _runs(dates, wanted):
    """Consecutive runs (by position in `dates`) of the dates in `wanted`: one fetch each."""
    runs, cur = [], []
    for d in dates:
        if d in wanted:
            cur.append(d)
        elif cur:
            runs.append(cur)
            cur = []
    return runs + ([cur] if cur else [])


MAX_EXTEND_ROUNDS = 6       # each round reaches back past the empty days (holidays calendar.json lacks) it found


def ensure_timemachine_window(trader_dir, end_date_str, symbol, segment, token, client, now=None, days=60,
                              cal=None, store=None):
    """Keep the stored window at exactly the last `days` closed trading days WITH BARS ending at end_date_str.

    A day that comes back empty (an exchange holiday calendar.json does not list) is stored as empty, never
    fetched again, and the window reaches one day further back for it. Stored days older than the window are
    dropped; only the days it lacks (the new day, or a gap) are fetched. Returns {"path", "dates" (the span,
    empty days included), "bars": {day: rows}, "fetched", "dropped", "missing", "empty", "partial"}.
    end_date_str later than the last closed day is moved back to it: today's bars are never stored."""
    now = now or smart.now_ist()
    cal = cal if cal is not None else (smart.read_json(os.path.join(trader_dir, "calendar.json"), {}) or {})
    end = datetime.date.fromisoformat(end_date_str)
    if end >= now.date():
        end = smart.previous_trading_day(now.date(), cal)
    own = store is None
    store = store or Window(trader_dir)
    try:
        have = store.days(symbol, token)
        fetched, failed, extra = [], [], 0
        cache = smart.trader_paths(trader_dir)["cache"]
        for _ in range(MAX_EXTEND_ROUNDS):
            dates = [d.isoformat() for d in window_dates(end, days + extra, cal)]
            missing = [d for d in dates if d not in have]
            if missing and client is None:
                failed.append("no broker session to fetch %d missing day(s)" % len(missing))
            elif missing:
                for run in _runs(dates, set(missing)):
                    got, partial = smart.candles(client, cache, segment, token, INTERVAL,
                                                 datetime.date.fromisoformat(run[0]),
                                                 datetime.date.fromisoformat(run[-1]), now)
                    if partial:
                        failed.append(partial)
                    for d in run:
                        if d in got:
                            store.put(symbol, token, d, got[d], now)
                            have[d] = got[d]
                            fetched.append(d)
            empty = sum(1 for d in dates if d in have and not have[d])
            if empty == extra or (missing and client is None):
                break
            extra = empty
        dropped = store.drop_before(symbol, token, dates[0])
        bars = {d: have[d] for d in dates if d in have}
        out = {"path": store.path, "dates": dates, "bars": bars, "fetched": fetched, "dropped": dropped,
               "missing": [d for d in dates if d not in bars], "empty": [d for d in dates if d in bars and not bars[d]],
               "partial": "; ".join(failed) or None}
        if fetched or dropped:
            log.info("time machine window %s: %s to %s, %d day(s) with bars, fetched %d, dropped %d%s", symbol,
                     dates[0], dates[-1], sum(1 for v in bars.values() if v), len(fetched), dropped,
                     ", no bars on %s" % ", ".join(out["empty"]) if out["empty"] else "")
        return out
    finally:
        if own:
            store.close()


# ---------------------------------------------------------------- the replay

def _bars(rows):
    return strat.clean_bars([{"start_ist": str(r[0])[11:16], "open": r[1], "high": r[2], "low": r[3],
                              "close": r[4]} for r in sorted(rows or [])])


def _mins(hm):
    return int(hm[:2]) * 60 + int(hm[3:5])


def _rule(cfg, s):
    sess, ex = cfg.get("session") or {}, cfg.get("exit") or {}
    stop = tm.parse_stop(s.get("stop_rule") or "or-mid") or ("or_mid", None)
    return {"range_start": "09:15", "range_end": sess.get("range_end", "09:30"),
            "entry_start": sess.get("entry_start", "09:30"), "entry_end": sess.get("entry_end", "14:00"),
            "flat_by": sess.get("flat_by", "15:10"),
            "expiry_last_entry": sess.get("expiry_day_last_entry", "11:00"),
            "expiry_flat_by": sess.get("expiry_day_flat_by", "14:45"),
            "stop": stop[0], "stop_k": stop[1], "target_r": float(s.get("target_r") or ex.get("target_r", 2.0))}


def walk(bars, i, bull, entry, stop, target, flat, target_r):
    """From the entry bar i on: (outcome, R, exit_time). The stop before the target inside one bar."""
    risk = (entry - stop) if bull else (stop - entry)
    outcome, last = "flat_by", bars[i]
    for b in bars[i + 1:]:
        if _mins(b["start_ist"]) + 5 > _mins(flat):
            break
        last = b
        if (b["low"] <= stop) if bull else (b["high"] >= stop):
            outcome = "stop"
            break
        if (b["high"] >= target) if bull else (b["low"] <= target):
            outcome = "target"
            break
    r = -1.0 if outcome == "stop" else target_r if outcome == "target" else \
        float((Decimal(str(last["close"])) - Decimal(str(entry))) * (1 if bull else -1) / Decimal(str(risk)))
    return outcome, round(r, 4), tm.clock(_mins(last["start_ist"]) + 5)


def _ema_day(symbol, day, prev, bars, s, rule, expiry):
    return _sequence(strat.ema_pullback_signals(symbol, day, prev, bars, s), day, bars, s, rule, expiry)


def _ema_orb_day(symbol, day, prev, bars, s, rule, expiry):
    """EMA_ORB through the very function the live engine calls (strat.ema_orb_signals)."""
    sigs = strat.ema_orb_signals(symbol, day, prev, bars, s, rule["range_end"])
    return _sequence(sigs, day, bars, s, rule, expiry)


def _sequence(signals, day, bars, s, rule, expiry):
    """The day's signals as the engine would trade them: inside the entry window, one position at a time,
    at most max_trades_per_day, and (stop_after_first_sl) no more after a first trade that hit its stop."""
    last_entry = min(rule["entry_end"], rule["expiry_last_entry"]) if expiry else rule["entry_end"]
    flat = rule["expiry_flat_by"] if expiry else rule["flat_by"]
    out, free_at = [], "00:00"
    for sig in signals:
        if len(out) >= int(s.get("max_trades_per_day", 2)):
            break
        if s.get("stop_after_first_sl") and out and out[0]["outcome"] == "stop":
            break
        if not rule["entry_start"] <= sig["bar_close_ist"] <= last_entry or sig["bar_start_ist"] < free_at:
            continue
        i = next(k for k, b in enumerate(bars) if b["start_ist"] == sig["bar_start_ist"])
        bull = sig["direction"] == "bullish"
        outcome, r, exit_time = walk(bars, i, bull, sig["entry_underlying"], sig["stop_underlying"],
                                     sig["target_underlying"], flat, float(s["target_r"]))
        out.append({"status": "trade", "date": day, "direction": sig["direction"], "expiry_day": expiry,
                    "entry_time": sig["bar_close_ist"], "entry": sig["entry_underlying"],
                    "stop": sig["stop_underlying"], "target": sig["target_underlying"], "outcome": outcome,
                    "exit_time": exit_time, "r": r, "signal_id": sig["id"],
                    **({"or_pct": sig["or_pct"], "stop_method": sig["stop_method"]} if "or_pct" in sig else {})})
        free_at = exit_time
    return out or [{"status": "no_trigger", "date": day}]


def simulate(symbol, strategy, cfg, window, expiries=()):
    """Replay `strategy` on the window's days and resample: the data part of the verdict."""
    s = strat.strategy_cfg(cfg, strategy)
    rule = _rule(cfg, s)
    used = [d for d in window["dates"] if window["bars"].get(d)]
    bars = {d: _bars(window["bars"][d]) for d in used}
    used = [d for d in used if bars[d]]
    if len(used) < tm.MIN_DAYS:
        raise smart.SmartError("only %d day(s) of %s bars in the window, the replay needs %d%s" % (
            len(used), symbol, tm.MIN_DAYS, " (%s)" % window["partial"] if window.get("partial") else ""))
    max_gap = float((cfg.get("timemachine") or {}).get("max_gap_pct", tm.MAX_GAP_PCT))
    gaps = [(d, abs(bars[d][0]["open"] / bars[p][-1]["close"] - 1) * 100) for p, d in zip(used, used[1:])]
    gap_day, gap = max(gaps, key=lambda g: g[1]) if gaps else (None, 0.0)
    if gap > max_gap:
        raise smart.SmartError("an overnight gap of %.2f %% into %s is over timemachine.max_gap_pct %g, likely a "
                               "corporate action: no replay" % (gap, gap_day, max_gap))
    expiries = set(expiries)
    days = []
    for k, d in enumerate(used):
        exp = d in expiries
        if strategy == "ORB":
            days.append(dict(tm.replay_day(bars[d], rule, exp), date=d))
            if days[-1]["status"] == "trade":
                days[-1]["r"] = days[-1]["r_gross"]
        elif k > 0:                                     # EMA_PULLBACK and EMA_ORB: seeded with the day before
            day_fn = _ema_orb_day if strategy == "EMA_ORB" else _ema_day
            days += day_fn(symbol, d, bars[used[k - 1]], bars[d], s, rule, exp)
    trades = [t for t in days if t["status"] == "trade"]
    if not trades:
        raise smart.SmartError("%s never fired on the %d days from %s to %s: nothing to resample" % (
            strategy, len(used), used[0], used[-1]))
    rs = [t["r"] for t in trades]
    n = len(trades)
    count = {k: sum(1 for t in trades if t["outcome"] == k) for k in ("target", "stop", "flat_by")}
    return {"symbol": symbol, "strategy": strategy, "window": {"from": window["dates"][0], "to": window["dates"][-1],
            "days_asked": len(window["dates"]), "days_used": len(used), "missing": window["missing"]},
            "rule": rule, "underlying_only": True, "charges_applied": False,
            "history": dict(tm.stats_of(rs), trades=n, **count,
                            target_first_pct=round(100.0 * count["target"] / n, 1),
                            stop_first_pct=round(100.0 * count["stop"] / n, 1),
                            neither_pct=round(100.0 * count["flat_by"] / n, 1)),
            "bootstrap": tm.bootstrap(rs), "largest_gap": {"date": gap_day, "pct": round(gap, 2)},
            "days": days, "note": tm.NOT_FORECAST}


def verdict_line(d):
    h, b = d["history"], d["bootstrap"]["expectancy_r"]
    w = d["window"]
    return ("%s %s, %d of %d days %s..%s: %d trades, target first %.1f %%, stop first %.1f %%, neither %.1f %%, "
            "average %+.3f R (resampled 5th-95th %+.3f to %+.3f R), win rate %.1f %%" % (
                d["symbol"], d["strategy"], w["days_used"], w["days_asked"], w["from"], w["to"], h["trades"],
                h["target_first_pct"], h["stop_first_pct"], h["neither_pct"], h["expectancy_r"], b["p5"], b["p95"],
                100 * h["win_rate"]))


def summary(doc):
    """What a signal keeps of the verdict (no per-day rows)."""
    d = doc.get("data") or {}
    h, b = d.get("history") or {}, (d.get("bootstrap") or {}).get("expectancy_r") or {}
    return {"ok": doc["ok"], "reason": doc["reason"], "window": d.get("window"),
            "trades": h.get("trades"), "target_first_pct": h.get("target_first_pct"),
            "stop_first_pct": h.get("stop_first_pct"), "neither_pct": h.get("neither_pct"),
            "expectancy_r": h.get("expectancy_r"), "win_rate": h.get("win_rate"),
            "expectancy_p5": b.get("p5"), "expectancy_p95": b.get("p95")}


def check(trader_dir, symbol, strategy, cfg, window, expiries=()):
    """{"ok", "reason", "data"}: ok False blocks the trade. Written to state/timemachine/<SYMBOL>_<STRATEGY>.json."""
    st = settings(cfg)
    try:
        data = simulate(symbol, strategy, cfg, window, expiries)
    except smart.SmartError as e:
        doc = {"ok": not st["required"], "reason": "time machine unavailable: %s%s" % (
            e, "" if st["required"] else " (timemachine.required is false: not blocking)"), "data": None}
    else:
        line = verdict_line(data)
        floor = st.get("min_expectancy_r")
        if floor is not None and data["history"]["expectancy_r"] < float(floor):
            doc = {"ok": False, "reason": "%s; below timemachine.min_expectancy_r %+.3f" % (line, float(floor)),
                   "data": data}
        else:
            doc = {"ok": True, "reason": line, "data": data}
    doc["as_of_ist"] = smart.now_ist().isoformat()
    smart.write_json(os.path.join(folder(trader_dir), "%s_%s.json" % (symbol, strategy)), doc)
    return doc


def with_charges(doc, charges_r, cfg):
    """The verdict again with one lot's round-trip charges (in R of that lot's risk) off every replayed trade,
    as in-timemachine applies them once the plan is known. Unavailable verdicts pass through unchanged."""
    d = doc.get("data")
    if not d:
        return doc
    trades = [t for t in d["days"] if t["status"] == "trade"]
    rs = [round(t["r"] - charges_r, 4) for t in trades]
    h = dict(d["history"], **tm.stats_of(rs))
    data = dict(d, history=h, bootstrap=tm.bootstrap(rs), charges_applied=True, charges_r=round(charges_r, 4))
    floor = settings(cfg).get("min_expectancy_r")
    line = verdict_line(data) + ", after charges %.4f R a trade" % charges_r
    if floor is not None and h["expectancy_r"] < float(floor):
        return {"ok": False, "reason": "%s; below timemachine.min_expectancy_r %+.3f" % (line, float(floor)),
                "data": data, "as_of_ist": smart.now_ist().isoformat()}
    return {"ok": True, "reason": line, "data": data, "as_of_ist": smart.now_ist().isoformat()}


# ---------------------------------------------------------------- the backtest

BACKTEST_MAX_DAYS = 250


def backtest(trader_dir, symbol, segment, token, client, cfg, strategy, days, now=None, cal=None, expiries=()):
    """The strategy replayed on the last `days` closed trading days (20..250), for the UI's backtest page.

    Bars come through smart.candles and its forever cache (a closed day is fetched once, ever), never through
    the rolling window, so a long backtest does not disturb the pre-trade window. Returns simulate()'s result
    plus the equity curve in R, per-direction and per-month splits. Raises smart.SmartError when it cannot run."""
    now = now or smart.now_ist()
    cal = cal if cal is not None else (smart.read_json(os.path.join(trader_dir, "calendar.json"), {}) or {})
    if strategy not in strat.STRATEGIES:
        raise smart.SmartError("unknown strategy %s" % strategy)
    days = int(days)
    if not tm.MIN_DAYS <= days <= BACKTEST_MAX_DAYS:
        raise smart.SmartError("days must be %d to %d" % (tm.MIN_DAYS, BACKTEST_MAX_DAYS))
    window = history_window(trader_dir, segment, token, client, days, now, cal, seed_day=strategy != "ORB")
    data = simulate(symbol, strategy, cfg, window, expiries)
    return _report(data, window, strategy, cfg, now)


def history_window(trader_dir, segment, token, client, days, now, cal, seed_day=True):
    """The last `days` closed trading days WITH BARS (plus one seed day before them when seed_day), through
    smart.candles and its forever cache, reaching back past days that come back empty."""
    if client is None:
        raise smart.SmartError("no broker session: log in (the engine does it in market hours) to fetch history")
    # one day more than asked: EMA_PULLBACK seeds its EMAs with the day before the first one; and one more for
    # every day that comes back empty (a holiday calendar.json does not list), so `days` days have bars
    end, extra = smart.previous_trading_day(now.date(), cal), 0
    for _ in range(MAX_EXTEND_ROUNDS):
        dates = [d.isoformat() for d in window_dates(end, days + 1 + extra, cal)]
        got, partial = smart.candles(client, smart.trader_paths(trader_dir)["cache"], segment, token, INTERVAL,
                                     datetime.date.fromisoformat(dates[0]), datetime.date.fromisoformat(dates[-1]),
                                     now)
        empty = sum(1 for d in dates if d in got and not got[d])
        if empty == extra or partial:
            break
        extra = empty
    full = [d for d in dates if got.get(d)]
    if not seed_day and len(full) > days:                   # ORB needs no seed day: the oldest one is left out
        dates = dates[dates.index(full[0]) + 1:]
    return {"dates": dates, "bars": {d: got[d] for d in dates if d in got},
            "missing": [d for d in dates if d not in got], "partial": partial}


def _report(data, window, strategy, cfg, now):
    partial = window["partial"]
    trades = [t for t in data["days"] if t["status"] == "trade"]
    cum, curve = 0.0, []
    for t in trades:
        cum = round(cum + t["r"], 4)
        curve.append({"date": t["date"], "time": t["entry_time"], "r": t["r"], "cum_r": cum})
    split = {}
    for t in trades:
        for key in (t["direction"], t["date"][:7]):
            g = split.setdefault(key, [])
            g.append(t["r"])
    data.update(equity_curve=curve, partial=partial, as_of_ist=now.isoformat(),
                by_direction={k: dict(tm.stats_of(v), trades=len(v)) for k, v in split.items()
                              if k in ("bullish", "bearish")},
                by_month={k: dict(tm.stats_of(v), trades=len(v), total_r=round(sum(v), 4))
                          for k, v in sorted(split.items()) if k[:2] == "20"},
                settings=strat.strategy_cfg(cfg, strategy))
    return data
