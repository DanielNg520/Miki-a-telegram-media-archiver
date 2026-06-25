"""Self-healing supervision for Telegram webhook registration.

Webhook mode has one fragile, unmanaged dependency that polling does not: the
registration Telegram holds for *us*. Telegram silently stops delivering after a
run of endpoint errors (a cert blip, an nginx restart, brief downtime) and only
resumes once the webhook is re-set. Nothing in the bot re-set it.

This module adds that missing layer as a small **reconciliation control loop**
(desired state vs. observed state, idempotent reconcile -- the Kubernetes
controller idiom), guarded by a **circuit breaker** so self-heal can never flap
or hammer the Bot API, and fed by a **heartbeat watchdog** for liveness. The
read path (``snapshot``) is a cache refreshed by the loop, so ``/healthz``,
``/metrics``, ``/doctor`` and ``/status`` never block on a live Bot API call.

A :class:`NullWebhookSupervisor` keeps every read path uniform under polling.
"""

from __future__ import annotations

import logging
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol

from telegram import Update, WebhookInfo

from miki_sorter_bot.config import Settings

LOGGER = logging.getLogger(__name__)

Clock = Callable[[], float]
Now = Callable[[], datetime]
MetricSink = Callable[[str, int], None]


@dataclass(frozen=True, slots=True)
class DesiredWebhookState:
    """The registration we want Telegram to hold. Single source of truth.

    Reused by ``run_webhook(...)`` at startup so the initial registration and
    every later reconcile agree -- they can never fight over parameters.
    """

    url: str
    secret_token: str | None
    max_connections: int
    allowed_updates: tuple[str, ...]
    drop_pending_updates: bool = False


def webhook_desired_state(settings: Settings) -> DesiredWebhookState:
    return DesiredWebhookState(
        url=settings.webhook_url,
        secret_token=settings.webhook_secret_token or None,
        max_connections=settings.webhook_max_connections,
        allowed_updates=tuple(Update.ALL_TYPES),
        drop_pending_updates=False,
    )


