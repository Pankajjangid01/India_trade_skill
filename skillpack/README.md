# Indian Day Trader Skill Pack

A toolkit for **intraday analysis of Indian index options** (NIFTY, SENSEX,
BANKNIFTY) using live data from your own **Angel One SmartAPI** account. It
scans the market, finds an opening-range breakout, picks an option strike by
rule, plans the stop and target, sizes the trade to your risk limits, checks it
against every limit, and prints a ticket that **you** type into the Angel One
app yourself.

It is 13 small Python tools ("skills") that work on their own from a terminal,
or from [Claude Code](https://claude.com/claude-code), where you can simply
ask "scan the market" or "plan my exit".

> **Analysis only. It never places an order.** The code has no order function at
> all, and a test fails the build if any file even names an order endpoint. You
> place and close every trade by hand in the Angel One app.
>
> **Not financial advice.** It reports rule matches and arithmetic, never
> predictions. Most people who trade intraday options lose money.

---

## Contents

1. [What you need](#what-you-need)
2. [Setup, step by step](#setup-step-by-step)
3. [A trading day](#a-trading-day)
4. [The 13 skills](#the-13-skills)
5. [Using it from Claude Code](#using-it-from-claude-code)
6. [Your settings](#your-settings)
7. [Status: what is tested](#status-what-is-tested)
8. [Troubleshooting](#troubleshooting)
9. [How it is built](#how-it-is-built)
10. [Contributing](#contributing)

---

## What you need

| | |
| --- | --- |
| **An Angel One trading account** | with F&O enabled |
| **A SmartAPI app** | create one at [smartapi.angelone.in](https://smartapi.angelone.in) to get an **API key** |
| **TOTP enabled** on your Angel One account | keep the **TOTP secret** (the text shown with the QR code) so the tools can log in without asking for a code |
| **Python 3.9 or newer** | nothing else: no `pip install`, standard library only |
| **Git** | to download the project |
| Claude Code (optional) | to use the skills by asking in plain English |

Works on Windows, macOS and Linux.

---

## Setup, step by step

### 1. Download

```bash
git clone https://github.com/Rahul-carpenter/indian-day-trader-skill.git
cd indian-day-trader-skill
```

The repository is private: ask the owner to add you as a collaborator first.

### 2. Put your credentials in a `.env` file

Create a file named `.env` in the folder you will run the commands from (the
project folder is fine; git already ignores it):

```text
ANGEL_API_KEY=your-smartapi-app-key
ANGEL_CLIENT_CODE=your-angel-one-client-code
ANGEL_PIN=your-4-digit-pin
ANGEL_TOTP_SECRET=the-secret-shown-with-the-TOTP-QR-code
```

- Use exactly these four names.
- Never paste them into a chat, an email or a file you commit.
- `ANGEL_TOTP_SECRET` is optional: without it, `login` asks for the 6-digit
  code in your terminal each morning.
- Environment variables with the same names work too, and win over the file.

### 3. Create your working folder

```bash
python in-setup/firstrun.py init
```

(Use `python3` on macOS and Linux.) This creates a `trader/` folder next to your
`.env`: your settings, the day's login session, the live price store and your
trade journal all live there. Git ignores it.

### 4. Fill in your settings

Open these files in `trader/` and set:

| file | set this |
| --- | --- |
| `config.json` | `account.capital_inr` (your trading capital in rupees) and `api.risk_free_rate_pct` (for example the current T-bill yield). The pack refuses to size or check trades until both are set. The other values are starting defaults you own. |
| `calendar.json` | this year's NSE and BSE trading holidays, from the exchanges' holiday circulars. The pack never guesses a holiday. |
| `charges.json` | already filled with Angel One's rates as checked on 2026-11-24. The probe compares them with the broker's own calculator every time; set `"verified": true` once you have checked them yourself. |

### 5. Check your account (once, in market hours)

On a trading day between **09:15 and 15:30 IST**:

```bash
python in-setup/firstrun.py probe --key-label main
```

The probe logs in and measures what the documentation leaves open: which data
your API key may use, the server clock, the instrument master's formats, the
live stream and its delay ("feed lag"), and whether your charges match the
broker. It writes `trader/state/capabilities.json`. **Every other skill refuses
a feature the probe has not seen working on your account**, so run this first.

Outside market hours it still checks login, history and charges, and tells you
what it skipped.

### 6. Start the live feed (every trading day)

```bash
python in-common/feed.py run --underlyings NIFTY,SENSEX --strikes 2
```

Leave it running in its own terminal. It is the only process that holds the
live connection; every other skill reads the prices it stores. It reconnects by
itself and stops at 15:45. Check on it any time:

```bash
python in-common/feed.py status
```

### 7. (Optional) Install the skills for Claude Code

Copy the skill folders, **including `in-common`**, into Claude's skills folder:

```bash
# macOS / Linux
cp -r in-* ~/.claude/skills/
```

```powershell
# Windows PowerShell
Copy-Item -Recurse in-* $HOME\.claude\skills\
```

Then open Claude Code in the folder that holds your `.env` and `trader/`.

---

## A trading day

Each skill reads what the one before it wrote, through small JSON files in
`trader/` (described in [CONTRACTS.md](CONTRACTS.md)).

```
before 09:15   in-news        headlines, events, ban list, surveillance flags
09:15          feed.py run    live prices start flowing
09:31          in-scanner     the 09:15 to 09:30 opening range for each index
09:30 - 11:30  in-entry       waits for the first 5-minute close beyond the range
on a trigger   in-chain pick  picks the CE (breakout up) or PE (breakdown) by rule
               in-exit        stop and target, on the index and on the premium
               in-budget      how many lots fit your max loss, charges included
               in-risk        PASS or BLOCK against every limit you set
               in-ticket      the ticket you type into the Angel One app yourself
after you buy  in-ticket fill record what you actually got
               in-alerts      rings on stop, target, flat-by time or stale prices
after you sell in-ticket close  P&L after charges, into your journal
evening        in-flows, in-timemachine, in-analyst   (when you want them)
```

The same day as commands (run from the folder with your `.env`):

```bash
python in-news/news.py
python in-scanner/scan.py orb
python in-entry/entry.py watch NIFTY --every 60      # until it triggers, expires or the data goes stale
python in-chain/chain.py pick NIFTY
python in-exit/exit.py plan --write
python in-budget/size.py --write
python in-risk/check.py
python in-ticket/ticket.py show                       # now place it yourself in the Angel One app
python in-ticket/ticket.py fill T20261125-01 --price 112.40 --time 09:41
python in-alerts/alerts.py watch --notify
python in-ticket/ticket.py close T20261125-01 --price 150.00 --time 10:55 --reason target
```

"No signal", WAIT and BLOCK are normal answers. A BLOCK is final for that
ticket; there is no override flag. You change your limits by editing
`trader/config.json` yourself.

---

## The 13 skills

| skill | what it does | main commands |
| --- | --- | --- |
| `/in-setup` | First run, daily login, instrument master, account probe, charges check | `firstrun.py init`, `login`, `master`, `probe` |
| `/in-news` | Headlines from RSS feeds you choose, your event calendar, F&O ban, ASM/GSM and price-band flags | `news.py [--symbols A,B] [--hours 12]` |
| `/in-scanner` | The F&O universe minus the ban list, opening ranges, daily setups (gap, volume, ATR, 52-week) | `scan.py universe`, `orb`, `daily` |
| `/in-chain` | Option chain with PCR, max pain, OI, straddle and implied move, Greeks (from the API, or computed and labelled COMPUTED for SENSEX); the strike pick | `chain.py snapshot NIFTY`, `pick NIFTY`, `history NIFTY` |
| `/in-entry` | The trigger: first closed 5-minute bar beyond the 09:15 to 09:30 range, inside the entry window | `entry.py check NIFTY`, `watch NIFTY --every 60` |
| `/in-exit` | Stops on the index and on the premium, targets as a multiple of the risk, flat-by time, no-progress exit | `exit.py plan --write`, `show` |
| `/in-budget` | Lots from your max loss per trade, charges included, split at the exchange freeze limit | `size.py --write` |
| `/in-risk` | PASS or BLOCK: max loss, daily loss, trades per day, losses in a row, time window, expiry day, ban, stale data, events, liquidity | `check.py` |
| `/in-ticket` | The manual ticket for the Angel One app; records fills and exits; journal and yearly CSV for your accountant | `ticket.py show`, `fill`, `close`, `skip`, `journal` |
| `/in-alerts` | Watches your open trades on live prices: stop, target, flat-by, no progress, expiry day, stale prices | `alerts.py check`, `watch --notify` |
| `/in-timemachine` | Replays the same rules over the last 60 days, then 10,000 resamples (seed 19, so anyone gets the same answer) | `timemachine.py run --write` |
| `/in-flows` | End-of-day FII and DII flows, participant OI, bulk and block deals, insider trades, from exchange files you download | `flows.py inbox`, `load FILE`, `show` |
| `/in-analyst` | Quarterly results you paste from the exchange filing: growth, margins and a reading checklist | `facts.py add`, `show`, `checklist` |

Each skill folder has a `SKILL.md` with its full rules, every option and a
worked example. Common flags everywhere: `--trader DIR` (default `./trader`),
`--now "YYYY-MM-DD HH:MM"` to pretend it is another time (IST), and
`--offline DIR` to replay saved data without the internet.

---

## Using it from Claude Code

With the skills installed, ask in plain words, or type the command name:

- "Set up the Indian pack" or `/in-setup`
- "Scan for opening range breakouts" or `/in-scanner`
- "Has NIFTY triggered yet?" or `/in-entry`
- "Pick a strike for NIFTY" or `/in-chain`
- "Plan my stop and target" or `/in-exit`
- "How many lots can I take?" or `/in-budget`
- "Check this trade against my limits" or `/in-risk`
- "Show me the ticket" or `/in-ticket`
- "Replay this setup over the last 60 days" or `/in-timemachine`

Claude runs the same tools and reads back the results. It never asks for your
credentials in the chat: they stay in your `.env`.

---

## Your settings

Everything you can tune is in `trader/config.json`. The important ones:

| setting | default | meaning |
| --- | --- | --- |
| `account.capital_inr` | empty | your trading capital; required |
| `account.risk_per_trade_pct` | 1.0 | max loss per trade, % of capital |
| `account.max_daily_loss_pct` | 2.0 | stop trading for the day after this loss |
| `account.max_trades_per_day` | 3 | |
| `account.max_consecutive_losses` | 3 | stop after this many losses in a row (counted within the day) |
| `account.max_capital_per_trade_pct` | 30 | most of your capital one trade may use |
| `session.entry_start` / `entry_end` | 09:30 / 11:30 | the entry window |
| `session.flat_by` | 15:10 | out by then, win or lose (14:45 on expiry day) |
| `options.delta_min` / `delta_max` | 0.35 / 0.55 | the delta band for the strike pick |
| `options.max_spread_pct`, `min_oi`, `min_volume` | 3.0, 5000, 1000 | liquidity rules for the pick |
| `exit.max_premium_loss_pct` | 30 | premium stop below the entry price |
| `exit.target_r` | 2.0 | target as a multiple of the risk |
| `news.rss` | empty | the RSS feed URLs you want headlines from |
| `freshness.stale_tick_sec` | 3 | a price older than this is STALE and gives no signal |

Other files in `trader/` you may edit: `calendar.json` (holidays), `charges.json`
(brokerage and taxes), `events.json` (RBI policy, Budget, results days),
`ban.txt` (the day's F&O ban list, one symbol per line).

---

## Status: what is tested

| | |
| --- | --- |
| **All 13 skills** | built and covered by **272 offline tests**, including one that runs a whole trading day through every skill in order |
| **Checked live on Angel One** (2026-11-24, after the close) | login with TOTP, profile, server clock, instrument master, candle and OI history, and the charges check (the broker's calculator matched within 2 paise) |
| **Not yet checked live** | live quotes, option Greeks, PCR and the live stream, which need market hours; the 30-minute feed test with forced disconnects; SENSEX option exchange charges |
| **Never run** | desktop notifications on macOS and Linux |

Real formats seen on the live master: NSE option symbols look like
`NIFTY01DEC2623500CE`; BSE monthly ones like `SENSEX26NOV80700CE` and weekly
ones like `SENSEX26D0370600PE`; strikes are stored times 100; NIFTY's freeze
quantity is listed as 1801. Index 5-minute candles skip 15:15 and 15:20 (the
closing auction).

---

## Troubleshooting

| message | what to do |
| --- | --- |
| `set ANGEL_API_KEY, ... in the environment or in a .env file` | the `.env` is missing, in another folder, or uses other names. Run commands from the folder that holds it. |
| `... is not confirmed on this account yet: run in-setup probe` | run `firstrun.py probe` once in market hours. |
| `market data is closed` | live calls run only 09:00 to 15:45 IST on trading days; check `trader/calendar.json`. |
| `login failed (...)` | check the PIN, the TOTP secret and the API key. One attempt per run, never a loop. |
| `no ANGEL_TOTP_SECRET: run login in a terminal` | add the secret to `.env`, or run `firstrun.py login` yourself once a day. |
| `server ... from this computer: OFF BY MORE THAN 2 s` | fix your system clock; TOTP codes depend on it. |
| `charges ... DIFFER` | update `trader/charges.json` from the numbers the probe shows. |
| `a feed is already running` | only one feed per `trader/` folder; use `feed.py status`. |
| an index has no candles today | probably an exchange holiday: add it to `calendar.json`. |

---

## How it is built

```
in-common/        shared code, not a skill
  smart.py        SmartAPI client (rate limiter, retry, circuit breaker), session and token refresh,
                  instrument master, candle cache, calendar, charges, Greeks, the live price store
  feed.py         the one live feed: holds the stream, writes trader/state/ticks.sqlite
in-setup/ ... in-flows/   one folder per skill: SKILL.md + one Python script
tests/            272 offline tests and synthetic fixtures
CONTRACTS.md      the JSON files the skills hand to each other
```

Design rules, all enforced by tests:

- **No orders.** No order function exists, and no file may name an order endpoint.
- **Standard library only**, Python 3.9 syntax, IST as a fixed offset.
- **One shared rate limiter** for every process, at about 70% of Angel One's
  documented limits; retries only on rate-limit and server errors (2, 4, 8 s).
- **Honest output.** Every number says where it came from, when, and how old it
  is; stale data gives no signal; partial results say partial.
- **Nothing hardcoded that the exchange can change.** Lot sizes, expiries, tick
  sizes and freeze limits are read from the instrument master every day.
- **Reproducible.** `--now` pins the clock, `--offline` replays saved data, and
  every example in every `SKILL.md` is reproduced exactly by a test.

Run the tests:

```bash
python -m unittest discover -s tests
```

---

## Contributing

Read [CONTRACTS.md](CONTRACTS.md) and one skill (for example `in-setup/`) first.
Keep to the rules above, add a test for anything that touches money or safety,
and run the whole suite before you push. If you run a live check (`probe
--record DIR`), the replies are saved with tokens and personal data removed,
ready to become test fixtures.

## Fine print

**Not financial advice.** This is software that reads market data and does
arithmetic. It does not know your situation and cannot see the future. Decide
for yourself, and talk to a SEBI-registered adviser if you need advice.

**Personal, local use.** Your credentials stay on your computer, in `.env` or
environment variables. `trader/` holds the day's login session. Both are in
`.gitignore`; never commit them. Sharing trading signals with others is a
regulated activity in India: this pack is for your own use.

**The Time Machine is a replay of the past, not a forecast.** It never creates
a virtual account and never makes a live signal.

## License and credits

MIT. Modelled on [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
by Jake Schincariol (the US version), rebuilt for NSE, BSE and Angel One
SmartAPI.
