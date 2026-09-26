import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

from telegram.error import BadRequest
from miki_sorter_bot.diagnostics import _source_activity_checks
from miki_sorter_bot.main import _add_management_handlers, _schedule_rotation
from miki_sorter_bot.management import ManagementCommands
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.rotation import RotationService
from miki_sorter_bot.settings_registry import LiveSettings
from miki_sorter_bot.topic_activity import TopicActivity

CHAT = -1001234567890


def _build(database_connection, *, wrap=None, notify=None, source_thread=5, **overrides):
    settings = SimpleNamespace(
        source_chat_id=-1001234567890,
        source_thread_id=source_thread,
        admin_user_ids=frozenset({1}),
        **overrides,
    )
    repositories = SqliteRepositories(database_connection)
    live = LiveSettings(settings, repositories)
    activity = TopicActivity(repositories, live)
    clock_state = [10_000_000.0]
    service_repositories = wrap(repositories) if wrap else repositories
    service = RotationService(
        settings,
        service_repositories,
        live,
        activity,
        clock=lambda: clock_state[0],
        notify=notify,
    )
    return service, repositories, live, activity, clock_state


def _context(new_thread_id=99):
    return SimpleNamespace(
        bot=SimpleNamespace(
            create_forum_topic=AsyncMock(
                return_value=SimpleNamespace(message_thread_id=new_thread_id)
            ),
            close_forum_topic=AsyncMock(),
            delete_forum_topic=AsyncMock(),
            send_message=AsyncMock(),
        )
    )


class _Flaky:
    def __init__(self, inner, fail_key=None, fail_method=None, times=1):
        self._inner = inner
        self._fail_key = fail_key
        self._fail_method = fail_method
        self._times = times
        self._calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def set_runtime_setting(self, key, value, updated_by_user_id=None):
        if key == self._fail_key and self._calls < self._times:
            self._calls += 1
            raise RuntimeError("boom")
        return self._inner.set_runtime_setting(key, value, updated_by_user_id)

    def mark_rotated_topic_deleted(self, *args, **kwargs):
        if self._fail_method == "mark":
            raise RuntimeError("boom")
        return self._inner.mark_rotated_topic_deleted(*args, **kwargs)


def test_retry_storm_is_throttled(database_connection):
    create = AsyncMock(side_effect=RuntimeError("boom"))
    notify = AsyncMock()
    service, repositories, live, activity, clock_state = _build(
        database_connection,
        rotate_enabled=True,
        rotate_media_threshold=1,
        notify=notify,
    )
    activity.record(5, None)
    ctx = _context()
    ctx.bot.create_forum_topic = create

    asyncio.run(service.tick(ctx))
    asyncio.run(service.tick(ctx))
    asyncio.run(service.tick(ctx))

    assert create.await_count == 1
    events = repositories.list_audit_events(50)
    assert sum(1 for e in events if e["outcome"] == "failed") == 1

    clock_state[0] += 299
    asyncio.run(service.tick(ctx))
    assert create.await_count == 1

    clock_state[0] += 2
    asyncio.run(service.tick(ctx))
    assert create.await_count == 2
    assert repositories.get_runtime_setting("rotation_retry").endswith(":2")
    assert notify.await_count <= 2


def test_backoff_cleared_on_success(database_connection):
    create = AsyncMock(
        side_effect=[
            RuntimeError("boom"),
            RuntimeError("boom"),
            SimpleNamespace(message_thread_id=99),
        ]
    )
    service, repositories, live, activity, clock_state = _build(
        database_connection,
        rotate_enabled=True,
        rotate_media_threshold=1,
    )
    activity.record(5, None)
    ctx = _context()
    ctx.bot.create_forum_topic = create

    asyncio.run(service.tick(ctx))
    clock_state[0] += 700
    asyncio.run(service.tick(ctx))
    clock_state[0] += 1300
    asyncio.run(service.tick(ctx))

    assert repositories.get_runtime_setting("rotation_retry") is None
    assert repositories.get_runtime_setting("rotation_pending") is None
    assert repositories.get_runtime_setting("source_thread_id") == "99"


