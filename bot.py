"""Multi-coin paper-trading bot v4 - long-only spot, ONE shared wallet.

Trend following: daily regime filter + 4h Donchian breakout + ATR chandelier trailing stop.
Kraken prices, Telegram alerts. Runs from GitHub Actions (triggered every minute).
Pure standard library - no pip install needed.
"""
import json
import os
import shutil
import time
import urllib.request
from datetime import datetime, timezone
from adaptive_strategy import analyze as analyze_strategy

# ---------------- settings ----------------
PAIRS = [
    ("XBTUSD", "BTC/USD"),
    ("ETHUSD", "ETH/USD"),
    ("SOLUSD", "SOL/USD"),
    ("XRPUSD", "XRP/USD"),
    ("ADAUSD", "ADA/USD"),
    ("LINKUSD", "LINK/USD"),
    ("AVAXUSD", "AVAX/USD"),
    ("LTCUSD", "LTC/USD"),
    ("DOTUSD", "DOT/USD"),
    ("ATOMUSD", "ATOM/USD"),
    ("PAXGUSD", "PAXG/USD"),   # gold token
]
NO_BTC_FILTER = {"PAXG/USD"}   # gold does not follow the crypto regime (set() to disable)
USD_TOMAN = 0            # dollar price in toman, typed by hand. 0 = off; if set, /plan also shows toman
INTERVAL = 240           # entry candle size (minutes)
MGMT_INTERVAL = 60       # candle size used to manage open trades (minutes)
HTF_INTERVAL = 1440      # daily candles for the regime filter
WINDOW = 500             # candles fed to the indicators
MIN_CANDLES = 210
MIN_DAILY = 80

# --- entry rules ---
DONCHIAN = 55            # breakout lookback (4h candles, about 9 days)
RANK_DAYS = 20           # relative-strength lookback (daily candles) used to rank signals
ATR_PCT_MAX = 0.08       # ATR/price above this: too wild
MAX_EXT = 3.0            # do not chase: max distance above EMA20, in ATRs
MAX_SIGNAL_AGE = 2 * 3600  # skip a signal found more than 2h after candle close
MAX_CHASE = 0.5          # skip if price already ran this many ATRs above signal close

# --- exits ---
ATR_SL = 3.0             # initial stop AND trailing distance behind the best high (ATRs)
MAX_HOURS = 720          # safety time stop (30 days); winners are normally ended by the trailing stop
COOLDOWN_H = 24          # no re-entry on a pair for this long after a real loss
COOLDOWN_R = -0.3        # a loss counts as "real" only below this R

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
HARD_DD = 0.30           # drawdown from the all-time peak: halts until you reset it by hand

# --- cost filter: costs may take at most this share of the stop distance ---
ROUND_TRIP = 2 * FEE + 2 * SLIP
MAX_COST_SHARE = 0.25
ATR_PCT_MIN = ROUND_TRIP / (ATR_SL * MAX_COST_SHARE)

DAILY_HOUR_UTC = 6
STATE_FILE = "state.json"
BAK_FILE = STATE_FILE + ".bak"
STATE_VERSION = 3
STATE_KIND = "shared-wallet-long"   # identifies THIS bot's state file (kept so the wallet continues)
REQUIRED_KEYS = ("balance", "open", "history", "pairs")
OUTBOX_MAX = 200

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


def daily_trend_up(d):
    """Big-picture trend: daily close above a rising EMA 50. None = not enough data."""
    if len(d) < MIN_DAILY:
        return None
    closes = [x["c"] for x in d[-200:]]
    e = ema(closes, 50)
    return bool(closes[-1] > e[-1] and e[-1] > e[-6])


# ---------------- signal ----------------
def analyze(c, d, btc_up, name=""):
    """Score-based multi-factor strategy; legacy signature retained for backtest compatibility."""
    return analyze_strategy(c, d, btc_up, name)


# ---------------- state ----------------
def new_state():
    return {"v": STATE_VERSION, "kind": STATE_KIND, "balance": START_BALANCE, "peak": START_BALANCE,
            "peak_all": START_BALANCE, "open": {},
            "history": [], "pairs": {}, "cooldown": {}, "last_daily": "", "failed": [],
            "halted": False, "halt_until": 0, "reduced_until": 0, "outbox": [], "btc_warn": False,
            "diagnostics": {}}


