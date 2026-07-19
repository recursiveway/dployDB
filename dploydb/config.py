"""Strict, side-effect-free configuration parsing for DployDB.

Configuration handling is deliberately split into phases. Structural parsing does
not inspect the host. Environment interpolation and secret registration happen in
a separate in-memory phase. Filesystem, database, socket, Docker, and executable
checks belong to ``doctor`` in Milestone 1G.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Final, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from dploydb.errors import ConfigurationError
from dploydb.redaction import SecretRegistry, is_sensitive_key

DEFAULT_CONFIG_PATH = Path("dploydb.yaml")
CONFIG_FILE_MODE = 0o600

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_COMPOSE_SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_S3_BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_LOCAL_HEALTH_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
_RESERVED_CANDIDATE_ENVIRONMENT: Final[frozenset[str]] = frozenset({"DPLOYDB_VERSION"})


def _configuration_error(what_failed: str) -> ConfigurationError:
    return ConfigurationError(
        what_failed,
        production_changed=False,
        previous_application_running=None,
        next_safe_action="Correct the configuration and run the command again.",
    )


def _allow_unresolved(info: ValidationInfo) -> bool:
    context = info.context
    return bool(context and context.get("allow_unresolved_environment"))


def _contains_interpolation(value: str) -> bool:
    return _INTERPOLATION.search(value) is not None


def _non_empty_text(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    if "\x00" in value:
        raise ValueError("must not contain a NUL byte")
    return value


def _absolute_path(value: object, info: ValidationInfo) -> Path:
    if not isinstance(value, str):
        raise ValueError("must be an absolute path string")
    _non_empty_text(value)
    if _allow_unresolved(info) and _contains_interpolation(value):
        return Path(value)

    path = Path(value)
    if not path.is_absolute():
        raise ValueError("must be an absolute path")
    if path == Path("/"):
        raise ValueError("must not be the filesystem root")
    return path


def _argument_array(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("must be a YAML argument array")
    if not value:
        raise ValueError("must contain at least one argument")
    arguments: list[str] = []
    for argument in value:
        if not isinstance(argument, str):
            raise ValueError("arguments must be strings")
        arguments.append(_non_empty_text(argument))
    return tuple(arguments)


NonEmptyText = Annotated[str, Field(strict=True), AfterValidator(_non_empty_text)]
AbsolutePath = Annotated[Path, BeforeValidator(_absolute_path)]
ArgumentArray = Annotated[tuple[str, ...], BeforeValidator(_argument_array)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
PositiveNumber = Annotated[float, Field(strict=True, gt=0)]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]


class StrictConfigModel(BaseModel):
    """Base model that rejects typos and implicit type coercion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def __repr__(self) -> str:
        """Avoid exposing interpolated values through incidental diagnostics."""
        return f"{type(self).__name__}(<configuration values hidden>)"

    def __str__(self) -> str:
        """Avoid exposing interpolated values through incidental diagnostics."""
        return repr(self)


class DatabaseConfig(StrictConfigModel):
    """SQLite database location and migration environment contract."""

    path: AbsolutePath
    path_env: NonEmptyText
    minimum_free_space_multiplier: PositiveNumber = 3.0

    @field_validator("path_env")
    @classmethod
    def validate_path_environment_name(cls, value: str) -> str:
        if _ENVIRONMENT_NAME.fullmatch(value) is None:
            raise ValueError("must be a valid environment-variable name")
        return value


class MigrationConfig(StrictConfigModel):
    """Developer-supplied migration command and its mandatory timeout."""

    command: ArgumentArray
    timeout_seconds: PositiveInt


def _validate_local_health_url(
    value: str,
    *,
    expected_port: int,
    field_name: str,
    port_name: str,
    info: ValidationInfo,
) -> None:
    if _allow_unresolved(info) and _contains_interpolation(value):
        return
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid local HTTP URL") from exc

    if parsed.scheme != "http":
        raise ValueError(f"{field_name} must use http")
    if parsed.hostname is None or parsed.hostname.lower() not in _LOCAL_HEALTH_HOSTS:
        raise ValueError(f"{field_name} must use a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain credentials")
    if parsed_port is None:
        raise ValueError(f"{field_name} must include the {port_name}")
    if parsed_port != expected_port:
        raise ValueError(f"{field_name} port must match {port_name}")
    if not parsed.path.startswith("/"):
        raise ValueError(f"{field_name} must contain an absolute URL path")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must not contain a query or fragment")


