"""Backend: FastAPI (REST) + Socket.IO (live state push) around the trading engine."""

import asyncio
import hmac
import logging
import logging.handlers
import os
import re
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import socketio
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from app import config

os.makedirs(os.path.join(config.TRADER_DIR, "state"), exist_ok=True)
logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_fh = logging.handlers.RotatingFileHandler(os.path.join(config.TRADER_DIR, "state", "engine.log"),
                                           maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_fh)
log = logging.getLogger("backend")

from app import lorentzian, research, skills  # noqa: E402
from app.engine import EngineError, engine  # noqa: E402
from app.sp import smart  # noqa: E402
from app.strategies import INDICES, STOP_METHODS, STRATEGIES, ZERO_OK, strategy_cfg  # noqa: E402

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=config.CORS_ORIGINS)
PUSH_SEC = 2


async def push_loop():
    while True:
        try:
            await sio.emit("state", await run_in_threadpool(engine.snapshot))
        except Exception:
            log.exception("state push failed")
        await asyncio.sleep(PUSH_SEC)


@asynccontextmanager
async def lifespan(app):
    loop = asyncio.get_running_loop()
    engine.listeners.append(lambda ev, data: asyncio.run_coroutine_threadsafe(sio.emit(ev, data), loop))
    pusher = asyncio.create_task(push_loop())
    if config.AUTO_START and engine.store.get("desired_running", "1") == "1":
        try:
            await run_in_threadpool(engine.start)
        except EngineError as e:
            log.error("auto-start refused: %s", e)
    yield
    pusher.cancel()
    await run_in_threadpool(engine.stop, False)    # keep the user's choice for the next start


def authorized(token):
    """True when no APP_TOKEN is set (local only) or `token` matches it (constant-time compare)."""
    if not config.APP_TOKEN:
        return True
    return isinstance(token, str) and hmac.compare_digest(token.encode(), config.APP_TOKEN.encode())


api = FastAPI(title=config.APP_NAME, lifespan=lifespan)


@api.middleware("http")
async def require_token(request: Request, call_next):
    # everything but the health check (Render probes it) needs "Authorization: Bearer <APP_TOKEN>", /docs included
    header = request.headers.get("authorization", "")
    if request.url.path != "/health" and not authorized(header[7:] if header.startswith("Bearer ") else None):
        return JSONResponse({"detail": "access token missing or wrong"}, status_code=401)
    return await call_next(request)


# added after the token check so it wraps it: preflights pass and a 401 still carries CORS headers
api.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])
app = socketio.ASGIApp(sio, api, socketio_path="/ws")


@sio.event
async def connect(sid, environ, auth=None):
    if not authorized((auth or {}).get("token") if isinstance(auth, dict) else None):
        raise socketio.exceptions.ConnectionRefusedError("unauthorized")
    await sio.emit("state", await run_in_threadpool(engine.snapshot), to=sid)


@api.get("/health")
async def health():
    return {"ok": True, "engine_running": engine.running}


@api.get("/api/v1/state")
async def state():
    return await run_in_threadpool(engine.snapshot)


@api.post("/api/v1/engine/start")
async def start():
    try:
        await run_in_threadpool(engine.start)
    except EngineError as e:
        raise HTTPException(409, str(e))
    if not engine.running:
        raise HTTPException(500, "the engine thread did not start")
    return await run_in_threadpool(engine.snapshot)


@api.post("/api/v1/engine/stop")
async def stop():
    try:
        await run_in_threadpool(engine.stop)
    except EngineError as e:
        raise HTTPException(409, str(e))
    if engine.running:
        raise HTTPException(500, "the engine is still running")
    return await run_in_threadpool(engine.snapshot)


@api.post("/api/v1/trades/{trade_id}/close")
async def close_trade(trade_id: str):
    try:
        return await run_in_threadpool(engine.close_manual, trade_id)
    except EngineError as e:
        raise HTTPException(409, str(e))


