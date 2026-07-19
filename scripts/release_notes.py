"""Extract one version's Markdown section from CHANGELOG.md."""

from __future__ import annotations

import argparse
from pathlib import Path


def release_notes(changelog: str, version: str) -> str:
    marker = f"## [{version}]"
    lines = changelog.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith(marker):
            start = index + 1
            break
    if start is None:
        raise RuntimeError(f"CHANGELOG.md has no section for {version}")
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## ["):
            end = index
            break
    selected = "\n".join(lines[start:end]).strip()
    if not selected:
        raise RuntimeError(f"CHANGELOG.md section for {version} is empty")
    return selected + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    selected = release_notes((root / "CHANGELOG.md").read_text(encoding="utf-8"), arguments.version)
    if arguments.output is None:
        print(selected, end="")
    else:
        arguments.output.write_text(selected, encoding="utf-8")


if __name__ == "__main__":
    main()
