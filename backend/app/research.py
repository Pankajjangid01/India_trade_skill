"""Strategy research: compare strategy versions and test targets / stops with costs, realistic fills and a
chronological train / validation / test split. Index-price results in R; never presented as option P&L.

Signals come from app.timemachine.simulate, which calls the same strategy functions as the live engine
(strategies.ema_pullback_signals, strategies.ema_orb_signals; ORB through in-timemachine's replay_day).
On top of each replayed trade this module re-runs the exit realistically:

  entry   the OPEN of the bar after the signal bar (the signal is only known at its close); a trade whose next
          open is already through its stop or target is skipped (the gate's "no longer valid" rule)
  stop    checked before the target inside a bar; a bar that OPENS through the stop fills at that open (a gap is
          worse than the stop), and every stop exit pays stop_slippage_r more
  target  filled at the target (a gap through it is still the target: no gap luck)
  flat-by out at the close of the last bar that ends by the flat-by time (expiry-day times on expiry days)
  R       the move over the PLANNED risk (signal close to stop), as the position was sized, minus costs

Costs, in R of one lot's planned risk, from charges.json and the plan's premium stop:
  (round-trip charges of one lot at an assumed premium P sold at P x (1 - max_premium_loss_pct))
  / (P x max_premium_loss_pct x lot) + 2 x option_slippage_pct / max_premium_loss_pct
P is an assumption (backtest.assumed_premium per index, configurable): no historical option prices exist here.

Split: each index's trading days in order: the first 50 % train, the next 25 % validation, the last 25 % test.
The sweep chooses on train, confirms on validation and reports the test period for the chosen setting only.
"""

import math
from decimal import Decimal

from app import strategies as strat
from app import timemachine as T
from app.sp import size, smart, tm

SEGMENTS = (("train", 0.0, 0.5), ("validation", 0.5, 0.75), ("test", 0.75, 1.0))
DEFAULT_PREMIUM = {"NIFTY": 150.0, "BANKNIFTY": 350.0, "SENSEX": 300.0}
DEFAULTS = {"option_slippage_pct": 0.5, "stop_slippage_r": 0.05}
TARGETS = (1.0, 1.5, 2.0, 2.5, 3.0)
MIN_TRAIN_TRADES = 30
FULL_DAY_BARS = 75                  # 09:15..15:25
INCOMPLETE_BARS = 70                # a day with fewer closed bars is counted as incomplete


def versions(cfg):
    """The four strategy versions compared: (key, label, strategy, cfg)."""
    hyb = strat.strategy_cfg(cfg, "EMA_ORB")
    min_or = float(hyb.get("min_or_pct") or 0.35) or 0.35

    def with_hybrid(**kw):
        return dict(cfg, strategies=dict(cfg.get("strategies") or {}, EMA_ORB=dict(hyb, **kw)))
    return [("EMA_PULLBACK", "A: Existing EMA Pullback", "EMA_PULLBACK", cfg),
            ("EMA_ORB_B", "B: EMA + ORB confirmation", "EMA_ORB", with_hybrid(min_or_pct=0.0)),
            ("EMA_ORB_C", "C: EMA + ORB + min range %.2f%%" % min_or, "EMA_ORB", with_hybrid(min_or_pct=min_or)),
            ("ORB", "ORB standalone", "ORB", cfg)]


# ---------------------------------------------------------------- costs

def cost_model(cfg, charges, lots, premiums=None, option_slippage_pct=None):
    """{symbol: {"cost_r", "charges_r", "slippage_r", "premium", "lot", "premium_stop"}} for one lot."""
    loss = float((cfg.get("exit") or {}).get("max_premium_loss_pct", 30.0))
    slip = float(DEFAULTS["option_slippage_pct"] if option_slippage_pct is None else option_slippage_pct)
    out = {}
    for sym, lot in lots.items():
        p = float((premiums or {}).get(sym) or DEFAULT_PREMIUM.get(sym, 150.0))
        stop = round(p * (1 - loss / 100), 2)
        ch = float(size.round_trip([lot], Decimal(str(p)), Decimal(str(stop)), "NFO" if sym != "SENSEX" else "BFO",
                                   charges))
        risk = (p - stop) * lot
        out[sym] = {"premium": p, "premium_stop": stop, "lot": lot, "charges_inr": round(ch, 2),
                    "charges_r": round(ch / risk, 4), "slippage_r": round(2 * slip / loss, 4),
                    "cost_r": round(ch / risk + 2 * slip / loss, 4), "option_slippage_pct": slip,
                    "max_premium_loss_pct": loss}
    return out


