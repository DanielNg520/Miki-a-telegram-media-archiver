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
JOB_RECOVERY_INTERVAL_SECONDS=60
JOB_RECOVERY_BATCH_SIZE=100
```

Transient failures use bounded exponential backoff with jitter. Telegram rate-limit responses use
the server-provided delay. Permanent failures are attempted once.

Telegram send/copy timeouts are treated differently from ordinary connection failures: the server
may have accepted the media even though Miki never received the response. Retrying or falling back
would risk duplicate posts, so Miki records an `outcome_unknown` dead letter after one attempt and
suppresses automatic replay. Rate-limit responses remain safe to retry because Telegram explicitly
rejected the attempt.

## Dead Letters

Terminal non-source failures create durable dead-letter records containing job identity, operation,
bounded payload metadata, category, and error message.

Administrators can inspect and requeue them:

```text
/dead_letters
/dead_letter_retry <dead_letter_id>
```

Requeueing returns the associated job to `pending` and immediately asks the recovery coordinator
to resume it. A periodic bounded sweep provides a second path if the immediate attempt is
interrupted. Existing delivery or retrieval-item records continue to provide duplicate protection.
Completing a recovered job automatically resolves its remaining dead letters.

## Recovery and Shutdown

At startup, jobs left `running` by an interrupted process return to `pending`. A strategy-based
recovery coordinator dispatches sorting and retrieval jobs from durable payloads. Workers claim jobs
with one atomic SQLite transition, preventing two concurrent update/recovery paths from performing
the same delivery. The coordinator limits each pass to `JOB_RECOVERY_BATCH_SIZE` and repeats every
`JOB_RECOVERY_INTERVAL_SECONDS`.

During normal Telegram application shutdown, album timers are cancelled and routable buffered
albums are drained before Telegram and SQLite shut down. If termination interrupts that process,
startup recovery and idempotent delivery/item state resume it without replaying completed copies.

## Unavailable Posts

When Telegram reports that an indexed source cannot be copied, Miki:

1. Marks the indexed post unavailable.
2. Marks the retrieval item skipped.
3. Counts it as unavailable in the request summary.

Future searches exclude the unavailable record. Other permanent failures remain visible in the
dead-letter queue for diagnosis.
