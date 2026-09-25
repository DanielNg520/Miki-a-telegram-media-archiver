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
- Tooling: `scripts/bench_indexing.py` (indexing benchmark, outside `make verify`).
- Notice counter: `topic_activity.py` (album dedup, persisted rotation count). Planned: `rotation.py` (phase 5), `forward_mute.py` (phase 6).

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

## Phase 1 — Backup hardening and sender removal [x] DONE
- Needs: phase 0. Files: `sorting.py`, `settings_registry.py`, `indexing.py`, `tests/test_sorting.py`.
- Match hashtags as whole tags (`#java`, `#javascript` must not match `#jav`); import the single `HASHTAG_RE` from `indexing.py`.
- Move backup chat id, tag-to-topic map into registry specs; remove hardcoded values and trailing whitespace.
- Dedupe per `(chat_id, message_id)` so retries and look-back never back up twice. Run backup inside or after `_album_send_gate`, not before.
- Check `copy_messages` result count; audit or count failures instead of only logging.
- Sender removal: strip @mentions and links (also text_link/mention entities) from captions before backup; keep `#JAV`/`#Asian` tags matched on the original text.
- Verify the copied post carries no "forwarded from" header. Remove duplicate `_effective_source_thread_id` in favour of `LiveSettings`.
- Tests: tag precedence, substring false positives, album, dedupe, failure swallowed, no sender/@mention in backup.
- Handoff: specs `media_backup_chat_id`, `media_backup_tag_topics` (`jav:2,asian:3`, ordered). Sorting: `_strip_sender_identifiers`, `_copy_one_to_backup`, in-RAM `_backed_up` dedupe (5000), metric `media_backup_failures`, album backup inside `_album_send_gate`. `_effective_source_thread_id` removed; use `_live`. 439 tests green. Phase 1 code came from TriAPI DeepSeek dispatch (4 audit rounds): audits caught UTF-16 offsets, wrong chat id, wrong-member dedupe. Cleanup 2026-09-25 (`make verify` green, 440 tests): added `_win_uninstall`, fixed types, CVE bumps in `uv.lock`. Next: phase 2.

## Integration rules (audit 2026-09-25, binding for phases 2-6)
- Run order: 2, 3, 4, 5, 6. Phase 3 needs the phase 2 benchmark; phase 5 needs 3 and 4; phase 6 needs 4.
- Baseline `make verify` is green (cleanup 2026-09-25). Each phase must keep it green: lint, format, mypy, bandit, pip-audit, package.
- Format only touched files with `ruff format <files>`; keep unrelated diffs out. After `uv sync` use `--all-extras` or dev tools vanish.
- Each phase lands: repo protocol methods, `main.py` wiring, `docs/codebase-map.md` entry, this file, tests. No half-wired service.
- New timed work is one JobQueue job via the `make_tick_job` pattern. Nothing awaits inside the sorting hot path.
- Source topics live in `source_chat_id`; the `topics` table holds archive-chat topics only. Never touch archive topics from rotation.
- Retrieval copies from archive-chat posts (`index_copy`), so deleting closed source topics cannot break requests.
- Admin checks reuse `Management._is_admin`; audit rows reuse `_audit`. Do not add parallel permission or audit code.
- Dispatch recipe: write the prompt to a scratchpad file, then from `~/Documents/Coding/TriAPI/rebuild` run `python3 scripts/call_deepseek.py --prompt-file F --system-file RULES.md`.
- Prompts quote target code verbatim, state one function per call, and ask for code blocks only. Reject and redispatch on any defect; never patch silently.
- Use quoted heredocs (`<<'E'`) for prompt files; unquoted ones let the shell eat backticks. Ignore the harmless `oh-my-llama` stderr lines.
- Tests are dispatched too; hand edits are limited to wiring, imports, types, and one-token test fixes, and are reported to the user.
- Dispatch prompts state behaviour, not code. Audit every reply: past defects were UTF-16 offsets, wrong chat id, wrong-member bookkeeping.

