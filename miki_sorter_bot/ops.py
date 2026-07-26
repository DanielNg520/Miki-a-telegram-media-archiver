from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from miki_sorter_bot import service
from miki_sorter_bot.config import Settings, get_settings
from miki_sorter_bot.diagnostics import DiagnosticReport, run_diagnostics
from miki_sorter_bot.operations import OperationsService
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.storage import Storage

LOG_DIR = service.LOG_DIR
DEFAULT_MAX_BYTES = 1 * 1024 * 1024
DEFAULT_KEEP = 7


@dataclass(slots=True)
class Runtime:
    settings: Settings
    storage: Storage
    repositories: SqliteRepositories

    def close(self) -> None:
        self.storage.close()


def _open_runtime() -> Runtime:
    try:
        settings = get_settings()
    except ValidationError as error:
        messages = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )
        raise SystemExit(f"Invalid bot configuration: {messages}") from error
    storage = Storage(settings.database_path)
    return Runtime(settings, storage, storage.open())


def cmd_doctor(_args: argparse.Namespace) -> int:
    runtime = _open_runtime()
    try:
        report = run_diagnostics(runtime.settings, runtime.repositories)
    finally:
        runtime.close()
    print(report.format())
    return 1 if report.has_errors else 0


def cmd_health(_args: argparse.Namespace) -> int:
    runtime = _open_runtime()
    try:
        report = run_diagnostics(runtime.settings, runtime.repositories)
        status = runtime.repositories.operational_status()
        print(render(report, status))
        return 1 if report.has_errors else 0
    finally:
        runtime.close()


def cmd_status(_args: argparse.Namespace) -> int:
    runtime = _open_runtime()
    try:
        print(_status_text(runtime.repositories.operational_status()))
        return 0
    finally:
        runtime.close()


def cmd_watch(args: argparse.Namespace) -> int:
    out = sys.stdout
    out.write("\033[?1049h\033[?25l")
    out.flush()
    try:
        while True:
            runtime = _open_runtime()
            try:
                report = run_diagnostics(runtime.settings, runtime.repositories)
                status = runtime.repositories.operational_status()
                frame = render(report, status)
            finally:
                runtime.close()
            out.write(_watch_frame(frame))
            out.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        out.write("\033[?25h\033[?1049l")
        out.flush()


def cmd_backup(_args: argparse.Namespace) -> int:
    runtime = _open_runtime()
    try:
        operations = _operations(runtime)
        destination = operations.backup()
        print(f"verified backup: {destination}")
        return 0
    finally:
        runtime.close()


def cmd_maintenance(_args: argparse.Namespace) -> int:
    runtime = _open_runtime()
    try:
        deleted = _operations(runtime).maintain()
        for name, count in sorted(deleted.items()):
            print(f"{name}: {count}")
        return 0
    finally:
        runtime.close()


def cmd_logrotate(args: argparse.Namespace) -> int:
    actions = rotate_logs(
        LOG_DIR,
        max_bytes=int(args.max_mb * 1024 * 1024),
        keep=args.keep,
    )
    if actions:
        print("\n".join(actions))
    else:
        print("logrotate: nothing over threshold")
    return 1 if any(action.startswith("ERROR") for action in actions) else 0


def _emit(result: "service.Result") -> int:
    """Print a service Result's messages and return its exit code."""
    for message in result.messages:
        print(message)
    return result.code


def cmd_install(_args: argparse.Namespace) -> int:
    # Working directory is captured now (where .env lives), like the launchd plist.
    return _emit(service.install(Path.cwd()))


def cmd_uninstall(_args: argparse.Namespace) -> int:
    return _emit(service.uninstall())


def cmd_load(_args: argparse.Namespace) -> int:
    return _emit(service.load())


def cmd_unload(_args: argparse.Namespace) -> int:
    return _emit(service.unload())


def cmd_restart(_args: argparse.Namespace) -> int:
    return _emit(service.restart())


def cmd_service_status(_args: argparse.Namespace) -> int:
    return _emit(service.status())


def cmd_backfill(args: argparse.Namespace) -> int:
    from miki_sorter_bot.burner_backfill import backfill_and_report

    runtime = _open_runtime()
    try:
        settings = runtime.settings
        repositories = runtime.repositories
        chat_id = args.chat if args.chat is not None else settings.archive_chat_id
        # Show what the config resolved to: the archive chat and the topic ids to
        # sweep come straight from settings + the topics table — no ids to pass.
        if args.topic_id is None:
            topics = repositories.list_topics(chat_id)
            if topics:
                listing = ", ".join(f"{t.name} ({t.thread_id})" for t in topics)
                print(f"Sweeping {len(topics)} archive topic(s) of chat {chat_id}: {listing}")
            else:
                print(f"No active topics registered for archive chat {chat_id}.")
                return 0
        # 0 disables that cap (see the burner CLI); pass None to the runner.
        try:
            code, lines = backfill_and_report(
                settings,
                repositories,
                topic_id=args.topic_id,
                chat_id=args.chat,
                limit=args.limit if args.limit else None,
                max_minutes=args.max_minutes if args.max_minutes else None,
                jitter=args.jitter,
            )
        except SystemExit as exc:  # e.g. burner not configured
            print(str(exc))
            return 2
    finally:
        runtime.close()
    for line in lines:
        print(line)
    return code


