# Hosted Webhook Deployment

Miki can run as a webhook web service on Render or Koyeb. Both platforms provide a public HTTPS
route and a `PORT` environment variable; Miki binds to `0.0.0.0:$PORT` when `WEBHOOK_PORT` is not
set.

## Required environment

Set these on the host:

```env
BOT_TOKEN=...
SOURCE_CHAT_ID=...
SOURCE_THREAD_ID=...
ARCHIVE_CHAT_ID=...
ADMIN_USER_IDS=...
RUN_MODE=webhook
WEBHOOK_URL=https://your-service.example.com/telegram/webhook
WEBHOOK_PATH=/telegram/webhook
WEBHOOK_LISTEN=0.0.0.0
TELEGRAM_BOOTSTRAP_RETRIES=-1
TELEGRAM_DROP_PENDING_UPDATES=false
```

Optional hardening:

```env
WEBHOOK_SECRET_TOKEN=long-random-secret
WEBHOOK_MAX_CONNECTIONS=40
TELEGRAM_STARTUP_CHECKIN_ENABLED=true
TELEGRAM_NOTIFICATION_CHAT_IDS=123456789
SANITY_CHECK_ENABLED=true
SOURCE_ACTIVITY_CHECK_ENABLED=true
ERROR_REPORTING_DSN=
```

## Webhook self-healing

In webhook mode Miki supervises its own Telegram registration so it stays healthy
without manual intervention. A background reconcile loop runs every
`WEBHOOK_RECONCILE_INTERVAL_SECONDS` (default 120s):

1. **Observe** — reads Telegram's live `getWebhookInfo`.
2. **Detect drift** — registered URL missing/mismatched, a pending-update backlog
   (`WEBHOOK_PENDING_ALERT_THRESHOLD`), recent delivery errors, or a stale liveness
   heartbeat corroborated by a Telegram-side symptom.
3. **Heal** — re-runs `setWebhook` toward the desired state. A circuit breaker
   (`WEBHOOK_HEAL_FAILURE_THRESHOLD` / `WEBHOOK_HEAL_RESET_SECONDS`) prevents flapping
   when the underlying cause (DNS, cert, routing) is not yet fixed.

This recovers automatically from the common webhook failure modes — a cert renewal,
a reverse-proxy restart, brief downtime, or another process overwriting the webhook —
which would otherwise silently stop delivery until someone re-ran `setWebhook`.

Tuning knobs (all optional, sane defaults):

```env
WEBHOOK_RECONCILE_ENABLED=true
WEBHOOK_RECONCILE_INTERVAL_SECONDS=120
WEBHOOK_STALE_AFTER_SECONDS=900
WEBHOOK_PENDING_ALERT_THRESHOLD=50
WEBHOOK_HEAL_FAILURE_THRESHOLD=3
WEBHOOK_HEAL_RESET_SECONDS=300
```

Observe it via `/doctor` and `/status` in Telegram (both print a "Webhook supervision"
section) or the `/metrics` endpoint (`miki_webhook_*` gauges). `/healthz` reports the
process as unhealthy **only when the webhook is confidently wedged** (breaker open and
updates stale), which a container `HEALTHCHECK` + `restart: unless-stopped` uses as a
last-resort restart trigger without false positives during quiet periods.

To send exceptions to Sentry-compatible reporting, install with:

```bash
pip install ".[monitoring]"
```

Do not set both polling and webhook services for the same bot token. Telegram can deliver updates to
only one active consumer.

SQLite needs persistent storage. On hosts with ephemeral filesystems, mount a persistent disk and set:

```env
DATABASE_PATH=/data/miki.sqlite3
BACKUP_DIRECTORY=/data/backups
```

## Render

Use the included `render.yaml` as a starter blueprint. Render web services must bind to
`0.0.0.0` and the host-provided `PORT`; Miki accepts that automatically.

Render free web services can spin down after inactivity. Telegram will wake the service with the
next webhook request, but delivery may be delayed while the service starts.

## Koyeb

Use the included `Dockerfile`. Create a Web Service, expose the HTTP port Koyeb provides through
`PORT`, and set the same environment variables above.

Koyeb performs TCP health checks on exposed ports by default, so the webhook process becoming
reachable is enough for basic platform health.

## Health and Metrics

For VPS or polling deployments, enable the lightweight local HTTP helper:

```env
HEALTH_SERVER_ENABLED=true
HEALTH_LISTEN=0.0.0.0
HEALTH_PORT=8081
```

It serves:

- `/healthz` for a JSON health result.
- `/metrics` for Prometheus-style counters.

For webhook services, keep the helper on a different exposed port or leave it disabled; the webhook
server already owns the platform `PORT`.

## Future Database Scaling

Miki currently supports `DATABASE_BACKEND=sqlite`. Keep one running instance per bot token. If the
library grows large enough to need multi-instance hosting or remote managed storage, add a Postgres
repository implementation behind the existing repository interface before enabling
`DATABASE_BACKEND=postgres`.

## Self-check

After deploy, run locally or in a one-off shell:

```bash
miki-doctor
```

The same check is also available inside Telegram for admins:

```text
/doctor
```
