"""Verify that pipx can install and run DployDB in an isolated environment."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

COMMAND_TIMEOUT_SECONDS = 300


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


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
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
            }
        )

        run(
            [sys.executable, "-m", "pipx", "install", str(project_root)],
            env=env,
        )

        executable_name = "dploydb.exe" if os.name == "nt" else "dploydb"
        executable = bin_directory / executable_name

        help_result = run([str(executable), "--help"], env=env)
        if "Deployment safety for SQLite applications" not in help_result.stdout:
            raise RuntimeError("The pipx-installed CLI help did not identify DployDB")

        version_result = run([str(executable), "version"], env=env)
        option_result = run([str(executable), "--version"], env=env)
        if version_result.stdout.strip() != expected_version:
            raise RuntimeError(
                "The pipx-installed version command returned unexpected output: "
                f"{version_result.stdout.strip()!r}"
            )
        if option_result.stdout != version_result.stdout:
            raise RuntimeError("The pipx-installed version interfaces returned different output")

    print("pipx installation verification passed")


if __name__ == "__main__":
    main()
