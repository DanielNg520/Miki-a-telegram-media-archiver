from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable

from pydantic import ValidationError

from miki_sorter_bot.config import Settings, get_settings
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.storage import Storage


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    level: str
    name: str
    message: str


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    checks: tuple[DiagnosticCheck, ...]

    @property
    def has_errors(self) -> bool:
        return any(check.level == "error" for check in self.checks)

    def format(self) -> str:
        lines = ["Miki checkup:"]
        lines.extend(
            f"- [{_human_level(check.level)}] {check.name}: {check.message}"
            for check in self.checks
        )
        if not self.has_errors:
            lines.append("Result: Miki is ready from this machine's point of view.")
        return "\n".join(lines)


def run_diagnostics(settings: Settings, repositories: SqliteRepositories) -> DiagnosticReport:
    checks = [
        DiagnosticCheck("ok", "database", "SQLite opened and migrations completed."),
        DiagnosticCheck(
            "ok",
            "source",
            f"Listening to chat {settings.source_chat_id}, topic {settings.source_thread_id}.",
        ),
        _runtime_check(settings),
        *_archive_checks(settings, repositories),
        *_retrieval_checks(settings),
        *_source_activity_checks(settings, repositories),
        *_burner_checks(settings, repositories),
        *_operational_checks(repositories),
    ]
    return DiagnosticReport(tuple(checks))


def _runtime_check(settings: Settings) -> DiagnosticCheck:
    if settings.run_mode == "webhook":
        if not settings.webhook_url.rstrip("/").endswith(settings.webhook_path.rstrip("/")):
            return DiagnosticCheck(
                "warning",
                "runtime",
                "Webhook URL does not end with WEBHOOK_PATH; verify host routing.",
            )
        return DiagnosticCheck(
            "ok",
            "runtime",
            f"Webhook mode on {settings.webhook_listen}:{settings.webhook_port}; "
            f"Telegram will call {settings.webhook_url}.",
        )
    return DiagnosticCheck(
        "ok",
        "runtime",
        "Polling mode; Miki continuously asks Telegram for new updates.",
    )


def _archive_checks(
    settings: Settings,
    repositories: SqliteRepositories,
) -> Iterable[DiagnosticCheck]:
    topics = repositories.list_topics(settings.archive_chat_id)
    mappings = repositories.list_mappings(settings.archive_chat_id)
    forwarding_pairs = getattr(settings, "topic_forwarding_pairs", ())
    if not topics:
        yield DiagnosticCheck(
            "error",
            "archive_topics",
            f"No active topics registered for archive chat {settings.archive_chat_id}. "
            "Run /topic_register inside each destination topic.",
        )
        return
    yield DiagnosticCheck(
        "ok",
        "archive_topics",
        f"{len(topics)} active topic(s) registered for archive chat {settings.archive_chat_id}.",
    )
    registered_thread_ids = {topic.thread_id for topic in topics}
    missing_forwarding_destinations = sorted(
        {
            pair.destination_thread_id
            for pair in forwarding_pairs
            if pair.destination_thread_id not in registered_thread_ids
        }
    )
    if missing_forwarding_destinations:
        yield DiagnosticCheck(
            "error",
            "direct_forwarding",
            "Forwarding destination topic(s) are not registered or active: "
            + ", ".join(str(thread_id) for thread_id in missing_forwarding_destinations),
        )
    elif forwarding_pairs:
        yield DiagnosticCheck(
            "ok",
            "direct_forwarding",
            f"{len(forwarding_pairs)} direct topic forwarding pair(s) configured.",
        )
    if not mappings and not forwarding_pairs:
        yield DiagnosticCheck(
            "error",
            "routes",
            "No direct forwarding pairs or hashtag, keyword, or phrase routes configured. "
            "Set TOPIC_FORWARDING_JSON or add a route after registering topics.",
        )
        return
    if not mappings:
        return
    yield DiagnosticCheck("ok", "routes", f"{len(mappings)} route mapping(s) configured.")
    mapped_topic_ids = {mapping.topic_id for mapping in mappings}
    forwarding_thread_ids = {pair.destination_thread_id for pair in forwarding_pairs}
    unmapped = [
        topic
        for topic in topics
        if topic.id not in mapped_topic_ids and topic.thread_id not in forwarding_thread_ids
    ]
    if unmapped:
        names = ", ".join(f"{topic.name} ({topic.thread_id})" for topic in unmapped[:5])
        suffix = "..." if len(unmapped) > 5 else ""
        yield DiagnosticCheck(
            "warning",
            "unmapped_topics",
            f"{len(unmapped)} active topic(s) have no routes: {names}{suffix}",
        )


