"""Focused contracts for doctor and read-only status."""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from dploydb.cli import app
from dploydb.config import STARTER_CONFIGURATION, LoadedConfiguration, load_configuration
from dploydb.diagnostics import inspect_runtime_status, run_doctor
from dploydb.locking import DeploymentLock
from dploydb.models import (
    DiagnosticOutcome,
    LockOwnerMetadata,
    LockOwnerState,
    OperationStatus,
    ProcessIdentity,
    RuntimeStatus,
)
from dploydb.state import StateStore

FINGERPRINT = "a" * 64
runner = CliRunner()


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(0o700)


def configured_project(tmp_path: Path, *, secret: str | None = None) -> tuple[Path, dict[str, str]]:
    database = tmp_path / "data" / "app.db"
    database.parent.mkdir()
    database.write_bytes(b"not-opened-by-milestone-1")
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  app:\n    image: example\n", encoding="utf-8")
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    docker = binary_directory / "docker"
    _write_executable(
        docker,
        """import os, sys
if os.environ.get("API_TOKEN"):
    print(os.environ["API_TOKEN"], file=sys.stderr)
if "--services" in sys.argv:
    print("app")
else:
    print("Docker test fixture")""",
    )

    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = "test-project"
    value["state_directory"] = str(tmp_path / "state")
    value["database"]["path"] = str(database)
    value["migration"]["command"] = [sys.executable, "-c", "pass"]
    value["application"]["compose_file"] = str(compose)
    value["application"]["candidate_port"] = _free_port()
    value["application"]["candidate_health_url"] = (
        f"http://127.0.0.1:{value['application']['candidate_port']}/health"
    )
    value["application"].pop("smoke_command", None)
    for name in (
        "maintenance_on_command",
        "maintenance_off_command",
        "activate_new_command",
        "activate_old_command",
    ):
        value["traffic"][name] = [sys.executable, "-c", "pass"]
    value["backup"]["local_directory"] = str(tmp_path / "backups")
    value["backup"]["remote"] = {"enabled": False, "provider": "s3"}
    if secret is not None:
        value["application"]["test_mode_env"] = {"API_TOKEN": secret}
    config_path = tmp_path / "dploydb.yaml"
    config_path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    environment = dict(os.environ)
    environment["PATH"] = f"{binary_directory}{os.pathsep}{environment.get('PATH', '')}"
    if secret is not None:
        environment["API_TOKEN"] = secret
    return config_path, environment


def _load(config_path: Path, environment: dict[str, str]) -> LoadedConfiguration:
    return load_configuration(config_path, environment=environment)


def test_status_is_idle_and_does_not_create_state(tmp_path: Path) -> None:
    config_path, environment = configured_project(tmp_path)
    loaded = _load(config_path, environment)

    report = inspect_runtime_status(loaded.config, secrets=loaded.secrets)

    assert report.status is RuntimeStatus.IDLE
    assert report.exit_code == 0
    assert report.operation is None
    assert not loaded.config.state_directory.exists()


def test_status_reports_active_lock_as_coherent(tmp_path: Path) -> None:
    config_path, environment = configured_project(tmp_path)
    loaded = _load(config_path, environment)
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    operation = store.create_operation(
        operation_type="deploy",
        project=loaded.config.project,
        configuration_fingerprint=FINGERPRINT,
    )

    with DeploymentLock(loaded.config.state_directory, secrets=loaded.secrets) as lock:
        lock.record_owner(operation_id=operation.operation_id, operation_type="deploy")
        report = inspect_runtime_status(loaded.config, secrets=loaded.secrets)

    assert report.status is RuntimeStatus.ACTIVE
    assert report.exit_code == 0
    assert report.operation is not None
    assert report.operation["operation_id"] == operation.operation_id


