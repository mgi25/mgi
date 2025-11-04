"""broker.py

MetaTrader5 plumbing for the scalper. The module concentrates all direct MT5
interactions: connection, market data retrieval and order operations with
robust stop handling compliant with stop/freeze rules.
"""
from __future__ import annotations

import os
import time
from typing import Optional, Tuple

import MetaTrader5 as mt5

import config

SYMBOL = config.CFG["SYMBOL"]
MAGIC = 20251030

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _norm(price: float, digits: int) -> float:
    factor = 10 ** digits
    return int(price * factor + 0.5) / factor


def _snap_to_step(volume: float, step: float) -> float:
    if step <= 0:
        return float(volume)
    steps = round(float(volume) / step)
    return round(steps * step, 8)


def _round_volume(vol: float, symbol: str) -> float:
    info = mt5.symbol_info(symbol)
    if info is None:
        return float(vol)
    vol = float(vol)
    v_min = getattr(info, "volume_min", 0.0) or 0.0
    v_max = getattr(info, "volume_max", 1000.0) or 1000.0
    v_step = getattr(info, "volume_step", 0.01) or 0.01
    vol = max(v_min, min(v_max, vol))
    snapped = _snap_to_step(vol, v_step)
    return max(v_min, min(v_max, snapped))


def _ensure_symbol(symbol: str) -> bool:
    info = mt5.symbol_info(symbol)
    if info is None:
        return False
    if not info.visible:
        mt5.symbol_select(symbol, True)
    return True


def _tick(symbol: str):
    return mt5.symbol_info_tick(symbol)


def get_tick(symbol: str = SYMBOL):
    return _tick(symbol)


def point_size(symbol: str):
    info = mt5.symbol_info(symbol)
    return info.point if info else None


def symbol_digits(symbol: str):
    info = mt5.symbol_info(symbol)
    return info.digits if info else None


