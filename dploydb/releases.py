"""Atomic deployment release manifests and active/previous selection."""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, cast
from uuid import uuid4

from pydantic import ValidationError

from dploydb.errors import OperationFailedError, SafetyCheckError, StateCorruptionError
from dploydb.models import (
    DeploymentState,
    FailureRecord,
    ProductionApplicationHandle,
    ReleaseHealthEvidence,
    ReleaseHookEvidence,
    ReleaseManifest,
    ReleasePointers,
    new_release_id,
    utc_now,
)
from dploydb.redaction import JsonValue, SecretRegistry

DIRECTORY_MODE: Final = 0o700
FILE_MODE: Final = 0o600
MAX_RELEASE_BYTES: Final = 1024 * 1024
POINTER_FILE_NAME: Final = "current.json"

_RELEASE_ID = re.compile(r"^release_[0-9a-f]{32}$")
_TERMINAL_STATES = frozenset(
    {
        DeploymentState.ACTIVE,
        DeploymentState.ROLLED_BACK,
        DeploymentState.FAILED_SAFE,
        DeploymentState.RECOVERY_REQUIRED,
    }
)
_TRANSITIONS: Final[dict[DeploymentState, frozenset[DeploymentState]]] = {
    DeploymentState.CREATED: frozenset(
        {
            DeploymentState.PREFLIGHT_PASSED,
            DeploymentState.FAILED_SAFE,
            DeploymentState.RECOVERY_REQUIRED,
        }
    ),
    DeploymentState.PREFLIGHT_PASSED: frozenset(
        {
            DeploymentState.SNAPSHOT_VERIFIED,
            DeploymentState.FAILED_SAFE,
            DeploymentState.RECOVERY_REQUIRED,
        }
    ),
    DeploymentState.SNAPSHOT_VERIFIED: frozenset(
        {
            DeploymentState.REHEARSAL_PASSED,
            DeploymentState.FAILED_SAFE,
            DeploymentState.RECOVERY_REQUIRED,
        }
    ),
    DeploymentState.REHEARSAL_PASSED: frozenset(
        {
            DeploymentState.CANDIDATE_HEALTHY,
            DeploymentState.FAILED_SAFE,
            DeploymentState.RECOVERY_REQUIRED,
        }
    ),
    DeploymentState.CANDIDATE_HEALTHY: frozenset(
        {
            DeploymentState.MAINTENANCE_ENABLED,
            DeploymentState.FAILED_SAFE,
            DeploymentState.RECOVERY_REQUIRED,
        }
    ),
    DeploymentState.MAINTENANCE_ENABLED: frozenset(
        {
            DeploymentState.CURRENT_APP_STOPPED,
            DeploymentState.ROLLBACK_STARTED,
            DeploymentState.RECOVERY_REQUIRED,
        }
    ),
    DeploymentState.CURRENT_APP_STOPPED: frozenset(
        {
            DeploymentState.FINAL_SNAPSHOT_VERIFIED,
            DeploymentState.ROLLBACK_STARTED,
            DeploymentState.RECOVERY_REQUIRED,
        }
    ),
    DeploymentState.FINAL_SNAPSHOT_VERIFIED: frozenset(
        {
            DeploymentState.PRODUCTION_MIGRATED,
            DeploymentState.ROLLBACK_STARTED,
            DeploymentState.RECOVERY_REQUIRED,
        }
    ),
    DeploymentState.PRODUCTION_MIGRATED: frozenset(
        {
            DeploymentState.NEW_APP_HEALTHY,
            DeploymentState.ROLLBACK_STARTED,
            DeploymentState.RECOVERY_REQUIRED,
        }
    ),
    DeploymentState.NEW_APP_HEALTHY: frozenset(
        {
            DeploymentState.TRAFFIC_ACTIVATED,
            DeploymentState.ROLLBACK_STARTED,
            DeploymentState.RECOVERY_REQUIRED,
        }
    ),
    DeploymentState.TRAFFIC_ACTIVATED: frozenset(
        {DeploymentState.ACTIVE, DeploymentState.RECOVERY_REQUIRED}
    ),
    DeploymentState.ROLLBACK_STARTED: frozenset(
        {DeploymentState.ROLLED_BACK, DeploymentState.RECOVERY_REQUIRED}
    ),
}
_UNSET = object()
_IGNORABLE_DIRECTORY_FSYNC_ERRORS = frozenset({errno.EINVAL, errno.ENOTSUP})


