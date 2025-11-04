"""risk.py

Risk controls and basket management for the XAUUSDm scalper.
The module purposefully avoids any broker dependencies so it remains
unit-testable. Main orchestrator injects live data and acts on the returned
signals.
"""
from __future__ import annotations

from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Lot sizing
# ---------------------------------------------------------------------------

def base_lot(equity: float) -> float:
    """Tiered lot sizing based on current equity."""
    if equity <= 150:
        return 0.01
    if equity <= 400:
        return 0.02
    if equity <= 700:
        return 0.03
    return 0.05


# ---------------------------------------------------------------------------
# Spread discipline
# ---------------------------------------------------------------------------

def check_spread_ok(spread_points: int, limit_points: int) -> bool:
    return spread_points <= limit_points


# ---------------------------------------------------------------------------
# Daily guardrails
# ---------------------------------------------------------------------------

def daily_stop(
    equity: float,
    baseline_equity: Optional[float],
    gain_limit_pct: float = 4.0,
    drawdown_limit_pct: float = 6.0,
) -> Literal["GO", "STOP_GAIN", "STOP_LOSS", "BASELINE_UNKNOWN"]:
    if baseline_equity is None or baseline_equity <= 0:
        return "BASELINE_UNKNOWN"

    gain_pct = 100.0 * (equity - baseline_equity) / baseline_equity
    drawdown_pct = 100.0 * (baseline_equity - equity) / baseline_equity

    if gain_pct >= gain_limit_pct:
        return "STOP_GAIN"
    if drawdown_pct >= drawdown_limit_pct:
        return "STOP_LOSS"
    return "GO"


# ---------------------------------------------------------------------------
# Trade management
# ---------------------------------------------------------------------------

def manage_open_trade(
    pnl_dollars: float,
    tp_target: float,
    sl_cut: float,
) -> Literal["HOLD", "TAKE_PROFIT", "BREAKEVEN_SL", "HEDGE_NOW"]:
    """Return the management action for a live trade."""
    if pnl_dollars >= tp_target:
        return "TAKE_PROFIT"
    breakeven_trigger = min(0.10, 0.33 * tp_target)
    if pnl_dollars >= breakeven_trigger:
        return "BREAKEVEN_SL"
    if pnl_dollars <= -sl_cut:
        return "HEDGE_NOW"
    return "HOLD"


# ---------------------------------------------------------------------------
# Hedge handling
# ---------------------------------------------------------------------------

def next_hedge_lot(base: float, hedge_layers: int) -> float:
    """Scale hedge lot linearly with the number of layers already deployed."""
    layer = hedge_layers + 1
    return round(base * layer, 2)


def hedge_controller(
    hedge_layers: int,
    max_layers: int = 2,
) -> Literal["ADD", "LOCK"]:
    return "ADD" if hedge_layers < max_layers else "LOCK"


# ---------------------------------------------------------------------------
# Basket protection
# ---------------------------------------------------------------------------

def basket_killswitch(
    equity: float,
    baseline_equity: Optional[float],
    floating_pnl: float,
    kill_pct: float = 12.0,
) -> Literal["GO", "KILL"]:
    if equity <= 0:
        return "GO"
    if floating_pnl <= -(kill_pct / 100.0) * equity:
        return "KILL"
    return "GO"


# ---------------------------------------------------------------------------
# Lock-mode logic
# ---------------------------------------------------------------------------

def lock_mode_controller(
    total_pnl: float,
    breakout_confirmed: bool,
    threshold: float = 0.0,
) -> Literal["CLOSE_ALL_AND_RESET", "TAKE_RECOVERY_TRADE", "WAIT"]:
    if total_pnl >= threshold:
        return "CLOSE_ALL_AND_RESET"
    if breakout_confirmed:
        return "TAKE_RECOVERY_TRADE"
    return "WAIT"
