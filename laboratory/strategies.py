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
    # Only the trailing period is needed; avoid rescanning years of history per candle.
    start = len(candles) - period
    tr = [max(candles[i]["h"] - candles[i]["l"],
              abs(candles[i]["h"] - candles[i-1]["c"]),
              abs(candles[i]["l"] - candles[i-1]["c"]))
          for i in range(start, len(candles))]
    return sum(tr) / period


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
    recent = c[-45:]
    prior_atrs = [atr(recent[:end]) for end in range(len(recent) - 31, len(recent) - 1)]
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


def daily_from_4h(candles):
    """Aggregate completed UTC days from 4h candles."""
    grouped = {}
    for candle in candles[-1200:]:
        day = candle["t"] // 86400 * 86400
        grouped.setdefault(day, []).append(candle)
    daily = []
    for day, group in sorted(grouped.items()):
        if len(group) >= 6:
            daily.append({"t": day, "o": group[0]["o"],
                          "h": max(x["h"] for x in group), "l": min(x["l"] for x in group),
                          "c": group[-1]["c"], "v": sum(x.get("v", 0) for x in group)})
    return daily


def daily_trend_up(daily):
    """Match bot.daily_trend_up: 80 daily bars, close above rising EMA50."""
    if len(daily) < 80:
        return None
    closes = [x["c"] for x in daily[-200:]]
    if len(closes) < 55:
        return None
    alpha = 2.0 / 51.0
    value = sum(closes[:50]) / 50
    ema_values = [value]
    for close in closes[50:]:
        value = alpha * close + (1.0 - alpha) * value
        ema_values.append(value)
    return bool(closes[-1] > ema_values[-1] and ema_values[-1] > ema_values[-6])


def existing_adaptive(c, regime, btc_up=None, pair_name="BTC/USD"):
    """Run the current adaptive strategy with the matching historical BTC context."""
    if len(c) < 210:
        return False
    from adaptive_strategy import analyze
    daily = daily_from_4h(c)
    result = analyze(c, daily, btc_up, pair_name)
    return bool(result.get("ok"))
