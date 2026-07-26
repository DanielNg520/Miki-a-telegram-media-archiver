# Offline Migration & `miki-backfill` Plan

Working note capturing the decisions from this session: taking Miki offline onto a
Windows PC, and the design + phased implementation plan for a new standalone
`miki-backfill` mechanism. Companion to [docs/burner-layer.md](docs/burner-layer.md)
and [docs/deployment.md](docs/deployment.md).

Status legend: ✅ done · ⚠️ needs elevation / manual · ⬜ not started · 🔒 decided (not built)

---

## 1. Session context

The user is moving Miki from a DigitalOcean droplet (webhook) to a home Windows PC
(AMD Ryzen 7 255, 8C/16T, 27.8 GB RAM, ~310 GB free on C:, Python 3.13.14) to run
24/7, and wants the burner user-account layer to act as a **backup + retrieve
manager**, including building an index of pre-Miki archive history over several days
without getting the account flagged.

---

## 2. What we did / decided this session

### Applied to this machine
- ✅ Disabled sleep/standby on AC — `powercfg /change standby-timeout-ac 0` (verified `0x0`).
- ✅ Disabled hibernate idle on AC — `hibernate-timeout-ac 0` (verified `0x0`).
- ✅ Disabled disk spindown on AC — `disk-timeout-ac 0` (verified `0x0`) to protect the WAL DB.
- ✅ Confirmed via `powercfg /availablesleepstates` that this machine has **no sleep
  states at all** (S1/S2/S3/S0 low-power idle all unsupported by firmware) — the
  "PC falls asleep → bot dark" risk is structurally impossible here.

### Verified in the codebase (no code changed — `git status` clean)
- ✅ `RUN_MODE` is runtime config, not a build variant: only 5 branch points
  ([config.py:441](miki_sorter_bot/config.py:441), [diagnostics.py:59](miki_sorter_bot/diagnostics.py:59),
  [main.py:222](miki_sorter_bot/main.py:222), [main.py:286](miki_sorter_bot/main.py:286),
  [main.py:453](miki_sorter_bot/main.py:453)); mode branch happens *after* the whole app is
  assembled, so polling ↔ webhook is a one-line `.env` change with full feature parity.
