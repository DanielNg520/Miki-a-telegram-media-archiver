from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from miki_sorter_bot.burner import process_pending_commands
from miki_sorter_bot.burner_reporting import BurnerResultReporter
from miki_sorter_bot.config import Settings
from miki_sorter_bot.management import ManagementCommands
from miki_sorter_bot.repositories import SqliteRepositories


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "BOT_TOKEN": "token",
        "SOURCE_CHAT_ID": -100,
        "SOURCE_THREAD_ID": 5,
        "ARCHIVE_CHAT_ID": -200,
        "BURNER_ENABLED": True,
        "TELETHON_API_ID": 1,
        "TELETHON_API_HASH": "hash",
        "TELETHON_SESSION": "session",
        "BURNER_POLL_INTERVAL_SECONDS": 30,
        "BURNER_OPERATOR_USER_IDS": "55",
        "ADMIN_USER_IDS": "10",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _update(text: str, *, user_id: int = 55, chat_id: int = -100, thread_id: int = 7):
    message = SimpleNamespace(
        text=text,
        message_id=999,
        message_thread_id=thread_id,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id, type="supergroup"),
        effective_user=SimpleNamespace(id=user_id),
    )


def _heartbeat(repositories: SqliteRepositories) -> None:
    repositories.record_burner_heartbeat(
        session_valid=True, capabilities={"heartbeat": True}, version="0"
    )


def test_non_operator_is_rejected(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    commands = ManagementCommands(_settings(), repositories)
    update = _update("/burner status", user_id=999)

    asyncio.run(commands.burner(update, None))

    update.effective_message.reply_text.assert_awaited_once()
    assert "not authorized" in update.effective_message.reply_text.await_args.args[0]


def test_status_reports_availability(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _heartbeat(repositories)
    commands = ManagementCommands(_settings(), repositories)
    update = _update("/burner status")

    asyncio.run(commands.burner(update, None))

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "available" in reply
    assert repositories.list_audit_events()[0]["action"] == "burner.status"


def test_admin_is_also_authorized(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _heartbeat(repositories)
    commands = ManagementCommands(_settings(), repositories)
    update = _update("/burner status", user_id=10)  # admin, not in operator list

    asyncio.run(commands.burner(update, None))

    assert "available" in update.effective_message.reply_text.await_args.args[0]


def test_enqueue_fails_fast_when_unavailable(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    # No heartbeat -> unavailable.
    commands = ManagementCommands(_settings(), repositories)
    update = _update("/burner noop hello")

    asyncio.run(commands.burner(update, None))

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "unavailable" in reply
    assert repositories.list_pending_burner_commands() == []
    assert repositories.list_audit_events()[0]["outcome"] == "denied"


def test_enqueue_when_available(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _heartbeat(repositories)
    commands = ManagementCommands(_settings(), repositories)
    update = _update("/burner noop hello world")

    asyncio.run(commands.burner(update, None))

    pending = repositories.list_pending_burner_commands()
    assert len(pending) == 1
    assert pending[0].kind == "noop"
    assert pending[0].payload["echo"] == "hello world"
    assert pending[0].payload["chat_id"] == -100
    assert repositories.list_audit_events()[0]["action"] == "burner.enqueue"


def test_unknown_subcommand(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _heartbeat(repositories)
    commands = ManagementCommands(_settings(), repositories)
    update = _update("/burner explode")

    asyncio.run(commands.burner(update, None))

    assert "Unknown burner command" in update.effective_message.reply_text.await_args.args[0]
    assert repositories.list_pending_burner_commands() == []


def test_round_trip_enqueue_execute_report(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _heartbeat(repositories)
    settings = _settings()
    commands = ManagementCommands(settings, repositories)

    # Bot enqueues.
    asyncio.run(commands.burner(_update("/burner noop ping"), None))
    # Burner executes.
    process_pending_commands(repositories, settings)
    # Bot reports back into the originating chat.
    bot = SimpleNamespace(send_message=AsyncMock())
    sent = asyncio.run(BurnerResultReporter(repositories).run_once(bot))

    assert sent == 1
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == -100
    assert kwargs["message_thread_id"] == 7
    assert "completed" in kwargs["text"]
    # Reported exactly once.
    assert repositories.list_unreported_burner_results() == []
    assert asyncio.run(BurnerResultReporter(repositories).run_once(bot)) == 0


def _backdate_updated_at(connection, command_id: int, expression: str) -> None:
    connection.execute(
        f"UPDATE burner_commands SET updated_at = datetime('now', '{expression}') WHERE id = ?",
        (command_id,),
    )
    connection.commit()


def test_reaper_fails_only_stale_running_commands(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    fresh = repositories.enqueue_burner_command("noop", "fresh", {}, 55)
    stale = repositories.enqueue_burner_command("noop", "stale", {}, 55)
    repositories.claim_burner_command(fresh.id)
    repositories.claim_burner_command(stale.id)
    _backdate_updated_at(database_connection, stale.id, "-1 hour")

    reclaimed = repositories.fail_stale_running_burner_commands(1800)

    assert reclaimed == 1
    assert repositories.get_burner_command(stale.id).status == "failed"
    assert "did not finish" in repositories.get_burner_command(stale.id).last_error
    assert repositories.get_burner_command(fresh.id).status == "running"


def test_reporter_reclaims_and_reports_stale_command(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    stuck = repositories.enqueue_burner_command("backup_now", "stuck", {"chat_id": -100}, 55)
    repositories.claim_burner_command(stuck.id)
    _backdate_updated_at(database_connection, stuck.id, "-1 hour")

    bot = SimpleNamespace(send_message=AsyncMock())
    sent = asyncio.run(
        BurnerResultReporter(repositories, stale_after_seconds=60).run_once(bot)
    )

    assert sent == 1
    assert "failed" in bot.send_message.await_args.kwargs["text"]
    assert repositories.get_burner_command(stuck.id).status == "failed"
    assert repositories.list_unreported_burner_results() == []


def test_reporter_marks_unreachable_chat_to_avoid_loop(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    repositories.enqueue_burner_command("noop", "k1", {"chat_id": -1}, 55)
    process_pending_commands(repositories, settings)

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("chat gone")))
    asyncio.run(BurnerResultReporter(repositories).run_once(bot))

    # Even though sending failed, it is marked reported so it never loops.
    assert repositories.list_unreported_burner_results() == []
