---
name: in-exit
description: >-
  The exit plan for the Indian Day Trader Skill Pack, set BEFORE the buy. Reads
  today's trigger from in-entry (trader/entry.json) and the option chosen by
  in-chain pick (trader/pick.json), then sets two stops: one on the
  underlying index (the opening-range midpoint by default, or the other side
  of the range, K x ATR14, or a percent) and one on the option premium (30%
  under the entry premium by default), with targets at 2R on both, a flat-by
  time (15:10, or 14:45 on expiry day, ahead of Angel One's auto square-off)
  and a no-progress exit. Rounds premium levels to the instrument's tick from
  the master and index levels to 0.05. Refuses a stop on the wrong side, a
  zero risk, a stale or mismatched pick, or a plan at or after flat-by, and
  says why. Writes trader/plan.json for in-budget, in-risk, in-ticket and
  in-alerts. Analysis only: it never places an order. Use when the user says
  "where's my stop", "set my stop and target", "plan the exit", "when do I
  get out", "stop loss on the option", "what's my R", "risk per lot", or
  right after in-entry triggers and in-chain picks the strike.
argument-hint: "plan [--stop or-mid|or-opposite|atr:K|pct:X] [--entry-premium P] [--write] | show"
---

# in-exit

The exit plan decides how a trade ends before it starts. The stop is where
the setup has failed; the target is where you take what you came for; the
flat-by time ends it either way. All three are set BEFORE the buy, written
down and handed on, so nobody has to decide anything in the middle of a fast
move. A stop is a plan, not a guarantee: a gap or a fast market can fill past
it, and an option premium can jump. Most intraday option buyers lose money,
and a neat plan does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md
lives). Use `python3` instead of `python` on macOS and Linux.

```bash
python "${CLAUDE_SKILL_DIR}/exit.py" plan                        # or-mid stop, 30% premium stop, 2R, flat-by
python "${CLAUDE_SKILL_DIR}/exit.py" plan --write                # also save trader/plan.json
python "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5 --write # needs today's bars: the feed or getCandleData
python "${CLAUDE_SKILL_DIR}/exit.py" plan --entry-premium 115.20 # the premium you expect instead of the pick's
python "${CLAUDE_SKILL_DIR}/exit.py" show                        # the saved plan
```

Other flags: `--trader DIR` (default `./trader`), `--now "2026-11-25 09:42"`
(IST), `--offline DIR` (for `atr:K` only: replay `DIR/getCandleData.json`
and `DIR/master.json`).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | in-entry's record. It must be `triggered` with today's date: the entry price is the trigger bar's close, plus the opening range |
| `trader/pick.json` | in-chain pick: the instrument object (lot size, tick size, expiry from the master) and the premium. It must be for the same underlying and direction (a CE for bullish, a PE for bearish) and made at or after the trigger bar closed; otherwise it refuses and says to run in-chain pick again |
| `trader/config.json` | `exit.max_premium_loss_pct` (30), `exit.target_r` (2.0), `exit.no_progress_bars` (6), `session.flat_by` (15:10), `session.expiry_day_flat_by` (14:45) |
| live feed or `getCandleData` | only for `atr:K`: today's 5-minute bars of the index up to the entry bar (`smart.day_bars`) |
| `trader/plan.json` | with `--write` (CONTRACTS.md): both stops and their rules, both targets, risk a unit, `r_value_inr` (one lot), flat-by, expiry day, no-progress bars, tick size |

## Stops and targets

| `--stop` | the underlying stop |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default) |
| `or-opposite` | the other side of the range: the low for a bullish trade, the high for a bearish one |
| `atr:K` | K times the ATR14 of today's 5-minute bars up to the entry bar (the average of the last 14 true ranges; with fewer bars it averages all of them and says so) |
| `pct:X` | X percent from the entry |

- **Premium stop.** `exit.max_premium_loss_pct` (30) under the entry
  premium. The entry premium is the pick's, or `--entry-premium`.
- **Rounding.** A stop rounds AWAY from the entry: premium stops down to the
  instrument's tick (`smart.round_tick`), underlying stops to 0.05 (down for
  bullish, up for bearish). Targets round half up.
- **Targets.** `exit.target_r` (2.0) times the risk, on the underlying and on
  the premium separately. Out on whichever stop is hit first.
- **R.** `risk_per_unit_inr` = entry premium minus premium stop;
  `r_value_inr` = that times the lot size, for one lot. in-budget sizes the
  lots from it.
