from __future__ import annotations

from dataclasses import dataclass
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
    if not mappings:
        yield DiagnosticCheck(
            "error",
            "routes",
            "No hashtag, keyword, or phrase routes configured. "
            "Run /hashtag_add, /keyword_add, or /keyword_replace after registering topics.",
        )
        return
    yield DiagnosticCheck("ok", "routes", f"{len(mappings)} route mapping(s) configured.")
    mapped_topic_ids = {mapping.topic_id for mapping in mappings}
    unmapped = [topic for topic in topics if topic.id not in mapped_topic_ids]
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
    failed = status["jobs"].get("failed", 0)
    if running or failed:
        yield DiagnosticCheck(
            "warning",
            "jobs",
            f"Job states need review: running={running}, failed={failed}.",
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
