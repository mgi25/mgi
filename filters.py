# filters.py — Market condition analysis (v3.3)
from datetime import time
from statistics import mean

# ---------- Pips helper ----------
def price_to_pips(diff_price, pip_value=0.01):  # XAUUSD: $0.01 per pip typical (Exness feeds often 3 "points" = $0.001)
    return diff_price / pip_value

# ---------- ATR ----------
def compute_true_range(prev_close, high, low):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))

def compute_atr(candles, period=14):
    if not candles or len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        pc = candles[i - 1]['close']
        h, l = candles[i]['high'], candles[i]['low']
        trs.append(compute_true_range(pc, h, l))
    if len(trs) < period:
        return None
    return mean(trs[-period:])

# ---------- Single-bar momentum ----------
def is_momentum_bull(candles, atr_value, momentum_factor=1.0):
    if atr_value is None or not candles: return False
    last = candles[-1]; body = last['close'] - last['open']
    return body > 0 and abs(body) >= momentum_factor * atr_value

def is_momentum_bear(candles, atr_value, momentum_factor=1.0):
    if atr_value is None or not candles: return False
    last = candles[-1]; body = last['close'] - last['open']
    return body < 0 and abs(body) >= momentum_factor * atr_value

# ---------- Range detection ----------
def detect_range(candles, lookback_bars=20, max_width_pips=250.0, pip_value=0.01):
    if not candles or len(candles) < lookback_bars:
        return (False, None, None)
    win = candles[-lookback_bars:]
    hi = max(c['high'] for c in win)
    lo = min(c['low'] for c in win)
    width_pips = price_to_pips(hi - lo, pip_value=pip_value)
    return (width_pips <= max_width_pips, hi, lo)

# ---------- Bias ----------
def dominant_bias(candles, lookback_bars=20):
    if not candles or len(candles) < lookback_bars:
        return "NONE"
    win = candles[-lookback_bars:]
    bull = bear = 0.0
    for c in win:
        o, h, l, cl = c['open'], c['high'], c['low'], c['close']
        body = cl - o
        upper = h - max(o, cl)
        lower = min(o, cl) - l
        if body > 0:
            bull += max(body - max(upper, 0.0), 0.0)
        elif body < 0:
            bear += max(abs(body) - max(lower, 0.0), 0.0)
    if bull > bear * 1.2:  return "BULL"
    if bear > bull * 1.2:  return "BEAR"
    return "NONE"

# ---------- Session (kept for compatibility) ----------
def session_ok(now_dt):
    t = now_dt.time()
    london_start, london_end = time(12, 30), time(16, 30)
    ny_start, ny_end         = time(18, 30), time(22, 30)
    return (london_start <= t <= london_end) or (ny_start <= t <= ny_end)

# ---------- TA helpers ----------
def _ema_series(values, period):
    if not values: return []
    k = 2.0 / (period + 1.0)
    out = []; ema = values[0]
    for v in values:
        ema = v * k + ema * (1 - k)
        out.append(ema)
    return out

def _donchian_hilo(candles, lookback=14):
    if not candles or len(candles) < lookback:
        return None, None
    win = candles[-lookback:]
    return max(c['high'] for c in win), min(c['low'] for c in win)

def _rma(seq, n):
    if len(seq) < n: return None
    out = []; r = sum(seq[:n]) / n; out.append(r)
    for x in seq[n:]:
        r = (r * (n - 1) + x) / n
        out.append(r)
    return out

def _compute_adx(candles, period=14):
    if not candles or len(candles) < period + 2:
        return None
    highs  = [c['high']  for c in candles]
    lows   = [c['low']   for c in candles]
    closes = [c['close'] for c in candles]
    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        tr_i = max(highs[i] - lows[i],
                   abs(highs[i] - closes[i - 1]),
                   abs(lows[i]  - closes[i - 1]))
        tr.append(tr_i)
        plus_dm.append(up   if (up > down and up   > 0) else 0.0)
        minus_dm.append(down if (down > up  and down > 0) else 0.0)
    tr_n, plus_n, minus_n = _rma(tr, period), _rma(plus_dm, period), _rma(minus_dm, period)
    if not tr_n or not plus_n or not minus_n: return None
    k = min(len(tr_n), len(plus_n), len(minus_n))
    tr_n, plus_n, minus_n = tr_n[-k:], plus_n[-k:], minus_n[-k:]
    plus_di  = [0 if t == 0 else 100 * (p / t) for p, t in zip(plus_n, tr_n)]
    minus_di = [0 if t == 0 else 100 * (m / t) for m, t in zip(minus_n, tr_n)]
    dx = []
    for p, m in zip(plus_di, minus_di):
        den = p + m
        dx.append(0 if den == 0 else 100 * abs(p - m) / den)
    adx_series = _rma(dx, period)
    return adx_series[-1] if adx_series else None

# ---------- Bollinger Bands ----------
def _sma(values, n):
    if len(values) < n: return None
    return sum(values[-n:]) / n

def _std(values, n):
    if len(values) < n: return None
    m = _sma(values, n)
    var = sum((v - m) ** 2 for v in values[-n:]) / n
    return var ** 0.5

def bollinger_bands(closes, n=20, k=2.0):
    if len(closes) < n: return (None, None, None)
    mid = _sma(closes, n); sd = _std(closes, n)
    if sd is None: return (None, None, None)
    return (mid + k * sd, mid, mid - k * sd)

