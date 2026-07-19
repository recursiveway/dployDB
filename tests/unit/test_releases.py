"""Tests for atomic Milestone 5 deployment release state."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from dploydb.errors import OperationFailedError, StateCorruptionError
from dploydb.models import (
    DeploymentState,
    FailureRecord,
    ProductionApplicationHandle,
    ReleaseManifest,
)
from dploydb.redaction import REDACTION_MARKER, SecretRegistry
from dploydb.releases import ReleaseStore

FINGERPRINT = "a" * 64
OPERATION_ID = "op_" + "1" * 32
BACKUP_ID = "backup_" + "2" * 32
FINAL_BACKUP_ID = "backup_" + "3" * 32
SHA256 = "b" * 64
FINAL_SHA256 = "c" * 64


def bootstrap_handle(tmp_path: Path) -> ProductionApplicationHandle:
    return ProductionApplicationHandle(
        source="bootstrap",
        container_id="a" * 64,
        container_name="example-app-app-1",
        compose_project="example-app",
        compose_service="app",
        version=None,
        release_id=None,
        operation_id=None,
        database_directory=(tmp_path / "data").resolve(),
        database_target="/data",
        host_port=4510,
        container_port=8080,
        health_url="http://127.0.0.1:4510/health",
    )


def release_handle(
    tmp_path: Path,
    *,
    release_id: str,
    version: str,
    operation_id: str,
    suffix: str,
) -> ProductionApplicationHandle:
    return ProductionApplicationHandle(
        source="release",
        container_id=suffix * 64,
        container_name=f"dploydb-release-{suffix}",
        compose_project=f"dploydb-release-{suffix}",
        compose_service="app",
        version=version,
        release_id=release_id,
        operation_id=operation_id,
        database_directory=(tmp_path / "data").resolve(),
        database_target="/data",
        host_port=4510,
        container_port=8080,
        health_url="http://127.0.0.1:4510/health",
    )


def store(tmp_path: Path, *, secrets: SecretRegistry | None = None) -> ReleaseStore:
    return ReleaseStore(
        (tmp_path / "state").resolve(),
        secrets=secrets or SecretRegistry(),
    )


def create(
    selected: ReleaseStore,
    tmp_path: Path,
    *,
    version: str = "v2",
    operation_id: str = OPERATION_ID,
    previous_application: ProductionApplicationHandle | None = None,
):
    return selected.create_release(
        operation_id=operation_id,
        project="example-app",
        requested_version=version,
        configuration_fingerprint=FINGERPRINT,
        operation_log_path=(
            tmp_path / "state" / "operations" / operation_id / "events.jsonl"
        ).resolve(),
        previous_application=previous_application,
    )


def drive_active(
    selected: ReleaseStore,
    tmp_path: Path,
    manifest: ReleaseManifest,
) -> tuple[ReleaseManifest, ProductionApplicationHandle]:
    release_id = manifest.release_id
    previous = manifest.previous_application or bootstrap_handle(tmp_path)
    selected.transition(release_id, status=DeploymentState.PREFLIGHT_PASSED)
    selected.transition(
        release_id,
        status=DeploymentState.SNAPSHOT_VERIFIED,
        rehearsal_backup_id=BACKUP_ID,
        rehearsal_backup_sha256=SHA256,
    )
    selected.transition(release_id, status=DeploymentState.REHEARSAL_PASSED)
    selected.transition(release_id, status=DeploymentState.CANDIDATE_HEALTHY)
    selected.transition(
        release_id,
        status=DeploymentState.MAINTENANCE_ENABLED,
        previous_application=previous,
    )
    selected.transition(release_id, status=DeploymentState.CURRENT_APP_STOPPED)
    selected.transition(
        release_id,
        status=DeploymentState.FINAL_SNAPSHOT_VERIFIED,
        final_backup_id=FINAL_BACKUP_ID,
        final_backup_sha256=FINAL_SHA256,
    )
    selected.transition(
        release_id,
        status=DeploymentState.PRODUCTION_MIGRATED,
        production_changed=True,
    )
    new_application = release_handle(
        tmp_path,
        release_id=release_id,
        version=manifest.requested_version,
        operation_id=manifest.operation_id,
        suffix="d" if manifest.requested_version == "v2" else "e",
    )
    selected.transition(
        release_id,
        status=DeploymentState.NEW_APP_HEALTHY,
        new_application=new_application,
        production_health_passed=True,
    )
    selected.transition(
        release_id,
        status=DeploymentState.TRAFFIC_ACTIVATED,
        traffic_activated=True,
    )
    active = selected.transition(release_id, status=DeploymentState.ACTIVE)
    return active, new_application


def test_create_release_is_private_atomic_and_redacted(tmp_path: Path) -> None:
    secrets = SecretRegistry()
    secrets.register("top-secret")
    selected = store(tmp_path, secrets=secrets)

    manifest = selected.create_release(
        operation_id=OPERATION_ID,
        project="top-secret",
        requested_version="v2",
        configuration_fingerprint=FINGERPRINT,
        operation_log_path=(tmp_path / "top-secret" / "events.jsonl").resolve(),
    )

    path = selected.releases_directory / manifest.release_id / "manifest.json"
    assert stat.S_IMODE(selected.releases_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    persisted = path.read_text(encoding="utf-8")
    assert "top-secret" not in persisted
    assert REDACTION_MARKER in persisted
    assert manifest.status is DeploymentState.CREATED
    assert selected.read_pointers() is None


def test_release_transition_graph_and_terminal_immutability(tmp_path: Path) -> None:
    selected = store(tmp_path)
    manifest = create(selected, tmp_path, previous_application=bootstrap_handle(tmp_path))

    with pytest.raises(ValueError, match="not allowed"):
        selected.transition(manifest.release_id, status=DeploymentState.CANDIDATE_HEALTHY)

    active, _handle = drive_active(selected, tmp_path, manifest)

    assert active.status is DeploymentState.ACTIVE
    assert active.completed_at == active.updated_at
    assert active.production_changed is True
    assert active.traffic_activated is True
    with pytest.raises(ValueError, match="terminal release"):
        selected.transition(active.release_id, status=DeploymentState.RECOVERY_REQUIRED)


def test_active_and_previous_selection_are_atomic_and_explicit(tmp_path: Path) -> None:
    selected = store(tmp_path)
    first = create(selected, tmp_path, previous_application=bootstrap_handle(tmp_path))
    first_active, first_handle = drive_active(selected, tmp_path, first)
    first_pointers = selected.activate_release(first.release_id)

    second = create(
        selected,
        tmp_path,
        version="v3",
        operation_id="op_" + "4" * 32,
        previous_application=first_handle,
    )
    second_active, _second_handle = drive_active(selected, tmp_path, second)
    second_pointers = selected.activate_release(second.release_id)

    assert first_pointers.active_release_id == first_active.release_id
    assert first_pointers.previous_release_id is None
    assert second.previous_release_id == first_active.release_id
    assert second_pointers.active_release_id == second_active.release_id
    assert second_pointers.previous_release_id == first_active.release_id
    assert selected.active_release() == second_active
    assert selected.previous_release() == first_active


def test_failed_safe_release_requires_failure_and_preserves_production_fact(
    tmp_path: Path,
) -> None:
    selected = store(tmp_path)
    manifest = create(selected, tmp_path)
    failure = FailureRecord(
        error_code="operation_failed",
        what_failed="preflight failed",
        log_path=str(manifest.operation_log_path),
        next_safe_action="Correct the database and retry.",
    )

    failed = selected.transition(
        manifest.release_id,
        status=DeploymentState.FAILED_SAFE,
        failure=failure,
    )

    assert failed.completed_at is not None
    assert failed.production_changed is False
    assert failed.traffic_activated is False
    assert failed.failure == failure


@pytest.mark.parametrize(
    "updates",
    (
        {"rehearsal_backup_id": BACKUP_ID},
        {"final_backup_sha256": FINAL_SHA256},
        {"traffic_activated": True},
    ),
)
def test_release_model_rejects_contradictory_evidence(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "release_id": "release_" + "5" * 32,
        "operation_id": OPERATION_ID,
        "project": "example-app",
        "requested_version": "v2",
        "status": DeploymentState.CREATED,
        "configuration_fingerprint": FINGERPRINT,
        "operation_log_path": (tmp_path / "events.jsonl").resolve(),
        "started_at": now,
        "updated_at": now,
    }
    base.update(updates)

    with pytest.raises(ValidationError):
        ReleaseManifest.model_validate(base)


def test_atomic_replace_failure_preserves_previous_complete_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = store(tmp_path)
    manifest = create(selected, tmp_path)
    before = selected.read_manifest(manifest.release_id)

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OperationFailedError, match="injected replace failure"):
        selected.transition(manifest.release_id, status=DeploymentState.PREFLIGHT_PASSED)

    assert selected.read_manifest(manifest.release_id) == before
    assert not list((selected.releases_directory / manifest.release_id).glob("*.tmp"))


def test_truncated_or_wrong_mode_release_state_requires_recovery(tmp_path: Path) -> None:
    selected = store(tmp_path)
    manifest = create(selected, tmp_path)
    path = selected.releases_directory / manifest.release_id / "manifest.json"

    path.write_text(json.dumps({"release_id": manifest.release_id}), encoding="utf-8")
    with pytest.raises(StateCorruptionError, match="truncated"):
        selected.read_manifest(manifest.release_id)

    path.write_bytes(manifest.model_dump_json().encode() + b"\n")
    path.chmod(0o644)
    with pytest.raises(StateCorruptionError, match="mode-0600"):
        selected.read_manifest(manifest.release_id)


def test_release_store_rejects_naive_or_decreasing_clock(tmp_path: Path) -> None:
    moments = iter(
        (
            datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
            datetime(2026, 7, 19, 9, 0, tzinfo=UTC),
        )
    )
    selected = ReleaseStore(
        (tmp_path / "state").resolve(),
        secrets=SecretRegistry(),
        clock=lambda: next(moments),
    )
    manifest = create(selected, tmp_path)

    updated = selected.transition(manifest.release_id, status=DeploymentState.PREFLIGHT_PASSED)

    assert updated.updated_at == manifest.updated_at

    naive = ReleaseStore(
        (tmp_path / "other-state").resolve(),
        secrets=SecretRegistry(),
        clock=lambda: datetime.now() + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="aware"):
        create(naive, tmp_path, operation_id="op_" + "6" * 32)
