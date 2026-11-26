"""in-risk tests, all offline.

The fixtures in tests/fixtures/in-risk are synthetic, shaped like CONTRACTS.md:
orders.json is the DRAFT ticket in-budget's example writes, pick.json an
in-chain pick, a six-row instrument master shaped like OpenAPIScripMaster.json,
journals, a calendar, events and a ban list. Live prices come from a TickStore
built in the temp dir, its ticks timed against the pinned clock.
"""

import copy
import io
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

check = load("in-risk", "check")
smart = check.smart
OFF = os.path.join(FIX, "in-risk")
NOW = "2026-11-24 09:42"   # a Tuesday; NIFTY 24NOV is on expiry, 01DEC is not
ORDERS = os.path.join("trader", "orders.json")
FRESH = {"45014": 500, "99926000": 400}   # tick age in ms: the option and the NIFTY index


def skill_example(heading):
    with open(os.path.join(ROOT, "in-risk", "SKILL.md"), encoding="utf-8") as fh:
        return re.search(re.escape(heading) + r".*?```text\n(.*?)```", fh.read(), re.S).group(1)


def trade(exit_time, net, day="2026-11-24", closed=True):
    return {"ticket_id": "T-x", "date": day, "symbol": "NIFTY", "quantity": 65, "fill_price": 100.0,
            "fill_time_ist": "09:35", "exit_price": 90.0 if closed else None, "exit_time_ist": exit_time if closed else None,
            "net_pnl_inr": net if closed else None}


