import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.rotation import RotationService, make_tick_job
from miki_sorter_bot.settings_registry import LiveSettings, parse_topic_title
from miki_sorter_bot.topic_activity import TopicActivity
from miki_sorter_bot.management import ManagementCommands


def _build(database_connection, *, clock=None, notify=None, source_thread=5, **overrides):
    settings = SimpleNamespace(
        source_chat_id=-1001234567890,
        source_thread_id=source_thread,
        admin_user_ids=frozenset({1}),
        **overrides,
    )
    repositories = SqliteRepositories(database_connection)
    live = LiveSettings(settings, repositories)
    activity = TopicActivity(repositories, live)
    clock_state = [1_000_000.0]
    service = RotationService(
        settings,
        repositories,
        live,
        activity,
        clock=clock or (lambda: clock_state[0]),
        notify=notify,
    )
    return service, repositories, live, activity, clock_state, settings


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


def test_parse_topic_title():
    assert parse_topic_title("Cycle {n}") == "Cycle {n}"
    assert parse_topic_title("  Cycle {n}  ") == "Cycle {n}"
    for bad in ["Cycle", "{n} {n}", "{x}", "Cycle {n", "C" * 200 + "{n}"]:
        with pytest.raises(ValueError):
            parse_topic_title(bad)


def test_due_reason_none_when_rotate_disabled(database_connection):
    service, repositories, live, activity, clock_state, settings = _build(database_connection)
    for _ in range(5):
        assert activity.record(5, None)
    assert service.due_reason() is None


def test_due_reason_media_threshold(database_connection):
    service, repositories, live, activity, clock_state, settings = _build(
        database_connection, rotate_enabled=True, rotate_media_threshold=3
    )
    assert activity.record(5, None)
    assert activity.record(5, None)
    assert service.due_reason() is None
    assert activity.record(5, None)
    assert service.due_reason() == "media"


def test_due_reason_interval(database_connection):
    service, repositories, live, activity, clock_state, settings = _build(
        database_connection,
        rotate_enabled=True,
        rotate_interval_hours=1,
        rotate_media_threshold=0,
    )
    assert activity.record(5, None)
    assert service.due_reason() is None
    clock_state[0] += 3600.0
    assert service.due_reason() == "interval"


def test_due_reason_first_wins(database_connection):
    service, repositories, live, activity, clock_state, settings = _build(
        database_connection,
        rotate_enabled=True,
        rotate_media_threshold=1,
        rotate_interval_hours=1,
    )
    assert activity.record(5, None)
    clock_state[0] += 3600.0
    assert service.due_reason() == "media"


def test_cycle_clock_persists_across_services(database_connection):
    service, repositories, live, activity, clock_state, settings = _build(
        database_connection,
        rotate_enabled=True,
        rotate_interval_hours=1,
        rotate_media_threshold=0,
    )
    assert activity.record(5, None)
    assert service.due_reason() is None
    service2 = RotationService(
        settings,
        repositories,
        live,
        activity,
        clock=lambda: clock_state[0],
    )
    clock_state[0] += 3600.0
    assert service2.due_reason() == "interval"


def test_rotate_success_defaults(database_connection):
    service, repositories, live, activity, clock_state, settings = _build(
        database_connection, rotate_enabled=True
    )
    assert activity.record(5, None)
    assert activity.record(5, None)
    ctx = _context()
    asyncio.run(service.rotate(ctx, actor="1", reason="test"))
    ctx.bot.create_forum_topic.assert_awaited_once()
    assert ctx.bot.create_forum_topic.await_args.kwargs.get("name") == "Cycle 11"
    assert repositories.get_runtime_setting("source_thread_id") == "99"
    assert live.topic_cycle() == 11
    assert activity.rotation_count() == 0
    ctx.bot.close_forum_topic.assert_awaited_once()
    assert repositories.is_rotated_topic(-1001234567890, 5)
    assert ctx.bot.send_message.await_count == 2
    audit = repositories.list_audit_events(20)
    assert any(e["action"] == "rotation.rotate" and e["outcome"] == "success" for e in audit)

    ctx2 = _context(new_thread_id=100)
    asyncio.run(service.rotate(ctx2, actor="1", reason="test"))
    assert ctx2.bot.create_forum_topic.await_args.kwargs.get("name") == "Cycle 12"


def test_rotate_failed_creation(database_connection):
    notify = AsyncMock()
    service, repositories, live, activity, clock_state, settings = _build(
        database_connection, rotate_enabled=True, notify=notify
    )
    assert activity.record(5, None)
    ctx = _context()
    ctx.bot.create_forum_topic.side_effect = RuntimeError("boom")
    asyncio.run(service.rotate(ctx, actor="1", reason="test"))
    assert live.topic_cycle() == 10
    assert repositories.get_runtime_setting("source_thread_id") is None
    ctx.bot.close_forum_topic.assert_not_awaited()
    notify.assert_awaited_once()
    audit = repositories.list_audit_events(20)
    assert any(e["action"] == "rotation.rotate" and e["outcome"] == "failed" for e in audit)


def test_rotate_roster_rewrite(database_connection):
    service, repositories, live, activity, clock_state, settings = _build(
        database_connection,
        rotate_enabled=True,
    )
    repositories.set_runtime_setting("periodic_notice_topics", "5, 7")
    assert activity.record(5, None)
    ctx = _context()
    asyncio.run(service.rotate(ctx, actor="1", reason="test"))
    assert repositories.get_runtime_setting("periodic_notice_topics") == "7, 99"

    repositories.delete_runtime_setting("periodic_notice_topics")
    repositories.delete_runtime_setting("source_thread_id")
    service2, repositories2, live2, activity2, clock_state2, settings2 = _build(
        database_connection, rotate_enabled=True
    )
    assert activity2.record(5, None)
    ctx2 = _context(new_thread_id=100)
    asyncio.run(service2.rotate(ctx2, actor="1", reason="test"))
    assert repositories2.get_runtime_setting("periodic_notice_topics") is None


def test_rotate_close_failure(database_connection):
    notify = AsyncMock()
    service, repositories, live, activity, clock_state, settings = _build(
        database_connection, rotate_enabled=True, notify=notify
    )
    assert activity.record(5, None)
    ctx = _context()
    ctx.bot.close_forum_topic.side_effect = RuntimeError("boom")
    asyncio.run(service.rotate(ctx, actor="1", reason="test"))
    assert not repositories.is_rotated_topic(-1001234567890, 5)
    audit = repositories.list_audit_events(20)
    rotate_events = [e for e in audit if e["action"] == "rotation.rotate"]
    assert rotate_events and rotate_events[0]["outcome"] == "success"
    assert rotate_events[0]["details"].get("problems")
    notify.assert_awaited_once()
    assert repositories.get_runtime_setting("source_thread_id") == "99"


def test_rotate_concurrency(database_connection):
    service, repositories, live, activity, clock_state, settings = _build(
        database_connection, rotate_enabled=True
    )
    assert activity.record(5, None)
    ctx = _context()

    async def slow_create(*args, **kwargs):
        await asyncio.sleep(0.01)
        return SimpleNamespace(message_thread_id=99)

    ctx.bot.create_forum_topic.side_effect = slow_create

    async def run():
        return await asyncio.gather(
            service.rotate(ctx, actor="1", reason="manual"),
            service.rotate(ctx, actor="1", reason="manual"),
        )

    results = asyncio.run(run())
    assert ctx.bot.create_forum_topic.await_count == 1
    assert any(r == "Rotation already in progress." for r in results)


def test_tick(database_connection):
    service, repositories, live, activity, clock_state, settings = _build(
        database_connection, rotate_enabled=True, rotate_media_threshold=1
    )
    tick = make_tick_job(service)
    ctx = _context()
    asyncio.run(tick(ctx))
    ctx.bot.create_forum_topic.assert_not_awaited()

    assert activity.record(5, None)
    ctx2 = _context()
    asyncio.run(tick(ctx2))
    ctx2.bot.create_forum_topic.assert_awaited_once()


def test_cleanup(database_connection):
    service, repositories, live, activity, clock_state, settings = _build(
        database_connection, closed_topic_delete_days=0
    )
    ctx = _context()
    result = asyncio.run(service.cleanup(ctx, confirm=False, actor="1"))
    assert "disabled" in result
    ctx.bot.delete_forum_topic.assert_not_awaited()

    service2, repositories2, live2, activity2, clock_state2, settings2 = _build(
        database_connection, rotate_enabled=True
    )
    now = clock_state2[0]
    repositories2.add_rotated_topic(-1001234567890, 3, 8, closed_at=now - 31 * 86400)
    repositories2.add_rotated_topic(-1001234567890, 4, 9, closed_at=now - 1 * 86400)
    ctx2 = _context()
    result2 = asyncio.run(service2.cleanup(ctx2, confirm=False, actor="1"))
    assert "3" in result2
    assert "4" not in result2
    ctx2.bot.delete_forum_topic.assert_not_awaited()

    ctx3 = _context()
    asyncio.run(service2.cleanup(ctx3, confirm=True, actor="1"))
    ctx3.bot.delete_forum_topic.assert_awaited_once_with(
        chat_id=-1001234567890, message_thread_id=3
    )
    result4 = asyncio.run(service2.cleanup(ctx3, confirm=True, actor="1"))
    assert "Nothing eligible for deletion" in result4

    service3, repositories3, live3, activity3, clock_state3, settings3 = _build(
        database_connection, rotate_enabled=True, source_thread=3
    )
    now = clock_state3[0]
    repositories3.add_rotated_topic(-1001234567890, 3, 8, closed_at=now - 31 * 86400)
    ctx4 = _context()
    asyncio.run(service3.cleanup(ctx4, confirm=True, actor="1"))
    ctx4.bot.delete_forum_topic.assert_not_awaited()

    service4, repositories4, live4, activity4, clock_state4, settings4 = _build(
        database_connection, rotate_enabled=True
    )
    repositories4.register_topic(-1001234567890, 3, "Archive")
    now = clock_state4[0]
    repositories4.add_rotated_topic(-1001234567890, 3, 8, closed_at=now - 31 * 86400)
    ctx5 = _context()
    asyncio.run(service4.cleanup(ctx5, confirm=True, actor="1"))
    ctx5.bot.delete_forum_topic.assert_not_awaited()
    assert repositories4.get(-1001234567890, 3).is_active


def test_management_guard(database_connection):
    repositories = SqliteRepositories(database_connection)
    commands = ManagementCommands(
        SimpleNamespace(admin_user_ids=frozenset({1}), source_chat_id=-1001234567890),
        repositories,
    )
    repositories.register_topic(-1001234567890, 5, "Archive")
    repositories.add_rotated_topic(-1001234567890, 5, 10, 1)
    update = SimpleNamespace(
        effective_message=SimpleNamespace(
            message_thread_id=5,
            forum_topic_closed=object(),
            forum_topic_reopened=None,
            forum_topic_edited=None,
        ),
        effective_chat=SimpleNamespace(id=-1001234567890),
    )
    asyncio.run(commands.track_topic_status(update, SimpleNamespace()))
    assert repositories.get(-1001234567890, 5).is_active

    repositories.register_topic(-1001234567890, 6, "Regular")
    update6 = SimpleNamespace(
        effective_message=SimpleNamespace(
            message_thread_id=6,
            forum_topic_closed=object(),
            forum_topic_reopened=None,
            forum_topic_edited=None,
        ),
        effective_chat=SimpleNamespace(id=-1001234567890),
    )
    asyncio.run(commands.track_topic_status(update6, SimpleNamespace()))
    assert not repositories.get(-1001234567890, 6).is_active
