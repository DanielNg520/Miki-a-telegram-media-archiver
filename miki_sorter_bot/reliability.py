from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Awaitable, Callable, TypeVar

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Failure:
    category: str
    retryable: bool
    unavailable_source: bool = False
    retry_after: float | None = None


def classify_error(error: Exception) -> Failure:
    if isinstance(error, RetryAfter):
        value = error.retry_after
        seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
        return Failure("rate_limit", True, retry_after=max(0.0, seconds))
    if isinstance(error, Forbidden):
        return Failure("permission", False)
    if isinstance(error, BadRequest):
        text = str(error).casefold()
        unavailable = any(
            phrase in text
            for phrase in (
                "message to copy not found",
                "message can't be copied",
                "message cannot be copied",
                "message_id_invalid",
            )
        )
        return Failure("unavailable_source" if unavailable else "invalid_request", False, unavailable)
    if isinstance(error, (TimedOut, NetworkError, TimeoutError, OSError)):
        return Failure("transient", True)
    return Failure("unexpected", False)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0

    def delay(self, attempt: int) -> float:
        return min(self.base_delay * (2**attempt), self.max_delay) * random.uniform(0.8, 1.2)


class RateLimiter:
    def __init__(self, messages_per_second: float) -> None:
        if messages_per_second <= 0:
            raise ValueError("messages_per_second must be positive")
        self._interval = 1 / messages_per_second
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            if delay:
                await asyncio.sleep(delay)
            self._next_at = max(now, self._next_at) + self._interval


class DeliveryExecutor:
    def __init__(
        self,
        *,
        retry_policy: RetryPolicy,
        rate_limiter: RateLimiter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        metric: Callable[[str, int], None] | None = None,
    ) -> None:
        self._retry_policy = retry_policy
        self._rate_limiter = rate_limiter
        self._sleep = sleep
        self._metric = metric or (lambda _name, _amount: None)

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        started_at = time.monotonic()
        last_error: Exception | None = None
        try:
            for attempt in range(self._retry_policy.attempts):
                await self._rate_limiter.wait()
                try:
                    result = await operation()
                    self._metric("telegram_delivery_successes", 1)
                    return result
                except Exception as error:
                    failure = classify_error(error)
                    last_error = error
                    if failure.category == "rate_limit":
                        self._metric("telegram_throttles", 1)
                    if not failure.retryable or attempt + 1 >= self._retry_policy.attempts:
                        self._metric("telegram_delivery_failures", 1)
                        raise
                    self._metric("telegram_retries", 1)
                    delay = (
                        failure.retry_after
                        if failure.retry_after is not None
                        else self._retry_policy.delay(attempt)
                    )
                    await self._sleep(delay)
            raise last_error or RuntimeError("delivery failed")
        finally:
            elapsed_ms = max(0, round((time.monotonic() - started_at) * 1000))
            self._metric("telegram_delivery_operations", 1)
            self._metric("telegram_delivery_duration_ms", elapsed_ms)
