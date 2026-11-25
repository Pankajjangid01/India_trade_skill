"""in-common tests, all offline: the client's retry and breaker, token recovery,
.env, candles cache, calendar, money, Greeks, the tick store, subscriptions, and
the gate check that two processes sharing one limiter never exceed a cap."""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal

from _load import ROOT

sys.path.insert(0, os.path.join(ROOT, "in-common"))
import smart  # noqa: E402

IST = smart.IST
NOW = smart.parse_ist("2026-11-24 10:20")
EPOCH = NOW.timestamp()


class InTemp(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)


def body(ok=True, data=None, code=""):
    return {"status": ok, "message": "SUCCESS" if ok else "failed", "errorcode": code, "data": data}


class Scripted:
    """A transport that answers from a list of (status, body) per endpoint path tail."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def __call__(self, method, url, headers=None, data=None):
        name = url.rstrip("/").rsplit("/", 1)[-1]
        self.calls.append((name, json.loads(data) if data else None, (headers or {}).get("Authorization")))
        status, b = self.script[name].pop(0) if len(self.script[name]) > 1 else self.script[name][0]
        return status, {}, json.dumps(b).encode()


def client(script, t=None):
    t = t if t is not None else [EPOCH]
    waits = []

    def sleep(s):
        waits.append(s)
        t[0] += s
    c = smart.Client("key", transport=Scripted(script), clock=lambda: t[0], sleep=sleep, rand=lambda: 0.5)
    c.jwt = "jwt-1"
    return c, waits, t


class Retry(unittest.TestCase):
    def test_rate_limit_403_is_retried_with_backoff(self):
        c, waits, _ = client({"quote": [(403, {"message": "Access denied"}), (403, {}), (200, body(data={}))]})
        self.assertTrue(c.call("quote", {}).ok)
        self.assertEqual(waits, [2.5, 4.5])

    def test_5xx_and_ab2001_retry_three_times_then_give_back_the_failure(self):
        c, waits, _ = client({"quote": [(502, {})]})
        r = c.call("quote", {})
        self.assertEqual((r.ok, r.status, waits), (False, 502, [2.5, 4.5, 8.5]))
        c, waits, _ = client({"quote": [(200, body(False, code="AB2001")), (200, body(data={}))]})
        self.assertTrue(c.call("quote", {}).ok)
        self.assertEqual(waits, [2.5])

    def test_a_bad_request_is_never_retried(self):
        c, waits, _ = client({"quote": [(200, body(False, code="AB1009"))]})
        self.assertEqual((c.call("quote", {}).errorcode, waits), ("AB1009", []))

    def test_retry_false_sends_once(self):
        c, waits, _ = client({"quote": [(403, {})]})
        self.assertEqual((c.call("quote", {}, retry=False).status, waits, len(c.transport.calls)), (403, [], 1))

    def test_three_failures_pause_the_endpoint_for_60_s(self):
        c, _, t = client({"optionGreek": [(200, body(False, code="AB1009"))], "quote": [(200, body(data={}))]})
        for _ in range(3):
            c.call("optionGreek", {})
        sent = len(c.transport.calls)
        r = c.call("optionGreek", {})
        self.assertEqual((r.errorcode, len(c.transport.calls)), ("PAUSED", sent))
        self.assertTrue(c.call("quote", {}).ok)            # other endpoints are not paused
        t[0] += 61
        self.assertEqual(c.call("optionGreek", {}).errorcode, "AB1009")


class Recovery(InTemp):
    def session(self, script, data=None):
        c, waits, t = client(script)
        sess = smart.Session(c, {"api_key": "key", "client_code": "C", "pin": "1", "totp_secret": "JBSWY3DPEHPK3PXP"},
                             "session.json", "2026-11-24")
        sess.data = data or {"date_ist": "2026-11-24", "key": smart.key_fingerprint("key"), "jwt": "jwt-1",
                             "refresh": "ref-1", "feed": "feed-1"}
        smart.write_json("session.json", sess.data)
        return c, sess

    def test_expired_token_is_refreshed_once_and_the_call_retried(self):
        c, sess = self.session({"quote": [(200, body(False, code="AG8002")), (200, body(data={"x": 1}))],
                                "generateTokens": [(200, body(data={"jwtToken": "jwt-2", "refreshToken": "ref-2",
                                                                    "feedToken": "feed-2"}))]})
        self.assertEqual(c.call("quote", {}).data, {"x": 1})
        names = [n for n, _, _ in c.transport.calls]
        self.assertEqual(names, ["quote", "generateTokens", "quote"])
        self.assertEqual(c.transport.calls[1][1], {"refreshToken": "ref-1"})
        self.assertEqual(c.transport.calls[2][2], "Bearer jwt-2")
        self.assertEqual(smart.read_json("session.json")["jwt"], "jwt-2")

    def test_a_token_refreshed_by_another_process_is_picked_up(self):
        c, sess = self.session({"quote": [(200, body(False, code="AG8001")), (200, body(data={}))]})
        smart.write_json("session.json", dict(sess.data, jwt="jwt-other"))
        self.assertTrue(c.call("quote", {}).ok)
        self.assertEqual([n for n, _, _ in c.transport.calls], ["quote", "quote"])

    def test_refresh_expired_logs_in_again_once_and_never_loops(self):
        c, sess = self.session({"quote": [(200, body(False, code="AB8051"))],
                                "loginByPassword": [(200, body(data={"jwtToken": "jwt-3", "refreshToken": "r3",
                                                                     "feedToken": "f3"}))]})
        r = c.call("quote", {})
        self.assertFalse(r.ok)
        self.assertEqual(c.call("quote", {}).errorcode, "AB8051")
        names = [n for n, _, _ in c.transport.calls]
        self.assertEqual(names.count("loginByPassword"), 1)


class DotEnv(InTemp):
    def test_env_file_fills_what_the_environment_lacks(self):
        with open(".env", "w", encoding="utf-8") as fh:
            fh.write("# keys\nexport ANGEL_API_KEY='from-file'\nANGEL_CLIENT_CODE=\"C1\"\nANGEL_PIN=1234\nOTHER=x\n")
        c = smart.credentials(env={"ANGEL_API_KEY": "from-env"})
        self.assertEqual((c["api_key"], c["client_code"], c["pin"], c["totp_secret"]), ("from-env", "C1", "1234", None))
        self.assertEqual(smart.read_env_file("missing.env"), {})

    def test_still_missing_names_the_variables_never_values(self):
        with open(".env", "w", encoding="utf-8") as fh:
            fh.write("ANGEL_API_KEY=secret-value\n")
        with self.assertRaises(smart.SmartError) as e:
            smart.credentials(env={})
        self.assertIn("ANGEL_CLIENT_CODE, ANGEL_PIN", str(e.exception))
        self.assertNotIn("secret-value", str(e.exception))


class Calendar(unittest.TestCase):
    def test_trading_days(self):
        cal = {"holidays": {"2026-11-24": "x"}, "session_overrides": {"2026-11-21": "special Saturday"}}
        d = datetime.date
        self.assertFalse(smart.trading_day(d(2026, 11, 24), cal))
        self.assertTrue(smart.trading_day(d(2026, 11, 21), cal))
        self.assertFalse(smart.trading_day(d(2026, 11, 22), cal))
        self.assertEqual(smart.previous_trading_day(d(2026, 11, 25), cal), d(2026, 11, 23))
        self.assertEqual(smart.previous_trading_day(d(2026, 11, 23), cal), d(2026, 11, 21))


class Candles(InTemp):
    def bars(self, day, times):
        return [["%sT%s:00+05:30" % (day, hm), 1.0, 2.0, 0.5, 1.5, 0] for hm in times]

    def test_past_days_are_cached_forever_and_today_keeps_closed_bars(self):
        data = self.bars("2026-11-20", ["09:15", "09:20"]) + self.bars("2026-11-23", ["09:15"]) + \
            self.bars("2026-11-24", ["10:15", "10:20"])
        c, _, _ = client({"getCandleData": [(200, body(data=data))]})
        days, partial = smart.candles(c, "cache", "NSE", "99926000", "FIVE_MINUTE",
                                      datetime.date(2026, 11, 20), datetime.date(2026, 11, 24), NOW)
        self.assertIsNone(partial)
        self.assertEqual(len(c.transport.calls), 1)                        # one call for the whole span
        self.assertEqual(c.transport.calls[0][1]["fromdate"], "2026-11-20 00:00")
        self.assertEqual(c.transport.calls[0][1]["todate"], "2026-11-24 10:20")
        self.assertEqual([len(days[d]) for d in sorted(days)], [2, 0, 0, 1, 1])  # the 10:20 bar is still open
        self.assertEqual(days["2026-11-21"], [])                           # a weekend, cached empty
        c2, _, _ = client({"getCandleData": [(200, body(data=self.bars("2026-11-24", ["10:10"])))]})
        days2, _ = smart.candles(c2, "cache", "NSE", "99926000", "FIVE_MINUTE",
                                 datetime.date(2026, 11, 20), datetime.date(2026, 11, 24), NOW)
        self.assertEqual(c2.transport.calls[0][1]["fromdate"], "2026-11-24 00:00")   # only today is fetched
        self.assertEqual(days2["2026-11-20"], days["2026-11-20"])

    def test_spans_respect_the_interval_day_limit(self):
        days = [datetime.date(2026, 1, 1) + datetime.timedelta(days=i) for i in range(70)]
        self.assertEqual([len(s) for s in smart._spans(days, 30)], [30, 30, 10])

    def test_a_failure_is_partial_and_caches_nothing(self):
        c, _, _ = client({"getCandleData": [(200, body(False, code="AB1009"))]})
        days, partial = smart.candles(c, "cache", "NSE", "1", "FIVE_MINUTE",
                                      datetime.date(2026, 11, 23), datetime.date(2026, 11, 23), NOW)
        self.assertEqual(days, {})
        self.assertIn("AB1009", partial)
        self.assertFalse(os.path.exists("cache"))


class Money(unittest.TestCase):
    def test_tick_rounding(self):
        self.assertEqual(smart.round_tick("112.43", "0.05"), Decimal("112.45"))
        self.assertEqual(smart.round_tick("112.43", "0.05", "down"), Decimal("112.40"))
        self.assertEqual(smart.round_tick("112.41", "0.05", "up"), Decimal("112.45"))
        self.assertEqual(smart.tick_inr({"tick_size": "5.000000"}, 100), Decimal("0.05"))

    def test_lots_per_order_and_the_split(self):
        self.assertEqual(smart.max_lots_per_order("1801", "65"), 27)
        self.assertEqual(smart.max_lots_per_order("1000", "20"), 49)
        self.assertEqual(smart.split_lots(60, 27), [27, 27, 6])
        self.assertEqual(smart.split_lots(54, 27), [27, 27])

    def test_option_charges_against_hand_arithmetic(self):
        ch = smart.read_json(os.path.join(ROOT, "in-setup", "charges.example.json"))
        buy = smart.option_charges("BUY", 112.4, 65, "NFO", ch)    # turnover 7,306.00
        self.assertEqual([str(buy[k]) for k in buy], ["30.00", "2.60", "0.01", "0.00", "0.22", "5.87", "38.70"])
        sell = smart.option_charges("SELL", 112.4, 65, "NFO", ch)  # STT 0.15% = 10.959
        self.assertEqual((sell["stt"], sell["stamp"], sell["total"]), (Decimal("10.96"), Decimal("0.00"), Decimal("49.44")))
        with self.assertRaises(smart.SmartError):
            smart.option_charges("BUY", 1, 1, "NFO", dict(ch, gst_pct=None))


class Greeks(unittest.TestCase):
    def test_textbook_values(self):   # Hull, Options Futures and Other Derivatives, example 15.6
        c = smart.black_scholes("CE", 42, 40, 0.5, 0.10, 0.20)
        p = smart.black_scholes("PE", 42, 40, 0.5, 0.10, 0.20)
        self.assertEqual((round(c["price"], 2), round(p["price"], 2), round(c["delta"], 4)), (4.76, 0.81, 0.7791))
        parity = c["price"] - p["price"] - (42 - 40 * 2.718281828459045 ** (-0.05))
        self.assertAlmostEqual(parity, 0, places=9)
        self.assertAlmostEqual(c["gamma"], p["gamma"])
        self.assertLess(c["theta"], 0)

    def test_implied_volatility(self):
        price = smart.black_scholes("PE", 23500, 23400, 7 / 365, 0.065, 0.142)["price"]
        self.assertAlmostEqual(smart.implied_vol("PE", price, 23500, 23400, 7 / 365, 0.065), 0.142, places=5)
        self.assertIsNone(smart.implied_vol("CE", 50, 23500, 23400, 7 / 365, 0.065))   # below intrinsic
        self.assertIsNone(smart.implied_vol("CE", 150, 23500, 23400, 0, 0.065))

    def test_time_to_expiry_and_the_rate(self):
        self.assertAlmostEqual(smart.year_fraction(NOW, datetime.date(2026, 11, 26)), (2 * 86400 + 5 * 3600 + 600) / (365 * 86400))
        self.assertEqual(smart.year_fraction(NOW, datetime.date(2026, 11, 23)), 0.0)
        with self.assertRaises(smart.SmartError):
            smart.risk_free_rate({"api": {"risk_free_rate_pct": None}})
        self.assertAlmostEqual(smart.risk_free_rate({"api": {"risk_free_rate_pct": 6.5}}), 0.065)


def tick(token, ex, exch_ms, ltp, volume=None, oi=None):
    t = {"token": token, "exchange_type": ex, "exch_ms": exch_ms, "ltp": ltp}
    if volume is not None:
        t["volume"] = volume
    if oi is not None:
        t["oi"] = oi
    return t


def at(hm):
    return int(smart.parse_ist("2026-11-24 " + hm).timestamp() * 1000)


class TickStore(InTemp):
    def test_latest_lag_age_and_stale(self):
        s = smart.TickStore("t.sqlite")
        s.add(tick("A", 1, at("10:19:58"), 100.0), at("10:19:58") + 250)
        s.add(tick("B", 2, at("10:19:50"), 5.0, oi=10), at("10:19:50") + 900)
        s.flush()
        got = smart.read_latest("t.sqlite", ["A", "B", "C"], at("10:20:00"), 3000)
        self.assertEqual(sorted(got), ["A", "B"])
        self.assertEqual((got["A"]["lag_ms"], got["A"]["age_ms"], got["A"]["stale"]), (250, 1750, False))
        self.assertEqual((got["B"]["lag_ms"], got["B"]["stale"], got["B"]["oi"]), (900, True, 10))
        self.assertEqual(smart.exchange_clock("t.sqlite"), at("10:19:58"))
        self.assertEqual(smart.read_latest("none.sqlite", ["A"], 0, 1), {})

    def test_five_minute_bars_closed_only_with_volume_deltas(self):
        s = smart.TickStore("t.sqlite")
        ticks = [("09:15:05", 10.0, 100), ("09:17:00", 12.0, 150), ("09:19:59", 11.0, 400), ("09:20:10", 11.5, 450),
                 ("09:20:40", 11.8, 470), ("09:22:00", 9.0, 600), ("09:24:30", 9.5, 700), ("09:25:01", 9.6, 720)]
        for i, (hm, px, vol) in enumerate(ticks):
            s.add(tick("A", 2, at(hm), px, vol), at(hm) + 100)
            if i == 3:
                s.flush()   # a bar split across two flushes still merges
        s.flush()
        bars = smart.read_bars("t.sqlite", "A", 5, at("09:00"), smart.exchange_clock("t.sqlite"))
        self.assertEqual(bars, [[at("09:15"), 10.0, 12.0, 10.0, 11.0, 400, None, 3],
                                [at("09:20"), 11.5, 11.8, 9.0, 9.5, 300, None, 3]])   # 09:25 is still open
        self.assertEqual(smart.read_bars("t.sqlite", "A", 5, at("09:20"), at("09:30"))[0][5], None)  # no 09:15 to diff


class Subscriptions(InTemp):
    def test_union_richer_mode_wins_and_the_cap(self):
        smart.subscribe("trader", "chain", [{"segment": "NFO", "token": "45004", "mode": "snapquote"},
                                            {"segment": "nse_cm", "token": "99926000", "mode": "quote"}])
        smart.subscribe("trader", "scanner", [{"segment": "NSE", "token": "99926000", "mode": "ltp"},
                                              {"segment": "NSE", "token": "2885"}])
        want, dropped = smart.wanted("trader", 10)
        self.assertEqual(dict(want), {(2, "45004"): 3, (1, "99926000"): 2, (1, "2885"): 2})
        self.assertEqual(dropped, 0)
        self.assertEqual(smart.wanted("trader", 2)[1], 1)
        smart.subscribe("trader", "chain", [])
        self.assertEqual(dict(smart.wanted("trader", 10)[0]), {(1, "99926000"): 1, (1, "2885"): 2})

    def test_require_refuses_what_the_probe_did_not_confirm(self):
        with self.assertRaises(smart.SmartError) as e:
            smart.require("trader", "stream")
        self.assertIn("run in-setup probe", str(e.exception))
        smart.write_json(os.path.join("trader", "state", "capabilities.json"), {"confirmed": {"stream": ["feed"]}})
        self.assertEqual(smart.require("trader", "stream"), ["feed"])


CHILD = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
import smart
lim = smart.Limiter(sys.argv[2], {"x": ((2, 1),)})
for _ in range(4):
    lim.wait("x")
"""


class TwoProcesses(InTemp):
    def test_two_processes_together_never_exceed_the_cap(self):   # the Phase 1 gate, offline
        procs = [subprocess.Popen([sys.executable, "-c", CHILD, os.path.join(ROOT, "in-common"), self.tmp])
                 for _ in range(2)]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        times = sorted(smart.read_json("ratelimit.json")["calls"]["x"])
        self.assertEqual(len(times), 8)
        for a, b in zip(times, times[2:]):   # any 3 calls span at least 1 s: never 3 in one second
            self.assertGreaterEqual(b - a, 1.0 - 1e-6)


if __name__ == "__main__":
    unittest.main()
