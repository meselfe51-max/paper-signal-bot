"""Paper-trading lab: 10 independent strategies (own wallets) + 1 group-vote account.
Kraken prices, Telegram alerts/commands, candlestick charts. No real money."""
import io
import json
import os
import time
import urllib.request
import uuid
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

# ---------------- settings ----------------
PAIRS = [
    ("XBTUSD", "BTC/USD"),
    ("ETHUSD", "ETH/USD"),
    ("SOLUSD", "SOL/USD"),
    ("XRPUSD", "XRP/USD"),
    ("ADAUSD", "ADA/USD"),
]
TIMEFRAMES = (60, 240)    # candle sizes in minutes used by the strategies
FEE = 0.004               # per side. CHECK YOUR KRAKEN TIER and set it (0.0025 .. 0.008)
SLIP = 0.0005             # slippage on market-style fills
RISK = 0.01               # share of wallet risked per trade
MAX_POS = 0.25            # max position size as share of wallet
MIN_TRADE = 20.0
HALT_DD = 0.15            # stop opening trades in an account after this drawdown from peak
START_BALANCE = 1000.0    # virtual dollars PER account
MIN_VOTES = 5             # group account: votes needed to open
MIN_MARGIN = 2
EXIT_VOTES = 4            # group account: opposite votes needed to close early
EXIT_MARGIN = 2
DAILY_HOUR_UTC = 6
MIN_CANDLES = 210
ANALYSIS_EVERY = 1800     # seconds between price checks (loop mode)
POLL_TIMEOUT = 10         # Telegram long-poll seconds (loop mode)
CHART_CANDLES = 120
NOTIFY_INDEPENDENT = False  # True = message for every trade of the 10 independent strategies
STATE_FILE = "state.json"
LOOP_MIN = float(os.environ.get("LOOP_MINUTES") or 0)
NAN = float("nan")

CACHE = {"time": 0, "info": {}, "data": {}}

MODE_TEXT = ("حالت: پاسخ سریع به پیام‌ها (چند ثانیه)" if LOOP_MIN > 0 else
             "حالت: فقط هر بار که بات اجرا شود به پیام‌ها جواب می‌دهد (ممکن است تا ۳۰ دقیقه یا بیشتر طول بکشد)")
HELP_TEXT = ("دستورها:\n"
             "/stats رتبه‌بندی ۱۰ استراتژی و حساب گروهی\n"
             "/votes دقت رأی هر روش در معاملات گروهی\n"
             "/status معاملات باز و قیمت‌ها\n"
             "/chart BTC چارت حساب گروهی (BTC, ETH, SOL, XRP, ADA)\n"
             "/chart BTC S3 چارت یک استراتژی (S1 تا S10)\n"
             "/help این راهنما\n\n" + MODE_TEXT + "\nهمه‌ی معاملات مجازی‌اند.")


# ---------------- helpers ----------------
def http_json(url, data=None, tries=3, timeout=30):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode() if data is not None else None,
                headers={"User-Agent": "paper-signal-bot", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa
            last = e
            if i < tries - 1:
                time.sleep(3)
    raise last


def fetch_candles(pair, interval):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    j = http_json(url)
    if j.get("error"):
        raise RuntimeError(str(j["error"]))
    res = j["result"]
    key = next(k for k in res if k != "last")
    return [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
         "c": float(r[4]), "v": float(r[6])}
        for r in res[key]
    ]


def fmt(x):
    if abs(x) >= 100:
        return f"{x:,.1f}"
    if abs(x) >= 1:
        return f"{x:,.3f}"
    return f"{x:,.5f}"


def tg_creds():
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat


def send(text):
    token, chat = tg_creds()
    if not token or not chat:
        print("[DRY RUN]\n" + text + "\n")
        return
    http_json(f"https://api.telegram.org/bot{token}/sendMessage",
              {"chat_id": chat, "text": text, "disable_web_page_preview": True})


def send_photo(png, caption=""):
    token, chat = tg_creds()
    if not token or not chat:
        print(f"[DRY RUN] photo {len(png)} bytes")
        return
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'.encode()]
    if caption:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption[:1000]}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="chart.png"\r\n'
                  f'Content-Type: image/png\r\n\r\n').encode())
    parts.append(png)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "paper-signal-bot"})
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


# ---------------- indicators ----------------
def ema(v, n):
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


