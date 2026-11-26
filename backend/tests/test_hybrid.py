"""Tests for the EMA_ORB hybrid: its conditions, its risk rule, the research layer (realistic fills, metrics,
chronological split, sweep) and that the live engine and the backtest produce the same signals.

All data is synthetic (controlled test data): no network, no broker.
"""

import datetime
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import paper, research as R, strategies as S, timemachine as T
from app.engine import Engine
from app.sp import smart
from test_logic import CFG, MASTER_ROWS, ROOT, at, bar, ctx, series

DAY = "2026-11-26"
PREV = series("09:15", [24800.0 + i * 3 for i in range(75)], 6)                 # yesterday: steady uptrend
TODAY = series("09:15", [25030.0 + i * 8 for i in range(10)] + [25088.0, 25074.0, 25070.0, 25085.0, 25093.0], 6)
WIDE = [dict(b) for b in TODAY]                                                   # same day, a 0.40 % range
WIDE[0]["low"], WIDE[1]["high"] = 24960.0, 25060.0
ON = dict(S.strategy_cfg({}, "EMA_ORB"), enabled=True)


def ev(bars, **kw):
    return S.ema_orb_evaluate("NIFTY", DAY, PREV, bars, dict(ON, **kw))


class HybridConditionsTest(unittest.TestCase):
    def test_defaults_are_strategy_c_and_disabled(self):
        d = S.strategy_cfg({}, "EMA_ORB")
        self.assertEqual((d["enabled"], d["min_or_pct"], d["stop_method"], d["max_trades_per_day"],
                          d["stop_after_first_sl"]), (False, 0.35, "ema", 2, True))
        self.assertFalse([s for s in S.evaluate({}, "NIFTY", DAY, PREV, WIDE) if s["strategy"] == "EMA_ORB"])

    def test_opening_range_and_its_reference_price(self):
        rng = S.opening_range(WIDE)
        self.assertEqual((rng["high"], rng["low"], rng["ref"]), (25060.0, 24960.0, WIDE[0]["open"]))
        self.assertAlmostEqual(rng["pct"], (25060.0 - 24960.0) / WIDE[0]["open"] * 100, places=4)
        self.assertFalse(S.opening_range(WIDE[:2])["complete"])

    def test_strategy_b_accepts_after_a_breakout(self):
        sigs, st = ev(TODAY, min_or_pct=0.0)
        self.assertEqual([s["id"] for s in sigs], ["2026-11-26:EMA_ORB:NIFTY:10:20:bullish"])
        self.assertEqual(st["breakout"]["bullish"], "09:30")
        s = sigs[0]
        self.assertGreater(s["entry_underlying"], s["opening_range"]["high"])
        self.assertLess(s["stop_underlying"], s["entry_underlying"])
        self.assertAlmostEqual(s["target_underlying"] - s["entry_underlying"],
                               2 * (s["entry_underlying"] - s["stop_underlying"]), delta=0.1)

    def test_strategy_c_skips_a_narrow_range(self):
        sigs, st = ev(TODAY)                                          # 0.09 % < 0.35 %
        self.assertEqual(sigs, [])
        self.assertIn("under min_or_pct", st["setups"][0]["reason"])
        self.assertEqual(len(ev(WIDE)[0]), 1)                         # 0.40 %: taken
        self.assertEqual(ev(WIDE, min_or_pct=0.5)[0], [])             # configurable

    def test_no_trade_inside_the_range_or_before_the_breakout(self):
        high = [dict(b) for b in WIDE]
        for b in high[:3]:
            b["high"] = 25200.0                                       # the range high above every later close
        sigs, st = ev(high)
        self.assertEqual(sigs, [])
        self.assertTrue(st["setups"][0]["reason"].startswith("no close above"))
        self.assertIsNone(st["breakout"]["bullish"])

    def test_bearish_mirror(self):
        flip = lambda bars: [bar(b["start_ist"], 50000 - b["open"], 50000 - b["low"], 50000 - b["high"],  # noqa: E731
                                 50000 - b["close"]) for b in bars]
        sigs, st = S.ema_orb_evaluate("NIFTY", DAY, flip(PREV), flip(WIDE), ON)
        self.assertEqual([(s["direction"], s["bar_start_ist"]) for s in sigs], [("bearish", "10:20")])
        self.assertLess(sigs[0]["entry_underlying"], sigs[0]["opening_range"]["low"])

    def test_stop_methods_and_the_risk_band(self):
        stops = {m: ev(WIDE, stop_method=m, max_risk_pct=5)[0][0]["stop_underlying"] for m in S.STOP_METHODS}
        self.assertEqual(stops["ema"], ev(WIDE)[0][0]["stop_underlying"])      # the EMA setup's own stop
        swing = min(b["low"] for b in WIDE[13 - 9:14])
        self.assertEqual(stops["swing"], S._round(swing, "down"))
        atr = S.atr14(WIDE[:14])
        self.assertEqual(stops["atr"], S._round(25085.0 - 1.5 * atr, "down"))
        sigs, st = ev(WIDE, stop_method="swing", max_risk_pct=0.1)            # a wider stop never qualifies
        self.assertEqual(sigs, [])
        self.assertIn("outside", st["setups"][0]["reason"])

    def test_deterministic_and_existing_strategies_unchanged(self):
        self.assertEqual(ev(WIDE)[0], ev(WIDE)[0])
        cfg_on = {"strategies": {"EMA_ORB": {"enabled": True}}}
        base = [s for s in S.evaluate({}, "NIFTY", DAY, PREV, WIDE)]
        with_h = [s for s in S.evaluate(cfg_on, "NIFTY", DAY, PREV, WIDE) if s["strategy"] != "EMA_ORB"]
        self.assertEqual(base, with_h)
        self.assertEqual(S.ema_pullback_signals("NIFTY", DAY, PREV, WIDE, S.strategy_cfg({}, "EMA_PULLBACK"))[0]["id"],
                         "2026-11-26:EMA_PULLBACK:NIFTY:10:20:bullish")


