from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from crypto_trader.notifier import send_telegram_message, sync_telegram_commands


class NetworkTimeout(Exception):
    pass


class TelegramCommandSyncTests(TestCase):
    def _config(self) -> dict:
        return {
            "telegram": {
                "enabled": True,
                "bot_token_env": "TELEGRAM_BOT_TOKEN",
            }
        }

    @patch("crypto_trader.notifier.telegram_enabled", return_value=True)
    @patch("crypto_trader.notifier._telegram_api_request", return_value={"ok": True})
    @patch("crypto_trader.notifier.get_journal_state", side_effect=NetworkTimeout("read operation timed out"))
    def test_sync_commands_ignores_retryable_storage_read_timeout(self, _get_state, telegram_request, _enabled) -> None:
        config = self._config()
        with patch("crypto_trader.notifier.set_journal_state"):
            self.assertTrue(sync_telegram_commands(config))
        self.assertGreaterEqual(telegram_request.call_count, 1)

    @patch("crypto_trader.notifier.telegram_enabled", return_value=True)
    @patch("crypto_trader.notifier._telegram_api_request", return_value={"ok": True})
    @patch("crypto_trader.notifier.get_journal_state", return_value=None)
    @patch("crypto_trader.notifier.set_journal_state", side_effect=NetworkTimeout("read operation timed out"))
    def test_sync_commands_ignores_retryable_storage_write_timeout(
        self,
        _set_state,
        _get_state,
        telegram_request,
        _enabled,
    ) -> None:
        self.assertTrue(sync_telegram_commands(self._config()))
        self.assertGreaterEqual(telegram_request.call_count, 1)

    @patch("crypto_trader.notifier.telegram_startup_quiet_active", return_value=False)
    @patch("crypto_trader.notifier.telegram_buttons_enabled", return_value=False)
    @patch("crypto_trader.notifier._telegram_api_request", return_value={"ok": True, "result": {"message_id": 123}})
    @patch("crypto_trader.notifier.get_journal_state", side_effect=NetworkTimeout("read operation timed out"))
    def test_send_message_ignores_retryable_storage_read_timeout(
        self,
        _get_state,
        telegram_request,
        _buttons,
        _quiet,
    ) -> None:
        with patch("crypto_trader.notifier.set_journal_state"):
            self.assertTrue(send_telegram_message(self._config(), "hello"))
        telegram_request.assert_called_once()
