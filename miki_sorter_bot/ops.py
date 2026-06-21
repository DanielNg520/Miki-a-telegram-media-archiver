from __future__ import annotations

import argparse
import gzip
from html import escape
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from miki_sorter_bot.config import Settings, get_settings
from miki_sorter_bot.diagnostics import DiagnosticReport, run_diagnostics
from miki_sorter_bot.operations import OperationsService
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.storage import Storage

LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()
LOG_DIR = Path("~/.local/log").expanduser()
SERVICE_NAME = "miki"
SERVICE_LABEL = "com.duy.miki-sorter"
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


def cmd_install(_args: argparse.Namespace) -> int:
    program = _resolve_bin("miki-sorter")
    if program is None:
        print("miki: 'miki-sorter' not found on PATH or in ~/.local/bin")
        return 1
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = _plist_path(SERVICE_LABEL)
    path.write_text(_plist_xml(SERVICE_LABEL, program, Path.cwd()), encoding="utf-8")
    print(f"miki: wrote {path} -> {program}")
    print("installed. Run: miki-ops load")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    cmd_unload(args)
    path = _plist_path(SERVICE_LABEL)
    if path.exists():
        path.unlink()
        print(f"miki: removed {path}")
    return 0


def cmd_load(_args: argparse.Namespace) -> int:
    path = _plist_path(SERVICE_LABEL)
    if not path.exists():
        print(f"miki: plist missing ({path})")
        return 1
    # Fixed executable and argument vector; no shell interpolation.
    result = subprocess.run(
        ["/bin/launchctl", "load", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("miki: loaded")
        return 0
    print(f"miki: load failed - {result.stderr.strip()}")
    return 1


def cmd_unload(_args: argparse.Namespace) -> int:
    path = _plist_path(SERVICE_LABEL)
    if not path.exists():
        return 0
    # Fixed executable and argument vector; no shell interpolation.
    result = subprocess.run(
        ["/bin/launchctl", "unload", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("miki: unloaded")
        return 0
    print(f"miki: unload failed - {result.stderr.strip()}")
    return 1


def cmd_restart(_args: argparse.Namespace) -> int:
    # Fixed executables and argument vectors; no shell interpolation.
    uid = subprocess.run(
        ["/usr/bin/id", "-u"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    result = subprocess.run(
        ["/bin/launchctl", "kickstart", "-k", f"gui/{uid}/{SERVICE_LABEL}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("miki: restarted")
        return 0
    print(f"miki: restart failed - {result.stderr.strip()}")
    return 1


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


def _plist_path(label: str) -> Path:
    return LAUNCH_AGENTS / f"{label}.plist"


def _resolve_bin(command: str) -> str | None:
    found = shutil.which(command)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / command
    return str(fallback) if fallback.exists() else None


def _plist_xml(label: str, program: str, workdir: Path) -> str:
    escaped_label = escape(label)
    escaped_program = escape(program)
    escaped_workdir = escape(str(workdir))
    tag = label.rsplit(".", 1)[-1]
    stdout_path = escape(str(LOG_DIR / f"{tag}.out.log"))
    stderr_path = escape(str(LOG_DIR / f"{tag}.err.log"))
    bindir = str(Path(program).parent)
    path_env = ":".join(
        [bindir, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{escaped_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{escaped_program}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{escape(path_env)}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{stdout_path}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_path}</string>
    <key>WorkingDirectory</key>
    <string>{escaped_workdir}</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""


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
    sub.add_parser("install", help="generate a launchd plist for miki-sorter")
    sub.add_parser("uninstall", help="unload and remove the launchd plist")
    sub.add_parser("load", help="launchctl load the managed plist")
    sub.add_parser("unload", help="launchctl unload the managed plist")
    sub.add_parser("restart", help="launchctl kickstart the managed service")
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
    "backup": cmd_backup,
    "doctor": cmd_doctor,
    "health": cmd_health,
    "install": cmd_install,
    "load": cmd_load,
    "logrotate": cmd_logrotate,
    "maintenance": cmd_maintenance,
    "restart": cmd_restart,
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
