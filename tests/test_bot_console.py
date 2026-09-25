from __future__ import annotations

import inspect

from miki_sorter_bot.bot_console import (
    COMMAND_METHODS,
    NEEDS_BOT,
    list_commands,
    run_command,
)
from miki_sorter_bot.config import Settings
from miki_sorter_bot.management import ManagementCommands
from miki_sorter_bot.storage import Storage


def test_every_mapped_command_is_a_real_handler() -> None:
    # Guards against a rename in ManagementCommands silently breaking the CLI.
    for command, method in COMMAND_METHODS.items():
        handler = getattr(ManagementCommands, method, None)
        assert handler is not None, f"{command} -> missing method {method}"
        assert inspect.iscoroutinefunction(handler), f"{method} must be async"


def test_needs_bot_is_subset_of_commands() -> None:
    assert NEEDS_BOT <= set(COMMAND_METHODS)


def test_list_commands_is_sorted_and_nonempty() -> None:
    commands = list_commands()
    assert commands == sorted(commands)
    assert "status" in commands and "keyword_add" in commands


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[arg-type]
        BOT_TOKEN="token",
        SOURCE_CHAT_ID=-100,
        SOURCE_THREAD_ID=5,
        ARCHIVE_CHAT_ID=-200,
        ADMIN_USER_IDS="1372630907",
        DATABASE_PATH=str(tmp_path / "miki.sqlite3"),
    )


def test_run_command_status_as_admin(tmp_path) -> None:
    settings = _settings(tmp_path)
    storage = Storage(settings.database_path)
    repositories = storage.open()
    try:
        result = run_command(
            settings, repositories, storage, name="status", args=[]
        )
    finally:
        storage.close()
    assert result.ok
    assert "Operational status" in result.output


def test_run_command_rejects_non_admin(tmp_path) -> None:
    settings = _settings(tmp_path)
    storage = Storage(settings.database_path)
    repositories = storage.open()
    try:
        result = run_command(
            settings, repositories, storage, name="status", args=[], user_id=999
        )
    finally:
        storage.close()
    # The handler runs but replies with the authorization refusal (same as the bot).
    assert "not authorized" in result.output.lower()


def test_run_command_unknown_is_reported(tmp_path) -> None:
    settings = _settings(tmp_path)
    storage = Storage(settings.database_path)
    repositories = storage.open()
    try:
        result = run_command(
            settings, repositories, storage, name="nope", args=[]
        )
    finally:
        storage.close()
    assert not result.ok
    assert "Unknown command" in result.output
def test_commands_that_deliver_messages_open_a_bot() -> None:
    """Regression: dead_letter_retry re-drives the job through copy_message.

    Without a real Bot it failed with "'NoneType' object has no attribute
    'copy_message'" and marked the job failed a second time, so the operator's
    only manual recovery path silently made things worse.
    """

    assert "dead_letter_retry" in NEEDS_BOT