# ---------------------------------------------------------------- realistic exits

def realistic(t, bars, rule, cost_r, stop_slip_r):
    """The replayed trade t re-filled realistically on that day's bars, or None when it is skipped."""
    hm = "%02d:%02d" % divmod(T._mins(t["entry_time"]) - 5, 60)
    i = next((k for k, b in enumerate(bars) if b["start_ist"] == hm), None)
    if i is None or i + 1 >= len(bars):
        return None
    bull = t["direction"] == "bullish"
    flat = rule["expiry_flat_by"] if t.get("expiry_day") else rule["flat_by"]
    stop, target, planned = t["stop"], t["target"], abs(t["entry"] - t["stop"])
    fill = bars[i + 1]["open"]
    if planned <= 0 or (fill <= stop or fill >= target if bull else fill >= stop or fill <= target):
        return None
    exit_px, outcome, last = None, "flat_by", bars[i + 1]
    for b in bars[i + 1:]:
        if T._mins(b["start_ist"]) + 5 > T._mins(flat):
            break
        last = b
        if bull:
            if b["open"] <= stop:
                exit_px, outcome = b["open"], "gap_stop"
            elif b["low"] <= stop:
                exit_px, outcome = stop, "stop"
            elif b["high"] >= target:
                exit_px, outcome = target, "target"
        else:
            if b["open"] >= stop:
                exit_px, outcome = b["open"], "gap_stop"
            elif b["high"] >= stop:
                exit_px, outcome = stop, "stop"
            elif b["low"] <= target:
                exit_px, outcome = target, "target"
        if exit_px is not None:
            break
    if exit_px is None:
        exit_px = last["close"]
    gross = (exit_px - fill) * (1 if bull else -1) / planned
    net = gross - cost_r - (stop_slip_r if outcome in ("stop", "gap_stop") else 0.0)
    return dict(t, fill=fill, exit=exit_px, outcome=outcome, exit_time="%02d:%02d" % divmod(T._mins(last["start_ist"]) + 5, 60),
                r_gross=round(gross, 4), r=round(net, 4))


# ---------------------------------------------------------------- metrics

def metrics(trades, risk_inr):
    """Everything the comparison shows, from trades in time order; R and rupees (R x risk_inr)."""
    rs = [t["r"] for t in trades]
    n = len(rs)
    if not n:
        return {"trades": 0}
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r < 0]
    gp, gl = sum(wins), sum(losses)
    run_w = run_l = best_w = best_l = 0
    for r in rs:
        run_w, run_l = (run_w + 1, 0) if r > 0 else (0, run_l + 1) if r < 0 else (0, 0)
        best_w, best_l = max(best_w, run_w), max(best_l, run_l)
    st = tm.stats_of(rs)
    monthly = {}
    for t in trades:
        monthly[t["date"][:7]] = round(monthly.get(t["date"][:7], 0.0) + t["r"], 4)
    aw = gp / len(wins) if wins else 0.0
    al = gl / len(losses) if losses else 0.0
    costs = sum(t.get("r_gross", t["r"]) - t["r"] for t in trades)
    rupee = lambda x: round(x * risk_inr, 2)  # noqa: E731
    return {"trades": n, "wins": len(wins), "losses": len(losses), "win_rate": round(len(wins) / n, 4),
            "gross_profit_r": round(gp, 4), "gross_loss_r": round(gl, 4), "net_r": round(sum(rs), 4),
            "costs_r": round(costs, 4), "avg_r": round(sum(rs) / n, 4),
            "profit_factor": round(gp / -gl, 3) if gl < 0 else None, "max_drawdown_r": st["worst_drawdown_r"],
            "avg_win_r": round(aw, 4), "avg_loss_r": round(al, 4), "reward_risk": round(aw / -al, 3) if al < 0 else None,
            "max_consecutive_wins": best_w, "max_consecutive_losses": best_l,
            "gross_profit_inr": rupee(gp), "gross_loss_inr": rupee(gl), "net_inr": rupee(sum(rs)),
            "max_drawdown_inr": rupee(st["worst_drawdown_r"]),
            "monthly_r": monthly, "monthly_inr": {k: rupee(v) for k, v in monthly.items()},
            "outcomes": {k: sum(1 for t in trades if t["outcome"] == k) for k in ("target", "stop", "gap_stop", "flat_by")}}


