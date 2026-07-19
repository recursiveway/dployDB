"""Real SQLite and HTTP tests for the deterministic demo runtime."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from dploydb.cli import app as dploydb_cli
from dploydb.config import STARTER_CONFIGURATION, load_configuration
from dploydb.restore import restore_stopped_database
from dploydb.storage.local import LocalBackupStorage

ROOT = Path(__file__).resolve().parents[2]
RELEASES = ROOT / "demo" / "releases"
PROCESS_TIMEOUT_SECONDS = 20
HTTP_TIMEOUT_SECONDS = 2
cli_runner = CliRunner()


def create_database(path: Path) -> None:
    with sqlite3.connect(path):
        pass


def run_migration(database: Path, release: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DATABASE_PATH"] = str(database.resolve())
    return subprocess.run(
        [sys.executable, "-m", "demo.runtime.migration", str(RELEASES / release)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
        check=False,
    )


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    http_request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(http_request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = response.status
            response_body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        response_body = error.read()
    return status, json.loads(response_body)


def json_request(
    port: int,
    path: str,
    *,
    method: str = "POST",
    payload: object,
) -> tuple[int, Any]:
    return request(
        port,
        path,
        method=method,
        body=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )


@contextmanager
def running_app(database: Path, release: str, *, test_mode: bool = False) -> Iterator[int]:
    port = available_port()
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_PATH": str(database.resolve()),
            "RELEASE_DIRECTORY": str((RELEASES / release).resolve()),
            "PORT": str(port),
        }
    )
    if test_mode:
        environment["DPLOYDB_TEST_MODE"] = "1"
    else:
        environment.pop("DPLOYDB_TEST_MODE", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "demo.runtime.app"],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                pytest.fail(f"demo app exited early\nstdout:\n{stdout}\nstderr:\n{stderr}")
            try:
                status, _payload = request(port, "/health")
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
                continue
            if status in {200, 503}:
                break
        else:
            pytest.fail("demo app did not expose its health endpoint before the deadline")
        yield port
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.communicate(timeout=1)


def database_snapshot(database: Path) -> tuple[str, int, list[tuple[str]], list[tuple[int, str]]]:
    checksum = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(database) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        schema = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()
        rows = connection.execute("SELECT id, body FROM notes ORDER BY id").fetchall()
    return checksum, version, schema, rows


def dploydb_configuration(tmp_path: Path, database: Path) -> Path:
    value: dict[str, Any] = yaml.safe_load(STARTER_CONFIGURATION)
    candidate_port = available_port()
    value["project"] = "demo-restore"
    value["state_directory"] = str(tmp_path / "dploydb-state")
    value["database"]["path"] = str(database)
    value["migration"]["command"] = [sys.executable, "-c", "pass"]
    value["application"]["compose_file"] = str(ROOT / "demo" / "compose.yaml")
    value["application"]["candidate_port"] = candidate_port
    value["application"]["candidate_health_url"] = f"http://127.0.0.1:{candidate_port}/health"
    value["application"].pop("smoke_command", None)
    value["backup"]["local_directory"] = str(tmp_path / "backups")
    for name in (
        "maintenance_on_command",
        "maintenance_off_command",
        "activate_new_command",
        "activate_old_command",
    ):
        value["traffic"][name] = [sys.executable, "-c", "pass"]
    path = tmp_path / "dploydb.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def test_v1_http_io_is_real_and_persists_across_restart(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    create_database(database)

    migration = run_migration(database, "v1")
    assert migration.returncode == 0, migration.stderr

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert [row[1] for row in connection.execute("PRAGMA table_info(notes)")] == [
            "id",
            "body",
        ]

    with running_app(database, "v1") as port:
        assert request(port, "/health") == (
            200,
            {"ok": True, "release": "v1", "schema_version": 1},
        )
        assert json_request(port, "/notes", payload={"body": "written under v1"}) == (
            201,
            {"id": 1, "body": "written under v1"},
        )
        assert request(port, "/notes") == (
            200,
            [{"id": 1, "body": "written under v1"}],
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id, body FROM notes").fetchall() == [
            (1, "written under v1")
        ]

    with running_app(database, "v1") as port:
        assert request(port, "/notes") == (
            200,
            [{"id": 1, "body": "written under v1"}],
        )


def test_v2_migration_preserves_v1_data_and_adds_category(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    create_database(database)
    assert run_migration(database, "v1").returncode == 0

    with running_app(database, "v1") as port:
        status, _payload = json_request(port, "/notes", payload={"body": "keep me"})
        assert status == 201

    migration = run_migration(database, "v2")
    assert migration.returncode == 0, migration.stderr

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("SELECT id, body, category FROM notes").fetchall() == [
            (1, "keep me", "general")
        ]

    with running_app(database, "v2") as port:
        assert request(port, "/notes") == (
            200,
            [{"id": 1, "body": "keep me", "category": "general"}],
        )
        assert json_request(
            port,
            "/notes",
            payload={"body": "new in v2", "category": "deployment"},
        ) == (
            201,
            {"id": 2, "body": "new in v2", "category": "deployment"},
        )


def test_broken_migration_is_repeatable_and_atomic(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    create_database(database)
    assert run_migration(database, "v1").returncode == 0
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO notes (body) VALUES (?)", ("must survive",))
        connection.commit()

    before = database_snapshot(database)
    for _attempt in range(2):
        result = run_migration(database, "broken-migration")
        assert result.returncode == 1
        assert "no such table: deliberate_missing_table" in result.stderr
        assert "Traceback" not in result.stderr
        assert database_snapshot(database) == before


def test_broken_health_is_a_deterministic_http_failure(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    create_database(database)
    assert run_migration(database, "v1").returncode == 0
    migration = run_migration(database, "broken-health")
    assert migration.returncode == 0, migration.stderr

    with running_app(database, "broken-health") as port:
        assert request(port, "/health") == (
            503,
            {"ok": False, "reason": "fixture_broken_health"},
        )
        assert request(port, "/notes") == (200, [])


def test_final_health_fixture_passes_candidate_mode_and_fails_production(
    tmp_path: Path,
) -> None:
    database = tmp_path / "app.db"
    create_database(database)
    assert run_migration(database, "v1").returncode == 0
    migration = run_migration(database, "final-health-failure")
    assert migration.returncode == 0, migration.stderr

    with running_app(database, "final-health-failure", test_mode=True) as port:
        assert request(port, "/health") == (
            200,
            {
                "ok": True,
                "release": "final-health-failure",
                "schema_version": 2,
            },
        )

    with running_app(database, "final-health-failure") as port:
        assert request(port, "/health") == (
            503,
            {
                "ok": False,
                "reason": "fixture_final_production_health_failure",
            },
        )


def test_schema_mismatch_is_never_reported_healthy(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    create_database(database)
    assert run_migration(database, "v1").returncode == 0

    with running_app(database, "v2") as port:
        assert request(port, "/health") == (
            503,
            {"ok": False, "reason": "schema_mismatch"},
        )


def test_http_validation_contract(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    create_database(database)
    assert run_migration(database, "v1").returncode == 0

    with running_app(database, "v1") as port:
        assert request(port, "/missing") == (404, {"error": "route"})
        assert request(port, "/health", method="POST") == (405, {"error": "method"})
        assert request(port, "/notes", method="POST", body=b"{}") == (
            415,
            {"error": "media_type"},
        )
        assert request(
            port,
            "/notes",
            method="POST",
            body=b"{",
            headers={"Content-Type": "application/json"},
        ) == (400, {"error": "input"})
        assert json_request(port, "/notes", payload={"body": ""}) == (
            400,
            {"error": "input"},
        )
        assert json_request(port, "/notes", payload={"body": "v1", "category": "wrong"}) == (
            400,
            {"error": "input"},
        )


def test_stopped_demo_application_restores_verified_backup_end_to_end(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    create_database(database)
    assert run_migration(database, "v1").returncode == 0
    config_path = dploydb_configuration(tmp_path, database)
    loaded = load_configuration(config_path)

    with running_app(database, "v1") as port:
        assert json_request(port, "/notes", payload={"body": "kept in selected backup"})[0] == 201

    backup_result = cli_runner.invoke(
        dploydb_cli,
        ["backup", "--config", str(config_path), "--json"],
    )
    assert backup_result.exit_code == 0, backup_result.output
    selected_backup_id = json.loads(backup_result.output)["backup_id"]
    verify_result = cli_runner.invoke(
        dploydb_cli,
        ["verify", selected_backup_id, "--config", str(config_path), "--json"],
    )
    assert verify_result.exit_code == 0, verify_result.output

    with running_app(database, "v1") as port:
        assert json_request(port, "/notes", payload={"body": "written after backup"})[0] == 201

    restored = restore_stopped_database(
        loaded,
        selected_backup_id,
        application_stopped=True,
    )

    with running_app(database, "v1") as port:
        assert request(port, "/health")[0] == 200
        assert request(port, "/notes") == (
            200,
            [{"id": 1, "body": "kept in selected backup"}],
        )

    pre_restore = LocalBackupStorage(loaded.config.backup.local_directory).get(
        restored.pre_restore_backup_id
    )
    with sqlite3.connect(pre_restore.database_path) as connection:
        assert connection.execute("SELECT body FROM notes ORDER BY id").fetchall() == [
            ("kept in selected backup",),
            ("written after backup",),
        ]
