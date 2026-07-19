"""Tests for the final-backup, production-migration, and rollback transaction."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

import dploydb.restore as restore_module
from dploydb.config import STARTER_CONFIGURATION, LoadedConfiguration, load_configuration
from dploydb.cutover import (
    create_final_backup,
    migrate_production_database,
    restore_final_backup,
)
from dploydb.errors import (
    ExternalCommandError,
    OperationFailedError,
    RecoveryRequiredError,
    SafetyCheckError,
)
from dploydb.models import (
    BackupPurpose,
    MigrationCommandEvidence,
    ProductionApplicationHandle,
)
from dploydb.runners.base import CandidateMount, ProductionInspection, ProductionStop
from dploydb.subprocesses import (
    CapturedOutput,
    CommandOutcome,
    CommandResult,
    SubprocessRunner,
    TerminationReason,
)

OPERATION_ID = "op_" + "8" * 32


def loaded_project(
    tmp_path: Path,
    *,
    migration: list[str],
    timeout_seconds: int = 5,
) -> tuple[Path, LoadedConfiguration]:
    database = (tmp_path / "data" / "app.db").resolve()
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("INSERT INTO notes(body) VALUES ('preserved-before-cutover')")
        connection.execute("PRAGMA user_version = 1")

    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = "cutover-test"
    value["state_directory"] = str(tmp_path / "state")
    value["database"]["path"] = str(database)
    value["migration"]["command"] = migration
    value["migration"]["timeout_seconds"] = timeout_seconds
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
    config_path = tmp_path / "dploydb.yaml"
    config_path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return config_path, load_configuration(config_path, environment={})


def successful_migration() -> list[str]:
    code = """
import os, sqlite3
with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
    connection.execute("ALTER TABLE notes ADD COLUMN category TEXT NOT NULL DEFAULT 'general'")
    connection.execute("PRAGMA user_version = 2")
