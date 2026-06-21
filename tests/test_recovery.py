from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from miki_sorter_bot.indexing import MessageIndexer
from miki_sorter_bot.recovery import JobRecoveryService
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.retrieval import RetrievalService
from miki_sorter_bot.sorting import SortingService


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        source_chat_id=-100,
        source_thread_id=5,
        archive_chat_id=-200,
        sort_dry_run=False,
        send_confirmation=False,
        max_request_limit=100,
    )


def _media(message_id: int, caption: str) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        message_thread_id=9,
        media_group_id=None,
        caption=caption,
        text=None,
        date=datetime(2026, 6, 19, tzinfo=UTC),
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


def test_recovery_coordinator_replays_sort_and_retrieval_jobs(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Inbox")
    repositories.add_mapping(-200, 9, "keyword", "Tokyo", 1)
    MessageIndexer(repositories, bot_id=99).index(_media(70, "Tokyo"), -200)
    settings = _settings()
    sorting = SortingService(
        settings,
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=False)),
    )
    retrieval = RetrievalService(settings, repositories)
    recovery = JobRecoveryService(repositories, sorting, retrieval)
    sort_job = repositories.enqueue(
        "sort",
        "sort:-100:12:-200:9",
        {
            "source_chat_id": -100,
            "source_message_id": 12,
            "destination_chat_id": -200,
            "destination_thread_id": 9,
            "reason": "forwarding-pair:5->9",
        },
    )
    retrieval_job = repositories.enqueue(
        "retrieve",
        "retrieve:-300:50",
        {
            "request_chat_id": -300,
            "request_thread_id": 50,
            "request_message_id": 500,
            "requester_id": 10,
            "source_thread_id": 9,
            "keywords": ["tokyo"],
            "match": "all",
            "limit": 20,
        },
    )
    bot = SimpleNamespace(
        id=99,
        copy_message=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=201),
                SimpleNamespace(message_id=202),
            ]
        ),
        send_message=AsyncMock(),
    )

    recovered = asyncio.run(recovery.run_once(SimpleNamespace(bot=bot)))

    assert recovered == 2
    assert repositories.get_job(sort_job.id).status == "completed"
    assert repositories.get_job(retrieval_job.id).status == "completed"
    assert bot.copy_message.await_count == 2
    bot.send_message.assert_awaited_once()
    assert repositories.metrics_snapshot()["jobs_recovered"] == 2


def test_recovery_coordinator_dead_letters_invalid_payload(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    recovery = JobRecoveryService(
        repositories,
        SortingService(
            settings,
            repositories,
            SimpleNamespace(index_copy=Mock()),
        ),
        RetrievalService(settings, repositories),
    )
    job = repositories.enqueue("sort", "sort:broken", {"missing": "fields"})

    assert asyncio.run(recovery.run_once(SimpleNamespace(bot=SimpleNamespace()))) == 0

    assert repositories.get_job(job.id).status == "failed"
    assert repositories.list_dead_letters()[0]["operation"] == "job_recovery"
