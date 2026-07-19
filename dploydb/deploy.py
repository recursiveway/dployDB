"""Internal Milestone 5 deployment coordinator and pre-traffic rollback."""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from dploydb.candidate import CandidateStageObserver, HealthChecker
from dploydb.config import (
    LoadedConfiguration,
    configuration_fingerprint,
    require_deploy_topology,
)
from dploydb.deployment_dependencies import (
    DeploymentDependencies,
    default_dependencies,
)
from dploydb.deployment_evidence import (
    backup_evidence as _backup_evidence,
)
from dploydb.deployment_evidence import (
    cleanup_evidence as _cleanup_evidence,
)
from dploydb.deployment_evidence import (
    discovery_evidence as _discovery_evidence,
)
from dploydb.deployment_evidence import (
    health_summary as _health_summary,
)
from dploydb.deployment_evidence import (
    hook_summary as _hook_summary,
)
from dploydb.deployment_evidence import (
    inspection_evidence as _inspection_evidence,
)
from dploydb.deployment_evidence import (
    restart_evidence as _restart_evidence,
)
from dploydb.deployment_evidence import (
    start_evidence as _start_evidence,
)
from dploydb.deployment_evidence import (
    stop_evidence as _stop_evidence,
)
from dploydb.errors import (
    DployDBError,
    ExternalCommandError,
    OperationFailedError,
    RecoveryRequiredError,
)
from dploydb.health import ApplicationHealthChecker, ReadinessCheckError, SmokeCheckError
from dploydb.locking import DeploymentLock
from dploydb.migration import require_clean_operation_state
from dploydb.models import (
    BackupArtifact,
    DeploymentState,
    FailureRecord,
    MigrationCommandEvidence,
    OperationManifest,
    OperationStatus,
    ProductionApplicationHandle,
    ReleaseHealthEvidence,
    ReleaseHookEvidence,
    ReleaseManifest,
    SafetyFacts,
)
from dploydb.redaction import JsonValue
from dploydb.releases import ReleaseStore
from dploydb.runners.base import (
    ApplicationRunner,
    ProductionCleanupError,
    ProductionInspectionError,
    ProductionLogs,
    ProductionRunnerError,
    ProductionStartError,
    ProductionStop,
    validate_release_identifier,
)
from dploydb.sqlite_checks import verify_sqlite_database
from dploydb.state import StateStore
from dploydb.subprocesses import CommandOutcome, SubprocessRunner
from dploydb.traffic import TrafficHookResult


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    """Terminal active or fully proven rolled-back deployment evidence."""

    release: ReleaseManifest
    operation: OperationManifest

    @property
    def active(self) -> bool:
        return self.release.status is DeploymentState.ACTIVE

    @property
    def rolled_back(self) -> bool:
        return self.release.status is DeploymentState.ROLLED_BACK


@dataclass(slots=True)
class _Context:
    loaded: LoadedConfiguration
    version: str
    operation_id: str
    release_id: str
    store: StateStore
    releases: ReleaseStore
    dependencies: DeploymentDependencies
    operation_log: Path
    cancellation_event: threading.Event | None
    active_release: ReleaseManifest | None
    previous: ProductionApplicationHandle | None = None
    stopped: ProductionStop | None = None
    final_backup: BackupArtifact | None = None
    new_application: ProductionApplicationHandle | None = None
    maintenance_enabled: bool = False
    production_may_have_changed: bool = False
    traffic_activation_attempted: bool = False
    traffic_activated: bool = False
    hook_evidence: list[ReleaseHookEvidence] = field(default_factory=list)
    health_evidence: list[ReleaseHealthEvidence] = field(default_factory=list)


