"""Automatic source-topic rotation: persisted cycle clock, trigger tick, rotate and cleanup."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from telegram.error import BadRequest

from miki_sorter_bot.logging_config import reset_correlation_id, set_correlation_id
from miki_sorter_bot.repositories import RotatedTopicRepository

LOGGER = logging.getLogger(__name__)


class _Repositories(RotatedTopicRepository, Protocol):
    def get_runtime_setting(self, key: str) -> str | None: ...
    def set_runtime_setting(
        self, key: str, value: str, updated_by_user_id: int | None = None
    ) -> None: ...
    def delete_runtime_setting(self, key: str) -> bool: ...
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
        deletion: Any | None = None,
    ) -> None:
        self._settings = settings
        self._repositories = repositories
        self._live = live_settings
        self._activity = activity
        self._clock = clock
        self._notify = notify
        self._deletion = deletion
        self._lock = asyncio.Lock()

    def _pending(self) -> tuple[int, int, int] | None:
        raw = self._repositories.get_runtime_setting("rotation_pending")
        if raw is None:
            return None
        parts = raw.split(":")
        if len(parts) != 3:
            return None
        try:
            return int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            return None

    def _set_pending(self, old: int, new: int, cycle: int) -> None:
        self._repositories.set_runtime_setting("rotation_pending", f"{old}:{new}:{cycle}")

    def _clear_pending(self) -> None:
        try:
            self._repositories.delete_runtime_setting("rotation_pending")
        except Exception:
            LOGGER.warning("Failed to clear rotation_pending", exc_info=True)

    def _backoff(self) -> tuple[int, int] | None:
        raw = self._repositories.get_runtime_setting("rotation_retry")
        if raw is None:
            return None
        parts = raw.split(":")
        if len(parts) != 2:
            return None
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None

    def _backoff_active(self) -> bool:
        parsed = self._backoff()
        if parsed is None:
            return False
        retry_at, _ = parsed
        return int(self._clock()) < retry_at

    def _record_failure(self) -> None:
        previous = self._backoff()
        failures = 1 if previous is None else previous[1] + 1
        delay = min(300 * 2 ** (failures - 1), 21600)
        retry_at = int(self._clock()) + delay
        try:
            self._repositories.set_runtime_setting("rotation_retry", f"{retry_at}:{failures}")
        except Exception:
            LOGGER.warning("Failed to persist rotation_retry", exc_info=True)

    def _clear_backoff(self) -> None:
        try:
            self._repositories.delete_runtime_setting("rotation_retry")
        except Exception:
            LOGGER.warning("Failed to clear rotation_retry", exc_info=True)

    def _record_last(self, ok: bool, text: str) -> None:
        payload = json.dumps({"at": int(self._clock()), "ok": ok, "text": text[:200]})
        try:
            self._repositories.set_runtime_setting("rotation_last", payload)
        except Exception:
            LOGGER.warning("Failed to persist rotation_last", exc_info=True)

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
        if interval > 0:
            cycle_started = self._cycle_started()
            if (
                self._activity.rotation_count() >= 1
                and int(self._clock()) - cycle_started >= interval
            ):
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

        lines = [
            f"Rotation enabled: {enabled}",
            f"Source topic id: {thread_id}",
            f"Current cycle: {cycle}",
            f"Next cycle: {cycle + 1}",
            f"Media count: {media_count}/{threshold}",
            f"Hours elapsed: {elapsed / 3600:.1f}/{interval / 3600:.1f}",
        ]

        delete_seconds = self._live.closed_topic_delete_seconds()
        if delete_seconds > 0:
            eligible = len(
                self._repositories.rotated_topics_due_for_deletion(
                    self._settings.source_chat_id,
                    int(self._clock()) - delete_seconds,
                )
            )
            lines.append(f"Eligible closed topics: {eligible}")

        pending = self._pending()
        if pending is not None:
            old, new, pending_cycle = pending
            lines.append(
                f"Pending rotation: {old} -> {new} (cycle {pending_cycle}); "
                "will resume automatically"
            )

        backoff = self._backoff()
        if backoff is not None:
            retry_at, failures = backoff
            remaining = max(0, retry_at - int(self._clock()))
            lines.append(f"Retry backoff: failures {failures}, retry in {remaining / 60:.0f} min")

        raw_last = self._repositories.get_runtime_setting("rotation_last")
        if raw_last is not None:
            try:
                last = json.loads(raw_last)
                at = int(last["at"])
                ok = bool(last["ok"])
                text = str(last["text"])
                hours_ago = (int(self._clock()) - at) / 3600
                state = "ok" if ok else "failed"
                lines.append(f"Last rotation: {state} {hours_ago:.1f}h ago - {text}")
            except (ValueError, KeyError, TypeError):
                pass

        return "\n".join(lines)

    def _milestones_sent(self) -> set[str]:
        raw = self._repositories.get_runtime_setting("rotation_milestones")
        if raw is None:
            return set()
        try:
            thread_str, cycle_str, labels_str = raw.split(":", 2)
            thread_id = int(thread_str)
            cycle = int(cycle_str)
        except (ValueError, TypeError):
            LOGGER.warning("Malformed rotation_milestones state", exc_info=True)
            return set()
        if thread_id != self._live.effective_source_thread_id():
            return set()
        if cycle != self._live.topic_cycle():
            return set()
        labels = {label.strip() for label in labels_str.split(",") if label.strip()}
        return labels

    def _mark_milestones(self, labels: set[str]) -> None:
        announced = self._milestones_sent()
        announced.update(labels)
        thread_id = self._live.effective_source_thread_id()
        cycle = self._live.topic_cycle()
        raw_labels = ",".join(sorted(announced))
        try:
            self._repositories.set_runtime_setting(
                "rotation_milestones",
                f"{thread_id}:{cycle}:{raw_labels}",
            )
        except Exception:
            LOGGER.warning("Failed to persist rotation_milestones", exc_info=True)

    def _due_milestones(self) -> tuple[set[str], list[str]]:
        if not self._live.rotate_enabled():
            return set(), []
        if not self._live.rotate_milestones_enabled():
            return set(), []
        thread_id = self._live.effective_source_thread_id()
        if thread_id == 0:
            return set(), []
        announced = self._milestones_sent()
        labels: set[str] = set()
        messages: list[str] = []
        next_title = self._live.rotate_topic_title().format(n=self._live.topic_cycle() + 1)
        threshold = self._live.rotate_media_threshold()
        if threshold > 0:
            count = self._activity.rotation_count()
            crossed_levels = [level for level in (80, 90, 100) if count * 100 >= level * threshold]
            if crossed_levels:
                highest = crossed_levels[-1]
                new_label = f"media{highest}"
                if new_label not in announced:
                    labels.update(f"media{level}" for level in crossed_levels)
                    if highest == 100:
                        messages.append(
                            f"Media threshold reached ({count}/{threshold} posts). "
                            f"Moving to {next_title} now."
                        )
                    else:
                        messages.append(
                            f"{highest}% of media threshold reached "
                            f"({count}/{threshold} posts). Next topic: {next_title}."
                        )
        interval = self._live.rotate_interval_seconds()
        if interval > 0 and self._activity.rotation_count() >= 1:
            elapsed = int(self._clock()) - self._cycle_started()
            crossed_levels = [level for level in (80, 90, 100) if elapsed * 100 >= level * interval]
            if crossed_levels:
                highest = crossed_levels[-1]
                new_label = f"time{highest}"
                if new_label not in announced:
                    labels.update(f"time{level}" for level in crossed_levels)
                    if highest == 100:
                        messages.append(
                            f"Time interval reached "
                            f"({elapsed / 3600:.1f}h of {interval / 3600:.1f}h). "
                            f"Moving to {next_title} now."
                        )
                    else:
                        messages.append(
                            f"{highest}% of time interval reached "
                            f"({elapsed / 3600:.1f}h of {interval / 3600:.1f}h). "
                            f"Next topic: {next_title}."
                        )
            if (
                interval > 2 * 86400
                and elapsed < interval
                and interval - elapsed <= 86400
                and "timeday" not in announced
            ):
                labels.add("timeday")
                messages.append(f"This topic rotates within 24 hours. Next topic: {next_title}.")
        return labels, messages

    async def _announce_milestones(self, context) -> None:
        labels, messages = self._due_milestones()
        if not labels and not messages:
            return
        self._mark_milestones(labels)
        thread_id = self._live.effective_source_thread_id()
        for text in messages:
            try:
                sent = await context.bot.send_message(
                    chat_id=self._settings.source_chat_id,
                    message_thread_id=thread_id,
                    text=text,
                )
                if self._deletion is not None:
                    self._deletion.schedule(
                        self._settings.source_chat_id,
                        sent.message_id,
                        86400,
                    )
            except Exception:
                LOGGER.warning("Failed to post milestone notice", exc_info=True)

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
            pending = self._pending()
            if pending is not None:
                old, new, cycle = pending
                return await self._finish(context, old, new, cycle, actor=actor, reason=reason)

            chat_id = self._settings.source_chat_id
            old = self._live.effective_source_thread_id()
            cycle = self._live.topic_cycle() + 1
            title = self._live.rotate_topic_title().format(n=cycle)
            try:
                created = await context.bot.create_forum_topic(chat_id=chat_id, name=title)
                new_thread_id = created.message_thread_id
            except Exception as error:
                self._record_failure()
                self._audit(
                    actor,
                    "rotation.rotate",
                    "failed",
                    {"reason": reason, "error": str(error)},
                )
                self._record_last(False, f"create topic failed: {error}")
                await self._tell(context, f"Topic rotation failed: {error}")
                return f"Rotation failed: {error}"

            try:
                self._set_pending(old, new_thread_id, cycle)
            except Exception as error:
                self._record_failure()
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
                self._record_last(False, f"persist pending failed: {error}")
                await self._tell(
                    context,
                    f"Rotation failed after creating topic {title} "
                    f"(id {new_thread_id}); topic is orphaned and must be reported.",
                )
                return (
                    f"Rotation failed: created topic {title} (id {new_thread_id}) "
                    f"but could not persist pending state: {error}"
                )

            return await self._finish(
                context, old, new_thread_id, cycle, actor=actor, reason=reason
            )

    async def _finish(
        self, context, old: int, new: int, cycle: int, *, actor: str, reason: str
    ) -> str:
        title = self._live.rotate_topic_title().format(n=cycle)
        chat_id = self._settings.source_chat_id
        problems: list[str] = []

        try:
            self._repositories.set_runtime_setting("source_thread_id", str(new))
        except Exception as error:
            self._record_failure()
            self._audit(
                actor,
                "rotation.rotate",
                "failed",
                {"reason": reason, "error": str(error), "new_thread_id": new},
            )
            self._record_last(False, f"persist source_thread_id failed: {error}")
            await self._tell(
                context,
                f"Rotation switch to topic {title} (id {new}) failed: {error}; "
                "will retry automatically.",
            )
            return (
                f"Rotation switch failed: could not persist source_thread_id "
                f"({title}, id {new}); will retry automatically."
            )

        try:
            self._live.registry.set(
                "topic_cycle",
                str(cycle),
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
            if (
                explicit_roster is not None
                and old != 0
                and old in self._live.get("periodic_notice_topics")
            ):
                roster = set(self._live.get("periodic_notice_topics"))
                roster.discard(old)
                roster.add(new)
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

        if old != 0:
            try:
                self._repositories.retarget_bridges(old, new)
            except Exception as error:
                LOGGER.warning("Failed to retarget bridges", exc_info=True)
                problems.append(f"retarget bridges: {error}")

        link = (
            f"https://t.me/c/{str(chat_id)[4:]}/{new}"
            if str(chat_id).startswith("-100")
            else f"https://t.me/c/{chat_id}/{new}"
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
                message_thread_id=new,
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
                        chat_id, old, cycle - 1, int(self._clock())
                    )
                except Exception as error:
                    LOGGER.warning("Failed to record rotated topic", exc_info=True)
                    problems.append(f"record old: {error}")

        self._clear_pending()
        self._clear_backoff()
        self._audit(
            actor,
            "rotation.rotate",
            "success",
            {
                "reason": reason,
                "old_thread_id": old,
                "new_thread_id": new,
                "cycle": cycle,
                "problems": problems,
            },
        )
        summary = f"Rotated to {title} (topic {new})"
        if problems:
            summary += "; problems: " + "; ".join(problems)
        self._record_last(True, summary)
        if problems:
            await self._tell(
                context,
                f"Rotation to {title} (id {new}) had problems: " + "; ".join(problems),
            )
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
            extra = len(eligible) - 30
            preview = lines[:30]
            if extra > 0:
                preview.append(f"...and {extra} more")
            return (
                "Eligible for deletion:\n"
                + "\n".join(preview)
                + "\nRun /rotate_cleanup confirm to delete."
            )
        deleted = 0
        failed = 0
        for thread_id, cycle in eligible:
            already_gone = False
            try:
                await context.bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
            except Exception as error:
                if isinstance(error, BadRequest):
                    msg = str(error).lower()
                    if "topic_id_invalid" in msg or "not found" in msg:
                        already_gone = True
                    else:
                        LOGGER.warning(
                            "Failed to delete rotated topic %s", thread_id, exc_info=True
                        )
                        self._audit(
                            actor,
                            "rotation.cleanup",
                            "failed",
                            {"thread_id": thread_id, "cycle": cycle, "error": str(error)},
                        )
                        failed += 1
                        continue
                else:
                    LOGGER.warning("Failed to delete rotated topic %s", thread_id, exc_info=True)
                    self._audit(
                        actor,
                        "rotation.cleanup",
                        "failed",
                        {"thread_id": thread_id, "cycle": cycle, "error": str(error)},
                    )
                    failed += 1
                    continue
            try:
                self._repositories.mark_rotated_topic_deleted(
                    chat_id, thread_id, int(self._clock())
                )
            except Exception:
                LOGGER.warning("Failed to mark rotated topic %s deleted", thread_id, exc_info=True)
            self._audit(
                actor,
                "rotation.cleanup",
                "success",
                {"thread_id": thread_id, "cycle": cycle, "already_gone": already_gone},
            )
            deleted += 1
        return f"Cleanup finished: {deleted} deleted, {failed} failed."

    async def tick(self, context) -> None:
        if self._lock.locked():
            return
        if self._backoff_active():
            return
        if self._pending() is not None:
            await self.rotate(context, actor="system", reason="resume")
            return
        await self._announce_milestones(context)
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
