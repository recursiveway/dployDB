"""Milestone 1D atomic state and append-only event tests."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from dploydb.errors import StateCorruptionError
from dploydb.models import FailureRecord, OperationStatus, SafetyFacts
from dploydb.redaction import REDACTION_MARKER, SecretRegistry
from dploydb.state import DIRECTORY_MODE, FILE_MODE, MAX_EVENT_BYTES, StateStore

FINGERPRINT = "a" * 64


class ControlledClock:
    """Small deterministic UTC clock used by state tests."""

    def __init__(self) -> None:
        self.value = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(milliseconds=1)
        return result


@pytest.fixture
def clock() -> ControlledClock:
    return ControlledClock()


@pytest.fixture
def store(tmp_path: Path, clock: ControlledClock) -> StateStore:
    return StateStore(tmp_path / "state", secrets=SecretRegistry(), clock=clock)


def create_operation(store: StateStore, *, operation_type: str = "deploy") -> Any:
    return store.create_operation(
        operation_type=operation_type,
        project="example",
        configuration_fingerprint=FINGERPRINT,
    )


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def rewrite_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(FILE_MODE)


def test_create_operation_writes_private_strict_manifest_and_first_event(
    store: StateStore,
) -> None:
    manifest = create_operation(store)
    paths = store.operation_paths(manifest.operation_id)

    assert manifest.status is OperationStatus.IN_PROGRESS
    assert manifest.stage == "created"
    assert manifest.last_event_sequence == 1
    assert manifest.started_at.tzinfo is UTC
    assert file_mode(store.root) == DIRECTORY_MODE
    assert file_mode(store.operations_directory) == DIRECTORY_MODE
    assert file_mode(paths.directory) == DIRECTORY_MODE
    assert file_mode(paths.manifest) == FILE_MODE
    assert file_mode(paths.events) == FILE_MODE

    raw_manifest = json.loads(paths.manifest.read_text())
    assert set(raw_manifest) == {
        "completed_at",
        "configuration_fingerprint",
        "failure",
        "last_event_sequence",
        "operation_id",
        "operation_type",
        "process",
        "project",
        "safety",
        "schema_version",
        "stage",
        "started_at",
        "status",
        "updated_at",
    }
    assert raw_manifest["started_at"].endswith("Z")
    event = store.read_events(manifest.operation_id)[0]
    assert event.sequence == 1
    assert event.operation_id == manifest.operation_id
    assert event.status is manifest.status
    assert event.stage == manifest.stage
    consistent_manifest, consistent_events = store.read_operation(manifest.operation_id)
    assert consistent_manifest == manifest
    assert consistent_events == [event]


def test_append_event_preserves_existing_bytes_and_updates_manifest(store: StateStore) -> None:
    manifest = create_operation(store)
    paths = store.operation_paths(manifest.operation_id)
    prefix = paths.events.read_bytes()

    event = store.append_event(
        manifest.operation_id,
        message="Preflight evidence captured.",
        evidence={"checks": ["configuration", "paths"]},
    )

    content = paths.events.read_bytes()
    assert content.startswith(prefix)
    assert event.sequence == 2
    assert [item.sequence for item in store.read_events(manifest.operation_id)] == [1, 2]
    updated = store.read_manifest(manifest.operation_id)
    assert updated.last_event_sequence == 2
    assert updated.updated_at == event.timestamp


def test_valid_lifecycle_transitions_and_terminal_immutability(store: StateStore) -> None:
    manifest = create_operation(store)
    manifest = store.transition(
        manifest.operation_id,
        status=OperationStatus.IN_PROGRESS,
        stage="preflight_passed",
        message="Preflight passed.",
    )
    completed = store.transition(
        manifest.operation_id,
        status=OperationStatus.SUCCEEDED,
        stage="completed",
        message="Operation completed.",
    )

    assert completed.completed_at == completed.updated_at
    assert completed.failure is None
    with pytest.raises(ValueError, match="terminal operation"):
        store.transition(
            completed.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="another_stage",
            message="Invalid transition.",
        )
    with pytest.raises(ValueError, match="immutable"):
        store.append_event(completed.operation_id, message="Late evidence.")


def test_same_stage_transition_is_rejected_without_writing(store: StateStore) -> None:
    manifest = create_operation(store)
    paths = store.operation_paths(manifest.operation_id)
    before_manifest = paths.manifest.read_bytes()
    before_events = paths.events.read_bytes()

    with pytest.raises(ValueError, match="must change"):
        store.transition(
            manifest.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="created",
            message="No state change.",
        )

    assert paths.manifest.read_bytes() == before_manifest
    assert paths.events.read_bytes() == before_events


@pytest.mark.parametrize(
    ("status", "safety", "failure"),
    [
        (OperationStatus.FAILED_SAFE, SafetyFacts(), None),
        (
            OperationStatus.RECOVERY_REQUIRED,
            SafetyFacts(recovery_required=False),
            FailureRecord(
                error_code="cutover_failed",
                what_failed="cutover",
                next_safe_action="Inspect state.",
            ),
        ),
        (
            OperationStatus.SUCCEEDED,
            SafetyFacts(),
            FailureRecord(
                error_code="unexpected",
                what_failed="unexpected failure",
                next_safe_action="Inspect state.",
            ),
        ),
    ],
)
def test_invalid_terminal_invariants_are_rejected_before_append(
    store: StateStore,
    status: OperationStatus,
    safety: SafetyFacts,
    failure: FailureRecord | None,
) -> None:
    manifest = create_operation(store)
    paths = store.operation_paths(manifest.operation_id)
    before = paths.events.read_bytes()

    with pytest.raises(ValidationError):
        store.transition(
            manifest.operation_id,
            status=status,
            stage="terminal",
            message="Terminal transition.",
            safety=safety,
            failure=failure,
        )

    assert paths.events.read_bytes() == before


def test_failed_safe_and_recovery_required_records_are_valid(store: StateStore) -> None:
    failed = create_operation(store)
    failure = FailureRecord(
        error_code="candidate_failed",
        what_failed="candidate did not become healthy",
        log_path="candidate.log",
        next_safe_action="Correct the release and retry.",
    )
    failed = store.transition(
        failed.operation_id,
        status=OperationStatus.FAILED_SAFE,
        stage="candidate_failed",
        message="Candidate failed safely.",
        failure=failure,
    )
    assert failed.status is OperationStatus.FAILED_SAFE
    assert failed.safety.recovery_required is False

    recovering = create_operation(store)
    recovering = store.transition(
        recovering.operation_id,
        status=OperationStatus.RECOVERY_REQUIRED,
        stage="cutover_uncertain",
        message="Cutover state is uncertain.",
        safety=SafetyFacts(
            production_changed=True,
            previous_application_running=None,
            recovery_required=True,
        ),
        failure=failure,
    )
    assert recovering.status is OperationStatus.RECOVERY_REQUIRED
    assert recovering.safety.production_changed is True


def test_redacts_every_persisted_secret_boundary(tmp_path: Path, clock: ControlledClock) -> None:
    secret = "state-secret-value"
    registry = SecretRegistry()
    registry.register(secret)
    store = StateStore(tmp_path / "state", secrets=registry, clock=clock)
    manifest = store.create_operation(
        operation_type="deploy",
        project="example",
        configuration_fingerprint=FINGERPRINT,
        evidence={"token": secret, "nested": [f"Bearer {secret}"]},
    )
    store.append_event(
        manifest.operation_id,
        message=f"diagnostic {secret}",
        evidence={"url": f"https://user:{secret}@example.invalid/path"},
    )
    store.transition(
        manifest.operation_id,
        status=OperationStatus.FAILED_SAFE,
        stage="failed",
        message=f"failure output {secret}",
        failure=FailureRecord(
            error_code="failed",
            what_failed=f"command exposed {secret}",
            log_path=f"logs/{secret}.log",
            next_safe_action=f"remove {secret} and retry",
        ),
    )

    combined = b"".join(path.read_bytes() for path in store.root.rglob("*") if path.is_file())
    assert secret.encode() not in combined
    assert REDACTION_MARKER.encode() in combined


def test_event_size_limit_preserves_existing_state(store: StateStore) -> None:
    manifest = create_operation(store)
    paths = store.operation_paths(manifest.operation_id)
    before_manifest = paths.manifest.read_bytes()
    before_events = paths.events.read_bytes()

    with pytest.raises(ValueError, match=str(MAX_EVENT_BYTES)):
        store.append_event(
            manifest.operation_id,
            message="Oversized event.",
            evidence={"output": "x" * MAX_EVENT_BYTES},
        )

    assert paths.manifest.read_bytes() == before_manifest
    assert paths.events.read_bytes() == before_events


def test_manifest_file_sync_failure_preserves_previous_complete_manifest(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_operation(store)
    paths = store.operation_paths(manifest.operation_id)
    before = paths.manifest.read_bytes()
    real_fsync = os.fsync
    calls = 0

    def fail_second_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected manifest fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr("dploydb.state.os.fsync", fail_second_fsync)
    with pytest.raises(StateCorruptionError):
        store.transition(
            manifest.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="preflight",
            message="Preflight complete.",
        )

    assert paths.manifest.read_bytes() == before
    json.loads(paths.manifest.read_text())
    assert not list(paths.directory.glob(".manifest.json.*.tmp"))


def test_manifest_write_failure_preserves_previous_complete_manifest(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_operation(store)
    paths = store.operation_paths(manifest.operation_id)
    before = paths.manifest.read_bytes()
    real_fdopen = os.fdopen

    class FailingStream:
        def __init__(self, descriptor: int) -> None:
            self.stream = real_fdopen(descriptor, "wb", closefd=True)

        def __enter__(self) -> FailingStream:
            return self

        def __exit__(self, *_args: object) -> None:
            self.stream.close()

        def write(self, _payload: bytes) -> int:
            raise OSError("injected manifest write failure")

        def flush(self) -> None:
            self.stream.flush()

        def fileno(self) -> int:
            return self.stream.fileno()

    def fail_fdopen(descriptor: int, *_args: object, **_kwargs: object) -> FailingStream:
        return FailingStream(descriptor)

    monkeypatch.setattr("dploydb.state.os.fdopen", fail_fdopen)
    with pytest.raises(StateCorruptionError):
        store.transition(
            manifest.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="preflight",
            message="Preflight complete.",
        )

    assert paths.manifest.read_bytes() == before
    json.loads(paths.manifest.read_text())
    assert not list(paths.directory.glob(".manifest.json.*.tmp"))


def test_replace_failure_preserves_previous_manifest_and_exposes_disagreement(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_operation(store)
    paths = store.operation_paths(manifest.operation_id)
    before = paths.manifest.read_bytes()

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("dploydb.state.os.replace", fail_replace)
    with pytest.raises(StateCorruptionError):
        store.transition(
            manifest.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="preflight",
            message="Preflight complete.",
        )

    assert paths.manifest.read_bytes() == before
    json.loads(paths.manifest.read_text())
    assert not list(paths.directory.glob(".manifest.json.*.tmp"))
    with pytest.raises(StateCorruptionError, match="sequence disagree"):
        store.latest_operation()


def test_directory_sync_failure_leaves_new_complete_manifest(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = create_operation(store)

    def fail_directory_sync(_directory: Path) -> None:
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(store, "_fsync_directory", fail_directory_sync)
    with pytest.raises(StateCorruptionError):
        store.transition(
            manifest.operation_id,
            status=OperationStatus.IN_PROGRESS,
            stage="preflight",
            message="Preflight complete.",
        )

    updated = store.read_manifest(manifest.operation_id)
    assert updated.stage == "preflight"
    assert store.read_events(manifest.operation_id)[-1].stage == "preflight"


@pytest.mark.parametrize(
    "corruption",
    [
        "empty",
        "truncated",
        "malformed",
        "empty_record",
        "duplicate_sequence",
        "skipped_sequence",
        "reordered",
        "operation_id",
    ],
)
def test_event_corruption_is_recovery_required(store: StateStore, corruption: str) -> None:
    manifest = create_operation(store)
    store.append_event(manifest.operation_id, message="Second event.")
    store.append_event(manifest.operation_id, message="Third event.")
    path = store.operation_paths(manifest.operation_id).events
    lines = path.read_text().splitlines()
    if corruption == "empty":
        path.write_text("")
    elif corruption == "truncated":
        path.write_text("\n".join(lines)[:-1])
    elif corruption == "malformed":
        lines[1] = "{not-json}"
        path.write_text("\n".join(lines) + "\n")
    elif corruption == "empty_record":
        lines.insert(1, "")
        path.write_text("\n".join(lines) + "\n")
    elif corruption == "reordered":
        lines[1], lines[2] = lines[2], lines[1]
        path.write_text("\n".join(lines) + "\n")
    else:
        record = json.loads(lines[1])
        if corruption == "duplicate_sequence":
            record["sequence"] = 1
        elif corruption == "skipped_sequence":
            record["sequence"] = 3
        else:
            record["operation_id"] = "op_" + "f" * 32
        lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")
    path.chmod(FILE_MODE)

    with pytest.raises(StateCorruptionError):
        store.read_events(manifest.operation_id)


@pytest.mark.parametrize("corruption", ["truncated", "malformed", "unknown_field"])
def test_manifest_corruption_is_recovery_required(store: StateStore, corruption: str) -> None:
    manifest = create_operation(store)
    path = store.operation_paths(manifest.operation_id).manifest
    if corruption == "truncated":
        path.write_text(path.read_text()[:20])
    elif corruption == "malformed":
        path.write_text("{not-json}\n")
    else:
        raw = json.loads(path.read_text())
        raw["invented"] = True
        rewrite_json(path, raw)
    path.chmod(FILE_MODE)

    with pytest.raises(StateCorruptionError, match="manifest is invalid"):
        store.read_manifest(manifest.operation_id)


@pytest.mark.parametrize("filename", ["manifest.json", "events.jsonl"])
def test_overly_broad_state_file_permissions_are_recovery_required(
    store: StateStore, filename: str
) -> None:
    manifest = create_operation(store)
    path = store.operation_paths(manifest.operation_id).directory / filename
    path.chmod(0o640)

    with pytest.raises(StateCorruptionError, match="unsafe mode"):
        store.read_operation(manifest.operation_id)


def test_event_timestamp_regression_is_recovery_required(store: StateStore) -> None:
    manifest = create_operation(store)
    store.append_event(manifest.operation_id, message="Second event.")
    path = store.operation_paths(manifest.operation_id).events
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[1]["timestamp"] = "2020-01-01T00:00:00.000Z"
    path.write_text("".join(json.dumps(item) + "\n" for item in records))
    path.chmod(FILE_MODE)

    with pytest.raises(StateCorruptionError, match="timestamp regressed"):
        store.read_events(manifest.operation_id)


def test_manifest_event_disagreement_is_recovery_required(store: StateStore) -> None:
    manifest = create_operation(store)
    path = store.operation_paths(manifest.operation_id).manifest
    raw = json.loads(path.read_text())
    raw["stage"] = "contradictory"
    rewrite_json(path, raw)

    with pytest.raises(StateCorruptionError, match="state disagree"):
        store.latest_operation()


def test_abandoned_temporary_file_is_recovery_required(store: StateStore) -> None:
    manifest = create_operation(store)
    directory = store.operation_paths(manifest.operation_id).directory
    temporary = directory / (".manifest.json." + "b" * 32 + ".tmp")
    temporary.write_text("partial")
    temporary.chmod(FILE_MODE)

    with pytest.raises(StateCorruptionError, match="abandoned"):
        store.latest_operation()


def test_multiple_unfinished_operations_are_contradictory(store: StateStore) -> None:
    create_operation(store)
    create_operation(store, operation_type="backup")

    with pytest.raises(StateCorruptionError, match="multiple unfinished"):
        store.latest_operation()


def test_latest_operation_selects_newest_completed_record(store: StateStore) -> None:
    first = create_operation(store)
    store.transition(
        first.operation_id,
        status=OperationStatus.SUCCEEDED,
        stage="completed",
        message="First completed.",
    )
    second = create_operation(store, operation_type="backup")
    second = store.transition(
        second.operation_id,
        status=OperationStatus.SUCCEEDED,
        stage="completed",
        message="Second completed.",
    )

    files = [path for path in store.root.rglob("*") if path.is_file()]
    before = {path: (path.read_bytes(), file_mode(path)) for path in files}
    latest = store.latest_operation()
    assert latest is not None
    assert latest.operation_id == second.operation_id
    assert {path: (path.read_bytes(), file_mode(path)) for path in files} == before


@pytest.mark.parametrize("operation_id", ["../escape", "op_bad", "op_" + "A" * 32])
def test_unsafe_operation_ids_are_rejected(store: StateStore, operation_id: str) -> None:
    with pytest.raises(ValueError, match="operation ID"):
        store.operation_paths(operation_id)


def test_symlinked_state_root_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=DIRECTORY_MODE)
    link = tmp_path / "state"
    link.symlink_to(target, target_is_directory=True)
    store = StateStore(link, secrets=SecretRegistry())

    with pytest.raises(StateCorruptionError, match="symlinked"):
        store.ensure_layout()


def test_overly_broad_existing_state_permissions_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o750)
    root.chmod(0o750)
    store = StateStore(root, secrets=SecretRegistry())

    with pytest.raises(StateCorruptionError, match="mode-0700"):
        store.ensure_layout()
