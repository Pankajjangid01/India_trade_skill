#!/usr/bin/env python3
"""Time Machine for in-timemachine: the pack's opening-range rule replayed on past
days of the underlying. Standard library only. Read-only: it fetches history and
calls no order endpoint.

    python in-timemachine/timemachine.py run                         # plan.json's symbol (else NIFTY), 60 days
    python in-timemachine/timemachine.py run --symbol SENSEX --days 60 --write
    python in-timemachine/timemachine.py run --stop pct:0.4          # or-mid (default), or-opposite, pct:X, atr:K
    python in-timemachine/timemachine.py show                        # the saved trader/timemachine.json
    python in-timemachine/timemachine.py run --offline DIR --now "2026-11-24 18:00"

Data: the index's FIVE_MINUTE candles for the last N trading days before today
(default 60), from getCandleData through the shared candle cache (one call covers
up to 100 days), the index token from the instrument master. Empty days
(weekends, holidays) are skipped.

Per day, the rule of in-entry and in-exit: the opening range 09:15 to
session.range_end; the first closed 5-minute bar beyond it inside
session.entry_start to entry_end (on an expiry day the master shows, no later
than expiry_day_last_entry) is the entry at its close, bullish above, bearish
below; the underlying stop by --stop, else plan.json's rule, else or-mid
(or-opposite, pct:X, atr:K as in-exit has them), rounded to 0.05 away from the
entry; the target at target_r times the risk, rounded half up; flat at
session.flat_by (expiry_day_flat_by on an expiry day). Inside a bar the stop is
checked before the target. Outcome in R on the underlying: +target_r, -1, or the
move to the flat-by close. With plan.json, one lot's round-trip charges at its
entry premium and premium stop, as R of its r_value_inr, come off every trade.

The spread: 10,000 bootstrap samples of the trade outcomes (as many trades as the
history, drawn with replacement by random.Random(19)): win rate, expectancy,
worst drawdown and longest losing streak, median and 5th to 95th percentile
(sorted value at index int(q * n)). Same input, same output.

Option premium is replayed only from the pack's own chain snapshots
(trader/history/chain/<U>/<date>.jsonl, from in-chain) once 20 days exist.

A replay of the past, not a forecast. It never makes a live signal and never
creates a virtual account. Exit codes: 0 = a result, 1 = nothing or an error, in one line.
"""

import argparse
import datetime
import glob
import json
import os
import random
import re
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

SEED, SAMPLES, MIN_DAYS = 19, 10000, 20
MAX_GAP_PCT = 15.0
ATR_BARS, INDEX_TICK = 14, Decimal("0.05")   # in-exit's
NOT_FORECAST = ("A replay of the past, not a forecast: it never makes a live signal and never creates a "
                "virtual account. Same input, same output (seed %d)." % SEED)
STOP_WORDS = {"or_mid": "the range midpoint", "or_opposite": "the opposite side of the range"}


def mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def clock(m):
    return "%02d:%02d" % divmod(m, 60)


def parse_stop(text):
    """in-exit's rules: 'or-mid', 'or_opposite', 'pct:0.4', 'atr:1.5' -> (rule, K or None); None if unknown."""
    m = re.match(r"^(?:(or[_-]mid)|(or[_-]opposite)|(pct|atr)[:_ ]?(\d*\.?\d+))$", str(text or "").strip().lower())
    if not m or (m.group(4) and float(m.group(4)) <= 0):
        return None
    return ("or_mid", None) if m.group(1) else ("or_opposite", None) if m.group(2) else (m.group(3), float(m.group(4)))


def D(x):
    return Decimal(str(x))


def atr(bars):
    """in-exit's ATR: the mean of the last 14 true ranges up to the entry bar (fewer if there are fewer)."""
    trs, prev = [], None
    for b in bars:
        h, low = D(b["high"]), D(b["low"])
        trs.append(h - low if prev is None else max(h - low, abs(h - prev), abs(low - prev)))
        prev = D(b["close"])
    return sum(trs[-ATR_BARS:]) / len(trs[-ATR_BARS:])


# ---------------------------------------------------------------- one day

