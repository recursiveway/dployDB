"""Milestone 3 gate for durable migration rehearsal and production isolation."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from dploydb.backup import calculate_sha256
from dploydb.config import STARTER_CONFIGURATION, LoadedConfiguration, load_configuration
from dploydb.errors import (
    ExternalCommandError,
    LockUnavailableError,
    OperationFailedError,
    RecoveryRequiredError,
)
from dploydb.locking import DeploymentLock
from dploydb.migration import rehearse_configured_migration
from dploydb.models import BackupPurpose, OperationStatus
from dploydb.state import StateStore
from dploydb.storage.local import LocalBackupStorage

ROOT = Path(__file__).resolve().parents[2]
RELEASES = ROOT / "demo" / "releases"


def _initialize_v1(database: Path) -> None:
    database.parent.mkdir()
    database.touch(mode=0o600)
    environment = dict(os.environ)
    environment["DATABASE_PATH"] = str(database)
    completed = subprocess.run(
        [sys.executable, "-m", "demo.runtime.migration", str(RELEASES / "v1")],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _configuration(
    tmp_path: Path,
    command: list[str],
    *,
    timeout_seconds: int = 5,
    interpolation_environment: dict[str, str] | None = None,
) -> tuple[Path, LoadedConfiguration]:
    database = tmp_path / "data" / "app.db"
    _initialize_v1(database)
    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = "migration-integration"
    value["state_directory"] = str(tmp_path / "state")
    value["database"]["path"] = str(database)
    value["migration"]["command"] = command
    value["migration"]["timeout_seconds"] = timeout_seconds
    value["application"]["compose_file"] = str(ROOT / "demo" / "compose.yaml")
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
    return config_path, load_configuration(
        config_path,
        environment={} if interpolation_environment is None else interpolation_environment,
    )


def _demo_command(release: str) -> list[str]:
    return [sys.executable, "-m", "demo.runtime.migration", str(RELEASES / release)]


def _command_environment(**extra: str) -> dict[str, str]:
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(ROOT) if not existing else os.pathsep.join((str(ROOT), existing))
    )
    environment.update(extra)
    return environment


def _production_state(database: Path) -> tuple[tuple[int, str], int, list[tuple[Any, ...]], list]:
    file_evidence = calculate_sha256(database)
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        ).fetchall()
        rows = connection.execute("SELECT id, body FROM notes ORDER BY id").fetchall()
    return file_evidence, user_version, schema, rows


def _operation(loaded: LoadedConfiguration):
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None
    return latest


def _events(loaded: LoadedConfiguration):
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    latest = store.latest_operation()
    assert latest is not None
    return store.read_events(latest.operation_id)


def _assert_workspace_clean(loaded: LoadedConfiguration) -> None:
    root = loaded.config.state_directory / "rehearsals"
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_real_v2_migration_passes_on_copy_with_durable_evidence(tmp_path: Path) -> None:
    config_path, loaded = _configuration(tmp_path, _demo_command("v2"))
    production_before = _production_state(loaded.config.database.path)

    result = rehearse_configured_migration(
        loaded,
        config_path=config_path,
        command_environment=_command_environment(),
    )

    assert result.command.outcome == "succeeded"
    assert result.command.exit_code == 0
    assert result.command.stdout.text == "migration complete: v2\n"
    assert result.backup_sha256 != result.database_sha256
    assert result.sqlite.quick_check_passed is True
    assert _production_state(loaded.config.database.path) == production_before
    operation = _operation(loaded)
    assert operation.operation_type == "rehearsal"
    assert operation.status is OperationStatus.SUCCEEDED
    assert operation.stage == "rehearsal_passed"
    assert operation.safety.production_changed is False
    events = _events(loaded)
    assert [event.stage for event in events] == [
        "created",
        "preflight_passed",
        "snapshot_verified",
        "snapshot_verified",
        "rehearsal_passed",
    ]
    command_event = events[-2].evidence["migration_command"]
    assert isinstance(command_event, dict)
    assert command_event["stdout"]["text"] == "migration complete: v2\n"
    storage = LocalBackupStorage(loaded.config.backup.local_directory)
    snapshot = storage.get(result.backup_id)
    assert snapshot.metadata.purpose is BackupPurpose.REHEARSAL
    with sqlite3.connect(snapshot.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
    _assert_workspace_clean(loaded)


def test_broken_demo_migration_is_failed_safe_and_production_is_identical(
    tmp_path: Path,
) -> None:
    config_path, loaded = _configuration(tmp_path, _demo_command("broken-migration"))
    production_before = _production_state(loaded.config.database.path)

    with pytest.raises(ExternalCommandError, match="status 1") as captured:
        rehearse_configured_migration(
            loaded,
            config_path=config_path,
            command_environment=_command_environment(),
        )

    assert captured.value.payload.production_changed is False
    assert captured.value.payload.recovery_required is False
    assert captured.value.payload.log_path is not None
    assert _production_state(loaded.config.database.path) == production_before
    operation = _operation(loaded)
    assert operation.status is OperationStatus.FAILED_SAFE
    assert operation.stage == "failed_safe"
    assert operation.failure is not None
    assert operation.failure.error_code == "external_command_failed"
    command_event = _events(loaded)[-2].evidence["migration_command"]
    assert command_event["outcome"] == "nonzero_exit"
    assert "deliberate_missing_table" in command_event["stderr"]["text"]
    _assert_workspace_clean(loaded)


def test_timed_out_process_tree_is_gone_and_production_is_identical(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    code = """
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
print("migration tree ready", flush=True)
time.sleep(30)
"""
    config_path, loaded = _configuration(
        tmp_path,
        [sys.executable, "-c", code, str(child_pid_path)],
        timeout_seconds=1,
    )
    production_before = _production_state(loaded.config.database.path)
    started = time.monotonic()

    with pytest.raises(ExternalCommandError, match="timed out"):
        rehearse_configured_migration(
            loaded,
            config_path=config_path,
            command_environment=_command_environment(),
        )

    assert time.monotonic() - started < 5
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert _production_state(loaded.config.database.path) == production_before
    command_event = _events(loaded)[-2].evidence["migration_command"]
    assert command_event["outcome"] == "timed_out"
    assert command_event["termination_attempted"] is True
    assert command_event["stdout"]["text"] == "migration tree ready\n"
    assert _operation(loaded).status is OperationStatus.FAILED_SAFE
    _assert_workspace_clean(loaded)


def test_exit_zero_post_migration_database_failure_is_durable_and_safe(
    tmp_path: Path,
) -> None:
    code = """