def segment_of(dates):
    """{date: segment} over one index's trading days in order: 50 % train, 25 % validation, 25 % test."""
    n, out = len(dates), {}
    for name, a, b in SEGMENTS:
        for d in dates[int(round(a * n)):int(round(b * n))]:
            out[d] = name
    return out


def segment_ranges(dates):
    seg = segment_of(dates)
    return {name: [min(d for d in dates if seg[d] == name), max(d for d in dates if seg[d] == name)]
            for name, _, _ in SEGMENTS if any(seg[d] == name for d in dates)}


def data_quality(window):
    bars = {d: T._bars(window["bars"].get(d) or []) for d in window["dates"]}
    return {"days": len([d for d in window["dates"] if bars[d]]), "span": [window["dates"][0], window["dates"][-1]],
            "empty_days": [d for d in window["dates"] if not bars[d]],
            "incomplete_days": [d for d in window["dates"] if bars[d] and len(bars[d]) < INCOMPLETE_BARS],
            "missing_opening_range": [d for d in window["dates"] if bars[d] and
                                      len([b for b in bars[d] if b["start_ist"] < "09:30"]) < 3],
            "failed_fetch": window.get("missing") or [], "partial": window.get("partial")}


# ---------------------------------------------------------------- one version on one index

def run_version(symbol, strategy, cfg, window, expiries, cost_r, stop_slip_r):
    """(realistic trades with their segment, skipped count) for one strategy version on one index."""
    data = T.simulate(symbol, strategy, cfg, window, expiries)
    s = strat.strategy_cfg(cfg, strategy)
    rule = T._rule(cfg, s)
    days = [d for d in window["dates"] if window["bars"].get(d)][1:]   # the EMA seed day: never traded by any version
    seg = segment_of(days)
    bars = {}
    out, skipped = [], 0
    for t in data["days"]:
        if t["status"] != "trade" or t["date"] not in seg:
            continue
        if t["date"] not in bars:
            bars[t["date"]] = T._bars(window["bars"][t["date"]])
        r = realistic(t, bars[t["date"]], rule, cost_r, stop_slip_r)
        if r is None:
            skipped += 1
            continue
        r.update(symbol=symbol, segment=seg[t["date"]])
        out.append(r)
    return out, skipped, days


def _by_segment(trades, risk_inr):
    out = {"all": metrics(trades, risk_inr)}
    for name, _, _ in SEGMENTS:
        out[name] = metrics([t for t in trades if t["segment"] == name], risk_inr)
    return out


def _curve(trades):
    cum, out = 0.0, []
    for t in sorted(trades, key=lambda t: (t["date"], t["entry_time"])):
        cum = round(cum + t["r"], 4)
        out.append({"date": t["date"], "cum_r": cum})
    return out


