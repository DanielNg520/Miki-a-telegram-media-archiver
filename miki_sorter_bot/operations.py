from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from miki_sorter_bot.repositories import SqliteRepositories
from miki_sorter_bot.storage import Storage


@dataclass(frozen=True, slots=True)
class HealthReport:
    database_ok: bool
    telegram_ok: bool
    details: dict[str, Any]

    @property
    def healthy(self) -> bool:
        return self.database_ok and self.telegram_ok


class OperationsService:
    def __init__(
        self,
        repositories: SqliteRepositories,
        storage: Storage,
        *,
        backup_directory: Path,
        transient_retention_days: int,
        audit_retention_days: int,
    ) -> None:
        self._repositories = repositories
        self._storage = storage
        self._backup_directory = backup_directory
        self._transient_retention_days = transient_retention_days
        self._audit_retention_days = audit_retention_days

    def status(self) -> dict[str, Any]:
        return self._repositories.operational_status()

    async def health(self, bot: Any) -> HealthReport:
        status = self.status()
        telegram_ok = False
        telegram_error: str | None = None
        try:
            await bot.get_me()
            telegram_ok = True
        except Exception as error:
            telegram_error = type(error).__name__
        status["telegram_error"] = telegram_error
        return HealthReport(
            database_ok=status["database"] == "ok" and status["foreign_keys"],
            telegram_ok=telegram_ok,
            details=status,
        )

    def maintain(self) -> dict[str, int]:
        return self._repositories.run_maintenance(
            transient_retention_days=self._transient_retention_days,
            audit_retention_days=self._audit_retention_days,
        )

    def backup(self) -> Path:
        return self._storage.backup(self._backup_directory)
