#!/usr/bin/env python3
"""in-entry: the opening-range breakout trigger for NIFTY, SENSEX and BANKNIFTY.
Standard library only. Read-only market data: it never places an order.

    python in-entry/entry.py check NIFTY
    python in-entry/entry.py check NIFTY --filter volume --filter trend
    python in-entry/entry.py watch NIFTY --every 30          # until triggered, expired or stale
    python in-entry/entry.py check NIFTY --offline tests/fixtures/in-entry --now "2026-11-25 09:41"

The rule: the opening range is the high and low of the closed 5-minute bars
from 09:15 to session.range_end (09:30). The trigger is the first CLOSED
5-minute bar after the range, inside the entry window (session.entry_start
to session.entry_end, 09:30 to 11:30: the bar closes by the end), whose close
is above the range high (bullish: buy a CE) or below the range low (bearish:
buy a PE). A close equal to an edge is not beyond it. The bar in progress
never counts. Bars come from the live feed, else getCandleData.

Statuses: waiting, triggered, expired (window over, no trigger), no_signal
(not a trading day in trader/calendar.json), stale (the feed's last tick for
the index is older than freshness.stale_tick_sec: no signal on stale data).
One entry per symbol per day: a trigger already in entry.json is reported
again, never re-made. Every check writes trader/entry.json, except that a
non-trigger never overwrites today's trigger.

Optional confirmations (--filter, off by default): volume (the nearest index
future's bar volume against the same bar's 20-day average, at least
entry.min_volume_ratio), vwap (the future's close on the right side of its
VWAP), trend (NIFTY and SENSEX beyond their ranges on the same side). With
filters the trigger is the first close beyond the range that passes them all.

Exit codes: check 0 = a result (any status), 1 = an error; watch 0 = triggered, 1 = anything else.
"""

import argparse
import collections
import datetime
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

BAR = 5
FILTERS = ("volume", "vwap", "trend")
PEERS = ("NIFTY", "SENSEX")
HISTORY_DAYS, HISTORY_SPAN = 20, 42     # 20 trading days fit in 6 calendar weeks
FINAL = ("triggered", "expired", "stale", "no_signal")


def show(path):
    return path.replace(os.sep, "/")


def px(x):
    return "{:,.2f}".format(x)


def add(hm, minutes):
    return (datetime.datetime.strptime(hm, "%H:%M") + datetime.timedelta(minutes=minutes)).strftime("%H:%M")


def mins(hm):
    return int(hm[:2]) * 60 + int(hm[3:5])


def settings(cfg):
    s, f, e = cfg.get("session") or {}, cfg.get("freshness") or {}, cfg.get("entry") or {}
    return {"range_end": s.get("range_end", "09:30"), "start": s.get("entry_start", "09:30"),
            "end": s.get("entry_end", "11:30"), "last_entry": s.get("expiry_day_last_entry", "11:00"),
            "flat_expiry": s.get("expiry_day_flat_by", "14:45"), "stale_sec": float(f.get("stale_tick_sec", 3)),
            "gap_pct": float(e.get("gap_pct", 1.0)), "min_volume_ratio": float(e.get("min_volume_ratio", 1.0))}


def nearest_future(rows, name, today):
    futs = [(smart.expiry_date(r.get("expiry")), r) for r in rows
            if r.get("instrumenttype") == "FUTIDX" and r.get("name") == name]
    futs = [(d, r) for d, r in futs if d and d >= today]
    return min(futs, key=lambda x: x[0])[1] if futs else None


def bars_today(a, p, client, seg, token, now):
    """smart.day_bars: a feed that started after 09:15 is passed over for candles (late_feed says when)."""
    return smart.day_bars(a.trader, seg, token, BAR, now, client)


# ---------------------------------------------------------------- filters

def volume_average(client, p, seg, token, now, cal):
    """{"HH:MM": the bar's average volume over the last 20 trading days with data}, days used, partial."""
    end = smart.previous_trading_day(now.date(), cal)
    days, partial = smart.candles(client, p["cache"], seg, token, "FIVE_MINUTE",
                                  end - datetime.timedelta(days=HISTORY_SPAN), end, now)
    used = [rows for _, rows in sorted(days.items()) if rows][-HISTORY_DAYS:]
    by = collections.defaultdict(list)
    for rows in used:
        for r in rows:
            by[str(r[0])[11:16]].append(r[5] or 0)
    return {k: sum(v) / len(v) for k, v in by.items()}, len(used), partial