import os, sqlite3
with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    connection.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
    connection.execute("INSERT INTO child(parent_id) VALUES (999)")
print("command exited zero")
"""
    config_path, loaded = _configuration(tmp_path, [sys.executable, "-c", code])
    production_before = _production_state(loaded.config.database.path)

    with pytest.raises(OperationFailedError, match="post-migration SQLite verification"):
        rehearse_configured_migration(
            loaded,
            config_path=config_path,
            command_environment=_command_environment(),
        )

    assert _production_state(loaded.config.database.path) == production_before
    operation = _operation(loaded)
    assert operation.status is OperationStatus.FAILED_SAFE
    assert operation.failure is not None
    assert operation.failure.error_code == "operation_failed"
    command_event = _events(loaded)[-2].evidence["migration_command"]
    assert command_event["outcome"] == "succeeded"
    assert command_event["stdout"]["text"] == "command exited zero\n"
    _assert_workspace_clean(loaded)


def test_rehearsal_command_and_all_durable_evidence_redact_secrets(tmp_path: Path) -> None:
    secret = "milestone-three-secret-value"
    code = """
import os, sqlite3, sys
assert sys.argv[1] == os.environ["API_TOKEN"]
print(sys.argv[1])
sys.stderr.write(f"token={os.environ['API_TOKEN']}\\n")
with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
    connection.execute("PRAGMA user_version = 2")
"""
    config_path, loaded = _configuration(
        tmp_path,
        [sys.executable, "-c", code, "${MIGRATION_API_TOKEN}"],
        interpolation_environment={"MIGRATION_API_TOKEN": secret},
    )
    production_before = _production_state(loaded.config.database.path)

    result = rehearse_configured_migration(
        loaded,
        config_path=config_path,
        command_environment=_command_environment(API_TOKEN=secret),
    )

    assert secret not in result.model_dump_json()
    assert _production_state(loaded.config.database.path) == production_before
    produced = result.model_dump_json().encode("utf-8")
    for path in tmp_path.rglob("*"):
        if path.is_file() and path != config_path:
            produced += path.read_bytes()
    assert secret.encode("utf-8") not in produced
    _assert_workspace_clean(loaded)


def test_lock_and_unfinished_state_block_rehearsal_before_backup(tmp_path: Path) -> None:
    config_path, loaded = _configuration(tmp_path, _demo_command("v2"))
    storage = LocalBackupStorage(loaded.config.backup.local_directory)

    with DeploymentLock(loaded.config.state_directory, secrets=loaded.secrets):
        with pytest.raises(LockUnavailableError):
            rehearse_configured_migration(
                loaded,
                config_path=config_path,
                command_environment=_command_environment(),
            )
    assert not loaded.config.backup.local_directory.exists()

    StateStore(loaded.config.state_directory, secrets=loaded.secrets).create_operation(
        operation_type="deploy",
        project=loaded.config.project,
        configuration_fingerprint="a" * 64,
    )
    with pytest.raises(RecoveryRequiredError):
        rehearse_configured_migration(
            loaded,
            config_path=config_path,
            command_environment=_command_environment(),
        )
    assert not storage.root.exists()
