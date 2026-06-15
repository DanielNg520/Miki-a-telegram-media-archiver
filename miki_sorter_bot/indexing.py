from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from miki_sorter_bot.config import Settings
from miki_sorter_bot.repositories import IndexedPostInput, SearchToken, SqliteRepositories

EXTRACTOR_VERSION = 1
TOKEN_RE = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)
HASHTAG_RE = re.compile(r"(?<!\w)#([^\W_]+(?:-[^\W_]+)*)", re.UNICODE)
SENTENCE_END_RE = re.compile(r"[.!?]\s*$")


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    tokens: frozenset[SearchToken]
    version: int = EXTRACTOR_VERSION


def extract_search_tokens(
    text: str,
    configured_values: set[tuple[str, str]] | None = None,
) -> ExtractionResult:
    tokens: set[SearchToken] = set()
    hashtag_spans: set[tuple[int, int]] = set()
    for match in HASHTAG_RE.finditer(text):
        value = match.group(1)
        hashtag_spans.add(match.span(1))
        tokens.add(SearchToken("hashtag", value, value.casefold()))

    previous_end = 0
    for match in TOKEN_RE.finditer(text):
        value = match.group(0)
        if match.span() in hashtag_spans:
            previous_end = match.end()
            continue
        letters = [character for character in value if character.isalpha()]
        has_digit = any(character.isdigit() for character in value)
        kind: str | None = None
        if letters and has_digit:
            kind = "code"
        elif len(letters) >= 2 and all(character.isupper() for character in letters):
            kind = "code"
        elif value[:1].isupper() and not _starts_sentence(text, match.start(), previous_end):
            kind = "name"
        if kind is not None:
            tokens.add(SearchToken(kind, value, value.casefold()))
        previous_end = match.end()

    normalized_text = " ".join(match.group(0).casefold() for match in TOKEN_RE.finditer(text))
    for kind, configured_value in configured_values or set():
        normalized_value = " ".join(configured_value.casefold().split())
        if kind == "hashtag":
            continue
        if kind == "keyword" and normalized_value in normalized_text.split():
            tokens.add(SearchToken("keyword", configured_value, normalized_value))
        elif kind == "phrase" and _contains_phrase(normalized_text, normalized_value):
            tokens.add(SearchToken("phrase", configured_value, normalized_value))
    return ExtractionResult(frozenset(tokens))


def media_type(message: object) -> str | None:
    for field in (
        "animation",
        "audio",
        "document",
        "photo",
        "sticker",
        "video",
        "video_note",
        "voice",
    ):
        if getattr(message, field, None):
            return field
    return None


class MessageIndexer:
    def __init__(self, repositories: SqliteRepositories, bot_id: int) -> None:
        self._repositories = repositories
        self._bot_id = bot_id

    def index(
        self,
        message: object,
        chat_id: int,
        *,
        thread_id_override: int | None = None,
        message_id_override: int | None = None,
        source_kind_override: str | None = None,
    ) -> bool:
        detected_media = media_type(message)
        thread_id = thread_id_override or getattr(message, "message_thread_id", None)
        if detected_media is None or thread_id is None:
            return False
        text = (getattr(message, "caption", None) or getattr(message, "text", None) or "").strip()
        mappings = self._repositories.list_mappings(chat_id)
        configured_values = {(item.kind, item.normalized_value) for item in mappings}
        extraction = extract_search_tokens(text, configured_values)
        sender = getattr(message, "from_user", None)
        sender_id = getattr(sender, "id", None)
        sender_is_bot = bool(getattr(sender, "is_bot", False))
        source_kind = source_kind_override or (
            "miki_copy"
            if sender_id == self._bot_id
            else "external_bot"
            if sender_is_bot
            else "telegram"
        )
        created_at = getattr(message, "date", None)
        if isinstance(created_at, datetime):
            created_at_value = created_at.isoformat()
        else:
            created_at_value = None
        media_group_id = getattr(message, "media_group_id", None)
        message_id = message_id_override or getattr(message, "message_id")
        logical_post_key = f"{chat_id}:{media_group_id or f'message:{message_id}'}"
        self._repositories.upsert_post(
            IndexedPostInput(
                source_chat_id=chat_id,
                source_thread_id=thread_id,
                source_message_id=message_id,
                media_group_id=media_group_id,
                logical_post_key=logical_post_key,
                media_type=detected_media,
                caption_preview=text[:500] or None,
                extractor_version=extraction.version,
                sender_user_id=sender_id,
                sender_is_bot=sender_is_bot,
                source_kind=source_kind,
                message_created_at=created_at_value,
            ),
            extraction.tokens,
        )
        return True


class IndexingService:
    def __init__(self, settings: Settings, repositories: SqliteRepositories) -> None:
        self._settings = settings
        self._repositories = repositories

    async def handle_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        if chat.id != self._settings.archive_chat_id:
            return
        thread_id = message.message_thread_id
        if thread_id is None:
            return
        topic = self._repositories.get(chat.id, thread_id)
        if topic is None or not topic.is_active:
            return
        MessageIndexer(self._repositories, context.bot.id).index(message, chat.id)

    def index_copy(
        self,
        source_message: object,
        *,
        bot_id: int,
        destination_chat_id: int,
        destination_thread_id: int,
        destination_message_id: int,
    ) -> bool:
        topic = self._repositories.get(destination_chat_id, destination_thread_id)
        if topic is None or not topic.is_active:
            return False
        return MessageIndexer(self._repositories, bot_id).index(
            source_message,
            destination_chat_id,
            thread_id_override=destination_thread_id,
            message_id_override=destination_message_id,
            source_kind_override="miki_copy",
        )

    def reindex(self, *, limit: int = 100) -> tuple[int, int | None]:
        posts = self._repositories.reindex_batch(
            EXTRACTOR_VERSION,
            limit=limit,
        )
        processed = 0
        last_id: int | None = None
        for post in posts:
            mappings = self._repositories.list_mappings(
                post.source_chat_id,
                thread_id=post.source_thread_id,
            )
            configured_values = {(item.kind, item.normalized_value) for item in mappings}
            extraction = extract_search_tokens(
                post.caption_preview or "",
                configured_values,
            )
            self._repositories.upsert_post(
                IndexedPostInput(
                    source_chat_id=post.source_chat_id,
                    source_thread_id=post.source_thread_id,
                    source_message_id=post.source_message_id,
                    media_group_id=post.media_group_id,
                    logical_post_key=post.logical_post_key,
                    media_type=post.media_type,
                    caption_preview=post.caption_preview,
                    extractor_version=EXTRACTOR_VERSION,
                    sender_user_id=post.sender_user_id,
                    sender_is_bot=post.sender_is_bot,
                    source_kind=post.source_kind,
                    message_created_at=post.message_created_at,
                ),
                extraction.tokens,
            )
            processed += 1
            last_id = post.id
        return processed, last_id


def _starts_sentence(text: str, start: int, previous_end: int) -> bool:
    if not text[:start].strip():
        return True
    return bool(SENTENCE_END_RE.search(text[previous_end:start]))


def _contains_phrase(normalized_text: str, normalized_phrase: str) -> bool:
    padded_text = f" {normalized_text} "
    return f" {normalized_phrase} " in padded_text
