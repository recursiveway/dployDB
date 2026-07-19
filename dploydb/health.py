"""Bounded HTTP readiness and optional candidate smoke checks."""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Self

import httpx

from dploydb.config import ApplicationConfig
from dploydb.redaction import JsonValue, SecretRegistry
from dploydb.runners.base import CommandExecutor, validate_release_identifier
from dploydb.subprocesses import CommandOutcome, CommandResult, SubprocessRunner

CANDIDATE_URL_ENV: Final = "DPLOYDB_CANDIDATE_URL"
APPLICATION_URL_ENV: Final = "DPLOYDB_APPLICATION_URL"
DPLOYDB_VERSION_ENV: Final = "DPLOYDB_VERSION"
DEFAULT_REQUEST_TIMEOUT_SECONDS: Final = 2.0
DEFAULT_RETRY_INTERVAL_SECONDS: Final = 0.1
DEFAULT_MAX_RESPONSE_BYTES: Final = 64 * 1024
SMOKE_MAX_OUTPUT_BYTES: Final = 256 * 1024


class HealthAttemptOutcome(StrEnum):
    """Terminal result of one bounded readiness request."""

    HEALTHY = "healthy"
    UNHEALTHY_HTTP = "unhealthy_http"
    REDIRECT_REFUSED = "redirect_refused"
    TRANSPORT_ERROR = "transport_error"
    DEADLINE_EXPIRED = "deadline_expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class BoundedResponseEvidence:
    """Redacted diagnostic HTTP body retained with a strict memory bound."""

    text: str
    total_bytes: int
    retained_bytes: int
    truncated: bool

    def __post_init__(self) -> None:
        if self.total_bytes < 0 or self.retained_bytes < 0:
            raise ValueError("response byte counts must not be negative")
        if self.retained_bytes > self.total_bytes:
            raise ValueError("retained response bytes must not exceed total bytes")
        if self.truncated != (self.retained_bytes < self.total_bytes):
            raise ValueError("response truncation evidence is contradictory")

    def as_evidence(self) -> dict[str, JsonValue]:
        return {
            "text": self.text,
            "total_bytes": self.total_bytes,
            "retained_bytes": self.retained_bytes,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class HealthAttemptEvidence:
    """Safe evidence for the last readiness request made before a decision."""

    attempt: int
    outcome: HealthAttemptOutcome
    status_code: int | None
    body: BoundedResponseEvidence | None
    reason: str
    duration_seconds: float

    def __post_init__(self) -> None:
        if self.attempt <= 0:
            raise ValueError("health attempt number must be positive")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("health attempt duration must be finite and non-negative")
        if not self.reason:
            raise ValueError("health attempt reason must not be empty")

    def as_evidence(self) -> dict[str, JsonValue]:
        return {
            "attempt": self.attempt,
            "outcome": self.outcome.value,
            "status_code": self.status_code,
            "body": None if self.body is None else self.body.as_evidence(),
            "reason": self.reason,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class ReadinessEvidence:
    """Terminal readiness decision under one fixed monotonic deadline."""

    url: str
    healthy: bool
    attempt_count: int
    last_attempt: HealthAttemptEvidence | None
    duration_seconds: float
    reason: str

    def __post_init__(self) -> None:
        if self.attempt_count < 0:
            raise ValueError("readiness attempt count must not be negative")
        if self.last_attempt is None and self.attempt_count != 0:
            raise ValueError("readiness without a last attempt must have zero attempts")
        if self.last_attempt is not None and self.last_attempt.attempt != self.attempt_count:
            raise ValueError("last readiness attempt must match the attempt count")
        if self.healthy and (
            self.last_attempt is None
            or self.last_attempt.outcome is not HealthAttemptOutcome.HEALTHY
        ):
            raise ValueError("healthy readiness requires a healthy final attempt")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("readiness duration must be finite and non-negative")
        if not self.reason:
            raise ValueError("readiness reason must not be empty")

    def as_evidence(self) -> dict[str, JsonValue]:
        return {
            "url": self.url,
            "healthy": self.healthy,
            "attempt_count": self.attempt_count,
            "last_attempt": (
                None if self.last_attempt is None else self.last_attempt.as_evidence()
            ),
            "duration_seconds": self.duration_seconds,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CandidateHealthResult:
    """Passing HTTP readiness plus optional complete smoke-command evidence."""

    readiness: ReadinessEvidence
    smoke: CommandResult | None

    def __post_init__(self) -> None:
        if not self.readiness.healthy:
            raise ValueError("candidate health result requires passing readiness")
        if self.smoke is not None and (
            self.smoke.outcome is not CommandOutcome.SUCCEEDED
            or self.smoke.stdout.truncated
            or self.smoke.stderr.truncated
        ):
            raise ValueError("candidate health result requires complete successful smoke evidence")

    def as_evidence(self) -> dict[str, JsonValue]:
        return {
            "readiness": self.readiness.as_evidence(),
            "smoke": None if self.smoke is None else self.smoke.as_evidence(),
        }


class CandidateHealthError(RuntimeError):
    """Base class for a rejected candidate health check."""


class ReadinessCheckError(CandidateHealthError):
    """The candidate did not return a 2xx response before the fixed deadline."""

    def __init__(self, evidence: ReadinessEvidence) -> None:
        self.evidence = evidence
        super().__init__(evidence.reason)


class SmokeCheckError(CandidateHealthError):
    """Readiness passed but the optional bounded smoke command did not."""

    def __init__(
        self,
        message: str,
        *,
        readiness: ReadinessEvidence,
        command: CommandResult,
    ) -> None:
        self.readiness = readiness
        self.command = command
        super().__init__(message)

    @property
    def cleanup_proven(self) -> bool:
        return self.command.outcome is not CommandOutcome.CLEANUP_FAILED

    def as_evidence(self) -> dict[str, JsonValue]:
        return {
            "readiness": self.readiness.as_evidence(),
            "smoke": self.command.as_evidence(),
        }


class _DeadlineExpired(RuntimeError):
    pass


class _RequestCancelled(RuntimeError):
    pass


class _BoundedBodyCapture:
    def __init__(self, limit: int) -> None:
        self._head_limit = limit // 2
        self._tail_limit = limit - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self._total_bytes = 0

    def append(self, data: bytes) -> None:
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

    def snapshot(self, secrets: SecretRegistry) -> BoundedResponseEvidence:
        retained = bytes(self._head + self._tail)
        retained_bytes = len(retained)
        return BoundedResponseEvidence(
            text=secrets.redact_text(retained.decode("utf-8", errors="replace")),
            total_bytes=self._total_bytes,
            retained_bytes=retained_bytes,
            truncated=retained_bytes < self._total_bytes,
        )


class ApplicationHealthChecker:
    """Check one configured application endpoint without trusting process state alone."""

    def __init__(
        self,
        *,
        application: ApplicationConfig,
        health_url: str,
        database_environment_name: str,
        secrets: SecretRegistry,
        working_directory: Path,
        smoke_environment: Mapping[str, str] | None = None,
        health_url_environment_name: str = APPLICATION_URL_ENV,
        command_environment: Mapping[str, str] | None = None,
        command_runner: CommandExecutor | None = None,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("provide either an HTTPX client or transport, not both")
        if not database_environment_name or "=" in database_environment_name:
            raise ValueError("database_environment_name must be a valid environment name")
        if not health_url or not health_url_environment_name or "=" in health_url_environment_name:
            raise ValueError("health URL and its environment name must be valid")
        if not working_directory.is_absolute():
            raise ValueError("working_directory must be absolute")
        self.application = application
        self.health_url = health_url
        self.health_url_environment_name = health_url_environment_name
        self.database_environment_name = database_environment_name
        self.secrets = secrets
        self.working_directory = working_directory
        self.command_environment = dict(
            os.environ if command_environment is None else command_environment
        )
        self.smoke_environment = dict(smoke_environment or {})
        self.command_runner = command_runner or SubprocessRunner(
            secrets=secrets,
            max_output_bytes=SMOKE_MAX_OUTPUT_BYTES,
        )
        self.request_timeout_seconds = _positive_seconds(
            request_timeout_seconds, "request_timeout_seconds"
        )
        self.retry_interval_seconds = _positive_seconds(
            retry_interval_seconds, "retry_interval_seconds"
        )
        self.max_response_bytes = _positive_integer(max_response_bytes, "max_response_bytes")
        self._clock = clock
        self._sleeper = sleeper
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close only the internally owned HTTPX client."""
        if self._owns_client:
            self._client.close()

    def check_application(
        self,
        *,
        version: str,
        database_path: Path,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateHealthResult:
        """Require readiness before the optional smoke command can execute."""
        release = validate_release_identifier(version)
        selected_database = _absolute_existing_file(database_path)
        readiness = self.wait_until_ready(cancellation_event=cancellation_event)
        command = self.application.smoke_command
        if command is None:
            return CandidateHealthResult(readiness=readiness, smoke=None)

        environment = dict(self.command_environment)
        environment.update(self.smoke_environment)
        environment[self.database_environment_name] = str(selected_database)
        environment[DPLOYDB_VERSION_ENV] = release
        environment[self.health_url_environment_name] = self.health_url
        smoke = self.command_runner.run(
            command,
            timeout_seconds=self.application.startup_timeout_seconds,
            environment=environment,
            working_directory=self.working_directory,
            cancellation_event=cancellation_event,
        )
        failure = _smoke_failure(smoke)
        if failure is not None:
            raise SmokeCheckError(failure, readiness=readiness, command=smoke)
        return CandidateHealthResult(readiness=readiness, smoke=smoke)

    def wait_until_ready(
        self,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> ReadinessEvidence:
        """Poll the configured URL under one monotonic overall deadline."""
        started_at = self._clock()
        deadline = started_at + float(self.application.startup_timeout_seconds)
        safe_url = self.secrets.redact_text(self.health_url)
        attempts = 0
        last_attempt: HealthAttemptEvidence | None = None

        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                evidence = ReadinessEvidence(
                    url=safe_url,
                    healthy=False,
                    attempt_count=attempts,
                    last_attempt=last_attempt,
                    duration_seconds=self._duration(started_at),
                    reason="candidate readiness was cancelled before a passing response",
                )
                raise ReadinessCheckError(evidence)
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ReadinessCheckError(
                    self._failed_readiness(
                        safe_url=safe_url,
                        attempts=attempts,
                        last_attempt=last_attempt,
                        started_at=started_at,
                    )
                )

            attempts += 1
            last_attempt = self._request_once(
                attempt=attempts,
                deadline=deadline,
                cancellation_event=cancellation_event,
            )
            if last_attempt.outcome is HealthAttemptOutcome.HEALTHY:
                return ReadinessEvidence(
                    url=safe_url,
                    healthy=True,
                    attempt_count=attempts,
                    last_attempt=last_attempt,
                    duration_seconds=self._duration(started_at),
                    reason="candidate returned a real HTTP 2xx readiness response",
                )
            if last_attempt.outcome is HealthAttemptOutcome.CANCELLED:
                raise ReadinessCheckError(
                    ReadinessEvidence(
                        url=safe_url,
                        healthy=False,
                        attempt_count=attempts,
                        last_attempt=last_attempt,
                        duration_seconds=self._duration(started_at),
                        reason="candidate readiness was cancelled before a passing response",
                    )
                )

            remaining = deadline - self._clock()
            if remaining <= 0:
                continue
            self._sleeper(min(self.retry_interval_seconds, remaining))

    def _request_once(
        self,
        *,
        attempt: int,
        deadline: float,
        cancellation_event: threading.Event | None,
    ) -> HealthAttemptEvidence:
        started_at = self._clock()
        status_code: int | None = None
        body: BoundedResponseEvidence | None = None
        try:
            remaining = deadline - started_at
            if remaining <= 0:
                raise _DeadlineExpired
            timeout = min(self.request_timeout_seconds, remaining)
            capture = _BoundedBodyCapture(self.max_response_bytes)
            with self._client.stream(
                "GET",
                self.health_url,
                follow_redirects=False,
                timeout=httpx.Timeout(timeout),
            ) as response:
                status_code = response.status_code
                for chunk in response.iter_bytes(chunk_size=8192):
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise _RequestCancelled
                    if self._clock() >= deadline:
                        raise _DeadlineExpired
                    capture.append(chunk)
                body = capture.snapshot(self.secrets)
        except _RequestCancelled:
            return self._attempt(
                attempt,
                HealthAttemptOutcome.CANCELLED,
                status_code,
                body,
                "readiness request was cancelled",
                started_at,
            )
        except _DeadlineExpired:
            return self._attempt(
                attempt,
                HealthAttemptOutcome.DEADLINE_EXPIRED,
                status_code,
                body,
                "readiness request reached the overall startup deadline",
                started_at,
            )
        except httpx.TransportError as error:
            return self._attempt(
                attempt,
                HealthAttemptOutcome.TRANSPORT_ERROR,
                None,
                None,
                "readiness request failed: " + self.secrets.redact_text(str(error)),
                started_at,
            )

        assert status_code is not None
        if 200 <= status_code < 300:
            outcome = HealthAttemptOutcome.HEALTHY
            reason = f"HTTP {status_code}"
        elif 300 <= status_code < 400:
            outcome = HealthAttemptOutcome.REDIRECT_REFUSED
            reason = f"HTTP {status_code} redirect was refused"
        else:
            outcome = HealthAttemptOutcome.UNHEALTHY_HTTP
            reason = f"HTTP {status_code} was not healthy"
        return self._attempt(attempt, outcome, status_code, body, reason, started_at)

    def _attempt(
        self,
        attempt: int,
        outcome: HealthAttemptOutcome,
        status_code: int | None,
        body: BoundedResponseEvidence | None,
        reason: str,
        started_at: float,
    ) -> HealthAttemptEvidence:
        return HealthAttemptEvidence(
            attempt=attempt,
            outcome=outcome,
            status_code=status_code,
            body=body,
            reason=self.secrets.redact_text(reason),
            duration_seconds=self._duration(started_at),
        )

    def _failed_readiness(
        self,
        *,
        safe_url: str,
        attempts: int,
        last_attempt: HealthAttemptEvidence | None,
        started_at: float,
    ) -> ReadinessEvidence:
        last_reason = "no request completed" if last_attempt is None else last_attempt.reason
        return ReadinessEvidence(
            url=safe_url,
            healthy=False,
            attempt_count=attempts,
            last_attempt=last_attempt,
            duration_seconds=self._duration(started_at),
            reason=(
                "candidate readiness deadline expired after "
                f"{self.application.startup_timeout_seconds:g} seconds; "
                f"last result: {last_reason}"
            ),
        )

    def _duration(self, started_at: float) -> float:
        return max(0.0, self._clock() - started_at)


class CandidateHealthChecker(ApplicationHealthChecker):
    """Backward-compatible candidate health adapter over the generic boundary."""

    def __init__(
        self,
        *,
        application: ApplicationConfig,
        database_environment_name: str,
        secrets: SecretRegistry,
        working_directory: Path,
        command_environment: Mapping[str, str] | None = None,
        command_runner: CommandExecutor | None = None,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            application=application,
            health_url=application.candidate_health_url,
            database_environment_name=database_environment_name,
            secrets=secrets,
            working_directory=working_directory,
            smoke_environment=application.test_mode_env,
            health_url_environment_name=CANDIDATE_URL_ENV,
            command_environment=command_environment,
            command_runner=command_runner,
            client=client,
            transport=transport,
            request_timeout_seconds=request_timeout_seconds,
            retry_interval_seconds=retry_interval_seconds,
            max_response_bytes=max_response_bytes,
            clock=clock,
            sleeper=sleeper,
        )

    def check(
        self,
        *,
        version: str,
        rehearsal_database_path: Path,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateHealthResult:
        return self.check_application(
            version=version,
            database_path=rehearsal_database_path,
            cancellation_event=cancellation_event,
        )


def _smoke_failure(result: CommandResult) -> str | None:
    if result.stdout.truncated or result.stderr.truncated:
        return "candidate smoke output exceeded the complete-capture safety bound"
    if result.outcome is CommandOutcome.SUCCEEDED:
        return None
    if result.outcome is CommandOutcome.NONZERO_EXIT:
        return f"candidate smoke command exited with status {result.exit_code}"
    if result.outcome is CommandOutcome.TIMED_OUT:
        return "candidate smoke command timed out and its process group was terminated"
    if result.outcome is CommandOutcome.CANCELLED:
        return "candidate smoke command was cancelled and its process group was terminated"
    if result.outcome is CommandOutcome.CLEANUP_FAILED:
        return "candidate smoke process cleanup could not be proven: " + (
            result.cleanup_error or "unknown cleanup failure"
        )
    return "candidate smoke command could not start: " + (
        result.start_error or "unknown start failure"
    )


def _absolute_existing_file(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError("rehearsal_database_path must be a pathlib.Path")
    if not path.is_absolute():
        raise ValueError("rehearsal_database_path must be absolute")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError("rehearsal_database_path must identify an existing file")
    return resolved


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
