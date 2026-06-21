from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

from miki_sorter_bot.config import TopicForwardingPair
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.sorting import RouteMatcher, SortingService


def _settings(
    *,
    dry_run: bool = False,
    forwarding_pairs: tuple[TopicForwardingPair, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        source_chat_id=-100,
        source_thread_id=5,
        archive_chat_id=-200,
        topic_forwarding_pairs=forwarding_pairs,
        sort_dry_run=dry_run,
        send_confirmation=False,
    )


def _message(
    caption: str,
    *,
    message_id: int = 12,
    sender_id: int = 10,
    media: bool = True,
    media_group_id: str | None = None,
    media_kind: str = "photo",
    thread_id: int | None = 5,
) -> SimpleNamespace:
    media_object = SimpleNamespace(file_id=f"file-{message_id}")
    return SimpleNamespace(
        message_id=message_id,
        message_thread_id=thread_id,
        caption=caption,
        caption_entities=None,
        text=None,
        from_user=SimpleNamespace(id=sender_id, is_bot=False),
        media_group_id=media_group_id,
        photo=[media_object] if media and media_kind == "photo" else [],
        animation=None,
        audio=media_object if media and media_kind == "audio" else None,
        document=media_object if media and media_kind == "document" else None,
        sticker=media_object if media and media_kind == "sticker" else None,
        video=media_object if media and media_kind == "video" else None,
        video_note=None,
        voice=None,
        reply_text=AsyncMock(),
    )


def _routes(repositories: SqliteRepositories) -> None:
    repositories.register_topic(-200, 9, "Japan")
    repositories.register_topic(-200, 10, "Codes")
    repositories.add_mapping(-200, 9, "hashtag", "Japan", 1)
    repositories.add_mapping(-200, 9, "keyword", "Tokyo", 1)
    repositories.add_mapping(-200, 10, "keyword", "ABC", 1)
    repositories.add_mapping(-200, 9, "phrase", "New York", 1)


def test_matcher_enforces_hashtag_precedence_and_exact_keywords(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    matcher = RouteMatcher(repositories, -200)

    hashtag = matcher.decide("ABC but explicitly #Japan")
    exact = matcher.decide("ABC only")
    punctuated = matcher.decide("(ABC)-only")
    joined = matcher.decide("ABCDEF only")
    split = matcher.decide("A BC only")
    phrase = matcher.decide("Visit new york today")
    punctuated_phrase = matcher.decide("Visit new, york today")

    assert hashtag.status == "matched"
    assert hashtag.topic.thread_id == 9
    assert hashtag.reason == "hashtag:japan"
    assert exact.status == "matched"
    assert exact.topic.thread_id == 10
    assert punctuated.status == "matched"
    assert punctuated.topic.thread_id == 10
    assert joined.status == "unmatched"
    assert split.status == "unmatched"
    assert phrase.topic.thread_id == 9
    assert punctuated_phrase.status == "unmatched"


def test_matcher_reports_cross_topic_conflict(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)

    decision = RouteMatcher(repositories, -200).decide("Tokyo and ABC")

    assert decision.status == "conflict"
    assert decision.topic is None


def test_successful_sort_persists_before_copy_and_indexes_result(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    indexing = SimpleNamespace(index_copy=Mock(return_value=True))
    service = SortingService(_settings(), repositories, indexing)
    message = _message("#Japan")
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    delivery = repositories.get_delivery(-100, 12, -200, 9)
    assert delivery.status == "sent"
    assert delivery.destination_message_id == 99
    bot.copy_message.assert_awaited_once()
    indexing.index_copy.assert_called_once()


def test_direct_destination_forwards_captionless_document_without_routes(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Inbox")
    indexing = SimpleNamespace(index_copy=Mock(return_value=True))
    service = SortingService(
        _settings(forwarding_pairs=(TopicForwardingPair(5, 9),)),
        repositories,
        indexing,
    )
    message = _message("", media_kind="document")
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    bot.copy_message.assert_awaited_once()
    assert bot.copy_message.await_args.kwargs["message_thread_id"] == 9
    assert repositories.get_delivery(-100, 12, -200, 9).reason == "forwarding-pair:5->9"


def test_multiple_source_topics_can_forward_to_one_destination(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Shared Inbox")
    service = SortingService(
        _settings(
            forwarding_pairs=(
                TopicForwardingPair(5, 9),
                TopicForwardingPair(6, 9),
            )
        ),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(
            side_effect=[SimpleNamespace(message_id=99), SimpleNamespace(message_id=100)]
        ),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def forward_both() -> None:
        await service.handle_update(
            SimpleNamespace(effective_message=_message("", message_id=12), effective_chat=chat),
            context,
        )
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message("", message_id=13, thread_id=6),
                effective_chat=chat,
            ),
            context,
        )

    asyncio.run(forward_both())

    assert bot.copy_message.await_count == 2
    assert {call.kwargs["message_thread_id"] for call in bot.copy_message.await_args_list} == {9}


def test_multiple_hashtags_for_same_topic_forward_single_message_once(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    repositories.add_mapping(-200, 9, "hashtag", "Tokyo", 1)
    repositories.add_mapping(-200, 9, "hashtag", "RX7", 1)
    indexing = SimpleNamespace(index_copy=Mock(return_value=True))
    service = SortingService(_settings(), repositories, indexing)
    message = _message("#Japan #Tokyo #RX7")
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    delivery = repositories.get_delivery(-100, 12, -200, 9)
    assert delivery.status == "sent"
    bot.copy_message.assert_awaited_once()
    indexing.index_copy.assert_called_once()
    assert repositories.metrics_snapshot()["sort_deliveries"] == 1


def test_keyword_inside_compact_identifier_is_not_sorted(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    repositories.add_mapping(-200, 10, "keyword", "COD", 1)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    message = _message("New COD123 release")
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    bot.copy_message.assert_not_awaited()


def test_hashtag_with_underscore_is_sorted(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    repositories.add_mapping(-200, 9, "hashtag", "New_York", 1)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    message = _message("A photo from #New_York")
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    bot.copy_message.assert_awaited_once()
    assert bot.copy_message.await_args.kwargs["message_thread_id"] == 9


def test_duplicate_update_does_not_copy_twice(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    update = SimpleNamespace(
        effective_message=_message("#Japan"),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
    )
    context = SimpleNamespace(bot=bot)

    asyncio.run(service.handle_update(update, context))
    asyncio.run(service.handle_update(update, context))

    assert bot.copy_message.await_count == 1


def test_dry_run_records_skip_without_copy(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(dry_run=True),
        repositories,
        SimpleNamespace(index_copy=Mock()),
    )
    update = SimpleNamespace(
        effective_message=_message("#Japan"),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )
    bot = SimpleNamespace(id=50, copy_message=AsyncMock())

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    assert repositories.get_delivery(-100, 12, -200, 9).status == "skipped"
    bot.copy_message.assert_not_awaited()


def test_copy_failure_is_recorded_and_propagated(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock()),
    )
    update = SimpleNamespace(
        effective_message=_message("#Japan"),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )
    bot = SimpleNamespace(id=50, copy_message=AsyncMock(side_effect=RuntimeError("down")))

    with pytest.raises(RuntimeError, match="down"):
        asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    assert repositories.get_delivery(-100, 12, -200, 9).status == "failed"


def test_transient_copy_failure_retries_without_dead_letter(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    update = SimpleNamespace(
        effective_message=_message("#Japan"),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(
            side_effect=[NetworkError("temporary"), SimpleNamespace(message_id=99)]
        ),
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    assert bot.copy_message.await_count == 2
    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"
    assert repositories.list_dead_letters() == []


def test_miki_authored_message_is_not_sorted(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock()),
    )
    update = SimpleNamespace(
        effective_message=_message("#Japan", sender_id=50),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )
    bot = SimpleNamespace(id=50, copy_message=AsyncMock())

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    bot.copy_message.assert_not_awaited()


def test_album_members_reuse_the_caption_decision_and_preserve_order(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(message_id=90),
                SimpleNamespace(message_id=91),
            ]
        ),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "#Japan",
                    message_id=12,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "",
                    message_id=13,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_awaited_once()
    assert [item.media for item in bot.send_media_group.await_args.kwargs["media"]] == [
        "file-12",
        "file-13",
    ]
    assert bot.send_media_group.await_args.kwargs["media"][0].caption == "#Japan"
    bot.copy_message.assert_not_awaited()
    bot.copy_messages.assert_not_awaited()
    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"
    assert repositories.get_delivery(-100, 13, -200, 9).status == "sent"


def test_short_media_group_response_suppresses_unsafe_fallback(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=90)]),
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=91)),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        for message_id, caption in ((12, "#Japan"), (13, "")):
            await service.handle_update(
                SimpleNamespace(
                    effective_message=_message(
                        caption,
                        message_id=message_id,
                        media_group_id="album-short-response",
                    ),
                    effective_chat=chat,
                ),
                context,
            )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_awaited_once()
    bot.copy_message.assert_not_awaited()
    assert repositories.get_delivery(-100, 12, -200, 9).destination_message_id == 90
    assert repositories.get_delivery(-100, 13, -200, 9).status == "failed"
    assert repositories.list_dead_letters()[0]["error_category"] == "outcome_unknown"


def test_shutdown_drains_routable_album_and_cancels_timers(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=90)),
    )
    context = SimpleNamespace(bot=bot)

    async def queue_and_shutdown() -> None:
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "#Japan",
                    media_group_id="album-shutdown",
                ),
                effective_chat=SimpleNamespace(id=-100, type="supergroup"),
            ),
            context,
        )
        await service.shutdown(context)
        assert service._album_flush_tasks == {}
        assert service._pending_albums == {}

    asyncio.run(queue_and_shutdown())

    bot.copy_message.assert_awaited_once()