@api.get("/api/v1/trades")
async def trades(date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"), status: Optional[str] = None):
    return engine.store.trades(date, status)


@api.get("/api/v1/signals")
async def signals(date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")):
    return engine.store.signals(date)


@api.get("/api/v1/stats/daily")
async def daily(days: int = Query(30, ge=1, le=365)):
    return engine.store.daily(days)


# ---------------------------------------------------------------- settings (trader/config.json)

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class Account(BaseModel):
    capital_inr: Optional[float] = Field(None, gt=0)
    risk_per_trade_pct: Optional[float] = Field(None, gt=0, le=10)
    max_daily_loss_pct: Optional[float] = Field(None, gt=0, le=50)
    max_trades_per_day: Optional[int] = Field(None, ge=1, le=50)
    max_consecutive_losses: Optional[int] = Field(None, ge=1, le=50)
    max_capital_per_trade_pct: Optional[float] = Field(None, gt=0, le=100)


class Session(BaseModel):
    entry_start: Optional[str] = Field(None, pattern=HHMM)
    entry_end: Optional[str] = Field(None, pattern=HHMM)
    flat_by: Optional[str] = Field(None, pattern=HHMM)
    expiry_day_flat_by: Optional[str] = Field(None, pattern=HHMM)


class Exit(BaseModel):
    max_premium_loss_pct: Optional[float] = Field(None, gt=0, lt=100)


class Paper(BaseModel):
    auto_execute: Optional[bool] = None
    symbols: Optional[List[str]] = None

    @field_validator("symbols")
    @classmethod
    def known(cls, v):
        bad = [s for s in v or [] if s.upper() not in ("NIFTY", "SENSEX", "BANKNIFTY")]
        if bad or (v is not None and not v):
            raise ValueError("symbols must be a non-empty subset of NIFTY, SENSEX, BANKNIFTY")
        return [s.upper() for s in v] if v else v


class SettingsIn(BaseModel):
    account: Optional[Account] = None
    session: Optional[Session] = None
    exit: Optional[Exit] = None
    paper: Optional[Paper] = None
    strategies: Optional[Dict[str, Dict[str, object]]] = None


def settings_view(cfg):
    return {"account": cfg.get("account") or {}, "session": cfg.get("session") or {}, "exit": cfg.get("exit") or {},
            "paper": {"auto_execute": True, "symbols": ["NIFTY", "SENSEX", "BANKNIFTY"], **(cfg.get("paper") or {})},
            "strategies": {k: {"name": v["name"], **strategy_cfg(cfg, k)} for k, v in STRATEGIES.items()}}


def clean_strategy(key, vals):
    """A strategy's settings checked against its defaults' types: 422 on anything unknown or out of range."""
    if key not in STRATEGIES:
        raise HTTPException(422, "unknown strategy %s" % key)
    defaults = STRATEGIES[key]["defaults"]
    clean = {}
    for k, v in (vals or {}).items():
        if k not in defaults:
            raise HTTPException(422, "%s has no setting %s" % (key, k))
        want = type(defaults[k])
        try:
            v = want(v) if want is not bool else bool(v)
        except (TypeError, ValueError):
            raise HTTPException(422, "%s.%s must be %s" % (key, k, want.__name__))
        lim = lorentzian.RANGES.get(k) if key == "LORENTZIAN" else None
        if lim:
            if not lim[0] <= v <= lim[1]:
                raise HTTPException(422, "%s.%s must be %g to %g" % (key, k, lim[0], lim[1]))
        elif want in (int, float) and (v < 0 if k in ZERO_OK else v <= 0):
            raise HTTPException(422, "%s.%s must be %s 0" % (key, k, "at least" if k in ZERO_OK else "above"))
        if k == "stop_method" and v not in STOP_METHODS:
            raise HTTPException(422, "%s.stop_method must be one of %s" % (key, ", ".join(STOP_METHODS)))
        if k == "start_after" and not re.match(HHMM, v):
            raise HTTPException(422, "%s.start_after must be HH:MM" % key)
        if k == "stop_rule" and v not in ("or-mid", "or-opposite"):
            raise HTTPException(422, "%s.stop_rule must be or-mid or or-opposite" % key)
        if key == "LORENTZIAN" and k in lorentzian.CHOICES:
            v = v.strip().lower() if k == "source" else v.strip().upper()
            if v not in lorentzian.CHOICES[k]:
                raise HTTPException(422, "%s.%s must be one of %s" % (key, k, ", ".join(lorentzian.CHOICES[k])))
        if k == "symbols":
            names = [x.strip().upper() for x in v.split(",") if x.strip()]
            if not names or any(x not in INDICES for x in names):
                raise HTTPException(422, "%s.symbols must be a comma-separated subset of %s" % (key, ", ".join(INDICES)))
            v = ",".join(names)
        clean[k] = v
    return clean


@api.get("/api/v1/settings")
async def get_settings():
    return settings_view(smart.load_config(config.TRADER_DIR))


@api.post("/api/v1/settings")
async def post_settings(body: SettingsIn):
    path = smart.trader_paths(config.TRADER_DIR)["config"]
    with smart.FileLock(path + ".lock"):
        cfg = smart.load_config(config.TRADER_DIR)
        for section in ("account", "session", "exit", "paper"):
            part = getattr(body, section)
            if part is not None:
                cfg[section] = {**(cfg.get(section) or {}), **part.model_dump(exclude_none=True)}
        s = cfg.get("session") or {}
        if not s.get("entry_start", "09:30") < s.get("entry_end", "14:00") <= s.get("flat_by", "15:10"):
            raise HTTPException(422, "need entry_start < entry cutoff <= flat_by")
        for key, vals in (body.strategies or {}).items():
            clean = clean_strategy(key, vals)
            cfg.setdefault("strategies", {})[key] = {**(cfg["strategies"].get(key) or {}), **clean}
        lc = strategy_cfg(cfg, "LORENTZIAN")
        if int(lc["kernel_lag"]) >= int(lc["kernel_lookback"]):
            raise HTTPException(422, "LORENTZIAN.kernel_lag must be below kernel_lookback (the Gaussian kernel's "
                                     "lookback is kernel_lookback - kernel_lag)")
        smart.write_json(path, cfg)
    log.info("settings updated: %s", ", ".join(k for k, v in body.model_dump(exclude_none=True).items() if v))
    return settings_view(cfg)


# ---------------------------------------------------------------- the skill pack, the time machine, the backtest

@api.get("/api/v1/skills")
async def skill_catalog():
    """Every runnable command, with the last run of each (its ok, reason and time)."""
    out = {}
    for skill, cmds in skills.catalog().items():
        out[skill] = {}
        for cmd, desc in cmds.items():
            last = skills.last(skill, cmd) or {}
            out[skill][cmd] = {"description": desc, "last": {"ok": last.get("ok"), "reason": last.get("reason"),
                                                             "started_ist": (last.get("data") or {}).get("started_ist")}
                               if last else None}
    return out


class SkillRun(BaseModel):
    args: Dict[str, object] = Field(default_factory=dict)


@api.post("/api/v1/skills/{skill}/{command}")
async def skill_run(skill: str, command: str, body: SkillRun):
    try:
        skills.argv_for(skill, command, body.args)        # 422 before anything runs
    except skills.SkillError as e:
        raise HTTPException(422, str(e))
    return await run_in_threadpool(skills.run, skill, command, body.args)


@api.get("/api/v1/skills/{skill}/{command}")
async def skill_last(skill: str, command: str):
    if command not in skills.COMMANDS.get(skill, {}):
        raise HTTPException(404, "unknown skill command")
    doc = skills.last(skill, command)
    if doc is None:
        raise HTTPException(404, "%s %s has not run yet" % (skill, command))
    return doc


@api.get("/api/v1/timemachine/{symbol}/{strategy}")
async def timemachine_last(symbol: str, strategy: str):
    """The engine's last pre-trade verdict for this index and strategy (state/timemachine/<S>_<STRATEGY>.json)."""
    if symbol.upper() not in ("NIFTY", "SENSEX", "BANKNIFTY") or strategy not in STRATEGIES:
        raise HTTPException(404, "unknown symbol or strategy")
    doc = smart.read_json(os.path.join(config.TRADER_DIR, "state", "timemachine",
                                       "%s_%s.json" % (symbol.upper(), strategy)))
    if doc is None:
        raise HTTPException(404, "no verdict yet: one is made at the first signal of the day")
    return doc


class LcBacktestIn(BaseModel):
    symbol: str = Field("NIFTY", pattern=r"^(NIFTY|SENSEX|BANKNIFTY)$")
    days: int = Field(40, ge=10, le=120)


# the backtest routes before /lorentzian/{symbol}: FastAPI matches in order
@api.post("/api/v1/lorentzian/backtest")
async def lc_backtest_start(body: LcBacktestIn):
    """Starts the four-configuration comparison in the background; GET the same path for its progress and result."""
    try:
        return engine.lc_backtest_start(body.symbol, body.days)
    except EngineError as e:
        raise HTTPException(409, str(e))


@api.get("/api/v1/lorentzian/backtest")
async def lc_backtest_status():
    return engine.lc_job or {"status": "idle"}


@api.get("/api/v1/lorentzian/{symbol}")
async def lorentzian_view(symbol: str, build: bool = False):
    """The Lorentzian tab: the live model's latest bar, today's bars and decisions; build=true builds it now."""
    if symbol.upper() not in INDICES:
        raise HTTPException(404, "unknown index %s" % symbol)
    try:
        return research.finite(await run_in_threadpool(engine.lc_view, symbol.upper(), build))
    except EngineError as e:
        raise HTTPException(409, str(e))


class BacktestIn(BaseModel):
    symbol: str = Field("NIFTY", pattern=r"^(NIFTY|SENSEX|BANKNIFTY)$")
    strategy: str = "ORB"
    days: int = Field(60, ge=20, le=250)
    overrides: Dict[str, object] = Field(default_factory=dict)


@api.post("/api/v1/backtest")
async def backtest(body: BacktestIn):
    overrides = clean_strategy(body.strategy, body.overrides)
    try:
        return await run_in_threadpool(engine.backtest, body.symbol, body.strategy, body.days, overrides)
    except EngineError as e:
        raise HTTPException(409, str(e))


class ResearchIn(BaseModel):
    days: int = Field(250, ge=60, le=250)
    symbols: List[str] = Field(default_factory=lambda: ["NIFTY", "BANKNIFTY", "SENSEX"])
    assumed_premium: Dict[str, float] = Field(default_factory=dict)
    option_slippage_pct: Optional[float] = Field(None, ge=0, le=10)
    stop_slippage_r: Optional[float] = Field(None, ge=0, le=1)

    @field_validator("symbols")
    @classmethod
    def known_symbols(cls, v):
        v = [s.upper() for s in v]
        if not v or any(s not in ("NIFTY", "SENSEX", "BANKNIFTY") for s in v):
            raise ValueError("symbols must be a non-empty subset of NIFTY, SENSEX, BANKNIFTY")
        return v

    @field_validator("assumed_premium")
    @classmethod
    def premiums(cls, v):
        if any(k.upper() not in ("NIFTY", "SENSEX", "BANKNIFTY") or not 1 <= x <= 5000 for k, x in v.items()):
            raise ValueError("assumed_premium wants {NIFTY|SENSEX|BANKNIFTY: 1..5000}")
        return {k.upper(): x for k, x in v.items()}


class SweepIn(ResearchIn):
    version: str = Field("EMA_ORB_C", pattern=r"^EMA_ORB_[BC]$")


@api.post("/api/v1/backtest/compare")
async def backtest_compare(body: ResearchIn):
    """The four strategy versions on each index and combined, with costs, realistic fills and a
    50 / 25 / 25 chronological train / validation / test split."""
    try:
        return await run_in_threadpool(engine.backtest_compare, body.days, body.symbols, body.assumed_premium,
                                       body.option_slippage_pct, body.stop_slippage_r)
    except EngineError as e:
        raise HTTPException(409, str(e))


@api.post("/api/v1/backtest/sweep")
async def backtest_sweep(body: SweepIn):
    """Targets 1R..3R x stop methods for the hybrid, chosen on train, confirmed on validation, test shown for
    the chosen setting only."""
    try:
        return await run_in_threadpool(engine.backtest_sweep, body.days, body.symbols, body.version,
                                       body.assumed_premium, body.option_slippage_pct, body.stop_slippage_r)
    except EngineError as e:
        raise HTTPException(409, str(e))
