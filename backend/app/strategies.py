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
}


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
    return out
