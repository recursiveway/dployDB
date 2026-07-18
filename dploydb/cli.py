"""Command-line interface for DployDB."""

import json
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console

from dploydb.config import DEFAULT_CONFIG_PATH, initialize_configuration
from dploydb.errors import DployDBError
from dploydb.models import FailurePayload

app = typer.Typer(
    help="Deployment safety for SQLite applications.",
    no_args_is_help=True,
)
console = Console(color_system=None, highlight=False, markup=False)


def _version_text() -> str:
    return f"dploydb {version('dploydb')}"


def _yes_no_unknown(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def render_failure(payload: FailurePayload, *, json_output: bool) -> str:
    """Render one failure contract without changing or hiding its safety facts."""
    if json_output:
        return json.dumps(payload.as_dict(), sort_keys=True, separators=(",", ":"))

    return "\n".join(
        (
            "DployDB could not complete the operation.",
            f"Error code: {payload.error_code}",
            f"Exit code: {payload.exit_code}",
            f"What failed: {payload.what_failed}",
            f"Production changed: {_yes_no_unknown(payload.production_changed)}",
            "Previous application running: "
            f"{_yes_no_unknown(payload.previous_application_running)}",
            f"Recovery required: {_yes_no_unknown(payload.recovery_required)}",
            f"Relevant log: {payload.log_path or 'not available'}",
            f"Next safe action: {payload.next_safe_action}",
        )
    )


def abort_with_error(error: DployDBError, *, json_output: bool) -> NoReturn:
    """Emit an expected failure and exit without exposing a traceback."""
    typer.echo(render_failure(error.payload, json_output=json_output), err=not json_output)
    raise typer.Exit(code=int(error.exit_code))


def _version_callback(value: bool) -> None:
    if value:
        console.print(_version_text())
        raise typer.Exit()


@app.callback()
def main(
    show_version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed DployDB version and exit.",
        ),
    ] = None,
) -> None:
    """Deployment safety for SQLite applications."""


@app.command("version")
def version_command() -> None:
    """Show the installed DployDB version."""
    console.print(_version_text())


@app.command("init")
def init_command(
    config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            help="Configuration file to create without overwriting an existing path.",
        ),
    ] = DEFAULT_CONFIG_PATH,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Create a restrictive, valid starter configuration."""
    try:
        created_path = initialize_configuration(config_path)
    except DployDBError as error:
        abort_with_error(error, json_output=json_output)

    if json_output:
        typer.echo(
            json.dumps(
                {"ok": True, "config_path": str(created_path)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return

    typer.echo(f"Created DployDB configuration: {created_path}")
    typer.echo("Permissions: 0600")
    typer.echo("Next safe action: edit the paths, then run dploydb doctor.")
