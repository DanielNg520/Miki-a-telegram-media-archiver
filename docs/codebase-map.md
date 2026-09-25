# Codebase Map

A navigation aid: what each module does, the background workers, the SQLite schema, and the seams
where components meet. For behavior see the [README](../README.md); for design rationale see
[architecture.md](architecture.md); for hosting see [deployment.md](deployment.md); for the optional
user-account layer see [burner-layer.md](burner-layer.md).

## Modules (`miki_sorter_bot/`)

### Core pipeline
| Module | Responsibility |
|---|---|
| `main.py` | Composition root. Builds the PTB `Application`, wires handlers, schedules the repeating/daily jobs, and runs polling or webhook mode. |
| `sorting.py` | `SortingService` — eligibility, route resolution/precedence, album buffering + flush timers, durable idempotent delivery. |
| `routing.py` | `Route` value type and route matching primitives. |
| `indexing.py` | `MessageIndexer` (duck-typed message → indexed post + tokens), `IndexingService`, the deterministic token `extract_search_tokens`, and `/reindex`. |
| `lookback.py` | Short-lived per-topic buffer of recent uncaptioned media, claimable by a later hashtag-only message. |
| `retrieval.py` | `#request` parsing/validation and `RetrievalService` — search, batched album delivery, idempotent per-item records. |

### State & configuration
| Module | Responsibility |
|---|---|
| `storage.py` | `Storage` — connection lifecycle (WAL, FK, busy-timeout), online backup/restore + verification. |
| `repositories.py` | `SqliteRepositories` — the single SQL adapter behind repository protocols; all tables live here. |
| `migrations.py` | Forward-only, immutable migrations (currently 13). |
| `config.py` | Pydantic `Settings` — env parsing/validation, the source of truth for `.env` keys and derived properties. |
| `settings_registry.py` | Runtime-tunable knobs (`/config` `/set` `/reset`) with read-through `LiveSettings`, self-healing on poisoned overrides. |

### Reliability & operations
| Module | Responsibility |
|---|---|
| `recovery.py` | `JobRecoveryService` — strategy-based resume of interrupted sort/retrieve jobs. |
| `reliability.py` | Error classification, backoff, dead-letter helpers. |
| `webhook_supervisor.py` | Self-healing webhook registration (observe → detect drift → heal → confirm, with a circuit breaker). |
| `health_server.py` | Optional `/healthz` + `/metrics` over an isolated read-only connection. |
| `operations.py` | `OperationsService` — backup + maintenance orchestration. |
| `diagnostics.py` | `run_diagnostics` / `/doctor` / `miki-doctor` checks (includes the burner line). |
| `error_reporting.py` | Optional Sentry-style capture. |
| `logging_config.py` | Structured/console logging + correlation IDs. |
| `instance_lock.py` | One-process-per-token OS lock. |
| `integrations.py` | `IntegrationService` — transport-neutral, signed, versioned request dispatcher (no open port). |
| `ops.py` | `miki-ops` console: health/watch/status/doctor/backup/maintenance/logrotate, `bot`/`backfill` subcommands, and service verbs delegated to `service.py`. |
| `service.py` | Cross-platform service management — launchd LaunchAgent (macOS) or Startup-folder launcher + process control (Windows) behind one platform-dispatched API. |
| `bot_console.py` | Runs any Telegram admin command locally by driving the same handlers with a synthetic admin `Update`/`Context` (`miki-ops bot`). |
| `show_ids.py` | Standalone `miki-show-ids` setup listener. |

### Burner layer (optional, capability-gated; all Telethon/pyrage imports are lazy)
| Module | Responsibility |
|---|---|
| `burner.py` | `BurnerCapability` gate, heartbeat loop, command dispatch (`process_pending_commands`), and the `miki-burner` CLI (`backup`/`backfill`/`bridge-*`/`once`/`run`). |
| `burner_session.py` | `miki-burner-login` — one-time interactive `StringSession` bootstrap + `validate_session`. |
| `burner_backup.py` | Consistent backup → gzip → age-encrypt → upload; retention; restore runbook. |
| `burner_backfill.py` | Telethon→duck-type adapter + the history crawl into `MessageIndexer`: single-topic or all-topics sweep, read via a takeout session, bounded by count/time, jittered, flood-wait-capped. Forward mode is `min_id`-checkpointed; `--deep` walks each topic downward into pre-Miki history from a persisted floor cursor (`backfill_cursors`); `--loop` runs cycle→sleep→repeat until caught up. Shared runner behind `miki-ops backfill` and `miki-burner backfill`. |
| `burner_bridge.py` | Cron-polled forward-bridge: seed-then-forward with checkpoint, `noforwards` detection, flood-wait. |
| `burner_reporting.py` | `BurnerResultReporter` (runs in the bot) — reclaims stale running commands, reports finished ones back into chat. |

## Background workers

Everything the bot runs on a timer, plus the out-of-process burner:

