#!/usr/bin/env python3
"""in-budget: size an option buy in lots, inside your max loss per trade.
Standard library only, no network. It writes a DRAFT ticket, never an order.

    python in-budget/size.py                         # plan.json, config.json, charges.json: a preview
    python in-budget/size.py --write                 # append the DRAFT ticket to trader/orders.json
    python in-budget/size.py --cash 20000 --write    # also keep the capital used within Rs 20,000
    python in-budget/size.py --now "2026-11-24 09:41"

Lots = floor(max loss / ((entry premium - premium stop) x lot size)), then one
lot fewer at a time until the premium risk at the stop plus the round-trip
charges (BUY at the entry, SELL at the stop, one brokerage per executed order)
fits the max loss. The capital used (premium x quantity) stays within
account.max_capital_per_trade_pct of capital and within --cash. The quantity is
split by the freeze quantity into one entry per order you would type.

Exit codes: 0 = sized (written with --write), 1 = zero lots or an error.
"""

import argparse
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

SOURCE = "plan.json + config.json + charges.json"


def D(v):
    return Decimal(str(v))


def rs(x):
    return "Rs {:,.2f}".format(x)


def inr(x):
    return float(D(x).quantize(smart.PAISA))


def setting(cfg, section, key):
    v = (cfg.get(section) or {}).get(key)
    if v is None:
        raise smart.SmartError("set %s.%s in trader/config.json" % (section, key))
    return D(v)


def round_trip(qtys, entry, stop, seg, ch):
    """BUY at the entry and SELL at the stop, every order paying its own brokerage."""
    return sum((smart.option_charges("BUY", entry, q, seg, ch)["total"] +
                smart.option_charges("SELL", stop, q, seg, ch)["total"] for q in qtys), Decimal(0))