def rsi_series(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def bb_series(closes, n=20, k=2):
    L = len(closes)
    mid, up, lo = [None] * L, [None] * L, [None] * L
    for i in range(n - 1, L):
        w = closes[i - n + 1:i + 1]
        m = sum(w) / n
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        mid[i], up[i], lo[i] = m, m + k * sd, m - k * sd
    return mid, up, lo


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
    k_s = [sum(ks[i - 2:i + 1]) / 3 for i in range(2, len(ks))]
    d_s = [sum(k_s[i - 2:i + 1]) / 3 for i in range(2, len(k_s))]
    return k_s[-1], d_s[-1]


def stoch_series(c, n=14):
    L = len(c)
    raw = [None] * L
    for i in range(n - 1, L):
        w = c[i - n + 1:i + 1]
        hh = max(x["h"] for x in w)
        ll = min(x["l"] for x in w)
        raw[i] = 0 if hh == ll else (c[i]["c"] - ll) / (hh - ll) * 100

    def smooth(s):
        out = [None] * len(s)
        for i in range(2, len(s)):
            w = s[i - 2:i + 1]
            if all(v is not None for v in w):
                out[i] = sum(w) / 3
        return out
    k = smooth(raw)
    return k, smooth(k)


def votes(c):
    """The original 10 voting methods (used ONLY by the group account)."""
    closes = [x["c"] for x in c]
    last = c[-1]
    out = []

    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    out.append(("روند", "EMA 20/50", 1 if e20[-1] > e50[-1] else -1))

    e12, e26 = ema(closes, 12), ema(closes, 26)
    macd = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    sig = ema(macd, 9)
    h_now = macd[-1] - sig[-1]
    h_prev = macd[-2] - sig[-2]
    out.append(("روند", "MACD", 1 if h_now > 0 and h_now > h_prev else -1 if h_now < 0 and h_now < h_prev else 0))

    out.append(("روند", "قیمت نسبت به EMA 200", 1 if last["c"] > e200[-1] else -1))

    r = rsi_last(closes)
    out.append(("مومنتوم", "RSI", 1 if r < 40 else -1 if r > 60 else 0))
    k, d = stoch(c)
    out.append(("مومنتوم", "Stochastic", 1 if k < 25 and k > d else -1 if k > 75 and k < d else 0))

    w = closes[-20:]
    mid = sum(w) / 20
    sd = (sum((x - mid) ** 2 for x in w) / 20) ** 0.5
    lo, hi = mid - 2 * sd, mid + 2 * sd
    pb = 0.5 if hi == lo else (last["c"] - lo) / (hi - lo)
    out.append(("نوسان", "Bollinger", 1 if pb < 0.15 else -1 if pb > 0.85 else 0))

    atr = atr_last(c)
    body = last["c"] - last["o"]
    out.append(("نوسان", "کندل قوی (ATR)", (1 if body > 0 else -1) if abs(body) > 0.8 * atr else 0))

    obv = [0.0]
    for i in range(1, len(c)):
        obv.append(obv[-1] + (c[i]["v"] if c[i]["c"] > c[i - 1]["c"] else -c[i]["v"] if c[i]["c"] < c[i - 1]["c"] else 0))
    obv_e = ema(obv, 20)
    out.append(("حجم", "OBV", 1 if obv[-1] > obv_e[-1] else -1))

    w = c[-20:]
    vol = sum(x["v"] for x in w)
    vwap = sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in w) / vol if vol else last["c"]
    out.append(("حجم", "VWAP ۲۰ کندل", 1 if last["c"] > vwap else -1))

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


def build_ind(c, tf):
    n = len(c)
    cl = [x["c"] for x in c]
    d = {"close": cl, "atr": atr_last(c)}
    d["e20"], d["e50"], d["e200"] = ema(cl, 20), ema(cl, 50), ema(cl, 200)
    e12, e26 = ema(cl, 12), ema(cl, 26)
    macd = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    sig = ema(macd, 9)
    d["macd"] = macd
    d["hist"] = [m - s if m is not None and s is not None else None for m, s in zip(macd, sig)]
    d["rsi"] = rsi_series(cl)
    d["k"], d["d"] = stoch_series(c)
    d["bbm"], d["bbu"], d["bbl"] = bb_series(cl)
    obv = [0.0]
    for i in range(1, n):
        obv.append(obv[-1] + (c[i]["v"] if c[i]["c"] > c[i - 1]["c"] else -c[i]["v"] if c[i]["c"] < c[i - 1]["c"] else 0))
    d["obv"], d["obve"] = obv, ema(obv, 20)
    d["avgv"] = [None] * n
    for i in range(20, n):
        d["avgv"][i] = sum(x["v"] for x in c[i - 20:i]) / 20
    vw = [None] * n
    pv = vv = 0.0
    day = None
    for i, x in enumerate(c):
        dd = x["t"] // 86400
        if dd != day:
            day, pv, vv = dd, 0.0, 0.0
        pv += (x["h"] + x["l"] + x["c"]) / 3 * x["v"]
        vv += x["v"]
        vw[i] = pv / vv if vv else x["c"]
    d["vwap"] = vw
    if tf == 60:
        d["votes"], _ = votes(c)
    return d


# ---------------- the 10 independent strategies + group ----------------
def side(t, long_cond, short_cond, why):
    return why if (long_cond if t["dir"] == "LONG" else short_cond) else None


def sig_ema(c, d):  # S1 EMA trend (4h)
    a, b, a1, b1 = d["e20"][-2], d["e50"][-2], d["e20"][-1], d["e50"][-1]
    e, cl = d["e200"][-1], d["close"][-1]
    if a <= b and a1 > b1 and cl > e:
        return "LONG"
    if a >= b and a1 < b1 and cl < e:
        return "SHORT"
    return None


def ex_ema(c, d, t):
    a, b = d["e20"][-1], d["e50"][-1]
    return side(t, a < b, a > b, "تقاطع معکوس EMA 20/50")


def sig_macd(c, d):  # S2 MACD (4h)
    h, h1, m = d["hist"][-2], d["hist"][-1], d["macd"][-1]
    if h <= 0 < h1 and m > 0:
        return "LONG"
    if h >= 0 > h1 and m < 0:
        return "SHORT"
    return None


def ex_macd(c, d, t):
    h1 = d["hist"][-1]
    return side(t, h1 < 0, h1 > 0, "هیستوگرام MACD برگشت")


def sig_e200(c, d):  # S3 EMA200 regime (4h)
    cl, e = d["close"], d["e200"]
    up = all(cl[-i] > e[-i] for i in (1, 2, 3))
    dn = all(cl[-i] < e[-i] for i in (1, 2, 3))
    if up and cl[-4] <= e[-4]:
        return "LONG"
    if dn and cl[-4] >= e[-4]:
        return "SHORT"
    return None


