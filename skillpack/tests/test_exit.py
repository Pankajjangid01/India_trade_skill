"""in-exit tests, all offline, with hand-checked numbers.

entry.json and pick.json are written by hand in a temp folder, shaped as
CONTRACTS.md says in-entry and in-chain write them. tests/fixtures/in-exit
holds a synthetic master slice (the NIFTY index and four options) and one
getCandleData reply shaped like the documented one (today's 5-minute bars),
used only by the atr:K stop. No real data.
"""

import io
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

ex = load("in-exit", "exit")
smart = ex.smart
OFF = os.path.join(FIX, "in-exit")
DAY = "2026-11-25"

ENTRY = {"created_by": "in-entry", "symbol": "NIFTY", "date": DAY, "status": "triggered", "direction": "bullish",
         "trigger_level": 23550.0, "bar_start_ist": "09:35", "bar_close_ist": "09:40", "bar_close": 23562.5,
         "opening_range": {"high": 23550.0, "low": 23480.0, "start": "09:15", "end": "09:30"},
         "filters_applied": [], "notes": [], "feed_lag_ms": 300, "source": "feed", "as_of_ist": DAY + "T09:41:00+05:30"}
CE = {"tradingsymbol": "NIFTY01DEC2623500CE", "token": "45100", "segment": "NFO", "underlying": "NIFTY",
      "strike": 23500.0, "type": "CE", "expiry": "2026-12-01", "expiry_raw": "01DEC2026", "lot_size": 65,
      "tick_size": 0.05, "freeze_qty": 1801}
PICK = {"created_by": "in-chain", "underlying": "NIFTY", "direction": "bullish", "as_of_ist": DAY + "T09:41:30+05:30",
        "instrument": CE, "premium": 112.4, "bid": 112.35, "ask": 112.45, "delta": 0.52, "greeks_source": "API",
        "one_lot_cost_inr": 7306.0, "rule": "delta 0.35 to 0.55, spread under 3%", "next_best": {}, "source": "feed"}
# the bearish day: the 09:30 bar closed 23,070 under the range 23,080 to 23,160
BEAR_ENTRY = dict(ENTRY, direction="bearish", trigger_level=23080.0, bar_start_ist="09:30", bar_close_ist="09:35",
                  bar_close=23070.0, opening_range={"high": 23160.0, "low": 23080.0, "start": "09:15", "end": "09:30"})
PE = dict(CE, tradingsymbol="NIFTY01DEC2623100PE", token="45201", strike=23100.0, type="PE")
BEAR_PICK = dict(PICK, direction="bearish", instrument=PE, premium=98.35)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ex.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def skill_example(heading):
    with open(os.path.join(ROOT, "in-exit", "SKILL.md"), encoding="utf-8") as fh:
        return re.search(re.escape(heading) + r".*?```text\n(.*?)```", fh.read(), re.S).group(1)


class InTemp(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        self.cfg = smart.read_json(os.path.join(ROOT, "in-setup", "config.example.json"))
        smart.write_json(os.path.join("trader", "config.json"), self.cfg)
        self.write(ENTRY, PICK)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, entry, pick):
        for name, obj in (("entry", entry), ("pick", pick)):
            path = os.path.join("trader", name + ".json")
            if obj is None:
                os.remove(path)
            else:
                smart.write_json(path, obj)

    def plan(self, *extra, hm="09:42"):
        code, out, err = run("plan", "--write", "--now", "%s %s" % (DAY, hm), *extra)
        return code, out, err, smart.read_json(os.path.join("trader", "plan.json"))

    def levels(self, plan):
        return tuple(plan[k] for k in ("underlying_stop", "target_underlying", "premium_stop", "target_premium",
                                       "risk_per_unit_inr", "r_value_inr"))


