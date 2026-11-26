import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
# backend/.env holds the ANGEL_* credentials; os.environ wins over it. Never logged, never sent to the UI.
load_dotenv(BACKEND_DIR / ".env")


def _bool(key, default):
    return os.environ.get(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


APP_NAME = os.environ.get("APP_NAME", "Indian Day Trader")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8000))
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
SKILL_PACK_ROOT = str(Path(os.environ.get("SKILL_PACK_ROOT") or PROJECT_ROOT / "skillpack").resolve())
TRADER_DIR = str(Path(os.environ.get("TRADER_DIR") or PROJECT_ROOT / "trader").resolve())
# start the engine with the backend unless the user's last action was Stop
AUTO_START = _bool("AUTO_START", True)
# Every /api call and Socket.IO connection must carry this token. Empty means open: only for a backend
# bound to 127.0.0.1 on your own machine. On Render (which sets RENDER) the backend refuses to start open.
APP_TOKEN = os.environ.get("APP_TOKEN", "").strip()
if os.environ.get("RENDER") and len(APP_TOKEN) < 24:
    raise SystemExit("APP_TOKEN (24+ characters) must be set on the server, or anyone could use the API")
