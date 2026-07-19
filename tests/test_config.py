"""Tests for Milestone 1C strict configuration and safe initialization."""

from __future__ import annotations

import os
import socket
import sqlite3
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from dploydb.config import (
    CONFIG_FILE_MODE,
    STARTER_CONFIGURATION,
    DployDBConfig,
    initialize_configuration,
    load_configuration,
    parse_configuration,
    require_deploy_topology,
    resolve_configuration,
)
from dploydb.errors import ConfigurationError, ExitCode
from dploydb.redaction import REDACTION_MARKER, SecretRegistry


def valid_mapping() -> dict[str, Any]:
    value = yaml.safe_load(STARTER_CONFIGURATION)
    assert isinstance(value, dict)
    return value


def render(value: dict[str, Any]) -> str:
    return yaml.safe_dump(value, sort_keys=False)


def nested_set(value: dict[str, Any], path: tuple[str, ...], replacement: Any) -> None:
    target: dict[str, Any] = value
    for part in path[:-1]:
        nested = target[part]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = replacement


def remove_field(value: dict[str, Any], path: tuple[str, ...]) -> None:
    target: dict[str, Any] = value
    for part in path[:-1]:
        nested = target[part]
        assert isinstance(nested, dict)
        target = nested
    del target[path[-1]]


def assert_configuration_error(text: str, match: str | None = None) -> ConfigurationError:
    with pytest.raises(ConfigurationError, match=match) as captured:
        parse_configuration(text)
    assert captured.value.exit_code is ExitCode.CONFIGURATION
    assert captured.value.payload.production_changed is False
    assert captured.value.payload.previous_application_running is None
    assert captured.value.payload.recovery_required is False
    return captured.value


def test_starter_configuration_is_strictly_valid() -> None:
    structural = parse_configuration(STARTER_CONFIGURATION)
    resolved = resolve_configuration(
        structural,
        environment={},
        secrets=SecretRegistry(),
    )

    assert isinstance(structural, DployDBConfig)
    assert resolved.project == "example-app"
    assert resolved.state_directory == Path("/srv/example/.dploydb")
    assert resolved.database.path == Path("/srv/example/data/app.db")
    assert resolved.migration.command == ("python", "scripts/migrate.py")
    assert resolved.application.production_project == "example-app"
    assert resolved.application.production_port == 4510
    assert resolved.application.production_health_url == "http://127.0.0.1:4510/health"
    assert resolved.application.candidate_container_port == 8080
    assert resolved.application.database_volume_target == "/data"
    assert resolved.application.smoke_command == ("python", "scripts/smoke_test.py")
    assert resolved.application.test_mode_env == {"DPLOYDB_TEST_MODE": "1"}
    assert resolved.traffic.timeout_seconds == 30
    assert resolved.backup.remote is not None
    assert resolved.backup.remote.enabled is False
    assert resolved.backup.remote.required is False
    assert resolved.backup.remote.region_name == "auto"
    assert resolved.backup.remote.storage_class == "STANDARD"
    assert resolved.backup.remote.timeout_seconds == 30
    assert resolved.backup.remote.max_attempts == 3


