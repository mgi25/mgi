# risk.py
#
# Trade risk management and state logic for the XAUUSDm tournament bot.
# Handles:
# - Dynamic lot sizing by account equity
# - Spread and session checks
# - Daily stop conditions (drawdown / profit target)
# - Hedge trigger
# - Lock mode and recovery logic
# - PnL-based resets

from datetime import datetime
from statistics import mean

# -------------------------------
# PHASED LOT SIZING (by equity)
# -------------------------------
def base_lot(equity):
    """
    Determines lot size based on current equity level.
    Designed for small accounts growing gradually.
    """
    if equity <= 150:
        return 0.01
    elif equity <= 400:
        return 0.02
    elif equity <= 700:
        return 0.03
    else:
        return 0.05


# -------------------------------
# SPREAD CHECK
# -------------------------------
def check_spread_ok(spread_points, limit_points):
    """
    Returns True if spread is within acceptable limits.
    """
    return spread_points <= limit_points


# -------------------------------
# DAILY RISK STOP RULES
# -------------------------------
def daily_stop(equity, daily_start_equity, max_dd_pct=5, daily_target_pct=3):
    """
    Controls whether the bot should continue trading for the day.
    """
    drawdown = 100 * (daily_start_equity - equity) / daily_start_equity
    gain = 100 * (equity - daily_start_equity) / daily_start_equity

    if drawdown >= max_dd_pct:
        return "STOP_DRAWDOWN"
    elif gain >= daily_target_pct:
        return "STOP_TARGET"
    else:
        return "GO"


# -------------------------------
# ENTRY DECISION
# -------------------------------
def plan_new_entry(market_state, spread_ok, locked_mode, hedge_layers):
    """
    Decides whether we can open a new position.
    """
    if not spread_ok or locked_mode:
        return "NO_TRADE"

    if hedge_layers > 0:
        return "NO_TRADE"

    if market_state == "TREND_LONG":
        return "LONG"
    elif market_state == "TREND_SHORT":
        return "SHORT"
    else:
        return "NO_TRADE"


# -------------------------------
# SINGLE TRADE MANAGEMENT
# -------------------------------
def manage_open_trade(pnl_dollars, tp_target=0.40, sl_cut=0.20):
    """
    Evaluates trade outcome and recommends action.
    Returns action signal:
    - 'HOLD'
    - 'BREAKEVEN_SL'
    - 'TAKE_PROFIT'
    - 'HEDGE_NOW'
    """
    if pnl_dollars >= tp_target:
        return "TAKE_PROFIT"
    elif pnl_dollars >= 0.10:
        return "BREAKEVEN_SL"
    elif pnl_dollars <= -sl_cut:
        return "HEDGE_NOW"
    else:
        return "HOLD"


# -------------------------------
# HEDGE SYSTEM (ladder pattern)
# -------------------------------
def next_hedge_lot(base_lot, hedge_layers):
    """
    Determines the lot for next hedge based on ladder sequence.
    """
    return round(base_lot * (hedge_layers + 1), 2)


def hedge_controller(hedge_layers, max_layers=3):
    """
    Decides whether to add new hedge or enter lock mode.
    """
    if hedge_layers < max_layers:
        return "ADD_HEDGE"
    else:
        return "ENTER_LOCK"


# -------------------------------
# LOCK MODE & RECOVERY
# -------------------------------
def lock_mode_controller(total_pnl, locked_loss_value, breakout_confirmed):
    """
    Logic for post-lock recovery.
    - Waits for breakout confirmation.
    - Once breakout occurs, if total PnL >= 0 → reset.
    """
    if total_pnl >= 0:
        return "CLOSE_ALL_AND_RESET"

    if breakout_confirmed:
        return "TAKE_RECOVERY_TRADE"

    return "WAIT"


# -------------------------------
# RESET LOGIC
# -------------------------------
def reset_all():
    """
    Resets trade state after basket close.
    """
    state = {
        "hedge_layers": 0,
        "locked_mode": False,
        "locked_loss_value": 0.0,
        "daily_start_equity": None
    }
    return state


# -------------------------------
# STATUS HELPER
# -------------------------------
def equity_status(equity, daily_start_equity):
    gain = 100 * (equity - daily_start_equity) / daily_start_equity
    return round(gain, 2)
