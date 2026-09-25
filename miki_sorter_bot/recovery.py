from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from miki_sorter_bot.repositories import JobRecord, SqliteRepositories
from miki_sorter_bot.retrieval import RetrievalService
from miki_sorter_bot.sorting import SortingService

LOGGER = logging.getLogger(__name__)
RecoveryStrategy = Callable[[int, Any], Awaitable[bool]]


class JobRecoveryService:
    """Bounded strategy dispatcher for durable jobs left pending after failures."""

    def __init__(
        self,
        repositories: SqliteRepositories,
        sorting: SortingService,
        retrieval: RetrievalService,
        *,
        batch_size: int = 100,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("recovery batch size must be between 1 and 1000")
        self._repositories = repositories
        self._batch_size = batch_size
        self._strategies: dict[str, RecoveryStrategy] = {
            "sort": sorting.resume_job,
            "retrieve": retrieval.resume_job,
        }
        self._lock = asyncio.Lock()

    async def run_once(self, context: Any) -> int:
        if self._lock.locked():
            return 0
        async with self._lock:
            recovered = 0
            for job in self._repositories.list_pending_jobs(self._batch_size):
                if await self._recover(job, context):
                    recovered += 1
            if recovered:
                self._repositories.increment_metric("jobs_recovered", recovered)
                LOGGER.info("Recovered pending jobs", extra={"count": recovered})
            return recovered

    async def resume_job(self, job_id: int, context: Any) -> bool:
        job = self._repositories.get_job(job_id)
        if job is None or job.status != "pending":
            return False
        return await self._recover(job, context)

    async def _recover(self, job: JobRecord, context: Any) -> bool:
        strategy = self._strategies.get(job.kind)
        if strategy is None:
            self._fail(job, f"no recovery strategy for job kind {job.kind}")
            return False
        try:
            return await strategy(job.id, context)
        except Exception as error:
            current = self._repositories.get_job(job.id)
            if current is None or current.status != "failed":
                self._fail(job, str(error))
            else:
                self._repositories.increment_metric("job_recovery_failures", 1)
            LOGGER.exception(
                "Pending job recovery failed",
                extra={"job_id": job.id, "job_kind": job.kind},
            )
            return False

    def _fail(self, job: JobRecord, message: str) -> None:
        self._repositories.update_job(job.id, "failed", error=message)
        self._repositories.add_dead_letter(
            job.id,
            "job_recovery",
            job.payload,
            "recovery_failed",
            message,
        )
        self._repositories.increment_metric("job_recovery_failures", 1)
