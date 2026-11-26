"""Tests for LORENTZIAN (Machine Learning: Lorentzian Classification): the Pine ta.* conventions, the labels, the
approximate-nearest-neighbor search against a literal transcription of the Pine loop, no look-ahead / no
repainting, the signal state machine and both exits (incl. the source's unreachable branch), the kernels, the
volume rules, the backtest's fills and comparison, the engine's signals and exits, the settings.

All data is synthetic (controlled test data): no network, no broker.
"""

import datetime
import math
import os
import random
import shutil
import tempfile
import unittest
from unittest import mock

from app import engine as E, lorentzian as L, strategies as S, timemachine as T
from app.engine import Engine
from app.sp import smart
from test_logic import ROOT

DAY, PREV = "2026-11-26", "2026-11-25"
NAN = float("nan")
DEF = S.strategy_cfg({}, "LORENTZIAN")
OFF = dict(DEF, use_volatility_filter=False, use_regime_filter=False, use_kernel_filter=False,
           use_volume_filter=False)


def walk(n, seed=3, start=25000.0, step=12.0, date=PREV):
    """A random walk of 5-minute bars with futures volume and candles (synthetic)."""
    rnd, px, out = random.Random(seed), start, []
    for i in range(n):
        o = px
        c = o + rnd.gauss(0, step)
        out.append({"date": date, "start_ist": "%02d:%02d" % divmod(555 + 5 * (i % 75), 60), "open": o,
                    "high": max(o, c) + abs(rnd.gauss(0, 5)), "low": min(o, c) - abs(rnd.gauss(0, 5)), "close": c,
                    "volume": float(rnd.randint(4000, 40000)), "vol_open": o, "vol_close": c})
        px = c
    return out


class FixedAnn(L.Ann):
    """Predictions chosen by the test, to drive the state machine."""

    def __init__(self, preds):
        super().__init__(8, 2000)
        self.fixed = [float(p) for p in preds]

    def run(self, rows, labels):
        return self.fixed[:len(rows)], [8] * len(rows)


def flat_bars(n, date=DAY, vol=10000.0):
    out, px = [], 25000.0
    for i in range(n):
        out.append({"date": date, "start_ist": "%02d:%02d" % divmod(555 + 5 * i, 60), "open": px, "high": px + 8,
                    "low": px - 8, "close": px + 2, "volume": vol, "vol_open": px, "vol_close": px + 2})
        px += 2
    return out


def pine_ann(rows, labels, k, mbb):
    """The Core ML Logic, transcribed line by line from the Pine source (maxBarsBackIndex = 0)."""
    predictions, distances, y_train, out, prediction = [], [], [], [], 0.0
    for bar_index in range(len(rows)):
        y_train.append(labels[bar_index])
        lastDistance = -1.0
        size = min(mbb - 1, len(y_train) - 1)
        sizeLoop = min(mbb - 1, size)
        for i in range(0, sizeLoop + 1):
            d = sum(math.log(1 + abs(a - b)) for a, b in zip(rows[bar_index], rows[i]))
            if d >= lastDistance and i % 4:
                lastDistance = d
                distances.append(d)
                predictions.append(round(y_train[i]))
                if len(predictions) > k:
                    lastDistance = distances[math.floor(k * 3 / 4 + 0.5)]   # Pine math.round: half up
                    distances.pop(0)
                    predictions.pop(0)
        prediction = float(sum(predictions))
        out.append(prediction)
    return out


