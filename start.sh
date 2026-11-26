#!/bin/bash
# Starts the backend (engine auto-starts) and the UI. Works in Git Bash on Windows, macOS and Linux.
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
PORT="${PORT:-8000}"

if [ ! -f "$BACKEND/.env" ]; then
  echo "backend/.env not found: create it with ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, ANGEL_TOTP_SECRET"
  exit 1
fi
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
if [ ! -d "$BACKEND/venv" ]; then
  echo "Creating backend virtualenv..."
  "$PY" -m venv "$BACKEND/venv"
fi
VPY="$BACKEND/venv/bin/python"; [ -x "$VPY" ] || VPY="$BACKEND/venv/Scripts/python.exe"
"$VPY" -m pip install -q -r "$BACKEND/requirements.txt"
[ -d "$FRONTEND/node_modules" ] || (cd "$FRONTEND" && npm install)

(cd "$BACKEND" && "$VPY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT") &
BACKEND_PID=$!
for i in $(seq 1 30); do curl -s "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
(cd "$FRONTEND" && npx vite --port 5173) &
FRONTEND_PID=$!

echo "UI: http://localhost:5173   API: http://127.0.0.1:$PORT/docs   logs: trader/state/engine.log"
trap 'kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0' INT TERM
wait $BACKEND_PID $FRONTEND_PID
