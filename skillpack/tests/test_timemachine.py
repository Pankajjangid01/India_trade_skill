"""in-timemachine tests, all offline.

The candles are synthetic: tests/fixtures/in-timemachine/candles.py writes a
seeded random walk shaped like getCandleData's documented reply ([time, open,
high, low, close, volume]), replayed through smart.Replay. master.json there is
a hand-made slice shaped like OpenAPIScripMaster.json. No real data, no secrets.
"""

import io
import json
import os
import random
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

tm = load("in-timemachine", "timemachine")
candles = load(os.path.join("tests", "fixtures", "in-timemachine"), "candles")
smart = tm.smart
NOW = "2026-11-24 18:00"   # a Tuesday after the close: the window is the 60 trading days before it
PLAN = {"created_by": "in-exit", "symbol": "NIFTY", "date": "2026-11-24", "direction": "bullish",   # CONTRACTS.md
        "instrument": {"tradingsymbol": "NIFTY01DEC2623500CE", "token": "45014", "segment": "NFO", "underlying": "NIFTY",
                       "strike": 23500.0, "type": "CE", "expiry": "2026-12-01", "expiry_raw": "01DEC2026",
                       "lot_size": 65, "tick_size": 0.05, "freeze_qty": 1801},
        "entry_underlying": 23562.5, "entry_premium": 112.4, "underlying_stop": 23515.0,
        "underlying_stop_rule": "or_mid", "premium_stop": 78.7, "premium_stop_rule": "max_premium_loss_pct 30",
        "target_r": 2.0, "target_underlying": 23657.5, "target_premium": 179.8, "risk_per_unit_inr": 33.7,
        "r_value_inr": 2190.5, "flat_by": "15:10", "expiry_day": False, "no_progress_bars": 6, "tick_size": 0.05,
        "as_of_ist": "2026-11-24T09:40:05+05:30", "source": "entry.json + pick.json"}
RULE = {"range_start": "09:15", "range_end": "09:30", "entry_start": "09:30", "entry_end": "11:30", "flat_by": "15:10",
        "expiry_last_entry": "11:00", "expiry_flat_by": "14:45", "stop": "or_mid", "stop_k": None, "target_r": 2.0}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


def skill_example(heading):
    """The ```text block under `heading` in in-timemachine/SKILL.md."""
    with open(os.path.join(ROOT, "in-timemachine", "SKILL.md"), encoding="utf-8") as fh:
        return re.search(re.escape(heading) + r".*?```text\n(.*?)```", fh.read(), re.S).group(1)


def day(moves):
    """A day of 5-minute bars 09:15 to 15:25 from {"HH:MM": (high, low, close)}, each bar opening at the
    last close; a time not given is a flat bar at the last close."""
    out, last = [], 100.0
    for m in range(9 * 60 + 15, 15 * 60 + 30, 5):
        t = "%02d:%02d" % divmod(m, 60)
        h, low, c = moves.get(t, (last, last, last))
        out.append({"start_ist": t, "open": last, "high": h, "low": low, "close": c})
        last = c
    return out


RANGE = {"09:15": (101.0, 99.5, 100.5), "09:20": (100.8, 99.0, 100.0), "09:25": (100.6, 99.8, 100.2)}  # 101 / 99


