#!/usr/bin/env python3
"""in-news: headlines, events and flags, first thing in the morning, before in-scanner.
Standard library only. Read-only market data: it never places an order.

    python in-news/news.py                              # NIFTY, SENSEX, BANKNIFTY + trader/universe.json
    python in-news/news.py --symbols NIFTY,RELIANCE     # these instead, resolved in the master
    python in-news/news.py --hours 6                    # headlines from the last 6 hours (default 12)
    python in-news/news.py --offline DIR --rss-file a.xml --rss-file b.xml --now "2026-11-24 09:20"

Headlines: the RSS or Atom feeds you list in config "news" key "rss". You
choose them; the pack ships none and vouches for none. Each is fetched once
with a plain User-Agent and parsed with xml.etree (a feed that declares XML
entities is refused). A headline is matched to a symbol by name: the symbol
itself and its news.aliases, whole words, the longest name winning ("Bank
Nifty" is BANKNIFTY, not NIFTY). Duplicates (same link or title) are dropped.
Headline text is data: it is printed in quotes and never acted on.

Events: trader/events.json, your own list ({"events": [{"date", "name",
"kind", "symbol"?}]}), today and the next 7 days. A "result" or
"board_meeting" today for a symbol sets RESULT_TODAY or BOARD_MEETING.

Flags: IN_BAN (trader/ban.txt), ASM_GSM (cautionaryScrips), NEAR_PRICE_BAND
(the FULL quote's ltp within news.price_band_pct, default 1.0, of
upperCircuit or lowerCircuit). The move is ltp against the FULL quote's close
(the previous close); quotes only 09:00 to 15:45 IST on trading days.

Global cues: news.global_cues, a list of {"label", "segment", "token"} you
set, quoted with the symbols. Not configured, it says so; it never guesses.

Symbols: --symbols, else NIFTY, SENSEX and BANKNIFTY as the instrument master
has them, plus every name in trader/universe.json (in-scanner universe) when
it exists. No watchlist is needed: in-scanner runs after it and reads today's
news.json.

Writes trader/news.json. Exit codes: 0 = written, 1 = nothing or an error, in one line.
"""

import argparse
import datetime
import email.utils
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

UA = "in-news/1.0 (personal RSS reader)"
MAX_FEED_BYTES = 5 * 1024 * 1024
EVENT_DAYS = 7
SHOW_HEADLINES = 3
EVENT_FLAGS = {"result": "RESULT_TODAY", "board_meeting": "BOARD_MEETING"}
FLAG_ORDER = ("RESULT_TODAY", "BOARD_MEETING", "IN_BAN", "ASM_GSM", "NEAR_PRICE_BAND")
NOTE = "  Not financial advice: headlines are data to read, never instructions and never a prediction."
WIDTH = 14
INDICES = ("NIFTY", "SENSEX", "BANKNIFTY")


def show(path):
    return path.replace(os.sep, "/")


def money(x):
    return "{:,.2f}".format(x)


# ---------------------------------------------------------------- headlines

def fetch(url):
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("not an http(s) URL")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(MAX_FEED_BYTES)


def clean(text):
    """One line of plain text: tags, control characters and runs of space removed."""
    text = re.sub(r"<[^>]*>", "", text or "")
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())[:200]