class PineSeriesTest(unittest.TestCase):
    def test_sma_ema_rma_follow_pine(self):
        xs = [float(x) for x in range(1, 11)]
        self.assertTrue(all(math.isnan(v) for v in L.sma(xs, 3)[:2]))
        self.assertEqual(L.sma(xs, 3)[2:4], [2.0, 3.0])
        e = L.ema(xs, 3)                                      # seeded with the SMA, then alpha 0.5
        self.assertTrue(math.isnan(e[1]))
        self.assertEqual((e[2], e[3]), (2.0, 3.0))
        self.assertEqual(L.rma(xs, 1), xs)                     # ATR(1) is the true range itself
        self.assertEqual(L.rsi(xs, 3)[-1], 100.0)              # only gains
        self.assertEqual(L.rsi(xs[::-1], 3)[-1], 0.0)          # only losses
        self.assertAlmostEqual(L.cci([float(x) for x in range(1, 21)], 20)[-1], (20 - 10.5) / (0.015 * 5), places=9)

    def test_normalize_uses_the_running_min_max_only(self):
        self.assertEqual(L.normalize([5.0, 10.0, 0.0]), [0.0, 1.0, 0.0])
        xs = [math.sin(i / 3.0) * (i + 1) for i in range(80)]
        for k in (10, 40, 79):
            self.assertEqual(L.normalize(xs[:k]), L.normalize(xs)[:k])      # a later bar never changes an earlier one
        self.assertEqual(L.rescale([0.0, 50.0, 100.0], 0, 100), [0.0, 0.5, 1.0])

    def test_kernels(self):
        self.assertAlmostEqual(L.rational_quadratic([1.0, 2.0, 3.0, 4.0], 1, 1.0, 1)[2],
                               (3 * 1 + 2 * (1 / 1.5) + 1 * (1 / 3)) / (1 + 1 / 1.5 + 1 / 3), places=12)
        k = L.rational_quadratic([100.0] * 40, 8, 8.0, 25)
        self.assertTrue(all(math.isnan(v) for v in k[:26]) and all(abs(v - 100) < 1e-9 for v in k[26:]))
        g = L.gaussian([100.0] * 40, 6, 25)
        self.assertTrue(math.isnan(g[25]) and abs(g[26] - 100) < 1e-9)

    def test_pine_round_is_half_up(self):
        self.assertEqual((L.pine_round(4.5), L.pine_round(3.75), L.pine_round(6.0)), (5, 4, 6))
        self.assertEqual(round(4.5), 4)                        # why Python's round is not used


class AnnTest(unittest.TestCase):
    def setUp(self):
        rnd = random.Random(11)
        self.rows = [(NAN,) * 3 if i < 9 else tuple(rnd.random() for _ in range(3)) for i in range(260)]
        self.labels = [rnd.choice((-1, 0, 1)) for _ in range(260)]

    def test_matches_the_pine_loop_line_by_line(self):
        for k in (8, 5, 6, 1):
            ann = L.Ann(k, 120)
            got, used = ann.run(self.rows, self.labels)
            self.assertEqual(got, pine_ann(self.rows, self.labels, k, 120), "k=%d" % k)
            self.assertTrue(max(used) <= k)

    def test_incremental_equals_one_build_and_a_changed_past_rebuilds(self):
        ann = L.Ann(8, 120)
        ann.run(self.rows[:150], self.labels[:150])
        self.assertEqual(ann.run(self.rows, self.labels)[0], pine_ann(self.rows, self.labels, 8, 120))
        rows = list(self.rows)
        rows[20] = (0.5, 0.5, 0.5)                             # history corrected: the model rebuilds
        self.assertEqual(ann.run(rows, self.labels)[0], pine_ann(rows, self.labels, 8, 120))

    def test_neighbors_are_the_first_max_bars_back_bars_never_multiples_of_4(self):
        rows = [(0.0,)] * 4 + [(1.0,)] * 4                     # bar 4 matches bar 7 best, but 4 % 4 == 0
        labels = [1, 1, 1, 1, -1, 1, 1, 1]
        ann = L.Ann(1, 4)                                       # only bars 0..3 can ever be neighbors
        got, _ = ann.run(rows, labels)
        self.assertEqual(got[-1], 1.0)                          # bars 1..3 (label +1), never bar 4 (-1)

    def test_labels_are_the_past_four_bar_move_inverted(self):
        up = [dict(b, close=25000.0 + 10 * i, high=25005.0 + 10 * i, low=24995.0 + 10 * i, open=25000.0 + 10 * i)
              for i, b in enumerate(flat_bars(12))]
        recs = L.run(up, OFF)
        self.assertEqual([r["label"] for r in recs], [0, 0, 0, 0] + [-1] * 8)   # src[4] < src[0] -> short


