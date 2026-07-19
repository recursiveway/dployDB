"""Retention gates for protected releases and resumable deletion."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dploydb.backup import create_verified_backup
from dploydb.errors import OperationFailedError, SafetyCheckError
from dploydb.models import (
    BackupArtifact,
    BackupPurpose,
    ReleaseManifest,
    ReleasePointers,
    RemoteBackupMetadata,
    utc_now,
)
from dploydb.releases import ReleaseHistorySnapshot
from dploydb.retention import apply_retention, protected_backup_ids
from dploydb.storage.local import LocalBackupStorage


class MemoryRemoteRetention:
    def __init__(self, records: tuple[RemoteBackupMetadata, ...]) -> None:
        self.records = {record.backup.backup_id: record for record in records}
        self.deleted: list[str] = []
        self.fail_once = False

    def list(self) -> tuple[RemoteBackupMetadata, ...]:
        return tuple(self.records.values())

    def delete(self, backup_id: str) -> None:
        if self.fail_once:
            self.fail_once = False
            raise OperationFailedError(
                "injected remote retention failure",
                production_changed=False,
                previous_application_running=None,
                next_safe_action="Retry retention.",
            )
        self.records.pop(backup_id, None)
        self.deleted.append(backup_id)


def _backups(
    tmp_path: Path,
    *,
    count: int,
) -> tuple[LocalBackupStorage, tuple[BackupArtifact, ...]]:
    database = tmp_path / "app.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    storage = LocalBackupStorage(tmp_path / "backups")
    artifacts: list[BackupArtifact] = []
    for index in range(count):
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO notes(body) VALUES (?)", (f"row-{index}",))
        artifacts.append(
            create_verified_backup(
                database,
                project="retention-test",
                purpose=BackupPurpose.STANDALONE,
                storage=storage,
            )
        )
    return storage, tuple(artifacts)


def _history(
    *,
    active_backups: tuple[str, str],
    previous_backups: tuple[str, str],
) -> ReleaseHistorySnapshot:
    active_id = "release_" + "a" * 32
    previous_id = "release_" + "b" * 32
    active = ReleaseManifest.model_construct(
        release_id=active_id,
        project="retention-test",
        rehearsal_backup_id=active_backups[0],
        final_backup_id=active_backups[1],
    )
    previous = ReleaseManifest.model_construct(
        release_id=previous_id,
        project="retention-test",
        rehearsal_backup_id=previous_backups[0],
        final_backup_id=previous_backups[1],
    )
    return ReleaseHistorySnapshot(
        releases=(active, previous),
        pointers=ReleasePointers(
            active_release_id=active_id,
            previous_release_id=previous_id,
            updated_at=utc_now(),
        ),
    )


def _remote_records(
    artifacts: tuple[BackupArtifact, ...],
) -> tuple[RemoteBackupMetadata, ...]:
    return tuple(
        RemoteBackupMetadata(
            backup=artifact.metadata,
            database_object_key=f"retention/{artifact.metadata.database_file_name}",
            uploaded_at=utc_now(),
        )
        for artifact in artifacts
    )


def test_active_and_previous_backups_survive_beyond_keep_count_locally_and_remotely(
    tmp_path: Path,
) -> None:
    local, artifacts = _backups(tmp_path, count=8)
    history = _history(
        active_backups=(artifacts[0].metadata.backup_id, artifacts[1].metadata.backup_id),
        previous_backups=(artifacts[2].metadata.backup_id, artifacts[3].metadata.backup_id),
    )
    remote = MemoryRemoteRetention(_remote_records(artifacts))

    result = apply_retention(
        project="retention-test",
        keep_last=2,
        history=history,
        local_storage=local,
        remote_storage=remote,
    )

    protected = {
        artifacts[0].metadata.backup_id,
        artifacts[1].metadata.backup_id,
        artifacts[2].metadata.backup_id,
        artifacts[3].metadata.backup_id,
    }
    newest = {artifacts[6].metadata.backup_id, artifacts[7].metadata.backup_id}
    assert set(result.protected_backup_ids) == protected
    assert {record.backup_id for record in local.list()} == protected | newest
    assert set(remote.records) == protected | newest
    assert set(result.local_deleted) == {
        artifacts[4].metadata.backup_id,
        artifacts[5].metadata.backup_id,
    }
    assert set(result.remote_deleted) == set(result.local_deleted)

    repeated = apply_retention(
        project="retention-test",
        keep_last=2,
        history=history,
        local_storage=local,
        remote_storage=remote,
    )
    assert repeated.local_deleted == ()
    assert repeated.remote_deleted == ()


def test_partial_local_then_remote_retention_failure_is_safe_to_retry(tmp_path: Path) -> None:
    local, artifacts = _backups(tmp_path, count=6)
    history = _history(
        active_backups=(artifacts[0].metadata.backup_id, artifacts[1].metadata.backup_id),
        previous_backups=(artifacts[2].metadata.backup_id, artifacts[3].metadata.backup_id),
    )
    remote = MemoryRemoteRetention(_remote_records(artifacts))
    remote.fail_once = True

    with pytest.raises(OperationFailedError, match="remote retention failure"):
        apply_retention(
            project="retention-test",
            keep_last=1,
            history=history,
            local_storage=local,
            remote_storage=remote,
        )

    result = apply_retention(
        project="retention-test",
        keep_last=1,
        history=history,
        local_storage=local,
        remote_storage=remote,
    )
    assert result.local_deleted == ()
    assert result.remote_deleted == (artifacts[4].metadata.backup_id,)
    assert artifacts[5].metadata.backup_id in remote.records


def test_local_metadata_first_deletion_resumes_after_database_unlink_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local, artifacts = _backups(tmp_path, count=1)
    artifact = artifacts[0]
    original_unlink = Path.unlink
    fail_database_once = True

    def injected_unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal fail_database_once
        if path == artifact.database_path and fail_database_once:
            fail_database_once = False
            raise OSError("injected database unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", injected_unlink)

    with pytest.raises(OperationFailedError, match="backup deletion failed"):
        local.delete(artifact.metadata.backup_id)

    assert not artifact.metadata_path.exists()
    assert artifact.database_path.exists()
    local.delete(artifact.metadata.backup_id)
    local.delete(artifact.metadata.backup_id)
    assert not artifact.database_path.exists()


def test_protection_refuses_pointer_history_from_another_project() -> None:
    history = _history(
        active_backups=("backup_" + "1" * 32, "backup_" + "2" * 32),
        previous_backups=("backup_" + "3" * 32, "backup_" + "4" * 32),
    )

    with pytest.raises(SafetyCheckError, match="different project"):
        protected_backup_ids(history, project="another-project")
