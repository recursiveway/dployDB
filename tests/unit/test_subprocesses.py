"""Focused tests for bounded, redacted subprocess execution."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from dploydb.redaction import REDACTION_MARKER, SecretRegistry
from dploydb.subprocesses import (
    CommandOutcome,
    SubprocessRunner,
    TerminationReason,
)

PYTHON = str(Path(sys.executable).resolve())


def runner(
    *,
    secrets: SecretRegistry | None = None,
    max_output_bytes: int = 4096,
    grace: float = 0.3,
) -> SubprocessRunner:
    return SubprocessRunner(
        secrets=secrets if secrets is not None else SecretRegistry(),
        max_output_bytes=max_output_bytes,
        termination_grace_seconds=grace,
        poll_interval_seconds=0.01,
    )


def test_success_captures_both_streams_metadata_and_exact_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DPLOYDB_PARENT_ONLY", "must-not-be-inherited")
    code = """
import json, os, sys
print(json.dumps({
    "explicit": os.environ.get("DPLOYDB_EXPLICIT"),
    "parent": os.environ.get("DPLOYDB_PARENT_ONLY"),
}, sort_keys=True))
sys.stderr.write("diagnostic stderr\\n")
"""

    result = runner().run(
        [PYTHON, "-c", code],
        timeout_seconds=2,
        environment={"DPLOYDB_EXPLICIT": "present"},
        working_directory=tmp_path,
    )

    assert result.succeeded
    assert result.outcome is CommandOutcome.SUCCEEDED
    assert result.exit_code == 0
    assert json.loads(result.stdout.text) == {"explicit": "present", "parent": None}
    assert result.stderr.text == "diagnostic stderr\n"
    assert result.stdout.total_bytes == result.stdout.retained_bytes
    assert not result.stdout.truncated
    assert result.duration_seconds >= 0
    assert result.working_directory == str(tmp_path)
    assert result.environment_keys == ("DPLOYDB_EXPLICIT",)
    assert result.termination_reason is None
    assert not result.termination_attempted
    assert not result.forced_kill
    assert result.as_evidence()["outcome"] == "succeeded"


def test_nonzero_exit_preserves_redacted_output_and_exit_code(tmp_path: Path) -> None:
    result = runner().run(
        [
            PYTHON,
            "-c",
            "import sys; print('before failure'); sys.stderr.write('bad release\\n'); sys.exit(7)",
        ],
        timeout_seconds=2,
        environment={},
        working_directory=tmp_path,
    )

    assert not result.succeeded
    assert result.outcome is CommandOutcome.NONZERO_EXIT
    assert result.exit_code == 7
    assert result.stdout.text == "before failure\n"
    assert result.stderr.text == "bad release\n"


def test_missing_executable_returns_redacted_start_failure(tmp_path: Path) -> None:
    secret = "missing-secret-executable"
    registry = SecretRegistry()
    registry.register(secret)

    result = runner(secrets=registry).run(
        [str(tmp_path / secret)],
        timeout_seconds=2,
        environment={},
        working_directory=tmp_path,
    )

    assert result.outcome is CommandOutcome.START_FAILED
    assert result.exit_code is None
    assert result.start_error is not None
    assert secret not in result.start_error
    assert secret not in repr(result)
    assert REDACTION_MARKER in result.command[0]


@pytest.mark.parametrize(
    ("command", "error_type", "message"),
    [
        ([], ValueError, "at least one"),
        ("echo unsafe", TypeError, "argument sequence"),
        ([""], ValueError, "must not be empty"),
        (["echo", 4], TypeError, "must be strings"),
        (["echo", "bad\x00argument"], ValueError, "NUL"),
    ],
)
def test_invalid_command_is_rejected_before_spawn(
    command: Any,
    error_type: type[Exception],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("Popen must not be called"),
    )

    with pytest.raises(error_type, match=message):
        runner().run(command, timeout_seconds=1, environment={})


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_is_rejected_before_spawn(
    timeout: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("Popen must not be called"),
    )
    with pytest.raises(ValueError, match="finite and greater than zero"):
        runner().run([PYTHON, "-c", "pass"], timeout_seconds=timeout, environment={})


@pytest.mark.parametrize(
    "environment",
    [
        {"": "value"},
        {"BAD=NAME": "value"},
        {"BAD\x00NAME": "value"},
        {"NAME": "bad\x00value"},
        {4: "value"},
        {"NAME": 4},
    ],
)
def test_invalid_environment_is_rejected_before_spawn(
    environment: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("Popen must not be called"),
    )
    with pytest.raises((TypeError, ValueError)):
        runner().run([PYTHON, "-c", "pass"], timeout_seconds=1, environment=environment)


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [
        ({"max_output_bytes": 0}, ValueError),
        ({"max_output_bytes": 1.5}, TypeError),
        ({"termination_grace_seconds": 0}, ValueError),
        ({"poll_interval_seconds": float("inf")}, ValueError),
    ],
)
def test_runner_rejects_invalid_bounds(
    arguments: dict[str, Any], error_type: type[Exception]
) -> None:
    with pytest.raises(error_type):
        SubprocessRunner(secrets=SecretRegistry(), **arguments)


def test_popen_uses_no_shell_and_a_new_process_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: dict[str, Any] = {}

    def fail_to_start(*args: Any, **kwargs: Any) -> Any:
        received.update(kwargs)
        raise FileNotFoundError("deliberate start failure")

    monkeypatch.setattr(subprocess, "Popen", fail_to_start)
    result = runner().run(
        ["missing"],
        timeout_seconds=1,
        environment={"ONLY": "this"},
        working_directory=tmp_path,
    )

    assert result.outcome is CommandOutcome.START_FAILED
    assert received["shell"] is False
    assert received["start_new_session"] is True
    assert received["close_fds"] is True
    assert received["stdin"] is subprocess.DEVNULL
    assert received["env"] == {"ONLY": "this"}
    assert received["cwd"] == tmp_path


def test_timeout_terminates_a_hung_process(tmp_path: Path) -> None:
    result = runner().run(
        [PYTHON, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
        timeout_seconds=0.15,
        environment={},
        working_directory=tmp_path,
    )

    assert result.outcome is CommandOutcome.TIMED_OUT
    assert result.termination_reason is TerminationReason.TIMEOUT
    assert result.termination_attempted
    assert result.exit_code is not None
    assert result.stdout.text == "ready\n"
    assert result.duration_seconds < 2


def test_timeout_escalates_when_process_ignores_sigterm(tmp_path: Path) -> None:
    code = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(30)
"""
    result = runner(grace=0.1).run(
        [PYTHON, "-c", code],
        timeout_seconds=0.15,
        environment={},
        working_directory=tmp_path,
    )

    assert result.outcome is CommandOutcome.TIMED_OUT
    assert result.forced_kill
    assert result.exit_code == -signal.SIGKILL
    assert result.duration_seconds < 2


