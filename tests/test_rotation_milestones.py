import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.rotation import RotationService
from miki_sorter_bot.settings_registry import LiveSettings
from miki_sorter_bot.topic_activity import TopicActivity

CHAT = -1001234567890


def _build(database_connection, *, deletion=None, source_thread=5, **overrides):
    values = {
        "source_chat_id": CHAT,
        "source_thread_id": source_thread,
        "admin_user_ids": frozenset({1}),
        "rotate_enabled": True,
        **overrides,
    }
    settings = SimpleNamespace(**values)
    repositories = SqliteRepositories(database_connection)
    live = LiveSettings(settings, repositories)
    activity = TopicActivity(repositories, live)
    clock_state = [10_000_000.0]
    service = RotationService(
        settings,
        repositories,
        live,
        activity,
        clock=lambda: clock_state[0],
        deletion=deletion,
    )
    return service, repositories, live, activity, clock_state


def _context(new_thread_id=99):
    bot = SimpleNamespace(
        create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=new_thread_id)),
        close_forum_topic=AsyncMock(),
        delete_forum_topic=AsyncMock(),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=555)),
    )
    return SimpleNamespace(bot=bot)


def _posts(activity, count, thread=5):
    for _ in range(count):
        activity.record(thread, None)


def _texts(ctx):
    return [c.kwargs["text"] for c in ctx.bot.send_message.await_args_list]


def test_media_80_notice_posted_and_scheduled(database_connection):
    deletion = Mock()
    service, repositories, live, activity, clock_state = _build(
        database_connection, deletion=deletion, rotate_media_threshold=10
    )
    ctx = _context()
    _posts(activity, 8)
    asyncio.run(service.tick(ctx))
    assert len(ctx.bot.send_message.await_args_list) == 1
    call_kwargs = ctx.bot.send_message.await_args_list[0].kwargs
    assert call_kwargs["message_thread_id"] == 5
    assert call_kwargs["chat_id"] == CHAT
    assert "80%" in call_kwargs["text"]
    assert "8/10" in call_kwargs["text"]
    deletion.schedule.assert_called_once_with(CHAT, 555, 86400)
    ctx.bot.create_forum_topic.assert_not_awaited()


def test_media_notice_not_repeated(database_connection):
    deletion = Mock()
    service, repositories, live, activity, clock_state = _build(
        database_connection, deletion=deletion, rotate_media_threshold=10
    )
    ctx = _context()
    _posts(activity, 8)
    asyncio.run(service.tick(ctx))
    assert len(ctx.bot.send_message.await_args_list) == 1
    asyncio.run(service.tick(ctx))
    assert len(ctx.bot.send_message.await_args_list) == 1
    activity.record(5, None)
    asyncio.run(service.tick(ctx))
    assert len(ctx.bot.send_message.await_args_list) == 2
    assert "90%" in _texts(ctx)[-1]


def test_media_100_notice_then_rotation(database_connection):
    deletion = Mock()
    service, repositories, live, activity, clock_state = _build(
        database_connection, deletion=deletion, rotate_media_threshold=10
    )
    ctx = _context()
    _posts(activity, 10)
    asyncio.run(service.tick(ctx))
    texts = _texts(ctx)
    assert "Moving to Cycle 11" in texts[0]
    ctx.bot.create_forum_topic.assert_awaited_once()


def test_catch_up_posts_single_message(database_connection):
    deletion = Mock()
    service, repositories, live, activity, clock_state = _build(
        database_connection, deletion=deletion, rotate_media_threshold=10
    )
    ctx = _context()
    _posts(activity, 9)
    asyncio.run(service.tick(ctx))
    texts = _texts(ctx)
    assert len(texts) == 1
    assert "90%" in texts[0]
    asyncio.run(service.tick(ctx))
    assert len(_texts(ctx)) == 1
    activity.record(5, None)
    asyncio.run(service.tick(ctx))
    assert any("Moving to Cycle 11" in text for text in _texts(ctx))


