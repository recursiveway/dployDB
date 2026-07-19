"""Focused S3-compatible storage tests with an in-memory protocol peer."""

from __future__ import annotations

import io
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from dploydb.backup import create_verified_backup, open_verified_configured_backup
from dploydb.config import (
    STARTER_CONFIGURATION,
    LoadedConfiguration,
    RemoteBackupConfig,
    load_configuration,
)
from dploydb.errors import ConfigurationError, OperationFailedError, SafetyCheckError
from dploydb.models import BackupArtifact, BackupPurpose
from dploydb.redaction import REDACTION_MARKER, SecretRegistry
from dploydb.restore import restore_verified_database
from dploydb.storage.local import LocalBackupStorage
from dploydb.storage.s3 import S3BackupStorage, configured_s3_storage


class MemoryBody(io.BytesIO):
    pass


class MemoryS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.operations: list[tuple[str, str]] = []
        self.failure_message: str | None = None

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._fail_if_requested("PutObject")
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        payload = body.read() if hasattr(body, "read") else bytes(body)
        assert int(kwargs["ContentLength"]) == len(payload)
        metadata = {str(name).lower(): str(value) for name, value in kwargs["Metadata"].items()}
        self.objects[key] = (payload, metadata)
        self.operations.append(("put", key))
        return {"ETag": '"memory"'}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._fail_if_requested("GetObject")
        key = str(kwargs["Key"])
        try:
            payload, metadata = self.objects[key]
        except KeyError:
            raise _not_found("GetObject") from None
        self.operations.append(("get", key))
        return {
            "Body": MemoryBody(payload),
            "ContentLength": len(payload),
            "Metadata": dict(metadata),
        }

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self._fail_if_requested("HeadObject")
        key = str(kwargs["Key"])
        try:
            payload, metadata = self.objects[key]
        except KeyError:
            raise _not_found("HeadObject") from None
        self.operations.append(("head", key))
        return {"ContentLength": len(payload), "Metadata": dict(metadata)}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self._fail_if_requested("ListObjectsV2")
        prefix = str(kwargs.get("Prefix", ""))
        self.operations.append(("list", prefix))
        return {
            "Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(prefix)],
            "IsTruncated": False,
        }

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self._fail_if_requested("DeleteObject")
        key = str(kwargs["Key"])
        self.objects.pop(key, None)
        self.operations.append(("delete", key))
        return {}

    def _fail_if_requested(self, operation: str) -> None:
        if self.failure_message is not None:
            raise ClientError(
                {
                    "Error": {"Code": "Injected", "Message": self.failure_message},
                    "ResponseMetadata": {"HTTPStatusCode": 500},
                },
                operation,
            )


def _not_found(operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "NoSuchKey", "Message": "missing"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        operation,
    )


def _local_backup(tmp_path: Path) -> tuple[LocalBackupStorage, BackupArtifact]:
    database = tmp_path / "app.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("INSERT INTO notes(body) VALUES ('remote-safe')")
    storage = LocalBackupStorage(tmp_path / "backups")
    artifact = create_verified_backup(
        database,
        project="remote-test",
        purpose=BackupPurpose.FINAL,
        storage=storage,
        operation_id="op_" + "a" * 32,
    )
    return storage, artifact


def _remote(client: MemoryS3Client, secrets: SecretRegistry | None = None) -> S3BackupStorage:
    return S3BackupStorage(
        client=client,
        bucket="verified-backups",
        prefix="dploydb/remote-test",
        storage_class="STANDARD",
        secrets=secrets or SecretRegistry(),
    )


def _loaded_remote_project(tmp_path: Path) -> LoadedConfiguration:
    value = yaml.safe_load(STARTER_CONFIGURATION)
    value["project"] = "remote-test"
    value["state_directory"] = str(tmp_path / "state")
    value["database"]["path"] = str(tmp_path / "production.db")
    value["application"]["compose_file"] = str(tmp_path / "compose.yaml")
    value["backup"]["local_directory"] = str(tmp_path / "backups")
    value["backup"]["remote"]["enabled"] = True
    path = tmp_path / "dploydb.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return load_configuration(path)


