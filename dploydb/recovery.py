"""Crash-safe recovery diagnosis and execution planning."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from dploydb.backup import calculate_sha256, open_verified_configured_backup
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
from dploydb.deployment_evidence import cleanup_evidence, hook_summary, restart_evidence
from dploydb.errors import (
    DployDBError,
    LockUnavailableError,
    RecoveryRequiredError,
    SafetyCheckError,
)
from dploydb.health import ApplicationHealthChecker
from dploydb.locking import DeploymentLock, LockInspectionState, inspect_lock
from dploydb.models import (
    DeploymentState,
    FailureRecord,
    LockOwnerState,
    OperationEvent,
    OperationManifest,
    OperationStatus,
    ProductionApplicationHandle,
    ReleaseManifest,
    SafetyFacts,
)
from dploydb.releases import ReleaseStore
from dploydb.restore import restore_verified_database
from dploydb.runners.base import (
    ProductionApplicationRunner,
    ProductionCleanupProof,
    ProductionInspection,
    ProductionInspectionError,
)
from dploydb.sqlite_checks import verify_sqlite_database
from dploydb.state import StateStore
from dploydb.storage.base import RemoteBackupStorage
from dploydb.traffic import TrafficController, TrafficHookResult


class ApplicationRuntimeState(StrEnum):
    """Live state of one exact recorded production application."""

    RUNNING = "running"
    STOPPED = "stopped"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class RecoveryDisposition(StrEnum):
    """Top-level decision produced before any recovery mutation."""

    NO_ACTION = "no_action"
    RECOVER_PREVIOUS = "recover_previous"
    COMPLETE_NEW = "complete_new"
    MANUAL_REQUIRED = "manual_required"


class RecoveryAction(StrEnum):
    """Ordered, individually provable recovery actions."""

    REMOVE_NEW_APPLICATION = "remove_new_application"
    RESTORE_FINAL_BACKUP = "restore_final_backup"
    RESTART_PREVIOUS_APPLICATION = "restart_previous_application"
    ACTIVATE_PREVIOUS_TRAFFIC = "activate_previous_traffic"
    DISABLE_MAINTENANCE = "disable_maintenance"
    VERIFY_PREVIOUS = "verify_previous"
    MARK_ROLLED_BACK = "mark_rolled_back"
    VERIFY_NEW = "verify_new"
    MARK_ACTIVE = "mark_active"


@dataclass(frozen=True, slots=True)
class RecoveryLiveState:
    """Read-only facts collected from exact live resources and verified files."""

    previous_application: ApplicationRuntimeState
    new_application: ApplicationRuntimeState
    final_backup_verified: bool
    production_database_sha256: str | None
    final_backup_sha256: str | None


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """A deterministic plan that authorizes no action outside its ordered list."""

    disposition: RecoveryDisposition
    release_id: str
    operation_id: str
    durable_stage: str
    production_may_have_changed: bool
    traffic_may_have_switched: bool
    automatic_database_restore_allowed: bool
    actions: tuple[RecoveryAction, ...]
    reason: str
    next_safe_action: str
    final_backup_id: str | None

    @property
    def executable(self) -> bool:
        return self.disposition in {
            RecoveryDisposition.RECOVER_PREVIOUS,
            RecoveryDisposition.COMPLETE_NEW,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "release_id": self.release_id,
            "operation_id": self.operation_id,
            "durable_stage": self.durable_stage,
            "production_may_have_changed": self.production_may_have_changed,
            "traffic_may_have_switched": self.traffic_may_have_switched,
            "automatic_database_restore_allowed": self.automatic_database_restore_allowed,
            "actions": [action.value for action in self.actions],
            "reason": self.reason,
            "next_safe_action": self.next_safe_action,
            "final_backup_id": self.final_backup_id,
        }


class RecoveryApplicationInspector(Protocol):
    """Read-only application inspection needed before planning recovery."""

    def inspect_live(
        self,
        handle: ProductionApplicationHandle,
    ) -> ProductionInspection: ...

    def prove_release_absent(
        self,
        *,
        release_id: str,
        version: str,
    ) -> ProductionCleanupProof: ...


@dataclass(frozen=True, slots=True)
class RecoveryDependencies:
    """Operational boundaries used after the read-only plan authorizes recovery."""

    production: ProductionApplicationRunner
    traffic: TrafficController
    health: ProductionHealthBoundary


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Proven terminal result of an executed recovery plan."""

    plan: RecoveryPlan
    operation: OperationManifest
    release: ReleaseManifest
    operation_log_path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "command": "recover",
            "outcome": (
                "active" if self.release.status is DeploymentState.ACTIVE else "rolled_back"
            ),
            "recovery_operation_id": self.operation.operation_id,
            "source_operation_id": self.plan.operation_id,
            "release_id": self.release.release_id,
            "release_status": self.release.status.value,
            "production_changed": self.operation.safety.production_changed,
            "previous_application_running": (self.operation.safety.previous_application_running),
            "recovery_required": False,
            "actions": [action.value for action in self.plan.actions],
            "log_path": str(self.operation_log_path),
        }


