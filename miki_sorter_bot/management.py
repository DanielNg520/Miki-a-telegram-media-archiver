from __future__ import annotations

import logging
import shlex

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import ContextTypes

from miki_sorter_bot.config import Settings
from miki_sorter_bot.diagnostics import run_diagnostics
from miki_sorter_bot.indexing import IndexingService
from miki_sorter_bot.operations import OperationsService
from miki_sorter_bot.repositories import SqliteRepositories, normalize_mapping
from miki_sorter_bot.sorting import SortingService

LOGGER = logging.getLogger(__name__)


class ManagementCommands:
    def __init__(
        self,
        settings: Settings,
        repositories: SqliteRepositories,
        indexing: IndexingService | None = None,
        sorting: SortingService | None = None,
        operations: OperationsService | None = None,
    ) -> None:
        self._settings = settings
        self._repositories = repositories
        self._indexing = indexing
        self._sorting = sorting
        self._operations = operations

    async def topic_register(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, chat, user = command
        if chat.type != ChatType.SUPERGROUP or not message.message_thread_id:
            await message.reply_text("Run this command inside a supergroup forum topic.")
            return
        live_chat = await context.bot.get_chat(chat.id)
        if not live_chat.is_forum:
            await message.reply_text("This supergroup does not have forum topics enabled.")
            return
        bot_user = await context.bot.get_me()
        membership = await context.bot.get_chat_member(chat.id, bot_user.id)
        if membership.status != ChatMemberStatus.ADMINISTRATOR:
            await message.reply_text(
                "Miki must be an administrator in this forum before registering topics."
            )
            return
        name = _command_tail(message.text or "")
        if not name:
            await message.reply_text("Usage: /topic_register <unique topic name>")
            return
        try:
            topic = self._repositories.register_topic(
                chat.id,
                message.message_thread_id,
                name,
            )
        except ValueError as error:
            await message.reply_text(str(error))
            return
        await message.reply_text(
            f"Registered topic {topic.name} with ID {topic.thread_id}."
        )
        self._audit(user.id, "topic.register", "topic", str(topic.id))

    async def topic_list(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, chat, _ = command
        topics = self._repositories.list_topics(chat.id)
        if not topics:
            await message.reply_text("No active topics are registered.")
            return
        await message.reply_text(
            "Registered topics:\n"
            + "\n".join(f"- {topic.thread_id}: {topic.name}" for topic in topics)
        )

    async def keyword_add(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._add_mapping(update, "keyword")

    async def keyword_remove(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._remove_mapping(update, "keyword")

    async def keyword_replace(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._replace_mapping(update, "keyword")

    async def keyword_list(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._list_mappings(update, None)

    async def keyword_find(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, chat, _ = command
        value = _command_tail(message.text or "")
        if not value:
            await message.reply_text("Usage: /keyword_find <keyword or quoted phrase>")
            return
        kind = _keyword_kind(value)
        try:
            result = self._repositories.find_mapping(chat.id, kind, value)
        except ValueError as error:
            await message.reply_text(str(error))
            return
        if result is None:
            await message.reply_text("No matching route is configured.")
            return
        mapping, topic = result
        await message.reply_text(
            f"{mapping.kind} '{mapping.value}' routes to {topic.name} ({topic.thread_id})."
        )

    async def hashtag_add(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._add_mapping(update, "hashtag")

    async def hashtag_remove(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._remove_mapping(update, "hashtag")

    async def hashtag_replace(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._replace_mapping(update, "hashtag")

    async def hashtag_list(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._list_mappings(update, "hashtag")

    async def manager_add(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, chat, user = command
        target = _single_integer_argument(message.text or "")
        if target is None:
            await message.reply_text("Usage: /manager_add <Telegram user ID>")
            return
        created = self._repositories.grant_route_manager(chat.id, target, user.id)
        await message.reply_text(
            f"Manager {target} {'added' if created else 'was already authorized'} "
            "(full access, all chats, effective immediately)."
        )
        self._audit(user.id, "manager.add", "telegram_user", str(target))

    async def manager_remove(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, chat, user = command
        target = _single_integer_argument(message.text or "")
        if target is None:
            await message.reply_text("Usage: /manager_remove <Telegram user ID>")
            return
        removed = self._repositories.revoke_manager(target)
        await message.reply_text(
            f"Manager {target} {'removed' if removed else 'was not authorized'}."
        )
        self._audit(
            user.id,
            "manager.remove",
            "telegram_user",
            str(target),
            "success" if removed else "denied",
        )

    async def reindex(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        if self._indexing is None:
            await message.reply_text("Indexing service is unavailable.")
            return
        limit = _optional_integer_argument(message.text or "")
        if limit is False:
            await message.reply_text("Usage: /reindex [batch size from 1 to 1000]")
            return
        batch_size = limit or 100
        try:
            processed, last_id = self._indexing.reindex(limit=batch_size)
        except ValueError as error:
            await message.reply_text(str(error))
            return
        suffix = f" Last post ID: {last_id}." if last_id is not None else ""
        await message.reply_text(f"Reindexed {processed} posts.{suffix}")

    async def route_explain(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, _, user = command
        if self._sorting is None:
            await message.reply_text("Sorting service is unavailable.")
            return
        text = _command_tail(message.text or "")
        if not text:
            await message.reply_text("Usage: /route_explain <caption text>")
            return
        decision = self._sorting.explain(text)
        if decision.status == "unmatched":
            await message.reply_text("No configured route matched.")
        elif decision.status == "conflict":
            destinations = sorted(
                {f"{match.topic.name} ({match.topic.thread_id})" for match in decision.matches}
            )
            await message.reply_text("Conflict between: " + ", ".join(destinations))
        else:
            await message.reply_text(
                f"Routes to {decision.topic.name} ({decision.topic.thread_id}) "
                f"because {decision.reason}."
            )

    async def dead_letters(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, _ = command
        rows = self._repositories.list_dead_letters()
        if not rows:
            await message.reply_text("No unresolved dead letters.")
            return
        await message.reply_text(
            "Unresolved dead letters:\n"
            + "\n".join(
                f"- {row['id']}: {row['operation']} / {row['error_category']} "
                f"(job {row['job_id']})"
                for row in rows
            )
        )

    async def dead_letter_retry(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        dead_letter_id = _single_integer_argument(message.text or "")
        if dead_letter_id is None:
            await message.reply_text("Usage: /dead_letter_retry <dead letter ID>")
            return
        retried = self._repositories.retry_dead_letter(dead_letter_id)
        await message.reply_text(
            "Dead letter requeued." if retried else "Dead letter could not be requeued."
        )
        self._audit(
            user.id,
            "dead_letter.retry",
            "dead_letter",
            str(dead_letter_id),
            "success" if retried else "denied",
        )

    async def audit_log(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, _ = command
        limit = _optional_integer_argument(message.text or "")
        if limit is False:
            await message.reply_text("Usage: /audit_log [limit from 1 to 100]")
            return
        requested = limit or 20
        if not 1 <= requested <= 100:
            await message.reply_text("Audit limit must be between 1 and 100.")
            return
        events = self._repositories.list_audit_events(requested)
        if not events:
            await message.reply_text("No audit events recorded.")
            return
        await message.reply_text(
            "Recent audit events:\n"
            + "\n".join(
                f"- {event['id']}: {event['actor_type']}:{event['actor_id']} "
                f"{event['action']} -> {event['outcome']}"
                for event in events
            )
        )

    async def health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, _ = command
        if self._operations is None:
            await message.reply_text("Operations service is unavailable.")
            return
        report = await self._operations.health(context.bot)
        await message.reply_text(
            f"Miki is {'healthy' if report.healthy else 'degraded'}.\n"
            f"Database: {'ok' if report.database_ok else 'failed'}\n"
            f"Telegram: {'ok' if report.telegram_ok else 'failed'}"
        )

    async def status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, _ = command
        if self._operations is None:
            await message.reply_text("Operations service is unavailable.")
            return
        status = self._operations.status()
        jobs = ", ".join(f"{key}={value}" for key, value in status["jobs"].items()) or "none"
        metrics = status["metrics"]
        operations = metrics.get("telegram_delivery_operations", 0)
        duration = metrics.get("telegram_delivery_duration_ms", 0)
        average_ms = round(duration / operations) if operations else 0
        await message.reply_text(
            "Operational status:\n"
            f"- database: {status['database']}\n"
            f"- available posts: {status['posts']}\n"
            f"- unavailable posts: {status['unavailable_posts']}\n"
            f"- unresolved dead letters: {status['unresolved_dead_letters']}\n"
            f"- jobs: {jobs}\n"
            f"- Telegram retries: {metrics.get('telegram_retries', 0)}\n"
            f"- Telegram throttles: {metrics.get('telegram_throttles', 0)}\n"
            f"- average delivery time: {average_ms} ms"
        )

    async def doctor(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, _ = command
        await message.reply_text(run_diagnostics(self._settings, self._repositories).format())

    async def maintenance(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        if self._operations is None:
            await message.reply_text("Operations service is unavailable.")
            return
        deleted = self._operations.maintain()
        await message.reply_text(
            "Maintenance complete: "
            + ", ".join(f"{name}={count}" for name, count in deleted.items())
        )
        self._audit(user.id, "operations.maintenance", "database", "primary")

    async def backup(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        if self._operations is None:
            await message.reply_text("Operations service is unavailable.")
            return
        destination = self._operations.backup()
        await message.reply_text(f"Verified backup created: {destination.name}")
        self._audit(user.id, "operations.backup", "database_backup", destination.name)

    async def track_topic_status(
        self,
        update: Update,
        _: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or not message.message_thread_id:
            return
        if message.forum_topic_closed:
            self._repositories.update_topic_state(
                chat.id,
                message.message_thread_id,
                is_active=False,
            )
        elif message.forum_topic_reopened:
            self._repositories.update_topic_state(
                chat.id,
                message.message_thread_id,
                is_active=True,
            )
        elif message.forum_topic_edited and message.forum_topic_edited.name:
            try:
                self._repositories.update_topic_state(
                    chat.id,
                    message.message_thread_id,
                    name=message.forum_topic_edited.name,
                )
            except ValueError:
                LOGGER.warning(
                    "Could not update registered topic name",
                    extra={
                        "chat_id": chat.id,
                        "thread_id": message.message_thread_id,
                    },
                )

    async def _add_mapping(self, update: Update, requested_kind: str) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, chat, user = command
        parsed = _target_and_value(message.text or "")
        if parsed is None:
            await message.reply_text(
                f"Usage: /{requested_kind}_add <topic ID> "
                f"<{requested_kind if requested_kind == 'hashtag' else 'keyword or quoted phrase'}>"
            )
            return
        thread_id, value = parsed
        kind = requested_kind if requested_kind == "hashtag" else _keyword_kind(value)
        try:
            mapping = self._repositories.add_mapping(
                chat.id,
                thread_id,
                kind,
                value,
                user.id,
            )
        except ValueError as error:
            await message.reply_text(str(error))
            return
        await message.reply_text(
            f"Added {mapping.kind} '{mapping.value}' to topic {thread_id}."
        )
        self._audit(user.id, "route.add", "route_mapping", str(mapping.id))

    async def _remove_mapping(self, update: Update, requested_kind: str) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, chat, user = command
        parsed = _target_and_value(message.text or "")
        if parsed is None:
            await message.reply_text(
                f"Usage: /{requested_kind}_remove <topic ID> <value>"
            )
            return
        thread_id, value = parsed
        kind = requested_kind if requested_kind == "hashtag" else _keyword_kind(value)
        try:
            removed = self._repositories.remove_mapping(chat.id, thread_id, kind, value)
        except ValueError as error:
            await message.reply_text(str(error))
            return
        await message.reply_text("Mapping removed." if removed else "Mapping was not found.")
        self._audit(
            user.id,
            "route.remove",
            "topic",
            str(thread_id),
            "success" if removed else "denied",
        )

    async def _replace_mapping(self, update: Update, requested_kind: str) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, chat, user = command
        parsed = _target_and_value(message.text or "")
        if parsed is None:
            await message.reply_text(
                f"Usage: /{requested_kind}_replace <topic ID> <value>"
            )
            return
        thread_id, value = parsed
        kind = requested_kind if requested_kind == "hashtag" else _keyword_kind(value)
        try:
            mapping = self._repositories.replace_mapping(
                chat.id,
                thread_id,
                kind,
                value,
                user.id,
            )
        except ValueError as error:
            await message.reply_text(str(error))
            return
        await message.reply_text(
            f"Moved {mapping.kind} '{mapping.value}' to topic {thread_id}."
        )
        self._audit(user.id, "route.replace", "route_mapping", str(mapping.id))

    async def _list_mappings(self, update: Update, kind: str | None) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, chat, _ = command
        thread_id = _optional_integer_argument(message.text or "")
        if thread_id is False:
            await message.reply_text("Optional topic ID must be an integer.")
            return
        mappings = self._repositories.list_mappings(
            chat.id,
            thread_id=thread_id,
            kind=kind,
        )
        if not mappings:
            await message.reply_text("No matching routes are configured.")
            return
        topic_names = {topic.id: topic for topic in self._repositories.list_topics(chat.id)}
        lines = []
        for mapping in mappings:
            topic = topic_names.get(mapping.topic_id)
            destination = (
                f"{topic.name} ({topic.thread_id})" if topic else f"topic record {mapping.topic_id}"
            )
            lines.append(f"- {mapping.kind} '{mapping.value}' -> {destination}")
        await message.reply_text("Configured routes:\n" + "\n".join(lines))

    async def _authorized_context(self, update: Update):
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if message is None or chat is None or user is None:
            return None
        if not self._is_admin(user.id):
            await message.reply_text("You are not authorized to manage Miki's routes.")
            return None
        return message, chat, user

    async def _admin_context(self, update: Update):
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if message is None or chat is None or user is None:
            return None
        if not self._is_admin(user.id):
            await message.reply_text("Only a configured Miki administrator can do that.")
            return None
        return message, chat, user

    def _is_admin(self, user_id: int) -> bool:
        """Configured admins and users granted via /manager_add are equivalent.

        Managers are checked across all chats so the grant is universal, and they
        share full admin powers — both effective immediately, without a restart.
        """

        return user_id in self._settings.admin_user_ids or self._repositories.is_manager(user_id)

    def _audit(
        self,
        user_id: int,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str = "success",
    ) -> None:
        self._repositories.add_audit_event(
            actor_type="telegram_user",
            actor_id=str(user_id),
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
        )


def _command_tail(text: str) -> str:
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    tail = parts[1].strip()
    try:
        parsed = shlex.split(tail)
    except ValueError:
        return ""
    return " ".join(parsed)


def _target_and_value(text: str) -> tuple[int, str] | None:
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        return None
    try:
        thread_id = int(parts[1])
        values = shlex.split(parts[2])
    except (ValueError, TypeError):
        return None
    value = " ".join(values)
    if thread_id <= 0 or not value:
        return None
    return thread_id, value


def _keyword_kind(value: str) -> str:
    _, normalized = normalize_mapping(
        "phrase" if len(value.split()) > 1 else "keyword",
        value,
    )
    return "phrase" if " " in normalized else "keyword"


def _single_integer_argument(text: str) -> int | None:
    parts = text.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _optional_integer_argument(text: str) -> int | None | bool:
    parts = text.split()
    if len(parts) == 1:
        return None
    if len(parts) != 2:
        return False
    try:
        return int(parts[1])
    except ValueError:
        return False
