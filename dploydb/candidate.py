"""Durable rehearsal-plus-candidate validation with production left untouched."""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from dploydb.backup import create_verified_backup
from dploydb.config import LoadedConfiguration, configuration_fingerprint
from dploydb.errors import (
    DployDBError,
    ExternalCommandError,
    OperationFailedError,
    RecoveryRequiredError,
)
from dploydb.health import (
    CandidateHealthChecker,
    CandidateHealthResult,
    ReadinessCheckError,
    SmokeCheckError,
)
from dploydb.locking import DeploymentLock
from dploydb.migration import (
    MIGRATION_MAX_OUTPUT_BYTES,
    WORKSPACE_DIRECTORY_NAME,
    ActiveMigrationRehearsal,
    MigrationWorkspaceCleanupError,
    migration_rehearsal,
    require_clean_operation_state,
)
from dploydb.models import (
    BackupPurpose,
    FailureRecord,
    MigrationCommandEvidence,
    MigrationRehearsalResult,
    OperationStatus,
    SafetyFacts,
    serialize_utc_timestamp,
    utc_now,
)
from dploydb.redaction import JsonValue, SecretRegistry
from dploydb.runners.base import (
    ApplicationRunner,
    CandidateCleanup,
    CandidateCleanupError,
    CandidateInspection,
    CandidateInspectionError,
    CandidateLogs,
    CandidateRunnerError,
    CandidateStart,
    CandidateStartError,
    validate_release_identifier,
)
from dploydb.runners.docker_compose import DockerComposeCandidateRunner
from dploydb.sqlite_checks import verify_sqlite_database
from dploydb.state import StateStore
from dploydb.storage.local import LocalBackupStorage
from dploydb.subprocesses import CommandOutcome, CommandResult, SubprocessRunner


