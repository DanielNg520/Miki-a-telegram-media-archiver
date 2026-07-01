from __future__ import annotations

import sys
import types

import pytest

from miki_sorter_bot import burner_session
from miki_sorter_bot.burner_session import (
    TelethonNotInstalled,
    create_session_interactive,
    validate_session,
)


class _FakeClient:
    """Minimal stand-in for telethon.sync.TelegramClient."""

    instances: list[_FakeClient] = []

    def __init__(self, session: object, api_id: int, api_hash: str) -> None:
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.started = False
        self.disconnected = False
        _FakeClient.instances.append(self)

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnected = True

    def start(self) -> None:
        self.started = True

    def is_user_authorized(self) -> bool:
        return True


class _FakeStringSession:
    def __init__(self, value: str = "") -> None:
        self._value = value

    def save(self) -> str:
        return "MINTED-SESSION"


def _install_fake_telethon(monkeypatch: pytest.MonkeyPatch, client_cls: type) -> None:
    telethon = types.ModuleType("telethon")
    sync = types.ModuleType("telethon.sync")
    sessions = types.ModuleType("telethon.sessions")
    sync.TelegramClient = client_cls  # type: ignore[attr-defined]
    sessions.StringSession = _FakeStringSession  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telethon", telethon)
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions)


def test_import_telethon_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the lazy import to fail even if telethon is installed.
    monkeypatch.setitem(sys.modules, "telethon.sync", None)
    monkeypatch.setitem(sys.modules, "telethon.sessions", None)
    with pytest.raises(TelethonNotInstalled):
        burner_session._import_telethon()


def test_create_session_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.instances.clear()
    _install_fake_telethon(monkeypatch, _FakeClient)

    session = create_session_interactive(123, "hash")

    assert session == "MINTED-SESSION"
    client = _FakeClient.instances[-1]
    assert client.started is True
    assert client.disconnected is True


def test_create_session_interactive_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Unauthorized(_FakeClient):
        def is_user_authorized(self) -> bool:
            return False

    _install_fake_telethon(monkeypatch, _Unauthorized)
    with pytest.raises(RuntimeError, match="not authorized"):
        create_session_interactive(123, "hash")


def test_validate_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_telethon(monkeypatch, _FakeClient)
    assert validate_session(123, "hash", "some-session") is True


def test_validate_session_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Unauthorized(_FakeClient):
        def is_user_authorized(self) -> bool:
            return False

    _install_fake_telethon(monkeypatch, _Unauthorized)
    assert validate_session(123, "hash", "some-session") is False


def test_main_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(burner_session, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("TELETHON_API_ID", raising=False)
    monkeypatch.delenv("TELETHON_API_HASH", raising=False)
    with pytest.raises(SystemExit, match="TELETHON_API_ID"):
        burner_session.main()


def test_main_prints_session(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(burner_session, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    monkeypatch.setattr(
        burner_session, "create_session_interactive", lambda api_id, api_hash: "SECRET-XYZ"
    )

    burner_session.main()

    out = capsys.readouterr().out
    assert "SECRET-XYZ" in out
    assert "FULL access" in out
