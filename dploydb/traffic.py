"""Bounded command-based maintenance and traffic hook execution."""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from dploydb.config import TrafficConfig
from dploydb.redaction import JsonValue, SecretRegistry
from dploydb.runners.base import CommandExecutor
from dploydb.subprocesses import CommandOutcome, CommandResult, SubprocessRunner

TRAFFIC_MAX_OUTPUT_BYTES: Final = 256 * 1024


class TrafficAction(StrEnum):
    """The four developer-supplied cutover hook actions."""

    ENABLE_MAINTENANCE = "enable_maintenance"
    DISABLE_MAINTENANCE = "disable_maintenance"
    ACTIVATE_NEW = "activate_new"
    ACTIVATE_OLD = "activate_old"


@dataclass(frozen=True, slots=True)
class TrafficHookResult:
    """Complete bounded evidence for one attempted traffic action."""

    action: TrafficAction
    command: CommandResult

    @property
    def passed(self) -> bool:
        """Require exit zero and complete stdout/stderr evidence."""
        return (
            self.command.outcome is CommandOutcome.SUCCEEDED
            and not self.command.stdout.truncated
            and not self.command.stderr.truncated
        )

    def as_evidence(self) -> dict[str, JsonValue]:
        return {
            "action": self.action.value,
            "passed": self.passed,
            "command": self.command.as_evidence(),
        }


class TrafficController(Protocol):
    """Narrow hook boundary consumed by the deployment coordinator."""

    def enable_maintenance(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult: ...

    def disable_maintenance(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult: ...

    def activate_new(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult: ...

    def activate_old(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult: ...


class CommandTrafficController:
    """Execute configured traffic hooks without a shell and preserve every outcome."""

    def __init__(
        self,
        *,
        traffic: TrafficConfig,
        secrets: SecretRegistry,
        working_directory: Path,
        command_environment: Mapping[str, str] | None = None,
        command_runner: CommandExecutor | None = None,
    ) -> None:
        if not working_directory.is_absolute():
            raise ValueError("traffic hook working_directory must be absolute")
        self.traffic = traffic
        self.secrets = secrets
        self.working_directory = working_directory
        self.command_environment = dict(
            os.environ if command_environment is None else command_environment
        )
        self.command_runner = command_runner or SubprocessRunner(
            secrets=secrets,
            max_output_bytes=TRAFFIC_MAX_OUTPUT_BYTES,
        )

    def enable_maintenance(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult:
        return self._run(
            TrafficAction.ENABLE_MAINTENANCE,
            self.traffic.maintenance_on_command,
            cancellation_event=cancellation_event,
        )

    def disable_maintenance(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult:
        return self._run(
            TrafficAction.DISABLE_MAINTENANCE,
            self.traffic.maintenance_off_command,
            cancellation_event=cancellation_event,
        )

    def activate_new(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult:
        return self._run(
            TrafficAction.ACTIVATE_NEW,
            self.traffic.activate_new_command,
            cancellation_event=cancellation_event,
        )

    def activate_old(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> TrafficHookResult:
        return self._run(
            TrafficAction.ACTIVATE_OLD,
            self.traffic.activate_old_command,
            cancellation_event=cancellation_event,
        )

    def _run(
        self,
        action: TrafficAction,
        command: tuple[str, ...],
        *,
        cancellation_event: threading.Event | None,
    ) -> TrafficHookResult:
        result = self.command_runner.run(
            command,
            timeout_seconds=self.traffic.timeout_seconds,
            environment=self.command_environment,
            working_directory=self.working_directory,
            cancellation_event=cancellation_event,
        )
        return TrafficHookResult(action=action, command=result)
