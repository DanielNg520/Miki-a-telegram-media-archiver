"""Burner layer — Phase 0 capability gate and process skeleton.

This module is intentionally self-contained: it talks to Telegram *not at all*
yet (no MTProto), and it must never import the live bot's PTB runtime. It shares
only the SQLite layer (``Storage``/``SqliteRepositories``) with the core bot,
which is the IPC channel described in ``docs/burner-layer.md``.

Two pieces live here:

* :class:`BurnerCapability` — read-only helper the *bot* uses to answer
  "is the burner available?" per the three-point rule in the design guide.
* the burner *process* (``run_burner`` / :func:`main`) — a standalone
  entrypoint that writes a heartbeat on a fixed interval and otherwise idles.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from miki_sorter_bot.config import Settings, get_settings
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.storage import Storage

logger = logging.getLogger(__name__)

# Phase 0 ships heartbeat only; later phases flip these on as handlers land.
BURNER_CAPABILITIES: dict[str, bool] = {
    "heartbeat": True,
    "backup": True,
    "backfill": True,
    "bridge": True,
}

# Bumped as the burner gains real behaviour; surfaced in /doctor and heartbeats.
BURNER_VERSION = "3"

# Later-phase stubs, if any. All defined command kinds now execute for real.
STUB_COMMAND_PHASES: dict[str, int] = {}
# Kinds the bot may enqueue via `/burner <kind>` (status is answered inline).
ENQUEUEABLE_COMMAND_KINDS: frozenset[str] = frozenset(
    {"noop", "backup_now", "backfill", "bridge_add", "bridge_remove"}
)


@dataclass(frozen=True, slots=True)
class BurnerAvailability:
    """The bot's view of the burner, derived from config + heartbeat."""

    configured: bool
    available: bool
    reason: str
    last_seen_at: datetime | None
    seconds_since_seen: float | None
    session_valid: bool
    version: str | None
    last_error: str | None

    def summary(self) -> str:
        if not self.configured:
            return "burner: not configured"
        if self.available:
            age = (
                f"{self.seconds_since_seen:.0f}s ago"
                if self.seconds_since_seen is not None
                else "just now"
            )
            return f"burner: available (last seen {age})"
        return f"burner: unavailable ({self.reason})"


def _parse_timestamp(value: str) -> datetime | None:
    """Parse a SQLite ``CURRENT_TIMESTAMP`` (UTC, no tz suffix) into aware UTC."""

    if not value:
        return None
    text = value.strip().replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


class BurnerCapability:
    """Answers "is the burner available?" for feature gating.

    Availability requires **all** of: credentials present, the burner reports a
    valid session, and a recent heartbeat (within ``2x`` the poll interval).
    """

    def __init__(self, settings: Settings, repositories: SqliteRepositories) -> None:
        self._settings = settings
        self._repositories = repositories

    def evaluate(self, *, now: datetime | None = None) -> BurnerAvailability:
        configured = self._settings.burner_configured
        if not configured:
            return BurnerAvailability(
                configured=False,
                available=False,
                reason="credentials absent",
                last_seen_at=None,
                seconds_since_seen=None,
                session_valid=False,
                version=None,
                last_error=None,
            )

        status = self._repositories.get_burner_status()
        if status is None:
            return BurnerAvailability(
                configured=True,
                available=False,
                reason="no heartbeat recorded",
                last_seen_at=None,
                seconds_since_seen=None,
                session_valid=False,
                version=None,
                last_error=None,
            )

        last_seen = _parse_timestamp(status.last_seen_at)
        reference = now or datetime.now(UTC)
        seconds_since = (reference - last_seen).total_seconds() if last_seen else None
        freshness_window = self._settings.burner_poll_interval_seconds * 2

        if not status.session_valid:
            reason = "session not valid"
            available = False
        elif seconds_since is None:
            reason = "heartbeat timestamp unreadable"
            available = False
        elif seconds_since > freshness_window:
            reason = f"heartbeat stale ({seconds_since:.0f}s > {freshness_window}s)"
            available = False
        else:
            reason = "healthy"
            available = True

        return BurnerAvailability(
            configured=True,
            available=available,
            reason=reason,
            last_seen_at=last_seen,
            seconds_since_seen=seconds_since,
            session_valid=status.session_valid,
            version=status.version,
            last_error=status.last_error,
        )


