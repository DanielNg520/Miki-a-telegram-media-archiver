"""Automatic source-topic rotation: persisted cycle clock, trigger tick, rotate and cleanup."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from miki_sorter_bot.logging_config import reset_correlation_id, set_correlation_id
from miki_sorter_bot.repositories import RotatedTopicRepository

LOGGER = logging.getLogger(__name__)


class _Repositories(RotatedTopicRepository, Protocol):
    def get_runtime_setting(self, key: str) -> str | None: ...
    def set_runtime_setting(
        self, key: str, value: str, updated_by_user_id: int | None = None
    ) -> None: ...
    def add_audit_event(
        self,
        *,
        actor_type: str,
        actor_id: str,
        action: str,
        outcome: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> int: ...


class RotationService:
    def __init__(
        self,
        settings,
        repositories: _Repositories,
        live_settings,
        activity,
        *,
        clock: Callable[[], float] = time.time,
        notify: Callable[[Any, str], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._repositories = repositories
        self._live = live_settings
        self._activity = activity
        self._clock = clock
        self._notify = notify
        self._lock = asyncio.Lock()

    def _cycle_started(self) -> int:
        thread_id = self._live.effective_source_thread_id()
        raw = self._repositories.get_runtime_setting("cycle_started_at")
        now = int(self._clock())
        if raw is not None:
            try:
                stored_thread, stored_epoch = raw.split(":", 1)
                if int(stored_thread) == thread_id:
                    return int(stored_epoch)
            except ValueError:
                pass
        try:
            self._repositories.set_runtime_setting("cycle_started_at", f"{thread_id}:{now}")
        except Exception:
            LOGGER.warning("Failed to persist cycle start", exc_info=True)
        return now

    def _restart_cycle_clock(self) -> None:
        thread_id = self._live.effective_source_thread_id()
        now = int(self._clock())
        try:
            self._repositories.set_runtime_setting("cycle_started_at", f"{thread_id}:{now}")
        except Exception:
            LOGGER.warning("Failed to persist cycle restart", exc_info=True)

    def _audit(
        self, actor: str, action: str, outcome: str, details: dict[str, Any] | None = None
    ) -> None:
        try:
            self._repositories.add_audit_event(
                actor_type="system" if actor == "system" else "telegram_user",
                actor_id=actor,
                action=action,
                outcome=outcome,
                resource_type="source_topic",
                resource_id=str(self._live.effective_source_thread_id()),
                details=details,
            )
        except Exception:
            LOGGER.warning("Failed to write audit event", exc_info=True)

    def due_reason(self) -> str | None:
        if not self._live.rotate_enabled() or self._live.effective_source_thread_id() == 0:
            return None
        threshold = self._live.rotate_media_threshold()
        if threshold > 0 and self._activity.rotation_count() >= threshold:
            return "media"
        interval = self._live.rotate_interval_seconds()
        if interval > 0 and int(self._clock()) - self._cycle_started() >= interval:
            return "interval"
        return None

    def status_text(self) -> str:
        enabled = self._live.rotate_enabled()
        thread_id = self._live.effective_source_thread_id()
        cycle = self._live.topic_cycle()
        media_count = self._activity.rotation_count()
        threshold = self._live.rotate_media_threshold()
        interval = self._live.rotate_interval_seconds()
        elapsed = int(self._clock()) - self._cycle_started()
        return (
            f"Rotation enabled: {enabled}\n"
            f"Source topic id: {thread_id}\n"
            f"Current cycle: {cycle}\n"
            f"Next cycle: {cycle + 1}\n"
            f"Media count: {media_count}/{threshold}\n"
            f"Hours elapsed: {elapsed / 3600:.1f}/{interval / 3600:.1f}"
        )

    async def _tell(self, context, text: str) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(context, text)
        except Exception:
            LOGGER.warning("Failed to notify admins", exc_info=True)

    async def rotate(self, context, *, actor: str, reason: str) -> str:
        if self._lock.locked():
            return "Rotation already in progress."
        async with self._lock:
            chat_id = self._settings.source_chat_id
            old = self._live.effective_source_thread_id()
            next_cycle = self._live.topic_cycle() + 1
            title = self._live.rotate_topic_title().format(n=next_cycle)
            try:
                created = await context.bot.create_forum_topic(chat_id=chat_id, name=title)
                new_thread_id = created.message_thread_id
            except Exception as error:
                self._audit(
                    actor,
                    "rotation.rotate",
                    "failed",
                    {"reason": reason, "error": str(error)},
                )
                await self._tell(context, f"Topic rotation failed: {error}")
                return f"Rotation failed: {error}"

            problems: list[str] = []

            try:
                self._repositories.set_runtime_setting("source_thread_id", str(new_thread_id))
            except Exception as error:
                LOGGER.warning("Failed to persist source_thread_id", exc_info=True)
                self._audit(
                    actor,
                    "rotation.rotate",
                    "failed",
                    {
                        "reason": reason,
                        "error": str(error),
                        "new_thread_id": new_thread_id,
                    },
                )
                await self._tell(
                    context,
                    f"Rotation failed after creating topic {title} "
                    f"(id {new_thread_id}); source topic unchanged.",
                )
                return (
                    f"Rotation failed: could not persist new source topic "
                    f"({title}, id {new_thread_id})"
                )

            try:
                self._live.registry.set(
                    "topic_cycle",
                    str(next_cycle),
                    self._live.settings,
                    self._live.store,
                    None,
                )
            except Exception as error:
                LOGGER.warning("Failed to set topic_cycle", exc_info=True)
                problems.append(f"set cycle: {error}")

            try:
                self._activity.reset_rotation()
                self._restart_cycle_clock()
            except Exception as error:
                LOGGER.warning("Failed to reset activity or cycle clock", exc_info=True)
                problems.append(f"reset counters: {error}")

            try:
                explicit_roster = self._repositories.get_runtime_setting("periodic_notice_topics")
                if explicit_roster is not None and old in self._live.get("periodic_notice_topics"):
                    roster = set(self._live.get("periodic_notice_topics"))
                    roster.discard(old)
                    roster.add(new_thread_id)
                    raw_roster = ", ".join(str(t) for t in sorted(roster))
                    self._live.registry.set(
                        "periodic_notice_topics",
                        raw_roster,
                        self._live.settings,
                        self._live.store,
                        None,
                    )
            except Exception as error:
                LOGGER.warning("Failed to rewrite notice roster", exc_info=True)
                problems.append(f"rewrite roster: {error}")

            link = (
                f"https://t.me/c/{str(chat_id)[4:]}/{new_thread_id}"
                if str(chat_id).startswith("-100")
                else f"https://t.me/c/{chat_id}/{new_thread_id}"
            )
            if old != 0:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=old,
                        text=f"Topic moved: {link}",
                    )
                except Exception as error:
                    LOGGER.warning("Failed to post pointer in old topic", exc_info=True)
                    problems.append(f"pointer old: {error}")
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=new_thread_id,
                    text=f"New topic: {title}",
                )
            except Exception as error:
                LOGGER.warning("Failed to post announcement in new topic", exc_info=True)
                problems.append(f"announce new: {error}")

            if old != 0:
                try:
                    await context.bot.close_forum_topic(chat_id=chat_id, message_thread_id=old)
                except Exception as error:
                    LOGGER.warning("Failed to close old topic", exc_info=True)
                    problems.append(f"close old: {error}")
                else:
                    try:
                        self._repositories.add_rotated_topic(
                            chat_id, old, next_cycle - 1, int(self._clock())
                        )
                    except Exception as error:
                        LOGGER.warning("Failed to record rotated topic", exc_info=True)
                        problems.append(f"record old: {error}")

            outcome = "success"
            self._audit(
                actor,
                "rotation.rotate",
                outcome,
                {
                    "reason": reason,
                    "old_thread_id": old,
                    "new_thread_id": new_thread_id,
                    "cycle": next_cycle,
                    "problems": problems,
                },
            )
            if problems:
                await self._tell(
                    context,
                    f"Rotation to {title} (id {new_thread_id}) had problems: "
                    + "; ".join(problems),
                )
            summary = f"Rotated to {title} (topic {new_thread_id})"
            if problems:
                summary += "; problems: " + "; ".join(problems)
            return summary

    async def cleanup(self, context, *, confirm: bool, actor: str) -> str:
        delete_seconds = self._live.closed_topic_delete_seconds()
        if delete_seconds == 0:
            return "Closed-topic deletion is disabled."
        chat_id = self._settings.source_chat_id
        current = self._live.effective_source_thread_id()
        eligible = [
            (thread_id, cycle)
            for thread_id, cycle in self._repositories.rotated_topics_due_for_deletion(
                chat_id, int(self._clock()) - delete_seconds
            )
            if thread_id != current
        ]
        if not eligible:
            return "Nothing eligible for deletion."
        if not confirm:
            lines = [f"Cycle {cycle} (topic {thread_id})" for thread_id, cycle in eligible]
            return (
                "Eligible for deletion:\n"
                + "\n".join(lines)
                + "\nRun /rotate_cleanup confirm to delete."
            )
        deleted = 0
        failed = 0
        for thread_id, cycle in eligible:
            try:
                await context.bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
                self._repositories.mark_rotated_topic_deleted(
                    chat_id, thread_id, int(self._clock())
                )
                self._audit(
                    actor,
                    "rotation.cleanup",
                    "success",
                    {"thread_id": thread_id, "cycle": cycle},
                )
                deleted += 1
            except Exception as error:
                LOGGER.warning("Failed to delete rotated topic %s", thread_id, exc_info=True)
                self._audit(
                    actor,
                    "rotation.cleanup",
                    "failed",
                    {"thread_id": thread_id, "cycle": cycle, "error": str(error)},
                )
                failed += 1
        return f"Cleanup finished: {deleted} deleted, {failed} failed."

    async def tick(self, context) -> None:
        if self._lock.locked():
            return
        reason = self.due_reason()
        if reason is None:
            return
        await self.rotate(context, actor="system", reason=reason)


def make_tick_job(service: RotationService) -> Callable[[Any], Any]:
    """Wrap :meth:`tick` with a correlation id for the JobQueue."""

    async def run(context: Any) -> None:
        token = set_correlation_id("topic-rotation")
        try:
            await service.tick(context)
        finally:
            reset_correlation_id(token)

    return run
