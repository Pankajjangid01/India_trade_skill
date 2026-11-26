# Indian Day Trader

An automated intraday system for NIFTY, SENSEX and BANKNIFTY index options on live
Angel One SmartAPI data. Two independent strategies (Opening Range Breakout and EMA
Trend Pullback) generate signals all session; valid signals become **paper trades**
automatically, with stops, targets, charges and P&L tracked per strategy.

> **Paper trading only. It never places a real order.** The skill pack has no order
> function at all, and `skillpack/tests/test_repo.py` fails the build if any file
> names an order endpoint. Not financial advice.

## Structure

```
backend/                FastAPI + Socket.IO, the engine
  app/main.py           REST API, Socket.IO state push every 2 s, settings validation
  app/engine.py         the engine thread: lifecycle, login, feed supervisor, scan, paper trades
  app/strategies.py     ORB and EMA_PULLBACK as pure functions over closed 5-minute bars
  app/paper.py          entry gate (14:00 cutoff, limits), premium plan, exits, P&L with charges
  app/store.py          SQLite: signals, trades, per-strategy daily stats (never deleted)
  app/sp.py             imports the skill pack in-process
  tests/test_logic.py   offline tests of the rules above + a simulated trading day
frontend/               React + Vite + Tailwind + Zustand; renders backend state only
skillpack/              the Indian Day Trader Skill Pack (stdlib only): SmartAPI client,
                        session, rate limiter, live feed, option chain/pick, sizing, charges
trader/                 runtime folder (git-ignored except config.json): session, tick store,
                        paper.sqlite, caches, logs
```

## Run it

1. Credentials: create `backend/.env` (git-ignored) with
   `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET`.
2. Once, in market hours (09:15–15:30 IST), confirm what the account can do:
   ```bash
   cd backend
   python ../skillpack/in-setup/firstrun.py probe --key-label main --trader ../trader
   ```
   It writes `trader/state/capabilities.json`; the feed and option pick refuse features it has not confirmed.
3. Start everything: `./start.sh` (Git Bash on Windows, macOS, Linux), or by hand:
   ```bash
   cd backend && python -m venv venv && venv/Scripts/pip install -r requirements.txt   # venv/bin on macOS/Linux
   venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   cd frontend && npm install && npx vite          # UI on http://localhost:5173
   ```

The engine starts with the backend (unless your last action was **Stop**; `AUTO_START=false`
disables it) and then runs every trading day by itself.

## Deploy (Render + Vercel)

The backend, engine and feed run as one Render web service with a disk for `trader/`
([render.yaml](render.yaml)); the UI is static on Vercel and talks to Render directly. Only someone
with the access token can use either: the backend refuses every call and live connection without it,
and on Render it refuses to start without one.

1. **Render**: New → Blueprint → this repo. Fill in `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`,
   `ANGEL_PIN`, `ANGEL_TOTP_SECRET`; leave `CORS_ORIGINS` for step 3. Render generates `APP_TOKEN`
   (Environment tab: that is the token you type into the UI). Starter plan + 1 GB disk, Singapore.
2. **Once, in market hours**, in the service's Shell tab, then restart the service:
   `python skillpack/in-setup/firstrun.py probe --key-label main --trader $TRADER_DIR`
3. **Vercel**: Add New → Project → this repo, root directory `frontend` (Vite). Set
   `VITE_API_URL=https://<service>.onrender.com` and deploy. Put the Vercel address
   (`https://<project>.vercel.app`) in Render's `CORS_ORIGINS` and redeploy Render.
4. Open the Vercel address and enter the token. **Lock** (top right) forgets it in that browser.

Deploys restart the engine and auto-deploy is off: deploy by hand, outside 09:00–15:45 IST. Anyone
who can open the Render dashboard can read the Angel keys and the token, so keep that account to
the people who should trade it.

## Lifecycle

| | what happens |
| --- | --- |
| **Start** | takes an OS file lock (one engine per `trader/` folder: a second backend cannot start a second engine), starts the engine thread, remembers "running" for the next restart |
| **Running** | every 3 s: 09:00–15:45 IST on trading days it logs in once per day (session reuse, token refresh, re-login), keeps the skill pack's live feed (`in-common/feed.py`, the only WebSocket) running and restarts it with backoff if it dies, subscribes the indices and each open trade's option, reads prices from the feed's tick store (REST quote fallback for a stale tick), manages open trades; 09:15–15:30 it runs both strategies on each newly closed 5-minute bar. Outside those hours it idles and resumes by itself |
| **Stop** | ends new signal processing, joins the thread (the API answers only after it has stopped), stops the feed. Open trades stay OPEN in the database |
| **Restart / crash** | trades and signals are in `trader/state/paper.sqlite`; open trades are managed again; a feed left running by a killed backend is adopted (not duplicated) and stopped on the next Stop; signal ids are deterministic, so re-detected signals are never recorded or traded twice; a trade left open from an earlier day is closed at its last price as `SESSION_END_UNMONITORED` |

