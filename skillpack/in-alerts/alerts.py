#!/usr/bin/env python3
"""Alerts on open trades for in-alerts. Standard library only. It reads the
live feed's file (trader/state/ticks.sqlite) and calls no API, rings the
terminal bell (with --notify also a desktop notification), and never places
an order.

    python in-alerts/alerts.py check                     # once; exit 0 = something new rang
    python in-alerts/alerts.py watch --every 5           # every 5 s until 15:40 IST (F&O close)
    python in-alerts/alerts.py check --notify --now "2026-11-24 10:20"
    python in-alerts/alerts.py check --offline DIR       # DIR/master.json as the instrument master

It watches the open trades in trader/state/journal.json (filled with in-ticket,
not closed) against their ticket's levels in trader/orders.json:

    stop_premium       premium at or below the premium stop
    stop_underlying    underlying through its stop (below for a CE, above for a PE)
    target_premium     premium at or above the premium target
    target_underlying  underlying at its target
    flat_by            the flat-by time is reached
    flat_by_soon       10 minutes or less before the flat-by time
    expiry_day         the contract expires today
    no_progress        in-exit's no-progress exit: the first no_progress_bars closed 5-minute bars of
                       the underlying after the fill hold no close beyond entry_underlying (above
                       for a CE, below for a PE); both come from the ticket. Bars from the feed only
                       (smart.read_bars); a bar missing or short of minutes, or a stale underlying,
                       decides nothing and the report says so
    stale_premium      no tick for freshness.stale_tick_sec: no stop or target decided on it
    stale_underlying   the same for the underlying index

Prices come only from smart.read_latest on the option and index tokens, bars
from smart.read_bars on the index (the index token from today's instrument
master, smart.load_master, or DIR/master.json with --offline); it asks the feed
for them through smart.subscribe. Each ticket's alert kind rings once per IST
day (trader/state/alerted.json). Nothing runs on a non-trading day
(calendar.json) or outside session.fo_open to session.fo_close (09:15 to 15:40 IST).

Exit codes: check 0 = something new rang, 1 = nothing new or nothing to watch
(one line why); watch 0 = ran until the F&O close, 1 = closed today.
"""

import argparse
import datetime
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

KINDS = ("stop_premium", "stop_underlying", "target_premium", "target_underlying", "flat_by", "flat_by_soon",
         "expiry_day", "no_progress", "stale_premium", "stale_underlying")
SOON_MIN = 10
BAR_MS = 5 * 60000   # in-exit counts its no-progress bars in 5-minute bars
OWNER = "in-alerts"
PS_BALLOON = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
              "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Warning; "
              "$n.Visible = $true; $n.ShowBalloonTip(10000, 'in-alerts', $env:IN_ALERTS_TEXT, 'Warning'); "
              "Start-Sleep -Seconds 10; $n.Dispose()")
FOOTER = ("An alert is information, never an order: if you act, do it yourself in the Angel One app, "
          "then record it with ticket.py close.")


def show_path(path):
    return path.replace(os.sep, "/")


def price(x):
    return "{:,.2f}".format(float(x))


def window(cfg):
    s = cfg.get("session") or {}
    return s.get("fo_open", "09:15"), s.get("fo_close", "15:40")


# ---------------------------------------------------------------- the rules

def tag(label, q):
    """The price with its feed lag and age, or that there is none."""
    if q is None:
        return "%s (no tick yet)" % label
    return "%s %s (lag %d ms, age %.1f s%s)" % (label, price(q["ltp"]), q["lag_ms"], q["age_ms"] / 1000.0,
                                                ", STALE" if q["stale"] else "")


