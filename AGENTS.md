# AGENTS.md — Miki (Telegram media archiver/sorter)

Read this first. Update it after every implementation. Max 1000 lines, each line ≤30 words.

## Conventions
- Smallest correct diff. Before adding anything, name the existing primitive it extends and why it does not fit.
- Chat-configurable knobs are one `SettingSpec` in `settings_registry.py`. Never add a per-setting command.
- Timed or repeating work uses the shared `JobQueue` (`main.py`), wrapped like `make_tick_job` in `periodic_notice.py`.
- Persistent key/value state uses `runtime_settings` via `get/set/delete_runtime_setting`. Schema changes are a new `Migration` in `migrations.py`.
- Admin actions call `_audit(...)`. Audit outcomes must be `success`, `denied` or `failed` (DB CHECK drops anything else).
- Telegram calls are best-effort: log and audit failures, never break sorting. Nothing awaits inside the sorting hot path.
- Application code is dispatched through TriAPI rebuild; sessions plan, write prompts, and audit responses.
- Work only in the root package. The old `windows/` copy was purged (recoverable from commit 808c019).

## Test and deploy
- `make test` runs `python -m pytest -q` (project `.venv`). `make verify` is the full gate: lint, format, mypy, bandit, pip-audit, package.
- Single file: `python -m pytest tests/test_sorting.py -q`. Format only touched files with `ruff format <files>`.
- After `uv sync` use `--all-extras` or the dev tools vanish.
- `miki-sorter.service` runs a uv-installed COPY, not the repo. Deploy: back up `var/miki.sqlite3`, `uv tool install --reinstall .`.
- Then `systemctl --user restart miki-sorter.service` and `miki-doctor`. Migrations are forward-only. Live schema is 17 (2026-09-27).
- Bot admin rights on the source group: Manage Topics yes (rotation works), Delete Messages no, Restrict Members no.

## Dispatch recipe (TriAPI)
- Write the prompt to a scratchpad file. From `~/Documents/Coding/TriAPI/rebuild` run `python3 scripts/call_deepseek.py --prompt-file F --system-file RULES.md`.
- Use quoted heredocs (`<<'E'`). Prompts quote target code verbatim, state behaviour not code, one function per call, code blocks only.
- Prompts must list exact import paths (`miki_sorter_bot.*`) and use the `database_connection` fixture directly.
- Reject and redispatch on any defect; never patch silently. Hand edits: wiring, imports, types, one-token test fixes; report them.
- Recurring DeepSeek defects: invented attributes/paths, dropped imports or `{n}` braces, dict-vs-tuple rows, UTF-16 offsets, wrong chat id.
- Ignore the harmless `oh-my-llama` stderr lines.

## Architecture (see `docs/architecture.md`, `docs/codebase-map.md`)
- `main.py` wires services, handlers, and JobQueue jobs. `sorting.py` is the hot path for source-topic media.
- `retrieval.py` serves member requests. `indexing.py` builds the searchable post index. `burner_backfill.py` crawls history into it.
- `settings_registry.py` holds `SettingSpec`, `SettingsRegistry`, `LiveSettings`. `management.py` holds admin commands.
- `repositories.py` is all SQLite access. `storage.py` opens the DB (WAL, `synchronous=NORMAL`). `bot_console.py` runs commands via `miki-ops bot`.
- Source topics live in `source_chat_id`; the `topics` table holds archive-chat topics only. Retrieval copies from archive posts.

## File index (package `miki_sorter_bot/`)
- Routing: `sorting.py`, `routing.py`, `lookback.py`. Requests: `retrieval.py`. Index: `indexing.py`, `burner_backfill.py`.
- Config: `config.py`, `settings_registry.py`. Admin: `management.py`, `ops.py`, `bot_console.py`, `operations.py`.
- Storage: `repositories.py`, `migrations.py`, `storage.py`. Reliability: `recovery.py`, `reliability.py`, `diagnostics.py`.
- Notices: `periodic_notice.py`. Counter and album dedup: `topic_activity.py`. Timed deletion: `message_deletion.py`. Rotation: `rotation.py`.
- Burner account: `burner*.py`. Serving: `main.py`, `health_server.py`, `webhook_supervisor.py`. Planned: `forward_mute.py` (phase 6).
- Tooling: `scripts/bench_indexing.py` (indexing benchmark, outside `make verify`).

---

# Completed work

## Backup to second group and sender removal (phase 1)
- `Sorting` copies `#JAV` media to topic 2 and `#Asian` to topic 3 of the backup group; JAV wins. Whole-tag match via `HASHTAG_RE`.
- Specs `media_backup_chat_id`, `media_backup_tag_topics`. Captions lose @mentions and links (UTF-16 aware); copies show no sender.
- Dedupe per `(chat_id, message_id)` in RAM (5000) and per `file_unique_id` in `backup_files` (migration 17, not backfilled). Failures count in `media_backup_failures`.
- Same file twice in one batch is sent once. `_record_backup_success` DB write is best-effort (logged, never raised). Key ignores destination topic: first tag wins.
- Albums copy inside `_album_send_gate` (one album upload at a time).

## Index write speed (phase 2)
- `PRAGMA synchronous=NORMAL` in `Storage.open` gave ~15x (347 to 5294 posts/s). Power loss may drop the last commit, never corrupt.
- `MessageIndexer(cache_mappings=True)` for burner crawls only. Benchmark: `TMPDIR=~/.cache/bench python scripts/bench_indexing.py --posts 20000 --mappings 50 --reindex`.
- Bulk transactions reached 26k posts/s but were reverted as unused; burner batching needs buffering so no write lock spans Telegram waits.