def _read(path):
    with open(path) as f:
        return json.load(f)


def valid_state(st):
    return (isinstance(st, dict) and st.get("v") == STATE_VERSION and st.get("kind") == STATE_KIND
            and all(k in st for k in REQUIRED_KEYS))


def fill_defaults(st):
    for k, v in new_state().items():
        st.setdefault(k, v)
    if not isinstance(st.get("diagnostics"), dict):
        st["diagnostics"] = {}
    if st.get("peak_all", 0) < st["peak"]:
        st["peak_all"] = st["peak"]
    return st


def load_state():
    """Returns (state, status). status: ok | fresh | restored.

    Never silently deletes or resets a wallet: a state file from another bot/version is archived
    (state_old_*.json), a corrupt one is restored from the backup, and if that fails too the bot
    stops with an error instead of starting from zero.
    """
    if not os.path.exists(STATE_FILE):
        return new_state(), "fresh"
    try:
        st = _read(STATE_FILE)
    except Exception as e:  # noqa
        print(f"[STATE ERROR] {e}")
        if os.path.exists(BAK_FILE):
            try:
                bak = _read(BAK_FILE)
                if valid_state(bak):
                    return fill_defaults(bak), "restored"
            except Exception as e2:  # noqa
                print(f"[BACKUP ERROR] {e2}")
        raise RuntimeError("state.json is corrupt and no valid backup exists; "
                           "fix or delete it manually (the bot will not reset the wallet by itself)")
    if valid_state(st):
        shutil.copyfile(STATE_FILE, BAK_FILE)  # backup of the last good state
        return fill_defaults(st), "ok"
    v = st.get("v", "x") if isinstance(st, dict) else "x"
    os.replace(STATE_FILE, f"state_old_v{v}_{int(time.time())}.json")  # old/foreign format: archive it
    print("[STATE] state.json is not from this bot version; archived it and starting fresh")
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
    st["peak_all"] = max(st.get("peak_all", START_BALANCE), st["balance"])
    if st.get("halted") and now >= st.get("halt_until", 0):
        st["halted"] = False
        st["peak"] = st["balance"]  # new reference so the soft guard does not trigger in a loop
        st["reduced_until"] = now + REDUCED_DAYS * 86400
        msgs.append(f"▶️ توقف معامله‌ی جدید تمام شد. {REDUCED_DAYS} روز آینده با نصف ریسک ادامه می‌دهد.")
    if not st.get("halted") and st["balance"] < st["peak_all"] * (1 - HARD_DD):
        st["halted"] = True
        st["halt_until"] = now + 10 * 365 * 86400   # effectively until you reset it by hand
        msgs.append(f"🛑 کیف پول بیش از {HARD_DD * 100:.0f}% از اوج واقعی خود افت کرد. "
                    f"معامله‌ی جدید تا بررسی دستی متوقف شد.\n"
                    f"برای ادامه: در state.json مقدار halted را false و peak_all و peak را برابر balance بگذار.")
    elif not st.get("halted") and st["balance"] < st["peak"] * (1 - HALT_DD):
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
    rpd = risk_per_dollar(entry, sl)
    bal = st["balance"]
    heat_room = max(0.0, bal * MAX_HEAT - open_risk(st))
    size = min(bal * RISK * risk_mult / rpd, bal * MAX_POS, free_cash(st), heat_room / rpd)
    if size < MIN_TRADE:
        return None, (f"پول آزاد یا سقف ریسک کل کافی نیست "
                      f"(آزاد {free_cash(st):.2f} دلار، ریسک باز {open_risk(st):.2f} دلار)")
    t = {"dir": "LONG", "entry": entry, "sl": sl, "sl0": sl, "tp": None, "size": size, "atr": atr,
         "best": entry, "checked": 0, "opened": ts, "balance_before": bal,
         "risk0": size * rpd, "setup": setup, "notified_sl": sl}
    st["open"][name] = t
    return t, ""


