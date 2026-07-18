"""Milestone 4A unit tests for the isolated Docker Compose candidate runner."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from dploydb.config import STARTER_CONFIGURATION, ApplicationConfig, parse_configuration
from dploydb.redaction import REDACTION_MARKER, SecretRegistry
from dploydb.runners.base import (
    CandidateCleanupError,
    CandidateHandle,
    CandidateInspectionError,
    CandidateStart,
    CandidateStartError,
)
from dploydb.runners.docker_compose import DockerComposeCandidateRunner
from dploydb.subprocesses import (
    CapturedOutput,
    CommandOutcome,
    CommandResult,
    TerminationReason,
)

OPERATION_ID = "op_0123456789abcdef0123456789abcdef"
CONTAINER_ID = "a" * 64


@dataclass(frozen=True)
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
                command=tuple(command),
                timeout_seconds=timeout_seconds,
                environment=dict(environment),
                working_directory=working_directory,
                cancellation_event=cancellation_event,
            )
        )
        if not self.results:
            raise AssertionError("fake executor ran more commands than expected")
        return self.results.pop(0)


def capture(text: str = "", *, truncated: bool = False) -> CapturedOutput:
    encoded = text.encode()
    if truncated:
        total = max(len(encoded) + 1, 2)
        retained = total - 1
    else:
        total = retained = len(encoded)
    return CapturedOutput(
        text=text,
        total_bytes=total,
        retained_bytes=retained,
        truncated=truncated,
    )


def result(
    *,
    outcome: CommandOutcome = CommandOutcome.SUCCEEDED,
    stdout: str = "",
    stderr: str = "",
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
) -> CommandResult:
    exit_code = 0
    start_error = None
    termination_reason = None
    termination_attempted = False
    if outcome is CommandOutcome.NONZERO_EXIT:
        exit_code = 1
    elif outcome is CommandOutcome.START_FAILED:
        exit_code = None
        start_error = "FileNotFoundError: docker"
    elif outcome is CommandOutcome.TIMED_OUT:
        exit_code = -15
        termination_reason = TerminationReason.TIMEOUT
        termination_attempted = True
    return CommandResult(
        command=("docker",),
        working_directory="/work",
        environment_keys=(),
        outcome=outcome,
        exit_code=exit_code,
        stdout=capture(stdout, truncated=stdout_truncated),
        stderr=capture(stderr, truncated=stderr_truncated),
        duration_seconds=0.1,
        termination_reason=termination_reason,
        termination_attempted=termination_attempted,
        start_error=start_error,
    )


def application(**updates: object) -> ApplicationConfig:
    base = parse_configuration(STARTER_CONFIGURATION).application
    return base.model_copy(update=updates)


def paths(tmp_path: Path) -> tuple[Path, Path]:
    production = tmp_path / "production" / "app.db"
    rehearsal = tmp_path / "rehearsal" / "rehearsal.db"
    production.parent.mkdir(parents=True)
    rehearsal.parent.mkdir(parents=True)
    production.write_bytes(b"production")
    rehearsal.write_bytes(b"rehearsal")
    return production.resolve(), rehearsal.resolve()


def runner(
    tmp_path: Path,
    fake: FakeExecutor,
    *,
    app: ApplicationConfig | None = None,
    secrets: SecretRegistry | None = None,
) -> tuple[DockerComposeCandidateRunner, Path]:
    production, rehearsal = paths(tmp_path)
    selected_secrets = secrets or SecretRegistry()
    return (
        DockerComposeCandidateRunner(
            project="Example.App",
            application=app or application(),
            database_environment_name="DATABASE_PATH",
            production_database_path=production,
            secrets=selected_secrets,
            working_directory=tmp_path.resolve(),
            command_environment={"PATH": "/usr/bin"},
            command_runner=fake,
        ),
        rehearsal,
    )


def successful_start(
    selected: DockerComposeCandidateRunner,
    rehearsal: Path,
) -> CandidateStart:
    return selected.start(
        operation_id=OPERATION_ID,
        version="v2",
        rehearsal_database_path=rehearsal,
    )


def inspection_payload(
    handle: CandidateHandle, production: Path | None = None
) -> dict[str, object]:
    name = handle.container_name
    project = handle.compose_project
    operation_id = handle.operation_id
    rehearsal = handle.rehearsal_database_path
    mounts: list[dict[str, object]] = [
        {
            "Type": "bind",
            "Source": str(Path(rehearsal).parent),
            "Destination": "/data",
            "RW": True,
        }
    ]
    if production is not None:
        mounts.append(
            {
                "Type": "bind",
                "Source": str(production.parent),
                "Destination": "/unsafe-production",
                "RW": True,
            }
        )
    return {
        "Id": "b" * 64,
        "Name": f"/{name}",
        "State": {"Running": True},
        "Config": {
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "app",
                "io.dploydb.operation_id": operation_id,
                "io.dploydb.role": "candidate",
            }
        },
        "Mounts": mounts,
        "NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4511"}]}},
    }


def cleanup_results(*, present_name: str | None = None) -> list[CommandResult]:
    values = [result(stdout="" if present_name is None else present_name + "\n")]
    if present_name is not None:
        values.append(result(stdout=present_name + "\n"))
    values.extend((result(), result(), result()))
    return values


def test_start_constructs_exact_isolated_argument_array_and_environment(tmp_path: Path) -> None:
    fake = FakeExecutor([result(stdout=CONTAINER_ID + "\n")])
    selected, rehearsal = runner(tmp_path, fake)

    started = successful_start(selected, rehearsal)

    handle = started.handle
    assert handle.compose_project == "dploydb-example-app-0123456789abcdef"
    assert handle.container_name == "dploydb-example-app-0123456789abcdef-candidate"
    assert handle.candidate_database_path == "/data/rehearsal.db"
    assert fake.calls == [
        Call(
            command=(
                "docker",
                "compose",
                "--file",
                "/srv/example/compose.yaml",
                "--project-name",
                handle.compose_project,
                "run",
                "--detach",
                "--no-TTY",
                "--no-deps",
                "--build",
                "--name",
                handle.container_name,
                "--label",
                f"io.dploydb.operation_id={OPERATION_ID}",
                "--label",
                "io.dploydb.role=candidate",
                "--publish",
                "127.0.0.1:4511:8080",
                "--volume",
                f"{rehearsal.parent}:/data:rw",
                "--env",
                "DATABASE_PATH=/data/rehearsal.db",
                "--env",
                "DPLOYDB_TEST_MODE=1",
                "app",
            ),
            timeout_seconds=45,
            environment={
                "PATH": "/usr/bin",
                "DPLOYDB_VERSION": "v2",
                "DPLOYDB_TEST_MODE": "1",
            },
            working_directory=tmp_path.resolve(),
            cancellation_event=None,
        )
    ]


@pytest.mark.parametrize(
    "version",
    ("-v2", "../v2", "v2/next", "v2..next", "", "x" * 65, "bad\x00value"),
)
def test_version_rejection_happens_before_compose_execution(tmp_path: Path, version: str) -> None:
    fake = FakeExecutor([])
    selected, rehearsal = runner(tmp_path, fake)

    with pytest.raises(ValueError, match="version"):
        selected.start(
            operation_id=OPERATION_ID,
            version=version,
            rehearsal_database_path=rehearsal,
        )

    assert fake.calls == []


def test_rehearsal_hard_link_to_production_is_rejected_before_compose(tmp_path: Path) -> None:
    fake = FakeExecutor([])
    selected, rehearsal = runner(tmp_path, fake)
    production = tmp_path / "production" / "app.db"
    rehearsal.unlink()
    rehearsal.hardlink_to(production)

    with pytest.raises(ValueError, match="must not alias"):
        successful_start(selected, rehearsal)

    assert fake.calls == []


def test_workspace_containing_production_is_rejected_before_compose(tmp_path: Path) -> None:
    fake = FakeExecutor([])
    selected, _rehearsal = runner(tmp_path, fake)
    unsafe_rehearsal = tmp_path / "production" / "rehearsal.db"
    unsafe_rehearsal.write_bytes(b"rehearsal")

    with pytest.raises(ValueError, match="must not contain the production"):
        successful_start(selected, unsafe_rehearsal.resolve())

    assert fake.calls == []


def test_resource_identity_depends_on_project_and_operation_not_version(tmp_path: Path) -> None:
    first_fake = FakeExecutor([result(stdout=CONTAINER_ID + "\n")])
    first, rehearsal = runner(tmp_path / "first", first_fake)
    first_handle = first.start(
        operation_id=OPERATION_ID,
        version="v1",
        rehearsal_database_path=rehearsal,
    ).handle
    second_fake = FakeExecutor([result(stdout=CONTAINER_ID + "\n")])
    second, second_rehearsal = runner(tmp_path / "second", second_fake)
    second_handle = second.start(
        operation_id=OPERATION_ID,
        version="release-2026.07",
        rehearsal_database_path=second_rehearsal,
    ).handle

    assert first_handle.compose_project == second_handle.compose_project
    assert first_handle.container_name == second_handle.container_name


def test_sensitive_test_environment_is_registered_before_execution(tmp_path: Path) -> None:
    secret = "direct-super-secret-123"
    registry = SecretRegistry()
    fake = FakeExecutor([result(stdout=CONTAINER_ID + "\n")])
    selected, rehearsal = runner(
        tmp_path,
        fake,
        app=application(test_mode_env={"API_TOKEN": secret}),
        secrets=registry,
    )

    successful_start(selected, rehearsal)

    assert registry.redact_text(secret) == REDACTION_MARKER
    assert f"API_TOKEN={secret}" in fake.calls[0].command


def test_real_subprocess_boundary_redacts_sensitive_candidate_arguments_and_output(
    tmp_path: Path,
) -> None:
    secret = "direct-super-secret-456"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" >&2\nprintf '%s\\n' '" + CONTAINER_ID + "'\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    production, rehearsal = paths(tmp_path)
    registry = SecretRegistry()
    selected = DockerComposeCandidateRunner(
        project="Example.App",
        application=application(test_mode_env={"API_TOKEN": secret}),
        database_environment_name="DATABASE_PATH",
        production_database_path=production,
        secrets=registry,
        working_directory=tmp_path.resolve(),
        command_environment={"PATH": str(fake_bin)},
    )

    started = successful_start(selected, rehearsal)

    assert secret not in " ".join(started.command.command)
    assert secret not in started.command.stderr.text
    assert REDACTION_MARKER in " ".join(started.command.command)
    assert REDACTION_MARKER in started.command.stderr.text


def test_inspect_accepts_only_expected_live_mount_port_and_labels(tmp_path: Path) -> None:
    start_result = result(stdout=CONTAINER_ID + "\n")
    fake = FakeExecutor([start_result])
    selected, rehearsal = runner(tmp_path, fake)
    started = successful_start(selected, rehearsal)
    payload = inspection_payload(started.handle)
    fake.results.append(result(stdout=json.dumps([payload])))

    inspected = selected.inspect(started.handle)

    assert inspected.running is True
    assert inspected.host_ip == "127.0.0.1"
    assert inspected.host_port == 4511
    assert inspected.container_port == 8080
    assert inspected.mounts[0].source == str(rehearsal.parent)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("stopped", "not running"),
        ("wrong_project", "project label"),
        ("wildcard_port", "loopback endpoint"),
        ("extra_port", "beyond"),
        ("wrong_database_mount", "rehearsal workspace"),
        ("production_mount", "production database"),
    ),
)
def test_inspect_rejects_contradictory_or_unsafe_docker_state(
    tmp_path: Path, mutation: str, message: str
) -> None:
    fake = FakeExecutor([result(stdout=CONTAINER_ID + "\n")])
    selected, rehearsal = runner(tmp_path, fake)
    started = successful_start(selected, rehearsal)
    production = tmp_path / "production" / "app.db"
    payload = inspection_payload(
        started.handle,
        production=production if mutation == "production_mount" else None,
    )
    if mutation == "stopped":
        payload["State"] = {"Running": False}
    elif mutation == "wrong_project":
        config = payload["Config"]
        assert isinstance(config, dict)
        labels = config["Labels"]
        assert isinstance(labels, dict)
        labels["com.docker.compose.project"] = "production"
    elif mutation == "wildcard_port":
        payload["NetworkSettings"] = {
            "Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "4511"}]}
        }
    elif mutation == "extra_port":
        payload["NetworkSettings"] = {
            "Ports": {
                "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4511"}],
                "9090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9090"}],
            }
        }
    elif mutation == "wrong_database_mount":
        mounts = payload["Mounts"]
        assert isinstance(mounts, list)
        mounts[0]["Source"] = str(tmp_path / "other")
    fake.results.append(result(stdout=json.dumps([payload])))

    with pytest.raises(CandidateInspectionError, match=message):
        selected.inspect(started.handle)


def test_collect_logs_preserves_bounded_truncated_evidence(tmp_path: Path) -> None:
    fake = FakeExecutor([result(stdout=CONTAINER_ID + "\n")])
    selected, rehearsal = runner(tmp_path, fake)
    started = successful_start(selected, rehearsal)
    fake.results.append(
        result(stdout="head\n...[output truncated]...\ntail", stdout_truncated=True)
    )

    logs = selected.collect_logs(started.handle)

    assert logs.command.stdout.truncated is True
    assert logs.command.stdout.text.endswith("tail")
    assert fake.calls[-1].command == (
        "docker",
        "container",
        "logs",
        started.handle.container_name,
    )


def test_stop_is_idempotent_and_proves_container_and_network_absence(tmp_path: Path) -> None:
    fake = FakeExecutor([result(stdout=CONTAINER_ID + "\n")])
    selected, rehearsal = runner(tmp_path, fake)
    started = successful_start(selected, rehearsal)
    fake.results.extend(cleanup_results() + cleanup_results())

    first = selected.stop(started.handle)
    second = selected.stop(started.handle)

    assert first.proof.proven is True
    assert second.proof.proven is True
    assert first.remove_command is None
    assert second.remove_command is None
    assert fake.calls[2].command[-4:] == (
        "down",
        "--remove-orphans",
        "--timeout",
        "45",
    )


def test_stop_removes_present_candidate_by_exact_derived_name(tmp_path: Path) -> None:
    fake = FakeExecutor([result(stdout=CONTAINER_ID + "\n")])
    selected, rehearsal = runner(tmp_path, fake)
    started = successful_start(selected, rehearsal)
    fake.results.extend(cleanup_results(present_name=started.handle.container_name))

    cleanup = selected.stop(started.handle)

    assert cleanup.remove_command is not None
    assert fake.calls[2].command == (
        "docker",
        "container",
        "rm",
        "--force",
        started.handle.container_name,
    )
    assert cleanup.proof.proven is True


def test_cleanup_command_failure_is_surfaced_even_when_absence_is_proven(tmp_path: Path) -> None:
    fake = FakeExecutor([result(stdout=CONTAINER_ID + "\n")])
    selected, rehearsal = runner(tmp_path, fake)
    started = successful_start(selected, rehearsal)
    fake.results.extend(
        [
            result(),
            result(outcome=CommandOutcome.NONZERO_EXIT, stderr="compose failed"),
            result(),
            result(),
        ]
    )

    with pytest.raises(CandidateCleanupError) as captured:
        selected.stop(started.handle)

    assert captured.value.cleanup_proven is True
    assert captured.value.cleanup is not None
    assert captured.value.cleanup.compose_down.exit_code == 1


def test_failed_start_preserves_primary_evidence_and_proven_cleanup(tmp_path: Path) -> None:
    fake = FakeExecutor(
        [result(outcome=CommandOutcome.NONZERO_EXIT, stderr="port is already allocated")]
        + cleanup_results()
    )
    selected, rehearsal = runner(tmp_path, fake)

    with pytest.raises(CandidateStartError, match="exited with status 1") as captured:
        successful_start(selected, rehearsal)

    assert captured.value.command is not None
    assert captured.value.command.stderr.text == "port is already allocated"
    assert captured.value.cleanup_proven is True


def test_timed_out_start_preserves_timeout_evidence_and_proven_cleanup(tmp_path: Path) -> None:
    fake = FakeExecutor([result(outcome=CommandOutcome.TIMED_OUT)] + cleanup_results())
    selected, rehearsal = runner(tmp_path, fake)

    with pytest.raises(CandidateStartError, match="timed out") as captured:
        successful_start(selected, rehearsal)

    assert captured.value.command is not None
    assert captured.value.command.termination_reason is TerminationReason.TIMEOUT
    assert captured.value.command.termination_attempted is True
    assert captured.value.cleanup_proven is True


def test_failed_start_surfaces_unproven_cleanup(tmp_path: Path) -> None:
    fake = FakeExecutor(
        [
            result(outcome=CommandOutcome.NONZERO_EXIT),
            result(),
            result(),
            result(stdout="candidate-still-present\n"),
            result(),
        ]
    )
    selected, rehearsal = runner(tmp_path, fake)

    with pytest.raises(CandidateStartError, match="cleanup could not be proven") as captured:
        successful_start(selected, rehearsal)

    assert captured.value.cleanup_proven is False
