"""The two strategies, as pure functions over CLOSED 5-minute index bars.

A bar is {"start_ist": "HH:MM", "open", "high", "low", "close", ...}. The bar in
progress never reaches here (smart.read_bars and smart.candles drop it), and
clean_bars() drops malformed ones. Each function returns every signal of the
day so far, deterministically: re-running over the same bars gives the same
signals with the same ids, so a restart cannot create duplicates.

ORB  (Opening Range Breakout)
  Range   high/low of the 09:15-09:30 bars (three 5-minute bars).
  Entry   the first bar after the range that CLOSES above the high (bullish,
          buy CE) or below the low (bearish, buy PE). One signal per direction
          per day; a close equal to an edge is not beyond it.
  Stop    or-mid: the range midpoint (or-opposite: the other edge).
  Target  entry +/- target_r x risk.

EMA_PULLBACK  (trend continuation, complements ORB)
  Trend   EMA(fast) above EMA(slow) and EMA(slow) rising over 3 bars
          (mirror for bearish). EMAs are seeded with the previous day's bars.
  Pullback one of the previous `pullback_bars` bars dipped to the fast EMA
          (low <= EMA fast) while every close held above the slow EMA.
  Entry   the bar that resumes: close above the previous bar's high and above
          the fast EMA (mirror for bearish), from `start_after` on.
  Stop    the pullback swing low (high for bearish) over those bars.
  Target  entry +/- target_r x risk. Risk must be min_risk_pct..max_risk_pct of
          price, else the setup is not taken. After a signal, the same
          direction waits `cooldown_bars` bars.
ORB fires once per direction around the open; EMA_PULLBACK keeps finding
entries in an established trend later in the day, where ORB is silent.

EMA_ORB  (hybrid: EMA_PULLBACK confirmed by an opening-range breakout; a third strategy,
          ORB and EMA_PULLBACK above are unchanged)
  Range   high/low of the 09:15 to range_end bars. Width % = (high - low) / reference x 100,
          reference = the OPEN of the 09:15 bar (the day's opening price, known at 09:15 and
          the same in the backtest and live). Days narrower than min_or_pct are skipped
          (0 turns the filter off: "EMA + ORB"; 0.35, the default: "EMA + ORB + minimum range").
  Setup   the existing EMA_PULLBACK setup (ema_pullback_signals, same trend, pullback and resume
          rules, same fast/slow/pullback/cooldown/start_after settings), found with its risk band
          opened so the hybrid's own stop method decides the risk.
  Confirm bullish only after a 5-minute bar has CLOSED above the range high, and the setup bar
          itself closes above it (never inside the range); bearish mirrored below the low.
  Stop    stop_method: "ema" (the EMA setup's own stop: the pullback swing over pullback_bars),
          "atr" (atr_mult x ATR14 of the bars up to the setup bar), "swing" (the lowest low /
          highest high of the last swing_bars bars). The risk must be min_risk_pct..max_risk_pct
          of price, else the setup is skipped: a wider stop never makes a trade valid.
  Target  entry +/- target_r x risk.
  Day     at most max_trades_per_day (2); with stop_after_first_sl, a first trade that hits its
          stop ends this strategy's day (paper.gate live, the replay in app/timemachine.py).
ema_orb_evaluate() returns the signals AND every setup's accept/reject reason, for the UI.

OI_PCR  (Intraday OI Shift + PCR confirmation; a fourth strategy, needs the option chain, not only bars)
  Data    one option-chain record per closed bar (oi_record(): open interest of every strike +/-
          strikes_each_side around the money, the PCR = put OI / call OI of those strikes, spot,
          expiry), taken by the engine right after the bar closes (in-chain's take_snapshot).
  Walls   from the record of the bar closing at range_end (the 09:15-09:30 battleground): the call
          wall = the strike with the most call OI (resistance), the put wall = the most put OI.
  OI shift  % change of a strike's OI since that battleground record (intraday change in OI).
  Bullish the bar CLOSES at or above call wall x (1 - near_pct%), call OI at the wall is down at
          least min_unwind_pct % (call writers covering), put OI at the wall or one strike below is
          up at least min_surge_pct % (put writers stepping in), and the PCR is above pcr_bull and
          higher than at the previous record ("keeps climbing").
  Bearish mirrored at the put wall: put OI unwinds, call OI surges at the wall or one strike above,
          PCR under pcr_bear and falling.
  No trade a wall reached while its writers keep ADDING (call OI up at the call wall): they defend
          it, the range-bound trap; recorded as a rejected setup with that reason.
  Stop    the lowest low (bullish) / highest high of the last swing_bars bars, within
          min_risk_pct..max_risk_pct of price. Target entry +/- target_r x risk.
  Day     one signal per direction; none once the chain rolls to another expiry than the battleground's
          (expiry day after session.expiry_day_last_entry): its OI is not comparable.
  It cannot be replayed (Angel One keeps no per-strike OI history): live paper trades are its record.
"""

