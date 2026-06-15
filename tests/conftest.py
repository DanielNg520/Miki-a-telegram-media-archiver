from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from miki_sorter_bot.config import Settings
from miki_sorter_bot.migrations import migrate


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop ``Settings`` from reading the developer's real ``.env`` during tests.

    Without this, any field a test omits silently falls back to the local
    ``.env`` file, which both leaks personal configuration into assertions and
    makes results depend on the machine the suite runs on.
    """

    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture
def database_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()
