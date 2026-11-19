import os
import unittest
from unittest.mock import patch

import bot


class EntryDiagnosticsTests(unittest.TestCase):
    def test_counts_each_candle_once(self):
        state = {"diagnostics": {}}
        result = {
            "candle": 123, "score": 7.0, "regime": "bullish",
            "setup": "breakout", "ok": True, "filters": [],
            "reasons": []
        }
        bot.record_diagnostic(state, "BTC/USD", result, 1000, "ورود مجازی باز شد", True)
        bot.record_diagnostic(state, "BTC/USD", result, 1001, "ورود مجازی باز شد", True)
        item = state["diagnostics"]["BTC/USD"]
        self.assertEqual(item["checks"], 1)
        self.assertEqual(item["strategy_pass"], 1)
        self.assertEqual(item["entries"], 1)

    def test_blocker_and_rejected_filters_are_recorded(self):
        state = {"diagnostics": {}}
        result = {
            "candle": 456, "score": 4.0, "regime": "neutral",
            "setup": "", "ok": False, "filters": [("RSI", False)],
            "reasons": ["الگوی ورود معتبر شکل نگرفته"]
        }
        reason = bot.strategy_block_reason(result)
        bot.record_diagnostic(state, "ETH/USD", result, 2000, reason, True)
        item = state["diagnostics"]["ETH/USD"]
        self.assertEqual(item["checks"], 1)
        self.assertEqual(item["strategy_pass"], 0)
        self.assertIn("RSI", item["last"]["failed_filters"])
        self.assertIn(reason, item["blocked"])

    def test_report_uses_real_newlines_and_includes_data_errors(self):
        state = {"diagnostics": {}}
        bot.record_diagnostic_error(state, "ETH/USD", 2000, "daily candles unavailable", 456)
        report = bot.diagnostics_reply(state)
        self.assertIn("\nETH/USD", report)
        self.assertNotIn("\\nETH/USD", report)
        self.assertIn("daily candles unavailable", report)
        self.assertIn("خطاهای داده", report)

    def test_old_state_gets_diagnostic_defaults_without_resetting_wallet(self):
        state = {"v": bot.STATE_VERSION, "kind": bot.STATE_KIND,
                 "balance": 777.0, "peak": 900.0, "peak_all": 900.0,
                 "open": {}, "history": [], "pairs": {}}
        bot.fill_defaults(state)
        self.assertEqual(state["balance"], 777.0)
        self.assertEqual(state["diagnostics"], {})
        self.assertEqual(state["tg_commands_version"], 0)

    def test_every_advertised_command_has_a_polling_handler(self):
        for item in bot.TELEGRAM_COMMANDS:
            command = "/" + item["command"]
            update = {"result": [{"update_id": 1, "message": {
                "chat": {"id": 123}, "text": command
            }}]}
            with self.subTest(command=command):
                with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token",
                                             "TELEGRAM_CHAT_ID": "123"}):
                    with patch.object(bot, "http_json", return_value=update):
                        with patch.object(bot, "plan_reply", return_value="plan result"):
                            replies = bot.poll_commands(bot.new_state(), {}, 1000)
                self.assertEqual(len(replies), 1)
                self.assertNotIn("دستور ناشناخته", replies[0])

    def test_telegram_menu_sync_is_versioned_and_uses_same_command_list(self):
        state = bot.new_state()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}):
            with patch.object(bot, "http_json", return_value={"ok": True}) as request:
                bot.sync_telegram_commands(state)
        self.assertEqual(state["tg_commands_version"], bot.TELEGRAM_COMMANDS_VERSION)
        self.assertEqual(request.call_args.args[1]["commands"], list(bot.TELEGRAM_COMMANDS))


if __name__ == "__main__":
    unittest.main()
