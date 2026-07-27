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
    completed_job = repositories.enqueue("sort", "sort:completed", {})
    repositories.update_job(completed_job.id, "completed")
    repositories.add_dead_letter(
        completed_job.id,
        "sort_copy",
        {},
        "transient",
        "legacy residue",
    )
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
        "completed_job_dead_letters",
        "processed_updates",
        "resolved_dead_letters",
        "integration_nonces",
        "integration_usage",
        "audit_events",
    }
    assert deleted["completed_job_dead_letters"] == 1
    assert repositories.list_dead_letters() == []
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


def test_backup_and_prune_enforces_retention_window(tmp_path) -> None:
    storage = Storage(tmp_path / "miki.sqlite3")
    repositories = storage.open()
    backups = tmp_path / "backups"
    operations = OperationsService(
        repositories,
        storage,
        backup_directory=backups,
        transient_retention_days=30,
        audit_retention_days=90,
    )

    created = []
    for _ in range(5):
        destination, pruned = operations.backup_and_prune(keep=3)
        created.append(destination)
        assert destination not in pruned

    surviving = sorted(backups.glob("miki-*.sqlite3"))
    assert len(surviving) == 3
    # The three newest backups (by timestamped filename) are the ones retained.
    assert surviving == sorted(created[-3:])
    for path in surviving:
        Storage.verify_database(path)
    storage.close()


def test_prune_backups_rejects_non_positive_keep(tmp_path) -> None:
    storage = Storage(tmp_path / "miki.sqlite3")
    storage.open()
    operations = OperationsService(
        storage.open(),
        storage,
        backup_directory=tmp_path / "backups",
        transient_retention_days=30,
        audit_retention_days=90,
    )

    try:
        operations.prune_backups(0)
    except ValueError:
        pass
    else:  # pragma: no cover - guard
        raise AssertionError("expected ValueError for keep < 1")
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
