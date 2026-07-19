"""Atomic operation manifests and append-only recovery events."""

from __future__ import annotations

import errno
import json
import os
import re
import socket
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, cast
from uuid import uuid4

from pydantic import ValidationError

from dploydb.errors import OperationFailedError, StateCorruptionError
from dploydb.models import (
    FailureRecord,
    OperationEvent,
    OperationManifest,
    OperationStatus,
    ProcessIdentity,
    SafetyFacts,
    new_operation_id,
    utc_now,
)
from dploydb.redaction import JsonValue, SecretRegistry

DIRECTORY_MODE: Final = 0o700
FILE_MODE: Final = 0o600
MAX_EVENT_BYTES: Final = 1024 * 1024

_OPERATION_ID = re.compile(r"^op_[0-9a-f]{32}$")
_TEMP_FILE = re.compile(r"^\.manifest\.json\.[0-9a-f]{32}\.tmp$")
_IGNORABLE_DIRECTORY_FSYNC_ERRORS = frozenset({errno.EINVAL, errno.ENOTSUP})


@dataclass(frozen=True, slots=True)
class OperationPaths:
    """Filesystem paths owned by one operation."""

    directory: Path
    manifest: Path
    events: Path


class StateStore:
    """Persist operation evidence without deleting or silently repairing it."""

    def __init__(
        self,
        root: Path,
        *,
        secrets: SecretRegistry,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("state directory must be absolute")
        self.root = root
        self.secrets = secrets
        self.clock = clock
        self.operations_directory = root / "operations"

    def ensure_layout(self) -> None:
        """Create the private state layout and validate managed permissions."""
        self._ensure_private_directory(self.root, create_parents=True)
        self._ensure_private_directory(self.operations_directory, create_parents=False)

    def operation_paths(self, operation_id: str) -> OperationPaths:
        """Return safe paths for one syntactically valid operation ID."""
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise ValueError("operation ID is invalid or contains unsafe characters")
        directory = self.operations_directory / operation_id
        self._require_descendant(directory)
        return OperationPaths(
            directory=directory,
            manifest=directory / "manifest.json",
            events=directory / "events.jsonl",
        )

    def create_operation(
        self,
        *,
        operation_type: str,
        project: str,
        configuration_fingerprint: str,
        stage: str = "created",
        process: ProcessIdentity | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> OperationManifest:
        """Create durable in-progress evidence before external work begins."""
        self.ensure_layout()
        operation_id = str(new_operation_id())
        paths = self.operation_paths(operation_id)
        try:
            paths.directory.mkdir(mode=DIRECTORY_MODE)
        except OSError as exc:
            raise self._write_error(
                f"operation directory could not be created: {paths.directory}", paths.directory
            ) from exc
        self._validate_private_directory(paths.directory)

        now = self._now()
        identity = process or ProcessIdentity(pid=os.getpid(), hostname=socket.gethostname())
        event = OperationEvent(
            sequence=1,
            timestamp=now,
            operation_id=operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage=stage,
            message="Operation started.",
            evidence=self._redact_mapping(evidence),
        )
        manifest = OperationManifest(
            operation_id=operation_id,
            operation_type=operation_type,
            project=project,
            status=OperationStatus.IN_PROGRESS,
            stage=stage,
            configuration_fingerprint=configuration_fingerprint,
            process=identity,
            safety=SafetyFacts(),
            started_at=now,
            updated_at=now,
            last_event_sequence=1,
        )

        self._append_event_record(paths, event)
        try:
            self._atomic_write_manifest(paths.manifest, manifest)
        except OperationFailedError as exc:
            raise self._corruption_error(
                "operation creation was interrupted after its first event was recorded",
                paths.events,
            ) from exc
        return self.read_manifest(operation_id)

    def transition(
        self,
        operation_id: str,
        *,
        status: OperationStatus,
        stage: str,
        message: str,
        evidence: Mapping[str, Any] | None = None,
        safety: SafetyFacts | None = None,
        failure: FailureRecord | None = None,
    ) -> OperationManifest:
        """Append and persist one valid operation lifecycle transition."""
        current, events = self._read_consistent(operation_id)
        if current.status is not OperationStatus.IN_PROGRESS:
            raise ValueError(
                f"terminal operation {operation_id} cannot transition from {current.status.value}"
            )
        if status is OperationStatus.IN_PROGRESS and stage == current.stage:
            raise ValueError("in-progress transition must change the operation stage")

        now = self._now_not_before(current.updated_at)
        next_safety = safety or current.safety
        redacted_failure = self._redact_failure(failure)
        completed_at = None if status is OperationStatus.IN_PROGRESS else now
        updated = current.model_copy(
            update={
                "status": status,
                "stage": stage,
                "safety": next_safety,
                "updated_at": now,
                "completed_at": completed_at,
                "failure": redacted_failure,
                "last_event_sequence": current.last_event_sequence + 1,
            }
        )
        updated = OperationManifest.model_validate_json(updated.model_dump_json())
        event = OperationEvent(
            sequence=updated.last_event_sequence,
            timestamp=now,
            operation_id=operation_id,
            status=status,
            stage=stage,
            message=self.secrets.redact_text(message),
            evidence=self._redact_mapping(evidence),
        )
        paths = self.operation_paths(operation_id)
        self._append_event_record(paths, event, expected_existing=len(events))
        try:
            self._atomic_write_manifest(paths.manifest, updated)
        except OperationFailedError as exc:
            raise self._corruption_error(
                "operation transition event was durable but its manifest was not updated",
                paths.manifest,
                current,
            ) from exc
        return self.read_manifest(operation_id)

    def append_event(
        self,
        operation_id: str,
        *,
        message: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> OperationEvent:
        """Append same-stage evidence to an unfinished operation."""
        current, events = self._read_consistent(operation_id)
        if current.status is not OperationStatus.IN_PROGRESS:
            raise ValueError("terminal operations are immutable")
        now = self._now_not_before(current.updated_at)
        event = OperationEvent(
            sequence=current.last_event_sequence + 1,
            timestamp=now,
            operation_id=operation_id,
            status=current.status,
            stage=current.stage,
            message=self.secrets.redact_text(message),
            evidence=self._redact_mapping(evidence),
        )
        paths = self.operation_paths(operation_id)
        self._append_event_record(paths, event, expected_existing=len(events))
        updated = current.model_copy(
            update={"updated_at": now, "last_event_sequence": event.sequence}
        )
        updated = OperationManifest.model_validate_json(updated.model_dump_json())
        try:
            self._atomic_write_manifest(paths.manifest, updated)
        except OperationFailedError as exc:
            raise self._corruption_error(
                "operation evidence was durable but its manifest was not updated",
                paths.manifest,
                current,
            ) from exc
        return self.read_events(operation_id)[-1]

    def read_manifest(self, operation_id: str) -> OperationManifest:
        """Read one complete strict operation manifest."""
        path = self.operation_paths(operation_id).manifest
        data = self._read_private_file(path)
        try:
            manifest = OperationManifest.model_validate_json(data)
        except ValidationError as exc:
            raise self._corruption_error(
                f"operation manifest is invalid ({exc.error_count()} validation errors)", path
            ) from None
        if manifest.operation_id != operation_id:
            raise self._corruption_error("operation manifest ID does not match its path", path)
        return manifest

    def read_operation(self, operation_id: str) -> tuple[OperationManifest, list[OperationEvent]]:
        """Read a manifest and event trail only when they agree completely."""
        return self._read_consistent(operation_id)

    def read_events(self, operation_id: str) -> list[OperationEvent]:
        """Read an event trail without repairing malformed evidence."""
        path = self.operation_paths(operation_id).events
        data = self._read_private_file(path)
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise self._corruption_error("operation event log is not valid UTF-8", path) from None
        if not text or not text.endswith("\n"):
            raise self._corruption_error("operation event log is empty or truncated", path)

        events: list[OperationEvent] = []
        previous_timestamp: datetime | None = None
        for sequence, line in enumerate(text.splitlines(), start=1):
            if not line:
                raise self._corruption_error("operation event log contains an empty record", path)
            if len(line.encode("utf-8")) + 1 > MAX_EVENT_BYTES:
                raise self._corruption_error(
                    f"operation event {sequence} exceeds the maximum record size", path
                )
            try:
                event = OperationEvent.model_validate_json(line)
            except ValidationError as exc:
                raise self._corruption_error(
                    f"operation event {sequence} is invalid "
                    f"({exc.error_count()} validation errors)",
                    path,
                ) from None
            if event.sequence != sequence:
                raise self._corruption_error(
                    f"operation event sequence is invalid at record {sequence}", path
                )
            if event.operation_id != operation_id:
                raise self._corruption_error(
                    f"operation event {sequence} belongs to another operation", path
                )
            if previous_timestamp is not None and event.timestamp < previous_timestamp:
                raise self._corruption_error(
                    f"operation event timestamp regressed at record {sequence}", path
                )
            previous_timestamp = event.timestamp
            events.append(event)
        return events

    def latest_operation(self) -> OperationManifest | None:
        """Return the newest valid operation after validating all durable evidence."""
        operations = list(self.list_operations())
        unfinished = [item for item in operations if item.status is OperationStatus.IN_PROGRESS]
        if len(unfinished) > 1:
            raise self._corruption_error(
                "multiple unfinished operations make the active state contradictory",
                self.operations_directory,
            )
        if not operations:
            return None
        return max(operations, key=lambda item: (item.started_at, item.operation_id))

    def list_operations(self) -> tuple[OperationManifest, ...]:
        """Return every valid operation newest first without changing state."""
        if not self.root.exists():
            return ()
        self._validate_private_directory(self.root)
        if not self.operations_directory.exists():
            return ()
        self._validate_private_directory(self.operations_directory)

        operations: list[OperationManifest] = []
        for entry in self.operations_directory.iterdir():
            if (
                entry.is_symlink()
                or not entry.is_dir()
                or _OPERATION_ID.fullmatch(entry.name) is None
            ):
                raise self._corruption_error(
                    f"operations directory contains an unexpected entry: {entry.name}", entry
                )
            manifest, _ = self._read_consistent(entry.name)
            operations.append(manifest)

        operations.sort(key=lambda item: (item.started_at, item.operation_id), reverse=True)
        return tuple(operations)

    def _read_consistent(self, operation_id: str) -> tuple[OperationManifest, list[OperationEvent]]:
        paths = self.operation_paths(operation_id)
        self._validate_private_directory(paths.directory)
        leftovers = [
            entry for entry in paths.directory.iterdir() if _TEMP_FILE.fullmatch(entry.name)
        ]
        if leftovers:
            raise self._corruption_error(
                "operation contains an abandoned atomic-write temporary file", leftovers[0]
            )
        expected_names = {paths.manifest.name, paths.events.name}
        unexpected = [
            entry for entry in paths.directory.iterdir() if entry.name not in expected_names
        ]
        if unexpected:
            raise self._corruption_error(
                f"operation directory contains an unexpected entry: {unexpected[0].name}",
                unexpected[0],
            )

        manifest = self.read_manifest(operation_id)
        events = self.read_events(operation_id)
        final = events[-1]
        if manifest.last_event_sequence != len(events):
            raise self._corruption_error(
                "manifest and event sequence disagree", paths.events, manifest
            )
        if final.status is not manifest.status or final.stage != manifest.stage:
            raise self._corruption_error(
                "manifest and final event state disagree", paths.events, manifest
            )
        if final.timestamp != manifest.updated_at:
            raise self._corruption_error(
                "manifest and final event timestamps disagree", paths.events, manifest
            )
        return manifest, events

    def _atomic_write_manifest(self, path: Path, manifest: OperationManifest) -> None:
        payload = self._json_bytes(manifest.model_dump(mode="json")) + b"\n"
        temporary = path.parent / f".manifest.json.{uuid4().hex}.tmp"
        descriptor = -1
        replaced = False
        try:
            self._reject_symlink(path)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, FILE_MODE)
            os.fchmod(descriptor, FILE_MODE)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                written = stream.write(payload)
                if written != len(payload):
                    raise OSError("short manifest write")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            replaced = True
            self._fsync_directory(path.parent)
        except OSError as exc:
            detail = (
                f"manifest replacement completed but directory sync failed: {path}"
                if replaced
                else f"manifest could not be written atomically: {path}"
            )
            raise self._write_error(detail, path) from exc
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

    def _append_event_record(
        self,
        paths: OperationPaths,
        event: OperationEvent,
        *,
        expected_existing: int = 0,
    ) -> None:
        if paths.events.exists():
            existing = self.read_events(event.operation_id)
            if len(existing) != expected_existing:
                raise self._corruption_error(
                    "event log changed while preparing an append", paths.events
                )
        elif expected_existing:
            raise self._corruption_error("operation event log disappeared", paths.events)

        payload = self._json_bytes(event.model_dump(mode="json")) + b"\n"
        if len(payload) > MAX_EVENT_BYTES:
            raise ValueError(f"serialized operation event exceeds {MAX_EVENT_BYTES} bytes")
        self._reject_symlink(paths.events)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        existed = paths.events.exists()
        try:
            descriptor = os.open(paths.events, flags, FILE_MODE)
            os.fchmod(descriptor, FILE_MODE)
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("short event-log write")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if not existed:
                self._fsync_directory(paths.events.parent)
        except OSError as exc:
            raise self._corruption_error(
                f"operation event could not be appended durably: {exc}", paths.events
            ) from exc
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _read_private_file(self, path: Path) -> bytes:
        self._reject_symlink(path)
        try:
            details = path.stat()
            if not stat.S_ISREG(details.st_mode):
                raise OSError("not a regular file")
            if stat.S_IMODE(details.st_mode) != FILE_MODE:
                raise OSError(f"unsafe mode {stat.S_IMODE(details.st_mode):04o}")
            return path.read_bytes()
        except OSError as exc:
            raise self._corruption_error(
                f"state file is missing, unreadable, or unsafe: {exc}", path
            ) from None

    def _ensure_private_directory(self, path: Path, *, create_parents: bool) -> None:
        self._reject_symlink(path)
        try:
            path.mkdir(mode=DIRECTORY_MODE, parents=create_parents, exist_ok=True)
        except OSError as exc:
            raise self._write_error(f"state directory could not be created: {path}", path) from exc
        self._validate_private_directory(path)

    def _validate_private_directory(self, path: Path) -> None:
        self._reject_symlink(path)
        try:
            details = path.stat()
        except OSError:
            raise self._corruption_error("state directory is missing or unreadable", path) from None
        mode = stat.S_IMODE(details.st_mode)
        if not stat.S_ISDIR(details.st_mode) or mode != DIRECTORY_MODE:
            raise self._corruption_error(
                f"state directory must be a mode-0700 directory, found {mode:04o}", path
            )

    def _require_descendant(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError:
            raise ValueError("state path is outside the configured root") from None

    def _reject_symlink(self, path: Path) -> None:
        try:
            if path.is_symlink():
                raise self._corruption_error("refusing symlinked managed state path", path)
        except OSError:
            raise self._corruption_error(
                "managed state path could not be inspected", path
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

    def _json_bytes(self, value: object) -> bytes:
        redacted = self.secrets.redact(cast(JsonValue, value))
        try:
            return json.dumps(
                redacted,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"state evidence is not JSON-compatible: {exc}") from None

    def _redact_mapping(self, value: Mapping[str, Any] | None) -> dict[str, Any]:
        source = {} if value is None else dict(value)
        redacted = self.secrets.redact(cast(JsonValue, source))
        if not isinstance(redacted, dict):
            raise TypeError("redacted evidence must remain a mapping")
        return cast(dict[str, Any], redacted)

    def _redact_failure(self, failure: FailureRecord | None) -> FailureRecord | None:
        if failure is None:
            return None
        redacted = self.secrets.redact(cast(JsonValue, failure.model_dump(mode="json")))
        return FailureRecord.model_validate(redacted)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("state clock must return a timezone-aware timestamp")
        return value

    def _now_not_before(self, previous: datetime) -> datetime:
        value = self._now()
        if value < previous:
            raise ValueError("state clock moved backwards")
        return value

    def _write_error(self, detail: str, path: Path) -> OperationFailedError:
        return OperationFailedError(
            self.secrets.redact_text(detail),
            production_changed=False,
            previous_application_running=None,
            log_path=self.secrets.redact_text(str(path)),
            next_safe_action="Inspect the state path and retry only after correcting the cause.",
        )

    def _corruption_error(
        self,
        detail: str,
        path: Path,
        manifest: OperationManifest | None = None,
    ) -> StateCorruptionError:
        safety = manifest.safety if manifest is not None else None
        return StateCorruptionError(
            self.secrets.redact_text(detail),
            production_changed=True if safety is None else safety.production_changed,
            previous_application_running=(
                None if safety is None else safety.previous_application_running
            ),
            log_path=self.secrets.redact_text(str(path)),
            next_safe_action=(
                "Preserve the state files and inspect the recorded operation before any retry."
            ),
        )
