from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import NetworkError

from miki_sorter_bot.operations import OperationsService
from miki_sorter_bot.reliability import DeliveryExecutor, RateLimiter, RetryPolicy
from miki_sorter_bot.storage import Storage


def test_operations_metrics_maintenance_backup_and_restore(tmp_path) -> None:
    database_path = tmp_path / "miki.sqlite3"
    storage = Storage(database_path)
    repositories = storage.open()
    repositories.increment_metric("duplicates", 2)
    operations = OperationsService(
        repositories,
        storage,
        backup_directory=tmp_path / "backups",
        transient_retention_days=30,
        audit_retention_days=90,
    )

    report = asyncio.run(operations.health(SimpleNamespace(get_me=AsyncMock())))
    deleted = operations.maintain()
    backup = operations.backup()
    restored = tmp_path / "restored.sqlite3"
    Storage.restore_backup(backup, restored)

    assert report.healthy
    assert report.details["metrics"]["duplicates"] == 2
    assert set(deleted) == {
        "processed_updates",
        "resolved_dead_letters",
        "integration_nonces",
        "integration_usage",
        "audit_events",
    }
    Storage.verify_database(restored)
    storage.close()


def test_rapid_backups_use_distinct_verified_files(tmp_path) -> None:
    storage = Storage(tmp_path / "miki.sqlite3")
    storage.open()

    first = storage.backup(tmp_path / "backups")
    second = storage.backup(tmp_path / "backups")

    assert first != second
    Storage.verify_database(first)
    Storage.verify_database(second)
    storage.close()


def test_delivery_metrics_cover_retry_failure_and_timing(tmp_path) -> None:
    storage = Storage(tmp_path / "miki.sqlite3")
    repositories = storage.open()
    executor = DeliveryExecutor(
        retry_policy=RetryPolicy(attempts=2, base_delay=0, max_delay=0),
        rate_limiter=RateLimiter(1000),
        sleep=AsyncMock(),
        metric=repositories.increment_metric,
    )
    operation = AsyncMock(side_effect=[NetworkError("temporary"), "ok"])

    assert asyncio.run(executor.run(operation)) == "ok"

    metrics = repositories.metrics_snapshot()
    assert metrics["telegram_retries"] == 1
    assert metrics["telegram_delivery_successes"] == 1
    assert metrics["telegram_delivery_operations"] == 1
    assert metrics["telegram_delivery_duration_ms"] >= 0
    storage.close()