class HybridRiskRuleTest(unittest.TestCase):
    SIG = {"id": "h", "strategy": "EMA_ORB", "symbol": "NIFTY", "date": DAY, "direction": "bullish",
           "bar_start_ist": "10:20", "bar_close_ist": "10:25", "entry_underlying": 25900.0, "stop_underlying": 25850.0,
           "target_underlying": 26000.0, "risk_points": 50.0}

    def test_first_stop_loss_ends_the_day(self):
        s = S.strategy_cfg(CFG, "EMA_ORB")
        for first, want in (("SL_UNDERLYING", "REJECTED"), ("SL_PREMIUM", "REJECTED"), ("TARGET_PREMIUM", "EXECUTE"),
                            (None, "EXECUTE")):
            st, why = paper.gate(self.SIG, at("10:25", 5), CFG, ctx(s=s, strategy_first_exit=first))
            self.assertEqual(st, want, (first, why))
        st, _ = paper.gate(self.SIG, at("10:25", 5), CFG, ctx(s=s, strategy_trades_today=2))
        self.assertEqual(st, "REJECTED")                                  # max 2 a day
        st, _ = paper.gate(dict(self.SIG, bar_close_ist="14:00"), at("14:00", 5), CFG, ctx(s=s))
        self.assertEqual(st, "ENTRY_CUTOFF")                              # no new entry from 14:00
        ema = S.strategy_cfg(CFG, "EMA_PULLBACK")                         # the existing strategy: no such rule
        st, _ = paper.gate(dict(self.SIG, strategy="EMA_PULLBACK"), at("10:25", 5), CFG,
                           ctx(s=ema, strategy_first_exit="SL_UNDERLYING"))
        self.assertEqual(st, "EXECUTE")

    def test_the_replay_keeps_the_same_rule(self):
        rule = T._rule({}, ON)
        mk = lambda t: {"id": t, "direction": "bullish", "bar_start_ist": t, "bar_close_ist": S.add_min(t, 5),  # noqa
                        "entry_underlying": 100.0, "stop_underlying": 99.0, "target_underlying": 102.0}
        bars = [bar("%02d:%02d" % divmod(m, 60), 100, 100.2, 99.8, 100) for m in range(9 * 60 + 15, 15 * 60 + 30, 5)]
        bars[5] = bar(bars[5]["start_ist"], 100, 100, 98, 98.5)          # 09:40: the first trade's stop
        out = T._sequence([mk("09:30"), mk("10:30"), mk("11:30")], DAY, bars, dict(ON, max_trades_per_day=2), rule,
                          False)
        self.assertEqual([t["outcome"] for t in out], ["stop"])           # no second trade after a first SL
        bars[17] = bar(bars[17]["start_ist"], 100, 103, 99.9, 102.5)    # 10:40: the 10:30 trade's target
        bars[29] = bar(bars[29]["start_ist"], 100, 103, 99.9, 102.5)    # 11:40: the 11:30 trade's target
        out = T._sequence([mk("10:30"), mk("11:30"), mk("12:30")], DAY, bars, dict(ON, stop_after_first_sl=False),
                          rule, False)
        self.assertEqual(len(out), 2)                                     # max_trades_per_day 2