| Worker | Where | Cadence | Self-healing |
|---|---|---|---|
| Album flush | `SortingService` (in-process timers) | per album debounce | drained on graceful shutdown; startup recovery resumes routable buffers; uploads pass through a one-at-a-time gate |
| Job recovery | `JobRecoveryService` via `job_queue` | `JOB_RECOVERY_INTERVAL_SECONDS` + at startup | running→pending, atomic claim prevents double-delivery; **also retries `failed` jobs whose delivery never landed** |
| Webhook supervisor | `WebhookSupervisor` via `job_queue` | `WEBHOOK_RECONCILE_INTERVAL_SECONDS` | re-registers on confirmed drift; circuit breaker stops ineffective heals |
| Daily backup | `_schedule_daily_backup` | `BACKUP_TIME` daily | verified snapshot; failures counted, never fatal |
| Sanity checks | `_schedule_sanity_checks` | `SANITY_CHECK_INTERVAL_MINUTES` | surfaces config/activity drift |
| Health probe | `HealthServer` | on request | isolated read-only connection; reports unhealthy only when confidently wedged |
| Burner result reporter | `BurnerResultReporter` via `job_queue` | `BURNER_POLL_INTERVAL_SECONDS` | **reclaims stale `running` commands** (time-based), reports terminal ones once |
| Burner CLI ops | `miki-burner` (cron/systemd, separate process) | on demand | checkpoint/`min_id` resume; flood-wait sleep-and-continue; idempotent upserts |

## Data model (SQLite)

Core: `topics`, `route_mappings`, `route_managers`, `posts` (+ `post_tokens`), `processed_updates`,
`jobs`, `deliveries`, `retrieval_items`, `dead_letters`, `integration_nonces`/`integration_usage`,
`audit_events`, `metric_counters`, `runtime_settings`, `forwarding_pairs`.

Burner (migrations 9–13): `burner_status` (single-row heartbeat), `burner_commands` (jobs-style
queue with `reported_at`), `burner_bridges` (foreign chat → source topic + checkpoint),
`backfill_cursors` (deep-backfill floor per chat/topic — lowest scanned id + done). Migration 11
rebuilt `posts` to add the `backfill` `source_kind` (children snapshotted/restored to preserve FK
integrity under the FK-on migration transaction).

## Album delivery: how a member can be lost, and what stops it

Three defects, all found from the same symptom ("only the first of the batch reached the archive"):

1. **Concurrent uploads after a restart.** Polling replays the whole backlog in one `getUpdates`
   (`TELEGRAM_DROP_PENDING_UPDATES=false`, which is correct — nothing should be dropped), so every
   buffered album's debounce expires in the same second. Firing them all at once saturated the link
   and returned every one as `outcome_unknown`. `SortingService._album_send_gate`
   (`_MAX_CONCURRENT_ALBUM_SENDS`) now allows one album upload at a time; a Telegram album is capped
   at 10 items, so serialising costs little.
2. **`failed` jobs were never retried.** An `outcome_unknown` group marks *every* member's job and
   delivery `failed` and dead-letters them, deliberately suppressing the per-member fallback.
   `list_pending_jobs` only sees `pending`, so those members were stranded permanently.
   `JobRecoveryService._retry_undelivered_failures` now re-drives them via
   `list_undelivered_failed_jobs` → `SortingService.resume_job` → `copy_message`. Safety: the query
   requires `destination_message_id IS NULL`, so a member Telegram actually accepted is never
   re-copied; `attempts` caps the retries and `exhaust_failed_job` charges an attempt even when the
   job raises before `claim_job`, so a hopeless job drops out instead of looping forever.
3. **The manual escape hatch was broken.** `dead_letter_retry` was missing from `bot_console.NEEDS_BOT`,
   so `miki-ops bot dead_letter_retry <id>` ran with `context.bot = None` and failed the job again.

Related prior fixes still in force: straggler members must not cancel an in-flight send
(`_delivering_albums`), and an orphaned uncaptioned member inherits its group's remembered decision.

## Seams (where components meet)

- **Bot ↔ burner: the SQLite DB is the only channel.** Two processes, separate connections, WAL +
  `busy_timeout`. The bot never imports Telethon (verified: core import graph is Telethon-free); the
  burner never touches the PTB runtime. A burner crash/ban cannot stall the bot.
- **Burner backfill → indexer → retrieval.** Backfill feeds the *same* `MessageIndexer.index()` with
  `source_kind='backfill'`; album keys (`str(grouped_id)`) match live posts, so retrieval treats
  backfilled and live albums identically. Delivery works only where the Miki bot can `copy_message`.
- **Burner bridge → sorting.** The bridge forwards foreign media into a Miki source topic; the burner
  account ≠ the bot, so sorting stays eligible (no loop), and a `TOPIC_FORWARDING_JSON` pair carries
  it to the archive where `copy_message` strips the forward header.
- **Burner commands ↔ reporter.** The burner claims/executes commands; the always-on bot's reporter
  reclaims stale `running` rows and delivers every terminal result exactly once (`reported_at`).
- **Backups ↔ restore.** Uploaded to the archive group (multi-member) and restored by *any* user
  account — never the bot (>20 MB Bot-API limit), never assuming the burner survives.

## Entry points (`pyproject.toml [project.scripts]`)

`miki-sorter` (bot) · `miki-doctor` (diagnostics) · `miki-ops` (ops console) · `miki-show-ids`
(setup listener) · `miki-burner` (on-demand burner ops) · `miki-burner-login` (session bootstrap).

## Verification

`make verify` = test · compile · deps · lint · format-check · typecheck · security · audit ·
package. The enforced gates are test / compile / deps / lint (`ruff check`) / typecheck / security /
audit / package; `ruff format --check` is not enforced (the codebase uses manual line-wrapping).
