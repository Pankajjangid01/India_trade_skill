---
name: in-setup
description: >-
  First run and capability probe for the Indian Day Trader Skill Pack (NSE and
  BSE through Angel One SmartAPI). Creates the trader/ folder and config.json,
  logs in once per IST day with credentials from environment variables only,
  checks this computer's clock against the server, downloads the instrument
  master, and measures what the SmartAPI documentation does not say: which
  endpoints this API key may call, the master's strike and tick scale, lot
  sizes and expiry weekdays as the master states them, the stream's
  exchange-type codes, feed lag and heartbeat, and with --limits the real
  quote rate and the token cap. Writes trader/state/capabilities.json; every
  other in-* skill refuses a feature it did not confirm. Read-only: it never
  places an order. Use when the user says "set up the Indian pack", "first
  run", "connect Angel One", "log in to SmartAPI", "probe the API", "check my
  API key", "what does my key allow", "feed lag", "download the instrument
  master", or before any other in-* skill on a new machine.
argument-hint: "init | login | master | probe [--key-label NAME] [--limits] [--record DIR] [--offline DIR]"
---

# in-setup

The first skill of the pack, and Phase 0 of the build. It sets up the
working folder, logs in to Angel One SmartAPI for market data, and measures
the facts the documentation leaves open. Nothing in the rest of the pack
trusts a feature until this probe has seen it work on your account, with
your key, at a time the market was open.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md
lives). Use `python3` instead of `python` on macOS and Linux.

```bash
python "${CLAUDE_SKILL_DIR}/firstrun.py" init                           # trader/, config.json, calendar.json
python "${CLAUDE_SKILL_DIR}/firstrun.py" login                          # one login per IST day, clock check
python "${CLAUDE_SKILL_DIR}/firstrun.py" master                         # today's instrument master, what it says
python "${CLAUDE_SKILL_DIR}/firstrun.py" probe --key-label historical   # writes trader/state/capabilities.json
python "${CLAUDE_SKILL_DIR}/firstrun.py" probe --key-label feed --limits --record trader/recorded
```

Other flags: `--trader DIR` (default `./trader`), `--now "2026-11-24 10:20"`
(IST), `--seconds N` (how long to listen to the stream, default 20),
`--offline DIR` (replay saved replies instead of calling the API).

## Credentials

From environment variables, or else from `ANGEL_*` lines in a `.env` file in
the folder the commands run from (the one holding `trader/`, outside any repo).
The environment wins over the file. Never in chat, never in a file in a repo.

```text
ANGEL_API_KEY=...
ANGEL_CLIENT_CODE=...
ANGEL_PIN=...
ANGEL_TOTP_SECRET=...
```

| variable | what |
| --- | --- |
| `ANGEL_API_KEY` | the SmartAPI app's key (one per app: Historical Data, Market Feed or Trading) |
| `ANGEL_CLIENT_CODE` | the Angel One client code |
| `ANGEL_PIN` | the login PIN |
| `ANGEL_TOTP_SECRET` | optional: the TOTP secret, so login computes the code itself (RFC 6238, standard library) |

Without `ANGEL_TOTP_SECRET`, `login` asks for the 6-digit code, and only in a
real terminal: Claude's own shell cannot answer that prompt, so the user runs
`login` once in their terminal and the session is reused for the rest of the
IST day. No value is ever printed, logged or written; the session tokens go to
`trader/state/session.json`, owner-only.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | written from `config.example.json` if missing, never overwritten. Capital and the risk-free rate start empty: the trading skills refuse to run until the user sets them |
| `trader/calendar.json` | holidays and special sessions, filled by the user from the NSE and BSE circulars. Live calls stop on a listed holiday |
| `trader/charges.json` | brokerage, STT, exchange, SEBI, stamp duty and GST rates. Starts `"verified": false`; the probe compares it with the broker's own calculator |
| `trader/state/session.json` | the day's login, for this API key only |
| `trader/state/ratelimit.json` | every REST call's time, shared by all skills through one limiter |
| `trader/state/public_ip.json` | the public IP the API wants in a header, looked up once a day unless `api.client_public_ip` is set |
| `trader/cache/instrument_master_<date>.json` | the master, once per IST day; older copies are deleted |
| `trader/state/capabilities.json` | the probe's measurements, per API key, and `confirmed`: feature to the key labels that worked live |