def ex_e200(c, d, t):
    cl, e = d["close"], d["e200"]
    below = cl[-1] < e[-1] and cl[-2] < e[-2]
    above = cl[-1] > e[-1] and cl[-2] > e[-2]
    return side(t, below, above, "دو کندل پشت‌سرهم آن‌طرف EMA 200")


def sig_rsi(c, d):  # S4 RSI reversion (1h)
    r0, r1 = d["rsi"][-2], d["rsi"][-1]
    if r0 < 30 <= r1:
        return "LONG"
    if r0 > 70 >= r1:
        return "SHORT"
    return None


def ex_rsi(c, d, t):
    r = d["rsi"][-1]
    return side(t, r >= 55, r <= 45, "RSI به ناحیه‌ی میانی رسید")


def sig_stoch(c, d):  # S5 Stochastic (1h)
    k0, d0, k1, d1 = d["k"][-2], d["d"][-2], d["k"][-1], d["d"][-1]
    if k0 <= d0 and k1 > d1 and max(k1, d1) < 25:
        return "LONG"
    if k0 >= d0 and k1 < d1 and min(k1, d1) > 75:
        return "SHORT"
    return None


def ex_stoch(c, d, t):
    k1 = d["k"][-1]
    return side(t, k1 > 80, k1 < 20, "Stochastic به ناحیه‌ی مقابل رسید")


def sig_bb(c, d):  # S6 Bollinger reversion (1h)
    cl = d["close"]
    if cl[-2] < d["bbl"][-2] and cl[-1] > d["bbl"][-1]:
        return "LONG"
    if cl[-2] > d["bbu"][-2] and cl[-1] < d["bbu"][-1]:
        return "SHORT"
    return None


def ex_bb(c, d, t):
    cl, m = d["close"][-1], d["bbm"][-1]
    return side(t, cl >= m, cl <= m, "رسیدن به باند میانی")


def sig_candle(c, d):  # S7 momentum candle (1h)
    x = c[-1]
    atr, av = d["atr"], d["avgv"][-1]
    rng = x["h"] - x["l"]
    if rng <= 0 or not av:
        return None
    body, pos = x["c"] - x["o"], (x["c"] - x["l"]) / rng
    if x["v"] > 1.5 * av:
        if body > 1.2 * atr and pos >= 0.75:
            return "LONG"
        if -body > 1.2 * atr and pos <= 0.25:
            return "SHORT"
    return None


def ex_none(c, d, t):
    return None


def sig_obv(c, d):  # S8 OBV (4h)
    o, e = d["obv"], d["obve"]
    if o[-2] <= e[-2] and o[-1] > e[-1] and o[-1] > o[-10]:
        return "LONG"
    if o[-2] >= e[-2] and o[-1] < e[-1] and o[-1] < o[-10]:
        return "SHORT"
    return None


def ex_obv(c, d, t):
    o, e = d["obv"][-1], d["obve"][-1]
    return side(t, o < e, o > e, "OBV از میانگینش برگشت")


def sig_vwap(c, d):  # S9 daily VWAP (1h)
    x = c[-1]
    if (x["t"] % 86400) // 3600 < 3:   # skip first hours of the UTC day (VWAP not meaningful yet)
        return None
    cl, vw, av = d["close"], d["vwap"], d["avgv"][-1]
    if not av or x["v"] <= av:
        return None
    if cl[-2] <= vw[-2] and cl[-1] > vw[-1]:
        return "LONG"
    if cl[-2] >= vw[-2] and cl[-1] < vw[-1]:
        return "SHORT"
    return None


def ex_vwap(c, d, t):
    if c[-1]["t"] // 86400 != t["opened"] // 86400:
        return "پایان روز UTC (VWAP دوباره شروع می‌شود)"
    cl, vw = d["close"][-1], d["vwap"][-1]
    return side(t, cl < vw, cl > vw, "برگشت قیمت به آن‌طرف VWAP")


def sig_donchian(c, d):  # S10 breakout (4h)
    hi = max(x["h"] for x in c[-21:-1])
    lo = min(x["l"] for x in c[-21:-1])
    cl = d["close"][-1]
    if cl > hi:
        return "LONG"
    if cl < lo:
        return "SHORT"
    return None


def ex_donchian(c, d, t):
    hi = max(x["h"] for x in c[-11:-1])
    lo = min(x["l"] for x in c[-11:-1])
    cl = d["close"][-1]
    return side(t, cl < lo, cl > hi, "شکست کانال ۱۰ کندلی در جهت مخالف")


def sig_group(c, d):  # GROUP: the original 10-vote consensus (1h)
    return decide(d["votes"])[0]


def ex_group(c, d, t):
    vs = d["votes"]
    buys = sum(1 for _, _, v in vs if v == 1)
    sells = sum(1 for _, _, v in vs if v == -1)
    own, opp = (buys, sells) if t["dir"] == "LONG" else (sells, buys)
    if opp >= EXIT_VOTES and opp - own >= EXIT_MARGIN:
        return f"تغییر نظر روش‌ها ({opp} مخالف، {own} موافق)"
    return None