def test_manual_rotate_bypasses_backoff(database_connection):
    create = AsyncMock(side_effect=[RuntimeError("boom"), SimpleNamespace(message_thread_id=99)])
    service, repositories, live, activity, clock_state = _build(
        database_connection,
        rotate_enabled=True,
        rotate_media_threshold=1,
    )
    activity.record(5, None)
    ctx = _context()
    ctx.bot.create_forum_topic = create

    asyncio.run(service.tick(ctx))
    result = asyncio.run(service.rotate(ctx, actor="1", reason="manual"))
    assert result.startswith("Rotated to")


def test_switch_failure_resumes_without_second_topic(database_connection):
    def wrap(repositories):
        return _Flaky(repositories, fail_key="source_thread_id")

    create = AsyncMock(return_value=SimpleNamespace(message_thread_id=99))
    service, repositories, live, activity, clock_state = _build(
        database_connection,
        wrap=wrap,
        rotate_enabled=True,
        rotate_media_threshold=1,
    )
    activity.record(5, None)
    ctx = _context()
    ctx.bot.create_forum_topic = create

    result = asyncio.run(service.rotate(ctx, actor="1", reason="manual"))
    assert "retry automatically" in result
    assert create.await_count == 1
    assert repositories.get_runtime_setting("rotation_pending") == "5:99:11"

    clock_state[0] += 301
    asyncio.run(service.tick(ctx))
    assert create.await_count == 1
    assert repositories.get_runtime_setting("source_thread_id") == "99"
    assert repositories.get_runtime_setting("rotation_pending") is None
    assert live.topic_cycle() == 11
    ctx.bot.close_forum_topic.assert_awaited_with(chat_id=CHAT, message_thread_id=5)
    assert repositories.is_rotated_topic(CHAT, 5)


def test_crash_leftover_pending_is_resumed(database_connection):
    service, repositories, live, activity, clock_state = _build(
        database_connection,
        rotate_enabled=False,
    )
    repositories.set_runtime_setting("rotation_pending", "5:99:11")
    ctx = _context()

    asyncio.run(service.tick(ctx))

    ctx.bot.create_forum_topic.assert_not_awaited()
    assert repositories.get_runtime_setting("source_thread_id") == "99"
    assert live.topic_cycle() == 11
    ctx.bot.close_forum_topic.assert_awaited_with(chat_id=CHAT, message_thread_id=5)
    assert repositories.is_rotated_topic(CHAT, 5)
    assert repositories.get_runtime_setting("rotation_pending") is None


def test_interval_needs_media(database_connection):
    service, repositories, live, activity, clock_state = _build(
        database_connection,
        rotate_enabled=True,
        rotate_media_threshold=0,
        rotate_interval_hours=1,
    )
    assert service.due_reason() is None
    clock_state[0] += 2 * 3600
    assert service.due_reason() is None

    activity.record(5, None)
    assert service.due_reason() == "interval"


def test_bridges_follow_rotation(database_connection):
    create = AsyncMock(return_value=SimpleNamespace(message_thread_id=99))
    service, repositories, live, activity, clock_state = _build(
        database_connection,
        rotate_enabled=True,
        rotate_media_threshold=1,
    )
    activity.record(5, None)
    repositories.add_bridge(-500, 5)
    repositories.add_bridge(-501, 7)
    ctx = _context()
    ctx.bot.create_forum_topic = create

    asyncio.run(service.rotate(ctx, actor="1", reason="manual"))

    assert repositories.get_bridge(-500).source_thread_id == 99
    assert repositories.get_bridge(-501).source_thread_id == 7


def test_status_shows_backoff_and_last_failure(database_connection):
    create = AsyncMock(side_effect=RuntimeError("boom"))
    service, repositories, live, activity, clock_state = _build(
        database_connection,
        rotate_enabled=True,
        rotate_media_threshold=1,
    )
    activity.record(5, None)
    ctx = _context()
    ctx.bot.create_forum_topic = create

    asyncio.run(service.tick(ctx))
    text = service.status_text()
    assert "Retry backoff" in text
    assert "Last rotation: failed" in text

    repositories.set_runtime_setting("rotation_pending", "5:99:11")
    text = service.status_text()
    assert "Pending rotation: 5 -> 99 (cycle 11)" in text

    create.side_effect = SimpleNamespace(message_thread_id=99)
    asyncio.run(service.rotate(ctx, actor="1", reason="manual"))
    text = service.status_text()
    assert "Last rotation: ok" in text