def test_mixed_ten_item_photo_video_album_is_sent_as_one_group(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(
            return_value=[SimpleNamespace(message_id=message_id) for message_id in range(90, 100)]
        ),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        for offset in range(10):
            await service.handle_update(
                SimpleNamespace(
                    effective_message=_message(
                        "#Japan" if offset == 0 else "",
                        message_id=12 + offset,
                        media_group_id="album-1",
                        media_kind="video" if offset == 9 else "photo",
                    ),
                    effective_chat=chat,
                ),
                context,
            )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_awaited_once()
    media = bot.send_media_group.await_args.kwargs["media"]
    assert [item.media for item in media] == [f"file-{message_id}" for message_id in range(12, 22)]
    assert [item.__class__.__name__ for item in media] == [
        *["InputMediaPhoto"] * 9,
        "InputMediaVideo",
    ]
    bot.copy_message.assert_not_awaited()
    assert [
        repositories.get_delivery(-100, message_id, -200, 9).status for message_id in range(12, 22)
    ] == ["sent"] * 10


def test_multiple_hashtags_for_same_topic_forward_ten_media_album_once(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    for value in ("男男", "去马赛克", "demosaiced", "AI字幕", "无码", "AI画质增强"):
        repositories.add_mapping(-200, 9, "hashtag", value, 1)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(
            return_value=[SimpleNamespace(message_id=message_id) for message_id in range(90, 100)]
        ),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        for offset in range(10):
            await service.handle_update(
                SimpleNamespace(
                    effective_message=_message(
                        "#男男 #去马赛克 #demosaiced #AI字幕 #无码 #AI画质增强"
                        if offset == 0
                        else "",
                        message_id=12 + offset,
                        media_group_id="album-1",
                        media_kind="video" if offset == 9 else "photo",
                    ),
                    effective_chat=chat,
                ),
                context,
            )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_awaited_once()
    bot.copy_message.assert_not_awaited()
    assert [
        repositories.get_delivery(-100, message_id, -200, 9).status for message_id in range(12, 22)
    ] == ["sent"] * 10
    assert repositories.metrics_snapshot()["sort_deliveries"] == 10


@pytest.mark.parametrize("media_kind", ["audio", "document"])
def test_homogeneous_non_visual_album_is_sent_as_one_group(
    database_connection,
    media_kind: str,
) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(message_id=90),
                SimpleNamespace(message_id=91),
                SimpleNamespace(message_id=92),
            ]
        ),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        for offset in range(3):
            await service.handle_update(
                SimpleNamespace(
                    effective_message=_message(
                        "#Japan" if offset == 0 else "",
                        message_id=12 + offset,
                        media_group_id="album-1",
                        media_kind=media_kind,
                    ),
                    effective_chat=chat,
                ),
                context,
            )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_awaited_once()
    assert [item.media for item in bot.send_media_group.await_args.kwargs["media"]] == [
        "file-12",
        "file-13",
        "file-14",
    ]
    bot.copy_message.assert_not_awaited()
    assert [
        repositories.get_delivery(-100, message_id, -200, 9).status for message_id in range(12, 15)
    ] == ["sent"] * 3


