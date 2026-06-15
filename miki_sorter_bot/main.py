from __future__ import annotations

import logging

from pydantic import ValidationError
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from miki_sorter_bot.config import get_settings
from miki_sorter_bot.logging_config import (
    configure_logging,
    reset_correlation_id,
    set_correlation_id,
)
from miki_sorter_bot.indexing import IndexingService
from miki_sorter_bot.integrations import IntegrationService
from miki_sorter_bot.management import ManagementCommands
from miki_sorter_bot.operations import OperationsService
from miki_sorter_bot.retrieval import RetrievalService
from miki_sorter_bot.reliability import DeliveryExecutor, RateLimiter, RetryPolicy
from miki_sorter_bot.show_ids import show_ids
from miki_sorter_bot.sorting import SortingService
from miki_sorter_bot.storage import Storage

LOGGER = logging.getLogger(__name__)


async def sort_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    update_key = getattr(update, "update_id", None)
    if update_key is None and message is not None:
        update_key = f"message-{message.message_id}"
    token = set_correlation_id(f"telegram-{update_key or 'unknown'}")
    try:
        sorting = context.bot_data.get("sorting")
        if sorting is not None:
            await sorting.handle_update(update, context)
    finally:
        reset_correlation_id(token)


async def retrieve_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    update_key = getattr(update, "update_id", None)
    if update_key is None and message is not None:
        update_key = f"message-{message.message_id}"
    token = set_correlation_id(f"telegram-{update_key or 'unknown'}")
    try:
        retrieval = context.bot_data.get("retrieval")
        if retrieval is not None:
            await retrieval.handle_update(update, context)
    finally:
        reset_correlation_id(token)


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as error:
        messages = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )
        raise SystemExit(f"Invalid bot configuration: {messages}") from error

    configure_logging(settings.log_level, settings.log_format)
    storage = Storage(settings.database_path)
    repositories = storage.open()
    recovered_by_kind = repositories.recover_interrupted_jobs_by_kind()
    recovered = sum(recovered_by_kind.values())
    if recovered:
        LOGGER.warning(
            "Recovered interrupted jobs",
            extra={"count": recovered, "jobs_by_kind": recovered_by_kind},
        )
    delivery_executor = DeliveryExecutor(
        retry_policy=RetryPolicy(
            attempts=settings.telegram_retry_attempts,
            base_delay=settings.telegram_retry_base_delay,
            max_delay=settings.telegram_retry_max_delay,
        ),
        rate_limiter=RateLimiter(settings.telegram_messages_per_second),
        metric=repositories.increment_metric,
    )
    indexing = IndexingService(settings, repositories)
    sorting = SortingService(settings, repositories, indexing, delivery_executor)
    retrieval = RetrievalService(settings, repositories, delivery_executor)
    integrations = IntegrationService(settings, repositories, sorting)
    operations = OperationsService(
        repositories,
        storage,
        backup_directory=settings.backup_directory,
        transient_retention_days=settings.transient_retention_days,
        audit_retention_days=settings.audit_retention_days,
    )
    management = ManagementCommands(
        settings,
        repositories,
        indexing,
        sorting,
        operations,
    )

    async def close_storage(_: Application) -> None:
        storage.close()

    application = (
        Application.builder()
        .token(settings.bot_token)
        .post_shutdown(close_storage)
        .build()
    )
    application.bot_data["indexing"] = indexing
    application.bot_data["sorting"] = sorting
    application.bot_data["retrieval"] = retrieval
    application.bot_data["integrations"] = integrations
    application.bot_data["operations"] = operations
    application.bot_data["settings"] = settings
    _add_management_handlers(application, management)
    application.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, sort_message))
    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.StatusUpdate.ALL,
            indexing.handle_update,
        ),
        group=1,
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, retrieve_message),
        group=2,
    )
    application.add_handler(
        CommandHandler("request_cancel", retrieval.cancel),
        group=2,
    )

    LOGGER.info("Miki sorter is running.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


def _add_management_handlers(
    application: Application,
    management: ManagementCommands,
) -> None:
    handlers = {
        "topic_register": management.topic_register,
        "topic_list": management.topic_list,
        "keyword_add": management.keyword_add,
        "keyword_remove": management.keyword_remove,
        "keyword_replace": management.keyword_replace,
        "keyword_list": management.keyword_list,
        "keyword_find": management.keyword_find,
        "hashtag_add": management.hashtag_add,
        "hashtag_remove": management.hashtag_remove,
        "hashtag_replace": management.hashtag_replace,
        "hashtag_list": management.hashtag_list,
        "manager_add": management.manager_add,
        "manager_remove": management.manager_remove,
        "reindex": management.reindex,
        "route_explain": management.route_explain,
        "dead_letters": management.dead_letters,
        "dead_letter_retry": management.dead_letter_retry,
        "audit_log": management.audit_log,
        "health": management.health,
        "status": management.status,
        "maintenance": management.maintenance,
        "backup": management.backup,
        "show_ids": show_ids,
        "where": show_ids,
    }
    for command, callback in handlers.items():
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED
            | filters.StatusUpdate.FORUM_TOPIC_REOPENED
            | filters.StatusUpdate.FORUM_TOPIC_EDITED,
            management.track_topic_status,
        )
    )


if __name__ == "__main__":
    main()
