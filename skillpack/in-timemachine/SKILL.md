---
name: in-timemachine
description: >-
  Time Machine for the Indian Day Trader Skill Pack (NSE and BSE index options
  through Angel One SmartAPI). An on-demand check of the pack's own rule, not
  part of the daily flow: it replays the opening-range breakout of in-entry
  and the stop, 2R target and flat-by of in-exit on the last 60 trading days
  of NIFTY, SENSEX or BANKNIFTY 5-minute candles (one getCandleData call,
  cached), takes one lot's charges off every trade in R, then resamples the
  day outcomes 10,000 times with seed 19 for the spread: win rate, expectancy
  in R, worst drawdown and longest losing streak. A replay of the past, never
  a forecast and never a live signal; it creates no virtual account and never
  places an order. Use when the user says "run the time machine", "backtest
  the ORB rule", "replay the last 60 days", "how has this rule done on
  NIFTY", "what is the win rate", "expectancy in R", "worst drawdown",
  "losing streak", "validate the strategy", "monte carlo the rule", or
  "show the last replay".
argument-hint: "run [--symbol NIFTY] [--days 60] [--stop or-mid|or-opposite|pct:X|atr:K] [--write] | show"
---

# in-timemachine

The pack's research tool (Phase 5). Before trusting the rule with money, ask
how it did: the same opening-range breakout, stop, target and flat-by that
in-entry and in-exit apply today, run on every one of the last 60 trading
days of the index, with charges taken off, and then resampled 10,000 times
so one lucky or unlucky streak does not decide the answer. It replays the
past. The past does not promise the future, and a backtest on the underlying
is not an option trade.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md
lives). Use `python3` instead of `python` on macOS and Linux.

```bash
python "${CLAUDE_SKILL_DIR}/timemachine.py" run                      # plan.json's symbol (else NIFTY), last 60 trading days
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol SENSEX --days 60 --write
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --stop pct:0.4       # or-mid (default), or-opposite, pct:X, atr:K
python "${CLAUDE_SKILL_DIR}/timemachine.py" show                     # the saved trader/timemachine.json, nothing fetched
```

Other flags: `--trader DIR` (default `./trader`), `--now "2026-11-24 18:00"`
(IST), `--offline DIR` (replay `DIR/getCandleData.json` and read
`DIR/master.json` instead of calling the API; an offline run keeps its own
candle cache under `trader/cache/offline/`, never the real one).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | `session.range_end`, `entry_start`, `entry_end`, `flat_by`, `expiry_day_last_entry`, `expiry_day_flat_by`; `exit.target_r`, `exit.max_premium_loss_pct`; `timemachine.max_gap_pct` (default 15, not in the template) |
| `trader/calendar.json` | which days are trading days, to count the window back |
| `trader/plan.json` | optional, from in-exit: the symbol, `underlying_stop_rule`, `target_r`, and for charges `entry_premium`, `premium_stop`, `r_value_inr`, `instrument.lot_size` and `segment` |
| `trader/charges.json` | the rates for one lot's round trip (`smart.option_charges`) |
| `trader/history/chain/<U>/<date>.jsonl` | in-chain's snapshots; premium is replayed only once 20 days exist |
| getCandleData | the index's FIVE_MINUTE candles, through the shared cache in `trader/cache/candles/` (closed days are cached forever, so a rerun calls nothing) |
| `trader/timemachine.json` | with `--write`: `created_by`, `as_of_ist`, `source`, `partial`, the window, the rule, charges, every day's outcome, the history and the bootstrap |

## How it works

1. **The window.** The last N trading days before today (default 60) by
   `calendar.json`. Today is left out: only closed days. One getCandleData
   call covers up to 100 calendar days of 5-minute bars, so 60 trading days
   is one call per symbol, and none once they are cached. A day that comes
   back empty (a holiday the calendar does not list) is skipped and named.
