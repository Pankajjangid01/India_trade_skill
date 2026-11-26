"""Paper-trading rules: the entry gate, the premium plan, exits and P&L. Pure functions.

Every trade BUYS an option (CE for bullish, PE for bearish), so P&L is always
(exit - entry) x quantity, minus Angel One's charges for both legs (in-budget's
round_trip on trader/charges.json, split at the exchange freeze limit).
"""

import datetime
from decimal import Decimal

from app.sp import smart, size

FRESH_SIGNAL_SEC = 150      # a signal is only traded if its bar closed this recently


def gate(sig, now, cfg, ctx):
    """(status, reason) before any broker call. ctx: open_trades, trades_today, strategy_trades_today,
    realized_today, losses_in_a_row, underlying_ltp, auto_execute, strategy settings `s`."""
    sess, acct = cfg.get("session") or {}, cfg.get("account") or {}
    start, cutoff = sess.get("entry_start", "09:30"), sess.get("entry_end", "14:00")
    hm = now.strftime("%H:%M")
    closed_at = smart.parse_ist("%s %s" % (sig["date"], sig["bar_close_ist"]))
    age = (now - closed_at).total_seconds()
    if age > FRESH_SIGNAL_SEC:
        if sig["bar_close_ist"] >= cutoff:
            return "ENTRY_CUTOFF", "after the %s entry cutoff (seen late, at %s)" % (cutoff, now.strftime("%H:%M:%S"))
        return "REJECTED", "stale: the bar closed at %s, first seen at %s (the engine was not watching then)" % (
            sig["bar_close_ist"], now.strftime("%H:%M:%S"))
    if hm >= cutoff:
        return "ENTRY_CUTOFF", "after the %s IST entry cutoff: recorded, no new trade" % cutoff
    if hm < start:
        return "REJECTED", "before the %s entry start" % start
    if not ctx["auto_execute"]:
        return "EXECUTABLE", "auto-execute is off: not traded"
    same = [t for t in ctx["open_trades"] if t["symbol"] == sig["symbol"]]
    if same:
        t = same[0]
        return "REJECTED", "%s: %s already has an open %s %s trade (%s)" % (
            "duplicate" if t["direction"] == sig["direction"] else "conflict", sig["symbol"], t["strategy"],
            t["direction"], t["id"])
    capital = float(acct.get("capital_inr") or 0)
    max_trades, s = int(acct.get("max_trades_per_day", 3)), ctx["s"]
    if ctx["trades_today"] >= max_trades:
        return "REJECTED", "max trades per day reached (%d)" % max_trades
    if ctx["strategy_trades_today"] >= int(s.get("max_trades_per_day", max_trades)):
        return "REJECTED", "%s max trades per day reached (%d)" % (sig["strategy"], int(s["max_trades_per_day"]))
    max_loss = capital * float(acct.get("max_daily_loss_pct", 2.0)) / 100
    if ctx["realized_today"] <= -max_loss:
        return "REJECTED", "daily loss limit hit: realised %.2f, limit -%.2f" % (ctx["realized_today"], max_loss)
    streak = int(acct.get("max_consecutive_losses", 3))
    if ctx["losses_in_a_row"] >= streak:
        return "REJECTED", "%d losses in a row today (limit %d)" % (ctx["losses_in_a_row"], streak)
    u = ctx["underlying_ltp"]
    if u is not None and (u <= sig["stop_underlying"] if sig["direction"] == "bullish" else u >= sig["stop_underlying"]):
        return "REJECTED", "signal no longer valid: %s %.2f is already through the stop %.2f" % (
            sig["symbol"], u, sig["stop_underlying"])
    return "EXECUTE", None


def premium_plan(entry, inst, cfg, target_r):
    """(premium stop, premium target): exit.max_premium_loss_pct under the entry, target_r x that risk above."""
    prem, tick = Decimal(str(entry)), Decimal(str(inst["tick_size"]))
    loss = Decimal(str((cfg.get("exit") or {}).get("max_premium_loss_pct", 30)))
    pstop = smart.round_tick(prem * (1 - loss / 100), tick, "down")
    if not 0 < pstop < prem:
        raise ValueError("premium %s too small for a %s%% stop" % (prem, loss))
    ptarget = smart.round_tick(prem + Decimal(str(target_r)) * (prem - pstop), tick)
    return float(pstop), float(ptarget)


def sizing(entry, pstop, inst, cfg, ch):
    """in-budget's size(): lots from the max loss per trade and the capital cap, charges included."""
    return size.size({"instrument": inst, "entry_premium": entry, "premium_stop": pstop}, cfg, ch)


def exit_reason(t, opt_ltp, und_ltp, hm):
    """Why this open trade must close now, or None. Premium and underlying levels, whichever first, then flat-by."""
    if opt_ltp is not None:
        if opt_ltp <= t["stop_premium"]:
            return "SL_PREMIUM"
        if opt_ltp >= t["target_premium"]:
            return "TARGET_PREMIUM"
    if und_ltp is not None:
        up = t["direction"] == "bullish"
        if (und_ltp <= t["stop_underlying"]) if up else (und_ltp >= t["stop_underlying"]):
            return "SL_UNDERLYING"
        if (und_ltp >= t["target_underlying"]) if up else (und_ltp <= t["target_underlying"]):
            return "TARGET_UNDERLYING"
    if t.get("flat_by") and hm >= t["flat_by"]:
        return "TIME_EXIT"
    return None


def no_progress(t, bars, now):
    """in-exit's no-progress exit (as in-alerts watches it): True when the first `no_progress_bars` closed
    5-minute underlying bars from the one holding the entry all closed without going beyond the entry
    (above for bullish, below for bearish). Missing bars or bars not closed yet decide nothing (False)."""
    n, level = t.get("no_progress_bars"), t.get("entry_underlying")
    if not n or level is None or not bars:
        return False
    fill = smart.parse_ist(t["entry_time"])
    m = fill.hour * 60 + fill.minute
    first = 555 + max(0, (m - 555) // 5) * 5                      # 555 = 09:15, the bars' anchor
    want = ["%02d:%02d" % divmod(first + 5 * i, 60) for i in range(int(n))]
    got = {b["start_ist"]: b for b in bars if b.get("start_ist") in want}
    up = t["direction"] == "bullish"
    if any((b["close"] > level) if up else (b["close"] < level) for b in got.values()):
        return False
    last_close = smart.parse_ist("%s %s" % (t["date"], want[-1])) + datetime.timedelta(minutes=5)
    return now >= last_close and len(got) == len(want)


def pnl(entry, exit_price, qtys, segment, ch):
    """(gross, charges, net) in rupees for a bought option sold at exit_price."""
    gross = (Decimal(str(exit_price)) - Decimal(str(entry))) * sum(qtys)
    charges = size.round_trip(qtys, Decimal(str(entry)), Decimal(str(exit_price)), segment, ch)
    return float(gross), float(charges), float(gross - charges)


def valid_price(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if x > 0 and x == x and x != float("inf") else None
