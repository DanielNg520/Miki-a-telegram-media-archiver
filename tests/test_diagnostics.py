from __future__ import annotations

from types import SimpleNamespace

from miki_sorter_bot.diagnostics import run_diagnostics
from miki_sorter_bot.repositories import SqliteRepositories


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "source_chat_id": -100,
        "source_thread_id": 5,
        "archive_chat_id": -200,
        "run_mode": "polling",
        "webhook_url": "",
        "webhook_path": "/telegram/webhook",
        "webhook_listen": "0.0.0.0",
        "webhook_port": 8080,
        "request_topic_ids": frozenset(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_diagnostics_reports_missing_archive_setup(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)

    report = run_diagnostics(_settings(), repositories)

    assert report.has_errors
    assert "No active topics" in report.format()


def test_diagnostics_accepts_registered_routes(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "hashtag", "JAV", 1)

    report = run_diagnostics(_settings(request_topic_ids=frozenset({50})), repositories)

    assert not report.has_errors
    assert "1 active topic" in report.format()
    assert "1 route mapping" in report.format()


def test_diagnostics_warns_on_webhook_path_mismatch(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "hashtag", "JAV", 1)

    report = run_diagnostics(
        _settings(
            run_mode="webhook",
            webhook_url="https://miki.example.com/wrong",
            request_topic_ids=frozenset({50}),
        ),
        repositories,
    )

    assert not report.has_errors
    assert "[CHECK] runtime" in report.format()
