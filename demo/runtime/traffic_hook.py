"""Atomic traffic-state hooks for the deterministic end-to-end demo."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_ACTIONS = frozenset({"maintenance-on", "maintenance-off", "activate-new", "activate-old"})


def _load(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read traffic state: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("traffic state must be a JSON object")
    if type(value.get("maintenance")) is not bool:
        raise ValueError("traffic state maintenance must be a boolean")
    if value.get("target") not in {"old", "new"}:
        raise ValueError("traffic state target must be old or new")
    events = value.get("events")
    if not isinstance(events, list):
        raise ValueError("traffic state events must be a list")
    return value


def _write_atomic(path: Path, value: dict[str, object]) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("traffic-state write made no progress")
            written += count
        os.fsync(descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def apply_action(path: Path, action: str) -> None:
    """Apply one idempotent maintenance or routing transition."""
    if action not in _ACTIONS:
        raise ValueError(f"unsupported traffic action: {action}")
    state = _load(path)
    if action == "maintenance-on":
        state["maintenance"] = True
    elif action == "maintenance-off":
        state["maintenance"] = False
    elif action == "activate-new":
        state["target"] = "new"
    else:
        state["target"] = "old"
    events = state["events"]
    assert isinstance(events, list)
    events.append({"action": action, "time_ns": time.time_ns()})
    _write_atomic(path, state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("action", choices=sorted(_ACTIONS))
    arguments = parser.parse_args()
    try:
        apply_action(arguments.state.resolve(), arguments.action)
    except (OSError, ValueError) as exc:
        print(f"traffic hook failed: {exc}", file=sys.stderr)
        return 1
    print(f"traffic hook complete: {arguments.action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
