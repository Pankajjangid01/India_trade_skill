"""Skill Pack Auto-Runner - Runs Indian Day Trader Skill Pack skills automatically."""

import asyncio
import datetime
import json
import logging
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Project root (backend/app/skillpack/runner.py -> indian-day-trader)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
IST = datetime.timezone(IST_OFFSET, "IST")

MARKET_OPEN = "09:00"
MARKET_CLOSE = "15:30"


def _now_ist() -> datetime.datetime:
    return datetime.datetime.now(IST)


def _today_ist() -> str:
    return _now_ist().date().isoformat()


def _is_market_hours(now: Optional[datetime.datetime] = None) -> bool:
    now = now or _now_ist()
    hhmm = now.strftime("%H:%M")
    return MARKET_OPEN <= hhmm <= MARKET_CLOSE and now.weekday() < 5


class SkillPackRunner:
    """Runs skill pack Python scripts as subprocesses."""

    def __init__(self, skill_pack_root: str, trader_dir: str):
        self.skill_pack_root = skill_pack_root
        self.trader_dir = trader_dir
        os.makedirs(self.trader_dir, exist_ok=True)
        os.makedirs(os.path.join(self.trader_dir, "state"), exist_ok=True)
        os.makedirs(os.path.join(self.trader_dir, "cache"), exist_ok=True)

    SCRIPT_MAP = {
        "in-setup": "firstrun.py",
        "in-scanner": "scan.py",
        "in-chain": "chain.py",
        "in-news": "news.py",
        "in-entry": "entry.py",
        "in-exit": "exit.py",
        "in-budget": "size.py",
        "in-risk": "check.py",
        "in-ticket": "ticket.py",
        "in-alerts": "alerts.py",
        "in-timemachine": "timemachine.py",
        "in-flows": "flows.py",
        "in-analyst": "facts.py",
    }

    def run_skill(
        self,
        skill: str,
        command: str,
        args: List[str] = None,
        now_str: str = None,
    ) -> Dict[str, Any]:
        script_name = self.SCRIPT_MAP.get(skill)
        if not script_name:
            return {"error": f"Unknown skill: {skill}", "success": False}

        script_path = os.path.join(self.skill_pack_root, skill, script_name)
        if not os.path.exists(script_path):
            return {"error": f"Script not found: {script_path}", "success": False}

        # Skill pack scripts use subcommands: <script> <cmd> [args] [--trader DIR] [--now TIME]
        cmd = [sys.executable, script_path, command]
        if args:
            cmd += args
        cmd += ["--trader", self.trader_dir]
        if now_str:
            cmd += ["--now", now_str]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "PYTHONPATH": self.skill_pack_root},
            )
            output = result.stdout.strip()
            error = result.stderr.strip()

            parsed = None
            if output:
                try:
                    parsed = json.loads(output)
                except json.JSONDecodeError:
                    for line in reversed(output.splitlines()):
                        line = line.strip()
                        if line.startswith("{") and line.endswith("}"):
                            try:
                                parsed = json.loads(line)
                                break
                            except json.JSONDecodeError:
                                continue

            return {
                "returncode": result.returncode,
                "stdout": output,
                "stderr": error,
                "json": parsed,
                "success": result.returncode == 0,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Script timed out: {script_path}", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}

    def read_json_file(self, filename: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(self.trader_dir, filename)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None


class AutoTrader:
    """Auto paper-trading engine that executes the full skill pipeline on a schedule."""

    def __init__(self, runner: SkillPackRunner, sio=None):
        self.runner = runner
        self.sio = sio
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.paper_positions: List[Dict[str, Any]] = []
        self.paper_orders: List[Dict[str, Any]] = []
        self.daily_pnl = Decimal("0")
        self.consecutive_losses = 0
        self.settings: Dict[str, Any] = self._default_settings()
        self.last_signal: Optional[Dict[str, Any]] = None
        self.last_scan: Optional[Dict[str, Any]] = None
        self._trade_id_counter = 0
        self._last_ltp: Dict[str, float] = {}
        self._load_positions_from_file()

    def _load_positions_from_file(self):
        import json as _json
        path = os.path.join(self.runner.trader_dir, "state", "positions.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.paper_positions = _json.load(f)
            except Exception:
                pass

    def _default_settings(self) -> Dict[str, Any]:
        return {
            "capital_inr": 500000,
            "risk_per_trade_pct": 1.0,
            "max_daily_loss_pct": 2.0,
            "max_trades_per_day": 3,
            "max_consecutive_losses": 3,
            "max_capital_per_trade_pct": 30,
            "entry_start": "09:30",
            "entry_end": "14:00",
            "flat_by": "15:10",
            "expiry_day_flat_by": "14:45",
            "stop_rule": "or-mid",
            "target_r": 2.0,
            "max_premium_loss_pct": 30,
            "no_progress_bars": 6,
            "paper_trading_enabled": True,
            "auto_execute": True,
            "symbols": ["NIFTY", "SENSEX", "BANKNIFTY"],
        }

    def update_settings(self, new_settings: Dict[str, Any]):
        self.settings.update(new_settings)
        # Persist to trader/config.json
        import json as _json
        config_path = os.path.join(self.runner.trader_dir, "config.json")
        cfg = {}
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    cfg = _json.load(f)
            except Exception:
                cfg = {}
        cfg["account"] = {
            "capital_inr": self.settings["capital_inr"],
            "risk_per_trade_pct": self.settings["risk_per_trade_pct"],
            "max_daily_loss_pct": self.settings["max_daily_loss_pct"],
            "max_trades_per_day": self.settings["max_trades_per_day"],
            "max_consecutive_losses": self.settings["max_consecutive_losses"],
            "max_capital_per_trade_pct": self.settings["max_capital_per_trade_pct"],
        }
        cfg["session"] = {
            "entry_start": self.settings["entry_start"],
            "entry_end": self.settings["entry_end"],
            "flat_by": self.settings["flat_by"],
            "expiry_day_flat_by": self.settings["expiry_day_flat_by"],
        }
        cfg["exit"] = {
            "max_premium_loss_pct": self.settings["max_premium_loss_pct"],
            "target_r": self.settings["target_r"],
            "no_progress_bars": self.settings["no_progress_bars"],
        }
        try:
            with open(config_path, "w") as f:
                _json.dump(cfg, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to persist settings: {e}")

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("AutoTrader started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("AutoTrader stopped")

    async def _loop(self):
        while self._running:
            try:
                if _is_market_hours():
                    await self._run_pipeline()
                    await self._track_positions()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AutoTrader loop error: {e}")
                await asyncio.sleep(30)

    async def _track_positions(self):
        """Fetch live LTP for open positions and check SL/Target hits."""
        if not self.paper_positions:
            return
        try:
            sys.path.insert(0, str(PROJECT_ROOT / "skillpack" / "in-common"))
            import smart
            from smart import credentials, Session
            from datetime import datetime

            creds = credentials()
            client = smart.Client(creds["api_key"])
            sess = Session(
                client, creds,
                str(PROJECT_ROOT / "trader" / "state" / "session.json"),
                datetime.now().strftime("%Y-%m-%d")
            )
            sess.start()

            tokens = [p["token"] for p in self.paper_positions if p["status"] == "PAPER_ACTIVE"]
            if not tokens:
                return

            r = client.call("quote", {"mode": "LTP", "exchangeTokens": {"NFO": tokens}}, retry=False)
            if not r.ok:
                logger.error(f"Live quote failed: {r.summary()}")
                return

            d = r.data or {}
            fetched = d.get("fetched") or []
            ltp_map = {f.get("symbolToken"): f.get("ltp") for f in fetched}

            underlying_ltp = None
            for pos in self.paper_positions:
                if pos["status"] != "PAPER_ACTIVE":
                    continue
                token = pos.get("token")
                ltp = ltp_map.get(token)
                if ltp is None:
                    continue
                self.update_position_from_tick(pos["symbol"], float(ltp), underlying_ltp)
        except Exception as e:
            logger.error(f"Position tracking error: {e}")

    async def _run_pipeline(self):
        now = _now_ist()
        now_str = now.strftime("%Y-%m-%d %H:%M")

        # 1. in-news
        news_result = self.runner.run_skill("in-news", "news", now_str=now_str)
        if news_result.get("success") and news_result.get("json"):
            self.last_scan = {"news": news_result["json"], "timestamp": now_str}
            await self._broadcast("news_update", self.last_scan)

        # 2. in-scanner (orb mode)
        scan_result = self.runner.run_skill("in-scanner", "scan", ["orb", "--stocks"], now_str=now_str)
        if scan_result.get("success") and scan_result.get("json"):
            self.last_scan = {**(self.last_scan or {}), "scan": scan_result["json"], "timestamp": now_str}
            await self._broadcast("scan_update", self.last_scan)

        # 3. in-entry for each symbol
        for symbol in self.settings.get("symbols", ["NIFTY"]):
            entry_result = self.runner.run_skill("in-entry", "entry", ["check", symbol], now_str=now_str)
            if entry_result.get("success") and entry_result.get("json"):
                entry_data = entry_result["json"]
                if entry_data.get("status") == "triggered":
                    await self._handle_trigger(entry_data, now_str)
                self.last_signal = {
                    "symbol": symbol,
                    "status": entry_data.get("status"),
                    "direction": entry_data.get("direction"),
                    "trigger_level": entry_data.get("trigger_level"),
                    "bar_close": entry_data.get("bar_close"),
                    "bar_close_ist": entry_data.get("bar_close_ist"),
                    "timestamp": now_str,
                }
                await self._broadcast("signal_update", self.last_signal)

        # 4. If triggered, run chain -> exit -> budget -> risk -> ticket
        if self.last_signal and self.last_signal.get("status") == "triggered":
            symbol = self.last_signal["symbol"]
            direction = self.last_signal["direction"]

            chain_args = ["pick", symbol]
            if direction:
                chain_args += ["--direction", "bullish" if direction == "bullish" else "bearish"]
            chain_result = self.runner.run_skill("in-chain", "chain", chain_args, now_str=now_str)
            if chain_result.get("success") and chain_result.get("json"):
                pick_data = chain_result["json"]
                self.last_signal["pick"] = pick_data
                await self._broadcast("signal_update", self.last_signal)

                exit_result = self.runner.run_skill("in-exit", "exit", ["plan", "--write"], now_str=now_str)
                if exit_result.get("success") and exit_result.get("json"):
                    plan_data = exit_result["json"]
                    self.last_signal["plan"] = plan_data
                    await self._broadcast("signal_update", self.last_signal)

                    budget_result = self.runner.run_skill("in-budget", "size", ["--write"], now_str=now_str)
                    if budget_result.get("success"):
                        self.last_signal["budget"] = budget_result.get("stdout", "")
                        await self._broadcast("signal_update", self.last_signal)

                        risk_result = self.runner.run_skill("in-risk", "check", now_str=now_str)
                        if risk_result.get("success") and risk_result.get("json"):
                            risk_data = risk_result["json"]
                            self.last_signal["risk"] = risk_data
                            await self._broadcast("signal_update", self.last_signal)

                            if (self.settings.get("auto_execute")
                                    and self.settings.get("paper_trading_enabled")
                                    and risk_data.get("verdict") == "PASS"):
                                await self._auto_execute_paper_trade(self.last_signal)

    async def _handle_trigger(self, entry_data: Dict[str, Any], now_str: str):
        logger.info(f"TRIGGER DETECTED: {entry_data.get('symbol')} {entry_data.get('direction')} at {entry_data.get('trigger_level')}")
        await self._broadcast("trigger_detected", {
            "symbol": entry_data.get("symbol"),
            "direction": entry_data.get("direction"),
            "trigger_level": entry_data.get("trigger_level"),
            "bar_close": entry_data.get("bar_close"),
            "timestamp": now_str,
            "type": "TRIGGER",
        })

    async def _auto_execute_paper_trade(self, signal: Dict[str, Any]):
        pick = signal.get("pick", {})
        plan = signal.get("plan", {})
        instrument = pick.get("instrument", {})
        premium = float(pick.get("premium", 0))
        stop = float(plan.get("premium_stop", 0))
        target = float(plan.get("target_premium", 0))
        underlying_stop = float(plan.get("underlying_stop", 0))
        underlying_target = float(plan.get("target_underlying", 0))
        lot_size = int(instrument.get("lot_size", 65))
        symbol = signal.get("symbol", "NIFTY")
        direction = signal.get("direction", "bullish")

        self._trade_id_counter += 1
        trade_id = f"P{_today_ist()}-{self._trade_id_counter:03d}"

        trade = {
            "id": trade_id,
            "symbol": symbol,
            "instrument": instrument.get("tradingsymbol", f"{symbol} CE"),
            "token": instrument.get("token", ""),
            "direction": direction,
            "entry_price": premium,
            "stop_loss": stop,
            "target": target,
            "underlying_stop": underlying_stop,
            "underlying_target": underlying_target,
            "lot_size": lot_size,
            "quantity": lot_size,
            "status": "PAPER_ACTIVE",
            "entry_time": _now_ist().isoformat(),
            "pnl": 0.0,
            "exit_reason": None,
            "exit_time": None,
            "exit_price": None,
            "risk_r": float(plan.get("risk_per_unit_inr", 0)),
            "r_value": float(plan.get("r_value_inr", 0)),
        }

        self.paper_positions.append(trade)
        self.paper_orders.append({
            "id": trade_id,
            "symbol": symbol,
            "side": "BUY",
            "price": premium,
            "quantity": lot_size,
            "status": "FILLED",
            "timestamp": _now_ist().isoformat(),
        })

        await self._broadcast("paper_trade_executed", trade)
        logger.info(f"AUTO PAPER TRADE: {trade_id} {symbol} {direction} @ {premium} SL={stop} TGT={target}")

    def update_position_from_tick(self, symbol: str, ltp: float, underlying_ltp: float = None):
        """Check SL/TP hits for open paper positions."""
        for pos in self.paper_positions:
            if pos["status"] != "PAPER_ACTIVE":
                continue

            hit = False
            reason = None

            if pos["direction"] == "bullish":
                if ltp <= pos["stop_loss"]:
                    hit, reason = True, "STOP_HIT"
                elif ltp >= pos["target"]:
                    hit, reason = True, "TARGET_HIT"
            else:
                if ltp >= pos["stop_loss"]:
                    hit, reason = True, "STOP_HIT"
                elif ltp <= pos["target"]:
                    hit, reason = True, "TARGET_HIT"

            # Underlying checks
            if not hit and underlying_ltp is not None:
                us = pos.get("underlying_stop", 0)
                ut = pos.get("underlying_target", float("inf"))
                if pos["direction"] == "bullish":
                    if underlying_ltp <= us:
                        hit, reason = True, "UNDERLYING_STOP_HIT"
                    elif underlying_ltp >= ut:
                        hit, reason = True, "UNDERLYING_TARGET_HIT"
                else:
                    if underlying_ltp >= us:
                        hit, reason = True, "UNDERLYING_STOP_HIT"
                    elif underlying_ltp <= ut:
                        hit, reason = True, "UNDERLYING_TARGET_HIT"

            if hit:
                self._close_position(pos, ltp, reason)

    def _close_position(self, pos: Dict[str, Any], exit_price: float, reason: str):
        pos["status"] = "CLOSED"
        pos["exit_price"] = exit_price
        pos["exit_time"] = _now_ist().isoformat()
        pos["exit_reason"] = reason

        entry = pos["entry_price"]
        qty = pos["quantity"]
        if pos["direction"] == "bullish":
            pnl = (exit_price - entry) * qty
        else:
            pnl = (entry - exit_price) * qty

        pos["pnl"] = round(pnl, 2)
        self.daily_pnl += Decimal(str(pnl))

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        logger.info(f"POSITION CLOSED: {pos['id']} {reason} PnL={pnl}")
        asyncio.create_task(self._broadcast("position_closed", pos))
        self._save_positions_to_file()

    def _save_positions_to_file(self):
        import json
        path = os.path.join(self.runner.trader_dir, "state", "positions.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w") as f:
                json.dump(self.paper_positions, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save positions: {e}")

    async def _broadcast(self, event: str, data: Any):
        if self.sio:
            try:
                await self.sio.emit(event, data)
            except Exception as e:
                logger.error(f"Broadcast error: {e}")

    def get_state(self) -> Dict[str, Any]:
        return {
            "settings": self.settings,
            "positions": self.paper_positions,
            "orders": self.paper_orders,
            "daily_pnl": float(self.daily_pnl),
            "consecutive_losses": self.consecutive_losses,
            "last_signal": self.last_signal,
            "last_scan": self.last_scan,
            "is_market_hours": _is_market_hours(),
            "timestamp": _now_ist().isoformat(),
            "running": self._running,
        }


# Global instances
runner = SkillPackRunner(
    skill_pack_root=os.environ.get("SKILL_PACK_ROOT", str(PROJECT_ROOT / "skillpack")),
    trader_dir=os.environ.get("TRADER_DIR", str(PROJECT_ROOT / "trader")),
)
auto_trader = AutoTrader(runner)
