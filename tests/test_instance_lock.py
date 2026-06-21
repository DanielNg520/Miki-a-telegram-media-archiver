from __future__ import annotations

import subprocess
import sys

import pytest

from miki_sorter_bot.instance_lock import AlreadyRunningError, InstanceLock


def test_same_token_cannot_be_locked_twice_and_can_be_reacquired(tmp_path) -> None:
    first = InstanceLock("shared-token", role="sorter", lock_directory=tmp_path)
    second = InstanceLock("shared-token", role="show-ids", lock_directory=tmp_path)

    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError, match=r"pid=\d+ role=sorter"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_different_bot_tokens_have_independent_locks(tmp_path) -> None:
    first = InstanceLock("token-one", role="sorter", lock_directory=tmp_path)
    second = InstanceLock("token-two", role="sorter", lock_directory=tmp_path)

    with first, second:
        assert first.path != second.path


def test_lock_blocks_a_separate_process(tmp_path) -> None:
    lock = InstanceLock("cross-process-token", role="sorter", lock_directory=tmp_path)
    script = """
from pathlib import Path
import sys
from miki_sorter_bot.instance_lock import AlreadyRunningError, InstanceLock

lock = InstanceLock(sys.argv[1], role="child", lock_directory=Path(sys.argv[2]))
try:
    lock.acquire()
except AlreadyRunningError:
    raise SystemExit(23)
raise SystemExit(0)
"""

    with lock:
        result = subprocess.run(
            [sys.executable, "-c", script, "cross-process-token", str(tmp_path)],
            check=False,
        )

    assert result.returncode == 23
