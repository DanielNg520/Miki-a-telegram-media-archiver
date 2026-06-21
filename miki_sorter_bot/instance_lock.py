from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from pathlib import Path
from typing import TextIO


class AlreadyRunningError(RuntimeError):
    """Raised when another local process owns a bot token's runtime lock."""


class InstanceLock:
    """Host-local, token-scoped advisory lock held for a worker's lifetime."""

    def __init__(
        self,
        bot_token: str,
        *,
        role: str,
        lock_directory: Path | None = None,
    ) -> None:
        token_fingerprint = hashlib.sha256(
            bot_token.encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()[:24]
        base_directory = lock_directory or (
            Path(tempfile.gettempdir()) / f"miki-sorter-{os.getuid()}"
        )
        self._directory = base_directory
        self._path = base_directory / f"{token_fingerprint}.lock"
        self._role = role
        self._handle: TextIO | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="utf-8")
        os.chmod(self._path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.seek(0)
            owner = handle.read().strip() or "owner details unavailable"
            handle.close()
            raise AlreadyRunningError(
                f"Another Miki process already owns this bot token ({owner})."
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} role={self._role}")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
