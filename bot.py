"""Multi-coin paper-trading bot v3 - long-only spot, ONE shared wallet.

Kraken prices, Telegram alerts. Built to run from GitHub Actions every 30 minutes.
Entries are evaluated once per closed 4h candle; open trades are managed on 1h candles.
Pure standard library - no pip install needed.
"""
import json
import os
import shutil
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
INTERVAL = 240           # entry candle size (minutes)
MGMT_INTERVAL = 60       # candle size used to manage open trades (minutes)
HTF_INTERVAL = 1440      # daily candles for the big-trend filter
WINDOW = 500             # candles fed to the indicators
MIN_CANDLES = 210
MIN_DAILY = 80

# --- entry rules ---
MIN_GROUPS = 3           # independent signal groups (out of 4) that must agree
ADX_MIN = 20             # trend-strength filter (no trading in choppy ranges)
ATR_PCT_MIN = 0.006      # ATR/price below this: costs eat the move
ATR_PCT_MAX = 0.08       # ATR/price above this: too wild
MAX_EXT = 3.0            # do not chase: max distance above EMA20, in ATRs
RSI_MIN = 48
RSI_MAX = 75
DONCHIAN = 20            # breakout lookback (candles)
MAX_SIGNAL_AGE = 2 * 3600  # skip a signal found more than 2h after candle close
MAX_CHASE = 0.5          # skip if price already ran this many ATRs above signal close

# --- exits ---
ATR_SL = 2.0             # initial stop distance (ATRs)
BE_TRIGGER = 1.5         # after this gain (ATRs) stop moves to break-even (+costs)
TRAIL_START = 2.5        # trailing starts after this gain (ATRs)
TRAIL_DIST = 1.8         # trailing distance behind the best high (ATRs)
ATR_TP = 6.0             # far take-profit cap (ATRs); winners are mostly trailed
MAX_HOURS = 168          # time stop (7 days)
COOLDOWN_H = 24          # no re-entry on a pair for this long after a loss

# --- money management ---
START_BALANCE = 1000.0   # ONE shared virtual wallet
RISK = 0.0075            # share of wallet risked per trade (stop-loss distance)
MAX_POS = 0.30           # max single position as share of wallet
MAX_OPEN = 3             # max simultaneous positions (coins are correlated)
MAX_HEAT = 0.025         # max total open risk as share of wallet
MIN_TRADE = 20.0         # smallest position worth opening (dollars)
FEE = 0.0026             # per side (taker, conservative - check your Kraken tier)
SLIP = 0.0007            # slippage against us on entries and market-style exits
SOFT_DD = 0.08           # risk is halved when drawdown from peak exceeds this
HALT_DD = 0.15           # stop opening trades after this drawdown from peak
HALT_HOURS = 72          # pause length after a halt
REDUCED_DAYS = 7         # reduced risk after a pause ends

DAILY_HOUR_UTC = 6
STATE_FILE = "state.json"
BAK_FILE = STATE_FILE + ".bak"
STATE_VERSION = 3

NAME2PAIR = {name: pair for pair, name in PAIRS}
PERIOD = INTERVAL * 60
MGMT_SEC = MGMT_INTERVAL * 60
API_PAUSE = 0.5          # seconds between Kraken calls


# ---------------- helpers ----------------
def http_json(url, data=None, tries=3):
    last = None
    for i in range(tries):
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
            time.sleep(3 * (i + 1))
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


def split_text(text, limit=3900):
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


def send(text):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        print("[DRY RUN]\n" + text + "\n")
        return
    for part in split_text(text):
        http_json(f"https://api.telegram.org/bot{token}/sendMessage",
                  {"chat_id": chat, "text": part, "disable_web_page_preview": True})


def flush_outbox(st):
    """Send queued messages in order; anything that fails stays queued for the next run."""
    while st["outbox"]:
        try:
            send(st["outbox"][0])
            st["outbox"].pop(0)
        except Exception as e:  # noqa
            print(f"[SEND ERROR] {e}")
            return False
    return True


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
    if len(closes) < n + 2:
        return 50.0
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


def true_ranges(c):
    return [max(c[i]["h"] - c[i]["l"],
                abs(c[i]["h"] - c[i - 1]["c"]),
                abs(c[i]["l"] - c[i - 1]["c"])) for i in range(1, len(c))]