def make_legal_sl_tp(
    direction: str,
    entry_price: float,
    sl_price: Optional[float],
    tp_price: Optional[float],
    symbol: str = SYMBOL,
) -> Tuple[Optional[float], Optional[float]]:
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return sl_price, tp_price

    digits = info.digits
    point = info.point
    stops = getattr(info, "trade_stops_level", 0) * point
    freeze = getattr(info, "trade_freeze_level", 0) * point
    bid = float(getattr(tick, "bid", 0.0))
    ask = float(getattr(tick, "ask", 0.0))
    buffer = point

    def _norm_local(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        return _norm(value, digits)

    if direction == "LONG":
        if sl_price is not None:
            min_sl = min(bid - stops - buffer, entry_price - buffer)
            if freeze:
                min_sl = min(min_sl, bid - freeze - buffer)
            if sl_price > min_sl:
                print(
                    f"[SLTP] adjust SL LONG from {sl_price} to {min_sl} due to stops/freeze"
                )
                sl_price = min_sl
        if tp_price is not None:
            min_tp = max(ask + stops + buffer, entry_price + buffer)
            if freeze:
                min_tp = max(min_tp, ask + freeze + buffer)
            if tp_price < min_tp:
                print(
                    f"[SLTP] adjust TP LONG from {tp_price} to {min_tp} due to stops/freeze"
                )
                tp_price = min_tp
    else:
        if sl_price is not None:
            min_sl = max(ask + stops + buffer, entry_price + buffer)
            if freeze:
                min_sl = max(min_sl, ask + freeze + buffer)
            if sl_price < min_sl:
                print(
                    f"[SLTP] adjust SL SHORT from {sl_price} to {min_sl} due to stops/freeze"
                )
                sl_price = min_sl
        if tp_price is not None:
            min_tp = min(bid - stops - buffer, entry_price - buffer)
            if freeze:
                min_tp = min(min_tp, bid - freeze - buffer)
            if tp_price > min_tp:
                print(
                    f"[SLTP] adjust TP SHORT from {tp_price} to {min_tp} due to stops/freeze"
                )
                tp_price = min_tp

    return _norm_local(sl_price), _norm_local(tp_price)


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
        "trade_tick_value": getattr(info, "trade_tick_value", 0.0),
        "trade_tick_size": getattr(info, "trade_tick_size", 0.0),
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


# ---------------------------------------------------------------------------
# Order operations
# ---------------------------------------------------------------------------

def send_entry(
    direction: str,
    lot: float,
    symbol: str = SYMBOL,
    comment: str = "ScalperEntry",
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
) -> Optional[int]:
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    if sl_price is None or tp_price is None:
        print("[ORDER] SL/TP must be provided for entry")
        return None

    if not _ensure_symbol(symbol):
        print("[ORDER] symbol not ready")
        return None

    info = mt5.symbol_info(symbol)
    if info is None:
        print("[ORDER] symbol info unavailable")
        return None

    lot = _round_volume(lot, symbol)

    attempt = 0
    ticket: Optional[int] = None
    while attempt < 3:
        tick = _tick(symbol)
        if tick is None:
            print("[ORDER] tick unavailable during entry")
            break
        price = float(tick.ask if direction == "LONG" else tick.bid)
        adj_sl, adj_tp = make_legal_sl_tp(direction, price, sl_price, tp_price, symbol)
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
        if result and retcode in {
            mt5.TRADE_RETCODE_DONE,
            getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
        }:
            preferred_ticket = getattr(result, "order", 0) or getattr(result, "deal", 0)
            time.sleep(0.25)
            ticket = _resolve_position_ticket(preferred_ticket, symbol, lot, comment)
            return ticket

        if retcode == mt5.TRADE_RETCODE_REQUOTE:
            attempt += 1
            continue
        if retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
            buffer = max(info.point, getattr(info, "trade_stops_level", 0) * info.point)
            if direction == "LONG":
                sl_price = (adj_sl or sl_price) - buffer
                tp_price = (adj_tp or tp_price) + buffer
            else:
                sl_price = (adj_sl or sl_price) + buffer
                tp_price = (adj_tp or tp_price) - buffer
            print(
                f"[ORDER] INVALID_STOPS adjust buffer={buffer} stops={getattr(info, 'trade_stops_level', 0)} "
                f"freeze={getattr(info, 'trade_freeze_level', 0)}"
            )
            attempt += 1
            continue
        if retcode == mt5.TRADE_RETCODE_FROZEN:
            print("[ORDER] FROZEN entry attempt. Waiting 0.5s before retry")
            time.sleep(0.5)
            attempt += 1
            continue
        if result is None:
            print(f"[ORDER] send failed error={mt5.last_error()}")
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

    direction = "LONG" if pos.type == mt5.POSITION_TYPE_BUY else "SHORT"
    entry_price = float(pos.price_open)
    current_tp = float(pos.tp) if pos.tp else 0.0
    target_sl = entry_price

    legal_sl, _ = make_legal_sl_tp(direction, entry_price, target_sl, current_tp or None, symbol)
    if legal_sl is None:
        print("[SL] unable to compute legal breakeven stop")
        return False

    point = info.point
    freeze = getattr(info, "trade_freeze_level", 0) * point
    stops = getattr(info, "trade_stops_level", 0) * point
    min_distance = max(freeze, stops) + point

    if direction == "LONG":
        market_ref = float(tick.bid)
        if market_ref - legal_sl < min_distance:
            print(
                f"[SL] breakeven defer freeze-level distance={market_ref - legal_sl:.5f} "
                f"min_required={min_distance:.5f}"
            )
            return False
        if pos.sl and abs(float(pos.sl) - legal_sl) <= point * 0.5:
            print(f"[SL] breakeven already set ticket={ticket}")
            return True
    else:
        market_ref = float(tick.ask)
        if legal_sl - market_ref < min_distance:
            print(
                f"[SL] breakeven defer freeze-level distance={legal_sl - market_ref:.5f} "
                f"min_required={min_distance:.5f}"
            )
            return False
        if pos.sl and abs(float(pos.sl) - legal_sl) <= point * 0.5:
            print(f"[SL] breakeven already set ticket={ticket}")
            return True

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": ticket,
        "sl": float(legal_sl),
        "tp": current_tp,
        "magic": MAGIC,
        "comment": "breakeven",
        "type_time": mt5.ORDER_TIME_GTC,
        "deviation": 50,
    }

    res = mt5.order_send(request)
    retcode = getattr(res, "retcode", None)
    print(f"[SL] breakeven retcode={retcode} ticket={ticket} sl->{legal_sl}")
    if res and retcode in {
        mt5.TRADE_RETCODE_DONE,
        getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
    }:
        return True
    if retcode in {
        getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025),
        mt5.TRADE_RETCODE_FROZEN,
    }:
        print(f"[SL] breakeven deferred retcode={retcode}")
        return False
    if retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
        print(
            f"[SL] INVALID_STOPS when moving to BE. stops={stops/info.point if info.point else 0} "
            f"freeze={freeze/info.point if info.point else 0}"
        )
    return False


