from __future__ import annotations

from types import SimpleNamespace

import pytest

from miki_sorter_bot.settings_registry import (
    LiveSettings,
    UnknownSettingError,
    bounded_float,
    bounded_int,
    default_registry,
    parse_bool,
)


class FakeStore:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(initial or {})

    def get_runtime_setting(self, key: str) -> str | None:
        return self.data.get(key)

    def set_runtime_setting(self, key: str, value: str, updated_by_user_id: int | None = None) -> None:
        self.data[key] = value

    def delete_runtime_setting(self, key: str) -> bool:
        return self.data.pop(key, None) is not None

    def list_runtime_settings(self) -> dict[str, str]:
        return dict(self.data)


def _settings(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "album_flush_delay_seconds": 5.0,
        "album_max_wait_seconds": 30.0,
        "lookback_enabled": True,
        "lookback_ttl_seconds": 120.0,
        "lookback_capacity": 5,
        "send_confirmation": False,
        "sort_dry_run": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- parsers -------------------------------------------------------------
@pytest.mark.parametrize("raw", ["true", "on", "YES", "1", "y"])
def test_parse_bool_truthy(raw: str) -> None:
    assert parse_bool(raw) is True


@pytest.mark.parametrize("raw", ["false", "off", "No", "0", "n"])
def test_parse_bool_falsy(raw: str) -> None:
    assert parse_bool(raw) is False


def test_parse_bool_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_bool("maybe")


def test_bounded_float_enforces_range() -> None:
    parse = bounded_float(1.0, 10.0)
    assert parse(" 4.5 ") == 4.5
    with pytest.raises(ValueError):
        parse("0.5")
    with pytest.raises(ValueError):
        parse("nope")


def test_bounded_int_enforces_range() -> None:
    parse = bounded_int(1, 50)
    assert parse("5") == 5
    with pytest.raises(ValueError):
        parse("0")


# --- registry resolve / set / reset -------------------------------------
def test_resolve_returns_env_default_without_override() -> None:
    registry = default_registry()
    store = FakeStore()
    assert registry.resolve("album_flush_delay_seconds", _settings(), store) == 5.0


def test_resolve_returns_override_when_set() -> None:
    registry = default_registry()
    store = FakeStore({"album_flush_delay_seconds": "12"})
    assert registry.resolve("album_flush_delay_seconds", _settings(), store) == 12.0


def test_resolve_self_heals_poisoned_override() -> None:
    registry = default_registry()
    store = FakeStore({"album_flush_delay_seconds": "not-a-number"})
    value = registry.resolve("album_flush_delay_seconds", _settings(), store)
    assert value == 5.0  # fell back to default
    assert "album_flush_delay_seconds" not in store.data  # and dropped the poison


def test_set_validates_and_persists_rendered_value() -> None:
    registry = default_registry()
    store = FakeStore()
    value = registry.set("lookback_enabled", "off", _settings(), store, user_id=10)
    assert value is False
    assert store.data["lookback_enabled"] == "false"


def test_set_rejects_invalid_value() -> None:
    registry = default_registry()
    store = FakeStore()
    with pytest.raises(ValueError):
        registry.set("lookback_ttl_seconds", "99999", _settings(), store, user_id=10)


def test_set_unknown_key_raises() -> None:
    registry = default_registry()
    with pytest.raises(UnknownSettingError):
        registry.set("bot_token", "secret", _settings(), FakeStore(), user_id=10)


def test_reset_removes_override() -> None:
    registry = default_registry()
    store = FakeStore({"sort_dry_run": "true"})
    assert registry.reset("sort_dry_run", store) is True
    assert registry.reset("sort_dry_run", store) is False


def test_describe_marks_overridden() -> None:
    registry = default_registry()
    store = FakeStore({"lookback_capacity": "9"})
    views = {view.key: view for view in registry.describe(_settings(), store)}
    assert views["lookback_capacity"].value == "9"
    assert views["lookback_capacity"].overridden is True
    assert views["album_flush_delay_seconds"].overridden is False


# --- LiveSettings facade -------------------------------------------------
def test_live_settings_reflect_overrides_immediately() -> None:
    store = FakeStore()
    live = LiveSettings(_settings(), store)
    assert live.lookback_ttl() == 120.0
    live.registry.set("lookback_ttl_seconds", "300", live.settings, live.store, user_id=1)
    assert live.lookback_ttl() == 300.0  # no caching; resolved live


def test_live_settings_accessors_cover_registered_keys() -> None:
    live = LiveSettings(_settings(), FakeStore())
    assert live.album_flush_delay() == 5.0
    assert live.album_max_wait() == 30.0
    assert live.lookback_enabled() is True
    assert live.lookback_capacity() == 5
    assert live.send_confirmation() is False
    assert live.sort_dry_run() is False
