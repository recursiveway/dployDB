"""Release-aware manual restore preview and selection tests."""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from dploydb.backup import create_verified_backup
from dploydb.config import STARTER_CONFIGURATION, LoadedConfiguration, load_configuration
from dploydb.errors import OperationFailedError, RecoveryRequiredError, SafetyCheckError
from dploydb.health import (
    BoundedResponseEvidence,
    CandidateHealthResult,
    HealthAttemptEvidence,
    HealthAttemptOutcome,
    ReadinessEvidence,
)
from dploydb.manual_restore import (
    DATA_LOSS_WARNING,
    ManualRestoreDependencies,
    preview_configured_restore,
    restore_configured_release,
)
from dploydb.models import (
    BackupPurpose,
    DeploymentState,
    LockOwnerMetadata,
    LockOwnerState,
    OperationStatus,
    ProcessIdentity,
    ProductionApplicationHandle,
    ReleaseManifest,
    SafetyFacts,
    utc_now,
)
from dploydb.recovery import (
    RecoveryDependencies,
    RecoveryDisposition,
    preview_configured_recovery,
    recover_configured_deployment,
)
from dploydb.releases import ReleaseStore
from dploydb.runners.base import (
    CandidateMount,
    ProductionCleanupProof,
    ProductionInspection,
    ProductionRestart,
    ProductionStop,
)
from dploydb.state import StateStore
from dploydb.storage.local import LocalBackupStorage
from dploydb.subprocesses import CapturedOutput, CommandOutcome, CommandResult
from dploydb.traffic import TrafficAction, TrafficHookResult


def loaded_project(tmp_path: Path) -> tuple[Path, LoadedConfiguration]:
    database = tmp_path / "data" / "app.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("INSERT INTO notes(body) VALUES ('previous-release-state')")
    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = "manual-restore-test"
    value["state_directory"] = str(tmp_path / "state")
    value["database"]["path"] = str(database)
    value["migration"]["command"] = [sys.executable, "-c", "pass"]
    value["application"]["compose_file"] = str(tmp_path / "compose.yaml")
    value["application"].pop("smoke_command", None)
    value["application"]["production_project"] = "manual-restore-current"
    value["application"]["production_port"] = 4510
    value["application"]["production_health_url"] = "http://127.0.0.1:4510/health"
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
    return path, load_configuration(path)


def application_handle(
    tmp_path: Path,
    *,
    release_id: str,
    operation_id: str,
    version: str,
    token: str,
) -> ProductionApplicationHandle:
    return ProductionApplicationHandle(
        source="release",
        container_id=token * 64,
        container_name=f"release-{token}",
        compose_project=f"release-{token}",
        compose_service="app",
        version=version,
        release_id=release_id,
        operation_id=operation_id,
        database_directory=(tmp_path / "data").resolve(),
        database_target="/data",
        host_port=4510,
        container_port=8080,
        health_url="http://127.0.0.1:4510/health",
    )


def bootstrap_handle(tmp_path: Path) -> ProductionApplicationHandle:
    return ProductionApplicationHandle(
        source="bootstrap",
        container_id="0" * 64,
        container_name="bootstrap",
        compose_project="manual-restore-current",
        compose_service="app",
        database_directory=(tmp_path / "data").resolve(),
        database_target="/data",
        host_port=4510,
        container_port=8080,
        health_url="http://127.0.0.1:4510/health",
    )


