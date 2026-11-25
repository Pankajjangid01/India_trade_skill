#!/usr/bin/env python3
"""in-analyst: quarterly results you copy from the exchange filing. Standard library only.
Analysis only: it never places an order.

Version 1 has no automated fetch: no free structured results feed was
confirmed (BRD section 5), so you read the numbers in the results filing on
the NSE or BSE site and add them here with where you read them. It stores
them in trader/analyst.json, computes QoQ and YoY growth and the EBITDA and
net margins, says which quarter is missing for a comparison, and prints the
source next to every number.

    python in-analyst/facts.py add TCS --quarter Q2FY27 --sales 65799 --ebitda 17432 --profit 12075 --eps 33.37 --source "NSE filing, consolidated"
    python in-analyst/facts.py show TCS
    python in-analyst/facts.py checklist TCS

Sales, EBITDA and profit are in Rs crore, EPS in rupees. Quarters follow the
Indian financial year: Q1 is April to June, so Q2FY27 is July to September 2026.

Exit codes: 0 = a result, 1 = nothing or an error, in one line.
"""

import argparse
import os
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

NOT_ADVICE = "  " + smart.NOT_ADVICE
NO_FETCH = ("  No automated fetch: version 1 reads no results feed. You copy the numbers from the exchange filing; "
            "check each one against it.")
FIELDS = (("sales_cr", "sales"), ("ebitda_cr", "EBITDA"), ("profit_cr", "profit"), ("eps", "EPS"))
CHECKLIST = (
    "Standalone or consolidated: the filing has both. Note which one you copied and copy the same one every "
    "quarter; the group's numbers are the consolidated ones.",
    "Exceptional items: one-off gains or losses (a sale, an impairment, a settlement). Read profit with and "
    "without them before calling it growth.",
    "Segment results: revenue and profit by segment. Which segment moved the total?",
    "Auditor's remarks: the limited review or audit report. Any qualified or modified opinion, emphasis of "
    "matter or material uncertainty is a finding in itself.",
    "Related-party notes: transactions with promoters and group companies, and any change from last quarter.",
    "Other income, tax rate and share count: a jump in any of them moves profit and EPS without the "
    "business changing.")


def analyst_path(trader):
    return os.path.join(trader, "analyst.json")


def quarter(text):
    """'Q2FY27' (or 'q2 fy2027') -> (2, 27)."""
    m = re.match(r"^\s*Q([1-4])\s*FY\s*(\d{2}|\d{4})\s*$", text or "", re.I)
    if not m:
        raise smart.SmartError("a quarter looks like Q2FY27 (Indian financial year, Q1 is April to June), got %r"
                               % text)
    return int(m.group(1)), int(m.group(2)) % 100


def label(q):
    return "Q%dFY%02d" % q


def previous(q):
    return (q[0] - 1, q[1]) if q[0] > 1 else (4, (q[1] - 1) % 100)


def year_ago(q):
    return q[0], (q[1] - 1) % 100


def amount(text):
    try:
        v = Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise argparse.ArgumentTypeError("not a number: %r" % text)
    if not v.is_finite():
        raise argparse.ArgumentTypeError("not a number: %r" % text)
    return v


def dec(v):
    return None if v is None else Decimal(str(v))


def pct(part, whole):
    """part / whole in percent, one decimal, half up; None without a positive whole."""
    if part is None or whole is None or whole <= 0:
        return None
    return (part / whole * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)


def growth(now, then):
    """Percent change against a positive base; None when the base is missing, zero or a loss."""
    if now is None or then is None or then <= 0:
        return None
    return ((now - then) / then * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)


def quarter_line(name, r):
    sales, ebitda, profit = dec(r["sales_cr"]), dec(r["ebitda_cr"]), dec(r["profit_cr"])
    eps = "" if r.get("eps") is None else "  EPS %.2f" % r["eps"]
    return ("  %s  sales %s  EBITDA %s (margin %s%%)  profit %s (net margin %s%%)%s  [source: %s]"
            % (name, "{:,.2f}".format(sales), "{:,.2f}".format(ebitda), pct(ebitda, sales),
               "{:,.2f}".format(profit), pct(profit, sales), eps, r["source"]))


def compare_line(sym, q, other, what, quarters):
    head = "  %s vs %s (%s)" % (label(q), label(other), what)
    then = quarters.get(label(other))
    if not then:
        return "%s  missing %s: add it to compare (python in-analyst/facts.py add %s --quarter %s ...)" % (
            head, label(other), sym, label(other))
    now = quarters[label(q)]
    parts = []
    for key, name in FIELDS:
        if key == "eps" and (now.get("eps") is None or then.get("eps") is None):
            continue
        g = growth(dec(now[key]), dec(then[key]))
        parts.append("%s %s" % (name, "n/a (base not above zero)" if g is None else "%+.1f%%" % g))
    return "%s  %s" % (head, "  ".join(parts))


def report(sym, quarters, now):
    order = sorted(quarters, key=lambda k: quarter(k)[::-1], reverse=True)
    latest = quarter(order[0])
    out = ["%s quarterly results, %s IST: numbers you copied from the exchange filings (Rs crore, EPS in Rs)"
           % (sym, smart.stamp(now)), NO_FETCH, NOT_ADVICE]
    out += [quarter_line(k, quarters[k]) for k in order]
    out.append("  Growth for %s, computed from the lines above:" % label(latest))
    out.append(compare_line(sym, latest, previous(latest), "QoQ", quarters))
    out.append(compare_line(sym, latest, year_ago(latest), "YoY", quarters))
    return out


