"""Durable OS-backed deployment locking and stale-owner diagnosis."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Literal, Self, cast
from uuid import uuid4

from pydantic import ValidationError

from dploydb.errors import (
    DployDBError,
    LockUnavailableError,
    RecoveryRequiredError,
    SafetyCheckError,
)
from dploydb.models import (
    LockOwnerMetadata,
    LockOwnerState,
    ProcessIdentity,
    utc_now,
)
from dploydb.redaction import JsonValue, SecretRegistry

DIRECTORY_MODE: Final = 0o700
FILE_MODE: Final = 0o600
LOCK_FILE_NAME: Final = "deployment.lock"
OWNER_FILE_NAME: Final = "deployment-lock-owner.json"
MAX_OWNER_BYTES: Final = 64 * 1024

_OWNER_TEMPORARY_PREFIX: Final = f".{OWNER_FILE_NAME}."
_IGNORABLE_DIRECTORY_FSYNC_ERRORS = frozenset({errno.EINVAL, errno.ENOTSUP})
_CONTENTION_ERRORS = frozenset({errno.EACCES, errno.EAGAIN})


class LockInspectionState(StrEnum):
    """Read-only deployment-lock states consumed by later diagnostics."""

    IDLE = "idle"
    ACTIVE = "active"
    STALE_OWNER = "stale_owner"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class LockInspection:
    """One read-only snapshot of kernel-lock and diagnostic metadata state."""

    state: LockInspectionState
    lock_held: bool
    owner: LockOwnerMetadata | None
    metadata_error: str | None
    lock_path: Path
    owner_path: Path


class DeploymentLock:
    """Nonblocking exclusive flock with separate diagnostic owner metadata."""

    def __init__(
        self,
        state_directory: Path,
        *,
        secrets: SecretRegistry,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not state_directory.is_absolute():
            raise ValueError("state directory must be absolute")
        if state_directory == Path(state_directory.anchor):
            raise ValueError("state directory must not be the filesystem root")
        self.state_directory = state_directory
        self.lock_path = state_directory / LOCK_FILE_NAME
        self.owner_path = state_directory / OWNER_FILE_NAME
        self.secrets = secrets
        self.clock = clock
        self._descriptor: int | None = None
        self._acquired_at: datetime | None = None
        self._owner: LockOwnerMetadata | None = None
        self.previous_owner: LockOwnerMetadata | None = None

    @property
    def acquired(self) -> bool:
        """Return whether this instance currently owns the kernel lock."""
        return self._descriptor is not None

    @property
    def owner(self) -> LockOwnerMetadata | None:
        """Return the redacted owner record written by this lock instance."""
        return self._owner

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exc_type, traceback
        try:
            self.release()
        except BaseException as cleanup_error:
            if exc_value is None:
                raise
            exc_value.add_note(
                self.secrets.redact_text(f"Deployment lock cleanup also failed: {cleanup_error}")
            )
        return False

    def acquire(self) -> Self:
        """Acquire the kernel lock without replacing prior diagnostic evidence."""
        if self._descriptor is not None:
            raise RuntimeError("deployment lock instance is already acquired")
        self._ensure_layout()
        descriptor = self._open_lock_for_acquire()
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in _CONTENTION_ERRORS:
                    owner = self._read_owner_best_effort()
                    raise self._contention_error(owner) from None
                raise self._safety_error(
                    f"deployment lock could not be acquired: {exc}", self.lock_path
                ) from exc

            try:
                previous_owner = self._read_owner_metadata()
            except BaseException:
                self._unlock_and_close(descriptor)
                raise
            acquired_at = self._now()
            self._descriptor = descriptor
            self._acquired_at = acquired_at
            self.previous_owner = previous_owner
            self._owner = None
            return self
        except BaseException:
            if self._descriptor is None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    def record_owner(
        self,
        *,
        operation_id: str,
        operation_type: str,
        process: ProcessIdentity | None = None,
        replace_stale_owner_id: str | None = None,
    ) -> LockOwnerMetadata:
        """Record a new owner after the matching operation manifest is durable."""
        if self._descriptor is None or self._acquired_at is None:
            raise RuntimeError("the kernel lock must be held before recording an owner")
        if self._owner is not None:
            raise RuntimeError("this lock instance already recorded its owner")

        current = self._read_owner_metadata()
        if current != self.previous_owner:
            raise self._recovery_error(
                "deployment lock owner metadata changed while the kernel lock was held",
                self.owner_path,
            )
        if current is not None and current.state is LockOwnerState.ACTIVE:
            if replace_stale_owner_id != current.owner_id:
                raise self._recovery_error(
                    "stale lock-owner evidence requires explicit owner-token acknowledgement",
                    self.owner_path,
                )
        elif replace_stale_owner_id is not None:
            raise ValueError("replace_stale_owner_id was supplied without stale owner metadata")

        owner = LockOwnerMetadata(
            owner_id=f"lock_{uuid4().hex}",
            operation_id=operation_id,
            operation_type=operation_type,
            process=process or ProcessIdentity(pid=os.getpid(), hostname=socket.gethostname()),
            state=LockOwnerState.ACTIVE,
            acquired_at=self._acquired_at,
        )
        persisted = self._atomic_write_owner(owner)
        self._owner = persisted
        return persisted

    def release(self) -> None:
        """Finalize owner metadata and release the kernel lock idempotently."""
        descriptor = self._descriptor
        if descriptor is None:
            return

        metadata_error: BaseException | None = None
        try:
            if self._owner is not None:
                current = self._read_owner_metadata()
                if current is None or current.owner_id != self._owner.owner_id:
                    raise self._recovery_error(
                        "deployment lock owner metadata no longer matches its holder",
                        self.owner_path,
                    )
                if current.state is not LockOwnerState.ACTIVE:
                    raise self._recovery_error(
                        "deployment lock owner was not active during release", self.owner_path
                    )
                released = current.model_copy(
                    update={
                        "state": LockOwnerState.RELEASED,
                        "released_at": self._now_not_before(current.acquired_at),
                    }
                )
                released = LockOwnerMetadata.model_validate_json(released.model_dump_json())
                self._owner = self._atomic_write_owner(released)
        except BaseException as exc:
            metadata_error = exc

        cleanup_errors = self._unlock_and_close(descriptor)
        self._descriptor = None
        self._acquired_at = None

        if metadata_error is not None:
            for detail in cleanup_errors:
                metadata_error.add_note(self.secrets.redact_text(detail))
            raise metadata_error
        if cleanup_errors:
            raise self._recovery_error("; ".join(cleanup_errors), self.lock_path)

    def inspect(self) -> LockInspection:
        """Inspect lock state without creating, replacing, or deleting any path."""
        return inspect_lock(self.state_directory, secrets=self.secrets)

    def _ensure_layout(self) -> None:
        self._reject_symlink(self.state_directory)
        try:
            self.state_directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        except OSError as exc:
            raise self._safety_error(
                f"state directory could not be created for locking: {exc}",
                self.state_directory,
            ) from exc
        self._validate_directory(self.state_directory)

    def _open_lock_for_acquire(self) -> int:
        self._reject_symlink(self.lock_path)
        existed = self.lock_path.exists()
        if existed:
            self._validate_regular_file(self.lock_path)
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, FILE_MODE)
            if not existed:
                os.fchmod(descriptor, FILE_MODE)
            self._validate_lock_descriptor(descriptor)
            if not existed:
                self._fsync_directory(self.state_directory)
            return descriptor
        except OSError as exc:
            try:
                os.close(descriptor)
            except (OSError, UnboundLocalError):
                pass
            raise self._safety_error(
                f"deployment lock file could not be opened safely: {exc}", self.lock_path
            ) from exc

    def _validate_lock_descriptor(self, descriptor: int) -> None:
        details = os.fstat(descriptor)
        mode = stat.S_IMODE(details.st_mode)
        if not stat.S_ISREG(details.st_mode) or mode != FILE_MODE:
            raise OSError(f"deployment lock must be a mode-0600 regular file, found {mode:04o}")

    def _read_owner_metadata(self) -> LockOwnerMetadata | None:
        if not self.owner_path.exists():
            if self.owner_path.is_symlink():
                raise self._recovery_error(
                    "refusing symlinked deployment lock owner metadata", self.owner_path
                )
            return None
        self._reject_owner_symlink()
        try:
            details = self.owner_path.stat()
            mode = stat.S_IMODE(details.st_mode)
            if not stat.S_ISREG(details.st_mode) or mode != FILE_MODE:
                raise OSError(
                    f"lock owner metadata must be a mode-0600 regular file, found {mode:04o}"
                )
            if details.st_size <= 0 or details.st_size > MAX_OWNER_BYTES:
                raise OSError("lock owner metadata has an invalid size")
            payload = self.owner_path.read_bytes()
            if not payload.endswith(b"\n"):
                raise OSError("lock owner metadata is truncated")
            return LockOwnerMetadata.model_validate_json(payload)
        except (OSError, ValidationError) as exc:
            raise self._recovery_error(
                f"deployment lock owner metadata is invalid or unreadable: {exc}",
                self.owner_path,
            ) from None

    def _read_owner_best_effort(self) -> LockOwnerMetadata | None:
        try:
            return self._read_owner_metadata()
        except DployDBError:
            return None

    def _atomic_write_owner(self, owner: LockOwnerMetadata) -> LockOwnerMetadata:
        raw = cast(JsonValue, owner.model_dump(mode="json"))
        redacted = self.secrets.redact(raw)
        persisted = LockOwnerMetadata.model_validate_json(
            json.dumps(redacted, ensure_ascii=False, allow_nan=False)
        )
        payload = (
            json.dumps(
                persisted.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        if len(payload) > MAX_OWNER_BYTES:
            raise ValueError("serialized lock owner metadata exceeds the size limit")

        temporary = self.state_directory / f"{_OWNER_TEMPORARY_PREFIX}{uuid4().hex}.tmp"
        descriptor = -1
        replaced = False
        try:
            self._reject_owner_symlink()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, FILE_MODE)
            os.fchmod(descriptor, FILE_MODE)
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("short lock-owner metadata write")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.owner_path)
            replaced = True
            self._fsync_directory(self.state_directory)
            return persisted
        except OSError as exc:
            if replaced:
                raise self._recovery_error(
                    "lock owner metadata was replaced but its directory sync failed",
                    self.owner_path,
                ) from exc
            raise self._safety_error(
                f"lock owner metadata could not be written atomically: {exc}",
                self.owner_path,
            ) from exc
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if not replaced:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _validate_directory(self, path: Path) -> None:
        self._reject_symlink(path)
        try:
            details = path.stat()
        except OSError as exc:
            raise self._safety_error(
                f"state directory could not be inspected for locking: {exc}", path
            ) from None
        mode = stat.S_IMODE(details.st_mode)
        if not stat.S_ISDIR(details.st_mode) or mode != DIRECTORY_MODE:
            raise self._safety_error(
                f"state directory must be a mode-0700 directory, found {mode:04o}", path
            )

    def _validate_regular_file(self, path: Path) -> None:
        self._reject_symlink(path)
        try:
            details = path.stat()
        except OSError as exc:
            raise self._safety_error(f"managed lock file is unreadable: {exc}", path) from None
        mode = stat.S_IMODE(details.st_mode)
        if not stat.S_ISREG(details.st_mode) or mode != FILE_MODE:
            raise self._safety_error(
                f"deployment lock must be a mode-0600 regular file, found {mode:04o}", path
            )

    def _reject_symlink(self, path: Path) -> None:
        try:
            if path.is_symlink():
                raise self._safety_error(f"refusing symlinked managed lock path: {path}", path)
        except OSError:
            raise self._safety_error("managed lock path could not be inspected", path) from None

    def _reject_owner_symlink(self) -> None:
        try:
            if self.owner_path.is_symlink():
                raise self._recovery_error(
                    "refusing symlinked deployment lock owner metadata", self.owner_path
                )
        except OSError:
            raise self._recovery_error(
                "deployment lock owner metadata could not be inspected", self.owner_path
            ) from None

    def _fsync_directory(self, directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(directory, flags)
        try:
            try:
                os.fsync(descriptor)
            except OSError as exc:
                if exc.errno not in _IGNORABLE_DIRECTORY_FSYNC_ERRORS:
                    raise
        finally:
            os.close(descriptor)

    def _unlock_and_close(self, descriptor: int) -> list[str]:
        errors: list[str] = []
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as exc:
            errors.append(f"deployment lock could not be explicitly unlocked: {exc}")
        try:
            os.close(descriptor)
        except OSError as exc:
            errors.append(f"deployment lock descriptor could not be closed: {exc}")
        return errors

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lock clock must return a timezone-aware timestamp")
        return value

    def _now_not_before(self, previous: datetime) -> datetime:
        value = self._now()
        if value < previous:
            raise ValueError("lock clock moved backwards")
        return value

    def _contention_error(self, owner: LockOwnerMetadata | None) -> LockUnavailableError:
        detail = "Another DployDB operation holds the deployment lock."
        if owner is not None:
            detail += (
                f" Owner operation: {owner.operation_id}; PID: {owner.process.pid}; "
                f"host: {owner.process.hostname}."
            )
        return LockUnavailableError(
            self.secrets.redact_text(detail),
            production_changed=False,
            previous_application_running=None,
            log_path=self.secrets.redact_text(str(self.owner_path)),
            next_safe_action=(
                "Wait for the active operation to finish. If it does not, preserve the owner "
                "metadata and inspect operation state; do not delete the lock file."
            ),
        )

    def _safety_error(self, detail: str, path: Path) -> SafetyCheckError:
        return SafetyCheckError(
            self.secrets.redact_text(detail),
            production_changed=False,
            previous_application_running=None,
            log_path=self.secrets.redact_text(str(path)),
            next_safe_action="Correct the lock path safety problem before retrying.",
        )

    def _recovery_error(self, detail: str, path: Path) -> RecoveryRequiredError:
        return RecoveryRequiredError(
            self.secrets.redact_text(detail),
            production_changed=True,
            previous_application_running=None,
            log_path=self.secrets.redact_text(str(path)),
            next_safe_action=(
                "Preserve the lock metadata and inspect the recorded operation before retrying."
            ),
        )


def inspect_lock(state_directory: Path, *, secrets: SecretRegistry) -> LockInspection:
    """Return a read-only snapshot; PID metadata never substitutes for flock."""
    probe = DeploymentLock(state_directory, secrets=secrets)
    if state_directory.is_symlink():
        return _inspection_recovery(
            probe,
            probe._recovery_error("state directory is a symlink", state_directory),
        )
    if not state_directory.exists():
        return LockInspection(
            state=LockInspectionState.IDLE,
            lock_held=False,
            owner=None,
            metadata_error=None,
            lock_path=probe.lock_path,
            owner_path=probe.owner_path,
        )
    try:
        probe._validate_directory(state_directory)
    except DployDBError as error:
        return _inspection_recovery(probe, error)

    if not probe.lock_path.exists():
        if probe.lock_path.is_symlink():
            return _inspection_recovery(
                probe,
                probe._recovery_error("deployment lock path is a symlink", probe.lock_path),
            )
        return _classify_available_lock(probe)

    try:
        probe._validate_regular_file(probe.lock_path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(probe.lock_path, flags)
        probe._validate_lock_descriptor(descriptor)
    except (DployDBError, OSError) as exc:
        if isinstance(exc, DployDBError):
            inspection_error = exc
        else:
            inspection_error = probe._recovery_error(
                f"deployment lock could not be inspected safely: {exc}", probe.lock_path
            )
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        return _inspection_recovery(probe, inspection_error)

    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in _CONTENTION_ERRORS:
                return _inspection_recovery(
                    probe,
                    probe._recovery_error(f"deployment lock probe failed: {exc}", probe.lock_path),
                )
            try:
                owner = probe._read_owner_metadata()
                metadata_error = None
            except DployDBError as error:
                owner = None
                metadata_error = error.payload.what_failed
            return LockInspection(
                state=LockInspectionState.ACTIVE,
                lock_held=True,
                owner=owner,
                metadata_error=metadata_error,
                lock_path=probe.lock_path,
                owner_path=probe.owner_path,
            )

        return _classify_available_lock(probe)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass


def _classify_available_lock(probe: DeploymentLock) -> LockInspection:
    try:
        owner = probe._read_owner_metadata()
    except DployDBError as error:
        return _inspection_recovery(probe, error)
    state = (
        LockInspectionState.STALE_OWNER
        if owner is not None and owner.state is LockOwnerState.ACTIVE
        else LockInspectionState.IDLE
    )
    return LockInspection(
        state=state,
        lock_held=False,
        owner=owner,
        metadata_error=None,
        lock_path=probe.lock_path,
        owner_path=probe.owner_path,
    )


def _inspection_recovery(probe: DeploymentLock, error: DployDBError) -> LockInspection:
    return LockInspection(
        state=LockInspectionState.RECOVERY_REQUIRED,
        lock_held=False,
        owner=None,
        metadata_error=error.payload.what_failed,
        lock_path=probe.lock_path,
        owner_path=probe.owner_path,
    )
