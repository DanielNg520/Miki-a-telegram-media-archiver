# Linux port

This folder is a Linux-ready copy of Miki. The application code was already
cross-platform (the single-instance lock uses `fcntl` on POSIX); the only gap
was `miki-ops` service management, which previously supported only macOS
(launchd) and Windows (Startup folder).

## What changed vs. the parent copy

- `miki_sorter_bot/service.py` — added a **systemd user service** backend
  (`systemctl --user`). `install`/`uninstall`/`load`/`unload`/`restart`/`status`
  now work on Linux:
  - `install` writes `~/.config/systemd/user/miki-sorter.service`
    (`Type=simple`, `Restart=on-failure`, `RestartSec=30`, logs appended to
    `~/.local/log/miki.out.log`), runs `daemon-reload`, and enables autostart.
  - `load`/`unload`/`restart` map to `systemctl --user start|stop|restart`.
  - `status`/`is_running` use `systemctl --user is-active`.
  - Falls back with clear guidance if `systemctl` is unavailable.
- `miki_sorter_bot/ops.py` — `install` help text now mentions systemd.
- `README.md` — service-management section documents the systemd backend and
  the `loginctl enable-linger $USER` tip for headless/VPS use.
- `tests/test_service.py` — added Linux install coverage.

## Setup on Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
cp .env.sample .env   # then fill in real values

miki-ops install                 # writes + enables the systemd user unit
loginctl enable-linger $USER     # keep it running without an active session
miki-ops load                    # start now
miki-ops service-status
```

Autostart-crash-restart is handled by systemd (`Restart=on-failure`). Only one
Miki instance may run per bot token; the OS-level single-instance lock enforces
this locally.
