"""Local backup/restore helpers (spec §14) + CLI.

Usage:
    python -m backend.jobs.backup          # create a backup now
    python -m backend.jobs.backup --list   # list backups
"""
import os
import sqlite3
import sys
from datetime import datetime

import backend.config as config

BACKUP_RETENTION = 14


def _db_path():
    return os.getenv("DB_PATH", config.DB_PATH)


def _backup_dir():
    directory = os.getenv("BACKUP_DIR", config.BACKUP_DIR)
    os.makedirs(directory, exist_ok=True)
    return directory


def create_backup():
    """Consistent snapshot of the live DB via SQLite's backup API."""
    source = sqlite3.connect(_db_path())
    filename = f"school_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    target_path = os.path.join(_backup_dir(), filename)
    # Avoid clobbering a backup created in the same second.
    counter = 1
    while os.path.exists(target_path):
        filename = f"school_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{counter}.db"
        target_path = os.path.join(_backup_dir(), filename)
        counter += 1

    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    _prune_backups()
    return filename


def _backup_entries():
    directory = _backup_dir()
    items = []
    for name in os.listdir(directory):
        if not name.endswith(".db"):
            continue
        path = os.path.join(directory, name)
        stat = os.stat(path)
        items.append({
            "filename": name,
            "size_bytes": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "_mtime": stat.st_mtime,
        })
    # mtime is the source of truth for same-second backups; the filename is
    # only a stable tie-breaker.
    items.sort(key=lambda item: (item["_mtime"], item["filename"]), reverse=True)
    return items


def _prune_backups(keep=BACKUP_RETENTION):
    """Keep only the newest ``keep`` local snapshots."""
    for item in _backup_entries()[keep:]:
        try:
            os.remove(os.path.join(_backup_dir(), item["filename"]))
        except OSError:
            pass


def list_backups():
    return [
        {key: value for key, value in item.items() if key != "_mtime"}
        for item in _backup_entries()
    ]


def _safe_backup_path(filename):
    """Resolve a backup filename, rejecting traversal."""
    directory = os.path.abspath(_backup_dir())
    path = os.path.abspath(os.path.join(directory, os.path.basename(filename)))
    if not path.startswith(directory + os.sep):
        return None
    if not os.path.isfile(path):
        return None
    return path


def restore_backup(filename):
    """Copy a backup's contents into the live DB (SQLite backup API, so open
    connections see the restored data). Returns False if not found."""
    source_path = _safe_backup_path(filename)
    if not source_path:
        return False

    source = sqlite3.connect(source_path)
    target = sqlite3.connect(_db_path())
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return True


if __name__ == "__main__":
    if "--list" in sys.argv:
        for item in list_backups():
            print(f"{item['filename']}  ({item['size_bytes']} bytes)")
    else:
        print(f"Backup created: {create_backup()}")
