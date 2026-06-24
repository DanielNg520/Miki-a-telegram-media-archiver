from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
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
    IndexingService,
    contains_keyword,
    contains_phrase,
    media_type,
)
from miki_sorter_bot.repositories import RouteMappingRecord, SqliteRepositories, TopicRecord
from miki_sorter_bot.reliability import DeliveryExecutor, RateLimiter, RetryPolicy, classify_error

LOGGER = logging.getLogger(__name__)
HASHTAG_RE = re.compile(r"(?<!\w)#(\w+(?:-\w+)*)", re.UNICODE)
ALBUM_VISUAL_MEDIA_TYPES = {"photo", "video"}
ALBUM_HOMOGENEOUS_MEDIA_TYPES = {"audio", "document"}


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
    ) -> None:
        self._settings = settings
        self._repositories = repositories
        self._indexing = indexing
        self._delivery_executor = delivery_executor or DeliveryExecutor(
            retry_policy=RetryPolicy(),
            rate_limiter=RateLimiter(1000),
        )
        self._matcher = RouteMatcher(repositories, settings.archive_chat_id)
        self._album_decisions: OrderedDict[tuple[int, str], SortDecision] = OrderedDict()
        self._album_source_threads: OrderedDict[tuple[int, str], int] = OrderedDict()
        self._pending_albums: dict[tuple[int, str], PendingAlbum] = {}
        self._album_flush_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._album_flush_delay = getattr(settings, "album_flush_delay_seconds", 5.0)
        self._album_max_wait = getattr(settings, "album_max_wait_seconds", 30.0)
        self._suspend_album_reschedule = False
        # Reconcile env-configured forwarding pairs into the database once, so
        # the DB is the live source of truth and pairs become manageable via
        # Telegram without a restart.
        self._repositories.seed_forwarding_pairs(getattr(settings, "topic_forwarding_pairs", ()))

    def _effective_source_thread_id(self) -> int:
        """The topic Miki listens to: a runtime override if set, else the env value.

        Read live from the database so ``/source_set`` takes effect without a
        restart, mirroring how route mappings are resolved per message.
        """

        override = self._repositories.get_runtime_setting("source_thread_id")
        if override is not None:
            try:
                return int(override)
            except ValueError:
                LOGGER.warning(
                    "Ignoring invalid runtime source_thread_id override",
                    extra={"value": override},
                )
        return self._settings.source_thread_id

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

        forwarding_pair = self._forwarding_pair(chat.id, source_thread_id)
        is_primary_source = (
            chat.id == self._settings.source_chat_id
            and source_thread_id == self._effective_source_thread_id()
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
                    return
                else:
                    self._remember_album_decision(album_key, decision)
            self._queue_album_message(album_key, message, chat.id, decision, context)
            return
        if direct_decision is not None:
            await self._deliver(message, chat.id, direct_decision, context)
            return
        if not text:
            return
        decision = self._matcher.decide(text)
        if decision.status == "unmatched":
            return
        if decision.status == "conflict":
            self._record_skip(message, decision)
            LOGGER.warning(
                "Sorting conflict",
                extra={"chat_id": chat.id, "message_id": message.message_id},
            )
            return
        await self._deliver(message, chat.id, decision, context)

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
        if existing_task is not None and not existing_task.done():
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
            await asyncio.sleep(self._album_flush_delay)
            await self._deliver_album_background(key, context)
        finally:
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
        if pending.decision is None:
            if time.monotonic() - pending.first_seen_at < self._album_max_wait:
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
            LOGGER.info(
                "Dropping unrouted album after decision wait expired",
                extra={
                    "source_chat_id": pending.source_chat_id,
                    "media_group_id": key[1],
                    "message_count": len(pending.messages),
                    "caption_count": _album_caption_count(tuple(pending.messages.values())),
                },
            )
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
        if len(messages) == 1:
            await self._deliver(messages[0], pending.source_chat_id, pending.decision, context)
            return
        group_outcome = await self._deliver_media_group(
            messages,
            pending.source_chat_id,
            pending.decision,
            context,
        )
        if group_outcome is not AlbumDeliveryOutcome.SAFE_FALLBACK:
            return
        failed_count = 0
        for message in messages:
            try:
                await self._deliver(message, pending.source_chat_id, pending.decision, context)
            except Exception as error:
                failed_count += 1
                failure = classify_error(error)
                LOGGER.warning(
                    "Individual album member delivery failed; continuing album",
                    extra={
                        "source_chat_id": pending.source_chat_id,
                        "source_message_id": getattr(message, "message_id", None),
                        "media_group_id": key[1],
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
                    "source_chat_id": pending.source_chat_id,
                    "media_group_id": key[1],
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
        if self._settings.sort_dry_run:
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
        if self._settings.send_confirmation:
            await message.reply_text(f"Sorted to {topic.name}.", quote=True)

    def _decide_album_text(self, pending: PendingAlbum) -> SortDecision | None:
        text = _album_text(tuple(pending.messages.values()))
        if not text:
            return None
        decision = self._matcher.decide(text)
        if decision.status == "matched":
            return decision
        if decision.status == "conflict":
            for message in pending.messages.values():
                self._record_skip(message, decision)
            LOGGER.warning(
                "Album sorting conflict",
                extra={
                    "source_chat_id": pending.source_chat_id,
                    "message_count": len(pending.messages),
                },
            )
        return None

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
            if self._settings.sort_dry_run:
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
        if not deliveries or self._settings.sort_dry_run:
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
        if self._settings.send_confirmation:
            await messages[0].reply_text(f"Sorted to {topic.name}.", quote=True)
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


def _album_text(messages: tuple[Any, ...]) -> str:
    return "\n".join(text for message in messages if (text := _message_text(message)))


def _album_caption_count(messages: tuple[Any, ...]) -> int:
    return sum(1 for message in messages if _message_text(message))


def _message_text(message: Any) -> str:
    return (getattr(message, "caption", None) or getattr(message, "text", None) or "").strip()


def _media_group_payload(
    messages: tuple[Any, ...],
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

    payload: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument | InputMediaAudio] = []
    for message, kind in zip(messages, media_types):
        media_id = _album_file_id(message, kind)
        if media_id is None:
            return None
        caption = (getattr(message, "caption", None) or "").strip() or None
        caption_entities = getattr(message, "caption_entities", None)
        if kind == "photo":
            payload.append(
                InputMediaPhoto(
                    media_id,
                    caption=caption,
                    caption_entities=caption_entities,
                )
            )
        elif kind == "video":
            payload.append(
                InputMediaVideo(
                    media_id,
                    caption=caption,
                    caption_entities=caption_entities,
                )
            )
        elif kind == "document":
            payload.append(
                InputMediaDocument(
                    media_id,
                    caption=caption,
                    caption_entities=caption_entities,
                )
            )
        elif kind == "audio":
            payload.append(
                InputMediaAudio(
                    media_id,
                    caption=caption,
                    caption_entities=caption_entities,
                )
            )
        else:
            return None
    return tuple(payload)


def _album_file_id(message: Any, kind: str | None) -> str | None:
    if kind == "photo":
        photos = getattr(message, "photo", None) or ()
        if not photos:
            return None
        return getattr(photos[-1], "file_id", None)
    media = getattr(message, kind or "", None)
    return getattr(media, "file_id", None)
