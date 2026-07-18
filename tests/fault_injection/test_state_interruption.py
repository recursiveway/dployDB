"""Real-process interruption checks for atomic operation manifests."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dploydb.errors import StateCorruptionError
from dploydb.redaction import SecretRegistry
from dploydb.state import StateStore

FINGERPRINT = "c" * 64


@pytest.mark.parametrize("interruption_point", ["before_replace", "after_replace"])
def test_killed_writer_leaves_previous_or_new_complete_manifest(
    tmp_path: Path, interruption_point: str
) -> None:
    root = tmp_path / interruption_point / "state"
    store = StateStore(root, secrets=SecretRegistry())
    manifest = store.create_operation(
        operation_type="deploy",
        project="example",
        configuration_fingerprint=FINGERPRINT,
    )
    paths = store.operation_paths(manifest.operation_id)
    old_raw = json.loads(paths.manifest.read_text())
    sentinel = tmp_path / interruption_point / "writer-ready"
    repository = Path(__file__).resolve().parents[2]
    script = r"""
import os
import sys
import time
from pathlib import Path

from dploydb.models import OperationStatus
from dploydb.redaction import SecretRegistry
from dploydb.state import StateStore

root = Path(sys.argv[1])
operation_id = sys.argv[2]
sentinel = Path(sys.argv[3])
point = sys.argv[4]
store = StateStore(root, secrets=SecretRegistry())

def block_forever() -> None:
    sentinel.write_text("ready", encoding="utf-8")
    while True:
        time.sleep(1)

if point == "before_replace":
    def blocked_replace(source: Path, destination: Path) -> None:
        block_forever()
    os.replace = blocked_replace
else:
    def blocked_directory_sync(directory: Path) -> None:
        block_forever()
    store._fsync_directory = blocked_directory_sync

store.transition(
    operation_id,
    status=OperationStatus.IN_PROGRESS,
    stage="preflight",
    message="Preflight completed.",
)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(root),
            manifest.operation_id,
            str(sentinel),
            interruption_point,
        ],
        cwd=repository,
    )
    try:
        deadline = time.monotonic() + 10
        while not sentinel.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sentinel.exists(), "child did not reach the requested atomic-write boundary"
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    durable_raw = json.loads(paths.manifest.read_text())
    assert durable_raw["stage"] in {old_raw["stage"], "preflight"}
    assert durable_raw["operation_id"] == manifest.operation_id
    parsed = store.read_manifest(manifest.operation_id)
    assert parsed.stage == durable_raw["stage"]

    if interruption_point == "before_replace":
        assert parsed.stage == old_raw["stage"]
        with pytest.raises(StateCorruptionError):
            store.latest_operation()
    else:
        assert parsed.stage == "preflight"
        assert store.latest_operation() is not None