def cmd_bot(args: argparse.Namespace) -> int:
    from miki_sorter_bot.bot_console import list_commands, run_command

    if args.list or not args.name:
        commands = list_commands()
        print("Telegram commands runnable via `miki-ops bot <command> [args…]`:")
        print("  " + "  ".join(commands))
        print(
            "\nRuns the same handler the bot runs, as an admin. Examples:\n"
            "  miki-ops bot status\n"
            "  miki-ops bot keyword_list\n"
            "  miki-ops bot keyword_add JAV 日本\n"
            "  miki-ops bot config LOG_LEVEL"
        )
        return 0
    runtime = _open_runtime()
    try:
        result = run_command(
            runtime.settings,
            runtime.repositories,
            runtime.storage,
            name=args.name,
            args=args.args,
            chat_id=args.chat,
            thread_id=args.thread,
            user_id=args.as_user,
        )
    finally:
        runtime.close()
    print(result.output)
    return 0 if result.ok else 1


def render(report: DiagnosticReport, status: dict[str, object]) -> str:
    jobs = status.get("jobs", {})
    deliveries = status.get("deliveries", {})
    metrics = status.get("metrics", {})
    lines = [
        f"MIKI SORTER health  {datetime.now().strftime('%H:%M:%S')}",
        "─" * 64,
        _summary_line(report),
        "",
        f"database       {status.get('database')}  foreign_keys={status.get('foreign_keys')}",
        f"posts          {status.get('posts', 0):,} available"
        f"  {status.get('unavailable_posts', 0):,} unavailable",
        f"dead letters   {status.get('unresolved_dead_letters', 0):,} unresolved",
        f"jobs           {_counts_text(jobs)}",
        f"deliveries     {_counts_text(deliveries)}",
        f"metrics        {_metrics_text(metrics)}",
        "",
        "checks",
    ]
    lines.extend(
        f"  [{check.level.upper()}] {check.name}: {check.message}" for check in report.checks
    )
    return "\n".join(lines)


def _status_text(status: dict[str, object]) -> str:
    return "\n".join(
        (
            f"database: {status.get('database')}",
            f"foreign_keys: {status.get('foreign_keys')}",
            f"posts: {status.get('posts', 0)}",
            f"unavailable_posts: {status.get('unavailable_posts', 0)}",
            f"unresolved_dead_letters: {status.get('unresolved_dead_letters', 0)}",
            f"jobs: {_counts_text(status.get('jobs', {}))}",
            f"deliveries: {_counts_text(status.get('deliveries', {}))}",
            f"metrics: {_metrics_text(status.get('metrics', {}))}",
        )
    )


def _summary_line(report: DiagnosticReport) -> str:
    errors = sum(1 for check in report.checks if check.level == "error")
    warnings = sum(1 for check in report.checks if check.level == "warning")
    if errors:
        return f"● degraded: {errors} error(s), {warnings} warning(s)"
    if warnings:
        return f"● check: {warnings} warning(s)"
    return "● all systems nominal"


