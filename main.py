"""main.py

Main orchestrator for the XAUUSDm M1 scalper. Runs continuously (24×5) while
respecting strict daily and basket risk guardrails.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta
from statistics import median
from typing import Optional

import broker
import filters
import risk

SYMBOL = "XAUUSDm"
ENTRY_COOLDOWN_SEC = 10
SPREAD_HISTORY = deque(maxlen=120)

state = {
    "daily_start_equity": None,
    "baseline_date": None,
    "hedge_layers": 0,
    "locked_mode": False,
    "locked_loss_value": 0.0,
    "lock_recovery_taken": False,
    "last_entry_ts": None,
    "sleep_seconds": 2.0,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def initialize_day() -> None:
    account = broker.get_account_info()
    equity = account["equity"]
    today = datetime.now().date()
    state.update(
        {
            "daily_start_equity": equity,
            "baseline_date": today,
            "hedge_layers": 0,
            "locked_mode": False,
            "locked_loss_value": 0.0,
            "lock_recovery_taken": False,
            "last_entry_ts": None,
        }
    )
    print(f"[INIT] baseline rolled to {today} with equity={equity:.2f} USD")


def dynamic_spread_cap(default: int = 200) -> int:
    if len(SPREAD_HISTORY) < 30:
        return max(160, min(220, default))
    med = median(SPREAD_HISTORY)
    cap = int(med * 2.0)
    return max(160, min(220, cap))


def can_open_new_entry() -> bool:
    last = state.get("last_entry_ts")
    if last is None:
        return True
    return datetime.now() - last >= timedelta(seconds=ENTRY_COOLDOWN_SEC)


def compute_targets_for_lot(
    atr_price: Optional[float],
    contract_size: Optional[float],
    lot: float,
) -> tuple:
    if contract_size is None or contract_size <= 0 or lot <= 0:
        return 0.60, 0.30
    if atr_price is None:
        atr_dollars = None
    else:
        atr_dollars = atr_price * contract_size * lot
    if atr_dollars is None:
        tp_dollars = 0.60
        sl_dollars = 0.30
    else:
        tp_dollars = min(1.20, max(0.30, 0.35 * atr_dollars))
        sl_dollars = min(0.60, max(0.15, 0.18 * atr_dollars))
    return tp_dollars, sl_dollars


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def analyze_market() -> dict:
    candles = broker.get_ohlc(SYMBOL, timeframe="M1", n=160)
    symbol_info = broker.get_symbol_info(SYMBOL) or {}
    spread_points = broker.get_spread_points(SYMBOL)
    SPREAD_HISTORY.append(spread_points)
    spread_cap = dynamic_spread_cap()

    atr_price = filters.compute_atr(candles, period=10)
    adx_val = filters.compute_adx(candles, period=10)
    regime = filters.market_state(candles, atr_price)
    breakout_dir, donchian_hi, donchian_lo, last_close = filters.donchian_breakout(candles)
    atr_dollars_one_lot = filters.atr_dollars(
        candles, period=10, contract=symbol_info.get("trade_contract_size", 1.0)
    )
    spread_ok = risk.check_spread_ok(spread_points, spread_cap)

    diag = filters.market_diag(candles, adx_period=10, ema_fast=13, ema_slow=34, donchian_lkb=14)

    return {
        "candles": candles,
        "atr_price": atr_price,
        "atr_dollars_one_lot": atr_dollars_one_lot,
        "adx": adx_val,
        "donchian_hi": donchian_hi,
        "donchian_lo": donchian_lo,
        "last_close": last_close,
        "breakout_dir": breakout_dir,
        "regime": regime,
        "spread_points": spread_points,
        "spread_cap": spread_cap,
        "spread_ok": spread_ok,
        "symbol_info": symbol_info,
        "diag": diag,
    }


def evaluate_account() -> dict:
    account = broker.get_account_info()
    positions = broker.get_positions(SYMBOL)
    basket_pnl = sum(p["pnl_dollars"] for p in positions)
    stop_signal = risk.daily_stop(account["equity"], state["daily_start_equity"])
    kill_signal = risk.basket_killswitch(
        equity=account["equity"],
        baseline_equity=state["daily_start_equity"],
        floating_pnl=basket_pnl,
    )
    return {
        "balance": account["balance"],
        "equity": account["equity"],
        "positions": positions,
        "basket_pnl": basket_pnl,
        "stop_signal": stop_signal,
        "kill_signal": kill_signal,
    }


# ---------------------------------------------------------------------------
# Position management
# ---------------------------------------------------------------------------

def act_on_positions(market: dict, account: dict) -> None:
    candles = market["candles"]
    atr_price = market["atr_price"]
    contract_size = market["symbol_info"].get("trade_contract_size", 1.0)
    positions = account["positions"]
    basket_pnl = account["basket_pnl"]
    stop_signal = account["stop_signal"]
    kill_signal = account["kill_signal"]
    regime = market["regime"]

    # Adjust polling cadence depending on lock state
    state["sleep_seconds"] = 3.4 if state["locked_mode"] else 2.0

    # Basket kill-switch
    if kill_signal == "KILL":
        print("[KILL] Floating loss breached -12%. Closing all and resetting.")
        broker.close_all(SYMBOL)
        state["hedge_layers"] = 0
        state["locked_mode"] = False
        state["locked_loss_value"] = 0.0
        state["lock_recovery_taken"] = False
        return

    # Lock mode logic
    if state["locked_mode"]:
        breakout_confirmed = regime in ("TREND_LONG", "TREND_SHORT")
        lock_action = risk.lock_mode_controller(
            total_pnl=basket_pnl,
            breakout_confirmed=breakout_confirmed,
        )
        if lock_action == "CLOSE_ALL_AND_RESET":
            print("[LOCK] Basket recovered >=0. Closing all positions.")
            broker.close_all(SYMBOL)
            state["hedge_layers"] = 0
            state["locked_mode"] = False
            state["locked_loss_value"] = 0.0
            state["lock_recovery_taken"] = False
            return
        if lock_action == "TAKE_RECOVERY_TRADE" and not state["lock_recovery_taken"]:
            if stop_signal == "GO" and market["spread_ok"] and can_open_new_entry():
                direction = "LONG" if regime == "TREND_LONG" else "SHORT"
                lot = risk.base_lot(account["equity"])
                print(f"[LOCK] Recovery trade {direction} lot={lot}")
                broker.send_entry(direction, lot, comment="LOCK_RECOVERY")
                state["last_entry_ts"] = datetime.now()
                state["lock_recovery_taken"] = True
            else:
                print("[LOCK] Breakout detected but gated by spread/daily stop/cooldown.")
        else:
            print("[LOCK] Waiting for breakout recovery.")
        return

    # Manage existing trades
    for pos in positions:
        tp_target, sl_cut = compute_targets_for_lot(atr_price, contract_size, pos["lot"])
        action = risk.manage_open_trade(pos["pnl_dollars"], tp_target, sl_cut)
        if action == "TAKE_PROFIT":
            print(f"[TRADE] ticket={pos['ticket']} TAKE_PROFIT pnl={pos['pnl_dollars']:.2f}")
            broker.close_position(pos["ticket"])
        elif action == "BREAKEVEN_SL":
            print(f"[TRADE] ticket={pos['ticket']} move SL to breakeven")
            broker.modify_stop_to_breakeven(pos["ticket"])
        elif action == "HEDGE_NOW":
            hedge_call = risk.hedge_controller(state["hedge_layers"])
            if hedge_call == "ADD":
                hedge_lot = risk.next_hedge_lot(risk.base_lot(account["equity"]), state["hedge_layers"])
                hedge_dir = "SHORT" if pos["direction"] == "LONG" else "LONG"
                print(
                    f"[HEDGE] layer={state['hedge_layers']+1} direction={hedge_dir} lot={hedge_lot} "
                    f"against ticket={pos['ticket']}"
                )
                broker.add_hedge(hedge_dir, hedge_lot)
                state["hedge_layers"] += 1
            else:
                print("[HEDGE] Max hedge layers reached. Entering LOCK mode.")
                state["locked_mode"] = True
                state["locked_loss_value"] = basket_pnl
                state["lock_recovery_taken"] = False
                state["sleep_seconds"] = 3.4
                return

    # Reset hedge counter when flat
    if not positions:
        if state["hedge_layers"] != 0:
            print("[STATE] Basket flat. Resetting hedge layers.")
        state["hedge_layers"] = 0
        state["lock_recovery_taken"] = False

    # No new trades allowed when there are open positions
    if positions:
        return

    if stop_signal not in ("GO", "BASELINE_UNKNOWN"):
        print(f"[RISK] Daily stop active: {stop_signal}. No new entries.")
        return

    if not market["spread_ok"]:
        print(
            f"[SPREAD] Blocked. spread={market['spread_points']} cap={market['spread_cap']}"
        )
        return

    if not can_open_new_entry():
        return

    equity = account["equity"]
    base_lot = risk.base_lot(equity)

    # Trend entries
    if regime in ("TREND_LONG", "TREND_SHORT"):
        direction = "LONG" if regime == "TREND_LONG" else "SHORT"
        print(f"[ENTRY] Trend {direction} lot={base_lot}")
        broker.send_entry(direction, base_lot)
        state["last_entry_ts"] = datetime.now()
        return

    # Micro breakout when regime unsure
    if regime == "UNSURE":
        micro_dir = filters.micro_breakout_signal(candles)
        if micro_dir:
            lot = round(base_lot * 0.5, 2)
            print(f"[ENTRY] Micro-breakout {micro_dir} lot={lot}")
            broker.send_entry(micro_dir, lot)
            state["last_entry_ts"] = datetime.now()
            return

    # Range reversion in calm conditions
    if regime == "RANGING":
        rr_dir = filters.range_reversion_signal(candles)
        if rr_dir:
            lot = round(base_lot * 0.25, 2)
            print(f"[ENTRY] Range reversion {rr_dir} lot={lot}")
            broker.send_entry(rr_dir, lot)
            state["last_entry_ts"] = datetime.now()
            return


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main_loop() -> None:
    if not broker.init_connection():
        print("[FATAL] Unable to connect to MetaTrader 5")
        return
    initialize_day()

    while True:
        try:
            now = datetime.now()
            if state["baseline_date"] != now.date() and now.weekday() <= 4:
                initialize_day()

            market = analyze_market()
            account = evaluate_account()

            print("-----")
            print(f"[TIME] {now.isoformat(timespec='seconds')}")
            print(
                f"[STATE] regime={market['regime']} spread_ok={market['spread_ok']} "
                f"spread={market['spread_points']} cap={market['spread_cap']}"
            )
            print(
                f"[EQUITY] equity={account['equity']:.2f} balance={account['balance']:.2f} "
                f"basket={account['basket_pnl']:.2f} daily_stop={account['stop_signal']} "
                f"kill={account['kill_signal']}"
            )
            atr_txt = "n/a" if market["atr_price"] is None else f"{market['atr_price']:.3f}"
            adx_txt = "n/a" if market["adx"] is None else f"{market['adx']:.1f}"
            print(
                f"[FILTERS] ATR={atr_txt} ADX={adx_txt} "
                f"DonchianHi={market['donchian_hi']} DonchianLo={market['donchian_lo']}"
            )
            print(
                f"[HEDGE] layers={state['hedge_layers']} locked={state['locked_mode']} "
                f"locked_loss={state['locked_loss_value']:.2f} cooldown={ENTRY_COOLDOWN_SEC}s"
            )

            act_on_positions(market, account)

            sleep_time = state.get("sleep_seconds", 2.0)
            time.sleep(sleep_time)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[ERROR] {exc}")
            time.sleep(2.5)


if __name__ == "__main__":
    main_loop()
