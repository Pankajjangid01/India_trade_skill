"""Offline tests for the in-timemachine integration: the rolling 60-day window and the pre-trade verdict.

All candles are synthetic (controlled test data) served by a fake client: no network, no broker.
"""

import datetime
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app import timemachine as tmi
from app.engine import Engine
from app.sp import smart

IST = smart.IST
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CAL = {}                                   # weekdays only, no holidays: easy to count


def day_rows(day, win=True, base=25000.0):
    """One day of 5-minute bars 09:15..15:25 as getCandleData rows. Range 09:15-09:30 is 25000 +/- 10;
    09:30 closes above it; then the target (win) or the stop (not win) is hit."""
    rows, px = [], base
    for m in range(9 * 60 + 15, 15 * 60 + 30, 5):
        hm = "%02d:%02d" % divmod(m, 60)
        if hm < "09:30":
            o, h, l, c = px, px + 10, px - 10, px
        elif hm == "09:30":
            o, h, l, c = px, px + 25, px - 1, px + 20         # entry at 25020, or-mid stop 25000, 2R target 25060
        elif hm == "09:35":
            o, h, l, c = (px + 20, px + 70, px + 19, px + 65) if win else (px + 20, px + 21, px - 5, px - 3)
        else:
            c = rows[-1][4]
            o, h, l = c, c + 1, c - 1
        rows.append(["%sT%s:00+05:30" % (day, hm), o, h, l, c, 1000])
    return rows


class Reply:
    def __init__(self, data):
        self.ok, self.data = True, data

    def why(self):
        return ""


class FakeClient:
    def __init__(self, win=True, holidays=()):
        self.calls, self.win, self.holidays = [], win, set(holidays)

    def call(self, endpoint, body):
        assert endpoint == "getCandleData"
        self.calls.append((body["fromdate"][:10], body["todate"][:10]))
        d = datetime.date.fromisoformat(body["fromdate"][:10])
        last = datetime.date.fromisoformat(body["todate"][:10])
        rows = []
        while d <= last:
            if d.weekday() < 5 and d.isoformat() not in self.holidays:
                rows += day_rows(d.isoformat(), self.win)
            d += datetime.timedelta(1)
        return Reply(rows)


def at(day, hm="09:00"):
    return smart.parse_ist("%s %s" % (day, hm))


class WindowTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def ensure(self, client, today, store=None):
        end = smart.previous_trading_day(datetime.date.fromisoformat(today), CAL).isoformat()
        return tmi.ensure_timemachine_window(self.dir, end, "NIFTY", "NSE", "99926000", client, at(today), 60, CAL,
                                             store=store)

    def test_first_run_then_roll_one_day(self):
        c = FakeClient()
        w = self.ensure(c, "2026-11-26")                       # Thursday: the window ends Wed 2026-11-25
        self.assertEqual(len(w["dates"]), 60)
        self.assertEqual(w["dates"][-1], "2026-11-25")
        self.assertEqual(len(w["fetched"]), 60)
        self.assertEqual((w["dropped"], w["missing"], w["partial"]), (0, [], None))
        self.assertEqual(len(c.calls), 1)                      # one getCandleData call covers the 60 days
        first = w["dates"][0]

        c2 = FakeClient()
        w2 = self.ensure(c2, "2026-11-27")                     # next day: 2026-11-26 joins, the oldest drops
        self.assertEqual(len(w2["dates"]), 60)
        self.assertEqual(w2["dates"][-1], "2026-11-26")
        self.assertNotIn(first, w2["dates"])
        self.assertEqual((w2["fetched"], w2["dropped"]), (["2026-11-26"], 1))
        self.assertEqual(c2.calls, [("2026-11-26", "2026-11-26")])
        self.assertEqual(len(tmi.Window(self.dir).days("NIFTY", "99926000")), 60)

        c3 = FakeClient()                                      # same day again: nothing fetched
        self.assertEqual(self.ensure(c3, "2026-11-27")["fetched"], [])
        self.assertEqual(c3.calls, [])

    def test_a_holiday_the_calendar_lacks_extends_the_window(self):
        c = FakeClient(holidays=["2026-10-02"])
        w = self.ensure(c, "2026-11-26")
        self.assertEqual(len(w["dates"]), 61)                         # one more day in the span...
        self.assertEqual(sum(1 for v in w["bars"].values() if v), 60)  # ...so 60 days have bars
        self.assertEqual(w["empty"], ["2026-10-02"])
        c2 = FakeClient(holidays=["2026-10-02"])
        w2 = self.ensure(c2, "2026-11-27")                             # the holiday is stored, never refetched
        self.assertEqual(c2.calls, [("2026-11-26", "2026-11-26")])
        self.assertEqual(sum(1 for v in w2["bars"].values() if v), 60)

    def test_a_gap_fetches_only_that_day(self):
        self.ensure(FakeClient(), "2026-11-26")
        store = tmi.Window(self.dir)
        with store.db:
            store.db.execute("DELETE FROM days WHERE day = '2026-10-15'")
        shutil.rmtree(os.path.join(self.dir, "cache"))          # and the shared candle cache: a real refetch
        c = FakeClient()
        w = self.ensure(c, "2026-11-26", store)
        self.assertEqual(w["fetched"], ["2026-10-15"])
        self.assertEqual(c.calls, [("2026-10-15", "2026-10-15")])
        store.close()

    def test_no_session_names_the_missing_days(self):
        w = self.ensure(None, "2026-11-26")
        self.assertEqual(len(w["missing"]), 60)
        self.assertIn("no broker session", w["partial"])

    def test_today_is_never_stored(self):
        w = tmi.ensure_timemachine_window(self.dir, "2026-11-26", "NIFTY", "NSE", "1", FakeClient(), at("2026-11-26"),
                                          60, CAL)
        self.assertEqual(w["dates"][-1], "2026-11-25")


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "trader", "config.json"), os.path.join(self.dir, "config.json"))
        self.cfg = smart.load_config(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def window(self, win):
        end = "2026-11-25"
        return tmi.ensure_timemachine_window(self.dir, end, "NIFTY", "NSE", "1" if win else "2", FakeClient(win),
                                             at("2026-11-26"), 60, CAL)

    def test_orb_target_first_passes(self):
        doc = tmi.check(self.dir, "NIFTY", "ORB", self.cfg, self.window(True))
        self.assertTrue(doc["ok"])
        h = doc["data"]["history"]
        self.assertEqual((h["trades"], h["target"], h["target_first_pct"], h["expectancy_r"]), (60, 60, 100.0, 2.0))
        self.assertEqual(doc["data"]["bootstrap"]["seed"], 19)
        self.assertIn("target first 100.0 %", doc["reason"])
        saved = smart.read_json(os.path.join(self.dir, "state", "timemachine", "NIFTY_ORB.json"))
        self.assertEqual(set(saved) >= {"ok", "reason", "data"}, True)

    def test_orb_stop_first_blocks(self):
        doc = tmi.check(self.dir, "NIFTY", "ORB", self.cfg, self.window(False))
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["data"]["history"]["stop_first_pct"], 100.0)
        self.assertIn("below timemachine.min_expectancy_r", doc["reason"])

    def test_inform_only_and_required(self):
        cfg = dict(self.cfg, timemachine={"min_expectancy_r": None})
        self.assertTrue(tmi.check(self.dir, "NIFTY", "ORB", cfg, self.window(False))["ok"])
        empty = {"dates": [], "bars": {}, "missing": [], "partial": "x"}
        self.assertTrue(tmi.check(self.dir, "NIFTY", "ORB", self.cfg, empty)["ok"])
        cfg = dict(self.cfg, timemachine={"required": True})
        doc = tmi.check(self.dir, "NIFTY", "ORB", cfg, empty)
        self.assertFalse(doc["ok"])
        self.assertIn("time machine unavailable", doc["reason"])

    def test_ema_pullback_replays(self):
        doc = tmi.check(self.dir, "NIFTY", "EMA_PULLBACK", self.cfg, self.window(True))
        # flat synthetic afternoons give no pullback: "never fired" is an honest unavailable, not a crash
        self.assertTrue(doc["ok"])
        self.assertTrue(doc["data"] is None or doc["data"]["history"]["trades"] > 0)

    def test_walk_stop_before_target(self):
        bars = [{"start_ist": "10:00", "open": 100, "high": 100, "low": 100, "close": 100},
                {"start_ist": "10:05", "open": 100, "high": 130, "low": 80, "close": 100}]
        self.assertEqual(tmi.walk(bars, 0, True, 100, 90, 120, "15:10", 2.0)[:2], ("stop", -1.0))


