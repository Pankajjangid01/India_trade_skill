#!/usr/bin/env python3
"""Manual order ticket and trade journal for in-ticket. Standard library only.
It sends nothing and calls no API: it prints what you type into the Angel One
app yourself, then records what you actually got.

    python in-ticket/ticket.py show [ID]                                   # tickets in-risk passed today
    python in-ticket/ticket.py fill ID --price 112.40 --time 09:41 [--qty 130]
    python in-ticket/ticket.py close ID --price 150.00 --time 10:55 --reason target
    python in-ticket/ticket.py skip ID                                     # a ticket you will not place
    python in-ticket/ticket.py journal                                     # today, the year, the CSV

Reads and updates trader/orders.json (status DRAFT, then FILLED and CLOSED,
or SKIPPED). Writes trader/state/journal.json (atomic) and regenerates
trader/journal_<year>.csv from it: closed trades with the columns an
accountant asks for. No tax advice. Charges come from trader/charges.json
through smart.option_charges: BUY at the fill, SELL at the exit, one
brokerage per executed order (a freeze split is several orders).

Exit codes: 0 = done, 1 = nothing to show or an error, in one line.
"""

import argparse
import csv
import datetime
import io
import os
import re
import sys
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

SEGMENT_CLASSES = ("FO_OPTIONS", "FO_FUTURES", "EQ_INTRADAY")  # version 1 records FO_OPTIONS only
REASONS = ("stop", "target", "flat_by", "no_progress", "manual")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CHARGE_PARTS = ("brokerage", "exchange", "sebi", "stt", "stamp", "gst")
NEAR_CLOSE = ["Near the close (SmartAPI documentation, Closing Auction Session, from 03 Aug 2026):",
              "  15:15 to 15:20  order entry through SmartAPI is rejected",
              "  15:15 to 15:40  stop-loss orders in CAS-eligible stocks are disallowed",
              "  15:20           Angel One auto square-off of equity F&O intraday orders"]
CSV_COLUMNS = ("ticket_id", "segment_class", "exchange", "tradingsymbol", "underlying", "option_type", "strike",
               "expiry", "product", "quantity", "executed_orders", "buy_date", "buy_time_ist", "buy_price",
               "buy_value_inr", "sell_date", "sell_time_ist", "sell_price", "sell_value_inr", "gross_pnl_inr",
               "brokerage_inr", "exchange_charges_inr", "sebi_fee_inr", "stt_inr", "stamp_duty_inr", "gst_inr",
               "charges_inr", "net_pnl_inr", "exit_reason", "financial_year")


def show_path(path):
    return path.replace(os.sep, "/")


def money(x):
    return "{:,.2f}".format(float(x))


def num(x):
    return ("%.2f" % float(x)).rstrip("0").rstrip(".")


def day_text(iso):
    return "%s %s" % (smart.WEEKDAYS[datetime.date.fromisoformat(iso).weekday()], iso)


def hhmm_of(iso):
    return smart.parse_ist(iso).strftime("%H:%M") if iso else "?"


def D(x):
    return Decimal(str(x))


def paisa(x):
    return float(D(x).quantize(smart.PAISA))


# ---------------------------------------------------------------- files

def load_orders(p):
    doc = smart.read_json(p["orders"])
    if not doc or not doc.get("orders"):
        raise smart.SmartError("no tickets in %s: run in-budget and in-risk first" % show_path(p["orders"]))
    return doc


def find(doc, tid):
    for o in doc["orders"]:
        if o.get("ticket_id") == tid:
            return o
    raise smart.SmartError("no ticket %s in trader/orders.json: ticket.py show lists them" % tid)


def save_journal(p, trades, now):
    """journal.json (atomic), then every year's CSV regenerated from it."""
    smart.write_json(p["journal"], {"created_by": "in-ticket", "as_of_ist": now.isoformat(),
                                    "source": "fills and exits you recorded by hand", "trades": trades})
    return write_csvs(p["trader"], trades)


