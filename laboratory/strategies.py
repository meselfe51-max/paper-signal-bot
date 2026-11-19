"""Independent, deterministic strategy signal generators; no shared strategy state."""
from math import sqrt
from .regimes import classify_regime


def ema(values, period):
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for x in values[period:]:
        value = alpha * x + (1.0 - alpha) * value
    return value


def rsi(closes, period=14):
    if len(closes) <= period:
        return None
    changes = [b - a for a, b in zip(closes[-period-1:-1], closes[-period:])]
    gains = sum(max(x, 0.0) for x in changes) / period
    losses = sum(max(-x, 0.0) for x in changes) / period
    return 100.0 if losses == 0 and gains else 50.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    tr = [max(candles[i]["h"] - candles[i]["l"],
              abs(candles[i]["h"] - candles[i-1]["c"]),
              abs(candles[i]["l"] - candles[i-1]["c"])) for i in range(1, len(candles))]
    return sum(tr[-period:]) / period


def _base(c):
    closes = [x["c"] for x in c]
    return closes, atr(c), rsi(closes)


def trend_following(c, regime):
    closes, a, strength = _base(c)
    if a is None or len(closes) < 55: return False
    fast, slow = ema(closes[-80:], 20), ema(closes[-80:], 50)
    prev_fast = ema(closes[-81:-1], 20)
    return bool(fast and slow and prev_fast and closes[-1] > fast > slow
                and closes[-2] <= prev_fast and 45 <= strength <= 68)


def mean_reversion(c, regime):
    closes, a, strength = _base(c)
    if a is None or len(closes) < 30 or regime["trend"] != "sideways": return False
    window = closes[-20:]
    mean = sum(window) / len(window)
    variance = sum((x - mean) ** 2 for x in window) / len(window)
    z = (closes[-1] - mean) / sqrt(variance) if variance > 0 else 0
    return z <= -1.5 and strength is not None and strength < 38


def momentum(c, regime):
    closes, a, strength = _base(c)
    if a is None or len(closes) < 32: return False
    roc10 = closes[-1] / closes[-11] - 1 if closes[-11] else 0
    roc30 = closes[-1] / closes[-31] - 1 if closes[-31] else 0
    return roc10 > 0.02 and roc10 > roc30 and closes[-1] > ema(closes[-60:], 20)


def breakout(c, regime):
    if len(c) < 30: return False
    prior = c[-21:-1]
    avg_vol = sum(x.get("v", 0) for x in prior) / len(prior)
    volume_ok = avg_vol <= 0 or c[-1].get("v", 0) >= avg_vol * 1.1
    return c[-1]["c"] > max(x["h"] for x in prior) and volume_ok


def volatility_expansion(c, regime):
    if len(c) < 50: return False
    current_atr = atr(c)
    prior_atrs = [atr(c[:end]) for end in range(len(c) - 31, len(c) - 1)]
    prior_atrs = [x for x in prior_atrs if x is not None]
    if not prior_atrs or current_atr is None: return False
    expanding = current_atr > (sum(prior_atrs) / len(prior_atrs)) * 1.2
    breakout10 = c[-1]["c"] > max(x["h"] for x in c[-11:-1])
    return expanding and breakout10 and c[-1]["c"] > ema([x["c"] for x in c[-60:]], 20)


def range_rotation(c, regime):
    closes, a, strength = _base(c)
    if a is None or len(c) < 30 or regime["trend"] != "sideways": return False
    lows = min(x["l"] for x in c[-20:-1])
    return c[-1]["l"] <= lows * 1.005 and c[-1]["c"] > c[-1]["o"] and strength < 48


def existing_adaptive(c, regime):
    """Run the repository's current adaptive strategy as a benchmark, unchanged."""
    if len(c) < 210: return False
    from adaptive_strategy import analyze
    daily = []
    for start in range(0, len(c) - 5, 6):
        group = c[start:start + 6]
        if len(group) == 6:
            daily.append({"t": group[0]["t"], "o": group[0]["o"],
                          "h": max(x["h"] for x in group), "l": min(x["l"] for x in group),
                          "c": group[-1]["c"], "v": sum(x.get("v", 0) for x in group)})
    result = analyze(c, daily, True, "BTC/USD")
    return bool(result.get("ok"))


STRATEGIES = {
    "trend_following": trend_following,
    "mean_reversion": mean_reversion,
    "momentum": momentum,
    "breakout": breakout,
    "volatility_expansion": volatility_expansion,
    "range_rotation": range_rotation,
    "existing_adaptive_baseline": existing_adaptive,
}