def test_cleanup_treats_missing_topic_as_deleted(database_connection):
    service, repositories, live, activity, clock_state = _build(
        database_connection,
    )
    repositories.add_rotated_topic(CHAT, 3, 11, 1)
    ctx = _context()
    ctx.bot.delete_forum_topic.side_effect = BadRequest("Topic_id_invalid")

    result = asyncio.run(service.cleanup(ctx, confirm=True, actor="1"))
    assert "1 deleted, 0 failed" in result

    result = asyncio.run(service.cleanup(ctx, confirm=True, actor="1"))
    assert "Nothing eligible for deletion." in result


def test_cleanup_other_error_counts_failed_and_stays(database_connection):
    service, repositories, live, activity, clock_state = _build(
        database_connection,
    )
    repositories.add_rotated_topic(CHAT, 3, 11, 1)
    ctx = _context()
    ctx.bot.delete_forum_topic.side_effect = RuntimeError("boom")

    result = asyncio.run(service.cleanup(ctx, confirm=True, actor="1"))
    assert "0 deleted, 1 failed" in result

    result = asyncio.run(service.cleanup(ctx, confirm=False, actor="1"))
    assert "Cycle 11 (topic 3)" in result


def test_cleanup_mark_failure_still_counts_deleted(database_connection):
    def wrap(repositories):
        return _Flaky(repositories, fail_method="mark")

    service, repositories, live, activity, clock_state = _build(
        database_connection,
        wrap=wrap,
    )
    repositories.add_rotated_topic(CHAT, 3, 11, 1)
    ctx = _context()

    result = asyncio.run(service.cleanup(ctx, confirm=True, actor="1"))
    assert "1 deleted, 0 failed" in result


def test_cleanup_preview_is_capped(database_connection):
    service, repositories, live, activity, clock_state = _build(
        database_connection,
    )
    for thread in range(100, 140):
        repositories.add_rotated_topic(CHAT, thread, thread - 89, 1)
    ctx = _context()

    text = asyncio.run(service.cleanup(ctx, confirm=False, actor="1"))
    lines = [line for line in text.splitlines() if line.startswith("Cycle ")]
    assert len(lines) == 30
    assert "...and 10 more" in text


def test_diagnostics_use_effective_topic():

    settings = SimpleNamespace(
        source_activity_check_enabled=True,
        source_activity_window_hours=24,
        source_chat_id=-100,
        source_thread_id=5,
    )
    stub = SimpleNamespace(
        get_runtime_setting=Mock(return_value="99"),
        count_recent_source_posts=Mock(return_value=3),
    )
    list(_source_activity_checks(settings, stub))
    stub.count_recent_source_posts.assert_called_with(-100, 99, ANY)


def _make_update(text, user_id, message_thread_id=7):
    return SimpleNamespace(
        effective_message=SimpleNamespace(
            text=text,
            message_thread_id=message_thread_id,
            reply_text=AsyncMock(),
        ),
        effective_chat=SimpleNamespace(id=CHAT, type="supergroup"),
        effective_user=SimpleNamespace(id=user_id),
    )


def test_management_non_admin(database_connection):
    repositories = SqliteRepositories(database_connection)
    rotation = SimpleNamespace(
        rotate=AsyncMock(return_value="ok"),
        status_text=Mock(return_value="status"),
        cleanup=AsyncMock(return_value="cleaned"),
    )
    management = ManagementCommands(
        SimpleNamespace(admin_user_ids=frozenset({1}), source_chat_id=CHAT),
        repositories,
        rotation=rotation,
    )
    update = _make_update("/rotate_now", 2)
    context = SimpleNamespace()

    asyncio.run(management.rotate_now(update, context))
    asyncio.run(management.rotate_status(update, context))
    asyncio.run(management.rotate_cleanup(update, context))

    assert update.effective_message.reply_text.await_count == 3
    for call in update.effective_message.reply_text.call_args_list:
        assert call.args[0] == "Only a Miki super administrator can do that."
    rotation.rotate.assert_not_awaited()
    rotation.cleanup.assert_not_awaited()
    rotation.status_text.assert_not_called()


