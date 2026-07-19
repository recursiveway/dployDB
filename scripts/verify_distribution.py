"""Validate DployDB wheel and source-distribution metadata and public contents."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import zipfile
from email import message_from_bytes
from email.message import Message
from pathlib import Path, PurePosixPath
from typing import NoReturn

PROJECT_NAME = "dploydb"
EXPECTED_LICENSE = "Apache-2.0"
EXPECTED_AUTHOR = "RecursiveWay"
EXPECTED_CLASSIFIER = "Development Status :: 3 - Alpha"
EXPECTED_URLS = {
    "Homepage": "https://github.com/recursiveway/dployDB",
    "Repository": "https://github.com/recursiveway/dployDB",
    "Issues": "https://github.com/recursiveway/dployDB/issues",
    "Documentation": "https://github.com/recursiveway/dployDB/tree/main/docs",
}
REQUIRED_SDIST_TOP_LEVEL = {
    ".gitignore",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "RELEASING.md",
    "SECURITY.md",
    "demo",
    "docs",
    "dploydb",
    "examples",
    "pyproject.toml",
    "scripts",
    "tests",
    "uv.lock",
}
ALLOWED_SDIST_TOP_LEVEL = REQUIRED_SDIST_TOP_LEVEL | {"PKG-INFO"}
FORBIDDEN_PARTS = {
    ".agents",
    ".claude",
    ".codex",
    ".env",
    ".envrc",
    ".git",
    ".state",
    ".venv",
    "AGENTS.md",
    "IMPLEMENTATION_PLAN.md",
    "__pycache__",
    "dist",
}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


class DistributionError(RuntimeError):
    """One controlled public-distribution validation failure."""


def _stop(message: str) -> NoReturn:
    raise DistributionError(message)


def _only_artifact(directory: Path, pattern: str, label: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        _stop(f"expected exactly one {label} matching {pattern!r}, found {len(matches)}")
    return matches[0]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--tag", help="Optional release tag, for example v0.1.0")
    return parser.parse_args()


def _metadata(values: dict[str, bytes], suffix: str) -> tuple[str, Message]:
    matches = [name for name in values if name.endswith(suffix)]
    if len(matches) != 1:
        _stop(f"expected exactly one {suffix} metadata file, found {matches!r}")
    name = matches[0]
    return name, message_from_bytes(values[name])


def _project_urls(metadata: Message) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in metadata.get_all("Project-URL", []):
        label, separator, url = value.partition(",")
        if not separator:
            _stop(f"invalid Project-URL metadata: {value!r}")
        result[label.strip()] = url.strip()
    return result


def _validate_metadata(metadata: Message, expected_version: str | None) -> str:
    if metadata.get("Name") != PROJECT_NAME:
        _stop(f"unexpected package name: {metadata.get('Name')!r}")
    version = metadata.get("Version")
    if not version:
        _stop("package metadata has no version")
    if expected_version is not None and version != expected_version:
        _stop(f"artifact version {version!r} does not match {expected_version!r}")
    if metadata.get("Author") != EXPECTED_AUTHOR:
        _stop(f"unexpected author metadata: {metadata.get('Author')!r}")
    if metadata.get("License-Expression") != EXPECTED_LICENSE:
        _stop(f"unexpected license expression: {metadata.get('License-Expression')!r}")
    license_files = set(metadata.get_all("License-File", []))
    if license_files != {"LICENSE", "NOTICE"}:
        _stop(f"unexpected license files: {sorted(license_files)!r}")
    if EXPECTED_CLASSIFIER not in metadata.get_all("Classifier", []):
        _stop("Alpha classifier is missing from package metadata")
    if metadata.get("Requires-Python") != ">=3.12":
        _stop(f"unexpected Python requirement: {metadata.get('Requires-Python')!r}")
    if _project_urls(metadata) != EXPECTED_URLS:
        _stop(f"unexpected project URLs: {_project_urls(metadata)!r}")
    return version


def _validate_names(names: set[str]) -> None:
    for raw_name in names:
        path = PurePosixPath(raw_name)
        if any(part in FORBIDDEN_PARTS for part in path.parts):
            _stop(f"private or internal path is present in the distribution: {raw_name}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            _stop(f"database artifact is present in the distribution: {raw_name}")


def _validate_license_bytes(
    values: dict[str, bytes], *, license_name: str, notice_name: str, root: Path
) -> None:
    expected_license = (root / "LICENSE").read_bytes()
    expected_notice = (root / "NOTICE").read_bytes()
    if values.get(license_name) != expected_license:
        _stop(f"published LICENSE bytes do not match the repository: {license_name}")
    if values.get(notice_name) != expected_notice:
        _stop(f"published NOTICE bytes do not match the repository: {notice_name}")


def _read_wheel(path: Path) -> dict[str, bytes]:
    if not path.is_file():
        _stop(f"wheel does not exist: {path}")
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}


def _read_sdist(path: Path) -> dict[str, bytes]:
    if not path.is_file():
        _stop(f"source distribution does not exist: {path}")
    with tarfile.open(path, "r:gz") as archive:
        result: dict[str, bytes] = {}
        for member in archive.getmembers():
            if not member.isfile():
                continue
            selected = archive.extractfile(member)
            if selected is None:
                _stop(f"could not read source-distribution member: {member.name}")
            result[member.name] = selected.read()
        return result


def _sdist_prefix(names: set[str]) -> str:
    roots = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
    if len(roots) != 1:
        _stop(f"source distribution must have one root directory, found {sorted(roots)!r}")
    return next(iter(roots))


def _version_from_tag(tag: str | None) -> str | None:
    if tag is None:
        return None
    if not tag.startswith("v") or len(tag) == 1:
        _stop(f"release tag must start with v: {tag!r}")
    return tag[1:]


def main() -> None:
    arguments = _arguments()
    root = Path(__file__).resolve().parents[1]
    dist = root / "dist"
    wheel = (arguments.wheel or _only_artifact(dist, "dploydb-*.whl", "wheel")).resolve()
    sdist = (
        arguments.sdist or _only_artifact(dist, "dploydb-*.tar.gz", "source distribution")
    ).resolve()
    expected_version = _version_from_tag(arguments.tag)

    wheel_values = _read_wheel(wheel)
    wheel_names = set(wheel_values)
    _validate_names(wheel_names)
    wheel_metadata_name, wheel_metadata = _metadata(wheel_values, ".dist-info/METADATA")
    version = _validate_metadata(wheel_metadata, expected_version)
    wheel_dist_info = wheel_metadata_name.removesuffix("METADATA")
    _validate_license_bytes(
        wheel_values,
        license_name=f"{wheel_dist_info}licenses/LICENSE",
        notice_name=f"{wheel_dist_info}licenses/NOTICE",
        root=root,
    )
    unexpected_wheel_roots = {
        PurePosixPath(name).parts[0]
        for name in wheel_names
        if PurePosixPath(name).parts[0] != "dploydb"
        and not PurePosixPath(name).parts[0].endswith(".dist-info")
    }
    if unexpected_wheel_roots:
        _stop(f"wheel contains unexpected top-level paths: {sorted(unexpected_wheel_roots)!r}")

    sdist_values = _read_sdist(sdist)
    sdist_names = set(sdist_values)
    _validate_names(sdist_names)
    prefix = _sdist_prefix(sdist_names)
    relative_names = {str(PurePosixPath(*PurePosixPath(name).parts[1:])) for name in sdist_names}
    top_level = {PurePosixPath(name).parts[0] for name in relative_names if name != "."}
    missing = REQUIRED_SDIST_TOP_LEVEL - top_level
    unexpected = top_level - ALLOWED_SDIST_TOP_LEVEL
    if missing or unexpected:
        _stop(
            "source-distribution boundary mismatch: "
            f"missing={sorted(missing)!r}, unexpected={sorted(unexpected)!r}"
        )
    _, sdist_metadata = _metadata(sdist_values, "/PKG-INFO")
    _validate_metadata(sdist_metadata, version)
    _validate_license_bytes(
        sdist_values,
        license_name=f"{prefix}/LICENSE",
        notice_name=f"{prefix}/NOTICE",
        root=root,
    )

    summary = {
        "ok": True,
        "version": version,
        "wheel": wheel.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "wheel_files": len(wheel_names),
        "sdist": sdist.name,
        "sdist_sha256": hashlib.sha256(sdist.read_bytes()).hexdigest(),
        "sdist_files": len(sdist_names),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
