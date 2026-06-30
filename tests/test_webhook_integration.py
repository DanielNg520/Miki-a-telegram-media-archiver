"""Webhook-transport integration: a Telegram update arriving over the webhook is
deserialized into a real ``telegram.Update`` and dispatched to the same
``SortingService.handle_update`` that polling uses. This exercises the look-back
forwarded-media edge case against genuine PTB objects (not test doubles), proving
the field access in the sorter holds up for the deserialization seam too.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import Update

from miki_sorter_bot.indexing import IndexingService
from miki_sorter_bot.sorting import SortingService
from miki_sorter_bot.storage import Storage


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        source_chat_id=-100,
        source_thread_id=5,
        archive_chat_id=-200,
        effective_request_chat_id=-200,
        sort_dry_run=False,
        send_confirmation=False,
        request_topic_ids=frozenset({50}),
        requester_bot_ids=frozenset(),
        admin_user_ids=frozenset({1}),
        default_request_limit=20,
        max_request_limit=100,
        topic_forwarding_pairs=(),
    )


def _epoch(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp())


def _forwarded_photo_update() -> Update:
    """A photo forwarded into the source topic, still carrying its origin's
    unrelated caption (which routes nowhere)."""

    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 12,
                "date": _epoch(2026, 6, 13),
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 10, "is_bot": False, "first_name": "U"},
                "message_thread_id": 5,
                "caption": "a forwarded caption",
                "photo": [
                    {"file_id": "f1", "file_unique_id": "u1", "width": 90, "height": 90}
                ],
                "forward_origin": {
                    "type": "user",
                    "date": _epoch(2026, 6, 1),
                    "sender_user": {"id": 77, "is_bot": False, "first_name": "O"},
                },
            },
        },
        None,
    )


def _hashtag_update() -> Update:
    return Update.de_json(
        {
            "update_id": 2,
            "message": {
                "message_id": 13,
                "date": _epoch(2026, 6, 13),
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 10, "is_bot": False, "first_name": "U"},
                "message_thread_id": 5,
                "text": "#Japan",
                "entities": [{"type": "hashtag", "offset": 0, "length": 6}],
            },
        },
        None,
    )


def test_webhook_forwarded_media_then_hashtag_routes_via_lookback(tmp_path) -> None:
    storage = Storage(tmp_path / "state" / "miki.sqlite3")
    repositories = storage.open()
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "hashtag", "Japan", 1)
    settings = _settings()
    sorting = SortingService(settings, repositories, IndexingService(settings, repositories))
    bot = SimpleNamespace(
        id=99,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
    )
    context = SimpleNamespace(bot=bot)

    async def run() -> None:
        # First webhook POST: forwarded media buffered (caption routes nowhere).
        await sorting.handle_update(_forwarded_photo_update(), context)
        # Second webhook POST: hashtag-only message claims and routes it.
        await sorting.handle_update(_hashtag_update(), context)

    asyncio.run(run())

    bot.copy_message.assert_awaited_once()
    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"
    storage.close()