FaultInjector = Callable[[str], None]


def preview_configured_recovery(
    loaded: LoadedConfiguration,
    *,
    config_path: Path,
    command_environment: Mapping[str, str] | None = None,
    dependencies: RecoveryDependencies | None = None,
    remote_storage: RemoteBackupStorage | None = None,
) -> RecoveryPlan:
    """Build a live recovery plan without acquiring a mutating lock or writing state."""
    environment = dict(os.environ if command_environment is None else command_environment)
    selected, owned_health = _configured_recovery_dependencies(
        loaded,
        config_path=config_path,
        command_environment=environment,
        dependencies=dependencies,
    )
    try:
        return diagnose_configured_recovery(
            loaded,
            application_inspector=selected.production,
            environment=environment,
            remote_storage=remote_storage,
        )
    finally:
        if owned_health is not None:
            owned_health.close()


def recover_configured_deployment(
    loaded: LoadedConfiguration,
    *,
    config_path: Path,
    command_environment: Mapping[str, str] | None = None,
    dependencies: RecoveryDependencies | None = None,
    remote_storage: RemoteBackupStorage | None = None,
    cancellation_event: threading.Event | None = None,
    fault_injector: FaultInjector | None = None,
) -> RecoveryResult:
    """Execute only a freshly revalidated, deterministic recovery plan."""
    environment = dict(os.environ if command_environment is None else command_environment)
    selected, owned_health = _configured_recovery_dependencies(
        loaded,
        config_path=config_path,
        command_environment=environment,
        dependencies=dependencies,
    )
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    releases = ReleaseStore(loaded.config.state_directory, secrets=loaded.secrets)
    lock = DeploymentLock(loaded.config.state_directory, secrets=loaded.secrets)
    inject = fault_injector or _no_fault
    try:
        with lock:
            plan = diagnose_configured_recovery(
                loaded,
                application_inspector=selected.production,
                check_lock=False,
                environment=environment,
                remote_storage=remote_storage,
            )
            if not plan.executable:
                raise RecoveryRequiredError(
                    plan.reason,
                    production_changed=plan.production_may_have_changed,
                    previous_application_running=None,
                    log_path=store.operation_paths(plan.operation_id).events,
                    next_safe_action=plan.next_safe_action,
                )
            release = releases.read_manifest(plan.release_id)
            stale_owner_id = _acknowledged_stale_owner_id(lock, store, plan)
            _take_recovery_ownership(store, releases, plan, release)
            operation = store.create_operation(
                operation_type="recover",
                project=loaded.config.project,
                configuration_fingerprint=configuration_fingerprint(
                    loaded.config, secrets=loaded.secrets
                ),
                stage="recovery_started",
                evidence={"source_plan": plan.as_dict()},
            )
            lock.record_owner(
                operation_id=operation.operation_id,
                operation_type="recover",
                replace_stale_owner_id=stale_owner_id,
            )
            operation_log = store.operation_paths(operation.operation_id).events
            try:
                return _execute_recovery_plan(
                    loaded,
                    plan=plan,
                    operation_id=operation.operation_id,
                    store=store,
                    releases=releases,
                    dependencies=selected,
                    operation_log=operation_log,
                    cancellation_event=cancellation_event,
                    inject=inject,
                    environment=environment,
                    remote_storage=remote_storage,
                )
            except BaseException as raw_error:
                error = _normalize_recovery_error(raw_error, plan, operation_log, loaded)
                if error is None:
                    raise
                _finish_recovery_operation(store, operation.operation_id, error)
                raise error from None
    finally:
        if owned_health is not None:
            owned_health.close()