@dataclass(frozen=True, slots=True)
class ReleaseHistorySnapshot:
    """One strictly validated, read-only view of local release history."""

    releases: tuple[ReleaseManifest, ...]
    pointers: ReleasePointers | None

    def find(self, release_id: str) -> ReleaseManifest | None:
        """Return one release from this validated snapshot without filesystem access."""
        return next((item for item in self.releases if item.release_id == release_id), None)


class ReleaseStore:
    """Persist strict redacted release summaries without rewriting history."""

    def __init__(
        self,
        state_directory: Path,
        *,
        secrets: SecretRegistry,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not state_directory.is_absolute() or state_directory == Path("/"):
            raise ValueError("state directory must be an absolute non-root path")
        self.state_directory = state_directory
        self.releases_directory = state_directory / "releases"
        self.pointer_path = self.releases_directory / POINTER_FILE_NAME
        self.secrets = secrets
        self.clock = clock

    def create_release(
        self,
        *,
        operation_id: str,
        project: str,
        requested_version: str,
        configuration_fingerprint: str,
        operation_log_path: Path,
        previous_application: ProductionApplicationHandle | None = None,
    ) -> ReleaseManifest:
        """Create the first durable release record before deployment work starts."""
        self._ensure_layout()
        pointers = self.read_pointers()
        release_id = new_release_id()
        directory = self._release_directory(release_id)
        try:
            directory.mkdir(mode=DIRECTORY_MODE)
            self._validate_directory(directory)
        except OSError as exc:
            raise self._write_error(
                f"release directory could not be created: {exc}", directory
            ) from exc
        now = self._now()
        manifest = ReleaseManifest(
            recovery_protocol_version=2,
            release_id=release_id,
            operation_id=operation_id,
            project=self.secrets.redact_text(project),
            requested_version=requested_version,
            status=DeploymentState.CREATED,
            configuration_fingerprint=configuration_fingerprint,
            operation_log_path=operation_log_path,
            previous_release_id=(None if pointers is None else pointers.active_release_id),
            previous_application=previous_application,
            started_at=now,
            updated_at=now,
        )
        self._atomic_write(self._manifest_path(release_id), manifest)
        return self.read_manifest(release_id)

    def transition(
        self,
        release_id: str,
        *,
        status: DeploymentState,
        previous_application: ProductionApplicationHandle | None | object = _UNSET,
        new_application: ProductionApplicationHandle | None | object = _UNSET,
        rehearsal_backup_id: str | None | object = _UNSET,
        rehearsal_backup_sha256: str | None | object = _UNSET,
        final_backup_id: str | None | object = _UNSET,
        final_backup_sha256: str | None | object = _UNSET,
        production_health_passed: bool | object = _UNSET,
        production_changed: bool | object = _UNSET,
        traffic_activated: bool | object = _UNSET,
        traffic_hooks: tuple[ReleaseHookEvidence, ...] | object = _UNSET,
        health_checks: tuple[ReleaseHealthEvidence, ...] | object = _UNSET,
        failure: FailureRecord | None | object = _UNSET,
    ) -> ReleaseManifest:
        """Advance one release through an explicitly allowed durable transition."""
        current = self.read_manifest(release_id)
        if current.status in _TERMINAL_STATES:
            raise ValueError(f"terminal release cannot transition from {current.status.value}")
        allowed = _TRANSITIONS.get(current.status, frozenset())
        if status not in allowed:
            raise ValueError(
                f"release transition {current.status.value} -> {status.value} is not allowed"
            )
        now = self._now_not_before(current.updated_at)
        changes: dict[str, Any] = {
            "status": status,
            "updated_at": now,
            "completed_at": now if status in _TERMINAL_STATES else None,
        }
        supplied = {
            "previous_application": previous_application,
            "new_application": new_application,
            "rehearsal_backup_id": rehearsal_backup_id,
            "rehearsal_backup_sha256": rehearsal_backup_sha256,
            "final_backup_id": final_backup_id,
            "final_backup_sha256": final_backup_sha256,
            "production_health_passed": production_health_passed,
            "production_changed": production_changed,
            "traffic_activated": traffic_activated,
            "traffic_hooks": traffic_hooks,
            "health_checks": health_checks,
            "failure": failure,
        }
        changes.update({name: value for name, value in supplied.items() if value is not _UNSET})
        updated = current.model_copy(update=changes)
        persisted = ReleaseManifest.model_validate_json(updated.model_dump_json())
        self._atomic_write(self._manifest_path(release_id), persisted)
        return self.read_manifest(release_id)

    def record_recovery_intent(
        self,
        release_id: str,
        *,
        production_migration_started: bool = False,
        traffic_activation_attempted: bool = False,
    ) -> ReleaseManifest:
        """Durably mark a side-effect window before invoking the external action."""
        if not production_migration_started and not traffic_activation_attempted:
            raise ValueError("at least one recovery intent must be recorded")
        current = self.read_manifest(release_id)
        if current.status in _TERMINAL_STATES:
            raise ValueError("terminal release recovery intent is immutable")
        changes: dict[str, Any] = {"updated_at": self._now_not_before(current.updated_at)}
        if production_migration_started:
            changes["production_migration_started"] = True
        if traffic_activation_attempted:
            changes["traffic_activation_attempted"] = True
        updated = current.model_copy(update=changes)
        persisted = ReleaseManifest.model_validate_json(updated.model_dump_json())
        self._atomic_write(self._manifest_path(release_id), persisted)
        return self.read_manifest(release_id)

    def resolve_recovery(
        self,
        release_id: str,
        *,
        status: DeploymentState,
        recovery_operation_id: str,
        traffic_activated: bool,
    ) -> ReleaseManifest:
        """Resolve recovery-required state while preserving its original failure."""
        if status not in {DeploymentState.ACTIVE, DeploymentState.ROLLED_BACK}:
            raise ValueError("recovery can resolve only to active or rolled_back")
        current = self.read_manifest(release_id)
        if current.status is not DeploymentState.RECOVERY_REQUIRED or current.failure is None:
            raise ValueError("release is not recovery_required with preserved failure evidence")
        now = self._now_not_before(current.updated_at)
        changes: dict[str, Any] = {
            "status": status,
            "updated_at": now,
            "completed_at": now,
            "traffic_activated": traffic_activated,
            "recovery_operation_id": recovery_operation_id,
            "recovered_at": now,
            "recovery_failure": current.failure,
        }
        if status is DeploymentState.ACTIVE:
            changes["failure"] = None
        updated = current.model_copy(update=changes)
        persisted = ReleaseManifest.model_validate_json(updated.model_dump_json())
        self._atomic_write(self._manifest_path(release_id), persisted)
        return self.read_manifest(release_id)

    def read_manifest(self, release_id: str) -> ReleaseManifest:
        """Read one strict complete release manifest by opaque ID."""
        path = self._manifest_path(release_id)
        payload = self._read_private_file(path)
        try:
            manifest = ReleaseManifest.model_validate_json(payload)
        except ValidationError as exc:
            raise self._corruption_error(
                f"release manifest is invalid ({exc.error_count()} validation errors)", path
            ) from None
        if manifest.release_id != release_id:
            raise self._corruption_error("release manifest ID does not match its path", path)
        return manifest

    def read_pointers(self) -> ReleasePointers | None:
        """Read active/previous selection without creating state."""
        if not self.pointer_path.exists():
            if self.pointer_path.is_symlink():
                raise self._corruption_error("release pointer is a symlink", self.pointer_path)
            return None
        payload = self._read_private_file(self.pointer_path)
        try:
            pointers = ReleasePointers.model_validate_json(payload)
        except ValidationError as exc:
            raise self._corruption_error(
                f"release pointer is invalid ({exc.error_count()} validation errors)",
                self.pointer_path,
            ) from None
        active = self.read_manifest(pointers.active_release_id)
        if active.status is not DeploymentState.ACTIVE:
            raise self._corruption_error(
                "release pointer does not select an active manifest", self.pointer_path
            )
        if pointers.previous_release_id is not None:
            self.read_manifest(pointers.previous_release_id)
        return pointers

    def activate_release(self, release_id: str) -> ReleasePointers:
        """Atomically select a terminal active release and preserve the old selection."""
        self._ensure_layout()
        selected = self.read_manifest(release_id)
        if selected.status is not DeploymentState.ACTIVE:
            raise ValueError("only an active release manifest can become current")
        current = self.read_pointers()
        if current is not None and current.active_release_id == release_id:
            return current
        pointers = ReleasePointers(
            active_release_id=release_id,
            previous_release_id=(None if current is None else current.active_release_id),
            updated_at=self._now(),
        )
        self._atomic_write(self.pointer_path, pointers)
        reread = self.read_pointers()
        assert reread is not None
        return reread

    def active_release(self) -> ReleaseManifest | None:
        """Return the selected active manifest, if deployment history has one."""
        pointers = self.read_pointers()
        return None if pointers is None else self.read_manifest(pointers.active_release_id)

    def previous_release(self) -> ReleaseManifest | None:
        """Return the selected immediately previous manifest, if present."""
        pointers = self.read_pointers()
        if pointers is None or pointers.previous_release_id is None:
            return None
        return self.read_manifest(pointers.previous_release_id)

    def read_history(self) -> ReleaseHistorySnapshot:
        """Validate and return every release without creating or rewriting state."""
        if not self.state_directory.exists():
            self._reject_symlink(self.state_directory)
            return ReleaseHistorySnapshot(releases=(), pointers=None)
        self._validate_directory(self.state_directory)
        if not self.releases_directory.exists():
            self._reject_symlink(self.releases_directory)
            return ReleaseHistorySnapshot(releases=(), pointers=None)
        self._validate_directory(self.releases_directory)

        manifests: list[ReleaseManifest] = []
        for entry in self.releases_directory.iterdir():
            if entry.name == POINTER_FILE_NAME:
                if entry.is_symlink() or not entry.is_file():
                    raise self._corruption_error("release pointer is not a regular file", entry)
                continue
            if (
                entry.is_symlink()
                or not entry.is_dir()
                or _RELEASE_ID.fullmatch(entry.name) is None
            ):
                raise self._corruption_error(
                    f"releases directory contains an unexpected entry: {entry.name}", entry
                )
            self._validate_release_directory_contents(entry)
            manifests.append(self.read_manifest(entry.name))

        pointers = self.read_pointers()
        release_ids = {item.release_id for item in manifests}
        if pointers is not None:
            selected_ids = {pointers.active_release_id}
            if pointers.previous_release_id is not None:
                selected_ids.add(pointers.previous_release_id)
            if not selected_ids.issubset(release_ids):
                raise self._corruption_error(
                    "release pointers select a manifest missing from history", self.pointer_path
                )
        manifests.sort(key=lambda item: (item.started_at, item.release_id), reverse=True)
        return ReleaseHistorySnapshot(releases=tuple(manifests), pointers=pointers)

    def lookup_history_release(
        self, release_id: str
    ) -> tuple[ReleaseManifest, ReleasePointers | None]:
        """Resolve one user-selected release from a fully validated history snapshot."""
        try:
            self._validate_release_id(release_id)
        except (TypeError, ValueError):
            raise SafetyCheckError(
                "release ID is invalid",
                production_changed=False,
                previous_application_running=None,
                log_path=self.releases_directory,
                next_safe_action="Copy an exact release ID from dploydb releases.",
            ) from None
        history = self.read_history()
        selected = history.find(release_id)
        if selected is None:
            raise SafetyCheckError(
                f"release does not exist: {release_id}",
                production_changed=False,
                previous_application_running=None,
                log_path=self.releases_directory,
                next_safe_action="Run dploydb releases and select an existing release ID.",
            )
        return selected, history.pointers

    def _ensure_layout(self) -> None:
        self._ensure_directory(self.state_directory, parents=True)
        self._ensure_directory(self.releases_directory, parents=False)

    def _release_directory(self, release_id: str) -> Path:
        self._validate_release_id(release_id)
        return self.releases_directory / release_id

    def _manifest_path(self, release_id: str) -> Path:
        return self._release_directory(release_id) / "manifest.json"

    def _validate_release_directory_contents(self, directory: Path) -> None:
        self._validate_directory(directory)
        entries = tuple(directory.iterdir())
        expected = directory / "manifest.json"
        if len(entries) != 1 or entries[0] != expected:
            unexpected = next((entry for entry in entries if entry != expected), directory)
            raise self._corruption_error(
                "release directory must contain exactly one manifest", unexpected
            )

    def _validate_release_id(self, release_id: str) -> None:
        if not isinstance(release_id, str) or _RELEASE_ID.fullmatch(release_id) is None:
            raise ValueError("release ID is invalid or contains unsafe characters")

    def _ensure_directory(self, path: Path, *, parents: bool) -> None:
        self._reject_symlink(path)
        try:
            path.mkdir(mode=DIRECTORY_MODE, parents=parents, exist_ok=True)
        except OSError as exc:
            raise self._write_error(
                f"private release directory could not be created: {exc}", path
            ) from exc
        self._validate_directory(path)

    def _validate_directory(self, path: Path) -> None:
        self._reject_symlink(path)
        try:
            details = path.stat()
        except OSError as exc:
            raise self._corruption_error(f"release directory is unreadable: {exc}", path) from exc
        mode = stat.S_IMODE(details.st_mode)
        if not stat.S_ISDIR(details.st_mode) or mode != DIRECTORY_MODE:
            raise self._corruption_error(
                f"release directory must be mode-0700, found {mode:04o}", path
            )

    def _read_private_file(self, path: Path) -> bytes:
        abandoned = tuple(path.parent.glob(f".{path.name}.*.tmp"))
        if abandoned:
            raise self._corruption_error(
                "release state has an abandoned atomic-write temporary file",
                abandoned[0],
            )
        self._reject_symlink(path)
        try:
            details = path.stat()
            mode = stat.S_IMODE(details.st_mode)
            if not stat.S_ISREG(details.st_mode) or mode != FILE_MODE:
                raise OSError(f"release file must be mode-0600, found {mode:04o}")
            if details.st_size <= 0 or details.st_size > MAX_RELEASE_BYTES:
                raise OSError("release file has an invalid size")
            payload = path.read_bytes()
        except OSError as exc:
            raise self._corruption_error(
                f"release file is invalid or unreadable: {exc}", path
            ) from exc
        if not payload.endswith(b"\n"):
            raise self._corruption_error("release file is truncated", path)
        return payload

    def _atomic_write(self, path: Path, value: ReleaseManifest | ReleasePointers) -> None:
        self._reject_symlink(path)
        raw = cast(JsonValue, value.model_dump(mode="json"))
        redacted = self.secrets.redact(raw)
        model = type(value)
        persisted = model.model_validate_json(
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
        if len(payload) > MAX_RELEASE_BYTES:
            raise self._write_error("release record exceeds the durable size limit", path)
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        descriptor = -1
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, FILE_MODE)
            os.fchmod(descriptor, FILE_MODE)
            self._write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        except OSError as exc:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise self._write_error(
                f"release record could not be written atomically: {exc}", path
            ) from exc

    def _write_all(self, descriptor: int, payload: bytes) -> None:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("release write made no progress")
            written += count

    def _fsync_directory(self, directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            try:
                os.fsync(descriptor)
            except OSError as exc:
                if exc.errno not in _IGNORABLE_DIRECTORY_FSYNC_ERRORS:
                    raise
        finally:
            os.close(descriptor)

    def _reject_symlink(self, path: Path) -> None:
        if path.is_symlink():
            raise self._corruption_error("refusing symlinked release state", path)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("release-store clock must return an aware timestamp")
        return value

    def _now_not_before(self, previous: datetime) -> datetime:
        current = self._now()
        return previous if current < previous else current

    def _write_error(self, detail: str, path: Path) -> OperationFailedError:
        return OperationFailedError(
            self.secrets.redact_text(detail),
            production_changed=False,
            previous_application_running=None,
            log_path=path,
            next_safe_action="Preserve the release state and retry only after fixing storage.",
        )

    def _corruption_error(self, detail: str, path: Path) -> StateCorruptionError:
        return StateCorruptionError(
            self.secrets.redact_text(detail),
            production_changed=True,
            previous_application_running=None,
            log_path=path,
            next_safe_action=(
                "Do not guess the active release; preserve state and inspect the release "
                "manifest and operation log."
            ),
        )
