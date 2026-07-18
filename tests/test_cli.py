"""CLI contract tests."""

from importlib.metadata import version

from typer.testing import CliRunner

from dploydb.cli import app

runner = CliRunner()


def test_help_succeeds() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Deployment safety for SQLite applications" in result.output
    assert "version" in result.output
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