# sl / tp are in ATR multiples; trail = (start ATRs, distance ATRs); maxc = max candles to hold
STRATS = [
    dict(id="S1", name="EMA", tf=240, sl=2.5, tp=None, trail=None, maxc=120, sig=sig_ema, exit=ex_ema),
    dict(id="S2", name="MACD", tf=240, sl=2.0, tp=None, trail=None, maxc=120, sig=sig_macd, exit=ex_macd),
    dict(id="S3", name="EMA200", tf=240, sl=3.0, tp=None, trail=None, maxc=150, sig=sig_e200, exit=ex_e200),
    dict(id="S4", name="RSI", tf=60, sl=1.5, tp=None, trail=None, maxc=24, sig=sig_rsi, exit=ex_rsi),
    dict(id="S5", name="Stochastic", tf=60, sl=1.5, tp=None, trail=None, maxc=24, sig=sig_stoch, exit=ex_stoch),
    dict(id="S6", name="Bollinger", tf=60, sl=1.5, tp=None, trail=None, maxc=24, sig=sig_bb, exit=ex_bb),
    dict(id="S7", name="کندل قوی", tf=60, sl=1.5, tp=None, trail=(0.0, 1.5), maxc=12, sig=sig_candle, exit=ex_none),
    dict(id="S8", name="OBV", tf=240, sl=2.0, tp=None, trail=None, maxc=120, sig=sig_obv, exit=ex_obv),
    dict(id="S9", name="VWAP روزانه", tf=60, sl=1.5, tp=None, trail=None, maxc=24, sig=sig_vwap, exit=ex_vwap),
    dict(id="S10", name="شکست کانال", tf=240, sl=2.0, tp=None, trail=None, maxc=120, sig=sig_donchian, exit=ex_donchian),
    dict(id="GROUP", name="گروهی (رأی‌گیری)", tf=60, sl=1.5, tp=3.0, trail=(1.5, 1.0), maxc=72,
         sig=sig_group, exit=ex_group),
]
STRAT_BY_ID = {s["id"]: s for s in STRATS}


# ---------------- state ----------------
def new_acct():
    return {"balance": START_BALANCE, "open": {}, "history": [], "halted": False}


def new_state():
    return {"v": 3, "accts": {}, "last": {}, "acc": {}, "last_daily": "", "failed": [], "tg_offset": 0}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                st = json.load(f)
            if st.get("v") == 3:
                st.setdefault("tg_offset", 0)
                return st, False
        except Exception:
            pass
    return new_state(), True


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def acct(st, sid):
    return st["accts"].setdefault(sid, new_acct())


def free_cash(a):
    return a["balance"] - sum(t["size"] for t in a["open"].values())


def peak_balance(a):
    return max([START_BALANCE] + [x["balance"] for x in a["history"]])


def acct_stats(a):
    h = a["history"]
    wins = [x for x in h if x["net"] > 0]
    gw = sum(x["net"] for x in wins)
    gl = -sum(x["net"] for x in h if x["net"] <= 0)
    pf = gw / gl if gl > 0 else None
    peak, dd = START_BALANCE, 0.0
    for x in h:
        peak = max(peak, x["balance"])
        dd = max(dd, (peak - x["balance"]) / peak * 100)
    return len(h), len(wins), pf, (a["balance"] / START_BALANCE - 1) * 100, dd


def board_text(st):
    rows = []
    for s in STRATS:
        a = st["accts"].get(s["id"])
        if a:
            n, w, pf, ret, dd = acct_stats(a)
            rows.append((ret, s, a, n, w, pf, dd))
    rows.sort(key=lambda r: -r[0])
    lines = [f"رتبه‌بندی (هر حساب کیف پول مجازی جدا، شروع {START_BALANCE:.0f} دلار):"]
    for ret, s, a, n, w, pf, dd in rows:
        pf_t = f"{pf:.2f}" if pf is not None else "-"
        lines.append(f"{s['id']} {s['name']}: {ret:+.1f}% · {n} معامله ({w} سود) · PF {pf_t} · افت {dd:.1f}%"
                     f"{' ⚠️' if n < 30 else ''}{' ⛔' if a.get('halted') else ''}")
    lines.append("⚠️ یعنی کمتر از ۳۰ معامله و هنوز بی‌اعتبار. PF = سود ناخالص ÷ ضرر ناخالص. ⛔ = توقف بعد از افت زیاد.")
    return "\n".join(lines)


def votes_text(st):
    acc = st.get("acc", {})
    if not acc:
        return "هنوز معامله‌ی گروهی‌ای بسته نشده که مشخص شود حق با کدام روش بوده."
    rows = []
    for nme, r in acc.items():
        tot = r["right"] + r["wrong"]
        rows.append((r["right"] / tot if tot else 0, nme, r, tot))
    rows.sort(reverse=True)
    lines = ["دقت رأی هر روش در معاملات گروهی (حق با کدام بود):"]
    for frac, nme, r, tot in rows:
        lines.append(f"{nme}: {r['right']} درست · {r['wrong']} غلط · {r['neutral']} بی‌نظر"
                     + (f" · {frac * 100:.0f}%" if tot else ""))
    lines.append("معیار: جهت حرکت قیمت از ورود تا خروج، نه سود بعد از کارمزد. با داده‌ی کم قابل اتکا نیست.")
    return "\n".join(lines)


def status_text(st):
    lines = []
    for s in STRATS:
        a = st["accts"].get(s["id"])
        if not a:
            continue
        for name, t in a["open"].items():
            price = CACHE["info"].get(name)
            is_long = t["dir"] == "LONG"
            line = f"[{s['id']}] {name} {'Long' if is_long else 'Short'} از {fmt(t['entry'])} · حد ضرر {fmt(t['sl'])}"
            if price:
                move = (price / t["entry"] - 1) * (1 if is_long else -1)
                line += f" · شناور {t['size'] * move:+.2f}$"
            lines.append(line)
    head = f"معاملات باز: {len(lines)}" if lines else "الان هیچ معامله‌ی بازی نیست."
    out = [head] + lines
    if CACHE["time"]:
        out.append(f"\nآخرین بررسی قیمت‌ها: {(time.time() - CACHE['time']) / 60:.0f} دقیقه پیش")
    out.append(MODE_TEXT)
    return "\n".join(out)