from decimal import Decimal

TICK = Decimal("0.05")
BAR_MIN = 5

STRATEGIES = {
    "ORB": {"name": "Opening Range Breakout",
            "defaults": {"enabled": True, "stop_rule": "or-mid", "target_r": 2.0, "max_trades_per_day": 2}},
    "EMA_PULLBACK": {"name": "EMA Trend Pullback",
                     "defaults": {"enabled": True, "fast": 9, "slow": 21, "pullback_bars": 3, "cooldown_bars": 6,
                                  "start_after": "09:45", "min_risk_pct": 0.05, "max_risk_pct": 0.6,
                                  "target_r": 2.0, "max_trades_per_day": 2}},
    "EMA_ORB": {"name": "EMA Pullback + ORB Hybrid",
                "defaults": {"enabled": False, "fast": 9, "slow": 21, "pullback_bars": 3, "cooldown_bars": 6,
                             "start_after": "09:45", "min_risk_pct": 0.05, "max_risk_pct": 0.6, "min_or_pct": 0.35,
                             "stop_method": "ema", "atr_mult": 1.5, "swing_bars": 10, "target_r": 2.0,
                             "max_trades_per_day": 2, "stop_after_first_sl": True}},
    "OI_PCR": {"name": "OI Shift + PCR Confirmation",
               "defaults": {"enabled": False, "symbols": "NIFTY", "strikes_each_side": 20, "start_after": "09:30",
                            "near_pct": 0.1, "min_unwind_pct": 1.0, "min_surge_pct": 5.0, "pcr_bull": 1.1,
                            "pcr_bear": 0.85, "swing_bars": 3, "min_risk_pct": 0.05, "max_risk_pct": 0.6,
                            "target_r": 2.0, "max_trades_per_day": 2}},
    # standalone, app/lorentzian.py: the original indicator's settings, then the added volume rules, then the
    # paper trade's own stop (the indicator has none) and limits
    "LORENTZIAN": {"name": "Machine Learning: Lorentzian Classification",
                   "defaults": {"enabled": False, "symbols": "NIFTY", "source": "hlc3", "neighbors_count": 8,
                                "max_bars_back": 2000, "feature_count": 5, "color_compression": 1,
                                "f1": "RSI", "f1_a": 14, "f1_b": 1, "f2": "WT", "f2_a": 10, "f2_b": 11,
                                "f3": "CCI", "f3_a": 20, "f3_b": 1, "f4": "ADX", "f4_a": 20, "f4_b": 2,
                                "f5": "RSI", "f5_a": 9, "f5_b": 1,
                                "use_volatility_filter": True, "use_regime_filter": True, "regime_threshold": -0.1,
                                "use_adx_filter": False, "adx_threshold": 20, "use_ema_filter": False,
                                "ema_period": 200, "use_sma_filter": False, "sma_period": 200,
                                "use_kernel_filter": True, "show_kernel_estimate": True, "kernel_smoothing": False,
                                "kernel_lookback": 8, "kernel_relative_weight": 8.0, "kernel_regression_level": 25,
                                "kernel_lag": 2, "use_dynamic_exits": False, "exit_on_flip": True,
                                "use_volume_filter": True, "volume_ma_period": 20, "min_rvol": 1.2,
                                "use_rvol_feature": False, "rvol_feature_period": 20,
                                "swing_bars": 4, "min_risk_pct": 0.05, "max_risk_pct": 0.6, "target_r": 3.0,
                                "max_trades_per_day": 3}},
}
STOP_METHODS = ("ema", "atr", "swing")
INDICES = ("NIFTY", "SENSEX", "BANKNIFTY")
ZERO_OK = {"min_or_pct", "near_pct", "min_unwind_pct", "min_surge_pct"}   # 0 is valid: it turns the filter off