class EngineGateTest(unittest.TestCase):
    SIG = {"id": "tm", "strategy": "ORB", "symbol": "NIFTY", "date": "2026-11-26", "direction": "bullish",
           "bar_start_ist": "09:50", "bar_close_ist": "09:55", "entry_underlying": 25900.0,
           "stop_underlying": 25850.0, "target_underlying": 26000.0, "risk_points": 50.0}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "trader", "config.json"), os.path.join(self.dir, "config.json"))
        self.eng = Engine(trader=self.dir)
        self.eng.day = "2026-11-26"
        self.eng.market["NIFTY"] = {"ltp": 25905.0}
        self.eng.facts["NIFTY"] = {"token": "99926000", "segment": "NSE", "expiry_today": False, "expiries": []}
        self.cfg = smart.load_config(self.dir)
        smart.write_json(os.path.join(self.dir, "calendar.json"), CAL)

    def tearDown(self):
        self.eng.store.db.close()
        if self.eng.tm_window:
            self.eng.tm_window.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def process(self, win):
        now = smart.parse_ist("2026-11-26 09:55:02")
        self.eng.client = FakeClient(win)
        with mock.patch.object(smart, "now_ist", lambda pinned=None: now), \
                mock.patch.object(self.eng, "execute") as ex:
            self.eng.process(dict(self.SIG), self.cfg)
        return ex, self.eng.store.signals("2026-11-26")[0]

    def test_a_losing_rule_is_blocked_before_the_trade(self):
        ex, s = self.process(False)
        ex.assert_not_called()
        self.assertEqual(s["status"], "REJECTED")
        self.assertTrue(s["reason"].startswith("time machine: NIFTY ORB"))
        self.assertEqual(s["meta"]["timemachine"]["stop_first_pct"], 100.0)

    def test_a_winning_rule_trades_and_is_cached(self):
        ex, s = self.process(True)
        ex.assert_called_once()
        self.assertEqual(s["status"], "EXECUTABLE")
        self.assertEqual(s["meta"]["timemachine"]["target_first_pct"], 100.0)
        self.assertEqual(len(self.eng.tm_cache), 1)
        calls = len(self.eng.client.calls)
        self.eng.timemachine_verdict("NIFTY", "ORB", smart.parse_ist("2026-11-26 10:30"), self.cfg)
        self.assertEqual(len(self.eng.client.calls), calls)     # same day and rule: no refetch, no rerun


if __name__ == "__main__":
    unittest.main()