def test_database_is_read_back_before_metadata_last_commit_and_download(
    tmp_path: Path,
) -> None:
    _, local = _local_backup(tmp_path)
    client = MemoryS3Client()
    remote = _remote(client)
    release_id = "release_" + "b" * 32

    committed = remote.put(local, release_id=release_id)

    database_key = f"dploydb/remote-test/{local.metadata.backup_id}.db"
    metadata_key = f"dploydb/remote-test/{local.metadata.backup_id}.json"
    put_operations = [item for item in client.operations if item[0] == "put"]
    assert put_operations == [("put", database_key), ("put", metadata_key)]
    assert ("get", database_key) in client.operations
    assert committed.metadata.release_id == release_id
    assert committed.metadata.backup.sha256 == local.metadata.sha256
    assert remote.exists(local.metadata.backup_id) is True
    assert remote.verify_metadata(local.metadata.backup_id) == committed.metadata
    assert remote.list() == (committed.metadata,)

    staging = LocalBackupStorage(tmp_path / "hydrated").create_staging_database(
        local.metadata.backup_id
    )
    downloaded = remote.download(local.metadata.backup_id, staging)

    assert downloaded == committed
    assert staging.read_bytes() == local.database_path.read_bytes()
    with sqlite3.connect(staging) as connection:
        assert connection.execute("SELECT body FROM notes").fetchone() == ("remote-safe",)


def test_repeated_put_is_idempotent_but_contradictory_release_is_refused(
    tmp_path: Path,
) -> None:
    _, local = _local_backup(tmp_path)
    client = MemoryS3Client()
    remote = _remote(client)
    release_id = "release_" + "c" * 32
    first = remote.put(local, release_id=release_id)
    puts_before = [item for item in client.operations if item[0] == "put"]

    assert remote.put(local, release_id=release_id) == first
    assert [item for item in client.operations if item[0] == "put"] == puts_before
    with pytest.raises(SafetyCheckError, match="contradicts"):
        remote.put(local, release_id="release_" + "d" * 32)


def test_corrupt_remote_download_is_removed_and_refused(tmp_path: Path) -> None:
    _, local = _local_backup(tmp_path)
    client = MemoryS3Client()
    remote = _remote(client)
    remote.put(local)
    database_key = f"dploydb/remote-test/{local.metadata.backup_id}.db"
    payload, headers = client.objects[database_key]
    corrupted = bytearray(payload)
    corrupted[-1] ^= 0xFF
    client.objects[database_key] = (bytes(corrupted), headers)
    staging = LocalBackupStorage(tmp_path / "hydrated").create_staging_database(
        local.metadata.backup_id
    )

    with pytest.raises(SafetyCheckError, match="does not match metadata"):
        remote.download(local.metadata.backup_id, staging)

    assert not staging.exists()


def test_absent_local_backup_is_ephemerally_hydrated_and_restores_correctly(
    tmp_path: Path,
) -> None:
    _, local = _local_backup(tmp_path)
    loaded = _loaded_remote_project(tmp_path)
    client = MemoryS3Client()
    remote = _remote(client, loaded.secrets)
    expected_bytes = local.database_path.read_bytes()
    remote.put(local)
    local.database_path.unlink()
    local.metadata_path.unlink()
    production = loaded.config.database.path
    with sqlite3.connect(production) as connection:
        connection.execute("CREATE TABLE old_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO old_data(value) VALUES ('replace-me')")

    with open_verified_configured_backup(
        loaded,
        local.metadata.backup_id,
        remote_storage=remote,
    ) as hydrated:
        hydrated_root = hydrated.database_path.parent
        assert hydrated.database_path.read_bytes() == expected_bytes
        assert stat.S_IMODE(hydrated.database_path.stat().st_mode) == 0o600
        restored = restore_verified_database(
            hydrated,
            production,
            application_stopped=True,
            traffic_activated=False,
            secrets=loaded.secrets,
        )
        assert restored.sha256 == local.metadata.sha256
        assert not hydrated.database_path.samefile(production)

    assert not hydrated_root.exists()
    assert not local.database_path.exists()
    assert not local.metadata_path.exists()
    with sqlite3.connect(production) as connection:
        assert connection.execute("SELECT body FROM notes").fetchone() == ("remote-safe",)


def test_present_but_corrupt_local_backup_never_falls_back_to_remote(tmp_path: Path) -> None:
    _, local = _local_backup(tmp_path)
    loaded = _loaded_remote_project(tmp_path)
    client = MemoryS3Client()
    remote = _remote(client, loaded.secrets)
    remote.put(local)
    payload = bytearray(local.database_path.read_bytes())
    payload[-1] ^= 0xFF
    local.database_path.write_bytes(payload)
    local.database_path.chmod(0o600)
    client.operations.clear()

    with pytest.raises(SafetyCheckError, match="checksum mismatch"):
        with open_verified_configured_backup(
            loaded,
            local.metadata.backup_id,
            remote_storage=remote,
        ):
            raise AssertionError("corrupt local backup must not be yielded")

    assert client.operations == []


