from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from miki_sorter_bot import burner_backfill as _bf
from miki_sorter_bot.burner_backfill import (
    BackfillOutcome,
    adapt_message,
    backfill_all_topics,
    backfill_topic,
)
from miki_sorter_bot.config import Settings
from miki_sorter_bot.indexing import media_type
from miki_sorter_bot.repositories import SqliteRepositories


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "BOT_TOKEN": "token",
        "SOURCE_CHAT_ID": -100,
        "SOURCE_THREAD_ID": 5,
        "ARCHIVE_CHAT_ID": -200,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _msg(
    mid: int,
    *,
    caption: str = "",
    media: str = "photo",
    extra_media: str | None = None,
    grouped_id: int | None = None,
    sender_id: int = 1,
    is_bot: bool = False,
) -> SimpleNamespace:
    ns = SimpleNamespace(
        id=mid,
        message=caption,
        date=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        grouped_id=grouped_id,
        sender_id=sender_id,
        sender=SimpleNamespace(bot=is_bot),
    )
    setattr(ns, media, True)
    if extra_media:
        setattr(ns, extra_media, True)
    return ns


class FakeFlood(Exception):
    def __init__(self, seconds: int = 0) -> None:
        super().__init__("flood")
        self.seconds = seconds


def test_adapt_message_picks_single_media_field() -> None:
    adapted = adapt_message(_msg(1, media="photo"))
    assert media_type(adapted) == "photo"


def test_adapt_message_video_wins_over_document() -> None:
    # Telethon exposes a video as both .video and .document; adapter must pick video.
    adapted = adapt_message(_msg(1, media="video", extra_media="document"))
    assert media_type(adapted) == "video"


def test_adapt_message_gif_maps_to_animation() -> None:
    adapted = adapt_message(_msg(1, media="gif", extra_media="document"))
    assert media_type(adapted) == "animation"


def test_adapt_message_non_media_returns_none() -> None:
    text_only = SimpleNamespace(
        id=1, message="hi", date=None, grouped_id=None, sender_id=1, sender=None
    )
    assert adapt_message(text_only) is None


def test_adapt_message_stringifies_grouped_id() -> None:
    adapted = adapt_message(_msg(1, grouped_id=987654321))
    assert adapted.media_group_id == "987654321"


def _history(messages):
    def factory(min_id: int):
        return [m for m in messages if m.id > min_id]

    return factory