## What the probe measures

| check | how |
| --- | --- |
| clock | the server's Date header against this computer; more than 2 s off is a warning (TOTP depends on the clock) |
| master | fields present, `exch_seg` and `instrumenttype` labels, strike scale (raw strike over the strike in the trading symbol, every option row), tick scale (inferred), lot size, strike step, freeze quantity and the next four expiries with their weekdays for NIFTY, BANKNIFTY and SENSEX |
| endpoints | `getProfile`, `getCandleData` (and whether index candles carry volume), `getOIData`, `quote` on the indices and on ATM options in FULL mode (OI, depth, circuits, feed time), `optionGreek`, `putCallRatio`: OK or the exact error, per API key |
| stream | SnapQuote subscription to the NIFTY and SENSEX indices and one ATM option each: which exchange-type code works for each segment, feed lag (receipt time minus exchange time, median and worst), the pong reply, and the paise scale against the REST quote |
| charges | the broker's `estimateCharges` for one NIFTY lot bought and sold at the ATM premium, against `charges.json`: match within Rs 0.10 or the difference (a calculator, so it runs any time) |
| `--limits` | 5 quote calls as fast as the API answers (does 1/s or 10/s hold, and what a 403 looks like), and a 1,001-token subscription (what the stream does past the 1,000-token figure from the forum) |

Live checks (quote, Greeks, PCR, the stream, `--limits`) run only 09:00 to
15:45 IST on a trading day. Outside that the probe still logs in, reads the
master and the history endpoints, and says the rest was skipped.

## Steps

1. Run `init`. Read back what it wrote and the to-do lines.
2. Ask the user to set the credential variables in their own terminal or a
   `.env` file next to `trader/`. Never ask for the values in chat. Without a
   TOTP secret, the user runs `login` themselves once.
3. In market hours, run `probe --key-label NAME` once per API key the user
   has (for example `historical`, then `feed`), switching `ANGEL_API_KEY`
   between runs. Add `--limits` once, on one key.
4. Read back the report: what was confirmed, what failed and the exact error,
   the feed lag, the scales. Do not round a failure into a success.
5. If the charges differ, show both totals; the user fixes `charges.json`
   from Angel One's charges page and sets `verified`, `as_of` and `source`.
6. Offer `--record trader/recorded` on one run: it saves the real replies,
   with tokens and the profile redacted, to become the offline test fixtures.
7. This is the Phase 0 gate. Show the user the measured numbers, so section 3
   of the BRD can be updated. The live feed (`in-common/feed.py`) refuses to
   start until the probe has confirmed the stream.

## Example: the first run

`init` on a new machine with the three required variables set:

```text
Set up trader/ (state, cache, history/chain).
  config.json    written from the template
  calendar.json  written from the template
  charges.json   written from the template
  Python         3.13.1 OK
  Credentials    ANGEL_API_KEY set, ANGEL_CLIENT_CODE set, ANGEL_PIN set, ANGEL_TOTP_SECRET not set (login asks for the 6-digit code)
  To do          set account.capital_inr and api.risk_free_rate_pct in trader/config.json: the trading skills refuse to run until you do
  To do          calendar.json has no 2026 holidays: copy them from the NSE and BSE holiday circulars
  To do          charges.json is unverified: the probe checks it against the broker's estimateCharges
Keep trader/ out of git: it holds the day's session token.
Next: python in-setup/firstrun.py probe, in market hours (09:15 to 15:30 IST) for the live checks.
```

## Example: the offline replay