def deploy_configured_release(
    loaded: LoadedConfiguration,
    *,
    version: str,
    config_path: Path,
    command_environment: Mapping[str, str] | None = None,
    candidate_command_runner: SubprocessRunner | None = None,
    candidate_application_runner: ApplicationRunner | None = None,
    candidate_health_checker: HealthChecker | None = None,
    dependencies: DeploymentDependencies | None = None,
    cancellation_event: threading.Event | None = None,
) -> DeploymentResult:
    """Deploy one release or return a fully proven pre-traffic rollback."""
    release_version = validate_release_identifier(version)
    topology = require_deploy_topology(loaded.config)
    selected_environment = dict(os.environ if command_environment is None else command_environment)
    working_directory = config_path.resolve().parent
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    releases = ReleaseStore(loaded.config.state_directory, secrets=loaded.secrets)
    lock = DeploymentLock(loaded.config.state_directory, secrets=loaded.secrets)
    owned_health: ApplicationHealthChecker | None = None

    with lock:
        require_clean_operation_state(lock, store)
        fingerprint = configuration_fingerprint(loaded.config, secrets=loaded.secrets)
        operation = store.create_operation(
            operation_type="deploy",
            project=loaded.config.project,
            configuration_fingerprint=fingerprint,
            evidence={
                "database_path": str(loaded.config.database.path),
                "requested_version": release_version,
            },
        )
        lock.record_owner(operation_id=operation.operation_id, operation_type="deploy")
        operation_log = store.operation_paths(operation.operation_id).events
        release: ReleaseManifest | None = None
        try:
            selected_dependencies = dependencies
            if selected_dependencies is None:
                selected_dependencies, owned_health = default_dependencies(
                    loaded,
                    topology=topology,
                    config_path=config_path,
                    working_directory=working_directory,
                    command_environment=selected_environment,
                    candidate_command_runner=candidate_command_runner,
                    candidate_application_runner=candidate_application_runner,
                    candidate_health_checker=candidate_health_checker,
                )
            active_release = releases.active_release()
            release = releases.create_release(
                operation_id=operation.operation_id,
                project=loaded.config.project,
                requested_version=release_version,
                configuration_fingerprint=fingerprint,
                operation_log_path=operation_log,
                previous_application=(
                    None if active_release is None else active_release.new_application
                ),
            )
            context = _Context(
                loaded=loaded,
                version=release_version,
                operation_id=operation.operation_id,
                release_id=release.release_id,
                store=store,
                releases=releases,
                dependencies=selected_dependencies,
                operation_log=operation_log,
                cancellation_event=cancellation_event,
                active_release=active_release,
                previous=(None if active_release is None else active_release.new_application),
            )
            return _run_deployment(context, config_path=config_path, lock=lock)
        except DployDBError as error:
            if release is None:
                _finish_operation_without_release(store, operation.operation_id, error)
            raise
        except Exception as raw_error:
            unexpected_error = RecoveryRequiredError(
                "deployment coordinator failed unexpectedly: "
                + loaded.secrets.redact_text(f"{type(raw_error).__name__}: {raw_error}"),
                production_changed=False,
                previous_application_running=None,
                log_path=operation_log,
                next_safe_action=(
                    "Preserve the operation and release evidence; inspect production before "
                    "taking any recovery action."
                ),
            )
            if release is None:
                _finish_operation_without_release(store, operation.operation_id, unexpected_error)
            raise unexpected_error from None
        finally:
            if owned_health is not None:
                owned_health.close()


