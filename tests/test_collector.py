import json
import hashlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from miki_sorter_bot.collector import CollectorClient, CollectorError, RetryPolicy


class _Handler(BaseHTTPRequestHandler):
    response = {"matches": ["CR"]}
    failures_remaining = 0

    def do_POST(self) -> None:
        if self.failures_remaining:
            type(self).failures_remaining -= 1
            self.send_error(503)
            return
        length = int(self.headers["Content-Length"])
        self.server.request_body = json.loads(self.rfile.read(length))
        body = json.dumps(self.response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class CollectorClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = CollectorClient(
            f"http://127.0.0.1:{self.server.server_port}", "key", "gvdb", 1.0
        )

    def tearDown(self) -> None:
        _Handler.response = {"matches": ["CR"]}
        _Handler.failures_remaining = 0
        self.server.shutdown()
        self.server.server_close()

    async def test_lookup_posts_terms_and_normalizes_matches(self) -> None:
        self.assertEqual(await self.client.lookup({"cr", "fc"}), {"cr"})
        self.assertEqual(self.server.request_body, {"terms": ["cr", "fc"]})

    async def test_lookup_rejects_invalid_response(self) -> None:
        _Handler.response = {"matches": "CR"}
        with self.assertRaises(CollectorError):
            await self.client.lookup({"cr"})

    async def test_lookup_retries_transient_server_errors(self) -> None:
        _Handler.failures_remaining = 2
        client = CollectorClient(
            f"http://127.0.0.1:{self.server.server_port}",
            "key",
            "gvdb",
            1.0,
            retry_policy=RetryPolicy(attempts=3, base_delay=0, max_delay=0),
        )

        self.assertEqual(await client.lookup({"CR"}), {"cr"})


class RealCollectorContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_client_matches_real_collector_api_contract(self) -> None:
        collector_root = Path(__file__).resolve().parents[2] / "data-collector"
        sys.path.insert(0, str(collector_root))
        try:
            from data_collector.api import CollectorHTTPServer
            from data_collector.jobs import JobRepository
            from data_collector.repository import CollectorRepository
            from data_collector.service_config import (
                ClientConfig,
                ClientRegistry,
                ServiceConfig,
            )
            from data_collector.worker import WorkerService

            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                token = "contract-secret"
                client_config = ClientConfig(
                    client_id="miki",
                    api_key_hash=hashlib.sha256(token.encode()).hexdigest(),
                    allowed_domains=frozenset({"example.com"}),
                    cookie_files={},
                )
                service = ServiceConfig(
                    state_root=root / "state",
                    credentials_root=root / "credentials",
                    database_root=root / "databases",
                    control_database=root / "state" / "control.sqlite3",
                )
                database = service.client_database("miki", "gvdb")
                repository = CollectorRepository(database)
                repository.enqueue(["https://example.com/"])
                url = repository.claim()
                repository.complete(url, {"CR", "FC"}, [])
                repository.close()

                jobs = JobRepository(service.control_database)
                registry = ClientRegistry({"miki": client_config})
                worker = WorkerService(
                    config=service,
                    clients=registry.as_dict(),
                    jobs=jobs,
                )
                server = CollectorHTTPServer(
                    ("127.0.0.1", 0),
                    clients=registry,
                    config=service,
                    jobs=jobs,
                    worker=worker,
                )
                worker.start()
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    client = CollectorClient(
                        f"http://127.0.0.1:{server.server_port}",
                        token,
                        "gvdb",
                        1.0,
                    )
                    self.assertEqual(await client.lookup({"cr", "missing"}), {"cr"})
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.close()
                    jobs.close()
        finally:
            sys.path.remove(str(collector_root))


if __name__ == "__main__":
    unittest.main()
