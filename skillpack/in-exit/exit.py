#!/usr/bin/env python3
"""in-exit: the exit plan for today's trigger, set BEFORE the buy. Standard library only.
Read-only: it never places an order.

    python in-exit/exit.py plan                          # or-mid stop, premium stop, 2R targets, flat-by
    python in-exit/exit.py plan --stop atr:1.5 --write   # writes trader/plan.json
    python in-exit/exit.py plan --entry-premium 115.20   # the premium you expect instead of the pick's
    python in-exit/exit.py show                          # the saved trader/plan.json

Inputs: trader/entry.json (triggered today) and trader/pick.json (in-chain pick:
the instrument and premium, same underlying and direction, picked at or after
the trigger bar's close).

Underlying stop (--stop): or-mid (default, the opening-range midpoint),
or-opposite (the other side of the range), atr:K (K x ATR14 of today's
5-minute bars up to the entry bar; with fewer than 14 bars it averages them
all), pct:X (X percent from the entry), rounded to 0.05 away from the entry.
Premium stop: exit.max_premium_loss_pct (30) under the entry premium, rounded
down to the instrument's tick. Targets: exit.target_r (2.0) times the risk on
both, rounded half up. Out on whichever stop is hit first.

Time: flat by session.flat_by (15:10), on expiry day session.expiry_day_flat_by
(14:45). No-progress exit after exit.no_progress_bars (6) five-minute bars.
r_value_inr = (entry premium - premium stop) x lot size: one lot.

Refused, with the reason: no trigger today, a missing, stale or mismatched
pick, a stop on the wrong side, a zero risk, a plan at or after flat-by.
Exit codes: plan 0 = a plan, 1 = refused or an error; show 0 = a plan exists.
"""

import argparse
import datetime
import os
import sys
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

BAR = 5
ATR_BARS = 14
INDEX_TICK = Decimal("0.05")
PAISA = Decimal("0.01")
SQUARE_OFF = "Angel One's auto square-off from 3 Aug 2026 is 15:10 equity MIS, 15:20 equity F&O"


def D(x):
    return Decimal(str(x))


def px(x):
    return "{:,.2f}".format(x)


def exact(x):
    """2 decimals, or 3 when the third is not zero: the unrounded level before the tick."""
    q = x.quantize(Decimal("0.001"))
    return px(q) if q == q.quantize(PAISA) else "{:,.3f}".format(q)


def show(path):
    return path.replace(os.sep, "/")


def add(hm, minutes):
    return (datetime.datetime.strptime(hm, "%H:%M") + datetime.timedelta(minutes=minutes)).strftime("%H:%M")


def inputs(p, today):
    """(entry, pick, instrument), or a refusal that names the next step."""
    entry = smart.read_json(p["entry"])
    if not entry:
        raise smart.SmartError("no %s: run in-entry check first" % show(p["entry"]))
    if entry.get("status") != "triggered" or entry.get("date") != today:
        raise smart.SmartError("entry.json is %s for %s on %s: in-exit plans only today's trigger (run in-entry check)"
                               % (entry.get("status"), entry.get("symbol"), entry.get("date")))
    sym, side = entry["symbol"], entry["direction"]
    kind = "CE" if side == "bullish" else "PE"
    again = "run in-chain pick for a %s %s" % (sym, kind)
    pick = smart.read_json(p["pick"])
    if not pick:
        raise smart.SmartError("no %s: %s first" % (show(p["pick"]), again))
    inst = pick.get("instrument") or {}
    if (pick.get("underlying"), pick.get("direction"), inst.get("type")) != (sym, side, kind):
        raise smart.SmartError("pick.json is a %s %s %s, the entry is %s %s: %s" % (
            pick.get("underlying"), pick.get("direction"), inst.get("type"), sym, side, again))
    trigger = smart.parse_ist("%s %s" % (entry["date"], entry["bar_close_ist"]))
    if not pick.get("as_of_ist") or smart.parse_ist(pick["as_of_ist"]) < trigger:
        raise smart.SmartError("pick.json is from %s, before the %s trigger: %s again" % (
            pick.get("as_of_ist"), entry["bar_close_ist"], again))
    return entry, pick, inst


def atr(a, p, entry, now):
    """(ATR of the last 14 true ranges up to the entry bar, bars used, source, limiter)."""
    rows = smart.load_master(p["cache"], now.date().isoformat(),
                             path=os.path.join(a.offline, "master.json") if a.offline else None)
    f = smart.underlying_facts(rows, entry["symbol"], now.date(), None)
    if not f["index_token"]:
        raise smart.SmartError("%s is not an index in today's instrument master" % entry["symbol"])
    if not a.offline:
        smart.require(a.trader, "getCandleData")
    client, limiter = smart.open_client(a.trader, now, a.offline)
    d = smart.day_bars(a.trader, f["index_segment"], f["index_token"], BAR, now, client)
    bars = [b for b in d["bars"] if b["start_ist"] <= entry["bar_start_ist"]]
    if not bars or bars[-1]["start_ist"] != entry["bar_start_ist"]:
        raise smart.SmartError("no %s 5-minute bars up to the %s entry bar (%s): use another --stop" % (
            entry["symbol"], entry["bar_start_ist"], d["partial"] or "not in the data"))
    trs, prev = [], None
    for b in bars:
        h, l = D(b["high"]), D(b["low"])
        trs.append(h - l if prev is None else max(h - l, abs(h - prev), abs(l - prev)))
        prev = D(b["close"])
    last = trs[-ATR_BARS:]
    return sum(last) / len(last), len(last), d["source"], limiter


