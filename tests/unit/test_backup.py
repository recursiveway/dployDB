"""Focused tests for verified local backup artifacts."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from dploydb.backup import create_verified_backup, verify_backup
from dploydb.errors import OperationFailedError, SafetyCheckError
from dploydb.models import BackupPurpose
from dploydb.storage.local import LocalBackupStorage


def _database(path: Path, rows: int = 3) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO notes(body) VALUES (?)",
            [(f"note-{index}",) for index in range(rows)],
        )


def test_create_and_reverify_committed_local_backup(tmp_path: Path) -> None:
    source = tmp_path / "app.db"
    _database(source)
    storage = LocalBackupStorage(tmp_path / "backups")

    artifact = create_verified_backup(
        source,
        project="backup-test",
        purpose=BackupPurpose.STANDALONE,
        storage=storage,
    )

    assert artifact.metadata.backup_id.startswith("backup_")
    assert artifact.metadata.source_database_path == source
    assert artifact.metadata.size_bytes == artifact.database_path.stat().st_size
    assert len(artifact.metadata.sha256) == 64
    assert stat.S_IMODE(artifact.database_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(artifact.metadata_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(storage.root.stat().st_mode) == 0o700
    assert verify_backup(storage, artifact.metadata.backup_id) == artifact
    with sqlite3.connect(artifact.database_path) as connection:
        assert connection.execute("SELECT body FROM notes ORDER BY id").fetchall() == [
            ("note-0",),
            ("note-1",),
            ("note-2",),
        ]


def test_corrupted_backup_is_rejected_by_checksum(tmp_path: Path) -> None:
    source = tmp_path / "app.db"
    _database(source)
    storage = LocalBackupStorage(tmp_path / "backups")
    artifact = create_verified_backup(
        source,
        project="backup-test",
        purpose=BackupPurpose.STANDALONE,
        storage=storage,
    )
    payload = bytearray(artifact.database_path.read_bytes())
    payload[-1] ^= 0xFF
    artifact.database_path.write_bytes(payload)
    os.chmod(artifact.database_path, 0o600)

    with pytest.raises(SafetyCheckError, match="checksum mismatch"):
        verify_backup(storage, artifact.metadata.backup_id)


def test_metadata_tampering_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "app.db"
    _database(source)
    storage = LocalBackupStorage(tmp_path / "backups")
    artifact = create_verified_backup(
        source,
        project="backup-test",
        purpose=BackupPurpose.STANDALONE,
        storage=storage,
    )
    artifact.metadata_path.write_text("{}\n", encoding="utf-8")
    artifact.metadata_path.chmod(0o600)

    with pytest.raises(SafetyCheckError, match="metadata is invalid"):
        storage.get(artifact.metadata.backup_id)


def test_symlinked_backup_database_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "app.db"
    _database(source)
    storage = LocalBackupStorage(tmp_path / "backups")
    artifact = create_verified_backup(
        source,
        project="backup-test",
        purpose=BackupPurpose.STANDALONE,
        storage=storage,
    )
    moved = tmp_path / "moved.db"
    artifact.database_path.replace(moved)
    artifact.database_path.symlink_to(moved)

    with pytest.raises(SafetyCheckError, match="non-symlink"):
        storage.get(artifact.metadata.backup_id)


def test_invalid_backup_identifier_cannot_escape_storage(tmp_path: Path) -> None:
    storage = LocalBackupStorage(tmp_path / "backups")

    with pytest.raises(SafetyCheckError, match="backup ID is invalid"):
        storage.get("../production.db")


def test_metadata_publication_failure_does_not_commit_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "app.db"
    _database(source)
    storage = LocalBackupStorage(tmp_path / "backups")

    def fail_metadata(*_args: object, **_kwargs: object) -> None:
        raise OperationFailedError(
            "injected metadata failure",
            next_safe_action="Retry.",
        )

    monkeypatch.setattr(storage, "_publish_metadata", fail_metadata)

    with pytest.raises(OperationFailedError, match="publication failed"):
        create_verified_backup(
            source,
            project="backup-test",
            purpose=BackupPurpose.STANDALONE,
            storage=storage,
        )

    assert not list(storage.root.glob("backup_*.json"))
    assert not list(storage.root.glob("backup_*.db"))
    assert not list(storage.root.glob(".*.tmp"))


def test_storage_requires_private_directory_mode(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o755)
    root.chmod(0o755)

    with pytest.raises(OperationFailedError, match="mode-0700"):
        LocalBackupStorage(root).ensure_layout()
