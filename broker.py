# broker.py — MT5 plumbing (orders enabled, robust stops/BE handling)
import os
import time
from datetime import datetime
import MetaTrader5 as mt5

SYMBOL = "XAUUSDm"
MAGIC  = 20251030

# ---------- Utility ----------
def _norm(price: float, digits: int) -> float:
    f = 10 ** digits
    return int(price * f + 0.5) / f

def _round_volume(vol: float, symbol: str) -> float:
    si = mt5.symbol_info(symbol)
    if not si:
        return float(vol)
    vmin  = getattr(si, "volume_min", 0.0) or 0.0
    vmax  = getattr(si, "volume_max", 0.0) or 1000.0
    vstep = getattr(si, "volume_step", 0.01) or 0.01
    # clamp then snap to step
    vol = max(vmin, min(vmax, vol))
    steps = round(vol / vstep)
    return round(steps * vstep, 8)

def _ensure_symbol(symbol: str) -> bool:
    si = mt5.symbol_info(symbol)
    if si is None:
        return False
    if not si.visible:
        mt5.symbol_select(symbol, True)
    return True

def _tick(symbol: str):
    return mt5.symbol_info_tick(symbol)

def point_size(symbol):
    si = mt5.symbol_info(symbol)
    return si.point if si else None

def symbol_digits(symbol):
    si = mt5.symbol_info(symbol)
    return si.digits if si else None

# ---------- Connect ----------
def init_connection(login=None, password=None, server=None):
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
        info = mt5.account_info()
        if info is None:
            print("[BROKER] No login info provided and terminal not logged in.")
            return False
        print(f"[BROKER] Using existing terminal session: {info.login}")

    _ensure_symbol(SYMBOL)
    return True

# ---------- Info helpers ----------
def get_account_info():
    ai = mt5.account_info()
    if ai is None:
        return {"balance": 0.0, "equity": 0.0, "margin_free": 0.0}
    return {"balance": ai.balance, "equity": ai.equity, "margin_free": ai.margin_free}

def get_symbol_info(symbol):
    si = mt5.symbol_info(symbol)
    if si is None:
        return None
    return {
        "digits": si.digits,
        "point": si.point,
        "trade_contract_size": si.trade_contract_size,
        "trade_stops_level": getattr(si, "trade_stops_level", 0),
        "trade_freeze_level": getattr(si, "trade_freeze_level", 0),
        "volume_min": getattr(si, "volume_min", 0.0),
        "volume_step": getattr(si, "volume_step", 0.01),
    }

def get_spread_points(symbol):
    si = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if si is None or tick is None:
        return 999999
    spread_price = tick.ask - tick.bid
    return int(round(spread_price / si.point))

# ---------- Market data ----------
TF_MAP = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}

def get_ohlc(symbol, timeframe="M1", n=120):
    tf = TF_MAP.get(timeframe, mt5.TIMEFRAME_M1)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)
    out = []
    if rates is None:
        return out
    for r in rates:
        out.append({
            "time": r["time"],
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low":  float(r["low"]),
            "close":float(r["close"]),
            "tick_volume": int(r["tick_volume"])
        })
    return out

# ---------- Positions ----------
def get_positions(symbol):
    ps = mt5.positions_get(symbol=symbol)
    out = []
    if ps is None:
        return out
    for p in ps:
        out.append({
            "ticket": int(p.ticket),
            "direction": "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT",
            "lot": float(p.volume),
            "entry_price": float(p.price_open),
            "pnl_dollars": float(p.profit)
        })
    return out

# ---------- Orders ----------
def _market_price(direction, symbol):
    t = _tick(symbol)
    if not t:
        return None
    return t.ask if direction == "LONG" else t.bid

def _send_deal(request):
    """Send, with graceful retry for INVALID_FILL by switching FOK->IOC."""
    res = mt5.order_send(request)
    if res and res.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
        # Retry with IOC if FOK rejected on this symbol
        req2 = dict(request)
        req2["type_filling"] = mt5.ORDER_FILLING_IOC
        res = mt5.order_send(req2)
    return res

def send_entry(direction, lot, symbol=SYMBOL, comment="TourneyBot", sl=None, tp=None):
    if not _ensure_symbol(symbol):
        print("[ORDER] symbol not ready")
        return None

    si = mt5.symbol_info(symbol)
    t  = _tick(symbol)
    if si is None or t is None:
        print("[ORDER] symbol/tick unavailable")
        return None

    price = _market_price(direction, symbol)
    if price is None:
        print("[ORDER] no price")
        return None

    lot = _round_volume(float(lot), symbol)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL,
        "price": float(price),
        "deviation": 100,
        "magic": MAGIC,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }

    # Optional immediate SL/TP if provided (they will still be validated by server)
    if sl is not None:
        req["sl"] = float(sl)
    if tp is not None:
        req["tp"] = float(tp)

    res = _send_deal(req)
    if res is None:
        print(f"[ORDER] send failed: {mt5.last_error()}")
        return None

    ok_codes = {mt5.TRADE_RETCODE_DONE, getattr(mt5, "TRADE_RETCODE_PLACED", 10008)}
    if res.retcode not in ok_codes:
        print(f"[ORDER] retcode={res.retcode} {getattr(res,'comment','')}")
        return None

    print(f"[ORDER] OPEN {direction} lot={lot} at {price:.3f} ticket={res.order}")
    return res.order  # In your setup this matches the position ticket you use downstream

