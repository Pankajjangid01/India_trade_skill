#!/usr/bin/env python3
"""Standalone signal runner - generates signals directly from backend during market hours."""
import asyncio
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
IST = datetime.timezone(IST_OFFSET, "IST")
MARKET_OPEN = "09:00"
MARKET_CLOSE = "15:30"

# Project root (trader -> indian-day-trader)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.skillpack.runner import SkillPackRunner, AutoTrader

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("signal_runner")

SKILL_PACK_ROOT = str(PROJECT_ROOT / "skillpack")
TRADER_DIR = str(PROJECT_ROOT / "trader")


def _now_ist():
    return datetime.datetime.now(IST)


def _is_market_hours(now=None):
    now = now or _now_ist()
    hhmm = now.strftime("%H:%M")
    return MARKET_OPEN <= hhmm <= MARKET_CLOSE and now.weekday() < 5


def run_pipeline_once(runner, trader, now_str):
    """Run one full signal pipeline cycle."""
    # 1. in-news
    news_result = runner.run_skill("in-news", "news", now_str=now_str)
    if news_result.get("success") and news_result.get("json"):
        trader.last_scan = {"news": news_result["json"], "timestamp": now_str}

    # 2. in-scanner (orb mode)
    scan_result = runner.run_skill("in-scanner", "scan", ["orb", "--stocks"], now_str=now_str)
    if scan_result.get("success") and scan_result.get("json"):
        trader.last_scan = {**(trader.last_scan or {}), "scan": scan_result["json"], "timestamp": now_str}

    # 3. in-entry for each symbol
    for symbol in trader.settings.get("symbols", ["NIFTY"]):
        entry_result = runner.run_skill("in-entry", "entry", ["check", symbol], now_str=now_str)
        if not (entry_result.get("success") and entry_result.get("json")):
            continue

        entry_data = entry_result["json"]
        if entry_data.get("status") == "triggered":
            trader.last_signal = {
                "symbol": symbol,
                "status": entry_data.get("status"),
                "direction": entry_data.get("direction"),
                "trigger_level": entry_data.get("trigger_level"),
                "bar_close": entry_data.get("bar_close"),
                "bar_close_ist": entry_data.get("bar_close_ist"),
                "timestamp": now_str,
            }
            logger.info(f"TRIGGER: {symbol} {entry_data.get('direction')} at {entry_data.get('trigger_level')}")

            # 4. chain
            chain_args = ["pick", symbol]
            direction = entry_data.get("direction")
            if direction:
                chain_args += ["--direction", "bullish" if direction == "bullish" else "bearish"]
            chain_result = runner.run_skill("in-chain", "chain", chain_args, now_str=now_str)
            if chain_result.get("success") and chain_result.get("json"):
                pick_data = chain_result["json"]
                trader.last_signal["pick"] = pick_data

                # 5. exit plan
                exit_result = runner.run_skill("in-exit", "exit", ["plan", "--write"], now_str=now_str)
                if exit_result.get("success") and exit_result.get("json"):
                    plan_data = exit_result["json"]
                    trader.last_signal["plan"] = plan_data

                    # 6. budget
                    budget_result = runner.run_skill("in-budget", "size", ["--write"], now_str=now_str)
                    if budget_result.get("success"):
                        trader.last_signal["budget"] = budget_result.get("stdout", "")

                        # 7. risk check
                        risk_result = runner.run_skill("in-risk", "check", now_str=now_str)
                        if risk_result.get("success") and risk_result.get("json"):
                            risk_data = risk_result["json"]
                            trader.last_signal["risk"] = risk_data
                            logger.info(f"Risk verdict: {risk_data.get('verdict')}")

                            if (trader.settings.get("auto_execute")
                                    and trader.settings.get("paper_trading_enabled")
                                    and risk_data.get("verdict") == "PASS"):
                                trader._auto_execute_paper_trade(trader.last_signal)
                                logger.info(f"PAPER TRADE EXECUTED: {symbol} {direction}")
        else:
            trader.last_signal = {
                "symbol": symbol,
                "status": entry_data.get("status"),
                "direction": entry_data.get("direction"),
                "trigger_level": entry_data.get("trigger_level"),
                "bar_close": entry_data.get("bar_close"),
                "bar_close_ist": entry_data.get("bar_close_ist"),
                "timestamp": now_str,
            }


def main():
    runner = SkillPackRunner(skill_pack_root=SKILL_PACK_ROOT, trader_dir=TRADER_DIR)
    trader = AutoTrader(runner)

    logger.info("Signal runner started. Will run pipeline every 30s during market hours (09:00-15:30 IST).")

    while True:
        try:
            now = _now_ist()
            if _is_market_hours(now):
                now_str = now.strftime("%Y-%m-%d %H:%M")
                logger.info(f"Running pipeline at {now_str}")
                run_pipeline_once(runner, trader, now_str)
            else:
                logger.debug(f"Outside market hours at {now.strftime('%H:%M %Z')}")
        except Exception as e:
            logger.error(f"Pipeline error: {e}")

        time.sleep(30)


if __name__ == "__main__":
    main()