def replay_day(bars, rule, expiry=False):
    """One day by the rule. bars: [{"start_ist": "09:15", "open", "high", "low", "close"}], oldest first.
    {"status": "trade", ...} or {"status": "no_trigger" | "no_range"}. Levels as in-exit sets them: the stop
    rounded to 0.05 away from the entry, the target half up."""
    rng = smart.opening_range(bars, rule["range_start"], rule["range_end"])
    if rng is None:
        return {"status": "no_range"}
    hi, lo = rng["high"], rng["low"]
    first = max(rule["range_end"], rule["entry_start"])
    last_entry = min(rule["entry_end"], rule["expiry_last_entry"]) if expiry else rule["entry_end"]
    flat = rule["expiry_flat_by"] if expiry else rule["flat_by"]
    i = next((k for k, b in enumerate(bars) if mins(first) <= mins(b["start_ist"])
              and mins(b["start_ist"]) + 5 <= mins(last_entry) and not lo <= b["close"] <= hi), None)
    if i is None:
        return {"status": "no_trigger"}
    bull, e = bars[i]["close"] > hi, D(bars[i]["close"])
    sign = 1 if bull else -1
    if rule["stop"] == "or_mid":
        raw = (D(hi) + D(lo)) / 2
    elif rule["stop"] == "or_opposite":
        raw = D(lo if bull else hi)
    else:
        raw = e - sign * D(rule["stop_k"]) * (e / 100 if rule["stop"] == "pct" else atr(bars[:i + 1]))
    stop = smart.round_tick(raw, INDEX_TICK, "down" if bull else "up")
    risk = (e - stop) * sign   # above 0: the close is beyond the range and the stop rounds away from it
    target = smart.round_tick(e + sign * D(rule["target_r"]) * risk, INDEX_TICK)
    stop, target, entry = float(stop), float(target), float(e)
    outcome, last = "flat_by", bars[i]
    for b in bars[i + 1:]:
        if mins(b["start_ist"]) + 5 > mins(flat):
            break
        last = b
        if (b["low"] <= stop) if bull else (b["high"] >= stop):   # both in one bar: the stop (conservative)
            outcome = "stop"
            break
        if (b["high"] >= target) if bull else (b["low"] <= target):
            outcome = "target"
            break
    r = -1.0 if outcome == "stop" else rule["target_r"] if outcome == "target" else \
        float((D(last["close"]) - e) * sign / risk)
    return {"status": "trade", "direction": "bullish" if bull else "bearish", "expiry_day": expiry,
            "range": [hi, lo], "entry_time": clock(mins(bars[i]["start_ist"]) + 5), "entry": entry,
            "stop": stop, "target": target, "outcome": outcome, "exit_time": clock(mins(last["start_ist"]) + 5),
            "exit": stop if outcome == "stop" else target if outcome == "target" else last["close"],
            "r_gross": round(r, 4)}


# ---------------------------------------------------------------- the spread

def stats_of(rs):
    """Win rate, expectancy, worst peak-to-trough drawdown (R) and longest run of losses, in this order."""
    cum = peak = dd = 0.0
    run = streak = 0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        run = run + 1 if r < 0 else 0
        streak = max(streak, run)
    return {"win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 4), "expectancy_r": round(cum / len(rs), 4),
            "worst_drawdown_r": round(dd, 4), "longest_losing_streak": streak}


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def bootstrap(rs, samples=SAMPLES, seed=SEED):
    """`samples` resamples of rs (same count, with replacement): median, 5th and 95th percentile of each stat."""
    rng = random.Random(seed)
    runs = [stats_of(rng.choices(rs, k=len(rs))) for _ in range(samples)]
    out = {"samples": samples, "seed": seed, "trades_per_sample": len(rs)}
    for k in runs[0]:
        col = sorted(s[k] for s in runs)
        out[k] = {"median": pct(col, 0.5), "p5": pct(col, 0.05), "p95": pct(col, 0.95)}
    return out


# ---------------------------------------------------------------- options