def _run_deployment(
    context: _Context,
    *,
    config_path: Path,
    lock: DeploymentLock,
) -> DeploymentResult:
    try:
        observer = _release_stage_observer(context)
        candidate = context.dependencies.pre_cutover.run(
            context.loaded,
            version=context.version,
            config_path=config_path,
            operation_id=context.operation_id,
            store=context.store,
            lock=lock,
            cancellation_event=context.cancellation_event,
            stage_observer=observer,
        )
        context.store.append_event(
            context.operation_id,
            message="Caller-owned candidate stage returned complete passing evidence.",
            evidence={"candidate_stage": candidate.as_evidence()},
        )
        _resolve_previous_application(context)
        _enable_maintenance(context)
        assert context.previous is not None
        stopped = context.dependencies.production.stop_current(
            context.previous,
            cancellation_event=context.cancellation_event,
        )
        context.stopped = stopped
        _persist_stage(
            context,
            DeploymentState.CURRENT_APP_STOPPED,
            message="The exact previous application was stopped and inspected.",
            evidence={"production_stop": _stop_evidence(stopped)},
            previous_application=context.previous,
            safety=SafetyFacts(
                production_changed=False,
                previous_application_running=False,
                recovery_required=False,
            ),
        )
        final_backup = context.dependencies.database.create_final(
            operation_id=context.operation_id,
            stopped=stopped,
        )
        context.final_backup = final_backup
        _persist_stage(
            context,
            DeploymentState.FINAL_SNAPSHOT_VERIFIED,
            message="The stopped-writer final backup was created and reverified.",
            evidence={"final_backup": _backup_evidence(final_backup)},
            final_backup_id=final_backup.metadata.backup_id,
            final_backup_sha256=final_backup.metadata.sha256,
            safety=SafetyFacts(
                production_changed=False,
                previous_application_running=False,
                recovery_required=False,
            ),
        )

        def record_migration(evidence: MigrationCommandEvidence) -> None:
            context.store.append_event(
                context.operation_id,
                message="Production migration command reached a terminal outcome.",
                evidence={"production_migration_command": evidence.model_dump(mode="json")},
            )

        context.production_may_have_changed = True
        migrated = context.dependencies.database.migrate(
            operation_id=context.operation_id,
            stopped=stopped,
            final_backup=final_backup,
            traffic_activated=context.traffic_activated,
            evidence_sink=record_migration,
            cancellation_event=context.cancellation_event,
            log_path=context.operation_log,
        )
        _persist_stage(
            context,
            DeploymentState.PRODUCTION_MIGRATED,
            message="Production migration and SQLite checks passed.",
            evidence={"production_migration": migrated.model_dump(mode="json")},
            production_changed=True,
            safety=SafetyFacts(
                production_changed=True,
                previous_application_running=False,
                recovery_required=False,
            ),
        )
        started = context.dependencies.production.start_new(
            operation_id=context.operation_id,
            release_id=context.release_id,
            version=context.version,
            cancellation_event=context.cancellation_event,
        )
        context.new_application = started.handle
        context.store.append_event(
            context.operation_id,
            message="The new production application started with validated identity.",
            evidence={"production_start": _start_evidence(started)},
        )
        logs = context.dependencies.production.collect_logs(
            started.handle,
            cancellation_event=context.cancellation_event,
        )
        _require_complete_logs(logs, context.operation_log)
        database_check = verify_sqlite_database(context.loaded.config.database.path)
        health = context.dependencies.health.check_application(
            version=context.version,
            database_path=context.loaded.config.database.path,
            cancellation_event=context.cancellation_event,
        )
        context.health_evidence.append(_health_summary(health, role="new", version=context.version))
        _persist_stage(
            context,
            DeploymentState.NEW_APP_HEALTHY,
            message="Final production database, logs, HTTP, and smoke checks passed.",
            evidence={
                "production_logs": logs.command.as_evidence(),
                "database": database_check.model_dump(mode="json"),
                "application_health": health.as_evidence(),
            },
            new_application=started.handle,
            production_health_passed=True,
            production_changed=True,
            safety=SafetyFacts(
                production_changed=True,
                previous_application_running=False,
                recovery_required=False,
            ),
        )
        _activate_new_traffic(context)
        _disable_maintenance_after_activation(context)
        return _complete_active(context)
    except BaseException as raw_error:
        error = _normalize_deployment_error(raw_error, context)
        if error is None:
            raise
        if error.payload.recovery_required or context.traffic_activated:
            _finish_recovery_required(context, error)
            raise error from None
        if context.traffic_activation_attempted:
            uncertain = RecoveryRequiredError(
                "new-traffic activation was attempted without proof of a safe routing state: "
                + error.payload.what_failed,
                production_changed=True,
                previous_application_running=False,
                log_path=context.operation_log,
                next_safe_action=(
                    "Do not restore the database. Keep the checked new application available "
                    "and determine the live traffic target before recovery."
                ),
            )
            _finish_recovery_required(context, uncertain)
            raise uncertain from None
        if context.maintenance_enabled:
            return _rollback(context, error)
        _finish_failed_before_rollback(context, error)
        raise error from None


