"""Tests for Milestone 1 shared operation and failure contracts."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from dploydb.errors import (
    ConfigurationError,
    DployDBError,
    ErrorKind,
    ExitCode,
    ExternalCommandError,
    InternalError,
    LockUnavailableError,
    OperationFailedError,
    RecoveryRequiredError,
    SafetyCheckError,
)
from dploydb.models import (
    DeploymentState,
    FailurePayload,
    new_operation_id,
    serialize_utc_timestamp,
    utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


ERROR_CASES: Sequence[tuple[type[DployDBError], ErrorKind, ExitCode, bool]] = (
    (ConfigurationError, ErrorKind.CONFIGURATION, ExitCode.CONFIGURATION, False),
    (SafetyCheckError, ErrorKind.SAFETY_CHECK, ExitCode.SAFETY_CHECK, False),
    (LockUnavailableError, ErrorKind.LOCK_UNAVAILABLE, ExitCode.LOCK_UNAVAILABLE, False),
    (ExternalCommandError, ErrorKind.EXTERNAL_COMMAND, ExitCode.EXTERNAL_COMMAND, False),
    (OperationFailedError, ErrorKind.OPERATION_FAILED, ExitCode.OPERATION_FAILED, False),
    (RecoveryRequiredError, ErrorKind.RECOVERY_REQUIRED, ExitCode.RECOVERY_REQUIRED, True),
    (InternalError, ErrorKind.INTERNAL_ERROR, ExitCode.INTERNAL_ERROR, False),
)


@pytest.mark.parametrize(("error_type", "kind", "exit_code", "recovery_required"), ERROR_CASES)
def test_every_expected_error_has_stable_exit_code_and_required_facts(
    error_type: type[DployDBError],
    kind: ErrorKind,
    exit_code: ExitCode,
    recovery_required: bool,
) -> None:
    error = error_type(
        "the bounded operation failed",
        production_changed=True,
        previous_application_running=False,
        log_path="/var/log/dploydb/operation.log",
        next_safe_action="Inspect the log and retry only after correcting the cause.",
    )

    assert error.exit_code is exit_code
    assert error.payload.as_dict() == {
        "ok": False,
        "error_code": kind.value,
        "exit_code": int(exit_code),
        "what_failed": "the bounded operation failed",
        "production_changed": True,
        "previous_application_running": False,
        "recovery_required": recovery_required,
        "log_path": "/var/log/dploydb/operation.log",
        "next_safe_action": "Inspect the log and retry only after correcting the cause.",
    }
    assert str(error) == "the bounded operation failed"


@pytest.mark.parametrize(
    ("field", "value"),
    (("error_code", ""), ("what_failed", "  "), ("next_safe_action", "")),
)
def test_failure_payload_rejects_missing_required_text(field: str, value: str) -> None:
    arguments: dict[str, object] = {
        "error_code": "operation_failed",
        "exit_code": 50,
        "what_failed": "operation failed",
        "production_changed": False,
        "previous_application_running": None,
        "recovery_required": False,
        "log_path": None,
        "next_safe_action": "Retry safely.",
    }
    arguments[field] = value

    with pytest.raises(ValueError):
        FailurePayload(**arguments)  # type: ignore[arg-type]


def test_failure_payload_rejects_non_failure_exit_code() -> None:
    with pytest.raises(ValueError, match="positive"):
        FailurePayload(
            error_code="operation_failed",
            exit_code=0,
            what_failed="operation failed",
            production_changed=False,
            previous_application_running=None,
            recovery_required=False,
            log_path=None,
            next_safe_action="Retry safely.",
        )


def test_deployment_state_contract_is_exact() -> None:
    assert [state.value for state in DeploymentState] == [
        "created",
        "preflight_passed",
        "snapshot_verified",
        "rehearsal_passed",
        "candidate_healthy",
        "maintenance_enabled",
        "current_app_stopped",
        "final_snapshot_verified",
        "production_migrated",
        "new_app_healthy",
        "traffic_activated",
        "active",
        "rollback_started",
        "rolled_back",
        "failed_safe",
        "recovery_required",
        "manual_restore_started",
        "manual_restore_completed",
    ]


def test_operation_identifiers_are_opaque_and_unique() -> None:
    first = new_operation_id()
    second = new_operation_id()

    assert re.fullmatch(r"op_[0-9a-f]{32}", first)
    assert first != second


def test_utc_timestamp_serialization_normalizes_an_aware_value() -> None:
    source = datetime(
        2026,
        7,
        18,
        12,
        34,
        56,
        987654,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )

    assert serialize_utc_timestamp(source) == "2026-07-18T07:04:56.987Z"


def test_utc_timestamp_serialization_rejects_naive_values() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        serialize_utc_timestamp(datetime(2026, 7, 18, 12, 34, 56))


def test_utc_now_is_aware_and_serializable() -> None:
    current = utc_now()

    assert current.utcoffset() == timedelta(0)
    assert serialize_utc_timestamp(current).endswith("Z")
