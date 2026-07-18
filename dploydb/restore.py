"""Internal stopped-application restore with verified pre-restore recovery."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final
from uuid import uuid4

from dploydb.backup import (
    calculate_sha256,
    create_verified_backup,
    verify_configured_backup,
)
from dploydb.config import LoadedConfiguration, configuration_fingerprint
from dploydb.errors import (
    DployDBError,
    OperationFailedError,
    RecoveryRequiredError,
    SafetyCheckError,
)
from dploydb.locking import DeploymentLock
from dploydb.models import (
    BackupArtifact,
    BackupPurpose,
    FailureRecord,
    LockOwnerState,
    OperationStatus,
    RestoreResult,
    SafetyFacts,
    utc_now,
)
from dploydb.sqlite_checks import verify_sqlite_database
from dploydb.state import StateStore
from dploydb.storage.local import LocalBackupStorage

RESTORE_TIMEOUT_SECONDS: Final[float] = 120.0
COPY_CHUNK_BYTES: Final[int] = 1024 * 1024
FILE_MODE: Final[int] = 0o600
FaultInjector = Callable[[str], None]


def restore_stopped_database(
    loaded: LoadedConfiguration,
    backup_id: str,
    *,
    application_stopped: bool,
    fault_injector: FaultInjector | None = None,
) -> RestoreResult:
    """Restore a backup only after the caller has stopped every database user."""
    if not application_stopped:
        raise SafetyCheckError(
            "restore requires the application and all database users to be stopped",
            production_changed=False,
            previous_application_running=None,
            log_path=loaded.config.database.path,
            next_safe_action="Stop every database user, then invoke the controlled restore flow.",
        )

    config = loaded.config
    secrets = loaded.secrets
    target = config.database.path
    storage = LocalBackupStorage(config.backup.local_directory)
    store = StateStore(config.state_directory, secrets=secrets)
    lock = DeploymentLock(config.state_directory, secrets=secrets)
    inject = fault_injector or _no_fault

    with lock:
        _require_clean_operation_state(lock, store)
        operation = store.create_operation(
            operation_type="restore",
            project=config.project,
            configuration_fingerprint=configuration_fingerprint(config, secrets=secrets),
            evidence={"selected_backup_id": backup_id, "database_path": str(target)},
        )
        lock.record_owner(operation_id=operation.operation_id, operation_type="restore")

        staged: Path | None = None
        pre_restore: BackupArtifact | None = None
        production_mutation_started = False
        try:
            selected = verify_configured_backup(loaded, backup_id)
            pre_restore = create_verified_backup(
                target,
                project=secrets.redact_text(config.project),
                purpose=BackupPurpose.PRE_RESTORE,
                storage=storage,
                operation_id=operation.operation_id,
                metadata_source_path=_safe_metadata_path(target, loaded),
            )
            staged = _materialize_backup(selected, target.parent)
            _verify_materialized(staged, selected)
            inject("after_staging")
            store.transition(
                operation.operation_id,
                status=OperationStatus.IN_PROGRESS,
                stage="restore_prepared",
                message="Selected and pre-restore backups are verified.",
                evidence={
                    "selected_backup_id": selected.metadata.backup_id,
                    "pre_restore_backup_id": pre_restore.metadata.backup_id,
                },
            )
            store.transition(
                operation.operation_id,
                status=OperationStatus.IN_PROGRESS,
                stage="manual_restore_started",
                message="Production database replacement is starting with all users stopped.",
                safety=SafetyFacts(
                    production_changed=True,
                    previous_application_running=False,
                    recovery_required=False,
                ),
            )
            production_mutation_started = True
            _remove_sqlite_sidecars(target)
            os.replace(staged, target)
            staged = None
            os.chmod(target, FILE_MODE, follow_symlinks=False)
            _fsync_directory(target.parent)
            inject("after_replace")
            _verify_materialized(target, selected)
            selected_size, selected_sha256 = calculate_sha256(target)
            if selected_size <= 0:
                raise AssertionError("a verified restored database cannot be empty")
            result = RestoreResult(
                selected_backup_id=backup_id,
                pre_restore_backup_id=pre_restore.metadata.backup_id,
                database_path=target,
                sha256=selected_sha256,
                restored_at=utc_now(),
            )
            store.transition(
                operation.operation_id,
                status=OperationStatus.SUCCEEDED,
                stage="manual_restore_completed",
                message="Selected backup restored and verified with the application stopped.",
                evidence=result.model_dump(mode="json"),
                safety=SafetyFacts(
                    production_changed=True,
                    previous_application_running=False,
                    recovery_required=False,
                ),
            )
            return result
        except Exception as raw_error:
            _cleanup_temporary(staged, raw_error)
            error = _normalize_restore_error(raw_error, target)
            if production_mutation_started and pre_restore is not None:
                try:
                    _restore_previous_database(
                        target,
                        pre_restore,
                        fault_injector=inject,
                    )
                except Exception as rollback_error:
                    recovery = RecoveryRequiredError(
                        "Restore failed and the pre-restore database could not be proven restored: "
                        f"{rollback_error}",
                        production_changed=True,
                        previous_application_running=False,
                        log_path=store.operation_paths(operation.operation_id).events,
                        next_safe_action=(
                            "Keep the application stopped. Preserve all backup and operation "
                            "evidence, then restore the recorded pre-restore backup manually."
                        ),
                    )
                    _finish_failure(store, operation.operation_id, recovery)
                    raise recovery from None
                error = OperationFailedError(
                    f"Restore did not complete; the previous database was restored and verified: "
                    f"{error.payload.what_failed}",
                    production_changed=True,
                    previous_application_running=False,
                    log_path=store.operation_paths(operation.operation_id).events,
                    next_safe_action=(
                        "Keep the application stopped, inspect the restore evidence, and retry "
                        "only after correcting the original failure."
                    ),
                )
            _finish_failure(store, operation.operation_id, error)
            raise error from None


def _safe_metadata_path(path: Path, loaded: LoadedConfiguration) -> Path:
    safe = Path(loaded.secrets.redact_text(str(path)))
    return safe if safe.is_absolute() else Path("/[REDACTED]")


def _require_clean_operation_state(lock: DeploymentLock, store: StateStore) -> None:
    if lock.previous_owner is not None and lock.previous_owner.state is LockOwnerState.ACTIVE:
        raise RecoveryRequiredError(
            "A prior operation left active lock-owner evidence.",
            production_changed=False,
            previous_application_running=False,
            log_path=lock.owner_path,
            next_safe_action="Run dploydb status and preserve the interrupted evidence.",
        )
    latest = store.latest_operation()
    if latest is not None and latest.status in {
        OperationStatus.IN_PROGRESS,
        OperationStatus.RECOVERY_REQUIRED,
    }:
        raise RecoveryRequiredError(
            "An unfinished or recovery-required operation blocks restore.",
            production_changed=latest.safety.production_changed,
            previous_application_running=latest.safety.previous_application_running,
            log_path=store.operation_paths(latest.operation_id).events,
            next_safe_action="Run dploydb status and resolve the recorded operation first.",
        )


def _materialize_backup(artifact: BackupArtifact, destination: Path) -> Path:
    if destination.is_symlink():
        raise OSError("database directory must not be a symlink")
    details = destination.stat()
    if not stat.S_ISDIR(details.st_mode):
        raise OSError("database directory is not a directory")
    path = destination / f".dploydb-restore-{uuid4().hex}.tmp"
    source_descriptor = -1
    destination_descriptor = -1
    deadline = time.monotonic() + RESTORE_TIMEOUT_SECONDS
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(
            artifact.database_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        destination_descriptor = os.open(path, flags, FILE_MODE)
        os.fchmod(destination_descriptor, FILE_MODE)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"restore staging timed out after {RESTORE_TIMEOUT_SECONDS:g} seconds"
                )
            chunk = os.read(source_descriptor, COPY_CHUNK_BYTES)
            if not chunk:
                break
            _write_all(destination_descriptor, chunk)
        os.fsync(destination_descriptor)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
    _fsync_directory(destination)
    return path


def _verify_materialized(path: Path, artifact: BackupArtifact) -> None:
    size_bytes, sha256 = calculate_sha256(path)
    if size_bytes != artifact.metadata.size_bytes or sha256 != artifact.metadata.sha256:
        raise SafetyCheckError(
            "materialized restore file does not match selected backup metadata",
            production_changed=False,
            previous_application_running=False,
            log_path=path,
            next_safe_action="Do not activate this restore file; preserve the verified backup.",
        )
    verify_sqlite_database(path)


def _restore_previous_database(
    target: Path,
    pre_restore: BackupArtifact,
    *,
    fault_injector: FaultInjector,
) -> None:
    rollback_staged: Path | None = _materialize_backup(pre_restore, target.parent)
    try:
        assert rollback_staged is not None
        _verify_materialized(rollback_staged, pre_restore)
        fault_injector("rollback_before_replace")
        _remove_sqlite_sidecars(target)
        os.replace(rollback_staged, target)
        rollback_staged = None
        os.chmod(target, FILE_MODE, follow_symlinks=False)
        _fsync_directory(target.parent)
        _verify_materialized(target, pre_restore)
    finally:
        if rollback_staged is not None:
            rollback_staged.unlink(missing_ok=True)


def _remove_sqlite_sidecars(target: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target}{suffix}")
        if not sidecar.exists() and not sidecar.is_symlink():
            continue
        details = sidecar.lstat()
        if sidecar.is_symlink() or not stat.S_ISREG(details.st_mode):
            raise OSError(f"refusing unsafe SQLite sidecar: {sidecar}")
        sidecar.unlink()
    _fsync_directory(target.parent)


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
        message="Restore operation did not complete.",
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


def _normalize_restore_error(error: Exception, target: Path) -> DployDBError:
    if isinstance(error, DployDBError):
        return error
    return OperationFailedError(
        f"stopped-application restore failed: {error}",
        production_changed=False,
        previous_application_running=False,
        log_path=target,
        next_safe_action="Production was not replaced; correct the restore failure and retry.",
    )


def _cleanup_temporary(path: Path | None, error: BaseException) -> None:
    if path is None or not path.exists():
        return
    try:
        path.unlink()
    except OSError as cleanup_error:
        error.add_note(f"Restore staging cleanup also failed: {cleanup_error}")


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError("restore copy made no progress")
        written += count


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _no_fault(_stage: str) -> None:
    return
