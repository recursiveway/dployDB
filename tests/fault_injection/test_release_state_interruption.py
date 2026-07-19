"""Real-process interruption checks for atomic release manifests."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dploydb.errors import StateCorruptionError
from dploydb.models import DeploymentState
from dploydb.redaction import SecretRegistry
from dploydb.releases import ReleaseStore

FINGERPRINT = "f" * 64
OPERATION_ID = "op_" + "7" * 32


@pytest.mark.parametrize("interruption_point", ["before_replace", "after_replace"])
def test_killed_release_writer_leaves_complete_state_and_interruption_evidence(
    tmp_path: Path,
    interruption_point: str,
) -> None:
    root = (tmp_path / interruption_point / "state").resolve()
    store = ReleaseStore(root, secrets=SecretRegistry())
    manifest = store.create_release(
        operation_id=OPERATION_ID,
        project="example",
        requested_version="v2",
        configuration_fingerprint=FINGERPRINT,
        operation_log_path=(root / "operations" / OPERATION_ID / "events.jsonl"),
    )
    path = store.releases_directory / manifest.release_id / "manifest.json"
    old_raw = json.loads(path.read_text(encoding="utf-8"))
    sentinel = tmp_path / interruption_point / "writer-ready"
    repository = Path(__file__).resolve().parents[2]
    script = r"""
import os
import sys
import time
from pathlib import Path

from dploydb.models import DeploymentState
from dploydb.redaction import SecretRegistry
from dploydb.releases import ReleaseStore

root = Path(sys.argv[1])
release_id = sys.argv[2]
sentinel = Path(sys.argv[3])
point = sys.argv[4]
store = ReleaseStore(root, secrets=SecretRegistry())

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

store.transition(release_id, status=DeploymentState.PREFLIGHT_PASSED)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(root),
            manifest.release_id,
            str(sentinel),
            interruption_point,
        ],
        cwd=repository,
    )
    try:
        deadline = time.monotonic() + 10
        while not sentinel.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sentinel.exists(), "child did not reach the requested release-write boundary"
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    durable_raw = json.loads(path.read_text(encoding="utf-8"))
    assert durable_raw["status"] in {
        old_raw["status"],
        DeploymentState.PREFLIGHT_PASSED.value,
    }
    assert durable_raw["release_id"] == manifest.release_id

    if interruption_point == "before_replace":
        assert durable_raw["status"] == old_raw["status"]
        with pytest.raises(StateCorruptionError, match="abandoned atomic-write"):
            store.read_manifest(manifest.release_id)
    else:
        parsed = store.read_manifest(manifest.release_id)
        assert parsed.status is DeploymentState.PREFLIGHT_PASSED
