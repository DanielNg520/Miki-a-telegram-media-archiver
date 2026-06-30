# Architecture & Design Reference

How Miki works internally and the design decisions behind it. For setup and commands see the
[README](../README.md); for hosting see [deployment.md](deployment.md).

## Overview

Miki watches forum-topic supergroups, copies media into destination topics by route, indexes a
compact searchable library (never the media bytes), and serves `#request` retrieval back into the
requesting topic. It is a single-process service backed by SQLite, designed to be crash-safe and
idempotent rather than horizontally scaled.

## Telegram constraints that shape the design

- A forum destination is the stable pair `chat_id + message_thread_id`; topic *names* can change,
  so routing never depends on a name alone.
- `copyMessage`/`copyMessages` copy into a topic without downloading or re-uploading media.
- Album members share a `media_group_id` but arrive as separate updates — they must be buffered to
  act at album level.
- There is no reliable deleted-message update for supergroups; a failed future copy is the signal
  that an indexed source is gone.
- Updates can be replayed around failures, so all processing must be idempotent.
- Rate limits are dynamic; bulk output goes through a rate-limited queue that honors `retry_after`.
- Message IDs are per-chat, and a `file_id` is per-bot — neither is a global library key.

## Data model (SQLite)

Media bytes are never stored. Connections enable foreign keys, WAL, and a busy timeout. The schema
covers: registered topics, hashtag/keyword/phrase mappings, indexed posts and normalized tokens,
processed-update idempotency records, durable jobs, and delivery lineage. Migrations are
forward-only and immutable once released; rollback means restoring a backup and running the prior
version. Telegram handlers depend on repository protocols (`SqliteRepositories` is the adapter),
not raw SQL.

## Sorting

A message is eligible when it arrives in `SOURCE_CHAT_ID`, in the watched source topic (or a topic
with a direct forwarding pair), contains supported media, and was not authored by Miki. Miki's own
copies are rejected at the input boundary to prevent loops; other bots stay eligible.

**Precedence:**

1. **Direct topic forwarding** (`source_thread_id → destination_thread_id`) wins over all text
   rules and ignores captions entirely. Many sources may share one destination.
2. **Hashtag** routes beat keyword/phrase routes.
3. Several rules pointing at one topic collapse to a single copy.
4. Rules pointing at *different* topics are a **conflict**: nothing is copied, the conflict is
   recorded and reported. Configuration order never silently decides a conflict.
5. Unknown hashtags do not block an otherwise valid keyword/phrase match.

Matching is Unicode-aware and case-insensitive: hashtags match the whole tag; keywords match whole
alphanumeric terms on punctuation/edge boundaries (`abc` matches `ABC` and `(ABC)-`, not `ABC123`);
phrases match consecutive whitespace-separated words.

**Hashtag look-back:** if media lands with no route (no caption, or a caption matching nothing —
e.g. forwarded media still carrying an unrelated caption) and a hashtag-only message follows, Miki
routes that recent media too. Because a bot cannot read history, this only covers media Miki already
received: a small per-topic in-memory buffer, both time-bounded (`LOOKBACK_TTL_SECONDS`) and
count-bounded (`LOOKBACK_CAPACITY`), that self-cleans and starts empty after a restart.

**Durable delivery:** before calling Telegram, Miki creates/loads an idempotent sorting job and a
pending delivery record, then skips Telegram if that delivery is already sent or skipped. After the
copy it stores the destination message ID, completes the job, and hands the copy to the indexer. The
delivery-lineage key (source message + destination topic) makes replayed updates unable to create a
second copy. `SORT_DRY_RUN=true` records skipped deliveries without calling Telegram.

## Indexing

Miki indexes media only in active registered topics of `ARCHIVE_CHAT_ID`, storing identifiers,
source metadata, media type, `media_group_id`, a logical post key, a ≤500-char caption preview, and
normalized tokens. Physical identity is `source_chat_id + source_message_id`; reprocessing upserts.

Album members keep separate message IDs (so each can be copied later) but share one logical post
key derived from chat + `media_group_id`, letting retrieval treat the album as one result while
preserving member order.

**Extractor (version 1)** retains hashtags, ALL-CAPS identifiers (≥2 letters), mixed letter-number
codes (`RX7`, `A320`, `FC-2`), capitalized words not explained by sentence position, and exact
configured keywords/phrases. Values are case-folded and deduplicated; the extractor is deterministic
(no AI). Each post records its `extractor_version`; `/reindex [batch_size]` rebuilds older versions
in bounded batches (1–1000) from the stored preview. Edited captions atomically replace the token
set. Posts created by Miki are marked `miki_copy`, other bots `external_bot`, for loop prevention
without excluding them from search.

## Retrieval

A `#request` message (first line `#request`) is accepted only when the chat is the effective request
chat and the thread is an effective request topic (both runtime-configurable — see README). Bot
requesters must also be in `REQUESTER_BOT_IDS`. Request form:

```text
#request
topic: <archive topic ID or unique registered name>
keywords: <token or "quoted phrase">[, ...]
match: all | any        # optional, default all
limit: <n>              # optional, default DEFAULT_REQUEST_LIMIT, capped by MAX_REQUEST_LIMIT
```

`topic` and `keywords` are required; unknown/duplicate/malformed fields are rejected without
creating a job. Hashtag searches may include or omit `#`. `all` requires every term, `any` at least
one; tokens are aggregated across album members. Results are newest-first and `limit` counts logical
posts, not album members.