def test_backfill_indexes_media_with_backfill_provenance(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    messages = [
        _msg(10, caption="Alice CR123", media="photo"),
        _msg(11, caption="just text only, no media", media="photo"),
        _msg(12, caption="#vacation", media="video"),
    ]

    outcome = backfill_topic(
        repositories,
        settings,
        chat_id=-200,
        topic_id=7,
        history_factory=_history(messages),
    )

    assert outcome.indexed == 3
    assert outcome.scanned == 3
    assert outcome.last_message_id == 12
    post = repositories.get_post(-200, 12)
    assert post is not None
    assert post.source_kind == "backfill"
    assert post.source_thread_id == 7
    assert post.media_type == "video"


def test_backfill_is_idempotent_via_min_id(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    messages = [_msg(i, media="photo") for i in (5, 6, 7)]
    factory = _history(messages)

    first = backfill_topic(
        repositories, settings, chat_id=-200, topic_id=7, history_factory=factory
    )
    assert first.indexed == 3

    # Second run resolves min_id from the max already-indexed id -> reads nothing.
    second = backfill_topic(
        repositories, settings, chat_id=-200, topic_id=7, history_factory=factory
    )
    assert second.start_min_id == 7
    assert second.scanned == 0
    assert second.indexed == 0


def test_backfill_respects_limit(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    messages = [_msg(i, media="photo") for i in range(1, 11)]

    outcome = backfill_topic(
        repositories,
        settings,
        chat_id=-200,
        topic_id=7,
        history_factory=_history(messages),
        limit=4,
    )

    assert outcome.indexed == 4
    assert repositories.max_indexed_message_id(-200, 7) == 4


def test_backfill_resumes_after_flood_wait(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    messages = [_msg(i, media="photo") for i in (1, 2, 3)]
    state = {"raised": False}
    slept: list[float] = []

    def factory(min_id: int):
        def gen():
            for m in messages:
                if m.id <= min_id:
                    continue
                if m.id == 2 and not state["raised"]:
                    state["raised"] = True
                    raise FakeFlood(seconds=0)
                yield m

        return gen()

    outcome = backfill_topic(
        repositories,
        settings,
        chat_id=-200,
        topic_id=7,
        history_factory=factory,
        sleep=slept.append,
        flood_wait_types=(FakeFlood,),
    )

    assert outcome.indexed == 3
    assert slept == [1.0]  # seconds + 1
    assert repositories.max_indexed_message_id(-200, 7) == 3


def test_max_indexed_message_id_empty(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    assert repositories.max_indexed_message_id(-200, 7) == 0


def test_backfill_stops_on_time_budget(database_connection) -> None:
    # A fake monotonic clock that advances 1s per read. With max_seconds=2 the
    # crawl must stop after the time check trips, well before history exhausts.
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    messages = [_msg(i, media="photo") for i in range(1, 21)]
    ticks = iter(range(0, 1000))

    outcome = backfill_topic(
        repositories,
        settings,
        chat_id=-200,
        topic_id=7,
        history_factory=_history(messages),
        max_seconds=2,
        clock=lambda: next(ticks),
    )

    assert outcome.stop_reason == "time"
    assert outcome.indexed < len(messages)


def test_backfill_limit_reports_stop_reason(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    messages = [_msg(i, media="photo") for i in range(1, 11)]

    outcome = backfill_topic(
        repositories,
        settings,
        chat_id=-200,
        topic_id=7,
        history_factory=_history(messages),
        limit=4,
    )

    assert outcome.indexed == 4
    assert outcome.stop_reason == "limit"


def test_backfill_exhausted_reports_stop_reason(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    messages = [_msg(i, media="photo") for i in (1, 2, 3)]

    outcome = backfill_topic(
        repositories, settings, chat_id=-200, topic_id=7, history_factory=_history(messages)
    )

    assert outcome.stop_reason == "exhausted"


def test_backfill_applies_jittered_batch_delay(database_connection) -> None:
    # batch_size=2 → a delay after every 2 scanned; delay = batch_delay + jitter*rand.
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    messages = [_msg(i, media="photo") for i in range(1, 5)]
    slept: list[float] = []

    backfill_topic(
        repositories,
        settings,
        chat_id=-200,
        topic_id=7,
        history_factory=_history(messages),
        batch_size=2,
        batch_delay=1.0,
        jitter=0.5,
        rand=lambda: 1.0,  # deterministic: full jitter each time
        sleep=slept.append,
    )

    assert slept == [1.5, 1.5]  # 1.0 base + 0.5 jitter, after msgs 2 and 4


def test_backfill_flood_cap_stops_without_long_sleep(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    messages = [_msg(i, media="photo") for i in (1, 2, 3)]
    slept: list[float] = []

    def factory(min_id: int):
        def gen():
            for m in messages:
                if m.id <= min_id:
                    continue
                if m.id == 2:
                    raise FakeFlood(seconds=3600)  # 1h wait, over the cap
                yield m

        return gen()

    outcome = backfill_topic(
        repositories,
        settings,
        chat_id=-200,
        topic_id=7,
        history_factory=factory,
        sleep=slept.append,
        flood_wait_types=(FakeFlood,),
        max_flood_wait_seconds=300.0,
    )

    assert outcome.stop_reason == "flood_cap"
    assert slept == []  # never slept off the pathological wait
    assert outcome.indexed == 1  # message 1 indexed before the flood


def test_backfill_flood_under_cap_still_sleeps_and_resumes(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    messages = [_msg(i, media="photo") for i in (1, 2, 3)]
    state = {"raised": False}
    slept: list[float] = []

    def factory(min_id: int):
        def gen():
            for m in messages:
                if m.id <= min_id:
                    continue
                if m.id == 2 and not state["raised"]:
                    state["raised"] = True
                    raise FakeFlood(seconds=10)
                yield m

        return gen()

    outcome = backfill_topic(
        repositories,
        settings,
        chat_id=-200,
        topic_id=7,
        history_factory=factory,
        sleep=slept.append,
        flood_wait_types=(FakeFlood,),
        max_flood_wait_seconds=300.0,
    )

    assert outcome.indexed == 3
    assert outcome.stop_reason == "exhausted"
    assert slept == [11.0]  # seconds + 1, under the cap


def test_backfill_all_topics_sweeps_every_active_topic(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    repositories.register_topic(-200, 10, "A")
    repositories.register_topic(-200, 20, "B")
    histories = {
        10: [_msg(1, media="photo"), _msg(2, media="video")],
        20: [_msg(3, media="photo")],
    }

    def factory_for(thread_id: int):
        return _history(histories[thread_id])

    outcomes = backfill_all_topics(
        repositories, settings, chat_id=-200, factory_for=factory_for
    )

    assert [o.topic_id for o in outcomes] == [10, 20]  # ordered by topic name
    assert sum(o.indexed for o in outcomes) == 3
    assert all(o.stop_reason == "exhausted" for o in outcomes)
    # Each row is stamped with the topic it was swept from.
    assert repositories.get_post(-200, 2).source_thread_id == 10
    assert repositories.get_post(-200, 3).source_thread_id == 20


def test_backfill_all_topics_skips_when_no_active_topics(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    outcomes = backfill_all_topics(
        repositories, settings, chat_id=-200, factory_for=lambda _tid: _history([])
    )
    assert outcomes == []


def test_backfill_all_topics_halts_sweep_on_flood_cap(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    settings = _settings()
    repositories.register_topic(-200, 10, "A")
    repositories.register_topic(-200, 20, "B")

    def factory_for(thread_id: int):
        def factory(min_id: int):
            def gen():
                if thread_id == 10:
                    raise FakeFlood(seconds=3600)  # over the cap on the first topic
                yield _msg(3, media="photo")

            return gen()

        return factory

    outcomes = backfill_all_topics(
        repositories,
        settings,
        chat_id=-200,
        factory_for=factory_for,
        flood_wait_types=(FakeFlood,),
        max_flood_wait_seconds=300.0,
        sleep=lambda _s: None,
    )

    # Sweep stops after the hard flood on topic 10; topic 20 is never touched.
    assert len(outcomes) == 1
    assert outcomes[0].stop_reason == "flood_cap"


def test_backfill_and_report_formats_sweep(monkeypatch) -> None:
    outcomes = [
        BackfillOutcome(-200, 10, 3, 2, 12, 0, "exhausted"),
        BackfillOutcome(-200, 20, 1, 1, 5, 0, "limit"),
    ]
    monkeypatch.setattr(_bf, "run_backfill_all", lambda *a, **k: outcomes)
    code, lines = _bf.backfill_and_report(_settings(), None, topic_id=None)
    assert code == 0
    assert any("topic 10" in line for line in lines)
    assert any("Swept 2 topic(s); indexed 3 total" in line for line in lines)
    # A 'limit' stop marks the run incomplete -> the re-run hint appears.
    assert any("More history may remain" in line for line in lines)


def test_backfill_and_report_single_topic(monkeypatch) -> None:
    monkeypatch.setattr(
        _bf, "run_backfill",
        lambda *a, **k: BackfillOutcome(-200, 10, 3, 3, 12, 0, "exhausted"),
    )
    code, lines = _bf.backfill_and_report(_settings(), None, topic_id=10)
    assert code == 0
    assert len(lines) == 1 and "topic 10" in lines[0]


def test_backfill_and_report_no_topics(monkeypatch) -> None:
    monkeypatch.setattr(_bf, "run_backfill_all", lambda *a, **k: [])
    code, lines = _bf.backfill_and_report(_settings(), None, topic_id=None)
    assert code == 0
    assert lines == ["Backfill: no active archive topics to index."]


def test_use_takeout_defaults_on_and_threads_through(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_all(*_a, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(_bf, "run_backfill_all", fake_all)
    _bf.backfill_and_report(_settings(), None, topic_id=None)
    assert captured["use_takeout"] is True  # safest mode is the default

    captured.clear()
    _bf.backfill_and_report(_settings(), None, topic_id=None, use_takeout=False)
    assert captured["use_takeout"] is False


def test_use_takeout_threads_through_single_topic(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_one(*_a, **kwargs):
        captured.update(kwargs)
        return BackfillOutcome(-200, 10, 0, 0, 0, 0, "exhausted")

    monkeypatch.setattr(_bf, "run_backfill", fake_one)
    _bf.backfill_and_report(_settings(), None, topic_id=10, use_takeout=False)
    assert captured["use_takeout"] is False


def test_loop_stops_when_all_topics_exhausted() -> None:
    # cycle 1 indexes some (limit-capped, more to do); cycle 2 indexes nothing and
    # every topic is exhausted -> caught up, loop stops.
    cycles = iter([
        [BackfillOutcome(-200, 10, 100, 100, 100, 0, "limit")],
        [BackfillOutcome(-200, 10, 5, 0, 105, 100, "exhausted")],
    ])
    events: list[str] = []
    slept: list[float] = []
    outcome = _bf._drive_backfill_loop(
        lambda: next(cycles),
        sleep_min=10, sleep_max=10,
        on_event=events.append,
        should_stop=lambda: False,
        wait=slept.append,
        rand=lambda: 0.0,
    )
    assert outcome.done is True
    assert outcome.cycles == 2
    assert outcome.indexed == 100
    assert slept == [10]  # slept once, between the two cycles
    assert any("complete" in e for e in events)


def test_loop_stops_on_stop_event() -> None:
    state = {"stop": False}

    def run_cycle():
        state["stop"] = True  # request stop after the first cycle
        return [BackfillOutcome(-200, 10, 100, 100, 100, 0, "limit")]  # more to do

    outcome = _bf._drive_backfill_loop(
        run_cycle,
        sleep_min=10, sleep_max=10,
        on_event=lambda _m: None,
        should_stop=lambda: state["stop"],
        wait=lambda _s: None,
        rand=lambda: 0.0,
    )
    assert outcome.done is False  # stopped before catching up
    assert outcome.cycles == 1


def test_loop_no_topics_is_done_immediately() -> None:
    outcome = _bf._drive_backfill_loop(
        lambda: [],
        sleep_min=10, sleep_max=10,
        on_event=lambda _m: None,
        should_stop=lambda: False,
        wait=lambda _s: None,
    )
    assert outcome.done is True
    assert outcome.cycles == 1
