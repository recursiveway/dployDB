"""Tests for the exact-container Docker Compose production lifecycle."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from dploydb.config import ApplicationConfig, ProductionTopology
from dploydb.models import ProductionApplicationHandle
from dploydb.redaction import SecretRegistry
from dploydb.runners.base import (
    ProductionCleanupError,
    ProductionDiscoveryError,
    ProductionInspectionError,
    ProductionStartError,
)
from dploydb.runners.docker_compose_production import (
    RELEASE_LABEL,
    ROLE_PRODUCTION_RELEASE,
    DockerComposeProductionRunner,
)
from dploydb.subprocesses import CapturedOutput, CommandOutcome, CommandResult

OPERATION_ID = "op_" + "1" * 32
RELEASE_ID = "release_" + "2" * 32
CONTAINER_ID = "a" * 64
NEW_CONTAINER_ID = "b" * 64


@dataclass(frozen=True, slots=True)
class Call:
    command: tuple[str, ...]
    timeout_seconds: float
    environment: dict[str, str]
    working_directory: Path | None
    cancellation_event: threading.Event | None


class FakeExecutor:
    def __init__(self, results: Sequence[CommandResult]) -> None:
        self.results = list(results)
        self.calls: list[Call] = []

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str],
        working_directory: Path | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> CommandResult:
        self.calls.append(
            Call(
                tuple(command),
                timeout_seconds,
                dict(environment),
                working_directory,
                cancellation_event,
            )
        )
        if not self.results:
            raise AssertionError(f"unexpected command: {tuple(command)!r}")
        return self.results.pop(0)


def capture(text: str = "", *, truncated: bool = False) -> CapturedOutput:
    retained = len(text.encode())
    return CapturedOutput(
        text=text,
        total_bytes=retained + (1 if truncated else 0),
        retained_bytes=retained,
        truncated=truncated,
    )


def result(
    action: str,
    *,
    stdout: str = "",
    outcome: CommandOutcome = CommandOutcome.SUCCEEDED,
    exit_code: int | None = 0,
    truncated: bool = False,
) -> CommandResult:
    return CommandResult(
        command=("fake", action),
        working_directory="/work",
        environment_keys=(),
        outcome=outcome,
        exit_code=exit_code,
        stdout=capture(stdout, truncated=truncated),
        stderr=capture(),
        duration_seconds=0.01,
        start_error="missing" if outcome is CommandOutcome.START_FAILED else None,
    )


def application(tmp_path: Path) -> ApplicationConfig:
    return ApplicationConfig.model_validate(
        {
            "runner": "docker_compose",
            "compose_file": str((tmp_path / "compose.yaml").resolve()),
            "service": "app",
            "production_project": "example-current",
            "production_port": 4510,
            "production_health_url": "http://127.0.0.1:4510/health",
            "candidate_port": 4511,
            "candidate_container_port": 8080,
            "database_volume_target": "/data",
            "candidate_health_url": "http://127.0.0.1:4511/health",
            "startup_timeout_seconds": 13,
            "test_mode_env": {"DPLOYDB_TEST_MODE": "1"},
        }
    )


def topology() -> ProductionTopology:
    return ProductionTopology(
        compose_project="example-current",
        host_port=4510,
        health_url="http://127.0.0.1:4510/health",
    )


def paths(tmp_path: Path) -> Path:
    database = (tmp_path / "production" / "app.db").resolve()
    database.parent.mkdir(parents=True)
    database.write_bytes(b"production")
    return database


def selected_runner(
    tmp_path: Path,
    results: Sequence[CommandResult],
) -> tuple[DockerComposeProductionRunner, FakeExecutor, Path]:
    executor = FakeExecutor(results)
    database = paths(tmp_path)
    selected = DockerComposeProductionRunner(
        project="example",
        application=application(tmp_path),
        topology=topology(),
        database_environment_name="DATABASE_PATH",
        production_database_path=database,
        secrets=SecretRegistry(),
        working_directory=tmp_path.resolve(),
        command_environment={
            "BASE": "yes",
            "DPLOYDB_VERSION": "v1",
            "DPLOYDB_TEST_MODE": "must-remove",
            "DPLOYDB_CANDIDATE_URL": "must-remove",
        },
        command_runner=executor,
    )
    return selected, executor, database


def identity() -> tuple[str, str]:
    project = "dploydb-example-release-" + "2" * 16
    return project, f"{project}-app"


def inspection_payload(
    database: Path,
    *,
    container_id: str = CONTAINER_ID,
    container_name: str = "example-current-app-1",
    project: str = "example-current",
    running: bool = True,
    release: bool = False,
    mount_source: Path | None = None,
    host_ip: str = "127.0.0.1",
    host_port: int = 4510,
    database_assignment: str = "DATABASE_PATH=/data/app.db",
    labels_update: dict[str, str] | None = None,
) -> str:
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.service": "app",
    }
    if release:
        labels.update(
            {
                "io.dploydb.role": ROLE_PRODUCTION_RELEASE,
                "io.dploydb.operation_id": OPERATION_ID,
                RELEASE_LABEL: RELEASE_ID,
            }
        )
    labels.update(labels_update or {})
    return json.dumps(
        [
            {
                "Id": container_id,
                "Name": f"/{container_name}",
                "State": {"Running": running},
                "Config": {
                    "Labels": labels,
                    "Env": [database_assignment, "OTHER=value"],
                },
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": str((mount_source or database.parent).resolve()),
                        "Destination": "/data",
                        "RW": True,
                    }
                ],
                "NetworkSettings": {
                    "Ports": (
                        {"8080/tcp": [{"HostIp": host_ip, "HostPort": str(host_port)}]}
                        if running
                        else {}
                    )
                },
                "HostConfig": {
                    "PortBindings": {"8080/tcp": [{"HostIp": host_ip, "HostPort": str(host_port)}]}
                },
            }
        ]
    )


def bootstrap_handle(database: Path) -> ProductionApplicationHandle:
    return ProductionApplicationHandle(
        source="bootstrap",
        container_id=CONTAINER_ID,
        container_name="example-current-app-1",
        compose_project="example-current",
        compose_service="app",
        version=None,
        release_id=None,
        operation_id=None,
        database_directory=database.parent,
        database_target="/data",
        host_port=4510,
        container_port=8080,
        health_url="http://127.0.0.1:4510/health",
    )


def release_handle(database: Path) -> ProductionApplicationHandle:
    project, name = identity()
    return ProductionApplicationHandle(
        source="release",
        container_id=NEW_CONTAINER_ID,
        container_name=name,
        compose_project=project,
        compose_service="app",
        version="v2",
        release_id=RELEASE_ID,
        operation_id=OPERATION_ID,
        database_directory=database.parent,
        database_target="/data",
        host_port=4510,
        container_port=8080,
        health_url="http://127.0.0.1:4510/health",
    )


def cleanup_results(*, container_present: bool = False) -> list[CommandResult]:
    _project, container_name = identity()
    results = [result("presence", stdout=f"{container_name}\n" if container_present else "")]
    if container_present:
        results.append(result("remove"))
    results.extend([result("down"), result("container-proof"), result("network-proof")])
    return results


def test_discover_current_requires_one_live_validated_compose_service(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "production" / "app.db").resolve()
    runner, executor, _created = selected_runner(
        tmp_path,
        [
            result("ps", stdout=f"{CONTAINER_ID}\n"),
            result("inspect", stdout=inspection_payload(database)),
        ],
    )

    discovered = runner.discover_current()

    assert discovered.inspection.running is True
    assert discovered.inspection.handle == bootstrap_handle(database)
    assert executor.calls[0].command == (
        "docker",
        "compose",
        "--file",
        str(application(tmp_path).compose_file),
        "--project-name",
        "example-current",
        "ps",
        "--all",
        "--quiet",
        "app",
    )
    assert executor.calls[1].command == (
        "docker",
        "container",
        "inspect",
        CONTAINER_ID,
    )


@pytest.mark.parametrize("stdout", ["", f"{CONTAINER_ID}\n{'c' * 64}\n", "not-an-id\n"])
def test_discovery_rejects_missing_multiple_or_invalid_identity(
    tmp_path: Path,
    stdout: str,
) -> None:
    runner, executor, _database = selected_runner(tmp_path, [result("ps", stdout=stdout)])

    with pytest.raises(ProductionDiscoveryError):
        runner.discover_current()

    assert len(executor.calls) == 1


def test_stop_preserves_exact_previous_container_and_proves_stopped(tmp_path: Path) -> None:
    database = (tmp_path / "production" / "app.db").resolve()
    runner, executor, _created = selected_runner(
        tmp_path,
        [
            result("stop", stdout="example-current-app-1\n"),
            result("inspect", stdout=inspection_payload(database, running=False)),
        ],
    )
    handle = bootstrap_handle(database)

    stopped = runner.stop_current(handle)

    assert stopped.handle == handle
    assert stopped.inspection.running is False
    assert executor.calls[0].command == (
        "docker",
        "container",
        "stop",
        "--time",
        "13",
        CONTAINER_ID,
    )
    assert all("rm" not in call.command for call in executor.calls)


def test_start_new_uses_release_project_production_mount_and_no_test_environment(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "production" / "app.db").resolve()
    project, name = identity()
    runner, executor, _created = selected_runner(
        tmp_path,
        [
            result("run", stdout=f"{NEW_CONTAINER_ID}\n"),
            result(
                "inspect",
                stdout=inspection_payload(
                    database,
                    container_id=NEW_CONTAINER_ID,
                    container_name=name,
                    project=project,
                    release=True,
                ),
            ),
        ],
    )

    started = runner.start_new(
        operation_id=OPERATION_ID,
        release_id=RELEASE_ID,
        version="v2",
    )

    assert started.handle == release_handle(database)
    command = executor.calls[0].command
    assert command[:6] == (
        "docker",
        "compose",
        "--file",
        str(application(tmp_path).compose_file),
        "--project-name",
        project,
    )
    assert "127.0.0.1:4510:8080" in command
    assert f"{database.parent}:/data:rw" in command
    assert "DATABASE_PATH=/data/app.db" in command
    assert f"io.dploydb.release_id={RELEASE_ID}" in command
    assert executor.calls[0].environment["DPLOYDB_VERSION"] == "v2"
    assert "DPLOYDB_TEST_MODE" not in executor.calls[0].environment
    assert "DPLOYDB_CANDIDATE_URL" not in executor.calls[0].environment


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        ({"running": False}, "running state"),
        ({"project": "wrong"}, "project label"),
        ({"mount_source": Path("/tmp/wrong")}, "database target"),
        ({"host_ip": "0.0.0.0"}, "loopback"),
        ({"host_port": 9999}, "loopback"),
        ({"database_assignment": "DATABASE_PATH=/wrong.db"}, "database environment"),
        ({"labels_update": {RELEASE_LABEL: "release_" + "9" * 32}}, "release label"),
    ),
)
def test_release_inspection_rejects_contradictory_live_state(
    tmp_path: Path,
    mutation: dict[str, Any],
    match: str,
) -> None:
    database = (tmp_path / "production" / "app.db").resolve()
    project, name = identity()
    runner, _executor, _created = selected_runner(
        tmp_path,
        [
            result(
                "inspect",
                stdout=inspection_payload(
                    database,
                    **{
                        "container_id": NEW_CONTAINER_ID,
                        "container_name": name,
                        "project": project,
                        "release": True,
                        **mutation,
                    },
                ),
            )
        ],
    )

    with pytest.raises(ProductionInspectionError, match=match):
        runner.inspect(release_handle(database), expected_running=True)


def test_remove_new_is_exact_idempotent_and_never_targets_previous(tmp_path: Path) -> None:
    database = (tmp_path / "production" / "app.db").resolve()
    project, name = identity()
    runner, executor, _created = selected_runner(
        tmp_path,
        cleanup_results(container_present=True) + cleanup_results(container_present=False),
    )
    handle = release_handle(database)

    first = runner.remove_new(handle)
    second = runner.remove_new(handle)

    assert first.proof.proven and second.proof.proven
    commands = [call.command for call in executor.calls]
    assert ("docker", "container", "rm", "--force", name) in commands
    assert all(CONTAINER_ID not in command for command in commands)
    assert any(project in command for command in commands)


def test_failed_start_preserves_primary_evidence_and_cleanup_proof(tmp_path: Path) -> None:
    runner, _executor, _database = selected_runner(
        tmp_path,
        [
            result("run", outcome=CommandOutcome.NONZERO_EXIT, exit_code=7),
            *cleanup_results(container_present=False),
        ],
    )

    with pytest.raises(ProductionStartError, match="status 7") as captured:
        runner.start_new(
            operation_id=OPERATION_ID,
            release_id=RELEASE_ID,
            version="v2",
        )

    assert captured.value.cleanup_proven is True


def test_unproven_cleanup_is_surfaced(tmp_path: Path) -> None:
    database = (tmp_path / "production" / "app.db").resolve()
    failed_proof = cleanup_results(container_present=False)
    failed_proof[-1] = result("network-proof", stdout="network-id\n")
    runner, _executor, _created = selected_runner(tmp_path, failed_proof)

    with pytest.raises(ProductionCleanupError) as captured:
        runner.remove_new(release_handle(database))

    assert captured.value.cleanup_proven is False


def test_restart_previous_uses_exact_container_and_proves_running(tmp_path: Path) -> None:
    database = (tmp_path / "production" / "app.db").resolve()
    runner, executor, _created = selected_runner(
        tmp_path,
        [
            result("start", stdout="example-current-app-1\n"),
            result("inspect", stdout=inspection_payload(database)),
        ],
    )
    previous = bootstrap_handle(database)

    restarted = runner.restart_previous(previous)

    assert restarted.inspection.running is True
    assert executor.calls[0].command == (
        "docker",
        "container",
        "start",
        CONTAINER_ID,
    )


def test_logs_are_bounded_evidence_from_exact_container(tmp_path: Path) -> None:
    database = (tmp_path / "production" / "app.db").resolve()
    runner, executor, _created = selected_runner(
        tmp_path,
        [result("logs", stdout="ready\n", truncated=True)],
    )

    logs = runner.collect_logs(release_handle(database))

    assert logs.command.stdout.truncated is True
    assert executor.calls[0].command == (
        "docker",
        "container",
        "logs",
        NEW_CONTAINER_ID,
    )


def test_invalid_release_identity_is_rejected_before_compose_execution(tmp_path: Path) -> None:
    runner, executor, _database = selected_runner(tmp_path, [])

    with pytest.raises(ValueError, match="release_id"):
        runner.start_new(
            operation_id=OPERATION_ID,
            release_id="../unsafe",
            version="v2",
        )

    assert executor.calls == []
