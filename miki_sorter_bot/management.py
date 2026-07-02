from __future__ import annotations

import csv
import logging
import shlex

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import ContextTypes

from miki_sorter_bot.config import Settings
from miki_sorter_bot.diagnostics import run_diagnostics
from miki_sorter_bot.indexing import IndexingService
from miki_sorter_bot.operations import OperationsService
from miki_sorter_bot.periodic_notice import PeriodicNoticeService
from miki_sorter_bot.recovery import JobRecoveryService
from miki_sorter_bot.repositories import SqliteRepositories, normalize_mapping
from miki_sorter_bot.settings_registry import LiveSettings, UnknownSettingError
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
        recovery: JobRecoveryService | None = None,
        live_settings: LiveSettings | None = None,
        notice: "PeriodicNoticeService | None" = None,
    ) -> None:
        self._settings = settings
        self._repositories = repositories
        self._indexing = indexing
        self._sorting = sorting
        self._operations = operations
        self._recovery = recovery
        self._live = live_settings or LiveSettings(settings, repositories)
        self._notice = notice

    async def topic_register(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        command = await self._admin_context(update)
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
        await message.reply_text(f"Registered topic {topic.name} with ID {topic.thread_id}.")
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

    async def request_topic_add(
        self,
        update: Update,
        _: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        await self._toggle_request_topic(update, add=True)

    async def request_topic_remove(
        self,
        update: Update,
        _: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        await self._toggle_request_topic(update, add=False)

    async def request_topic_list(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, _, _ = command
        topic_ids = sorted(self._live.request_topic_ids())
        if not topic_ids:
            await message.reply_text(
                "No request topics are configured. Add one with /request_topic_add "
                "inside the topic, or /set request_topic_ids <id,...>."
            )
            return
        request_chat_id = self._live.effective_request_chat_id()
        await message.reply_text(
            f"#request retrieval is accepted in chat {request_chat_id}, topics:\n"
            + "\n".join(f"- {topic_id}" for topic_id in topic_ids)
        )

    async def _toggle_request_topic(self, update: Update, *, add: bool) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, chat, user = command
        thread_id = message.message_thread_id
        if not thread_id:
            verb = "accept" if add else "stop accepting"
            await message.reply_text(
                f"Run this command inside the forum topic you want to {verb} #request "
                "messages in (or use /set request_topic_ids <id,...> from anywhere)."
            )
            return
        request_chat_id = self._live.effective_request_chat_id()
        if chat.id != request_chat_id:
            await message.reply_text(
                f"Retrieval requests are only accepted in chat {request_chat_id}. "
                "Run this in that chat, or change it with /set request_chat_id "
                f"{chat.id} first."
            )
            return
        current = self._live.request_topic_ids()
        updated = current | {thread_id} if add else current - {thread_id}
        if updated == current:
            state = "already" if add else "not"
            await message.reply_text(f"Topic {thread_id} is {state} a request topic.")
            return
        rendered = ", ".join(str(topic_id) for topic_id in sorted(updated))
        self._live.registry.set(
            "request_topic_ids", rendered, self._live.settings, self._live.store, user.id
        )
        action = "added to" if add else "removed from"
        await message.reply_text(
            f"Topic {thread_id} {action} request topics (effective immediately). "
            f"Now: {rendered or '(none)'}."
        )
        self._audit(
            user.id,
            "request_topic.add" if add else "request_topic.remove",
            "runtime_setting",
            "request_topic_ids",
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

    async def source_show(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, _, _ = command
        override = self._repositories.get_runtime_setting("source_thread_id")
        if override is not None:
            await message.reply_text(
                f"Listening to source topic {override} (runtime override; "
                f".env default is {self._settings.source_thread_id})."
            )
        else:
            await message.reply_text(
                f"Listening to source topic {self._settings.source_thread_id} "
                "(from .env; no runtime override set)."
            )

    async def source_set(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        target = _single_integer_argument(message.text or "")
        if target is None or target <= 0:
            await message.reply_text("Usage: /source_set <source topic ID (positive integer)>")
            return
        self._repositories.set_runtime_setting("source_thread_id", str(target), user.id)
        await message.reply_text(
            f"Now listening to source topic {target} (effective immediately, no restart)."
        )
        self._audit(user.id, "source.set", "runtime_setting", str(target))

    async def forward_add(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        parsed = _two_integer_arguments(message.text or "")
        if parsed is None:
            await message.reply_text("Usage: /forward_add <source topic ID> <destination topic ID>")
            return
        source_thread_id, destination_thread_id = parsed
        try:
            self._repositories.add_forwarding_pair(
                source_thread_id,
                destination_thread_id,
                user.id,
            )
        except ValueError as error:
            await message.reply_text(str(error))
            return
        await message.reply_text(
            f"Forwarding source topic {source_thread_id} -> "
            f"destination topic {destination_thread_id} (effective immediately)."
        )
        self._audit(
            user.id,
            "forwarding.add",
            "forwarding_pair",
            f"{source_thread_id}->{destination_thread_id}",
        )

    async def forward_remove(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        target = _single_integer_argument(message.text or "")
        if target is None or target <= 0:
            await message.reply_text("Usage: /forward_remove <source topic ID>")
            return
        removed = self._repositories.remove_forwarding_pair(target)
        await message.reply_text(
            f"Forwarding for source topic {target} removed."
            if removed
            else f"No forwarding pair was configured for source topic {target}."
        )
        self._audit(
            user.id,
            "forwarding.remove",
            "forwarding_pair",
            str(target),
            "success" if removed else "denied",
        )

    async def forward_list(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, _, _ = command
        pairs = self._repositories.list_forwarding_pairs()
        if not pairs:
            await message.reply_text("No forwarding pairs are configured.")
            return
        await message.reply_text(
            "Forwarding pairs (source -> destination):\n"
            + "\n".join(
                f"- {pair.source_thread_id} -> {pair.destination_thread_id}" for pair in pairs
            )
        )

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
            f"Admin {target} {'added' if created else 'was already authorized'} "
            "(limited tier: keywords/hashtags and diagnostics, all chats, "
            "effective immediately)."
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
            topic = decision.topic
            if topic is None:
                await message.reply_text("The matched route has no active destination.")
                return
            await message.reply_text(
                f"Routes to {topic.name} ({topic.thread_id}) because {decision.reason}."
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
                f"- {row['id']}: {row['operation']} / {row['error_category']} (job {row['job_id']})"
                for row in rows
            )
        )

    async def dead_letter_retry(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        dead_letter_id = _single_integer_argument(message.text or "")
        if dead_letter_id is None:
            await message.reply_text("Usage: /dead_letter_retry <dead letter ID>")
            return
        job_id = self._repositories.retry_dead_letter(dead_letter_id)
        retried = job_id is not None
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
        if job_id is not None and self._recovery is not None:
            coroutine = self._recovery.resume_job(job_id, context)
            application = getattr(context, "application", None)
            if application is not None:
                application.create_task(coroutine, update=update)
            else:
                await coroutine

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
        command = await self._authorized_context(update)
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

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._authorized_context(update)
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
        body = (
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
        await message.reply_text(body + self._webhook_section(context))

    async def doctor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, _, _ = command
        report = run_diagnostics(self._settings, self._repositories).format()
        await message.reply_text(report + self._webhook_section(context))

    async def burner(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Drive the burner from Telegram: `/burner status` or enqueue a command."""

        command = await self._burner_context(update)
        if command is None:
            return
        message, chat, user = command

        from miki_sorter_bot.burner import (
            ENQUEUEABLE_COMMAND_KINDS,
            BurnerCapability,
        )

        parts = (message.text or "").split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else "status"
        tail = parts[2].strip() if len(parts) > 2 else ""

        availability = BurnerCapability(self._settings, self._repositories).evaluate()

        if sub == "status":
            await message.reply_text(_format_burner_status(availability, self._repositories))
            self._audit(user.id, "burner.status", "burner", "status")
            return

        if sub not in ENQUEUEABLE_COMMAND_KINDS:
            allowed = ", ".join(sorted({"status", *ENQUEUEABLE_COMMAND_KINDS}))
            await message.reply_text(f"Unknown burner command '{sub}'. Try: {allowed}")
            return

        # Fail-fast: never queue against an unavailable burner.
        if not availability.available:
            await message.reply_text(
                f"Burner unavailable ({availability.reason}); '{sub}' was not queued."
            )
            self._audit(user.id, "burner.enqueue", "burner_command", sub, outcome="denied")
            return

        payload: dict[str, object] = {
            "echo": tail,
            "chat_id": chat.id,
            "thread_id": message.message_thread_id,
            "requested_by": user.id,
        }
        key = f"burner:{sub}:{user.id}:{message.message_id}"
        record = self._repositories.enqueue_burner_command(sub, key, payload, user.id)
        await message.reply_text(
            f"Queued burner command '{sub}' (#{record.id}). "
            "You'll get the result here when it finishes."
        )
        self._audit(user.id, "burner.enqueue", "burner_command", str(record.id))

    async def _burner_context(self, update: Update):
        """Gate burner commands to operators or super admins."""

        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if message is None or chat is None or user is None:
            return None
        if user.id not in self._settings.burner_operator_or_admin_ids:
            await message.reply_text("You are not authorized to drive Miki's burner.")
            return None
        return message, chat, user

    @staticmethod
    def _webhook_section(context: ContextTypes.DEFAULT_TYPE) -> str:
        application = getattr(context, "application", None)
        bot_data = getattr(application, "bot_data", None) or {}
        supervisor = bot_data.get("webhook_supervisor")
        if supervisor is None:
            return ""
        snapshot = supervisor.snapshot()
        if not snapshot.enabled:
            return ""
        return "\n\nWebhook supervision:\n" + "\n".join(snapshot.summary_lines())

    async def config_show(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, _, _ = command
        views = self._live.registry.describe(self._live.settings, self._live.store)
        lines = ["Runtime settings — change with /set <key> <value>, /reset <key>:"]
        category = None
        for view in views:
            if view.category != category:
                category = view.category
                lines.append(f"\n[{category}]")
            marker = "*" if view.overridden else " "
            suffix = f"  (default {view.default})" if view.overridden else ""
            lines.append(f"{marker} {view.key} = {view.value}{suffix}")
        lines.append("\n* = overridden at runtime. Send /config for this list anytime.")
        await message.reply_text("\n".join(lines))

    async def config_set(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3:
            await message.reply_text("Usage: /set <key> <value>  (see /config for keys)")
            return
        key, raw = parts[1], parts[2]
        try:
            value = self._live.registry.set(
                key, raw, self._live.settings, self._live.store, user.id
            )
        except UnknownSettingError:
            await message.reply_text(f"Unknown setting '{key}'. Send /config to list settings.")
            return
        except ValueError as error:
            await message.reply_text(f"Invalid value for {key}: {error}")
            return
        rendered = self._live.registry.require(key).render(value)
        await message.reply_text(f"{key} = {rendered} (effective immediately, no restart).")
        self._audit(user.id, "config.set", "runtime_setting", key)

    async def config_reset(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("Usage: /reset <key>  (see /config for keys)")
            return
        key = parts[1].strip()
        try:
            spec = self._live.registry.require(key)
            removed = self._live.registry.reset(key, self._live.store)
        except UnknownSettingError:
            await message.reply_text(f"Unknown setting '{key}'. Send /config to list settings.")
            return
        default = spec.render(spec.default(self._live.settings))
        if removed:
            await message.reply_text(f"{key} reset to default {default}.")
        else:
            await message.reply_text(f"{key} was already at its default ({default}).")
        self._audit(user.id, "config.reset", "runtime_setting", key)

    async def notice_set(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        if self._notice is None:
            await message.reply_text("Periodic notices are unavailable.")
            return
        raw = message.text or ""
        parts = raw.split(maxsplit=1)
        body = parts[1].strip() if len(parts) > 1 else ""
        if not body:
            await message.reply_text(
                "Usage: /notice_set <message text>\n"
                "Everything after the command (including line breaks) becomes the "
                "notice. Enable it with /set periodic_notice_enabled true."
            )
            return
        self._notice.set_text(body, user.id)
        await message.reply_text(
            "Notice text updated. It reposts after "
            f"{self._live.notice_media_threshold()} media or every "
            f"{self._live.notice_interval_minutes()} min, whichever comes first.\n\n"
            f"Preview:\n{body}"
        )
        self._audit(user.id, "notice.set", "runtime_setting", "periodic_notice_text")

    async def notice_show(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, _, _ = command
        if self._notice is None:
            await message.reply_text("Periodic notices are unavailable.")
            return
        text = self._notice.get_text()
        topics = ", ".join(str(t) for t in sorted(self._live.notice_topics())) or "(none)"
        state = "on" if self._live.notice_enabled() else "off"
        threshold = self._live.notice_media_threshold()
        interval = self._live.notice_interval_minutes()
        count_rule = f"every {threshold} media" if threshold > 0 else "count trigger off"
        interval_rule = f"every {interval} min" if interval > 0 else "interval trigger off"
        await message.reply_text(
            f"Periodic notice: {state}\n"
            f"Triggers: {count_rule}; {interval_rule} (whichever first, only on new media)\n"
            f"Topics: {topics}\n"
            f"Text:\n{text or '(not set — use /notice_set)'}"
        )

    async def notice_topic_add(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._toggle_notice_topic(update, add=True)

    async def notice_topic_remove(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._toggle_notice_topic(update, add=False)

    async def _toggle_notice_topic(self, update: Update, *, add: bool) -> None:
        command = await self._admin_context(update)
        if command is None:
            return
        message, _, user = command
        thread_id = message.message_thread_id
        if not thread_id:
            verb = "post notices in" if add else "stop posting notices in"
            await message.reply_text(
                f"Run this command inside the forum topic you want Miki to {verb} "
                "(or use /set periodic_notice_topics <id,...> from anywhere)."
            )
            return
        current = self._live.notice_topics()
        updated = current | {thread_id} if add else current - {thread_id}
        if updated == current:
            state = "already" if add else "not"
            await message.reply_text(f"Topic {thread_id} is {state} a notice topic.")
            return
        rendered = ", ".join(str(topic_id) for topic_id in sorted(updated))
        self._live.registry.set(
            "periodic_notice_topics", rendered, self._live.settings, self._live.store, user.id
        )
        action = "added to" if add else "removed from"
        await message.reply_text(
            f"Topic {thread_id} {action} notice topics (effective immediately). "
            f"Now: {rendered or '(none)'}."
        )
        self._audit(
            user.id,
            "notice_topic.add" if add else "notice_topic.remove",
            "runtime_setting",
            "periodic_notice_topics",
        )

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
        parsed = _target_and_values(message.text or "", requested_kind)
        if parsed is None:
            value_label = (
                "comma- or space-separated hashtags"
                if requested_kind == "hashtag"
                else "comma-separated keywords or quoted phrases"
            )
            await message.reply_text(f"Usage: /{requested_kind}_add <topic ID> <{value_label}>")
            return
        thread_id, values = parsed
        added = []
        errors = []
        for value in values:
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
                errors.append(f"{value}: {error}")
                continue
            added.append(mapping)
            self._audit(user.id, "route.add", "route_mapping", str(mapping.id))
        if added:
            labels = ", ".join(f"{mapping.kind} '{mapping.value}'" for mapping in added)
            reply = f"Added {len(added)} route(s) to topic {thread_id}: {labels}."
        else:
            reply = f"No routes were added to topic {thread_id}."
        if errors:
            reply += "\nSkipped:\n" + "\n".join(f"- {error}" for error in errors)
        await message.reply_text(reply)

    async def _remove_mapping(self, update: Update, requested_kind: str) -> None:
        command = await self._authorized_context(update)
        if command is None:
            return
        message, chat, user = command
        parsed = _target_and_value(message.text or "")
        if parsed is None:
            await message.reply_text(f"Usage: /{requested_kind}_remove <topic ID> <value>")
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
            await message.reply_text(f"Usage: /{requested_kind}_replace <topic ID> <value>")
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
        await message.reply_text(f"Moved {mapping.kind} '{mapping.value}' to topic {thread_id}.")
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
        """Gate super-admin-only commands (operationally critical / destructive)."""

        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if message is None or chat is None or user is None:
            return None
        if not self._is_super_admin(user.id):
            await message.reply_text("Only a Miki super administrator can do that.")
            return None
        return message, chat, user

    def _is_super_admin(self, user_id: int) -> bool:
        """Super admins are the ``ADMIN_USER_IDS`` roster from ``.env``.

        This roster is the top tier with full authority and, being file-based,
        can never be locked out by a runtime change.
        """

        return user_id in self._settings.admin_user_ids

    def _is_admin(self, user_id: int) -> bool:
        """Super admins plus limited admins granted at runtime via /manager_add.

        Limited admins (route managers) may manage keywords/hashtags and view
        diagnostics, but not the super-admin-only commands. Both tiers are
        effective immediately, without a restart.
        """

        return self._is_super_admin(user_id) or self._repositories.is_manager(user_id)

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


def _format_burner_status(availability, repositories: SqliteRepositories) -> str:
    lines = [availability.summary()]
    if availability.version is not None:
        lines.append(f"version: {availability.version}")
    if availability.last_error:
        lines.append(f"last error: {availability.last_error}")
    pending = len(repositories.list_pending_burner_commands(limit=1000))
    if pending:
        lines.append(f"pending commands: {pending}")
    return "\n".join(lines)


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


def _target_and_values(text: str, requested_kind: str) -> tuple[int, tuple[str, ...]] | None:
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        return None
    try:
        thread_id = int(parts[1])
    except (ValueError, TypeError):
        return None
    if requested_kind == "hashtag" and "," not in parts[2]:
        try:
            row = shlex.split(parts[2])
        except ValueError:
            return None
    else:
        try:
            row = next(csv.reader([parts[2]], skipinitialspace=True))
        except (csv.Error, StopIteration):
            return None
    values = tuple(dict.fromkeys(value.strip() for value in row if value.strip()))
    if thread_id <= 0 or not values:
        return None
    return thread_id, values


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


def _two_integer_arguments(text: str) -> tuple[int, int] | None:
    parts = text.split()
    if len(parts) != 3:
        return None
    try:
        first = int(parts[1])
        second = int(parts[2])
    except ValueError:
        return None
    return first, second


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
