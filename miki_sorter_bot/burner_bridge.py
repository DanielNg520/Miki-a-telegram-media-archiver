"""Burner layer — Phase 5 forward-bridge for unreachable groups (optional).

For groups the Miki bot cannot join, the burner *forwards* new media into a Miki
**source topic**, where the existing live pipeline takes over: a
``TOPIC_FORWARDING_JSON`` pair (source topic → archive topic) copies it into the
archive, and ``copy_message`` strips the "forwarded from" header for a clean
archive. Forwarding is server-side (no droplet bandwidth); there is no
download+re-upload fallback (rejected non-goal).

Resource model: run as a **cron-polled** pass (``miki-burner bridge-once``), not
an always-on event listener — consistent with the on-demand burner and scarce
droplet resources. Each bridge keeps a ``last_forwarded_id`` checkpoint:

* First poll of a new bridge **seeds** the checkpoint to the group's latest
  message and forwards nothing — history is never bulk-forwarded (mass-sending
  is the most ban-prone action). If you want history, use Phase-4 read-only
  backfill (searchable, not deliverable) instead.
* Later polls forward only messages newer than the checkpoint, capped per run.

Hard stop: a source group with ``noforwards`` (restrict saving/forwarding)
cannot be bridged. It is detected and the bridge is disabled with a reported
reason rather than silently failing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from miki_sorter_bot.config import Settings
from miki_sorter_bot.repositories import SqliteRepositories

logger = logging.getLogger(__name__)

# Bound the work each cron pass does, so a burst in a busy group never turns one
# invocation into a long-running (ban-prone, resource-heavy) send loop.
DEFAULT_PER_BRIDGE_LIMIT = 50


@dataclass
class BridgeOutcome:
    seeded: list[int] = field(default_factory=list)  # foreign_chat_ids seeded this run
    forwarded: dict[int, int] = field(default_factory=dict)  # foreign_chat_id -> count
    disabled: dict[int, str] = field(default_factory=dict)  # foreign_chat_id -> reason
    total_forwarded: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "seeded": self.seeded,
            "forwarded": self.forwarded,
            "disabled": self.disabled,
            "total_forwarded": self.total_forwarded,
        }


def _default_flood_wait_types() -> tuple[type[BaseException], ...]:
    try:
        from telethon.errors import FloodWaitError

        return (FloodWaitError,)
    except ImportError:  # pragma: no cover - burner extra not installed
        return ()


def _default_noforwards_types() -> tuple[type[BaseException], ...]:
    try:
        from telethon.errors import ChatForwardsRestrictedError

        return (ChatForwardsRestrictedError,)
    except ImportError:  # pragma: no cover - burner extra not installed
        return ()


def bridge_once(
    repositories: SqliteRepositories,
    settings: Settings,
    *,
    client: object,
    per_bridge_limit: int = DEFAULT_PER_BRIDGE_LIMIT,
    sleep: Callable[[float], None] = time.sleep,
    flood_wait_types: tuple[type[BaseException], ...] | None = None,
    noforwards_types: tuple[type[BaseException], ...] | None = None,
) -> BridgeOutcome:
    """Forward new media from each active bridge into its Miki source topic."""

    flood_types = flood_wait_types if flood_wait_types is not None else _default_flood_wait_types()
    forbid_types = (
        noforwards_types if noforwards_types is not None else _default_noforwards_types()
    )
    outcome = BridgeOutcome()

    for bridge in repositories.list_active_bridges():
        fcid = bridge.foreign_chat_id

        if client.is_noforwards(fcid):  # type: ignore[attr-defined]
            reason = "source group restricts forwarding (noforwards)"
            repositories.disable_bridge(bridge.id, reason)
            outcome.disabled[fcid] = reason
            logger.warning("Bridge %s disabled: %s", fcid, reason)
            continue

        # Seed a brand-new bridge to "now" so history is not bulk-forwarded.
        if bridge.last_forwarded_id == 0:
            latest = int(client.latest_message_id(fcid))  # type: ignore[attr-defined]
            repositories.update_bridge_checkpoint(bridge.id, latest)
            outcome.seeded.append(fcid)
            logger.info("Bridge %s seeded at message %d (no history forwarded).", fcid, latest)
            continue

        cursor = bridge.last_forwarded_id
        forwarded = 0
        while True:
            try:
                for message in client.iter_new_media(  # type: ignore[attr-defined]
                    fcid, cursor, per_bridge_limit - forwarded
                ):
                    message_id = int(getattr(message, "id"))
                    client.forward(  # type: ignore[attr-defined]
                        fcid, message_id, settings.source_chat_id, bridge.source_thread_id
                    )
                    cursor = message_id
                    forwarded += 1
                    repositories.update_bridge_checkpoint(bridge.id, cursor)
                    if forwarded >= per_bridge_limit:
                        break
                break
            except flood_types as error:  # type: ignore[misc]
                seconds = float(getattr(error, "seconds", 1))
                logger.warning("Bridge %s hit flood-wait; sleeping %.0fs.", fcid, seconds + 1)
                sleep(seconds + 1)
            except forbid_types:  # type: ignore[misc]
                reason = "source group restricts forwarding (noforwards)"
                repositories.disable_bridge(bridge.id, reason)
                outcome.disabled[fcid] = reason
                logger.warning("Bridge %s disabled mid-forward: %s", fcid, reason)
                break

        if forwarded:
            outcome.forwarded[fcid] = forwarded
            outcome.total_forwarded += forwarded

    return outcome


class TelethonBridgeClient:
    """Bridge operations backed by a connected Telethon user client."""

    def __init__(self, client: object) -> None:
        self._client = client

    def is_noforwards(self, chat_id: int) -> bool:
        entity = self._client.get_entity(chat_id)  # type: ignore[attr-defined]
        return bool(getattr(entity, "noforwards", False))

    def latest_message_id(self, chat_id: int) -> int:
        for message in self._client.iter_messages(chat_id, limit=1):  # type: ignore[attr-defined]
            return int(message.id)
        return 0

    def iter_new_media(self, chat_id: int, min_id: int, limit: int):
        from miki_sorter_bot.burner_backfill import adapt_message

        count = 0
        for message in self._client.iter_messages(  # type: ignore[attr-defined]
            chat_id, min_id=min_id, reverse=True
        ):
            if adapt_message(message) is None:
                continue  # non-media: nothing for the pipeline to sort
            yield message
            count += 1
            if count >= limit:
                return

    def forward(
        self, chat_id: int, message_id: int, dest_chat_id: int, dest_thread_id: int
    ) -> None:
        import random

        from telethon.tl import functions

        # High-level forward_messages cannot target a forum topic, so issue the
        # raw request with top_msg_id. drop_author=False keeps the forward header
        # (Miki strips it when it archives via copy_message).
        self._client(  # type: ignore[operator]
            functions.messages.ForwardMessagesRequest(
                from_peer=chat_id,
                id=[message_id],
                to_peer=dest_chat_id,
                top_msg_id=dest_thread_id,
                random_id=[random.randrange(-(2**63), 2**63)],
                drop_author=False,
            )
        )


def run_bridge(
    settings: Settings,
    repositories: SqliteRepositories,
    *,
    per_bridge_limit: int = DEFAULT_PER_BRIDGE_LIMIT,
) -> BridgeOutcome:
    """Open a Telethon client and run one bridge pass over all active bridges."""

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
    with client:
        return bridge_once(
            repositories,
            settings,
            client=TelethonBridgeClient(client),
            per_bridge_limit=per_bridge_limit,
        )
