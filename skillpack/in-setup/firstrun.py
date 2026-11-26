#!/usr/bin/env python3
"""First run and capability probe for in-setup. Standard library only.
Read-only: it logs in for market data and calls no order endpoint.

    python in-setup/firstrun.py init      # trader/ folders, config.json and calendar.json from the templates
    python in-setup/firstrun.py login     # one login per IST day, server clock check
    python in-setup/firstrun.py master    # today's instrument master and what it says
    python in-setup/firstrun.py probe     # all of it, writes trader/state/capabilities.json
    python in-setup/firstrun.py probe --key-label historical --record trader/recorded
    python in-setup/firstrun.py probe --limits     # also the quote burst and the stream token cap
    python in-setup/firstrun.py probe --offline tests/fixtures/in-setup --now "2026-11-24 10:20"

Credentials come only from the environment: ANGEL_API_KEY, ANGEL_CLIENT_CODE,
ANGEL_PIN and optionally ANGEL_TOTP_SECRET (without it, login asks for the
6-digit code in the terminal). They are never printed, logged or written.
The day's session tokens go to trader/state/session.json, owner-only.

The probe measures what the SmartAPI documentation does not say: the server
clock, the master's field formats, which endpoints this API key may call,
the stream's exchange-type codes, feed lag and heartbeat, and (with --limits)
whether quote takes more than 1 call a second and what the stream does past
1,000 tokens. Live checks run only 09:00 to 15:45 IST on trading days; outside
that the probe says so and skips them. An offline replay never counts as
confirmed.

Exit codes: 0 = done (probe: capabilities.json written), 1 = an error, in one line.
"""

import argparse
import datetime
import json
import os
import platform
import socket
import statistics
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

ENDPOINT_ORDER = ("loginByPassword", "getProfile", "getCandleData", "getOIData", "quote", "quote_options",
                  "optionGreek", "putCallRatio", "estimateCharges")
FEATURES = ENDPOINT_ORDER + ("charges", "stream", "stream_nse_cm", "stream_nse_fo", "stream_bse_cm", "stream_bse_fo")
CHARGES_MATCH_INR = Decimal("0.10")
QUOTE_FIELDS = ("opnInterest", "depth", "upperCircuit", "lowerCircuit", "exchFeedTime")
PROBE_SECONDS = 20
PING_EVERY_MS = 25000  # the stream wants a heartbeat about every 30 s
BURST = 5
TOKEN_CAP_TARGET = 1001
NOT_ADVICE = "  Not financial advice: this measures the data feed and says nothing about any trade."


# ---------------------------------------------------------------- folders

def trader_paths(trader):
    state = os.path.join(trader, "state")
    return {"trader": trader, "state": state, "cache": os.path.join(trader, "cache"),
            "config": os.path.join(trader, "config.json"), "calendar": os.path.join(trader, "calendar.json"),
            "charges": os.path.join(trader, "charges.json"),
            "caps": os.path.join(state, "capabilities.json"), "session": os.path.join(state, "session.json"),
            "session_offline": os.path.join(state, "session.offline.json")}


def show(path):
    return path.replace(os.sep, "/")


def ensure(p):
    """trader/ folders, plus config.json, calendar.json and charges.json from the templates if missing.
    Never overwrites."""
    for sub in ("state", "cache", os.path.join("history", "chain")):
        os.makedirs(os.path.join(p["trader"], sub), exist_ok=True)
    made = []
    for name in ("config", "calendar", "charges"):
        if not os.path.exists(p[name]):
            smart.write_json(p[name], smart.read_json(os.path.join(HERE, name + ".example.json")))
            made.append(name)
    return made


def stamp(now):
    return "%s %s" % (smart.WEEKDAYS[now.weekday()], now.strftime("%Y-%m-%d %H:%M"))


