"""Focused tests for the disposable migration rehearsal engine."""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

import dploydb.migration as migration_module
from dploydb.backup import calculate_sha256, create_verified_backup
from dploydb.errors import ExternalCommandError, OperationFailedError
from dploydb.migration import migration_rehearsal
from dploydb.models import BackupArtifact, BackupPurpose, MigrationCommandEvidence
from dploydb.redaction import SecretRegistry
from dploydb.storage.local import LocalBackupStorage
from dploydb.subprocesses import SubprocessRunner

OPERATION_ID = "op_" + "a" * 32
DATABASE_ENV = "REHEARSAL_DATABASE"


def _snapshot(tmp_path: Path) -> tuple[Path, BackupArtifact]:
    production = tmp_path / "production.db"
    with sqlite3.connect(production) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("INSERT INTO notes(body) VALUES ('production-row')")
        connection.execute("PRAGMA user_version = 1")
    artifact = create_verified_backup(
        production,
        project="migration-unit",
        purpose=BackupPurpose.REHEARSAL,
        storage=LocalBackupStorage(tmp_path / "backups"),
    )
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return production, artifact


def _runner(*, max_output_bytes: int = 4096) -> SubprocessRunner:
    return SubprocessRunner(
        secrets=SecretRegistry(),
        max_output_bytes=max_output_bytes,
        termination_grace_seconds=0.1,
        poll_interval_seconds=0.01,
    )


def _run(
    tmp_path: Path,
    artifact: BackupArtifact,
    command: tuple[str, ...],
    *,
    runner: SubprocessRunner | None = None,
    cancellation_event: threading.Event | None = None,
    evidence: list[MigrationCommandEvidence] | None = None,
):
    records = evidence if evidence is not None else []
    return migration_rehearsal(
        artifact,
        operation_id=OPERATION_ID,
        command=command,
        database_environment_name=DATABASE_ENV,
        timeout_seconds=0.25,
        workspace_root=tmp_path / "state" / "rehearsals",
        working_directory=tmp_path,
        environment={"EXPLICIT_PARENT": "present"},
        runner=runner or _runner(),
        command_evidence_sink=records.append,
        cancellation_event=cancellation_event,
        log_path=tmp_path / "events.jsonl",
    )


def test_success_uses_disposable_path_and_preserves_verified_snapshot(tmp_path: Path) -> None:
    production, artifact = _snapshot(tmp_path)
    before_production = calculate_sha256(production)
    before_snapshot = calculate_sha256(artifact.database_path)
    records: list[MigrationCommandEvidence] = []
    code = """
import os, sqlite3
path = os.environ["REHEARSAL_DATABASE"]
assert os.environ["EXPLICIT_PARENT"] == "present"
with sqlite3.connect(path) as connection:
    connection.execute("ALTER TABLE notes ADD COLUMN category TEXT NOT NULL DEFAULT 'general'")
    connection.execute("PRAGMA user_version = 2")
print(f"migrated:{path}")
"""

    with _run(
        tmp_path,
        artifact,
        (sys.executable, "-c", code),
        evidence=records,
    ) as active:
        assert active.database_path.exists()
        assert active.database_path != production
        assert active.database_path != artifact.database_path
        with sqlite3.connect(active.database_path) as connection:
            columns = connection.execute("PRAGMA table_info(notes)").fetchall()
            assert [column[1] for column in columns] == ["id", "body", "category"]
            assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert active.result.command.outcome == "succeeded"
        assert active.result.command.stdout.truncated is False
        assert str(active.database_path) in active.result.command.stdout.text

    assert calculate_sha256(production) == before_production
    assert calculate_sha256(artifact.database_path) == before_snapshot
    assert len(records) == 1
    assert records[0].stdout.text.startswith("migrated:")
    assert not (tmp_path / "state" / "rehearsals" / OPERATION_ID).exists()


