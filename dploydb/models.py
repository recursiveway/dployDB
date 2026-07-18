"""Shared durable contracts for DployDB operations and failures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NewType, Self, TypedDict
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

OperationId = NewType("OperationId", str)


class DeploymentState(StrEnum):
    """Durable states allowed by the DployDB deployment contract."""

    CREATED = "created"
    PREFLIGHT_PASSED = "preflight_passed"
    SNAPSHOT_VERIFIED = "snapshot_verified"
    REHEARSAL_PASSED = "rehearsal_passed"
    CANDIDATE_HEALTHY = "candidate_healthy"
    MAINTENANCE_ENABLED = "maintenance_enabled"
    CURRENT_APP_STOPPED = "current_app_stopped"
    FINAL_SNAPSHOT_VERIFIED = "final_snapshot_verified"
    PRODUCTION_MIGRATED = "production_migrated"
    NEW_APP_HEALTHY = "new_app_healthy"
    TRAFFIC_ACTIVATED = "traffic_activated"
    ACTIVE = "active"
    ROLLBACK_STARTED = "rollback_started"
    ROLLED_BACK = "rolled_back"
    FAILED_SAFE = "failed_safe"
    RECOVERY_REQUIRED = "recovery_required"
    MANUAL_RESTORE_STARTED = "manual_restore_started"
    MANUAL_RESTORE_COMPLETED = "manual_restore_completed"


class OperationStatus(StrEnum):
    """Generic durable lifecycle for all state-tracked operations."""

    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED_SAFE = "failed_safe"
    RECOVERY_REQUIRED = "recovery_required"


class BackupPurpose(StrEnum):
    """Why an immutable verified backup was created."""

    STANDALONE = "standalone"
    PRE_RESTORE = "pre_restore"
    REHEARSAL = "rehearsal"
    FINAL = "final"


class LockOwnerState(StrEnum):
    """Lifecycle recorded in the diagnostic deployment-lock owner file."""

    ACTIVE = "active"
    RELEASED = "released"


class DiagnosticOutcome(StrEnum):
    """Stable outcome values emitted by host diagnostics."""

    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


class RuntimeStatus(StrEnum):
    """Read-only summary of the deployment lock and durable operation state."""

    IDLE = "idle"
    ACTIVE = "active"
    INTERRUPTED = "interrupted"
    RECOVERY_REQUIRED = "recovery_required"


class DurableModel(BaseModel):
    """Strict immutable base for versioned JSON state records."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProcessIdentity(DurableModel):
    """Diagnostic identity of the process that created an operation."""

    pid: int = Field(gt=0)
    hostname: str = Field(min_length=1, max_length=255)


class LockOwnerMetadata(DurableModel):
    """Versioned diagnostic metadata; the kernel lock remains authoritative."""

    schema_version: Literal[1] = 1
    owner_id: str = Field(pattern=r"^lock_[0-9a-f]{32}$")
    operation_id: str = Field(pattern=r"^op_[0-9a-f]{32}$")
    operation_type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    process: ProcessIdentity
    state: LockOwnerState
    acquired_at: datetime
    released_at: datetime | None = None

    @field_validator("acquired_at", "released_at")
    @classmethod
    def normalize_lock_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("acquired_at", "released_at")
    def serialize_lock_timestamp(self, value: datetime | None) -> str | None:
        return None if value is None else serialize_utc_timestamp(value)

    @model_validator(mode="after")
    def validate_lock_lifecycle(self) -> Self:
        if self.state is LockOwnerState.ACTIVE:
            if self.released_at is not None:
                raise ValueError("active lock owner must not have released_at")
        elif self.released_at is None:
            raise ValueError("released lock owner requires released_at")
        if self.released_at is not None and self.released_at < self.acquired_at:
            raise ValueError("released_at must not precede acquired_at")
        return self


class SafetyFacts(DurableModel):
    """Persisted safety facts needed to diagnose an interrupted operation."""

    production_changed: bool = False
    previous_application_running: bool | None = None
    recovery_required: bool = False


class FailureRecord(DurableModel):
    """Redacted durable details for an operation failure."""

    error_code: str = Field(min_length=1, max_length=128)
    what_failed: str = Field(min_length=1, max_length=4096)
    log_path: str | None = Field(default=None, max_length=4096)
    next_safe_action: str = Field(min_length=1, max_length=4096)