def test_unlocked_in_progress_operation_is_interrupted_and_read_only(tmp_path: Path) -> None:
    config_path, environment = configured_project(tmp_path)
    loaded = _load(config_path, environment)
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    operation = store.create_operation(
        operation_type="deploy",
        project=loaded.config.project,
        configuration_fingerprint=FINGERPRINT,
    )
    before = {
        path.relative_to(loaded.config.state_directory): path.read_bytes()
        for path in loaded.config.state_directory.rglob("*")
        if path.is_file()
    }

    report = inspect_runtime_status(loaded.config, secrets=loaded.secrets)

    after = {
        path.relative_to(loaded.config.state_directory): path.read_bytes()
        for path in loaded.config.state_directory.rglob("*")
        if path.is_file()
    }
    assert report.status is RuntimeStatus.INTERRUPTED
    assert report.exit_code == 60
    assert report.operation is not None
    assert report.operation["operation_id"] == operation.operation_id
    assert before == after


def test_terminal_safe_operation_returns_idle(tmp_path: Path) -> None:
    config_path, environment = configured_project(tmp_path)
    loaded = _load(config_path, environment)
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    operation = store.create_operation(
        operation_type="doctor_fixture",
        project=loaded.config.project,
        configuration_fingerprint=FINGERPRINT,
    )
    store.transition(
        operation.operation_id,
        status=OperationStatus.SUCCEEDED,
        stage="completed",
        message="Fixture completed.",
    )

    report = inspect_runtime_status(loaded.config, secrets=loaded.secrets)

    assert report.status is RuntimeStatus.IDLE
    assert report.operation is not None
    assert report.operation["status"] == "succeeded"


