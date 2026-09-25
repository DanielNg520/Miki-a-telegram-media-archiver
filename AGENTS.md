# AGENTS.md — Miki (Telegram media archiver/sorter)

Read this first. Update it after every implementation. Max 1000 lines, each line ≤30 words.

## Conventions
- Smallest correct diff. Before adding anything, name the existing primitive it extends and why it does not fit.
- Chat-configurable knobs are one `SettingSpec` in `settings_registry.py`. Never add a per-setting command.
- Timed or repeating work uses the shared `JobQueue` (`main.py`), wrapped like `make_tick_job` in `periodic_notice.py`.
- Persistent key/value state uses `runtime_settings` via `get/set/delete_runtime_setting`. Schema changes are a new `Migration` in `migrations.py`.
- Admin actions call `_audit(...)`. Telegram calls are best-effort: log and audit failures, never break sorting.
- Application code is dispatched through TriAPI rebuild; sessions plan, write prompts, and audit responses.
- The old `windows/` copy was purged (staged deletion, recoverable from commit 808c019). Work only in the root package.

## Test commands
- `make test` runs `python -m pytest -q` (use the project `.venv`). `make verify` is the full gate: lint, typecheck, security, package.
- Single file: `python -m pytest tests/test_sorting.py -q`. `make coverage` adds a coverage report.

## Architecture (see `docs/architecture.md`, `docs/codebase-map.md`)
- `main.py` wires services, handlers, and JobQueue jobs. `sorting.py` is the hot path for source-topic media.
- `retrieval.py` serves member requests. `indexing.py` builds the searchable post index. `burner_backfill.py` crawls history into it.
- `settings_registry.py` holds `SettingSpec`, `SettingsRegistry`, `LiveSettings`. `management.py` holds admin commands.
- `periodic_notice.py` is the model service: counts media, JobQueue tick, delete-previous, runtime-KV state.
- `repositories.py` is all SQLite access. `storage.py` opens the DB (WAL). `bot_console.py` runs commands via `miki-ops bot`.

## File index (package `miki_sorter_bot/`)
- Routing: `sorting.py`, `routing.py`, `lookback.py`. Requests: `retrieval.py`. Index: `indexing.py`, `burner_backfill.py`.
- Config: `config.py`, `settings_registry.py`. Admin: `management.py`, `ops.py`, `bot_console.py`, `operations.py`.
- Storage: `repositories.py`, `migrations.py`, `storage.py`. Reliability: `recovery.py`, `reliability.py`, `diagnostics.py`.
- Notices: `periodic_notice.py`. Burner account: `burner*.py`. Serving: `main.py`, `health_server.py`, `webhook_supervisor.py`.
- New files planned below: `rotation.py` (phase 5), `topic_activity.py` (phase 3), `forward_mute.py` (phase 6).

---

# Completed Work

## Backup media to second group (commit 59f8e95)
- `Sorting._backup_to_second_group` copies media to chat `-1004365154840`: `#JAV` to topic 2, `#Asian` to topic 3, JAV wins.
- Albums use `copy_messages`; singles use `copy_message`. Hooked into `_deliver_album_messages` and three `handle_update` fast paths.
- Same commit added `_album_send_gate` (one album upload at a time), undocumented until now.
- Audit found defects; fixed in phase 1 below.

---

# Carryover: phased roadmap

Each phase runs in its own session. Start of session: read this file, run the test command, take the first phase not marked DONE.
End of session: tick the phase, fill its Handoff line, keep this file within limits, commit.
Phases are ordered by dependency. Do not start a phase whose "Needs" phases are not DONE.

## Temporary waiver (granted by user 2026-09-25)
- The size and format limits above (1000 lines, 30 words per line, single file) are waived until phase 6 is DONE.
- Waiver ends when phase 6 is ticked: trim this file back within limits and delete this section in that session.

## Locked decisions
- Rotation: new topic when 1000 media posts (album = 1) or 336 hours (2 weeks) pass, whichever first.
- Topic title `Cycle {n}`; current cycle is 10, so the next topic is `Cycle 11`. Counter advances only after a successful switch.
- Request responses (Miki text replies and delivered media) and the requester's request message auto-delete after 24h, configurable.
- Forwarded media with a visible sender: delete the media first, then mute, then post a tagged reason notice deleted after 24h.
- Backup copies must show no sender: no forward header, and captions have @mentions and links stripped.
- Stripping needs a caption override: `copy_message(caption=...)` for singles; albums re-send via `send_media_group` since `copy_messages` cannot edit captions.
- Rotation media count persists in `runtime_settings` (option C, one row upsert per counted post) so restarts never reset it.
- Persisted keys: `rotation_media_count`, `cycle_started_at`, `topic_cycle`. Album dedup memory stays in RAM.
- Deleting closed topics is manual-confirm in the first release; only topics Miki closed herself.

## Open decisions (ask the user before the phase that needs them)
- Muted-member notice (phase 6): assumed posted in the source topic tagging the user, since bots cannot DM non-starters.

## Phase 0 — Line endings and baseline [x] DONE
- Needs: none. Decided order: (1) commit the `windows/` purge (86 files already staged), (2) `.gitattributes` + normalization, (3) review the ~575 real edits, run `make test`, commit separately.
- `.codegraph/` is already added to `.gitignore` (uncommitted); include it in the first commit. Always gitignore it in every repo.
- Working tree shows ~3,800 changed lines but only ~575 real; HEAD is CRLF, tree is LF.
- Add `.gitattributes` (`* text=auto eol=lf`), commit normalization alone, then commit or stash the real pending edits separately.
- Done when: `git diff --stat` is small and `uv run pytest -q` is green.
- Handoff: commits 76556ea (windows/ purge, .codegraph ignore), LF normalization + `.gitattributes`, then pending edits. `git diff` clean, 421 tests green. Next: phase 1.

