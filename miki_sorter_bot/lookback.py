"""Short-lived memory of recent uncaptioned media, claimable by a later tag.

Workflow this supports: a user posts media (or an album) with no caption, then
sends a separate hashtag-only message to route it. A bot cannot fetch "the
previous message" from Telegram, so Miki remembers the media it *already saw*
for a bounded window and lets the following tag claim it.

The buffer is bounded in both **time** (``ttl``) and **count** (``capacity``),
both read live so they stay chat-configurable, and is **self-cleaning**: expired
entries are purged on every capture and skipped on every claim, so memory never
grows unbounded and a restart simply starts empty. It is pure and synchronous —
trivially unit-testable with an injected clock.
"""

from __future__ import annotations

import time as _time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

Clock = Callable[[], float]
BucketKey = tuple[int, int | None]


@dataclass(frozen=True, slots=True)
class CapturedMedia:
    """One unrouted post awaiting a tag: a single message or an assembled album."""

    messages: tuple[Any, ...]
    media_group_id: str | None
    captured_at: float

    @property
    def is_album(self) -> bool:
        return self.media_group_id is not None or len(self.messages) > 1


class RecentMediaBuffer:
    def __init__(
        self,
        *,
        ttl: Callable[[], float],
        capacity: Callable[[], int],
        clock: Clock = _time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._clock = clock
        self._buckets: dict[BucketKey, deque[CapturedMedia]] = {}

    def capture(
        self,
        chat_id: int,
        thread_id: int | None,
        messages: Iterable[Any],
        *,
        media_group_id: str | None = None,
    ) -> None:
        items = tuple(messages)
        if not items:
            return
        key: BucketKey = (chat_id, thread_id)
        now = self._clock()
        bucket = self._buckets.setdefault(key, deque())
        # Dedupe: re-capturing the same album replaces the older entry so a
        # straggling member or a re-flush cannot leave two copies.
        if media_group_id is not None:
            for existing in [item for item in bucket if item.media_group_id == media_group_id]:
                bucket.remove(existing)
        bucket.append(CapturedMedia(items, media_group_id, now))
        self._prune(bucket, now)
        if not bucket:
            self._buckets.pop(key, None)

    def claim_latest(self, chat_id: int, thread_id: int | None) -> CapturedMedia | None:
        key: BucketKey = (chat_id, thread_id)
        bucket = self._buckets.get(key)
        if not bucket:
            return None
        now = self._clock()
        self._prune(bucket, now)
        if not bucket:
            self._buckets.pop(key, None)
            return None
        item = bucket.pop()  # most recent = "the message before" the tag
        if not bucket:
            self._buckets.pop(key, None)
        return item

    def sweep(self) -> None:
        """Purge expired entries everywhere (safe to call periodically)."""

        now = self._clock()
        for key in list(self._buckets):
            bucket = self._buckets[key]
            self._prune(bucket, now)
            if not bucket:
                self._buckets.pop(key, None)

    def _prune(self, bucket: deque[CapturedMedia], now: float) -> None:
        ttl = self._ttl()
        while bucket and now - bucket[0].captured_at > ttl:
            bucket.popleft()
        capacity = self._capacity()
        while len(bucket) > capacity:
            bucket.popleft()  # drop oldest beyond capacity

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self._buckets.values())
