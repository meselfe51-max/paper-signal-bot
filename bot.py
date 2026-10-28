"""Multi-coin paper-trading bot with ONE shared wallet (Kraken prices, Telegram alerts)."""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

# ---------------- settings ----------------
PAIRS = [
    ("XBTUSD", "BTC/USD"),
    ("ETHUSD", "ETH/USD"),
    ("SOLUSD", "SOL/USD"),
    ("XRPUSD", "XRP/USD"),
    ("ADAUSD", "ADA/USD"),
]
INTERVAL = 60            # candle size in minutes
MIN_VOTES = 5            # votes to open a trade
MIN_MARGIN = 2
EXIT_VOTES = 4           # opposite votes needed to close early
EXIT_MARGIN = 2
ATR_SL = 1.5             # initial stop distance (ATRs)
ATR_TP = 3.0             # take profit distance (ATRs)
TRAIL_START = 1.5        # trailing starts after this much gain (ATRs)
TRAIL_DIST = 1.0         # trailing stop distance behind best price (ATRs)
RISK = 0.01              # share of wallet risked per trade
MAX_POS = 0.25           # max position size as share of wallet
MIN_TRADE = 20.0         # smallest position worth opening (dollars)
FEE = 0.001              # per side
SLIP = 0.001             # slippage against us on market-style exits
MAX_HOURS = 72           # safety time stop
HALT_DD = 0.15           # stop opening trades after this drawdown from peak
START_BALANCE = 1000.0   # ONE shared virtual wallet for all coins
DAILY_HOUR_UTC = 6
MIN_CANDLES = 210
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
def new_state():
    return {"v": 2, "balance": START_BALANCE, "open": {}, "history": [],
            "pairs": {}, "last_daily": "", "failed": [], "halted": False}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                st = json.load(f)
            if st.get("v") == 2:
                return st, False
        except Exception:
            pass
    return new_state(), True


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def free_cash(st):
    return st["balance"] - sum(t["size"] for t in st["open"].values())


def peak_balance(st):
    return max([START_BALANCE] + [x["balance"] for x in st["history"]])


def stats(h):
    wins = sum(1 for x in h if x["net"] > 0)
    peak, dd = START_BALANCE, 0.0
    for x in h:
        peak = max(peak, x["balance"])
        dd = max(dd, (peak - x["balance"]) / peak * 100)
    return len(h), wins, len(h) - wins, dd


def wallet_text(st):
    n, w, l, dd = stats(st["history"])
    total = (st["balance"] / START_BALANCE - 1) * 100
    if n == 0:
        return (f"کیف پول: {st['balance']:.2f} دلار (آزاد: {free_cash(st):.2f})\n"
                f"هنوز معامله‌ای بسته نشده.")
    return (f"کیف پول: {st['balance']:.2f} دلار (آزاد: {free_cash(st):.2f})\n"
            f"کل: {n} معامله · {w} سود · {l} ضرر\n"
            f"بازده کل: {total:+.2f}% · بیشترین افت: -{dd:.2f}%")


def pair_text(st, name):
    h = [x for x in st["history"] if x.get("pair") == name]
    if not h:
        return f"{name}: هنوز معامله‌ی بسته‌شده ندارد."
    w = sum(1 for x in h if x["net"] > 0)
    pnl = sum(x["net"] for x in h)
    return f"{name}: {len(h)} معامله · {w} سود · {len(h) - w} ضرر · مجموع {pnl:+.2f} دلار"


# ---------------- trade logic ----------------
def open_trade(st, name, direction, entry, atr, vs, buys, sells, ts_close, size):
    sl_d, tp_d = ATR_SL * atr, ATR_TP * atr
    sl = entry - sl_d if direction == "LONG" else entry + sl_d
    tp = entry + tp_d if direction == "LONG" else entry - tp_d
    st["open"][name] = {"dir": direction, "entry": entry, "sl": sl, "sl0": sl, "tp": tp,
                        "size": size, "atr": atr, "best": entry, "checked": 0,
                        "opened": ts_close, "balance_before": st["balance"]}
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
            f"اندازه‌ی معامله: {size:.2f} دلار از کیف پول {st['balance']:.2f} "
            f"(آزاد بعد از این: {free_cash(st):.2f})\n\n"
            f"خروج: حد سود، حد ضرر (متحرک بعد از سود)، یا تغییر نظر روش‌ها، هر کدام زودتر.\n"
            f"این معامله مجازی است. نتیجه بعد از بسته شدن اعلام می‌شود.")


