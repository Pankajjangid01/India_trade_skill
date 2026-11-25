#!/usr/bin/env python3
"""in-scanner: the universe and the morning numbers for the Indian pack.
Standard library only. Read-only market data: it never places an order.

    python in-scanner/scan.py universe            # indices + F&O stocks minus trader/ban.txt -> trader/universe.json
    python in-scanner/scan.py orb                 # opening range 09:15 to 09:30 of NIFTY, SENSEX, BANKNIFTY
    python in-scanner/scan.py orb --stocks        # and the F&O stocks, most liquid first
    python in-scanner/scan.py daily [--stocks]    # gap, volume vs the 20-day average, ATR(14), 52-week distance
    python in-scanner/scan.py orb --offline DIR --now "2026-11-24 09:31"

universe  From the instrument master: NIFTY, SENSEX and BANKNIFTY (index token
          and segment, F&O segment, lot size of the nearest expiry, nearest
          FUTIDX for volume) and every name with NSE stock options (OPTSTK on
          NFO), minus the symbols in trader/ban.txt. A name with no NSE cash
          row is reported, never guessed. orb and daily rebuild it the same way.
orb       The opening range from 5-minute bars: the live feed first
          (smart.day_bars), candles when the feed has no complete range (a
          late start). Before 09:30 the range is forming and nothing is
          written as complete. Index candles carry no volume, so an index's
          volume ratio uses its nearest FUTIDX: today's range volume against
          the average of the same window over the last 5 sessions. orb also
          subscribes what it scans (owner in-scanner), so the feed builds the
          next ranges live from 09:15.
daily     The last completed session from ONE_DAY candles: volume against the
          20-day average, ATR(14) (Wilder), distance from the 52-week high and
          low; today's gap from the FULL quote's open once the market is open.

Stocks (--stocks) pass the config "scanner" filters: min_price, min_avg_volume,
max_spread_pct (best bid and ask of the FULL quote's depth), no RESULT_TODAY,
BOARD_MEETING or ASM_GSM flag in today's trader/news.json, not on the
cautionaryScrips list. liquidity_grade A, B or C comes from spread and average
volume (GRADES below).

A symbol whose data failed makes the file partial ("Fetched 2 of 3").
Exit codes: 0 = a result written, 1 = nothing or an error, in one line.
"""

import argparse
import collections
import datetime
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

INDICES = ("NIFTY", "SENSEX", "BANKNIFTY")
FILTERS = {"min_price": 50.0, "min_avg_volume": 100000, "max_spread_pct": 0.5}
# (grade, spread % at most, average daily volume at least); anything else is C
# ponytail: fixed thresholds; move them into config "scanner" if users ask to tune them
GRADES = (("A", 0.05, 1000000), ("B", 0.15, 200000))
SKIP_FLAGS = ("RESULT_TODAY", "BOARD_MEETING", "ASM_GSM")
HISTORY_DAYS = 30     # calendar days of 5-minute candles behind the orb volume numbers
BASELINE = 5          # sessions in the opening-range volume average
DAILY_DAYS = 400      # calendar days of ONE_DAY candles: 52 weeks plus ATR warm-up
START = "09:15"
WIDTH = 14


def show(path):
    return path.replace(os.sep, "/")


def money(x):
    return "{:,.2f}".format(x)


def r2(x):
    return None if x is None else round(x, 2)


# ---------------------------------------------------------------- universe