def test_stale_owner_mismatch_requires_recovery(tmp_path: Path) -> None:
    config_path, environment = configured_project(tmp_path)
    loaded = _load(config_path, environment)
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    store.create_operation(
        operation_type="deploy",
        project=loaded.config.project,
        configuration_fingerprint=FINGERPRINT,
    )
    owner = LockOwnerMetadata(
        owner_id="lock_" + "c" * 32,
        operation_id="op_" + "d" * 32,
        operation_type="deploy",
        process=ProcessIdentity(pid=123, hostname="test-host"),
        state=LockOwnerState.ACTIVE,
        acquired_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    owner_path = loaded.config.state_directory / "deployment-lock-owner.json"
    owner_path.write_text(owner.model_dump_json() + "\n", encoding="utf-8")
    owner_path.chmod(0o600)

    report = inspect_runtime_status(loaded.config, secrets=loaded.secrets)

    assert report.status is RuntimeStatus.RECOVERY_REQUIRED
    assert report.exit_code == 60
    assert report.failure is not None
    assert report.failure.production_changed is False


def test_corrupt_operation_state_is_reported_without_repair(tmp_path: Path) -> None:
    config_path, environment = configured_project(tmp_path)
    loaded = _load(config_path, environment)
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    operation = store.create_operation(
        operation_type="deploy",
        project=loaded.config.project,
        configuration_fingerprint=FINGERPRINT,
    )
    manifest = store.operation_paths(operation.operation_id).manifest
    manifest.write_text("{broken\n", encoding="utf-8")
    before = manifest.read_bytes()

    report = inspect_runtime_status(loaded.config, secrets=loaded.secrets)

    assert report.status is RuntimeStatus.RECOVERY_REQUIRED
    assert report.exit_code == 60
    assert report.failure is not None
    assert report.failure.production_changed is True
    assert manifest.read_bytes() == before


def test_standard_and_deep_doctor_pass_and_mark_future_checks_skipped(tmp_path: Path) -> None:
    config_path, environment = configured_project(tmp_path)
    loaded = _load(config_path, environment)

    standard = run_doctor(
        loaded,
        config_path=config_path,
        deep=False,
        environment=environment,
    )
    deep = run_doctor(
        loaded,
        config_path=config_path,
        deep=True,
        environment=environment,
    )

    assert standard.exit_code == 0
    assert deep.exit_code == 0
    assert "docker_daemon" not in {check.check_id for check in standard.checks}
    assert "docker_daemon" in {check.check_id for check in deep.checks}
    deferred = {
        check.check_id: check.outcome
        for check in deep.checks
        if check.check_id
        in {
            "sqlite_integrity",
            "remote_storage",
            "migration_execution",
            "application_health",
            "traffic_execution",
        }
    }
    assert set(deferred.values()) == {DiagnosticOutcome.SKIPPED}
    assert not list(tmp_path.rglob(".dploydb-doctor-*.tmp"))


def test_doctor_failure_uses_safety_exit_and_lists_failed_check(tmp_path: Path) -> None:
    config_path, environment = configured_project(tmp_path)
    loaded = _load(config_path, environment)
    loaded.config.database.path.unlink()

    report = run_doctor(
        loaded,
        config_path=config_path,
        deep=False,
        environment=environment,
    )

    assert report.exit_code == 20
    assert report.failure is not None
    assert any(
        check.check_id == "database_file" and check.outcome is DiagnosticOutcome.FAILED
        for check in report.checks
    )


def test_doctor_resolves_relative_executable_from_configuration_directory(
    tmp_path: Path,
) -> None:
    config_path, environment = configured_project(tmp_path)
    tools_directory = tmp_path / "tools"
    tools_directory.mkdir()
    migration = tools_directory / "migrate"
    _write_executable(migration, "print('unused migration fixture')")
    value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    value["migration"]["command"] = ["tools/migrate"]
    config_path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    loaded = _load(config_path, environment)

    report = run_doctor(
        loaded,
        config_path=config_path,
        deep=False,
        environment=environment,
    )

    check = next(item for item in report.checks if item.check_id == "migration_executable")
    assert check.outcome is DiagnosticOutcome.PASSED
    assert check.evidence["path"] == str(migration)


def test_doctor_rejects_occupied_candidate_port(tmp_path: Path) -> None:
    config_path, environment = configured_project(tmp_path)
    loaded = _load(config_path, environment)
    listener = socket.socket()
    listener.bind(("127.0.0.1", loaded.config.application.candidate_port))
    listener.listen()
    try:
        report = run_doctor(
            loaded,
            config_path=config_path,
            deep=False,
            environment=environment,
        )
    finally:
        listener.close()

    assert report.exit_code == 20
    check = next(item for item in report.checks if item.check_id == "candidate_port")
    assert check.outcome is DiagnosticOutcome.FAILED


def test_cli_json_status_and_doctor_are_stable_and_redacted(tmp_path: Path) -> None:
    secret = "doctor-super-secret-value"
    config_path, environment = configured_project(tmp_path, secret=secret)
    previous = os.environ.copy()
    os.environ.update(environment)
    try:
        doctor_result = runner.invoke(app, ["doctor", "--config", str(config_path), "--json"])
        status_result = runner.invoke(app, ["status", "--config", str(config_path), "--json"])
    finally:
        os.environ.clear()
        os.environ.update(previous)

    assert doctor_result.exit_code == 0
    assert status_result.exit_code == 0
    doctor_payload = json.loads(doctor_result.output)
    status_payload = json.loads(status_result.output)
    assert doctor_payload["command"] == "doctor"
    assert doctor_payload["summary"]["skipped"] == 5
    assert status_payload["command"] == "status"
    assert status_payload["status"] == "idle"
    assert secret not in doctor_result.output
    assert secret not in status_result.output


def test_status_cli_interrupted_returns_stable_recovery_exit(tmp_path: Path) -> None:
    config_path, environment = configured_project(tmp_path)
    loaded = _load(config_path, environment)
    StateStore(loaded.config.state_directory, secrets=loaded.secrets).create_operation(
        operation_type="deploy",
        project=loaded.config.project,
        configuration_fingerprint=FINGERPRINT,
    )

    result = runner.invoke(app, ["status", "--config", str(config_path), "--json"])

    assert result.exit_code == 60
    payload = json.loads(result.output)
    assert payload["status"] == "interrupted"
    assert payload["recovery_required"] is True
    assert payload["production_changed"] is False
    assert payload["exit_code"] == 60


def test_invalid_doctor_configuration_reaches_no_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("project: broken\nunknown: value\n", encoding="utf-8")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("host diagnostics must not run")

    monkeypatch.setattr("dploydb.cli.run_doctor", forbidden)

    result = runner.invoke(app, ["doctor", "--config", str(path), "--json"])

    assert result.exit_code == 10
    assert json.loads(result.output)["error_code"] == "configuration_error"


def test_fake_docker_fixture_is_private_executable(tmp_path: Path) -> None:
    config_path, environment = configured_project(tmp_path)
    docker = Path(environment["PATH"].split(os.pathsep)[0]) / "docker"

    assert config_path.exists()
    assert stat.S_IMODE(docker.stat().st_mode) == 0o700
