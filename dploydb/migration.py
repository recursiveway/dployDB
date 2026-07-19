"""Migration rehearsal against a disposable copy of a verified SQLite snapshot."""

from __future__ import annotations

import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dploydb.backup import calculate_sha256, create_verified_backup
from dploydb.config import LoadedConfiguration, configuration_fingerprint
from dploydb.errors import (
    DployDBError,
    ExternalCommandError,
    OperationFailedError,
    RecoveryRequiredError,
    SafetyCheckError,
)
from dploydb.locking import DeploymentLock
from dploydb.models import (
    BackupArtifact,
    BackupPurpose,
    CapturedCommandOutput,
    FailureRecord,
    LockOwnerState,
    MigrationCommandEvidence,
    MigrationRehearsalResult,
    OperationStatus,
    SafetyFacts,
    utc_now,
)
from dploydb.sqlite_checks import verify_sqlite_database
from dploydb.state import DIRECTORY_MODE, StateStore
from dploydb.storage.local import LocalBackupStorage
from dploydb.subprocesses import CommandOutcome, CommandResult, SubprocessRunner

WORKSPACE_DIRECTORY_NAME: Final = "rehearsals"
WORKSPACE_DATABASE_NAME: Final = "rehearsal.db"
WORKSPACE_FILE_MODE: Final = 0o600
WORKSPACE_COPY_TIMEOUT_SECONDS: Final = 120.0
WORKSPACE_COPY_CHUNK_BYTES: Final = 1024 * 1024
MIGRATION_MAX_OUTPUT_BYTES: Final = 256 * 1024
_OPERATION_ID = re.compile(r"^op_[0-9a-f]{32}$")

CommandEvidenceSink = Callable[[MigrationCommandEvidence], None]


class MigrationWorkspaceCleanupError(OperationFailedError):
    """A private rehearsal workspace could not be proven clean."""


@dataclass(frozen=True, slots=True)
class ActiveMigrationRehearsal:
    """A verified migrated database that exists only inside its context."""

    database_path: Path
    result: MigrationRehearsalResult


def rehearse_configured_migration(
    loaded: LoadedConfiguration,
    *,
    config_path: Path,
    command_environment: Mapping[str, str] | None = None,
    command_runner: SubprocessRunner | None = None,
    cancellation_event: threading.Event | None = None,
) -> MigrationRehearsalResult:
    """Run one exclusive, durable rehearsal without modifying production."""
    config = loaded.config
    secrets = loaded.secrets
    store = StateStore(config.state_directory, secrets=secrets)
    lock = DeploymentLock(config.state_directory, secrets=secrets)

    with lock:
        require_clean_operation_state(lock, store)
        operation = store.create_operation(
            operation_type="rehearsal",
            project=config.project,
            configuration_fingerprint=configuration_fingerprint(config, secrets=secrets),
            evidence={"database_path": str(config.database.path)},
        )
        lock.record_owner(operation_id=operation.operation_id, operation_type="rehearsal")
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
                message="Verified rehearsal snapshot completed before migration execution.",
                evidence={
                    "backup_id": snapshot.metadata.backup_id,
                    "backup_path": str(snapshot.database_path),
                    "sha256": snapshot.metadata.sha256,
                    "size_bytes": snapshot.metadata.size_bytes,
                },
            )

            def record_command(evidence: MigrationCommandEvidence) -> None:
                store.append_event(
                    operation.operation_id,
                    message="Migration command reached a terminal outcome.",
                    evidence={"migration_command": evidence.model_dump(mode="json")},
                )

            runner = command_runner or SubprocessRunner(
                secrets=secrets,
                max_output_bytes=MIGRATION_MAX_OUTPUT_BYTES,
            )
            environment = dict(os.environ if command_environment is None else command_environment)
            working_directory = config_path.resolve().parent
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
                command_evidence_sink=record_command,
                cancellation_event=cancellation_event,
                log_path=operation_log,
            ) as active:
                result = active.result

            store.transition(
                operation.operation_id,
                status=OperationStatus.SUCCEEDED,
                stage="rehearsal_passed",
                message="Migration rehearsal and disposable-workspace cleanup passed.",
                evidence={
                    "backup_id": result.backup_id,
                    "backup_sha256": result.backup_sha256,
                    "database_size_bytes": result.database_size_bytes,
                    "database_sha256": result.database_sha256,
                    "migration_outcome": result.command.outcome,
                    "migration_duration_seconds": result.command.duration_seconds,
                    "sqlite": result.sqlite.model_dump(mode="json"),
                },
                safety=SafetyFacts(
                    production_changed=False,
                    previous_application_running=None,
                    recovery_required=False,
                ),
            )
            return result
        except DployDBError as error:
            _finish_failure(store, operation.operation_id, error)
            raise
        except Exception as raw_error:
            failure = OperationFailedError(
                f"migration rehearsal failed safely: {secrets.redact_text(str(raw_error))}",
                production_changed=False,
                previous_application_running=None,
                log_path=operation_log,
                next_safe_action=(
                    "Production was not changed; inspect the rehearsal log, correct the "
                    "failure, and retry."
                ),
            )
            _finish_failure(store, operation.operation_id, failure)
            raise failure from None