## Phase 1 — Backup hardening and sender removal [ ] TODO
- Needs: phase 0. Files: `sorting.py`, `settings_registry.py`, `indexing.py`, `tests/test_sorting.py`.
- Match hashtags as whole tags (`#java`, `#javascript` must not match `#jav`); import the single `HASHTAG_RE` from `indexing.py`.
- Move backup chat id, tag-to-topic map into registry specs; remove hardcoded values and trailing whitespace.
- Dedupe per `(chat_id, message_id)` so retries and look-back never back up twice. Run backup inside or after `_album_send_gate`, not before.
- Check `copy_messages` result count; audit or count failures instead of only logging.
- Sender removal: strip @mentions and links (also text_link/mention entities) from captions before backup; keep `#JAV`/`#Asian` tags matched on the original text.
- Verify the copied post carries no "forwarded from" header. Remove duplicate `_effective_source_thread_id` in favour of `LiveSettings`.
- Tests: tag precedence, substring false positives, album, dedupe, failure swallowed, no sender/@mention in backup.
- Handoff:

## Phase 2 — Index/database build optimization [ ] TODO
- Needs: phase 0. Files: `indexing.py`, `repositories.py`, `storage.py`, `burner_backfill.py`, new benchmark script.
- First write a benchmark: 20k synthetic posts through `MessageIndexer.index`; record before numbers here.
- Cache `list_mappings` per crawl in `MessageIndexer`; use `RETURNING id`; stop the extra `get_post` re-read when unused.
- Skip token delete/insert when caption and `extractor_version` are unchanged.
- Add a `bulk()` transaction context; wrap each backfill batch, checkpoint `min_id` only after commit.
- Add `PRAGMA synchronous=NORMAL` and `PRAGMA optimize` after bulk runs.
- Gate: `test_indexing`, `test_retrieval`, `test_recovery`, `test_burner_backfill` pass unchanged. Record after numbers here.
- Handoff:

## Phase 3 — Shared persisted media counter [ ] TODO
- Needs: phase 0. Files: new `topic_activity.py`, `periodic_notice.py`, `sorting.py`, `settings_registry.py`.
- Extract count and album-dedup logic from `PeriodicNoticeService.on_media` into `TopicActivity`; notice behaviour must not change.
- Add persistence (option C): count stored under `rotation_media_count` in runtime KV, upserted per counted post. Notice keeps in-memory.
- Persistence failure is logged and skipped; it must never break sorting. Measure write cost in the phase 2 benchmark.
- Tests: `tests/test_periodic_notice.py` unchanged and green, plus restart-survives-count and album-counts-once.
- Handoff:

## Phase 4 — Timed message deletion queue and 24h request cleanup [ ] TODO
- Needs: phase 0. Files: `migrations.py`, `repositories.py`, `retrieval.py`, `main.py`, `settings_registry.py`.
- Migration: `scheduled_deletions(chat_id, message_id, delete_at, PRIMARY KEY(chat_id, message_id))` plus index on `delete_at`.
- Repository: `schedule_deletion`, `due_deletions`, `remove_deletions`. One 60s sweep job on the shared JobQueue.
- Settings: `request_response_ttl_hours` (default 24, 0 = never), `request_delete_user_message` (default on).
- Hook one helper around `retrieval.py` sends so Miki replies, delivered media copies, and the request message are scheduled.
- Sweep removes rows even when delete fails (already gone), so nothing retries forever.
- Tests: survives restart, deleted once, TTL 0 schedules nothing, album members all scheduled.
- Handoff:

## Phase 5 — Topic rotation and closed-topic cleanup [ ] TODO
- Needs: phases 3 and 4. Files: new `rotation.py`, `migrations.py`, `repositories.py`, `management.py`, `main.py`, `settings_registry.py`.
- Specs: `rotate_enabled`, `rotate_media_threshold` (1000), `rotate_interval_hours` (336), `rotate_topic_title` (`Cycle {n}`), `topic_cycle` (seed 10 via `/set`), `closed_topic_delete_days`.
- Persist `cycle_started_at` and media count in runtime KV; reset both only after a successful rotation.
- Rotate: create topic, write `source_thread_id`, close old topic, set `topics.closed_at`, increment `topic_cycle`, audit. Lock against double rotation.
- Failure at creation changes nothing. Add `/rotate_now` and `/rotate_status` (admin).
- Delete closed topics only after the day count, only ones Miki closed, and require confirmation in v1.
- Tests: each trigger, first-wins, restart persistence, failed creation keeps cycle 10, next title is `Cycle 11`.
- Handoff:

## Phase 6 — Forwarded-media sender mute [ ] TODO
- Needs: phase 4 (notice deletion) and phase 1. Files: new `forward_mute.py`, `sorting.py`, `settings_registry.py`.
- Detect `forward_origin` with a visible user in the source topic; skip admins, managers, hidden senders, channels. Mute the forwarder.
- Runs before backup and sorting in `handle_update`, so offending media never reaches routing or the second group.
- Order: delete all offending messages (whole album) then `restrict_chat_member` then audit then tagged reason notice scheduled +24h.
- Settings: `forward_mute_enabled`, `forward_mute_minutes`, `forward_mute_reason` (editable text).
- Tests: delete precedes restrict, album fully deleted, admin exempt, API errors audited, media never backed up.
- Handoff:
