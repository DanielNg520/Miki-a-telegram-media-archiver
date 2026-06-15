from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import NetworkError

from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.sorting import RouteMatcher, SortingService


def _settings(*, dry_run: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        source_chat_id=-100,
        source_thread_id=5,
        archive_chat_id=-200,
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
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        message_thread_id=5,
        caption=caption,
        text=None,
        from_user=SimpleNamespace(id=sender_id, is_bot=False),
        media_group_id=media_group_id,
        photo=[object()] if media else [],
        animation=None,
        audio=None,
        document=None,
        sticker=None,
        video=None,
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


def test_matcher_enforces_hashtag_precedence_and_exact_tokens(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _routes(repositories)
    matcher = RouteMatcher(repositories, -200)

    hashtag = matcher.decide("ABC but explicitly #Japan")
    substring = matcher.decide("ABCDEF only")
    phrase = matcher.decide("Visit new york today")

    assert hashtag.status == "matched"
    assert hashtag.topic.thread_id == 9
    assert hashtag.reason == "hashtag:japan"
    assert substring.status == "unmatched"
    assert phrase.topic.thread_id == 9


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
        copy_message=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=90),
                SimpleNamespace(message_id=91),
            ]
        ),
    )
    context = SimpleNamespace(bot=bot)
    chat = SimpleNamespace(id=-100, type="supergroup")

    asyncio.run(
        service.handle_update(
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
    )
    asyncio.run(
        service.handle_update(
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
    )

    assert [call.kwargs["message_id"] for call in bot.copy_message.await_args_list] == [12, 13]
    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"
    assert repositories.get_delivery(-100, 13, -200, 9).status == "sent"
