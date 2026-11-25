"""in-entry tests, all offline.

Live prices are built with smart.TickStore in a temp folder, as the feed would
write them. tests/fixtures/in-entry holds a synthetic master slice (NIFTY,
SENSEX and BANKNIFTY indices, two index futures, a few options) and
getCandleData replies shaped like the documented ones: [the previous day's
ONE_DAY candle, today's FIVE_MINUTE bars] in call order. No real data.
"""

import datetime
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

entry = load("in-entry", "entry")
smart = entry.smart
OFF = os.path.join(FIX, "in-entry")
DAY = "2026-11-25"          # a Wednesday; NIFTY's nearest expiry in the fixture master is 2026-12-01
NIFTY, SENSEX, FUT = "99926000", "99919000", "46000"

# (start, open, high, low, close): range 09:15 to 09:30 is 23,480 to 23,550
BULL = [("09:15", 23500, 23530, 23480, 23520), ("09:20", 23520, 23550, 23505, 23540),
        ("09:25", 23540, 23545, 23500, 23510), ("09:30", 23510, 23548, 23495, 23530),
        ("09:35", 23530, 23570, 23525, 23562.5), ("09:40", 23562.5, 23590, 23555, 23585),
        ("09:45", 23585, 23600, 23570, 23595)]
# opens 23,100 against the previous close 23,410 (-1.32%); range 23,080 to 23,160
BEAR = [("09:15", 23100, 23150, 23080, 23120), ("09:20", 23120, 23160, 23100, 23140),
        ("09:25", 23140, 23145, 23090, 23110), ("09:30", 23110, 23115, 23060, 23070),
        ("09:35", 23070, 23090, 23040, 23050)]
FLAT = BULL[:4] + [("%02d:%02d" % divmod(9 * 60 + 35 + 5 * i, 60), 23520, 23540, 23500, 23525) for i in range(24)]


def at(hms):
    return int(smart.parse_ist("%s %s" % (DAY, hms)).timestamp() * 1000)


def feed(bars, until, token=NIFTY, ex=1, lag=300, volumes=None):
    """Ticks for each bar (open, high, low, close inside it) before `until`, then one more
    tick 2 s before `until` at the last price, as a live feed would have."""
    s = smart.TickStore(os.path.join("trader", "state", "ticks.sqlite"))
    last, cum = None, 0
    for i, (t, o, h, l, c) in enumerate(bars):
        cum += volumes[i] if volumes else 0
        for sec, price in ((5, o), (70, h), (130, l), (298, c)):
            ms = at(t) + sec * 1000
            if ms < at(until):
                tick = {"token": token, "exchange_type": ex, "exch_ms": ms, "ltp": float(price)}
                if volumes:
                    tick["volume"] = cum
                s.add(tick, ms + lag)
                last = tick
    s.add(dict(last, exch_ms=at(until) - 2000), at(until) - 2000 + lag)
    s.close()


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def skill_example(heading, skill="in-entry"):
    with open(os.path.join(ROOT, skill, "SKILL.md"), encoding="utf-8") as fh:
        return re.search(re.escape(heading) + r".*?```text\n(.*?)```", fh.read(), re.S).group(1)


class InTemp(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        smart.write_json(os.path.join("trader", "config.json"),
                         smart.read_json(os.path.join(ROOT, "in-setup", "config.example.json")))
        smart.write_json(os.path.join("trader", "calendar.json"), {"holidays": {}, "session_overrides": {}})

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self, hm, *extra, off=OFF, cmd="check", symbol="NIFTY"):
        return run(cmd, symbol, "--offline", off, "--now", "%s %s" % (DAY, hm), *extra)

    def saved(self):
        return smart.read_json(os.path.join("trader", "entry.json"))


