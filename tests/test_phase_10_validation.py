from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from miki_sorter_bot.config import IntegrationClient
from miki_sorter_bot.indexing import MessageIndexer
from miki_sorter_bot.integrations import IntegrationService, sign_request
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.retrieval import RetrievalService
from miki_sorter_bot.sorting import SortingService

NOW = 1_750_000_000
SECRET = "phase-ten-secret-value"


def _sort_settings() -> SimpleNamespace:
    return SimpleNamespace(
        source_chat_id=-100,
        source_thread_id=5,
        archive_chat_id=-200,
        effective_request_chat_id=-200,
        sort_dry_run=False,
        send_confirmation=False,
    )


def _media(
    message_id: int,
    caption: str,
    *,
    thread_id: int = 9,
    sender_id: int = 10,
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        message_thread_id=thread_id,
        media_group_id=None,
        caption=caption,
        text=None,
        date=datetime(2026, 6, 13, tzinfo=UTC),
        from_user=SimpleNamespace(id=sender_id, is_bot=False),
        photo=[object()],
        animation=None,
        audio=None,
        document=None,
        sticker=None,
        video=None,
        video_note=None,
        voice=None,
        reply_text=AsyncMock(),
    )


def test_edited_sort_update_does_not_create_a_second_delivery(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "keyword", "Tokyo", 1)
    indexing = SimpleNamespace(index_copy=Mock(return_value=True))
    service = SortingService(_sort_settings(), repositories, indexing)
    bot = SimpleNamespace(
        id=99,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=200)),
    )
    chat = SimpleNamespace(id=-100, type="supergroup")

    for caption in ("Tokyo photo", "Tokyo photo edited"):
        update = SimpleNamespace(
            effective_message=_media(12, caption, thread_id=5),
            effective_chat=chat,
        )
        asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    assert bot.copy_message.await_count == 1
    assert indexing.index_copy.call_count == 1
    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"
    assert repositories.metrics_snapshot()["sort_duplicates"] == 1


def test_large_retrieval_is_bounded_by_requested_limit(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "keyword", "Tokyo", 1)
    indexer = MessageIndexer(repositories, bot_id=99)
    for message_id in range(1, 121):
        indexer.index(_media(message_id, "Tokyo"), -200)
    settings = SimpleNamespace(
        archive_chat_id=-200,
        effective_request_chat_id=-200,
        request_topic_ids=frozenset({50}),
        requester_bot_ids=frozenset(),
        admin_user_ids=frozenset({1}),
        default_request_limit=20,
        max_request_limit=100,
    )
    message = SimpleNamespace(
        message_id=500,
        message_thread_id=50,
        text="#request\ntopic: Japan\nkeywords: Tokyo\nlimit: 75",
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=-200),
        effective_user=SimpleNamespace(id=10, is_bot=False),
    )
    bot = SimpleNamespace(
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=900))
    )

    asyncio.run(
        RetrievalService(settings, repositories).handle_update(
            update,
            SimpleNamespace(bot=bot),
        )
    )

    assert bot.copy_message.await_count == 75
    assert "75 matched, 75 copied" in message.reply_text.await_args_list[-1].args[0]


@pytest.mark.parametrize("kind", ("sort", "retrieve"))
def test_restart_recovers_only_interrupted_running_jobs(database_connection, kind) -> None:
    repositories = SqliteRepositories(database_connection)
    running = repositories.enqueue(kind, f"{kind}:running", {})
    pending = repositories.enqueue(kind, f"{kind}:pending", {})
    completed = repositories.enqueue(kind, f"{kind}:completed", {})
    failed = repositories.enqueue(kind, f"{kind}:failed", {})
    cancelled = repositories.enqueue(kind, f"{kind}:cancelled", {})
    repositories.update_job(running.id, "running")
    repositories.update_job(completed.id, "completed")
    repositories.update_job(failed.id, "failed")
    repositories.update_job(cancelled.id, "cancelled")

    assert repositories.recover_interrupted_jobs() == 1
    assert repositories.get_job(running.id).status == "pending"
    assert repositories.get_job(pending.id).status == "pending"
    assert repositories.get_job(completed.id).status == "completed"
    assert repositories.get_job(failed.id).status == "failed"
    assert repositories.get_job(cancelled.id).status == "cancelled"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"not-json", "invalid_json"),
        (b"[]", "invalid_request"),
        (
            json.dumps(
                {
                    "version": 99,
                    "request_id": "bad-version",
                    "operation": "route.preview",
                    "data": {},
                }
            ).encode(),
            "unsupported_version",
        ),
        (
            json.dumps(
                {
                    "version": 1,
                    "request_id": "bad-operation",
                    "operation": "does.not.exist",
                    "data": {},
                }
            ).encode(),
            "unknown_operation",
        ),
    ],
)
def test_integration_malformed_payloads_return_stable_errors(
    database_connection,
    body,
    expected,
) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = SimpleNamespace(
        integration_clients=(
            IntegrationClient("phase-ten", SECRET, frozenset({"submit"}), 20),
        ),
        integration_signature_ttl=300,
        archive_chat_id=-200,
        default_request_limit=20,
        max_request_limit=100,
    )
    service = IntegrationService(
        settings,
        repositories,
        SimpleNamespace(),
        now=lambda: NOW,
    )
    timestamp = str(NOW)
    nonce = f"nonce-{expected}"

    response = service.dispatch(
        body,
        client_id="phase-ten",
        timestamp=timestamp,
        nonce=nonce,
        signature=sign_request(SECRET, timestamp, nonce, body),
    )

    assert response.status == 400
    assert response.body["error"]["code"] == expected


def test_miki_output_cannot_loop_back_into_sorting(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "hashtag", "Japan", 1)
    service = SortingService(
        _sort_settings(),
        repositories,
        SimpleNamespace(index_copy=Mock()),
    )
    bot = SimpleNamespace(id=99, copy_message=AsyncMock())
    update = SimpleNamespace(
        effective_message=_media(12, "#Japan", thread_id=5, sender_id=99),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )

    asyncio.run(service.handle_update(update, SimpleNamespace(bot=bot)))

    bot.copy_message.assert_not_awaited()
    assert repositories.operational_status()["jobs"] == {}
