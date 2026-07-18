"""Real-process gates for deployment-lock contention and abrupt termination."""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

from dploydb.errors import LockUnavailableError
from dploydb.locking import DeploymentLock, LockInspectionState, inspect_lock
from dploydb.redaction import SecretRegistry

FIRST_OPERATION_ID = "op_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SECOND_OPERATION_ID = "op_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _hold_lock(
    root_text: str,
    ready: Any,
    release: Any,
    entered_text: str,
) -> None:
    root = Path(root_text)
    lock = DeploymentLock(root, secrets=SecretRegistry())
    with lock:
        lock.record_owner(operation_id=FIRST_OPERATION_ID, operation_type="deploy")
        Path(entered_text).write_text("holder\n", encoding="utf-8")
        ready.set()
        release.wait(30)


def _attempt_lock(
    root_text: str,
    result: Any,
    entered_text: str,
    stale_owner_id: str | None = None,
) -> None:
    lock = DeploymentLock(Path(root_text), secrets=SecretRegistry())
    try:
        with lock:
            previous_owner_id = (
                None if lock.previous_owner is None else lock.previous_owner.owner_id
            )
            lock.record_owner(
                operation_id=SECOND_OPERATION_ID,
                operation_type="deploy",
                replace_stale_owner_id=stale_owner_id,
            )
            Path(entered_text).write_text("contender\n", encoding="utf-8")
            result.put(("acquired", previous_owner_id))
    except LockUnavailableError as error:
        result.put(("blocked", int(error.exit_code)))


def _stop_process(process: multiprocessing.Process) -> None:
    process.join(timeout=2)
    if process.is_alive():
        process.kill()
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=10)


def test_real_process_contention_allows_only_one_holder(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    root = tmp_path / "state"
    holder_entered = tmp_path / "holder.entered"
    contender_entered = tmp_path / "contender.entered"
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    holder = context.Process(
        target=_hold_lock,
        args=(str(root), ready, release, str(holder_entered)),
    )
    contender: multiprocessing.Process | None = None

    try:
        holder.start()
        assert ready.wait(10), "holder did not durably record lock ownership"
        contender = context.Process(
            target=_attempt_lock,
            args=(str(root), result, str(contender_entered)),
        )
        contender.start()
        contender.join(timeout=10)
        assert not contender.is_alive(), "contender did not exit after nonblocking acquisition"
        assert result.get(timeout=2) == ("blocked", 30)
        assert holder_entered.read_text(encoding="utf-8") == "holder\n"
        assert not contender_entered.exists()
    finally:
        release.set()
        if contender is not None:
            _stop_process(contender)
        _stop_process(holder)

    assert holder.exitcode == 0


def test_sigkill_releases_kernel_lock_and_preserves_stale_owner(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    root = tmp_path / "state"
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_lock,
        args=(str(root), ready, release, str(tmp_path / "holder.entered")),
    )
    result = context.Queue()
    replacement: multiprocessing.Process | None = None

    try:
        holder.start()
        assert ready.wait(10), "holder did not durably record lock ownership"
        active = inspect_lock(root, secrets=SecretRegistry())
        assert active.state is LockInspectionState.ACTIVE
        assert active.owner is not None
        assert active.owner.process.pid == holder.pid
        stale_owner_id = active.owner.owner_id
        owner_bytes = active.owner_path.read_bytes()

        holder.kill()
        holder.join(timeout=10)
        assert not holder.is_alive()

        stale = inspect_lock(root, secrets=SecretRegistry())
        assert stale.state is LockInspectionState.STALE_OWNER
        assert stale.lock_held is False
        assert stale.owner is not None
        assert stale.owner.owner_id == stale_owner_id
        assert stale.owner_path.read_bytes() == owner_bytes

        replacement = context.Process(
            target=_attempt_lock,
            args=(
                str(root),
                result,
                str(tmp_path / "replacement.entered"),
                stale_owner_id,
            ),
        )
        replacement.start()
        replacement.join(timeout=10)
        assert not replacement.is_alive(), "replacement did not complete"
        assert result.get(timeout=2) == ("acquired", stale_owner_id)
    finally:
        if replacement is not None:
            _stop_process(replacement)
        _stop_process(holder)

    assert replacement is not None
    assert replacement.exitcode == 0
    final = inspect_lock(root, secrets=SecretRegistry())
    assert final.state is LockInspectionState.IDLE
    assert final.owner is not None
    assert final.owner.state.value == "released"
    assert final.owner.owner_id != stale_owner_id
