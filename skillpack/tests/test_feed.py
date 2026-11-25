"""in-common/feed.py tests, offline: a replay with a dropped connection, the
re-subscription after it, subscription changes mid-session, staleness, the
tick store it leaves behind, and the checks a live start must pass."""

import datetime
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

feed = load("in-common", "feed")
smart = feed.smart
IDX, OPT = "99926000", "45004"


def le(v):
    return int(v).to_bytes(8, "little", signed=True)


def pkt(ex, token, exch_ms, ltp_paise, volume=0):
    """A Quote-mode SmartStream packet (123 bytes)."""
    b = bytes([2, ex]) + token.encode().ljust(25, b"\0") + le(1) + le(exch_ms) + le(ltp_paise)
    return b + le(0) + le(ltp_paise) + le(volume) + bytes(16) + le(ltp_paise) * 4


def at(hms):
    return int(smart.parse_ist("2026-11-24 " + hms).timestamp() * 1000)


def frame(hms, ex, token, paise, lag=200, volume=0):
    recv = at(hms)
    return {"ms": recv, "op": 2, "hex": pkt(ex, token, recv - lag, paise, volume).hex()}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = feed.main(argv)
    return code, out.getvalue(), err.getvalue()


class InTemp(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        self.db = os.path.join("trader", "state", "ticks.sqlite")

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)


