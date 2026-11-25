# Indian Day Trader

Indian Day Trader UI - Live ORB Signal & Auto Paper Trading System for NIFTY, SENSEX, BANKNIFTY options.

## Structure

```
indian-day-trader/
├── backend/              # FastAPI + Socket.IO backend
│   ├── app/
│   │   ├── main.py       # App entrypoint (lifespan, Socket.IO, routes)
│   │   ├── config.py     # Environment config (relative paths)
│   │   └── skillpack/
│   │       └── runner.py # SkillPackRunner + AutoTrader engine
│   ├── .env              # Environment variables (gitignored - contains secrets)
│   └── requirements.txt  # Python dependencies
├── frontend/             # React + Vite + Zustand UI
│   ├── src/
│   │   ├── pages/        # Dashboard, Signals, Positions, Settings
│   │   ├── store/        # Zustand trader store
│   │   ├── services/     # API + Socket.IO clients
│   │   └── App.tsx       # Router + Socket connection
│   └── package.json
├── skillpack/            # Trading skill pack scripts
│   ├── in-common/        # Shared SmartAPI client
│   ├── in-entry/         # ORB breakout trigger detection
│   ├── in-chain/         # Option instrument selection
│   ├── in-exit/          # Stop-loss & target planning
│   ├── in-budget/        # Position sizing
│   ├── in-risk/          # Risk validation
│   ├── in-scanner/       # Market scanner
│   ├── in-news/          # News monitor
│   └── ...
├── trader/               # Runtime state (gitignored)
│   ├── state/            # positions.json, session.json, capabilities.json
│   ├── config.json       # Trading configuration
│   └── ...
├── start.sh              # Start backend + frontend
├── trader/signal_runner.py   # Standalone signal pipeline runner
└── trader/execute_paper_trade.py  # Manual paper trade executor
```

## Key Features

- **ORB (Opening Range Breakout)** signal detection on NIFTY, SENSEX, BANKNIFTY
- **Auto paper trading** with risk checks (max loss per trade, daily loss limit, trades per day)
- **Live WebSocket** updates from backend to frontend
- **Optimistic UI** Start/Stop button with loading states and error revert
- **Settings page** loads from backend on mount

## Paths

All paths are **relative** — the repo works on any system without modification:
- `SKILL_PACK_ROOT` defaults to `./skillpack`
- `TRADER_DIR` defaults to `./trader`
- Backend `PROJECT_ROOT` computed from file location

## Quick Start

```bash
# Clone
git clone https://github.com/Pankajjangid01/India_trade_skill.git
cd India_trade_skill

# Backend
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Copy .env example and add your Angel One API credentials
cp .env.example .env  # create .env with ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, ANGEL_TOTP_SECRET

# Start backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Frontend (new terminal)
cd ../frontend
npm install
npm run dev

# Or use start.sh
./start.sh
```

## Environment Variables (.env)

```env
ANGEL_API_KEY=your_api_key
ANGEL_CLIENT_CODE=your_client_code
ANGEL_PIN=your_pin
ANGEL_TOTP_SECRET=your_totp_secret
PORT=8000
HOST=0.0.0.0
```

## Market Hours

- Market: 09:00–15:30 IST (Mon–Fri)
- Entry window: 09:30–14:00 IST
- Flat by: 15:10 IST

## License

MIT