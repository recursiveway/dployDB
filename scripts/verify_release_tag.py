"""Verify that one release tag matches metadata and points into origin/main."""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from pathlib import Path

TAG_PATTERN = re.compile(
    r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?$"
)


def _run(root: Path, arguments: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git command failed ({result.returncode}): {arguments!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    arguments = parser.parse_args()
    tag = arguments.tag
    if TAG_PATTERN.fullmatch(tag) is None:
        raise RuntimeError(f"release tag is not a supported canonical version: {tag!r}")

    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    if tag != f"v{version}":
        raise RuntimeError(f"tag {tag!r} does not match project version {version!r}")
    if _run(root, ["cat-file", "-t", f"refs/tags/{tag}"]) != "tag":
        raise RuntimeError("release tag must be annotated and signed, not lightweight")
    _run(root, ["merge-base", "--is-ancestor", tag, "refs/remotes/origin/main"])
    print(f"release tag {tag} matches metadata and is contained in origin/main")


if __name__ == "__main__":
    main()