def _release_stage_observer(context: _Context) -> CandidateStageObserver:
    def observe(stage: DeploymentState, evidence: Mapping[str, JsonValue]) -> None:
        changes: dict[str, object] = {}
        if stage is DeploymentState.SNAPSHOT_VERIFIED:
            backup_id = evidence.get("backup_id")
            sha256 = evidence.get("sha256")
            if not isinstance(backup_id, str) or not isinstance(sha256, str):
                raise RecoveryRequiredError(
                    "candidate snapshot evidence is incomplete for the release manifest",
                    production_changed=False,
                    previous_application_running=None,
                    log_path=context.operation_log,
                    next_safe_action="Preserve the operation evidence and inspect the backup.",
                )
            changes = {
                "rehearsal_backup_id": backup_id,
                "rehearsal_backup_sha256": sha256,
            }
        context.releases.transition(
            context.release_id,
            status=stage,
            traffic_hooks=tuple(context.hook_evidence),
            health_checks=tuple(context.health_evidence),
            **changes,
        )

    return observe


def _resolve_previous_application(context: _Context) -> None:
    if context.previous is not None:
        inspection = context.dependencies.production.inspect(
            context.previous,
            expected_running=True,
            cancellation_event=context.cancellation_event,
        )
        evidence: dict[str, JsonValue] = {
            "source": "active_release",
            "inspection": _inspection_evidence(inspection),
        }
    else:
        discovery = context.dependencies.production.discover_current(
            cancellation_event=context.cancellation_event
        )
        context.previous = discovery.inspection.handle
        evidence = {
            "source": "configured_bootstrap",
            "discovery": _discovery_evidence(discovery),
        }
    context.store.append_event(
        context.operation_id,
        message="The exact currently running production application was identified.",
        evidence={"previous_application": evidence},
    )


def _enable_maintenance(context: _Context) -> None:
    result = context.dependencies.traffic.enable_maintenance(
        cancellation_event=context.cancellation_event
    )
    _record_hook(context, result)
    if not result.passed:
        cleanup = context.dependencies.traffic.disable_maintenance(
            cancellation_event=context.cancellation_event
        )
        _record_hook(context, cleanup)
        error = _hook_error(result, context, production_changed=False)
        if not cleanup.passed:
            raise RecoveryRequiredError(
                error.payload.what_failed
                + "; maintenance cleanup also failed: "
                + _hook_description(cleanup),
                production_changed=False,
                previous_application_running=True,
                log_path=context.operation_log,
                next_safe_action=(
                    "The previous application was not stopped. Determine and disable the "
                    "maintenance state manually before retrying."
                ),
            )
        _verify_previous(context)
        raise error
    context.maintenance_enabled = True
    assert context.previous is not None
    _persist_stage(
        context,
        DeploymentState.MAINTENANCE_ENABLED,
        message="Maintenance mode was enabled with complete hook evidence.",
        evidence={"traffic_hook": result.as_evidence()},
        previous_application=context.previous,
        safety=SafetyFacts(
            production_changed=False,
            previous_application_running=True,
            recovery_required=False,
        ),
    )


def _activate_new_traffic(context: _Context) -> None:
    result = context.dependencies.traffic.activate_new(
        cancellation_event=context.cancellation_event
    )
    _record_hook(context, result)
    if not result.passed:
        context.traffic_activation_attempted = _hook_may_have_run(result)
        raise _hook_error(result, context, production_changed=True)
    context.traffic_activation_attempted = True
    context.traffic_activated = True
    _persist_stage(
        context,
        DeploymentState.TRAFFIC_ACTIVATED,
        message="New-release traffic activation succeeded and was stored immediately.",
        evidence={"traffic_hook": result.as_evidence()},
        production_changed=True,
        traffic_activated=True,
        safety=SafetyFacts(
            production_changed=True,
            previous_application_running=False,
            recovery_required=False,
        ),
    )


def _disable_maintenance_after_activation(context: _Context) -> None:
    result = context.dependencies.traffic.disable_maintenance(
        cancellation_event=context.cancellation_event
    )
    _record_hook(context, result)
    if not result.passed:
        raise RecoveryRequiredError(
            "new traffic is active but maintenance disable failed: " + _hook_description(result),
            production_changed=True,
            previous_application_running=False,
            log_path=context.operation_log,
            next_safe_action=(
                "Do not restore the database. Keep the checked new release running and "
                "disable maintenance manually."
            ),
        )
    context.maintenance_enabled = False


