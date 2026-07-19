from __future__ import annotations

import os
import socket
import sys
import threading
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from dploydb.config import ApplicationConfig
from dploydb.health import (
    APPLICATION_URL_ENV,
    CANDIDATE_URL_ENV,
    ApplicationHealthChecker,
    BoundedResponseEvidence,
    CandidateHealthChecker,
    HealthAttemptOutcome,
    ReadinessCheckError,
    SmokeCheckError,
)
from dploydb.redaction import REDACTION_MARKER, SecretRegistry
from dploydb.subprocesses import CommandResult, SubprocessRunner


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ForbiddenExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str],
        working_directory: Path | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> CommandResult:
        self.calls += 1
        raise AssertionError("smoke command ran before readiness")


def application(
    *,
    port: int = 4511,
    startup_timeout_seconds: int = 1,
    smoke_command: list[str] | None = None,
) -> ApplicationConfig:
    return ApplicationConfig.model_validate(
        {
            "runner": "docker_compose",
            "compose_file": "/tmp/compose.yaml",
            "service": "app",
            "candidate_port": port,
            "candidate_container_port": 8080,
            "database_volume_target": "/data",
            "candidate_health_url": f"http://127.0.0.1:{port}/health",
            "startup_timeout_seconds": startup_timeout_seconds,
            "smoke_command": smoke_command,
            "test_mode_env": {"DPLOYDB_TEST_MODE": "1"},
        }
    )


def database(tmp_path: Path) -> Path:
    path = (tmp_path / "rehearsal.db").resolve()
    path.touch()
    return path


def checker(
    tmp_path: Path,
    *,
    selected_application: ApplicationConfig,
    secrets: SecretRegistry | None = None,
    transport: httpx.BaseTransport | None = None,
    command_runner: object | None = None,
    command_environment: Mapping[str, str] | None = None,
    max_response_bytes: int = 64 * 1024,
    clock: FakeClock | None = None,
) -> CandidateHealthChecker:
    registry = secrets or SecretRegistry()
    return CandidateHealthChecker(
        application=selected_application,
        database_environment_name="DATABASE_PATH",
        secrets=registry,
        working_directory=tmp_path.resolve(),
        command_environment={} if command_environment is None else command_environment,
        command_runner=command_runner,  # type: ignore[arg-type]
        transport=transport,
        request_timeout_seconds=0.1,
        retry_interval_seconds=0.25,
        max_response_bytes=max_response_bytes,
        clock=clock or __import__("time").monotonic,
        sleeper=(clock.sleep if clock is not None else __import__("time").sleep),
    )


def test_real_loopback_retries_http_500_and_503_until_2xx(tmp_path: Path) -> None:
    statuses = [500, 503, 204]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            status = statuses.pop(0)
            body = f"status={status}".encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with checker(
            tmp_path,
            selected_application=application(port=port, startup_timeout_seconds=2),
        ) as selected:
            result = selected.check(version="v2", rehearsal_database_path=database(tmp_path))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.readiness.attempt_count == 3
    assert result.readiness.last_attempt is not None
    assert result.readiness.last_attempt.outcome is HealthAttemptOutcome.HEALTHY
    assert result.readiness.last_attempt.status_code == 204
    assert result.smoke is None


def test_real_connection_refusal_expires_under_the_fixed_deadline(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])

    with checker(tmp_path, selected_application=application(port=port)) as selected:
        with pytest.raises(ReadinessCheckError) as captured:
            selected.check(version="v2", rehearsal_database_path=database(tmp_path))

    evidence = captured.value.evidence
    assert evidence.healthy is False
    assert evidence.attempt_count > 0
    assert evidence.duration_seconds < 1.5
    assert evidence.last_attempt is not None
    assert evidence.last_attempt.outcome is HealthAttemptOutcome.TRANSPORT_ERROR
    assert "deadline expired" in evidence.reason


def test_redirect_is_never_followed_and_deadline_uses_monotonic_clock(tmp_path: Path) -> None:
    requested: list[str] = []
    fake_clock = FakeClock()

    def redirect(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://example.com/unsafe"})

    with checker(
        tmp_path,
        selected_application=application(),
        transport=httpx.MockTransport(redirect),
        clock=fake_clock,
    ) as selected:
        with pytest.raises(ReadinessCheckError) as captured:
            selected.check(version="v2", rehearsal_database_path=database(tmp_path))

    evidence = captured.value.evidence
    assert evidence.duration_seconds == 1.0
    assert evidence.last_attempt is not None
    assert evidence.last_attempt.outcome is HealthAttemptOutcome.REDIRECT_REFUSED
    assert evidence.last_attempt.status_code == 302
    assert requested
    assert all(url == "http://127.0.0.1:4511/health" for url in requested)


def test_oversized_response_is_bounded_and_redacted_but_2xx_is_healthy(
    tmp_path: Path,
) -> None:
    secret = "health-response-secret"
    registry = SecretRegistry()
    registry.register(secret)
    body = (secret + "-" + "x" * 500 + "-" + secret).encode()

    with checker(
        tmp_path,
        selected_application=application(),
        secrets=registry,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body)),
        max_response_bytes=96,
    ) as selected:
        result = selected.check(version="v2", rehearsal_database_path=database(tmp_path))

    attempt = result.readiness.last_attempt
    assert attempt is not None
    assert attempt.body == BoundedResponseEvidence(
        text=attempt.body.text,
        total_bytes=len(body),
        retained_bytes=96,
        truncated=True,
    )
    assert secret not in attempt.body.text
    assert REDACTION_MARKER in attempt.body.text


