"""Burner layer — Phase 2 result reporter.

The bot enqueues burner commands and the burner executes them out-of-band. This
reporter closes the loop: it polls the shared DB for finished commands that have
not yet been reported and posts the outcome back into the chat the command came
from, then marks it reported so it is announced exactly once.
"""

from __future__ import annotations

import json
import logging

from miki_sorter_bot.repositories import BurnerCommandRecord, SqliteRepositories

logger = logging.getLogger(__name__)


class BurnerResultReporter:
    def __init__(
        self,
        repositories: SqliteRepositories,
        *,
        limit: int = 20,
        stale_after_seconds: int = 1800,
    ) -> None:
        self._repositories = repositories
        self._limit = limit
        # Generous by default: larger than any legitimate handler runtime (the
        # slowest, backup_now, is an upload of a <300 MB artifact), so a healthy
        # slow command is never falsely reclaimed.
        self._stale_after_seconds = stale_after_seconds

    async def run_once(self, bot: object) -> int:
        """Reclaim stale running commands, then report all unreported terminal ones."""

        reclaimed = self._repositories.fail_stale_running_burner_commands(
            self._stale_after_seconds
        )
        if reclaimed:
            logger.warning("Reclaimed %d stale running burner command(s).", reclaimed)
        commands = self._repositories.list_unreported_burner_results(self._limit)
        reported = 0
        for command in commands:
            chat_id = command.payload.get("chat_id")
            if chat_id is not None:
                try:
                    await bot.send_message(  # type: ignore[attr-defined]
                        chat_id=chat_id,
                        message_thread_id=command.payload.get("thread_id"),
                        text=format_result(command),
                    )
                    reported += 1
                except Exception:  # never let one bad chat stall the rest
                    logger.exception(
                        "Failed to report burner command %s to chat %s.",
                        command.id,
                        chat_id,
                    )
            # Mark reported regardless: a chat we can't reach must not loop forever.
            self._repositories.mark_burner_command_reported(command.id)
        return reported


def format_result(command: BurnerCommandRecord) -> str:
    header = f"Burner command '{command.kind}' (#{command.id}) {command.status}."
    if command.status == "completed":
        if command.result:
            return f"{header}\n{json.dumps(command.result, ensure_ascii=False, indent=2)}"
        return header
    if command.last_error:
        return f"{header}\n{command.last_error}"
    return header