print("production migration passed")
"""
    return [sys.executable, "-c", code]


def command_result(
    *,
    outcome: CommandOutcome = CommandOutcome.SUCCEEDED,
    exit_code: int | None = 0,
    cleanup_error: str | None = None,
) -> CommandResult:
    empty = CapturedOutput(text="", total_bytes=0, retained_bytes=0, truncated=False)
    termination_reason = None
    if outcome is CommandOutcome.CLEANUP_FAILED:
        termination_reason = TerminationReason.INTERRUPTION
    elif outcome is CommandOutcome.TIMED_OUT:
        termination_reason = TerminationReason.TIMEOUT
    elif outcome is CommandOutcome.CANCELLED:
        termination_reason = TerminationReason.CANCELLATION
    return CommandResult(
        command=("fake", "migration"),
        working_directory="/tmp",
        environment_keys=(),
        outcome=outcome,
        exit_code=exit_code,
        stdout=empty,
        stderr=empty,
        duration_seconds=0.01,
        termination_reason=termination_reason,
        termination_attempted=outcome
        in {
            CommandOutcome.CLEANUP_FAILED,
            CommandOutcome.TIMED_OUT,
            CommandOutcome.CANCELLED,
        },
        cleanup_error=cleanup_error,
    )


class FakeExecutor:
    def __init__(self, selected: CommandResult) -> None:
        self.selected = selected

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str],
        working_directory: Path | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> CommandResult:
        del command, timeout_seconds, environment, working_directory, cancellation_event
        return self.selected


def stopped_proof(loaded: LoadedConfiguration, *, running: bool = False) -> ProductionStop:
    database = loaded.config.database.path
    handle = ProductionApplicationHandle(
        source="bootstrap",
        container_id="a" * 64,
        container_name="cutover-current-app-1",
        compose_project="example-app",
        compose_service="app",
        version=None,
        release_id=None,
        operation_id=None,
        database_directory=database.parent,
        database_target=loaded.config.application.database_volume_target,
        host_port=4510,
        container_port=8080,
        health_url="http://127.0.0.1:4510/health",
    )
    command = command_result()
    inspection = ProductionInspection(
        handle=handle,
        running=running,
        mounts=(
            CandidateMount(
                mount_type="bind",
                source=str(database.parent),
                destination="/data",
                read_write=True,
            ),
        ),
        command=command,
    )
    return ProductionStop(handle=handle, command=command, inspection=inspection)


def database_state(path: Path) -> tuple[str, int, list[tuple[Any, ...]], list[str]]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        rows = connection.execute("SELECT * FROM notes ORDER BY id").fetchall()
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
            )
        ]
    return digest, version, rows, tables


def test_final_backup_requires_stopped_proof_and_is_operation_bound(tmp_path: Path) -> None:
    config_path, loaded = loaded_project(tmp_path, migration=successful_migration())
    del config_path

    with pytest.raises(SafetyCheckError, match="requires matching proof"):
        create_final_backup(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped_proof(loaded, running=True),
        )

    final = create_final_backup(
        loaded,
        operation_id=OPERATION_ID,
        stopped=stopped_proof(loaded),
    )

    assert final.metadata.purpose is BackupPurpose.FINAL
    assert final.metadata.operation_id == OPERATION_ID
    assert final.metadata.sha256 == hashlib.sha256(final.database_path.read_bytes()).hexdigest()


def test_final_backup_failure_is_safe_with_previous_application_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config_path, loaded = loaded_project(tmp_path, migration=successful_migration())

    def fail_snapshot(*_args: object, **_kwargs: object) -> None:
        raise OperationFailedError(
            "injected final snapshot failure",
            production_changed=False,
            previous_application_running=False,
            log_path=tmp_path / "backup.log",
            next_safe_action="Restart the previous application.",
        )

    monkeypatch.setattr("dploydb.cutover.create_verified_backup", fail_snapshot)

    with pytest.raises(OperationFailedError, match="final snapshot") as captured:
        create_final_backup(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped_proof(loaded),
        )

    assert captured.value.payload.production_changed is False
    assert captured.value.payload.previous_application_running is False
    assert database_state(loaded.config.database.path)[1] == 1


def test_successful_production_migration_records_complete_evidence(tmp_path: Path) -> None:
    config_path, loaded = loaded_project(tmp_path, migration=successful_migration())
    stopped = stopped_proof(loaded)
    final = create_final_backup(loaded, operation_id=OPERATION_ID, stopped=stopped)
    evidence: list[MigrationCommandEvidence] = []

    migrated = migrate_production_database(
        loaded,
        operation_id=OPERATION_ID,
        stopped=stopped,
        final_backup=final,
        config_path=config_path,
        traffic_activated=False,
        evidence_sink=evidence.append,
        command_environment=dict(os.environ),
        log_path=tmp_path / "events.jsonl",
    )

    assert migrated.final_backup_id == final.metadata.backup_id
    assert migrated.command.outcome == "succeeded"
    assert evidence == [migrated.command]
    assert database_state(loaded.config.database.path)[1] == 2


@pytest.mark.parametrize(
    "failure",
    ["nonzero", "timeout", "cancelled", "truncated", "postcheck"],
)
def test_failed_production_migration_can_restore_exact_final_backup(
    tmp_path: Path,
    failure: str,
) -> None:
    if failure == "nonzero":
        code = """
import os, sqlite3
with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
    connection.execute("CREATE TABLE partial_change(id INTEGER PRIMARY KEY)")
raise SystemExit(9)
"""
        command = [sys.executable, "-c", code]
        timeout = 5
        runner = None
    elif failure == "timeout":
        code = """
import os, sqlite3, time
with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
    connection.execute("CREATE TABLE partial_change(id INTEGER PRIMARY KEY)")
time.sleep(60)
"""
        command = [sys.executable, "-c", code]
        timeout = 1
        runner = None
    elif failure == "cancelled":
        command = successful_migration()
        timeout = 5
        runner = FakeExecutor(
            command_result(
                outcome=CommandOutcome.CANCELLED,
                exit_code=-15,
            )
        )
    elif failure == "truncated":
        code = """
import os, sqlite3
with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
    connection.execute("CREATE TABLE partial_change(id INTEGER PRIMARY KEY)")
print("x" * 10000)
"""
        command = [sys.executable, "-c", code]
        timeout = 5
        runner = None
    else:
        code = """
import os, sqlite3
with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
    connection.execute("CREATE TABLE child(parent_id INTEGER REFERENCES parent(id))")
    connection.execute("INSERT INTO child(parent_id) VALUES (999)")
