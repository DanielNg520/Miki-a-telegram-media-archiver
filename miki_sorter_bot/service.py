"""Cross-platform service management for the Miki sorter bot.

``miki-ops install|uninstall|load|unload|restart|status`` delegate here so the
same verbs work on Linux (systemd), macOS (launchd) and Windows, and degrade
with guidance elsewhere. The public API is a handful of functions returning
``Result``; the CLI prints the messages and propagates the code.

Linux  — a systemd user service (``systemctl --user``) with Restart=on-failure,
          enabled to start at logon (``loginctl enable-linger`` recommended so it
          runs without an active session). No root required.
macOS  — a launchd LaunchAgent (RunAtLoad + KeepAlive), managed with launchctl.
Windows — no-admin autostart via a hidden launcher in the user's Startup folder
          (Task Scheduler registration requires elevation), plus process
          control (tasklist/taskkill) for load/unload/restart/status. This gives
          start-at-logon but not automatic crash-restart; the single-instance
          lock keeps a manual `load` from double-starting the bot.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path

SERVICE_NAME = "miki"
SERVICE_LABEL = "com.duy.miki-sorter"
PROGRAM_NAME = "miki-sorter"
WINDOWS_IMAGE = "miki-sorter.exe"
LOG_DIR = Path("~/.local/log").expanduser()

LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()

SYSTEMD_USER_DIR = Path("~/.config/systemd/user").expanduser()
SYSTEMD_UNIT = f"{SERVICE_NAME}-sorter.service"
BACKFILL_SERVICE_UNIT = "miki-burner-backfill.service"
BACKFILL_TIMER_UNIT = "miki-burner-backfill.timer"


@dataclass(slots=True)
class Result:
    code: int
    messages: list[str]


def _ok(*messages: str) -> Result:
    return Result(0, list(messages))


def _err(*messages: str) -> Result:
    return Result(1, list(messages))


def _run(args: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    """Run a command with a bounded timeout so a stuck helper can't hang the CLI
    (or a health-check/verification client) indefinitely."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(
            args,
            124,
            stdout=error.stdout or "",
            stderr=(error.stderr or "") + "\nmiki: command timed out",
        )


def resolve_program() -> str | None:
    """Absolute path to the ``miki-sorter`` console script. Prefer PATH (uv/pipx
    put it there), fall back to ~/.local/bin. An absolute path is required — the
    OS launcher does not source a shell, so a bare name would not resolve."""
    found = shutil.which(PROGRAM_NAME)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / PROGRAM_NAME
    return str(fallback) if fallback.exists() else None


# ── public API (platform dispatch) ──────────────────────────────────────────

def install(workdir: Path) -> Result:
    if sys.platform == "darwin":
        return _mac_install(workdir)
    if sys.platform == "win32":
        return _win_install(workdir)
    if sys.platform.startswith("linux"):
        return _linux_install(workdir)
    return _unsupported()


def uninstall() -> Result:
    if sys.platform == "darwin":
        return _mac_uninstall()
    if sys.platform == "win32":
        return _win_uninstall()
    if sys.platform.startswith("linux"):
        return _linux_uninstall()
    return _unsupported()


def load() -> Result:
    if sys.platform == "darwin":
        return _mac_load()
    if sys.platform == "win32":
        return _win_load()
    if sys.platform.startswith("linux"):
        return _linux_load()
    return _unsupported()


def unload() -> Result:
    if sys.platform == "darwin":
        return _mac_unload()
    if sys.platform == "win32":
        return _win_unload()
    if sys.platform.startswith("linux"):
        return _linux_unload()
    return _unsupported()


def restart() -> Result:
    if sys.platform == "darwin":
        return _mac_restart()
    if sys.platform == "win32":
        return _win_restart()
    if sys.platform.startswith("linux"):
        return _linux_restart()
    return _unsupported()