def diagnose_configured_recovery(
    loaded: LoadedConfiguration,
    *,
    application_inspector: RecoveryApplicationInspector,
    check_lock: bool = True,
    environment: Mapping[str, str] | None = None,
    remote_storage: RemoteBackupStorage | None = None,
) -> RecoveryPlan:
    """Read all durable/live evidence and return a plan without changing state."""
    state_directory = loaded.config.state_directory
    if check_lock:
        lock = inspect_lock(state_directory, secrets=loaded.secrets)
        if lock.state is LockInspectionState.ACTIVE:
            raise LockUnavailableError(
                "another DployDB operation still holds the deployment lock",
                production_changed=False,
                previous_application_running=None,
                log_path=lock.owner_path,
                next_safe_action=(
                    "Wait for the active operation, then run recovery diagnosis again."
                ),
            )
        if lock.state is LockInspectionState.RECOVERY_REQUIRED:
            raise RecoveryRequiredError(
                lock.metadata_error or "deployment lock evidence is contradictory",
                production_changed=True,
                previous_application_running=None,
                log_path=lock.owner_path,
                next_safe_action="Preserve the lock evidence and inspect the host manually.",
            )

    store = StateStore(state_directory, secrets=loaded.secrets)
    operations = store.list_operations()
    if not operations:
        raise SafetyCheckError(
            "there is no deployment operation to recover",
            production_changed=False,
            previous_application_running=None,
            log_path=state_directory,
            next_safe_action="Run dploydb status; no recovery action is required.",
        )
    history = ReleaseStore(state_directory, secrets=loaded.secrets).read_history()
    operations_by_id = {item.operation_id: item for item in operations}
    incomplete_resolution = next(
        (
            (release, operations_by_id[release.recovery_operation_id])
            for release in history.releases
            if release.recovery_operation_id is not None
            and release.recovery_operation_id in operations_by_id
            and operations_by_id[release.recovery_operation_id].status
            is not OperationStatus.SUCCEEDED
        ),
        None,
    )
    if incomplete_resolution is not None:
        resolved_release, recovery_operation = incomplete_resolution
        raise RecoveryRequiredError(
            "release recovery was resolved but its recovery operation is not durably complete",
            production_changed=resolved_release.production_changed,
            previous_application_running=(resolved_release.status is DeploymentState.ROLLED_BACK),
            log_path=store.operation_paths(recovery_operation.operation_id).events,
            next_safe_action=(
                "Do not repeat database actions; preserve both records and reconcile the "
                "recovery operation manually."
            ),
        )
    unresolved = [
        item
        for item in history.releases
        if item.status
        not in {
            DeploymentState.ACTIVE,
            DeploymentState.ROLLED_BACK,
            DeploymentState.FAILED_SAFE,
        }
        and item.operation_id in operations_by_id
    ]
    if len(unresolved) > 1:
        raise RecoveryRequiredError(
            "multiple unresolved releases make automatic recovery ambiguous",
            production_changed=True,
            previous_application_running=None,
            log_path=state_directory,
            next_safe_action="Preserve state and correlate the operations and releases manually.",
        )
    if unresolved:
        release = unresolved[0]
        operation = operations_by_id[release.operation_id]
    else:
        paired = [
            (item, operations_by_id[item.operation_id])
            for item in history.releases
            if item.operation_id in operations_by_id
        ]
        if not paired:
            raise RecoveryRequiredError(
                "no deployment operation maps to a release manifest",
                production_changed=False,
                previous_application_running=None,
                log_path=state_directory,
                next_safe_action="Preserve state and correlate operation history manually.",
            )
        release, operation = max(
            paired,
            key=lambda item: (item[1].started_at, item[1].operation_id),
        )
    events = store.read_events(operation.operation_id)
    previous_state = _inspect_application(application_inspector, release.previous_application)
    new_state = _inspect_new_application(application_inspector, release)
    final_verified = False
    final_sha256: str | None = None
    if release.final_backup_id is not None:
        try:
            with open_verified_configured_backup(
                loaded,
                release.final_backup_id,
                environment=environment,
                remote_storage=remote_storage,
            ) as artifact:
                final_verified = artifact.metadata.sha256 == release.final_backup_sha256
                final_sha256 = artifact.metadata.sha256
        except Exception:
            pass
    try:
        _size_bytes, production_sha256 = calculate_sha256(loaded.config.database.path)
    except Exception:
        production_sha256 = None
    return build_recovery_plan(
        operation,
        events,
        release,
        RecoveryLiveState(
            previous_application=previous_state,
            new_application=new_state,
            final_backup_verified=final_verified,
            production_database_sha256=production_sha256,
            final_backup_sha256=final_sha256,
        ),
    )


def _inspect_application(
    inspector: RecoveryApplicationInspector,
    handle: ProductionApplicationHandle | None,
) -> ApplicationRuntimeState:
    if handle is None:
        return ApplicationRuntimeState.UNKNOWN
    try:
        inspection = inspector.inspect_live(handle)
    except ProductionInspectionError:
        return ApplicationRuntimeState.UNKNOWN
    return (
        ApplicationRuntimeState.RUNNING if inspection.running else ApplicationRuntimeState.STOPPED
    )


