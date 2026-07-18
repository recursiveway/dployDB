#!/usr/bin/env python3
"""Deterministic controller for the Docker Compose demo application."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = REPO_ROOT / "demo"
COMPOSE_FILE = DEMO_DIR / "compose.yaml"
RELEASES_ROOT = DEMO_DIR / "releases"
DEFAULT_STATE_ROOT = DEMO_DIR / ".state"
ALLOWED_RELEASES = ("v1", "v2", "broken-migration", "broken-health")
INSTANCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
COMMAND_TIMEOUT_SECONDS = 90.0
MIGRATION_TIMEOUT_SECONDS = 30.0
HEALTH_DEADLINE_SECONDS = 30.0
HTTP_TIMEOUT_SECONDS = 2.0
DIAGNOSTIC_LIMIT = 12_000


class DemoError(Exception):
    """A controlled demo operation failure."""


@dataclass(frozen=True)
class DemoContext:
    instance: str
    port: int
    state_root: Path
    instance_dir: Path
    data_dir: Path
    database_path: Path
    project_name: str


@dataclass(frozen=True)
class HealthResult:
    healthy: bool
    reason: str


def fail(message: str) -> NoReturn:
    raise DemoError(message)


def validate_instance(value: str) -> str:
    if not INSTANCE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "instance must be 1-64 ASCII letters, digits, dots, underscores, or hyphens, "
            "and must start with a letter or digit"
        )
    return value


def validate_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def is_strict_descendant(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return path != parent


def build_context(instance: str, port: int) -> DemoContext:
    raw_state_root = os.environ.get("DPLOYDB_DEMO_STATE_ROOT")
    state_root = Path(raw_state_root).expanduser() if raw_state_root else DEFAULT_STATE_ROOT
    state_root = state_root.resolve()
    instance_candidate = state_root / instance
    if instance_candidate.is_symlink():
        fail(f"refusing symlinked instance state: {instance_candidate}")
    instance_dir = instance_candidate.resolve()

    protected_paths = {REPO_ROOT.resolve(), DEMO_DIR.resolve(), state_root}
    if instance_dir in protected_paths or not is_strict_descendant(instance_dir, state_root):
        fail(
            f"refusing unsafe instance state path {instance_dir}; it must be a strict "
            f"descendant of {state_root}"
        )

    digest = hashlib.sha256(str(instance_dir).encode("utf-8")).hexdigest()[:10]
    project_instance = re.sub(r"[^a-z0-9_-]", "-", instance.lower())[:32]
    project_name = f"dploydb-demo-{project_instance}-{digest}"
    data_dir = instance_dir / "data"
    return DemoContext(
        instance=instance,
        port=port,
        state_root=state_root,
        instance_dir=instance_dir,
        data_dir=data_dir,
        database_path=data_dir / "app.db",
        project_name=project_name,
    )


def release_path(release: str, *, require_exists: bool = True) -> Path:
    if release not in ALLOWED_RELEASES:
        fail(f"unsupported release {release!r}; choose one of: {', '.join(ALLOWED_RELEASES)}")
    path = (RELEASES_ROOT / release).resolve()
    if not is_strict_descendant(path, RELEASES_ROOT.resolve()):
        fail(f"refusing release path outside {RELEASES_ROOT.resolve()}: {path}")
    if require_exists and not path.is_dir():
        fail(f"release directory does not exist: {path}")
    return path


def host_id(name: str) -> str:
    getter = getattr(os, name, None)
    if getter is None:
        fail(f"this controller requires a host that provides os.{name}()")
    return str(getter())


def command_environment(context: DemoContext, release: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DPLOYDB_DEMO_DATA_DIR": str(context.data_dir.resolve()),
            "DPLOYDB_DEMO_RELEASE_DIR": str(release_path(release, require_exists=False)),
            "DPLOYDB_DEMO_RELEASE_NAME": release,
            "DPLOYDB_DEMO_PORT": str(context.port),
            "DPLOYDB_DEMO_UID": host_id("getuid"),
            "DPLOYDB_DEMO_GID": host_id("getgid"),
        }
    )
    return environment


def format_output(
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    *,
    limit: int = DIAGNOSTIC_LIMIT,
) -> str:
    sections: list[str] = []
    for label, value in (("stdout", stdout), ("stderr", stderr)):
        decoded = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        text = (decoded or "").strip()
        if text:
            if len(text) > limit:
                text = f"{text[:limit]}\n... output truncated ..."
            sections.append(f"{label}:\n{text}")
    return "\n".join(sections)


def run_command(
    arguments: Sequence[str],
    *,
    environment: dict[str, str],
    timeout: float,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(arguments),
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        details = format_output(exc.stdout, exc.stderr)
        suffix = f"\n{details}" if details else ""
        fail(f"{operation} timed out after {timeout:g} seconds{suffix}")
    except OSError as exc:
        fail(f"{operation} could not start: {exc}")

    if result.returncode != 0:
        details = format_output(result.stdout, result.stderr)
        suffix = f"\n{details}" if details else ""
        fail(f"{operation} failed with exit code {result.returncode}{suffix}")
    return result


def compose_arguments(context: DemoContext, *arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "-p",
        context.project_name,
        *arguments,
    ]


def run_compose(
    context: DemoContext,
    release: str,
    *arguments: str,
    operation: str,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return run_command(
        compose_arguments(context, *arguments),
        environment=command_environment(context, release),
        timeout=timeout,
        operation=operation,
    )


def compose_down(context: DemoContext, release: str) -> None:
    run_compose(
        context,
        release,
        "down",
        "--remove-orphans",
        operation="stopping the demo application",
    )


def safe_reset_state(context: DemoContext) -> None:
    instance_dir = context.instance_dir.resolve()
    state_root = context.state_root.resolve()
    protected_paths = {REPO_ROOT.resolve(), DEMO_DIR.resolve(), state_root}
    if instance_dir in protected_paths or not is_strict_descendant(instance_dir, state_root):
        fail(
            f"refusing to delete unsafe path {instance_dir}; it must be a strict descendant "
            f"of {state_root}"
        )

    if instance_dir.exists():
        if instance_dir.is_symlink():
            fail(f"refusing to delete symlinked instance state: {instance_dir}")
        try:
            shutil.rmtree(instance_dir)
        except OSError as exc:
            fail(f"could not delete instance state {instance_dir}: {exc}")
    try:
        context.data_dir.mkdir(parents=True, exist_ok=False)
        context.database_path.touch(exist_ok=False)
    except OSError as exc:
        fail(f"could not create empty database {context.database_path}: {exc}")


def migrate_release(context: DemoContext, release: str) -> None:
    selected_release = release_path(release)
    if not context.database_path.is_file():
        fail(f"database does not exist: {context.database_path}; run reset first")
    environment = os.environ.copy()
    environment["DATABASE_PATH"] = str(context.database_path.resolve())
    result = run_command(
        [sys.executable, "-m", "demo.runtime.migration", str(selected_release)],
        environment=environment,
        timeout=MIGRATION_TIMEOUT_SECONDS,
        operation=f"migration for release {release}",
    )
    output = format_output(result.stdout, result.stderr)
    if output:
        print(output)


def up_release(context: DemoContext, release: str) -> None:
    release_path(release)
    if not context.database_path.is_file():
        fail(f"database does not exist: {context.database_path}; run reset first")
    run_compose(
        context,
        release,
        "up",
        "-d",
        "--build",
        "--force-recreate",
        "--remove-orphans",
        "app",
        operation=f"starting release {release}",
    )


def application_url(context: DemoContext) -> str:
    return f"http://127.0.0.1:{context.port}"


def health_url(context: DemoContext) -> str:
    return f"{application_url(context)}/health"


def response_failure_reason(body: bytes) -> str | None:
    if len(body) > 65_536:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    reason = payload.get("reason")
    return reason if isinstance(reason, str) and reason else None


def check_health(context: DemoContext, *, timeout: float = HTTP_TIMEOUT_SECONDS) -> HealthResult:
    url = health_url(context)
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read(65_537)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(65_537)
        finally:
            exc.close()
        reason = response_failure_reason(body)
        detail = f": {reason}" if reason else ""
        return HealthResult(False, f"HTTP status {exc.code}{detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        request_reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return HealthResult(False, f"request failed: {request_reason}")

    if status != 200:
        return HealthResult(False, f"HTTP status {status}")
    if len(body) > 65_536:
        return HealthResult(False, "response body exceeded 65536 bytes")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HealthResult(False, "response was not valid JSON")
    if not isinstance(payload, dict):
        return HealthResult(False, "response JSON was not an object")
    if payload.get("ok") is not True:
        return HealthResult(False, "response field 'ok' was not true")
    return HealthResult(True, "ok")


def wait_for_health(context: DemoContext) -> HealthResult:
    deadline = time.monotonic() + HEALTH_DEADLINE_SECONDS
    last_result = HealthResult(False, "health deadline expired before the first request")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return HealthResult(
                False,
                f"health deadline expired after {HEALTH_DEADLINE_SECONDS:g} seconds; "
                f"last result: {last_result.reason}",
            )
        last_result = check_health(context, timeout=min(HTTP_TIMEOUT_SECONDS, remaining))
        if last_result.healthy:
            return last_result
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def diagnostic_command(
    context: DemoContext, release: str, arguments: Sequence[str], label: str
) -> str:
    try:
        result = subprocess.run(
            compose_arguments(context, *arguments),
            cwd=REPO_ROOT,
            env=command_environment(context, release),
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"{label}: diagnostic command timed out after 15 seconds"
    except OSError as exc:
        return f"{label}: diagnostic command could not start: {exc}"
    details = format_output(result.stdout, result.stderr)
    heading = f"{label} (exit {result.returncode})"
    return f"{heading}:\n{details}" if details else f"{heading}: no output"


def fail_start_and_cleanup(context: DemoContext, release: str, primary_failure: str) -> NoReturn:
    diagnostics = [
        diagnostic_command(context, release, ["ps", "--all"], "compose ps"),
        diagnostic_command(
            context,
            release,
            ["logs", "--no-color", "--tail", "100", "app"],
            "compose logs",
        ),
    ]
    cleanup_failure: str | None = None
    try:
        compose_down(context, release)
    except DemoError as exc:
        cleanup_failure = str(exc)

    message = f"{primary_failure}\n" + "\n".join(diagnostics)
    if cleanup_failure:
        message += f"\ncleanup also failed: {cleanup_failure}"
    fail(message)


def start_release(context: DemoContext, release: str) -> None:
    try:
        up_release(context, release)
    except DemoError as exc:
        fail_start_and_cleanup(context, release, f"release {release} failed to start: {exc}")

    result = wait_for_health(context)
    if not result.healthy:
        fail_start_and_cleanup(
            context,
            release,
            f"release {release} failed health: {result.reason}",
        )

    print(f"URL: {application_url(context)}")
    print(f"Database: {context.database_path}")


def reset_to_v1(context: DemoContext) -> None:
    compose_down(context, "v1")
    safe_reset_state(context)
    migrate_release(context, "v1")


def start_v1(context: DemoContext) -> None:
    reset_to_v1(context)
    start_release(context, "v1")


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", type=validate_instance, default="default")
    parser.add_argument("--port", type=validate_port, default=4510)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start-v1", help="reset, migrate, start, and verify v1")
    subparsers.add_parser("reset", help="stop the app and recreate a migrated v1 database")
    for command, help_text in (
        ("migrate", "migrate the existing database"),
        ("up", "start or recreate a release without waiting for health"),
        ("start", "start or recreate a release and require healthy HTTP"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("release", choices=ALLOWED_RELEASES, metavar="RELEASE")
    subparsers.add_parser("health", help="check the running application's HTTP health")
    subparsers.add_parser("stop", help="stop the application while preserving its database")
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    options = parse_arguments(arguments)
    try:
        context = build_context(options.instance, options.port)
        if options.command == "start-v1":
            start_v1(context)
        elif options.command == "reset":
            reset_to_v1(context)
            print(f"Database: {context.database_path}")
        elif options.command == "migrate":
            migrate_release(context, options.release)
        elif options.command == "up":
            up_release(context, options.release)
        elif options.command == "start":
            start_release(context, options.release)
        elif options.command == "health":
            result = check_health(context)
            if not result.healthy:
                print(f"controller failed: unhealthy: {result.reason}", file=sys.stderr)
                return 1
            print("healthy: ok")
        elif options.command == "stop":
            compose_down(context, "v1")
        else:  # pragma: no cover - argparse enforces the command set.
            fail(f"unsupported command: {options.command}")
    except DemoError as exc:
        print(f"controller failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