def multi_day(n=25, start=datetime.date(2026, 10, 1)):
    """n weekdays of {date: getCandleData rows}: each day a wide range, a breakout and the EMA pullback."""
    out, d, base = {}, start, 25000.0
    while len(out) < n:
        if d.weekday() < 5:
            closes = ([base + 30 + i * 8 for i in range(10)] + [base + 88, base + 74, base + 70, base + 85, base + 93]
                      + [base + 100 + i * 6 for i in range(60)])
            bars = series("09:15", closes, 6)
            bars[0]["low"], bars[1]["high"] = base * 0.9975, base * 1.0015      # a 0.40 % range
            out[d.isoformat()] = [["%sT%s:00+05:30" % (d, b["start_ist"]), b["open"], b["high"], b["low"], b["close"], 0]
                                  for b in bars]
            base = closes[-1] - 70
        d += datetime.timedelta(1)
    return out


class LiveBacktestConsistencyTest(unittest.TestCase):
    """The engine's scan and the backtest replay call the same strategy function: same signals, same ids."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "trader", "config.json"), os.path.join(self.dir, "config.json"))
        self.cfg = smart.load_config(self.dir)
        self.cfg["strategies"] = {"EMA_ORB": {"enabled": True}, "ORB": {"enabled": False},
                                  "EMA_PULLBACK": {"enabled": False}}
        smart.write_json(os.path.join(self.dir, "config.json"), self.cfg)
        self.rows = multi_day()
        self.dates = sorted(self.rows)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_same_signals_live_and_in_the_replay(self):
        window = {"dates": self.dates, "bars": self.rows, "missing": [], "partial": None}
        data = T.simulate("NIFTY", "EMA_ORB", self.cfg, window)
        replay_ids = [t["signal_id"] for t in data["days"] if t["status"] == "trade"]
        self.assertTrue(replay_ids)
        live_ids = []
        for prev, d in zip(self.dates, self.dates[1:]):
            sigs = S.evaluate(self.cfg, "NIFTY", d, T._bars(self.rows[prev]), T._bars(self.rows[d]))
            live_ids += [s["id"] for s in sigs]
        self.assertTrue(set(replay_ids) <= set(live_ids))
        first_per_day = {}
        for i in live_ids:
            first_per_day.setdefault(i[:10], i)
        self.assertEqual(sorted(i for i in replay_ids if first_per_day.get(i[:10]) == i), sorted(first_per_day.values()))

    def test_the_engine_scan_records_the_replays_signal(self):
        d, prev = self.dates[-1], self.dates[-2]
        e = Engine(trader=self.dir)
        try:
            y, m, dd = (int(x) for x in d.split("-"))
            now = datetime.datetime(y, m, dd, 10, 25, 5, tzinfo=smart.IST)
            e.day, e.client = d, object()
            e.facts["NIFTY"] = {"token": "99926000", "segment": "NSE", "expiry_today": False, "expiries": []}
            e.prev_bars["NIFTY"] = T._bars(self.rows[prev])
            bars = [b for b in T._bars(self.rows[d]) if b["start_ist"] <= "10:20"]
            with mock.patch.object(smart, "now_ist", lambda pinned=None: now), \
                    mock.patch.object(smart, "day_bars", lambda *a, **k: {"bars": bars, "source": "test", "partial": None}), \
                    mock.patch.object(e, "execute"), mock.patch.object(e, "update_market"), \
                    mock.patch.object(e, "monitor"), mock.patch.object(e, "timemachine_verdict",
                                                                       lambda *a: {"ok": True, "reason": "x", "data": None}):
                e.scan(now, self.cfg)
            live = [s["id"] for s in e.store.signals(d)]
            window = {"dates": [prev, d], "bars": {prev: self.rows[prev], d: self.rows[d]}, "missing": [], "partial": None}
            sigs = S.ema_orb_signals("NIFTY", d, T._bars(self.rows[prev]), T._bars(self.rows[d]), S.strategy_cfg(self.cfg, "EMA_ORB"))
            self.assertEqual(sorted(live), sorted(s["id"] for s in sigs if s["bar_start_ist"] <= "10:20"))
            self.assertTrue(live)
            h = e.market["NIFTY"]["hybrid"]
            self.assertTrue(h["range_ok"])
            self.assertTrue(h["breakout"].startswith("above the high"))
            self.assertEqual(h["ema"]["trend"], "bullish")
            self.assertTrue(h["setups"][-1]["accepted"])
            _ = window
        finally:
            e.store.db.close()


class ResearchTest(unittest.TestCase):
    RULE = T._rule({}, ON)

    def day(self):
        return [bar("%02d:%02d" % divmod(m, 60), 100, 100.2, 99.8, 100) for m in range(9 * 60 + 15, 15 * 60 + 30, 5)]

    def trade(self, **kw):
        return dict({"date": DAY, "direction": "bullish", "entry_time": "10:00", "entry": 100.0, "stop": 99.0,
                     "target": 102.0, "expiry_day": False, "r": 0.0, "outcome": "flat_by"}, **kw)

    def test_next_open_entry_gap_stop_and_costs(self):
        bars = self.day()
        bars[9] = bar("10:00", 100.1, 100.3, 100.0, 100.1)                # the fill bar opens at 100.1
        bars[10] = bar("10:05", 98.5, 98.8, 98.2, 98.4)                  # opens through the 99 stop: a gap
        r = R.realistic(self.trade(), bars, self.RULE, 0.06, 0.05)
        self.assertEqual((r["fill"], r["exit"], r["outcome"]), (100.1, 98.5, "gap_stop"))
        self.assertAlmostEqual(r["r"], (98.5 - 100.1) / 1.0 - 0.06 - 0.05, places=4)   # worse than -1R
        bars[10] = bar("10:05", 100.1, 102.5, 100.0, 102.2)
        r = R.realistic(self.trade(), bars, self.RULE, 0.06, 0.05)
        self.assertEqual((r["outcome"], r["exit"]), ("target", 102.0))
        self.assertAlmostEqual(r["r"], (102.0 - 100.1) - 0.06, places=4)
        bars[9] = bar("10:00", 98.9, 99.0, 98.8, 98.9)                    # next open already through the stop
        self.assertIsNone(R.realistic(self.trade(), bars, self.RULE, 0.06, 0.05))

    def test_metrics(self):
        ts = [self.trade(r=x, date="2026-0%d-01" % (1 + i // 3)) for i, x in enumerate([2.0, -1.0, -1.0, -1.0, 2.0, 2.0])]
        m = R.metrics(ts, 5000.0)
        self.assertEqual((m["trades"], m["wins"], m["losses"], m["max_consecutive_losses"], m["max_consecutive_wins"]),
                         (6, 3, 3, 3, 2))
        self.assertEqual((m["gross_profit_r"], m["gross_loss_r"], m["net_r"], m["profit_factor"], m["reward_risk"]),
                         (6.0, -3.0, 3.0, 2.0, 2.0))
        self.assertEqual(m["max_drawdown_r"], 3.0)
        self.assertEqual(m["net_inr"], 15000.0)
        self.assertEqual(m["monthly_r"], {"2026-01": 0.0, "2026-02": 3.0})

    def test_chronological_split(self):
        dates = ["2026-01-%02d" % i for i in range(1, 21)]
        seg = R.segment_of(dates)
        self.assertEqual([seg[d] for d in dates], ["train"] * 10 + ["validation"] * 5 + ["test"] * 5)

    def test_costs(self):
        ch = smart.read_json(os.path.join(ROOT, "skillpack", "in-setup", "charges.example.json"))
        c = R.cost_model(CFG, ch, {"NIFTY": 65})["NIFTY"]
        self.assertAlmostEqual(c["slippage_r"], 2 * 0.5 / 30, places=4)
        self.assertGreater(c["charges_r"], 0)
        self.assertAlmostEqual(c["cost_r"], c["charges_r"] + c["slippage_r"], places=3)

    def test_sweep_shows_the_test_period_only_for_the_chosen_setting(self):
        rows = multi_day(60)
        window = {"dates": sorted(rows), "bars": rows, "missing": [], "partial": None}
        costs = {"NIFTY": {"cost_r": 0.06}}
        out = R.sweep({"NIFTY": window}, CFG, {}, costs, 5000.0, "EMA_ORB_C")
        self.assertEqual(len(out["rows"]), len(R.TARGETS) * len(S.STOP_METHODS))
        for r in out["rows"]:
            self.assertEqual("test" in r, r["chosen"])
        self.assertLessEqual(sum(r["chosen"] for r in out["rows"]), 1)

    def test_compare_has_every_version_index_and_segment(self):
        rows = multi_day(40)
        window = {"dates": sorted(rows), "bars": rows, "missing": [], "partial": None}
        out = R.compare({"NIFTY": window}, CFG, {}, {"NIFTY": {"cost_r": 0.06}}, 5000.0)
        self.assertEqual([v["key"] for v in out["versions"]], ["EMA_PULLBACK", "EMA_ORB_B", "EMA_ORB_C", "ORB"])
        for k in ("EMA_ORB_B", "EMA_ORB_C"):
            self.assertEqual(set(out["combined"][k]), {"all", "train", "validation", "test"})
        self.assertGreater(out["combined"]["EMA_ORB_C"]["all"]["trades"], 0)
        self.assertEqual(out["data"]["NIFTY"]["empty_days"], [])


class SettingsValidationTest(unittest.TestCase):
    def test_hybrid_settings(self):
        from fastapi import HTTPException
        from app.main import clean_strategy
        self.assertEqual(clean_strategy("EMA_ORB", {"min_or_pct": 0, "stop_method": "atr"}),
                         {"min_or_pct": 0.0, "stop_method": "atr"})
        for bad in ({"min_or_pct": -1}, {"stop_method": "magic"}, {"target_r": 0}, {"nope": 1}):
            with self.assertRaises(HTTPException):
                clean_strategy("EMA_ORB", bad)
        with self.assertRaises(HTTPException):
            clean_strategy("EMA_PULLBACK", {"min_or_pct": 0.2})          # the existing strategy has no such setting


if __name__ == "__main__":
    unittest.main()
