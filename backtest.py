"""Backtest for bot.py - uses the SAME functions as the live bot (no second copy of the logic).

Usage
  python backtest.py --csv-dir data     # Kraken 4h files: XBTUSD_240.csv, ETHUSD_240.csv, ...
  python backtest.py --live             # last ~720 4h candles from the Kraken API (short, only a smoke test)

CSV format (Kraken OHLCVT download): timestamp, open, high, low, close, volume, trades
Realism: signal at candle close -> entry at the NEXT candle open (+slippage); fees and slippage
from bot.py; shared wallet, position cap, total-risk cap, cooldown and drawdown guard all active.
"""
import argparse
import csv
import os
import sys
import time
from bisect import bisect_right

import bot

BTC = "BTC/USD"


def read_csv(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.reader(f):
            try:
                t = int(float(r[0]))
            except ValueError:
                continue  # header line
            rows.append({"t": t, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                         "c": float(r[4]), "v": float(r[5])})
    rows.sort(key=lambda x: x["t"])
    return rows


def to_daily(c4):
    days = {}
    for x in c4:
        days.setdefault(x["t"] // 86400 * 86400, []).append(x)
    out = []
    for t in sorted(days):
        g = days[t]
        if len(g) < 4:
            continue
        out.append({"t": t, "o": g[0]["o"], "h": max(x["h"] for x in g), "l": min(x["l"] for x in g),
                    "c": g[-1]["c"], "v": sum(x["v"] for x in g)})
    return out


def load_data(args):
    data = {}
    now = time.time()
    for pair, name in bot.PAIRS:
        if args.live:
            raw = bot.fetch_candles(pair, bot.INTERVAL)
            c4 = [c for c in raw if c["t"] + bot.PERIOD <= now]
            time.sleep(1)
        else:
            path = os.path.join(args.csv_dir, f"{pair}_{bot.INTERVAL}.csv")
            if not os.path.exists(path):
                print(f"[skip] {path} not found")
                continue
            c4 = read_csv(path)
        if len(c4) > bot.MIN_CANDLES:
            data[name] = c4
    if BTC not in data:
        sys.exit("BTC/USD data is required (it is the market filter).")
    return data


def simulate(data, fee_mult=1.0):
    old = (bot.FEE, bot.SLIP)
    bot.FEE, bot.SLIP = old[0] * fee_mult, old[1] * fee_mult
    try:
        return _simulate(data)
    finally:
        bot.FEE, bot.SLIP = old


def _simulate(data):
    names = list(data)
    idx = {n: {c["t"]: i for i, c in enumerate(data[n])} for n in names}
    daily = {n: to_daily(data[n]) for n in names}
    dends = {n: [d["t"] + 86400 for d in daily[n]] for n in names}
    ts_all = sorted({c["t"] for n in names for c in data[n]})
    st = bot.new_state()
    pending, equity, last_close = [], [], {}

    for ts in ts_all:
        now_close = ts + bot.PERIOD
        cur = {n: (idx[n][ts], data[n][idx[n][ts]]) for n in names if ts in idx[n]}
        for n, (_, c) in cur.items():
            last_close[n] = c["c"]
        _, risk_mult = bot.update_halt(st, ts)

        # 1) entries decided at the previous close are filled at this candle's open
        for _, _, n, atr, setup in sorted(pending, key=lambda x: (-x[0], -x[1])):
            if n not in cur or n in st["open"] or st["halted"]:
                continue
            bot.open_position(st, n, cur[n][1]["o"] * (1 + bot.SLIP), atr, ts, setup, risk_mult)
        pending = []

        # 2) manage open trades with this candle
        for n in list(st["open"]):
            if n not in cur:
                continue
            c, t = cur[n][1], st["open"][n]
            hit = bot.step_trade(t, c["h"], c["l"], True)
            if hit is None and now_close - t["opened"] > bot.MAX_HOURS * 3600:
                hit = (c["c"], "time", True)
            if hit:
                px, why, market = hit
                if market:
                    px *= 1 - bot.SLIP
                bot.close_trade(st, n, px, why, now_close)

        # 3) look for signals on the candle that just closed
        kb = bisect_right(dends[BTC], now_close)
        btc_up = bot.daily_trend_up(daily[BTC][max(0, kb - 200):kb])
        for n in names:
            if n not in cur or n in st["open"] or st["cooldown"].get(n, 0) > now_close:
                continue
            i = cur[n][0]
            if i + 1 < bot.MIN_CANDLES:
                continue
            k = bisect_right(dends[n], now_close)
            res = bot.analyze(data[n][max(0, i + 1 - bot.WINDOW):i + 1], daily[n][max(0, k - 200):k], btc_up)
            if res["ok"]:
                pending.append((res["n"], res["adx"], n, res["atr"], res["setup"]))

        eq = st["balance"] + sum(t["size"] * (last_close[n] / t["entry"] - 1) for n, t in st["open"].items())
        equity.append((ts, eq))
    return st, equity, ts_all


def block(label, h, extra=""):
    n, w, l, _, pf, avg_r = bot.stats(h)
    pf_txt = "inf" if pf == float("inf") else f"{pf:.2f}"
    wr = (w / n * 100) if n else 0
    tot_r = sum(x.get("r", 0) for x in h)
    print(f"  {label:<28} trades {n:>4} | win {wr:5.1f}% | PF {pf_txt:>5} | avg {avg_r:+.2f}R | sum {tot_r:+.1f}R {extra}")


def summary(title, st, equity, ts_all, data):
    h = st["history"]
    peak, mdd = equity[0][1], 0.0
    for _, e in equity:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak * 100)
    ret = (equity[-1][1] / bot.START_BALANCE - 1) * 100
    days = (ts_all[-1] - ts_all[0]) / 86400
    print(f"\n=== {title} ({days:.0f} days) ===")
    print(f"  return {ret:+.1f}% | max drawdown (mark-to-market) -{mdd:.1f}% | open at end: {len(st['open'])}")
    block("ALL TRADES", h)
    cut = ts_all[0] + 0.6 * (ts_all[-1] - ts_all[0])
    block("first 60% (in-sample)", [x for x in h if x["closed"] <= cut])
    block("last 40% (out-of-sample)", [x for x in h if x["closed"] > cut])
    for _, name in bot.PAIRS:
        hp = [x for x in h if x["pair"] == name]
        if hp:
            block(name, hp, f"| pnl {sum(x['net'] for x in hp):+.1f}$")
    return ret


def buy_and_hold(data):
    rets = []
    for n, c4 in data.items():
        a = c4[bot.MIN_CANDLES]["c"]
        rets.append((c4[-1]["c"] / a - 1) * 100)
    return sum(rets) / len(rets)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="data")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    data = load_data(args)
    print("pairs:", ", ".join(data), "| candles each:", ", ".join(str(len(v)) for v in data.values()))

    t0 = time.time()
    base = summary("BASE (fees from bot.py)", *simulate(data), data)
    summary("STRESS (fees and slippage x2)", *simulate(data, 2.0), data)
    print(f"\n  buy & hold, equal weight, same period: {buy_and_hold(data):+.1f}%  (run took {time.time() - t0:.0f}s)")

    print("""
How to read this (honest rules of thumb)
  - Fewer than ~100 trades: the numbers are mostly luck, do not trust them.
  - Want: PF > 1.3 in BOTH the in-sample and the out-of-sample block, avg R > +0.15,
    and STRESS still positive. If only the first block is good, that is overfitting.
  - Do not tune the settings in bot.py until the backtest looks great: that only fits the past.""")
    return base


if __name__ == "__main__":
    main()
