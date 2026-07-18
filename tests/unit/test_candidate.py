from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from dploydb.candidate import validate_configured_candidate
from dploydb.config import STARTER_CONFIGURATION, LoadedConfiguration, load_configuration
from dploydb.errors import (
    ExternalCommandError,
    OperationFailedError,
    RecoveryRequiredError,
)
from dploydb.health import (
    BoundedResponseEvidence,
    CandidateHealthResult,
    HealthAttemptEvidence,
    HealthAttemptOutcome,
    ReadinessCheckError,
    ReadinessEvidence,
    SmokeCheckError,
)
from dploydb.models import OperationStatus
from dploydb.runners.base import (
    CandidateCleanup,
    CandidateCleanupError,
    CandidateCleanupProof,
    CandidateHandle,
    CandidateInspection,
    CandidateInspectionError,
    CandidateLogs,
    CandidateMount,
    CandidateStart,
    CandidateStartError,
)
from dploydb.state import StateStore
from dploydb.subprocesses import (
    CapturedOutput,
    CommandOutcome,
    CommandResult,
)


def command_result(
    action: str,
    *,
    outcome: CommandOutcome = CommandOutcome.SUCCEEDED,
    exit_code: int | None = 0,
    stdout: str = "",
) -> CommandResult:
    captured = CapturedOutput(
        text=stdout,
        total_bytes=len(stdout.encode()),
        retained_bytes=len(stdout.encode()),
        truncated=False,
    )
    return CommandResult(
        command=("fake", action),
        working_directory="/tmp",
        environment_keys=(),
        outcome=outcome,
        exit_code=exit_code,
        stdout=captured,
        stderr=CapturedOutput(text="", total_bytes=0, retained_bytes=0, truncated=False),
        duration_seconds=0.01,
        start_error="missing" if outcome is CommandOutcome.START_FAILED else None,
    )


def cleanup_evidence(*, proven: bool) -> CandidateCleanup:
    success = command_result("cleanup")
    container_query = command_result("container-query", stdout="" if proven else "present\n")
    network_query = command_result("network-query", stdout="" if proven else "network\n")
    proof = CandidateCleanupProof(
        container_absent=proven,
        networks_absent=proven,
        container_query=container_query,
        network_query=network_query,
    )
    return CandidateCleanup(
        presence_query=success,
        remove_command=None,
        compose_down=success,
        proof=proof,
    )


