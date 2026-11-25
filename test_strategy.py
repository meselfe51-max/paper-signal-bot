import unittest
from unittest.mock import patch
from adaptive_strategy import analyze, _ema, _atr, _rsi

def candles(n=260, start=100.0, step=0.12):
    rows=[]
    p=start
    for i in range(n):
        p += step
        rows.append({"t":i*14400,"o":p-step/2,"h":p+0.3,"l":p-0.3,"c":p,"v":1000+(i%7)*80})
    return rows

class StrategyTests(unittest.TestCase):
    def test_indicators_return_values(self):
        cs=candles()
        self.assertIsNotNone(_ema([x["c"] for x in cs], 200))
        self.assertGreater(_atr(cs), 0)
        self.assertGreaterEqual(_rsi([x["c"] for x in cs]), 0)

    def test_insufficient_data_is_safe(self):
        r=analyze(candles(30), candles(30), True, "BTC/USD")
        self.assertFalse(r["ok"])
        self.assertTrue(r["reasons"])

    def test_result_contract(self):
        r=analyze(candles(), candles(100, step=0.5), True, "BTC/USD")
        for key in ("ok","setup","filters","atr","price","level","rank","score","regime","reasons"):
            self.assertIn(key,r)

    def test_clean_uptrend_can_produce_an_entry(self):
        r = analyze(candles(260, start=100.0, step=0.12),
                    candles(100, start=100.0, step=0.5), True, "BTC/USD")
        self.assertTrue(r["ok"], msg=f"Expected a valid paper signal, got: {r}")
        self.assertGreaterEqual(r["score"], 6.0)

    def test_bearish_daily_regime_is_not_an_absolute_veto(self):
        # The daily regime should reduce the score, not block a sufficiently
        # strong closed-candle 4h setup by itself.
        with patch("adaptive_strategy._daily_regime", return_value=("bearish", {
            "close": 100.0, "ema20": 101.0, "ema50": 102.0, "slope": -1.0
        })), patch("adaptive_strategy.ENTRY_SCORE", 0.0):
            result = analyze(candles(260, start=100.0, step=0.12),
                             candles(100, start=120.0, step=-0.2),
                             True, "BTC/USD")
        self.assertTrue(result["ok"], msg=f"Bearish regime unexpectedly vetoed setup: {result}")