def cmd_add(a):
    q = quarter(a.quarter)
    if a.sales <= 0:
        raise smart.SmartError("--sales must be above zero: the margins divide by it")
    now = smart.now_ist(a.now)
    path = analyst_path(a.trader)
    data = smart.read_json(path) or {}
    symbols = data.get("symbols") or {}
    sym = a.symbol.upper()
    quarters = symbols.setdefault(sym, {})
    replaced = label(q) in quarters
    paisa = Decimal("0.01")
    quarters[label(q)] = {"sales_cr": float(a.sales.quantize(paisa)), "ebitda_cr": float(a.ebitda.quantize(paisa)),
                          "profit_cr": float(a.profit.quantize(paisa)),
                          "eps": None if a.eps is None else float(a.eps.quantize(paisa)),
                          "source": a.source or "not given", "added_at_ist": now.isoformat()}
    smart.write_json(path, {"created_by": "in-analyst", "as_of_ist": now.isoformat(),
                            "source": "numbers the user copied from exchange filings", "symbols": symbols})
    out = ["%s %s %s in %s" % ("Replaced" if replaced else "Saved", sym, label(q), path.replace(os.sep, "/"))]
    if not a.source:
        out.append('  Source not given: add --source "where you read it" so every number keeps its source')
    print("\n".join(out + report(sym, quarters, now)))
    return 0


def cmd_show(a):
    sym = a.symbol.upper()
    quarters = ((smart.read_json(analyst_path(a.trader)) or {}).get("symbols") or {}).get(sym)
    if not quarters:
        raise smart.SmartError("no quarters for %s in %s: add them from the results filing with "
                               "python in-analyst/facts.py add %s --quarter Q2FY27 --sales ... --ebitda ... "
                               "--profit ... --source \"...\"" % (sym, analyst_path(a.trader).replace(os.sep, "/"),
                                                                   sym))
    print("\n".join(report(sym, quarters, smart.now_ist(a.now))))
    return 0


def results_days(sym, events_path, today):
    data = smart.read_json(events_path)
    hint = '{"date": "YYYY-MM-DD", "name": "%s results", "kind": "results"} from the board meeting notice' % sym
    if data is None:
        return ["  Results day     no trader/events.json (your own list of dates): add %s" % hint]
    word = re.compile(r"\b%s\b" % re.escape(sym), re.I)
    hits = sorted((e for e in data.get("events") or []
                   if word.search(e.get("name") or "") and "result" in ((e.get("kind") or "") + " "
                                                                        + (e.get("name") or "")).lower()),
                  key=lambda e: e.get("date") or "")
    if not hits:
        return ["  Results day     none for %s in trader/events.json: add %s" % (sym, hint)]
    out = []
    for e in hits:
        try:
            days = (smart.parse_ist(e["date"]).date() - today).days
        except (KeyError, smart.SmartError):
            continue
        when = "today" if days == 0 else "in %d day%s" % (days, "" if days == 1 else "s") if days > 0 else \
            "%d day%s ago" % (-days, "" if days == -1 else "s")
        out.append("  Results day     %s, %s: %s (trader/events.json)" % (e["date"], when, e.get("name")))
    return out


def cmd_checklist(a):
    sym = a.symbol.upper()
    now = smart.now_ist(a.now)
    out = ["%s results checklist, %s IST" % (sym, smart.stamp(now)), NO_FETCH, NOT_ADVICE]
    out += results_days(sym, smart.trader_paths(a.trader)["events"], now.date())
    out.append("  Read in the filing (NSE or BSE corporate announcements, Financial Results):")
    out += ["    %d. %s" % (i, t) for i, t in enumerate(CHECKLIST, 1)]
    out.append("  Then: python in-analyst/facts.py add %s --quarter QnFYyy --sales ... --ebitda ... --profit ... "
               "--eps ... --source \"which filing, standalone or consolidated\"" % sym)
    out.append("  A results day is a gap risk: an option held through it can open far past its stop.")
    print("\n".join(out))
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    common.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-24 10:20"')
    ap = argparse.ArgumentParser(description="in-analyst: quarterly results you copy from the exchange filing "
                                             "(analysis only, no orders, no automated fetch).")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("add", parents=[common], help="store one quarter's numbers (Rs crore)")
    s.add_argument("symbol")
    s.add_argument("--quarter", required=True, help="e.g. Q2FY27 (Q1 is April to June)")
    s.add_argument("--sales", type=amount, required=True, help="revenue from operations, Rs crore")
    s.add_argument("--ebitda", type=amount, required=True, help="Rs crore")
    s.add_argument("--profit", type=amount, required=True, help="net profit, Rs crore")
    s.add_argument("--eps", type=amount, help="EPS in rupees")
    s.add_argument("--source", help="where you read it, e.g. \"NSE filing 09-Oct-2026, consolidated\"")
    s = sub.add_parser("show", parents=[common], help="the stored quarters, margins, QoQ and YoY growth")
    s.add_argument("symbol")
    s = sub.add_parser("checklist", parents=[common], help="what to read in a results filing, and the results day")
    s.add_argument("symbol")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"add": cmd_add, "show": cmd_show, "checklist": cmd_checklist}[a.cmd](a)
    except smart.SmartError as e:
        print("in-analyst: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
