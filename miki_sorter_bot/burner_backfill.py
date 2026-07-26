"""Burner layer — Phase 4 read-only history backfill (index-only).

Reads an archive topic's history with the burner user account and feeds each
media message into the *existing* ``MessageIndexer.index()`` — so tokens, album
keys, ``extractor_version`` and the idempotent ``upsert_post`` are reused
verbatim. The only new logic is the Telethon→duck-type adapter and the crawl
loop.

Run on demand from the CLI (``miki-burner backfill <topic_id>``); it is bounded
(``--limit`` *and* ``--max-minutes``, first hit wins) and resumes from a
``min_id`` checkpoint so each run reads only messages newer than what is already
indexed. Reads only — never sends, never copies media (delivery still happens
via the Miki bot's ``copy_message``, which works for any message in a chat the
bot belongs to).

Account safety (the burner is a *user* account, so ban risk is real — unlike the
bot API):
* **Read-only by construction** — the crawl calls only ``iter_messages``.
* **Always bounded** — the CLI defaults apply BOTH a count cap and a time cap;
  a run can never turn into an open-ended scan by accident.
* **Throttled + jittered** — a base inter-batch delay plus random jitter avoids
  a robotic, evenly-spaced request cadence.
* **Flood-wait aware, and capped** — a flood-wait is slept off and the crawl
  resumes from the cursor, but a flood longer than ``max_flood_wait_seconds``
  stops the run cleanly (it is resumable) instead of sleeping for hours.
* **Non-interactive client** — an invalid/expired session fails fast with a
  clear message instead of blocking on an interactive login prompt.

Provenance: backfilled rows are stamped ``source_kind='backfill'`` so coverage
can be audited and a bad run selectively purged.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
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

# Default account-safety envelope for a CLI run. Both caps are applied at once
# (first hit wins); the jitter/delay throttle the request cadence; the flood cap
# turns a pathological multi-hour wait into a clean, resumable stop.
DEFAULT_LIMIT = 500
DEFAULT_MAX_MINUTES = 15.0
DEFAULT_JITTER_SECONDS = 0.5
DEFAULT_BATCH_DELAY_SECONDS = 1.0
DEFAULT_MAX_FLOOD_WAIT_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    chat_id: int
    topic_id: int
    scanned: int
    indexed: int
    last_message_id: int
    start_min_id: int
    # Why the run ended: 'exhausted' (no more history), 'limit' (count cap),
    # 'time' (time cap), or 'flood_cap' (a flood-wait longer than the cap —
    # resume with another run). Defaults to 'exhausted' so older callers and
    # direct constructions keep working.
    stop_reason: str = "exhausted"

    def as_dict(self) -> dict[str, object]:
        return {
            "chat_id": self.chat_id,
            "topic_id": self.topic_id,
            "scanned": self.scanned,
            "indexed": self.indexed,
            "last_message_id": self.last_message_id,
            "start_min_id": self.start_min_id,
            "stop_reason": self.stop_reason,
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
    max_seconds: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    flood_wait_types: tuple[type[BaseException], ...] | None = None,
    max_flood_wait_seconds: float | None = None,
    batch_size: int = 200,
    batch_delay: float = 0.0,
    jitter: float = 0.0,
    rand: Callable[[], float] = random.random,
) -> BackfillOutcome:
    """Crawl an archive topic oldest→newest, indexing each media message.

    ``min_id`` defaults to the highest already-indexed message id for this
    (chat, topic) so the run is incremental. The run stops at the FIRST of:
    ``limit`` posts indexed, ``max_seconds`` elapsed, or history exhausted.

    Account safety: between batches the crawl sleeps ``batch_delay`` plus a
    random ``jitter`` fraction so the request cadence is not robotically even.
    Flood-waits are caught and slept off (then iteration resumes from the
    cursor); a flood longer than ``max_flood_wait_seconds`` stops the run
    cleanly rather than sleeping indefinitely — the ``min_id`` checkpoint makes
    the next run pick up exactly where this one left off.
    """

    indexer = MessageIndexer(repositories, bot_id)
    start_min_id = (
        min_id if min_id is not None else repositories.max_indexed_message_id(chat_id, topic_id)
    )
    flood_types = flood_wait_types if flood_wait_types is not None else _default_flood_wait_types()

    scanned = 0
    indexed = 0
    cursor = start_min_id
    started_at = clock()

    def _result(reason: str) -> BackfillOutcome:
        return BackfillOutcome(
            chat_id, topic_id, scanned, indexed, cursor, start_min_id, reason
        )

    def _over_time() -> bool:
        return max_seconds is not None and (clock() - started_at) >= max_seconds

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
                    return _result("limit")
                if _over_time():
                    return _result("time")
                if batch_delay and scanned % batch_size == 0:
                    sleep(batch_delay + (jitter * rand() if jitter else 0.0))
            break
        except flood_types as error:  # type: ignore[misc]
            seconds = float(getattr(error, "seconds", 1))
            if max_flood_wait_seconds is not None and seconds > max_flood_wait_seconds:
                logger.warning(
                    "Backfill flood-wait %.0fs exceeds cap %.0fs; stopping cleanly "
                    "(resume with another run — the checkpoint is durable).",
                    seconds,
                    max_flood_wait_seconds,
                )
                return _result("flood_cap")
            logger.warning("Backfill hit flood-wait; sleeping %.0fs.", seconds + 1)
            sleep(seconds + 1)
            if _over_time():
                return _result("time")
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
    return _result("exhausted")


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


FactoryFor = Callable[[int], HistoryFactory]


def backfill_all_topics(
    repositories: SqliteRepositories,
    settings: Settings,
    *,
    chat_id: int,
    factory_for: FactoryFor,
    limit: int | None = None,
    max_seconds: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    flood_wait_types: tuple[type[BaseException], ...] | None = None,
    max_flood_wait_seconds: float | None = None,
    batch_size: int = 200,
    batch_delay: float = 0.0,
    jitter: float = 0.0,
    rand: Callable[[], float] = random.random,
) -> list[BackfillOutcome]:
    """Backfill EVERY active topic of ``chat_id`` in sequence, one shared client.

    Enumerates the archive chat's active topics (the ``topics`` table — the same
    registry the router delivers into) so no topic id need be supplied. ``limit``
    is a PER-TOPIC count cap; ``max_seconds`` is a SINGLE budget shared across the
    whole sweep (first topic to exhaust it ends the run), so the account-safety
    envelope bounds the entire pass, not each topic independently. A topic that
    hits the flood-wait cap ends the sweep (global backoff)."""

    topics = repositories.list_topics(chat_id)  # active only
    deadline = clock() + max_seconds if max_seconds is not None else None
    outcomes: list[BackfillOutcome] = []
    for topic in topics:
        remaining: float | None = None
        if deadline is not None:
            remaining = deadline - clock()
            if remaining <= 0:
                break
        outcome = backfill_topic(
            repositories,
            settings,
            chat_id=chat_id,
            topic_id=topic.thread_id,
            history_factory=factory_for(topic.thread_id),
            limit=limit,
            max_seconds=remaining,
            sleep=sleep,
            clock=clock,
            flood_wait_types=flood_wait_types,
            max_flood_wait_seconds=max_flood_wait_seconds,
            batch_size=batch_size,
            batch_delay=batch_delay,
            jitter=jitter,
            rand=rand,
        )
        outcomes.append(outcome)
        if outcome.stop_reason == "flood_cap":
            break  # global backoff: don't hammer the next topic after a hard flood
    return outcomes


@contextmanager
def _connected_client(settings: Settings) -> Iterator[object]:
    """A connected, authorized Telethon client — reused across topics so a sweep
    does not reconnect per topic (wasteful and itself ban-prone). Connects
    WITHOUT an interactive login: an invalid/expired session fails fast with a
    clear message instead of blocking on a console prompt in a headless run."""

    if not settings.burner_configured:
        raise SystemExit(
            "Burner is not configured. Provide TELETHON_API_ID, TELETHON_API_HASH, "
            "and TELETHON_SESSION."
        )

    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient

    assert settings.telethon_api_id is not None
    client = TelegramClient(
        StringSession(settings.telethon_session),
        settings.telethon_api_id,
        settings.telethon_api_hash,
    )
    client.connect()
    try:
        if not client.is_user_authorized():
            raise SystemExit(
                "Burner session is not authorized (expired or invalid). Re-generate "
                "TELETHON_SESSION; backfill will not attempt an interactive login."
            )
        yield client
    finally:
        client.disconnect()


def run_backfill(
    settings: Settings,
    repositories: SqliteRepositories,
    *,
    topic_id: int,
    chat_id: int | None = None,
    limit: int | None = None,
    max_minutes: float | None = None,
    jitter: float = DEFAULT_JITTER_SECONDS,
    batch_delay: float = DEFAULT_BATCH_DELAY_SECONDS,
    max_flood_wait_seconds: float | None = DEFAULT_MAX_FLOOD_WAIT_SECONDS,
) -> BackfillOutcome:
    """Open a Telethon client and backfill a single archive topic, then close it."""

    target_chat = chat_id if chat_id is not None else settings.archive_chat_id
    max_seconds = max_minutes * 60.0 if max_minutes is not None else None
    with _connected_client(settings) as client:
        factory = telethon_history_factory(client, target_chat, topic_id)
        return backfill_topic(
            repositories,
            settings,
            chat_id=target_chat,
            topic_id=topic_id,
            history_factory=factory,
            limit=limit,
            max_seconds=max_seconds,
            batch_delay=batch_delay,
            jitter=jitter,
            max_flood_wait_seconds=max_flood_wait_seconds,
        )


def backfill_and_report(
    settings: Settings,
    repositories: SqliteRepositories,
    *,
    topic_id: int | None = None,
    chat_id: int | None = None,
    limit: int | None = None,
    max_minutes: float | None = None,
    jitter: float = DEFAULT_JITTER_SECONDS,
) -> tuple[int, list[str]]:
    """Run a single-topic or all-topics backfill and format a human report.

    Shared by ``miki-burner backfill`` and ``miki-ops backfill`` so both print
    the same thing. ``topic_id`` omitted -> sweep every active archive topic
    (chat + topic ids come from settings/DB). Returns (exit_code, lines);
    ``run_backfill*`` may raise SystemExit if the burner is unconfigured — the
    caller decides how to surface that."""

    if topic_id is None:
        outcomes = run_backfill_all(
            settings, repositories, chat_id=chat_id, limit=limit,
            max_minutes=max_minutes, jitter=jitter,
        )
    else:
        outcomes = [
            run_backfill(
                settings, repositories, topic_id=topic_id, chat_id=chat_id,
                limit=limit, max_minutes=max_minutes, jitter=jitter,
            )
        ]

    if not outcomes:
        return 0, ["Backfill: no active archive topics to index."]
    lines: list[str] = []
    total_indexed = 0
    incomplete = False
    for outcome in outcomes:
        total_indexed += outcome.indexed
        incomplete = incomplete or outcome.stop_reason in ("limit", "time", "flood_cap")
        lines.append(
            f"Backfill chat {outcome.chat_id} topic {outcome.topic_id}: "
            f"scanned {outcome.scanned}, indexed {outcome.indexed} "
            f"(min_id {outcome.start_min_id} -> {outcome.last_message_id}); "
            f"stopped: {outcome.stop_reason}."
        )
    if len(outcomes) > 1:
        lines.append(f"Swept {len(outcomes)} topic(s); indexed {total_indexed} total.")
    if incomplete:
        lines.append(
            "  More history may remain — re-run the same command to continue from "
            "the checkpoint (each run is incremental via min_id)."
        )
    return 0, lines


def run_backfill_all(
    settings: Settings,
    repositories: SqliteRepositories,
    *,
    chat_id: int | None = None,
    limit: int | None = None,
    max_minutes: float | None = None,
    jitter: float = DEFAULT_JITTER_SECONDS,
    batch_delay: float = DEFAULT_BATCH_DELAY_SECONDS,
    max_flood_wait_seconds: float | None = DEFAULT_MAX_FLOOD_WAIT_SECONDS,
) -> list[BackfillOutcome]:
    """Sweep every active archive topic with one shared client (no topic id
    needed). ``max_minutes`` is the budget for the WHOLE sweep."""

    target_chat = chat_id if chat_id is not None else settings.archive_chat_id
    max_seconds = max_minutes * 60.0 if max_minutes is not None else None
    with _connected_client(settings) as client:
        def factory_for(thread_id: int) -> HistoryFactory:
            return telethon_history_factory(client, target_chat, thread_id)

        return backfill_all_topics(
            repositories,
            settings,
            chat_id=target_chat,
            factory_for=factory_for,
            limit=limit,
            max_seconds=max_seconds,
            batch_delay=batch_delay,
            jitter=jitter,
            max_flood_wait_seconds=max_flood_wait_seconds,
        )
