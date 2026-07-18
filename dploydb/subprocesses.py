"""Bounded, redacted execution of untrusted external commands.

The runner deliberately returns structured outcomes rather than user-facing
``DployDBError`` instances. Only an orchestration layer knows whether a command
ran before or after production changed, so it must attach those safety facts when
it converts an unsuccessful result into a CLI failure.
"""

from __future__ import annotations

import errno
import math
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Final, cast

from dploydb.redaction import REDACTION_MARKER, JsonValue, SecretRegistry, is_sensitive_key

DEFAULT_MAX_OUTPUT_BYTES: Final = 1024 * 1024
DEFAULT_TERMINATION_GRACE_SECONDS: Final = 2.0
DEFAULT_POLL_INTERVAL_SECONDS: Final = 0.05
_READ_CHUNK_BYTES: Final = 64 * 1024
_TRUNCATION_MARKER: Final = b"\n...[output truncated]...\n"


class CommandOutcome(StrEnum):
    """Terminal outcome of one bounded command invocation."""

    SUCCEEDED = "succeeded"
    NONZERO_EXIT = "nonzero_exit"
    START_FAILED = "start_failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    CLEANUP_FAILED = "cleanup_failed"


class TerminationReason(StrEnum):
    """Reason DployDB attempted to terminate a command process group."""

    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    INTERRUPTION = "interruption"