@pytest.mark.parametrize(
    ("path", "replacement", "message"),
    (
        (("project",), 42, "project"),
        (("project",), "bad project", "project"),
        (("state_directory",), "relative/state", "absolute path"),
        (("state_directory",), "/", "filesystem root"),
        (("database", "path"), "data/app.db", "absolute path"),
        (("database", "path_env"), "NOT-AN-ENV", "environment-variable name"),
        (("database", "minimum_free_space_multiplier"), 0, "greater than 0"),
        (("database", "minimum_free_space_multiplier"), "3", "valid number"),
        (("migration", "command"), [], "at least one argument"),
        (("migration", "command"), "python migrate.py", "argument array"),
        (("migration", "command"), ["python", 42], "arguments must be strings"),
        (("migration", "timeout_seconds"), 0, "greater than 0"),
        (("migration", "timeout_seconds"), "120", "valid integer"),
        (("application", "runner"), "custom", "docker_compose"),
        (("application", "compose_file"), "compose.yaml", "absolute path"),
        (("application", "service"), " ", "must not be empty"),
        (("application", "production_project"), "bad project!", "Compose service name"),
        (("application", "production_port"), 0, "greater than or equal to 1"),
        (
            ("application", "production_health_url"),
            "https://127.0.0.1:4510/health",
            "must use http",
        ),
        (
            ("application", "production_health_url"),
            "http://example.com:4510/health",
            "loopback host",
        ),
        (
            ("application", "production_health_url"),
            "http://127.0.0.1:9999/health",
            "port must match",
        ),
        (("application", "candidate_port"), 0, "greater than or equal to 1"),
        (("application", "candidate_port"), 65536, "less than or equal to 65535"),
        (("application", "candidate_port"), "4511", "valid integer"),
        (("application", "candidate_container_port"), 0, "greater than or equal to 1"),
        (("application", "candidate_container_port"), "8080", "valid integer"),
        (("application", "database_volume_target"), "data", "absolute non-root"),
        (("application", "database_volume_target"), "/", "absolute non-root"),
        (("application", "database_volume_target"), "/data/../prod", "traversal"),
        (("application", "database_volume_target"), "/data/./nested", "normalized"),
        (("application", "database_volume_target"), "//data", "normalized"),
        (("application", "database_volume_target"), "/data:unsafe", "colon"),
        (
            ("application", "candidate_health_url"),
            "https://127.0.0.1:4511/health",
            "must use http",
        ),
        (
            ("application", "candidate_health_url"),
            "http://example.com:4511/health",
            "loopback host",
        ),
        (
            ("application", "candidate_health_url"),
            "http://127.0.0.1:9999/health",
            "port must match",
        ),
        (
            ("application", "candidate_health_url"),
            "http://user:private@127.0.0.1:4511/health",
            "must not contain credentials",
        ),
        (
            ("application", "candidate_health_url"),
            "http://127.0.0.1:4511/health?token=private",
            "query or fragment",
        ),
        (("application", "startup_timeout_seconds"), -1, "greater than 0"),
        (("application", "smoke_command"), [], "at least one argument"),
        (("application", "test_mode_env"), [], "must be a mapping"),
        (("application", "test_mode_env"), {"BAD-NAME": "1"}, "invalid environment"),
        (("application", "test_mode_env"), {"GOOD_NAME": 1}, "valid string"),
        (
            ("application", "test_mode_env"),
            {"DPLOYDB_VERSION": "unsafe"},
            "reserved environment variable",
        ),
        (("traffic", "maintenance_on_command"), [], "at least one argument"),
        (("traffic", "activate_new_command"), "activate candidate", "argument array"),
        (("traffic", "timeout_seconds"), 0, "greater than 0"),
        (("backup", "local_directory"), "backups", "absolute path"),
        (("backup", "keep_last"), 0, "greater than 0"),
        (("backup", "keep_last"), "10", "valid integer"),
        (("backup", "remote", "provider"), "gcs", "s3"),
        (("backup", "remote", "prefix"), "/absolute", "relative object prefix"),
        (("backup", "remote", "prefix"), "safe/../unsafe", "normalized"),
        (("backup", "remote", "prefix"), "safe//unsafe", "normalized"),
        (("backup", "remote", "bucket"), "INVALID_BUCKET", "bucket name"),
        (("backup", "remote", "endpoint_url"), "http://example.com", "HTTPS"),
        (("backup", "remote", "endpoint_url"), "https://example.com/path", "object path"),
        (("backup", "remote", "access_key_env"), "BAD-ENV", "environment-variable"),
        (("backup", "remote", "timeout_seconds"), 0, "greater than 0"),
        (("backup", "remote", "max_attempts"), 0, "greater than 0"),
    ),
)
def test_invalid_field_families_are_rejected(
    path: tuple[str, ...], replacement: Any, message: str
) -> None:
    value = valid_mapping()
    nested_set(value, path, replacement)

    error = assert_configuration_error(render(value))

    assert message in error.payload.what_failed
    assert "private" not in error.payload.what_failed


@pytest.mark.parametrize(
    "path",
    (
        ("project",),
        ("database", "path"),
        ("migration", "command"),
        ("application", "candidate_health_url"),
        ("traffic", "activate_old_command"),
        ("backup", "keep_last"),
    ),
)
def test_missing_required_field_families_are_rejected(path: tuple[str, ...]) -> None:
    value = valid_mapping()
    remove_field(value, path)

    assert_configuration_error(render(value), match="validation failed")


@pytest.mark.parametrize(
    "path",
    (
        (),
        ("database",),
        ("migration",),
        ("application",),
        ("traffic",),
        ("backup",),
        ("backup", "remote"),
    ),
)
def test_unknown_keys_are_rejected_at_every_model_level(path: tuple[str, ...]) -> None:
    value = valid_mapping()
    target = value
    for part in path:
        nested = target[part]
        assert isinstance(nested, dict)
        target = nested
    target["unexpected_option"] = True

    error = assert_configuration_error(render(value))

    assert "unexpected_option" in error.payload.what_failed
    assert "Extra inputs are not permitted" in error.payload.what_failed