def write_heartbeat(
    repositories: SqliteRepositories,
    settings: Settings,
    *,
    session_valid: bool = True,
    last_error: str | None = None,
) -> None:
    """Write a single heartbeat row reflecting current burner state."""

    repositories.record_burner_heartbeat(
        session_valid=session_valid,
        capabilities=BURNER_CAPABILITIES,
        version=BURNER_VERSION,
        last_error=last_error,
    )


def default_session_validator(settings: Settings) -> bool:
    """Validate the configured session against Telegram via Telethon.

    Imported lazily so the optional ``burner`` extra is only needed at runtime.
    Raises on failure (missing extra, revoked session, network error); the
    burner loop catches and records the error.
    """

    from miki_sorter_bot.burner_session import validate_session

    assert settings.telethon_api_id is not None  # guaranteed by burner_configured
    return validate_session(
        settings.telethon_api_id,
        settings.telethon_api_hash,
        settings.telethon_session,
    )


def _handle_status(
    settings: Settings,
    repositories: SqliteRepositories,
    payload: dict[str, object],
) -> dict[str, object]:
    return {"version": BURNER_VERSION, "capabilities": BURNER_CAPABILITIES}


def _handle_noop(
    settings: Settings,
    repositories: SqliteRepositories,
    payload: dict[str, object],
) -> dict[str, object]:
    # A round-trip test command: echo whatever the operator passed.
    return {"message": "noop ok", "echo": payload.get("echo", "")}


def _handle_backup_now(
    settings: Settings,
    repositories: SqliteRepositories,
    payload: dict[str, object],
) -> dict[str, object]:
    from miki_sorter_bot.burner_backup import TelethonBackupUploader, run_backup_offload

    storage = Storage(settings.database_path)
    try:
        storage.open()
        outcome = run_backup_offload(
            settings, storage=storage, uploader=TelethonBackupUploader(settings)
        )
    finally:
        storage.close()
    return outcome.as_dict()


def _handle_backfill(
    settings: Settings,
    repositories: SqliteRepositories,
    payload: dict[str, object],
) -> dict[str, object]:
    from miki_sorter_bot.burner_backfill import run_backfill

    topic_id = payload.get("topic_id")
    if not isinstance(topic_id, int):
        raise ValueError("backfill requires an integer 'topic_id' in the payload")
    chat_id = payload.get("backfill_chat_id")
    limit = payload.get("limit")
    outcome = run_backfill(
        settings,
        repositories,
        topic_id=topic_id,
        chat_id=chat_id if isinstance(chat_id, int) else None,
        limit=limit if isinstance(limit, int) else None,
    )
    return outcome.as_dict()


def _handle_bridge_add(
    settings: Settings,
    repositories: SqliteRepositories,
    payload: dict[str, object],
) -> dict[str, object]:
    foreign_chat_id = payload.get("foreign_chat_id")
    source_thread_id = payload.get("source_thread_id")
    if not isinstance(foreign_chat_id, int) or not isinstance(source_thread_id, int):
        raise ValueError(
            "bridge_add requires integer 'foreign_chat_id' and 'source_thread_id'"
        )
    requested_by = payload.get("requested_by")
    bridge = repositories.add_bridge(
        foreign_chat_id,
        source_thread_id,
        created_by_user_id=requested_by if isinstance(requested_by, int) else None,
    )
    return {"foreign_chat_id": bridge.foreign_chat_id, "source_thread_id": bridge.source_thread_id}


def _handle_bridge_remove(
    settings: Settings,
    repositories: SqliteRepositories,
    payload: dict[str, object],
) -> dict[str, object]:
    foreign_chat_id = payload.get("foreign_chat_id")
    if not isinstance(foreign_chat_id, int):
        raise ValueError("bridge_remove requires an integer 'foreign_chat_id'")
    removed = repositories.remove_bridge(foreign_chat_id)
    return {"foreign_chat_id": foreign_chat_id, "removed": removed}


CommandHandler = Callable[[Settings, SqliteRepositories, dict[str, object]], dict[str, object]]

COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "status": _handle_status,
    "noop": _handle_noop,
    "backup_now": _handle_backup_now,
    "backfill": _handle_backfill,
    "bridge_add": _handle_bridge_add,
    "bridge_remove": _handle_bridge_remove,
}


