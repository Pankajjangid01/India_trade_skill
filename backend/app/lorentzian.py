"""Machine Learning: Lorentzian Classification (jdehorty, TradingView, MPL-2.0), on closed 5-minute bars.

A faithful port of the indicator's source EXPRESSIONS (Pine v5, MLExtensions v2, KernelFunctions v2), checked
against the script and a published Python port. Where the source's comments say something else, the
expressions win, as on TradingView:

  label     y = src[4] < src[0] ? -1 (short) : src[4] > src[0] ? +1 (long) : 0, stored at bar i. It is the
            PAST four-bar move, inverted (the comment calls it "the next 4 bars"); features[i] pair with it.
  neighbors the FIRST max_bars_back bars of the data (array indices 0..min(max_bars_back-1, bar)), in order;
            `d >= lastDistance and i%4` keeps i with i % 4 != 0 (3 of every 4 bars; the comment says "every 4
            bars"). The predictions/distances buffers persist across bars (Pine `var`); lastDistance restarts
            at -1 each bar; on overflow lastDistance = distances[round(k*3/4)] (the comment says "lower 25%").
  distance  sum(ln(1 + |x_i - y_i|)) over the selected features; a NaN distance never qualifies.
  features  RSI and CCI read close, WaveTrend reads hlc3, ADX reads high/low/close whatever `source` is;
            ADX ignores its parameter B. normalize() uses the running min/max (no look-ahead).
  exits     strict: ((held 4 bars and signal[4] long) or (held < 4 and new sell and signal[4] long)) and
            startLong[4]; the second branch can never fire (held is 0 on a signal change). Dynamic exits replace
            it only when no EMA/SMA filter and no kernel smoothing is on.
  kernels   rational quadratic and Gaussian sums over i = 0..regression_level+1.
ta.ema / ta.rma / ta.sma follow Pine: na until `length` values, seeded with their SMA.

Adapted to the Indian session (the same in live and in the backtest, so the two cannot differ):
  the data    as if the chart were loaded at the session open with exactly max_bars_back closed 5-minute bars
              of history (the previous ~27 trading days) and then ran forward bar by bar: maxBarsBackIndex =
              0, the neighbors are those history bars, today's bars append. Rebuilt every trading day.
  source      hlc3 by default (the original's default is close).
  volume      index candles carry no volume: the near-month index FUTURES' volume, bar by bar (added rules,
              kept apart from the original logic): RVOL = volume / SMA(volume, volume_ma_period); the volume
              filter confirms an entry when RVOL >= min_rvol on a futures candle in the trade's direction;
              the optional sixth feature is normalize(RVOL over rvol_feature_period).
numpy for the distances only (about 2 million bar pairs a day); the neighbor rules stay a sequential loop.
Deterministic: a bar's record depends only on that bar and the bars before it.
"""

import math

import numpy as np

NAN = float("nan")
FEATURE_KINDS = ("RSI", "WT", "CCI", "ADX")
SOURCES = ("close", "open", "high", "low", "hl2", "hlc3", "ohlc4")
DIRECTION = {1: "bullish", -1: "bearish", 0: None}
# the settings' allowed ranges and choices (the original's input bounds where it has them)
RANGES = dict({"neighbors_count": (1, 100), "max_bars_back": (200, 5000), "feature_count": (2, 5),
               "color_compression": (1, 10), "regime_threshold": (-10, 10), "adx_threshold": (0, 100),
               "ema_period": (1, 1000), "sma_period": (1, 1000), "kernel_lookback": (3, 500),
               "kernel_relative_weight": (0.25, 100), "kernel_regression_level": (2, 500), "kernel_lag": (1, 100),
               "volume_ma_period": (2, 500), "min_rvol": (0, 20), "rvol_feature_period": (2, 500), "swing_bars": (1, 50)},
              **{"f%d_%s" % (k, p): (1, 500) for k in range(1, 6) for p in "ab"})
CHOICES = dict({"source": SOURCES}, **{"f%d" % k: FEATURE_KINDS for k in range(1, 6)})


def _na(x):
    return x is None or x != x