def size(plan, cfg, ch, cash=None):
    capital = smart.require_account(cfg)
    inst = plan["instrument"]
    lot, seg = int(inst["lot_size"]), smart.segment(inst["segment"])
    entry, stop = D(plan["entry_premium"]), D(plan["premium_stop"])
    if not 0 < stop < entry:
        raise smart.SmartError("plan.json's premium stop %s is not below the entry %s: run in-exit again" % (stop, entry))
    if seg not in (ch.get("exchange_txn_pct") or {}):
        raise smart.SmartError("charges.json has no exchange_txn_pct for %s" % seg)
    risk_pct = setting(cfg, "account", "risk_per_trade_pct")
    cap_pct = setting(cfg, "account", "max_capital_per_trade_pct")
    max_loss = (capital * risk_pct / 100).quantize(smart.PAISA)
    cap = (capital * cap_pct / 100).quantize(smart.PAISA)
    lot_cost = entry * lot
    limits = [("max loss", int(max_loss // ((entry - stop) * lot))), ("capital cap", int(cap // lot_cost))]
    if cash is not None:
        limits.append(("cash", int(max(D(cash), 0) // lot_cost)))
    decided, lots = min(limits, key=lambda kv: kv[1])
    per_order = smart.max_lots_per_order(inst.get("freeze_qty"), lot)

    def at(n):
        qtys = [k * lot for k in smart.split_lots(n, per_order)]
        return qtys, (entry - stop) * n * lot, round_trip(qtys, entry, stop, seg, ch)

    qtys, premium_risk, charges = at(lots)
    while lots and premium_risk + charges > max_loss:
        lots, decided = lots - 1, "max loss with charges"
        qtys, premium_risk, charges = at(lots)
    _, one_premium, one_charges = at(1)
    return {"capital": capital, "risk_pct": risk_pct, "cap_pct": cap_pct, "max_loss": max_loss, "cap": cap,
            "cash": D(cash) if cash is not None else None, "lot": lot, "seg": seg, "entry": entry, "stop": stop,
            "lot_cost": lot_cost, "one_premium": one_premium, "one_charges": one_charges, "limits": limits,
            "decided": decided, "lots": lots, "per_order": per_order, "qtys": qtys, "premium_risk": premium_risk,
            "charges": charges, "verified": bool(ch.get("verified"))}


def ticket_id(doc, today):
    prefix = "T%s-" % today.strftime("%Y%m%d")
    taken = [int(o["ticket_id"][len(prefix):]) for o in doc.get("orders", [])
             if str(o.get("ticket_id", "")).startswith(prefix) and o["ticket_id"][len(prefix):].isdigit()]
    return "%s%02d" % (prefix, max(taken, default=0) + 1)


def draft(plan, s, tid, now):
    """The DRAFT ticket, shaped as CONTRACTS.md's orders.json entry."""
    return {"ticket_id": tid, "created_by": "in-budget", "date": now.date().isoformat(),
            "as_of_ist": now.isoformat(), "source": SOURCE,
            "symbol": plan["symbol"], "direction": plan["direction"], "instrument": plan["instrument"],
            "side": "BUY", "product": "INTRADAY", "limit_price": inr(s["entry"]), "lots": s["lots"],
            "quantity": s["lots"] * s["lot"], "order_quantities": s["qtys"], "premium_stop": inr(s["stop"]),
            "target_premium": plan.get("target_premium"), "underlying_stop": plan.get("underlying_stop"),
            "target_underlying": plan.get("target_underlying"), "entry_underlying": plan.get("entry_underlying"),
            "no_progress_bars": plan.get("no_progress_bars"),
            "risk_inr": {"premium_at_stop": inr(s["premium_risk"]), "charges_round_trip": inr(s["charges"]),
                         "total": inr(s["premium_risk"] + s["charges"])},
            "capital_used_inr": inr(s["entry"] * s["lots"] * s["lot"]), "charges_verified": s["verified"],
            "risk_result": None, "status": "DRAFT", "fill": None, "exit": None}


def report(plan, s, now):
    inst = plan["instrument"]
    made = smart.parse_ist(plan["as_of_ist"])
    out = ["Sizing %s (%s), %s IST, computed from plan.json (in-exit, as of %s IST, %d min old)"
           % (inst["tradingsymbol"], s["seg"], smart.stamp(now), made.strftime("%H:%M"),
              (now - made).total_seconds() // 60),
           "  " + smart.NOT_ADVICE,
           "  %-14s %s; max loss %s a trade (%s%%); capital cap %s a trade (%s%%)"
           % ("Capital", rs(s["capital"]), rs(s["max_loss"]), "%g" % s["risk_pct"], rs(s["cap"]),
              "%g" % s["cap_pct"]),
           "  %-14s %d units: costs %s at %.2f, risks %s at the stop %.2f (%s premium + %s charges)"
           % ("One lot", s["lot"], rs(s["lot_cost"]), s["entry"], rs(s["one_premium"] + s["one_charges"]),
              s["stop"], rs(s["one_premium"]), rs(s["one_charges"])),
           "  %-14s %s: %d lot%s, decided by %s"
           % ("Lots", ", ".join("%s %d" % kv for kv in s["limits"]), s["lots"], "" if s["lots"] == 1 else "s",
              s["decided"])]
    if not s["lots"]:
        over = {"capital cap": "costs %s, over your %s capital cap a trade" % (rs(s["lot_cost"]), rs(s["cap"])),
                "cash": "costs %s, over the %s cash you gave" % (rs(s["lot_cost"]), rs(s["cash"] or 0))}
        out.append("No ticket: one lot %s." % over.get(s["decided"], "risks %s, over your %s max loss a trade" % (
            rs(s["one_premium"] + s["one_charges"]), rs(s["max_loss"]))))
        return out
    n = len(s["qtys"])
    freeze = "freeze %s allows %d lots an order" % (inst.get("freeze_qty"), s["per_order"]) \
        if s["per_order"] else "no freeze quantity on the instrument: one order"
    out += ["  %-14s %d units in %d order%s %s; %s" % ("Orders", s["lots"] * s["lot"], n, "" if n == 1 else "s",
                                                    s["qtys"], freeze),
            "  %-14s %s at the stop + %s charges = %s of %s"
            % ("Risk", rs(s["premium_risk"]), rs(s["charges"]), rs(s["premium_risk"] + s["charges"]),
               rs(s["max_loss"])),
            "  %-14s %s" % ("Capital used", rs(s["entry"] * s["lots"] * s["lot"])),
            "  %-14s %s" % ("Charges", "verified (charges.json)" if s["verified"] else
                            "UNVERIFIED: charges.json is not yet checked against the broker's calculator "
                            "(in-setup probe); the real charges can differ")]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="in-budget: size an option buy in lots (writes a DRAFT ticket, "
                                             "never an order).")
    ap.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    ap.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-24 09:41"')
    ap.add_argument("--cash", type=float, metavar="RUPEES", help="free cash: the capital used stays within it")
    ap.add_argument("--write", action="store_true", help="append the DRAFT ticket to trader/orders.json")
    a = ap.parse_args(argv)
    try:
        now = smart.now_ist(a.now)
        p = smart.trader_paths(a.trader)
        cfg = smart.load_config(a.trader)
        plan = smart.read_json(p["plan"])
        if plan is None:
            raise smart.SmartError("no trader/plan.json: run in-exit first")
        if plan.get("date") != now.date().isoformat():
            raise smart.SmartError("plan.json is for %s, not today (%s): run in-exit again"
                                   % (plan.get("date"), now.date()))
        ch = smart.read_json(p["charges"])
        if ch is None:
            raise smart.SmartError("no trader/charges.json: run in-setup init first")
        s = size(plan, cfg, ch, a.cash)
        for line in report(plan, s, now):
            print(line)
        if not s["lots"]:
            return 1
        if not a.write:
            print("Preview only: run with --write to save it as DRAFT ticket %s."
                  % ticket_id(smart.read_json(p["orders"]) or {}, now.date()))
            return 0
        with smart.FileLock(p["orders"] + ".lock"):
            doc = smart.read_json(p["orders"]) or {}
            doc.update(created_by="in-budget", as_of_ist=now.isoformat(), source=SOURCE)
            order = draft(plan, s, ticket_id(doc, now.date()), now)
            doc.setdefault("orders", []).append(order)
            smart.write_json(p["orders"], doc)
        print("Wrote DRAFT ticket %s to %s/orders.json. in-risk checks it next."
              % (order["ticket_id"], a.trader.replace(os.sep, "/")))
        return 0
    except smart.SmartError as e:
        print("in-budget: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
