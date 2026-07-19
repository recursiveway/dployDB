"""Public manual restore preview, confirmation, and output contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from dploydb.cli import app
from dploydb.manual_restore import DATA_LOSS_WARNING
from dploydb.redaction import SecretRegistry

runner = CliRunner()
RELEASE_ID = "release_" + "1" * 32


class Preview:
    def as_dict(self) -> dict[str, object]:
        return {
            "active_release_id": "release_" + "2" * 32,
            "active_version": "v2",
            "selected_release_id": RELEASE_ID,
            "selected_version": "v1",
            "selected_backup_id": "backup_" + "3" * 32,
            "selected_backup_sha256": "4" * 64,
            "current_container_id": "5" * 64,
            "current_container_name": "current-v2",
            "selected_container_id": "6" * 64,
            "selected_container_name": "previous-v1",
            "database_path": "/srv/app.db",
            "data_loss_possible": True,
            "pre_restore_backup_required": True,
            "warning": DATA_LOSS_WARNING,
        }


class Result:
    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "command": "restore",
            "outcome": "manual_restore_completed",
            "operation_id": "op_" + "7" * 32,
            "selected_release_id": RELEASE_ID,
            "selected_version": "v1",
            "replaced_release_id": "release_" + "2" * 32,
            "pre_restore_backup_id": "backup_" + "8" * 32,
            "restored_backup_id": "backup_" + "3" * 32,
            "database_sha256": "4" * 64,
            "active_release_id": RELEASE_ID,
            "previous_release_id": "release_" + "2" * 32,
            "production_changed": True,
            "previous_application_running": True,
            "recovery_required": False,
            "data_loss_warning": DATA_LOSS_WARNING,
            "log_path": "/tmp/restore/events.jsonl",
        }


def install(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    loaded = SimpleNamespace(secrets=SecretRegistry())
    monkeypatch.setattr("dploydb.cli.load_configuration", lambda _path: loaded)
    monkeypatch.setattr(
        "dploydb.cli.preview_configured_restore",
        lambda _loaded, release_id: Preview() if release_id == RELEASE_ID else None,
    )

    def restore(*_args: object, **_kwargs: object) -> Result:
        calls.append("restore")
        return Result()

    monkeypatch.setattr("dploydb.cli.restore_configured_release", restore)
    return calls


def test_json_without_yes_is_read_only_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install(monkeypatch)

    result = runner.invoke(
        app,
        ["restore", RELEASE_ID, "--config", "/tmp/config.yaml", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["outcome"] == "preview"
    assert payload["executed"] is False
    assert payload["confirmation_required"] is True
    assert payload["data_loss_possible"] is True
    assert payload["warning"] == DATA_LOSS_WARNING
    assert calls == []


def test_human_declined_confirmation_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install(monkeypatch)

    result = runner.invoke(
        app,
        ["restore", RELEASE_ID, "--config", "/tmp/config.yaml"],
        input="n\n",
    )

    assert result.exit_code == 0
    assert DATA_LOSS_WARNING in result.output
    assert "production was not changed" in result.output
    assert calls == []


@pytest.mark.parametrize("arguments,input_text", [(["--yes", "--json"], None), ([], "y\n")])
def test_confirmed_restore_executes_and_reports_backup_first_result(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    input_text: str | None,
) -> None:
    calls = install(monkeypatch)
    command = ["restore", RELEASE_ID, "--config", "/tmp/config.yaml", *arguments]

    result = runner.invoke(app, command, input=input_text)

    assert result.exit_code == 0
    assert calls == ["restore"]
    assert "Traceback" not in result.output
    if "--json" in arguments:
        payload = json.loads(result.output)
        assert payload["outcome"] == "manual_restore_completed"
        assert payload["pre_restore_backup_id"].startswith("backup_")
        assert payload["recovery_required"] is False
    else:
        assert "manual restore completed" in result.output
        assert "Pre-restore backup:" in result.output