def test_cancellation_event_terminates_a_running_process(tmp_path: Path) -> None:
    cancellation = threading.Event()
    timer = threading.Timer(0.15, cancellation.set)
    timer.start()
    try:
        result = runner().run(
            [PYTHON, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            timeout_seconds=5,
            environment={},
            working_directory=tmp_path,
            cancellation_event=cancellation,
        )
    finally:
        timer.cancel()

    assert result.outcome is CommandOutcome.CANCELLED
    assert result.termination_reason is TerminationReason.CANCELLATION
    assert result.termination_attempted
    assert result.exit_code is not None
    assert result.duration_seconds < 2


def test_preexisting_cancellation_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    cancellation = threading.Event()
    cancellation.set()
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("cancelled command must not start"),
    )

    result = runner().run(
        [PYTHON, "-c", "pass"],
        timeout_seconds=1,
        environment={},
        cancellation_event=cancellation,
    )

    assert result.outcome is CommandOutcome.CANCELLED
    assert result.exit_code is None
    assert not result.termination_attempted


def test_large_stdout_and_stderr_are_drained_concurrently_and_truncated(tmp_path: Path) -> None:
    stream_bytes = 200_000
    result = runner(max_output_bytes=1024).run(
        [
            PYTHON,
            "-c",
            (f"import os; os.write(1, b'A' * {stream_bytes}); os.write(2, b'B' * {stream_bytes})"),
        ],
        timeout_seconds=3,
        environment={},
        working_directory=tmp_path,
    )

    assert result.succeeded
    for captured, expected in ((result.stdout, "A"), (result.stderr, "B")):
        assert captured.total_bytes == stream_bytes
        assert captured.retained_bytes == 1024
        assert captured.truncated
        assert "[output truncated]" in captured.text
        assert captured.text.startswith(expected * 512)
        assert captured.text.endswith(expected * 512)


def test_invalid_utf8_is_replaced_without_losing_byte_metadata(tmp_path: Path) -> None:
    result = runner().run(
        [PYTHON, "-c", "import os; os.write(1, b'valid\\xfftail')"],
        timeout_seconds=2,
        environment={},
        working_directory=tmp_path,
    )

    assert result.succeeded
    assert result.stdout.text == "valid�tail"
    assert result.stdout.total_bytes == 10


def test_secrets_are_absent_from_every_returned_diagnostic(tmp_path: Path) -> None:
    secret = "top-secret-command-value"
    code = """
import os, sys
value = os.environ["API_TOKEN"]
print(f"token={value}")
sys.stderr.write(f"Authorization: Bearer {value}\\n")
print(sys.argv[1:])
"""
    result = runner().run(
        [PYTHON, "-c", code, "--password", secret],
        timeout_seconds=2,
        environment={"API_TOKEN": secret},
        working_directory=tmp_path,
    )
    serialized = json.dumps(result.as_evidence(), sort_keys=True)

    assert result.succeeded
    assert secret not in serialized
    assert secret not in repr(result)
    assert REDACTION_MARKER in serialized
    assert result.command[-1] == REDACTION_MARKER


def test_cleanup_failure_is_bounded_and_preserves_the_timeout_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "pid"
    secret = "cleanup-secret"
    registry = SecretRegistry()
    registry.register(secret)
    command_runner = runner(secrets=registry, grace=0.05)
    original_signal = command_runner._signal_group

    def fail_sigkill(group_id: int, sig: signal.Signals) -> str | None:
        if sig is signal.SIGKILL:
            original_signal(group_id, sig)
            return f"SIGKILL failed token={secret}"
        return original_signal(group_id, sig)

    monkeypatch.setattr(command_runner, "_signal_group", fail_sigkill)
    code = """
import os, signal, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(30)
"""
    result = command_runner.run(
        [PYTHON, "-c", code, str(pid_path)],
        timeout_seconds=0.1,
        environment={},
        working_directory=tmp_path,
    )
    pid = int(pid_path.read_text())

    assert result.outcome is CommandOutcome.CLEANUP_FAILED
    assert result.termination_reason is TerminationReason.TIMEOUT
    assert result.forced_kill
    assert result.cleanup_error is not None
    assert secret not in result.cleanup_error
    assert REDACTION_MARKER in result.cleanup_error
    assert result.duration_seconds < 2
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