def _complete_active(context: _Context) -> DeploymentResult:
    release = context.releases.transition(
        context.release_id,
        status=DeploymentState.ACTIVE,
        production_changed=True,
        traffic_activated=True,
        traffic_hooks=tuple(context.hook_evidence),
        health_checks=tuple(context.health_evidence),
    )
    pointers = context.releases.activate_release(context.release_id)
    operation = context.store.transition(
        context.operation_id,
        status=OperationStatus.SUCCEEDED,
        stage="active",
        message="The new release is active and maintenance is disabled.",
        evidence={
            "release_id": context.release_id,
            "active_release_id": pointers.active_release_id,
            "previous_release_id": pointers.previous_release_id,
        },
        safety=SafetyFacts(
            production_changed=True,
            previous_application_running=False,
            recovery_required=False,
        ),
    )
    return DeploymentResult(release=release, operation=operation)


def _rollback(context: _Context, original: DployDBError) -> DeploymentResult:
    try:
        _persist_stage(
            context,
            DeploymentState.ROLLBACK_STARTED,
            message="Pre-traffic rollback started after deployment failure.",
            evidence={"original_failure": original.payload.as_dict()},
            production_changed=context.production_may_have_changed,
            safety=SafetyFacts(
                production_changed=context.production_may_have_changed,
                previous_application_running=False,
                recovery_required=False,
            ),
        )
        if context.new_application is not None:
            cleanup = context.dependencies.production.remove_new(context.new_application)
            context.store.append_event(
                context.operation_id,
                message="Failed new-release application cleanup was proven.",
                evidence={"production_cleanup": _cleanup_evidence(cleanup)},
            )
        if context.production_may_have_changed:
            if context.stopped is None or context.final_backup is None:
                raise RecoveryRequiredError(
                    "database rollback prerequisites are incomplete",
                    production_changed=True,
                    previous_application_running=False,
                    log_path=context.operation_log,
                    next_safe_action=(
                        "Keep every application and traffic path blocked; inspect the final "
                        "backup and stopped-container evidence manually."
                    ),
                )
            restored = context.dependencies.database.restore(
                operation_id=context.operation_id,
                stopped=context.stopped,
                final_backup=context.final_backup,
                traffic_activated=context.traffic_activated,
            )
            context.store.append_event(
                context.operation_id,
                message="The verified final backup was restored before traffic activation.",
                evidence={"database_restore": restored.model_dump(mode="json")},
            )
        if context.previous is None:
            raise RecoveryRequiredError(
                "the exact previous application identity is unavailable for rollback",
                production_changed=context.production_may_have_changed,
                previous_application_running=None,
                log_path=context.operation_log,
                next_safe_action="Keep maintenance enabled and inspect production manually.",
            )
        restarted = context.dependencies.production.restart_previous(
            context.previous,
            cancellation_event=context.cancellation_event,
        )
        context.store.append_event(
            context.operation_id,
            message="The exact previous application was restarted and inspected.",
            evidence={"previous_restart": _restart_evidence(restarted)},
        )
        old_target = context.dependencies.traffic.activate_old(
            cancellation_event=context.cancellation_event
        )
        _record_hook(context, old_target)
        if not old_target.passed:
            raise _hook_error(
                old_target,
                context,
                production_changed=context.production_may_have_changed,
                recovery=True,
            )
        maintenance_off = context.dependencies.traffic.disable_maintenance(
            cancellation_event=context.cancellation_event
        )
        _record_hook(context, maintenance_off)
        if not maintenance_off.passed:
            raise _hook_error(
                maintenance_off,
                context,
                production_changed=context.production_may_have_changed,
                recovery=True,
            )
        context.maintenance_enabled = False
        verification = _verify_previous(context)
        failure = _failure_record(original)
        release = context.releases.transition(
            context.release_id,
            status=DeploymentState.ROLLED_BACK,
            production_changed=context.production_may_have_changed,
            traffic_activated=False,
            traffic_hooks=tuple(context.hook_evidence),
            health_checks=tuple(context.health_evidence),
            failure=failure,
        )
        operation = context.store.transition(
            context.operation_id,
            status=OperationStatus.FAILED_SAFE,
            stage="rolled_back",
            message="The previous application and database were restored and verified.",
            evidence={"rollback_verification": verification},
            safety=SafetyFacts(
                production_changed=context.production_may_have_changed,
                previous_application_running=True,
                recovery_required=False,
            ),
            failure=failure,
        )
        return DeploymentResult(release=release, operation=operation)
    except BaseException as rollback_error:
        normalized = _normalize_deployment_error(rollback_error, context)
        if normalized is None:
            raise
        recovery = RecoveryRequiredError(
            "pre-traffic rollback could not be proven: "
            + normalized.payload.what_failed
            + "; original deployment failure: "
            + original.payload.what_failed,
            production_changed=context.production_may_have_changed,
            previous_application_running=False,
            log_path=context.operation_log,
            next_safe_action=(
                "Keep traffic blocked. Preserve all evidence and manually verify the database, "
                "previous container, old target, and maintenance state."
            ),
        )
        _finish_recovery_required(context, recovery)
        raise recovery from None


