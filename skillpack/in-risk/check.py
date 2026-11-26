#!/usr/bin/env python3
"""in-risk: the gate. PASS or BLOCK for every DRAFT ticket in trader/orders.json.
Standard library only, no REST call. It never places an order.

    python in-risk/check.py                            # today's DRAFT tickets not already blocked
    python in-risk/check.py --ticket T20261124-01      # one ticket
    python in-risk/check.py --now "2026-11-24 09:42"   # pin the clock (IST)
    python in-risk/check.py --offline DIR              # DIR/master.json as the instrument master

Every check runs and prints its numbers; a BLOCK lists every failing reason.
The verdict goes into the ticket's risk_result. A BLOCK is final for that
ticket: it is never checked again, and there is no override flag (change
trader/config.json by hand, then size a new ticket). An exit is never blocked.

Reads: orders.json, config.json, pick.json, calendar.json,
events.json, ban.txt, state/journal.json, the feed (state/ticks.sqlite) and
today's instrument master (trader/cache, downloaded once a day if missing; with
--offline DIR, DIR/master.json as in-chain and in-entry read it).

Exit codes: 0 = every checked ticket passed, 1 = a BLOCK, nothing to check, or an error.
"""

import argparse
import datetime
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

BLOCK_EVENT_KINDS = ["rbi", "budget", "election"]


def D(v):
    return Decimal(str(v))


def n(x):
    return "{:,.2f}".format(x)


def setting(cfg, section, key):
    v = (cfg.get(section) or {}).get(key)
    if v is None:
        raise smart.SmartError("set %s.%s in trader/config.json" % (section, key))
    return v


def next_trading_day(day, calendar):
    day += datetime.timedelta(days=1)
    while not smart.trading_day(day, calendar):
        day += datetime.timedelta(days=1)
    return day


def underlying_row(rows, row, name):
    """The index (AMXIDX) for an index option, the NSE cash share for a stock option."""
    if row and row.get("instrumenttype") == "OPTSTK":
        return next((r for r in rows if smart.segment(r.get("exch_seg")) == "NSE" and r.get("symbol") == name + "-EQ"),
                    None)
    return next((r for r in rows if r.get("instrumenttype") == "AMXIDX" and r.get("name") == name), None)


def show(v, f="{:,}"):
    return "unknown" if v is None else f.format(v)


