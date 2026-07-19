"""Narrow storage contract for immutable verified backups."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from dploydb.models import (
    BackupArtifact,
    BackupMetadata,
    RemoteBackupArtifact,
    RemoteBackupMetadata,
)


class BackupStorage(Protocol):
    """Storage behavior consumed by the backup engine."""

    def create_staging_database(self, backup_id: str) -> Path: ...

    def put(self, staged_database: Path, metadata: BackupMetadata) -> BackupArtifact: ...

    def get(self, backup_id: str) -> BackupArtifact: ...

    def exists(self, backup_id: str) -> bool: ...

    def list(self) -> tuple[BackupMetadata, ...]: ...

    def delete(self, backup_id: str) -> None: ...

    def verify_metadata(self, backup_id: str) -> BackupMetadata: ...


class RemoteBackupStorage(Protocol):
    """Off-server replica behavior for already verified local backups."""

    def put(
        self,
        artifact: BackupArtifact,
        *,
        release_id: str | None = None,
    ) -> RemoteBackupArtifact: ...

    def download(self, backup_id: str, destination: Path) -> RemoteBackupArtifact: ...

    def exists(self, backup_id: str) -> bool: ...

    def list(self) -> tuple[RemoteBackupMetadata, ...]: ...

    def delete(self, backup_id: str) -> None: ...

    def verify_metadata(self, backup_id: str) -> RemoteBackupMetadata: ...