def process_pending_commands(
    repositories: SqliteRepositories,
    settings: Settings,
    *,
    limit: int = 50,
) -> int:
    """Claim and execute pending burner commands. Returns how many ran."""

    processed = 0
    for command in repositories.list_pending_burner_commands(limit):
        if not repositories.claim_burner_command(command.id):
            continue  # another worker won the claim, or it is no longer runnable
        processed += 1
        handler = COMMAND_HANDLERS.get(command.kind)
        try:
            if handler is not None:
                result = handler(settings, repositories, command.payload)
                repositories.finish_burner_command(command.id, "completed", result=result)
            elif command.kind in STUB_COMMAND_PHASES:
                phase = STUB_COMMAND_PHASES[command.kind]
                repositories.finish_burner_command(
                    command.id,
                    "failed",
                    error=f"'{command.kind}' handler is not implemented yet (arrives in Phase {phase}).",
                )
            else:
                repositories.finish_burner_command(
                    command.id, "failed", error=f"unknown command kind '{command.kind}'"
                )
        except Exception as error:  # a bad handler must not stall the loop
            logger.exception("Burner command %s (%s) failed.", command.id, command.kind)
            repositories.finish_burner_command(
                command.id, "failed", error=str(error) or error.__class__.__name__
            )
    return processed


def run_burner(
    settings: Settings,
    *,
    stop_event: threading.Event | None = None,
    max_iterations: int | None = None,
    session_validator: Callable[[Settings], bool] | None = None,
) -> None:
    """Heartbeat loop for the standalone burner process.

    Validates the session once at startup and reflects that in every heartbeat
    (re-connecting each poll would be wasteful and itself ban-prone).
    ``stop_event`` lets a signal handler request a clean shutdown; the loop
    waits on it between heartbeats so termination is prompt. ``max_iterations``
    bounds the loop for tests.
    """

    if not settings.burner_configured:
        raise SystemExit(
            "Burner is not configured. Set BURNER_ENABLED=true and provide "
            "TELETHON_API_ID, TELETHON_API_HASH, and TELETHON_SESSION."
        )

    validator = session_validator or default_session_validator
    try:
        session_valid = bool(validator(settings))
        validation_error = None if session_valid else "session not authorized"
    except Exception as error:  # missing extra, revoked session, network error
        session_valid = False
        validation_error = str(error) or error.__class__.__name__
        logger.warning("Burner session validation failed: %s", validation_error)

    stop = stop_event or threading.Event()
    interval = settings.burner_poll_interval_seconds
    storage = Storage(settings.database_path)
    iterations = 0
    try:
        repositories = storage.open()
        logger.info(
            "Burner started; session_valid=%s; heartbeat every %ss.",
            session_valid,
            interval,
        )
        while not stop.is_set():
            try:
                write_heartbeat(
                    repositories,
                    settings,
                    session_valid=session_valid,
                    last_error=validation_error,
                )
            except Exception:  # heartbeat must never crash the loop
                logger.exception("Failed to write burner heartbeat.")
            try:
                process_pending_commands(repositories, settings)
            except Exception:  # command processing must never crash the loop
                logger.exception("Failed while processing burner commands.")
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            stop.wait(interval)
    finally:
        storage.close()
        logger.info("Burner stopped.")


def _load_settings() -> Settings:
    try:
        return get_settings()
    except ValidationError as error:
        messages = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )
        raise SystemExit(f"Invalid burner configuration: {messages}") from error


