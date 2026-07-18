"""Integration proof that SQLite online backup remains valid with a live writer."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from dploydb.backup import create_verified_backup, verify_backup
from dploydb.models import BackupPurpose
from dploydb.storage.local import LocalBackupStorage


def test_live_writer_snapshot_opens_and_passes_checks(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO events(value) VALUES ('initial')")

    stop = threading.Event()
    ready = threading.Event()

    def write_forever() -> None:
        with sqlite3.connect(source, timeout=5) as connection:
            index = 0
            while not stop.is_set():
                connection.execute("INSERT INTO events(value) VALUES (?)", (f"event-{index}",))
                connection.commit()
                index += 1
                ready.set()
                time.sleep(0.001)

    writer = threading.Thread(target=write_forever, daemon=True)
    writer.start()
    assert ready.wait(5)
    try:
        storage = LocalBackupStorage(tmp_path / "backups")
        artifact = create_verified_backup(
            source,
            project="live-writer",
            purpose=BackupPurpose.STANDALONE,
            storage=storage,
        )
    finally:
        stop.set()
        writer.join(timeout=5)

    assert not writer.is_alive()
    assert verify_backup(storage, artifact.metadata.backup_id) == artifact
    with sqlite3.connect(f"{artifact.database_path.as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= 2
