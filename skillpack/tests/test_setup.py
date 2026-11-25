"""in-setup and in-common tests, all offline.

The fixtures in tests/fixtures/in-setup are synthetic: shaped like Angel One's
documented replies, with no real data and no real secrets. After the first
live run, `firstrun.py probe --record DIR` saves the real replies with the
tokens and the profile redacted; swap them in and these tests run on them.
"""

import base64
import datetime
import io
import os
import platform
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

firstrun = load("in-setup", "firstrun")
smart = firstrun.smart
OFF = os.path.join(FIX, "in-setup")
NOW = "2026-11-24 10:20"   # a Tuesday: NIFTY's weekly expiry in the fixture master
FAKE = {"ANGEL_API_KEY": "APIKEY-NOT-REAL-7", "ANGEL_CLIENT_CODE": "CLIENT-NOT-REAL-7",
        "ANGEL_PIN": "PIN-NOT-REAL-7", "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP"}
FIXTURE_TOKENS = ("FIXTURE-JWT-NOT-REAL", "FIXTURE-REFRESH-NOT-REAL", "FIXTURE-FEED-NOT-REAL")


def run(argv, env=None):
    """firstrun.main with only the given ANGEL_* variables set."""
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("ANGEL_")}
    os.environ.update(env or {})
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = firstrun.main(argv)
    finally:
        for k in [k for k in os.environ if k.startswith("ANGEL_")]:
            del os.environ[k]
        os.environ.update(saved)
    return code, out.getvalue(), err.getvalue()


