"""Tests for the deployment coordinator and pre-traffic rollback matrix."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from dploydb.candidate import CandidateStageObserver, CandidateValidationResult
from dploydb.config import STARTER_CONFIGURATION, LoadedConfiguration, load_configuration
from dploydb.cutover import (
    create_final_backup,
    migrate_production_database,
    restore_final_backup,
)
from dploydb.deploy import DeploymentResult, deploy_configured_release
from dploydb.deployment_dependencies import CutoverDatabase, DeploymentDependencies
from dploydb.errors import ExternalCommandError, OperationFailedError, RecoveryRequiredError
from dploydb.health import (
    BoundedResponseEvidence,
    CandidateHealthResult,
    HealthAttemptEvidence,
    HealthAttemptOutcome,
    ReadinessCheckError,
    ReadinessEvidence,
)
from dploydb.locking import DeploymentLock
from dploydb.models import (
    BackupArtifact,
    DeploymentState,
    MigrationCommandEvidence,
    OperationStatus,
    ProductionApplicationHandle,
    ProductionMigrationResult,
    ReleaseManifest,
    VerifiedDatabaseRestoreResult,
)
from dploydb.releases import ReleaseStore
from dploydb.runners.base import (
    CandidateMount,
    ProductionCleanup,
    ProductionCleanupError,
    ProductionCleanupProof,
    ProductionDiscovery,
    ProductionInspection,
    ProductionLogs,
    ProductionRestart,
    ProductionStart,
    ProductionStartError,
    ProductionStop,
)
from dploydb.state import StateStore
from dploydb.subprocesses import (
    CapturedOutput,
    CommandOutcome,
    CommandResult,
    TerminationReason,
)
from dploydb.traffic import TrafficAction, TrafficHookResult

OPERATION_ID_PLACEHOLDER = "op_" + "1" * 32
REHEARSAL_BACKUP_ID = "backup_" + "2" * 32
REHEARSAL_SHA256 = "3" * 64


def loaded_project(tmp_path: Path) -> tuple[Path, LoadedConfiguration]:
    database = (tmp_path / "data" / "app.db").resolve()
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("INSERT INTO notes(body) VALUES ('preserved-v1-row')")
        connection.execute("PRAGMA user_version = 1")

    migration = """
import os, sqlite3
with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
    connection.execute("ALTER TABLE notes ADD COLUMN category TEXT NOT NULL DEFAULT 'general'")
    connection.execute("PRAGMA user_version = 2")
