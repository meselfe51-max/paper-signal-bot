"""Causal market-regime labels. Every value uses candles available at that index only."""
from statistics import median


def _ema(values, period):
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for item in values[period:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _atr_pct(candles, period=14):
    if len(candles) < period + 1 or candles[-1]["c"] <= 0:
        return None
    ranges = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        ranges.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]),
                          abs(cur["l"] - prev["c"])))
    return (sum(ranges[-period:]) / period) / candles[-1]["c"]


def classify_regime(candles):
    """Return trend, volatility and combined regime from a closed-candle window."""
    if len(candles) < 60:
        return {"trend": "unknown", "volatility": "unknown", "regime": "unknown"}
    closes = [x["c"] for x in candles]
    e20, e50 = _ema(closes[-100:], 20), _ema(closes[-100:], 50)
    prior_e50 = _ema(closes[-105:-5], 50) if len(closes) >= 105 else None
    if e20 is None or e50 is None:
        trend = "unknown"
    elif closes[-1] > e50 and e20 > e50 and (prior_e50 is None or e50 > prior_e50):
        trend = "bullish"
    elif closes[-1] < e50 and e20 < e50 and (prior_e50 is None or e50 < prior_e50):
        trend = "bearish"
    else:
        trend = "sideways"

    current = _atr_pct(candles)
    history = []
    for end in range(max(15, len(candles) - 61), len(candles) - 1):
        value = _atr_pct(candles[:end + 1])
        if value is not None:
            history.append(value)
    if current is None or len(history) < 10:
        volatility = "unknown"
    else:
        baseline = median(history)
        volatility = "normal" if baseline <= 0 else (
            "high" if current > baseline * 1.35 else
            "low" if current < baseline * 0.70 else "normal")
    return {"trend": trend, "volatility": volatility,
            "regime": f"{trend}_{volatility}"}
