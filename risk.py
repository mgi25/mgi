"""risk.py

Risk sizing and management helpers for the XAUUSDm strategy.
"""
from __future__ import annotations

from typing import Literal, Optional

import MetaTrader5 as mt5


def _snap_to_step(volume: float, step: float) -> float:
    if step <= 0:
        return float(volume)
    steps = round(float(volume) / step)
    return round(steps * step, 8)


def lots_for_risk(symbol: str, equity: float, risk_pct: float, stop_distance_price: float) -> float:
    """Return lot size that risks ``risk_pct`` of equity for a stop distance."""
    if equity <= 0 or risk_pct <= 0 or stop_distance_price <= 0:
        return 0.0

    info = mt5.symbol_info(symbol)
    if info is None:
        return 0.0

    tick_value = getattr(info, "trade_tick_value", 0.0) or None
    tick_size = getattr(info, "trade_tick_size", 0.0) or None
    if tick_value and tick_size:
        dollars_per_price = tick_value / tick_size
    else:
        dollars_per_price = getattr(info, "trade_contract_size", 1.0)

    dollars_per_lot_at_stop = abs(stop_distance_price) * dollars_per_price
    if dollars_per_lot_at_stop <= 1e-9:
        return 0.0

    account_risk = equity * risk_pct
    raw_lots = account_risk / dollars_per_lot_at_stop

    vol_min = getattr(info, "volume_min", 0.0) or 0.0
    vol_max = getattr(info, "volume_max", 1000.0) or 1000.0
    vol_step = getattr(info, "volume_step", 0.01) or 0.01

    snapped = _snap_to_step(round(raw_lots, 2), vol_step)
    return max(vol_min, min(vol_max, snapped))


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


def manage_open_trade(
    pnl_dollars: float,
    r_value_dollars: float,
    be_trigger_r: float,
    trail_after_r: float,
) -> Literal["HOLD", "BREAKEVEN_SL", "TRAIL", "CUT_OR_HEDGE"]:
    if r_value_dollars <= 1e-9:
        return "HOLD"

    r_multiple = pnl_dollars / r_value_dollars
    if r_multiple >= trail_after_r:
        return "TRAIL"
    if r_multiple >= be_trigger_r:
        return "BREAKEVEN_SL"
    if r_multiple <= -1.0:
        return "CUT_OR_HEDGE"
    return "HOLD"


def max_trades_reached(trades_count: int, limit: int) -> bool:
    return trades_count >= limit
