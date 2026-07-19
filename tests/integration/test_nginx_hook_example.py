"""Executable acceptance tests for the documented fixed-port Nginx hooks."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

from dploydb.config import load_configuration

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "examples" / "nginx" / "dploydb-hook.py"
EXAMPLE_CONFIG = ROOT / "examples" / "nginx" / "dploydb.yaml"


def _run(state: Path, marker: Path, action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(HOOK),
            "--state-file",
            str(state),
            "--maintenance-file",
            str(marker),
            action,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_hooks_are_idempotent_atomic_and_require_blocked_traffic(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    run_directory = tmp_path / "run"
    state_directory.mkdir()
    run_directory.mkdir()
    state = state_directory / "target.json"
    marker = run_directory / "maintenance"

    refused = _run(state, marker, "activate-new")
    assert refused.returncode == 2
    assert "only while maintenance mode is enabled" in refused.stderr
    assert not state.exists()

    enabled = _run(state, marker, "maintenance-on")
    repeated_enabled = _run(state, marker, "maintenance-on")
    assert enabled.returncode == repeated_enabled.returncode == 0
    assert json.loads(enabled.stdout) == {
        "action": "maintenance-on",
        "maintenance": True,
        "ok": True,
        "target": None,
    }
    assert stat.S_IMODE(marker.stat().st_mode) == 0o644

    activated = _run(state, marker, "activate-new")
    assert activated.returncode == 0
    assert json.loads(activated.stdout)["target"] == "new"
    assert json.loads(state.read_text())["target"] == "new"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert not list(state_directory.glob(".*.tmp"))

    old = _run(state, marker, "activate-old")
    assert old.returncode == 0
    assert json.loads(state.read_text())["target"] == "old"

    disabled = _run(state, marker, "maintenance-off")
    repeated_disabled = _run(state, marker, "maintenance-off")
    assert disabled.returncode == repeated_disabled.returncode == 0
    assert json.loads(disabled.stdout)["maintenance"] is False
    assert not marker.exists()


def test_hook_rejects_symlinked_marker_without_changing_target(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    run_directory = tmp_path / "run"
    state_directory.mkdir()
    run_directory.mkdir()
    state = state_directory / "target.json"
    state.write_text('{"schema_version":1,"target":"old","updated_at":"now"}\n')
    state.chmod(0o600)
    marker = run_directory / "maintenance"
    marker.symlink_to(tmp_path / "outside")

    result = _run(state, marker, "maintenance-on")

    assert result.returncode == 2
    assert "must not be a symlink" in result.stderr
    assert json.loads(state.read_text())["target"] == "old"


def test_nginx_configuration_example_uses_the_executable_hook_contract() -> None:
    loaded = load_configuration(EXAMPLE_CONFIG, environment={})
    traffic = loaded.config.traffic

    assert loaded.config.application.production_port == 4510
    assert loaded.config.application.candidate_port == 4511
    for command, action in (
        (traffic.maintenance_on_command, "maintenance-on"),
        (traffic.maintenance_off_command, "maintenance-off"),
        (traffic.activate_new_command, "activate-new"),
        (traffic.activate_old_command, "activate-old"),
    ):
        assert command == (
            "python3",
            "/opt/dploydb/nginx/dploydb-hook.py",
            "--state-file",
            "/var/lib/dploydb/example-app/proxy-target.json",
            "--maintenance-file",
            "/run/dploydb/example-app.maintenance",
            action,
        )