def status() -> Result:
    if sys.platform == "win32":
        pid = _win_pid()
        if pid is not None:
            return _ok(f"miki: running (pid {pid})")
        installed = _win_startup_vbs().exists()
        return _ok(
            "miki: not running"
            + ("  (autostart installed — starts at next logon)" if installed
               else "  (autostart not installed — run `miki-ops install`)")
        )
    if sys.platform == "darwin":
        running = _mac_running()
        return _ok(f"miki: {'running' if running else 'not running'}")
    if sys.platform.startswith("linux"):
        running = _linux_running()
        installed = _systemd_unit_path().exists()
        note = "" if running else (
            "  (autostart installed — starts at next logon)" if installed
            else "  (autostart not installed — run `miki-ops install`)"
        )
        return _ok(f"miki: {'running' if running else 'not running'}{note}")
    return _unsupported()


def is_running() -> bool:
    if sys.platform == "win32":
        return _win_pid() is not None
    if sys.platform == "darwin":
        return _mac_running()
    if sys.platform.startswith("linux"):
        return _linux_running()
    return False


def _unsupported() -> Result:
    return Result(
        2,
        [
            f"Service management is not implemented for {sys.platform}. Run the bot "
            "directly (`miki-sorter`) or via your OS's service manager."
        ],
    )


# ── Windows: Startup-folder autostart + process control ─────────────────────

def _win_app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "miki"


def _win_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _win_startup_vbs() -> Path:
    return _win_startup_dir() / "MikiSorter.vbs"


def _win_launcher_bat() -> Path:
    return _win_app_dir() / "run-miki.bat"


def _win_install(workdir: Path) -> Result:
    program = resolve_program()
    if program is None:
        return _err(
            f"miki: '{PROGRAM_NAME}' not found on PATH or ~/.local/bin — install it "
            "first (e.g. `uv sync`), then re-run from the project dir."
        )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    app_dir = _win_app_dir()
    app_dir.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / "miki.out.log"

    # A .bat carries the cd + redirect (robust quoting), and a Startup .vbs runs
    # it with a hidden window so logon autostart shows no console.
    bat = _win_launcher_bat()
    bat.write_text(
        "@echo off\r\n"
        f'cd /d "{workdir}"\r\n'
        f'"{program}" >> "{log}" 2>&1\r\n',
        encoding="utf-8",
    )
    startup = _win_startup_dir()
    startup.mkdir(parents=True, exist_ok=True)
    _win_startup_vbs().write_text(
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run """{bat}""", 0, False\r\n',
        encoding="utf-8",
    )
    return _ok(
        f"miki: installed autostart -> {_win_startup_vbs()}",
        f"miki: launcher -> {bat}  (program {program})",
        "installed. Start now with:  miki-ops load",
    )


def _win_load() -> Result:
    if _win_pid() is not None:
        return _ok("miki: already running")
    bat = _win_launcher_bat()
    if not bat.exists():
        return _err(f"miki: launcher missing ({bat}) — run `miki-ops install` first")
    vbs = _win_startup_vbs()
    # Launch through the hidden-window .vbs so a manual start matches autostart.
    launcher = vbs if vbs.exists() else None
    try:
        if launcher is not None:
            subprocess.Popen(["wscript", str(launcher)], close_fds=True)
        else:
            subprocess.Popen(
                ["cmd", "/c", str(bat)],
                creationflags=0x08000008,  # DETACHED_PROCESS | CREATE_NO_WINDOW
                close_fds=True,
            )
    except OSError as error:
        return _err(f"miki: load failed — {error}")
    return _ok("miki: started")


def _win_unload() -> Result:
    if _win_pid() is None:
        return _ok("miki: not running")
    result = _run(["taskkill", "/F", "/T", "/IM", WINDOWS_IMAGE])
    if result.returncode == 0:
        return _ok("miki: stopped")
    return _err(f"miki: stop failed — {result.stdout.strip() or result.stderr.strip()}")