Errors in one cycle are logged (`trader/state/engine.log`) and shown on the dashboard; the next
cycle runs normally. The skill pack client handles rate limits (a shared limiter at ~70% of the
documented caps, retries with backoff on 403/5xx, a circuit breaker) and expired sessions.

## Strategies

Both run on **closed** 5-minute index bars only: the bar in progress never reaches a strategy,
and malformed bars (non-positive prices, high below open/close, duplicates) are dropped.
Bars come from the live feed, or from `getCandleData` when the feed started after 09:15.

### ORB — Opening Range Breakout
- **Range:** high and low of the 09:15, 09:20 and 09:25 bars (`session.range_end` 09:30).
- **Entry:** the first bar after the range that **closes** above the high (bullish → buy CE) or
  below the low (bearish → buy PE). A close equal to an edge is not a breakout. One signal per
  direction per day.
- **Stop (index):** `or-mid`, the range midpoint (or `or-opposite`, the other edge).
- **Target (index):** entry ± `target_r` (2.0) × risk.

### EMA_PULLBACK — EMA Trend Pullback (complements ORB)
ORB fires once per direction around the open; this one finds continuation entries in an
established trend later in the day, where ORB is silent.
- **Trend:** EMA(9) above EMA(21) and EMA(21) rising over 3 bars (mirror for bearish). EMAs are
  seeded with the previous trading day's bars; if those cannot be fetched the strategy waits
  rather than signal on a different seed.
- **Pullback:** within the previous 3 bars a low touched EMA(9) while every close held above EMA(21).
- **Entry:** from 09:45, the bar that closes above the previous bar's high and above EMA(9)
  (mirror for bearish).
- **Stop (index):** the pullback swing low (high for bearish). Risk must be 0.05–0.6% of price.
- **Target (index):** entry ± 2 × risk. After a signal the same direction waits 6 bars.

### OI_PCR — Intraday OI Shift + PCR Confirmation (off by default, NIFTY only by default)
It waits for option writers to give up a level, instead of guessing direction from price alone.
- **Data:** after every closed bar the engine takes one option-chain record (in-chain's snapshot,
  ±20 strikes of the current expiry: OI per strike, PCR = put OI ÷ call OI of those strikes), kept
  in `trader/state/oi/<INDEX>_<date>.jsonl` so a restart keeps the day's battleground.
- **Battleground (09:15–09:30):** from the 09:30 record, the **call wall** = the strike with the
  most call OI (resistance) and the **put wall** = the most put OI (support).
- **Bullish:** a bar closes at or above the call wall (`near_pct` 0.1% below counts), call OI at the
  wall is **down** at least 1% since 09:30 (call writers covering), put OI at the wall or one strike
  below is **up** at least 5% (put writers stepping in), and PCR is above 1.10 and higher than at
  the previous bar. **Bearish** mirrored at the put wall: put OI unwinds, call OI surges, PCR under
  0.85 and falling.
- **No trade:** a wall reached while its writers keep adding OI (they defend it: the range-bound
  trap) — recorded as a rejected setup with the reason, shown on the dashboard.
- **Stop (index):** the swing low (high for bearish) of the last 3 bars; risk 0.05–0.6% of price.
  **Target:** entry ± 2 × risk. One signal per direction per day.
- **Expiry day:** no signal once the chain rolls to the next expiry
  (`session.expiry_day_last_entry`): its OI is not comparable with the 09:30 battleground.
- **Not backtestable:** Angel One keeps no per-strike OI history, so the time machine and the
  backtest cannot replay it (they say so; with `timemachine.required` false it does not block).
  Its live paper trades build its record. It buys one option (the in-chain pick, delta 0.35–0.55)
  like the others; the debit-spread variant is not modelled.

### LORENTZIAN — Machine Learning: Lorentzian Classification (off by default, own tab)
A standalone port of jdehorty's TradingView indicator (`backend/app/lorentzian.py`), on closed 5-minute
bars, with its five features (RSI 14,1 · WaveTrend 10,11 · CCI 20,1 · ADX 20,2 · RSI 9,1), Lorentzian
distance `Σ ln(1 + |xᵢ − yᵢ|)`, the approximate nearest-neighbor search (k = 8, 2000 bars back), the
volatility / regime (−0.1) / ADX / EMA 200 / SMA 200 filters, Nadaraya-Watson kernel confirmation
(8, 8, 25, lag 2) and the fixed 4-bar or dynamic kernel exits. The source's **expressions** are followed
where its comments differ: the training label is the past 4-bar move inverted (`src[4] < src[0]` → short),
neighbors are the first `max_bars_back` bars with `i % 4 != 0`, the buffers persist across bars, and the
"held < 4 bars" exit branch can never fire. Normalization uses the running min/max (no look-ahead); a test
checks the neighbor search against a line-by-line transcription of the Pine loop.
- **Data:** every trading day the model is built as if the chart were loaded at the open with exactly
  2000 closed bars of history (~27 days), then extended bar by bar. The backtest rebuilds it the same way
  for every day, so live and backtest signals are the same calculation. Source: hlc3.
