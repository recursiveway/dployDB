"""Docker Compose production lifecycle with exact previous-container preservation."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Final

from dploydb.config import ApplicationConfig, ProductionTopology
from dploydb.health import CANDIDATE_URL_ENV
from dploydb.models import ProductionApplicationHandle
from dploydb.redaction import SecretRegistry, is_sensitive_key
from dploydb.runners.base import (
    CandidateMount,
    CommandExecutor,
    ProductionCleanup,
    ProductionCleanupError,
    ProductionCleanupProof,
    ProductionDiscovery,
    ProductionDiscoveryError,
    ProductionInspection,
    ProductionInspectionError,
    ProductionLogs,
    ProductionRestart,
    ProductionRestartError,
    ProductionStart,
    ProductionStartError,
    ProductionStop,
    ProductionStopError,
    validate_operation_id,
    validate_release_identifier,
)
from dploydb.runners.docker_compose import (
    COMPOSE_PROJECT_LABEL,
    COMPOSE_SERVICE_LABEL,
    DPLOYDB_VERSION_ENV,
    OPERATION_LABEL,
    ROLE_LABEL,
)
from dploydb.subprocesses import CommandOutcome, CommandResult, SubprocessRunner

ROLE_PRODUCTION_RELEASE: Final = "production_release"
RELEASE_LABEL: Final = "io.dploydb.release_id"
PRODUCTION_MAX_OUTPUT_BYTES: Final = 256 * 1024

_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_RELEASE_ID = re.compile(r"release_[0-9a-f]{32}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class DockerComposeProductionRunner:
    """Preserve, stop, start, inspect, and restart exact production containers."""

    def __init__(
        self,
        *,
        project: str,
        application: ApplicationConfig,
        topology: ProductionTopology,
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
        if application.production_project != topology.compose_project:
            raise ValueError("production topology project does not match application config")
        if application.production_port != topology.host_port:
            raise ValueError("production topology port does not match application config")
        if application.production_health_url != topology.health_url:
            raise ValueError("production topology health URL does not match application config")
        self.project = project
        self.application = application
        self.topology = topology
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
            max_output_bytes=PRODUCTION_MAX_OUTPUT_BYTES,
        )

    def discover_current(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionDiscovery:
        """Find exactly one configured current service and validate its live topology."""
        query = self._run(
            self._compose_command(
                self.topology.compose_project,
                "ps",
                "--all",
                "--quiet",
                self.application.service,
            ),
            environment=self._base_environment(),
            cancellation_event=cancellation_event,
        )
        references = _nonempty_lines(query.stdout.text)
        if not _successful_complete(query) or len(references) != 1:
            raise ProductionDiscoveryError(
                _command_failure("current application discovery", query),
                command=query,
            )
        reference = references[0]
        if _CONTAINER_ID.fullmatch(reference) is None:
            raise ProductionDiscoveryError(
                "current application discovery returned an invalid container identity",
                command=query,
            )
        inspect_result = query
        try:
            inspect_result, raw = self._inspect_raw(
                reference,
                environment=self._base_environment(),
                cancellation_event=cancellation_event,
            )
            container_id = _required_text(raw, "Id")
            container_name = _required_text(raw, "Name").removeprefix("/")
            handle = ProductionApplicationHandle(
                source="bootstrap",
                container_id=container_id,
                container_name=container_name,
                compose_project=self.topology.compose_project,
                compose_service=self.application.service,
                version=None,
                release_id=None,
                operation_id=None,
                database_directory=self.production_database_path.parent,
                database_target=self.application.database_volume_target,
                host_port=self.topology.host_port,
                container_port=self.application.candidate_container_port,
                health_url=self.topology.health_url,
            )
            inspection = self._validated_inspection(
                handle,
                raw,
                inspect_result,
                expected_running=True,
            )
        except (ProductionInspectionError, KeyError, TypeError, ValueError) as exc:
            raise ProductionDiscoveryError(
                "current application inspection was unsafe: " + self.secrets.redact_text(str(exc)),
                command=(
                    exc.command if isinstance(exc, ProductionInspectionError) else inspect_result
                ),
            ) from None
        return ProductionDiscovery(query=query, inspection=inspection)

    def inspect(
        self,
        handle: ProductionApplicationHandle,
        *,
        expected_running: bool,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionInspection:
        """Revalidate the exact stored application identity and required topology."""
        self._validate_handle(handle)
        result, raw = self._inspect_raw(
            handle.container_id,
            environment=self._environment_for_handle(handle),
            cancellation_event=cancellation_event,
        )
        try:
            return self._validated_inspection(
                handle,
                raw,
                result,
                expected_running=expected_running,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductionInspectionError(
                "production application inspection was unsafe: "
                + self.secrets.redact_text(str(exc)),
                command=result,
            ) from None

    def stop_current(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionStop:
        """Stop, but never remove, the exact previous application container."""
        self._validate_handle(handle)
        result = self._run(
            (
                "docker",
                "container",
                "stop",
                "--time",
                str(self.application.startup_timeout_seconds),
                handle.container_id,
            ),
            environment=self._environment_for_handle(handle),
            cancellation_event=cancellation_event,
        )
        if not _successful_complete(result):
            raise ProductionStopError(
                _command_failure("current application stop", result),
                command=result,
            )
        try:
            inspection = self.inspect(
                handle,
                expected_running=False,
                cancellation_event=cancellation_event,
            )
        except ProductionInspectionError as exc:
            raise ProductionStopError(str(exc), command=exc.command) from None
        return ProductionStop(handle=handle, command=result, inspection=inspection)

    def start_new(
        self,
        *,
        operation_id: str,
        release_id: str,
        version: str,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionStart:
        """Start a new release on the production port while preserving the old container."""
        operation = validate_operation_id(operation_id)
        if not isinstance(release_id, str) or _RELEASE_ID.fullmatch(release_id) is None:
            raise ValueError("release_id must be an opaque DployDB release ID")
        release = validate_release_identifier(version)
        compose_project, container_name = self._new_identity(release_id)
        environment = self._production_environment(release)
        target = PurePosixPath(self.application.database_volume_target)
        container_database_path = str(target / self.production_database_path.name)
        command = self._compose_command(
            compose_project,
            "run",
            "--detach",
            "--no-TTY",
            "--no-deps",
            "--build",
            "--name",
            container_name,
            "--label",
            f"{OPERATION_LABEL}={operation}",
            "--label",
            f"{ROLE_LABEL}={ROLE_PRODUCTION_RELEASE}",
            "--label",
            f"{RELEASE_LABEL}={release_id}",
            "--publish",
            (f"127.0.0.1:{self.topology.host_port}:{self.application.candidate_container_port}"),
            "--volume",
            f"{self.production_database_path.parent}:{self.application.database_volume_target}:rw",
            "--env",
            f"{self.database_environment_name}={container_database_path}",
            self.application.service,
        )
        result = self._run(
            command,
            environment=environment,
            cancellation_event=cancellation_event,
        )
        output_lines = _nonempty_lines(result.stdout.text)
        reference = "" if not output_lines else output_lines[-1]
        if not _successful_complete(result) or (
            _CONTAINER_ID.fullmatch(reference) is None and reference != container_name
        ):
            cleanup = self._cleanup_identity(
                compose_project=compose_project,
                container_name=container_name,
                version=release,
            )
            message = _command_failure("new production application startup", result)
            if not cleanup.proof.proven:
                message += "; new-release cleanup could not be proven"
            raise ProductionStartError(message, command=result, cleanup=cleanup)

        try:
            inspect_result, raw = self._inspect_raw(
                container_name,
                environment=environment,
                cancellation_event=cancellation_event,
            )
            handle = ProductionApplicationHandle(
                source="release",
                container_id=_required_text(raw, "Id"),
                container_name=container_name,
                compose_project=compose_project,
                compose_service=self.application.service,
                version=release,
                release_id=release_id,
                operation_id=operation,
                database_directory=self.production_database_path.parent,
                database_target=self.application.database_volume_target,
                host_port=self.topology.host_port,
                container_port=self.application.candidate_container_port,
                health_url=self.topology.health_url,
            )
            inspection = self._validated_inspection(
                handle,
                raw,
                inspect_result,
                expected_running=True,
            )
        except (ProductionInspectionError, KeyError, TypeError, ValueError) as exc:
            cleanup = self._cleanup_identity(
                compose_project=compose_project,
                container_name=container_name,
                version=release,
            )
            raise ProductionStartError(
                "new production application identity or inspection was unsafe: "
                + self.secrets.redact_text(str(exc)),
                command=result,
                cleanup=cleanup,
            ) from None
        return ProductionStart(
            handle=handle,
            container_reference=reference,
            command=result,
            inspection=inspection,
        )

    def collect_logs(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionLogs:
        """Collect bounded logs from exactly the stored production container."""
        self._validate_handle(handle)
        result = self._run(
            ("docker", "container", "logs", handle.container_id),
            environment=self._environment_for_handle(handle),
            cancellation_event=cancellation_event,
        )
        return ProductionLogs(handle=handle, command=result)

    def remove_new(self, handle: ProductionApplicationHandle) -> ProductionCleanup:
        """Idempotently remove only an operation-created new release and its network."""
        self._validate_release_handle(handle)
        assert handle.version is not None
        cleanup = self._cleanup_identity(
            compose_project=handle.compose_project,
            container_name=handle.container_name,
            version=handle.version,
        )
        commands_succeeded = _successful_complete(cleanup.presence_query) and _successful_complete(
            cleanup.compose_down
        )
        if cleanup.remove_command is not None:
            commands_succeeded = commands_succeeded and _successful_complete(cleanup.remove_command)
        if not commands_succeeded or not cleanup.proof.proven:
            raise ProductionCleanupError(
                "new production release cleanup could not be proven",
                command=cleanup.compose_down,
                cleanup=cleanup,
            )
        return cleanup

    def restart_previous(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionRestart:
        """Restart the exact preserved previous container and prove it is running."""
        self._validate_handle(handle)
        result = self._run(
            ("docker", "container", "start", handle.container_id),
            environment=self._environment_for_handle(handle),
            cancellation_event=cancellation_event,
        )
        if not _successful_complete(result):
            raise ProductionRestartError(
                _command_failure("previous application restart", result),
                command=result,
            )
        try:
            inspection = self.inspect(
                handle,
                expected_running=True,
                cancellation_event=cancellation_event,
            )
        except ProductionInspectionError as exc:
            raise ProductionRestartError(str(exc), command=exc.command) from None
        return ProductionRestart(handle=handle, command=result, inspection=inspection)

    def prove_new_absent(
        self,
        *,
        compose_project: str,
        container_name: str,
        version: str,
    ) -> ProductionCleanupProof:
        """Prove exact new-release container and project-network absence."""
        environment = self._production_environment(version)
        container_query = self._container_query(container_name, environment=environment)
        network_query = self._run(
            (
                "docker",
                "network",
                "ls",
                "--filter",
                f"label={COMPOSE_PROJECT_LABEL}={compose_project}",
                "--format",
                "{{.ID}}",
            ),
            environment=environment,
        )
        return ProductionCleanupProof(
            container_absent=_successful_complete(container_query)
            and not _nonempty_lines(container_query.stdout.text),
            networks_absent=_successful_complete(network_query)
            and not _nonempty_lines(network_query.stdout.text),
            container_query=container_query,
            network_query=network_query,
        )

    def _validated_inspection(
        self,
        handle: ProductionApplicationHandle,
        raw: dict[str, Any],
        command: CommandResult,
        *,
        expected_running: bool,
    ) -> ProductionInspection:
        failures: list[str] = []
        container_id = _required_text(raw, "Id")
        container_name = _required_text(raw, "Name").removeprefix("/")
        state = _required_mapping(raw, "State")
        running = state.get("Running") is True
        config = _required_mapping(raw, "Config")
        labels = config.get("Labels")
        if not isinstance(labels, dict):
            labels = {}
        environment = config.get("Env")
        if not isinstance(environment, list) or not all(
            isinstance(item, str) for item in environment
        ):
            raise ValueError("inspection Config.Env must be an array of strings")
        mounts = _mounts(raw.get("Mounts"))
        ports = _published_ports(raw, running=running)

        if container_id != handle.container_id:
            failures.append("container ID does not match the durable application handle")
        if container_name != handle.container_name:
            failures.append("container name does not match the durable application handle")
        if running is not expected_running:
            failures.append(
                "container running state does not match the required "
                + ("running" if expected_running else "stopped")
                + " state"
            )
        if labels.get(COMPOSE_PROJECT_LABEL) != handle.compose_project:
            failures.append("Compose project label contradicts the application handle")
        if labels.get(COMPOSE_SERVICE_LABEL) != handle.compose_service:
            failures.append("Compose service label contradicts the application handle")
        if handle.source == "release":
            if labels.get(ROLE_LABEL) != ROLE_PRODUCTION_RELEASE:
                failures.append("production-release role label is missing or contradictory")
            if labels.get(RELEASE_LABEL) != handle.release_id:
                failures.append("release label contradicts the application handle")
            if labels.get(OPERATION_LABEL) != handle.operation_id:
                failures.append("operation label contradicts the application handle")

        expected_source = self.production_database_path.parent.resolve()
        database_mounts = [mount for mount in mounts if mount.destination == handle.database_target]
        if len(database_mounts) != 1:
            failures.append("production database target does not have exactly one mount")
        else:
            mount = database_mounts[0]
            source = Path(mount.source).resolve() if mount.source.startswith("/") else None
            if mount.mount_type != "bind" or source != expected_source:
                failures.append(
                    "production database target is not bound to its configured directory"
                )
            if not mount.read_write:
                failures.append("production database mount is not writable")

        expected_database = str(
            PurePosixPath(handle.database_target) / self.production_database_path.name
        )
        expected_assignment = f"{self.database_environment_name}={expected_database}"
        if expected_assignment not in environment:
            failures.append("container database environment does not select production SQLite")

        published = [binding for binding in ports if binding[3]]
        matching = [binding for binding in published if binding[0] == handle.container_port]
        if len(matching) != 1:
            failures.append("production container port lacks exactly one published binding")
        else:
            _container_port, host_ip, host_port, _published = matching[0]
            if host_ip != "127.0.0.1" or host_port != handle.host_port:
                failures.append("production port is not bound to the configured loopback endpoint")
        if len(published) != 1:
            failures.append("production application publishes unexpected additional ports")

        if failures:
            raise ProductionInspectionError(
                "production application inspection failed: " + "; ".join(failures),
                command=command,
            )
        return ProductionInspection(
            handle=handle,
            running=running,
            mounts=mounts,
            command=command,
        )

    def _inspect_raw(
        self,
        reference: str,
        *,
        environment: Mapping[str, str],
        cancellation_event: threading.Event | None,
    ) -> tuple[CommandResult, dict[str, Any]]:
        result = self._run(
            ("docker", "container", "inspect", reference),
            environment=environment,
            cancellation_event=cancellation_event,
        )
        if not _successful_complete(result):
            raise ProductionInspectionError(
                _command_failure("production application inspection", result),
                command=result,
            )
        try:
            payload = json.loads(result.stdout.text)
            raw = _single_inspection(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProductionInspectionError(
                "production application inspection returned invalid Docker JSON: "
                + self.secrets.redact_text(str(exc)),
                command=result,
            ) from None
        return result, raw

    def _cleanup_identity(
        self,
        *,
        compose_project: str,
        container_name: str,
        version: str,
    ) -> ProductionCleanup:
        environment = self._production_environment(version)
        presence = self._container_query(container_name, environment=environment)
        remove: CommandResult | None = None
        if _successful_complete(presence) and container_name in _nonempty_lines(
            presence.stdout.text
        ):
            remove = self._run(
                ("docker", "container", "rm", "--force", container_name),
                environment=environment,
            )
        down = self._run(
            self._compose_command(
                compose_project,
                "down",
                "--remove-orphans",
                "--timeout",
                str(self.application.startup_timeout_seconds),
            ),
            environment=environment,
        )
        proof = self.prove_new_absent(
            compose_project=compose_project,
            container_name=container_name,
            version=version,
        )
        return ProductionCleanup(
            presence_query=presence,
            remove_command=remove,
            compose_down=down,
            proof=proof,
        )

    def _container_query(
        self,
        container_name: str,
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
                f"name=^/{container_name}$",
                "--format",
                "{{.Names}}",
            ),
            environment=environment,
        )

    def _validate_handle(self, handle: ProductionApplicationHandle) -> None:
        if not isinstance(handle, ProductionApplicationHandle):
            raise TypeError("handle must be a ProductionApplicationHandle")
        if handle.database_directory.resolve() != self.production_database_path.parent:
            raise ValueError("application handle database directory is not production")
        if handle.database_target != self.application.database_volume_target:
            raise ValueError("application handle database target contradicts configuration")
        if (
            handle.host_port != self.topology.host_port
            or handle.container_port != self.application.candidate_container_port
            or handle.health_url != self.topology.health_url
        ):
            raise ValueError("application handle network topology contradicts configuration")
        if handle.compose_service != self.application.service:
            raise ValueError("application handle service contradicts configuration")
        if handle.source == "bootstrap" and handle.compose_project != self.topology.compose_project:
            raise ValueError("bootstrap handle project contradicts configured production project")
        if handle.source == "release":
            self._validate_release_handle(handle)

    def _validate_release_handle(self, handle: ProductionApplicationHandle) -> None:
        if handle.source != "release":
            raise ValueError("cleanup requires an operation-created release handle")
        assert handle.release_id is not None
        expected_project, expected_name = self._new_identity(handle.release_id)
        if handle.compose_project != expected_project or handle.container_name != expected_name:
            raise ValueError("release handle does not match its derived resource identity")

    def _new_identity(self, release_id: str) -> tuple[str, str]:
        release_suffix = release_id.removeprefix("release_")[:16]
        safe_project = re.sub(r"[^a-z0-9_-]", "-", self.project.lower())
        safe_project = safe_project.strip("-_") or "app"
        compose_project = f"dploydb-{safe_project[:20]}-release-{release_suffix}"
        return compose_project, f"{compose_project}-app"

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

    def _base_environment(self) -> dict[str, str]:
        environment = dict(self.command_environment)
        for name, value in environment.items():
            if is_sensitive_key(name):
                self.secrets.register(value)
        return environment

    def _production_environment(self, version: str) -> dict[str, str]:
        environment = self._base_environment()
        environment[DPLOYDB_VERSION_ENV] = version
        environment.pop(CANDIDATE_URL_ENV, None)
        for name in self.application.test_mode_env:
            environment.pop(name, None)
        return environment

    def _environment_for_handle(self, handle: ProductionApplicationHandle) -> dict[str, str]:
        return (
            self._base_environment()
            if handle.version is None
            else self._production_environment(handle.version)
        )

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


def _published_ports(
    value: Mapping[str, Any],
    *,
    running: bool,
) -> list[tuple[int, str, int, bool]]:
    parent = _required_mapping(value, "NetworkSettings" if running else "HostConfig")
    ports = parent.get("Ports" if running else "PortBindings")
    if not isinstance(ports, dict):
        location = "NetworkSettings.Ports" if running else "HostConfig.PortBindings"
        raise ValueError(f"inspection {location} must be an object")
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
    return f"{action} returned invalid success evidence"