def _inspect_new_application(
    inspector: RecoveryApplicationInspector,
    release: ReleaseManifest,
) -> ApplicationRuntimeState:
    if release.new_application is not None:
        state = _inspect_application(inspector, release.new_application)
        if state is not ApplicationRuntimeState.UNKNOWN:
            return state
    try:
        proof = inspector.prove_release_absent(
            release_id=release.release_id,
            version=release.requested_version,
        )
    except Exception:
        return ApplicationRuntimeState.UNKNOWN
    return ApplicationRuntimeState.ABSENT if proof.proven else ApplicationRuntimeState.UNKNOWN


def _configured_recovery_dependencies(
    loaded: LoadedConfiguration,
    *,
    config_path: Path,
    command_environment: Mapping[str, str] | None,
    dependencies: RecoveryDependencies | None,
) -> tuple[RecoveryDependencies, ApplicationHealthChecker | None]:
    if dependencies is not None:
        return dependencies, None
    topology = require_deploy_topology(loaded.config)
    environment = dict(os.environ if command_environment is None else command_environment)
    configured, health = default_dependencies(
        loaded,
        topology=topology,
        config_path=config_path,
        working_directory=config_path.resolve().parent,
        command_environment=environment,
        candidate_command_runner=None,
        candidate_application_runner=None,
        candidate_health_checker=None,
    )
    return _recovery_dependencies(configured), health


def _recovery_dependencies(dependencies: DeploymentDependencies) -> RecoveryDependencies:
    return RecoveryDependencies(
        production=dependencies.production,
        traffic=dependencies.traffic,
        health=dependencies.health,
    )


def _acknowledged_stale_owner_id(
    lock: DeploymentLock,
    store: StateStore,
    plan: RecoveryPlan,
) -> str | None:
    """Authorize replacing only the dead owner of this exact recovery lineage."""
    owner = lock.previous_owner
    if owner is None or owner.state is LockOwnerState.RELEASED:
        return None
    if owner.operation_id == plan.operation_id:
        return owner.owner_id

    try:
        owner_operation = store.read_manifest(owner.operation_id)
        owner_events = store.read_events(owner.operation_id)
    except DployDBError as exc:
        raise RecoveryRequiredError(
            "stale deployment-lock ownership cannot be correlated to durable state",
            production_changed=plan.production_may_have_changed,
            previous_application_running=None,
            log_path=lock.owner_path,
            next_safe_action="Preserve the lock metadata and operation history for inspection.",
        ) from exc
    source_plan = owner_events[0].evidence.get("source_plan") if owner_events else None
    related_recovery = (
        owner_operation.operation_type == "recover"
        and owner_operation.status is OperationStatus.IN_PROGRESS
        and isinstance(source_plan, dict)
        and source_plan.get("operation_id") == plan.operation_id
        and source_plan.get("release_id") == plan.release_id
    )
    if related_recovery:
        return owner.owner_id
    raise RecoveryRequiredError(
        "stale deployment-lock ownership belongs to a different operation",
        production_changed=plan.production_may_have_changed,
        previous_application_running=None,
        log_path=lock.owner_path,
        next_safe_action=(
            "Do not replace the owner token; reconcile the recorded lock and operations."
        ),
    )


def _take_recovery_ownership(
    store: StateStore,
    releases: ReleaseStore,
    plan: RecoveryPlan,
    release: ReleaseManifest,
) -> None:
    failure = FailureRecord(
        error_code="recovery_required",
        what_failed="The prior operation was interrupted and recovery took ownership.",
        log_path=str(store.operation_paths(plan.operation_id).events),
        next_safe_action="Continue only through the recorded recovery plan.",
    )
    for operation in store.list_operations():
        if operation.status is not OperationStatus.IN_PROGRESS:
            continue
        if operation.operation_id != plan.operation_id and operation.operation_type != "recover":
            raise RecoveryRequiredError(
                "an unrelated unfinished operation blocks automatic recovery",
                production_changed=plan.production_may_have_changed,
                previous_application_running=None,
                log_path=store.operation_paths(operation.operation_id).events,
                next_safe_action="Resolve the unrelated operation before deployment recovery.",
            )
        store.transition(
            operation.operation_id,
            status=OperationStatus.RECOVERY_REQUIRED,
            stage="recovery_required",
            message="An interrupted operation was superseded by explicit recovery.",
            safety=SafetyFacts(
                production_changed=(
                    operation.safety.production_changed or plan.production_may_have_changed
                ),
                previous_application_running=None,
                recovery_required=True,
            ),
            failure=failure,
        )
    if release.status is not DeploymentState.RECOVERY_REQUIRED:
        releases.transition(
            release.release_id,
            status=DeploymentState.RECOVERY_REQUIRED,
            production_changed=plan.production_may_have_changed,
            traffic_activated=(plan.disposition is RecoveryDisposition.COMPLETE_NEW),
            failure=failure,
        )