def _counts_text(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "none"
    return ", ".join(f"{key}={count}" for key, count in sorted(value.items()))


def _metrics_text(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "none"
    interesting = (
        "sort_deliveries",
        "retrieval_items_copied",
        "telegram_retries",
        "telegram_throttles",
        "application_errors",
        "album_flush_failures",
    )
    pairs = [f"{key}={value[key]}" for key in interesting if key in value]
    return ", ".join(pairs) if pairs else f"{len(value)} counter(s)"


def _watch_frame(body: str) -> str:
    return "\033[H" + "\033[K\n".join(body.split("\n")) + "\033[K\033[J"


def rotate_logs(log_dir: Path, *, max_bytes: int, keep: int) -> list[str]:
    actions: list[str] = []
    for live in sorted(log_dir.glob("miki*.log")):
        try:
            size = live.stat().st_size
        except OSError:
            continue
        if size < max_bytes:
            continue
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S.%fZ")
        destination = live.with_name(f"{live.name}.{stamp}.gz")
        try:
            with live.open("rb") as source, gzip.open(destination, "wb") as archive:
                shutil.copyfileobj(source, archive)
            with live.open("r+b") as handle:
                handle.truncate(0)
        except OSError as error:
            actions.append(f"ERROR rotating {live.name}: {error}")
            continue
        actions.append(f"rotated {live.name} ({size} bytes -> {destination.name})")
        generations = sorted(log_dir.glob(f"{live.name}.*.gz"))
        for old in generations[:-keep] if keep > 0 else generations:
            try:
                old.unlink()
            except OSError:
                continue
            actions.append(f"pruned {old.name}")
    return actions


def _operations(runtime: Runtime) -> OperationsService:
    settings = runtime.settings
    return OperationsService(
        runtime.repositories,
        runtime.storage,
        backup_directory=settings.backup_directory,
        transient_retention_days=settings.transient_retention_days,
        audit_retention_days=settings.audit_retention_days,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="miki-ops",
        description="Terminal ops tooling for the Miki sorter bot.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health", help="one-shot health dashboard")
    sub.add_parser("doctor", help="plain diagnostic report")
    sub.add_parser("status", help="compact database status")
    watch = sub.add_parser("watch", help="auto-refreshing health dashboard")
    watch.add_argument("--interval", type=_positive_float, default=3.0)
    sub.add_parser("backup", help="create a verified database backup")
    sub.add_parser("maintenance", help="prune transient operational records")
    rotate = sub.add_parser("logrotate", help="rotate oversized miki logs")
    rotate.add_argument(
        "--max-mb",
        type=_positive_float,
        default=DEFAULT_MAX_BYTES / (1024 * 1024),
    )
    rotate.add_argument("--keep", type=_non_negative_int, default=DEFAULT_KEEP)
    sub.add_parser(
        "install",
        help="register miki-sorter for autostart (launchd on macOS, Startup "
        "folder on Windows) using the current directory for .env",
    )
    sub.add_parser("uninstall", help="stop and remove the autostart registration")
    sub.add_parser("load", help="start the managed service now")
    sub.add_parser("unload", help="stop the managed service")
    sub.add_parser("restart", help="restart the managed service")
    sub.add_parser("service-status", help="is the managed bot process running?")
    from miki_sorter_bot import burner_backfill as _bf

    backfill = sub.add_parser(
        "backfill",
        help="index archive history via the burner; OMIT topic id to sweep every "
        "active archive topic (chat + topics come from config)",
    )
    backfill.add_argument(
        "topic_id", type=int, nargs="?", default=None,
        help="archive topic (thread) id; omit to sweep all active archive topics",
    )
    backfill.add_argument(
        "--chat", type=int, default=None, help="chat id (default: ARCHIVE_CHAT_ID)"
    )
    backfill.add_argument(
        "--limit", type=int, default=_bf.DEFAULT_LIMIT,
        help=f"per-topic count cap (default {_bf.DEFAULT_LIMIT}; 0 disables it)",
    )
    backfill.add_argument(
        "--max-minutes", dest="max_minutes", type=float, default=_bf.DEFAULT_MAX_MINUTES,
        help=f"time budget for the whole run (default {_bf.DEFAULT_MAX_MINUTES}; 0 disables it)",
    )
    backfill.add_argument(
        "--jitter", type=float, default=_bf.DEFAULT_JITTER_SECONDS,
        help=f"random extra seconds per inter-batch pause (default {_bf.DEFAULT_JITTER_SECONDS})",
    )
    bot = sub.add_parser(
        "bot",
        help="run any Telegram admin command locally (e.g. `bot status`, "
        "`bot keyword_add JAV 日本`); `bot --list` to enumerate",
    )
    bot.add_argument("name", nargs="?", help="command name (omit or --list to list)")
    bot.add_argument(
        "args", nargs=argparse.REMAINDER, help="arguments passed to the command"
    )
    bot.add_argument("--list", action="store_true", help="list available commands")
    bot.add_argument(
        "--chat", type=int, default=None,
        help="chat id context (default: SOURCE_CHAT_ID)",
    )
    bot.add_argument(
        "--thread", type=int, default=None, help="forum topic (thread) id context"
    )
    bot.add_argument(
        "--as", dest="as_user", type=int, default=None,
        help="act as this user id (default: first ADMIN_USER_IDS)",
    )
    return parser


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


_DISPATCH = {
    "backfill": cmd_backfill,
    "backup": cmd_backup,
    "bot": cmd_bot,
    "doctor": cmd_doctor,
    "health": cmd_health,
    "install": cmd_install,
    "load": cmd_load,
    "logrotate": cmd_logrotate,
    "maintenance": cmd_maintenance,
    "restart": cmd_restart,
    "service-status": cmd_service_status,
    "status": cmd_status,
    "uninstall": cmd_uninstall,
    "unload": cmd_unload,
    "watch": cmd_watch,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