class OperationManifest(DurableModel):
    """Atomic summary of one generic DployDB operation."""

    schema_version: Literal[1] = 1
    operation_id: str = Field(pattern=r"^op_[0-9a-f]{32}$")
    operation_type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    project: str = Field(min_length=1, max_length=64)
    status: OperationStatus
    stage: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    process: ProcessIdentity
    safety: SafetyFacts
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    failure: FailureRecord | None = None
    last_event_sequence: int = Field(ge=1)

    @field_validator("started_at", "updated_at", "completed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("started_at", "updated_at", "completed_at")
    def serialize_timestamp(self, value: datetime | None) -> str | None:
        return None if value is None else serialize_utc_timestamp(value)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.updated_at < self.started_at:
            raise ValueError("updated_at must not precede started_at")

        terminal = self.status is not OperationStatus.IN_PROGRESS
        if terminal:
            if self.completed_at is None:
                raise ValueError("terminal operation requires completed_at")
            if self.completed_at != self.updated_at:
                raise ValueError("completed_at must equal updated_at for a terminal operation")
        elif self.completed_at is not None:
            raise ValueError("in-progress operation must not have completed_at")

        failed = self.status in {
            OperationStatus.FAILED_SAFE,
            OperationStatus.RECOVERY_REQUIRED,
        }
        if failed != (self.failure is not None):
            raise ValueError("failure details must be present exactly for failed operations")

        recovery_required = self.status is OperationStatus.RECOVERY_REQUIRED
        if self.safety.recovery_required != recovery_required:
            raise ValueError("recovery_required safety fact must match operation status")
        return self


class OperationEvent(DurableModel):
    """One immutable append-only operation event."""

    schema_version: Literal[1] = 1
    sequence: int = Field(ge=1)
    timestamp: datetime
    operation_id: str = Field(pattern=r"^op_[0-9a-f]{32}$")
    status: OperationStatus
    stage: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    message: str = Field(min_length=1, max_length=4096)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def normalize_event_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("timestamp")
    def serialize_event_timestamp(self, value: datetime) -> str:
        return serialize_utc_timestamp(value)


class DiagnosticCheck(DurableModel):
    """One stable, redacted doctor check result."""

    check_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    outcome: DiagnosticOutcome
    message: str = Field(min_length=1, max_length=4096)
    evidence: dict[str, Any] = Field(default_factory=dict)


class SQLiteVerification(DurableModel):
    """Bounded read-only evidence that a SQLite database passed required checks."""

    schema_version: Literal[1] = 1
    quick_check_passed: Literal[True] = True
    foreign_key_check_passed: Literal[True] = True
    integrity_check_passed: Literal[True] | None = None
    checked_at: datetime
    duration_seconds: float = Field(ge=0)

    @field_validator("checked_at")
    @classmethod
    def normalize_checked_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("checked_at")
    def serialize_checked_at(self, value: datetime) -> str:
        return serialize_utc_timestamp(value)


class BackupMetadata(DurableModel):
    """Metadata commit marker for one immutable verified local backup."""

    schema_version: Literal[1] = 1
    backup_id: str = Field(pattern=r"^backup_[0-9a-f]{32}$")
    project: str = Field(min_length=1, max_length=64)
    purpose: BackupPurpose
    source_database_path: Path
    database_file_name: str = Field(pattern=r"^backup_[0-9a-f]{32}\.db$")
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sqlite: SQLiteVerification
    operation_id: str | None = Field(default=None, pattern=r"^op_[0-9a-f]{32}$")
    created_at: datetime
    completed_at: datetime

    @field_validator("source_database_path")
    @classmethod
    def validate_source_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("source database path must be absolute")
        return value

    @field_validator("created_at", "completed_at")
    @classmethod
    def normalize_backup_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("created_at", "completed_at")
    def serialize_backup_timestamp(self, value: datetime) -> str:
        return serialize_utc_timestamp(value)

    @model_validator(mode="after")
    def validate_backup_identity(self) -> Self:
        if self.database_file_name != f"{self.backup_id}.db":
            raise ValueError("database filename must match backup ID")
        if self.completed_at < self.created_at:
            raise ValueError("completed_at must not precede created_at")
        return self


class BackupArtifact(DurableModel):
    """Resolved paths and metadata for one committed local backup."""

    metadata: BackupMetadata
    database_path: Path
    metadata_path: Path

    @field_validator("database_path", "metadata_path")
    @classmethod
    def validate_artifact_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("backup artifact paths must be absolute")
        return value

    @model_validator(mode="after")
    def validate_artifact_identity(self) -> Self:
        if self.database_path.name != self.metadata.database_file_name:
            raise ValueError("database artifact path does not match metadata")
        if self.metadata_path.name != f"{self.metadata.backup_id}.json":
            raise ValueError("metadata artifact path does not match backup ID")
        if self.database_path.parent != self.metadata_path.parent:
            raise ValueError("backup database and metadata must share a directory")
        return self


class RestoreResult(DurableModel):
    """Durable result returned by the internal stopped-application restore engine."""

    selected_backup_id: str = Field(pattern=r"^backup_[0-9a-f]{32}$")
    pre_restore_backup_id: str = Field(pattern=r"^backup_[0-9a-f]{32}$")
    database_path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    restored_at: datetime

    @field_validator("database_path")
    @classmethod
    def validate_restore_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("restored database path must be absolute")
        return value

    @field_validator("restored_at")
    @classmethod
    def normalize_restored_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("restored_at")
    def serialize_restored_at(self, value: datetime) -> str:
        return serialize_utc_timestamp(value)


class CapturedCommandOutput(DurableModel):
    """One complete bounded and redacted subprocess stream."""

    text: str
    total_bytes: int = Field(ge=0)
    retained_bytes: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def validate_capture(self) -> Self:
        if self.retained_bytes > self.total_bytes:
            raise ValueError("retained output bytes must not exceed total bytes")
        if self.truncated != (self.retained_bytes < self.total_bytes):
            raise ValueError("output truncation metadata is contradictory")
        return self


class MigrationCommandEvidence(DurableModel):
    """Redacted durable evidence for the developer-supplied migration command."""

    command: tuple[str, ...]
    working_directory: str
    environment_keys: tuple[str, ...]
    outcome: Literal[
        "succeeded",
        "nonzero_exit",
        "start_failed",
        "timed_out",
        "cancelled",
        "cleanup_failed",
    ]
    exit_code: int | None
    stdout: CapturedCommandOutput
    stderr: CapturedCommandOutput
    duration_seconds: float = Field(ge=0)
    termination_reason: Literal["timeout", "cancellation", "interruption"] | None = None
    termination_attempted: bool = False
    forced_kill: bool = False
    start_error: str | None = None
    cleanup_error: str | None = None


class MigrationRehearsalResult(DurableModel):
    """Verified evidence produced by a completed migration rehearsal."""

    operation_id: str = Field(pattern=r"^op_[0-9a-f]{32}$")
    backup_id: str = Field(pattern=r"^backup_[0-9a-f]{32}$")
    backup_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_size_bytes: int = Field(gt=0)
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    command: MigrationCommandEvidence
    sqlite: SQLiteVerification
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def normalize_completed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("completed_at")
    def serialize_completed_at(self, value: datetime) -> str:
        return serialize_utc_timestamp(value)


class FailureData(TypedDict):
    """Stable JSON-compatible failure shape used by human and CI output."""

    ok: Literal[False]
    error_code: str
    exit_code: int
    what_failed: str
    production_changed: bool
    previous_application_running: bool | None
    recovery_required: bool
    log_path: str | None
    next_safe_action: str


@dataclass(frozen=True, slots=True)
class FailurePayload:
    """Safety facts that every expected DployDB failure must report."""

    error_code: str
    exit_code: int
    what_failed: str
    production_changed: bool
    previous_application_running: bool | None
    recovery_required: bool
    log_path: str | None
    next_safe_action: str

    def __post_init__(self) -> None:
        if not self.error_code.strip():
            raise ValueError("error_code must not be empty")
        if self.exit_code <= 0:
            raise ValueError("failure exit_code must be positive")
        if not self.what_failed.strip():
            raise ValueError("what_failed must not be empty")
        if not self.next_safe_action.strip():
            raise ValueError("next_safe_action must not be empty")

    def as_dict(self) -> FailureData:
        """Return the stable machine-readable representation."""
        return {
            "ok": False,
            "error_code": self.error_code,
            "exit_code": self.exit_code,
            "what_failed": self.what_failed,
            "production_changed": self.production_changed,
            "previous_application_running": self.previous_application_running,
            "recovery_required": self.recovery_required,
            "log_path": self.log_path,
            "next_safe_action": self.next_safe_action,
        }


def new_operation_id() -> OperationId:
    """Create an opaque identifier suitable for durable operation records."""
    return OperationId(f"op_{uuid4().hex}")


def new_backup_id() -> str:
    """Create an opaque identifier for one immutable backup artifact."""
    return f"backup_{uuid4().hex}"


def utc_now() -> datetime:
    """Return an aware current UTC timestamp."""
    return datetime.now(UTC)


def serialize_utc_timestamp(value: datetime) -> str:
    """Serialize an aware timestamp as stable RFC 3339 UTC with milliseconds."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
