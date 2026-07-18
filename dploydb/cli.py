"""Command-line interface for DployDB."""

import json
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console

from dploydb.backup import create_configured_backup, verify_configured_backup
from dploydb.config import DEFAULT_CONFIG_PATH, initialize_configuration, load_configuration
from dploydb.diagnostics import (
    DoctorReport,
    StatusReport,
    inspect_runtime_status,
    run_doctor,
)
from dploydb.errors import DployDBError, InternalError, redact_error
from dploydb.models import BackupArtifact, DiagnosticOutcome, FailurePayload
from dploydb.redaction import SecretRegistry

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


def _internal_error() -> InternalError:
    return InternalError(
        "An unexpected internal failure prevented the command from completing.",
        production_changed=False,
        previous_application_running=None,
        next_safe_action="Preserve the current evidence and rerun with a corrected installation.",
    )


def _render_doctor(report: DoctorReport, *, json_output: bool) -> str:
    if json_output:
        return json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":"))
    mode = "deep" if report.deep else "standard"
    lines = [f"DployDB doctor ({mode})", f"Project: {report.project}"]
    labels = {
        DiagnosticOutcome.PASSED: "PASS",
        DiagnosticOutcome.WARNING: "WARN",
        DiagnosticOutcome.FAILED: "FAIL",
        DiagnosticOutcome.SKIPPED: "SKIP",
    }
    for check in report.checks:
        lines.append(f"[{labels[check.outcome]}] {check.check_id}: {check.message}")
    summary = report.as_dict()["summary"]
    assert isinstance(summary, dict)
    lines.append("Summary: " + ", ".join(f"{name}={count}" for name, count in summary.items()))
    if report.failure is not None:
        lines.extend(("", render_failure(report.failure, json_output=False)))
    else:
        lines.append("Next safe action: host diagnostics passed for the implemented checks.")
    return "\n".join(lines)


def _render_status(report: StatusReport, *, json_output: bool) -> str:
    if json_output:
        return json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":"))
    lines = [
        "DployDB status",
        f"Project: {report.project}",
        f"Status: {report.status.value}",
        f"Lock state: {report.lock['state']}",
    ]
    if report.operation is not None:
        lines.extend(
            (
                f"Operation: {report.operation['operation_id']}",
                f"Operation type: {report.operation['operation_type']}",
                f"Operation status: {report.operation['status']}",
                f"Stage: {report.operation['stage']}",
            )
        )
    lines.extend(f"Warning: {warning}" for warning in report.warnings)
    if report.failure is not None:
        lines.extend(("", render_failure(report.failure, json_output=False)))
    else:
        lines.append(f"Next safe action: {report.next_safe_action}")
    return "\n".join(lines)


def _backup_result(
    artifact: BackupArtifact,
    *,
    command: str,
    secrets: SecretRegistry,
) -> dict[str, object]:
    metadata = artifact.metadata
    return {
        "ok": True,
        "command": command,
        "project": secrets.redact_text(metadata.project),
        "backup_id": metadata.backup_id,
        "database_path": secrets.redact_text(str(artifact.database_path)),
        "metadata_path": secrets.redact_text(str(artifact.metadata_path)),
        "size_bytes": metadata.size_bytes,
        "sha256": metadata.sha256,
        "purpose": metadata.purpose.value,
        "created_at": metadata.model_dump(mode="json")["created_at"],
        "completed_at": metadata.model_dump(mode="json")["completed_at"],
        "checks": metadata.sqlite.model_dump(mode="json"),
    }


def _render_backup_result(
    artifact: BackupArtifact,
    *,
    command: str,
    json_output: bool,
    secrets: SecretRegistry,
) -> str:
    result = _backup_result(artifact, command=command, secrets=secrets)
    if json_output:
        return json.dumps(result, sort_keys=True, separators=(",", ":"))
    action = "created" if command == "backup" else "verified"
    return "\n".join(
        (
            f"DployDB backup {action}.",
            f"Project: {result['project']}",
            f"Backup ID: {result['backup_id']}",
            f"Database: {result['database_path']}",
            f"Size: {result['size_bytes']} bytes",
            f"SHA-256: {result['sha256']}",
            "SQLite checks: passed",
        )
    )


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


@app.command("doctor")
def doctor_command(
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Configuration file to inspect."),
    ] = DEFAULT_CONFIG_PATH,
    deep: Annotated[
        bool,
        typer.Option("--deep", help="Run bounded Docker, write, and disk-space probes."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Check Milestone 1 host safety without changing production."""
    try:
        loaded = load_configuration(config_path)
        report = run_doctor(loaded, config_path=config_path, deep=deep)
    except DployDBError as error:
        abort_with_error(error, json_output=json_output)
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)
    typer.echo(
        _render_doctor(report, json_output=json_output),
        err=not json_output and report.exit_code != 0,
    )
    if report.exit_code != 0:
        raise typer.Exit(code=report.exit_code)


@app.command("status")
def status_command(
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Configuration file whose state is inspected."),
    ] = DEFAULT_CONFIG_PATH,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Explain lock and durable operation state without modifying it."""
    try:
        loaded = load_configuration(config_path)
        report = inspect_runtime_status(loaded.config, secrets=loaded.secrets)
    except DployDBError as error:
        abort_with_error(error, json_output=json_output)
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)
    typer.echo(
        _render_status(report, json_output=json_output),
        err=not json_output and report.exit_code != 0,
    )
    if report.exit_code != 0:
        raise typer.Exit(code=report.exit_code)


@app.command("backup")
def backup_command(
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Configuration file for the backup."),
    ] = DEFAULT_CONFIG_PATH,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Create and verify one consistent local SQLite backup."""
    loaded = None
    try:
        loaded = load_configuration(config_path)
        artifact = create_configured_backup(loaded)
    except DployDBError as error:
        if loaded is not None:
            error = redact_error(error, secrets=loaded.secrets)
        abort_with_error(error, json_output=json_output)
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)
    typer.echo(
        _render_backup_result(
            artifact,
            command="backup",
            json_output=json_output,
            secrets=loaded.secrets,
        )
    )


@app.command("verify")
def verify_command(
    backup_id: Annotated[str, typer.Argument(help="Committed local backup ID to verify.")],
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Configuration file for backup storage."),
    ] = DEFAULT_CONFIG_PATH,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Reverify one committed local backup without modifying state."""
    loaded = None
    try:
        loaded = load_configuration(config_path)
        artifact = verify_configured_backup(loaded, backup_id)
    except DployDBError as error:
        if loaded is not None:
            error = redact_error(error, secrets=loaded.secrets)
        abort_with_error(error, json_output=json_output)
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)
    typer.echo(
        _render_backup_result(
            artifact,
            command="verify",
            json_output=json_output,
            secrets=loaded.secrets,
        )
    )
