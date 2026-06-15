# Miki Implementation Checklist

Each job should stay small, independently reviewable, and usually touch only one concern.

## Phase 1: Inspect and Specify

- [x] **J01 - Audit the existing repository**
  Identify framework, entry points, configuration, database code, and tests.
- [x] **J02 - Document Telegram constraints**
  Confirm forum-topic behavior, update types, permissions, and bot interoperability.
- [x] **J03 - Define sorter syntax**
  Specify accepted hashtags, manually assigned keywords, multiple-match behavior, unknown tags, and conflicts.
- [x] **J04 - Define `#request` syntax**
  Specify topic, keywords, limits, validation, and response behavior.
- [x] **J05 - Define keyword extraction**
  Specify hashtags, ALL-CAPS words, capitalized names, codes, exclusions, and normalization.
- [x] **J06 - Define acceptance scenarios**
  Write concrete examples of successful and rejected sorting/retrieval actions.

## Phase 2: Foundation

- [x] **J07 - Centralize configuration**
  Environment variables, limits, database location, and authorized administrators.
- [x] **J08 - Add structured logging**
  Correlation IDs and safe logs without bot tokens or unnecessary captions.
- [x] **J09 - Design the database schema**
  Posts, topic mappings, processed updates, jobs, delivery records, and schema versions.
- [x] **J10 - Add database migrations**
  Versioned, reversible schema changes.
- [x] **J11 - Add repository interfaces**
  Isolate database operations from Telegram handlers.
- [x] **J12 - Add automated test infrastructure**
  Unit tests, fixtures, temporary database, and mocked Telegram calls.

## Phase 3: Topic Management

- [x] **J13 - Build a stable topic registry**
  Identify topics using `chat_id + message_thread_id`.
- [x] **J14 - Add hashtag-to-topic mappings**
  Support creation, replacement, listing, and removal.
- [x] **J14A - Add keyword-to-topic mappings**
  Let authorized users assign searchable words or phrases to stable destination topic IDs.
- [x] **J14B - Add keyword mapping commands**
  Define commands to add, list, inspect, replace, and remove keyword mappings for a topic.
- [x] **J14C - Define keyword matching rules**
  Specify case sensitivity, whole-word versus partial matching, phrases, punctuation, and caption boundaries.
- [x] **J14D - Define routing precedence and conflicts**
  Decide how hashtag routes and keyword routes interact when one message matches multiple topics.
- [x] **J15 - Add admin authorization**
  Restrict mapping and maintenance operations, with optional delegated permission for trusted users.
- [x] **J16 - Validate topic availability**
  Detect missing, closed, or inaccessible destinations.

## Phase 4: Indexing

- [x] **J17 - Model an indexed post**
  Store Telegram references, topic, media group, media type, timestamps, and source.
- [x] **J18 - Implement token extraction**
  Extract hashtags, ALL-CAPS tokens, names, numbers, and mixed codes.
- [x] **J19 - Version the extractor**
  Record which extraction rules produced each index.
- [x] **J20 - Handle media albums**
  Treat one `media_group_id` as a logical post.
- [x] **J21 - Handle edited captions**
  Refresh tokens without duplicating records.
- [x] **J22 - Add duplicate detection**
  Enforce unique message and operation identifiers.
- [x] **J23 - Add loop prevention**
  Recognize Miki-generated and previously delivered copies.
- [x] **J24 - Add controlled reindexing**
  Rebuild tokens in bounded batches.

## Phase 5: Sorting

- [x] **J25 - Detect sortable media**
  Ignore unrelated messages and unsupported sources.
- [x] **J26 - Resolve destination hashtags**
  Resolve hashtag and user-configured keyword matches using the documented precedence and conflict rules.
- [x] **J26A - Explain routing decisions**
  Provide an authorized diagnostic command showing which hashtag or keyword caused a post to match a topic.
- [x] **J27 - Create durable sorting jobs**
  Save intended actions before contacting Telegram.
- [x] **J28 - Copy media to its destination**
  Preserve captions and albums as intended.
- [x] **J29 - Record delivery lineage**
  Connect original and copied message IDs.
