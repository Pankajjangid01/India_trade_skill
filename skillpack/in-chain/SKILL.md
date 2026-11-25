---
name: in-chain
description: >-
  Option chain, chain analytics and strike pick for the Indian Day Trader
  Skill Pack (NIFTY, SENSEX and BANKNIFTY index options, and F&O stocks,
  through Angel One SmartAPI). Takes a snapshot of the nearest expiry's chain
  at ATM +/- N strikes from the instrument master, the live feed and FULL
  quotes; Greeks from optionGreek for NSE, computed locally (Black-Scholes,
  labelled COMPUTED) for SENSEX and BANKEX; then PCR by OI and volume
  (cross-checked against putCallRatio), max pain, the highest-OI strikes, OI
  change over about 15 minutes, the ATM straddle and its implied move, IV rank
  against its own history, and a buildup label. pick chooses a CE (bullish)
  or PE (bearish) by rule: delta band, spread, volume, OI and one-lot cost,
  and writes trader/pick.json for in-exit. Every snapshot is logged to
  trader/history/chain. Read-only: it never places an order. Use when the
  user says "show the NIFTY chain", "option chain", "SENSEX chain", "PCR",
  "put call ratio", "max pain", "where is the OI", "OI buildup", "straddle
  price", "implied move", "IV rank", "which strike should the rule pick",
  "pick a strike", "pick the CE", "pick the PE", "chain history", or after
  in-entry triggers and before in-exit.
argument-hint: "snapshot UNDERLYING | pick UNDERLYING [--direction bullish|bearish] | history UNDERLYING [--last N] [--date YYYY-MM-DD]"
---

# in-chain

The option chain of one underlying, described, and a strike chosen by a
written rule when the user has a direction. Everything it prints is a
description of the chain right now: PCR, max pain, OI and buildup are
counts and labels, never a forecast, and a pick is a rule match, not a
recommendation.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md
lives). Use `python3` instead of `python` on macOS and Linux.

```bash
python "${CLAUDE_SKILL_DIR}/chain.py" snapshot NIFTY                    # -> trader/chain/NIFTY.json + a history line
python "${CLAUDE_SKILL_DIR}/chain.py" snapshot SENSEX                   # Greeks COMPUTED (no optionGreek for BSE)
python "${CLAUDE_SKILL_DIR}/chain.py" pick NIFTY                        # direction from trader/entry.json -> trader/pick.json
python "${CLAUDE_SKILL_DIR}/chain.py" pick BANKNIFTY --direction bearish
python "${CLAUDE_SKILL_DIR}/chain.py" history NIFTY --last 5            # today's snapshots
```

Other flags: `--trader DIR` (default `./trader`), `--now "2026-11-25 10:20"`
(IST), `--offline DIR` (replay saved replies and `DIR/master.json` instead of
calling the API), `--date YYYY-MM-DD` for `history`. Exit code 0 for a
snapshot, a pick or history lines; 1 when no strike passed the pick rules or
on an error, in one plain line.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | `options.strikes_each_side`, `delta_min`, `delta_max`, `max_spread_pct`, `min_volume`, `min_oi`; `account.capital_inr` and `max_capital_per_trade_pct` (pick); `api.risk_free_rate_pct` (computed Greeks); `session.expiry_day_last_entry`, `data_calls_stop`; `limits.quote_tokens_per_call`; `freshness.stale_tick_sec` |
| `trader/cache/instrument_master_<date>.json` | the day's master: expiries, strikes, step, lot, tick, freeze quantity, the futures contract PCR is named by |
| `trader/state/ticks.sqlite` | the live feed's latest ticks (read only; `in-common/feed.py` writes it) |
| `trader/state/capabilities.json` | live runs refuse quotes the probe did not confirm and skip optionGreek or putCallRatio it did not confirm |
| `trader/entry.json` | pick: the direction, when it is `triggered` today for this underlying |
| `trader/state/subscriptions.json` | written: the chain's tokens (SnapQuote) and the index (Quote) under the owner `in-chain:<UNDERLYING>`, so the feed streams them |
| `trader/chain/<UNDERLYING>.json` | written: the latest snapshot, as `CONTRACTS.md` shows, plus `partial_note`, `stale` (the spot), `pcr_api`, `oi_change_since_ist`, and `age_sec` and `stale` on each row |
| `trader/history/chain/<UNDERLYING>/<date>.jsonl` | appended: one line per snapshot, the same object. This is how option history outlives its contracts |
| `trader/pick.json` | written by pick: the instrument object from the master, premium, bid, ask, spread, volume and OI (in-risk checks them), delta, one-lot cost, the rule and the next best |

## How a snapshot is built

1. Refuses outside market-data hours (trading days, 09:00 to
   `session.data_calls_stop` IST, holidays from `calendar.json`).