def strategy_cfg(cfg, key):
    return {**STRATEGIES[key]["defaults"], **((cfg.get("strategies") or {}).get(key) or {})}


def add_min(hm, minutes):
    t = int(hm[:2]) * 60 + int(hm[3:5]) + minutes
    return "%02d:%02d" % (t // 60, t % 60)


def _round(x, mode):
    """To the 0.05 index tick: 'down', 'up' or nearest."""
    q = Decimal(str(x)) / TICK
    q = q.to_integral_value(rounding={"down": "ROUND_FLOOR", "up": "ROUND_CEILING"}.get(mode, "ROUND_HALF_UP"))
    return float(q * TICK)


def clean_bars(bars):
    """Sorted, unique, well-formed bars only: positive prices, low <= open/close <= high."""
    out, seen = [], set()
    for b in sorted(bars or [], key=lambda b: b.get("start_ist") or ""):
        try:
            o, h, l, c = (float(b[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            continue
        if b["start_ist"] in seen or min(o, h, l, c) <= 0 or not (l <= min(o, c) and max(o, c) <= h):
            continue
        seen.add(b["start_ist"])
        out.append(dict(b, open=o, high=h, low=l, close=c))
    return out


def _signal(strategy, symbol, date, bar, side, stop, target_r, note):
    entry = bar["close"]
    up = side == "bullish"
    stop = _round(stop, "down" if up else "up")
    risk = entry - stop if up else stop - entry
    if risk <= 0:
        return None
    target = _round(entry + target_r * risk if up else entry - target_r * risk, "nearest")
    return {"id": "%s:%s:%s:%s:%s" % (date, strategy, symbol, bar["start_ist"], side),
            "strategy": strategy, "symbol": symbol, "date": date, "direction": side,
            "bar_start_ist": bar["start_ist"], "bar_close_ist": add_min(bar["start_ist"], BAR_MIN),
            "entry_underlying": entry, "stop_underlying": stop, "target_underlying": target,
            "risk_points": round(risk, 2), "note": note}


def orb_signals(symbol, date, bars, s, range_end="09:30"):
    inside = [b for b in bars if "09:15" <= b["start_ist"] < range_end]
    need = (int(range_end[:2]) * 60 + int(range_end[3:]) - 555) // BAR_MIN   # 555 = 09:15 in minutes
    if len(inside) < need:
        return []
    hi, lo = max(b["high"] for b in inside), min(b["low"] for b in inside)
    out, done = [], set()
    for b in bars:
        if b["start_ist"] < range_end:
            continue
        side = "bullish" if b["close"] > hi else "bearish" if b["close"] < lo else None
        if not side or side in done:
            continue
        done.add(side)
        if s.get("stop_rule") == "or-opposite":
            stop = lo if side == "bullish" else hi
        else:
            stop = (hi + lo) / 2
        sig = _signal("ORB", symbol, date, b, side, stop, float(s["target_r"]),
                      "closed %s the opening range %s-%s (%s)" % ("above" if side == "bullish" else "below",
                                                                  lo, hi, s.get("stop_rule", "or-mid")))
        if sig:
            sig["opening_range"] = {"high": hi, "low": lo}
            out.append(sig)
    return out


def ema(values, n):
    k, out, e = 2.0 / (n + 1), [], None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def ema_pullback_signals(symbol, date, prev_bars, bars, s):
    allb = list(prev_bars) + list(bars)
    first_today = len(prev_bars)
    fast, slow = int(s["fast"]), int(s["slow"])
    pb, cool = int(s["pullback_bars"]), int(s["cooldown_bars"])
    if len(allb) < slow + pb + 3:
        return []
    closes = [b["close"] for b in allb]
    f, sl = ema(closes, fast), ema(closes, slow)
    out, last = [], {}
    for i in range(max(first_today, slow + pb), len(allb)):
        b = allb[i]
        if b["start_ist"] < s["start_after"]:
            continue
        win = range(i - pb, i)
        for side in ("bullish", "bearish"):
            up = side == "bullish"
            if i - last.get(side, -10 ** 9) <= cool:
                continue
            trend = (f[i] > sl[i] and sl[i] > sl[i - 3]) if up else (f[i] < sl[i] and sl[i] < sl[i - 3])
            if not trend:
                continue
            touched = any(allb[j]["low"] <= f[j] if up else allb[j]["high"] >= f[j] for j in win)
            held = all(allb[j]["close"] > sl[j] if up else allb[j]["close"] < sl[j] for j in win)
            resumed = (b["close"] > allb[i - 1]["high"] and b["close"] > f[i]) if up else \
                (b["close"] < allb[i - 1]["low"] and b["close"] < f[i])
            if not (touched and held and resumed):
                continue
            span = [allb[j] for j in range(i - pb, i + 1)]
            stop = min(x["low"] for x in span) if up else max(x["high"] for x in span)
            risk_pct = abs(b["close"] - stop) / b["close"] * 100
            if not float(s["min_risk_pct"]) <= risk_pct <= float(s["max_risk_pct"]):
                continue
            sig = _signal("EMA_PULLBACK", symbol, date, b, side, stop, float(s["target_r"]),
                          "EMA%d %.2f %s EMA%d %.2f; pullback to EMA%d then resumed" % (
                              fast, f[i], ">" if up else "<", slow, sl[i], fast))
            if sig:
                sig["ema"] = {"fast": round(f[i], 2), "slow": round(sl[i], 2)}
                out.append(sig)
                last[side] = i
    return out


def opening_range(bars, range_end="09:30"):
    """{"high", "low", "ref", "pct", "complete"} of the 09:15..range_end bars; None before the first bar.
    ref is the 09:15 bar's open; pct = (high - low) / ref x 100."""
    inside = [b for b in bars if "09:15" <= b["start_ist"] < range_end]
    if not inside:
        return None
    need = (int(range_end[:2]) * 60 + int(range_end[3:]) - 555) // BAR_MIN
    hi, lo = max(b["high"] for b in inside), min(b["low"] for b in inside)
    ref = inside[0]["open"]
    return {"high": hi, "low": lo, "ref": ref, "pct": round((hi - lo) / ref * 100, 4), "complete": len(inside) >= need,
            "start": "09:15", "end": range_end}


def atr14(bars):
    """The mean of the last 14 true ranges (fewer when there are fewer bars)."""
    trs, prev = [], None
    for b in bars:
        trs.append(b["high"] - b["low"] if prev is None else
                   max(b["high"] - b["low"], abs(b["high"] - prev), abs(b["low"] - prev)))
        prev = b["close"]
    last = trs[-14:]
    return sum(last) / len(last) if last else 0.0


def ema_orb_evaluate(symbol, date, prev_bars, bars, s, range_end="09:30"):
    """EMA_ORB's signals for the day so far and the decision on every EMA setup:
    (signals, {"opening_range", "breakout", "setups": [{bar, direction, accepted, reason}]})."""
    bars = list(bars)
    rng = opening_range(bars, range_end)
    state = {"opening_range": rng, "breakout": {"bullish": None, "bearish": None}, "setups": []}
    if rng is None:
        return [], state
    for b in bars:                                   # the first CLOSE beyond each edge after the range
        if b["start_ist"] < range_end:
            continue
        if state["breakout"]["bullish"] is None and b["close"] > rng["high"]:
            state["breakout"]["bullish"] = b["start_ist"]
        if state["breakout"]["bearish"] is None and b["close"] < rng["low"]:
            state["breakout"]["bearish"] = b["start_ist"]
    raw = ema_pullback_signals(symbol, date, prev_bars, bars, dict(s, min_risk_pct=0.0, max_risk_pct=1e9))
    allb = list(prev_bars) + bars
    index = {b["start_ist"]: len(prev_bars) + k for k, b in enumerate(bars)}
    method = s.get("stop_method", "ema")
    out = []
    for r in raw:
        side, up, entry = r["direction"], r["direction"] == "bullish", r["entry_underlying"]
        edge = rng["high"] if up else rng["low"]
        seen = state["breakout"][side]

        def decide(ok, reason):
            state["setups"].append({"bar_start_ist": r["bar_start_ist"], "bar_close_ist": r["bar_close_ist"],
                                    "direction": side, "entry": entry, "accepted": ok, "reason": reason})
        if not rng["complete"]:
            decide(False, "opening range %s-%s incomplete" % (rng["start"], rng["end"]))
            continue
        if rng["pct"] < float(s.get("min_or_pct", 0)):
            decide(False, "opening range %.2f%% under min_or_pct %.2f%%: sideways day" % (rng["pct"],
                                                                                    float(s["min_or_pct"])))
            continue
        if seen is None or seen > r["bar_start_ist"]:
            decide(False, "no close %s the range %s %.2f yet" % ("above" if up else "below",
                                                                  "high" if up else "low", edge))
            continue
        if not (entry > edge if up else entry < edge):
            decide(False, "setup bar closed inside the opening range (%.2f vs %s %.2f)" % (
                entry, "high" if up else "low", edge))
            continue
        i = index[r["bar_start_ist"]]
        if method == "atr":
            stop = entry - float(s.get("atr_mult", 1.5)) * atr14(bars[:i - len(prev_bars) + 1]) * (1 if up else -1)
        elif method == "swing":
            span = allb[max(0, i - int(s.get("swing_bars", 10)) + 1):i + 1]
            stop = min(x["low"] for x in span) if up else max(x["high"] for x in span)
        else:
            stop = r["stop_underlying"]
        risk_pct = abs(entry - stop) / entry * 100
        if not float(s["min_risk_pct"]) <= risk_pct <= float(s["max_risk_pct"]):
            decide(False, "%s stop risk %.2f%% outside %.2f%%..%.2f%%" % (method, risk_pct, float(s["min_risk_pct"]),
                                                                       float(s["max_risk_pct"])))
            continue
        bar = allb[i]
        sig = _signal("EMA_ORB", symbol, date, bar, side, stop, float(s["target_r"]),
                      "EMA pullback beyond the opening range %.2f-%.2f (%.2f%%), breakout %s, %s stop" % (
                          rng["low"], rng["high"], rng["pct"], seen, method))
        if sig is None:
            decide(False, "zero risk after rounding the stop")
            continue
        sig.update(opening_range={"high": rng["high"], "low": rng["low"]}, or_pct=rng["pct"], stop_method=method,
                   breakout_bar=seen, ema=r.get("ema"))
        decide(True, "accepted: %s" % sig["note"])
        out.append(sig)
    return out, state


def ema_orb_signals(symbol, date, prev_bars, bars, s, range_end="09:30"):
    return ema_orb_evaluate(symbol, date, prev_bars, bars, s, range_end)[0]


# ---------------------------------------------------------------- OI_PCR

def oi_symbols(s):
    """OI_PCR's indices from its comma-separated `symbols` setting."""
    return [x.strip().upper() for x in str(s.get("symbols") or "").split(",") if x.strip().upper() in INDICES]


def oi_record(chain, bar):
    """What OI_PCR keeps of one in-chain snapshot taken after `bar` (HH:MM start) closed."""
    by = {}
    for r in chain.get("rows") or []:
        if r.get("oi") is not None and r.get("type") in ("CE", "PE"):
            by.setdefault(float(r["strike"]), [None, None])[0 if r["type"] == "CE" else 1] = int(r["oi"])
    return {"bar": bar, "as_of_ist": chain["as_of_ist"], "expiry": chain["expiry"], "spot": chain["spot"],
            "step": chain["step"], "pcr": chain.get("pcr_oi"), "pcr_api": (chain.get("pcr_api") or {}).get("pcr"),
            "partial": chain.get("partial_note"), "oi": [[k, ce, pe] for k, (ce, pe) in sorted(by.items())]}


def _table(rec):
    return {float(k): (ce, pe) for k, ce, pe in rec.get("oi") or []}


def _pct(now, base):
    return None if now is None or not base else round((now - base) / base * 100, 2)


def oi_walls(rec):
    """(call wall, put wall): the strikes with the most call / put OI, ties to the one nearer spot."""
    t, spot = _table(rec), rec["spot"]
    ce = [(v[0], -abs(k - spot), k) for k, v in t.items() if v[0]]
    pe = [(v[1], -abs(k - spot), k) for k, v in t.items() if v[1]]
    return (max(ce)[2], max(pe)[2]) if ce and pe else None


def _fmt(x):
    return "n/a" if x is None else "%+.1f%%" % x


def oi_pcr_evaluate(symbol, date, bars, snaps, s, range_end="09:30"):
    """OI_PCR's signals for the day so far and its decisions, for the UI: (signals, state).
    snaps: {bar start HH:MM: oi_record}. Deterministic over the same bars and records."""
    base_bar = add_min(range_end, -BAR_MIN)
    base = snaps.get(base_bar)
    state = {"baseline": None, "now": None, "decisions": [], "snapshots": len(snaps), "note": None}
    if base is None:
        state["note"] = "no chain record for the %s bar (closing %s): the battleground is marked from it" % (
            base_bar, range_end)
        return [], state
    walls = oi_walls(base)
    if walls is None:
        state["note"] = "the %s chain record has no open interest" % base_bar
        return [], state
    call_w, put_w = walls
    b0, step = _table(base), float(base["step"])
    bp = base["pcr"]
    state["baseline"] = {"bar": base_bar, "as_of_ist": base["as_of_ist"], "expiry": base["expiry"],
                         "spot": base["spot"], "pcr": bp, "call_wall": call_w, "call_wall_oi": b0[call_w][0],
                         "put_wall": put_w, "put_wall_oi": b0[put_w][1],
                         "opening": None if bp is None else "balanced" if 0.9 <= bp <= 1.1 else
                         "put-heavy" if bp > 1.1 else "call-heavy"}
    near, unwind, surge = float(s["near_pct"]), float(s["min_unwind_pct"]), float(s["min_surge_pct"])
    bull, bear, swing = float(s["pcr_bull"]), float(s["pcr_bear"]), max(1, int(s["swing_bars"]))
    out, done, prev_pcr = [], set(), bp

    def chg(t, k, i):
        return _pct((t.get(k) or (None, None))[i], (b0.get(k) or (None, None))[i])

    for i, b in enumerate(bars):
        snap = snaps.get(b["start_ist"])
        if b["start_ist"] <= base_bar or b["start_ist"] < s["start_after"] or snap is None:
            continue
        pcr, t = snap["pcr"], _table(snap)
        state["now"] = {"bar": b["start_ist"], "as_of_ist": snap["as_of_ist"], "spot": snap["spot"], "pcr": pcr,
                        "pcr_prev": prev_pcr, "call_wall_ce_pct": chg(t, call_w, 0),
                        "call_wall_pe_pct": chg(t, call_w, 1), "put_wall_pe_pct": chg(t, put_w, 1),
                        "put_wall_ce_pct": chg(t, put_w, 0), "expiry": snap["expiry"]}
        for side in ("bullish", "bearish"):
            up = side == "bullish"
            wall = call_w if up else put_w
            at = b["close"] >= wall * (1 - near / 100) if up else b["close"] <= wall * (1 + near / 100)
            if side in done or not at:
                continue

            def decide(ok, reason):
                state["decisions"].append({"bar_start_ist": b["start_ist"], "bar_close_ist": add_min(b["start_ist"], BAR_MIN),
                                           "direction": side, "wall": wall, "accepted": ok, "reason": reason})
            if snap["expiry"] != base["expiry"]:
                decide(False, "the chain rolled to the %s expiry: OI is not comparable with the %s battleground" % (
                    snap["expiry"], base["expiry"]))
                continue
            own = chg(t, wall, 0 if up else 1)                     # the wall's writers: covering or adding?
            other = [x for x in (chg(t, wall, 1 if up else 0), chg(t, wall - step if up else wall + step, 1 if up else 0))
                     if x is not None]
            surged = max(other) if other else None
            kind, opp = ("call", "put") if up else ("put", "call")
            if own is None:
                decide(False, "no %s OI at the %s wall %g in this record" % (kind, kind, wall))
                continue
            if own > -unwind:
                decide(False, "%s writers %s at %g (%s %s OI since %s): %s" % (
                    kind, "adding" if own > 0 else "holding", wall, _fmt(own), kind, base_bar,
                    "they defend the wall, range-bound trap" if own >= 0 else "not unwinding %.1f%% yet" % unwind))
                continue
            if surged is None or surged < surge:
                decide(False, "%s writers covering at %g (%s) but %s OI %s, under +%.1f%%" % (
                    kind, wall, _fmt(own), opp, _fmt(surged), surge))
                continue
            if pcr is None or not (pcr > bull and (prev_pcr is None or pcr > prev_pcr) if up else
                                   pcr < bear and (prev_pcr is None or pcr < prev_pcr)):
                decide(False, "OI shift at %g, but PCR %s %s, needs %s %.2f and %s" % (
                    wall, "n/a" if pcr is None else "%.2f" % pcr,
                    "from %.2f" % prev_pcr if prev_pcr is not None else "", "above" if up else "under",
                    bull if up else bear, "rising" if up else "falling"))
                continue
            span = bars[max(0, i - swing + 1):i + 1]
            stop = min(x["low"] for x in span) if up else max(x["high"] for x in span)
            risk_pct = abs(b["close"] - stop) / b["close"] * 100
            if not float(s["min_risk_pct"]) <= risk_pct <= float(s["max_risk_pct"]):
                decide(False, "stop risk %.2f%% outside %.2f%%..%.2f%%" % (risk_pct, float(s["min_risk_pct"]),
                                                                         float(s["max_risk_pct"])))
                continue
            sig = _signal("OI_PCR", symbol, date, b, side, stop, float(s["target_r"]),
                          "%s writers covering at %g (%s), %s writers adding (%s), PCR %.2f %s from %.2f" % (
                              kind, wall, _fmt(own), opp, _fmt(surged), pcr, "rising" if up else "falling",
                              prev_pcr if prev_pcr is not None else pcr))
            if sig is None:
                decide(False, "zero risk after rounding the stop")
                continue
            sig["oi"] = {"wall": wall, "own_change_pct": own, "opposite_change_pct": surged, "pcr": pcr,
                         "pcr_prev": prev_pcr, "battleground_pcr": bp, "record_as_of_ist": snap["as_of_ist"],
                         "expiry": snap["expiry"]}
            decide(True, "accepted: %s" % sig["note"])
            done.add(side)
            out.append(sig)
        prev_pcr = pcr
    return out, state


# ---------------------------------------------------------------- LORENTZIAN (the model is app/lorentzian.py)

def lorentzian_signals(symbol, date, recs, s):
    """The paper signals of the indicator's volume-confirmed entries on `date`'s bars. recs: lorentzian.run()
    records (history first). The stop is lorentzian.stop_for: the indicator itself has none."""
    from app import lorentzian as L
    out = []
    for i, r in enumerate(recs):
        if r["date"] != date or not (r["confirmed_long"] or r["confirmed_short"]):
            continue
        up = bool(r["confirmed_long"])
        sig = _signal("LORENTZIAN", symbol, date, r, "bullish" if up else "bearish", L.stop_for(recs, i, s, up),
                      float(s["target_r"]), "Lorentzian %s: prediction %+g from %d neighbors, kernel %s%s" % (
                          "BUY" if up else "SELL", r["prediction"], r["neighbors"], r["kernel_dir"] or "flat",
                          ", RVOL %.2f" % r["rvol"] if r["rvol"] is not None else ""))
        if sig:
            sig["lorentzian"] = {"prediction": r["prediction"], "neighbors": r["neighbors"], "kernel": r["kernel"],
                                 "kernel_dir": r["kernel_dir"], "rvol": r["rvol"], "features": r["features"]}
            out.append(sig)
    return out


def evaluate(cfg, symbol, date, prev_bars, bars, range_end="09:30", oi=None, lc=None):
    """Every enabled strategy's signals for today's closed bars. prev_bars None (the previous day
    could not be fetched) skips EMA_PULLBACK: EMAs seeded on less data would give different signals.
    oi: OI_PCR's {bar: oi_record} for this symbol today; None skips OI_PCR. lc: LORENTZIAN's records
    (app/lorentzian.run over the history and today's bars); None skips it."""
    bars = clean_bars(bars)
    out = []
    s = strategy_cfg(cfg, "ORB")
    if s["enabled"]:
        out += orb_signals(symbol, date, bars, s, range_end)
    s = strategy_cfg(cfg, "EMA_PULLBACK")
    if s["enabled"] and prev_bars is not None:
        out += ema_pullback_signals(symbol, date, clean_bars(prev_bars), bars, s)
    s = strategy_cfg(cfg, "EMA_ORB")
    if s["enabled"] and prev_bars is not None:
        out += ema_orb_signals(symbol, date, clean_bars(prev_bars), bars, s, range_end)
    s = strategy_cfg(cfg, "OI_PCR")
    if s["enabled"] and oi is not None and symbol in oi_symbols(s):
        out += oi_pcr_evaluate(symbol, date, bars, oi, s, range_end)[0]
    s = strategy_cfg(cfg, "LORENTZIAN")
    if s["enabled"] and lc is not None and symbol in oi_symbols(s):
        out += lorentzian_signals(symbol, date, lc, s)
    return out
