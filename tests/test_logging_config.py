from __future__ import annotations

import json
import logging

from miki_sorter_bot.logging_config import (
    ConsoleFormatter,
    JsonFormatter,
    configure_logging,
    reset_correlation_id,
    set_correlation_id,
)


def test_json_formatter_adds_correlation_id_and_redacts_sensitive_fields() -> None:
    token = set_correlation_id("request-123")
    try:
        record = logging.LogRecord(
            "miki.test",
            logging.INFO,
            __file__,
            1,
            "Processed message %s",
            (42,),
            None,
        )
        record.bot_token = "secret"
        record.topic_id = 7

        payload = json.loads(JsonFormatter().format(record))

        assert payload["message"] == "Processed message 42"
        assert payload["correlation_id"] == "request-123"
        assert payload["bot_token"] == "[REDACTED]"
        assert payload["topic_id"] == 7
    finally:
        reset_correlation_id(token)


def test_console_formatter_is_readable_and_compact() -> None:
    token = set_correlation_id("telegram-123")
    try:
        record = logging.LogRecord(
            "miki.test",
            logging.INFO,
            __file__,
            1,
            "Miki sorter is running in polling mode.",
            (),
            None,
        )
        record.chat_id = -100
        record.bot_token = "secret"

        line = ConsoleFormatter().format(record)

        assert "INFO" in line
        assert "Miki sorter is running in polling mode." in line
        assert "chat_id=-100" in line
        assert "bot_token" not in line
        assert "telegram-123" in line
        assert not line.startswith("{")
    finally:
        reset_correlation_id(token)


def test_configure_logging_quiets_noisy_third_party_loggers() -> None:
    configure_logging("INFO", "console")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("telegram.ext").level == logging.WARNING
