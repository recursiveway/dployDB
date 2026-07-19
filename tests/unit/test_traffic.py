"""Tests for bounded command-based maintenance and traffic hooks."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from dploydb.config import TrafficConfig
from dploydb.redaction import REDACTION_MARKER, SecretRegistry
from dploydb.subprocesses import (
    CapturedOutput,
    CommandOutcome,
    CommandResult,
    TerminationReason,
)
from dploydb.traffic import CommandTrafficController, TrafficAction


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
                command=tuple(command),
                timeout_seconds=timeout_seconds,
                environment=dict(environment),
                working_directory=working_directory,
                cancellation_event=cancellation_event,
            )
        )
        return self.results.pop(0)


def capture(text: str = "", *, truncated: bool = False) -> CapturedOutput:
    total = len(text.encode()) + (1 if truncated else 0)
    retained = len(text.encode())
    return CapturedOutput(
        text=text,
        total_bytes=total,
        retained_bytes=retained,
        truncated=truncated,
    )


def result(
    action: str,
    *,
    outcome: CommandOutcome = CommandOutcome.SUCCEEDED,
    exit_code: int | None = 0,
    stdout: CapturedOutput | None = None,
) -> CommandResult:
    return CommandResult(
        command=("hook", action),
        working_directory="/work",
        environment_keys=(),
        outcome=outcome,
        exit_code=exit_code,
        stdout=stdout or capture(),
        stderr=capture(),
        duration_seconds=0.01,
        termination_reason=(
            TerminationReason.TIMEOUT if outcome is CommandOutcome.TIMED_OUT else None
        ),
        termination_attempted=outcome is CommandOutcome.TIMED_OUT,
        start_error="missing" if outcome is CommandOutcome.START_FAILED else None,
    )


def traffic(
    *,
    maintenance_on: list[str] | None = None,
    timeout_seconds: int = 7,
) -> TrafficConfig:
    return TrafficConfig.model_validate(
        {
            "maintenance_on_command": maintenance_on or ["hook", "maintenance-on"],
            "maintenance_off_command": ["hook", "maintenance-off"],
            "activate_new_command": ["hook", "activate-new"],
            "activate_old_command": ["hook", "activate-old"],
            "timeout_seconds": timeout_seconds,
        }
    )


def test_all_hooks_use_exact_arrays_timeout_environment_and_working_directory(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor(
        [
            result("maintenance-on"),
            result("maintenance-off"),
            result("activate-new"),
            result("activate-old"),
        ]
    )
    cancellation = threading.Event()
    controller = CommandTrafficController(
        traffic=traffic(),
        secrets=SecretRegistry(),
        working_directory=tmp_path.resolve(),
        command_environment={"EXACT": "value"},
        command_runner=executor,
    )

    results = (
        controller.enable_maintenance(cancellation_event=cancellation),
        controller.disable_maintenance(cancellation_event=cancellation),
        controller.activate_new(cancellation_event=cancellation),
        controller.activate_old(cancellation_event=cancellation),
    )

    assert [item.action for item in results] == list(TrafficAction)
    assert all(item.passed for item in results)
    assert [call.command for call in executor.calls] == [
        ("hook", "maintenance-on"),
        ("hook", "maintenance-off"),
        ("hook", "activate-new"),
        ("hook", "activate-old"),
    ]
    assert all(call.timeout_seconds == 7 for call in executor.calls)
    assert all(call.environment == {"EXACT": "value"} for call in executor.calls)
    assert all(call.working_directory == tmp_path.resolve() for call in executor.calls)
    assert all(call.cancellation_event is cancellation for call in executor.calls)


@pytest.mark.parametrize(
    "selected",
    (
        result("nonzero", outcome=CommandOutcome.NONZERO_EXIT, exit_code=9),
        result("missing", outcome=CommandOutcome.START_FAILED, exit_code=None),
        result("timeout", outcome=CommandOutcome.TIMED_OUT, exit_code=-15),
        result("truncated", stdout=capture("partial", truncated=True)),
    ),
)
def test_no_unsuccessful_or_incomplete_hook_is_reported_passed(
    tmp_path: Path,
    selected: CommandResult,
) -> None:
    controller = CommandTrafficController(
        traffic=traffic(),
        secrets=SecretRegistry(),
        working_directory=tmp_path.resolve(),
        command_environment={},
        command_runner=FakeExecutor([selected]),
    )

    outcome = controller.enable_maintenance()

    assert outcome.passed is False
    assert outcome.as_evidence()["passed"] is False
    assert outcome.command == selected


def test_pre_cancelled_hook_never_starts(tmp_path: Path) -> None:
    cancellation = threading.Event()
    cancellation.set()
    controller = CommandTrafficController(
        traffic=traffic(maintenance_on=[sys.executable, "-c", "raise SystemExit(99)"]),
        secrets=SecretRegistry(),
        working_directory=tmp_path.resolve(),
        command_environment={},
    )

    selected = controller.enable_maintenance(cancellation_event=cancellation)

    assert selected.passed is False
    assert selected.command.outcome is CommandOutcome.CANCELLED
    assert selected.command.exit_code is None


def test_real_hook_timeout_terminates_parent_and_descendant(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    script = """
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
time.sleep(60)
"""
    controller = CommandTrafficController(
        traffic=traffic(
            maintenance_on=[sys.executable, "-c", script, str(child_pid_path)],
            timeout_seconds=1,
        ),
        secrets=SecretRegistry(),
        working_directory=tmp_path.resolve(),
        command_environment=dict(os.environ),
    )

    selected = controller.enable_maintenance()

    assert selected.passed is False
    assert selected.command.outcome is CommandOutcome.TIMED_OUT
    assert selected.command.termination_attempted is True
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while _process_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _process_exists(child_pid)


def test_real_hook_redacts_sensitive_environment_and_rejects_truncated_success(
    tmp_path: Path,
) -> None:
    secret = "traffic-hook-secret"
    registry = SecretRegistry()
    print_secret = CommandTrafficController(
        traffic=traffic(
            maintenance_on=[
                sys.executable,
                "-c",
                "import os; print(os.environ['API_TOKEN'])",
            ]
        ),
        secrets=registry,
        working_directory=tmp_path.resolve(),
        command_environment={**os.environ, "API_TOKEN": secret},
    )

    redacted = print_secret.enable_maintenance()

    assert redacted.passed is True
    assert redacted.command.stdout.text == f"{REDACTION_MARKER}\n"
    assert secret not in str(redacted.as_evidence())

    oversized = CommandTrafficController(
        traffic=traffic(
            maintenance_on=[sys.executable, "-c", "print('x' * 300000)"],
        ),
        secrets=SecretRegistry(),
        working_directory=tmp_path.resolve(),
        command_environment=dict(os.environ),
    ).enable_maintenance()

    assert oversized.command.outcome is CommandOutcome.SUCCEEDED
    assert oversized.command.stdout.truncated is True
    assert oversized.passed is False


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