def test_management_rotation_unavailable(database_connection):
    repositories = SqliteRepositories(database_connection)
    management = ManagementCommands(
        SimpleNamespace(admin_user_ids=frozenset({1}), source_chat_id=CHAT),
        repositories,
        rotation=None,
    )
    update = _make_update("/rotate_now", 1)
    context = SimpleNamespace()

    for handler in [management.rotate_now, management.rotate_status, management.rotate_cleanup]:
        asyncio.run(handler(update, context))
        update.effective_message.reply_text.assert_awaited_with("Rotation service is unavailable.")
        update.effective_message.reply_text.reset_mock()


def test_management_rotate_now(database_connection):
    repositories = SqliteRepositories(database_connection)
    rotation = SimpleNamespace(
        rotate=AsyncMock(return_value="ok"),
        status_text=Mock(return_value="status"),
        cleanup=AsyncMock(return_value="cleaned"),
    )
    management = ManagementCommands(
        SimpleNamespace(admin_user_ids=frozenset({1}), source_chat_id=CHAT),
        repositories,
        rotation=rotation,
    )
    update = _make_update("/rotate_now", 1)
    context = SimpleNamespace()

    asyncio.run(management.rotate_now(update, context))

    rotation.rotate.assert_awaited_with(context, actor="1", reason="manual")
    update.effective_message.reply_text.assert_awaited_with("ok")


def test_management_rotate_status(database_connection):
    repositories = SqliteRepositories(database_connection)
    rotation = SimpleNamespace(
        rotate=AsyncMock(return_value="ok"),
        status_text=Mock(return_value="status"),
        cleanup=AsyncMock(return_value="cleaned"),
    )
    management = ManagementCommands(
        SimpleNamespace(admin_user_ids=frozenset({1}), source_chat_id=CHAT),
        repositories,
        rotation=rotation,
    )
    update = _make_update("/rotate_status", 1)
    context = SimpleNamespace()

    asyncio.run(management.rotate_status(update, context))

    update.effective_message.reply_text.assert_awaited_with("status")
    events = repositories.list_audit_events(10)
    assert any(e["action"] == "rotation.status" for e in events)


def test_management_rotate_cleanup(database_connection):
    repositories = SqliteRepositories(database_connection)
    rotation = SimpleNamespace(
        rotate=AsyncMock(return_value="ok"),
        status_text=Mock(return_value="status"),
        cleanup=AsyncMock(return_value="cleaned"),
    )
    management = ManagementCommands(
        SimpleNamespace(admin_user_ids=frozenset({1}), source_chat_id=CHAT),
        repositories,
        rotation=rotation,
    )

    test_cases = [
        ("/rotate_cleanup", False),
        ("/rotate_cleanup confirm", True),
        ("/rotate_cleanup CONFIRM", True),
        ("/rotate_cleanup nope", False),
    ]
    for text, expected_confirm in test_cases:
        update = _make_update(text, 1)
        context = SimpleNamespace()
        asyncio.run(management.rotate_cleanup(update, context))
        rotation.cleanup.assert_awaited_with(context, confirm=expected_confirm, actor="1")
        update.effective_message.reply_text.assert_awaited_with("cleaned")


def test_main_wiring(database_connection):

    repositories = SqliteRepositories(database_connection)
    stub = SimpleNamespace(
        rotate=AsyncMock(return_value="ok"),
        status_text=Mock(return_value="status"),
        cleanup=AsyncMock(return_value="cleaned"),
    )
    management = ManagementCommands(
        SimpleNamespace(admin_user_ids=frozenset({1}), source_chat_id=CHAT),
        repositories,
        rotation=stub,
    )
    application = SimpleNamespace(add_handler=Mock())
    _add_management_handlers(application, management)

    commands = set()
    for call in application.add_handler.call_args_list:
        handler = call.args[0]
        if hasattr(handler, "commands"):
            commands.update(handler.commands)
    assert {"rotate_now", "rotate_status", "rotate_cleanup"} <= commands

    job_queue = SimpleNamespace(run_repeating=Mock())
    application = SimpleNamespace(job_queue=job_queue)
    tick = AsyncMock()
    _schedule_rotation(application, SimpleNamespace(tick=tick))
    kwargs = job_queue.run_repeating.call_args.kwargs
    assert kwargs["name"] == "topic-rotation"
    assert kwargs["interval"] == 60