2. **One day.** The opening range is the high and low of the 09:15 to 09:30
   bars. The first 5-minute bar inside 09:30 to 11:30 to close above the
   high is a bullish entry at that close (a CE), below the low a bearish one
   (a PE). The stop rule is `--stop` when given, else plan.json's
   `underlying_stop_rule`, else `or-mid`; the rules are in-exit's: `or-mid`
   (the range midpoint), `or-opposite` (the other side of the range), `pct:X`
   (X % from the entry), `atr:K` (K times the mean of the last 14 true ranges
   of that day's bars up to the entry bar). The stop is rounded to 0.05 away
   from the entry, the target, `target_r` (plan.json's, else config's) times
   the risk, half up, as in-exit rounds them. Bar by bar after the entry, the
   stop is checked before the target, so a bar that touches both is a loss.
   Not out by `flat_by` (15:10): out at the close of the bar that ends then.
3. **Expiry days.** On a day the master lists as an expiry of the index, the
   last entry is `expiry_day_last_entry` and the flat-by is
   `expiry_day_flat_by`. Today's master lists live contracts only, so days
   before its first expiry are not known as expiry days; the report says
   which days used the normal times.
4. **R.** +`target_r` at the target, -1 at the stop, else the move to the
   flat-by close over the risk, all on the underlying. A gap through the stop
   still counts -1 R here; a real fill can be worse.
5. **Charges.** With plan.json: one lot bought at its entry premium and sold
   at its premium stop, priced by `charges.json`, divided by `r_value_inr`,
   and taken off every trade. Without it the report says charges were not
   applied.
6. **The spread.** 10,000 samples, each as many trades as the history,
   drawn with replacement by `random.Random(19)`: the median and the 5th to
   95th percentile of the win rate, expectancy, worst drawdown (peak to
   trough of the running total, in R) and longest run of losses. Same input,
   same output, on any machine.
7. **Options.** Premium is replayed only from in-chain's own snapshots, once
   20 days of them exist: the ATM CE or PE at the entry, its premium stop
   `exit.max_premium_loss_pct` below, priced snapshot by snapshot until the
   trade's exit time. Before that it says "option premium was not replayed:
   underlying only". It never models a premium it did not record.
8. **Corporate actions.** An overnight gap over `timemachine.max_gap_pct`
   (default 15 %) inside the window looks like a split or bonus: the symbol
   is skipped and the run says so. An index normally has none.
9. **Partial.** If a fetch fails, the days that did arrive are used, the run
   is labelled PARTIAL with the count, and the failure is quoted. Fewer than
   20 days with bars, or a rule that never fired, gives no result.

## Steps

1. Run `run --write` for the symbol in question (it defaults to plan.json's).
2. Read back, in this order: the window and how many days had bars, the
   charges line, the options line, the history (trades, outcomes, win rate,
   expectancy after charges, worst drawdown, losing streak), then the
   resampled spread. Quote the numbers as printed.
3. Say plainly where the 5th to 95th percentile of the expectancy crosses
   zero: the sample cannot tell an edge from no edge. Do not round a coin
   flip up into an edge.
4. Say once that this is a replay of the past on the underlying, not a
   forecast and not an option result, and that it makes no live signal.
5. `show` prints the saved run again without fetching anything.

## Example: sixty days of NIFTY

`run --offline DIR --now "2026-11-24 18:00" --write`, where `DIR` holds the
synthetic fixture (`python tests/fixtures/in-timemachine/candles.py DIR`
writes it: a seeded random walk shaped like the documented getCandleData
reply, not real prices), `trader/plan.json` is the CONTRACTS.md example, and
config.json and charges.json are the in-setup templates.

```text
Time Machine, NIFTY: a replay of the past, not a forecast (Tue 2026-11-24 18:00 IST)
  Not financial advice: rule matches and arithmetic, never a prediction.
  Data          NSE index token 99926000: getCandleData FIVE_MINUTE, through the candle cache (offline replay); newest bar 2026-11-23 15:25, today left out
  Window        2026-09-01 to 2026-11-23: 60 trading days by the calendar, 59 with bars; no bars on 2026-10-02 (a holiday calendar.json does not list?)
  Gaps          largest overnight gap 0.57 % (2026-09-04), under timemachine.max_gap_pct 15
  Rule          opening range 09:15 to 09:30; the first 5-minute close beyond it, 09:30 to 11:30
                stop at the range midpoint (plan.json), target 2R, flat by 15:10; a bar that touches both counts as the stop
  Expiry days   2026-11-17 from the master: last entry 11:00, flat by 14:45; days before 2026-11-17 are not in today's master and used flat by 15:10
  Charges       Rs 83.93 for one lot bought and sold (NIFTY01DEC2623500CE, 65 qty, 112.40 in, 78.70 stop) = 0.0383 R of Rs 2,190.50, off every trade; charges.json not verified
  Options       option premium was not replayed: underlying only (0 days of chain snapshots, 20 needed)
History, 59 days: 49 trades (26 bullish, 23 bearish), 10 no trigger
  outcomes        10 target, 27 stop, 12 flat-by
  win rate        36.7 %
  expectancy      -0.095 R a trade, after charges
  worst drawdown  8.12 R
  losing streak   7 trades
Resampled 10,000 times (49 trades drawn with replacement, seed 19): median, 5th to 95th percentile
  win rate        36.7 %    (26.5 % to 49.0 %)
  expectancy      -0.097 R  (-0.374 R to +0.203 R)
  worst drawdown  11.12 R   (5.19 R to 21.36 R)
  losing streak   7         (4 to 12)
A replay of the past, not a forecast: it never makes a live signal and never creates a virtual account. Same input, same output (seed 19).
Wrote trader/timemachine.json
API usage: none (offline replay)
```

On this made-up data the rule lost a little after charges, and the 5th to
95th percentile of the expectancy runs from -0.363 R to +0.214 R: sixty days
cannot tell this rule from a coin flip with costs. That is the honest answer
to give. A live run says `getCandleData FIVE_MINUTE, through the candle
cache` and ends with the day's API usage against the design caps.

## Example: a likely corporate action

The same fixture with every price from 2026-10-15 on halved, as a 1:1 bonus
would do to a stock. The run stops before replaying anything (exit code 1):

```text
in-timemachine: NIFTY skipped: an overnight gap of 50.10 % into 2026-10-15 is over timemachine.max_gap_pct 15, likely a corporate action; no replay
```

## Rules

- The rule is the pack's rule: the same opening range, entry window, stop
  rule, target R and flat-by as in-entry and in-exit, read from the same
  config and plan. A changed rule is a new run.
- Inside a bar the stop comes first. Only closed 5-minute bars are used,
  never today's.
- Every number comes from the run. Quote the spread, not only the median.
- Charges are always either applied and shown in rupees and R, or named as
  not applied.
- History goes through the one shared client, limiter and candle cache in
  `in-common/smart.py`; a live run refuses until the in-setup probe has
  confirmed getCandleData on this account.

## What it never does

It never places an order: there is no order code in the pack, and a test
fails the build if any file names an order endpoint. It never makes a live
signal, never writes entry.json, plan.json or orders.json, never creates a
virtual or paper account, never models an option premium it did not record,
and never calls a replay a forecast.

## Safety

- Not financial advice. A replay is arithmetic on past bars; the next 60
  days can look nothing like the last 60.
- Analysis only: the pack never places an order. The user places and closes
  every order by hand in the Angel One app.
- The R here is on the underlying. An option's premium also decays and
  moves with volatility, so an option trade can lose where the underlying
  replay won.
- Personal, local use only.