def check_exit(st, name, raw, closed, buys, sells, now):
    t = st["open"][name]
    is_long = t["dir"] == "LONG"
    t.setdefault("best", t["entry"])
    t.setdefault("sl0", t["sl"])
    t.setdefault("atr", abs(t["entry"] - t["sl0"]) / ATR_SL)
    t.setdefault("checked", 0)
    px, why, market = None, None, True

    for c in raw:
        if c["t"] < t["opened"]:
            continue
        done = c["t"] + INTERVAL * 60 <= now
        if done and c["t"] <= t["checked"]:
            continue
        sl_hit = c["l"] <= t["sl"] if is_long else c["h"] >= t["sl"]
        tp_hit = c["h"] >= t["tp"] if is_long else c["l"] <= t["tp"]
        if sl_hit:  # if both hit in one candle, assume the worse case
            px = t["sl"]
            why = ("رسیدن به حد ضرر" if abs(t["sl"] - t["sl0"]) < 1e-12
                   else "حد ضرر متحرک (بخشی از سود قفل شده بود)")
            break
        if tp_hit:
            px, why, market = t["tp"], "رسیدن به حد سود", False
            break
        if done:  # update trailing stop only from fully closed candles
            if is_long:
                t["best"] = max(t["best"], c["h"])
                gain = t["best"] - t["entry"]
            else:
                t["best"] = min(t["best"], c["l"])
                gain = t["entry"] - t["best"]
            if gain >= TRAIL_START * t["atr"]:
                if is_long:
                    t["sl"] = max(t["sl"], t["best"] - TRAIL_DIST * t["atr"])
                else:
                    t["sl"] = min(t["sl"], t["best"] + TRAIL_DIST * t["atr"])
            t["checked"] = c["t"]

    if px is None and closed and closed[-1]["t"] >= t["opened"]:
        own, opp = (buys, sells) if is_long else (sells, buys)
        if opp >= EXIT_VOTES and opp - own >= EXIT_MARGIN:
            px = closed[-1]["c"]
            why = f"تغییر نظر روش‌ها ({opp} شاخص خلاف جهت معامله، {own} موافق)"

    if px is None and now - t["opened"] > MAX_HOURS * 3600:
        px, why = raw[-1]["c"], f"گذشتن {MAX_HOURS} ساعت"

    if px is None:
        return None

    if market:  # market-style exit: slippage against us
        px = px * (1 - SLIP) if is_long else px * (1 + SLIP)

    move = (px / t["entry"] - 1) * (1 if is_long else -1)
    net = t["size"] * move - t["size"] * FEE * 2
    st["balance"] += net
    hours = max(0, (now - t["opened"]) / 3600)
    st["history"].append({"pair": name, "dir": t["dir"], "entry": t["entry"], "exit": px,
                          "size": t["size"], "net": net, "balance": st["balance"],
                          "closed": int(now), "why": why})
    del st["open"][name]
    pct = net / t["balance_before"] * 100
    win = net > 0
    return (f"{'🟢' if win else '🔴'} معامله بسته شد · {name}\n"
            f"دلیل: {why}\n"
            f"مدت: حدود {hours:.0f} ساعت از ورود · اندازه {t['size']:.2f} دلار\n\n"
            f"{'سود' if win else 'ضرر'}: {net:+.2f} دلار ({pct:+.2f}% روی کیف پول)\n"
            f"(بعد از کارمزد و اسلیپیج)\n"
            f"ورود {fmt(t['entry'])} ← خروج {fmt(px)}\n\n"
            f"{wallet_text(st)}\n{pair_text(st, name)}")


