from __future__ import annotations

import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)
MetricProvider = Callable[[], dict[str, Any]]


class HealthServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        status_provider: MetricProvider,
    ) -> None:
        self._host = host
        self._port = port
        self._status_provider = status_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        handler = _handler(self._status_provider)
        self._server = ThreadingHTTPServer((self._host, self._port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="miki-health-server",
            daemon=True,
        )
        self._thread.start()
        LOGGER.info("Health server started", extra={"listen": self._host, "port": self._port})

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        LOGGER.info("Health server stopped")


def _handler(status_provider: MetricProvider) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            try:
                status = status_provider()
            except Exception:
                LOGGER.exception("Health status provider failed")
                self._send_json(
                    {"ok": False, "error": "status_unavailable"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            if self.path == "/healthz":
                healthy = status.get("database") == "ok" and status.get("foreign_keys") is True
                body = {"ok": healthy, "database": status.get("database")}
                self._send_json(body, HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if self.path == "/metrics":
                self._send_text(_metrics_text(status))
                return
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send_json(self, body: dict[str, Any], status: HTTPStatus) -> None:
            payload = json.dumps(body, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_text(self, body: str) -> None:
            payload = body.encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def _metrics_text(status: dict[str, Any]) -> str:
    lines = [
        f"miki_posts_available {int(status.get('posts', 0))}",
        f"miki_posts_unavailable {int(status.get('unavailable_posts', 0))}",
        f"miki_dead_letters_unresolved {int(status.get('unresolved_dead_letters', 0))}",
    ]
    for state, count in sorted(status.get("jobs", {}).items()):
        lines.append(f'miki_jobs{{state="{_label(state)}"}} {int(count)}')
    for state, count in sorted(status.get("deliveries", {}).items()):
        lines.append(f'miki_deliveries{{state="{_label(state)}"}} {int(count)}')
    for name, value in sorted(status.get("metrics", {}).items()):
        lines.append(f"miki_metric_{_metric_name(name)} {int(value)}")
    return "\n".join(lines) + "\n"


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _metric_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_:]", "_", value)
