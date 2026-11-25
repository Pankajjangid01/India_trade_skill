---
name: in-scanner
description: >-
  Scanner for the Indian Day Trader Skill Pack (NSE and BSE through Angel One
  SmartAPI). Builds the universe from the instrument master (NIFTY, SENSEX,
  BANKNIFTY and the F&O stocks, minus the day's F&O ban list in
  trader/ban.txt), measures the 09:15 to 09:30 opening range from the live
  feed (candles only for a late start), the volume ratio (an index uses its
  nearest futures contract, since index candles carry no volume), and the
  daily numbers: gap against the previous close, volume against the 20-day
  average, ATR(14) and distance from the 52-week high and low. Stocks pass
  price, average-volume and bid-ask spread filters, skip results and board
  meeting days and cautionary (ASM/GSM) scrips, and get a liquidity grade.
  Writes trader/watchlist.json for in-entry. Read-only: it never
  places an order. Use when the user says "scan the market", "opening range",
  "what is the range on NIFTY", "ORB scan", "build my watchlist", "F&O
  universe", "which stocks are in ban", "daily scan", "gap and ATR",
  "pre-market scan", or at the start of the trading day, after /in-news.
argument-hint: "universe | orb [--stocks] | daily [--stocks] [--now \"YYYY-MM-DD HH:MM\"] [--offline DIR]"
---

# in-scanner

The first step of a day: which instruments are in play, and where their
opening range sits. A scan is a list of numbers that match a rule, not a list
of things to buy. It reads, measures and writes `trader/watchlist.json`;
nothing else.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md
lives). Use `python3` instead of `python` on macOS and Linux.

```bash
python "${CLAUDE_SKILL_DIR}/scan.py" universe          # indices + F&O stocks minus trader/ban.txt
python "${CLAUDE_SKILL_DIR}/scan.py" orb               # opening range of NIFTY, SENSEX, BANKNIFTY
python "${CLAUDE_SKILL_DIR}/scan.py" orb --stocks      # and the F&O stocks, most liquid first
python "${CLAUDE_SKILL_DIR}/scan.py" daily             # last session: gap, volume, ATR(14), 52-week distance
```

Other flags: `--trader DIR` (default `./trader`), `--now "2026-11-24 09:31"`
(IST), `--offline DIR` (replay saved replies; `DIR/master.json` is the
master). Exit code 0 when a file was written with something in it, 1 when
nothing could be fetched or on an error, in one plain line.

## What it reads and writes

| file | what |
| --- | --- |
| instrument master | today's copy in `trader/cache/` (in-setup downloads it once per IST day) |
| `trader/ban.txt` | the day's F&O ban list, one symbol a line, `#` comments allowed. You keep it. A stock enters the ban at 95% of its market-wide position limit and leaves below 80%. A file not changed today is questioned in the report |
| `trader/news.json` | today's flags from in-news, which runs first: a stock flagged `RESULT_TODAY`, `BOARD_MEETING` or `ASM_GSM` is skipped. Yesterday's file is ignored |
| `trader/state/ticks.sqlite` | the live feed's bars and last ticks (`in-common/feed.py`) |
| `trader/config.json` | `session.range_end` (09:30), `freshness.stale_tick_sec` (3), `limits.quote_tokens_per_call` (50), and the `scanner` section below |
| `trader/universe.json` | written by `universe`: every instrument, what the ban removed, what was left out and why |
| `trader/watchlist.json` | written by every mode (shape in CONTRACTS.md): `mode`, `as_of_ist`, `source`, `partial`, `partial_note`, `items`, `skipped` |
| `trader/state/subscriptions.json` | `orb` asks the feed (owner `in-scanner`) for every token it scans |

The `scanner` section of `trader/config.json` is optional; these are the
defaults in the code:

```json
"scanner": {"min_price": 50.0, "min_avg_volume": 100000, "max_spread_pct": 0.5}
```

## The universe

From the master, every day, never from memory:

