# Deployment

Covers generic install, the two run modes, the DigitalOcean droplet (webhook) and Render/Koyeb
targets, operations, backup/restore, upgrade/rollback, and the go-live checklist. For configuration
keys see the [README](../README.md); for internals see [architecture.md](architecture.md).

## Prerequisites

1. Install the supported Python and project dependencies in a virtualenv.
2. Create `.env` from `.env.sample`. Keep `BOT_TOKEN` and integration secrets out of source control.
3. Grant Miki admin in the forum supergroup(s) — at least read and copy/send.
4. Put `DATABASE_PATH` and `BACKUP_DIRECTORY` on persistent storage writable only by the service
   account. Keep exactly one running instance per bot token (Telegram delivers updates to one
   consumer only).

## Run modes

- **`RUN_MODE=polling`** (default) — for an always-on machine or VPS. No inbound port, proxy, or
  TLS; the bot dials out to Telegram. Album members arrive batched per `getUpdates` pull.
- **`RUN_MODE=webhook`** — for HTTPS hosts (DO droplet behind a proxy, Render, Koyeb). Telegram
  pushes each update to Miki's public URL; lower latency. Webhook delivers album members as
  separate staggered POSTs, so a longer `ALBUM_FLUSH_DELAY_SECONDS` lets every member join the group
  before it flushes.

Start with a process supervisor (auto-restart with delay, log capture):

```bash
python -m miki_sorter_bot.main   # or: miki-sorter
```

After startup run `/health` and `/status` (or `miki-doctor` locally).

## DigitalOcean droplet (webhook, recommended)

Runs on a shared $4 droplet alongside other tiny services — Miki is a stateless ~100 MB container.
The droplet base setup (2 GB swap, Docker + Compose, UFW) is shared and done once elsewhere; don't
repeat it here.

1. `git clone` to `/opt/miki` (or `scp` it up).
2. Copy `.env.sample` → `.env` and fill in `BOT_TOKEN`, `SOURCE_CHAT_ID`, `ARCHIVE_CHAT_ID`,
   `ADMIN_USER_IDS`, `RUN_MODE=webhook`, the `WEBHOOK_*` / `HEALTH_*` keys, and
   `SORT_DRY_RUN=true` for the first run.
3. Persist the DB + backups on the host: `DATABASE_PATH=var/miki.sqlite3`,
   `BACKUP_DIRECTORY=var/backups`. The bundled `docker-compose.yml` mounts `./var:/app/var`.
