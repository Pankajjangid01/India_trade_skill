---
name: in-ticket
description: >-
  Manual order ticket and trade journal for the Indian Day Trader Skill Pack
  (NSE and BSE index options, Angel One). It replaces a broker connection:
  for each ticket in-risk passed today it prints exactly what to type into
  the Angel One app by hand (segment NFO or BFO, trading symbol, expiry,
  strike, CE or PE, BUY, INTRADAY or CARRYFORWARD, the LIMIT price, the
  quantity of each order so a freeze split shows as several orders, the stop
  and target on the premium and on the underlying, the flat-by time, the risk
  result with its checks, and the closing-auction warnings). It sends nothing
  and calls no API: it never places an order. Then it records what you
  actually got (fill, close, skip), computes charges and net P&L from
  charges.json, tags the trade FO_OPTIONS, and writes the journal and a
  yearly CSV for your accountant. Use when the user says "show the ticket",
  "what do I type in the app", "give me the order details", "I bought it at",
  "record my fill", "I got out at", "close the trade", "skip this one", "my
  journal", "today's P&L", "the CSV for my CA", or right after in-risk passes.
argument-hint: "show [ID] | fill ID --price P --time HH:MM [--qty N] | close ID --price P --time HH:MM --reason stop|target|flat_by|no_progress|manual | skip ID | journal"
---

# in-ticket

The last step of the pipeline, and the one that replaces the US pack's
broker skill. In India the pack has no order code at all: this skill prints
a ticket, the user types it into the Angel One app with their own hands, and
then tells this skill what they got. The journal it keeps is what in-risk
reads for the daily loss, trades per day and losses in a row, and what
in-alerts watches.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md
lives). Use `python3` instead of `python` on macOS and Linux.

```bash
python "${CLAUDE_SKILL_DIR}/ticket.py" show                     # today's tickets, READY or NOT READY
python "${CLAUDE_SKILL_DIR}/ticket.py" show T20261124-01
python "${CLAUDE_SKILL_DIR}/ticket.py" fill T20261124-01 --price 112.40 --time 09:41
python "${CLAUDE_SKILL_DIR}/ticket.py" fill T20261124-01 --price 112.40 --time 09:41 --qty 65    # part filled
python "${CLAUDE_SKILL_DIR}/ticket.py" close T20261124-01 --price 150.00 --time 10:55 --reason target
python "${CLAUDE_SKILL_DIR}/ticket.py" skip T20261124-02
python "${CLAUDE_SKILL_DIR}/ticket.py" journal                  # today, the year so far, the CSV
```

Other flags: `--trader DIR` (default `./trader`), `--now "2026-11-24 09:42"`
(IST). There is no `--offline`: nothing here calls the API.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the tickets from in-budget, checked by in-risk. This skill sets `status` FILLED (with `fill`), CLOSED (with `exit`) or SKIPPED |
| `trader/plan.json` | in-exit's flat-by time, when the plan is for the same contract and day |
| `trader/config.json` | `session.flat_by` and `session.expiry_day_flat_by` when no plan matches |
| `trader/charges.json` | brokerage, STT, exchange, SEBI, stamp duty and GST rates (in-setup), for `close` |
| `trader/state/journal.json` | every trade: fill and exit price and time, quantity, the executed orders, charges, gross and net P&L, segment class, exit reason. Rewritten atomically |
| `trader/journal_<year>.csv` | closed trades of that year, regenerated from the journal on every change |

## The ticket

A ticket is printed only for an order dated today, status DRAFT, side BUY
on a CE or PE, whose `order_quantities` split its quantity into whole lots
under the freeze quantity, and whose in-risk verdict is PASS. Anything else
is NOT READY with every reason, and its price and quantity are not shown, so
there is nothing to copy. A BLOCK is final: there is no override flag.

The quantity is listed per order. NIFTY's master freeze quantity of 1801
allows 27 lots of 65 in one order, so 30 lots is two orders, 1,755 and 195,
each typed separately. The flat-by time comes from the order, else the
matching plan.json, else config, and the ticket says which.