def evaluate(trade, order, opt, und, has_underlying, now, flat, stale_sec):
    """[(kind, text)] for every alert whose condition holds now; dedupe comes later.
    A stale or missing price decides no stop and no target."""
    inst = trade["instrument"]
    pt, ut = tag("premium", opt), tag(inst["underlying"], und)
    out = []

    def stale(kind, text, q):
        out.append((kind, "%s: %s, so no stop or target is decided on it" % (
            text, "nothing from the feed for this token (is in-common/feed.py running?)" if q is None
            else "no tick for over %g s" % stale_sec)))

    if opt is None or opt["stale"]:
        stale("stale_premium", pt, opt)
    else:
        if order.get("premium_stop") is not None and opt["ltp"] <= order["premium_stop"]:
            out.append(("stop_premium", "%s is at or below the stop %s" % (pt, price(order["premium_stop"]))))
        if order.get("target_premium") is not None and opt["ltp"] >= order["target_premium"]:
            out.append(("target_premium", "%s is at or above the target %s" % (pt, price(order["target_premium"]))))
    if has_underlying:
        if und is None or und["stale"]:
            stale("stale_underlying", ut, und)
        else:
            up = inst["type"] == "CE"   # a CE is stopped below its level, a PE above
            stop, target = order.get("underlying_stop"), order.get("target_underlying")
            if stop is not None and (und["ltp"] <= stop if up else und["ltp"] >= stop):
                out.append(("stop_underlying", "%s is at or %s the stop %s" % (ut, "below" if up else "above", price(stop))))
            if target is not None and (und["ltp"] >= target if up else und["ltp"] <= target):
                out.append(("target_underlying", "%s is at or %s the target %s" % (ut, "above" if up else "below",
                                                                                   price(target))))
    both = "%s; %s" % (pt, ut) if has_underlying else pt
    left = (now.replace(hour=int(flat[:2]), minute=int(flat[3:]), second=0, microsecond=0) - now).total_seconds() / 60
    if left <= 0:
        out.append(("flat_by", "flat-by %s reached (%s): close it by hand in the Angel One app; %s"
                    % (flat, now.strftime("%H:%M"), both)))
    elif left <= SOON_MIN:
        out.append(("flat_by_soon", "flat-by %s in %d min; %s" % (flat, math.ceil(left), both)))
    if inst.get("expiry") == now.date().isoformat():
        out.append(("expiry_day", "%s expires today; %s" % (inst["tradingsymbol"], both)))
    return out


def hm(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, smart.IST).strftime("%H:%M")


def first_bar_ms(trade):
    """The start of the 5-minute bar (aligned to 09:15) holding the fill: its close is the first after it."""
    fill = smart.parse_ist("%s %s" % (trade["date"], trade["fill_time_ist"]))
    anchor = int(fill.replace(hour=9, minute=15).timestamp() * 1000)
    return anchor + (int(fill.timestamp() * 1000) - anchor) // BAR_MS * BAR_MS