def _f(x):
    return NAN if x is None else float(x)


# ---------------------------------------------------------------- Pine ta.* on lists (NaN = na)

def sma(xs, n):
    out, s, bad = [], 0.0, 0
    for i, x in enumerate(xs):
        if x != x:
            bad += 1
        else:
            s += x
        if i >= n:
            y = xs[i - n]
            if y != y:
                bad -= 1
            else:
                s -= y
        out.append(s / n if i >= n - 1 and not bad else NAN)
    return out


def _seeded(xs, n, alpha):
    """pine_rma / pine_ema: na(sum[1]) ? ta.sma(src, length) : alpha * src + (1 - alpha) * sum[1]."""
    seed, out, prev = sma(xs, n), [], NAN
    for i, x in enumerate(xs):
        v = seed[i] if prev != prev else alpha * x + (1 - alpha) * prev
        out.append(v)
        prev = v
    return out


def ema(xs, n):
    return _seeded(xs, n, 2.0 / (n + 1))


def rma(xs, n):
    return _seeded(xs, n, 1.0 / n)


def rsi(xs, n):
    up = [NAN] + [max(b - a, 0.0) for a, b in zip(xs, xs[1:])]
    dn = [NAN] + [max(a - b, 0.0) for a, b in zip(xs, xs[1:])]
    out = []
    for u, d in zip(rma(up, n), rma(dn, n)):
        out.append(NAN if u != u or d != d else 100.0 if d == 0 else 0.0 if u == 0 else 100 - 100 / (1 + u / d))
    return out


def cci(xs, n):
    ma, out = sma(xs, n), []
    for i, m in enumerate(ma):
        if m != m:
            out.append(NAN)
            continue
        dev = sum(abs(x - m) for x in xs[i - n + 1:i + 1]) / n
        out.append((xs[i] - m) / (0.015 * dev) if dev else NAN)
    return out


def true_range(h, l, c):
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]


def atr(h, l, c, n):
    return rma(true_range(h, l, c), n)


def rescale(xs, old_min, old_max, new_min=0.0, new_max=1.0):
    span = max(old_max - old_min, 10e-10)
    return [new_min + (new_max - new_min) * (x - old_min) / span for x in xs]


def normalize(xs, lo=0.0, hi=1.0):
    """MLExtensions.normalize: the running historic min/max, so a value never sees a later bar."""
    hmin, hmax, out = 10e10, -10e10, []
    for x in xs:
        if x == x:
            hmin, hmax = min(x, hmin), max(x, hmax)
        out.append(lo + (hi - lo) * (x - hmin) / max(hmax - hmin, 10e-10))
    return out


def _div(a, b):
    return a / b if b else NAN


def dmi_adx(h, l, c, n):
    """MLExtensions' own ADX (n_adx and filter_adx): Wilder sums with nz(x[1]) = 0 on the first bar."""
    trs = sp = sn = 0.0
    dx = []
    for i in range(len(c)):
        h1, l1, c1 = (h[i - 1], l[i - 1], c[i - 1]) if i else (0.0, 0.0, 0.0)
        tr = max(h[i] - l[i], abs(h[i] - c1), abs(l[i] - c1))
        plus = max(h[i] - h1, 0.0) if h[i] - h1 > l1 - l[i] else 0.0
        minus = max(l1 - l[i], 0.0) if l1 - l[i] > h[i] - h1 else 0.0
        trs, sp, sn = trs - trs / n + tr, sp - sp / n + plus, sn - sn / n + minus
        dip, din = _div(sp, trs) * 100, _div(sn, trs) * 100
        dx.append(_div(abs(dip - din), dip + din) * 100)
    return rma(dx, n)


def n_rsi(close, a, b):
    return rescale(ema(rsi(close, a), b), 0, 100)


def n_cci(close, a, b):
    return normalize(ema(cci(close, a), b))


