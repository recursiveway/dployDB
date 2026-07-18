"""Stable expected-error taxonomy for DployDB."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from pathlib import Path
from typing import ClassVar, Final

from dploydb.models import FailurePayload


class ExitCode(IntEnum):
    """Stable DployDB process exit codes."""

    SUCCESS = 0
    USAGE = 2
    CONFIGURATION = 10
    SAFETY_CHECK = 20
    LOCK_UNAVAILABLE = 30
    EXTERNAL_COMMAND = 40
    OPERATION_FAILED = 50
    RECOVERY_REQUIRED = 60
    INTERNAL_ERROR = 70


class ErrorKind(StrEnum):
    """Stable machine-readable categories for expected failures."""

    CONFIGURATION = "configuration_error"
    SAFETY_CHECK = "safety_check_failed"
    LOCK_UNAVAILABLE = "deployment_lock_unavailable"
    EXTERNAL_COMMAND = "external_command_failed"
    OPERATION_FAILED = "operation_failed"
    RECOVERY_REQUIRED = "recovery_required"
    INTERNAL_ERROR = "internal_error"


ERROR_EXIT_CODES: Final[dict[ErrorKind, ExitCode]] = {
    ErrorKind.CONFIGURATION: ExitCode.CONFIGURATION,
    ErrorKind.SAFETY_CHECK: ExitCode.SAFETY_CHECK,
    ErrorKind.LOCK_UNAVAILABLE: ExitCode.LOCK_UNAVAILABLE,
    ErrorKind.EXTERNAL_COMMAND: ExitCode.EXTERNAL_COMMAND,
    ErrorKind.OPERATION_FAILED: ExitCode.OPERATION_FAILED,
    ErrorKind.RECOVERY_REQUIRED: ExitCode.RECOVERY_REQUIRED,
    ErrorKind.INTERNAL_ERROR: ExitCode.INTERNAL_ERROR,
}


class DployDBError(Exception):
    """Base class for failures that the CLI can report without a traceback."""

    kind: ClassVar[ErrorKind] = ErrorKind.OPERATION_FAILED
    requires_recovery: ClassVar[bool] = False

    def __init__(
        self,
        what_failed: str,
        *,
        production_changed: bool = False,
        previous_application_running: bool | None = None,
        log_path: str | Path | None = None,
        next_safe_action: str,
    ) -> None:
        self.payload = FailurePayload(
            error_code=self.kind.value,
            exit_code=int(ERROR_EXIT_CODES[self.kind]),
            what_failed=what_failed,
            production_changed=production_changed,
            previous_application_running=previous_application_running,
            recovery_required=self.requires_recovery,
            log_path=None if log_path is None else str(log_path),
            next_safe_action=next_safe_action,
        )
        super().__init__(what_failed)

    @property
    def exit_code(self) -> ExitCode:
        """Return the stable process exit code for this failure."""
        return ERROR_EXIT_CODES[self.kind]


class ConfigurationError(DployDBError):
    """Configuration is absent, invalid, or cannot be resolved safely."""

    kind = ErrorKind.CONFIGURATION


class SafetyCheckError(DployDBError):
    """A host or operation safety precondition did not pass."""

    kind = ErrorKind.SAFETY_CHECK


class LockUnavailableError(DployDBError):
    """Another process currently owns the operating-system deployment lock."""

    kind = ErrorKind.LOCK_UNAVAILABLE


class ExternalCommandError(DployDBError):
    """A bounded external command failed, timed out, or could not start."""

    kind = ErrorKind.EXTERNAL_COMMAND


class OperationFailedError(DployDBError):
    """An operation failed safely and does not require recovery."""

    kind = ErrorKind.OPERATION_FAILED


class RecoveryRequiredError(DployDBError):
    """State is uncertain and a recovery action is required."""

    kind = ErrorKind.RECOVERY_REQUIRED
    requires_recovery = True


class StateCorruptionError(RecoveryRequiredError):
    """Durable state is malformed, contradictory, or incomplete."""


class InternalError(DployDBError):
    """An unexpected failure converted at the outer CLI boundary."""

    kind = ErrorKind.INTERNAL_ERROR