class FakeRunner:
    def __init__(
        self,
        *,
        start_failure: bool = False,
        inspection_failure: bool = False,
        cleanup_proven: bool = True,
    ) -> None:
        self.start_failure = start_failure
        self.inspection_failure = inspection_failure
        self.cleanup_proven = cleanup_proven
        self.database_path: Path | None = None
        self.stop_calls = 0

    def start(
        self,
        *,
        operation_id: str,
        version: str,
        rehearsal_database_path: Path,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateStart:
        self.database_path = rehearsal_database_path
        handle = CandidateHandle(
            operation_id=operation_id,
            version=version,
            compose_project="fake-project",
            container_name="fake-container",
            rehearsal_database_path=rehearsal_database_path,
            candidate_database_path="/data/rehearsal.db",
        )
        if self.start_failure:
            raise CandidateStartError(
                "candidate startup failed",
                command=command_result("start", outcome=CommandOutcome.NONZERO_EXIT, exit_code=7),
                cleanup=cleanup_evidence(proven=self.cleanup_proven),
            )
        return CandidateStart(
            handle=handle,
            container_reference="fake-container",
            command=command_result("start"),
        )

    def inspect(
        self,
        handle: CandidateHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateInspection:
        if self.inspection_failure:
            raise CandidateInspectionError(
                "candidate mounted production",
                command=command_result("inspect"),
            )
        return CandidateInspection(
            container_id="a" * 64,
            container_name=handle.container_name,
            running=True,
            compose_project=handle.compose_project,
            compose_service="app",
            operation_id=handle.operation_id,
            host_ip="127.0.0.1",
            host_port=4511,
            container_port=8080,
            mounts=(
                CandidateMount(
                    mount_type="bind",
                    source=str(handle.rehearsal_database_path.parent),
                    destination="/data",
                    read_write=True,
                ),
            ),
            command=command_result("inspect"),
        )

    def collect_logs(
        self,
        handle: CandidateHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateLogs:
        return CandidateLogs(handle=handle, command=command_result("logs", stdout="ready\n"))

    def stop(self, handle: CandidateHandle) -> CandidateCleanup:
        self.stop_calls += 1
        cleanup = cleanup_evidence(proven=self.cleanup_proven)
        if not self.cleanup_proven:
            raise CandidateCleanupError(
                "candidate cleanup unproven",
                command=cleanup.compose_down,
                cleanup=cleanup,
            )
        return cleanup

    def prove_cleanup(self, handle: CandidateHandle) -> CandidateCleanupProof:
        return cleanup_evidence(proven=self.cleanup_proven).proof


def readiness(*, healthy: bool) -> ReadinessEvidence:
    body = BoundedResponseEvidence(
        text="ok" if healthy else "unhealthy",
        total_bytes=2 if healthy else 9,
        retained_bytes=2 if healthy else 9,
        truncated=False,
    )
    attempt = HealthAttemptEvidence(
        attempt=1,
        outcome=(HealthAttemptOutcome.HEALTHY if healthy else HealthAttemptOutcome.UNHEALTHY_HTTP),
        status_code=200 if healthy else 503,
        body=body,
        reason="HTTP 200" if healthy else "HTTP 503 was not healthy",
        duration_seconds=0.01,
    )
    return ReadinessEvidence(
        url="http://127.0.0.1:4511/health",
        healthy=healthy,
        attempt_count=1,
        last_attempt=attempt,
        duration_seconds=0.01,
        reason="ready" if healthy else "readiness deadline expired",
    )


class FakeHealth:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.database_seen = False

    def check(
        self,
        *,
        version: str,
        rehearsal_database_path: Path,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateHealthResult:
        self.database_seen = rehearsal_database_path.is_file()
        if self.mode == "readiness":
            raise ReadinessCheckError(readiness(healthy=False))
        if self.mode == "smoke":
            smoke = command_result("smoke", outcome=CommandOutcome.NONZERO_EXIT, exit_code=8)
            raise SmokeCheckError(
                "candidate smoke command exited with status 8",
                readiness=readiness(healthy=True),
                command=smoke,
            )
        return CandidateHealthResult(readiness=readiness(healthy=True), smoke=None)


def configured(tmp_path: Path, *, secret: str | None = None) -> tuple[Path, LoadedConfiguration]:
    database = (tmp_path / "data" / "app.db").resolve()
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("INSERT INTO notes(body) VALUES ('before')")
        connection.execute("PRAGMA user_version = 1")
    migration = """
import os, sqlite3
with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
    connection.execute("ALTER TABLE notes ADD COLUMN category TEXT NOT NULL DEFAULT 'general'")
    connection.execute("PRAGMA user_version = 2")
print("candidate migration passed")
"""
    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = "candidate-unit"
    value["state_directory"] = str(tmp_path / "state")
    value["database"]["path"] = str(database)
    value["migration"]["command"] = [sys.executable, "-c", migration]
    value["migration"]["timeout_seconds"] = 5
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
    if secret is not None:
        value["application"]["test_mode_env"] = {"API_TOKEN": "${CANDIDATE_SECRET}"}
    config_path = tmp_path / "dploydb.yaml"
    config_path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    environment = {} if secret is None else {"CANDIDATE_SECRET": secret}
    return config_path, load_configuration(config_path, environment=environment)


def production_sha(loaded: LoadedConfiguration) -> str:
    return hashlib.sha256(loaded.config.database.path.read_bytes()).hexdigest()


def operation(loaded: LoadedConfiguration):
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    selected = store.latest_operation()
    assert selected is not None
    return selected, store.read_events(selected.operation_id)


def assert_workspace_clean(loaded: LoadedConfiguration) -> None:
    root = loaded.config.state_directory / "rehearsals"
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_success_is_durable_only_after_candidate_and_workspace_cleanup(tmp_path: Path) -> None:
    config_path, loaded = configured(tmp_path)
    before = production_sha(loaded)
    runner = FakeRunner()
    health = FakeHealth()

    result = validate_configured_candidate(
        loaded,
        version="v2",
        config_path=config_path,
        command_environment=os.environ,
        application_runner=runner,
        health_checker=health,
    )

    assert result.health.readiness.healthy is True
    assert result.cleanup.proof.proven is True
    assert runner.stop_calls == 1
    assert health.database_seen is True
    assert runner.database_path is not None and not runner.database_path.exists()
    assert production_sha(loaded) == before
    manifest, events = operation(loaded)
    assert manifest.status is OperationStatus.SUCCEEDED
    assert manifest.stage == "candidate_healthy"
    assert manifest.safety.production_changed is False
    assert events[-2].evidence["candidate_cleanup"]["proof"]["proven"] is True
    assert events[-1].evidence["rehearsal_workspace_cleaned"] is True
    assert_workspace_clean(loaded)


@pytest.mark.parametrize(
    ("runner", "health", "error_type", "expected_status"),
    [
        (FakeRunner(start_failure=True), FakeHealth(), ExternalCommandError, "failed_safe"),
        (FakeRunner(), FakeHealth("readiness"), OperationFailedError, "failed_safe"),
        (FakeRunner(), FakeHealth("smoke"), OperationFailedError, "failed_safe"),
        (
            FakeRunner(inspection_failure=True),
            FakeHealth(),
            RecoveryRequiredError,
            "recovery_required",
        ),
    ],
)
def test_expected_candidate_rejections_have_stable_durable_outcomes(
    tmp_path: Path,
    runner: FakeRunner,
    health: FakeHealth,
    error_type: type[Exception],
    expected_status: str,
) -> None:
    config_path, loaded = configured(tmp_path)
    before = production_sha(loaded)

    with pytest.raises(error_type):
        validate_configured_candidate(
            loaded,
            version="v2",
            config_path=config_path,
            command_environment=os.environ,
            application_runner=runner,
            health_checker=health,
        )

    manifest, _events = operation(loaded)
    assert manifest.status.value == expected_status
    assert manifest.failure is not None
    assert manifest.safety.production_changed is False
    assert production_sha(loaded) == before
    if not runner.start_failure:
        assert runner.stop_calls == 1
    assert_workspace_clean(loaded)


def test_unproven_candidate_cleanup_overrides_health_rejection_with_recovery(
    tmp_path: Path,
) -> None:
    config_path, loaded = configured(tmp_path)
    runner = FakeRunner(cleanup_proven=False)

    with pytest.raises(RecoveryRequiredError, match="cleanup"):
        validate_configured_candidate(
            loaded,
            version="v2",
            config_path=config_path,
            command_environment=os.environ,
            application_runner=runner,
            health_checker=FakeHealth("readiness"),
        )

    manifest, events = operation(loaded)
    assert manifest.status is OperationStatus.RECOVERY_REQUIRED
    assert events[-2].evidence["candidate_cleanup"]["proof"]["proven"] is False
    assert_workspace_clean(loaded)


def test_unproven_rehearsal_workspace_cleanup_is_recovery_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, loaded = configured(tmp_path)
    monkeypatch.setattr(
        "dploydb.migration._cleanup_workspace",
        lambda _workspace: "injected workspace cleanup failure",
    )

    with pytest.raises(RecoveryRequiredError, match="workspace cleanup"):
        validate_configured_candidate(
            loaded,
            version="v2",
            config_path=config_path,
            command_environment=os.environ,
            application_runner=FakeRunner(),
            health_checker=FakeHealth(),
        )

    manifest, _events = operation(loaded)
    assert manifest.status is OperationStatus.RECOVERY_REQUIRED
    assert manifest.safety.recovery_required is True


def test_candidate_operation_redacts_returned_and_durable_evidence(tmp_path: Path) -> None:
    secret = "candidate-operation-secret"
    config_path, loaded = configured(tmp_path, secret=secret)

    result = validate_configured_candidate(
        loaded,
        version="v2",
        config_path=config_path,
        command_environment={**os.environ, "API_TOKEN": secret},
        application_runner=FakeRunner(),
        health_checker=FakeHealth(),
    )

    produced = str(result.as_evidence()).encode()
    for path in loaded.config.state_directory.rglob("*"):
        if path.is_file():
            produced += path.read_bytes()
    assert secret.encode() not in produced