def filter_data(names, a, p, client, rows, sym, now, cal):
    out = {}
    if "volume" in names or "vwap" in names:
        fut = nearest_future(rows, sym, now.date())
        out["fut"] = fut
        if fut:
            seg = smart.segment(fut["exch_seg"])
            out["fut_bars"] = bars_today(a, p, client, seg, fut["token"], now)["bars"]
            if "volume" in names:
                out["avg"] = volume_average(client, p, seg, fut["token"], now, cal)
    if "trend" in names:
        out["peers"] = {}
        for peer in PEERS:
            f = smart.underlying_facts(rows, peer, now.date(), None)
            if peer != sym and f["index_token"]:
                pb = bars_today(a, p, client, f["index_segment"], f["index_token"], now)["bars"]
                out["peers"][peer] = (smart.opening_range(pb), {b["start_ist"]: b for b in pb})
    return out


def run_filter(name, bar, side, fd, st):
    """(passed, why) for one filter on one candidate bar."""
    t = bar["start_ist"]
    if name in ("volume", "vwap") and not fd.get("fut"):
        return False, "%s: no index future in the master" % name
    if name == "volume":
        fb = next((b for b in fd["fut_bars"] if b["start_ist"] == t), None)
        avg, n, partial = fd["avg"]
        if not fb or fb["volume"] is None or not avg.get(t):
            return False, "volume: no %s volume for the %s bar%s" % (fd["fut"]["symbol"], t,
                                                                    " (%s)" % partial if partial else "")
        ratio = fb["volume"] / avg[t]
        return ratio >= st["min_volume_ratio"], "volume %.2fx the %d-day average of the %s bar (%s, needs %.2fx)" % (
            ratio, n, t, fd["fut"]["symbol"], st["min_volume_ratio"])
    if name == "vwap":
        upto = [b for b in fd["fut_bars"] if b["start_ist"] <= t and b["volume"]]
        if not upto or upto[-1]["start_ist"] != t:
            return False, "vwap: no %s volume up to the %s bar" % (fd["fut"]["symbol"], t)
        vwap = sum((b["high"] + b["low"] + b["close"]) / 3 * b["volume"] for b in upto) / sum(b["volume"] for b in upto)
        close = upto[-1]["close"]
        return close > vwap if side == "bullish" else close < vwap, "vwap: %s closed %s, %s its VWAP %s" % (
            fd["fut"]["symbol"], px(close), "above" if close > vwap else "below" if close < vwap else "at", px(vwap))
    bits, ok = [], bool(fd["peers"])
    for peer, (rng, by) in sorted(fd["peers"].items()):
        b = by.get(t)
        if not rng or not b:
            ok = False
            bits.append("no %s %s" % (peer, "range" if not rng else "%s bar" % t))
            continue
        ok = ok and (b["close"] > rng["high"] if side == "bullish" else b["close"] < rng["low"])
        bits.append("%s closed %s, range %s to %s" % (peer, px(b["close"]), px(rng["low"]), px(rng["high"])))
    return ok, "trend: " + ("; ".join(bits) or "no peer index in the master")


# ---------------------------------------------------------------- the check

def trigger_text(rec):
    up = rec["direction"] == "bullish"
    return "TRIGGERED %s (buy a %s): the %s bar closed %s at %s, %s the range %s %s" % (
        rec["direction"], "CE" if up else "PE", rec["bar_start_ist"], px(rec["bar_close"]), rec["bar_close_ist"],
        "above" if up else "below", "high" if up else "low", px(rec["trigger_level"]))


def report(rec, now, source, status, st):
    rng = rec["opening_range"]
    out = ["in-entry %s, %s IST (%s)" % (rec["symbol"], smart.stamp(now), source), "  " + smart.NOT_ADVICE]
    if rec["status"] != "no_signal":
        out.append("  %-15s %s" % ("Range", "%s to %s: high %s, low %s" % (rng["start"], rng["end"], px(rng["high"]),
                                                                           px(rng["low"])) if rng else "not complete"))
        out.append("  %-15s %s to %s, closed 5-minute bars only" % ("Window", st["start"], st["end"]))
    out.append("  %-15s %s" % ("Status", status))
    if rec["status"] != "no_signal":
        out.append("  %-15s %s" % ("Filters", ", ".join(rec["filters_applied"]) or "none (the plain rule)"))
    out += ["  %-15s %s" % ("Note", n) for n in rec["notes"]]
    return out


