"""Standard-library HTTP application for the deterministic SQLite demo."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar, cast
from urllib.parse import urlsplit

from .release import ReleaseDefinition, ReleaseDefinitionError, load_release

_SQLITE_TIMEOUT_SECONDS = 5.0
_MAX_REQUEST_BYTES = 16 * 1024
_MAX_BODY_CHARACTERS = 500
_MAX_CATEGORY_CHARACTERS = 100
_KNOWN_PATHS = frozenset({"/health", "/notes"})

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class StartupError(RuntimeError):
    """Raised when application startup configuration is invalid."""


class InputError(ValueError):
    """Raised when an HTTP request body is invalid."""


class DatabaseStateError(RuntimeError):
    """Raised when database results do not match the demo contract."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Validated runtime configuration."""

    database_path: Path
    release: ReleaseDefinition
    port: int


def _required_environment_path(name: str) -> Path:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        raise StartupError(f"{name} is required")
    return Path(raw_value)


def _load_config() -> AppConfig:
    database_path = _required_environment_path("DATABASE_PATH")
    if not database_path.is_absolute():
        raise StartupError("DATABASE_PATH must be absolute")
    if not database_path.exists():
        raise StartupError(f"database does not exist: {database_path}")
    if not database_path.is_file():
        raise StartupError(f"database is not a regular file: {database_path}")

    release_directory = _required_environment_path("RELEASE_DIRECTORY")
    release = load_release(release_directory)

    raw_port = os.environ.get("PORT", "8080")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise StartupError("PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise StartupError("PORT must be between 1 and 65535")

    return AppConfig(database_path=database_path, release=release, port=port)


def _json_bytes(payload: JsonValue) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _single_integer(connection: sqlite3.Connection, statement: str) -> int:
    row = connection.execute(statement).fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise DatabaseStateError("database returned an unexpected integer result")
    return row[0]


def _connect(config: AppConfig) -> sqlite3.Connection:
    connection = sqlite3.connect(config.database_path, timeout=_SQLITE_TIMEOUT_SECONDS)
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(_SQLITE_TIMEOUT_SECONDS * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        if _single_integer(connection, "PRAGMA foreign_keys") != 1:
            raise DatabaseStateError("foreign key enforcement could not be enabled")
    except BaseException:
        connection.close()
        raise
    return connection


def _schema_matches(connection: sqlite3.Connection, target_version: int) -> bool:
    columns = connection.execute("PRAGMA table_info(notes)").fetchall()
    column_names: list[str] = []
    for column in columns:
        if len(column) < 2 or not isinstance(column[1], str):
            return False
        column_names.append(column[1])

    expected_columns = ["id", "body"]
    if target_version >= 2:
        expected_columns.append("category")
    return column_names == expected_columns


def _verify_database_health(connection: sqlite3.Connection, target_version: int) -> bool:
    if _single_integer(connection, "PRAGMA user_version") != target_version:
        return False
    if not _schema_matches(connection, target_version):
        return False

    selected_columns = "id, body" if target_version == 1 else "id, body, category"
    connection.execute(f"SELECT {selected_columns} FROM notes ORDER BY id LIMIT 1").fetchone()

    quick_check = connection.execute("PRAGMA quick_check").fetchall()
    if quick_check != [("ok",)]:
        raise DatabaseStateError("SQLite quick check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise DatabaseStateError("SQLite foreign key check failed")
    return True


def _require_string(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InputError
    return value


def _require_json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InputError
    return cast(dict[str, object], value)


class DemoRequestHandler(BaseHTTPRequestHandler):
    """Serve the release's health and notes API."""

    protocol_version = "HTTP/1.1"
    config: ClassVar[AppConfig]

    def log_message(self, _format: str, *_args: object) -> None:
        """Suppress the base class's access log."""

    def _path(self) -> str:
        return urlsplit(self.path).path

    def _send_json(self, status: HTTPStatus, payload: JsonValue) -> None:
        body = _json_bytes(payload)
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_error_json(self, status: HTTPStatus, error: str) -> None:
        self._send_json(status, {"error": error})

    def _unsupported_method(self) -> None:
        if self._path() in _KNOWN_PATHS:
            self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "method")
        else:
            self._send_error_json(HTTPStatus.NOT_FOUND, "route")

    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        if path == "/health":
            self._get_health()
        elif path == "/notes":
            self._get_notes()
        else:
            self._send_error_json(HTTPStatus.NOT_FOUND, "route")

    def do_POST(self) -> None:  # noqa: N802
        path = self._path()
        if path == "/notes":
            self._post_note()
        elif path == "/health":
            self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "method")
        else:
            self._send_error_json(HTTPStatus.NOT_FOUND, "route")

    def do_HEAD(self) -> None:  # noqa: N802
        self._unsupported_method()

    def do_PUT(self) -> None:  # noqa: N802
        self._unsupported_method()

    def do_DELETE(self) -> None:  # noqa: N802
        self._unsupported_method()

    def do_PATCH(self) -> None:  # noqa: N802
        self._unsupported_method()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._unsupported_method()

    def do_TRACE(self) -> None:  # noqa: N802
        self._unsupported_method()

    def do_CONNECT(self) -> None:  # noqa: N802
        self._unsupported_method()

    def _get_health(self) -> None:
        target_version = self.config.release.schema.to_version
        try:
            connection = _connect(self.config)
            try:
                schema_matches = _verify_database_health(connection, target_version)
            finally:
                connection.close()
        except (DatabaseStateError, OSError, sqlite3.Error):
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "reason": "database_unhealthy"},
            )
            return

        if not schema_matches:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "reason": "schema_mismatch"},
            )
            return

        if self.config.release.health.mode == "broken":
            reason = self.config.release.health.failure_reason or "fixture_broken_health"
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "reason": reason})
            return

        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "release": self.config.release.name,
                "schema_version": target_version,
            },
        )

    def _get_notes(self) -> None:
        target_version = self.config.release.schema.to_version
        try:
            connection = _connect(self.config)
            try:
                if target_version >= 2:
                    rows = connection.execute(
                        "SELECT id, body, category FROM notes ORDER BY id"
                    ).fetchall()
                    notes: list[JsonValue] = [self._note_from_v2_row(row) for row in rows]
                else:
                    rows = connection.execute("SELECT id, body FROM notes ORDER BY id").fetchall()
                    notes = [self._note_from_v1_row(row) for row in rows]
            finally:
                connection.close()
        except (DatabaseStateError, OSError, sqlite3.Error):
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "database_error")
            return

        self._send_json(HTTPStatus.OK, notes)

    def _post_note(self) -> None:
        if not self._has_json_content_type():
            self._send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "media_type")
            return

        try:
            payload = self._read_json_body()
            body, category = self._validate_note_payload(payload)
        except (InputError, UnicodeError, json.JSONDecodeError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "input")
            return

        target_version = self.config.release.schema.to_version
        try:
            connection = _connect(self.config)
            try:
                if target_version >= 2:
                    cursor = connection.execute(
                        "INSERT INTO notes (body, category) VALUES (?, ?)",
                        (body, category),
                    )
                    note: JsonValue = {
                        "id": cursor.lastrowid,
                        "body": body,
                        "category": category,
                    }
                else:
                    cursor = connection.execute("INSERT INTO notes (body) VALUES (?)", (body,))
                    note = {"id": cursor.lastrowid, "body": body}
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        except (DatabaseStateError, OSError, sqlite3.Error):
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "database_error")
            return

        self._send_json(HTTPStatus.CREATED, note)

    def _has_json_content_type(self) -> bool:
        raw_content_type = self.headers.get("Content-Type", "")
        media_type = raw_content_type.split(";", maxsplit=1)[0].strip().lower()
        return media_type == "application/json"

    def _read_json_body(self) -> object:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise InputError
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise InputError from exc
        if content_length <= 0 or content_length > _MAX_REQUEST_BYTES:
            raise InputError

        body = self.rfile.read(content_length)
        if len(body) != content_length:
            raise InputError
        return json.loads(body.decode("utf-8"))

    def _validate_note_payload(self, payload: object) -> tuple[str, str | None]:
        note = _require_json_object(payload)
        target_version = self.config.release.schema.to_version
        allowed_fields = {"body", "category"} if target_version >= 2 else {"body"}
        if set(note) - allowed_fields or "body" not in note:
            raise InputError

        body = _require_string(note["body"], maximum=_MAX_BODY_CHARACTERS)
        if target_version < 2:
            return body, None

        category_value = note.get("category", "general")
        category = _require_string(category_value, maximum=_MAX_CATEGORY_CHARACTERS)
        return body, category

    @staticmethod
    def _note_from_v1_row(row: Sequence[object]) -> JsonValue:
        if len(row) != 2 or type(row[0]) is not int or not isinstance(row[1], str):
            raise DatabaseStateError("invalid v1 note row")
        return {"id": row[0], "body": row[1]}

    @staticmethod
    def _note_from_v2_row(row: Sequence[object]) -> JsonValue:
        if (
            len(row) != 3
            or type(row[0]) is not int
            or not isinstance(row[1], str)
            or not isinstance(row[2], str)
        ):
            raise DatabaseStateError("invalid v2 note row")
        return {"id": row[0], "body": row[1], "category": row[2]}


class DemoHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server with bounded worker lifetime at process shutdown."""

    daemon_threads = True
    allow_reuse_address = True


def run(config: AppConfig) -> None:
    """Serve requests until interrupted."""

    DemoRequestHandler.config = config
    with DemoHTTPServer(("0.0.0.0", config.port), DemoRequestHandler) as server:
        server.serve_forever()


def main(_argv: Sequence[str] | None = None) -> int:
    """Load environment configuration and run the HTTP application."""

    try:
        config = _load_config()
        run(config)
    except KeyboardInterrupt:
        return 0
    except (ReleaseDefinitionError, StartupError, OSError, sqlite3.Error) as exc:
        print(f"startup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
