"""Tests for OI_PCR (Intraday OI Shift + PCR confirmation): the battleground walls, the trigger and its
confirmation, the range-bound trap, the mirror, the expiry roll, determinism, the engine's per-bar records
(kept across a restart) and its end-to-end scan.

All data is synthetic (controlled test data): no network, no broker.
"""

import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import engine as E, strategies as S, timemachine as T
from app.engine import Engine
from app.sp import smart
from test_logic import ROOT, bar

DAY = "2026-11-26"
EXP = "2026-12-01"
ON = dict(S.strategy_cfg({}, "OI_PCR"), enabled=True)
STRIKES = [25800.0, 25850.0, 25900.0, 25950.0, 26000.0, 26050.0, 26100.0]
# the 09:30 battleground: the call wall 26000 (most call OI), the put wall 25900 (most put OI), PCR 1.00
BASE_CE = {25800: 1e6, 25850: 1.5e6, 25900: 3e6, 25950: 5e6, 26000: 9e6, 26050: 4e6, 26100: 3.5e6}
BASE_PE = {25800: 3e6, 25850: 4e6, 25900: 8e6, 25950: 5e6, 26000: 3e6, 26050: 1.5e6, 26100: 1e6}


def rec(b, spot, ce=None, pe=None, pcr=1.0, expiry=EXP):
    """An oi_record for bar b: the battleground's OI with the given strikes changed."""
    ce, pe = {**BASE_CE, **(ce or {})}, {**BASE_PE, **(pe or {})}
    return {"bar": b, "as_of_ist": "%sT%s:02+05:30" % (DAY, S.add_min(b, 5)), "expiry": expiry, "spot": spot,
            "step": 50.0, "pcr": pcr, "pcr_api": None, "partial": None,
            "oi": [[k, int(ce[int(k)]), int(pe[int(k)])] for k in STRIKES]}


