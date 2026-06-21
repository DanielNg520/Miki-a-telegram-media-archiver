# Deploy Plan — miki_a_friendly_sorter_bot on DigitalOcean

Brief plan for running miki on the **shared $4 droplet** alongside `PresenseObserver`.
miki is a tiny stateless Python container — it rides along on the box for $0 extra.

## Key decision: polling, not webhook
On a VPS, **`RUN_MODE=polling` is the right choice** (see `.env.sample`):
- No inbound port, no Nginx route, no public webhook URL, no TLS cert for the bot.
- The bot dials out to Telegram — works behind any firewall.
- The existing `render.yaml` uses webhook because Render is HTTP-only; **ignore it here**.

> Switch to webhook only if you later want to share Observer's Nginx + 443.
> For now, polling keeps miki dead simple.

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
