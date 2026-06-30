from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from miki_sorter_bot.config import Settings
from miki_sorter_bot.repositories import (
    IndexedPostRecord,
    JobRecord,
    RetrievalItemRecord,
    SqliteRepositories,
    TopicRecord,
)
from miki_sorter_bot.reliability import DeliveryExecutor, RateLimiter, RetryPolicy, classify_error
from miki_sorter_bot.settings_registry import LiveSettings

LOGGER = logging.getLogger(__name__)

# Telegram caps a media group (album) at 10 items.
_TELEGRAM_ALBUM_LIMIT = 10


class RequestValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    topic_reference: str
    keywords: tuple[str, ...]
    match_mode: str
    limit: int


@dataclass(slots=True)
class RetrievalSummary:
    matched: int = 0
    copied: int = 0
    unavailable: int = 0
    skipped: int = 0
    failed: int = 0
    albums: int = 0
    cancelled: bool = False

    def text(self, job_id: int) -> str:
        state = "cancelled" if self.cancelled else "completed"
        album_note = f" ({self.albums} as album{'s' if self.albums != 1 else ''})" if self.albums else ""
        return (
            f"Request {job_id} {state}: {self.matched} matched, "
            f"{self.copied} copied{album_note}, {self.unavailable} unavailable, "
            f"{self.skipped} skipped, {self.failed} failed."
        )


@dataclass(slots=True)
class RecoveredRequestMessage:
    bot: Any
    chat_id: int
    message_thread_id: int
    message_id: int

    async def reply_text(self, text: str) -> None:
        await self.bot.send_message(
            chat_id=self.chat_id,
            message_thread_id=self.message_thread_id,
            reply_to_message_id=self.message_id,
            text=text,
        )


def parse_request(text: str, *, default_limit: int, max_limit: int) -> RetrievalRequest:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0].casefold() != "#request":
        raise RequestValidationError("The first line must be #request.")
    fields: dict[str, str] = {}
    allowed = {"topic", "keywords", "match", "limit"}
    for line in lines[1:]:
        if ":" not in line:
            raise RequestValidationError(f"Invalid field line: {line}")
        key, value = (part.strip() for part in line.split(":", 1))
        normalized_key = key.casefold()
        if normalized_key not in allowed:
            raise RequestValidationError(f"Unknown field: {key}")
        if normalized_key in fields:
            raise RequestValidationError(f"Duplicate field: {key}")
        fields[normalized_key] = value
    missing = [name for name in ("topic", "keywords") if not fields.get(name)]
    if missing:
        raise RequestValidationError("Missing required field(s): " + ", ".join(missing))
    keywords = _parse_keywords(fields["keywords"])
    match_mode = fields.get("match", "all").casefold()
    if match_mode not in {"all", "any"}:
        raise RequestValidationError("match must be all or any.")
    try:
        limit = int(fields.get("limit", str(default_limit)))
    except ValueError as error:
        raise RequestValidationError("limit must be an integer.") from error
    if limit < 1 or limit > max_limit:
        raise RequestValidationError(f"limit must be between 1 and {max_limit}.")
    return RetrievalRequest(fields["topic"], keywords, match_mode, limit)


class RetrievalService:
    def __init__(
        self,
        settings: Settings,
        repositories: SqliteRepositories,
        delivery_executor: DeliveryExecutor | None = None,
        live_settings: LiveSettings | None = None,
    ) -> None:
        self._settings = settings
        self._repositories = repositories
        self._live = live_settings or LiveSettings(settings, repositories)
        self._delivery_executor = delivery_executor or DeliveryExecutor(
            retry_policy=RetryPolicy(),
            rate_limiter=RateLimiter(1000),
        )

    async def handle_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if message is None or chat is None or user is None:
            return
        text = (message.text or "").strip()
        if not text.casefold().startswith("#request"):
            return
        if (
            chat.id != self._live.effective_request_chat_id()
            or message.message_thread_id not in self._live.request_topic_ids()
        ):
            await message.reply_text("Retrieval requests are not allowed in this topic.")
            return
        if user.is_bot and user.id not in self._settings.requester_bot_ids:
            await message.reply_text("This bot is not authorized to submit retrieval requests.")
            return
        try:
            request = parse_request(
                text,
                default_limit=self._settings.default_request_limit,
                max_limit=self._settings.max_request_limit,
            )
            topic = self._resolve_topic(request.topic_reference)
        except RequestValidationError as error:
            await message.reply_text(f"Invalid request: {error}")
            return
        key = f"retrieve:{chat.id}:{message.message_id}"
        job = self._repositories.enqueue(
            "retrieve",
            key,
            {
                "request_chat_id": chat.id,
                "request_thread_id": message.message_thread_id,
                "request_message_id": message.message_id,
                "requester_id": user.id,
                "source_thread_id": topic.thread_id,
                "keywords": list(request.keywords),
                "match": request.match_mode,
                "limit": request.limit,
            },
        )
        self._repositories.add_audit_event(
            actor_type="telegram_bot" if user.is_bot else "telegram_user",
            actor_id=str(user.id),
            action="retrieval.submit",
            resource_type="job",
            resource_id=str(job.id),
            outcome="success",
            details={"request_message_id": message.message_id},
        )
        if job.status in {"completed", "cancelled"}:
            self._repositories.increment_metric("retrieval_duplicates", 1)
            await message.reply_text(f"Request {job.id} was already {job.status}.")
            return
        if job.status == "running":
            self._repositories.increment_metric("retrieval_duplicates", 1)
            await message.reply_text(f"Request {job.id} is already running.")
            return
        await message.reply_text(f"Request {job.id} queued.")
        coroutine = self._execute(job.id, request, topic, message, chat.id, context)
        application = getattr(context, "application", None)
        if application is not None:
            application.create_task(coroutine, update=update)
        else:
            await coroutine

    async def cancel(
        self,
        update: Update,
        _: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        if user.id not in self._settings.admin_user_ids:
            await message.reply_text("Only a configured Miki administrator can cancel requests.")
            return
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.reply_text("Usage: /request_cancel <job ID>")
            return
        try:
            job_id = int(parts[1])
        except ValueError:
            await message.reply_text("Job ID must be an integer.")
            return
        cancelled = self._repositories.cancel_job(job_id, "retrieve")
        self._repositories.add_audit_event(
            actor_type="telegram_bot" if user.is_bot else "telegram_user",
            actor_id=str(user.id),
            action="retrieval.cancel",
            resource_type="job",
            resource_id=str(job_id),
            outcome="success" if cancelled else "denied",
        )
        await message.reply_text(
            f"Request {job_id} cancellation recorded."
            if cancelled
            else f"Request {job_id} could not be cancelled."
        )

    async def _execute(
        self,
        job_id: int,
        request: RetrievalRequest,
        topic: TopicRecord,
        request_message: Any,
        destination_chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        job = self._repositories.get_job(job_id)
        if job is None or job.status == "cancelled":
            return
        if not self._repositories.claim_job(job_id):
            return
        posts = self._repositories.search_posts(
            self._settings.archive_chat_id,
            topic.thread_id,
            request.keywords,
            request.match_mode,
            request.limit,
        )
        summary = RetrievalSummary(matched=len({post.logical_post_key for post in posts}))
        for group in _group_by_logical_post(posts):
            current = self._repositories.get_job(job_id)
            if current is None or current.status == "cancelled":
                summary.cancelled = True
                break
            pending: list[tuple[IndexedPostRecord, RetrievalItemRecord]] = []
            for post in group:
                item = self._repositories.ensure_retrieval_item(
                    job_id,
                    post.id,
                    destination_chat_id,
                    request_message.message_thread_id,
                )
                if item.status == "sent":
                    summary.skipped += 1
                    continue
                if not post.is_available:
                    self._repositories.update_retrieval_item(
                        item.id, "skipped", error="unavailable"
                    )
                    summary.unavailable += 1
                    continue
                pending.append((post, item))
            if not pending:
                continue
            if len(pending) > 1:
                await self._deliver_album(
                    job_id, job, pending, request_message, destination_chat_id, context, summary
                )
            else:
                post, item = pending[0]
                await self._deliver_single(
                    job_id, job, post, item, request_message, destination_chat_id, context, summary
                )
        if summary.copied:
            self._repositories.increment_metric("retrieval_items_copied", summary.copied)
        if summary.albums:
            self._repositories.increment_metric("retrieval_albums_batched", summary.albums)
        if summary.skipped:
            self._repositories.increment_metric("retrieval_items_skipped", summary.skipped)
        if summary.cancelled:
            self._repositories.update_job(job_id, "cancelled")
        elif summary.failed:
            self._repositories.update_job(job_id, "failed", error=f"{summary.failed} copies failed")
        else:
            self._repositories.update_job(job_id, "completed")
        self._repositories.add_audit_event(
            actor_type="system",
            actor_id="miki",
            action="retrieval.complete",
            resource_type="job",
            resource_id=str(job_id),
            outcome="failed" if summary.failed else "success",
            details={
                "matched": summary.matched,
                "copied": summary.copied,
                "unavailable": summary.unavailable,
                "skipped": summary.skipped,
                "failed": summary.failed,
                "cancelled": summary.cancelled,
            },
        )
        await request_message.reply_text(summary.text(job_id))

    async def _deliver_album(
        self,
        job_id: int,
        job: JobRecord,
        pending: list[tuple[IndexedPostRecord, RetrievalItemRecord]],
        request_message: Any,
        destination_chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        summary: RetrievalSummary,
    ) -> None:
        """Deliver album members as grouped batches via copy_messages.

        A logical post group is normally a single Telegram album (<= 10 items),
        but we defensively split into chunks of ``_TELEGRAM_ALBUM_LIMIT`` so an
        over-sized group can never make Telegram reject the whole batch.
        """
        for start in range(0, len(pending), _TELEGRAM_ALBUM_LIMIT):
            chunk = pending[start : start + _TELEGRAM_ALBUM_LIMIT]
            await self._deliver_album_chunk(
                job_id, job, chunk, request_message, destination_chat_id, context, summary
            )

    async def _deliver_album_chunk(
        self,
        job_id: int,
        job: JobRecord,
        chunk: list[tuple[IndexedPostRecord, RetrievalItemRecord]],
        request_message: Any,
        destination_chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        summary: RetrievalSummary,
    ) -> None:
        from_chat_id = chunk[0][0].source_chat_id
        message_ids = [post.source_message_id for post, _ in chunk]
        try:
            copied = await self._delivery_executor.run(
                lambda: context.bot.copy_messages(
                    chat_id=destination_chat_id,
                    from_chat_id=from_chat_id,
                    message_ids=message_ids,
                    message_thread_id=request_message.message_thread_id,
                ),
                retry_unknown_outcome=False,
            )
        except Exception as error:
            if classify_error(error).outcome_unknown:
                # The batch may have already delivered; re-copying per message would
                # duplicate the album. Fail the whole group and let recovery requeue it.
                self._fail_album(job_id, job, chunk, error, summary)
                return
            # A definite failure: fall back to per-message copies so unavailable
            # members are pinpointed rather than failing the whole album.
            LOGGER.warning(
                "Retrieval album copy failed; falling back to single copies",
                extra={"job_id": job_id, "message_ids": message_ids, "error": str(error)},
            )
            for post, item in chunk:
                await self._deliver_single(
                    job_id, job, post, item, request_message, destination_chat_id, context, summary
                )
            return
        for (post, item), copied_message in zip(chunk, copied):
            self._repositories.update_retrieval_item(
                item.id,
                "sent",
                destination_message_id=copied_message.message_id,
            )
            summary.copied += 1
        summary.albums += 1

    def _fail_album(
        self,
        job_id: int,
        job: JobRecord,
        pending: list[tuple[IndexedPostRecord, RetrievalItemRecord]],
        error: Exception,
        summary: RetrievalSummary,
    ) -> None:
        """Record every album member as failed after an unknown-outcome batch copy."""
        for post, item in pending:
            self._repositories.update_retrieval_item(
                item.id, "failed", error="delivery outcome unknown after timeout"
            )
            summary.failed += 1
            self._repositories.add_dead_letter(
                job_id,
                "retrieve_copy",
                {"post_id": post.id, "request": job.payload},
                "outcome_unknown",
                str(error),
            )
        self._repositories.increment_metric("telegram_delivery_outcome_unknown", len(pending))
        LOGGER.warning(
            "Retrieval album copy outcome unknown; deferred to recovery",
            extra={"job_id": job_id, "post_ids": [post.id for post, _ in pending]},
        )

    async def _deliver_single(
        self,
        job_id: int,
        job: JobRecord,
        post: IndexedPostRecord,
        item: RetrievalItemRecord,
        request_message: Any,
        destination_chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        summary: RetrievalSummary,
    ) -> None:
        try:
            copied = await self._delivery_executor.run(
                lambda: context.bot.copy_message(
                    chat_id=destination_chat_id,
                    from_chat_id=post.source_chat_id,
                    message_id=post.source_message_id,
                    message_thread_id=request_message.message_thread_id,
                ),
                retry_unknown_outcome=False,
            )
        except Exception as error:
            failure = classify_error(error)
            if failure.unavailable_source:
                self._repositories.mark_post_unavailable(post.id)
                self._repositories.update_retrieval_item(
                    item.id,
                    "skipped",
                    error="unavailable",
                )
                summary.unavailable += 1
            else:
                category = "outcome_unknown" if failure.outcome_unknown else failure.category
                item_error = (
                    "delivery outcome unknown after timeout"
                    if failure.outcome_unknown
                    else str(error)
                )
                self._repositories.update_retrieval_item(
                    item.id,
                    "failed",
                    error=item_error,
                )
                summary.failed += 1
                self._repositories.add_dead_letter(
                    job_id,
                    "retrieve_copy",
                    {"post_id": post.id, "request": job.payload},
                    category,
                    str(error),
                )
                if failure.outcome_unknown:
                    self._repositories.increment_metric(
                        "telegram_delivery_outcome_unknown",
                        1,
                    )
            LOGGER.warning(
                "Retrieval copy failed",
                extra={"job_id": job_id, "post_id": post.id},
            )
            return
        self._repositories.update_retrieval_item(
            item.id,
            "sent",
            destination_message_id=copied.message_id,
        )
        summary.copied += 1

    async def resume_job(
        self,
        job_id: int,
        context: Any,
    ) -> bool:
        job = self._repositories.get_job(job_id)
        if job is None or job.kind != "retrieve" or job.status not in {"pending", "failed"}:
            return False
        request, topic, message, destination_chat_id = self._recovery_request(job, context.bot)
        await self._execute(job.id, request, topic, message, destination_chat_id, context)
        recovered = self._repositories.get_job(job_id)
        return recovered is not None and recovered.status == "completed"

    def _recovery_request(
        self,
        job: JobRecord,
        bot: Any,
    ) -> tuple[RetrievalRequest, TopicRecord, RecoveredRequestMessage, int]:
        try:
            destination_chat_id = int(job.payload["request_chat_id"])
            destination_thread_id = int(job.payload["request_thread_id"])
            request_message_id = int(job.payload["request_message_id"])
            source_thread_id = int(job.payload["source_thread_id"])
            keywords = tuple(str(value) for value in job.payload["keywords"])
            match_mode = str(job.payload["match"])
            limit = int(job.payload["limit"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"retrieval job {job.id} has an invalid recovery payload") from error
        if (
            not keywords
            or match_mode not in {"all", "any"}
            or not 1 <= limit <= self._settings.max_request_limit
        ):
            raise ValueError(f"retrieval job {job.id} has invalid recovery parameters")
        topic = self._repositories.get(self._settings.archive_chat_id, source_thread_id)
        if topic is None or not topic.is_active:
            raise ValueError(f"retrieval job {job.id} targets an inactive source topic")
        request = RetrievalRequest(str(source_thread_id), keywords, match_mode, limit)
        message = RecoveredRequestMessage(
            bot,
            destination_chat_id,
            destination_thread_id,
            request_message_id,
        )
        return request, topic, message, destination_chat_id

    def _resolve_topic(self, reference: str) -> TopicRecord:
        topics = self._repositories.list_topics(self._settings.archive_chat_id)
        try:
            thread_id = int(reference)
        except ValueError:
            matches = [topic for topic in topics if topic.name.casefold() == reference.casefold()]
        else:
            matches = [topic for topic in topics if topic.thread_id == thread_id]
        if not matches:
            raise RequestValidationError("The requested topic is unknown or inactive.")
        if len(matches) > 1:
            raise RequestValidationError("The requested topic name is ambiguous.")
        return matches[0]


def _group_by_logical_post(
    posts: list[IndexedPostRecord],
) -> list[list[IndexedPostRecord]]:
    """Group consecutive posts that share a logical_post_key (album members)."""
    groups: list[list[IndexedPostRecord]] = []
    sentinel = object()
    current_key: object = sentinel
    for post in posts:
        if post.logical_post_key != current_key:
            current_key = post.logical_post_key
            groups.append([])
        groups[-1].append(post)
    return groups


def _parse_keywords(value: str) -> tuple[str, ...]:
    try:
        row = next(csv.reader(io.StringIO(value), skipinitialspace=True))
    except (csv.Error, StopIteration) as error:
        raise RequestValidationError("keywords are malformed.") from error
    keywords = tuple(
        dict.fromkeys(
            " ".join(item.strip().casefold().removeprefix("#").split())
            for item in row
            if item.strip().removeprefix("#")
        )
    )
    if not keywords:
        raise RequestValidationError("keywords must contain at least one value.")
    return keywords