class Stops(InTemp):
    def test_the_example_in_skill_md(self):
        code, out, err, _ = self.plan()
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: the bullish NIFTY plan"))

    def test_or_mid_is_the_default_and_fills_the_contract(self):
        _, _, _, p = self.plan()
        # stop (23,550 + 23,480) / 2 = 23,515.00; risk 47.50; target 23,562.50 + 2 x 47.50 = 23,657.50
        # premium 112.40 x 0.70 = 78.68, down to the tick 78.65; risk 33.75; target 112.40 + 67.50 = 179.90
        # one lot: 33.75 x 65 = 2,193.75
        self.assertEqual(self.levels(p), (23515.0, 23657.5, 78.65, 179.9, 33.75, 2193.75))
        self.assertEqual((p["underlying_stop_rule"], p["premium_stop_rule"]), ("or_mid", "max_premium_loss_pct 30"))
        self.assertEqual((p["flat_by"], p["expiry_day"], p["no_progress_bars"], p["tick_size"]),
                         ("15:10", False, 6, 0.05))
        self.assertEqual((p["entry_underlying"], p["entry_premium"], p["target_r"]), (23562.5, 112.4, 2.0))
        self.assertEqual(p["instrument"], CE)
        self.assertEqual(p["source"], "entry.json + pick.json")

    def test_or_opposite(self):
        _, _, _, p = self.plan("--stop", "or-opposite")
        # stop at the range low 23,480.00; risk 82.50; target 23,562.50 + 165.00 = 23,727.50
        self.assertEqual(self.levels(p)[:2], (23480.0, 23727.5))
        self.assertEqual(p["underlying_stop_rule"], "or_opposite")

    def test_pct_rounds_away_from_the_entry(self):
        _, out, _, p = self.plan("--stop", "pct:0.2")
        # 23,562.50 x 0.998 = 23,515.375, down to 23,515.35; risk 47.15; target 23,562.50 + 94.30 = 23,656.80
        self.assertEqual(self.levels(p)[:2], (23515.35, 23656.8))
        self.assertIn("stop: 0.2% under the entry = 23,515.375, rounded down to 0.05", out)

    def test_atr_from_candles_with_fewer_than_14_bars(self):
        code, out, err, p = self.plan("--stop", "atr:1.5", "--offline", OFF)
        self.assertEqual(code, 0, err)
        # true ranges 09:15 to 09:35: 50, 45, 45, 53, 45; ATR 238 / 5 = 47.60; 1.5 x 47.60 = 71.40
        # stop 23,562.50 - 71.40 = 23,491.10; target 23,562.50 + 142.80 = 23,705.30
        self.assertEqual(self.levels(p)[:2], (23491.1, 23705.3))
        self.assertIn("stop: 1.5 x ATR 47.60 (the last 5 of today's 5-minute bars up to the entry bar, fewer than 14; "
                      "candles) = 23,491.10, rounded down to 0.05", out)
        self.assertIn("API usage: none (offline replay)", out)

    def test_bearish_or_mid_rounds_up(self):
        self.write(BEAR_ENTRY, BEAR_PICK)
        _, out, _, p = self.plan()
        # stop (23,160 + 23,080) / 2 = 23,120.00; risk 50.00; target 23,070 - 100 = 22,970.00
        # premium 98.35 x 0.70 = 68.845, down to 68.80; risk 29.55; target 98.35 + 59.10 = 157.45; lot 1,920.75
        self.assertEqual(self.levels(p), (23120.0, 22970.0, 68.8, 157.45, 29.55, 1920.75))
        self.assertIn("out if NIFTY has not closed below 23,070.00 by 10:05", out)

    def test_entry_premium_override(self):
        _, out, _, p = self.plan("--entry-premium", "120")
        # 120 x 0.70 = 84.00; risk 36.00; target 120 + 72 = 192.00; one lot 2,340.00
        self.assertEqual(self.levels(p)[2:], (84.0, 192.0, 36.0, 2340.0))
        self.assertIn("premium 120.00 from --entry-premium", out)

    def test_expiry_day_flat_by_is_earlier(self):
        self.write(ENTRY, dict(PICK, instrument=dict(CE, expiry=DAY)))
        _, out, _, p = self.plan()
        self.assertEqual((p["flat_by"], p["expiry_day"]), ("14:45", True))
        self.assertIn("14:45 IST on expiry day, win or lose", out)

    def test_show_prints_the_saved_plan(self):
        self.plan()
        code, out, _ = run("show", "--now", DAY + " 10:00")
        self.assertEqual(code, 0)
        self.assertIn("in-exit plan saved 09:42: NIFTY bullish, buy NIFTY01DEC2623500CE", out)
        self.assertIn("within 6 five-minute bars of the entry", out)
        code, out, _ = run("show", "--now", "2026-11-26 10:00")
        self.assertIn("this plan is for 2026-11-25, not today", out)

    def test_without_write_nothing_is_saved(self):
        code, out, _ = run("plan", "--now", DAY + " 09:42")
        self.assertEqual(code, 0)
        self.assertIn("Not saved: add --write to save trader/plan.json", out)
        self.assertFalse(os.path.exists(os.path.join("trader", "plan.json")))