class Offline(InTemp):
    def write_frames(self):
        lines = [{"ms": at("09:15:01"), "op": 1, "text": "pong"},
                 frame("09:15:05", 1, IDX, 2350000, lag=180), frame("09:16:00", 2, OPT, 11240, lag=300, volume=650),
                 frame("09:19:59", 1, IDX, 2351500, lag=220),
                 {"drop": True},
                 frame("09:21:00", 1, IDX, 2349000, lag=150), frame("09:25:30", 1, IDX, 2352000, lag=950)]
        with open("frames.jsonl", "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(x) + "\n" for x in lines))

    def test_a_dropped_connection_reconnects_and_the_ticks_are_stored(self):
        smart.subscribe("trader", "test", [{"segment": "NSE", "token": IDX, "mode": "quote"},
                                           {"segment": "NFO", "token": OPT, "mode": "snapquote"}])
        self.write_frames()
        code, out, err = run(["run", "--offline", "frames.jsonl", "--now", "2026-11-24 09:15"])
        self.assertEqual(code, 0, err)
        self.assertIn("  Connects       2 connect(s), 1 reconnect(s)", out)
        st = smart.read_json(os.path.join("trader", "state", "feed_status.json"))
        self.assertEqual((st["connects"], st["reconnects"], st["ticks"], st["running"]), (2, 1, 5, False))
        self.assertEqual(st["subscribed"], 2)
        latest = smart.read_latest(self.db, [IDX, OPT], at("09:25:31"), 3000)
        self.assertEqual((latest[IDX]["ltp"], latest[IDX]["lag_ms"], latest[IDX]["stale"]), (23520.0, 950, False))
        self.assertEqual((latest[OPT]["ltp"], latest[OPT]["stale"]), (112.4, True))
        bars = smart.read_bars(self.db, IDX, 5, at("09:00:00"), smart.exchange_clock(self.db))
        self.assertEqual([(b[0], b[1], b[2], b[4]) for b in bars],
                         [(at("09:15:00"), 23500.0, 23515.0, 23515.0), (at("09:20:00"), 23490.0, 23490.0, 23490.0)])

    def test_status_command(self):
        self.assertEqual(run(["status"])[0], 1)
        smart.subscribe("trader", "test", [{"segment": "NSE", "token": IDX}])
        self.write_frames()
        run(["run", "--offline", "frames.jsonl", "--now", "2026-11-24 09:15"])
        code, out, _ = run(["status"])
        self.assertEqual(code, 0)
        self.assertIn("(stopped)", out)
        self.assertIn("Not financial advice", out)


class Loop(InTemp):
    def make(self, streams, wanted, **kw):
        it = iter(streams)

        def connect():
            return next(it)
        store = smart.TickStore(self.db)
        f = feed.Feed(store, os.path.join("trader", "state", "feed_status.json"), connect, wanted, 3000,
                      replay_from_ms=at("09:15:00"), sleep=lambda s: None, **kw)
        return f, store

    def test_resubscribes_after_a_drop_and_unsubscribes_what_is_no_longer_wanted(self):
        both = {(1, IDX): 2, (2, OPT): 3}
        s1 = smart.ReplayStream([frame("09:15:02", 1, IDX, 2350000)], end_with_close=True)
        s2 = smart.ReplayStream([frame("09:16:00", 1, IDX, 2350000), frame("09:16:10", 1, IDX, 2350100)],
                                end_with_close=True)
        f, store = self.make([s1, s2], lambda: ((both if f.clock_ms() < at("09:16:05") else {(1, IDX): 2}), 0))
        st = f.run(float("inf"))
        store.close()
        first = json.loads(s1.sent[0])
        self.assertEqual((first["action"], first["params"]["mode"]), (1, 2))
        subs = [json.loads(m) for m in s2.sent if m != "ping"]
        self.assertEqual([(m["action"], m["params"]["mode"]) for m in subs[:2]], [(1, 2), (1, 3)])   # everything again
        self.assertEqual(subs[-1]["action"], 0)                                                      # then OPT dropped
        self.assertEqual(subs[-1]["params"]["tokenList"], [{"exchangeType": 2, "tokens": [OPT]}])
        self.assertEqual((st["connects"], st["subscribed"]), (2, 1))

    def test_stale_tokens(self):
        f, store = self.make([], lambda: ({}, 0))
        f.have = {(1, IDX): 2, (2, OPT): 3}
        f.connected_at = at("09:15:00")
        f.seen = {IDX: at("09:15:09")}
        self.assertEqual(f.stale_tokens(at("09:15:10")), [OPT])     # never ticked, connected 10 s ago
        self.assertEqual(f.stale_tokens(at("09:15:02")), [])
        store.close()

    def test_two_refusals_in_a_row_stop_with_the_next_step(self):
        def refuse():
            raise smart.StreamRefused("stream refused the connection: HTTP/1.1 401 Unauthorized")
        store = smart.TickStore(self.db)
        f = feed.Feed(store, os.path.join("trader", "state", "feed_status.json"), refuse, lambda: ({}, 0), 3000,
                      replay_from_ms=at("09:15:00"), sleep=lambda s: None)
        with self.assertRaises(smart.SmartError) as e:
            f.run(float("inf"))
        store.close()
        self.assertIn("run in-setup login", str(e.exception))


class LiveChecks(InTemp):
    def test_closed_market_unconfirmed_stream_and_a_second_feed_are_refused(self):
        code, _, err = run(["run", "--now", "2026-11-28 10:00"])
        self.assertEqual((code, err), (1, "feed: market data is closed: weekend: the feed runs on trading days, "
                                          "09:00 to 15:45 IST\n"))
        code, _, err = run(["run", "--now", "2026-11-24 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("stream is not confirmed on this account yet", err)
        smart.write_json(os.path.join("trader", "state", "feed_status.json"),
                         {"running": True, "pid": 4242, "updated_ms": int(time.time() * 1000)})
        code, _, err = run(["run", "--now", "2026-11-24 10:00"])
        self.assertIn("a feed is already running", err)
        self.assertIn("4242", err)


class Chain(unittest.TestCase):
    def test_index_and_atm_strikes(self):
        rows = smart.read_json(os.path.join(FIX, "in-setup", "master.json"))
        items = feed.chain_items(rows, "NIFTY", 23512.35, 100, datetime.date(2026, 11, 24), 1)
        self.assertEqual(items[0], {"segment": "NSE", "token": IDX, "mode": "quote"})
        syms = sorted(r["symbol"] for r in rows if r["token"] in {i["token"] for i in items[1:]})
        self.assertEqual(syms, ["NIFTY24NOV2623450CE", "NIFTY24NOV2623450PE", "NIFTY24NOV2623500CE",
                                "NIFTY24NOV2623500PE", "NIFTY24NOV2623550CE", "NIFTY24NOV2623550PE"])
        self.assertEqual({i["mode"] for i in items[1:]}, {"snapquote"})


if __name__ == "__main__":
    unittest.main()