class HealthChecker(Protocol):
    """Small injectable health boundary consumed by the candidate coordinator."""

    def check(
        self,
        *,
        version: str,
        rehearsal_database_path: Path,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateHealthResult: ...


@dataclass(frozen=True, slots=True)
class CandidateValidationResult:
    """Complete passing evidence returned only after every cleanup proof passes."""

    operation_id: str
    version: str
    rehearsal: MigrationRehearsalResult
    health: CandidateHealthResult
    logs: CandidateLogs
    cleanup: CandidateCleanup
    completed_at: datetime

    def as_evidence(self) -> dict[str, JsonValue]:
        return {
            "operation_id": self.operation_id,
            "version": self.version,
            "rehearsal": self.rehearsal.model_dump(mode="json"),
            "health": self.health.as_evidence(),
            "logs": self.logs.command.as_evidence(),
            "cleanup": _cleanup_evidence(self.cleanup),
            "completed_at": serialize_utc_timestamp(self.completed_at),
        }


@dataclass(frozen=True, slots=True)
class _RuntimeResult:
    health: CandidateHealthResult
    logs: CandidateLogs
    cleanup: CandidateCleanup


def validate_configured_candidate(
    loaded: LoadedConfiguration,
    *,
    version: str,
    config_path: Path,
    command_environment: Mapping[str, str] | None = None,
    command_runner: SubprocessRunner | None = None,
    application_runner: ApplicationRunner | None = None,
    health_checker: HealthChecker | None = None,
    cancellation_event: threading.Event | None = None,
) -> CandidateValidationResult:
    """Rehearse and validate one candidate under a single durable operation."""
    release = validate_release_identifier(version)
    config = loaded.config
    secrets = loaded.secrets
    store = StateStore(config.state_directory, secrets=secrets)
    lock = DeploymentLock(config.state_directory, secrets=secrets)
    environment = dict(os.environ if command_environment is None else command_environment)
    working_directory = config_path.resolve().parent
    runner = command_runner or SubprocessRunner(
        secrets=secrets,
        max_output_bytes=MIGRATION_MAX_OUTPUT_BYTES,
    )
    owned_health: CandidateHealthChecker | None = None

    with lock:
        require_clean_operation_state(lock, store)
        operation = store.create_operation(
            operation_type="candidate_validation",
            project=config.project,
            configuration_fingerprint=configuration_fingerprint(config, secrets=secrets),
            evidence={
                "database_path": str(config.database.path),
                "requested_version": release,
            },
        )
        lock.record_owner(
            operation_id=operation.operation_id,
            operation_type="candidate_validation",
        )
        operation_log = store.operation_paths(operation.operation_id).events
        try:
            preflight = verify_sqlite_database(config.database.path)
            store.transition(
                operation.operation_id,
                status=OperationStatus.IN_PROGRESS,
                stage="preflight_passed",
                message="Production SQLite preflight passed without mutation.",
                evidence=preflight.model_dump(mode="json"),
            )
            snapshot = create_verified_backup(
                config.database.path,
                project=secrets.redact_text(config.project),
                purpose=BackupPurpose.REHEARSAL,
                storage=LocalBackupStorage(config.backup.local_directory),
                operation_id=operation.operation_id,
                metadata_source_path=_safe_metadata_path(config.database.path, loaded),
            )
            store.transition(
                operation.operation_id,
                status=OperationStatus.IN_PROGRESS,
                stage="snapshot_verified",
                message="Verified candidate rehearsal snapshot completed.",
                evidence={
                    "backup_id": snapshot.metadata.backup_id,
                    "backup_path": str(snapshot.database_path),
                    "sha256": snapshot.metadata.sha256,
                    "size_bytes": snapshot.metadata.size_bytes,
                },
            )

            def record_migration_command(evidence: MigrationCommandEvidence) -> None:
                store.append_event(
                    operation.operation_id,
                    message="Candidate migration command reached a terminal outcome.",
                    evidence={"migration_command": evidence.model_dump(mode="json")},
                )

            selected_runner = application_runner or DockerComposeCandidateRunner(
                project=config.project,
                application=config.application,
                database_environment_name=config.database.path_env,
                production_database_path=config.database.path,
                secrets=secrets,
                working_directory=working_directory,
                command_environment=environment,
                command_runner=runner,
            )
            selected_health = health_checker
            if selected_health is None:
                owned_health = CandidateHealthChecker(
                    application=config.application,
                    database_environment_name=config.database.path_env,
                    secrets=secrets,
                    working_directory=working_directory,
                    command_environment=environment,
                    command_runner=runner,
                )
                selected_health = owned_health

            with migration_rehearsal(
                snapshot,
                operation_id=operation.operation_id,
                command=config.migration.command,
                database_environment_name=config.database.path_env,
                timeout_seconds=config.migration.timeout_seconds,
                workspace_root=config.state_directory / WORKSPACE_DIRECTORY_NAME,
                working_directory=working_directory,
                environment=environment,
                runner=runner,
                command_evidence_sink=record_migration_command,
                cancellation_event=cancellation_event,
                log_path=operation_log,
            ) as active:
                store.transition(
                    operation.operation_id,
                    status=OperationStatus.IN_PROGRESS,
                    stage="rehearsal_passed",
                    message="Migration rehearsal passed; candidate validation may begin.",
                    evidence=_rehearsal_summary(active.result),
                )
                runtime = _validate_runtime(
                    runner=selected_runner,
                    health_checker=selected_health,
                    active=active,
                    operation_id=operation.operation_id,
                    version=release,
                    store=store,
                    operation_log=operation_log,
                    cancellation_event=cancellation_event,
                )
                if owned_health is not None:
                    owned_health.close()
                    owned_health = None

            result = CandidateValidationResult(
                operation_id=operation.operation_id,
                version=release,
                rehearsal=active.result,
                health=runtime.health,
                logs=runtime.logs,
                cleanup=runtime.cleanup,
                completed_at=utc_now(),
            )
            store.transition(
                operation.operation_id,
                status=OperationStatus.SUCCEEDED,
                stage="candidate_healthy",
                message=(
                    "Candidate checks and candidate/rehearsal cleanup completed successfully."
                ),
                evidence={
                    **_rehearsal_summary(result.rehearsal),
                    "requested_version": release,
                    "readiness_attempts": result.health.readiness.attempt_count,
                    "smoke_outcome": (
                        None if result.health.smoke is None else result.health.smoke.outcome.value
                    ),
                    "candidate_cleanup_proven": result.cleanup.proof.proven,
                    "rehearsal_workspace_cleaned": True,
                },
                safety=SafetyFacts(
                    production_changed=False,
                    previous_application_running=None,
                    recovery_required=False,
                ),
            )
            return result
        except MigrationWorkspaceCleanupError as error:
            failure = RecoveryRequiredError(
                error.payload.what_failed,
                production_changed=False,
                previous_application_running=None,
                log_path=operation_log,
                next_safe_action=(
                    "Production was not changed. Preserve the operation evidence and remove "
                    "only the recorded private rehearsal workspace before recovery."
                ),
            )
            _finish_failure(store, operation.operation_id, failure)
            raise failure from None
        except DployDBError as error:
            _finish_failure(store, operation.operation_id, error)
            raise
        except Exception as raw_error:
            unexpected_failure = OperationFailedError(
                "candidate validation failed safely: "
                + secrets.redact_text(f"{type(raw_error).__name__}: {raw_error}"),
                production_changed=False,
                previous_application_running=None,
                log_path=operation_log,
                next_safe_action=(
                    "Production was not changed; inspect the candidate event log, correct the "
                    "failure, and retry."
                ),
            )
            _finish_failure(store, operation.operation_id, unexpected_failure)
            raise unexpected_failure from None
        finally:
            if owned_health is not None:
                owned_health.close()


def _validate_runtime(
    *,
    runner: ApplicationRunner,
    health_checker: HealthChecker,
    active: ActiveMigrationRehearsal,
    operation_id: str,
    version: str,
    store: StateStore,
    operation_log: Path,
    cancellation_event: threading.Event | None,
) -> _RuntimeResult:
    started: CandidateStart | None = None
    inspection: CandidateInspection | None = None
    health: CandidateHealthResult | None = None
    logs: CandidateLogs | None = None
    cleanup: CandidateCleanup | None = None
    primary: BaseException | None = None
    logs_failure: DployDBError | None = None
    cleanup_failure: DployDBError | None = None

    try:
        started = runner.start(
            operation_id=operation_id,
            version=version,
            rehearsal_database_path=active.database_path,
            cancellation_event=cancellation_event,
        )
        store.append_event(
            operation_id,
            message="Candidate Compose startup completed.",
            evidence={"candidate_start": _start_evidence(started)},
        )
        inspection = runner.inspect(started.handle, cancellation_event=cancellation_event)
        store.append_event(
            operation_id,
            message="Candidate live isolation inspection passed.",
            evidence={"candidate_inspection": _inspection_evidence(inspection)},
        )
        health = health_checker.check(
            version=version,
            rehearsal_database_path=active.database_path,
            cancellation_event=cancellation_event,
        )
        store.append_event(
            operation_id,
            message="Candidate HTTP readiness and optional smoke checks passed.",
            evidence={"candidate_health": health.as_evidence()},
        )
    except BaseException as error:
        primary = error
        try:
            store.append_event(
                operation_id,
                message="Candidate startup, inspection, or health checks were rejected.",
                evidence={"candidate_failure": _primary_evidence(error)},
            )
        except BaseException as evidence_error:
            primary = evidence_error

    if started is not None:
        try:
            logs = runner.collect_logs(started.handle, cancellation_event=cancellation_event)
            store.append_event(
                operation_id,
                message="Bounded candidate application logs were collected.",
                evidence={"candidate_logs": logs.command.as_evidence()},
            )
            logs_failure = _logs_failure(logs.command, operation_log)
        except BaseException as error:
            if isinstance(error, DployDBError):
                logs_failure = error
            elif isinstance(error, Exception):
                logs_failure = OperationFailedError(
                    "candidate log collection failed: "
                    + store.secrets.redact_text(f"{type(error).__name__}: {error}"),
                    production_changed=False,
                    previous_application_running=None,
                    log_path=operation_log,
                    next_safe_action=(
                        "Production was not changed; inspect Docker directly and retry after "
                        "correcting log collection."
                    ),
                )
            else:
                primary = error

        try:
            cleanup = runner.stop(started.handle)
            store.append_event(
                operation_id,
                message="Candidate container and isolated network cleanup was proven.",
                evidence={"candidate_cleanup": _cleanup_evidence(cleanup)},
            )
        except CandidateCleanupError as error:
            if error.cleanup is not None:
                cleanup = error.cleanup
                try:
                    store.append_event(
                        operation_id,
                        message="Candidate cleanup command reported a failure.",
                        evidence={"candidate_cleanup": _cleanup_evidence(cleanup)},
                    )
                except BaseException as evidence_error:
                    cleanup_failure = _unknown_cleanup_failure(evidence_error, operation_log, store)
            if cleanup_failure is None:
                cleanup_failure = _candidate_cleanup_failure(error, operation_log)
        except BaseException as error:
            cleanup_failure = _unknown_cleanup_failure(error, operation_log, store)

    normalized_primary = _normalize_primary(
        primary,
        operation_log=operation_log,
        candidate_started=started is not None,
        secrets=store.secrets,
    )
    failures = [
        error for error in (normalized_primary, logs_failure, cleanup_failure) if error is not None
    ]
    if failures:
        if len(failures) == 1:
            raise failures[0]
        raise _combined_failure(failures, operation_log)
    if primary is not None:
        raise primary
    if started is None or inspection is None or health is None or logs is None or cleanup is None:
        raise RecoveryRequiredError(
            "candidate validation ended without a complete runtime evidence set",
            production_changed=False,
            previous_application_running=None,
            log_path=operation_log,
            next_safe_action="Preserve the event log and inspect candidate resources manually.",
        )
    return _RuntimeResult(health=health, logs=logs, cleanup=cleanup)


def _normalize_primary(
    error: BaseException | None,
    *,
    operation_log: Path,
    candidate_started: bool,
    secrets: SecretRegistry,
) -> DployDBError | None:
    if error is None:
        return None
    if isinstance(error, DployDBError):
        return error
    if isinstance(error, CandidateStartError):
        if error.cleanup_proven is not True:
            return RecoveryRequiredError(
                str(error),
                production_changed=False,
                previous_application_running=None,
                log_path=operation_log,
                next_safe_action=(
                    "Production was not changed. Confirm the operation-labeled candidate "
                    "container and network are gone before recovery."
                ),
            )
        return ExternalCommandError(
            str(error),
            production_changed=False,
            previous_application_running=None,
            log_path=operation_log,
            next_safe_action=(
                "Production was not changed and candidate cleanup was proven; inspect startup "
                "evidence, correct the release, and retry."
            ),
        )
    if isinstance(error, CandidateInspectionError):
        return RecoveryRequiredError(
            str(error),
            production_changed=False,
            previous_application_running=None,
            log_path=operation_log,
            next_safe_action=(
                "Production was not changed. Preserve the contradictory inspection evidence "
                "and confirm candidate isolation before recovery."
            ),
        )
    if isinstance(error, ReadinessCheckError):
        return OperationFailedError(
            error.evidence.reason,
            production_changed=False,
            previous_application_running=None,
            log_path=operation_log,
            next_safe_action=(
                "Production was not changed and candidate cleanup was proven; inspect readiness "
                "and application logs, correct the release, and retry."
            ),
        )
    if isinstance(error, SmokeCheckError):
        error_type = OperationFailedError if error.cleanup_proven else RecoveryRequiredError
        return error_type(
            str(error),
            production_changed=False,
            previous_application_running=None,
            log_path=operation_log,
            next_safe_action=(
                "Production was not changed. Inspect smoke and cleanup evidence before retrying."
            ),
        )
    if isinstance(error, CandidateRunnerError):
        return RecoveryRequiredError(
            str(error),
            production_changed=False,
            previous_application_running=None,
            log_path=operation_log,
            next_safe_action="Preserve the event log and inspect candidate resources manually.",
        )
    if not isinstance(error, Exception):
        return None
    detail = secrets.redact_text(f"{type(error).__name__}: {error}")
    error_type = OperationFailedError if candidate_started else RecoveryRequiredError
    return error_type(
        "unexpected candidate runtime failure: " + detail,
        production_changed=False,
        previous_application_running=None,
        log_path=operation_log,
        next_safe_action=(
            "Production was not changed; preserve the evidence and inspect candidate resources "
            "before retrying."
        ),
    )


def _logs_failure(result: CommandResult, operation_log: Path) -> DployDBError | None:
    if result.outcome is CommandOutcome.SUCCEEDED:
        return None
    if result.outcome is CommandOutcome.CLEANUP_FAILED:
        return RecoveryRequiredError(
            "candidate log-collection process cleanup could not be proven",
            production_changed=False,
            previous_application_running=None,
            log_path=operation_log,
            next_safe_action="Confirm the log process is gone before recovery.",
        )
    return OperationFailedError(
        f"candidate log collection ended with {result.outcome.value}",
        production_changed=False,
        previous_application_running=None,
        log_path=operation_log,
        next_safe_action="Inspect Docker logs directly, correct the cause, and retry.",
    )


def _candidate_cleanup_failure(
    error: CandidateCleanupError,
    operation_log: Path,
) -> DployDBError:
    error_type = OperationFailedError if error.cleanup_proven is True else RecoveryRequiredError
    return error_type(
        str(error),
        production_changed=False,
        previous_application_running=None,
        log_path=operation_log,
        next_safe_action=(
            "Production was not changed. Inspect the exact candidate container and isolated "
            "network evidence before retrying."
        ),
    )


def _unknown_cleanup_failure(
    error: BaseException,
    operation_log: Path,
    store: StateStore,
) -> DployDBError:
    if isinstance(error, DployDBError):
        return error
    return RecoveryRequiredError(
        "candidate cleanup could not be proven: "
        + store.secrets.redact_text(f"{type(error).__name__}: {error}"),
        production_changed=False,
        previous_application_running=None,
        log_path=operation_log,
        next_safe_action=(
            "Production was not changed. Confirm the operation-labeled candidate container and "
            "network are absent before recovery."
        ),
    )


def _combined_failure(errors: list[DployDBError], operation_log: Path) -> DployDBError:
    detail = "; ".join(error.payload.what_failed for error in errors)
    error_type = (
        RecoveryRequiredError
        if any(error.payload.recovery_required for error in errors)
        else OperationFailedError
    )
    return error_type(
        detail,
        production_changed=False,
        previous_application_running=None,
        log_path=operation_log,
        next_safe_action=(
            "Production was not changed. Resolve every recorded candidate, log, and cleanup "
            "failure before retrying."
        ),
    )


def _primary_evidence(error: BaseException) -> dict[str, JsonValue]:
    if isinstance(error, CandidateStartError):
        return {
            "kind": "candidate_start",
            "message": str(error),
            "command": None if error.command is None else error.command.as_evidence(),
            "cleanup": None if error.cleanup is None else _cleanup_evidence(error.cleanup),
        }
    if isinstance(error, CandidateInspectionError):
        return {
            "kind": "candidate_inspection",
            "message": str(error),
            "command": None if error.command is None else error.command.as_evidence(),
        }
    if isinstance(error, ReadinessCheckError):
        return {"kind": "readiness", "readiness": error.evidence.as_evidence()}
    if isinstance(error, SmokeCheckError):
        return {"kind": "smoke", **error.as_evidence()}
    if isinstance(error, DployDBError):
        return {
            "kind": "dploydb",
            "failure": cast(dict[str, JsonValue], error.payload.as_dict()),
        }
    return {"kind": type(error).__name__, "message": str(error)}


def _start_evidence(started: CandidateStart) -> dict[str, JsonValue]:
    handle = started.handle
    return {
        "operation_id": handle.operation_id,
        "version": handle.version,
        "compose_project": handle.compose_project,
        "container_name": handle.container_name,
        "rehearsal_database_path": str(handle.rehearsal_database_path),
        "candidate_database_path": handle.candidate_database_path,
        "container_reference": started.container_reference,
        "command": started.command.as_evidence(),
    }


def _inspection_evidence(inspection: CandidateInspection) -> dict[str, JsonValue]:
    return {
        "container_id": inspection.container_id,
        "container_name": inspection.container_name,
        "running": inspection.running,
        "compose_project": inspection.compose_project,
        "compose_service": inspection.compose_service,
        "operation_id": inspection.operation_id,
        "host_ip": inspection.host_ip,
        "host_port": inspection.host_port,
        "container_port": inspection.container_port,
        "mounts": [
            {
                "mount_type": mount.mount_type,
                "source": mount.source,
                "destination": mount.destination,
                "read_write": mount.read_write,
            }
            for mount in inspection.mounts
        ],
        "command": inspection.command.as_evidence(),
    }


def _cleanup_evidence(cleanup: CandidateCleanup) -> dict[str, JsonValue]:
    proof = cleanup.proof
    return {
        "presence_query": cleanup.presence_query.as_evidence(),
        "remove_command": (
            None if cleanup.remove_command is None else cleanup.remove_command.as_evidence()
        ),
        "compose_down": cleanup.compose_down.as_evidence(),
        "proof": {
            "container_absent": proof.container_absent,
            "networks_absent": proof.networks_absent,
            "proven": proof.proven,
            "container_query": proof.container_query.as_evidence(),
            "network_query": proof.network_query.as_evidence(),
        },
    }


def _rehearsal_summary(result: MigrationRehearsalResult) -> dict[str, JsonValue]:
    return {
        "backup_id": result.backup_id,
        "backup_sha256": result.backup_sha256,
        "database_size_bytes": result.database_size_bytes,
        "database_sha256": result.database_sha256,
        "migration_outcome": result.command.outcome,
        "migration_duration_seconds": result.command.duration_seconds,
        "sqlite": result.sqlite.model_dump(mode="json"),
    }


def _finish_failure(store: StateStore, operation_id: str, error: DployDBError) -> None:
    status = (
        OperationStatus.RECOVERY_REQUIRED
        if error.payload.recovery_required
        else OperationStatus.FAILED_SAFE
    )
    store.transition(
        operation_id,
        status=status,
        stage=status.value,
        message="Candidate validation did not complete.",
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


def _safe_metadata_path(path: Path, loaded: LoadedConfiguration) -> Path:
    safe = Path(loaded.secrets.redact_text(str(path)))
    return safe if safe.is_absolute() else Path("/[REDACTED]")