# ---------------- trade logic ----------------
def open_trade(st, a, s, name, direction, raw, closed, d):
    tf = s["tf"]
    opened = closed[-1]["t"] + tf * 60
    base = raw[-1]["o"] if raw[-1]["t"] == opened else closed[-1]["c"]
    entry = base * (1 + SLIP) if direction == "LONG" else base * (1 - SLIP)
    atr = d["atr"]
    sl_d = s["sl"] * atr
    if sl_d <= 0:
        return None
    sl = entry - sl_d if direction == "LONG" else entry + sl_d
    tp = None
    if s["tp"]:
        tp = entry + s["tp"] * atr if direction == "LONG" else entry - s["tp"] * atr
    size = min(a["balance"] * RISK / (sl_d / entry), a["balance"] * MAX_POS, free_cash(a))
    if size < MIN_TRADE:
        return None
    t = {"dir": direction, "entry": entry, "sl": sl, "sl0": sl, "tp": tp, "size": size,
         "atr": atr, "best": entry, "trail": s["trail"], "opened": opened, "tf": tf,
         "maxs": s["maxc"] * tf * 60, "checked": 0, "balance_before": a["balance"]}
    word = "خرید (Long)" if direction == "LONG" else "فروش (Short)"
    if s["id"] == "GROUP":
        vs = d["votes"]
        t["votes"] = [[n, v] for _, n, v in vs]
        buys = sum(1 for _, _, v in vs if v == 1)
        sells = sum(1 for _, _, v in vs if v == -1)
        a["open"][name] = t
        mark = {1: "✅", -1: "❌", 0: "➖"}
        sign = 1 if direction == "LONG" else -1
        lines = "\n".join(f"{mark[v * sign]} {n}" for _, n, v in vs)
        agree = buys if direction == "LONG" else sells
        text = (f"{'🟢' if direction == 'LONG' else '🔴'} [گروهی] سیگنال {word} · {name}\n"
                f"اجماع: {agree} از ۱۰ ({buys} خرید، {10 - buys - sells} خنثی، {sells} فروش)\n"
                f"{lines}\n\n"
                f"ورود: {fmt(entry)}\n"
                f"حد سود: {fmt(tp)} ({(tp / entry - 1) * 100:+.2f}%)\n"
                f"حد ضرر: {fmt(sl)} ({(sl / entry - 1) * 100:+.2f}%)\n"
                f"اندازه: {size:.2f} دلار از {a['balance']:.2f}\n\n"
                f"رأی هر روش ثبت شد؛ بعد از بسته شدن معامله مشخص می‌شود حق با کدام بود. (مجازی)")
        return text, (name, tf, "GROUP")
    a["open"][name] = t
    text = (f"{'🟢' if direction == 'LONG' else '🔴'} [{s['id']} {s['name']}] {word} · {name}\n"
            f"ورود {fmt(entry)} · حد ضرر {fmt(sl)} · اندازه {size:.0f}$ (مجازی)")
    return text, None