def atr_last(c, n=14):
    trs = true_ranges(c)
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def adx_last(c, n=14):
    """Wilder ADX. Returns (adx, +DI, -DI) for the last candle."""
    if len(c) < 3 * n:
        return 0.0, 0.0, 0.0
    trs = true_ranges(c)
    pdm, mdm = [], []
    for i in range(1, len(c)):
        up = c[i]["h"] - c[i - 1]["h"]
        dn = c[i - 1]["l"] - c[i]["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    a, p, m = sum(trs[:n]), sum(pdm[:n]), sum(mdm[:n])

    def di_dx(a, p, m):
        if a == 0:
            return 0.0, 0.0, 0.0
        pdi, mdi = 100 * p / a, 100 * m / a
        s = pdi + mdi
        return pdi, mdi, (0.0 if s == 0 else 100 * abs(pdi - mdi) / s)

    pdi, mdi, dx = di_dx(a, p, m)
    dxs = [dx]
    for i in range(n, len(trs)):
        a = a - a / n + trs[i]
        p = p - p / n + pdm[i]
        m = m - m / n + mdm[i]
        pdi, mdi, dx = di_dx(a, p, m)
        dxs.append(dx)
    adx = sum(dxs[:n]) / n
    for d in dxs[n:]:
        adx = (adx * (n - 1) + d) / n
    return adx, pdi, mdi


def daily_trend_up(d):
    """Big-picture trend: daily close above a rising EMA 50. None = not enough data."""
    if len(d) < MIN_DAILY:
        return None
    closes = [x["c"] for x in d[-200:]]
    e = ema(closes, 50)
    return bool(closes[-1] > e[-1] and e[-1] > e[-6])


# ---------------- signal ----------------
def analyze(c, d, btc_up):
    """c: closed 4h candles, d: closed daily candles, btc_up: True/False/None.

    Long-only. A signal needs ALL filters to pass plus MIN_GROUPS of 4 independent groups
    (trend, momentum, volume, setup). Same function is used by the bot and by backtest.py.
    """
    c = c[-WINDOW:]
    price = c[-1]["c"]
    res = {"ok": False, "setup": "", "groups": [], "filters": [], "n": 0,
           "adx": 0.0, "atr": 0.0, "rsi": 0.0, "price": price}

    own_up = daily_trend_up(d)
    res["filters"] = [("روند روزانه صعودی", own_up is True), ("BTC صعودی", btc_up is True)]
    if not all(ok for _, ok in res["filters"]):
        return res  # cheap early exit (also keeps backtests fast)

    closes = [x["c"] for x in c]
    atr = atr_last(c)
    e20s, e50s, e200s = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    e20, e50, e200 = e20s[-1], e50s[-1], e200s[-1]
    rsi, rsi_prev = rsi_last(closes), rsi_last(closes[:-1])
    adx, pdi, mdi = adx_last(c)
    res.update({"atr": atr, "adx": adx, "rsi": rsi})

    res["filters"] += [
        (f"قدرت روند (ADX {adx:.0f})", adx >= ADX_MIN and pdi > mdi),
        ("نوسان مناسب", ATR_PCT_MIN <= atr / price <= ATR_PCT_MAX),
        ("قیمت کشیده نشده", (price - e20) / atr <= MAX_EXT),
        (f"RSI بیش‌خرید نیست ({rsi:.0f})", rsi < RSI_MAX),
    ]

    # group 1: trend structure
    g_trend = bool(e20 > e50 and price > e200 and e50 > e50s[-6])

    # group 2: momentum
    e12, e26 = ema(closes, 12), ema(closes, 26)
    macd = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    sig = ema(macd, 9)
    h_now, h_prev = macd[-1] - sig[-1], macd[-2] - sig[-2]
    g_mom = bool(h_now > 0 and h_now > h_prev and rsi >= RSI_MIN)

    # group 3: volume / money flow
    obv = [0.0]
    for i in range(1, len(c)):
        dv = c[i]["v"] if c[i]["c"] > c[i - 1]["c"] else -c[i]["v"] if c[i]["c"] < c[i - 1]["c"] else 0
        obv.append(obv[-1] + dv)
    obv_e = ema(obv, 20)
    w = c[-20:]
    vol = sum(x["v"] for x in w)
    vwap = sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in w) / vol if vol else price
    g_vol = bool(obv[-1] > obv_e[-1] and price > vwap)

    # group 4: setup (fresh breakout with volume OR pullback bounce in an uptrend)
    prev = c[-DONCHIAN - 1:-1]
    avg_v = sum(x["v"] for x in c[-21:-1]) / 20
    breakout = price > max(x["h"] for x in prev) and c[-1]["v"] >= avg_v
    pullback = (min(x["l"] for x in c[-3:]) <= e20 + 0.3 * atr and price > e20
                and price > c[-1]["o"] and rsi > rsi_prev)
    g_setup = bool(breakout or pullback)
    res["setup"] = "شکست سقف با حجم" if breakout else "پولبک به EMA20" if pullback else ""

    res["groups"] = [
        ("روند", "ساختار EMA 20/50/200", g_trend),
        ("مومنتوم", "MACD و RSI", g_mom),
        ("حجم", "OBV و VWAP", g_vol),
        ("ستاپ", "شکست یا پولبک", g_setup),
    ]
    res["n"] = sum(1 for _, _, ok in res["groups"] if ok)
    res["ok"] = all(ok for _, ok in res["filters"]) and res["n"] >= MIN_GROUPS
    return res


