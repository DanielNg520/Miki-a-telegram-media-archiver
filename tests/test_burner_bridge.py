from __future__ import annotations

from types import SimpleNamespace

from miki_sorter_bot.burner_bridge import (
    TelethonBridgeClient,
    bridge_once,
)
from miki_sorter_bot.config import Settings
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


class FakeFlood(Exception):
    def __init__(self, seconds: int = 0) -> None:
        super().__init__("flood")
        self.seconds = seconds


class FakeForbidden(Exception):
    pass


class FakeBridgeClient:
    def __init__(
        self,
        *,
        noforwards: bool = False,
        latest: int = 0,
        messages: list[int] | None = None,
        forward_error: Exception | None = None,
    ) -> None:
        self.noforwards = noforwards
        self._latest = latest
        self._messages = [SimpleNamespace(id=m) for m in (messages or [])]
        self.forwarded: list[tuple[int, int, int, int]] = []
        self._forward_error = forward_error

    def is_noforwards(self, chat_id: int) -> bool:
        return self.noforwards

    def latest_message_id(self, chat_id: int) -> int:
        return self._latest

    def iter_new_media(self, chat_id: int, min_id: int, limit: int):
        return [m for m in self._messages if m.id > min_id][:limit]

    def forward(self, chat_id: int, message_id: int, dest_chat: int, dest_thread: int) -> None:
        if self._forward_error is not None:
            error = self._forward_error
            self._forward_error = None  # raise once, then succeed
            raise error
        self.forwarded.append((chat_id, message_id, dest_chat, dest_thread))


def test_add_remove_and_list_bridges(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    bridge = repositories.add_bridge(-9001, 7, created_by_user_id=55)
    assert bridge.foreign_chat_id == -9001
    assert bridge.last_forwarded_id == 0
    assert [b.foreign_chat_id for b in repositories.list_active_bridges()] == [-9001]

    # Re-adding reactivates / updates the target topic.
    repositories.add_bridge(-9001, 9)
    assert repositories.get_bridge(-9001).source_thread_id == 9

    assert repositories.remove_bridge(-9001) is True
    assert repositories.list_active_bridges() == []
    assert repositories.remove_bridge(-9001) is False


def test_first_pass_seeds_without_forwarding(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.add_bridge(-9001, 7)
    client = FakeBridgeClient(latest=500, messages=[490, 495, 510])

    outcome = bridge_once(repositories, _settings(), client=client)

    assert outcome.seeded == [-9001]
    assert outcome.total_forwarded == 0
    assert client.forwarded == []
    assert repositories.get_bridge(-9001).last_forwarded_id == 500


def test_forwards_new_media_into_source_topic(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.add_bridge(-9001, 7)
    repositories.update_bridge_checkpoint(repositories.get_bridge(-9001).id, 500)
    client = FakeBridgeClient(messages=[500, 501, 502])

    outcome = bridge_once(repositories, _settings(), client=client)

    assert outcome.total_forwarded == 2  # 501, 502 (> 500)
    assert client.forwarded == [(-9001, 501, -100, 7), (-9001, 502, -100, 7)]
    assert repositories.get_bridge(-9001).last_forwarded_id == 502


def test_noforwards_group_is_detected_and_disabled(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.add_bridge(-9001, 7)
    client = FakeBridgeClient(noforwards=True)

    outcome = bridge_once(repositories, _settings(), client=client)

    assert -9001 in outcome.disabled
    assert "noforwards" in outcome.disabled[-9001]
    bridge = repositories.get_bridge(-9001)
    assert bridge.is_active is False
    assert "noforwards" in bridge.last_error
    assert repositories.list_active_bridges() == []


def test_noforwards_mid_forward_disables_bridge(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    bridge_id = repositories.add_bridge(-9001, 7).id
    repositories.update_bridge_checkpoint(bridge_id, 500)
    client = FakeBridgeClient(messages=[501], forward_error=FakeForbidden())

    outcome = bridge_once(
        repositories, _settings(), client=client, noforwards_types=(FakeForbidden,)
    )

    assert -9001 in outcome.disabled
    assert repositories.get_bridge(-9001).is_active is False


def test_flood_wait_retries_forward(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    bridge_id = repositories.add_bridge(-9001, 7).id
    repositories.update_bridge_checkpoint(bridge_id, 500)
    client = FakeBridgeClient(messages=[501], forward_error=FakeFlood(0))
    slept: list[float] = []

    outcome = bridge_once(
        repositories,
        _settings(),
        client=client,
        sleep=slept.append,
        flood_wait_types=(FakeFlood,),
    )

    assert outcome.total_forwarded == 1
    assert slept == [1.0]
    assert client.forwarded == [(-9001, 501, -100, 7)]


def test_per_bridge_limit_caps_work(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    bridge_id = repositories.add_bridge(-9001, 7).id
    repositories.update_bridge_checkpoint(bridge_id, 0)
    # checkpoint 0 would seed; set to 1 so it forwards.
    repositories.update_bridge_checkpoint(bridge_id, 1)
    client = FakeBridgeClient(messages=list(range(2, 20)))

    outcome = bridge_once(repositories, _settings(), client=client, per_bridge_limit=3)

    assert outcome.total_forwarded == 3
    assert repositories.get_bridge(-9001).last_forwarded_id == 4


def test_telethon_client_filters_non_media() -> None:
    # Underlying iter_messages yields a mix; only media should pass through.
    photo = SimpleNamespace(id=2, message="", grouped_id=None, sender_id=1, sender=None, photo=True)
    text = SimpleNamespace(id=3, message="hi", grouped_id=None, sender_id=1, sender=None)
    video = SimpleNamespace(id=4, message="", grouped_id=None, sender_id=1, sender=None, video=True)

    class _Underlying:
        def iter_messages(self, chat_id, *, min_id=0, reverse=False):
            return [photo, text, video]

    client = TelethonBridgeClient(_Underlying())
    ids = [m.id for m in client.iter_new_media(-9001, 0, 10)]
    assert ids == [2, 4]  # text (id=3) filtered out