@contextmanager
def migration_rehearsal(
    snapshot: BackupArtifact,
    *,
    operation_id: str,
    command: tuple[str, ...],
    database_environment_name: str,
    timeout_seconds: float,
    workspace_root: Path,
    working_directory: Path,
    environment: Mapping[str, str],
    runner: SubprocessRunner,
    command_evidence_sink: CommandEvidenceSink,
    cancellation_event: threading.Event | None,
    log_path: Path,
) -> Iterator[ActiveMigrationRehearsal]:
    """Yield a checked migrated copy, then remove its workspace idempotently."""
    workspace: Path | None = None
    primary_error: BaseException | None = None
    try:
        _verify_snapshot(snapshot)
        workspace = _create_workspace(workspace_root, operation_id)
        database_path = _materialize_snapshot(snapshot, workspace)
        child_environment = dict(environment)
        child_environment[database_environment_name] = str(database_path)
        command_result = runner.run(
            command,
            timeout_seconds=timeout_seconds,
            environment=child_environment,
            working_directory=working_directory,
            cancellation_event=cancellation_event,
        )
        command_evidence = migration_command_evidence(command_result)
        command_evidence_sink(command_evidence)
        _require_usable_command_result(command_result, log_path)
        try:
            sqlite_evidence = verify_sqlite_database(database_path)
        except DployDBError as error:
            raise OperationFailedError(
                "post-migration SQLite verification failed: " + error.payload.what_failed,
                production_changed=False,
                previous_application_running=None,
                log_path=log_path,
                next_safe_action=(
                    "Production was not changed; correct the migration so the rehearsed "
                    "database passes every SQLite check."
                ),
            ) from None
        size_bytes, sha256 = calculate_sha256(database_path)
        result = MigrationRehearsalResult(
            operation_id=operation_id,
            backup_id=snapshot.metadata.backup_id,
            backup_sha256=snapshot.metadata.sha256,
            database_size_bytes=size_bytes,
            database_sha256=sha256,
            command=command_evidence,
            sqlite=sqlite_evidence,
            completed_at=utc_now(),
        )
        yield ActiveMigrationRehearsal(database_path=database_path, result=result)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error = _cleanup_workspace(workspace)
        if cleanup_error is not None:
            if primary_error is None:
                raise _workspace_cleanup_error(cleanup_error, log_path) from None
            if isinstance(primary_error, RecoveryRequiredError):
                raise RecoveryRequiredError(
                    primary_error.payload.what_failed
                    + f"; rehearsal workspace cleanup also failed: {cleanup_error}",
                    production_changed=False,
                    previous_application_running=None,
                    log_path=log_path,
                    next_safe_action=(
                        "Preserve the operation evidence, confirm the migration process group "
                        "is gone, and clean the private rehearsal workspace before recovery."
                    ),
                ) from None
            if isinstance(primary_error, Exception):
                detail = (
                    primary_error.payload.what_failed
                    if isinstance(primary_error, DployDBError)
                    else str(primary_error)
                )
                raise MigrationWorkspaceCleanupError(
                    detail + f"; rehearsal workspace cleanup also failed: {cleanup_error}",
                    production_changed=False,
                    previous_application_running=None,
                    log_path=log_path,
                    next_safe_action=(
                        "Production was not changed; inspect the log and remove only the "
                        "recorded private rehearsal workspace before retrying."
                    ),
                ) from None
            primary_error.add_note(f"Rehearsal workspace cleanup also failed: {cleanup_error}")