class ModelTest(unittest.TestCase):
    def test_no_look_ahead_no_repainting(self):
        bars = walk(560)
        s = dict(DEF, max_bars_back=300, use_rvol_feature=True)
        full = L.run(bars, s)
        for k in (320, 450, 559):
            self.assertEqual(L.run(bars[:k], s), full[:k], "prefix %d" % k)
        self.assertEqual(len(full[-1]["features"]), 6)
        self.assertEqual(L.feature_names(s)[-1], "RVOL(20)")

    def test_entry_then_the_fixed_four_bar_exit(self):
        recs = L.run(flat_bars(40), OFF, FixedAnn([0] * 30 + [5] * 10))
        self.assertEqual([i for i, r in enumerate(recs) if r["start_long"]], [30])
        self.assertEqual([i for i, r in enumerate(recs) if r["end_long"]], [34])
        self.assertEqual(L.exit_bar(recs, 30, True, OFF), (34, "LC_EXIT_4_BARS"))
        self.assertEqual((recs[30]["prediction"], recs[30]["direction"], recs[30]["signal"]), (5.0, "bullish", 1))

    def test_a_flip_within_four_bars_and_the_unreachable_branch(self):
        recs = L.run(flat_bars(40), OFF, FixedAnn([0] * 30 + [5, 5] + [-5] * 8))
        self.assertEqual([i for i, r in enumerate(recs) if r["start_short"]], [32])
        self.assertFalse(any(r["end_long"] for r in recs))     # the source's "held < 4 and new sell" never fires
        self.assertEqual([i for i, r in enumerate(recs) if r["end_short"]], [36])
        self.assertEqual(L.exit_bar(recs, 30, True, OFF), (32, "LC_EXIT_FLIP"))
        self.assertEqual(L.exit_bar(recs, 30, True, dict(OFF, exit_on_flip=False)), (None, None))

    def test_filters_hold_the_signal(self):
        preds = [0] * 30 + [5] * 10
        down = [dict(b, open=25000.0 - 3 * i, high=25002.0 - 3 * i, low=24995.0 - 3 * i, close=24997.0 - 3 * i)
                for i, b in enumerate(flat_bars(40))]                   # falling: every close under its EMA
        recs = L.run(down, dict(OFF, use_ema_filter=True, ema_period=5), FixedAnn(preds))
        self.assertFalse(any(r["start_long"] for r in recs))   # close under the EMA: no buy
        vol_on = L.run(flat_bars(40), dict(OFF, use_volatility_filter=True), FixedAnn(preds))
        self.assertTrue(all(r["signal"] == 0 for r in vol_on))  # equal ranges: ATR(1) never above ATR(10)

    def test_dynamic_exits_only_without_ema_sma_or_smoothing(self):
        bars = flat_bars(40)
        self.assertEqual(L.run(bars, dict(OFF, use_dynamic_exits=True))[-1]["exit_mode"], "dynamic")
        for k in ("use_ema_filter", "use_sma_filter", "kernel_smoothing"):
            self.assertEqual(L.run(bars, dict(OFF, use_dynamic_exits=True, **{k: True}))[-1]["exit_mode"], "fixed")

    def test_volume_confirmation(self):
        s = dict(OFF, use_volume_filter=True)
        preds = [0] * 30 + [5] * 10
        bars = flat_bars(40)
        bars[30] = dict(bars[30], volume=40000.0)                              # RVOL ~3.5 on a bullish candle
        r = L.run(bars, s, FixedAnn(preds))[30]
        self.assertTrue(r["confirmed_long"] and r["rvol"] > 1.2 and r["volume_reject_long"] is None)
        low = L.run(flat_bars(40), s, FixedAnn(preds))[30]                     # RVOL 1.0
        self.assertFalse(low["confirmed_long"])
        self.assertEqual(low["volume_reject_long"], "RVOL 1.00 under 1.20")
        red = [dict(b) for b in bars]
        red[30].update(vol_open=25100.0, vol_close=25050.0)                    # the futures candle fell
        self.assertEqual(L.run(red, s, FixedAnn(preds))[30]["volume_reject_long"],
                         "volume on a bearish futures candle")
        none = [dict(b, volume=None) for b in bars]
        self.assertEqual(L.run(none, s, FixedAnn(preds))[30]["volume_reject_long"], "no futures volume for this bar")
        self.assertTrue(L.run(flat_bars(40), OFF, FixedAnn(preds))[30]["confirmed_long"])   # filter off

    def test_paper_signals_today_only_with_a_bounded_stop(self):
        bars = flat_bars(30, PREV) + flat_bars(10, DAY)
        bars[30] = dict(bars[30])
        recs = L.run(bars, OFF, FixedAnn([0] * 30 + [5] * 10))
        sigs = S.lorentzian_signals("NIFTY", DAY, recs, OFF)
        self.assertEqual([x["id"] for x in sigs], ["2026-11-26:LORENTZIAN:NIFTY:09:15:bullish"])
        risk = sigs[0]["entry_underlying"] - sigs[0]["stop_underlying"]
        self.assertTrue(0.0005 * 25000 - 0.1 <= risk <= 0.006 * 25100)
        self.assertEqual(sigs[0]["lorentzian"]["prediction"], 5.0)
        cfg = {"strategies": {"LORENTZIAN": dict(OFF, enabled=True)}, "paper": {}}
        self.assertEqual([x["strategy"] for x in S.evaluate(cfg, "NIFTY", DAY, None, [], lc=recs)
                          if x["strategy"] == "LORENTZIAN"], ["LORENTZIAN"])
        self.assertFalse([x for x in S.evaluate(cfg, "SENSEX", DAY, None, [], lc=recs) if x["strategy"] == "LORENTZIAN"])


