"""Run the independent strategy competition on CSV history or recent Kraken candles.

CSV:  python -m laboratory.run --csv-dir data
Live: python -m laboratory.run --live
"""
import argparse
import csv
import json
import os
import time
from .engine import run_competition
from .analysis import build_report
from .strategies import STRATEGIES

PAIRS = ["XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD", "ADAUSD", "LINKUSD",
         "AVAXUSD", "LTCUSD", "DOTUSD", "ATOMUSD", "PAXGUSD"]


def read_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            try:
                item = {"t": int(float(row[0])), "o": float(row[1]), "h": float(row[2]),
                        "l": float(row[3]), "c": float(row[4]), "v": float(row[5])}
            except (ValueError, IndexError):
                continue
            if item["c"] > 0 and item["h"] >= item["l"]:
                rows.append(item)
    unique = {row["t"]: row for row in rows}
    return [unique[t] for t in sorted(unique)]


def load_live_data():
    # Reuse the project's existing public OHLC reader; this does not place orders.
    from bot import fetch_candles, INTERVAL
    data = {}
    now = time.time()
    for symbol in PAIRS:
        try:
            candles = fetch_candles(symbol, INTERVAL)
            # Kraken's final OHLC row may still be forming; never use it in a backtest.
            candles = [row for row in candles if row["t"] + INTERVAL * 60 <= now]
            if len(candles) >= 211:
                data[symbol] = candles
            print(f"[DATA] {symbol}: {len(candles)} closed candles")
        except Exception as exc:
            print(f"[DATA ERROR] {symbol}: {exc}")
        time.sleep(1)
    return data


def main():
    parser = argparse.ArgumentParser(description="Independent paper strategy competition")
    parser.add_argument("--csv-dir", default=None, help="Directory containing *_240.csv files")
    parser.add_argument("--live", action="store_true", help="Fetch recent public Kraken OHLC candles")
    parser.add_argument("--out", default="results/competition.json")
    parser.add_argument("--strategies", nargs="*", choices=sorted(STRATEGIES), default=None)
    args = parser.parse_args()
    if args.live and args.csv_dir:
        parser.error("Choose either --live or --csv-dir, not both")
    if not args.live and not args.csv_dir:
        parser.error("Provide --csv-dir data or use --live")
    data = load_live_data() if args.live else {}
    if args.csv_dir:
        for symbol in PAIRS:
            path = os.path.join(args.csv_dir, f"{symbol}_240.csv")
            if os.path.isfile(path):
                candles = read_csv(path)
                if len(candles) >= 211:
                    data[symbol] = candles
    if not data:
        parser.error("No usable data; expected at least 211 valid 4h candles per asset")
    selected = {name: STRATEGIES[name] for name in args.strategies} if args.strategies else STRATEGIES
    report = build_report(run_competition(data, selected))
    report["data"] = {pair: {"candles": len(rows), "first": rows[0]["t"], "last": rows[-1]["t"]}
                      for pair, rows in data.items()}
    report["data_source"] = "kraken_recent_ohlc" if args.live else "local_csv"
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"Saved competition report: {args.out}")
    for name, metrics in sorted(report["leaderboard"].items(),
                                key=lambda x: x[1]["mean_asset_return_pct"], reverse=True):
        print(f"{name:30} return={metrics['mean_asset_return_pct']:+.2f}% "
              f"trades={metrics['trades']} win={metrics['win_rate_pct']:.1f}% "
              f"worstDD={metrics['worst_asset_drawdown_pct']:.2f}%")
    print("Regime matrix and failure analysis are in the JSON report.")


if __name__ == "__main__":
    main()