def test_nonzero_exit_preserves_output_and_removes_workspace(tmp_path: Path) -> None:
    _production, artifact = _snapshot(tmp_path)
    records: list[MigrationCommandEvidence] = []

    with pytest.raises(ExternalCommandError, match="status 7") as captured:
        with _run(
            tmp_path,
            artifact,
            (
                sys.executable,
                "-c",
                "import sys; print('before failure'); sys.stderr.write('broken\\n'); sys.exit(7)",
            ),
            evidence=records,
        ):
            pytest.fail("a failed command must not yield a rehearsal database")

    assert captured.value.payload.production_changed is False
    assert records[0].exit_code == 7
    assert records[0].stdout.text == "before failure\n"
    assert records[0].stderr.text == "broken\n"
    assert not (tmp_path / "state" / "rehearsals" / OPERATION_ID).exists()


def test_timeout_terminates_command_and_removes_workspace(tmp_path: Path) -> None:
    _production, artifact = _snapshot(tmp_path)
    records: list[MigrationCommandEvidence] = []

    with pytest.raises(ExternalCommandError, match="timed out"):
        with _run(
            tmp_path,
            artifact,
            (sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"),
            evidence=records,
        ):
            pytest.fail("a timed-out command must not yield a rehearsal database")

    assert records[0].outcome == "timed_out"
    assert records[0].termination_attempted is True
    assert records[0].stdout.text == "ready\n"
    assert not (tmp_path / "state" / "rehearsals" / OPERATION_ID).exists()


def test_pre_cancelled_command_is_a_failed_safe_rehearsal(tmp_path: Path) -> None:
    _production, artifact = _snapshot(tmp_path)
    cancellation = threading.Event()
    cancellation.set()
    records: list[MigrationCommandEvidence] = []

    with pytest.raises(ExternalCommandError, match="cancelled"):
        with _run(
            tmp_path,
            artifact,
            (sys.executable, "-c", "raise SystemExit('must not run')"),
            cancellation_event=cancellation,
            evidence=records,
        ):
            pytest.fail("a cancelled command must not yield a rehearsal database")

    assert records[0].outcome == "cancelled"
    assert records[0].exit_code is None


def test_truncated_output_cannot_pass_rehearsal(tmp_path: Path) -> None:
    _production, artifact = _snapshot(tmp_path)
    records: list[MigrationCommandEvidence] = []

    with pytest.raises(OperationFailedError, match="complete-capture"):
        with _run(
            tmp_path,
            artifact,
            (sys.executable, "-c", "print('x' * 4096)"),
            runner=_runner(max_output_bytes=64),
            evidence=records,
        ):
            pytest.fail("truncated output must not yield a rehearsal database")

    assert records[0].outcome == "succeeded"
    assert records[0].stdout.truncated is True
    assert records[0].stdout.retained_bytes == 64


def test_exit_zero_with_foreign_key_failure_is_rejected(tmp_path: Path) -> None:
    _production, artifact = _snapshot(tmp_path)
    code = """
import os, sqlite3
with sqlite3.connect(os.environ["REHEARSAL_DATABASE"]) as connection:
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    connection.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
    connection.execute("INSERT INTO child(parent_id) VALUES (999)")
"""

    with pytest.raises(OperationFailedError, match="post-migration SQLite verification"):
        with _run(tmp_path, artifact, (sys.executable, "-c", code)):
            pytest.fail("an invalid database must not be yielded")

    assert not (tmp_path / "state" / "rehearsals" / OPERATION_ID).exists()


def test_cleanup_failure_is_reported_after_an_otherwise_valid_rehearsal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _production, artifact = _snapshot(tmp_path)
    original_cleanup = migration_module._cleanup_workspace

    def report_failure_after_cleanup(workspace: Path | None) -> str:
        assert original_cleanup(workspace) is None
        return "injected directory sync failure"

    monkeypatch.setattr(migration_module, "_cleanup_workspace", report_failure_after_cleanup)

    with pytest.raises(OperationFailedError, match="workspace cleanup failed"):
        with _run(tmp_path, artifact, (sys.executable, "-c", "pass")):
            pass

    assert not (tmp_path / "state" / "rehearsals" / OPERATION_ID).exists()