def check_exit(st, a, s, name, m, now):
    raw, closed, d = m
    t = a["open"][name]
    is_long = t["dir"] == "LONG"
    tf = t["tf"]
    px, why, market = None, None, True

    for c in raw:
        if c["t"] < t["opened"]:
            continue
        done = c["t"] + tf * 60 <= now
        if done and c["t"] <= t["checked"]:
            continue
        sl_hit = (c["l"] <= t["sl"]) if is_long else (c["h"] >= t["sl"])
        tp_hit = t["tp"] is not None and ((c["h"] >= t["tp"]) if is_long else (c["l"] <= t["tp"]))
        if sl_hit:  # if both hit in one candle, assume the worse case
            gap = (c["o"] < t["sl"]) if is_long else (c["o"] > t["sl"])
            px = c["o"] if gap else t["sl"]
            why = ("رسیدن به حد ضرر" if abs(t["sl"] - t["sl0"]) < 1e-12
                   else "حد ضرر متحرک (بخشی از سود قفل شده بود)")
            if gap:
                why += " (با گپ)"
            break
        if tp_hit:
            px, why, market = t["tp"], "رسیدن به حد سود", False
            break
        if done:
            if t.get("trail"):
                start, dist = t["trail"]
                if is_long:
                    t["best"] = max(t["best"], c["h"])
                    gain = t["best"] - t["entry"]
                else:
                    t["best"] = min(t["best"], c["l"])
                    gain = t["entry"] - t["best"]
                if gain >= start * t["atr"]:
                    if is_long:
                        t["sl"] = max(t["sl"], t["best"] - dist * t["atr"])
                    else:
                        t["sl"] = min(t["sl"], t["best"] + dist * t["atr"])
            t["checked"] = c["t"]

    if px is None and closed and closed[-1]["t"] >= t["opened"]:
        try:
            w = s["exit"](closed, d, t)
        except Exception as e:  # noqa
            print(f"[EXIT ERROR] {s['id']} {name}: {e}")
            w = None
        if w:
            px = raw[-1]["o"] if raw[-1]["t"] > closed[-1]["t"] else closed[-1]["c"]
            why = w

    if px is None and now - t["opened"] > t["maxs"]:
        px, why = raw[-1]["c"], "سقف زمانی نگهداری"

    if px is None:
        return None

    if market:
        px = px * (1 - SLIP) if is_long else px * (1 + SLIP)
    move = (px / t["entry"] - 1) * (1 if is_long else -1)
    net = t["size"] * move - t["size"] * FEE * 2
    a["balance"] += net
    rec = {"pair": name, "dir": t["dir"], "entry": t["entry"], "exit": px, "size": t["size"],
           "net": net, "balance": a["balance"], "opened": t["opened"], "closed": int(now), "why": why}
    a["history"].append(rec)
    del a["open"][name]
    pct = net / t["balance_before"] * 100
    win = net > 0
    hours = max(0, (now - t["opened"]) / 3600)

    if s["id"] == "GROUP":
        pm = px / t["entry"] - 1
        truth = 1 if pm > 0 else -1 if pm < 0 else 0
        right, wrong, neutral = [], [], []
        acc = st.setdefault("acc", {})
        for nme, v in t.get("votes", []):
            r = acc.setdefault(nme, {"right": 0, "wrong": 0, "neutral": 0})
            if v == 0 or truth == 0:
                r["neutral"] += 1
                neutral.append(nme)
            elif v == truth:
                r["right"] += 1
                right.append(nme)
            else:
                r["wrong"] += 1
                wrong.append(nme)
        n, w, pf, ret, dd = acct_stats(a)
        text = (f"{'🟢' if win else '🔴'} [گروهی] معامله بسته شد · {name} · {t['dir']}\n"
                f"دلیل: {why} · حدود {hours:.0f} ساعت\n"
                f"{'سود' if win else 'ضرر'}: {net:+.2f} دلار ({pct:+.2f}%) بعد از کارمزد و لغزش\n"
                f"ورود {fmt(t['entry'])} ← خروج {fmt(px)}\n\n"
                f"حرکت قیمت: {'بالا' if truth == 1 else 'پایین' if truth == -1 else 'ثابت'} ({pm * 100:+.2f}%)\n"
                f"✅ حق با: {('، '.join(right)) or '-'}\n"
                f"❌ اشتباه: {('، '.join(wrong)) or '-'}\n"
                f"➖ بی‌نظر: {('، '.join(neutral)) or '-'}\n\n"
                f"حساب گروهی: {a['balance']:.2f} دلار · {n} معامله ({w} سود) · {ret:+.1f}%")
        return text, (name, tf, "GROUP")
    text = (f"{'🟢' if win else '🔴'} [{s['id']} {s['name']}] {name} بسته شد: {net:+.2f}$ ({pct:+.2f}%)\n"
            f"{why} · ورود {fmt(t['entry'])} ← خروج {fmt(px)} · حساب {a['balance']:.2f}$")
    return text, None