def read_ban(path, today):
    """(set of symbols, warning or None). A missing file is an empty list, said out loud."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            names = {line.split("#")[0].strip().upper() for line in fh} - {""}
    except FileNotFoundError:
        return set(), ("no %s: nothing removed. Keep the day's F&O ban list there, one symbol a line"
                       % show(path))
    changed = datetime.datetime.fromtimestamp(os.path.getmtime(path), smart.IST).date()
    if changed != today:
        return names, "%s last changed %s: is it today's list?" % (show(path), changed)
    return names, None


def build_universe(rows, today, ban):
    """{"items", "fo_names": names with NSE stock options, "removed_ban", "left_out": why, "ban_not_fo"}."""
    scale = smart.strike_scale(rows)[0]
    futs = {}
    for r in rows:
        d = smart.expiry_date(r.get("expiry"))
        if r.get("instrumenttype") == "FUTIDX" and r.get("name") in INDICES and d and d >= today:
            if r["name"] not in futs or d < futs[r["name"]][0]:
                futs[r["name"]] = (d, r)
    items, left_out = [], []
    for name in INDICES:
        f = smart.underlying_facts(rows, name, today, scale)
        if not f.get("index_token") or not f.get("expiries"):
            left_out.append("%s: no index row or no live options in the master" % name)
            continue
        fut = futs.get(name, (None, None))[1]
        items.append({"symbol": name, "kind": "index", "segment": f["index_segment"], "token": f["index_token"],
                      "fo_segment": f["segment"], "lot_size": f["lot_size"], "expiry": f["expiries"][0][0],
                      "volume_symbol": fut["symbol"] if fut else None,
                      "volume_segment": smart.segment(fut["exch_seg"]) if fut else None,
                      "volume_token": fut["token"] if fut else None})
    opts = collections.defaultdict(list)
    for r in rows:
        d = smart.expiry_date(r.get("expiry"))
        if r.get("instrumenttype") == "OPTSTK" and smart.segment(r.get("exch_seg")) == "NFO" and d and d >= today:
            opts[r["name"]].append((d, r))
    cash = {r.get("name"): r for r in rows
            if smart.segment(r.get("exch_seg")) == "NSE" and (r.get("symbol") or "").endswith("-EQ")}
    for name in sorted(opts):
        if name in ban:
            continue
        near = min(d for d, _ in opts[name])
        lots = collections.Counter(int(float(r.get("lotsize") or 0)) for d, r in opts[name] if d == near)
        c = cash.get(name)
        if not c:
            left_out.append("%s: no NSE cash row in the master" % name)
            continue
        items.append({"symbol": name, "kind": "stock", "segment": "NSE", "token": c["token"], "fo_segment": "NFO",
                      "lot_size": lots.most_common(1)[0][0], "expiry": near.isoformat(),
                      "volume_symbol": c["symbol"], "volume_segment": "NSE", "volume_token": c["token"]})
    return {"items": items, "fo_names": len(opts), "removed_ban": sorted(n for n in opts if n in ban),
            "left_out": left_out, "ban_not_fo": sorted(ban - set(opts))}


def load_universe(a, p, now):
    rows = smart.load_master(p["cache"], now.date().isoformat(),
                             path=os.path.join(a.offline, "master.json") if a.offline else None)
    ban, note = read_ban(p["ban"], now.date())
    return dict(build_universe(rows, now.date(), ban), ban_note=note)


# ---------------------------------------------------------------- data helpers

def quote_full(client, pairs, per_call):
    """{(segment, token): FULL quote row} for (segment, token) pairs, per_call tokens a request; and what failed."""
    out, failed = {}, []
    for i in range(0, len(pairs), per_call):
        tokens = collections.OrderedDict()
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
                out[str(key).upper()] = c.get("message") or ""
    return out


def caution_for(it, caution):
    for key in (it["symbol"], it["token"], it.get("volume_symbol") or ""):
        if key and str(key).upper() in caution:
            return caution[str(key).upper()]
    return None


def spread_pct(q):
    """Best ask minus best bid over the mid, in percent, from the FULL quote's depth."""
    try:
        bid, ask = q["depth"]["buy"][0]["price"], q["depth"]["sell"][0]["price"]
    except (KeyError, IndexError, TypeError):
        return None
    if not bid or not ask or ask < bid:
        return None
    return round((ask - bid) / ((ask + bid) / 2) * 100, 3)


def grade(spread, avg_volume):
    if spread is None or avg_volume is None:
        return None
    for g, most, least in GRADES:
        if spread <= most and avg_volume >= least:
            return g
    return "C"