@dataclass(frozen=True, slots=True)
class CapturedOutput:
    """One redacted, memory-bounded output stream."""

    text: str
    total_bytes: int
    retained_bytes: int
    truncated: bool

    def __post_init__(self) -> None:
        if self.total_bytes < 0 or self.retained_bytes < 0:
            raise ValueError("captured output byte counts must not be negative")
        if self.retained_bytes > self.total_bytes:
            raise ValueError("retained output bytes must not exceed total bytes")
        if self.truncated != (self.retained_bytes < self.total_bytes):
            raise ValueError("captured output truncation metadata is contradictory")

    def as_evidence(self) -> dict[str, JsonValue]:
        """Return the stable JSON-compatible evidence representation."""
        return {
            "text": self.text,
            "total_bytes": self.total_bytes,
            "retained_bytes": self.retained_bytes,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Redacted terminal evidence for one attempted external command."""

    command: tuple[str, ...]
    working_directory: str
    environment_keys: tuple[str, ...]
    outcome: CommandOutcome
    exit_code: int | None
    stdout: CapturedOutput
    stderr: CapturedOutput
    duration_seconds: float
    termination_reason: TerminationReason | None = None
    termination_attempted: bool = False
    forced_kill: bool = False
    start_error: str | None = None
    cleanup_error: str | None = None

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("command result requires at least one argument")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("command duration must be finite and non-negative")
        if self.outcome is CommandOutcome.SUCCEEDED and self.exit_code != 0:
            raise ValueError("successful command requires exit code zero")
        if self.outcome is CommandOutcome.NONZERO_EXIT and self.exit_code in {None, 0}:
            raise ValueError("non-zero command outcome requires a non-zero exit code")
        if self.outcome is CommandOutcome.START_FAILED:
            if self.exit_code is not None or self.start_error is None:
                raise ValueError("start failure requires start_error and no exit code")
        elif self.start_error is not None:
            raise ValueError("start_error is valid only for a start failure")
        if self.outcome is CommandOutcome.CLEANUP_FAILED:
            if self.cleanup_error is None:
                raise ValueError("cleanup failure requires cleanup_error")
        elif self.cleanup_error is not None:
            raise ValueError("cleanup_error is valid only for a cleanup failure")
        if self.outcome in {CommandOutcome.TIMED_OUT, CommandOutcome.CANCELLED}:
            if self.termination_reason is None:
                raise ValueError("terminated command requires a reason")
        if self.outcome is CommandOutcome.TIMED_OUT and not self.termination_attempted:
            raise ValueError("timed-out command requires attempted cleanup")
        if (
            self.outcome is CommandOutcome.CANCELLED
            and self.exit_code is not None
            and not self.termination_attempted
        ):
            raise ValueError("started cancelled command requires attempted cleanup")
        if self.outcome is CommandOutcome.CLEANUP_FAILED and self.termination_reason is None:
            raise ValueError("cleanup failure requires its initiating termination reason")
        if self.forced_kill and not self.termination_attempted:
            raise ValueError("forced kill requires an attempted termination")

    @property
    def succeeded(self) -> bool:
        """Return whether the command completed with exit code zero."""
        return self.outcome is CommandOutcome.SUCCEEDED

    def as_evidence(self) -> dict[str, JsonValue]:
        """Return redacted JSON-compatible evidence for persistence or display."""
        return {
            "command": list(self.command),
            "working_directory": self.working_directory,
            "environment_keys": list(self.environment_keys),
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout.as_evidence(),
            "stderr": self.stderr.as_evidence(),
            "duration_seconds": self.duration_seconds,
            "termination_reason": (
                None if self.termination_reason is None else self.termination_reason.value
            ),
            "termination_attempted": self.termination_attempted,
            "forced_kill": self.forced_kill,
            "start_error": self.start_error,
            "cleanup_error": self.cleanup_error,
        }


class _BoundedCapture:
    """Drain a byte stream while retaining only a bounded head and tail."""

    def __init__(self, limit: int) -> None:
        self._head_limit = limit // 2
        self._tail_limit = limit - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self._total_bytes = 0
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            self._total_bytes += len(data)
            head_missing = self._head_limit - len(self._head)
            if head_missing > 0:
                self._head.extend(data[:head_missing])
                data = data[head_missing:]
            if not data or self._tail_limit == 0:
                return
            if len(data) >= self._tail_limit:
                self._tail[:] = data[-self._tail_limit :]
                return
            self._tail.extend(data)
            overflow = len(self._tail) - self._tail_limit
            if overflow > 0:
                del self._tail[:overflow]

    def snapshot(self, secrets: SecretRegistry) -> CapturedOutput:
        with self._lock:
            head = bytes(self._head)
            tail = bytes(self._tail)
            total_bytes = self._total_bytes
        retained_bytes = len(head) + len(tail)
        truncated = retained_bytes < total_bytes
        raw = head + (_TRUNCATION_MARKER if truncated else b"") + tail
        text = raw.decode("utf-8", errors="replace")
        return CapturedOutput(
            text=secrets.redact_text(text),
            total_bytes=total_bytes,
            retained_bytes=retained_bytes,
            truncated=truncated,
        )


@dataclass(slots=True)
class _Reader:
    pipe: BinaryIO
    capture: _BoundedCapture
    thread: threading.Thread | None = None
    error: BaseException | None = None

    def start(self, name: str) -> None:
        self.thread = threading.Thread(target=self._drain, name=name, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.pipe.read(_READ_CHUNK_BYTES)
                if not chunk:
                    return
                self.capture.append(chunk)
        except BaseException as exc:  # preserved for the controlling thread
            self.error = exc
        finally:
            try:
                self.pipe.close()
            except OSError:
                pass

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def join(self, timeout: float) -> None:
        if self.thread is not None:
            self.thread.join(timeout=max(0.0, timeout))


@dataclass(frozen=True, slots=True)
class _CleanupResult:
    forced_kill: bool
    error: str | None


class SubprocessRunner:
    """Execute commands with mandatory bounds, redaction, and group cleanup."""

    def __init__(
        self,
        *,
        secrets: SecretRegistry,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_output_bytes = _positive_integer(max_output_bytes, "max_output_bytes")
        self.termination_grace_seconds = _positive_seconds(
            termination_grace_seconds, "termination_grace_seconds"
        )
        self.poll_interval_seconds = _positive_seconds(
            poll_interval_seconds, "poll_interval_seconds"
        )
        self.secrets = secrets
        self._clock = clock

    def __repr__(self) -> str:
        return (
            "SubprocessRunner("
            f"max_output_bytes={self.max_output_bytes}, "
            f"termination_grace_seconds={self.termination_grace_seconds}, "
            f"poll_interval_seconds={self.poll_interval_seconds}, "
            f"secrets={self.secrets!r})"
        )

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str],
        working_directory: Path | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> CommandResult:
        """Run one command and return redacted evidence for every expected outcome."""
        arguments = _validate_command(command)
        timeout = _positive_seconds(timeout_seconds, "timeout_seconds")
        child_environment = _validate_environment(environment)
        directory = _working_directory(working_directory)

        for name, value in child_environment.items():
            if is_sensitive_key(name):
                self.secrets.register(value)

        safe_command = _redacted_command(arguments, self.secrets)
        safe_directory = self.secrets.redact_text(str(directory))
        safe_environment_keys = tuple(
            sorted(self.secrets.redact_text(name) for name in child_environment)
        )
        empty = CapturedOutput(text="", total_bytes=0, retained_bytes=0, truncated=False)
        started_at = self._clock()

        if cancellation_event is not None and cancellation_event.is_set():
            return CommandResult(
                command=safe_command,
                working_directory=safe_directory,
                environment_keys=safe_environment_keys,
                outcome=CommandOutcome.CANCELLED,
                exit_code=None,
                stdout=empty,
                stderr=empty,
                duration_seconds=self._duration(started_at),
                termination_reason=TerminationReason.CANCELLATION,
            )

        try:
            process = subprocess.Popen(
                arguments,
                cwd=directory,
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            return CommandResult(
                command=safe_command,
                working_directory=safe_directory,
                environment_keys=safe_environment_keys,
                outcome=CommandOutcome.START_FAILED,
                exit_code=None,
                stdout=empty,
                stderr=empty,
                duration_seconds=self._duration(started_at),
                start_error=self._safe_exception(exc),
            )

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_reader = _Reader(
            cast(BinaryIO, process.stdout), _BoundedCapture(self.max_output_bytes)
        )
        stderr_reader = _Reader(
            cast(BinaryIO, process.stderr), _BoundedCapture(self.max_output_bytes)
        )
        readers = (stdout_reader, stderr_reader)
        stdout_reader.start(f"dploydb-stdout-{process.pid}")
        stderr_reader.start(f"dploydb-stderr-{process.pid}")
        deadline = started_at + timeout

        try:
            reason = self._wait_for_completion(
                process,
                readers,
                deadline=deadline,
                cancellation_event=cancellation_event,
            )
            if reason is None:
                reader_error = self._reader_error(readers)
                if reader_error is not None:
                    cleanup = self._terminate_group(process, readers)
                    errors = [reader_error]
                    if cleanup.error is not None:
                        errors.append(cleanup.error)
                    return self._result(
                        safe_command=safe_command,
                        safe_directory=safe_directory,
                        safe_environment_keys=safe_environment_keys,
                        process=process,
                        readers=readers,
                        started_at=started_at,
                        outcome=CommandOutcome.CLEANUP_FAILED,
                        termination_reason=TerminationReason.INTERRUPTION,
                        termination_attempted=True,
                        forced_kill=cleanup.forced_kill,
                        cleanup_error="; ".join(errors),
                    )
                outcome = (
                    CommandOutcome.SUCCEEDED
                    if process.returncode == 0
                    else CommandOutcome.NONZERO_EXIT
                )
                return self._result(
                    safe_command=safe_command,
                    safe_directory=safe_directory,
                    safe_environment_keys=safe_environment_keys,
                    process=process,
                    readers=readers,
                    started_at=started_at,
                    outcome=outcome,
                )

            cleanup = self._terminate_group(process, readers)
            outcome = (
                CommandOutcome.CLEANUP_FAILED
                if cleanup.error is not None
                else (
                    CommandOutcome.TIMED_OUT
                    if reason is TerminationReason.TIMEOUT
                    else CommandOutcome.CANCELLED
                )
            )
            return self._result(
                safe_command=safe_command,
                safe_directory=safe_directory,
                safe_environment_keys=safe_environment_keys,
                process=process,
                readers=readers,
                started_at=started_at,
                outcome=outcome,
                termination_reason=reason,
                termination_attempted=True,
                forced_kill=cleanup.forced_kill,
                cleanup_error=cleanup.error,
            )
        except BaseException as exc:
            cleanup = self._terminate_group(process, readers)
            if cleanup.error is not None:
                exc.add_note(
                    "DployDB subprocess cleanup could not be proven: "
                    + self.secrets.redact_text(cleanup.error)
                )
            raise

    def _wait_for_completion(
        self,
        process: subprocess.Popen[bytes],
        readers: tuple[_Reader, _Reader],
        *,
        deadline: float,
        cancellation_event: threading.Event | None,
    ) -> TerminationReason | None:
        while True:
            process.poll()
            if process.returncode is not None and not any(reader.alive for reader in readers):
                return None
            if cancellation_event is not None and cancellation_event.is_set():
                return TerminationReason.CANCELLATION
            remaining = deadline - self._clock()
            if remaining <= 0:
                return TerminationReason.TIMEOUT
            time.sleep(min(self.poll_interval_seconds, remaining))

    def _terminate_group(
        self,
        process: subprocess.Popen[bytes],
        readers: tuple[_Reader, _Reader],
    ) -> _CleanupResult:
        errors: list[str] = []
        forced_kill = False
        group_id = process.pid

        term_error = self._signal_group(group_id, signal.SIGTERM)
        if term_error is not None:
            errors.append(term_error)
        if not self._wait_for_group_cleanup(process, readers, self.termination_grace_seconds):
            forced_kill = True
            kill_error = self._signal_group(group_id, signal.SIGKILL)
            if kill_error is not None:
                errors.append(kill_error)
            if not self._wait_for_group_cleanup(process, readers, self.termination_grace_seconds):
                errors.append("process group remained after forced termination")

        reader_error = self._reader_error(readers)
        if reader_error is not None:
            errors.append(reader_error)
        return _CleanupResult(
            forced_kill=forced_kill,
            error=None if not errors else self.secrets.redact_text("; ".join(errors)),
        )

    def _wait_for_group_cleanup(
        self,
        process: subprocess.Popen[bytes],
        readers: tuple[_Reader, _Reader],
        timeout: float,
    ) -> bool:
        deadline = self._clock() + timeout
        while True:
            process.poll()
            group_exists = _process_group_exists(process.pid)
            readers_alive = any(reader.alive for reader in readers)
            if not group_exists and process.returncode is not None and not readers_alive:
                return True
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            for reader in readers:
                reader.join(min(self.poll_interval_seconds, remaining))
            if process.returncode is None:
                try:
                    process.wait(timeout=min(self.poll_interval_seconds, remaining))
                except subprocess.TimeoutExpired:
                    pass

        process.poll()
        return (
            not _process_group_exists(process.pid)
            and process.returncode is not None
            and not any(reader.alive for reader in readers)
        )

    @staticmethod
    def _signal_group(group_id: int, sig: signal.Signals) -> str | None:
        try:
            os.killpg(group_id, sig)
        except ProcessLookupError:
            return None
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return None
            return f"{sig.name} could not be sent to process group: {exc}"
        return None

    def _reader_error(self, readers: tuple[_Reader, _Reader]) -> str | None:
        details = [self._safe_exception(reader.error) for reader in readers if reader.error]
        return None if not details else "output capture failed: " + "; ".join(details)

    def _result(
        self,
        *,
        safe_command: tuple[str, ...],
        safe_directory: str,
        safe_environment_keys: tuple[str, ...],
        process: subprocess.Popen[bytes],
        readers: tuple[_Reader, _Reader],
        started_at: float,
        outcome: CommandOutcome,
        termination_reason: TerminationReason | None = None,
        termination_attempted: bool = False,
        forced_kill: bool = False,
        cleanup_error: str | None = None,
    ) -> CommandResult:
        return CommandResult(
            command=safe_command,
            working_directory=safe_directory,
            environment_keys=safe_environment_keys,
            outcome=outcome,
            exit_code=process.returncode,
            stdout=readers[0].capture.snapshot(self.secrets),
            stderr=readers[1].capture.snapshot(self.secrets),
            duration_seconds=self._duration(started_at),
            termination_reason=termination_reason,
            termination_attempted=termination_attempted,
            forced_kill=forced_kill,
            cleanup_error=cleanup_error,
        )

    def _duration(self, started_at: float) -> float:
        return max(0.0, self._clock() - started_at)

    def _safe_exception(self, exc: BaseException) -> str:
        return self.secrets.redact_text(f"{type(exc).__name__}: {exc}")


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, str | bytes) or not isinstance(command, Sequence):
        raise TypeError("command must be an argument sequence")
    if not command:
        raise ValueError("command must contain at least one argument")
    arguments: list[str] = []
    for argument in command:
        if not isinstance(argument, str):
            raise TypeError("command arguments must be strings")
        if not argument:
            raise ValueError("command arguments must not be empty")
        if "\x00" in argument:
            raise ValueError("command arguments must not contain NUL bytes")
        arguments.append(argument)
    return tuple(arguments)


def _validate_environment(environment: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(environment, Mapping):
        raise TypeError("environment must be a string mapping")
    result: dict[str, str] = {}
    for name, value in environment.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("environment names and values must be strings")
        if not name:
            raise ValueError("environment names must not be empty")
        if "=" in name or "\x00" in name or "\x00" in value:
            raise ValueError("environment contains an invalid name or value")
        result[name] = value
    return result


def _working_directory(value: Path | None) -> Path:
    if value is None:
        return Path.cwd()
    if not isinstance(value, Path):
        raise TypeError("working_directory must be a pathlib.Path")
    return value if value.is_absolute() else Path.cwd() / value


def _positive_seconds(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return result


def _positive_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _redacted_command(command: tuple[str, ...], secrets: SecretRegistry) -> tuple[str, ...]:
    redacted: list[str] = []
    redact_next = False
    for argument in command:
        if redact_next:
            redacted.append(REDACTION_MARKER)
            redact_next = False
            continue
        redacted.append(secrets.redact_text(argument))
        option = argument.removeprefix("--")
        if argument.startswith("--") and "=" not in option and is_sensitive_key(option):
            redact_next = True
    return tuple(redacted)


def _process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True