Every ticket ends with the closing-auction lines from the SmartAPI
documentation's Closing Auction Session section (from 03 Aug 2026): order
entry through SmartAPI is rejected 15:15 to 15:20, stop-loss orders in
CAS-eligible stocks are disallowed 15:15 to 15:40, and Angel One squares off
equity F&O intraday orders at 15:20.

## The journal and the CSV

- `fill` records the price, time and quantity you got (default: the whole
  ticket; `--qty` must be whole lots, no more than the ticket). The price
  must sit on the contract's tick. The trade is open from then on.
- `close` records the exit (any FILLED ticket: an exit is never blocked) and
  computes, with `smart.option_charges` and `trader/charges.json`, a BUY at
  the fill and a SELL at the exit, with one brokerage per executed order:
  a fill split under the freeze quantity is several orders each way.
  Gross P&L is (exit minus fill) times quantity; net P&L is gross minus
  charges, each to the paisa.
- Every trade is tagged `segment_class` FO_OPTIONS (FO_FUTURES and
  EQ_INTRADAY are reserved names for later versions).
- The CSV has one row per closed trade: ticket, segment class, exchange,
  symbol, underlying, CE or PE, strike, expiry, product, quantity, executed
  orders, buy date, time, price and value, sell date, time, price and value,
  gross P&L, brokerage, exchange charges, SEBI fee, STT, stamp duty, GST,
  total charges, net P&L, exit reason and the April-to-March financial year.
  It is a record for your accountant. No tax advice.

## Steps

1. After in-risk, run `show`. Read each READY ticket to the user as it is
   printed. A NOT READY ticket goes back to in-budget or in-risk with its
   reasons; never read its numbers out as something to type.
2. The user types the order into the Angel One app themselves. Never say it
   was placed; only the user knows.
3. Ask what they got from the app's trade book (price, time, quantity) and
   run `fill`. If they did not place it, run `skip`.
4. in-alerts watches the open trade. When the user is out, ask for the exit
   price and time and the reason, and run `close`. Read back the charges and
   the net P&L.
5. `journal` at the end of the day: the day's trades, the year so far, and
   the CSV path.

## Example: the morning tickets

`show --now "2026-11-24 09:42"` on `tests/fixtures/in-ticket` (synthetic
tickets shaped like CONTRACTS.md): one PASS, one BLOCK, and one PASS split
by the freeze quantity.

```text
Tickets to type by hand, Tue 2026-11-24 09:42 IST (source trader/orders.json, from in-budget and in-risk)
  Not financial advice: rule matches and arithmetic, never a prediction.
  This tool sends nothing: you type each order into the Angel One app yourself.

TICKET T20261124-01  READY (in-risk PASS at 09:41)
  Segment       NFO
  Symbol        NIFTY01DEC2623500CE
  Contract      NIFTY 23500 CE, expiry Tue 2026-12-01
  Side          BUY
  Product       INTRADAY
  Price         LIMIT 112.40
  Quantity      130 (2 lots of 65) in 1 order
                order 1 of 1: 130 (2 lots)
  Stop          premium at or below 78.70; NIFTY at or below 23,515.00
  Target        premium at or above 179.80; NIFTY at or above 23,657.50
  Flat by       15:05 IST (plan.json)
  At the stop   about 4,445.44 lost (premium 4,381.00 + charges 64.44; charges.json unverified)
  Risk          in-risk PASS
                ok  max_loss_per_trade: 4,445.44 of 5,000.00
                ok  daily_loss: 0.00 lost today of 10,000.00
                ok  trades_per_day: 0 of 3
                ok  stale_data: feed lag 300 ms, age 1.0 s

TICKET T20261124-02  NOT READY: do not type it
  Contract      SENSEX26D0377200CE (SENSEX 77200 CE, expiry Thu 2026-12-03)
  Why           in-risk BLOCK at 09:41
                spread: 4.2% is over max_spread_pct 3.0
                liquidity_grade: C is below B

TICKET T20261124-03  READY (in-risk PASS at 09:41)
  Segment       NFO
  Symbol        NIFTY01DEC2623400CE
  Contract      NIFTY 23400 CE, expiry Tue 2026-12-01
  Side          BUY
  Product       INTRADAY
  Price         LIMIT 168.50
  Quantity      1,950 (30 lots of 65) in 2 orders, freeze quantity 1801
                order 1 of 2: 1,755 (27 lots)
                order 2 of 2: 195 (3 lots)
  Stop          premium at or below 117.95; NIFTY at or below 23,515.00
  Target        premium at or above 269.60; NIFTY at or above 23,657.50
  Flat by       15:10 IST (config session.flat_by)
  At the stop   about 98,959.78 lost (premium 98,572.50 + charges 387.28; charges.json unverified)
  Risk          in-risk PASS
                ok  max_loss_per_trade: 98,959.78 of 100,000.00
                ok  lot_cost: 328,575.00 of 1,500,000.00

Near the close (SmartAPI documentation, Closing Auction Session, from 03 Aug 2026):
  15:15 to 15:20  order entry through SmartAPI is rejected
  15:15 to 15:40  stop-loss orders in CAS-eligible stocks are disallowed
  15:20           Angel One auto square-off of equity F&O intraday orders
Then record what you got: ticket.py fill ID --price P --time HH:MM, or ticket.py skip ID.
```