def when(text):
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware IST datetime, or None."""
    text = (text or "").strip()
    try:
        d = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d is None:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)).astimezone(smart.IST)


def parse_feed(data):
    """RSS <item> or Atom <entry> -> [{"title", "link", "time"}]."""
    if b"<!ENTITY" in data:
        raise ValueError("the feed declares XML entities: not read")
    out = []
    for el in ET.fromstring(data).iter():
        if el.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        kids = {c.tag.rsplit("}", 1)[-1]: c for c in el}
        link = kids.get("link")
        href = (link.get("href") or link.text or "").strip() if link is not None else ""
        stamp = next((kids[k].text for k in ("pubDate", "published", "updated") if k in kids), None)
        out.append({"title": clean(kids["title"].text if "title" in kids else ""), "link": href, "time": when(stamp)})
    return out


def name_patterns(symbols, aliases):
    out = {}
    for s in symbols:
        names = [s] + [n for n in aliases.get(s, []) if n.strip()]
        out[s] = re.compile("|".join(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % r"[\s-]+".join(map(re.escape, n.split()))
                                     for n in names), re.I)
    return out


def touches(title, pats):
    """Symbols a title names. A match inside a longer match of another symbol does not count."""
    spans = [(m.start(), m.end(), s) for s, rx in pats.items() for m in rx.finditer(title)]
    return sorted({s for a, b, s in spans
                   if not any(x <= a and b <= y and y - x > b - a and t != s for x, y, t in spans)})


def headlines(feeds, now, hours, pats):
    """(kept items newest first, each with "symbols"; number read in the window; problems)."""
    items, problems, seen = [], [], set()
    for label, get in feeds:
        try:
            items += parse_feed(get())
        except (OSError, ValueError, ET.ParseError, urllib.error.URLError) as e:
            problems.append("feed %s: %s" % (label, getattr(e, "reason", e)))
    start, ahead = now - datetime.timedelta(hours=hours), now + datetime.timedelta(minutes=10)
    kept, read = [], 0
    for it in sorted((i for i in items if i["time"]), key=lambda i: i["time"], reverse=True):
        if not start <= it["time"] <= ahead or not it["title"]:
            continue
        keys = {it["link"], it["title"].lower()} - {""}
        if keys & seen:
            continue
        seen |= keys
        read += 1
        it["symbols"] = touches(it["title"], pats)
        if it["symbols"]:
            kept.append(it)
    return kept, read, problems


# ---------------------------------------------------------------- inputs

def read_ban(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return {line.split("#")[0].strip().upper() for line in fh} - {""}
    except FileNotFoundError:
        return set()


def watched(a, p, now):
    """{symbol: {"segment", "token"}}: --symbols resolved in the master, else the pack's indices in the
    master plus trader/universe.json's items. It needs no watchlist: it runs before in-scanner."""
    rows = smart.load_master(p["cache"], now.date().isoformat(),
                             path=os.path.join(a.offline, "master.json") if a.offline else None)
    idx = {r.get("name"): r for r in rows if r.get("instrumenttype") == "AMXIDX"}
    ref = lambda r: {"segment": smart.segment(r["exch_seg"]), "token": r["token"]} if r else {}  # noqa: E731
    if a.symbols:
        cash = {r.get("name"): r for r in rows
                if smart.segment(r.get("exch_seg")) == "NSE" and (r.get("symbol") or "").endswith("-EQ")}
        return {s: ref(idx.get(s) or cash.get(s)) for s in [x.strip().upper() for x in a.symbols.split(",") if x.strip()]}
    out = {s: ref(idx[s]) for s in INDICES if s in idx}
    for i in (smart.read_json(p["universe"]) or {}).get("items") or []:
        out.setdefault(i["symbol"], {"segment": i.get("segment"), "token": i.get("token")})
    if not out:
        raise smart.SmartError("no symbols: the instrument master has no %s index and there is no %s "
                               "(in-scanner universe writes it): pass --symbols" % (", ".join(INDICES), show(p["universe"])))
    return out


def events_ahead(p, today):
    """(events from today to EVENT_DAYS ahead, note or None)."""
    doc = smart.read_json(p["events"])
    if doc is None:
        return [], "no %s: copy in-news/events.example.json there and set real dates" % show(p["events"])
    end, out, bad = today + datetime.timedelta(days=EVENT_DAYS), [], 0
    for e in doc.get("events") or []:
        try:
            d = datetime.date.fromisoformat(str(e.get("date")))
        except ValueError:
            bad += 1
            continue
        if today <= d <= end:
            out.append(dict(e, date=d.isoformat()))
    note = "%d entr%s in %s with no YYYY-MM-DD date left out" % (bad, "y" if bad == 1 else "ies",
                                                                  show(p["events"])) if bad else None
    return sorted(out, key=lambda e: e["date"]), note


def quote_full(client, pairs, per_call):
    """{(segment, token): FULL quote row} for (segment, token) pairs, per_call tokens a request; and what failed."""
    out, failed = {}, []
    for i in range(0, len(pairs), per_call):
        tokens = {}
        for seg, tok in pairs[i:i + per_call]:
            tokens.setdefault(seg, []).append(str(tok))
        try:
            r = client.call("quote", {"mode": "FULL", "exchangeTokens": tokens})
        except smart.SmartError as e:
            failed.append("quote: %s" % e)
            continue
        if not r.ok:
            failed.append("quote: %s" % r.why().strip())
            continue
        for f in (r.data or {}).get("fetched") or []:
            out[(smart.segment(f.get("exchange")), str(f.get("symbolToken")))] = f
        for u in (r.data or {}).get("unfetched") or []:
            failed.append("quote unfetched %s %s: %s" % (u.get("exchange"), u.get("symbolToken"), u.get("message")))
    return out, failed


