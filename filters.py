"""filters.py

Pure analytical helpers for the XAUUSDm scalper. All functions operate on
plain candle dictionaries and return deterministic outputs which makes them
unit-test friendly.

The module exposes a minimal TA toolset:
- ATR/ADX/EMA/Donchian calculations
- Regime classification between TREND/RANGING/UNSURE
- Breakout and range reversion micro-signals used by main.py

All price inputs are expressed in USD. For XAUUSDm, one "pip" equals
0.01 USD, hence helpers accept a ``pip_value`` argument to keep the logic
reusable.
"""
from __future__ import annotations

from statistics import mean
from typing import List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

PIP_VALUE_XAU = 0.01  # 1 pip = $0.01 for gold


def price_to_pips(diff_price: float, pip_value: float = PIP_VALUE_XAU) -> float:
    """Convert a price difference (USD) to pips."""
    if pip_value <= 0:
        raise ValueError("pip_value must be positive")
    return diff_price / pip_value


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def compute_true_range(prev_close: float, high: float, low: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def compute_atr(candles: Sequence[dict], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs: List[float] = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1]["close"]
        high = candles[i]["high"]
        low = candles[i]["low"]
        trs.append(compute_true_range(prev_close, high, low))
    if len(trs) < period:
        return None
    return mean(trs[-period:])


def atr_dollars(
    candles: Sequence[dict], period: int = 10, contract: float = 1.0
) -> Optional[float]:
    """Return ATR in dollar terms for a given contract size.

    ``contract`` corresponds to the instrument's contract size for 1 lot.
    The returned value represents the dollar swing for a **one-lot** position.
    """
    atr_val = compute_atr(candles, period=period)
    if atr_val is None:
        return None
    return atr_val * contract


# ---------------------------------------------------------------------------
# EMA / Donchian / ADX utilities
# ---------------------------------------------------------------------------

def _ema_series(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    ema_values: List[float] = []
    ema = values[0]
    for value in values:
        ema = value * k + ema * (1.0 - k)
        ema_values.append(ema)
    return ema_values


def donchian_breakout(
    candles: Sequence[dict], lkb: int = 14
) -> Tuple[Optional[str], Optional[float], Optional[float], Optional[float]]:
    """Return breakout direction and the channel boundaries.

    The Donchian channel is computed using the previous ``lkb`` candles. The
    latest candle close is compared against this channel to flag LONG/SHORT
    breakouts. If no breakout is detected ``None`` is returned.
    """
    if len(candles) < lkb + 1:
        return None, None, None, None

    # Use the previous ``lkb`` completed candles to build the channel
    channel = candles[-(lkb + 1) : -1]
    highs = [c["high"] for c in channel]
    lows = [c["low"] for c in channel]
    hi = max(highs)
    lo = min(lows)
    last_close = candles[-1]["close"]

    direction: Optional[str] = None
    if last_close > hi:
        direction = "LONG"
    elif last_close < lo:
        direction = "SHORT"

    return direction, hi, lo, last_close


def _rma(values: Sequence[float], period: int) -> Optional[List[float]]:
    if len(values) < period:
        return None
    out: List[float] = []
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

    tr_list: List[float] = []
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    for i in range(1, len(candles)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    tr_smoothed = _rma(tr_list, period)
    plus_smoothed = _rma(plus_dm, period)
    minus_smoothed = _rma(minus_dm, period)
    if not tr_smoothed or not plus_smoothed or not minus_smoothed:
        return None

    length = min(len(tr_smoothed), len(plus_smoothed), len(minus_smoothed))
    plus_di = [
        0 if tr_smoothed[i] == 0 else 100.0 * plus_smoothed[i] / tr_smoothed[i]
        for i in range(length)
    ]
    minus_di = [
        0 if tr_smoothed[i] == 0 else 100.0 * minus_smoothed[i] / tr_smoothed[i]
        for i in range(length)
    ]
    dx = []
    for p, m in zip(plus_di, minus_di):
        denom = p + m
        dx.append(0 if denom == 0 else 100.0 * abs(p - m) / denom)
    adx_series = _rma(dx, period)
    return adx_series[-1] if adx_series else None


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def _sma(values: Sequence[float], length: int) -> Optional[float]:
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


def _stddev(values: Sequence[float], length: int) -> Optional[float]:
    if len(values) < length:
        return None
    mean_val = _sma(values, length)
    if mean_val is None:
        return None
    variance = sum((v - mean_val) ** 2 for v in values[-length:]) / length
    return variance ** 0.5


def bollinger_bands(values: Sequence[float], length: int = 20, k: float = 2.0) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(values) < length:
        return None, None, None
    mid = _sma(values, length)
    std = _stddev(values, length)
    if mid is None or std is None:
        return None, None, None
    return mid + k * std, mid, mid - k * std


# ---------------------------------------------------------------------------
# Range detection
# ---------------------------------------------------------------------------

def detect_range(
    candles: Sequence[dict], lookback_bars: int = 20, max_width_pips: float = 250.0, pip_value: float = PIP_VALUE_XAU
) -> Tuple[bool, Optional[float], Optional[float]]:
    if len(candles) < lookback_bars:
        return False, None, None
    window = candles[-lookback_bars:]
    high = max(c["high"] for c in window)
    low = min(c["low"] for c in window)
    width_pips = price_to_pips(high - low, pip_value)
    return width_pips <= max_width_pips, high, low


# ---------------------------------------------------------------------------
# Regime engine
# ---------------------------------------------------------------------------

def market_state(
    candles: Sequence[dict],
    atr_value: Optional[float],
    pip_value: float = PIP_VALUE_XAU,
    adx_period: int = 10,
    ema_fast: int = 13,
    ema_slow: int = 34,
    donchian_lkb: int = 14,
    range_lookback: int = 20,
    range_max_width_pips: float = 250.0,
) -> str:
    """Classify the market regime.

    Returns one of ``TREND_LONG``, ``TREND_SHORT``, ``RANGING`` or ``UNSURE``.
    """
    min_history = max(ema_slow, donchian_lkb + 1, range_lookback)
    if len(candles) < min_history:
        return "UNSURE"

    # Range first – avoid trading trends inside narrow channels
    in_range, _, _ = detect_range(
        candles, lookback_bars=range_lookback, max_width_pips=range_max_width_pips, pip_value=pip_value
    )
    if in_range:
        return "RANGING"

    closes = [c["close"] for c in candles]
    ema_fast_series = _ema_series(closes, ema_fast)
    ema_slow_series = _ema_series(closes, ema_slow)
    if not ema_fast_series or not ema_slow_series:
        return "UNSURE"
    ema_fast_val = ema_fast_series[-1]
    ema_slow_val = ema_slow_series[-1]

    adx_val = compute_adx(candles, period=adx_period)
    adx_ok = adx_val is not None and adx_val >= 25  # strong trend threshold

    breakout_dir, _, _, _ = donchian_breakout(candles, lkb=donchian_lkb)

    if breakout_dir == "LONG" and ema_fast_val > ema_slow_val and adx_ok:
        return "TREND_LONG"
    if breakout_dir == "SHORT" and ema_fast_val < ema_slow_val and adx_ok:
        return "TREND_SHORT"

    return "UNSURE"


# ---------------------------------------------------------------------------
# Micro signals
# ---------------------------------------------------------------------------

def micro_breakout_signal(
    candles: Sequence[dict],
    adx_period: int = 10,
    adx_min: float = 14.0,
    donchian_lkb: int = 14,
    proximity_factor: float = 0.15,
) -> Optional[str]:
    """Return a lightweight breakout signal near the Donchian boundary.

    Triggered only when ADX is mildly supportive (>= ``adx_min``). The latest
    close must be within ``proximity_factor`` ATR of the respective Donchian
    boundary.
    """
    if len(candles) < donchian_lkb + 2:
        return None

    adx_val = compute_adx(candles, period=adx_period)
    if adx_val is None or adx_val < adx_min:
        return None

    atr_val = compute_atr(candles, period=10)
    if atr_val is None:
        return None

    direction, hi, lo, last_close = donchian_breakout(candles, lkb=donchian_lkb)
    # If we already have an outright breakout we hand control to the primary
    # regime logic; here we only care about "near touch" situations.
    if direction is not None:
        return None

    threshold = proximity_factor * atr_val
    if hi is not None and last_close is not None and (hi - last_close) <= threshold:
        return "LONG"
    if lo is not None and last_close is not None and (last_close - lo) <= threshold:
        return "SHORT"
    return None


def range_reversion_signal(
    candles: Sequence[dict],
    adx_period: int = 10,
    adx_max: float = 20.0,
    bb_length: int = 20,
    bb_std: float = 2.0,
) -> Optional[str]:
    """Fade extremes when volatility is contracting.

    Only valid when ADX is subdued (<= ``adx_max``). We fade Bollinger band
    breaches back to the middle line and trade at most once per side until flat.
    """
    if len(candles) < max(bb_length, adx_period + 2):
        return None

    adx_val = compute_adx(candles, period=adx_period)
    if adx_val is None or adx_val > adx_max:
        return None

    closes = [c["close"] for c in candles]
    upper, mid, lower = bollinger_bands(closes, length=bb_length, k=bb_std)
    if upper is None or mid is None or lower is None:
        return None

    last_close = closes[-1]
    if last_close > upper:
        return "SHORT"
    if last_close < lower:
        return "LONG"
    return None


# ---------------------------------------------------------------------------
# Diagnostics for logging
# ---------------------------------------------------------------------------

def market_diag(
    candles: Sequence[dict],
    adx_period: int = 10,
    ema_fast: int = 13,
    ema_slow: int = 34,
    donchian_lkb: int = 14,
) -> dict:
    diag = {
        "adx": None,
        "ema_fast": None,
        "ema_slow": None,
        "donchian_hi": None,
        "donchian_lo": None,
        "last_close": None,
    }
    if not candles:
        return diag
    closes = [c["close"] for c in candles]
    diag["last_close"] = closes[-1]
    diag["adx"] = compute_adx(candles, period=adx_period)
    ef = _ema_series(closes, ema_fast)
    es = _ema_series(closes, ema_slow)
    diag["ema_fast"] = ef[-1] if ef else None
    diag["ema_slow"] = es[-1] if es else None
    _, hi, lo, _ = donchian_breakout(candles, lkb=donchian_lkb)
    diag["donchian_hi"] = hi
    diag["donchian_lo"] = lo
    return diag
