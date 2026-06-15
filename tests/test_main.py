from types import SimpleNamespace
from unittest.mock import AsyncMock

from miki_sorter_bot.main import sort_message


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
