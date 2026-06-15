# Phase 5: Durable Sorting

Phase 5 replaces the legacy collector-confirmed sorter with registered hashtag, keyword, and phrase
mappings from Phase 3.

## Eligibility

Miki sorts only messages that:

- Arrive in `SOURCE_CHAT_ID`
- Arrive in `SOURCE_THREAD_ID`
- Belong to a group or supergroup
- Contain supported media
- Include a caption/text, or belong to an album whose first member already established a decision
- Were not authored by Miki itself

Messages from other bots remain eligible so Miki can cooperate with approved programs. Miki's own
messages are rejected at the input boundary to prevent loops.

## Routing

Matching is Unicode-aware and case-insensitive:

- Hashtags and keywords require exact tokens.
- Phrases require exact consecutive normalized tokens.
- Hashtags have priority over keywords and phrases.
- Several rules targeting one topic collapse into one route.
- Matches targeting different topics produce a conflict and no copy.
- Unknown hashtags do not block a valid keyword or phrase match.

Use this authorized diagnostic command without copying anything:

```text
/route_explain <caption text>
```

## Durable Delivery

Before calling Telegram, Miki:

1. Creates or retrieves an idempotent sorting job.
2. Creates or retrieves a pending delivery record.
3. Skips Telegram if that delivery is already sent or intentionally skipped.

After copying, Miki stores the destination message ID, completes the job, and passes the copy to the
Phase 4 indexer. Copy failures mark both the delivery and job failed before the exception is
propagated for later retry handling.

The delivery lineage key includes source message and destination topic identity. Replayed Telegram
updates therefore cannot create a second copy.

## Albums

The captioned album member establishes the route. Later captionless members reuse that decision
through a bounded cache and are copied in update order. Every member receives its own durable job
and delivery record, while Phase 4 gives all members one logical album identity.

## Dry Run

Set:

```text
SORT_DRY_RUN=true
```

Miki will resolve routes and record skipped deliveries without calling Telegram. Switching dry run
off does not replay those intentionally skipped records; use new test messages when validating live
delivery.

## Legacy Configuration

`COLLECTOR_*` and `ROUTES_JSON` remain accepted for compatibility but are optional and do not
control Phase 5 routing. Registered database mappings are the source of truth.
