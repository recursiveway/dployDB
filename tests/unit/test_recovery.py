"""Pure recovery decision matrix for interrupted cutovers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dploydb.models import (
    DeploymentState,
    OperationEvent,
    OperationManifest,
    OperationStatus,
    ProcessIdentity,
    ProductionApplicationHandle,
    ReleaseManifest,
    SafetyFacts,
)
from dploydb.recovery import (
    ApplicationRuntimeState,
    RecoveryAction,
    RecoveryDisposition,
    RecoveryLiveState,
    build_recovery_plan,
)

OPERATION_ID = "op_" + "1" * 32
RELEASE_ID = "release_" + "2" * 32
BACKUP_ID = "backup_" + "3" * 32
FINAL_BACKUP_ID = "backup_" + "4" * 32
SHA256 = "a" * 64
FINAL_SHA256 = "b" * 64
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def handle(tmp_path: Path, *, previous: bool) -> ProductionApplicationHandle:
    if previous:
        return ProductionApplicationHandle(
            source="bootstrap",
            container_id="c" * 64,
            container_name="example-app",
            compose_project="example",
            compose_service="app",
            database_directory=(tmp_path / "data").resolve(),
            database_target="/data",
            host_port=4510,
            container_port=8080,
            health_url="http://127.0.0.1:4510/health",
        )
    return ProductionApplicationHandle(
        source="release",
        container_id="d" * 64,
        container_name="dploydb-release",
        compose_project="dploydb-release",
        compose_service="app",
        version="v2",
        release_id=RELEASE_ID,
        operation_id=OPERATION_ID,
        database_directory=(tmp_path / "data").resolve(),
        database_target="/data",
        host_port=4510,
        container_port=8080,
        health_url="http://127.0.0.1:4510/health",
    )


def release(
    tmp_path: Path,
    state: DeploymentState,
    *,
    migration_started: bool = False,
    activation_attempted: bool = False,
    traffic_activated: bool = False,
    protocol: int | None = 2,
) -> ReleaseManifest:
    production_changed = (
        state
        in {
            DeploymentState.PRODUCTION_MIGRATED,
            DeploymentState.NEW_APP_HEALTHY,
            DeploymentState.TRAFFIC_ACTIVATED,
            DeploymentState.ACTIVE,
            DeploymentState.ROLLBACK_STARTED,
            DeploymentState.RECOVERY_REQUIRED,
        }
        or migration_started
    )
    has_final = state in {
        DeploymentState.FINAL_SNAPSHOT_VERIFIED,
        DeploymentState.PRODUCTION_MIGRATED,
        DeploymentState.NEW_APP_HEALTHY,
        DeploymentState.TRAFFIC_ACTIVATED,
        DeploymentState.ACTIVE,
        DeploymentState.ROLLBACK_STARTED,
        DeploymentState.RECOVERY_REQUIRED,
    }
    has_new = (
        state
        in {
            DeploymentState.NEW_APP_HEALTHY,
            DeploymentState.TRAFFIC_ACTIVATED,
            DeploymentState.ACTIVE,
        }
        or activation_attempted
    )
    terminal = state in {
        DeploymentState.ACTIVE,
        DeploymentState.ROLLED_BACK,
        DeploymentState.FAILED_SAFE,
        DeploymentState.RECOVERY_REQUIRED,
    }
    failure = None
    if state is DeploymentState.RECOVERY_REQUIRED:
        from dploydb.models import FailureRecord

        failure = FailureRecord(
            error_code="recovery_required",
            what_failed="interrupted",
            log_path="/tmp/events.jsonl",
            next_safe_action="recover",
        )
    return ReleaseManifest(
        recovery_protocol_version=protocol,  # type: ignore[arg-type]
        release_id=RELEASE_ID,
        operation_id=OPERATION_ID,
        project="example",
        requested_version="v2",
        status=state,
        configuration_fingerprint="f" * 64,
        operation_log_path=(tmp_path / "events.jsonl").resolve(),
        previous_application=handle(tmp_path, previous=True),
        rehearsal_backup_id=BACKUP_ID,
        rehearsal_backup_sha256=SHA256,
        final_backup_id=FINAL_BACKUP_ID if has_final else None,
        final_backup_sha256=FINAL_SHA256 if has_final else None,
        production_health_passed=has_new,
        production_changed=production_changed,
        production_migration_started=migration_started,
        traffic_activation_attempted=activation_attempted,
        traffic_activated=traffic_activated,
        new_application=handle(tmp_path, previous=False) if has_new else None,
        started_at=NOW,
        updated_at=NOW,
        completed_at=NOW if terminal else None,
        failure=failure,
    )


def operation_and_events(
    stage: str,
    *,
    production_changed: bool,
    hook: dict[str, object] | None = None,
    terminal: bool = False,
) -> tuple[OperationManifest, list[OperationEvent]]:
    events = [
        OperationEvent(
            sequence=1,
            timestamp=NOW,
            operation_id=OPERATION_ID,
            status=OperationStatus.IN_PROGRESS,
            stage=stage,
            message="durable stage",
            evidence={},
        )
    ]
    if hook is not None:
        events.append(
            OperationEvent(
                sequence=2,
                timestamp=NOW,
                operation_id=OPERATION_ID,
                status=OperationStatus.IN_PROGRESS,
                stage=stage,
                message="traffic result",
                evidence={"traffic_hook": hook},
            )
        )
    status = OperationStatus.SUCCEEDED if terminal else OperationStatus.IN_PROGRESS
    operation = OperationManifest(
        operation_id=OPERATION_ID,
        operation_type="deploy",
        project="example",
        status=status,
        stage=stage,
        configuration_fingerprint="f" * 64,
        process=ProcessIdentity(pid=123, hostname="host"),
        safety=SafetyFacts(production_changed=production_changed),
        started_at=NOW,
        updated_at=NOW,
        completed_at=NOW if terminal else None,
        last_event_sequence=len(events),
    )
    return operation, events


def live(
    *,
    previous: ApplicationRuntimeState,
    new: ApplicationRuntimeState = ApplicationRuntimeState.ABSENT,
    final_verified: bool = False,
    database_sha: str | None = None,
) -> RecoveryLiveState:
    return RecoveryLiveState(
        previous_application=previous,
        new_application=new,
        final_backup_verified=final_verified,
        production_database_sha256=database_sha,
        final_backup_sha256=FINAL_SHA256 if final_verified else None,
    )


@pytest.mark.parametrize(
    ("state", "previous", "expected_restart"),
    [
        (DeploymentState.MAINTENANCE_ENABLED, ApplicationRuntimeState.RUNNING, False),
        (DeploymentState.CURRENT_APP_STOPPED, ApplicationRuntimeState.STOPPED, True),
    ],
)
def test_pre_migration_crashes_recover_previous_without_database_restore(
    tmp_path: Path,
    state: DeploymentState,
    previous: ApplicationRuntimeState,
    expected_restart: bool,
) -> None:
    operation, events = operation_and_events(state.value, production_changed=False)

    plan = build_recovery_plan(
        operation,
        events,
        release(tmp_path, state),
        live(previous=previous),
    )

    assert plan.disposition is RecoveryDisposition.RECOVER_PREVIOUS
    assert (RecoveryAction.RESTART_PREVIOUS_APPLICATION in plan.actions) is expected_restart
    assert RecoveryAction.RESTORE_FINAL_BACKUP not in plan.actions
    assert plan.automatic_database_restore_allowed is True


def test_crash_after_migration_intent_restores_verified_final_backup(
    tmp_path: Path,
) -> None:
    operation, events = operation_and_events(
        "production_migration_started", production_changed=True
    )

    plan = build_recovery_plan(
        operation,
        events,
        release(
            tmp_path,
            DeploymentState.FINAL_SNAPSHOT_VERIFIED,
            migration_started=True,
        ),
        live(
            previous=ApplicationRuntimeState.STOPPED,
            final_verified=True,
            database_sha="c" * 64,
        ),
    )

    assert plan.disposition is RecoveryDisposition.RECOVER_PREVIOUS
    assert plan.production_may_have_changed is True
    assert plan.actions[:2] == (
        RecoveryAction.RESTORE_FINAL_BACKUP,
        RecoveryAction.RESTART_PREVIOUS_APPLICATION,
    )


def test_idempotent_recovery_skips_database_already_matching_final_backup(
    tmp_path: Path,
) -> None:
    operation, events = operation_and_events("rollback_started", production_changed=True)

    plan = build_recovery_plan(
        operation,
        events,
        release(
            tmp_path,
            DeploymentState.ROLLBACK_STARTED,
            migration_started=True,
        ),
        live(
            previous=ApplicationRuntimeState.RUNNING,
            final_verified=True,
            database_sha=FINAL_SHA256,
        ),
    )

    assert plan.disposition is RecoveryDisposition.RECOVER_PREVIOUS
    assert RecoveryAction.RESTORE_FINAL_BACKUP not in plan.actions
    assert RecoveryAction.RESTART_PREVIOUS_APPLICATION not in plan.actions


def traffic_hook(outcome: str, *, passed: bool, exit_code: int | None) -> dict[str, object]:
    return {
        "action": "activate_new",
        "passed": passed,
        "command": {"outcome": outcome, "exit_code": exit_code},
    }


def test_unresolved_traffic_attempt_forbids_automatic_database_restore(
    tmp_path: Path,
) -> None:
    operation, events = operation_and_events("traffic_activation_started", production_changed=True)

    plan = build_recovery_plan(
        operation,
        events,
        release(
            tmp_path,
            DeploymentState.NEW_APP_HEALTHY,
            migration_started=True,
            activation_attempted=True,
        ),
        live(
            previous=ApplicationRuntimeState.STOPPED,
            new=ApplicationRuntimeState.RUNNING,
            final_verified=True,
            database_sha="c" * 64,
        ),
    )

    assert plan.disposition is RecoveryDisposition.MANUAL_REQUIRED
    assert plan.traffic_may_have_switched is True
    assert plan.automatic_database_restore_allowed is False
    assert plan.actions == ()


def test_durable_successful_activation_completes_new_release_without_rollback(
    tmp_path: Path,
) -> None:
    operation, events = operation_and_events(
        "traffic_activation_started",
        production_changed=True,
        hook=traffic_hook("succeeded", passed=True, exit_code=0),
    )

    plan = build_recovery_plan(
        operation,
        events,
        release(
            tmp_path,
            DeploymentState.NEW_APP_HEALTHY,
            migration_started=True,
            activation_attempted=True,
        ),
        live(
            previous=ApplicationRuntimeState.STOPPED,
            new=ApplicationRuntimeState.RUNNING,
            final_verified=True,
            database_sha="c" * 64,
        ),
    )

    assert plan.disposition is RecoveryDisposition.COMPLETE_NEW
    assert plan.actions == (
        RecoveryAction.DISABLE_MAINTENANCE,
        RecoveryAction.VERIFY_NEW,
        RecoveryAction.MARK_ACTIVE,
    )
    assert RecoveryAction.RESTORE_FINAL_BACKUP not in plan.actions


def test_activation_start_failure_is_proven_safe_for_previous_recovery(
    tmp_path: Path,
) -> None:
    operation, events = operation_and_events(
        "traffic_activation_started",
        production_changed=True,
        hook=traffic_hook("start_failed", passed=False, exit_code=None),
    )

    plan = build_recovery_plan(
        operation,
        events,
        release(
            tmp_path,
            DeploymentState.NEW_APP_HEALTHY,
            migration_started=True,
            activation_attempted=True,
        ),
        live(
            previous=ApplicationRuntimeState.STOPPED,
            new=ApplicationRuntimeState.RUNNING,
            final_verified=True,
            database_sha="c" * 64,
        ),
    )

    assert plan.disposition is RecoveryDisposition.RECOVER_PREVIOUS
    assert plan.actions[0] is RecoveryAction.REMOVE_NEW_APPLICATION
    assert RecoveryAction.RESTORE_FINAL_BACKUP in plan.actions


def test_legacy_interrupted_release_refuses_automatic_recovery(tmp_path: Path) -> None:
    operation, events = operation_and_events(
        DeploymentState.MAINTENANCE_ENABLED.value,
        production_changed=False,
    )

    plan = build_recovery_plan(
        operation,
        events,
        release(
            tmp_path,
            DeploymentState.MAINTENANCE_ENABLED,
            protocol=None,
        ),
        live(previous=ApplicationRuntimeState.RUNNING),
    )

    assert plan.disposition is RecoveryDisposition.MANUAL_REQUIRED
    assert "predates durable recovery intent" in plan.reason
