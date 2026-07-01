"""Burner layer — Phase 3 backup offload to Telegram storage.

Pushes an encrypted, compressed copy of the SQLite index off the droplet into a
private Telegram group, so the index survives even if the droplet or the burner
account is lost. Designed to run **on demand from the CLI** (``miki-burner
backup``), typically wired to cron/systemd — it does not require an always-on
burner process.

Pipeline: consistent SQLite backup → gzip → age-encrypt → upload to the backup
group → prune local copies to a small last-N.

Security: the artifact is encrypted with **age** to a recipient
public key kept on the droplet (``BURNER_BACKUP_AGE_RECIPIENT``). The matching
private key lives OFF the droplet (with the owner) and is needed only to
restore, so a compromised droplet cannot decrypt past backups.

──────────────────────────────────────────────────────────────────────────────
RESTORE RUNBOOK (must use a *user account*, never the Miki bot — the Bot API
cannot download files larger than 20 MB):

  1. From a Telegram user account that is a member of the backup group, download
     the desired ``miki-<ts>.sqlite3.gz.age`` artifact.
  2. Decrypt with the age *private* key (kept off the droplet):
         age -d -i age-key.txt -o miki.sqlite3.gz miki-<ts>.sqlite3.gz.age
  3. Decompress:
         gunzip miki.sqlite3.gz
  4. Verify + install with the existing tooling (integrity check + schema probe):
         python -c "from miki_sorter_bot.storage import Storage; \
                    Storage.restore_backup(__import__('pathlib').Path('miki.sqlite3'), \
                    __import__('pathlib').Path('var/miki.sqlite3'))"
  5. Restart Miki. Never assume the original burner is alive — any user account
     in the group can perform steps 1–2.
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import gzip
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from miki_sorter_bot.config import Settings
from miki_sorter_bot.storage import Storage

logger = logging.getLogger(__name__)

# Telegram per-file upload limit (2 GB; 4 GB with Premium). Compressed DB is
# projected well under this for the foreseeable future; chunking is a future step.
MAX_UPLOAD_BYTES = 2 * 1024**3

ENCRYPTED_SUFFIX = ".sqlite3.gz.age"

# (data, recipient_public_key) -> ciphertext
AgeEncryptor = Callable[[bytes, str], bytes]


@dataclass(frozen=True, slots=True)
class BackupOutcome:
    artifact: Path
    size_bytes: int
    uploaded_message_id: int | None
    pruned: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.name,
            "size_bytes": self.size_bytes,
            "uploaded_message_id": self.uploaded_message_id,
            "pruned": list(self.pruned),
        }


def _age_encrypt(data: bytes, recipient: str) -> bytes:
    try:
        import pyrage
    except ImportError as error:  # pragma: no cover - exercised via injection
        raise RuntimeError(
            "pyrage is not installed. Install the burner extra: "
            "pip install 'miki-a-friendly-sorter-bot[burner]'"
        ) from error
    try:
        public_key = pyrage.x25519.Recipient.from_str(recipient.strip())
    except Exception as error:  # malformed recipient
        raise ValueError(f"Invalid age recipient public key: {error}") from error
    return pyrage.encrypt(data, [public_key])


def _remove_sqlite_files(database: Path) -> None:
    """Remove a SQLite file and any WAL/SHM/journal sidecars left by verification."""

    for suffix in ("", "-wal", "-shm", "-journal"):
        database.with_name(database.name + suffix).unlink(missing_ok=True)


def compress_file(source: Path) -> Path:
    """Gzip ``source`` into a sibling ``<name>.gz`` and return it."""

    destination = source.with_name(source.name + ".gz")
    with source.open("rb") as raw, gzip.open(destination, "wb") as packed:
        shutil.copyfileobj(raw, packed)
    return destination


def encrypt_file(source: Path, recipient: str, *, encryptor: AgeEncryptor | None = None) -> Path:
    """age-encrypt ``source`` into ``<name>.age`` and return it."""

    encrypt = encryptor or _age_encrypt
    ciphertext = encrypt(source.read_bytes(), recipient)
    destination = source.with_name(source.name + ".age")
    destination.write_bytes(ciphertext)
    return destination


def create_backup_artifact(
    storage: Storage,
    settings: Settings,
    directory: Path,
    *,
    encryptor: AgeEncryptor | None = None,
) -> Path:
    """Produce a verified, compressed, encrypted artifact and clean intermediates."""

    directory.mkdir(parents=True, exist_ok=True)
    raw = storage.backup(directory)  # consistent backup + integrity check
    try:
        compressed = compress_file(raw)
    finally:
        _remove_sqlite_files(raw)
    try:
        encrypted = encrypt_file(
            compressed, settings.burner_backup_age_recipient, encryptor=encryptor
        )
    finally:
        compressed.unlink(missing_ok=True)
    return encrypted


def prune_local_backups(directory: Path, keep: int) -> tuple[str, ...]:
    """Keep the newest ``keep`` encrypted artifacts; delete older ones."""

    artifacts = sorted(directory.glob(f"*{ENCRYPTED_SUFFIX}"), reverse=True)
    removed: list[str] = []
    for stale in artifacts[keep:]:
        stale.unlink(missing_ok=True)
        removed.append(stale.name)
    return tuple(removed)


class TelethonBackupUploader:
    """Uploads a file to the configured backup group via the burner account."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def upload(self, file_path: Path, *, caption: str) -> int:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient

        settings = self._settings
        assert settings.telethon_api_id is not None
        client = TelegramClient(
            StringSession(settings.telethon_session),
            settings.telethon_api_id,
            settings.telethon_api_hash,
        )
        with client:
            message = client.send_file(  # type: ignore[attr-defined]
                settings.burner_backup_chat_id,
                str(file_path),
                caption=caption,
                reply_to=settings.burner_backup_thread_id,
                force_document=True,
            )
        return int(message.id)


def run_backup_offload(
    settings: Settings,
    *,
    storage: Storage,
    uploader: object,
    encryptor: AgeEncryptor | None = None,
) -> BackupOutcome:
    """Create, upload, and prune a backup. ``storage`` must already be open."""

    if not settings.burner_backup_configured:
        raise SystemExit(
            "Backup offload is not configured. Set BURNER_BACKUP_CHAT_ID and "
            "BURNER_BACKUP_AGE_RECIPIENT (plus the TELETHON_* burner credentials)."
        )

    artifact = create_backup_artifact(
        storage, settings, settings.backup_directory, encryptor=encryptor
    )
    size = artifact.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        artifact.unlink(missing_ok=True)
        raise ValueError(
            f"Backup artifact is {size} bytes, over the {MAX_UPLOAD_BYTES}-byte upload limit; "
            "chunking is not yet implemented."
        )

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    caption = f"miki index backup\n{artifact.name}\n{size} bytes\n{stamp}"
    message_id = uploader.upload(artifact, caption=caption)  # type: ignore[attr-defined]

    pruned = prune_local_backups(
        settings.backup_directory, settings.burner_backup_local_retention
    )
    logger.info(
        "Backup offloaded: %s (%s bytes), message %s, pruned %d local artifact(s).",
        artifact.name,
        size,
        message_id,
        len(pruned),
    )
    return BackupOutcome(
        artifact=artifact,
        size_bytes=size,
        uploaded_message_id=message_id,
        pruned=pruned,
    )