def n_wt(src, a, b):
    e1 = ema(src, a)
    e2 = ema([abs(x - e) for x, e in zip(src, e1)], a)
    ci = [_div(x - e, 0.015 * d) if d == d else NAN for x, e, d in zip(src, e1, e2)]
    wt1 = ema(ci, b)
    wt2 = sma(wt1, 4)
    return normalize([x - y for x, y in zip(wt1, wt2)])


def n_adx(h, l, c, a):
    return rescale(dmi_adx(h, l, c, a), 0, 100)


def series_from(kind, c, h, l, hlc3, a, b):
    if kind == "RSI":
        return n_rsi(c, a, b)
    if kind == "WT":
        return n_wt(hlc3, a, b)
    if kind == "CCI":
        return n_cci(c, a, b)
    if kind == "ADX":
        return n_adx(h, l, c, a)
    raise ValueError("feature must be one of %s, not %r" % (", ".join(FEATURE_KINDS), kind))


def regime_slope(src, h, l):
    """MLExtensions.regime_filter's normalized_slope_decline (passes when >= threshold)."""
    v1 = v2 = kl = 0.0
    prev_src, prev_kl, slope = NAN, NAN, []
    for i, x in enumerate(src):
        v1 = 0.2 * (x - prev_src) + 0.8 * (v1 if v1 == v1 else 0.0)
        v2 = 0.1 * (h[i] - l[i]) + 0.8 * (v2 if v2 == v2 else 0.0)
        om = abs(_div(v1, v2))
        alpha = (-om ** 2 + math.sqrt(om ** 4 + 16 * om ** 2)) / 8 if om == om else NAN
        kl = alpha * x + (1 - alpha) * (prev_kl if prev_kl == prev_kl else 0.0)
        slope.append(abs(kl - prev_kl))
        prev_src, prev_kl = x, kl
    avg = ema(slope, 200)
    return [_div(s - a, a) for s, a in zip(slope, avg)]


def rational_quadratic(src, h, r, x):
    w = [(1 + i * i / (h * h * 2 * r)) ** -r for i in range(x + 2)]
    return _kernel(src, w)


def gaussian(src, h, x):
    w = [math.exp(-i * i / (2 * h * h)) for i in range(x + 2)]
    return _kernel(src, w)


def _kernel(src, w):
    tw, n, out = sum(w), len(w), []
    for b in range(len(src)):
        if b + 1 < n:
            out.append(NAN)                      # src[i] is na before the first bar
            continue
        out.append(sum(src[b - i] * w[i] for i in range(n)) / tw)
    return out


def pine_round(x):
    return math.floor(x + 0.5)


def barssince(flags):
    out, last = [], None
    for i, f in enumerate(flags):
        if f:
            last = i
        out.append(None if last is None else i - last)
    return out


def _gt(a, b):
    return a is not None and b is not None and a > b


# ---------------------------------------------------------------- the approximate nearest neighbors