# ---------- Regime decision ----------
def market_state(
    candles, atr_value,
    momentum_factor=1.0,
    range_lookback=20, range_max_width_pips=250.0, pip_value=0.01,
    bias_lookback=20,
    adx_period=10, adx_min=20,
    ema_fast=13, ema_slow=34,
    donchian_lkb=14
):
    if not candles or len(candles) < max(ema_slow, donchian_lkb) + 2:
        return "UNSURE"

    in_range, _, _ = detect_range(
        candles, lookback_bars=range_lookback,
        max_width_pips=range_max_width_pips, pip_value=pip_value
    )
    if in_range:
        return "RANGING"

    closes = [c['close'] for c in candles]
    hi_dch, lo_dch = _donchian_hilo(candles, lookback=donchian_lkb)
    last_close = closes[-1]
    breakout_long  = (hi_dch is not None) and (last_close > hi_dch)
    breakout_short = (lo_dch is not None) and (last_close < lo_dch)

    ef = _ema_series(closes, ema_fast); es = _ema_series(closes, ema_slow)
    if not ef or not es: return "UNSURE"
    fast, slow = ef[-1], es[-1]
    ema_long, ema_short = (fast > slow), (fast < slow)

    adx_val = _compute_adx(candles, period=adx_period)
    adx_ok = (adx_val is not None and adx_val >= adx_min)

    # Optional fast-path: very strong trend without Donchian print
    if adx_val is not None and adx_val >= 30:
        if ema_long:  return "TREND_LONG"
        if ema_short: return "TREND_SHORT"

    if breakout_long and ema_long and adx_ok:  return "TREND_LONG"
    if breakout_short and ema_short and adx_ok: return "TREND_SHORT"

    bias_dir = dominant_bias(candles, lookback_bars=bias_lookback)
    if bias_dir == "BULL" and is_momentum_bull(candles, atr_value, momentum_factor): return "TREND_LONG"
    if bias_dir == "BEAR" and is_momentum_bear(candles, atr_value, momentum_factor): return "TREND_SHORT"
    return "UNSURE"

# ---------- Diagnostics ----------
def market_diag(candles, adx_period=10, ema_fast=13, ema_slow=34, donchian_lkb=14):
    out = {"adx": 0.0, "ema_fast": None, "ema_slow": None,
           "donchian_hi": None, "donchian_lo": None, "last_close": None}
    if not candles: return out
    closes = [c['close'] for c in candles]
    out["last_close"] = closes[-1]
    out["adx"] = _compute_adx(candles, period=adx_period) or 0.0
    ef = _ema_series(closes, ema_fast); es = _ema_series(closes, ema_slow)
    out["ema_fast"] = ef[-1] if ef else None
    out["ema_slow"] = es[-1] if es else None
    hi, lo = _donchian_hilo(candles, lookback=donchian_lkb)
    out["donchian_hi"], out["donchian_lo"] = hi, lo
    return out

# ---------- Micro-breakout (half/full size by ADX) ----------
def micro_signal(
    candles, atr_value,
    ema_fast=13, ema_slow=34,
    adx_period=10, adx_min_micro=14,
    donchian_lkb=14, donchian_touch_k=0.50
):
    if not candles or atr_value is None or len(candles) < max(ema_slow, donchian_lkb) + 1:
        return None
    closes = [c['close'] for c in candles]
    last_close = closes[-1]
    ef = _ema_series(closes, ema_fast); es = _ema_series(closes, ema_slow)
    if not ef or not es: return None
    ema_f, ema_s = ef[-1], es[-1]
    adx_val = (_compute_adx(candles, period=adx_period) or 0.0)
    if adx_val < adx_min_micro: return None
    hi_dch, lo_dch = _donchian_hilo(candles, lookback=donchian_lkb)
    if hi_dch is None or lo_dch is None: return None
    proximity = 0.50 * (atr_value if atr_value is not None else 0.0)
    if (ema_f > ema_s) and (last_close > ema_f) and (last_close > (hi_dch - proximity)): return "LONG"
    if (ema_f < ema_s) and (last_close < ema_f) and (last_close < (lo_dch + proximity)): return "SHORT"
    return None

# ---------- Range-reversion scalp ----------
def range_reversion_signal(
    candles,
    adx_period=10, adx_max=20,
    bb_n=20, bb_k=2.0,
    rsi_n=14, rsi_low=40, rsi_high=60
):
    if not candles or len(candles) < max(bb_n, rsi_n) + 2:
        return None
    closes = [c['close'] for c in candles]
    last_close = closes[-1]
    # ADX gate
    adx_val = (_compute_adx(candles, period=adx_period) or 0.0)
    if adx_val > adx_max:
        return None
    # Bollinger
    upper, mid, lower = bollinger_bands(closes, n=bb_n, k=bb_k)
    if upper is None or lower is None:
        return None
    # RSI (simple Wilder-style)
    gains, losses = [], []
    for i in range(-rsi_n, -1):
        chg = closes[i+1] - closes[i]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_gain = sum(gains) / rsi_n
    avg_loss = sum(losses) / rsi_n
    rs = (avg_gain / avg_loss) if avg_loss != 0 else 999.0
    rsi = 100 - (100 / (1 + rs))
    if (last_close < lower) and (rsi < rsi_low):  return "LONG"
    if (last_close > upper) and (rsi > rsi_high): return "SHORT"
    return None
