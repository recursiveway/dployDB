"""Real-process Milestone 1 gate for abrupt termination plus status."""

from __future__ import annotations

import json
import multiprocessing
import signal
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from dploydb.cli import app
from dploydb.config import STARTER_CONFIGURATION
from dploydb.locking import DeploymentLock, LockInspectionState, inspect_lock
from dploydb.redaction import SecretRegistry
from dploydb.state import StateStore

FINGERPRINT = "b" * 64
runner = CliRunner()


def _interrupted_holder(
    state_text: str,
    project: str,
    secret: str,
    ready: Any,
) -> None:
    state = Path(state_text)
    secrets = SecretRegistry()
    secrets.register(secret)
    store = StateStore(state, secrets=secrets)
    operation = store.create_operation(
        operation_type="deploy",
        project=project,
        configuration_fingerprint=FINGERPRINT,
        evidence={"token": secret},
    )
    lock = DeploymentLock(state, secrets=secrets).acquire()
    lock.record_owner(operation_id=operation.operation_id, operation_type="deploy")
    ready.set()
    while True:
        signal.pause()


def _write_config(tmp_path: Path, secret: str) -> Path:
    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = "milestone-one-gate"
    value["state_directory"] = str(tmp_path / "state")
    value["database"]["path"] = str(tmp_path / "data" / "app.db")
    value["migration"]["command"] = ["python", "-c", "pass"]
    value["application"]["compose_file"] = str(tmp_path / "compose.yaml")
    value["application"].pop("smoke_command", None)
    value["application"]["test_mode_env"] = {"API_TOKEN": secret}
    value["backup"]["local_directory"] = str(tmp_path / "backups")
    for name in (
        "maintenance_on_command",
        "maintenance_off_command",
        "activate_new_command",
        "activate_old_command",
    ):
        value["traffic"][name] = ["python", "-c", "pass"]
    path = tmp_path / "dploydb.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def _stop_process(process: multiprocessing.Process) -> None:
    process.join(timeout=2)
    if process.is_alive():
        process.kill()
    process.join(timeout=10)


def test_sigkill_is_explained_by_read_only_redacted_status(tmp_path: Path) -> None:
    secret = "milestone-one-cross-sink-secret"
    config_path = _write_config(tmp_path, secret)
    state = tmp_path / "state"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    holder = context.Process(
        target=_interrupted_holder,
        args=(str(state), "milestone-one-gate", secret, ready),
    )

    try:
        holder.start()
        assert ready.wait(10), "holder did not durably record operation and lock ownership"
        active = inspect_lock(state, secrets=SecretRegistry())
        assert active.state is LockInspectionState.ACTIVE

        holder.kill()
        holder.join(timeout=10)
        assert not holder.is_alive()

        before = {
            path.relative_to(state): path.read_bytes()
            for path in state.rglob("*")
            if path.is_file()
        }
        interrupted = runner.invoke(
            app,
            ["status", "--config", str(config_path), "--json"],
        )
        after = {
            path.relative_to(state): path.read_bytes()
            for path in state.rglob("*")
            if path.is_file()
        }
    finally:
        _stop_process(holder)

    assert interrupted.exit_code == 60
    payload = json.loads(interrupted.output)
    assert payload["status"] == "interrupted"
    assert payload["lock"]["state"] == "stale_owner"
    assert payload["operation"]["status"] == "in_progress"
    assert payload["production_changed"] is False
    assert payload["previous_application_running"] is None
    assert payload["recovery_required"] is True
    assert before == after

    combined_files = b"".join(before.values())
    assert secret not in interrupted.output
    assert secret.encode() not in combined_files