def step_trade(t, hi, lo, done, op=None):
    """Advance a LONG by one candle. Returns (price, reason, is_market_exit) or None.

    Stops are checked before targets (worst case). If the candle opens below the stop
    (a gap), the exit fills at the open. The trailing stop (chandelier) only moves from
    fully closed candles and only upward, so there is no look-ahead.
    """
    if lo <= t["sl"]:
        fill = t["sl"] if (op is None or op >= t["sl"]) else op
        moved = t["sl"] > t["sl0"] + 1e-12
        return fill, ("استاپ متحرک (سود یا ضرر کمتر قفل شده بود)" if moved else "رسیدن به حد ضرر"), True
    tp = t.get("tp")
    if tp and hi >= tp:   # old trades opened by earlier versions may still have a target
        return tp, "رسیدن به حد سود", False
    if done:
        t["best"] = max(t["best"], hi)
        t["sl"] = max(t["sl"], t["best"] - ATR_SL * t["atr"])
    return None


def close_trade(st, name, px, why, now):
    t = st["open"][name]
    move = px / t["entry"] - 1
    fees = t["size"] * FEE + t["size"] * (1 + move) * FEE   # entry fee + fee on the exit value
    net = t["size"] * move - fees
    st["balance"] += net
    rec = {"pair": name, "dir": t["dir"], "entry": t["entry"], "exit": px, "size": t["size"],
           "net": net, "r": (net / t["risk0"]) if t["risk0"] > 0 else 0.0,
           "balance": st["balance"], "opened": int(t["opened"]), "closed": int(now), "why": why,
           "pct": net / t["balance_before"] * 100}
    st["history"].append(rec)
    del st["open"][name]
    if rec["r"] < COOLDOWN_R:
        st["cooldown"][name] = now + COOLDOWN_H * 3600
    return rec