def test_duplicate_keys_are_rejected_at_root_and_nested_levels() -> None:
    root_duplicate = STARTER_CONFIGURATION + "project: overwritten\n"
    nested_duplicate = STARTER_CONFIGURATION.replace(
        "  timeout_seconds: 120\n",
        "  timeout_seconds: 120\n  timeout_seconds: 999\n",
    )

    for text in (root_duplicate, nested_duplicate):
        error = assert_configuration_error(text)
        assert "duplicate YAML key" in error.payload.what_failed


@pytest.mark.parametrize("text", ("", "[]", "project: [", "---\n{}\n---\n{}\n"))
def test_empty_non_mapping_and_invalid_yaml_documents_are_rejected(text: str) -> None:
    assert_configuration_error(text)


def test_enabled_remote_requires_credential_references_and_bucket() -> None:
    value = valid_mapping()
    remote = value["backup"]["remote"]
    assert isinstance(remote, dict)
    remote["enabled"] = True
    for field in ("bucket", "access_key_env", "secret_key_env"):
        remote.pop(field)

    error = assert_configuration_error(render(value))

    assert "enabled remote backup requires bucket, access_key_env, secret_key_env" in (
        error.payload.what_failed
    )


def test_required_remote_must_be_enabled() -> None:
    value = valid_mapping()
    remote = value["backup"]["remote"]
    assert isinstance(remote, dict)
    remote["required"] = True

    error = assert_configuration_error(render(value))

    assert "required remote backup must also be enabled" in error.payload.what_failed


def test_remote_endpoint_value_and_environment_reference_are_mutually_exclusive() -> None:
    value = valid_mapping()
    remote = value["backup"]["remote"]
    assert isinstance(remote, dict)
    remote["endpoint_url"] = "https://example.r2.cloudflarestorage.com"

    error = assert_configuration_error(render(value))

    assert "endpoint_url and endpoint_url_env are mutually exclusive" in error.payload.what_failed


def test_valid_enabled_remote_configuration_parses() -> None:
    value = valid_mapping()
    remote = value["backup"]["remote"]
    assert isinstance(remote, dict)
    remote.pop("endpoint_url_env")
    remote.update(
        {
            "enabled": True,
            "required": True,
            "bucket": "verified-backups",
            "prefix": "dploydb/example",
            "endpoint_url": "https://example.r2.cloudflarestorage.com",
            "access_key_env": "S3_ACCESS_KEY_ID",
            "secret_key_env": "S3_SECRET_ACCESS_KEY",
            "region_name": "auto",
            "storage_class": "STANDARD",
            "timeout_seconds": 20,
            "max_attempts": 4,
        }
    )

    config = parse_configuration(render(value))

    assert config.backup.remote is not None
    assert config.backup.remote.enabled is True
    assert config.backup.remote.required is True
    assert config.backup.remote.bucket == "verified-backups"
    assert config.backup.remote.endpoint_url == "https://example.r2.cloudflarestorage.com"
    assert config.backup.remote.region_name == "auto"


def test_candidate_container_settings_have_backward_compatible_defaults() -> None:
    value = valid_mapping()
    remove_field(value, ("application", "candidate_container_port"))
    remove_field(value, ("application", "database_volume_target"))

    config = parse_configuration(render(value))

    assert config.application.candidate_container_port == 8080
    assert config.application.database_volume_target == "/data"


def test_pre_milestone5_configuration_parses_but_deploy_topology_is_required() -> None:
    value = valid_mapping()
    for field in ("production_project", "production_port", "production_health_url"):
        remove_field(value, ("application", field))
    remove_field(value, ("traffic", "timeout_seconds"))

    config = parse_configuration(render(value))

    assert config.application.production_project is None
    assert config.application.production_port is None
    assert config.application.production_health_url is None
    assert config.traffic.timeout_seconds == 30
    with pytest.raises(ConfigurationError, match="deployment requires") as captured:
        require_deploy_topology(config)
    assert captured.value.payload.production_changed is False


@pytest.mark.parametrize(
    "missing",
    ("production_project", "production_port", "production_health_url"),
)
def test_partial_production_topology_is_rejected(missing: str) -> None:
    value = valid_mapping()
    remove_field(value, ("application", missing))

    error = assert_configuration_error(render(value))

    assert "must be configured together" in error.payload.what_failed


