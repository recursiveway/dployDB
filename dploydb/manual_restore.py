"""Release-aware manual restore preview and controlled execution."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dploydb.backup import create_verified_backup, verify_configured_backup
from dploydb.config import (
    LoadedConfiguration,
    configuration_fingerprint,
    require_deploy_topology,
)
from dploydb.deployment_dependencies import (
    DeploymentDependencies,
    ProductionHealthBoundary,
    default_dependencies,
)
from dploydb.deployment_evidence import (
    health_summary,
    hook_summary,
    inspection_evidence,
    restart_evidence,
    stop_evidence,
)
from dploydb.errors import (
    DployDBError,
    ExternalCommandError,
    OperationFailedError,
    RecoveryRequiredError,
    SafetyCheckError,
)
from dploydb.health import ApplicationHealthChecker
from dploydb.locking import DeploymentLock
from dploydb.migration import require_clean_operation_state
from dploydb.models import (
    BackupArtifact,
    BackupPurpose,
    DeploymentState,
    FailureRecord,
    OperationManifest,
    OperationStatus,
    ProductionApplicationHandle,
    ReleaseManifest,
    ReleasePointers,
    SafetyFacts,
    VerifiedDatabaseRestoreResult,
)
from dploydb.releases import ReleaseStore
from dploydb.restore import restore_verified_database
from dploydb.runners.base import ProductionApplicationRunner
from dploydb.sqlite_checks import verify_sqlite_database
from dploydb.state import StateStore
from dploydb.storage.local import LocalBackupStorage
from dploydb.subprocesses import CommandOutcome
from dploydb.traffic import TrafficController, TrafficHookResult

DATA_LOSS_WARNING = (
    "WARNING: restoring an older release replaces the current production database and may "
    "permanently discard data written after that release. DployDB will first create and verify "
    "a backup of the current state."
)


@dataclass(frozen=True, slots=True)
class RestoreSelection:
    """Fully verified release-to-backup/application mapping used by manual restore."""

    active_release: ReleaseManifest
    selected_release: ReleaseManifest
    selected_backup: BackupArtifact
    current_application: ProductionApplicationHandle
    selected_application: ProductionApplicationHandle
    database_path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "active_release_id": self.active_release.release_id,
            "active_version": self.active_release.requested_version,
            "selected_release_id": self.selected_release.release_id,
            "selected_version": self.selected_release.requested_version,
            "selected_backup_id": self.selected_backup.metadata.backup_id,
            "selected_backup_sha256": self.selected_backup.metadata.sha256,
            "current_container_id": self.current_application.container_id,
            "current_container_name": self.current_application.container_name,
            "selected_container_id": self.selected_application.container_id,
            "selected_container_name": self.selected_application.container_name,
            "database_path": str(self.database_path),
            "data_loss_possible": True,
            "pre_restore_backup_required": True,
            "warning": DATA_LOSS_WARNING,
        }


@dataclass(frozen=True, slots=True)
class ManualRestoreDependencies:
    """Narrow operational boundaries used by the manual restore coordinator."""

    production: ProductionApplicationRunner
    traffic: TrafficController
    health: ProductionHealthBoundary


@dataclass(frozen=True, slots=True)
class ManualRestoreResult:
    """Proven terminal result of a successful release-aware manual restore."""

    operation: OperationManifest
    selected_release: ReleaseManifest
    replaced_release: ReleaseManifest
    pre_restore_backup: BackupArtifact
    database_restore: VerifiedDatabaseRestoreResult
    pointers: ReleasePointers
    operation_log_path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "command": "restore",
            "outcome": "manual_restore_completed",
            "operation_id": self.operation.operation_id,
            "selected_release_id": self.selected_release.release_id,
            "selected_version": self.selected_release.requested_version,
            "replaced_release_id": self.replaced_release.release_id,
            "pre_restore_backup_id": self.pre_restore_backup.metadata.backup_id,
            "restored_backup_id": self.database_restore.backup_id,
            "database_sha256": self.database_restore.sha256,
            "active_release_id": self.pointers.active_release_id,
            "previous_release_id": self.pointers.previous_release_id,
            "production_changed": True,
            "previous_application_running": True,
            "recovery_required": False,
            "data_loss_warning": DATA_LOSS_WARNING,
            "log_path": str(self.operation_log_path),
        }


@dataclass(slots=True)
class _RestoreContext:
    loaded: LoadedConfiguration
    selection: RestoreSelection
    operation_id: str
    store: StateStore
    releases: ReleaseStore
    dependencies: ManualRestoreDependencies
    operation_log: Path
    cancellation_event: threading.Event | None
    maintenance_enabled: bool = False
    current_stopped: bool = False
    current_stop_attempted: bool = False
    selected_running: bool = False
    selected_restart_attempted: bool = False
    database_replaced: bool = False
    traffic_activation_attempted: bool = False
    normal_traffic_may_be_enabled: bool = False
    pre_restore_backup: BackupArtifact | None = None
    database_restore: VerifiedDatabaseRestoreResult | None = None


FaultInjector = Callable[[str], None]


def preview_configured_restore(
    loaded: LoadedConfiguration,
    release_id: str,
) -> RestoreSelection:
    """Resolve the protected previous release without mutating any local state."""
    releases = ReleaseStore(loaded.config.state_directory, secrets=loaded.secrets)
    selected, pointers = releases.lookup_history_release(release_id)
    if pointers is None or pointers.previous_release_id is None:
        raise SafetyCheckError(
            "there is no protected previous release to restore",
            production_changed=False,
            previous_application_running=None,
            log_path=releases.releases_directory,
            next_safe_action="Deploy a verified release before requesting a previous restore.",
        )
    if release_id != pointers.previous_release_id:
        raise SafetyCheckError(
            "manual restore is limited to the immediately previous protected release",
            production_changed=False,
            previous_application_running=None,
            log_path=releases.pointer_path,
            next_safe_action=(
                f"Select protected previous release {pointers.previous_release_id}; older "
                "history is read-only during the hackathon."
            ),
        )
    active = releases.read_manifest(pointers.active_release_id)
    if active.status is not DeploymentState.ACTIVE or selected.status is not DeploymentState.ACTIVE:
        raise SafetyCheckError(
            "active/previous release status is not safe for manual restore",
            production_changed=False,
            previous_application_running=None,
            log_path=releases.pointer_path,
            next_safe_action="Run dploydb status and resolve release-state contradictions first.",
        )
    if active.previous_release_id != selected.release_id:
        raise SafetyCheckError(
            "active release lineage does not match the protected previous pointer",
            production_changed=False,
            previous_application_running=None,
            log_path=releases.pointer_path,
            next_safe_action="Preserve release state and inspect the active manifest manually.",
        )
    current_application = active.new_application
    selected_application = selected.new_application
    if current_application is None or selected_application is None:
        raise SafetyCheckError(
            "manual restore requires exact active and previous application identities",
            production_changed=False,
            previous_application_running=None,
            log_path=active.operation_log_path,
            next_safe_action="Do not guess a container; preserve the release manifests.",
        )
    if active.previous_application != selected_application:
        raise SafetyCheckError(
            "active release does not preserve the selected previous application identity",
            production_changed=False,
            previous_application_running=None,
            log_path=active.operation_log_path,
            next_safe_action="Do not restore until the application identity is reconciled.",
        )
    if current_application.container_id == selected_application.container_id:
        raise SafetyCheckError(
            "active and selected releases unexpectedly identify the same container",
            production_changed=False,
            previous_application_running=None,
            log_path=active.operation_log_path,
            next_safe_action="Inspect the release manifests and live Docker state.",
        )
    if active.final_backup_id is None or active.final_backup_sha256 is None:
        raise SafetyCheckError(
            "active release does not reference its verified pre-migration final backup",
            production_changed=False,
            previous_application_running=None,
            log_path=active.operation_log_path,
            next_safe_action="Do not restore an older release without its final backup.",
        )
    backup = verify_configured_backup(loaded, active.final_backup_id)
    if (
        backup.metadata.purpose is not BackupPurpose.FINAL
        or backup.metadata.operation_id != active.operation_id
        or backup.metadata.sha256 != active.final_backup_sha256
    ):
        raise SafetyCheckError(
            "selected restore backup contradicts the active release manifest",
            production_changed=False,
            previous_application_running=None,
            log_path=backup.metadata_path,
            next_safe_action="Preserve the backup and release evidence; do not restore it.",
        )
    return RestoreSelection(
        active_release=active,
        selected_release=selected,
        selected_backup=backup,
        current_application=current_application,
        selected_application=selected_application,
        database_path=loaded.config.database.path,
    )


def restore_configured_release(
    loaded: LoadedConfiguration,
    release_id: str,
    *,
    config_path: Path,
    command_environment: Mapping[str, str] | None = None,
    dependencies: ManualRestoreDependencies | None = None,
    cancellation_event: threading.Event | None = None,
    fault_injector: FaultInjector | None = None,
) -> ManualRestoreResult:
    """Restore the protected previous release under one checked operation and lock."""
    topology = require_deploy_topology(loaded.config)
    selected_environment = dict(os.environ if command_environment is None else command_environment)
    working_directory = config_path.resolve().parent
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    releases = ReleaseStore(loaded.config.state_directory, secrets=loaded.secrets)
    lock = DeploymentLock(loaded.config.state_directory, secrets=loaded.secrets)
    owned_health: ApplicationHealthChecker | None = None
    inject = fault_injector or _no_fault

    with lock:
        require_clean_operation_state(lock, store)
        selection = preview_configured_restore(loaded, release_id)
        selected_dependencies = dependencies
        if selected_dependencies is None:
            deployment_dependencies, owned_health = default_dependencies(
                loaded,
                topology=topology,
                config_path=config_path,
                working_directory=working_directory,
                command_environment=selected_environment,
                candidate_command_runner=None,
                candidate_application_runner=None,
                candidate_health_checker=None,
            )
            selected_dependencies = _manual_dependencies(deployment_dependencies)
        operation = store.create_operation(
            operation_type="restore",
            project=loaded.config.project,
            configuration_fingerprint=configuration_fingerprint(
                loaded.config, secrets=loaded.secrets
            ),
            stage="restore_previewed",
            evidence=selection.as_dict(),
        )
        lock.record_owner(operation_id=operation.operation_id, operation_type="restore")
        context = _RestoreContext(
            loaded=loaded,
            selection=selection,
            operation_id=operation.operation_id,
            store=store,
            releases=releases,
            dependencies=selected_dependencies,
            operation_log=store.operation_paths(operation.operation_id).events,
            cancellation_event=cancellation_event,
        )
        try:
            return _run_manual_restore(context, inject=inject)
        except DployDBError:
            raise
        except Exception as raw_error:
            recovery = RecoveryRequiredError(
                "manual restore coordinator failed unexpectedly: "
                + loaded.secrets.redact_text(f"{type(raw_error).__name__}: {raw_error}"),
                production_changed=context.database_replaced,
                previous_application_running=False if context.current_stopped else None,
                log_path=context.operation_log,
                next_safe_action=(
                    "Keep maintenance enabled, preserve the pre-restore backup and operation "
                    "evidence, and inspect both recorded applications."
                ),
            )
            _finish_restore_failure(context, recovery)
            raise recovery from None
        finally:
            if owned_health is not None:
                owned_health.close()


def _run_manual_restore(
    context: _RestoreContext,
    *,
    inject: FaultInjector,
) -> ManualRestoreResult:
    try:
        current = context.dependencies.production.inspect(
            context.selection.current_application,
            expected_running=True,
            cancellation_event=context.cancellation_event,
        )
        selected = context.dependencies.production.inspect(
            context.selection.selected_application,
            expected_running=False,
            cancellation_event=context.cancellation_event,
        )
        context.store.append_event(
            context.operation_id,
            message="Active and selected application identities were inspected.",
            evidence={
                "current_application": inspection_evidence(current),
                "selected_application": inspection_evidence(selected),
            },
        )
        _enable_restore_maintenance(context)
        context.current_stop_attempted = True
        stopped = context.dependencies.production.stop_current(
            context.selection.current_application,
            cancellation_event=context.cancellation_event,
        )
        context.current_stopped = True
        context.store.transition(
            context.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="manual_restore_current_app_stopped",
            message="The exact current application was stopped and inspected.",
            evidence={"production_stop": stop_evidence(stopped)},
            safety=SafetyFacts(
                production_changed=False,
                previous_application_running=False,
                recovery_required=False,
            ),
        )
        context.pre_restore_backup = create_verified_backup(
            context.loaded.config.database.path,
            project=context.loaded.secrets.redact_text(context.loaded.config.project),
            purpose=BackupPurpose.PRE_RESTORE,
            storage=LocalBackupStorage(context.loaded.config.backup.local_directory),
            operation_id=context.operation_id,
            metadata_source_path=context.loaded.config.database.path,
        )
        context.store.transition(
            context.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="manual_restore_prepared",
            message="The selected and current-state backups are verified.",
            evidence={
                "selected_backup_id": context.selection.selected_backup.metadata.backup_id,
                "pre_restore_backup_id": context.pre_restore_backup.metadata.backup_id,
            },
            safety=SafetyFacts(
                production_changed=False,
                previous_application_running=False,
                recovery_required=False,
            ),
        )
        inject("after_pre_restore_backup")
        context.store.transition(
            context.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="manual_restore_started",
            message="Production database replacement is starting with traffic blocked.",
            safety=SafetyFacts(
                production_changed=True,
                previous_application_running=False,
                recovery_required=False,
            ),
        )
        context.database_restore = restore_verified_database(
            context.selection.selected_backup,
            context.loaded.config.database.path,
            application_stopped=True,
            traffic_activated=False,
            secrets=context.loaded.secrets,
        )
        context.database_replaced = True
        context.store.append_event(
            context.operation_id,
            message="The selected previous database was restored and verified.",
            evidence={"database_restore": context.database_restore.model_dump(mode="json")},
        )
        inject("after_database_restore")
        context.selected_restart_attempted = True
        restarted = context.dependencies.production.restart_previous(
            context.selection.selected_application,
            cancellation_event=context.cancellation_event,
        )
        context.selected_running = True
        context.store.append_event(
            context.operation_id,
            message="The selected previous application was restarted and inspected.",
            evidence={"selected_restart": restart_evidence(restarted)},
        )
        sqlite = verify_sqlite_database(context.loaded.config.database.path)
        health = context.dependencies.health.check_application(
            version=context.selection.selected_release.requested_version,
            database_path=context.loaded.config.database.path,
            cancellation_event=context.cancellation_event,
        )
        context.store.transition(
            context.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="manual_restore_selected_healthy",
            message="The selected database and application passed final checks.",
            evidence={
                "database": sqlite.model_dump(mode="json"),
                "application_health": health.as_evidence(),
                "health_summary": health_summary(
                    health,
                    role="previous",
                    version=context.selection.selected_release.requested_version,
                ).model_dump(mode="json"),
            },
            safety=SafetyFacts(
                production_changed=True,
                previous_application_running=True,
                recovery_required=False,
            ),
        )
        inject("before_traffic_activation")
        context.traffic_activation_attempted = True
        context.store.transition(
            context.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="manual_restore_traffic_activation_started",
            message="Selected-release traffic activation is about to run.",
            safety=SafetyFacts(
                production_changed=True,
                previous_application_running=True,
                recovery_required=False,
            ),
        )
        old_target = context.dependencies.traffic.activate_old(
            cancellation_event=context.cancellation_event
        )
        _record_restore_hook(context, old_target)
        if not old_target.passed:
            context.traffic_activation_attempted = _hook_may_have_run(old_target)
            raise _restore_hook_error(old_target, context)
        context.store.transition(
            context.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="manual_restore_traffic_target_selected",
            message="The selected previous traffic target is active behind maintenance.",
            evidence={"traffic_hook": old_target.as_evidence()},
            safety=SafetyFacts(
                production_changed=True,
                previous_application_running=True,
                recovery_required=False,
            ),
        )
        context.normal_traffic_may_be_enabled = True
        context.store.transition(
            context.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="manual_restore_maintenance_disable_started",
            message="Maintenance disable is about to expose the selected previous release.",
            safety=SafetyFacts(
                production_changed=True,
                previous_application_running=True,
                recovery_required=False,
            ),
        )
        maintenance_off = context.dependencies.traffic.disable_maintenance(
            cancellation_event=context.cancellation_event
        )
        _record_restore_hook(context, maintenance_off)
        if not maintenance_off.passed:
            raise _restore_hook_error(maintenance_off, context, recovery=True)
        context.maintenance_enabled = False
        inject("after_traffic_activation")
        pointers = context.releases.activate_release(context.selection.selected_release.release_id)
        assert context.pre_restore_backup is not None
        assert context.database_restore is not None
        operation = context.store.transition(
            context.operation_id,
            status=OperationStatus.SUCCEEDED,
            stage="manual_restore_completed",
            message="The protected previous release is active and verified.",
            evidence={
                "selected_release_id": context.selection.selected_release.release_id,
                "replaced_release_id": context.selection.active_release.release_id,
                "pre_restore_backup_id": context.pre_restore_backup.metadata.backup_id,
                "active_release_id": pointers.active_release_id,
                "previous_release_id": pointers.previous_release_id,
            },
            safety=SafetyFacts(
                production_changed=True,
                previous_application_running=True,
                recovery_required=False,
            ),
        )
        return ManualRestoreResult(
            operation=operation,
            selected_release=context.selection.selected_release,
            replaced_release=context.selection.active_release,
            pre_restore_backup=context.pre_restore_backup,
            database_restore=context.database_restore,
            pointers=pointers,
            operation_log_path=context.operation_log,
        )
    except BaseException as raw_error:
        error = _normalize_restore_coordinator_error(raw_error, context)
        if error is None:
            raise
        if (
            error.payload.recovery_required
            or context.traffic_activation_attempted
            or context.normal_traffic_may_be_enabled
        ):
            recovery = RecoveryRequiredError(
                "manual restore requires recovery: " + error.payload.what_failed,
                production_changed=context.database_replaced,
                previous_application_running=context.selected_running,
                log_path=context.operation_log,
                next_safe_action=(
                    "Keep the recorded database and applications unchanged. Determine the live "
                    "traffic and maintenance state before any database restore."
                ),
            )
            _finish_restore_failure(context, recovery)
            raise recovery from None
        try:
            rolled_back = _rollback_manual_restore(context, error)
        except DployDBError as rollback_error:
            _finish_restore_failure(context, rollback_error)
            raise rollback_error from None
        _finish_restore_failure(context, rolled_back)
        raise rolled_back from None


def _rollback_manual_restore(
    context: _RestoreContext,
    original: DployDBError,
) -> OperationFailedError:
    try:
        if context.selected_running:
            context.dependencies.production.stop_current(
                context.selection.selected_application,
                cancellation_event=context.cancellation_event,
            )
            context.selected_running = False
        if context.database_replaced:
            if context.pre_restore_backup is None:
                raise RuntimeError("pre-restore backup identity is unavailable")
            restore_verified_database(
                context.pre_restore_backup,
                context.loaded.config.database.path,
                application_stopped=True,
                traffic_activated=False,
                secrets=context.loaded.secrets,
            )
        if context.current_stopped:
            context.dependencies.production.restart_previous(
                context.selection.current_application,
                cancellation_event=context.cancellation_event,
            )
        current_target = context.dependencies.traffic.activate_new(
            cancellation_event=context.cancellation_event
        )
        _record_restore_hook(context, current_target)
        if not current_target.passed:
            raise RuntimeError("current traffic target could not be restored")
        maintenance_off = context.dependencies.traffic.disable_maintenance(
            cancellation_event=context.cancellation_event
        )
        _record_restore_hook(context, maintenance_off)
        if not maintenance_off.passed:
            raise RuntimeError("maintenance could not be disabled after restore rollback")
        context.dependencies.health.check_application(
            version=context.selection.active_release.requested_version,
            database_path=context.loaded.config.database.path,
            cancellation_event=context.cancellation_event,
        )
        verify_sqlite_database(context.loaded.config.database.path)
    except Exception as rollback_error:
        raise RecoveryRequiredError(
            "manual restore failed and the original release could not be proven restored: "
            + context.loaded.secrets.redact_text(str(rollback_error)),
            production_changed=context.database_replaced,
            previous_application_running=False,
            log_path=context.operation_log,
            next_safe_action=(
                "Keep maintenance enabled and restore the recorded pre-restore backup and active "
                "application manually."
            ),
        ) from None
    return OperationFailedError(
        "manual restore did not complete; the original database and application were restored "
        "and verified: " + original.payload.what_failed,
        production_changed=context.database_replaced,
        previous_application_running=True,
        log_path=context.operation_log,
        next_safe_action="Inspect the restore log, correct the failure, and request restore again.",
    )


def _enable_restore_maintenance(context: _RestoreContext) -> None:
    result = context.dependencies.traffic.enable_maintenance(
        cancellation_event=context.cancellation_event
    )
    _record_restore_hook(context, result)
    if not result.passed:
        cleanup = context.dependencies.traffic.disable_maintenance(
            cancellation_event=context.cancellation_event
        )
        _record_restore_hook(context, cleanup)
        if not cleanup.passed:
            raise RecoveryRequiredError(
                "maintenance enable failed and cleanup could not be proven",
                production_changed=False,
                previous_application_running=True,
                log_path=context.operation_log,
                next_safe_action="Keep the current application running and disable maintenance.",
            )
        raise _restore_hook_error(result, context)
    context.maintenance_enabled = True
    context.store.transition(
        context.operation_id,
        status=OperationStatus.IN_PROGRESS,
        stage="manual_restore_maintenance_enabled",
        message="Maintenance mode is enabled before manual restore.",
        evidence={"traffic_hook": result.as_evidence()},
        safety=SafetyFacts(
            production_changed=False,
            previous_application_running=True,
            recovery_required=False,
        ),
    )


def _record_restore_hook(context: _RestoreContext, result: TrafficHookResult) -> None:
    context.store.append_event(
        context.operation_id,
        message=f"Manual restore traffic hook {result.action.value} reached a terminal outcome.",
        evidence={
            "traffic_hook": result.as_evidence(),
            "hook_summary": hook_summary(result).model_dump(mode="json"),
        },
    )


def _restore_hook_error(
    result: TrafficHookResult,
    context: _RestoreContext,
    *,
    recovery: bool = False,
) -> DployDBError:
    error_type: type[DployDBError] = (
        RecoveryRequiredError
        if recovery or result.command.outcome is CommandOutcome.CLEANUP_FAILED
        else ExternalCommandError
    )
    return error_type(
        f"manual restore traffic hook {result.action.value} ended {result.command.outcome.value}",
        production_changed=context.database_replaced,
        previous_application_running=context.selected_running,
        log_path=context.operation_log,
        next_safe_action="Keep maintenance enabled and inspect the recorded hook evidence.",
    )


def _hook_may_have_run(result: TrafficHookResult) -> bool:
    return not (
        result.command.outcome is CommandOutcome.START_FAILED
        or (result.command.outcome is CommandOutcome.CANCELLED and result.command.exit_code is None)
    )


def _normalize_restore_coordinator_error(
    error: BaseException,
    context: _RestoreContext,
) -> DployDBError | None:
    if isinstance(error, DployDBError):
        return error
    if not isinstance(error, Exception):
        return None
    if context.current_stop_attempted and not context.current_stopped:
        return RecoveryRequiredError(
            "the current application stop was attempted without durable stopped proof",
            production_changed=False,
            previous_application_running=None,
            log_path=context.operation_log,
            next_safe_action="Keep maintenance enabled and inspect the exact current container.",
        )
    if context.selected_restart_attempted and not context.selected_running:
        return RecoveryRequiredError(
            "the selected application restart was attempted without durable running proof",
            production_changed=context.database_replaced,
            previous_application_running=None,
            log_path=context.operation_log,
            next_safe_action=(
                "Keep maintenance enabled and inspect both exact containers before database "
                "rollback."
            ),
        )
    return OperationFailedError(
        "manual restore boundary failed: "
        + context.loaded.secrets.redact_text(f"{type(error).__name__}: {error}"),
        production_changed=context.database_replaced,
        previous_application_running=(False if context.current_stopped else True),
        log_path=context.operation_log,
        next_safe_action="Keep maintenance enabled until the original release is restored.",
    )


def _finish_restore_failure(context: _RestoreContext, error: DployDBError) -> None:
    current = context.store.read_manifest(context.operation_id)
    if current.status is not OperationStatus.IN_PROGRESS:
        return
    status = (
        OperationStatus.RECOVERY_REQUIRED
        if error.payload.recovery_required
        else OperationStatus.FAILED_SAFE
    )
    context.store.transition(
        context.operation_id,
        status=status,
        stage=status.value,
        message="Manual restore did not complete.",
        safety=SafetyFacts(
            production_changed=error.payload.production_changed,
            previous_application_running=error.payload.previous_application_running,
            recovery_required=error.payload.recovery_required,
        ),
        failure=FailureRecord(
            error_code=error.payload.error_code,
            what_failed=error.payload.what_failed,
            log_path=error.payload.log_path,
            next_safe_action=error.payload.next_safe_action,
        ),
    )


def _manual_dependencies(dependencies: DeploymentDependencies) -> ManualRestoreDependencies:
    return ManualRestoreDependencies(
        production=dependencies.production,
        traffic=dependencies.traffic,
        health=dependencies.health,
    )


def _no_fault(_stage: str) -> None:
    return
