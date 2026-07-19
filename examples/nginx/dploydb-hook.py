#!/usr/bin/env python3
"""Idempotent fixed-port Nginx traffic hooks for one DployDB application."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

STATE_MODE = 0o600
MARKER_MODE = 0o644
MARKER_CONTENT = b"dploydb-maintenance-v1\n"
ACTIONS = ("maintenance-on", "maintenance-off", "activate-new", "activate-old")


class HookError(Exception):
    """Controlled hook refusal that is safe to show to an operator."""


def _fail(message: str) -> NoReturn:
    raise HookError(message)


def _require_absolute_file(path: Path, label: str) -> Path:
    if not path.is_absolute() or path == Path("/"):
        _fail(f"{label} must be an absolute non-root path")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        _fail(f"{label} parent must be an existing non-symlink directory: {parent}")
    if path.is_symlink():
        _fail(f"{label} must not be a symlink: {path}")
    return path


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_marker(path: Path, *, required: bool) -> bool:
    _require_absolute_file(path, "maintenance marker")
    if not path.exists():
        if required:
            _fail("target activation is allowed only while maintenance mode is enabled")
        return False
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != MARKER_MODE:
        _fail("maintenance marker is not a mode-0644 regular file")
    if path.read_bytes() != MARKER_CONTENT:
        _fail("maintenance marker has unexpected content")
    return True


def _maintenance_on(path: Path) -> None:
    if _read_marker(path, required=False):
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    created = False
    try:
        descriptor = os.open(path, flags, MARKER_MODE)
        created = True
        os.fchmod(descriptor, MARKER_MODE)
        if os.write(descriptor, MARKER_CONTENT) != len(MARKER_CONTENT):
            raise OSError("short maintenance marker write")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _sync_directory(path.parent)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            path.unlink(missing_ok=True)
        _fail(f"maintenance marker could not be created durably: {exc}")


def _maintenance_off(path: Path) -> None:
    if not _read_marker(path, required=False):
        return
    try:
        path.unlink()
        _sync_directory(path.parent)
    except OSError as exc:
        _fail(f"maintenance marker could not be removed durably: {exc}")


def _target_state(path: Path) -> str | None:
    _require_absolute_file(path, "target state")
    if not path.exists():
        return None
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != STATE_MODE:
        _fail("target state is not a mode-0600 regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"target state is unreadable: {exc}")
    if not isinstance(value, dict) or value.get("target") not in {"old", "new"}:
        _fail("target state does not contain an old or new target")
    return str(value["target"])


def _activate(path: Path, maintenance_path: Path, target: str) -> None:
    _read_marker(maintenance_path, required=True)
    _target_state(path)
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "target": target,
                "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = -1
    replaced = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, STATE_MODE)
        os.fchmod(descriptor, STATE_MODE)
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short target-state write")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        replaced = True
        _sync_directory(path.parent)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            temporary.unlink(missing_ok=True)
        _fail(f"target state could not be published durably: {exc}")


def apply_action(*, action: str, state_path: Path, maintenance_path: Path) -> dict[str, object]:
    """Apply one hook action and return bounded evidence derived from durable state."""
    _require_absolute_file(state_path, "target state")
    _require_absolute_file(maintenance_path, "maintenance marker")
    if action == "maintenance-on":
        _maintenance_on(maintenance_path)
    elif action == "maintenance-off":
        _maintenance_off(maintenance_path)
    elif action == "activate-new":
        _activate(state_path, maintenance_path, "new")
    elif action == "activate-old":
        _activate(state_path, maintenance_path, "old")
    else:
        _fail(f"unsupported action: {action}")
    return {
        "ok": True,
        "action": action,
        "maintenance": _read_marker(maintenance_path, required=False),
        "target": _target_state(state_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--maintenance-file", required=True, type=Path)
    parser.add_argument("action", choices=ACTIONS)
    arguments = parser.parse_args()
    try:
        result = apply_action(
            action=arguments.action,
            state_path=arguments.state_file,
            maintenance_path=arguments.maintenance_file,
        )
    except HookError as exc:
        print(f"dploydb nginx hook refused: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("dploydb nginx hook failed unexpectedly; inspect host logs", file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
