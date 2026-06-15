from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from miki_sorter_bot.indexing import MessageIndexer
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.retrieval import (
    RequestValidationError,
    RetrievalService,
    parse_request,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        archive_chat_id=-200,
        effective_request_chat_id=-300,
        request_topic_ids=frozenset({50}),
        requester_bot_ids=frozenset({900}),
        admin_user_ids=frozenset({1}),
        default_request_limit=20,
        max_request_limit=100,
    )


def _media(
    message_id: int,
    caption: str,
    *,
    thread_id: int = 9,
    album: str | None = None,
    created_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        message_thread_id=thread_id,
        media_group_id=album,
        caption=caption,
        text=None,
        date=created_at or datetime(2026, 6, 13, tzinfo=UTC),
        from_user=SimpleNamespace(id=10, is_bot=False),
        photo=[object()],
        animation=None,
        audio=None,
        document=None,
        sticker=None,
        video=None,
        video_note=None,
        voice=None,
    )


def _request_update(
    text: str,
    *,
    message_id: int = 100,
    thread_id: int = 50,
    user_id: int = 10,
    is_bot: bool = False,
) -> SimpleNamespace:
    message = SimpleNamespace(
        message_id=message_id,
        message_thread_id=thread_id,
        text=text,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=-300),
        effective_user=SimpleNamespace(id=user_id, is_bot=is_bot),
    )


def _library(repositories: SqliteRepositories) -> None:
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "keyword", "Tokyo", 1)
    repositories.add_mapping(-200, 9, "keyword", "RX7", 1)
    indexer = MessageIndexer(repositories, bot_id=99)
    now = datetime(2026, 6, 13, tzinfo=UTC)
    indexer.index(_media(1, "Photo in Tokyo with RX7", created_at=now), -200)
    indexer.index(
        _media(2, "Photo in Tokyo", album="album-1", created_at=now - timedelta(days=1)),
        -200,
    )
    indexer.index(
        _media(3, "", album="album-1", created_at=now - timedelta(days=1)),
        -200,
    )
    indexer.index(_media(4, "Photo with RX7", created_at=now - timedelta(days=2)), -200)


def test_parse_request_supports_quoted_phrases_and_defaults() -> None:
    request = parse_request(
        '#request\ntopic: Japan\nkeywords: TOKYO, "Mount Fuji"',
        default_limit=20,
        max_limit=100,
    )

    assert request.topic_reference == "Japan"
    assert request.keywords == ("tokyo", "mount fuji")
    assert request.match_mode == "all"
    assert request.limit == 20


def test_parse_request_normalizes_hashtag_search() -> None:
    request = parse_request(
        "#request\ntopic: Japan\nkeywords: #Japan",
        default_limit=20,
        max_limit=100,
    )

    assert request.keywords == ("japan",)


@pytest.mark.parametrize(
    "text,error",
    [
        ("#request\nkeywords: Tokyo", "Missing required"),
        ("#request\ntopic: Japan\nkeywords: Tokyo\nextra: no", "Unknown field"),
        ("#request\ntopic: Japan\nkeywords: Tokyo\nmatch: maybe", "match must"),
        ("#request\ntopic: Japan\nkeywords: Tokyo\nlimit: 101", "between 1 and 100"),
    ],
)
def test_parse_request_rejects_invalid_forms(text: str, error: str) -> None:
    with pytest.raises(RequestValidationError, match=error):
        parse_request(text, default_limit=20, max_limit=100)


def test_search_supports_all_any_newest_first_and_album_dedup(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _library(repositories)

    all_results = repositories.search_posts(-200, 9, ("tokyo", "rx7"), "all", 10)
    any_results = repositories.search_posts(-200, 9, ("tokyo", "rx7"), "any", 2)

    assert [post.source_message_id for post in all_results] == [1]
    assert [post.source_message_id for post in any_results] == [1, 2, 3]
    assert any_results[1].logical_post_key == any_results[2].logical_post_key


def test_all_matching_aggregates_tokens_across_album_members(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "keyword", "Tokyo", 1)
    repositories.add_mapping(-200, 9, "keyword", "RX7", 1)
    indexer = MessageIndexer(repositories, bot_id=99)
    indexer.index(_media(10, "Tokyo", album="album-split"), -200)
    indexer.index(_media(11, "RX7", album="album-split"), -200)

    results = repositories.search_posts(-200, 9, ("tokyo", "rx7"), "all", 10)

    assert [post.source_message_id for post in results] == [10, 11]


def test_human_request_copies_results_and_reports_summary(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    service = RetrievalService(_settings(), repositories)
    update = _request_update(
        "#request\ntopic: Japan\nkeywords: Tokyo\nmatch: any\nlimit: 10"
    )
    bot = SimpleNamespace(
        copy_message=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=201),
                SimpleNamespace(message_id=202),
                SimpleNamespace(message_id=203),
            ]
        )
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    assert [call.kwargs["message_id"] for call in bot.copy_message.await_args_list] == [1, 2, 3]
    replies = [call.args[0] for call in update.effective_message.reply_text.await_args_list]
    assert replies[0].endswith("queued.")
    assert "2 matched, 3 copied" in replies[-1]


