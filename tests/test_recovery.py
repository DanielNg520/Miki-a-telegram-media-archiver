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


def test_recovered_sort_job_completes_with_confirmations_enabled(database_connection) -> None:
    """A recovered job replays without the original Telegram message, so the
    confirmation step must not flip the committed delivery back to failed."""

    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Inbox")
    settings = _settings()
    settings.send_confirmation = True
    sorting = SortingService(
        settings,
        repositories,
        SimpleNamespace(index_copy=Mock(return_value=False)),
    )
    recovery = JobRecoveryService(repositories, sorting, RetrievalService(settings, repositories))
    job = repositories.enqueue(
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
    bot = SimpleNamespace(
        id=99,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=201)),
    )

    recovered = asyncio.run(recovery.run_once(SimpleNamespace(bot=bot)))

    assert recovered == 1
    assert repositories.get_job(job.id).status == "completed"
    assert repositories.list_dead_letters() == []


def _strand_album_member(
    repositories: SqliteRepositories,
    source_message_id: int,
) -> int:
    """Reproduce what an outcome_unknown album upload leaves behind: a failed
    job plus a delivery that never got a destination message id."""

    job = repositories.enqueue(
        "sort",
        f"sort:-100:{source_message_id}:-200:9",
        {
            "source_chat_id": -100,
            "source_message_id": source_message_id,
            "destination_chat_id": -200,
            "destination_thread_id": 9,
            "reason": "hashtag:tokyo",
            "delivery_method": "send_media_group",
        },
    )
    delivery = repositories.ensure_delivery(
        job.id,
        source_chat_id=-100,
        source_message_id=source_message_id,
        destination_chat_id=-200,
        destination_thread_id=9,
        reason="hashtag:tokyo",
    )
    repositories.claim_job(job.id)
    repositories.update_delivery(delivery.id, "failed", reason="outcome unknown after timeout")
    repositories.update_job(job.id, "failed", error="delivery outcome unknown after timeout")
    repositories.add_dead_letter(
        job.id,
        "sort_media_group_uncertain",
        job.payload,
        "outcome_unknown",
        "delivery outcome unknown after timeout",
    )
    return job.id


def _recovery_for(repositories: SqliteRepositories) -> JobRecoveryService:
    settings = _settings()
    return JobRecoveryService(
        repositories,
        SortingService(
            settings,
            repositories,
            SimpleNamespace(index_copy=Mock(return_value=False)),
        ),
        RetrievalService(settings, repositories),
        failed_cooldown_minutes=0,
    )


def test_undelivered_failed_album_members_are_retried(database_connection) -> None:
    """The whole point: a grouped upload that timed out marks every member's job
    failed, and the pending sweep never looks at failed jobs. Without this the
    siblings of the first member stay stranded for good."""

    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Inbox")
    recovery = _recovery_for(repositories)
    first = _strand_album_member(repositories, 61878)
    second = _strand_album_member(repositories, 61879)
    bot = SimpleNamespace(
        id=99,
        copy_message=AsyncMock(
            side_effect=[SimpleNamespace(message_id=301), SimpleNamespace(message_id=302)]
        ),
    )

    recovered = asyncio.run(recovery.run_once(SimpleNamespace(bot=bot)))

    assert recovered == 2
    assert repositories.get_job(first).status == "completed"
    assert repositories.get_job(second).status == "completed"
    assert bot.copy_message.await_count == 2
    assert repositories.metrics_snapshot()["failed_jobs_retried"] == 2
    # The original dead letters are resolved once the job completes.
    assert all(entry["resolved_at"] is not None for entry in repositories.list_dead_letters())


def test_failed_job_retry_skips_members_already_delivered(database_connection) -> None:
    """A member Telegram did accept has a destination message id recorded;
    re-copying it would duplicate it in the archive."""

    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Inbox")
    job_id = _strand_album_member(repositories, 61878)
    delivery = repositories.get_delivery(-100, 61878, -200, 9)
    repositories.update_delivery(delivery.id, "sent", destination_message_id=555)
    repositories.update_job(job_id, "failed", error="delivery outcome unknown after timeout")

    assert repositories.list_undelivered_failed_jobs(100) == []


def test_failed_job_retry_gives_up_after_max_attempts(database_connection) -> None:
    """A job that can never succeed must stop being re-selected, and must not
    add a fresh dead letter on every sweep."""

    repositories = SqliteRepositories(database_connection)
    # No topic registered, so resume_job raises "targets an inactive topic".
    recovery = _recovery_for(repositories)
    job_id = _strand_album_member(repositories, 61879)
    context = SimpleNamespace(bot=SimpleNamespace(id=99, copy_message=AsyncMock()))

    for _ in range(6):
        assert asyncio.run(recovery.run_once(context)) == 0

    assert repositories.get_job(job_id).attempts >= 5
    assert repositories.list_undelivered_failed_jobs(100) == []
    # One dead letter from the original failure; the retries must not pile on.
    assert len(repositories.list_dead_letters()) == 1
