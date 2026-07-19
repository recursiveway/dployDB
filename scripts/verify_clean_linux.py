"""Run the README installation and real deployment gate in disposable Docker-in-Docker Linux."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
import time
import zipfile
from email import message_from_bytes
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

DEFAULT_IMAGE = "docker:29.1-dind"
COMMAND_TIMEOUT_SECONDS = 300
DEPLOY_TIMEOUT_SECONDS = 900
MINIMUM_PYTHON = (3, 12)
REQUIRED_HELP: tuple[tuple[str, ...], ...] = (
    ("--help",),
    ("init", "--help"),
    ("doctor", "--help"),
    ("backup", "--help"),
    ("verify", "--help"),
    ("deploy", "--help"),
    ("status", "--help"),
    ("releases", "--help"),
    ("release", "--help"),
    ("release", "show", "--help"),
    ("restore", "--help"),
    ("recover", "--help"),
    ("version", "--help"),
)


class GateError(RuntimeError):
    """One controlled clean-Linux acceptance failure."""


def _run(
    command: list[str],
    *,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(f"command could not complete: {command!r}: {exc}") from exc
    if check and result.returncode != 0:
        raise GateError(
            f"command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _stop(message: str) -> NoReturn:
    raise GateError(message)


def _source_files(root: Path) -> tuple[Path, ...]:
    result = _run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
    )
    paths: list[Path] = []
    for item in result.stdout.split("\0"):
        if not item:
            continue
        relative = Path(item)
        source = root / relative
        if source.is_file() and not source.is_symlink():
            paths.append(relative)
    if not paths:
        _stop("the clean-Linux source bundle would be empty")
    return tuple(sorted(paths))


def _write_source_archive(root: Path, destination: Path) -> int:
    files = _source_files(root)
    with tarfile.open(destination, "w") as archive:
        for relative in files:
            archive.add(root / relative, arcname=relative, recursive=False)
    return len(files)


def _exec(
    container: str,
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
    workdir: str | None = "/workspace",
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "exec"]
    if workdir is not None:
        command.extend(("--workdir", workdir))
    for name, value in sorted((environment or {}).items()):
        command.extend(("--env", f"{name}={value}"))
    command.append(container)
    command.extend(arguments)
    return _run(command, timeout=timeout, check=check)


def _wait_for_nested_docker(container: str) -> str:
    deadline = time.monotonic() + 90
    last = "nested Docker did not answer"
    while time.monotonic() < deadline:
        result = _exec(
            container,
            ["docker", "info", "--format", "{{json .ServerVersion}}"],
            workdir=None,
            timeout=15,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().strip('"')
        last = result.stderr.strip() or result.stdout.strip() or last
        time.sleep(1)
    _stop(f"nested Docker did not become ready: {last}")


def _python_version(container: str) -> str:
    result = _exec(container, ["python3", "--version"], workdir=None)
    match = re.search(r"Python (\d+)\.(\d+)\.(\d+)", result.stdout)
    if match is None:
        _stop(f"clean Linux returned an unknown Python version: {result.stdout!r}")
    version = tuple(int(part) for part in match.groups())
    if version < (*MINIMUM_PYTHON, 0):
        _stop(f"clean Linux Python is too old: {result.stdout.strip()}")
    return result.stdout.strip()


def _tree_hash(container: str) -> dict[str, str]:
    code = """
import hashlib, json
from pathlib import Path
root = Path('/workspace/demo/.state/quickstart')
result = {}
for path in sorted(item for item in root.rglob('*') if item.is_file()):
    result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps(result, sort_keys=True, separators=(',', ':')))
""".strip()
    result = _exec(container, ["python3", "-c", code])
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or not value:
        _stop("clean Linux did not preserve any demo state to audit")
    return {str(name): str(digest) for name, digest in value.items()}


def wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        matches = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(matches) != 1:
            _stop(f"wheel has unexpected metadata files: {matches!r}")
        selected = message_from_bytes(archive.read(matches[0])).get("Version")
    if not selected:
        _stop("wheel metadata has no Version field")
    return selected


def _installed_cli_audit(container: str, executable: str, *, expected_version: str) -> None:
    version = _exec(container, [executable, "--no-color", "version"])
    if version.stdout.strip() != f"dploydb {expected_version}":
        _stop(f"installed CLI returned an unexpected version: {version.stdout!r}")
    for arguments in REQUIRED_HELP:
        result = _exec(container, [executable, "--no-color", *arguments])
        if "Usage:" not in result.stdout or "\x1b[" in result.stdout + result.stderr:
            _stop(f"installed help audit failed for {arguments!r}")


def _demo_environment() -> dict[str, str]:
    return {
        "DPLOYDB_DEMO_DATA_DIR": "/workspace/demo/.state/quickstart/data",
        "DPLOYDB_DEMO_GID": "0",
        "DPLOYDB_DEMO_PORT": "4510",
        "DPLOYDB_DEMO_RELEASE_DIR": "/workspace/demo/releases/v2",
        "DPLOYDB_DEMO_UID": "0",
        "DPLOYDB_VERSION": "v1",
        "NO_COLOR": "1",
        "PWD": "/workspace",
        "PYTHONPATH": "/workspace",
    }


def _run_readme_demo(container: str, executable: str) -> dict[str, Any]:
    _exec(
        container,
        [
            "python3",
            "demo/controller.py",
            "--instance",
            "quickstart",
            "--port",
            "4510",
            "start-v1",
        ],
        timeout=DEPLOY_TIMEOUT_SECONDS,
    )
    _exec(
        container,
        [
            "python3",
            "-m",
            "demo.prepare",
            "--instance",
            "quickstart",
            "--port",
            "4510",
            "--candidate-port",
            "4511",
        ],
    )
    environment = _demo_environment()
    config = "/workspace/demo/.state/quickstart/dploydb.yaml"
    doctor = _exec(
        container,
        [executable, "--no-color", "doctor", "--deep", "--config", config, "--json"],
        environment=environment,
        timeout=DEPLOY_TIMEOUT_SECONDS,
    )
    doctor_payload = json.loads(doctor.stdout)
    if doctor_payload.get("ok") is not True or doctor_payload.get("deep") is not True:
        _stop(f"clean Linux doctor did not pass: {doctor_payload!r}")

    deployment = _exec(
        container,
        [
            executable,
            "--no-color",
            "deploy",
            "--version",
            "v2",
            "--config",
            config,
            "--json",
            "--non-interactive",
        ],
        environment=environment,
        timeout=DEPLOY_TIMEOUT_SECONDS,
    )
    if "\x1b[" in deployment.stdout + deployment.stderr:
        _stop("clean Linux JSON deployment emitted ANSI escapes")
    raw_deployment = json.loads(deployment.stdout)
    if not isinstance(raw_deployment, dict):
        _stop("clean Linux deployment JSON was not an object")
    deployment_payload: dict[str, Any] = raw_deployment
    if not (
        deployment_payload.get("ok") is True
        and deployment_payload.get("outcome") == "active"
        and deployment_payload.get("traffic_activated") is True
        and deployment_payload.get("recovery_required") is False
    ):
        _stop(f"clean Linux deployment result was not active: {deployment_payload!r}")

    health = _exec(
        container,
        ["curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:4510/health"],
    )
    health_payload = json.loads(health.stdout)
    if health_payload.get("release") != "v2" or health_payload.get("ok") is not True:
        _stop(f"clean Linux v2 health result was unexpected: {health_payload!r}")

    releases = _exec(
        container,
        [executable, "releases", "--config", config, "--json"],
        environment=environment,
    )
    release_payload = json.loads(releases.stdout)
    if release_payload.get("active_release_id") != deployment_payload.get("release_id"):
        _stop("clean Linux release history does not select the deployed release")
    selected_releases = release_payload.get("releases")
    if not isinstance(selected_releases, list) or len(selected_releases) != 1:
        _stop(f"clean Linux release history was unexpected: {release_payload!r}")
    operation_log = selected_releases[0].get("log_path")
    if not isinstance(operation_log, str) or not operation_log.startswith("/workspace/"):
        _stop(f"clean Linux release log path was not absolute: {operation_log!r}")

    database_check = _exec(
        container,
        [
            "python3",
            "-c",
            (
                "import sqlite3; p='/workspace/demo/.state/quickstart/data/app.db'; "
                "c=sqlite3.connect(p); assert c.execute('pragma user_version').fetchone()==(2,); "
                "assert [r[1] for r in c.execute('pragma table_info(notes)')]=="
                "['id','body','category']; print('sqlite_v2_verified')"
            ),
        ],
    )
    if database_check.stdout.strip() != "sqlite_v2_verified":
        _stop("clean Linux SQLite v2 verification did not complete")

    _exec(
        container,
        [
            "python3",
            "demo/controller.py",
            "--instance",
            "quickstart",
            "--port",
            "4510",
            "stop",
        ],
        environment=environment,
        timeout=DEPLOY_TIMEOUT_SECONDS,
    )
    remaining = _exec(
        container,
        [
            "docker",
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project",
        ],
    )
    if remaining.stdout.strip():
        identifiers = remaining.stdout.split()
        details = _exec(
            container,
            [
                "docker",
                "container",
                "inspect",
                "--format",
                "{{json .Name}} {{json .Config.Labels}} {{json .State.Status}}",
                *identifiers,
            ],
        )
        _stop(
            "clean Linux demo left Compose containers behind: "
            f"{remaining.stdout.strip()}\n{details.stdout.strip()}"
        )
    networks = _exec(
        container,
        ["docker", "network", "ls", "--format", "{{.Name}}"],
    )
    remaining_networks = [
        name for name in networks.stdout.splitlines() if "dploydb" in name.lower()
    ]
    if remaining_networks:
        _stop(f"clean Linux demo left DployDB networks behind: {remaining_networks!r}")
    return deployment_payload


def _image_identity(image: str) -> str:
    result = _run(["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"])
    values = json.loads(result.stdout)
    if isinstance(values, list) and values:
        return str(values[0])
    return image


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--wheel", type=Path)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    root = Path(__file__).resolve().parents[1]
    wheel = arguments.wheel
    if wheel is None:
        wheels = sorted((root / "dist").glob("dploydb-*.whl"))
        if len(wheels) != 1:
            _stop("build exactly one DployDB wheel or pass --wheel")
        wheel = wheels[0]
    wheel = wheel.resolve()
    if not wheel.is_file():
        _stop(f"wheel does not exist: {wheel}")

    container = f"dploydb-m8-linux-{uuid4().hex[:12]}"
    container_wheel = f"/root/{wheel.name}"
    started = False
    summary: dict[str, Any] = {
        "ok": False,
        "container": container,
        "wheel": wheel.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    expected_version = wheel_version(wheel)
    summary["version"] = expected_version
    with tempfile.TemporaryDirectory(prefix="dploydb-m8-linux-") as temporary:
        archive = Path(temporary) / "source.tar"
        summary["source_file_count"] = _write_source_archive(root, archive)
        try:
            _run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--privileged",
                    "--name",
                    container,
                    arguments.image,
                ],
                timeout=DEPLOY_TIMEOUT_SECONDS,
            )
            started = True
            summary["docker_server"] = _wait_for_nested_docker(container)
            summary["image"] = _image_identity(arguments.image)
            _run(["docker", "cp", str(archive), f"{container}:/root/source.tar"])
            _run(["docker", "cp", str(wheel), f"{container}:{container_wheel}"])
            _exec(container, ["test", "-f", "/root/source.tar"], workdir=None)
            _exec(container, ["test", "-f", container_wheel], workdir=None)
            _exec(container, ["mkdir", "-p", "/workspace"], workdir=None)
            _exec(
                container,
                ["tar", "-xf", "/root/source.tar", "-C", "/workspace"],
                workdir=None,
            )
            _exec(
                container,
                [
                    "apk",
                    "add",
                    "--no-cache",
                    "curl",
                    "docker-cli-compose",
                    "nginx",
                    "pipx",
                    "python3",
                ],
                workdir=None,
                timeout=DEPLOY_TIMEOUT_SECONDS,
            )
            _exec(
                container,
                [
                    "cp",
                    "/workspace/examples/nginx/site.conf",
                    "/etc/nginx/http.d/default.conf",
                ],
                workdir=None,
            )
            _exec(container, ["nginx", "-t"], workdir=None)
            summary["nginx_config_verified"] = True
            summary["python"] = _python_version(container)
            _exec(
                container,
                ["pipx", "install", container_wheel],
                workdir=None,
                timeout=DEPLOY_TIMEOUT_SECONDS,
            )
            executable = "/root/.local/bin/dploydb"
            _installed_cli_audit(container, executable, expected_version=expected_version)
            deployment = _run_readme_demo(container, executable)
            summary["release_id"] = deployment["release_id"]
            summary["outcome"] = deployment["outcome"]
            before_uninstall = _tree_hash(container)
            if not any(name.startswith("backups/") for name in before_uninstall):
                _stop("clean Linux deployment produced no local backup evidence")
            _exec(container, ["pipx", "uninstall", "dploydb"], workdir=None)
            after_uninstall = _tree_hash(container)
            if after_uninstall != before_uninstall:
                _stop("pipx uninstall changed demo database, state, release, or backup bytes")
            executable_check = _exec(
                container,
                ["test", "!", "-e", executable],
                workdir=None,
            )
            if executable_check.returncode != 0:
                _stop("pipx uninstall left the console entry point installed")
            summary["preserved_file_count"] = len(after_uninstall)
            summary["uninstall_preserved_evidence"] = True
            summary["ok"] = True
        finally:
            if started:
                cleanup = _run(
                    ["docker", "container", "rm", "--force", container],
                    timeout=120,
                    check=False,
                )
                summary["outer_cleanup"] = cleanup.returncode == 0
    if summary.get("ok") is not True or summary.get("outer_cleanup") is not True:
        _stop(f"clean Linux gate did not finish safely: {summary!r}")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