def create_active(
    releases: ReleaseStore,
    tmp_path: Path,
    *,
    operation_id: str,
    version: str,
    previous: ProductionApplicationHandle,
    final_backup_id: str,
    final_sha256: str,
    token: str,
) -> tuple[ReleaseManifest, ProductionApplicationHandle]:
    manifest = releases.create_release(
        operation_id=operation_id,
        project="manual-restore-test",
        requested_version=version,
        configuration_fingerprint="f" * 64,
        operation_log_path=(tmp_path / "state" / "operations" / operation_id / "events.jsonl"),
        previous_application=previous,
    )
    releases.transition(manifest.release_id, status=DeploymentState.PREFLIGHT_PASSED)
    releases.transition(
        manifest.release_id,
        status=DeploymentState.SNAPSHOT_VERIFIED,
        rehearsal_backup_id="backup_" + token * 32,
        rehearsal_backup_sha256=token * 64,
    )
    releases.transition(manifest.release_id, status=DeploymentState.REHEARSAL_PASSED)
    releases.transition(manifest.release_id, status=DeploymentState.CANDIDATE_HEALTHY)
    releases.transition(manifest.release_id, status=DeploymentState.MAINTENANCE_ENABLED)
    releases.transition(manifest.release_id, status=DeploymentState.CURRENT_APP_STOPPED)
    releases.transition(
        manifest.release_id,
        status=DeploymentState.FINAL_SNAPSHOT_VERIFIED,
        final_backup_id=final_backup_id,
        final_backup_sha256=final_sha256,
    )
    releases.record_recovery_intent(manifest.release_id, production_migration_started=True)
    releases.transition(
        manifest.release_id,
        status=DeploymentState.PRODUCTION_MIGRATED,
        production_changed=True,
    )
    new = application_handle(
        tmp_path,
        release_id=manifest.release_id,
        operation_id=operation_id,
        version=version,
        token=token,
    )
    releases.transition(
        manifest.release_id,
        status=DeploymentState.NEW_APP_HEALTHY,
        new_application=new,
        production_health_passed=True,
    )
    releases.record_recovery_intent(manifest.release_id, traffic_activation_attempted=True)
    releases.transition(
        manifest.release_id,
        status=DeploymentState.TRAFFIC_ACTIVATED,
        traffic_activated=True,
    )
    active = releases.transition(manifest.release_id, status=DeploymentState.ACTIVE)
    releases.activate_release(active.release_id)
    return active, new


def two_release_history(
    tmp_path: Path,
) -> tuple[
    LoadedConfiguration,
    ReleaseManifest,
    ReleaseManifest,
    ProductionApplicationHandle,
]:
    _config_path, loaded = loaded_project(tmp_path)
    releases = ReleaseStore(loaded.config.state_directory, secrets=loaded.secrets)
    first, first_app = create_active(
        releases,
        tmp_path,
        operation_id="op_" + "1" * 32,
        version="v1",
        previous=bootstrap_handle(tmp_path),
        final_backup_id="backup_" + "8" * 32,
        final_sha256="8" * 64,
        token="1",
    )
    final = create_verified_backup(
        loaded.config.database.path,
        project=loaded.config.project,
        purpose=BackupPurpose.FINAL,
        storage=LocalBackupStorage(loaded.config.backup.local_directory),
        operation_id="op_" + "2" * 32,
        metadata_source_path=loaded.config.database.path,
    )
    with sqlite3.connect(loaded.config.database.path) as connection:
        connection.execute("ALTER TABLE notes ADD COLUMN category TEXT DEFAULT 'general'")
    second, second_app = create_active(
        releases,
        tmp_path,
        operation_id="op_" + "2" * 32,
        version="v2",
        previous=first_app,
        final_backup_id=final.metadata.backup_id,
        final_sha256=final.metadata.sha256,
        token="2",
    )
    return loaded, first, second, second_app


def test_preview_maps_previous_release_to_active_final_backup(tmp_path: Path) -> None:
    loaded, first, second, second_app = two_release_history(tmp_path)

    preview = preview_configured_restore(loaded, first.release_id)

    assert preview.active_release == second
    assert preview.selected_release == first
    assert preview.current_application == second_app
    assert preview.selected_application == first.new_application
    assert preview.selected_backup.metadata.backup_id == second.final_backup_id
    assert preview.selected_backup.metadata.purpose is BackupPurpose.FINAL
    assert preview.as_dict()["warning"] == DATA_LOSS_WARNING
    assert preview.as_dict()["pre_restore_backup_required"] is True


def test_preview_refuses_active_or_unprotected_release(tmp_path: Path) -> None:
    loaded, _first, second, _second_app = two_release_history(tmp_path)

    with pytest.raises(SafetyCheckError, match="immediately previous"):
        preview_configured_restore(loaded, second.release_id)