def test_pre_cancelled_readiness_makes_no_request(tmp_path: Path) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200)

    cancellation = threading.Event()
    cancellation.set()
    with checker(
        tmp_path,
        selected_application=application(),
        transport=httpx.MockTransport(handler),
    ) as selected:
        with pytest.raises(ReadinessCheckError) as captured:
            selected.check(
                version="v2",
                rehearsal_database_path=database(tmp_path),
                cancellation_event=cancellation,
            )

    assert requests == 0
    assert captured.value.evidence.attempt_count == 0
    assert "cancelled" in captured.value.evidence.reason


def test_smoke_runs_only_after_readiness_and_receives_candidate_environment(
    tmp_path: Path,
) -> None:
    secret = "candidate-smoke-secret"
    code = """
import os
assert os.environ["DPLOYDB_VERSION"] == "v2"
assert os.environ["DPLOYDB_CANDIDATE_URL"].endswith("/health")
assert os.path.isfile(os.environ["DATABASE_PATH"])
print(os.environ["API_TOKEN"])
"""
    registry = SecretRegistry()
    selected_application = application(smoke_command=[sys.executable, "-c", code])
    with checker(
        tmp_path,
        selected_application=selected_application,
        secrets=registry,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
        command_environment={"API_TOKEN": secret},
    ) as selected:
        result = selected.check(version="v2", rehearsal_database_path=database(tmp_path))

    assert result.smoke is not None
    assert result.smoke.succeeded is True
    assert result.smoke.stdout.text == f"{REDACTION_MARKER}\n"
    assert CANDIDATE_URL_ENV in result.smoke.environment_keys
    assert secret not in str(result.as_evidence())


def test_generic_application_health_uses_supplied_url_without_candidate_test_mode(
    tmp_path: Path,
) -> None:
    code = """
import os
assert os.environ["DPLOYDB_VERSION"] == "v2"
assert os.environ["DPLOYDB_APPLICATION_URL"] == "http://127.0.0.1:4510/health"
assert os.path.isfile(os.environ["DATABASE_PATH"])
assert "DPLOYDB_TEST_MODE" not in os.environ
"""
    selected_application = application(smoke_command=[sys.executable, "-c", code])
    with ApplicationHealthChecker(
        application=selected_application,
        health_url="http://127.0.0.1:4510/health",
        database_environment_name="DATABASE_PATH",
        secrets=SecretRegistry(),
        working_directory=tmp_path.resolve(),
        command_environment={},
        command_runner=SubprocessRunner(secrets=SecretRegistry()),
        transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
        request_timeout_seconds=0.1,
        retry_interval_seconds=0.1,
    ) as selected:
        result = selected.check_application(
            version="v2",
            database_path=database(tmp_path),
        )

    assert result.readiness.healthy is True
    assert result.smoke is not None and result.smoke.succeeded is True
    assert APPLICATION_URL_ENV in result.smoke.environment_keys


@pytest.mark.parametrize(
    ("command", "match"),
    [
        ([sys.executable, "-c", "raise SystemExit(7)"], "status 7"),
        (["/definitely/missing/dploydb-smoke"], "could not start"),
    ],
)
def test_smoke_nonzero_and_start_failure_are_typed(
    tmp_path: Path,
    command: list[str],
    match: str,
) -> None:
    with checker(
        tmp_path,
        selected_application=application(smoke_command=command),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
    ) as selected:
        with pytest.raises(SmokeCheckError, match=match) as captured:
            selected.check(version="v2", rehearsal_database_path=database(tmp_path))

    assert captured.value.readiness.healthy is True
    assert captured.value.cleanup_proven is True


def test_truncated_smoke_output_cannot_pass(tmp_path: Path) -> None:
    registry = SecretRegistry()
    runner = SubprocessRunner(secrets=registry, max_output_bytes=32)
    with checker(
        tmp_path,
        selected_application=application(
            smoke_command=[sys.executable, "-c", "print('x' * 10000)"]
        ),
        secrets=registry,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
        command_runner=runner,
    ) as selected:
        with pytest.raises(SmokeCheckError, match="complete-capture") as captured:
            selected.check(version="v2", rehearsal_database_path=database(tmp_path))

    assert captured.value.command.stdout.truncated is True


def test_timed_out_smoke_terminates_its_descendant(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "smoke-child.pid"
    code = """
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
print("smoke tree ready", flush=True)
time.sleep(30)
"""
    registry = SecretRegistry()
    runner = SubprocessRunner(
        secrets=registry,
        termination_grace_seconds=0.2,
        poll_interval_seconds=0.02,
    )
    with checker(
        tmp_path,
        selected_application=application(
            smoke_command=[sys.executable, "-c", code, str(child_pid_path)]
        ),
        secrets=registry,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
        command_runner=runner,
    ) as selected:
        with pytest.raises(SmokeCheckError, match="timed out") as captured:
            selected.check(version="v2", rehearsal_database_path=database(tmp_path))

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert captured.value.command.termination_attempted is True
    assert captured.value.cleanup_proven is True


def test_smoke_never_runs_when_readiness_fails(tmp_path: Path) -> None:
    fake_clock = FakeClock()
    executor = ForbiddenExecutor()
    selected_application = application(smoke_command=[sys.executable, "-c", "pass"])
    with checker(
        tmp_path,
        selected_application=selected_application,
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
        command_runner=executor,
        clock=fake_clock,
    ) as selected:
        with pytest.raises(ReadinessCheckError):
            selected.check(version="v2", rehearsal_database_path=database(tmp_path))

    assert executor.calls == 0
