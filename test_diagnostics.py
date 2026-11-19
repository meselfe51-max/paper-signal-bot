import unittest

import bot


class EntryDiagnosticsTests(unittest.TestCase):
    def test_counts_each_candle_once(self):
        state = {"diagnostics": {}}
        result = {
            "candle": 123, "score": 7.0, "regime": "bullish",
            "setup": "breakout", "ok": True, "filters": [],
            "reasons": []
        }
        bot.record_diagnostic(state, "BTC/USD", result, 1000,
                              "در انتظار بررسی محدودیت‌های اجرا", True)
        bot.record_diagnostic(state, "BTC/USD", result, 1001,
                              "ورود مجازی باز شد", True)
        item = state["diagnostics"]["BTC/USD"]
        self.assertEqual(item["checks"], 1)
        self.assertEqual(item["strategy_pass"], 1)
        self.assertEqual(item["entries"], 0)  # same candle cannot be double-counted
        self.assertEqual(item["last"]["decision"], "ورود مجازی باز شد")

    def test_blocker_is_counted(self):
        state = {"diagnostics": {}}
        result = {
            "candle": 456, "score": 4.0, "regime": "neutral",
            "setup": "", "ok": False, "filters": [("RSI", False)],
            "reasons": ["no setup"]
        }
        bot.record_diagnostic(state, "ETH/USD", result, 2000,
                              "رد شروط خود استراتژی", True)
        item = state["diagnostics"]["ETH/USD"]
        self.assertEqual(item["checks"], 1)
        self.assertEqual(item["strategy_pass"], 0)
        self.assertEqual(item["blocked"]["رد شروط خود استراتژی"], 1)


if __name__ == "__main__":
    unittest.main()