def _retrieval_checks(settings: Settings) -> Iterable[DiagnosticCheck]:
    if not settings.request_topic_ids:
        yield DiagnosticCheck(
            "warning",
            "retrieval",
            "REQUEST_TOPIC_IDS is empty, so #request retrieval submissions are disabled.",
        )


def _source_activity_checks(
    settings: Settings,
    repositories: SqliteRepositories,
) -> Iterable[DiagnosticCheck]:
    if not getattr(settings, "source_activity_check_enabled", False):
        return
    hours = getattr(settings, "source_activity_window_hours", 24)
    since = datetime.now(UTC) - timedelta(hours=hours)
    recent = repositories.count_recent_source_posts(
        settings.source_chat_id,
        settings.source_thread_id,
        since.isoformat(),
    )
    if recent:
        yield DiagnosticCheck(
            "ok",
            "source_activity",
            f"{recent} indexed post(s) from the source topic in the last {hours}h.",
        )
        return
    yield DiagnosticCheck(
        "warning",
        "source_activity",
        f"No indexed posts from the source topic in the last {hours}h. "
        "If posts were expected, verify Miki is running, privacy mode is off, "
        "and SOURCE_THREAD_ID is correct.",
    )


def _burner_checks(
    settings: Settings,
    repositories: SqliteRepositories,
) -> Iterable[DiagnosticCheck]:
    # Import locally so the burner module never becomes a hard dependency of the
    # core diagnostics path; the layer is entirely optional.
    from miki_sorter_bot.burner import BurnerCapability

    if not getattr(settings, "burner_configured", False):
        yield DiagnosticCheck("ok", "burner", "Burner layer not configured (core-only mode).")
        return

    availability = BurnerCapability(settings, repositories).evaluate()
    level = "ok" if availability.available else "warning"
    message = availability.summary().removeprefix("burner: ")
    if availability.last_error:
        message = f"{message}; last error: {availability.last_error}"
    yield DiagnosticCheck(level, "burner", message)


def _human_level(level: str) -> str:
    return {
        "ok": "OK",
        "warning": "CHECK",
        "error": "NEEDS FIX",
    }.get(level, level.upper())


def _operational_checks(repositories: SqliteRepositories) -> Iterable[DiagnosticCheck]:
    status = repositories.operational_status()
    unresolved = status["unresolved_dead_letters"]
    if unresolved:
        yield DiagnosticCheck(
            "warning",
            "dead_letters",
            f"{unresolved} unresolved dead letter(s) need operator review.",
        )
    running = status["jobs"].get("running", 0)
    pending = status["jobs"].get("pending", 0)
    failed = status["jobs"].get("failed", 0)
    if pending or running or failed:
        yield DiagnosticCheck(
            "warning",
            "jobs",
            f"Job states need review: pending={pending}, running={running}, failed={failed}.",
        )


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as error:
        messages = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )
        raise SystemExit(f"Invalid bot configuration: {messages}") from error

    storage = Storage(settings.database_path)
    try:
        repositories = storage.open()
        report = run_diagnostics(settings, repositories)
    finally:
        storage.close()

    print(report.format())
    if report.has_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
