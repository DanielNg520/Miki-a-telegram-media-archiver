from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from miki_sorter_bot.indexing import (
    EXTRACTOR_VERSION,
    IndexingService,
    MessageIndexer,
    extract_search_tokens,
)
from miki_sorter_bot.repositories import SqliteRepositories


def _message(
    message_id: int = 12,
    *,
    caption: str = "Trip with John in TOKYO using RX7 #Japan",
    thread_id: int = 7,
    media_group_id: str | None = None,
    sender_id: int = 10,
    sender_is_bot: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        message_thread_id=thread_id,
        media_group_id=media_group_id,
        caption=caption,
        text=None,
        date=datetime(2026, 6, 13, tzinfo=UTC),
        from_user=SimpleNamespace(id=sender_id, is_bot=sender_is_bot),
        photo=[object()],
        animation=None,
        audio=None,
        document=None,
        sticker=None,
        video=None,
        video_note=None,
        voice=None,
    )


def test_extractor_retains_compact_identifiers_and_configured_values() -> None:
    result = extract_search_tokens(
        "Hello from John in New York with RX7 and NASA #Travel. Later Alice arrived.",
        {
            ("keyword", "hello"),
            ("phrase", "new york"),
        },
    )
    values = {(token.kind, token.normalized_value) for token in result.tokens}

    assert ("hashtag", "travel") in values
    assert ("name", "john") in values
    assert ("name", "new") in values
    assert ("name", "york") in values
    assert ("code", "rx7") in values
    assert ("code", "nasa") in values
    assert ("keyword", "hello") in values
    assert ("phrase", "new york") in values
    assert ("name", "hello") not in values
    assert ("name", "later") not in values
    assert result.version == EXTRACTOR_VERSION


def test_configured_keyword_requires_non_alphanumeric_boundaries() -> None:
    joined = extract_search_tokens("New COD123 release", {("keyword", "cod")})
    punctuated = extract_search_tokens("New (COD)-release", {("keyword", "cod")})

    assert ("keyword", "cod") not in {
        (token.kind, token.normalized_value) for token in joined.tokens
    }
    assert ("keyword", "cod") in {
        (token.kind, token.normalized_value) for token in punctuated.tokens
    }


def test_configured_phrase_requires_whitespace_between_words() -> None:
    spaced = extract_search_tokens("Visit New York", {("phrase", "new york")})
    punctuated = extract_search_tokens("Visit New, York", {("phrase", "new york")})

    assert ("phrase", "new york") in {
        (token.kind, token.normalized_value) for token in spaced.tokens
    }
    assert ("phrase", "new york") not in {
        (token.kind, token.normalized_value) for token in punctuated.tokens
    }


def test_hashtag_with_underscore_is_extracted() -> None:
    result = extract_search_tokens("Visiting #New_York")

    assert ("hashtag", "new_york") in {
        (token.kind, token.normalized_value) for token in result.tokens
    }


def test_index_upsert_replaces_stale_tokens_on_caption_edit(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    indexer = MessageIndexer(repositories, bot_id=99)

    indexer.index(_message(caption="Photo of John using RX7 #Cars"), -100)
    post = repositories.get_post(-100, 12)
    assert {token.normalized_value for token in repositories.get_post_tokens(post.id)} >= {
        "john",
        "rx7",
        "cars",
    }

    indexer.index(_message(caption="Photo of Alice using A320 #Planes"), -100)
    edited = repositories.get_post(-100, 12)
    values = {token.normalized_value for token in repositories.get_post_tokens(edited.id)}

    assert edited.id == post.id
    assert {"alice", "a320", "planes"} <= values
    assert "john" not in values
    assert "rx7" not in values


def test_album_members_share_logical_key_but_keep_message_identity(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    indexer = MessageIndexer(repositories, bot_id=99)

    indexer.index(_message(12, media_group_id="album-1"), -100)
    indexer.index(_message(13, media_group_id="album-1"), -100)

    first = repositories.get_post(-100, 12)
    second = repositories.get_post(-100, 13)
    assert first.id != second.id
    assert first.logical_post_key == second.logical_post_key == "-100:album-1"


def test_index_marks_miki_and_external_bot_sources(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    indexer = MessageIndexer(repositories, bot_id=99)

    indexer.index(_message(12, sender_id=99, sender_is_bot=True), -100)
    indexer.index(_message(13, sender_id=50, sender_is_bot=True), -100)

    assert repositories.get_post(-100, 12).source_kind == "miki_copy"
    assert repositories.get_post(-100, 13).source_kind == "external_bot"


def test_update_handler_indexes_only_active_registered_archive_topics(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 7, "Japan")
    service = IndexingService(
        SimpleNamespace(archive_chat_id=-200),
        repositories,
    )
    context = SimpleNamespace(bot=SimpleNamespace(id=99))
    update = SimpleNamespace(
        effective_message=_message(),
        effective_chat=SimpleNamespace(id=-200),
    )

    asyncio.run(service.handle_update(update, context))
    assert repositories.get_post(-200, 12) is not None

    repositories.update_topic_state(-200, 7, is_active=False)
    update.effective_message = _message(13)
    asyncio.run(service.handle_update(update, context))
    assert repositories.get_post(-200, 13) is None


def test_successful_bot_copy_can_be_indexed_without_waiting_for_an_update(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    service = IndexingService(SimpleNamespace(archive_chat_id=-200), repositories)

    indexed = service.index_copy(
        _message(caption="Photo of Tokyo #Japan"),
        bot_id=99,
        destination_chat_id=-200,
        destination_thread_id=9,
        destination_message_id=88,
    )

    post = repositories.get_post(-200, 88)
    assert indexed is True
    assert post.source_kind == "miki_copy"
    assert post.source_thread_id == 9


def test_reindex_processes_only_bounded_outdated_rows(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 7, "Japan")
    indexer = MessageIndexer(repositories, bot_id=99)
    indexer.index(_message(12), -200)
    indexer.index(_message(13), -200)
    database_connection.execute("UPDATE posts SET extractor_version = 0")
    database_connection.commit()
    service = IndexingService(SimpleNamespace(archive_chat_id=-200), repositories)

    processed, last_id = service.reindex(limit=1)

    assert processed == 1
    assert last_id is not None
    versions = [
        row["extractor_version"]
        for row in database_connection.execute("SELECT extractor_version FROM posts ORDER BY id")
    ]
    assert versions == [EXTRACTOR_VERSION, 0]