def migration_command_evidence(result: CommandResult) -> MigrationCommandEvidence:
    """Convert one bounded subprocess result into durable migration evidence."""
    return MigrationCommandEvidence(
        command=result.command,
        working_directory=result.working_directory,
        environment_keys=result.environment_keys,
        outcome=result.outcome.value,
        exit_code=result.exit_code,
        stdout=CapturedCommandOutput(
            text=result.stdout.text,
            total_bytes=result.stdout.total_bytes,
            retained_bytes=result.stdout.retained_bytes,
            truncated=result.stdout.truncated,
        ),
        stderr=CapturedCommandOutput(
            text=result.stderr.text,
            total_bytes=result.stderr.total_bytes,
            retained_bytes=result.stderr.retained_bytes,
            truncated=result.stderr.truncated,
        ),
        duration_seconds=result.duration_seconds,
        termination_reason=(
            None if result.termination_reason is None else result.termination_reason.value
        ),
        termination_attempted=result.termination_attempted,
        forced_kill=result.forced_kill,
        start_error=result.start_error,
        cleanup_error=result.cleanup_error,
    )


def _require_usable_command_result(result: CommandResult, log_path: Path) -> None:
    if result.stdout.truncated or result.stderr.truncated:
        raise OperationFailedError(
            "migration output exceeded the complete-capture safety bound",
            production_changed=False,
            previous_application_running=None,
            log_path=log_path,
            next_safe_action=(
                "Production was not changed; reduce migration output and retry so complete "
                "stdout and stderr can be preserved."
            ),
        )
    if result.outcome is CommandOutcome.SUCCEEDED:
        return
    if result.outcome is CommandOutcome.CLEANUP_FAILED:
        raise RecoveryRequiredError(
            "migration process cleanup could not be proven: "
            + (result.cleanup_error or "unknown cleanup failure"),
            production_changed=False,
            previous_application_running=None,
            log_path=log_path,
            next_safe_action=(
                "Production was not changed. Preserve the evidence and confirm the complete "
                "migration process group is stopped before running recovery."
            ),
        )
    if result.outcome is CommandOutcome.NONZERO_EXIT:
        detail = f"migration command exited with status {result.exit_code}"
    elif result.outcome is CommandOutcome.TIMED_OUT:
        detail = "migration command timed out and its process group was terminated"
    elif result.outcome is CommandOutcome.CANCELLED:
        detail = "migration command was cancelled and its process group was terminated"
    else:
        detail = "migration command could not start: " + (result.start_error or "unknown error")
    raise ExternalCommandError(
        detail,
        production_changed=False,
        previous_application_running=None,
        log_path=log_path,
        next_safe_action=(
            "Production was not changed; inspect the captured migration output, correct the "
            "command, and retry."
        ),
    )


def _verify_snapshot(snapshot: BackupArtifact) -> None:
    before = calculate_sha256(snapshot.database_path)
    if before != (snapshot.metadata.size_bytes, snapshot.metadata.sha256):
        raise SafetyCheckError(
            "rehearsal snapshot no longer matches its verified metadata",
            production_changed=False,
            previous_application_running=None,
            log_path=snapshot.database_path,
            next_safe_action="Create a new verified rehearsal snapshot before retrying.",
        )
    verify_sqlite_database(snapshot.database_path)
    after = calculate_sha256(snapshot.database_path)
    if after != before:
        raise SafetyCheckError(
            "rehearsal snapshot changed while it was reverified",
            production_changed=False,
            previous_application_running=None,
            log_path=snapshot.database_path,
            next_safe_action="Create a new verified rehearsal snapshot before retrying.",
        )


def _create_workspace(root: Path, operation_id: str) -> Path:
    if not root.is_absolute() or root == Path(root.anchor):
        raise ValueError("rehearsal workspace root must be an absolute non-root path")
    if _OPERATION_ID.fullmatch(operation_id) is None:
        raise ValueError("operation ID is invalid or unsafe for a rehearsal workspace")
    if root.is_symlink():
        raise OSError("rehearsal workspace root must not be a symlink")
    root.mkdir(mode=DIRECTORY_MODE, parents=False, exist_ok=True)
    details = root.stat()
    if not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) != DIRECTORY_MODE:
        raise OSError("rehearsal workspace root must be a mode-0700 directory")
    workspace = root / operation_id
    workspace_created = False
    try:
        workspace.mkdir(mode=DIRECTORY_MODE, exist_ok=False)
        workspace_created = True
        _fsync_directory(root)
    except OSError:
        if workspace_created:
            try:
                workspace.rmdir()
            except OSError:
                pass
        raise
    return workspace


