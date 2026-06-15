from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass

from telegram import Update
from telegram.ext import ContextTypes

from miki_sorter_bot.config import Settings
from miki_sorter_bot.repositories import IndexedPostRecord, SqliteRepositories, TopicRecord
from miki_sorter_bot.reliability import DeliveryExecutor, RateLimiter, RetryPolicy, classify_error

LOGGER = logging.getLogger(__name__)


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
    cancelled: bool = False

    def text(self, job_id: int) -> str:
        state = "cancelled" if self.cancelled else "completed"
        return (
            f"Request {job_id} {state}: {self.matched} matched, "
            f"{self.copied} copied, {self.unavailable} unavailable, "
            f"{self.skipped} skipped, {self.failed} failed."
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
    ) -> None:
        self._settings = settings
        self._repositories = repositories
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
            chat.id != self._settings.effective_request_chat_id
            or message.message_thread_id not in self._settings.request_topic_ids
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
        request_message: object,
        destination_chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        job = self._repositories.get_job(job_id)
        if job is None or job.status == "cancelled":
            return
        self._repositories.update_job(job_id, "running")
        posts = self._repositories.search_posts(
            self._settings.archive_chat_id,
            topic.thread_id,
            request.keywords,
            request.match_mode,
            request.limit,
        )
        summary = RetrievalSummary(matched=len({post.logical_post_key for post in posts}))
        for post in posts:
            current = self._repositories.get_job(job_id)
            if current is None or current.status == "cancelled":
                summary.cancelled = True
                break
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
                self._repositories.update_retrieval_item(item.id, "skipped", error="unavailable")
                summary.unavailable += 1
                continue
            try:
                copied = await self._delivery_executor.run(
                    lambda: context.bot.copy_message(
                        chat_id=destination_chat_id,
                        from_chat_id=post.source_chat_id,
                        message_id=post.source_message_id,
                        message_thread_id=request_message.message_thread_id,
                    )
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
                    self._repositories.update_retrieval_item(item.id, "failed", error=str(error))
                    summary.failed += 1
                    self._repositories.add_dead_letter(
                        job_id,
                        "retrieve_copy",
                        {"post_id": post.id, "request": job.payload},
                        failure.category,
                        str(error),
                    )
                LOGGER.warning(
                    "Retrieval copy failed",
                    extra={"job_id": job_id, "post_id": post.id},
                )
                continue
            self._repositories.update_retrieval_item(
                item.id,
                "sent",
                destination_message_id=copied.message_id,
            )
            summary.copied += 1
        if summary.copied:
            self._repositories.increment_metric("retrieval_items_copied", summary.copied)
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