# ---------------- state ----------------
def new_state():
    return {"v": STATE_VERSION, "balance": START_BALANCE, "peak": START_BALANCE, "open": {},
            "history": [], "pairs": {}, "cooldown": {}, "last_daily": "", "failed": [],
            "halted": False, "halt_until": 0, "reduced_until": 0, "outbox": []}


def _read(path):
    with open(path) as f:
        return json.load(f)


def load_state():
    """Returns (state, status). status: ok | fresh | restored.

    Never silently resets a wallet: a corrupt state.json is restored from the backup,
    and if that fails too the bot stops with an error instead of starting from zero.
    """
    if os.path.exists(STATE_FILE):
        try:
            st = _read(STATE_FILE)
            if st.get("v") == STATE_VERSION:
                shutil.copyfile(STATE_FILE, BAK_FILE)  # backup of the last good state
                return st, "ok"
            os.replace(STATE_FILE, f"state_old_v{st.get('v', 'x')}.json")  # old format: archive it
            return new_state(), "fresh"
        except Exception as e:  # noqa
            print(f"[STATE ERROR] {e}")
            if os.path.exists(BAK_FILE):
                try:
                    st = _read(BAK_FILE)
                    if st.get("v") == STATE_VERSION:
                        return st, "restored"
                except Exception as e2:  # noqa
                    print(f"[BACKUP ERROR] {e2}")
            raise RuntimeError("state.json is corrupt and no valid backup exists; "
                               "fix or delete it manually (the bot will not reset the wallet by itself)")
    return new_state(), "fresh"


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)  # atomic: never leaves a half-written file


def free_cash(st):
    return st["balance"] - sum(t["size"] for t in st["open"].values())


def stats(h):
    n = len(h)
    wins = [x for x in h if x["net"] > 0]
    gw = sum(x["net"] for x in wins)
    gl = -sum(x["net"] for x in h if x["net"] <= 0)
    pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)
    avg_r = sum(x.get("r", 0.0) for x in h) / n if n else 0.0
    peak, dd = START_BALANCE, 0.0
    for x in h:
        peak = max(peak, x["balance"])
        dd = max(dd, (peak - x["balance"]) / peak * 100)
    return n, len(wins), n - len(wins), dd, pf, avg_r


def wallet_text(st):
    n, w, l, dd, pf, avg_r = stats(st["history"])
    total = (st["balance"] / START_BALANCE - 1) * 100
    head = f"کیف پول: {st['balance']:.2f} دلار (آزاد: {free_cash(st):.2f})"
    if n == 0:
        return head + "\nهنوز معامله‌ای بسته نشده."
    pf_txt = "∞" if pf == float("inf") else f"{pf:.2f}"
    return (f"{head}\n"
            f"کل: {n} معامله · {w} سود · {l} ضرر\n"
            f"بازده کل: {total:+.2f}% · بیشترین افت: -{dd:.2f}%\n"
            f"ضریب سود (PF): {pf_txt} · میانگین نتیجه: {avg_r:+.2f}R")


