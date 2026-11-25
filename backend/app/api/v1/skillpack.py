"""Skill Pack API endpoints."""

import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from typing import Dict, Any, List, Optional
from pydantic import BaseModel

from app.skillpack.runner import runner, auto_trader, _is_market_hours, _now_ist

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/skillpack", tags=["skillpack"])


class SettingsUpdate(BaseModel):
    capital_inr: Optional[float] = None
    risk_per_trade_pct: Optional[float] = None
    max_daily_loss_pct: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    max_consecutive_losses: Optional[int] = None
    max_capital_per_trade_pct: Optional[float] = None
    entry_start: Optional[str] = None
    entry_end: Optional[str] = None
    flat_by: Optional[str] = None
    expiry_day_flat_by: Optional[str] = None
    stop_rule: Optional[str] = None
    target_r: Optional[float] = None
    max_premium_loss_pct: Optional[float] = None
    no_progress_bars: Optional[int] = None
    paper_trading_enabled: Optional[bool] = None
    auto_execute: Optional[bool] = None
    symbols: Optional[List[str]] = None


class SkillRunRequest(BaseModel):
    skill: str
    command: str
    args: Optional[List[str]] = None


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for conn in list(self.active_connections):
            try:
                await conn.send_json(message)
            except Exception:
                self.disconnect(conn)


manager = ConnectionManager()


@router.get("/state")
async def get_state():
    return {"success": True, "data": auto_trader.get_state()}


@router.get("/status")
async def get_status():
    return {
        "success": True,
        "data": {
            "running": auto_trader._running,
            "market_hours": _is_market_hours(),
            "timestamp": _now_ist().isoformat(),
            "positions_count": len(auto_trader.paper_positions),
            "active_positions": len([p for p in auto_trader.paper_positions if p["status"] == "PAPER_ACTIVE"]),
            "daily_pnl": float(auto_trader.daily_pnl),
            "consecutive_losses": auto_trader.consecutive_losses,
        },
    }


@router.post("/start")
async def start_auto_trader():
    await auto_trader.start()
    return {"success": True, "message": "Auto-trader started", "running": auto_trader._running}


@router.post("/stop")
async def stop_auto_trader():
    await auto_trader.stop()
    return {"success": True, "message": "Auto-trader stopped", "running": auto_trader._running}


@router.post("/settings")
async def update_settings(update: SettingsUpdate):
    data = update.model_dump(exclude_unset=True)
    auto_trader.update_settings(data)
    return {"success": True, "data": auto_trader.settings, "message": "Settings updated"}


@router.get("/settings")
async def get_settings():
    return {"success": True, "data": auto_trader.settings}


@router.post("/run-skill")
async def run_skill(request: SkillRunRequest):
    result = auto_trader.runner.run_skill(request.skill, request.command, request.args)
    return {"success": result.get("success", False), "data": result}


@router.get("/signal")
async def get_last_signal():
    return {"success": True, "data": auto_trader.last_signal}


@router.get("/scan")
async def get_last_scan():
    return {"success": True, "data": auto_trader.last_scan}


@router.post("/positions/{position_id}/close")
async def close_position(position_id: str):
    for pos in auto_trader.paper_positions:
        if pos["id"] == position_id and pos["status"] == "PAPER_ACTIVE":
            pos["status"] = "CLOSED"
            pos["exit_time"] = _now_ist().isoformat()
            pos["exit_reason"] = "MANUAL_CLOSE"
            pos["exit_price"] = pos["entry_price"]
            await manager.broadcast({"event": "position_closed", "data": pos})
            return {"success": True, "data": pos}
    raise HTTPException(status_code=404, detail="Position not found or not active")


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_json({"event": "state", "data": auto_trader.get_state()})
        while True:
            data = await websocket.receive_json()
            if data.get("action") == "get_state":
                await websocket.send_json({"event": "state", "data": auto_trader.get_state()})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
