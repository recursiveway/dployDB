"""Real-Docker Milestone 4C continuity, durability, and cleanup gate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from dploydb.candidate import validate_configured_candidate
from dploydb.config import STARTER_CONFIGURATION, LoadedConfiguration, load_configuration
from dploydb.errors import ExternalCommandError, OperationFailedError
from dploydb.health import CandidateHealthChecker
from dploydb.models import OperationStatus, new_operation_id
from dploydb.runners.docker_compose import DockerComposeCandidateRunner
from dploydb.state import StateStore
from dploydb.subprocesses import SubprocessRunner

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "demo" / "controller.py"
COMPOSE_FILE = ROOT / "demo" / "compose.yaml"
RELEASES = ROOT / "demo" / "releases"


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def assert_docker_required() -> None:
    result = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"Docker is required for Milestone 4C:\n{result.stderr}"


def controller_environment(state_root: Path) -> dict[str, str]:
    return {**os.environ, "DPLOYDB_DEMO_STATE_ROOT": str(state_root)}


def run_controller(
    state_root: Path,
    port: int,
    command: str,
    *,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CONTROLLER),
            "--instance",
            "current",
            "--port",
            str(port),
            command,
        ],
        cwd=ROOT,
        env=controller_environment(state_root),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@contextmanager
def current_v1(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    assert_docker_required()
    state_root = (tmp_path / "current-app").resolve()
    port = available_port()
    started = run_controller(state_root, port, "start-v1")
    assert started.returncode == 0, started.stderr
    database = state_root / "current" / "data" / "app.db"
    assert database.is_file()
    try:
        _post_note(port, "visible-current-v1-row")
        yield database, port
    finally:
        stopped = run_controller(state_root, port, "stop")
        assert stopped.returncode == 0, stopped.stderr


def _post_note(port: int, body: str) -> None:
    payload = json.dumps({"body": body}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/notes",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        assert response.status == 201


def _get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=1) as response:
        assert response.status == 200
        return json.load(response)


@dataclass
class CurrentApplicationMonitor:
    port: int
    stop_event: threading.Event = field(default_factory=threading.Event)
    successes: int = 0
    failures: list[str] = field(default_factory=list)
    thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        assert self.thread is not None
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                health = _get_json(f"http://127.0.0.1:{self.port}/health")
                notes = _get_json(f"http://127.0.0.1:{self.port}/notes")
                if not isinstance(health, dict) or health.get("release") != "v1":
                    raise AssertionError(f"unexpected health payload: {health!r}")
                if not isinstance(notes, list) or not any(
                    isinstance(note, dict) and note.get("body") == "visible-current-v1-row"
                    for note in notes
                ):
                    raise AssertionError(f"current row disappeared: {notes!r}")
                self.successes += 1
            except Exception as error:  # evidence captured for the controlling test
                self.failures.append(f"{type(error).__name__}: {error}")
            self.stop_event.wait(0.05)


@contextmanager
def monitoring_current(port: int) -> Iterator[CurrentApplicationMonitor]:
    monitor = CurrentApplicationMonitor(port)
    monitor.start()
    try:
        yield monitor
    finally:
        monitor.stop()


def command_environment(
    *,
    production: Path,
    candidate_port: int,
    release: str,
    secret: str,
) -> dict[str, str]:
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH")
    environment.update(
        {
            "PYTHONPATH": (str(ROOT) if not existing else os.pathsep.join((str(ROOT), existing))),
            "DPLOYDB_DEMO_DATA_DIR": str(production.parent),
            "DPLOYDB_DEMO_RELEASE_DIR": str((RELEASES / release).resolve()),
            "DPLOYDB_DEMO_PORT": str(candidate_port),
            "DPLOYDB_DEMO_UID": str(os.getuid()),
            "DPLOYDB_DEMO_GID": str(os.getgid()),
            "API_TOKEN": secret,
        }
    )
    return environment


def configuration(
    tmp_path: Path,
    *,
    production: Path,
    candidate_port: int,
    release: str,
    secret: str,
    smoke: str | None,
) -> tuple[Path, LoadedConfiguration, dict[str, str]]:
    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = "candidate-gate"
    value["state_directory"] = str(tmp_path / "dploydb-state")
    value["database"]["path"] = str(production)
    value["migration"]["command"] = [
        sys.executable,
        "-m",
        "demo.runtime.migration",
        str((RELEASES / release).resolve()),
    ]
    value["migration"]["timeout_seconds"] = 10
    application = value["application"]
    application["compose_file"] = str(COMPOSE_FILE)
    application["candidate_port"] = candidate_port
    application["candidate_health_url"] = f"http://127.0.0.1:{candidate_port}/health"
    application["startup_timeout_seconds"] = 30
    application["test_mode_env"] = {
        "DPLOYDB_TEST_MODE": "1",
        "API_TOKEN": "${CANDIDATE_SECRET}",
    }
    if smoke is None:
        application.pop("smoke_command", None)
    else:
        application["smoke_command"] = [sys.executable, "-c", smoke]
    value["backup"]["local_directory"] = str(tmp_path / "backups")
    for name in (
        "maintenance_on_command",
        "maintenance_off_command",
        "activate_new_command",
        "activate_old_command",
    ):
        value["traffic"][name] = [sys.executable, "-c", "pass"]
    config_path = tmp_path / "dploydb.yaml"
    config_path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    loaded = load_configuration(
        config_path,
        environment={"CANDIDATE_SECRET": secret},
    )
    return (
        config_path,
        loaded,
        command_environment(
            production=production,
            candidate_port=candidate_port,
            release=release,
            secret=secret,
        ),
    )


def production_state(database: Path) -> tuple[str, int, list[tuple[Any, ...]], list[Any]]:
    sha256 = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        ).fetchall()
        rows = connection.execute("SELECT id, body FROM notes ORDER BY id").fetchall()
    return sha256, version, schema, rows


def create_v2_database(path: Path) -> None:
    path.parent.mkdir()
    path.touch()
    environment = {**os.environ, "DATABASE_PATH": str(path)}
    for release in ("v1", "v2"):
        result = subprocess.run(
            [sys.executable, "-m", "demo.runtime.migration", str(RELEASES / release)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def latest_operation(loaded: LoadedConfiguration):
    store = StateStore(loaded.config.state_directory, secrets=loaded.secrets)
    manifest = store.latest_operation()
    assert manifest is not None
    return manifest, store.read_events(manifest.operation_id)


def assert_resources_clean(loaded: LoadedConfiguration, operation_id: str) -> None:
    container = subprocess.run(
        [
            "docker",
            "container",
            "ls",
            "--all",
            "--filter",
            f"label=io.dploydb.operation_id={operation_id}",
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert container.returncode == 0, container.stderr
    assert container.stdout.strip() == ""
    safe_project = re.sub(r"[^a-z0-9_-]", "-", loaded.config.project.lower())
    safe_project = safe_project.strip("-_") or "app"
    compose_project = f"dploydb-{safe_project[:24]}-{operation_id.removeprefix('op_')[:16]}"
    network = subprocess.run(
        [
            "docker",
            "network",
            "ls",
            "--filter",
            f"label=com.docker.compose.project={compose_project}",
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert network.returncode == 0, network.stderr
    assert network.stdout.strip() == ""
    workspace = loaded.config.state_directory / "rehearsals"
    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []


SUCCESS_SMOKE = """
import json, os, sqlite3, urllib.request
with urllib.request.urlopen(os.environ["DPLOYDB_CANDIDATE_URL"], timeout=2) as response:
    assert response.status == 200 and json.load(response)["release"] == "v2"
