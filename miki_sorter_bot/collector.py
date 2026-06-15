from __future__ import annotations

import asyncio
import json
import random
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class CollectorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.2
    max_delay: float = 2.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if self.base_delay < 0 or self.max_delay < self.base_delay:
            raise ValueError("retry delays must be non-negative and ordered")

    def delay(self, attempt: int) -> float:
        return min(self.base_delay * (2**attempt), self.max_delay) * random.uniform(
            0.8,
            1.2,
        )


class CollectorClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        database: str,
        timeout: float,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._database = database
        self._timeout = timeout
        self._retry_policy = retry_policy or RetryPolicy()

    async def lookup(self, terms: set[str]) -> set[str]:
        if not terms:
            return set()
        normalized = sorted({term.strip().casefold() for term in terms if term.strip()})
        if not normalized:
            return set()
        return await asyncio.to_thread(self._lookup_with_retry, normalized)

    def _lookup_with_retry(self, terms: list[str]) -> set[str]:
        last_error: CollectorError | None = None
        for attempt in range(self._retry_policy.attempts):
            try:
                return self._lookup_sync(terms)
            except _TransientCollectorError as error:
                last_error = error
                if attempt + 1 < self._retry_policy.attempts:
                    import time

                    time.sleep(self._retry_policy.delay(attempt))
        raise last_error or CollectorError("collector lookup failed")

    def _lookup_sync(self, terms: list[str]) -> set[str]:
        database = urllib.parse.quote(self._database, safe="")
        request = urllib.request.Request(
            f"{self._base_url}/v1/databases/{database}/terms",
            data=json.dumps({"terms": terms}, separators=(",", ":")).encode(),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                if response.headers.get_content_type() != "application/json":
                    raise CollectorError("collector returned a non-JSON response")
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                message = _http_error_message(error)
                exception = (
                    _TransientCollectorError
                    if error.code in {408, 425, 429, 500, 502, 503, 504}
                    else CollectorError
                )
                raise exception(
                    f"collector lookup failed with HTTP {error.code}: {message}"
                ) from error
            finally:
                error.close()
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            raise _TransientCollectorError(f"collector lookup failed: {error}") from error
        except json.JSONDecodeError as error:
            raise CollectorError("collector returned invalid JSON") from error

        matches = payload.get("matches")
        if not isinstance(matches, list) or not all(isinstance(term, str) for term in matches):
            raise CollectorError("collector returned an invalid lookup response")
        return {term.casefold() for term in matches}


class _TransientCollectorError(CollectorError):
    pass


def _http_error_message(error: urllib.error.HTTPError) -> str:
    try:
        payload: Any = json.load(error)
        message = payload.get("error", {}).get("message")
        if isinstance(message, str) and message:
            return message
    except (json.JSONDecodeError, AttributeError, OSError):
        pass
    return str(error.reason)
