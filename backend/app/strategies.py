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
}
STOP_METHODS = ("ema", "atr", "swing")
ZERO_OK = {"min_or_pct"}          # settings where 0 is a valid value (it turns the filter off)


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


def evaluate(cfg, symbol, date, prev_bars, bars, range_end="09:30"):
    """Every enabled strategy's signals for today's closed bars. prev_bars None (the previous day
    could not be fetched) skips EMA_PULLBACK: EMAs seeded on less data would give different signals."""
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
    return out