def _run_persistent(settings: Settings) -> None:
    stop_event = threading.Event()

    def _request_stop(*_: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    run_burner(settings, stop_event=stop_event)


def _cli_backup(settings: Settings) -> None:
    from miki_sorter_bot.burner_backup import TelethonBackupUploader, run_backup_offload

    storage = Storage(settings.database_path)
    try:
        storage.open()
        outcome = run_backup_offload(
            settings, storage=storage, uploader=TelethonBackupUploader(settings)
        )
    finally:
        storage.close()
    print(
        f"Backup offloaded: {outcome.artifact.name} ({outcome.size_bytes} bytes), "
        f"message {outcome.uploaded_message_id}, pruned {len(outcome.pruned)} local artifact(s)."
    )


def _cli_backfill(settings: Settings, *, topic_id: int, chat_id: int | None, limit: int | None) -> None:
    from miki_sorter_bot.burner_backfill import run_backfill

    storage = Storage(settings.database_path)
    try:
        repositories = storage.open()
        outcome = run_backfill(
            settings, repositories, topic_id=topic_id, chat_id=chat_id, limit=limit
        )
    finally:
        storage.close()
    print(
        f"Backfill chat {outcome.chat_id} topic {outcome.topic_id}: "
        f"scanned {outcome.scanned}, indexed {outcome.indexed} "
        f"(min_id {outcome.start_min_id} -> {outcome.last_message_id})."
    )


def _with_repositories(settings: Settings, work: Callable[[SqliteRepositories], None]) -> None:
    storage = Storage(settings.database_path)
    try:
        work(storage.open())
    finally:
        storage.close()


def _cli_bridge_add(settings: Settings, *, foreign_chat_id: int, source_thread_id: int) -> None:
    def _run(repositories: SqliteRepositories) -> None:
        bridge = repositories.add_bridge(foreign_chat_id, source_thread_id)
        print(
            f"Bridge registered: foreign chat {bridge.foreign_chat_id} -> "
            f"source topic {bridge.source_thread_id}. First bridge-once seeds the "
            "checkpoint to now (no history forwarded). Ensure a TOPIC_FORWARDING_JSON "
            f"pair maps source topic {bridge.source_thread_id} to its archive topic."
        )

    _with_repositories(settings, _run)


def _cli_bridge_remove(settings: Settings, *, foreign_chat_id: int) -> None:
    def _run(repositories: SqliteRepositories) -> None:
        removed = repositories.remove_bridge(foreign_chat_id)
        print(
            f"Bridge for foreign chat {foreign_chat_id} "
            + ("removed." if removed else "was not active.")
        )

    _with_repositories(settings, _run)


def _cli_bridge_once(settings: Settings) -> None:
    from miki_sorter_bot.burner_bridge import run_bridge

    def _run(repositories: SqliteRepositories) -> None:
        outcome = run_bridge(settings, repositories)
        print(
            f"Bridge pass: forwarded {outcome.total_forwarded}, "
            f"seeded {len(outcome.seeded)}, disabled {len(outcome.disabled)}."
        )
        for chat_id, reason in outcome.disabled.items():
            print(f"  disabled {chat_id}: {reason}")

    _with_repositories(settings, _run)


def main() -> None:
    # Console-script entry points run from the installed package, so load the
    # caller's .env explicitly (mirrors miki-show-ids).
    load_dotenv(dotenv_path=Path.cwd() / ".env")
    logging.basicConfig(level=logging.INFO)

    import argparse

    parser = argparse.ArgumentParser(
        prog="miki-burner",
        description="On-demand burner operations (designed for cron/systemd).",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("backup", help="create, encrypt, and upload a backup, then exit")
    backfill_parser = subparsers.add_parser(
        "backfill", help="index an archive topic's history, then exit"
    )
    backfill_parser.add_argument("topic_id", type=int, help="archive forum topic (thread) id")
    backfill_parser.add_argument(
        "--chat", type=int, default=None, help="chat id (defaults to ARCHIVE_CHAT_ID)"
    )
    backfill_parser.add_argument(
        "--limit", type=int, default=None, help="stop after indexing this many posts"
    )
    bridge_add_parser = subparsers.add_parser(
        "bridge-add", help="register a forward-bridge (foreign group -> Miki source topic)"
    )
    bridge_add_parser.add_argument("foreign_chat_id", type=int, help="foreign group chat id")
    bridge_add_parser.add_argument(
        "source_thread_id", type=int, help="Miki source topic (thread) id to forward into"
    )
    bridge_remove_parser = subparsers.add_parser(
        "bridge-remove", help="deactivate a forward-bridge"
    )
    bridge_remove_parser.add_argument("foreign_chat_id", type=int, help="foreign group chat id")
    subparsers.add_parser(
        "bridge-once", help="forward new media for all active bridges, then exit"
    )
    subparsers.add_parser("once", help="one heartbeat + command drain, then exit")
    subparsers.add_parser("run", help="persistent heartbeat + command-drain loop")
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        raise SystemExit(2)

    settings = _load_settings()
    if args.command == "backup":
        _cli_backup(settings)
    elif args.command == "backfill":
        _cli_backfill(settings, topic_id=args.topic_id, chat_id=args.chat, limit=args.limit)
    elif args.command == "bridge-add":
        _cli_bridge_add(
            settings,
            foreign_chat_id=args.foreign_chat_id,
            source_thread_id=args.source_thread_id,
        )
    elif args.command == "bridge-remove":
        _cli_bridge_remove(settings, foreign_chat_id=args.foreign_chat_id)
    elif args.command == "bridge-once":
        _cli_bridge_once(settings)
    elif args.command == "once":
        run_burner(settings, max_iterations=1)
    else:
        _run_persistent(settings)


if __name__ == "__main__":
    main()
