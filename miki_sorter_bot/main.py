from __future__ import annotations

import logging
from types import SimpleNamespace

from pydantic import ValidationError
from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from miki_sorter_bot.config import Settings, get_settings
from miki_sorter_bot.diagnostics import DiagnosticReport, run_diagnostics
from miki_sorter_bot.error_reporting import configure_error_reporter
from miki_sorter_bot.health_server import HealthServer
from miki_sorter_bot.logging_config import (
    configure_logging,
    reset_correlation_id,
    set_correlation_id,
)
from miki_sorter_bot.indexing import IndexingService
from miki_sorter_bot.integrations import IntegrationService
from miki_sorter_bot.instance_lock import AlreadyRunningError, InstanceLock
from miki_sorter_bot.management import ManagementCommands
from miki_sorter_bot.operations import OperationsService
from miki_sorter_bot.recovery import JobRecoveryService
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

    lock = InstanceLock(settings.bot_token, role="sorter")
    try:
        lock.acquire()
    except AlreadyRunningError as error:
        raise SystemExit(str(error)) from error
    try:
        _run(settings)
    finally:
        lock.release()


def _run(settings: Settings) -> None:

    configure_logging(settings.log_level, settings.log_format)
    error_reporter = configure_error_reporter(
        dsn=settings.error_reporting_dsn,
        environment=settings.error_reporting_environment,
    )
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
    recovery = JobRecoveryService(
        repositories,
        sorting,
        retrieval,
        batch_size=settings.job_recovery_batch_size,
    )
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
        recovery,
    )
    health_server = (
        HealthServer(
            host=settings.health_listen,
            port=settings.health_port,
            status_provider=storage.operational_status,
        )
        if settings.health_server_enabled
        else None
    )

    async def close_storage(_: Application) -> None:
        if health_server is not None:
            health_server.stop()
        storage.close()

    async def stop_workers(application: Application) -> None:
        context = SimpleNamespace(bot=application.bot, application=application)
        await sorting.shutdown(context)

    async def startup_tasks(application: Application) -> None:
        if health_server is not None:
            health_server.start()
        context = SimpleNamespace(bot=application.bot, application=application)
        await recovery.run_once(context)
        await _send_startup_checkin(application, settings, repositories)

    application = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(startup_tasks)
        .post_stop(stop_workers)
        .post_shutdown(close_storage)
        .build()
    )
    application.bot_data["indexing"] = indexing
    application.bot_data["sorting"] = sorting
    application.bot_data["retrieval"] = retrieval
    application.bot_data["integrations"] = integrations
    application.bot_data["operations"] = operations
    application.bot_data["recovery"] = recovery
    application.bot_data["repositories"] = repositories
    application.bot_data["settings"] = settings
    application.bot_data["error_reporter"] = error_reporter
    LOGGER.info(
        "Album buffering configured",
        extra={
            "flush_delay_seconds": settings.album_flush_delay_seconds,
            "max_wait_seconds": settings.album_max_wait_seconds,
        },
    )
    application.add_error_handler(_handle_error)
    _schedule_daily_backup(application, settings, operations, repositories)
    _schedule_sanity_checks(application, settings, repositories)
    _schedule_job_recovery(application, settings, recovery)
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

    _run_application(application, settings)


async def _handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error is None:
        return
    repositories = context.application.bot_data.get("repositories")
    if isinstance(error, (NetworkError, TimedOut)):
        if repositories is not None:
            repositories.increment_metric("telegram_polling_network_errors", 1)
        LOGGER.warning("Telegram polling network error: %s", error)
        return
    reporter = context.application.bot_data.get("error_reporter")
    if reporter is not None:
        reporter.capture_exception(error)
    if repositories is not None:
        repositories.increment_metric("application_errors", 1)
    LOGGER.error(
        "Unhandled application error",
        exc_info=(type(error), error, error.__traceback__),
    )


def _run_application(application: Application, settings) -> None:
    if settings.run_mode == "webhook":
        LOGGER.info(
            "Miki sorter is running in webhook mode.",
            extra={
                "webhook_url": settings.webhook_url,
                "listen": settings.webhook_listen,
                "port": settings.webhook_port,
                "path": settings.webhook_path,
            },
        )
        application.run_webhook(
            listen=settings.webhook_listen,
            port=settings.webhook_port,
            url_path=settings.webhook_path.lstrip("/"),
            webhook_url=settings.webhook_url,
            allowed_updates=Update.ALL_TYPES,
            bootstrap_retries=settings.telegram_bootstrap_retries,
            drop_pending_updates=settings.telegram_drop_pending_updates,
            max_connections=settings.webhook_max_connections,
            secret_token=settings.webhook_secret_token or None,
        )
        return

    LOGGER.info("Miki sorter is running in polling mode.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=settings.telegram_bootstrap_retries,
        drop_pending_updates=settings.telegram_drop_pending_updates,
    )


