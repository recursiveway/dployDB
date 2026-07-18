"""Strict release manifest loading for the deterministic demo."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast


class ReleaseDefinitionError(ValueError):
    """Raised when a demo release definition is invalid."""


@dataclass(frozen=True, slots=True)
class SchemaSpec:
    """The schema transition declared by a release."""

    from_version: int
    to_version: int


@dataclass(frozen=True, slots=True)
class HealthSpec:
    """The deterministic health behavior for a release."""

    mode: str
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class ReleaseDefinition:
    """A validated release definition and its source directory."""

    name: str
    directory: Path
    schema: SchemaSpec
    health: HealthSpec


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseDefinitionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReleaseDefinitionError(f"{location} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise ReleaseDefinitionError(f"{location} keys must be strings")
    return cast(dict[str, object], value)


def _require_exact_keys(value: dict[str, object], expected: set[str], location: str) -> None:
    actual = set(value)
    if actual == expected:
        return

    details: list[str] = []
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        details.append(f"missing keys: {', '.join(missing)}")
    if unknown:
        details.append(f"unknown keys: {', '.join(unknown)}")
    raise ReleaseDefinitionError(f"{location} has invalid keys ({'; '.join(details)})")


def _require_string(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise ReleaseDefinitionError(f"{location} must be a string")
    if not value.strip():
        raise ReleaseDefinitionError(f"{location} must be nonempty")
    return value


def _require_version(value: object, location: str) -> int:
    if type(value) is not int:
        raise ReleaseDefinitionError(f"{location} must be an integer")
    if value < 0:
        raise ReleaseDefinitionError(f"{location} must be non-negative")
    return value


def load_release(path: Path) -> ReleaseDefinition:
    """Load and strictly validate ``release.json`` from a release directory."""

    directory = path.resolve()
    if not directory.is_dir():
        raise ReleaseDefinitionError(f"release directory does not exist: {path}")

    manifest_path = directory / "release.json"
    if not manifest_path.is_file():
        raise ReleaseDefinitionError(f"release manifest does not exist: {manifest_path}")

    try:
        raw = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except ReleaseDefinitionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseDefinitionError(f"cannot read release manifest: {exc}") from exc

    manifest = _require_object(raw, "release manifest")
    _require_exact_keys(manifest, {"name", "schema", "health"}, "release manifest")

    name = _require_string(manifest["name"], "release name")
    if name != directory.name:
        raise ReleaseDefinitionError(
            f"release name {name!r} does not match directory {directory.name!r}"
        )

    schema_value = _require_object(manifest["schema"], "schema")
    _require_exact_keys(schema_value, {"from_version", "to_version"}, "schema")
    from_version = _require_version(schema_value["from_version"], "schema.from_version")
    to_version = _require_version(schema_value["to_version"], "schema.to_version")
    if to_version <= from_version:
        raise ReleaseDefinitionError("schema.to_version must be greater than schema.from_version")

    health_value = _require_object(manifest["health"], "health")
    mode_value = health_value.get("mode")
    if mode_value == "ok":
        _require_exact_keys(health_value, {"mode"}, "health")
        health = HealthSpec(mode="ok", failure_reason=None)
    elif mode_value == "broken":
        _require_exact_keys(health_value, {"mode", "failure_reason"}, "health")
        failure_reason = _require_string(health_value["failure_reason"], "health.failure_reason")
        health = HealthSpec(mode="broken", failure_reason=failure_reason)
    else:
        raise ReleaseDefinitionError("health.mode must be 'ok' or 'broken'")

    return ReleaseDefinition(
        name=name,
        directory=directory,
        schema=SchemaSpec(from_version=from_version, to_version=to_version),
        health=health,
    )
