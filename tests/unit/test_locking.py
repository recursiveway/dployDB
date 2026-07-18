"""Unit tests for durable deployment locking and owner diagnosis."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from dploydb.errors import (
    ExitCode,
    LockUnavailableError,
    RecoveryRequiredError,
    SafetyCheckError,
)
from dploydb.locking import (
    FILE_MODE,
    LOCK_FILE_NAME,
    OWNER_FILE_NAME,
    DeploymentLock,
    LockInspectionState,
    inspect_lock,
)
from dploydb.models import (
    LockOwnerMetadata,
    LockOwnerState,
    ProcessIdentity,
)
from dploydb.redaction import REDACTION_MARKER, SecretRegistry

NOW = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
OPERATION_ID = "op_11111111111111111111111111111111"
SECOND_OPERATION_ID = "op_22222222222222222222222222222222"


def make_lock(
    tmp_path: Path,
    *,
    secrets: SecretRegistry | None = None,
    clock: Callable[[], datetime] | None = None,
) -> DeploymentLock:
    registry = secrets if secrets is not None else SecretRegistry()
    if clock is None:
        return DeploymentLock(tmp_path / "state", secrets=registry)
    return DeploymentLock(tmp_path / "state", secrets=registry, clock=clock)


def active_owner(
    *,
    owner_id: str = "lock_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    pid: int | None = None,
    hostname: str = "test-host",
) -> LockOwnerMetadata:
    return LockOwnerMetadata(
        owner_id=owner_id,
        operation_id=OPERATION_ID,
        operation_type="deploy",
        process=ProcessIdentity(pid=pid or os.getpid(), hostname=hostname),
        state=LockOwnerState.ACTIVE,
        acquired_at=NOW,
    )


def write_owner(path: Path, owner: LockOwnerMetadata) -> bytes:
    payload = (
        json.dumps(
            owner.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    path.write_bytes(payload)
    path.chmod(FILE_MODE)
    return payload


def test_lock_owner_metadata_enforces_lifecycle_and_utc() -> None:
    localized = datetime(2026, 7, 18, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    owner = active_owner().model_copy(update={"acquired_at": localized})
    owner = LockOwnerMetadata.model_validate_json(owner.model_dump_json())

    assert owner.acquired_at == NOW
    assert owner.model_dump(mode="json")["acquired_at"] == "2026-07-18T10:00:00.000Z"

    with pytest.raises(ValidationError, match="released_at"):
        LockOwnerMetadata.model_validate_json(
            active_owner()
            .model_copy(update={"released_at": NOW + timedelta(seconds=1)})
            .model_dump_json()
        )

    with pytest.raises(ValidationError, match="requires released_at"):
        LockOwnerMetadata.model_validate_json(
            active_owner().model_copy(update={"state": LockOwnerState.RELEASED}).model_dump_json()
        )


def test_context_manager_records_and_releases_owner_atomically(tmp_path: Path) -> None:
    lock = make_lock(tmp_path, clock=lambda: NOW)

    with lock:
        owner = lock.record_owner(
            operation_id=OPERATION_ID,
            operation_type="deploy",
            process=ProcessIdentity(pid=321, hostname="host-a"),
        )
        assert lock.acquired is True
        assert owner.state is LockOwnerState.ACTIVE
        inspection = lock.inspect()
        assert inspection.state is LockInspectionState.ACTIVE
        assert inspection.lock_held is True
        assert inspection.owner == owner

    assert lock.acquired is False
    assert lock.owner is not None
    assert lock.owner.state is LockOwnerState.RELEASED
    lock.release()

    inspection = lock.inspect()
    assert inspection.state is LockInspectionState.IDLE
    assert inspection.lock_held is False
    assert inspection.owner == lock.owner
    assert stat.S_IMODE(lock.state_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock.lock_path.stat().st_mode) == FILE_MODE
    assert stat.S_IMODE(lock.owner_path.stat().st_mode) == FILE_MODE


def test_lock_file_is_persistent_and_never_replaced(tmp_path: Path) -> None:
    lock = make_lock(tmp_path, clock=lambda: NOW)
    with lock:
        inode = lock.lock_path.stat().st_ino
        lock.record_owner(operation_id=OPERATION_ID, operation_type="deploy")

    with make_lock(tmp_path, clock=lambda: NOW + timedelta(seconds=1)) as later:
        assert later.lock_path.stat().st_ino == inode
        later.record_owner(operation_id=SECOND_OPERATION_ID, operation_type="deploy")

    assert lock.lock_path.exists()


def test_second_lock_is_blocked_by_kernel_even_in_same_process(tmp_path: Path) -> None:
    first = make_lock(tmp_path, clock=lambda: NOW)
    second = make_lock(tmp_path, clock=lambda: NOW)

    with first:
        first.record_owner(operation_id=OPERATION_ID, operation_type="deploy")
        with pytest.raises(LockUnavailableError) as captured:
            second.acquire()

    assert captured.value.exit_code is ExitCode.LOCK_UNAVAILABLE
    assert "Another DployDB operation" in captured.value.payload.what_failed
    assert captured.value.payload.production_changed is False


def test_stale_owner_requires_exact_acknowledgement(tmp_path: Path) -> None:
    initial = make_lock(tmp_path, clock=lambda: NOW)
    initial.acquire()
    stale = active_owner()
    write_owner(initial.owner_path, stale)
    initial.previous_owner = stale
    initial.release()

    later = make_lock(tmp_path, clock=lambda: NOW + timedelta(minutes=1)).acquire()
    assert later.previous_owner == stale
    with pytest.raises(RecoveryRequiredError, match="explicit owner-token"):
        later.record_owner(operation_id=SECOND_OPERATION_ID, operation_type="deploy")

    replacement = later.record_owner(
        operation_id=SECOND_OPERATION_ID,
        operation_type="deploy",
        replace_stale_owner_id=stale.owner_id,
    )
    assert replacement.owner_id != stale.owner_id
    later.release()


def test_live_or_reused_pid_never_overrides_kernel_truth(tmp_path: Path) -> None:
    lock = make_lock(tmp_path)
    with lock:
        pass
    stale = active_owner(pid=os.getpid())
    before = write_owner(lock.owner_path, stale)

    inspection = inspect_lock(lock.state_directory, secrets=SecretRegistry())

    assert inspection.state is LockInspectionState.STALE_OWNER
    assert inspection.lock_held is False
    assert inspection.owner == stale
    assert lock.owner_path.read_bytes() == before


@pytest.mark.parametrize(
    ("payload", "mode"),
    [
        (b"{broken\n", FILE_MODE),
        (b"{}\n", FILE_MODE),
        (active_owner().model_dump_json().encode() + b"\n", 0o640),
    ],
)
def test_invalid_owner_metadata_requires_recovery_without_mutation(
    tmp_path: Path, payload: bytes, mode: int
) -> None:
    lock = make_lock(tmp_path)
    with lock:
        pass
    lock.owner_path.write_bytes(payload)
    lock.owner_path.chmod(mode)
    before = lock.owner_path.read_bytes()

    inspection = lock.inspect()
    assert inspection.state is LockInspectionState.RECOVERY_REQUIRED
    assert inspection.metadata_error is not None
    assert lock.owner_path.read_bytes() == before

    with pytest.raises(RecoveryRequiredError):
        lock.acquire()
    assert lock.owner_path.read_bytes() == before
    lock.owner_path.unlink()
    with lock:
        pass


def test_active_lock_remains_authoritative_when_owner_metadata_is_corrupt(
    tmp_path: Path,
) -> None:
    first = make_lock(tmp_path)
    first.acquire()
    first.owner_path.write_bytes(b"not-json\n")
    first.owner_path.chmod(FILE_MODE)

    inspection = inspect_lock(first.state_directory, secrets=SecretRegistry())

    assert inspection.state is LockInspectionState.ACTIVE
    assert inspection.lock_held is True
    assert inspection.owner is None
    assert inspection.metadata_error is not None
    first.release()


def test_missing_state_inspection_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "missing" / "state"

    inspection = inspect_lock(root, secrets=SecretRegistry())

    assert inspection.state is LockInspectionState.IDLE
    assert not root.exists()


def test_dangling_symlinked_state_inspection_requires_recovery(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.symlink_to(tmp_path / "missing", target_is_directory=True)

    inspection = inspect_lock(root, secrets=SecretRegistry())

    assert inspection.state is LockInspectionState.RECOVERY_REQUIRED
    assert inspection.metadata_error is not None
    assert root.is_symlink()


def test_registered_secret_is_redacted_before_owner_persistence(tmp_path: Path) -> None:
    secret = "sensitive-hostname"
    registry = SecretRegistry()
    registry.register(secret)
    lock = make_lock(tmp_path, secrets=registry, clock=lambda: NOW)

    with lock:
        persisted = lock.record_owner(
            operation_id=OPERATION_ID,
            operation_type="deploy",
            process=ProcessIdentity(pid=123, hostname=secret),
        )
        assert persisted.process.hostname == REDACTION_MARKER

    assert secret.encode() not in lock.owner_path.read_bytes()


def test_owner_replace_failure_preserves_previous_complete_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = make_lock(tmp_path, clock=lambda: NOW)
    with lock:
        pass
    previous = LockOwnerMetadata.model_validate_json(
        active_owner()
        .model_copy(update={"state": LockOwnerState.RELEASED, "released_at": NOW})
        .model_dump_json()
    )
    before = write_owner(lock.owner_path, previous)
    holder = make_lock(tmp_path, clock=lambda: NOW + timedelta(seconds=1)).acquire()

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("injected replace failure")

    monkeypatch.setattr("dploydb.locking.os.replace", fail_replace)
    with pytest.raises(SafetyCheckError, match="atomically"):
        holder.record_owner(operation_id=SECOND_OPERATION_ID, operation_type="deploy")
    holder.release()

    assert lock.owner_path.read_bytes() == before
    assert not list(lock.state_directory.glob(f".{OWNER_FILE_NAME}.*.tmp"))


def test_post_replace_sync_failure_leaves_complete_stale_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = make_lock(tmp_path, clock=lambda: NOW).acquire()

    def fail_directory_sync(directory: Path) -> None:
        del directory
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(lock, "_fsync_directory", fail_directory_sync)
    with pytest.raises(RecoveryRequiredError, match="directory sync failed"):
        lock.record_owner(operation_id=OPERATION_ID, operation_type="deploy")
    lock.release()

    inspection = inspect_lock(lock.state_directory, secrets=SecretRegistry())
    assert inspection.state is LockInspectionState.STALE_OWNER
    assert inspection.owner is not None
    assert inspection.owner.state is LockOwnerState.ACTIVE


def test_release_metadata_failure_still_relinquishes_kernel_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = make_lock(tmp_path, clock=lambda: NOW).acquire()
    owner = lock.record_owner(operation_id=OPERATION_ID, operation_type="deploy")

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("injected release replace failure")

    monkeypatch.setattr("dploydb.locking.os.replace", fail_replace)
    with pytest.raises(SafetyCheckError, match="atomically"):
        lock.release()
    assert lock.acquired is False

    later = make_lock(tmp_path).acquire()
    assert later.previous_owner is not None
    assert later.previous_owner.owner_id == owner.owner_id
    later.release()


def test_symlinked_lock_and_owner_paths_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    target.chmod(FILE_MODE)
    (root / LOCK_FILE_NAME).symlink_to(target)

    with pytest.raises(SafetyCheckError, match="symlinked"):
        DeploymentLock(root, secrets=SecretRegistry()).acquire()

    (root / LOCK_FILE_NAME).unlink()
    with DeploymentLock(root, secrets=SecretRegistry()):
        pass
    (root / OWNER_FILE_NAME).symlink_to(target)

    inspection = inspect_lock(root, secrets=SecretRegistry())
    assert inspection.state is LockInspectionState.RECOVERY_REQUIRED
    assert target.read_text(encoding="utf-8") == "target"


def test_unsafe_existing_lock_permissions_are_not_repaired(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    path = root / LOCK_FILE_NAME
    path.touch(mode=0o640)
    path.chmod(0o640)

    with pytest.raises(SafetyCheckError, match="mode-0600"):
        DeploymentLock(root, secrets=SecretRegistry()).acquire()

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_relative_and_filesystem_root_state_paths_are_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        DeploymentLock(Path("relative"), secrets=SecretRegistry())
    with pytest.raises(ValueError, match="filesystem root"):
        DeploymentLock(Path("/"), secrets=SecretRegistry())


def test_invalid_clock_does_not_leave_kernel_lock_held(tmp_path: Path) -> None:
    invalid = make_lock(tmp_path, clock=lambda: datetime(2026, 7, 18, 10, 0))

    with pytest.raises(ValueError, match="timezone-aware"):
        invalid.acquire()

    with make_lock(tmp_path):
        pass
