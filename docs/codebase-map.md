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
| `collector.py` | Legacy Data Collector client (keyword confirmation). |

### State & configuration
| Module | Responsibility |
|---|---|
| `storage.py` | `Storage` — connection lifecycle (WAL, FK, busy-timeout), online backup/restore + verification. |
| `repositories.py` | `SqliteRepositories` — the single SQL adapter behind repository protocols; all tables live here. |
| `migrations.py` | Forward-only, immutable migrations (currently 12). |
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
| `ops.py` / `show_ids.py` | `miki-ops` console and the standalone `miki-show-ids` listener. |

### Burner layer (optional, capability-gated; all Telethon/pyrage imports are lazy)
| Module | Responsibility |
|---|---|
| `burner.py` | `BurnerCapability` gate, heartbeat loop, command dispatch (`process_pending_commands`), and the `miki-burner` CLI (`backup`/`backfill`/`bridge-*`/`once`/`run`). |
| `burner_session.py` | `miki-burner-login` — one-time interactive `StringSession` bootstrap + `validate_session`. |
| `burner_backup.py` | Consistent backup → gzip → age-encrypt → upload; retention; restore runbook. |
| `burner_backfill.py` | Telethon→duck-type adapter + bounded, flood-wait-aware, `min_id`-checkpointed history crawl into `MessageIndexer`. |
| `burner_bridge.py` | Cron-polled forward-bridge: seed-then-forward with checkpoint, `noforwards` detection, flood-wait. |
| `burner_reporting.py` | `BurnerResultReporter` (runs in the bot) — reclaims stale running commands, reports finished ones back into chat. |

## Background workers

Everything the bot runs on a timer, plus the out-of-process burner:

| Worker | Where | Cadence | Self-healing |
|---|---|---|---|
| Album flush | `SortingService` (in-process timers) | per album debounce | drained on graceful shutdown; startup recovery resumes routable buffers |
| Job recovery | `JobRecoveryService` via `job_queue` | `JOB_RECOVERY_INTERVAL_SECONDS` + at startup | running→pending, atomic claim prevents double-delivery |
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

Burner (migrations 9–12): `burner_status` (single-row heartbeat), `burner_commands` (jobs-style
queue with `reported_at`), `burner_bridges` (foreign chat → source topic + checkpoint). Migration 11
rebuilt `posts` to add the `backfill` `source_kind` (children snapshotted/restored to preserve FK
integrity under the FK-on migration transaction).

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
