from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from miki_sorter_bot.config import TopicForwardingPair
from miki_sorter_bot.management import ManagementCommands
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.sorting import SortingService


def _settings(*admins: int, source_thread_id: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        admin_user_ids=frozenset(admins),
        source_chat_id=-100,
        source_thread_id=source_thread_id,
    )


def _update(text: str, *, user_id: int = 10) -> SimpleNamespace:
    message = SimpleNamespace(text=text, message_thread_id=7, reply_text=AsyncMock())
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
        effective_user=SimpleNamespace(id=user_id),
    )


# --- Repository layer -------------------------------------------------------


def test_runtime_setting_round_trips(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    assert repositories.get_runtime_setting("source_thread_id") is None
    repositories.set_runtime_setting("source_thread_id", "42", 10)
    assert repositories.get_runtime_setting("source_thread_id") == "42"
    repositories.set_runtime_setting("source_thread_id", "43", 11)
    assert repositories.get_runtime_setting("source_thread_id") == "43"


def test_forwarding_pair_is_many_to_one(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.add_forwarding_pair(5, 9, 10)
    repositories.add_forwarding_pair(6, 9, 10)  # second source -> same destination
    assert repositories.get_forwarding_destination(5) == 9
    assert repositories.get_forwarding_destination(6) == 9
    # A source maps to exactly one destination; re-adding replaces it.
    repositories.add_forwarding_pair(5, 12, 10)
    assert repositories.get_forwarding_destination(5) == 12
    pairs = repositories.list_forwarding_pairs()
    assert {(p.source_thread_id, p.destination_thread_id) for p in pairs} == {(5, 12), (6, 9)}
    assert repositories.remove_forwarding_pair(5) is True
    assert repositories.get_forwarding_destination(5) is None
    assert repositories.remove_forwarding_pair(5) is False


def test_seed_forwarding_pairs_runs_once(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.seed_forwarding_pairs((TopicForwardingPair(5, 9),))
    assert repositories.get_forwarding_destination(5) == 9
    # A pair removed via Telegram must not reappear on the next seed.
    repositories.remove_forwarding_pair(5)
    repositories.seed_forwarding_pairs((TopicForwardingPair(5, 9),))
    assert repositories.get_forwarding_destination(5) is None


# --- Permission tiers -------------------------------------------------------


def test_limited_admin_can_add_keywords_but_not_critical_ops(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-100, 7, "Japan")
    repositories.grant_route_manager(-100, 20, 10)  # user 20 = limited admin
    commands = ManagementCommands(_settings(10), repositories)

    # Allowed: keyword management.
    asyncio.run(
        commands.keyword_add(_update("/keyword_add 7 Tokyo", user_id=20), SimpleNamespace())
    )
    assert repositories.list_mappings(-100, thread_id=7)

    # Denied: source topic and forwarding changes.
    source_update = _update("/source_set 99", user_id=20)
    asyncio.run(commands.source_set(source_update, SimpleNamespace()))
    assert repositories.get_runtime_setting("source_thread_id") is None
    source_update.effective_message.reply_text.assert_awaited_once_with(
        "Only a Miki super administrator can do that."
    )

    forward_update = _update("/forward_add 5 9", user_id=20)
    asyncio.run(commands.forward_add(forward_update, SimpleNamespace()))
    assert repositories.get_forwarding_destination(5) is None


def test_super_admin_sets_source_topic_and_forwarding(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    commands = ManagementCommands(_settings(10), repositories)

    asyncio.run(commands.source_set(_update("/source_set 88", user_id=10), SimpleNamespace()))
    assert repositories.get_runtime_setting("source_thread_id") == "88"

    asyncio.run(commands.forward_add(_update("/forward_add 5 9", user_id=10), SimpleNamespace()))
    assert repositories.get_forwarding_destination(5) == 9


def test_source_show_reports_override_and_default(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    commands = ManagementCommands(_settings(10), repositories)

    update = _update("/source_show", user_id=10)
    asyncio.run(commands.source_show(update, SimpleNamespace()))
    update.effective_message.reply_text.assert_awaited_once_with(
        "Listening to source topic 5 (from .env; no runtime override set)."
    )

    repositories.set_runtime_setting("source_thread_id", "88", 10)
    override_update = _update("/source_show", user_id=10)
    asyncio.run(commands.source_show(override_update, SimpleNamespace()))
    override_update.effective_message.reply_text.assert_awaited_once_with(
        "Listening to source topic 88 (runtime override; .env default is 5)."
    )


def test_limited_admin_can_list_but_not_change_forwarding(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.grant_route_manager(-100, 20, 10)
    repositories.add_forwarding_pair(5, 9, 10)
    commands = ManagementCommands(_settings(10), repositories)

    update = _update("/forward_list", user_id=20)
    asyncio.run(commands.forward_list(update, SimpleNamespace()))
    update.effective_message.reply_text.assert_awaited_once_with(
        "Forwarding pairs (source -> destination):\n- 5 -> 9"
    )


# --- Sorter reads runtime config live --------------------------------------


def test_sorter_uses_runtime_source_thread_override(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = SimpleNamespace(
        source_chat_id=-100,
        source_thread_id=5,
        archive_chat_id=-200,
        topic_forwarding_pairs=(),
    )
    sorting = SortingService(settings, repositories, indexing=SimpleNamespace())

    assert sorting._effective_source_thread_id() == 5
    repositories.set_runtime_setting("source_thread_id", "77", 10)
    assert sorting._effective_source_thread_id() == 77  # no restart needed


def test_sorter_reads_forwarding_pair_from_database(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = SimpleNamespace(
        source_chat_id=-100,
        source_thread_id=5,
        archive_chat_id=-200,
        topic_forwarding_pairs=(),
    )
    sorting = SortingService(settings, repositories, indexing=SimpleNamespace())

    assert sorting._forwarding_pair(-100, 6) is None
    repositories.add_forwarding_pair(6, 9, 10)
    pair = sorting._forwarding_pair(-100, 6)
    assert pair is not None
    assert (pair.source_thread_id, pair.destination_thread_id) == (6, 9)
    # Forwarding only applies within the configured source chat.
    assert sorting._forwarding_pair(-999, 6) is None
