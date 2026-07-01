"""Burner layer — Phase 4 read-only history backfill (index-only).

Reads an archive topic's history with the burner user account and feeds each
media message into the *existing* ``MessageIndexer.index()`` — so tokens, album
keys, ``extractor_version`` and the idempotent ``upsert_post`` are reused
verbatim. The only new logic is the Telethon→duck-type adapter and the crawl
loop.

Run on demand from the CLI (``miki-burner backfill <topic_id>``); it is bounded
(``--limit``) and resumes from a ``min_id`` checkpoint so each run reads only
messages newer than what is already indexed. Reads only — never sends, never
copies media (delivery still happens via the Miki bot's ``copy_message``, which
works for any message in a chat the bot belongs to).

Provenance: backfilled rows are stamped ``source_kind='backfill'`` so coverage
can be audited and a bad run selectively purged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from types import SimpleNamespace

from miki_sorter_bot.config import Settings
from miki_sorter_bot.indexing import MessageIndexer
from miki_sorter_bot.repositories import SqliteRepositories

logger = logging.getLogger(__name__)

# Telethon media property -> the PTB field name MessageIndexer.media_type() reads.
# Order is significant: specific kinds before the generic 'document' (a video,
# gif, etc. is also a document in Telethon), so the adapter picks one field.
_MEDIA_FIELDS: tuple[tuple[str, str], ...] = (
    ("gif", "animation"),
    ("sticker", "sticker"),
    ("video_note", "video_note"),
    ("voice", "voice"),
    ("video", "video"),
    ("audio", "audio"),
    ("photo", "photo"),
    ("document", "document"),
)

# A factory that yields history messages with id greater than ``min_id``,
# oldest-first. Re-callable so a flood-wait can resume from a checkpoint.
HistoryFactory = Callable[[int], Iterable[object]]


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    chat_id: int
    topic_id: int
    scanned: int
    indexed: int
    last_message_id: int
    start_min_id: int

    def as_dict(self) -> dict[str, object]:
        return {
            "chat_id": self.chat_id,
            "topic_id": self.topic_id,
            "scanned": self.scanned,
            "indexed": self.indexed,
            "last_message_id": self.last_message_id,
            "start_min_id": self.start_min_id,
        }


def adapt_message(message: object) -> object | None:
    """Adapt a Telethon message to the shape ``MessageIndexer.index()`` reads.

    Returns ``None`` for non-media messages (nothing to index). Exactly one media
    field is set so ``media_type()`` resolves it unambiguously.
    """

    detected: str | None = None
    for telethon_attr, ptb_field in _MEDIA_FIELDS:
        if getattr(message, telethon_attr, None):
            detected = ptb_field
            break
    if detected is None:
        return None

    sender = getattr(message, "sender", None)
    from_user = SimpleNamespace(
        id=getattr(message, "sender_id", None),
        is_bot=bool(getattr(sender, "bot", False)),
    )
    grouped_id = getattr(message, "grouped_id", None)
    adapted = SimpleNamespace(
        text=getattr(message, "message", None) or "",
        caption=None,
        from_user=from_user,
        date=getattr(message, "date", None),
        media_group_id=str(grouped_id) if grouped_id else None,
    )
    setattr(adapted, detected, True)
    return adapted


def _default_flood_wait_types() -> tuple[type[BaseException], ...]:
    try:
        from telethon.errors import FloodWaitError

        return (FloodWaitError,)
    except ImportError:  # pragma: no cover - burner extra not installed
        return ()


def backfill_topic(
    repositories: SqliteRepositories,
    settings: Settings,
    *,
    chat_id: int,
    topic_id: int,
    history_factory: HistoryFactory,
    bot_id: int = 0,
    min_id: int | None = None,
    limit: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    flood_wait_types: tuple[type[BaseException], ...] | None = None,
    batch_size: int = 200,
    batch_delay: float = 0.0,
) -> BackfillOutcome:
    """Crawl an archive topic oldest→newest, indexing each media message.

    ``min_id`` defaults to the highest already-indexed message id for this
    (chat, topic) so the run is incremental. Flood-waits are caught and slept
    off, then iteration resumes from the last processed id.
    """

    indexer = MessageIndexer(repositories, bot_id)
    start_min_id = (
        min_id
        if min_id is not None
        else repositories.max_indexed_message_id(chat_id, topic_id)
    )
    flood_types = flood_wait_types if flood_wait_types is not None else _default_flood_wait_types()

    scanned = 0
    indexed = 0
    cursor = start_min_id

    while True:
        try:
            iterator: Iterator[object] = iter(history_factory(cursor))
            for message in iterator:
                scanned += 1
                message_id = int(getattr(message, "id"))
                adapted = adapt_message(message)
                if adapted is not None and indexer.index(
                    adapted,
                    chat_id,
                    thread_id_override=topic_id,
                    message_id_override=message_id,
                    source_kind_override="backfill",
                ):
                    indexed += 1
                cursor = max(cursor, message_id)
                if limit is not None and indexed >= limit:
                    return BackfillOutcome(
                        chat_id, topic_id, scanned, indexed, cursor, start_min_id
                    )
                if batch_delay and scanned % batch_size == 0:
                    sleep(batch_delay)
            break
        except flood_types as error:  # type: ignore[misc]
            seconds = float(getattr(error, "seconds", 1))
            logger.warning("Backfill hit flood-wait; sleeping %.0fs.", seconds + 1)
            sleep(seconds + 1)
            # Loop re-opens the iterator from the updated cursor (min_id).

    logger.info(
        "Backfill of chat %s topic %s: scanned %d, indexed %d (min_id %d -> %d).",
        chat_id,
        topic_id,
        scanned,
        indexed,
        start_min_id,
        cursor,
    )
    return BackfillOutcome(chat_id, topic_id, scanned, indexed, cursor, start_min_id)


def telethon_history_factory(client: object, chat: object, topic_id: int) -> HistoryFactory:
    """Build a re-callable history factory over a connected Telethon client."""

    def factory(min_id: int) -> Iterable[object]:
        return client.iter_messages(  # type: ignore[attr-defined]
            chat,
            reply_to=topic_id,
            reverse=True,
            min_id=min_id or 0,
        )

    return factory


def run_backfill(
    settings: Settings,
    repositories: SqliteRepositories,
    *,
    topic_id: int,
    chat_id: int | None = None,
    limit: int | None = None,
) -> BackfillOutcome:
    """Open a Telethon client and backfill a single archive topic, then close it."""

    if not settings.burner_configured:
        raise SystemExit(
            "Burner is not configured. Provide TELETHON_API_ID, TELETHON_API_HASH, "
            "and TELETHON_SESSION."
        )
    target_chat = chat_id if chat_id is not None else settings.archive_chat_id

    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient

    assert settings.telethon_api_id is not None
    client = TelegramClient(
        StringSession(settings.telethon_session),
        settings.telethon_api_id,
        settings.telethon_api_hash,
    )
    with client:
        factory = telethon_history_factory(client, target_chat, topic_id)
        return backfill_topic(
            repositories,
            settings,
            chat_id=target_chat,
            topic_id=topic_id,
            history_factory=factory,
            limit=limit,
            batch_delay=1.0,
        )
