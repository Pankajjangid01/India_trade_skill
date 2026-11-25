"""in-scanner tests, all offline.

The fixtures are synthetic, shaped like Angel One's documented replies:
tests/fixtures/in-scanner/master.json is the in-setup master slice plus F&O
stocks (one banned, one with no cash row) and the index futures;
orb/ holds a 5-minute candle reply (five past sessions and today), a FULL
quote for the stocks and a cautionaryScrips reply. The live feed is a
TickStore built in the temp folder; the daily candles are generated below.
"""

import datetime
import io
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

scan = load("in-scanner", "scan")
smart = scan.smart
HERE = os.path.join(FIX, "in-scanner")
NOW = "2026-11-24 09:31"   # a Tuesday, one minute after the opening range closed

TICKS = [  # (token, exchange type, exchange time, price, cumulative volume, feed lag ms)
    ("99926000", 1, "09:15:05", 23500.0, None, 250), ("99926000", 1, "09:17:00", 23550.0, None, 250),
    ("99926000", 1, "09:19:58", 23520.0, None, 250), ("99926000", 1, "09:21:00", 23480.0, None, 250),
    ("99926000", 1, "09:24:58", 23505.0, None, 250), ("99926000", 1, "09:26:00", 23530.0, None, 250),
    ("99926000", 1, "09:29:58", 23540.0, None, 250), ("99926000", 1, "09:30:58", 23562.5, None, 250),
    ("99919000", 3, "09:15:05", 77200.0, None, 300), ("99919000", 3, "09:18:00", 77320.0, None, 300),
    ("99919000", 3, "09:22:00", 77150.0, None, 300), ("99919000", 3, "09:27:00", 77280.0, None, 300),
    ("99919000", 3, "09:30:57", 77301.1, None, 300),
    ("44990", 2, "09:15:10", 23560.0, 200000, 200), ("44990", 2, "09:19:50", 23570.0, 500000, 200),
    ("44990", 2, "09:24:50", 23540.0, 950000, 200), ("44990", 2, "09:29:50", 23580.0, 1350000, 200),
    ("44990", 2, "09:30:30", 23600.0, 1400000, 200),
    ("2885", 1, "09:15:05", 1495.0, 300000, 200), ("2885", 1, "09:18:00", 1510.0, 600000, 200),
    ("2885", 1, "09:22:00", 1480.0, 900000, 200), ("2885", 1, "09:28:00", 1500.0, 1100000, 200),
    ("2885", 1, "09:30:59", 1505.0, 1200000, 200),
]


def at(hms):
    return int(smart.parse_ist("2026-11-24 " + hms).timestamp() * 1000)


