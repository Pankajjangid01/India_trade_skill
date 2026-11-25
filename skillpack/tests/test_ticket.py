"""in-ticket tests, all offline, on a pinned clock.

The fixtures in tests/fixtures/in-ticket are synthetic, shaped like
CONTRACTS.md: three tickets (a PASS, a BLOCK, and a PASS split by NIFTY's
freeze quantity), a plan.json and a copy of in-setup's build-time charges.
Nothing here calls an API: the script has no client at all.
"""

import csv
import io
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

ticket = load("in-ticket", "ticket")
smart = ticket.smart
OFF = os.path.join(FIX, "in-ticket")
DAY = "2026-11-24"


def skill_example(heading):
    """The ```text block under `heading` in in-ticket/SKILL.md."""
    with open(os.path.join(ROOT, "in-ticket", "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    return re.search(re.escape(heading) + r".*?```text\n(.*?)```", text, re.S).group(1)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def at(hm):
    return "%s %s" % (DAY, hm)


class InTemp(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        os.makedirs("trader")
        for f in os.listdir(OFF):
            shutil.copy(os.path.join(OFF, f), os.path.join("trader", f))

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def order(self, tid):
        return ticket.find(smart.read_json(os.path.join("trader", "orders.json")), tid)

    def trade(self, tid):
        trades = smart.read_json(os.path.join("trader", "state", "journal.json"))["trades"]
        return next(t for t in trades if t["ticket_id"] == tid)

    def edit_order(self, tid, **changes):
        path = os.path.join("trader", "orders.json")
        doc = smart.read_json(path)
        ticket.find(doc, tid).update(changes)
        smart.write_json(path, doc)


class Examples(InTemp):
    def test_the_morning_tickets(self):
        code, out, err = run("show", "--now", at("09:42"))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, skill_example("## Example: the morning tickets"))

    def test_a_fill_and_its_exit(self):
        c1, fill, _ = run("fill", "T20261124-01", "--price", "112.40", "--time", "09:41", "--now", at("09:43"))
        c2, close, _ = run("close", "T20261124-01", "--price", "150.00", "--time", "10:55", "--reason", "target",
                           "--now", at("10:56"))
        self.assertEqual((c1, c2), (0, 0))
        self.assertEqual(fill + close, skill_example("## Example: a fill and its exit"))


class Tickets(InTemp):
    def block(self, out, tid):
        return out.split("TICKET " + tid, 1)[1].split("\n\n", 1)[0]

    def test_a_block_shows_reasons_and_nothing_to_type(self):
        _, out, _ = run("show", "T20261124-02", "--now", at("09:42"))
        b = self.block(out, "T20261124-02")
        self.assertIn("NOT READY: do not type it", b)
        self.assertIn("spread: 4.2% is over max_spread_pct 3.0", b)
        for typed in ("LIMIT", "Quantity", "210", "order 1 of"):
            self.assertNotIn(typed, b)
        self.assertNotIn("Near the close", out)        # no READY ticket, no closing-auction block

    def test_the_freeze_split_is_one_line_per_order(self):
        _, out, _ = run("show", "T20261124-03", "--now", at("09:42"))
        b = self.block(out, "T20261124-03")
        self.assertIn("1,950 (30 lots of 65) in 2 orders, freeze quantity 1801", b)
        self.assertIn("order 1 of 2: 1,755 (27 lots)\n                order 2 of 2: 195 (3 lots)", b)
        self.assertEqual(smart.max_lots_per_order(1801, 65), 27)    # the split the ticket shows

    def test_not_ready_without_a_check_on_another_day_or_with_a_bad_split(self):
        self.edit_order("T20261124-01", risk_result=None)
        self.edit_order("T20261124-03", order_quantities=[1950])           # over the freeze quantity
        _, out, _ = run("show", "--now", at("09:42"))
        self.assertIn("in-risk has not checked it: run in-risk", self.block(out, "T20261124-01"))
        self.assertIn("do not split quantity 1950 into whole lots under the freeze quantity 1801",
                      self.block(out, "T20261124-03"))
        _, out, _ = run("show", "T20261124-01", "--now", "2026-11-25 09:42")
        self.assertIn("dated 2026-11-24, not today", out)
        code, _, err = run("show", "--now", "2026-11-25 09:42")
        self.assertEqual(code, 1)
        self.assertIn("no ticket dated 2026-11-25", err)

    def test_the_script_has_no_client(self):
        with open(os.path.join(ROOT, "in-ticket", "ticket.py"), encoding="utf-8") as fh:
            src = fh.read()
        for word in ("open_client", "Client(", "urllib", "subscribe("):
            self.assertNotIn(word, src)


class Journal(InTemp):
    def test_fill_and_close_arithmetic_by_hand(self):
        run("fill", "T20261124-01", "--price", "112.40", "--time", "09:41", "--now", at("09:43"))
        t = self.trade("T20261124-01")
        self.assertEqual((t["quantity"], t["executed_orders"], t["flat_by"], t["exit_price"]), (130, [130], "15:05", None))
        self.assertEqual(self.order("T20261124-01")["status"], "FILLED")
        run("close", "T20261124-01", "--price", "150", "--time", "10:55", "--reason", "target", "--now", at("10:56"))
        t = self.trade("T20261124-01")
        # buy 112.40 x 130 = 14,612.00: brokerage 20, exchange 0.03503% = 5.12, SEBI 10/crore = 0.01,
        #   stamp 0.003% = 0.44, GST 18% of (20 + 5.1186 + 0.0146) = 4.52; total 30.09
        # sell 150.00 x 130 = 19,500.00: brokerage 20, exchange 6.83, SEBI 0.02, STT 0.15% = 29.25,
        #   GST 18% of (20 + 6.8309 + 0.0195) = 4.83; total 60.93
        self.assertEqual(t["charges_detail"], {"brokerage": 40.0, "exchange": 11.95, "sebi": 0.03, "stt": 29.25,
                                               "stamp": 0.44, "gst": 9.35})
        self.assertEqual((t["charges_inr"], t["gross_pnl_inr"], t["net_pnl_inr"]), (91.02, 4888.0, 4796.98))
        self.assertEqual((t["segment_class"], t["exit_reason"], t["exit_date"]), ("FO_OPTIONS", "target", DAY))
        o = self.order("T20261124-01")
        self.assertEqual((o["status"], o["exit"]["price"], o["fill"]["price"]), ("CLOSED", 150.0, 112.4))
        top = smart.read_json(os.path.join(OFF, "orders.json"))
        doc = smart.read_json(os.path.join("trader", "orders.json"))
        self.assertEqual({k: doc[k] for k in top if k != "orders"},   # in-budget's created_by, as_of_ist, source
                         {k: top[k] for k in top if k != "orders"})

    def test_a_freeze_split_pays_one_brokerage_per_executed_order(self):
        run("fill", "T20261124-03", "--price", "168.50", "--time", "09:50", "--now", at("09:51"))
        self.assertEqual(self.trade("T20261124-03")["executed_orders"], [1755, 195])
        code, out, _ = run("close", "T20261124-03", "--price", "117.95", "--time", "11:02", "--reason", "stop",
                           "--now", at("11:03"))
        self.assertEqual(code, 0)
        t = self.trade("T20261124-03")
        # buy: 1,755 @ 168.50 = 155.06 (20 + 103.59 + 0.30 + 8.87 stamp + 22.30 GST), 195 @ 168.50 = 38.21
        # sell: 1,755 @ 117.95 = 419.91 (20 + 72.51 + 0.21 + 310.50 STT + 16.69 GST), 195 @ 117.95 = 67.63
        self.assertIn("Charges     680.81 for 2 orders each way (buy 193.27, sell 487.54)", out)
        self.assertEqual(t["charges_detail"]["brokerage"], 80.0)
        self.assertEqual((t["charges_inr"], t["gross_pnl_inr"], t["net_pnl_inr"]), (680.81, -98572.5, -99253.31))

    def test_a_part_fill_is_whole_lots(self):
        code, _, err = run("fill", "T20261124-03", "--price", "168.50", "--time", "09:50", "--qty", "100", "--now", at("09:51"))
        self.assertEqual(code, 1)
        self.assertIn("multiple of the lot size 65", err)
        run("fill", "T20261124-03", "--price", "168.50", "--time", "09:50", "--qty", "1820", "--now", at("09:51"))
        self.assertEqual(self.trade("T20261124-03")["executed_orders"], [1755, 65])

    def test_the_csv_for_the_accountant(self):
        run("fill", "T20261124-01", "--price", "112.40", "--time", "09:41", "--now", at("09:43"))
        run("fill", "T20261124-03", "--price", "168.50", "--time", "09:50", "--now", at("09:51"))
        run("close", "T20261124-01", "--price", "150.00", "--time", "10:55", "--reason", "target", "--now", at("10:56"))
        with open(os.path.join("trader", "journal_2026.csv"), encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)                      # the open trade is not in it
        r = rows[0]
        self.assertEqual(list(r), list(ticket.CSV_COLUMNS))
        self.assertEqual({k: r[k] for k in ("ticket_id", "segment_class", "exchange", "tradingsymbol", "option_type",
                                            "strike", "quantity", "buy_value_inr", "sell_value_inr", "gross_pnl_inr",
                                            "stt_inr", "charges_inr", "net_pnl_inr", "financial_year")},
                         {"ticket_id": "T20261124-01", "segment_class": "FO_OPTIONS", "exchange": "NFO",
                          "tradingsymbol": "NIFTY01DEC2623500CE", "option_type": "CE", "strike": "23500",
                          "quantity": "130", "buy_value_inr": "14612.00", "sell_value_inr": "19500.00",
                          "gross_pnl_inr": "4888.00", "stt_inr": "29.25", "charges_inr": "91.02",
                          "net_pnl_inr": "4796.98", "financial_year": "2026-27"})
        self.assertEqual(sorted(os.listdir(os.path.join("trader", "state"))), ["journal.json"])   # atomic, no leftovers
        _, out, _ = run("journal", "--now", at("16:00"))
        self.assertIn("Today       1 closed, net 4,796.98; 1 open", out)
        self.assertIn("T20261124-03 NIFTY01DEC2623400CE 1,950, bought 168.50 at 09:50: OPEN since 2026-11-24", out)

    def test_statuses_and_typo_guards(self):
        self.assertEqual(run("close", "T20261124-01", "--price", "150", "--time", "10:55", "--reason", "stop",
                             "--now", at("10:56"))[2].strip(),
                         "in-ticket: T20261124-01 is DRAFT: only a FILLED ticket can be closed")
        self.assertIn("not a multiple of the tick 0.05",
                      run("fill", "T20261124-01", "--price", "112.43", "--time", "09:41", "--now", at("09:43"))[2])
        run("fill", "T20261124-01", "--price", "112.40", "--time", "09:41", "--now", at("09:43"))
        self.assertIn("is FILLED: only a DRAFT ticket can be filled",
                      run("fill", "T20261124-01", "--price", "112.40", "--time", "09:41", "--now", at("09:43"))[2])
        self.assertIn("exit 09:30 is before the fill 09:41",
                      run("close", "T20261124-01", "--price", "100", "--time", "09:30", "--reason", "stop",
                          "--now", at("10:00"))[2])
        self.assertEqual(run("skip", "T20261124-02", "--now", at("09:45"))[0], 0)
        self.assertEqual(self.order("T20261124-02")["status"], "SKIPPED")
        self.assertIn("is SKIPPED", run("fill", "T20261124-02", "--price", "210", "--time", "09:46",
                                        "--now", at("09:47"))[2])

    def test_a_fill_on_a_block_is_recorded_with_a_note(self):
        code, out, _ = run("fill", "T20261124-02", "--price", "210.00", "--time", "09:46", "--now", at("09:47"))
        self.assertEqual(code, 0)
        self.assertIn("in-risk did not pass this ticket today: recorded because you placed it, not approved", out)
        self.assertEqual(self.trade("T20261124-02")["executed_orders"], [40])


if __name__ == "__main__":
    unittest.main()