- ✅ Polling is the right mode for a home NAT machine (outbound-only, tighter album
  batching, drops only webhook-specific self-healing which polling can't need).
  Webhook code left fully intact.
- ✅ Retrieval overflow rule ([retrieval.py:287](miki_sorter_bot/retrieval.py:287)): if
  `limit` not explicit and logical-post matches `> DEFAULT_REQUEST_LIMIT` (10), Miki
  replies a numbered preview list (cap 30) and copies **nothing**; exactly 10 delivers;
  albums count as one logical post.
- ✅ Multi-keyword search confirmed ([repositories.py:835](miki_sorter_bot/repositories.py:835)):
  comma-separated keywords + `"phrases"`, `match: all` (default, AND) vs `any` (OR) via
  `HAVING matched_count = N` / `>= 1`; dedups; matches aggregate across album members.
- ✅ Burner backfill is **already** a standalone process independent of the bot
  ([burner.py](miki_sorter_bot/burner.py) — never imports PTB, shares only SQLite, no token
  lock) and already resumable via a `min_id` checkpoint.

### Decisions locked (🔒 — design agreed, not yet built)
- 🔒 Build a **new dedicated `miki-backfill` command** (read-only by construction),
  not just flags on `miki-burner backfill`.
- 🔒 Bound each run by **count AND time, first hit wins** (`--limit` + `--max-minutes`).
- 🔒 Backfill boundary = **`MIN(source_message_id)` over all posts Miki has already
  indexed in the topic** ("earliest post Miki has") — guarantees no overlap and no gap.
- 🔒 Keep **one shared database**; backfilled rows stamped `source_kind='backfill'`
  (searchable/retrievable/auditable/purgeable). No separate DB file.

---

## 3. What we have NOT done yet

### Offline migration (OS prepped; app side untouched)
- ⚠️ **Fully disable hibernate feature + Fast Startup** — needs elevated shell:
  `powercfg /hibernate off`.
- ⚠️ **Windows Defender exclusion for `var\`** — attempt was blocked by the permission
  classifier; run elevated:
  `Add-MpPreference -ExclusionPath "C:\Users\danie\Documents\Coding\Miki-a-telegram-media-archiver\var"`.
- ⬜ Configure Windows Update active hours / deferral.
- ⬜ Time sync (`w32tm /resync`) — `BACKUP_TIME` is UTC; integration HMAC uses a 300s window.
- ⬜ BIOS auto-restart-after-power-loss (if unattended).
- ⬜ **App install**: venv + `pip install .` (there is currently **no `.env` and no
  `var/`** — Miki is not installed on this machine yet).
- ⬜ Merge `.env` from the droplet (via `scp`, UTF-8 **no BOM**, no inline comments):
  set `RUN_MODE=polling`, blank `WEBHOOK_*`, `ALBUM_FLUSH_DELAY_SECONDS` back to `5`,
  `HEALTH_SERVER_ENABLED=true` + `HEALTH_LISTEN=127.0.0.1`. Keep `BOT_TOKEN`,
  `SOURCE_CHAT_ID`, `ARCHIVE_CHAT_ID`, `ADMIN_USER_IDS`, `REQUEST_*`.
- ⬜ **Copy the droplet DB** (`var/`) — runtime state (source topic, forwarding pairs,
  request topics, notice text, managers, `/set` overrides) lives in SQLite, not `.env`.
  Stop the droplet bot first; never plain-copy a live WAL DB.
- ⬜ **Stop the droplet instance before starting the PC** — Telegram allows one consumer
  per bot token. Consider regenerating the token after decommission.
- ⬜ NSSM service for 24/7 supervision (auto-start + restart-on-exit, `AppDirectory` pinned).

### Burner as backup + retrieve manager
- ⬜ `pip install ".[burner]"` (telethon + pyrage) — verify wheels resolve on Python 3.13.
- ⬜ `age` keypair (public key on PC, **private key off-machine**); `miki-burner-login` once.
- ⬜ Task Scheduler jobs (no cron on Windows): backup offload, backfill catch-up, bridge-once.
- ⬜ Restore drill to prove the age private key works before relying on it.

### The new `miki-backfill` mechanism
- ⬜ **Not started** — full phased plan in §4.

---

## 4. `miki-backfill` implementation plan

### Goal
A standalone, **read-only**, resumable crawler that indexes an archive topic's
**pre-Miki history only**, day-budgeted for account safety, sharing Miki's one DB.

### Design invariants (must hold at every phase)
1. **No overlap:** crawl window is strictly below the boundary
   (`iter_messages(min_id=resume, max_id=boundary)`; Telethon yields `resume < id < boundary`).
2. **No gap:** boundary is the earliest post Miki already has, so `[start, boundary)` is
   exactly the un-indexed history.
3. **Read-only by construction:** the entry point imports only the backfill path — no
   backup/bridge/send/heartbeat. Its sole Telegram call is `iter_messages`.
4. **Reuse, don't fork:** the crawl engine (`backfill_topic`) stays the single source of
   truth; the new command is a thin wrapper.
5. **Crash-safe:** resumption derives from durable DB state; a `--max-minutes` cutoff or a
   kill never loses a committed chunk.
6. **Bot-independent:** runs whether Miki is up or down (shared WAL SQLite, no token lock).
7. **Topic fidelity:** the crawl's `topic_id` is an archive-chat topic thread_id from the
   `topics` table (keyed by `archive_chat_id`). Because the crawl is `reply_to=topic_id`-scoped
   ([burner_backfill.py:192](miki_sorter_bot/burner_backfill.py:192)) and stamps
   `thread_id_override=topic_id` ([burner_backfill.py:155](miki_sorter_bot/burner_backfill.py:155)),
   every backfilled row's `source_thread_id` equals the thread_id a `topic: <name>` request
   resolves to ([retrieval.py:595](miki_sorter_bot/retrieval.py:595) →
   [retrieval.py:279](miki_sorter_bot/retrieval.py:279)) — so backfilled rows are retrievable
   through the same path as live rows, with no per-message topic inference needed. Backfill is
   invoked **once per archive topic**.

### Key seams
- **Seam A — repository queries** (`repositories.py`): boundary + below-boundary resume.
- **Seam B — crawl engine** (`burner_backfill.py`): `max_id`, `max_seconds`, `jitter`,
  incremental commit.
- **Seam C — CLI/entry point** (`backfill_cli.py` + `pyproject.toml`): arg parsing,
  auto-boundary, budget wiring, summary output.
- **Seam D — schedule + docs**: Task Scheduler + `burner-layer.md`.

---

### Phase 0 — Baseline & safety net
**Do:** run the existing suite green first; capture current behavior of
`backfill_topic` / `run_backfill` / `max_indexed_message_id` as the reference.
Confirm **commit granularity** in `upsert_post` / `Storage` (does it commit per post or
per batch?) — this decides whether Phase 2 must add periodic commits.
**Test/gate:**
```
python -m pytest -q
python -m compileall -q miki_sorter_bot tests
```
**Seam:** none touched. Establishes the "before" line.

---

### Phase 1 — Repository boundary queries (Seam A)
**Do:** add
- `first_indexed_message_id(chat_id, thread_id) -> int | None` — `MIN(source_message_id)`
  over all posts in the topic (the boundary); `None` when Miki has nothing there.
- `backfill_resume_message_id(chat_id, thread_id, boundary) -> int` —
  `MAX(source_message_id)` among `source_kind='backfill'` rows **below** the boundary; `0` if none.

Do **not** alter existing `max_indexed_message_id` (the bot/`miki-burner` path still uses it).

**Test (unit, in-memory SQLite, no Telegram):**
- Empty topic → boundary `None`, resume `0`.
- Topic with only `miki_copy` rows → boundary = min of those.
- Mixed `miki_copy` + `backfill` + live rows → boundary = global min; resume = max backfill below boundary.
- Backfill rows exist right up to boundary-1 → resume = boundary-1.
- Off-by-one: a post exactly at the boundary is never selected as resume.

**Gate:** new unit tests + full suite green. Seam A callable and correct in isolation
before anything consumes it.

---

### Phase 2 — Crawl engine parameters (Seam B)
**Do:** extend `backfill_topic` with `max_id: int | None`, `max_seconds: float | None`,
`jitter: float`; thread them (and existing `batch_delay`/`batch_size`) through
`run_backfill` and `telethon_history_factory` (pass `max_id` to `iter_messages`).
Add **incremental commit** if Phase 0 showed end-only commits. Stop when *either*
`limit` posts indexed *or* `max_seconds` elapsed (first hit wins), returning a clean
`BackfillOutcome`. Preserve flood-wait handling unchanged.

**Test (unit, fake `HistoryFactory` — no live Telegram, per existing test style):**
- `max_id` boundary excludes messages `>= boundary` (fake yields ids straddling boundary).
- First-hit-wins: limit reached before time → stops on count; time reached before limit
  → stops on count-not-reached with partial `indexed`.
- `max_seconds` cutoff mid-batch leaves a durable checkpoint (assert committed rows).
- Jitter stays within `[batch_delay, batch_delay+jitter]` (inject fake `sleep`, capture args).
- Flood-wait still caught → sleeps `seconds+1` → resumes from cursor.
- Resume across two simulated runs reaches exactly `[start, boundary)` with no repeats.

**Gate:** engine tests + full suite green. Engine correct with fakes before any real
session or CLI exists.

---

### Phase 3 — CLI + entry point (Seam C)
**Do:** add `miki_sorter_bot/backfill_cli.py` (`main()`): `load_dotenv`, load settings,
open `Storage`, compute boundary via Seam A (unless `--max-id` given), compute resume,
call `run_backfill` with the budget, print a summary
(`boundary`, `resumed_from`, `scanned`, `indexed`, `next checkpoint`, `remaining est.`).
Args per §4 CLI. `--estimate` reports boundary/remaining without indexing.
Register `miki-backfill = "miki_sorter_bot.backfill_cli:main"` in `pyproject.toml`.
Assert **read-only**: module imports no backup/bridge/heartbeat symbols.

**Test:**
- Arg-parse unit tests (defaults applied; `--limit`/`--max-minutes` parsed; bad topic id rejected).
- CLI wired to a fake engine (monkeypatched `run_backfill`) asserts boundary auto-compute,
  budget pass-through, and `--max-id`/`--min-id` overrides.
- `--estimate` performs no writes (assert DB unchanged).
- Import-surface test: importing `backfill_cli` pulls in no send/forward capable module.
- `pip install .` exposes the `miki-backfill` console script (smoke: `miki-backfill --help`).

**Gate:** CLI tests + full suite + `python -m pip check` green.

---

### Phase 4 — Live validation (single, guarded, real session)
**Do:** one manual, throttled run against the real archive topic on the residential IP:
`miki-backfill <topic> --limit 200 --max-minutes 10 --jitter 0.5`. Confirm boundary
excludes Miki's copies, rows land as `source_kind='backfill'`, `/status` counts rise,
and a `#request` returns a known pre-Miki post. **Verify topic fidelity (invariant 7):**
issue the `#request` by **topic *name*** (not thread id) and confirm a backfilled row comes
back — proves the crawl's `topic_id` matches the thread_id the request path resolves the name
to. This is the only step touching live Telegram; everything above is hermetic.
**Gate:** manual checklist recorded; no dead letters; account healthy.

---

### Phase 5 — Schedule + docs (Seam D)
**Do:** Windows Task Scheduler task (off-peak daily):
`miki-backfill <topic> --limit 2000 --max-minutes 20 --jitter 0.5`, working dir the repo
root. Document the multi-day model, overlap boundary, and account-safety rationale in
[docs/burner-layer.md](docs/burner-layer.md); cross-link this note.
**Gate:** doc review; a dry scheduled run advances the checkpoint by one bounded chunk
and stops cleanly at the boundary.

---

### Release gate (whole feature)
```
python -m pytest -q
python -m compileall -q miki_sorter_bot tests
python -m pip check
```
Plus `miki-doctor` and a limited `#request` smoke over backfilled rows.

---

## 5. Open questions / to confirm before/around build
- Phase 0 finding: is `upsert_post` commit per-row or per-batch? (decides Phase 2 commit work).
- Multi-topic sequencing: one topic per invocation (schedule several) vs. a repeatable
  `--topic` — defaulting to one-per-invocation unless requested otherwise.
- Whether to also expose the new pacing knobs on the existing `miki-burner backfill`
  (currently **no** — dedicated read-only tool only, per the locked decision).
