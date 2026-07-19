"""Protected, idempotent retention for verified local and remote backups."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from dploydb.errors import SafetyCheckError
from dploydb.models import BackupMetadata, RemoteBackupMetadata
from dploydb.releases import ReleaseHistorySnapshot


class LocalRetentionStorage(Protocol):
    """Small local inventory/deletion boundary used by retention."""

    def list(self) -> tuple[BackupMetadata, ...]: ...

    def delete(self, backup_id: str) -> None: ...


class RemoteRetentionStorage(Protocol):
    """Small remote inventory/deletion boundary used by retention."""

    def list(self) -> tuple[RemoteBackupMetadata, ...]: ...

    def delete(self, backup_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """Non-secret evidence from one completed retention pass."""

    protected_backup_ids: tuple[str, ...]
    local_deleted: tuple[str, ...]
    remote_deleted: tuple[str, ...]
    local_retained: tuple[str, ...]
    remote_retained: tuple[str, ...]

    def as_evidence(self) -> dict[str, object]:
        return {
            "protected_backup_ids": list(self.protected_backup_ids),
            "local_deleted": list(self.local_deleted),
            "remote_deleted": list(self.remote_deleted),
            "local_retained": list(self.local_retained),
            "remote_retained": list(self.remote_retained),
        }


def protected_backup_ids(
    history: ReleaseHistorySnapshot,
    *,
    project: str,
) -> frozenset[str]:
    """Resolve backup protection from one immutable validated history snapshot."""
    pointers = history.pointers
    if pointers is None:
        return frozenset()
    selected_release_ids = [pointers.active_release_id]
    if pointers.previous_release_id is not None:
        selected_release_ids.append(pointers.previous_release_id)

    protected: set[str] = set()
    for release_id in selected_release_ids:
        release = history.find(release_id)
        if release is None:
            raise _retention_safety_error(
                "release pointers select a manifest missing from the retention snapshot"
            )
        if release.project != project:
            raise _retention_safety_error(
                "release pointers select a different project; backup retention was refused"
            )
        for backup_id in (release.rehearsal_backup_id, release.final_backup_id):
            if backup_id is not None:
                protected.add(backup_id)
    return frozenset(protected)


def apply_retention(
    *,
    project: str,
    keep_last: int,
    history: ReleaseHistorySnapshot,
    local_storage: LocalRetentionStorage,
    remote_storage: RemoteRetentionStorage | None = None,
) -> RetentionResult:
    """Delete old unprotected backups from fully verified inventories."""
    if keep_last < 1:
        raise ValueError("keep_last must be positive")
    protected = protected_backup_ids(history, project=project)
    local_records = local_storage.list()
    remote_records = () if remote_storage is None else remote_storage.list()

    local_delete, local_retain = _plan_inventory(
        ((record.backup_id, record.project, record.completed_at) for record in local_records),
        project=project,
        protected=protected,
        keep_last=keep_last,
    )
    remote_delete, remote_retain = _plan_inventory(
        (
            (record.backup.backup_id, record.backup.project, record.backup.completed_at)
            for record in remote_records
        ),
        project=project,
        protected=protected,
        keep_last=keep_last,
    )

    for backup_id in local_delete:
        local_storage.delete(backup_id)
    if remote_storage is not None:
        for backup_id in remote_delete:
            remote_storage.delete(backup_id)

    return RetentionResult(
        protected_backup_ids=tuple(sorted(protected)),
        local_deleted=local_delete,
        remote_deleted=remote_delete,
        local_retained=local_retain,
        remote_retained=remote_retain,
    )


def _plan_inventory(
    records: Iterable[tuple[str, str, datetime]],
    *,
    project: str,
    protected: frozenset[str],
    keep_last: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    materialized = tuple(records)
    identities = [record[0] for record in materialized]
    if len(identities) != len(set(identities)):
        raise _retention_safety_error("backup inventory contains duplicate identities")

    managed = [record for record in materialized if record[1] == project]
    foreign_ids = {record[0] for record in materialized if record[1] != project}
    unprotected = [record for record in managed if record[0] not in protected]
    unprotected.sort(key=lambda record: (record[2], record[0]), reverse=True)
    keep_unprotected = {record[0] for record in unprotected[:keep_last]}
    retained = {
        record[0]
        for record in materialized
        if record[0] in protected or record[0] in keep_unprotected or record[0] in foreign_ids
    }
    deleted = sorted(
        (record for record in managed if record[0] not in retained),
        key=lambda record: (record[2], record[0]),
    )
    return tuple(record[0] for record in deleted), tuple(sorted(retained))


def _retention_safety_error(detail: str) -> SafetyCheckError:
    return SafetyCheckError(
        detail,
        production_changed=False,
        previous_application_running=None,
        next_safe_action=(
            "Preserve all backups, repair release or backup inventory evidence, then retry "
            "retention."
        ),
    )
