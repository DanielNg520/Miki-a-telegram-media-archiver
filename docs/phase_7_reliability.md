# Phase 7: Reliability

Phase 7 gives sorting and retrieval one shared Telegram delivery policy.

## Failure Classification

Failures are classified as:

- `rate_limit`: retryable and honors Telegram's `retry_after`
- `transient`: timeouts, network failures, and temporary operating-system errors
- `permission`: permanent authorization failure
- `invalid_request`: permanent malformed destination or request
- `unavailable_source`: source message no longer exists or cannot be copied
- `unexpected`: unclassified permanent failure

Permanent Telegram errors are checked before broad network base classes because
`python-telegram-bot` models `BadRequest` as a `NetworkError` subclass.

## Retry and Rate Limits

Sorting and retrieval share one executor and therefore one process-wide output limit.

Configuration:

```text
TELEGRAM_RETRY_ATTEMPTS=3
TELEGRAM_RETRY_BASE_DELAY=0.5
TELEGRAM_RETRY_MAX_DELAY=8
TELEGRAM_MESSAGES_PER_SECOND=10
```

Transient failures use bounded exponential backoff with jitter. Telegram rate-limit responses use
the server-provided delay. Permanent failures are attempted once.

## Dead Letters

Terminal non-source failures create durable dead-letter records containing job identity, operation,
bounded payload metadata, category, and error message.

Administrators can inspect and requeue them:

```text
/dead_letters
/dead_letter_retry <dead_letter_id>
```

Requeueing returns the associated job to `pending`; its existing delivery or retrieval-item records
continue to provide duplicate protection.

## Recovery and Shutdown

At startup, jobs left `running` by an interrupted process return to `pending`. Sorting updates and
application-managed retrieval tasks retain durable state before every Telegram operation.

During normal Telegram application shutdown, managed tasks finish before the post-shutdown hook
closes SQLite. If termination interrupts that process, startup recovery and idempotent item state
resume it without replaying completed copies.

## Unavailable Posts

When Telegram reports that an indexed source cannot be copied, Miki:

1. Marks the indexed post unavailable.
2. Marks the retrieval item skipped.
3. Counts it as unavailable in the request summary.

Future searches exclude the unavailable record. Other permanent failures remain visible in the
dead-letter queue for diagnosis.
