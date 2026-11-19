"""Historical OHLCVT ingestion with deterministic merge and data-quality checks."""
import csv
import math
import os
import re

INTERVAL_SECONDS = 240 * 60
MIN_CANDLES = 211


def read_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            try:
                timestamp = float(row[0])
                values = [float(row[i]) for i in range(1, 6)]
                if not math.isfinite(timestamp) or not all(math.isfinite(x) for x in values):
                    continue
                item = {"t": int(timestamp), "o": values[0], "h": values[1],
                        "l": values[2], "c": values[3], "v": values[4]}
            except (ValueError, IndexError, TypeError, OverflowError):
                continue
            if (item["t"] >= 0 and item["o"] > 0 and item["c"] > 0 and item["l"] > 0
                    and item["v"] >= 0 and item["h"] >= max(item["o"], item["c"], item["l"])):
                rows.append(item)
    # Last occurrence wins deterministically within a file.
    unique = {row["t"]: row for row in rows}
    return [unique[t] for t in sorted(unique)]


def normalize_candles(rows, interval_seconds=INTERVAL_SECONDS, max_fill=500):
    """Sort/deduplicate; fill only exact no-trade gaps and flag unsafe discontinuities."""
    unique = {int(row["t"]): row for row in rows}
    ordered = [unique[t] for t in sorted(unique)]
    if not ordered:
        return [], {"raw_candles": 0, "filled_no_trade_candles": 0,
                    "large_gaps": 0, "misaligned_timestamps": 0}

    misaligned = sum(1 for row in ordered if row["t"] % interval_seconds != 0)
    out = [ordered[0]]
    filled = 0
    large_gaps = 0
    for row in ordered[1:]:
        previous = out[-1]
        delta = row["t"] - previous["t"]
        steps = round(delta / interval_seconds)
        exact_grid_gap = steps >= 1 and abs(delta - steps * interval_seconds) <= 60
        missing = steps - 1 if exact_grid_gap else 0
        if missing:
            if missing <= max_fill:
                close = previous["c"]
                for step in range(1, missing + 1):
                    ts = previous["t"] + step * interval_seconds
                    out.append({"t": ts, "o": close, "h": close, "l": close,
                                "c": close, "v": 0.0, "synthetic_no_trade": True})
                    filled += 1
            else:
                large_gaps += 1
        elif delta > interval_seconds * 1.25:
            large_gaps += 1
        out.append(row)
    return out, {"raw_candles": len(ordered), "filled_no_trade_candles": filled,
                 "large_gaps": large_gaps, "misaligned_timestamps": misaligned}


def find_pair_files(root, pair):
    """Return every matching 240-minute CSV so full + incremental archives are merged."""
    candidates = []
    wanted = {pair.upper() + "240", pair.upper() + "240MIN"}
    for current, _, files in os.walk(root):
        for filename in files:
            if not filename.lower().endswith(".csv"):
                continue
            stem = os.path.splitext(filename)[0].upper()
            normalized = re.sub(r"[^A-Z0-9]", "", stem)
            if normalized in wanted:
                candidates.append(os.path.join(current, filename))
    return sorted(candidates)


def find_pair_file(root, pair):
    """Backward-compatible single-file helper; prefer the largest matching CSV."""
    candidates = find_pair_files(root, pair)
    if not candidates:
        return None
    return max(candidates, key=lambda p: (os.path.getsize(p), p))


def load_csv_directory(root, pairs):
    data, diagnostics = {}, {}
    for pair in pairs:
        paths = find_pair_files(root, pair)
        if not paths:
            diagnostics[pair] = {"status": "missing", "source_files": []}
            continue
        rows = []
        for path in paths:
            rows.extend(read_csv(path))
        candles, quality = normalize_candles(rows)
        status = ("insufficient_history" if len(candles) < MIN_CANDLES else
                  "unsafe_gaps" if quality["large_gaps"] else "ok")
        diagnostics[pair] = {
            "status": status,
            "source_files": [os.path.relpath(path, root) for path in paths],
            **quality,
            "candles_used": len(candles),
            "first_timestamp": candles[0]["t"] if candles else None,
            "last_timestamp": candles[-1]["t"] if candles else None,
        }
        # A large unfilled gap breaks bar-based holding periods and indicator continuity.
        if status == "ok":
            data[pair] = candles
    return data, diagnostics