# ---------------- messages ----------------
def open_msg(st, name, t, res, risk_mult):
    half = "\n(ریسک نصف است چون کیف پول از اوج افت کرده)" if risk_mult < 1 else ""
    return (f"🟢 سیگنال خرید (Long) · {name}\n"
            f"تایم‌فریم {INTERVAL // 60} ساعته · الگو: {res['setup']}\n"
            f"امتیاز کیفیت: {res.get('score', 0):.1f} · رژیم بازار: {res.get('regime', 'نامشخص')}\n"
            f"عوامل تأییدشده: {'، '.join(label for label, ok in res['filters'] if ok)}\n\n"
            f"قیمت ورود: {fmt(t['entry'])}\n"
            f"حد ضرر اولیه: {fmt(t['sl'])} ({(t['sl'] / t['entry'] - 1) * 100:+.2f}%)\n"
            f"حد سود: ندارد؛ خروج با استاپ متحرک ({ATR_SL:g} ATR پشت بالاترین قیمت)\n"
            f"اندازه: {t['size']:.2f} دلار · ریسک: {t['risk0']:.2f} دلار "
            f"({t['risk0'] / st['balance'] * 100:.2f}% کیف پول){half}\n"
            f"باز: {len(st['open'])} از {MAX_OPEN} · آزاد بعد از این: {free_cash(st):.2f}\n\n"
            f"بالا رفتن قابل‌توجه استاپ اینجا اعلام می‌شود.\n"
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
    score = res.get("score")
    score_txt = f" · امتیاز {score:.1f}" if score is not None else ""
    if res["ok"]:
        return f"سیگنال معتبر: {res.get('setup', 'ورود')} ({res.get('regime', 'نامشخص')}){score_txt}"
    reasons = res.get("reasons") or []
    if reasons:
        return "بدون ورود · " + "، ".join(reasons[:2]) + score_txt
    failed = [label for label, ok in res["filters"] if not ok]
    return ("نیازمند تأیید عوامل دیگر" if not failed else "امتیاز ناکافی: " + "، ".join(failed[:2])) + score_txt





def record_diagnostic(st, name, res, now, reason, new_candle):
    """Persist the strategy result and the first execution gate that blocked entry."""
    item = st.setdefault("diagnostics", {}).setdefault(
        name, {"checks": 0, "strategy_pass": 0, "entries": 0, "blocked": {}, "last": {}})
    if new_candle:
        item["checks"] = int(item.get("checks", 0)) + 1
        if res.get("ok"):
            item["strategy_pass"] = int(item.get("strategy_pass", 0)) + 1
        if reason == "ورود مجازی باز شد":
            item["entries"] = int(item.get("entries", 0)) + 1
        elif reason:
            blocked = item.setdefault("blocked", {})
            blocked[reason] = int(blocked.get(reason, 0)) + 1
    item["last"] = {
        "at": int(now), "candle": int(res.get("candle", 0)),
        "score": float(res.get("score", 0)), "threshold": 6.0,
        "regime": res.get("regime", "unknown"), "setup": res.get("setup", ""),
        "strategy_ok": bool(res.get("ok")),
        "failed_filters": [label for label, passed in res.get("filters", []) if not passed],
        "strategy_reasons": list(res.get("reasons", [])),
        "decision": reason or ("در انتظار بررسی محدودیت‌های اجرا" if res.get("ok") else "شرایط استراتژی برقرار نیست")
    }


def diagnostics_reply(st):
    lines = ["گزارش تشخیصی ورودها (فقط شبیه‌سازی):"]
    for _, name in PAIRS:
        item = st.get("diagnostics", {}).get(name)
        if not item or not item.get("last"):
            lines.append(f"\\n{name}: هنوز تحلیل ثبت نشده.")
            continue
        last = item["last"]
        top = sorted(item.get("blocked", {}).items(), key=lambda x: -x[1])[:3]
        blocked = "؛ ".join(f"{k}: {v}" for k, v in top) or "مانع ثبت‌شده‌ای ندارد"
        failed = "، ".join(last.get("failed_filters", [])) or "هیچ‌کدام"
        lines.append(
            f"\\n{name} | بررسی {item.get('checks', 0)} | عبور استراتژی {item.get('strategy_pass', 0)} | ورود مجازی {item.get('entries', 0)}"
            f"\\nامتیاز {last.get('score', 0):.2f}/{last.get('threshold', 6):.1f} | رژیم {last.get('regime')} | الگو {last.get('setup') or 'ندارد'}"
            f"\\nتصمیم اخیر: {last.get('decision')}\\nفیلترهای ردشده: {failed}"
            f"\\nموانع پرتکرار: {blocked}"
        )
    return "\\n".join(lines)

# ---------------- live management of an open trade ----------------
def manage_live(st, name, raw_m, now):
    """Returns ("exit"|"trail", text) or None."""
    t = st["open"][name]
    bucket = int(t["opened"] // MGMT_SEC) * MGMT_SEC  # 1h candle that contains the entry
    hit = None
    for c in raw_m:
        if c["t"] < bucket:
            continue
        done = c["t"] + MGMT_SEC <= now
        if done and c["t"] <= t["checked"]:
            continue
        if c["t"] == bucket:
            hi = lo = op = c["c"]  # price moves before our entry must not count
        else:
            hi, lo, op = c["h"], c["l"], c["o"]
        hit = step_trade(t, hi, lo, done, op)
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
    notified = t.get("notified_sl", t["sl0"])
    if t["sl"] >= notified + 0.5 * t["atr"]:   # tell me only about meaningful stop moves
        t["notified_sl"] = t["sl"]
        return "trail", trail_msg(name, t, notified, raw_m[-1]["c"])
    return None


# ---------------- /plan report: how far price must move for entry or exit ----------------
def move_text(level, live):
    """'+6.0% | +4,648$' (plus toman when USD_TOMAN is set): move needed from live price to level."""
    diff = level - live
    sign = "+" if diff >= 0 else "-"
    txt = f"{(level / live - 1) * 100:+.1f}% | {sign}{fmt(abs(diff))}$"
    if USD_TOMAN > 0:
        txt += f" | {diff * USD_TOMAN:+,.0f} تومان"
    return txt


def daily_detail(d):
    """Same test as daily_trend_up, but returns the numbers: (ok, close, ema50, slope_5d)."""
    if len(d) < MIN_DAILY:
        return None
    closes = [x["c"] for x in d[-200:]]
    e = ema(closes, 50)
    return (bool(closes[-1] > e[-1] and e[-1] > e[-6]), closes[-1], e[-1], e[-1] / e[-6] - 1)


def plan_pair(name, c4, d, live, btc_up):
    """Report the SAME score-based entry decision used by the live strategy."""
    res = analyze(c4[-WINDOW:], d[-200:], btc_up, name)
    lines = [f"{name} · قیمت فعلی {fmt(live)}",
             f"امتیاز استراتژی: {res.get('score', 0):.2f} / حد ورود {analyze_strategy.ENTRY_SCORE if hasattr(analyze_strategy, 'ENTRY_SCORE') else 6.0:.1f}",
             f"رژیم روزانه: {res.get('regime', 'نامشخص')}"]
    if res.get("setup"):
        lines.append(f"الگوی شناسایی‌شده: {res['setup']}")
    if res.get("ok"):
        lines.append("✅ شرایط امتیازی ورود در آخرین کندل بسته‌شده برقرار است.")
    else:
        lines.append("❌ در آخرین بررسی، شرط ورود برقرار نیست.")
    passed = [label for label, ok in res.get("filters", []) if ok]
    failed = [label for label, ok in res.get("filters", []) if not ok]
    if passed:
        lines.append("تأییدشده: " + "، ".join(passed))
    if failed:
        lines.append("تأییدنشده: " + "، ".join(failed))
    reasons = res.get("reasons") or []
    if reasons:
        lines.append("دلایل/هشدارها: " + "، ".join(dict.fromkeys(reasons)))
    lines.append("توجه: این گزارش بر اساس کندل‌های بسته‌شده و همان امتیازدهی adaptive_strategy است؛ قیمت لحظه‌ای به‌تنهایی سیگنال را فعال نمی‌کند.")
    return lines

def plan_open(name, t, live, now):
    """Lines for a coin WITH an open trade: where the exit is."""
    left = max(0.0, (t["opened"] + MAX_HOURS * 3600 - now) / 86400)
    pnl = t["size"] * (live / t["entry"] - 1)
    return [f"{name} · {fmt(live)} · معامله باز",
            f"ورود {fmt(t['entry'])} | شناور {pnl:+.2f}$ ({(live / t['entry'] - 1) * 100:+.1f}%)",
            f"خروج: اگر پایین بیاید تا {fmt(t['sl'])} ({move_text(t['sl'], live)}) بسته می‌شود",
            "با بالا رفتن قیمت، این سطح هم بالا می‌رود",
            f"حداکثر {left:.0f} روز دیگر باز می‌ماند"]


def plan_reply(st, now):
    """On-demand report for the /plan command."""
    head = [f"معاملات باز: {len(st['open'])} از {MAX_OPEN}"
            + (" · ⛔ معامله‌ی جدید فعلاً متوقف است" if st.get("halted") else "")]
    raws = {}
    for pair, name in PAIRS:
        try:
            raws[name] = fetch_candles(pair, INTERVAL)
        except Exception as e:  # noqa
            raws[name] = e
        time.sleep(API_PAUSE)

    daily_cache = {}

    def daily(pair):
        if pair not in daily_cache:
            raw = fetch_candles(pair, HTF_INTERVAL)
            daily_cache[pair] = [c for c in raw if c["t"] + HTF_INTERVAL * 60 <= now]
            time.sleep(API_PAUSE)
        return daily_cache[pair]

    btc_d = btc_live = None
    try:
        btc_d = daily_detail(daily(NAME2PAIR["BTC/USD"]))
        if not isinstance(raws.get("BTC/USD"), Exception):
            btc_live = raws["BTC/USD"][-1]["c"]
    except Exception as e:  # noqa
        print(f"[PLAN] BTC daily: {e}")

    blocks = []
    for pair, name in PAIRS:
        raw = raws[name]
        try:
            if isinstance(raw, Exception):
                raise raw
            live = raw[-1]["c"]
            if name in st["open"]:
                lines = plan_open(name, st["open"][name], live, now)
            else:
                closed = [c for c in raw if c["t"] + PERIOD <= now]
                if len(closed) < MIN_CANDLES:
                    raise RuntimeError(f"فقط {len(closed)} کندل بسته موجود است")
                lines = plan_pair(name, closed, daily(pair), live, btc_d[0] if btc_d is not None else None)
                cd = st["cooldown"].get(name, 0)
                if cd > now:
                    lines.append(f"⏳ کول‌داون بعد از ضرر: حدود {(cd - now) / 3600:.0f} ساعت دیگر")
        except Exception as e:  # noqa
            lines = [f"{name}: دریافت داده ناموفق ({str(e)[:80]})"]
        blocks.append("\n".join(lines))
    return "\n".join(head) + "\n\n" + "\n\n".join(blocks)


# ---------------- Telegram commands ----------------
HELP_TEXT = ("دستورها:\n"
             "/status معاملات باز با قیمت لحظه‌ای\n"
             "/stats وضعیت کیف پول و آمار هر ارز\n"
             "/plan هر ارز چقدر باید بالا یا پایین برود تا ورود یا خروج شود\n"
             "/diagnostics علت رد یا عبور هر ارز از شروط ورود\n"
             "/help همین راهنما\n\n"
             "جواب‌ها با اجرای بعدی بات می‌آیند (معمولاً ۱ تا ۲ دقیقه). همه‌ی معاملات مجازی‌اند.")


def status_reply(st, prices, now):
    if not st["open"]:
        return "الان هیچ معامله‌ی بازی نیست.\n\n" + wallet_text(st)
    lines = []
    for name, t in st["open"].items():
        hours = max(0, (now - t["opened"]) / 3600)
        tp_txt = fmt(t["tp"]) if t.get("tp") else "ندارد (استاپ متحرک)"
        line = (f"{name} Long از {fmt(t['entry'])} · استاپ {fmt(t['sl'])} · حد سود {tp_txt} · "
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
        elif cmd == "/plan":
            try:
                out.append(plan_reply(st, now))
            except Exception as e:  # noqa
                out.append(f"⚠️ ساخت گزارش ناموفق بود: {str(e)[:100]}")
        elif cmd == "/diagnostics":
            out.append(diagnostics_reply(st))
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
    # 1) 4h candles for every pair - only when a signal check is due.
    need_signals = report_due or bool(st["failed"]) or signals_pending(st, now)
    data = {}
    if need_signals:
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
            print(f"[ERROR] manage {name}: {e}")  # retried next run
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
                st["btc_warn"] = False
            except Exception as e:  # noqa
                print(f"[ERROR] BTC daily: {e}")
                btc_cache["v"] = None
                if not st.get("btc_warn"):   # tell me once, not every run
                    st["btc_warn"] = True
                    msgs.append(f"⚠️ دریافت کندل روزانه‌ی BTC شکست خورد: {str(e)[:120]}\n"
                                f"تا رفع مشکل، سیگنال جدیدی بررسی نمی‌شود.")
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
            if name in NO_BTC_FILTER:
                b = True   # BTC filter is not applied to this pair
            else:
                b = btc_up()
                if b is None:
                    raise RuntimeError("وضعیت BTC نامشخص است")
            res = analyze(closed, get_daily(pair), b, name)
        except Exception as e:  # noqa
            print(f"[ERROR] analyze {name}: {e}")  # candle not marked as seen: retried next run
            continue
        analyses[name] = res
        res["candle"] = last["t"]
        if not new_candle:
            continue
        if not res["ok"]:
            record_diagnostic(st, name, res, now, "رد شروط خود استراتژی", True)
        if ps["last_candle"]:
            missed = (last["t"] - ps["last_candle"]) // PERIOD - 1
            if missed >= 1:
                gaps.append(f"{name}: {missed}")
        ps["last_candle"] = last["t"]
        if name in st["open"]:
            record_diagnostic(st, name, res, now, "برای این ارز معامله‌ی باز وجود دارد", True)
            continue
        if name in exited:
            record_diagnostic(st, name, res, now, "معامله‌ی همین ارز در این چرخه بسته شد", True)
            continue
        if not res["ok"]:
            continue
        if st["cooldown"].get(name, 0) > now:
            record_diagnostic(st, name, res, now, "دوره‌ی انتظار پس از ضرر فعال است", True)
            continue
        age = now - (last["t"] + PERIOD)
        if age > MAX_SIGNAL_AGE:
            record_diagnostic(st, name, res, now, "سیگنال دیر شناسایی شد", True)
            msgs.append(f"⚪ سیگنال خرید {name} رد شد: {age / 60:.0f} دقیقه بعد از بسته شدن کندل پیدا شد (دیر).")
            continue
        if data[name]["price"] - last["c"] > MAX_CHASE * res["atr"]:
            record_diagnostic(st, name, res, now, "قیمت بیش از حد از نقطه‌ی سیگنال فاصله گرفته", True)
            msgs.append(f"⚪ سیگنال خرید {name} رد شد: قیمت از نقطه‌ی سیگنال ({fmt(last['c'])}) "
                        f"بیش از {MAX_CHASE:g} ATR بالاتر رفته ({fmt(data[name]['price'])}).")
            continue
        record_diagnostic(st, name, res, now, "در انتظار بررسی محدودیت‌های اجرا", True)
        cands.append((res["rank"], name, res))

    if gaps:
        msgs.append(f"⚠️ این تعداد کندل {INTERVAL // 60} ساعته بررسی نشده بود "
                    f"(تأخیر یا توقف GitHub Actions): " + "، ".join(gaps))

    # 4) drawdown guard
    hm, risk_mult = update_halt(st, now)
    msgs += hm

    # 5) open new trades: strongest recent momentum first, limited by cash, count and total risk
    for _, name, res in sorted(cands, key=lambda x: -x[0]):
        if st["halted"]:
            record_diagnostic(st, name, res, now, "توقف حفاظتی کیف پول فعال است", True)
            msgs.append(f"⚪ سیگنال خرید {name} رد شد: معامله‌ی جدید به خاطر افت کیف پول متوقف است.")
            continue
        entry = data[name]["price"] * (1 + SLIP)
        t, why = open_position(st, name, entry, res["atr"], now, res["setup"], risk_mult)
        if t is None:
            record_diagnostic(st, name, res, now, why, True)
            msgs.append(f"⚪ سیگنال خرید {name} رد شد: {why}.")
            continue
        record_diagnostic(st, name, res, now, "ورود مجازی باز شد", True)
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
        msgs.append(f"✅ بات نسخه‌ی ۴ روشن شد (روندیابی، فقط خرید/اسپات، کیف پول مشترک): {names}\n"
                    f"سرمایه‌ی مجازی: {START_BALANCE:.0f} دلار · کندل {INTERVAL // 60} ساعته\n"
                    f"ریسک هر معامله {RISK * 100:.2f}% · حداکثر {MAX_OPEN} معامله‌ی هم‌زمان.\n"
                    f"برای راهنما /help را بفرست.")
    elif status == "restored":
        msgs.append("⚠️ state.json خراب بود و از نسخه‌ی پشتیبان بازیابی شد. "
                    "ممکن است آخرین تغییرات از دست رفته باشد.")

    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    report_due = now_utc.hour == DAILY_HOUR_UTC and st["last_daily"] != today

    # Fast path: most runs have nothing to do and finish in a few seconds.
    prices = {}
    if st["open"] or report_due or st["failed"] or signals_pending(st, now):
        prices = market_cycle(st, now, msgs, report_due, today)
    else:
        print("[IDLE] nothing to evaluate in this run")

    # Telegram commands (/status, /stats, /plan, /help)
    msgs += poll_commands(st, prices, now)

    # Save first, then send (undelivered messages stay queued in the state file)
    st["outbox"].extend(msgs)
    st["outbox"] = st["outbox"][-OUTBOX_MAX:]
    save_state(st)
    ok = flush_outbox(st)
    save_state(st)
    if not ok:
        raise SystemExit(1)  # makes the GitHub run red so you notice Telegram problems


if __name__ == "__main__":
    main()