def rec(hm, date=DAY, **kw):
    """A hand-made record for the fills: flat bars at 25000 rising 2 points a bar."""
    t = (int(hm[:2]) * 60 + int(hm[3:]) - 555) // 5
    px = 25000.0 + 2 * t
    r = {"date": date, "start_ist": hm, "open": px, "high": px + 4, "low": px - 4, "close": px + 2,
         "start_long": False, "start_short": False, "confirmed_long": False, "confirmed_short": False,
         "end_long": False, "end_short": False, "exit_mode": "fixed", "signal": 0, "signal_change": False,
         "prediction": 0.0, "neighbors": 8, "kernel": px, "kernel2": px, "kernel_dir": "bullish", "rvol": 1.5,
         "features": [0.5] * 5, "color": 0.0, "volume": 1000.0, "volume_reject_long": None,
         "volume_reject_short": None, "direction": None}
    r.update(kw)
    return r


def a_day(date=DAY):
    out = [rec("%02d:%02d" % divmod(555 + 5 * i, 60), date) for i in range(75)]
    out[5].update(start_long=True, confirmed_long=True, signal=1, signal_change=True, prediction=6.0)     # 09:40
    out[7].update(start_long=True, confirmed_long=False, volume_reject_long="RVOL 0.80 under 1.20")      # busy
    out[9].update(end_long=True)                                                                         # 10:00
    out[12].update(start_short=True, confirmed_short=False, volume_reject_short="RVOL 0.90 under 1.20")  # 10:15
    out[58].update(start_long=True, confirmed_long=True)                                                 # 14:05
    return out