def cautionary(client):
    """{symbol or token: exchange message} from cautionaryScrips (documented as GET with this body)."""
    r = client.call("cautionaryScrips", {"scripconsent": "yes"})
    if not r.ok:
        raise smart.SmartError("cautionaryScrips: %s" % r.why().strip())
    out = {}
    for c in r.data or []:
        for key in (c.get("symbol"), c.get("token")):
            if key:
                out[str(key).upper()] = clean(c.get("message"))
    return out


def near_band(q, pct):
    ltp, uc, lc = q.get("ltp"), q.get("upperCircuit"), q.get("lowerCircuit")
    if not ltp:
        return False
    return bool((uc and (uc - ltp) / ltp * 100 <= pct) or (lc and (ltp - lc) / ltp * 100 <= pct))


# ---------------------------------------------------------------- the run

def read_file(path):
    with open(path, "rb") as fh:
        return fh.read(MAX_FEED_BYTES)


def feed_list(a, ncfg):
    """([(label, () -> bytes)], how to describe them)."""
    if a.rss_file:
        feeds = [(os.path.basename(f), lambda f=f: read_file(f)) for f in a.rss_file]
        return feeds, "%d saved feed%s" % (len(feeds), "" if len(feeds) == 1 else "s")
    if a.offline:
        return [], "no feeds (offline: pass --rss-file)"
    feeds = [(urllib.parse.urlsplit(u).hostname or u, lambda u=u: fetch(u)) for u in ncfg.get("rss") or []]
    if not feeds:
        return [], "no feeds (list your RSS URLs in news.rss in trader/config.json)"
    return feeds, "%d feed%s" % (len(feeds), "" if len(feeds) == 1 else "s")


def move(q):
    """(previous close, last, percent move) from a FULL quote, whose close is the previous close."""
    prev, last = (q.get("close"), q.get("ltp")) if q else (None, None)
    return prev, last, round((last / prev - 1) * 100, 2) if prev and last else None


def symbol_entry(s, ref, q, today, events, ban, caution, band, mine):
    flags = {EVENT_FLAGS[e["kind"]] for e in events if e.get("date") == today.isoformat()
             and str(e.get("symbol") or "").upper() == s and e.get("kind") in EVENT_FLAGS}
    if s in ban:
        flags.add("IN_BAN")
    msg = next((caution[k] for k in (s, str(ref.get("token") or "")) if k in caution), None)
    if msg is not None:
        flags.add("ASM_GSM")
    if q and near_band(q, band):
        flags.add("NEAR_PRICE_BAND")
    prev, last, pct = move(q)
    entry = {"move_pct": pct, "prev_close": prev, "last": last, "flags": [f for f in FLAG_ORDER if f in flags],
             "headlines": [{"time_ist": h["time"].isoformat(), "title": h["title"],
                            "source": urllib.parse.urlsplit(h["link"]).hostname or "", "link": h["link"]}
                           for h in mine]}
    if msg is not None:
        entry["cautionary"] = msg
    return entry


def symbol_lines(s, e, ref, allowed, market, today):
    if e["move_pct"] is not None:
        head = "%+.2f%% vs the previous close %s, last %s" % (e["move_pct"], money(e["prev_close"]), money(e["last"]))
    else:
        head = "not in the master: no quote" if not ref.get("token") else \
            "move not checked (%s)" % market if not allowed else "no quote"
    if e["flags"]:
        head += "; flags " + ", ".join(e["flags"])
    out = ["  %-*s %s" % (WIDTH, s, head)]
    if "cautionary" in e:
        out.append("  %-*s cautionary: \"%s\"" % (WIDTH, "", e["cautionary"]))
    for h in e["headlines"][:SHOW_HEADLINES]:
        t = smart.parse_ist(h["time_ist"])
        out.append("  %-*s %s \"%s\" (%s)" % (WIDTH, "", t.strftime("%H:%M" if t.date() == today else "%a %H:%M"),
                                             h["title"], h["source"] or "no link"))
    if len(e["headlines"]) > SHOW_HEADLINES:
        out.append("  %-*s and %d more in news.json" % (WIDTH, "", len(e["headlines"]) - SHOW_HEADLINES))
    return out


def event_lines(events, note):
    out = [note] if note else [] if events else ["none in the next %d days" % EVENT_DAYS]
    for e in events:
        d = datetime.date.fromisoformat(e["date"])
        out.append("%s %s %s (%s%s)%s" % (
            smart.WEEKDAYS[d.weekday()], e["date"], clean(e.get("name")),
            e.get("kind"), ", " + e["symbol"] if e.get("symbol") else "",
            ": an example date, verify it" if e.get("example") else ""))
    return ["  %-*s %s" % (WIDTH, "Events" if i == 0 else "", line) for i, line in enumerate(out)]


