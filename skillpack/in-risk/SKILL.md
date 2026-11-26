---
name: in-risk
description: >-
  Risk Manager and the gate of the Indian Day Trader Skill Pack (NSE and BSE
  index options through Angel One SmartAPI). Checks every DRAFT ticket that
  in-budget wrote to trader/orders.json and gives PASS or BLOCK: max loss per
  trade, the daily loss limit from the journal, trades per day, losses in a
  row, the entry window and trading day, the expiry-day cutoff, the F&O ban
  list, stale feed data, RBI, Budget and election days, lot cost against
  capital, spread, OI and volume, and stock options carried into expiry
  (physically settled). Every check prints its numbers and a BLOCK lists every
  failing reason together. A BLOCK is final; there is no override flag. An
  exit is never blocked. It never places an order. Use when the user says
  "check my ticket", "risk check", "can I take this trade", "is this within my
  limits", "how much have I lost today", "am I over my daily loss", "why was
  it blocked", or after /in-budget, always.
argument-hint: "[--ticket ID] [--now \"YYYY-MM-DD HH:MM\"] [--offline DIR]"
---

# in-risk

The Risk Manager stands between a sized ticket and the user's hands. Every
DRAFT ticket from in-budget passes through it, and in-ticket prints a ticket
only for a PASS. A BLOCK is final for that ticket: it is never checked
again, and nothing in the pack can turn it into a PASS. To trade after a
BLOCK the user changes `trader/config.json` by hand, if they choose to, and
sizes a new ticket. Most people who day trade options lose money; these
limits keep each loss and each day's losses small, they do not make a trade
win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md
lives). Use `python3` instead of `python` on macOS and Linux.

```bash
python "${CLAUDE_SKILL_DIR}/check.py"                            # today's DRAFT tickets not already blocked
python "${CLAUDE_SKILL_DIR}/check.py" --ticket T20261124-01      # one ticket
python "${CLAUDE_SKILL_DIR}/check.py" --now "2026-11-24 09:42"   # pin the clock (IST)
```

`--trader DIR` (default `./trader`) points at the working folder. No REST
call is made: live prices come from the feed's SQLite file. `--offline DIR`
reads `DIR/master.json` as the instrument master, as in-chain and in-entry do
(tests and replays).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the DRAFT tickets (in-budget); it writes each checked ticket's `risk_result` |
| `trader/config.json` | `account.*` limits, `session.*` times, `options.*` liquidity, `freshness.*`, and `risk.block_event_kinds` |
| `trader/state/journal.json` | today's filled and closed trades (in-ticket): realised loss, trades, losses in a row |
| `trader/pick.json` | in-chain's pick: spread, OI and volume, and its age when the feed has nothing |
| `trader/state/ticks.sqlite` | the live feed (`smart.read_latest`): tick age and feed lag for the option and its underlying |
| `trader/cache/instrument_master_<date>.json` | today's master: index or stock option, lot size, the underlying's token (`smart.load_master` downloads it once a day if no other skill has; `DIR/master.json` with `--offline`) |
| `trader/calendar.json`, `trader/events.json`, `trader/ban.txt` | trading days and holidays, the user's event list, today's F&O ban list |

## The checks

Every check runs on every ticket; a BLOCK lists all that failed.

