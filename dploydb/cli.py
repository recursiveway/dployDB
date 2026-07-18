"""Command-line interface for DployDB."""

from importlib.metadata import version
from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(
    help="Deployment safety for SQLite applications.",
    no_args_is_help=True,
)
console = Console(color_system=None, highlight=False, markup=False)


def _version_text() -> str:
    return f"dploydb {version('dploydb')}"


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
