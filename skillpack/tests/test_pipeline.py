"""The pack end to end: one trading day, offline, every skill's main() in pipeline
order, each step reading only the files the steps before it wrote (CONTRACTS.md).

Tuesday 2026-11-24. The fixtures in tests/fixtures/pipeline are synthetic, shaped
like Angel One's documented SmartAPI replies: a master slice (the NIFTY index, its
29DEC26 future and the 01DEC2026 chain from 23400 to 23700), the future's
5-minute candles for in-scanner (today, then the last sessions, in call order),
yesterday's ONE_DAY candle for in-entry, the index FULL quote and cautionaryScrips
for in-news, FULL option quotes, optionGreek and putCallRatio for in-chain
(premiums and Greeks from Black-Scholes on a 14% smile, so they agree). The
headlines are in-news's saved RSS file; the past days for in-timemachine come from
tests/fixtures/in-timemachine/candles.py. No number is real.

The live feed is a smart.TickStore written one tick a second as the day goes on
(250 ms of lag): NIFTY ranges 23,480 to 23,550 from 09:15 to 09:30, the 09:35 bar
closes 23,562.50 above it, then NIFTY climbs to 23,790 by 10:54; the 23650 CE the
pick chooses goes from 155.70 at 09:40 to 252.00.
"""

import glob
import io
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

firstrun = load("in-setup", "firstrun")
scan = load("in-scanner", "scan")
news = load("in-news", "news")
entry = load("in-entry", "entry")
chain = load("in-chain", "chain")
exit_ = load("in-exit", "exit")
size = load("in-budget", "size")
check = load("in-risk", "check")
ticket = load("in-ticket", "ticket")
alerts = load("in-alerts", "alerts")
tm = load("in-timemachine", "timemachine")
candles = load(os.path.join("tests", "fixtures", "in-timemachine"), "candles")
smart = firstrun.smart

PIPE = os.path.join(FIX, "pipeline")
RSS = os.path.join(FIX, "in-news", "feed-rss.xml")
DAY = "2026-11-24"
IDX, FUT, CE = "99926000", "53001", "45010"   # NIFTY, NIFTY29DEC26FUT, NIFTY01DEC2623650CE


def ms(hms):
    return int(smart.parse_ist("%s %s" % (DAY, hms)).timestamp() * 1000)


def at(hhmm):
    return "--now", "%s %s" % (DAY, hhmm)


INDEX_PATH = [(ms(h), p) for h, p in (
    ("09:15:00", 23500.0), ("09:17:00", 23480.0), ("09:19:59", 23520.0), ("09:22:00", 23550.0),
    ("09:24:59", 23540.0), ("09:27:00", 23500.0), ("09:29:59", 23510.0), ("09:32:00", 23545.0),
    ("09:34:59", 23530.0), ("09:37:00", 23570.0), ("09:39:59", 23562.5), ("09:44:59", 23562.5),
    ("10:20:00", 23660.0), ("10:54:00", 23790.0), ("11:05:00", 23790.0))]
CE_PATH = [(ms(h), p) for h, p in (("09:40:00", 155.7), ("09:44:59", 155.7), ("10:54:00", 252.0),
                                   ("11:05:00", 252.0))]


def price(path, t):
    """Linear between the waypoints, on the 0.05 tick; None outside them."""
    for (t0, p0), (t1, p1) in zip(path, path[1:]):
        if t0 <= t <= t1:
            return float(smart.round_tick(p0 + (p1 - p0) * (t - t0) / (t1 - t0), "0.05"))
    return None


