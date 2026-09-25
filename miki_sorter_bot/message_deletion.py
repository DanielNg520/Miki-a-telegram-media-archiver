"""Timed message deletion: a persisted queue swept by one JobQueue job."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from miki_sorter_bot.logging_config import reset_correlation_id, set_correlation_id
from miki_sorter_bot.reliability import classify_error
from miki_sorter_bot.repositories import ScheduledDeletionRepository

RETRY_DELAY_SECONDS = 300

LOGGER = logging.getLogger(__name__)


class MessageDeletionService:
    def __init__(
        self,
        repositories: ScheduledDeletionRepository,
        *,
        clock: Callable[[], float] = time.time,
        batch_size: int = 50,
    ) -> None:
        self._repositories = repositories
        self._clock = clock
        self._batch_size = batch_size

    def schedule(self, chat_id: int, message_id: int, delay_seconds: float) -> None:
        if delay_seconds <= 0:
            return
        delete_at = int(self._clock() + delay_seconds)
        self._repositories.schedule_deletion(chat_id, message_id, delete_at)

    async def sweep(self, context: Any) -> None:
        now = int(self._clock())
        due = self._repositories.due_deletions(now, self._batch_size)
        for chat_id, message_id in due:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception as error:
                LOGGER.warning(
                    "Failed to delete message %s in chat %s: %s",
                    message_id,
                    chat_id,
                    error,
                )
                failure = classify_error(error)
                if failure.retry_after is not None:
                    return
                self._repositories.remove_deletion(chat_id, message_id)
                if failure.retryable:
                    # Re-queue behind the rest so a stuck row cannot block the batch.
                    self._repositories.schedule_deletion(
                        chat_id, message_id, int(self._clock()) + RETRY_DELAY_SECONDS
                    )
                else:
                    self._repositories.increment_metric("scheduled_deletions_failed")
                continue
            self._repositories.remove_deletion(chat_id, message_id)


def make_sweep_job(service: MessageDeletionService) -> Callable[[Any], Any]:
    """Wrap :meth:`sweep` with a correlation id for the JobQueue."""

    async def run(context: Any) -> None:
        token = set_correlation_id("message-deletion")
        try:
            await service.sweep(context)
        finally:
            reset_correlation_id(token)

    return run
