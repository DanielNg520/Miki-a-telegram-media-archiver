from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import NetworkError

from miki_sorter_bot.webhook_supervisor import (
    BreakerState,
    CircuitBreaker,
    Heartbeat,
    NullWebhookSupervisor,
    WebhookSupervisor,
    webhook_desired_state,
)

EXPECTED_URL = "https://miki.example/telegram/webhook"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Metrics:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def __call__(self, name: str, amount: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + amount


def _settings(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "webhook_url": EXPECTED_URL,
        "webhook_secret_token": "secret",
        "webhook_max_connections": 40,
        "webhook_heal_failure_threshold": 3,
        "webhook_heal_reset_seconds": 300,
        "webhook_reconcile_interval_seconds": 120,
        "webhook_stale_after_seconds": 900,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _webhook_info(
    *,
    url: str = EXPECTED_URL,
    pending: int = 0,
    last_error_message: str | None = None,
    last_error_date: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        url=url,
        pending_update_count=pending,
        last_error_message=last_error_message,
        last_error_date=last_error_date,
    )


def _bot(info: object, *, set_webhook: AsyncMock | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        get_webhook_info=AsyncMock(return_value=info),
        set_webhook=set_webhook or AsyncMock(),
    )


def _supervisor(
    bot: object,
    *,
    settings: SimpleNamespace | None = None,
    heartbeat: Heartbeat | None = None,
    metrics: Metrics | None = None,
    breaker: CircuitBreaker | None = None,
    clock: FakeClock | None = None,
    now: datetime | None = None,
) -> WebhookSupervisor:
    return WebhookSupervisor(
        bot=bot,
        settings=settings or _settings(),
        heartbeat=heartbeat or Heartbeat(),
        increment_metric=metrics or Metrics(),
        breaker=breaker,
        clock=clock or (lambda: 0.0),
        now=(lambda: now) if now is not None else None,
    )


# --- circuit breaker -----------------------------------------------------
def test_circuit_breaker_opens_then_half_opens_then_closes() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=30, clock=clock)
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure()
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert breaker.allow() is False
    clock.advance(30)
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow() is True
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 0


# --- drift detection / healing ------------------------------------------
def test_reconcile_reregisters_on_url_drift() -> None:
    bot = _bot(_webhook_info(url="https://stale.example/old"))
    metrics = Metrics()
    sup = _supervisor(bot, metrics=metrics)

    asyncio.run(sup.reconcile())

    bot.set_webhook.assert_awaited_once()
    kwargs = bot.set_webhook.await_args.kwargs
    assert kwargs["url"] == EXPECTED_URL
    assert kwargs["secret_token"] == "secret"
    assert kwargs["drop_pending_updates"] is False
    assert metrics.counts.get("webhook_reconciliations") == 1
    assert sup.snapshot().last_reconcile_outcome == "healing:url_mismatch"


def test_reconcile_reregisters_when_url_unset() -> None:
    bot = _bot(_webhook_info(url=""))
    sup = _supervisor(bot)
    asyncio.run(sup.reconcile())
    bot.set_webhook.assert_awaited_once()
    assert sup.snapshot().last_reconcile_outcome == "healing:url_unset"


def test_effective_heal_is_confirmed_and_closes_breaker() -> None:
    # First observe is drifted; after the heal, observe returns a matching URL.
    bot = _bot(_webhook_info(url="https://stale.example/old"))
    metrics = Metrics()
    sup = _supervisor(bot, metrics=metrics)

    asyncio.run(sup.reconcile())  # detects drift -> heals -> awaits confirmation
    bot.get_webhook_info.return_value = _webhook_info()  # heal worked
    asyncio.run(sup.reconcile())  # confirms it stuck

    bot.set_webhook.assert_awaited_once()  # no second heal
    assert metrics.counts.get("webhook_heal_confirmed") == 1
    assert sup.snapshot().breaker_state == "closed"
    assert sup.snapshot().last_reconcile_outcome == "ok"


def test_persistent_drift_backs_off_instead_of_spinning() -> None:
    # set_webhook "succeeds" every time but never fixes the drift (the every-2-min
    # spin scenario). The breaker must open and suppress further heals.
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=10_000, clock=clock)
    bot = _bot(_webhook_info(url="https://stale.example/old"))
    metrics = Metrics()
    sup = _supervisor(bot, metrics=metrics, breaker=breaker, clock=clock)

    for _ in range(8):
        asyncio.run(sup.reconcile())

    # heal, confirm-fail, heal, confirm-fail(open), then suppressed -- far fewer
    # than 8 re-registrations, and the breaker is now open.
    assert bot.set_webhook.await_count == 2
    assert metrics.counts.get("webhook_heal_ineffective") == 2
    assert breaker.state is BreakerState.OPEN
    assert sup.snapshot().last_reconcile_outcome.startswith("suppressed")


def test_pending_backlog_alone_does_not_trigger_reregistration() -> None:
    # A backlog with a matching URL and no errors must NOT heal -- re-registering
    # only re-floods it.
    bot = _bot(_webhook_info(pending=500))
    sup = _supervisor(bot)
    asyncio.run(sup.reconcile())
    bot.set_webhook.assert_not_awaited()
    assert sup.snapshot().last_reconcile_outcome == "ok"


def test_reconcile_is_noop_when_registration_matches() -> None:
    bot = _bot(_webhook_info())
    sup = _supervisor(bot)
    asyncio.run(sup.reconcile())
    bot.set_webhook.assert_not_awaited()
    snapshot = sup.snapshot()
    assert snapshot.last_reconcile_outcome == "ok"
    assert snapshot.url_matches is True
    assert snapshot.healthy is True


