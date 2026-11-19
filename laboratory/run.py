"""Run the independent strategy competition on full CSV history or recent Kraken candles."""
import argparse
import json
import os
import time
from .data import load_csv_directory
from .engine import run_competition
from .analysis import build_report
from .strategies import STRATEGIES

PAIRS = ["XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD", "ADAUSD", "LINKUSD",
         "AVAXUSD", "LTCUSD", "DOTUSD", "ATOMUSD", "PAXGUSD"]


def load_live_data():
    # This fallback is deliberately recent-only; use the official historical CSV
    # archive through --csv-dir for multi-year tests.
    from bot import fetch_candles, INTERVAL
    from .data import normalize_candles
    data, diagnostics = {}, {}
    now = time.time()
    for symbol in PAIRS:
        try:
            candles = fetch_candles(symbol, INTERVAL)
            candles = [row for row in candles if row["t"] + INTERVAL * 60 <= now]
            candles, quality = normalize_candles(candles)
            diagnostics[symbol] = {"source": "recent_api", **quality, "candles_used": len(candles)}
            if len(candles) >= 211:
                data[symbol] = candles
            print(f"[DATA] {symbol}: {len(candles)} closed candles")
        except Exception as exc:
            diagnostics[symbol] = {"source": "recent_api", "error": str(exc)}
            print(f"[DATA ERROR] {symbol}: {exc}")
        time.sleep(1)
    return data, diagnostics


def main():
    parser = argparse.ArgumentParser(description="Independent paper strategy competition")
    parser.add_argument("--csv-dir", default=None, help="Directory containing extracted historical Kraken CSVs")
    parser.add_argument("--live", action="store_true", help="Fetch recent public Kraken OHLC candles (not full history)")
    parser.add_argument("--out", default="results/competition.json")
    parser.add_argument("--strategies", nargs="*", choices=sorted(STRATEGIES), default=None)
    args = parser.parse_args()
    if args.live and args.csv_dir:
        parser.error("Choose either --live or --csv-dir, not both")
    if not args.live and not args.csv_dir:
        parser.error("Provide --csv-dir <historical-data-folder> or use --live")

    if args.live:
        data, diagnostics = load_live_data()
        source = "kraken_recent_ohlc"
    else:
        data, diagnostics = load_csv_directory(args.csv_dir, PAIRS)
        source = "kraken_historical_csv"

    if not data:
        parser.error("No usable history. Need at least 211 valid 4h candles for one supported pair.")
    selected = {name: STRATEGIES[name] for name in args.strategies} if args.strategies else STRATEGIES
    report = build_report(run_competition(data, selected))
    report["data"] = diagnostics
    report["data_source"] = source
    report["history_policy"] = {
        "same_full_history_for_every_contestant": True,
        "signal_history": "all candles strictly before the decision candle",
        "timeframe_minutes": 240,
        "missing_trade_intervals": "flat zero-volume candles where the gap is an exact multiple of 4h",
        "live_api_limit": "recent-only; use the official historical CSV archive for multi-year evaluation",
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"Saved competition report: {args.out}")
    for name, metrics in sorted(report["leaderboard"].items(),
                                key=lambda x: x[1]["mean_asset_return_pct"], reverse=True):
        print(f"{name:30} return={metrics['mean_asset_return_pct']:+.2f}% "
              f"trades={metrics['trades']} win={metrics['win_rate_pct']:.1f}% "
              f"worstDD={metrics['worst_asset_drawdown_pct']:.2f}%")
    print("Full regime matrix, data coverage and failure analysis are in the JSON report.")


if __name__ == "__main__":
    main()
