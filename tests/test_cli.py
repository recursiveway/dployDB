"""CLI contract tests."""

import json
import stat
from importlib.metadata import version
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from dploydb.cli import abort_with_error, app, render_failure
from dploydb.errors import ConfigurationError

runner = CliRunner()


def test_help_succeeds() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Deployment safety for SQLite applications" in result.output
    assert "version" in result.output
    assert "init" in result.output
    assert "doctor" in result.output
    assert "status" in result.output
    assert "Traceback" not in result.output


def test_version_command_reports_project_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"dploydb {version('dploydb')}"


def test_version_option_matches_version_command() -> None:
    command_result = runner.invoke(app, ["version"])
    option_result = runner.invoke(app, ["--version"])

    assert option_result.exit_code == 0
    assert option_result.output == command_result.output


def test_unknown_command_fails_without_traceback() -> None:
    result = runner.invoke(app, ["unknown"])

    assert result.exit_code != 0
    assert "No such command" in result.output
    assert "Traceback" not in result.output


def expected_failure() -> ConfigurationError:
    return ConfigurationError(
        "configuration contains an unknown key",
        production_changed=False,
        previous_application_running=True,
        log_path="/tmp/dploydb-operation.log",
        next_safe_action="Correct the configuration and run doctor again.",
    )


def test_human_and_json_failure_rendering_contain_the_same_safety_facts() -> None:
    failure = expected_failure().payload
    human = render_failure(failure, json_output=False)
    machine = json.loads(render_failure(failure, json_output=True))

    assert machine == failure.as_dict()
    assert "What failed: configuration contains an unknown key" in human
    assert "Production changed: no" in human
    assert "Previous application running: yes" in human
    assert "Recovery required: no" in human
    assert "Relevant log: /tmp/dploydb-operation.log" in human
    assert "Next safe action: Correct the configuration and run doctor again." in human
    assert "Traceback" not in human


@pytest.mark.parametrize("json_output", (False, True))
def test_expected_cli_failure_uses_stable_exit_code_without_traceback(json_output: bool) -> None:
    failure_app = typer.Typer()

    @failure_app.callback()
    def failure_root() -> None:
        pass

    @failure_app.command()
    def fail() -> None:
        abort_with_error(expected_failure(), json_output=json_output)

    result = runner.invoke(failure_app, ["fail"])

    assert result.exit_code == 10
    assert "configuration contains an unknown key" in result.output
    assert "Traceback" not in result.output
    if json_output:
        assert json.loads(result.output) == expected_failure().payload.as_dict()


def test_init_creates_a_valid_restrictive_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "dploydb.yaml"

    result = runner.invoke(app, ["init", "--config", str(config_path)])

    assert result.exit_code == 0
    assert f"Created DployDB configuration: {config_path}" in result.output
    assert "Permissions: 0600" in result.output
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert "Traceback" not in result.output


def test_init_json_success_is_machine_readable(tmp_path: Path) -> None:
    config_path = tmp_path / "dploydb.yaml"

    result = runner.invoke(
        app,
        ["init", "--config", str(config_path), "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {"ok": True, "config_path": str(config_path)}


@pytest.mark.parametrize("json_output", (False, True))
def test_init_refuses_to_overwrite_existing_file(tmp_path: Path, json_output: bool) -> None:
    config_path = tmp_path / "dploydb.yaml"
    original = "existing private contents\n"
    config_path.write_text(original, encoding="utf-8")
    arguments = ["init", "--config", str(config_path)]
    if json_output:
        arguments.append("--json")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 10
    assert config_path.read_text(encoding="utf-8") == original
    assert "already exists and was preserved" in result.output
    assert "existing private contents" not in result.output
    assert "Traceback" not in result.output
    if json_output:
        payload = json.loads(result.output)
        assert payload["error_code"] == "configuration_error"
        assert payload["production_changed"] is False
        assert payload["recovery_required"] is False
