"""Real-process proof that bounded command cleanup includes descendants."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dploydb.redaction import SecretRegistry
from dploydb.subprocesses import CommandOutcome, SubprocessRunner

PYTHON = str(Path(sys.executable).resolve())


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until_process_is_gone(pid: int, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_exists(pid):
            return True
        time.sleep(0.01)
    return not process_exists(pid)


def test_timeout_terminates_and_reaps_the_complete_process_group(tmp_path: Path) -> None:
    pid_path = tmp_path / "processes.json"
    child_code = "import time; time.sleep(30)"
    parent_code = """
import json, os, signal, subprocess, sys, time
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", sys.argv[2]])
Path(sys.argv[1]).write_text(json.dumps({"parent": os.getpid(), "child": child.pid}))

def terminate(_signum, _frame):
    child.wait(timeout=2)
    raise SystemExit(143)

signal.signal(signal.SIGTERM, terminate)
time.sleep(30)
"""
    runner = SubprocessRunner(
        secrets=SecretRegistry(),
        max_output_bytes=4096,
        termination_grace_seconds=1,
        poll_interval_seconds=0.01,
    )

    result = runner.run(
        [PYTHON, "-c", parent_code, str(pid_path), child_code],
        timeout_seconds=0.3,
        environment={},
        working_directory=tmp_path,
    )

    pids = json.loads(pid_path.read_text())
    assert result.outcome is CommandOutcome.TIMED_OUT
    assert result.termination_attempted
    assert not result.forced_kill
    assert wait_until_process_is_gone(pids["parent"])
    assert wait_until_process_is_gone(pids["child"])
