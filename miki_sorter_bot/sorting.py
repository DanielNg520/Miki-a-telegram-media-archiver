from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass

from telegram import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from miki_sorter_bot.config import Settings
from miki_sorter_bot.indexing import IndexingService, TOKEN_RE, media_type
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


class RouteMatcher:
    def __init__(self, repositories: SqliteRepositories, archive_chat_id: int) -> None:
        self._repositories = repositories
        self._archive_chat_id = archive_chat_id

    def decide(self, text: str) -> SortDecision:
        mappings = self._repositories.list_mappings(self._archive_chat_id)
        topics = {
            topic.id: topic
            for topic in self._repositories.list_topics(self._archive_chat_id)
        }
        normalized_tokens = {
            match.group(0).casefold() for match in TOKEN_RE.finditer(text)
        }
        normalized_text = " ".join(match.group(0).casefold() for match in TOKEN_RE.finditer(text))
        hashtags = {match.group(1).casefold() for match in HASHTAG_RE.finditer(text)}

        hashtag_matches = _matching_routes(mappings, topics, "hashtag", hashtags, normalized_text)
        candidates = hashtag_matches or _matching_non_hashtags(
            mappings,
            topics,
            normalized_tokens,
            normalized_text,
        )
        if not candidates:
            return SortDecision("unmatched", None, (), "no configured route matched")
        topic_ids = {match.topic.id for match in candidates}
        if len(topic_ids) > 1:
            return SortDecision("conflict", None, tuple(candidates), "multiple destinations matched")
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
        self._pending_albums: dict[tuple[int, str], PendingAlbum] = {}
        self._album_flush_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._album_flush_delay = getattr(settings, "album_flush_delay_seconds", 5.0)
        self._album_max_wait = getattr(settings, "album_max_wait_seconds", 30.0)

    async def handle_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        if chat.id != self._settings.source_chat_id:
            return
        if chat.type not in {ChatType.SUPERGROUP, ChatType.GROUP}:
            return
        if message.message_thread_id != self._settings.source_thread_id:
            return
        if media_type(message) is None:
            return
        sender = getattr(message, "from_user", None)
        if getattr(sender, "id", None) == context.bot.id:
            return
        media_group_id = getattr(message, "media_group_id", None)
        album_key = (chat.id, media_group_id) if media_group_id else None
        text = (message.caption or message.text or "").strip()
        if album_key is not None:
            decision = self._matcher.decide(text) if text else self._album_decisions.get(album_key)
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
                self._remember_album_decision(album_key, decision)
            self._queue_album_message(album_key, message, chat.id, decision, context)
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

    def _queue_album_message(
        self,
        key: tuple[int, str],
        message: object,
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
                if key in self._pending_albums:
                    self._album_flush_tasks[key] = asyncio.create_task(
                        self._flush_album_after_delay(key, context)
                    )

    async def flush_pending_albums(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        tasks = tuple(self._album_flush_tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        keys = tuple(self._pending_albums)
        for key in keys:
            await self._deliver_album(key, context)

    async def _deliver_album(
        self,
        key: tuple[int, str],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        pending = self._pending_albums.pop(key, None)
        if pending is None:
            return
        if pending.decision is None:
            if time.monotonic() - pending.first_seen_at < self._album_max_wait:
                self._pending_albums[key] = pending
                LOGGER.info(
                    "Album is waiting for a route decision",
                    extra={
                        "source_chat_id": pending.source_chat_id,
                        "media_group_id": key[1],
                        "message_count": len(pending.messages),
                    },
                )
                return
            LOGGER.info(
                "Dropping unrouted album after decision wait expired",
                extra={
                    "source_chat_id": pending.source_chat_id,
                    "media_group_id": key[1],
                    "message_count": len(pending.messages),
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
        if await self._deliver_media_group(
            messages,
            pending.source_chat_id,
            pending.decision,
            context,
        ):
            return
        for message in messages:
            await self._deliver(message, pending.source_chat_id, pending.decision, context)

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
        message: object,
        source_chat_id: int,
        decision: SortDecision,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        assert decision.topic is not None
        key = (
            f"sort:{source_chat_id}:{message.message_id}:"
            f"{self._settings.archive_chat_id}:{decision.topic.thread_id}"
        )
        job = self._repositories.enqueue(
            "sort",
            key,
            {
                "source_chat_id": source_chat_id,
                "source_message_id": message.message_id,
                "destination_chat_id": self._settings.archive_chat_id,
                "destination_thread_id": decision.topic.thread_id,
                "reason": decision.reason,
            },
        )
        delivery = self._repositories.ensure_delivery(
            job.id,
            source_chat_id=source_chat_id,
            source_message_id=message.message_id,
            destination_chat_id=self._settings.archive_chat_id,
            destination_thread_id=decision.topic.thread_id,
            reason=decision.reason,
        )
        if delivery.status in {"sent", "skipped"}:
            self._repositories.increment_metric("sort_duplicates", 1)
            return
        if self._settings.sort_dry_run:
            self._repositories.update_delivery(delivery.id, "skipped", reason="dry-run")
            self._repositories.update_job(job.id, "completed")
            return
        self._repositories.update_job(job.id, "running")
        try:
            copied = await self._delivery_executor.run(
                lambda: context.bot.copy_message(
                    chat_id=self._settings.archive_chat_id,
                    from_chat_id=source_chat_id,
                    message_id=message.message_id,
                    message_thread_id=decision.topic.thread_id,
                )
            )
        except Exception as error:
            failure = classify_error(error)
            self._repositories.update_delivery(delivery.id, "failed", reason=str(error))
            self._repositories.update_job(job.id, "failed", error=str(error))
            self._repositories.add_dead_letter(
                job.id,
                "sort_copy",
                job.payload,
                failure.category,
                str(error),
            )
            self._audit(message, "sort.copy", "failed", str(job.id), failure.category)
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
            destination_thread_id=decision.topic.thread_id,
            destination_message_id=copied.message_id,
        )
        if self._settings.send_confirmation:
            await message.reply_text(f"Sorted to {decision.topic.name}.", quote=True)

    async def _deliver_media_group(
        self,
        messages: tuple[object, ...],
        source_chat_id: int,
        decision: SortDecision,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        assert decision.topic is not None
        media = _media_group_payload(messages)
        if media is None:
            return False

        completed_count = 0
        deliveries = []
        for message in messages:
            key = (
                f"sort:{source_chat_id}:{message.message_id}:"
                f"{self._settings.archive_chat_id}:{decision.topic.thread_id}"
            )
            job = self._repositories.enqueue(
                "sort",
                key,
                {
                    "source_chat_id": source_chat_id,
                    "source_message_id": message.message_id,
                    "destination_chat_id": self._settings.archive_chat_id,
                    "destination_thread_id": decision.topic.thread_id,
                    "reason": decision.reason,
                    "delivery_method": "send_media_group",
                },
            )
            delivery = self._repositories.ensure_delivery(
                job.id,
                source_chat_id=source_chat_id,
                source_message_id=message.message_id,
                destination_chat_id=self._settings.archive_chat_id,
                destination_thread_id=decision.topic.thread_id,
                reason=decision.reason,
            )
            if delivery.status in {"sent", "skipped"}:
                completed_count += 1
                continue
            if self._settings.sort_dry_run:
                self._repositories.update_delivery(delivery.id, "skipped", reason="dry-run")
                self._repositories.update_job(job.id, "completed")
                continue
            self._repositories.update_job(job.id, "running")
            deliveries.append((message, job, delivery))
        if deliveries and completed_count:
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
            return False
        if completed_count:
            self._repositories.increment_metric("sort_duplicates", completed_count)
        if not deliveries or self._settings.sort_dry_run:
            return True

        try:
            sent_messages = await self._delivery_executor.run(
                lambda: context.bot.send_media_group(
                    chat_id=self._settings.archive_chat_id,
                    media=media,
                    message_thread_id=decision.topic.thread_id,
                )
            )
        except Exception as error:
            failure = classify_error(error)
            self._repositories.increment_metric("media_group_fallbacks", 1)
            LOGGER.warning(
                "Grouped album delivery failed; falling back to individual copies",
                extra={
                    "source_chat_id": source_chat_id,
                    "media_group_id": getattr(messages[0], "media_group_id", None),
                    "error_category": failure.category,
                },
            )
            return False

        sent_ids = [sent.message_id for sent in sent_messages]
        if len(sent_ids) != len(deliveries):
            self._repositories.increment_metric("media_group_fallbacks", 1)
            LOGGER.warning(
                "Grouped album delivery returned an unexpected number of messages",
                extra={
                    "source_chat_id": source_chat_id,
                    "expected_count": len(deliveries),
                    "actual_count": len(sent_ids),
                },
            )
            return False

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
                destination_thread_id=decision.topic.thread_id,
                destination_message_id=destination_message_id,
            )
        if self._settings.send_confirmation:
            await messages[0].reply_text(f"Sorted to {decision.topic.name}.", quote=True)
        return True

    def _record_skip(self, message: object, decision: SortDecision) -> None:
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

    def _audit(
        self,
        message: object,
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
    _: str,
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
    tokens: set[str],
    normalized_text: str,
) -> list[RouteMatch]:
    padded = f" {normalized_text} "
    return [
        RouteMatch(topics[mapping.topic_id], mapping)
        for mapping in mappings
        if mapping.topic_id in topics
        and (
            (
                mapping.kind == "keyword"
                and any(mapping.normalized_value in token for token in tokens)
            )
            or (
                mapping.kind == "phrase"
                and f" {mapping.normalized_value} " in padded
            )
        )
    ]


def _media_group_payload(
    messages: tuple[object, ...],
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

    payload = []
    for message, kind in zip(messages, media_types):
        media_id = _album_file_id(message, kind)
        if media_id is None:
            return None
        caption = (getattr(message, "caption", None) or "").strip() or None
        caption_entities = getattr(message, "caption_entities", None)
        kwargs = {"caption": caption, "caption_entities": caption_entities}
        if kind == "photo":
            payload.append(InputMediaPhoto(media_id, **kwargs))
        elif kind == "video":
            payload.append(InputMediaVideo(media_id, **kwargs))
        elif kind == "document":
            payload.append(InputMediaDocument(media_id, **kwargs))
        elif kind == "audio":
            payload.append(InputMediaAudio(media_id, **kwargs))
        else:
            return None
    return tuple(payload)


def _album_file_id(message: object, kind: str | None) -> str | None:
    if kind == "photo":
        photos = getattr(message, "photo", None) or ()
        if not photos:
            return None
        return getattr(photos[-1], "file_id", None)
    media = getattr(message, kind or "", None)
    return getattr(media, "file_id", None)
