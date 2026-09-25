"""Local console harness — run Miki's Telegram admin commands from the terminal.

Every command the bot exposes over Telegram (``/keyword_add``, ``/status``,
``/config``, ``/dead_letters`` …) is an ``async def name(update, context)``
handler that reads ``message.text`` + ``user.id`` and answers via
``reply_text``. This module drives those *same* handlers with a synthetic
admin ``Update``/``Context`` so ``miki-ops bot <command>`` behaves exactly like
sending the command to the bot — no reimplementation, so the two can't drift.

The whole service graph is built with no network (``DeliveryExecutor`` takes no
Bot at construction); only the two commands that call the live Telegram API
(``topic_register``, ``health``) get a real ``Bot``, opened just for that call.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field

from telegram.constants import ChatType

from miki_sorter_bot.config import Settings
from miki_sorter_bot.indexing import IndexingService
from miki_sorter_bot.management import ManagementCommands
from miki_sorter_bot.operations import OperationsService
from miki_sorter_bot.periodic_notice import PeriodicNoticeService
from miki_sorter_bot.recovery import JobRecoveryService
from miki_sorter_bot.reliability import DeliveryExecutor, RateLimiter, RetryPolicy
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.retrieval import RetrievalService
from miki_sorter_bot.settings_registry import LiveSettings
from miki_sorter_bot.sorting import SortingService
from miki_sorter_bot.storage import Storage

# command name -> ManagementCommands method. Mirrors the handler table wired in
# main._add_management_handlers; kept in lock-step by test_bot_console. The
# handler code is shared, so behaviour matches the Telegram bot exactly.
COMMAND_METHODS: dict[str, str] = {
    "topic_register": "topic_register",
    "topic_list": "topic_list",
    "request_topic_add": "request_topic_add",
    "request_topic_remove": "request_topic_remove",
    "request_topic_list": "request_topic_list",
    "keyword_add": "keyword_add",
    "keyword_remove": "keyword_remove",
    "keyword_replace": "keyword_replace",
    "keyword_list": "keyword_list",
    "keyword_find": "keyword_find",
    "hashtag_add": "hashtag_add",
    "hashtag_remove": "hashtag_remove",
    "hashtag_replace": "hashtag_replace",
    "hashtag_list": "hashtag_list",
    "source_show": "source_show",
    "source_set": "source_set",
    "forward_add": "forward_add",
    "forward_remove": "forward_remove",
    "forward_list": "forward_list",
    "doctor": "doctor",
    "manager_add": "manager_add",
    "manager_remove": "manager_remove",
    "reindex": "reindex",
    "route_explain": "route_explain",
    "dead_letters": "dead_letters",
    "dead_letter_retry": "dead_letter_retry",
    "audit_log": "audit_log",
    "health": "health",
    "status": "status",
    "config": "config_show",
    "settings": "config_show",
    "set": "config_set",
    "reset": "config_reset",
    "notice_set": "notice_set",
    "notice_show": "notice_show",
    "notice_topic_add": "notice_topic_add",
    "notice_topic_remove": "notice_topic_remove",
    "maintenance": "maintenance",
    "backup": "backup",
    "burner": "burner",
}

# Commands that call the live Telegram API (context.bot). Only these open a Bot.
# dead_letter_retry re-drives the job through copy_message, so it needs a real
# Bot: without one it failed with "'NoneType' object has no attribute
# 'copy_message'" and left the job marked failed a second time.
NEEDS_BOT: frozenset[str] = frozenset({"topic_register", "health", "dead_letter_retry"})


def list_commands() -> list[str]:
    return sorted(COMMAND_METHODS)


# ── synthetic Telegram objects (only the attributes the handlers read) ───────


@dataclass(slots=True)
class _FakeMessage:
    text: str
    chat: "_FakeChat"
    message_thread_id: int | None
    _sink: list[str]
    message_id: int = 0
    forum_topic_closed: object | None = None
    forum_topic_reopened: object | None = None
    forum_topic_edited: object | None = None

    async def reply_text(self, text: str, *_args: object, **_kwargs: object) -> None:
        self._sink.append(text)


@dataclass(slots=True)
class _FakeChat:
    id: int
    type: str = ChatType.SUPERGROUP
    is_forum: bool = True


@dataclass(slots=True)
class _FakeUser:
    id: int


@dataclass(slots=True)
class _FakeUpdate:
    effective_message: _FakeMessage
    effective_chat: _FakeChat
    effective_user: _FakeUser


@dataclass(slots=True)
class _FakeContext:
    bot: object | None
    args: list[str] = field(default_factory=list)


def build_management(
    settings: Settings,
    repositories: SqliteRepositories,
    storage: Storage,
) -> ManagementCommands:
    """Construct the same service graph main._run wires for the bot. No Bot is
    needed here — delivery/API objects take one only at call time."""

    live_settings = LiveSettings(settings, repositories)
    indexing = IndexingService(settings, repositories)
    notice = PeriodicNoticeService(settings, repositories, live_settings)
    delivery_executor = DeliveryExecutor(
        retry_policy=RetryPolicy(
            attempts=settings.telegram_retry_attempts,
            base_delay=settings.telegram_retry_base_delay,
            max_delay=settings.telegram_retry_max_delay,
        ),
        rate_limiter=RateLimiter(settings.telegram_messages_per_second),
        metric=repositories.increment_metric,
    )
    sorting = SortingService(
        settings,
        repositories,
        indexing,
        delivery_executor,
        live_settings=live_settings,
        notice=notice,
    )
    retrieval = RetrievalService(
        settings, repositories, delivery_executor, live_settings=live_settings
    )
    recovery = JobRecoveryService(
        repositories,
        sorting,
        retrieval,
        batch_size=settings.job_recovery_batch_size,
    )
    operations = OperationsService(
        repositories,
        storage,
        backup_directory=settings.backup_directory,
        transient_retention_days=settings.transient_retention_days,
        audit_retention_days=settings.audit_retention_days,
    )
    return ManagementCommands(
        settings,
        repositories,
        indexing,
        sorting,
        operations,
        recovery,
        live_settings=live_settings,
        notice=notice,
    )


@dataclass(frozen=True, slots=True)
class ConsoleResult:
    ok: bool
    output: str


def run_command(
    settings: Settings,
    repositories: SqliteRepositories,
    storage: Storage,
    *,
    name: str,
    args: list[str],
    chat_id: int | None = None,
    thread_id: int | None = None,
    user_id: int | None = None,
) -> ConsoleResult:
    """Run one Telegram admin command locally and capture its reply text."""

    method_name = COMMAND_METHODS.get(name)
    if method_name is None:
        return ConsoleResult(
            False,
            f"Unknown command '{name}'. Try `miki-ops bot --list`.",
        )
    if not settings.admin_user_ids:
        return ConsoleResult(
            False,
            "No ADMIN_USER_IDS configured — every admin command would be rejected.",
        )

    management = build_management(settings, repositories, storage)
    method = getattr(management, method_name)

    actor = user_id if user_id is not None else next(iter(settings.admin_user_ids))
    # Route/topic/keyword/hashtag commands are scoped to the chat their topics
    # live in — the archive chat — so default there (override with --chat). The
    # chat-agnostic commands (status/config/source_*) ignore this.
    chat = _FakeChat(id=chat_id if chat_id is not None else settings.archive_chat_id)
    sink: list[str] = []
    # Reconstruct the command line the handler parses; shlex.join re-quotes any
    # argument that contains spaces so quoted-phrase commands (keyword_find,
    # keyword_add …) see the same tokens they would from Telegram.
    tail = (" " + shlex.join(args)) if args else ""
    message = _FakeMessage(
        text=f"/{name}{tail}",
        chat=chat,
        message_thread_id=thread_id,
        _sink=sink,
    )
    update = _FakeUpdate(message, chat, _FakeUser(actor))
    context = _FakeContext(bot=None, args=list(args))

    async def _invoke() -> None:
        if name in NEEDS_BOT:
            from telegram import Bot

            bot = Bot(settings.bot_token)
            async with bot:  # opens the HTTP client for the one live call
                context.bot = bot
                await method(update, context)
        else:
            await method(update, context)

    try:
        asyncio.run(_invoke())
    except Exception as exc:  # a handler blew up — surface it, don't traceback
        return ConsoleResult(False, f"{name} failed: {exc}")

    return ConsoleResult(True, "\n".join(sink) if sink else "(no reply)")
