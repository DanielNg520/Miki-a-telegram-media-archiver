"""Runtime-tunable settings, chat-configurable and self-healing.

Every behavioural knob Miki exposes to operators lives here as a typed
:class:`SettingSpec`. The registry is the single source of truth: it knows how to
parse/validate a value from a chat message, how to render it back, and what the
``.env`` default is. A :class:`LiveSettings` facade resolves the *effective*
value on every read, so changing a setting via Telegram takes effect with no
restart and no cached staleness.

Design notes (patterns):
- **Registry of descriptors** — adding a new chat-configurable setting is one
  ``SettingSpec`` entry; the ``/config``, ``/set`` and ``/reset`` commands and the
  live resolver all pick it up automatically. No per-setting plumbing.
- **Strategy** — each spec carries its own ``parse``/``render``/``default``.
- **Self-healing** — if a stored override is unparseable (corruption, a schema
  change, a hand-edited DB), the resolver logs once, deletes the poisoned value,
  and falls back to the ``.env`` default instead of breaking delivery.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)


class RuntimeStore(Protocol):
    """The slice of the repository the registry needs (KV overrides)."""

    def get_runtime_setting(self, key: str) -> str | None: ...

    def set_runtime_setting(
        self, key: str, value: str, updated_by_user_id: int | None = None
    ) -> None: ...

    def delete_runtime_setting(self, key: str) -> bool: ...


# -- value parsers / renderers (strategies) ------------------------------
def parse_bool(raw: str) -> bool:
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on", "y"}:
        return True
    if value in {"0", "false", "no", "off", "n"}:
        return False
    raise ValueError("expected a boolean: true/false, on/off, yes/no")


def render_bool(value: bool) -> str:
    return "true" if value else "false"


def bounded_float(low: float, high: float) -> Callable[[str], float]:
    def parse(raw: str) -> float:
        try:
            value = float(raw.strip())
        except ValueError as error:
            raise ValueError(f"expected a number between {low:g} and {high:g}") from error
        if not low <= value <= high:
            raise ValueError(f"must be between {low:g} and {high:g}")
        return value

    return parse


def bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        try:
            value = int(raw.strip())
        except ValueError as error:
            raise ValueError(f"expected a whole number between {low} and {high}") from error
        if not low <= value <= high:
            raise ValueError(f"must be between {low} and {high}")
        return value

    return parse


def render_number(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    category: str
    description: str
    parse: Callable[[str], Any]
    render: Callable[[Any], str]
    default: Callable[[Any], Any]


@dataclass(frozen=True, slots=True)
class SettingView:
    key: str
    category: str
    description: str
    value: str
    default: str
    overridden: bool


class UnknownSettingError(KeyError):
    """Raised when a key is not a registered, chat-configurable setting."""


class SettingsRegistry:
    def __init__(self, specs: Iterable[SettingSpec]) -> None:
        self._specs: dict[str, SettingSpec] = {spec.key: spec for spec in specs}

    def __contains__(self, key: str) -> bool:
        return key in self._specs

    def keys(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def require(self, key: str) -> SettingSpec:
        spec = self._specs.get(key)
        if spec is None:
            raise UnknownSettingError(key)
        return spec

    def resolve(self, key: str, settings: Any, store: RuntimeStore) -> Any:
        spec = self.require(key)
        raw = store.get_runtime_setting(key)
        if raw is None:
            return spec.default(settings)
        try:
            return spec.parse(raw)
        except (ValueError, TypeError) as error:
            LOGGER.warning(
                "Discarding invalid runtime setting %s=%r (%s); reverting to default",
                key,
                raw,
                error,
            )
            # Self-heal: drop the poisoned override so it cannot keep biting.
            try:
                store.delete_runtime_setting(key)
            except Exception:  # pragma: no cover - defensive, never fatal to a read
                LOGGER.debug("Could not delete poisoned runtime setting %s", key)
            return spec.default(settings)

    def set(self, key: str, raw: str, settings: Any, store: RuntimeStore, user_id: int | None) -> Any:
        spec = self.require(key)
        value = spec.parse(raw)  # validates; raises ValueError on bad input
        store.set_runtime_setting(key, spec.render(value), user_id)
        return value

    def reset(self, key: str, store: RuntimeStore) -> bool:
        self.require(key)
        return store.delete_runtime_setting(key)

    def describe(self, settings: Any, store: RuntimeStore) -> list[SettingView]:
        views: list[SettingView] = []
        for key, spec in self._specs.items():
            overridden = store.get_runtime_setting(key) is not None
            value = self.resolve(key, settings, store)
            views.append(
                SettingView(
                    key=key,
                    category=spec.category,
                    description=spec.description,
                    value=spec.render(value),
                    default=spec.render(spec.default(settings)),
                    overridden=overridden,
                )
            )
        return views


def default_registry() -> SettingsRegistry:
    """The behavioural knobs that are safe to change live, from chat.

    Deliberately excludes secrets and transport/infrastructure settings
    (``BOT_TOKEN``, ``WEBHOOK_URL``, ``DATABASE_PATH`` ...): those are not
    hot-swappable and must not be exposed over chat.
    """

    return SettingsRegistry(
        [
            SettingSpec(
                "album_flush_delay_seconds",
                "albums",
                "Seconds to wait after the last album member before forwarding the group.",
                bounded_float(0.1, 30.0),
                render_number,
                lambda s: float(getattr(s, "album_flush_delay_seconds", 5.0)),
            ),
            SettingSpec(
                "album_max_wait_seconds",
                "albums",
                "Max seconds to hold an unrouted album waiting for a route decision.",
                bounded_float(1.0, 300.0),
                render_number,
                lambda s: float(getattr(s, "album_max_wait_seconds", 30.0)),
            ),
            SettingSpec(
                "lookback_enabled",
                "lookback",
                "Forward recent uncaptioned media when a later hashtag-only message matches a route.",
                parse_bool,
                render_bool,
                lambda s: bool(getattr(s, "lookback_enabled", True)),
            ),
            SettingSpec(
                "lookback_ttl_seconds",
                "lookback",
                "How long recent uncaptioned media stays claimable by a later hashtag.",
                bounded_float(5.0, 3600.0),
                render_number,
                lambda s: float(getattr(s, "lookback_ttl_seconds", 120.0)),
            ),
            SettingSpec(
                "lookback_capacity",
                "lookback",
                "How many recent uncaptioned media items to remember per topic.",
                bounded_int(1, 50),
                render_number,
                lambda s: int(getattr(s, "lookback_capacity", 5)),
            ),
            SettingSpec(
                "send_confirmation",
                "delivery",
                "Reply with a short confirmation after sorting each item.",
                parse_bool,
                render_bool,
                lambda s: bool(getattr(s, "send_confirmation", False)),
            ),
            SettingSpec(
                "sort_dry_run",
                "delivery",
                "Record what would be sorted without actually delivering it.",
                parse_bool,
                render_bool,
                lambda s: bool(getattr(s, "sort_dry_run", False)),
            ),
        ]
    )


class LiveSettings:
    """Read-through facade resolving effective values on every access.

    Domain code depends on this rather than on raw ``Settings`` so that any
    registered knob is chat-configurable and takes effect immediately.
    """

    def __init__(
        self,
        settings: Any,
        store: RuntimeStore,
        registry: SettingsRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry or default_registry()

    @property
    def registry(self) -> SettingsRegistry:
        return self._registry

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def store(self) -> RuntimeStore:
        return self._store

    def get(self, key: str) -> Any:
        return self._registry.resolve(key, self._settings, self._store)

    # Convenience accessors used on hot paths.
    def album_flush_delay(self) -> float:
        return float(self.get("album_flush_delay_seconds"))

    def album_max_wait(self) -> float:
        return float(self.get("album_max_wait_seconds"))

    def lookback_enabled(self) -> bool:
        return bool(self.get("lookback_enabled"))

    def lookback_ttl(self) -> float:
        return float(self.get("lookback_ttl_seconds"))

    def lookback_capacity(self) -> int:
        return int(self.get("lookback_capacity"))

    def send_confirmation(self) -> bool:
        return bool(self.get("send_confirmation"))

    def sort_dry_run(self) -> bool:
        return bool(self.get("sort_dry_run"))
