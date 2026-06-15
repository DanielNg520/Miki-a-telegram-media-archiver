# Phase 6: Retrieval

Phase 6 lets users search Miki's indexed library from configured request topics and copy matching
posts back into the request topic.

## Request Form

```text
#request
topic: <archive topic ID or unique registered name>
keywords: <token or quoted phrase>[, <token or quoted phrase>...]
match: all
limit: 20
```

`topic` and `keywords` are required. `match` defaults to `all`; `limit` defaults to
`DEFAULT_REQUEST_LIMIT` and cannot exceed `MAX_REQUEST_LIMIT`. Unknown, duplicate, malformed, or
missing fields are rejected without creating a job.

Hashtag searches may include or omit `#`. Matching is exact after Unicode case-folding. `all`
requires every requested value; `any` requires at least one.

## Authorization

Requests are accepted only when both are true:

- The chat is `ARCHIVE_CHAT_ID`.
- The current thread is listed in `REQUEST_TOPIC_IDS`.

Human users in those topics may request retrieval. Bot users must also appear in
`REQUESTER_BOT_IDS`. This prevents an unrelated bot or a same-numbered topic in another chat from
triggering retrieval.

## Search Semantics

Search is restricted to the requested active registered archive topic. Results are ordered newest
first and `limit` counts logical posts, not physical album members.

An album is one logical search result. Tokens are aggregated across every member for `match: all`.
After selection, all available members are copied in ascending Telegram message order.

## Durable Execution

Every request creates an idempotent retrieval job keyed by request chat and message ID. Each result
member has a durable retrieval-item record keyed by job and indexed post.

Successful members are not copied again if the same request resumes. Failed members can be retried
without replaying successful members. Separate request messages remain independent, even in the
same destination topic.

Under the Telegram application, execution runs as an application-managed background task so an
administrator can cancel between result copies:

```text
/request_cancel <job_id>
```

## Summary

Miki replies when the job is queued and sends a final summary containing:

- Logical matches
- Physical messages copied
- Unavailable records
- Already completed/skipped members
- Failed copies
- Cancellation state

Detailed retry classification and automatic unavailable-message reconciliation belong to Phase 7.
