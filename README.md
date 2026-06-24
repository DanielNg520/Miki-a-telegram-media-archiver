# miki_a_friendly_sorter_bot

Miki is a Telegram sorter bot for forum-style supergroups. It watches one source topic and
processes only messages containing media. It checks keywords from the message text or caption
against the Data Collector database, then copies confirmed matches into the configured topic.

## What Telegram Setup Is Required

- The source and archive chats must be Telegram supergroups.
- The destination "subfolders" must be forum topics.
- The bot must be admin, or at least have permission to read messages and send messages in both groups.
- If the source group has privacy mode enabled for bots, disable it with BotFather or only send messages that mention/command the bot.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

Use a regular install for the runnable bot. On recent macOS/Python versions, editable
installs inside a hidden `.venv` can leave console commands unable to import the package.
Re-run `pip install . --force-reinstall` after code changes.

For development and the complete local verification matrix:

```bash
pip install ".[dev]"
make verify
make audit  # online dependency vulnerability lookup
```

`make runtime-check` runs diagnostics against the configured live `.env` and database; it is kept
separate from the hermetic build/test matrix.

## Configure

Copy `.env.sample` to `.env` and fill in your real values.

```bash
cp .env.sample .env
```

Important IDs:

- `SOURCE_CHAT_ID`: main supergroup ID, usually a negative number like `-1001234567890`.
- `SOURCE_THREAD_ID`: forum topic ID that Miki should watch.
- `ARCHIVE_CHAT_ID`: archive supergroup ID.
- `TOPIC_FORWARDING_JSON`: optional source-topic → archive-topic pairs. Every supported attachment
  in a listed source topic is copied to its registered destination without requiring a hashtag,
  keyword, phrase, or caption. Source topics must be unique; multiple sources may share a destination.
- `REQUEST_CHAT_ID`: supergroup where retrieval requests are submitted and results are copied.
  Leave blank to use `ARCHIVE_CHAT_ID`.
- `COLLECTOR_URL`: Data Collector API URL.
- `COLLECTOR_API_KEY`: Miki's Data Collector API key.
- `COLLECTOR_DATABASE`: database name to query, such as `gvdb`.
- `DATABASE_PATH`: local SQLite database used for Miki's durable state and search index.
- `ADMIN_USER_IDS`: comma-separated Telegram user IDs allowed to manage Miki.
- `REQUEST_TOPIC_IDS`: comma-separated topic IDs within `REQUEST_CHAT_ID` where retrieval requests
  are accepted.
- `DEFAULT_REQUEST_LIMIT` and `MAX_REQUEST_LIMIT`: bounds for future retrieval jobs.
- `LOG_LEVEL` and `LOG_FORMAT`: application logging controls. Use `LOG_FORMAT=console` for a
  readable local terminal, or `LOG_FORMAT=json` for structured hosted logs.
- `SORT_DRY_RUN`: resolve and record routes without copying media.
- `JOB_RECOVERY_INTERVAL_SECONDS` and `JOB_RECOVERY_BATCH_SIZE`: control the bounded worker that
  atomically resumes interrupted sorting/retrieval jobs and operator-requeued dead letters.
- `TELEGRAM_STARTUP_CHECKIN_ENABLED`: sends admins or `TELEGRAM_NOTIFICATION_CHAT_IDS` a startup
  `/doctor` summary.
- `HEALTH_SERVER_ENABLED`: starts optional `/healthz` and `/metrics` endpoints for polling/VPS
  deployments.
- `ERROR_REPORTING_DSN`: enables optional Sentry-style external exception reporting if
  `sentry-sdk` is installed.
- `ROUTES_JSON`: routing rules.

`COLLECTOR_*` and `ROUTES_JSON` are legacy-compatible settings. The active sorter uses registered
database mappings.

Example direct forwarding (all topic IDs are within `SOURCE_CHAT_ID` and `ARCHIVE_CHAT_ID`):

