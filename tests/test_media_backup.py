import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.sorting import SortingService, _strip_sender_identifiers


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


def _service(repositories) -> SortingService:
    settings = _settings()
    indexing = SimpleNamespace(index_copy=Mock(return_value=True))
    return SortingService(settings, repositories, indexing)


def _run(coro):
    return asyncio.run(coro)


def test_tag_to_topic_mapping_first_pair_wins(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = _service(repositories)

    bot = SimpleNamespace(
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(),
        forward_message=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    jav_msg = _message("photo #jav", message_id=1)
    asian_msg = _message("photo #asian", message_id=2)
    both_msg = _message("photo #jav #asian", message_id=3)

    _run(service._backup_to_second_group((jav_msg,), context))
    _run(service._backup_to_second_group((asian_msg,), context))
    _run(service._backup_to_second_group((both_msg,), context))

    jav_call = bot.copy_message.await_args_list[0]
    asian_call = bot.copy_message.await_args_list[1]
    both_call = bot.copy_message.await_args_list[2]

    assert jav_call.kwargs["message_thread_id"] == 2
    assert asian_call.kwargs["message_thread_id"] == 3
    assert both_call.kwargs["message_thread_id"] == 2
    assert bot.forward_message.await_count == 0
    assert bot.copy_messages.await_count == 0
    assert bot.send_media_group.await_count == 0


def test_whole_hashtag_only_no_partial_match(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = _service(repositories)

    bot = SimpleNamespace(
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(),
        forward_message=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    msg1 = _message("photo #javascript", message_id=1)
    msg2 = _message("photo #java", message_id=2)

    _run(service._backup_to_second_group((msg1, msg2), context))

    assert bot.copy_message.await_count == 0
    assert bot.send_media_group.await_count == 0
    assert bot.forward_message.await_count == 0
    assert bot.copy_messages.await_count == 0


def test_runtime_override_chat_id_and_tag_map_and_empty_disables(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = _service(repositories)

    bot = SimpleNamespace(
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(),
        forward_message=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    repositories.set_runtime_setting("media_backup_chat_id", "-999")
    repositories.set_runtime_setting("media_backup_tag_topics", "foo:7")

    msg = _message("photo #foo", message_id=1)
    _run(service._backup_to_second_group((msg,), context))

    assert bot.copy_message.await_count == 1
    kwargs = bot.copy_message.await_args.kwargs
    assert kwargs["chat_id"] == -999
    assert kwargs["message_thread_id"] == 7

    bot.copy_message.reset_mock()
    repositories.set_runtime_setting("media_backup_tag_topics", "")

    _run(service._backup_to_second_group((msg,), context))
    assert bot.copy_message.await_count == 0
    assert bot.send_media_group.await_count == 0
    assert bot.forward_message.await_count == 0
    assert bot.copy_messages.await_count == 0


def test_single_strips_identifiers_preserves_hashtag_no_forward(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = _service(repositories)

    bot = SimpleNamespace(
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(),
        forward_message=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    caption = "#JAV @someone_x see https://t.me/x"
    entities = [
        SimpleNamespace(type="mention", offset=5, length=10),
        SimpleNamespace(type="url", offset=20, length=15),
    ]
    msg = _message(caption, message_id=1, entities=entities)

    _run(service._backup_to_second_group((msg,), context))

    assert bot.copy_message.await_count == 1
    kwargs = bot.copy_message.await_args.kwargs
    assert kwargs["chat_id"] == -1004365154840
    assert kwargs["message_thread_id"] == 2
    assert kwargs["from_chat_id"] == -100
    assert kwargs["message_id"] == 1
    stripped = kwargs["caption"]
    assert "#JAV" in stripped
    assert "@someone_x" not in stripped
    assert "https://t.me/x" not in stripped
    assert bot.forward_message.await_count == 0
    assert bot.copy_messages.await_count == 0
    assert bot.send_media_group.await_count == 0


def test_caption_only_hashtag_and_handle_keeps_hashtag(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = _service(repositories)

    bot = SimpleNamespace(
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(),
        forward_message=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    caption = "#jav @someone_x"
    entities = [SimpleNamespace(type="mention", offset=5, length=10)]
    msg = _message(caption, message_id=1, entities=entities)

    _run(service._backup_to_second_group((msg,), context))

    assert bot.copy_message.await_count == 1
    kwargs = bot.copy_message.await_args.kwargs
    assert "caption" in kwargs
    assert "#jav" in kwargs["caption"]
    assert "@someone_x" not in kwargs["caption"]


def test_album_sends_media_group_stripped_no_copy(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = _service(repositories)

    bot = SimpleNamespace(
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(message_id=100),
                SimpleNamespace(message_id=101),
            ]
        ),
        forward_message=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    msg1 = _message(
        "first #jav @alice",
        message_id=1,
        entities=[SimpleNamespace(type="mention", offset=11, length=6)],
    )
    msg2 = _message(
        "second #jav https://t.me/x",
        message_id=2,
        entities=[SimpleNamespace(type="url", offset=12, length=14)],
    )

    _run(service._backup_to_second_group((msg1, msg2), context))

    assert bot.send_media_group.await_count == 1
    kwargs = bot.send_media_group.await_args.kwargs
    assert kwargs["chat_id"] == -1004365154840
    assert kwargs["message_thread_id"] == 2
    media = kwargs["media"]
    assert len(media) == 2
    assert media[0].caption == "first #jav"
    assert not media[0].caption_entities
    assert media[1].caption == "second #jav"
    assert not media[1].caption_entities
    assert bot.copy_message.await_count == 0
    assert bot.forward_message.await_count == 0
    assert bot.copy_messages.await_count == 0


def test_album_send_media_group_failure_falls_back_to_copy(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = _service(repositories)

    bot = SimpleNamespace(
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(side_effect=Exception("fail")),
        forward_message=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    msg1 = _message("first #jav", message_id=1)
    msg2 = _message("second #jav", message_id=2)

    _run(service._backup_to_second_group((msg1, msg2), context))

    assert bot.send_media_group.await_count == 1
    assert bot.copy_message.await_count == 2
    assert bot.forward_message.await_count == 0
    assert bot.copy_messages.await_count == 0


def test_dedupe_same_message_sent_once(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = _service(repositories)

    bot = SimpleNamespace(
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(),
        forward_message=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    msg = _message("photo #jav", message_id=1)

    _run(service._backup_to_second_group((msg,), context))
    _run(service._backup_to_second_group((msg,), context))

    assert bot.copy_message.await_count == 1


def test_dedupe_retry_after_failure(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = _service(repositories)

    bot = SimpleNamespace(
        copy_message=AsyncMock(side_effect=[Exception("fail"), None]),
        send_media_group=AsyncMock(),
        forward_message=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    msg = _message("photo #jav", message_id=1)

    _run(service._backup_to_second_group((msg,), context))
    _run(service._backup_to_second_group((msg,), context))

    assert bot.copy_message.await_count == 2
    assert repositories.metrics_snapshot()["media_backup_failures"] == 1


def test_failure_swallowed_metric_incremented(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = _service(repositories)

    bot = SimpleNamespace(
        copy_message=AsyncMock(side_effect=Exception("fail")),
        send_media_group=AsyncMock(),
        forward_message=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    msg = _message("photo #jav", message_id=1)

    _run(service._backup_to_second_group((msg,), context))

    assert repositories.metrics_snapshot()["media_backup_failures"] == 1


def test_partial_media_group_result_dedupes_only_successful(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = _service(repositories)

    bot = SimpleNamespace(
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=100)]),
        forward_message=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    msg1 = _message("first #jav", message_id=1)
    msg2 = _message("second #jav", message_id=2)

    _run(service._backup_to_second_group((msg1, msg2), context))

    assert repositories.metrics_snapshot()["media_backup_failures"] == 1
    assert bot.copy_message.await_count == 0

    _run(service._backup_to_second_group((msg1, msg2), context))

    assert bot.send_media_group.await_count == 1
    assert bot.copy_message.await_count == 1
    assert bot.copy_message.await_args.kwargs["message_id"] == 2
    assert repositories.metrics_snapshot()["media_backup_failures"] == 1

    _run(service._backup_to_second_group((msg1, msg2), context))

    assert bot.send_media_group.await_count == 1
    assert bot.copy_message.await_count == 1


def test_strip_emoji_before_mention():
    text = "😀 @alice hi"
    entities = [SimpleNamespace(type="mention", offset=3, length=6)]
    assert _strip_sender_identifiers(text, entities) == "😀 hi"


def test_strip_text_link_removes_visible_text():
    text = "see this link"
    entities = [SimpleNamespace(type="text_link", offset=4, length=4)]
    assert _strip_sender_identifiers(text, entities) == "see link"


def test_strip_plain_handle_and_bare_url():
    text = "hello @alice and https://example.com and t.me/x"
    result = _strip_sender_identifiers(text, None)
    assert "@alice" not in result
    assert "https://example.com" not in result
    assert "t.me/x" not in result
    assert "hello" in result
    assert "and" in result


def test_strip_keeps_hashtags():
    text = "#jav #asian"
    assert _strip_sender_identifiers(text, None) == "#jav #asian"


def test_strip_mention_only_becomes_empty():
    text = "@alice"
    entities = [SimpleNamespace(type="mention", offset=0, length=6)]
    assert _strip_sender_identifiers(text, entities) == ""


def test_strip_none_entities_ok():
    text = "plain text"
    assert _strip_sender_identifiers(text, None) == "plain text"


def test_strip_entity_beyond_end_no_raise():
    text = "hi"
    entities = [SimpleNamespace(type="mention", offset=10, length=5)]
    assert _strip_sender_identifiers(text, entities) == "hi"