| check | blocks when |
| --- | --- |
| `ticket` | lots x lot size, quantity, the order split or the risk at the stop disagree (edited after sizing) |
| `instrument` | the token is not in today's master, or the master's lot size changed |
| `max_loss_per_trade` | `risk_inr.total` (premium at the stop plus charges) over capital x `risk_per_trade_pct` |
| `daily_loss_limit` | today's realised net loss from the journal plus this trade's risk over capital x `max_daily_loss_pct` |
| `trades_per_day` | `max_trades_per_day` trades already filled today |
| `losses_in_a_row` | today's last `max_consecutive_losses` closed trades were all losses |
| `trading_window` | not a trading day (weekend or a `calendar.json` holiday), or outside `session.entry_start` to `session.entry_end` |
| `expiry_day_cutoff` | on the option's expiry day, after `session.expiry_day_last_entry` |
| `fno_ban` | a stock in `ban.txt`, or no `ban.txt` for a stock; an index is never banned |
| `stale_data` | the option's or the underlying's last tick older than `freshness.stale_tick_sec`; with no feed data, a pick older than `freshness.chain_ttl_sec` |
| `signal_valid` | the signal must still stand: BLOCK when the underlying's live price is already through the plan's underlying stop, or when the plan's no-progress deadline (the trigger bar's close plus `no_progress_bars` five-minute bars) has passed without a move beyond the entry. Found on the first live day: a 09:45 bearish trigger had reversed through its stop by 11:29 |
| `event_day` | an `events.json` entry today whose kind is in `risk.block_event_kinds` (default `["rbi", "budget", "election"]`) |
| `lot_cost` | the premium x quantity over capital x `max_capital_per_trade_pct` |
| `spread_liquidity` | the pick's spread over `options.max_spread_pct`, OI under `options.min_oi`, volume under `options.min_volume`, or no pick for this option |
| `stock_expiry` | a stock option (OPTSTK) with product CARRYFORWARD whose expiry comes before the next trading day: in-the-money stock options settle in shares |

There is no pattern-day-trader rule in India and none here. A ticket whose
side is not BUY is an exit, and an exit is never blocked.

## Steps

1. Make sure the feed is running (`in-common/feed.py run`): without fresh
   ticks the stale-data check falls back to the pick's age, usually too old.
2. Run `check.py` right before the user would place the order. Exit code 0
   means every checked ticket passed, 1 means a BLOCK (or nothing to check).
3. Tell the user the result in plain words: the risk against each limit,
   and for a BLOCK every reason. Never change a limit yourself and never
   edit a ticket to make it pass.
4. A PASS can be checked again later and can turn into a BLOCK (the window
   closes, the data goes stale). A BLOCK stays a BLOCK.
5. Hand a PASS over to `/in-ticket`.

## Example: PASS

The in-budget example's ticket at 09:42 on Tue 2026-11-24, with fresh feed
ticks, an empty journal and the test fixtures (synthetic).

```text
Risk check, Tue 2026-11-24 09:42 IST, computed from orders.json, config.json, journal.json, pick.json, the feed and today's master
  Not financial advice: rule matches and arithmetic, never a prediction.
PASS  T20261124-01  BUY NIFTY01DEC2623500CE (NFO), 2 lots = 130 units at 112.40, stop 78.70
      ok   ticket              2 lots x 65 = 130 units in [130]; 112.40 - 78.70 stop = 33.70 a unit, 4,381.00 at the stop
      ok   instrument          NIFTY01DEC2623500CE OPTIDX in today's master, lot 65
      ok   max_loss_per_trade  4,454.28 of 5,000.00 (1% of capital 500,000.00)
      ok   daily_loss_limit    realised loss 0.00 + this trade 4,454.28 = 4,454.28 of 10,000.00 (2% of capital)
      ok   trades_per_day      0 filled today, limit 3
      ok   losses_in_a_row     0 losses in a row today (0 closed), stop at 3
      ok   trading_window      09:42 inside the entry window 09:30 to 11:30 IST, a trading day
      ok   expiry_day_cutoff   not the expiry day (expires 2026-12-01)
      ok   fno_ban             NIFTY is an index: never in the F&O ban
      ok   stale_data          NIFTY01DEC2623500CE 0.5 s old, NIFTY 0.4 s old (limit 3 s); feed lag 250 ms
      ok   signal_valid        NIFTY 23,562.50, the stop 23,515.00 not reached
      ok   event_day           no event of kind rbi, budget, election today
      ok   lot_cost            2 lots x 7,306.00 = 14,612.00 of 150,000.00 (30% of capital)
      ok   spread_liquidity    spread 0.09% (max 3%), OI 4,512,300 (min 5,000), volume 123,456 (min 1,000)
      ok   stock_expiry        not a stock option: index options settle in cash
1 passed, 0 blocked. Wrote risk_result to trader/orders.json.
A PASS is a rules check, not a recommendation. A BLOCK is final for that ticket.
```