def _execute_recovery_plan(
    loaded: LoadedConfiguration,
    *,
    plan: RecoveryPlan,
    operation_id: str,
    store: StateStore,
    releases: ReleaseStore,
    dependencies: RecoveryDependencies,
    operation_log: Path,
    cancellation_event: threading.Event | None,
    inject: FaultInjector,
    environment: Mapping[str, str],
    remote_storage: RemoteBackupStorage | None,
) -> RecoveryResult:
    release = releases.read_manifest(plan.release_id)
    previous = release.previous_application
    new = release.new_application
    for action in plan.actions:
        store.transition(
            operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage=f"recovery_{action.value}",
            message=f"Recovery action {action.value} is starting.",
            evidence={"action": action.value},
            safety=SafetyFacts(
                production_changed=plan.production_may_have_changed,
                previous_application_running=None,
                recovery_required=False,
            ),
        )
        if action is RecoveryAction.REMOVE_NEW_APPLICATION:
            if new is None:
                raise RecoveryRequiredError(
                    "recovery plan requires a missing new-application handle",
                    production_changed=plan.production_may_have_changed,
                    previous_application_running=False,
                    log_path=operation_log,
                    next_safe_action="Keep traffic blocked and inspect release resources.",
                )
            cleanup = dependencies.production.remove_new(new)
            store.append_event(
                operation_id,
                message="New-release cleanup was proven during recovery.",
                evidence={"production_cleanup": cleanup_evidence(cleanup)},
            )
        elif action is RecoveryAction.RESTORE_FINAL_BACKUP:
            _execute_database_recovery(
                loaded,
                release=release,
                previous=previous,
                dependencies=dependencies,
                operation_id=operation_id,
                store=store,
                operation_log=operation_log,
                cancellation_event=cancellation_event,
                environment=environment,
                remote_storage=remote_storage,
            )
        elif action is RecoveryAction.RESTART_PREVIOUS_APPLICATION:
            if previous is None:
                raise RecoveryRequiredError(
                    "recovery previous-application identity is missing",
                    production_changed=plan.production_may_have_changed,
                    previous_application_running=None,
                    log_path=operation_log,
                    next_safe_action="Keep maintenance enabled and inspect production.",
                )
            restarted = dependencies.production.restart_previous(
                previous,
                cancellation_event=cancellation_event,
            )
            store.append_event(
                operation_id,
                message="The exact previous application was restarted during recovery.",
                evidence={"previous_restart": restart_evidence(restarted)},
            )
        elif action is RecoveryAction.ACTIVATE_PREVIOUS_TRAFFIC:
            hook = dependencies.traffic.activate_old(cancellation_event=cancellation_event)
            _record_recovery_hook(store, operation_id, hook)
            _require_recovery_hook(hook, operation_log, plan)
        elif action is RecoveryAction.DISABLE_MAINTENANCE:
            hook = dependencies.traffic.disable_maintenance(cancellation_event=cancellation_event)
            _record_recovery_hook(store, operation_id, hook)
            _require_recovery_hook(hook, operation_log, plan)
        elif action is RecoveryAction.VERIFY_PREVIOUS:
            _verify_recovery_application(
                loaded,
                handle=previous,
                version=(None if previous is None else previous.version) or "previous",
                dependencies=dependencies,
                operation_id=operation_id,
                store=store,
                operation_log=operation_log,
                cancellation_event=cancellation_event,
                role="previous",
            )
        elif action is RecoveryAction.VERIFY_NEW:
            if new is not None:
                dependencies.production.inspect(
                    new,
                    expected_running=True,
                    cancellation_event=cancellation_event,
                )
            _verify_recovery_application(
                loaded,
                handle=new,
                version=release.requested_version,
                dependencies=dependencies,
                operation_id=operation_id,
                store=store,
                operation_log=operation_log,
                cancellation_event=cancellation_event,
                role="new",
            )
        elif action in {RecoveryAction.MARK_ROLLED_BACK, RecoveryAction.MARK_ACTIVE}:
            return _complete_recovery(
                plan,
                action=action,
                operation_id=operation_id,
                store=store,
                releases=releases,
                release=release,
                operation_log=operation_log,
            )
        inject(f"after_{action.value}")
    raise RecoveryRequiredError(
        "recovery plan ended without a terminal marking action",
        production_changed=plan.production_may_have_changed,
        previous_application_running=None,
        log_path=operation_log,
        next_safe_action="Preserve recovery evidence and inspect the plan.",
    )


