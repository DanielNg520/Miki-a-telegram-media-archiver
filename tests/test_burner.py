from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from miki_sorter_bot.burner import (
    BURNER_CAPABILITIES,
    BURNER_VERSION,
    BurnerCapability,
    process_pending_commands,
    run_burner,
    write_heartbeat,
)
from miki_sorter_bot.config import Settings
from miki_sorter_bot.repositories import SqliteRepositories


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "BOT_TOKEN": "token",
        "SOURCE_CHAT_ID": -100,
        "SOURCE_THREAD_ID": 5,
        "ARCHIVE_CHAT_ID": -200,
        "BURNER_ENABLED": True,
        "TELETHON_API_ID": 12345,
        "TELETHON_API_HASH": "hash",
        "TELETHON_SESSION": "session-string",
        "BURNER_POLL_INTERVAL_SECONDS": 30,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_burner_configured_requires_all_credentials() -> None:
    assert _settings().burner_configured is True
    assert _settings(BURNER_ENABLED=False).burner_configured is False
    assert _settings(TELETHON_SESSION="").burner_configured is False
    assert _settings(TELETHON_API_ID=None).burner_configured is False


def test_operator_or_admin_ids_union() -> None:
    settings = _settings(BURNER_OPERATOR_USER_IDS="7,8", ADMIN_USER_IDS="8,9")
    assert settings.burner_operator_or_admin_ids == frozenset({7, 8, 9})


def test_availability_not_configured(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    availability = BurnerCapability(_settings(BURNER_ENABLED=False), repositories).evaluate()

    assert availability.configured is False
    assert availability.available is False
    assert availability.summary() == "burner: not configured"


def test_availability_no_heartbeat(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    availability = BurnerCapability(_settings(), repositories).evaluate()

    assert availability.configured is True
    assert availability.available is False
    assert availability.reason == "no heartbeat recorded"


def test_availability_healthy_after_heartbeat(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    write_heartbeat(repositories, settings)

    availability = BurnerCapability(settings, repositories).evaluate()

    assert availability.available is True
    assert availability.session_valid is True
    assert availability.version == BURNER_VERSION
    assert availability.summary().startswith("burner: available (last seen")


def test_availability_invalid_session(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    repositories.record_burner_heartbeat(
        session_valid=False, capabilities=BURNER_CAPABILITIES, version=BURNER_VERSION
    )

    availability = BurnerCapability(settings, repositories).evaluate()

    assert availability.available is False
    assert availability.reason == "session not valid"


def test_availability_stale_heartbeat(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings(BURNER_POLL_INTERVAL_SECONDS=30)
    write_heartbeat(repositories, settings)

    # 5 minutes later, well past the 60s freshness window.
    later = datetime.now(UTC) + timedelta(minutes=5)
    availability = BurnerCapability(settings, repositories).evaluate(now=later)

    assert availability.available is False
    assert "stale" in availability.reason


def test_run_burner_refuses_when_unconfigured(tmp_path) -> None:
    settings = _settings(BURNER_ENABLED=False, DATABASE_PATH=str(tmp_path / "db.sqlite3"))
    with pytest.raises(SystemExit):
        run_burner(settings, stop_event=threading.Event())


def test_run_burner_writes_heartbeat(tmp_path) -> None:
    from miki_sorter_bot.storage import Storage

    db_path = tmp_path / "db.sqlite3"
    settings = _settings(DATABASE_PATH=str(db_path))

    run_burner(
        settings,
        stop_event=threading.Event(),
        max_iterations=1,
        session_validator=lambda _s: True,
    )

    storage = Storage(db_path)
    try:
        status = storage.open().get_burner_status()
    finally:
        storage.close()
    assert status is not None
    assert status.session_valid is True
    assert status.capabilities == BURNER_CAPABILITIES


def test_run_burner_records_validation_failure(tmp_path) -> None:
    from miki_sorter_bot.storage import Storage

    db_path = tmp_path / "db.sqlite3"
    settings = _settings(DATABASE_PATH=str(db_path))

    def _boom(_s: object) -> bool:
        raise RuntimeError("session revoked")

    run_burner(
        settings,
        stop_event=threading.Event(),
        max_iterations=1,
        session_validator=_boom,
    )

    storage = Storage(db_path)
    try:
        status = storage.open().get_burner_status()
    finally:
        storage.close()
    assert status is not None
    assert status.session_valid is False
    assert status.last_error == "session revoked"


def test_run_burner_session_unauthorized(tmp_path) -> None:
    from miki_sorter_bot.storage import Storage

    db_path = tmp_path / "db.sqlite3"
    settings = _settings(DATABASE_PATH=str(db_path))

    run_burner(
        settings,
        stop_event=threading.Event(),
        max_iterations=1,
        session_validator=lambda _s: False,
    )

    storage = Storage(db_path)
    try:
        status = storage.open().get_burner_status()
    finally:
        storage.close()
    assert status is not None
    assert status.session_valid is False
    assert status.last_error == "session not authorized"


def _enqueue(repositories, kind="noop", key=None, payload=None):
    return repositories.enqueue_burner_command(
        kind, key or f"k-{kind}", payload or {"echo": "hi"}, 10
    )


def test_enqueue_is_idempotent(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    first = _enqueue(repositories, key="same")
    second = _enqueue(repositories, key="same", payload={"echo": "other"})
    assert first.id == second.id
    assert len(repositories.list_pending_burner_commands()) == 1


def test_process_noop_command_completes(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    record = _enqueue(repositories, kind="noop", payload={"echo": "ping"})

    assert process_pending_commands(repositories, _settings()) == 1

    done = repositories.get_burner_command(record.id)
    assert done.status == "completed"
    assert done.result == {"message": "noop ok", "echo": "ping"}


def test_process_status_command_returns_capabilities(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    record = _enqueue(repositories, kind="status")

    process_pending_commands(repositories, _settings())

    done = repositories.get_burner_command(record.id)
    assert done.status == "completed"
    assert done.result["version"] == BURNER_VERSION
    assert done.result["capabilities"] == BURNER_CAPABILITIES


def test_claim_prevents_double_processing(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    record = _enqueue(repositories, kind="noop")
    assert repositories.claim_burner_command(record.id) is True
    # Already running: a second claim must fail.
    assert repositories.claim_burner_command(record.id) is False
    # And it is no longer pending.
    assert repositories.list_pending_burner_commands() == []


def test_unreported_results_lifecycle(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    record = _enqueue(repositories, kind="noop")
    process_pending_commands(repositories, _settings())

    unreported = repositories.list_unreported_burner_results()
    assert [c.id for c in unreported] == [record.id]

    repositories.mark_burner_command_reported(record.id)
    assert repositories.list_unreported_burner_results() == []
