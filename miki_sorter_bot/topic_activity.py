"""Shared persisted media counter for source-topic posts.

Owns the album dedup window and the rotation media count (survives restarts).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

LOGGER = logging.getLogger(__name__)

_GROUP_DEDUP_WINDOW_SECONDS = 300.0
_GROUP_DEDUP_MAX = 128


class _Store(Protocol):
    def get_runtime_setting(self, key: str) -> str | None: ...
    def set_runtime_setting(
        self, key: str, value: str, updated_by_user_id: int | None = None
    ) -> None: ...


class _Live(Protocol):
    def effective_source_thread_id(self) -> int: ...


class TopicActivity:
    def __init__(
        self,
        repositories: _Store,
        live_settings: _Live,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._live = live_settings
        self._clock = clock
        self._store: _Store = repositories
        self._seen: OrderedDict[tuple[int, str | None], float] = OrderedDict()
        self._cached_thread_id: int | None = None
        self._cached_count: int = 0
        self._loaded = False

    def _prune(self) -> None:
        now = self._clock()
        expired = [key for key, ts in self._seen.items() if now - ts > _GROUP_DEDUP_WINDOW_SECONDS]
        for key in expired:
            del self._seen[key]
        while len(self._seen) > _GROUP_DEDUP_MAX:
            self._seen.popitem(last=False)

    def is_new_post(self, topic_id: int, group_id: str | None) -> bool:
        self._prune()
        if group_id is None:
            return True
        key = (topic_id, group_id)
        first = key not in self._seen
        self._seen[key] = self._clock()
        self._seen.move_to_end(key)
        return first

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        raw = self._store.get_runtime_setting("rotation_media_count")
        if raw is None:
            return
        try:
            thread_str, count_str = raw.split(":", 1)
            self._cached_thread_id = int(thread_str)
            self._cached_count = int(count_str)
        except (ValueError, TypeError):
            self._cached_thread_id = None
            self._cached_count = 0

    def _current_count(self) -> int:
        self._load()
        if self._cached_thread_id != self._live.effective_source_thread_id():
            return 0
        return self._cached_count

    def record(self, topic_id: int, group_id: str | None) -> bool:
        counted = self.is_new_post(topic_id, group_id)
        if not counted:
            return False
        if topic_id != self._live.effective_source_thread_id():
            return True
        self._load()
        current = self._current_count() + 1
        self._cached_thread_id = self._live.effective_source_thread_id()
        self._cached_count = current
        try:
            self._store.set_runtime_setting(
                "rotation_media_count",
                f"{topic_id}:{current}",
            )
        except Exception:
            LOGGER.warning(
                "Failed to persist rotation_media_count",
                exc_info=True,
            )
        return True

    def rotation_count(self) -> int:
        return self._current_count()

    def reset_rotation(self) -> None:
        self._load()
        thread_id = self._live.effective_source_thread_id()
        self._cached_thread_id = thread_id
        self._cached_count = 0
        try:
            self._store.set_runtime_setting(
                "rotation_media_count",
                f"{thread_id}:0",
            )
        except Exception:
            LOGGER.warning(
                "Failed to reset rotation_media_count",
                exc_info=True,
            )
