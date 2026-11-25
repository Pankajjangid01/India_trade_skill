"""in-budget tests, all offline.

The fixtures in tests/fixtures/in-budget are synthetic: a plan.json and a
config.json shaped like CONTRACTS.md, and the in-setup template's unverified
charges. Every number below is worked by hand from those rates:

  one NIFTY lot (65) BUY at 112.40: turnover 7,306.00; brokerage 20.00, exchange
  0.03503% 2.56, SEBI 0.01, stamp 0.003% 0.22, GST 18% of (20 + 2.5593 + 0.0073)
  4.06 = 26.85. SELL at 78.70: turnover 5,115.50; 20.00 + 1.79 + 0.01 + STT
  0.15% 7.67 + GST 3.92 = 33.39. Round trip 60.24.
  two lots (130) in one order: BUY 20 + 5.12 + 0.01 + 0.44 + 4.52 = 30.09,
  SELL 20 + 3.58 + 0.01 + 15.35 + 4.25 = 43.19. Round trip 73.28.
"""

import io
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

size = load("in-budget", "size")
smart = size.smart
OFF = os.path.join(FIX, "in-budget")
NOW = "2026-11-24 09:41"
ORDERS = os.path.join("trader", "orders.json")


def skill_example(heading):
    with open(os.path.join(ROOT, "in-budget", "SKILL.md"), encoding="utf-8") as fh:
        return re.search(re.escape(heading) + r".*?```text\n(.*?)```", fh.read(), re.S).group(1)


