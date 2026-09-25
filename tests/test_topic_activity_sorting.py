import asyncio
from types import SimpleNamespace
from unittest.mock import Mock


from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.settings_registry import LiveSettings
from miki_sorter_bot.sorting import SortingService
from miki_sorter_bot.topic_activity import TopicActivity


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        source_chat_id=-100,
        source_thread_id=5,
        archive_chat_id=-200,
        topic_forwarding_pairs=(),
        sort_dry_run=False,
        send_confirmation=False,
        default_topic_id=0,
    )


def _message(
    caption, *, message_id=12, chat_id=-100, entities=None, kind="photo"
) -> SimpleNamespace:
    media_object = SimpleNamespace(file_id=f"file-{message_id}")
    return SimpleNamespace(
        message_id=message_id,
        chat_id=chat_id,
        message_thread_id=5,
        caption=caption,
        caption_entities=entities,
        text=None,
        from_user=SimpleNamespace(id=10, is_bot=False),
        media_group_id=None,
        photo=[media_object] if kind == "photo" else [],
        animation=None,
        audio=None,
        document=None,
        sticker=None,
        video=media_object if kind == "video" else None,
        video_note=None,
        voice=None,
    )


def _make(database_connection, notice):
    repositories = SqliteRepositories(database_connection)
    live_settings = LiveSettings(_settings(), repositories)
    activity = TopicActivity(repositories, live_settings)
    indexing = SimpleNamespace(index_copy=Mock(return_value=True))
    service = SortingService(
        _settings(),
        repositories,
        indexing,
        live_settings=live_settings,
        notice=notice,
        activity=activity,
    )
    return service, activity


def _run(service, *msgs, context_bot_id=50, edited_message=None):
    context = SimpleNamespace(bot=SimpleNamespace(id=context_bot_id))

    async def go():
        for msg in msgs:
            update = SimpleNamespace(
                update_id=1,
                effective_message=msg,
                effective_chat=SimpleNamespace(id=-100, type="supergroup"),
                edited_message=edited_message,
            )
            await service.handle_update(update, context)

    asyncio.run(go())


def test_single_media_counts_once(database_connection):
    notice = Mock()
    service, activity = _make(database_connection, notice)
    _run(service, _message("single"))
    assert activity.rotation_count() == 1
    assert notice.on_media.call_count == 1
    assert notice.on_media.call_args.kwargs["counted"] is True


def test_album_counts_once_and_second_not_counted(database_connection):
    notice = Mock()
    service, activity = _make(database_connection, notice)
    first = _message("album 1", message_id=12)
    first.media_group_id = "album-1"
    second = _message("album 2", message_id=13)
    second.media_group_id = "album-1"
    _run(service, first, second)
    assert activity.rotation_count() == 1
    assert notice.on_media.call_count == 2
    assert notice.on_media.call_args_list[0].kwargs["counted"] is True
    assert notice.on_media.call_args_list[1].kwargs["counted"] is False


def test_edited_message_not_counted(database_connection):
    notice = Mock()
    service, activity = _make(database_connection, notice)
    _run(service, _message("edited"), edited_message=SimpleNamespace())
    assert activity.rotation_count() == 0
    assert notice.on_media.call_count == 0


def test_bot_authored_message_not_counted(database_connection):
    notice = Mock()
    service, activity = _make(database_connection, notice)
    msg = _message("bot")
    msg.from_user = SimpleNamespace(id=50, is_bot=True)
    _run(service, msg)
    assert activity.rotation_count() == 0


def test_no_notice_still_counts(database_connection):
    service, activity = _make(database_connection, None)
    _run(service, _message("no notice"))
    assert activity.rotation_count() == 1


def test_media_outside_source_topic_not_counted(database_connection):
    notice = Mock()
    service, activity = _make(database_connection, notice)
    msg = _message("other topic")
    msg.message_thread_id = 9
    _run(service, msg)
    assert activity.rotation_count() == 0
    assert notice.on_media.call_count == 1