def _verify_previous(context: _Context) -> dict[str, JsonValue]:
    if context.previous is None:
        raise RecoveryRequiredError(
            "previous application identity is unavailable for health verification",
            production_changed=context.production_may_have_changed,
            previous_application_running=None,
            log_path=context.operation_log,
            next_safe_action="Inspect the current production container manually.",
        )
    database = verify_sqlite_database(context.loaded.config.database.path)
    version = context.previous.version or "previous"
    health = context.dependencies.health.check_application(
        version=version,
        database_path=context.loaded.config.database.path,
        cancellation_event=context.cancellation_event,
    )
    context.health_evidence.append(_health_summary(health, role="previous", version=version))
    evidence: dict[str, JsonValue] = {
        "database": cast(JsonValue, database.model_dump(mode="json")),
        "application_health": health.as_evidence(),
    }
    context.store.append_event(
        context.operation_id,
        message="Previous database and application health were verified.",
        evidence={"previous_health": evidence},
    )
    return evidence


def _persist_stage(
    context: _Context,
    status: DeploymentState,
    *,
    message: str,
    evidence: Mapping[str, object],
    safety: SafetyFacts,
    **release_changes: object,
) -> None:
    release_changes.setdefault("traffic_hooks", tuple(context.hook_evidence))
    release_changes.setdefault("health_checks", tuple(context.health_evidence))
    context.store.transition(
        context.operation_id,
        status=OperationStatus.IN_PROGRESS,
        stage=status.value,
        message=message,
        evidence=evidence,
        safety=safety,
    )
    context.releases.transition(
        context.release_id,
        status=status,
        **release_changes,
    )


def _finish_failed_before_rollback(context: _Context, error: DployDBError) -> None:
    status = (
        DeploymentState.RECOVERY_REQUIRED
        if error.payload.recovery_required
        else DeploymentState.FAILED_SAFE
    )
    operation_status = (
        OperationStatus.RECOVERY_REQUIRED
        if error.payload.recovery_required
        else OperationStatus.FAILED_SAFE
    )
    failure = _failure_record(error)
    context.releases.transition(
        context.release_id,
        status=status,
        production_changed=error.payload.production_changed,
        traffic_activated=False,
        traffic_hooks=tuple(context.hook_evidence),
        health_checks=tuple(context.health_evidence),
        failure=failure,
    )
    context.store.transition(
        context.operation_id,
        status=operation_status,
        stage=status.value,
        message="Deployment stopped before a cutover rollback was required.",
        safety=SafetyFacts(
            production_changed=error.payload.production_changed,
            previous_application_running=error.payload.previous_application_running,
            recovery_required=error.payload.recovery_required,
        ),
        failure=failure,
    )


