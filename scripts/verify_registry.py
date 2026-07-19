"""Verify public index metadata and artifact hashes for one DployDB release."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NoReturn

EXPECTED_URLS = {
    "Homepage": "https://github.com/recursiveway/dployDB",
    "Repository": "https://github.com/recursiveway/dployDB",
    "Issues": "https://github.com/recursiveway/dployDB/issues",
    "Documentation": "https://github.com/recursiveway/dployDB/tree/main/docs",
}
EXPECTED_CLASSIFIER = "Development Status :: 3 - Alpha"
REQUEST_TIMEOUT_SECONDS = 20
CONSISTENCY_TIMEOUT_SECONDS = 120


class RegistryError(RuntimeError):
    """One controlled package-registry verification failure."""


def _stop(message: str) -> NoReturn:
    raise RegistryError(message)


def _fetch(url: str) -> dict[str, Any]:
    deadline = time.monotonic() + CONSISTENCY_TIMEOUT_SECONDS
    last_error = "registry did not answer"
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                selected = json.load(response)
            if not isinstance(selected, dict):
                _stop("registry response was not a JSON object")
            return selected
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(5)
    _stop(f"registry metadata did not become available: {last_error}")


def _local_hashes(directory: Path) -> dict[str, str]:
    files = sorted((*directory.glob("dploydb-*.whl"), *directory.glob("dploydb-*.tar.gz")))
    if len(files) != 2:
        _stop(f"expected one wheel and one source archive in {directory}, found {files!r}")
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--base-url", default="https://pypi.org")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    arguments = parser.parse_args()

    base_url = arguments.base_url.rstrip("/")
    endpoint = f"{base_url}/pypi/dploydb/{arguments.version}/json"
    payload = _fetch(endpoint)
    info = payload.get("info")
    urls = payload.get("urls")
    if not isinstance(info, dict) or not isinstance(urls, list):
        _stop("registry response omitted info or release files")
    if info.get("version") != arguments.version:
        _stop(f"registry returned unexpected version: {info.get('version')!r}")
    if info.get("author") != "RecursiveWay":
        _stop(f"registry returned unexpected author: {info.get('author')!r}")
    if info.get("license_expression") != "Apache-2.0":
        _stop(f"registry returned unexpected license: {info.get('license_expression')!r}")
    if info.get("requires_python") != ">=3.12":
        _stop(f"registry returned unexpected Python requirement: {info.get('requires_python')!r}")
    if EXPECTED_CLASSIFIER not in info.get("classifiers", []):
        _stop("registry metadata omitted the Alpha classifier")
    project_urls = info.get("project_urls")
    if project_urls != EXPECTED_URLS:
        _stop(f"registry returned unexpected project URLs: {project_urls!r}")

    remote_hashes: dict[str, str] = {}
    for value in urls:
        if not isinstance(value, dict):
            _stop("registry release-file entry was not an object")
        filename = value.get("filename")
        digests = value.get("digests")
        if not isinstance(filename, str) or not isinstance(digests, dict):
            _stop("registry release-file entry omitted filename or digests")
        sha256 = digests.get("sha256")
        if not isinstance(sha256, str):
            _stop(f"registry release file omitted SHA-256: {filename!r}")
        remote_hashes[filename] = sha256
    local_hashes = _local_hashes(arguments.dist)
    if remote_hashes != local_hashes:
        _stop(f"registry artifact mismatch: local={local_hashes!r}, remote={remote_hashes!r}")
    print(
        json.dumps(
            {
                "base_url": base_url,
                "files": local_hashes,
                "ok": True,
                "version": arguments.version,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
