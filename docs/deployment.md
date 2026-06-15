# Deployment

## Install and Configure

1. Install the supported Python version and project dependencies in a virtual
   environment.
2. Create `.env` from the documented settings. Keep bot tokens and integration
   secrets outside source control.
3. Grant Miki administrator access to the forum supergroup, including permission
   to read and copy messages.
4. Put `DATABASE_PATH` and `BACKUP_DIRECTORY` on persistent storage writable only
   by the service account.

## Start

Run `python -m miki_sorter_bot.main` under a process supervisor. Use automatic
restart with a delay, log capture, and one active instance per database.
After startup, run `/health` and `/status`.

## Upgrade

1. Create and verify a backup.
2. Stop the current process.
3. Install the new release and dependencies.
4. Start Miki; migrations run automatically.
5. Run the test suite, `/health`, and a limited sort/retrieve smoke test.

## Rollback

Stop Miki, restore the pre-upgrade backup, deploy the previous release, restart,
and confirm `/health`. Never copy a live WAL database with ordinary file-copy
commands; use Miki's SQLite backup procedure.