def compare(windows, cfg, expiries, costs, risk_inr, stop_slip_r=None):
    """All four versions on every index in `windows` ({symbol: window}) and combined."""
    stop_slip_r = DEFAULTS["stop_slippage_r"] if stop_slip_r is None else float(stop_slip_r)
    vers = versions(cfg)
    result = {"versions": [{"key": k, "label": label, "strategy": st,
                            "settings": strat.strategy_cfg(c, st)} for k, label, st, c in vers],
              "by_index": {}, "combined": {}, "equity": {}, "skipped": {}, "errors": {},
              "segments": {}, "data": {}, "costs": costs, "stop_slippage_r": stop_slip_r, "risk_inr": risk_inr}
    pooled = {k: [] for k, _, _, _ in vers}
    for sym, w in windows.items():
        result["data"][sym] = data_quality(w)
        result["by_index"][sym], result["skipped"][sym] = {}, {}
        for key, _, st, c in vers:
            try:
                trades, skipped, days = run_version(sym, st, c, w, expiries.get(sym, ()), costs[sym]["cost_r"],
                                                    stop_slip_r)
            except smart.SmartError as e:
                result["errors"]["%s %s" % (sym, key)] = str(e)
                continue
            result["segments"][sym] = segment_ranges(days)
            result["by_index"][sym][key] = _by_segment(trades, risk_inr)
            result["skipped"][sym][key] = skipped
            result["equity"]["%s %s" % (sym, key)] = _curve(trades)
            pooled[key] += trades
    for key, trades in pooled.items():
        result["combined"][key] = _by_segment(sorted(trades, key=lambda t: (t["date"], t["entry_time"])), risk_inr)
        result["equity"][key] = _curve(trades)
    return result


def sweep(windows, cfg, expiries, costs, risk_inr, version="EMA_ORB_C", stop_slip_r=None):
    """Targets 1..3R x stop methods for one EMA_ORB version, all indices pooled. Chosen on TRAIN (best average
    R with at least MIN_TRAIN_TRADES), confirmed on VALIDATION (average R and profit factor above break-even);
    the TEST period is reported for the chosen setting only, never used to choose."""
    stop_slip_r = DEFAULTS["stop_slippage_r"] if stop_slip_r is None else float(stop_slip_r)
    hyb_cfg = next(c for k, _, _, c in versions(cfg) if k == version)
    rows = []
    for method in strat.STOP_METHODS:
        for target in TARGETS:
            c = dict(hyb_cfg, strategies=dict(hyb_cfg["strategies"], EMA_ORB=dict(
                hyb_cfg["strategies"]["EMA_ORB"], stop_method=method, target_r=target)))
            trades = []
            for sym, w in windows.items():
                try:
                    trades += run_version(sym, "EMA_ORB", c, w, expiries.get(sym, ()), costs[sym]["cost_r"],
                                          stop_slip_r)[0]
                except smart.SmartError:
                    continue
            trades.sort(key=lambda t: (t["date"], t["entry_time"]))
            seg = _by_segment(trades, risk_inr)
            rows.append({"stop_method": method, "target_r": target, "train": seg["train"],
                         "validation": seg["validation"], "_test": seg["test"]})
    ranked = sorted((r for r in rows if r["train"].get("trades", 0) >= MIN_TRAIN_TRADES),
                    key=lambda r: r["train"]["avg_r"], reverse=True)
    chosen = next((r for r in ranked if r["validation"].get("trades") and r["validation"]["avg_r"] > 0
                   and (r["validation"].get("profit_factor") or 0) > 1), None)
    for r in rows:
        r["chosen"] = r is chosen
        test = r.pop("_test")
        if r is chosen:
            r["test"] = test
    return {"version": version, "rows": rows, "chosen": {"stop_method": chosen["stop_method"],
                                                         "target_r": chosen["target_r"]} if chosen else None,
            "rule": "chosen on train (best average R, at least %d trades), confirmed on validation (average R > 0 "
                    "and profit factor > 1); the test period is shown for the chosen setting only" % MIN_TRAIN_TRADES,
            "costs": costs, "stop_slippage_r": stop_slip_r}


def lots_from_master(rows, symbols, today):
    """{symbol: lot size} of each index's nearest option expiry in today's master."""
    out = {}
    for sym in symbols:
        opts = [r for r in rows or [] if r.get("name") == sym and r.get("instrumenttype") == "OPTIDX"
                and smart.expiry_date(r.get("expiry"))]
        live = [r for r in opts if smart.expiry_date(r["expiry"]) >= today]
        if live:
            r = min(live, key=lambda r: smart.expiry_date(r["expiry"]))
            out[sym] = int(float(r.get("lotsize") or 0))
    return out


def finite(x):
    """JSON-safe: inf / nan never leave the API."""
    if isinstance(x, dict):
        return {k: finite(v) for k, v in x.items()}
    if isinstance(x, list):
        return [finite(v) for v in x]
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x

