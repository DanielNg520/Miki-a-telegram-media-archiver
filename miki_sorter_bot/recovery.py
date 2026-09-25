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
        failed_max_attempts: int = 5,
        failed_cooldown_minutes: int = 5,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("recovery batch size must be between 1 and 1000")
        self._repositories = repositories
        self._batch_size = batch_size
        self._failed_max_attempts = failed_max_attempts
        self._failed_cooldown_minutes = failed_cooldown_minutes
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
            return recovered + await self._retry_undelivered_failures(context)

    async def _retry_undelivered_failures(self, context: Any) -> int:
        """Re-drive jobs left ``failed`` with nothing delivered.

        A grouped album upload that times out marks every member's job failed
        and dead-letters it, and the pending sweep above never looks at failed
        jobs — so those members were stranded permanently. ``resume_job``
        re-delivers each one individually via ``copy_message``; ``claim_job``
        already accepts ``failed``, and the delivery row keyed on
        (source, destination) keeps a second attempt idempotent.
        """

        jobs = self._repositories.list_undelivered_failed_jobs(
            self._batch_size,
            max_attempts=self._failed_max_attempts,
            cooldown_minutes=self._failed_cooldown_minutes,
        )
        if not jobs:
            return 0
        retried = 0
        for job in jobs:
            strategy = self._strategies.get(job.kind)
            if strategy is None:
                self._repositories.exhaust_failed_job(
                    job.id,
                    f"no recovery strategy for job kind {job.kind}",
                )
                continue
            try:
                if await strategy(job.id, context):
                    retried += 1
            except Exception as error:
                # Deliberately no dead letter here: this job already has one from
                # the original failure, and re-adding one every sweep would grow
                # the table without bound. Charge an attempt instead so a job
                # that can never succeed drops out once it hits the cap.
                self._repositories.exhaust_failed_job(job.id, str(error))
                self._repositories.increment_metric("job_recovery_failures", 1)
                LOGGER.warning(
                    "Retry of an undelivered failed job did not succeed",
                    extra={"job_id": job.id, "job_kind": job.kind, "error": str(error)},
                )
        self._repositories.increment_metric("failed_jobs_retried", len(jobs))
        if retried:
            self._repositories.increment_metric("jobs_recovered", retried)
        LOGGER.info(
            "Retried undelivered failed jobs",
            extra={"attempted": len(jobs), "recovered": retried},
        )
        return retried

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