"""
        command = [sys.executable, "-c", code]
        timeout = 5
        runner = None

    config_path, loaded = loaded_project(
        tmp_path,
        migration=command,
        timeout_seconds=timeout,
    )
    if failure == "truncated":
        runner = SubprocessRunner(secrets=loaded.secrets, max_output_bytes=64)
    stopped = stopped_proof(loaded)
    final = create_final_backup(loaded, operation_id=OPERATION_ID, stopped=stopped)
    expected_sha = final.metadata.sha256

    with pytest.raises((ExternalCommandError, OperationFailedError)) as captured:
        migrate_production_database(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped,
            final_backup=final,
            config_path=config_path,
            traffic_activated=False,
            evidence_sink=lambda _evidence: None,
            command_environment=dict(os.environ),
            command_runner=runner,
            log_path=tmp_path / "events.jsonl",
        )

    assert captured.value.payload.previous_application_running is False
    restored = restore_final_backup(
        loaded,
        operation_id=OPERATION_ID,
        stopped=stopped,
        final_backup=final,
        traffic_activated=False,
    )
    assert restored.sha256 == expected_sha
    assert hashlib.sha256(loaded.config.database.path.read_bytes()).hexdigest() == expected_sha
    state = database_state(loaded.config.database.path)
    assert state[1] == 1
    assert state[2] == [(1, "preserved-before-cutover")]
    assert "partial_change" not in state[3]


def test_tampered_final_backup_is_rejected_before_restore_replaces_database(
    tmp_path: Path,
) -> None:
    _config_path, loaded = loaded_project(tmp_path, migration=successful_migration())
    stopped = stopped_proof(loaded)
    final = create_final_backup(loaded, operation_id=OPERATION_ID, stopped=stopped)
    with sqlite3.connect(loaded.config.database.path) as connection:
        connection.execute("CREATE TABLE current_state(id INTEGER PRIMARY KEY)")
    current = loaded.config.database.path.read_bytes()
    final.database_path.chmod(0o600)
    final.database_path.write_bytes(final.database_path.read_bytes() + b"tampered")

    with pytest.raises(SafetyCheckError, match="mismatch") as captured:
        restore_final_backup(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped,
            final_backup=final,
            traffic_activated=False,
        )

    assert captured.value.payload.production_changed is True
    assert loaded.config.database.path.read_bytes() == current


def test_evidence_persistence_failure_is_recovery_required_after_command(tmp_path: Path) -> None:
    config_path, loaded = loaded_project(tmp_path, migration=successful_migration())
    stopped = stopped_proof(loaded)
    final = create_final_backup(loaded, operation_id=OPERATION_ID, stopped=stopped)

    def fail_evidence(_evidence: MigrationCommandEvidence) -> None:
        raise OSError("injected durable evidence failure")

    with pytest.raises(RecoveryRequiredError, match="durable evidence") as captured:
        migrate_production_database(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped,
            final_backup=final,
            config_path=config_path,
            traffic_activated=False,
            evidence_sink=fail_evidence,
            command_environment=dict(os.environ),
            log_path=tmp_path / "events.jsonl",
        )

    assert captured.value.payload.production_changed is True


def test_unproven_migration_process_cleanup_requires_recovery(tmp_path: Path) -> None:
    config_path, loaded = loaded_project(tmp_path, migration=successful_migration())
    stopped = stopped_proof(loaded)
    final = create_final_backup(loaded, operation_id=OPERATION_ID, stopped=stopped)
    cleanup_failed = command_result(
        outcome=CommandOutcome.CLEANUP_FAILED,
        exit_code=-9,
        cleanup_error="descendant remained",
    )

    with pytest.raises(RecoveryRequiredError, match="cleanup"):
        migrate_production_database(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped,
            final_backup=final,
            config_path=config_path,
            traffic_activated=False,
            evidence_sink=lambda _evidence: None,
            command_runner=FakeExecutor(cleanup_failed),
            log_path=tmp_path / "events.jsonl",
        )


def test_automatic_restore_is_refused_after_traffic_without_touching_database(
    tmp_path: Path,
) -> None:
    _config_path, loaded = loaded_project(tmp_path, migration=successful_migration())
    stopped = stopped_proof(loaded)
    final = create_final_backup(loaded, operation_id=OPERATION_ID, stopped=stopped)
    before = loaded.config.database.path.read_bytes()

    with pytest.raises(SafetyCheckError, match="forbidden after new traffic") as captured:
        restore_final_backup(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped,
            final_backup=final,
            traffic_activated=True,
        )

    assert captured.value.payload.production_changed is True
    assert loaded.config.database.path.read_bytes() == before


def test_restore_faults_distinguish_pre_replace_failure_from_uncertain_post_replace(
    tmp_path: Path,
) -> None:
    _config_path, loaded = loaded_project(tmp_path, migration=successful_migration())
    stopped = stopped_proof(loaded)
    final = create_final_backup(loaded, operation_id=OPERATION_ID, stopped=stopped)
    with sqlite3.connect(loaded.config.database.path) as connection:
        connection.execute("CREATE TABLE changed(id INTEGER PRIMARY KEY)")
    changed = loaded.config.database.path.read_bytes()

    def fail_before(stage: str) -> None:
        if stage == "rollback_after_staging":
            raise OSError("injected pre-replace failure")

    with pytest.raises(OperationFailedError, match="before replacement"):
        restore_final_backup(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped,
            final_backup=final,
            traffic_activated=False,
            fault_injector=fail_before,
        )
    assert loaded.config.database.path.read_bytes() == changed

    def fail_after(stage: str) -> None:
        if stage == "rollback_after_replace":
            raise OSError("injected post-replace failure")

    with pytest.raises(RecoveryRequiredError, match="could not be proven"):
        restore_final_backup(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped,
            final_backup=final,
            traffic_activated=False,
            fault_injector=fail_after,
        )
    assert hashlib.sha256(loaded.config.database.path.read_bytes()).hexdigest() == (
        final.metadata.sha256
    )


def test_restore_replace_failure_preserves_current_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config_path, loaded = loaded_project(tmp_path, migration=successful_migration())
    stopped = stopped_proof(loaded)
    final = create_final_backup(loaded, operation_id=OPERATION_ID, stopped=stopped)
    with sqlite3.connect(loaded.config.database.path) as connection:
        connection.execute("CREATE TABLE changed(id INTEGER PRIMARY KEY)")
    changed = loaded.config.database.path.read_bytes()

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("injected atomic replacement failure")

    monkeypatch.setattr(restore_module.os, "replace", fail_replace)

    with pytest.raises(OperationFailedError, match="replacement failure"):
        restore_final_backup(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped,
            final_backup=final,
            traffic_activated=False,
        )

    assert loaded.config.database.path.read_bytes() == changed


def test_restore_directory_fsync_failure_requires_recovery_after_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config_path, loaded = loaded_project(tmp_path, migration=successful_migration())
    stopped = stopped_proof(loaded)
    final = create_final_backup(loaded, operation_id=OPERATION_ID, stopped=stopped)
    with sqlite3.connect(loaded.config.database.path) as connection:
        connection.execute("CREATE TABLE changed(id INTEGER PRIMARY KEY)")
    real_fsync = restore_module._fsync_directory
    calls = 0

    def fail_final_fsync(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected directory fsync failure")
        real_fsync(path)

    monkeypatch.setattr(restore_module, "_fsync_directory", fail_final_fsync)

    with pytest.raises(RecoveryRequiredError, match="fsync failure") as captured:
        restore_final_backup(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped,
            final_backup=final,
            traffic_activated=False,
        )

    assert calls == 3
    assert captured.value.payload.production_changed is True
    assert hashlib.sha256(loaded.config.database.path.read_bytes()).hexdigest() == (
        final.metadata.sha256
    )


def test_restore_rejects_unsafe_sidecar_without_replacing_database(tmp_path: Path) -> None:
    _config_path, loaded = loaded_project(tmp_path, migration=successful_migration())
    stopped = stopped_proof(loaded)
    final = create_final_backup(loaded, operation_id=OPERATION_ID, stopped=stopped)
    with sqlite3.connect(loaded.config.database.path) as connection:
        connection.execute("CREATE TABLE changed(id INTEGER PRIMARY KEY)")
    changed = loaded.config.database.path.read_bytes()
    sidecar = Path(f"{loaded.config.database.path}-wal")
    sidecar.symlink_to(tmp_path / "unsafe-target")

    with pytest.raises(OperationFailedError, match="unsafe SQLite sidecar"):
        restore_final_backup(
            loaded,
            operation_id=OPERATION_ID,
            stopped=stopped,
            final_backup=final,
            traffic_activated=False,
        )

    assert loaded.config.database.path.read_bytes() == changed
    assert sidecar.is_symlink()
