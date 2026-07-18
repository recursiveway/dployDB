"""Read-only runtime status and bounded host diagnostics."""

from __future__ import annotations

import math
import os
import shutil
import socket
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit
from uuid import uuid4

from dploydb.config import DployDBConfig, LoadedConfiguration
from dploydb.errors import (
    DployDBError,
    ExternalCommandError,
    LockUnavailableError,
    RecoveryRequiredError,
    SafetyCheckError,
)
from dploydb.locking import LockInspection, LockInspectionState, inspect_lock
from dploydb.models import (
    DiagnosticCheck,
    DiagnosticOutcome,
    FailurePayload,
    LockOwnerState,
    OperationManifest,
    OperationStatus,
    RuntimeStatus,
    serialize_utc_timestamp,
    utc_now,
)
from dploydb.redaction import JsonValue, SecretRegistry
from dploydb.sqlite_checks import verify_sqlite_database
from dploydb.state import DIRECTORY_MODE, StateStore
from dploydb.storage.local import DIRECTORY_MODE as BACKUP_DIRECTORY_MODE
from dploydb.subprocesses import CommandResult, SubprocessRunner

DOCTOR_COMMAND_TIMEOUT_SECONDS: Final[float] = 10.0
_DEFERRED_CHECKS: Final[tuple[tuple[str, str], ...]] = (
    ("remote_storage", "Remote storage checks begin in Milestone 7."),
    (
        "migration_execution",
        "Doctor never executes migrations; the lock-tracked rehearsal stage owns this check.",
    ),
    ("application_health", "Application health checks begin in Milestone 4."),
    ("traffic_execution", "Traffic-hook execution begins in Milestone 5."),
)


