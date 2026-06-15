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
```

Do not set both polling and webhook services for the same bot token. Telegram can deliver updates to
only one active consumer.

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

## Self-check

After deploy, run locally or in a one-off shell:

```bash
miki-doctor
```

The same check is also available inside Telegram for admins:

```text
/doctor
```