def _finish_recovery_required(context: _Context, error: DployDBError) -> None:
    failure = _failure_record(error)
    try:
        manifest = context.releases.read_manifest(context.release_id)
        if manifest.status not in {
            DeploymentState.ACTIVE,
            DeploymentState.ROLLED_BACK,
            DeploymentState.FAILED_SAFE,
            DeploymentState.RECOVERY_REQUIRED,
        }:
            context.releases.transition(
                context.release_id,
                status=DeploymentState.RECOVERY_REQUIRED,
                production_changed=(
                    error.payload.production_changed or context.production_may_have_changed
                ),
                traffic_activated=context.traffic_activated,
                traffic_hooks=tuple(context.hook_evidence),
                health_checks=tuple(context.health_evidence),
                failure=failure,
            )
    except Exception as release_error:
        error.add_note(
            context.loaded.secrets.redact_text(
                "Release recovery evidence also failed: "
                f"{type(release_error).__name__}: {release_error}"
            )
        )
    try:
        operation = context.store.read_manifest(context.operation_id)
        if operation.status is OperationStatus.IN_PROGRESS:
            context.store.transition(
                context.operation_id,
                status=OperationStatus.RECOVERY_REQUIRED,
                stage="recovery_required",
                message="Deployment state requires manual recovery.",
                safety=SafetyFacts(
                    production_changed=(
                        error.payload.production_changed or context.production_may_have_changed
                    ),
                    previous_application_running=error.payload.previous_application_running,
                    recovery_required=True,
                ),
                failure=failure,
            )
    except Exception as state_error:
        error.add_note(
            context.loaded.secrets.redact_text(
                "Operation recovery evidence also failed: "
                f"{type(state_error).__name__}: {state_error}"
            )
        )


def _finish_operation_without_release(
    store: StateStore,
    operation_id: str,
    error: DployDBError,
) -> None:
    current = store.read_manifest(operation_id)
    if current.status is not OperationStatus.IN_PROGRESS:
        return
    status = (
        OperationStatus.RECOVERY_REQUIRED
        if error.payload.recovery_required
        else OperationStatus.FAILED_SAFE
    )
    store.transition(
        operation_id,
        status=status,
        stage=status.value,
        message="Deployment failed before its release manifest was created.",
        safety=SafetyFacts(
            production_changed=error.payload.production_changed,
            previous_application_running=error.payload.previous_application_running,
            recovery_required=error.payload.recovery_required,
        ),
        failure=_failure_record(error),
    )


def _normalize_deployment_error(
    error: BaseException,
    context: _Context,
) -> DployDBError | None:
    if isinstance(error, DployDBError):
        if context.production_may_have_changed and not error.payload.production_changed:
            return type(error)(
                error.payload.what_failed,
                production_changed=True,
                previous_application_running=False,
                log_path=error.payload.log_path or context.operation_log,
                next_safe_action=error.payload.next_safe_action,
            )
        return error
    if isinstance(error, ProductionStartError):
        start_error_type = (
            OperationFailedError if error.cleanup_proven is True else RecoveryRequiredError
        )
        return start_error_type(
            str(error),
            production_changed=True,
            previous_application_running=False,
            log_path=context.operation_log,
            next_safe_action=(
                "Keep maintenance enabled and inspect the operation-labeled new release "
                "before database rollback."
            ),
        )
    if isinstance(error, ProductionCleanupError):
        return RecoveryRequiredError(
            str(error),
            production_changed=context.production_may_have_changed,
            previous_application_running=False,
            log_path=context.operation_log,
            next_safe_action="Keep traffic blocked and inspect the failed new release.",
        )
    if isinstance(error, ProductionInspectionError):
        return RecoveryRequiredError(
            str(error),
            production_changed=context.production_may_have_changed,
            previous_application_running=None,
            log_path=context.operation_log,
            next_safe_action="Preserve the contradictory container inspection evidence.",
        )
    if isinstance(error, ProductionRunnerError):
        if error.command is not None and error.command.outcome is CommandOutcome.CLEANUP_FAILED:
            runner_error_type: type[DployDBError] = RecoveryRequiredError
        else:
            runner_error_type = OperationFailedError
        return runner_error_type(
            str(error),
            production_changed=context.production_may_have_changed,
            previous_application_running=(False if context.stopped is not None else None),
            log_path=context.operation_log,
            next_safe_action="Keep maintenance enabled and inspect the recorded container state.",
        )
    if isinstance(error, ReadinessCheckError):
        return OperationFailedError(
            error.evidence.reason,
            production_changed=context.production_may_have_changed,
            previous_application_running=False,
            log_path=context.operation_log,
            next_safe_action="Keep traffic blocked and roll back the checked release.",
        )
    if isinstance(error, SmokeCheckError):
        error_type = OperationFailedError if error.cleanup_proven else RecoveryRequiredError
        return error_type(
            str(error),
            production_changed=context.production_may_have_changed,
            previous_application_running=False,
            log_path=context.operation_log,
            next_safe_action="Keep traffic blocked and inspect smoke-command cleanup evidence.",
        )
    if not isinstance(error, Exception):
        return None
    return RecoveryRequiredError(
        "unexpected deployment boundary failure: "
        + context.loaded.secrets.redact_text(f"{type(error).__name__}: {error}"),
        production_changed=context.production_may_have_changed,
        previous_application_running=(False if context.stopped is not None else None),
        log_path=context.operation_log,
        next_safe_action="Preserve all evidence and inspect production before recovery.",
    )


