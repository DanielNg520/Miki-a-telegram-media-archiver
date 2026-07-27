from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from miki_sorter_bot.health_server import HealthServer
from miki_sorter_bot.storage import Storage


def test_health_server_exposes_health_and_metrics() -> None:
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {
            "database": "ok",
            "foreign_keys": True,
            "posts": 2,
            "unavailable_posts": 1,
            "unresolved_dead_letters": 0,
            "jobs": {"pending": 3},
            "deliveries": {"sent": 4},
            "metrics": {"telegram_retries": 5},
        },
    )
    server.start()
    try:
        assert server._server is not None
        port = server._server.server_address[1]
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
            assert json.loads(response.read()) == {
                "database": "ok",
                "ok": True,
                "webhook_wedged": False,
            }
        with urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            body = response.read().decode()
            assert "miki_posts_available 2" in body
            assert 'miki_jobs{state="pending"} 3' in body
            assert "miki_metric_telegram_retries 5" in body
    finally:
        server.stop()


def test_health_server_flags_webhook_wedged() -> None:
    base_status = {
        "database": "ok",
        "foreign_keys": True,
        "posts": 0,
        "unavailable_posts": 0,
        "unresolved_dead_letters": 0,
        "jobs": {},
        "deliveries": {},
        "metrics": {},
    }
    webhook = {
        "enabled": True,
        "healthy": False,
        "wedged": True,
        "url_matches": False,
        "pending_update_count": 12,
        "seconds_since_update": 1800,
        "reconciliations": 4,
        "breaker_state": "open",
        "last_error_age_seconds": 30,
    }
    server = HealthServer(
        host="127.0.0.1",
        port=0,
        status_provider=lambda: {**base_status, "webhook": webhook},
    )
    server.start()
    try:
        port = server._server.server_address[1]
        with pytest.raises(HTTPError) as raised:
            urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
        assert raised.value.code == 503
        assert json.loads(raised.value.read())["webhook_wedged"] is True
        with urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            body = response.read().decode()
            assert "miki_webhook_wedged 1" in body
            assert "miki_webhook_pending_updates 12" in body
            assert "miki_webhook_breaker_open 1" in body
            assert "miki_webhook_url_matches 0" in body
    finally:
        server.stop()


def test_health_server_reads_real_sqlite_from_probe_thread(tmp_path) -> None:
    storage = Storage(tmp_path / "miki.sqlite3")
    repositories = storage.open()
    repositories.increment_metric("jobs_recovered", 2)
    server = HealthServer(host="127.0.0.1", port=0, status_provider=storage.operational_status)
    server.start()
    port = server._server.server_address[1]
    try:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
            assert response.status == 200
        with urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            assert "miki_metric_jobs_recovered 2" in response.read().decode()
    finally:
        server.stop()
        storage.close()


def test_health_server_returns_503_when_status_provider_fails() -> None:
    def fail() -> dict[str, object]:
        raise RuntimeError("database unavailable")

    server = HealthServer(host="127.0.0.1", port=0, status_provider=fail)
    server.start()
    port = server._server.server_address[1]
    try:
        with pytest.raises(HTTPError) as raised:
            urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
        assert raised.value.code == 503
        assert json.loads(raised.value.read()) == {
            "error": "status_unavailable",
            "ok": False,
        }
    finally:
        server.stop()
