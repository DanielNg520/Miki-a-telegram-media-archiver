from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram import Update

from miki_sorter_bot.main import _run_application, sort_message


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