def _materialize_snapshot(snapshot: BackupArtifact, workspace: Path) -> Path:
    target = workspace / WORKSPACE_DATABASE_NAME
    source_descriptor = -1
    target_descriptor = -1
    deadline = time.monotonic() + WORKSPACE_COPY_TIMEOUT_SECONDS
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    target_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    target_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(snapshot.database_path, source_flags)
        source_details = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_details.st_mode):
            raise OSError("verified snapshot is not a regular file")
        target_descriptor = os.open(target, target_flags, WORKSPACE_FILE_MODE)
        os.fchmod(target_descriptor, WORKSPACE_FILE_MODE)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"rehearsal copy timed out after {WORKSPACE_COPY_TIMEOUT_SECONDS:g} seconds"
                )
            chunk = os.read(source_descriptor, WORKSPACE_COPY_CHUNK_BYTES)
            if not chunk:
                break
            _write_all(target_descriptor, chunk)
        os.fsync(target_descriptor)
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
    _fsync_directory(workspace)
    size_bytes, sha256 = calculate_sha256(target)
    if size_bytes != snapshot.metadata.size_bytes or sha256 != snapshot.metadata.sha256:
        raise SafetyCheckError(
            "disposable rehearsal database does not match the verified snapshot",
            production_changed=False,
            previous_application_running=None,
            log_path=target,
            next_safe_action="Discard the rehearsal workspace and create a new verified snapshot.",
        )
    verify_sqlite_database(target)
    return target


def _cleanup_workspace(workspace: Path | None) -> str | None:
    if workspace is None:
        return None
    try:
        if not workspace.exists():
            return None
        if workspace.is_symlink():
            raise OSError("refusing a symlinked rehearsal workspace")
        for name in (
            f"{WORKSPACE_DATABASE_NAME}-wal",
            f"{WORKSPACE_DATABASE_NAME}-shm",
            f"{WORKSPACE_DATABASE_NAME}-journal",
            WORKSPACE_DATABASE_NAME,
        ):
            path = workspace / name
            if not path.exists() and not path.is_symlink():
                continue
            details = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(details.st_mode):
                raise OSError(f"refusing unsafe rehearsal artifact: {path.name}")
            path.unlink()
        unexpected = list(workspace.iterdir())
        if unexpected:
            raise OSError(f"rehearsal workspace contains unexpected entry: {unexpected[0].name}")
        workspace.rmdir()
        _fsync_directory(workspace.parent)
    except OSError as error:
        return str(error)
    return None


def _workspace_cleanup_error(detail: str, log_path: Path) -> MigrationWorkspaceCleanupError:
    return MigrationWorkspaceCleanupError(
        f"rehearsal workspace cleanup failed: {detail}",
        production_changed=False,
        previous_application_running=None,
        log_path=log_path,
        next_safe_action=(
            "Production was not changed; inspect the log and remove only the recorded private "
            "rehearsal workspace before retrying."
        ),
    )


def require_clean_operation_state(lock: DeploymentLock, store: StateStore) -> None:
    """Refuse new work while durable evidence requires diagnosis or recovery."""
    if lock.previous_owner is not None and lock.previous_owner.state is LockOwnerState.ACTIVE:
        raise RecoveryRequiredError(
            "A prior operation left active lock-owner evidence.",
            production_changed=False,
            previous_application_running=None,
            log_path=lock.owner_path,
            next_safe_action="Run dploydb status and preserve the interrupted evidence.",
        )
    latest = store.latest_operation()
    if latest is not None and latest.status in {
        OperationStatus.IN_PROGRESS,
        OperationStatus.RECOVERY_REQUIRED,
    }:
        raise RecoveryRequiredError(
            "An unfinished or recovery-required operation blocks migration rehearsal.",
            production_changed=latest.safety.production_changed,
            previous_application_running=latest.safety.previous_application_running,
            log_path=store.operation_paths(latest.operation_id).events,
            next_safe_action="Run dploydb status and resolve the recorded operation first.",
        )


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
        message="Migration rehearsal did not complete.",
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


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError("rehearsal copy made no progress")
        written += count


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
