"""in-chain tests, all offline.

The fixtures in tests/fixtures/in-chain are synthetic, shaped like Angel One's
documented replies: a master slice per underlying (NIFTY with an expired
weekly, the 01DEC2026 chain and the next expiry; SENSEX with the 26NOV2026
chain), FULL quote replies in call order (SENSEX: the index first, then two
option batches), optionGreek and putCallRatio for NIFTY, and a 10:05
snapshot line for the OI change. Premiums and Greeks come from Black-Scholes
on a made-up smile, so they agree with each other; no number is real. The
live feed is a TickStore built in the temp folder.
"""

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

chain = load("in-chain", "chain")
smart = chain.smart
NIFTY = os.path.join(FIX, "in-chain", "nifty")
SENSEX = os.path.join(FIX, "in-chain", "sensex")
NOW = "2026-11-25 10:20"   # a Wednesday: NIFTY's nearest expiry is Tue 1 Dec, SENSEX's Thu 26 Nov
DAY = "2026-11-25"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = chain.main(argv)
    return code, out.getvalue(), err.getvalue()


def skill_example(heading):
    """The ```text block under `heading` in in-chain/SKILL.md."""
    with open(os.path.join(ROOT, "in-chain", "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    return re.search(re.escape(heading) + r".*?```text\n(.*?)```", text, re.S).group(1)


def at(hms):
    return int(smart.parse_ist(DAY + " " + hms).timestamp() * 1000)


class InTemp(unittest.TestCase):
    """A trader/ folder with a filled config, the live feed's tick store, a 10:05 snapshot in
    today's history and a bullish NIFTY trigger in entry.json."""

    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        cfg = smart.read_json(os.path.join(ROOT, "in-setup", "config.example.json"))
        cfg["account"]["capital_inr"] = 500000
        cfg["api"]["risk_free_rate_pct"] = 6.5
        cfg["options"]["strikes_each_side"] = 3      # a short chain for the examples
        cfg["limits"]["quote_tokens_per_call"] = 10  # 14 tokens: two quote calls
        self.config(cfg)
        s = smart.TickStore(os.path.join("trader", "state", "ticks.sqlite"))
        s.add({"token": "99926000", "exchange_type": 1, "exch_ms": at("10:19:58.800"), "ltp": 23512.35},
              at("10:19:59.050"))
        s.add({"token": "45004", "exchange_type": 2, "exch_ms": at("10:19:59.600"), "ltp": 191.15, "volume": 1204500,
               "oi": 3207500}, at("10:19:59.800"))      # fresher than its quote: overlaid
        s.add({"token": "45009", "exchange_type": 2, "exch_ms": at("10:19:50"), "ltp": 199.0, "volume": 1, "oi": 1},
              at("10:19:50.300"))                     # 10 s old: ignored
        s.close()
        hist = os.path.join("trader", "history", "chain", "NIFTY")
        os.makedirs(hist)
        shutil.copy(os.path.join(NIFTY, "history-1005.jsonl"), os.path.join(hist, DAY + ".jsonl"))
        smart.write_json(os.path.join("trader", "entry.json"), {
            "created_by": "in-entry", "symbol": "NIFTY", "date": DAY, "status": "triggered", "direction": "bullish",
            "trigger_level": 23500.0, "bar_start_ist": "10:10", "bar_close_ist": "10:15", "source": "feed",
            "as_of_ist": DAY + "T10:15:02+05:30"})

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, cfg):
        smart.write_json(os.path.join("trader", "config.json"), cfg)
        self.cfg = cfg

    def offline_copy(self, src, **files):
        """A copy of a fixture folder with some replies replaced."""
        dst = os.path.join(self.tmp, "off")
        shutil.copytree(src, dst)
        for name, body in files.items():
            smart.write_json(os.path.join(dst, name + ".json"), body)
        return dst

    def chain_file(self, name="NIFTY"):
        return smart.read_json(os.path.join("trader", "chain", name + ".json"))


class Snapshot(InTemp):
    def test_the_nifty_example_in_skill_md(self):
        code, out, err = run(["snapshot", "NIFTY", "--offline", NIFTY, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: a NIFTY snapshot"))

    def test_the_chain_file_keeps_the_contract(self):
        run(["snapshot", "NIFTY", "--offline", NIFTY, "--now", NOW])
        c = self.chain_file()
        for key in ("created_by", "underlying", "expiry", "spot", "atm", "step", "as_of_ist", "age_sec", "source",
                    "partial", "greeks_source", "rows", "pcr_oi", "pcr_volume", "max_pain", "top_oi", "straddle",
                    "implied_move_pct", "iv_rank", "buildup"):
            self.assertIn(key, c)
        for key in ("strike", "type", "token", "tradingsymbol", "ltp", "bid", "ask", "spread_pct", "volume", "oi",
                    "oi_change", "iv", "delta", "gamma", "theta", "vega"):
            self.assertIn(key, c["rows"][0])
        self.assertEqual((c["expiry"], c["spot"], c["atm"], c["step"]), ("2026-12-01", 23512.35, 23500.0, 50.0))
        self.assertEqual((c["source"], c["greeks_source"], c["partial"], c["stale"]), ("feed + quote", "API", False, False))
        rows = {(r["strike"], r["type"]): r for r in c["rows"]}
        self.assertEqual(len(rows), 14)                                     # ATM +/- 3, CE and PE
        atm_ce = rows[(23500.0, "CE")]
        self.assertEqual((atm_ce["ltp"], atm_ce["volume"], atm_ce["oi"], atm_ce["bid"], atm_ce["ask"]),
                         (191.15, 1204500, 3207500, 191.05, 191.15))       # feed overlay; depth from the quote
        self.assertEqual(rows[(23600.0, "PE")]["oi"], 600000)               # its feed tick was stale: the quote stands
        self.assertTrue(rows[(23500.0, "PE")]["stale"])                     # exchFeedTime 9 s old
        self.assertEqual(atm_ce["delta"], 0.5392)                           # optionGreek, by strike and type
        self.assertEqual(c["pcr_api"], {"tradingsymbol": "NIFTY29DEC26FUT", "pcr": 1.04})
        self.assertEqual(rows[(23500.0, "CE")]["oi_change"], 3207500 - 3185000)
        self.assertEqual((c["buildup"], c["oi_change_since_ist"]), ("Long Built Up", "2026-11-25T10:05:00+05:30"))

    def test_the_sensex_example_computes_greeks(self):
        code, out, err = run(["snapshot", "SENSEX", "--offline", SENSEX, "--now", NOW])  # no optionGreek.json there
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: SENSEX, Greeks computed"))
        c = self.chain_file("SENSEX")
        self.assertEqual((c["greeks_source"], c["source"], c["pcr_api"]), ("COMPUTED", "quote", None))
        r = next(r for r in c["rows"] if r["strike"] == 77200.0 and r["type"] == "CE")
        years = smart.year_fraction(smart.parse_ist(NOW), smart.parse_ist("2026-11-26 00:00").date())
        iv = smart.implied_vol("CE", r["ltp"], c["spot"], 77200.0, years, 0.065)
        self.assertAlmostEqual(r["iv"], iv * 100, places=2)
        self.assertAlmostEqual(r["iv"], 13.0, delta=0.1)                    # the smile the fixture was made from
        self.assertEqual(r["delta"], round(smart.black_scholes("CE", c["spot"], 77200.0, years, 0.065, iv)["delta"], 4))

    def test_computed_greeks_refuse_without_the_rate(self):
        self.cfg["api"]["risk_free_rate_pct"] = None
        self.config(self.cfg)
        code, out, err = run(["snapshot", "SENSEX", "--offline", SENSEX, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertEqual(err, "in-chain: set api.risk_free_rate_pct in trader/config.json: computed Greeks need it\n")
        self.assertIsNone(self.chain_file("SENSEX"))

    def test_a_failed_batch_is_partial_and_says_so(self):
        quotes = smart.read_json(os.path.join(NIFTY, "quote.json"))
        bad = {"status": 400, "headers": {}, "body": {"status": False, "message": "Invalid Token", "errorcode": "AB1018"}}
        off = self.offline_copy(NIFTY, quote=[quotes[0], bad])
        code, out, err = run(["snapshot", "NIFTY", "--offline", off, "--now", NOW])
        self.assertEqual(code, 0, err)
        c = self.chain_file()
        self.assertTrue(c["partial"])
        self.assertEqual(len(c["rows"]), 10)
        self.assertEqual(c["partial_note"], "quote call 2 of 2: HTTP 400 AB1018 Invalid Token; 10 of 14 rows quoted")
        self.assertIn("  PARTIAL         quote call 2 of 2", out)

    def test_a_stale_spot_is_said_and_gets_no_pick(self):
        quotes = smart.read_json(os.path.join(SENSEX, "quote.json"))
        quotes[0]["body"]["data"]["fetched"][0]["exchFeedTime"] = "25-Nov-2026 10:19:30"
        off = self.offline_copy(SENSEX, quote=quotes)
        code, out, err = run(["snapshot", "SENSEX", "--offline", off, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertIn("age 30.0 s, STALE", out)
        self.assertTrue(self.chain_file("SENSEX")["stale"])
        code, out, err = run(["pick", "SENSEX", "--direction", "bullish", "--offline", off, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("in-chain: the SENSEX spot is stale (30.0 s old): no pick on stale data", err)
        self.assertFalse(os.path.exists(os.path.join("trader", "pick.json")))

    def test_every_snapshot_is_appended_to_the_history(self):
        path = os.path.join("trader", "history", "chain", "NIFTY", DAY + ".jsonl")
        run(["snapshot", "NIFTY", "--offline", NIFTY, "--now", NOW])
        code, out, err = run(["history", "NIFTY", "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: history"))
        run(["snapshot", "NIFTY", "--offline", NIFTY, "--now", "2026-11-25 10:20:01"])  # the feed spot still fresh
        lines = chain.read_lines(path)
        self.assertEqual([s["as_of_ist"][11:19] for s in lines], ["10:05:00", "10:20:00", "10:20:01"])
        self.assertEqual(lines[-1], self.chain_file())      # the latest file and the last line are one snapshot
        code, _, err = run(["history", "SENSEX", "--now", NOW])
        self.assertEqual((code, err), (1, "in-chain: no SENSEX chain history for 2026-11-25 "
                                          "(trader/history/chain/SENSEX/2026-11-25.jsonl)\n"))

    def test_the_feed_is_asked_for_the_chain(self):
        run(["snapshot", "NIFTY", "--offline", NIFTY, "--now", NOW])
        subs = smart.read_json(os.path.join("trader", "state", "subscriptions.json"))["in-chain:NIFTY"]
        self.assertEqual(subs[0], {"segment": "NSE", "token": "99926000", "mode": "quote"})
        self.assertEqual(len(subs), 15)
        self.assertEqual({s["mode"] for s in subs[1:]}, {"snapquote"})
        self.assertEqual({s["segment"] for s in subs[1:]}, {"NFO"})

    def test_closed_market_and_unconfirmed_live_quotes_refuse(self):
        code, _, err = run(["snapshot", "NIFTY", "--offline", NIFTY, "--now", "2026-11-28 10:20"])
        self.assertEqual((code, err), (1, "in-chain: no live chain (closed: weekend): market data calls run on "
                                          "trading days, 09:00 to 15:45 IST\n"))
        code, _, err = run(["snapshot", "NIFTY", "--now", NOW])   # live: refused before any network call
        self.assertEqual((code, err), (1, "in-chain: quote_options is not confirmed on this account yet: run "
                                          "in-setup probe in market hours first\n"))


class Analytics(InTemp):
    def rows(self):
        mk = lambda k, t, oi, vol: {"strike": k, "type": t, "oi": oi, "volume": vol}  # noqa: E731
        return [mk(100.0, "CE", 10, 5), mk(110.0, "CE", 30, 5), mk(120.0, "CE", 50, 10),
                mk(100.0, "PE", 40, 3), mk(110.0, "PE", 20, 3), mk(120.0, "PE", 5, None)]

    def test_max_pain_and_pcr_by_hand(self):
        # paid to holders at 100: PE110 20x10 + PE120 5x20 = 300; at 110: CE100 10x10 + PE120 5x10 = 150;
        # at 120: CE100 10x20 + CE110 30x10 = 500. Least at 110.
        self.assertEqual(chain.max_pain(self.rows()), 110.0)
        self.assertEqual(chain.pcr(self.rows(), "oi"), round(65 / 90, 2))
        self.assertEqual(chain.pcr(self.rows(), "volume"), 0.3)
        self.assertIsNone(chain.max_pain([dict(r, oi=0) for r in self.rows()]))

    def test_expiry_rolls_only_past_the_expiry_day_cutoff(self):
        rows = smart.read_json(os.path.join(NIFTY, "master.json"))

        def pick(t):
            now = smart.parse_ist(t)
            return chain.choose_expiry(chain.contracts(rows, "NIFTY", now.date(), ("OPTIDX",)), now, "11:00").isoformat()
        self.assertEqual(pick("2026-11-25 10:20"), "2026-12-01")       # 24 Nov has expired
        self.assertEqual(pick("2026-12-01 11:00"), "2026-12-01")       # expiry day, not yet past 11:00
        self.assertEqual(pick("2026-12-01 11:01"), "2026-12-08")
        self.assertEqual(pick("2026-12-02 09:20"), "2026-12-08")

    def test_iv_rank_waits_for_20_days_of_its_own_history(self):
        folder = os.path.join("trader", "history", "chain", "NIFTY")

        def day(i, iv):
            line = {"atm": 23500.0, "rows": [{"strike": 23500.0, "type": "CE", "iv": iv},
                                             {"strike": 23500.0, "type": "PE", "iv": iv}]}
            with open(os.path.join(folder, "2026-10-%02d.jsonl" % i), "w", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        for i in range(1, 20):
            day(i, 10.0 + i)                    # 11 to 29
        self.assertEqual(chain.iv_rank(folder, DAY, 14.0), (None, 19))
        day(20, 34.0)
        self.assertEqual(chain.iv_rank(folder, DAY, 14.0), (round((14.0 - 11.0) / (34.0 - 11.0) * 100, 1), 20))
        run(["snapshot", "NIFTY", "--offline", NIFTY, "--now", NOW])
        self.assertEqual(self.chain_file()["iv_rank"], 13.0)   # today's ATM IV is 14.0 in the fixture


class Pick(InTemp):
    def pick(self, *extra, src=NIFTY):
        return run(["pick", "NIFTY", "--offline", src, "--now", NOW] + list(extra))

    def test_the_example_in_skill_md(self):
        code, out, err = self.pick()
        self.assertEqual(code, 0, err)
        self.assertEqual(out, skill_example("## Example: the pick"))

    def test_pick_json_carries_the_instrument_from_the_master(self):
        self.pick()
        p = smart.read_json(os.path.join("trader", "pick.json"))
        self.assertEqual(p["instrument"], {
            "tradingsymbol": "NIFTY01DEC2623550CE", "token": "45006", "segment": "NFO", "underlying": "NIFTY",
            "strike": 23550.0, "type": "CE", "expiry": "2026-12-01", "expiry_raw": "01DEC2026", "lot_size": 65,
            "tick_size": 0.05, "freeze_qty": 1801})
        self.assertEqual((p["created_by"], p["direction"], p["premium"], p["bid"], p["ask"], p["greeks_source"]),
                         ("in-chain", "bullish", 164.35, 164.3, 164.4, "API"))
        self.assertEqual(p["one_lot_cost_inr"], 10682.75)                  # 65 x 164.35
        row = next(r for r in self.chain_file()["rows"] if r["token"] == "45006")
        self.assertEqual((p["spread_pct"], p["volume"], p["oi"]), (row["spread_pct"], row["volume"], row["oi"]))
        self.assertEqual(p["next_best"]["token"], "45004")
        self.assertTrue(p["rule"].startswith("CE, |delta| 0.35 to 0.55"))

    def test_a_stale_row_is_refused_even_when_closest(self):
        code, out, err = self.pick("--direction", "bearish")   # --direction wins over entry.json
        self.assertEqual(code, 0, err)
        p = smart.read_json(os.path.join("trader", "pick.json"))
        self.assertEqual((p["direction"], p["instrument"]["tradingsymbol"]), ("bearish", "NIFTY01DEC2623450PE"))
        self.assertIn("  Failed          stale (no update for over 3 s): 23500 (9.0 s)\n", out)  # delta 0.46, closest

    def test_nothing_passes_writes_nothing(self):
        self.cfg["account"]["capital_inr"] = 20000        # 30% is Rs 6,000: no lot fits
        self.config(self.cfg)
        code, out, err = self.pick()
        self.assertEqual(code, 1)
        self.assertEqual(err, "in-chain: no NIFTY CE passed the pick rules; nothing written\n")
        self.assertIn("one lot over Rs 6,000.00: 23350 (Rs 18,495.75)", out)
        self.assertFalse(os.path.exists(os.path.join("trader", "pick.json")))

    def test_refuses_without_a_direction_or_capital(self):
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.pick()
        self.assertEqual((code, err), (1, "in-chain: no direction for NIFTY: trader/entry.json has no trigger for it "
                                          "today; pass --direction bullish or bearish\n"))
        smart.write_json(os.path.join("trader", "entry.json"), {"symbol": "NIFTY", "date": "2026-11-24",
                                                                "status": "triggered", "direction": "bullish"})
        self.assertEqual(self.pick()[0], 1)                 # yesterday's trigger is not today's
        self.cfg["account"]["capital_inr"] = None
        self.config(self.cfg)
        code, _, err = self.pick("--direction", "bullish")
        self.assertEqual((code, err), (1, "in-chain: set account.capital_inr in trader/config.json: the pack refuses "
                                          "to size or check trades without it\n"))
        self.assertFalse(os.path.exists(os.path.join("trader", "pick.json")))


if __name__ == "__main__":
    unittest.main()
