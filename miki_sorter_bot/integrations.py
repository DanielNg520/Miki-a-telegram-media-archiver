from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from miki_sorter_bot.config import IntegrationClient, Settings
from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.sorting import SortingService

CONTRACT_VERSION = 1
MAX_BODY_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class IntegrationResponse:
    status: int
    body: dict[str, Any]


class IntegrationError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class IntegrationService:
    def __init__(
        self,
        settings: Settings,
        repositories: SqliteRepositories,
        sorting: SortingService,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._repositories = repositories
        self._sorting = sorting
        self._clients = {client.client_id: client for client in settings.integration_clients}
        self._now = now

    def dispatch(
        self,
        body: bytes,
        *,
        client_id: str,
        timestamp: str,
        nonce: str,
        signature: str,
    ) -> IntegrationResponse:
        operation = "unknown"
        request_id: str | None = None
        try:
            client = self._authenticate(body, client_id, timestamp, nonce, signature)
            payload = self._parse_body(body)
            operation = payload["operation"]
            request_id = payload["request_id"]
            required_scope = {
                "route.preview": "submit",
                "library.search": "search",
                "audit.list": "admin",
            }.get(operation)
            if required_scope is None:
                raise IntegrationError(400, "unknown_operation", "Unsupported operation.")
            if required_scope not in client.scopes:
                raise IntegrationError(403, "scope_denied", "Client lacks the required scope.")
            result = self._execute(operation, payload["data"])
            self._audit(client_id, operation, "success", request_id)
            return IntegrationResponse(
                200,
                {
                    "version": CONTRACT_VERSION,
                    "request_id": request_id,
                    "ok": True,
                    "result": result,
                },
            )
        except IntegrationError as error:
            self._audit(client_id or "unknown", operation, "denied", request_id, error.code)
            return IntegrationResponse(
                error.status,
                {
                    "version": CONTRACT_VERSION,
                    "request_id": request_id,
                    "ok": False,
                    "error": {"code": error.code, "message": error.message},
                },
            )
        except Exception:
            self._audit(client_id or "unknown", operation, "failed", request_id, "internal_error")
            return IntegrationResponse(
                500,
                {
                    "version": CONTRACT_VERSION,
                    "request_id": request_id,
                    "ok": False,
                    "error": {
                        "code": "internal_error",
                        "message": "The integration request could not be completed.",
                    },
                },
            )

    def _authenticate(
        self,
        body: bytes,
        client_id: str,
        timestamp: str,
        nonce: str,
        signature: str,
    ) -> IntegrationClient:
        if len(body) > MAX_BODY_BYTES:
            raise IntegrationError(413, "body_too_large", "Request body exceeds 64 KiB.")
        client = self._clients.get(client_id)
        if client is None:
            raise IntegrationError(401, "unknown_client", "Unknown integration client.")
        if not nonce or len(nonce) > 128:
            raise IntegrationError(401, "invalid_nonce", "Nonce is required and must be bounded.")
        try:
            timestamp_value = int(timestamp)
        except ValueError as error:
            raise IntegrationError(401, "invalid_timestamp", "Timestamp must be Unix seconds.") from error
        now = int(self._now())
        if abs(now - timestamp_value) > self._settings.integration_signature_ttl:
            raise IntegrationError(401, "stale_timestamp", "Timestamp is outside the allowed window.")
        canonical = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body
        expected = hmac.new(client.secret.encode(), canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature.casefold()):
            raise IntegrationError(401, "invalid_signature", "Signature verification failed.")
        claimed = self._repositories.claim_integration_nonce(
            client_id,
            nonce,
            timestamp_value,
            oldest_allowed=now - self._settings.integration_signature_ttl,
        )
        if not claimed:
            self._repositories.increment_metric("integration_replays", 1)
            raise IntegrationError(409, "replay_detected", "This nonce has already been used.")
        window_start = now - (now % 60)
        allowed, _ = self._repositories.consume_integration_quota(
            client_id,
            window_start,
            client.requests_per_minute,
        )
        if not allowed:
            self._repositories.increment_metric("integration_throttles", 1)
            raise IntegrationError(429, "quota_exceeded", "Client request quota exceeded.")
        return client

    def _parse_body(self, body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IntegrationError(400, "invalid_json", "Body must be valid UTF-8 JSON.") from error
        if not isinstance(payload, dict):
            raise IntegrationError(400, "invalid_request", "Request body must be an object.")
        if payload.get("version") != CONTRACT_VERSION:
            raise IntegrationError(400, "unsupported_version", "Only contract version 1 is supported.")
        request_id = payload.get("request_id")
        operation = payload.get("operation")
        data = payload.get("data", {})
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise IntegrationError(400, "invalid_request_id", "request_id is required and bounded.")
        if not isinstance(operation, str) or not isinstance(data, dict):
            raise IntegrationError(400, "invalid_request", "operation and object data are required.")
        return {
            "version": CONTRACT_VERSION,
            "request_id": request_id,
            "operation": operation,
            "data": data,
        }

    def _execute(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        if operation == "route.preview":
            text = data.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 4096:
                raise IntegrationError(400, "invalid_text", "text is required and limited to 4096.")
            decision = self._sorting.explain(text)
            return {
                "status": decision.status,
                "topic": (
                    {"chat_id": decision.topic.chat_id, "thread_id": decision.topic.thread_id}
                    if decision.topic
                    else None
                ),
                "reason": decision.reason,
            }
        if operation == "library.search":
            return self._search(data)
        if operation == "audit.list":
            limit = data.get("limit", 50)
            if not isinstance(limit, int):
                raise IntegrationError(400, "invalid_limit", "limit must be an integer.")
            try:
                events = self._repositories.list_audit_events(limit)
            except ValueError as error:
                raise IntegrationError(400, "invalid_limit", str(error)) from error
            return {"events": events}
        raise IntegrationError(400, "unknown_operation", "Unsupported operation.")

    def _search(self, data: dict[str, Any]) -> dict[str, Any]:
        thread_id = data.get("topic_id")
        keywords = data.get("keywords")
        match_mode = data.get("match", "all")
        limit = data.get("limit", self._settings.default_request_limit)
        if not isinstance(thread_id, int) or thread_id <= 0:
            raise IntegrationError(400, "invalid_topic", "topic_id must be a positive integer.")
        if not isinstance(keywords, list) or not keywords or not all(
            isinstance(value, str) and value.strip() for value in keywords
        ):
            raise IntegrationError(400, "invalid_keywords", "keywords must be non-empty strings.")
        if match_mode not in {"all", "any"}:
            raise IntegrationError(400, "invalid_match", "match must be all or any.")
        if not isinstance(limit, int) or not 1 <= limit <= self._settings.max_request_limit:
            raise IntegrationError(
                400,
                "invalid_limit",
                f"limit must be between 1 and {self._settings.max_request_limit}.",
            )
        topic = self._repositories.get(self._settings.archive_chat_id, thread_id)
        if topic is None or not topic.is_active:
            raise IntegrationError(404, "topic_not_found", "Topic is unknown or inactive.")
        normalized = tuple(
            dict.fromkeys(
                " ".join(value.casefold().removeprefix("#").split()) for value in keywords
            )
        )
        posts = self._repositories.search_posts(
            self._settings.archive_chat_id,
            thread_id,
            normalized,
            match_mode,
            limit,
        )
        logical_count = len({post.logical_post_key for post in posts})
        return {
            "logical_count": logical_count,
            "posts": [
                {
                    "chat_id": post.source_chat_id,
                    "thread_id": post.source_thread_id,
                    "message_id": post.source_message_id,
                    "logical_post_key": post.logical_post_key,
                    "media_type": post.media_type,
                }
                for post in posts
            ],
        }

    def _audit(
        self,
        client_id: str,
        action: str,
        outcome: str,
        request_id: str | None,
        error_code: str | None = None,
    ) -> None:
        details = {"request_id": request_id}
        if error_code is not None:
            details["error_code"] = error_code
        self._repositories.add_audit_event(
            actor_type="integration",
            actor_id=client_id,
            action=action,
            resource_type="integration_request",
            resource_id=request_id,
            outcome=outcome,
            details=details,
            correlation_id=request_id,
        )


def sign_request(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    canonical = timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