class OneDay(unittest.TestCase):
    """Hand-checked days: range 101 to 99, midpoint stop 100, 2R target."""

    def test_bullish_target(self):
        t = tm.replay_day(day(dict(RANGE, **{"09:30": (101.6, 100.1, 101.5), "09:35": (103.0, 101.2, 102.8),
                                             "09:40": (104.6, 102.5, 104.4)})), RULE)
        # entry 101.5 at the 09:35 close, stop 100, risk 1.5, target 101.5 + 2 x 1.5 = 104.5, hit in the 09:40 bar
        self.assertEqual((t["direction"], t["entry_time"], t["entry"], t["stop"], t["target"]),
                         ("bullish", "09:35", 101.5, 100.0, 104.5))
        self.assertEqual((t["outcome"], t["exit_time"], t["exit"], t["r_gross"]), ("target", "09:45", 104.5, 2.0))

    def test_bearish_stop_when_one_bar_touches_both(self):
        t = tm.replay_day(day(dict(RANGE, **{"09:30": (100.0, 98.8, 98.9), "09:35": (100.2, 96.5, 97.0)})), RULE)
        # entry 98.9, stop 100, target 98.9 - 2 x 1.1 = 96.7: the 09:35 bar reaches both, the stop counts
        self.assertEqual((t["direction"], t["entry"], t["stop"], t["target"]), ("bearish", 98.9, 100.0, 96.7))
        self.assertEqual((t["outcome"], t["exit"], t["r_gross"]), ("stop", 100.0, -1.0))

    def test_flat_by_and_the_expiry_day(self):
        bars = day(dict(RANGE, **{"09:30": (101.6, 100.1, 101.5), "09:35": (102.2, 101.2, 102.0),
                                  "14:40": (102.0, 101.7, 101.8), "15:05": (102.3, 101.9, 102.25),
                                  "15:10": (105.0, 102.0, 104.9)}))   # the 15:10 bar is after flat-by: never seen
        t = tm.replay_day(bars, RULE)
        self.assertEqual((t["outcome"], t["exit_time"], t["exit"], t["r_gross"]), ("flat_by", "15:10", 102.25, 0.5))
        t = tm.replay_day(bars, RULE, expiry=True)                    # out at 14:45 on an expiry day
        self.assertEqual((t["outcome"], t["exit_time"], t["exit"], t["r_gross"]), ("flat_by", "14:45", 101.8, 0.2))

    def test_no_trigger(self):
        self.assertEqual(tm.replay_day(day(dict(RANGE, **{"11:30": (101.6, 100.1, 101.5)})), RULE),
                         {"status": "no_trigger"})                     # the 11:30 bar closes after 11:30
        late = day(dict(RANGE, **{"11:05": (101.6, 100.1, 101.5)}))    # closes 11:10: in the window, not on expiry
        self.assertEqual(tm.replay_day(late, RULE)["entry_time"], "11:10")
        self.assertEqual(tm.replay_day(late, RULE, expiry=True), {"status": "no_trigger"})
        self.assertEqual(tm.replay_day(day(RANGE)[1:], RULE), {"status": "no_range"})   # the 09:15 bar missing

    def test_stop_rules(self):
        bars = day(dict(RANGE, **{"09:30": (101.6, 100.1, 101.5)}))
        self.assertEqual(tm.replay_day(bars, dict(RULE, stop="or_opposite"))["stop"], 99.0)
        t = tm.replay_day(bars, dict(RULE, stop="pct", stop_k=0.4))
        self.assertEqual((t["stop"], t["target"]), (101.05, 102.4))    # 101.5 x 0.996 = 101.094 down to 101.05
        # in-exit's ATR: true ranges 1.5, 1.8, 0.8, 1.5 up to the entry bar, mean 1.4; stop 101.5 - 1.4
        t = tm.replay_day(bars, dict(RULE, stop="atr", stop_k=1))
        self.assertEqual((t["stop"], t["target"]), (100.1, 104.3))
        self.assertEqual([tm.parse_stop(s) for s in ("or-mid", "OR_OPPOSITE", "pct:0.4", "atr:1.5", "atr", "pct:0")],
                         [("or_mid", None), ("or_opposite", None), ("pct", 0.4), ("atr", 1.5), None, None])

    def test_the_stop_rounds_away_from_the_entry(self):
        odd = dict(RANGE, **{"09:15": (101.03, 99.5, 100.5)})          # midpoint (101.03 + 99) / 2 = 100.015
        t = tm.replay_day(day(dict(odd, **{"09:30": (101.6, 100.1, 101.5)})), RULE)
        self.assertEqual((t["stop"], t["target"]), (100.0, 104.5))     # bullish: down to 100.00
        t = tm.replay_day(day(dict(odd, **{"09:30": (100.0, 98.8, 98.9)})), RULE)
        self.assertEqual((t["stop"], t["target"]), (100.05, 96.6))     # bearish: up to 100.05, risk 1.15


