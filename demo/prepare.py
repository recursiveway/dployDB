#!/usr/bin/env python3
"""Prepare generated DployDB configuration for the documented source-tree demo."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import NoReturn

from demo.controller import REPO_ROOT, build_context, release_path, validate_instance, validate_port

FILE_MODE = 0o600
DIRECTORY_MODE = 0o700


def fail(message: str) -> NoReturn:
    raise SystemExit(f"demo preparation failed: {message}")


def _write_new_private(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    created = False
    try:
        descriptor = os.open(path, flags, FILE_MODE)
        created = True
        os.fchmod(descriptor, FILE_MODE)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("write made no progress")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            path.unlink(missing_ok=True)
        fail(f"could not create {path}: {exc}")


def _environment_text(*, data: Path, release: Path, port: int) -> str:
    values = {
        "DPLOYDB_DEMO_DATA_DIR": str(data),
        "DPLOYDB_DEMO_RELEASE_DIR": str(release),
        "DPLOYDB_DEMO_PORT": str(port),
        "DPLOYDB_DEMO_UID": str(os.getuid()),
        "DPLOYDB_DEMO_GID": str(os.getgid()),
        "DPLOYDB_VERSION": "v1",
        "PYTHONPATH": str(REPO_ROOT),
    }
    return "".join(f"export {name}={shlex.quote(value)}\n" for name, value in values.items())


def prepare_demo(
    *,
    instance: str,
    production_port: int,
    candidate_port: int,
    python_executable: Path,
) -> tuple[Path, Path, Path]:
    """Create a private config, traffic state, and shell environment without overwriting."""
    if production_port == candidate_port:
        fail("production and candidate ports must differ")
    if not python_executable.is_absolute():
        fail("the Python executable must be an absolute path")

    context = build_context(instance, production_port)
    if not context.database_path.is_file():
        fail(
            f"the v1 database does not exist at {context.database_path}; "
            "run demo/controller.py start-v1 first"
        )
    try:
        context.instance_dir.chmod(DIRECTORY_MODE)
    except OSError as exc:
        fail(f"could not make the demo instance private: {exc}")

    config_path = context.instance_dir / "dploydb.yaml"
    traffic_path = context.instance_dir / "traffic.json"
    environment_path = context.instance_dir / "dploydb.env"
    existing = [path for path in (config_path, traffic_path, environment_path) if path.exists()]
    if existing:
        fail(
            "generated demo files already exist and were preserved: "
            + ", ".join(str(path) for path in existing)
            + "; run start-v1 to reset the demo instance before preparing it again"
        )

    v2_release = release_path("v2").resolve()
    hook = [
        str(python_executable),
        "-m",
        "demo.runtime.traffic_hook",
        str(traffic_path),
    ]
    configuration = {
        "project": context.project_name,
        "state_directory": str((context.instance_dir / "dploydb-state").resolve()),
        "database": {
            "path": str(context.database_path.resolve()),
            "path_env": "DATABASE_PATH",
            "minimum_free_space_multiplier": 3,
        },
        "migration": {
            "command": [
                str(python_executable),
                "-m",
                "demo.runtime.migration",
                str(v2_release),
            ],
            "timeout_seconds": 30,
        },
        "application": {
            "runner": "docker_compose",
            "compose_file": str((REPO_ROOT / "demo" / "compose.yaml").resolve()),
            "service": "app",
            "production_project": context.project_name,
            "production_port": production_port,
            "production_health_url": f"http://127.0.0.1:{production_port}/health",
            "candidate_port": candidate_port,
            "candidate_container_port": 8080,
            "database_volume_target": "/data",
            "candidate_health_url": f"http://127.0.0.1:{candidate_port}/health",
            "startup_timeout_seconds": 30,
            "test_mode_env": {"DPLOYDB_TEST_MODE": "1"},
        },
        "traffic": {
            "maintenance_on_command": [*hook, "maintenance-on"],
            "maintenance_off_command": [*hook, "maintenance-off"],
            "activate_new_command": [*hook, "activate-new"],
            "activate_old_command": [*hook, "activate-old"],
            "timeout_seconds": 10,
        },
        "backup": {
            "local_directory": str((context.instance_dir / "backups").resolve()),
            "keep_last": 10,
            "remote": {"enabled": False, "required": False},
        },
    }
    config_bytes = (json.dumps(configuration, indent=2, sort_keys=True) + "\n").encode()
    traffic_bytes = b'{"events":[],"maintenance":false,"target":"old"}\n'
    environment_bytes = _environment_text(
        data=context.data_dir.resolve(),
        release=v2_release,
        port=production_port,
    ).encode()

    created_paths: list[Path] = []
    try:
        _write_new_private(traffic_path, traffic_bytes)
        created_paths.append(traffic_path)
        _write_new_private(config_path, config_bytes)
        created_paths.append(config_path)
        _write_new_private(environment_path, environment_bytes)
        created_paths.append(environment_path)
    except BaseException:
        for created_path in created_paths:
            created_path.unlink(missing_ok=True)
        raise
    return config_path, environment_path, traffic_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", type=validate_instance, default="quickstart")
    parser.add_argument("--port", type=validate_port, default=4510)
    parser.add_argument("--candidate-port", type=validate_port, default=4511)
    arguments = parser.parse_args()

    config_path, environment_path, traffic_path = prepare_demo(
        instance=arguments.instance,
        production_port=arguments.port,
        candidate_port=arguments.candidate_port,
        python_executable=Path(sys.executable).resolve(),
    )
    print(f"config={config_path}")
    print(f"environment={environment_path}")
    print(f"traffic_state={traffic_path}")
    print(f"next: . {shlex.quote(str(environment_path))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