def test_time_milestones_sequence(database_connection):
    deletion = Mock()
    service, repositories, live, activity, clock_state = _build(
        database_connection,
        deletion=deletion,
        rotate_media_threshold=0,
        rotate_interval_hours=400,
    )
    ctx = _context()
    _posts(activity, 1)
    asyncio.run(service.tick(ctx))
    assert len(_texts(ctx)) == 0
    clock_state[0] += 320 * 3600
    asyncio.run(service.tick(ctx))
    assert "80%" in _texts(ctx)[-1]
    clock_state[0] += 40 * 3600
    asyncio.run(service.tick(ctx))
    assert "90%" in _texts(ctx)[-1]
    clock_state[0] += 16 * 3600
    asyncio.run(service.tick(ctx))
    texts = _texts(ctx)
    assert "within 24 hours" in texts[-1]
    assert "%" not in texts[-1]
    clock_state[0] += 24 * 3600
    asyncio.run(service.tick(ctx))
    assert any("Moving to Cycle 11" in text for text in _texts(ctx))
    ctx.bot.create_forum_topic.assert_awaited_once()


def test_time_notices_need_a_post(database_connection):
    deletion = Mock()
    service, repositories, live, activity, clock_state = _build(
        database_connection,
        deletion=deletion,
        rotate_media_threshold=0,
        rotate_interval_hours=400,
    )
    ctx = _context()
    clock_state[0] += 330 * 3600
    asyncio.run(service.tick(ctx))
    assert len(_texts(ctx)) == 0


def test_disabled_by_setting(database_connection):
    deletion = Mock()
    service, repositories, live, activity, clock_state = _build(
        database_connection,
        deletion=deletion,
        rotate_media_threshold=10,
        rotate_milestones_enabled=False,
    )
    ctx = _context()
    _posts(activity, 8)
    asyncio.run(service.tick(ctx))
    assert len(_texts(ctx)) == 0

    service2, repositories2, live2, activity2, clock_state2 = _build(
        database_connection,
        deletion=deletion,
        rotate_media_threshold=10,
        rotate_enabled=False,
    )
    ctx2 = _context()
    _posts(activity2, 8)
    asyncio.run(service2.tick(ctx2))
    assert len(_texts(ctx2)) == 0


def test_works_without_deletion_service(database_connection):
    service, repositories, live, activity, clock_state = _build(
        database_connection, deletion=None, rotate_media_threshold=10
    )
    ctx = _context()
    _posts(activity, 8)
    asyncio.run(service.tick(ctx))
    assert len(_texts(ctx)) == 1


def test_send_failure_is_swallowed_and_not_repeated(database_connection):
    service, repositories, live, activity, clock_state = _build(
        database_connection, deletion=None, rotate_media_threshold=10
    )
    ctx = _context()
    ctx.bot.send_message.side_effect = RuntimeError("boom")
    _posts(activity, 8)
    asyncio.run(service.tick(ctx))
    assert ctx.bot.send_message.await_count == 1
    asyncio.run(service.tick(ctx))
    assert ctx.bot.send_message.await_count == 1


def test_notices_restart_after_rotation(database_connection):
    deletion = Mock()
    service, repositories, live, activity, clock_state = _build(
        database_connection, deletion=deletion, rotate_media_threshold=10
    )
    ctx = _context(99)
    _posts(activity, 10)
    asyncio.run(service.tick(ctx))
    ctx.bot.create_forum_topic.assert_awaited_once()
    _posts(activity, 8, thread=99)
    ctx2 = _context(100)
    asyncio.run(service.tick(ctx2))
    assert ctx2.bot.send_message.await_count == 1
    call_kwargs = ctx2.bot.send_message.await_args_list[0].kwargs
    assert call_kwargs["message_thread_id"] == 99
    assert "80%" in call_kwargs["text"]