def pair_text(st, name):
    h = [x for x in st["history"] if x.get("pair") == name]
    if not h:
        return f"{name}: هنوز معامله‌ی بسته‌شده ندارد."
    w = sum(1 for x in h if x["net"] > 0)
    pnl = sum(x["net"] for x in h)
    return f"{name}: {len(h)} معامله · {w} سود · {len(h) - w} ضرر · مجموع {pnl:+.2f} دلار"


# ---------------- risk / sizing ----------------
def risk_per_dollar(entry, sl):
    """Loss per dollar of position if the stop is hit, including fees and exit slippage."""
    return (entry - sl) / entry + 2 * FEE + SLIP


def open_risk(st):
    return sum(max(0.0, risk_per_dollar(t["entry"], t["sl"])) * t["size"] for t in st["open"].values())


def update_halt(st, now):
    """Drawdown guard. Returns (messages, risk_multiplier)."""
    msgs = []
    st["peak"] = max(st.get("peak", START_BALANCE), st["balance"])
    if st.get("halted") and now >= st.get("halt_until", 0):
        st["halted"] = False
        st["peak"] = st["balance"]  # new reference so the guard does not trigger in a loop
        st["reduced_until"] = now + REDUCED_DAYS * 86400
        msgs.append(f"▶️ توقف معامله‌ی جدید تمام شد. {REDUCED_DAYS} روز آینده با نصف ریسک ادامه می‌دهد.")
    if not st.get("halted") and st["balance"] < st["peak"] * (1 - HALT_DD):
        st["halted"] = True
        st["halt_until"] = now + HALT_HOURS * 3600
        msgs.append(f"⛔ کیف پول بیش از {HALT_DD * 100:.0f}% از اوج افت کرد. "
                    f"معامله‌ی جدید {HALT_HOURS} ساعت باز نمی‌شود (معامله‌های باز مدیریت می‌شوند). "
                    f"این بهترین زمان برای بررسی دلیل افت است.")
    dd = 1 - st["balance"] / st["peak"]
    mult = 0.5 if (dd >= SOFT_DD or now < st.get("reduced_until", 0)) else 1.0
    return msgs, mult


def open_position(st, name, entry, atr, ts, setup, risk_mult):
    """Sizes and registers a LONG. Returns (trade, "") or (None, reason)."""
    if len(st["open"]) >= MAX_OPEN:
        return None, f"سقف {MAX_OPEN} معامله‌ی هم‌زمان پر است"
    sl = entry - ATR_SL * atr
    tp = entry + ATR_TP * atr
    rpd = risk_per_dollar(entry, sl)
    bal = st["balance"]
    heat_room = max(0.0, bal * MAX_HEAT - open_risk(st))
    size = min(bal * RISK * risk_mult / rpd, bal * MAX_POS, free_cash(st), heat_room / rpd)
    if size < MIN_TRADE:
        return None, (f"پول آزاد یا سقف ریسک کل کافی نیست "
                      f"(آزاد {free_cash(st):.2f} دلار، ریسک باز {open_risk(st):.2f} دلار)")
    t = {"dir": "LONG", "entry": entry, "sl": sl, "sl0": sl, "tp": tp, "size": size, "atr": atr,
         "best": entry, "checked": 0, "opened": ts, "balance_before": bal,
         "risk0": size * rpd, "setup": setup}
    st["open"][name] = t
    return t, ""


def step_trade(t, hi, lo, done):
    """Advance a LONG by one candle. Returns (price, reason, is_market_exit) or None.

    Stops are checked before targets (worst case). Trailing/break-even only move from
    fully closed candles, so there is no look-ahead.
    """
    if lo <= t["sl"]:
        moved = t["sl"] > t["sl0"] + 1e-12
        return t["sl"], ("حد ضرر متحرک (سود یا سر به سر قفل شده بود)" if moved else "رسیدن به حد ضرر"), True
    if hi >= t["tp"]:
        return t["tp"], "رسیدن به حد سود", False
    if done:
        t["best"] = max(t["best"], hi)
        gain = t["best"] - t["entry"]
        if gain >= BE_TRIGGER * t["atr"]:
            t["sl"] = max(t["sl"], t["entry"] * (1 + 2 * (FEE + SLIP)))
        if gain >= TRAIL_START * t["atr"]:
            t["sl"] = max(t["sl"], t["best"] - TRAIL_DIST * t["atr"])
    return None


