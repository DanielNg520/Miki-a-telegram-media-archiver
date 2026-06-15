from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, time
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from miki_sorter_bot.routing import Route


@dataclass(frozen=True, slots=True)
class IntegrationClient:
    client_id: str
    secret: str
    scopes: frozenset[str]
    requests_per_minute: int


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    bot_token: str = Field(min_length=1, alias="BOT_TOKEN")
    source_chat_id: int = Field(alias="SOURCE_CHAT_ID")
    source_thread_id: int = Field(alias="SOURCE_THREAD_ID")
    archive_chat_id: int = Field(alias="ARCHIVE_CHAT_ID")
    request_chat_id: int | None = Field(default=None, alias="REQUEST_CHAT_ID")
    collector_url: str = Field(default="http://127.0.0.1:8787", alias="COLLECTOR_URL")
    collector_api_key: str = Field(default="", alias="COLLECTOR_API_KEY")
    collector_database: str = Field(default="", alias="COLLECTOR_DATABASE")
    collector_timeout: float = Field(default=5.0, gt=0, alias="COLLECTOR_TIMEOUT")
    database_path: Path = Field(default=Path("var/miki.sqlite3"), alias="DATABASE_PATH")
    backup_directory: Path = Field(default=Path("var/backups"), alias="BACKUP_DIRECTORY")
    transient_retention_days: int = Field(
        default=30,
        ge=1,
        alias="TRANSIENT_RETENTION_DAYS",
    )
    audit_retention_days: int = Field(
        default=90,
        ge=1,
        alias="AUDIT_RETENTION_DAYS",
    )
    admin_user_ids: Annotated[frozenset[int], NoDecode] = Field(
        default_factory=frozenset,
        alias="ADMIN_USER_IDS",
    )
    request_topic_ids: Annotated[frozenset[int], NoDecode] = Field(
        default_factory=frozenset,
        alias="REQUEST_TOPIC_IDS",
    )
    requester_bot_ids: Annotated[frozenset[int], NoDecode] = Field(
        default_factory=frozenset,
        alias="REQUESTER_BOT_IDS",
    )
    default_request_limit: int = Field(default=20, gt=0, alias="DEFAULT_REQUEST_LIMIT")
    max_request_limit: int = Field(default=100, gt=0, alias="MAX_REQUEST_LIMIT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="json", alias="LOG_FORMAT")
    sort_dry_run: bool = Field(default=False, alias="SORT_DRY_RUN")
    telegram_retry_attempts: int = Field(default=3, ge=1, le=10, alias="TELEGRAM_RETRY_ATTEMPTS")
    telegram_retry_base_delay: float = Field(default=0.5, ge=0, alias="TELEGRAM_RETRY_BASE_DELAY")
    telegram_retry_max_delay: float = Field(default=8.0, ge=0, alias="TELEGRAM_RETRY_MAX_DELAY")
    telegram_messages_per_second: float = Field(
        default=10.0,
        gt=0,
        alias="TELEGRAM_MESSAGES_PER_SECOND",
    )
    integration_clients: tuple[IntegrationClient, ...] = Field(
        default_factory=tuple,
        alias="INTEGRATION_CLIENTS_JSON",
    )
    integration_signature_ttl: int = Field(
        default=300,
        ge=30,
        le=3600,
        alias="INTEGRATION_SIGNATURE_TTL",
    )
    send_confirmation: bool = Field(default=False, alias="SEND_CONFIRMATION")
    backup_daily_enabled: bool = Field(default=True, alias="BACKUP_DAILY_ENABLED")
    backup_time: str = Field(default="03:00", alias="BACKUP_TIME")
    backup_retention_count: int = Field(default=14, ge=1, alias="BACKUP_RETENTION_COUNT")
    routes: list[Route] = Field(default_factory=list, alias="ROUTES_JSON")

    @field_validator("routes", mode="before")
    @classmethod
    def parse_routes(cls, value: object) -> object:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return json.loads(value)
        return value

    @field_validator("integration_clients", mode="before")
    @classmethod
    def parse_integration_clients(cls, value: object) -> object:
        if value in (None, ""):
            return ()
        payload = json.loads(value) if isinstance(value, str) else value
        return tuple(
            IntegrationClient(
                client_id=str(item["client_id"]).strip(),
                secret=str(item["secret"]),
                scopes=frozenset(str(scope).strip() for scope in item.get("scopes", [])),
                requests_per_minute=int(item.get("requests_per_minute", 60)),
            )
            for item in payload
        )

    @field_validator("admin_user_ids", "request_topic_ids", "requester_bot_ids", mode="before")
    @classmethod
    def parse_integer_sets(cls, value: object) -> object:
        if value in (None, ""):
            return frozenset()
        if isinstance(value, str):
            return frozenset(int(item.strip()) for item in value.split(",") if item.strip())
        return value

    @field_validator("request_chat_id", mode="before")
    @classmethod
    def parse_optional_request_chat_id(cls, value: object) -> object:
        if value in (None, ""):
            return None
        return value

    @field_validator("backup_time")
    @classmethod
    def validate_backup_time(cls, value: str) -> str:
        normalized = value.strip()
        parts = normalized.split(":")
        try:
            hour, minute = (int(part) for part in parts)
        except ValueError as error:
            raise ValueError("BACKUP_TIME must be HH:MM in 24-hour UTC") from error
        if len(parts) != 2 or not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("BACKUP_TIME must be a valid 24-hour HH:MM time")
        return f"{hour:02d}:{minute:02d}"

    @property
    def effective_request_chat_id(self) -> int:
        return self.request_chat_id or self.archive_chat_id

    @property
    def backup_time_utc(self) -> time:
        hour, minute = (int(part) for part in self.backup_time.split(":"))
        return time(hour=hour, minute=minute, tzinfo=UTC)

    @field_validator("bot_token")
    @classmethod
    def reject_blank_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("database_path", "backup_directory")
    @classmethod
    def reject_blank_database_path(cls, value: Path) -> Path:
        if not str(value).strip():
            raise ValueError("must not be blank")
        return value.expanduser()

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"json", "text"}:
            raise ValueError("must be json or text")
        return normalized

    @model_validator(mode="after")
    def validate_routes(self) -> Settings:
        names = [route.name.casefold() for route in self.routes]
        thread_ids = [route.thread_id for route in self.routes]
        if len(names) != len(set(names)):
            raise ValueError("route names must be unique")
        if len(thread_ids) != len(set(thread_ids)):
            raise ValueError("route thread IDs must be unique")
        if any(not route.keywords for route in self.routes):
            raise ValueError("every route must contain at least one keyword")
        if self.default_request_limit > self.max_request_limit:
            raise ValueError("DEFAULT_REQUEST_LIMIT must not exceed MAX_REQUEST_LIMIT")
        if self.telegram_retry_base_delay > self.telegram_retry_max_delay:
            raise ValueError("TELEGRAM_RETRY_BASE_DELAY must not exceed TELEGRAM_RETRY_MAX_DELAY")
        client_ids = [client.client_id for client in self.integration_clients]
        if len(client_ids) != len(set(client_ids)):
            raise ValueError("integration client IDs must be unique")
        for client in self.integration_clients:
            if not client.client_id or len(client.secret) < 16:
                raise ValueError("integration clients require an ID and a secret of 16+ characters")
            if not client.scopes <= {"submit", "search", "admin"}:
                raise ValueError("integration scopes must be submit, search, or admin")
            if client.requests_per_minute < 1:
                raise ValueError("integration quota must be positive")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
