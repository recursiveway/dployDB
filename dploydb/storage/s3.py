"""Verified metadata-last backup replication for S3-compatible services."""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Protocol, cast

import boto3  # type: ignore[import-untyped]
from botocore.client import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]
from pydantic import ValidationError

from dploydb.config import RemoteBackupConfig
from dploydb.errors import ConfigurationError, OperationFailedError, SafetyCheckError
from dploydb.models import (
    BackupArtifact,
    BackupMetadata,
    RemoteBackupArtifact,
    RemoteBackupMetadata,
    utc_now,
)
from dploydb.redaction import SecretRegistry
from dploydb.sqlite_checks import verify_sqlite_database
from dploydb.storage.local import FILE_MODE, MAX_METADATA_BYTES

_BACKUP_ID = re.compile(r"^backup_[0-9a-f]{32}$")
_NOT_FOUND_CODES: Final[frozenset[str]] = frozenset({"404", "NoSuchKey", "NotFound"})
_STREAM_CHUNK_BYTES: Final[int] = 1024 * 1024
_MAX_LISTED_BACKUPS: Final[int] = 100_000


class _StreamingBody(Protocol):
    def read(self, amount: int | None = None) -> bytes: ...

    def close(self) -> None: ...


class S3Client(Protocol):
    """Small low-level client surface used by the adapter and its tests."""

    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]: ...

    def delete_object(self, **kwargs: Any) -> dict[str, Any]: ...