class BacktestTest(unittest.TestCase):
    RULE = T._rule({}, DEF)

    def test_fills_like_the_research_layer(self):
        ts = L.day_trades(a_day(), DAY, OFF, True, self.RULE, 0.1, 0.05, 3, False, "NIFTY")
        self.assertEqual(len(ts), 1)                            # the 09:50 one is busy, the 14:05 one is late
        t = ts[0]
        self.assertEqual((t["fill"], t["exit"], t["outcome"], t["exit_reason"], t["exit_time"]),
                         (25012.0, 25020.0, "signal_exit", "LC_EXIT_4_BARS", "10:05"))
        risk = t["entry"] - t["stop"]
        self.assertAlmostEqual(t["r"], (25020.0 - 25012.0) / risk - 0.1, places=3)
        no_filter = L.day_trades(a_day(), DAY, OFF, False, self.RULE, 0.1, 0.05, 3, False, "NIFTY")
        self.assertEqual([x["direction"] for x in no_filter], ["bullish", "bearish"])   # 10:15 short unfiltered

    def test_compare_four_configs_on_the_same_days(self):
        days = [("2026-11-%02d" % d, None) for d in (17, 18, 19, 20, 23, 24, 25, 26)]
        model = lambda d, bars, ss: a_day(d)                    # noqa: E731
        res = L.compare(days, OFF, self.RULE, 0.1, 0.05, 1000.0, 3, set(), "NIFTY", model=model)
        self.assertEqual([c["key"] for c in res["configs"]], ["LC", "LC_VOLUME_FILTER", "LC_RVOL_FEATURE", "LC_BOTH"])
        self.assertEqual(res["by_config"]["LC_VOLUME_FILTER"]["all"]["trades"], 8)
        self.assertEqual(res["by_config"]["LC"]["all"]["trades"], 16)
        for k in ("win_rate", "net_inr", "profit_factor", "avg_r", "max_drawdown_r", "trades"):
            self.assertIn(k, res["by_config"]["LC"]["all"])
        self.assertEqual(set(res["segments"]), {"train", "validation", "test"})
        self.assertEqual(len(res["verdict"]), 3)
        self.assertTrue(all("too few trades" in v for v in res["verdict"]))         # 2-4 a period: no verdict
        many = [("2026-%02d-%02d" % (m, d), None) for m in (6, 7, 8, 9, 10, 11) for d in range(1, 29)]
        res = L.compare(many, OFF, self.RULE, 0.1, 0.05, 1000.0, 3, set(), "NIFTY", model=model)
        self.assertTrue(all("too few" not in v for v in res["verdict"]))           # 40+ a period: compared

    def test_dataset_joins_the_futures_volume(self):
        idx = {PREV: [["%sT09:%02d:00+05:30" % (PREV, m), 1, 2, 0.5, 1.5, 0] for m in (15, 20, 25)],
               DAY: [["%sT09:15:00+05:30" % DAY, 2, 3, 1.5, 2.5, 0]]}
        fut = {PREV: [["%sT09:20:00+05:30" % PREV, 10, 11, 9, 10.5, 777]]}
        bars = L.dataset(idx, fut, DAY, 2)
        self.assertEqual([(b["date"], b["start_ist"], b["volume"]) for b in bars],
                         [(PREV, "09:20", 777.0), (PREV, "09:25", None), (DAY, "09:15", None)])

    def test_the_time_machine_points_to_the_tab(self):
        d = tempfile.mkdtemp()
        try:
            doc = T.check(d, "NIFTY", "LORENTZIAN", {}, {"dates": [], "bars": {}, "missing": [], "partial": None})
            self.assertTrue(doc["ok"])
            self.assertIn("backtested on its own tab", doc["reason"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "trader", "config.json"), os.path.join(self.dir, "config.json"))
        self.cfg = smart.load_config(self.dir)
        self.cfg["strategies"] = {"LORENTZIAN": {"enabled": True}, "ORB": {"enabled": False},
                                  "EMA_PULLBACK": {"enabled": False}, "EMA_ORB": {"enabled": False}}
        smart.write_json(os.path.join(self.dir, "config.json"), self.cfg)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def engine(self):
        e = Engine(trader=self.dir)
        e.day, e.client = DAY, object()
        e.facts["NIFTY"] = {"token": "99926000", "segment": "NSE", "expiry_today": False, "expiries": [],
                            "fut": {"token": "777", "segment": "NFO", "symbol": "NIFTY29DEC26FUT", "expiry": "2026-12-29"}}
        e.prev_bars["NIFTY"] = []
        return e

    def test_scan_records_the_signal_and_the_indicator_exit_closes_the_trade(self):
        day = a_day()[:6]                                       # 09:15..09:40 closed: the 09:40 entry is fresh
        now = datetime.datetime(2026, 11, 26, 9, 45, 4, tzinfo=smart.IST)
        bars = [{k: r[k] for k in ("start_ist", "open", "high", "low", "close")} for r in day]
        e = self.engine()
        try:
            with mock.patch.object(smart, "now_ist", lambda pinned=None: now), \
                    mock.patch.object(smart, "day_bars", lambda *a, **k: {"bars": bars, "source": "test", "partial": None}), \
                    mock.patch.object(e, "lc_history", lambda *a, **k: []), \
                    mock.patch.object(E.lorentzian, "run", lambda b, s, ann=None: day), \
                    mock.patch.object(e, "execute") as ex, mock.patch.object(e, "update_market"), \
                    mock.patch.object(e, "monitor"), mock.patch.object(e, "timemachine_verdict",
                                                                       lambda *a: {"ok": True, "reason": "x", "data": None}):
                e.scan(now, self.cfg)
            sigs = e.store.signals(DAY)
            self.assertEqual([x["id"] for x in sigs], ["2026-11-26:LORENTZIAN:NIFTY:09:40:bullish"])
            self.assertEqual(sigs[0]["meta"]["lorentzian"]["prediction"], 6.0)
            self.assertTrue(ex.called)
            self.assertEqual(e.market["NIFTY"]["lorentzian"]["prediction"], 6.0)      # the latest bar's
            view = e.lc_view("NIFTY")
            self.assertEqual(view["decisions"][0]["direction"], "bullish")
            self.assertTrue(view["decisions"][0]["accepted"])
            self.assertEqual(view["data"]["futures"], "NIFTY29DEC26FUT")

            t = {"id": "P20261126-001", "signal_id": sigs[0]["id"], "date": DAY, "strategy": "LORENTZIAN",
                 "symbol": "NIFTY", "direction": "bullish", "tradingsymbol": "NIFTY01DEC2625000CE", "token": "999",
                 "segment": "NFO", "instrument": {}, "lots": 1, "quantity": 65, "order_qtys": [65],
                 "entry_price": 150.0, "entry_time": now.isoformat(), "entry_underlying": 25012.0,
                 "stop_premium": 105.0, "target_premium": 285.0, "stop_underlying": 24990.0,
                 "target_underlying": 25080.0, "flat_by": "15:10", "status": "OPEN"}
            e.store.open_trade(t, sigs[0]["id"])
            later = datetime.datetime(2026, 11, 26, 10, 5, 3, tzinfo=smart.IST)
            with mock.patch.object(e, "prices", lambda *a: {"999": (171.0, "feed", 0.2)}):
                e.lc_exits("NIFTY", a_day()[:9], later, self.cfg)       # the 10:00 bar not closed yet
                self.assertEqual(e.store.trade(t["id"])["status"], "OPEN")
                e.lc_exits("NIFTY", a_day()[:10], later, self.cfg)
            done = e.store.trade(t["id"])
            self.assertEqual((done["status"], done["exit_reason"], done["exit_price"]), ("CLOSED", "LC_EXIT_4_BARS", 171.0))
        finally:
            e.store.db.close()

    def test_a_failure_skips_only_this_strategy(self):
        e = self.engine()
        try:
            with mock.patch.object(e, "lc_history", mock.Mock(side_effect=smart.SmartError("candles 403"))):
                self.assertIsNone(e.lc_records("NIFTY", [], datetime.datetime(2026, 11, 26, 9, 45, tzinfo=smart.IST),
                                               self.cfg))
            self.assertIsNone(e.lc_records("SENSEX", [], None, self.cfg))                  # not one of its symbols
        finally:
            e.store.db.close()


