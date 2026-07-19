"""Command-line interface for DployDB."""

import json
import os
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, NoReturn, cast

import typer
from rich.console import Console

from dploydb.backup import create_configured_backup_result, verify_configured_backup
from dploydb.config import DEFAULT_CONFIG_PATH, initialize_configuration, load_configuration
from dploydb.deploy import DeploymentResult, deploy_configured_release
from dploydb.diagnostics import (
    DoctorReport,
    StatusReport,
    inspect_runtime_status,
    run_doctor,
)
from dploydb.errors import (
    ERROR_EXIT_CODES,
    DployDBError,
    ErrorKind,
    ExitCode,
    InternalError,
    RecoveryRequiredError,
    redact_error,
)
from dploydb.manual_restore import (
    DATA_LOSS_WARNING,
    ManualRestoreResult,
    RestoreSelection,
    preview_configured_restore,
    restore_configured_release,
)
from dploydb.models import (
    BackupArtifact,
    DeploymentState,
    DiagnosticOutcome,
    FailurePayload,
    ReleaseManifest,
    ReleasePointers,
    RemoteBackupArtifact,
)
from dploydb.recovery import (
    RecoveryDisposition,
    RecoveryPlan,
    RecoveryResult,
    preview_configured_recovery,
    recover_configured_deployment,
)
from dploydb.redaction import JsonValue, SecretRegistry
from dploydb.releases import ReleaseHistorySnapshot, ReleaseStore