class Trigger(InTemp):
    def test_the_example_in_skill_md(self):
        feed(BULL, "09:41")
        code, out, err = self.check("09:41")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: a bullish break"))

    def test_bullish_day_writes_the_contract(self):
        feed(BULL, "09:41")
        self.check("09:41")
        e = self.saved()
        self.assertEqual((e["status"], e["direction"], e["trigger_level"]), ("triggered", "bullish", 23550.0))
        self.assertEqual((e["bar_start_ist"], e["bar_close_ist"], e["bar_close"]), ("09:35", "09:40", 23562.5))
        self.assertEqual(e["opening_range"], {"high": 23550.0, "low": 23480.0, "start": "09:15", "end": "09:30"})
        self.assertEqual((e["source"], e["feed_lag_ms"], e["notes"], e["filters_applied"]), ("feed", 300, [], []))
        self.assertEqual(e["as_of_ist"], "2026-11-25T09:41:00+05:30")

    def test_the_bar_in_progress_never_counts(self):
        feed(BULL, "09:39:30")    # the 09:35 bar is above the high so far, but still open
        code, out, _ = self.check("09:39:30")
        self.assertEqual(self.saved()["status"], "waiting")
        self.assertIn("last close 23,530.00 (09:30 bar) is inside the range", out)

    def test_bearish_day_and_the_gap_note(self):
        feed(BEAR, "09:36")
        code, out, _ = self.check("09:36")
        e = self.saved()
        self.assertEqual((e["status"], e["direction"], e["trigger_level"], e["bar_close_ist"]),
                         ("triggered", "bearish", 23080.0, "09:35"))
        self.assertIn("TRIGGERED bearish (buy a PE): the 09:30 bar closed 23,070.00 at 09:35, below the range low "
                      "23,080.00", out)
        self.assertEqual(e["notes"], ["gap day: opened 23,100.00, -1.32% from the previous close 23,410.00 "
                                      "(2026-11-24); entry.gap_pct is 1"])

    def test_no_trigger_day_expires(self):
        feed(FLAT, "11:31")
        code, out, _ = self.check("11:31")
        self.assertEqual((code, self.saved()["status"]), (0, "expired"))
        self.assertIn("EXPIRED  no confirmed 5-minute close beyond the range from 09:30 to 11:30", out)

    def test_before_the_range_ends_it_waits(self):
        feed(BULL, "09:26")
        self.check("09:26")
        e = self.saved()
        self.assertEqual((e["status"], e["opening_range"]), ("waiting", None))

    def test_one_entry_per_symbol_per_day(self):
        feed(BULL, "09:41")
        self.check("09:41")
        first = self.saved()
        code, out, _ = self.check("10:30")
        self.assertEqual(code, 0)
        self.assertIn("Already triggered today: one entry per symbol per day, so it is not re-made.", out)
        self.assertEqual(self.saved(), first)

    def test_a_non_trigger_never_overwrites_todays_trigger(self):
        feed(BULL, "09:41")
        self.check("09:41")
        feed([(t, 77300, 77400, 77250, 77320) for t, *_ in BULL[:6]], "09:41", token=SENSEX, ex=3)
        code, out, _ = self.check("09:41", symbol="SENSEX")    # SENSEX stays inside its range
        self.assertIn("Kept trader/entry.json: it holds today's NIFTY trigger.", out)
        self.assertEqual(self.saved()["symbol"], "NIFTY")


class Honesty(InTemp):
    def test_stale_feed_gives_no_signal(self):
        feed(BULL, "09:41")        # the break is in the bars, but the last tick is 4 minutes old
        code, out, _ = self.check("09:45")
        e = self.saved()
        self.assertEqual((code, e["status"], e["direction"]), (0, "stale", None))
        self.assertIn("STALE  the feed's last NIFTY tick is 241.7 s old (limit 3 s): no signal on stale data", out)

    def test_holiday_is_no_signal(self):
        smart.write_json(os.path.join("trader", "calendar.json"), {"holidays": {DAY: "test holiday"}})
        code, out, _ = self.check("09:41")
        self.assertEqual((code, self.saved()["status"]), (0, "no_signal"))
        self.assertIn("NO SIGNAL  not a trading day (closed: holiday (test holiday))", out)

    def test_expiry_day_note_from_the_master(self):
        off = os.path.join(self.tmp, "off")
        shutil.copytree(OFF, off)
        rows = smart.read_json(os.path.join(off, "master.json"))
        for r in rows:   # a synthetic slice whose nearest NIFTY expiry is today
            if r["name"] == "NIFTY" and r["expiry"] == "01DEC2026":
                r["expiry"] = "25NOV2026"
        smart.write_json(os.path.join(off, "master.json"), rows)
        feed(BULL, "09:41")
        self.check("09:41", off=off)
        self.assertEqual(self.saved()["notes"], [
            "expiry day: NIFTY options expire today; earlier cutoffs apply, last entry 11:00 and flat by 14:45 "
            "(config session.expiry_day_last_entry and expiry_day_flat_by)"])

    def test_candles_fallback_without_a_feed(self):
        code, out, err = self.check("09:41")
        self.assertEqual(code, 0, err)
        e = self.saved()
        self.assertEqual((e["status"], e["source"], e["bar_close_ist"], e["feed_lag_ms"]),
                         ("triggered", "candles", "09:40", None))
        self.assertIn("(getCandleData, closed 5-minute bars)", out)

    def test_a_feed_that_started_late_is_passed_over_for_candles(self):
        feed(BULL[4:], "09:41")     # ticks only from 09:35: no 09:15 bar, so no range from the feed
        code, out, _ = self.check("09:41")
        e = self.saved()
        self.assertEqual((e["source"], e["status"]), ("candles", "triggered"))
        self.assertIn("the feed's first bar today is 09:35, after the open: bars from getCandleData", e["notes"])

    def test_not_an_index_is_one_plain_line(self):
        code, out, err = self.check("09:41", symbol="RELIANCE")
        self.assertEqual(code, 1)
        self.assertEqual(err, "in-entry: RELIANCE is not an index in today's instrument master: version 1 covers "
                              "NIFTY, SENSEX and BANKNIFTY options\n")


