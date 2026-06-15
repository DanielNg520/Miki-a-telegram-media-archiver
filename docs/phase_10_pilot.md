# Phase 10: End-to-End Validation and Pilot

## Automated Validation

The automated suite covers:

- sorter routing for media, albums, hashtags, keywords, phrases, conflicts, edits,
  duplicate updates, and retries;
- retrieval parsing, authorization, no-result cases, bounded large results,
  unavailable sources, cancellation, and partial retry;
- restart recovery for interrupted sorter and retrieval jobs without reopening
  completed, failed, or cancelled work;
- cooperation safeguards for duplicate work, Miki-authored loops, malformed
  integration payloads, signatures, replay protection, scopes, and quotas.

## Limited Pilot Scope

Use exactly one intake topic, one request topic, and two or three unambiguous
routes. Set `DEFAULT_REQUEST_LIMIT` to 5 and `MAX_REQUEST_LIMIT` to 10. Keep
`SORT_DRY_RUN=true` for the first observation period, inspect `/route_explain`
and `/status`, then enable copying only for the pilot topics.

Do not enable the whole supergroup during the pilot.

## Pilot Script

1. Create a verified `/backup` and record `/health` and `/status`.
2. Submit one photo, one video, and one two-item album for each pilot route.
3. Submit one unmatched caption and one deliberate cross-topic conflict.
4. Edit a previously sorted caption and confirm no duplicate copy is created.
5. Submit valid, invalid, empty-result, and unavailable-source requests.
6. Restart Miki while one controlled request is processing and confirm recovery.
7. Send a signed integration preview and search, then test bad authentication,
   replay, and quota rejection.
8. Record delivery counts, retries, dead letters, latency, and any duplicates.

## Evidence Review

Record the date, configuration, message IDs, expected and actual destinations,
request summaries, `/status` before and after, dead letters, and operator notes.
J59 is complete only after the limited live run. J60 is complete only after all
unexpected results are either fixed and retested or explicitly accepted.