class Ann:
    """The Core ML Logic, incremental: a bar already processed is never recomputed while the data before it is
    unchanged (its feature row and label are kept as a fingerprint); any change rebuilds from the first bar."""

    def __init__(self, k, max_bars_back, start=0):
        self.k, self.mbb, self.start = int(k), int(max_bars_back), int(start)
        self.reset()

    def reset(self):
        self.preds, self.dists, self.out, self.used, self.keys = [], [], [], [], []

    def run(self, rows, labels):
        keys = [repr((r, y)) for r, y in zip(rows, labels)]
        n = len(self.out)
        if keys[:n] != self.keys:
            self.reset()
        k, mbb, cut = self.k, self.mbb, pine_round(self.k * 3 / 4)
        feats = np.array(rows, dtype=float).reshape(len(rows), -1)
        idx = [i for i in range(1, mbb) if i % 4]                  # `and i%4`: never a multiple of 4
        for b in range(len(self.out), len(rows)):
            pred = self.out[-1] if self.out else 0.0
            if b >= self.start:
                m = min(mbb - 1, b)
                t = np.log(1 + np.abs(feats[:m + 1] - feats[b]))   # ln(1 + |x_i - y_i|), NaN stays NaN
                dist = t[:, 0].copy()
                for f in range(1, t.shape[1]):                     # summed feature by feature, as the source
                    dist += t[:, f]
                dist = dist.tolist()
                last = -1.0
                for i in idx[:m - m // 4]:
                    d = dist[i]
                    if d >= last:                                  # a NaN distance never qualifies
                        last = d
                        self.dists.append(d)
                        self.preds.append(labels[i])
                        if len(self.preds) > k:
                            last = self.dists[cut]
                            del self.dists[0], self.preds[0]
                pred = float(sum(self.preds))
            self.out.append(pred)
            self.used.append(len(self.preds))
            self.keys.append(keys[b])
        return self.out, self.used


# ---------------------------------------------------------------- the indicator

def source_of(b, name):
    o, h, l, c = b["open"], b["high"], b["low"], b["close"]
    return {"close": c, "open": o, "high": h, "low": l, "hl2": (h + l) / 2, "hlc3": (h + l + c) / 3,
            "ohlc4": (o + h + l + c) / 4}[name]


def feature_names(s):
    out = ["%s(%d,%d)" % (s["f%d" % k], int(s["f%d_a" % k]), int(s["f%d_b" % k]))
           for k in range(1, int(s["feature_count"]) + 1)]
    return out + (["RVOL(%d)" % int(s["rvol_feature_period"])] if s.get("use_rvol_feature") else [])


def rvol_series(vol, n):
    ma = sma(vol, n)
    return [_div(v, m) if v == v and m == m else NAN for v, m in zip(vol, ma)], ma


def run(bars, s, ann=None):
    """One record per bar (oldest first). bars: {"date", "start_ist", "open", "high", "low", "close",
    "volume" (the futures volume or None), "vol_open", "vol_close" (that futures candle)}. `ann` keeps the
    neighbor search between calls (live: one per index per day)."""
    n = len(bars)
    if n == 0:
        return []
    o = [float(b["open"]) for b in bars]
    h = [float(b["high"]) for b in bars]
    l = [float(b["low"]) for b in bars]
    c = [float(b["close"]) for b in bars]
    hlc3 = [(a + b_ + d) / 3 for a, b_, d in zip(h, l, c)]
    src = [source_of(b, s["source"]) for b in bars]
    vol = [_f(b.get("volume")) for b in bars]

    # features (series_from), the optional RVOL feature, the labels
    feats = [series_from(s["f%d" % k], c, h, l, hlc3, int(s["f%d_a" % k]), int(s["f%d_b" % k]))
             for k in range(1, int(s["feature_count"]) + 1)]
    if s.get("use_rvol_feature"):
        feats.append(normalize(rvol_series(vol, int(s["rvol_feature_period"]))[0]))
    rows = list(zip(*feats))
    labels = [0 if i < 4 else (-1 if src[i - 4] < src[i] else 1 if src[i - 4] > src[i] else 0) for i in range(n)]

    ann = ann or Ann(s["neighbors_count"], s["max_bars_back"])
    preds, used = ann.run(rows, labels)

    # filters
    volatility = [a > b_ if a == a and b_ == b_ else False for a, b_ in zip(atr(h, l, c, 1), atr(h, l, c, 10))]
    slope = regime_slope([(a + b_ + d + e) / 4 for a, b_, d, e in zip(o, h, l, c)], h, l)
    regime = [x >= float(s["regime_threshold"]) if x == x else False for x in slope]
    adx14 = dmi_adx(h, l, c, 14)
    adx_ok = [x > float(s["adx_threshold"]) if x == x else False for x in adx14]
    f_vol = volatility if s["use_volatility_filter"] else [True] * n
    f_reg = regime if s["use_regime_filter"] else [True] * n
    f_adx = adx_ok if s["use_adx_filter"] else [True] * n
    ema_t = ema(c, int(s["ema_period"]))
    sma_t = sma(c, int(s["sma_period"]))
    ema_up = [x > e if s["use_ema_filter"] else True for x, e in zip(c, ema_t)]
    ema_dn = [x < e if s["use_ema_filter"] else True for x, e in zip(c, ema_t)]
    sma_up = [x > m if s["use_sma_filter"] else True for x, m in zip(c, sma_t)]
    sma_dn = [x < m if s["use_sma_filter"] else True for x, m in zip(c, sma_t)]

    # kernels
    hk, lag, xk = int(s["kernel_lookback"]), int(s["kernel_lag"]), int(s["kernel_regression_level"])
    y1 = rational_quadratic(src, hk, float(s["kernel_relative_weight"]), xk)
    y2 = gaussian(src, hk - lag, xk)

    def at(xs, i):
        return xs[i] if i >= 0 else NAN

    bull_rate = [at(y1, i - 1) < y1[i] for i in range(n)]
    bear_rate = [at(y1, i - 1) > y1[i] for i in range(n)]
    was_bull = [at(y1, i - 2) < at(y1, i - 1) for i in range(n)]
    was_bear = [at(y1, i - 2) > at(y1, i - 1) for i in range(n)]
    bull_change = [a and b_ for a, b_ in zip(bull_rate, was_bear)]
    bear_change = [a and b_ for a, b_ in zip(bear_rate, was_bull)]
    bull_cross = [y2[i] > y1[i] and at(y2, i - 1) <= at(y1, i - 1) for i in range(n)]
    bear_cross = [y2[i] < y1[i] and at(y2, i - 1) >= at(y1, i - 1) for i in range(n)]
    smooth = bool(s["kernel_smoothing"])
    alert_bull = bull_cross if smooth else bull_change
    alert_bear = bear_cross if smooth else bear_change
    if s["use_kernel_filter"]:
        is_bull = [y2[i] >= y1[i] for i in range(n)] if smooth else bull_rate
        is_bear = [y2[i] <= y1[i] for i in range(n)] if smooth else bear_rate
    else:
        is_bull = is_bear = [True] * n

    # the filtered signal, the bar-count filters, entries
    signal, change, held = [], [], []
    sig, bh = 0, 0
    for i in range(n):
        ok = f_vol[i] and f_reg[i] and f_adx[i]
        new = 1 if preds[i] > 0 and ok else -1 if preds[i] < 0 and ok else sig
        ch = i > 0 and new != sig
        bh = 0 if ch else bh + 1
        sig = new
        signal.append(sig)
        change.append(ch)
        held.append(bh)
    buy = [signal[i] == 1 and ema_up[i] and sma_up[i] for i in range(n)]
    sell = [signal[i] == -1 and ema_dn[i] and sma_dn[i] for i in range(n)]
    new_buy = [buy[i] and change[i] for i in range(n)]
    new_sell = [sell[i] and change[i] for i in range(n)]
    start_long = [new_buy[i] and is_bull[i] and ema_up[i] and sma_up[i] for i in range(n)]
    start_short = [new_sell[i] and is_bear[i] and ema_dn[i] and sma_dn[i] for i in range(n)]

    # exits
    since_red_entry, since_red_exit = barssince(start_short), barssince(alert_bull)
    since_green_entry, since_green_exit = barssince(start_long), barssince(alert_bear)
    valid_short = [_gt(a, b_) for a, b_ in zip(since_red_exit, since_red_entry)]
    valid_long = [_gt(a, b_) for a, b_ in zip(since_green_exit, since_green_entry)]
    dynamic = bool(s["use_dynamic_exits"]) and not s["use_ema_filter"] and not s["use_sma_filter"] and not smooth
    end_long, end_short = [], []
    for i in range(n):
        j = i - 4
        last_buy = j >= 0 and signal[j] == 1 and ema_up[j] and sma_up[j]
        last_sell = j >= 0 and signal[j] == -1 and ema_dn[j] and sma_dn[j]
        less = 0 < held[i] < 4
        strict_l = ((held[i] == 4 and last_buy) or (less and new_sell[i] and last_buy)) and j >= 0 and start_long[j]
        strict_s = ((held[i] == 4 and last_sell) or (less and new_buy[i] and last_sell)) and j >= 0 and start_short[j]
        dyn_l = bear_change[i] and i > 0 and valid_long[i - 1]
        dyn_s = bull_change[i] and i > 0 and valid_short[i - 1]
        end_long.append(dyn_l if dynamic else strict_l)
        end_short.append(dyn_s if dynamic else strict_s)

    # volume (added rules, outside the original logic)
    vp = int(s["volume_ma_period"])
    rv, vma = rvol_series(vol, vp)
    vdir = []
    for b in bars:
        vo, vc = b.get("vol_open"), b.get("vol_close")
        a, z = (vo, vc) if vo is not None and vc is not None else (b["open"], b["close"])
        vdir.append(1 if z > a else -1 if z < a else 0)
    up_vol = sma([v if d > 0 and v == v else 0.0 for v, d in zip(vol, vdir)], vp)
    dn_vol = sma([v if d < 0 and v == v else 0.0 for v, d in zip(vol, vdir)], vp)
    min_rvol = float(s["min_rvol"])
    comp = float(s["neighbors_count"]) / float(s["color_compression"])

    out = []
    for i, b in enumerate(bars):
        p = preds[i]
        rvol_ok = rv[i] == rv[i] and rv[i] >= min_rvol
        vl, vs = rvol_ok and vdir[i] > 0, rvol_ok and vdir[i] < 0
        if not s["use_volume_filter"]:
            why_l = why_s = None
        else:
            why = None if rvol_ok else ("no futures volume for this bar" if vol[i] != vol[i] else
                                        "no 20-bar volume average yet" if rv[i] != rv[i] else
                                        "RVOL %.2f under %.2f" % (rv[i], min_rvol))
            why_l = why or (None if vl else "volume on a %s futures candle" % ("bearish" if vdir[i] < 0 else "flat"))
            why_s = why or (None if vs else "volume on a %s futures candle" % ("bullish" if vdir[i] > 0 else "flat"))
        out.append({
            "date": b.get("date"), "start_ist": b.get("start_ist"), "open": o[i], "high": h[i], "low": l[i],
            "close": c[i], "prediction": p, "direction": DIRECTION[(p > 0) - (p < 0)], "neighbors": used[i],
            "color": max(-1.0, min(1.0, p / comp)) if comp else 0.0, "signal": signal[i], "signal_change": change[i],
            "bars_held": held[i], "features": [None if _na(x) else round(x, 6) for x in rows[i]],
            "label": labels[i], "filters": {
                "volatility": f_vol[i], "regime": f_reg[i], "adx": f_adx[i], "ema_up": ema_up[i], "ema_down": ema_dn[i],
                "sma_up": sma_up[i], "sma_down": sma_dn[i], "kernel_bullish": is_bull[i], "kernel_bearish": is_bear[i],
                "raw": {"volatility": volatility[i], "regime": regime[i], "regime_slope": None if _na(slope[i]) else
                        round(slope[i], 4), "adx": None if _na(adx14[i]) else round(adx14[i], 2), "adx_ok": adx_ok[i],
                        "ema": None if _na(ema_t[i]) else round(ema_t[i], 2),
                        "sma": None if _na(sma_t[i]) else round(sma_t[i], 2)}},
            "kernel": None if _na(y1[i]) else round(y1[i], 2), "kernel2": None if _na(y2[i]) else round(y2[i], 2),
            "kernel_dir": "bullish" if bull_rate[i] else "bearish" if bear_rate[i] else None,
            "start_long": start_long[i], "start_short": start_short[i], "end_long": end_long[i],
            "end_short": end_short[i], "exit_mode": "dynamic" if dynamic else "fixed",
            "volume": None if _na(vol[i]) else vol[i], "volume_ma": None if _na(vma[i]) else round(vma[i], 1),
            "rvol": None if _na(rv[i]) else round(rv[i], 3), "volume_dir": vdir[i],
            "bull_volume": None if _na(up_vol[i]) else round(up_vol[i] * vp, 1),
            "bear_volume": None if _na(dn_vol[i]) else round(dn_vol[i] * vp, 1),
            "volume_reject_long": why_l if start_long[i] else None, "volume_reject_short": why_s if start_short[i] else None,
            "confirmed_long": start_long[i] and why_l is None, "confirmed_short": start_short[i] and why_s is None})
    return out


# ---------------------------------------------------------------- signals and exits for the paper engine

def stop_for(recs, i, s, up):
    """The trade's index stop (the indicator has none): the swing low/high of the last swing_bars bars, its
    distance held inside min_risk_pct..max_risk_pct of price (never a reason to drop the indicator's signal)."""
    span = recs[max(0, i - int(s["swing_bars"]) + 1):i + 1]
    entry = recs[i]["close"]
    raw = entry - min(r["low"] for r in span) if up else max(r["high"] for r in span) - entry
    risk = min(max(raw, entry * float(s["min_risk_pct"]) / 100), entry * float(s["max_risk_pct"]) / 100)
    return entry - risk if up else entry + risk


def _bars_of(day):
    return [{"date": d, "start_ist": str(r[0])[11:16], "open": r[1], "high": r[2], "low": r[3], "close": r[4],
             "volume": r[5] if len(r) > 5 else None} for d, rows in day for r in rows]


def dataset(index_days, futures_days, day, max_bars_back):
    """What the indicator sees on `day`: the last max_bars_back closed bars before it, then that day's bars, each
    with its futures bar's volume and candle (None where the futures have no bar). index_days/futures_days:
    {"YYYY-MM-DD": [[time, o, h, l, c, v], ...]} (smart.candles rows)."""
    before = [d for d in sorted(index_days) if d < day and index_days[d]]
    hist = _bars_of([(d, index_days[d]) for d in before])[-int(max_bars_back):]
    bars = hist + _bars_of([(day, index_days.get(day) or [])])
    fut = {(d, str(r[0])[11:16]): r for d in futures_days for r in futures_days[d] or []}
    for b in bars:
        f = fut.get((b["date"], b["start_ist"]))
        b.update(volume=float(f[5]) if f and len(f) > 5 and f[5] else None, vol_open=f[1] if f else None,
                 vol_close=f[4] if f else None)
    return bars


CONFIGS = (("LC", "1: Lorentzian, no volume filter", False, False),
           ("LC_VOLUME_FILTER", "2: with the volume confirmation filter", True, False),
           ("LC_RVOL_FEATURE", "3: with RVOL as a sixth ML feature", False, True),
           ("LC_BOTH", "4: volume filter and RVOL feature", True, True))


def day_trades(recs, day, s, use_filter, rule, cost_r, stop_slip_r, max_trades, expiry_day, symbol):
    """The paper engine's trades on `day` from these records, filled by research.realistic: entries inside the
    entry window, one trade at a time per index, at most max_trades, the indicator's exit at a bar close."""
    from app import research
    from app import strategies as strat
    first = next((i for i, r in enumerate(recs) if r["date"] == day), None)
    if first is None:
        return []
    bars = recs[first:]
    last_entry = min(rule["entry_end"], rule["expiry_last_entry"]) if expiry_day else rule["entry_end"]
    out, busy = [], None
    for i in range(first, len(recs)):
        r = recs[i]
        up = r["confirmed_long"] if use_filter else r["start_long"]
        down = r["confirmed_short"] if use_filter else r["start_short"]
        close_hm = strat.add_min(r["start_ist"], strat.BAR_MIN)
        if not (up or down) or not rule["entry_start"] <= close_hm < last_entry or (busy and close_hm <= busy):
            continue
        if len(out) >= max_trades:
            break
        sig = strat._signal("LORENTZIAN", symbol, day, r, "bullish" if up else "bearish",
                            stop_for(recs, i, s, bool(up)), float(s["target_r"]), None)
        if sig is None:
            continue
        j, why = exit_bar(recs, i, bool(up), s)
        t = {"date": day, "direction": sig["direction"], "entry": sig["entry_underlying"], "stop": sig["stop_underlying"],
             "target": sig["target_underlying"], "entry_time": close_hm, "expiry_day": expiry_day,
             "signal_id": sig["id"], "strategy": "LORENTZIAN"}
        rt = research.realistic(t, bars, rule, cost_r, stop_slip_r, recs[j]["start_ist"] if j is not None else None)
        if rt is None:
            continue
        rt["exit_reason"] = why if rt["outcome"] == "signal_exit" else rt["outcome"].upper()
        out.append(rt)
        busy = rt["exit_time"]
    return out


def compare(days, s, rule, cost_r, stop_slip_r, risk_inr, max_trades, expiries, symbol, model=None,
            progress=None):
    """The four configurations on the same days. days: [(date, dataset bars)] oldest first. Each day is one model
    built as live builds it (history + that day); `model(day, bars, s)` may cache it. Results by segment
    (research's chronological 50/25/25 train/validation/test: nothing is fitted, so validation and test are
    plain out-of-sample periods to check that a difference holds up)."""
    from app import research
    model = model or (lambda d, bars, ss: run(bars, ss))
    trades = {k: [] for k, _, _, _ in CONFIGS}
    for n, (d, bars) in enumerate(days):
        for rv in (False, True):
            ss = dict(s, use_rvol_feature=rv, use_volume_filter=True)
            recs = model(d, bars, ss)
            for key, _, vf, feat in CONFIGS:
                if feat == rv:
                    trades[key] += day_trades(recs, d, ss, vf, rule, cost_r, stop_slip_r, max_trades,
                                              d in expiries, symbol)
        if progress:
            progress(n + 1, len(days))
    seg = research.segment_of([d for d, _ in days])
    result = {"configs": [{"key": k, "label": label, "volume_filter": vf, "rvol_feature": feat}
                          for k, label, vf, feat in CONFIGS], "by_config": {}, "equity": {},
              "segments": research.segment_ranges([d for d, _ in days]) if days else {}}
    for key, ts in trades.items():
        for t in ts:
            t["segment"] = seg[t["date"]]
        result["by_config"][key] = research._by_segment(ts, risk_inr)
        result["equity"][key] = research._curve(ts)
        result.setdefault("exits", {})[key] = {k: sum(1 for t in ts if t["exit_reason"] == k)
                                               for k in sorted({t["exit_reason"] for t in ts})}
    result["verdict"] = verdict(result)
    return result


MIN_PERIOD_TRADES = 30          # per configuration and out-of-sample period, before any difference is called


def verdict(result):
    """Plain words: does a volume configuration beat config 1 out of sample? Never assumed, and never called on
    fewer than MIN_PERIOD_TRADES trades in a period (a handful of trades decides nothing)."""
    base = result["by_config"]["LC"]
    lines = []
    for key, label, _, _ in CONFIGS[1:]:
        m = result["by_config"][key]
        ok, counts = [], []
        for segname in ("validation", "test"):
            a, b = m.get(segname) or {}, base.get(segname) or {}
            counts.append(min(a.get("trades") or 0, b.get("trades") or 0))
            if counts[-1] < MIN_PERIOD_TRADES:
                ok.append(None)
                continue
            ok.append(a["avg_r"] > b["avg_r"] and (a.get("profit_factor") or 0) >= (b.get("profit_factor") or 0))
        if None in ok:
            lines.append("%s: too few trades out of sample to tell (validation %d, test %d; at least %d each)" % (
                label, counts[0], counts[1], MIN_PERIOD_TRADES))
        elif all(ok):
            lines.append("%s: better than config 1 in BOTH out-of-sample periods (average R and profit factor)"
                         % label)
        else:
            lines.append("%s: not better than config 1 out of sample (validation %s, test %s)" % (
                label, "better" if ok[0] else "not better", "better" if ok[1] else "not better"))
    return lines


def exit_bar(recs, i, up, s):
    """(index of the bar whose close exits a trade entered on recs[i], reason) or (None, None)."""
    for j in range(i + 1, len(recs)):
        r = recs[j]
        if r["end_long" if up else "end_short"]:
            return j, "LC_EXIT_DYNAMIC" if r["exit_mode"] == "dynamic" else "LC_EXIT_4_BARS"
        if s.get("exit_on_flip") and r["signal_change"] and r["signal"] == (-1 if up else 1):
            return j, "LC_EXIT_FLIP"
    return None, None
