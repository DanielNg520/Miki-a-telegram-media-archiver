# Burner Layer (optional user-account adjunct)

An **optional** enhancement that runs a Telegram *user account* ("burner") alongside the Miki
bot to do things the Bot API cannot: read chat history, read groups Miki isn't in, and use
Telegram's own storage to offload the droplet. The whole layer is **additive and
capability-gated** — with no burner configured, Miki behaves exactly as it does without it.

For core internals see [architecture.md](architecture.md); for hosting see
[deployment.md](deployment.md).

## What it does

| Capability | Command | Purpose |
|---|---|---|
| **Backup offload** | `miki-burner backup` | Encrypted, compressed DB snapshot pushed into the archive group so the index survives loss of the droplet or the burner account. |
| **History backfill** | `miki-ops backfill` / `miki-burner backfill` | Reads archive topic history (all active topics by default) and feeds the existing indexer, making pre-bot posts searchable/retrievable. See [History backfill](#history-backfill). |
| **Forward-bridge** | `miki-burner bridge-once` | Forwards new media from a group Miki can't join into a Miki source topic, where the normal pipeline archives it. |

Everything runs as **on-demand CLI**, invoked from cron/systemd — not an always-on process. That
keeps the droplet footprint near zero when idle (an always-on Telethon poller would cost
~60–100 MB resident). A persistent `miki-burner run` loop exists but is deliberately not the
deployed model.

## Design principles

1. **Core is untouchable.** The sorting/indexing/retrieval pipeline works byte-for-byte with or
   without the burner. Every burner feature self-disables when the burner is absent.
2. **Assume the burner will be banned.** Userbots violate Telegram ToS; read-only/low-volume use
   is tolerated but the account *will* eventually die. Nothing critical may depend on it being
   alive — backups live in a multi-member group and restore via any user account.
3. **Reuse, don't fork.** Backfill feeds the existing `MessageIndexer`; commands reuse the
   `jobs`-style claim/worker pattern, the `audit_events` table, and the shared SQLite DB as the
   IPC channel. New code is a thin adapter.
4. **No inbound network surface.** The droplet only makes *outbound* connections to Telegram.
   There is no web server; control is the CLI (and optionally Telegram commands).
5. **Two processes, one DB.** Bot and burner never call each other — they communicate only through
   SQLite tables, so a burner crash/flood-wait/ban can never stall the core bot.

## Capability gate

A single helper (`BurnerCapability`) answers "is the burner available?" and gates every feature.
Available means **all** of: (1) `TELETHON_*` + `BURNER_*` config present; (2) the stored session
validates (authorized, not revoked); (3) a recent heartbeat (within 2× the poll interval). When
unavailable, each feature falls back to core behavior (e.g. the daily local-only backup) or is
simply absent. `/doctor` shows the burner line: `not configured`, `available (last seen Ns ago)`,
or `unavailable (reason)`.

## Configuration

All optional; absence disables the layer. Add to `.env`:

```env
BURNER_ENABLED=true
TELETHON_API_ID=1234567           # from https://my.telegram.org
TELETHON_API_HASH=...
TELETHON_SESSION=...              # minted by `miki-burner-login`, treat like BOT_TOKEN
BURNER_POLL_INTERVAL_SECONDS=30
BURNER_OPERATOR_USER_IDS=         # teammates allowed to run /burner (⊆/⊇ ADMIN_USER_IDS)

# Backup offload
BURNER_BACKUP_CHAT_ID=            # the archive group (already has members)
BURNER_BACKUP_THREAD_ID=         # optional topic to keep backups out of media topics
BURNER_BACKUP_AGE_RECIPIENT=age1... # age public key; private key stays OFF the droplet
BURNER_BACKUP_LOCAL_RETENTION=3
```

Install the extra on the droplet: `pip install '.[burner]'` (adds `telethon` and `pyrage`).

## Session bootstrap

Run **once**, locally over SSH by the account owner (never a teammate, never via Telegram —
Telegram invalidates login codes shared in chat):

```bash
miki-burner-login   # prompts for phone, code, 2FA; prints the StringSession once
```

The session string is a **full-account credential** — store it the way you store `BOT_TOKEN`
(secret storage), never log it, never render it in command output.

## Cron automation

```cron
30 3 * * *  cd /opt/miki && miki-burner backup       # encrypted offload to the group
0  4 * * *  cd /opt/miki && miki-burner backfill      # incremental sweep of every archive topic
*/5 * * * * cd /opt/miki && miki-burner bridge-once   # near-live forward-bridge
```

Each command exits after bounded work. Backfill and bridge use a stored checkpoint so repeated
runs do the minimum — backfill only reads messages newer than what's indexed; a bridge forwards
only messages newer than its last-forwarded id.

## History backfill

Reads archive-topic history with the burner account and feeds each media message into the *existing*
`MessageIndexer`, making pre-bot posts searchable/retrievable. Run it from either CLI — omit the
topic id to sweep **every active archive topic**, resolving the chat (`ARCHIVE_CHAT_ID`) and topic
ids from the `topics` table, so no ids need be passed:

```bash
miki-ops backfill                  # sweep all active archive topics once
miki-ops backfill <topic_id>       # one topic
miki-burner backfill [<topic_id>]  # same, from the burner CLI
miki-ops backfill --loop           # leave-it-running: small cycles + sleeps until caught up
```

**Loop mode** (`--loop`) mirrors the archiver's run→sleep→run cadence: each cycle indexes at most
`--cycle-limit` posts per topic, then sleeps a random `--sleep-min..--sleep-max` seconds, repeating
until every topic is caught up (then it stops on its own). One client/takeout session is held for
the whole loop; Ctrl-C (or SIGTERM) stops cleanly and the `min_id` checkpoint resumes the next run.

**Account safety** — the burner is a real user account, so ban risk is real; the crawl is built to
be gentle:

- **Read-only** — only `iter_messages`; it never sends or copies (delivery still happens via the bot).
- **Takeout session by default** — reads run inside Telegram's sanctioned data-export ("takeout")
  mode, the gentlest way to bulk-read history. Falls back to a throttled direct read if a takeout
  can't be started; `--no-takeout` forces the direct path.
- **Always bounded** — `--limit` (per-topic post cap, default 500) *and* `--max-minutes` (budget for
  the whole run, default 15), first hit wins; `0` disables either cap. A sweep shares one time budget
  across all topics, so a pass can never become an open-ended scan.
- **Throttled + jittered** — a base inter-batch delay plus `--jitter` random seconds keeps the
  request cadence from being robotically even.
- **Flood-wait aware, capped** — a flood-wait is slept off and the crawl resumes from the cursor; a
  wait longer than the cap stops the run cleanly (it is resumable) instead of sleeping for hours.
- **Incremental** — resumes from a per-topic `min_id` checkpoint, so re-running continues where it
  left off. A run that stops on limit/time/flood prints a "re-run to continue" hint.
- **One shared client** is reused across all topics in a sweep (reconnecting per topic is itself
  ban-prone).

A dedicated or secondary account is safest since a ban lands on whichever account holds the session,
but a bounded, takeout-based sweep of your own account is a reasonable choice.

## Telegram command plane (optional)

If a `miki-burner run` (or periodic `miki-burner once`) process is draining the queue, teammates
can drive the burner from Telegram, gated by `BURNER_OPERATOR_USER_IDS ∪ ADMIN_USER_IDS`:

- `/burner status` — heartbeat/capability summary (answered inline, always available).
- `/burner <kind>` — enqueues a command (`noop`, `backup_now`, `backfill`, `bridge_add`,
  `bridge_remove`). If the burner is unavailable it replies "unavailable" and does **not** queue
  (fail-fast). Results are posted back into the originating chat; every command writes an audit row.

Under the deployed cron model the CLI is the primary path; `/burner status` still works any time.

## Backup restore runbook

Backups are `age`-encrypted and gzip-compressed, uploaded to the archive group. Because the Bot
API cannot download files > 20 MB, **restore uses a user account, never the Miki bot** — and never
assumes the original burner is alive (any group member can do it):

1. From a user account in the archive group, download the desired `miki-<ts>.sqlite3.gz.age`.
2. Decrypt with the age *private* key (kept off the droplet):
   `age -d -i age-key.txt -o miki.sqlite3.gz miki-<ts>.sqlite3.gz.age`
3. Decompress: `gunzip miki.sqlite3.gz`
4. Install with verification (integrity + schema checks):
   `Storage.restore_backup(Path("miki.sqlite3"), Path("var/miki.sqlite3"))`
5. Restart Miki.

## Forward-bridge setup

For a group Miki can't join, forward its new media into a Miki source topic that already routes to
an archive topic:

```bash
miki-burner bridge-add <foreign_chat_id> <source_thread_id>   # register (checkpoint seeds on first poll)
miki-burner bridge-remove <foreign_chat_id>                   # deactivate
```

Ensure a `TOPIC_FORWARDING_JSON` pair maps `source_thread_id` to its archive topic — the burner
only forwards *into* the source topic; the normal pipeline copies it to the archive (stripping the
"forwarded from" header). The first `bridge-once` seeds the checkpoint to "now" and forwards
nothing, so **history is never bulk-forwarded** (mass-sending is the most ban-prone action). A
group with `noforwards` (restrict saving/forwarding) cannot be bridged — it is detected and the
bridge disabled with a reported reason. For history of an unreachable group, use `backfill`
(searchable, but deliverable only once Miki can copy from that chat).

## Constraints & caveats

- **Retrievability:** an indexed post is deliverable only for chats the Miki bot is a member of
  (delivery is `copy_message` by id). Archive-topic backfill satisfies this; backfilling a chat the
  bot isn't in makes posts *searchable* but not *deliverable*.
- **Provenance:** backfilled rows are stamped `source_kind='backfill'` so coverage can be audited
  and a bad run selectively purged. `/reindex` (re-tokenises existing rows) and backfill (adds rows)
  are complementary.
- **Upload limits:** per-file 2 GB (4 GB Premium). The compressed DB is projected well under this;
  chunking is a future step if ever exceeded.
- **Forward-into-topic (validate on the droplet):** targeting a forum topic uses the raw
  `messages.ForwardMessagesRequest(..., top_msg_id=...)` (the high-level Telethon helper can't). All
  bridge logic (checkpointing, seeding, flood-wait, `noforwards`) is covered by tests with fakes,
  but this single live MTProto call should be verified with a real session before relying on the
  bridge.

## Security

- The session string and the age private key are the sensitive credentials. Session lives in secret
  storage on the droplet; the age private key lives **off** the droplet (only the public recipient
  key is on it), so a compromised droplet cannot decrypt past backups.
- Backups contain the whole index (captions, tokens) — always encrypted before upload.
- Every burner command writes an `audit_events` row attributed to the caller (`/audit_log`).
