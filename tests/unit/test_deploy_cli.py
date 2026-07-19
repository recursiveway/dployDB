"""Public deploy CLI rendering and exit-contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from dploydb.cli import app
from dploydb.errors import RecoveryRequiredError
from dploydb.models import DeploymentState, SafetyFacts
from dploydb.redaction import SecretRegistry

runner = CliRunner()


def loaded_stub() -> SimpleNamespace:
    return SimpleNamespace(secrets=SecretRegistry())


def active_result() -> SimpleNamespace:
    release = SimpleNamespace(
        status=DeploymentState.ACTIVE,
        release_id="release_" + "1" * 32,
        operation_id="op_" + "2" * 32,
        requested_version="v2",
        traffic_activated=True,
        final_backup_id="backup_" + "3" * 32,
        final_backup_sha256="4" * 64,
        operation_log_path=Path("/tmp/deploy-events.jsonl"),
        failure=None,
    )
    operation = SimpleNamespace(
        safety=SafetyFacts(
            production_changed=True,
            previous_application_running=False,
            recovery_required=False,
        )
    )
    return SimpleNamespace(
        release=release,
        operation=operation,
        active=True,
        rolled_back=False,
    )


def rolled_back_result() -> SimpleNamespace:
    failure = SimpleNamespace(
        error_code="operation_failed",
        what_failed="final production health check returned HTTP 500",
        log_path="/tmp/deploy-events.jsonl",
        next_safe_action="Correct the release before retrying.",
    )
    release = SimpleNamespace(
        status=DeploymentState.ROLLED_BACK,
        release_id="release_" + "5" * 32,
        operation_id="op_" + "6" * 32,
        requested_version="broken-health",
        traffic_activated=False,
        final_backup_id="backup_" + "7" * 32,
        final_backup_sha256="8" * 64,
        operation_log_path=Path("/tmp/deploy-events.jsonl"),
        failure=failure,
    )
    operation = SimpleNamespace(
        safety=SafetyFacts(
            production_changed=True,
            previous_application_running=True,
            recovery_required=False,
        )
    )
    return SimpleNamespace(
        release=release,
        operation=operation,
        active=False,
        rolled_back=True,
    )


def install_deploy_stubs(
    monkeypatch: pytest.MonkeyPatch,
    result: SimpleNamespace,
) -> None:
    monkeypatch.setattr("dploydb.cli.load_configuration", lambda _path: loaded_stub())
    monkeypatch.setattr(
        "dploydb.cli.deploy_configured_release",
        lambda *_args, **_kwargs: result,
    )


@pytest.mark.parametrize("json_output", [False, True])
def test_active_deploy_renders_stable_success(
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
) -> None:
    install_deploy_stubs(monkeypatch, active_result())
    arguments = ["deploy", "--version", "v2", "--config", "/tmp/config.yaml"]
    if json_output:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 0
    assert "Traceback" not in result.output
    if json_output:
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["outcome"] == "active"
        assert payload["requested_version"] == "v2"
        assert payload["production_changed"] is True
        assert payload["previous_application_running"] is False
        assert payload["recovery_required"] is False
    else:
        assert "Outcome: active" in result.output
        assert "Production changed: yes" in result.output
        assert "Recovery required: no" in result.output


@pytest.mark.parametrize("json_output", [False, True])
def test_rolled_back_deploy_renders_failure_and_uses_original_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    json_output: bool,
) -> None:
    install_deploy_stubs(monkeypatch, rolled_back_result())
    arguments = [
        "deploy",
        "--version",
        "broken-health",
        "--config",
        "/tmp/config.yaml",
    ]
    if json_output:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 50
    assert "Traceback" not in result.output
    if json_output:
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["outcome"] == "rolled_back"
        assert payload["error_code"] == "operation_failed"
        assert payload["production_changed"] is True
        assert payload["previous_application_running"] is True
        assert payload["recovery_required"] is False
    else:
        assert "Outcome: rolled_back" in result.output
        assert "Previous application running: yes" in result.output
        assert "Recovery required: no" in result.output


def test_recovery_required_deploy_uses_stable_json_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dploydb.cli.load_configuration", lambda _path: loaded_stub())

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RecoveryRequiredError(
            "traffic activation outcome is uncertain",
            production_changed=True,
            previous_application_running=False,
            log_path="/tmp/deploy-events.jsonl",
            next_safe_action="Determine the live traffic target; do not restore the database.",
        )

    monkeypatch.setattr("dploydb.cli.deploy_configured_release", fail)

    result = runner.invoke(
        app,
        ["deploy", "--version", "v2", "--config", "/tmp/config.yaml", "--json"],
    )

    assert result.exit_code == 60
    payload = json.loads(result.output)
    assert payload["error_code"] == "recovery_required"
    assert payload["production_changed"] is True
    assert payload["previous_application_running"] is False
    assert payload["recovery_required"] is True
    assert "Traceback" not in result.output


def test_non_interactive_deploy_never_requests_terminal_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_deploy_stubs(monkeypatch, active_result())

    def refuse_input(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("deploy must not request terminal input")

    monkeypatch.setattr("builtins.input", refuse_input)

    result = runner.invoke(
        app,
        [
            "deploy",
            "--version",
            "v2",
            "--config",
            "/tmp/config.yaml",
            "--json",
            "--non-interactive",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["non_interactive"] is True


def test_deploy_requires_version_option() -> None:
    result = runner.invoke(app, ["deploy", "--config", "/tmp/config.yaml"])

    assert result.exit_code == 2
    assert "Missing option '--version'" in result.output
    assert "Traceback" not in result.output