def premium_r(snaps, t, loss_pct, target_r):
    """The trade's R on the option, from in-chain snapshots between its entry and exit times: the ATM CE
    (bullish) or PE (bearish) at the first snapshot, premium stop loss_pct below, target target_r x that."""
    # ponytail: ATM at entry, not in-chain's delta-band pick; use the pick rule once snapshots record it
    kind = "CE" if t["direction"] == "bullish" else "PE"
    live = [s for s in snaps if t["entry_time"] <= str(s.get("as_of_ist"))[11:16] <= t["exit_time"]]
    if not live:
        return None
    strike = live[0].get("atm")

    def ltp(s):
        return next((r.get("ltp") for r in s.get("rows") or [] if r.get("strike") == strike and r.get("type") == kind), None)
    entry = ltp(live[0])
    if not entry:
        return None
    risk, last = entry * loss_pct / 100.0, entry
    for s in live[1:]:
        px = ltp(s)
        if px is None:
            continue
        if px <= entry - risk:
            return -1.0
        if px >= entry + target_r * risk:
            return target_r
        last = px
    return (last - entry) / risk


def option_replay(folder, days, loss_pct, target_r, charges_r):
    files = sorted(glob.glob(os.path.join(folder, "*.jsonl")))
    out = {"snapshot_days": len(files), "replayed": len(files) >= MIN_DAYS}
    if not out["replayed"]:
        out["note"] = "option premium was not replayed: underlying only"
        return out
    by_day = {os.path.basename(f)[:-6]: f for f in files}
    rs = []
    for t in days:
        if t["status"] == "trade" and t["date"] in by_day:
            with open(by_day[t["date"]], encoding="utf-8") as fh:
                r = premium_r([json.loads(x) for x in fh if x.strip()], t, loss_pct, target_r)
            if r is not None:
                t["option_r"] = round(r - charges_r, 4)
                rs.append(t["option_r"])
    out.update(priced=len(rs), premium_stop_pct=loss_pct, history=stats_of(rs) if rs else None)
    return out


# ---------------------------------------------------------------- the run

def plan_charges(plan, ch):
    """One lot bought at the plan's entry premium and sold at its premium stop: rupees, and R of r_value_inr."""
    try:
        inst, buy, sell = plan["instrument"], plan["entry_premium"], plan["premium_stop"]
        lot, seg = int(inst["lot_size"]), inst["segment"]
        total = (smart.option_charges("BUY", buy, lot, seg, ch)["total"]
                 + smart.option_charges("SELL", sell, lot, seg, ch)["total"])
        r_value = Decimal(str(plan["r_value_inr"]))
        in_r = round(float(total / r_value), 4)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise smart.SmartError("trader/plan.json lacks instrument.lot_size and segment, entry_premium, premium_stop "
                               "or r_value_inr: run in-exit again")
    return {"round_trip_inr": float(total), "r_value_inr": float(r_value), "in_r": in_r,
            "tradingsymbol": inst.get("tradingsymbol"), "entry_premium": buy, "premium_stop": sell,
            "lot_size": lot, "charges_verified": bool(ch.get("verified"))}


