from __future__ import annotations

import sqlite3

from miki_sorter_bot.migrations import MIGRATIONS
from miki_sorter_bot.storage import Storage


def test_storage_creates_parent_directory_and_migrates(tmp_path) -> None:
    database_path = tmp_path / "state" / "miki.sqlite3"

    with Storage(database_path) as repositories:
        assert repositories.claim(1, "test")

    assert database_path.exists()
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(
            MIGRATIONS
        )
    finally:
        connection.close()
