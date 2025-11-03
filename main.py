# main.py — Tournament bot controller (v3.4, orders enabled, 24×5)
import time
from datetime import datetime, timedelta
from collections import deque
from statistics import median

import filters
import risk
import broker

SYMBOL = "XAUUSDm"

# ---- run 24×5
ALWAYS_ON_TRADING = True

# ---- execute real orders
EXECUTE_ORDERS = True

PRESET = "BALANCED"
PRESETS = {
    "BALANCED": {
        "ATR_WINDOW": 10, "MOMENTUM_FACTOR": 1.0,
        "RANGE_LOOKBACK": 20,
        "RANGE_MAX_WIDTH_PIPS": 250.0,
        "SPREAD_LIMIT_POINTS_BASE": 190,        # your live prints ~160 pts
        "DAILY_TARGET_PCT": 4, "MAX_DD_PCT": 6,
        "TAKE_PROFIT_DOLLARS": 0.60, "STOP_LOSS_DOLLARS": 0.30,
        "LOSS_TRIGGER_PIPS": 25,
    },
}

CFG = PRESETS[PRESET]
PIP_VALUE = 0.01
MAX_HEDGE_LAYERS = 3
ENTRY_COOLDOWN_SEC = 10
SPREAD_SAMPLES = deque(maxlen=120)

state = {
    "daily_start_equity": None,
    "hedge_layers": 0,
    "locked_mode": False,
    "locked_loss_value": 0.0,
    "last_entry_ts": None
}

def initialize_day():
    acct = broker.get_account_info()
    equity = acct["equity"]
    state.update({
        "daily_start_equity": equity,
        "hedge_layers": 0,
        "locked_mode": False,
        "locked_loss_value": 0.0,
        "last_entry_ts": None
    })
    print(f"[INIT] New session. Equity baseline = {equity:.2f} USD")

def dynamic_spread_limit():
    base = CFG["SPREAD_LIMIT_POINTS_BASE"]
    if len(SPREAD_SAMPLES) < 30:
        return base
    cap = int(min(max(median(SPREAD_SAMPLES) * 2.3, base * 0.8), 240))
    return cap

def analyze_market():
    candles = broker.get_ohlc(SYMBOL, timeframe="M1", n=120)
    spread_points = broker.get_spread_points(SYMBOL)
    SPREAD_SAMPLES.append(spread_points)
    spread_cap = dynamic_spread_limit()

    atr_val = filters.compute_atr(candles, period=CFG["ATR_WINDOW"])
    in_session = True if ALWAYS_ON_TRADING else filters.session_ok(datetime.now())

    m_state = filters.market_state(
        candles, atr_val,
        momentum_factor=CFG["MOMENTUM_FACTOR"],
        range_lookback=CFG["RANGE_LOOKBACK"],
        range_max_width_pips=CFG["RANGE_MAX_WIDTH_PIPS"],
        pip_value=PIP_VALUE,
        bias_lookback=20,
        adx_period=10, adx_min=20,
        ema_fast=13, ema_slow=34,
        donchian_lkb=14
    )

    spread_ok = risk.check_spread_ok(spread_points, spread_cap)
    point = broker.point_size(SYMBOL) or 0.0
    digits = broker.symbol_digits(SYMBOL)
    spread_usd = spread_points * point

    diag = filters.market_diag(candles, adx_period=10, ema_fast=13, ema_slow=34, donchian_lkb=14)
    print(f"[DIAG] ADX={diag.get('adx'):.1f} | EMA13={diag.get('ema_fast'):.2f} / "
          f"EMA34={diag.get('ema_slow'):.2f} | DCH(Hi/Lo)={diag.get('donchian_hi'):.2f}/"
          f"{diag.get('donchian_lo'):.2f} | close={diag.get('last_close'):.2f}")

    return {
        "market_state": m_state,
        "in_session": in_session,
        "spread_ok": spread_ok,
        "spread_points": spread_points,
        "spread_limit_used": spread_cap,
        "spread_usd": spread_usd,
        "digits": digits,
        "atr": atr_val,
        "candles": candles,
        "adx": diag.get("adx", 0.0)
    }

