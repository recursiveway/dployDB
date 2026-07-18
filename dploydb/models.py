"""Shared durable contracts for DployDB operations and failures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
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


class DurableModel(BaseModel):
    """Strict immutable base for versioned JSON state records."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProcessIdentity(DurableModel):
    """Diagnostic identity of the process that created an operation."""

    pid: int = Field(gt=0)
    hostname: str = Field(min_length=1, max_length=255)


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


def utc_now() -> datetime:
    """Return an aware current UTC timestamp."""
    return datetime.now(UTC)


def serialize_utc_timestamp(value: datetime) -> str:
    """Serialize an aware timestamp as stable RFC 3339 UTC with milliseconds."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