def underlying_stop(a, p, entry, now):
    """(unrounded stop, how it was found, limiter or None)."""
    up, e = entry["direction"] == "bullish", D(entry["bar_close"])
    hi, lo = D(entry["opening_range"]["high"]), D(entry["opening_range"]["low"])
    rule = a.stop
    if rule == "or-mid":
        return (hi + lo) / 2, "opening-range midpoint (%s + %s) / 2" % (px(hi), px(lo)), None
    if rule == "or-opposite":
        return (lo, "the opening-range low", None) if up else (hi, "the opening-range high", None)
    try:
        k = D(rule[4:]) if rule[:4] in ("atr:", "pct:") else None
    except InvalidOperation:
        k = None
    if k is None or not k.is_finite():
        raise smart.SmartError("--stop is or-mid, or-opposite, atr:K or pct:X, got %r" % rule)
    sign = 1 if up else -1
    if rule.startswith("pct:"):
        return e * (1 - sign * k / 100), "%s%% %s the entry" % (k, "under" if up else "over"), None
    value, n, source, limiter = atr(a, p, entry, now)
    return e - sign * k * value, "%s x ATR %s (the last %d of today's 5-minute bars up to the entry bar%s; %s)" % (
        k, px(value), n, "" if n == ATR_BARS else ", fewer than %d" % ATR_BARS, source), limiter


def make_plan(a, p, cfg, now):
    today = now.date().isoformat()
    entry, pick, inst = inputs(p, today)
    ex, sess = cfg.get("exit") or {}, cfg.get("session") or {}
    sym, side = entry["symbol"], entry["direction"]
    up = side == "bullish"
    expiry_day = inst.get("expiry") == today
    flat = sess.get("expiry_day_flat_by", "14:45") if expiry_day else sess.get("flat_by", "15:10")
    at = max(entry["bar_close_ist"], now.strftime("%H:%M"))
    if at >= flat:
        raise smart.SmartError("it is %s, at or after the flat-by time %s%s: no new trade today" % (
            at, flat, " (expiry day)" if expiry_day else ""))

    e = D(entry["bar_close"])
    raw, how, limiter = underlying_stop(a, p, entry, now)
    ustop = smart.round_tick(raw, INDEX_TICK, "down" if up else "up")
    urisk = (e - ustop) if up else (ustop - e)
    if urisk < 0:
        raise smart.SmartError("the %s stop %s is on the wrong side of the entry %s: a %s stop goes %s it" % (
            a.stop, px(ustop), px(e), side, "below" if up else "above"))
    if urisk == 0:
        raise smart.SmartError("the %s stop %s equals the entry: zero risk, choose a wider --stop"
                               % (a.stop, px(ustop)))
    r = D(ex.get("target_r", 2.0))
    utarget = smart.round_tick(e + r * urisk if up else e - r * urisk, INDEX_TICK)

    prem = D(a.entry_premium if a.entry_premium is not None else pick["premium"])
    tick, loss = D(inst["tick_size"]), D(ex.get("max_premium_loss_pct", 30))
    if prem <= 0:
        raise smart.SmartError("the entry premium must be above zero, got %s" % prem)
    pstop = smart.round_tick(prem * (1 - loss / 100), tick, "down")
    if pstop >= prem:
        raise smart.SmartError("the premium stop %s is not below the entry premium %s: zero risk, "
                               "exit.max_premium_loss_pct must be above 0" % (pstop, prem))
    if pstop <= 0:
        raise smart.SmartError("the premium stop %s is at or below zero: set exit.max_premium_loss_pct under 100, "
                               "or this premium is too small to plan" % pstop)
    prisk = prem - pstop
    ptarget = smart.round_tick(prem + r * prisk, tick)
    lot = int(inst["lot_size"])
    n = int(ex.get("no_progress_bars", 6))
    plan = {"created_by": "in-exit", "symbol": sym, "date": today, "direction": side, "instrument": inst,
            "entry_underlying": float(e), "entry_premium": float(prem),
            "underlying_stop": float(ustop), "underlying_stop_rule": a.stop.replace("-", "_"),
            "premium_stop": float(pstop), "premium_stop_rule": "max_premium_loss_pct %g" % loss,
            "target_r": float(r), "target_underlying": float(utarget), "target_premium": float(ptarget),
            "risk_per_unit_inr": float(prisk), "r_value_inr": float((prisk * lot).quantize(PAISA)),
            "flat_by": flat, "expiry_day": expiry_day, "no_progress_bars": n, "tick_size": float(tick),
            "as_of_ist": now.isoformat(), "source": "entry.json + pick.json"}
    why = ("%s = %s, rounded %s to 0.05" % (how, exact(raw), "down" if up else "up"),
           "%g%% under the entry premium = %s, rounded down to the %s tick" % (loss, exact(prem * (1 - loss / 100)),
                                                                                tick))
    source = "entry.json (the %s bar closed %s at %s) + pick.json (%s)" % (
        entry["bar_start_ist"], px(e), entry["bar_close_ist"], "premium %s from --entry-premium" % px(prem)
        if a.entry_premium is not None else "premium at %s" % pick["as_of_ist"][11:16])
    by = add(entry["bar_close_ist"], BAR * n)
    return plan, lines(plan, now, "plan", source, why, by), limiter


