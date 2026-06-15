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

## Configure

Copy `.env.sample` to `.env` and fill in your real values.

```bash
cp .env.sample .env
```

Important IDs:

- `SOURCE_CHAT_ID`: main supergroup ID, usually a negative number like `-1001234567890`.
- `SOURCE_THREAD_ID`: forum topic ID that Miki should watch.
- `ARCHIVE_CHAT_ID`: archive supergroup ID.
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
- `LOG_LEVEL` and `LOG_FORMAT`: application logging controls (`json` is recommended).
- `SORT_DRY_RUN`: resolve and record routes without copying media.
- `ROUTES_JSON`: routing rules.

`COLLECTOR_*` and `ROUTES_JSON` are legacy-compatible settings. The active sorter uses registered
database mappings.

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

```bash
miki-sorter
```

Or:

```bash
python -m miki_sorter_bot.main
```

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

Then send `/show_ids` or `/where` inside the target topic. Do not run this listener at the same
time as `miki-sorter` with the same bot token because Telegram permits only one polling process.

## Command Reference (User Guide)

This is the complete list of every command Miki understands. Send commands as normal Telegram
messages inside the relevant chat/topic.

### Permission levels

| Level | Who | How granted |
| --- | --- | --- |
| **Admin** | Full control | Listed in `ADMIN_USER_IDS` (requires restart), **or** added live with `/manager_add` (no restart). |
| **Manager** | Same powers as Admin | Added with `/manager_add <user_id>`. Universal (all chats), effective immediately. |
| **Anyone** | Public/requesters | No special grant; subject to per-command chat/topic rules. |

> Since `/manager_add` grants full admin-equivalent access, "Admin" and "Manager" are
> interchangeable below. The only difference is how they were added.

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
| `/topic_register <name>` | Manager | `/topic_register Japan` | Run **inside** the destination forum topic. Miki must be an admin of the forum. Names must be unique. |
| `/topic_list` | Manager | `/topic_list` | Lists all active registered topics as `thread_id: name`. |

Topic open/close/rename in Telegram is tracked automatically — closing a topic deactivates it
(routing/indexing pause), reopening reactivates it, and renaming updates the stored name.

### Keyword routes

Keywords route a post to a topic when a matching word/phrase appears in its caption. A single word
is a `keyword`; a `"quoted phrase"` is a `phrase`.

| Command | Who | Usage |
| --- | --- | --- |
| `/keyword_add <topic_id> <keyword or "phrase">` | Manager | `/keyword_add 7 TOKYO` or `/keyword_add 7 "Mount Fuji"` |
| `/keyword_remove <topic_id> <value>` | Manager | `/keyword_remove 7 TOKYO` |
| `/keyword_replace <topic_id> <value>` | Manager | Moves an existing keyword/phrase to a different topic. |
| `/keyword_list [topic_id]` | Manager | Lists all keyword **and** phrase routes; optional topic filter. |
| `/keyword_find <keyword or "phrase">` | Manager | Shows which topic a keyword/phrase routes to. |

### Hashtag routes

Hashtag routes match `#tags` in a post's caption.

| Command | Who | Usage |
| --- | --- | --- |
| `/hashtag_add <topic_id> <hashtag>` | Manager | `/hashtag_add 7 travel` |
| `/hashtag_remove <topic_id> <hashtag>` | Manager | `/hashtag_remove 7 travel` |
| `/hashtag_replace <topic_id> <hashtag>` | Manager | Moves a hashtag route to a different topic. |
| `/hashtag_list [topic_id]` | Manager | Lists hashtag routes; optional topic filter. |

### Routing diagnostics

| Command | Who | Usage | What it does |
| --- | --- | --- | --- |
| `/route_explain <caption text>` | Manager | `/route_explain Visiting #TOKYO today` | Dry-run: shows which topic the text would route to, the reason, or reports `unmatched` / a `conflict` between topics. |

### Access control

| Command | Who | Usage | What it does |
| --- | --- | --- | --- |
| `/manager_add <user_id>` | Admin/Manager | `/manager_add 123456789` | Grants full, universal, restart-free access. |
| `/manager_remove <user_id>` | Admin/Manager | `/manager_remove 123456789` | Revokes the manager from every chat. |

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
| `/request_cancel <job_id>` | Admin/Manager | `/request_cancel 42` | Cancels an in-progress retrieval job. |

### Search index maintenance

| Command | Who | Usage | What it does |
| --- | --- | --- | --- |
| `/reindex [batch_size]` | Admin/Manager | `/reindex 200` | Re-extracts search tokens for stored posts (batch 1–1000, default 100). Reports how many were processed. |

### Operations & monitoring

| Command | Who | Usage | What it does |
| --- | --- | --- | --- |
| `/health` | Admin/Manager | `/health` | Reports overall health plus database and Telegram connectivity. |
| `/status` | Admin/Manager | `/status` | Operational snapshot: posts, dead letters, job counts, retries/throttles, average delivery time. |
| `/maintenance` | Admin/Manager | `/maintenance` | Prunes expired transient records and old audit events per retention settings. |
| `/backup` | Admin/Manager | `/backup` | Creates an on-demand verified database backup (in addition to the daily auto-backup). |
| `/dead_letters` | Admin/Manager | `/dead_letters` | Lists unresolved terminal failures (id, operation, error category, job). |
| `/dead_letter_retry <id>` | Admin/Manager | `/dead_letter_retry 5` | Requeues a single dead-lettered operation. |
| `/audit_log [limit]` | Admin/Manager | `/audit_log 50` | Shows recent audit events (limit 1–100, default 20). |

## Topic and Route Management

Set `ADMIN_USER_IDS`, restart Miki, and run `/topic_register <name>` inside each destination
topic. Miki must be an administrator in that forum. Use `/topic_list` to inspect the registry.

Route commands:

```text
/hashtag_add <topic_id> <hashtag>
/hashtag_replace <topic_id> <hashtag>
/hashtag_remove <topic_id> <hashtag>
/hashtag_list [topic_id]

/keyword_add <topic_id> <keyword or quoted phrase>
/keyword_replace <topic_id> <keyword or quoted phrase>
/keyword_remove <topic_id> <keyword or quoted phrase>
/keyword_list [topic_id]
/keyword_find <keyword or quoted phrase>
```

An admin or existing manager can grant full management access with
`/manager_add <user_id>` and revoke it with `/manager_remove <user_id>`. Managers
added this way are **universal** — they have the same powers as users in
`ADMIN_USER_IDS`, work in every chat, can themselves delegate, and the grant takes
effect **immediately with no restart** (it is stored in the database and checked
live). By contrast, editing `ADMIN_USER_IDS` in `.env` is only read at startup and
**does** require a restart, so `/manager_add` is the preferred way to add people.

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
