#!/usr/bin/env python3
"""Manually execute the paper trade from the prepared signal data."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

IST_OFFSET = __import__('datetime').timedelta(hours=5, minutes=30)
IST = __import__('datetime').timezone(IST_OFFSET, "IST")

# Project root (trader -> indian-day-trader)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRADER_DIR = str(PROJECT_ROOT / "trader")

# Load pick data
with open(os.path.join(TRADER_DIR, "pick.json")) as f:
    pick = json.load(f)

# Load plan data
with open(os.path.join(TRADER_DIR, "plan.json")) as f:
    plan = json.load(f)

# Load risk result from orders.json
with open(os.path.join(TRADER_DIR, "orders.json")) as f:
    orders = json.load(f)

# Find the risk result for our ticket
risk_data = None
for ticket in orders.get("tickets", []):
    if ticket.get("id") == "T20261125-01":
        risk_data = ticket
        break

print("=== PICK ===")
print(json.dumps(pick, indent=2)[:500])
print("=== PLAN ===")
print(json.dumps(plan, indent=2)[:500])
print("=== RISK ===")
print(json.dumps(risk_data, indent=2)[:500] if risk_data else "No risk data found")

# Build the trade
instrument = pick.get("instrument", {})
premium = float(pick.get("premium", 0))
stop = float(plan.get("premium_stop", 0))
target = float(plan.get("target_premium", 0))
underlying_stop = float(plan.get("underlying_stop", 0))
underlying_target = float(plan.get("target_underlying", 0))
lot_size = int(instrument.get("lot_size", 65))
symbol = "NIFTY"
direction = "bullish"

trade_id = f"P{datetime.now(IST).strftime('%Y%m%d')}-001"

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
    "entry_time": datetime.now(IST).isoformat(),
    "pnl": 0.0,
    "exit_reason": None,
    "exit_time": None,
    "exit_price": None,
    "risk_r": float(plan.get("risk_per_unit_inr", 0)),
    "r_value": float(plan.get("r_value_inr", 0)),
}

print("\n=== EXECUTING PAPER TRADE ===")
print(f"Trade ID: {trade_id}")
print(f"Symbol: {symbol} {direction}")
print(f"Instrument: {trade['instrument']}")
print(f"Entry: {premium}, Stop: {stop}, Target: {target}")
print(f"Quantity: {lot_size}")

# Load existing positions
positions_path = os.path.join(TRADER_DIR, "state", "positions.json")
positions = []
if os.path.exists(positions_path):
    with open(positions_path) as f:
        positions = json.load(f)

positions.append(trade)

os.makedirs(os.path.join(TRADER_DIR, "state"), exist_ok=True)
with open(positions_path, "w") as f:
    json.dump(positions, f, indent=2)

print(f"\nPaper trade executed: {trade_id} {symbol} {direction} @ {premium}")
print(f"Position written to {positions_path}")
