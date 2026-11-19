"""Historical OHLCV loading and quality checks for the paper competition.

All contestants receive the same complete, chronological history available for an
asset. Missing no-trade intervals are forward-filled as flat, zero-volume candles
so bar-count based exits keep their intended wall-clock meaning.
"""
import csv
import os
import re

INTERVAL_SECONDS = 240 * 60
MIN_CANDLES = 211


def read_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            try:
                item = {"t": int(float(row[0])), "o": float(row[1]), "h": float(row[2]),
                        "l": float(row[3]), "c": float(row[4]), "v": float(row[5])}
            except (ValueError, IndexError, TypeError):
                continue
            values = (item["o"], item["h"], item["l"], item["c"], item["v"])
            if (item["t"] >= 0 and all(map(lambda x: x == x and abs(x) != float("inf"), values))
                    and item["o"] > 0 and item["c"] > 0 and item["h"] >= max(item["o"], item["c"], item["l"])
                    and item["l"] > 0 and item["v"] >= 0):
                rows.append(item)

    unique = {row["t"]: row for row in rows}
    ordered = [unique[t] for t in sorted(unique)]
    return ordered


def normalize_candles(rows, interval_seconds=INTERVAL_SECONDS):
    """Deduplicate, sort and fill documented no-trade gaps with flat candles."""
    unique = {int(row["t"]): row for row in rows}
    ordered = [unique[t] for t in sorted(unique)]
    if not ordered:
        return [], {"raw_candles": 0, "filled_no_trade_candles": 0, "large_gaps": 0}

    out = [ordered[0]]
    filled = 0
    large_gaps = 0
    # Avoid expanding corrupt timestamps into unbounded memory.
    max_fill = 500
    for row in ordered[1:]:
        previous = out[-1]
        delta = row["t"] - previous["t"]
        missing = max(0, round(delta / interval_seconds) - 1)
        if missing and abs(delta - (missing + 1) * interval_seconds) <= 60:
            if missing <= max_fill:
                close = previous["c"]
                for step in range(1, missing + 1):
                    ts = previous["t"] + step * interval_seconds
                    out.append({"t": ts, "o": close, "h": close, "l": close,
                                "c": close, "v": 0.0, "synthetic_no_trade": True})
                    filled += 1
            else:
                large_gaps += 1
        elif delta > interval_seconds * 1.5:
            large_gaps += 1
        out.append(row)
    return out, {"raw_candles": len(ordered), "filled_no_trade_candles": filled,
                 "large_gaps": large_gaps}


def find_pair_file(root, pair):
    """Find Kraken's pair/interval CSV even when it is nested in extracted ZIP folders."""
    candidates = []
    for current, _, files in os.walk(root):
        for filename in files:
            if not filename.lower().endswith(".csv"):
                continue
            stem = os.path.splitext(filename)[0].upper()
            normalized = re.sub(r"[^A-Z0-9]", "", stem)
            if normalized in {pair.upper() + "240", pair.upper() + "240MIN"}:
                candidates.append(os.path.join(current, filename))
    if not candidates:
        return None
    # Prefer a direct, explicitly named file; otherwise choose the largest history file.
    candidates.sort(key=lambda p: (os.path.basename(p).upper() != f"{pair.upper()}_240.CSV",
                                   -os.path.getsize(p), p))
    return candidates[0]


def load_csv_directory(root, pairs):
    data, diagnostics = {}, {}
    for pair in pairs:
        path = find_pair_file(root, pair)
        if not path:
            diagnostics[pair] = {"status": "missing"}
            continue
        raw = read_csv(path)
        candles, quality = normalize_candles(raw)
        diagnostics[pair] = {"status": "ok" if len(candles) >= MIN_CANDLES else "insufficient_history",
                             "file": os.path.relpath(path, root), **quality,
                             "candles_used": len(candles),
                             "first_timestamp": candles[0]["t"] if candles else None,
                             "last_timestamp": candles[-1]["t"] if candles else None}
        if len(candles) >= MIN_CANDLES:
            data[pair] = candles
    return data, diagnostics