def _hook_error(
    result: TrafficHookResult,
    context: _Context,
    *,
    production_changed: bool,
    recovery: bool = False,
) -> DployDBError:
    error_type: type[DployDBError]
    if recovery or result.command.outcome is CommandOutcome.CLEANUP_FAILED:
        error_type = RecoveryRequiredError
    else:
        error_type = ExternalCommandError
    return error_type(
        _hook_description(result),
        production_changed=production_changed,
        previous_application_running=(False if context.stopped is not None else True),
        log_path=context.operation_log,
        next_safe_action="Keep traffic blocked and inspect the recorded hook evidence.",
    )


def _hook_may_have_run(result: TrafficHookResult) -> bool:
    return not (
        result.command.outcome is CommandOutcome.START_FAILED
        or (result.command.outcome is CommandOutcome.CANCELLED and result.command.exit_code is None)
    )


def _record_hook(context: _Context, result: TrafficHookResult) -> None:
    context.hook_evidence.append(_hook_summary(result))
    context.store.append_event(
        context.operation_id,
        message=f"Traffic hook {result.action.value} reached a terminal outcome.",
        evidence={"traffic_hook": result.as_evidence()},
    )


def _hook_description(result: TrafficHookResult) -> str:
    command = result.command
    if command.stdout.truncated or command.stderr.truncated:
        detail = "complete output evidence was unavailable"
    elif command.outcome is CommandOutcome.NONZERO_EXIT:
        detail = f"exited with status {command.exit_code}"
    elif command.outcome is CommandOutcome.TIMED_OUT:
        detail = "timed out"
    elif command.outcome is CommandOutcome.CANCELLED:
        detail = "was cancelled"
    elif command.outcome is CommandOutcome.CLEANUP_FAILED:
        detail = "process cleanup could not be proven"
    elif command.outcome is CommandOutcome.START_FAILED:
        detail = "could not start"
    else:
        detail = "returned invalid success evidence"
    return f"traffic hook {result.action.value} {detail}"


def _require_complete_logs(logs: ProductionLogs, log_path: Path) -> None:
    command = logs.command
    if (
        command.outcome is CommandOutcome.SUCCEEDED
        and not command.stdout.truncated
        and not command.stderr.truncated
    ):
        return
    error_type = (
        RecoveryRequiredError
        if command.outcome is CommandOutcome.CLEANUP_FAILED
        else OperationFailedError
    )
    raise error_type(
        "new production application logs were not captured completely",
        production_changed=True,
        previous_application_running=False,
        log_path=log_path,
        next_safe_action="Keep traffic blocked and inspect the new application directly.",
    )


def _failure_record(error: DployDBError) -> FailureRecord:
    return FailureRecord(
        error_code=error.payload.error_code,
        what_failed=error.payload.what_failed,
        log_path=error.payload.log_path,
        next_safe_action=error.payload.next_safe_action,
    )
