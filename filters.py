"""filters.py

Indicator calculations and regime classification for the strategy.
"""
from __future__ import annotations

from statistics import mean
from typing import Dict, Optional, Sequence, Tuple


def compute_true_range(prev_close: float, high: float, low: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def compute_atr(candles: Sequence[dict], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1]["close"]
        high = candles[i]["high"]
        low = candles[i]["low"]
        trs.append(compute_true_range(prev_close, high, low))
    if len(trs) < period:
        return None
    return mean(trs[-period:])


def _ema_series(values: Sequence[float], period: int):
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    ema_values = []
    ema = values[0]
    for value in values:
        ema = value * k + ema * (1.0 - k)
        ema_values.append(ema)
    return ema_values


def ema_latest(candles: Sequence[dict], period: int) -> Optional[float]:
    closes = [c["close"] for c in candles]
    if len(closes) < period:
        return None
    return _ema_series(closes, period)[-1]


def _rma(values: Sequence[float], period: int) -> Optional[Sequence[float]]:
    if len(values) < period:
        return None
    out = []
    r = sum(values[:period]) / period
    out.append(r)
    for value in values[period:]:
        r = (r * (period - 1) + value) / period
        out.append(r)
    return out


def compute_adx(candles: Sequence[dict], period: int = 14) -> Optional[float]:
    if len(candles) < period + 2:
        return None
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]

    tr_list = []
    plus_dm = []
    minus_dm = []
    for i in range(1, len(candles)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_list.append(tr)
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    tr_smoothed = _rma(tr_list, period)
    plus_smoothed = _rma(plus_dm, period)
    minus_smoothed = _rma(minus_dm, period)
    if not tr_smoothed or not plus_smoothed or not minus_smoothed:
        return None

    length = min(len(tr_smoothed), len(plus_smoothed), len(minus_smoothed))
    plus_di = [0.0 if tr_smoothed[i] == 0 else 100.0 * plus_smoothed[i] / tr_smoothed[i] for i in range(length)]
    minus_di = [0.0 if tr_smoothed[i] == 0 else 100.0 * minus_smoothed[i] / tr_smoothed[i] for i in range(length)]

    dx = []
    for p, m in zip(plus_di, minus_di):
        denom = p + m
        dx.append(0.0 if denom == 0 else 100.0 * abs(p - m) / denom)
    adx_series = _rma(dx, period)
    return adx_series[-1] if adx_series else None


def donchian_channel(candles: Sequence[dict], lkb: int) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(candles) < lkb + 1:
        return None, None, None
    window = candles[-(lkb + 1) : -1]
    highs = [c["high"] for c in window]
    lows = [c["low"] for c in window]
    hi = max(highs)
    lo = min(lows)
    last_close = candles[-1]["close"]
    return hi, lo, last_close


def market_state(
    candles: Sequence[dict],
    adx_value: Optional[float],
    atr_value: Optional[float],
    cfg: Dict[str, float],
) -> Dict[str, Optional[float]]:
    ema_fast_val = ema_latest(candles, int(cfg["EMA_FAST"]))
    ema_slow_val = ema_latest(candles, int(cfg["EMA_SLOW"]))
    donchian_hi, donchian_lo, last_close = donchian_channel(candles, int(cfg["DONCHIAN_LKB"]))

    result: Dict[str, Optional[float]] = {
        "regime": "UNSURE",
        "micro_bias": None,
        "ema_fast": ema_fast_val,
        "ema_slow": ema_slow_val,
        "donchian_high": donchian_hi,
        "donchian_low": donchian_lo,
        "last_close": last_close,
        "atr_quiet": None,
    }

    if atr_value is None:
        result["atr_quiet"] = True
    else:
        result["atr_quiet"] = atr_value < cfg["ATR_MIN"]

    if (
        adx_value is None
        or ema_fast_val is None
        or ema_slow_val is None
        or donchian_hi is None
        or donchian_lo is None
        or last_close is None
    ):
        return result

    if result["atr_quiet"]:
        return result

    if adx_value >= cfg["ADX_TREND_MIN"]:
        if ema_fast_val > ema_slow_val and last_close > donchian_hi:
            result["regime"] = "TREND_LONG"
        elif ema_fast_val < ema_slow_val and last_close < donchian_lo:
            result["regime"] = "TREND_SHORT"
    else:
        if cfg["ADX_MICRO_MIN"] <= adx_value < cfg["ADX_TREND_MIN"] and atr_value is not None:
            band = 0.5 * atr_value
            if ema_fast_val > ema_slow_val and abs(last_close - donchian_hi) <= band:
                result["micro_bias"] = "LONG"
            elif ema_fast_val < ema_slow_val and abs(last_close - donchian_lo) <= band:
                result["micro_bias"] = "SHORT"

    return result
