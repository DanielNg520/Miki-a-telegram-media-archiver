# Deploy Plan — miki_a_friendly_sorter_bot on DigitalOcean

Brief plan for running miki on the **shared $4 droplet** alongside `PresenseObserver`.
miki is a tiny stateless Python container — it rides along on the box for $0 extra.

## Run mode: webhook
The droplet runs **`RUN_MODE=webhook`** behind a host reverse proxy (Caddy/nginx) that
terminates TLS and forwards inward. Webhook gives Telegram-batched album delivery and
lower latency than polling. Miki **supervises its own webhook registration**, so it stays
low-maintenance (details in [Self-healing](#self-healing)).

> Polling (`RUN_MODE=polling`) still works and needs no inbound port or proxy, but it is
> **no longer used for this droplet**. See [Alternative: polling](#alternative-polling)
> if you ever need it. The `render.yaml` is for Render only; ignore it here.

## Prereqs (already done by Observer's DEPLOY_DO.md)
The droplet setup — 2 GB swap, Docker + Compose, UFW — is shared. Do it once (see
`PresenseObserver/DEPLOY_DO.md`); don't repeat it here.

## Deploy steps
1. `git clone` this repo to `/opt/miki` (or `scp` it up).
2. Copy `.env.sample` → `.env` and fill in (see [`.env` for webhook mode](#env-for-webhook-mode)):
   - `BOT_TOKEN`, `SOURCE_CHAT_ID`, `ARCHIVE_CHAT_ID`, `ADMIN_USER_IDS`
   - `RUN_MODE=webhook` plus the `WEBHOOK_*` / `HEALTH_*` keys
   - `SORT_DRY_RUN=true` for the first run to verify behavior safely, then flip to `false`
3. Persist the SQLite DB + backups on the host so they survive redeploys:
   - `DATABASE_PATH=var/miki.sqlite3`, `BACKUP_DIRECTORY=var/backups`
   - the bundled `docker-compose.yml` already mounts `./var:/app/var`
4. Stand up the [reverse proxy + TLS](#reverse-proxy--tls) for `miki.<domain>`.
5. Build & run with the bundled compose file (publishes `127.0.0.1:8080`, mounts `./var`,
   sets the memory cap and restart policy):
   ```bash
   cd /opt/miki
   docker compose up -d --build
   docker compose logs -f
   ```

## Redeploying an update
```bash
cd /opt/miki
git pull
docker compose up -d --build      # rebuilds the image and recreates the container
docker compose logs -f            # expect: "running in webhook mode"
```
The `./var` volume (DB + backups) is untouched by the rebuild.

## Footprint
- ~100 MB idle RAM, ~150 MB cap. Comfortable within the 512 MB + 2 GB swap budget.
- Daily verified backups already built in (`BACKUP_DAILY_ENABLED=true`, retention 14).

## Sanity checks after deploy
```bash
docker compose logs -f                 # watch startup + webhook registration
docker compose exec miki miki-doctor   # diagnostics
docker compose exec miki miki-show-ids # confirm chat/thread IDs
```
Keep `SORT_DRY_RUN=true` until logs show it picking up the right messages, then set `false`.

## Self-healing
Miki supervises its own webhook registration so it stays low-maintenance:

- A reconcile loop (every `WEBHOOK_RECONCILE_INTERVAL_SECONDS`, default 120s) compares
  Telegram's live `getWebhookInfo` against the desired registration and re-runs
  `setWebhook` when the URL is lost/wrong or Telegram reports delivery errors —
  recovering from cert blips, proxy restarts, or a dropped webhook on its own.
- A circuit breaker **backs off** if a re-registration doesn't actually fix the drift
  (e.g. the proxy is misconfigured), so it can never spin "self-healing" every tick.
- The container `HEALTHCHECK` is the last-resort backstop: it probes `/healthz` and,
  with `--restart unless-stopped`, restarts only a *confidently wedged* process
  (breaker open **and** updates stale) — a quiet source never triggers a restart loop.

> ⚠️ **`env_file` does not strip inline comments.** In `.env`, never write
> `KEY=value   # note` — Docker keeps `value   # note` as the literal value and Miki
> will fail to start. Put comments on their own lines.

### `.env` for webhook mode
```env
RUN_MODE=webhook
WEBHOOK_URL=https://miki.your-domain.com/telegram/webhook
WEBHOOK_PATH=/telegram/webhook
WEBHOOK_SECRET_TOKEN=long-random-secret
WEBHOOK_RECONCILE_ENABLED=true
HEALTH_SERVER_ENABLED=true
HEALTH_PORT=8081
# Album reassembly (webhook delivers album members as separate, staggered POSTs;
# a longer debounce lets every member join the group before it flushes).
ALBUM_FLUSH_DELAY_SECONDS=10
ALBUM_MAX_WAIT_SECONDS=60
```

The bundled `docker-compose.yml` publishes the webhook port to `127.0.0.1:8080` (the
proxy terminates TLS and forwards inward — never the public interface), mounts `./var`,
and applies the memory cap + restart policy. After `docker compose up -d --build`:
```bash
docker compose port miki 8080   # expect: 127.0.0.1:8080
```

### Reverse proxy + TLS
**Caddy** (recommended — automatic Let's Encrypt, whole config is two lines in
`/etc/caddy/Caddyfile`):
```caddy
miki.your-domain.com {
    reverse_proxy 127.0.0.1:8080
}
```
Then `systemctl reload caddy`. (For nginx instead, a `server { listen 443 ssl; location
/telegram/webhook { proxy_pass http://127.0.0.1:8080; } }` block with a certbot cert does
the same job, but you manage the cert yourself.)

### Verify
```bash
curl -sI http://127.0.0.1:8080/telegram/webhook         # 405 = server reachable (good)
TOKEN=$(grep -E '^BOT_TOKEN=' .env | cut -d= -f2- | tr -d "\"'")
curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" | python3 -m json.tool
```
Want `pending_update_count` near 0 and no fresh `last_error_message`. Watch self-healing
via `/doctor` or `/status` in Telegram (both print a "Webhook supervision" section), or
`curl -s localhost:8081/metrics | grep webhook`.

## Alternative: polling
Not used on this droplet, kept for reference. Polling needs no inbound port, proxy, or
TLS — the bot dials out to Telegram. To run it instead, set `RUN_MODE=polling` in `.env`,
drop the `ports:` block from `docker-compose.yml` (the webhook port is unused), and
`docker compose up -d --build`. Album members arrive batched in each `getUpdates` pull,
so the staggered-delivery race that webhook mitigates with `ALBUM_FLUSH_DELAY_SECONDS`
does not apply.