def make_client(a, p, cfg, now):
    """(client, credentials, limiter). Offline: the fixtures, a pinned clock, no limiter, no network."""
    if getattr(a, "offline", None):
        creds = smart.credentials(required=False)
        epoch = now.timestamp()
        return smart.Client(creds["api_key"], transport=smart.Replay(a.offline), clock=lambda: epoch), creds, None
    creds = smart.credentials()
    api = cfg.get("api") or {}
    ips = {"local": smart.local_ip(), "mac": smart.mac_address(),
           "public": smart.public_ip(api.get("client_public_ip"), p["state"], now.date().isoformat())}
    limiter = smart.Limiter(p["state"], smart.caps_from_config(cfg.get("limits")))
    return smart.Client(creds["api_key"], limiter=limiter, record=getattr(a, "record", None), ips=ips), creds, limiter


def usage_text(limiter):
    return limiter.usage_line() if limiter else "API usage: none (offline replay)"


# ---------------------------------------------------------------- text

def clock_line(c):
    if c.get("ok") is None:
        return "not checked: " + c.get("note", "")
    return "server %+.1f s from this computer (Date header, 1 s resolution): %s" % (
        c["offset_sec"], "OK" if c["ok"] else "OFF BY MORE THAN 2 s, fix the system clock (TOTP login depends on it)")


def master_lines(m):
    agree, checked = m["strike_scale_agree"]
    out = ["  %-15s %s rows; strike scale %s (%d of %d option rows agree); tick_size scale %s, inferred"
           % ("Master", "{:,}".format(m["rows"]), m["strike_scale"], agree, checked, m["tick_size_scale"]),
           "  %-15s %s" % ("Segments", ", ".join("%s %s" % (k, "{:,}".format(v)) for k, v in m["exch_seg"].items()))]
    if m["fields_missing"]:
        out.append("  %-15s %s" % ("No field", ", ".join(m["fields_missing"])))
    if m["unknown_segments"]:
        out.append("  %-15s %s" % ("Unknown seg", ", ".join(m["unknown_segments"])))
    for u, f in m["underlyings"].items():
        if not f.get("index_token") and not f.get("expiries"):
            out.append("  %-15s not in the master" % u)
            continue
        out.append("  %-15s index %s, token %s (%s)" % (u, f["index_symbol"], f["index_token"], f["index_segment"]))
        if f.get("expiries"):
            lots = " (lot sizes seen: %s)" % f["lot_sizes_seen"] if f.get("lot_sizes_seen") else ""
            out.append("  %-15s options %s, lot %s%s, strike step %g, freeze %s; expiries %s"
                       % ("", f["segment"], f["lot_size"], lots, f["strike_step"] or 0, f["freeze_qty"],
                          ", ".join("%s %s" % (w, d) for d, w in f["expiries"])))
    return out


def endpoint_text(name, e):
    if "skipped" in e:
        return "skipped (%s)" % e["skipped"]
    if not e.get("ok"):
        return ("FAILED HTTP %s %s %s" % (e.get("http"), e.get("errorcode") or "", e.get("message") or "")).strip()
    bits = ["OK"]
    if e.get("note"):
        bits.append(e["note"])
    if "bars" in e:
        bits.append("%d five-minute bars%s" % (e["bars"], ", index volume all 0" if e.get("index_volume_all_zero") else ""))
    if "points" in e:
        bits.append("%d OI points" % e["points"])
    if "rows" in e:
        bits.append("%d rows" % e["rows"])
    if "broker_inr" in e:
        bits.append("broker Rs %.2f vs charges.json Rs %.2f for one lot bought and sold: %s" % (
            e["broker_inr"], e["local_inr"], "match" if e["matches"] else "DIFFER by Rs %.2f, fix charges.json"
            % abs(e["diff_inr"])))
    if "fetched" in e:
        bits.append("%d fetched, %d unfetched" % (e["fetched"], e["unfetched"]))
        ages = [v for v in e["age_sec"].values() if v is not None]
        if ages:
            bits.append("snapshot age worst %.1f s" % max(ages))
        if name == "quote_options":
            lacking = [f for f in QUOTE_FIELDS if f not in e["fields"]]
            bits.append("FULL lacks " + ", ".join(lacking) if lacking else "FULL has OI, depth, circuits, feed time")
    return ", ".join(bits)


