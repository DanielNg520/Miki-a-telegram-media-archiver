from __future__ import annotations

from miki_sorter_bot.error_reporting import ErrorReporter, configure_error_reporter


class _Client:
    def __init__(self) -> None:
        self.errors: list[BaseException] = []

    def capture_exception(self, error: BaseException) -> None:
        self.errors.append(error)


def test_error_reporter_is_noop_without_client() -> None:
    reporter = configure_error_reporter(dsn="", environment="test")

    assert not reporter.enabled
    reporter.capture_exception(RuntimeError("ignored"))


def test_error_reporter_delegates_to_client() -> None:
    client = _Client()
    reporter = ErrorReporter(client)
    error = RuntimeError("boom")

    reporter.capture_exception(error)

    assert client.errors == [error]