def skill_example(heading):
    """The ```text block under `heading` in in-setup/SKILL.md."""
    with open(os.path.join(ROOT, "in-setup", "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(re.escape(heading) + r".*?```text\n(.*?)```", text, re.S)
    return m.group(1)


class InTemp(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)


class Totp(unittest.TestCase):
    def test_rfc6238_vectors(self):
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(smart.totp(secret, at=59, digits=8), "94287082")
        self.assertEqual(smart.totp(secret, at=1111111109, digits=8), "07081804")
        self.assertEqual(smart.totp(secret, at=1111111109), "081804")
        self.assertEqual(smart.totp(" " + secret.lower(), at=59, digits=8), "94287082")


class Clock(unittest.TestCase):
    def test_ist_is_a_fixed_offset(self):
        self.assertEqual(smart.parse_ist(NOW).utcoffset(), datetime.timedelta(hours=5, minutes=30))
        self.assertEqual(smart.parse_ist("2026-11-24T04:50:00+00:00").strftime("%H:%M"), "10:20")
        with self.assertRaises(smart.SmartError):
            smart.parse_ist("tomorrow")

    def test_market_data_window(self):
        cal = {"holidays": {"2026-11-25": "test holiday"}, "session_overrides": {"2026-11-28": "special Saturday"}}

        def f(t):
            return smart.market_data_allowed(smart.parse_ist(t), cal)
        self.assertEqual(f("2026-11-24 10:20"), (True, "open"))
        self.assertFalse(f("2026-11-24 08:59")[0])
        self.assertTrue(f("2026-11-24 15:44")[0])
        self.assertFalse(f("2026-11-24 15:45")[0])
        self.assertEqual(f("2026-11-25 10:00"), (False, "closed: holiday (test holiday)"))
        self.assertEqual(f("2026-11-29 10:00"), (False, "closed: weekend"))
        self.assertEqual(f("2026-11-28 10:00"), (True, "open"))

    def test_clock_check_from_the_date_header(self):
        epoch = smart.parse_ist(NOW).timestamp()
        r = smart.Reply("getProfile", 200, {"date": "Tue, 24 Nov 2026 04:50:00 GMT"}, {}, 0, epoch)
        self.assertEqual(smart.clock_check(r)["offset_sec"], 0.5)
        self.assertTrue(smart.clock_check(r)["ok"])
        r.received = epoch - 3
        self.assertFalse(smart.clock_check(r)["ok"])
        self.assertIsNone(smart.clock_check(smart.Reply("x", 200, {}, {}, 0, epoch))["ok"])


class Files(InTemp):
    def test_atomic_write_leaves_only_the_file(self):
        smart.write_json(os.path.join("d", "x.json"), {"a": 1})
        self.assertEqual(os.listdir("d"), ["x.json"])
        self.assertEqual(smart.read_json(os.path.join("d", "x.json")), {"a": 1})
        self.assertEqual(smart.read_json("missing.json", {}), {})

    def test_a_stale_lock_is_broken(self):
        with open("l.lock", "w"):
            pass
        old = os.path.getmtime("l.lock") - 60
        os.utime("l.lock", (old, old))
        with smart.FileLock("l.lock"):
            self.assertTrue(os.path.exists("l.lock"))
        self.assertFalse(os.path.exists("l.lock"))


class Limit(InTemp):
    def setUp(self):
        super().setUp()
        self.t, self.slept = [smart.parse_ist(NOW).timestamp()], []

    def lim(self, caps=None):
        def sleep(s):
            self.slept.append(round(s, 3))
            self.t[0] += s
        return smart.Limiter(".", caps, clock=lambda: self.t[0], sleep=sleep)

    def test_waits_for_every_window(self):
        lim = self.lim({"x": ((1, 1), (3, 60))})
        for _ in range(4):
            lim.wait("x")
        self.assertEqual(self.slept, [1.0, 1.0, 58.0])

    def test_two_processes_share_one_budget(self):
        a, b = self.lim(), self.lim()   # two instances on one ratelimit.json, as two skills would be
        a.wait("quote")
        b.wait("quote")
        a.wait("optionGreek")
        b.wait("optionGreek")
        self.assertEqual(self.slept, [1.0, 2.0])

    def test_caps_come_from_the_config(self):
        cfg = smart.read_json(os.path.join(ROOT, "in-setup", "config.example.json"))
        caps = smart.caps_from_config(cfg["limits"])
        self.assertEqual(caps["getCandleData"], ((1, 1), (28, 60), (1400, 3600)))
        self.assertEqual(caps["quote"], ((1, 1),))
        self.assertEqual(caps["optionGreek"], ((1, 2),))

    def test_usage_line(self):
        lim = self.lim()
        self.assertEqual(lim.usage_line(), "API usage today: no calls")
        lim.wait("getCandleData")
        lim.wait("getCandleData")
        self.assertEqual(lim.usage_line(), "API usage: getCandleData 2 today, 2 this hour (cap 1/s, 28/min, 1400/h)")


class Master(unittest.TestCase):
    def setUp(self):
        self.rows = smart.read_json(os.path.join(OFF, "master.json"))
        self.today = datetime.date(2026, 11, 24)

    def test_what_the_master_says(self):
        m = smart.describe_master(self.rows, self.today)
        self.assertEqual(m["strike_scale"], 100)
        self.assertEqual(m["strike_scale_agree"], [41, 41])   # the future and the cash rows are not options
        self.assertEqual(m["fields_missing"], [])
        self.assertEqual(m["unknown_segments"], [])
        n = m["underlyings"]["NIFTY"]
        self.assertEqual((n["index_token"], n["index_segment"], n["segment"]), ("99926000", "NSE", "NFO"))
        self.assertEqual((n["lot_size"], n["strike_step"], n["freeze_qty"]), (65, 50.0, 1801))
        self.assertEqual(n["expiries"], [["2026-11-24", "Tue"], ["2026-12-01", "Tue"]])  # 17 Nov has expired
        self.assertEqual(m["underlyings"]["SENSEX"]["expiries"], [["2026-11-26", "Thu"], ["2026-12-03", "Thu"]])
        self.assertEqual(m["underlyings"]["BANKNIFTY"]["expiries"], [["2026-11-24", "Tue"], ["2026-12-29", "Tue"]])

    def test_lot_size_is_read_never_assumed(self):
        for r in self.rows:
            if r["name"] == "NIFTY" and r["expiry"] == "24NOV2026":
                r["lotsize"] = "75"
        self.assertEqual(smart.describe_master(self.rows, self.today)["underlyings"]["NIFTY"]["lot_size"], 75)
        self.rows[6]["lotsize"] = "65"
        self.assertEqual(smart.describe_master(self.rows, self.today)["underlyings"]["NIFTY"]["lot_sizes_seen"], [65, 75])

    def test_a_strike_that_disagrees_is_counted(self):
        self.rows[6]["strike"] = "23400.000000"
        self.assertEqual(smart.describe_master(self.rows, self.today)["strike_scale_agree"], [40, 41])

    def test_segment_labels(self):
        self.assertEqual([smart.segment(s) for s in ("nse_fo", "BFO", "bse_cm", "odd")], ["NFO", "BFO", "BSE", "odd"])

    def test_nearest_option(self):
        pick = smart.nearest_option(self.rows, "NIFTY", 23512.35, 100, self.today)
        self.assertEqual(pick["symbol"], "NIFTY24NOV2623500CE")
        self.assertEqual(smart.nearest_option(self.rows, "NIFTY", 23380, 100, self.today, "PE")["symbol"],
                         "NIFTY24NOV2623400PE")
        self.assertEqual(smart.nearest_option(self.rows, "SENSEX", None, 100, self.today)["symbol"],
                         "SENSEX26NOV77200CE")


class FakeSock:
    def __init__(self, data, chunk=3):
        self.chunks = [data[i:i + chunk] for i in range(0, len(data), chunk)]
        self.sent = []

    def settimeout(self, t):
        pass

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, b):
        self.sent.append(b)


def unmask(frame):
    n, i = frame[1] & 0x7F, 2
    if n == 126:
        n, i = int.from_bytes(frame[2:4], "big"), 4
    mask = frame[i:i + 4]
    return bytes(c ^ mask[k % 4] for k, c in enumerate(frame[i + 4:i + 4 + n]))


class Stream(unittest.TestCase):
    def test_parse_a_hand_built_snapquote_packet(self):
        le = lambda v: v.to_bytes(8, "little", signed=True)  # noqa: E731
        b = bytes([3, 2]) + b"45004".ljust(25, b"\0") + le(7) + le(1795501199850) + le(11240)
        b += bytes(16) + le(12345) + bytes(16) + le(11000) + le(11500) + le(10900) + le(11240)
        b += bytes(8) + le(4512300) + bytes(8) + bytes(200) + le(60000) + le(5) + bytes(16)
        self.assertEqual(len(b), 379)
        t = smart.parse_tick(b)
        self.assertEqual((t["mode"], t["exchange_type"], t["token"], t["seq"]), (3, 2, "45004", 7))
        self.assertEqual((t["exch_ms"], t["ltp"], t["volume"], t["oi"]), (1795501199850, 112.4, 12345, 4512300))
        self.assertEqual((t["open"], t["high"], t["low"], t["upper_circuit"], t["lower_circuit"]),
                         (110.0, 115.0, 109.0, 600.0, 0.05))
        with self.assertRaises(ValueError):
            smart.parse_tick(b[:50])

    def test_frames_in_pieces_fragments_ping_and_close(self):
        big = bytes(range(200))
        server = (b"\x81\x04pong" + b"\x02\x02ab" + b"\x89\x02hi" + b"\x80\x02cd"
                  + b"\x82\x7e\x00\xc8" + big + b"\x88\x00")
        sock = FakeSock(server)
        ws = smart.WebSocket(sock)
        self.assertEqual(ws.recv(1)[:2], (1, b"pong"))
        self.assertEqual(ws.recv(1)[:2], (2, b"abcd"))      # the ping inside the fragments got its pong
        self.assertEqual(sock.sent[0][0], 0x8A)
        self.assertEqual(unmask(sock.sent[0]), b"hi")
        self.assertEqual(ws.recv(1)[:2], (2, big))
        self.assertEqual(ws.recv(1)[0], 8)

    def test_client_frames_are_masked(self):
        sock = FakeSock(b"")
        ws = smart.WebSocket(sock)
        ws.send("ping")
        ws.send("x" * 300)
        self.assertEqual(sock.sent[0][:2], b"\x81\x84")
        self.assertEqual(unmask(sock.sent[0]), b"ping")
        self.assertEqual(sock.sent[1][1], 0x80 | 126)
        self.assertEqual(unmask(sock.sent[1]), b"x" * 300)


class ClientAndRecording(InTemp):
    def test_replay_goes_in_call_order(self):
        c = smart.Client("k", transport=smart.Replay(OFF))
        c.jwt = "j"
        first, second, third = (c.call("quote", {}) for _ in range(3))
        self.assertEqual(first.data["fetched"][0]["symbolToken"], "99926000")
        self.assertEqual(second.data["fetched"][0]["tradingSymbol"], "NIFTY24NOV2623500CE")
        self.assertEqual(third.data, second.data)

    def test_record_redacts_tokens_and_the_profile(self):
        c = smart.Client("k", transport=smart.Replay(OFF), record="rec")
        c.call("loginByPassword", {"clientcode": "x"})
        c.jwt = "j"
        c.call("getProfile")
        c.call("optionGreek", {})
        login = smart.read_json(os.path.join("rec", "loginByPassword.json"))[0]["body"]["data"]
        self.assertEqual(set(login.values()), {"REDACTED"})
        prof = smart.read_json(os.path.join("rec", "getProfile.json"))[0]["body"]["data"]
        self.assertEqual(prof["exchanges"], ["REDACTED"] * 4)
        greeks = smart.read_json(os.path.join("rec", "optionGreek.json"))[0]["body"]["data"]
        self.assertEqual(greeks[0]["name"], "NIFTY")   # market data keeps its names

    def test_a_secure_call_needs_the_session(self):
        with self.assertRaises(smart.SmartError):
            smart.Client("k", transport=smart.Replay(OFF)).call("quote", {})


class Probe(InTemp):
    def probe(self, *extra, env=FAKE):
        return run(["probe", "--offline", OFF, "--now", NOW, "--key-label", "historical"] + list(extra), env)

    def test_the_example_in_skill_md(self):
        code, out, err = self.probe("--limits")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: the offline replay"))

    def test_the_capabilities_file(self):
        self.probe("--limits")
        caps = smart.read_json(os.path.join("trader", "state", "capabilities.json"))
        (fp, k), = caps["keys"].items()
        self.assertTrue(fp.startswith("offline-"))
        self.assertEqual(k["source"], "offline")
        self.assertEqual(caps["confirmed"], {})            # a replay never confirms anything
        self.assertEqual(caps["clock"]["offset_sec"], 0.5)
        self.assertEqual(caps["master"]["strike_scale"], 100)
        s = k["stream"]
        self.assertEqual(s["lag_ms"], {"median": 300, "worst": 950})
        self.assertEqual(s["exchange_types"], {"nse_cm": 1, "nse_fo": 2, "bse_cm": 3, "bse_fo": 4})
        self.assertEqual((s["pong_ms"], s["price_scale_ok"]), (85, True))
        self.assertIn("opnInterest", k["endpoints"]["quote_options"]["fields"])
        self.assertTrue(k["endpoints"]["getCandleData"]["index_volume_all_zero"])
        self.assertEqual(k["limits"]["quote_burst"]["accepted"], 5)

    def test_only_live_runs_count_as_confirmed(self):
        keys = {"a": {"label": "feed", "source": "live", "endpoints": {"quote": {"ok": True}},
                      "stream": {"ok": True, "exchange_types": {"nse_cm": 1, "bse_cm": None}}},
                "b": {"label": "hist", "source": "live", "endpoints": {"quote": {"ok": False}, "getCandleData": {"ok": True}}},
                "c": {"label": "fake", "source": "offline", "endpoints": {"optionGreek": {"ok": True}}}}
        self.assertEqual(firstrun.confirmed(keys), {"getCandleData": ["hist"], "quote": ["feed"],
                                                    "stream": ["feed"], "stream_nse_cm": ["feed"]})

    def test_no_secret_reaches_the_output_or_a_file(self):
        code, out, err = self.probe("--limits")
        self.assertEqual(code, 0, err)
        for secret in list(FAKE.values()) + list(FIXTURE_TOKENS):
            self.assertNotIn(secret, out + err)
        for base, _, files in os.walk("trader"):
            for f in files:
                with open(os.path.join(base, f), encoding="utf-8") as fh:
                    text = fh.read()
                for secret in FAKE.values():
                    self.assertNotIn(secret, text, f)
                if f != "session.offline.json":   # the day's tokens live there and nowhere else
                    for token in FIXTURE_TOKENS:
                        self.assertNotIn(token, text, f)

    def test_one_login_a_day_and_offline_never_touches_the_live_session(self):
        self.probe()
        self.probe()
        caps = smart.read_json(os.path.join("trader", "state", "capabilities.json"))
        (k,) = caps["keys"].values()
        self.assertEqual(k["endpoints"]["loginByPassword"], {"ok": True, "note": "today's session reused"})
        self.assertFalse(os.path.exists(os.path.join("trader", "state", "session.json")))

    def test_weekend_skips_every_live_call(self):
        code, out, err = run(["probe", "--offline", OFF, "--now", "2026-11-28 10:20"], FAKE)
        self.assertEqual(code, 0, err)
        self.assertIn("  quote           skipped (closed: weekend)", out)
        self.assertIn("  Stream          skipped (closed: weekend)", out)
        self.assertIn("  getCandleData   OK", out)                 # history is allowed
        self.assertIn("Run it again on a trading day", out)

    def test_holiday_from_the_calendar(self):
        run(["init"])
        smart.write_json(os.path.join("trader", "calendar.json"), {"holidays": {"2026-11-24": "test"}})
        code, out, _ = self.probe()
        self.assertIn("  Market          closed: holiday (test)", out)

    def test_missing_credentials_is_one_plain_line(self):
        code, out, err = run(["probe", "--now", NOW], {})
        self.assertEqual(code, 1)
        self.assertEqual(err, "in-setup: set ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN in the environment or in a "
                              ".env file in this folder (never in chat or in a file in the repo)\n")


class Init(InTemp):
    def test_the_example_in_skill_md(self):
        code, out, err = run(["init", "--now", NOW], {"ANGEL_API_KEY": "x", "ANGEL_CLIENT_CODE": "y", "ANGEL_PIN": "z"})
        self.assertEqual(code, 0, err)
        out = out.replace(" %s OK" % platform.python_version(), " 3.13.1 OK")   # the example's Python
        self.assertEqual(out, skill_example("## Example: the first run"))

    def test_never_overwrites_your_config(self):
        run(["init", "--now", NOW])
        path = os.path.join("trader", "config.json")
        cfg = smart.read_json(path)
        cfg["account"]["capital_inr"] = 500000
        smart.write_json(path, cfg)
        code, out, _ = run(["init", "--now", NOW])
        self.assertIn("config.json    kept (yours)", out)
        self.assertEqual(smart.read_json(path)["account"]["capital_inr"], 500000)
        self.assertIn("api.risk_free_rate_pct", out)        # still empty, still flagged


if __name__ == "__main__":
    unittest.main()
