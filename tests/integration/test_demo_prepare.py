"""Tests for the documented real-CLI demo preparation step."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from demo.controller import DemoError, _managed_release_project, build_context
from demo.prepare import prepare_demo
from dploydb.config import load_configuration
from dploydb.redaction import SecretRegistry


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_prepare_demo_creates_private_valid_real_deployment_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DPLOYDB_DEMO_STATE_ROOT", str(tmp_path / "demo-state"))
    context = build_context("quickstart", 4510)
    context.data_dir.mkdir(parents=True)
    context.database_path.touch()

    config_path, environment_path, traffic_path = prepare_demo(
        instance="quickstart",
        production_port=4510,
        candidate_port=4511,
        python_executable=Path(sys.executable).resolve(),
    )

    loaded = load_configuration(config_path, environment={}, secrets=SecretRegistry())
    assert loaded.config.project == context.project_name
    assert loaded.config.database.path == context.database_path
    assert loaded.config.application.production_project == context.project_name
    assert loaded.config.application.production_port == 4510
    assert loaded.config.application.candidate_port == 4511
    assert loaded.config.migration.command[0] == str(Path(sys.executable).resolve())
    assert json.loads(traffic_path.read_text()) == {
        "events": [],
        "maintenance": False,
        "target": "old",
    }
    environment = environment_path.read_text()
    assert f"export DPLOYDB_DEMO_DATA_DIR={context.data_dir.resolve()}" in environment
    assert f"export PYTHONPATH={Path(__file__).resolve().parents[2]}" in environment
    assert _mode(context.instance_dir) == 0o700
    assert _mode(config_path) == 0o600
    assert _mode(environment_path) == 0o600
    assert _mode(traffic_path) == 0o600


def test_prepare_demo_refuses_to_overwrite_or_run_without_v1_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DPLOYDB_DEMO_STATE_ROOT", str(tmp_path / "demo-state"))
    context = build_context("quickstart", 4510)

    with pytest.raises(SystemExit, match="start-v1 first"):
        prepare_demo(
            instance="quickstart",
            production_port=4510,
            candidate_port=4511,
            python_executable=Path(sys.executable).resolve(),
        )

    context.data_dir.mkdir(parents=True)
    context.database_path.touch()
    prepare_demo(
        instance="quickstart",
        production_port=4510,
        candidate_port=4511,
        python_executable=Path(sys.executable).resolve(),
    )
    with pytest.raises(SystemExit, match="already exist and were preserved"):
        prepare_demo(
            instance="quickstart",
            production_port=4510,
            candidate_port=4511,
            python_executable=Path(sys.executable).resolve(),
        )


def test_prepare_demo_rejects_ambiguous_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DPLOYDB_DEMO_STATE_ROOT", str(tmp_path / "demo-state"))

    with pytest.raises(SystemExit, match="ports must differ"):
        prepare_demo(
            instance="quickstart",
            production_port=4510,
            candidate_port=4510,
            python_executable=Path(sys.executable).resolve(),
        )


def test_demo_cleanup_identity_requires_exact_database_project_and_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DPLOYDB_DEMO_STATE_ROOT", str(tmp_path / "demo-state"))
    context = build_context("quickstart", 4510)
    safe_project = context.project_name[:20]
    project = f"dploydb-{safe_project}-release-0123456789abcdef"
    record = {
        "Name": f"/{project}-app",
        "Config": {
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "app",
                "io.dploydb.role": "production_release",
            }
        },
        "HostConfig": {"PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4510"}]}},
        "Mounts": [{"Source": str(context.data_dir), "Destination": "/data", "RW": True}],
    }

    assert _managed_release_project(context, record) == project
    unrelated = {**record, "Mounts": [{"Source": "/other", "Destination": "/data", "RW": True}]}
    assert _managed_release_project(context, unrelated) is None
    wrong_port = {
        **record,
        "HostConfig": {"PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9999"}]}},
    }
    with pytest.raises(DemoError, match="wrong host port"):
        _managed_release_project(context, wrong_port)