def test_unsupported_mixed_media_album_falls_back_and_forwards_every_member(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=90),
                SimpleNamespace(message_id=91),
                SimpleNamespace(message_id=92),
                SimpleNamespace(message_id=93),
            ]
        ),
        send_media_group=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        for offset, media_kind in enumerate(("photo", "video", "document", "audio")):
            await service.handle_update(
                SimpleNamespace(
                    effective_message=_message(
                        "#Japan" if offset == 0 else "",
                        message_id=12 + offset,
                        media_group_id="album-1",
                        media_kind=media_kind,
                    ),
                    effective_chat=chat,
                ),
                context,
            )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_not_awaited()
    assert [call.kwargs["message_id"] for call in bot.copy_message.await_args_list] == [
        12,
        13,
        14,
        15,
    ]
    assert [
        repositories.get_delivery(-100, message_id, -200, 9).status for message_id in range(12, 16)
    ] == ["sent"] * 4


def test_album_members_wait_for_later_caption_decision(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(message_id=90),
                SimpleNamespace(message_id=91),
            ]
        ),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "",
                    message_id=12,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "#Japan",
                    message_id=13,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_awaited_once()
    assert [item.media for item in bot.send_media_group.await_args.kwargs["media"]] == [
        "file-12",
        "file-13",
    ]
    assert bot.send_media_group.await_args.kwargs["media"][1].caption == "#Japan"
    bot.copy_message.assert_not_awaited()
    bot.copy_messages.assert_not_awaited()
    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"
    assert repositories.get_delivery(-100, 13, -200, 9).status == "sent"


