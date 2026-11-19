"""Run the strategy competition on local Kraken-format 4h CSV files.

Example: python -m laboratory.run --csv-dir data --out results/competition.json
Expected files: XBTUSD_240.csv, ETHUSD_240.csv, etc.
"""
import argparse
import csv
import json
import os
from .engine import run_competition
from .analysis import build_report
from .strategies import STRATEGIES

PAIRS = ["XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD", "ADAUSD", "LINKUSD",
         "AVAXUSD", "LTCUSD", "DOTUSD", "ATOMUSD", "PAXGUSD"]


def read_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            try: timestamp = int(float(row[0]))
            except (ValueError, IndexError): continue
            try:
                item = {"t": timestamp, "o": float(row[1]), "h": float(row[2]),
                        "l": float(row[3]), "c": float(row[4]), "v": float(row[5])}
            except (ValueError, IndexError):
                continue
            if item["c"] > 0 and item["h"] >= item["l"]:
                rows.append(item)
    rows.sort(key=lambda x: x["t"])
    unique = {}
    for row in rows: unique[row["t"]] = row
    return [unique[t] for t in sorted(unique)]


def main():
    parser = argparse.ArgumentParser(description="Independent paper strategy competition")
    parser.add_argument("--csv-dir", required=True)
    parser.add_argument("--out", default="results/competition.json")
    parser.add_argument("--strategies", nargs="*", choices=sorted(STRATEGIES), default=None)
    args = parser.parse_args()
    data = {}
    for symbol in PAIRS:
        path = os.path.join(args.csv_dir, f"{symbol}_240.csv")
        if os.path.isfile(path):
            candles = read_csv(path)
            if len(candles) >= 211: data[symbol] = candles
    if not data:
        parser.error("No usable CSV data found; expected *_240.csv files with at least 211 candles")
    selected = {name: STRATEGIES[name] for name in args.strategies} if args.strategies else STRATEGIES
    report = build_report(run_competition(data, selected))
    report["data"] = {pair: {"candles": len(rows), "first": rows[0]["t"], "last": rows[-1]["t"]}
                      for pair, rows in data.items()}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"Saved competition report: {args.out}")
    for name, metrics in sorted(report["leaderboard"].items(), key=lambda x: x[1]["mean_asset_return_pct"], reverse=True):
        print(f"{name:30} return={metrics['mean_asset_return_pct']:+.2f}% trades={metrics['trades']} "
              f"win={metrics['win_rate_pct']:.1f}% worstDD={metrics['worst_asset_drawdown_pct']:.2f}%")
    print("Regime matrix and failure analysis are in the JSON report.")


if __name__ == "__main__": main()
