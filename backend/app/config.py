import os
from dotenv import load_dotenv
from pathlib import Path

# Load .env from backend folder
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

# Project root (two levels up from backend/app/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def get_env(key: str, default: str = None) -> str:
    return os.environ.get(key, default)


def get_env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


def get_env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def get_env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def get_env_list(key: str, default: list = None) -> list:
    val = os.environ.get(key)
    if not val:
        return default or []
    return [v.strip() for v in val.split(",") if v.strip()]


# App
APP_NAME = get_env("APP_NAME", "Indian Day Trader UI")
APP_VERSION = get_env("APP_VERSION", "1.0.0")
DEBUG = get_env_bool("DEBUG", True)
ENVIRONMENT = get_env("ENVIRONMENT", "development")
LOG_LEVEL = get_env("LOG_LEVEL", "INFO")

# Server
HOST = get_env("HOST", "0.0.0.0")
PORT = get_env_int("PORT", 8000)
API_PREFIX = get_env("API_PREFIX", "/api/v1")
CORS_ORIGINS = get_env_list("CORS_ORIGINS", ["http://localhost:5173"])

# Skill Pack
SKILL_PACK_ROOT = get_env("SKILL_PACK_ROOT", str(PROJECT_ROOT / "skillpack"))
TRADER_DIR = get_env("TRADER_DIR", str(PROJECT_ROOT / "trader"))

# Market Hours
MARKET_OPEN = get_env("MARKET_OPEN", "09:00")
MARKET_CLOSE = get_env("MARKET_CLOSE", "15:30")
IST_TIMEZONE = get_env("IST_TIMEZONE", "Asia/Kolkata")

# Defaults
DEFAULT_CAPITAL = get_env_float("DEFAULT_CAPITAL", 500000)
DEFAULT_RISK_PCT = get_env_float("DEFAULT_RISK_PCT", 1.0)
DEFAULT_MAX_DAILY_LOSS_PCT = get_env_float("DEFAULT_MAX_DAILY_LOSS_PCT", 2.0)
DEFAULT_MAX_TRADES_PER_DAY = get_env_int("DEFAULT_MAX_TRADES_PER_DAY", 3)
DEFAULT_MAX_CONSECUTIVE_LOSSES = get_env_int("DEFAULT_MAX_CONSECUTIVE_LOSSES", 3)
DEFAULT_MAX_CAPITAL_PCT = get_env_float("DEFAULT_MAX_CAPITAL_PCT", 30)
DEFAULT_STOP_RULE = get_env("DEFAULT_STOP_RULE", "or-mid")
DEFAULT_TARGET_R = get_env_float("DEFAULT_TARGET_R", 2.0)
DEFAULT_MAX_PREMIUM_LOSS_PCT = get_env_float("DEFAULT_MAX_PREMIUM_LOSS_PCT", 30)
DEFAULT_NO_PROGRESS_BARS = get_env_int("DEFAULT_NO_PROGRESS_BARS", 6)
PAPER_TRADING_ENABLED = get_env_bool("PAPER_TRADING_ENABLED", True)
AUTO_EXECUTE = get_env_bool("AUTO_EXECUTE", True)
SYMBOLS = get_env_list("SYMBOLS", ["NIFTY", "SENSEX", "BANKNIFTY"])