def stream_lines(s):
    if s is None:
        return ["not run"]
    if "skipped" in s:
        return ["skipped (%s)" % s["skipped"]]
    if s.get("error"):
        return ["FAILED " + s["error"]]
    said = "; server said: " + " | ".join(s["server_text"]) if s["server_text"] else ""
    if not s["ok"]:
        return ["connected, no ticks in %d s%s" % (s["seconds"], said)]
    codes = ", ".join("%s=%s" % (seg, c) for seg, c in s["exchange_types"].items() if c)
    quiet = [seg for seg, c in s["exchange_types"].items() if not c]
    first = "%d ticks in %d s; feed lag median %d ms, worst %d ms" % (
        s["ticks"], s["seconds"], s["lag_ms"]["median"], s["lag_ms"]["worst"])
    second = "exchange types %s%s; pong %s" % (
        codes, "; no ticks for " + ", ".join(quiet) if quiet else "",
        "%d ms" % s["pong_ms"] if s["pong_ms"] is not None else "not received")
    if "price_scale_ok" in s:
        second += "; paise scale %s against the REST quote" % ("OK" if s["price_scale_ok"] else "MISMATCH")
    return [first, second + said]


def limits_lines(lim, s):
    if lim is None:
        return ["not tested (add --limits)"]
    if "skipped" in lim:
        return ["skipped (%s)" % lim["skipped"]]
    out = ["quote burst: " + lim["quote_burst"]["verdict"]]
    cap = (s or {}).get("token_cap") or {}
    if cap.get("tested"):
        out.append("token cap: %d subscribed, %d ticks after%s" % (
            cap["subscribed"], cap["ticks_after"],
            "; server said: " + " | ".join(cap["server_text"]) if cap["server_text"] else "; server said nothing"))
    elif cap:
        out.append("token cap: not tested, " + cap["note"])
    return out


# ---------------------------------------------------------------- probe parts

def history_window(now):
    start = (now - datetime.timedelta(days=4)).replace(hour=9, minute=15)
    return start.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M")


def feed_age(text, received):
    try:
        t = datetime.datetime.strptime(text, "%d-%b-%Y %H:%M:%S").replace(tzinfo=smart.IST)
    except (TypeError, ValueError):
        return None
    return round(received - t.timestamp(), 1)


def quote_facts(r):
    e = r.summary()
    if not r.ok:
        return e
    d = r.data or {}
    fetched = d.get("fetched") or []
    e.update(fetched=len(fetched), unfetched=len(d.get("unfetched") or []),
             fields=sorted(set().union(*[set(f) for f in fetched])) if fetched else [],
             ltp={str(f.get("symbolToken")): f.get("ltp") for f in fetched},
             age_sec={str(f.get("symbolToken")): feed_age(f.get("exchFeedTime"), r.received) for f in fetched})
    return e


def counted(r, key, n):
    e = r.summary()
    if r.ok:
        e[key] = n(r.data)
    return e


def charges_check(client, charges, row, premium, lot):
    """The broker's estimateCharges for one lot bought and sold, against charges.json."""
    seg = smart.segment(row["exch_seg"])
    orders = [{"product_type": "INTRADAY", "transaction_type": side, "quantity": str(lot), "price": "%.2f" % premium,
               "exchange": seg, "symbol_name": row["symbol"], "token": row["token"]} for side in ("BUY", "SELL")]
    r = client.call("estimateCharges", {"orders": orders}, retry=False)
    e = r.summary()
    if not r.ok:
        return e
    try:
        broker = Decimal(str(r.data["summary"]["total_charges"]))
    except (KeyError, TypeError):
        e["note"] = "no summary.total_charges in the reply"
        return e
    local = sum((smart.option_charges(side, premium, lot, seg, charges)["total"] for side in ("BUY", "SELL")),
                Decimal(0))
    e.update(broker_inr=float(broker), local_inr=float(local), diff_inr=float(broker - local),
             matches=abs(broker - local) <= CHARGES_MATCH_INR, premium=premium, lot=lot, symbol=row["symbol"])
    return e