class Money(unittest.TestCase):
    def test_charges_in_r(self):
        ch = smart.read_json(os.path.join(ROOT, "in-setup", "charges.example.json"))
        c = tm.plan_charges(PLAN, ch)
        # buy 65 x 112.40 = 7,306.00: brokerage 20 + exchange 2.56 + SEBI 0.01 + stamp 0.22 + GST 4.06 = 26.85
        # sell 65 x 78.70 = 5,115.50: brokerage 20 + exchange 1.79 + SEBI 0.01 + STT 7.67 + GST 3.92 = 33.39
        self.assertEqual((c["round_trip_inr"], c["r_value_inr"], c["in_r"]), (83.93, 2190.5, 0.0383))
        with self.assertRaises(smart.SmartError):
            tm.plan_charges(dict(PLAN, r_value_inr=None), ch)

    def test_the_spread(self):
        s = tm.stats_of([2.0, -1.0, -1.0, 0.5, -1.0, 2.0])
        # running total 2, 1, 0, 0.5, -0.5, 1.5: from the peak 2 down to -0.5 is 2.5 R
        self.assertEqual(s, {"win_rate": 0.5, "expectancy_r": 0.25, "worst_drawdown_r": 2.5, "longest_losing_streak": 2})
        self.assertEqual(tm.pct(list(range(100)), 0.05), 5)
        # random.Random(seed).choices is plain arithmetic on random(): the same draws on every Python 3.9+
        self.assertEqual(random.Random(19).choices(range(49), k=6), [33, 38, 25, 25, 19, 48])

    def test_premium_r_by_hand(self):
        def snaps(*ltps):
            return [{"as_of_ist": "2026-11-24T%s:05+05:30" % t, "atm": 23500.0,
                     "rows": [{"strike": 23500.0, "type": "CE", "ltp": p}, {"strike": 23500.0, "type": "PE", "ltp": 1.0}]}
                    for t, p in zip(("09:30", "09:35", "09:50", "10:05", "10:20", "10:35"), ltps)]
        t = {"direction": "bullish", "entry_time": "09:35", "exit_time": "10:30"}
        # entry 100 at 09:35 (the 09:30 snapshot is before the entry), risk 30 % = 30, target 160
        self.assertEqual(tm.premium_r(snaps(1, 100, 110, 128, 160.5, 999), t, 30, 2.0), 2.0)
        self.assertEqual(tm.premium_r(snaps(1, 100, 110, 69.9, 160.5), t, 30, 2.0), -1.0)
        self.assertEqual(tm.premium_r(snaps(1, 100, 110, 120, 115, 999), t, 30, 2.0), 0.5)   # 10:35 is after the exit
        self.assertIsNone(tm.premium_r(snaps(1), t, 30, 2.0))