print("migration complete")
"""
    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = "deploy-test"
    value["state_directory"] = str(tmp_path / "state")
    value["database"]["path"] = str(database)
    value["migration"]["command"] = [sys.executable, "-c", migration]
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


def command_result(
    name: str,
    *,
    outcome: CommandOutcome = CommandOutcome.SUCCEEDED,
    exit_code: int | None = 0,
    cleanup_error: str | None = None,
) -> CommandResult:
    empty = CapturedOutput(text="", total_bytes=0, retained_bytes=0, truncated=False)
    termination_reason = None
    termination_attempted = False
    if outcome is CommandOutcome.CLEANUP_FAILED:
        termination_reason = TerminationReason.INTERRUPTION
        termination_attempted = True
    elif outcome is CommandOutcome.TIMED_OUT:
        termination_reason = TerminationReason.TIMEOUT
        termination_attempted = True
    elif outcome is CommandOutcome.CANCELLED:
        termination_reason = TerminationReason.CANCELLATION
        termination_attempted = exit_code is not None
    return CommandResult(
        command=(name,),
        working_directory="/tmp",
        environment_keys=(),
        outcome=outcome,
        exit_code=exit_code,
        stdout=empty,
        stderr=empty,
        duration_seconds=0.01,
        termination_reason=termination_reason,
        termination_attempted=termination_attempted,
        cleanup_error=cleanup_error,
        start_error=("missing executable" if outcome is CommandOutcome.START_FAILED else None),
    )


def healthy_result(url: str = "http://127.0.0.1:4510/health") -> CandidateHealthResult:
    body = BoundedResponseEvidence(
        text='{"ok": true}',
        total_bytes=12,
        retained_bytes=12,
        truncated=False,
    )
    attempt = HealthAttemptEvidence(
        attempt=1,
        outcome=HealthAttemptOutcome.HEALTHY,
        status_code=200,
        body=body,
        reason="application returned HTTP 200",
        duration_seconds=0.01,
    )
    return CandidateHealthResult(
        readiness=ReadinessEvidence(
            url=url,
            healthy=True,
            attempt_count=1,
            last_attempt=attempt,
            duration_seconds=0.01,
            reason="application is ready",
        ),
        smoke=None,
    )


def unhealthy_error(url: str = "http://127.0.0.1:4510/health") -> ReadinessCheckError:
    attempt = HealthAttemptEvidence(
        attempt=1,
        outcome=HealthAttemptOutcome.UNHEALTHY_HTTP,
        status_code=500,
        body=None,
        reason="application returned HTTP 500",
        duration_seconds=0.01,
    )
    return ReadinessCheckError(
        ReadinessEvidence(
            url=url,
            healthy=False,
            attempt_count=1,
            last_attempt=attempt,
            duration_seconds=0.01,
            reason="application returned HTTP 500",
        )
    )


@dataclass(frozen=True, slots=True)
class FakeCandidateResult:
    operation_id: str

    def as_evidence(self) -> dict[str, Any]:
        return {"operation_id": self.operation_id, "candidate_cleanup_proven": True}


class FakePreCutover:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def run(
        self,
        loaded: LoadedConfiguration,
        *,
        version: str,
        config_path: Path,
        operation_id: str,
        store: StateStore,
        lock: DeploymentLock,
        cancellation_event: threading.Event | None,
        stage_observer: CandidateStageObserver,
    ) -> CandidateValidationResult:
        del loaded, version, config_path, lock, cancellation_event
        if self.fail:
            raise OperationFailedError(
                "candidate rejected",
                production_changed=False,
                previous_application_running=None,
                log_path=store.operation_paths(operation_id).events,
                next_safe_action="Correct the candidate and retry.",
            )
        stages: list[tuple[DeploymentState, dict[str, Any]]] = [
            (DeploymentState.PREFLIGHT_PASSED, {"quick_check_passed": True}),
            (
                DeploymentState.SNAPSHOT_VERIFIED,
                {
                    "backup_id": REHEARSAL_BACKUP_ID,
                    "sha256": REHEARSAL_SHA256,
                    "size_bytes": 8192,
                },
            ),
            (DeploymentState.REHEARSAL_PASSED, {"migration_outcome": "succeeded"}),
            (DeploymentState.CANDIDATE_HEALTHY, {"candidate_cleanup_proven": True}),
        ]
        for stage, evidence in stages:
            store.transition(
                operation_id,
                status=OperationStatus.IN_PROGRESS,
                stage=stage.value,
                message=f"Fake candidate stage reached {stage.value}.",
                evidence=evidence,
            )
            stage_observer(stage, evidence)
        return cast(CandidateValidationResult, FakeCandidateResult(operation_id))


class FakeProduction:
    def __init__(self, loaded: LoadedConfiguration, *, fault: str | None = None) -> None:
        database = loaded.config.database.path
        self.loaded = loaded
        self.fault = fault
        self.calls: list[str] = []
        self.previous_running = True
        self.new_running = False
        self.previous = ProductionApplicationHandle(
            source="bootstrap",
            container_id="a" * 64,
            container_name="deploy-test-app-1",
            compose_project="example-app",
            compose_service=loaded.config.application.service,
            version=None,
            release_id=None,
            operation_id=None,
            database_directory=database.parent,
            database_target=loaded.config.application.database_volume_target,
            host_port=4510,
            container_port=loaded.config.application.candidate_container_port,
            health_url="http://127.0.0.1:4510/health",
        )
        self.new: ProductionApplicationHandle | None = None

    def _inspection(
        self,
        handle: ProductionApplicationHandle,
        *,
        running: bool,
    ) -> ProductionInspection:
        return ProductionInspection(
            handle=handle,
            running=running,
            mounts=(
                CandidateMount(
                    mount_type="bind",
                    source=str(self.loaded.config.database.path.parent),
                    destination=self.loaded.config.application.database_volume_target,
                    read_write=True,
                ),
            ),
            command=command_result("inspect"),
        )

    def discover_current(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionDiscovery:
        del cancellation_event
        self.calls.append("discover_current")
        return ProductionDiscovery(
            query=command_result("discover"),
            inspection=self._inspection(self.previous, running=self.previous_running),
        )

    def inspect(
        self,
        handle: ProductionApplicationHandle,
        *,
        expected_running: bool,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionInspection:
        del cancellation_event
        self.calls.append("inspect")
        running = self.previous_running if handle == self.previous else self.new_running
        if running is not expected_running:
            raise AssertionError("fake production running state mismatch")
        return self._inspection(handle, running=running)

    def stop_current(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionStop:
        del cancellation_event
        self.calls.append("stop_current")
        if self.fault == "stop_current":
            raise OperationFailedError(
                "injected stop failure",
                production_changed=False,
                previous_application_running=True,
                log_path=Path("/tmp/stop.log"),
                next_safe_action="Restart the previous application.",
            )
        self.previous_running = False
        return ProductionStop(
            handle=handle,
            command=command_result("stop"),
            inspection=self._inspection(handle, running=False),
        )

    def start_new(
        self,
        *,
        operation_id: str,
        release_id: str,
        version: str,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionStart:
        del cancellation_event
        self.calls.append("start_new")
        if self.fault == "start_new_unproven":
            result = command_result("start")
            proof = ProductionCleanupProof(
                container_absent=False,
                networks_absent=False,
                container_query=result,
                network_query=result,
            )
            cleanup = ProductionCleanup(
                presence_query=result,
                remove_command=None,
                compose_down=result,
                proof=proof,
            )
            raise ProductionStartError(
                "injected unproven startup cleanup",
                command=result,
                cleanup=cleanup,
            )
        if self.fault == "start_new":
            raise OperationFailedError(
                "injected new application startup failure",
                production_changed=True,
                previous_application_running=False,
                log_path=Path("/tmp/start.log"),
                next_safe_action="Roll back before traffic activation.",
            )
        handle = ProductionApplicationHandle(
            source="release",
            container_id="b" * 64,
            container_name=f"dploydb-release-{release_id[-8:]}",
            compose_project=f"dploydb-release-{release_id[-8:]}",
            compose_service=self.loaded.config.application.service,
            version=version,
            release_id=release_id,
            operation_id=operation_id,
            database_directory=self.loaded.config.database.path.parent,
            database_target=self.loaded.config.application.database_volume_target,
            host_port=4510,
            container_port=self.loaded.config.application.candidate_container_port,
            health_url="http://127.0.0.1:4510/health",
        )
        self.new = handle
        self.new_running = True
        inspection = self._inspection(handle, running=True)
        return ProductionStart(
            handle=handle,
            container_reference=handle.container_id,
            command=command_result("start"),
            inspection=inspection,
        )

    def collect_logs(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionLogs:
        del cancellation_event
        self.calls.append("collect_logs")
        if self.fault == "collect_logs":
            raise OperationFailedError(
                "injected log failure",
                production_changed=True,
                previous_application_running=False,
                log_path=Path("/tmp/logs"),
                next_safe_action="Roll back before traffic activation.",
            )
        return ProductionLogs(handle=handle, command=command_result("logs"))

    def remove_new(self, handle: ProductionApplicationHandle) -> ProductionCleanup:
        self.calls.append("remove_new")
        proof_value = self.fault != "remove_new"
        result = command_result("cleanup")
        proof = ProductionCleanupProof(
            container_absent=proof_value,
            networks_absent=proof_value,
            container_query=result,
            network_query=result,
        )
        cleanup = ProductionCleanup(
            presence_query=result,
            remove_command=result,
            compose_down=result,
            proof=proof,
        )
        if not proof_value:
            raise ProductionCleanupError(
                "injected new cleanup failure",
                command=result,
                cleanup=cleanup,
            )
        assert handle == self.new
        self.new_running = False
        return cleanup

    def restart_previous(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionRestart:
        del cancellation_event
        self.calls.append("restart_previous")
        if self.fault == "restart_previous":
            raise OperationFailedError(
                "injected restart failure",
                production_changed=True,
                previous_application_running=False,
                log_path=Path("/tmp/restart.log"),
                next_safe_action="Restart the exact previous container manually.",
            )
        assert handle == self.previous
        self.previous_running = True
        return ProductionRestart(
            handle=handle,
            command=command_result("restart"),
            inspection=self._inspection(handle, running=True),
        )


class FakeTraffic:
    def __init__(
        self,
        failures: Mapping[TrafficAction, list[CommandOutcome]] | None = None,
    ) -> None:
        self.failures = {key: list(value) for key, value in (failures or {}).items()}
        self.calls: list[TrafficAction] = []

    def _result(self, action: TrafficAction) -> TrafficHookResult:
        self.calls.append(action)
        selected = self.failures.get(action, [])
        outcome = selected.pop(0) if selected else CommandOutcome.SUCCEEDED
        if outcome is CommandOutcome.SUCCEEDED:
            exit_code = 0
        elif outcome is CommandOutcome.START_FAILED:
            exit_code = None
        elif outcome is CommandOutcome.CANCELLED:
            exit_code = None
        else:
            exit_code = 7
        cleanup_error = "descendant remained" if outcome is CommandOutcome.CLEANUP_FAILED else None
        return TrafficHookResult(
            action=action,
            command=command_result(
                action.value,
                outcome=outcome,
                exit_code=exit_code,
                cleanup_error=cleanup_error,
            ),
        )

    def enable_maintenance(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult:
        del cancellation_event
        return self._result(TrafficAction.ENABLE_MAINTENANCE)

    def disable_maintenance(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult:
        del cancellation_event
        return self._result(TrafficAction.DISABLE_MAINTENANCE)

    def activate_new(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult:
        del cancellation_event
        return self._result(TrafficAction.ACTIVATE_NEW)

    def activate_old(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult:
        del cancellation_event
        return self._result(TrafficAction.ACTIVATE_OLD)


class FakeDatabase(CutoverDatabase):
    def __init__(
        self,
        loaded: LoadedConfiguration,
        config_path: Path,
        *,
        fault: str | None = None,
    ) -> None:
        self.loaded = loaded
        self.config_path = config_path
        self.fault = fault
        self.calls: list[str] = []

    def create_final(
        self,
        *,
        operation_id: str,
        stopped: ProductionStop,
    ) -> BackupArtifact:
        self.calls.append("create_final")
        if self.fault == "create_final":
            raise OperationFailedError(
                "injected final backup failure",
                production_changed=False,
                previous_application_running=False,
                log_path=Path("/tmp/final-backup.log"),
                next_safe_action="Restart the previous application.",
            )
        return create_final_backup(
            self.loaded,
            operation_id=operation_id,
            stopped=stopped,
        )

    def migrate(
        self,
        *,
        operation_id: str,
        stopped: ProductionStop,
        final_backup: BackupArtifact,
        traffic_activated: bool,
        evidence_sink: Callable[[MigrationCommandEvidence], None],
        cancellation_event: threading.Event | None,
        log_path: Path,
    ) -> ProductionMigrationResult:
        self.calls.append("migrate")
        if self.fault == "migrate":
            with sqlite3.connect(self.loaded.config.database.path) as connection:
                connection.execute("CREATE TABLE partial_change(id INTEGER PRIMARY KEY)")
            raise OperationFailedError(
                "injected production migration failure",
                production_changed=True,
                previous_application_running=False,
                log_path=log_path,
                next_safe_action="Restore the final backup.",
            )
        return migrate_production_database(
            self.loaded,
            operation_id=operation_id,
            stopped=stopped,
            final_backup=final_backup,
            config_path=self.config_path,
            traffic_activated=traffic_activated,
            evidence_sink=evidence_sink,
            command_environment=dict(os.environ),
            cancellation_event=cancellation_event,
            log_path=log_path,
        )

    def restore(
        self,
        *,
        operation_id: str,
        stopped: ProductionStop,
        final_backup: BackupArtifact,
        traffic_activated: bool,
    ) -> VerifiedDatabaseRestoreResult:
        self.calls.append("restore")
        if self.fault == "restore":
            raise RecoveryRequiredError(
                "injected database restore failure",
                production_changed=True,
                previous_application_running=False,
                log_path=Path("/tmp/restore.log"),
                next_safe_action="Restore the final backup manually.",
            )
        return restore_final_backup(
            self.loaded,
            operation_id=operation_id,
            stopped=stopped,
            final_backup=final_backup,
            traffic_activated=traffic_activated,
        )


class FakeHealth:
    def __init__(self, *, fail_new: bool = False, fail_previous: bool = False) -> None:
        self.fail_new = fail_new
        self.fail_previous = fail_previous
        self.calls: list[str] = []

    def check_application(
        self,
        *,
        version: str,
        database_path: Path,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateHealthResult:
        del database_path, cancellation_event
        self.calls.append(version)
        if (version == "v2" and self.fail_new) or (version == "previous" and self.fail_previous):
            raise unhealthy_error()
        return healthy_result()


@dataclass(slots=True)
class Harness:
    dependencies: DeploymentDependencies
    production: FakeProduction
    traffic: FakeTraffic
    database: FakeDatabase
    health: FakeHealth


def harness(
    loaded: LoadedConfiguration,
    config_path: Path,
    *,
    pre_fail: bool = False,
    production_fault: str | None = None,
    database_fault: str | None = None,
    traffic_failures: Mapping[TrafficAction, list[CommandOutcome]] | None = None,
    fail_new_health: bool = False,
    fail_previous_health: bool = False,
) -> Harness:
    production = FakeProduction(loaded, fault=production_fault)
    traffic = FakeTraffic(traffic_failures)
    database = FakeDatabase(loaded, config_path, fault=database_fault)
    health = FakeHealth(
        fail_new=fail_new_health,
        fail_previous=fail_previous_health,
    )
    return Harness(
        dependencies=DeploymentDependencies(
            pre_cutover=FakePreCutover(fail=pre_fail),
            production=production,
            traffic=traffic,
            database=database,
            health=health,
        ),
        production=production,
        traffic=traffic,
        database=database,
        health=health,
    )


def database_state(path: Path) -> tuple[str, int, list[tuple[Any, ...]], set[str]]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        rows = connection.execute("SELECT * FROM notes ORDER BY id").fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
            )
        }
    return digest, version, rows, tables


def run_deploy(
    loaded: LoadedConfiguration,
    config_path: Path,
    selected: Harness,
) -> DeploymentResult:
    return deploy_configured_release(
        loaded,
        version="v2",
        config_path=config_path,
        dependencies=selected.dependencies,
    )


def test_successful_coordinator_activates_checked_release_in_exact_order(
    tmp_path: Path,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    selected = harness(loaded, config_path)

    result = run_deploy(loaded, config_path, selected)

    assert result.active is True
    assert result.operation.status is OperationStatus.SUCCEEDED
    assert result.release.status is DeploymentState.ACTIVE
    assert result.release.production_changed is True
    assert result.release.traffic_activated is True
    assert result.release.final_backup_id is not None
    assert result.release.new_application == selected.production.new
    assert [item.action for item in result.release.traffic_hooks] == [
        "enable_maintenance",
        "activate_new",
        "disable_maintenance",
    ]
    assert [item.role for item in result.release.health_checks] == ["new"]
    assert selected.production.previous_running is False
    assert selected.production.new_running is True
    assert selected.traffic.calls == [
        TrafficAction.ENABLE_MAINTENANCE,
        TrafficAction.ACTIVATE_NEW,
        TrafficAction.DISABLE_MAINTENANCE,
    ]
    assert selected.production.calls == [
        "discover_current",
        "stop_current",
        "start_new",
        "collect_logs",
    ]
    assert selected.database.calls == ["create_final", "migrate"]
    assert database_state(loaded.config.database.path)[1:] == (
        2,
        [(1, "preserved-v1-row", "general")],
        {"notes"},
    )
    pointers = ReleaseStore(loaded.config.state_directory, secrets=loaded.secrets).read_pointers()
    assert pointers is not None and pointers.active_release_id == result.release.release_id


def test_later_deploy_uses_active_release_exact_application_instead_of_discovery(
    tmp_path: Path,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    first = harness(loaded, config_path)
    active = run_deploy(loaded, config_path, first)
    assert active.release.new_application is not None

    second = harness(loaded, config_path, database_fault="create_final")
    second.production.previous = active.release.new_application
    second.production.previous_running = True

    rolled_back = run_deploy(loaded, config_path, second)

    assert rolled_back.rolled_back is True
    assert second.production.calls[0] == "inspect"
    assert "discover_current" not in second.production.calls
    assert rolled_back.release.previous_release_id == active.release.release_id
    assert rolled_back.release.previous_application == active.release.new_application
    assert second.production.previous_running is True


@pytest.mark.parametrize("failure", ["migrate", "final_health"])
def test_pretraffic_database_or_health_failure_restores_database_and_application(
    tmp_path: Path,
    failure: str,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    selected = harness(
        loaded,
        config_path,
        database_fault=("migrate" if failure == "migrate" else None),
        fail_new_health=failure == "final_health",
    )

    result = run_deploy(loaded, config_path, selected)

    assert result.rolled_back is True
    assert result.operation.status is OperationStatus.FAILED_SAFE
    assert result.operation.stage == "rolled_back"
    assert result.operation.safety.previous_application_running is True
    assert result.release.failure is not None
    assert result.release.traffic_hooks[-2].action == "activate_old"
    assert result.release.traffic_hooks[-1].action == "disable_maintenance"
    assert result.release.health_checks[-1].role == "previous"
    assert selected.production.previous_running is True
    assert selected.production.new_running is False
    assert selected.database.calls[-1] == "restore"
    assert selected.traffic.calls == [
        TrafficAction.ENABLE_MAINTENANCE,
        TrafficAction.ACTIVATE_OLD,
        TrafficAction.DISABLE_MAINTENANCE,
    ]
    state = database_state(loaded.config.database.path)
    assert state[1] == 1
    assert state[2] == [(1, "preserved-v1-row")]
    assert state[3] == {"notes"}
    assert "restart_previous" in selected.production.calls
    if failure == "final_health":
        assert "remove_new" in selected.production.calls


@pytest.mark.parametrize(
    ("production_fault", "database_fault", "expects_restore", "expects_new_cleanup"),
    [
        ("stop_current", None, False, False),
        (None, "create_final", False, False),
        ("start_new", None, True, False),
        ("collect_logs", None, True, True),
    ],
)
def test_each_safe_pretraffic_boundary_failure_completes_full_rollback(
    tmp_path: Path,
    production_fault: str | None,
    database_fault: str | None,
    expects_restore: bool,
    expects_new_cleanup: bool,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    before = database_state(loaded.config.database.path)
    selected = harness(
        loaded,
        config_path,
        production_fault=production_fault,
        database_fault=database_fault,
    )

    result = run_deploy(loaded, config_path, selected)

    assert result.rolled_back is True
    assert selected.production.previous_running is True
    assert selected.database.calls.count("restore") == int(expects_restore)
    assert ("remove_new" in selected.production.calls) is expects_new_cleanup
    assert database_state(loaded.config.database.path)[1:] == before[1:]
    assert selected.traffic.calls[-2:] == [
        TrafficAction.ACTIVATE_OLD,
        TrafficAction.DISABLE_MAINTENANCE,
    ]


def test_candidate_failure_is_terminal_before_any_production_boundary(tmp_path: Path) -> None:
    config_path, loaded = loaded_project(tmp_path)
    selected = harness(loaded, config_path, pre_fail=True)

    with pytest.raises(OperationFailedError, match="candidate rejected"):
        run_deploy(loaded, config_path, selected)

    assert selected.production.calls == []
    assert selected.traffic.calls == []
    assert selected.database.calls == []
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None
    assert latest.status is OperationStatus.FAILED_SAFE
    assert latest.safety.production_changed is False


def test_unproven_new_application_cleanup_stops_without_database_restore(
    tmp_path: Path,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    selected = harness(loaded, config_path, production_fault="start_new_unproven")

    with pytest.raises(RecoveryRequiredError, match="unproven startup cleanup"):
        run_deploy(loaded, config_path, selected)

    assert selected.database.calls == ["create_final", "migrate"]
    assert "restart_previous" not in selected.production.calls
    assert selected.production.previous_running is False
    assert database_state(loaded.config.database.path)[1] == 2


def test_maintenance_failure_never_stops_application_or_touches_database(
    tmp_path: Path,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    before = database_state(loaded.config.database.path)
    selected = harness(
        loaded,
        config_path,
        traffic_failures={TrafficAction.ENABLE_MAINTENANCE: [CommandOutcome.NONZERO_EXIT]},
    )

    with pytest.raises(ExternalCommandError, match="enable_maintenance"):
        run_deploy(loaded, config_path, selected)

    assert selected.production.calls == ["discover_current"]
    assert selected.database.calls == []
    assert selected.production.previous_running is True
    assert database_state(loaded.config.database.path) == before
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None and latest.status is OperationStatus.FAILED_SAFE
    assert selected.traffic.calls == [
        TrafficAction.ENABLE_MAINTENANCE,
        TrafficAction.DISABLE_MAINTENANCE,
    ]


def test_failed_activation_with_possible_side_effect_never_rolls_back_database(
    tmp_path: Path,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    selected = harness(
        loaded,
        config_path,
        traffic_failures={TrafficAction.ACTIVATE_NEW: [CommandOutcome.NONZERO_EXIT]},
    )

    with pytest.raises(RecoveryRequiredError, match="activation was attempted"):
        run_deploy(loaded, config_path, selected)

    assert selected.database.calls == ["create_final", "migrate"]
    assert "remove_new" not in selected.production.calls
    assert "restart_previous" not in selected.production.calls
    assert selected.production.new_running is True
    assert selected.production.previous_running is False
    assert database_state(loaded.config.database.path)[1] == 2
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None and latest.status is OperationStatus.RECOVERY_REQUIRED


def test_activation_start_failure_is_proven_pretraffic_and_rolls_back(tmp_path: Path) -> None:
    config_path, loaded = loaded_project(tmp_path)
    selected = harness(
        loaded,
        config_path,
        traffic_failures={TrafficAction.ACTIVATE_NEW: [CommandOutcome.START_FAILED]},
    )

    result = run_deploy(loaded, config_path, selected)

    assert result.rolled_back is True
    assert selected.database.calls[-1] == "restore"
    assert selected.production.previous_running is True
    assert database_state(loaded.config.database.path)[1] == 1


@pytest.mark.parametrize(
    ("production_fault", "database_fault", "expected_call"),
    [
        ("restart_previous", "migrate", "restart_previous"),
        (None, "restore", "restore"),
        ("remove_new", None, "remove_new"),
    ],
)
def test_unproven_rollback_step_ends_recovery_required(
    tmp_path: Path,
    production_fault: str | None,
    database_fault: str | None,
    expected_call: str,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    selected = harness(
        loaded,
        config_path,
        production_fault=production_fault,
        database_fault=database_fault,
        fail_new_health=(production_fault == "remove_new" or database_fault == "restore"),
    )

    with pytest.raises(RecoveryRequiredError, match="rollback could not be proven"):
        run_deploy(loaded, config_path, selected)

    assert expected_call in selected.production.calls + selected.database.calls
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None and latest.status is OperationStatus.RECOVERY_REQUIRED
    assert latest.safety.recovery_required is True


@pytest.mark.parametrize(
    ("failed_action", "fail_previous_health"),
    [
        (TrafficAction.ACTIVATE_OLD, False),
        (TrafficAction.DISABLE_MAINTENANCE, False),
        (None, True),
    ],
)
def test_unproven_rollback_traffic_or_previous_health_requires_recovery(
    tmp_path: Path,
    failed_action: TrafficAction | None,
    fail_previous_health: bool,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    failures = {} if failed_action is None else {failed_action: [CommandOutcome.NONZERO_EXIT]}
    selected = harness(
        loaded,
        config_path,
        database_fault="migrate",
        traffic_failures=failures,
        fail_previous_health=fail_previous_health,
    )

    with pytest.raises(RecoveryRequiredError, match="rollback could not be proven"):
        run_deploy(loaded, config_path, selected)

    assert selected.database.calls[-1] == "restore"
    assert database_state(loaded.config.database.path)[1] == 1
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None and latest.status is OperationStatus.RECOVERY_REQUIRED


def test_failed_maintenance_enable_and_cleanup_requires_recovery_without_stop(
    tmp_path: Path,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    selected = harness(
        loaded,
        config_path,
        traffic_failures={
            TrafficAction.ENABLE_MAINTENANCE: [CommandOutcome.NONZERO_EXIT],
            TrafficAction.DISABLE_MAINTENANCE: [CommandOutcome.NONZERO_EXIT],
        },
    )

    with pytest.raises(RecoveryRequiredError, match="maintenance cleanup also failed"):
        run_deploy(loaded, config_path, selected)

    assert selected.production.calls == ["discover_current"]
    assert selected.database.calls == []
    assert selected.production.previous_running is True


def test_post_activation_maintenance_failure_keeps_new_database_and_application(
    tmp_path: Path,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    selected = harness(
        loaded,
        config_path,
        traffic_failures={TrafficAction.DISABLE_MAINTENANCE: [CommandOutcome.NONZERO_EXIT]},
    )

    with pytest.raises(RecoveryRequiredError, match="maintenance disable failed"):
        run_deploy(loaded, config_path, selected)

    assert selected.database.calls == ["create_final", "migrate"]
    assert selected.production.new_running is True
    assert selected.production.previous_running is False
    assert "remove_new" not in selected.production.calls
    assert database_state(loaded.config.database.path)[1] == 2
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None and latest.status is OperationStatus.RECOVERY_REQUIRED


def test_cross_sink_evidence_redacts_secrets(tmp_path: Path) -> None:
    config_path, _loaded = loaded_project(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    secret = "deploy-super-secret-value"
    raw["traffic"]["maintenance_on_command"] = [
        sys.executable,
        "-c",
        "pass",
        "${DEPLOY_SECRET}",
    ]
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    loaded = load_configuration(config_path, environment={"DEPLOY_SECRET": secret})
    selected = harness(loaded, config_path)

    result = run_deploy(loaded, config_path, selected)

    assert result.active is True
    produced = "\n".join(
        path.read_text(encoding="utf-8")
        for path in loaded.config.state_directory.rglob("*")
        if path.is_file()
    )
    assert secret not in produced


def test_release_sink_failure_becomes_recovery_required_without_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, loaded = loaded_project(tmp_path)
    selected = harness(loaded, config_path)
    original = ReleaseStore.transition
    injected = False

    def fail_snapshot_once(
        store: ReleaseStore,
        release_id: str,
        *,
        status: DeploymentState,
        **changes: object,
    ) -> ReleaseManifest:
        nonlocal injected
        if status is DeploymentState.SNAPSHOT_VERIFIED and not injected:
            injected = True
            raise OSError("injected release manifest persistence failure")
        selected_original = cast(Callable[..., ReleaseManifest], original)
        return selected_original(store, release_id, status=status, **changes)

    monkeypatch.setattr(ReleaseStore, "transition", fail_snapshot_once)

    with pytest.raises(RecoveryRequiredError, match="boundary failure"):
        run_deploy(loaded, config_path, selected)

    assert injected is True
    assert selected.production.calls == []
    assert selected.traffic.calls == []
    assert selected.database.calls == []
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None and latest.status is OperationStatus.RECOVERY_REQUIRED