def test_candidate_and_production_ports_must_be_distinct() -> None:
    value = valid_mapping()
    nested_set(value, ("application", "production_port"), 4511)
    nested_set(
        value,
        ("application", "production_health_url"),
        "http://127.0.0.1:4511/health",
    )

    error = assert_configuration_error(render(value))

    assert "production_port must differ from candidate_port" in error.payload.what_failed


def test_complete_deploy_topology_is_returned_without_operational_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = parse_configuration(STARTER_CONFIGURATION)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("deploy topology validation performed operational access")

    monkeypatch.setattr(Path, "exists", forbidden)
    monkeypatch.setattr(Path, "is_file", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)

    topology = require_deploy_topology(config)

    assert topology.compose_project == "example-app"
    assert topology.host_port == 4510
    assert topology.health_url == "http://127.0.0.1:4510/health"


def test_database_path_environment_cannot_override_reserved_candidate_version() -> None:
    value = valid_mapping()
    nested_set(value, ("database", "path_env"), "DPLOYDB_VERSION")

    error = assert_configuration_error(render(value))

    assert "reserved environment variable DPLOYDB_VERSION" in error.payload.what_failed


@pytest.mark.parametrize(
    "bad_value",
    ("${BAD-NAME}", "${UNCLOSED", "prefix ${ALSO.BAD}"),
)
def test_invalid_interpolation_syntax_is_rejected_structurally(bad_value: str) -> None:
    value = valid_mapping()
    nested_set(value, ("application", "service"), bad_value)

    assert_configuration_error(render(value), match="invalid environment interpolation")


def test_environment_interpolation_resolves_paths_and_registers_secrets() -> None:
    value = valid_mapping()
    nested_set(value, ("project",), "${PROJECT_NAME}")
    nested_set(value, ("state_directory",), "${DEPLOY_ROOT}/state")
    nested_set(value, ("database", "path"), "${DEPLOY_ROOT}/data/app.db")
    nested_set(value, ("application", "compose_file"), "${DEPLOY_ROOT}/compose.yaml")
    nested_set(
        value,
        ("application", "candidate_health_url"),
        "http://127.0.0.1:${CANDIDATE_PORT}/health",
    )
    nested_set(value, ("backup", "local_directory"), "${DEPLOY_ROOT}/backups")
    nested_set(
        value,
        ("application", "test_mode_env"),
        {"API_TOKEN": "Bearer ${DEPLOY_API_TOKEN}", "SAFE_VALUE": "${VISIBLE_VALUE}"},
    )
    registry = SecretRegistry()
    structural = parse_configuration(render(value))

    resolved = resolve_configuration(
        structural,
        environment={
            "PROJECT_NAME": "configured-project",
            "DEPLOY_ROOT": "/srv/configured",
            "CANDIDATE_PORT": "4511",
            "DEPLOY_API_TOKEN": "top-secret-token",
            "VISIBLE_VALUE": "not-sensitive",
        },
        secrets=registry,
    )

    assert resolved.project == "configured-project"
    assert resolved.state_directory == Path("/srv/configured/state")
    assert resolved.database.path == Path("/srv/configured/data/app.db")
    assert resolved.application.candidate_health_url == "http://127.0.0.1:4511/health"
    assert resolved.application.test_mode_env == {
        "API_TOKEN": "Bearer top-secret-token",
        "SAFE_VALUE": "not-sensitive",
    }
    assert registry.redact_text("top-secret-token") == REDACTION_MARKER
    assert registry.redact_text("not-sensitive") == "not-sensitive"
    assert "top-secret-token" not in repr(registry)
    assert "top-secret-token" not in repr(resolved)
    assert "top-secret-token" not in str(resolved.application)


def test_all_missing_environment_variables_are_reported_without_partial_secret_registration() -> (
    None
):
    value = valid_mapping()
    nested_set(value, ("project",), "${PROJECT_NAME}")
    nested_set(
        value,
        ("application", "test_mode_env"),
        {"API_TOKEN": "${API_TOKEN}"},
    )
    registry = SecretRegistry()
    structural = parse_configuration(render(value))

    with pytest.raises(ConfigurationError) as captured:
        resolve_configuration(
            structural,
            environment={"API_TOKEN": "must-not-register-on-failure"},
            secrets=registry,
        )

    assert "PROJECT_NAME" in captured.value.payload.what_failed
    assert "must-not-register-on-failure" not in captured.value.payload.what_failed
    assert registry.redact_text("must-not-register-on-failure") == ("must-not-register-on-failure")