def feed(until="23:59:59"):
    """The feed's tick store with every tick before `until` (HH:MM:SS)."""
    s = smart.TickStore(os.path.join("trader", "state", "ticks.sqlite"))
    for tok, ex, hms, px, vol, lag in TICKS:
        if hms < until:
            t = {"token": tok, "exchange_type": ex, "exch_ms": at(hms), "ltp": px}
            if vol is not None:
                t["volume"] = vol
            s.add(t, at(hms) + lag)
    s.close()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def skill_example(heading):
    """The ```text block under `heading` in in-scanner/SKILL.md."""
    with open(os.path.join(ROOT, "in-scanner", "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    return re.search(re.escape(heading) + r".*?```text\n(.*?)```", text, re.S).group(1)


def ok(data):
    return {"status": 200, "headers": {}, "body": {"status": True, "message": "SUCCESS", "errorcode": "", "data": data}}


def daily_rows(close, spread, last_volume=None, n=260):
    """n weekday sessions to Mon 2026-11-23, closes rising 10 a day to `close`, high and low +/- spread."""
    days, d = [], datetime.date(2026, 11, 23)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= datetime.timedelta(days=1)
    rows = []
    for i, d in enumerate(reversed(days)):
        c = close - 10.0 * (n - 1 - i)
        v = 0 if last_volume is None else (last_volume if i == n - 1 else 100000)
        rows.append(["%sT00:00:00+05:30" % d, c - 5, c + spread, c - spread, c, v])
    return rows


def index_quote(exchange, name, token, open_, close):
    return {"exchange": exchange, "tradingSymbol": name, "symbolToken": token, "ltp": open_, "open": open_,
            "high": open_, "low": open_, "close": close, "tradeVolume": 0, "upperCircuit": 0.0, "lowerCircuit": 0.0,
            "depth": {"buy": [], "sell": []}}


class InTemp(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        self.off = os.path.join(self.tmp, "off")
        shutil.copytree(os.path.join(HERE, "orb"), self.off)
        shutil.copy(os.path.join(HERE, "master.json"), self.off)
        smart.write_json(os.path.join("trader", "config.json"),
                         smart.read_json(os.path.join(ROOT, "in-setup", "config.example.json")))

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ban(self, text):
        path = os.path.join("trader", "ban.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        t = smart.parse_ist(NOW).timestamp()
        os.utime(path, (t, t))   # kept today, as the pinned clock sees it

    def watchlist(self):
        return smart.read_json(os.path.join("trader", "watchlist.json"))


class Universe(InTemp):
    def test_the_example_in_skill_md(self):
        self.ban("SAIL\n# added by hand from the exchange's list\nXYZ\n")
        code, out, err = run(["universe", "--offline", HERE, "--now", "2026-11-24 08:50"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: the universe"))

    def test_the_files(self):
        self.ban("SAIL\n")
        run(["universe", "--offline", HERE, "--now", NOW])
        u = smart.read_json(os.path.join("trader", "universe.json"))
        self.assertEqual(u["removed_ban"], ["SAIL"])
        self.assertEqual(u["left_out"], ["TATAMOTORS: no NSE cash row in the master"])
        by = {i["symbol"]: i for i in u["items"]}
        self.assertNotIn("SAIL", by)
        self.assertEqual((by["NIFTY"]["lot_size"], by["NIFTY"]["volume_token"]), (65, "44990"))
        self.assertEqual((by["SENSEX"]["fo_segment"], by["SENSEX"]["volume_segment"]), ("BFO", "BFO"))
        self.assertEqual(by["RELIANCE"]["lot_size"], 500)        # the nearest expiry's lot, read from the master
        self.assertEqual(self.watchlist()["mode"], "universe")

    def test_an_old_ban_list_is_questioned(self):
        self.ban("SAIL\n")
        old = smart.parse_ist("2026-11-20 18:00").timestamp()
        os.utime(os.path.join("trader", "ban.txt"), (old, old))
        _, out, _ = run(["universe", "--offline", HERE, "--now", NOW])
        self.assertIn("  Ban list       trader/ban.txt last changed 2026-11-20: is it today's list?", out)


class Orb(InTemp):
    def test_the_example_in_skill_md(self):
        feed()
        code, out, err = run(["orb", "--offline", self.off, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: the opening range"))

    def test_the_watchlist_file(self):
        feed()
        run(["orb", "--offline", self.off, "--now", NOW])
        w = self.watchlist()
        self.assertEqual((w["mode"], w["source"], w["partial"], w["partial_note"]), ("orb", "feed + candles", False, None))
        self.assertEqual(w["range"], {"start": "09:15", "end": "09:30"})
        n = w["items"][0]
        self.assertEqual((n["symbol"], n["range_high"], n["range_low"], n["last"], n["feed_lag_ms"], n["stale"]),
                         ("NIFTY", 23550.0, 23480.0, 23562.5, 250, False))
        self.assertEqual((n["token"], n["segment"], n["fo_segment"], n["lot_size"]), ("99926000", "NSE", "NFO", 65))
        b = w["items"][2]
        self.assertEqual((b["symbol"], b["source"], b["last"], b["stale"]), ("BANKNIFTY", "candles", None, True))

    def test_index_volume_comes_from_its_future(self):
        feed()
        run(["orb", "--offline", self.off, "--now", NOW])
        n = self.watchlist()["items"][0]
        # the index ticks carry no volume; NIFTY24NOV26FUT traded 1,350,000 in the range against 1,000,000 a day
        self.assertEqual((n["volume_symbol"], n["volume_ratio"]), ("NIFTY24NOV26FUT", 1.35))

    def test_before_0930_the_range_is_forming_and_nothing_is_complete(self):
        feed("09:22:00")
        code, out, err = run(["orb", "--offline", self.off, "--now", "2026-11-24 09:22"])
        self.assertEqual(code, 0, err)
        self.assertIn("  NIFTY          forming: 23,500.00 to 23,550.00 so far (1 of 3 bars)", out)
        self.assertIn("  BANKNIFTY      forming: not in the live feed; candles back-fill it after the range ends", out)
        w = self.watchlist()
        self.assertEqual((w["partial"], w["partial_note"]), (True, "opening range forming until 09:30"))
        self.assertTrue(all(i["range_high"] is None and i["range_low"] is None for i in w["items"]))

    def test_a_failed_symbol_makes_the_file_partial(self):
        feed()
        os.remove(os.path.join(self.off, "getCandleData.json"))
        code, out, err = run(["orb", "--offline", self.off, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertIn("  BANKNIFTY      FAILED: offline: no getCandleData.json in", out)
        self.assertIn("\nFetched 2 of 3.\n", out)
        self.assertIn("Wrote trader/watchlist.json (orb, 2 items, partial)", out)
        w = self.watchlist()
        self.assertTrue(w["partial"])
        self.assertTrue(w["partial_note"].startswith("Fetched 2 of 3; BANKNIFTY: offline: no getCandleData.json"))
        self.assertIn("SENSEX volume ratio: offline: no getCandleData.json", w["partial_note"])
        self.assertEqual([i["symbol"] for i in w["items"]], ["NIFTY", "SENSEX"])

    def test_nothing_fetched_is_one_line_and_exit_1(self):
        os.remove(os.path.join(self.off, "getCandleData.json"))
        code, out, err = run(["orb", "--offline", self.off, "--now", NOW])
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("in-scanner: Fetched 0 of 3; NIFTY: offline: no getCandleData.json"), err)
        self.assertEqual(err.count("\n"), 1)
        self.assertTrue(self.watchlist()["partial"])          # yesterday's file is not left looking current

    def test_a_weekend_has_no_opening_range(self):
        code, out, err = run(["orb", "--offline", self.off, "--now", "2026-11-28 09:31"])
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "in-scanner: no session today (closed: weekend): the opening range needs a trading "
                              "day; daily works any day\n")

    def test_a_live_run_refuses_what_the_probe_did_not_confirm(self):
        os.makedirs(os.path.join("trader", "cache"))
        shutil.copy(os.path.join(HERE, "master.json"), os.path.join("trader", "cache", "instrument_master_2026-11-24.json"))
        code, out, err = run(["orb", "--now", NOW])           # no --offline, and no probe has run
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "in-scanner: getCandleData is not confirmed on this account yet: run in-setup probe "
                              "in market hours first\n")

    def test_it_asks_the_feed_for_what_it_scans(self):
        feed()
        run(["orb", "--offline", self.off, "--now", NOW])
        subs = smart.read_json(os.path.join("trader", "state", "subscriptions.json"))["in-scanner"]
        self.assertEqual([(s["segment"], s["token"]) for s in subs],
                         [("NSE", "99926000"), ("BSE", "99919000"), ("NSE", "99926009"), ("NFO", "44990"),
                          ("BFO", "1100"), ("NFO", "44991")])


class Stocks(InTemp):
    def test_filters_ban_grade_and_most_liquid_first(self):
        feed()
        self.ban("SAIL\n")
        smart.write_json(os.path.join("trader", "news.json"), {
            "as_of_ist": "2026-11-24T09:05:00+05:30", "symbols": {"INFY": {"flags": ["RESULT_TODAY"]}}})
        code, out, err = run(["orb", "--stocks", "--offline", self.off, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertIn("  RELIANCE       1,480.00 to 1,510.00 (feed); last 1,505.00, lag 200 ms; volume ratio 1.10; "
                      "grade A (spread 0.03%, average volume 1,200,000)", out)
        self.assertIn("  Skipped        IDEA (cautionary: Stage 1 Short Term ASM scrip), INFY (RESULT_TODAY in "
                      "news.json), SBIN (spread 0.62% over 0.50%)", out)
        w = self.watchlist()
        self.assertEqual([i["symbol"] for i in w["items"]], ["NIFTY", "SENSEX", "BANKNIFTY", "RELIANCE"])
        r = w["items"][3]
        self.assertEqual((r["kind"], r["liquidity_grade"], r["spread_pct"], r["lot_size"]), ("stock", "A", 0.027, 500))
        self.assertNotIn("SAIL", out)                          # banned: never scanned
        self.assertEqual(set(w["skipped"]), {"IDEA", "INFY", "SBIN"})

    def test_yesterdays_news_flags_are_ignored(self):
        feed()
        smart.write_json(os.path.join("trader", "news.json"), {
            "as_of_ist": "2026-11-23T09:05:00+05:30", "symbols": {"INFY": {"flags": ["RESULT_TODAY"]}}})
        _, out, _ = run(["orb", "--stocks", "--offline", self.off, "--now", NOW])
        self.assertNotIn("INFY (RESULT_TODAY", out)

    def test_config_filters_win_over_the_defaults(self):
        feed()
        cfg = smart.read_json(os.path.join("trader", "config.json"))
        cfg["scanner"] = {"max_spread_pct": 1.0, "min_avg_volume": 2000000}
        smart.write_json(os.path.join("trader", "config.json"), cfg)
        _, out, _ = run(["orb", "--stocks", "--offline", self.off, "--now", NOW])
        self.assertIn("RELIANCE (average volume 1,200,000 under 2,000,000)", out)
        self.assertIn("SBIN (average volume 1,200,000 under 2,000,000)", out)   # the spread passes at 1%

    def test_the_grade(self):
        self.assertEqual([scan.grade(s, v) for s, v in ((0.03, 1500000), (0.03, 500000), (0.3, 5000000),
                                                         (None, 1))], ["A", "B", "C", None])
        q = {"depth": {"buy": [{"price": 99.95}], "sell": [{"price": 100.05}]}}
        self.assertEqual(scan.spread_pct(q), 0.1)
        self.assertIsNone(scan.spread_pct({"depth": {"buy": [], "sell": []}}))


def daily_run(now):
    """A daily scan in a fresh folder: (exit code, stdout, stderr, watchlist.json)."""
    old, tmp = os.getcwd(), tempfile.mkdtemp()
    os.chdir(tmp)
    try:
        off = os.path.join(tmp, "off")
        os.makedirs(off)
        shutil.copy(os.path.join(HERE, "master.json"), off)
        smart.write_json(os.path.join("trader", "config.json"),
                         smart.read_json(os.path.join(ROOT, "in-setup", "config.example.json")))
        smart.write_json(os.path.join(off, "getCandleData.json"), [   # in call order: index, then its future
            ok(daily_rows(23380.0, 60)), ok(daily_rows(23400.0, 60, 125000)),
            ok(daily_rows(77480.0, 200)), ok(daily_rows(77500.0, 200, 80000)),
            ok(daily_rows(52090.0, 150)), ok(daily_rows(52100.0, 150, 150000))])
        smart.write_json(os.path.join(off, "quote.json"), ok({"fetched": [
            index_quote("NSE", "Nifty 50", "99926000", 23450.0, 23380.0),
            index_quote("BSE", "SENSEX", "99919000", 77400.0, 77480.0),
            index_quote("NSE", "Nifty Bank", "99926009", 52050.0, 52090.0)], "unfetched": []}))
        return run(["daily", "--offline", off, "--now", now]) + (smart.read_json(os.path.join("trader", "watchlist.json")),)
    finally:
        os.chdir(old)
        shutil.rmtree(tmp, ignore_errors=True)


class Daily(unittest.TestCase):
    """Each run gets a fresh folder: smart.candles caches a file per calendar day, and reading
    6 symbols of 400 days back is slow on Windows."""

    @classmethod
    def setUpClass(cls):
        cls.code, cls.out, cls.err, cls.w = daily_run("2026-11-24 09:20")

    def test_the_example_in_skill_md(self):
        self.assertEqual(self.code, 0, self.err)
        self.assertEqual(self.out, skill_example("## Example: the daily scan"))

    def test_the_numbers(self):
        n = self.w["items"][0]
        # true range is the high-low span (120) every day: the closes rise only 10 a day
        self.assertEqual((n["prev_close"], n["atr14"], n["volume_ratio"], n["gap_pct"]), (23380.0, 120.0, 1.25, 0.3))
        self.assertEqual((n["high_52w"], n["low_52w"], n["sessions_52w"]), (23440.0, 20730.0, 260))
        self.assertEqual((self.w["mode"], self.w["session"], self.w["partial"]), ("daily", "2026-11-23", False))

    def test_before_the_open_there_is_no_gap(self):
        _, out, _, w = daily_run("2026-11-24 08:30")
        self.assertIn("  Session        ONE_DAY candles to Mon 2026-11-23; gap not yet (closed: outside 09:00 to "
                      "15:45 IST)", out)
        self.assertIsNone(w["items"][0]["gap_pct"])

    def test_wilder_atr_by_hand(self):
        bars = [["d", 0, 11.0, 9.0, 10.0, 0]] * 15 + [["d", 0, 16.0, 10.0, 15.0, 0]]
        # 14 true ranges of 2, then one of 6: (2 * 13 + 6) / 14
        self.assertAlmostEqual(scan.wilder_atr(bars), 32 / 14)
        self.assertIsNone(scan.wilder_atr(bars[:14]))


if __name__ == "__main__":
    unittest.main()
