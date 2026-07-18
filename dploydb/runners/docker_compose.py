"""Isolated Docker Compose candidate runner for one Linux host."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Final

from dploydb.config import ApplicationConfig
from dploydb.redaction import SecretRegistry, is_sensitive_key
from dploydb.runners.base import (
    CandidateCleanup,
    CandidateCleanupError,
    CandidateCleanupProof,
    CandidateHandle,
    CandidateInspection,
    CandidateInspectionError,
    CandidateLogs,
    CandidateMount,
    CandidateStart,
    CandidateStartError,
    CommandExecutor,
    validate_operation_id,
    validate_release_identifier,
)
from dploydb.subprocesses import CommandOutcome, CommandResult, SubprocessRunner

DPLOYDB_VERSION_ENV: Final = "DPLOYDB_VERSION"
OPERATION_LABEL: Final = "io.dploydb.operation_id"
ROLE_LABEL: Final = "io.dploydb.role"
ROLE_CANDIDATE: Final = "candidate"
COMPOSE_PROJECT_LABEL: Final = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL: Final = "com.docker.compose.service"
CANDIDATE_MAX_OUTPUT_BYTES: Final = 256 * 1024
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class DockerComposeCandidateRunner:
    """Start and remove one operation-scoped Compose candidate without production control."""

    def __init__(
        self,
        *,
        project: str,
        application: ApplicationConfig,
        database_environment_name: str,
        production_database_path: Path,
        secrets: SecretRegistry,
        working_directory: Path,
        command_environment: Mapping[str, str] | None = None,
        command_runner: CommandExecutor | None = None,
    ) -> None:
        if not isinstance(project, str) or _PROJECT_NAME.fullmatch(project) is None:
            raise ValueError("project must be a bounded DployDB project identifier")
        if (
            not isinstance(database_environment_name, str)
            or _ENVIRONMENT_NAME.fullmatch(database_environment_name) is None
            or database_environment_name == DPLOYDB_VERSION_ENV
        ):
            raise ValueError("database_environment_name must be a safe non-reserved name")
        self.project = project
        self.application = application
        if DPLOYDB_VERSION_ENV in application.test_mode_env:
            raise ValueError("test_mode_env must not override DPLOYDB_VERSION")
        self.database_environment_name = database_environment_name
        self.production_database_path = _absolute_resolved_path(
            production_database_path, "production_database_path"
        )
        if not self.production_database_path.is_file():
            raise ValueError("production_database_path must identify an existing file")
        self.secrets = secrets
        self.working_directory = _absolute_resolved_path(working_directory, "working_directory")
        self.command_environment = dict(
            os.environ if command_environment is None else command_environment
        )
        self.command_runner = command_runner or SubprocessRunner(
            secrets=secrets,
            max_output_bytes=CANDIDATE_MAX_OUTPUT_BYTES,
        )

    def start(
        self,
        *,
        operation_id: str,
        version: str,
        rehearsal_database_path: Path,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateStart:
        """Create one isolated one-off service container and clean failed starts."""
        operation = validate_operation_id(operation_id)
        release = validate_release_identifier(version)
        rehearsal = _absolute_resolved_path(rehearsal_database_path, "rehearsal_database_path")
        if not rehearsal.is_file():
            raise ValueError("rehearsal_database_path must identify an existing file")
        if ":" in str(rehearsal.parent):
            raise ValueError("rehearsal workspace path must not contain a colon")
        if rehearsal == self.production_database_path or os.path.samefile(
            rehearsal, self.production_database_path
        ):
            raise ValueError("candidate database must not alias the production database")
        if _is_relative_to(self.production_database_path, rehearsal.parent):
            raise ValueError("rehearsal workspace must not contain the production database")

        handle = self._handle(operation, release, rehearsal)
        environment = self._environment(release)
        command = self._compose_command(
            handle.compose_project,
            "run",
            "--detach",
            "--no-TTY",
            "--no-deps",
            "--build",
            "--name",
            handle.container_name,
            "--label",
            f"{OPERATION_LABEL}={operation}",
            "--label",
            f"{ROLE_LABEL}={ROLE_CANDIDATE}",
            "--publish",
            (
                f"127.0.0.1:{self.application.candidate_port}:"
                f"{self.application.candidate_container_port}"
            ),
            "--volume",
            f"{rehearsal.parent}:{self.application.database_volume_target}:rw",
            "--env",
            f"{self.database_environment_name}={handle.candidate_database_path}",
            *self._test_environment_arguments(),
            self.application.service,
        )
        result = self._run(command, environment=environment, cancellation_event=cancellation_event)
        output_lines = _nonempty_lines(result.stdout.text)
        container_reference = "" if not output_lines else output_lines[-1]
        expected_reference = (
            _CONTAINER_ID.fullmatch(container_reference) is not None
            or container_reference == handle.container_name
        )
        if (
            result.outcome is not CommandOutcome.SUCCEEDED
            or result.stdout.truncated
            or result.stderr.truncated
            or not expected_reference
        ):
            message = _command_failure("candidate Compose startup", result)
            cleanup = self._cleanup_after_failure(handle)
            if not cleanup.proof.proven:
                message += "; candidate cleanup could not be proven"
            raise CandidateStartError(message, command=result, cleanup=cleanup)
        return CandidateStart(
            handle=handle,
            container_reference=container_reference,
            command=result,
        )

    def inspect(
        self,
        handle: CandidateHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateInspection:
        """Inspect and validate actual state before readiness checks may begin."""
        self._validate_handle(handle)
        result = self._run(
            ("docker", "container", "inspect", handle.container_name),
            environment=self._environment(handle.version),
            cancellation_event=cancellation_event,
        )
        if result.outcome is not CommandOutcome.SUCCEEDED:
            raise CandidateInspectionError(
                _command_failure("candidate inspection", result), command=result
            )
        if result.stdout.truncated or result.stderr.truncated:
            raise CandidateInspectionError(
                "candidate inspection exceeded the bounded evidence limit", command=result
            )
        try:
            payload = json.loads(result.stdout.text)
            raw = _single_inspection(payload)
            inspection, failures = self._validated_inspection(handle, raw, result)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CandidateInspectionError(
                "candidate inspection returned invalid Docker JSON: "
                + self.secrets.redact_text(str(exc)),
                command=result,
            ) from None
        if failures:
            raise CandidateInspectionError(
                "candidate isolation inspection failed: " + "; ".join(failures),
                command=result,
            )
        return inspection

    def collect_logs(
        self,
        handle: CandidateHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateLogs:
        """Collect bounded redacted logs from exactly the candidate container."""
        self._validate_handle(handle)
        result = self._run(
            ("docker", "container", "logs", handle.container_name),
            environment=self._environment(handle.version),
            cancellation_event=cancellation_event,
        )
        return CandidateLogs(handle=handle, command=result)

    def stop(self, handle: CandidateHandle) -> CandidateCleanup:
        """Idempotently remove only the candidate and its isolated Compose project."""
        self._validate_handle(handle)
        environment = self._environment(handle.version)
        presence = self._candidate_container_query(handle, environment=environment)
        remove: CommandResult | None = None
        if _successful_complete(presence):
            names = _nonempty_lines(presence.stdout.text)
            if handle.container_name in names:
                remove = self._run(
                    ("docker", "container", "rm", "--force", handle.container_name),
                    environment=environment,
                )
        down = self._run(
            self._compose_command(
                handle.compose_project,
                "down",
                "--remove-orphans",
                "--timeout",
                str(self.application.startup_timeout_seconds),
            ),
            environment=environment,
        )
        proof = self.prove_cleanup(handle)
        cleanup = CandidateCleanup(
            presence_query=presence,
            remove_command=remove,
            compose_down=down,
            proof=proof,
        )
        commands_succeeded = _successful_complete(presence) and _successful_complete(down)
        if remove is not None:
            commands_succeeded = commands_succeeded and _successful_complete(remove)
        if not commands_succeeded or not proof.proven:
            raise CandidateCleanupError(
                "candidate cleanup could not be proven for the isolated Compose project",
                command=down,
                cleanup=cleanup,
            )
        return cleanup

    def prove_cleanup(self, handle: CandidateHandle) -> CandidateCleanupProof:
        """Prove absence through successful bounded Docker container/network queries."""
        self._validate_handle(handle)
        environment = self._environment(handle.version)
        container_query = self._candidate_container_query(handle, environment=environment)
        network_query = self._run(
            (
                "docker",
                "network",
                "ls",
                "--filter",
                f"label={COMPOSE_PROJECT_LABEL}={handle.compose_project}",
                "--format",
                "{{.ID}}",
            ),
            environment=environment,
        )
        container_absent = _successful_complete(container_query) and not _nonempty_lines(
            container_query.stdout.text
        )
        networks_absent = _successful_complete(network_query) and not _nonempty_lines(
            network_query.stdout.text
        )
        return CandidateCleanupProof(
            container_absent=container_absent,
            networks_absent=networks_absent,
            container_query=container_query,
            network_query=network_query,
        )

    def _handle(self, operation_id: str, version: str, rehearsal: Path) -> CandidateHandle:
        operation_suffix = operation_id.removeprefix("op_")[:16]
        safe_project = re.sub(r"[^a-z0-9_-]", "-", self.project.lower())
        safe_project = safe_project.strip("-_") or "app"
        compose_project = f"dploydb-{safe_project[:24]}-{operation_suffix}"
        container_name = f"{compose_project}-candidate"
        target = PurePosixPath(self.application.database_volume_target)
        return CandidateHandle(
            operation_id=operation_id,
            version=version,
            compose_project=compose_project,
            container_name=container_name,
            rehearsal_database_path=rehearsal,
            candidate_database_path=str(target / rehearsal.name),
        )

    def _validate_handle(self, handle: CandidateHandle) -> None:
        if not isinstance(handle, CandidateHandle):
            raise TypeError("handle must be a CandidateHandle")
        expected = self._handle(
            validate_operation_id(handle.operation_id),
            validate_release_identifier(handle.version),
            _absolute_resolved_path(
                handle.rehearsal_database_path, "handle.rehearsal_database_path"
            ),
        )
        if handle != expected:
            raise ValueError("candidate handle does not match its derived resource identity")

    def _validated_inspection(
        self,
        handle: CandidateHandle,
        raw: dict[str, Any],
        command: CommandResult,
    ) -> tuple[CandidateInspection, list[str]]:
        failures: list[str] = []
        container_id = _required_text(raw, "Id")
        name = _required_text(raw, "Name").removeprefix("/")
        state = _required_mapping(raw, "State")
        running = state.get("Running") is True
        config = _required_mapping(raw, "Config")
        labels = config.get("Labels")
        if not isinstance(labels, dict):
            labels = {}
        mounts = _mounts(raw.get("Mounts"))
        port_bindings = _published_ports(raw)

        if name != handle.container_name:
            failures.append("container name does not match the operation-derived name")
        if _CONTAINER_ID.fullmatch(container_id) is None:
            failures.append("container ID is invalid")
        if not running:
            failures.append("container is not running")
        if labels.get(COMPOSE_PROJECT_LABEL) != handle.compose_project:
            failures.append("Compose project label does not match the isolated project")
        if labels.get(COMPOSE_SERVICE_LABEL) != self.application.service:
            failures.append("Compose service label does not match the configured service")
        if labels.get(OPERATION_LABEL) != handle.operation_id:
            failures.append("DployDB operation label is missing or contradictory")
        if labels.get(ROLE_LABEL) != ROLE_CANDIDATE:
            failures.append("DployDB candidate role label is missing or contradictory")

        expected_target = self.application.database_volume_target
        database_mounts = [mount for mount in mounts if mount.destination == expected_target]
        expected_source = handle.rehearsal_database_path.parent.resolve()
        if len(database_mounts) != 1:
            failures.append("candidate database target does not have exactly one mount")
        else:
            database_mount = database_mounts[0]
            source = Path(database_mount.source).resolve()
            if database_mount.mount_type != "bind" or source != expected_source:
                failures.append("candidate database target is not bound to the rehearsal workspace")
            if not database_mount.read_write:
                failures.append("candidate database mount is not writable")

        production = self.production_database_path
        for mount in mounts:
            if mount.mount_type != "bind" or not mount.source.startswith("/"):
                continue
            source = Path(mount.source).resolve()
            if source == production or _is_relative_to(production, source):
                failures.append("candidate bind mount exposes the production database")
                break

        expected_port = self.application.candidate_container_port
        published = [binding for binding in port_bindings if binding[3]]
        matching = [binding for binding in published if binding[0] == expected_port]
        if len(matching) != 1:
            failures.append("candidate container port does not have exactly one published binding")
            host_ip, host_port = "", 0
        else:
            _container_port, host_ip, host_port, _published = matching[0]
            if host_ip != "127.0.0.1" or host_port != self.application.candidate_port:
                failures.append("candidate port is not bound to the configured loopback endpoint")
        if len(published) != 1:
            failures.append("candidate publishes ports beyond the configured candidate endpoint")

        inspection = CandidateInspection(
            container_id=container_id,
            container_name=name,
            running=running,
            compose_project=str(labels.get(COMPOSE_PROJECT_LABEL, "")),
            compose_service=str(labels.get(COMPOSE_SERVICE_LABEL, "")),
            operation_id=str(labels.get(OPERATION_LABEL, "")),
            host_ip=host_ip,
            host_port=host_port,
            container_port=expected_port,
            mounts=mounts,
            command=command,
        )
        return inspection, failures

    def _cleanup_after_failure(self, handle: CandidateHandle) -> CandidateCleanup:
        try:
            return self.stop(handle)
        except CandidateCleanupError as error:
            assert error.cleanup is not None
            return error.cleanup

    def _candidate_container_query(
        self,
        handle: CandidateHandle,
        *,
        environment: Mapping[str, str],
    ) -> CommandResult:
        return self._run(
            (
                "docker",
                "container",
                "ls",
                "--all",
                "--filter",
                f"name=^/{handle.container_name}$",
                "--format",
                "{{.Names}}",
            ),
            environment=environment,
        )

    def _compose_command(self, project: str, *arguments: str) -> tuple[str, ...]:
        return (
            "docker",
            "compose",
            "--file",
            str(self.application.compose_file),
            "--project-name",
            project,
            *arguments,
        )

    def _test_environment_arguments(self) -> tuple[str, ...]:
        arguments: list[str] = []
        for name in sorted(self.application.test_mode_env):
            arguments.extend(("--env", f"{name}={self.application.test_mode_env[name]}"))
        return tuple(arguments)

    def _environment(self, version: str) -> dict[str, str]:
        environment = dict(self.command_environment)
        environment[DPLOYDB_VERSION_ENV] = version
        environment.update(self.application.test_mode_env)
        for name, value in environment.items():
            if is_sensitive_key(name):
                self.secrets.register(value)
        return environment

    def _run(
        self,
        command: tuple[str, ...],
        *,
        environment: Mapping[str, str],
        cancellation_event: threading.Event | None = None,
    ) -> CommandResult:
        return self.command_runner.run(
            command,
            timeout_seconds=self.application.startup_timeout_seconds,
            environment=environment,
            working_directory=self.working_directory,
            cancellation_event=cancellation_event,
        )


def _absolute_resolved_path(value: Path, name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a pathlib.Path")
    if not value.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return value.resolve()


def _single_inspection(value: object) -> dict[str, Any]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ValueError("expected exactly one inspected container")
    return value[0]


def _required_mapping(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    selected = value.get(key)
    if not isinstance(selected, dict):
        raise ValueError(f"inspection field {key} must be an object")
    return selected


def _required_text(value: Mapping[str, Any], key: str) -> str:
    selected = value.get(key)
    if not isinstance(selected, str) or not selected:
        raise ValueError(f"inspection field {key} must be non-empty text")
    return selected


def _mounts(value: object) -> tuple[CandidateMount, ...]:
    if not isinstance(value, list):
        raise ValueError("inspection Mounts must be an array")
    mounts: list[CandidateMount] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("inspection mount must be an object")
        mount_type = item.get("Type")
        source = item.get("Source")
        destination = item.get("Destination")
        read_write = item.get("RW")
        if (
            not isinstance(mount_type, str)
            or not isinstance(source, str)
            or not isinstance(destination, str)
            or not isinstance(read_write, bool)
        ):
            raise ValueError("inspection mount contains invalid fields")
        mounts.append(
            CandidateMount(
                mount_type=mount_type,
                source=source,
                destination=destination,
                read_write=read_write,
            )
        )
    return tuple(mounts)


def _published_ports(value: Mapping[str, Any]) -> list[tuple[int, str, int, bool]]:
    network = _required_mapping(value, "NetworkSettings")
    ports = network.get("Ports")
    if not isinstance(ports, dict):
        raise ValueError("inspection NetworkSettings.Ports must be an object")
    bindings: list[tuple[int, str, int, bool]] = []
    for name, raw_bindings in ports.items():
        if not isinstance(name, str) or not name.endswith("/tcp"):
            continue
        try:
            container_port = int(name.removesuffix("/tcp"))
        except ValueError as exc:
            raise ValueError("inspection contains an invalid container port") from exc
        if raw_bindings is None:
            bindings.append((container_port, "", 0, False))
            continue
        if not isinstance(raw_bindings, list):
            raise ValueError("inspection port bindings must be an array or null")
        for binding in raw_bindings:
            if not isinstance(binding, dict):
                raise ValueError("inspection port binding must be an object")
            host_ip = binding.get("HostIp")
            host_port_text = binding.get("HostPort")
            if not isinstance(host_ip, str) or not isinstance(host_port_text, str):
                raise ValueError("inspection port binding contains invalid fields")
            try:
                host_port = int(host_port_text)
            except ValueError as exc:
                raise ValueError("inspection host port is invalid") from exc
            bindings.append((container_port, host_ip, host_port, True))
    return bindings


def _successful_complete(result: CommandResult) -> bool:
    return (
        result.outcome is CommandOutcome.SUCCEEDED
        and not result.stdout.truncated
        and not result.stderr.truncated
    )


def _nonempty_lines(value: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in value.splitlines() if line.strip())


def _command_failure(action: str, result: CommandResult) -> str:
    if result.stdout.truncated or result.stderr.truncated:
        return f"{action} exceeded the bounded evidence limit"
    if result.outcome is CommandOutcome.NONZERO_EXIT:
        return f"{action} exited with status {result.exit_code}"
    if result.outcome is CommandOutcome.TIMED_OUT:
        return f"{action} timed out and its process group was terminated"
    if result.outcome is CommandOutcome.CANCELLED:
        return f"{action} was cancelled and its process group was terminated"
    if result.outcome is CommandOutcome.CLEANUP_FAILED:
        return f"{action} process cleanup could not be proven"
    if result.outcome is CommandOutcome.START_FAILED:
        return f"{action} could not start: {result.start_error or 'unknown error'}"
    reference = result.stdout.text.strip()
    return f"{action} returned invalid success evidence: {reference!r}"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