## Example: a fill and its exit

The user bought the first ticket at 112.40 at 09:41 and sold at 150.00 at
10:55 on the target: `fill T20261124-01 --price 112.40 --time 09:41 --now
"2026-11-24 09:43"`, then `close T20261124-01 --price 150.00 --time 10:55
--reason target --now "2026-11-24 10:56"`. The charges are charges.json's
build-time rates: buy 20.00 + 5.12 exchange + 0.01 SEBI + 0.44 stamp + 4.52
GST = 30.09; sell 20.00 + 6.83 + 0.02 + 29.25 STT + 4.83 GST = 60.93.

```text
Recorded fill T20261124-01, Tue 2026-11-24 09:43 IST
  Not financial advice: rule matches and arithmetic, never a prediction.
  BUY 130 NIFTY01DEC2623500CE at 112.40, 09:41, in 1 order (the ticket said LIMIT 112.40)
  Status FILLED in trader/orders.json; open trade in trader/state/journal.json
  Watch it: in-alerts check (stop 78.70 / NIFTY 23,515.00, target 179.80 / NIFTY 23,657.50, flat by 15:05)
Close it yourself in the Angel One app, then: ticket.py close T20261124-01 --price P --time HH:MM --reason R
Recorded exit T20261124-01, Tue 2026-11-24 10:56 IST
  Not financial advice: rule matches and arithmetic, never a prediction.
  SELL 130 NIFTY01DEC2623500CE at 150.00, 10:55, reason target
  Bought      112.40 x 130 = 14,612.00 (09:41)
  Sold        150.00 x 130 = 19,500.00 (10:55)
  Gross P&L   4,888.00
  Charges     91.02 for 1 order each way (buy 30.09, sell 60.93), computed from charges.json (unverified)
              brokerage 40.00, exchange 11.95, SEBI 0.03, STT 29.25, stamp 0.44, GST 9.35
  Net P&L     4,796.98
Wrote trader/state/journal.json and trader/journal_2026.csv (for your accountant; no tax advice)
```

## Rules

- A ticket is a record of intent for the user to type, never an
  instruction to a broker. Print it exactly; do not round, merge or reorder
  the per-order quantities.
- Record only what the user reports from the app. Never guess a fill price
  or time, and never mark a ticket FILLED because it looked likely.
- A fill on a ticket in-risk did not pass today is recorded (it happened and
  in-risk must count it) with a note saying it was not approved.
- Charges are computed from `trader/charges.json` and say "unverified" until
  the user verifies that file. The broker's contract note is the final word.

## What it never does

It never places, modifies or cancels an order, never calls the SmartAPI or
any other API, never opens a connection to the broker, and never sends a
ticket anywhere: the code has no order function and a test fails the build
if any file names an order endpoint. It never gives tax advice, never
overrides a BLOCK, and never prints a NOT READY ticket's price or quantity.

## Safety

- Not financial advice. A ticket is the arithmetic of your own rules, not a
  recommendation to buy.
- Analysis only: the pack never places an order. You type and close every
  order yourself in the Angel One app.
- Version 1 buys CE and PE only: no short selling, no option selling, no
  futures.
- Personal, local use only. Nothing is shared.