def _execute_database_recovery(
    loaded: LoadedConfiguration,
    *,
    release: ReleaseManifest,
    previous: ProductionApplicationHandle | None,
    dependencies: RecoveryDependencies,
    operation_id: str,
    store: StateStore,
    operation_log: Path,
    cancellation_event: threading.Event | None,
    environment: Mapping[str, str],
    remote_storage: RemoteBackupStorage | None,
) -> None:
    if previous is None or release.final_backup_id is None:
        raise RecoveryRequiredError(
            "recovery database prerequisites are incomplete",
            production_changed=True,
            previous_application_running=None,
            log_path=operation_log,
            next_safe_action="Keep every application stopped and inspect the final backup.",
        )
    dependencies.production.inspect(
        previous,
        expected_running=False,
        cancellation_event=cancellation_event,
    )
    with open_verified_configured_backup(
        loaded,
        release.final_backup_id,
        environment=environment,
        remote_storage=remote_storage,
    ) as artifact:
        if artifact.metadata.sha256 != release.final_backup_sha256:
            raise RecoveryRequiredError(
                "recovery final backup checksum contradicts the release manifest",
                production_changed=True,
                previous_application_running=False,
                log_path=artifact.metadata_path,
                next_safe_action="Do not restore the contradictory backup.",
            )
        restored = restore_verified_database(
            artifact,
            loaded.config.database.path,
            application_stopped=True,
            traffic_activated=False,
            secrets=loaded.secrets,
        )
    store.append_event(
        operation_id,
        message="The verified final backup was restored during recovery.",
        evidence={"database_restore": restored.model_dump(mode="json")},
    )


def _verify_recovery_application(
    loaded: LoadedConfiguration,
    *,
    handle: ProductionApplicationHandle | None,
    version: str,
    dependencies: RecoveryDependencies,
    operation_id: str,
    store: StateStore,
    operation_log: Path,
    cancellation_event: threading.Event | None,
    role: str,
) -> None:
    if handle is None:
        raise RecoveryRequiredError(
            f"{role} application identity is missing for recovery verification",
            production_changed=True,
            previous_application_running=None,
            log_path=operation_log,
            next_safe_action="Inspect the live production application manually.",
        )
    database = verify_sqlite_database(loaded.config.database.path)
    health = dependencies.health.check_application(
        version=version,
        database_path=loaded.config.database.path,
        cancellation_event=cancellation_event,
    )
    store.append_event(
        operation_id,
        message=f"{role.capitalize()} database and application health passed recovery checks.",
        evidence={
            "database": database.model_dump(mode="json"),
            "application_health": health.as_evidence(),
        },
    )


def _complete_recovery(
    plan: RecoveryPlan,
    *,
    action: RecoveryAction,
    operation_id: str,
    store: StateStore,
    releases: ReleaseStore,
    release: ReleaseManifest,
    operation_log: Path,
) -> RecoveryResult:
    active = action is RecoveryAction.MARK_ACTIVE
    resolved = releases.resolve_recovery(
        release.release_id,
        status=(DeploymentState.ACTIVE if active else DeploymentState.ROLLED_BACK),
        recovery_operation_id=operation_id,
        traffic_activated=active,
    )
    evidence: dict[str, object] = {"release_id": resolved.release_id}
    if active:
        pointers = releases.activate_release(resolved.release_id)
        evidence["active_release_id"] = pointers.active_release_id
    operation = store.transition(
        operation_id,
        status=OperationStatus.SUCCEEDED,
        stage="recovered_active" if active else "recovered_rolled_back",
        message=(
            "Recovery verified and activated the new release."
            if active
            else "Recovery restored and verified the previous release."
        ),
        evidence=evidence,
        safety=SafetyFacts(
            production_changed=plan.production_may_have_changed,
            previous_application_running=not active,
            recovery_required=False,
        ),
    )
    return RecoveryResult(
        plan=plan,
        operation=operation,
        release=resolved,
        operation_log_path=operation_log,
    )


def _record_recovery_hook(
    store: StateStore,
    operation_id: str,
    hook: TrafficHookResult,
) -> None:
    store.append_event(
        operation_id,
        message=f"Recovery traffic hook {hook.action.value} reached a terminal outcome.",
        evidence={
            "traffic_hook": hook.as_evidence(),
            "hook_summary": hook_summary(hook).model_dump(mode="json"),
        },
    )


def _require_recovery_hook(
    hook: TrafficHookResult,
    operation_log: Path,
    plan: RecoveryPlan,
) -> None:
    if hook.passed:
        return
    raise RecoveryRequiredError(
        f"recovery traffic hook {hook.action.value} ended {hook.command.outcome.value}",
        production_changed=plan.production_may_have_changed,
        previous_application_running=None,
        log_path=operation_log,
        next_safe_action="Keep maintenance enabled and inspect the recorded hook evidence.",
    )


