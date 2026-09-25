from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import Forbidden, NetworkError, RetryAfter

from miki_sorter_bot.message_deletion import MessageDeletionService, make_sweep_job
from miki_sorter_bot.repositories import SqliteRepositories


def test_sweep_before_due_deletes_nothing(database_connection):
    repositories = SqliteRepositories(database_connection)
    clock_value = [1000]
    service = MessageDeletionService(repositories, clock=lambda: clock_value[0])
    service.schedule(10, 20, 300)

    context = SimpleNamespace(bot=SimpleNamespace(delete_message=AsyncMock()))
    asyncio.run(service.sweep(context))

    context.bot.delete_message.assert_not_awaited()


def test_sweep_after_due_deletes_once(database_connection):
    repositories = SqliteRepositories(database_connection)
    clock_value = [1000]
    service = MessageDeletionService(repositories, clock=lambda: clock_value[0])
    service.schedule(10, 20, 300)

    clock_value[0] = 1300
    context = SimpleNamespace(bot=SimpleNamespace(delete_message=AsyncMock()))
    asyncio.run(service.sweep(context))

    context.bot.delete_message.assert_awaited_once_with(chat_id=10, message_id=20)

    context.bot.delete_message.reset_mock()
    asyncio.run(service.sweep(context))
    context.bot.delete_message.assert_not_awaited()


def test_delay_zero_schedules_nothing(database_connection):
    repositories = SqliteRepositories(database_connection)
    service = MessageDeletionService(repositories)
    service.schedule(11, 21, 0)

    assert repositories.due_deletions(10**12, 10) == []


def test_restart_survives(database_connection):
    repositories = SqliteRepositories(database_connection)
    clock_value = [1000]
    service = MessageDeletionService(repositories, clock=lambda: clock_value[0])
    service.schedule(12, 22, 300)

    clock_value[0] = 1300
    new_service = MessageDeletionService(repositories, clock=lambda: clock_value[0])
    context = SimpleNamespace(bot=SimpleNamespace(delete_message=AsyncMock()))
    asyncio.run(new_service.sweep(context))

    context.bot.delete_message.assert_awaited_once_with(chat_id=12, message_id=22)


def test_rescheduling_does_not_push_deadline(database_connection):
    repositories = SqliteRepositories(database_connection)
    clock_value = [1000]
    service = MessageDeletionService(repositories, clock=lambda: clock_value[0])
    service.schedule(13, 23, 300)

    clock_value[0] = 1100
    service.schedule(13, 23, 300)

    clock_value[0] = 1300
    context = SimpleNamespace(bot=SimpleNamespace(delete_message=AsyncMock()))
    asyncio.run(service.sweep(context))

    context.bot.delete_message.assert_awaited_once_with(chat_id=13, message_id=23)


def test_forbidden_drops_row_and_counts_metric(database_connection):
    repositories = SqliteRepositories(database_connection)
    clock_value = [1000]
    service = MessageDeletionService(repositories, clock=lambda: clock_value[0])
    service.schedule(14, 24, 300)

    clock_value[0] = 1300
    context = SimpleNamespace(
        bot=SimpleNamespace(delete_message=AsyncMock(side_effect=Forbidden("x")))
    )
    asyncio.run(service.sweep(context))

    assert repositories.metrics_snapshot().get("scheduled_deletions_failed", 0) == 1
    assert repositories.due_deletions(10**12, 10) == []


def test_network_error_keeps_row_and_does_not_count(database_connection):
    repositories = SqliteRepositories(database_connection)
    clock_value = [1000]
    service = MessageDeletionService(repositories, clock=lambda: clock_value[0])
    service.schedule(15, 25, 300)

    clock_value[0] = 1300
    context = SimpleNamespace(
        bot=SimpleNamespace(delete_message=AsyncMock(side_effect=NetworkError("x")))
    )
    asyncio.run(service.sweep(context))

    assert repositories.metrics_snapshot().get("scheduled_deletions_failed", 0) == 0
    assert repositories.due_deletions(10**12, 10) == [(15, 25)]


def test_retry_after_stops_sweep_immediately(database_connection):
    repositories = SqliteRepositories(database_connection)
    clock_value = [1000]
    service = MessageDeletionService(repositories, clock=lambda: clock_value[0])
    service.schedule(16, 26, 300)
    service.schedule(17, 27, 300)
    service.schedule(18, 28, 300)

    clock_value[0] = 1300
    context = SimpleNamespace(
        bot=SimpleNamespace(delete_message=AsyncMock(side_effect=RetryAfter(5)))
    )
    asyncio.run(service.sweep(context))

    context.bot.delete_message.assert_awaited_once()
    assert sorted(repositories.due_deletions(10**12, 10)) == [(16, 26), (17, 27), (18, 28)]


def test_batch_size_limits_sweep(database_connection):
    repositories = SqliteRepositories(database_connection)
    clock_value = [1000]
    service = MessageDeletionService(repositories, clock=lambda: clock_value[0], batch_size=2)
    for chat_id, message_id in [(1, 101), (2, 102), (3, 103), (4, 104), (5, 105)]:
        service.schedule(chat_id, message_id, 300)

    clock_value[0] = 1300
    context = SimpleNamespace(bot=SimpleNamespace(delete_message=AsyncMock()))
    asyncio.run(service.sweep(context))

    assert context.bot.delete_message.await_count == 2
    assert len(repositories.due_deletions(10**12, 10)) == 3


def test_make_sweep_job_runs_sweep(database_connection):
    repositories = SqliteRepositories(database_connection)
    clock_value = [1000]
    service = MessageDeletionService(repositories, clock=lambda: clock_value[0])
    service.schedule(19, 29, 300)

    clock_value[0] = 1300
    context = SimpleNamespace(bot=SimpleNamespace(delete_message=AsyncMock()))
    job = make_sweep_job(service)
    asyncio.run(job(context))

    context.bot.delete_message.assert_awaited_once_with(chat_id=19, message_id=29)
