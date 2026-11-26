"""Offline tests for the engine's business rules. Run from backend/:  python -m unittest discover tests -v

All data here is synthetic (controlled test data): no network, no broker, no live prices.
"""

import argparse
import asyncio
import datetime
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from app import paper, strategies as strat
from app.engine import Engine, EngineError, last_closed_bar, phase
from app.sp import smart
from app.store import Store

IST = smart.IST
DAY = "2026-11-26"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def at(hm, sec=0):
    h, m = int(hm[:2]), int(hm[3:])
    return datetime.datetime(2026, 11, 26, h, m, sec, tzinfo=IST)


def bar(hm, o, h, l, c):
    return {"start_ist": hm, "open": o, "high": h, "low": l, "close": c}


def series(start, closes, spread=4.0):
    """Bars every 5 minutes from `start`, each opening at the previous close."""
    out, t, prev = [], int(start[:2]) * 60 + int(start[3:]), closes[0]
    for c in closes:
        o = prev
        out.append(bar("%02d:%02d" % divmod(t, 60), o, max(o, c) + spread / 2, min(o, c) - spread / 2, c))
        prev, t = c, t + 5
    return out


ORB_CFG = strat.strategy_cfg({}, "ORB")
OPENING = [bar("09:15", 100, 110, 95, 105), bar("09:20", 105, 108, 98, 100), bar("09:25", 100, 104, 96, 102)]


class OrbTest(unittest.TestCase):
    def test_breakout_up_then_down(self):
        bars = OPENING + [bar("09:30", 102, 110, 101, 110),      # close == high: not beyond
                          bar("09:35", 110, 113, 109, 112),      # bullish trigger
                          bar("09:40", 112, 115, 111, 114),      # second close above: no repeat
                          bar("10:00", 100, 100, 90, 92)]        # bearish breakdown later in the day
        sigs = strat.orb_signals("NIFTY", DAY, bars, ORB_CFG)
        self.assertEqual([(s["direction"], s["bar_start_ist"]) for s in sigs], [("bullish", "09:35"), ("bearish", "10:00")])
        up = sigs[0]
        self.assertEqual(up["bar_close_ist"], "09:40")
        self.assertEqual(up["stop_underlying"], 102.5)              # or-mid of 110 / 95
        self.assertEqual(up["target_underlying"], 112 + 2 * 9.5)
        self.assertEqual(up["id"], "2026-11-26:ORB:NIFTY:09:35:bullish")

    def test_no_signal_before_range_complete(self):
        self.assertEqual(strat.orb_signals("NIFTY", DAY, OPENING[:2] + [bar("09:30", 1, 200, 1, 199)], ORB_CFG), [])

    def test_or_opposite_stop(self):
        bars = OPENING + [bar("09:30", 102, 113, 101, 112)]
        s = dict(ORB_CFG, stop_rule="or-opposite")
        self.assertEqual(strat.orb_signals("NIFTY", DAY, bars, s)[0]["stop_underlying"], 95)

    def test_malformed_bars_are_dropped(self):
        bad = [bar("09:15", 100, 90, 95, 105), bar("09:20", 0, 1, 0, 1), {"start_ist": "09:25", "open": None}]
        self.assertEqual(strat.clean_bars(bad + OPENING[:1]), OPENING[:1])