- **Volume (added rules, outside the original logic):** index candles have no volume, so it is the
  near-month index futures' 5-minute volume. RVOL = volume ÷ SMA(volume, 20). The volume filter (on)
  confirms an entry when RVOL ≥ 1.2 on a futures candle in the trade's direction; an optional sixth ML
  feature (off) is the normalized RVOL. Rejections are shown with their reason.
- **Paper trade:** a confirmed entry goes through the same gate, in-chain pick, in-exit, in-budget and
  in-risk as the others. The indicator has no stop, so the index stop is the 4-bar swing (kept within
  0.05–0.6% of price) with a 3R target; its own exit (4 bars, or a flip, or the kernel with dynamic exits)
  closes the trade at that bar's close if the stops have not.
- **Tab (Lorentzian ML):** prediction score, neighbors, kernel, filtered signal, entry/exit, feature values,
  every filter's status, volume, a chart coloured by the prediction with the kernel line, and the
  four-configuration backtest (no volume filter / volume filter / RVOL feature / both) by
  train-validation-test period. A difference is only called with ≥ 30 trades in each out-of-sample period.
  The futures contract limits volume history to its listing (~2–3 months).
- **Speed:** numpy computes the distances (about 2 million bar pairs a day; a day's build takes ~0.6 s).

### Shared trade rules (all strategies)
- **Option:** `in-chain pick` — nearest expiry, |delta| 0.35–0.55, spread ≤ 3%, volume and OI
  minimums, one lot within the capital cap. Paper entry at the **ask** (realistic fill).
- **Premium stop / target:** 30% (`exit.max_premium_loss_pct`) under the entry; target `target_r` × that risk.
- **Exit:** whichever comes first of premium stop/target, index stop/target, or the flat-by time
  (15:10; 14:45 on the option's expiry day). Exit at the live option LTP.
- **Size:** `in-budget` — lots so that the loss at the premium stop plus round-trip charges stays
  within `risk_per_trade_pct` of capital, split at the exchange freeze limit.
- **P&L:** always (exit − entry) × qty for a bought option, minus brokerage, exchange, SEBI, STT,
  stamp duty and GST from `trader/charges.json` (checked against the broker's calculator by the probe).

## Signals and the 14:00 entry cutoff

Signals are generated and recorded all session. The **backend** decides each one's status:

| status | meaning |
| --- | --- |
| Executed | passed every check; a paper trade was opened |
| Executable | valid, but auto-execute is off in Settings |
| Entry Cutoff | generated at or after `session.entry_end` (14:00 IST): recorded, no trade |
| Rejected | before entry start, stale (bar closed > 150 s before the engine saw it), a conflicting or duplicate open trade on the same index, max trades (total 3, per strategy 2), daily loss limit (2%, realised), 3 losses in a row, index already through the stop, no option passed the pick rules, or one lot over the max loss |

Open trades are **not** closed at 14:00: stops, targets and flat-by keep working. Every time is
Asia/Kolkata, never the server's local zone.

## Performance

Per strategy, per IST date (history is kept; a new day starts from zero): signals, executable,
entry-cutoff, rejected, trades, open, wins, losses, net realised P&L, charges, best/worst.
**Win rate = winning completed trades ÷ completed trades × 100**; open trades and signals without
a trade are excluded. A trade with net P&L ≤ 0 (after charges) counts as a loss.

## API

`GET /api/v1/state` (the full snapshot, also pushed on Socket.IO `state` every 2 s, path `/ws`),
`POST /api/v1/engine/start|stop`, `POST /api/v1/trades/{id}/close`, `GET /api/v1/trades?date=`,
`GET /api/v1/signals?date=`, `GET /api/v1/stats/daily?days=`, `GET|POST /api/v1/settings`.
Socket.IO events `signal`, `trade_opened`, `trade_closed`, `engine` drive notifications.
With `APP_TOKEN` set, every route but `/health` needs `Authorization: Bearer <APP_TOKEN>` and Socket.IO
needs `auth: {token}`; without it the API is open, so only run it that way bound to 127.0.0.1.

## Tests

```bash
cd backend && venv/Scripts/python -m unittest discover tests -v     # engine rules + a simulated day
cd skillpack && python -m unittest discover tests                    # the skill pack's 273 tests
```

## Configuration

`trader/config.json` (editable on the Settings page): `account` limits, `session` times
(`entry_end` is the entry cutoff), `exit.max_premium_loss_pct`, `paper.auto_execute` and
`paper.symbols`, and `strategies.ORB` / `strategies.EMA_PULLBACK` parameters. `trader/calendar.json`
lists exchange holidays: fill it from the NSE/BSE circulars (the pack never guesses a holiday).
