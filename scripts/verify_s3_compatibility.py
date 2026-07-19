"""Run a credential-safe, destructive-only-to-its-own-prefix S3 acceptance check."""

from __future__ import annotations

import getpass
import json
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

from dploydb.backup import create_verified_backup
from dploydb.config import RemoteBackupConfig
from dploydb.models import BackupPurpose
from dploydb.redaction import SecretRegistry
from dploydb.restore import restore_verified_database
from dploydb.storage.local import LocalBackupStorage
from dploydb.storage.s3 import configured_s3_storage


def main() -> None:
    endpoint = input("S3-compatible endpoint URL: ").strip()
    bucket = input("Bucket: ").strip()
    base_prefix = input("Base prefix: ").strip().strip("/")
    access_key = getpass.getpass("S3 access key ID: ")
    secret_key = getpass.getpass("S3 secret access key: ")
    run_id = uuid4().hex
    prefix = "/".join(part for part in (base_prefix, f"acceptance-{run_id}") if part)
    secrets = SecretRegistry()
    remote = configured_s3_storage(
        RemoteBackupConfig(
            enabled=True,
            required=True,
            bucket=bucket,
            prefix=prefix,
            region_name="auto",
            endpoint_url=endpoint,
            access_key_env="DPLOYDB_ACCEPTANCE_ACCESS_KEY",
            secret_key_env="DPLOYDB_ACCEPTANCE_SECRET_KEY",
            timeout_seconds=30,
            max_attempts=3,
        ),
        secrets=secrets,
        environment={
            "DPLOYDB_ACCEPTANCE_ACCESS_KEY": access_key,
            "DPLOYDB_ACCEPTANCE_SECRET_KEY": secret_key,
        },
    )
    committed_backup_id: str | None = None
    cleanup_error: str | None = None
    try:
        remote.probe_access()
        with tempfile.TemporaryDirectory(prefix="dploydb-s3-acceptance-") as directory:
            root = Path(directory)
            source = root / "source.db"
            target = root / "restored.db"
            with sqlite3.connect(source) as connection:
                connection.execute(
                    "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
                )
                connection.execute("INSERT INTO notes(body) VALUES ('r2-round-trip')")
            with sqlite3.connect(target) as connection:
                connection.execute(
                    "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
                )
                connection.execute("INSERT INTO notes(body) VALUES ('replace-me')")

            local = LocalBackupStorage((root / "local").resolve())
            artifact = create_verified_backup(
                source.resolve(),
                project="s3-compatibility-check",
                purpose=BackupPurpose.FINAL,
                storage=local,
                operation_id="op_" + uuid4().hex,
            )
            release_id = "release_" + uuid4().hex
            committed = remote.put(artifact, release_id=release_id)
            committed_backup_id = artifact.metadata.backup_id
            listed = {record.backup.backup_id: record for record in remote.list()}
            if listed.get(committed_backup_id) != committed.metadata:
                raise RuntimeError("committed backup was not returned by verified listing")

            hydrated_store = LocalBackupStorage((root / "hydrated").resolve())
            staging = hydrated_store.create_staging_database(committed_backup_id)
            downloaded = remote.download(committed_backup_id, staging)
            if downloaded != committed:
                raise RuntimeError("downloaded remote evidence changed")
            hydrated = hydrated_store.put(staging, artifact.metadata)
            restored = restore_verified_database(
                hydrated,
                target.resolve(),
                application_stopped=True,
                traffic_activated=False,
                secrets=secrets,
            )
            with sqlite3.connect(target) as connection:
                row = connection.execute("SELECT body FROM notes").fetchone()
            if row != ("r2-round-trip",):
                raise RuntimeError("restored SQLite row does not match the uploaded backup")
            if restored.sha256 != artifact.metadata.sha256:
                raise RuntimeError("restored SQLite checksum does not match remote metadata")

            remote.delete(committed_backup_id)
            if remote.exists(committed_backup_id):
                raise RuntimeError("acceptance object cleanup could not be verified")
            committed_backup_id = None
            print(
                json.dumps(
                    {
                        "ok": True,
                        "provider": "s3",
                        "bucket": bucket,
                        "prefix": prefix,
                        "upload_verified": True,
                        "download_verified": True,
                        "sqlite_restore_verified": True,
                        "cleanup_verified": True,
                    },
                    sort_keys=True,
                )
            )
    finally:
        if committed_backup_id is not None:
            try:
                remote.delete(committed_backup_id)
            except Exception as error:
                cleanup_error = secrets.redact_text(f"{type(error).__name__}: {error}")
        if cleanup_error is not None:
            raise RuntimeError(f"acceptance object cleanup failed: {cleanup_error}")


if __name__ == "__main__":
    main()