def checks(o, x):
    """Every rule for one BUY ticket: [{"name", "ok", "detail"}], numbers included."""
    out = []

    def add(name, ok, detail):
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    cfg, capital, now, cal = x["cfg"], x["capital"], x["now"], x["calendar"]
    today, hhmm = now.date(), now.strftime("%H:%M")
    inst = o["instrument"]
    sym, token, lot = inst["tradingsymbol"], str(inst["token"]), int(inst["lot_size"])
    lots, qty, qtys = int(o["lots"]), int(o["quantity"]), list(o["order_quantities"])
    limit, stop = D(o["limit_price"]), D(o["premium_stop"])
    risk = o.get("risk_inr") or {}
    total = D(risk.get("total", "0"))
    at_stop = (limit - stop) * qty
    ok = (lots * lot == qty == sum(qtys) and 0 < stop < limit and D(risk.get("premium_at_stop", "-1")) == at_stop
          and D(risk.get("charges_round_trip", "-1")) >= 0 and total == at_stop + D(risk["charges_round_trip"]))
    add("ticket", ok, "%d lots x %d = %d units in %s; %.2f - %.2f stop = %.2f a unit, %s at the stop%s"
        % (lots, lot, qty, qtys, limit, stop, limit - stop, n(at_stop),
           "" if ok else ": the numbers disagree (edited after sizing?): size it again with in-budget"))

    rows = x["rows"]
    row = x["master"].get((smart.segment(inst["segment"]), token))
    kind = row.get("instrumenttype") if row else None
    if row is None:
        add("instrument", False, "%s (token %s) is not in today's instrument master: expired or a wrong token"
            % (sym, token))
    else:
        mlot = int(float(row.get("lotsize") or 0))
        add("instrument", mlot == lot, "%s %s in today's master, lot %d%s"
            % (sym, kind, mlot, "" if mlot == lot else ", the ticket says %d: size it again with in-budget" % lot))

    acct = "account"
    risk_pct = D(setting(cfg, acct, "risk_per_trade_pct"))
    max_loss = capital * risk_pct / 100
    add("max_loss_per_trade", total <= max_loss, "%s of %s (%g%% of capital %s)"
        % (n(total), n(max_loss), risk_pct, n(capital)))

    trades = [t for t in x["trades"] if t.get("date") == today.isoformat()]
    closed = sorted((t for t in trades if t.get("net_pnl_inr") is not None), key=lambda t: t.get("exit_time_ist") or "")
    realised = sum((D(t["net_pnl_inr"]) for t in closed), Decimal(0))
    loss = max(-realised, Decimal(0))
    day_pct = D(setting(cfg, acct, "max_daily_loss_pct"))
    day_cap = capital * day_pct / 100
    # ponytail: open trades' risk is not added, only realised P&L; add it from FILLED tickets if a limit is ever missed
    add("daily_loss_limit", loss + total <= day_cap, "realised loss %s + this trade %s = %s of %s (%g%% of capital)"
        % (n(loss), n(total), n(loss + total), n(day_cap), day_pct))

    max_trades = int(setting(cfg, acct, "max_trades_per_day"))
    filled = sum(1 for t in trades if t.get("fill_price") is not None)
    add("trades_per_day", filled < max_trades, "%d filled today, limit %d" % (filled, max_trades))

    stop_after = int(setting(cfg, acct, "max_consecutive_losses"))
    streak = 0
    for t in closed:
        streak = streak + 1 if D(t["net_pnl_inr"]) < 0 else 0
    add("losses_in_a_row", streak < stop_after, "%d losses in a row today (%d closed), stop at %d"
        % (streak, len(closed), stop_after))

    start, end = setting(cfg, "session", "entry_start"), setting(cfg, "session", "entry_end")
    if not smart.trading_day(today, cal):
        why = (cal.get("holidays") or {}).get(today.isoformat()) or "weekend"
        add("trading_window", False, "%s is not a trading day (%s)" % (today, why))
    else:
        inside = start <= hhmm <= end
        add("trading_window", inside, "%s %s the entry window %s to %s IST, a trading day"
            % (hhmm, "inside" if inside else "outside", start, end))

    last = setting(cfg, "session", "expiry_day_last_entry")
    if inst["expiry"] == today.isoformat():
        add("expiry_day_cutoff", hhmm <= last, "expiry day: %s, last entry %s" % (hhmm, last))
    else:
        add("expiry_day_cutoff", True, "not the expiry day (expires %s)" % inst["expiry"])

    name = (o.get("symbol") or inst.get("underlying") or "").upper()
    if kind == "OPTIDX":
        add("fno_ban", True, "%s is an index: never in the F&O ban" % name)
    elif x["ban"] is None:
        add("fno_ban", False, "no trader/ban.txt: cannot check today's F&O ban for %s" % name)
    else:
        add("fno_ban", name not in x["ban"], "%s %s today's ban.txt (%d names)"
            % (name, "is in" if name in x["ban"] else "is not in", len(x["ban"])))

    und = underlying_row(rows, row, name)
    pair = [(sym, token), (name, und and str(und["token"]))]
    stale_sec = setting(cfg, "freshness", "stale_tick_sec")
    feed = smart.read_latest(x["db"], [t for _, t in pair if t], int(now.timestamp() * 1000), stale_sec * 1000)
    pick = x["pick"]
    pick_ok = bool(pick) and str((pick.get("instrument") or {}).get("token")) == token
    if not feed:
        ttl = setting(cfg, "freshness", "chain_ttl_sec")
        if not pick_ok:
            add("stale_data", False, "no feed ticks, and pick.json is not for %s" % sym)
        else:
            age = (now - smart.parse_ist(pick["as_of_ist"])).total_seconds()
            add("stale_data", age <= ttl, "no feed ticks; pick.json %.0f s old (limit %g s)" % (age, ttl))
    else:
        parts, fresh = [], True
        for label, tok in pair:
            tick = feed.get(tok)
            fresh = fresh and tick is not None and not tick["stale"]
            parts.append("%s %s" % (label, "%.1f s old" % (tick["age_ms"] / 1000.0) if tick else
                                    "no tick" if tok else "not in today's master"))
        lag = max(t["lag_ms"] for t in feed.values())
        add("stale_data", fresh, "%s (limit %g s); feed lag %d ms" % (", ".join(parts), stale_sec, lag))

    # the signal must still stand: a trigger the market has already reversed through the stop, or one past
    # the plan's own no-progress deadline without progress, is not an entry any more
    last = ((feed or {}).get(und and str(und["token"])) or {}).get("ltp")
    stop, base, up = o.get("underlying_stop"), o.get("entry_underlying"), o.get("direction") == "bullish"
    gone = []
    if last is not None and stop is not None and (last <= stop if up else last >= stop):
        gone.append("%s %s is already %s the stop %s" % (name, "{:,.2f}".format(last),
                                                         "at or below" if up else "at or above", "{:,.2f}".format(stop)))
    entry, bars_n = x.get("entry") or {}, o.get("no_progress_bars")
    if bars_n and entry.get("bar_close_ist") and entry.get("date") == today.isoformat() and base is not None:
        due = smart.parse_ist("%s %s" % (entry["date"], entry["bar_close_ist"])) + datetime.timedelta(minutes=5 * int(bars_n))
        moved = last is not None and (last > base if up else last < base)
        if now >= due and not moved:
            gone.append("the plan's no-progress deadline %s has passed (trigger closed %s at %s)"
                        % (due.strftime("%H:%M"), entry["bar_close_ist"], "{:,.2f}".format(base)))
    add("signal_valid", not gone, "; ".join(gone) or (
        "%s %s, the stop %s not reached" % (name, "{:,.2f}".format(last), "{:,.2f}".format(stop))
        if last is not None and stop is not None else "no live underlying price: the stop could not be compared"))

    kinds = (cfg.get("risk") or {}).get("block_event_kinds", BLOCK_EVENT_KINDS)
    if x["events"] is None:
        add("event_day", True, "no trader/events.json: no events to check")
    else:
        hits = [e for e in x["events"] if e.get("date") == today.isoformat() and e.get("kind") in kinds]
        add("event_day", not hits, "; ".join("%s (%s) today" % (e.get("name"), e.get("kind")) for e in hits)
            or "no event of kind %s today" % ", ".join(kinds))

    cap_pct = D(setting(cfg, acct, "max_capital_per_trade_pct"))
    cap = capital * cap_pct / 100
    add("lot_cost", limit * qty <= cap, "%d lots x %s = %s of %s (%g%% of capital)"
        % (lots, n(limit * lot), n(limit * qty), n(cap), cap_pct))

    max_spread, min_oi, min_vol = (setting(cfg, "options", k) for k in ("max_spread_pct", "min_oi", "min_volume"))
    if not pick_ok:
        add("spread_liquidity", False, "pick.json is not for %s: no spread, OI or volume to check" % sym)
    else:
        tick = feed.get(token) or {}
        spread, bid, ask = pick.get("spread_pct"), pick.get("bid"), pick.get("ask")
        if spread is None and bid and ask:
            spread = round((ask - bid) / ((ask + bid) / 2.0) * 100, 2)
        oi = pick.get("oi") if pick.get("oi") is not None else tick.get("oi")
        vol = pick.get("volume") if pick.get("volume") is not None else tick.get("cum_volume")
        ok = None not in (spread, oi, vol) and spread <= max_spread and oi >= min_oi and vol >= min_vol
        add("spread_liquidity", ok, "spread %s (max %g%%), OI %s (min %s), volume %s (min %s)"
            % (show(spread, "{:g}%"), max_spread, show(oi), show(min_oi), show(vol), show(min_vol)))

    product = o.get("product") or "INTRADAY"
    if kind != "OPTSTK":
        add("stock_expiry", True, "not a stock option: index options settle in cash")
    elif product != "CARRYFORWARD":
        add("stock_expiry", True, "stock option, %s: closed the same day" % product)
    else:
        nxt = next_trading_day(today, cal)
        ok = inst["expiry"] >= nxt.isoformat()
        add("stock_expiry", ok, "stock option, CARRYFORWARD, expires %s, next trading day %s%s"
            % (inst["expiry"], nxt, "" if ok else ": carried, it is held into expiry and in-the-money stock "
                                                  "options settle in shares"))
    return out