class Risk(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        os.makedirs(os.path.join("trader", "cache"))
        os.makedirs(os.path.join("trader", "state"))
        for f in ("config.json", "pick.json", "calendar.json", "events.json", "ban.txt"):
            shutil.copy(os.path.join(OFF, f), os.path.join("trader", f))
        self.master(smart.read_json(os.path.join(OFF, "master.json")))
        self.journal([])
        (self.base,) = smart.read_json(os.path.join(OFF, "orders.json"))["orders"]

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- crafting the trader/ folder

    def master(self, rows, day="2026-11-24"):
        smart.write_json(os.path.join("trader", "cache", "instrument_master_%s.json" % day), rows)

    def journal(self, trades):
        smart.write_json(os.path.join("trader", "state", "journal.json"), {"trades": trades})

    def edit(self, name, fn):
        path = os.path.join("trader", name)
        doc = smart.read_json(path)
        fn(doc)
        smart.write_json(path, doc)

    def feed(self, now, ages):
        """The latest tick for each token, `age` ms old at `now`, 250 ms of feed lag."""
        ms = int(smart.parse_ist(now).timestamp() * 1000)
        store = smart.TickStore(os.path.join("trader", "state", "ticks.sqlite"))
        for token, age in ages.items():
            store.add({"token": token, "exchange_type": 1 if token in ("99926000", "2885") else 2,
                       "exch_ms": ms - age - 250, "volume": 99999, "oi": 77777,
                       # the underlyings sit between the base ticket's stop (23,515) and its target
                       "ltp": getattr(self, "prices", {}).get(token, 23562.5 if token in ("99926000", "2885") else 100.0)},
                      ms - age)
        store.close()

    def ticket(self, **kv):
        o = copy.deepcopy(self.base)
        inst = kv.pop("instrument", None)
        o.update(kv)
        if inst:
            o["instrument"].update(inst)
        return o

    def stock(self, token="61010", expiry="2026-12-29", product="INTRADAY"):
        """One RELIANCE 1300 CE lot (500) at 20.00, stop 14.00, and a pick for it."""
        sym = {"61010": "RELIANCE29DEC261300CE", "61000": "RELIANCE24NOV261300CE"}[token]
        o = self.ticket(symbol="RELIANCE", product=product, limit_price=20.0, premium_stop=14.0, lots=1, quantity=500,
                        order_quantities=[500], capital_used_inr=10000.0,
                        risk_inr={"premium_at_stop": 3000.0, "charges_round_trip": 50.0, "total": 3050.0},
                        instrument={"tradingsymbol": sym, "token": token, "underlying": "RELIANCE", "strike": 1300.0,
                                    "expiry": expiry, "lot_size": 500, "freeze_qty": 10001})
        self.edit("pick.json", lambda p: p.update(instrument=o["instrument"]))
        return o

    # ---- running it

    def run_check(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = check.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def verdict(self, order=None, now=NOW, ages=FRESH):
        """Check one ticket from a fresh orders.json: (risk_result, {check name: check}, exit code, stdout)."""
        smart.write_json(ORDERS, {"orders": [order or self.base]})
        if ages:
            self.feed(now, ages)
        code, out, err = self.run_check("--now", now)
        self.assertEqual(err, "")
        (o,) = smart.read_json(ORDERS)["orders"]
        r = o["risk_result"]
        return r, {c["name"]: c for c in r["checks"]}, code, out

    def failing(self, order=None, now=NOW, ages=FRESH):
        r, by, code, _ = self.verdict(order, now, ages)
        failed = [c["name"] for c in r["checks"] if not c["ok"]]
        self.assertEqual(r["verdict"], "BLOCK" if failed else "PASS")
        self.assertEqual(code, 1 if failed else 0)
        self.assertEqual(len(r["reasons"]), len(failed))
        return failed, by

    # ---- the examples

    def test_the_pass_example_in_skill_md(self):
        smart.write_json(ORDERS, {"orders": [self.base]})
        self.feed(NOW, FRESH)
        code, out, err = self.run_check("--now", NOW)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: PASS"))
        (o,) = smart.read_json(ORDERS)["orders"]
        r = o["risk_result"]
        self.assertEqual((r["verdict"], r["reasons"], r["checked_at_ist"]), ("PASS", [], "2026-11-24T09:42:00+05:30"))
        self.assertEqual(len(r["checks"]), 15)   # signal_valid is the 15th
        self.assertEqual(r["checks"][2], {"name": "max_loss_per_trade", "ok": True,
                                          "detail": "4,454.28 of 5,000.00 (1% of capital 500,000.00)"})
        self.assertEqual(o["status"], "DRAFT")

    def test_a_block_lists_every_reason_together(self):
        shutil.copy(os.path.join(OFF, "journal_three_losses.json"), os.path.join("trader", "state", "journal.json"))
        smart.write_json(ORDERS, {"orders": [self.ticket(ticket_id="T20261124-04")]})
        self.feed("2026-11-24 11:40", FRESH)
        code, out, err = self.run_check("--now", "2026-11-24 11:40")
        self.assertEqual(code, 1, err)
        self.assertEqual(out, skill_example("## Example: BLOCK"))
        r = smart.read_json(ORDERS)["orders"][0]["risk_result"]
        self.assertEqual([x.split(":")[0] for x in r["reasons"]],
                         ["daily_loss_limit", "trades_per_day", "losses_in_a_row", "trading_window"])

    # ---- each rule, passing and blocking

    def test_max_loss_per_trade(self):
        self.edit("config.json", lambda c: c["account"].update(capital_inr=445428))      # 1% = 4,454.28 exactly
        self.assertEqual(self.failing()[0], [])
        self.edit("config.json", lambda c: c["account"].update(capital_inr=440000))      # 1% = 4,400.00
        failed, by = self.failing()
        self.assertEqual(failed, ["max_loss_per_trade"])
        self.assertEqual(by["max_loss_per_trade"]["detail"], "4,454.28 of 4,400.00 (1% of capital 440,000.00)")

    def test_daily_loss_limit(self):
        self.journal([trade("10:00", -5545.72)])                       # 5,545.72 + 4,454.28 = 10,000.00: allowed
        self.assertEqual(self.failing()[0], [])
        self.journal([trade("10:00", -7000.0), trade("10:30", 1000.0)])    # a win offsets a loss: 6,000 net
        failed, by = self.failing()
        self.assertEqual(failed, ["daily_loss_limit"])
        self.assertEqual(by["daily_loss_limit"]["detail"], "realised loss 6,000.00 + this trade 4,454.28 = "
                                                           "10,454.28 of 10,000.00 (2% of capital)")
        self.journal([trade("10:00", -9000.0, day="2026-11-23")])      # yesterday's loss is yesterday's
        self.assertEqual(self.failing()[0], [])

    def test_trades_per_day(self):
        self.journal([trade("10:00", 3000.0), trade("10:30", -1000.0)])
        self.assertEqual(self.failing()[0], [])
        self.journal([trade("10:00", 3000.0), trade("10:30", -1000.0), trade(None, None, closed=False)])  # one still open
        failed, by = self.failing()
        self.assertEqual(failed, ["trades_per_day"])
        self.assertEqual(by["trades_per_day"]["detail"], "3 filled today, limit 3")

    def test_losses_in_a_row(self):
        self.edit("config.json", lambda c: c["account"].update(max_trades_per_day=10))
        self.journal([trade("09:50", -100.0), trade("10:10", -100.0), trade("10:20", 500.0),
                      trade("10:40", -100.0), trade("11:00", -100.0)])
        self.assertEqual(self.failing()[0], [])                        # the win broke the run: 2 in a row
        self.journal([trade("09:50", 500.0), trade("11:00", -100.0), trade("10:10", -100.0), trade("10:40", -100.0)])
        failed, by = self.failing()                                    # ordered by exit time, not file order
        self.assertEqual(failed, ["losses_in_a_row"])
        self.assertEqual(by["losses_in_a_row"]["detail"], "3 losses in a row today (4 closed), stop at 3")
        self.journal([trade("10:00", -100.0, day="2026-11-23")] * 3)  # a new day starts a new count
        self.assertEqual(self.failing()[0], [])

    def test_trading_window(self):
        for hhmm, ok in (("09:29", False), ("09:30", True), ("11:30", True), ("11:31", False)):
            failed, by = self.failing(now="2026-11-24 " + hhmm)
            self.assertEqual(failed, [] if ok else ["trading_window"], hhmm)
        self.assertEqual(by["trading_window"]["detail"], "11:31 outside the entry window 09:30 to 11:30 IST, "
                                                         "a trading day")
        for day, why in (("2026-11-27", "test holiday"), ("2026-11-28", "weekend")):
            self.master(smart.read_json(os.path.join(OFF, "master.json")), day)
            failed, by = self.failing(self.ticket(date=day), now=day + " 10:00")
            self.assertEqual(failed, ["trading_window"], day)
            self.assertEqual(by["trading_window"]["detail"], "%s is not a trading day (%s)" % (day, why))

    def test_expiry_day_cutoff(self):
        o = self.ticket(instrument={"tradingsymbol": "NIFTY24NOV2623500CE", "token": "45004", "expiry": "2026-11-24"})
        self.edit("pick.json", lambda p: p.update(instrument=o["instrument"]))
        ages = {"45004": 500, "99926000": 400}
        self.assertEqual(self.failing(o, "2026-11-24 11:00", ages)[0], [])
        failed, by = self.failing(o, "2026-11-24 11:01", ages)
        self.assertEqual(failed, ["expiry_day_cutoff"])
        self.assertEqual(by["expiry_day_cutoff"]["detail"], "expiry day: 11:01, last entry 11:00")

    def test_fno_ban(self):
        ages = {"61010": 500, "2885": 400}
        failed, by = self.failing(self.stock(), ages=ages)
        self.assertEqual(failed, [])
        self.assertEqual(by["fno_ban"]["detail"], "RELIANCE is not in today's ban.txt (2 names)")
        with open(os.path.join("trader", "ban.txt"), "a") as fh:
            fh.write("reliance\nNIFTY\n")
        failed, by = self.failing(self.stock(), ages=ages)
        self.assertEqual(failed, ["fno_ban"])
        self.assertEqual(by["fno_ban"]["detail"], "RELIANCE is in today's ban.txt (4 names)")
        shutil.copy(os.path.join(OFF, "pick.json"), os.path.join("trader", "pick.json"))
        failed, by = self.failing()                                    # an index is never banned
        self.assertEqual((failed, by["fno_ban"]["detail"]), ([], "NIFTY is an index: never in the F&O ban"))
        os.remove(os.path.join("trader", "ban.txt"))
        self.assertEqual(self.failing(self.stock(), ages=ages)[0], ["fno_ban"])   # no list, no guess

    def test_stale_data_from_the_feed(self):
        self.assertEqual(self.failing(ages={"45014": 3000, "99926000": 400})[0], [])   # 3 s: at the limit
        failed, by = self.failing(ages={"45014": 3500, "99926000": 400})
        self.assertEqual(failed, ["stale_data"])
        self.assertEqual(by["stale_data"]["detail"], "NIFTY01DEC2623500CE 3.5 s old, NIFTY 0.4 s old (limit 3 s); "
                                                     "feed lag 250 ms")
        failed, by = self.failing(ages={"45014": 500, "99926000": 9000})             # the underlying counts too
        self.assertEqual(failed, ["stale_data"])

    def test_a_signal_that_no_longer_stands_is_blocked(self):
        self.prices = {"99926000": 23510.0}                     # bullish, stop 23,515: already through it
        failed, by = self.failing()
        self.assertEqual(failed, ["signal_valid"])
        self.assertEqual(by["signal_valid"]["detail"], "NIFTY 23,510.00 is already at or below the stop 23,515.00")
        self.prices = {}
        smart.write_json(os.path.join("trader", "entry.json"), {"date": "2026-11-24", "bar_close_ist": "09:35"})
        late, o = "2026-11-24 10:10", self.ticket(entry_underlying=23562.5, no_progress_bars=6)   # due 10:05
        failed, by = self.failing(o, now=late)
        self.assertEqual(failed, ["signal_valid"])
        self.assertEqual(by["signal_valid"]["detail"], "the plan's no-progress deadline 10:05 has passed "
                                                       "(trigger closed 09:35 at 23,562.50)")
        self.prices = {"99926000": 23600.0}                     # it moved the right way: it still stands
        self.assertEqual(self.failing(o, now=late)[0], [])

    def test_stale_data_without_a_feed_falls_back_to_the_pick_age(self):
        failed, by = self.failing(ages=None)                           # the pick is from 09:39:50
        self.assertEqual(failed, ["stale_data"])
        self.assertEqual(by["stale_data"]["detail"], "no feed ticks; pick.json 130 s old (limit 10 s)")
        self.edit("pick.json", lambda p: p.update(as_of_ist="2026-11-24T09:41:55+05:30"))
        failed, by = self.failing(ages=None)
        self.assertEqual((failed, by["stale_data"]["detail"]), ([], "no feed ticks; pick.json 5 s old (limit 10 s)"))

    def test_event_day(self):
        self.edit("events.json", lambda e: e["events"].append({"date": "2026-11-24", "name": "FOMC", "kind": "fed"}))
        self.assertEqual(self.failing()[0], [])
        self.edit("events.json", lambda e: e["events"].append({"date": "2026-11-24", "name": "RBI policy", "kind": "rbi"}))
        failed, by = self.failing()
        self.assertEqual((failed, by["event_day"]["detail"]), (["event_day"], "RBI policy (rbi) today"))
        self.edit("config.json", lambda c: c.update(risk={"block_event_kinds": ["fed"]}))
        self.assertEqual(self.failing()[1]["event_day"]["detail"], "FOMC (fed) today")
        os.remove(os.path.join("trader", "events.json"))
        failed, by = self.failing()
        self.assertEqual((failed, by["event_day"]["detail"]), ([], "no trader/events.json: no events to check"))

    def test_lot_cost_against_capital(self):
        self.edit("config.json", lambda c: c["account"].update(max_capital_per_trade_pct=3))   # 15,000
        self.assertEqual(self.failing()[0], [])
        self.edit("config.json", lambda c: c["account"].update(max_capital_per_trade_pct=2))   # 10,000
        failed, by = self.failing()
        self.assertEqual(failed, ["lot_cost"])
        self.assertEqual(by["lot_cost"]["detail"], "2 lots x 7,306.00 = 14,612.00 of 10,000.00 (2% of capital)")

    def test_spread_and_liquidity(self):
        for key, value in (("spread_pct", 3.5), ("oi", 4999), ("volume", 999)):
            self.edit("pick.json", lambda p: p.update({key: value}))
            self.assertEqual(self.failing()[0], ["spread_liquidity"], key)
            shutil.copy(os.path.join(OFF, "pick.json"), os.path.join("trader", "pick.json"))
        self.edit("pick.json", lambda p: [p.pop(k) for k in ("spread_pct", "oi", "volume")])
        failed, by = self.failing()                                    # bid and ask, then the feed's OI and volume
        self.assertEqual((failed, by["spread_liquidity"]["detail"]),
                         ([], "spread 0.09% (max 3%), OI 77,777 (min 5,000), volume 99,999 (min 1,000)"))
        self.edit("pick.json", lambda p: p["instrument"].update(token="45015"))
        failed, by = self.failing()
        self.assertEqual(failed, ["spread_liquidity"])

    def test_stock_options_are_never_carried_into_expiry(self):
        ages = {"61000": 500, "2885": 400}
        failed, by = self.failing(self.stock("61000", "2026-11-24", "CARRYFORWARD"), ages=ages)
        self.assertEqual(failed, ["stock_expiry"])
        self.assertIn("in-the-money stock options settle in shares", by["stock_expiry"]["detail"])
        self.assertEqual(self.failing(self.stock("61000", "2026-11-24", "INTRADAY"), ages=ages)[0], [])
        failed, by = self.failing(self.stock(product="CARRYFORWARD"), ages={"61010": 500, "2885": 400})
        self.assertEqual(failed, [])
        self.assertEqual(by["stock_expiry"]["detail"], "stock option, CARRYFORWARD, expires 2026-12-29, "
                                                       "next trading day 2026-11-25")

    def test_an_edited_ticket_and_a_changed_lot_size(self):
        failed, by = self.failing(self.ticket(lots=3))
        self.assertEqual(failed, ["ticket"])
        self.assertTrue(by["ticket"]["detail"].endswith("size it again with in-budget"))
        rows = smart.read_json(os.path.join(OFF, "master.json"))
        for r in rows:
            r["lotsize"] = "75" if r["token"] == "45014" else r["lotsize"]
        self.master(rows)
        failed, by = self.failing()
        self.assertEqual(failed, ["instrument"])
        self.master([r for r in rows if r["token"] != "45014"])
        self.assertEqual(self.failing()[0], ["instrument"])

    # ---- finality, exits, scope

    def test_a_block_is_final(self):
        self.edit("config.json", lambda c: c["account"].update(capital_inr=440000))
        self.verdict()
        before = smart.read_json(ORDERS)
        self.edit("config.json", lambda c: c["account"].update(capital_inr=500000))   # the user raises it by hand
        code, out, err = self.run_check("--now", NOW)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "in-risk: no DRAFT ticket to check for 2026-11-24 in trader/orders.json "
                              "(1 blocked, final): run in-budget first\n")
        code, _, err = self.run_check("--now", NOW, "--ticket", "T20261124-01")
        self.assertEqual(err, "in-risk: T20261124-01 was blocked at 09:42: a BLOCK is final, size a new ticket "
                              "with in-budget\n")
        self.assertEqual(smart.read_json(ORDERS), before)

    def test_a_pass_is_checked_again_and_can_turn_into_a_block(self):
        self.verdict()
        self.feed("2026-11-24 11:45", FRESH)
        code, _, _ = self.run_check("--now", "2026-11-24 11:45", "--ticket", "T20261124-01")
        self.assertEqual(code, 1)
        self.assertEqual(smart.read_json(ORDERS)["orders"][0]["risk_result"]["verdict"], "BLOCK")

    def test_an_exit_is_never_blocked(self):
        self.journal([trade("09:50", -9000.0)] * 3)
        r, by, code, _ = self.verdict(self.ticket(side="SELL"), now="2026-11-24 15:00", ages=None)
        self.assertEqual((r["verdict"], code, list(by)), ("PASS", 0, ["exit"]))

    def test_only_todays_drafts_are_checked(self):
        others = [self.ticket(ticket_id="T20261123-01", date="2026-11-23"),
                  self.ticket(ticket_id="T20261124-02", status="FILLED")]
        smart.write_json(ORDERS, {"orders": others})
        code, _, err = self.run_check("--now", NOW)
        self.assertEqual((code, err), (1, "in-risk: no DRAFT ticket to check for 2026-11-24 in trader/orders.json: "
                                          "run in-budget first\n"))
        code, _, err = self.run_check("--now", NOW, "--ticket", "T20261124-02")
        self.assertEqual(err, "in-risk: T20261124-02 is FILLED, not a DRAFT: only drafts are checked\n")
        self.assertEqual(smart.read_json(ORDERS), {"orders": others})

    def test_the_offline_master_and_the_files_own_keys(self):
        """No cached master: --offline DIR reads DIR/master.json, as in-chain and in-entry do. The rewrite
        keeps every top-level key of orders.json."""
        cached = os.path.join("trader", "cache", "instrument_master_2026-11-24.json")
        os.remove(cached)
        top = {"created_by": "in-budget", "as_of_ist": "2026-11-24T09:41:00+05:30",
               "source": "plan.json + config.json + charges.json"}
        smart.write_json(ORDERS, dict(top, orders=[self.base]))
        self.feed(NOW, FRESH)
        code, out, err = self.run_check("--now", NOW, "--offline", OFF)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ok   instrument          NIFTY01DEC2623500CE OPTIDX in today's master, lot 65", out)
        doc = smart.read_json(ORDERS)
        self.assertEqual({k: doc[k] for k in top}, top)
        self.assertFalse(os.path.exists(cached))          # nothing was downloaded

    def test_refuses_without_capital(self):
        self.edit("config.json", lambda c: c["account"].update(capital_inr=None))
        smart.write_json(ORDERS, {"orders": [self.base]})
        code, out, err = self.run_check("--now", NOW)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("set account.capital_inr in trader/config.json", err)


if __name__ == "__main__":
    unittest.main()
