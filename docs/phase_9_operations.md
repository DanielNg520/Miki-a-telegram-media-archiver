# Phase 9: Operations

## Admin Commands

- `/health` checks SQLite integrity, foreign-key enforcement, and Telegram connectivity.
- `/status` reports library size, unavailable posts, queue states, dead letters, retries,
  throttling, and average Telegram delivery time.
- `/maintenance` prunes expired transient records and old audit events, then runs
  SQLite index optimization.
- `/backup` creates a timestamped SQLite backup and verifies its integrity.

## Configuration

```dotenv
DATABASE_PATH=var/miki.sqlite3
BACKUP_DIRECTORY=var/backups
TRANSIENT_RETENTION_DAYS=30
AUDIT_RETENTION_DAYS=90
```

Metrics are stored as compact integer counters. Queue depth and database counts are
calculated from the source tables, avoiding duplicated operational data.

The HTTP health worker opens an isolated read-only SQLite connection for each probe. This keeps
threaded `/healthz` and `/metrics` requests away from the event-loop delivery connection; provider
failures return a bounded HTTP 503 response instead of dropping the probe connection.

## Restore Drill

Stop Miki before restoring. Verify the selected backup, restore it to a temporary
path, verify the restored database, replace the primary database, and restart Miki.
The `Storage.restore_backup` procedure performs the temporary restore and both
integrity checks. Keep the previous primary database until startup health passes.
