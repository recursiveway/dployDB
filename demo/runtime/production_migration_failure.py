"""Demo fault command that fails only against the named production database."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from .migration import run_migration


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("production_database", type=Path)
    arguments = parser.parse_args()
    database_value = os.environ.get("DATABASE_PATH")
    if database_value is None:
        print("production migration fixture failed: DATABASE_PATH is required", file=sys.stderr)
        return 1
    database = Path(database_value).resolve()
    production = arguments.production_database.resolve()
    if database == production:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "ALTER TABLE notes ADD COLUMN partial_cutover TEXT DEFAULT 'must-rollback'"
            )
        print(
            "forced production migration failure after a committed schema mutation",
            file=sys.stderr,
        )
        return 9
    try:
        run_migration(arguments.release_dir.resolve())
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    print(f"rehearsal migration complete: {arguments.release_dir.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
