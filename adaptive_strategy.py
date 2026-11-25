"""Regime-aware, score-based paper-trading strategy.

Long-only spot simulation. Uses closed daily and 4h candles only.
No external dependencies; deliberately keeps execution/risk controls in bot.py.
"""
from math import isfinite

WINDOW = 500
MIN_4H = 210
MIN_DAILY = 60
ATR_PERIOD = 14
ENTRY_SCORE = 6.0
DONCHIAN_FAST = 20
DONCHIAN_SLOW = 55
ATR_PCT_HARD_MAX = 0.08
MAX_STRETCH_ATR = 3.0


def _ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    out = [None] * (period - 1) + [value]
    for x in values[period:]:
        value = alpha * x + (1.0 - alpha) * value
        out.append(value)
    return out


def _atr(candles, period=ATR_PERIOD):
    if len(candles) < period + 1:
        return None
    tr = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        tr.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]),
                      abs(cur["l"] - prev["c"])))
    value = sum(tr[:period]) / period
    for x in tr[period:]:
        value = (value * (period - 1) + x) / period
    return value


def _rsi(closes, period=14):
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for a, b in zip(closes[-period-1:-1], closes[-period:]):
        change = b - a
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _volume_ratio(candles, period=20):
    if len(candles) < period + 1:
        return None
    prior = candles[-period-1:-1]
    avg = sum(x.get("v", 0.0) for x in prior) / period
    return candles[-1].get("v", 0.0) / avg if avg > 0 else None


def _daily_regime(d):
    if len(d) < MIN_DAILY:
        return "unknown", None
    closes = [x["c"] for x in d[-200:]]
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    if not e20 or not e50:
        return "unknown", None
    close, fast, slow = closes[-1], e20[-1], e50[-1]
    slope = slow - e50[-6] if len(e50) >= 6 else 0.0
    if close > slow and fast > slow and slope > 0:
        return "bullish", {"close": close, "ema20": fast, "ema50": slow, "slope": slope}
    if close < slow and fast < slow and slope < 0:
        return "bearish", {"close": close, "ema20": fast, "ema50": slow, "slope": slope}
    return "neutral", {"close": close, "ema20": fast, "ema50": slow, "slope": slope}


def analyze(c, d, btc_up, name=""):
    c = c[-WINDOW:]
    d = d[-200:]
    price = c[-1]["c"] if c else 0.0
    result = {"ok": False, "setup": "", "filters": [], "atr": 0.0,
              "price": price, "level": 0.0, "rank": 0.0, "score": 0.0,
              "regime": "unknown", "reasons": []}
    if len(c) < MIN_4H or len(d) < MIN_DAILY or price <= 0:
        result["reasons"].append("داده کافی نیست")
        return result

    closes = [x["c"] for x in c]
    atr = _atr(c)
    e20, e50, e200 = _ema(closes, 20), _ema(closes, 50), _ema(closes, 200)
    rsi = _rsi(closes)
    vol_ratio = _volume_ratio(c)
    if atr is None or not e20 or not e50 or not e200 or rsi is None:
        result["reasons"].append("محاسبه شاخص‌ها ممکن نشد")
        return result

    atr_pct = atr / price
    fast_high = max(x["h"] for x in c[-DONCHIAN_FAST-1:-1])
    slow_high = max(x["h"] for x in c[-DONCHIAN_SLOW-1:-1])
    result.update({"atr": atr, "level": fast_high})
    daily_state, daily = _daily_regime(d)
    result["regime"] = daily_state

    score, factors = 0.0, []
    def add(label, points, passed, detail=""):
        nonlocal score
        if passed:
            score += points
        factors.append((label, bool(passed)))
        if detail:
            result["reasons"].append(detail)

    # Higher-timeframe regime is weighted context. Bearish daily trend lowers the score,
    # but a strong, independently confirmed 4h setup may still qualify.
    add("رژیم روزانه صعودی", 2.0, daily_state == "bullish",
        "روند روزانه نزولی است" if daily_state == "bearish" else "")
    if daily_state == "neutral":
        add("ساختار روزانه خنثی", 0.5, price > daily["ema50"])

    # BTC is a market-wide risk context, not an absolute veto for every altcoin.
    if name not in ("PAXG/USD", "BTC/USD"):
        add("همراهی با روند BTC", 1.0, btc_up is True,
            "روند روزانه BTC ضعیف است" if btc_up is False else
            "روند روزانه BTC نامشخص است" if btc_up is None else "")

    add("قیمت بالای EMA200 چهار‌ساعته", 1.25, price > e200[-1])
    add("EMA20 بالای EMA50", 1.0, e20[-1] > e50[-1])
    add("شیب EMA50 مثبت", 0.5, e50[-1] > e50[-6])
    add("قدرت حرکت مناسب", 1.0, 52 <= rsi <= 70,
        "RSI بیش از حد داغ است" if rsi > 76 else "")
    add("حجم تأییدکننده", 0.75, vol_ratio is not None and vol_ratio >= 1.2)
    add("حجم حداقلی", 0.25, vol_ratio is not None and vol_ratio >= 0.85)

    breakout_fast = price > fast_high
    breakout_slow = price > slow_high
    near_breakout = (fast_high - price) <= 0.35 * atr and price > e20[-1]
    reclaim = (c[-2]["c"] <= e20[-2] and price > e20[-1]
               and price > e50[-1] and rsi >= 50)
    if breakout_slow:
        add("شکست معتبر سقف ۵۵ کندل", 2.0, True)
        setup = "شکست سقف میان‌مدت"
    elif breakout_fast:
        add("شکست سقف ۲۰ کندل", 1.6, True)
        setup = "شکست سقف کوتاه‌مدت"
    elif near_breakout:
        add("نزدیکی به شکست", 1.0, True)
        setup = "آمادگی شکست"
    elif reclaim:
        add("بازپس‌گیری EMA20", 1.0, True)
        setup = "بازگشت به روند"
    else:
        add("ساختار ورود", 0.0, False)
        setup = ""

    # Volatility and transaction costs: penalize extremes, do not reject every quiet market.
    try:
        import bot as _bot
        min_atr_pct = _bot.ATR_PCT_MIN
    except Exception:
        min_atr_pct = 0.004
    add("نوسان اقتصادی", 0.75, min_atr_pct <= atr_pct <= 0.055,
        "نوسان بیش از حد بالاست" if atr_pct > ATR_PCT_HARD_MAX else "")
    stretch = (price - e20[-1]) / atr
    add("فاصله مناسب از EMA20", 0.75, -0.5 <= stretch <= 2.0,
        "قیمت بیش از حد از میانگین فاصله گرفته" if stretch > MAX_STRETCH_ATR else "")

    # Relative strength is used for ranking, not as a hard entry requirement.
    if len(d) > 20 and d[-21]["c"] > 0:
        result["rank"] = d[-1]["c"] / d[-21]["c"] - 1.0
    result["score"] = round(score, 2)
    result["filters"] = factors
    result["setup"] = setup
    result["ok"] = bool(setup and score >= ENTRY_SCORE
                        and atr_pct <= ATR_PCT_HARD_MAX
                        and stretch <= MAX_STRETCH_ATR
                        and (btc_up is not None or name in ("PAXG/USD", "BTC/USD")))
    if not setup:
        result["reasons"].append("الگوی ورود معتبر شکل نگرفته")
    if score < ENTRY_SCORE:
        result["reasons"].append("امتیاز کیفیت کمتر از حد لازم")
    return result