## Phase 2 — Index/database build optimization [x] DONE
- Needs: phase 0. Files: `indexing.py`, `repositories.py`, `storage.py`, `burner_backfill.py`, new benchmark script.
- Extends: `MessageIndexer.index`, `SqliteRepositories.upsert_post`, `Storage.open`. Nothing new to build except the benchmark.
- Step 1: benchmark 20k synthetic posts through `MessageIndexer.index`; record before numbers here. Keep only if it stays useful.
- Every further step must show a measured gain in the benchmark, else drop it. Do not ship unmeasured optimizations.
- Cache `list_mappings` per crawl in `MessageIndexer` with explicit invalidation; the burner process holds it only for one crawl.
- Drop the extra `get_post` re-read only after checking `upsert_post` callers; `MessageIndexer.index` ignores the return value.
- Token skip: compare the computed token set to the stored set, not caption plus `extractor_version`. Tokens also depend on route mappings.
- Bulk transactions: 43 `with self._connection:` blocks each commit, and inner commits would break an outer transaction.
- If bulk is justified, replace them with one `_tx()` context manager (depth counter, commit at depth 0). Otherwise rely on WAL plus `synchronous=NORMAL`.
- `PRAGMA synchronous=NORMAL` in `Storage.open` is global: a power loss can lose the last commit, never corrupt. Accept for phases 3-4.
- Checkpoint `min_id` after commit only when bulk exists. Add `PRAGMA optimize` after bulk runs.
- Gate: `test_indexing`, `test_retrieval`, `test_recovery`, `test_burner_backfill` pass unchanged. Record after numbers here.
- Step 1 DONE: `scripts/bench_indexing.py` (TriAPI DeepSeek, 3 rounds: audits caught non-media messages, invented private attribute). Run `TMPDIR=~/.cache/bench python scripts/bench_indexing.py --posts 20000 --mappings 50 --reindex`. `/tmp` is tmpfs; hides fsync cost.
- BEFORE (20k posts, btrfs): 0 mappings 348 posts/s first, 352 reindex. 50 mappings 328 / 329. `list_mappings` called once per post (20000). Cost is per-post commit plus fsync.
- Step 2 DONE: `MessageIndexer(cache_mappings=True)` (opt-in, burner crawls only; instance lifetime is the invalidation, no invalidate method). 328 to 347 posts/s (+6%), `list_mappings` calls 20000 to 1. Bench flag `--cache-mappings`.
- Audit 2026-09-25: clean. Bulk `_tx()` and `PRAGMA optimize` plan bullets above are superseded by Step 3. Cache dict fills even when caching is off (harmless).
- Step 3 DONE: `PRAGMA synchronous=NORMAL` in `Storage.open` (main connection only): 347 to 5294 posts/s (~15x). Bulk `transaction()` reached 26k posts/s (batch 100) but was reverted: unused code, and burner batching needs buffering so no write lock spans Telegram waits.
- Not pursued (unmeasured, plan says drop): `get_post` re-read, token-set skip, `PRAGMA optimize`, `min_id` checkpoint.
- Handoff: commits e0eea02 (benchmark), 42ac7a2 (mapping cache), then the pragma commit. 442 tests, `make verify` green. Benchmark is `scripts/bench_indexing.py`. Phase 3 can use it for write cost: ~5300 posts/s means one persisted counter row per post is cheap. Optional follow-up: burner batch flush (26k posts/s). Next: phase 3.

## Phase 3 — Shared persisted media counter [x] DONE
- Needs: phase 2 (benchmark for write cost). Files: new `topic_activity.py`, `periodic_notice.py`, `sorting.py`, `settings_registry.py`.
- Extends: the count and album-dedup logic already in `PeriodicNoticeService.on_media`. Move it, do not copy it.
- `TopicActivity.record(topic_id, group_id) -> bool` returns whether this post counted. It owns the shared album dedup window.
- Counters are independent per consumer: notice resets its count on every post; rotation resets only on rotation.
- Hook once in `sorting.py` beside the existing `_notice.on_media` call, not inside it: `on_media` returns early when notices are disabled.
- Rotation counts only the effective source topic, not every notice topic. Skip edits and Miki-authored messages as the notice does.
- Persist `rotation_media_count` as `<thread_id>:<count>`. A stored thread different from the effective source topic reads as 0.
- That self-heals `/source_set` and rotation with no change to `management.py`. Cache the count in RAM, load once, write once per counted post.
- Persistence failure is logged and skipped; it never breaks sorting.
- Tests: `tests/test_periodic_notice.py` unchanged and green, restart-survives-count, album-counts-once, stale-thread-reads-zero, notices-disabled-still-counts.
- Handoff: `topic_activity.py` `TopicActivity` (`is_new_post`, `record`, `rotation_count`, `reset_rotation`), key `rotation_media_count`=`<thread>:<n>`. Sorting calls `record` then `notice.on_media(..., counted=)`; notice falls back to own instance. 458 tests, `make verify` green. DeepSeek defects: invented `repositories.runtime_settings`, invented import paths, fixture passed as arg (redispatched once, then hand-fixed one loop and one assertion). Sorting-level counting covered by `tests/test_topic_activity_sorting.py`. Next: phase 4.

