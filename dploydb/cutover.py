"""Final backup, production migration, and pre-traffic database rollback."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from dploydb.backup import create_verified_backup, verify_backup
from dploydb.config import LoadedConfiguration
from dploydb.errors import (
    DployDBError,
    ExternalCommandError,
    OperationFailedError,
    RecoveryRequiredError,
    SafetyCheckError,
)
from dploydb.migration import MIGRATION_MAX_OUTPUT_BYTES, migration_command_evidence
from dploydb.models import (
    BackupArtifact,
    BackupPurpose,
    MigrationCommandEvidence,
    ProductionMigrationResult,
    VerifiedDatabaseRestoreResult,
    utc_now,
)
from dploydb.restore import FaultInjector, restore_verified_database
from dploydb.runners.base import CommandExecutor, ProductionStop, validate_operation_id
from dploydb.sqlite_checks import verify_sqlite_database
from dploydb.storage.local import LocalBackupStorage
from dploydb.subprocesses import CommandOutcome, CommandResult, SubprocessRunner

MigrationEvidenceSink = Callable[[MigrationCommandEvidence], None]


def create_final_backup(
    loaded: LoadedConfiguration,
    *,
    operation_id: str,
    stopped: ProductionStop,
) -> BackupArtifact:
    """Create and reverify the final snapshot only after managed writers stopped."""
    operation = validate_operation_id(operation_id)
    _require_stopped_application(loaded, stopped)
    config = loaded.config
    storage = LocalBackupStorage(config.backup.local_directory)
    artifact = create_verified_backup(
        config.database.path,
        project=loaded.secrets.redact_text(config.project),
        purpose=BackupPurpose.FINAL,
        storage=storage,
        operation_id=operation,
        metadata_source_path=_safe_metadata_path(config.database.path, loaded),
    )
    return _verify_final_backup(
        loaded,
        artifact,
        operation_id=operation,
        production_changed=False,
    )


def migrate_production_database(
    loaded: LoadedConfiguration,
    *,
    operation_id: str,
    stopped: ProductionStop,
    final_backup: BackupArtifact,
    config_path: Path,
    traffic_activated: bool,
    evidence_sink: MigrationEvidenceSink,
    command_environment: Mapping[str, str] | None = None,
    command_runner: CommandExecutor | None = None,
    cancellation_event: threading.Event | None = None,
    log_path: Path,
) -> ProductionMigrationResult:
    """Run the rehearsed configured command on production with complete evidence."""
    operation = validate_operation_id(operation_id)
    _require_stopped_application(loaded, stopped)
    if traffic_activated:
        raise SafetyCheckError(
            "production migration cannot start after new traffic was activated",
            production_changed=False,
            previous_application_running=False,
            log_path=log_path,
            next_safe_action="Keep the current release and inspect the traffic state.",
        )
    verified_final = _verify_final_backup(
        loaded,
        final_backup,
        operation_id=operation,
        production_changed=False,
    )
    config = loaded.config
    environment = dict(os.environ if command_environment is None else command_environment)
    environment[config.database.path_env] = str(config.database.path)
    runner = command_runner or SubprocessRunner(
        secrets=loaded.secrets,
        max_output_bytes=MIGRATION_MAX_OUTPUT_BYTES,
    )
    result = runner.run(
        config.migration.command,
        timeout_seconds=config.migration.timeout_seconds,
        environment=environment,
        working_directory=config_path.resolve().parent,
        cancellation_event=cancellation_event,
    )
    evidence = migration_command_evidence(result)
    try:
        evidence_sink(evidence)
    except Exception as error:
        changed = _migration_may_have_changed_production(result)
        raise RecoveryRequiredError(
            "production migration command finished but its durable evidence failed: "
            + loaded.secrets.redact_text(f"{type(error).__name__}: {error}"),
            production_changed=changed,
            previous_application_running=False,
            log_path=log_path,
            next_safe_action=(
                "Keep traffic blocked and applications stopped. Preserve state and restore "
                "the final backup if the migration process started."
            ),
        ) from None
    _require_usable_production_migration(result, log_path=log_path)
    try:
        sqlite = verify_sqlite_database(config.database.path)
    except DployDBError as error:
        raise OperationFailedError(
            "post-migration production SQLite verification failed: " + error.payload.what_failed,
            production_changed=True,
            previous_application_running=False,
            log_path=log_path,
            next_safe_action=(
                "Keep traffic blocked and applications stopped; restore the verified final backup."
            ),
        ) from None
    return ProductionMigrationResult(
        operation_id=operation,
        final_backup_id=verified_final.metadata.backup_id,
        final_backup_sha256=verified_final.metadata.sha256,
        command=evidence,
        sqlite=sqlite,
        completed_at=utc_now(),
    )


def restore_final_backup(
    loaded: LoadedConfiguration,
    *,
    operation_id: str,
    stopped: ProductionStop,
    final_backup: BackupArtifact,
    traffic_activated: bool,
    fault_injector: FaultInjector | None = None,
) -> VerifiedDatabaseRestoreResult:
    """Restore the operation's final backup while traffic and database users are blocked."""
    operation = validate_operation_id(operation_id)
    _require_stopped_application(loaded, stopped)
    if traffic_activated:
        raise SafetyCheckError(
            "automatic database restore is forbidden after new traffic was activated",
            production_changed=True,
            previous_application_running=False,
            log_path=loaded.config.database.path,
            next_safe_action=(
                "Keep the current database and use the confirmed manual restore workflow."
            ),
        )
    verified = _verify_final_backup(
        loaded,
        final_backup,
        operation_id=operation,
        production_changed=True,
    )
    restored = restore_verified_database(
        verified,
        loaded.config.database.path,
        application_stopped=True,
        traffic_activated=traffic_activated,
        secrets=loaded.secrets,
        fault_injector=fault_injector,
    )
    if (
        restored.backup_id != verified.metadata.backup_id
        or restored.size_bytes != verified.metadata.size_bytes
        or restored.sha256 != verified.metadata.sha256
    ):
        raise RecoveryRequiredError(
            "database rollback result contradicts the verified final backup",
            production_changed=True,
            previous_application_running=False,
            log_path=loaded.config.database.path,
            next_safe_action="Keep applications stopped and verify the final backup manually.",
        )
    return restored