def close_trade(st, name, px, why, now):
    t = st["open"][name]
    move = px / t["entry"] - 1
    net = t["size"] * move - t["size"] * FEE * 2
    st["balance"] += net
    rec = {"pair": name, "dir": t["dir"], "entry": t["entry"], "exit": px, "size": t["size"],
           "net": net, "r": (net / t["risk0"]) if t["risk0"] > 0 else 0.0,
           "balance": st["balance"], "opened": int(t["opened"]), "closed": int(now), "why": why,
           "pct": net / t["balance_before"] * 100}
    st["history"].append(rec)
    del st["open"][name]
    if net < 0:
        st["cooldown"][name] = now + COOLDOWN_H * 3600
    return rec


# ---------------- messages ----------------
def open_msg(st, name, t, res, risk_mult):
    mark = {True: "✅", False: "➖"}
    glines = "\n".join(f"{mark[ok]} {label}" for _, label, ok in res["groups"])
    half = "\n(ریسک نصف است چون کیف پول از اوج افت کرده)" if risk_mult < 1 else ""
    return (f"🟢 سیگنال خرید (Long) · {name}\n"
            f"تایم‌فریم {INTERVAL // 60} ساعته · الگو: {res['setup']}\n\n"
            f"اجماع: {res['n']} از ۴ گروه مستقل\n{glines}\n"
            f"فیلترها (همه قبول): روند روزانه، BTC، ADX {res['adx']:.0f}، نوسان، RSI {res['rsi']:.0f}\n\n"
            f"قیمت ورود: {fmt(t['entry'])}\n"
            f"حد ضرر: {fmt(t['sl'])} ({(t['sl'] / t['entry'] - 1) * 100:+.2f}%)\n"
            f"حد سود: {fmt(t['tp'])} ({(t['tp'] / t['entry'] - 1) * 100:+.2f}%)\n"
            f"اندازه: {t['size']:.2f} دلار · ریسک: {t['risk0']:.2f} دلار "
            f"({t['risk0'] / st['balance'] * 100:.2f}% کیف پول){half}\n"
            f"باز: {len(st['open'])} از {MAX_OPEN} · آزاد بعد از این: {free_cash(st):.2f}\n\n"
            f"مدیریت: بعد از {BE_TRIGGER:g} ATR سود استاپ می‌رود روی سر به سر؛ "
            f"بعد از {TRAIL_START:g} ATR تریلینگ شروع می‌شود. هر جابه‌جایی استاپ اینجا اعلام می‌شود.\n"
            f"این معامله مجازی است. نتیجه بعد از بسته شدن اعلام می‌شود.")


def close_msg(st, name, rec):
    win = rec["net"] > 0
    hours = max(0, (rec["closed"] - rec["opened"]) / 3600)
    return (f"{'🟢' if win else '🔴'} معامله بسته شد · {name}\n"
            f"دلیل: {rec['why']}\n"
            f"مدت: حدود {hours:.0f} ساعت · اندازه {rec['size']:.2f} دلار\n\n"
            f"{'سود' if win else 'ضرر'}: {rec['net']:+.2f} دلار "
            f"({rec['pct']:+.2f}% روی کیف پول · {rec['r']:+.2f}R)\n"
            f"(بعد از کارمزد و اسلیپیج)\n"
            f"ورود {fmt(rec['entry'])} ← خروج {fmt(rec['exit'])}\n\n"
            f"{wallet_text(st)}\n{pair_text(st, name)}")


def trail_msg(name, t, old_sl, price):
    locked = (t["sl"] / t["entry"] - 1) * 100
    return (f"🔔 استاپ {name} بالا رفت\n"
            f"{fmt(old_sl)} ← {fmt(t['sl'])} ({locked:+.2f}% نسبت به ورود)\n"
            f"قیمت فعلی: {fmt(price)}\n"
            f"اگر هم‌زمان با بات معامله می‌کنی، استاپ خودت را همین بگذار.")


def status_text(res):
    if res["ok"]:
        return f"سیگنال فعال ({res['n']}/4)"
    failed = [label for label, ok in res["filters"] if not ok]
    if failed:
        return "بدون سیگنال · رد شده در: " + "، ".join(failed[:2])
    return f"بدون سیگنال ({res['n']}/4 گروه)"