2. Expiry from the master: the nearest one, or the next one when today is the
   nearest's expiry day and the time is past `session.expiry_day_last_entry`.
   No weekday is assumed.
3. Spot from the feed's latest tick on the index (or the stock's NSE cash
   row) when it is fresh, else one FULL quote. ATM is spot rounded to the
   strike step, which is counted from the expiry's listed strikes. Rows are
   ATM +/- `strikes_each_side` strikes, CE and PE.
4. FULL quotes, at most 50 tokens a call (`limits.quote_tokens_per_call`),
   spaced by the shared limiter. Bid and ask are the depth's best level;
   `spread_pct` is the spread over the mid. A feed tick newer than the
   quote's feed time replaces LTP, volume and OI (the feed has no depth).
5. Greeks: NSE calls `optionGreek` with the master's expiry
   (`{"name", "expirydate"}`), `greeks_source` API. BSE has no such endpoint:
   IV is solved from each LTP with Black-Scholes, the rate from
   `api.risk_free_rate_pct` (it refuses while that is empty), time to the
   expiry day's 15:30, and the output says COMPUTED.
6. Every row carries its age (now minus the exchange time). A row with no
   update for over `freshness.stale_tick_sec` is stale and named; a stale
   spot marks the whole snapshot stale. A failed quote call, a missing row or
   missing Greeks make it partial, with the reason.

## The analytics, all descriptive

