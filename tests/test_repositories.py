from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from miki_sorter_bot.indexing import MessageIndexer
from miki_sorter_bot.repositories import SqliteRepositories, normalize_mapping


def test_processed_update_claim_is_idempotent(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)

    assert repositories.claim(100, "sort") is True
    assert repositories.claim(100, "sort") is False


def test_job_enqueue_returns_existing_job_for_same_key(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)

    first = repositories.enqueue("sort", "sort:-100:42", {"message_id": 42})
    second = repositories.enqueue("sort", "sort:-100:42", {"message_id": 999})

    assert first.id == second.id
    assert second.payload == {"message_id": 42}


def test_job_claim_is_atomic_and_tracks_attempts(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    job = repositories.enqueue("sort", "sort:claim", {})

    assert repositories.claim_job(job.id) is True
    assert repositories.claim_job(job.id) is False
    claimed = repositories.get_job(job.id)
    assert claimed.status == "running"
    assert claimed.attempts == 1

    repositories.update_job(job.id, "failed", error="retry me")
    assert repositories.claim_job(job.id) is True
    assert repositories.get_job(job.id).attempts == 2


def test_delivery_lineage_is_idempotent(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    job = repositories.enqueue("sort", "sort:1", {"message_id": 42})

    first = repositories.ensure_delivery(
        job.id,
        source_chat_id=-100,
        source_message_id=42,
        destination_chat_id=-200,
        destination_thread_id=9,
        reason="hashtag:japan",
    )
    second = repositories.ensure_delivery(
        job.id,
        source_chat_id=-100,
        source_message_id=42,
        destination_chat_id=-200,
        destination_thread_id=9,
        reason="hashtag:japan",
    )

    assert first.id == second.id


def test_interrupted_jobs_and_dead_letters_can_be_recovered(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    job = repositories.enqueue("retrieve", "retrieve:recover", {})
    repositories.update_job(job.id, "running")

    assert repositories.recover_interrupted_jobs() == 1
    assert repositories.get_job(job.id).status == "pending"

    dead_id = repositories.add_dead_letter(
        job.id,
        "retrieve_copy",
        {"post_id": 1},
        "permission",
        "denied",
    )
    assert repositories.list_dead_letters()[0]["id"] == dead_id
    repositories.update_job(job.id, "failed")
    assert repositories.retry_dead_letter(dead_id)
    assert repositories.get_job(job.id).status == "pending"
    repositories.add_dead_letter(job.id, "retrieve_copy", {}, "transient", "again")

    repositories.update_job(job.id, "completed")
    assert repositories.list_dead_letters() == []


def test_recovery_reports_interrupted_jobs_by_kind(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    sort_job = repositories.enqueue("sort", "sort:recover", {})
    retrieve_job = repositories.enqueue("retrieve", "retrieve:recover-by-kind", {})
    repositories.update_job(sort_job.id, "running")
    repositories.update_job(retrieve_job.id, "running")

    assert repositories.recover_interrupted_jobs_by_kind() == {
        "retrieve": 1,
        "sort": 1,
    }
    assert repositories.recover_interrupted_jobs_by_kind() == {}


def test_topic_lookup_uses_chat_and_thread_identity(database_connection) -> None:
    cursor = database_connection.execute(
        "INSERT INTO topics (chat_id, thread_id, name) VALUES (?, ?, ?)",
        (-100, 7, "Japan"),
    )
    repositories = SqliteRepositories(database_connection)

    topic = repositories.get(-100, 7)

    assert topic is not None
    assert topic.id == cursor.lastrowid
    assert topic.name == "Japan"
    assert repositories.get(-200, 7) is None


def test_topic_registration_is_stable_and_names_are_unique_per_chat(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)

    first = repositories.register_topic(-100, 7, "Japan")
    renamed = repositories.register_topic(-100, 7, "Tokyo")

    assert first.id == renamed.id
    assert renamed.name == "Tokyo"
    with pytest.raises(ValueError, match="already registered"):
        repositories.register_topic(-100, 8, "tokyo")


def test_mapping_add_is_idempotent_and_rejects_cross_topic_conflicts(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-100, 7, "Japan")
    repositories.register_topic(-100, 8, "Codes")

    first = repositories.add_mapping(-100, 7, "keyword", "ABC", 10)
    repeated = repositories.add_mapping(-100, 7, "keyword", "abc", 10)

    assert first.id == repeated.id
    assert first.normalized_value == "abc"
    with pytest.raises(ValueError, match="another topic"):
        repositories.add_mapping(-100, 8, "keyword", "Abc", 10)


def test_mapping_namespaces_are_isolated_by_chat(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-100, 7, "Japan")
    repositories.register_topic(-200, 7, "Japan")

    first = repositories.add_mapping(-100, 7, "keyword", "ABC", 10)
    second = repositories.add_mapping(-200, 7, "keyword", "ABC", 20)

    assert first.id != second.id


def test_counts_recent_source_posts(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    indexer = MessageIndexer(repositories, bot_id=99)
    message = SimpleNamespace(
        message_id=12,
        message_thread_id=7,
        media_group_id=None,
        caption="#JAV",
        text=None,
        date=datetime(2026, 6, 15, tzinfo=UTC),
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
    indexer.index(message, -100)

    assert (
        repositories.count_recent_source_posts(
            -100,
            7,
            datetime(2026, 6, 14, tzinfo=UTC).isoformat(),
        )
        == 1
    )
    assert (
        repositories.count_recent_source_posts(
            -100,
            7,
            datetime(2026, 6, 16, tzinfo=UTC).isoformat(),
        )
        == 0
    )


def test_replace_mapping_moves_it_between_topics(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    first_topic = repositories.register_topic(-100, 7, "Japan")
    second_topic = repositories.register_topic(-100, 8, "Codes")
    repositories.add_mapping(-100, 7, "keyword", "ABC", 10)

    moved = repositories.replace_mapping(-100, 8, "keyword", "abc", 10)

    assert moved.topic_id == second_topic.id
    assert repositories.list_mappings(-100, thread_id=first_topic.thread_id) == []


def test_inactive_topic_rejects_new_mappings(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-100, 7, "Japan")
    repositories.update_topic_state(-100, 7, is_active=False)

    with pytest.raises(ValueError, match="not registered and active"):
        repositories.add_mapping(-100, 7, "hashtag", "#Japan", 10)


def test_route_manager_grants_are_scoped_to_chat(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)

    assert repositories.grant_route_manager(-100, 20, 10)
    assert not repositories.grant_route_manager(-100, 20, 10)
    assert repositories.is_route_manager(-100, 20)
    assert not repositories.is_route_manager(-200, 20)
    assert repositories.revoke_route_manager(-100, 20)


def test_is_manager_is_chat_independent_and_revoke_is_universal(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)

    repositories.grant_route_manager(-100, 20, 10)
    # Recognized regardless of which chat a command arrives in.
    assert repositories.is_manager(20)
    assert not repositories.is_manager(21)

    # A second grant in another chat is independent but still one universal manager.
    repositories.grant_route_manager(-200, 20, 10)
    assert repositories.revoke_manager(20)
    assert not repositories.is_manager(20)
    assert not repositories.revoke_manager(20)


def test_mapping_normalization_distinguishes_tokens_and_phrases() -> None:
    assert normalize_mapping("hashtag", "#Japan") == ("Japan", "japan")
    assert normalize_mapping("keyword", " ABC ") == ("ABC", "abc")
    assert normalize_mapping("phrase", " New   York ") == ("New York", "new york")
    with pytest.raises(ValueError, match="single token"):
        normalize_mapping("keyword", "New York")
