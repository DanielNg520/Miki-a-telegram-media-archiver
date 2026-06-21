from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from miki_sorter_bot.reliability import (
    DeliveryExecutor,
    RateLimiter,
    RetryPolicy,
    classify_error,
)


def test_error_classification_checks_permanent_errors_before_network_base() -> None:
    assert classify_error(Forbidden("no")).category == "permission"
    unavailable = classify_error(BadRequest("Message to copy not found"))
    assert unavailable.category == "unavailable_source"
    assert unavailable.unavailable_source
    assert classify_error(NetworkError("down")).retryable
    assert classify_error(TimedOut("timed out")).outcome_unknown


def test_retry_after_is_honored_before_success() -> None:
    operation = AsyncMock(
        side_effect=[
            RetryAfter(timedelta(seconds=2)),
            "ok",
        ]
    )
    sleep = AsyncMock()
    executor = DeliveryExecutor(
        retry_policy=RetryPolicy(attempts=2, base_delay=0, max_delay=0),
        rate_limiter=RateLimiter(1000),
        sleep=sleep,
    )

    result = asyncio.run(executor.run(operation))

    assert result == "ok"
    sleep.assert_awaited_once_with(2.0)


def test_permanent_error_is_not_retried() -> None:
    operation = AsyncMock(side_effect=Forbidden("no"))
    executor = DeliveryExecutor(
        retry_policy=RetryPolicy(attempts=3, base_delay=0, max_delay=0),
        rate_limiter=RateLimiter(1000),
    )

    with pytest.raises(Forbidden):
        asyncio.run(executor.run(operation))

    assert operation.await_count == 1


def test_unknown_delivery_outcome_is_not_retried_when_safety_is_required() -> None:
    operation = AsyncMock(side_effect=TimedOut("timed out"))
    executor = DeliveryExecutor(
        retry_policy=RetryPolicy(attempts=3, base_delay=0, max_delay=0),
        rate_limiter=RateLimiter(1000),
    )

    with pytest.raises(TimedOut):
        asyncio.run(executor.run(operation, retry_unknown_outcome=False))

    assert operation.await_count == 1
