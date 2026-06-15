from __future__ import annotations

import json
import logging

from miki_sorter_bot.logging_config import (
    JsonFormatter,
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