def quote_burst(client, token, n=BURST):
    """n quote calls as fast as the API answers, to see whether 1/s or 10/s is the real limit."""
    t0 = client.clock()
    replies = [client.call("quote", {"mode": "LTP", "exchangeTokens": {"NSE": [token]}}, caps=((n, 1),), retry=False)
               for _ in range(n)]
    secs = round(client.clock() - t0, 2)
    first = next((r for r in replies if r.status == 403), None)
    refused = sum(1 for r in replies if not r.ok)
    if first:
        verdict = "HTTP 403 inside a burst of %d in %.2f s: keep 1 per second" % (n, secs)
    elif refused:
        verdict = "%d of %d refused without HTTP 403: read the replies in capabilities.json" % (refused, n)
    else:
        verdict = "%d calls in %.2f s all accepted: more than 1 per second works; the design cap stays 1/s" % (n, secs)
    return {"tested": True, "calls": n, "seconds": secs, "accepted": n - refused,
            "http_403": sum(1 for r in replies if r.status == 403),
            "first_403": first.summary() if first else None,
            "replies": [r.summary() for r in replies if not r.ok][:3], "verdict": verdict}


def lag_stats(v):
    return {"median": int(statistics.median(v)), "worst": max(v)} if v else {"median": None, "worst": None}


def frame_record(op, data, ms):
    return {"ms": ms, "op": op, "text": data.decode("utf-8", "replace")} if op == 1 else {"ms": ms, "op": op, "hex": data.hex()}


def listen(conn, subs, seconds, clock_ms, record):
    """Subscribe in SnapQuote mode, ping, and measure feed lag (receipt minus exchange time)."""
    conn.send(json.dumps({"correlationID": "probe0001", "action": 1,
                          "params": {"mode": smart.MODES["snapquote"],
                                     "tokenList": [{"exchangeType": c, "tokens": [t]} for c, _, t in subs]}}))
    ping_at = clock_ms()
    conn.send("ping")
    expect = {(c, t): seg for c, seg, t in subs}
    lags = {seg: [] for _, seg, _ in subs}
    ltp, texts, pong, other, bad = {}, [], None, 0, 0
    deadline = clock_ms() + seconds * 1000
    while clock_ms() < deadline:
        if clock_ms() - ping_at > PING_EVERY_MS:
            ping_at = clock_ms()
            conn.send("ping")
        try:
            op, data, ms = conn.recv(timeout=max(0.1, min(5.0, (deadline - clock_ms()) / 1000.0)))
        except socket.timeout:  # live: quiet for 5 s, keep listening; a replay: out of frames
            if isinstance(conn, smart.ReplayStream) or clock_ms() >= deadline:
                break
            continue
        if record is not None:
            record.append(frame_record(op, data, ms))
        if op == 8:
            texts.append("closed by the server")
            break
        if op == 1:
            text = data.decode("utf-8", "replace").strip()
            if text == "pong":
                pong = ms - ping_at if pong is None else pong
            else:
                texts.append(text[:200])
            continue
        try:
            tick = smart.parse_tick(data)
        except ValueError:
            bad += 1
            continue
        seg = expect.get((tick["exchange_type"], tick["token"]))
        if seg is None:
            other += 1
            continue
        lags[seg].append(ms - tick["exch_ms"])
        ltp.setdefault(tick["token"], tick["ltp"])   # the first tick: nearest in time to the REST snapshot
    every = [x for v in lags.values() for x in v]
    return {"ok": bool(every), "ticks": len(every), "seconds": seconds, "lag_ms": lag_stats(every),
            "by_segment": {seg: dict(ticks=len(v), **lag_stats(v)) for seg, v in lags.items()},
            "exchange_types": {seg: (c if lags[seg] else None) for c, seg, _ in subs},
            "pong_ms": pong, "server_text": texts[:5], "unmatched_ticks": other, "bad_packets": bad, "ltp": ltp}