```json
[
  {"source_thread_id": 101, "destination_thread_id": 901},
  {"source_thread_id": 102, "destination_thread_id": 902},
  {"source_thread_id": 103, "destination_thread_id": 902}
]
```

The bot validates configuration before connecting to Telegram. IDs and credentials cannot
be blank; every route must have keywords; route names and destination thread IDs must be
unique.

Example route:

```json
[
  {
    "name": "Japan",
    "thread_id": 111,
    "keywords": ["cr", "gona", "fc"]
  }
]
```

## Run

Miki supports two runtime modes:

- `RUN_MODE=polling` for an always-on machine or VPS. This is the default and simplest mode.
- `RUN_MODE=webhook` for HTTPS web app hosts such as Koyeb or Render. Telegram pushes each update
  to Miki's public URL instead of Miki continuously polling Telegram.

```bash
miki-sorter
```

Or:

```bash
python -m miki_sorter_bot.main
```

For webhook mode, set:

```env
RUN_MODE=webhook
WEBHOOK_URL=https://your-app.example.com/telegram/webhook
WEBHOOK_LISTEN=0.0.0.0
WEBHOOK_PORT=8080
WEBHOOK_PATH=/telegram/webhook
```

`WEBHOOK_URL` must be the public HTTPS URL that reaches this process. `WEBHOOK_PORT` should match
the port expected by your host. Only one Miki instance should run for a bot token, regardless of
whether it uses polling or webhook mode.

While Miki is running, an administrator can identify the current chat and forum topic by
sending either command inside that topic:

```text
/show_ids
/where
```

Miki replies with the chat ID, chat type and name, topic ID, message ID, and caller's user ID.
Only users listed in `ADMIN_USER_IDS` can use these commands.

For initial setup, before the full bot configuration is valid, run the standalone listener:

```bash
miki-show-ids
```

Then send `/show_ids` or `/where` inside the target topic. Miki enforces one local process per bot
token with an operating-system lock: starting another sorter or this listener with the same token
exits immediately and reports the PID holding the lock. Telegram permits only one polling process;
deployments on different hosts must still use distinct tokens or ensure only one host is active.

To run local deployment checks without inspecting SQLite manually:

```bash
miki-doctor
```

It verifies configuration, database migrations, archive topic registration, route mappings, runtime
mode, and outstanding operational warnings. It exits non-zero when required archive setup is missing.
The same check is available to Telegram admins with `/doctor`.

For a full local verification pass before deploying:

```bash
make verify
```

That runs the test suite, bytecode import compilation, dependency consistency checks, and
`miki-doctor`.

For Render/Koyeb webhook hosting, see `docs/hosted_webhook_deployment.md`.

## Command Reference (User Guide)

This is the complete list of every command Miki understands. Send commands as normal Telegram
messages inside the relevant chat/topic.

### Permission levels

| Level | Powers | How granted |
| --- | --- | --- |
| **Super admin** | Everything, including operationally critical commands (source topic, forwarding pairs, topic registration, backups, maintenance, reindex, granting admins). | Listed in `ADMIN_USER_IDS` in `.env`. File-based, so it can never be locked out by a runtime change. Adding/removing requires a restart. |
| **Admin** (limited) | Keyword & hashtag routes (add/remove/replace/list/find), routing diagnostics, and read-only views (lists, status, health, doctor). **Cannot** touch source topic, forwarding, backups, maintenance, reindex, or grant other admins. | Granted live by a super admin with `/manager_add <user_id>`. Universal (all chats), effective immediately, no restart. |
| **Anyone** | Public/requesters | No special grant; subject to per-command chat/topic rules. |

In the tables below, **Who** is the minimum tier required: a command marked **Admin** is also
available to super admins.

Most commands report a usage hint if you call them with missing or malformed arguments, so when in
doubt just send the bare command (e.g. `/keyword_add`) to see the expected syntax.

### Discovering IDs

| Command | Who | What it does |
| --- | --- | --- |
| `/show_ids` | Admin only | Replies with the current `chat_id`, chat type/name, `topic_id`, `message_id`, and your `user_id`. |
| `/where` | Admin only | Alias of `/show_ids`. |

Send either inside the topic you want to identify. For first-time setup (before config is valid),
run the standalone listener `miki-show-ids` instead — don't run it at the same time as `miki-sorter`
with the same token (Telegram allows only one polling process).

### Topic management

| Command | Who | Usage | Notes |
| --- | --- | --- | --- |
| `/topic_register <name>` | Super admin | `/topic_register Japan` | Run **inside** the destination forum topic. Miki must be an admin of the forum. Names must be unique. |
| `/topic_list` | Admin | `/topic_list` | Lists all active registered topics as `thread_id: name`. |

Topic open/close/rename in Telegram is tracked automatically — closing a topic deactivates it
(routing/indexing pause), reopening reactivates it, and renaming updates the stored name.

### Keyword routes

Keywords route a post to a topic when a matching word/phrase appears in its caption. A single word
is a `keyword`; a `"quoted phrase"` is a `phrase`.

| Command | Who | Usage |
| --- | --- | --- |
| `/keyword_add <topic_id> <keyword or "phrase"[, ...]>` | Admin | `/keyword_add 7 TOKYO, "Mount Fuji"` |
| `/keyword_remove <topic_id> <value>` | Admin | `/keyword_remove 7 TOKYO` |
| `/keyword_replace <topic_id> <value>` | Admin | Moves an existing keyword/phrase to a different topic. |
| `/keyword_list [topic_id]` | Admin | Lists all keyword **and** phrase routes; optional topic filter. |
| `/keyword_find <keyword or "phrase">` | Admin | Shows which topic a keyword/phrase routes to. |

### Hashtag routes

Hashtag routes match `#tags` in a post's caption.

| Command | Who | Usage |
| --- | --- | --- |
| `/hashtag_add <topic_id> <hashtag> [...]` | Admin | `/hashtag_add 7 travel #tokyo` or `/hashtag_add 7 travel, #tokyo` |
| `/hashtag_remove <topic_id> <hashtag>` | Admin | `/hashtag_remove 7 travel` |
| `/hashtag_replace <topic_id> <hashtag>` | Admin | Moves a hashtag route to a different topic. |
| `/hashtag_list [topic_id]` | Admin | Lists hashtag routes; optional topic filter. |

### Routing diagnostics

| Command | Who | Usage | What it does |
| --- | --- | --- | --- |
| `/route_explain <caption text>` | Admin | `/route_explain Visiting #TOKYO today` | Dry-run: shows which topic the text would route to, the reason, or reports `unmatched` / a `conflict` between topics. |

### Source topic & forwarding

These change which topic Miki listens to and where attachments are forwarded. They are stored in
the database and take effect **immediately, without a restart** — handy for changes you make
periodically. `SOURCE_THREAD_ID` and `TOPIC_FORWARDING_JSON` in `.env` are only the initial seed;
once set at runtime, the database is authoritative.

| Command | Who | Usage | What it does |
| --- | --- | --- | --- |
| `/source_show` | Admin | `/source_show` | Shows the topic Miki currently listens to and whether it's a runtime override or the `.env` default. |
| `/source_set <topic_id>` | Super admin | `/source_set 4242` | Switches the listening source topic. Effective immediately. |
| `/forward_list` | Admin | `/forward_list` | Lists all `source -> destination` forwarding pairs. |
| `/forward_add <src_topic_id> <dest_topic_id>` | Super admin | `/forward_add 5 9` | Forwards attachments from a source topic to a destination topic. Many sources may point at one destination; re-adding a source replaces its destination. |
| `/forward_remove <src_topic_id>` | Super admin | `/forward_remove 5` | Removes the forwarding pair for a source topic. |

### Access control

| Command | Who | Usage | What it does |
| --- | --- | --- | --- |
| `/manager_add <user_id>` | Super admin | `/manager_add 123456789` | Grants a limited admin (keywords/hashtags + diagnostics), universal across chats, restart-free. |
| `/manager_remove <user_id>` | Super admin | `/manager_remove 123456789` | Revokes a limited admin from every chat. |

### Retrieval (`#request`)

Not a slash command — post a message whose **first line** is `#request` in an allowed request
chat/topic (`REQUEST_CHAT_ID` + `REQUEST_TOPIC_IDS`). Miki searches its index and copies matching
media back into the topic where the request was posted.

```text
#request
topic: Japan
keywords: TOKYO, "Mount Fuji"
match: any
limit: 10
```

| Field | Required | Meaning |
| --- | --- | --- |
| `topic` | yes | Destination topic name or numeric thread ID to search. |
| `keywords` | yes | Comma-separated words/`"phrases"`; a leading `#` is allowed. |
| `match` | no | `all` (default) requires every keyword; `any` requires at least one. |
| `limit` | no | Max posts to copy (defaults to `DEFAULT_REQUEST_LIMIT`, capped by `MAX_REQUEST_LIMIT`). |

Bot requesters must also be listed in `REQUESTER_BOT_IDS`. Miki replies with a job ID and a summary
(matched/copied/unavailable/skipped/failed).

| Command | Who | Usage | What it does |
| --- | --- | --- | --- |
| `/request_cancel <job_id>` | Admin | `/request_cancel 42` | Cancels an in-progress retrieval job. |

### Search index maintenance

| Command | Who | Usage | What it does |
| --- | --- | --- | --- |
| `/reindex [batch_size]` | Super admin | `/reindex 200` | Re-extracts search tokens for stored posts (batch 1–1000, default 100). Reports how many were processed. |

### Operations & monitoring

| Command | Who | Usage | What it does |
| --- | --- | --- | --- |
| `/health` | Admin | `/health` | Reports overall health plus database and Telegram connectivity. |
| `/status` | Admin | `/status` | Operational snapshot: posts, dead letters, job counts, retries/throttles, average delivery time. |
| `/doctor` | Admin | `/doctor` | Human-readable configuration and connectivity diagnostics. |
| `/maintenance` | Super admin | `/maintenance` | Prunes expired transient records and old audit events per retention settings. |
| `/backup` | Super admin | `/backup` | Creates an on-demand verified database backup (in addition to the daily auto-backup). |
| `/dead_letters` | Super admin | `/dead_letters` | Lists unresolved terminal failures (id, operation, error category, job). |
| `/dead_letter_retry <id>` | Super admin | `/dead_letter_retry 5` | Requeues a single dead-lettered operation. |
| `/audit_log [limit]` | Super admin | `/audit_log 50` | Shows recent audit events (limit 1–100, default 20). |

## Topic and Route Management

Set `ADMIN_USER_IDS`, restart Miki, and run `/topic_register <name>` inside each destination
topic. Miki must be an administrator in that forum. Use `/topic_list` to inspect the registry.

Route commands:

```text
/hashtag_add <topic_id> <hashtag> [...]
/hashtag_replace <topic_id> <hashtag>
/hashtag_remove <topic_id> <hashtag>
/hashtag_list [topic_id]

/keyword_add <topic_id> <keyword or quoted phrase>[, ...]
/keyword_replace <topic_id> <keyword or quoted phrase>
/keyword_remove <topic_id> <keyword or quoted phrase>
/keyword_list [topic_id]
/keyword_find <keyword or quoted phrase>
```

A **super admin** (a user in `ADMIN_USER_IDS`) can grant a **limited admin** with
`/manager_add <user_id>` and revoke it with `/manager_remove <user_id>`. Limited
admins can manage keyword/hashtag routes and view diagnostics across every chat,
effective **immediately with no restart** (stored in the database and checked live),
but they **cannot** change the source topic or forwarding, run backups/maintenance/
reindex, register topics, or grant other admins — those stay super-admin-only.
Editing `ADMIN_USER_IDS` in `.env` is only read at startup and **does** require a
restart; that roster is deliberately file-based so it can never be locked out by a
runtime change.

The source topic (`/source_set`) and forwarding pairs (`/forward_add` /
`/forward_remove`) are likewise stored in the database and applied live — change them
from Telegram without restarting. The `.env` values `SOURCE_THREAD_ID` and
`TOPIC_FORWARDING_JSON` seed the initial configuration on first run only.

## Search Index

Miki indexes media in active registered archive topics without downloading media files. The index
contains Telegram message references, album identity, compact sender/source metadata, a short
caption preview, and normalized hashtags, names, codes, configured keywords, and phrases.

Edited captions replace stale tokens automatically. Administrators can rebuild older extractor
versions in bounded batches with `/reindex [batch_size]`.

## Durable Sorting

Configured hashtags take priority over keywords and phrases. Conflicting destination topics are
recorded without copying. Every delivery is persisted before Telegram is called, and repeated
updates reuse the existing delivery record.

Use `/route_explain <caption text>` to inspect a route decision. Set `SORT_DRY_RUN=true` to test
matching without sending media.

## Retrieval

Set `REQUEST_CHAT_ID` to the request supergroup and `REQUEST_TOPIC_IDS` to the forum topics
allowed to receive requests. Human users may
submit:

```text
#request
topic: Japan
keywords: TOKYO, "Mount Fuji"
match: any
limit: 10
```

Bot requesters must also be listed in `REQUESTER_BOT_IDS`. Results are copied into the topic where
the request was posted. Administrators can cancel active work with `/request_cancel <job_id>`.

## Reliability

Telegram copies use shared rate limiting and bounded retries. `retry_after` responses are honored;
permanent failures are not repeatedly retried. Interrupted running jobs return to pending on the
next startup.

Inspect terminal failures with `/dead_letters` and requeue one with
`/dead_letter_retry <dead_letter_id>`. Indexed messages Telegram can no longer copy are marked
unavailable and excluded from later searches.

## Program Integrations

Configure signed clients with `INTEGRATION_CLIENTS_JSON`. Contract version 1 supports scoped route
previews, library searches, and audit reads with HMAC-SHA256 signatures, timestamp/nonce replay
protection, and per-client quotas.

Phase 8 intentionally opens no network listener. See `docs/phase_8_interoperability.md` for the
transport-neutral contract. Administrators can inspect recent events with `/audit_log [limit]`.

## Operations

Administrators can use `/health`, `/status`, `/maintenance`, and `/backup`.

Local terminal operations are also available through `miki-ops`, modeled after the Archiver Suite
ops console:

```bash
miki-ops health
miki-ops watch --interval 3
miki-ops status
miki-ops doctor
miki-ops backup
miki-ops maintenance
miki-ops logrotate
miki-ops install
miki-ops load
miki-ops restart
```

A **daily automatic backup** runs in-process via the bot's job queue (no external
cron needed). Each run takes a verified online SQLite snapshot (consistent under
WAL, integrity-checked) into `BACKUP_DIRECTORY`, then prunes all but the
`BACKUP_RETENTION_COUNT` most recent backups. Configure it with
`BACKUP_DAILY_ENABLED`, `BACKUP_TIME` (24-hour `HH:MM`, UTC), and
`BACKUP_RETENTION_COUNT`. A scheduled backup that fails is logged and counted in
the `database_backup_failures` metric without interrupting the bot.

Operational counters include Telegram retries and throttling, duplicate suppression,
integration replay and quota rejection, and retrieval delivery totals. Review
`docs/deployment.md`, `docs/phase_9_operations.md`, and `docs/release_readiness.md`
before production rollout.

## Behavior

Miki ignores messages outside `SOURCE_THREAD_ID`, messages without media, and messages without
text or a caption. It resolves registered hashtags, keywords, and phrases, persists the intended
delivery, and copies the original message without downloading or re-uploading media. Messages
without a match are left untouched; conflicting destinations are recorded without copying.
