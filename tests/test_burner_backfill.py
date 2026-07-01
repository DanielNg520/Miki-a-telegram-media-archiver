from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from miki_sorter_bot.burner_backfill import adapt_message, backfill_topic
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
