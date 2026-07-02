from __future__ import annotations

import asyncio
from types import SimpleNamespace

from miki_sorter_bot.periodic_notice import PeriodicNoticeService, TEXT_KEY


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeStore:
    """Minimal runtime-settings KV, matching the repository slice used."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.kv: dict[str, str] = dict(initial or {})

    def get_runtime_setting(self, key: str) -> str | None:
        return self.kv.get(key)

    def set_runtime_setting(self, key: str, value: str, updated_by_user_id=None) -> None:
        self.kv[key] = value

    def delete_runtime_setting(self, key: str) -> bool:
        return self.kv.pop(key, None) is not None


class FakeBot:
    def __init__(self, *, next_message_id: int = 500) -> None:
        self.id = 999
        self.sent: list[dict] = []
        self.deleted: list[int] = []
        self._next = next_message_id
        self.delete_error: Exception | None = None
        self.send_error: Exception | None = None

    async def send_message(self, chat_id, text, message_thread_id=None):
        if self.send_error is not None:
            raise self.send_error
        self._next += 1
        self.sent.append(
            {"chat_id": chat_id, "text": text, "thread_id": message_thread_id, "id": self._next}
        )
        return SimpleNamespace(message_id=self._next)

    async def delete_message(self, chat_id, message_id):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(message_id)


class FakeLive:
    def __init__(
        self,
        *,
        enabled=True,
        threshold=10,
        interval=60,
        topics=frozenset({7}),
    ) -> None:
        self._enabled = enabled
        self._threshold = threshold
        self._interval = interval
        self._topics = frozenset(topics)

    def notice_enabled(self):
        return self._enabled

    def notice_media_threshold(self):
        return self._threshold

    def notice_interval_minutes(self):
        return self._interval

    def notice_topics(self):
        return self._topics


def _service(live, store, clock, *, source_chat_id=-100, delay=0.0):
    settings = SimpleNamespace(source_chat_id=source_chat_id)
    return PeriodicNoticeService(settings, store, live, clock=clock, post_delay_seconds=delay)


def _context(bot):
    return SimpleNamespace(bot=bot)


async def _drain_tasks():
    # Let the debounce task (post_delay=0) run to completion.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_count_trigger_posts_after_threshold() -> None:
    clock = FakeClock()
    store = FakeStore({TEXT_KEY: "Tag your posts!"})
    bot = FakeBot()
    service = _service(FakeLive(threshold=3), store, clock)

    async def scenario() -> None:
        for _ in range(2):
            service.on_media(7, _context(bot))
        await _drain_tasks()
        assert bot.sent == []  # below threshold

        service.on_media(7, _context(bot))
        await _drain_tasks()

    asyncio.run(scenario())
    assert len(bot.sent) == 1
    assert bot.sent[0]["thread_id"] == 7
    assert bot.sent[0]["text"] == "Tag your posts!"


def test_album_members_count_as_one_message() -> None:
    clock = FakeClock()
    store = FakeStore({TEXT_KEY: "Tag your posts!"})
    bot = FakeBot()
    service = _service(FakeLive(threshold=2), store, clock)

    async def scenario() -> None:
        # A 5-photo album (same media_group_id) counts once -> below threshold.
        for _ in range(5):
            service.on_media(7, _context(bot), group_id="album-1")
        await _drain_tasks()
        assert bot.sent == []

        # A second distinct post reaches the threshold of 2 messages.
        service.on_media(7, _context(bot), group_id="album-2")
        await _drain_tasks()

    asyncio.run(scenario())
    assert len(bot.sent) == 1


def test_deletes_previous_before_posting_new() -> None:
    clock = FakeClock()
    store = FakeStore({TEXT_KEY: "hi"})
    bot = FakeBot(next_message_id=800)
    service = _service(FakeLive(threshold=1), store, clock)

    async def scenario() -> None:
        service.on_media(7, _context(bot))
        await _drain_tasks()
        assert bot.deleted == []  # nothing to delete first time
        service.on_media(7, _context(bot))
        await _drain_tasks()

    asyncio.run(scenario())
    assert bot.deleted == [bot.sent[0]["id"]]  # deleted the previous notice
    assert len(bot.sent) == 2


def test_interval_skips_when_no_new_activity() -> None:
    clock = FakeClock()
    store = FakeStore({TEXT_KEY: "hi"})
    bot = FakeBot()
    service = _service(FakeLive(threshold=0, interval=60), store, clock)

    async def scenario() -> None:
        # No media at all -> nothing due.
        await service.tick(_context(bot))
        assert bot.sent == []

        # One media, then interval elapses -> posts once, resetting the counter.
        service.on_media(7, _context(bot))
        clock.advance(60 * 60)
        await service.tick(_context(bot))
        assert len(bot.sent) == 1

        # No further media; interval elapses again -> skipped (only on new activity).
        clock.advance(60 * 60)
        await service.tick(_context(bot))

    asyncio.run(scenario())
    assert len(bot.sent) == 1


def test_count_post_resets_interval_timer_no_double_post() -> None:
    clock = FakeClock()
    store = FakeStore({TEXT_KEY: "hi"})
    bot = FakeBot()
    service = _service(FakeLive(threshold=2, interval=60), store, clock)

    async def scenario() -> None:
        service.on_media(7, _context(bot))
        service.on_media(7, _context(bot))
        await _drain_tasks()
        assert len(bot.sent) == 1  # count trigger fired

        # Interval elapses but count was reset by the count post -> no repost.
        clock.advance(60 * 60)
        await service.tick(_context(bot))

    asyncio.run(scenario())
    assert len(bot.sent) == 1


def test_no_text_means_no_post_but_keeps_counting() -> None:
    clock = FakeClock()
    store = FakeStore()  # no text configured
    bot = FakeBot()
    service = _service(FakeLive(threshold=1), store, clock)

    async def scenario() -> None:
        service.on_media(7, _context(bot))
        await _drain_tasks()
        assert bot.sent == []  # nothing to say yet

        # Configure text; next threshold crossing posts.
        store.kv[TEXT_KEY] = "now set"
        service.on_media(7, _context(bot))
        await _drain_tasks()

    asyncio.run(scenario())
    assert len(bot.sent) == 1


def test_failed_delete_still_posts() -> None:
    clock = FakeClock()
    store = FakeStore({TEXT_KEY: "hi"})
    bot = FakeBot()
    service = _service(FakeLive(threshold=1), store, clock)

    async def scenario() -> None:
        service.on_media(7, _context(bot))
        await _drain_tasks()
        bot.delete_error = RuntimeError("message to delete not found")
        service.on_media(7, _context(bot))
        await _drain_tasks()

    asyncio.run(scenario())
    assert len(bot.sent) == 2  # posted despite the delete failure


def test_disabled_and_unconfigured_topics_ignored() -> None:
    clock = FakeClock()
    store = FakeStore({TEXT_KEY: "hi"})
    bot = FakeBot()

    async def scenario() -> None:
        disabled = _service(FakeLive(enabled=False, threshold=1), store, clock)
        disabled.on_media(7, _context(bot))
        await _drain_tasks()

        other_topic = _service(FakeLive(threshold=1, topics=frozenset({7})), store, clock)
        other_topic.on_media(99, _context(bot))  # not a configured topic
        await _drain_tasks()

    asyncio.run(scenario())
    assert bot.sent == []