def row(label, text):
    return "  %-15s %s" % (label, text)


def lines(plan, now, title, source=None, why=None, by=None):
    """The report from plan.json alone; plan adds the source, how each stop was found and the clock time."""
    i, up = plan["instrument"], plan["direction"] == "bullish"
    out = ["in-exit %s: %s %s, buy %s, %s IST" % (title, plan["symbol"], plan["direction"], i["tradingsymbol"],
                                                  smart.stamp(now)), "  " + smart.NOT_ADVICE]
    if source:
        out.append(row("Source", source))
    out.append(row("Instrument", "%s (%s), expiry %s%s, lot %s, tick %s" % (
        i["tradingsymbol"], i["segment"], i["expiry"], " (today)" if plan["expiry_day"] else "", i["lot_size"],
        plan["tick_size"])))
    out.append(row("Underlying", "entry %s, stop %s, target %s (%gR)" % (
        px(plan["entry_underlying"]), px(plan["underlying_stop"]), px(plan["target_underlying"]), plan["target_r"])))
    if why:
        out.append(row("", "stop: " + why[0]))
    out.append(row("Premium", "entry %s, stop %s, target %s (%gR)" % (
        px(plan["entry_premium"]), px(plan["premium_stop"]), px(plan["target_premium"]), plan["target_r"])))
    if why:
        out.append(row("", "stop: " + why[1]))
    n = plan["no_progress_bars"]
    out += [row("Risk", "Rs %s a unit, Rs %s for one lot of %s (1R)" % (
                px(plan["risk_per_unit_inr"]), px(plan["r_value_inr"]), i["lot_size"])),
            row("Exit", "on whichever stop is hit first; a stop is a plan, not a guarantee"),
            row("Flat by", "%s IST%s, win or lose (%s)" % (
                plan["flat_by"], " on expiry day" if plan["expiry_day"] else "", SQUARE_OFF)),
            row("No progress", "out if %s has not closed %s %s %s: the premium decays while it waits" % (
                plan["symbol"], "above" if up else "below", px(plan["entry_underlying"]),
                "by %s (%d five-minute bars after the entry)" % (by, n) if by
                else "within %d five-minute bars of the entry" % n))]
    return out


def cmd_plan(a):
    p = smart.trader_paths(a.trader)
    plan, out, limiter = make_plan(a, p, smart.load_config(a.trader), smart.now_ist(a.now))
    if a.write:
        smart.write_json(p["plan"], plan)
        out.append("Wrote %s" % show(p["plan"]))
    else:
        out.append("Not saved: add --write to save %s" % show(p["plan"]))
    out.append("Next: in-budget sizes it in lots, then in-risk checks it. Nothing was placed: this pack never "
               "places an order.")
    if a.stop.startswith("atr:"):
        out.append(smart.usage_text(limiter))
    print("\n".join(out))
    return 0


def cmd_show(a):
    p = smart.trader_paths(a.trader)
    now = smart.now_ist(a.now)
    plan = smart.read_json(p["plan"])
    if not plan:
        raise smart.SmartError("no %s: run in-exit plan --write" % show(p["plan"]))
    out = lines(plan, now, "plan saved %s" % plan["as_of_ist"][11:16])
    if plan["date"] != now.date().isoformat():
        out.append("  %-15s this plan is for %s, not today" % ("Note", plan["date"]))
    print("\n".join(out))
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    common.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-25 09:42"')
    ap = argparse.ArgumentParser(description="in-exit: stops, targets and flat-by before the buy (no orders).")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("plan", parents=[common], help="the plan for today's trigger and pick")
    s.add_argument("--stop", default="or-mid", help="or-mid (default), or-opposite, atr:K or pct:X")
    s.add_argument("--entry-premium", type=float, metavar="PRICE", help="use this premium instead of pick.json's")
    s.add_argument("--write", action="store_true", help="save trader/plan.json")
    s.add_argument("--offline", metavar="DIR", help="replay saved replies and DIR/master.json (atr:K only)")
    sub.add_parser("show", parents=[common], help="the saved trader/plan.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except smart.SmartError as e:
        print("in-exit: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
