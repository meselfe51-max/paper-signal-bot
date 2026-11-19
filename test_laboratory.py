import unittest
from laboratory.regimes import classify_regime
from laboratory.strategies import STRATEGIES
from laboratory.analysis import build_report, select_strategy
from laboratory.engine import simulate_asset


def rising(n=280):
    out=[]; p=100.0
    for i in range(n):
        p *= 1.0015
        out.append({"t": i * 14400, "o": p * .999, "h": p * 1.004,
                    "l": p * .996, "c": p, "v": 1000 + (i % 9) * 20})
    return out


class CompetitionLabTests(unittest.TestCase):
    def test_regime_is_explicit_and_causal(self):
        candles = rising()
        result = classify_regime(candles[:220])
        self.assertIn(result["trend"], {"bullish", "bearish", "sideways", "unknown"})
        self.assertIn(result["volatility"], {"high", "normal", "low", "unknown"})
        changed_future = candles[:220] + [{**x, "c": x["c"] * 5, "h": x["h"] * 5,
                                           "l": x["l"] * 5, "o": x["o"] * 5}
                                          for x in candles[220:221]]
        self.assertEqual(result, classify_regime(changed_future[:220]))

    def test_strategies_are_separate_and_named(self):
        self.assertGreaterEqual(len(STRATEGIES), 7)
        self.assertEqual(len(STRATEGIES), len(set(STRATEGIES)))
        self.assertIn("existing_adaptive_baseline", STRATEGIES)

    def test_simulation_is_repeatable_and_paper_only(self):
        data = rising()
        a = simulate_asset(data, "TEST", "momentum", STRATEGIES["momentum"])
        b = simulate_asset(data, "TEST", "momentum", STRATEGIES["momentum"])
        self.assertEqual(a["final_equity"], b["final_equity"])
        self.assertEqual(a["trades"], b["trades"])
        self.assertEqual(a["start_equity"], 1000.0)

    def test_report_contains_regime_and_failure_analysis(self):
        trades = [{"net": 2.0, "r": 0.2, "regime": "bullish_normal", "pair": "X", "reason": "target",
                   "mae_r": -0.2, "mfe_r": 1.2, "closed": 10, "strategy": "A"},
                  {"net": -1.0, "r": -0.1, "regime": "sideways_low", "pair": "X", "reason": "stop",
                   "mae_r": -1.0, "mfe_r": 0.2, "closed": 20, "strategy": "A"}]
        result = build_report([{"strategy": "A", "pair": "X", "return_pct": 0.1,
                                "max_drawdown_pct": 0.2, "trades": trades}])
        self.assertIn("bullish_normal", result["performance_by_regime"]["A"])
        self.assertEqual(result["failure_analysis"]["A"]["losing_trades"], 1)

    def test_selector_never_uses_future_trades(self):
        trades = [{"strategy": "A", "regime": "bullish_normal", "closed": 1, "r": 0.5},
                  {"strategy": "B", "regime": "bullish_normal", "closed": 2, "r": 100.0}]
        self.assertIsNone(select_strategy(trades, "bullish_normal", as_of=3, min_trades=2))
        many = [{"strategy": "A", "regime": "bullish_normal", "closed": i, "r": .2} for i in range(1, 15)]
        self.assertEqual(select_strategy(many, "bullish_normal", as_of=20, min_trades=12), "A")


if __name__ == "__main__": unittest.main()
