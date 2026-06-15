from __future__ import annotations

import json
from urllib.request import urlopen

from miki_sorter_bot.health_server import HealthServer


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
            assert json.loads(response.read()) == {"database": "ok", "ok": True}
        with urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            body = response.read().decode()
            assert "miki_posts_available 2" in body
            assert 'miki_jobs{state="pending"} 3' in body
            assert "miki_metric_telegram_retries 5" in body
    finally:
        server.stop()