def test_partial_uncommitted_database_is_repaired_before_commit(tmp_path: Path) -> None:
    _, local = _local_backup(tmp_path)
    client = MemoryS3Client()
    remote = _remote(client)
    database_key = f"dploydb/remote-test/{local.metadata.backup_id}.db"
    client.objects[database_key] = (
        b"corrupt",
        {
            "backup-id": local.metadata.backup_id,
            "sha256": local.metadata.sha256,
            "size-bytes": str(local.metadata.size_bytes),
            "project": local.metadata.project,
        },
    )

    committed = remote.put(local)

    assert committed.metadata.backup == local.metadata
    assert client.objects[database_key][0] == local.database_path.read_bytes()


def test_remote_delete_is_idempotent_and_metadata_is_deleted_first(tmp_path: Path) -> None:
    _, local = _local_backup(tmp_path)
    client = MemoryS3Client()
    remote = _remote(client)
    remote.put(local)
    client.operations.clear()

    remote.delete(local.metadata.backup_id)
    remote.delete(local.metadata.backup_id)

    keys = [key for operation, key in client.operations if operation == "delete"]
    assert keys[0].endswith(".json")
    assert keys[1].endswith(".db")
    assert client.objects == {}
    assert remote.exists(local.metadata.backup_id) is False


def test_remote_access_probe_is_read_only_and_prefix_scoped() -> None:
    client = MemoryS3Client()
    remote = _remote(client)

    remote.probe_access()

    assert client.operations == [("list", "dploydb/remote-test/")]
    assert client.objects == {}


def test_runtime_configuration_registers_credentials_and_uses_r2_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_config = RemoteBackupConfig(
        enabled=True,
        required=True,
        bucket="verified-backups",
        prefix="dploydb/example",
        region_name="auto",
        endpoint_url="https://account.r2.cloudflarestorage.com",
        access_key_env="TEST_ACCESS_KEY",
        secret_key_env="TEST_SECRET_KEY",
        timeout_seconds=17,
        max_attempts=4,
    )
    client = MemoryS3Client()
    captured: dict[str, Any] = {}

    def fake_boto_client(service: str, **kwargs: Any) -> MemoryS3Client:
        captured["service"] = service
        captured.update(kwargs)
        return client

    monkeypatch.setattr("dploydb.storage.s3.boto3.client", fake_boto_client)
    secrets = SecretRegistry()
    access_key = "synthetic-access-key"
    secret_key = "synthetic-secret-key"

    storage = configured_s3_storage(
        remote_config,
        secrets=secrets,
        environment={"TEST_ACCESS_KEY": access_key, "TEST_SECRET_KEY": secret_key},
    )

    assert storage.bucket == "verified-backups"
    assert captured["service"] == "s3"
    assert captured["endpoint_url"] == "https://account.r2.cloudflarestorage.com"
    assert captured["region_name"] == "auto"
    config = captured["config"]
    assert config.connect_timeout == 17
    assert config.read_timeout == 17
    assert config.retries["total_max_attempts"] == 4
    assert config.s3["addressing_style"] == "path"
    assert secrets.redact_text(f"{access_key}:{secret_key}") == (
        f"{REDACTION_MARKER}:{REDACTION_MARKER}"
    )
    assert access_key not in repr(storage)
    assert secret_key not in repr(storage)


def test_missing_runtime_credential_fails_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_config = RemoteBackupConfig(
        enabled=True,
        bucket="verified-backups",
        access_key_env="TEST_ACCESS_KEY",
        secret_key_env="TEST_SECRET_KEY",
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("client must not be constructed")

    monkeypatch.setattr("dploydb.storage.s3.boto3.client", forbidden)

    with pytest.raises(ConfigurationError, match="environment variable is missing"):
        configured_s3_storage(
            remote_config,
            secrets=SecretRegistry(),
            environment={"TEST_ACCESS_KEY": "present"},
        )


def test_client_errors_are_redacted(tmp_path: Path) -> None:
    _, local = _local_backup(tmp_path)
    secret = "synthetic-remote-secret"
    secrets = SecretRegistry()
    secrets.register(secret)
    client = MemoryS3Client()
    client.failure_message = secret
    remote = _remote(client, secrets)

    with pytest.raises(OperationFailedError) as captured:
        remote.put(local)

    assert secret not in captured.value.payload.what_failed
    assert REDACTION_MARKER in captured.value.payload.what_failed