def test_replayed_request_does_not_copy_again(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\ntopic: 9\nkeywords: RX7")
    bot = SimpleNamespace(
        copy_message=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=201),
                SimpleNamespace(message_id=202),
            ]
        )
    )
    context = SimpleNamespace(bot=bot)

    asyncio.run(service.handle_update(update, context))
    asyncio.run(service.handle_update(update, context))

    assert bot.copy_message.await_count == 2
    assert "already completed" in update.effective_message.reply_text.await_args_list[-1].args[0]


def test_request_topic_and_bot_authorization_are_enforced(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    service = RetrievalService(_settings(), repositories)
    wrong_topic = _request_update("#request\ntopic: Japan\nkeywords: Tokyo", thread_id=99)
    wrong_chat = _request_update("#request\ntopic: Japan\nkeywords: Tokyo")
    wrong_chat.effective_chat.id = -999
    bot_request = _request_update(
        "#request\ntopic: Japan\nkeywords: Tokyo",
        user_id=901,
        is_bot=True,
    )
    context = SimpleNamespace(bot=SimpleNamespace(copy_message=AsyncMock()))

    asyncio.run(service.handle_update(wrong_topic, context))
    asyncio.run(service.handle_update(wrong_chat, context))
    asyncio.run(service.handle_update(bot_request, context))

    assert "not allowed" in wrong_topic.effective_message.reply_text.await_args.args[0]
    assert "not allowed" in wrong_chat.effective_message.reply_text.await_args.args[0]
    assert "not authorized" in bot_request.effective_message.reply_text.await_args.args[0]
    context.bot.copy_message.assert_not_awaited()


def test_no_results_produces_summary_without_copy(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\ntopic: Japan\nkeywords: MISSING")
    bot = SimpleNamespace(copy_message=AsyncMock())

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    bot.copy_message.assert_not_awaited()
    assert "0 matched, 0 copied" in update.effective_message.reply_text.await_args_list[-1].args[0]


def test_admin_can_cancel_pending_retrieval_job(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    service = RetrievalService(_settings(), repositories)
    job = repositories.enqueue("retrieve", "retrieve:test", {})
    update = _request_update("/request_cancel " + str(job.id), user_id=1)

    asyncio.run(service.cancel(update, SimpleNamespace()))

    assert repositories.get_job(job.id).status == "cancelled"
    assert "cancellation recorded" in update.effective_message.reply_text.await_args.args[0]


def test_failed_items_resume_without_recopied_successes(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\ntopic: Japan\nkeywords: RX7")
    first_bot = SimpleNamespace(
        copy_message=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=201),
                RuntimeError("temporary"),
            ]
        )
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=first_bot)))
    job_id = 1
    assert repositories.get_job(job_id).status == "failed"

    second_bot = SimpleNamespace(
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=202))
    )
    asyncio.run(service.handle_update(update, SimpleNamespace(bot=second_bot)))

    assert second_bot.copy_message.await_count == 1
    assert repositories.get_job(job_id).status == "completed"


def test_execution_stops_after_cancellation_between_items(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\ntopic: Japan\nkeywords: Tokyo")

    async def copy_then_cancel(**_: object) -> SimpleNamespace:
        repositories.cancel_job(1, "retrieve")
        return SimpleNamespace(message_id=201)

    bot = SimpleNamespace(copy_message=AsyncMock(side_effect=copy_then_cancel))

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    assert bot.copy_message.await_count == 1
    assert repositories.get_job(1).status == "cancelled"
    assert "cancelled" in update.effective_message.reply_text.await_args_list[-1].args[0]


def test_missing_source_is_marked_unavailable_and_removed_from_search(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\ntopic: Japan\nkeywords: Tokyo\nlimit: 1")
    bot = SimpleNamespace(
        copy_message=AsyncMock(side_effect=BadRequest("Message to copy not found"))
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    assert "1 unavailable" in update.effective_message.reply_text.await_args_list[-1].args[0]
    remaining = repositories.search_posts(-200, 9, ("tokyo",), "any", 10)
    assert all(post.source_message_id != 1 for post in remaining)
