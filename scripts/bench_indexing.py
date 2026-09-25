from __future__ import annotations

import argparse
import pathlib
import random
import sys
import tempfile
import time
from dataclasses import dataclass

from miki_sorter_bot.indexing import MessageIndexer
from miki_sorter_bot.storage import Storage


@dataclass
class FakeUser:
    id: int
    is_bot: bool = False


@dataclass
class FakeMessage:
    message_id: int
    message_thread_id: int | None
    media_group_id: str | None
    caption: str | None
    text: str | None
    photo: bool
    from_user: FakeUser
    date: object


def build_messages(posts: int) -> list[FakeMessage]:
    rng = random.Random(12345)
    words = ["sunset", "lake", "mountain", "hike", "food", "coffee", "city", "trip"]
    names = ["#travel", "#nature", "#photography", "John", "Alice", "MountEverest", "Bob", "Eva"]
    messages: list[FakeMessage] = []
    msg_id = 1
    album_counter = 1
    i = 0
    while i < posts:
        album_size = 1
        if rng.random() < 0.2:
            album_size = rng.randint(2, 4)
            album_size = min(album_size, posts - i)
        media_group_id = f"album{album_counter}" if album_size > 1 else None
        thread_id = 100
        for _ in range(album_size):
            n_words = rng.randint(2, 6)
            caption_parts = [rng.choice(words) for _ in range(n_words)]
            for _ in range(2):
                if rng.random() < 0.5:
                    caption_parts.insert(rng.randint(0, len(caption_parts)), rng.choice(names))
            caption = " ".join(caption_parts)
            messages.append(
                FakeMessage(
                    message_id=msg_id,
                    message_thread_id=thread_id,
                    media_group_id=media_group_id,
                    caption=caption,
                    text=None,
                    photo=True,
                    from_user=FakeUser(id=1),
                    date=object(),
                )
            )
            msg_id += 1
            i += 1
        album_counter += 1
    return messages


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--posts", type=int, default=20000)
    parser.add_argument("--mappings", type=int, default=0)
    parser.add_argument("--reindex", action="store_true")
    args = parser.parse_args()

    messages = build_messages(args.posts)
    list_mappings_calls = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        storage = Storage(pathlib.Path(tmpdir) / "bench.db")
        repositories = storage.open()

        if args.mappings > 0:
            repositories.register_topic(chat_id=1, thread_id=100, name="bench")
            for n in range(args.mappings):
                repositories.add_mapping(
                    chat_id=1,
                    thread_id=100,
                    kind="hashtag",
                    value=f"tag{n}",
                    created_by_user_id=1,
                )

        original_list_mappings = repositories.list_mappings

        def counted_list_mappings(chat_id):
            nonlocal list_mappings_calls
            list_mappings_calls += 1
            return original_list_mappings(chat_id)

        repositories.list_mappings = counted_list_mappings

        indexer = MessageIndexer(repositories, bot_id=1)

        def run_pass(label: str) -> None:
            nonlocal list_mappings_calls
            list_mappings_calls = 0
            start = time.perf_counter()
            true_count = 0
            for message in messages:
                result = indexer.index(message, chat_id=1)
                assert result is True
                true_count += 1
            elapsed = time.perf_counter() - start
            pps = len(messages) / elapsed if elapsed > 0 else float("inf")
            print(
                f"{label}: {elapsed:.3f}s, {pps:.0f} posts/s, "
                f"list_mappings calls={list_mappings_calls}, true_count={true_count}"
            )

        run_pass("first pass")
        if args.reindex:
            run_pass("reindex pass")

        storage.close()


if __name__ == "__main__":
    sys.exit(main())
