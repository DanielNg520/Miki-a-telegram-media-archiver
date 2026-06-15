from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram import Update

from miki_sorter_bot.main import (
    _handle_error,
    _non_ok_summary,
    _schedule_sanity_checks,
    _send_startup_checkin,
    _run_application,
    sort_message,
)
from miki_sorter_bot.diagnostics import DiagnosticCheck, DiagnosticReport


async def _run() -> tuple[AsyncMock, SimpleNamespace]:
    service = AsyncMock()
    update = SimpleNamespace(
        update_id=123,
        effective_message=SimpleNamespace(message_id=12),
    )
    context = SimpleNamespace(bot_data={"sorting": service})
    await sort_message(update, context)
    return service, update


def test_sort_message_delegates_to_sorting_service() -> None:
    import asyncio

    service, update = asyncio.run(_run())

    service.handle_update.assert_awaited_once_with(
        update,
        SimpleNamespace(bot_data={"sorting": service}),
    )


def test_run_application_uses_polling_by_default() -> None:
    application = SimpleNamespace(run_polling=Mock(), run_webhook=Mock())
    settings = SimpleNamespace(
        run_mode="polling",
        telegram_bootstrap_retries=-1,
        telegram_drop_pending_updates=False,
    )

    _run_application(application, settings)

    application.run_polling.assert_called_once_with(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=-1,
        drop_pending_updates=False,
    )
    application.run_webhook.assert_not_called()


def test_run_application_uses_webhook_mode() -> None:
    application = SimpleNamespace(run_polling=Mock(), run_webhook=Mock())
    settings = SimpleNamespace(
        run_mode="webhook",
        webhook_url="https://miki.example.com/telegram/webhook",
        webhook_listen="0.0.0.0",
        webhook_port=8080,
        webhook_path="/telegram/webhook",
        telegram_bootstrap_retries=-1,
        telegram_drop_pending_updates=False,
        webhook_max_connections=40,
        webhook_secret_token="secret-token",
    )

    _run_application(application, settings)

    application.run_webhook.assert_called_once_with(
        listen="0.0.0.0",
        port=8080,
        url_path="telegram/webhook",
        webhook_url="https://miki.example.com/telegram/webhook",
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=-1,
        drop_pending_updates=False,
        max_connections=40,
        secret_token="secret-token",
    )
    application.run_polling.assert_not_called()


def test_startup_checkin_sends_doctor_summary_to_notification_targets() -> None:
    import asyncio

    repositories = SimpleNamespace(
        list_topics=Mock(return_value=[]),
        list_mappings=Mock(return_value=[]),
        operational_status=Mock(
            return_value={
                "database": "ok",
                "foreign_keys": True,
                "posts": 0,
                "unavailable_posts": 0,
                "unresolved_dead_letters": 0,
                "jobs": {},
                "deliveries": {},
                "metrics": {},
            }
        ),
    )
    settings = SimpleNamespace(
        telegram_startup_checkin_enabled=True,
        telegram_notification_chat_ids=frozenset({100}),
        admin_user_ids=frozenset({1}),
        source_chat_id=-100,
        source_thread_id=5,
        archive_chat_id=-200,
        run_mode="polling",
        webhook_url="",
        webhook_path="/telegram/webhook",
        webhook_listen="0.0.0.0",
        webhook_port=8080,
        request_topic_ids=frozenset(),
        source_activity_check_enabled=False,
    )
    application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    asyncio.run(_send_startup_checkin(application, settings, repositories))

    application.bot.send_message.assert_awaited_once()
    assert application.bot.send_message.await_args.kwargs["chat_id"] == 100
    assert "Miki started" in application.bot.send_message.await_args.kwargs["text"]


def test_error_handler_reports_and_counts_errors() -> None:
    import asyncio

    error = RuntimeError("boom")
    reporter = SimpleNamespace(capture_exception=Mock())
    repositories = SimpleNamespace(increment_metric=Mock())
    context = SimpleNamespace(
        error=error,
        application=SimpleNamespace(
            bot_data={"error_reporter": reporter, "repositories": repositories}
        ),
    )

    asyncio.run(_handle_error(None, context))

    reporter.capture_exception.assert_called_once_with(error)
    repositories.increment_metric.assert_called_once_with("application_errors", 1)


def test_non_ok_summary_filters_successful_checks() -> None:
    report = DiagnosticReport(
        (
            DiagnosticCheck("ok", "database", "fine"),
            DiagnosticCheck("warning", "routes", "missing"),
        )
    )

    assert _non_ok_summary(report) == "- [WARNING] routes: missing"


def test_sanity_checks_are_scheduled_when_enabled() -> None:
    job_queue = SimpleNamespace(run_repeating=Mock())
    application = SimpleNamespace(job_queue=job_queue)
    settings = SimpleNamespace(sanity_check_enabled=True, sanity_check_interval_minutes=15)

    _schedule_sanity_checks(application, settings, SimpleNamespace())

    job_queue.run_repeating.assert_called_once()
    assert job_queue.run_repeating.call_args.kwargs["interval"] == 900