def _win_restart() -> Result:
    messages = list(_win_unload().messages)
    # taskkill returns before the image is fully gone; wait briefly so the new
    # process can acquire the single-instance lock cleanly.
    import time

    for _ in range(20):
        if _win_pid() is None:
            break
        time.sleep(0.1)
    started = _win_load()
    return Result(started.code, messages + started.messages)


def _win_pid() -> int | None:
    try:
        out = _run(
            ["tasklist", "/fi", f"imagename eq {WINDOWS_IMAGE}", "/fo", "csv", "/nh"],
            timeout=8,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if WINDOWS_IMAGE in line:
            try:
                return int(line.split('","')[1].strip('"'))
            except (IndexError, ValueError):
                return None
    return None


# ── Linux: systemd user service ─────────────────────────────────────────────

def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return _run(["systemctl", "--user", *args])


def _systemd_unit_path() -> Path:
    return SYSTEMD_USER_DIR / SYSTEMD_UNIT


def _backfill_service_path() -> Path:
    return SYSTEMD_USER_DIR / BACKFILL_SERVICE_UNIT


def _backfill_timer_path() -> Path:
    return SYSTEMD_USER_DIR / BACKFILL_TIMER_UNIT


def _systemd_unit_text(program: str, workdir: Path) -> str:
    bindir = str(Path(program).parent)
    log = LOG_DIR / "miki.out.log"
    return f"""[Unit]
Description=Miki Telegram sorter bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
Environment=PATH={bindir}:/usr/local/bin:/usr/bin:/bin
ExecStart={program}
Restart=on-failure
RestartSec=30
StandardOutput=append:{log}
StandardError=append:{log}

[Install]
WantedBy=default.target
"""


def _backfill_unit_text(program: str, workdir: Path) -> str:
    bindir = str(Path(program).parent)
    log = LOG_DIR / "miki-backfill.out.log"
    return f"""[Unit]
Description=Miki Telegram sorter bot backfill
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={workdir}
Environment=PATH={bindir}:/usr/local/bin:/usr/bin:/bin
ExecStart={bindir}/miki-burner backfill
StandardOutput=append:{log}
StandardError=append:{log}
"""


def _backfill_timer_text() -> str:
    return f"""[Unit]
Description=Run the Miki sorter bot backfill periodically

[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true


[Install]
WantedBy=timers.target
"""


def _linux_install(workdir: Path) -> Result:
    program = resolve_program()
    if program is None:
        return _err(
            f"miki: '{PROGRAM_NAME}' not found on PATH or ~/.local/bin — install it "
            "first (e.g. `pip install .`), then re-run from the project dir."
        )
    if shutil.which("systemctl") is None:
        return _err(
            "miki: systemctl not found. This host has no systemd user session; run "
            "the bot directly (`miki-sorter`) or add it to your init system manually."
        )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    path = _systemd_unit_path()
    path.write_text(_systemd_unit_text(program, workdir), encoding="utf-8")
    backfill_service = _backfill_service_path()
    backfill_service.write_text(_backfill_unit_text(program, workdir), encoding="utf-8")
    backfill_timer = _backfill_timer_path()
    backfill_timer.write_text(_backfill_timer_text(), encoding="utf-8")
    _systemctl("daemon-reload")
    enabled = _systemctl("enable", SYSTEMD_UNIT)
    messages = [
        f"miki: wrote {path} -> {program}",
        f"miki: wrote {backfill_timer} (service {backfill_service.name})",
    ]
    if enabled.returncode != 0:
        messages.append(
            f"miki: warning — could not enable autostart: {enabled.stderr.strip()}"
        )
    else:
        messages.append("miki: enabled autostart at logon")
    timer_enabled = _systemctl("enable", BACKFILL_TIMER_UNIT)
    if timer_enabled.returncode != 0:
        messages.append(
            "miki: warning — could not enable backfill timer: "
            f"{timer_enabled.stderr.strip()}"
        )
    else:
        messages.append("miki: enabled periodic backfill timer")
    messages.append(
        "installed. Start now with:  miki-ops load  "
        "(tip: `loginctl enable-linger $USER` to run without an active session)"
    )
    return _ok(*messages)


def _linux_uninstall() -> Result:
    messages = list(_linux_unload().messages)
    _systemctl("stop", BACKFILL_TIMER_UNIT)
    _systemctl("disable", SYSTEMD_UNIT)
    _systemctl("disable", BACKFILL_TIMER_UNIT)
    for path in (
        _systemd_unit_path(),
        _backfill_service_path(),
        _backfill_timer_path(),
    ):
        if path.exists():
            path.unlink()
            messages.append(f"miki: removed {path}")
    _systemctl("daemon-reload")
    return _ok(*messages)


def _linux_load() -> Result:
    path = _systemd_unit_path()
    if not path.exists():
        return _err(f"miki: unit missing ({path}) — run `miki-ops install` first")
    result = _systemctl("start", SYSTEMD_UNIT)
    if result.returncode == 0:
        return _ok("miki: started")
    return _err(f"miki: load failed - {result.stderr.strip()}")


def _linux_unload() -> Result:
    if not _systemd_unit_path().exists():
        return _ok()
    result = _systemctl("stop", SYSTEMD_UNIT)
    if result.returncode == 0:
        return _ok("miki: stopped")
    return _err(f"miki: unload failed - {result.stderr.strip()}")


def _linux_restart() -> Result:
    path = _systemd_unit_path()
    if not path.exists():
        return _err(f"miki: unit missing ({path}) — run `miki-ops install` first")
    result = _systemctl("restart", SYSTEMD_UNIT)
    if result.returncode == 0:
        return _ok("miki: restarted")
    return _err(f"miki: restart failed - {result.stderr.strip()}")


def _linux_running() -> bool:
    result = _systemctl("is-active", SYSTEMD_UNIT)
    return result.stdout.strip() == "active"


# ── macOS: launchd LaunchAgent ──────────────────────────────────────────────

def _plist_path() -> Path:
    return LAUNCH_AGENTS / f"{SERVICE_LABEL}.plist"


def _mac_install(workdir: Path) -> Result:
    program = resolve_program()
    if program is None:
        return _err(f"miki: '{PROGRAM_NAME}' not found on PATH or in ~/.local/bin")
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = _plist_path()
    path.write_text(_plist_xml(SERVICE_LABEL, program, workdir), encoding="utf-8")
    return _ok(f"miki: wrote {path} -> {program}", "installed. Run: miki-ops load")


def _mac_uninstall() -> Result:
    messages = list(_mac_unload().messages)
    path = _plist_path()
    if path.exists():
        path.unlink()
        messages.append(f"miki: removed {path}")
    return _ok(*messages)


def _mac_load() -> Result:
    path = _plist_path()
    if not path.exists():
        return _err(f"miki: plist missing ({path})")
    result = _run(["/bin/launchctl", "load", str(path)])
    if result.returncode == 0:
        return _ok("miki: loaded")
    return _err(f"miki: load failed - {result.stderr.strip()}")


def _mac_unload() -> Result:
    path = _plist_path()
    if not path.exists():
        return _ok()
    result = _run(["/bin/launchctl", "unload", str(path)])
    if result.returncode == 0:
        return _ok("miki: unloaded")
    return _err(f"miki: unload failed - {result.stderr.strip()}")


def _mac_restart() -> Result:
    uid = _run(["/usr/bin/id", "-u"]).stdout.strip()
    result = _run(
        ["/bin/launchctl", "kickstart", "-k", f"gui/{uid}/{SERVICE_LABEL}"]
    )
    if result.returncode == 0:
        return _ok("miki: restarted")
    return _err(f"miki: restart failed - {result.stderr.strip()}")


def _mac_running() -> bool:
    uid = _run(["/usr/bin/id", "-u"]).stdout.strip()
    result = _run(["/bin/launchctl", "print", f"gui/{uid}/{SERVICE_LABEL}"])
    return result.returncode == 0


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
