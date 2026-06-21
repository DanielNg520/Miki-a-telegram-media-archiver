# Phase 1: Inspection and Behavior Specification

Status: approved baseline for implementation  
Scope: J01-J06 only

## J01 - Existing Repository Audit

Miki is currently a Python 3.11+ package built with `setuptools`.

Runtime dependencies:

- `python-telegram-bot`
- `pydantic-settings`
- `python-dotenv`

Current entry points:

- `miki-sorter` starts the sorter.
- `miki-show-ids` reports Telegram chat and topic IDs.

Current modules:

- `main.py` filters incoming messages and copies matched media.
- `routing.py` extracts tokens and selects a configured route.
- `collector.py` checks candidate terms through the external Data Collector API.
- `config.py` loads and validates environment configuration.
- `show_ids.py` provides the topic-ID helper.

Current behavior:

- One configured source chat and source topic are monitored.
- Only messages containing supported media are considered.
- Exact, case-insensitive tokens are compared with static route keywords.
- Candidate tokens are confirmed by the external Data Collector.
- The first configured route with a confirmed token wins.
- The original Telegram message is copied without downloading its media.

Current limitations:

- There is no local post library or database.
- There is no retrieval workflow.
- Hashtags have no special routing priority.
- Routes and keywords cannot be managed through Telegram commands.
- Phrases are not supported.
- Albums are processed as independent messages.
- Edited messages are not handled explicitly.
- Delivery is not durable or idempotent.
- Authorization is configuration-based only.

The existing `build/`, `*.egg-info`, `__pycache__`, and `.pytest_cache` paths are generated
artifacts, not source-of-truth code.

## J02 - Telegram Constraints

- Forum destinations are identified by the stable pair `chat_id + message_thread_id`.
- Miki must receive the source message and have permission to send into the destination topic.
- An administrator bot receives all group messages regardless of normal privacy-mode filtering.
- `copyMessage` can copy a message into a forum topic without storing or downloading its media.
- Telegram albums share a `media_group_id`, but updates arrive as individual messages. Miki must
  buffer and group them if album-level behavior is required.
- Edited messages can arrive as `edited_message` updates and must update routing/index metadata.
- The regular Bot API does not provide a general deleted-message update for supergroups. A failed
  future copy is the reliable signal that an indexed source may no longer be available.
- Bot API updates can be repeated or replayed around failures. Processing must be idempotent.
- Telegram rate limits are dynamic. Miki must honor `retry_after` and process bulk results through
  a rate-limited queue.
- Message IDs are scoped to a chat and are not globally unique.
- A Telegram `file_id` is scoped to one bot identity and should not be the primary library key.
- Topic names may change; routing must never depend on a topic name alone.

## J03 - Sorter Syntax and Rules

Miki first checks whether supported media arrived in a directly forwarded source topic. If it did,
the configured source-topic → archive-topic pair determines the destination without inspecting the
message. Otherwise, Miki evaluates the text or caption in the legacy configured intake topic.

### Route Inputs

Routes may be triggered by:

1. A direct topic forwarding pair.
2. An explicit hashtag, such as `#Japan`.
3. A configured keyword, such as `ABC`.
4. A configured phrase, such as `New York`.

Matching is Unicode-aware and case-insensitive.

- Hashtags match the complete hashtag.
- Single keywords match whole alphanumeric terms, with punctuation or text edges as boundaries.
- Phrases match complete consecutive words separated only by whitespace.
- `abc` matches `ABC` and `(ABC)-`, but not `ABC123` or `a bc`.
- A message without text or a caption is not routed by text rules, but remains eligible for a
  direct topic forwarding pair.

### Routing Precedence

1. Direct topic forwarding takes priority over all text routes.
2. Explicit hashtag routes take priority over keyword and phrase routes.
3. A single matched destination is copied to that destination.
4. Several matching rules for the same destination still produce only one copy.
5. Matches for different destinations are considered a conflict.
6. A conflict is not copied automatically. Miki records it and reports it to an authorized user.
7. Route configuration order must not silently decide a conflict.
8. Unknown hashtags do not block a valid configured keyword match.

This conservative conflict policy prevents accidental duplication and misfiling.

### Keyword Management Command Contract

The command names below define the intended interface; implementation belongs to later jobs.

```text
/keyword_add <topic_id> <keyword or quoted phrase>
/keyword_remove <topic_id> <keyword or quoted phrase>
/keyword_list [topic_id]
/keyword_find <keyword or quoted phrase>
```

Hashtag mappings use equivalent commands:

```text
/hashtag_add <topic_id> <hashtag>
/hashtag_remove <topic_id> <hashtag>
/hashtag_list [topic_id]
```

Only Miki administrators or explicitly delegated route managers may modify mappings. Commands use
numeric topic IDs as the durable identifier. Human-readable topic names are display labels only.

## J04 - Request Syntax

Retrieval requests are accepted only in configured request topics.

Canonical form:

```text
#request
topic: <topic ID or unique registered topic name>
keywords: <token or quoted phrase>[, <token or quoted phrase>...]
match: all
limit: 20
```

Rules:

- `#request`, `topic`, and `keywords` are required.
- `match` is optional and is either `all` or `any`; the default is `all`.
- `limit` is optional and uses an administrator-configured default and maximum.
- Field names and matching are case-insensitive.
- Unknown fields are rejected instead of silently ignored.
- A registered topic name must resolve to exactly one topic.
- Results are ordered newest first.
- The same logical album appears once in search results and is delivered as one grouped result.
- The request message's own topic is the delivery destination.
- Invalid requests receive a concise validation response and cause no copies.
- No-result requests receive a summary and cause no copies.

Example:

```text
#request
topic: Japan
keywords: TOKYO, "Mount Fuji"
match: any
limit: 10
```

## J05 - Search-Token Extraction

The searchable library stores Telegram references and compact derived tokens, not media bytes.

Always retain:

- Hashtags without the leading `#`
- ALL-CAPS words containing at least two letters
- Capitalized words that are not retained only because they start a sentence
- Mixed letter-number codes such as `RX7`, `A320`, and `FC-2`
- Explicitly configured routing keywords and phrases found in the caption

Normalization:

- Preserve the original display value separately when useful.
- Search using Unicode `casefold`.
- Remove surrounding punctuation.
- Deduplicate normalized values.
- Match configured keywords within individual tokens.
- Keep phrase token order.
- Record an `extractor_version` with every indexed post.

Do not infer names with an AI model in the first implementation. The deterministic extractor is
cheaper, testable, and consistent. Explicit configured keywords override capitalization heuristics,
so a lowercase routing keyword such as `abc` remains searchable.

## J06 - Acceptance Scenarios

### Sorting

1. Media captioned `Trip to Tokyo #Japan` routes to the registered `#Japan` topic.
2. Media captioned `new ABC release` routes to the topic mapped to keyword `abc`.
3. Keyword `abc` matches `ABC` surrounded by punctuation, but not identifiers such as `abcdef` or `ABC123`.
4. Phrase `New York` matches `NEW YORK`, but not `new project in York`.
5. A hashtag and keyword targeting the same topic create one copy.
6. Matches targeting two different topics create no copy and record a conflict.
7. An unknown hashtag plus one valid keyword routes using the valid keyword.
8. Plain text without media is not sorted.
9. Media outside the legacy intake topic is sorted only when its source topic has a direct
   forwarding pair.
10. Reprocessing the same Telegram update does not create another copy.
11. Miki does not recursively sort a copy that Miki created.
12. Every item in one album reaches the same destination in its original order.
13. Captionless attachments in directly forwarded topics reach their configured destination.
14. Multiple source topics may forward to the same archive topic.

### Keyword Administration

1. An authorized manager can add `abc` to topic X.
2. A later media caption containing token `ABC` routes to topic X.
3. Adding the same normalized mapping twice is idempotent.
4. An unauthorized user cannot add, replace, or remove a mapping.
5. Mapping one keyword to two topics is rejected unless the conflict is explicitly resolved.
6. Removing a mapping affects future routing but does not delete existing library records.

### Retrieval

1. A valid `#request` returns posts from only the requested source topic.
2. `match: all` requires every requested search term.
3. `match: any` requires at least one requested search term.
4. Results are newest first and respect the configured limit.
5. Duplicate records and album members are not delivered twice.
6. An invalid or ambiguous topic produces an error and no copies.
7. An unavailable source post is skipped and counted in the final summary.
8. Reprocessing the request does not duplicate an already completed delivery.

## Decisions Deferred to Later Jobs

- Database engine and detailed schema
- Exact administrator/delegated-manager storage
- Queue implementation
- Default and maximum retrieval limits
- Conflict notification destination
- Data Collector's role after Miki gains its local library
- Whether edited captions may automatically move an already sorted post

## Phase 1 Completion Rule

Feature implementation must follow this specification unless a later reviewed job explicitly
amends it. Any changed behavior must update this document and its acceptance scenarios first.