Each request is an idempotent job keyed by request chat + message ID; each result member has a
durable retrieval-item record keyed by job + post. Available members are delivered **as a batched
media group (album)** via `copyMessages` in ascending message order, split into chunks of 10 (the
Telegram album cap); single results use a plain copy. Successful members are not re-copied on
resume; failed members retry without replaying successful ones. Admins can stop a job with
`/request_cancel <job_id>`. The reply summarizes matched / copied (with album count) / unavailable /
skipped / failed / cancellation.

## Reliability & recovery

Sorting and retrieval share one rate-limited delivery executor (one process-wide output limit).
Failures are classified as `rate_limit` (retry per server `retry_after`), `transient`
(timeout/network/OS — bounded exponential backoff with jitter), `permission`, `invalid_request`,
`unavailable_source`, or `unexpected` (permanent, attempted once). Permanent Telegram errors are
checked before broad network base classes because PTB models `BadRequest` as a `NetworkError`.

**Unknown outcomes:** a send/copy timeout may mean Telegram accepted the media even though Miki
never got the response. Retrying or per-member fallback would risk duplicates, so Miki records an
`outcome_unknown` dead letter after one attempt and suppresses automatic replay; recovery requeues
the job idempotently. (A timed-out album batch is deferred whole rather than re-copied per member.)

**Dead letters** capture terminal non-source failures (job identity, operation, bounded payload,
category, message). `/dead_letters` lists them; `/dead_letter_retry <id>` returns the job to pending
and asks the recovery coordinator to resume immediately, with a periodic bounded sweep as backup.
Delivery/retrieval-item records keep providing duplicate protection; completing a recovered job
resolves its dead letters.

**Recovery:** at startup, jobs left `running` return to `pending`. A strategy-based coordinator
dispatches sorting/retrieval jobs from durable payloads; workers claim each with one atomic SQLite
transition so two paths can't perform the same delivery. Each pass is bounded by
`JOB_RECOVERY_BATCH_SIZE` and repeats every `JOB_RECOVERY_INTERVAL_SECONDS`. On graceful shutdown,
album timers are cancelled and routable buffered albums drained first; an interrupted shutdown is
resumed by startup recovery without replaying completed copies.

**Unavailable posts:** when Telegram reports a source can't be copied, Miki marks the post
unavailable, the retrieval item skipped, counts it in the summary, and excludes it from future
searches.

## Runtime configuration

Behavioural knobs live in a single typed settings registry that is the source of truth: each knows
how to parse/validate, render, and find its `.env` default. A read-through `LiveSettings` facade
resolves the effective value on every read, so `/set <key> <value>` takes effect immediately with no
restart and no cached staleness; `/config` lists them and `/reset <key>` reverts to the `.env`
default. A poisoned or out-of-range stored override is logged once, discarded, and falls back to the
default rather than disrupting delivery. Registered knobs include the album timers, the look-back
controls, `send_confirmation`, `sort_dry_run`, and the request chat/topics.

The source topic (`/source_set`) and forwarding pairs (`/forward_add` / `/forward_remove`) are
likewise stored in the database and applied live; the `.env` `SOURCE_THREAD_ID` and
`TOPIC_FORWARDING_JSON` only seed first-run configuration.

## Authorization

- **Super admin** — `ADMIN_USER_IDS` from `.env`. Full authority including operationally critical
  commands. File-based so it can never be locked out by a runtime change; editing it requires a
  restart.
- **Limited admin** (route manager) — granted live with `/manager_add <user_id>`, revoked with
  `/manager_remove`. May manage keyword/hashtag routes and view diagnostics across all chats,
  effective immediately, but cannot touch source topic/forwarding, backups, maintenance, reindex,
  topic registration, request topics, or admin grants.

Commands are gated by user tier, not by chat, so global commands (e.g. `/set`, `/config`, `/status`)
work in a DM with Miki; chat-scoped commands (`/keyword_add`, `/topic_register`, `/request_topic_*`)
act on the chat/topic they are sent in.

## Integrations & security

`IntegrationService` is a transport-neutral, versioned dispatcher — it opens **no network port**, so
a future HTTP/Unix-socket/plugin adapter can hand it raw request bytes plus auth metadata without
changing security or business rules.

Contract v1 requests carry `version`, caller `request_id`, `operation`, and `data` (≤64 KiB);
responses repeat them with `ok` and either `result` or a stable `error.code`/`error.message`.
Operations and scopes: `route.preview` (`submit`), `library.search` (`search`), `audit.list`
(`admin`). Clients come from `INTEGRATION_CLIENTS_JSON` (secret ≥16 chars, env-only). An adapter
supplies client ID, Unix timestamp, unique nonce, hex HMAC-SHA256, and exact raw bytes; canonical
signed bytes are `<timestamp>\n<nonce>\n<raw body>`. Timestamps must be within
`INTEGRATION_SIGNATURE_TTL` (default 300s), comparison is constant-time, and nonces are atomically
claimed against replay. Each client has an atomic fixed-window quota. Audit events (excluding
secrets, signatures, raw captions, and raw search terms) cover integration accept/deny, topic/route
changes, manager changes, sorting outcomes, retrieval lifecycle, and dead-letter retries; inspect
with `/audit_log [limit]`.

## Future database scaling

Miki supports `DATABASE_BACKEND=sqlite` with one instance per bot token. To scale beyond a single
host, add a Postgres repository behind the existing repository interface before enabling
`DATABASE_BACKEND=postgres`. Keeping the boundary repository-shaped is what makes that a contained
change.