app = typer.Typer(
    help="Deployment safety for SQLite applications.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
release_app = typer.Typer(
    help="Inspect one durable deployment release.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(release_app, name="release")
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
    remote: RemoteBackupArtifact | None = None,
) -> dict[str, object]:
    metadata = artifact.metadata
    result: dict[str, object] = {
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
    if command == "backup":
        result["remote_uploaded"] = remote is not None
        result["remote"] = (
            None
            if remote is None
            else {
                "provider": "s3",
                "bucket": remote.bucket,
                "database_object_key": remote.metadata.database_object_key,
                "metadata_object_key": remote.metadata_object_key,
                "uploaded_at": remote.metadata.model_dump(mode="json")["uploaded_at"],
                "release_id": remote.metadata.release_id,
            }
        )
    return result


def _render_backup_result(
    artifact: BackupArtifact,
    *,
    command: str,
    json_output: bool,
    secrets: SecretRegistry,
    remote: RemoteBackupArtifact | None = None,
) -> str:
    result = _backup_result(artifact, command=command, secrets=secrets, remote=remote)
    if json_output:
        return json.dumps(result, sort_keys=True, separators=(",", ":"))
    action = "created" if command == "backup" else "verified"
    lines = [
        f"DployDB backup {action}.",
        f"Project: {result['project']}",
        f"Backup ID: {result['backup_id']}",
        f"Database: {result['database_path']}",
        f"Size: {result['size_bytes']} bytes",
        f"SHA-256: {result['sha256']}",
        "SQLite checks: passed",
    ]
    if command == "backup":
        lines.append(
            "Remote backup: verified and committed"
            if remote is not None
            else "Remote backup: not requested"
        )
    return "\n".join(lines)


def _rolled_back_failure(result: DeploymentResult) -> FailurePayload:
    failure = result.release.failure
    if failure is None:
        raise ValueError("rolled-back deployment requires durable failure evidence")
    try:
        exit_code = int(ERROR_EXIT_CODES[ErrorKind(failure.error_code)])
    except (KeyError, ValueError):
        exit_code = int(ExitCode.OPERATION_FAILED)
    return FailurePayload(
        error_code=failure.error_code,
        exit_code=exit_code,
        what_failed=failure.what_failed,
        production_changed=result.operation.safety.production_changed,
        previous_application_running=result.operation.safety.previous_application_running,
        recovery_required=False,
        log_path=failure.log_path or str(result.release.operation_log_path),
        next_safe_action=failure.next_safe_action,
    )


def _deployment_result_data(
    result: DeploymentResult,
    *,
    non_interactive: bool,
) -> dict[str, object]:
    release = result.release
    common: dict[str, object] = {
        "command": "deploy",
        "outcome": release.status.value,
        "release_id": release.release_id,
        "operation_id": release.operation_id,
        "requested_version": release.requested_version,
        "production_changed": result.operation.safety.production_changed,
        "previous_application_running": result.operation.safety.previous_application_running,
        "recovery_required": result.operation.safety.recovery_required,
        "traffic_activated": release.traffic_activated,
        "final_backup_id": release.final_backup_id,
        "final_backup_sha256": release.final_backup_sha256,
        "log_path": str(release.operation_log_path),
        "non_interactive": non_interactive,
    }
    if result.active:
        return {"ok": True, **common}
    if result.rolled_back:
        payload = _rolled_back_failure(result)
        return {**payload.as_dict(), **common}
    raise ValueError(f"deployment returned non-terminal state {release.status.value}")


def _render_deployment_result(
    result: DeploymentResult,
    *,
    json_output: bool,
    non_interactive: bool,
) -> str:
    data = _deployment_result_data(result, non_interactive=non_interactive)
    if json_output:
        return json.dumps(data, sort_keys=True, separators=(",", ":"))
    release = result.release
    if release.status is DeploymentState.ACTIVE:
        return "\n".join(
            (
                "DployDB deployment completed.",
                "Outcome: active",
                f"Release ID: {release.release_id}",
                f"Version: {release.requested_version}",
                "Production changed: yes",
                "Previous application running: no",
                "Recovery required: no",
                f"Traffic activated: {_yes_no_unknown(release.traffic_activated)}",
                f"Final backup: {release.final_backup_id or 'not available'}",
                f"Relevant log: {release.operation_log_path}",
                "Next safe action: monitor the active release and preserve its final backup.",
            )
        )
    failure = _rolled_back_failure(result)
    return "\n".join(
        (
            "DployDB deployment was rolled back safely.",
            "Outcome: rolled_back",
            f"Release ID: {release.release_id}",
            f"Version: {release.requested_version}",
            render_failure(failure, json_output=False),
        )
    )


def _release_role(release_id: str, pointers: ReleasePointers | None) -> str:
    if pointers is None:
        return "history"
    if release_id == pointers.active_release_id:
        return "active"
    if release_id == pointers.previous_release_id:
        return "previous"
    return "history"


def _release_summary(
    manifest: ReleaseManifest,
    *,
    pointers: ReleasePointers | None,
) -> dict[str, object]:
    timestamps = manifest.model_dump(mode="json")
    return {
        "release_id": manifest.release_id,
        "operation_id": manifest.operation_id,
        "requested_version": manifest.requested_version,
        "status": manifest.status.value,
        "role": _release_role(manifest.release_id, pointers),
        "protected": manifest.release_id
        in {
            None if pointers is None else pointers.active_release_id,
            None if pointers is None else pointers.previous_release_id,
        },
        "production_changed": manifest.production_changed,
        "traffic_activated": manifest.traffic_activated,
        "final_backup_id": manifest.final_backup_id,
        "started_at": timestamps["started_at"],
        "completed_at": timestamps["completed_at"],
        "failure_code": None if manifest.failure is None else manifest.failure.error_code,
        "log_path": str(manifest.operation_log_path),
    }


def _release_history_data(
    history: ReleaseHistorySnapshot,
    *,
    project: str,
    secrets: SecretRegistry,
) -> dict[str, object]:
    pointers = history.pointers
    releases = [_release_summary(manifest, pointers=pointers) for manifest in history.releases]
    raw: JsonValue = {
        "ok": True,
        "command": "releases",
        "project": project,
        "active_release_id": None if pointers is None else pointers.active_release_id,
        "previous_release_id": None if pointers is None else pointers.previous_release_id,
        "count": len(releases),
        "releases": cast(list[JsonValue], releases),
    }
    redacted = secrets.redact(raw)
    if not isinstance(redacted, dict):
        raise TypeError("release history must serialize as an object")
    return cast(dict[str, object], redacted)


def _render_release_history(
    history: ReleaseHistorySnapshot,
    *,
    project: str,
    json_output: bool,
    secrets: SecretRegistry,
) -> str:
    data = _release_history_data(history, project=project, secrets=secrets)
    if json_output:
        return json.dumps(data, sort_keys=True, separators=(",", ":"))
    lines = [
        "DployDB releases",
        f"Project: {data['project']}",
        f"Active release: {data['active_release_id'] or 'none'}",
        f"Previous release: {data['previous_release_id'] or 'none'}",
        f"Release count: {data['count']}",
    ]
    summaries = cast(list[dict[str, object]], data["releases"])
    if not summaries:
        lines.append("No deployment releases have been recorded.")
    for item in summaries:
        lines.append(
            "- "
            f"{item['release_id']} version={item['requested_version']} "
            f"status={item['status']} role={item['role']} started={item['started_at']}"
        )
    lines.append("Next safe action: inspect a release with dploydb release show <release-id>.")
    return "\n".join(lines)


def _release_detail_data(
    manifest: ReleaseManifest,
    *,
    pointers: ReleasePointers | None,
    secrets: SecretRegistry,
) -> dict[str, object]:
    raw_manifest = cast(JsonValue, manifest.model_dump(mode="json"))
    raw: JsonValue = {
        "ok": True,
        "command": "release show",
        "role": _release_role(manifest.release_id, pointers),
        "protected": manifest.release_id
        in {
            None if pointers is None else pointers.active_release_id,
            None if pointers is None else pointers.previous_release_id,
        },
        "release": raw_manifest,
    }
    redacted = secrets.redact(raw)
    if not isinstance(redacted, dict):
        raise TypeError("release detail must serialize as an object")
    return cast(dict[str, object], redacted)


def _render_release_detail(
    manifest: ReleaseManifest,
    *,
    pointers: ReleasePointers | None,
    json_output: bool,
    secrets: SecretRegistry,
) -> str:
    data = _release_detail_data(manifest, pointers=pointers, secrets=secrets)
    if json_output:
        return json.dumps(data, sort_keys=True, separators=(",", ":"))
    release = cast(dict[str, object], data["release"])
    lines = [
        "DployDB release",
        f"Release ID: {release['release_id']}",
        f"Version: {release['requested_version']}",
        f"Status: {release['status']}",
        f"Role: {data['role']}",
        f"Protected: {_yes_no_unknown(cast(bool, data['protected']))}",
        f"Previous release: {release['previous_release_id'] or 'none'}",
        f"Production changed: {_yes_no_unknown(cast(bool, release['production_changed']))}",
        f"Traffic activated: {_yes_no_unknown(cast(bool, release['traffic_activated']))}",
        f"Rehearsal backup: {release['rehearsal_backup_id'] or 'none'}",
        f"Final backup: {release['final_backup_id'] or 'none'}",
        f"Started: {release['started_at']}",
        f"Completed: {release['completed_at'] or 'not completed'}",
        f"Relevant log: {release['operation_log_path']}",
    ]
    failure = release["failure"]
    if isinstance(failure, dict):
        lines.extend(
            (
                f"Failure: {failure['what_failed']}",
                f"Next safe action: {failure['next_safe_action']}",
            )
        )
    else:
        lines.append("Next safe action: preserve this release and its referenced backups.")
    return "\n".join(lines)


def _restore_preview_data(
    selection: RestoreSelection,
    *,
    secrets: SecretRegistry,
) -> dict[str, object]:
    raw = cast(JsonValue, selection.as_dict())
    redacted = secrets.redact(raw)
    if not isinstance(redacted, dict):
        raise TypeError("restore preview must serialize as an object")
    return {
        "ok": True,
        "command": "restore",
        "outcome": "preview",
        "executed": False,
        "confirmation_required": True,
        **cast(dict[str, object], redacted),
    }


def _render_restore_preview(
    selection: RestoreSelection,
    *,
    json_output: bool,
    secrets: SecretRegistry,
) -> str:
    data = _restore_preview_data(selection, secrets=secrets)
    if json_output:
        return json.dumps(data, sort_keys=True, separators=(",", ":"))
    return "\n".join(
        (
            "DployDB manual restore preview",
            DATA_LOSS_WARNING,
            f"Current release: {data['active_release_id']} ({data['active_version']})",
            f"Selected release: {data['selected_release_id']} ({data['selected_version']})",
            f"Selected backup: {data['selected_backup_id']}",
            f"Selected backup SHA-256: {data['selected_backup_sha256']}",
            f"Production database: {data['database_path']}",
            f"Current container: {data['current_container_name']}",
            f"Selected container: {data['selected_container_name']}",
            "A verified backup of the current database will be created before replacement.",
        )
    )


def _render_manual_restore_result(
    result: ManualRestoreResult,
    *,
    json_output: bool,
    secrets: SecretRegistry,
) -> str:
    raw = cast(JsonValue, result.as_dict())
    redacted = secrets.redact(raw)
    if not isinstance(redacted, dict):
        raise TypeError("manual restore result must serialize as an object")
    data = cast(dict[str, object], redacted)
    if json_output:
        return json.dumps(data, sort_keys=True, separators=(",", ":"))
    return "\n".join(
        (
            "DployDB manual restore completed.",
            f"Selected release: {data['selected_release_id']} ({data['selected_version']})",
            f"Replaced release: {data['replaced_release_id']}",
            f"Pre-restore backup: {data['pre_restore_backup_id']}",
            f"Restored backup: {data['restored_backup_id']}",
            f"Database SHA-256: {data['database_sha256']}",
            "Production changed: yes",
            "Previous application running: yes",
            "Recovery required: no",
            f"Relevant log: {data['log_path']}",
            "Next safe action: monitor the restored release and preserve the pre-restore backup.",
        )
    )


def _recovery_plan_data(
    plan: RecoveryPlan,
    *,
    secrets: SecretRegistry,
) -> dict[str, object]:
    raw = cast(JsonValue, plan.as_dict())
    redacted = secrets.redact(raw)
    if not isinstance(redacted, dict):
        raise TypeError("recovery plan must serialize as an object")
    return {
        "ok": plan.disposition is not RecoveryDisposition.MANUAL_REQUIRED,
        "command": "recover",
        "outcome": "preview",
        "executed": False,
        "confirmation_required": plan.executable,
        **cast(dict[str, object], redacted),
    }


def _render_recovery_plan(
    plan: RecoveryPlan,
    *,
    json_output: bool,
    secrets: SecretRegistry,
) -> str:
    data = _recovery_plan_data(plan, secrets=secrets)
    if json_output:
        return json.dumps(data, sort_keys=True, separators=(",", ":"))
    actions = cast(list[str], data["actions"])
    return "\n".join(
        (
            "DployDB recovery diagnosis",
            f"Disposition: {data['disposition']}",
            f"Release ID: {data['release_id']}",
            f"Source operation: {data['operation_id']}",
            f"Durable stage: {data['durable_stage']}",
            "Production may have changed: "
            f"{_yes_no_unknown(cast(bool, data['production_may_have_changed']))}",
            "Traffic may have switched: "
            f"{_yes_no_unknown(cast(bool, data['traffic_may_have_switched']))}",
            "Automatic database restore allowed: "
            f"{_yes_no_unknown(cast(bool, data['automatic_database_restore_allowed']))}",
            f"Ordered actions: {', '.join(actions) if actions else 'none'}",
            f"Reason: {data['reason']}",
            f"Next safe action: {data['next_safe_action']}",
        )
    )


def _render_recovery_result(
    result: RecoveryResult,
    *,
    json_output: bool,
    secrets: SecretRegistry,
) -> str:
    raw = cast(JsonValue, result.as_dict())
    redacted = secrets.redact(raw)
    if not isinstance(redacted, dict):
        raise TypeError("recovery result must serialize as an object")
    data = cast(dict[str, object], redacted)
    if json_output:
        return json.dumps(data, sort_keys=True, separators=(",", ":"))
    return "\n".join(
        (
            "DployDB recovery completed.",
            f"Outcome: {data['outcome']}",
            f"Release ID: {data['release_id']}",
            f"Recovery operation: {data['recovery_operation_id']}",
            f"Source operation: {data['source_operation_id']}",
            f"Production changed: {_yes_no_unknown(cast(bool, data['production_changed']))}",
            "Previous application running: "
            f"{_yes_no_unknown(cast(bool, data['previous_application_running']))}",
            "Recovery required: no",
            f"Relevant log: {data['log_path']}",
            "Next safe action: monitor the recovered release and preserve all recovery evidence.",
        )
    )


def _version_callback(value: bool) -> None:
    if value:
        console.print(_version_text())
        raise typer.Exit()


@app.callback()
def main(
    context: typer.Context,
    show_version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed DployDB version and exit.",
        ),
    ] = None,
    no_color: Annotated[
        bool,
        typer.Option(
            "--no-color",
            help="Disable terminal color; the NO_COLOR environment variable is also honored.",
        ),
    ] = False,
) -> None:
    """Deployment safety for SQLite applications."""
    if no_color or "NO_COLOR" in os.environ:
        context.color = False


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
    """Check configured host safety without changing production."""
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
    upload: Annotated[
        bool,
        typer.Option(
            "--upload",
            help="Upload the verified local backup to configured remote storage.",
        ),
    ] = False,
) -> None:
    """Create one verified local backup and optionally commit it remotely."""
    loaded = None
    try:
        loaded = load_configuration(config_path)
        result = create_configured_backup_result(loaded, upload=upload)
    except DployDBError as error:
        if loaded is not None:
            error = redact_error(error, secrets=loaded.secrets)
        abort_with_error(error, json_output=json_output)
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)
    typer.echo(
        _render_backup_result(
            result.local,
            command="backup",
            json_output=json_output,
            secrets=loaded.secrets,
            remote=result.remote,
        )
    )