# ---------------- chart ----------------
def make_chart(name, candles, tf, st, sid):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    times = [c["t"] for c in candles]
    closes = [c["c"] for c in candles]
    n_all = len(candles)
    N = min(CHART_CANDLES, n_all)
    off = n_all - N
    view = candles[off:]
    xs = list(range(N))
    cols = ["#26a69a" if c["c"] >= c["o"] else "#ef5350" for c in view]

    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    bb_m, bb_u, bb_l = bb_series(closes)
    rsi = rsi_series(closes)
    e12, e26 = ema(closes, 12), ema(closes, 26)
    macd = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    sig = ema(macd, 9)
    hist = [m - s if m is not None and s is not None else None for m, s in zip(macd, sig)]

    def seg(s):
        return [NAN if v is None else v for v in s[off:]]

    fig, (ax, axv, axr, axm) = plt.subplots(
        4, 1, figsize=(11, 10), sharex=True,
        gridspec_kw={"height_ratios": [5, 1.3, 1.3, 1.6], "hspace": 0.05})

    rng = max(c["h"] for c in view) - min(c["l"] for c in view)
    ax.vlines(xs, [c["l"] for c in view], [c["h"] for c in view], colors=cols, linewidth=0.8)
    ax.bar(xs, [max(abs(c["c"] - c["o"]), rng * 0.002) for c in view],
           bottom=[min(c["o"], c["c"]) for c in view], width=0.6, color=cols, linewidth=0)
    ax.plot(xs, seg(e20), color="#2962ff", linewidth=1.0, label="EMA 20")
    ax.plot(xs, seg(e50), color="#ff9800", linewidth=1.0, label="EMA 50")
    ax.plot(xs, seg(e200), color="#8e24aa", linewidth=1.2, label="EMA 200")
    ax.plot(xs, seg(bb_u), color="#9e9e9e", linewidth=0.7, linestyle="--", label="Bollinger 20,2")
    ax.plot(xs, seg(bb_l), color="#9e9e9e", linewidth=0.7, linestyle="--")
    ax.fill_between(xs, seg(bb_l), seg(bb_u), color="#9e9e9e", alpha=0.08)

    levels = []
    shown = set()

    def lab(s):
        if s in shown:
            return None
        shown.add(s)
        return s

    a = st["accts"].get(sid) or new_acct()
    for x in a["history"]:
        if x.get("pair") != name or not x.get("opened"):
            continue
        i0 = bisect_left(times, x["opened"]) - off
        i1 = bisect_right(times, x["closed"]) - 1 - off
        if i1 < 0 or i0 >= N:
            continue
        i0, i1 = max(0, min(N - 1, i0)), max(0, min(N - 1, i1))
        is_long = x["dir"] == "LONG"
        col = "#2e7d32" if x["net"] > 0 else "#c62828"
        ax.plot([i0, i1], [x["entry"], x["exit"]], color=col, linestyle=":", linewidth=1.2)
        ax.scatter([i0], [x["entry"]], marker="^" if is_long else "v", s=70,
                   color="#1b5e20" if is_long else "#b71c1c", zorder=5,
                   label=lab("Entry long" if is_long else "Entry short"))
        ax.scatter([i1], [x["exit"]], marker="x", s=50, color=col, zorder=5, label=lab("Exit"))
        ax.annotate(f"{x['net']:+.1f}$", (i1, x["exit"]), textcoords="offset points",
                    xytext=(4, 6), fontsize=7, color=col)

    t = a["open"].get(name)
    if t:
        i0 = max(0, min(N - 1, bisect_left(times, t["opened"]) - off))
        is_long = t["dir"] == "LONG"
        ax.scatter([i0], [t["entry"]], marker="^" if is_long else "v", s=90,
                   color="#1b5e20" if is_long else "#b71c1c", zorder=6, label=lab("Open trade"))
        lv = [(t["entry"], "#616161", "Entry"), (t["sl"], "#c62828", "SL")]
        if t.get("tp"):
            lv.append((t["tp"], "#2e7d32", "TP"))
        for y, c, txt in lv:
            ax.axhline(y, color=c, linestyle="--", linewidth=1.0)
            ax.text(N - 0.5, y, f" {txt} {fmt(y)}", color=c, va="center", fontsize=8)
            levels.append(y)

    last = candles[-1]["c"]
    ax.axhline(last, color="#1565c0", linestyle=":", linewidth=0.9)
    ax.text(N - 0.5, last, f" {fmt(last)}", color="#1565c0", va="bottom", fontsize=8)
    lo = min([c["l"] for c in view] + levels)
    hi = max([c["h"] for c in view] + levels)
    pad = (hi - lo) * 0.04 or hi * 0.01
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(-1, N + 9)
    ax.legend(loc="upper left", fontsize=7, ncol=3)
    ax.set_ylabel("Price (USD)")

    axv.bar(xs, [c["v"] for c in view], width=0.6, color=cols, linewidth=0)
    axv.set_ylabel("Volume")
    axr.plot(xs, seg(rsi), color="#6a1b9a", linewidth=1.0)
    for y, c in ((70, "#c62828"), (50, "#9e9e9e"), (30, "#2e7d32")):
        axr.axhline(y, color=c, linestyle="--", linewidth=0.7)
    axr.set_ylim(0, 100)
    axr.set_ylabel("RSI 14")
    hs = seg(hist)
    axm.bar(xs, hs, width=0.6, color=["#26a69a" if (h == h and h >= 0) else "#ef5350" for h in hs], linewidth=0)
    axm.plot(xs, seg(macd), color="#2962ff", linewidth=1.0, label="MACD")
    axm.plot(xs, seg(sig), color="#ff9800", linewidth=1.0, label="Signal")
    axm.legend(loc="upper left", fontsize=7, ncol=2)
    axm.set_ylabel("MACD")

    step = max(1, N // 8)
    ticks = list(range(0, N, step))
    axm.set_xticks(ticks)
    axm.set_xticklabels(
        [datetime.fromtimestamp(times[off + i], tz=timezone.utc).strftime("%m-%d %H:%M") for i in ticks],
        rotation=30, ha="right", fontsize=8)
    axm.set_xlabel("UTC")
    for g in (ax, axv, axr, axm):
        g.grid(alpha=0.25)
    fig.suptitle(f"{name}  {tf}m candles  account {sid}  last {fmt(last)}", fontsize=13, y=0.92)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ---------------- analysis cycle ----------------
def deliver(st, msgs):
    failed = False
    for text, spec in msgs:
        try:
            send(text)
        except Exception as e:  # noqa
            print(f"[SEND ERROR] {e}")
            failed = True
            continue
        if spec:
            name, tf, sid = spec
            m = CACHE["data"].get((name, tf))
            if m:
                try:
                    send_photo(make_chart(name, m[0], tf, st, sid), caption=f"{name} - {tf}m - {sid}")
                except Exception as e:  # noqa
                    print(f"[CHART ERROR] {name}: {e}")
    return failed


def analysis_cycle(st, fresh=False):
    now = time.time()
    msgs = []
    market, info = {}, {}
    for s in STRATS:
        acct(st, s["id"])

    if fresh:
        msgs.append((f"✅ آزمایشگاه استراتژی روشن شد: ۱۰ استراتژی مستقل + یک حساب گروهی، هرکدام {START_BALANCE:.0f} "
                     f"دلار مجازی جدا. فقط معاملات گروهی پیام و چارت دارند. برای رتبه‌بندی /stats و برای راهنما /help.", None))

    # 1) fetch + indicators for every (coin, timeframe)
    for pair, name in PAIRS:
        for tf in TIMEFRAMES:
            key = f"{name}|{tf}"
            try:
                raw = fetch_candles(pair, tf)
                closed = [x for x in raw if x["t"] + tf * 60 <= now]
                if len(closed) < MIN_CANDLES:
                    raise RuntimeError(f"فقط {len(closed)} کندل بسته موجود است")
                market[(name, tf)] = (raw, closed, build_ind(closed, tf))
                if tf == 60:
                    info[name] = raw[-1]["c"]
                if key in st["failed"]:
                    st["failed"].remove(key)
            except Exception as e:  # noqa
                print(f"[ERROR] {key}: {e}")
                if key not in st["failed"]:
                    st["failed"].append(key)
                    msgs.append((f"⚠️ مشکل در دریافت یا تحلیل {name} ({tf} دقیقه): {str(e)[:150]}", None))
            time.sleep(1)
    CACHE["time"], CACHE["info"], CACHE["data"] = now, info, market

    # 2) exits
    exited = set()
    for s in STRATS:
        a = acct(st, s["id"])
        for name in list(a["open"]):
            m = market.get((name, s["tf"]))
            if not m:
                continue
            res = check_exit(st, a, s, name, m, now)
            if res:
                exited.add((s["id"], name))
                if s["id"] == "GROUP" or NOTIFY_INDEPENDENT:
                    msgs.append(res)

    # 3) drawdown halts
    for s in STRATS:
        a = acct(st, s["id"])
        h = a["balance"] < peak_balance(a) * (1 - HALT_DD)
        if h and not a.get("halted") and (s["id"] == "GROUP" or NOTIFY_INDEPENDENT):
            msgs.append((f"⛔ حساب {s['id']} {s['name']} بیش از {HALT_DD * 100:.0f}% از اوج افت کرد. "
                         f"معامله‌ی جدید برایش باز نمی‌شود.", None))
        a["halted"] = h

    # 4) new signals: once per closed candle per (coin, timeframe)
    for (name, tf), (raw, closed, d) in market.items():
        key = f"{name}|{tf}"
        if st["last"].get(key) == closed[-1]["t"]:
            continue
        st["last"][key] = closed[-1]["t"]
        for s in STRATS:
            if s["tf"] != tf:
                continue
            a = acct(st, s["id"])
            if name in a["open"] or a.get("halted") or (s["id"], name) in exited:
                continue
            try:
                direction = s["sig"](closed, d)
            except Exception as e:  # noqa
                print(f"[SIGNAL ERROR] {s['id']} {name}: {e}")
                continue
            if direction:
                res = open_trade(st, a, s, name, direction, raw, closed, d)
                if res and (s["id"] == "GROUP" or NOTIFY_INDEPENDENT):
                    msgs.append(res)

    # 5) daily report
    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    if now_utc.hour == DAILY_HOUR_UTC and st["last_daily"] != today:
        st["last_daily"] = today
        opened = sum(len(acct(st, s["id"])["open"]) for s in STRATS)
        msgs.append((f"📊 گزارش روزانه\n\n{board_text(st)}\n\nمعاملات باز الان: {opened}\n\n{votes_text(st)}", None))

    save_state(st)
    return deliver(st, msgs)


# ---------------- telegram commands ----------------
def reply(st, text):
    parts = text.split()
    c = parts[0].lower().split("@")[0]
    arg = parts[1].upper() if len(parts) > 1 else ""
    if c in ("/start", "/help"):
        send("✅ بات وصل است.\n\n" + HELP_TEXT)
    elif c == "/status":
        send(status_text(st))
    elif c == "/stats":
        send(board_text(st))
    elif c == "/votes":
        send(votes_text(st))
    elif c == "/chart":
        key = arg.split("/")[0].replace("XBT", "BTC")
        target = next(((p, n) for p, n in PAIRS if n.split("/")[0] == key), None)
        sid = parts[2].upper() if len(parts) > 2 else "GROUP"
        s = STRAT_BY_ID.get(sid)
        if not target or not s:
            send("مثال: /chart BTC یا /chart BTC S3\nارزها: " + "، ".join(n.split("/")[0] for _, n in PAIRS)
                 + "\nحساب‌ها: S1 تا S10 و GROUP")
            return
        pair, name = target
        try:
            raw = fetch_candles(pair, s["tf"])
        except Exception:  # noqa
            raw = (CACHE["data"].get((name, s["tf"])) or (None,))[0]
        if not raw:
            send("داده‌ی قیمت در دسترس نیست.")
            return
        send_photo(make_chart(name, raw, s["tf"], st, sid),
                   caption=f"{name} - {s['tf']}m - {sid} - last {fmt(raw[-1]['c'])}")
    else:
        send("دستور ناشناخته. /help را بفرست.")


def handle_updates(st, wait):
    token, chat = tg_creds()
    if not token or not chat:
        time.sleep(max(1, wait))
        return
    params = {"timeout": int(wait), "allowed_updates": ["message"]}
    if st.get("tg_offset"):
        params["offset"] = st["tg_offset"]
    j = http_json(f"https://api.telegram.org/bot{token}/getUpdates", params,
                  tries=1, timeout=int(wait) + 20)
    updates = j.get("result", [])
    for u in updates:
        st["tg_offset"] = u["update_id"] + 1
        msg = u.get("message")
        if not msg or str(msg.get("chat", {}).get("id")) != chat:
            continue  # ignore everyone except you
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        try:
            reply(st, text)
        except Exception as e:  # noqa
            print(f"[COMMAND ERROR] {e}")
            try:
                send("⚠️ خطا در اجرای دستور: " + str(e)[:150])
            except Exception:  # noqa
                pass
    if updates:
        save_state(st)


# ---------------- main ----------------
def main():
    st, fresh = load_state()
    if LOOP_MIN <= 0:
        failed = analysis_cycle(st, fresh)
        try:
            handle_updates(st, 0)
        except Exception as e:  # noqa
            print(f"[UPDATES ERROR] {e}")
        save_state(st)
        if failed:
            raise SystemExit(1)
        return

    end = time.time() + LOOP_MIN * 60
    next_analysis = 0.0
    while time.time() < end:
        if time.time() >= next_analysis:
            try:
                analysis_cycle(st, fresh)
                fresh = False
            except Exception as e:  # noqa
                print(f"[CYCLE ERROR] {e}")
            next_analysis = time.time() + ANALYSIS_EVERY
        try:
            handle_updates(st, POLL_TIMEOUT)
        except Exception as e:  # noqa
            print(f"[UPDATES ERROR] {e}")
            time.sleep(5)
    save_state(st)


if __name__ == "__main__":
    main()
