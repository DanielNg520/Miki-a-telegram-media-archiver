from __future__ import annotations

import contextvars
import json
import logging
from datetime import UTC, datetime
from typing import Any

CORRELATION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id",
    default="-",
)
STANDARD_LOG_RECORD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
)
REDACTED_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "bot_token",
        "caption",
        "collector_api_key",
        "message_text",
        "password",
        "text",
        "token",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", CORRELATION_ID.get()),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in STANDARD_LOG_RECORD_FIELDS or key in payload or key.startswith("_"):
                continue
            payload[key] = "[REDACTED]" if key.casefold() in REDACTED_KEYS else value
        return json.dumps(payload, default=str, ensure_ascii=True, separators=(",", ":"))


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = CORRELATION_ID.get()
        return True


def configure_logging(level: str = "INFO", log_format: str = "json") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(CorrelationFilter())
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s "
                "[correlation_id=%(correlation_id)s]: %(message)s"
            )
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def set_correlation_id(value: str) -> contextvars.Token[str]:
    return CORRELATION_ID.set(value)


def reset_correlation_id(token: contextvars.Token[str]) -> None:
    CORRELATION_ID.reset(token)
