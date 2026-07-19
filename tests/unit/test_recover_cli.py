"""Public recovery diagnosis, confirmation, refusal, and result contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from dploydb.cli import app
from dploydb.recovery import (
    RecoveryAction,
    RecoveryDisposition,
    RecoveryPlan,
)
from dploydb.redaction import SecretRegistry

runner = CliRunner()


def plan(disposition: RecoveryDisposition) -> RecoveryPlan:
    actions = (
        RecoveryAction.RESTART_PREVIOUS_APPLICATION,
        RecoveryAction.ACTIVATE_PREVIOUS_TRAFFIC,
        RecoveryAction.DISABLE_MAINTENANCE,
        RecoveryAction.VERIFY_PREVIOUS,
        RecoveryAction.MARK_ROLLED_BACK,
    )
    if disposition in {RecoveryDisposition.MANUAL_REQUIRED, RecoveryDisposition.NO_ACTION}:
        actions = ()
    return RecoveryPlan(
        disposition=disposition,
        release_id="release_" + "1" * 32,
        operation_id="op_" + "2" * 32,
        durable_stage="current_app_stopped",
        production_may_have_changed=False,
        traffic_may_have_switched=disposition is RecoveryDisposition.MANUAL_REQUIRED,
        automatic_database_restore_allowed=(disposition is RecoveryDisposition.RECOVER_PREVIOUS),
        actions=actions,
        reason=(
            "traffic target is uncertain"
            if disposition is RecoveryDisposition.MANUAL_REQUIRED
            else "durable evidence is deterministic"
        ),
        next_safe_action="Preserve evidence and follow this exact plan.",
        final_backup_id=None,
    )


class Result:
    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "command": "recover",
            "outcome": "rolled_back",
            "recovery_operation_id": "op_" + "3" * 32,
            "source_operation_id": "op_" + "2" * 32,
            "release_id": "release_" + "1" * 32,
            "release_status": "rolled_back",
            "production_changed": False,
            "previous_application_running": True,
            "recovery_required": False,
            "actions": ["restart_previous_application"],
            "log_path": "/tmp/recover/events.jsonl",
        }


def install(
    monkeypatch: pytest.MonkeyPatch,
    selected: RecoveryPlan,
) -> list[str]:
    calls: list[str] = []
    loaded = SimpleNamespace(
        secrets=SecretRegistry(),
        config=SimpleNamespace(state_directory=Path("/tmp/state")),
    )
    monkeypatch.setattr("dploydb.cli.load_configuration", lambda _path: loaded)
    monkeypatch.setattr(
        "dploydb.cli.preview_configured_recovery",
        lambda *_args, **_kwargs: selected,
    )

    def execute(*_args: object, **_kwargs: object) -> Result:
        calls.append("recover")
        return Result()

    monkeypatch.setattr("dploydb.cli.recover_configured_deployment", execute)
    return calls


def test_recover_json_without_yes_is_read_only_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install(monkeypatch, plan(RecoveryDisposition.RECOVER_PREVIOUS))

    result = runner.invoke(app, ["recover", "--config", "/tmp/config", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["outcome"] == "preview"
    assert payload["disposition"] == "recover_previous"
    assert payload["confirmation_required"] is True
    assert payload["executed"] is False
    assert calls == []


def test_recover_declined_confirmation_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install(monkeypatch, plan(RecoveryDisposition.RECOVER_PREVIOUS))

    result = runner.invoke(
        app,
        ["recover", "--config", "/tmp/config"],
        input="n\n",
    )

    assert result.exit_code == 0
    assert "diagnosis was read-only" in result.output
    assert calls == []


def test_recover_yes_executes_revalidated_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install(monkeypatch, plan(RecoveryDisposition.RECOVER_PREVIOUS))

    result = runner.invoke(
        app,
        ["recover", "--config", "/tmp/config", "--yes", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["outcome"] == "rolled_back"
    assert payload["recovery_required"] is False
    assert calls == ["recover"]


def test_recover_refuses_ambiguous_plan_with_stable_recovery_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install(monkeypatch, plan(RecoveryDisposition.MANUAL_REQUIRED))

    result = runner.invoke(
        app,
        ["recover", "--config", "/tmp/config", "--yes", "--json"],
    )

    assert result.exit_code == 60
    payload = json.loads(result.output)
    assert payload["error_code"] == "recovery_required"
    assert payload["what_failed"] == "traffic target is uncertain"
    assert payload["recovery_required"] is True
    assert calls == []
    assert "Traceback" not in result.output


def test_recover_no_action_returns_success_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install(monkeypatch, plan(RecoveryDisposition.NO_ACTION))

    result = runner.invoke(app, ["recover", "--config", "/tmp/config", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["disposition"] == "no_action"
    assert calls == []