class EmaPullbackTest(unittest.TestCase):
    def setUp(self):
        self.s = strat.strategy_cfg({}, "EMA_PULLBACK")
        self.prev = series("09:15", [24800.0 + i * 3 for i in range(75)], 6)    # yesterday: steady uptrend
        up = [25030.0 + i * 8 for i in range(10)]                                 # today 09:15..10:00 rising
        pull = [25088.0, 25074.0, 25070.0]                                        # 10:05..10:15 dip to EMA9
        go = [25085.0, 25093.0]                                                   # 10:20 resumes above prior high
        self.today = series("09:15", up + pull + go, 6)

    def test_pullback_then_resume_is_a_bullish_signal(self):
        sigs = strat.ema_pullback_signals("NIFTY", DAY, self.prev, self.today, self.s)
        self.assertTrue(sigs, "expected a pullback signal")
        s = sigs[0]
        self.assertEqual((s["strategy"], s["direction"], s["bar_start_ist"]), ("EMA_PULLBACK", "bullish", "10:20"))
        self.assertLess(s["stop_underlying"], s["entry_underlying"])
        self.assertAlmostEqual(s["target_underlying"] - s["entry_underlying"],
                               2 * (s["entry_underlying"] - s["stop_underlying"]), delta=0.1)
        again = strat.ema_pullback_signals("NIFTY", DAY, self.prev, self.today, self.s)
        self.assertEqual([x["id"] for x in sigs], [x["id"] for x in again])      # deterministic ids

    def test_cooldown_blocks_an_immediate_repeat(self):
        ids = [s["bar_start_ist"] for s in strat.ema_pullback_signals("NIFTY", DAY, self.prev, self.today, self.s)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertLessEqual(len(ids), 1)

    def test_evaluate_skips_ema_without_previous_day(self):
        sigs = strat.evaluate({}, "NIFTY", DAY, None, self.today)
        self.assertFalse([s for s in sigs if s["strategy"] == "EMA_PULLBACK"])


CFG = json.load(open(os.path.join(ROOT, "trader", "config.json")))
SIG = {"id": "x", "strategy": "ORB", "symbol": "NIFTY", "date": DAY, "direction": "bullish", "bar_start_ist": "13:50",
       "bar_close_ist": "13:55", "entry_underlying": 25900.0, "stop_underlying": 25850.0, "target_underlying": 26000.0,
       "risk_points": 50.0}


def ctx(**kw):
    base = {"open_trades": [], "trades_today": 0, "strategy_trades_today": 0, "realized_today": 0.0,
            "losses_in_a_row": 0, "underlying_ltp": 25905.0, "auto_execute": True, "s": strat.strategy_cfg(CFG, "ORB")}
    return dict(base, **kw)


class GateTest(unittest.TestCase):
    def test_before_cutoff_executes(self):
        self.assertEqual(paper.gate(SIG, at("13:55", 4), CFG, ctx()), ("EXECUTE", None))

    def test_after_cutoff_is_recorded_not_traded(self):
        sig = dict(SIG, bar_start_ist="13:55", bar_close_ist="14:00")
        status, why = paper.gate(sig, at("14:00", 3), CFG, ctx())
        self.assertEqual(status, "ENTRY_CUTOFF")
        self.assertIn("14:00", why)
        status, _ = paper.gate(dict(SIG, bar_start_ist="14:25", bar_close_ist="14:30"), at("14:30", 2), CFG, ctx())
        self.assertEqual(status, "ENTRY_CUTOFF")

    def test_stale_signals(self):
        self.assertEqual(paper.gate(SIG, at("13:59", 0), CFG, ctx())[0], "REJECTED")        # closed 13:55, 4 min ago
        late = dict(SIG, bar_close_ist="14:10")
        self.assertEqual(paper.gate(late, at("14:20"), CFG, ctx())[0], "ENTRY_CUTOFF")

    def test_limits_and_conflicts(self):
        now = at("13:55", 4)
        open_t = [{"id": "P1", "symbol": "NIFTY", "strategy": "EMA_PULLBACK", "direction": "bearish"}]
        self.assertIn("conflict", paper.gate(SIG, now, CFG, ctx(open_trades=open_t))[1])
        self.assertIn("duplicate", paper.gate(SIG, now, CFG, ctx(open_trades=[dict(open_t[0], direction="bullish")]))[1])
        self.assertIn("max trades", paper.gate(SIG, now, CFG, ctx(trades_today=3))[1])
        self.assertIn("ORB max trades", paper.gate(SIG, now, CFG, ctx(strategy_trades_today=2))[1])
        self.assertIn("daily loss", paper.gate(SIG, now, CFG, ctx(realized_today=-10000.0))[1])
        self.assertIn("in a row", paper.gate(SIG, now, CFG, ctx(losses_in_a_row=3))[1])
        self.assertIn("no longer valid", paper.gate(SIG, now, CFG, ctx(underlying_ltp=25840.0))[1])
        self.assertEqual(paper.gate(SIG, now, CFG, ctx(auto_execute=False))[0], "EXECUTABLE")


TRADE = {"direction": "bearish", "stop_premium": 100.0, "target_premium": 160.0, "stop_underlying": 25950.0,
         "target_underlying": 25800.0, "flat_by": "15:10"}


class ExitTest(unittest.TestCase):
    def test_exits(self):
        self.assertEqual(paper.exit_reason(TRADE, 99.0, None, "11:00"), "SL_PREMIUM")
        self.assertEqual(paper.exit_reason(TRADE, 161.0, None, "11:00"), "TARGET_PREMIUM")
        self.assertEqual(paper.exit_reason(TRADE, 120.0, 25955.0, "11:00"), "SL_UNDERLYING")     # PE: index up = stop
        self.assertEqual(paper.exit_reason(TRADE, 120.0, 25790.0, "11:00"), "TARGET_UNDERLYING")
        self.assertEqual(paper.exit_reason(TRADE, 120.0, 25900.0, "15:10"), "TIME_EXIT")

    def test_no_forced_exit_at_the_entry_cutoff(self):
        self.assertIsNone(paper.exit_reason(TRADE, 120.0, 25900.0, "14:00"))
        self.assertIsNone(paper.exit_reason(TRADE, 120.0, 25900.0, "14:45"))

    def test_pnl_is_long_option_for_both_directions(self):
        ch = json.load(open(os.path.join(ROOT, "skillpack", "in-setup", "charges.example.json")))
        g, c, n = paper.pnl(100.0, 120.0, [65], "NFO", ch)
        self.assertEqual(g, 1300.0)
        self.assertGreater(c, 60)                 # two orders of Rs 30 brokerage plus taxes
        self.assertAlmostEqual(n, g - c)
        self.assertLess(paper.pnl(100.0, 80.0, [65], "NFO", ch)[2], -1300)

    def test_premium_plan(self):
        stop, tgt = paper.premium_plan(159.75, {"tick_size": 0.05}, CFG, 2.0)
        self.assertEqual((stop, tgt), (111.8, 255.65))                    # the live trade of 2026-11-26

    def test_bad_prices(self):
        for x in (None, 0, -1, "x", float("nan"), float("inf")):
            self.assertIsNone(paper.valid_price(x))


class ClockTest(unittest.TestCase):
    def test_last_closed_bar(self):
        self.assertIsNone(last_closed_bar(at("09:19", 59)))
        self.assertEqual(last_closed_bar(at("09:20")), "09:15")
        self.assertEqual(last_closed_bar(at("13:59", 59)), "13:50")
        self.assertEqual(last_closed_bar(at("15:40")), "15:25")

    def test_phase_uses_ist_and_calendar(self):
        self.assertEqual(phase(at("10:00"), {})[0], "open")
        self.assertEqual(phase(at("15:35"), {})[0], "post_close")
        self.assertEqual(phase(at("10:00"), {"holidays": {DAY: "test"}})[0], "closed")
        utc = datetime.datetime(2026, 11, 26, 4, 30, tzinfo=datetime.timezone.utc)     # 10:00 IST
        self.assertEqual(phase(utc.astimezone(IST), {})[0], "open")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.st = Store(os.path.join(self.dir, "t.sqlite"))

    def tearDown(self):
        self.st.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_duplicates_and_win_rate(self):
        sig = dict(SIG, note=None)
        self.assertTrue(self.st.add_signal(sig, "EXECUTABLE", None, at("13:55").isoformat()))
        self.assertFalse(self.st.add_signal(sig, "EXECUTABLE", None, at("13:56").isoformat()))
        t = {"id": "P1", "signal_id": "x", "date": DAY, "strategy": "ORB", "symbol": "NIFTY", "direction": "bullish",
             "tradingsymbol": "N", "token": "1", "segment": "NFO", "instrument": {}, "lots": 1, "quantity": 65,
             "order_qtys": [65], "entry_price": 100.0, "entry_time": at("13:55").isoformat(), "entry_underlying": 1.0,
             "stop_premium": 70.0, "target_premium": 160.0, "stop_underlying": 0.5, "target_underlying": 2.0,
             "flat_by": "15:10", "status": "OPEN"}
        self.st.open_trade(t, "x")
        with self.assertRaises(sqlite3.IntegrityError):
            self.st.open_trade(dict(t, id="P2"), "x")                       # one signal, one trade
        st = self.st.stats(DAY, ["ORB"])["ORB"]
        self.assertEqual((st["trades"], st["open"], st["completed"], st["win_rate"]), (1, 1, 0, None))
        self.assertTrue(self.st.close_trade("P1", 120.0, at("14:30").isoformat(), "TARGET", 1300.0, 70.0, 1230.0))
        self.assertFalse(self.st.close_trade("P1", 121.0, at("14:31").isoformat(), "TARGET", 1.0, 1.0, 0.0))
        st = self.st.stats(DAY, ["ORB", "EMA_PULLBACK"])
        self.assertEqual((st["ORB"]["wins"], st["ORB"]["win_rate"], st["ORB"]["net_pnl"]), (1, 100.0, 1230.0))
        self.assertEqual(st["EMA_PULLBACK"]["trades"], 0)
        self.assertEqual(self.st.stats("2026-11-27", ["ORB"])["ORB"]["trades"], 0)  # a new day starts at zero


class SimulatedDayTest(unittest.TestCase):
    """The engine's signal -> paper trade -> exit path on controlled data: the option pick and
    every price are stubbed. This is a simulation, not live validation."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "trader", "config.json"), os.path.join(self.dir, "config.json"))
        self.eng = Engine(trader=self.dir)
        self.eng.day = DAY
        self.cfg = smart.load_config(self.dir)
        self.eng.market["NIFTY"] = {"ltp": 25905.0}

    def tearDown(self):
        self.eng.store.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake_pick(self, a):
        smart.write_json(os.path.join(self.dir, "pick.json"), {
            "underlying": a.underlying, "direction": a.direction, "as_of_ist": self.now.isoformat(),
            "premium": 150.0, "bid": 149.5, "ask": 150.5,
            "instrument": {"tradingsymbol": "NIFTY01DEC2625900CE", "token": "999", "segment": "NFO", "type": "CE",
                           "expiry": "2026-12-01", "lot_size": 65, "tick_size": 0.05, "freeze_qty": 1801}})
        return 0

    def run_at(self, now, fn, *args):
        self.now = now
        with mock.patch.object(smart, "now_ist", lambda pinned=None: now), \
                mock.patch("app.sp.chain.cmd_pick", self.fake_pick):
            return fn(*args)

    def test_a_day(self):
        e = self.eng
        self.run_at(at("13:55", 3), e.process, dict(SIG), self.cfg)
        trades = e.store.open_trades()
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["strategy"], t["entry_price"], t["stop_premium"], t["status"]), ("ORB", 150.5, 105.35, "OPEN"))
        self.assertEqual(e.store.signals(DAY)[0]["status"], "EXECUTED")

        # the same signal again (a restart re-detects it): nothing new
        self.run_at(at("13:55", 9), e.process, dict(SIG), self.cfg)
        self.assertEqual(e.store.count_trades(DAY), 1)

        # after 14:00 an EMA signal on BANKNIFTY is recorded as ENTRY_CUTOFF and makes no trade
        late = dict(SIG, id="late", strategy="EMA_PULLBACK", symbol="BANKNIFTY", bar_start_ist="14:05",
                    bar_close_ist="14:10")
        self.run_at(at("14:10", 2), e.process, late, self.cfg)
        self.assertEqual([s["status"] for s in e.store.signals(DAY) if s["id"] == "late"], ["ENTRY_CUTOFF"])
        self.assertEqual(e.store.count_trades(DAY), 1)

        # the open trade is still managed after 14:00: no exit at 14:20 inside the levels, target at 14:40
        e.market["NIFTY"] = {"ltp": 25950.0}
        with mock.patch.object(e, "prices", lambda tb, now: {"999": (170.0, "feed", 0.1)}):
            self.run_at(at("14:20"), e.monitor, at("14:20"), self.cfg)
            self.assertEqual(e.store.open_trades()[0]["ltp"], 170.0)
        with mock.patch.object(e, "prices", lambda tb, now: {"999": (241.0, "feed", 0.1)}):
            self.run_at(at("14:40"), e.monitor, at("14:40"), self.cfg)
        closed = e.store.trades(DAY, "CLOSED")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "TARGET_PREMIUM")
        self.assertAlmostEqual(closed[0]["gross_pnl"], (241.0 - 150.5) * 65)
        st = e.store.stats(DAY, ["ORB", "EMA_PULLBACK"])
        self.assertEqual((st["ORB"]["win_rate"], st["ORB"]["completed"], st["EMA_PULLBACK"]["entry_cutoff"]), (100.0, 1, 1))

    def test_stop_loss_and_restart_safety(self):
        e = self.eng
        self.run_at(at("10:00", 2), e.process, dict(SIG, bar_start_ist="09:55", bar_close_ist="10:00"), self.cfg)
        e.market["NIFTY"] = {"ltp": 25840.0}                            # index through the 25,850 stop
        with mock.patch.object(e, "prices", lambda tb, now: {"999": (120.0, "feed", 0.1)}):
            self.run_at(at("10:15"), e.monitor, at("10:15"), self.cfg)
        t = e.store.trades(DAY, "CLOSED")[0]
        self.assertEqual(t["exit_reason"], "SL_UNDERLYING")
        self.assertLess(t["net_pnl"], 0)
        # manual close: refused without a live price, done at the live price otherwise
        e.market["NIFTY"] = {"ltp": 25905.0}
        self.run_at(at("10:30", 2), e.process, dict(SIG, id="m", bar_start_ist="10:25", bar_close_ist="10:30"), self.cfg)
        tid = e.store.open_trades()[0]["id"]
        e.client = object()
        with mock.patch.object(e, "prices", lambda tb, now: {}):
            with self.assertRaises(EngineError):
                self.run_at(at("10:40"), e.close_manual, tid)
        with mock.patch.object(e, "prices", lambda tb, now: {"999": (155.0, "quote", 0.0)}):
            self.assertEqual(self.run_at(at("10:41"), e.close_manual, tid)["exit_reason"], "MANUAL")
        e2 = Engine(trader=self.dir)                                    # a restart reads the same database
        self.assertEqual(e2.store.stats(DAY, ["ORB"])["ORB"]["losses"], 1)
        self.assertEqual(e2.store.stats(DAY, ["ORB"])["ORB"]["completed"], 2)
        e2.store.db.close()


class AuthTest(unittest.TestCase):
    """APP_TOKEN guards every route but /health, and the Socket.IO connect."""

    TOKEN = "t" * 32

    def status(self, path, headers=()):
        from app.main import app
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "https",
                 "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "headers": [(k.encode(), v.encode()) for k, v in headers], "server": ("x", 443), "client": ("y", 1)}
        asyncio.run(app(scope, receive, send))
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    def test_token_required(self):
        from app import config
        with mock.patch.object(config, "APP_TOKEN", self.TOKEN):
            self.assertEqual(self.status("/health"), 200)
            for path in ("/api/v1/state", "/api/v1/settings", "/docs", "/openapi.json"):
                self.assertEqual(self.status(path), 401, path)
                self.assertEqual(self.status(path, [("authorization", "Bearer wrong")]), 401, path)
            # the right token gets past the check (a route that does not exist, so nothing else runs)
            self.assertEqual(self.status("/api/v1/nope", [("authorization", "Bearer " + self.TOKEN)]), 404)
            self.assertEqual(self.status("/api/v1/nope", [("authorization", self.TOKEN)]), 401)   # no Bearer

    def test_socket_refused_without_token(self):
        import socketio
        from app import config, main
        with mock.patch.object(config, "APP_TOKEN", self.TOKEN):
            for auth in (None, {}, {"token": "wrong"}, "not-a-dict"):
                with self.assertRaises(socketio.exceptions.ConnectionRefusedError):
                    asyncio.run(main.connect("sid", {}, auth))

    def test_render_refuses_to_start_open(self):
        import importlib
        from app import config
        try:
            with mock.patch.dict(os.environ, {"RENDER": "true", "APP_TOKEN": "short"}):
                with self.assertRaises(SystemExit):
                    importlib.reload(config)
        finally:
            importlib.reload(config)


if __name__ == "__main__":
    unittest.main()
