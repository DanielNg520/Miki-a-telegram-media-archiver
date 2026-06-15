from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from miki_sorter_bot.config import Settings
from miki_sorter_bot.indexing import IndexingService, TOKEN_RE, media_type
from miki_sorter_bot.repositories import RouteMappingRecord, SqliteRepositories, TopicRecord
from miki_sorter_bot.reliability import DeliveryExecutor, RateLimiter, RetryPolicy, classify_error

LOGGER = logging.getLogger(__name__)
HASHTAG_RE = re.compile(r"(?<!\w)#(\w+(?:-\w+)*)", re.UNICODE)


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
        if text:
            decision = self._matcher.decide(text)
            if album_key is not None:
                self._remember_album_decision(album_key, decision)
        elif album_key is not None:
            decision = self._album_decisions.get(album_key)
            if decision is None:
                return
        else:
            return
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
