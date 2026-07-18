"""Shared durable contracts for DployDB operations and failures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, NewType, TypedDict
from uuid import uuid4

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
