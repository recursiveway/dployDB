"""Tests for the internal stopped-application restore engine."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from dploydb.backup import create_configured_backup
from dploydb.config import STARTER_CONFIGURATION, LoadedConfiguration, load_configuration
from dploydb.errors import OperationFailedError, RecoveryRequiredError, SafetyCheckError
from dploydb.models import BackupPurpose, OperationStatus
from dploydb.restore import restore_stopped_database
from dploydb.state import StateStore
from dploydb.storage.local import LocalBackupStorage


def _loaded_project(tmp_path: Path) -> LoadedConfiguration:
    database = tmp_path / "data" / "app.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("INSERT INTO notes(body) VALUES ('selected-backup-state')")

    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = "restore-test"
    value["state_directory"] = str(tmp_path / "state")
    value["database"]["path"] = str(database)
    value["migration"]["command"] = [sys.executable, "-c", "pass"]
    value["application"]["compose_file"] = str(tmp_path / "compose.yaml")
    value["application"].pop("smoke_command", None)
    value["backup"]["local_directory"] = str(tmp_path / "backups")
    for name in (
        "maintenance_on_command",
        "maintenance_off_command",
        "activate_new_command",
        "activate_old_command",
    ):
        value["traffic"][name] = [sys.executable, "-c", "pass"]
    path = tmp_path / "dploydb.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return load_configuration(path)


def _add_later_row(loaded: LoadedConfiguration) -> None:
    with sqlite3.connect(loaded.config.database.path) as connection:
        connection.execute("INSERT INTO notes(body) VALUES ('current-before-restore')")


def _rows(loaded: LoadedConfiguration) -> list[tuple[str]]:
    with sqlite3.connect(loaded.config.database.path) as connection:
        return connection.execute("SELECT body FROM notes ORDER BY id").fetchall()


def test_restore_replaces_database_and_preserves_pre_restore_backup(tmp_path: Path) -> None:
    loaded = _loaded_project(tmp_path)
    selected = create_configured_backup(loaded)
    _add_later_row(loaded)

    result = restore_stopped_database(
        loaded,
        selected.metadata.backup_id,
        application_stopped=True,
    )

    assert _rows(loaded) == [("selected-backup-state",)]
    assert result.selected_backup_id == selected.metadata.backup_id
    assert result.pre_restore_backup_id != selected.metadata.backup_id
    storage = LocalBackupStorage(loaded.config.backup.local_directory)
    pre_restore = storage.get(result.pre_restore_backup_id)
    assert pre_restore.metadata.purpose is BackupPurpose.PRE_RESTORE
    with sqlite3.connect(pre_restore.database_path) as connection:
        assert connection.execute("SELECT body FROM notes ORDER BY id").fetchall() == [
            ("selected-backup-state",),
            ("current-before-restore",),
        ]
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None
    assert latest.operation_type == "restore"
    assert latest.status is OperationStatus.SUCCEEDED
    assert latest.stage == "manual_restore_completed"
    assert latest.safety.production_changed is True


def test_restore_refuses_unstopped_application_without_new_state(tmp_path: Path) -> None:
    loaded = _loaded_project(tmp_path)
    selected = create_configured_backup(loaded)
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    before = store.latest_operation()

    with pytest.raises(SafetyCheckError, match="requires the application"):
        restore_stopped_database(
            loaded,
            selected.metadata.backup_id,
            application_stopped=False,
        )

    assert store.latest_operation() == before


def test_precommit_failure_leaves_current_database_unchanged(tmp_path: Path) -> None:
    loaded = _loaded_project(tmp_path)
    selected = create_configured_backup(loaded)
    _add_later_row(loaded)

    def fail_after_staging(stage: str) -> None:
        if stage == "after_staging":
            raise OSError("injected precommit failure")

    with pytest.raises(OperationFailedError, match="injected precommit failure") as captured:
        restore_stopped_database(
            loaded,
            selected.metadata.backup_id,
            application_stopped=True,
            fault_injector=fail_after_staging,
        )

    assert captured.value.payload.production_changed is False
    assert _rows(loaded) == [
        ("selected-backup-state",),
        ("current-before-restore",),
    ]
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None and latest.status is OperationStatus.FAILED_SAFE
    assert latest.safety.production_changed is False


def test_postcommit_failure_restores_and_verifies_previous_database(tmp_path: Path) -> None:
    loaded = _loaded_project(tmp_path)
    selected = create_configured_backup(loaded)
    _add_later_row(loaded)

    def fail_after_replace(stage: str) -> None:
        if stage == "after_replace":
            raise OSError("injected postcommit failure")

    with pytest.raises(OperationFailedError, match="previous database was restored") as captured:
        restore_stopped_database(
            loaded,
            selected.metadata.backup_id,
            application_stopped=True,
            fault_injector=fail_after_replace,
        )

    assert captured.value.payload.production_changed is True
    assert captured.value.payload.recovery_required is False
    assert _rows(loaded) == [
        ("selected-backup-state",),
        ("current-before-restore",),
    ]
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None and latest.status is OperationStatus.FAILED_SAFE
    assert latest.safety.production_changed is True


def test_failed_rollback_records_recovery_required(tmp_path: Path) -> None:
    loaded = _loaded_project(tmp_path)
    selected = create_configured_backup(loaded)
    _add_later_row(loaded)

    def fail_restore_and_rollback(stage: str) -> None:
        if stage == "after_replace":
            raise OSError("injected restore failure")
        if stage == "rollback_before_replace":
            raise OSError("injected rollback failure")

    with pytest.raises(RecoveryRequiredError) as captured:
        restore_stopped_database(
            loaded,
            selected.metadata.backup_id,
            application_stopped=True,
            fault_injector=fail_restore_and_rollback,
        )

    assert captured.value.payload.production_changed is True
    assert captured.value.payload.recovery_required is True
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None and latest.status is OperationStatus.RECOVERY_REQUIRED


def test_restore_removes_regular_wal_and_shm_sidecars_while_stopped(tmp_path: Path) -> None:
    loaded = _loaded_project(tmp_path)
    selected = create_configured_backup(loaded)
    _add_later_row(loaded)
    sidecars = [Path(f"{loaded.config.database.path}{suffix}") for suffix in ("-wal", "-shm")]

    def create_sidecars(stage: str) -> None:
        if stage == "after_staging":
            for sidecar in sidecars:
                sidecar.write_bytes(b"stale stopped-application sidecar")

    restore_stopped_database(
        loaded,
        selected.metadata.backup_id,
        application_stopped=True,
        fault_injector=create_sidecars,
    )

    assert _rows(loaded) == [("selected-backup-state",)]
    assert not any(sidecar.exists() for sidecar in sidecars)