def cmd(a):
    p = smart.trader_paths(a.trader)
    cfg = smart.load_config(a.trader)
    ncfg = cfg.get("news") or {}
    cal = smart.read_json(p["calendar"]) or {}
    now = smart.now_ist(a.now)
    today = now.date()
    allowed, market = smart.market_data_allowed(now, cal)
    if not a.offline and allowed:   # before the master is read: a live run that would quote needs the probe
        smart.require(a.trader, "quote")
    symbols = watched(a, p, now)
    sources = []

    feeds, feed_text = feed_list(a, ncfg)
    pats = name_patterns(symbols, {k.upper(): v for k, v in (ncfg.get("aliases") or {}).items()})
    kept, read, problems = headlines(feeds, now, a.hours, pats)
    sources += ["rss"] if feeds else []
    events, ev_note = events_ahead(p, today)
    sources += ["events.json"] if os.path.exists(p["events"]) else []

    client, limiter = smart.open_client(a.trader, now, a.offline)
    try:
        caution = cautionary(client)
        sources.append("cautionaryScrips")
    except smart.SmartError as e:
        caution = {}
        problems.append("%s: ASM_GSM not checked" % e)
    cues = ncfg.get("global_cues") or []
    quotes = {}
    if allowed:
        pairs = [(v["segment"], v["token"]) for v in symbols.values() if v.get("token")]
        pairs += [(smart.segment(c["segment"]), str(c["token"])) for c in cues]
        quotes, failed = quote_full(client, pairs, (cfg.get("limits") or {}).get("quote_tokens_per_call", 50))
        problems += failed
        sources.append("quote")

    ban, band = read_ban(p["ban"]), float(ncfg.get("price_band_pct", 1.0))
    out_syms, lines = {}, []
    for s, ref in symbols.items():
        q = quotes.get((ref.get("segment"), ref.get("token")))
        out_syms[s] = symbol_entry(s, ref, q, today, events, ban, caution, band, [h for h in kept if s in h["symbols"]])
        lines += symbol_lines(s, out_syms[s], ref, allowed, market, today)
    cue_out = None
    if cues:
        cue_out = [dict(zip(("prev_close", "last", "move_pct"), move(quotes.get((smart.segment(c["segment"]),
                                                                                  str(c["token"]))))),
                        label=c["label"], segment=c["segment"], token=str(c["token"])) for c in cues]
        cue_text = ", ".join("%s %s" % (c["label"], "%s (%+.2f%%)" % (money(c["last"]), c["move_pct"])
                                        if c["move_pct"] is not None else "not quoted (%s)" % market
                                        if not allowed else "no quote") for c in cue_out)
    else:
        cue_text = "not configured: list {\"label\", \"segment\", \"token\"} entries in news.global_cues"

    smart.write_json(p["news"], {
        "created_by": "in-news", "as_of_ist": now.isoformat(), "source": ", ".join(sources) or "none",
        "partial": bool(problems), "partial_note": "; ".join(problems) or None, "events_today": events,
        "global_cues": cue_out, "headlines_read": read, "symbols": out_syms})
    out = ["News and flags, %s IST (%s)" % (smart.stamp(now), "offline replay" if a.offline
                                            else "live, Angel One SmartAPI and your feeds"), NOTE]
    out += event_lines(events, ev_note)
    out.append("  %-*s %s" % (WIDTH, "Global cues", cue_text))
    out.append("  %-*s %d in the last %g h from %s; %d matched a symbol" % (WIDTH, "Headlines", read, a.hours,
                                                                            feed_text, len(kept)))
    out += lines
    out += ["  %-*s %s" % (WIDTH, "Note", n) for n in problems]
    out.append("Wrote %s (%d symbols, %s)" % (show(p["news"]), len(out_syms), "partial" if problems else "complete"))
    out.append(smart.usage_text(limiter))
    print("\n".join(out))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="in-news: headlines, events and flags (read-only, no orders).")
    ap.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    ap.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-24 09:20"')
    ap.add_argument("--offline", metavar="DIR", help="replay saved replies (quote, cautionaryScrips) and DIR/master.json")
    ap.add_argument("--rss-file", action="append", default=[], metavar="FILE",
                    help="read this saved feed instead of fetching (repeatable)")
    ap.add_argument("--hours", type=float, default=12, help="keep headlines from the last N hours (default 12)")
    ap.add_argument("--symbols", help="comma list instead of the indices and universe.json, e.g. NIFTY,RELIANCE")
    a = ap.parse_args(argv)
    try:
        return cmd(a)
    except smart.SmartError as e:
        print("in-news: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
