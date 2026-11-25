"""in-alerts tests, all offline, on a pinned clock.

The fixtures in tests/fixtures/in-alerts are synthetic, shaped like
CONTRACTS.md (orders.json, journal.json) and the instrument master (the index
rows only). Live prices are built with smart.TickStore in a temp folder, the
way the feed writes them; morning() also writes NIFTY minute by minute from
09:40 so the 5-minute bars behind the no-progress watch exist. No desktop notification command is ever run:
subprocess.Popen is replaced wherever --notify is used.
"""

import datetime
import io
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, FIX, ROOT

alerts = load("in-alerts", "alerts")
smart = alerts.smart
OFF = os.path.join(FIX, "in-alerts")
DAY = "2026-11-24"   # a Tuesday: T20261124-04's contract expires today
OPT1, OPT4, IDX = "45014", "45005", "99926000"


def at(hms):
    return int(smart.parse_ist(DAY + " " + hms).timestamp() * 1000)


def skill_example(heading):
    """The ```text block under `heading` in in-alerts/SKILL.md."""
    with open(os.path.join(ROOT, "in-alerts", "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    return re.search(re.escape(heading) + r".*?```text\n(.*?)```", text, re.S).group(1)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class InTemp(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        os.makedirs(os.path.join("trader", "cache"))
        for f in ("orders.json", "config.json"):
            shutil.copy(os.path.join(OFF, f), os.path.join("trader", f))
        os.makedirs(os.path.join("trader", "state"))
        shutil.copy(os.path.join(OFF, "journal.json"), os.path.join("trader", "state", "journal.json"))
        shutil.copy(os.path.join(OFF, "master.json"), os.path.join("trader", "cache", "instrument_master_%s.json" % DAY))

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def feed(self, *ticks):
        """(token, exchange time HH:MM:SS(.f), price, lag ms) -> trader/state/ticks.sqlite."""
        s = smart.TickStore(os.path.join("trader", "state", "ticks.sqlite"))
        for token, hms, ltp, lag in ticks:
            s.add({"token": token, "exchange_type": 1 if token == IDX else 2, "exch_ms": at(hms), "ltp": ltp},
                  at(hms) + lag)
        s.close()

    def morning(self):
        # NIFTY each minute from 09:40: up to 23,590.00 at 09:50, then down to 23,520.00 at 10:18
        path = [(IDX, "%02d:%02d:30" % divmod(m, 60), 23590.0 - 2.5 * abs(m - 590), 300) for m in range(580, 619)]
        self.feed(*path + [(OPT1, "10:19:59", 96.4, 250), (IDX, "10:19:59.1", 23510.0, 300),
                           (OPT4, "10:19:58.5", 92.15, 200)])
        return run("check", "--now", DAY + " 10:20")

    def afternoon(self):
        self.feed((OPT1, "14:35:59", 131.2, 250), (IDX, "14:35:59.4", 23598.25, 300), (OPT4, "14:35:40", 70.05, 250))
        return run("check", "--now", DAY + " 14:36")

    def alerted(self):
        return smart.read_json(os.path.join("trader", "state", "alerted.json"))


class Examples(InTemp):
    def test_a_stop_on_the_underlying(self):
        code, out, err = self.morning()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("\a", out)                       # the terminal bell
        self.assertEqual(out.replace("\a", ""), skill_example("## Example: a stop on the underlying"))

    def test_later_a_stale_premium_near_the_flat_by(self):
        self.morning()
        code, out, err = self.afternoon()
        self.assertEqual(code, 0)
        self.assertEqual(out.replace("\a", ""), skill_example("## Example: later, a stale premium near the flat-by"))


class Rules(InTemp):
    T1 = {"instrument": {"underlying": "NIFTY", "type": "CE", "expiry": "2026-12-01", "tradingsymbol": "X"}}
    T4 = {"instrument": {"underlying": "NIFTY", "type": "PE", "expiry": DAY, "tradingsymbol": "Y"}}
    O1 = {"premium_stop": 78.7, "target_premium": 179.8, "underlying_stop": 23515.0, "target_underlying": 23657.5}
    O4 = {"premium_stop": 59.7, "target_premium": 136.5, "underlying_stop": 23640.0, "target_underlying": 23420.0}

    def kinds(self, trade, order, opt, und, hm="10:20", flat="15:10", has_und=True):
        q = lambda px, stale=False: None if px is None else {  # noqa: E731
            "ltp": px, "lag_ms": 250, "age_ms": 9000 if stale else 500, "stale": stale}
        o = q(*opt) if isinstance(opt, tuple) else q(opt)
        u = q(*und) if isinstance(und, tuple) else q(und)
        return [k for k, _ in alerts.evaluate(trade, order, o, u, has_und, smart.parse_ist(DAY + " " + hm), flat, 3)]

    def test_every_price_kind_and_the_direction_of_a_pe(self):
        T1, O1, T4, O4 = self.T1, self.O1, self.T4, self.O4
        self.assertEqual(self.kinds(T1, O1, 100.0, 23560.0), [])
        self.assertEqual(self.kinds(T1, O1, 78.7, 23560.0), ["stop_premium"])          # at the stop counts
        self.assertEqual(self.kinds(T1, O1, 179.8, 23657.5), ["target_premium", "target_underlying"])
        self.assertEqual(self.kinds(T1, O1, 100.0, 23515.0), ["stop_underlying"])
        self.assertEqual(self.kinds(T4, O4, 90.0, 23500.0), ["expiry_day"])
        self.assertEqual(self.kinds(T4, O4, 90.0, 23640.0), ["stop_underlying", "expiry_day"])   # a PE stops above
        self.assertEqual(self.kinds(T4, O4, 140.0, 23420.0), ["target_premium", "target_underlying", "expiry_day"])

    def test_time_kinds(self):
        T1, O1 = self.T1, self.O1
        self.assertEqual(self.kinds(T1, O1, 100.0, 23560.0, "14:59"), [])
        self.assertEqual(self.kinds(T1, O1, 100.0, 23560.0, "15:00"), ["flat_by_soon"])   # 10 minutes before
        self.assertEqual(self.kinds(T1, O1, 100.0, 23560.0, "15:09"), ["flat_by_soon"])
        self.assertEqual(self.kinds(T1, O1, 100.0, 23560.0, "15:10"), ["flat_by"])
        self.assertEqual(self.kinds(T1, O1, 100.0, 23560.0, "15:30"), ["flat_by"])

    def test_a_stale_price_decides_no_stop_or_target(self):
        T1, O1 = self.T1, self.O1
        self.assertEqual(self.kinds(T1, O1, (50.0, True), (23000.0, True)), ["stale_premium", "stale_underlying"])
        self.assertEqual(self.kinds(T1, O1, None, 23515.0), ["stale_premium", "stop_underlying"])
        self.assertEqual(self.kinds(T1, O1, 50.0, None), ["stop_premium", "stale_underlying"])
        self.assertEqual(self.kinds(T1, O1, 50.0, None, has_und=False), ["stop_premium"])   # no index in the master

    def test_every_kind_from_the_feed(self):
        """Each kind rings from TickStore data at a pinned time. Calm prices unless the case moves one;
        an age of 10 s is past the 3 s stale limit."""
        cases = [("T20261124-01", "stop_premium", "10:20", {OPT1: 78.7}, ()),
                 ("T20261124-01", "target_premium", "10:20", {OPT1: 179.8}, ()),
                 ("T20261124-01", "stop_underlying", "10:20", {IDX: 23515.0}, ()),
                 ("T20261124-01", "target_underlying", "10:20", {IDX: 23657.5}, ()),
                 ("T20261124-04", "stop_underlying", "10:20", {IDX: 23640.0}, ()),     # a PE stops above
                 ("T20261124-04", "target_underlying", "10:20", {IDX: 23420.0}, ()),
                 ("T20261124-01", "flat_by_soon", "14:55", {}, ()),
                 ("T20261124-01", "flat_by", "15:05", {}, ()),
                 ("T20261124-04", "expiry_day", "10:20", {}, ()),
                 ("T20261124-01", "no_progress", "10:20", {}, ()),
                 ("T20261124-01", "stale_premium", "10:20", {}, (OPT1,)),
                 ("T20261124-01", "stale_underlying", "10:20", {}, (IDX,))]
        for tid, kind, hm, moved, old in cases:
            with self.subTest(kind=kind, ticket=tid):
                for f in ("alerted.json", "ticks.sqlite", "ticks.sqlite-wal", "ticks.sqlite-shm"):
                    if os.path.exists(os.path.join("trader", "state", f)):
                        os.remove(os.path.join("trader", "state", f))
                calm = {OPT1: 100.0, IDX: 23560.0, OPT4: 90.0}
                calm.update(moved)
                t = smart.parse_ist(DAY + " " + hm)
                if kind == "no_progress":   # NIFTY calm under the CE's entry 23,562.50 from 09:40 on
                    self.feed(*[(IDX, "%02d:%02d:30" % divmod(m, 60), 23560.0, 0) for m in range(580, 619)])
                self.feed(*[(tok, (t - datetime.timedelta(seconds=10 if tok in old else 1)).strftime("%H:%M:%S"), px, 0)
                            for tok, px in calm.items()])
                code, out, _ = run("check", "--now", DAY + " " + hm)
                self.assertEqual(code, 0)
                self.assertIn("%s:%s" % (tid, kind), self.alerted()[DAY])
                self.assertIn("ALERT %s %s: " % (tid, kind), out)
        self.assertEqual({c[1] for c in cases}, set(alerts.KINDS))

    def test_no_progress_as_in_exit_plans_it(self):
        """in-exit: out when no 5-minute close beyond the entry comes within no_progress_bars bars. T01 was
        filled at 09:41, so its bars start 09:40 and the sixth closes 10:10. Rows are smart.read_bars':
        [start_ms, open, high, low, close, volume, oi, minutes with ticks]."""
        T, O = dict(self.T1, date=DAY, fill_time_ist="09:41"), dict(self.O1, entry_underlying=23562.5, no_progress_bars=6)

        def bars(*closes):
            return [[at("09:40:00") + i * 300000, 0, 0, 0, c, None, None, 5] for i, c in enumerate(closes)]

        fresh = {"stale": False}

        def np(rows, und=fresh, to="10:10:00", t=T, o=O):
            return alerts.no_progress(t, o, rows, und, at(to))

        flat = [23562.5] * 6                                        # at the entry is not beyond it
        self.assertEqual(np(bars(*flat))[0], "NIFTY has not closed above the entry 23,562.50 in the 6 five-minute "
                                             "bars 09:40 to 10:10: the plan's no-progress exit")
        self.assertEqual(np(bars(*flat[:5] + [23562.55])),
                         (None, "no progress: cleared, NIFTY closed above the entry 23,562.50 at 10:10"))
        self.assertEqual(np(bars(*flat[:3]), to="09:57:00"), (None, "no progress: 3 of 6 five-minute bars since the "
                                                                    "fill closed, none above the entry 23,562.50 (due 10:10)"))
        gap, short = bars(*flat), bars(*flat)
        del gap[2]
        short[2][7] = 4                                             # a minute missing from the feed
        for rows in (gap, short):
            self.assertEqual(np(rows), (None, "no progress: not decided, the feed has 5 of the 6 closed NIFTY "
                                              "5-minute bars of 09:40 to 10:10"))
        self.assertEqual(np(bars(*flat), und=None), (None, "no progress: not decided, no fresh NIFTY tick"))
        self.assertIsNone(np(bars(*flat), und={"stale": True})[0])
        pe = dict(self.T4, date=DAY, fill_time_ist="09:41")         # a PE needs a close below the entry
        self.assertTrue(np(bars(*flat), t=pe)[0].startswith("NIFTY has not closed below the entry 23,562.50"))
        self.assertIsNone(np(bars(*flat[:5] + [23562.45]), t=pe)[0])
        self.assertTrue(np(bars(*flat), o=self.O1)[1].startswith("no progress: not watched"))   # no plan numbers

    def test_stale_from_the_feed_with_its_age(self):
        self.feed((OPT1, "10:19:50", 50.0, 250), (IDX, "10:19:59", 23000.0, 300), (OPT4, "10:19:59", 92.0, 200))
        code, out, _ = run("check", "--now", DAY + " 10:20")
        self.assertIn("ALERT T20261124-01 stale_premium: premium 50.00 (lag 250 ms, age 9.8 s, STALE): no tick for "
                      "over 3 s, so no stop or target is decided on it", out)
        self.assertNotIn("stop_premium", out)          # 50.00 is under the stop, but stale
        self.assertIn("T20261124-01 stop_underlying", out)

    def test_no_tick_at_all_says_so(self):
        code, out, _ = run("check", "--now", DAY + " 10:20")
        self.assertEqual(code, 0)
        self.assertIn("premium (no tick yet): nothing from the feed for this token (is in-common/feed.py running?)", out)


class Dedupe(InTemp):
    def test_each_kind_rings_once_per_day(self):
        self.morning()
        self.assertEqual(self.alerted(), {DAY: ["T20261124-01:stop_underlying", "T20261124-04:expiry_day"]})
        code, out, _ = run("check", "--now", DAY + " 10:20")
        self.assertEqual(code, 1)
        self.assertNotIn("\a", out)
        self.assertIn("rang earlier today: stop_underlying", out)
        self.assertIn("Nothing new rang.", out)

    def test_a_new_day_rings_again_and_keeps_only_that_day(self):
        self.morning()
        nxt = "2026-11-25"
        with mock.patch.object(alerts.smart, "load_master", return_value=smart.read_json(os.path.join(OFF, "master.json"))):
            code, out, _ = run("check", "--now", nxt + " 10:20")
        self.assertEqual(code, 0)
        self.assertEqual(list(self.alerted()), [nxt])


class Master(InTemp):
    def test_the_offline_master_when_todays_copy_is_missing(self):
        os.remove(os.path.join("trader", "cache", "instrument_master_%s.json" % DAY))
        code, out, err = run("check", "--offline", OFF, "--now", DAY + " 10:20")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("    NIFTY (no tick yet): stop 23,515.00, target 23,657.50", out)   # the index token was found
        self.assertEqual(os.listdir(os.path.join("trader", "cache")), [])                  # nothing downloaded


class Ringing(InTemp):
    def test_the_command_for_each_platform(self):
        mac, _ = alerts.notify_command("hi", "darwin")
        self.assertEqual((mac[0], mac[-1]), ("osascript", "hi"))
        self.assertEqual(alerts.notify_command("hi", "linux")[0], ["notify-send", "in-alerts", "hi"])
        win, env = alerts.notify_command("it's", "win32")
        self.assertEqual(win[0], "powershell")
        self.assertNotIn("it's", " ".join(win))        # the text goes by environment, never inside the script
        self.assertEqual(env, {"IN_ALERTS_TEXT": "it's"})
        self.assertIsNone(alerts.notify_command("hi", "sunos5"))

    def test_notify_starts_one_command_and_a_failure_is_one_note(self):
        with mock.patch.object(alerts.subprocess, "Popen") as popen:
            code, out, _ = self.morning_notify()
        self.assertEqual(popen.call_count, 1)
        self.assertNotIn("Note:", out)
        os.remove(os.path.join("trader", "state", "alerted.json"))
        with mock.patch.object(alerts.subprocess, "Popen", side_effect=FileNotFoundError("not found")):
            code, out, _ = run("check", "--notify", "--now", DAY + " 10:20")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("Note: desktop notification failed (not found): the terminal bell rang"), 1)

    def morning_notify(self):
        self.feed((OPT1, "10:19:59", 96.4, 250), (IDX, "10:19:59.1", 23510.0, 300), (OPT4, "10:19:58.5", 92.15, 200))
        return run("check", "--notify", "--now", DAY + " 10:20")

    def test_no_notify_runs_nothing(self):
        with mock.patch.object(alerts.subprocess, "Popen") as popen:
            self.morning()
        popen.assert_not_called()


class Session(InTemp):
    def test_subscribes_the_option_and_the_index(self):
        self.morning()
        subs = smart.read_json(os.path.join("trader", "state", "subscriptions.json"))
        self.assertEqual(sorted(i["token"] for i in subs["in-alerts"]), sorted([OPT1, IDX, OPT4, IDX]))

    def test_closed_days_and_hours_watch_nothing(self):
        smart.write_json(os.path.join("trader", "calendar.json"), {"holidays": {"2026-11-25": "test holiday"}})
        for when, why in (("2026-11-25 10:00", "F&O closed: holiday (test holiday), nothing watched"),
                          ("2026-11-28 10:00", "F&O closed: weekend, nothing watched"),
                          (DAY + " 09:10", "F&O closed: outside 09:15 to 15:40 IST, nothing watched"),
                          (DAY + " 15:40", "F&O closed: outside 09:15 to 15:40 IST, nothing watched")):
            code, out, err = run("check", "--now", when)
            self.assertEqual((code, out, err.strip()), (1, "", "in-alerts: " + why), when)
        code, out, err = run("watch", "--now", "2026-11-25 10:00")
        self.assertEqual((code, out), (1, ""))

    def test_no_open_trades(self):
        smart.write_json(os.path.join("trader", "state", "journal.json"), {"trades": []})
        smart.subscribe("trader", "in-alerts", [{"segment": "NFO", "token": OPT1}])
        code, out, err = run("check", "--now", DAY + " 10:20")
        self.assertEqual(code, 1)
        self.assertIn("no open trades", err)
        self.assertNotIn("in-alerts", smart.read_json(os.path.join("trader", "state", "subscriptions.json")))

    def test_watch_ends_at_the_fo_close(self):
        self.feed((OPT1, "15:37:59", 96.4, 250), (IDX, "15:37:59", 23560.0, 300), (OPT4, "15:37:59", 92.0, 200))
        code, out, _ = run("watch", "--every", "60", "--now", DAY + " 15:38")
        self.assertEqual(code, 0)
        self.assertIn("Alerts check, Tue 2026-11-24 15:38 IST", out)
        self.assertIn("15:39:00 ALERT T20261124-01 stale_premium", out)   # the simulated clock moved on
        self.assertTrue(out.replace("\a", "").rstrip().split("\n")[-1].startswith(
            "Watch ended at 15:40 IST: F&O is closed for the day."))
        self.assertEqual(run("watch", "--now", DAY + " 15:40")[2].strip(),
                         "in-alerts: F&O closed: outside 09:15 to 15:40 IST, nothing watched")

    def test_watch_waits_for_the_open(self):
        with mock.patch.object(alerts, "check_once", wraps=alerts.check_once) as once:
            code, out, _ = run("watch", "--every", "600", "--now", DAY + " 09:00")
        self.assertEqual(code, 0)
        first = once.call_args_list[0][0][2]
        self.assertEqual(first.strftime("%H:%M"), "09:20")     # 09:00 and 09:10 are before the 09:15 open


if __name__ == "__main__":
    unittest.main()