- **Time stop.** Flat by `session.flat_by` (15:10), win or lose; on expiry
  day (the option expires today) `session.expiry_day_flat_by` (14:45). Angel
  One's auto square-off from 3 Aug 2026 is 15:10 for equity MIS and 15:20 for
  equity F&O orders, so the flat-by stays no later than both.
- **No progress.** If the underlying has not closed beyond the entry in the
  trade's direction within `exit.no_progress_bars` (6) five-minute bars,
  get out: an option buyer's premium decays while it waits.
- **Refused, with the reason.** No trigger today. A missing pick, a pick
  for another underlying, direction or option type, or one made before the
  trigger. A stop on the wrong side of the entry. A zero risk (on the
  underlying or the premium). A plan at or after the flat-by time.

## Steps

1. Run it right after `in-entry` triggers and `in-chain pick` chooses the
   strike. If pick.json is missing or stale, run `in-chain pick` first.
2. Use the default stop unless the user names another. Run `exit.py plan
   --write`. Read back both stops and their rules, both targets, the rupee
   risk of one lot, the flat-by time and the no-progress time.
3. If it refuses, say the reason and the fix. Never loosen a stop on your own.
4. Say once that a stop is a plan, not a guarantee.
5. Hand off to `in-budget` to size it in lots, then `in-risk` to check it.
   A risk BLOCK stops a new entry, never an exit: once a trade is on, its
   stop, target and flat-by stand whatever in-risk says. Offer `in-alerts`
   for the stop, the target and the flat-by time.

## Example: the bullish NIFTY plan

Wednesday 2026-11-25. in-entry triggered at 09:40 (the 09:35 bar closed
23,562.50 above the range 23,480.00 to 23,550.00) and in-chain picked
NIFTY01DEC2623500CE at 112.40 at 09:41. `plan --write --now "2026-11-25 09:42"`:

```text
in-exit plan: NIFTY bullish, buy NIFTY01DEC2623500CE, Wed 2026-11-25 09:42 IST
  Not financial advice: rule matches and arithmetic, never a prediction.
  Source          entry.json (the 09:35 bar closed 23,562.50 at 09:40) + pick.json (premium at 09:41)
  Instrument      NIFTY01DEC2623500CE (NFO), expiry 2026-12-01, lot 65, tick 0.05
  Underlying      entry 23,562.50, stop 23,515.00, target 23,657.50 (2R)
                  stop: opening-range midpoint (23,550.00 + 23,480.00) / 2 = 23,515.00, rounded down to 0.05
  Premium         entry 112.40, stop 78.65, target 179.90 (2R)
                  stop: 30% under the entry premium = 78.68, rounded down to the 0.05 tick
  Risk            Rs 33.75 a unit, Rs 2,193.75 for one lot of 65 (1R)
  Exit            on whichever stop is hit first; a stop is a plan, not a guarantee
  Flat by         15:10 IST, win or lose (Angel One's auto square-off from 3 Aug 2026 is 15:10 equity MIS, 15:20 equity F&O)
  No progress     out if NIFTY has not closed above 23,562.50 by 10:10 (6 five-minute bars after the entry): the premium decays while it waits
Wrote trader/plan.json
Next: in-budget sizes it in lots, then in-risk checks it. Nothing was placed: this pack never places an order.
```

The arithmetic: underlying risk 23,562.50 - 23,515.00 = 47.50 points, target
23,562.50 + 2 x 47.50. Premium risk 112.40 - 78.65 = 33.75, target 112.40 + 2
x 33.75 = 179.90, and one lot risks 33.75 x 65 = Rs 2,193.75.

## Rules

- The stops, the targets and the flat-by are set before the buy, never after.
- Never move a stop further from the entry once the trade is on.
- A risk BLOCK never stops an exit. in-risk gates new entries only.
- Lot size, tick size and expiry come from the instrument object (the
  master), never from a number in the code.
- Buy-only CE and PE. No short selling, no option selling.
- Plain numbers only. Nothing here promises a winning trade; a 2R target is
  not a prediction that the price gets there.

## What it never does

It never places an order, never sizes a position (that is `in-budget`),
never widens a stop to make a plan pass, never calls a stop a guarantee, and
never plans a trade at or after the flat-by time.

## Safety

- Not financial advice. A plan is arithmetic on the entry and the pick.
- Analysis only: the pack never places an order. The user places and closes
  every order by hand in the Angel One app.
- Personal, local use only.
