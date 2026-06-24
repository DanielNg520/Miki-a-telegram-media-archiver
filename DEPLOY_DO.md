# Deploy Plan — miki_a_friendly_sorter_bot on DigitalOcean

Brief plan for running miki on the **shared $4 droplet** alongside `PresenseObserver`.
miki is a tiny stateless Python container — it rides along on the box for $0 extra.

## Choosing a run mode
Two supported modes (see `.env.sample` → `RUN_MODE`):

- **`polling`** — simplest. No inbound port, no proxy, no TLS, no DNS. The bot dials
  out to Telegram, works behind any firewall. Good default for a single VPS.
- **`webhook`** — Telegram POSTs updates to a public HTTPS URL. Lower latency and no
  long-poll loop, but needs a domain, a TLS-terminating reverse proxy, and an open 443.

Both are first-class in the code (`_run_application` in `miki_sorter_bot/main.py`).
Pick polling unless you specifically want webhooks — the **webhook walkthrough is below**.
The `render.yaml` also uses webhook (Render is HTTPS-only); its env is a useful reference.

## Prereqs (already done by Observer's DEPLOY_DO.md)
The droplet setup — 2 GB swap, Docker + Compose, UFW — is shared. Do it once (see
`PresenseObserver/DEPLOY_DO.md`); don't repeat it here.

## Deploy steps
1. `git clone` this repo to `/opt/miki` (or `scp` it up).
2. Copy `.env.sample` → `.env` and fill in:
   - `BOT_TOKEN`, `SOURCE_CHAT_ID`, `ARCHIVE_CHAT_ID`, `ADMIN_USER_IDS`
   - `RUN_MODE=polling`
   - `SORT_DRY_RUN=true` for the first run to verify behavior safely, then flip to `false`
3. Persist the SQLite DB + backups on the host so they survive redeploys:
   - `DATABASE_PATH=var/miki.sqlite3`, `BACKUP_DIRECTORY=var/backups`
   - mount `./var:/app/var` as a volume
4. Build & run (the repo already has a `Dockerfile`):
   ```bash
   docker build -t miki .
   docker run -d --name miki --restart unless-stopped \
     --env-file .env --memory=150m \
     -v "$PWD/var:/app/var" miki
   ```
   …or fold it into the shared `docker-compose.yml` as a `miki` service.

## Recommended: one shared compose file
Cleaner than separate `docker run`s. Add miki as a service next to Observer's:
```yaml
  miki:
    build: ../miki_a_friendly_sorter_bot   # adjust path
    restart: unless-stopped
    env_file: ../miki_a_friendly_sorter_bot/.env
    volumes:
      - ../miki_a_friendly_sorter_bot/var:/app/var
    deploy:
      resources:
        limits:
          memory: 150M
```

## Footprint
- ~100 MB idle RAM, ~150 MB cap. Comfortable within the 512 MB + 2 GB swap budget.
- Daily verified backups already built in (`BACKUP_DAILY_ENABLED=true`, retention 14).

## Sanity checks after deploy
```bash
docker logs -f miki            # watch startup + polling
# inside container or via console scripts:
miki-doctor                    # diagnostics
miki-show-ids                  # confirm chat/thread IDs
```
Keep `SORT_DRY_RUN=true` until logs show it picking up the right messages, then set `false`.

---

## Webhook deployment (alternative to polling)

`RUN_MODE=webhook` starts PTB's **internal plain-HTTP server** on
`WEBHOOK_LISTEN:WEBHOOK_PORT` at `WEBHOOK_PATH`, and registers `WEBHOOK_URL` with
Telegram. It does **not** terminate TLS itself, so you need a reverse proxy in front:

```
Telegram ──HTTPS:443──> [Caddy/nginx, terminates TLS] ──HTTP:8080──> miki container
```

No IPv6 required — Telegram delivers webhooks fine over IPv4.

### 1. DNS
Point a subdomain at the droplet's IPv4: `A  miki.yourdomain.com -> <droplet_ipv4>`.

### 2. `.env`
```bash
RUN_MODE=webhook
WEBHOOK_URL=https://miki.yourdomain.com/telegram/webhook   # full public URL, incl. path
WEBHOOK_LISTEN=0.0.0.0
WEBHOOK_PORT=8080
WEBHOOK_PATH=/telegram/webhook                             # must match the URL's path
WEBHOOK_SECRET_TOKEN=<openssl rand -hex 32>                # Telegram-only request check
WEBHOOK_MAX_CONNECTIONS=40
```
Config validation requires `WEBHOOK_URL` when `RUN_MODE=webhook`, so it fails fast if missing.

### 3. Run the container — publish to localhost only
The proxy is the only thing that should reach the bot port; never expose 8080 publicly.
```bash
docker run -d --name miki --restart unless-stopped \
  --env-file .env --memory=150m \
  -p 127.0.0.1:8080:8080 \
  -v "$PWD/var:/app/var" miki
```

### 4. Reverse proxy with auto-HTTPS
**Caddy (simplest)** — see `deploy/Caddyfile` in this repo. Drop it in `/etc/caddy/Caddyfile`
(edit the domain), then `systemctl reload caddy`. TLS is issued automatically.

**Reusing Observer's nginx** — add a server block for the subdomain with
`proxy_pass http://127.0.0.1:8080;`, then `certbot --nginx -d miki.yourdomain.com`.

### 5. Firewall
```bash
ufw allow 80     # cert issuance / redirect
ufw allow 443    # webhook traffic
# do NOT open 8080
```

### 6. Verify
```bash
docker logs -f miki     # expect "running in webhook mode"
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```
Want: your URL set, low `pending_update_count`, and **no** `last_error_message`.

### Gotchas
- One webhook URL per bot token; webhook and `getUpdates` polling are mutually exclusive.
  Switching to `setWebhook` auto-disables polling. Keep exactly one container running
  (the single-instance lock helps here).
- `WEBHOOK_URL` (public) and `WEBHOOK_PATH` (internal) must share the same path.
- `WEBHOOK_SECRET_TOKEN` rejects scanners that hit the path without Telegram's header.
