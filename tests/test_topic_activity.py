class FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeStore:
    def __init__(self, settings=None, fail=False):
        self._settings = dict(settings or {})
        self.writes = 0
        self.fail = fail

    def get_runtime_setting(self, key):
        return self._settings.get(key)

    def set_runtime_setting(self, key, value, updated_by_user_id=None):
        self.writes += 1
        if self.fail:
            raise RuntimeError("persistence failure")
        self._settings[key] = value


class FakeLive:
    def __init__(self, thread=7):
        self.thread = thread

    def effective_source_thread_id(self):
        return self.thread


def _activity(store, live, clock):
    from miki_sorter_bot.topic_activity import TopicActivity

    return TopicActivity(store, live, clock=clock)


def test_counts_only_effective_source_topic():
    store = FakeStore()
    live = FakeLive(thread=7)
    clock = FakeClock()
    activity = _activity(store, live, clock)

    assert activity.record(7, "a") is True
    assert activity.rotation_count() == 1

    assert activity.record(8, "b") is True
    assert activity.rotation_count() == 1


def test_album_counts_once():
    store = FakeStore()
    live = FakeLive(thread=7)
    clock = FakeClock()
    activity = _activity(store, live, clock)

    assert activity.record(7, "g1") is True
    assert activity.record(7, "g1") is False
    assert activity.record(7, "g1") is False
    assert activity.rotation_count() == 1
    assert store.writes == 1

    assert activity.record(7, "g2") is True
    assert activity.rotation_count() == 2


def test_dedup_window_expiry():
    store = FakeStore()
    live = FakeLive(thread=7)
    clock = FakeClock()
    activity = _activity(store, live, clock)

    assert activity.record(7, "g1") is True
    clock.advance(301.0)
    assert activity.record(7, "g1") is True
    assert activity.rotation_count() == 2


def test_restart_survives():
    store = FakeStore()
    live = FakeLive(thread=7)
    clock = FakeClock()
    activity = _activity(store, live, clock)

    for i in range(3):
        activity.record(7, f"g{i}")

    restarted = _activity(store, live, clock)
    assert restarted.rotation_count() == 3

    assert restarted.record(7, "g3") is True
    assert restarted.rotation_count() == 4


def test_stale_thread_reads_zero():
    store = FakeStore(settings={"rotation_media_count": "7:5"})
    live = FakeLive(thread=9)
    clock = FakeClock()
    activity = _activity(store, live, clock)

    assert activity.rotation_count() == 0

    assert activity.record(9, "g1") is True
    assert store.get_runtime_setting("rotation_media_count") == "9:1"


def test_malformed_stored_value_reads_zero():
    store = FakeStore(settings={"rotation_media_count": "garbage"})
    live = FakeLive(thread=7)
    clock = FakeClock()
    activity = _activity(store, live, clock)

    assert activity.rotation_count() == 0

    assert activity.record(7, "g1") is True
    assert activity.rotation_count() == 1


def test_reset_rotation_zeroes_and_persists():
    store = FakeStore(settings={"rotation_media_count": "7:5"})
    live = FakeLive(thread=7)
    clock = FakeClock()
    activity = _activity(store, live, clock)

    activity.reset_rotation()

    assert activity.rotation_count() == 0
    assert store.get_runtime_setting("rotation_media_count") == "7:0"


def test_persistence_failure_swallowed():
    store = FakeStore(fail=True)
    live = FakeLive(thread=7)
    clock = FakeClock()
    activity = _activity(store, live, clock)

    assert activity.record(7, "g1") is True
    assert activity.rotation_count() == 1


def test_one_write_per_counted_post():
    store = FakeStore()
    live = FakeLive(thread=7)
    clock = FakeClock()
    activity = _activity(store, live, clock)

    for i in range(5):
        activity.record(7, f"g{i}")

    assert store.writes == 5


def test_group_id_none_never_dedups():
    store = FakeStore()
    live = FakeLive(thread=7)
    clock = FakeClock()
    activity = _activity(store, live, clock)

    assert activity.record(7, None) is True
    assert activity.record(7, None) is True
    assert activity.rotation_count() == 2