## Shared media counter (phase 3)
- `TopicActivity` (`is_new_post`, `record`, `rotation_count`, `reset_rotation`) owns album dedup and the persisted count `rotation_media_count`=`<thread>:<n>`.
- A stored thread different from the effective source topic reads as 0, which self-heals `/source_set` and rotation.
- Sorting calls `record` once, then `notice.on_media(..., counted=)`. Edits and Miki-authored messages are not counted.

## Timed deletion and request cleanup (phase 4)
- `MessageDeletionService.schedule(chat_id, message_id, delay_seconds)` plus a 60s sweep job. Table `scheduled_deletions` (migration 14), `INSERT OR IGNORE`.
- Sweep drops rows on non-retryable errors (Forbidden, BadRequest), re-queues retryable ones +300s, stops on RetryAfter, caps 50 per tick.
- Specs `request_response_ttl_hours` (24, max 48, 0 disables), `request_delete_user_message`. Deleting members' messages needs Delete Messages; Forbidden counts in `scheduled_deletions_failed`.
- `RetrievalService(deletion=)` routes replies through `_reply` and schedules copies and the request message. A crash between copy and schedule leaks one message (accepted).

## Duplicate media link (phase 4b)
- `posts.file_unique_id` (migration 15). `has_duplicate_file` checks for an earlier available post with the same file in the archive.
- Duplicates are silently skipped before being archived or forwarded. `duplicate_notice_enabled` spec remains but unused.
- Limits: exact file match only; posts indexed before migration 15 have no id. Archive dedupe and backup dedupe are fully decoupled.

## Topic rotation (phases 5, 5b, 5c)
- `RotationService` (`rotation.py`): 60s tick rotates the source topic at `rotate_media_threshold` posts or `rotate_interval_hours`, whichever first. Ships off.
- Specs: `rotate_enabled`, `rotate_media_threshold` (1000), `rotate_interval_hours` (336), `rotate_topic_title` (needs `{n}`), `topic_cycle` (unset means 10), `closed_topic_delete_days` (30), `rotate_milestones_enabled`.
- Commands (super admin): `/rotate_now`, `/rotate_status`, `/rotate_cleanup [confirm]`. Announcements in old and new topic are permanent.
- Keys: `cycle_started_at`=`<thread>:<epoch>`, `rotation_pending`=`<old>:<new>:<cycle>`, `rotation_retry`=`<retry_at>:<failures>`, `rotation_last` JSON, `rotation_milestones`=`<thread>:<cycle>:<labels>`.
- `rotate` creates the topic, persists pending, then `_finish` (idempotent). A tick resumes pending work; no second topic is created while pending.
- `_finish` order: switch `source_thread_id`, bump `topic_cycle`, reset counters, rewrite explicit notice roster, `retarget_bridges`, pointer messages, close and record old topic.
- Failures back off 300s doubling, cap 6h (tick only; `/rotate_now` bypasses) and notify operators. Interval trigger needs at least one counted post.
- Closed topics are recorded in `rotated_topics` (migration 16). `track_topic_status` ignores them. `/rotate_cleanup` deletes only those rows, only after confirm; needs Delete Messages.
- Milestones: notices at 80/90/100% of each trigger plus "within 24 hours"; highest crossed level only; labels marked before sending; auto-delete after 24h.
- `run_diagnostics` reads the effective source topic. Tests: `tests/test_rotation*.py`, `tests/test_topic_activity_sorting.py`.
- Accepted gaps: posts in the old topic between switch and close go unsorted; a resume may repeat the pointer; a failed close is not retried.
- Accepted gaps: two 100% notices can post together; a 100% notice may precede a failed rotation; a stale cycle clock can rotate at once after a long disabled period.
- Live test 2026-09-25 worked; reverted (source 66512, cycle 10, next `Cycle 11`). Test topic 68164 was left in place.

---

# Carryover

## Phase 6 — Forwarded-media sender mute [ ] DEFERRED (blocked on admin rights)
- Decided 2026-09-25: no burner-account bridge for deletes or mutes (personal-account session and ban risk). Bot rights only.
- Do not start until the group admin grants the bot Delete Messages and Restrict Members (decided 2026-09-25: unlikely, so deferred). Needs phases 4 and 1 (done). Start of session: read this file, run `make test`. End: tick the phase, fill Handoff, commit.
- Files: new `forward_mute.py`, `sorting.py`, `settings_registry.py`, `main.py`, `docs/codebase-map.md`.
- Extends `Management._is_admin` (exempt admins and managers), `_audit`, `MessageDeletionService.schedule`, and sorting entry `handle_update`.
- Locked: delete the media first, then restrict the sender, then post a tagged reason notice deleted after 24h.
- Check `forward_origin` (PTB >= 21.4). Only origin type `user` counts; `hidden_user`, `chat`, `channel` are skipped.
- Scope: the effective source topic and forwarding-pair source topics, the same predicate sorting uses. Run before counting, look-back, backup and sorting.
- Every album member is deleted on arrival. Mute and notice fire once per (chat, user) within a short window.
- Settings: `forward_mute_enabled`, `forward_mute_minutes`, `forward_mute_reason` as `SettingSpec`s, editable text via `/set`.
- Open: notice posts in the source topic tagging the user, since bots cannot DM non-starters.
- Tests: delete precedes restrict, album fully deleted, admin and manager exempt, hidden sender skipped, API errors audited, one mute per album, never backed up.
- Handoff:
