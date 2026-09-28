from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict, deque
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Any

from telegram import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from miki_sorter_bot.config import Settings, TopicForwardingPair
from miki_sorter_bot.indexing import (
    HASHTAG_RE,
    IndexingService,
    contains_keyword,
    contains_phrase,
    media_type,
    media_unique_id,
)
from miki_sorter_bot.lookback import CapturedMedia, RecentMediaBuffer
from miki_sorter_bot.periodic_notice import PeriodicNoticeService
from miki_sorter_bot.topic_activity import TopicActivity
from miki_sorter_bot.repositories import (
    RouteMappingRecord,
    SqliteRepositories,
    TopicRecord,
)
from miki_sorter_bot.reliability import DeliveryExecutor, RateLimiter, RetryPolicy, classify_error
from miki_sorter_bot.settings_registry import LiveSettings

LOGGER = logging.getLogger(__name__)
ALBUM_VISUAL_MEDIA_TYPES = {"photo", "video"}
ALBUM_HOMOGENEOUS_MEDIA_TYPES = {"audio", "document"}
# How many album uploads may be in flight at once across all pending albums.
_MAX_CONCURRENT_ALBUM_SENDS = 1


@dataclass(frozen=True, slots=True)
class RouteMatch:
    topic: TopicRecord
    mapping: RouteMappingRecord


@dataclass(frozen=True, slots=True)
class SortDecision:
    status: str
    topic: TopicRecord | None
    matches: tuple[RouteMatch, ...]
    reason: str


@dataclass(slots=True)
class PendingAlbum:
    source_chat_id: int
    decision: SortDecision | None
    messages: OrderedDict[int, object]
    first_seen_at: float


@dataclass(frozen=True, slots=True)
class RecoveredMessage:
    message_id: int
    from_user: None = None


class AlbumDeliveryOutcome(Enum):
    DELIVERED = "delivered"
    SAFE_FALLBACK = "safe_fallback"
    OUTCOME_UNKNOWN = "outcome_unknown"


class RouteMatcher:
    def __init__(self, repositories: SqliteRepositories, archive_chat_id: int) -> None:
        self._repositories = repositories
        self._archive_chat_id = archive_chat_id

    def decide(self, text: str) -> SortDecision:
        mappings = self._repositories.list_mappings(self._archive_chat_id)
        topics = {
            topic.id: topic for topic in self._repositories.list_topics(self._archive_chat_id)
        }
        hashtags = {match.group(1).casefold() for match in HASHTAG_RE.finditer(text)}

        hashtag_matches = _matching_routes(mappings, topics, "hashtag", hashtags)
        candidates = hashtag_matches or _matching_non_hashtags(
            mappings,
            topics,
            text,
        )
        if not candidates:
            return SortDecision("unmatched", None, (), "no configured route matched")
        topic_ids = {match.topic.id for match in candidates}
        if len(topic_ids) > 1:
            return SortDecision(
                "conflict", None, tuple(candidates), "multiple destinations matched"
            )
        topic = candidates[0].topic
        reasons = ",".join(
            f"{match.mapping.kind}:{match.mapping.normalized_value}" for match in candidates
        )
        return SortDecision("matched", topic, tuple(candidates), reasons)


class SortingService:
    def __init__(
        self,
        settings: Settings,
        repositories: SqliteRepositories,
        indexing: IndexingService,
        delivery_executor: DeliveryExecutor | None = None,
        live_settings: LiveSettings | None = None,
        notice: "PeriodicNoticeService | None" = None,
        activity: "TopicActivity | None" = None,
    ) -> None:
        self._settings = settings
        self._repositories = repositories
        self._indexing = indexing
        self._notice = notice
        self._activity = activity
        self._delivery_executor = delivery_executor or DeliveryExecutor(
            retry_policy=RetryPolicy(),
            rate_limiter=RateLimiter(1000),
        )
        # Effective (chat-configurable) values are resolved live on each read.
        self._live = live_settings or LiveSettings(settings, repositories)
        self._matcher = RouteMatcher(repositories, settings.archive_chat_id)
        self._album_decisions: OrderedDict[tuple[int, str], SortDecision] = OrderedDict()
        self._backed_up: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._album_source_threads: OrderedDict[tuple[int, str], int] = OrderedDict()
        self._pending_albums: dict[tuple[int, str], PendingAlbum] = {}
        self._album_flush_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        # Keys whose flush task has passed its debounce sleep and is mid-delivery.
        # A straggling album member must not cancel an in-flight send, or those
        # members are lost (webhook delivers album members as separate, staggered
        # POSTs, so stragglers past the flush delay are common; polling batches
        # them so the race never triggers).
        self._delivering_albums: set[tuple[int, str]] = set()
        # One album upload at a time. A restart replays the whole polling backlog
        # in a single getUpdates, so every buffered album's debounce expires in
        # the same second; firing N concurrent send_media_group uploads saturated
        # the link and returned them all as outcome_unknown, which strands the
        # members permanently (a failed job is not retried by the pending sweep).
        # A Telegram album is at most 10 items, so serialising costs little.
        self._album_send_gate = asyncio.Semaphore(_MAX_CONCURRENT_ALBUM_SENDS)
        # Short-lived memory of uncaptioned media so a following hashtag-only
        # message can route "the post before it" (look-back feature). Media that
        # expires unclaimed is collected here and later filed under the default
        # topic by the periodic ``sweep_lookback`` pass.
        self._expired_lookback: deque[tuple[tuple[int, int | None], CapturedMedia]] = deque()
        self._lookback = RecentMediaBuffer(
            ttl=self._live.lookback_ttl,
            capacity=self._live.lookback_capacity,
            on_expire=self._on_lookback_expire,
        )
        self._suspend_album_reschedule = False
        # Reconcile env-configured forwarding pairs into the database once, so
        # the DB is the live source of truth and pairs become manageable via
        # Telegram without a restart.
        self._repositories.seed_forwarding_pairs(getattr(settings, "topic_forwarding_pairs", ()))

    def _forwarding_pair(
        self,
        source_chat_id: int,
        source_thread_id: int | None,
    ) -> TopicForwardingPair | None:
        if source_chat_id != self._settings.source_chat_id or source_thread_id is None:
            return None
        destination = self._repositories.get_forwarding_destination(source_thread_id)
        if destination is None:
            return None
        return TopicForwardingPair(
            source_thread_id=source_thread_id,
            destination_thread_id=destination,
        )

    def _direct_decision(self, pair: TopicForwardingPair) -> SortDecision | None:
        thread_id = pair.destination_thread_id
        topic = self._repositories.get(self._settings.archive_chat_id, thread_id)
        if topic is None or not topic.is_active:
            LOGGER.error(
                "Configured direct destination topic is not registered or active",
                extra={
                    "archive_chat_id": self._settings.archive_chat_id,
                    "destination_thread_id": thread_id,
                },
            )
            return None
        return SortDecision(
            "matched",
            topic,
            (),
            f"forwarding-pair:{pair.source_thread_id}->{pair.destination_thread_id}",
        )

    def _default_topic(self) -> TopicRecord | None:
        """The catch-all topic for posts that belong to no topic (0 = disabled)."""

        thread_id = self._live.default_topic_id()
        if not thread_id:
            return None
        topic = self._repositories.get(self._settings.archive_chat_id, thread_id)
        if topic is None or not topic.is_active:
            LOGGER.error(
                "Configured default topic is not registered or active",
                extra={
                    "archive_chat_id": self._settings.archive_chat_id,
                    "default_topic_id": thread_id,
                },
            )
            return None
        return topic

    def _default_decision(self, reason: str) -> SortDecision | None:
        topic = self._default_topic()
        if topic is None:
            return None
        return SortDecision("matched", topic, (), reason)

    @staticmethod
    def _conflicting_hashtags(decision: SortDecision) -> list[str]:
        """Distinct hashtag values whose routes disagreed, in first-seen order."""

        tags: list[str] = []
        for match in decision.matches:
            if match.mapping.kind == "hashtag":
                value = match.mapping.normalized_value
                if value not in tags:
                    tags.append(value)
        return tags

    async def _reply_double_tag(self, message: Any, decision: SortDecision) -> None:
        """Tell the poster a post carried more than one topic hashtag.

        Best-effort: a reply failure must never derail routing, so it only logs
        and counts a metric. Only fires for a genuine multi-hashtag conflict; a
        keyword/keyword conflict has no user-facing tag advice to give.
        """

        tags = self._conflicting_hashtags(decision)
        if len(tags) < 2:
            return
        reply = getattr(message, "reply_text", None)
        if reply is None:
            return
        tag_list = ", ".join(f"#{tag}" for tag in tags)
        topic = self._default_topic()
        suffix = f" For now it has been filed under {topic.name}." if topic is not None else ""
        try:
            await reply(
                "This post is tagged with more than one topic hashtag "
                f"({tag_list}). Please tag it with only one of them so it lands "
                f"in the right topic.{suffix}",
                do_quote=True,
            )
        except Exception as error:
            self._repositories.increment_metric("double_tag_reply_failures", 1)
            LOGGER.warning(
                "Could not send double-tag notice",
                extra={
                    "message_id": getattr(message, "message_id", None),
                    "error": str(error),
                },
            )

    def _on_lookback_expire(
        self,
        key: tuple[int, int | None],
        captured: CapturedMedia,
    ) -> None:
        """Collect look-back media evicted by TTL for later default-topic filing.

        Runs synchronously inside the buffer's prune; delivery (which needs the
        bot) is deferred to ``sweep_lookback``.
        """

        self._expired_lookback.append((key, captured))

    async def sweep_lookback(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """File look-back media that expired unclaimed under the default topic.

        Forces a TTL sweep so idle buckets expire even without new traffic, then
        drains everything the buffer handed off. When no default topic is
        configured, the queue is drained and dropped (the prior behaviour).
        """

        self._lookback.sweep()
        if not self._expired_lookback:
            return
        topic = self._default_topic()
        while self._expired_lookback:
            key, captured = self._expired_lookback.popleft()
            if topic is None:
                continue
            decision = SortDecision(
                "matched",
                topic,
                (),
                f"lookback-expired-default:{topic.thread_id}",
            )
            try:
                await self._deliver_album_messages(
                    captured.messages,
                    key[0],
                    decision,
                    captured.media_group_id,
                    context,
                )
                self._repositories.increment_metric("lookback_expired_default_deliveries", 1)
            except Exception as error:
                failure = classify_error(error)
                LOGGER.warning(
                    "Failed filing expired look-back media under the default topic",
                    extra={
                        "source_chat_id": key[0],
                        "media_group_id": captured.media_group_id,
                        "error_category": failure.category,
                    },
                )

    async def handle_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        if chat.type not in {ChatType.SUPERGROUP, ChatType.GROUP}:
            return
        detected_media_type = media_type(message)
        if detected_media_type is None:
            # Not media. It may be a hashtag-only message tagging media that was
            # posted just before it without a caption (look-back feature).
            await self._maybe_route_from_lookback(message, chat, context)
            return
        media_group_id = getattr(message, "media_group_id", None)
        album_key = (chat.id, media_group_id) if media_group_id else None
        reported_thread_id = getattr(message, "message_thread_id", None)
        source_thread_id = reported_thread_id
        if album_key is not None:
            if reported_thread_id is not None:
                self._remember_album_source_thread(album_key, reported_thread_id)
            else:
                source_thread_id = self._album_source_threads.get(album_key)

        if chat.id == self._settings.source_chat_id:
            LOGGER.info(
                "Observed source media update",
                extra={
                    "update_id": getattr(update, "update_id", None),
                    "message_id": getattr(message, "message_id", None),
                    "reported_thread_id": reported_thread_id,
                    "effective_thread_id": source_thread_id,
                    "media_group_id": media_group_id,
                    "media_type": detected_media_type,
                },
            )

        # Count user media for the periodic-notice feature before the routing
        # gate below, so notices can target any source topic (not only the one
        # Miki sorts). Skip edits so a re-edited post is not double-counted.
        if (
            (self._notice is not None or self._activity is not None)
            and getattr(update, "edited_message", None) is None
            and chat.id == self._settings.source_chat_id
            and source_thread_id is not None
        ):
            sender = getattr(message, "from_user", None)
            if getattr(sender, "id", None) != context.bot.id:
                # An album's members share media_group_id and count once.
                group_id = str(media_group_id) if media_group_id else None
                counted = (
                    self._activity.record(source_thread_id, group_id)
                    if self._activity is not None
                    else None
                )
                if self._notice is not None:
                    self._notice.on_media(
                        source_thread_id, context, group_id=group_id, counted=counted
                    )

        forwarding_pair = self._forwarding_pair(chat.id, source_thread_id)
        is_primary_source = (
            chat.id == self._settings.source_chat_id
            and source_thread_id == self._live.effective_source_thread_id()
        )
        if forwarding_pair is None and not is_primary_source:
            return
        sender = getattr(message, "from_user", None)
        if getattr(sender, "id", None) == context.bot.id:
            return
        text = (message.caption or message.text or "").strip()
        direct_decision = (
            self._direct_decision(forwarding_pair) if forwarding_pair is not None else None
        )
        if forwarding_pair is not None and direct_decision is None:
            return
        if album_key is not None:
            decision = direct_decision or (
                self._matcher.decide(text) if text else self._album_decisions.get(album_key)
            )
            if decision is not None:
                if decision.status == "unmatched":
                    decision = None
                elif decision.status == "conflict":
                    self._record_skip(message, decision)
                    LOGGER.warning(
                        "Sorting conflict",
                        extra={"chat_id": chat.id, "message_id": message.message_id},
                    )
                    await self._reply_double_tag(message, decision)
                    fallback = self._default_decision(f"conflict-default:{decision.reason}")
                    if fallback is None:
                        return
                    decision = fallback
                    self._remember_album_decision(album_key, decision)
                else:
                    self._remember_album_decision(album_key, decision)
            self._queue_album_message(album_key, message, chat.id, decision, context)
            return
        if direct_decision is not None:
            await self._backup_to_second_group((message,), context)
            await self._deliver(message, chat.id, direct_decision, context)
            return
        if not text:
            # Uncaptioned media in the source topic: remember it briefly so a
            # following hashtag-only message can still route it.
            if self._live.lookback_enabled():
                self._lookback.capture(chat.id, source_thread_id, (message,))
            return
        decision = self._matcher.decide(text)
        if decision.status == "unmatched":
            # Media whose caption routes nowhere (e.g. forwarded media still
            # carrying its origin's unrelated caption): remember it like
            # uncaptioned media so a following hashtag-only message can claim it.
            if self._live.lookback_enabled():
                self._lookback.capture(chat.id, source_thread_id, (message,))
            return
        if decision.status == "conflict":
            self._record_skip(message, decision)
            LOGGER.warning(
                "Sorting conflict",
                extra={"chat_id": chat.id, "message_id": message.message_id},
            )
            await self._reply_double_tag(message, decision)
            fallback = self._default_decision(f"conflict-default:{decision.reason}")
            if fallback is not None:
                await self._backup_to_second_group((message,), context)
                await self._deliver(message, chat.id, fallback, context)
            return
        await self._backup_to_second_group((message,), context)
        await self._deliver(message, chat.id, decision, context)

    async def _maybe_route_from_lookback(
        self,
        message: Any,
        chat: Any,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """A hashtag-only message can route the uncaptioned media just before it."""

        if not self._live.lookback_enabled():
            return
        text = (getattr(message, "text", None) or "").strip()
        if not text:
            return
        source_thread_id = getattr(message, "message_thread_id", None)
        # Look-back mirrors the primary sort source only (forwarding pairs deliver
        # uncaptioned media immediately, so nothing is ever buffered for them).
        is_primary_source = (
            chat.id == self._settings.source_chat_id
            and source_thread_id == self._live.effective_source_thread_id()
        )
        if not is_primary_source:
            return
        sender = getattr(message, "from_user", None)
        if getattr(sender, "id", None) == context.bot.id:
            return
        decision = self._matcher.decide(text)
        if decision.status == "conflict":
            # The follow-up tag itself carries two conflicting topic hashtags:
            # warn the poster and file the buffered media under the default topic,
            # mirroring the captioned-media conflict path.
            await self._route_lookback_conflict(message, chat, source_thread_id, decision, context)
            return
        if decision.status != "matched":
            return
        # 1) Quick tag: an album posted seconds ago is still assembling/awaiting a
        #    route. Hand it this decision and let its existing flush deliver it.
        if self._attach_decision_to_pending_album(chat.id, source_thread_id, decision):
            return
        # 2) Otherwise claim the most recent buffered media and deliver it now.
        captured = self._lookback.claim_latest(chat.id, source_thread_id)
        if captured is None:
            return
        LOGGER.info(
            "Routing look-back media from a following hashtag",
            extra={
                "source_chat_id": chat.id,
                "message_count": len(captured.messages),
                "media_group_id": captured.media_group_id,
                "destination_thread_id": decision.topic.thread_id
                if decision.topic is not None
                else None,
            },
        )
        await self._deliver_captured(captured, chat.id, decision, context)

    async def _route_lookback_conflict(
        self,
        message: Any,
        chat: Any,
        source_thread_id: int | None,
        decision: SortDecision,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle a look-back trigger whose own tags disagree.

        Warns the poster (for a genuine multi-hashtag clash) and files the media
        it would have claimed under the default topic. When no default topic is
        configured, the media is left buffered to expire, as elsewhere.
        """

        await self._reply_double_tag(message, decision)
        fallback = self._default_decision(f"conflict-default:{decision.reason}")
        if fallback is None:
            return
        # An album still assembling: hand it the default decision so its flush
        # delivers it (nothing is in the look-back buffer yet).
        if self._attach_decision_to_pending_album(chat.id, source_thread_id, fallback):
            self._repositories.increment_metric("sort_conflicts", 1)
            return
        captured = self._lookback.claim_latest(chat.id, source_thread_id)
        if captured is None:
            return
        for buffered in captured.messages:
            self._record_skip(buffered, decision)
        await self._deliver_captured(captured, chat.id, fallback, context)

    def _attach_decision_to_pending_album(
        self,
        chat_id: int,
        thread_id: int | None,
        decision: SortDecision,
    ) -> bool:
        candidates = [
            (pending.first_seen_at, key)
            for key, pending in self._pending_albums.items()
            if key[0] == chat_id
            and pending.decision is None
            and self._album_source_threads.get(key) == thread_id
        ]
        if not candidates:
            return False
        candidates.sort()
        _, key = candidates[-1]  # most recent undecided album in this topic
        self._pending_albums[key].decision = decision
        self._remember_album_decision(key, decision)
        return True

    async def _deliver_captured(
        self,
        captured: CapturedMedia,
        source_chat_id: int,
        decision: SortDecision,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        await self._deliver_album_messages(
            captured.messages,
            source_chat_id,
            decision,
            captured.media_group_id,
            context,
        )

    def _remember_album_decision(
        self,
        key: tuple[int, str],
        decision: SortDecision,
    ) -> None:
        self._album_decisions[key] = decision
        self._album_decisions.move_to_end(key)
        while len(self._album_decisions) > 1000:
            self._album_decisions.popitem(last=False)

    def _remember_album_source_thread(
        self,
        key: tuple[int, str],
        thread_id: int,
    ) -> None:
        self._album_source_threads[key] = thread_id
        self._album_source_threads.move_to_end(key)
        while len(self._album_source_threads) > 1000:
            self._album_source_threads.popitem(last=False)

    def _queue_album_message(
        self,
        key: tuple[int, str],
        message: Any,
        source_chat_id: int,
        decision: SortDecision | None,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        pending = self._pending_albums.get(key)
        if pending is None:
            pending = PendingAlbum(source_chat_id, decision, OrderedDict(), time.monotonic())
            self._pending_albums[key] = pending
        elif decision is not None:
            pending.decision = decision
        pending.messages[message.message_id] = message
        existing_task = self._album_flush_tasks.get(key)
        if (
            existing_task is not None
            and not existing_task.done()
            and key not in self._delivering_albums
        ):
            # Only reset the debounce while the prior task is still waiting. If it
            # is already delivering, cancelling it would abort the in-flight send
            # and drop those members; let it finish and route this straggler in a
            # fresh flush instead.
            existing_task.cancel()
        self._album_flush_tasks[key] = asyncio.create_task(
            self._flush_album_after_delay(key, context)
        )

    async def _flush_album_after_delay(
        self,
        key: tuple[int, str],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        try:
            await asyncio.sleep(self._live.album_flush_delay())
            # No await between here and the delivery's first await, so marking the
            # key now reliably protects the in-flight send from a straggler cancel.
            self._delivering_albums.add(key)
            await self._deliver_album_background(key, context)
        finally:
            self._delivering_albums.discard(key)
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if current_task is not None and self._album_flush_tasks.get(key) is current_task:
                self._album_flush_tasks.pop(key, None)
                if key in self._pending_albums and not self._suspend_album_reschedule:
                    self._album_flush_tasks[key] = asyncio.create_task(
                        self._flush_album_after_delay(key, context)
                    )

    async def flush_pending_albums(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._suspend_album_reschedule = True
        try:
            tasks = tuple(self._album_flush_tasks.values())
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            self._album_flush_tasks.clear()
            keys = tuple(self._pending_albums)
            for key in keys:
                await self._deliver_album(key, context)
        finally:
            self._suspend_album_reschedule = False

    async def shutdown(self, context: Any) -> None:
        """Drain routable albums and cancel every timer before storage closes."""

        await self.flush_pending_albums(context)
        if self._notice is not None:
            self._notice.cancel_timers()
        dropped = len(self._pending_albums)
        self._pending_albums.clear()
        if dropped:
            LOGGER.info("Discarded unrouted albums during shutdown", extra={"count": dropped})

    async def _deliver_album(
        self,
        key: tuple[int, str],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        pending = self._pending_albums.pop(key, None)
        if pending is None:
            return
        if pending.decision is None:
            pending.decision = self._decide_album_text(pending)
        if pending.decision is not None and pending.decision.status == "conflict":
            await self._handle_album_flush_conflict(key, pending, context)
            return
        if pending.decision is not None and pending.decision.status == "unmatched":
            pending.decision = None
        if pending.decision is None:
            # A late or uncaptioned member can land in its own pending album when
            # the captioned sibling already flushed (webhook delivers members as
            # separate, staggered POSTs). Inherit the decision remembered for this
            # media_group so the straggler is routed instead of dropped.
            remembered = self._album_decisions.get(key)
            if remembered is not None:
                pending.decision = remembered
        if pending.decision is None:
            if time.monotonic() - pending.first_seen_at < self._live.album_max_wait():
                self._pending_albums[key] = pending
                LOGGER.info(
                    "Album is waiting for a route decision",
                    extra={
                        "source_chat_id": pending.source_chat_id,
                        "media_group_id": key[1],
                        "message_count": len(pending.messages),
                        "caption_count": _album_caption_count(tuple(pending.messages.values())),
                    },
                )
                return
            self._buffer_unrouted_album(key, pending)
            return
        messages = tuple(
            message for _, message in sorted(pending.messages.items(), key=lambda item: item[0])
        )
        LOGGER.info(
            "Delivering album",
            extra={
                "source_chat_id": pending.source_chat_id,
                "media_group_id": key[1],
                "message_count": len(messages),
                "destination_thread_id": pending.decision.topic.thread_id
                if pending.decision.topic is not None
                else None,
            },
        )
        await self._deliver_album_messages(
            messages,
            pending.source_chat_id,
            pending.decision,
            key[1],
            context,
        )

    def _buffer_unrouted_album(self, key: tuple[int, str], pending: PendingAlbum) -> None:
        """An album nobody routed within the wait window: hand it to look-back
        (so a later hashtag can still claim it) instead of dropping it."""

        messages = tuple(
            message for _, message in sorted(pending.messages.items(), key=lambda item: item[0])
        )
        if self._live.lookback_enabled():
            self._lookback.capture(
                pending.source_chat_id,
                self._album_source_threads.get(key),
                messages,
                media_group_id=key[1],
            )
            LOGGER.info(
                "Buffered unrouted album for hashtag look-back",
                extra={
                    "source_chat_id": pending.source_chat_id,
                    "media_group_id": key[1],
                    "message_count": len(messages),
                },
            )
            return
        LOGGER.info(
            "Dropping unrouted album after decision wait expired",
            extra={
                "source_chat_id": pending.source_chat_id,
                "media_group_id": key[1],
                "message_count": len(messages),
            },
        )

    async def _deliver_album_messages(
        self,
        messages: tuple[Any, ...],
        source_chat_id: int,
        decision: SortDecision,
        media_group_id: str | None,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Deliver an assembled, decided album as a group, with a per-member
        fallback. Shared by the album flush path and look-back delivery."""

        filtered_messages: list[Any] = []
        seen_unique_ids: set[str] = set()
        for message in messages:
            detected_media = media_type(message)
            file_unique_id = media_unique_id(message, detected_media) if detected_media else None
            if file_unique_id and (
                file_unique_id in seen_unique_ids
                or self._repositories.has_duplicate_file(file_unique_id)
            ):
                self._repositories.increment_metric("sort_duplicates", 1)
            else:
                if file_unique_id:
                    seen_unique_ids.add(file_unique_id)
                filtered_messages.append(message)

        messages = tuple(filtered_messages)
        if not messages:
            return

        # The gate spans the fallback loop too: an album that falls back to
        # per-member copies is exactly the case that must not compete for
        # bandwidth with the next album's grouped upload.
        async with self._album_send_gate:
            await self._backup_to_second_group(messages, context)
            if len(messages) == 1:
                await self._deliver(messages[0], source_chat_id, decision, context)
                return
            group_outcome = await self._deliver_media_group(
                messages,
                source_chat_id,
                decision,
                context,
            )
            if group_outcome is not AlbumDeliveryOutcome.SAFE_FALLBACK:
                return
            failed_count = 0
            for message in messages:
                try:
                    await self._deliver(message, source_chat_id, decision, context)
                except Exception as error:
                    failed_count += 1
                    failure = classify_error(error)
                    LOGGER.warning(
                        "Individual album member delivery failed; continuing album",
                        extra={
                            "source_chat_id": source_chat_id,
                            "source_message_id": getattr(message, "message_id", None),
                            "media_group_id": media_group_id,
                            "error_category": failure.category,
                        },
                    )
        if failed_count:
            self._repositories.increment_metric(
                "album_member_delivery_failures",
                failed_count,
            )
            LOGGER.warning(
                "Album fallback completed with failed members",
                extra={
                    "source_chat_id": source_chat_id,
                    "media_group_id": media_group_id,
                    "failed_count": failed_count,
                    "message_count": len(messages),
                },
            )

    async def _deliver_album_background(
        self,
        key: tuple[int, str],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        try:
            await self._deliver_album(key, context)
        except Exception as error:
            failure = classify_error(error)
            self._repositories.increment_metric("album_flush_failures", 1)
            LOGGER.warning(
                "Album flush failed",
                extra={
                    "source_chat_id": key[0],
                    "media_group_id": key[1],
                    "error_category": failure.category,
                },
            )

    async def _deliver(
        self,
        message: Any,
        source_chat_id: int,
        decision: SortDecision,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        detected_media = media_type(message)
        file_unique_id = media_unique_id(message, detected_media) if detected_media else None
        if file_unique_id and self._repositories.has_duplicate_file(file_unique_id):
            self._repositories.increment_metric("sort_duplicates", 1)
            return

        topic = decision.topic
        if topic is None:
            raise ValueError("matched sort decision requires a destination topic")
        key = (
            f"sort:{source_chat_id}:{message.message_id}:"
            f"{self._settings.archive_chat_id}:{topic.thread_id}"
        )
        job = self._repositories.enqueue(
            "sort",
            key,
            {
                "source_chat_id": source_chat_id,
                "source_message_id": message.message_id,
                "destination_chat_id": self._settings.archive_chat_id,
                "destination_thread_id": topic.thread_id,
                "reason": decision.reason,
            },
        )
        delivery = self._repositories.ensure_delivery(
            job.id,
            source_chat_id=source_chat_id,
            source_message_id=message.message_id,
            destination_chat_id=self._settings.archive_chat_id,
            destination_thread_id=topic.thread_id,
            reason=decision.reason,
        )
        if delivery.status in {"sent", "skipped"}:
            self._repositories.increment_metric("sort_duplicates", 1)
            return
        if not self._repositories.claim_job(job.id):
            self._repositories.increment_metric("sort_duplicates", 1)
            return
        if self._live.sort_dry_run():
            self._repositories.update_delivery(delivery.id, "skipped", reason="dry-run")
            self._repositories.update_job(job.id, "completed")
            return
        try:
            copied = await self._delivery_executor.run(
                lambda: context.bot.copy_message(
                    chat_id=self._settings.archive_chat_id,
                    from_chat_id=source_chat_id,
                    message_id=message.message_id,
                    message_thread_id=topic.thread_id,
                ),
                retry_unknown_outcome=False,
            )
        except Exception as error:
            failure = classify_error(error)
            reason = (
                "delivery outcome unknown after timeout" if failure.outcome_unknown else str(error)
            )
            category = "outcome_unknown" if failure.outcome_unknown else failure.category
            self._repositories.update_delivery(delivery.id, "failed", reason=reason)
            self._repositories.update_job(job.id, "failed", error=reason)
            self._repositories.add_dead_letter(
                job.id,
                "sort_copy",
                job.payload,
                category,
                str(error),
            )
            if failure.outcome_unknown:
                self._repositories.increment_metric("telegram_delivery_outcome_unknown", 1)
            self._audit(message, "sort.copy", "failed", str(job.id), category)
            raise
        self._repositories.update_delivery(
            delivery.id,
            "sent",
            destination_message_id=copied.message_id,
        )
        self._repositories.update_job(job.id, "completed")
        self._repositories.increment_metric("sort_deliveries", 1)
        self._audit(message, "sort.copy", "success", str(job.id))
        self._indexing.index_copy(
            message,
            bot_id=context.bot.id,
            destination_chat_id=self._settings.archive_chat_id,
            destination_thread_id=topic.thread_id,
            destination_message_id=copied.message_id,
        )
        if self._live.send_confirmation():
            await self._confirm_delivery(message, topic)

    async def _confirm_delivery(self, message: Any, topic: TopicRecord) -> None:
        """Best-effort "Sorted to X." reply after a committed delivery.

        The job is already completed by the time this runs, so a confirmation
        failure (recovered job replaying without the original Telegram message,
        deleted source message, missing permissions) must never bubble up and
        mark the finished delivery as failed.
        """

        reply = getattr(message, "reply_text", None)
        if reply is None:
            return
        try:
            await reply(f"Sorted to {topic.name}.", do_quote=True)
        except Exception as error:
            self._repositories.increment_metric("sort_confirmation_failures", 1)
            LOGGER.warning(
                "Could not send sort confirmation (delivery already committed)",
                extra={
                    "message_id": getattr(message, "message_id", None),
                    "destination_thread_id": topic.thread_id,
                    "error": str(error),
                },
            )

    def _decide_album_text(self, pending: PendingAlbum) -> SortDecision | None:
        text = _album_text(tuple(pending.messages.values()))
        if not text:
            return None
        # Return the raw decision; ``_deliver_album`` interprets conflict
        # (file under the default topic) and unmatched (keep waiting / look-back).
        return self._matcher.decide(text)

    async def _handle_album_flush_conflict(
        self,
        key: tuple[int, str],
        pending: PendingAlbum,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        decision = pending.decision
        assert decision is not None
        messages = tuple(
            message for _, message in sorted(pending.messages.items(), key=lambda item: item[0])
        )
        for message in messages:
            self._record_skip(message, decision)
        LOGGER.warning(
            "Album sorting conflict",
            extra={
                "source_chat_id": pending.source_chat_id,
                "message_count": len(messages),
            },
        )
        if messages:
            await self._reply_double_tag(messages[0], decision)
        fallback = self._default_decision(f"conflict-default:{decision.reason}")
        if fallback is None:
            return
        await self._deliver_album_messages(
            messages,
            pending.source_chat_id,
            fallback,
            key[1],
            context,
        )

    async def _deliver_media_group(
        self,
        messages: tuple[Any, ...],
        source_chat_id: int,
        decision: SortDecision,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> AlbumDeliveryOutcome:
        topic = decision.topic
        if topic is None:
            raise ValueError("matched album decision requires a destination topic")
        media = _media_group_payload(messages)
        if media is None:
            return AlbumDeliveryOutcome.SAFE_FALLBACK

        completed_count = 0
        deliveries = []
        for message in messages:
            key = (
                f"sort:{source_chat_id}:{message.message_id}:"
                f"{self._settings.archive_chat_id}:{topic.thread_id}"
            )
            job = self._repositories.enqueue(
                "sort",
                key,
                {
                    "source_chat_id": source_chat_id,
                    "source_message_id": message.message_id,
                    "destination_chat_id": self._settings.archive_chat_id,
                    "destination_thread_id": topic.thread_id,
                    "reason": decision.reason,
                    "delivery_method": "send_media_group",
                },
            )
            delivery = self._repositories.ensure_delivery(
                job.id,
                source_chat_id=source_chat_id,
                source_message_id=message.message_id,
                destination_chat_id=self._settings.archive_chat_id,
                destination_thread_id=topic.thread_id,
                reason=decision.reason,
            )
            if delivery.status in {"sent", "skipped"}:
                completed_count += 1
                continue
            if not self._repositories.claim_job(job.id):
                completed_count += 1
                continue
            if self._live.sort_dry_run():
                self._repositories.update_delivery(delivery.id, "skipped", reason="dry-run")
                self._repositories.update_job(job.id, "completed")
                continue
            deliveries.append((message, job, delivery))
        if deliveries and completed_count:
            for _, job, _ in deliveries:
                self._repositories.update_job(
                    job.id,
                    "failed",
                    error="album requires individual delivery",
                )
            self._repositories.increment_metric("media_group_fallbacks", 1)
            LOGGER.info(
                "Album delivery has prior completed members; falling back to individual copies",
                extra={
                    "source_chat_id": source_chat_id,
                    "media_group_id": getattr(messages[0], "media_group_id", None),
                    "pending_count": len(deliveries),
                    "album_count": len(messages),
                },
            )
            return AlbumDeliveryOutcome.SAFE_FALLBACK
        if completed_count:
            self._repositories.increment_metric("sort_duplicates", completed_count)
        if not deliveries or self._live.sort_dry_run():
            return AlbumDeliveryOutcome.DELIVERED

        try:
            sent_messages = await self._delivery_executor.run(
                lambda: context.bot.send_media_group(
                    chat_id=self._settings.archive_chat_id,
                    media=media,
                    message_thread_id=topic.thread_id,
                ),
                retry_unknown_outcome=False,
            )
        except Exception as error:
            failure = classify_error(error)
            for message, job, delivery in deliveries:
                reason = (
                    "delivery outcome unknown after timeout"
                    if failure.outcome_unknown
                    else str(error)
                )
                self._repositories.update_job(job.id, "failed", error=reason)
                if failure.outcome_unknown:
                    self._repositories.update_delivery(delivery.id, "failed", reason=reason)
                    self._repositories.add_dead_letter(
                        job.id,
                        "sort_media_group_uncertain",
                        job.payload,
                        "outcome_unknown",
                        str(error),
                    )
                    self._audit(
                        message,
                        "sort.media_group",
                        "failed",
                        str(job.id),
                        "outcome_unknown",
                    )
            if failure.outcome_unknown:
                self._repositories.increment_metric(
                    "telegram_delivery_outcome_unknown",
                    len(deliveries),
                )
                LOGGER.error(
                    "Grouped album outcome is unknown; automatic fallback suppressed",
                    extra={
                        "source_chat_id": source_chat_id,
                        "media_group_id": getattr(messages[0], "media_group_id", None),
                        "error_category": failure.category,
                    },
                )
                return AlbumDeliveryOutcome.OUTCOME_UNKNOWN
            self._repositories.increment_metric("media_group_fallbacks", 1)
            LOGGER.warning(
                "Grouped album delivery was rejected; falling back to individual copies",
                extra={
                    "source_chat_id": source_chat_id,
                    "media_group_id": getattr(messages[0], "media_group_id", None),
                    "error_category": failure.category,
                },
            )
            return AlbumDeliveryOutcome.SAFE_FALLBACK

        sent_ids = [sent.message_id for sent in sent_messages]
        if len(sent_ids) != len(deliveries):
            self._repositories.increment_metric("media_group_response_mismatches", 1)
            LOGGER.warning(
                "Grouped album delivery returned an unexpected number of messages",
                extra={
                    "source_chat_id": source_chat_id,
                    "expected_count": len(deliveries),
                    "actual_count": len(sent_ids),
                },
            )
            for message, job, delivery in deliveries[len(sent_ids) :]:
                reason = "group delivery returned too few messages; outcome unknown"
                self._repositories.update_job(
                    job.id,
                    "failed",
                    error=reason,
                )
                self._repositories.update_delivery(
                    delivery.id,
                    "failed",
                    reason=reason,
                )
                self._repositories.add_dead_letter(
                    job.id,
                    "sort_media_group_uncertain",
                    job.payload,
                    "outcome_unknown",
                    reason,
                )
                self._audit(
                    message,
                    "sort.media_group",
                    "failed",
                    str(job.id),
                    "outcome_unknown",
                )

        for (message, job, delivery), destination_message_id in zip(deliveries, sent_ids):
            self._repositories.update_delivery(
                delivery.id,
                "sent",
                destination_message_id=destination_message_id,
            )
            self._repositories.update_job(job.id, "completed")
            self._repositories.increment_metric("sort_deliveries", 1)
            self._audit(message, "sort.media_group", "success", str(job.id))
            self._indexing.index_copy(
                message,
                bot_id=context.bot.id,
                destination_chat_id=self._settings.archive_chat_id,
                destination_thread_id=topic.thread_id,
                destination_message_id=destination_message_id,
            )
        if len(sent_ids) < len(deliveries):
            self._repositories.increment_metric(
                "telegram_delivery_outcome_unknown",
                len(deliveries) - len(sent_ids),
            )
            return AlbumDeliveryOutcome.OUTCOME_UNKNOWN
        if self._live.send_confirmation():
            await self._confirm_delivery(messages[0], topic)
        return AlbumDeliveryOutcome.DELIVERED

    def _record_skip(self, message: Any, decision: SortDecision) -> None:
        key = f"sort-conflict:{self._settings.source_chat_id}:{message.message_id}"
        job = self._repositories.enqueue(
            "sort",
            key,
            {"source_message_id": message.message_id, "reason": decision.reason},
        )
        self._repositories.update_job(job.id, "completed")
        self._repositories.increment_metric("sort_conflicts", 1)
        self._audit(message, "sort.conflict", "denied", str(job.id))

    def explain(self, text: str) -> SortDecision:
        return self._matcher.decide(text)

    async def resume_job(
        self,
        job_id: int,
        context: Any,
    ) -> bool:
        """Replay a persisted sort job without needing the original Telegram update."""

        job = self._repositories.get_job(job_id)
        if job is None or job.kind != "sort" or job.status not in {"pending", "failed"}:
            return False
        try:
            source_chat_id = int(job.payload["source_chat_id"])
            source_message_id = int(job.payload["source_message_id"])
            destination_chat_id = int(job.payload["destination_chat_id"])
            destination_thread_id = int(job.payload["destination_thread_id"])
            reason = str(job.payload["reason"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"sort job {job_id} has an invalid recovery payload") from error
        if destination_chat_id != self._settings.archive_chat_id:
            raise ValueError(f"sort job {job_id} targets an unexpected archive chat")
        topic = self._repositories.get(destination_chat_id, destination_thread_id)
        if topic is None or not topic.is_active:
            raise ValueError(f"sort job {job_id} targets an inactive topic")
        await self._deliver(
            RecoveredMessage(source_message_id),
            source_chat_id,
            SortDecision("matched", topic, (), reason),
            context,
        )
        recovered = self._repositories.get_job(job_id)
        return recovered is not None and recovered.status == "completed"

    def _audit(
        self,
        message: Any,
        action: str,
        outcome: str,
        resource_id: str,
        error_category: str | None = None,
    ) -> None:
        sender = getattr(message, "from_user", None)
        details = {"message_id": getattr(message, "message_id", None)}
        if error_category:
            details["error_category"] = error_category
        self._repositories.add_audit_event(
            actor_type="telegram_bot" if getattr(sender, "is_bot", False) else "telegram_user",
            actor_id=str(getattr(sender, "id", "unknown")),
            action=action,
            resource_type="job",
            resource_id=resource_id,
            outcome=outcome,
            details=details,
        )

    async def _copy_one_to_backup(
        self,
        message: Any,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        topic_id: int,
    ) -> bool:
        try:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "message_thread_id": topic_id,
                "from_chat_id": message.chat_id,
                "message_id": message.message_id,
            }
            if message.caption is not None:
                kwargs["caption"] = _strip_sender_identifiers(
                    message.caption, message.caption_entities
                )
            await context.bot.copy_message(**kwargs)
            return True
        except Exception as error:
            LOGGER.warning("Backup copy failed (%s): %s", classify_error(error).category, error)
            return False

    def _record_backup_success(self, msg: Any, backup_chat_id: int, destination_topic: int) -> None:
        self._backed_up[(msg.chat_id, msg.message_id)] = None
        detected = media_type(msg)
        unique_id = media_unique_id(msg, detected) if detected else None
        if unique_id:
            try:
                self._repositories.record_backup_file(backup_chat_id, destination_topic, unique_id)
            except Exception as error:
                LOGGER.warning("Recording backup file failed: %s", error)

    async def _backup_to_second_group(
        self, messages: tuple[Any, ...], context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            album_text = _album_text(messages)
            hashtags = {match.group(1).casefold() for match in HASHTAG_RE.finditer(album_text)}
            if not hashtags:
                return

            destination_topic = None
            for tag, topic_id in self._live.media_backup_tag_topics():
                if tag.casefold() in hashtags:
                    destination_topic = topic_id
                    break
            if destination_topic is None:
                return

            backup_chat_id = self._live.media_backup_chat_id()

            pending = []
            seen_unique_ids: set[str] = set()
            for msg in messages:
                if (msg.chat_id, msg.message_id) in self._backed_up:
                    continue
                detected = media_type(msg)
                unique_id = media_unique_id(msg, detected) if detected else None
                if unique_id and (
                    unique_id in seen_unique_ids
                    or self._repositories.has_backup_file(
                        backup_chat_id, destination_topic, unique_id
                    )
                ):
                    continue
                pending.append(msg)
                if unique_id:
                    seen_unique_ids.add(unique_id)
            if not pending:
                return

            backed_up_count = 0

            if len(pending) == 1:
                if await self._copy_one_to_backup(
                    pending[0], context, backup_chat_id, destination_topic
                ):
                    self._record_backup_success(pending[0], backup_chat_id, destination_topic)
                    backed_up_count = 1
            else:
                captions = tuple(
                    _strip_sender_identifiers(msg.caption or "", msg.caption_entities) or None
                    for msg in pending
                )
                payload = _media_group_payload(tuple(pending), captions=captions)
                if payload is not None:
                    try:
                        sent = await context.bot.send_media_group(
                            chat_id=backup_chat_id,
                            message_thread_id=destination_topic,
                            media=payload,
                        )
                        sent_count = len(sent)
                        for msg in pending[:sent_count]:
                            self._record_backup_success(msg, backup_chat_id, destination_topic)
                        backed_up_count = sent_count
                    except Exception as e:
                        LOGGER.warning(
                            "send_media_group failed in _backup_to_second_group: %s",
                            classify_error(e).category,
                        )
                        for msg in pending:
                            if await self._copy_one_to_backup(
                                msg, context, backup_chat_id, destination_topic
                            ):
                                self._record_backup_success(msg, backup_chat_id, destination_topic)
                                backed_up_count += 1
                else:
                    for msg in pending:
                        if await self._copy_one_to_backup(
                            msg, context, backup_chat_id, destination_topic
                        ):
                            self._record_backup_success(msg, backup_chat_id, destination_topic)
                            backed_up_count += 1

            while len(self._backed_up) > 5000:
                self._backed_up.popitem(last=False)

            failed_count = len(pending) - backed_up_count
            if failed_count > 0:
                self._repositories.increment_metric("media_backup_failures", failed_count)
        except Exception:
            LOGGER.warning("Unexpected error in _backup_to_second_group", exc_info=True)


def _matching_routes(
    mappings: list[RouteMappingRecord],
    topics: dict[int, TopicRecord],
    kind: str,
    values: set[str],
) -> list[RouteMatch]:
    return [
        RouteMatch(topics[mapping.topic_id], mapping)
        for mapping in mappings
        if mapping.kind == kind
        and mapping.normalized_value in values
        and mapping.topic_id in topics
    ]


def _matching_non_hashtags(
    mappings: list[RouteMappingRecord],
    topics: dict[int, TopicRecord],
    text: str,
) -> list[RouteMatch]:
    return [
        RouteMatch(topics[mapping.topic_id], mapping)
        for mapping in mappings
        if mapping.topic_id in topics
        and (
            (mapping.kind == "keyword" and contains_keyword(text, mapping.normalized_value))
            or (mapping.kind == "phrase" and contains_phrase(text, mapping.normalized_value))
        )
    ]


_AT_MENTION_RE = re.compile(r"(?<!\w)@\w{5,32}")
_URL_RE = re.compile(r"(?:https?://|t\.me/)\S+")


def _strip_sender_identifiers(text: str, entities: tuple[Any, ...] | None) -> str:
    if not text:
        return ""

    utf16 = text.encode("utf-16-le")
    removal_ranges = []

    if entities:
        for ent in entities:
            etype = getattr(ent, "type", "")
            if etype not in ("mention", "text_mention", "url", "text_link", "email"):
                continue
            offset = getattr(ent, "offset", 0)
            length = getattr(ent, "length", 0)
            if length <= 0:
                continue
            start_u16 = max(0, offset * 2)
            end_u16 = min(len(utf16), (offset + length) * 2)
            if start_u16 >= end_u16:
                continue
            removal_ranges.append((start_u16, end_u16))

    if removal_ranges:
        removal_ranges.sort()
        merged: list[tuple[int, int]] = []
        for start, end in removal_ranges:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))

        chunks = []
        pos = 0
        for start, end in merged:
            if start > pos:
                chunks.append(utf16[pos:start])
            pos = end
        if pos < len(utf16):
            chunks.append(utf16[pos:])
        text = b"".join(chunks).decode("utf-16-le")

    text = _AT_MENTION_RE.sub("", text)
    text = _URL_RE.sub("", text)

    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(line)

    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()

    return "\n".join(lines)


def _album_text(messages: tuple[Any, ...]) -> str:
    return "\n".join(text for message in messages if (text := _message_text(message)))


def _album_caption_count(messages: tuple[Any, ...]) -> int:
    return sum(1 for message in messages if _message_text(message))


def _message_text(message: Any) -> str:
    return (getattr(message, "caption", None) or getattr(message, "text", None) or "").strip()


def _media_group_payload(
    messages: tuple[Any, ...],
    captions: tuple[str | None, ...] | None = None,
) -> tuple[InputMediaPhoto | InputMediaVideo | InputMediaDocument | InputMediaAudio, ...] | None:
    media_types = tuple(media_type(message) for message in messages)
    if any(kind is None for kind in media_types):
        return None
    unique_types = set(media_types)
    if not (
        unique_types <= ALBUM_VISUAL_MEDIA_TYPES
        or len(unique_types) == 1
        and next(iter(unique_types)) in ALBUM_HOMOGENEOUS_MEDIA_TYPES
    ):
        return None

    if captions is not None and len(captions) != len(messages):
        return None

    payload: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument | InputMediaAudio] = []
    for i, (message, kind) in enumerate(zip(messages, media_types)):
        media_id = _album_file_id(message, kind)
        if media_id is None:
            return None
        caption = (
            captions[i]
            if captions is not None
            else (getattr(message, "caption", None) or "").strip() or None
        )
        caption_entities = (
            None if captions is not None else getattr(message, "caption_entities", None)
        )
        if kind == "photo":
            payload.append(
                InputMediaPhoto(media_id, caption=caption, caption_entities=caption_entities)
            )
        elif kind == "video":
            payload.append(
                InputMediaVideo(media_id, caption=caption, caption_entities=caption_entities)
            )
        elif kind == "document":
            payload.append(
                InputMediaDocument(media_id, caption=caption, caption_entities=caption_entities)
            )
        elif kind == "audio":
            payload.append(
                InputMediaAudio(media_id, caption=caption, caption_entities=caption_entities)
            )
        else:
            return None
    return tuple(payload)


def post_link(chat_id: int, thread_id: int, message_id: int) -> str:
    internal_id = str(chat_id).removeprefix("-100").lstrip("-")
    return f"https://t.me/c/{internal_id}/{thread_id}/{message_id}"


def _album_file_id(message: Any, kind: str | None) -> str | None:
    if kind == "photo":
        photos = getattr(message, "photo", None) or ()
        if not photos:
            return None
        return getattr(photos[-1], "file_id", None)
    media = getattr(message, kind or "", None)
    return getattr(media, "file_id", None)
