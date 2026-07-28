from __future__ import annotations

from pathlib import Path

import pytest

from miki_sorter_bot import service


def test_win_install_writes_self_contained_launcher(tmp_path, monkeypatch) -> None:
    # _win_install is platform-agnostic in what it writes (no Windows-only calls),
    # so it can be exercised on any host by redirecting its target directories.
    appdata = tmp_path / "roaming"
    localappdata = tmp_path / "local"
    logdir = tmp_path / "log"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setattr(service, "LOG_DIR", logdir)
    program = tmp_path / "venv" / "Scripts" / "miki-sorter.exe"
    monkeypatch.setattr(service, "resolve_program", lambda: str(program))

    workdir = tmp_path / "project"
    result = service._win_install(workdir)

    assert result.code == 0
    vbs = service._win_startup_vbs()
    bat = service._win_launcher_bat()
    assert vbs.exists() and bat.exists()

    bat_text = bat.read_text(encoding="utf-8")
    assert str(workdir) in bat_text        # cd to the .env directory
    assert str(program) in bat_text        # runs the resolved program
    assert str(logdir / "miki.out.log") in bat_text  # redirects to the log
    assert str(bat) in vbs.read_text(encoding="utf-8")  # vbs launches the bat


def test_win_install_without_program_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "r"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "l"))
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(service, "resolve_program", lambda: None)

    result = service._win_install(tmp_path)

    assert result.code == 1
    assert "not found" in result.messages[0]


def test_unsupported_platform_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(service.sys, "platform", "sunos")
    result = service.install(Path("."))
    assert result.code == 2
    assert "not implemented" in result.messages[0]
