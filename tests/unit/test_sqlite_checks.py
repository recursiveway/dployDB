"""Tests for bounded read-only SQLite verification."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from dploydb.errors import SafetyCheckError
from dploydb.sqlite_checks import verify_sqlite_database


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parent(id)
            );
            INSERT INTO parent VALUES (1);
            INSERT INTO child VALUES (1, 1);
            """
        )


def test_standard_and_deep_verification_return_typed_evidence(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    _database(path)

    standard = verify_sqlite_database(path)
    deep = verify_sqlite_database(path, deep=True)

    assert standard.quick_check_passed is True
    assert standard.foreign_key_check_passed is True
    assert standard.integrity_check_passed is None
    assert deep.integrity_check_passed is True
    assert deep.model_dump(mode="json")["checked_at"].endswith("Z")


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "malformed"])
def test_invalid_database_paths_fail_safely(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "app.db"
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target.db"
        _database(target)
        path.symlink_to(target)
    elif kind == "malformed":
        path.write_bytes(b"not a sqlite database")

    with pytest.raises(SafetyCheckError) as captured:
        verify_sqlite_database(path)

    assert captured.value.payload.production_changed is False
    assert captured.value.payload.recovery_required is False
    assert captured.value.payload.log_path == str(path)


def test_foreign_key_violation_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (parent_id INTEGER REFERENCES parent(id));
            INSERT INTO child VALUES (999);
            """
        )

    with pytest.raises(SafetyCheckError, match="foreign_key_check failed"):
        verify_sqlite_database(path)


def test_locked_database_fails_safely_within_timeout(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    _database(path)
    writer = sqlite3.connect(path, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    started_at = time.monotonic()
    try:
        with pytest.raises(SafetyCheckError) as captured:
            verify_sqlite_database(path, timeout_seconds=0.02)
    finally:
        writer.rollback()
        writer.close()

    # SQLite builds may either honor busy_timeout before failing or return
    # SQLITE_BUSY immediately. Both outcomes are explicit, bounded, and safe.
    detail = str(captured.value)
    assert "timed out" in detail or "database is locked" in detail
    assert time.monotonic() - started_at < 1.0
    assert captured.value.payload.production_changed is False
    assert captured.value.payload.recovery_required is False


def test_progress_deadline_interrupts_long_check(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    _database(path)
    ticks = iter((0.0, 0.0, 2.0, 2.0, 2.0, 2.0))

    with pytest.raises(SafetyCheckError, match="timed out"):
        verify_sqlite_database(
            path,
            timeout_seconds=1.0,
            monotonic=lambda: next(ticks, 2.0),
            progress_instructions=1,
        )


def test_verification_does_not_change_database_bytes(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    _database(path)
    before = path.read_bytes()

    verify_sqlite_database(path, deep=True)

    assert path.read_bytes() == before
