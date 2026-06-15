from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from miki_sorter_bot.migrations import migrate
from miki_sorter_bot.repositories import SqliteRepositories


class Storage:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._connection: sqlite3.Connection | None = None

    def open(self) -> SqliteRepositories:
        if self._connection is None:
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._database_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            migrate(connection)
            self._connection = connection
        return SqliteRepositories(self._connection)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def backup(self, directory: Path) -> Path:
        if self._connection is None:
            raise RuntimeError("storage must be open before creating a backup")
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = directory / f"miki-{timestamp}.sqlite3"
        with sqlite3.connect(destination) as target:
            self._connection.backup(target)
        self.verify_database(destination)
        return destination

    @staticmethod
    def verify_database(path: Path) -> None:
        if not path.is_file():
            raise ValueError(f"database does not exist: {path}")
        with sqlite3.connect(path) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"database integrity check failed: {result}")
            connection.execute("SELECT version FROM schema_migrations LIMIT 1").fetchone()

    @staticmethod
    def restore_backup(source: Path, destination: Path) -> None:
        Storage.verify_database(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".restore")
        temporary.unlink(missing_ok=True)
        try:
            with sqlite3.connect(source) as source_connection:
                with sqlite3.connect(temporary) as target_connection:
                    source_connection.backup(target_connection)
            Storage.verify_database(temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def __enter__(self) -> SqliteRepositories:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()
