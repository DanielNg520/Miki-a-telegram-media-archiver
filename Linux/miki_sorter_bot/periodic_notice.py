"""Periodic reminder notices posted back into source topics.

Miki can drop a short, operator-authored message into a source topic on a
cadence, deleting the previous copy first so only the newest reminder is ever
visible. Two triggers drive it, whichever fires first:

- **Count** — after the Nth media message in the topic (an album counts as one
  message, not one per photo), then a short quiet gap so the reminder lands
  after the batch finishes rather than mid-send.
- **Interval** — post once every configured number of minutes.

Both are gated on *new activity*: a topic with no media since its last notice
is skipped, so a quiet topic never churns delete/re-post cycles.

Design notes:
- **No new process.** Counting piggybacks on the sorting hot path
  (:meth:`on_media`); the interval piggybacks on the shared ``JobQueue``
  (:meth:`tick`). The only persistent state is the last message id per topic,
  kept in the runtime-settings KV so a restart can still clean up the previous
  notice.
- **In-memory counters** reset to 0 on restart, mirroring look-back/albums.
- **Best-effort.** A failed delete (notice too old, already removed) is logged
  and never blocks posting the replacement.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from miki_sorter_bot.logging_config import set_correlation_id, reset_correlation_id
from miki_sorter_bot.settings_registry import LiveSettings

LOGGER = logging.getLogger(__name__)

# Quiet gap required after the last media before the reminder posts. The timer
# resets on every new media once the threshold is reached, so the reminder lands
# after a batch/album finishes rather than mid-send. Not operator-tunable.
POST_DELAY_SECONDS = 5.0

# Runtime-settings KV key prefix for the last notice message id, per topic.
_LAST_MESSAGE_KEY = "periodic_notice_last_message_id"
# Runtime-settings KV key holding the operator-authored notice body.
TEXT_KEY = "periodic_notice_text"

# An album (media group) arrives as several separate messages; we count it once.
# Members of one album land within seconds, so a short remembering window with a
# bounded size is enough to collapse them into a single "message with media".
_GROUP_DEDUP_WINDOW_SECONDS = 300.0
_GROUP_DEDUP_MAX = 128


class RuntimeStore(Protocol):
    """Structural type: the KV slice this service persists into."""

    def get_runtime_setting(self, key: str) -> str | None: ...  # pragma: no cover

    def set_runtime_setting(
        self, key: str, value: str, updated_by_user_id: int | None = None
    ) -> None: ...  # pragma: no cover

    def delete_runtime_setting(self, key: str) -> bool: ...  # pragma: no cover


@dataclass
class _TopicState:
    count: int = 0
    last_post_at: float = 0.0
    post_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # media_group_id -> last-seen time, so repeated album members count once.
    recent_groups: dict[str, float] = field(default_factory=dict)


class PeriodicNoticeService:
    def __init__(
        self,
        settings: Any,
        repositories: RuntimeStore,
        live_settings: LiveSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        post_delay_seconds: float = POST_DELAY_SECONDS,
    ) -> None:
        self._settings = settings
        self._repositories = repositories
        self._live = live_settings
        self._clock = clock
        self._post_delay = post_delay_seconds
        self._state: dict[int, _TopicState] = {}

    # -- text (operator-authored, stored outside the typed registry) --------
    def get_text(self) -> str:
        return self._repositories.get_runtime_setting(TEXT_KEY) or ""

    def set_text(self, text: str, user_id: int | None = None) -> None:
        self._repositories.set_runtime_setting(TEXT_KEY, text, user_id)

    # -- count trigger (hot path) -------------------------------------------
    def on_media(self, topic_id: int, context: Any, group_id: str | None = None) -> None:
        """Record one media *message* in ``topic_id`` and arm the count trigger.

        Called from the sorting hot path for every user media message observed
        in a source topic. An album (media group) arrives as several messages
        sharing ``group_id``; those collapse to a single count so the threshold
        is in whole posts, not individual photos.

        Once the threshold is reached, the post is debounced: the timer resets on
        every new media (album members included) and only fires after a quiet gap
        (``post_delay_seconds``), so the reminder never cuts into a batch that is
        still being sent. Cheap and synchronous.
        """

        if not self._live.notice_enabled():
            return
        if topic_id not in self._live.notice_topics():
            return
        state = self._state.get(topic_id)
        if state is None:
            state = _TopicState(last_post_at=self._clock())
            self._state[topic_id] = state

        # Album members after the first do not add to the count, but they still
        # push back the debounce below so the post waits for the album to finish.
        if group_id is None or not self._already_counted_group(state, group_id):
            state.count += 1

        threshold = self._live.notice_media_threshold()
        if threshold <= 0 or state.count < threshold:
            return
        # Threshold reached: (re)arm a debounce that fires only after the *last*
        # observed media, so a still-arriving batch/album is never interrupted.
        if state.post_task is not None and not state.post_task.done():
            state.post_task.cancel()
        state.post_task = asyncio.ensure_future(self._post_after_delay(topic_id, context))

    def _already_counted_group(self, state: _TopicState, group_id: str) -> bool:
        """True if this album was seen recently; records it for next time.

        Prunes entries older than the dedup window and caps the map size so a
        long-lived, busy topic cannot accumulate group ids without bound.
        """

        now = self._clock()
        recent = state.recent_groups
        if recent:
            stale = [g for g, seen in recent.items() if now - seen > _GROUP_DEDUP_WINDOW_SECONDS]
            for g in stale:
                del recent[g]
        seen_before = group_id in recent
        recent[group_id] = now
        if len(recent) > _GROUP_DEDUP_MAX:
            # Drop the oldest entries down to the cap.
            for g in sorted(recent, key=recent.__getitem__)[: len(recent) - _GROUP_DEDUP_MAX]:
                del recent[g]
        return seen_before

    async def _post_after_delay(self, topic_id: int, context: Any) -> None:
        try:
            await asyncio.sleep(self._post_delay)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            return
        await self._post(topic_id, context)

    # -- interval trigger (JobQueue tick) -----------------------------------
    async def tick(self, context: Any) -> None:
        """Post in any configured topic whose interval has elapsed with activity.

        Invoked by a lightweight repeating job; returns immediately when the
        feature is off or nothing is due.
        """

        if not self._live.notice_enabled():
            return
        interval_minutes = self._live.notice_interval_minutes()
        if interval_minutes <= 0:
            return
        topics = self._live.notice_topics()
        due = interval_minutes * 60
        now = self._clock()
        for topic_id in topics:
            state = self._state.get(topic_id)
            if state is None or state.count <= 0:
                continue
            if now - state.last_post_at >= due:
                await self._post(topic_id, context)

    # -- shared post (delete previous, send new, persist) -------------------
    async def _post(self, topic_id: int, context: Any) -> None:
        state = self._state.get(topic_id)
        if state is None:
            return
        async with state.lock:
            # Re-check under the lock: the other trigger may have just posted.
            if state.count <= 0:
                return
            text = self.get_text().strip()
            if not text:
                # Nothing to say yet; keep counting so it fires once configured.
                return
            chat_id = self._settings.source_chat_id
            await self._delete_previous(topic_id, chat_id, context)
            try:
                sent = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    message_thread_id=topic_id,
                )
            except Exception as error:  # noqa: BLE001 - notices are best-effort
                LOGGER.warning(
                    "Failed to post periodic notice",
                    extra={"topic_id": topic_id, "error": str(error)},
                )
                return
            self._repositories.set_runtime_setting(_self_last_key(topic_id), str(sent.message_id))
            state.count = 0
            state.last_post_at = self._clock()
            LOGGER.info("Posted periodic notice", extra={"topic_id": topic_id})

    async def _delete_previous(self, topic_id: int, chat_id: int, context: Any) -> None:
        key = _self_last_key(topic_id)
        previous = self._repositories.get_runtime_setting(key)
        if not previous:
            return
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=int(previous))
        except Exception as error:  # noqa: BLE001 - stale/removed message is fine
            LOGGER.info(
                "Could not delete previous periodic notice (continuing)",
                extra={"topic_id": topic_id, "message_id": previous, "error": str(error)},
            )
        finally:
            # Drop the stale pointer either way so we never retry a dead id.
            self._repositories.delete_runtime_setting(key)

    # -- lifecycle ----------------------------------------------------------
    def cancel_timers(self) -> None:
        """Cancel any pending debounce tasks (shutdown)."""

        for state in self._state.values():
            task = state.post_task
            if task is not None and not task.done():
                task.cancel()


def _self_last_key(topic_id: int) -> str:
    return f"{_LAST_MESSAGE_KEY}:{topic_id}"


def make_tick_job(service: PeriodicNoticeService) -> Callable[[Any], Any]:
    """Wrap :meth:`tick` with a correlation id for the JobQueue."""

    async def run(context: Any) -> None:
        token = set_correlation_id("periodic-notice")
        try:
            await service.tick(context)
        finally:
            reset_correlation_id(token)

    return run