def no_progress(trade, order, bars, und, closed_to):
    """(alert text or None, report line). in-exit's rule: out when the first `no_progress_bars` closed
    5-minute bars after the fill hold no close beyond `entry_underlying` in the trade's direction.
    `bars` are smart.read_bars rows from first_bar_ms(trade), closed up to the exchange time `closed_to`.
    A missing bar, one short of minutes, or a stale underlying decides nothing."""
    level, n, name = order.get("entry_underlying"), order.get("no_progress_bars"), trade["instrument"]["underlying"]
    if level is None or not n:
        return None, ("no progress: not watched, the ticket has no entry_underlying or no_progress_bars "
                      "(in-budget copies them from plan.json)")
    up = trade["instrument"]["type"] == "CE"
    side = "above" if up else "below"
    want = [first_bar_ms(trade) + i * BAR_MS for i in range(int(n))]
    got = {b[0]: b for b in bars if b[7] == BAR_MS // 60000 and b[0] in want}   # every minute in the feed
    span = "%s to %s" % (hm(want[0]), hm(want[-1] + BAR_MS))
    for start in want:
        b = got.get(start)
        if b and (b[4] > level if up else b[4] < level):
            return None, "no progress: cleared, %s closed %s the entry %s at %s" % (
                name, side, price(level), hm(start + BAR_MS))
    if und is None or und["stale"]:
        return None, "no progress: not decided, no fresh %s tick" % name
    due = [s for s in want if s + BAR_MS <= closed_to]
    if len(got) < len(due):
        return None, "no progress: not decided, the feed has %d of the %d closed %s 5-minute bars of %s" % (
            len(got), len(due), name, span)
    if len(due) < len(want):
        return None, "no progress: %d of %d five-minute bars since the fill closed, none %s the entry %s (due %s)" % (
            len(due), len(want), side, price(level), hm(want[-1] + BAR_MS))
    text = "%s has not closed %s the entry %s in the %d five-minute bars %s: the plan's no-progress exit" % (
        name, side, price(level), len(want), span)
    return text, "no progress: due, " + text


# ---------------------------------------------------------------- one check

def check_once(p, cfg, now, idx, offline=None):
    """(report lines, new alerts [(ticket_id, kind, text)], open trades). `idx` caches
    {underlying: (index token, segment)} from the master across a watch."""
    trades = [t for t in (smart.read_json(p["journal"]) or {}).get("trades", [])
              if t.get("fill_price") is not None and t.get("exit_price") is None]
    if not trades:
        smart.subscribe(p["trader"], OWNER, [])
        return ["  No open trades in %s." % show_path(p["journal"])], [], 0
    orders = {o.get("ticket_id"): o for o in (smart.read_json(p["orders"]) or {}).get("orders", [])}
    names = {t["instrument"]["underlying"] for t in trades} - set(idx)
    if names:
        rows = smart.load_master(p["cache"], now.date().isoformat(),
                                 path=os.path.join(offline, "master.json") if offline else None)
        for n in names:
            f = smart.underlying_facts(rows, n, now.date(), None)
            idx[n] = (f["index_token"], f["index_segment"])
    items = []
    for t in trades:
        inst = t["instrument"]
        items.append({"segment": inst["segment"], "token": str(inst["token"]), "mode": "ltp"})
        tok, seg = idx[inst["underlying"]]
        if tok:
            items.append({"segment": seg, "token": str(tok), "mode": "ltp"})
    smart.subscribe(p["trader"], OWNER, items)
    stale_sec = (cfg.get("freshness") or {}).get("stale_tick_sec", 3)
    latest = smart.read_latest(p["db"], [i["token"] for i in items], int(now.timestamp() * 1000), stale_sec * 1000)
    default_flat = (cfg.get("session") or {}).get("flat_by", "15:10")
    closed_to = min(smart.exchange_clock(p["db"]) or 0, int(now.timestamp() * 1000))

    watched, holding = [], []
    for t in trades:
        inst, o = t["instrument"], orders.get(t["ticket_id"])
        if o is None:
            watched.append((t, None, None, None, None, [], None))
            continue
        tok = idx[inst["underlying"]][0]
        opt, und = latest.get(str(inst["token"])), latest.get(str(tok)) if tok else None
        flat = t.get("flat_by") or default_flat
        found = evaluate(t, o, opt, und, bool(tok), now, flat, stale_sec)
        np_line = None
        if tok:
            text, np_line = no_progress(t, o, smart.read_bars(p["db"], tok, 5, first_bar_ms(t), closed_to), und, closed_to)
            if text:
                found.append(("no_progress", text))
        watched.append((t, o, opt, und, flat, found, np_line))
        holding += [(t["ticket_id"], kind, text) for kind, text in found]

    day = now.date().isoformat()
    with smart.FileLock(p["alerted"] + ".lock"):
        rung = (smart.read_json(p["alerted"], {}) or {}).get(day, [])
        new = [h for h in holding if "%s:%s" % h[:2] not in rung]
        if new:   # only today's list is kept
            smart.write_json(p["alerted"], {day: rung + ["%s:%s" % h[:2] for h in new]})

    lines = []
    for t, o, opt, und, flat, found, np_line in watched:
        inst = t["instrument"]
        head = "  %s %s: %s bought at %.2f (%s)" % (t["ticket_id"], inst["tradingsymbol"], "{:,}".format(t["quantity"]),
                                                   t["fill_price"], t["fill_time_ist"])
        if o is None:
            lines.append(head + ": no ticket in orders.json, so no levels to watch")
            continue
        lines += [head + ", flat by " + flat,
                  "    %s: stop %s, target %s" % (tag("premium", opt), price(o["premium_stop"]), price(o["target_premium"]))]
        if idx[inst["underlying"]][0]:
            lines.append("    %s: stop %s, target %s" % (tag(inst["underlying"], und), price(o["underlying_stop"]),
                                                         price(o["target_underlying"])))
        else:
            lines.append("    %s index is not in the instrument master: the underlying is not watched" % inst["underlying"])
        if np_line:
            lines.append("    " + np_line)
        earlier = [k for k, _ in found if "%s:%s" % (t["ticket_id"], k) in rung]
        if earlier:
            lines.append("    rang earlier today: " + ", ".join(earlier))
    return lines, new, len(trades)


def alert_lines(new):
    return ["ALERT %s %s: %s" % h for h in new]


# ---------------------------------------------------------------- ringing

def notify_command(text, platform=None):
    """The desktop notification for this platform as (argv, extra environment), or None.
    The text travels as an argument or an environment variable, never inside a script."""
    platform = platform or sys.platform
    if platform == "darwin":
        return ["osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "in-alerts"',
                "-e", "end run", text], {}
    if platform.startswith("linux"):
        return ["notify-send", "in-alerts", text], {}
    if platform == "win32":
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_BALLOON], {"IN_ALERTS_TEXT": text[:250]}
    return None