def test_reconcile_heals_on_recent_delivery_errors() -> None:
    now = datetime(2026, 6, 24, tzinfo=UTC)
    bot = _bot(
        _webhook_info(pending=3, last_error_message="boom", last_error_date=now - timedelta(seconds=10))
    )
    sup = _supervisor(bot, now=now)
    asyncio.run(sup.reconcile())
    bot.set_webhook.assert_awaited_once()
    assert sup.snapshot().last_reconcile_outcome == "healing:delivery_errors"


def test_quiet_source_does_not_trigger_heal_even_when_stale() -> None:
    clock = FakeClock()
    heartbeat = Heartbeat(clock=clock)
    clock.advance(5000)  # far past stale threshold, but no Telegram-side symptom
    bot = _bot(_webhook_info())
    sup = _supervisor(bot, heartbeat=heartbeat, clock=clock)
    asyncio.run(sup.reconcile())
    bot.set_webhook.assert_not_awaited()
    assert sup.snapshot().last_reconcile_outcome == "ok"


def test_stale_with_symptom_triggers_heal() -> None:
    clock = FakeClock()
    heartbeat = Heartbeat(clock=clock)
    clock.advance(5000)
    bot = _bot(_webhook_info(pending=3))  # stale + a pending symptom
    sup = _supervisor(bot, heartbeat=heartbeat, clock=clock)
    asyncio.run(sup.reconcile())
    bot.set_webhook.assert_awaited_once()
    assert sup.snapshot().last_reconcile_outcome == "healing:stale_with_symptom"


# --- circuit breaker integration ----------------------------------------
def test_open_breaker_suppresses_heal() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=300, clock=clock)
    breaker.record_failure()  # OPEN
    bot = _bot(_webhook_info(url="https://stale.example/old"))
    metrics = Metrics()
    sup = _supervisor(bot, metrics=metrics, breaker=breaker, clock=clock)
    asyncio.run(sup.reconcile())
    bot.set_webhook.assert_not_awaited()
    assert metrics.counts.get("webhook_heal_suppressed") == 1
    assert sup.snapshot().last_reconcile_outcome == "suppressed:url_mismatch"


def test_half_open_breaker_allows_one_trial() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=300, clock=clock)
    breaker.record_failure()
    clock.advance(300)  # HALF_OPEN
    bot = _bot(_webhook_info(url="https://stale.example/old"))
    sup = _supervisor(bot, breaker=breaker, clock=clock)
    asyncio.run(sup.reconcile())
    bot.set_webhook.assert_awaited_once()  # half-open allowed the trial re-register
    assert sup.snapshot().last_reconcile_outcome == "healing:url_mismatch"


def test_repeated_heal_failures_trip_breaker() -> None:
    clock = FakeClock()
    bot = _bot(
        _webhook_info(url="https://stale.example/old"),
        set_webhook=AsyncMock(side_effect=NetworkError("set failed")),
    )
    metrics = Metrics()
    sup = _supervisor(bot, metrics=metrics, clock=clock)
    for _ in range(3):
        asyncio.run(sup.reconcile())
    assert metrics.counts.get("webhook_reconcile_failures") == 3
    snapshot = sup.snapshot()
    assert snapshot.breaker_state == "open"
    assert snapshot.consecutive_heal_failures == 3


# --- observation failures -----------------------------------------------
def test_observe_failure_keeps_loop_alive() -> None:
    bot = SimpleNamespace(
        get_webhook_info=AsyncMock(side_effect=NetworkError("down")),
        set_webhook=AsyncMock(),
    )
    metrics = Metrics()
    sup = _supervisor(bot, metrics=metrics)
    asyncio.run(sup.reconcile())
    bot.set_webhook.assert_not_awaited()
    assert metrics.counts.get("webhook_observe_failures") == 1
    assert sup.snapshot().last_reconcile_outcome == "observe_failed"


# --- wedged / snapshot ---------------------------------------------------
def test_snapshot_reports_wedged_when_breaker_open_and_stale() -> None:
    clock = FakeClock()
    heartbeat = Heartbeat(clock=clock)
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10_000, clock=clock)
    breaker.record_failure()  # OPEN and stays open
    clock.advance(5000)  # updates stale
    bot = _bot(_webhook_info())  # registration matches: no drift
    sup = _supervisor(bot, heartbeat=heartbeat, breaker=breaker, clock=clock)
    asyncio.run(sup.reconcile())
    snapshot = sup.snapshot()
    assert snapshot.wedged is True
    assert snapshot.healthy is False
    assert "wedged" in "\n".join(snapshot.summary_lines())


def test_snapshot_as_dict_exposes_metrics_fields() -> None:
    bot = _bot(_webhook_info(pending=7))
    sup = _supervisor(bot)
    asyncio.run(sup.reconcile())
    data = sup.snapshot().as_dict()
    assert data["enabled"] is True
    assert data["pending_update_count"] == 7
    assert {"url_matches", "breaker_state", "reconciliations", "wedged"} <= data.keys()


# --- desired state / null object ----------------------------------------
def test_webhook_desired_state_from_settings() -> None:
    desired = webhook_desired_state(_settings(webhook_secret_token=""))
    assert desired.url == EXPECTED_URL
    assert desired.secret_token is None  # empty token normalised to None
    assert desired.drop_pending_updates is False
    assert desired.allowed_updates  # non-empty Update.ALL_TYPES


def test_null_supervisor_is_healthy_and_inert() -> None:
    sup = NullWebhookSupervisor()
    snapshot = asyncio.run(sup.reconcile())
    assert snapshot.enabled is False
    assert snapshot.healthy is True
    assert snapshot.wedged is False
    assert sup.snapshot().summary_lines()  # renders without raising
