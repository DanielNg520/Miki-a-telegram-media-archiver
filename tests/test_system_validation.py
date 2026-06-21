from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from miki_sorter_bot.indexing import IndexingService
from miki_sorter_bot.retrieval import RetrievalService
from miki_sorter_bot.sorting import SortingService
from miki_sorter_bot.storage import Storage


def test_fresh_database_sort_index_retrieve_restart_and_backup(tmp_path) -> None:
    storage = Storage(tmp_path / "state" / "miki.sqlite3")
    repositories = storage.open()
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "hashtag", "Japan", 1)
    settings = SimpleNamespace(
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
    )
    indexing = IndexingService(settings, repositories)
    sorting = SortingService(settings, repositories, indexing)
    source_message = SimpleNamespace(
        message_id=12,
        message_thread_id=5,
        media_group_id=None,
        caption="#Japan TOKYO",
        text=None,
        date=datetime(2026, 6, 13, tzinfo=UTC),
        from_user=SimpleNamespace(id=10, is_bot=False),
        photo=[object()],
        animation=None,
        audio=None,
        document=None,
        sticker=None,
        video=None,
        video_note=None,
        voice=None,
        reply_text=AsyncMock(),
    )
    sort_update = SimpleNamespace(
        effective_message=source_message,
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
    )
    sort_bot = SimpleNamespace(
        id=99,
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
    )

    asyncio.run(sorting.handle_update(sort_update, SimpleNamespace(bot=sort_bot)))

    request_message = SimpleNamespace(
        message_id=50,
        message_thread_id=50,
        text="#request\ntopic: Japan\nkeywords: TOKYO",
        reply_text=AsyncMock(),
    )
    request_update = SimpleNamespace(
        effective_message=request_message,
        effective_chat=SimpleNamespace(id=-200),
        effective_user=SimpleNamespace(id=10, is_bot=False),
    )
    retrieval_bot = SimpleNamespace(
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=202))
    )
    retrieval = RetrievalService(settings, repositories)

    asyncio.run(retrieval.handle_update(request_update, SimpleNamespace(bot=retrieval_bot)))

    assert repositories.get_delivery(-100, 12, -200, 9).status == "sent"
    assert retrieval_bot.copy_message.await_count == 1
    assert repositories.operational_status()["posts"] == 1

    backup = storage.backup(tmp_path / "backups")
    storage.close()
    reopened = Storage(tmp_path / "state" / "miki.sqlite3")
    reopened_repositories = reopened.open()
    assert reopened_repositories.recover_interrupted_jobs() == 0
    Storage.verify_database(backup)
    reopened.close()
