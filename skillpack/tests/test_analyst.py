"""in-analyst tests, all offline.

The company (LMNTECH) and its numbers are synthetic, round enough to check
by hand: version 1 has no results feed, so there is nothing to replay; the
user types the numbers from the exchange filing.
"""

import io
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, ROOT

facts = load("in-analyst", "facts")
smart = facts.smart
NOW = "2026-11-24 10:00"
Q1 = ["add", "LMNTECH", "--quarter", "Q1FY27", "--sales", "12,000", "--ebitda", "2700", "--profit", "1560",
      "--eps", "15.60", "--source", "NSE filing 2026-07-15, consolidated", "--now", "2026-07-15 19:00"]
Q2 = ["add", "LMNTECH", "--quarter", "Q2FY27", "--sales", "12480", "--ebitda", "2745.60", "--profit", "1622.40",
      "--eps", "16.22", "--source", "NSE filing 2026-10-14, consolidated", "--now", "2026-10-14 19:10"]
Q2_LAST_YEAR = ["add", "LMNTECH", "--quarter", "Q2FY26", "--sales", "10400", "--ebitda", "2080", "--profit", "1352",
                "--eps", "13.52", "--source", "BSE filing 2025-10-16, consolidated", "--now", NOW]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(argv)
    return code, out.getvalue(), err.getvalue()


def skill_example(heading):
    with open(os.path.join(ROOT, "in-analyst", "SKILL.md"), encoding="utf-8") as fh:
        return re.search(re.escape(heading) + r".*?```text\n(.*?)```", fh.read(), re.S).group(1)


class InTemp(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)


class Examples(InTemp):
    def test_adding_a_quarter_flags_the_missing_one(self):
        run(Q1)
        code, out, err = run(Q2)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, skill_example("## Example: adding a quarter"))

    def test_three_quarters(self):
        for argv in (Q1, Q2, Q2_LAST_YEAR):
            run(argv)
        code, out, err = run(["show", "lmntech", "--now", NOW])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, skill_example("## Example: three quarters"))

    def test_the_checklist(self):
        smart.write_json(os.path.join("trader", "events.json"), {"events": [
            {"date": "2027-01-14", "name": "LMNTECH Q3 results", "kind": "results"},
            {"date": "2026-12-05", "name": "RBI policy", "kind": "rbi"},
            {"date": "2026-12-01", "name": "LMNTECH AGM", "kind": "agm"}]})       # not a results day
        code, out, err = run(["checklist", "LMNTECH", "--now", NOW])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, skill_example("## Example: the checklist"))


class Arithmetic(unittest.TestCase):
    def test_growth_and_margins_by_hand(self):
        D = Decimal
        self.assertEqual(facts.growth(D("12480"), D("10400")), D("20.0"))        # 2,080 on 10,400
        self.assertEqual(facts.growth(D("2745.60"), D("2700")), D("1.7"))        # 45.60 / 2,700 = 1.69%
        self.assertEqual(facts.growth(D("102.25"), D("100")), D("2.3"))          # half up, not to even
        self.assertEqual(facts.growth(D("90"), D("120")), D("-25.0"))
        self.assertIsNone(facts.growth(D("100"), D("-50")))                      # against a loss: n/a
        self.assertIsNone(facts.growth(D("100"), None))
        self.assertEqual(facts.pct(D("2745.60"), D("12480")), D("22.0"))
        self.assertEqual(facts.pct(D("-120"), D("1000")), D("-12.0"))           # an EBITDA loss is a negative margin

    def test_quarters_follow_the_indian_financial_year(self):
        self.assertEqual(facts.quarter("q3 fy2027"), (3, 27))
        self.assertEqual(facts.label(facts.previous((1, 27))), "Q4FY26")
        self.assertEqual(facts.label(facts.previous((3, 27))), "Q2FY27")
        self.assertEqual(facts.label(facts.year_ago((1, 27))), "Q1FY26")
        with self.assertRaises(smart.SmartError):
            facts.quarter("Q5FY27")


class Store(InTemp):
    def test_a_loss_base_says_na_and_a_missing_quarter_is_named(self):
        run(["add", "ABC", "--quarter", "Q4FY26", "--sales", "500", "--ebitda", "-20", "--profit", "-50",
             "--source", "BSE filing", "--now", NOW])
        code, out, _ = run(["add", "ABC", "--quarter", "Q1FY27", "--sales", "550", "--ebitda", "30", "--profit", "10",
                            "--source", "BSE filing", "--now", NOW])
        self.assertIn("  Q1FY27 vs Q4FY26 (QoQ)  sales +10.0%  EBITDA n/a (base not above zero)  "
                      "profit n/a (base not above zero)\n", out)
        self.assertIn("  Q1FY27 vs Q1FY26 (YoY)  missing Q1FY26: add it to compare", out)
        self.assertIn("EBITDA -20.00 (margin -4.0%)  profit -50.00 (net margin -10.0%)  [source: BSE filing]", out)

    def test_adding_again_replaces_and_a_missing_source_is_said(self):
        run(Q1)
        code, out, _ = run(["add", "LMNTECH", "--quarter", "Q1FY27", "--sales", "12001", "--ebitda", "2700",
                            "--profit", "1560", "--now", NOW])
        self.assertTrue(out.startswith("Replaced LMNTECH Q1FY27 in trader/analyst.json\n  Source not given"))
        self.assertIn("[source: not given]", out)
        q = smart.read_json(os.path.join("trader", "analyst.json"))["symbols"]["LMNTECH"]["Q1FY27"]
        self.assertEqual((q["sales_cr"], q["eps"], q["source"]), (12001.0, None, "not given"))

    def test_sales_must_be_above_zero(self):
        code, _, err = run(["add", "X", "--quarter", "Q1FY27", "--sales", "0", "--ebitda", "1", "--profit", "1"])
        self.assertEqual(code, 1)
        self.assertIn("--sales must be above zero", err)

    def test_show_without_quarters_and_checklist_without_events(self):
        code, _, err = run(["show", "XYZ", "--now", NOW])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("in-analyst: no quarters for XYZ in trader/analyst.json"))
        code, out, _ = run(["checklist", "XYZ", "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("  Results day     no trader/events.json (your own list of dates)", out)


if __name__ == "__main__":
    unittest.main()