class Heartbeat:
    """Monotonic liveness marker bumped on every received update."""

    def __init__(self, *, clock: Clock = _time.monotonic) -> None:
        self._clock = clock
        self._last = clock()

    async def tap(self, _update: object, _context: object) -> None:
        """PTB handler callback: any inbound update proves we are alive."""

        self._last = self._clock()

    def mark(self) -> None:
        self._last = self._clock()

    def seconds_since(self) -> float:
        return max(0.0, self._clock() - self._last)


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Trips OPEN after consecutive failures; allows one HALF_OPEN trial after a
    cooldown; closes on the first success. Keeps self-heal from hammering the Bot
    API when the underlying problem (DNS, cert, routing) is not yet fixed."""

    def __init__(
        self,
        *,
        failure_threshold: int,
        reset_seconds: float,
        clock: Clock = _time.monotonic,
    ) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._reset_seconds = max(0.0, reset_seconds)
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self._reset_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def allow(self) -> bool:
        return self.state is not BreakerState.OPEN

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._opened_at = self._clock()


@dataclass(frozen=True, slots=True)
class WebhookHealth:
    """Immutable snapshot published by the reconcile loop and read elsewhere.

    Frozen + replaced by atomic reference assignment, so the health-server
    thread can read a consistent snapshot without locking.
    """

    mode: str
    enabled: bool
    healthy: bool
    wedged: bool
    url_matches: bool
    registered_url: str
    expected_url: str
    pending_update_count: int
    seconds_since_update: float
    last_error_message: str | None
    last_error_age_seconds: float | None
    breaker_state: str
    consecutive_heal_failures: int
    reconciliations: int
    last_reconcile_outcome: str
    observed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "healthy": self.healthy,
            "wedged": self.wedged,
            "url_matches": self.url_matches,
            "registered_url": self.registered_url,
            "expected_url": self.expected_url,
            "pending_update_count": self.pending_update_count,
            "seconds_since_update": round(self.seconds_since_update, 1),
            "last_error_message": self.last_error_message,
            "last_error_age_seconds": (
                round(self.last_error_age_seconds, 1)
                if self.last_error_age_seconds is not None
                else None
            ),
            "breaker_state": self.breaker_state,
            "consecutive_heal_failures": self.consecutive_heal_failures,
            "reconciliations": self.reconciliations,
            "last_reconcile_outcome": self.last_reconcile_outcome,
            "observed_at": self.observed_at,
        }

    def summary_lines(self) -> list[str]:
        if not self.enabled:
            return [f"- webhook: supervision disabled ({self.mode} mode)"]
        status = "wedged" if self.wedged else ("healthy" if self.healthy else "degraded")
        lines = [
            f"- webhook: {status} (last action: {self.last_reconcile_outcome})",
            f"- webhook url match: {'yes' if self.url_matches else 'NO'}",
            f"- webhook pending updates: {self.pending_update_count}",
            f"- seconds since last update: {round(self.seconds_since_update)}",
            f"- self-heal breaker: {self.breaker_state} "
            f"(reconciliations={self.reconciliations})",
        ]
        if self.last_error_message:
            age = (
                f", {round(self.last_error_age_seconds)}s ago"
                if self.last_error_age_seconds is not None
                else ""
            )
            lines.append(f"- last Telegram webhook error: {self.last_error_message}{age}")
        return lines


class SupervisorLike(Protocol):
    def snapshot(self) -> WebhookHealth: ...

    async def reconcile(self) -> WebhookHealth: ...


class WebhookSupervisor:
    """Reconciles Telegram's webhook registration toward the desired state."""

    def __init__(
        self,
        *,
        bot: Any,
        settings: Settings,
        heartbeat: Heartbeat,
        increment_metric: MetricSink,
        breaker: CircuitBreaker | None = None,
        clock: Clock = _time.monotonic,
        now: Now | None = None,
    ) -> None:
        self._bot = bot
        self._settings = settings
        self._heartbeat = heartbeat
        self._increment_metric = increment_metric
        self._desired = webhook_desired_state(settings)
        self._breaker = breaker or CircuitBreaker(
            failure_threshold=settings.webhook_heal_failure_threshold,
            reset_seconds=settings.webhook_heal_reset_seconds,
            clock=clock,
        )
        self._now: Now = now or (lambda: datetime.now(UTC))
        self._reconciliations = 0
        self._consecutive_heal_failures = 0
        self._last_outcome = "pending"
        # Drift reason of a heal awaiting effectiveness confirmation on the next
        # tick. This is what stops the "self-heal every 2 minutes forever" spin:
        # re-registering does not drain a backlog, so if the same drift survives a
        # heal we treat it as a breaker failure and back off instead of retrying.
        self._awaiting_confirmation: str | None = None
        # Strategy list: each detector returns a drift reason or None. A pure
        # pending backlog is intentionally NOT a trigger -- re-registering only
        # re-floods it; only a lost/wrong URL or Telegram-reported errors warrant
        # a re-registration.
        self._drift_detectors: Sequence[Callable[[WebhookInfo], str | None]] = (
            self._detect_url_drift,
            self._detect_active_errors,
            self._detect_stale_liveness,
        )
        self._snapshot = self._build_snapshot(observed=None, drift=None)

    # -- read path -------------------------------------------------------
    def snapshot(self) -> WebhookHealth:
        return self._snapshot

    # -- control loop ----------------------------------------------------
    async def reconcile(self) -> WebhookHealth:
        observed = await self._observe()
        drift: str | None = None
        if observed is not None:
            drift = self._detect_drift(observed)
            if self._awaiting_confirmation is not None:
                # We re-registered last tick; judge whether it worked before
                # doing anything else. Never heal again in the same tick.
                self._confirm_previous_heal(drift)
            elif drift is None:
                self._last_outcome = "ok"
            elif self._breaker.allow():
                await self._heal(drift)
            else:
                self._last_outcome = f"suppressed:{drift}"
                self._increment_metric("webhook_heal_suppressed", 1)
        else:
            self._last_outcome = "observe_failed"
        self._snapshot = self._build_snapshot(observed=observed, drift=drift)
        return self._snapshot

    def _confirm_previous_heal(self, drift: str | None) -> None:
        """Grade the previous heal: drift gone => effective; still here => back off."""

        if drift is None:
            self._breaker.record_success()
            self._consecutive_heal_failures = 0
            self._increment_metric("webhook_heal_confirmed", 1)
            self._last_outcome = "ok"
        else:
            self._breaker.record_failure()
            self._consecutive_heal_failures += 1
            self._increment_metric("webhook_heal_ineffective", 1)
            self._last_outcome = f"backing_off:{drift}"
        self._awaiting_confirmation = None

    async def _observe(self) -> WebhookInfo | None:
        try:
            return await self._bot.get_webhook_info()
        except Exception as error:  # Telegram/network/OS -- never fatal to the loop
            LOGGER.warning("Could not read webhook info: %s", error)
            self._increment_metric("webhook_observe_failures", 1)
            return None

    def _detect_drift(self, observed: WebhookInfo) -> str | None:
        for detector in self._drift_detectors:
            reason = detector(observed)
            if reason:
                return reason
        return None

    async def _heal(self, reason: str) -> None:
        """Re-register the webhook once. Effectiveness is graded next tick.

        A successful API call does NOT reset the breaker -- only a confirmed
        drift clearance does. That is what turns a persistent, unfixable problem
        into exponential back-off instead of an endless every-tick re-register.
        """

        try:
            await self._bot.set_webhook(
                url=self._desired.url,
                secret_token=self._desired.secret_token,
                max_connections=self._desired.max_connections,
                allowed_updates=list(self._desired.allowed_updates),
                drop_pending_updates=self._desired.drop_pending_updates,
            )
        except Exception as error:
            self._breaker.record_failure()
            self._consecutive_heal_failures += 1
            self._increment_metric("webhook_reconcile_failures", 1)
            self._last_outcome = f"heal_failed:{reason}"
            LOGGER.warning("Webhook re-registration call failed (%s): %s", reason, error)
            return
        self._reconciliations += 1
        self._awaiting_confirmation = reason
        self._increment_metric("webhook_reconciliations", 1)
        self._last_outcome = f"healing:{reason}"
        LOGGER.info(
            "Webhook re-registered; awaiting confirmation",
            extra={"reason": reason, "url": self._desired.url},
        )

    # -- drift detectors (strategies) -----------------------------------
    def _detect_url_drift(self, observed: WebhookInfo) -> str | None:
        registered = observed.url or ""
        if not registered:
            return "url_unset"
        if registered != self._desired.url:
            return "url_mismatch"
        return None

    def _detect_active_errors(self, observed: WebhookInfo) -> str | None:
        age = self._error_age_seconds(observed)
        recent_window = self._settings.webhook_reconcile_interval_seconds * 2
        if age is not None and age <= recent_window and (observed.pending_update_count or 0) > 0:
            return "delivery_errors"
        return None

    def _detect_stale_liveness(self, observed: WebhookInfo) -> str | None:
        if self._heartbeat.seconds_since() <= self._settings.webhook_stale_after_seconds:
            return None
        # Only act on staleness corroborated by a Telegram-side symptom, so a
        # genuinely quiet source never triggers a needless re-registration.
        age = self._error_age_seconds(observed)
        if (observed.pending_update_count or 0) > 0 or age is not None:
            return "stale_with_symptom"
        return None

    # -- snapshot assembly ----------------------------------------------
    def _error_age_seconds(self, observed: WebhookInfo) -> float | None:
        if observed.last_error_date is None:
            return None
        return max(0.0, (self._now() - observed.last_error_date).total_seconds())

    def _build_snapshot(
        self,
        *,
        observed: WebhookInfo | None,
        drift: str | None,
    ) -> WebhookHealth:
        if observed is not None:
            registered_url = observed.url or ""
            pending = observed.pending_update_count or 0
            last_error_message = observed.last_error_message
            last_error_age = self._error_age_seconds(observed)
        else:
            prev = getattr(self, "_snapshot", None)
            registered_url = prev.registered_url if prev else ""
            pending = prev.pending_update_count if prev else 0
            last_error_message = prev.last_error_message if prev else None
            last_error_age = prev.last_error_age_seconds if prev else None
        seconds_since = self._heartbeat.seconds_since()
        breaker_state = self._breaker.state
        wedged = (
            breaker_state is BreakerState.OPEN
            and seconds_since > self._settings.webhook_stale_after_seconds
        )
        healthy = not wedged and not self._last_outcome.startswith(
            ("suppressed", "heal_failed", "backing_off")
        )
        return WebhookHealth(
            mode="webhook",
            enabled=True,
            healthy=healthy,
            wedged=wedged,
            url_matches=registered_url == self._desired.url,
            registered_url=registered_url,
            expected_url=self._desired.url,
            pending_update_count=pending,
            seconds_since_update=seconds_since,
            last_error_message=last_error_message,
            last_error_age_seconds=last_error_age,
            breaker_state=breaker_state.value,
            consecutive_heal_failures=self._consecutive_heal_failures,
            reconciliations=self._reconciliations,
            last_reconcile_outcome=self._last_outcome,
            observed_at=self._now().isoformat(),
        )


class NullWebhookSupervisor:
    """No-op supervisor for polling mode; keeps read paths uniform."""

    def __init__(self, *, mode: str = "polling") -> None:
        self._snapshot = WebhookHealth(
            mode=mode,
            enabled=False,
            healthy=True,
            wedged=False,
            url_matches=True,
            registered_url="",
            expected_url="",
            pending_update_count=0,
            seconds_since_update=0.0,
            last_error_message=None,
            last_error_age_seconds=None,
            breaker_state="closed",
            consecutive_heal_failures=0,
            reconciliations=0,
            last_reconcile_outcome="disabled",
            observed_at=datetime.now(UTC).isoformat(),
        )

    def snapshot(self) -> WebhookHealth:
        return self._snapshot

    async def reconcile(self) -> WebhookHealth:
        return self._snapshot
