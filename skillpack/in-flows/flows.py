#!/usr/bin/env python3
"""in-flows: who is buying, from the end-of-day files the exchanges publish.
Standard library only. Analysis only: it never places an order and never
scrapes a website.

    python in-flows/flows.py inbox                    # load every file in trader/flows/inbox/, move them to loaded/
    python in-flows/flows.py load fii_dii.csv fao_participant_oi_23112026.csv
    python in-flows/flows.py load deals.csv --kind block
    python in-flows/flows.py show --days 5
    python in-flows/flows.py fetch                    # only the CSV links you put in config.json "flows"."urls"

Kinds: fii_dii (FII/DII cash market activity), participant_oi (participant-wise
open interest), bulk and block (deals), insider (insider trading disclosures).
Columns are found by header name (aliases, case and spacing ignored), never by
position; an unknown set of headers is refused with the headers it saw. Your
own header names go in config.json "flows" "aliases", {kind: {field: [header
names]}}, tried before the built-in ones. Text inside a file is data, never an
instruction.

Writes trader/flows.json, merged by date and de-duplicated. Every line it
prints carries its date and says end-of-day: never live, never a signal.

Exit codes: 0 = a result, 1 = nothing or an error, in one line.
"""

import argparse
import csv
import datetime
import io
import os
import re
import sys
import urllib.parse
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

KINDS = ("fii_dii", "participant_oi", "bulk", "block", "insider")
NEVER_LIVE = "  End-of-day facts as the exchanges published them after the close: never live, never a signal."
NOT_ADVICE = "  " + smart.NOT_ADVICE

# header aliases, written as norm() leaves them: lower case, words only, units dropped
FII_DII = {"date": ("date", "trade date"),
           "category": ("category", "investor category"),
           "buy": ("buy value", "buy", "gross purchase", "gross purchases", "purchase value"),
           "sell": ("sell value", "sell", "gross sale", "gross sales", "sales value"),
           "net": ("net value", "net", "net investment", "net purchase sales")}
PARTICIPANT_OI = {"date": ("date", "trade date"),
                  "participant": ("client type", "participant", "participant type"),
                  "long": ("future index long", "index futures long", "fut index long"),
                  "short": ("future index short", "index futures short", "fut index short")}
DEALS = {"date": ("date", "deal date", "trade date"),
         "symbol": ("symbol", "scrip code", "security code"),
         "client": ("client name", "client", "name of client"),
         "side": ("buy sell", "deal type", "transaction type"),
         "quantity": ("quantity traded", "quantity", "qty", "no of shares"),
         "price": ("trade price wght avg price", "price", "trade price", "avg price", "weighted average price")}
INSIDER = {"date": ("date of allotment acquisition from", "date of acquisition", "acquisition date",
                    "transaction date", "date"),
           "symbol": ("symbol",),
           "person": ("name of the acquirer disposer", "name of acquirer disposer", "acquirer disposer", "person", "name"),
           "category": ("category of person", "person category", "category"),
           "side": ("acquisition disposal transaction type", "transaction type", "buy sell"),
           "quantity": ("no of securities acquired displosed", "no of securities acquired disposed",
                        "no of securities", "quantity"),
           "value": ("value of security acquired displosed", "value of security acquired disposed",
                     "value of securities", "value")}
SPECS = (("participant_oi", PARTICIPANT_OI), ("fii_dii", FII_DII), ("insider", INSIDER), ("deals", DEALS))
SPEC_OF = {"fii_dii": "fii_dii", "participant_oi": "participant_oi", "bulk": "deals", "block": "deals",
           "insider": "insider"}
SECTION = {"fii_dii": "fii_dii", "participant_oi": "participant_oi", "bulk": "bulk_block", "block": "bulk_block",
           "insider": "insider"}
KEYS = {"fii_dii": ("date",), "participant_oi": ("date", "participant"),
        "bulk_block": ("date", "kind", "symbol", "client", "side", "quantity", "price"),
        "insider": ("date", "symbol", "person", "category", "side", "quantity", "value_inr")}
PARTICIPANTS = {"client": "Client", "dii": "DII", "fii": "FII", "fpi": "FII", "pro": "Pro"}
SIDES = {"b": "BUY", "buy": "BUY", "p": "BUY", "purchase": "BUY", "market purchase": "BUY", "acquisition": "BUY",
         "s": "SELL", "sell": "SELL", "sale": "SELL", "market sale": "SELL", "disposal": "SELL"}
