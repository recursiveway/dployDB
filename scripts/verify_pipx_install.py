"""Verify that pipx can install and run DployDB in an isolated environment."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

COMMAND_TIMEOUT_SECONDS = 300
REQUIRED_HELP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("--help",), "Deployment safety for SQLite applications"),
    (("init", "--help"), "Create a restrictive, valid starter configuration"),
    (("doctor", "--help"), "Check configured host safety"),
    (("backup", "--help"), "Create one verified local backup"),
    (("verify", "--help"), "Reverify one committed local backup"),
    (("deploy", "--help"), "automatic pre-traffic rollback"),
    (("status", "--help"), "durable operation state"),
    (("releases", "--help"), "validated local deployment release"),
    (("release", "--help"), "Inspect one durable deployment release"),
    (("release", "show", "--help"), "complete validated release manifest"),
    (("restore", "--help"), "backup-first restore"),
    (("recover", "--help"), "interrupted deployment"),
    (("version", "--help"), "installed DployDB version"),
)


def run(command: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifact",
        nargs="?",
        type=Path,
        help="Optional wheel or source tree to install; defaults to the repository root.",
    )
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    project_root = Path(__file__).resolve().parents[1]
    install_target = (arguments.artifact or project_root).resolve()
    if not install_target.exists():
        raise RuntimeError(f"install target does not exist: {install_target}")
    expected_version = f"dploydb {version('dploydb')}"

    with tempfile.TemporaryDirectory(prefix="dploydb-pipx-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        bin_directory = temporary_path / "bin"
        env = os.environ.copy()
        env.update(
            {
                "PIPX_HOME": str(temporary_path / "home"),
                "PIPX_BIN_DIR": str(bin_directory),
                "PIPX_MAN_DIR": str(temporary_path / "man"),
                "NO_COLOR": "1",
            }
        )

        run(
            [sys.executable, "-m", "pipx", "install", str(install_target)],
            env=env,
        )

        executable_name = "dploydb.exe" if os.name == "nt" else "dploydb"
        executable = bin_directory / executable_name

        for help_arguments, expected_text in REQUIRED_HELP:
            help_result = run(
                [str(executable), "--no-color", *help_arguments],
                env=env,
            )
            if expected_text not in help_result.stdout or "Usage:" not in help_result.stdout:
                raise RuntimeError(
                    "The pipx-installed CLI returned unhelpful output for "
                    + " ".join(help_arguments)
                )
            if "\x1b[" in help_result.stdout or "\x1b[" in help_result.stderr:
                raise RuntimeError("The pipx-installed CLI emitted ANSI escapes despite --no-color")

        version_result = run([str(executable), "version"], env=env)
        option_result = run([str(executable), "--no-color", "--version"], env=env)
        if version_result.stdout.strip() != expected_version:
            raise RuntimeError(
                "The pipx-installed version command returned unexpected output: "
                f"{version_result.stdout.strip()!r}"
            )
        if option_result.stdout != version_result.stdout:
            raise RuntimeError("The pipx-installed version interfaces returned different output")

        persistent_directory = temporary_path / "must-survive-uninstall"
        persistent_directory.mkdir()
        config_path = persistent_directory / "dploydb.yaml"
        backup_sentinel = persistent_directory / "backup-sentinel.db"
        backup_sentinel.write_bytes(b"verified-backup-must-survive\n")
        init_result = run(
            [
                str(executable),
                "--no-color",
                "init",
                "--config",
                str(config_path),
                "--json",
            ],
            env=env,
        )
        init_payload = json.loads(init_result.stdout)
        if init_payload != {"config_path": str(config_path), "ok": True}:
            raise RuntimeError("The pipx-installed init command returned unstable JSON")
        if "\x1b[" in init_result.stdout or "\x1b[" in init_result.stderr:
            raise RuntimeError("The pipx-installed JSON command emitted ANSI escapes")
        config_before = config_path.read_bytes()
        backup_before = backup_sentinel.read_bytes()

        run([sys.executable, "-m", "pipx", "uninstall", "dploydb"], env=env)
        if config_path.read_bytes() != config_before:
            raise RuntimeError("pipx uninstall changed the user configuration")
        if backup_sentinel.read_bytes() != backup_before:
            raise RuntimeError("pipx uninstall changed user backup data")
        if executable.exists():
            raise RuntimeError("pipx uninstall left the DployDB console entry point installed")

    print(f"pipx installation, CLI, JSON, and uninstall verification passed: {install_target}")


if __name__ == "__main__":
    main()
