from __future__ import annotations

from pathlib import Path

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
    assert str(workdir) in bat_text  # cd to the .env directory
    assert str(program) in bat_text  # runs the resolved program
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


def test_linux_install_writes_systemd_unit(tmp_path, monkeypatch) -> None:
    # _linux_install writes the unit and shells out to systemctl; stub systemctl
    # and shutil.which so it can be exercised on any host by redirecting dirs.
    logdir = tmp_path / "log"
    unitdir = tmp_path / "systemd"
    monkeypatch.setattr(service, "LOG_DIR", logdir)
    monkeypatch.setattr(service, "SYSTEMD_USER_DIR", unitdir)
    program = tmp_path / "venv" / "bin" / "miki-sorter"
    monkeypatch.setattr(service, "resolve_program", lambda: str(program))
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(service, "_systemctl", lambda *args: _fake_completed(0, "", ""))

    workdir = tmp_path / "project"
    result = service._linux_install(workdir)

    assert result.code == 0
    unit = unitdir / service.SYSTEMD_UNIT
    assert unit.exists()
    text = unit.read_text(encoding="utf-8")
    assert f"WorkingDirectory={workdir}" in text  # cd to the .env directory
    assert f"ExecStart={program}" in text  # runs the resolved program
    assert str(logdir / "miki.out.log") in text  # redirects to the log
    assert "Restart=on-failure" in text  # crash-restart

    backfill_service_path = unitdir / service.BACKFILL_SERVICE_UNIT
    assert backfill_service_path.exists()
    backtext = backfill_service_path.read_text(encoding="utf-8")
    assert "Type=oneshot" in backtext
    assert "miki-burner backfill" in backtext

    backfill_timer_path = unitdir / service.BACKFILL_TIMER_UNIT
    assert backfill_timer_path.exists()
    timetext = backfill_timer_path.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 04:00:00" in timetext
    assert "Persistent=true" in timetext
    assert "OnUnitActiveSec" not in timetext


def test_linux_install_without_program_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(service, "SYSTEMD_USER_DIR", tmp_path / "systemd")
    monkeypatch.setattr(service, "resolve_program", lambda: None)

    result = service._linux_install(tmp_path)

    assert result.code == 1
    assert "not found" in result.messages[0]


def _fake_completed(code, out, err):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


def test_unsupported_platform_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(service.sys, "platform", "sunos")
    result = service.install(Path("."))
    assert result.code == 2
    assert "not implemented" in result.messages[0]


def test_linux_uninstall_removes_backfill_timer_and_service(tmp_path, monkeypatch) -> None:
    # Stub _linux_install and then uninstall to ensure timer and service files are removed
    logdir = tmp_path / "log"
    unitdir = tmp_path / "systemd"
    monkeypatch.setattr(service, "LOG_DIR", logdir)
    monkeypatch.setattr(service, "SYSTEMD_USER_DIR", unitdir)
    program = tmp_path / "venv" / "bin" / "miki-sorter"
    monkeypatch.setattr(service, "resolve_program", lambda: str(program))
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(service, "_systemctl", lambda *args: _fake_completed(0, "", ""))

    workdir = tmp_path / "project"
    # Run install to create files
    result_install = service._linux_install(workdir)
    assert result_install.code == 0

    # Now uninstall
    monkeypatch.setattr(service, "_systemctl", lambda *args: _fake_completed(0, "", ""))
    result_uninstall = service._linux_uninstall()
    assert result_uninstall.code == 0
    backfill_service_path = unitdir / service.BACKFILL_SERVICE_UNIT
    backfill_timer_path = unitdir / service.BACKFILL_TIMER_UNIT
    assert not backfill_service_path.exists()
    assert not backfill_timer_path.exists()


def test_win_uninstall_removes_startup_and_launcher(tmp_path, monkeypatch):
    appdata = tmp_path / "AppData" / "Roaming"
    localappdata = tmp_path / "AppData" / "Local"
    appdata.mkdir(parents=True)
    localappdata.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(service, "resolve_program", lambda: tmp_path / "program")

    service._win_install(tmp_path / "workdir")
    assert service._win_startup_vbs().exists() and service._win_launcher_bat().exists()
    monkeypatch.setattr(service, "_win_unload", lambda: service._ok("miki: not running"))

    result = service._win_uninstall()
    assert result.code == 0
    assert not service._win_startup_vbs().exists()
    assert not service._win_launcher_bat().exists()

    result_second = service._win_uninstall()
    assert result_second.code == 0