| number | how |
| --- | --- |
| PCR | put OI over call OI, and put volume over call volume, on the fetched strikes. For NSE it also prints `putCallRatio` for the underlying's near futures symbol (all strikes) |
| max pain | the fetched strike at which option holders, by OI, would be paid the least at expiry |
| highest OI | the three strikes with the most OI on each side |
| OI change | against today's snapshot nearest to 15 minutes earlier (10 to 20 minutes back), per row; none without one |
| straddle | ATM CE + ATM PE premium; implied move is the straddle over spot, to expiry |
| IV rank | today's ATM IV within the last 252 days of this skill's own snapshots (each day's last); `null` until 20 days exist |
| buildup | from the change in spot and in the chain's total OI since that earlier snapshot: Long Built Up (up, OI up), Short Built Up (down, OI up), Short Covering (up, OI down), Long Unwinding (down, OI down) |

## The pick rule

Direction from `trader/entry.json` when its status is `triggered`, its date
is today and its symbol is this underlying, or `--direction`. Bullish looks
at CEs, bearish at PEs. A strike passes when every one holds:

- not stale, and the spot is not stale;
- absolute delta within `options.delta_min` to `delta_max` (default 0.35 to 0.55);
- `spread_pct` at most `options.max_spread_pct` (default 3%);
- volume at least `options.min_volume`, OI at least `options.min_oi`;
- one lot (LTP x lot size from the master) within
  `account.capital_inr` x `account.max_capital_per_trade_pct` / 100.

Of those that pass, the delta closest to the middle of the band wins (then
the tighter spread, then the lower strike). It prints the rule, the strike
it chose, the next best, and every strike that failed with the reason and
the number. When nothing passes it says so, writes no pick.json and exits 1.

## Steps

1. The feed should be running (`in-common/feed.py run`) so spot and premiums
   are live; without it the snapshot uses FULL quotes only and says so.
2. Run `snapshot UNDERLYING`. Read back the source, the age and anything
   stale or partial before any number. Say COMPUTED when it says COMPUTED.
3. After in-entry triggers, run `pick UNDERLYING`. Read back the rule, the
   strike it matched, the next best and why the others failed. Then hand
   on to in-exit, which reads `trader/pick.json`.
4. If nothing passes, say so. Do not loosen a rule to make something pass;
   the user changes `config.json` by hand if they want different rules.

## Example: a NIFTY snapshot

`snapshot NIFTY --offline tests/fixtures/in-chain/nifty --now "2026-11-25 10:20"`
with `options.strikes_each_side` 3 and `limits.quote_tokens_per_call` 10,
a feed tick store holding the index and the 23500 CE, and a 10:05 snapshot
in today's history (the test builds all three). The fixtures are synthetic,
shaped like the documented replies, so this shows the report's shape, not
a real market.

```text
NIFTY option chain, Wed 2026-11-25 10:20 IST (offline replay)
  Not financial advice: rule matches and arithmetic, never a prediction.
  Expiry          Tue 2026-12-01 (01DEC2026), the nearest
  Spot            23512.35 from the feed, lag 250 ms, age 1.2 s
  Strikes         ATM 23500, step 50, ATM +/- 3 strikes: 14 of 14 rows quoted in 2 quote calls, 1 from fresher feed ticks
  Freshness       rows 1.0 s median, 9.0 s worst; stale (no update for over 3 s): 23500 PE
  Greeks          API (optionGreek)
  PCR             OI 0.92, volume 0.96 on these strikes; putCallRatio 1.04 for NIFTY29DEC26FUT (all strikes)
  Max pain        23500 (these strikes only)
  Highest OI      CE 23600, 23500, 23550; PE 23400, 23500, 23350
  Straddle        CE 191.15 + PE 152.75 = 343.90 at 23500, implied move 1.46% by expiry
  OI change       since the 10:05 snapshot: CE +112,500, PE +154,000
  Buildup         Long Built Up (spot +32.35, chain OI +266,500 since 10:05): a label for what happened, not a forecast
  IV rank         not yet: 0 of 20 days of this skill's own history
      CE OI      chg     IV  delta      LTP  spr% | strike  |      LTP  spr%  delta     IV      chg     PE OI
  1,200,000  +15,000   14.3   0.67   284.55  0.04 | 23350   |    96.35  0.10  -0.33   14.3  +22,000 2,400,000
  1,500,000  +15,000   14.2   0.63   251.25  0.04 | 23400   |   113.05  0.09  -0.37   14.2  +22,000 4,100,000
  1,900,000  +15,000   14.1   0.58   220.05  0.05 | 23450   |   131.80  0.08  -0.42   14.1  +22,000 2,200,000
  3,207,500  +22,500   14.0   0.54   191.15  0.05 | 23500 * |   152.75  0.07  -0.46   14.0  +22,000 3,000,000
  2,600,000  +15,000   13.9   0.49   164.35  0.06 | 23550   |   175.95  0.06  -0.51   13.9  +22,000 1,100,000
  4,500,000  +15,000   13.8   0.45   140.00  3.57 | 23600   |   201.55  0.05  -0.55   13.8  +22,000   600,000
      3,000  +15,000   13.7   0.40   117.95  0.08 | 23650   |   229.45  0.04  -0.60   13.7  +22,000   350,000
  Feed            15 tokens asked of the live feed (trader/state/subscriptions.json, owner in-chain:NIFTY)
Wrote trader/chain/NIFTY.json; appended trader/history/chain/NIFTY/2026-11-25.jsonl
API usage: none (offline replay)
```

The 23500 CE's LTP, volume and OI came from the feed tick, newer than its
quote; its bid and ask still come from the quote's depth. The 23500 PE's
quote was 9 s old, so it is named stale and pick will not choose it. A live
run's first line says `live, Angel One SmartAPI` and the last line is the
day's API usage against the design caps.

## Example: SENSEX, Greeks computed

`snapshot SENSEX --offline tests/fixtures/in-chain/sensex --now "2026-11-25 10:20"`,
same config with `api.risk_free_rate_pct` 6.5, no feed ticks for SENSEX:
spot comes from one FULL quote, and there is no optionGreek reply in the
folder because none is asked for.

```text
SENSEX option chain, Wed 2026-11-25 10:20 IST (offline replay)
  Not financial advice: rule matches and arithmetic, never a prediction.
  Expiry          Thu 2026-11-26 (26NOV2026), the nearest
  Spot            77215.60 from the FULL quote (no fresh feed tick), age 1.0 s
  Strikes         ATM 77200, step 100, ATM +/- 3 strikes: 14 of 14 rows quoted in 2 quote calls, 0 from fresher feed ticks
  Freshness       rows 1.0 s median, 1.0 s worst; stale (no update for over 3 s): none
  Greeks          COMPUTED: Black-Scholes, IV solved from each LTP, rate 6.50% from config, 1.22 days to expiry (no optionGreek for BSE)
  PCR             OI 0.73, volume 0.70 on these strikes; no putCallRatio cross-check (NSE only)
  Max pain        77200 (these strikes only)
  Highest OI      CE 77400, 77200, 77300; PE 77200, 77000, 77100
  Straddle        CE 247.55 + PE 215.25 = 462.80 at 77200, implied move 0.60% by expiry
  OI change       none: no snapshot from 10 to 20 minutes ago in today's history
  IV rank         not yet: 0 of 20 days of this skill's own history
      CE OI      chg     IV  delta      LTP  spr% | strike  |      LTP  spr%  delta     IV      chg     PE OI
    310,000        -   13.2   0.72   436.10  0.23 | 76900   |   103.90  0.96  -0.28   13.2        -   520,000
    420,000        -   13.1   0.66   366.90  0.27 | 77000   |   134.65  0.74  -0.34   13.1        -   690,000
    560,000        -   13.1   0.59   303.90  0.33 | 77100   |   171.65  0.58  -0.41   13.1        -   610,000
    880,000        -   13.0   0.52   247.55  0.40 | 77200 * |   215.25  0.46  -0.48   13.0        -   740,000
    720,000        -   12.9   0.45   198.00  0.51 | 77300   |   265.65  0.38  -0.55   12.9        -   380,000
    950,000        -   12.9   0.39   155.35  0.64 | 77400   |   323.00  0.31  -0.61   12.9        -   210,000
    640,000        -   12.8   0.32   119.35  0.84 | 77500   |   387.00  0.26  -0.68   12.8        -   120,000
  Feed            15 tokens asked of the live feed (trader/state/subscriptions.json, owner in-chain:SENSEX)
Wrote trader/chain/SENSEX.json; appended trader/history/chain/SENSEX/2026-11-25.jsonl
API usage: none (offline replay)
```

## Example: the pick

`pick NIFTY --offline tests/fixtures/in-chain/nifty --now "2026-11-25 10:20"`,
same setup, with `trader/entry.json` triggered bullish on NIFTY at 10:15 and
`account.capital_inr` 500000.

```text
NIFTY pick, Wed 2026-11-25 10:20 IST (offline replay): bullish from entry.json (bar closed 10:15), so CE strikes
  Not financial advice: rule matches and arithmetic, never a prediction.
  Chain           expiry Tue 2026-12-01, spot 23512.35, Greeks API, as of 10:20
  Rule            CE, |delta| 0.35 to 0.55 (closest to 0.45 wins), spread <= 3%, volume >= 1000, OI >= 5000, one lot <= 30% of capital (Rs 150,000.00), not stale
  Picked          NIFTY01DEC2623550CE (token 45006), delta 0.49, premium 164.35 (bid 164.30, ask 164.40, spread 0.06%), volume 950,000, OI 2,600,000, one lot 65 x 164.35 = Rs 10,682.75
  Next best       NIFTY01DEC2623500CE (token 45004), delta 0.54, premium 191.15 (bid 191.05, ask 191.15, spread 0.05%), volume 1,204,500, OI 3,207,500, one lot 65 x 191.15 = Rs 12,424.75
  Failed          delta outside 0.35 to 0.55: 23350 (0.67), 23400 (0.63), 23450 (0.58)
                  spread over 3%: 23600 (3.57%)
                  volume under 1,000: 23650 (420)
                  OI under 5,000: 23650 (3,000)
Wrote trader/pick.json and trader/chain/NIFTY.json; appended trader/history/chain/NIFTY/2026-11-25.jsonl
API usage: none (offline replay)
```

The 23600 CE's delta (0.45) sits right on the band's middle, but its spread
fails the rule, so the 23550 CE is the match. With `--direction bearish` the
23500 PE (delta 0.46, the closest) is refused as stale and the 23450 PE is
the match.

## Example: history

`history NIFTY --now "2026-11-25 10:20"` after the snapshot above:

```text
NIFTY chain history, 2026-11-25: 2 snapshots in trader/history/chain/NIFTY/2026-11-25.jsonl, the last 2 shown
  Not financial advice: rule matches and arithmetic, never a prediction.
  time   expiry           spot  ATM      PCR OI  PCR vol  max pain  straddle   move%  buildup         flags
  10:05  2026-12-01   23480.00  23500      1.06     0.90  23500       318.40    1.36  -               -
  10:20  2026-12-01   23512.35  23500      0.92     0.96  23500       343.90    1.46  Long Built Up   -
```

## Rules

- Every REST call goes through the shared client and limiter in
  `in-common/smart.py`; quotes take at most 50 tokens a call.
- Expiry, strikes, strike step, lot size, tick size and freeze quantity come
  from the day's master, never from memory. No weekday is assumed.
- Computed Greeks say COMPUTED, and refuse to run without the risk-free rate.
- Stale rows are named and never picked; a stale spot gives no pick at all.
  A partial snapshot says what is missing and why.
- PCR, max pain, OI change and buildup are descriptions of the chain now.
  Never call them a signal, a forecast or a reason to trade.
- A pick is the strike a written rule matched. Show the rule, the match, the
  next best and every failure, and let the user decide.

## What it never does

It never places, modifies or cancels an order: the code has no order
function and a test fails the build if any file names an order endpoint. It
never sells an option, never picks without a direction, never loosens a rule
to find a match, never scrapes the NSE website, and never fills a missing
number with a guess.

## Safety

- Not financial advice. A chain is arithmetic on market data; a pick is a
  rule match, not a recommendation. Most intraday option buyers lose money.
- Analysis only: the pack never places an order. The user places and closes
  every order by hand in the Angel One app.
- Buy-only CE and PE in version 1. No option selling, no short selling.
- Personal, local use only. Credentials stay in environment variables; this
  skill never sees, prints or writes one.