def csv_row(t):
    inst, ch, q = t["instrument"], t["charges_detail"], t["quantity"]
    d = datetime.date.fromisoformat(t["exit_date"])
    fy = d.year if d.month >= 4 else d.year - 1   # the Indian financial year, April to March
    return ([t["ticket_id"], t["segment_class"], inst["segment"], inst["tradingsymbol"], inst["underlying"],
             inst["type"], num(inst["strike"]), inst["expiry"], t["product"], q,
             " + ".join(str(x) for x in t["executed_orders"]), t["date"], t["fill_time_ist"],
             "%.2f" % t["fill_price"], "%.2f" % (D(t["fill_price"]) * q), t["exit_date"], t["exit_time_ist"],
             "%.2f" % t["exit_price"], "%.2f" % (D(t["exit_price"]) * q), "%.2f" % t["gross_pnl_inr"]]
            + ["%.2f" % ch[k] for k in CHARGE_PARTS]
            + ["%.2f" % t["charges_inr"], "%.2f" % t["net_pnl_inr"], t["exit_reason"], "%d-%02d" % (fy, (fy + 1) % 100)])


def write_csvs(trader, trades):
    """trader/journal_<year>.csv per year of exit: closed trades only, oldest first."""
    years = {}
    for t in trades:
        if t.get("exit_date"):
            years.setdefault(t["exit_date"][:4], []).append(t)
    out = []
    for year in sorted(years):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(CSV_COLUMNS)
        w.writerows(csv_row(t) for t in years[year])
        path = os.path.join(trader, "journal_%s.csv" % year)
        smart.write_bytes(path, buf.getvalue().encode("utf-8"))
        out.append(path)
    return out


# ---------------------------------------------------------------- the ticket

def flat_by(o, plan, cfg):
    """(HH:MM, where it came from): the order's own, the matching plan.json, else config."""
    if o.get("flat_by"):
        return o["flat_by"], "orders.json"
    inst = o["instrument"]
    if plan and plan.get("date") == o.get("date") and (plan.get("instrument") or {}).get("token") == inst["token"] \
            and plan.get("flat_by"):
        return plan["flat_by"], "plan.json"
    s = cfg.get("session") or {}
    if inst.get("expiry") == o.get("date"):
        return s.get("expiry_day_flat_by", "14:45"), "config session.expiry_day_flat_by"
    return s.get("flat_by", "15:10"), "config session.flat_by"


def not_ready(o, today):
    """[] when the ticket may be typed, else every reason it may not."""
    inst = o.get("instrument") or {}
    rr = o.get("risk_result")
    why = []
    if o.get("date") != today:
        why.append("dated %s, not today: size it and check it again" % o.get("date"))
    if o.get("side") != "BUY" or inst.get("type") not in ("CE", "PE"):
        why.append("version 1 buys a CE or a PE only (this is %s %s)" % (o.get("side"), inst.get("type")))
    qs, lot, freeze = o.get("order_quantities") or [], int(inst.get("lot_size") or 0), inst.get("freeze_qty")
    if not qs or not lot or sum(qs) != o.get("quantity") or any(q % lot for q in qs) or \
            (freeze and max(qs) >= int(freeze)):
        why.append("order_quantities %s do not split quantity %s into whole lots under the freeze quantity %s"
                   % (qs, o.get("quantity"), freeze))
    if not rr:
        why.append("in-risk has not checked it: run in-risk")
    elif rr.get("verdict") != "PASS":
        why.append("in-risk %s at %s" % (rr.get("verdict"), hhmm_of(rr.get("checked_at_ist"))))
        why += list(rr.get("reasons") or [])
    return why


