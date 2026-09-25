from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


class ErrorReporter:
    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def capture_exception(self, error: BaseException) -> None:
        if self._client is None:
            return
        try:
            self._client.capture_exception(error)
        except Exception:
            LOGGER.exception("Error reporter failed while capturing exception")


def configure_error_reporter(*, dsn: str, environment: str) -> ErrorReporter:
    if not dsn.strip():
        return ErrorReporter()
    try:
        import sentry_sdk  # type: ignore[import-not-found]
    except ImportError:
        LOGGER.warning(
            "ERROR_REPORTING_DSN is set but sentry-sdk is not installed; "
            "continuing without external error reporting."
        )
        return ErrorReporter()
    sentry_sdk.init(dsn=dsn, environment=environment)
    LOGGER.info("External error reporting enabled")
    return ErrorReporter(sentry_sdk)