def _require_stopped_application(
    loaded: LoadedConfiguration,
    stopped: ProductionStop,
) -> None:
    if not isinstance(stopped, ProductionStop):
        raise TypeError("stopped must be a ProductionStop proof")
    handle = stopped.handle
    if (
        stopped.inspection.handle != handle
        or stopped.inspection.running
        or not stopped.command.succeeded
        or stopped.command.stdout.truncated
        or stopped.command.stderr.truncated
        or not stopped.inspection.command.succeeded
        or stopped.inspection.command.stdout.truncated
        or stopped.inspection.command.stderr.truncated
        or handle.database_directory.resolve() != loaded.config.database.path.parent.resolve()
        or handle.database_target != loaded.config.application.database_volume_target
    ):
        raise SafetyCheckError(
            "final database operation requires matching proof that production is stopped",
            production_changed=False,
            previous_application_running=None,
            log_path=loaded.config.database.path,
            next_safe_action=(
                "Do not access production; stop and inspect the exact current application first."
            ),
        )


def _verify_final_backup(
    loaded: LoadedConfiguration,
    artifact: BackupArtifact,
    *,
    operation_id: str,
    production_changed: bool,
) -> BackupArtifact:
    storage = LocalBackupStorage(loaded.config.backup.local_directory)
    try:
        verified = verify_backup(storage, artifact.metadata.backup_id)
    except DployDBError as error:
        raise SafetyCheckError(
            "final backup verification failed: " + error.payload.what_failed,
            production_changed=production_changed,
            previous_application_running=False,
            log_path=artifact.metadata_path,
            next_safe_action="Do not migrate or restore production; create a new final backup.",
        ) from None
    if (
        verified != artifact
        or verified.metadata.purpose is not BackupPurpose.FINAL
        or verified.metadata.operation_id != operation_id
        or verified.metadata.project != loaded.secrets.redact_text(loaded.config.project)
    ):
        raise SafetyCheckError(
            "final backup identity does not match the active deployment operation",
            production_changed=production_changed,
            previous_application_running=False,
            log_path=artifact.metadata_path,
            next_safe_action="Do not migrate or restore production; create a new final backup.",
        )
    return verified


def _require_usable_production_migration(
    result: CommandResult,
    *,
    log_path: Path,
) -> None:
    if result.outcome is CommandOutcome.SUCCEEDED:
        if not result.stdout.truncated and not result.stderr.truncated:
            return
        raise ExternalCommandError(
            "production migration succeeded but complete output evidence was unavailable",
            production_changed=True,
            previous_application_running=False,
            log_path=log_path,
            next_safe_action=(
                "Keep traffic blocked and applications stopped; restore the final backup."
            ),
        )
    if result.outcome is CommandOutcome.CLEANUP_FAILED:
        raise RecoveryRequiredError(
            "production migration process cleanup could not be proven: "
            + (result.cleanup_error or "unknown cleanup failure"),
            production_changed=True,
            previous_application_running=False,
            log_path=log_path,
            next_safe_action=(
                "Keep traffic blocked. Confirm the migration process group is gone before "
                "restoring the final backup."
            ),
        )
    production_changed = _migration_may_have_changed_production(result)
    if result.outcome is CommandOutcome.NONZERO_EXIT:
        detail = f"exited with status {result.exit_code}"
    elif result.outcome is CommandOutcome.TIMED_OUT:
        detail = "timed out and its process group was terminated"
    elif result.outcome is CommandOutcome.CANCELLED:
        detail = "was cancelled and its process group was terminated"
    else:
        detail = "could not start: " + (result.start_error or "unknown start failure")
    raise ExternalCommandError(
        "production migration " + detail,
        production_changed=production_changed,
        previous_application_running=False,
        log_path=log_path,
        next_safe_action=(
            "Keep traffic blocked and applications stopped; restore the final backup if the "
            "migration process started."
        ),
    )


def _migration_may_have_changed_production(result: CommandResult) -> bool:
    return (
        result.outcome
        not in {
            CommandOutcome.START_FAILED,
            CommandOutcome.CANCELLED,
        }
        or result.exit_code is not None
    )


def _safe_metadata_path(path: Path, loaded: LoadedConfiguration) -> Path:
    safe = Path(loaded.secrets.redact_text(str(path)))
    return safe if safe.is_absolute() else Path("/[REDACTED]")
