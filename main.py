"""Main orchestrator for the XAUUSDm strategy."""
from __future__ import annotations

import argparse
import time
from collections import deque
from datetime import datetime, timedelta
from statistics import median
from typing import Dict, Optional

import broker
import filters
import risk
from config import CFG, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER

SPREAD_HISTORY = deque(maxlen=120)

state: Dict[str, Optional[object]] = {
    "daily_start_equity": None,
    "baseline_date": None,
    "trades_today": 0,
    "last_entry_ts": None,
    "open_trades": {},
    "hedge_used": False,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dryrun", action="store_true", help="Log actions without sending orders")
    return parser.parse_args()


def initialize_day(equity: float, today) -> None:
    state["daily_start_equity"] = equity
    state["baseline_date"] = today
    state["trades_today"] = 0
    state["hedge_used"] = False
    print(f"[INIT] baseline rolled to {today} with equity={equity:.2f}")


def cooldown_ready() -> bool:
    last_ts = state.get("last_entry_ts")
    if last_ts is None:
        return True
    return datetime.now() - last_ts >= timedelta(seconds=CFG["ENTRY_COOLDOWN_SEC"])


def dynamic_spread_cap() -> int:
    if SPREAD_HISTORY:
        med = median(SPREAD_HISTORY)
    else:
        med = CFG["SPREAD_POINTS_BASE_CAP"]
    cap = max(120, min(240, int(med * 2.2)))
    return max(cap, CFG["SPREAD_POINTS_BASE_CAP"])


def dollars_per_price(symbol_info: Dict[str, float]) -> float:
    tick_value = symbol_info.get("trade_tick_value") or 0.0
    tick_size = symbol_info.get("trade_tick_size") or 0.0
    if tick_value and tick_size:
        return tick_value / tick_size
    return symbol_info.get("trade_contract_size", 1.0)


def collect_market(symbol: str) -> Dict[str, object]:
    candles = broker.get_ohlc(symbol, timeframe="M1", n=200)
    spread_points = broker.get_spread_points(symbol)
    SPREAD_HISTORY.append(spread_points)
    atr_value = filters.compute_atr(candles, period=int(CFG["ATR_PERIOD"]))
    adx_value = filters.compute_adx(candles, period=int(CFG["ATR_PERIOD"]))
    regime_info = filters.market_state(candles, adx_value, atr_value, CFG)
    spread_cap = dynamic_spread_cap()
    return {
        "candles": candles,
        "atr": atr_value,
        "adx": adx_value,
        "regime_info": regime_info,
        "spread_points": spread_points,
        "spread_cap": spread_cap,
    }


def update_trade_registry(positions: list, dpp: float) -> None:
    active = {pos["ticket"] for pos in positions}
    for ticket in list(state["open_trades"].keys()):
        if ticket not in active:
            state["open_trades"].pop(ticket, None)
    if not positions:
        state["hedge_used"] = False
    for pos in positions:
        info = state["open_trades"].setdefault(
            pos["ticket"],
            {"r_value": 0.0, "breakeven_done": False, "trail_started": False, "direction": pos["direction"]},
        )
        info["direction"] = pos["direction"]
        if info["r_value"] <= 0 and pos.get("sl"):
            stop_distance = abs(pos["entry_price"] - pos["sl"])
            info["r_value"] = stop_distance * dpp * pos["lot"]


def log_gate(gates: list) -> None:
    if gates:
        print(f"[GATE] no_trade: {', '.join(gates)}")
    else:
        print("[GATE] clear")


def manage_positions(
    positions: list,
    market: Dict[str, object],
    symbol_info: Dict[str, float],
    dryrun: bool,
) -> None:
    if not positions:
        return

    atr_value = market["atr"]
    dpp = dollars_per_price(symbol_info)
    for pos in positions:
        ticket = pos["ticket"]
        trade_info = state["open_trades"].get(ticket, {"r_value": 0.0, "breakeven_done": False, "trail_started": False})
        r_value = trade_info.get("r_value", 0.0)
        action = risk.manage_open_trade(
            pos["pnl_dollars"],
            r_value,
            CFG["BE_TRIGGER_R"],
            CFG["TRAIL_AFTER_R"],
        )
        r_multiple = pos["pnl_dollars"] / r_value if r_value > 1e-9 else 0.0
        if action == "BREAKEVEN_SL" and not trade_info.get("breakeven_done"):
            print(f"[MX] ticket={ticket} action=BE r_mult={r_multiple:.2f}")
            if not dryrun:
                success = broker.modify_stop_to_breakeven(ticket)
                if success:
                    trade_info["breakeven_done"] = True
            else:
                trade_info["breakeven_done"] = True
        elif action == "TRAIL":
            if atr_value is None:
                print(f"[MX] ticket={ticket} trail skipped: ATR unavailable")
                continue
            trail_dist = CFG["TRAIL_ATR_MULT"] * atr_value
            tick = broker.get_tick(CFG["SYMBOL"])
            if not tick:
                print(f"[MX] ticket={ticket} trail skipped: tick unavailable")
                continue
            last_price = float(tick.bid if pos["direction"] == "LONG" else tick.ask)
            current_sl = pos["sl"] or (pos["entry_price"] - CFG["SL_ATR_MULT"] * atr_value if pos["direction"] == "LONG" else pos["entry_price"] + CFG["SL_ATR_MULT"] * atr_value)
            if pos["direction"] == "LONG":
                candidate = max(current_sl, last_price - trail_dist)
            else:
                candidate = min(current_sl, last_price + trail_dist)
            print(f"[MX] ticket={ticket} action=TRAIL target_sl={candidate:.3f} r_mult={r_multiple:.2f}")
            if not dryrun:
                broker.trail_stop(ticket, candidate)
            trade_info["trail_started"] = True
        elif action == "CUT_OR_HEDGE":
            print(f"[MX] ticket={ticket} action=CUT_OR_HEDGE r_mult={r_multiple:.2f}")
            if CFG["ALLOW_SINGLE_HEDGE"] and not state["hedge_used"] and atr_value is not None:
                hedge_dir = "SHORT" if pos["direction"] == "LONG" else "LONG"
                tick = broker.get_tick(CFG["SYMBOL"])
                if not tick:
                    print("[MX] hedge skipped: tick unavailable")
                else:
                    hedge_price = float(tick.ask if hedge_dir == "LONG" else tick.bid)
                    stop_distance = CFG["SL_ATR_MULT"] * atr_value
                    tp_distance = CFG["TP_R_MULT"] * stop_distance
                    sl_price = hedge_price - stop_distance if hedge_dir == "LONG" else hedge_price + stop_distance
                    tp_price = hedge_price + tp_distance if hedge_dir == "LONG" else hedge_price - tp_distance
                    print(
                        f"[MX] hedge side={hedge_dir} lot={pos['lot']:.2f} sl={sl_price:.3f} tp={tp_price:.3f}"
                    )
                    if not dryrun:
                        hedge_ticket = broker.add_hedge(
                            hedge_dir,
                            pos["lot"],
                            sl_price,
                            tp_price,
                            CFG["SYMBOL"],
                        )
                        if hedge_ticket:
                            state["hedge_used"] = True
                            state["trades_today"] += 1
                            state["last_entry_ts"] = datetime.now()
                            state["open_trades"][hedge_ticket] = {
                                "r_value": stop_distance * dpp * pos["lot"],
                                "breakeven_done": False,
                                "trail_started": False,
                                "direction": hedge_dir,
                            }
                    else:
                        state["hedge_used"] = True
            else:
                if not dryrun:
                    broker.close_position(ticket)
        state["open_trades"][ticket] = trade_info


def attempt_entry(
    market: Dict[str, object],
    account_equity: float,
    symbol_info: Dict[str, float],
    dryrun: bool,
) -> None:
    positions_active = bool(state["open_trades"])
    if positions_active:
        return

    if not cooldown_ready():
        print("[ENTRY] blocked: cooldown active")
        return

    atr_value = market["atr"]
    regime_info = market["regime_info"]
    if atr_value is None:
        print("[ENTRY] blocked: ATR unavailable")
        return
    if regime_info.get("atr_quiet"):
        print("[ENTRY] blocked: ATR below minimum")
        return

    direction = None
    size_factor = 1.0
    if regime_info.get("regime") == "TREND_LONG":
        direction = "LONG"
    elif regime_info.get("regime") == "TREND_SHORT":
        direction = "SHORT"
    elif regime_info.get("micro_bias"):
        direction = regime_info["micro_bias"]
        size_factor = 0.5

    if direction is None:
        print("[ENTRY] blocked: regime not aligned")
        return

    stop_distance = CFG["SL_ATR_MULT"] * atr_value
    tp_distance = CFG["TP_R_MULT"] * stop_distance
    base_lot = risk.lots_for_risk(CFG["SYMBOL"], account_equity, CFG["RISK_PCT_PER_TRADE"], stop_distance)
    lot = base_lot * size_factor
    if lot <= 0:
        print("[ENTRY] blocked: calculated lot size <= 0")
        return

    min_vol = symbol_info.get("volume_min") or 0.0
    if lot < min_vol:
        print(f"[ENTRY] blocked: lot {lot:.2f} below broker minimum {min_vol}")
        return

    tick = broker.get_tick(CFG["SYMBOL"])
    if not tick:
        print("[ENTRY] blocked: tick unavailable")
        return

    entry_price = float(tick.ask if direction == "LONG" else tick.bid)
    sl_price = entry_price - stop_distance if direction == "LONG" else entry_price + stop_distance
    tp_price = entry_price + tp_distance if direction == "LONG" else entry_price - tp_distance

    dpp = dollars_per_price(symbol_info)
    r_value = stop_distance * dpp * lot

    print(
        f"[ENTRY] side={direction} lot={lot:.2f} entry={entry_price:.3f} SL={sl_price:.3f} TP={tp_price:.3f} R=${r_value:.2f}"
    )

    if dryrun:
        state["last_entry_ts"] = datetime.now()
        return

    ticket = broker.send_entry(direction, lot, CFG["SYMBOL"], sl_price=sl_price, tp_price=tp_price)
    if ticket:
        state["trades_today"] += 1
        state["last_entry_ts"] = datetime.now()
        state["hedge_used"] = False
        time.sleep(0.3)
        positions = broker.get_positions(CFG["SYMBOL"])
        for pos in positions:
            if pos["ticket"] == ticket:
                stop_distance_actual = abs(pos["entry_price"] - pos.get("sl", pos["entry_price"]))
                state["open_trades"][ticket] = {
                    "r_value": stop_distance_actual * dpp * pos["lot"],
                    "breakeven_done": False,
                    "trail_started": False,
                    "direction": direction,
                }
                break


def main() -> None:
    args = parse_args()
    dryrun = args.dryrun

    if not broker.init_connection(MT5_LOGIN, MT5_PASSWORD, MT5_SERVER):
        print("[FATAL] Unable to connect to MetaTrader 5")
        return

    account = broker.get_account_info()
    initialize_day(account["equity"], datetime.now().date())

    while True:
        try:
            now = datetime.now()
            account = broker.get_account_info()
            if state["baseline_date"] != now.date():
                initialize_day(account["equity"], now.date())

            symbol_info = broker.get_symbol_info(CFG["SYMBOL"]) or {}
            market = collect_market(CFG["SYMBOL"])
            positions = broker.get_positions(CFG["SYMBOL"])
            dpp = dollars_per_price(symbol_info)
            update_trade_registry(positions, dpp)

            daily_reason = risk.daily_stop(
                account["equity"],
                state["daily_start_equity"],
                gain_limit_pct=CFG["DAILY_TARGET_PCT"],
                drawdown_limit_pct=CFG["DAILY_MAX_DD_PCT"],
            )
            spread_ok = market["spread_points"] <= market["spread_cap"]
            trades_limit_reached = risk.max_trades_reached(state["trades_today"], CFG["MAX_TRADES_PER_DAY"])

            print("-----")
            print(f"[TIME] {now.isoformat(timespec='seconds')}")
            atr_txt = "n/a" if market["atr"] is None else f"{market['atr']:.3f}"
            adx_txt = "n/a" if market["adx"] is None else f"{market['adx']:.1f}"
            regime = market["regime_info"].get("regime")
            print(
                f"[STATE] regime={regime} spread={market['spread_points']}/{market['spread_cap']} "
                f"ATR={atr_txt} ADX={adx_txt}"
            )
            print(
                f"[RISK] equity={account['equity']:.2f} trades_today={state['trades_today']}/{CFG['MAX_TRADES_PER_DAY']} "
                f"daily_stop={daily_reason}"
            )

            gates = []
            if market["atr"] is None:
                gates.append("ATR_unavailable")
            elif market["regime_info"].get("atr_quiet"):
                gates.append("ATR<min")
            if not spread_ok:
                gates.append("spread>cap")
            if daily_reason in {"STOP_GAIN", "STOP_LOSS"}:
                gates.append(f"daily_stop={daily_reason}")
            if trades_limit_reached:
                gates.append("max_trades")
            if len(state["open_trades"]) >= CFG["MAX_CONCURRENT_POS"]:
                gates.append("max_pos")
            log_gate(gates)

            manage_positions(positions, market, symbol_info, dryrun)

            if not gates or gates == ["max_pos"]:
                if spread_ok and daily_reason == "GO" and not trades_limit_reached:
                    attempt_entry(market, account["equity"], symbol_info, dryrun)

            time.sleep(2.0)
        except Exception as exc:  # pragma: no cover
            print(f"[ERROR] {exc}")
            time.sleep(2.0)


if __name__ == "__main__":
    main()