class Refusals(InTemp):
    def refused(self, *extra, hm="09:42"):
        code, out, err, p = self.plan(*extra, hm=hm)
        self.assertEqual((code, out, p), (1, "", None))
        self.assertTrue(err.startswith("in-exit: ") and err.count("\n") == 1, err)
        return err[9:].strip()

    def test_no_trigger_today(self):
        self.write(dict(ENTRY, status="waiting"), PICK)
        self.assertIn("in-exit plans only today's trigger", self.refused())
        self.write(dict(ENTRY, date="2026-11-24"), PICK)
        self.assertIn("in-exit plans only today's trigger", self.refused())

    def test_missing_stale_or_mismatched_pick_names_the_next_step(self):
        self.write(ENTRY, None)
        self.assertEqual(self.refused(), "no trader/pick.json: run in-chain pick for a NIFTY CE first")
        self.write(ENTRY, dict(PICK, as_of_ist=DAY + "T09:38:00+05:30"))
        self.assertEqual(self.refused(), "pick.json is from 2026-11-25T09:38:00+05:30, before the 09:40 trigger: "
                                         "run in-chain pick for a NIFTY CE again")
        self.write(ENTRY, dict(PICK, underlying="SENSEX"))
        self.assertIn("pick.json is a SENSEX bullish CE, the entry is NIFTY bullish", self.refused())
        self.write(ENTRY, dict(PICK, direction="bearish", instrument=PE))
        self.assertIn("pick.json is a NIFTY bearish PE", self.refused())

    def test_wrong_side_and_zero_risk(self):
        self.assertIn("is on the wrong side of the entry 23,562.50: a bullish stop goes below it",
                      self.refused("--stop", "pct:-0.5"))
        self.assertIn("equals the entry: zero risk", self.refused("--stop", "atr:0", "--offline", OFF))
        self.assertIn("--stop is or-mid, or-opposite, atr:K or pct:X", self.refused("--stop", "pct:abc"))
        self.cfg["exit"]["max_premium_loss_pct"] = 0
        smart.write_json(os.path.join("trader", "config.json"), self.cfg)
        self.assertIn("is not below the entry premium 112.4: zero risk", self.refused())

    def test_at_or_after_flat_by(self):
        self.assertEqual(self.refused(hm="15:10"),
                         "it is 15:10, at or after the flat-by time 15:10: no new trade today")
        self.write(ENTRY, dict(PICK, instrument=dict(CE, expiry=DAY)))
        self.assertEqual(self.refused(hm="14:50"), "it is 14:50, at or after the flat-by time 14:45 (expiry day): "
                                                   "no new trade today")

    def test_show_without_a_plan(self):
        code, out, err = run("show", "--now", DAY + " 10:00")
        self.assertEqual((code, err), (1, "in-exit: no trader/plan.json: run in-exit plan --write\n"))


if __name__ == "__main__":
    unittest.main()