def day_bars(closes):
    """5-minute bars from 09:15, each opening at the previous close, 6 points either side."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(bar("%02d:%02d" % divmod(555 + 5 * i, 60), prev, max(prev, c) + 6, min(prev, c) - 6, c))
        prev = c
    return out


UP = day_bars([25960, 25965, 25970, 25975, 25985, 25995, 26005])     # 09:15..09:45: rises to the call wall
DOWN = day_bars([25940, 25935, 25930, 25925, 25915, 25905, 25895])   # falls to the put wall
COVER = {"ce": {26000: 9e6 * 0.96}, "pe": {26000: 3e6 * 1.08}}        # call writers -4 %, put writers +8 %


def ev(bars, snaps, **kw):
    return S.oi_pcr_evaluate("NIFTY", DAY, bars, snaps, dict(ON, **kw))


def bullish_snaps(**last):
    return {"09:25": rec("09:25", 25975), "09:35": rec("09:35", 25990, pcr=1.08),
            "09:45": rec("09:45", 26005, **{**COVER, "pcr": 1.15, **last})}


def oi_only(cfg, symbol, **kw):
    return [s for s in S.evaluate(cfg, symbol, DAY, None, UP, **kw) if s["strategy"] == "OI_PCR"]


class OiPcrRulesTest(unittest.TestCase):
    def test_defaults_disabled_and_nifty_only(self):
        d = S.strategy_cfg({}, "OI_PCR")
        self.assertEqual((d["enabled"], S.oi_symbols(d), d["pcr_bull"], d["pcr_bear"]), (False, ["NIFTY"], 1.1, 0.85))
        snaps = bullish_snaps()
        self.assertEqual(oi_only({}, "NIFTY", oi=snaps), [])                              # off by default
        cfg = {"strategies": {"OI_PCR": {"enabled": True}}}
        self.assertEqual(len(oi_only(cfg, "NIFTY", oi=snaps)), 1)
        self.assertEqual(oi_only(cfg, "SENSEX", oi=snaps), [])                            # not in its symbols
        self.assertEqual(oi_only(cfg, "NIFTY"), [])                                       # no records: skipped
        orb = [s["strategy"] for s in S.evaluate(cfg, "NIFTY", DAY, None, UP, oi=snaps)]
        self.assertIn("ORB", orb)                                                         # the others unchanged
        self.assertEqual(S.oi_symbols({"symbols": " nifty, SENSEX ,foo"}), ["NIFTY", "SENSEX"])

    def test_oi_record_from_an_in_chain_snapshot(self):
        chain = {"as_of_ist": DAY + "T09:30:03+05:30", "expiry": EXP, "spot": 25975.0, "step": 50.0, "pcr_oi": 0.97,
                 "pcr_api": {"pcr": 1.02}, "partial_note": None,
                 "rows": [{"strike": 26000.0, "type": "CE", "oi": 900}, {"strike": 25950.0, "type": "PE", "oi": 500},
                          {"strike": 26000.0, "type": "PE", "oi": 300}, {"strike": 25950.0, "type": "CE", "oi": None}]}
        r = S.oi_record(chain, "09:25")
        self.assertEqual(r["oi"], [[25950.0, None, 500], [26000.0, 900, 300]])
        self.assertEqual((r["bar"], r["pcr"], r["pcr_api"], r["expiry"]), ("09:25", 0.97, 1.02, EXP))
        self.assertEqual(json.loads(json.dumps(r)), r)                                  # one JSON line on disk

    def test_a_recorded_in_chain_snapshot_makes_a_record(self):
        """The contract with in-chain: a real snapshot line (the skill pack's fixture) gives in-chain's own top OI."""
        path = os.path.join(ROOT, "skillpack", "tests", "fixtures", "in-chain", "nifty", "history-1005.jsonl")
        with open(path, encoding="utf-8") as fh:
            doc = json.loads(fh.readline())
        r = S.oi_record(doc, "10:00")
        self.assertEqual(S.oi_walls(r), (doc["top_oi"]["CE"][0], doc["top_oi"]["PE"][0]))
        self.assertEqual((r["pcr"], r["expiry"], r["step"]), (doc["pcr_oi"], doc["expiry"], doc["step"]))

    def test_walls_from_the_battleground(self):
        self.assertEqual(S.oi_walls(rec("09:25", 25975)), (26000.0, 25900.0))
        tie = rec("09:25", 26030, ce={26050: 9e6})                                       # 26000 and 26050 tie
        self.assertEqual(S.oi_walls(tie)[0], 26050.0)                                    # nearer the spot
        _, st = ev(UP, bullish_snaps())
        b = st["baseline"]
        self.assertEqual((b["call_wall"], b["put_wall"], b["opening"], b["bar"]), (26000.0, 25900.0, "balanced", "09:25"))

    def test_bullish_short_covering_with_pcr_rising(self):
        sigs, st = ev(UP, bullish_snaps())
        self.assertEqual([s["id"] for s in sigs], ["2026-11-26:OI_PCR:NIFTY:09:45:bullish"])
        s = sigs[0]
        self.assertEqual(s["entry_underlying"], 26005)
        self.assertEqual(s["stop_underlying"], min(b["low"] for b in UP[-3:]))           # swing low of 3 bars
        self.assertAlmostEqual(s["target_underlying"] - 26005, 2 * (26005 - s["stop_underlying"]), delta=0.1)
        self.assertEqual((s["oi"]["wall"], s["oi"]["own_change_pct"], s["oi"]["opposite_change_pct"]),
                         (26000.0, -4.0, 8.0))
        self.assertEqual((s["oi"]["pcr"], s["oi"]["pcr_prev"]), (1.15, 1.08))
        self.assertIn("call writers covering at 26000", s["note"])
        self.assertTrue(st["decisions"][-1]["accepted"])
        self.assertEqual(st["now"]["call_wall_ce_pct"], -4.0)

    def test_put_surge_one_strike_below_counts(self):
        sigs, _ = ev(UP, bullish_snaps(pe={26000: 3e6, 25950: 5e6 * 1.06}))
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["oi"]["opposite_change_pct"], 6.0)

    def test_range_bound_trap_is_no_trade(self):
        sigs, st = ev(UP, bullish_snaps(ce={26000: 9e6 * 1.03}))                         # call writers ADD
        self.assertEqual(sigs, [])
        d = st["decisions"][-1]
        self.assertFalse(d["accepted"])
        self.assertIn("range-bound trap", d["reason"])

    def test_the_other_side_must_step_in(self):
        sigs, st = ev(UP, bullish_snaps(pe={26000: 3e6 * 1.02}))                         # put OI +2 % only
        self.assertEqual(sigs, [])
        self.assertIn("put OI +2.0%, under +5.0%", st["decisions"][-1]["reason"])

    def test_pcr_must_be_above_and_rising(self):
        self.assertEqual(ev(UP, bullish_snaps(pcr=1.05))[0], [])                          # under 1.10
        snaps = bullish_snaps()
        snaps["09:35"] = rec("09:35", 25990, pcr=1.2)                                     # 1.15 < 1.20: falling
        sigs, st = ev(UP, snaps)
        self.assertEqual(sigs, [])
        self.assertIn("PCR 1.15 from 1.20", st["decisions"][-1]["reason"])

    def test_bearish_mirror(self):
        snaps = {"09:25": rec("09:25", 25935), "09:35": rec("09:35", 25915, pcr=0.88),
                 "09:45": rec("09:45", 25895, ce={25950: 5e6 * 1.1}, pe={25900: 8e6 * 0.95}, pcr=0.8)}
        sigs, _ = ev(DOWN, snaps)
        self.assertEqual([s["direction"] for s in sigs], ["bearish"])
        s = sigs[0]
        self.assertEqual(s["stop_underlying"], max(b["high"] for b in DOWN[-3:]))
        self.assertLess(s["target_underlying"], s["entry_underlying"])
        self.assertEqual((s["oi"]["wall"], s["oi"]["own_change_pct"], s["oi"]["opposite_change_pct"]),
                         (25900.0, -5.0, 10.0))

    def test_no_battleground_no_signal(self):
        snaps = bullish_snaps()
        del snaps["09:25"]
        sigs, st = ev(UP, snaps)
        self.assertEqual(sigs, [])
        self.assertIn("no chain record for the 09:25 bar", st["note"])

    def test_no_signal_once_the_chain_rolls_to_another_expiry(self):
        sigs, st = ev(UP, bullish_snaps(expiry="2026-12-08"))
        self.assertEqual(sigs, [])
        self.assertIn("rolled to the 2026-12-08 expiry", st["decisions"][-1]["reason"])

    def test_risk_band(self):
        sigs, st = ev(UP, bullish_snaps(), max_risk_pct=0.01)
        self.assertEqual(sigs, [])
        self.assertIn("stop risk", st["decisions"][-1]["reason"])

    def test_one_signal_per_direction_and_deterministic(self):
        bars = day_bars([25960, 25965, 25970, 25975, 25985, 25995, 26005, 26015])
        snaps = dict(bullish_snaps(), **{"09:50": rec("09:50", 26015, ce={26000: 8e6}, pe={26000: 3.5e6}, pcr=1.2)})
        first = ev(bars, snaps)[0]
        self.assertEqual([s["bar_start_ist"] for s in first], ["09:45"])
        self.assertEqual(ev(bars, snaps)[0], first)
        self.assertEqual(ev(bars[:-1], snaps)[0], first)                                  # same as live at 09:50


class OiPcrReplayTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_time_machine_cannot_replay_it_and_says_so(self):
        window = {"dates": [], "bars": {}, "missing": [], "partial": None}
        doc = T.check(self.dir, "NIFTY", "OI_PCR", {}, window)
        self.assertTrue(doc["ok"])                                                        # required is false
        self.assertIn("cannot be replayed", doc["reason"])
        self.assertFalse(T.check(self.dir, "NIFTY", "OI_PCR", {"timemachine": {"required": True}}, window)["ok"])


class OiPcrEngineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "trader", "config.json"), os.path.join(self.dir, "config.json"))
        self.cfg = smart.load_config(self.dir)
        self.cfg["strategies"] = {"OI_PCR": {"enabled": True}, "ORB": {"enabled": False},
                                  "EMA_PULLBACK": {"enabled": False}, "EMA_ORB": {"enabled": False}}
        smart.write_json(os.path.join(self.dir, "config.json"), self.cfg)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def engine(self):
        e = Engine(trader=self.dir)
        e.day, e.client = DAY, object()
        e.facts["NIFTY"] = {"token": "99926000", "segment": "NSE", "expiry_today": False, "expiries": []}
        e.prev_bars["NIFTY"] = []
        return e

    def chain_doc(self):
        return {"as_of_ist": DAY + "T09:30:03+05:30", "expiry": EXP, "spot": 25975.0, "step": 50.0, "pcr_oi": 1.0,
                "pcr_api": None, "partial_note": None,
                "rows": [{"strike": k, "type": t, "oi": int((BASE_CE if t == "CE" else BASE_PE)[int(k)])}
                         for k in STRIKES for t in ("CE", "PE")]}

    def test_one_record_per_bar_kept_across_a_restart(self):
        now = datetime.datetime(2026, 11, 26, 9, 30, 3, tzinfo=smart.IST)
        calls = []
        fake = lambda a, p, c, n: (calls.append(c["options"]["strikes_each_side"]) or self.chain_doc(), {})  # noqa: E731
        e = self.engine()
        try:
            with mock.patch.object(E.chain, "take_snapshot", fake):
                e.oi_records("NIFTY", "09:25", now, self.cfg)
                recs = e.oi_records("NIFTY", "09:25", now, self.cfg)                      # same bar: no 2nd call
            self.assertEqual(calls, [20])                                                 # its own strikes_each_side
            self.assertEqual(S.oi_walls(recs["09:25"]), (26000.0, 25900.0))
            self.assertIsNone(e.oi_records("SENSEX", "09:25", now, self.cfg))             # not one of its symbols
        finally:
            e.store.db.close()
        e2 = self.engine()                                                                # a restart
        try:
            boom = mock.Mock(side_effect=AssertionError("no new snapshot for a recorded bar"))
            with mock.patch.object(E.chain, "take_snapshot", boom):
                self.assertIn("09:25", e2.oi_records("NIFTY", "09:25", now, self.cfg))
            for err in (smart.SmartError("quote 403"), KeyError("rows")):                # failure: no record,
                with mock.patch.object(E.chain, "take_snapshot", mock.Mock(side_effect=err)):  # never a raise
                    self.assertNotIn("09:30", e2.oi_records("NIFTY", "09:30", now, self.cfg))
        finally:
            e2.store.db.close()

    def test_the_engine_scan_records_the_signal_with_its_oi_evidence(self):
        path = os.path.join(self.dir, "state", "oi", "NIFTY_%s.jsonl" % DAY)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for r in bullish_snaps().values():
                fh.write(json.dumps(r) + "\n")
        now = datetime.datetime(2026, 11, 26, 9, 50, 4, tzinfo=smart.IST)                # the 09:45 bar closed
        e = self.engine()
        try:
            with mock.patch.object(smart, "now_ist", lambda pinned=None: now), \
                    mock.patch.object(smart, "day_bars", lambda *a, **k: {"bars": UP, "source": "test", "partial": None}), \
                    mock.patch.object(E.chain, "take_snapshot", mock.Mock(side_effect=AssertionError("recorded"))), \
                    mock.patch.object(e, "execute") as ex, mock.patch.object(e, "update_market"), \
                    mock.patch.object(e, "monitor"), mock.patch.object(e, "timemachine_verdict",
                                                                       lambda *a: {"ok": True, "reason": "x", "data": None}):
                e.scan(now, self.cfg)
            sigs = e.store.signals(DAY)
            self.assertEqual([s["id"] for s in sigs], ["2026-11-26:OI_PCR:NIFTY:09:45:bullish"])
            self.assertEqual(sigs[0]["meta"]["oi"]["wall"], 26000.0)
            self.assertTrue(ex.called)                                                    # the gate passed it on
            v = e.market["NIFTY"]["oi_pcr"]
            self.assertEqual((v["enabled"], v["traded"], v["baseline"]["call_wall"]), (True, True, 26000.0))
            self.assertTrue(v["decisions"][-1]["accepted"])
        finally:
            e.store.db.close()


class OiPcrSettingsTest(unittest.TestCase):
    def test_settings(self):
        from fastapi import HTTPException
        from app.main import clean_strategy
        self.assertEqual(clean_strategy("OI_PCR", {"symbols": "nifty, sensex", "min_unwind_pct": 0}),
                         {"symbols": "NIFTY,SENSEX", "min_unwind_pct": 0.0})
        for bad in ({"symbols": "NIFTY,FOO"}, {"symbols": " "}, {"pcr_bull": 0}, {"start_after": "9:30"}):
            with self.assertRaises(HTTPException):
                clean_strategy("OI_PCR", bad)


if __name__ == "__main__":
    unittest.main()