def main():
    st, fresh = load_state()
    now = time.time()
    msgs = []
    info = {}
    data = {}

    if fresh:
        names = "، ".join(n for _, n in PAIRS)
        msgs.append(f"✅ بات چندارزی با کیف پول مشترک روشن شد: {names}\n"
                    f"کل سرمایه‌ی مجازی برای همه‌ی ارزها: {START_BALANCE:.0f} دلار.")

    # 1) fetch and analyse every pair
    for pair, name in PAIRS:
        try:
            raw = fetch_candles(pair)
            closed = [c for c in raw if c["t"] + INTERVAL * 60 <= now]
            if len(closed) < MIN_CANDLES:
                raise RuntimeError(f"فقط {len(closed)} کندل بسته موجود است")
            vs, atr = votes(closed)
            buys = sum(1 for _, _, v in vs if v == 1)
            sells = sum(1 for _, _, v in vs if v == -1)
            data[name] = (raw, closed, vs, atr, buys, sells)
            info[name] = (raw[-1]["c"], buys, sells)
            if name in st["failed"]:
                st["failed"].remove(name)
        except Exception as e:  # noqa
            print(f"[ERROR] {name}: {e}")
            if name not in st["failed"]:
                st["failed"].append(name)
                msgs.append(f"⚠️ مشکل در دریافت یا تحلیل {name}: {str(e)[:150]}\n"
                            f"این ارز فعلاً رد می‌شود.")
        time.sleep(1)

    # 2) exits first (they free up cash)
    exited = set()
    for name, (raw, closed, vs, atr, buys, sells) in data.items():
        if name in st["open"]:
            m = check_exit(st, name, raw, closed, buys, sells, now)
            if m:
                msgs.append(m)
                exited.add(name)

    # 3) collect new signals (once per closed candle per coin)
    cands = []
    for name, (raw, closed, vs, atr, buys, sells) in data.items():
        ps = st["pairs"].setdefault(name, {"last_candle": 0})
        last = closed[-1]
        if ps["last_candle"] == last["t"]:
            continue
        ps["last_candle"] = last["t"]
        if name in st["open"] or name in exited:
            continue
        direction, b, s = decide(vs)
        if direction:
            agree, opp = (b, s) if direction == "LONG" else (s, b)
            cands.append((agree - opp, agree, name, direction, last["c"], atr, vs, b, s,
                          last["t"] + INTERVAL * 60))

    # 4) drawdown halt
    halted_now = st["balance"] < peak_balance(st) * (1 - HALT_DD)
    if halted_now and not st.get("halted"):
        msgs.append(f"⛔ کیف پول بیش از {HALT_DD * 100:.0f}% از اوج افت کرده. "
                    f"معامله‌ی جدید باز نمی‌شود تا خودت بررسی کنی. "
                    f"برای ادامه HALT_DD را بالاتر ببر یا state.json را حذف کن.")
    st["halted"] = halted_now

    # 5) open new trades, strongest agreement first, limited by free cash
    if not halted_now:
        for score, agree, name, direction, entry, atr, vs, b, s, ts_close in sorted(
                cands, key=lambda x: (-x[0], -x[1])):
            size = min(st["balance"] * RISK / (ATR_SL * atr / entry),
                       st["balance"] * MAX_POS,
                       free_cash(st))
            if size < MIN_TRADE:
                msgs.append(f"⚪ سیگنال {name} ({'خرید' if direction == 'LONG' else 'فروش'}) "
                            f"رد شد: پول آزاد کیف پول کافی نیست ({free_cash(st):.2f} دلار).")
                continue
            msgs.append(open_trade(st, name, direction, entry, atr, vs, b, s, ts_close, size))

    # 6) daily report
    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    if now_utc.hour == DAILY_HOUR_UTC and st["last_daily"] != today:
        st["last_daily"] = today
        lines = []
        for _, name in PAIRS:
            if name not in info:
                continue
            price, b, s = info[name]
            o = st["open"].get(name)
            opn = f"{'Long' if o['dir'] == 'LONG' else 'Short'} باز ({o['size']:.0f} دلار)" if o else "بدون معامله‌ی باز"
            lines.append(f"{name}: {fmt(price)} · نظر روش‌ها {b} خرید/{s} فروش · {opn}")
        per = "\n".join(pair_text(st, n) for _, n in PAIRS)
        msgs.append("📊 گزارش روزانه\n\n" + "\n".join(lines) +
                    f"\n\n{wallet_text(st)}\n\nسهم هر ارز:\n{per}")

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
