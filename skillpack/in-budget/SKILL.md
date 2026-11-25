---
name: in-budget
description: >-
  Budget Manager for the Indian Day Trader Skill Pack (NSE and BSE index
  options through Angel One SmartAPI). Sizes an option buy in whole lots from
  the trade plan (in-exit's plan.json: entry premium, premium stop, target) so
  the loss at the stop plus the round-trip charges fits your max loss per
  trade (account.risk_per_trade_pct of capital), caps the capital used at
  account.max_capital_per_trade_pct and your free cash, splits the quantity by
  the exchange freeze quantity into one entry per order, and writes ONE DRAFT
  ticket to trader/orders.json for in-risk to check. Zero lots is a plain
  answer that shows what one lot would risk. It never places an order. Use
  when the user says "how many lots", "size this trade", "how much should I
  buy", "lot size for this option", "I have Rs 20,000 free", "what does one
  lot risk", "budget this plan", or after /in-exit writes a plan.
argument-hint: "[--write] [--cash RUPEES] [--now \"YYYY-MM-DD HH:MM\"]"
---

# in-budget

The Budget Manager turns a plan ("buy NIFTY01DEC2623500CE at 112.40, stop
78.70") into a size ("2 lots, 130 units, one order"), inside rules the user
sets once in `trader/config.json`. The stop decides the size: the further the
premium stop, the fewer lots. It writes a DRAFT ticket only, a record of
intent. in-risk checks it, in-ticket prints it, and the user types the order
into the Angel One app by hand. Most people who day trade options lose money;
sizing to a max loss keeps each loss small, it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md
lives). Use `python3` instead of `python` on macOS and Linux.

```bash
python "${CLAUDE_SKILL_DIR}/size.py"                          # a preview: nothing written
python "${CLAUDE_SKILL_DIR}/size.py" --write                  # append the DRAFT ticket to trader/orders.json
python "${CLAUDE_SKILL_DIR}/size.py" --cash 20000 --write     # also keep the capital used within Rs 20,000
python "${CLAUDE_SKILL_DIR}/size.py" --now "2026-11-24 09:41" # pin the clock (IST)
```

`--trader DIR` (default `./trader`) points at the working folder. No API call
is made: everything comes from the files below.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | in-exit's plan: the instrument (lot size, freeze quantity and segment from the master), entry premium, premium stop, targets. A plan for another day is refused |
| `trader/config.json` | `account.capital_inr` (it refuses while empty), `account.risk_per_trade_pct`, `account.max_capital_per_trade_pct` |
| `trader/charges.json` | the charge rates (brokerage per executed order, STT, exchange, SEBI, stamp duty, GST) and whether they are `verified` |
| `trader/orders.json` | the DRAFT ticket it appends with `--write`, numbered `T<yyyymmdd>-<nn>` (CONTRACTS.md), with plan.json's `entry_underlying` and `no_progress_bars` for in-alerts; each write stamps the file's `created_by`, `as_of_ist` and `source` |

## How the size is worked out

| step | rupees |
| --- | --- |
| max loss | capital x `risk_per_trade_pct` / 100 |
| lots by max loss | floor(max loss / ((entry premium - premium stop) x lot size)) |
| charges | `smart.option_charges` BUY at the entry and SELL at the stop, for each order of the split: every executed order pays its own brokerage, so a split costs more |
| fit | one lot fewer at a time until premium risk at the stop + round-trip charges fits the max loss |
| capital cap | premium x quantity within capital x `max_capital_per_trade_pct` / 100 |
| cash | with `--cash`, premium x quantity within that amount |
| split | `smart.max_lots_per_order(freeze_qty, lot)` lots an order, `smart.split_lots` gives one quantity per order you type |

The smallest of the limits decides, and the output says which. Prices are
the plan's, already on the instrument's tick. Money is rupees to the paisa.

## Steps

1. Make sure in-exit has written today's `trader/plan.json`.
2. Run `size.py` once to preview. Show the user the limits, the lots, the
   orders, the risk with charges and the capital used.
3. If the charges are unverified, say so: the real total can differ until
   in-setup's probe has compared `charges.json` with the broker.
4. On the user's go-ahead, run `size.py --write`. It appends one DRAFT
   ticket. Then hand over to `/in-risk`.
5. Zero lots: tell the user plainly, with what one lot would risk. Never raise
   a limit for them; they change `config.json` themselves if they choose to.

## Example: two lots

`size.py --write --now "2026-11-24 09:41"` on the test fixtures (synthetic):
capital Rs 5,00,000, risk 1% a trade, cap 30%, the template's unverified
charges, and a NIFTY 23500 CE plan at 112.40 with the premium stop at 78.70.

```text
Sizing NIFTY01DEC2623500CE (NFO), Tue 2026-11-24 09:41 IST, computed from plan.json (in-exit, as of 09:40 IST, 1 min old)
  Not financial advice: rule matches and arithmetic, never a prediction.
  Capital        Rs 500,000.00; max loss Rs 5,000.00 a trade (1%); capital cap Rs 150,000.00 a trade (30%)
  One lot        65 units: costs Rs 7,306.00 at 112.40, risks Rs 2,250.74 at the stop 78.70 (Rs 2,190.50 premium + Rs 60.24 charges)
  Lots           max loss 2, capital cap 20: 2 lots, decided by max loss
  Orders         130 units in 1 order [130]; freeze 1801 allows 27 lots an order
  Risk           Rs 4,381.00 at the stop + Rs 73.28 charges = Rs 4,454.28 of Rs 5,000.00
  Capital used   Rs 14,612.00
  Charges        UNVERIFIED: charges.json is not yet checked against the broker's calculator (in-setup probe); the real charges can differ
Wrote DRAFT ticket T20261124-01 to trader/orders.json. in-risk checks it next.
```

The ticket holds `lots` 2, `quantity` 130, `order_quantities` [130],
`risk_inr` {4381.0, 73.28, 4454.28}, `capital_used_inr` 14612.0,
`charges_verified` false, `risk_result` null and `status` DRAFT.

## Example: zero lots

The same plan with capital Rs 2,00,000: a 1% max loss is Rs 2,000.00 and one
lot risks more. Nothing is written, and the exit code is 1.

```text
Sizing NIFTY01DEC2623500CE (NFO), Tue 2026-11-24 09:41 IST, computed from plan.json (in-exit, as of 09:40 IST, 1 min old)
  Not financial advice: rule matches and arithmetic, never a prediction.
  Capital        Rs 200,000.00; max loss Rs 2,000.00 a trade (1%); capital cap Rs 60,000.00 a trade (30%)
  One lot        65 units: costs Rs 7,306.00 at 112.40, risks Rs 2,250.74 at the stop 78.70 (Rs 2,190.50 premium + Rs 60.24 charges)
  Lots           max loss 0, capital cap 8: 0 lots, decided by max loss
No ticket: one lot risks Rs 2,250.74, over your Rs 2,000.00 max loss a trade.
```

## Rules

- Whole lots only; quantity is lots x the master's lot size, never typed by hand.
- Charges are part of the risk. A size that fits only without charges is one lot smaller.
- Every number comes from the plan, the config or the charges file. Nothing is guessed.
- Buy-only CE and PE in version 1: the ticket's side is always BUY and its product INTRADAY.
- One `--write` appends one ticket. Re-sizing after a plan changes means a new ticket.

## What it never does

It never places, modifies or cancels an order, and never places an order on
the user's behalf in any form: it writes a DRAFT record of intent and nothing
else. It never raises a limit, never shrinks a stop to fit more lots, never
sizes an option sale, a future or a short, and never sizes a plan from
another day.

## Safety

- Not financial advice. A size is arithmetic on your own limits, not a
  recommendation to trade.
- Analysis only: the pack never places an order. The user places and closes
  every order by hand in the Angel One app.
- A stop is a plan, not a promise: in a fast market the premium can fall
  through it, and the real loss can be larger than the risk shown.
- Personal, local use only.