- NIFTY, SENSEX and BANKNIFTY: index token and segment, the options segment,
  the lot size of the nearest expiry, and the nearest FUTIDX contract (its
  volume stands in for the index's).
- Every name with NSE stock options (OPTSTK on NFO): its NSE cash token and
  the lot size of its nearest expiry. A name with no `-EQ` cash row is
  reported and left out, never guessed.
- Minus every symbol in `trader/ban.txt`. A ban name that is not an F&O name
  in the master is reported, so a typo does not pass silently.

`orb` and `daily` rebuild it the same way on every run.

## The opening range (orb)

- The range is the high and low of the 09:15, 09:20 and 09:25 five-minute
  bars, built by the live feed on the exchange clock. A bar counts only once
  it has closed. Before 09:30 the range is `forming`: the report shows it so
  far, and the file says `partial` with every range left empty.
- A symbol the feed lacks (a late start) is back-filled from 5-minute candles
  after 09:30, through the shared limiter (28 a minute by design), most liquid
  first. Its last price is unknown and it is marked STALE.
- Last price and feed lag come from the feed's latest tick; a tick older than
  `freshness.stale_tick_sec` is STALE.
- Volume ratio: today's range volume against the average range volume of the
  last 5 sessions (5-minute candles, cached forever). For an index it is the
  nearest FUTIDX contract's volume, named in the report.
- Run `orb` once before 09:15 too: it subscribes the tokens, so the feed has
  the whole range live.

## The daily numbers (daily)

From ONE_DAY candles to the last completed session (the previous trading
day): its close, volume against the average of the 20 sessions before it
(the futures contract for an index), ATR(14) with Wilder smoothing, and the
percent distance from the 52-week high and low (fewer than 240 sessions is
said). Once the market is open the FULL quote's open gives today's gap.

## Stocks (--stocks)

Before any candle is spent, one FULL quote per 50 stocks and one
`cautionaryScrips` call (documented as a GET with `{"scripconsent": "yes"}`).
A stock is skipped, with the reason, when it carries a `RESULT_TODAY`,
`BOARD_MEETING` or `ASM_GSM` flag in today's news.json, appears on the
cautionary list, has no two-sided quote, trades under `min_price`, has a
spread (best ask minus best bid over the mid) over `max_spread_pct`, or
averages under `min_avg_volume`. The rest are scanned most liquid first
(price times today's volume). `liquidity_grade`: A for a spread of at most
0.05% and 1,000,000 a day, B for 0.15% and 200,000, else C. Outside market
hours the quote is skipped and the report says the spread was not checked.

## Steps

1. Once a day, before 09:15: `scan.py universe`. Read back the counts, what
   the ban removed and anything left out. If `ban.txt` is old, ask the user to
   update it from the exchange's list.
2. Then `/in-news`: it reads the indices and `universe.json` and writes
   today's flags to `news.json`, which the scans below apply.
3. Before 09:15: `scan.py orb` (subscribes the feed), and `scan.py daily` for
   the gap, ATR and volume context.
4. After 09:30: `scan.py orb`. Read back each range, the source (feed or
   candles), the feed lag, STALE marks and any `Fetched N of M`.
5. Hand the watchlist on to `/in-entry`. Never pick for the user.

## Example: the universe

`universe --offline tests/fixtures/in-scanner --now "2026-11-24 08:50"`
with `SAIL` and `XYZ` in `trader/ban.txt`. The master slice is synthetic.

```text
Universe, Tue 2026-11-24 08:50 IST (offline replay)
  Not financial advice: rule matches and arithmetic, never a prediction.
  Indices        NIFTY (NSE 99926000, options NFO, lot 65), SENSEX (BSE 99919000, options BFO, lot 20), BANKNIFTY (NSE 99926009, options NFO, lot 30)
  F&O stocks     6 names with NSE stock options in the master
  Ban removed    SAIL (in trader/ban.txt)
  Not matched    TATAMOTORS: no NSE cash row in the master, left out
  Ban list       XYZ is not an F&O name in the master (check trader/ban.txt)
  Universe       3 indices + 4 stocks: NIFTY, SENSEX, BANKNIFTY, IDEA, INFY, RELIANCE, SBIN
Wrote trader/universe.json and trader/watchlist.json (universe, 7 items)
```

## Example: the opening range

`orb --now "2026-11-24 09:31"` on synthetic data: the feed has NIFTY,
SENSEX and the NIFTY future; BANKNIFTY and the other futures come from
candles. NIFTY24NOV26FUT traded 1,350,000 in the range against a 5-session
average of 1,000,000, hence 1.35.

```text
Opening range scan, Tue 2026-11-24 09:31 IST (offline replay)
  Not financial advice: rule matches and arithmetic, never a prediction.
  Range          09:15 to 09:30, complete for 3 of 3
  NIFTY          23,480.00 to 23,550.00 (feed); last 23,562.50, lag 250 ms; volume ratio 1.35 (NIFTY24NOV26FUT)
  SENSEX         77,150.00 to 77,320.00 (feed); last 77,301.10, lag 300 ms; volume ratio 1.20 (SENSEX26NOVFUT)
  BANKNIFTY      52,010.00 to 52,240.00 (candles); no live tick, STALE; volume ratio 1.20 (BANKNIFTY24NOV26FUT)
  Feed           6 tokens asked of the live feed (owner in-scanner)
Fetched 3 of 3.
Wrote trader/watchlist.json (orb, 3 items, complete)
API usage: none (offline replay)
```

When a symbol fails, its line says `FAILED` with the reason, the count reads
`Fetched 2 of 3.` and the file is `partial`, its `partial_note` naming what
failed. A partial scan is never rounded up to a complete one.

## Example: the daily scan

`daily --now "2026-11-24 09:20"` on generated candles (260 sessions to Mon
2026-11-23) and a synthetic FULL quote for today's open.

```text
Daily scan, Tue 2026-11-24 09:20 IST (offline replay)
  Not financial advice: rule matches and arithmetic, never a prediction.
  Session        ONE_DAY candles to Mon 2026-11-23; gap from the FULL quote's open
  NIFTY          close 23,380.00, gap +0.30%; volume 1.25x the 20-day average (NIFTY24NOV26FUT); ATR(14) 120.00; -0.26% from the 52-week high, +12.78% from the low
  SENSEX         close 77,480.00, gap -0.10%; volume 0.80x the 20-day average (SENSEX26NOVFUT); ATR(14) 400.00; -0.26% from the 52-week high, +3.74% from the low
  BANKNIFTY      close 52,090.00, gap -0.08%; volume 1.50x the 20-day average (BANKNIFTY24NOV26FUT); ATR(14) 300.00; -0.29% from the 52-week high, +5.55% from the low
Fetched 3 of 3.
Wrote trader/watchlist.json (daily, 3 items, complete)
API usage: none (offline replay)
```

## Rules

- Lot sizes, tokens, segments and expiries come from the master every day.
  Never assume them.
- Say the source of every range (feed or candles), the feed lag, STALE, and
  `Fetched N of M`. Say "matched a rule" or "the range is", never "a buy".
- Every REST call goes through `in-common/smart.py` (one shared limiter).
  Quotes only 09:00 to 15:45 IST on trading days; history any time. A live
  run refuses unless in-setup's probe confirmed `getCandleData` (and `quote`
  when it quotes).
- Banned stocks are never scanned. A stale ban list is questioned, not used
  silently.
- Version 1 trades index options; `--stocks` is context only until phase 5.

## What it never does

It never places, modifies or cancels an order: the pack has no order
function and a test fails the build if any file names an order endpoint. It
never says which instrument to buy, never calls candle data live, never
scrapes the NSE website, and never fills a missing number with a guess.

## Safety

- Not financial advice. A scan is arithmetic on market data, not a
  recommendation or a prediction.
- Analysis only: the pack never places an order. The user places and closes
  every order by hand in the Angel One app.
- Personal, local use only.
