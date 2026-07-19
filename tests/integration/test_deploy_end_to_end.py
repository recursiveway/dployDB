"""Real-Docker Milestone 5F deployment, rollback, and traffic-isolation gate."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from dploydb.config import STARTER_CONFIGURATION
from dploydb.models import new_operation_id

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "demo" / "compose.yaml"
RELEASES = ROOT / "demo" / "releases"


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _environment(data: Path, port: int, release: str) -> dict[str, str]:
    return {
        **os.environ,
        "DPLOYDB_DEMO_DATA_DIR": str(data.resolve()),
        "DPLOYDB_DEMO_RELEASE_DIR": str((RELEASES / release).resolve()),
        "DPLOYDB_DEMO_PORT": str(port),
        "DPLOYDB_DEMO_UID": str(os.getuid()),
        "DPLOYDB_DEMO_GID": str(os.getgid()),
        "DPLOYDB_VERSION": release,
        "PYTHONPATH": str(ROOT),
    }


def _compose(
    project: str,
    data: Path,
    port: int,
    release: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(COMPOSE_FILE),
            "--project-name",
            project,
            *arguments,
        ],
        cwd=ROOT,
        env=_environment(data, port, release),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _migrate(database: Path, release: str) -> None:
    environment = {**os.environ, "DATABASE_PATH": str(database.resolve())}
    result = subprocess.run(
        [sys.executable, "-m", "demo.runtime.migration", str(RELEASES / release)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _json_request(port: int, path: str, *, timeout: float = 2) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _wait_for_release(port: int, release: str, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            status, payload = _json_request(port, "/health")
            last = (status, payload)
            if status == 200 and isinstance(payload, dict) and payload.get("release") == release:
                return
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last = exc
        time.sleep(0.05)
    pytest.fail(f"release {release} did not become healthy: {last!r}")


class _TrafficProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state_path: ClassVar[Path]
    upstream_port: ClassVar[int]
    records: ClassVar[list[dict[str, object]]]
    records_lock: ClassVar[threading.Lock]

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/notes":
            self._send(HTTPStatus.NOT_FOUND.value, {"error": "route"})
            return
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        maintenance = state["maintenance"] is True
        target = state["target"]
        status = HTTPStatus.SERVICE_UNAVAILABLE.value
        payload: object = {"error": "maintenance"}
        observed = "maintenance"
        if not maintenance:
            try:
                status, payload = _json_request(self.upstream_port, "/notes", timeout=0.5)
                if status == 200 and isinstance(payload, list):
                    observed = (
                        "new"
                        if payload and isinstance(payload[0], dict) and "category" in payload[0]
                        else "old"
                    )
                else:
                    observed = "upstream-error"
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                observed = "upstream-unavailable"
        with self.records_lock:
            self.records.append(
                {
                    "time_ns": time.time_ns(),
                    "status": status,
                    "observed": observed,
                    "maintenance": maintenance,
                    "target": target,
                }
            )
        self._send(status, payload)

    def _send(self, status: int, payload: object) -> None:
        body = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@dataclass
class _TrafficMonitor:
    server: ThreadingHTTPServer
    port: int
    records: list[dict[str, object]]
    stop: threading.Event
    server_thread: threading.Thread
    monitor_thread: threading.Thread

    @classmethod
    def start(cls, state_path: Path, upstream_port: int) -> _TrafficMonitor:
        records: list[dict[str, object]] = []
        handler = type("TrafficProxyHandler", (_TrafficProxyHandler,), {})
        handler.state_path = state_path
        handler.upstream_port = upstream_port
        handler.records = records
        handler.records_lock = threading.Lock()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = int(server.server_address[1])
        stop = threading.Event()
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)

        def monitor() -> None:
            while not stop.is_set():
                try:
                    _json_request(port, "/notes", timeout=1)
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    pass
                stop.wait(0.025)

        monitor_thread = threading.Thread(target=monitor, daemon=True)
        server_thread.start()
        monitor_thread.start()
        return cls(server, port, records, stop, server_thread, monitor_thread)

    def close(self) -> None:
        self.stop.set()
        self.monitor_thread.join(timeout=3)
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)


def _write_configuration(
    tmp_path: Path,
    *,
    project: str,
    current_project: str,
    database: Path,
    production_port: int,
    candidate_port: int,
    release: str,
    traffic_state: Path,
    production_migration_failure: bool,
) -> Path:
    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = project
    value["state_directory"] = str(tmp_path / "state")
    value["database"]["path"] = str(database.resolve())
    if production_migration_failure:
        value["migration"]["command"] = [
            sys.executable,
            "-m",
            "demo.runtime.production_migration_failure",
            str((RELEASES / release).resolve()),
            str(database.resolve()),
        ]
    else:
        value["migration"]["command"] = [
            sys.executable,
            "-m",
            "demo.runtime.migration",
            str((RELEASES / release).resolve()),
        ]
    value["migration"]["timeout_seconds"] = 30
    application = value["application"]
    application["compose_file"] = str(COMPOSE_FILE)
    application["production_project"] = current_project
    application["production_port"] = production_port
    application["production_health_url"] = f"http://127.0.0.1:{production_port}/health"
    application["candidate_port"] = candidate_port
    application["candidate_health_url"] = f"http://127.0.0.1:{candidate_port}/health"
    application["startup_timeout_seconds"] = 20
    application.pop("smoke_command", None)
    hook = [sys.executable, "-m", "demo.runtime.traffic_hook", str(traffic_state)]
    value["traffic"]["maintenance_on_command"] = [*hook, "maintenance-on"]
    value["traffic"]["maintenance_off_command"] = [*hook, "maintenance-off"]
    value["traffic"]["activate_new_command"] = [*hook, "activate-new"]
    value["traffic"]["activate_old_command"] = [*hook, "activate-old"]
    value["traffic"]["timeout_seconds"] = 10
    value["backup"]["local_directory"] = str(tmp_path / "backups")
    path = tmp_path / "dploydb.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def _cleanup_resources(token: str) -> None:
    containers = subprocess.run(
        ["docker", "container", "ls", "--all", "--quiet", "--filter", f"name={token}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert containers.returncode == 0, containers.stderr
    identifiers = [item for item in containers.stdout.splitlines() if item]
    if identifiers:
        removed = subprocess.run(
            ["docker", "container", "rm", "--force", *identifiers],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert removed.returncode == 0, removed.stderr
    networks = subprocess.run(
        ["docker", "network", "ls", "--quiet", "--filter", f"name={token}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert networks.returncode == 0, networks.stderr
    network_ids = [item for item in networks.stdout.splitlines() if item]
    if network_ids:
        removed = subprocess.run(
            ["docker", "network", "rm", *network_ids],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert removed.returncode == 0, removed.stderr


@pytest.mark.parametrize(
    ("scenario", "release", "expected_outcome", "expected_exit", "migration_failure"),
    [
        pytest.param("success", "v2", "active", 0, False, id="successful-v2"),
        pytest.param(
            "migration",
            "v2",
            "rolled_back",
            40,
            True,
            id="production-migration-failure-rollback",
        ),
        pytest.param(
            "health",
            "final-health-failure",
            "rolled_back",
            50,
            False,
            id="final-health-failure-rollback",
        ),
    ],
)
def test_real_docker_deploy_flows_and_pre_activation_traffic_isolation(
    tmp_path: Path,
    scenario: str,
    release: str,
    expected_outcome: str,
    expected_exit: int,
    migration_failure: bool,
) -> None:
    docker = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=30, check=False
    )
    assert docker.returncode == 0, f"Docker is required for Milestone 5F:\n{docker.stderr}"
    token = f"e2e{str(new_operation_id())[-10:]}"
    current_project = f"{token}-current"
    data = (tmp_path / "data").resolve()
    data.mkdir()
    database = data / "app.db"
    database.touch()
    _migrate(database, "v1")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO notes(body) VALUES ('traffic sentinel')")
    production_port = _available_port()
    candidate_port = _available_port()
    while candidate_port == production_port:
        candidate_port = _available_port()
    traffic_state = tmp_path / "traffic.json"
    traffic_state.write_text(
        json.dumps({"maintenance": False, "target": "old", "events": []}) + "\n",
        encoding="utf-8",
    )
    config_path = _write_configuration(
        tmp_path,
        project=token,
        current_project=current_project,
        database=database,
        production_port=production_port,
        candidate_port=candidate_port,
        release=release,
        traffic_state=traffic_state,
        production_migration_failure=migration_failure,
    )
    monitor: _TrafficMonitor | None = None
    try:
        started = _compose(
            current_project,
            data,
            production_port,
            "v1",
            "up",
            "--detach",
            "--build",
            "--force-recreate",
            "app",
        )
        assert started.returncode == 0, started.stderr
        _wait_for_release(production_port, "v1")
        monitor = _TrafficMonitor.start(traffic_state, production_port)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
            record["observed"] == "old" for record in monitor.records
        ):
            time.sleep(0.025)
        assert any(record["observed"] == "old" for record in monitor.records)

        result = subprocess.run(
            [
                str(ROOT / ".venv" / "bin" / "dploydb"),
                "deploy",
                "--version",
                release,
                "--config",
                str(config_path),
                "--json",
                "--non-interactive",
            ],
            cwd=ROOT,
            env=_environment(data, production_port, release),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert result.returncode == expected_exit, result.stderr or result.stdout
        payload = json.loads(result.stdout)
        assert payload["outcome"] == expected_outcome
        assert payload["traffic_activated"] is (scenario == "success")
        assert payload["recovery_required"] is False

        if scenario == "success":
            _wait_for_release(production_port, "v2")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not any(
                record["observed"] == "new" for record in monitor.records
            ):
                time.sleep(0.025)
            with sqlite3.connect(database) as connection:
                assert connection.execute("PRAGMA user_version").fetchone() == (2,)
                assert [row[1] for row in connection.execute("PRAGMA table_info(notes)")] == [
                    "id",
                    "body",
                    "category",
                ]
        else:
            _wait_for_release(production_port, "v1")
            with sqlite3.connect(database) as connection:
                assert connection.execute("PRAGMA user_version").fetchone() == (1,)
                assert [row[1] for row in connection.execute("PRAGMA table_info(notes)")] == [
                    "id",
                    "body",
                ]
                assert connection.execute("SELECT body FROM notes ORDER BY id").fetchall() == [
                    ("traffic sentinel",)
                ]

        state = json.loads(traffic_state.read_text(encoding="utf-8"))
        actions = [event["action"] for event in state["events"]]
        assert actions[0] == "maintenance-on"
        assert actions[-1] == "maintenance-off"
        if scenario == "success":
            assert actions == ["maintenance-on", "activate-new", "maintenance-off"]
            assert any(record["observed"] == "new" for record in monitor.records)
        else:
            assert actions == ["maintenance-on", "activate-old", "maintenance-off"]
        assert any(record["observed"] == "maintenance" for record in monitor.records)
        assert all(
            record["target"] == "new" and record["maintenance"] is False
            for record in monitor.records
            if record["observed"] == "new"
        ), "the new release received normal traffic before activation and maintenance removal"
    finally:
        if monitor is not None:
            monitor.close()
        _cleanup_resources(token)
        remaining = subprocess.run(
            ["docker", "container", "ls", "--all", "--quiet", "--filter", f"name={token}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert remaining.returncode == 0 and not remaining.stdout.strip(), remaining.stderr