def verdict(o, x):
    if o.get("side") != "BUY":
        result = [{"name": "exit", "ok": True, "detail": "an exit is never blocked"}]
    else:
        result = checks(o, x)
    failed = [c for c in result if not c["ok"]]
    return {"verdict": "BLOCK" if failed else "PASS", "reasons": ["%s: %s" % (c["name"], c["detail"]) for c in failed],
            "checks": result, "checked_at_ist": x["now"].isoformat()}


def lines(o, r):
    inst = o["instrument"]
    out = ["%-5s %s  %s %s (%s), %s lot%s = %s units at %s, stop %s"
           % (r["verdict"], o["ticket_id"], o.get("side"), inst["tradingsymbol"], inst["segment"], o.get("lots"),
              "" if o.get("lots") == 1 else "s", o.get("quantity"), show(o.get("limit_price"), "{:.2f}"),
              show(o.get("premium_stop"), "{:.2f}"))]
    out += ["      %-4s %-19s %s" % ("ok" if c["ok"] else "FAIL", c["name"], c["detail"]) for c in r["checks"]]
    if r["reasons"]:
        out.append("      Blocked by %d: %s" % (len(r["reasons"]), ", ".join(c["name"] for c in r["checks"]
                                                                         if not c["ok"])))
    return out


def ban_list(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return {t.upper() for t in (s.strip() for s in fh) if t and not t.startswith("#")}
    except FileNotFoundError:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="in-risk: PASS or BLOCK for each DRAFT ticket (never places an order).")
    ap.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    ap.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-24 09:42"')
    ap.add_argument("--ticket", metavar="ID", help="check this ticket only")
    ap.add_argument("--offline", metavar="DIR", help="read DIR/master.json as the instrument master")
    a = ap.parse_args(argv)
    try:
        now = smart.now_ist(a.now)
        today = now.date().isoformat()
        p = smart.trader_paths(a.trader)
        cfg = smart.load_config(a.trader)
        capital = smart.require_account(cfg)
        orders = (smart.read_json(p["orders"]) or {}).get("orders", [])
        done = [o for o in orders if (o.get("risk_result") or {}).get("verdict") == "BLOCK"]
        if a.ticket:
            todo = [o for o in orders if o.get("ticket_id") == a.ticket]
            if not todo:
                raise smart.SmartError("no ticket %s in trader/orders.json" % a.ticket)
            if todo[0].get("status") != "DRAFT":
                raise smart.SmartError("%s is %s, not a DRAFT: only drafts are checked" % (a.ticket, todo[0].get("status")))
            if todo[0] in done:
                raise smart.SmartError("%s was blocked at %s: a BLOCK is final, size a new ticket with in-budget"
                                       % (a.ticket, todo[0]["risk_result"]["checked_at_ist"][11:16]))
            if todo[0].get("date") != today:
                raise smart.SmartError("%s is from %s, not today: size a new ticket with in-budget"
                                       % (a.ticket, todo[0].get("date")))
        else:
            todo = [o for o in orders if o.get("status") == "DRAFT" and o.get("date") == today and o not in done]
        if not todo:
            final = sum(1 for o in done if o.get("date") == today)
            raise smart.SmartError("no DRAFT ticket to check for %s in trader/orders.json%s: run in-budget first"
                                   % (today, " (%d blocked, final)" % final if final else ""))
        rows = smart.load_master(p["cache"], today, path=os.path.join(a.offline, "master.json") if a.offline else None)
        events = smart.read_json(p["events"])
        x = {"cfg": cfg, "capital": capital, "now": now, "calendar": smart.read_json(p["calendar"], {}) or {},
             "trades": (smart.read_json(p["journal"]) or {}).get("trades", []), "ban": ban_list(p["ban"]),
             "events": None if events is None else events.get("events", []), "pick": smart.read_json(p["pick"]),
             "entry": smart.read_json(p["entry"]),
             "db": p["db"], "rows": rows,
             "master": {(smart.segment(r.get("exch_seg")), str(r.get("token"))): r for r in rows}}
        results = {o["ticket_id"]: verdict(o, x) for o in todo}
        with smart.FileLock(p["orders"] + ".lock"):
            doc = smart.read_json(p["orders"]) or {"orders": []}
            for o in doc.get("orders", []):
                if o.get("ticket_id") in results:
                    o["risk_result"] = results[o["ticket_id"]]
            smart.write_json(p["orders"], doc)
        print("Risk check, %s IST, computed from orders.json, config.json, journal.json, pick.json, the feed and "
              "today's master" % smart.stamp(now))
        print("  " + smart.NOT_ADVICE)
        for o in todo:
            for line in lines(o, results[o["ticket_id"]]):
                print(line)
        blocked = sum(1 for r in results.values() if r["verdict"] == "BLOCK")
        print("%d passed, %d blocked. Wrote risk_result to %s/orders.json."
              % (len(results) - blocked, blocked, a.trader.replace(os.sep, "/")))
        print("A PASS is a rules check, not a recommendation. A BLOCK is final for that ticket.")
        return 1 if blocked else 0
    except smart.SmartError as e:
        print("in-risk: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
