"""Bounded, read-only SQLite safety checks."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

from dploydb.errors import SafetyCheckError
from dploydb.models import SQLiteVerification, utc_now

DEFAULT_SQLITE_TIMEOUT_SECONDS: Final[float] = 10.0
PROGRESS_INSTRUCTIONS: Final[int] = 1_000


def verify_sqlite_database(
    path: Path,
    *,
    deep: bool = False,
    timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    progress_instructions: int = PROGRESS_INSTRUCTIONS,
) -> SQLiteVerification:
    """Open one database read-only and require all configured integrity checks."""
    if timeout_seconds <= 0:
        raise ValueError("SQLite verification timeout must be positive")
    if progress_instructions <= 0:
        raise ValueError("SQLite progress interval must be positive")

    started = monotonic()
    deadline = started + timeout_seconds
    before = _validate_database_file(path)
    connection: sqlite3.Connection | None = None
    timed_out = False

    def progress() -> int:
        nonlocal timed_out
        timed_out = monotonic() >= deadline
        return 1 if timed_out else 0

    try:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=remaining,
            isolation_level=None,
        )
        busy_milliseconds = max(1, min(2_147_483_647, int(remaining * 1_000)))
        connection.execute(f"PRAGMA busy_timeout = {busy_milliseconds}")
        connection.set_progress_handler(progress, progress_instructions)

        quick_rows = connection.execute("PRAGMA quick_check(1)").fetchall()
        if quick_rows != [("ok",)]:
            detail = _bounded_check_detail(quick_rows)
            raise _check_error(path, f"SQLite quick_check failed: {detail}")

        foreign_key_row = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_row is not None:
            detail = _bounded_check_detail([foreign_key_row])
            raise _check_error(path, f"SQLite foreign_key_check failed: {detail}")

        integrity_passed: Literal[True] | None = None
        if deep:
            integrity_rows = connection.execute("PRAGMA integrity_check(1)").fetchall()
            if integrity_rows != [("ok",)]:
                detail = _bounded_check_detail(integrity_rows)
                raise _check_error(path, f"SQLite integrity_check failed: {detail}")
            integrity_passed = True
    except TimeoutError:
        raise _timeout_error(path, timeout_seconds) from None
    except sqlite3.Error as exc:
        if timed_out or monotonic() >= deadline:
            raise _timeout_error(path, timeout_seconds) from None
        raise _check_error(path, f"SQLite verification could not complete: {exc}") from None
    finally:
        if connection is not None:
            connection.set_progress_handler(None, 0)
            connection.close()

    after = _validate_database_file(path)
    if (before.st_dev, before.st_ino) != (
        after.st_dev,
        after.st_ino,
    ):
        raise _check_error(path, "database changed identity during verification")

    return SQLiteVerification(
        quick_check_passed=True,
        foreign_key_check_passed=True,
        integrity_check_passed=integrity_passed,
        checked_at=utc_now(),
        duration_seconds=max(0.0, monotonic() - started),
    )


def _validate_database_file(path: Path) -> os.stat_result:
    try:
        details = path.lstat()
    except OSError as exc:
        raise _check_error(path, f"database file could not be inspected: {exc}") from None
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise _check_error(path, "database path must be a regular non-symlink file")
    if not os.access(path, os.R_OK):
        raise _check_error(path, "database file is not readable")
    return details


def _bounded_check_detail(rows: list[tuple[object, ...]]) -> str:
    if not rows:
        return "check returned no result"
    text = repr(rows[0])
    return text if len(text) <= 512 else f"{text[:512]}..."


def _check_error(path: Path, detail: str) -> SafetyCheckError:
    return SafetyCheckError(
        detail,
        production_changed=False,
        previous_application_running=None,
        log_path=path,
        next_safe_action="Repair or replace the database from a verified backup, then retry.",
    )


def _timeout_error(path: Path, timeout_seconds: float) -> SafetyCheckError:
    return _check_error(
        path,
        f"SQLite verification timed out after {timeout_seconds:g} seconds",
    )
