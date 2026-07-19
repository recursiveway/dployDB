"""Typed candidate-application lifecycle contracts."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dploydb.models import ProductionApplicationHandle
from dploydb.subprocesses import CommandResult

_OPERATION_ID = re.compile(r"op_[0-9a-f]{32}\Z")
_RELEASE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def validate_operation_id(value: str) -> str:
    """Validate an opaque operation identifier before deriving resource names."""
    if not isinstance(value, str):
        raise TypeError("operation_id must be a string")
    if _OPERATION_ID.fullmatch(value) is None:
        raise ValueError("operation_id must be an opaque DployDB operation ID")
    return value


def validate_release_identifier(value: str) -> str:
    """Validate the bounded value made available to Compose interpolation."""
    if not isinstance(value, str):
        raise TypeError("version must be a string")
    if _RELEASE_IDENTIFIER.fullmatch(value) is None or ".." in value:
        raise ValueError(
            "version must be 1-64 letters, digits, dots, underscores, or hyphens, "
            "start with a letter or digit, and contain no traversal sequence"
        )
    return value


class CommandExecutor(Protocol):
    """Subset of the bounded subprocess runner used by application runners."""

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str],
        working_directory: Path | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> CommandResult: ...


@dataclass(frozen=True, slots=True)
class CandidateHandle:
    """Opaque identity and expected isolation boundary for one candidate."""

    operation_id: str
    version: str
    compose_project: str
    container_name: str
    rehearsal_database_path: Path
    candidate_database_path: str


@dataclass(frozen=True, slots=True)
class CandidateMount:
    """One selected mount from live Docker inspection evidence."""

    mount_type: str
    source: str
    destination: str
    read_write: bool


@dataclass(frozen=True, slots=True)
class CandidateInspection:
    """Validated live evidence for an isolated running candidate."""

    container_id: str
    container_name: str
    running: bool
    compose_project: str
    compose_service: str
    operation_id: str
    host_ip: str
    host_port: int
    container_port: int
    mounts: tuple[CandidateMount, ...]
    command: CommandResult


@dataclass(frozen=True, slots=True)
class CandidateStart:
    """Evidence returned after Compose creates the isolated candidate."""

    handle: CandidateHandle
    container_reference: str
    command: CommandResult


@dataclass(frozen=True, slots=True)
class CandidateLogs:
    """Bounded redacted application-log capture."""

    handle: CandidateHandle
    command: CommandResult


@dataclass(frozen=True, slots=True)
class CandidateCleanupProof:
    """Read-only proof that no candidate container or project network remains."""

    container_absent: bool
    networks_absent: bool
    container_query: CommandResult
    network_query: CommandResult

    @property
    def proven(self) -> bool:
        return self.container_absent and self.networks_absent


@dataclass(frozen=True, slots=True)
class CandidateCleanup:
    """Commands and terminal proof from idempotent isolated cleanup."""

    presence_query: CommandResult
    remove_command: CommandResult | None
    compose_down: CommandResult
    proof: CandidateCleanupProof


class CandidateRunnerError(RuntimeError):
    """Typed low-level failure containing only bounded redacted evidence."""

    def __init__(
        self,
        message: str,
        *,
        command: CommandResult | None = None,
        cleanup: CandidateCleanup | None = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.cleanup = cleanup

    @property
    def cleanup_proven(self) -> bool | None:
        return None if self.cleanup is None else self.cleanup.proof.proven


class CandidateStartError(CandidateRunnerError):
    """Candidate creation failed; cleanup evidence states whether retry is safe."""


class CandidateInspectionError(CandidateRunnerError):
    """Live Docker state contradicted the required candidate isolation boundary."""


class CandidateCleanupError(CandidateRunnerError):
    """Candidate cleanup could not be proven."""


class ApplicationRunner(Protocol):
    """Small Milestone 4A candidate lifecycle; production controls come later."""

    def start(
        self,
        *,
        operation_id: str,
        version: str,
        rehearsal_database_path: Path,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateStart: ...

    def inspect(
        self,
        handle: CandidateHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateInspection: ...

    def collect_logs(
        self,
        handle: CandidateHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateLogs: ...

    def stop(self, handle: CandidateHandle) -> CandidateCleanup: ...

    def prove_cleanup(self, handle: CandidateHandle) -> CandidateCleanupProof: ...


@dataclass(frozen=True, slots=True)
class ProductionInspection:
    """Validated live Docker evidence for one exact production application."""

    handle: ProductionApplicationHandle
    running: bool
    mounts: tuple[CandidateMount, ...]
    command: CommandResult


@dataclass(frozen=True, slots=True)
class ProductionDiscovery:
    """Configured Compose lookup plus validated current-application inspection."""

    query: CommandResult
    inspection: ProductionInspection


@dataclass(frozen=True, slots=True)
class ProductionStop:
    """Exact-container stop command and proof that the previous app is stopped."""

    handle: ProductionApplicationHandle
    command: CommandResult
    inspection: ProductionInspection


@dataclass(frozen=True, slots=True)
class ProductionStart:
    """Evidence returned after a new release container is created."""

    handle: ProductionApplicationHandle
    container_reference: str
    command: CommandResult
    inspection: ProductionInspection


@dataclass(frozen=True, slots=True)
class ProductionNetworkRefresh:
    """Exact stopped-container network endpoint refresh evidence."""

    network_name: str
    aliases: tuple[str, ...]
    disconnect: CommandResult
    connect: CommandResult


@dataclass(frozen=True, slots=True)
class ProductionRestart:
    """Exact previous-container restart plus running-state proof."""

    handle: ProductionApplicationHandle
    command: CommandResult
    inspection: ProductionInspection
    network_refreshes: tuple[ProductionNetworkRefresh, ...] = ()


@dataclass(frozen=True, slots=True)
class ProductionLogs:
    """Bounded logs collected from one exact production container."""

    handle: ProductionApplicationHandle
    command: CommandResult


@dataclass(frozen=True, slots=True)
class ProductionCleanupProof:
    """Proof that a failed new release container and network are absent."""

    container_absent: bool
    networks_absent: bool
    container_query: CommandResult
    network_query: CommandResult

    @property
    def proven(self) -> bool:
        return self.container_absent and self.networks_absent


@dataclass(frozen=True, slots=True)
class ProductionCleanup:
    """Idempotent exact-target cleanup evidence for a failed new release."""

    presence_query: CommandResult
    remove_command: CommandResult | None
    compose_down: CommandResult
    proof: ProductionCleanupProof


class ProductionRunnerError(RuntimeError):
    """Base production lifecycle failure with bounded redacted command evidence."""

    def __init__(
        self,
        message: str,
        *,
        command: CommandResult | None = None,
        cleanup: ProductionCleanup | None = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.cleanup = cleanup

    @property
    def cleanup_proven(self) -> bool | None:
        return None if self.cleanup is None else self.cleanup.proof.proven


class ProductionDiscoveryError(ProductionRunnerError):
    """Configured current application could not be uniquely and safely identified."""


class ProductionInspectionError(ProductionRunnerError):
    """Live state contradicted the recorded production application identity."""


class ProductionStopError(ProductionRunnerError):
    """The current application could not be proven stopped."""


class ProductionStartError(ProductionRunnerError):
    """The new release could not be started with proven cleanup evidence."""


class ProductionRestartError(ProductionRunnerError):
    """The exact previous application could not be proven restarted."""


class ProductionCleanupError(ProductionRunnerError):
    """Failed new-release resources could not be proven absent."""


class ProductionApplicationRunner(Protocol):
    """Milestone 5 production application lifecycle boundary."""

    def discover_current(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionDiscovery: ...

    def inspect(
        self,
        handle: ProductionApplicationHandle,
        *,
        expected_running: bool,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionInspection: ...

    def inspect_live(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionInspection: ...

    def stop_current(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionStop: ...

    def start_new(
        self,
        *,
        operation_id: str,
        release_id: str,
        version: str,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionStart: ...

    def collect_logs(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionLogs: ...

    def remove_new(self, handle: ProductionApplicationHandle) -> ProductionCleanup: ...

    def prove_release_absent(
        self,
        *,
        release_id: str,
        version: str,
    ) -> ProductionCleanupProof: ...

    def restart_previous(
        self,
        handle: ProductionApplicationHandle,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ProductionRestart: ...