class Budget(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        shutil.copytree(OFF, "trader")

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_size(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = size.main(["--now", NOW] + list(args))
        return code, out.getvalue(), err.getvalue()

    def edit(self, name, fn):
        path = os.path.join("trader", name)
        doc = smart.read_json(path)
        fn(doc)
        smart.write_json(path, doc)

    def account(self, **kv):
        self.edit("config.json", lambda c: c["account"].update(kv))

    def only_order(self):
        (o,) = smart.read_json(ORDERS)["orders"]
        return o

    def test_round_trip_charges_by_hand(self):
        ch = smart.read_json(os.path.join("trader", "charges.json"))
        e, s = Decimal("112.4"), Decimal("78.7")
        self.assertEqual(smart.option_charges("BUY", e, 130, "NFO", ch)["total"], Decimal("30.09"))
        self.assertEqual(smart.option_charges("SELL", s, 130, "NFO", ch)["total"], Decimal("43.19"))
        self.assertEqual(size.round_trip([130], e, s, "NFO", ch), Decimal("73.28"))
        self.assertEqual(size.round_trip([65], e, s, "NFO", ch), Decimal("60.24"))
        self.assertEqual(size.round_trip([130, 130, 65], e, s, "NFO", ch), Decimal("206.80"))  # a brokerage each

    def test_the_example_in_skill_md(self):
        code, out, err = self.run_size("--write")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: two lots"))
        o = self.only_order()
        self.assertEqual((o["ticket_id"], o["status"], o["side"], o["product"]), ("T20261124-01", "DRAFT", "BUY", "INTRADAY"))
        self.assertEqual((o["lots"], o["quantity"], o["order_quantities"]), (2, 130, [130]))
        self.assertEqual(o["risk_inr"], {"premium_at_stop": 4381.0, "charges_round_trip": 73.28, "total": 4454.28})
        self.assertEqual((o["limit_price"], o["premium_stop"], o["capital_used_inr"]), (112.4, 78.7, 14612.0))
        self.assertEqual((o["target_premium"], o["underlying_stop"], o["target_underlying"]), (179.8, 23515.0, 23657.5))
        self.assertEqual((o["entry_underlying"], o["no_progress_bars"]), (23562.5, 6))   # in-alerts' no-progress watch
        doc = smart.read_json(ORDERS)
        self.assertEqual((doc["created_by"], doc["as_of_ist"], doc["source"]),
                         ("in-budget", "2026-11-24T09:41:00+05:30", "plan.json + config.json + charges.json"))
        self.assertIs(o["charges_verified"], False)
        self.assertIsNone(o["risk_result"])
        self.assertEqual(o["instrument"]["tradingsymbol"], "NIFTY01DEC2623500CE")

    def test_a_preview_writes_nothing(self):
        code, out, _ = self.run_size()
        self.assertEqual(code, 0)
        self.assertIn("Preview only: run with --write to save it as DRAFT ticket T20261124-01.", out)
        self.assertFalse(os.path.exists(ORDERS))

    def test_charges_take_a_lot_off(self):
        # max loss 4,400: two lots are 4,381.00 at the stop, under it, but 4,381.00 + 73.28 is not; one lot is
        self.account(capital_inr=440000)
        code, out, _ = self.run_size("--write")
        self.assertEqual(code, 0)
        self.assertIn("  Lots           max loss 2, capital cap 18: 1 lot, decided by max loss with charges", out)
        o = self.only_order()
        self.assertEqual((o["lots"], o["quantity"]), (1, 65))
        self.assertEqual(o["risk_inr"], {"premium_at_stop": 2190.5, "charges_round_trip": 60.24, "total": 2250.74})

    def test_the_freeze_split_pays_a_brokerage_per_order(self):
        # a freeze of 131 allows 2 lots an order; max loss 12,000 fits 5 lots: 10,952.50 + 2 x 73.28 + 60.24
        self.account(capital_inr=1200000)
        self.edit("plan.json", lambda p: p["instrument"].update(freeze_qty=131))
        code, out, _ = self.run_size("--write")
        self.assertEqual(code, 0)
        self.assertIn("  Orders         325 units in 3 orders [130, 130, 65]; freeze 131 allows 2 lots an order", out)
        o = self.only_order()
        self.assertEqual((o["lots"], o["quantity"], o["order_quantities"]), (5, 325, [130, 130, 65]))
        self.assertEqual(o["risk_inr"], {"premium_at_stop": 10952.5, "charges_round_trip": 206.8, "total": 11159.3})

    def test_the_real_freeze_quantity_splits_a_big_size(self):
        # NIFTY's 1801 allows 27 lots an order; max loss 65,000 fits 29 lots by premium, charges included
        self.account(capital_inr=6500000)
        self.run_size("--write")
        o = self.only_order()
        self.assertEqual((o["lots"], o["order_quantities"]), (29, [1755, 130]))
        r = o["risk_inr"]
        self.assertAlmostEqual(r["total"], r["premium_at_stop"] + r["charges_round_trip"], places=2)
        self.assertLessEqual(r["total"], 65000)

    def test_the_capital_cap_decides(self):
        # max loss 5% of 1,00,000 = 5,000 fits 2 lots; the 10% cap (10,000) buys 1 lot of 7,306.00
        self.account(capital_inr=100000, risk_per_trade_pct=5, max_capital_per_trade_pct=10)
        code, out, _ = self.run_size("--write")
        self.assertEqual(code, 0)
        self.assertIn("  Lots           max loss 2, capital cap 1: 1 lot, decided by capital cap", out)
        self.assertEqual(self.only_order()["capital_used_inr"], 7306.0)

    def test_cash_caps_the_capital_used(self):
        code, out, _ = self.run_size("--cash", "10000", "--write")
        self.assertEqual(code, 0)
        self.assertIn("  Lots           max loss 2, capital cap 20, cash 1: 1 lot, decided by cash", out)
        self.assertEqual(self.only_order()["lots"], 1)

    def test_zero_lots_says_what_one_lot_risks_and_writes_nothing(self):
        self.account(capital_inr=200000)
        code, out, err = self.run_size("--write")
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(out, skill_example("## Example: zero lots"))
        self.assertFalse(os.path.exists(ORDERS))

    def test_zero_lots_by_cash(self):
        code, out, _ = self.run_size("--cash", "5000", "--write")
        self.assertEqual(code, 1)
        self.assertTrue(out.endswith("No ticket: one lot costs Rs 7,306.00, over the Rs 5,000.00 cash you gave.\n"))
        self.assertFalse(os.path.exists(ORDERS))

    def test_tickets_append_with_the_next_number(self):
        smart.write_json(ORDERS, {"orders": [{"ticket_id": "T20261124-07", "status": "CLOSED"},
                                             {"ticket_id": "T20261123-09", "status": "CLOSED"}], "note": "kept"})
        self.run_size("--write")
        self.run_size("--write", "--now", "2026-11-24 09:45")
        doc = smart.read_json(ORDERS)
        self.assertEqual([o["ticket_id"] for o in doc["orders"]],
                         ["T20261124-07", "T20261123-09", "T20261124-08", "T20261124-09"])
        self.assertEqual((doc["created_by"], doc["as_of_ist"], doc["note"]),
                         ("in-budget", "2026-11-24T09:45:00+05:30", "kept"))   # an append stamps the file

    def test_verified_charges_say_so(self):
        self.edit("charges.json", lambda c: c.update(verified=True))
        _, out, _ = self.run_size("--write")
        self.assertIn("  Charges        verified (charges.json)", out)
        self.assertNotIn("UNVERIFIED", out)
        self.assertIs(self.only_order()["charges_verified"], True)

    def test_refusals_are_one_plain_line(self):
        self.account(capital_inr=None)
        code, out, err = self.run_size("--write")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "in-budget: set account.capital_inr in trader/config.json: the pack refuses to size or "
                              "check trades without it\n")
        self.account(capital_inr=500000)
        code, _, err = self.run_size("--now", "2026-11-25 09:41")
        self.assertEqual(err, "in-budget: plan.json is for 2026-11-24, not today (2026-11-25): run in-exit again\n")
        self.edit("plan.json", lambda p: p.update(premium_stop=112.4))
        code, _, err = self.run_size()
        self.assertEqual(err, "in-budget: plan.json's premium stop 112.4 is not below the entry 112.4: "
                              "run in-exit again\n")
        self.assertFalse(os.path.exists(ORDERS))


if __name__ == "__main__":
    unittest.main()