async def _send_startup_checkin(
    application: Application,
    settings,
    repositories,
) -> None:
    if not settings.telegram_startup_checkin_enabled:
        return
    report = run_diagnostics(settings, repositories)
    await _notify_operators(application, settings, "Miki started.\n\n" + report.format())


async def _notify_operators(application: Application, settings, text: str) -> None:
    targets = settings.telegram_notification_chat_ids or settings.admin_user_ids
    for chat_id in targets:
        try:
            await application.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            LOGGER.warning("Could not notify operator", extra={"chat_id": chat_id})


def _non_ok_summary(report: DiagnosticReport) -> str:
    checks = [check for check in report.checks if check.level != "ok"]
    if not checks:
        return ""
    return "\n".join(f"- [{check.level.upper()}] {check.name}: {check.message}" for check in checks)


def _schedule_sanity_checks(application: Application, settings, repositories) -> None:
    if not settings.sanity_check_enabled:
        return
    job_queue = application.job_queue
    if job_queue is None:
        LOGGER.warning("Sanity checks requested but JobQueue is unavailable.")
        return

    async def run_sanity_check(context: ContextTypes.DEFAULT_TYPE) -> None:
        token = set_correlation_id("sanity-check")
        try:
            report = run_diagnostics(settings, repositories)
            summary = _non_ok_summary(report)
            if not summary:
                repositories.increment_metric("sanity_checks_ok", 1)
                return
            repositories.increment_metric("sanity_check_warnings", 1)
            LOGGER.warning("Sanity check found issues", extra={"count": len(summary.splitlines())})
            if settings.telegram_notification_chat_ids:
                await _notify_operators(context.application, settings, "Miki checkup:\n" + summary)
        finally:
            reset_correlation_id(token)

    interval_seconds = settings.sanity_check_interval_minutes * 60
    job_queue.run_repeating(
        run_sanity_check,
        interval=interval_seconds,
        first=interval_seconds,
        name="sanity-check",
    )


def _schedule_job_recovery(
    application: Application,
    settings,
    recovery: JobRecoveryService,
) -> None:
    job_queue = application.job_queue
    if job_queue is None:
        LOGGER.warning("Job recovery requested but JobQueue is unavailable.")
        return

    async def recover_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
        await recovery.run_once(context)

    interval = settings.job_recovery_interval_seconds
    job_queue.run_repeating(
        recover_pending,
        interval=interval,
        first=interval,
        name="job-recovery",
    )


def _schedule_daily_backup(
    application: Application,
    settings,
    operations: OperationsService,
    repositories,
) -> None:
    if not settings.backup_daily_enabled:
        return
    job_queue = application.job_queue
    if job_queue is None:
        LOGGER.warning(
            "Daily backups requested but JobQueue is unavailable; "
            "install python-telegram-bot[job-queue] to enable them.",
        )
        return

    async def run_daily_backup(_: ContextTypes.DEFAULT_TYPE) -> None:
        token = set_correlation_id("daily-backup")
        try:
            destination, pruned = operations.backup_and_prune(
                keep=settings.backup_retention_count,
            )
            repositories.increment_metric("database_backups", 1)
            repositories.add_audit_event(
                actor_type="system",
                actor_id="miki",
                action="operations.backup.scheduled",
                outcome="success",
                resource_type="database_backup",
                resource_id=destination.name,
                details={"pruned": len(pruned)},
            )
            LOGGER.info(
                "Daily backup created",
                extra={"backup": destination.name, "pruned": len(pruned)},
            )
        except Exception:
            repositories.increment_metric("database_backup_failures", 1)
            repositories.add_audit_event(
                actor_type="system",
                actor_id="miki",
                action="operations.backup.scheduled",
                outcome="failed",
                resource_type="database_backup",
            )
            LOGGER.exception("Daily backup failed")
        finally:
            reset_correlation_id(token)

    job_queue.run_daily(
        run_daily_backup,
        time=settings.backup_time_utc,
        name="daily-backup",
    )
    LOGGER.info(
        "Scheduled daily backup",
        extra={
            "time_utc": settings.backup_time,
            "retention": settings.backup_retention_count,
        },
    )


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
        "source_show": management.source_show,
        "source_set": management.source_set,
        "forward_add": management.forward_add,
        "forward_remove": management.forward_remove,
        "forward_list": management.forward_list,
        "doctor": management.doctor,
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