def ring(new, notify):
    """The terminal bell, and with notify one desktop notification. Returns a note when that failed."""
    if not new:
        return None
    sys.stdout.write("\a")
    sys.stdout.flush()
    if not notify:
        return None
    cmd = notify_command("\n".join(alert_lines(new)))
    if cmd is None:
        return "no desktop notification on %s: the terminal bell rang" % sys.platform
    try:  # ponytail: started and not waited for, so only a launch failure is seen; wait on it if exits matter
        subprocess.Popen(cmd[0], env=dict(os.environ, **cmd[1]), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, ValueError) as e:
        return "desktop notification failed (%s): the terminal bell rang" % e
    return None


# ---------------------------------------------------------------- commands

def header(p, cfg, now):
    return ["Alerts check, %s IST (source: live feed %s; stale after %g s)" % (
        smart.stamp(now), show_path(p["db"]), (cfg.get("freshness") or {}).get("stale_tick_sec", 3)),
            "  " + smart.NOT_ADVICE]


def cmd_check(a, p, cfg, cal, now):
    ok, why = smart.market_data_allowed(now, cal, *window(cfg))
    if not ok:
        raise smart.SmartError("F&O %s, nothing watched" % why)
    lines, new, n = check_once(p, cfg, now, {}, a.offline)
    if not n:
        raise smart.SmartError("no open trades in %s: nothing to watch (ticket.py fill records one)"
                               % show_path(p["journal"]))
    print("\n".join(header(p, cfg, now) + lines + alert_lines(new) + [FOOTER if new else "Nothing new rang."]))
    note = ring(new, a.notify)
    if note:
        print("Note: " + note)
    return 0 if new else 1


def cmd_watch(a, p, cfg, cal, now):
    start, end = window(cfg)
    if not smart.trading_day(now.date(), cal) or now.strftime("%H:%M") >= end:   # before the open it waits
        raise smart.SmartError("F&O %s, nothing watched" % smart.market_data_allowed(now, cal, start, end)[1])
    print("Watching open trades every %d s until %s IST (F&O close)%s. Ctrl-C stops it."
          % (a.every, end, "; the clock is simulated from --now" if a.now else ""))
    idx, first, noted = {}, True, False
    try:
        while now.strftime("%H:%M") < end:
            if now.strftime("%H:%M") >= start:
                lines, new, _ = check_once(p, cfg, now, idx, a.offline)
                if first:
                    print("\n".join(header(p, cfg, now) + lines + alert_lines(new)))
                    first = False
                elif new:
                    print("\n".join("%s %s" % (now.strftime("%H:%M:%S"), line) for line in alert_lines(new)))
                note = ring(new, a.notify)
                if note and not noted:
                    print("Note: " + note)
                    noted = True
            if a.now:   # a pinned clock steps on without waiting (tests and replays)
                now += datetime.timedelta(seconds=a.every)
            else:
                time.sleep(a.every)
                now = smart.now_ist()
    except KeyboardInterrupt:
        print("Watch stopped at %s IST." % smart.now_ist().strftime("%H:%M"))
        return 0
    print("Watch ended at %s IST: F&O is closed for the day. %s" % (end, FOOTER))
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    common.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-24 10:20"')
    common.add_argument("--notify", action="store_true", help="also a desktop notification")
    common.add_argument("--offline", metavar="DIR", help="read DIR/master.json as the instrument master")
    ap = argparse.ArgumentParser(description="in-alerts: alerts on open trades from the live feed (no orders).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check", parents=[common], help="check once")
    s = sub.add_parser("watch", parents=[common], help="check every --every seconds until the F&O close")
    s.add_argument("--every", type=int, default=5, help="seconds between checks (default 5)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if a.cmd == "watch" and a.every < 1:
        print("in-alerts: --every wants 1 second or more", file=sys.stderr)
        return 1
    try:
        now = smart.now_ist(a.now)
        cfg = smart.load_config(a.trader)
        p = smart.trader_paths(a.trader)
        cal = smart.read_json(p["calendar"]) or {}
        return {"check": cmd_check, "watch": cmd_watch}[a.cmd](a, p, cfg, cal, now)
    except smart.SmartError as e:
        print("in-alerts: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
