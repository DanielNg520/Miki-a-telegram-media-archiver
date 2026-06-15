from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from miki_sorter_bot.management import ManagementCommands
from miki_sorter_bot.repositories import SqliteRepositories


def _settings(*admins: int) -> SimpleNamespace:
    return SimpleNamespace(admin_user_ids=frozenset(admins))


def _update(
    text: str,
    *,
    user_id: int = 10,
    chat_id: int = -100,
    thread_id: int | None = 7,
    chat_type: str = "supergroup",
    **status: object,
) -> SimpleNamespace:
    message = SimpleNamespace(
        text=text,
        message_thread_id=thread_id,
        reply_text=AsyncMock(),
        forum_topic_closed=status.get("forum_topic_closed"),
        forum_topic_reopened=status.get("forum_topic_reopened"),
        forum_topic_edited=status.get("forum_topic_edited"),
    )
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=SimpleNamespace(id=user_id),
    )


def test_admin_registers_current_live_forum_topic(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/topic_register Japan")
    context = SimpleNamespace(
        bot=SimpleNamespace(
            get_chat=AsyncMock(return_value=SimpleNamespace(is_forum=True)),
            get_me=AsyncMock(return_value=SimpleNamespace(id=50)),
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="administrator")
            ),
        )
    )

    asyncio.run(commands.topic_register(update, context))

    assert repositories.get(-100, 7).name == "Japan"
    update.effective_message.reply_text.assert_awaited_once_with(
        "Registered topic Japan with ID 7."
    )


def test_unauthorized_user_cannot_change_routes(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/topic_register Japan", user_id=99)
    context = SimpleNamespace(
        bot=SimpleNamespace(
            get_chat=AsyncMock(),
            get_me=AsyncMock(),
            get_chat_member=AsyncMock(),
        )
    )

    asyncio.run(commands.topic_register(update, context))

    assert repositories.get(-100, 7) is None
    context.bot.get_chat.assert_not_awaited()


def test_delegated_manager_can_add_phrase_mapping(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-100, 7, "Japan")
    repositories.grant_route_manager(-100, 20, 10)
    commands = ManagementCommands(_settings(10), repositories)
    update = _update('/keyword_add 7 "New York"', user_id=20)

    asyncio.run(commands.keyword_add(update, SimpleNamespace()))

    mappings = repositories.list_mappings(-100, thread_id=7)
    assert [(item.kind, item.normalized_value) for item in mappings] == [
        ("phrase", "new york")
    ]


def test_manager_grant_is_universal_and_can_delegate(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    # Granted in one chat...
    repositories.grant_route_manager(-100, 20, 10)
    commands = ManagementCommands(_settings(10), repositories)

    # ...the manager has full admin powers in a *different* chat, no restart.
    update = _update("/manager_add 30", user_id=20, chat_id=-999)
    asyncio.run(commands.manager_add(update, SimpleNamespace()))

    assert repositories.is_manager(30)
    assert repositories.is_manager(20)


def test_non_manager_cannot_delegate(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/manager_add 30", user_id=99)

    asyncio.run(commands.manager_add(update, SimpleNamespace()))

    assert not repositories.is_manager(30)


def test_topic_status_updates_change_registered_topic_state(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-100, 7, "Japan")
    commands = ManagementCommands(_settings(10), repositories)

    closed = _update("/status", forum_topic_closed=object())
    asyncio.run(commands.track_topic_status(closed, SimpleNamespace()))
    assert repositories.get(-100, 7).is_active is False

    reopened = _update("/status", forum_topic_reopened=object())
    asyncio.run(commands.track_topic_status(reopened, SimpleNamespace()))
    assert repositories.get(-100, 7).is_active is True


def test_admin_can_view_audit_log(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.add_audit_event(
        actor_type="system",
        actor_id="miki",
        action="test.event",
        outcome="success",
    )
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/audit_log 10")

    asyncio.run(commands.audit_log(update, SimpleNamespace()))

    assert "test.event" in update.effective_message.reply_text.await_args.args[0]


def test_admin_can_run_human_readable_doctor(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "hashtag", "JAV", 10)
    settings = SimpleNamespace(
        admin_user_ids=frozenset({10}),
        source_chat_id=-100,
        source_thread_id=5,
        archive_chat_id=-200,
        run_mode="polling",
        webhook_url="",
        webhook_path="/telegram/webhook",
        webhook_listen="0.0.0.0",
        webhook_port=8080,
        request_topic_ids=frozenset({50}),
    )
    commands = ManagementCommands(settings, repositories)
    update = _update("/doctor")

    asyncio.run(commands.doctor(update, SimpleNamespace()))

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "Miki checkup" in reply
    assert "Result: Miki is ready" in reply
