from __future__ import annotations

from types import SimpleNamespace

from miki_sorter_bot.lookback import RecentMediaBuffer


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _buffer(clock: FakeClock, *, ttl: float = 120.0, capacity: int = 5) -> RecentMediaBuffer:
    return RecentMediaBuffer(ttl=lambda: ttl, capacity=lambda: capacity, clock=clock)


def _msg(message_id: int) -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id)


def test_capture_then_claim_returns_latest_and_removes_it() -> None:
    clock = FakeClock()
    buffer = _buffer(clock)
    buffer.capture(-100, 5, (_msg(1),))
    clock.advance(1)
    buffer.capture(-100, 5, (_msg(2),))

    claimed = buffer.claim_latest(-100, 5)
    assert claimed is not None
    assert [m.message_id for m in claimed.messages] == [2]  # most recent
    # the older one is still claimable
    second = buffer.claim_latest(-100, 5)
    assert second is not None and second.messages[0].message_id == 1
    assert buffer.claim_latest(-100, 5) is None


def test_claim_empty_returns_none() -> None:
    assert _buffer(FakeClock()).claim_latest(-100, 5) is None


def test_expired_entries_are_not_claimable() -> None:
    clock = FakeClock()
    buffer = _buffer(clock, ttl=120.0)
    buffer.capture(-100, 5, (_msg(1),))
    clock.advance(121)
    assert buffer.claim_latest(-100, 5) is None
    assert len(buffer) == 0


def test_capacity_drops_oldest() -> None:
    clock = FakeClock()
    buffer = _buffer(clock, capacity=2)
    for mid in (1, 2, 3):
        clock.advance(1)
        buffer.capture(-100, 5, (_msg(mid),))
    assert len(buffer) == 2
    newest = buffer.claim_latest(-100, 5)
    middle = buffer.claim_latest(-100, 5)
    assert newest is not None and newest.messages[0].message_id == 3
    assert middle is not None and middle.messages[0].message_id == 2
    assert buffer.claim_latest(-100, 5) is None  # message 1 was evicted


def test_album_recapture_dedupes_by_group_id() -> None:
    clock = FakeClock()
    buffer = _buffer(clock)
    buffer.capture(-100, 5, (_msg(1), _msg(2)), media_group_id="g1")
    buffer.capture(-100, 5, (_msg(1), _msg(2), _msg(3)), media_group_id="g1")
    assert len(buffer) == 1
    claimed = buffer.claim_latest(-100, 5)
    assert claimed is not None
    assert [m.message_id for m in claimed.messages] == [1, 2, 3]
    assert claimed.is_album is True


def test_buckets_are_isolated_per_chat_and_thread() -> None:
    clock = FakeClock()
    buffer = _buffer(clock)
    buffer.capture(-100, 5, (_msg(1),))
    buffer.capture(-100, 9, (_msg(2),))
    buffer.capture(-200, 5, (_msg(3),))
    assert buffer.claim_latest(-100, 9) is not None
    assert buffer.claim_latest(-100, 9) is None
    assert buffer.claim_latest(-100, 5) is not None  # untouched by the thread-9 claim


def test_sweep_purges_expired_buckets() -> None:
    clock = FakeClock()
    buffer = _buffer(clock, ttl=60.0)
    buffer.capture(-100, 5, (_msg(1),))
    clock.advance(61)
    buffer.sweep()
    assert len(buffer) == 0
