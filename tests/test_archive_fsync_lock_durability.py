"""The archive fsync walk must not touch the live database.

`_fsync_readonly_file_sync` opens a descriptor and closes it. On POSIX,
closing ANY descriptor to a file releases every lock the process holds on
that file, so fsyncing the database made the server invisible as a reader:
another process could then checkpoint and unlink `-wal`/`-shm` underneath
it. Measured as the cause of three production corruptions on 2026-08-14.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from mcp_agent_mail.storage import _fsync_archive_initialization_tree_sync

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not Path("/proc/locks").exists(),
    reason="POSIX lock accounting is read from /proc/locks",
)


def _held_lock_count(paths: list[Path]) -> int:
    identities = set()
    for path in paths:
        try:
            status = path.stat()
        except FileNotFoundError:
            continue
        identities.add((status.st_dev, status.st_ino))
    held = 0
    for line in Path("/proc/locks").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        major, minor, inode = fields[5].split(":")
        try:
            device = (int(major, 16) << 8) | int(minor, 16)
            identity = (device, int(inode))
        except ValueError:
            continue
        if identity in identities:
            held += 1
    return held


def _archive_with_open_database(tmp_path: Path) -> tuple[Path, Path, sqlite3.Connection]:
    root = tmp_path / "mailbox"
    root.mkdir()
    (root / ".gitattributes").write_text("* -text\n", encoding="utf-8")
    database = root / "storage.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t (x)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    # Hold a read transaction open, the way the running server does.
    connection.isolation_level = None
    connection.execute("BEGIN")
    connection.execute("SELECT * FROM t").fetchall()
    return root, database, connection


def _sidecars(database: Path) -> list[Path]:
    return [
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
    ]


def test_fsync_walk_preserves_database_locks(tmp_path: Path) -> None:
    root, database, connection = _archive_with_open_database(tmp_path)
    try:
        before = _held_lock_count(_sidecars(database))
        assert before > 0, "the fixture must hold locks for this to measure anything"
        _fsync_archive_initialization_tree_sync(root, database)
        assert _held_lock_count(_sidecars(database)) == before
    finally:
        connection.close()


def test_fsync_walk_without_the_guard_drops_them(tmp_path: Path) -> None:
    """Negative control: without the exclusion the locks are silently lost.

    Pinning the defect keeps the guard above from passing for the wrong
    reason — e.g. if the fixture stopped holding locks at all.
    """
    root, database, connection = _archive_with_open_database(tmp_path)
    try:
        assert _held_lock_count(_sidecars(database)) > 0
        _fsync_archive_initialization_tree_sync(root, None)
        assert _held_lock_count(_sidecars(database)) == 0
    finally:
        connection.close()


def test_walk_still_persists_the_archive_files(tmp_path: Path) -> None:
    root, database, connection = _archive_with_open_database(tmp_path)
    try:
        document = root / "projects" / "example" / "message.md"
        document.parent.mkdir(parents=True)
        document.write_text("body\n", encoding="utf-8")
        _fsync_archive_initialization_tree_sync(root, database)
        assert document.read_text(encoding="utf-8") == "body\n"
        assert (root / ".gitattributes").exists()
    finally:
        connection.close()