# ---------- SL to Breakeven (robust) ----------
def modify_stop_to_breakeven(ticket: int, symbol=SYMBOL) -> bool:
    """
    Move SL to entry price while respecting:
      - SYMBOL_TRADE_STOPS_LEVEL (min distance)
      - SYMBOL_TRADE_FREEZE_LEVEL (no modification near market)
    Accepts NO_CHANGES (10025) as success when SL is effectively BE. 
    """
    pos_list = mt5.positions_get(ticket=ticket)
    if not pos_list:
        print(f"[SL] position {ticket} not found")
        return False

    p = pos_list[0]
    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"[SL] symbol {symbol} info not available.")
        return False

    digits = info.digits
    point  = info.point
    stops_level_pts  = getattr(info, "trade_stops_level", 0)
    freeze_level_pts = getattr(info, "trade_freeze_level", 0)

    tick = _tick(symbol)
    if tick is None:
        print("[SL] no tick available; retry next loop.")
        return False

    bid, ask = float(tick.bid), float(tick.ask)
    be_price = float(p.price_open)
    current_sl = float(p.sl) if p.sl else 0.0
    current_tp = float(p.tp) if p.tp else 0.0

    # Compute nearest legal SL beyond stops_level
    buffer_pts = (stops_level_pts + 1)  # add 1pt safety
    if p.type == mt5.POSITION_TYPE_BUY:
        max_allowed_sl = bid - buffer_pts * point
        target_sl = min(be_price, max_allowed_sl)
        if target_sl >= max_allowed_sl:
            target_sl = max_allowed_sl - 0.5 * point
    else:  # SELL
        min_allowed_sl = ask + buffer_pts * point
        target_sl = max(be_price, min_allowed_sl)
        if target_sl <= min_allowed_sl:
            target_sl = min_allowed_sl + 0.5 * point

    target_sl = _norm(target_sl, digits)

    # Already effectively at BE?
    if current_sl and abs(current_sl - target_sl) <= (0.5 * point):
        print(f"[SL] already ~breakeven for {ticket} (SL={current_sl}).")
        return True

    # Freeze check: if inside freeze band, defer
    in_freeze = False
    if freeze_level_pts and freeze_level_pts > 0:
        if p.type == mt5.POSITION_TYPE_BUY:
            in_freeze = (bid - target_sl) < (freeze_level_pts * point)
        else:
            in_freeze = (target_sl - ask) < (freeze_level_pts * point)
    if in_freeze:
        print(f"[SL] inside FREEZE level ({freeze_level_pts} pts). Will retry.")
        return False

    req = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   symbol,
        "position": int(ticket),
        "sl":       float(target_sl),
        "tp":       float(current_tp) if current_tp else 0.0,
        "magic":    MAGIC,
        "comment":  "BE",
        "type_time": mt5.ORDER_TIME_GTC,
        "deviation": 50,
    }
    res = mt5.order_send(req)

    ok_codes = {
        mt5.TRADE_RETCODE_DONE,
        getattr(mt5, "TRADE_RETCODE_PLACED", 10008),
        getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025),  # treat as success
    }

    if res and res.retcode in ok_codes:
        tag = "DONE" if res.retcode == mt5.TRADE_RETCODE_DONE else (
              "PLACED" if res.retcode == getattr(mt5, "TRADE_RETCODE_PLACED", 10008) else "NO_CHANGES")
        print(f"[SL] BE request {tag} for {ticket} | SL->{target_sl}")
        return True

    if res and res.retcode == mt5.TRADE_RETCODE_INVALID_STOPS:
        print(f"[SL] INVALID_STOPS (stops_level={stops_level_pts}pts). "
              f"bid={bid} ask={ask} be={be_price} target={target_sl}")
        return False

    if res and res.retcode == mt5.TRADE_RETCODE_FROZEN:
        print("[SL] FROZEN (freeze level prevents modification). Will retry.")
        return False

    print(f"[SL] modify failed rc={getattr(res,'retcode',None)}")
    return False

# ---------- Close helpers ----------
def close_position(ticket, symbol=SYMBOL):
    pos = next((p for p in mt5.positions_get(symbol=symbol) if p.ticket == ticket), None)
    if pos is None:
        print(f"[CLOSE] position {ticket} not found")
        return False

    side = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = _market_price("SHORT" if pos.type == mt5.POSITION_TYPE_BUY else "LONG", symbol)
    if price is None:
        print("[CLOSE] no price to close")
        return False

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "position": int(ticket),
        "volume": float(pos.volume),
        "type": side,
        "price": float(price),
        "deviation": 100,
        "magic": MAGIC,
        "comment": "CloseByPython",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK
    }
    res = _send_deal(req)
    if res and res.retcode in {mt5.TRADE_RETCODE_DONE, getattr(mt5,"TRADE_RETCODE_PLACED",10008)}:
        print(f"[CLOSE] ticket {ticket} closed at {price:.3f}")
        return True
    print(f"[CLOSE] failed rc={getattr(res,'retcode',None)} {getattr(res,'comment',None)}")
    return False

def add_hedge(direction, lot, symbol=SYMBOL):
    return send_entry(direction, lot, symbol, comment="HEDGE")

def close_all(symbol=SYMBOL):
    ps = mt5.positions_get(symbol=symbol)
    if not ps:
        print("[CLOSEALL] no positions")
        return
    for p in ps:
        close_position(p.ticket, symbol)