def ticket_lines(o, why, flat):
    inst = o["instrument"]
    u = inst["underlying"]
    contract = "%s %s %s, expiry %s" % (u, num(inst["strike"]), inst["type"], day_text(inst["expiry"]))
    if why:
        out = ["TICKET %s  NOT READY: do not type it" % o["ticket_id"],
               "  %-13s %s (%s)" % ("Contract", inst["tradingsymbol"], contract)]
        return out + ["  %-13s %s" % ("Why" if i == 0 else "", w) for i, w in enumerate(why)]
    rr, qs, lot = o["risk_result"], o["order_quantities"], int(inst["lot_size"])
    up = inst["type"] == "CE"   # a CE gains as the underlying rises, a PE as it falls
    out = ["TICKET %s  READY (in-risk PASS at %s)" % (o["ticket_id"], hhmm_of(rr.get("checked_at_ist"))),
           "  %-13s %s" % ("Segment", inst["segment"]),
           "  %-13s %s" % ("Symbol", inst["tradingsymbol"]),
           "  %-13s %s" % ("Contract", contract),
           "  %-13s %s" % ("Side", o["side"]),
           "  %-13s %s" % ("Product", o["product"]),
           "  %-13s LIMIT %.2f" % ("Price", o["limit_price"]),
           "  %-13s %s (%d lots of %d) in %d order%s%s" % (
               "Quantity", "{:,}".format(o["quantity"]), o["quantity"] // lot, lot, len(qs), "" if len(qs) == 1 else "s",
               ", freeze quantity %s" % inst["freeze_qty"] if len(qs) > 1 else "")]
    out += ["  %-13s order %d of %d: %s (%d lots)" % ("", i + 1, len(qs), "{:,}".format(q), q // lot)
            for i, q in enumerate(qs)]
    out += ["  %-13s premium at or below %.2f; %s at or %s %s" % ("Stop", o["premium_stop"], u, "below" if up else "above",
                                                                  money(o["underlying_stop"])),
            "  %-13s premium at or above %.2f; %s at or %s %s" % ("Target", o["target_premium"], u,
                                                                  "above" if up else "below", money(o["target_underlying"])),
            "  %-13s %s IST (%s)" % ("Flat by", flat[0], flat[1])]
    r = o.get("risk_inr") or {}
    if r.get("total") is not None:
        out.append("  %-13s about %s lost (premium %s + charges %s%s)" % (
            "At the stop", money(r["total"]), money(r.get("premium_at_stop") or 0), money(r.get("charges_round_trip") or 0),
            "" if o.get("charges_verified") else "; charges.json unverified"))
    out.append("  %-13s in-risk PASS" % "Risk")
    out += ["  %-13s %-3s %s: %s" % ("", "ok" if c.get("ok") else "NO", c.get("name"), c.get("detail"))
            for c in rr.get("checks") or []]
    return out


def cmd_show(a, p, cfg, now):
    today = now.date().isoformat()
    doc = load_orders(p)
    orders = [find(doc, a.id)] if a.id else [o for o in doc["orders"] if o.get("date") == today]
    if not orders:
        raise smart.SmartError("no ticket dated %s in %s: run in-budget and in-risk first" % (today, show_path(p["orders"])))
    plan = smart.read_json(p["plan"])
    out = ["Tickets to type by hand, %s IST (source %s, from in-budget and in-risk)" % (smart.stamp(now),
                                                                                      show_path(p["orders"])),
           "  " + smart.NOT_ADVICE,
           "  This tool sends nothing: you type each order into the Angel One app yourself."]
    ready = 0
    for o in orders:
        out.append("")
        if o.get("status") != "DRAFT":
            out.append("TICKET %s  %s: nothing to type" % (o["ticket_id"], o.get("status")))
            continue
        why = not_ready(o, today)
        ready += not why
        out += ticket_lines(o, why, None if why else flat_by(o, plan, cfg))
    out.append("")
    if ready:
        out += NEAR_CLOSE
    out.append("Then record what you got: ticket.py fill ID --price P --time HH:MM, or ticket.py skip ID.")
    print("\n".join(out))
    return 0


# ---------------------------------------------------------------- the journal

def on_tick(price, inst, what):
    """A typo guard: every traded price is a multiple of the instrument's tick (from the master)."""
    tick = inst.get("tick_size")
    if tick and smart.round_tick(price, tick) != price:
        raise smart.SmartError("%s %s is not a multiple of the tick %s: check the price in the app" % (what, price, tick))


def executed_orders(qty, inst):
    """The quantity of each order the exchange executed: the fill split under the freeze quantity."""
    lot = int(inst["lot_size"])
    return [n * lot for n in smart.split_lots(qty // lot, smart.max_lots_per_order(inst.get("freeze_qty"), lot))]


def charges(side, price, quantities, seg, ch):
    """Each part summed over the executed orders: one brokerage per order."""
    each = [smart.option_charges(side, price, q, seg, ch) for q in quantities]
    return {k: sum((c[k] for c in each), Decimal(0)) for k in each[0]}


def cmd_fill(a, p, cfg, now):
    with smart.FileLock(p["orders"] + ".lock"):
        doc = load_orders(p)
        o = find(doc, a.id)
        if o.get("status") != "DRAFT":
            raise smart.SmartError("%s is %s: only a DRAFT ticket can be filled" % (a.id, o.get("status")))
        inst = o["instrument"]
        if inst.get("type") not in ("CE", "PE"):
            raise smart.SmartError("version 1 records bought options only (CE or PE), not %s" % inst.get("type"))
        lot, qty = int(inst["lot_size"]), a.qty or o["quantity"]
        if qty <= 0 or qty % lot or qty > o["quantity"]:
            raise smart.SmartError("--qty wants a multiple of the lot size %d, at most the ticket's %d" % (lot, o["quantity"]))
        on_tick(a.price, inst, "fill price")
        flat = flat_by(o, smart.read_json(p["plan"]), cfg)[0]
        trades = [t for t in (smart.read_json(p["journal"]) or {}).get("trades", []) if t.get("ticket_id") != a.id]
        trades.append({"ticket_id": a.id, "date": o["date"], "symbol": o["symbol"], "instrument": inst,
                       "segment_class": "FO_OPTIONS", "product": o["product"], "quantity": qty,
                       "executed_orders": executed_orders(qty, inst), "fill_price": float(a.price),
                       "fill_time_ist": a.time, "flat_by": flat, "exit_date": None, "exit_price": None,
                       "exit_time_ist": None, "exit_reason": None, "charges_inr": None, "charges_detail": None,
                       "gross_pnl_inr": None, "net_pnl_inr": None})
        save_journal(p, trades, now)   # the journal first: a fill recorded twice replaces itself
        o["status"] = "FILLED"
        o["fill"] = {"price": float(a.price), "time_ist": a.time, "quantity": qty, "recorded_at_ist": now.isoformat()}
        smart.write_json(p["orders"], doc)
    warn = [] if o.get("risk_result") and o["risk_result"].get("verdict") == "PASS" and o["date"] == now.date().isoformat() \
        else ["  Note        in-risk did not pass this ticket today: recorded because you placed it, not approved"]
    n = len(trades[-1]["executed_orders"])
    print("\n".join([
        "Recorded fill %s, %s IST" % (a.id, smart.stamp(now)),
        "  " + smart.NOT_ADVICE,
        "  BUY %s %s at %.2f, %s, in %d order%s (the ticket said LIMIT %.2f)" % (
            "{:,}".format(qty), inst["tradingsymbol"], a.price, a.time, n, "" if n == 1 else "s", o["limit_price"])]
        + warn + [
        "  Status FILLED in %s; open trade in %s" % (show_path(p["orders"]), show_path(p["journal"])),
        "  Watch it: in-alerts check (stop %.2f / %s %s, target %.2f / %s %s, flat by %s)" % (
            o["premium_stop"], o["symbol"], money(o["underlying_stop"]), o["target_premium"], o["symbol"],
            money(o["target_underlying"]), flat),
        "Close it yourself in the Angel One app, then: ticket.py close %s --price P --time HH:MM --reason R" % a.id]))
    return 0


def cmd_close(a, p, cfg, now):
    ch = smart.read_json(p["charges"])
    if ch is None:
        raise smart.SmartError("no %s: run in-setup init, then check it against Angel One's charges page"
                               % show_path(p["charges"]))
    with smart.FileLock(p["orders"] + ".lock"):
        doc = load_orders(p)
        o = find(doc, a.id)
        if o.get("status") != "FILLED":
            raise smart.SmartError("%s is %s: only a FILLED ticket can be closed" % (a.id, o.get("status")))
        trades = (smart.read_json(p["journal"]) or {}).get("trades", [])
        t = next((t for t in trades if t.get("ticket_id") == a.id), None)
        if t is None:
            raise smart.SmartError("%s is FILLED in orders.json but not in %s: record the fill again with ticket.py fill"
                                   % (a.id, show_path(p["journal"])))
        inst, today = t["instrument"], now.date().isoformat()
        on_tick(a.price, inst, "exit price")
        if today == t["date"] and a.time < t["fill_time_ist"]:
            raise smart.SmartError("exit %s is before the fill %s" % (a.time, t["fill_time_ist"]))
        qty, fill, seg = t["quantity"], D(t["fill_price"]), inst["segment"]
        buy = charges("BUY", fill, t["executed_orders"], seg, ch)
        sell = charges("SELL", a.price, t["executed_orders"], seg, ch)
        gross = (a.price - fill) * qty
        total = buy["total"] + sell["total"]
        t.update(exit_date=today, exit_price=float(a.price), exit_time_ist=a.time, exit_reason=a.reason,
                 charges_inr=paisa(total), charges_detail={k: paisa(buy[k] + sell[k]) for k in CHARGE_PARTS},
                 gross_pnl_inr=paisa(gross), net_pnl_inr=paisa(gross - total))
        csvs = save_journal(p, trades, now)
        o["status"] = "CLOSED"
        o["exit"] = {"price": float(a.price), "time_ist": a.time, "reason": a.reason, "recorded_at_ist": now.isoformat()}
        smart.write_json(p["orders"], doc)
    c = t["charges_detail"]
    print("\n".join([
        "Recorded exit %s, %s IST" % (a.id, smart.stamp(now)),
        "  " + smart.NOT_ADVICE,
        "  SELL %s %s at %.2f, %s, reason %s" % ("{:,}".format(qty), inst["tradingsymbol"], a.price, a.time, a.reason),
        "  %-11s %.2f x %s = %s (%s)" % ("Bought", fill, "{:,}".format(qty), money(fill * qty), t["fill_time_ist"]),
        "  %-11s %.2f x %s = %s (%s)" % ("Sold", a.price, "{:,}".format(qty), money(a.price * qty), a.time),
        "  %-11s %s" % ("Gross P&L", money(gross)),
        "  %-11s %s for %d order%s each way (buy %s, sell %s), computed from charges.json%s" % (
            "Charges", money(total), len(t["executed_orders"]), "" if len(t["executed_orders"]) == 1 else "s",
            money(buy["total"]), money(sell["total"]),
            "" if ch.get("verified") else " (unverified)"),
        "  %-11s brokerage %s, exchange %s, SEBI %s, STT %s, stamp %s, GST %s" % (
            "", money(c["brokerage"]), money(c["exchange"]), money(c["sebi"]), money(c["stt"]), money(c["stamp"]),
            money(c["gst"])),
        "  %-11s %s" % ("Net P&L", money(t["net_pnl_inr"])),
        "Wrote %s and %s (for your accountant; no tax advice)" % (show_path(p["journal"]),
                                                                ", ".join(show_path(x) for x in csvs))]))
    return 0


def cmd_skip(a, p, cfg, now):
    with smart.FileLock(p["orders"] + ".lock"):
        doc = load_orders(p)
        o = find(doc, a.id)
        if o.get("status") != "DRAFT":
            raise smart.SmartError("%s is %s: only a DRAFT ticket can be skipped" % (a.id, o.get("status")))
        o["status"] = "SKIPPED"
        o["skipped_at_ist"] = now.isoformat()
        smart.write_json(p["orders"], doc)
    print("Skipped %s (%s): marked SKIPPED in %s, nothing recorded in the journal."
          % (a.id, o["instrument"]["tradingsymbol"], show_path(p["orders"])))
    return 0


def cmd_journal(a, p, cfg, now):
    trades = (smart.read_json(p["journal"]) or {}).get("trades", [])
    if not trades:
        raise smart.SmartError("no trades in %s yet: ticket.py fill records one" % show_path(p["journal"]))
    today, year = now.date().isoformat(), now.strftime("%Y")
    out = ["Journal, %s IST (source %s: what you recorded by hand)" % (smart.stamp(now), show_path(p["journal"])),
           "  " + smart.NOT_ADVICE]
    for t in trades:
        if t["date"] != today and t.get("exit_date") != today and t.get("exit_date"):
            continue
        head = "  %s %s %s, bought %.2f at %s" % (t["ticket_id"], t["instrument"]["tradingsymbol"],
                                                 "{:,}".format(t["quantity"]), t["fill_price"], t["fill_time_ist"])
        if t.get("exit_date"):
            out.append("%s, sold %.2f at %s (%s): net %s" % (head, t["exit_price"], t["exit_time_ist"], t["exit_reason"],
                                                            money(t["net_pnl_inr"])))
        else:
            out.append("%s: OPEN since %s" % (head, t["date"]))
    closed = [t for t in trades if t.get("exit_date") == today]
    out.append("  Today       %d closed, net %s; %d open" % (
        len(closed), money(sum(D(t["net_pnl_inr"]) for t in closed)), sum(1 for t in trades if not t.get("exit_date"))))
    ytd = [t for t in trades if (t.get("exit_date") or "")[:4] == year]
    out.append("  %-11s %d closed, gross %s, charges %s, net %s" % (
        year, len(ytd), money(sum(D(t["gross_pnl_inr"]) for t in ytd)), money(sum(D(t["charges_inr"]) for t in ytd)),
        money(sum(D(t["net_pnl_inr"]) for t in ytd))))
    csvs = write_csvs(p["trader"], trades)
    if csvs:
        out.append("Wrote %s (closed trades, for your accountant; no tax advice)" % ", ".join(show_path(x) for x in csvs))
    print("\n".join(out))
    return 0


# ---------------------------------------------------------------- main

def price_arg(text):
    try:
        v = Decimal(text)
    except InvalidOperation:
        v = None
    if v is None or not v.is_finite() or v <= 0:
        raise argparse.ArgumentTypeError("a price wants a positive number like 112.40, got %r" % text)
    return v


def hhmm_arg(text):
    if not HHMM.match(text):
        raise argparse.ArgumentTypeError("a time wants HH:MM in IST, like 09:41, got %r" % text)
    return text


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    common.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-24 09:42"')
    ap = argparse.ArgumentParser(description="in-ticket: manual order ticket and journal (sends nothing, no orders).")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("show", parents=[common], help="today's tickets to type by hand")
    s.add_argument("id", nargs="?")
    for name, help_ in (("fill", "record what you bought"), ("close", "record how you got out")):
        s = sub.add_parser(name, parents=[common], help=help_)
        s.add_argument("id")
        s.add_argument("--price", type=price_arg, required=True)
        s.add_argument("--time", type=hhmm_arg, required=True, help="HH:MM IST, from the app's trade book")
        if name == "fill":
            s.add_argument("--qty", type=int, help="if less than the ticket was filled")
        else:
            s.add_argument("--reason", choices=REASONS, required=True)
    s = sub.add_parser("skip", parents=[common], help="a ticket you will not place")
    s.add_argument("id")
    sub.add_parser("journal", parents=[common], help="today's trades, the year so far, the CSV")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        now = smart.now_ist(a.now)
        cfg = smart.load_config(a.trader)
        return {"show": cmd_show, "fill": cmd_fill, "close": cmd_close, "skip": cmd_skip,
                "journal": cmd_journal}[a.cmd](a, smart.trader_paths(a.trader), cfg, now)
    except smart.SmartError as e:
        print("in-ticket: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