@pytest.mark.parametrize(
    ("environment", "message"),
    (
        ({"DEPLOY_ROOT": "relative"}, "absolute path"),
        ({"HEALTH_URL": "http://example.com:4511/health"}, "loopback host"),
    ),
)
def test_resolved_values_are_revalidated(environment: dict[str, str], message: str) -> None:
    value = valid_mapping()
    if "DEPLOY_ROOT" in environment:
        nested_set(value, ("state_directory",), "${DEPLOY_ROOT}/state")
    else:
        nested_set(value, ("application", "candidate_health_url"), "${HEALTH_URL}")
    structural = parse_configuration(render(value))

    with pytest.raises(ConfigurationError) as captured:
        resolve_configuration(
            structural,
            environment=environment,
            secrets=SecretRegistry(),
        )

    assert message in captured.value.payload.what_failed


def test_invalid_structural_configuration_performs_no_operational_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., None]:
        def record(*_args: object, **_kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"unexpected operational access: {name}")

        return record

    monkeypatch.setattr(Path, "exists", forbidden("path.exists"))
    monkeypatch.setattr(Path, "is_file", forbidden("path.is_file"))
    monkeypatch.setattr(Path, "mkdir", forbidden("path.mkdir"))
    monkeypatch.setattr(sqlite3, "connect", forbidden("sqlite.connect"))
    monkeypatch.setattr(subprocess, "run", forbidden("subprocess.run"))
    monkeypatch.setattr(socket, "socket", forbidden("socket.socket"))
    value = valid_mapping()
    nested_set(value, ("database", "path"), "relative.db")

    assert_configuration_error(render(value))

    assert calls == []


def test_load_configuration_reads_and_resolves_only_the_selected_file(tmp_path: Path) -> None:
    config_path = tmp_path / "selected.yaml"
    config_path.write_text(
        STARTER_CONFIGURATION.replace("project: example-app", "project: ${PROJECT_NAME}"),
        encoding="utf-8",
    )

    loaded = load_configuration(config_path, environment={"PROJECT_NAME": "loaded-project"})

    assert loaded.config.project == "loaded-project"
    assert isinstance(loaded.secrets, SecretRegistry)


def test_shipped_demo_configuration_matches_the_strict_contract() -> None:
    repository_root = Path(__file__).resolve().parent.parent

    loaded = load_configuration(repository_root / "demo" / "dploydb.yaml", environment={})

    assert loaded.config.project == "dploydb-demo"


def test_load_configuration_reports_an_unreadable_path_without_traceback(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"

    with pytest.raises(ConfigurationError) as captured:
        load_configuration(missing, environment={})

    assert str(missing) in captured.value.payload.what_failed
    assert captured.value.exit_code is ExitCode.CONFIGURATION


def test_initialize_creates_mode_0600_configuration_that_loads(tmp_path: Path) -> None:
    config_path = tmp_path / "dploydb.yaml"

    created = initialize_configuration(config_path)

    assert created == config_path
    assert stat.S_IMODE(config_path.stat().st_mode) == CONFIG_FILE_MODE
    assert config_path.read_text(encoding="utf-8") == STARTER_CONFIGURATION
    loaded = load_configuration(config_path, environment={})
    assert loaded.config.project == "example-app"


def test_initialize_preserves_an_existing_file_byte_for_byte_and_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "dploydb.yaml"
    original = b"owner: existing\nprivate: preserve-me\n"
    config_path.write_bytes(original)
    config_path.chmod(0o640)

    with pytest.raises(ConfigurationError, match="already exists") as captured:
        initialize_configuration(config_path)

    assert config_path.read_bytes() == original
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    assert "preserve-me" not in captured.value.payload.what_failed


def test_initialize_does_not_create_missing_parent_directories(tmp_path: Path) -> None:
    config_path = tmp_path / "missing" / "dploydb.yaml"

    with pytest.raises(ConfigurationError, match="could not be created"):
        initialize_configuration(config_path)

    assert not config_path.parent.exists()


def test_initialize_removes_a_file_when_writing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "dploydb.yaml"

    def fail_write(_descriptor: int, _data: bytes) -> int:
        raise OSError("injected write failure")

    monkeypatch.setattr(os, "write", fail_write)

    with pytest.raises(ConfigurationError, match="could not be written"):
        initialize_configuration(config_path)

    assert not config_path.exists()