def check(a, now):
    """One look at the rule: (record, report lines, status text)."""
    p = smart.trader_paths(a.trader)
    cfg = smart.load_config(a.trader)
    cal = smart.read_json(p["calendar"], {}) or {}
    st = settings(cfg)
    sym, today, hhmm = a.symbol.upper(), now.date(), now.strftime("%H:%M")
    rec = {"created_by": "in-entry", "symbol": sym, "date": today.isoformat(), "status": None, "direction": None,
           "trigger_level": None, "bar_start_ist": None, "bar_close_ist": None, "bar_close": None,
           "opening_range": None, "filters_applied": list(dict.fromkeys(a.filter)), "notes": [],
           "feed_lag_ms": None, "source": None, "as_of_ist": now.isoformat()}
    old = smart.read_json(p["entry"]) or {}
    old_trigger = old if old.get("status") == "triggered" and old.get("date") == rec["date"] else None
    source, usage = "calendar.json", None

    def done(status):
        return finish(a, p, rec, report(rec, now, source, status, st), status, old_trigger, usage)

    if not smart.trading_day(today, cal):
        rec.update(status="no_signal", source="calendar.json")
        return done("NO SIGNAL  not a trading day (%s)" % smart.market_data_allowed(now, cal)[1])
    if old_trigger and old["symbol"] == sym:
        status = trigger_text(old)
        lines = report(old, now, "entry.json as of %s" % old["as_of_ist"][11:16], status, st)
        lines.append("Already triggered today: one entry per symbol per day, so it is not re-made. "
                     "%s unchanged." % show(p["entry"]))
        lines.append(next_step(old))
        return old, lines, status

    rows = smart.load_master(p["cache"], rec["date"],
                             path=os.path.join(a.offline, "master.json") if a.offline else None)
    f = smart.underlying_facts(rows, sym, today, None)
    if not f["index_token"]:
        raise smart.SmartError("%s is not an index in today's instrument master: version 1 covers NIFTY, SENSEX and "
                               "BANKNIFTY options" % sym)
    seg, token = f["index_segment"], f["index_token"]
    if f["expiries"] and f["expiries"][0][0] == rec["date"]:
        rec["notes"].append("expiry day: %s options expire today; earlier cutoffs apply, last entry %s and flat by %s "
                            "(config session.expiry_day_last_entry and expiry_day_flat_by)"
                            % (sym, st["last_entry"], st["flat_expiry"]))
    if not a.offline:
        smart.require(a.trader, "getCandleData")
    client, limiter = smart.open_client(a.trader, now, a.offline)
    usage = smart.usage_text(limiter)
    smart.subscribe(a.trader, "in-entry:" + sym, [{"segment": seg, "token": token, "mode": "quote"}])

    prev = smart.previous_trading_day(today, cal)
    days, why = smart.candles(client, p["cache"], seg, token, "ONE_DAY", prev, prev, now)
    prev_close = days[prev.isoformat()][-1][4] if days.get(prev.isoformat()) else None

    d = bars_today(a, p, client, seg, token, now)
    bars = d["bars"]
    rec["source"] = d["source"]
    if d.get("late_feed"):
        rec["notes"].append("the feed's first bar today is %s, after the open: bars from getCandleData"
                            % d["late_feed"])
    if d["partial"] and not bars:
        raise smart.SmartError("no 5-minute bars for %s today: %s" % (sym, d["partial"]))
    source = "getCandleData, closed 5-minute bars"
    if d["source"] == "feed":
        tick = smart.read_latest(p["db"], [token], int(now.timestamp() * 1000), st["stale_sec"] * 1000).get(str(token))
        rec["feed_lag_ms"] = tick["lag_ms"] if tick else None
        source = "feed, lag %d ms, last tick %.1f s old" % (tick["lag_ms"], max(0, tick["age_ms"]) / 1000.0) \
            if tick else "feed, no tick for %s" % sym
        if tick is None or tick["stale"]:
            rec["status"] = "stale"
            return done("STALE  the feed's last %s tick is %s (limit %g s): no signal on stale data" % (
                sym, "%.1f s old" % (tick["age_ms"] / 1000.0) if tick else "missing", st["stale_sec"]))

    if bars and bars[0]["start_ist"] == "09:15":
        if prev_close:
            gap = (bars[0]["open"] / prev_close - 1) * 100
            if abs(gap) > st["gap_pct"]:
                rec["notes"].append("gap day: opened %s, %+.2f%% from the previous close %s (%s); entry.gap_pct is %g"
                                    % (px(bars[0]["open"]), gap, px(prev_close), prev, st["gap_pct"]))
        else:
            rec["notes"].append("gap check skipped: no ONE_DAY candle for %s%s" % (prev, " (%s)" % why if why else ""))

    rng = smart.opening_range(bars, end=st["range_end"])
    if rng is None:
        rec["status"] = "waiting"
        return done("WAITING  the opening range is forming: %d of %d bars closed, complete at %s" % (
            len([b for b in bars if b["start_ist"] < st["range_end"]]), (mins(st["range_end"]) - mins("09:15")) // BAR,
            st["range_end"]))
    rec["opening_range"] = {k: rng[k] for k in ("high", "low", "start", "end")}

    fd = filter_data(rec["filters_applied"], a, p, client, rows, sym, now, cal) if rec["filters_applied"] else {}
    first, hit, rejected = max(st["range_end"], st["start"]), None, []
    for b in bars:
        if b["start_ist"] < first or add(b["start_ist"], BAR) > st["end"]:
            continue
        side = "bullish" if b["close"] > rng["high"] else "bearish" if b["close"] < rng["low"] else None
        if not side:
            continue
        results = [run_filter(n, b, side, fd, st) for n in rec["filters_applied"]]
        if all(ok for ok, _ in results):
            hit = (b, side, [w for _, w in results])
            break
        rejected.append((b, [w for ok, w in results if not ok]))
    if rejected:
        b, whys = rejected[-1]
        rec["notes"].append("%d close%s beyond the range not confirmed; the last, the %s bar: %s" % (
            len(rejected), "" if len(rejected) == 1 else "s", b["start_ist"], "; ".join(whys)))

    if hit:
        b, side, confirms = hit
        rec.update(status="triggered", direction=side, trigger_level=rng["high"] if side == "bullish" else rng["low"],
                   bar_start_ist=b["start_ist"], bar_close_ist=add(b["start_ist"], BAR), bar_close=b["close"])
        rec["notes"] += ["confirmed by " + w for w in confirms]
        status = trigger_text(rec)
    elif hhmm >= st["end"]:
        rec["status"] = "expired"
        status = "EXPIRED  no confirmed 5-minute close beyond the range from %s to %s" % (first, st["end"])
    else:
        rec["status"] = "waiting"
        last = bars[-1]
        status = ("WAITING  last close %s (%s bar) %s; a 5-minute close above %s or below %s triggers" % (
            px(last["close"]), last["start_ist"], "is inside the range" if rng["low"] <= last["close"] <= rng["high"]
            else "is beyond the range, not confirmed", px(rng["high"]), px(rng["low"])))
        if hhmm < first:
            status += " from %s" % first
    return done(status)