@app.command("releases")
def releases_command(
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Configuration file whose releases are listed."),
    ] = DEFAULT_CONFIG_PATH,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """List every validated local deployment release without modifying state."""
    loaded = None
    try:
        loaded = load_configuration(config_path)
        history = ReleaseStore(
            loaded.config.state_directory,
            secrets=loaded.secrets,
        ).read_history()
    except DployDBError as error:
        if loaded is not None:
            error = redact_error(error, secrets=loaded.secrets)
        abort_with_error(error, json_output=json_output)
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)
    typer.echo(
        _render_release_history(
            history,
            project=loaded.config.project,
            json_output=json_output,
            secrets=loaded.secrets,
        )
    )


@release_app.command("show")
def release_show_command(
    release_id: Annotated[str, typer.Argument(help="Exact release ID to inspect.")],
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Configuration file whose release is shown."),
    ] = DEFAULT_CONFIG_PATH,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Show one complete validated release manifest without modifying state."""
    loaded = None
    try:
        loaded = load_configuration(config_path)
        manifest, pointers = ReleaseStore(
            loaded.config.state_directory,
            secrets=loaded.secrets,
        ).lookup_history_release(release_id)
    except DployDBError as error:
        if loaded is not None:
            error = redact_error(error, secrets=loaded.secrets)
        abort_with_error(error, json_output=json_output)
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)
    typer.echo(
        _render_release_detail(
            manifest,
            pointers=pointers,
            json_output=json_output,
            secrets=loaded.secrets,
        )
    )


@app.command("restore")
def restore_command(
    release_id: Annotated[
        str,
        typer.Argument(help="Protected previous release ID to restore."),
    ],
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Configuration file for manual restore."),
    ] = DEFAULT_CONFIG_PATH,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Acknowledge the data-loss warning and execute restore."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Preview or confirm a backup-first restore of the protected previous release."""
    loaded = None
    try:
        loaded = load_configuration(config_path)
        preview = preview_configured_restore(loaded, release_id)
    except DployDBError as error:
        if loaded is not None:
            error = redact_error(error, secrets=loaded.secrets)
        abort_with_error(error, json_output=json_output)
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)

    if not yes:
        typer.echo(
            _render_restore_preview(
                preview,
                json_output=json_output,
                secrets=loaded.secrets,
            )
        )
        if json_output:
            return
        confirmed = typer.confirm(
            "Proceed with this destructive restore after creating the current-state backup?",
            default=False,
        )
        if not confirmed:
            typer.echo("Manual restore cancelled; production was not changed.")
            return

    try:
        result = restore_configured_release(
            loaded,
            release_id,
            config_path=config_path,
        )
    except DployDBError as error:
        abort_with_error(
            redact_error(error, secrets=loaded.secrets),
            json_output=json_output,
        )
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)
    typer.echo(
        _render_manual_restore_result(
            result,
            json_output=json_output,
            secrets=loaded.secrets,
        )
    )


