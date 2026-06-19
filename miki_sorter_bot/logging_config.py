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
EXTRA_KEYS_TO_SHOW = (
    "count",
    "jobs_by_kind",
    "webhook_url",
    "listen",
    "port",
    "path",
    "time_utc",
    "retention",
    "backup",
    "pruned",
    "chat_id",
    "thread_id",
    "message_id",
    "job_id",
    "post_id",
    "error_category",
    "source_chat_id",
    "media_group_id",
    "message_count",
    "caption_count",
    "destination_thread_id",
    "flush_delay_seconds",
    "max_wait_seconds",
)
NOISY_LOGGERS = (
    "apscheduler",
    "httpcore",
    "httpx",
    "telegram",
    "telegram.ext",
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


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        parts = [timestamp, record.levelname.ljust(7), record.getMessage()]
        details = _public_details(record)
        if details:
            parts.append(details)
        correlation_id = getattr(record, "correlation_id", CORRELATION_ID.get())
        if correlation_id and correlation_id != "-":
            parts.append(f"id={correlation_id}")
        line = " | ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


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
    elif log_format == "console":
        handler.setFormatter(ConsoleFormatter())
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
    for logger_name in NOISY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def _public_details(record: logging.LogRecord) -> str:
    pairs = []
    for key in EXTRA_KEYS_TO_SHOW:
        if not hasattr(record, key):
            continue
        value = getattr(record, key)
        if value is None:
            continue
        pairs.append(f"{key}={_safe_value(key, value)}")
    return " ".join(pairs)


def _safe_value(key: str, value: object) -> str:
    if key.casefold() in REDACTED_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return ",".join(f"{item_key}:{item_value}" for item_key, item_value in value.items())
    return str(value)


def set_correlation_id(value: str) -> contextvars.Token[str]:
    return CORRELATION_ID.set(value)


def reset_correlation_id(token: contextvars.Token[str]) -> None:
    CORRELATION_ID.reset(token)