def trail_stop(ticket: int, trail_price: float, symbol: str = SYMBOL) -> bool:
    pos_list = mt5.positions_get(ticket=ticket)
    if not pos_list:
        print(f"[SL] trail position {ticket} not found")
        return False

    pos = pos_list[0]
    info = mt5.symbol_info(symbol)
    tick = _tick(symbol)
    if info is None or tick is None:
        print("[SL] trail symbol info/tick unavailable")
        return False

    direction = "LONG" if pos.type == mt5.POSITION_TYPE_BUY else "SHORT"
    current_tp = float(pos.tp) if pos.tp else 0.0
    current_sl = float(pos.sl) if pos.sl else None

    if direction == "LONG" and current_sl is not None and trail_price <= current_sl:
        return False
    if direction == "SHORT" and current_sl is not None and trail_price >= current_sl:
        return False

    legal_sl, _ = make_legal_sl_tp(direction, float(pos.price_open), trail_price, current_tp or None, symbol)
    if legal_sl is None:
        print("[SL] unable to compute legal trail price")
        return False

    point = info.point
    freeze = getattr(info, "trade_freeze_level", 0) * point
    stops = getattr(info, "trade_stops_level", 0) * point
    min_distance = max(freeze, stops) + point

    if direction == "LONG":
        market_ref = float(tick.bid)
        if market_ref - legal_sl < min_distance:
            print(
                f"[SL] trail defer freeze-level distance={market_ref - legal_sl:.5f} "
                f"min_required={min_distance:.5f}"
            )
            return False
    else:
        market_ref = float(tick.ask)
        if legal_sl - market_ref < min_distance:
            print(
                f"[SL] trail defer freeze-level distance={legal_sl - market_ref:.5f} "
                f"min_required={min_distance:.5f}"
            )
            return False

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": ticket,
        "sl": float(legal_sl),
        "tp": current_tp,
        "magic": MAGIC,
        "comment": "trail",
        "type_time": mt5.ORDER_TIME_GTC,
        "deviation": 50,
    }

    res = mt5.order_send(request)
    retcode = getattr(res, "retcode", None)
    print(f"[SL] trail retcode={retcode} ticket={ticket} sl->{legal_sl}")
    if res and retcode in {
        mt5.TRADE_RETCODE_DONE,
        getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
    }:
        return True
    if retcode in {
        getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025),
        mt5.TRADE_RETCODE_FROZEN,
    }:
        print(f"[SL] trail deferred retcode={retcode}")
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


def add_hedge(
    direction: str,
    lot: float,
    sl_price: float,
    tp_price: float,
    symbol: str = SYMBOL,
) -> Optional[int]:
    return send_entry(direction, lot, symbol, comment="HEDGE", sl_price=sl_price, tp_price=tp_price)


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
