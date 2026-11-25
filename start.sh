#!/bin/bash
set -e

echo "========================================"
echo "  Indian Day Trader UI - Startup"
echo "========================================"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI_DIR="$SCRIPT_DIR"
BACKEND_DIR="$UI_DIR/backend"
FRONTEND_DIR="$UI_DIR/frontend"

# Check backend .env
if [ ! -f "$BACKEND_DIR/.env" ]; then
  echo "Error: backend/.env not found"
  exit 1
fi

# Load only the env vars the script needs (avoid CORS_ORIGINS brackets issue)
export HOST=$(grep '^HOST=' "$BACKEND_DIR/.env" | cut -d= -f2-)
export PORT=$(grep '^PORT=' "$BACKEND_DIR/.env" | cut -d= -f2-)
export SKILL_PACK_ROOT=$(grep '^SKILL_PACK_ROOT=' "$BACKEND_DIR/.env" | cut -d= -f2-)
export TRADER_DIR=$(grep '^TRADER_DIR=' "$BACKEND_DIR/.env" | cut -d= -f2-)

# Install backend deps if needed
if [ ! -d "$BACKEND_DIR/venv" ]; then
  echo "Creating backend virtualenv..."
  python3 -m venv "$BACKEND_DIR/venv"
  "$BACKEND_DIR/venv/bin/pip" install -r "$BACKEND_DIR/requirements.txt"
fi

# Start backend
echo "Starting backend on port $PORT..."
cd "$BACKEND_DIR"
source venv/bin/activate
python -m uvicorn app.main:app --host "$HOST" --port "$PORT" &
BACKEND_PID=$!
cd "$UI_DIR"

# Wait for backend
echo "Waiting for backend..."
for i in $(seq 1 30); do
  if curl -s http://localhost:$PORT/health > /dev/null 2>&1; then
    echo "Backend ready!"
    break
  fi
  sleep 1
done

# Install frontend deps
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "Installing frontend dependencies..."
  cd "$FRONTEND_DIR"
  npm install
  cd "$UI_DIR"
fi

# Start frontend
echo "Starting frontend on port 5173..."
cd "$FRONTEND_DIR"
npm run dev &
FRONTEND_PID=$!
cd "$UI_DIR"

echo ""
echo "========================================"
echo "  UI Running!"
echo "  Frontend: http://localhost:5173"
echo "  Backend:  http://localhost:$PORT"
echo "  Docs:     http://localhost:$PORT/docs"
echo "========================================"
echo "Press Ctrl+C to stop"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" INT
wait $BACKEND_PID $FRONTEND_PID
