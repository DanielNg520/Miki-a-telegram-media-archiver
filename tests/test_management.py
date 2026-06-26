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
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")),
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
    assert [(item.kind, item.normalized_value) for item in mappings] == [("phrase", "new york")]


def test_manager_can_add_multiple_keyword_and_phrase_mappings(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-100, 7, "Japan")
    commands = ManagementCommands(_settings(10), repositories)
    update = _update('/keyword_add 7 Tokyo, RX7, "New York"')

    asyncio.run(commands.keyword_add(update, SimpleNamespace()))

    mappings = repositories.list_mappings(-100, thread_id=7)
    assert [(item.kind, item.normalized_value) for item in mappings] == [
        ("keyword", "rx7"),
        ("keyword", "tokyo"),
        ("phrase", "new york"),
    ]
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "Added 3 route(s)" in reply


def test_manager_can_add_multiple_hashtags_with_optional_prefix(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-100, 7, "Japan")
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/hashtag_add 7 #japan, tokyo, #rx7")

    asyncio.run(commands.hashtag_add(update, SimpleNamespace()))

    mappings = repositories.list_mappings(-100, thread_id=7)
    assert [(item.kind, item.normalized_value) for item in mappings] == [
        ("hashtag", "japan"),
        ("hashtag", "rx7"),
        ("hashtag", "tokyo"),
    ]


def test_manager_can_add_space_separated_unicode_hashtags(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-100, 46, "Japan")
    commands = ManagementCommands(_settings(10), repositories)
    update = _update(
        "/hashtag_add 46 #日本 #男男  #去马赛克 #demosaiced #AI字幕  #无码 #AI画质增强"
    )

    asyncio.run(commands.hashtag_add(update, SimpleNamespace()))

    mappings = repositories.list_mappings(-100, thread_id=46)
    assert [item.normalized_value for item in mappings] == [
        "ai字幕",
        "ai画质增强",
        "demosaiced",
        "去马赛克",
        "无码",
        "日本",
        "男男",
    ]
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "Added 7 route(s)" in reply
    assert "Skipped" not in reply


def test_bulk_add_reports_invalid_values_without_blocking_valid_ones(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-100, 7, "Japan")
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/hashtag_add 7 japan, bad tag, tokyo")

    asyncio.run(commands.hashtag_add(update, SimpleNamespace()))

    mappings = repositories.list_mappings(-100, thread_id=7)
    assert [item.normalized_value for item in mappings] == ["japan", "tokyo"]
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "Added 2 route(s)" in reply
    assert "Skipped" in reply
    assert "bad tag" in reply


def test_limited_admin_cannot_delegate(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    # A limited admin (granted via /manager_add) is NOT a super admin...
    repositories.grant_route_manager(-100, 20, 10)
    commands = ManagementCommands(_settings(10), repositories)

    # ...so they cannot grant other admins; that is super-admin-only.
    update = _update("/manager_add 30", user_id=20, chat_id=-999)
    asyncio.run(commands.manager_add(update, SimpleNamespace()))

    assert not repositories.is_manager(30)
    update.effective_message.reply_text.assert_awaited_once_with(
        "Only a Miki super administrator can do that."
    )


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


def test_dead_letter_retry_immediately_invokes_recovery_worker(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    job = repositories.enqueue("sort", "sort:retry", {})
    repositories.update_job(job.id, "failed", error="boom")
    dead_letter_id = repositories.add_dead_letter(
        job.id,
        "sort_copy",
        {},
        "unexpected",
        "boom",
    )
    recovery = SimpleNamespace(resume_job=AsyncMock(return_value=True))
    commands = ManagementCommands(_settings(10), repositories, recovery=recovery)
    update = _update(f"/dead_letter_retry {dead_letter_id}")
    context = SimpleNamespace()

    asyncio.run(commands.dead_letter_retry(update, context))

    recovery.resume_job.assert_awaited_once_with(job.id, context)
    assert "requeued" in update.effective_message.reply_text.await_args.args[0]


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


# --- chat-configurable runtime settings (/config, /set, /reset) ------------
def test_admin_sets_runtime_setting_from_chat(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/set lookback_ttl_seconds 300")

    asyncio.run(commands.config_set(update, SimpleNamespace()))

    assert repositories.get_runtime_setting("lookback_ttl_seconds") == "300"
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "lookback_ttl_seconds = 300" in reply


def test_set_rejects_out_of_range_value(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/set lookback_ttl_seconds 999999")

    asyncio.run(commands.config_set(update, SimpleNamespace()))

    assert repositories.get_runtime_setting("lookback_ttl_seconds") is None
    assert "Invalid value" in update.effective_message.reply_text.await_args.args[0]


def test_set_unknown_key_is_rejected(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/set bot_token hunter2")

    asyncio.run(commands.config_set(update, SimpleNamespace()))

    assert repositories.get_runtime_setting("bot_token") is None
    assert "Unknown setting" in update.effective_message.reply_text.await_args.args[0]


def test_non_admin_cannot_set_runtime_setting(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/set lookback_ttl_seconds 300", user_id=99)

    asyncio.run(commands.config_set(update, SimpleNamespace()))

    assert repositories.get_runtime_setting("lookback_ttl_seconds") is None


def test_config_show_lists_registered_settings(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.set_runtime_setting("lookback_capacity", "9")
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/config")

    asyncio.run(commands.config_show(update, SimpleNamespace()))

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "album_flush_delay_seconds" in reply
    assert "[lookback]" in reply
    assert "* lookback_capacity = 9" in reply  # marked as overridden


def test_reset_clears_runtime_override(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.set_runtime_setting("sort_dry_run", "true")
    commands = ManagementCommands(_settings(10), repositories)
    update = _update("/reset sort_dry_run")

    asyncio.run(commands.config_reset(update, SimpleNamespace()))

    assert repositories.get_runtime_setting("sort_dry_run") is None
    assert "reset to default" in update.effective_message.reply_text.await_args.args[0]
