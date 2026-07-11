"""Technical indicators from OHLCV closes/candles.

Conventions (documented for LLM/UI consumers):
- EMA: seed with SMA at index period-1, then standard k=2/(period+1).
- RSI: Wilder smoothing (RMA) with period default 14.
- MACD: EMA(12) − EMA(26); signal = EMA(9) of MACD line; hist = macd − signal.
- VWAP: cumulative typical-price * vol / cumulative vol, anchored per UTC day.
"""

from __future__ import annotations

from app.models import Candle


def compute_ema(closes: list[float], period: int) -> list[float | None]:
    """Exponential moving average. First period-1 values are None; seed is SMA."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    sma = sum(closes[:period]) / period
    out[period - 1] = sma
    prev = sma
    for i in range(period, n):
        prev = closes[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def compute_rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder RSI. First `period` closes yield None (needs `period` deltas)."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period + 1:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period

    def _rsi(ag: float, al: float) -> float:
        if al == 0.0:
            return 100.0 if ag > 0 else 50.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi(avg_gain, avg_loss)

    for i in range(period + 1, n):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def compute_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> dict[str, list[float | None]]:
    """MACD 12/26/9 (defaults). Returns dict with macd, signal, hist lists."""
    n = len(closes)
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    macd_line: list[float | None] = [None] * n
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]  # type: ignore[operator]

    # Signal = EMA of MACD values (skip leading Nones for seed)
    signal: list[float | None] = [None] * n
    hist: list[float | None] = [None] * n
    valid_macd = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    if len(valid_macd) < signal_period:
        return {"macd": macd_line, "signal": signal, "hist": hist}

    k = 2.0 / (signal_period + 1)
    seed_vals = [v for _, v in valid_macd[:signal_period]]
    seed_idx = valid_macd[signal_period - 1][0]
    prev = sum(seed_vals) / signal_period
    signal[seed_idx] = prev
    hist[seed_idx] = macd_line[seed_idx] - prev  # type: ignore[operator]

    for j in range(signal_period, len(valid_macd)):
        i, v = valid_macd[j]
        prev = v * k + prev * (1.0 - k)
        signal[i] = prev
        hist[i] = v - prev

    return {"macd": macd_line, "signal": signal, "hist": hist}


def compute_atr(candles: list[Candle], period: int = 14) -> list[float | None]:
    """Wilder ATR from true range. First `period` values are None."""
    n = len(candles)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period + 1:
        return out
    trs: list[float] = []
    for i in range(1, n):
        h, l = candles[i].high, candles[i].low
        pc = candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    out[period] = atr
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i - 1]) / period
        out[i] = atr
    return out


def compute_vwap(candles: list[Candle]) -> list[float | None]:
    """Daily-anchored VWAP (typical = (H+L+C)/3).

    Cumulation resets at each UTC day boundary so the value does not depend
    on the arbitrary size of the loaded candle window.
    """
    out: list[float | None] = []
    cum_tp_vol = 0.0
    cum_vol = 0.0
    day: int | None = None
    for c in candles:
        c_day = int(c.time) // 86_400_000  # candle time is ms
        if day is None or c_day != day:
            day = c_day
            cum_tp_vol = 0.0
            cum_vol = 0.0
        typical = (c.high + c.low + c.close) / 3.0
        vol = c.vol
        if vol < 0:
            vol = 0.0
        cum_tp_vol += typical * vol
        cum_vol += vol
        if cum_vol == 0.0:
            out.append(None)
        else:
            out.append(cum_tp_vol / cum_vol)
    return out


def indicator_bundle(candles: list[Candle]) -> dict:
    """Compact indicator snapshot for API/LLM (last values + full series where useful)."""
    closes = [c.close for c in candles]
    macd = compute_macd(closes)
    ema20 = compute_ema(closes, 20)
    ema50 = compute_ema(closes, 50)
    ema200 = compute_ema(closes, 200)
    rsi = compute_rsi(closes, 14)
    vwap = compute_vwap(candles)
    atr = compute_atr(candles, 14)

    def _last(series: list[float | None]) -> float | None:
        for v in reversed(series):
            if v is not None:
                return v
        return None

    return {
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi14": rsi,
        "macd": macd["macd"],
        "macd_signal": macd["signal"],
        "macd_hist": macd["hist"],
        "vwap": vwap,
        "atr14": atr,
        "last": {
            "ema20": _last(ema20),
            "ema50": _last(ema50),
            "ema200": _last(ema200),
            "rsi14": _last(rsi),
            "macd": _last(macd["macd"]),
            "macd_signal": _last(macd["signal"]),
            "macd_hist": _last(macd["hist"]),
            "vwap": _last(vwap),
            "atr14": _last(atr),
        },
    }
