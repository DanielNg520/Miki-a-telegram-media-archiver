from __future__ import annotations

import sqlite3

import pytest

from miki_sorter_bot.migrations import MIGRATIONS, migrate


def test_migrations_create_the_foundation_schema(database_connection) -> None:
    tables = {
        row["name"]
        for row in database_connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }

    assert {
        "schema_migrations",
        "topics",
        "route_mappings",
        "posts",
        "post_tokens",
        "processed_updates",
        "jobs",
        "deliveries",
        "route_managers",
        "retrieval_items",
        "dead_letters",
        "integration_nonces",
        "integration_usage",
        "audit_events",
    } <= tables


def test_migrations_are_idempotent(database_connection) -> None:
    assert migrate(database_connection) == []
    versions = database_connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [row["version"] for row in versions] == [migration.version for migration in MIGRATIONS]


def test_route_mapping_cannot_point_to_an_unknown_topic(database_connection) -> None:
    try:
        database_connection.execute(
            """
            INSERT INTO route_mappings
                (topic_id, kind, value, normalized_value)
            VALUES (999, 'keyword', 'ABC', 'abc')
            """
        )
    except Exception as error:
        assert "FOREIGN KEY" in str(error)
    else:
        raise AssertionError("foreign-key violation was accepted")


def test_phase_two_database_upgrades_without_losing_mappings() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.executescript(MIGRATIONS[0].sql)
    connection.execute(
        "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
        (MIGRATIONS[0].version, MIGRATIONS[0].name),
    )
    topic_id = connection.execute(
        "INSERT INTO topics (chat_id, thread_id, name) VALUES (-100, 7, 'Japan')"
    ).lastrowid
    connection.execute(
        """
        INSERT INTO route_mappings
            (topic_id, kind, value, normalized_value, created_by_user_id)
        VALUES (?, 'keyword', 'ABC', 'abc', 10)
        """,
        (topic_id,),
    )
    connection.commit()

    migrate(connection)

    mapping = connection.execute("SELECT value, normalized_value FROM route_mappings").fetchone()
    assert (mapping["value"], mapping["normalized_value"]) == ("ABC", "abc")
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 7
    connection.close()


@pytest.mark.parametrize("applied_count", range(len(MIGRATIONS) + 1))
def test_upgrade_from_every_schema_boundary(applied_count) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for migration in MIGRATIONS[:applied_count]:
        connection.executescript(migration.sql)
        connection.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (migration.version, migration.name),
        )
    connection.commit()

    migrate(connection)

    versions = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [row["version"] for row in versions] == [migration.version for migration in MIGRATIONS]
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
