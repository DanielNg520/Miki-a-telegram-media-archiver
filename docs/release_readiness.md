# Release Readiness

## Automated Gate

A release candidate must pass:

```bash
python -m pytest -q
python -m compileall -q miki_sorter_bot tests
python -m pip check
```

The test suite validates fresh installation, every historical migration boundary,
sorting, indexing, retrieval, integrations, restart recovery, maintenance, metrics,
backup, restore, and database integrity.

## Operational Gate

Before enabling production traffic:

1. Create and verify a backup.
2. Start with `SORT_DRY_RUN=true`.
3. Confirm `/health` reports database and Telegram connectivity as healthy.
4. Confirm `/status` has no unexpected running jobs or dead letters.
5. Complete the bounded pilot in `phase_10_pilot.md`.
6. Review pilot evidence before widening topic or route coverage.

## Known Boundary

Telegram copying is an external side effect. A process termination after Telegram
accepts a copy but before SQLite records it can cause a duplicate when retried.
Miki minimizes this window by persisting intent before copying and recording the
result immediately afterward, but Telegram does not provide a transactional copy
and database commit. Pilot monitoring must therefore include duplicate checks.
