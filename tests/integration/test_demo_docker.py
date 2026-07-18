"""Docker Compose acceptance tests for the deterministic demo."""

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "demo" / "controller.py"
COMMAND_TIMEOUT_SECONDS = 240


@dataclass(frozen=True)
class DockerDemo:
    instance: str
    port: int
    state_root: Path
    environment: dict[str, str]

    @property
    def database(self) -> Path:
        return self.state_root / self.instance / "data" / "app.db"

    @property
    def project_name(self) -> str:
        instance_dir = (self.state_root / self.instance).resolve()
        digest = hashlib.sha256(str(instance_dir).encode()).hexdigest()[:10]
        return f"dploydb-demo-{self.instance}-{digest}"

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(CONTROLLER),
                "--instance",
                self.instance,
                "--port",
                str(self.port),
                *arguments,
            ],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    payload: object | None = None,
) -> tuple[int, Any]:
    body = None if payload is None else json.dumps(payload).encode()
    headers = {} if body is None else {"Content-Type": "application/json"}
    http_request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(http_request, timeout=3) as response:
            status = response.status
            response_body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        response_body = error.read()
    return status, json.loads(response_body)


def wait_for_status(port: int, expected: int) -> tuple[int, Any]:
    deadline = time.monotonic() + 30
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            result = request(port, "/health")
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.1)
            continue
        if result[0] == expected:
            return result
        time.sleep(0.1)
    pytest.fail(f"health did not reach HTTP {expected}; last error: {last_error}")


def assert_command_passed(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


@pytest.fixture
def docker_demo(tmp_path: Path, request: pytest.FixtureRequest) -> DockerDemo:
    docker_info = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if docker_info.returncode != 0:
        pytest.fail(f"Docker is required for Milestone 0B:\n{docker_info.stderr}")

    safe_name = request.node.name.replace("_", "-")[:32]
    instance = f"test-{os.getpid()}-{safe_name}"
    environment = os.environ.copy()
    environment["DPLOYDB_DEMO_STATE_ROOT"] = str((tmp_path / "state").resolve())
    demo = DockerDemo(
        instance=instance,
        port=available_port(),
        state_root=Path(environment["DPLOYDB_DEMO_STATE_ROOT"]),
        environment=environment,
    )
    try:
        yield demo
    finally:
        stop = demo.run("stop")
        if stop.returncode != 0:
            pytest.fail(f"demo cleanup failed:\n{stop.stdout}\n{stop.stderr}")

        containers = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={demo.project_name}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        networks = subprocess.run(
            [
                "docker",
                "network",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={demo.project_name}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert containers.returncode == 0 and not containers.stdout.strip()
        assert networks.returncode == 0 and not networks.stdout.strip()


def test_v1_start_persistence_and_deterministic_reset(docker_demo: DockerDemo) -> None:
    assert_command_passed(docker_demo.run("start-v1"))
    assert request(docker_demo.port, "/health")[0] == 200
    assert request(
        docker_demo.port,
        "/notes",
        method="POST",
        payload={"body": "persist through restart"},
    ) == (201, {"id": 1, "body": "persist through restart"})

    assert_command_passed(docker_demo.run("stop"))
    assert_command_passed(docker_demo.run("start", "v1"))
    assert request(docker_demo.port, "/notes") == (
        200,
        [{"id": 1, "body": "persist through restart"}],
    )

    assert_command_passed(docker_demo.run("start-v1"))
    assert request(docker_demo.port, "/notes") == (200, [])


def test_v2_preserves_v1_data_and_supports_category(docker_demo: DockerDemo) -> None:
    assert_command_passed(docker_demo.run("start-v1"))
    assert (
        request(
            docker_demo.port,
            "/notes",
            method="POST",
            payload={"body": "written under v1"},
        )[0]
        == 201
    )
    assert_command_passed(docker_demo.run("stop"))

    assert_command_passed(docker_demo.run("migrate", "v2"))
    assert_command_passed(docker_demo.run("start", "v2"))
    assert request(docker_demo.port, "/notes") == (
        200,
        [{"id": 1, "body": "written under v1", "category": "general"}],
    )
    assert request(
        docker_demo.port,
        "/notes",
        method="POST",
        payload={"body": "written under v2", "category": "deployment"},
    ) == (
        201,
        {"id": 2, "body": "written under v2", "category": "deployment"},
    )

    with sqlite3.connect(docker_demo.database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)


def test_broken_releases_fail_for_the_expected_reasons(docker_demo: DockerDemo) -> None:
    assert_command_passed(docker_demo.run("start-v1"))
    assert (
        request(
            docker_demo.port,
            "/notes",
            method="POST",
            payload={"body": "must survive"},
        )[0]
        == 201
    )
    assert_command_passed(docker_demo.run("stop"))

    before = hashlib.sha256(docker_demo.database.read_bytes()).hexdigest()
    broken_migration = docker_demo.run("migrate", "broken-migration")
    assert broken_migration.returncode == 1
    assert "no such table: deliberate_missing_table" in broken_migration.stderr
    assert hashlib.sha256(docker_demo.database.read_bytes()).hexdigest() == before

    assert_command_passed(docker_demo.run("start", "v1"))
    assert request(docker_demo.port, "/notes") == (
        200,
        [{"id": 1, "body": "must survive"}],
    )

    assert_command_passed(docker_demo.run("start-v1"))
    assert_command_passed(docker_demo.run("stop"))
    assert_command_passed(docker_demo.run("migrate", "broken-health"))
    assert_command_passed(docker_demo.run("up", "broken-health"))
    assert wait_for_status(docker_demo.port, 503) == (
        503,
        {"ok": False, "reason": "fixture_broken_health"},
    )

    health = docker_demo.run("health")
    assert health.returncode == 1
    assert "fixture_broken_health" in health.stderr