`probe --offline tests/fixtures/in-setup --now "2026-11-24 10:20" --key-label
historical --limits`. The fixtures are synthetic, shaped like the documented
replies, so this shows the report's shape, not real measurements. A replay is
saved but never counted as confirmed.

```text
Capability probe, Tue 2026-11-24 10:20 IST (offline replay; key historical)
  Not financial advice: this measures the data feed and says nothing about any trade.
  Clock           server +0.5 s from this computer (Date header, 1 s resolution): OK
  Master          46 rows; strike scale 100 (41 of 41 option rows agree); tick_size scale 100, inferred
  Segments        NFO 30, BFO 12, NSE 3, BSE 1
  NIFTY           index Nifty 50, token 99926000 (NSE)
                  options NFO, lot 65, strike step 50, freeze 1801; expiries Tue 2026-11-24, Tue 2026-12-01
  BANKNIFTY       index Nifty Bank, token 99926009 (NSE)
                  options NFO, lot 30, strike step 100, freeze 901; expiries Tue 2026-11-24, Tue 2026-12-29
  SENSEX          index SENSEX, token 99919000 (BSE)
                  options BFO, lot 20, strike step 100, freeze 1000; expiries Thu 2026-11-26, Thu 2026-12-03
  Market          open
  loginByPassword OK
  getProfile      OK
  getCandleData   OK, 6 five-minute bars, index volume all 0
  getOIData       OK, 4 OI points
  quote           OK, 2 fetched, 0 unfetched, snapshot age worst 2.0 s
  quote_options   OK, 2 fetched, 0 unfetched, snapshot age worst 1.0 s, FULL has OI, depth, circuits, feed time
  optionGreek     OK, 4 rows
  putCallRatio    OK, 2 rows
  estimateCharges OK, broker Rs 88.14 vs charges.json Rs 88.14 for one lot bought and sold: match
  Stream          7 ticks in 20 s; feed lag median 300 ms, worst 950 ms
                  exchange types nse_cm=1, nse_fo=2, bse_cm=3, bse_fo=4; pong 85 ms; paise scale OK against the REST quote
  Limits          quote burst: 5 calls in 0.00 s all accepted: more than 1 per second works; the design cap stays 1/s
                  token cap: not tested, only 28 NFO option tokens in the master, 997 needed
  Replay OK       loginByPassword, getProfile, getCandleData, getOIData, quote, quote_options, optionGreek, putCallRatio, estimateCharges, charges, stream, stream_nse_cm, stream_nse_fo, stream_bse_cm, stream_bse_fo
Wrote trader/state/capabilities.json (offline: saved, never counted as confirmed)
API usage: none (offline replay)
```

On a live run the first line says `live, Angel One SmartAPI`, the list is
headed `Confirmed`, anything missing is listed under `Not yet`, and the last
line is the day's API usage against the design caps.

## Rules

- Every REST call goes through the one shared client and limiter in
  `in-common/smart.py`, at about 70% of the documented limits.
- One login per IST day per API key, one attempt. A failed login is reported,
  never retried in a loop.
- Lot sizes, expiries, strike steps and freeze quantities are read from the
  master every day, never assumed. A weekday is never assumed for an expiry.
- A feature counts as confirmed only after a live run worked. An offline
  replay, a skipped check or a failure never counts.
- Report what was measured, what failed and what was inferred, in those words.

## What it never does

It never places, modifies or cancels an order: the code has no order function
and a test fails the build if any file names an order endpoint. It never
prints, logs or writes a credential, never asks for one in chat, never
scrapes a website, and never fills a gap with a guess.

## Safety

- Not financial advice. This skill measures a data feed; it says nothing
  about any trade.
- Analysis only: the pack never places an order. The user places and closes
  every order by hand in the Angel One app.
- Credentials live only in environment variables. `trader/` holds the day's
  session token: keep it out of git (the repo's `.gitignore` does).
- Personal, local use only.