@dataclass(frozen=True, slots=True)
class StatusReport:
    """Stable read-only status report for terminal and JSON rendering."""

    project: str
    checked_at: str
    status: RuntimeStatus
    lock: dict[str, JsonValue]
    operation: dict[str, JsonValue] | None
    warnings: tuple[str, ...]
    next_safe_action: str
    failure: FailurePayload | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.failure is None else self.failure.exit_code

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "ok": self.failure is None,
            "command": "status",
            "project": self.project,
            "checked_at": self.checked_at,
            "status": self.status.value,
            "recovery_required": self.failure is not None,
            "lock": self.lock,
            "operation": self.operation,
            "warnings": list(self.warnings),
            "next_safe_action": self.next_safe_action,
        }
        if self.failure is not None:
            value.update(dict(self.failure.as_dict()))
            value["command"] = "status"
            value["project"] = self.project
            value["checked_at"] = self.checked_at
            value["status"] = self.status.value
            value["lock"] = self.lock
            value["operation"] = self.operation
            value["warnings"] = list(self.warnings)
        return value


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Stable aggregate of one normal or deep doctor pass."""

    project: str
    checked_at: str
    deep: bool
    checks: tuple[DiagnosticCheck, ...]
    failure: FailurePayload | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.failure is None else self.failure.exit_code

    def as_dict(self) -> dict[str, Any]:
        counts = {
            outcome.value: sum(check.outcome is outcome for check in self.checks)
            for outcome in DiagnosticOutcome
        }
        value: dict[str, Any] = {
            "ok": self.failure is None,
            "command": "doctor",
            "project": self.project,
            "checked_at": self.checked_at,
            "deep": self.deep,
            "summary": counts,
            "checks": [check.model_dump(mode="json") for check in self.checks],
        }
        if self.failure is not None:
            value.update(dict(self.failure.as_dict()))
            value["command"] = "doctor"
            value["project"] = self.project
            value["checked_at"] = self.checked_at
            value["deep"] = self.deep
            value["summary"] = counts
            value["checks"] = [check.model_dump(mode="json") for check in self.checks]
        return value


def inspect_runtime_status(
    config: DployDBConfig,
    *,
    secrets: SecretRegistry,
) -> StatusReport:
    """Reconcile kernel-lock truth and durable operation evidence without writes."""
    checked_at = serialize_utc_timestamp(utc_now())
    lock = inspect_lock(config.state_directory, secrets=secrets)
    safe_lock = _lock_evidence(lock, secrets)
    store = StateStore(config.state_directory, secrets=secrets)

    if lock.state is LockInspectionState.ACTIVE:
        warnings: list[str] = []
        operation: OperationManifest | None = None
        if lock.metadata_error is not None:
            warnings.append(secrets.redact_text(lock.metadata_error))
        if lock.owner is None or lock.owner.state is not LockOwnerState.ACTIVE:
            warnings.append(
                "The kernel lock is held while current owner details are unavailable; "
                "the holder may be between durable updates."
            )
        else:
            try:
                operation = store.read_operation(lock.owner.operation_id)[0]
            except DployDBError:
                warnings.append(
                    "Operation details changed or were unreadable while the kernel lock was "
                    "held; wait for the active operation before diagnosing recovery."
                )
        return StatusReport(
            project=config.project,
            checked_at=checked_at,
            status=RuntimeStatus.ACTIVE,
            lock=safe_lock,
            operation=_operation_evidence(operation, secrets),
            warnings=tuple(warnings),
            next_safe_action="Wait for the active operation to finish, then run status again.",
        )

    if lock.state is LockInspectionState.RECOVERY_REQUIRED:
        return _recovery_status(
            config=config,
            checked_at=checked_at,
            lock=safe_lock,
            operation=None,
            secrets=secrets,
            what_failed=lock.metadata_error or "Deployment lock evidence is unsafe.",
            log_path=lock.owner_path,
        )

    try:
        latest = store.latest_operation()
    except DployDBError as error:
        return _recovery_status(
            config=config,
            checked_at=checked_at,
            lock=safe_lock,
            operation=None,
            secrets=secrets,
            what_failed=error.payload.what_failed,
            log_path=error.payload.log_path or config.state_directory,
        )

    operation_evidence = _operation_evidence(latest, secrets)
    if lock.state is LockInspectionState.STALE_OWNER:
        matching_unfinished = (
            lock.owner is not None
            and latest is not None
            and lock.owner.operation_id == latest.operation_id
            and latest.status is OperationStatus.IN_PROGRESS
        )
        if matching_unfinished:
            assert latest is not None
            return _interrupted_status(
                config=config,
                checked_at=checked_at,
                lock=safe_lock,
                operation=latest,
                secrets=secrets,
                detail="The lock owner is stale and its operation did not finish.",
            )
        return _recovery_status(
            config=config,
            checked_at=checked_at,
            lock=safe_lock,
            operation=latest,
            secrets=secrets,
            what_failed="Stale lock-owner metadata contradicts durable operation state.",
            log_path=lock.owner_path,
        )

    if latest is not None and latest.status is OperationStatus.IN_PROGRESS:
        return _interrupted_status(
            config=config,
            checked_at=checked_at,
            lock=safe_lock,
            operation=latest,
            secrets=secrets,
            detail="An operation is unfinished but no process holds the deployment lock.",
        )

    if latest is not None and latest.status is OperationStatus.RECOVERY_REQUIRED:
        return _recovery_status(
            config=config,
            checked_at=checked_at,
            lock=safe_lock,
            operation=latest,
            secrets=secrets,
            what_failed=(
                latest.failure.what_failed
                if latest.failure is not None
                else "The latest operation requires recovery."
            ),
            log_path=(latest.failure.log_path if latest.failure is not None else None),
        )

    return StatusReport(
        project=config.project,
        checked_at=checked_at,
        status=RuntimeStatus.IDLE,
        lock=safe_lock,
        operation=operation_evidence,
        warnings=(),
        next_safe_action="The deployment safety state is idle.",
    )


def run_doctor(
    loaded: LoadedConfiguration,
    *,
    config_path: Path,
    deep: bool,
    environment: Mapping[str, str] | None = None,
    runner: SubprocessRunner | None = None,
) -> DoctorReport:
    """Run bounded host and SQLite checks without executing production behavior."""
    config = loaded.config
    secrets = loaded.secrets
    checked_at = serialize_utc_timestamp(utc_now())
    checks: list[DiagnosticCheck] = []
    command_environment = dict(os.environ if environment is None else environment)
    command_runner = runner or SubprocessRunner(secrets=secrets)
    config_directory = config_path.absolute().parent

    status = inspect_runtime_status(config, secrets=secrets)
    if status.status is RuntimeStatus.ACTIVE:
        checks.append(
            _check(
                secrets,
                "runtime_state",
                DiagnosticOutcome.FAILED,
                "Another DployDB operation holds the deployment lock.",
                {"status": status.status.value},
            )
        )
        lock_failure = LockUnavailableError(
            "Another DployDB operation holds the deployment lock.",
            production_changed=False,
            previous_application_running=None,
            log_path=str(config.state_directory / "deployment-lock-owner.json"),
            next_safe_action="Wait for the active operation to finish, then run doctor again.",
        ).payload
        return _finish_doctor(config, checked_at, deep, checks, lock_failure, secrets)
    if status.failure is not None:
        checks.append(
            _check(
                secrets,
                "runtime_state",
                DiagnosticOutcome.FAILED,
                status.failure.what_failed,
                {"status": status.status.value},
            )
        )
        return _finish_doctor(config, checked_at, deep, checks, status.failure, secrets)
    checks.append(
        _check(
            secrets,
            "runtime_state",
            DiagnosticOutcome.PASSED,
            "No active lock or unresolved operation blocks diagnostics.",
        )
    )

    database_readable = _check_regular_readable(
        checks, secrets, "database_file", config.database.path
    )
    if database_readable:
        try:
            sqlite_evidence = verify_sqlite_database(config.database.path, deep=deep)
        except DployDBError as error:
            checks.append(
                _check(
                    secrets,
                    "sqlite_integrity",
                    DiagnosticOutcome.FAILED,
                    error.payload.what_failed,
                    {"path": str(config.database.path), "deep": deep},
                )
            )
        else:
            checks.append(
                _check(
                    secrets,
                    "sqlite_integrity",
                    DiagnosticOutcome.PASSED,
                    "SQLite integrity checks passed.",
                    sqlite_evidence.model_dump(mode="json"),
                )
            )
    else:
        checks.append(
            _check(
                secrets,
                "sqlite_integrity",
                DiagnosticOutcome.SKIPPED,
                "SQLite checks were skipped because the database file check failed.",
                {"path": str(config.database.path), "deep": deep},
            )
        )
    _check_regular_readable(checks, secrets, "compose_file", config.application.compose_file)
    _check_directory_destination(
        checks,
        secrets,
        "database_directory",
        config.database.path.parent,
        required_mode=None,
    )
    _check_directory_destination(
        checks,
        secrets,
        "state_directory",
        config.state_directory,
        required_mode=DIRECTORY_MODE,
    )
    _check_directory_destination(
        checks,
        secrets,
        "backup_directory",
        config.backup.local_directory,
        required_mode=BACKUP_DIRECTORY_MODE,
    )

    configured_commands: tuple[tuple[str, Sequence[str]], ...] = (
        ("migration_executable", config.migration.command),
        *(
            (("smoke_executable", config.application.smoke_command),)
            if config.application.smoke_command is not None
            else ()
        ),
        ("maintenance_on_executable", config.traffic.maintenance_on_command),
        ("maintenance_off_executable", config.traffic.maintenance_off_command),
        ("activate_new_executable", config.traffic.activate_new_command),
        ("activate_old_executable", config.traffic.activate_old_command),
    )
    for check_id, command in configured_commands:
        resolved = _resolve_executable(command[0], config_directory, command_environment)
        if resolved is None:
            checks.append(
                _check(
                    secrets,
                    check_id,
                    DiagnosticOutcome.FAILED,
                    f"Configured executable could not be resolved: {command[0]}",
                )
            )
        else:
            checks.append(
                _check(
                    secrets,
                    check_id,
                    DiagnosticOutcome.PASSED,
                    "Configured executable is available.",
                    {"path": str(resolved)},
                )
            )

    docker = _resolve_executable("docker", config_directory, command_environment)
    external_failed = False
    if docker is None:
        checks.append(
            _check(
                secrets,
                "docker_cli",
                DiagnosticOutcome.FAILED,
                "Docker executable could not be resolved from PATH.",
            )
        )
    else:
        for check_id, command in (
            ("docker_cli", (str(docker), "--version")),
            ("docker_compose", (str(docker), "compose", "version")),
        ):
            result = command_runner.run(
                command,
                timeout_seconds=DOCTOR_COMMAND_TIMEOUT_SECONDS,
                environment=command_environment,
                working_directory=config_directory,
            )
            external_failed |= not result.succeeded
            checks.append(_command_check(secrets, check_id, result))

    _check_candidate_port(checks, secrets, config)

    if deep:
        for check_id, target in (
            ("database_write_probe", config.database.path.parent),
            ("state_write_probe", config.state_directory),
            ("backup_write_probe", config.backup.local_directory),
        ):
            _check_write_probe(checks, secrets, check_id, target)
        _check_free_space(checks, secrets, config)

        if docker is not None:
            daemon = command_runner.run(
                (str(docker), "info", "--format", "{{json .ServerVersion}}"),
                timeout_seconds=DOCTOR_COMMAND_TIMEOUT_SECONDS,
                environment=command_environment,
                working_directory=config_directory,
            )
            external_failed |= not daemon.succeeded
            checks.append(_command_check(secrets, "docker_daemon", daemon))

            compose = command_runner.run(
                (
                    str(docker),
                    "compose",
                    "--file",
                    str(config.application.compose_file),
                    "config",
                    "--services",
                ),
                timeout_seconds=DOCTOR_COMMAND_TIMEOUT_SECONDS,
                environment=command_environment,
                working_directory=config_directory,
            )
            external_failed |= not compose.succeeded
            if compose.succeeded:
                services = set(compose.stdout.text.splitlines())
                if config.application.service in services:
                    checks.append(
                        _check(
                            secrets,
                            "compose_service",
                            DiagnosticOutcome.PASSED,
                            "Configured Compose service is present.",
                            {"service": config.application.service},
                        )
                    )
                else:
                    checks.append(
                        _check(
                            secrets,
                            "compose_service",
                            DiagnosticOutcome.FAILED,
                            "Configured Compose service is absent from the Compose model.",
                            {"service": config.application.service},
                        )
                    )
            else:
                checks.append(_command_check(secrets, "compose_service", compose))

    for check_id, message in _DEFERRED_CHECKS:
        checks.append(_check(secrets, check_id, DiagnosticOutcome.SKIPPED, message))

    failed = [check for check in checks if check.outcome is DiagnosticOutcome.FAILED]
    report_failure: FailurePayload | None = None
    if failed:
        summary = ", ".join(check.check_id for check in failed)
        error_type = ExternalCommandError if external_failed else SafetyCheckError
        report_failure = error_type(
            f"Doctor found {len(failed)} failed checks: {summary}.",
            production_changed=False,
            previous_application_running=None,
            next_safe_action="Correct the failed checks, then run doctor again.",
        ).payload
    return _finish_doctor(config, checked_at, deep, checks, report_failure, secrets)


def _finish_doctor(
    config: DployDBConfig,
    checked_at: str,
    deep: bool,
    checks: list[DiagnosticCheck],
    failure: FailurePayload | None,
    secrets: SecretRegistry,
) -> DoctorReport:
    safe_failure = failure
    if failure is not None:
        safe_failure = FailurePayload(
            error_code=secrets.redact_text(failure.error_code),
            exit_code=failure.exit_code,
            what_failed=secrets.redact_text(failure.what_failed),
            production_changed=failure.production_changed,
            previous_application_running=failure.previous_application_running,
            recovery_required=failure.recovery_required,
            log_path=(None if failure.log_path is None else secrets.redact_text(failure.log_path)),
            next_safe_action=secrets.redact_text(failure.next_safe_action),
        )
    return DoctorReport(
        project=secrets.redact_text(config.project),
        checked_at=checked_at,
        deep=deep,
        checks=tuple(checks),
        failure=safe_failure,
    )


def _check(
    secrets: SecretRegistry,
    check_id: str,
    outcome: DiagnosticOutcome,
    message: str,
    evidence: Mapping[str, Any] | None = None,
) -> DiagnosticCheck:
    raw = secrets.redact(dict(evidence or {}))
    assert isinstance(raw, dict)
    return DiagnosticCheck(
        check_id=check_id,
        outcome=outcome,
        message=secrets.redact_text(message),
        evidence=raw,
    )


def _lock_evidence(lock: LockInspection, secrets: SecretRegistry) -> dict[str, JsonValue]:
    owner: dict[str, JsonValue] | None = None
    if lock.owner is not None:
        owner = {
            "owner_id": lock.owner.owner_id,
            "operation_id": lock.owner.operation_id,
            "operation_type": lock.owner.operation_type,
            "pid": lock.owner.process.pid,
            "hostname": secrets.redact_text(lock.owner.process.hostname),
            "state": lock.owner.state.value,
            "acquired_at": lock.owner.model_dump(mode="json")["acquired_at"],
            "released_at": lock.owner.model_dump(mode="json")["released_at"],
        }
    return {
        "state": lock.state.value,
        "held": lock.lock_held,
        "owner": owner,
        "metadata_error": (
            None if lock.metadata_error is None else secrets.redact_text(lock.metadata_error)
        ),
        "lock_path": secrets.redact_text(str(lock.lock_path)),
        "owner_path": secrets.redact_text(str(lock.owner_path)),
    }


def _operation_evidence(
    operation: OperationManifest | None,
    secrets: SecretRegistry,
) -> dict[str, JsonValue] | None:
    if operation is None:
        return None
    raw: dict[str, JsonValue] = {
        "operation_id": operation.operation_id,
        "operation_type": operation.operation_type,
        "status": operation.status.value,
        "stage": operation.stage,
        "started_at": operation.model_dump(mode="json")["started_at"],
        "updated_at": operation.model_dump(mode="json")["updated_at"],
        "production_changed": operation.safety.production_changed,
        "previous_application_running": operation.safety.previous_application_running,
        "recovery_required": operation.safety.recovery_required,
    }
    redacted = secrets.redact(raw)
    assert isinstance(redacted, dict)
    return redacted


def _interrupted_status(
    *,
    config: DployDBConfig,
    checked_at: str,
    lock: dict[str, JsonValue],
    operation: OperationManifest,
    secrets: SecretRegistry,
    detail: str,
) -> StatusReport:
    log_path = (
        StateStore(config.state_directory, secrets=secrets)
        .operation_paths(operation.operation_id)
        .events
    )
    failure = RecoveryRequiredError(
        secrets.redact_text(detail),
        production_changed=operation.safety.production_changed,
        previous_application_running=operation.safety.previous_application_running,
        log_path=secrets.redact_text(str(log_path)),
        next_safe_action=(
            "Preserve the operation and lock evidence. Inspect the recorded operation before "
            "retrying; do not delete the lock or state files."
        ),
    ).payload
    return StatusReport(
        project=secrets.redact_text(config.project),
        checked_at=checked_at,
        status=RuntimeStatus.INTERRUPTED,
        lock=lock,
        operation=_operation_evidence(operation, secrets),
        warnings=(),
        next_safe_action=failure.next_safe_action,
        failure=failure,
    )


def _recovery_status(
    *,
    config: DployDBConfig,
    checked_at: str,
    lock: dict[str, JsonValue],
    operation: OperationManifest | None,
    secrets: SecretRegistry,
    what_failed: str,
    log_path: str | Path | None,
) -> StatusReport:
    production_changed = True
    previous_application_running: bool | None = None
    if operation is not None:
        production_changed = operation.safety.production_changed
        previous_application_running = operation.safety.previous_application_running
    failure = RecoveryRequiredError(
        secrets.redact_text(what_failed),
        production_changed=production_changed,
        previous_application_running=previous_application_running,
        log_path=None if log_path is None else secrets.redact_text(str(log_path)),
        next_safe_action=(
            "Preserve the operation and lock evidence. Inspect the recorded state before "
            "retrying; do not delete or repair files by guesswork."
        ),
    ).payload
    return StatusReport(
        project=secrets.redact_text(config.project),
        checked_at=checked_at,
        status=RuntimeStatus.RECOVERY_REQUIRED,
        lock=lock,
        operation=_operation_evidence(operation, secrets),
        warnings=(),
        next_safe_action=failure.next_safe_action,
        failure=failure,
    )


def _check_regular_readable(
    checks: list[DiagnosticCheck],
    secrets: SecretRegistry,
    check_id: str,
    path: Path,
) -> bool:
    try:
        details = path.stat()
        valid = stat.S_ISREG(details.st_mode) and os.access(path, os.R_OK)
    except OSError:
        valid = False
    checks.append(
        _check(
            secrets,
            check_id,
            DiagnosticOutcome.PASSED if valid else DiagnosticOutcome.FAILED,
            "Required file is readable and regular."
            if valid
            else f"Required file is missing, unreadable, or not regular: {path}",
            {"path": str(path)},
        )
    )
    return valid


def _nearest_existing_directory(path: Path) -> Path | None:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current if current.exists() and current.is_dir() else None


def _check_directory_destination(
    checks: list[DiagnosticCheck],
    secrets: SecretRegistry,
    check_id: str,
    path: Path,
    *,
    required_mode: int | None,
) -> None:
    valid = False
    message: str
    try:
        if path.exists():
            details = path.stat()
            mode = stat.S_IMODE(details.st_mode)
            valid = (
                not path.is_symlink()
                and stat.S_ISDIR(details.st_mode)
                and os.access(path, os.W_OK | os.X_OK)
                and (required_mode is None or mode == required_mode)
            )
            message = (
                "Directory exists and is writable."
                if valid
                else f"Directory is unsafe or not writable: {path}"
            )
        else:
            parent = _nearest_existing_directory(path.parent)
            valid = parent is not None and os.access(parent, os.W_OK | os.X_OK)
            message = (
                "Directory does not exist but its nearest parent is writable."
                if valid
                else f"Directory cannot be created safely: {path}"
            )
    except OSError:
        message = f"Directory could not be inspected: {path}"
    checks.append(
        _check(
            secrets,
            check_id,
            DiagnosticOutcome.PASSED if valid else DiagnosticOutcome.FAILED,
            message,
            {"path": str(path)},
        )
    )


def _resolve_executable(
    executable: str,
    config_directory: Path,
    environment: Mapping[str, str],
) -> Path | None:
    if "/" not in executable:
        resolved = shutil.which(executable, path=environment.get("PATH"))
        return None if resolved is None else Path(resolved)
    candidate = Path(executable)
    if not candidate.is_absolute():
        candidate = config_directory / candidate
    try:
        details = candidate.stat()
    except OSError:
        return None
    if not stat.S_ISREG(details.st_mode) or not os.access(candidate, os.X_OK):
        return None
    return candidate.absolute()


def _command_check(
    secrets: SecretRegistry,
    check_id: str,
    result: CommandResult,
) -> DiagnosticCheck:
    message = (
        "Bounded command completed successfully."
        if result.succeeded
        else f"Bounded command did not succeed ({result.outcome.value})."
    )
    return _check(
        secrets,
        check_id,
        DiagnosticOutcome.PASSED if result.succeeded else DiagnosticOutcome.FAILED,
        message,
        result.as_evidence(),
    )


def _check_candidate_port(
    checks: list[DiagnosticCheck],
    secrets: SecretRegistry,
    config: DployDBConfig,
) -> None:
    host = urlsplit(config.application.candidate_health_url).hostname
    available = host is not None
    error: str | None = None
    if host is not None:
        try:
            addresses = socket.getaddrinfo(
                host,
                config.application.candidate_port,
                type=socket.SOCK_STREAM,
            )
            for family, socket_type, protocol, _canonical, address in addresses:
                probe = socket.socket(family, socket_type, protocol)
                try:
                    probe.bind(address)
                finally:
                    probe.close()
        except OSError as exc:
            available = False
            error = secrets.redact_text(str(exc))
    checks.append(
        _check(
            secrets,
            "candidate_port",
            DiagnosticOutcome.PASSED if available else DiagnosticOutcome.FAILED,
            "Candidate port is available."
            if available
            else "Candidate port is unavailable on the configured loopback host.",
            {
                "host": host,
                "port": config.application.candidate_port,
                "error": error,
            },
        )
    )


def _check_write_probe(
    checks: list[DiagnosticCheck],
    secrets: SecretRegistry,
    check_id: str,
    destination: Path,
) -> None:
    directory = destination if destination.exists() else _nearest_existing_directory(destination)
    error: str | None = None
    temporary: Path | None = None
    descriptor = -1
    if directory is None:
        error = "No existing parent directory is available for the write probe."
    else:
        temporary = directory / f".dploydb-doctor-{uuid4().hex}.tmp"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600)
            os.write(descriptor, b"dploydb-doctor\n")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            temporary.unlink()
            temporary = None
        except OSError as exc:
            error = secrets.redact_text(str(exc))
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    cleanup = secrets.redact_text(str(cleanup_error))
                    error = f"{error or 'write probe failed'}; cleanup failed: {cleanup}"
    checks.append(
        _check(
            secrets,
            check_id,
            DiagnosticOutcome.PASSED if error is None else DiagnosticOutcome.FAILED,
            "Temporary write and cleanup probe passed."
            if error is None
            else "Temporary write or cleanup probe failed.",
            {"destination": str(destination), "error": error},
        )
    )


def _check_free_space(
    checks: list[DiagnosticCheck],
    secrets: SecretRegistry,
    config: DployDBConfig,
) -> None:
    try:
        database_size = config.database.path.stat().st_size
        required = math.ceil(database_size * config.database.minimum_free_space_multiplier)
    except OSError as exc:
        checks.append(
            _check(
                secrets,
                "disk_space",
                DiagnosticOutcome.FAILED,
                "Database size could not be inspected for the disk-space check.",
                {"error": str(exc)},
            )
        )
        return

    filesystems: dict[int, tuple[Path, int]] = {}
    error: str | None = None
    try:
        for target in (
            config.database.path.parent,
            config.state_directory,
            config.backup.local_directory,
        ):
            directory = target if target.exists() else _nearest_existing_directory(target)
            if directory is None:
                raise OSError(f"no existing parent for {target}")
            device = directory.stat().st_dev
            free = shutil.disk_usage(directory).free
            filesystems[device] = (directory, free)
    except OSError as exc:
        error = secrets.redact_text(str(exc))

    evidence = {
        "database_size_bytes": database_size,
        "required_free_bytes": required,
        "filesystems": [
            {"path": str(path), "free_bytes": free} for path, free in filesystems.values()
        ],
        "error": error,
    }
    sufficient = error is None and all(free >= required for _path, free in filesystems.values())
    checks.append(
        _check(
            secrets,
            "disk_space",
            DiagnosticOutcome.PASSED if sufficient else DiagnosticOutcome.FAILED,
            "Relevant filesystems meet the configured free-space threshold."
            if sufficient
            else "A relevant filesystem does not meet the configured free-space threshold.",
            evidence,
        )
    )