## Phase 4 — Timed message deletion queue and 24h request cleanup [ ] TODO (NEXT)
- Needs: phase 0. Files: `migrations.py`, `repositories.py`, `retrieval.py`, `main.py`, `settings_registry.py`, `docs/codebase-map.md`.
- Extends: `RetrievalService` sends, `update_retrieval_item(destination_message_id)`, the JobQueue tick pattern. New table is genuinely new.
- Migration: `scheduled_deletions(chat_id, message_id, delete_at, PRIMARY KEY(chat_id, message_id))` plus index on `delete_at`.
- Repository methods on the repo protocol and `SqliteRepositories`: `schedule_deletion`, `due_deletions`, `remove_deletions`.
- One 60s sweep job wrapped like `make_tick_job`; it lives with the deletion service, not in `retrieval.py`.
- Settings: `request_response_ttl_hours` (default 24, 0 = never), `request_delete_user_message` (default on), as `SettingSpec`s.
- Hook three points only: a `_reply` helper replacing every `request_message.reply_text`, both delivery success paths, and the request message.
- `RecoveredRequestMessage.reply_text` must return the sent message, like PTB, so recovered replies also schedule.
- Delivered copies use their `destination_message_id`; schedule at the same call that records `sent`.
- The sweep removes rows even when deletion fails (already gone) so nothing retries forever. Failures counted as a metric.
- Phase 6 notices reuse `schedule_deletion`; expose it as the public API of the deletion service.
- Tests: survives restart, deleted once, TTL 0 schedules nothing, album members all scheduled, recovered request replies scheduled.
- Handoff:

## Phase 5 — Topic rotation and closed-topic cleanup [ ] TODO
- Needs: phases 3 and 4. Files: new `rotation.py`, `migrations.py`, `repositories.py`, `management.py`, `main.py`, `settings_registry.py`.
- Extends: `TopicActivity` (count), `source_thread_id` runtime override (same key `/source_set` writes), JobQueue tick, `track_topic_status`.
- Trigger is one 60s tick job checking count and elapsed time, not a hot-path await. Lock so `/rotate_now` and the tick cannot double rotate.
- `cycle_started_at` persists as `<thread_id>:<epoch>`; a thread mismatch reads as now, mirroring the phase 3 counter self-heal.
- Specs: `rotate_enabled`, `rotate_media_threshold` (1000), `rotate_interval_hours` (336), `rotate_topic_title`, `topic_cycle`, `closed_topic_delete_days`.
- `rotate_topic_title` parser must require `{n}`; a bad title is rejected at `/set`, never at rotation time.
- Rotate order: create topic, write `source_thread_id`, update explicit `periodic_notice_topics` roster, close old topic, record it, increment `topic_cycle`, audit.
- If the roster is unset the notice already follows the effective source topic; only rewrite it when explicitly set and containing the old id.
- Creation failure changes nothing. A failure after creation is audited and the created topic is reported to admins, never silently orphaned.
- The `topics` table is archive-only. Add `rotated_topics(chat_id, thread_id, cycle, closed_at, deleted_at)` in a migration for Miki-closed source topics.
- `track_topic_status` sees the close event for the same chat; it must ignore rotated source topics rather than flip archive state.
- Announce in the old topic and the new topic (pointer message), scheduled through the phase 4 deletion service if temporary.
- Deletion of closed topics: only rows in `rotated_topics` with `chat_id == source_chat_id`, after the day count, manual confirm in v1, audited.
- Add `/rotate_now` and `/rotate_status` (admin) registered in the `main.py` handlers dict; both go through `_audit`.
- Tests: each trigger, first-wins, restart persistence, failed creation keeps cycle 10, next title `Cycle 11`, roster rewrite, archive topics never deleted.
- Handoff:

## Phase 6 — Forwarded-media sender mute [ ] TODO
- Needs: phase 4 (notice deletion) and phase 1. Files: new `forward_mute.py`, `sorting.py`, `settings_registry.py`, `main.py`.
- Extends: `Management._is_admin` (exempt admins and managers), `_audit`, phase 4 `schedule_deletion`, sorting entry `handle_update`.
- Check `forward_origin` on the message (PTB >= 21.4). Only origin type `user` counts; `hidden_user`, `chat`, `channel` are skipped.
- Scope: the effective source topic and forwarding-pair source topics, i.e. the same predicate sorting uses to decide it handles a message.
- Run before notice counting, look-back capture, backup and sorting, so offenders never count or reach routing.
- Every album member is deleted on arrival (members are separate updates). Mute and notice fire once per (chat, user) within a short window.
- Order: delete the message, then `restrict_chat_member`, then audit, then tagged notice scheduled +24h. All calls best-effort, failures audited.
- Settings: `forward_mute_enabled`, `forward_mute_minutes`, `forward_mute_reason` as `SettingSpec`s, editable text via `/set`.
- Tests: delete precedes restrict, album fully deleted, admin and manager exempt, hidden sender skipped, API errors audited, one mute per album, never backed up.
- Handoff:
