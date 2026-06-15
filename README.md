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

Configured administrators can delegate route management with `/manager_add <user_id>` and revoke
it with `/manager_remove <user_id>`.

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