def compute(a):
    p = smart.trader_paths(a.trader)
    cfg = smart.load_config(a.trader)
    cal = smart.read_json(p["calendar"]) or {}
    now = smart.now_ist(a.now)
    if a.days < MIN_DAYS:
        raise smart.SmartError("--days wants at least %d trading days" % MIN_DAYS)
    plan = smart.read_json(p["plan"])
    sym = (a.symbol or (plan or {}).get("symbol") or "NIFTY").upper()
    plan_note = None
    if plan and str(plan.get("symbol", "")).upper() != sym:
        plan, plan_note = None, "plan.json is for %s" % plan.get("symbol")

    session, ex = cfg.get("session") or {}, cfg.get("exit") or {}
    stop_text = a.stop or (plan or {}).get("underlying_stop_rule") or "or-mid"
    stop = parse_stop(stop_text)
    if stop is None:
        raise smart.SmartError("stop rule %r is not one the replay knows: pass --stop or-mid, or-opposite, pct:X "
                               "or atr:K" % stop_text)
    rule = {"range_start": "09:15", "range_end": session.get("range_end", "09:30"),
            "entry_start": session.get("entry_start", "09:30"), "entry_end": session.get("entry_end", "11:30"),
            "flat_by": session.get("flat_by", "15:10"),
            "expiry_last_entry": session.get("expiry_day_last_entry", "11:00"),
            "expiry_flat_by": session.get("expiry_day_flat_by", "14:45"),
            "stop": stop[0], "stop_k": stop[1],
            "stop_from": "--stop" if a.stop else "plan.json" if plan and plan.get("underlying_stop_rule") else "default",
            "target_r": float((plan or {}).get("target_r") or ex.get("target_r", 2.0))}
    max_gap = float((cfg.get("timemachine") or {}).get("max_gap_pct", MAX_GAP_PCT))

    offline_master = os.path.join(a.offline, "master.json") if a.offline else None
    if offline_master and not os.path.exists(offline_master):
        raise smart.SmartError("offline: no master.json in %s" % a.offline)
    if not a.offline:
        smart.require(a.trader, "getCandleData")
    master = smart.load_master(p["cache"], now.date().isoformat(), path=offline_master)
    facts = smart.underlying_facts(master, sym, now.date(), None)
    if not facts["index_token"]:
        raise smart.SmartError("no index named %s in the instrument master" % sym)
    expiries = {smart.expiry_date(r.get("expiry")) for r in master
                if r.get("instrumenttype") == "OPTIDX" and r.get("name") == sym} - {None}

    end = start = smart.previous_trading_day(now.date(), cal)
    for _ in range(a.days - 1):
        start = smart.previous_trading_day(start, cal)
    client, limiter = smart.open_client(a.trader, now, a.offline)
    cache = os.path.join(p["cache"], "offline") if a.offline else p["cache"]   # a replay never fills the real cache
    got, partial = smart.candles(client, cache, facts["index_segment"], facts["index_token"], "FIVE_MINUTE",
                                 start, end, now)
    asked = [d.isoformat() for d in (start + datetime.timedelta(n) for n in range((end - start).days + 1))
             if smart.trading_day(d, cal)]
    used = [d for d in asked if got.get(d)]
    if len(used) < MIN_DAYS:
        raise smart.SmartError("only %d day(s) of %s bars from %s to %s%s: the replay needs at least %d"
                               % (len(used), sym, start, end, " (%s)" % partial if partial else "", MIN_DAYS))
    bars = {d: [{"start_ist": r[0][11:16], "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
                for r in sorted(got[d])] for d in used}
    gaps = [(d, round(abs(bars[d][0]["open"] / bars[prev][-1]["close"] - 1) * 100, 2))
            for prev, d in zip(used, used[1:])]
    gap_day, gap = max(gaps, key=lambda g: g[1]) if gaps else (None, 0.0)
    if gap > max_gap:
        raise smart.SmartError("%s skipped: an overnight gap of %.2f %% into %s is over timemachine.max_gap_pct %g, "
                               "likely a corporate action; no replay" % (sym, gap, gap_day, max_gap))

    charges = plan_charges(plan, smart.read_json(p["charges"]) or {}) if plan else None
    charges_r = charges["in_r"] if charges else 0.0
    days = []
    for d in used:
        t = dict(date=d, **replay_day(bars[d], rule, datetime.date.fromisoformat(d) in expiries))
        if t["status"] == "trade":
            t["r"] = round(t["r_gross"] - charges_r, 4)
        days.append(t)
    trades = [t for t in days if t["status"] == "trade"]
    if not trades:
        raise smart.SmartError("%s: the rule never fired on the %d days with bars from %s to %s: nothing to resample"
                               % (sym, len(used), used[0], used[-1]))
    rs = [t["r"] for t in trades]
    option = option_replay(os.path.join(p["history"], "chain", sym), days,
                           float(ex.get("max_premium_loss_pct", 30)), rule["target_r"], charges_r)
    first_expiry = min(expiries) if expiries else now.date()
    history = dict(stats_of(rs), trades=len(trades), days=len(used),
                   no_trigger=sum(1 for t in days if t["status"] == "no_trigger"),
                   no_range=sum(1 for t in days if t["status"] == "no_range"),
                   bullish=sum(1 for t in trades if t["direction"] == "bullish"),
                   **{k: sum(1 for t in trades if t["outcome"] == k) for k in ("target", "stop", "flat_by")})
    return {"created_by": "in-timemachine", "as_of_ist": now.isoformat(),
            "source": "getCandleData FIVE_MINUTE, through the candle cache" + (" (offline replay)" if a.offline else ""),
            "replay_of_the_past": True, "note": NOT_FORECAST, "partial": partial,
            "symbol": sym, "exchange": facts["index_segment"], "token": facts["index_token"],
            "window": {"from": start.isoformat(), "to": end.isoformat(), "days_asked": len(asked),
                       "days_used": len(used), "newest_bar": "%s %s" % (used[-1], bars[used[-1]][-1]["start_ist"]),
                       "empty": [d for d in asked if d in got and not got[d]],
                       "failed": [d for d in asked if d not in got]},
            "largest_gap": {"date": gap_day, "pct": gap, "max_gap_pct": max_gap},
            "rule": rule, "expiry_days": [d for d in used if datetime.date.fromisoformat(d) in expiries],
            "master_expiries_from": first_expiry.isoformat(),
            "charges": charges, "charges_note": None if charges else "charges were not applied (%s)" % (
                plan_note or "no trader/plan.json"),
            "option": option, "history": history, "bootstrap": bootstrap(rs), "days": days}, limiter


# ---------------------------------------------------------------- text

def rupees(x):
    return "Rs {:,.2f}".format(x)


def report(doc):
    w, ru, h, b, ch, o = doc["window"], doc["rule"], doc["history"], doc["bootstrap"], doc["charges"], doc["option"]
    now = smart.parse_ist(doc["as_of_ist"])
    stop = STOP_WORDS.get(ru["stop"]) or ("%g %% from the entry" if ru["stop"] == "pct" else
                                          "%g x ATR14 of the day's bars to the entry") % ru["stop_k"]
    out = ["Time Machine, %s: a replay of the past, not a forecast (%s IST)" % (doc["symbol"], smart.stamp(now)),
           "  " + smart.NOT_ADVICE,
           "  %-13s %s index token %s: %s; newest bar %s, today left out" % (
               "Data", doc["exchange"], doc["token"], doc["source"], w["newest_bar"]),
           "  %-13s %s to %s: %d trading days by the calendar, %d with bars%s" % (
               "Window", w["from"], w["to"], w["days_asked"], w["days_used"],
               "; no bars on %s (a holiday calendar.json does not list?)" % ", ".join(w["empty"]) if w["empty"] else "")]
    if doc["partial"]:
        out.append("  %-13s PARTIAL: %d of %d days used; %s" % ("Partial", w["days_used"], w["days_asked"],
                                                                 doc["partial"]))
    g = doc["largest_gap"]
    out.append("  %-13s largest overnight gap %.2f %%%s, under timemachine.max_gap_pct %g" % (
        "Gaps", g["pct"], " (%s)" % g["date"] if g["date"] else "", g["max_gap_pct"]))
    out.append("  %-13s opening range %s to %s; the first 5-minute close beyond it, %s to %s" % (
        "Rule", ru["range_start"], ru["range_end"], ru["entry_start"], ru["entry_end"]))
    out.append("  %-13s stop at %s (%s), target %gR, flat by %s; a bar that touches both counts as the stop" % (
        "", stop, ru["stop_from"], ru["target_r"], ru["flat_by"]))
    said = ["%s from the master: last entry %s, flat by %s" % (", ".join(doc["expiry_days"]), ru["expiry_last_entry"],
                                                                ru["expiry_flat_by"])] if doc["expiry_days"] else []
    if doc["master_expiries_from"] > w["from"]:
        said.append("days before %s are not in today's master and used flat by %s" % (doc["master_expiries_from"],
                                                                                      ru["flat_by"]))
    out.append("  %-13s %s" % ("Expiry days", "; ".join(said) or "none in the window, by the master"))
    if ch:
        out.append("  %-13s %s for one lot bought and sold (%s, %d qty, %.2f in, %.2f stop) = %.4f R of %s, "
                   "off every trade%s" % ("Charges", rupees(ch["round_trip_inr"]), ch["tradingsymbol"], ch["lot_size"],
                                          ch["entry_premium"], ch["premium_stop"], ch["in_r"], rupees(ch["r_value_inr"]),
                                          "" if ch["charges_verified"] else "; charges.json not verified"))
    else:
        out.append("  %-13s %s" % ("Charges", doc["charges_note"]))
    if o["replayed"]:
        out.append("  %-13s premium replayed from %d days of your chain snapshots: %d of %d trades priced "
                   "(ATM CE or PE at entry, premium stop %g %%)" % ("Options", o["snapshot_days"], o["priced"],
                                                                    h["trades"], o["premium_stop_pct"]))
        if o["history"]:
            out.append("  %-13s win rate %.1f %%, expectancy %+.3f R a trade%s" % (
                "", 100 * o["history"]["win_rate"], o["history"]["expectancy_r"], ", after charges" if ch else ""))
    else:
        out.append("  %-13s %s (%d days of chain snapshots, %d needed)" % ("Options", o["note"], o["snapshot_days"],
                                                                           MIN_DAYS))
    skipped = ", %d without an opening range" % h["no_range"] if h["no_range"] else ""
    out += ["History, %d days: %d trades (%d bullish, %d bearish), %d no trigger%s" % (
                h["days"], h["trades"], h["bullish"], h["trades"] - h["bullish"], h["no_trigger"], skipped),
            "  %-15s %d target, %d stop, %d flat-by" % ("outcomes", h["target"], h["stop"], h["flat_by"]),
            "  %-15s %.1f %%" % ("win rate", 100 * h["win_rate"]),
            "  %-15s %+.3f R a trade%s" % ("expectancy", h["expectancy_r"], ", after charges" if ch else ""),
            "  %-15s %.2f R" % ("worst drawdown", h["worst_drawdown_r"]),
            "  %-15s %d trades" % ("losing streak", h["longest_losing_streak"]),
            "Resampled %s times (%d trades drawn with replacement, seed %d): median, 5th to 95th percentile" % (
                "{:,}".format(b["samples"]), b["trades_per_sample"], b["seed"])]
    fmt = {"win_rate": lambda x: "%.1f %%" % (100 * x), "expectancy_r": lambda x: "%+.3f R" % x,
           "worst_drawdown_r": lambda x: "%.2f R" % x, "longest_losing_streak": lambda x: "%d" % x}
    names = {"win_rate": "win rate", "expectancy_r": "expectancy", "worst_drawdown_r": "worst drawdown",
             "longest_losing_streak": "losing streak"}
    for k, f in fmt.items():
        s = b[k]
        out.append("  %-15s %-9s (%s to %s)" % (names[k], f(s["median"]), f(s["p5"]), f(s["p95"])))
    out.append(doc["note"])
    return out


# ---------------------------------------------------------------- commands

def show(path):
    return path.replace(os.sep, "/")


def cmd_run(a):
    doc, limiter = compute(a)
    lines = report(doc)
    if a.write:
        path = os.path.join(a.trader, "timemachine.json")
        smart.write_json(path, doc)
        lines.append("Wrote %s" % show(path))
    lines.append(smart.usage_text(limiter))
    print("\n".join(lines))
    return 0


def cmd_show(a):
    path = os.path.join(a.trader, "timemachine.json")
    doc = smart.read_json(path)
    if not doc or doc.get("created_by") != "in-timemachine":
        raise smart.SmartError("no saved replay in %s: run timemachine.py run --write first" % show(path))
    print("\n".join(report(doc) + ["Shown from %s, saved %s IST; nothing fetched." % (
        show(path), smart.stamp(smart.parse_ist(doc["as_of_ist"])))]))
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    common.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-24 18:00"')
    ap = argparse.ArgumentParser(description="in-timemachine: replay the pack's rule on past days (no orders).")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("run", parents=[common], help="replay the last N trading days and resample them")
    s.add_argument("--symbol", help="NIFTY, SENSEX or BANKNIFTY (default plan.json's symbol, else NIFTY)")
    s.add_argument("--days", type=int, default=60, help="trading days before today (default 60)")
    s.add_argument("--stop", help="or-mid, or-opposite, pct:X or atr:K (default plan.json's rule, else or-mid)")
    s.add_argument("--write", action="store_true", help="save trader/timemachine.json")
    s.add_argument("--offline", metavar="DIR", help="replay saved replies (DIR/getCandleData.json, DIR/master.json)")
    sub.add_parser("show", parents=[common], help="print the saved trader/timemachine.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "show": cmd_show}[a.cmd](a)
    except smart.SmartError as e:
        print("in-timemachine: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