def news_flags(p, today):
    """{symbol: flags} from trader/news.json, only when it is today's."""
    doc = smart.read_json(p["news"]) or {}
    if (doc.get("as_of_ist") or "")[:10] != today.isoformat():
        return {}
    return {s: list(v.get("flags") or []) for s, v in (doc.get("symbols") or {}).items()}


def as_bars(rows):
    return [{"start_ist": str(b[0])[11:16], "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]}
            for b in rows]


def n_bars(end):
    """5-minute bars in the range from 09:15 to `end`."""
    h, m = end.split(":")
    return (int(h) * 60 + int(m) - 9 * 60 - 15) // 5


def range_volume(bars, end):
    v = [b["volume"] for b in bars if START <= b["start_ist"] < end]
    return sum(v) if v and None not in v else None


# ---------------------------------------------------------------- orb

def range_bars(a, p, seg, token, now, client, end, done):
    """Today's 5-minute bars: the live feed, or candles when the range is due and the feed lacks it."""
    got = smart.day_bars(a.trader, seg, token, 5, now)   # the feed only
    if done and smart.opening_range(got["bars"], START, end) is None:
        days, partial = smart.candles(client, p["cache"], seg, token, "FIVE_MINUTE", now.date(), now.date(), now)
        got = {"bars": as_bars(days.get(now.date().isoformat(), [])), "source": "candles", "partial": partial}
    return got


def volume_history(client, p, seg, token, now, cal, end):
    """(average range volume of the last BASELINE sessions, average daily volume of the last 20, problem)."""
    last = smart.previous_trading_day(now.date(), cal)
    days, partial = smart.candles(client, p["cache"], seg, token, "FIVE_MINUTE",
                                  now.date() - datetime.timedelta(days=HISTORY_DAYS), last, now)
    sessions = [rows for _, rows in sorted(days.items()) if rows]
    ranges = [v for v in (range_volume(as_bars(rows), end) for rows in sessions[-BASELINE:]) if v]
    totals = [sum(b[5] or 0 for b in rows) for rows in sessions[-20:]]
    return (statistics.mean(ranges) if ranges else None, statistics.mean(totals) if totals else None,
            partial or (None if ranges else "no volume in the last %d sessions" % BASELINE))


def orb_item(a, p, it, now, cal, client, end, done, live):
    """(watchlist item, problem or None). The range is complete, forming, or a problem."""
    got = range_bars(a, p, it["segment"], it["token"], now, client, end, done)
    rng = smart.opening_range(got["bars"], START, end)
    tick = live.get(it["token"])
    item = {k: it[k] for k in ("symbol", "kind", "segment", "token", "fo_segment", "lot_size")}
    item.update(range_high=rng["high"] if rng and done else None, range_low=rng["low"] if rng and done else None,
                last=tick["ltp"] if tick else None, volume_ratio=None, liquidity_grade=None, flags=[],
                feed_lag_ms=tick["lag_ms"] if tick else None, stale=bool(not tick or tick["stale"]),
                source=got["source"], volume_symbol=it.get("volume_symbol"))
    inside = [b for b in got["bars"] if START <= b["start_ist"] < end]
    item["forming"] = None if done else {"high": max((b["high"] for b in inside), default=None),
                                         "low": min((b["low"] for b in inside), default=None), "bars": len(inside),
                                         "need": n_bars(end)}
    if done and not rng:
        return item, got["partial"] or "%d of %d range bars" % (len(inside), n_bars(end))
    if not done or not it.get("volume_token"):
        return item, None if not done else "volume ratio: no FUTIDX in the master"
    try:
        vbars = got if it["volume_token"] == it["token"] else range_bars(
            a, p, it["volume_segment"], it["volume_token"], now, client, end, done)
        today = range_volume(vbars["bars"], end)
        base, avg, why = volume_history(client, p, it["volume_segment"], it["volume_token"], now, cal, end)
    except smart.SmartError as e:
        return item, "volume ratio: %s" % e
    item["volume_ratio"] = round(today / base, 2) if today is not None and base else None
    item["avg_volume"] = int(avg) if avg is not None and it["kind"] == "stock" else None
    if item["volume_ratio"] is None:
        return item, "volume ratio: %s" % (vbars["partial"] or why or "no volume today")
    return item, None


def orb_line(it):
    name = "  %-*s " % (WIDTH, it["symbol"])
    f = it.get("forming")
    if f is not None:
        if not f["bars"]:
            return name + ("forming: no 5-minute bar closed yet" if it["source"] else
                           "forming: not in the live feed; candles back-fill it after the range ends")
        return name + "forming: %s to %s so far (%d of %d bars)" % (money(f["low"]), money(f["high"]), f["bars"],
                                                                    f["need"])
    bits = ["%s to %s (%s)" % (money(it["range_low"]), money(it["range_high"]), it["source"])]
    if it["last"] is not None:
        bits.append("last %s, lag %d ms%s" % (money(it["last"]), it["feed_lag_ms"], ", STALE" if it["stale"] else ""))
    else:
        bits.append("no live tick, STALE")
    if it["volume_ratio"] is not None:
        bits.append("volume ratio %.2f%s" % (it["volume_ratio"], " (%s)" % it["volume_symbol"]
                                             if it["kind"] == "index" else ""))
    if it["kind"] == "stock":
        bits.append(grade_text(it))
    if it["flags"]:
        bits.append("flags " + ", ".join(it["flags"]))
    return name + "; ".join(bits)


def grade_text(it):
    sp = "spread %.2f%%" % it["spread_pct"] if it.get("spread_pct") is not None else "spread not checked"
    vol = ", average volume {:,}".format(it["avg_volume"]) if it.get("avg_volume") is not None else ""
    return "grade %s (%s%s)" % (it["liquidity_grade"] or "none", sp, vol)


# ---------------------------------------------------------------- stocks

def stock_screen(stocks, client, cfg, flags, allowed, market):
    """Flags, the cautionary list and one FULL quote per 50 stocks, before any candle is spent.
    Returns (stocks to fetch, most liquid first; {symbol: why skipped}; filters; problems; quotes)."""
    f = dict(FILTERS, **{k: v for k, v in (cfg.get("scanner") or {}).items() if k in FILTERS})
    problems, skipped, quotes = [], collections.OrderedDict(), {}
    try:
        caution = cautionary(client)
    except smart.SmartError as e:
        caution = {}
        problems.append("%s: cautionary scrips not checked" % e)
    if allowed:
        per = (cfg.get("limits") or {}).get("quote_tokens_per_call", 50)
        quotes, failed = quote_full(client, [(s["segment"], s["token"]) for s in stocks], per)
        problems += failed
    keep = []
    for s in stocks:
        q = quotes.get((s["segment"], s["token"]))
        hit = [x for x in flags.get(s["symbol"], []) if x in SKIP_FLAGS]
        msg = caution_for(s, caution)
        s = dict(s, flags=flags.get(s["symbol"], []), quote=q, spread_pct=spread_pct(q) if q else None)
        if hit:
            skipped[s["symbol"]] = hit[0] + " in news.json"
        elif msg is not None:
            skipped[s["symbol"]] = "cautionary: " + msg[:60]
        elif allowed and q is None:
            skipped[s["symbol"]] = "no quote"
        elif q is not None and (q.get("ltp") or 0) < f["min_price"]:
            skipped[s["symbol"]] = "price %s under %s" % (money(q.get("ltp") or 0), money(f["min_price"]))
        elif allowed and s["spread_pct"] is None:
            skipped[s["symbol"]] = "no two-sided quote"
        elif s["spread_pct"] is not None and s["spread_pct"] > f["max_spread_pct"]:
            skipped[s["symbol"]] = "spread %.2f%% over %.2f%%" % (s["spread_pct"], f["max_spread_pct"])
        else:
            keep.append(s)
    if allowed:
        keep.sort(key=lambda s: -(s["quote"].get("ltp") or 0) * (s["quote"].get("tradeVolume") or 0))
    else:
        problems.append("spread and price not checked (%s), stocks taken by name" % market)
    return keep, skipped, f, problems, quotes


def after_history(item, s, f):
    """The checks that need the volume history: average volume, then the grade."""
    item["spread_pct"] = s["spread_pct"]
    item["liquidity_grade"] = grade(s["spread_pct"], item.get("avg_volume"))
    if item.get("avg_volume") is not None and item["avg_volume"] < f["min_avg_volume"]:
        return "average volume {:,} under {:,}".format(item["avg_volume"], int(f["min_avg_volume"]))
    if item.get("last") is None and s.get("quote") is None:
        price = item.get("prev_close") or item.get("range_low")
        if price is not None and price < f["min_price"]:
            return "price %s under %s" % (money(price), money(f["min_price"]))
    return None


# ---------------------------------------------------------------- commands

def header(title, now, a, cmd_source):
    return ["%s, %s IST (%s)" % (title, smart.stamp(now), "offline replay" if a.offline else cmd_source),
            "  " + smart.NOT_ADVICE]


def open_api(a, now, quote):
    """The client; a live run first checks the probe confirmed what it will call."""
    if not a.offline:
        smart.require(a.trader, "getCandleData")
        if quote:
            smart.require(a.trader, "quote")
    return smart.open_client(a.trader, now, a.offline)


def universe_lines(u, ban_path):
    idx = [i for i in u["items"] if i["kind"] == "index"]
    stocks = [i for i in u["items"] if i["kind"] == "stock"]
    out = ["  %-*s %s" % (WIDTH, "Indices", ", ".join(
        "%s (%s %s, options %s, lot %s)" % (i["symbol"], i["segment"], i["token"], i["fo_segment"], i["lot_size"])
        for i in idx) or "none")]
    out.append("  %-*s %d names with NSE stock options in the master" % (WIDTH, "F&O stocks", u["fo_names"]))
    out.append("  %-*s %s" % (WIDTH, "Ban removed", ", ".join(u["removed_ban"]) + " (in %s)" % show(ban_path)
                              if u["removed_ban"] else "none"))
    for line in u["left_out"]:
        out.append("  %-*s %s, left out" % (WIDTH, "Not matched", line))
    if u["ban_not_fo"]:
        out.append("  %-*s %s not an F&O name in the master (check %s)" % (
            WIDTH, "Ban list", ", ".join(u["ban_not_fo"]) + (" is" if len(u["ban_not_fo"]) == 1 else " are"),
            show(ban_path)))
    if u["ban_note"]:
        out.append("  %-*s %s" % (WIDTH, "Ban list", u["ban_note"]))
    out.append("  %-*s %d indices + %d stocks: %s" % (WIDTH, "Universe", len(idx), len(stocks),
                                                       ", ".join(i["symbol"] for i in u["items"])))
    return out


def write_watchlist(p, mode, now, source, notes, items, extra=None):
    doc = {"created_by": "in-scanner", "mode": mode, "as_of_ist": now.isoformat(), "source": source,
           "partial": bool(notes), "partial_note": "; ".join(notes) or None}
    doc.update(extra or {}, items=items)
    smart.write_json(p["watchlist"], doc)


def cmd_universe(a):
    p = smart.trader_paths(a.trader)
    now = smart.now_ist(a.now)
    u = load_universe(a, p, now)
    items = u["items"]
    src = "instrument master %s" % now.date()
    out = header("Universe", now, a, "instrument master, cached for today") + universe_lines(u, p["ban"])
    smart.write_json(os.path.join(a.trader, "universe.json"),
                     dict({"created_by": "in-scanner", "as_of_ist": now.isoformat(), "source": src}, **u))
    write_watchlist(p, "universe", now, src, [], [dict(i, flags=[]) for i in items])
    out.append("Wrote %s and %s (universe, %d items)" % (show(os.path.join(a.trader, "universe.json")),
                                                         show(p["watchlist"]), len(items)))
    print("\n".join(out))
    return 0 if items else 1


def finish(a, p, mode, now, out, items, failed, total, notes, skipped, limiter, source, extra, lead=None):
    """The common tail: skipped and notes, the fetched count, the file, the usage line, the exit code."""
    fetched = "Fetched %d of %d" % (total - len(failed), total)
    if lead is None:
        lead = ["%s; %s" % (fetched, "; ".join(failed))] if failed else []
    if skipped:
        out.append("  %-*s %s" % (WIDTH, "Skipped", ", ".join("%s (%s)" % kv for kv in skipped.items())))
    out += ["  %-*s %s" % (WIDTH, "Note", n) for n in notes]
    if not lead or failed:
        out.append(fetched + ".")
    every = lead + notes
    write_watchlist(p, mode, now, source, every, items, dict(extra, skipped=dict(skipped)))
    out.append("Wrote %s (%s, %d items, %s)" % (show(p["watchlist"]), mode, len(items),
                                                "partial" if every else "complete"))
    out.append(smart.usage_text(limiter))
    if not items:
        print("in-scanner: %s" % (every[0] if every else "nothing passed the filters"), file=sys.stderr)
        return 1
    print("\n".join(out))
    return 0


def cmd_orb(a):
    p = smart.trader_paths(a.trader)
    cfg = smart.load_config(a.trader)
    cal = smart.read_json(p["calendar"]) or {}
    now = smart.now_ist(a.now)
    allowed, market = smart.market_data_allowed(now, cal)
    if not smart.trading_day(now.date(), cal):
        raise smart.SmartError("no session today (%s): the opening range needs a trading day; daily works any day"
                               % market)
    end = (cfg.get("session") or {}).get("range_end", "09:30")
    done = now.strftime("%H:%M") >= end
    u = load_universe(a, p, now)
    todo = [i for i in u["items"] if i["kind"] == "index" or a.stocks]
    want = list(collections.OrderedDict.fromkeys(
        [(i["segment"], i["token"]) for i in todo] +
        [(i["volume_segment"], i["volume_token"]) for i in todo if i.get("volume_token")]))
    smart.subscribe(a.trader, "in-scanner", [{"segment": s, "token": t, "mode": "quote"} for s, t in want])
    client, limiter = open_api(a, now, allowed and a.stocks)
    stale_ms = int((cfg.get("freshness") or {}).get("stale_tick_sec", 3) * 1000)
    live = smart.read_latest(p["db"], [i["token"] for i in todo], int(now.timestamp() * 1000), stale_ms)
    flags = news_flags(p, now.date())
    notes, skipped, f = [], collections.OrderedDict(), FILTERS
    if a.stocks:
        stocks, skipped, f, notes, _ = stock_screen([i for i in todo if i["kind"] == "stock"], client, cfg, flags,
                                                    allowed, market)
        todo = [i for i in todo if i["kind"] == "index"] + stocks
    items, failed, lines = [], [], []
    for it in todo:
        try:
            item, problem = orb_item(a, p, it, now, cal, client, end, done, live)
        except smart.SmartError as e:
            item, problem = None, str(e)
        if item is None or (done and item["range_high"] is None):
            failed.append("%s: %s" % (it["symbol"], problem))
            lines.append("  %-*s FAILED: %s" % (WIDTH, it["symbol"], problem))
            continue
        if problem:
            notes.append("%s %s" % (it["symbol"], problem))
        item["flags"] = flags.get(it["symbol"], [])
        why = after_history(item, it, f) if it["kind"] == "stock" else None
        if why:
            skipped[it["symbol"]] = why
            continue
        lines.append(orb_line(item))
        item.pop("forming")
        items.append(item)
    out = header("Opening range scan", now, a, "live, Angel One SmartAPI")
    out.append("  %-*s %s to %s, %s" % (WIDTH, "Range", START, end, "complete for %d of %d" % (
        len(todo) - len(failed), len(todo)) if done else "forming until %s, nothing written as complete" % end))
    out += lines
    if a.stocks and u["ban_note"]:
        out.append("  %-*s %s" % (WIDTH, "Ban list", u["ban_note"]))
    out.append("  %-*s %d tokens asked of the live feed (owner in-scanner)" % (WIDTH, "Feed", len(want)))
    used = {i["source"] for i in items}
    source = " + ".join(s for s in ("feed", "candles") if s in used) or "none"
    return finish(a, p, "orb", now, out, items, failed, len(todo), notes, skipped, limiter, source,
                  {"range": {"start": START, "end": end}}, None if done else ["opening range forming until %s" % end])


# ---------------------------------------------------------------- daily

def wilder_atr(bars, n=14):
    tr = [max(h - l, abs(h - pc), abs(l - pc)) for (_, _, h, l, _, _), pc in zip(bars[1:], (b[4] for b in bars))]
    if len(tr) < n:
        return None
    atr = sum(tr[:n]) / n
    for x in tr[n:]:
        atr = (atr * (n - 1) + x) / n
    return atr


def daily_stats(bars, vol_bars, last_day):
    """The last completed session's numbers from ONE_DAY rows [time, o, h, l, c, v]."""
    close = bars[-1][4]
    year = [b for b in bars if str(b[0])[:10] > (last_day - datetime.timedelta(days=365)).isoformat()]
    hi, lo = max(b[2] for b in year), min(b[3] for b in year)
    vols = [b[5] or 0 for b in vol_bars]
    prior = statistics.mean(vols[-21:-1]) if len(vols) >= 21 else None
    return {"session": str(bars[-1][0])[:10], "prev_close": close,
            "volume_ratio": round(vols[-1] / prior, 2) if prior else None,
            "avg_volume": int(statistics.mean(vols[-20:])) if len(vols) >= 20 else None,
            "atr14": r2(wilder_atr(bars)), "high_52w": hi, "low_52w": lo, "sessions_52w": len(year),
            "pct_from_high": r2((close / hi - 1) * 100), "pct_from_low": r2((close / lo - 1) * 100)}


def daily_rows(client, p, seg, token, start, last, now):
    days, partial = smart.candles(client, p["cache"], seg, token, "ONE_DAY", start, last, now)
    return [b for _, rows in sorted(days.items()) for b in rows], partial


def daily_item(client, p, it, start, last, now, quote):
    """(watchlist item or None, problem or None)."""
    bars, why = daily_rows(client, p, it["segment"], it["token"], start, last, now)
    if not bars:
        return None, why or "no daily candles"
    if not it.get("volume_token"):
        vbars, vwhy = [], "no FUTIDX in the master"
    elif it["volume_token"] == it["token"]:
        vbars, vwhy = bars, why
    else:
        vbars, vwhy = daily_rows(client, p, it["volume_segment"], it["volume_token"], start, last, now)
    item = {k: it[k] for k in ("symbol", "kind", "segment", "token", "fo_segment", "lot_size")}
    item.update(daily_stats(bars, vbars, last))
    op = (quote or {}).get("open")
    item.update(gap_pct=r2((op / item["prev_close"] - 1) * 100) if op else None,
                volume_symbol=it.get("volume_symbol"), liquidity_grade=None)
    if it["kind"] == "index":
        item["avg_volume"] = None
    return item, None if item["volume_ratio"] is not None else "volume: %s" % (vwhy or "under 21 sessions")


def daily_line(it):
    bits = ["close %s" % money(it["prev_close"])]
    if it["gap_pct"] is not None:
        bits[0] += ", gap %+.2f%%" % it["gap_pct"]
    if it["volume_ratio"] is not None:
        bits.append("volume %.2fx the 20-day average%s" % (
            it["volume_ratio"], " (%s)" % it["volume_symbol"] if it["kind"] == "index" else ""))
    if it["atr14"] is not None:
        bits.append("ATR(14) %s" % money(it["atr14"]))
    bits.append("%+.2f%% from the 52-week high, %+.2f%% from the low%s" % (
        it["pct_from_high"], it["pct_from_low"],
        "" if it["sessions_52w"] >= 240 else " (%d sessions)" % it["sessions_52w"]))
    if it["kind"] == "stock":
        bits.append(grade_text(it))
    if it["flags"]:
        bits.append("flags " + ", ".join(it["flags"]))
    return "  %-*s %s" % (WIDTH, it["symbol"], "; ".join(bits))


def cmd_daily(a):
    p = smart.trader_paths(a.trader)
    cfg = smart.load_config(a.trader)
    cal = smart.read_json(p["calendar"]) or {}
    now = smart.now_ist(a.now)
    allowed, market = smart.market_data_allowed(now, cal)
    opened = allowed and now.strftime("%H:%M") >= START
    u = load_universe(a, p, now)
    todo = [i for i in u["items"] if i["kind"] == "index" or a.stocks]
    client, limiter = open_api(a, now, opened or (allowed and a.stocks))
    flags = news_flags(p, now.date())
    notes, skipped, f, quotes = [], collections.OrderedDict(), FILTERS, {}
    if a.stocks:
        stocks, skipped, f, notes, quotes = stock_screen([i for i in todo if i["kind"] == "stock"], client, cfg,
                                                         flags, allowed, market)
        todo = [i for i in todo if i["kind"] == "index"] + stocks
    if opened:
        got, problems = quote_full(client, [(i["segment"], i["token"]) for i in todo if i["kind"] == "index"],
                                   (cfg.get("limits") or {}).get("quote_tokens_per_call", 50))
        quotes.update(got)
        notes += problems
    last = smart.previous_trading_day(now.date(), cal)
    start = last - datetime.timedelta(days=DAILY_DAYS)
    items, failed, bad = [], [], []
    for it in todo:
        try:
            item, problem = daily_item(client, p, it, start, last, now, quotes.get((it["segment"], it["token"])))
        except smart.SmartError as e:
            item, problem = None, str(e)
        if item is None:
            failed.append("%s: %s" % (it["symbol"], problem))
            bad.append("  %-*s FAILED: %s" % (WIDTH, it["symbol"], problem))
            continue
        if problem:
            notes.append("%s %s" % (it["symbol"], problem))
        item["flags"] = flags.get(it["symbol"], [])
        why = after_history(item, it, f) if it["kind"] == "stock" else None
        if why:
            skipped[it["symbol"]] = why
            continue
        items.append(item)
    items.sort(key=lambda i: (i["kind"] != "index", -(i["avg_volume"] or 0) * i["prev_close"]))
    gap = "from the FULL quote's open" if opened else "not yet (%s)" % ("before %s" % START if allowed else market)
    out = header("Daily scan", now, a, "live, Angel One SmartAPI")
    out.append("  %-*s ONE_DAY candles to %s %s; gap %s" % (WIDTH, "Session", smart.WEEKDAYS[last.weekday()],
                                                          last, gap))
    out += [daily_line(i) for i in items] + bad
    if a.stocks and u["ban_note"]:
        out.append("  %-*s %s" % (WIDTH, "Ban list", u["ban_note"]))
    return finish(a, p, "daily", now, out, items, failed, len(todo), notes, skipped, limiter,
                  "candles ONE_DAY" + (" + quote" if opened else ""), {"session": last.isoformat()})


def main(argv=None):
    ap = argparse.ArgumentParser(description="in-scanner: universe, opening range and daily numbers "
                                             "(read-only, no orders).")
    ap.add_argument("mode", nargs="?", default="orb", choices=("orb", "daily", "universe"))
    ap.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    ap.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-24 09:31"')
    ap.add_argument("--offline", metavar="DIR", help="replay saved replies; DIR/master.json is the master")
    ap.add_argument("--stocks", action="store_true", help="orb and daily: add the F&O stocks, most liquid first")
    a = ap.parse_args(argv)
    try:
        return {"universe": cmd_universe, "orb": cmd_orb, "daily": cmd_daily}[a.mode](a)
    except smart.SmartError as e:
        print("in-scanner: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