class S3BackupStorage:
    """Replicate immutable verified backups with metadata as the commit marker."""

    def __init__(
        self,
        *,
        client: S3Client,
        bucket: str,
        prefix: str,
        storage_class: str,
        secrets: SecretRegistry,
    ) -> None:
        self._client = client
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.storage_class = storage_class
        self.secrets = secrets

    def __repr__(self) -> str:
        return (
            f"S3BackupStorage(bucket={self.bucket!r}, prefix={self.prefix!r}, credentials=<hidden>)"
        )

    def put(
        self,
        artifact: BackupArtifact,
        *,
        release_id: str | None = None,
    ) -> RemoteBackupArtifact:
        """Upload database bytes first and publish immutable metadata last."""
        self._validate_id(artifact.metadata.backup_id)
        self._verify_local_artifact(artifact)
        backup_id = artifact.metadata.backup_id

        try:
            committed = self.verify_metadata(backup_id)
        except SafetyCheckError as error:
            if not self._metadata_missing(error, backup_id):
                raise
        else:
            if committed.backup != artifact.metadata or committed.release_id != release_id:
                raise self._verification_error(
                    backup_id,
                    "committed remote metadata contradicts the requested backup identity",
                )
            return self._artifact(committed)

        database_key = self._database_key(backup_id)
        if self._object_exists(database_key):
            try:
                self._verify_remote_database_bytes(database_key, artifact.metadata)
            except SafetyCheckError:
                self._delete_key(database_key)
        if not self._object_exists(database_key):
            self._upload_database(artifact, database_key)

        try:
            self._verify_remote_database_bytes(database_key, artifact.metadata)
            record = RemoteBackupMetadata(
                backup=artifact.metadata,
                release_id=release_id,
                database_object_key=database_key,
                uploaded_at=utc_now(),
            )
            self._upload_metadata(record)
            committed = self.verify_metadata(backup_id)
            if committed != record:
                raise self._verification_error(
                    backup_id,
                    "remote metadata changed while the backup was committed",
                )
            return self._artifact(committed)
        except Exception as error:
            cleanup_errors = self._cleanup_uncommitted(backup_id)
            if cleanup_errors:
                error.add_note(self.secrets.redact_text("; ".join(cleanup_errors)))
            if isinstance(error, (OperationFailedError, SafetyCheckError)):
                raise
            raise self._operation_error(
                backup_id,
                f"remote backup commit failed: {type(error).__name__}: {error}",
            ) from None

    def download(self, backup_id: str, destination: Path) -> RemoteBackupArtifact:
        """Download one committed object into a caller-owned private staging file."""
        self._validate_id(backup_id)
        record = self.verify_metadata(backup_id)
        self._validate_download_destination(destination)
        body: _StreamingBody | None = None
        descriptor = -1
        try:
            response = self._client.get_object(
                Bucket=self.bucket,
                Key=record.database_object_key,
            )
            self._validate_database_response(response, record.backup)
            body = cast(_StreamingBody, response["Body"])
            flags = os.O_WRONLY | os.O_TRUNC
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(destination, flags)
            os.fchmod(descriptor, FILE_MODE)
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = body.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                _write_all(descriptor, chunk)
                digest.update(chunk)
                size += len(chunk)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if size != record.backup.size_bytes or digest.hexdigest() != record.backup.sha256:
                raise self._verification_error(
                    backup_id,
                    "downloaded remote backup SHA-256 or size does not match metadata",
                )
            verify_sqlite_database(destination)
            return self._artifact(record)
        except (ClientError, BotoCoreError, OSError, KeyError, TypeError) as error:
            self._remove_failed_download(destination)
            raise self._operation_error(
                backup_id,
                f"remote backup download failed: {type(error).__name__}: {error}",
            ) from None
        except Exception:
            self._remove_failed_download(destination)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if body is not None:
                body.close()

    def exists(self, backup_id: str) -> bool:
        self._validate_id(backup_id)
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._metadata_key(backup_id))
        except ClientError as error:
            if _is_not_found(error):
                return False
            raise self._operation_error(
                backup_id,
                f"remote metadata existence check failed: {error}",
            ) from None
        except BotoCoreError as error:
            raise self._operation_error(
                backup_id,
                f"remote metadata existence check failed: {error}",
            ) from None
        return True

    def probe_access(self) -> None:
        """Perform one bounded read-only request against the configured bucket."""
        prefix = f"{self.prefix}/" if self.prefix else ""
        try:
            response = self._client.list_objects_v2(
                Bucket=self.bucket,
                Prefix=prefix,
                MaxKeys=1,
            )
            if not isinstance(response, dict):
                raise TypeError("S3 access probe returned an invalid response")
        except (ClientError, BotoCoreError, TypeError) as error:
            raise self._operation_error(
                "probe",
                f"remote backup access probe failed: {type(error).__name__}: {error}",
            ) from None

    def list(self) -> tuple[RemoteBackupMetadata, ...]:
        prefix = f"{self.prefix}/" if self.prefix else ""
        continuation: str | None = None
        backup_ids: list[str] = []
        try:
            while True:
                arguments: dict[str, Any] = {
                    "Bucket": self.bucket,
                    "Prefix": prefix,
                    "MaxKeys": 1000,
                }
                if continuation is not None:
                    arguments["ContinuationToken"] = continuation
                response = self._client.list_objects_v2(**arguments)
                contents = response.get("Contents", [])
                if not isinstance(contents, list):
                    raise TypeError("S3 list response contents are invalid")
                for item in contents:
                    if not isinstance(item, dict) or not isinstance(item.get("Key"), str):
                        raise TypeError("S3 list response object is invalid")
                    key = item["Key"]
                    name = key.removeprefix(prefix)
                    if "/" not in name and name.startswith("backup_") and name.endswith(".json"):
                        backup_id = name.removesuffix(".json")
                        self._validate_id(backup_id)
                        backup_ids.append(backup_id)
                        if len(backup_ids) > _MAX_LISTED_BACKUPS:
                            raise ValueError("remote backup listing exceeds the safety limit")
                if not response.get("IsTruncated"):
                    break
                token = response.get("NextContinuationToken")
                if not isinstance(token, str) or not token:
                    raise TypeError("truncated S3 listing omitted its continuation token")
                continuation = token
        except (ClientError, BotoCoreError, TypeError, ValueError) as error:
            raise self._operation_error(
                "listing",
                f"remote backup listing failed: {type(error).__name__}: {error}",
            ) from None
        return tuple(self.verify_metadata(backup_id) for backup_id in sorted(set(backup_ids)))

    def delete(self, backup_id: str) -> None:
        """Delete metadata first so interrupted cleanup never advertises missing bytes."""
        self._validate_id(backup_id)
        metadata_key = self._metadata_key(backup_id)
        database_key = self._database_key(backup_id)
        self._delete_key(metadata_key)
        self._delete_key(database_key)
        remaining = [key for key in (metadata_key, database_key) if self._object_exists(key)]
        if remaining:
            raise self._operation_error(
                backup_id,
                "remote object deletion returned without proving every backup object absent",
            )

    def verify_metadata(self, backup_id: str) -> RemoteBackupMetadata:
        self._validate_id(backup_id)
        key = self._metadata_key(backup_id)
        body: _StreamingBody | None = None
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            body = cast(_StreamingBody, response["Body"])
            payload = _read_bounded(body, MAX_METADATA_BYTES)
            if not payload.endswith(b"\n"):
                raise ValueError("remote backup metadata is truncated")
            record = RemoteBackupMetadata.model_validate_json(payload)
        except ClientError as error:
            if _is_not_found(error):
                raise self._verification_error(
                    backup_id, "remote backup metadata is missing"
                ) from None
            raise self._operation_error(
                backup_id,
                f"remote metadata read failed: {error}",
            ) from None
        except BotoCoreError as error:
            raise self._operation_error(
                backup_id,
                f"remote metadata read failed: {error}",
            ) from None
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise self._verification_error(
                backup_id,
                f"remote backup metadata is invalid: {error}",
            ) from None
        finally:
            if body is not None:
                body.close()
        if record.backup.backup_id != backup_id:
            raise self._verification_error(
                backup_id,
                "remote backup metadata ID does not match its object key",
            )
        if record.database_object_key != self._database_key(backup_id):
            raise self._verification_error(
                backup_id,
                "remote backup metadata points outside its configured object key",
            )
        try:
            head = self._client.head_object(
                Bucket=self.bucket,
                Key=record.database_object_key,
            )
            self._validate_database_response(head, record.backup)
        except ClientError as error:
            if _is_not_found(error):
                raise self._verification_error(
                    backup_id,
                    "remote backup metadata exists but database bytes are missing",
                ) from None
            raise self._operation_error(
                backup_id,
                f"remote database metadata check failed: {error}",
            ) from None
        except BotoCoreError as error:
            raise self._operation_error(
                backup_id,
                f"remote database metadata check failed: {error}",
            ) from None
        return record

    def _upload_database(self, artifact: BackupArtifact, key: str) -> None:
        arguments: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "ContentLength": artifact.metadata.size_bytes,
            "ContentType": "application/vnd.sqlite3",
            "Metadata": _database_headers(artifact.metadata),
        }
        if self.storage_class != "STANDARD":
            arguments["StorageClass"] = self.storage_class
        try:
            with artifact.database_path.open("rb") as source:
                arguments["Body"] = source
                self._client.put_object(**arguments)
        except (ClientError, BotoCoreError, OSError) as error:
            raise self._operation_error(
                artifact.metadata.backup_id,
                f"remote database upload failed: {error}",
            ) from None

    def _upload_metadata(self, record: RemoteBackupMetadata) -> None:
        payload = _serialize_remote_metadata(record)
        arguments: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self._metadata_key(record.backup.backup_id),
            "Body": payload,
            "ContentLength": len(payload),
            "ContentType": "application/json",
            "Metadata": {
                "backup-id": record.backup.backup_id,
                "sha256": record.backup.sha256,
            },
        }
        if self.storage_class != "STANDARD":
            arguments["StorageClass"] = self.storage_class
        try:
            self._client.put_object(**arguments)
        except (ClientError, BotoCoreError) as error:
            raise self._operation_error(
                record.backup.backup_id,
                f"remote metadata upload failed: {error}",
            ) from None

    def _verify_remote_database_bytes(self, key: str, metadata: BackupMetadata) -> None:
        body: _StreamingBody | None = None
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            self._validate_database_response(response, metadata)
            body = cast(_StreamingBody, response["Body"])
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = body.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        except ClientError as error:
            if _is_not_found(error):
                raise self._verification_error(
                    metadata.backup_id,
                    "remote database bytes are missing",
                ) from None
            raise self._operation_error(
                metadata.backup_id,
                f"remote database readback failed: {error}",
            ) from None
        except BotoCoreError as error:
            raise self._operation_error(
                metadata.backup_id,
                f"remote database readback failed: {error}",
            ) from None
        finally:
            if body is not None:
                body.close()
        if size != metadata.size_bytes or digest.hexdigest() != metadata.sha256:
            raise self._verification_error(
                metadata.backup_id,
                "remote database readback SHA-256 or size does not match local metadata",
            )

    def _validate_database_response(
        self,
        response: Mapping[str, Any],
        metadata: BackupMetadata,
    ) -> None:
        content_length = response.get("ContentLength")
        headers = response.get("Metadata")
        if content_length != metadata.size_bytes:
            raise self._verification_error(
                metadata.backup_id,
                "remote database object size does not match backup metadata",
            )
        if not isinstance(headers, Mapping):
            raise self._verification_error(
                metadata.backup_id,
                "remote database object metadata is missing",
            )
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        expected = _database_headers(metadata)
        if any(normalized.get(key) != value for key, value in expected.items()):
            raise self._verification_error(
                metadata.backup_id,
                "remote database object metadata contradicts the backup identity",
            )

    def _object_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if _is_not_found(error):
                return False
            raise self._operation_error(key, f"remote object check failed: {error}") from None
        except BotoCoreError as error:
            raise self._operation_error(key, f"remote object check failed: {error}") from None
        return True

    def _delete_key(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError) as error:
            raise self._operation_error(key, f"remote object deletion failed: {error}") from None

    def _cleanup_uncommitted(self, backup_id: str) -> builtins.list[str]:
        errors: builtins.list[str] = []
        for key in (self._metadata_key(backup_id), self._database_key(backup_id)):
            try:
                self._delete_key(key)
            except OperationFailedError as error:
                errors.append(error.payload.what_failed)
        return errors

    def _artifact(self, metadata: RemoteBackupMetadata) -> RemoteBackupArtifact:
        return RemoteBackupArtifact(
            metadata=metadata,
            bucket=self.bucket,
            metadata_object_key=self._metadata_key(metadata.backup.backup_id),
        )

    def _database_key(self, backup_id: str) -> str:
        name = f"{backup_id}.db"
        return f"{self.prefix}/{name}" if self.prefix else name

    def _metadata_key(self, backup_id: str) -> str:
        name = f"{backup_id}.json"
        return f"{self.prefix}/{name}" if self.prefix else name

    def _validate_id(self, backup_id: str) -> None:
        if _BACKUP_ID.fullmatch(backup_id) is None:
            raise self._verification_error(backup_id, "remote backup ID is invalid")

    def _verify_local_artifact(self, artifact: BackupArtifact) -> None:
        from dploydb.backup import calculate_sha256

        try:
            details = artifact.database_path.lstat()
        except OSError as error:
            raise self._verification_error(
                artifact.metadata.backup_id,
                f"local backup is unavailable before upload: {error}",
            ) from None
        if artifact.database_path.is_symlink() or not stat.S_ISREG(details.st_mode):
            raise self._verification_error(
                artifact.metadata.backup_id,
                "local backup must be a regular non-symlink file before upload",
            )
        size, sha256 = calculate_sha256(artifact.database_path)
        if size != artifact.metadata.size_bytes or sha256 != artifact.metadata.sha256:
            raise self._verification_error(
                artifact.metadata.backup_id,
                "local backup changed before remote upload",
            )
        verify_sqlite_database(artifact.database_path)

    def _validate_download_destination(self, destination: Path) -> None:
        if not destination.is_absolute():
            raise ValueError("remote download destination must be absolute")
        try:
            details = destination.lstat()
        except OSError as error:
            raise self._operation_error(
                destination.name,
                f"remote download staging file is unavailable: {error}",
            ) from None
        if (
            destination.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != FILE_MODE
        ):
            raise self._operation_error(
                destination.name,
                "remote download requires a private mode-0600 regular staging file",
            )

    @staticmethod
    def _remove_failed_download(destination: Path) -> None:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass

    def _metadata_missing(self, error: SafetyCheckError, backup_id: str) -> bool:
        return (
            "metadata is missing" in error.payload.what_failed
            and error.payload.log_path == self._location(self._metadata_key(backup_id))
        )

    def _operation_error(self, identity: str, detail: str) -> OperationFailedError:
        return OperationFailedError(
            self.secrets.redact_text(detail),
            production_changed=False,
            previous_application_running=None,
            log_path=self._location(identity),
            next_safe_action=(
                "Production was not changed; correct remote backup storage and retry."
            ),
        )

    def _verification_error(self, backup_id: str, detail: str) -> SafetyCheckError:
        key = self._metadata_key(backup_id) if _BACKUP_ID.fullmatch(backup_id) else backup_id
        return SafetyCheckError(
            self.secrets.redact_text(detail),
            production_changed=False,
            previous_application_running=None,
            log_path=self._location(key),
            next_safe_action=(
                "Do not restore or retain this remote object as verified; use another backup."
            ),
        )

    def _location(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"


def configured_s3_storage(
    remote: RemoteBackupConfig,
    *,
    secrets: SecretRegistry,
    environment: Mapping[str, str],
    client: S3Client | None = None,
) -> S3BackupStorage:
    """Resolve runtime-only credentials and construct an R2/S3-compatible adapter."""
    if not remote.enabled:
        raise _configuration_error("remote backup is not enabled")
    if remote.bucket is None or remote.access_key_env is None or remote.secret_key_env is None:
        raise _configuration_error("enabled remote backup configuration is incomplete")

    endpoint_url = remote.endpoint_url
    if remote.endpoint_url_env is not None:
        endpoint_url = _required_environment_value(
            remote.endpoint_url_env,
            environment,
            label="remote endpoint",
        )
    access_key = _required_environment_value(
        remote.access_key_env,
        environment,
        label="remote access key",
    )
    secret_key = _required_environment_value(
        remote.secret_key_env,
        environment,
        label="remote secret key",
    )
    session_token = None
    if remote.session_token_env is not None:
        session_token = _required_environment_value(
            remote.session_token_env,
            environment,
            label="remote session token",
        )
    secrets.register_many(
        value for value in (access_key, secret_key, session_token) if value is not None
    )

    resolved = remote.model_dump(mode="python")
    resolved["endpoint_url"] = endpoint_url
    resolved["endpoint_url_env"] = None
    try:
        validated = RemoteBackupConfig.model_validate(resolved)
    except ValidationError as error:
        raise _configuration_error(
            "resolved remote backup endpoint is invalid: "
            + secrets.redact_text(str(error.errors(include_url=False, include_input=False)))
        ) from None

    selected_client = client
    if selected_client is None:
        boto_config = Config(
            signature_version="s3v4",
            connect_timeout=validated.timeout_seconds,
            read_timeout=validated.timeout_seconds,
            retries={
                "mode": "standard",
                "total_max_attempts": validated.max_attempts,
            },
            s3={"addressing_style": "path"},
        )
        selected_client = cast(
            S3Client,
            boto3.client(
                "s3",
                endpoint_url=validated.endpoint_url,
                region_name=validated.region_name,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                aws_session_token=session_token,
                config=boto_config,
            ),
        )
    return S3BackupStorage(
        client=selected_client,
        bucket=validated.bucket or "",
        prefix=validated.prefix,
        storage_class=validated.storage_class,
        secrets=secrets,
    )


def _required_environment_value(
    name: str,
    environment: Mapping[str, str],
    *,
    label: str,
) -> str:
    try:
        value = environment[name]
    except KeyError:
        raise _configuration_error(f"{label} environment variable is missing: {name}") from None
    if not value or "\x00" in value:
        raise _configuration_error(f"{label} environment variable is empty or invalid: {name}")
    return value


def _database_headers(metadata: BackupMetadata) -> dict[str, str]:
    return {
        "backup-id": metadata.backup_id,
        "sha256": metadata.sha256,
        "size-bytes": str(metadata.size_bytes),
        "project": metadata.project,
    }


def _serialize_remote_metadata(record: RemoteBackupMetadata) -> bytes:
    payload = (
        json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if len(payload) > MAX_METADATA_BYTES:
        raise ValueError("serialized remote backup metadata exceeds the size limit")
    return payload


def _read_bounded(body: _StreamingBody, maximum: int) -> bytes:
    payload = body.read(maximum + 1)
    if len(payload) > maximum:
        raise ValueError("remote backup metadata exceeds the size limit")
    return payload


def _is_not_found(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = str(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
    return code in _NOT_FOUND_CODES or status == "404"


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError("remote download write made no progress")
        written += count


def _configuration_error(detail: str) -> ConfigurationError:
    return ConfigurationError(
        detail,
        production_changed=False,
        previous_application_running=None,
        next_safe_action="Correct remote backup configuration and retry.",
    )
