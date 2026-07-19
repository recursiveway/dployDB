"""Real-Docker Milestone 5C production lifecycle and rollback gate."""

from __future__ import annotations

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

from dploydb.config import STARTER_CONFIGURATION, ApplicationConfig, ProductionTopology
from dploydb.models import ProductionApplicationHandle, new_operation_id, new_release_id
from dploydb.redaction import SecretRegistry
from dploydb.runners.docker_compose_production import DockerComposeProductionRunner
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


def command_environment(data: Path, port: int, release: str) -> dict[str, str]:
    return {
        **os.environ,
        "DPLOYDB_DEMO_DATA_DIR": str(data),
        "DPLOYDB_DEMO_RELEASE_DIR": str((RELEASES / release).resolve()),
        "DPLOYDB_DEMO_PORT": str(port),
        "DPLOYDB_DEMO_UID": str(os.getuid()),
        "DPLOYDB_DEMO_GID": str(os.getgid()),
        "DPLOYDB_VERSION": release,
    }


def compose_command(project: str, *arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--file",
        str(COMPOSE_FILE),
        "--project-name",
        project,
        *arguments,
    ]


def run_compose(
    project: str,
    data: Path,
    port: int,
    release: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        compose_command(project, *arguments),
        cwd=ROOT,
        env=command_environment(data, port, release),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def application(
    current_project: str,
    production_port: int,
    candidate_port: int,
) -> ApplicationConfig:
    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    selected = value["application"]
    selected["compose_file"] = str(COMPOSE_FILE)
    selected["production_project"] = current_project
    selected["production_port"] = production_port
    selected["production_health_url"] = f"http://127.0.0.1:{production_port}/health"
    selected["candidate_port"] = candidate_port
    selected["candidate_health_url"] = f"http://127.0.0.1:{candidate_port}/health"
    selected["startup_timeout_seconds"] = 90
    selected.pop("smoke_command", None)
    return ApplicationConfig.model_validate(selected)


def wait_for_health(port: int, release: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                body = json.loads(response.read())
                if response.status == 200 and isinstance(body, dict):
                    assert body.get("release") == release
                    return body
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.1)
    pytest.fail(f"{release} did not become healthy: {last_error}")


def assert_docker_required() -> None:
    info = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert info.returncode == 0, f"Docker is required for Milestone 5C:\n{info.stderr}"


def restore_stopped_fixture(database: Path, payload: bytes) -> None:
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)
    temporary = database.parent / ".runner-rollback.tmp"
    temporary.write_bytes(payload)
    os.replace(temporary, database)


def test_real_production_runner_preserves_and_restarts_exact_previous_container(
    tmp_path: Path,
) -> None:
    assert_docker_required()
    database = (tmp_path / "production" / "app.db").resolve()
    database.parent.mkdir()
    database.touch()
    migrate(database, "v1")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO notes(body) VALUES ('preserved-v1-row')")

    production_port = available_port()
    candidate_port = available_port()
    while candidate_port == production_port:
        candidate_port = available_port()
    current_project = f"dploydb-current-{str(new_operation_id())[-12:]}"
    selected_application = application(current_project, production_port, candidate_port)
    topology = ProductionTopology(
        compose_project=current_project,
        host_port=production_port,
        health_url=f"http://127.0.0.1:{production_port}/health",
    )
    secrets = SecretRegistry()
    runner = DockerComposeProductionRunner(
        project="dploydb-demo",
        application=selected_application,
        topology=topology,
        database_environment_name="DATABASE_PATH",
        production_database_path=database,
        secrets=secrets,
        working_directory=ROOT,
        command_environment=command_environment(database.parent, production_port, "v2"),
        command_runner=SubprocessRunner(secrets=secrets, max_output_bytes=256 * 1024),
    )

    previous: ProductionApplicationHandle | None = None
    new: ProductionApplicationHandle | None = None
    current_started = False
    try:
        started = run_compose(
            current_project,
            database.parent,
            production_port,
            "v1",
            "up",
            "--detach",
            "--build",
            "--force-recreate",
            "app",
        )
        assert started.returncode == 0, started.stderr
        current_started = True
        assert wait_for_health(production_port, "v1")["schema_version"] == 1

        discovered = runner.discover_current()
        previous = discovered.inspection.handle
        previous_container_id = previous.container_id
        stopped = runner.stop_current(previous)
        assert stopped.inspection.running is False
        v1_database = database.read_bytes()

        migrate(database, "v2")
        release_id = new_release_id()
        operation_id = str(new_operation_id())
        production_start = runner.start_new(
            operation_id=operation_id,
            release_id=release_id,
            version="v2",
        )
        new = production_start.handle
        assert production_start.inspection.running is True
        assert new.container_id != previous_container_id
        assert runner.inspect(previous, expected_running=False).running is False
        assert wait_for_health(production_port, "v2")["schema_version"] == 2
        logs = runner.collect_logs(new)
        assert logs.command.stdout.truncated is False
        assert logs.command.stderr.truncated is False

        first_cleanup = runner.remove_new(new)
        second_cleanup = runner.remove_new(new)
        assert first_cleanup.proof.proven and second_cleanup.proof.proven
        new = None

        restore_stopped_fixture(database, v1_database)
        restarted = runner.restart_previous(previous)
        assert restarted.handle.container_id == previous_container_id
        assert restarted.inspection.running is True
        health = wait_for_health(production_port, "v1")
        assert health["schema_version"] == 1
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT body FROM notes ORDER BY id").fetchall() == [
                ("preserved-v1-row",)
            ]
    finally:
        if new is not None:
            runner.remove_new(new)
        if current_started:
            cleanup = run_compose(
                current_project,
                database.parent,
                production_port,
                "v1",
                "down",
                "--remove-orphans",
            )
            assert cleanup.returncode == 0, cleanup.stderr