def test_unmatched_caption_album_member_waits_for_later_route(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(message_id=90),
                SimpleNamespace(message_id=91),
            ]
        ),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "album intro",
                    message_id=12,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "#Japan",
                    message_id=13,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_awaited_once()
    assert [item.media for item in bot.send_media_group.await_args.kwargs["media"]] == [
        "file-12",
        "file-13",
    ]
    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"
    assert repositories.get_delivery(-100, 13, -200, 9).status == "sent"


def test_album_route_can_be_derived_from_combined_member_captions(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(message_id=90),
                SimpleNamespace(message_id=91),
            ]
        ),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "New",
                    message_id=12,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "York",
                    message_id=13,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_awaited_once()
    assert [item.media for item in bot.send_media_group.await_args.kwargs["media"]] == [
        "file-12",
        "file-13",
    ]
    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"
    assert repositories.get_delivery(-100, 13, -200, 9).status == "sent"


def test_decisionless_album_stays_pending_until_max_wait_expires(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    service._album_flush_delay = 0.01
    service._album_max_wait = 60
    bot = SimpleNamespace(id=50, copy_message=AsyncMock(), send_media_group=AsyncMock())
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "",
                    message_id=12,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await asyncio.gather(*service._album_flush_tasks.values())
        assert (-100, "album-1") in service._pending_albums
        service._pending_albums.pop((-100, "album-1"), None)
        for task in tuple(service._album_flush_tasks.values()):
            task.cancel()
        await asyncio.gather(*service._album_flush_tasks.values(), return_exceptions=True)

    asyncio.run(run_album())

    bot.copy_message.assert_not_awaited()
    bot.send_media_group.assert_not_awaited()


def test_unsupported_album_member_falls_back_to_ordered_copy(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=90),
                SimpleNamespace(message_id=91),
            ]
        ),
        send_media_group=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "#Japan",
                    message_id=12,
                    media_group_id="album-1",
                    media_kind="photo",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "",
                    message_id=13,
                    media_group_id="album-1",
                    media_kind="sticker",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_not_awaited()
    assert [call.kwargs["message_id"] for call in bot.copy_message.await_args_list] == [12, 13]
    bot.copy_messages.assert_not_awaited()
    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"
    assert repositories.get_delivery(-100, 13, -200, 9).status == "sent"


def test_media_group_failure_falls_back_to_ordered_copy(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=90),
                SimpleNamespace(message_id=91),
            ]
        ),
        send_media_group=AsyncMock(side_effect=RuntimeError("group refused")),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "#Japan",
                    message_id=12,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "",
                    message_id=13,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_awaited_once()
    assert [call.kwargs["message_id"] for call in bot.copy_message.await_args_list] == [12, 13]
    bot.copy_messages.assert_not_awaited()
    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"
    assert repositories.get_delivery(-100, 13, -200, 9).status == "sent"
    assert repositories.metrics_snapshot()["media_group_fallbacks"] == 1


