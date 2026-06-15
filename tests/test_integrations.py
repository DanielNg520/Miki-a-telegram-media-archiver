from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from miki_sorter_bot.config import IntegrationClient
from miki_sorter_bot.indexing import MessageIndexer
from miki_sorter_bot.integrations import IntegrationService, sign_request
from miki_sorter_bot.repositories import SqliteRepositories

NOW = 1_750_000_000
SECRET = "integration-secret-value"


def _settings(*, scopes: frozenset[str], quota: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        integration_clients=(
            IntegrationClient("client-a", SECRET, scopes, quota),
        ),
        integration_signature_ttl=300,
        archive_chat_id=-200,
        default_request_limit=20,
        max_request_limit=100,
    )


def _dispatch(
    service: IntegrationService,
    payload: dict[str, object],
    *,
    nonce: str = "nonce-1",
    timestamp: str = str(NOW),
    secret: str = SECRET,
):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return service.dispatch(
        body,
        client_id="client-a",
        timestamp=timestamp,
        nonce=nonce,
        signature=sign_request(secret, timestamp, nonce, body),
    )


def test_signed_route_preview_returns_versioned_envelope(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    sorting = SimpleNamespace(
        explain=lambda _: SimpleNamespace(
            status="matched",
            topic=SimpleNamespace(chat_id=-200, thread_id=9),
            reason="keyword:tokyo",
        )
    )
    service = IntegrationService(
        _settings(scopes=frozenset({"submit"})),
        repositories,
        sorting,
        now=lambda: NOW,
    )

    response = _dispatch(
        service,
        {
            "version": 1,
            "request_id": "req-1",
            "operation": "route.preview",
            "data": {"text": "Tokyo"},
        },
    )

    assert response.status == 200
    assert response.body["ok"] is True
    assert response.body["result"]["topic"]["thread_id"] == 9


def test_tampering_replay_stale_time_and_scope_are_rejected(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    service = IntegrationService(
        _settings(scopes=frozenset({"search"})),
        repositories,
        SimpleNamespace(),
        now=lambda: NOW,
    )
    payload = {
        "version": 1,
        "request_id": "req-1",
        "operation": "route.preview",
        "data": {"text": "Tokyo"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    invalid = service.dispatch(
        body,
        client_id="client-a",
        timestamp=str(NOW),
        nonce="bad-signature",
        signature="0" * 64,
    )
    stale = _dispatch(service, payload, nonce="stale", timestamp=str(NOW - 301))
    first = _dispatch(service, payload, nonce="replay")
    replay = _dispatch(service, payload, nonce="replay")

    assert invalid.body["error"]["code"] == "invalid_signature"
    assert stale.body["error"]["code"] == "stale_timestamp"
    assert first.body["error"]["code"] == "scope_denied"
    assert replay.body["error"]["code"] == "replay_detected"


def test_client_quota_is_enforced_atomically(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    service = IntegrationService(
        _settings(scopes=frozenset({"submit"}), quota=1),
        repositories,
        SimpleNamespace(
            explain=lambda _: SimpleNamespace(status="unmatched", topic=None, reason="none")
        ),
        now=lambda: NOW,
    )
    payload = {
        "version": 1,
        "request_id": "req-1",
        "operation": "route.preview",
        "data": {"text": "none"},
    }

    assert _dispatch(service, payload, nonce="one").status == 200
    limited = _dispatch(service, payload, nonce="two")
    assert limited.status == 429
    assert limited.body["error"]["code"] == "quota_exceeded"
    assert repositories.metrics_snapshot()["integration_throttles"] == 1


def test_search_returns_safe_telegram_references_and_audits(database_connection) -> None:
    repositories = SqliteRepositories(database_connection)
    repositories.register_topic(-200, 9, "Japan")
    repositories.add_mapping(-200, 9, "keyword", "Tokyo", 1)
    message = SimpleNamespace(
        message_id=12,
        message_thread_id=9,
        media_group_id=None,
        caption="Tokyo",
        text=None,
        date=datetime(2026, 6, 13, tzinfo=UTC),
        from_user=SimpleNamespace(id=10, is_bot=False),
        photo=[object()],
        animation=None,
        audio=None,
        document=None,
        sticker=None,
        video=None,
        video_note=None,
        voice=None,
    )
    MessageIndexer(repositories, bot_id=99).index(message, -200)
    service = IntegrationService(
        _settings(scopes=frozenset({"search"})),
        repositories,
        SimpleNamespace(),
        now=lambda: NOW,
    )

    response = _dispatch(
        service,
        {
            "version": 1,
            "request_id": "search-1",
            "operation": "library.search",
            "data": {"topic_id": 9, "keywords": ["TOKYO"]},
        },
    )

    assert response.status == 200
    assert response.body["result"]["posts"][0]["message_id"] == 12
    event = repositories.list_audit_events()[0]
    assert event["action"] == "library.search"
    assert event["details"] == {"request_id": "search-1"}


def test_admin_scope_can_read_audit_without_recursive_sensitive_payload(
    database_connection,
) -> None:
    repositories = SqliteRepositories(database_connection)
    service = IntegrationService(
        _settings(scopes=frozenset({"admin"})),
        repositories,
        SimpleNamespace(),
        now=lambda: NOW,
    )

    response = _dispatch(
        service,
        {
            "version": 1,
            "request_id": "audit-1",
            "operation": "audit.list",
            "data": {"limit": 10},
        },
    )

    assert response.status == 200
    assert "events" in response.body["result"]