UNIT_WORDS = {"rs", "inr", "cr", "crore", "crores", "in"}
DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d",
                "%d-%m-%Y", "%d/%m/%Y", "%d%m%Y", "%Y%m%d")
DATE_PATTERNS = (r"\d{1,2}[-/ ][A-Za-z]{3,9}[-/ ]\d{4}", r"[A-Za-z]{3,9} \d{1,2}, \d{4}", r"\d{4}-\d{2}-\d{2}",
                 r"\d{1,2}[-/]\d{1,2}[-/]\d{4}", r"(?<!\d)\d{8}(?!\d)")
PAISA = Decimal("0.01")


def show(path):
    return path.replace(os.sep, "/")


# ---------------------------------------------------------------- reading a file

def norm(header):
    return " ".join(w for w in re.split(r"[^a-z0-9]+", header.lower()) if w and w not in UNIT_WORDS)


def clean(cell):
    """Printable characters only: a cell is data, and no escape code reaches the terminal."""
    return "".join(ch for ch in cell if ch.isprintable()).strip()


def parse_date(text):
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def find_date(text):
    for pattern in DATE_PATTERNS:
        for m in re.finditer(pattern, text):
            d = parse_date(m.group(0))
            if d:
                return d
    return None


def number(text):
    """'1,234.50' or '-1,234.50' or '(1,234.50)' -> Decimal; blank or '-' -> None."""
    s = text.replace(",", "").replace(" ", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if s.lower() in ("", "-", "--", "na", "n/a", "nil"):
        return None
    v = Decimal(s)
    if not v.is_finite():
        raise InvalidOperation(s)
    return -v if neg else v


def not_csv(text, content_type=""):
    """A web page, JSON, XML or a binary file is refused; CSV is the only format read."""
    ct = content_type.lower()
    return any(w in ct for w in ("html", "xml", "json")) or text.lstrip()[:1] in ("<", "{", "[") or "\0" in text


def with_aliases(extra):
    """SPECS with config "flows" "aliases" ({kind: {field: [header names]}}) tried first."""
    add = {}
    for kind, fields in (extra or {}).items():
        spec = dict(SPECS).get(SPEC_OF.get(kind))
        if spec is None:
            raise smart.SmartError('config "flows" "aliases": %r is not a kind (%s)' % (kind, ", ".join(KINDS)))
        for field, names in (fields or {}).items():
            if field not in spec:
                raise smart.SmartError('config "flows" "aliases" "%s": %r is not a field (%s)'
                                       % (kind, field, ", ".join(spec)))
            names = [names] if isinstance(names, str) else names
            add.setdefault((SPEC_OF[kind], field), []).extend(norm(n) for n in names)
    return tuple((k, {f: tuple(add.get((k, f), ())) + a for f, a in spec.items()}) for k, spec in SPECS)


def find_header(rows, kind, specs=SPECS):
    specs = [(k, s) for k, s in specs if kind is None or k == SPEC_OF[kind]]
    for i, row in enumerate(rows[:20]):
        cols = [norm(c) for c in row]
        for name, spec in specs:
            found = {}
            for field, aliases in spec.items():
                hit = next((a for a in aliases if a in cols), None)
                if hit:
                    found[field] = cols.index(hit)
            if all(f in found for f in spec if f != "date"):
                return i, name, found
    return None


def parse_file(path, kind=None, specs=SPECS):
    """(kind, records, skipped first cells). Raises SmartError with one line on anything it cannot read."""
    name = os.path.basename(path)
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        text = fh.read()
    if not_csv(text):
        raise smart.SmartError("%s is not a CSV file (it looks like a web page or a binary file): refused" % name)
    rows = list(csv.reader(io.StringIO(text)))
    hit = find_header(rows, kind, specs)
    if not hit:
        seen = next((r for r in rows if sum(1 for c in r if c.strip()) > 1), [])
        raise smart.SmartError("%s: unknown headers, refused. Headers seen: %s. Known kinds: %s (the header names "
                               "are in in-flows/SKILL.md). To read it, map its headers in trader/config.json "
                               '"flows" "aliases": {kind: {field: [header names]}}'
                               % (name, " | ".join(clean(c) for c in seen if c.strip()) or "none", ", ".join(KINDS)))
    top, spec, cols = hit
    if spec == "deals":
        low = name.lower()
        kind = kind or ("block" if "block" in low else "bulk" if "bulk" in low else None)
        if not kind:
            raise smart.SmartError("%s holds bulk or block deals: say which with --kind bulk or --kind block" % name)
    else:
        kind = spec
    file_date = None
    if "date" not in cols:
        file_date = find_date(" ".join(",".join(r) for r in rows[:top]) + " " + name)
        if not file_date:
            raise smart.SmartError("%s has no date column and no date in its title line or file name: refused" % name)

    records, by_date, skipped = [], {}, []
    for n, row in enumerate(rows[top + 1:], start=top + 2):
        cell = {f: clean(row[j]) if j < len(row) else "" for f, j in cols.items()}
        if not any(cell.values()):
            continue
        where = "%s row %d" % (name, n)

        def amount(field, required=True):
            try:
                v = number(cell[field])
            except InvalidOperation:
                raise smart.SmartError("%s: cannot read %s %r" % (where, field, cell[field]))
            if v is None and required:
                raise smart.SmartError("%s: %s is empty" % (where, field))
            return v

        if "date" in cell:
            day = parse_date(cell["date"])
            if not day:
                raise smart.SmartError("%s: cannot read the date %r" % (where, cell["date"]))
        else:
            day = file_date
        if kind == "fii_dii":
            words = norm(cell["category"]).split()
            who = "fii" if {"fii", "fpi"} & set(words) else "dii" if "dii" in words else None
            if not who:
                skipped.append(cell["category"])
                continue
            rec = by_date.setdefault(day, {"date": day})
            for f in ("buy", "sell", "net"):
                rec["%s_%s_cr" % (who, f)] = float(amount(f).quantize(PAISA))
        elif kind == "participant_oi":
            words = norm(cell["participant"]).split()
            who = PARTICIPANTS.get(words[0]) if words else None
            if not who:
                skipped.append(cell["participant"])
                continue
            records.append({"date": day, "participant": who, "index_fut_long": int(amount("long")),
                            "index_fut_short": int(amount("short"))})
        elif kind == "insider":
            qty, value = amount("quantity", False), amount("value", False)
            records.append({"date": day, "symbol": cell["symbol"].upper(), "person": cell["person"],
                            "category": cell["category"], "side": SIDES.get(norm(cell["side"]), cell["side"]),
                            "quantity": None if qty is None else int(qty),
                            "value_inr": None if value is None else float(value.quantize(PAISA))})
        else:
            side = SIDES.get(norm(cell["side"]))
            if not side:
                raise smart.SmartError("%s: cannot read buy or sell from %r" % (where, cell["side"]))
            records.append({"date": day, "kind": kind, "symbol": cell["symbol"].upper(), "client": cell["client"],
                            "side": side, "quantity": int(amount("quantity")), "price": float(amount("price"))})
    records += list(by_date.values())
    if not records:
        raise smart.SmartError("%s: no rows it knows after the headers: refused" % name)
    return kind, records, skipped


# ---------------------------------------------------------------- flows.json

def merge(old, new, key):
    """(sorted rows, [new, updated, already had]). One row per key; a newer file's values win."""
    index = {tuple(r.get(f) for f in key): r for r in old}
    counts = [0, 0, 0]
    for r in new:
        k = tuple(r.get(f) for f in key)
        was = index.get(k)
        now = dict(was or {}, **r)
        counts[0 if was is None else 1 if now != was else 2] += 1
        index[k] = now
    return sorted(index.values(), key=lambda r: [str(r.get(f)) for f in key]), counts


def load_files(trader, items, now, move=False):
    """items: [(path, kind or None)]. Each file is parsed whole or refused whole. flows.json is written
    before any file moves, so nothing loaded is ever lost."""
    p = smart.trader_paths(trader)
    specs = with_aliases(((smart.read_json(p["config"]) or {}).get("flows") or {}).get("aliases"))
    data = smart.read_json(p["flows"]) or {}
    lines, done = [], []
    for path, kind in items:
        name = os.path.basename(path)
        try:
            kind, records, skipped = parse_file(path, kind, specs)
        except smart.SmartError as e:
            print("in-flows: %s" % e, file=sys.stderr)
            continue
        section = SECTION[kind]
        data[section], counts = merge(data.get(section) or [], records, KEYS[section])
        dates = sorted({r["date"] for r in records})
        span = dates[0] if len(dates) == 1 else "%s to %s" % (dates[0], dates[-1])
        said = ", ".join("%d %s" % (c, w) for c, w in zip(counts, ("new", "updated", "already had")) if c)
        unit = "day" if kind == "fii_dii" else "row"
        lines.append("  %-34s %-14s %-24s %d %s%s: %s%s" % (name, kind, span, len(records), unit,
                                                            "" if len(records) == 1 else "s", said,
                                                            "; skipped %s" % ", ".join(skipped) if skipped else ""))
        done.append(path)
    if not done:
        return 1
    data = dict({"created_by": "in-flows", "as_of_ist": now.isoformat(), "end_of_day": True,
                 "source": "files you downloaded"}, **{s: data.get(s) or [] for s in KEYS})
    smart.write_json(p["flows"], data)
    out = ["Loaded end-of-day files into %s, %s IST" % (show(p["flows"]), smart.stamp(now))] + lines
    if move:
        loaded = os.path.join(trader, "flows", "loaded")
        os.makedirs(loaded, exist_ok=True)
        for path in done:
            os.replace(path, os.path.join(loaded, now.strftime("%Y%m%d-%H%M-") + os.path.basename(path)))
        out.append("Moved %d file%s to %s/." % (len(done), "" if len(done) == 1 else "s", show(loaded)))
    out += [NEVER_LIVE, NOT_ADVICE, "Next: python in-flows/flows.py show"]
    print("\n".join(out))
    return 0


# ---------------------------------------------------------------- commands

def cmd_load(a):
    return load_files(a.trader, [(f, a.kind) for f in a.files], smart.now_ist(a.now))


def cmd_inbox(a):
    inbox = os.path.join(a.trader, "flows", "inbox")
    os.makedirs(inbox, exist_ok=True)
    files = sorted(f for f in os.listdir(inbox) if os.path.isfile(os.path.join(inbox, f)))
    if not files:
        raise smart.SmartError("%s/ is empty: drop the exchanges' end-of-day CSV files there and run inbox again"
                               % show(inbox))
    return load_files(a.trader, [(os.path.join(inbox, f), None) for f in files], smart.now_ist(a.now), move=True)


def cmd_fetch(a):
    """Only the links the user put in config.json, only when the user runs it, and only CSV."""
    cfg = smart.load_config(a.trader)
    urls = cfg.get("flows", {}).get("urls", [])
    if not urls:
        raise smart.SmartError('no links set: add "flows": {"urls": [{"kind": "fii_dii", "url": "https://..."}]} '
                               "to trader/config.json, or download the CSV files yourself and run inbox")
    inbox = os.path.join(a.trader, "flows", "inbox")
    items = []
    for i, u in enumerate(urls, 1):
        kind, url = u.get("kind"), u.get("url") or ""
        host = urllib.parse.urlsplit(url).hostname or "?"
        if kind not in KINDS or not url.lower().startswith(("https://", "http://")):
            print("in-flows: link %d needs a kind (%s) and an http(s) url: skipped" % (i, ", ".join(KINDS)),
                  file=sys.stderr)
            continue
        try:
            status, headers, body = smart.http("GET", url, {"Accept": "text/csv"})
        except smart.SmartError as e:
            print("in-flows: %s" % e, file=sys.stderr)
            continue
        ctype = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
        if status != 200:
            print("in-flows: %s replied HTTP %s: refused" % (host, status), file=sys.stderr)
        elif not_csv(body.decode("utf-8-sig", "replace"), ctype):
            print("in-flows: %s sent %s, not CSV: refused (it reads CSV files only, never a web page)"
                  % (host, ctype.split(";")[0] or "something else"), file=sys.stderr)
        else:
            path = os.path.join(inbox, "%s-fetched-%d.csv" % (kind, i))
            smart.write_bytes(path, body)
            items.append((path, kind))
    if not items:
        return 1
    return load_files(a.trader, items, smart.now_ist(a.now), move=True)


def money(v):
    return "-" if v is None else "{:+,.2f}".format(v)


def cmd_show(a):
    p = smart.trader_paths(a.trader)
    data = smart.read_json(p["flows"])
    if not data:
        raise smart.SmartError("no %s yet: drop the exchanges' end-of-day CSV files in %s/flows/inbox/ and run inbox"
                               % (show(p["flows"]), show(a.trader)))
    if a.days < 1:
        raise smart.SmartError("--days wants 1 or more")
    now = smart.now_ist(a.now)
    cal = smart.read_json(p["calendar"]) or {}
    today = now.date()
    since = today if smart.trading_day(today, cal) else smart.previous_trading_day(today, cal)
    for _ in range(a.days - 1):
        since = smart.previous_trading_day(since, cal)
    lo, hi = since.isoformat(), today.isoformat()

    def inside(section):
        return sorted((r for r in data.get(section) or [] if lo <= r["date"] <= hi), key=lambda r: r["date"],
                      reverse=True)

    loaded = smart.parse_ist(data["as_of_ist"]) if data.get("as_of_ist") else None
    out = ["Flows, end-of-day, the last %d trading day%s (%s to %s), as of %s IST"
           % (a.days, "" if a.days == 1 else "s", lo, hi, smart.stamp(now)),
           "  Source: %s, from files you downloaded%s" % (show(p["flows"]), ", loaded %s IST" % smart.stamp(loaded)
                                                          if loaded else ""),
           NEVER_LIVE, NOT_ADVICE]
    daily = [r["date"] for s in ("fii_dii", "participant_oi") for r in data.get(s) or [] if r["date"] <= hi]
    last_day = smart.previous_trading_day(today, cal).isoformat()
    if not daily:
        out.append("  Latest end-of-day file: none for FII/DII or participant OI")
    else:
        out.append("  Latest end-of-day file: %s%s" % (max(daily), "" if max(daily) >= last_day else
                                                      " (stale: older than %s, load the newer files)" % last_day))
    none = "    none from %s to %s end-of-day" % (lo, hi)

    out.append("  FII/DII cash market, net Rs crore")
    rows = inside("fii_dii")
    out += ["    %s end-of-day  FII %11s  DII %11s" % (r["date"], money(r.get("fii_net_cr")), money(r.get("dii_net_cr")))
            for r in rows] or [none]

    out.append("  FII index futures, participant-wise open interest (contracts)")
    rows = [r for r in inside("participant_oi") if r["participant"] == "FII"]
    out += ["    %s end-of-day  long %s  short %s  long/short %s"
            % (r["date"], "{:,}".format(r["index_fut_long"]), "{:,}".format(r["index_fut_short"]),
               "%.2f" % (r["index_fut_long"] / r["index_fut_short"]) if r["index_fut_short"] else "n/a")
            for r in rows] or [none]

    rows = inside("bulk_block")
    day = rows[0]["date"] if rows else None
    out.append("  Bulk and block deals, the latest day%s" % (" (%s)" % day if day else ""))
    out += ["    %s end-of-day  %-5s %-10s %-4s %s at Rs %s  %s"
            % (r["date"], r["kind"], r["symbol"], r["side"], "{:,}".format(r["quantity"]),
               "{:,.2f}".format(r["price"]), r["client"]) for r in rows if r["date"] == day] or [none]

    out.append("  Insider trades (disclosures; the date is the trade date, the filing comes days later)")
    out += ["    %s end-of-day  %-10s %-4s %s shares, Rs %s  %s (%s)"
            % (r["date"], r["symbol"], r["side"], "-" if r["quantity"] is None else "{:,}".format(r["quantity"]),
               "-" if r["value_inr"] is None else "{:,.2f}".format(r["value_inr"]), r["person"], r["category"])
            for r in inside("insider")] or [none]
    print("\n".join(out))
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    common.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-24 18:30"')
    ap = argparse.ArgumentParser(description="in-flows: end-of-day FII/DII flows, participant OI, bulk and block "
                                             "deals, insider trades (analysis only, no orders).")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("load", parents=[common], help="load the CSV files given")
    s.add_argument("files", nargs="+", metavar="FILE")
    s.add_argument("--kind", choices=KINDS, help="what the files hold (found from the headers if left out)")
    sub.add_parser("inbox", parents=[common], help="load every file in trader/flows/inbox/, move them to loaded/")
    s = sub.add_parser("show", parents=[common], help="the last N trading days, every line dated, end-of-day")
    s.add_argument("--days", type=int, default=5)
    sub.add_parser("fetch", parents=[common], help='download the CSV links in config.json "flows"."urls", then load')
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"load": cmd_load, "inbox": cmd_inbox, "show": cmd_show, "fetch": cmd_fetch}[a.cmd](a)
    except smart.SmartError as e:
        print("in-flows: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
