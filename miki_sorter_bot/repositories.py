from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class TopicRecord:
    id: int
    chat_id: int
    thread_id: int
    name: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: int
    kind: str
    status: str
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    id: int
    job_id: int | None
    source_chat_id: int
    source_message_id: int
    destination_chat_id: int
    destination_thread_id: int
    destination_message_id: int | None
    status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class RouteMappingRecord:
    id: int
    topic_id: int
    kind: str
    value: str
    normalized_value: str
    created_by_user_id: int | None


@dataclass(frozen=True, slots=True)
class SearchToken:
    kind: str
    value: str
    normalized_value: str


@dataclass(frozen=True, slots=True)
class IndexedPostInput:
    source_chat_id: int
    source_thread_id: int
    source_message_id: int
    media_group_id: str | None
    logical_post_key: str
    media_type: str
    caption_preview: str | None
    extractor_version: int
    sender_user_id: int | None
    sender_is_bot: bool
    source_kind: str
    message_created_at: str | None


@dataclass(frozen=True, slots=True)
class IndexedPostRecord(IndexedPostInput):
    id: int
    is_available: bool


@dataclass(frozen=True, slots=True)
class RetrievalItemRecord:
    id: int
    job_id: int
    post_id: int
    destination_chat_id: int
    destination_thread_id: int
    destination_message_id: int | None
    status: str
    error: str | None


class TopicRepository(Protocol):
    def get(self, chat_id: int, thread_id: int) -> TopicRecord | None: ...

    def register_topic(self, chat_id: int, thread_id: int, name: str) -> TopicRecord: ...

    def list_topics(self, chat_id: int, *, active_only: bool = True) -> list[TopicRecord]: ...

    def update_topic_state(
        self,
        chat_id: int,
        thread_id: int,
        *,
        is_active: bool | None = None,
        name: str | None = None,
    ) -> bool: ...


class RouteMappingRepository(Protocol):
    def add_mapping(
        self,
        chat_id: int,
        thread_id: int,
        kind: str,
        value: str,
        created_by_user_id: int,
    ) -> RouteMappingRecord: ...

    def remove_mapping(self, chat_id: int, thread_id: int, kind: str, value: str) -> bool: ...

    def replace_mapping(
        self,
        chat_id: int,
        thread_id: int,
        kind: str,
        value: str,
        created_by_user_id: int,
    ) -> RouteMappingRecord: ...

    def list_mappings(
        self,
        chat_id: int,
        *,
        thread_id: int | None = None,
        kind: str | None = None,
    ) -> list[RouteMappingRecord]: ...


class AuthorizationRepository(Protocol):
    def is_route_manager(self, chat_id: int, user_id: int) -> bool: ...

    def is_manager(self, user_id: int) -> bool: ...

    def grant_route_manager(
        self,
        chat_id: int,
        user_id: int,
        granted_by_user_id: int,
    ) -> bool: ...

    def revoke_route_manager(self, chat_id: int, user_id: int) -> bool: ...

    def revoke_manager(self, user_id: int) -> bool: ...


class ProcessedUpdateRepository(Protocol):
    def claim(self, update_id: int, operation: str) -> bool: ...


class JobRepository(Protocol):
    def enqueue(self, kind: str, idempotency_key: str, payload: dict[str, Any]) -> JobRecord: ...

    def claim_job(self, job_id: int) -> bool: ...

    def update_job(self, job_id: int, status: str, *, error: str | None = None) -> None: ...