def evaluate_account():
    acct = broker.get_account_info()
    equity = acct["equity"]
    positions = broker.get_positions(SYMBOL)
    basket_pnl = sum(pos["pnl_dollars"] for pos in positions)

    stop_signal = risk.daily_stop(
        equity, state["daily_start_equity"],
        max_dd_pct=CFG["MAX_DD_PCT"], daily_target_pct=CFG["DAILY_TARGET_PCT"]
    )
    return {"equity": equity, "positions": positions, "basket_pnl": basket_pnl, "stop_signal": stop_signal}

def can_open_now():
    if state["last_entry_ts"] is None:
        return True
    return (datetime.now() - state["last_entry_ts"]) >= timedelta(seconds=ENTRY_COOLDOWN_SEC)

def base_lot_for_equity(equity):
    return risk.base_lot(equity)

# ATR-scaled targets in dollars (used by exit manager that closes via API)
def dynamic_targets(atr_value):
    if atr_value is None:
        return CFG["TAKE_PROFIT_DOLLARS"], CFG["STOP_LOSS_DOLLARS"]
    tp = max(0.30, round(0.35 * atr_value, 2))
    sl = max(0.15, round(0.18 * atr_value, 2))
    return tp, sl

def act_on_positions(market_info, account_info):
    global state
    equity = account_info["equity"]
    positions = account_info["positions"]
    basket_pnl = account_info["basket_pnl"]
    stop_signal = account_info["stop_signal"]

    m_state   = market_info["market_state"]
    spread_ok = market_info["spread_ok"]
    in_session= market_info["in_session"]
    atr_val   = market_info["atr"]
    candles   = market_info["candles"]
    adx_val   = market_info["adx"]

    allow_new_entry = (stop_signal == "GO")

    # ---------- LOCK MODE ----------
    if state["locked_mode"]:
        breakout_confirmed = (m_state in ("TREND_LONG", "TREND_SHORT"))
        lock_action = risk.lock_mode_controller(
            total_pnl=basket_pnl,
            locked_loss_value=state["locked_loss_value"],
            breakout_confirmed=breakout_confirmed
        )
        if lock_action == "CLOSE_ALL_AND_RESET":
            print("[LOCK MODE] Basket recovered >= 0. Close ALL and reset.")
            if EXECUTE_ORDERS:
                broker.close_all(SYMBOL)
            state["hedge_layers"] = 0
            state["locked_mode"] = False
            state["locked_loss_value"] = 0.0
            state["last_entry_ts"] = None
            return
        elif lock_action == "TAKE_RECOVERY_TRADE":
            if allow_new_entry and spread_ok and in_session and can_open_now():
                lot = base_lot_for_equity(equity)
                if m_state == "TREND_LONG":
                    print(f"[RECOVERY] Long recovery entry lot={lot}")
                    if EXECUTE_ORDERS: broker.send_entry("LONG", lot)
                elif m_state == "TREND_SHORT":
                    print(f"[RECOVERY] Short recovery entry lot={lot}")
                    if EXECUTE_ORDERS: broker.send_entry("SHORT", lot)
                state["last_entry_ts"] = datetime.now()
            else:
                print("[RECOVERY] Breakout but gated.")
        else:
            print("[LOCK MODE] Waiting for breakout or recovery.")
        return

    # ---------- MANAGE OPEN TRADES ----------
    tp_dyn, sl_dyn = dynamic_targets(atr_val)
    for pos in positions:
        pnl_dollars = pos["pnl_dollars"]
        decision = risk.manage_open_trade(
            pnl_dollars,
            tp_target=tp_dyn,
            sl_cut=sl_dyn
        )
        if decision == "TAKE_PROFIT":
            print(f"[TRADE {pos['ticket']}] TAKE_PROFIT at pnl ${pnl_dollars:.2f}")
            if EXECUTE_ORDERS: broker.close_position(pos["ticket"])
        elif decision == "BREAKEVEN_SL":
            print(f"[TRADE {pos['ticket']}] Move SL to breakeven.")
            if EXECUTE_ORDERS: broker.modify_stop_to_breakeven(pos["ticket"])
        elif decision == "HEDGE_NOW":
            hedge_action = risk.hedge_controller(state["hedge_layers"], MAX_HEDGE_LAYERS)
            if hedge_action == "ADD_HEDGE":
                new_lot = risk.next_hedge_lot(base_lot_for_equity(equity), state["hedge_layers"])
                side = "SHORT" if pos["direction"] == "LONG" else "LONG"
                print(f"[HEDGE] Add hedge layer {state['hedge_layers']+1} {side} lot={new_lot}")
                if EXECUTE_ORDERS: broker.add_hedge(side, new_lot)
                state["hedge_layers"] += 1
            else:
                print("[HEDGE] Max layers reached. Entering LOCK MODE.")
                state["locked_mode"] = True
                state["locked_loss_value"] = basket_pnl

    if len(positions) > 0:
        return

    # ---------- Range-reversion scalp (quarter size) ----------
    if allow_new_entry and in_session and spread_ok and can_open_now() and atr_val is not None:
        rr_dir = filters.range_reversion_signal(
            candles, adx_period=10, adx_max=20,
            bb_n=20, bb_k=2.0, rsi_n=14, rsi_low=40, rsi_high=60
        )
        if rr_dir in ("LONG", "SHORT"):
            lot = base_lot_for_equity(equity) * 0.25
            print(f"[RR] {rr_dir} range-reversion lot={lot} | TP={tp_dyn} SL={sl_dyn}")
            if EXECUTE_ORDERS: broker.send_entry(rr_dir, lot)
            state["last_entry_ts"] = datetime.now()
            return

    # ---------- Micro-breakout (half → full by ADX) ----------
    if allow_new_entry and in_session and spread_ok and can_open_now() and atr_val is not None:
        micro_dir = filters.micro_signal(
            candles=candles, atr_value=atr_val,
            ema_fast=13, ema_slow=34, adx_period=10, adx_min_micro=14,
            donchian_lkb=14, donchian_touch_k=0.50
        )
        if micro_dir in ("LONG", "SHORT") and (m_state == "UNSURE"):
            lot_base = base_lot_for_equity(equity)
            lot = lot_base * (1.0 if adx_val >= 40 else 0.5)
            print(f"[MICRO] {micro_dir} scalp lot={lot} (ADX={adx_val:.1f}) | TP={tp_dyn} SL={sl_dyn}")
            if EXECUTE_ORDERS: broker.send_entry(micro_dir, lot)
            state["last_entry_ts"] = datetime.now()
            return

    # ---------- Primary trend entries ----------
    if allow_new_entry and in_session and spread_ok and can_open_now():
        entry_plan = risk.plan_new_entry(
            market_state=m_state, spread_ok=spread_ok,
            locked_mode=state["locked_mode"], hedge_layers=state["hedge_layers"]
        )
        if entry_plan in ("LONG", "SHORT"):
            lot = base_lot_for_equity(equity)
            print(f"[ENTRY] Opening {entry_plan} lot={lot} in {m_state} | TP={tp_dyn} SL={sl_dyn}")
            if EXECUTE_ORDERS: broker.send_entry(entry_plan, lot)
            state["last_entry_ts"] = datetime.now()

