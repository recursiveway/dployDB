"""Backup storage adapters."""

from dploydb.storage.base import BackupStorage
from dploydb.storage.local import LocalBackupStorage

__all__ = ["BackupStorage", "LocalBackupStorage"]
