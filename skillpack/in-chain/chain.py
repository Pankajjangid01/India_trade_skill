#!/usr/bin/env python3
"""Option chain, analytics and strike pick for in-chain. Standard library only.
Read-only market data: it has no order function and never places an order.

    python in-chain/chain.py snapshot NIFTY                 # -> trader/chain/NIFTY.json + a history line
    python in-chain/chain.py pick NIFTY                     # direction from trader/entry.json -> trader/pick.json
    python in-chain/chain.py pick SENSEX --direction bearish
    python in-chain/chain.py history NIFTY --last 5         # today's snapshots from the history file
    python in-chain/chain.py snapshot NIFTY --offline tests/fixtures/in-chain/nifty --now "2026-11-25 10:20"

The expiry, strikes, lot size, tick and freeze quantity come from the
instrument master. Spot comes from the live feed's tick store when fresh,
else a FULL quote. Option rows come from FULL quotes (at most 50 tokens a
call, spaced by the shared limiter), overlaid with fresher feed ticks.
Greeks: optionGreek for NSE (greeks_source API); local Black-Scholes for BSE
(greeks_source COMPUTED, the rate from config). Every number is descriptive:
a chain says what is, never what will be.

Exit codes: 0 = a snapshot, a pick or history lines; 1 = no strike passed the
pick rules, or an error, in one line on stderr.
"""

import argparse
import collections
import datetime
import json
import math
import os
import statistics
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
import smart  # noqa: E402

MAX_TOKENS_PER_QUOTE = 50      # the documented cap per quote call
OI_LOOKBACK_MIN, OI_SLACK_MIN = 15, 5
IV_RANK_MIN_DAYS, IV_RANK_MAX_DAYS = 20, 252
TOP_OI = 3
BUILDUP = {(1, 1): "Long Built Up", (-1, 1): "Short Built Up", (1, -1): "Short Covering", (-1, -1): "Long Unwinding"}


# ---------------------------------------------------------------- small helpers

def show(path):
    return path.replace(os.sep, "/")


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def kt(k):
    """A strike as written: 23500, 1422.5."""
    return ("%.2f" % k).rstrip("0").rstrip(".")


def f2(x, nd=2):
    return "-" if x is None else "%.*f" % (nd, x)


def feed_ms(text):
    """quote's exchFeedTime ("25-Nov-2026 10:19:59", IST) -> epoch ms; None when unreadable."""
    try:
        t = datetime.datetime.strptime(text, "%d-%b-%Y %H:%M:%S").replace(tzinfo=smart.IST)
    except (TypeError, ValueError):
        return None
    return int(t.timestamp() * 1000)


def history_path(p, name, day):
    return os.path.join(p["history"], "chain", name, day + ".jsonl")