class ApplicationConfig(StrictConfigModel):
    """Single Docker Compose application and isolated candidate settings."""

    runner: Literal["docker_compose"]
    compose_file: AbsolutePath
    service: NonEmptyText
    production_project: NonEmptyText | None = None
    production_port: Port | None = None
    production_health_url: NonEmptyText | None = None
    candidate_port: Port
    candidate_container_port: Port = 8080
    database_volume_target: NonEmptyText = "/data"
    candidate_health_url: NonEmptyText
    startup_timeout_seconds: PositiveInt
    smoke_command: ArgumentArray | None = None
    test_mode_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("test_mode_env", mode="before")
    @classmethod
    def validate_test_environment_shape(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("must be a mapping of environment names to string values")
        return value

    @field_validator("test_mode_env")
    @classmethod
    def validate_test_environment(cls, value: dict[str, str]) -> dict[str, str]:
        for name, environment_value in value.items():
            if _ENVIRONMENT_NAME.fullmatch(name) is None:
                raise ValueError("contains an invalid environment-variable name")
            if name in _RESERVED_CANDIDATE_ENVIRONMENT:
                raise ValueError(f"must not override reserved environment variable {name}")
            _non_empty_text(environment_value)
        return value

    @field_validator("service", "production_project")
    @classmethod
    def validate_compose_name(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        if _allow_unresolved(info) and _contains_interpolation(value):
            return value
        if _COMPOSE_SERVICE_NAME.fullmatch(value) is None:
            raise ValueError("must be a safe Docker Compose service name")
        return value

    @field_validator("database_volume_target")
    @classmethod
    def validate_database_volume_target(cls, value: str, info: ValidationInfo) -> str:
        if _allow_unresolved(info) and _contains_interpolation(value):
            return value
        path = PurePosixPath(value)
        if not path.is_absolute() or path == PurePosixPath("/"):
            raise ValueError("must be an absolute non-root container directory")
        if str(path) != value or value.startswith("//"):
            raise ValueError("must be a normalized container path")
        if any(part in {".", ".."} for part in path.parts):
            raise ValueError("must not contain traversal segments")
        if ":" in value:
            raise ValueError("must not contain a colon")
        return value

    @model_validator(mode="after")
    def validate_health_topology(self, info: ValidationInfo) -> Self:
        _validate_local_health_url(
            self.candidate_health_url,
            expected_port=self.candidate_port,
            field_name="candidate_health_url",
            port_name="candidate_port",
            info=info,
        )

        production_values = (
            self.production_project,
            self.production_port,
            self.production_health_url,
        )
        if all(value is None for value in production_values):
            return self
        if any(value is None for value in production_values):
            raise ValueError(
                "production_project, production_port, and production_health_url "
                "must be configured together"
            )
        assert self.production_port is not None
        assert self.production_health_url is not None
        if self.production_port == self.candidate_port:
            raise ValueError("production_port must differ from candidate_port")
        _validate_local_health_url(
            self.production_health_url,
            expected_port=self.production_port,
            field_name="production_health_url",
            port_name="production_port",
            info=info,
        )
        return self


class TrafficConfig(StrictConfigModel):
    """Bounded command hooks used by the later cutover milestone."""

    maintenance_on_command: ArgumentArray
    maintenance_off_command: ArgumentArray
    activate_new_command: ArgumentArray
    activate_old_command: ArgumentArray
    timeout_seconds: PositiveInt = 30


class RemoteBackupConfig(StrictConfigModel):
    """Optional S3-compatible backup configuration."""

    enabled: bool = False
    required: bool = False
    provider: Literal["s3"] = "s3"
    bucket: NonEmptyText | None = None
    prefix: str = ""
    region_name: NonEmptyText = "auto"
    storage_class: Literal["STANDARD", "STANDARD_IA"] = "STANDARD"
    endpoint_url: NonEmptyText | None = None
    endpoint_url_env: NonEmptyText | None = None
    access_key_env: NonEmptyText | None = None
    secret_key_env: NonEmptyText | None = None
    session_token_env: NonEmptyText | None = None
    timeout_seconds: PositiveInt = 30
    max_attempts: PositiveInt = 3

    @field_validator(
        "endpoint_url_env",
        "access_key_env",
        "secret_key_env",
        "session_token_env",
    )
    @classmethod
    def validate_environment_name(cls, value: str | None) -> str | None:
        if value is not None and _ENVIRONMENT_NAME.fullmatch(value) is None:
            raise ValueError("must be a valid environment-variable name")
        return value

    @field_validator("bucket")
    @classmethod
    def validate_bucket(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None or (_allow_unresolved(info) and _contains_interpolation(value)):
            return value
        if _S3_BUCKET_NAME.fullmatch(value) is None or ".." in value:
            raise ValueError("must be a valid lowercase S3-compatible bucket name")
        return value

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None or (_allow_unresolved(info) and _contains_interpolation(value)):
            return value
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("must be a valid S3-compatible endpoint URL") from exc
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("must be an HTTP or HTTPS endpoint URL")
        if parsed.scheme == "http" and parsed.hostname.lower() not in _LOCAL_HEALTH_HOSTS:
            raise ValueError("must use HTTPS unless the endpoint host is loopback")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("must not contain a query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("must not contain an object path")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("contains an invalid port")
        return value.rstrip("/")

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str, info: ValidationInfo) -> str:
        if _allow_unresolved(info) and _contains_interpolation(value):
            return value
        if value.startswith("/"):
            raise ValueError("must be a relative object prefix")
        if value and any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("must be a normalized relative object prefix")
        if "\x00" in value:
            raise ValueError("must not contain a NUL byte")
        if len(value.encode("utf-8")) > 768:
            raise ValueError("must not exceed 768 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def validate_enabled_remote(self) -> Self:
        if self.required and not self.enabled:
            raise ValueError("required remote backup must also be enabled")
        if self.endpoint_url is not None and self.endpoint_url_env is not None:
            raise ValueError("endpoint_url and endpoint_url_env are mutually exclusive")
        if not self.enabled:
            return self
        missing = [
            name
            for name, value in (
                ("bucket", self.bucket),
                ("access_key_env", self.access_key_env),
                ("secret_key_env", self.secret_key_env),
            )
            if value is None
        ]
        if missing:
            raise ValueError("enabled remote backup requires " + ", ".join(missing))
        return self


class BackupConfig(StrictConfigModel):
    """Verified local backup retention and optional remote target."""

    local_directory: AbsolutePath
    keep_last: PositiveInt
    remote: RemoteBackupConfig | None = None


class DployDBConfig(StrictConfigModel):
    """Complete supported hackathon configuration contract."""

    project: NonEmptyText
    state_directory: AbsolutePath
    database: DatabaseConfig
    migration: MigrationConfig
    application: ApplicationConfig
    traffic: TrafficConfig
    backup: BackupConfig

    @field_validator("project")
    @classmethod
    def validate_project(cls, value: str, info: ValidationInfo) -> str:
        if _allow_unresolved(info) and _contains_interpolation(value):
            return value
        if _PROJECT_NAME.fullmatch(value) is None:
            raise ValueError(
                "must be 1-64 letters, digits, dots, underscores, or hyphens and "
                "start with a letter or digit"
            )
        return value

    @model_validator(mode="after")
    def validate_reserved_candidate_environment(self) -> Self:
        if self.database.path_env in _RESERVED_CANDIDATE_ENVIRONMENT:
            raise ValueError(
                "database.path_env must not use reserved environment variable DPLOYDB_VERSION"
            )
        return self


@dataclass(frozen=True, slots=True)
class LoadedConfiguration:
    """Resolved configuration paired with its non-serializable secret registry."""

    config: DployDBConfig
    secrets: SecretRegistry


@dataclass(frozen=True, slots=True)
class ProductionTopology:
    """Deploy-only production application topology proven by configuration."""

    compose_project: str
    host_port: int
    health_url: str


def require_deploy_topology(config: DployDBConfig) -> ProductionTopology:
    """Return complete deploy topology or fail before any operational access."""
    application = config.application
    if (
        application.production_project is None
        or application.production_port is None
        or application.production_health_url is None
    ):
        raise _configuration_error(
            "deployment requires application.production_project, production_port, "
            "and production_health_url"
        )
    return ProductionTopology(
        compose_project=application.production_project,
        host_port=application.production_port,
        health_url=application.production_health_url,
    )


def configuration_fingerprint(config: DployDBConfig, *, secrets: SecretRegistry) -> str:
    """Hash a canonical redacted configuration without persisting resolved secrets."""
    safe = secrets.redact(config.model_dump(mode="json"))
    payload = json.dumps(
        safe,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class _DuplicateKeyError(yaml.YAMLError):
    def __init__(self, mark: yaml.Mark) -> None:
        self.mark = mark
        super().__init__("duplicate YAML key")


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses duplicate keys at every mapping level."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise _DuplicateKeyError(key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _location_text(location: tuple[int | str, ...]) -> str:
    if not location:
        return "configuration"
    parts: list[str] = []
    for item in location:
        text = str(item)
        parts.append("[sensitive-key]" if is_sensitive_key(text) else text)
    return ".".join(parts)


def _validation_summary(error: ValidationError) -> str:
    details: list[str] = []
    for item in error.errors(include_url=False, include_input=False):
        location = _location_text(item["loc"])
        details.append(f"{location}: {item['msg']}")
    return "; ".join(details)


def _validate_interpolation_syntax(value: object, location: tuple[int | str, ...] = ()) -> None:
    if isinstance(value, str):
        remainder = _INTERPOLATION.sub("", value)
        if "${" in remainder:
            raise _configuration_error(
                f"{_location_text(location)} contains invalid environment interpolation"
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_interpolation_syntax(item, (*location, index))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_interpolation_syntax(item, (*location, str(key)))


def parse_configuration(text: str) -> DployDBConfig:
    """Parse and structurally validate YAML without consulting the host."""
    if not isinstance(text, str):
        raise TypeError("configuration text must be a string")
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except _DuplicateKeyError as exc:
        raise _configuration_error(
            f"configuration contains a duplicate YAML key at line "
            f"{exc.mark.line + 1}, column {exc.mark.column + 1}"
        ) from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise _configuration_error(f"configuration is not valid YAML{location}") from None

    if not isinstance(raw, dict):
        raise _configuration_error("configuration root must be a YAML mapping")

    _validate_interpolation_syntax(raw)
    try:
        return DployDBConfig.model_validate(
            raw,
            context={"allow_unresolved_environment": True},
        )
    except ValidationError as exc:
        raise _configuration_error(
            f"configuration validation failed: {_validation_summary(exc)}"
        ) from None


def _missing_environment_names(value: object, environment: Mapping[str, str]) -> set[str]:
    missing: set[str] = set()
    if isinstance(value, str):
        missing.update(name for name in _INTERPOLATION.findall(value) if name not in environment)
    elif isinstance(value, list):
        for item in value:
            missing.update(_missing_environment_names(item, environment))
    elif isinstance(value, dict):
        for item in value.values():
            missing.update(_missing_environment_names(item, environment))
    return missing


def _resolve_value(
    value: Any,
    *,
    environment: Mapping[str, str],
    secrets: SecretRegistry,
    field_name: str | None = None,
) -> Any:
    if isinstance(value, str):
        names = _INTERPOLATION.findall(value)
        resolved = _INTERPOLATION.sub(lambda match: environment[match.group(1)], value)
        if (field_name is not None and is_sensitive_key(field_name)) or any(
            is_sensitive_key(name) for name in names
        ):
            secrets.register_many(environment[name] for name in names)
            if field_name is not None and is_sensitive_key(field_name):
                secrets.register(resolved)
        return resolved
    if isinstance(value, list):
        return [_resolve_value(item, environment=environment, secrets=secrets) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_value(
                item,
                environment=environment,
                secrets=secrets,
                field_name=key,
            )
            for key, item in value.items()
        }
    return value


def resolve_configuration(
    config: DployDBConfig,
    *,
    environment: Mapping[str, str],
    secrets: SecretRegistry,
) -> DployDBConfig:
    """Resolve ``${NAME}`` values and register secrets without host checks."""
    raw = config.model_dump(mode="json")
    missing = sorted(_missing_environment_names(raw, environment))
    if missing:
        raise _configuration_error(
            "configuration references missing environment variables: " + ", ".join(missing)
        )

    resolved = _resolve_value(raw, environment=environment, secrets=secrets)
    try:
        return DployDBConfig.model_validate(
            resolved,
            context={"allow_unresolved_environment": False},
        )
    except ValidationError as exc:
        raise _configuration_error(
            f"resolved configuration validation failed: {_validation_summary(exc)}"
        ) from None


def load_configuration(
    path: Path = DEFAULT_CONFIG_PATH,
    *,
    environment: Mapping[str, str] | None = None,
    secrets: SecretRegistry | None = None,
) -> LoadedConfiguration:
    """Read, structurally validate, and resolve one configuration file."""
    registry = secrets if secrets is not None else SecretRegistry()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise _configuration_error(f"configuration file could not be read: {path}") from None
    structural = parse_configuration(text)
    resolved = resolve_configuration(
        structural,
        environment=os.environ if environment is None else environment,
        secrets=registry,
    )
    return LoadedConfiguration(config=resolved, secrets=registry)


STARTER_CONFIGURATION = """\
# DployDB configuration for one SQLite application on one Linux server.
# Adjust every /srv/example path before running doctor or a deployment.
project: example-app

state_directory: /srv/example/.dploydb

database:
  path: /srv/example/data/app.db
  path_env: DATABASE_PATH
  minimum_free_space_multiplier: 3

migration:
  # Commands are argument arrays. DployDB never enables a shell implicitly.
  command: [python, scripts/migrate.py]
  timeout_seconds: 120

application:
  runner: docker_compose
  compose_file: /srv/example/compose.yaml
  service: app
  # The existing production service is preserved exactly for rollback.
  production_project: example-app
  production_port: 4510
  production_health_url: http://127.0.0.1:4510/health
  candidate_port: 4511
  # Container-side defaults are explicit so candidate isolation can be inspected.
  candidate_container_port: 8080
  database_volume_target: /data
  candidate_health_url: http://127.0.0.1:4511/health
  startup_timeout_seconds: 45
  # Omit smoke_command when the HTTP health check is sufficient.
  smoke_command: [python, scripts/smoke_test.py]
  test_mode_env:
    DPLOYDB_TEST_MODE: "1"

traffic:
  maintenance_on_command: [/srv/example/ops/maintenance, "on"]
  maintenance_off_command: [/srv/example/ops/maintenance, "off"]
  activate_new_command: [/srv/example/ops/activate, candidate]
  activate_old_command: [/srv/example/ops/activate, current]
  timeout_seconds: 30

backup:
  local_directory: /srv/dploydb/backups/example-app
  keep_last: 10
  # Remote backup is optional. Credentials are read only from named variables.
  remote:
    enabled: false
    required: false
    provider: s3
    bucket: example-backups
    prefix: dploydb/example-app
    region_name: auto
    storage_class: STANDARD
    # Set endpoint_url for S3-compatible services such as Cloudflare R2, or
    # name an environment variable containing it with endpoint_url_env.
    endpoint_url_env: S3_ENDPOINT_URL
    access_key_env: S3_ACCESS_KEY_ID
    secret_key_env: S3_SECRET_ACCESS_KEY
    timeout_seconds: 30
    max_attempts: 3
"""


def initialize_configuration(path: Path = DEFAULT_CONFIG_PATH) -> Path:
    """Create a valid mode-0600 starter configuration without overwriting."""
    # Keep the shipped template subject to the same parser as user configuration.
    structural = parse_configuration(STARTER_CONFIGURATION)
    resolve_configuration(structural, environment={}, secrets=SecretRegistry())

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, CONFIG_FILE_MODE)
    except FileExistsError:
        raise _configuration_error(
            f"configuration path already exists and was preserved: {path}"
        ) from None
    except OSError:
        raise _configuration_error(f"configuration file could not be created: {path}") from None

    created = True
    try:
        os.fchmod(descriptor, CONFIG_FILE_MODE)
        data = STARTER_CONFIGURATION.encode("utf-8")
        written = 0
        while written < len(data):
            write_count = os.write(descriptor, data[written:])
            if write_count <= 0:
                raise OSError("configuration write made no progress")
            written += write_count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
    except OSError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            path.unlink()
            created = False
        except OSError:
            pass
        detail = (
            f"configuration creation failed and the incomplete file may remain: {path}"
            if created
            else f"configuration file could not be written: {path}"
        )
        raise _configuration_error(detail) from None
    return path