@app.command("recover")
def recover_command(
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Configuration file for recovery."),
    ] = DEFAULT_CONFIG_PATH,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm execution of the diagnosed recovery plan."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
) -> None:
    """Diagnose or execute recovery for an interrupted deployment."""
    loaded = None
    try:
        loaded = load_configuration(config_path)
        plan = preview_configured_recovery(loaded, config_path=config_path)
    except DployDBError as error:
        if loaded is not None:
            error = redact_error(error, secrets=loaded.secrets)
        abort_with_error(error, json_output=json_output)
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)

    if plan.disposition is RecoveryDisposition.MANUAL_REQUIRED:
        abort_with_error(
            RecoveryRequiredError(
                plan.reason,
                production_changed=plan.production_may_have_changed,
                previous_application_running=None,
                log_path=(
                    loaded.config.state_directory
                    / "operations"
                    / plan.operation_id
                    / "events.jsonl"
                ),
                next_safe_action=plan.next_safe_action,
            ),
            json_output=json_output,
        )
    if plan.disposition is RecoveryDisposition.NO_ACTION:
        typer.echo(
            _render_recovery_plan(
                plan,
                json_output=json_output,
                secrets=loaded.secrets,
            )
        )
        return
    if not yes:
        typer.echo(
            _render_recovery_plan(
                plan,
                json_output=json_output,
                secrets=loaded.secrets,
            )
        )
        if json_output:
            return
        confirmed = typer.confirm(
            "Execute exactly this recovery plan after revalidating it under the lock?",
            default=False,
        )
        if not confirmed:
            typer.echo("Recovery execution cancelled; diagnosis was read-only.")
            return
    try:
        result = recover_configured_deployment(
            loaded,
            config_path=config_path,
        )
    except DployDBError as error:
        abort_with_error(
            redact_error(error, secrets=loaded.secrets),
            json_output=json_output,
        )
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)
    typer.echo(
        _render_recovery_result(
            result,
            json_output=json_output,
            secrets=loaded.secrets,
        )
    )


@app.command("deploy")
def deploy_command(
    release_version: Annotated[
        str,
        typer.Option("--version", help="Validated release version to deploy."),
    ],
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Configuration file for the deployment."),
    ] = DEFAULT_CONFIG_PATH,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable output."),
    ] = False,
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            help="Never wait for terminal input; fail when future confirmation is required.",
        ),
    ] = False,
) -> None:
    """Run the checked production cutover with automatic pre-traffic rollback."""
    loaded = None
    try:
        loaded = load_configuration(config_path)
        result = deploy_configured_release(
            loaded,
            version=release_version,
            config_path=config_path,
        )
    except DployDBError as error:
        if loaded is not None:
            error = redact_error(error, secrets=loaded.secrets)
        abort_with_error(error, json_output=json_output)
    except Exception:
        abort_with_error(_internal_error(), json_output=json_output)

    typer.echo(
        _render_deployment_result(
            result,
            json_output=json_output,
            non_interactive=non_interactive,
        ),
        err=not json_output and result.rolled_back,
    )
    if result.rolled_back:
        raise typer.Exit(code=_rolled_back_failure(result).exit_code)


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
