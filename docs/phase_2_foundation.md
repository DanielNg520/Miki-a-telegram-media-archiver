# Phase 2: Foundation

Phase 2 connects the Phase 1 behavior contract to durable implementation boundaries without
changing the current sorter behavior.

## Configuration

`Settings` remains the single configuration entry point. It now includes:

- SQLite database path
- Administrator user IDs
- Request-topic IDs
- Default and maximum retrieval limits
- Log level and output format

Comma-separated ID settings are normalized to integer sets. Request limits are validated together,
and all secrets remain environment-only.

## Logging

Application logs can be JSON or text. Every Telegram handler receives a correlation ID derived
from its update or message ID. Structured extra fields are supported, while known credential and
message-content field names are redacted.

The correlation context is reset after every update so concurrent updates cannot inherit another
request's identifier.

## Database

SQLite is the initial database because Miki is a single-bot service and does not need a separate
database server to gain transactions, indexes, and crash-safe local persistence.

Connections enable:

- Foreign keys
- Write-ahead logging
- A five-second busy timeout

The first migration creates:

- Stable topics
- Hashtag, keyword, and phrase mappings
- Indexed posts and normalized tokens
- Processed-update idempotency records
- Durable jobs
- Delivery lineage

Media bytes are never stored.

## Migration Policy

Applied migrations are recorded in `schema_migrations`. A migration version is immutable after
release. Later schema changes must add a new numbered migration instead of editing a migration that
may already exist in a deployed database.

SQLite schema migrations are forward-only. Operational rollback means restoring the pre-migration
database backup and running the previous application version. This is safer than attempting
destructive reverse SQL on live indexed data.

## Repository Boundary

Telegram handlers and future services must depend on repository protocols rather than issue SQL.
`SqliteRepositories` is the initial adapter. Phase 2 exposes the first required operations:

- Look up a topic by `chat_id + thread_id`
- Claim a Telegram update once
- Enqueue an idempotent durable job

Later phases will expand these interfaces in the job where each behavior is introduced.

## Test Infrastructure

Tests use:

- The project `.venv`
- `pytest`
- In-memory SQLite fixtures with real migrations
- Existing async Telegram mocks
- Temporary on-disk databases for storage lifecycle checks

No test reads the real `.env`, calls Telegram, or writes to the production database path.
