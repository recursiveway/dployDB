"""Public read-only release history CLI contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from dploydb.cli import app
from dploydb.errors import SafetyCheckError
from dploydb.models import DeploymentState, ReleaseManifest
from dploydb.redaction import SecretRegistry
from dploydb.releases import ReleaseHistorySnapshot

runner = CliRunner()


def manifest(tmp_path: Path) -> ReleaseManifest:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    return ReleaseManifest(
        release_id="release_" + "1" * 32,
        operation_id="op_" + "2" * 32,
        project="example-app",
        requested_version="v2",
        status=DeploymentState.CREATED,
        configuration_fingerprint="a" * 64,
        operation_log_path=(tmp_path / "events.jsonl").resolve(),
        started_at=now,
        updated_at=now,
    )


def install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    history: ReleaseHistorySnapshot,
) -> None:
    loaded = SimpleNamespace(
        config=SimpleNamespace(
            state_directory=(tmp_path / "state").resolve(),
            project="example-app",
        ),
        secrets=SecretRegistry(),
    )
    selected = history.releases[0] if history.releases else None
    store = SimpleNamespace(
        read_history=lambda: history,
        lookup_history_release=lambda _release_id: (selected, history.pointers),
    )
    monkeypatch.setattr("dploydb.cli.load_configuration", lambda _path: loaded)
    monkeypatch.setattr("dploydb.cli.ReleaseStore", lambda *_args, **_kwargs: store)


@pytest.mark.parametrize("json_output", [False, True])
def test_releases_lists_validated_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
) -> None:
    selected = manifest(tmp_path)
    install(
        monkeypatch,
        tmp_path,
        ReleaseHistorySnapshot(releases=(selected,), pointers=None),
    )
    arguments = ["releases", "--config", "/tmp/config.yaml"]
    if json_output:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 0
    assert "Traceback" not in result.output
    if json_output:
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["command"] == "releases"
        assert payload["count"] == 1
        assert payload["releases"][0]["release_id"] == selected.release_id
        assert payload["releases"][0]["status"] == "created"
    else:
        assert "DployDB releases" in result.output
        assert f"{selected.release_id} version=v2 status=created" in result.output


@pytest.mark.parametrize("json_output", [False, True])
def test_release_show_renders_complete_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
) -> None:
    selected = manifest(tmp_path)
    install(
        monkeypatch,
        tmp_path,
        ReleaseHistorySnapshot(releases=(selected,), pointers=None),
    )
    arguments = [
        "release",
        "show",
        selected.release_id,
        "--config",
        "/tmp/config.yaml",
    ]
    if json_output:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 0
    assert "Traceback" not in result.output
    if json_output:
        payload = json.loads(result.output)
        assert payload["command"] == "release show"
        assert payload["release"]["release_id"] == selected.release_id
        assert payload["release"]["operation_log_path"] == str(selected.operation_log_path)
    else:
        assert "DployDB release" in result.output
        assert "Version: v2" in result.output
        assert "Status: created" in result.output


def test_release_show_uses_stable_expected_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, tmp_path, ReleaseHistorySnapshot(releases=(), pointers=None))

    def fail(_release_id: str) -> None:
        raise SafetyCheckError(
            "release does not exist",
            production_changed=False,
            previous_application_running=None,
            log_path="/tmp/releases",
            next_safe_action="Run dploydb releases.",
        )

    store = SimpleNamespace(lookup_history_release=fail)
    monkeypatch.setattr("dploydb.cli.ReleaseStore", lambda *_args, **_kwargs: store)

    result = runner.invoke(
        app,
        ["release", "show", "release_" + "9" * 32, "--config", "/tmp/c", "--json"],
    )

    assert result.exit_code == 20
    assert json.loads(result.output)["error_code"] == "safety_check_failed"
    assert "Traceback" not in result.output