def token_cap(conn, rows, subs, clock_ms, record, target=TOKEN_CAP_TARGET, listen_sec=5):
    """Subscribe up to `target` tokens in LTP mode and record what the server says."""
    have = {t for _, _, t in subs}
    pool = [r["token"] for r in rows if r.get("instrumenttype") == "OPTIDX"
            and smart.segment(r.get("exch_seg")) == "NFO" and r.get("token") not in have]
    need = target - len(have)
    if len(pool) < need:
        return {"tested": False, "note": "only %d NFO option tokens in the master, %d needed" % (len(pool), need)}
    conn.send(json.dumps({"correlationID": "probecap1", "action": 1,
                          "params": {"mode": smart.MODES["ltp"],
                                     "tokenList": [{"exchangeType": smart.SEGMENT_CODES["NFO"], "tokens": pool[:need]}]}}))
    texts, ticks = [], 0
    deadline = clock_ms() + listen_sec * 1000
    while clock_ms() < deadline:
        try:
            op, data, ms = conn.recv(timeout=max(0.1, (deadline - clock_ms()) / 1000.0))
        except socket.timeout:
            break
        if record is not None:
            record.append(frame_record(op, data, ms))
        if op == 8:
            texts.append("closed by the server")
            break
        if op == 1:
            text = data.decode("utf-8", "replace").strip()
            if text != "pong":
                texts.append(text[:200])
        else:
            ticks += 1
    return {"tested": True, "subscribed": target, "ticks_after": ticks, "server_text": texts[:3]}


def stream_probe(a, session, creds, subs, rest_ltp, rows):
    if a.offline:
        epoch_ms = int(smart.now_ist(a.now).timestamp() * 1000)
        conn, auth, clock_ms = smart.ReplayStream(os.path.join(a.offline, "ws.jsonl")), "offline", lambda: epoch_ms
    else:
        try:
            conn, auth = smart.open_stream(session, creds)
        except smart.SmartError as e:
            return {"ok": False, "error": str(e)}
        clock_ms = lambda: int(time.time() * 1000)  # noqa: E731
    record = [] if a.record else None
    try:
        out = listen(conn, subs, a.seconds, clock_ms, record)
        out["auth_header"] = auth
        if a.limits:
            out["token_cap"] = token_cap(conn, rows, subs, clock_ms, record)
    except OSError as e:
        out = {"ok": False, "error": "stream dropped: %s" % e}
    finally:
        conn.close()
    if record:
        smart.write_bytes(os.path.join(a.record, "ws.jsonl"),
                          "".join(json.dumps(f) + "\n" for f in record).encode("utf-8"))
    pairs = [(v, rest_ltp.get(t)) for t, v in out.get("ltp", {}).items() if rest_ltp.get(t)]
    if pairs:
        # a paise/rupee slip is 100x off; 5% leaves room for a fast premium (an expiry-day option moves 3% in 20 s)
        out["price_scale_ok"] = all(abs(ws / rest - 1) < 0.05 for ws, rest in pairs)
    return out


def key_confirmed(k):
    out = [n for n in ENDPOINT_ORDER if (k["endpoints"].get(n) or {}).get("ok")]
    if (k["endpoints"].get("estimateCharges") or {}).get("matches"):
        out.append("charges")
    s = k.get("stream") or {}
    if s.get("ok"):
        out.append("stream")
    out += ["stream_" + seg for seg, code in (s.get("exchange_types") or {}).items() if code]
    return out


def confirmed(keys):
    """feature -> the key labels that confirmed it, live runs only. Other skills
    refuse any feature missing here."""
    out = {}
    for k in keys.values():
        if k.get("source") == "live":
            for f in key_confirmed(k):
                out.setdefault(f, set()).add(k["label"])
    return {f: sorted(out[f]) for f in FEATURES if f in out}


# ---------------------------------------------------------------- commands

def cmd_init(a):
    p = trader_paths(a.trader)
    made = ensure(p)
    cfg = smart.read_json(p["config"]) or {}
    cal = smart.read_json(p["calendar"]) or {}
    now = smart.now_ist(a.now)
    out = ["Set up %s/ (state, cache, history/chain)." % show(a.trader)]
    for name in ("config", "calendar", "charges"):
        out.append("  %-14s %s" % (name + ".json", "written from the template" if name in made else "kept (yours)"))
    out.append("  %-14s %s OK" % ("Python", platform.python_version()))
    dotenv = smart.read_env_file(".env")

    def where(k):
        return "set" if os.environ.get(k) else "set (.env)" if dotenv.get(k) else None
    have = ["%s %s" % (k, where(k) or "MISSING") for k in smart.CREDENTIALS]
    have.append("ANGEL_TOTP_SECRET " + (where("ANGEL_TOTP_SECRET") or "not set (login asks for the 6-digit code)"))
    out.append("  %-14s %s" % ("Credentials", ", ".join(have)))
    if (cfg.get("account") or {}).get("capital_inr") is None or (cfg.get("api") or {}).get("risk_free_rate_pct") is None:
        out.append("  %-14s set account.capital_inr and api.risk_free_rate_pct in %s: the trading skills refuse "
                   "to run until you do" % ("To do", show(p["config"])))
    if not any(d.startswith(str(now.year)) for d in (cal.get("holidays") or {})):
        out.append("  %-14s calendar.json has no %d holidays: copy them from the NSE and BSE holiday circulars"
                   % ("To do", now.year))
    if not (smart.read_json(p["charges"]) or {}).get("verified"):
        out.append("  %-14s charges.json is unverified: the probe checks it against the broker's estimateCharges"
                   % "To do")
    out.append("Keep %s/ out of git: it holds the day's session token." % show(a.trader))
    out.append("Next: python in-setup/firstrun.py probe, in market hours (09:15 to 15:30 IST) for the live checks.")
    print("\n".join(out))
    return 0


def cmd_login(a):
    p = trader_paths(a.trader)
    ensure(p)
    cfg = smart.read_json(p["config"]) or {}
    now = smart.now_ist(a.now)
    client, creds, limiter = make_client(a, p, cfg, now)
    fresh = smart.Session(client, creds, p["session"], now.date().isoformat()).start()
    prof = client.call("getProfile")
    if not prof.ok:
        raise smart.SmartError("getProfile failed: " + prof.why())
    print("Logged in: %s session for %s IST (key %s)." % (
        "new" if fresh else "today's", now.date(), smart.key_fingerprint(creds["api_key"])))
    print("  Clock  " + clock_line(smart.clock_check(prof)))
    print(usage_text(limiter))
    return 0


def cmd_master(a):
    p = trader_paths(a.trader)
    ensure(p)
    now = smart.now_ist(a.now)
    rows = smart.load_master(p["cache"], now.date().isoformat(), path=a.offline)
    m = smart.describe_master(rows, now.date())
    print("\n".join(["Instrument master, %s IST (%s)" % (stamp(now), "offline file" if a.offline
                                                          else "Angel One, cached for today")] + master_lines(m)))
    return 0