def read_lines(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:   # a torn line: skip it, keep the rest
                    pass
    except FileNotFoundError:
        pass
    return out


# ---------------------------------------------------------------- master

def contracts(rows, name, today, types):
    """{expiry date: rows} of `name`'s contracts of these instrument types expiring today or later."""
    out = collections.defaultdict(list)
    for r in rows:
        if r.get("name") == name and r.get("instrumenttype") in types:
            d = smart.expiry_date(r.get("expiry"))
            if d and d >= today:
                out[d].append(r)
    return out


def choose_expiry(expiries, now, last_entry):
    """The nearest expiry; the next one once it is the nearest's expiry day and past last_entry."""
    exps = sorted(expiries)
    if exps[0] == now.date() and now.strftime("%H:%M") > last_entry and len(exps) > 1:
        return exps[1]
    return exps[0]


def spot_row(rows, name):
    """The index row (AMXIDX), else the NSE cash row of a stock."""
    for r in rows:
        if r.get("name") == name and r.get("instrumenttype") == "AMXIDX":
            return r
    return next((r for r in rows if r.get("name") == name and smart.segment(r.get("exch_seg")) == "NSE"
                 and (r.get("symbol") or "").endswith("-EQ")), None)


def instrument(m, name, scale):
    return {"tradingsymbol": m["symbol"], "token": m["token"], "segment": smart.segment(m["exch_seg"]),
            "underlying": name, "strike": round(float(m["strike"]) / scale, 2), "type": m["symbol"][-2:],
            "expiry": smart.expiry_date(m["expiry"]).isoformat(), "expiry_raw": m["expiry"],
            "lot_size": int(float(m["lotsize"])), "tick_size": float(smart.tick_inr(m, scale)),
            "freeze_qty": int(float(m["freeze_qty"])) if fnum(m.get("freeze_qty")) else None}


# ---------------------------------------------------------------- data

def quotes(client, seg, tokens, per_call):
    """FULL quotes, per_call tokens a call: ({token: (fetched row, received epoch)}, [failures], calls)."""
    got, failed = {}, []
    calls = (len(tokens) + per_call - 1) // per_call
    for i in range(calls):
        batch = tokens[i * per_call:(i + 1) * per_call]
        r = client.call("quote", {"mode": "FULL", "exchangeTokens": {seg: batch}})
        if not r.ok:
            failed.append("quote call %d of %d: %s" % (i + 1, calls, r.why().strip()))
            continue
        for f in (r.data or {}).get("fetched") or []:
            if str(f.get("symbolToken")) in batch:
                got[str(f["symbolToken"])] = (f, r.received)
    return got, failed, calls


def best(levels):
    price = fnum((levels or [{}])[0].get("price"))
    return price if price else None


def confirmed(a, feature):
    """Offline replays assume every feature; live runs use only what the probe confirmed."""
    if a.offline:
        return True
    try:
        smart.require(a.trader, feature)
        return True
    except smart.SmartError:
        return False


def take_snapshot(a, p, cfg, now):
    """(chain dict as CONTRACTS.md shows, info for the report). Writes nothing but the subscription."""
    name = a.underlying.upper()
    opts, fresh = cfg.get("options") or {}, cfg.get("freshness") or {}
    sess, limits = cfg.get("session") or {}, cfg.get("limits") or {}
    cal = smart.read_json(p["calendar"]) or {}
    allowed, why = smart.market_data_allowed(now, cal, stop=sess.get("data_calls_stop", "15:45"))
    if not allowed:
        raise smart.SmartError("no live chain (%s): market data calls run on trading days, 09:00 to %s IST"
                               % (why, sess.get("data_calls_stop", "15:45")))
    if not a.offline:
        smart.require(a.trader, "quote_options")
    today = now.date()
    rows = smart.load_master(p["cache"], today.isoformat(),
                             path=os.path.join(a.offline, "master.json") if a.offline else None)
    scale = smart.strike_scale(rows)[0]
    if not scale:
        raise smart.SmartError("the master's strike scale could not be read: run in-setup master")
    by_exp = contracts(rows, name, today, ("OPTIDX", "OPTSTK"))
    srow = spot_row(rows, name)
    if not by_exp or not srow:
        raise smart.SmartError("%s has no live options or no index or cash row in today's instrument master" % name)
    expiry = choose_expiry(by_exp, now, sess.get("expiry_day_last_entry", "11:00"))
    exp_rows = [r for r in by_exp[expiry] if (r.get("symbol") or "")[-2:] in ("CE", "PE")]
    seg, sseg = smart.segment(exp_rows[0]["exch_seg"]), smart.segment(srow["exch_seg"])
    computed = seg != "NFO"   # optionGreek is documented for NSE only
    rate = smart.risk_free_rate(cfg) if computed else None
    strikes = sorted({round(float(r["strike"]) / scale, 2) for r in exp_rows})
    steps = collections.Counter(round(b - x, 2) for x, b in zip(strikes, strikes[1:]))
    if not steps:
        raise smart.SmartError("%s %s lists one strike: no strike step to build a chain from" % (name, expiry))
    step = steps.most_common(1)[0][0]
    stale_ms = int(float(fresh.get("stale_tick_sec", 3)) * 1000)
    per_call = max(1, min(MAX_TOKENS_PER_QUOTE, int(limits.get("quote_tokens_per_call") or MAX_TOKENS_PER_QUOTE)))
    client, limiter = smart.open_client(a.trader, now, a.offline)
    clock_ms = int(client.clock() * 1000)
    info = {"limiter": limiter, "notes": [], "expiry": expiry, "expiry_raw": exp_rows[0]["expiry"],
            "rows_master": {r["token"]: r for r in exp_rows}, "scale": scale, "rate": rate,
            "rolled": expiry != min(by_exp), "last_entry": sess.get("expiry_day_last_entry", "11:00")}
    partial = []

    # spot: the feed when fresh, else a FULL quote
    tok = srow["token"]
    live = smart.read_latest(p["db"], [tok], clock_ms, stale_ms).get(tok)
    if live and clock_ms - live["exch_ms"] <= stale_ms:
        spot, spot_age = live["ltp"], (clock_ms - live["exch_ms"]) / 1000.0
        info["spot_from"] = "the feed, lag %d ms" % live["lag_ms"]
    else:
        got, failed, _ = quotes(client, sseg, [tok], 1)
        if tok not in got:
            raise smart.SmartError("no %s spot: no fresh feed tick and the quote failed (%s)"
                                   % (name, "; ".join(failed) or "token not fetched"))
        f, received = got[tok]
        spot, ft = fnum(f.get("ltp")), feed_ms(f.get("exchFeedTime"))
        spot_age = received - ft / 1000.0 if ft else None
        info["spot_from"] = "the FULL quote (no fresh feed tick)"
    if not spot:
        raise smart.SmartError("no %s spot price in the reply" % name)
    spot_stale = spot_age is None or spot_age * 1000 > stale_ms
    atm = round(math.floor(spot / step + 0.5) * step, 2)
    n = int(opts.get("strikes_each_side", 15))
    lo, hi = atm - n * step - 1e-6, atm + n * step + 1e-6
    chosen = sorted((r for r in exp_rows if lo <= float(r["strike"]) / scale <= hi),
                    key=lambda r: (float(r["strike"]), r["symbol"][-2:]))
    tokens = [r["token"] for r in chosen]
    smart.subscribe(a.trader, "in-chain:" + name, [{"segment": sseg, "token": tok, "mode": "quote"}]
                    + [{"segment": seg, "token": t, "mode": "snapquote"} for t in tokens])
    info["subscribed"] = len(tokens) + 1

    # option rows: FULL quotes, overlaid with fresher feed ticks
    got, failed, info["calls"] = quotes(client, seg, tokens, per_call)
    partial += failed
    ticks = smart.read_latest(p["db"], tokens, clock_ms, stale_ms)
    out, overlaid = [], 0
    for r in chosen:
        t = r["token"]
        if t not in got:
            continue
        f, received = got[t]
        ft = feed_ms(f.get("exchFeedTime"))
        ltp, vol, oi = fnum(f.get("ltp")), f.get("tradeVolume"), f.get("opnInterest")
        age = received - ft / 1000.0 if ft else None
        tk = ticks.get(t)
        if tk and clock_ms - tk["exch_ms"] <= stale_ms and (ft is None or tk["exch_ms"] > ft):
            ltp, age, overlaid = tk["ltp"], (clock_ms - tk["exch_ms"]) / 1000.0, overlaid + 1
            vol = tk["cum_volume"] if tk["cum_volume"] is not None else vol
            oi = tk["oi"] if tk["oi"] is not None else oi
        tick = smart.tick_inr(r, scale)
        px = lambda v: float(smart.round_tick(v, tick)) if v else None  # noqa: E731
        depth = f.get("depth") or {}
        bid, ask = px(best(depth.get("buy"))), px(best(depth.get("sell")))
        out.append({"strike": round(float(r["strike"]) / scale, 2), "type": r["symbol"][-2:], "token": t,
                    "tradingsymbol": r["symbol"], "ltp": px(ltp), "bid": bid, "ask": ask,
                    "spread_pct": round((ask - bid) / ((ask + bid) / 2) * 100, 2) if bid and ask else None,
                    "volume": int(vol) if vol is not None else None, "oi": int(oi) if oi is not None else None,
                    "oi_change": None, "iv": None, "delta": None, "gamma": None, "theta": None, "vega": None,
                    "age_sec": round(age, 1) if age is not None else None,
                    "stale": age is None or age * 1000 > stale_ms})
    if not out:
        raise smart.SmartError("no %s option quotes came back (%s)" % (name, "; ".join(failed) or "none fetched"))
    if len(out) < len(chosen):
        partial.append("%d of %d rows quoted" % (len(out), len(chosen)))
    info["overlaid"], info["wanted"], info["each_side"] = overlaid, len(chosen), n

    # Greeks
    if computed:
        years = smart.year_fraction(now, expiry)
        info["years"], unsolved = years, 0
        for row in out:
            iv = smart.implied_vol(row["type"], row["ltp"], spot, row["strike"], years, rate) if row["ltp"] else None
            if iv is None:
                unsolved += 1
                continue
            g = smart.black_scholes(row["type"], spot, row["strike"], years, rate, iv)
            row.update(iv=round(iv * 100, 2), delta=round(g["delta"], 4), gamma=round(g["gamma"], 6),
                       theta=round(g["theta"], 2), vega=round(g["vega"], 2))
        if unsolved:
            info["notes"].append("no IV for %d rows: premium below intrinsic value or no time left" % unsolved)
    elif confirmed(a, "optionGreek"):
        r = client.call("optionGreek", {"name": name, "expirydate": info["expiry_raw"]})
        if r.ok:
            table = {(round(fnum(g.get("strikePrice")) or 0, 2), g.get("optionType")): g for g in r.data or []}
            missing = 0
            for row in out:
                g = table.get((row["strike"], row["type"]))
                if not g:
                    missing += 1
                    continue
                vals = [fnum(g.get(k)) for k in ("impliedVolatility", "delta", "gamma", "theta", "vega")]
                row.update({k: (round(v, nd) if v is not None else None) for k, v, nd in
                            zip(("iv", "delta", "gamma", "theta", "vega"), vals, (2, 4, 6, 2, 2))})
            if missing:
                partial.append("optionGreek had no row for %d of %d" % (missing, len(out)))
        else:
            partial.append("optionGreek failed: " + r.why().strip())
    else:
        partial.append("optionGreek is not confirmed by the probe: no Greeks")

    # analytics, descriptive only
    hist = read_lines(history_path(p, name, today.isoformat()))
    target = now - datetime.timedelta(minutes=OI_LOOKBACK_MIN)
    near = [(abs((smart.parse_ist(s["as_of_ist"]) - target).total_seconds()), s) for s in hist if s.get("as_of_ist")]
    near = [x for x in near if x[0] <= OI_SLACK_MIN * 60]
    then = min(near, key=lambda x: x[0])[1] if near else None
    buildup = None
    if then:
        before = {x.get("token"): x.get("oi") for x in then.get("rows") or []}
        for row in out:
            if row["oi"] is not None and before.get(row["token"]) is not None:
                row["oi_change"] = row["oi"] - before[row["token"]]
        changes = [row["oi_change"] for row in out if row["oi_change"] is not None]
        d_oi, d_px = sum(changes), spot - (then.get("spot") or spot)
        buildup = BUILDUP.get(((d_px > 0) - (d_px < 0), (d_oi > 0) - (d_oi < 0)))
        info["then"], info["d_oi"], info["d_px"] = then, d_oi, d_px
    ce = {r["strike"]: r for r in out if r["type"] == "CE"}
    pe = {r["strike"]: r for r in out if r["type"] == "PE"}
    straddle = round(ce[atm]["ltp"] + pe[atm]["ltp"], 2) if atm in ce and atm in pe and ce[atm]["ltp"] \
        and pe[atm]["ltp"] else None
    iv_now = atm_iv({"atm": atm, "rows": out})
    rank, info["iv_days"] = iv_rank(os.path.join(p["history"], "chain", name), today.isoformat(), iv_now)

    pcr_api = None
    futs = contracts(rows, name, today, ("FUTIDX", "FUTSTK"))
    if not computed and futs and confirmed(a, "putCallRatio"):
        fut = futs[min(futs)][0]["symbol"]   # putCallRatio names each underlying by its near futures contract
        r = client.call("putCallRatio")
        if not r.ok:
            info["notes"].append("putCallRatio failed: " + r.why().strip())
        else:
            hit = next((x for x in r.data or [] if x.get("tradingSymbol") == fut), None)
            pcr_api = {"tradingsymbol": fut, "pcr": fnum(hit.get("pcr"))} if hit else None
            if not hit:
                info["notes"].append("putCallRatio has no row for %s" % fut)

    ages = [x["age_sec"] for x in out if x["age_sec"] is not None] + ([spot_age] if spot_age is not None else [])
    feed_used = info["spot_from"].startswith("the feed") or overlaid
    chain = collections.OrderedDict([
        ("created_by", "in-chain"), ("underlying", name), ("expiry", expiry.isoformat()), ("spot", spot),
        ("atm", atm), ("step", step), ("as_of_ist", now.isoformat()),
        ("age_sec", round(max(ages), 1) if ages else None),
        ("source", "feed + quote" if feed_used else "quote"), ("partial", bool(partial)),
        ("partial_note", "; ".join(partial) or None), ("stale", spot_stale),
        ("greeks_source", "COMPUTED" if computed else "API"), ("rows", out),
        ("pcr_oi", pcr(out, "oi")), ("pcr_volume", pcr(out, "volume")), ("pcr_api", pcr_api),
        ("max_pain", max_pain(out)),
        ("top_oi", {k: [r["strike"] for r in sorted((r for r in out if r["type"] == k and r["oi"]),
                                                     key=lambda r: (-r["oi"], r["strike"]))[:TOP_OI]]
                    for k in ("CE", "PE")}),
        ("straddle", straddle), ("implied_move_pct", round(straddle / spot * 100, 2) if straddle else None),
        ("iv_rank", rank), ("buildup", buildup),
        ("oi_change_since_ist", then["as_of_ist"] if then else None)])
    info["spot_age"] = spot_age
    return chain, info


def pcr(rows, key):
    ce = sum(r[key] or 0 for r in rows if r["type"] == "CE")
    pe = sum(r[key] or 0 for r in rows if r["type"] == "PE")
    return round(pe / ce, 2) if ce else None


def max_pain(rows):
    """The strike where option holders, by open interest, would be paid the least at expiry."""
    if not any(r["oi"] for r in rows):
        return None

    def paid(k):
        return sum((r["oi"] or 0) * max(0.0, (k - r["strike"]) if r["type"] == "CE" else (r["strike"] - k))
                   for r in rows)
    return min(sorted({r["strike"] for r in rows}), key=paid)


def atm_iv(snap):
    ivs = [r["iv"] for r in snap.get("rows") or [] if r.get("strike") == snap.get("atm") and r.get("iv") is not None]
    return round(statistics.mean(ivs), 2) if ivs else None


def iv_rank(folder, today, iv_now):
    """(rank 0 to 100 of today's ATM IV within the last 252 days of this skill's own snapshots, days used).
    None until IV_RANK_MIN_DAYS days exist. A day is its last snapshot."""
    try:
        days = sorted(f for f in os.listdir(folder) if f.endswith(".jsonl") and f[:-6] < today)[-IV_RANK_MAX_DAYS:]
    except FileNotFoundError:
        days = []
    # ponytail: reads each day file whole for its last line; keep a daily summary line if files grow large
    ivs = [v for v in (atm_iv((read_lines(os.path.join(folder, f)) or [{}])[-1]) for f in days) if v is not None]
    if iv_now is None or len(ivs) < IV_RANK_MIN_DAYS:
        return None, len(ivs)
    lo, hi = min(ivs + [iv_now]), max(ivs + [iv_now])
    return (round((iv_now - lo) / (hi - lo) * 100, 1) if hi > lo else None), len(ivs)


def save(p, chain, day):
    """trader/chain/<U>.json (atomic) and one line appended to today's history file."""
    path = os.path.join(p["chain_dir"], chain["underlying"] + ".json")
    smart.write_json(path, chain)
    hist = history_path(p, chain["underlying"], day)
    os.makedirs(os.path.dirname(hist), exist_ok=True)
    with smart.FileLock(hist + ".lock"):
        with open(hist, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(chain, separators=(",", ":")) + "\n")
    return path, hist


# ---------------------------------------------------------------- report

def head(chain, info, what, now, a):
    return "%s %s, %s IST (%s)" % (chain["underlying"], what, smart.stamp(now),
                                   "offline replay" if a.offline else "live, Angel One SmartAPI")


def snapshot_lines(chain, info, cfg):
    stale_sec = float((cfg.get("freshness") or {}).get("stale_tick_sec", 3))
    rows, exp = chain["rows"], info["expiry"]
    out = ["  " + smart.NOT_ADVICE,
           "  %-15s %s %s (%s), %s" % ("Expiry", smart.WEEKDAYS[exp.weekday()], exp.isoformat(), info["expiry_raw"],
                                       "the next: past %s on the nearest's expiry day" % info["last_entry"]
                                       if info["rolled"] else "the nearest"),
           "  %-15s %s from %s, age %s s%s" % ("Spot", f2(chain["spot"]), info["spot_from"], f2(info["spot_age"], 1),
                                               ", STALE" if chain["stale"] else ""),
           "  %-15s ATM %s, step %s, ATM +/- %d strikes: %d of %d rows quoted in %d quote calls, %d from fresher "
           "feed ticks" % ("Strikes", kt(chain["atm"]), kt(chain["step"]), info["each_side"], len(rows),
                           info["wanted"], info["calls"], info["overlaid"])]
    ages = [r["age_sec"] for r in rows if r["age_sec"] is not None]
    stale = ["%s %s" % (kt(r["strike"]), r["type"]) for r in rows if r["stale"]]
    out.append("  %-15s rows %s s median, %s s worst; stale (no update for over %g s): %s" % (
        "Freshness", f2(statistics.median(ages), 1) if ages else "-", f2(max(ages), 1) if ages else "-", stale_sec,
        ", ".join(stale) or "none"))
    if chain["partial"]:
        out.append("  %-15s %s" % ("PARTIAL", chain["partial_note"]))
    if chain["greeks_source"] == "API":
        out.append("  %-15s API (optionGreek)" % "Greeks")
    else:
        out.append("  %-15s COMPUTED: Black-Scholes, IV solved from each LTP, rate %.2f%% from config, %.2f days "
                   "to expiry (no optionGreek for BSE)" % ("Greeks", info["rate"] * 100, info["years"] * 365))
    api = chain["pcr_api"]
    out.append("  %-15s OI %s, volume %s on these strikes; %s" % (
        "PCR", f2(chain["pcr_oi"]), f2(chain["pcr_volume"]),
        "putCallRatio %s for %s (all strikes)" % (f2(api["pcr"]), api["tradingsymbol"]) if api
        else "no putCallRatio cross-check (NSE only)" if chain["greeks_source"] == "COMPUTED"
        else "no putCallRatio cross-check"))
    out.append("  %-15s %s (these strikes only)" % ("Max pain", kt(chain["max_pain"]) if chain["max_pain"] else "-"))
    out.append("  %-15s CE %s; PE %s" % ("Highest OI", ", ".join(kt(k) for k in chain["top_oi"]["CE"]) or "-",
                                         ", ".join(kt(k) for k in chain["top_oi"]["PE"]) or "-"))
    if chain["straddle"]:
        ce = next(r for r in rows if r["strike"] == chain["atm"] and r["type"] == "CE")
        out.append("  %-15s CE %s + PE %s = %s at %s, implied move %s%% by expiry" % (
            "Straddle", f2(ce["ltp"]), f2(chain["straddle"] - ce["ltp"]), f2(chain["straddle"]), kt(chain["atm"]),
            f2(chain["implied_move_pct"])))
    else:
        out.append("  %-15s - (no ATM CE and PE pair)" % "Straddle")
    then = info.get("then")
    if then:
        since = smart.parse_ist(then["as_of_ist"]).strftime("%H:%M")
        side = {k: sum(r["oi_change"] for r in rows if r["type"] == k and r["oi_change"] is not None)
                for k in ("CE", "PE")}
        out.append("  %-15s since the %s snapshot: CE %s, PE %s" % ("OI change", since, "{:+,}".format(side["CE"]),
                                                                     "{:+,}".format(side["PE"])))
        out.append("  %-15s %s (spot %+.2f, chain OI %s since %s): a label for what happened, not a forecast" % (
            "Buildup", chain["buildup"] or "none", info["d_px"], "{:+,}".format(info["d_oi"]), since))
    else:
        out.append("  %-15s none: no snapshot from %d to %d minutes ago in today's history" % (
            "OI change", OI_LOOKBACK_MIN - OI_SLACK_MIN, OI_LOOKBACK_MIN + OI_SLACK_MIN))
    out.append("  %-15s %s" % ("IV rank", "%s (ATM IV %s against %d days of this skill's own history)" % (
        f2(chain["iv_rank"], 1), f2(atm_iv(chain)), info["iv_days"]) if chain["iv_rank"] is not None
        else "not yet: %d of %d days of this skill's own history" % (info["iv_days"], IV_RANK_MIN_DAYS)))
    for note in info["notes"]:
        out.append("  %-15s %s" % ("Note", note))
    out.append("  %9s %8s %6s %6s %8s %5s | %-7s | %8s %5s %6s %6s %8s %9s" % (
        "CE OI", "chg", "IV", "delta", "LTP", "spr%", "strike", "LTP", "spr%", "delta", "IV", "chg", "PE OI"))
    by = {(r["strike"], r["type"]): r for r in rows}
    for k in sorted({r["strike"] for r in rows}):
        c, q = by.get((k, "CE"), {}), by.get((k, "PE"), {})
        n = lambda r, key: "-" if r.get(key) is None else "{:,}".format(r[key])  # noqa: E731
        ch = lambda r: "-" if r.get("oi_change") is None else "{:+,}".format(r["oi_change"])  # noqa: E731
        out.append("  %9s %8s %6s %6s %8s %5s | %-7s | %8s %5s %6s %6s %8s %9s" % (
            n(c, "oi"), ch(c), f2(c.get("iv"), 1), f2(c.get("delta")), f2(c.get("ltp")), f2(c.get("spread_pct")),
            kt(k) + (" *" if k == chain["atm"] else ""), f2(q.get("ltp")), f2(q.get("spread_pct")),
            f2(q.get("delta")), f2(q.get("iv"), 1), ch(q), n(q, "oi")))
    out.append("  %-15s %d tokens asked of the live feed (trader/state/subscriptions.json, owner in-chain:%s)"
               % ("Feed", info["subscribed"], chain["underlying"]))
    return out


# ---------------------------------------------------------------- commands

def cmd_snapshot(a):
    p = smart.trader_paths(a.trader)
    cfg = smart.load_config(a.trader)
    now = smart.now_ist(a.now)
    chain, info = take_snapshot(a, p, cfg, now)
    path, hist = save(p, chain, now.date().isoformat())
    out = [head(chain, info, "option chain", now, a)] + snapshot_lines(chain, info, cfg)
    out.append("Wrote %s; appended %s" % (show(path), show(hist)))
    out.append(smart.usage_text(info["limiter"]))
    print("\n".join(out))
    return 0


def direction_for(a, p, name, today):
    if a.direction:
        return a.direction, "--direction"
    e = smart.read_json(p["entry"]) or {}
    if e.get("status") == "triggered" and e.get("date") == today and (e.get("symbol") or "").upper() == name \
            and e.get("direction") in ("bullish", "bearish"):
        return e["direction"], "entry.json (bar closed %s)" % e.get("bar_close_ist", "?")
    raise smart.SmartError("no direction for %s: trader/entry.json has no trigger for it today; pass --direction "
                           "bullish or bearish" % name)


def cmd_pick(a):
    p = smart.trader_paths(a.trader)
    cfg = smart.load_config(a.trader)
    now = smart.now_ist(a.now)
    name = a.underlying.upper()
    direction, said = direction_for(a, p, name, now.date().isoformat())
    capital = smart.require_account(cfg)
    o, acct = cfg.get("options") or {}, cfg.get("account") or {}
    dmin, dmax = float(o.get("delta_min", 0.35)), float(o.get("delta_max", 0.55))
    max_spread, min_vol, min_oi = float(o.get("max_spread_pct", 3.0)), int(o.get("min_volume", 1000)), \
        int(o.get("min_oi", 5000))
    cap_pct = Decimal(str(acct.get("max_capital_per_trade_pct", 30)))
    budget = (capital * cap_pct / 100).quantize(smart.PAISA)
    stale_sec = float((cfg.get("freshness") or {}).get("stale_tick_sec", 3))

    chain, info = take_snapshot(a, p, cfg, now)
    path, hist = save(p, chain, now.date().isoformat())
    if chain["stale"]:
        raise smart.SmartError("the %s spot is stale (%s s old): no pick on stale data; snapshot saved to %s"
                               % (name, f2(info["spot_age"], 1), show(path)))
    kind, mid = ("CE" if direction == "bullish" else "PE"), (dmin + dmax) / 2
    fails = collections.OrderedDict((k, []) for k in ("stale", "delta", "spread", "volume", "oi", "cost"))
    passed = []
    for r in (r for r in chain["rows"] if r["type"] == kind):
        lot = int(float(info["rows_master"][r["token"]]["lotsize"]))
        cost = (Decimal(str(r["ltp"])) * lot).quantize(smart.PAISA) if r["ltp"] else None
        s = kt(r["strike"])
        checks = [("stale", r["stale"], "%s (%s s)" % (s, f2(r["age_sec"], 1))),
                  ("delta", r["delta"] is None or not dmin <= abs(r["delta"]) <= dmax, "%s (%s)" % (s, f2(r["delta"]))),
                  ("spread", r["spread_pct"] is None or r["spread_pct"] > max_spread,
                   "%s (%s)" % (s, "%s%%" % f2(r["spread_pct"]) if r["spread_pct"] is not None else "no bid or ask")),
                  ("volume", (r["volume"] or 0) < min_vol, "%s (%s)" % (s, "{:,}".format(r["volume"] or 0))),
                  ("oi", (r["oi"] or 0) < min_oi, "%s (%s)" % (s, "{:,}".format(r["oi"] or 0))),
                  ("cost", cost is None or cost > budget, "%s (Rs %s)" % (s, "{:,.2f}".format(cost) if cost else "-"))]
        bad = [(k, text) for k, failed, text in checks if failed]
        for k, text in bad:
            fails[k].append(text)
        if not bad:
            passed.append((abs(abs(r["delta"]) - mid), r["spread_pct"], r["strike"], r, cost))
    passed.sort(key=lambda x: x[:3])
    rule = ("%s, |delta| %g to %g (closest to %g wins), spread <= %g%%, volume >= %d, OI >= %d, one lot <= %s%% of "
            "capital (Rs %s), not stale" % (kind, dmin, dmax, mid, max_spread, min_vol, min_oi, cap_pct,
                                             "{:,.2f}".format(budget)))
    labels = {"stale": "stale (no update for over %g s)" % stale_sec, "delta": "delta outside %g to %g" % (dmin, dmax),
              "spread": "spread over %g%%" % max_spread, "volume": "volume under {:,}".format(min_vol),
              "oi": "OI under {:,}".format(min_oi), "cost": "one lot over Rs {:,.2f}".format(budget)}
    exp = info["expiry"]
    out = ["%s pick, %s IST (%s): %s from %s, so %s strikes" % (
        name, smart.stamp(now), "offline replay" if a.offline else "live, Angel One SmartAPI", direction, said, kind),
        "  " + smart.NOT_ADVICE,
        "  %-15s expiry %s %s, spot %s, Greeks %s, as of %s%s" % (
            "Chain", smart.WEEKDAYS[exp.weekday()], exp.isoformat(), f2(chain["spot"]), chain["greeks_source"],
            now.strftime("%H:%M"), ", PARTIAL: " + chain["partial_note"] if chain["partial"] else ""),
        "  %-15s %s" % ("Rule", rule)]

    def line(x):
        r, cost = x[3], x[4]
        return "%s (token %s), delta %s, premium %s (bid %s, ask %s, spread %s%%), volume %s, OI %s, one lot %d x %s " \
               "= Rs %s" % (r["tradingsymbol"], r["token"], f2(r["delta"]), f2(r["ltp"]), f2(r["bid"]), f2(r["ask"]),
                            f2(r["spread_pct"]), "{:,}".format(r["volume"]), "{:,}".format(r["oi"]),
                            int(float(info["rows_master"][r["token"]]["lotsize"])), f2(r["ltp"]),
                            "{:,.2f}".format(cost))
    if passed:
        out.append("  %-15s %s" % ("Picked", line(passed[0])))
        out.append("  %-15s %s" % ("Next best", line(passed[1]) if len(passed) > 1 else "none: only one strike passed"))
    failed = [(k, v) for k, v in fails.items() if v]
    for i, (k, v) in enumerate(failed):
        out.append("  %-15s %s: %s" % ("Failed" if i == 0 else "", labels[k], ", ".join(v)))
    if not failed:
        out.append("  %-15s none" % "Failed")
    if not passed:
        out.append("No %s %s passed every rule: no pick, nothing written to %s. Wrote %s; appended %s" % (
            name, kind, show(p["pick"]), show(path), show(hist)))
        out.append(smart.usage_text(info["limiter"]))
        print("\n".join(out))
        print("in-chain: no %s %s passed the pick rules; nothing written" % (name, kind), file=sys.stderr)
        return 1
    win, nxt = passed[0], (passed[1] if len(passed) > 1 else None)
    pick = collections.OrderedDict([
        ("created_by", "in-chain"), ("underlying", name), ("direction", direction), ("as_of_ist", chain["as_of_ist"]),
        ("instrument", instrument(info["rows_master"][win[3]["token"]], name, info["scale"])),
        ("premium", win[3]["ltp"]), ("bid", win[3]["bid"]), ("ask", win[3]["ask"]),
        ("spread_pct", win[3]["spread_pct"]), ("volume", win[3]["volume"]), ("oi", win[3]["oi"]),   # in-risk reads them
        ("delta", win[3]["delta"]),
        ("greeks_source", chain["greeks_source"]), ("one_lot_cost_inr", float(win[4])), ("rule", rule),
        ("next_best", {"tradingsymbol": nxt[3]["tradingsymbol"], "token": nxt[3]["token"], "strike": nxt[3]["strike"],
                       "delta": nxt[3]["delta"], "premium": nxt[3]["ltp"], "one_lot_cost_inr": float(nxt[4])}
         if nxt else None),
        ("source", "in-chain snapshot (%s, Greeks %s)" % (chain["source"], chain["greeks_source"]))])
    smart.write_json(p["pick"], pick)
    out.append("Wrote %s and %s; appended %s" % (show(p["pick"]), show(path), show(hist)))
    out.append(smart.usage_text(info["limiter"]))
    print("\n".join(out))
    return 0


def cmd_history(a):
    p = smart.trader_paths(a.trader)
    name = a.underlying.upper()
    day = a.date or smart.now_ist(a.now).date().isoformat()
    path = history_path(p, name, day)
    snaps = read_lines(path)
    if not snaps:
        raise smart.SmartError("no %s chain history for %s (%s)" % (name, day, show(path)))
    shown = snaps[-a.last:]
    out = ["%s chain history, %s: %d snapshots in %s, the last %d shown" % (name, day, len(snaps), show(path),
                                                                           len(shown)),
           "  " + smart.NOT_ADVICE,
           "  %-5s  %-10s  %9s  %-7s  %6s  %7s  %-8s  %8s  %6s  %-14s  %s" % (
               "time", "expiry", "spot", "ATM", "PCR OI", "PCR vol", "max pain", "straddle", "move%", "buildup",
               "flags")]
    for s in shown:
        flags = [f for f, on in (("partial", s.get("partial")), ("stale", s.get("stale")),
                                 ("computed", s.get("greeks_source") == "COMPUTED")) if on]
        out.append("  %-5s  %-10s  %9s  %-7s  %6s  %7s  %-8s  %8s  %6s  %-14s  %s" % (
            smart.parse_ist(s["as_of_ist"]).strftime("%H:%M"), s.get("expiry", "-"), f2(s.get("spot")),
            kt(s["atm"]) if s.get("atm") else "-", f2(s.get("pcr_oi")), f2(s.get("pcr_volume")),
            kt(s["max_pain"]) if s.get("max_pain") else "-", f2(s.get("straddle")), f2(s.get("implied_move_pct")),
            s.get("buildup") or "-", ", ".join(flags) or "-"))
    print("\n".join(out))
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("underlying", help="NIFTY, SENSEX, BANKNIFTY or an F&O stock name")
    common.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    common.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-25 10:20"')
    live = argparse.ArgumentParser(add_help=False)
    live.add_argument("--offline", metavar="DIR", help="replay saved replies and DIR/master.json")
    ap = argparse.ArgumentParser(description="in-chain: option chain, analytics and strike pick (read-only, no orders).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("snapshot", parents=[common, live], help="the chain now -> trader/chain/<U>.json + history")
    s = sub.add_parser("pick", parents=[common, live], help="a strike by rule for a direction -> trader/pick.json")
    s.add_argument("--direction", choices=("bullish", "bearish"), help="instead of trader/entry.json")
    s = sub.add_parser("history", parents=[common], help="today's snapshots from the history file")
    s.add_argument("--date", help="YYYY-MM-DD instead of today")
    s.add_argument("--last", type=int, default=10, help="how many snapshots to show (default 10)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"snapshot": cmd_snapshot, "pick": cmd_pick, "history": cmd_history}[a.cmd](a)
    except smart.SmartError as e:
        print("in-chain: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
