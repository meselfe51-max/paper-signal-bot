"""Paper-trading signal bot.

Real prices from Kraken's public API, 10 indicators vote, signals and results go to Telegram.
No real money, no exchange keys. State is kept in state.json (committed by the workflow).
"""
import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ---------------- settings ----------------
PAIR = "XBTUSD"          # Kraken pair name
NAME = "BTC/USD"
INTERVAL = 240           # candle size in minutes (4h)
MIN_VOTES = 6            # votes needed in one direction
MIN_MARGIN = 3           # and at least this many more than the opposite side
ATR_SL = 1.5             # stop loss distance in ATRs
ATR_TP = 2.5             # take profit distance in ATRs
RISK = 0.02              # share of balance risked per trade
FEE = 0.001              # 0.1% per side
MAX_HOURS = 72           # close the trade at market after this long
START_BALANCE = 1000.0   # virtual dollars
DAILY_HOUR_UTC = 6       # daily status message hour
STATE_FILE = "state.json"


# ---------------- helpers ----------------
def http_json(url, data=None, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode() if data is not None else None,
                headers={
                    "User-Agent": "paper-signal-bot",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                result = json.loads(r.read().decode())
                if isinstance(result, dict) and result.get("ok") is False:
                    raise RuntimeError(
                        f"Telegram API error {result.get('error_code')}: "
                        f"{result.get('description', 'Unknown error')}"
                    )
                return result
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                details = json.loads(body)
                description = details.get("description", body)
                error_code = details.get("error_code", e.code)
                last = RuntimeError(f"HTTP {error_code}: {description}")
            except (json.JSONDecodeError, AttributeError):
                last = RuntimeError(f"HTTP {e.code}: {body or e.reason}")
            # HTTP errors such as 403 are generally not fixed by retrying.
            raise last from e
        except Exception as e:  # noqa
            last = e
            time.sleep(3)
    raise last


def fetch_candles():
    url = f"https://api.kraken.com/0/public/OHLC?pair={PAIR}&interval={INTERVAL}"
    j = http_json(url)
    if j.get("error"):
        raise RuntimeError(j["error"])
    res = j["result"]
    key = next(k for k in res if k != "last")
    return [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
         "c": float(r[4]), "v": float(r[6])}
        for r in res[key]
    ]


def fmt(x):
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.4f}"


def send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
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