- [x] **J30 - Add dry-run mode**
  Show the proposed destination without copying anything.

## Phase 6: Retrieval

- [x] **J31 - Parse `#request` forms**
  Return understandable validation errors.
- [x] **J32 - Authorize retrieval requests**
  Restrict allowed users, bots, topics, and result counts.
- [x] **J33 - Implement indexed search**
  Match target topic and normalized keywords.
- [x] **J34 - Define matching semantics**
  Choose AND/OR behavior, exact codes, hashtag matching, ordering, and deduplication.
- [x] **J35 - Create durable retrieval jobs**
  Make large requests restartable.
- [x] **J36 - Copy matching posts**
  Deliver gradually while preserving album grouping.
- [x] **J37 - Send request summaries**
  Report matched, copied, unavailable, skipped, and failed totals.
- [x] **J38 - Add cancellation**
  Allow administrators to stop large retrieval jobs.

## Phase 7: Reliability

- [x] **J39 - Implement retry classification**
  Separate temporary, rate-limit, permission, and permanent errors.
- [x] **J40 - Add exponential backoff**
  Honor Telegram's `retry_after` value and add jitter.
- [x] **J41 - Add a dead-letter queue**
  Retain failed jobs for diagnosis and manual retry.
- [x] **J42 - Add rate limiting**
  Apply limits per user, bot, chat, and operation.
- [x] **J43 - Add graceful shutdown and recovery**
  Return interrupted work to the queue.
- [x] **J44 - Reconcile unavailable posts**
  Mark messages that Telegram can no longer copy.

## Phase 8: Interoperability and Security

- [x] **J45 - Define a versioned integration contract**
  Specify stable JSON inputs, outputs, errors, and schema versions.
- [x] **J46 - Add client identities and scopes**
  Separate `submit`, `search`, and `admin` permissions.
- [x] **J47 - Add signed webhook verification**
  Use HMAC, timestamps, and replay protection if an HTTP API is introduced.
- [x] **J48 - Add integration quotas**
  Prevent one program from exhausting the queue.
- [x] **J49 - Add an audit trail**
  Record important administrative and delivery actions.

## Phase 9: Operations

- [x] **J50 - Add health and status reporting**
  Show database, queue, worker, and Telegram connectivity status.
- [x] **J51 - Add useful metrics**
  Track processing time, queue depth, failures, retries, duplicates, and throttling.
- [x] **J52 - Add database maintenance**
  Retention, pruning, compaction, and index checks.
- [x] **J53 - Add backup and restore procedures**
  Test restoration rather than only creating backups.
- [x] **J54 - Add deployment documentation**
  Installation, permissions, configuration, startup, upgrade, and rollback.

## Phase 10: End-to-End Validation

- [x] **J55 - Test sorter workflows**
  Single media, albums, hashtags, keyword mappings, phrases, overlapping matches, edits, and retries.
- [x] **J56 - Test retrieval workflows**
  Valid forms, invalid forms, no results, large results, and unavailable posts.
- [x] **J57 - Test restart recovery**
  Interrupt processing at each important stage.
- [x] **J58 - Test bot/program cooperation**
  Duplicate submissions, loops, malformed payloads, quotas, and authentication.
- [ ] **J59 - Run a limited pilot**
  Enable one intake topic, a few hashtags, and a conservative request limit.
- [ ] **J60 - Review pilot evidence**
  Fix observed issues before enabling the whole supergroup.

## Release Validation

- [x] Run the complete automated suite.
- [x] Validate upgrades from every historical schema boundary.
- [x] Compile all application and test modules.
- [x] Verify installed dependency consistency.
- [x] Build the distributable wheel.
- [x] Stress a 2,000-post library with repeated bounded searches.
- [x] Verify maintenance, backup, restore, integrity, and foreign keys.
- [x] Review cross-service duplicate, retry, replay, quota, and restart behavior.
- [ ] Complete the limited live Telegram pilot (J59).
- [ ] Review and resolve pilot evidence (J60).

## Implementation Control

Authorize jobs by ID, such as **"Do J01 only."**

After each job, report:

- Changed files
- Tests performed
- Assumptions
- Unresolved decisions
