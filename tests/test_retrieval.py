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


def _settings(default_request_limit: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        archive_chat_id=-200,
        effective_request_chat_id=-300,
        request_topic_ids=frozenset({50}),
        requester_bot_ids=frozenset({900}),
        admin_user_ids=frozenset({1}),
        default_request_limit=default_request_limit,
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


def test_parse_request_marks_explicit_limit() -> None:
    default = parse_request(
        "#request\ntopic: Japan\nkeywords: Tokyo", default_limit=10, max_limit=100
    )
    explicit = parse_request(
        "#request\ntopic: Japan\nkeywords: Tokyo\nlimit: 5", default_limit=10, max_limit=100
    )

    assert (default.limit, default.limit_explicit) == (10, False)
    assert (explicit.limit, explicit.limit_explicit) == (5, True)


def test_invalid_request_reply_includes_worked_example(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\nkeywords: Tokyo")  # missing topic

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=SimpleNamespace())))

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "Invalid request" in reply
    assert "#request" in reply and "topic:" in reply


def test_too_many_results_lists_instead_of_copying(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "keyword", "Tokyo", 1)
    indexer = MessageIndexer(repositories, bot_id=99)
    now = datetime(2026, 6, 13, tzinfo=UTC)
    for message_id in range(1, 16):  # 15 standalone matches > default limit of 10
        indexer.index(_media(message_id, f"Tokyo shot {message_id}", created_at=now), -200)
    service = RetrievalService(_settings(default_request_limit=10), repositories)
    update = _request_update("#request\ntopic: Japan\nkeywords: Tokyo")
    bot = SimpleNamespace(copy_message=AsyncMock(), copy_messages=AsyncMock())

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    bot.copy_message.assert_not_awaited()
    bot.copy_messages.assert_not_awaited()
    reply = update.effective_message.reply_text.await_args_list[-1].args[0]
    assert "more than 10" in reply
    assert "1. Tokyo shot" in reply
    assert repositories.metrics_snapshot()["retrieval_overflow_prompts"] == 1


def test_explicit_limit_skips_narrowing_prompt(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "keyword", "Tokyo", 1)
    indexer = MessageIndexer(repositories, bot_id=99)
    for message_id in range(1, 16):
        indexer.index(_media(message_id, f"Tokyo shot {message_id}"), -200)
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\ntopic: Japan\nkeywords: Tokyo\nlimit: 3")
    bot = SimpleNamespace(copy_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    assert bot.copy_message.await_count == 3
    assert "3 matched, 3 copied" in update.effective_message.reply_text.await_args_list[-1].args[0]


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
    update = _request_update("#request\ntopic: Japan\nkeywords: Tokyo\nmatch: any\nlimit: 10")
    bot = SimpleNamespace(
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=201)),
        copy_messages=AsyncMock(
            return_value=[
                SimpleNamespace(message_id=202),
                SimpleNamespace(message_id=203),
            ]
        ),
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    # The standalone post is copied individually; the album members go out as one batch.
    assert [call.kwargs["message_id"] for call in bot.copy_message.await_args_list] == [1]
    assert bot.copy_messages.await_args_list[0].kwargs["message_ids"] == [2, 3]
    replies = [call.args[0] for call in update.effective_message.reply_text.await_args_list]
    assert replies[0].endswith("queued.")
    assert "2 matched, 3 copied (1 as album)" in replies[-1]


def test_oversized_album_is_split_into_chunks_of_ten(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "keyword", "Tokyo", 1)
    indexer = MessageIndexer(repositories, bot_id=99)
    for message_id in range(1, 13):  # 12 members of one album
        indexer.index(_media(message_id, "Tokyo", album="big-album"), -200)
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\ntopic: Japan\nkeywords: Tokyo\nlimit: 20")
    bot = SimpleNamespace(
        copy_messages=AsyncMock(
            side_effect=lambda **kw: [
                SimpleNamespace(message_id=900 + mid) for mid in kw["message_ids"]
            ]
        )
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    batches = [call.kwargs["message_ids"] for call in bot.copy_messages.await_args_list]
    assert batches == [list(range(1, 11)), [11, 12]]
    assert (
        "12 copied (2 as albums)" in update.effective_message.reply_text.await_args_list[-1].args[0]
    )


def test_album_batch_failure_falls_back_to_single_copies(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\ntopic: Japan\nkeywords: Tokyo\nmatch: any\nlimit: 10")
    bot = SimpleNamespace(
        copy_message=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=201),
                SimpleNamespace(message_id=202),
                SimpleNamespace(message_id=203),
            ]
        ),
        copy_messages=AsyncMock(side_effect=BadRequest("Too many requests")),
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    # Standalone post plus the two album members each retried via copy_message.
    assert [call.kwargs["message_id"] for call in bot.copy_message.await_args_list] == [1, 2, 3]
    assert "2 matched, 3 copied" in update.effective_message.reply_text.await_args_list[-1].args[0]


def test_album_batch_seam_builds_real_copy_messages_call(database_connection) -> None:
    """Seam test: drive a real telegram.Bot and capture the wire-level API call."""
    from unittest.mock import patch

    from telegram import Bot

    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\ntopic: Japan\nkeywords: Tokyo\nmatch: any\nlimit: 10")

    calls: list[tuple[str, dict]] = []

    async def fake_post(self: object, endpoint: str, data: dict, **_: object) -> object:
        calls.append((endpoint, data))
        if endpoint == "copyMessages":
            return [{"message_id": 900 + i} for i in range(len(data["message_ids"]))]
        return {"message_id": 800}

    bot = Bot("123:abc")
    with patch.object(Bot, "_post", fake_post):
        asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    batch = next(data for endpoint, data in calls if endpoint == "copyMessages")
    assert batch["from_chat_id"] == -200
    assert batch["message_ids"] == [2, 3]
    assert batch["message_thread_id"] == 50
    assert "2 matched, 3 copied" in update.effective_message.reply_text.await_args_list[-1].args[0]


def test_request_topic_ids_override_unlocks_new_topic(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    # Settings default forbids topic 50; the requester posts in topic 77.
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\ntopic: Japan\nkeywords: Tokyo\nlimit: 1", thread_id=77)
    bot = SimpleNamespace(copy_message=AsyncMock(return_value=SimpleNamespace(message_id=201)))

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))
    assert "not allowed in this topic" in update.effective_message.reply_text.await_args.args[0]

    # Operator opens topic 77 for requests at runtime; no restart, no new service.
    repositories.set_runtime_setting("request_topic_ids", "77")
    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))
    replies = [call.args[0] for call in update.effective_message.reply_text.await_args_list]
    assert replies[-1].startswith("Request")
    bot.copy_message.assert_awaited()


def test_album_unknown_outcome_does_not_resend_individually(database_connection) -> None:
    from telegram.error import TimedOut

    repositories = SqliteRepositories(database_connection)
    _library(repositories)
    service = RetrievalService(_settings(), repositories)
    update = _request_update("#request\ntopic: Japan\nkeywords: Tokyo\nmatch: any\nlimit: 10")
    bot = SimpleNamespace(
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=201)),
        copy_messages=AsyncMock(side_effect=TimedOut()),
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    # Standalone post still copied; the album is NOT re-sent per message (would duplicate).
    assert [call.kwargs["message_id"] for call in bot.copy_message.await_args_list] == [1]
    assert "2 failed" in update.effective_message.reply_text.await_args_list[-1].args[0]


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