def test_individual_album_fallback_continues_after_one_member_fails(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(
            side_effect=[
                BadRequest("member refused"),
                SimpleNamespace(message_id=91),
                SimpleNamespace(message_id=92),
            ]
        ),
        send_media_group=AsyncMock(),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        for offset, media_kind in enumerate(("photo", "document", "photo")):
            await service.handle_update(
                SimpleNamespace(
                    effective_message=_message(
                        "#Japan" if offset == 0 else "",
                        message_id=12 + offset,
                        media_group_id="album-member-failure",
                        media_kind=media_kind,
                    ),
                    effective_chat=chat,
                ),
                context,
            )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_not_awaited()
    assert [call.kwargs["message_id"] for call in bot.copy_message.await_args_list] == [12, 13, 14]
    assert repositories.get_delivery(-100, 12, -200, 9).status == "failed"
    assert repositories.get_delivery(-100, 13, -200, 9).status == "sent"
    assert repositories.get_delivery(-100, 14, -200, 9).status == "sent"
    assert repositories.metrics_snapshot()["album_member_delivery_failures"] == 1


def test_media_group_timeout_is_not_retried_or_fallen_back(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(side_effect=TimedOut("Timed out")),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        for offset in range(10):
            await service.handle_update(
                SimpleNamespace(
                    effective_message=_message(
                        "#Japan" if offset == 0 else "",
                        message_id=12 + offset,
                        media_group_id="album-timeout",
                    ),
                    effective_chat=chat,
                ),
                context,
            )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    assert bot.send_media_group.await_count == 1
    bot.copy_message.assert_not_awaited()
    assert [
        repositories.get_delivery(-100, message_id, -200, 9).status for message_id in range(12, 22)
    ] == ["failed"] * 10
    assert len(repositories.list_dead_letters()) == 10
    assert repositories.metrics_snapshot()["telegram_delivery_outcome_unknown"] == 10


def test_two_back_to_back_ten_item_albums_are_isolated_and_delivered_once(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Inbox")
    service = SortingService(
        _settings(forwarding_pairs=(TopicForwardingPair(5, 9),)),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(
            side_effect=[
                [SimpleNamespace(message_id=100 + offset) for offset in range(10)],
                [SimpleNamespace(message_id=200 + offset) for offset in range(10)],
            ]
        ),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_albums() -> None:
        for album_number, start_id in enumerate((1000, 2000), start=1):
            for offset in range(10):
                await service.handle_update(
                    SimpleNamespace(
                        effective_message=_message(
                            "#Japan #Tokyo #RX7" if offset == 0 else "",
                            message_id=start_id + offset,
                            media_group_id=f"album-{album_number}",
                        ),
                        effective_chat=chat,
                    ),
                    context,
                )
        await service.flush_pending_albums(context)

    asyncio.run(run_albums())

    assert bot.send_media_group.await_count == 2
    assert [len(call.kwargs["media"]) for call in bot.send_media_group.await_args_list] == [10, 10]
    bot.copy_message.assert_not_awaited()
    assert repositories.metrics_snapshot()["sort_deliveries"] == 20


def test_direct_album_reuses_topic_when_later_members_omit_thread_id(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Inbox")
    service = SortingService(
        _settings(forwarding_pairs=(TopicForwardingPair(5, 9),)),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(),
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(message_id=100),
                SimpleNamespace(message_id=101),
                SimpleNamespace(message_id=102),
            ]
        ),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        for offset, thread_id in enumerate((5, None, None)):
            await service.handle_update(
                SimpleNamespace(
                    update_id=500 + offset,
                    effective_message=_message(
                        "",
                        message_id=1000 + offset,
                        media_group_id="album-with-missing-thread-ids",
                        thread_id=thread_id,
                    ),
                    effective_chat=chat,
                ),
                context,
            )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_awaited_once()
    assert len(bot.send_media_group.await_args.kwargs["media"]) == 3
    assert repositories.metrics_snapshot()["sort_deliveries"] == 3


def test_twenty_photos_and_pdf_split_across_telegram_groups_all_forward(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Inbox")
    service = SortingService(
        _settings(forwarding_pairs=(TopicForwardingPair(5, 9),)),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=300)),
        send_media_group=AsyncMock(
            side_effect=[
                [SimpleNamespace(message_id=100 + offset) for offset in range(10)],
                [SimpleNamespace(message_id=200 + offset) for offset in range(10)],
            ]
        ),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_submission() -> None:
        for album_number, start_id in enumerate((1000, 1010), start=1):
            for offset in range(10):
                await service.handle_update(
                    SimpleNamespace(
                        update_id=start_id + offset,
                        effective_message=_message(
                            "",
                            message_id=start_id + offset,
                            media_group_id=f"album-{album_number}",
                        ),
                        effective_chat=chat,
                    ),
                    context,
                )
        await service.handle_update(
            SimpleNamespace(
                update_id=1020,
                effective_message=_message(
                    "",
                    message_id=1020,
                    media_kind="document",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.flush_pending_albums(context)

    asyncio.run(run_submission())

    assert bot.send_media_group.await_count == 2
    assert [len(call.kwargs["media"]) for call in bot.send_media_group.await_args_list] == [10, 10]
    bot.copy_message.assert_awaited_once()
    assert bot.copy_message.await_args.kwargs["message_id"] == 1020
    assert repositories.metrics_snapshot()["sort_deliveries"] == 21


def test_partially_delivered_album_retry_does_not_resend_completed_members(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    completed_job = repositories.enqueue(
        "sort",
        "sort:-100:12:-200:9",
        {
            "source_chat_id": -100,
            "source_message_id": 12,
            "destination_chat_id": -200,
            "destination_thread_id": 9,
            "reason": "hashtag:japan",
        },
    )
    completed_delivery = repositories.ensure_delivery(
        completed_job.id,
        source_chat_id=-100,
        source_message_id=12,
        destination_chat_id=-200,
        destination_thread_id=9,
        reason="hashtag:japan",
    )
    repositories.update_delivery(
        completed_delivery.id,
        "sent",
        destination_message_id=90,
    )
    repositories.update_job(completed_job.id, "completed")
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=91)),
        send_media_group=AsyncMock(
            return_value=[
                SimpleNamespace(message_id=100),
                SimpleNamespace(message_id=101),
            ]
        ),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    async def run_album() -> None:
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "#Japan",
                    message_id=12,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "",
                    message_id=13,
                    media_group_id="album-1",
                ),
                effective_chat=chat,
            ),
            context,
        )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.send_media_group.assert_not_awaited()
    bot.copy_message.assert_awaited_once()
    assert bot.copy_message.await_args.kwargs["message_id"] == 13
    assert repositories.get_delivery(-100, 12, -200, 9).destination_message_id == 90
    assert repositories.get_delivery(-100, 13, -200, 9).destination_message_id == 91
    assert repositories.metrics_snapshot()["media_group_fallbacks"] == 1
    assert repositories.metrics_snapshot()["sort_duplicates"] == 1


def test_single_album_member_flushes_as_one_copy(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=True)),
    )
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=90)),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    async def run_album() -> None:
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "#Japan",
                    message_id=12,
                    media_group_id="album-1",
                ),
                effective_chat=SimpleNamespace(id=-100, type="supergroup"),
            ),
            context,
        )
        await service.flush_pending_albums(context)

    asyncio.run(run_album())

    bot.copy_message.assert_awaited_once()
    bot.copy_messages.assert_not_awaited()
    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"


def test_background_album_flush_consumes_delivery_failures(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    service = SortingService(
        _settings(),
        repositories,
        SimpleNamespace(index_copy=Mock()),
    )
    service._album_flush_delay = 0
    bot = SimpleNamespace(
        id=50,
        copy_message=AsyncMock(side_effect=TimedOut("Timed out")),
        copy_messages=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot)

    async def run_album() -> None:
        await service.handle_update(
            SimpleNamespace(
                effective_message=_message(
                    "#Japan",
                    message_id=12,
                    media_group_id="album-1",
                ),
                effective_chat=SimpleNamespace(id=-100, type="supergroup"),
            ),
            context,
        )
        await asyncio.gather(*service._album_flush_tasks.values())

    asyncio.run(run_album())

    assert repositories.get_delivery(-100, 12, -200, 9).status == "failed"
    assert repositories.metrics_snapshot()["album_flush_failures"] == 1
