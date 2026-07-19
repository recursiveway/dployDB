"""Milestone 8 command-line usability contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dploydb.cli import app

runner = CliRunner()

REQUIRED_HELP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("--help",), "Deployment safety for SQLite applications"),
    (("init", "--help"), "Create a restrictive, valid starter configuration"),
    (("doctor", "--help"), "Check configured host safety"),
    (("backup", "--help"), "Create one verified local backup"),
    (("verify", "--help"), "Reverify one committed local backup"),
    (("deploy", "--help"), "automatic pre-traffic rollback"),
    (("status", "--help"), "durable operation state"),
    (("releases", "--help"), "validated local deployment release"),
    (("release", "--help"), "Inspect one durable deployment release"),
    (("release", "show", "--help"), "complete validated release manifest"),
    (("restore", "--help"), "backup-first restore"),
    (("recover", "--help"), "interrupted deployment"),
    (("version", "--help"), "installed DployDB version"),
)


@pytest.mark.parametrize(("arguments", "expected"), REQUIRED_HELP)
def test_every_required_command_has_useful_ansi_free_help(
    arguments: tuple[str, ...], expected: str
) -> None:
    result = runner.invoke(app, list(arguments), color=True)

    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert expected in result.output
    assert "\x1b[" not in result.output


def test_explicit_no_color_is_discoverable_and_works_before_a_command() -> None:
    root_help = runner.invoke(app, ["--help"], color=True)
    result = runner.invoke(app, ["--no-color", "doctor", "--help"], color=True)

    assert root_help.exit_code == 0
    assert "--no-color" in root_help.output
    assert result.exit_code == 0
    assert "\x1b[" not in result.output


def test_no_color_environment_variable_disables_ansi_output() -> None:
    result = runner.invoke(
        app,
        ["release", "show", "--help"],
        env={"NO_COLOR": "1"},
        color=True,
    )

    assert result.exit_code == 0
    assert "\x1b[" not in result.output


def test_no_color_json_success_is_one_parseable_document(tmp_path: Path) -> None:
    config_path = tmp_path / "dploydb.yaml"
    result = runner.invoke(
        app,
        ["--no-color", "init", "--config", str(config_path), "--json"],
        color=True,
    )

    assert result.exit_code == 0
    assert "\x1b[" not in result.output
    assert result.output.count("\n") == 1
    assert json.loads(result.output) == {"config_path": str(config_path), "ok": True}


def test_no_color_json_failure_is_one_parseable_document(tmp_path: Path) -> None:
    config_path = tmp_path / "dploydb.yaml"
    config_path.write_text("preserve me", encoding="utf-8")
    result = runner.invoke(
        app,
        ["--no-color", "init", "--config", str(config_path), "--json"],
        color=True,
    )

    assert result.exit_code == 10
    assert "\x1b[" not in result.output
    assert result.output.count("\n") == 1
    payload = json.loads(result.output)
    assert payload["error_code"] == "configuration_error"
    assert payload["production_changed"] is False
