    out = [None] * len(v) 
    idx = [i for i, x in enumerate(v) if x is not None] 
    if len(idx) < n: 
        return out 
    s = idx[0] 
    out[s + n - 1] = sum(v[s:s + n]) / n 
    k = 2 / (n + 1) 
    for i in range(s + n, len(v)): 
        out[i] = v[i] * k + out[i - 1] * (1 - k) 
    return out 
 
 
def sma_last(v, n): 
    return sum(v[-n:]) / n 
 
 
def rsi_last(closes, n=14): 
    gains, losses = [], [] 
    for i in range(1, len(closes)): 
        d = closes[i] - closes[i - 1] 
        gains.append(max(d, 0)) 
        losses.append(max(-d, 0)) 
    ag = sum(gains[:n]) / n 
    al = sum(losses[:n]) / n 
    for i in range(n, len(gains)): 
        ag = (ag * (n - 1) + gains[i]) / n 
        al = (al * (n - 1) + losses[i]) / n 
    if al == 0: 
        return 100.0 
    return 100 - 100 / (1 + ag / al) 
 
 
def atr_last(c, n=14): 
    trs = [] 
    for i in range(1, len(c)): 
        trs.append(max(c[i]["h"] - c[i]["l"], 
                       abs(c[i]["h"] - c[i - 1]["c"]), 
                       abs(c[i]["l"] - c[i - 1]["c"]))) 
    a = sum(trs[:n]) / n 
    for t in trs[n:]: 
        a = (a * (n - 1) + t) / n 
    return a 
 
 
def stoch(c, n=14): 
    ks = [] 
    for i in range(n - 1, len(c)): 
        w = c[i - n + 1:i + 1] 
        hh = max(x["h"] for x in w) 
        ll = min(x["l"] for x in w) 
        ks.append(0 if hh == ll else (c[i]["c"] - ll) / (hh - ll) * 100) 
    k_s = [sum(ks[i - 2:i + 1]) / 3 for i in range(2, len(ks))]   # smoothed %K 
    d_s = [sum(k_s[i - 2:i + 1]) / 3 for i in range(2, len(k_s))]  # %D 
    return k_s[-1], d_s[-1] 
 
 
def votes(c): 
    """Return list of (category, name, vote) where vote is +1 buy, -1 sell, 0 neutral.""" 
    closes = [x["c"] for x in c] 
    last = c[-1] 
    out = [] 
 
    # --- trend 
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200) 
    out.append(("روند", "EMA 20/50", 1 if e20[-1] > e50[-1] else -1)) 
 
    e12, e26 = ema(closes, 12), ema(closes, 26) 
    macd = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)] 
    sig = ema(macd, 9) 
    h_now = macd[-1] - sig[-1] 
    h_prev = macd[-2] - sig[-2] 
    out.append(("روند", "MACD", 1 if h_now > 0 and h_now > h_prev else -1 if h_now < 0 and h_now < h_prev else 0)) 
 
    out.append(("روند", "قیمت نسبت به EMA 200", 1 if last["c"] > e200[-1] else -1)) 
 
    # --- momentum 
    r = rsi_last(closes) 
    out.append(("مومنتوم", "RSI", 1 if r < 40 else -1 if r > 60 else 0)) 
    k, d = stoch(c) 
    out.append(("مومنتوم", "Stochastic", 1 if k < 25 and k > d else -1 if k > 75 and k < d else 0)) 
 
    # --- volatility 
    w = closes[-20:] 
    mid = sum(w) / 20 
    sd = (sum((x - mid) ** 2 for x in w) / 20) ** 0.5 
    lo, hi = mid - 2 * sd, mid + 2 * sd 
    pb = 0.5 if hi == lo else (last["c"] - lo) / (hi - lo) 
    out.append(("نوسان", "Bollinger", 1 if pb < 0.15 else -1 if pb > 0.85 else 0)) 
 
    atr = atr_last(c) 
    body = last["c"] - last["o"] 
    out.append(("نوسان", "کندل قوی (ATR)", (1 if body > 0 else -1) if abs(body) > 0.8 * atr else 0)) 
 
    # --- volume 
    obv = [0.0] 
    for i in range(1, len(c)): 
        obv.append(obv[-1] + (c[i]["v"] if c[i]["c"] > c[i - 1]["c"] else -c[i]["v"] if c[i]["c"] < c[i - 1]["c"] else 0)) 
    obv_e = ema(obv, 20) 
    out.append(("حجم", "OBV", 1 if obv[-1] > obv_e[-1] else -1)) 
 
    w = c[-20:] 
    vol = sum(x["v"] for x in w) 
    vwap = sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in w) / vol if vol else last["c"] 
    out.append(("حجم", "VWAP ۲۰ کندل", 1 if last["c"] > vwap else -1)) 
 
    # --- support / resistance 
    prev = c[-11:-1] 
    out.append(("حمایت/مقاومت", "شکست ۱۰ کندل", 
                1 if last["c"] > max(x["h"] for x in prev) else -1 if last["c"] < min(x["l"] for x in prev) else 0)) 
    return out, atr 
 
 
def decide(vs): 
    buys = sum(1 for _, _, v in vs if v == 1) 
    sells = sum(1 for _, _, v in vs if v == -1) 
    if buys >= MIN_VOTES and buys - sells >= MIN_MARGIN: 
        return "LONG", buys, sells 
    if sells >= MIN_VOTES and sells - buys >= MIN_MARGIN: 
        return "SHORT", buys, sells 
    return None, buys, sells 
 
 
# ---------------- state ---------------- 
def load_state(): 
    if os.path.exists(STATE_FILE): 
        with open(STATE_FILE) as f: 
            return json.load(f), False 
    return {"balance": START_BALANCE, "open": None, "history": [], 
            "last_candle": 0, "last_daily": ""}, True 
 
 
def save_state(st): 
    with open(STATE_FILE, "w") as f: 
        json.dump(st, f, ensure_ascii=False, indent=1) 
 
 
def stats(st): 
    h = st["history"] 
    wins = sum(1 for x in h if x["net"] > 0) 
    peak, dd = START_BALANCE, 0.0 
    for x in h: 
        peak = max(peak, x["balance"]) 
        dd = max(dd, (peak - x["balance"]) / peak * 100) 
    total = (st["balance"] / START_BALANCE - 1) * 100 
    return len(h), wins, len(h) - wins, total, dd 
 
 
def stats_text(st): 
    n, w, l, total, dd = stats(st) 
    if n == 0: 
        return "آمار کل: هنوز معامله‌ای بسته نشده." 
    return (f"آمار کل (همه‌ی معاملات، با ضررها):\n" 
            f"{n} معامله · {w} سود · {l} ضرر\n" 
            f"بازده کل: {total:+.2f}% · بیشترین افت: -{dd:.2f}%") 
 
 
# ---------------- trade logic ---------------- 
def open_trade(st, direction, entry, atr, vs, buys, sells, ts_close): 
    sl_d, tp_d = ATR_SL * atr, ATR_TP * atr 
    sl = entry - sl_d if direction == "LONG" else entry + sl_d 