def main_loop():
    ok = broker.init_connection()
    if not ok:
        print("[FATAL] Could not connect broker.")
        return
    if state["daily_start_equity"] is None:
        initialize_day()

    while True:
        try:
            market_info = analyze_market()
            account_info = evaluate_account()

            print("-----")
            print(f"[TIME] {datetime.now().isoformat(timespec='seconds')}")
            print(f"[STATE] market={market_info['market_state']}, "
                  f"spread_ok={market_info['spread_ok']} "
                  f"(now={market_info['spread_points']:.0f} pts, ~${market_info['spread_usd']:.3f}, "
                  f"cap={market_info['spread_limit_used']} pts, digits={market_info['digits']}), "
                  f"in_session={market_info['in_session']}")
            atr_txt = f"{market_info['atr']:.3f}" if market_info['atr'] is not None else "n/a"
            print(f"[EQUITY] {account_info['equity']:.2f} USD | basket PnL {account_info['basket_pnl']:.2f} USD | stop={account_info['stop_signal']}")
            print(f"[FILTERS] ATR={atr_txt}  MOMO={CFG['MOMENTUM_FACTOR']}  "
                  f"RANGE_LB={CFG['RANGE_LOOKBACK']}  RANGE_W={CFG['RANGE_MAX_WIDTH_PIPS']}")
            print(f"[HEDGE] layers={state['hedge_layers']}  locked_mode={state['locked_mode']}  cooldown={ENTRY_COOLDOWN_SEC}s")

            act_on_positions(market_info, account_info)
            time.sleep(2)
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(2)

if __name__ == "__main__":
    main_loop()