4. Stand up the [reverse proxy + TLS](#reverse-proxy--tls) for `miki.<domain>`.
5. Build and run:
   ```bash
   cd /opt/miki
   docker compose up -d --build      # publishes 127.0.0.1:8080, mounts ./var, sets mem cap + restart
   docker compose logs -f            # expect: "running in webhook mode"
   ```

> ⚠️ `env_file` does not strip inline comments. In `.env` never write `KEY=value  # note` — Docker
> keeps the comment as part of the value. Put comments on their own lines.

### `.env` for webhook mode

```env
RUN_MODE=webhook
WEBHOOK_URL=https://miki.your-domain.com/telegram/webhook
WEBHOOK_PATH=/telegram/webhook
WEBHOOK_SECRET_TOKEN=long-random-secret
WEBHOOK_RECONCILE_ENABLED=true
HEALTH_SERVER_ENABLED=true
HEALTH_PORT=8081
# Album reassembly: a longer debounce lets every staggered album member arrive before flush.
ALBUM_FLUSH_DELAY_SECONDS=10
ALBUM_MAX_WAIT_SECONDS=60
```

### Reverse proxy + TLS

**Caddy** (automatic Let's Encrypt) in `/etc/caddy/Caddyfile`, then `systemctl reload caddy`:

```caddy
miki.your-domain.com {
    reverse_proxy 127.0.0.1:8080
}
```

(nginx works too: a `listen 443 ssl` server with `location /telegram/webhook { proxy_pass
http://127.0.0.1:8080; }` and a certbot cert — you manage the cert yourself.)

### Verify

```bash
docker compose port miki 8080                              # expect 127.0.0.1:8080
curl -sI http://127.0.0.1:8080/telegram/webhook            # 405 = server reachable (good)
TOKEN=$(grep -E '^BOT_TOKEN=' .env | cut -d= -f2- | tr -d "\"'")
curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" | python3 -m json.tool
docker compose exec miki miki-doctor                       # diagnostics
docker compose exec miki miki-show-ids                     # confirm chat/thread IDs
```

Want `pending_update_count` near 0 and no fresh `last_error_message`. Keep `SORT_DRY_RUN=true` until
logs show it picking up the right messages, then set `false` and `docker compose up -d`.

### Redeploying an update

```bash
cd /opt/miki && git pull && docker compose up -d --build && docker compose logs -f
```

The `./var` volume (DB + backups) is untouched by the rebuild. Migrations run automatically on
start. No `.env` change is needed unless a release adds a new required key.

## Render / Koyeb (hosted webhook)

Both provide a public HTTPS route and a `PORT`; Miki binds `0.0.0.0:$PORT` when `WEBHOOK_PORT` is
unset. Required env:

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

Optional hardening: `WEBHOOK_SECRET_TOKEN`, `WEBHOOK_MAX_CONNECTIONS=40`,
`TELEGRAM_STARTUP_CHECKIN_ENABLED=true`, `TELEGRAM_NOTIFICATION_CHAT_IDS`, `SANITY_CHECK_ENABLED`,
`SOURCE_ACTIVITY_CHECK_ENABLED`, `ERROR_REPORTING_DSN`.

- **Render** — use the included `render.yaml` as a starter; free web services may spin down on
  inactivity and wake on the next webhook (delivery briefly delayed).
- **Koyeb** — use the included `Dockerfile`; expose the `PORT` it provides. Koyeb's default TCP
  health check is satisfied once the webhook process is reachable.

SQLite needs persistent storage. On ephemeral filesystems mount a disk and set
`DATABASE_PATH=/data/miki.sqlite3`, `BACKUP_DIRECTORY=/data/backups`. To send exceptions to
Sentry-compatible reporting, install with `pip install ".[monitoring]"`.

## Webhook self-healing

In webhook mode Miki supervises its own Telegram registration. A reconcile loop runs every
`WEBHOOK_RECONCILE_INTERVAL_SECONDS` (default 120s):

1. **Observe** live `getWebhookInfo`.
2. **Detect drift** — registered URL missing/mismatched, recent delivery errors, or a stale liveness
   heartbeat corroborated by a Telegram-side symptom. A pending-update backlog is deliberately *not*
   a trigger (re-registering doesn't drain a backlog, it re-floods it).
3. **Heal, then confirm** — re-run `setWebhook`, then next tick check the drift cleared. If it did
   not (e.g. a misconfigured proxy), that's a circuit-breaker failure
   (`WEBHOOK_HEAL_FAILURE_THRESHOLD` / `WEBHOOK_HEAL_RESET_SECONDS`) so ineffective heals back off
   instead of re-registering every tick.

This recovers from cert renewals, proxy restarts, brief downtime, or another process overwriting the
webhook. Observe via `/doctor` and `/status` (both print a "Webhook supervision" section) or the
`/metrics` `miki_webhook_*` gauges. `/healthz` reports unhealthy **only when confidently wedged**
(breaker open and updates stale), which a container `HEALTHCHECK` + `restart: unless-stopped` uses
as a last-resort restart trigger without false positives during quiet periods. Tuning knobs (all
optional): `WEBHOOK_RECONCILE_ENABLED`, `WEBHOOK_RECONCILE_INTERVAL_SECONDS`,
`WEBHOOK_STALE_AFTER_SECONDS`, `WEBHOOK_HEAL_FAILURE_THRESHOLD`, `WEBHOOK_HEAL_RESET_SECONDS`.

## Health & metrics

For VPS/polling deployments enable the lightweight HTTP helper:

```env
HEALTH_SERVER_ENABLED=true
HEALTH_LISTEN=0.0.0.0
HEALTH_PORT=8081
```

It serves `/healthz` (JSON health) and `/metrics` (Prometheus-style counters). The health worker
opens an isolated read-only SQLite connection per probe, keeping threaded probes off the event-loop
delivery connection. For webhook services keep the helper on a different port or disabled — the
webhook server already owns the platform `PORT`.

Admin commands: `/health` (SQLite integrity, FK enforcement, Telegram connectivity), `/status`
(library size, unavailable posts, queue states, dead letters, retries/throttles, average delivery
time), `/maintenance` (prune expired transient records + old audit events, then index optimization),
`/backup` (verified snapshot). Local equivalents: `miki-doctor`, and the `miki-ops` console
(`health`, `watch`, `status`, `doctor`, `backup`, `maintenance`, `logrotate`, `install`, `load`,
`restart`).

## Backup & restore

A **daily automatic backup** runs in-process via the bot's job queue (no external cron). Each run
takes a verified online SQLite snapshot (consistent under WAL, integrity-checked) into
`BACKUP_DIRECTORY`, then prunes all but the `BACKUP_RETENTION_COUNT` most recent. Configure with
`BACKUP_DAILY_ENABLED`, `BACKUP_TIME` (24h `HH:MM`, UTC), `BACKUP_RETENTION_COUNT`. A failed run is
logged and counted in `database_backup_failures` without interrupting the bot. Retention keys:
`TRANSIENT_RETENTION_DAYS` (default 30), `AUDIT_RETENTION_DAYS` (default 90).

**Restore drill:** stop Miki, verify the chosen backup, restore it to a temporary path, verify the
restored DB, replace the primary, and restart. `Storage.restore_backup` performs the temporary
restore and both integrity checks. Keep the previous primary until startup health passes. Never
copy a live WAL database with ordinary file-copy commands — use this procedure.

**Off-droplet backups (optional burner layer):** if the burner is configured, `miki-burner backup`
pushes an encrypted, compressed snapshot into the archive group so the index survives loss of the
droplet itself. It runs as an on-demand CLI (cron), separate from the bot process. Setup, the cron
schedule, and the user-account restore runbook are in [burner-layer.md](burner-layer.md).

## Upgrade & rollback

**Upgrade:** create and verify a backup → stop the process → install the new release + deps → start
(migrations run automatically) → run the test suite, `/health`, and a limited sort/retrieve smoke
test.

**Rollback:** stop Miki → restore the pre-upgrade backup → deploy the previous release → restart →
confirm `/health`. SQLite migrations are forward-only, so rollback is always restore-plus-previous-
version, never reverse SQL on live data.

## Release gate

A release candidate must pass:

```bash
python -m pytest -q
python -m compileall -q miki_sorter_bot tests
python -m pip check
```

(`make verify` wraps these plus `miki-doctor`.) The suite validates fresh installation, every
historical migration boundary, sorting, indexing, retrieval, integrations, restart recovery,
maintenance, metrics, backup, restore, and database integrity.

## Go-live checklist

1. Create and verify a `/backup`; record `/health` and `/status`.
2. Start with `SORT_DRY_RUN=true`; confirm `/health` is green and `/status` has no unexpected
   running jobs or dead letters.
3. Pilot small: one intake topic, one request topic, two or three unambiguous routes,
   `DEFAULT_REQUEST_LIMIT=5`, `MAX_REQUEST_LIMIT=10`. Do not enable the whole supergroup yet.
4. With dry-run on, submit a photo, a video, and a small album per route; one unmatched caption and
   one deliberate cross-topic conflict; inspect `/route_explain` and `/status`.
5. Turn copying on for the pilot topics. Edit a previously sorted caption and confirm no duplicate.
   Submit valid, invalid, empty-result, and unavailable-source requests.
6. Restart Miki while a request is processing and confirm recovery resumes without duplicating
   completed work.
7. Review delivery counts, retries, dead letters, latency, and any duplicates before widening
   coverage. Because Telegram copying is a non-transactional external side effect, a crash between a
   successful copy and the SQLite commit can duplicate on retry — monitoring must include duplicate
   checks.