def cmd_probe(a):
    p = trader_paths(a.trader)
    ensure(p)
    cfg = smart.read_json(p["config"]) or {}
    cal = smart.read_json(p["calendar"]) or {}
    now = smart.now_ist(a.now)
    today = now.date().isoformat()
    source = "offline" if a.offline else "live"
    client, creds, limiter = make_client(a, p, cfg, now)
    fp = smart.key_fingerprint(creds["api_key"])
    label = a.key_label or fp
    eps = {}

    sess = smart.Session(client, creds, p["session_offline" if a.offline else "session"], today)
    fresh = sess.start()
    eps["loginByPassword"] = fresh.summary() if fresh else {"ok": True, "note": "today's session reused"}
    prof = client.call("getProfile", retry=False)
    eps["getProfile"] = prof.summary()
    clock = smart.clock_check(prof)

    rows = smart.load_master(p["cache"], today, path=os.path.join(a.offline, "master.json") if a.offline else None)
    master = smart.describe_master(rows, now.date())
    scale = master["strike_scale"]
    idx = {u: master["underlyings"][u].get("index_token") for u in ("NIFTY", "SENSEX")}
    start, end = history_window(now)

    if idx["NIFTY"]:  # history: allowed outside market hours
        r = client.call("getCandleData", {"exchange": "NSE", "symboltoken": idx["NIFTY"], "interval": "FIVE_MINUTE",
                                          "fromdate": start, "todate": end}, retry=False)
        e = r.summary()
        if r.ok:
            bars = r.data or []
            e.update(bars=len(bars), index_volume_all_zero=bool(bars) and all(len(c) > 5 and not c[5] for c in bars))
        eps["getCandleData"] = e

    allowed, market = smart.market_data_allowed(now, cal)
    spot = {}
    if allowed:
        r = client.call("quote", {"mode": "FULL", "exchangeTokens": {
            "NSE": [idx["NIFTY"]] if idx["NIFTY"] else [], "BSE": [idx["SENSEX"]] if idx["SENSEX"] else []}},
            retry=False)
        eps["quote"] = quote_facts(r)
        spot = {u: (eps["quote"].get("ltp") or {}).get(idx[u]) for u in idx}
    opts = {u: smart.nearest_option(rows, u, spot.get(u), scale, now.date()) for u in ("NIFTY", "SENSEX")}

    if opts["NIFTY"]:  # history
        eps["getOIData"] = counted(client.call("getOIData", {
            "exchange": "NFO", "symboltoken": opts["NIFTY"]["token"], "interval": "FIVE_MINUTE",
            "fromdate": start, "todate": end}, retry=False), "points", lambda d: len(d or []))

    stream = limits = None
    if allowed:
        tokens = {seg: [opts[u]["token"]] for u, seg in (("NIFTY", "NFO"), ("SENSEX", "BFO")) if opts[u]}
        eps["quote_options"] = quote_facts(client.call("quote", {"mode": "FULL", "exchangeTokens": tokens},
                                                       retry=False))
        if opts["NIFTY"]:
            eps["optionGreek"] = counted(client.call("optionGreek", {"name": "NIFTY", "expirydate": opts["NIFTY"]["expiry"]},
                                                     retry=False), "rows", lambda d: len(d or []))
        eps["putCallRatio"] = counted(client.call("putCallRatio", retry=False), "rows", lambda d: len(d or []))
        rest_ltp = dict(eps["quote"].get("ltp") or {}, **(eps["quote_options"].get("ltp") or {}))
        subs = [(code, seg, tok) for code, seg, tok in (
            (1, "nse_cm", idx["NIFTY"]), (2, "nse_fo", (opts["NIFTY"] or {}).get("token")),
            (3, "bse_cm", idx["SENSEX"]), (4, "bse_fo", (opts["SENSEX"] or {}).get("token"))) if tok]
        stream = stream_probe(a, sess.data, creds, subs, rest_ltp, rows)
        if a.limits and idx["NIFTY"]:
            limits = {"quote_burst": quote_burst(client, idx["NIFTY"])}
    else:
        for name in ("quote", "quote_options", "optionGreek", "putCallRatio"):
            eps[name] = {"skipped": market}
        stream = {"skipped": market}
        if a.limits:
            limits = {"skipped": market}

    charges = smart.read_json(p["charges"]) or {}
    if opts["NIFTY"]:   # a calculator, not market data: allowed any time
        premium = ((eps.get("quote_options") or {}).get("ltp") or {}).get(opts["NIFTY"]["token"]) or 100.0
        eps["estimateCharges"] = charges_check(client, charges, opts["NIFTY"], premium,
                                               master["underlyings"]["NIFTY"].get("lot_size") or 1)

    caps = smart.read_json(p["caps"]) or {}
    keys = caps.get("keys") or {}
    before = (keys.get(("offline-" if a.offline else "") + fp) or {}).get("endpoints") or {}
    for name, e in list(eps.items()):   # a rate-limit refusal is not a capability: keep what worked live before
        if e.get("http") == 403 and (before.get(name) or {}).get("ok"):
            eps[name] = dict(before[name], note="HTTP 403 rate limit this run; worked on an earlier probe")
    k = {"label": label, "source": source, "probed_at_ist": now.isoformat(), "market": market,
         "endpoints": {n: eps[n] for n in ENDPOINT_ORDER if n in eps}, "stream": stream, "limits": limits}
    keys[("offline-" if a.offline else "") + fp] = k
    caps.update(created_by="in-setup", as_of_ist=now.isoformat(), python=platform.python_version(),
                note="Measured facts about the data feed, not financial advice. Offline runs never count as confirmed.",
                clock=clock, master=master, keys=keys, confirmed=confirmed(keys))
    smart.write_json(p["caps"], caps)

    out = ["Capability probe, %s IST (%s; key %s)" % (stamp(now), "offline replay" if a.offline
                                                      else "live, Angel One SmartAPI", label),
           NOT_ADVICE, "  %-15s %s" % ("Clock", clock_line(clock))]
    out += master_lines(master)
    out.append("  %-15s %s" % ("Market", market))
    for name in ENDPOINT_ORDER:
        if name in eps:
            out.append("  %-15s %s" % (name, endpoint_text(name, eps[name])))
    for i, line in enumerate(stream_lines(stream)):
        out.append("  %-15s %s" % ("Stream" if i == 0 else "", line))
    for i, line in enumerate(limits_lines(limits, stream)):
        out.append("  %-15s %s" % ("Limits" if i == 0 else "", line))
    mine = key_confirmed(k)
    out.append("  %-15s %s" % ("Replay OK" if a.offline else "Confirmed", ", ".join(mine) or "nothing"))
    rest = [f for f in FEATURES if f not in mine]
    if rest:
        out.append("  %-15s %s" % ("Not yet", ", ".join(rest)))
    out.append("Wrote %s%s" % (show(p["caps"]), " (offline: saved, never counted as confirmed)" if a.offline else ""))
    if not allowed:
        out.append("Run it again on a trading day between 09:15 and 15:30 IST for the live checks.")
    out.append(usage_text(limiter))
    print("\n".join(out))
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    common.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-24 10:20"')
    ap = argparse.ArgumentParser(description="in-setup: first run and capability probe (read-only, no orders).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("init", parents=[common], help="trader/ folders and the config templates")
    sub.add_parser("login", parents=[common], help="one login per IST day and the clock check")
    s = sub.add_parser("master", parents=[common], help="today's instrument master and what it says")
    s.add_argument("--offline", metavar="FILE", help="a saved OpenAPIScripMaster.json")
    s = sub.add_parser("probe", parents=[common], help="measure everything, write state/capabilities.json")
    s.add_argument("--offline", metavar="DIR", help="replay saved replies (see tests/fixtures/in-setup)")
    s.add_argument("--record", metavar="DIR", help="save the live replies, redacted, as fixtures")
    s.add_argument("--key-label", help="a name for this API key, e.g. historical or feed")
    s.add_argument("--seconds", type=int, default=PROBE_SECONDS, help="how long to listen to the stream")
    s.add_argument("--limits", action="store_true", help="also the quote burst and the 1,001-token subscription")
    a = ap.parse_args(argv)
    if sys.version_info < (3, 9):
        print("in-setup: Python 3.9 or newer is needed, this is %s" % platform.python_version(), file=sys.stderr)
        return 1
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"init": cmd_init, "login": cmd_login, "master": cmd_master, "probe": cmd_probe}[a.cmd](a)
    except smart.SmartError as e:
        print("in-setup: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
