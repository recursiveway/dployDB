"""Path-safe local storage for immutable verified backups."""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from pathlib import Path
from typing import Final
from uuid import uuid4

from pydantic import ValidationError

from dploydb.errors import OperationFailedError, SafetyCheckError
from dploydb.models import BackupArtifact, BackupMetadata

DIRECTORY_MODE: Final = 0o700
FILE_MODE: Final = 0o600
MAX_METADATA_BYTES: Final = 128 * 1024
_BACKUP_ID = re.compile(r"^backup_[0-9a-f]{32}$")
_IGNORABLE_DIRECTORY_FSYNC_ERRORS = frozenset({errno.EINVAL, errno.ENOTSUP})


class LocalBackupStorage:
    """Publish backup metadata last so it is the durable success marker."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root == Path(root.anchor):
            raise ValueError("local backup directory must be an absolute non-root path")
        self.root = root

    def ensure_layout(self) -> None:
        if self.root.is_symlink():
            raise self._storage_error("refusing a symlinked backup directory")
        try:
            self.root.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
            details = self.root.stat()
        except OSError as exc:
            raise self._storage_error(f"backup directory could not be created: {exc}") from None
        mode = stat.S_IMODE(details.st_mode)
        if not stat.S_ISDIR(details.st_mode) or mode != DIRECTORY_MODE:
            raise self._storage_error(
                f"backup directory must be a mode-0700 directory, found {mode:04o}"
            )

    def create_staging_database(self, backup_id: str) -> Path:
        self._validate_id(backup_id)
        self.ensure_layout()
        path = self.root / f".{backup_id}.{uuid4().hex}.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(path, flags, FILE_MODE)
            os.fchmod(descriptor, FILE_MODE)
            os.close(descriptor)
            descriptor = -1
            self._fsync_directory()
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise self._storage_error(f"backup staging file could not be created: {exc}") from None
        return path

    def put(self, staged_database: Path, metadata: BackupMetadata) -> BackupArtifact:
        self.ensure_layout()
        self._validate_id(metadata.backup_id)
        self._validate_staged_database(staged_database, metadata.backup_id)
        database_path = self.root / metadata.database_file_name
        metadata_path = self.root / f"{metadata.backup_id}.json"
        if database_path.exists() or database_path.is_symlink():
            raise self._storage_error(f"backup database already exists: {database_path}")
        if metadata_path.exists() or metadata_path.is_symlink():
            raise self._storage_error(f"backup metadata already exists: {metadata_path}")

        database_published = False
        try:
            os.link(staged_database, database_path, follow_symlinks=False)
            database_published = True
            staged_database.unlink()
            os.chmod(database_path, FILE_MODE, follow_symlinks=False)
            self._fsync_directory()
            self._publish_metadata(metadata_path, metadata)
        except (OSError, OperationFailedError) as exc:
            cleanup_error: OSError | None = None
            if database_published:
                try:
                    database_path.unlink(missing_ok=True)
                    self._fsync_directory()
                except OSError as cleanup:
                    cleanup_error = cleanup
            detail = f"backup publication failed: {exc}"
            if cleanup_error is not None:
                detail += f"; incomplete database cleanup also failed: {cleanup_error}"
            raise self._storage_error(detail) from None
        return self.get(metadata.backup_id)

    def get(self, backup_id: str) -> BackupArtifact:
        metadata = self.verify_metadata(backup_id)
        database_path = self.root / metadata.database_file_name
        self._validate_committed_file(database_path, "backup database")
        return BackupArtifact(
            metadata=metadata,
            database_path=database_path,
            metadata_path=self.root / f"{backup_id}.json",
        )

    def exists(self, backup_id: str) -> bool:
        try:
            self.get(backup_id)
        except SafetyCheckError:
            return False
        return True

    def list(self) -> tuple[BackupMetadata, ...]:
        self._validate_layout()
        metadata: list[BackupMetadata] = []
        for path in sorted(self.root.glob("backup_*.json")):
            backup_id = path.name.removesuffix(".json")
            metadata.append(self.verify_metadata(backup_id))
        return tuple(metadata)

    def delete(self, backup_id: str) -> None:
        artifact = self.get(backup_id)
        try:
            artifact.metadata_path.unlink()
            self._fsync_directory()
            artifact.database_path.unlink()
            self._fsync_directory()
        except OSError as exc:
            raise self._storage_error(f"backup deletion failed: {exc}") from None

    def verify_metadata(self, backup_id: str) -> BackupMetadata:
        self._validate_id(backup_id)
        self._validate_layout()
        path = self.root / f"{backup_id}.json"
        self._validate_committed_file(path, "backup metadata")
        try:
            details = path.stat()
            if details.st_size <= 0 or details.st_size > MAX_METADATA_BYTES:
                raise OSError("metadata has an invalid size")
            payload = path.read_bytes()
            if not payload.endswith(b"\n"):
                raise OSError("metadata is truncated")
            metadata = BackupMetadata.model_validate_json(payload)
        except (OSError, ValidationError) as exc:
            raise self._verification_error(f"backup metadata is invalid: {exc}", path) from None
        if metadata.backup_id != backup_id:
            raise self._verification_error("backup metadata ID does not match its path", path)
        return metadata

    def _publish_metadata(self, path: Path, metadata: BackupMetadata) -> None:
        payload = (
            json.dumps(
                metadata.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        if len(payload) > MAX_METADATA_BYTES:
            raise self._storage_error("serialized backup metadata exceeds the size limit")
        temporary = self.root / f".{metadata.backup_id}.{uuid4().hex}.json.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, FILE_MODE)
            os.fchmod(descriptor, FILE_MODE)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
            self._fsync_directory()
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise self._storage_error(f"backup metadata could not be published: {exc}") from None

    def _validate_layout(self) -> None:
        if self.root.is_symlink():
            raise self._verification_error("backup directory is a symlink", self.root)
        try:
            details = self.root.stat()
        except OSError as exc:
            raise self._verification_error(
                f"backup directory is unavailable: {exc}", self.root
            ) from None
        mode = stat.S_IMODE(details.st_mode)
        if not stat.S_ISDIR(details.st_mode) or mode != DIRECTORY_MODE:
            raise self._verification_error(
                f"backup directory must be a mode-0700 directory, found {mode:04o}",
                self.root,
            )

    def _validate_staged_database(self, path: Path, backup_id: str) -> None:
        try:
            relative = path.relative_to(self.root)
            details = path.lstat()
        except (OSError, ValueError) as exc:
            raise self._storage_error(f"backup staging file is unsafe: {exc}") from None
        if (
            relative.parent != Path(".")
            or not path.name.startswith(f".{backup_id}.")
            or path.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != FILE_MODE
        ):
            raise self._storage_error("backup staging file is not a private managed file")

    def _validate_committed_file(self, path: Path, label: str) -> None:
        try:
            details = path.lstat()
        except OSError as exc:
            raise self._verification_error(f"{label} is unavailable: {exc}", path) from None
        if path.is_symlink() or not stat.S_ISREG(details.st_mode):
            raise self._verification_error(f"{label} must be a regular non-symlink file", path)
        mode = stat.S_IMODE(details.st_mode)
        if mode != FILE_MODE:
            raise self._verification_error(f"{label} must have mode 0600, found {mode:04o}", path)

    def _validate_id(self, backup_id: str) -> None:
        if _BACKUP_ID.fullmatch(backup_id) is None:
            raise self._verification_error("backup ID is invalid", self.root)

    def _fsync_directory(self) -> None:
        descriptor = -1
        try:
            descriptor = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _IGNORABLE_DIRECTORY_FSYNC_ERRORS:
                raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _storage_error(self, detail: str) -> OperationFailedError:
        return OperationFailedError(
            detail,
            production_changed=False,
            previous_application_running=None,
            log_path=self.root,
            next_safe_action="Correct local backup storage, then retry the backup.",
        )

    @staticmethod
    def _verification_error(detail: str, path: Path) -> SafetyCheckError:
        return SafetyCheckError(
            detail,
            production_changed=False,
            previous_application_running=None,
            log_path=path,
            next_safe_action="Use another committed verified backup or create a new backup.",
        )


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError("metadata write made no progress")
        written += count
