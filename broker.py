"""broker.py

MetaTrader5 plumbing for the scalper. The module concentrates all direct MT5
interactions: connection, market data retrieval and order operations with
robust stop handling compliant with stop/freeze rules.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Optional, Tuple

import MetaTrader5 as mt5

import filters

SYMBOL = "XAUUSDm"
MAGIC = 20251030

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _norm(price: float, digits: int) -> float:
    factor = 10 ** digits
    return int(price * factor + 0.5) / factor


def _round_volume(vol: float, symbol: str) -> float:
    info = mt5.symbol_info(symbol)
    if info is None:
        return float(vol)
    vol = float(vol)
    v_min = getattr(info, "volume_min", 0.0) or 0.0
    v_max = getattr(info, "volume_max", 1000.0) or 1000.0
    v_step = getattr(info, "volume_step", 0.01) or 0.01
    vol = max(v_min, min(v_max, vol))
    steps = round(vol / v_step)
    return round(steps * v_step, 8)


def _ensure_symbol(symbol: str) -> bool:
    info = mt5.symbol_info(symbol)
    if info is None:
        return False
    if not info.visible:
        mt5.symbol_select(symbol, True)
    return True


def _tick(symbol: str):
    return mt5.symbol_info_tick(symbol)


def point_size(symbol: str):
    info = mt5.symbol_info(symbol)
    return info.point if info else None


def symbol_digits(symbol: str):
    info = mt5.symbol_info(symbol)
    return info.digits if info else None


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def init_connection(login: Optional[int] = None, password: Optional[str] = None, server: Optional[str] = None) -> bool:
    login = login or int(os.getenv("MT5_LOGIN", "0"))
    password = password or os.getenv("MT5_PASSWORD", "")
    server = server or os.getenv("MT5_SERVER", "")

    if not mt5.initialize():
        print(f"[BROKER] MT5 initialize failed: {mt5.last_error()}")
        return False

    if login and password and server:
        if not mt5.login(login=login, password=password, server=server):
            print(f"[BROKER] login failed {mt5.last_error()}")
            return False
        print(f"[BROKER] Connected to {server} as {login}")
    else:
        account = mt5.account_info()
        if account is None:
            print("[BROKER] Terminal not logged in and no credentials provided.")
            return False
        print(f"[BROKER] Using terminal session login={account.login}")

    if not _ensure_symbol(SYMBOL):
        print(f"[BROKER] Unable to select symbol {SYMBOL}")
        return False

    account = mt5.account_info()
    if account:
        print(
            f"[ACCOUNT] balance={account.balance:.2f} equity={account.equity:.2f} "
            f"margin_free={account.margin_free:.2f} currency={account.currency}"
        )
    return True


# ---------------------------------------------------------------------------
# Market / account information
# ---------------------------------------------------------------------------

def get_account_info() -> dict:
    info = mt5.account_info()
    if info is None:
        return {"balance": 0.0, "equity": 0.0, "margin_free": 0.0}
    return {
        "balance": float(info.balance),
        "equity": float(info.equity),
        "margin_free": float(info.margin_free),
    }


def get_symbol_info(symbol: str) -> Optional[dict]:
    info = mt5.symbol_info(symbol)
    if info is None:
        return None
    return {
        "digits": info.digits,
        "point": info.point,
        "trade_contract_size": getattr(info, "trade_contract_size", 1.0),
        "trade_stops_level": getattr(info, "trade_stops_level", 0),
        "trade_freeze_level": getattr(info, "trade_freeze_level", 0),
        "volume_min": getattr(info, "volume_min", 0.0),
        "volume_step": getattr(info, "volume_step", 0.01),
    }


def get_spread_points(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    tick = _tick(symbol)
    if info is None or tick is None:
        return 999_999
    spread_price = float(tick.ask) - float(tick.bid)
    return int(round(spread_price / info.point))


_TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
}


def get_ohlc(symbol: str, timeframe: str = "M1", n: int = 120):
    tf = _TIMEFRAME_MAP.get(timeframe, mt5.TIMEFRAME_M1)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)
    candles = []
    if rates is None:
        return candles
    for r in rates:
        candles.append(
            {
                "time": r["time"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
            }
        )
    return candles


def get_positions(symbol: str):
    positions = mt5.positions_get(symbol=symbol)
    out = []
    if not positions:
        return out
    for pos in positions:
        out.append(
            {
                "ticket": int(pos.ticket),
                "direction": "LONG" if pos.type == mt5.POSITION_TYPE_BUY else "SHORT",
                "lot": float(pos.volume),
                "entry_price": float(pos.price_open),
                "sl": float(pos.sl) if pos.sl else 0.0,
                "tp": float(pos.tp) if pos.tp else 0.0,
                "pnl_dollars": float(pos.profit),
                "comment": pos.comment,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Stop/target calculations
# ---------------------------------------------------------------------------

def _atr_price_targets(
    direction: str,
    price: float,
    lot: float,
    symbol: str,
    info,
    sl: Optional[float],
    tp: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    if sl is not None and tp is not None:
        return sl, tp

    candles = get_ohlc(symbol, timeframe="M1", n=120)
    atr_price = filters.compute_atr(candles, period=10)
    contract_size = getattr(info, "trade_contract_size", 1.0)
    atr_dollars = None
    if atr_price is not None:
        atr_dollars = atr_price * contract_size * lot

    if atr_dollars is None:
        # Fallback to modest static values
        tp_dollars = 0.60
        sl_dollars = 0.30
    else:
        tp_dollars = min(1.20, max(0.30, 0.35 * atr_dollars))
        sl_dollars = min(0.60, max(0.15, 0.18 * atr_dollars))

    if contract_size <= 0 or lot <= 0:
        price_per_dollar = getattr(info, "point", 0.01)
    else:
        # For a position, pnl = price_diff * contract_size * lot
        price_per_dollar = 1.0 / (contract_size * lot)

    point = getattr(info, "point", 0.01)
    digits = getattr(info, "digits", 2)

    if sl is None:
        sl_distance = sl_dollars * price_per_dollar
        if direction == "LONG":
            sl = _norm(price - sl_distance, digits)
        else:
            sl = _norm(price + sl_distance, digits)
    if tp is None:
        tp_distance = tp_dollars * price_per_dollar
        if direction == "LONG":
            tp = _norm(price + tp_distance, digits)
        else:
            tp = _norm(price - tp_distance, digits)
    return sl, tp


def _apply_distance_guards(
    direction: str,
    price: float,
    sl: Optional[float],
    tp: Optional[float],
    info,
    extra_points: int = 0,
) -> Tuple[Optional[float], Optional[float]]:
    if sl is None and tp is None:
        return sl, tp

    point = getattr(info, "point", 0.01)
    digits = getattr(info, "digits", 2)
    stops_level = getattr(info, "trade_stops_level", 0)
    freeze_level = getattr(info, "trade_freeze_level", 0)

    min_buffer_points = stops_level + 1 + extra_points
    freeze_buffer_points = freeze_level + extra_points
    min_buffer = max(min_buffer_points, freeze_buffer_points) * point

    if direction == "LONG":
        if sl is not None:
            sl = min(price - min_buffer, sl)
            sl = min(sl, price - point)
            sl = _norm(sl, digits)
        if tp is not None:
            tp = max(price + min_buffer, tp)
            tp = max(tp, price + point)
            tp = _norm(tp, digits)
    else:
        if sl is not None:
            sl = max(price + min_buffer, sl)
            sl = max(sl, price + point)
            sl = _norm(sl, digits)
        if tp is not None:
            tp = min(price - min_buffer, tp)
            tp = min(tp, price - point)
            tp = _norm(tp, digits)
    return sl, tp


def _resolve_position_ticket(preferred_ticket: int, symbol: str, lot: float, comment: str) -> Optional[int]:
    # Try direct lookup first
    if preferred_ticket:
        pos = mt5.positions_get(ticket=preferred_ticket)
        if pos:
            return preferred_ticket
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return None
    # fallback: find latest by comment + lot
    candidates = [
        p for p in positions
        if abs(p.volume - lot) <= 1e-6 and p.comment == comment and p.magic == MAGIC
    ]
    if candidates:
        return candidates[-1].ticket
    return positions[-1].ticket


def _send_deal(request: dict):
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
        retry = dict(request)
        retry["type_filling"] = mt5.ORDER_FILLING_IOC
        result = mt5.order_send(retry)
    return result


def _ensure_sl_tp(ticket: int, sl: Optional[float], tp: Optional[float], info) -> None:
    if ticket is None:
        return
    attempt = 0
    while attempt < 2:
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return
        pos = pos[0]
        need_update = False
        req_sl = sl
        req_tp = tp
        if req_sl and (pos.sl == 0 or abs(pos.sl - req_sl) > info.point * 1.5):
            need_update = True
        if req_tp and (pos.tp == 0 or abs(pos.tp - req_tp) > info.point * 1.5):
            need_update = True
        if not need_update:
            return
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": float(req_sl) if req_sl else 0.0,
            "tp": float(req_tp) if req_tp else 0.0,
            "magic": MAGIC,
            "comment": "init-stops",
            "type_time": mt5.ORDER_TIME_GTC,
            "deviation": 50,
        }
        res = mt5.order_send(request)
        print(
            f"[SLTP] attach retcode={getattr(res, 'retcode', None)} ticket={ticket} "
            f"sl={req_sl} tp={req_tp}"
        )
        if res and res.retcode in {
            mt5.TRADE_RETCODE_DONE,
            getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
            getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025),
        }:
            return
        if res and res.retcode == mt5.TRADE_RETCODE_FROZEN:
            print("[SLTP] FROZEN when attaching stops. Retrying after 3.5s")
            time.sleep(3.5)
        else:
            # Soften distances slightly and retry once
            sl_soft, tp_soft = _apply_distance_guards(
                "LONG" if pos.type == mt5.POSITION_TYPE_BUY else "SHORT",
                pos.price_open,
                req_sl,
                req_tp,
                info,
                extra_points=3,
            )
            request["sl"] = float(sl_soft) if sl_soft else 0.0
            request["tp"] = float(tp_soft) if tp_soft else 0.0
        attempt += 1


# ---------------------------------------------------------------------------
# Order operations
# ---------------------------------------------------------------------------

def send_entry(
    direction: str,
    lot: float,
    symbol: str = SYMBOL,
    comment: str = "ScalperEntry",
    sl: Optional[float] = None,
    tp: Optional[float] = None,
) -> Optional[int]:
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")

    if not _ensure_symbol(symbol):
        print("[ORDER] symbol not ready")
        return None

    info = mt5.symbol_info(symbol)
    tick = _tick(symbol)
    if info is None or tick is None:
        print("[ORDER] symbol info or tick unavailable")
        return None

    price = float(tick.ask if direction == "LONG" else tick.bid)
    lot = _round_volume(lot, symbol)

    sl, tp = _atr_price_targets(direction, price, lot, symbol, info, sl, tp)

    attempt = 0
    extra_points = 0
    ticket = None
    while attempt < 3:
        adj_sl, adj_tp = _apply_distance_guards(direction, price, sl, tp, info, extra_points=extra_points)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lot),
            "type": mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL,
            "price": price,
            "deviation": 100,
            "magic": MAGIC,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
            "sl": float(adj_sl) if adj_sl else 0.0,
            "tp": float(adj_tp) if adj_tp else 0.0,
        }
        result = _send_deal(request)
        retcode = getattr(result, "retcode", None)
        print(
            f"[ORDER] action=OPEN dir={direction} lot={lot} price={price:.3f} "
            f"sl={adj_sl} tp={adj_tp} retcode={retcode}"
        )
        if result is None:
            print(f"[ORDER] send failed error={mt5.last_error()}")
            break
        if result and retcode in {
            mt5.TRADE_RETCODE_DONE,
            getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
        }:
            preferred_ticket = getattr(result, "order", 0) or getattr(result, "deal", 0)
            time.sleep(0.25)
            ticket = _resolve_position_ticket(preferred_ticket, symbol, lot, comment)
            _ensure_sl_tp(ticket, adj_sl, adj_tp, info)
            return ticket

        if retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
            extra_points += 3
            print(
                f"[ORDER] INVALID_STOPS enforcing buffer extra_points={extra_points} "
                f"stops_level={getattr(info, 'trade_stops_level', 0)} freeze={getattr(info, 'trade_freeze_level', 0)}"
            )
            tick = _tick(symbol)
            if tick:
                price = float(tick.ask if direction == "LONG" else tick.bid)
            attempt += 1
            continue
        if retcode == mt5.TRADE_RETCODE_REQUOTE:
            tick = _tick(symbol)
            if not tick:
                break
            price = float(tick.ask if direction == "LONG" else tick.bid)
            attempt += 1
            continue
        if retcode == mt5.TRADE_RETCODE_FROZEN:
            print("[ORDER] FROZEN entry attempt. Waiting 0.5s before retry")
            time.sleep(0.5)
            attempt += 1
            continue
        break
    return ticket


def modify_stop_to_breakeven(ticket: int, symbol: str = SYMBOL) -> bool:
    pos_list = mt5.positions_get(ticket=ticket)
    if not pos_list:
        print(f"[SL] position {ticket} not found")
        return False

    pos = pos_list[0]
    info = mt5.symbol_info(symbol)
    tick = _tick(symbol)
    if info is None or tick is None:
        print("[SL] symbol info/tick unavailable")
        return False

    point = info.point
    digits = info.digits
    stops_level = getattr(info, "trade_stops_level", 0)
    freeze_level = getattr(info, "trade_freeze_level", 0)
    buffer_points = stops_level + 1
    price_open = pos.price_open

    if pos.type == mt5.POSITION_TYPE_BUY:
        market_ref = float(tick.bid)
        target_sl = min(price_open, market_ref - (buffer_points * point))
    else:
        market_ref = float(tick.ask)
        target_sl = max(price_open, market_ref + (buffer_points * point))

    target_sl = _norm(target_sl, digits)
    current_tp = float(pos.tp) if pos.tp else 0.0

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": ticket,
        "sl": float(target_sl),
        "tp": current_tp,
        "magic": MAGIC,
        "comment": "breakeven",
        "type_time": mt5.ORDER_TIME_GTC,
        "deviation": 50,
    }

    res = mt5.order_send(request)
    retcode = getattr(res, "retcode", None)
    print(f"[SL] breakeven retcode={retcode} ticket={ticket} sl->{target_sl}")
    if res and retcode in {
        mt5.TRADE_RETCODE_DONE,
        getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
        getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025),
    }:
        return True

    if retcode == mt5.TRADE_RETCODE_FROZEN:
        print("[SL] FROZEN near market. Re-queuing after 3.2s")
        time.sleep(3.2)
        res_retry = mt5.order_send(request)
        retcode_retry = getattr(res_retry, "retcode", None)
        print(f"[SL] retry retcode={retcode_retry} ticket={ticket}")
        return bool(
            res_retry
            and retcode_retry
            in {
                mt5.TRADE_RETCODE_DONE,
                getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
                getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025),
            }
        )

    if retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
        print(
            f"[SL] INVALID_STOPS when moving to BE. stops_level={stops_level} freeze={freeze_level}"
        )
    return False


def close_position(ticket: int, symbol: str = SYMBOL) -> bool:
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        print(f"[CLOSE] position {ticket} not found")
        return False
    pos = pos[0]
    tick = _tick(symbol)
    if tick is None:
        print("[CLOSE] tick unavailable")
        return False
    if pos.type == mt5.POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
        close_price = float(tick.bid)
        close_direction = "SELL"
    else:
        order_type = mt5.ORDER_TYPE_BUY
        close_price = float(tick.ask)
        close_direction = "BUY"
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "position": ticket,
        "volume": float(pos.volume),
        "type": order_type,
        "price": close_price,
        "deviation": 100,
        "magic": MAGIC,
        "comment": "close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    res = _send_deal(request)
    retcode = getattr(res, "retcode", None)
    print(f"[CLOSE] ticket={ticket} direction={close_direction} price={close_price:.3f} retcode={retcode}")
    return bool(res and retcode in {mt5.TRADE_RETCODE_DONE, getattr(mt5, "TRADE_RETCODE_PLACED", 10008)})


def add_hedge(direction: str, lot: float, symbol: str = SYMBOL) -> Optional[int]:
    return send_entry(direction, lot, symbol, comment="HEDGE")


def close_all(symbol: str = SYMBOL) -> None:
    while True:
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            print("[CLOSEALL] no positions left")
            return
        for pos in positions:
            success = close_position(pos.ticket, symbol)
            if not success:
                print(f"[CLOSEALL] retry closing {pos.ticket} after 0.5s")
                time.sleep(0.5)
        time.sleep(0.2)
