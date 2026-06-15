from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS = (
    Migration(
        1,
        "initial_foundation",
        """
        CREATE TABLE topics (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL CHECK (thread_id > 0),
            name TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (chat_id, thread_id)
        );

        CREATE TABLE route_mappings (
            id INTEGER PRIMARY KEY,
            topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('hashtag', 'keyword', 'phrase')),
            value TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            created_by_user_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (kind, normalized_value)
        );

        CREATE TABLE posts (
            id INTEGER PRIMARY KEY,
            source_chat_id INTEGER NOT NULL,
            source_thread_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            media_group_id TEXT,
            media_type TEXT NOT NULL,
            caption_preview TEXT,
            extractor_version INTEGER NOT NULL DEFAULT 1,
            is_available INTEGER NOT NULL DEFAULT 1 CHECK (is_available IN (0, 1)),
            message_created_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (source_chat_id, source_message_id)
        );

        CREATE TABLE post_tokens (
            post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('hashtag', 'keyword', 'name', 'code', 'phrase')),
            value TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            PRIMARY KEY (post_id, kind, normalized_value)
        );

        CREATE INDEX post_tokens_lookup_idx
            ON post_tokens (normalized_value, kind, post_id);
        CREATE INDEX posts_topic_time_idx
            ON posts (source_chat_id, source_thread_id, message_created_at DESC);
        CREATE INDEX posts_media_group_idx
            ON posts (source_chat_id, media_group_id)
            WHERE media_group_id IS NOT NULL;

        CREATE TABLE processed_updates (
            update_id INTEGER PRIMARY KEY,
            operation TEXT NOT NULL,
            processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('sort', 'retrieve', 'reindex')),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
            idempotency_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX jobs_claim_idx ON jobs (status, available_at, id);

        CREATE TABLE deliveries (
            id INTEGER PRIMARY KEY,
            job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            source_chat_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            destination_chat_id INTEGER NOT NULL,
            destination_thread_id INTEGER NOT NULL,
            destination_message_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (
                source_chat_id,
                source_message_id,
                destination_chat_id,
                destination_thread_id
            )
        );
        """,
    ),
    Migration(
        2,
        "topic_management",
        """
        CREATE UNIQUE INDEX topics_chat_name_idx
            ON topics (chat_id, name COLLATE NOCASE);

        ALTER TABLE route_mappings RENAME TO route_mappings_v1;

        CREATE TABLE route_mappings (
            id INTEGER PRIMARY KEY,
            topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('hashtag', 'keyword', 'phrase')),
            value TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            created_by_user_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (topic_id, kind, normalized_value)
        );

        INSERT INTO route_mappings (
            id, topic_id, kind, value, normalized_value, created_by_user_id, created_at
        )
        SELECT id, topic_id, kind, value, normalized_value, created_by_user_id, created_at
        FROM route_mappings_v1;

        DROP TABLE route_mappings_v1;

        CREATE TABLE route_managers (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            granted_by_user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, user_id)
        );
        """,
    ),
    Migration(
        3,
        "post_indexing",
        """
        ALTER TABLE posts ADD COLUMN logical_post_key TEXT;
        ALTER TABLE posts ADD COLUMN sender_user_id INTEGER;
        ALTER TABLE posts ADD COLUMN sender_is_bot INTEGER NOT NULL DEFAULT 0
            CHECK (sender_is_bot IN (0, 1));
        ALTER TABLE posts ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'telegram'
            CHECK (source_kind IN ('telegram', 'miki_copy', 'external_bot'));

        UPDATE posts
        SET logical_post_key = source_chat_id || ':' ||
            COALESCE(media_group_id, 'message:' || source_message_id);

        CREATE INDEX posts_logical_post_idx
            ON posts (source_chat_id, source_thread_id, logical_post_key);
        CREATE INDEX posts_extractor_version_idx
            ON posts (extractor_version, id);
        """,
    ),
    Migration(
        4,
        "retrieval",
        """
        CREATE TABLE retrieval_items (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            destination_chat_id INTEGER NOT NULL,
            destination_thread_id INTEGER NOT NULL,
            destination_message_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (job_id, post_id)
        );

        CREATE INDEX retrieval_items_job_status_idx
            ON retrieval_items (job_id, status, post_id);
        """,
    ),
    Migration(
        5,
        "reliability",
        """
        CREATE TABLE dead_letters (
            id INTEGER PRIMARY KEY,
            job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            operation TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            error_category TEXT NOT NULL,
            error_message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        );

        CREATE INDEX dead_letters_unresolved_idx
            ON dead_letters (resolved_at, created_at, id);
        """,
    ),
    Migration(
        6,
        "integrations_and_audit",
        """
        CREATE TABLE integration_nonces (
            client_id TEXT NOT NULL,
            nonce TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (client_id, nonce)
        );

        CREATE INDEX integration_nonces_time_idx
            ON integration_nonces (timestamp);

        CREATE TABLE integration_usage (
            client_id TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (client_id, window_start)
        );

        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY,
            actor_type TEXT NOT NULL CHECK (
                actor_type IN ('telegram_user', 'telegram_bot', 'integration', 'system')
            ),
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            resource_type TEXT,
            resource_id TEXT,
            outcome TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'failed')),
            details_json TEXT NOT NULL DEFAULT '{}',
            correlation_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX audit_events_created_idx
            ON audit_events (created_at DESC, id DESC);
        CREATE INDEX audit_events_actor_idx
            ON audit_events (actor_type, actor_id, id DESC);
        """,
    ),
    Migration(
        7,
        "operational_metrics",
        """
        CREATE TABLE metric_counters (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
    ),
)


def migrate(connection: sqlite3.Connection) -> list[Migration]:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {
        row[0]
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    completed: list[Migration] = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        escaped_name = migration.name.replace("'", "''")
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                f"{migration.sql}\n"
                "INSERT INTO schema_migrations (version, name) "
                f"VALUES ({migration.version}, '{escaped_name}');\n"
                "COMMIT;"
            )
        except Exception:
            connection.rollback()
            raise
        completed.append(migration)
    return completed
