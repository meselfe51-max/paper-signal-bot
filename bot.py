"""Multi-coin paper-trading signal bot (Kraken public prices, Telegram alerts). No real money."""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

# ---------------- settings ----------------
PAIRS = [                # (Kraken pair, display name)
    ("XBTUSD", "BTC/USD"),
    ("ETHUSD", "ETH/USD"),
    ("SOLUSD", "SOL/USD"),
    ("XRPUSD", "XRP/USD"),
    ("ADAUSD", "ADA/USD"),
]
INTERVAL = 60            # candle size in minutes (1h)
MIN_VOTES = 5
MIN_MARGIN = 2
ATR_SL = 1.5
ATR_TP = 2.5
RISK = 0.02
FEE = 0.001
MAX_HOURS = 24
START_BALANCE = 1000.0   # virtual dollars PER coin
DAILY_HOUR_UTC = 6
MIN_CANDLES = 210        # EMA 200 needs enough history
STATE_FILE = "state.json"


# ---------------- helpers ----------------
def http_json(url, data=None, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode() if data is not None else None,
                headers={"User-Agent": "paper-signal-bot", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa
            last = e
            time.sleep(3)
    raise last


def fetch_candles(pair):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={INTERVAL}"
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


def send(text):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        print("[DRY RUN]\n" + text + "\n")
        return
    http_json(f"https://api.telegram.org/bot{token}/sendMessage",
              {"chat_id": chat, "text": text, "disable_web_page_preview": True})


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


def votes(c):
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


# ---------------- state ----------------
def new_pair_state():
    return {"balance": START_BALANCE, "open": None, "history": [], "last_candle": 0}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                st = json.load(f)
            if "pairs" in st and "last_daily" in st:
                st.setdefault("failed", [])
                return st, False
        except Exception:
            pass
    return {"pairs": {}, "last_daily": "", "failed": []}, True


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def stats(ps):
    h = ps["history"]
    wins = sum(1 for x in h if x["net"] > 0)
    peak, dd = START_BALANCE, 0.0
    for x in h:
        peak = max(peak, x["balance"])
        dd = max(dd, (peak - x["balance"]) / peak * 100)
    total = (ps["balance"] / START_BALANCE - 1) * 100
    return len(h), wins, len(h) - wins, total, dd


def stats_text(ps):
    n, w, l, total, dd = stats(ps)
    if n == 0:
        return "آمار این ارز: هنوز معامله‌ای بسته نشده."
    return (f"آمار این ارز (همه‌ی معاملات، با ضررها):\n"
            f"{n} معامله · {w} سود · {l} ضرر\n"
            f"بازده کل: {total:+.2f}% · بیشترین افت: -{dd:.2f}%")


# ---------------- trade logic ----------------
def open_trade(ps, name, direction, entry, atr, vs, buys, sells, ts_close):
    sl_d, tp_d = ATR_SL * atr, ATR_TP * atr
    sl = entry - sl_d if direction == "LONG" else entry + sl_d
    tp = entry + tp_d if direction == "LONG" else entry - tp_d
    size = min(ps["balance"] * RISK / (sl_d / entry), ps["balance"])
    ps["open"] = {"dir": direction, "entry": entry, "sl": sl, "tp": tp, "size": size,
                  "opened": ts_close, "balance_before": ps["balance"]}
    word = "خرید (Long)" if direction == "LONG" else "فروش (Short)"
    mark = {1: "✅", -1: "❌", 0: "➖"}
    sign = 1 if direction == "LONG" else -1
    lines = "\n".join(f"{mark[v * sign]} {n}" for _, n, v in vs)
    agree = buys if direction == "LONG" else sells
    return (f"{'🟢' if direction == 'LONG' else '🔴'} سیگنال {word} · {name}\n"
            f"تایم‌فریم {INTERVAL} دقیقه\n\n"
            f"✅ موافق · ❌ مخالف · ➖ خنثی\n"
            f"اجماع: {agree} از ۱۰ روش هم‌نظر ({buys} خرید، {10 - buys - sells} خنثی، {sells} فروش)\n"
            f"{lines}\n\n"
            f"قیمت ورود: {fmt(entry)}\n"
            f"حد سود: {fmt(tp)} ({(tp / entry - 1) * 100:+.2f}%)\n"
            f"حد ضرر: {fmt(sl)} ({(sl / entry - 1) * 100:+.2f}%)\n"
            f"ریسک به ریوارد: 1 به {ATR_TP / ATR_SL:.1f}\n\n"
            f"این معامله مجازی است. سود یا ضرر بعد از بسته شدن اعلام می‌شود.")


def check_exit(ps, name, candles, now):
    t = ps["open"]
    px, why = None, None
    for c in candles:
        if c["t"] < t["opened"]:
            continue
        if t["dir"] == "LONG":
            hit_sl, hit_tp = c["l"] <= t["sl"], c["h"] >= t["tp"]
        else:
            hit_sl, hit_tp = c["h"] >= t["sl"], c["l"] <= t["tp"]
        if hit_sl:
            px, why = t["sl"], "رسیدن به حد ضرر"
            break
        if hit_tp:
            px, why = t["tp"], "رسیدن به حد سود"
            break
    if px is None and now - t["opened"] > MAX_HOURS * 3600:
        px, why = candles[-1]["c"], f"گذشتن {MAX_HOURS} ساعت"
    if px is None:
        return None

    move = (px / t["entry"] - 1) * (1 if t["dir"] == "LONG" else -1)
    net = t["size"] * move - t["size"] * FEE * 2
    ps["balance"] += net
    hours = (now - t["opened"]) / 3600
    ps["history"].append({"dir": t["dir"], "entry": t["entry"], "exit": px, "net": net,
                          "balance": ps["balance"], "closed": int(now)})
    ps["open"] = None
    pct = net / t["balance_before"] * 100
    win = net > 0
    return (f"{'🟢' if win else '🔴'} معامله بسته شد · {name}\n"
            f"دلیل: {why} · مدت {hours:.0f} ساعت\n\n"
            f"{'سود' if win else 'ضرر'}: {pct:+.2f}% روی سرمایه\n"
            f"معادل {net:+.2f} دلار (بعد از کارمزد)\n"
            f"ورود {fmt(t['entry'])} ← خروج {fmt(px)}\n"
            f"موجودی مجازی این ارز: {ps['balance']:.2f} دلار\n\n"
            f"{stats_text(ps)}")


def main():
    st, fresh = load_state()
    now = time.time()
    msgs = []
    info = {}

    if fresh:
        names = "، ".join(n for _, n in PAIRS)
        msgs.append(f"✅ بات چندارزی روشن شد: {names}\n"
                    f"هر ارز {START_BALANCE:.0f} دلار مجازی و معامله‌ی جدا دارد. هیچ پول واقعی‌ای درگیر نیست.")

    for pair, name in PAIRS:
        try:
            raw = fetch_candles(pair)
            closed = [c for c in raw if c["t"] + INTERVAL * 60 <= now]
            if len(closed) < MIN_CANDLES:
                raise RuntimeError(f"فقط {len(closed)} کندل بسته موجود است")
            ps = st["pairs"].setdefault(name, new_pair_state())

            if ps["open"]:
                m = check_exit(ps, name, raw, now)
                if m:
                    msgs.append(m)

            last = closed[-1]
            vs, atr = votes(closed)
            buys = sum(1 for _, _, v in vs if v == 1)
            sells = sum(1 for _, _, v in vs if v == -1)
            info[name] = (raw[-1]["c"], buys, sells)

            if ps["last_candle"] != last["t"]:
                ps["last_candle"] = last["t"]
                if not ps["open"]:
                    direction, b, s = decide(vs)
                    if direction:
                        msgs.append(open_trade(ps, name, direction, last["c"], atr, vs, b, s,
                                               last["t"] + INTERVAL * 60))
            if name in st["failed"]:
                st["failed"].remove(name)
        except Exception as e:  # noqa
            print(f"[ERROR] {name}: {e}")
            if name not in st["failed"]:
                st["failed"].append(name)
                msgs.append(f"⚠️ مشکل در دریافت یا تحلیل {name}: {str(e)[:150]}\n"
                            f"این ارز فعلاً رد می‌شود. اگر اسم جفت‌ارز در Kraken غلط است، از لیست PAIRS حذفش کن.")
        time.sleep(1)

    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    if now_utc.hour == DAILY_HOUR_UTC and st["last_daily"] != today:
        st["last_daily"] = today
        lines = []
        for _, name in PAIRS:
            ps = st["pairs"].get(name)
            if not ps or name not in info:
                continue
            price, b, s = info[name]
            n, w, l, total, dd = stats(ps)
            o = ps["open"]
            opn = f"{'Long' if o['dir'] == 'LONG' else 'Short'} باز" if o else "بدون معامله‌ی باز"
            lines.append(f"{name}: {fmt(price)} · نظر روش‌ها {b} خرید/{s} فروش · {opn}\n"
                         f"   {n} معامله ({w} سود، {l} ضرر) · بازده {total:+.2f}%")
        if lines:
            msgs.append("📊 گزارش روزانه\n\n" + "\n".join(lines) +
                        "\n\nهر ارز سرمایه‌ی مجازی جدا دارد.")

    save_state(st)
    send_failed = False
    for m in msgs:
        try:
            send(m)
        except Exception as e:  # noqa
            print(f"[SEND ERROR] {e}")
            send_failed = True
    if send_failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
