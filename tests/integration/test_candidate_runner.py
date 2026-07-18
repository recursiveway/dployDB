"""Real-Docker Milestone 4A candidate isolation and cleanup gate."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from dploydb.config import STARTER_CONFIGURATION, ApplicationConfig
from dploydb.models import new_operation_id
from dploydb.redaction import SecretRegistry
from dploydb.runners.base import CandidateHandle, CandidateStartError
from dploydb.runners.docker_compose import DockerComposeCandidateRunner
from dploydb.subprocesses import SubprocessRunner

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "demo" / "compose.yaml"
RELEASES = ROOT / "demo" / "releases"


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def migrate(database: Path, version: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_PATH"] = str(database)
    completed = subprocess.run(
        [sys.executable, "-m", "demo.runtime.migration", str(RELEASES / version)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def application(port: int) -> ApplicationConfig:
    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    selected = value["application"]
    selected["compose_file"] = str(COMPOSE_FILE)
    selected["candidate_port"] = port
    selected["candidate_health_url"] = f"http://127.0.0.1:{port}/health"
    selected["startup_timeout_seconds"] = 90
    selected.pop("smoke_command", None)
    return ApplicationConfig.model_validate(selected)


def command_environment(production_directory: Path, port: int, release: str) -> dict[str, str]:
    return {
        **os.environ,
        "DPLOYDB_DEMO_DATA_DIR": str(production_directory),
        "DPLOYDB_DEMO_RELEASE_DIR": str((RELEASES / release).resolve()),
        "DPLOYDB_DEMO_PORT": str(port),
        "DPLOYDB_DEMO_UID": str(os.getuid()),
        "DPLOYDB_DEMO_GID": str(os.getgid()),
    }


def runner(
    *,
    tmp_path: Path,
    production: Path,
    port: int,
    release: str,
) -> DockerComposeCandidateRunner:
    secrets = SecretRegistry()
    return DockerComposeCandidateRunner(
        project="dploydb-demo",
        application=application(port),
        database_environment_name="DATABASE_PATH",
        production_database_path=production,
        secrets=secrets,
        working_directory=ROOT,
        command_environment=command_environment(production.parent, port, release),
        command_runner=SubprocessRunner(secrets=secrets, max_output_bytes=256 * 1024),
    )


def wait_for_health(port: int) -> dict[str, object]:
    deadline = time.monotonic() + 30
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                body = json.loads(response.read())
                if response.status == 200:
                    assert isinstance(body, dict)
                    return body
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.1)
    pytest.fail(f"candidate did not become healthy for fixture verification: {last_error}")


def assert_docker_required() -> None:
    info = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert info.returncode == 0, f"Docker is required for Milestone 4A:\n{info.stderr}"


def test_real_candidate_is_isolated_inspected_and_removed_idempotently(tmp_path: Path) -> None:
    assert_docker_required()
    production = (tmp_path / "production" / "app.db").resolve()
    rehearsal = (tmp_path / "rehearsal" / "rehearsal.db").resolve()
    production.parent.mkdir()
    rehearsal.parent.mkdir()
    production.touch()
    rehearsal.touch()
    migrate(production, "v1")
    migrate(rehearsal, "v1")
    migrate(rehearsal, "v2")
    production_before = hashlib.sha256(production.read_bytes()).hexdigest()
    port = available_port()
    selected = runner(
        tmp_path=tmp_path,
        production=production,
        port=port,
        release="v2",
    )
    handle: CandidateHandle | None = None
    try:
        started = selected.start(
            operation_id=str(new_operation_id()),
            version="v2",
            rehearsal_database_path=rehearsal,
        )
        handle = started.handle
        inspected = selected.inspect(handle)

        assert inspected.running is True
        assert inspected.host_ip == "127.0.0.1"
        assert inspected.host_port == port
        assert [
            mount
            for mount in inspected.mounts
            if mount.destination == application(port).database_volume_target
        ][0].source == str(rehearsal.parent)
        assert all(str(production) != mount.source for mount in inspected.mounts)
        assert wait_for_health(port) == {"ok": True, "release": "v2", "schema_version": 2}
        logs = selected.collect_logs(handle)
        assert logs.command.stdout.truncated is False
        assert logs.command.stderr.truncated is False
        assert hashlib.sha256(production.read_bytes()).hexdigest() == production_before

        first_cleanup = selected.stop(handle)
        second_cleanup = selected.stop(handle)
        assert first_cleanup.proof.proven is True
        assert second_cleanup.proof.proven is True
        handle = None
    finally:
        if handle is not None:
            selected.stop(handle)

    with sqlite3.connect(production) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


def test_real_occupied_port_start_failure_leaves_no_candidate_resources(tmp_path: Path) -> None:
    assert_docker_required()
    production = (tmp_path / "production" / "app.db").resolve()
    rehearsal = (tmp_path / "rehearsal" / "rehearsal.db").resolve()
    production.parent.mkdir()
    rehearsal.parent.mkdir()
    production.touch()
    rehearsal.touch()
    migrate(production, "v1")
    migrate(rehearsal, "v1")
    port = available_port()
    selected = runner(
        tmp_path=tmp_path,
        production=production,
        port=port,
        release="v1",
    )
    blocker = selected.start(
        operation_id=str(new_operation_id()),
        version="v1",
        rehearsal_database_path=rehearsal,
    )
    try:
        with pytest.raises(CandidateStartError) as captured:
            selected.start(
                operation_id=str(new_operation_id()),
                version="v1",
                rehearsal_database_path=rehearsal,
            )
    finally:
        selected.stop(blocker.handle)

    assert captured.value.cleanup_proven is True
    assert captured.value.cleanup is not None
    assert captured.value.cleanup.proof.container_absent is True
    assert captured.value.cleanup.proof.networks_absent is True