def run(mod, *argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def on_tick(x, tick):
    return Decimal(str(x)) % Decimal(str(tick)) == 0


def rupees(text):
    return float(text.replace(",", ""))


class OneTradingDay(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        self.fed = ms("09:15:00")

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- the world outside the pack

    def feed_to(self, hhmm):
        """What in-common/feed.py would have written up to hhmm: one tick a second per token."""
        end = ms(hhmm + ":00")
        store = smart.TickStore(os.path.join("trader", "state", "ticks.sqlite"))
        t = self.fed
        while t < end:
            for token, ex, path in ((IDX, 1, INDEX_PATH), (CE, 2, CE_PATH)):
                px = price(path, t)
                if px is not None:
                    tick = {"token": token, "exchange_type": ex, "exch_ms": t, "ltp": px}
                    if token == CE:   # snapquote: cumulative volume and open interest
                        tick.update(volume=1250000 + (t - CE_PATH[0][0]) // 10, oi=3000000)
                    store.add(tick, t + 250)
            t += 1000
        store.close()
        self.fed = end

    def offline(self, step):
        """An --offline folder: the step's saved replies and the day's master."""
        dst = os.path.join(self.tmp, "off", step)
        src = os.path.join(PIPE, step)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            os.makedirs(dst)
        shutil.copy(os.path.join(PIPE, "master.json"), dst)
        return dst

    def step(self, mod, *argv):
        code, out, err = run(mod, *argv)
        self.assertEqual(code, 0, "%s %s\n%s%s" % (mod.__name__, " ".join(argv), out, err))
        return out

    def read(self, *path):
        return smart.read_json(os.path.join("trader", *path))

    # ---- the day

    def test_one_trading_day(self):
        # 1. in-setup init, then the values only the user owns
        self.step(firstrun, "init", *at("08:50"))
        cfg = self.read("config.json")
        cfg["account"]["capital_inr"] = 1000000
        cfg["api"]["risk_free_rate_pct"] = 6.5
        smart.write_json(os.path.join("trader", "config.json"), cfg)
        cal = self.read("calendar.json")
        cal.update(source="test calendar, synthetic", as_of="2026-11-01", holidays={"2026-10-02": "Gandhi Jayanti"})
        smart.write_json(os.path.join("trader", "calendar.json"), cal)
        smart.write_json(os.path.join("trader", "events.json"),
                         {"events": [{"date": "2026-11-26", "name": "Test policy meeting", "kind": "rbi"}]})

        # 2. what the live side would have left: today's master in the cache, the probe's
        # confirmed features, and the feed's tick store
        rows = smart.read_json(os.path.join(PIPE, "master.json"))
        smart.write_json(os.path.join("trader", "cache", "instrument_master_%s.json" % DAY), rows)
        need = set()
        for path in glob.glob(os.path.join(ROOT, "in-*", "*.py")):
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            need |= set(re.findall(r'smart\.require\(a\.trader, "(\w+)"\)', src))
            need |= set(re.findall(r'confirmed\(a, "(\w+)"\)', src))
        self.assertIn("quote_options", need)
        self.assertLessEqual(need, set(firstrun.FEATURES))   # every live path asks for what the probe confirms
        smart.write_json(os.path.join("trader", "state", "capabilities.json"), {
            "created_by": "in-setup", "as_of_ist": DAY + "T08:55:00+05:30", "source": "test: a live probe",
            "confirmed": {f: ["pipeline"] for f in sorted(need)}})
        for f in need:
            self.assertEqual(smart.require("trader", f), ["pipeline"])
        self.feed_to("09:31")

        # 3. the morning: news (no watchlist yet), scanner, the trigger, the pick
        self.assertFalse(os.path.exists(os.path.join("trader", "watchlist.json")))
        self.step(news, "--offline", self.offline("news"), "--rss-file", RSS, *at("09:31"))
        nw = self.read("news.json")
        self.assertEqual(list(nw["symbols"]), ["NIFTY"])                      # the indices in the master
        self.assertEqual((nw["partial"], nw["symbols"]["NIFTY"]["move_pct"], nw["symbols"]["NIFTY"]["flags"]),
                         (False, 0.34, []))
        self.assertEqual([h["title"] for h in nw["symbols"]["NIFTY"]["headlines"]],
                         ["Nifty set for a firm start as Asian markets rise"])
        self.assertEqual([e["name"] for e in nw["events_today"]], ["Test policy meeting"])

        self.step(scan, "orb", "--offline", self.offline("scan"), *at("09:31"))
        wl = self.read("watchlist.json")
        (nifty,) = wl["items"]
        self.assertEqual((wl["mode"], wl["partial"], nifty["symbol"], nifty["token"], nifty["source"]),
                         ("orb", False, "NIFTY", IDX, "feed"))
        self.assertEqual((nifty["range_high"], nifty["range_low"], nifty["volume_ratio"]), (23550.0, 23480.0, 1.3))
        self.assertEqual(nifty["flags"], nw["symbols"]["NIFTY"]["flags"])   # in-scanner read today's news.json

        self.feed_to("09:41")
        self.step(entry, "check", "NIFTY", "--offline", self.offline("entry"), *at("09:41"))
        en = self.read("entry.json")
        self.assertEqual((en["status"], en["direction"], en["trigger_level"], en["bar_start_ist"], en["bar_close_ist"],
                          en["bar_close"], en["source"], en["notes"]),
                         ("triggered", "bullish", 23550.0, "09:35", "09:40", 23562.5, "feed", []))
        self.assertEqual((en["opening_range"]["high"], en["opening_range"]["low"]),
                         (nifty["range_high"], nifty["range_low"]))           # in-scanner and in-entry agree

        self.step(chain, "pick", "NIFTY", "--offline", self.offline("chain"), *at("09:41"))
        pick = self.read("pick.json")
        inst = pick["instrument"]
        row = next(r for r in rows if r["token"] == inst["token"])
        self.assertEqual(inst, {"tradingsymbol": row["symbol"], "token": CE, "segment": "NFO", "underlying": "NIFTY",
                                "strike": 23650.0, "type": "CE", "expiry": "2026-12-01", "expiry_raw": "01DEC2026",
                                "lot_size": int(row["lotsize"]), "tick_size": 0.05, "freeze_qty": 1801})
        self.assertEqual((pick["direction"], pick["premium"], pick["bid"], pick["ask"], pick["delta"], pick["spread_pct"],
                          pick["volume"], pick["oi"], pick["greeks_source"]),
                         ("bullish", 155.7, 155.65, 155.75, 0.4543, 0.06, 1255900, 3000000, "API"))   # the 09:40:59 tick
        self.assertEqual(pick["next_best"]["token"], "45012")

        # 4. the plan, the size, the gate, the ticket
        self.step(exit_, "plan", "--write", *at("09:41"))
        plan = self.read("plan.json")
        self.assertEqual((plan["entry_underlying"], plan["underlying_stop"], plan["target_underlying"]),
                         (23562.5, 23515.0, 23657.5))                          # or-mid, 2R
        self.assertEqual((plan["entry_premium"], plan["premium_stop"], plan["target_premium"], plan["r_value_inr"]),
                         (155.7, 108.95, 249.2, 3038.75))                      # 30% under, rounded down; 2R

        sized = self.step(size, "--write", *at("09:42"))
        (o,) = self.read("orders.json")["orders"]
        self.assertEqual((o["ticket_id"], o["status"], o["lots"], o["quantity"], o["order_quantities"], o["limit_price"],
                          o["premium_stop"], o["target_premium"], o["underlying_stop"], o["target_underlying"]),
                         ("T20261124-01", "DRAFT", 3, 195, [195], 155.7, 108.95, 249.2, 23515.0, 23657.5))
        risk = re.search(r"Risk\s+Rs ([\d,.]+) at the stop \+ Rs ([\d,.]+) charges = Rs ([\d,.]+) of", sized)
        self.assertEqual(o["risk_inr"], {"premium_at_stop": rupees(risk.group(1)),
                                         "charges_round_trip": rupees(risk.group(2)), "total": rupees(risk.group(3))})
        self.assertEqual(o["capital_used_inr"], rupees(re.search(r"Capital used\s+Rs ([\d,.]+)", sized).group(1)))
        ch = self.read("charges.json")
        trip = smart.option_charges("BUY", "155.70", 195, "NFO", ch)["total"] + \
            smart.option_charges("SELL", "108.95", 195, "NFO", ch)["total"]
        self.assertEqual(o["risk_inr"]["charges_round_trip"], float(trip))

        self.feed_to("09:42")
        self.step(check, *at("09:42"))
        rr = self.read("orders.json")["orders"][0]["risk_result"]
        self.assertEqual((rr["verdict"], rr["reasons"]), ("PASS", []))
        by = {c["name"]: c["detail"] for c in rr["checks"]}
        self.assertEqual(by["spread_liquidity"], "spread 0.06% (max 3%), OI 3,000,000 (min 5,000), "
                                                 "volume 1,255,900 (min 1,000)")   # pick.json's, not the 09:41:59 tick
        self.assertEqual(by["trades_per_day"], "0 filled today, limit 3")

        shown = self.step(ticket, "show", *at("09:42"))
        self.assertIn("TICKET T20261124-01  READY (in-risk PASS at 09:42)", shown)
        self.assertIn("Flat by       15:10 IST (plan.json)", shown)
        self.step(ticket, "fill", "T20261124-01", "--price", "155.70", "--time", "09:43", *at("09:43"))

        # 5. the trade: in-alerts rings the target from the feed, the user closes it by hand
        self.feed_to("10:55")
        rang = self.step(alerts, "check", *at("10:55"))
        self.assertIn("ALERT T20261124-01 target_premium: premium 252.00", rang)
        self.assertIn("ALERT T20261124-01 target_underlying: NIFTY 23,790.00", rang)
        self.assertNotIn("stop_", rang)
        # the plan's entry and no-progress bars reach in-alerts through the ticket; 09:45 to 09:50 closed above
        self.assertIn("no progress: cleared, NIFTY closed above the entry 23,562.50 at 09:50", rang)
        self.assertEqual(self.read("state", "alerted.json"),
                         {DAY: ["T20261124-01:target_premium", "T20261124-01:target_underlying"]})
        self.step(ticket, "close", "T20261124-01", "--price", "251.50", "--time", "10:55", "--reason", "target",
                  *at("10:56"))
        (t,) = self.read("state", "journal.json")["trades"]
        buy = smart.option_charges("BUY", "155.70", 195, "NFO", ch)["total"]
        sell = smart.option_charges("SELL", "251.50", 195, "NFO", ch)["total"]
        gross = (Decimal("251.50") - Decimal("155.70")) * 195
        self.assertEqual((t["gross_pnl_inr"], t["charges_inr"], t["net_pnl_inr"]),
                         (float(gross), float(buy + sell), float(gross - buy - sell)))
        self.assertEqual(self.read("orders.json")["orders"][0]["status"], "CLOSED")

        # 6. a second draft: in-risk now counts the journal's trade
        self.step(size, "--write", *at("11:00"))
        self.feed_to("11:00")
        self.step(check, *at("11:00"))
        o1, o2 = self.read("orders.json")["orders"]
        self.assertEqual((o2["ticket_id"], o2["risk_result"]["verdict"]), ("T20261124-02", "PASS"))
        by = {c["name"]: c["detail"] for c in o2["risk_result"]["checks"]}
        self.assertEqual(by["trades_per_day"], "1 filled today, limit 3")
        self.assertEqual(by["losses_in_a_row"], "0 losses in a row today (1 closed), stop at 3")
        self.assertTrue(by["daily_loss_limit"].startswith("realised loss 0.00 + this trade "), by["daily_loss_limit"])

        # 7. in-timemachine replays the plan's rule, charges from the plan
        off = self.offline("timemachine")
        smart.write_json(os.path.join(off, "getCandleData.json"), candles.reply(candles.rows()))
        self.step(tm, "run", "--offline", off, "--write", *at("11:05"))
        doc = self.read("timemachine.json")
        self.assertEqual((doc["symbol"], doc["rule"]["stop"], doc["rule"]["stop_from"], doc["rule"]["target_r"]),
                         ("NIFTY", "or_mid", "plan.json", 2.0))
        one_lot = smart.option_charges("BUY", "155.70", 65, "NFO", ch)["total"] + \
            smart.option_charges("SELL", "108.95", 65, "NFO", ch)["total"]
        self.assertEqual((doc["charges"]["tradingsymbol"], doc["charges"]["round_trip_inr"], doc["charges"]["in_r"]),
                         (inst["tradingsymbol"], float(one_lot), round(float(one_lot / Decimal("3038.75")), 4)))

        # the handoffs line up
        lot, tick = int(row["lotsize"]), smart.tick_inr(row, smart.strike_scale(rows)[0])
        for doc_inst in (plan["instrument"], o1["instrument"], o2["instrument"], t["instrument"]):
            self.assertEqual(doc_inst, inst)                                   # unchanged from pick.json on
        for q in [o1["quantity"], t["quantity"]] + o1["order_quantities"] + t["executed_orders"]:
            self.assertEqual(q % lot, 0, q)
        for x in (pick["premium"], pick["bid"], pick["ask"], plan["entry_premium"], plan["premium_stop"],
                  plan["target_premium"], o1["limit_price"], o1["premium_stop"], o1["target_premium"],
                  t["fill_price"], t["exit_price"]):
            self.assertTrue(on_tick(x, tick), x)
        for x in (plan["underlying_stop"], plan["target_underlying"]):
            self.assertTrue(on_tick(x, "0.05"), x)
        self.assertEqual(t["flat_by"], plan["flat_by"])
        wanted, _ = smart.wanted("trader", 900)
        self.assertLessEqual({(1, IDX), (2, CE), (2, FUT)}, set(wanted))     # every reader asked the feed


if __name__ == "__main__":
    unittest.main()