## Example: BLOCK

The same size as ticket T20261124-04 at 11:40, after three losing trades of
Rs 2,000 each today. Four rules fail and all four are listed.

```text
Risk check, Tue 2026-11-24 11:40 IST, computed from orders.json, config.json, journal.json, pick.json, the feed and today's master
  Not financial advice: rule matches and arithmetic, never a prediction.
BLOCK T20261124-04  BUY NIFTY01DEC2623500CE (NFO), 2 lots = 130 units at 112.40, stop 78.70
      ok   ticket              2 lots x 65 = 130 units in [130]; 112.40 - 78.70 stop = 33.70 a unit, 4,381.00 at the stop
      ok   instrument          NIFTY01DEC2623500CE OPTIDX in today's master, lot 65
      ok   max_loss_per_trade  4,454.28 of 5,000.00 (1% of capital 500,000.00)
      FAIL daily_loss_limit    realised loss 6,000.00 + this trade 4,454.28 = 10,454.28 of 10,000.00 (2% of capital)
      FAIL trades_per_day      3 filled today, limit 3
      FAIL losses_in_a_row     3 losses in a row today (3 closed), stop at 3
      FAIL trading_window      11:40 outside the entry window 09:30 to 11:30 IST, a trading day
      ok   expiry_day_cutoff   not the expiry day (expires 2026-12-01)
      ok   fno_ban             NIFTY is an index: never in the F&O ban
      ok   stale_data          NIFTY01DEC2623500CE 0.5 s old, NIFTY 0.4 s old (limit 3 s); feed lag 250 ms
      ok   signal_valid        NIFTY 23,562.50, the stop 23,515.00 not reached
      ok   event_day           no event of kind rbi, budget, election today
      ok   lot_cost            2 lots x 7,306.00 = 14,612.00 of 150,000.00 (30% of capital)
      ok   spread_liquidity    spread 0.09% (max 3%), OI 4,512,300 (min 5,000), volume 123,456 (min 1,000)
      ok   stock_expiry        not a stock option: index options settle in cash
      Blocked by 4: daily_loss_limit, trades_per_day, losses_in_a_row, trading_window
0 passed, 1 blocked. Wrote risk_result to trader/orders.json.
A PASS is a rules check, not a recommendation. A BLOCK is final for that ticket.
```

Running it again says there is no DRAFT ticket left to check (1 blocked,
final). Raising a limit in `config.json` does not revive T20261124-04.

## Rules

- A PASS only means the ticket may be printed for the user. It is not a yes.
- Every number comes from the ticket, the config, the journal, the feed, the
  pick or the master; a missing input is a BLOCK, never a guess (except a
  missing `events.json`, which the output names).
- The daily loss counts realised net P&L only: an open trade's risk is not
  added. The losses-in-a-row count starts again each trading day.
- Index options are cash-settled and never in the F&O ban. Stock options are
  physically settled: never carried into expiry.

## What it never does

It never places an order, and never places, modifies or cancels one in any
form. It has no override flag, never loosens a limit, never edits a ticket
to make it pass, never turns a BLOCK into a PASS, and never blocks an exit.

## Safety

- Not financial advice. A PASS is a rules check, not a recommendation.
- Analysis only: the pack never places an order. The user places and closes
  every order by hand in the Angel One app.
- A stop is a plan, not a promise: in a fast market the premium can fall
  through it and the loss can be larger than the risk checked here.
- Personal, local use only.
