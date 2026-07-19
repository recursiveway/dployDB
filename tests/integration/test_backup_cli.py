"""CLI and durable-operation gate for local backup and verification."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from dploydb.cli import app
from dploydb.config import STARTER_CONFIGURATION, RemoteBackupConfig, load_configuration
from dploydb.errors import OperationFailedError
from dploydb.locking import DeploymentLock
from dploydb.models import (
    BackupArtifact,
    OperationStatus,
    RemoteBackupArtifact,
    RemoteBackupMetadata,
    utc_now,
)
from dploydb.redaction import SecretRegistry
from dploydb.state import StateStore

runner = CliRunner()


class RecordingRemoteStorage:
    def __init__(self, *, failure: str | None = None) -> None:
        self.failure = failure
        self.uploaded: list[str] = []

    def put(
        self,
        artifact: BackupArtifact,
        *,
        release_id: str | None = None,
    ) -> RemoteBackupArtifact:
        if self.failure is not None:
            raise OperationFailedError(
                self.failure,
                production_changed=False,
                previous_application_running=None,
                next_safe_action="Retry the remote upload.",
            )
        self.uploaded.append(artifact.metadata.backup_id)
        record = RemoteBackupMetadata(
            backup=artifact.metadata,
            release_id=release_id,
            database_object_key=f"dploydb/backup-cli/{artifact.metadata.database_file_name}",
            uploaded_at=utc_now(),
        )
        return RemoteBackupArtifact(
            metadata=record,
            bucket="verified-backups",
            metadata_object_key=f"dploydb/backup-cli/{artifact.metadata.backup_id}.json",
        )


def _configuration(tmp_path: Path, *, secret: str | None = None) -> Path:
    database = tmp_path / ("data" if secret is None else secret) / "app.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("INSERT INTO notes(body) VALUES ('before-backup')")

    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = "backup-cli"
    value["state_directory"] = str(tmp_path / "state")
    value["database"]["path"] = (
        str(database) if secret is None else str(tmp_path / "${BACKUP_API_TOKEN}" / "app.db")
    )
    value["migration"]["command"] = [sys.executable, "-c", "pass"]
    value["application"]["compose_file"] = str(tmp_path / "compose.yaml")
    value["application"].pop("smoke_command", None)
    value["backup"]["local_directory"] = (
        str(tmp_path / "backups")
        if secret is None
        else str(tmp_path / "${BACKUP_API_TOKEN}" / "backups")
    )
    for name in (
        "maintenance_on_command",
        "maintenance_off_command",
        "activate_new_command",
        "activate_old_command",
    ):
        value["traffic"][name] = [sys.executable, "-c", "pass"]
    if secret is not None:
        value["application"]["test_mode_env"] = {"API_TOKEN": "${BACKUP_API_TOKEN}"}
    path = tmp_path / "dploydb.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def _enable_remote(config_path: Path, *, required: bool) -> None:
    value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    remote = value["backup"]["remote"]
    remote["enabled"] = True
    remote["required"] = required
    config_path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _install_remote(
    monkeypatch: pytest.MonkeyPatch,
    storage: RecordingRemoteStorage,
    *,
    registered_secret: str | None = None,
) -> None:
    def factory(
        _remote: RemoteBackupConfig,
        *,
        secrets: SecretRegistry,
        environment: Mapping[str, str],
    ) -> RecordingRemoteStorage:
        assert isinstance(environment, Mapping)
        secrets.register(registered_secret)
        return storage

    monkeypatch.setattr("dploydb.backup.configured_s3_storage", factory)


def test_backup_and_read_only_verify_json_are_stable(tmp_path: Path) -> None:
    config_path = _configuration(tmp_path)

    backup_result = runner.invoke(
        app,
        ["backup", "--config", str(config_path), "--json"],
    )

    assert backup_result.exit_code == 0, backup_result.output
    backup_payload = json.loads(backup_result.output)
    assert backup_payload["ok"] is True
    assert backup_payload["command"] == "backup"
    assert backup_payload["backup_id"].startswith("backup_")
    assert len(backup_payload["sha256"]) == 64
    assert backup_payload["checks"]["quick_check_passed"] is True

    loaded = load_configuration(config_path)
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    latest = store.latest_operation()
    assert latest is not None
    assert latest.operation_type == "backup"
    assert latest.status is OperationStatus.SUCCEEDED
    assert latest.stage == "snapshot_verified"
    state_before = {
        path.relative_to(loaded.config.state_directory): path.read_bytes()
        for path in loaded.config.state_directory.rglob("*")
        if path.is_file()
    }

    verify_result = runner.invoke(
        app,
        [
            "verify",
            backup_payload["backup_id"],
            "--config",
            str(config_path),
            "--json",
        ],
    )

    assert verify_result.exit_code == 0, verify_result.output
    verify_payload = json.loads(verify_result.output)
    assert verify_payload["command"] == "verify"
    assert verify_payload["backup_id"] == backup_payload["backup_id"]
    assert verify_payload["sha256"] == backup_payload["sha256"]
    state_after = {
        path.relative_to(loaded.config.state_directory): path.read_bytes()
        for path in loaded.config.state_directory.rglob("*")
        if path.is_file()
    }
    assert state_after == state_before


def test_backup_human_output_contains_verification_evidence(tmp_path: Path) -> None:
    config_path = _configuration(tmp_path)

    result = runner.invoke(app, ["backup", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "DployDB backup created." in result.output
    assert "Backup ID: backup_" in result.output
    assert "SHA-256:" in result.output
    assert "SQLite checks: passed" in result.output
    assert "Remote backup: not requested" in result.output


def test_backup_upload_commits_remote_and_records_stable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _configuration(tmp_path)
    _enable_remote(config_path, required=False)
    remote = RecordingRemoteStorage()
    _install_remote(monkeypatch, remote)

    result = runner.invoke(
        app,
        ["backup", "--upload", "--config", str(config_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["remote_uploaded"] is True
    assert payload["remote"]["provider"] == "s3"
    assert payload["remote"]["bucket"] == "verified-backups"
    assert remote.uploaded == [payload["backup_id"]]
    loaded = load_configuration(config_path)
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None
    assert latest.status is OperationStatus.SUCCEEDED
    assert latest.stage == "remote_snapshot_verified"


def test_required_remote_policy_uploads_without_explicit_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _configuration(tmp_path)
    _enable_remote(config_path, required=True)
    remote = RecordingRemoteStorage()
    _install_remote(monkeypatch, remote)

    result = runner.invoke(app, ["backup", "--config", str(config_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["remote_uploaded"] is True
    assert remote.uploaded == [payload["backup_id"]]


def test_remote_upload_failure_preserves_verified_local_backup_and_redacts_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _configuration(tmp_path)
    _enable_remote(config_path, required=False)
    secret = "remote-upload-synthetic-secret"
    remote = RecordingRemoteStorage(failure=f"remote refused {secret}")
    _install_remote(monkeypatch, remote, registered_secret=secret)

    result = runner.invoke(
        app,
        ["backup", "--upload", "--config", str(config_path), "--json"],
    )

    assert result.exit_code == 50
    assert secret not in result.output
    payload = json.loads(result.output)
    assert payload["production_changed"] is False
    loaded = load_configuration(config_path)
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None
    assert latest.status is OperationStatus.FAILED_SAFE
    assert latest.stage == "failed_safe"
    backup_files = list(loaded.config.backup.local_directory.glob("backup_*.db"))
    metadata_files = list(loaded.config.backup.local_directory.glob("backup_*.json"))
    assert len(backup_files) == len(metadata_files) == 1


def test_explicit_upload_refuses_disabled_remote_before_local_or_state_mutation(
    tmp_path: Path,
) -> None:
    config_path = _configuration(tmp_path)

    result = runner.invoke(
        app,
        ["backup", "--upload", "--config", str(config_path), "--json"],
    )

    assert result.exit_code == 50
    loaded = load_configuration(config_path)
    assert not loaded.config.state_directory.exists()
    assert not loaded.config.backup.local_directory.exists()


def test_backup_missing_database_is_durable_failed_safe(tmp_path: Path) -> None:
    config_path = _configuration(tmp_path)
    value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    Path(value["database"]["path"]).unlink()

    result = runner.invoke(
        app,
        ["backup", "--config", str(config_path), "--json"],
    )

    assert result.exit_code == 20
    payload = json.loads(result.output)
    assert payload["production_changed"] is False
    assert payload["recovery_required"] is False
    loaded = load_configuration(config_path)
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None
    assert latest.status is OperationStatus.FAILED_SAFE


def test_backup_is_blocked_by_deployment_lock(tmp_path: Path) -> None:
    config_path = _configuration(tmp_path)
    loaded = load_configuration(config_path)

    with DeploymentLock(loaded.config.state_directory, secrets=loaded.secrets):
        result = runner.invoke(
            app,
            ["backup", "--config", str(config_path), "--json"],
        )

    assert result.exit_code == 30
    payload = json.loads(result.output)
    assert payload["error_code"] == "deployment_lock_unavailable"
    assert payload["production_changed"] is False


def test_unfinished_state_blocks_new_backup(tmp_path: Path) -> None:
    config_path = _configuration(tmp_path)
    loaded = load_configuration(config_path)
    StateStore(loaded.config.state_directory, secrets=loaded.secrets).create_operation(
        operation_type="deploy",
        project=loaded.config.project,
        configuration_fingerprint="a" * 64,
    )

    result = runner.invoke(
        app,
        ["backup", "--config", str(config_path), "--json"],
    )

    assert result.exit_code == 60
    assert json.loads(result.output)["recovery_required"] is True


def test_verify_rejects_unknown_and_corrupted_backups(tmp_path: Path) -> None:
    config_path = _configuration(tmp_path)
    unknown = runner.invoke(
        app,
        ["verify", "backup_" + "f" * 32, "--config", str(config_path), "--json"],
    )
    assert unknown.exit_code == 20

    created = runner.invoke(
        app,
        ["backup", "--config", str(config_path), "--json"],
    )
    payload = json.loads(created.output)
    database = Path(payload["database_path"])
    content = bytearray(database.read_bytes())
    content[-1] ^= 0x01
    database.write_bytes(content)
    database.chmod(0o600)

    corrupted = runner.invoke(
        app,
        ["verify", payload["backup_id"], "--config", str(config_path), "--json"],
    )

    assert corrupted.exit_code == 20
    assert "checksum mismatch" in json.loads(corrupted.output)["what_failed"]


def test_backup_storage_mode_failure_is_failed_safe(tmp_path: Path) -> None:
    config_path = _configuration(tmp_path)
    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o755)
    backup_root.chmod(0o755)

    result = runner.invoke(
        app,
        ["backup", "--config", str(config_path), "--json"],
    )

    assert result.exit_code == 50
    payload = json.loads(result.output)
    assert payload["production_changed"] is False
    assert payload["recovery_required"] is False
    assert stat.S_IMODE(backup_root.stat().st_mode) == 0o755


def test_backup_outputs_and_evidence_redact_resolved_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "backup-cli-super-secret"
    config_path = _configuration(tmp_path, secret=secret)
    monkeypatch.setenv("BACKUP_API_TOKEN", secret)

    result = runner.invoke(
        app,
        ["backup", "--config", str(config_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    produced = result.output.encode()
    for path in tmp_path.rglob("*"):
        if path.is_file() and path != config_path:
            produced += path.read_bytes()
    assert secret not in result.output
    assert secret.encode() not in produced
    assert os.environ["BACKUP_API_TOKEN"] == secret