def test_preview_refuses_tampered_selected_backup(tmp_path: Path) -> None:
    loaded, first, second, _second_app = two_release_history(tmp_path)
    storage = LocalBackupStorage(loaded.config.backup.local_directory)
    assert second.final_backup_id is not None
    artifact = storage.get(second.final_backup_id)
    artifact.database_path.write_bytes(b"not sqlite")

    with pytest.raises(SafetyCheckError):
        preview_configured_restore(loaded, first.release_id)


def command_result(
    action: str,
    *,
    outcome: CommandOutcome = CommandOutcome.SUCCEEDED,
) -> CommandResult:
    empty = CapturedOutput(text="", total_bytes=0, retained_bytes=0, truncated=False)
    exit_code = 0 if outcome is CommandOutcome.SUCCEEDED else None
    return CommandResult(
        command=(action,),
        working_directory="/tmp",
        environment_keys=(),
        outcome=outcome,
        exit_code=exit_code,
        stdout=empty,
        stderr=empty,
        duration_seconds=0.01,
        start_error="not started" if outcome is CommandOutcome.START_FAILED else None,
    )


class FakeProduction:
    def __init__(
        self,
        loaded: LoadedConfiguration,
        current: ProductionApplicationHandle,
        selected: ProductionApplicationHandle,
    ) -> None:
        self.loaded = loaded
        self.current = current
        self.selected = selected
        self.running = {current.container_id: True, selected.container_id: False}
        self.calls: list[tuple[str, str]] = []

    def _inspection(self, handle: ProductionApplicationHandle) -> ProductionInspection:
        return ProductionInspection(
            handle=handle,
            running=self.running[handle.container_id],
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

    def inspect(
        self,
        handle: ProductionApplicationHandle,
        *,
        expected_running: bool,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionInspection:
        del cancellation_event
        self.calls.append(("inspect", handle.container_id))
        inspection = self._inspection(handle)
        if inspection.running is not expected_running:
            raise AssertionError("unexpected fake running state")
        return inspection

    def inspect_live(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionInspection:
        del cancellation_event
        self.calls.append(("inspect_live", handle.container_id))
        return self._inspection(handle)

    def prove_release_absent(
        self,
        *,
        release_id: str,
        version: str,
    ) -> ProductionCleanupProof:
        del release_id, version
        command = command_result("prove_absent")
        return ProductionCleanupProof(
            container_absent=True,
            networks_absent=True,
            container_query=command,
            network_query=command,
        )

    def stop_current(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionStop:
        del cancellation_event
        self.calls.append(("stop", handle.container_id))
        self.running[handle.container_id] = False
        return ProductionStop(
            handle=handle,
            command=command_result("stop"),
            inspection=self._inspection(handle),
        )

    def restart_previous(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionRestart:
        del cancellation_event
        self.calls.append(("restart", handle.container_id))
        self.running[handle.container_id] = True
        return ProductionRestart(
            handle=handle,
            command=command_result("restart"),
            inspection=self._inspection(handle),
        )


class FakeTraffic:
    def __init__(
        self,
        failures: Mapping[TrafficAction, list[CommandOutcome]] | None = None,
    ) -> None:
        self.failures = {key: list(value) for key, value in (failures or {}).items()}
        self.calls: list[TrafficAction] = []

    def _run(self, action: TrafficAction) -> TrafficHookResult:
        self.calls.append(action)
        outcomes = self.failures.get(action, [])
        outcome = outcomes.pop(0) if outcomes else CommandOutcome.SUCCEEDED
        return TrafficHookResult(
            action=action, command=command_result(action.value, outcome=outcome)
        )

    def enable_maintenance(
        self, *, cancellation_event: threading.Event | None = None
    ) -> TrafficHookResult:
        del cancellation_event
        return self._run(TrafficAction.ENABLE_MAINTENANCE)

    def disable_maintenance(
        self, *, cancellation_event: threading.Event | None = None
    ) -> TrafficHookResult:
        del cancellation_event
        return self._run(TrafficAction.DISABLE_MAINTENANCE)

    def activate_new(
        self, *, cancellation_event: threading.Event | None = None
    ) -> TrafficHookResult:
        del cancellation_event
        return self._run(TrafficAction.ACTIVATE_NEW)

    def activate_old(
        self, *, cancellation_event: threading.Event | None = None
    ) -> TrafficHookResult:
        del cancellation_event
        return self._run(TrafficAction.ACTIVATE_OLD)


class FakeHealth:
    def __init__(self) -> None:
        self.versions: list[str] = []

    def check_application(
        self,
        *,
        version: str,
        database_path: Path,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateHealthResult:
        del database_path, cancellation_event
        self.versions.append(version)
        body = BoundedResponseEvidence(
            text='{"ok":true}', total_bytes=11, retained_bytes=11, truncated=False
        )
        attempt = HealthAttemptEvidence(
            attempt=1,
            outcome=HealthAttemptOutcome.HEALTHY,
            status_code=200,
            body=body,
            reason="healthy",
            duration_seconds=0.01,
        )
        return CandidateHealthResult(
            readiness=ReadinessEvidence(
                url="http://127.0.0.1:4510/health",
                healthy=True,
                attempt_count=1,
                last_attempt=attempt,
                duration_seconds=0.01,
                reason="healthy",
            ),
            smoke=None,
        )


@dataclass(slots=True)
class RestoreHarness:
    dependencies: ManualRestoreDependencies
    production: FakeProduction
    traffic: FakeTraffic
    health: FakeHealth


def restore_harness(
    loaded: LoadedConfiguration,
    first: ReleaseManifest,
    second: ReleaseManifest,
    *,
    traffic_failures: Mapping[TrafficAction, list[CommandOutcome]] | None = None,
) -> RestoreHarness:
    assert first.new_application is not None
    assert second.new_application is not None
    production = FakeProduction(loaded, second.new_application, first.new_application)
    traffic = FakeTraffic(traffic_failures)
    health = FakeHealth()
    return RestoreHarness(
        dependencies=ManualRestoreDependencies(
            production=production,  # type: ignore[arg-type]
            traffic=traffic,
            health=health,
        ),
        production=production,
        traffic=traffic,
        health=health,
    )


def columns(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info(notes)")]


def test_manual_restore_backs_up_current_and_restores_previous_end_to_end(
    tmp_path: Path,
) -> None:
    loaded, first, second, _second_app = two_release_history(tmp_path)
    harness = restore_harness(loaded, first, second)

    result = restore_configured_release(
        loaded,
        first.release_id,
        config_path=tmp_path / "dploydb.yaml",
        dependencies=harness.dependencies,
    )

    assert result.operation.status is OperationStatus.SUCCEEDED
    assert result.operation.stage == "manual_restore_completed"
    assert result.pointers.active_release_id == first.release_id
    assert result.pointers.previous_release_id == second.release_id
    assert columns(loaded.config.database.path) == ["id", "body"]
    assert result.pre_restore_backup.metadata.purpose is BackupPurpose.PRE_RESTORE
    with sqlite3.connect(result.pre_restore_backup.database_path) as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(notes)")] == [
            "id",
            "body",
            "category",
        ]
    assert harness.production.running[first.new_application.container_id] is True  # type: ignore[union-attr]
    assert harness.production.running[second.new_application.container_id] is False  # type: ignore[union-attr]
    assert harness.traffic.calls == [
        TrafficAction.ENABLE_MAINTENANCE,
        TrafficAction.ACTIVATE_OLD,
        TrafficAction.DISABLE_MAINTENANCE,
    ]
    assert harness.health.versions == ["v1"]


def test_manual_restore_failure_before_traffic_restores_pre_restore_state(
    tmp_path: Path,
) -> None:
    loaded, first, second, _second_app = two_release_history(tmp_path)
    harness = restore_harness(loaded, first, second)

    def fail(stage: str) -> None:
        if stage == "after_database_restore":
            raise OSError("injected post-restore failure")

    with pytest.raises(OperationFailedError, match="original database and application"):
        restore_configured_release(
            loaded,
            first.release_id,
            config_path=tmp_path / "dploydb.yaml",
            dependencies=harness.dependencies,
            fault_injector=fail,
        )

    assert columns(loaded.config.database.path) == ["id", "body", "category"]
    assert harness.production.running[second.new_application.container_id] is True  # type: ignore[union-attr]
    pointers = ReleaseStore(loaded.config.state_directory, secrets=loaded.secrets).read_pointers()
    assert pointers is not None and pointers.active_release_id == second.release_id
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None and latest.status is OperationStatus.FAILED_SAFE


def test_manual_restore_post_traffic_failure_never_rolls_database_back(
    tmp_path: Path,
) -> None:
    loaded, first, second, _second_app = two_release_history(tmp_path)
    harness = restore_harness(loaded, first, second)

    def fail(stage: str) -> None:
        if stage == "after_traffic_activation":
            raise OSError("injected post-traffic failure")

    with pytest.raises(RecoveryRequiredError, match="requires recovery"):
        restore_configured_release(
            loaded,
            first.release_id,
            config_path=tmp_path / "dploydb.yaml",
            dependencies=harness.dependencies,
            fault_injector=fail,
        )

    assert columns(loaded.config.database.path) == ["id", "body"]
    latest = StateStore(loaded.config.state_directory, secrets=loaded.secrets).latest_operation()
    assert latest is not None and latest.status is OperationStatus.RECOVERY_REQUIRED


def interrupted_after_migration(
    loaded: LoadedConfiguration,
    previous: ProductionApplicationHandle,
) -> ReleaseManifest:
    state = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    releases = ReleaseStore(loaded.config.state_directory, secrets=loaded.secrets)
    operation = state.create_operation(
        operation_type="deploy",
        project=loaded.config.project,
        configuration_fingerprint="f" * 64,
    )
    release = releases.create_release(
        operation_id=operation.operation_id,
        project=loaded.config.project,
        requested_version="v3",
        configuration_fingerprint="f" * 64,
        operation_log_path=state.operation_paths(operation.operation_id).events,
        previous_application=previous,
    )

    def stage(status: DeploymentState, **changes: object) -> None:
        state.transition(
            operation.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage=status.value,
            message=f"Reached {status.value} before injected crash.",
        )
        releases.transition(release.release_id, status=status, **changes)

    stage(DeploymentState.PREFLIGHT_PASSED)
    stage(
        DeploymentState.SNAPSHOT_VERIFIED,
        rehearsal_backup_id="backup_" + "a" * 32,
        rehearsal_backup_sha256="a" * 64,
    )
    stage(DeploymentState.REHEARSAL_PASSED)
    stage(DeploymentState.CANDIDATE_HEALTHY)
    stage(DeploymentState.MAINTENANCE_ENABLED)
    stage(DeploymentState.CURRENT_APP_STOPPED)
    final = create_verified_backup(
        loaded.config.database.path,
        project=loaded.config.project,
        purpose=BackupPurpose.FINAL,
        storage=LocalBackupStorage(loaded.config.backup.local_directory),
        operation_id=operation.operation_id,
        metadata_source_path=loaded.config.database.path,
    )
    stage(
        DeploymentState.FINAL_SNAPSHOT_VERIFIED,
        final_backup_id=final.metadata.backup_id,
        final_backup_sha256=final.metadata.sha256,
    )
    releases.record_recovery_intent(
        release.release_id,
        production_migration_started=True,
    )
    state.transition(
        operation.operation_id,
        status=OperationStatus.IN_PROGRESS,
        stage="production_migration_started",
        message="Production migration is about to start.",
        safety=SafetyFacts(
            production_changed=True,
            previous_application_running=False,
            recovery_required=False,
        ),
    )
    with sqlite3.connect(loaded.config.database.path) as connection:
        connection.execute("ALTER TABLE notes ADD COLUMN v3_extra TEXT")
    return releases.read_manifest(release.release_id)


def recovery_harness(
    loaded: LoadedConfiguration,
    previous: ProductionApplicationHandle,
    older: ProductionApplicationHandle,
) -> tuple[RecoveryDependencies, FakeProduction, FakeTraffic, FakeHealth]:
    production = FakeProduction(loaded, previous, older)
    production.running[previous.container_id] = False
    traffic = FakeTraffic()
    health = FakeHealth()
    dependencies = RecoveryDependencies(
        production=production,  # type: ignore[arg-type]
        traffic=traffic,
        health=health,
    )
    return dependencies, production, traffic, health


def write_stale_lock_owner(
    loaded: LoadedConfiguration,
    *,
    operation_id: str,
    owner_token: str = "e",
) -> None:
    owner = LockOwnerMetadata(
        owner_id="lock_" + owner_token * 32,
        operation_id=operation_id,
        operation_type="deploy",
        process=ProcessIdentity(pid=999_999, hostname="dead-test-process"),
        state=LockOwnerState.ACTIVE,
        acquired_at=utc_now(),
    )
    path = loaded.config.state_directory / "deployment-lock-owner.json"
    path.write_text(owner.model_dump_json() + "\n", encoding="utf-8")
    path.chmod(0o600)


def continue_to_interrupted_successful_activation(
    loaded: LoadedConfiguration,
    release: ReleaseManifest,
) -> tuple[ReleaseManifest, ProductionApplicationHandle]:
    state = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    releases = ReleaseStore(loaded.config.state_directory, secrets=loaded.secrets)
    state.transition(
        release.operation_id,
        status=OperationStatus.IN_PROGRESS,
        stage=DeploymentState.PRODUCTION_MIGRATED.value,
        message="Production migration completed.",
        safety=SafetyFacts(
            production_changed=True,
            previous_application_running=False,
            recovery_required=False,
        ),
    )
    release = releases.transition(
        release.release_id,
        status=DeploymentState.PRODUCTION_MIGRATED,
        production_changed=True,
    )
    new = application_handle(
        loaded.config.database.path.parent.parent,
        release_id=release.release_id,
        operation_id=release.operation_id,
        version="v3",
        token="3",
    )
    state.transition(
        release.operation_id,
        status=OperationStatus.IN_PROGRESS,
        stage=DeploymentState.NEW_APP_HEALTHY.value,
        message="New application passed final health.",
        safety=SafetyFacts(
            production_changed=True,
            previous_application_running=False,
            recovery_required=False,
        ),
    )
    release = releases.transition(
        release.release_id,
        status=DeploymentState.NEW_APP_HEALTHY,
        new_application=new,
        production_health_passed=True,
    )
    releases.record_recovery_intent(
        release.release_id,
        traffic_activation_attempted=True,
    )
    state.transition(
        release.operation_id,
        status=OperationStatus.IN_PROGRESS,
        stage="traffic_activation_started",
        message="Traffic activation is about to start.",
        safety=SafetyFacts(
            production_changed=True,
            previous_application_running=False,
            recovery_required=False,
        ),
    )
    hook = TrafficHookResult(
        action=TrafficAction.ACTIVATE_NEW,
        command=command_result("activate_new"),
    )
    state.append_event(
        release.operation_id,
        message="Traffic activation succeeded before the crash.",
        evidence={"traffic_hook": hook.as_evidence()},
    )
    return releases.read_manifest(release.release_id), new


def test_recover_after_production_migration_restores_previous_and_preserves_failure(
    tmp_path: Path,
) -> None:
    loaded, first, second, _second_app = two_release_history(tmp_path)
    assert first.new_application is not None
    assert second.new_application is not None
    interrupted = interrupted_after_migration(loaded, second.new_application)
    dependencies, production, traffic, health = recovery_harness(
        loaded,
        second.new_application,
        first.new_application,
    )

    result = recover_configured_deployment(
        loaded,
        config_path=tmp_path / "dploydb.yaml",
        dependencies=dependencies,
    )

    assert result.release.release_id == interrupted.release_id
    assert result.release.status is DeploymentState.ROLLED_BACK
    assert result.release.recovery_failure is not None
    assert result.release.recovery_operation_id == result.operation.operation_id
    assert result.operation.status is OperationStatus.SUCCEEDED
    assert columns(loaded.config.database.path) == ["id", "body", "category"]
    assert production.running[second.new_application.container_id] is True
    assert traffic.calls == [
        TrafficAction.ACTIVATE_OLD,
        TrafficAction.DISABLE_MAINTENANCE,
    ]
    assert health.versions == ["v2"]
    follow_up = preview_configured_recovery(
        loaded,
        config_path=tmp_path / "dploydb.yaml",
        dependencies=dependencies,
    )
    assert follow_up.disposition is RecoveryDisposition.NO_ACTION


def test_recover_acknowledges_exact_stale_owner_from_interrupted_deploy(
    tmp_path: Path,
) -> None:
    loaded, first, second, _second_app = two_release_history(tmp_path)
    assert first.new_application is not None
    assert second.new_application is not None
    interrupted = interrupted_after_migration(loaded, second.new_application)
    write_stale_lock_owner(loaded, operation_id=interrupted.operation_id)
    dependencies, _production, _traffic, _health = recovery_harness(
        loaded,
        second.new_application,
        first.new_application,
    )

    result = recover_configured_deployment(
        loaded,
        config_path=tmp_path / "dploydb.yaml",
        dependencies=dependencies,
    )

    assert result.release.status is DeploymentState.ROLLED_BACK
    assert result.operation.status is OperationStatus.SUCCEEDED


def test_recover_refuses_stale_owner_from_unrelated_operation(tmp_path: Path) -> None:
    loaded, first, second, _second_app = two_release_history(tmp_path)
    assert first.new_application is not None
    assert second.new_application is not None
    interrupted = interrupted_after_migration(loaded, second.new_application)
    write_stale_lock_owner(loaded, operation_id="op_" + "9" * 32)
    dependencies, production, _traffic, _health = recovery_harness(
        loaded,
        second.new_application,
        first.new_application,
    )

    with pytest.raises(RecoveryRequiredError, match="cannot be correlated"):
        recover_configured_deployment(
            loaded,
            config_path=tmp_path / "dploydb.yaml",
            dependencies=dependencies,
        )

    current = StateStore(loaded.config.state_directory, secrets=loaded.secrets).read_manifest(
        interrupted.operation_id
    )
    assert current.status is OperationStatus.IN_PROGRESS
    assert production.calls == [("inspect_live", second.new_application.container_id)]


def test_repeated_recover_skips_already_restored_database_after_interruption(
    tmp_path: Path,
) -> None:
    loaded, first, second, _second_app = two_release_history(tmp_path)
    assert first.new_application is not None
    assert second.new_application is not None
    interrupted_after_migration(loaded, second.new_application)
    dependencies, production, _traffic, _health = recovery_harness(
        loaded,
        second.new_application,
        first.new_application,
    )

    def interrupt(stage: str) -> None:
        if stage == "after_restore_final_backup":
            raise OSError("injected recovery interruption")

    with pytest.raises(RecoveryRequiredError, match="could not be proven"):
        recover_configured_deployment(
            loaded,
            config_path=tmp_path / "dploydb.yaml",
            dependencies=dependencies,
            fault_injector=interrupt,
        )

    assert columns(loaded.config.database.path) == ["id", "body", "category"]
    plan = preview_configured_recovery(
        loaded,
        config_path=tmp_path / "dploydb.yaml",
        dependencies=dependencies,
    )
    assert "restore_final_backup" not in [action.value for action in plan.actions]

    result = recover_configured_deployment(
        loaded,
        config_path=tmp_path / "dploydb.yaml",
        dependencies=dependencies,
    )

    assert result.release.status is DeploymentState.ROLLED_BACK
    assert production.running[second.new_application.container_id] is True


def test_recover_completes_checked_new_release_after_durable_activation_success(
    tmp_path: Path,
) -> None:
    loaded, first, second, _second_app = two_release_history(tmp_path)
    assert first.new_application is not None
    assert second.new_application is not None
    interrupted = interrupted_after_migration(loaded, second.new_application)
    interrupted, new = continue_to_interrupted_successful_activation(loaded, interrupted)
    dependencies, production, traffic, health = recovery_harness(
        loaded,
        second.new_application,
        first.new_application,
    )
    production.running[new.container_id] = True

    result = recover_configured_deployment(
        loaded,
        config_path=tmp_path / "dploydb.yaml",
        dependencies=dependencies,
    )

    assert result.release.release_id == interrupted.release_id
    assert result.release.status is DeploymentState.ACTIVE
    assert result.release.recovery_failure is not None
    assert result.release.traffic_activated is True
    assert traffic.calls == [TrafficAction.DISABLE_MAINTENANCE]
    assert health.versions == ["v3"]
    pointers = ReleaseStore(loaded.config.state_directory, secrets=loaded.secrets).read_pointers()
    assert pointers is not None and pointers.active_release_id == interrupted.release_id