# ---------------- live management of an open trade ----------------
def manage_live(st, name, raw_m, now):
    """Returns ("exit"|"trail", text) or None."""
    t = st["open"][name]
    old_sl = t["sl"]
    bucket = int(t["opened"] // MGMT_SEC) * MGMT_SEC  # 1h candle that contains the entry
    hit = None
    for c in raw_m:
        if c["t"] < bucket:
            continue
        done = c["t"] + MGMT_SEC <= now
        if done and c["t"] <= t["checked"]:
            continue
        if c["t"] == bucket:
            hi = lo = c["c"]  # price moves before our entry must not count
        else:
            hi, lo = c["h"], c["l"]
        hit = step_trade(t, hi, lo, done)
        if hit:
            break
        if done:
            t["checked"] = c["t"]
    if hit is None and now - t["opened"] > MAX_HOURS * 3600:
        hit = (raw_m[-1]["c"], f"گذشتن {MAX_HOURS} ساعت", True)
    if hit:
        px, why, market = hit
        if market:
            px *= 1 - SLIP
        rec = close_trade(st, name, px, why, now)
        return "exit", close_msg(st, name, rec)
    if t["sl"] > old_sl + 1e-12:
        return "trail", trail_msg(name, t, old_sl, raw_m[-1]["c"])
    return None


# ---------------- Telegram commands ----------------
HELP_TEXT = ("دستورها:\n"
             "/status معاملات باز با قیمت لحظه‌ای\n"
             "/stats وضعیت کیف پول و آمار هر ارز\n"
             "/help همین راهنما\n\n"
             "جواب‌ها با اجرای بعدی بات می‌آیند (معمولاً چند دقیقه). همه‌ی معاملات مجازی‌اند.")


def status_reply(st, prices, now):
    if not st["open"]:
        return "الان هیچ معامله‌ی بازی نیست.\n\n" + wallet_text(st)
    lines = []
    for name, t in st["open"].items():
        hours = max(0, (now - t["opened"]) / 3600)
        line = (f"{name} Long از {fmt(t['entry'])} · استاپ {fmt(t['sl'])} · حد سود {fmt(t['tp'])} · "
                f"{t['size']:.0f} دلار · {hours:.0f} ساعت پیش")
        px = prices.get(name)
        if px:
            pnl = t["size"] * (px / t["entry"] - 1)
            line += f"\n   الان {fmt(px)} ({pnl:+.2f} دلار شناور، {(px / t['entry'] - 1) * 100:+.2f}%)"
        lines.append(line)
    flag = "\n⛔ معامله‌ی جدید فعلاً متوقف است." if st.get("halted") else ""
    return (f"معاملات باز: {len(lines)} از {MAX_OPEN}\n" + "\n".join(lines) +
            f"\nریسک باز: {open_risk(st):.2f} دلار\n\n{wallet_text(st)}{flag}")


def stats_reply(st):
    per = "\n".join(pair_text(st, n) for _, n in PAIRS)
    flag = "\n⛔ معامله‌ی جدید فعلاً متوقف است." if st.get("halted") else ""
    return f"{wallet_text(st)}{flag}\n\nسهم هر ارز:\n{per}"


def poll_commands(st, prices, now):
    """Reads new Telegram messages from YOUR chat only and returns reply texts."""
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        return []
    try:
        j = http_json(f"https://api.telegram.org/bot{token}/getUpdates?offset={st.get('tg_offset', 0)}&timeout=0",
                      tries=1)
    except Exception as e:  # noqa  (for example: a webhook is active)
        print(f"[UPDATES ERROR] {e}")
        return []
    out = []
    for u in j.get("result", []):
        st["tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        text = (m.get("text") or "").strip()
        if str((m.get("chat") or {}).get("id")) != chat or not text.startswith("/"):
            continue
        if len(out) >= 5:
            continue
        cmd = text.split()[0].lower().split("@")[0]
        if cmd in ("/start", "/help"):
            out.append("✅ وصل است.\n\n" + HELP_TEXT)
        elif cmd == "/status":
            out.append(status_reply(st, prices, now))
        elif cmd == "/stats":
            out.append(stats_reply(st))
        else:
            out.append("دستور ناشناخته. /help را بفرست.")
    return out


# ---------------- one market cycle ----------------
def signals_pending(st, now):
    """True when a freshly closed 4h candle still has to be evaluated (and is not too old)."""
    boundary = int(now // PERIOD) * PERIOD
    if now - boundary > MAX_SIGNAL_AGE:
        return False
    return any(st["pairs"].get(n, {}).get("last_candle", 0) != boundary - PERIOD for _, n in PAIRS)


def market_cycle(st, now, msgs, report_due, today):
    """Fetch data, manage open trades, look for signals, open trades, daily report."""
    # 1) 4h candles for every pair
    data = {}
    for pair, name in PAIRS:
        try:
            raw = fetch_candles(pair, INTERVAL)
            closed = [c for c in raw if c["t"] + PERIOD <= now]
            if len(closed) < MIN_CANDLES:
                raise RuntimeError(f"فقط {len(closed)} کندل بسته موجود است")
            data[name] = {"closed": closed, "price": raw[-1]["c"]}
            if name in st["failed"]:
                st["failed"].remove(name)
        except Exception as e:  # noqa
            print(f"[ERROR] {name}: {e}")
            if name not in st["failed"]:
                st["failed"].append(name)
                msgs.append(f"⚠️ مشکل در دریافت یا تحلیل {name}: {str(e)[:150]}\n"
                            f"این ارز فعلاً رد می‌شود.")
        time.sleep(API_PAUSE)

    # 2) manage open trades first (exits free up cash)
    prices = {n: d["price"] for n, d in data.items()}
    exited = set()
    for name in list(st["open"]):
        try:
            raw_m = fetch_candles(NAME2PAIR[name], MGMT_INTERVAL)
        except Exception as e:  # noqa
            print(f"[ERROR] manage {name}: {e}")  # candles keep their highs/lows: retried next run
            continue
        time.sleep(API_PAUSE)
        prices[name] = raw_m[-1]["c"]
        out = manage_live(st, name, raw_m, now)
        if out:
            msgs.append(out[1])
            if out[0] == "exit":
                exited.add(name)

    # 3) new signals (once per closed 4h candle per coin)
    daily_cache = {}

    def get_daily(pair):
        if pair not in daily_cache:
            raw = fetch_candles(pair, HTF_INTERVAL)
            daily_cache[pair] = [c for c in raw if c["t"] + HTF_INTERVAL * 60 <= now]
            time.sleep(API_PAUSE)
        return daily_cache[pair]

    btc_cache = {}

    def btc_up():
        if "v" not in btc_cache:
            try:
                btc_cache["v"] = daily_trend_up(get_daily(NAME2PAIR["BTC/USD"]))
            except Exception as e:  # noqa
                print(f"[ERROR] BTC daily: {e}")
                btc_cache["v"] = None
        return btc_cache["v"]

    cands, analyses, gaps = [], {}, []
    for pair, name in PAIRS:
        if name not in data:
            continue
        closed = data[name]["closed"]
        last = closed[-1]
        ps = st["pairs"].setdefault(name, {"last_candle": 0})
        new_candle = ps["last_candle"] != last["t"]
        if not new_candle and not report_due:
            continue
        try:
            b = btc_up()
            if b is None:
                raise RuntimeError("وضعیت BTC نامشخص است")
            res = analyze(closed, get_daily(pair), b)
        except Exception as e:  # noqa
            print(f"[ERROR] analyze {name}: {e}")  # candle not marked as seen: retried next run
            continue
        analyses[name] = res
        if not new_candle:
            continue
        if ps["last_candle"]:
            missed = (last["t"] - ps["last_candle"]) // PERIOD - 1
            if missed >= 1:
                gaps.append(f"{name}: {missed}")
        ps["last_candle"] = last["t"]
        if name in st["open"] or name in exited or not res["ok"]:
            continue
        if st["cooldown"].get(name, 0) > now:
            continue
        age = now - (last["t"] + PERIOD)
        if age > MAX_SIGNAL_AGE:
            msgs.append(f"⚪ سیگنال خرید {name} رد شد: {age / 60:.0f} دقیقه بعد از بسته شدن کندل پیدا شد (دیر).")
            continue
        if data[name]["price"] - last["c"] > MAX_CHASE * res["atr"]:
            msgs.append(f"⚪ سیگنال خرید {name} رد شد: قیمت از نقطه‌ی سیگنال ({fmt(last['c'])}) "
                        f"بیش از {MAX_CHASE:g} ATR بالاتر رفته ({fmt(data[name]['price'])}).")
            continue
        cands.append((res["n"], res["adx"], name, res))

    if gaps:
        msgs.append(f"⚠️ این تعداد کندل {INTERVAL // 60} ساعته بررسی نشده بود "
                    f"(تأخیر یا توقف GitHub Actions): " + "، ".join(gaps))

    # 4) drawdown guard
    hm, risk_mult = update_halt(st, now)
    msgs += hm

    # 5) open new trades: strongest first, limited by cash, count and total risk
    for _, _, name, res in sorted(cands, key=lambda x: (-x[0], -x[1])):
        if st["halted"]:
            msgs.append(f"⚪ سیگنال خرید {name} رد شد: معامله‌ی جدید به خاطر افت کیف پول متوقف است.")
            continue
        entry = data[name]["price"] * (1 + SLIP)
        t, why = open_position(st, name, entry, res["atr"], now, res["setup"], risk_mult)
        if t is None:
            msgs.append(f"⚪ سیگنال خرید {name} رد شد: {why}.")
            continue
        msgs.append(open_msg(st, name, t, res, risk_mult))

    # 6) daily report
    if report_due:
        st["last_daily"] = today
        b = btc_up()
        btc_txt = "صعودی ✅" if b else "غیرصعودی ❌" if b is False else "نامشخص"
        lines = [f"وضعیت BTC (روزانه): {btc_txt}", ""]
        for _, name in PAIRS:
            if name not in prices:
                continue
            price = prices[name]
            o = st["open"].get(name)
            if o:
                pnl = o["size"] * (price / o["entry"] - 1)
                opn = f"Long باز ({o['size']:.0f} دلار، {pnl:+.2f} دلار شناور، استاپ {fmt(o['sl'])})"
            elif name in analyses:
                opn = status_text(analyses[name])
            else:
                opn = "بدون معامله‌ی باز"
            lines.append(f"{name}: {fmt(price)} · {opn}")
        per = "\n".join(pair_text(st, n) for _, n in PAIRS)
        flag = "\n⛔ معامله‌ی جدید فعلاً متوقف است." if st["halted"] else ""
        msgs.append("📊 گزارش روزانه\n\n" + "\n".join(lines) +
                    f"\n\n{wallet_text(st)}{flag}\n\nسهم هر ارز:\n{per}")
    return prices


# ---------------- main ----------------
def main():
    st, status = load_state()
    now = time.time()
    msgs = []
    if status == "fresh":
        names = "، ".join(n for _, n in PAIRS)
        msgs.append(f"✅ بات نسخه‌ی ۳ روشن شد (فقط خرید/اسپات، کیف پول مشترک): {names}\n"
                    f"سرمایه‌ی مجازی: {START_BALANCE:.0f} دلار · کندل {INTERVAL // 60} ساعته\n"
                    f"ریسک هر معامله {RISK * 100:.2f}% · حداکثر {MAX_OPEN} معامله‌ی هم‌زمان.\n"
                    f"برای راهنما /help را بفرست.")
    elif status == "restored":
        msgs.append("⚠️ state.json خراب بود و از نسخه‌ی پشتیبان بازیابی شد. "
                    "ممکن است آخرین تغییرات از دست رفته باشد.")

    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    report_due = now_utc.hour == DAILY_HOUR_UTC and st["last_daily"] != today

    # Fast path: most 5-minute runs have nothing to do and finish in a few seconds.
    prices = {}
    if st["open"] or report_due or st["failed"] or signals_pending(st, now):
        prices = market_cycle(st, now, msgs, report_due, today)
    else:
        print("[IDLE] nothing to evaluate in this run")

    # Telegram commands (/status, /stats, /help)
    msgs += poll_commands(st, prices, now)

    # Save first, then send (undelivered messages stay queued in the state file)
    st["outbox"].extend(msgs)
    st["outbox"] = st["outbox"][-40:]
    save_state(st)
    ok = flush_outbox(st)
    save_state(st)
    if not ok:
        raise SystemExit(1)  # makes the GitHub run red so you notice Telegram problems


if __name__ == "__main__":
    main()