class SettingsTest(unittest.TestCase):
    def test_settings(self):
        from fastapi import HTTPException
        from app.main import clean_strategy
        self.assertEqual(clean_strategy("LORENTZIAN", {"regime_threshold": -0.1, "f1": "wt", "source": "HLC3",
                                                       "adx_threshold": 0, "use_volume_filter": False}),
                         {"regime_threshold": -0.1, "f1": "WT", "source": "hlc3", "adx_threshold": 0,
                          "use_volume_filter": False})
        for bad in ({"regime_threshold": -11}, {"f2": "MACD"}, {"feature_count": 6}, {"source": "vwap"},
                    {"neighbors_count": 0}, {"min_rvol": -1}):
            with self.assertRaises(HTTPException, msg=str(bad)):
                clean_strategy("LORENTZIAN", bad)
        d = S.strategy_cfg({}, "LORENTZIAN")
        self.assertEqual((d["enabled"], d["neighbors_count"], d["max_bars_back"], d["feature_count"], d["source"],
                          d["regime_threshold"], d["use_volume_filter"], d["min_rvol"], d["use_rvol_feature"]),
                         (False, 8, 2000, 5, "hlc3", -0.1, True, 1.2, False))
        self.assertEqual(L.feature_names(d), ["RSI(14,1)", "WT(10,11)", "CCI(20,1)", "ADX(20,2)", "RSI(9,1)"])


if __name__ == "__main__":
    unittest.main()