class InTemp(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        os.makedirs("trader")
        shutil.copy(smart.EXAMPLE_CONFIG, os.path.join("trader", "config.json"))
        shutil.copy(os.path.join(ROOT, "in-setup", "charges.example.json"), os.path.join("trader", "charges.json"))
        candles.write("off")

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tm(self, *extra, off="off"):
        return run(["run", "--offline", off, "--now", NOW] + list(extra))

    def plan(self, **kw):
        smart.write_json(os.path.join("trader", "plan.json"), dict(PLAN, **kw))

    def saved(self):
        return smart.read_json(os.path.join("trader", "timemachine.json"))


class Run(InTemp):
    def test_the_example_in_skill_md(self):
        self.plan()
        code, out, err = self.tm("--write")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: sixty days of NIFTY"))

    def test_the_saved_file(self):
        self.plan()
        self.tm("--write")
        doc = self.saved()
        self.assertEqual((doc["created_by"], doc["as_of_ist"], doc["partial"]), ("in-timemachine", "2026-11-24T18:00:00+05:30", None))
        self.assertIn("getCandleData", doc["source"])
        self.assertTrue(doc["replay_of_the_past"])
        self.assertEqual(doc["window"]["empty"], ["2026-10-02"])
        self.assertEqual(len(doc["days"]), 59)
        trades = [t for t in doc["days"] if t["status"] == "trade"]
        self.assertTrue(all(t["r"] == round(t["r_gross"] - 0.0383, 4) for t in trades))
        self.assertEqual(doc["history"]["expectancy_r"], round(sum(t["r"] for t in trades) / len(trades), 4))
        self.assertEqual([t["date"] for t in trades if t["expiry_day"]], ["2026-11-17"])   # the master's one
        b = doc["bootstrap"]
        self.assertEqual((b["samples"], b["seed"], b["trades_per_sample"]), (10000, 19, 49))

    def test_same_input_same_output(self):
        self.plan()
        first = self.tm("--write")
        saved = self.saved()
        shutil.rmtree(os.path.join("trader", "cache"))            # the second run fetches again
        self.assertEqual(self.tm("--write"), first)
        self.assertEqual(self.saved(), saved)
        self.assertEqual(self.tm(), (0, first[1].replace("Wrote trader/timemachine.json\n", ""), ""))  # from the cache

    def test_show_prints_the_saved_run(self):
        code, out, err = run(["show"])
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "in-timemachine: no saved replay in trader/timemachine.json: run timemachine.py run "
                              "--write first\n")
        _, ran, _ = self.tm("--write")
        code, out, _ = run(["show"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:-1], ran.splitlines()[:-2])
        self.assertEqual(out.splitlines()[-1], "Shown from trader/timemachine.json, saved Tue 2026-11-24 18:00 IST; "
                                               "nothing fetched.")

    def test_without_a_plan_no_charges(self):
        code, out, _ = self.tm()
        self.assertEqual(code, 0)
        self.assertIn("  Charges       charges were not applied (no trader/plan.json)\n", out)
        self.assertIn("stop at the range midpoint (default), target 2R", out)
        self.assertIn("  expectancy      -0.056 R a trade\n", out)
        self.plan(symbol="SENSEX")
        self.assertIn("charges were not applied (plan.json is for SENSEX)", self.tm("--symbol", "NIFTY")[1])

    def test_the_stop_flag_wins_and_an_unknown_rule_is_refused(self):
        self.plan()
        self.assertIn("stop at 0.3 % from the entry (--stop)", self.tm("--stop", "pct:0.3")[1])
        self.plan(underlying_stop_rule="or_opposite")
        self.assertIn("stop at the opposite side of the range (plan.json)", self.tm()[1])
        self.plan(underlying_stop_rule="atr:1.5")
        self.assertIn("stop at 1.5 x ATR14 of the day's bars to the entry (plan.json)", self.tm()[1])
        self.plan(underlying_stop_rule="swing low")
        code, out, err = self.tm()
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "in-timemachine: stop rule 'swing low' is not one the replay knows: pass --stop or-mid, "
                              "or-opposite, pct:X or atr:K\n")

    def test_option_premium_only_from_twenty_days_of_snapshots(self):
        out = self.tm("--write")[1]
        self.assertIn("  Options       option premium was not replayed: underlying only (0 days of chain snapshots, "
                      "20 needed)\n", out)
        folder = os.path.join("trader", "history", "chain", "NIFTY")
        for n, d in enumerate([t["date"] for t in self.saved()["days"]][:20]):
            snaps = [{"as_of_ist": "%sT%02d:%02d:05+05:30" % (d, m // 60, m % 60), "atm": 23500.0,
                      "rows": [{"strike": 23500.0, "type": k, "ltp": 100.0} for k in ("CE", "PE")]}
                     for m in range(9 * 60 + 30, 15 * 60 + 15, 5)]
            smart.write_bytes(os.path.join(folder, d + ".jsonl"),
                              "".join(json.dumps(s) + "\n" for s in snaps).encode("utf-8"))
            if n == 4:
                self.assertIn("underlying only (5 days of chain snapshots, 20 needed)", self.tm()[1])
        out = self.tm()[1]
        self.assertRegex(out, r"  Options       premium replayed from 20 days of your chain snapshots: \d+ of 49 trades "
                              r"priced \(ATM CE or PE at entry, premium stop 30 %\)\n")
        self.assertIn("win rate 0.0 %, expectancy +0.000 R a trade\n", out)   # a flat premium: 0 R every time

    def test_partial_when_a_fetch_fails(self):
        self.tm()
        folder = os.path.join("trader", "cache", "offline", "candles", "NSE_99926000", "FIVE_MINUTE")
        month = os.path.join(folder, "2026-11.json")   # the cache holds one file per month
        smart.write_json(month, {d: v for d, v in smart.read_json(month).items() if d < "2026-11-02"})
        os.makedirs("down")
        shutil.copy(os.path.join("off", "master.json"), "down")
        smart.write_json(os.path.join("down", "getCandleData.json"), {"status": 200, "headers": {}, "body": {
            "status": False, "message": "Something Went Wrong, Please Try After Sometime", "errorcode": "AB1004"}})
        code, out, err = self.tm("--write", off="down")
        self.assertEqual(code, 0, err)
        self.assertIn("60 trading days by the calendar, 43 with bars;", out)
        self.assertIn("  Partial       PARTIAL: 43 of 60 days used; 2026-11-02 to 2026-11-23: HTTP 200 AB1004 Something "
                      "Went Wrong, Please Try After Sometime\n", out)
        doc = self.saved()
        self.assertIn("AB1004", doc["partial"])
        self.assertEqual(len(doc["window"]["failed"]), 16)

    def test_too_few_days_or_no_trade_is_nothing(self):
        self.assertEqual(self.tm("--days", "10"), (1, "", "in-timemachine: --days wants at least 20 trading days\n"))
        candles.write("none", [])
        self.assertIn("only 0 day(s) of NIFTY bars from 2026-09-01 to 2026-11-23", self.tm(off="none")[2])
        shutil.rmtree(os.path.join("trader", "cache"))   # the empty days above are cached for good
        flat = [[r[0], 100.0, 100.0, 100.0, 100.0, 0] for r in candles.rows()]
        candles.write("flat", flat)
        code, out, err = self.tm(off="flat")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "in-timemachine: NIFTY: the rule never fired on the 59 days with bars from 2026-09-01 "
                              "to 2026-11-23: nothing to resample\n")

    def test_a_likely_corporate_action_skips_the_symbol(self):
        cut = [[r[0]] + [x / 2 for x in r[1:5]] + [0] if r[0] >= "2026-10-15" else r for r in candles.rows()]
        candles.write("bonus", cut)
        code, out, err = self.tm("--write", off="bonus")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, skill_example("## Example: a likely corporate action"))
        self.assertIsNone(self.saved())

    def test_it_replays_and_writes_nothing_else(self):
        self.plan()
        before = smart.read_json(os.path.join("trader", "plan.json"))
        self.tm("--write")
        self.assertEqual(smart.read_json(os.path.join("trader", "plan.json")), before)
        mine = sorted(f for f in os.listdir("trader") if os.path.isfile(os.path.join("trader", f)))
        self.assertEqual(mine, ["charges.json", "config.json", "plan.json", "timemachine.json"])
        self.assertEqual(os.listdir(os.path.join("trader", "cache")), ["offline"])   # never the real candle cache
        self.tm()                                                                   # no --write: no new file
        self.assertFalse(os.path.exists(os.path.join("trader", "entry.json")))

    def test_errors_are_one_plain_line(self):
        os.remove(os.path.join("trader", "config.json"))
        code, out, err = self.tm()
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "in-timemachine: no trader/config.json: run in-setup init first\n")
        shutil.copy(smart.EXAMPLE_CONFIG, os.path.join("trader", "config.json"))
        self.assertEqual(self.tm("--symbol", "FINNIFTY")[2], "in-timemachine: no index named FINNIFTY in the "
                                                             "instrument master\n")
        self.assertEqual(self.tm(off="nowhere")[2], "in-timemachine: offline: no master.json in nowhere\n")
        self.assertEqual(run(["run", "--now", NOW]), (1, "", "in-timemachine: getCandleData is not confirmed on this "
                                                            "account yet: run in-setup probe in market hours first\n"))
        self.assertEqual(run([])[0], 1)


if __name__ == "__main__":
    unittest.main()