class Filters(InTemp):
    def test_volume_vwap_and_trend_move_the_trigger(self):
        feed(BULL, "09:51")
        fut = [(t, o + 60, h + 60, l + 60, c + 60) for t, o, h, l, c in BULL]
        feed(fut, "09:51", token=FUT, ex=2, volumes=[3000, 2000, 1500, 1200, 500, 1500, 900])
        sensex = [("09:15", 77300, 77350, 77250, 77320), ("09:20", 77320, 77400, 77300, 77380),
                  ("09:25", 77380, 77390, 77310, 77330), ("09:30", 77330, 77395, 77320, 77380),
                  ("09:35", 77380, 77420, 77370, 77410), ("09:40", 77410, 77480, 77400, 77470),
                  ("09:45", 77470, 77490, 77450, 77480)]
        feed(sensex, "09:51", token=SENSEX, ex=3)
        off = os.path.join(self.tmp, "off")
        shutil.copytree(OFF, off)
        day0 = datetime.date(2026, 10, 14)
        history = [["%sT%s:00+05:30" % (day0 + datetime.timedelta(days=i), t), 1.0, 1.0, 1.0, 1.0, 1000]
                   for i in range(42) if (day0 + datetime.timedelta(days=i)).weekday() < 5 for t in ("09:35", "09:40")]
        replies = smart.read_json(os.path.join(off, "getCandleData.json"))
        replies[1] = dict(replies[1], body=dict(replies[1]["body"], data=history))  # the future's 20-day history
        smart.write_json(os.path.join(off, "getCandleData.json"), replies)
        code, out, err = self.check("09:51", "--filter", "volume", "--filter", "vwap", "--filter", "trend", off=off)
        self.assertEqual(code, 0, err)
        e = self.saved()
        self.assertEqual((e["status"], e["bar_start_ist"], e["bar_close"]), ("triggered", "09:40", 23585.0))
        self.assertEqual(e["filters_applied"], ["volume", "vwap", "trend"])
        self.assertEqual(e["notes"][0], "1 close beyond the range not confirmed; the last, the 09:35 bar: volume 0.50x "
                                        "the 20-day average of the 09:35 bar (NIFTY29DEC26FUT, needs 1.00x)")
        self.assertIn("confirmed by volume 1.50x the 20-day average of the 09:40 bar (NIFTY29DEC26FUT, needs 1.00x)",
                      e["notes"])
        self.assertIn("confirmed by trend: SENSEX closed 77,470.00, range 77,250.00 to 77,400.00", e["notes"])
        self.assertTrue(any(n.startswith("confirmed by vwap: NIFTY29DEC26FUT closed 23,645.00, above its VWAP")
                            for n in e["notes"]))


class Watch(InTemp):
    def test_the_watch_example_in_skill_md(self):
        feed(BULL, "09:41")
        code, out, err = self.check("09:31", "--every", "300", cmd="watch")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: watching the morning"))
        self.assertEqual(self.saved()["status"], "triggered")

    def test_watch_stops_with_1_when_expired(self):
        feed(FLAT, "11:31")
        code, out, _ = self.check("11:31", cmd="watch")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