def next_step(rec):
    return ("Next: in-chain pick a %s %s, then in-exit plan. Nothing was placed: this pack never places an order."
            % (rec["symbol"], "CE" if rec["direction"] == "bullish" else "PE"))


def finish(a, p, rec, lines, status, old, usage):
    """Write entry.json (never a non-trigger over today's trigger) and close the report."""
    if old and rec["status"] != "triggered":
        lines.append("Kept %s: it holds today's %s trigger." % (show(p["entry"]), old["symbol"]))
    else:
        smart.write_json(p["entry"], rec)
        lines.append("Wrote %s" % show(p["entry"]))
    if rec["status"] == "triggered":
        lines.append(next_step(rec))
    if rec["status"] in ("triggered", "expired"):   # done for the day: the feed may drop the index
        smart.subscribe(a.trader, "in-entry:" + rec["symbol"], [])
    if usage:
        lines.append(usage)
    return rec, lines, status


# ---------------------------------------------------------------- commands

def cmd_check(a):
    _, lines, _ = check(a, smart.now_ist(a.now))
    print("\n".join(lines))
    return 0


def cmd_watch(a):
    """Re-check every --every seconds. A pinned --now runs a simulated clock: no waiting."""
    now = smart.now_ist(a.now)
    while True:
        rec, lines, status = check(a, now)
        if rec["status"] in FINAL:
            print("\n".join(lines), flush=True)
            return 0 if rec["status"] == "triggered" else 1
        print("%s  %s" % (now.strftime("%H:%M:%S"), status), flush=True)
        if a.now:
            now += datetime.timedelta(seconds=a.every)
        else:
            time.sleep(a.every)
            now = smart.now_ist()


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("symbol", help="NIFTY, SENSEX or BANKNIFTY")
    common.add_argument("--filter", action="append", default=[], choices=FILTERS,
                        help="an optional confirmation, repeatable (off by default)")
    common.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    common.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-25 09:41"')
    common.add_argument("--offline", metavar="DIR", help="replay saved replies and DIR/master.json")
    ap = argparse.ArgumentParser(description="in-entry: the opening-range breakout trigger (read-only, no orders).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check", parents=[common], help="one look: waiting, triggered, expired, no_signal or stale")
    s = sub.add_parser("watch", parents=[common], help="re-check until triggered, expired or stale")
    s.add_argument("--every", type=int, default=30, metavar="SEC", help="seconds between checks (default 30)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch}[a.cmd](a)
    except smart.SmartError as e:
        print("in-entry: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
