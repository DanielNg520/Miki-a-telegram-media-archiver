from __future__ import annotations

import unittest

from pydantic import ValidationError

from miki_sorter_bot.config import Settings


def _values() -> dict[str, object]:
    return {
        "BOT_TOKEN": "token",
        "SOURCE_CHAT_ID": -100,
        "SOURCE_THREAD_ID": 5,
        "ARCHIVE_CHAT_ID": -200,
        "COLLECTOR_API_KEY": "secret",
        "COLLECTOR_DATABASE": "gvdb",
        "ROUTES_JSON": '[{"name":"Codes","thread_id":9,"keywords":["CR"]}]',
    }


class SettingsTests(unittest.TestCase):
    def test_valid_configuration_normalizes_routes(self) -> None:
        settings = Settings(**_values())

        self.assertEqual(settings.routes[0].name, "Codes")
        self.assertEqual(settings.routes[0].keywords, ["cr"])
        self.assertEqual(settings.default_request_limit, 20)
        self.assertEqual(settings.max_request_limit, 100)
        self.assertFalse(settings.sort_dry_run)
        self.assertEqual(settings.album_flush_delay_seconds, 5.0)
        self.assertEqual(settings.album_max_wait_seconds, 30.0)
        self.assertEqual(settings.requester_bot_ids, frozenset())
        self.assertEqual(settings.telegram_retry_attempts, 3)
        self.assertEqual(settings.telegram_bootstrap_retries, -1)
        self.assertFalse(settings.telegram_drop_pending_updates)
        self.assertFalse(settings.telegram_startup_checkin_enabled)
        self.assertFalse(settings.health_server_enabled)
        self.assertTrue(settings.sanity_check_enabled)
        self.assertFalse(settings.source_activity_check_enabled)
        self.assertEqual(settings.database_backend, "sqlite")

    def test_rejects_blank_bot_token(self) -> None:
        values = _values()
        values["BOT_TOKEN"] = " "
        values["ROUTES_JSON"] = "[]"

        with self.assertRaises(ValidationError):
            Settings(**values)

    def test_legacy_collector_and_routes_are_optional(self) -> None:
        values = _values()
        values.pop("COLLECTOR_API_KEY")
        values.pop("COLLECTOR_DATABASE")
        values["ROUTES_JSON"] = "[]"

        settings = Settings(**values)

        self.assertEqual(settings.routes, [])

    def test_rejects_duplicate_route_destinations(self) -> None:
        values = _values()
        values["ROUTES_JSON"] = (
            '[{"name":"One","thread_id":9,"keywords":["cr"]},'
            '{"name":"Two","thread_id":9,"keywords":["fc"]}]'
        )

        with self.assertRaisesRegex(ValidationError, "thread IDs must be unique"):
            Settings(**values)

    def test_parses_operator_and_request_topic_ids(self) -> None:
        values = _values()
        values["ADMIN_USER_IDS"] = "10, 20"
        values["REQUEST_TOPIC_IDS"] = "30,40"
        values["REQUESTER_BOT_IDS"] = "50,60"

        settings = Settings(**values)

        self.assertEqual(settings.admin_user_ids, frozenset({10, 20}))
        self.assertEqual(settings.request_topic_ids, frozenset({30, 40}))
        self.assertEqual(settings.requester_bot_ids, frozenset({50, 60}))

    def test_blank_id_lists_from_dotenv_are_empty(self) -> None:
        values = _values()
        values["REQUEST_TOPIC_IDS"] = ""
        values["REQUESTER_BOT_IDS"] = ""
        settings = Settings(**values)

        self.assertEqual(settings.request_topic_ids, frozenset())
        self.assertEqual(settings.requester_bot_ids, frozenset())
        self.assertEqual(settings.effective_request_chat_id, -200)

    def test_request_chat_can_differ_from_archive_chat(self) -> None:
        values = _values()
        values["REQUEST_CHAT_ID"] = -300

        settings = Settings(**values)

        self.assertEqual(settings.effective_request_chat_id, -300)

    def test_rejects_request_default_above_maximum(self) -> None:
        values = _values()
        values["DEFAULT_REQUEST_LIMIT"] = 101
        values["MAX_REQUEST_LIMIT"] = 100

        with self.assertRaisesRegex(ValidationError, "must not exceed"):
            Settings(**values)

    def test_parses_integration_clients_and_rejects_short_secrets(self) -> None:
        values = _values()
        values["INTEGRATION_CLIENTS_JSON"] = (
            '[{"client_id":"program","secret":"1234567890abcdef",'
            '"scopes":["search"],"requests_per_minute":12}]'
        )

        settings = Settings(**values)

        self.assertEqual(settings.integration_clients[0].client_id, "program")
        self.assertEqual(settings.integration_clients[0].scopes, frozenset({"search"}))

        values["INTEGRATION_CLIENTS_JSON"] = (
            '[{"client_id":"program","secret":"short","scopes":["search"]}]'
        )
        with self.assertRaisesRegex(ValidationError, "16"):
            Settings(**values)


    def test_backup_time_normalizes_and_exposes_utc_time(self) -> None:
        values = _values()
        values["BACKUP_TIME"] = "3:5"

        settings = Settings(**values)

        self.assertEqual(settings.backup_time, "03:05")
        self.assertEqual(settings.backup_retention_count, 14)
        self.assertTrue(settings.backup_daily_enabled)
        self.assertEqual(settings.backup_time_utc.hour, 3)
        self.assertEqual(settings.backup_time_utc.minute, 5)
        self.assertIsNotNone(settings.backup_time_utc.tzinfo)

    def test_rejects_invalid_backup_time(self) -> None:
        values = _values()
        values["BACKUP_TIME"] = "25:00"

        with self.assertRaisesRegex(ValidationError, "BACKUP_TIME"):
            Settings(**values)

    def test_webhook_mode_requires_public_url(self) -> None:
        values = _values()
        values["RUN_MODE"] = "webhook"

        with self.assertRaisesRegex(ValidationError, "WEBHOOK_URL"):
            Settings(**values)

        values["WEBHOOK_URL"] = "https://miki.example.com/telegram/webhook"
        settings = Settings(**values)

        self.assertEqual(settings.run_mode, "webhook")
        self.assertEqual(settings.webhook_path, "/telegram/webhook")

    def test_webhook_port_can_use_host_port_env(self) -> None:
        values = _values()
        values["PORT"] = "9000"

        settings = Settings(**values)

        self.assertEqual(settings.webhook_port, 9000)

    def test_webhook_security_and_bootstrap_settings_are_configurable(self) -> None:
        values = _values()
        values["TELEGRAM_BOOTSTRAP_RETRIES"] = "5"
        values["TELEGRAM_DROP_PENDING_UPDATES"] = "true"
        values["TELEGRAM_STARTUP_CHECKIN_ENABLED"] = "true"
        values["TELEGRAM_NOTIFICATION_CHAT_IDS"] = "1,2"
        values["WEBHOOK_SECRET_TOKEN"] = "secret-token"
        values["WEBHOOK_MAX_CONNECTIONS"] = "20"
        values["HEALTH_SERVER_ENABLED"] = "true"
        values["HEALTH_PORT"] = "9090"
        values["SOURCE_ACTIVITY_CHECK_ENABLED"] = "true"
        values["ERROR_REPORTING_DSN"] = "https://public@example.invalid/1"

        settings = Settings(**values)

        self.assertEqual(settings.telegram_bootstrap_retries, 5)
        self.assertTrue(settings.telegram_drop_pending_updates)
        self.assertTrue(settings.telegram_startup_checkin_enabled)
        self.assertEqual(settings.telegram_notification_chat_ids, frozenset({1, 2}))
        self.assertEqual(settings.webhook_secret_token, "secret-token")
        self.assertEqual(settings.webhook_max_connections, 20)
        self.assertTrue(settings.health_server_enabled)
        self.assertEqual(settings.health_port, 9090)
        self.assertTrue(settings.source_activity_check_enabled)
        self.assertEqual(settings.error_reporting_dsn, "https://public@example.invalid/1")

    def test_rejects_future_database_backend_until_supported(self) -> None:
        values = _values()
        values["DATABASE_BACKEND"] = "postgres"

        with self.assertRaisesRegex(ValidationError, "only sqlite"):
            Settings(**values)

    def test_rejects_invalid_runtime_mode(self) -> None:
        values = _values()
        values["RUN_MODE"] = "cron"

        with self.assertRaisesRegex(ValidationError, "polling or webhook"):
            Settings(**values)


if __name__ == "__main__":
    unittest.main()