def _normalize_recovery_error(
    error: BaseException,
    plan: RecoveryPlan,
    operation_log: Path,
    loaded: LoadedConfiguration,
) -> DployDBError | None:
    if not isinstance(error, Exception):
        return None
    detail = (
        error.payload.what_failed
        if isinstance(error, DployDBError)
        else loaded.secrets.redact_text(f"{type(error).__name__}: {error}")
    )
    return RecoveryRequiredError(
        "recovery execution could not be proven: " + detail,
        production_changed=plan.production_may_have_changed,
        previous_application_running=None,
        log_path=operation_log,
        next_safe_action=(
            "Preserve the recovery log and rerun dploydb recover; repeated actions are "
            "re-inspected before execution."
        ),
    )


def _finish_recovery_operation(
    store: StateStore,
    operation_id: str,
    error: DployDBError,
) -> None:
    current = store.read_manifest(operation_id)
    if current.status is not OperationStatus.IN_PROGRESS:
        return
    store.transition(
        operation_id,
        status=OperationStatus.RECOVERY_REQUIRED,
        stage="recovery_required",
        message="Recovery execution did not reach a proven terminal state.",
        safety=SafetyFacts(
            production_changed=error.payload.production_changed,
            previous_application_running=error.payload.previous_application_running,
            recovery_required=True,
        ),
        failure=FailureRecord(
            error_code=error.payload.error_code,
            what_failed=error.payload.what_failed,
            log_path=error.payload.log_path,
            next_safe_action=error.payload.next_safe_action,
        ),
    )


def _no_fault(_stage: str) -> None:
    return


def build_recovery_plan(
    operation: OperationManifest,
    events: list[OperationEvent],
    release: ReleaseManifest,
    live: RecoveryLiveState,
) -> RecoveryPlan:
    """Reconcile durable and live facts without performing a mutation."""
    mismatch = _identity_problem(operation, events, release)
    if mismatch is not None:
        return _manual(operation, release, mismatch, production_changed=True)

    if release.status in {
        DeploymentState.ACTIVE,
        DeploymentState.ROLLED_BACK,
        DeploymentState.FAILED_SAFE,
    } and (
        operation.status is not OperationStatus.IN_PROGRESS
        or release.recovery_operation_id is not None
    ):
        return RecoveryPlan(
            disposition=RecoveryDisposition.NO_ACTION,
            release_id=release.release_id,
            operation_id=operation.operation_id,
            durable_stage=operation.stage,
            production_may_have_changed=release.production_changed,
            traffic_may_have_switched=release.traffic_activated,
            automatic_database_restore_allowed=not release.traffic_activated,
            actions=(),
            reason="The deployment already has a proven terminal state.",
            next_safe_action="No recovery action is required.",
            final_backup_id=release.final_backup_id,
        )

    if release.recovery_protocol_version != 2:
        return _manual(
            operation,
            release,
            "The interrupted release predates durable recovery intent markers.",
            production_changed=True,
        )

    activation = _activation_conclusion(release, events)
    production_changed = (
        release.production_changed
        or release.production_migration_started
        or operation.safety.production_changed
    )
    if activation == "succeeded":
        if live.new_application is not ApplicationRuntimeState.RUNNING:
            return _manual(
                operation,
                release,
                "New traffic was activated but the exact checked application is not running.",
                production_changed=True,
                traffic_may_have_switched=True,
            )
        return RecoveryPlan(
            disposition=RecoveryDisposition.COMPLETE_NEW,
            release_id=release.release_id,
            operation_id=operation.operation_id,
            durable_stage=operation.stage,
            production_may_have_changed=True,
            traffic_may_have_switched=True,
            automatic_database_restore_allowed=False,
            actions=(
                RecoveryAction.DISABLE_MAINTENANCE,
                RecoveryAction.VERIFY_NEW,
                RecoveryAction.MARK_ACTIVE,
            ),
            reason="New traffic activation succeeded before the operation was interrupted.",
            next_safe_action=(
                "Keep the new database and application; finish maintenance cleanup and verify "
                "the new release."
            ),
            final_backup_id=release.final_backup_id,
        )
    if activation == "uncertain":
        return _manual(
            operation,
            release,
            "New-traffic activation was attempted without proof of the live target.",
            production_changed=True,
            traffic_may_have_switched=True,
        )

    if release.status in {
        DeploymentState.CREATED,
        DeploymentState.PREFLIGHT_PASSED,
        DeploymentState.SNAPSHOT_VERIFIED,
        DeploymentState.REHEARSAL_PASSED,
        DeploymentState.CANDIDATE_HEALTHY,
    }:
        return _manual(
            operation,
            release,
            "The operation stopped before the exact production application was durably stored.",
            production_changed=False,
        )

    if release.previous_application is None:
        return _manual(
            operation,
            release,
            "The exact previous application identity is missing.",
            production_changed=production_changed,
        )
    if live.previous_application in {
        ApplicationRuntimeState.ABSENT,
        ApplicationRuntimeState.UNKNOWN,
    }:
        return _manual(
            operation,
            release,
            "The exact previous application cannot be inspected safely.",
            production_changed=production_changed,
        )
    if live.new_application is ApplicationRuntimeState.UNKNOWN:
        return _manual(
            operation,
            release,
            "New-release application presence cannot be proven.",
            production_changed=production_changed,
        )

    actions: list[RecoveryAction] = []
    if live.new_application in {
        ApplicationRuntimeState.RUNNING,
        ApplicationRuntimeState.STOPPED,
    }:
        if release.new_application is None:
            return _manual(
                operation,
                release,
                "A new-release resource exists without a durable exact application handle.",
                production_changed=production_changed,
            )
        actions.append(RecoveryAction.REMOVE_NEW_APPLICATION)

    database_matches_final = (
        live.production_database_sha256 is not None
        and live.production_database_sha256 == live.final_backup_sha256
    )
    if production_changed:
        if release.final_backup_id is None or not live.final_backup_verified:
            return _manual(
                operation,
                release,
                "Production may have changed but the final backup is not verified.",
                production_changed=True,
            )
        if not database_matches_final:
            if live.previous_application is ApplicationRuntimeState.RUNNING:
                return _manual(
                    operation,
                    release,
                    "The previous application is running while the database needs restoration.",
                    production_changed=True,
                )
            actions.append(RecoveryAction.RESTORE_FINAL_BACKUP)

    if live.previous_application is ApplicationRuntimeState.STOPPED:
        actions.append(RecoveryAction.RESTART_PREVIOUS_APPLICATION)
    actions.extend(
        (
            RecoveryAction.ACTIVATE_PREVIOUS_TRAFFIC,
            RecoveryAction.DISABLE_MAINTENANCE,
            RecoveryAction.VERIFY_PREVIOUS,
            RecoveryAction.MARK_ROLLED_BACK,
        )
    )
    return RecoveryPlan(
        disposition=RecoveryDisposition.RECOVER_PREVIOUS,
        release_id=release.release_id,
        operation_id=operation.operation_id,
        durable_stage=operation.stage,
        production_may_have_changed=production_changed,
        traffic_may_have_switched=False,
        automatic_database_restore_allowed=True,
        actions=tuple(actions),
        reason="Durable evidence proves recovery to the previous release is safe.",
        next_safe_action="Execute the ordered recovery plan and verify every resulting state.",
        final_backup_id=release.final_backup_id,
    )


