"""Consistent SQLite online snapshots and immutable verification."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Final

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
    BackupMetadata,
    BackupPurpose,
    FailureRecord,
    LockOwnerState,
    OperationStatus,
    SafetyFacts,
    new_backup_id,
    utc_now,
)
from dploydb.sqlite_checks import DEFAULT_SQLITE_TIMEOUT_SECONDS, verify_sqlite_database
from dploydb.state import StateStore
from dploydb.storage.base import BackupStorage
from dploydb.storage.local import LocalBackupStorage

BACKUP_TIMEOUT_SECONDS: Final[float] = 120.0
COPY_PAGES: Final[int] = 256
HASH_CHUNK_BYTES: Final[int] = 1024 * 1024


def create_configured_backup(loaded: LoadedConfiguration) -> BackupArtifact:
    """Run one exclusive, durable standalone backup operation."""
    config = loaded.config
    secrets = loaded.secrets
    store = StateStore(config.state_directory, secrets=secrets)
    lock = DeploymentLock(config.state_directory, secrets=secrets)
    with lock:
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
                "An unfinished or recovery-required operation blocks backup.",
                production_changed=latest.safety.production_changed,
                previous_application_running=latest.safety.previous_application_running,
                log_path=store.operation_paths(latest.operation_id).events,
                next_safe_action="Run dploydb status and resolve the recorded operation first.",
            )

        operation = store.create_operation(
            operation_type="backup",
            project=config.project,
            configuration_fingerprint=configuration_fingerprint(config, secrets=secrets),
            evidence={"database_path": str(config.database.path)},
        )
        lock.record_owner(operation_id=operation.operation_id, operation_type="backup")
        try:
            preflight = verify_sqlite_database(config.database.path)
            store.transition(
                operation.operation_id,
                status=OperationStatus.IN_PROGRESS,
                stage="preflight_passed",
                message="SQLite backup preflight passed.",
                evidence=preflight.model_dump(mode="json"),
            )
            artifact = create_verified_backup(
                config.database.path,
                project=secrets.redact_text(config.project),
                purpose=BackupPurpose.STANDALONE,
                storage=LocalBackupStorage(config.backup.local_directory),
                operation_id=operation.operation_id,
                metadata_source_path=_safe_metadata_path(config.database.path, loaded),
            )
            store.transition(
                operation.operation_id,
                status=OperationStatus.SUCCEEDED,
                stage="snapshot_verified",
                message="Verified local SQLite backup completed.",
                evidence={
                    "backup_id": artifact.metadata.backup_id,
                    "backup_path": str(artifact.database_path),
                    "sha256": artifact.metadata.sha256,
                    "size_bytes": artifact.metadata.size_bytes,
                },
            )
            return artifact
        except DployDBError as error:
            status = (
                OperationStatus.RECOVERY_REQUIRED
                if error.payload.recovery_required
                else OperationStatus.FAILED_SAFE
            )
            store.transition(
                operation.operation_id,
                status=status,
                stage=(
                    "recovery_required"
                    if status is OperationStatus.RECOVERY_REQUIRED
                    else "failed_safe"
                ),
                message="Backup operation did not complete.",
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
            raise


def verify_configured_backup(
    loaded: LoadedConfiguration,
    backup_id: str,
) -> BackupArtifact:
    """Read-only verification constrained to the configured project and storage."""
    storage = LocalBackupStorage(loaded.config.backup.local_directory)
    artifact = verify_backup(storage, backup_id)
    if artifact.metadata.project != loaded.secrets.redact_text(loaded.config.project):
        raise _verification_error(
            artifact.metadata_path,
            "backup project does not match the configured project",
        )
    return artifact


def _safe_metadata_path(path: Path, loaded: LoadedConfiguration) -> Path:
    safe = Path(loaded.secrets.redact_text(str(path)))
    return safe if safe.is_absolute() else Path("/[REDACTED]")


def create_verified_backup(
    source: Path,
    *,
    project: str,
    purpose: BackupPurpose,
    storage: BackupStorage,
    operation_id: str | None = None,
    metadata_source_path: Path | None = None,
    timeout_seconds: float = BACKUP_TIMEOUT_SECONDS,
) -> BackupArtifact:
    """Create, verify, checksum, and commit one live SQLite snapshot."""
    if timeout_seconds <= 0:
        raise ValueError("backup timeout must be positive")
    created_at = utc_now()
    backup_id = new_backup_id()
    staged = storage.create_staging_database(backup_id)
    try:
        verify_sqlite_database(source, timeout_seconds=min(timeout_seconds, 10.0))
        _online_snapshot(source, staged, timeout_seconds=timeout_seconds)
        _fsync_file(staged)
        sqlite_evidence = verify_sqlite_database(
            staged,
            timeout_seconds=min(timeout_seconds, DEFAULT_SQLITE_TIMEOUT_SECONDS),
        )
        size_bytes, sha256 = calculate_sha256(staged)
        metadata = BackupMetadata(
            backup_id=backup_id,
            project=project,
            purpose=purpose,
            source_database_path=metadata_source_path or source,
            database_file_name=f"{backup_id}.db",
            size_bytes=size_bytes,
            sha256=sha256,
            sqlite=sqlite_evidence,
            operation_id=operation_id,
            created_at=created_at,
            completed_at=utc_now(),
        )
        storage.put(staged, metadata)
        return verify_backup(storage, backup_id)
    except DployDBError as exc:
        _cleanup_staging(staged, exc)
        raise
    except (OSError, sqlite3.Error, TimeoutError) as exc:
        error = _backup_error(source, f"verified SQLite backup could not be created: {exc}")
        _cleanup_staging(staged, error)
        raise error from None


def verify_backup(storage: BackupStorage, backup_id: str) -> BackupArtifact:
    """Revalidate committed metadata, bytes, checksum, and SQLite contents."""
    artifact = storage.get(backup_id)
    size_bytes, sha256 = calculate_sha256(artifact.database_path)
    if size_bytes != artifact.metadata.size_bytes:
        raise _verification_error(
            artifact.database_path,
            f"backup size mismatch: expected {artifact.metadata.size_bytes}, found {size_bytes}",
        )
    if sha256 != artifact.metadata.sha256:
        raise _verification_error(artifact.database_path, "backup SHA-256 checksum mismatch")
    verify_sqlite_database(artifact.database_path)
    final_size, final_sha256 = calculate_sha256(artifact.database_path)
    if final_size != size_bytes or final_sha256 != sha256:
        raise _verification_error(
            artifact.database_path,
            "backup changed while its SQLite contents were verified",
        )
    return artifact


def calculate_sha256(path: Path) -> tuple[int, str]:
    """Hash one stable regular non-symlink file and reject concurrent mutation."""
    deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("checksum target is not a regular file")
        digest = hashlib.sha256()
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise OSError(f"checksum timed out after {BACKUP_TIMEOUT_SECONDS:g} seconds")
            chunk = os.read(descriptor, HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise _verification_error(path, f"backup checksum could not be calculated: {exc}") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or size != after.st_size:
        raise _verification_error(path, "backup changed while its checksum was calculated")
    return size, digest.hexdigest()


def _online_snapshot(source: Path, destination: Path, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"online backup timed out after {timeout_seconds:g} seconds")

    try:
        source_connection = sqlite3.connect(
            f"{source.as_uri()}?mode=ro",
            uri=True,
            timeout=timeout_seconds,
            isolation_level=None,
        )
        destination_connection = sqlite3.connect(
            destination,
            timeout=timeout_seconds,
            isolation_level=None,
        )
        source_connection.backup(
            destination_connection,
            pages=COPY_PAGES,
            progress=progress,
            sleep=0.05,
        )
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _cleanup_staging(path: Path, error: BaseException) -> None:
    if not path.exists():
        return
    try:
        path.unlink()
    except OSError as cleanup_error:
        error.add_note(f"Backup staging cleanup also failed: {cleanup_error}")


def _backup_error(path: Path, detail: str) -> OperationFailedError:
    return OperationFailedError(
        detail,
        production_changed=False,
        previous_application_running=None,
        log_path=path,
        next_safe_action="Production was not changed; correct the failure and create a new backup.",
    )


def _verification_error(path: Path, detail: str) -> SafetyCheckError:
    return SafetyCheckError(
        detail,
        production_changed=False,
        previous_application_running=None,
        log_path=path,
        next_safe_action="Do not restore this backup; use another verified backup or create one.",
    )
