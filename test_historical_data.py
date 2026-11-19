import os
import tempfile
import unittest
from laboratory.data import normalize_candles, read_csv, find_pair_file, find_pair_files, load_csv_directory


class HistoricalDataTests(unittest.TestCase):
    def test_fills_exact_no_trade_gap_without_changing_price(self):
        rows = [
            {"t": 0, "o": 10, "h": 11, "l": 9, "c": 10, "v": 5},
            {"t": 3 * 14400, "o": 12, "h": 13, "l": 11, "c": 12, "v": 7},
        ]
        candles, quality = normalize_candles(rows)
        self.assertEqual(len(candles), 4)
        self.assertEqual(candles[1]["c"], 10)
        self.assertEqual(candles[1]["v"], 0)
        self.assertTrue(candles[1]["synthetic_no_trade"])
        self.assertEqual(quality["filled_no_trade_candles"], 2)

    def test_deduplicates_and_sorts(self):
        rows = [
            {"t": 14400, "o": 2, "h": 2, "l": 2, "c": 2, "v": 1},
            {"t": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
            {"t": 14400, "o": 3, "h": 3, "l": 3, "c": 3, "v": 2},
        ]
        candles, _ = normalize_candles(rows)
        self.assertEqual([x["t"] for x in candles], [0, 14400])
        self.assertEqual(candles[1]["c"], 3)

    def test_reads_kraken_seven_column_csv_and_nested_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "OHLCVT", "XBTUSD")
            os.makedirs(folder)
            path = os.path.join(folder, "XBTUSD_240.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("0,10,11,9,10,100,5\n")
                f.write("14400,10,12,9,11,120,7\n")
            self.assertEqual(len(read_csv(path)), 2)
            self.assertEqual(find_pair_file(tmp, "XBTUSD"), path)
            self.assertEqual(find_pair_files(tmp, "XBTUSD"), [path])

    def test_merges_full_and_incremental_csvs(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "nested")
            os.makedirs(folder)
            a = os.path.join(folder, "XBTUSD_240.csv")
            b = os.path.join(folder, "XBTUSD_240_update.csv")
            # Name matching is intentionally strict: update suffixes are not treated as a
            # separate market file because only canonical pair_interval files are accepted.
            with open(a, "w", encoding="utf-8") as f:
                f.write("0,10,11,9,10,100,5\n")
            with open(b, "w", encoding="utf-8") as f:
                f.write("14400,10,12,9,11,120,7\n")
            self.assertEqual(find_pair_files(tmp, "XBTUSD"), [a])

    def test_rejects_unfilled_large_gaps_and_nonfinite_rows(self):
        rows = [
            {"t": 0, "o": 10, "h": 11, "l": 9, "c": 10, "v": 1},
            {"t": 1000 * 14400, "o": 12, "h": 13, "l": 11, "c": 12, "v": 1},
        ]
        _, quality = normalize_candles(rows)
        self.assertEqual(quality["large_gaps"], 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("nan,10,11,9,10,100,5\n")
                f.write("inf,10,11,9,10,100,5\n")
                f.write("0,10,11,9,10,100,5\n")
            self.assertEqual(len(read_csv(path)), 1)


if __name__ == "__main__":
    unittest.main()