def _identity_problem(
    operation: OperationManifest,
    events: list[OperationEvent],
    release: ReleaseManifest,
) -> str | None:
    if operation.operation_type != "deploy":
        return "The unfinished operation is not a deployment."
    if operation.operation_id != release.operation_id:
        return "The release and operation identities do not match."
    if not events or events[-1].operation_id != operation.operation_id:
        return "The operation event trail is missing or belongs to another operation."
    if events[-1].sequence != operation.last_event_sequence:
        return "The operation event trail is incomplete."
    return None


def _activation_conclusion(
    release: ReleaseManifest,
    events: list[OperationEvent],
) -> str:
    if release.traffic_activated:
        return "succeeded"
    if not release.traffic_activation_attempted:
        return "not_attempted"
    terminal: dict[str, Any] | None = None
    for event in events:
        candidate = event.evidence.get("traffic_hook")
        if isinstance(candidate, dict) and candidate.get("action") == "activate_new":
            terminal = candidate
    if terminal is None:
        return "uncertain"
    command = terminal.get("command")
    if not isinstance(command, dict):
        return "uncertain"
    outcome = command.get("outcome")
    exit_code = command.get("exit_code")
    if terminal.get("passed") is True and outcome == "succeeded":
        return "succeeded"
    if outcome == "start_failed" or (outcome == "cancelled" and exit_code is None):
        return "not_started"
    return "uncertain"


def _manual(
    operation: OperationManifest,
    release: ReleaseManifest,
    reason: str,
    *,
    production_changed: bool,
    traffic_may_have_switched: bool = False,
) -> RecoveryPlan:
    return RecoveryPlan(
        disposition=RecoveryDisposition.MANUAL_REQUIRED,
        release_id=release.release_id,
        operation_id=operation.operation_id,
        durable_stage=operation.stage,
        production_may_have_changed=production_changed,
        traffic_may_have_switched=traffic_may_have_switched,
        automatic_database_restore_allowed=False,
        actions=(),
        reason=reason,
        next_safe_action=(
            "Preserve all evidence, determine the live application and traffic target, and do "
            "not restore the database automatically."
        ),
        final_backup_id=release.final_backup_id,
    )
