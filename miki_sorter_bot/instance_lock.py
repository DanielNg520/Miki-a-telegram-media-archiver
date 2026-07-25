from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import TextIO

try:  # POSIX
    import fcntl

    _HAVE_FCNTL = True
except ModuleNotFoundError:  # Windows
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False

if not _HAVE_FCNTL:
    import msvcrt

# Windows byte-range locks (msvcrt.locking) are MANDATORY and lock at the current
# file position. Anchor every holder's lock on the SAME byte, placed FAR beyond
# the owner text so other processes can still READ that text (a mandatory lock
# over byte 0 would block the "who owns it?" read on the failure path). The
# region need not contain real bytes — Windows allows locking past EOF.
_LOCK_OFFSET = 1 << 30


def _current_uid() -> int | str:
    """Best-effort per-user tag for the lock directory (``os.getuid`` is POSIX-only)."""
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return getuid()
    return os.environ.get("USERNAME") or os.environ.get("USER") or "user"


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
            Path(tempfile.gettempdir()) / f"miki-sorter-{_current_uid()}"
        )
        self._directory = base_directory
        self._path = base_directory / f"{token_fingerprint}.lock"
        self._role = role
        self._handle: TextIO | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _try_lock(self, handle: TextIO) -> bool:
        """Attempt a non-blocking exclusive lock. Returns ``False`` if already held."""
        if _HAVE_FCNTL:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True
        # msvcrt.locking locks a byte range at the CURRENT file position (not the
        # whole file like flock), so every holder must lock the SAME offset or
        # two processes lock disjoint ranges and never conflict.
        handle.seek(_LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        finally:
            handle.seek(0)  # leave the position at the owner text for read/write
        return True

    def _unlock(self, handle: TextIO) -> None:
        if _HAVE_FCNTL:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:
            try:
                handle.seek(_LOCK_OFFSET)  # unlock the same range we locked
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="utf-8")
        os.chmod(self._path, 0o600)
        if not self._try_lock(handle):
            handle.seek(0)
            owner = handle.read().strip() or "owner details unavailable"
            handle.close()
            raise AlreadyRunningError(
                f"Another Miki process already owns this bot token ({owner})."
            )
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
            self._unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