class PostRepository(Protocol):
    def upsert_post(
        self,
        post: IndexedPostInput,
        tokens: frozenset[SearchToken],
    ) -> IndexedPostRecord: ...

    def get_post(self, source_chat_id: int, source_message_id: int) -> IndexedPostRecord | None: ...

    def reindex_batch(
        self,
        extractor_version: int,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[IndexedPostRecord]: ...

    def search_posts(
        self,
        source_chat_id: int,
        source_thread_id: int,
        keywords: tuple[str, ...],
        match_mode: str,
        limit: int,
    ) -> list[IndexedPostRecord]: ...


class SqliteRepositories:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, chat_id: int, thread_id: int) -> TopicRecord | None:
        row = self._connection.execute(
            """
            SELECT id, chat_id, thread_id, name, is_active
            FROM topics
            WHERE chat_id = ? AND thread_id = ?
            """,
            (chat_id, thread_id),
        ).fetchone()
        if row is None:
            return None
        return TopicRecord(
            id=row["id"],
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            name=row["name"],
            is_active=bool(row["is_active"]),
        )

    def register_topic(self, chat_id: int, thread_id: int, name: str) -> TopicRecord:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("topic name must not be blank")
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO topics (chat_id, thread_id, name)
                    VALUES (?, ?, ?)
                    ON CONFLICT (chat_id, thread_id) DO UPDATE SET
                        name = excluded.name,
                        is_active = 1,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (chat_id, thread_id, normalized_name),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("topic name is already registered in this chat") from error
        topic = self.get(chat_id, thread_id)
        if topic is None:
            raise RuntimeError("registered topic could not be read")
        return topic

    def list_topics(self, chat_id: int, *, active_only: bool = True) -> list[TopicRecord]:
        active_clause = "AND is_active = 1" if active_only else ""
        rows = self._connection.execute(
            f"""
            SELECT id, chat_id, thread_id, name, is_active
            FROM topics
            WHERE chat_id = ? {active_clause}
            ORDER BY name COLLATE NOCASE, thread_id
            """,
            (chat_id,),
        ).fetchall()
        return [
            TopicRecord(
                id=row["id"],
                chat_id=row["chat_id"],
                thread_id=row["thread_id"],
                name=row["name"],
                is_active=bool(row["is_active"]),
            )
            for row in rows
        ]

    def update_topic_state(
        self,
        chat_id: int,
        thread_id: int,
        *,
        is_active: bool | None = None,
        name: str | None = None,
    ) -> bool:
        assignments = ["updated_at = CURRENT_TIMESTAMP"]
        parameters: list[object] = []
        if is_active is not None:
            assignments.append("is_active = ?")
            parameters.append(int(is_active))
        if name is not None:
            normalized_name = name.strip()
            if not normalized_name:
                raise ValueError("topic name must not be blank")
            assignments.append("name = ?")
            parameters.append(normalized_name)
        parameters.extend((chat_id, thread_id))
        try:
            with self._connection:
                cursor = self._connection.execute(
                    f"""
                    UPDATE topics
                    SET {", ".join(assignments)}
                    WHERE chat_id = ? AND thread_id = ?
                    """,
                    parameters,
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("topic name is already registered in this chat") from error
        return cursor.rowcount == 1

    def add_mapping(
        self,
        chat_id: int,
        thread_id: int,
        kind: str,
        value: str,
        created_by_user_id: int,
    ) -> RouteMappingRecord:
        topic = self.get(chat_id, thread_id)
        if topic is None or not topic.is_active:
            raise ValueError("destination topic is not registered and active")
        display_value, normalized_value = normalize_mapping(kind, value)
        existing = self.find_mapping(chat_id, kind, value)
        if existing is not None and existing[1].id != topic.id:
            raise ValueError("mapping already belongs to another topic")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO route_mappings
                    (topic_id, kind, value, normalized_value, created_by_user_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (topic_id, kind, normalized_value) DO NOTHING
                """,
                (topic.id, kind, display_value, normalized_value, created_by_user_id),
            )
        row = self._connection.execute(
            """
            SELECT id, topic_id, kind, value, normalized_value, created_by_user_id
            FROM route_mappings
            WHERE topic_id = ? AND kind = ? AND normalized_value = ?
            """,
            (topic.id, kind, normalized_value),
        ).fetchone()
        return _mapping_record(row)

    def replace_mapping(
        self,
        chat_id: int,
        thread_id: int,
        kind: str,
        value: str,
        created_by_user_id: int,
    ) -> RouteMappingRecord:
        topic = self.get(chat_id, thread_id)
        if topic is None or not topic.is_active:
            raise ValueError("destination topic is not registered and active")
        display_value, normalized_value = normalize_mapping(kind, value)
        with self._connection:
            self._connection.execute(
                """
                DELETE FROM route_mappings
                WHERE kind = ?
                  AND normalized_value = ?
                  AND topic_id IN (SELECT id FROM topics WHERE chat_id = ?)
                """,
                (kind, normalized_value, chat_id),
            )
            cursor = self._connection.execute(
                """
                INSERT INTO route_mappings
                    (topic_id, kind, value, normalized_value, created_by_user_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (topic.id, kind, display_value, normalized_value, created_by_user_id),
            )
        row = self._connection.execute(
            """
            SELECT id, topic_id, kind, value, normalized_value, created_by_user_id
            FROM route_mappings
            WHERE id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        return _mapping_record(row)

    def remove_mapping(self, chat_id: int, thread_id: int, kind: str, value: str) -> bool:
        _, normalized_value = normalize_mapping(kind, value)
        with self._connection:
            cursor = self._connection.execute(
                """
                DELETE FROM route_mappings
                WHERE topic_id = (
                    SELECT id FROM topics WHERE chat_id = ? AND thread_id = ?
                )
                AND kind = ?
                AND normalized_value = ?
                """,
                (chat_id, thread_id, kind, normalized_value),
            )
        return cursor.rowcount == 1

    def list_mappings(
        self,
        chat_id: int,
        *,
        thread_id: int | None = None,
        kind: str | None = None,
    ) -> list[RouteMappingRecord]:
        conditions = ["topics.chat_id = ?"]
        parameters: list[object] = [chat_id]
        if thread_id is not None:
            conditions.append("topics.thread_id = ?")
            parameters.append(thread_id)
        if kind is not None:
            conditions.append("route_mappings.kind = ?")
            parameters.append(kind)
        rows = self._connection.execute(
            f"""
            SELECT route_mappings.id, route_mappings.topic_id, route_mappings.kind,
                   route_mappings.value, route_mappings.normalized_value,
                   route_mappings.created_by_user_id
            FROM route_mappings
            JOIN topics ON topics.id = route_mappings.topic_id
            WHERE {" AND ".join(conditions)}
            ORDER BY topics.name COLLATE NOCASE, route_mappings.kind,
                     route_mappings.normalized_value
            """,
            parameters,
        ).fetchall()
        return [_mapping_record(row) for row in rows]

    def find_mapping(
        self,
        chat_id: int,
        kind: str,
        value: str,
    ) -> tuple[RouteMappingRecord, TopicRecord] | None:
        _, normalized_value = normalize_mapping(kind, value)
        row = self._connection.execute(
            """
            SELECT route_mappings.id, route_mappings.topic_id, route_mappings.kind,
                   route_mappings.value, route_mappings.normalized_value,
                   route_mappings.created_by_user_id,
                   topics.id AS matched_topic_id, topics.chat_id, topics.thread_id,
                   topics.name, topics.is_active
            FROM route_mappings
            JOIN topics ON topics.id = route_mappings.topic_id
            WHERE topics.chat_id = ?
              AND route_mappings.kind = ?
              AND route_mappings.normalized_value = ?
            """,
            (chat_id, kind, normalized_value),
        ).fetchone()
        if row is None:
            return None
        return (
            _mapping_record(row),
            TopicRecord(
                id=row["matched_topic_id"],
                chat_id=row["chat_id"],
                thread_id=row["thread_id"],
                name=row["name"],
                is_active=bool(row["is_active"]),
            ),
        )

    def is_route_manager(self, chat_id: int, user_id: int) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM route_managers WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            is not None
        )

    def is_manager(self, user_id: int) -> bool:
        """True if the user is a manager in any chat (chat-independent)."""

        return (
            self._connection.execute(
                "SELECT 1 FROM route_managers WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            is not None
        )

    def grant_route_manager(
        self,
        chat_id: int,
        user_id: int,
        granted_by_user_id: int,
    ) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO route_managers (chat_id, user_id, granted_by_user_id)
                VALUES (?, ?, ?)
                ON CONFLICT (chat_id, user_id) DO NOTHING
                """,
                (chat_id, user_id, granted_by_user_id),
            )
        return cursor.rowcount == 1

    def revoke_route_manager(self, chat_id: int, user_id: int) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM route_managers WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
        return cursor.rowcount == 1

    def revoke_manager(self, user_id: int) -> bool:
        """Remove a manager from every chat (chat-independent). True if any removed."""

        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM route_managers WHERE user_id = ?",
                (user_id,),
            )
        return cursor.rowcount > 0

    def upsert_post(
        self,
        post: IndexedPostInput,
        tokens: frozenset[SearchToken],
    ) -> IndexedPostRecord:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO posts (
                    source_chat_id, source_thread_id, source_message_id,
                    media_group_id, logical_post_key, media_type, caption_preview,
                    extractor_version, sender_user_id, sender_is_bot, source_kind,
                    message_created_at, is_available
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (source_chat_id, source_message_id) DO UPDATE SET
                    source_thread_id = excluded.source_thread_id,
                    media_group_id = excluded.media_group_id,
                    logical_post_key = excluded.logical_post_key,
                    media_type = excluded.media_type,
                    caption_preview = excluded.caption_preview,
                    extractor_version = excluded.extractor_version,
                    sender_user_id = excluded.sender_user_id,
                    sender_is_bot = excluded.sender_is_bot,
                    source_kind = excluded.source_kind,
                    message_created_at = COALESCE(
                        excluded.message_created_at,
                        posts.message_created_at
                    ),
                    is_available = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    post.source_chat_id,
                    post.source_thread_id,
                    post.source_message_id,
                    post.media_group_id,
                    post.logical_post_key,
                    post.media_type,
                    post.caption_preview,
                    post.extractor_version,
                    post.sender_user_id,
                    int(post.sender_is_bot),
                    post.source_kind,
                    post.message_created_at,
                ),
            )
            row = self._connection.execute(
                """
                SELECT id FROM posts
                WHERE source_chat_id = ? AND source_message_id = ?
                """,
                (post.source_chat_id, post.source_message_id),
            ).fetchone()
            post_id = row["id"]
            self._connection.execute("DELETE FROM post_tokens WHERE post_id = ?", (post_id,))
            self._connection.executemany(
                """
                INSERT INTO post_tokens (post_id, kind, value, normalized_value)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (post_id, token.kind, token.value, token.normalized_value)
                    for token in sorted(
                        tokens,
                        key=lambda item: (item.kind, item.normalized_value),
                    )
                ],
            )
        indexed = self.get_post(post.source_chat_id, post.source_message_id)
        if indexed is None:
            raise RuntimeError("indexed post could not be read")
        return indexed

    def get_post(
        self,
        source_chat_id: int,
        source_message_id: int,
    ) -> IndexedPostRecord | None:
        row = self._connection.execute(
            """
            SELECT id, source_chat_id, source_thread_id, source_message_id,
                   media_group_id, logical_post_key, media_type, caption_preview,
                   extractor_version, sender_user_id, sender_is_bot, source_kind,
                   message_created_at, is_available
            FROM posts
            WHERE source_chat_id = ? AND source_message_id = ?
            """,
            (source_chat_id, source_message_id),
        ).fetchone()
        return _indexed_post_record(row) if row is not None else None

    def get_post_tokens(self, post_id: int) -> frozenset[SearchToken]:
        rows = self._connection.execute(
            """
            SELECT kind, value, normalized_value
            FROM post_tokens
            WHERE post_id = ?
            ORDER BY kind, normalized_value
            """,
            (post_id,),
        ).fetchall()
        return frozenset(
            SearchToken(row["kind"], row["value"], row["normalized_value"]) for row in rows
        )

    def reindex_batch(
        self,
        extractor_version: int,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[IndexedPostRecord]:
        if limit < 1 or limit > 1000:
            raise ValueError("reindex limit must be between 1 and 1000")
        rows = self._connection.execute(
            """
            SELECT id, source_chat_id, source_thread_id, source_message_id,
                   media_group_id, logical_post_key, media_type, caption_preview,
                   extractor_version, sender_user_id, sender_is_bot, source_kind,
                   message_created_at, is_available
            FROM posts
            WHERE id > ? AND extractor_version < ?
            ORDER BY id
            LIMIT ?
            """,
            (after_id, extractor_version, limit),
        ).fetchall()
        return [_indexed_post_record(row) for row in rows]

    def search_posts(
        self,
        source_chat_id: int,
        source_thread_id: int,
        keywords: tuple[str, ...],
        match_mode: str,
        limit: int,
    ) -> list[IndexedPostRecord]:
        normalized = tuple(
            dict.fromkeys(" ".join(value.casefold().split()) for value in keywords if value.strip())
        )
        if not normalized:
            return []
        if match_mode not in {"all", "any"}:
            raise ValueError("match mode must be all or any")
        if limit < 1:
            raise ValueError("search limit must be positive")
        placeholders = ",".join("?" for _ in normalized)
        comparison = f"= {len(normalized)}" if match_mode == "all" else ">= 1"
        rows = self._connection.execute(
            f"""
            WITH matching_groups AS (
                SELECT posts.logical_post_key,
                       MAX(COALESCE(posts.message_created_at, posts.created_at)) AS group_time,
                       COUNT(DISTINCT post_tokens.normalized_value) AS matched_count
                FROM posts
                JOIN post_tokens ON post_tokens.post_id = posts.id
                WHERE posts.source_chat_id = ?
                  AND posts.source_thread_id = ?
                  AND posts.is_available = 1
                  AND post_tokens.normalized_value IN ({placeholders})
                GROUP BY posts.logical_post_key
                HAVING matched_count {comparison}
            ),
            selected_groups AS (
                SELECT logical_post_key, group_time
                FROM matching_groups
                ORDER BY group_time DESC, logical_post_key DESC
                LIMIT ?
            )
            SELECT posts.id, posts.source_chat_id, posts.source_thread_id,
                   posts.source_message_id, posts.media_group_id,
                   posts.logical_post_key, posts.media_type, posts.caption_preview,
                   posts.extractor_version, posts.sender_user_id, posts.sender_is_bot,
                   posts.source_kind, posts.message_created_at, posts.is_available
            FROM posts
            JOIN selected_groups
              ON selected_groups.logical_post_key = posts.logical_post_key
            WHERE posts.source_chat_id = ?
              AND posts.source_thread_id = ?
              AND posts.is_available = 1
            ORDER BY selected_groups.group_time DESC,
                     posts.logical_post_key DESC,
                     posts.source_message_id ASC
            """,
            (
                source_chat_id,
                source_thread_id,
                *normalized,
                limit,
                source_chat_id,
                source_thread_id,
            ),
        ).fetchall()
        return [_indexed_post_record(row) for row in rows]

    def ensure_retrieval_item(
        self,
        job_id: int,
        post_id: int,
        destination_chat_id: int,
        destination_thread_id: int,
    ) -> RetrievalItemRecord:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO retrieval_items (
                    job_id, post_id, destination_chat_id, destination_thread_id
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT (job_id, post_id) DO NOTHING
                """,
                (job_id, post_id, destination_chat_id, destination_thread_id),
            )
        row = self._connection.execute(
            """
            SELECT id, job_id, post_id, destination_chat_id, destination_thread_id,
                   destination_message_id, status, error
            FROM retrieval_items
            WHERE job_id = ? AND post_id = ?
            """,
            (job_id, post_id),
        ).fetchone()
        return _retrieval_item_record(row)

    def update_retrieval_item(
        self,
        item_id: int,
        status: str,
        *,
        destination_message_id: int | None = None,
        error: str | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE retrieval_items
                SET status = ?,
                    destination_message_id = COALESCE(?, destination_message_id),
                    error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, destination_message_id, error, item_id),
            )

    def mark_post_unavailable(self, post_id: int) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE posts
                SET is_available = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND is_available = 1
                """,
                (post_id,),
            )
        return cursor.rowcount == 1

    def get_job(self, job_id: int) -> JobRecord | None:
        row = self._connection.execute(
            """
            SELECT id, kind, status, idempotency_key, payload_json, attempts
            FROM jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return JobRecord(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            payload=json.loads(row["payload_json"]),
            attempts=row["attempts"],
        )

    def cancel_job(self, job_id: int, kind: str) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND kind = ?
                  AND status IN ('pending', 'running', 'failed')
                """,
                (job_id, kind),
            )
        return cursor.rowcount == 1

    def recover_interrupted_jobs(self) -> int:
        return sum(self.recover_interrupted_jobs_by_kind().values())

    def recover_interrupted_jobs_by_kind(self) -> dict[str, int]:
        rows = self._connection.execute(
            """
            SELECT kind, COUNT(*) AS count
            FROM jobs
            WHERE status = 'running'
            GROUP BY kind
            """
        ).fetchall()
        recovered = {row["kind"]: row["count"] for row in rows}
        with self._connection:
            self._connection.execute(
                """
                UPDATE jobs
                SET status = 'pending',
                    last_error = 'recovered after interrupted shutdown',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                """
            )
        return recovered

    def add_dead_letter(
        self,
        job_id: int | None,
        operation: str,
        payload: dict[str, Any],
        error_category: str,
        error_message: str,
    ) -> int:
        serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO dead_letters (
                    job_id, operation, payload_json, error_category, error_message
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, operation, serialized, error_category, error_message),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("dead-letter insert did not return an ID")
        return int(cursor.lastrowid)

    def list_dead_letters(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT id, job_id, operation, payload_json, error_category,
                   error_message, created_at
            FROM dead_letters
            WHERE resolved_at IS NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "job_id": row["job_id"],
                "operation": row["operation"],
                "payload": json.loads(row["payload_json"]),
                "error_category": row["error_category"],
                "error_message": row["error_message"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def retry_dead_letter(self, dead_letter_id: int) -> int | None:
        row = self._connection.execute(
            "SELECT job_id FROM dead_letters WHERE id = ? AND resolved_at IS NULL",
            (dead_letter_id,),
        ).fetchone()
        if row is None or row["job_id"] is None:
            return None
        with self._connection:
            self._connection.execute(
                """
                UPDATE jobs SET status = 'pending', last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (row["job_id"],),
            )
            self._connection.execute(
                "UPDATE dead_letters SET resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                (dead_letter_id,),
            )
        return int(row["job_id"])

    def claim_integration_nonce(
        self,
        client_id: str,
        nonce: str,
        timestamp: int,
        *,
        oldest_allowed: int,
    ) -> bool:
        with self._connection:
            self._connection.execute(
                "DELETE FROM integration_nonces WHERE timestamp < ?",
                (oldest_allowed,),
            )
            cursor = self._connection.execute(
                """
                INSERT INTO integration_nonces (client_id, nonce, timestamp)
                VALUES (?, ?, ?)
                ON CONFLICT (client_id, nonce) DO NOTHING
                """,
                (client_id, nonce, timestamp),
            )
        return cursor.rowcount == 1

    def consume_integration_quota(
        self,
        client_id: str,
        window_start: int,
        limit: int,
    ) -> tuple[bool, int]:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO integration_usage (client_id, window_start, request_count)
                VALUES (?, ?, 0)
                ON CONFLICT (client_id, window_start) DO NOTHING
                """,
                (client_id, window_start),
            )
            cursor = self._connection.execute(
                """
                UPDATE integration_usage
                SET request_count = request_count + 1
                WHERE client_id = ? AND window_start = ? AND request_count < ?
                """,
                (client_id, window_start, limit),
            )
            count = self._connection.execute(
                """
                SELECT request_count FROM integration_usage
                WHERE client_id = ? AND window_start = ?
                """,
                (client_id, window_start),
            ).fetchone()["request_count"]
        return cursor.rowcount == 1, count

    def add_audit_event(
        self,
        *,
        actor_type: str,
        actor_id: str,
        action: str,
        outcome: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> int:
        serialized = json.dumps(
            details or {},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO audit_events (
                    actor_type, actor_id, action, resource_type, resource_id,
                    outcome, details_json, correlation_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_type,
                    actor_id,
                    action,
                    resource_type,
                    resource_id,
                    outcome,
                    serialized,
                    correlation_id,
                ),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("audit insert did not return an ID")
        return int(cursor.lastrowid)

    def list_audit_events(self, limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("audit limit must be between 1 and 500")
        rows = self._connection.execute(
            """
            SELECT id, actor_type, actor_id, action, resource_type, resource_id,
                   outcome, details_json, correlation_id, created_at
            FROM audit_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "actor_type": row["actor_type"],
                "actor_id": row["actor_id"],
                "action": row["action"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "outcome": row["outcome"],
                "details": json.loads(row["details_json"]),
                "correlation_id": row["correlation_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def claim(self, update_id: int, operation: str) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO processed_updates (update_id, operation)
                VALUES (?, ?)
                ON CONFLICT (update_id) DO NOTHING
                """,
                (update_id, operation),
            )
        return cursor.rowcount == 1

    def enqueue(
        self,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> JobRecord:
        serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO jobs (kind, idempotency_key, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (kind, idempotency_key, serialized),
            )
        row = self._connection.execute(
            """
            SELECT id, kind, status, idempotency_key, payload_json, attempts
            FROM jobs
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        return JobRecord(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            payload=json.loads(row["payload_json"]),
            attempts=row["attempts"],
        )

    def update_job(self, job_id: int, status: str, *, error: str | None = None) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE jobs
                SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error, job_id),
            )
            if status == "completed":
                self._connection.execute(
                    """
                    UPDATE dead_letters
                    SET resolved_at = COALESCE(resolved_at, CURRENT_TIMESTAMP)
                    WHERE job_id = ?
                    """,
                    (job_id,),
                )

    def claim_job(self, job_id: int) -> bool:
        """Atomically acquire a runnable job for exactly one worker."""

        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE jobs
                SET status = 'running', attempts = attempts + 1,
                    last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND status IN ('pending', 'failed')
                  AND datetime(available_at) <= datetime('now')
                """,
                (job_id,),
            )
        return cursor.rowcount == 1

    def list_pending_jobs(self, limit: int = 100) -> list[JobRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("pending job limit must be between 1 and 1000")
        rows = self._connection.execute(
            """
            SELECT id, kind, status, idempotency_key, payload_json, attempts
            FROM jobs
            WHERE status = 'pending'
              AND datetime(available_at) <= datetime('now')
            ORDER BY available_at, id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            JobRecord(
                id=row["id"],
                kind=row["kind"],
                status=row["status"],
                idempotency_key=row["idempotency_key"],
                payload=json.loads(row["payload_json"]),
                attempts=row["attempts"],
            )
            for row in rows
        ]

    def ensure_delivery(
        self,
        job_id: int,
        *,
        source_chat_id: int,
        source_message_id: int,
        destination_chat_id: int,
        destination_thread_id: int,
        reason: str,
    ) -> DeliveryRecord:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO deliveries (
                    job_id, source_chat_id, source_message_id,
                    destination_chat_id, destination_thread_id, reason
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    source_chat_id, source_message_id,
                    destination_chat_id, destination_thread_id
                ) DO NOTHING
                """,
                (
                    job_id,
                    source_chat_id,
                    source_message_id,
                    destination_chat_id,
                    destination_thread_id,
                    reason,
                ),
            )
        delivery = self.get_delivery(
            source_chat_id,
            source_message_id,
            destination_chat_id,
            destination_thread_id,
        )
        if delivery is None:
            raise RuntimeError("delivery could not be read")
        return delivery

    def get_delivery(
        self,
        source_chat_id: int,
        source_message_id: int,
        destination_chat_id: int,
        destination_thread_id: int,
    ) -> DeliveryRecord | None:
        row = self._connection.execute(
            """
            SELECT id, job_id, source_chat_id, source_message_id,
                   destination_chat_id, destination_thread_id,
                   destination_message_id, status, reason
            FROM deliveries
            WHERE source_chat_id = ? AND source_message_id = ?
              AND destination_chat_id = ? AND destination_thread_id = ?
            """,
            (
                source_chat_id,
                source_message_id,
                destination_chat_id,
                destination_thread_id,
            ),
        ).fetchone()
        return _delivery_record(row) if row is not None else None

    def update_delivery(
        self,
        delivery_id: int,
        status: str,
        *,
        destination_message_id: int | None = None,
        reason: str | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE deliveries
                SET status = ?,
                    destination_message_id = COALESCE(?, destination_message_id),
                    reason = COALESCE(?, reason),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, destination_message_id, reason, delivery_id),
            )

    def increment_metric(self, name: str, amount: int = 1) -> None:
        if not name or amount < 0:
            raise ValueError("metric name is required and amount must be non-negative")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO metric_counters (name, value)
                VALUES (?, ?)
                ON CONFLICT (name) DO UPDATE SET
                    value = value + excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (name, amount),
            )

    def metrics_snapshot(self) -> dict[str, int]:
        return {
            row["name"]: row["value"]
            for row in self._connection.execute(
                "SELECT name, value FROM metric_counters ORDER BY name"
            )
        }

    def count_recent_source_posts(
        self,
        source_chat_id: int,
        source_thread_id: int,
        since: str,
    ) -> int:
        return self._connection.execute(
            """
            SELECT COUNT(*)
            FROM posts
            WHERE source_chat_id = ?
              AND source_thread_id = ?
              AND is_available = 1
              AND datetime(COALESCE(message_created_at, created_at)) >= datetime(?)
            """,
            (source_chat_id, source_thread_id, since),
        ).fetchone()[0]

    def operational_status(self) -> dict[str, Any]:
        job_counts = {
            row["status"]: row["count"]
            for row in self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            )
        }
        delivery_counts = {
            row["status"]: row["count"]
            for row in self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM deliveries GROUP BY status"
            )
        }
        return {
            "database": self._connection.execute("PRAGMA quick_check").fetchone()[0],
            "foreign_keys": bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0]),
            "posts": self._connection.execute(
                "SELECT COUNT(*) FROM posts WHERE is_available = 1"
            ).fetchone()[0],
            "unavailable_posts": self._connection.execute(
                "SELECT COUNT(*) FROM posts WHERE is_available = 0"
            ).fetchone()[0],
            "unresolved_dead_letters": self._connection.execute(
                "SELECT COUNT(*) FROM dead_letters WHERE resolved_at IS NULL"
            ).fetchone()[0],
            "jobs": job_counts,
            "deliveries": delivery_counts,
            "metrics": self.metrics_snapshot(),
        }

    def run_maintenance(
        self,
        *,
        transient_retention_days: int,
        audit_retention_days: int,
    ) -> dict[str, int]:
        if transient_retention_days < 1 or audit_retention_days < 1:
            raise ValueError("retention periods must be positive")
        transient_cutoff = f"-{transient_retention_days} days"
        audit_cutoff = f"-{audit_retention_days} days"
        deleted: dict[str, int] = {}
        with self._connection:
            operations = (
                (
                    "completed_job_dead_letters",
                    """
                    UPDATE dead_letters
                    SET resolved_at = CURRENT_TIMESTAMP
                    WHERE resolved_at IS NULL
                      AND job_id IN (SELECT id FROM jobs WHERE status = 'completed')
                    """,
                    None,
                ),
                (
                    "processed_updates",
                    "DELETE FROM processed_updates WHERE processed_at < datetime('now', ?)",
                    transient_cutoff,
                ),
                (
                    "resolved_dead_letters",
                    """
                    DELETE FROM dead_letters
                    WHERE resolved_at IS NOT NULL
                      AND resolved_at < datetime('now', ?)
                    """,
                    transient_cutoff,
                ),
                (
                    "integration_nonces",
                    """
                    DELETE FROM integration_nonces
                    WHERE created_at < datetime('now', ?)
                    """,
                    transient_cutoff,
                ),
                (
                    "integration_usage",
                    """
                    DELETE FROM integration_usage
                    WHERE window_start < CAST(strftime('%s', 'now', ?) AS INTEGER)
                    """,
                    transient_cutoff,
                ),
                (
                    "audit_events",
                    "DELETE FROM audit_events WHERE created_at < datetime('now', ?)",
                    audit_cutoff,
                ),
            )
            for name, sql, cutoff in operations:
                cursor = self._connection.execute(sql, () if cutoff is None else (cutoff,))
                deleted[name] = cursor.rowcount
            self._connection.execute("PRAGMA optimize")
        return deleted


def normalize_mapping(kind: str, value: str) -> tuple[str, str]:
    if kind not in {"hashtag", "keyword", "phrase"}:
        raise ValueError("unsupported mapping kind")
    display_value = " ".join(value.strip().split())
    if kind == "hashtag":
        display_value = display_value.removeprefix("#")
    if not display_value:
        raise ValueError("mapping value must not be blank")
    if kind in {"hashtag", "keyword"} and any(character.isspace() for character in display_value):
        raise ValueError(f"{kind} must be a single token")
    if kind == "phrase" and len(display_value.split()) < 2:
        raise ValueError("phrase must contain at least two words")
    return display_value, display_value.casefold()


def _mapping_record(row: sqlite3.Row) -> RouteMappingRecord:
    return RouteMappingRecord(
        id=row["id"],
        topic_id=row["topic_id"],
        kind=row["kind"],
        value=row["value"],
        normalized_value=row["normalized_value"],
        created_by_user_id=row["created_by_user_id"],
    )


def _indexed_post_record(row: sqlite3.Row) -> IndexedPostRecord:
    return IndexedPostRecord(
        id=row["id"],
        source_chat_id=row["source_chat_id"],
        source_thread_id=row["source_thread_id"],
        source_message_id=row["source_message_id"],
        media_group_id=row["media_group_id"],
        logical_post_key=row["logical_post_key"],
        media_type=row["media_type"],
        caption_preview=row["caption_preview"],
        extractor_version=row["extractor_version"],
        sender_user_id=row["sender_user_id"],
        sender_is_bot=bool(row["sender_is_bot"]),
        source_kind=row["source_kind"],
        message_created_at=row["message_created_at"],
        is_available=bool(row["is_available"]),
    )


def _delivery_record(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(
        id=row["id"],
        job_id=row["job_id"],
        source_chat_id=row["source_chat_id"],
        source_message_id=row["source_message_id"],
        destination_chat_id=row["destination_chat_id"],
        destination_thread_id=row["destination_thread_id"],
        destination_message_id=row["destination_message_id"],
        status=row["status"],
        reason=row["reason"],
    )


def _retrieval_item_record(row: sqlite3.Row) -> RetrievalItemRecord:
    return RetrievalItemRecord(
        id=row["id"],
        job_id=row["job_id"],
        post_id=row["post_id"],
        destination_chat_id=row["destination_chat_id"],
        destination_thread_id=row["destination_thread_id"],
        destination_message_id=row["destination_message_id"],
        status=row["status"],
        error=row["error"],
    )
