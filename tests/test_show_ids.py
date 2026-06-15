from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio

from miki_sorter_bot.show_ids import format_update_ids, show_ids


def _update(user_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(
        effective_message=SimpleNamespace(
            message_id=99,
            message_thread_id=1234,
            reply_text=AsyncMock(),
        ),
        effective_chat=SimpleNamespace(
            id=-100123,
            type="supergroup",
            title="Archive",
        ),
        effective_user=SimpleNamespace(id=user_id),
    )


def test_format_update_ids_includes_topic_and_caller() -> None:
    details = format_update_ids(_update())

    assert "chat_id: -100123" in details
    assert "chat_type: supergroup" in details
    assert "chat_name: Archive" in details
    assert "topic_id: 1234" in details
    assert "message_id: 99" in details
    assert "user_id: 42" in details


def test_show_ids_rejects_non_admin_in_main_bot_mode() -> None:
    update = _update(user_id=7)
    context = SimpleNamespace(
        bot_data={"settings": SimpleNamespace(admin_user_ids=frozenset({42}))}
    )

    asyncio.run(show_ids(update, context))

    update.effective_message.reply_text.assert_awaited_once_with(
        "You are not authorized to inspect Miki's Telegram IDs."
    )


def test_show_ids_replies_to_admin() -> None:
    update = _update()
    context = SimpleNamespace(
        bot_data={"settings": SimpleNamespace(admin_user_ids=frozenset({42}))}
    )

    asyncio.run(show_ids(update, context))

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "topic_id: 1234" in reply