with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
    assert connection.execute("PRAGMA user_version").fetchone() == (2,)
print(os.environ["API_TOKEN"])
"""


def test_real_v2_candidate_passes_while_current_v1_serves_and_all_evidence_is_clean(
    tmp_path: Path,
) -> None:
    secret = "real-candidate-success-secret"
    with current_v1(tmp_path) as (production, current_port):
        candidate_port = available_port()
        config_path, loaded, environment = configuration(
            tmp_path,
            production=production,
            candidate_port=candidate_port,
            release="v2",
            secret=secret,
            smoke=SUCCESS_SMOKE,
        )
        before = production_state(production)
        with monitoring_current(current_port) as monitor:
            result = validate_configured_candidate(
                loaded,
                version="v2",
                config_path=config_path,
                command_environment=environment,
            )

        assert monitor.successes >= 2
        assert monitor.failures == []
        assert production_state(production) == before
        assert result.health.smoke is not None
        assert result.health.smoke.stdout.text == "[REDACTED]\n"
        assert result.cleanup.proof.proven is True
        manifest, events = latest_operation(loaded)
        assert manifest.status is OperationStatus.SUCCEEDED
        assert manifest.stage == "candidate_healthy"
        assert [event.stage for event in events][-1] == "candidate_healthy"
        assert events[-2].evidence["candidate_cleanup"]["proof"]["proven"] is True
        assert_resources_clean(loaded, result.operation_id)

        produced = result.as_evidence().__repr__().encode()
        for path in tmp_path.rglob("*"):
            if path.is_file() and path != config_path and path != production:
                produced += path.read_bytes()
        assert secret.encode() not in produced


def test_real_broken_health_times_out_failed_safe_while_current_v1_keeps_serving(
    tmp_path: Path,
) -> None:
    secret = "real-broken-health-secret"
    with current_v1(tmp_path) as (production, current_port):
        candidate_port = available_port()
        config_path, loaded, environment = configuration(
            tmp_path,
            production=production,
            candidate_port=candidate_port,
            release="broken-health",
            secret=secret,
            smoke=None,
        )
        fast_application = loaded.config.application.model_copy(
            update={"startup_timeout_seconds": 2}
        )
        before = production_state(production)
        with CandidateHealthChecker(
            application=fast_application,
            database_environment_name=loaded.config.database.path_env,
            secrets=loaded.secrets,
            working_directory=config_path.parent,
            command_environment=environment,
            request_timeout_seconds=0.25,
            retry_interval_seconds=0.1,
        ) as health_checker:
            with monitoring_current(current_port) as monitor:
                with pytest.raises(OperationFailedError, match="deadline expired"):
                    validate_configured_candidate(
                        loaded,
                        version="broken-health",
                        config_path=config_path,
                        command_environment=environment,
                        health_checker=health_checker,
                    )

        assert monitor.successes >= 2
        assert monitor.failures == []
        assert production_state(production) == before
        manifest, events = latest_operation(loaded)
        assert manifest.status is OperationStatus.FAILED_SAFE
        assert manifest.failure is not None
        health_event = next(event for event in events if "candidate_failure" in event.evidence)
        readiness = health_event.evidence["candidate_failure"]["readiness"]
        assert readiness["last_attempt"]["status_code"] == 503
        assert "fixture_broken_health" in readiness["last_attempt"]["body"]["text"]
        assert_resources_clean(loaded, manifest.operation_id)


def test_real_occupied_port_startup_failure_is_durable_and_current_v1_continues(
    tmp_path: Path,
) -> None:
    secret = "real-occupied-port-secret"
    with current_v1(tmp_path) as (production, current_port):
        candidate_port = available_port()
        config_path, loaded, environment = configuration(
            tmp_path,
            production=production,
            candidate_port=candidate_port,
            release="v2",
            secret=secret,
            smoke=None,
        )
        before = production_state(production)
        blocker_database = (tmp_path / "blocker" / "rehearsal.db").resolve()
        create_v2_database(blocker_database)
        blocker_runner = DockerComposeCandidateRunner(
            project=loaded.config.project,
            application=loaded.config.application,
            database_environment_name=loaded.config.database.path_env,
            production_database_path=production,
            secrets=loaded.secrets,
            working_directory=config_path.parent,
            command_environment=environment,
            command_runner=SubprocessRunner(secrets=loaded.secrets),
        )
        blocker = blocker_runner.start(
            operation_id=str(new_operation_id()),
            version="v2",
            rehearsal_database_path=blocker_database,
        )
        try:
            with monitoring_current(current_port) as monitor:
                with pytest.raises(ExternalCommandError, match="startup"):
                    validate_configured_candidate(
                        loaded,
                        version="v2",
                        config_path=config_path,
                        command_environment=environment,
                    )
        finally:
            blocker_runner.stop(blocker.handle)

        assert monitor.successes >= 1
        assert monitor.failures == []
        assert production_state(production) == before
        manifest, events = latest_operation(loaded)
        assert manifest.status is OperationStatus.FAILED_SAFE
        start_failure = next(
            event.evidence["candidate_failure"]
            for event in events
            if "candidate_failure" in event.evidence
        )
        assert start_failure["kind"] == "candidate_start"
        assert start_failure["cleanup"]["proof"]["proven"] is True
        assert_resources_clean(loaded, manifest.operation_id)


def test_real_smoke_failure_is_durable_and_current_v1_continues(tmp_path: Path) -> None:
    secret = "real-smoke-failure-secret"
    smoke = "import os, sys; print(os.environ['API_TOKEN']); raise SystemExit(9)"
    with current_v1(tmp_path) as (production, current_port):
        candidate_port = available_port()
        config_path, loaded, environment = configuration(
            tmp_path,
            production=production,
            candidate_port=candidate_port,
            release="v2",
            secret=secret,
            smoke=smoke,
        )
        before = production_state(production)
        with monitoring_current(current_port) as monitor:
            with pytest.raises(OperationFailedError, match="status 9"):
                validate_configured_candidate(
                    loaded,
                    version="v2",
                    config_path=config_path,
                    command_environment=environment,
                )

        assert monitor.successes >= 2
        assert monitor.failures == []
        assert production_state(production) == before
        manifest, events = latest_operation(loaded)
        assert manifest.status is OperationStatus.FAILED_SAFE
        smoke_failure = next(
            event.evidence["candidate_failure"]
            for event in events
            if event.evidence.get("candidate_failure", {}).get("kind") == "smoke"
        )
        assert smoke_failure["smoke"]["outcome"] == "nonzero_exit"
        assert smoke_failure["smoke"]["stdout"]["text"] == "[REDACTED]\n"
        assert secret not in json.dumps(smoke_failure)
        assert_resources_clean(loaded, manifest.operation_id)
