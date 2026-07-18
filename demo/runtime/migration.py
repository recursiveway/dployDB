"""Transactional SQLite migration runner for the deterministic demo."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from .release import ReleaseDefinition, ReleaseDefinitionError, load_release

_SQLITE_TIMEOUT_SECONDS = 5.0


class MigrationError(RuntimeError):
    """Raised when a migration cannot be run or verified safely."""


def _database_path_from_environment() -> Path:
    raw_path = os.environ.get("DATABASE_PATH")
    if raw_path is None or not raw_path.strip():
        raise MigrationError("DATABASE_PATH is required")

    database_path = Path(raw_path)
    if not database_path.is_absolute():
        raise MigrationError("DATABASE_PATH must be absolute")
    if not database_path.exists():
        raise MigrationError(f"database does not exist: {database_path}")
    if not database_path.is_file():
        raise MigrationError(f"database is not a regular file: {database_path}")
    return database_path


def _load_migration_sql(release: ReleaseDefinition) -> str:
    migration_path = release.directory / "migration.sql"
    if not migration_path.is_file():
        raise MigrationError(f"migration SQL does not exist: {migration_path}")
    try:
        sql = migration_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise MigrationError(f"cannot read migration SQL: {exc}") from exc
    if not sql.strip():
        raise MigrationError("migration SQL is empty")
    return sql


def _sql_statements(sql: str) -> list[str]:
    """Split SQL without ``executescript`` so the caller owns the transaction."""

    statements: list[str] = []
    pending: list[str] = []
    for character in sql:
        pending.append(character)
        if character == ";":
            candidate = "".join(pending)
            if sqlite3.complete_statement(candidate):
                statements.append(candidate)
                pending.clear()

    remainder = "".join(pending)
    if remainder.strip():
        if not sqlite3.complete_statement(f"{remainder};"):
            raise MigrationError("migration SQL contains an incomplete statement")
        statements.append(remainder)

    if not statements:
        raise MigrationError("migration SQL contains no statements")
    return statements


def _deny_migration_transaction_controls(
    action_code: int,
    _argument_one: str | None,
    _argument_two: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    if action_code in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT}:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _single_integer(connection: sqlite3.Connection, pragma: str) -> int:
    row = connection.execute(pragma).fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise MigrationError(f"unexpected result from {pragma}")
    return row[0]


def _verify_database(connection: sqlite3.Connection, target_version: int) -> None:
    actual_version = _single_integer(connection, "PRAGMA user_version")
    if actual_version != target_version:
        raise MigrationError(
            f"migration set user_version {actual_version}; expected {target_version}"
        )

    quick_check = connection.execute("PRAGMA quick_check").fetchall()
    if quick_check != [("ok",)]:
        raise MigrationError(f"PRAGMA quick_check failed: {quick_check!r}")

    foreign_key_failures = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_failures:
        raise MigrationError(f"PRAGMA foreign_key_check failed: {foreign_key_failures!r}")


def run_migration(release_directory: Path) -> None:
    """Apply one release migration atomically to ``DATABASE_PATH``."""

    release = load_release(release_directory)
    database_path = _database_path_from_environment()
    statements = _sql_statements(_load_migration_sql(release))

    connection = sqlite3.connect(
        database_path,
        timeout=_SQLITE_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(_SQLITE_TIMEOUT_SECONDS * 1000)}")
        connection.execute("PRAGMA foreign_keys = ON")
        if _single_integer(connection, "PRAGMA foreign_keys") != 1:
            raise MigrationError("could not enable SQLite foreign key enforcement")

        current_version = _single_integer(connection, "PRAGMA user_version")
        if current_version != release.schema.from_version:
            raise MigrationError(
                "database user_version "
                f"{current_version} does not match release from_version "
                f"{release.schema.from_version}"
            )

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.set_authorizer(_deny_migration_transaction_controls)
            try:
                for statement in statements:
                    connection.execute(statement)
            finally:
                connection.set_authorizer(None)

            _verify_database(connection, release.schema.to_version)
            connection.execute("COMMIT")
        except BaseException:
            connection.set_authorizer(None)
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m demo.runtime.migration",
        description="Apply a deterministic demo release migration.",
    )
    parser.add_argument("release_dir", type=Path, help="release directory to apply")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_migration(args.release_dir)
    except (MigrationError, ReleaseDefinitionError, OSError, sqlite3.Error) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1

    print(f"migration complete: {args.release_dir.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
