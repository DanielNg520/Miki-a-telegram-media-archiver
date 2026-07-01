from __future__ import annotations

import gzip

import pytest

from miki_sorter_bot import burner_backup
from miki_sorter_bot.burner_backup import (
    BackupOutcome,
    compress_file,
    create_backup_artifact,
    encrypt_file,
    prune_local_backups,
    run_backup_offload,
)
from miki_sorter_bot.config import Settings
from miki_sorter_bot.storage import Storage


def _settings(tmp_path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "BOT_TOKEN": "token",
        "SOURCE_CHAT_ID": -100,
        "SOURCE_THREAD_ID": 5,
        "ARCHIVE_CHAT_ID": -200,
        "BURNER_ENABLED": True,
        "TELETHON_API_ID": 1,
        "TELETHON_API_HASH": "hash",
        "TELETHON_SESSION": "session",
        "BURNER_BACKUP_CHAT_ID": -555,
        "BURNER_BACKUP_AGE_RECIPIENT": "age1examplerecipient",
        "BACKUP_DIRECTORY": str(tmp_path / "backups"),
        "DATABASE_PATH": str(tmp_path / "miki.sqlite3"),
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


# Identity-ish encryptor so tests never need pyrage.
def _fake_encrypt(data: bytes, recipient: str) -> bytes:
    return b"AGE:" + recipient.encode() + b":" + data


class _FakeUploader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def upload(self, file_path, *, caption: str) -> int:
        self.calls.append((file_path.name, caption))
        return 4242


def test_backup_configured_property(tmp_path) -> None:
    assert _settings(tmp_path).burner_backup_configured is True
    assert _settings(tmp_path, BURNER_BACKUP_CHAT_ID=None).burner_backup_configured is False
    assert _settings(tmp_path, BURNER_BACKUP_AGE_RECIPIENT="").burner_backup_configured is False
    assert _settings(tmp_path, BURNER_ENABLED=False).burner_backup_configured is False


def test_compress_file_roundtrip(tmp_path) -> None:
    src = tmp_path / "data.bin"
    src.write_bytes(b"hello" * 1000)
    gz = compress_file(src)
    assert gz.name == "data.bin.gz"
    with gzip.open(gz, "rb") as handle:
        assert handle.read() == b"hello" * 1000


def test_encrypt_file_uses_injected_encryptor(tmp_path) -> None:
    src = tmp_path / "data.gz"
    src.write_bytes(b"payload")
    enc = encrypt_file(src, "age1abc", encryptor=_fake_encrypt)
    assert enc.name == "data.gz.age"
    assert enc.read_bytes() == b"AGE:age1abc:payload"


def test_create_backup_artifact_cleans_intermediates(tmp_path) -> None:
    settings = _settings(tmp_path)
    storage = Storage(settings.database_path)
    storage.open()
    try:
        artifact = create_backup_artifact(
            storage, settings, settings.backup_directory, encryptor=_fake_encrypt
        )
    finally:
        storage.close()

    assert artifact.name.endswith(".sqlite3.gz.age")
    siblings = {p.name for p in settings.backup_directory.iterdir()}
    # Only the encrypted artifact remains; raw + gz are removed.
    assert siblings == {artifact.name}


def test_prune_local_backups_keeps_newest(tmp_path) -> None:
    directory = tmp_path / "backups"
    directory.mkdir()
    names = [f"miki-2024010{i}T000000Z.sqlite3.gz.age" for i in range(1, 6)]
    for name in names:
        (directory / name).write_bytes(b"x")

    removed = prune_local_backups(directory, keep=2)

    remaining = sorted(p.name for p in directory.glob("*.age"))
    assert remaining == names[-2:]  # newest two by name
    assert set(removed) == set(names[:-2])


def test_run_backup_offload_end_to_end(tmp_path) -> None:
    settings = _settings(tmp_path)
    uploader = _FakeUploader()
    storage = Storage(settings.database_path)
    storage.open()
    try:
        outcome = run_backup_offload(
            settings, storage=storage, uploader=uploader, encryptor=_fake_encrypt
        )
    finally:
        storage.close()

    assert isinstance(outcome, BackupOutcome)
    assert outcome.uploaded_message_id == 4242
    assert len(uploader.calls) == 1
    assert uploader.calls[0][0] == outcome.artifact.name
    assert outcome.artifact.exists()
    assert outcome.as_dict()["uploaded_message_id"] == 4242


def test_run_backup_offload_rejects_oversize(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    uploader = _FakeUploader()
    monkeypatch.setattr(burner_backup, "MAX_UPLOAD_BYTES", 1)
    storage = Storage(settings.database_path)
    storage.open()
    try:
        with pytest.raises(ValueError, match="over the"):
            run_backup_offload(
                settings, storage=storage, uploader=uploader, encryptor=_fake_encrypt
            )
    finally:
        storage.close()

    assert uploader.calls == []
    # Oversize artifact is cleaned up rather than left on the droplet.
    assert list(settings.backup_directory.glob("*.age")) == []


def test_run_backup_offload_requires_configuration(tmp_path) -> None:
    settings = _settings(tmp_path, BURNER_BACKUP_CHAT_ID=None)
    storage = Storage(settings.database_path)
    storage.open()
    try:
        with pytest.raises(SystemExit):
            run_backup_offload(
                settings, storage=storage, uploader=_FakeUploader(), encryptor=_fake_encrypt
            )
    finally:
        storage.close()


def test_retention_prunes_old_artifacts_after_upload(tmp_path) -> None:
    settings = _settings(tmp_path, BURNER_BACKUP_LOCAL_RETENTION=1)
    settings.backup_directory.mkdir(parents=True)
    # Pre-existing older artifact that should be pruned once a newer one lands.
    old = settings.backup_directory / "miki-20000101T000000Z.sqlite3.gz.age"
    old.write_bytes(b"old")

    storage = Storage(settings.database_path)
    storage.open()
    try:
        outcome = run_backup_offload(
            settings, storage=storage, uploader=_FakeUploader(), encryptor=_fake_encrypt
        )
    finally:
        storage.close()

    assert old.name in outcome.pruned
    assert not old.exists()
    assert outcome.artifact.exists()
