"""Offline tests for the skill pack wired into the engine: in-exit's plan and no-progress exit, in-risk's
PASS/BLOCK, the time machine after charges, the backtest, and the skill runner.

All data is synthetic (controlled test data): fake picks, fake candles, no network, no broker.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from app import paper, skills, strategies as strat, timemachine as tmi
from app.engine import Engine, EngineError
from app.sp import smart
from app.store import Store
from test_logic import MASTER_ROWS, ROOT, SIG, at, series
from test_timemachine import CAL, FakeClient

DAY = "2026-11-26"


class PipelineBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "trader", "config.json"), os.path.join(self.dir, "config.json"))
        self.eng = Engine(trader=self.dir)
        self.eng.day = DAY
        self.eng.market["NIFTY"] = {"ltp": 25905.0}
        self.eng.master_rows = MASTER_ROWS
        self.cfg = smart.load_config(self.dir)

    def tearDown(self):
        self.eng.store.db.close()
        if self.eng.tm_window:
            self.eng.tm_window.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake_pick(self, a):
        smart.write_json(os.path.join(self.dir, "pick.json"), {
            "underlying": a.underlying, "direction": a.direction, "as_of_ist": self.now.isoformat(),
            "premium": 150.0, "bid": 149.5, "ask": 150.5, "spread_pct": 0.67, "volume": 50000, "oi": 900000,
            "instrument": {"tradingsymbol": "NIFTY01DEC2625900CE", "token": "999", "segment": "NFO", "type": "CE",
                           "underlying": "NIFTY", "expiry": "2026-12-01", "lot_size": 65, "tick_size": 0.05,
                           "freeze_qty": 1801}})
        return 0

    def run_at(self, now, fn, *args):
        self.now = now
        with mock.patch.object(smart, "now_ist", lambda pinned=None: now), \
                mock.patch("app.sp.chain.cmd_pick", self.fake_pick):
            return fn(*args)

    def signal(self):
        return self.eng.store.signals(DAY)[0]


class PipelineTest(PipelineBase):

    def test_orb_plan_cross_checks_in_exit_and_records_risk(self):
        sig = dict(SIG, bar_start_ist="09:50", bar_close_ist="09:55", entry_underlying=25920.0,
                   stop_underlying=25875.0, target_underlying=26010.0, opening_range={"high": 25910.0, "low": 25840.0})
        self.run_at(at("09:55", 3), self.eng.process, sig, self.cfg)
        s = self.signal()
        self.assertEqual(s["status"], "EXECUTED", s["reason"])
        self.assertEqual(s["meta"]["plan"]["in_exit_check"], "match")            # or-mid 25875 both ways
        self.assertEqual(s["meta"]["risk"]["verdict"], "PASS")
        self.assertIn("max_loss_per_trade", [c["name"] for c in s["meta"]["risk"]["checks"]])
        t = self.eng.store.open_trades()[0]
        self.assertEqual((t["stop_premium"], t["target_premium"], t["no_progress_bars"]), (105.35, 240.8, 6))
        plan = smart.read_json(os.path.join(self.dir, "state", "engine", "plan.json"))
        self.assertEqual(plan["created_by"], "in-exit")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "plan.json")))   # the user's own files untouched

    def test_event_day_is_an_in_risk_block(self):
        smart.write_json(os.path.join(self.dir, "events.json"), {"events": [{"date": DAY, "name": "RBI policy",
                                                                            "kind": "rbi"}]})
        self.run_at(at("13:55", 3), self.eng.process, dict(SIG), self.cfg)
        s = self.signal()
        self.assertEqual(s["status"], "REJECTED")
        self.assertTrue(s["reason"].startswith("in-risk BLOCK: event_day"), s["reason"])
        self.assertEqual(self.eng.store.open_trades(), [])

    def test_advisory_checks_do_not_block(self):
        smart.write_json(os.path.join(self.dir, "events.json"), {"events": [{"date": DAY, "name": "RBI policy",
                                                                            "kind": "rbi"}]})
        cfg = dict(self.cfg, risk=dict(self.cfg["risk"], engine_advisory_checks=["event_day"]))
        self.run_at(at("13:55", 3), self.eng.process, dict(SIG), cfg)
        s = self.signal()
        self.assertEqual(s["status"], "EXECUTED")
        self.assertTrue(s["meta"]["risk"]["advisory"][0].startswith("event_day"))

    def test_time_machine_after_charges(self):
        smart.write_json(os.path.join(self.dir, "calendar.json"), CAL)
        self.eng.facts["NIFTY"] = {"token": "99926000", "segment": "NSE", "expiry_today": False, "expiries": []}
        self.eng.client = FakeClient(True)
        self.run_at(at("13:55", 3), self.eng.process, dict(SIG), self.cfg)
        tmeta = self.signal()["meta"]["timemachine"]
        self.assertGreater(tmeta["charges_r"], 0)
        self.assertAlmostEqual(tmeta["expectancy_r"], 2.0 - tmeta["charges_r"], places=3)

    def test_no_progress_exit(self):
        self.run_at(at("09:55", 3), self.eng.process, dict(SIG, bar_start_ist="09:50", bar_close_ist="09:55"),
                    self.cfg)
        t = self.eng.store.open_trades()[0]
        flat = series("09:15", [25900.0] * 15, 2)                     # never closes above the 25,900 entry
        self.assertFalse(paper.no_progress(t, flat, at("10:20")))     # five of six bars closed: not yet
        self.assertTrue(paper.no_progress(t, flat, at("10:25")))
        up = series("09:15", [25900.0] * 12 + [25910.0] + [25900.0] * 2, 2)
        self.assertFalse(paper.no_progress(t, up, at("10:25")))       # 10:15 closed above: progress
        self.assertFalse(paper.no_progress(t, flat[:12], at("10:25")))  # bars missing: decides nothing
        self.eng.bars["NIFTY"] = flat
        with mock.patch.object(self.eng, "prices", lambda tb, now: {"999": (151.0, "feed", 0.1)}):
            self.run_at(at("10:25"), self.eng.monitor, at("10:25"), self.cfg)
        self.assertEqual(self.eng.store.trades(DAY, "CLOSED")[0]["exit_reason"], "NO_PROGRESS")

    def test_backtest(self):
        smart.write_json(os.path.join(self.dir, "calendar.json"), CAL)
        self.eng.facts["NIFTY"] = {"token": "99926000", "segment": "NSE", "expiry_today": False, "expiries": []}
        self.eng.client, self.eng.client_day = FakeClient(True, holidays=["2026-10-02", "2026-10-21"]), DAY
        self.eng.day = DAY
        with mock.patch.object(smart, "now_ist", lambda pinned=None: at("18:00")):
            r = self.eng.backtest("NIFTY", "ORB", 120, {"target_r": 1.5})
            with self.assertRaises(EngineError):
                self.eng.backtest("NIFTY", "ORB", 10)
        self.assertEqual(r["window"]["days_used"], 120)                  # two holidays reached past
        with mock.patch.object(smart, "now_ist", lambda pinned=None: at("18:00")):
            with self.assertRaisesRegex(EngineError, "EMA_PULLBACK never fired on the 61 days"):  # 60 + the seed day
                self.eng.backtest("NIFTY", "EMA_PULLBACK", 60)                # flat afternoons: no pullback
        self.assertEqual(r["history"]["trades"], 120)
        self.assertEqual(r["rule"]["target_r"], 1.5)
        self.assertEqual(r["equity_curve"][-1]["cum_r"], 180.0)            # 120 targets at 1.5R
        self.assertEqual(r["by_direction"]["bullish"]["trades"], 120)
        self.assertEqual(sum(m["trades"] for m in r["by_month"].values()), 120)


class EmaReplayTest(unittest.TestCase):
    def test_a_pullback_day_walks_to_its_target(self):
        prev = series("09:15", [24800.0 + i * 3 for i in range(75)], 6)
        today = series("09:15", [25030.0 + i * 8 for i in range(10)] + [25088.0, 25074.0, 25070.0, 25085.0, 25093.0]
                       + [25100.0 + i * 15 for i in range(40)], 6)
        s = strat.strategy_cfg({}, "EMA_PULLBACK")
        rule = tmi._rule({}, s)
        out = tmi._ema_day("NIFTY", DAY, prev, today, s, rule, False)
        self.assertEqual(out[0]["status"], "trade")
        self.assertEqual((out[0]["entry_time"], out[0]["outcome"], out[0]["r"]), ("10:25", "target", 2.0))


class StoreMigrationTest(unittest.TestCase):
    def test_an_old_database_gains_the_column(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "paper.sqlite")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE trades (id TEXT PRIMARY KEY, signal_id TEXT NOT NULL UNIQUE, date TEXT NOT NULL,"
                       " strategy TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL, tradingsymbol TEXT,"
                       " token TEXT, segment TEXT, instrument TEXT, lots INTEGER, quantity INTEGER, order_qtys TEXT,"
                       " entry_price REAL NOT NULL, entry_time TEXT NOT NULL, entry_underlying REAL,"
                       " stop_premium REAL, target_premium REAL, stop_underlying REAL, target_underlying REAL,"
                       " flat_by TEXT, status TEXT NOT NULL, ltp REAL, ltp_time TEXT, underlying_ltp REAL,"
                       " exit_price REAL, exit_time TEXT, exit_reason TEXT, gross_pnl REAL, charges REAL,"
                       " net_pnl REAL)")
            db.commit()
            db.close()
            st = Store(path)
            self.assertIn("no_progress_bars", {r[1] for r in st.db.execute("PRAGMA table_info(trades)")})
            st.db.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class SkillRunnerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "trader", "config.json"), os.path.join(self.dir, "config.json"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_arguments_are_checked(self):
        bad = [("in-nope", "x", {}), ("in-entry", "watch", {"symbol": "NIFTY"}), ("in-entry", "check", {"symbol": "X"}),
               ("in-ticket", "fill", {"id": "--trader=/etc", "price": 1, "time": "10:00"}),
               ("in-ticket", "close", {"id": "T20261126-01", "price": 1, "time": "25:00", "reason": "stop"}),
               ("in-analyst", "add", {"symbol": "TCS", "quarter": "Q9FY27", "sales": 1, "ebitda": 1, "profit": 1}),
               ("in-news", "run", {"symbols": "NIFTY,../x"}), ("in-timemachine", "run", {"stop": "rm -rf"})]
        for skill, cmd, args in bad:
            with self.assertRaises(skills.SkillError, msg="%s %s %s" % (skill, cmd, args)):
                skills.argv_for(skill, cmd, args)
        self.assertEqual(skills.argv_for("in-entry", "check", {"symbol": "nifty", "filters": ["volume"]})[0],
                         ["check", "NIFTY", "--filter", "volume"])
        self.assertEqual(skills.argv_for("in-scanner", "daily", {"stocks": True})[0], ["daily", "--stocks"])

    def test_every_skill_folder_is_reachable(self):
        for skill in ("in-timemachine", "in-risk", "in-exit", "in-entry", "in-scanner", "in-news", "in-alerts",
                      "in-ticket", "in-flows", "in-analyst"):
            self.assertIn(skill, skills.COMMANDS)

    def test_analyst_add_then_show_runs_the_real_script(self):
        r = skills.run("in-analyst", "add", {"symbol": "TCS", "quarter": "Q2FY27", "sales": 65000, "ebitda": 17000,
                                             "profit": 12000, "eps": 33.1, "source": "synthetic test numbers"},
                       trader=self.dir)
        self.assertTrue(r["ok"], r["data"]["stderr"] or r["data"]["stdout"])
        self.assertIn("analyst.json", r["data"]["outputs"])
        r = skills.run("in-analyst", "show", {"symbol": "TCS"}, trader=self.dir)
        self.assertTrue(r["ok"])
        self.assertIn("Q2FY27", r["data"]["stdout"])
        saved = skills.last("in-analyst", "show", trader=self.dir)
        self.assertEqual(set(saved), {"ok", "reason", "data"})

    def test_a_refusal_is_reported_not_raised(self):
        r = skills.run("in-exit", "show", {}, trader=self.dir)                  # no plan.json yet
        self.assertFalse(r["ok"])
        self.assertEqual(r["data"]["exit_code"], 1)
        self.assertIn("in-exit", r["reason"])


if __name__ == "__main__":
    unittest.main()


class FreshTickTest(PipelineBase):
    """in-risk's stale_data check with a live feed: the engine waits for the just-picked option to tick."""

    def setUp(self):
        super().setUp()
        self.eng.facts["NIFTY"] = {"token": "99926000", "segment": "NSE", "expiry_today": False, "expiries": []}
        self.db = smart.trader_paths(self.dir)["db"]
        self.feed_alive = mock.patch.object(self.eng.feed, "status", lambda: {"alive": True})
        self.feed_alive.start()

    def tearDown(self):
        self.feed_alive.stop()
        super().tearDown()

    def tick(self, *tokens):
        st = smart.TickStore(self.db)
        now = int(__import__("time").time() * 1000)
        for tok in tokens:
            st.add({"token": tok, "exchange_type": 1 if tok == "99926000" else 2, "exch_ms": now - 200,
                    "ltp": 25905.0 if tok == "99926000" else 150.5}, now)
        st.flush()
        st.close()

    def test_fresh_ticks_pass(self):
        self.tick("999", "99926000")
        self.run_at(at("13:55", 3), self.eng.process, dict(SIG), self.cfg)
        s = self.signal()
        self.assertEqual(s["status"], "EXECUTED", s["reason"])
        self.assertTrue(s["meta"]["feed_wait"].startswith("fresh ticks"))
        stale = [c for c in s["meta"]["risk"]["checks"] if c["name"] == "stale_data"][0]
        self.assertTrue(stale["ok"], stale["detail"])

    def test_a_late_option_tick_is_waited_for(self):
        import threading
        self.tick("99926000")
        timer = threading.Timer(0.6, self.tick, ("999", "99926000"))
        timer.start()
        self.run_at(at("13:55", 3), self.eng.process, dict(SIG), self.cfg)
        timer.join()
        self.assertEqual(self.signal()["status"], "EXECUTED", self.signal()["reason"])

    def test_an_option_that_never_ticks_is_still_blocked(self):
        self.tick("99926000")
        with mock.patch("app.engine.FRESH_WAIT_SEC", 0.5):
            self.run_at(at("13:55", 3), self.eng.process, dict(SIG), self.cfg)
        s = self.signal()
        self.assertEqual(s["status"], "REJECTED")
        self.assertIn("stale_data", s["reason"])
        self.assertTrue(s["meta"]["feed_wait"].startswith("still stale"))
