"""Tools that read the live database from a second process must not write to it.

A read-write connection from a second process is one half of the corruption
chain measured on 2026-08-14: on close it can checkpoint the WAL and unlink
`-wal`/`-shm`, and if the server has meanwhile lost its own POSIX locks it
does exactly that underneath a running server.

Every test here keeps a writer attached for the whole test, because that is
the only situation where the property means anything: with nobody else
attached, whichever connection closes last checkpoints and removes the WAL,
and that is correct -- there is no reader to strand.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_agent_mail.db import connect_sqlite_readonly
from mcp_agent_mail.share import create_sqlite_snapshot


@pytest.fixture
def live_database(tmp_path: Path) -> Iterator[Path]:
    """A WAL database with an attached writer, i.e. a stand-in for the server."""
    path = tmp_path / "storage.sqlite3"
    server = sqlite3.connect(path)
    server.execute("PRAGMA journal_mode=WAL")
    server.execute("CREATE TABLE t (x)")
    server.execute("INSERT INTO t VALUES ('checkpointed')")
    server.commit()
    server.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # Stays in the WAL because this connection never closes during the test,
    # so a snapshot that ignores the WAL would lose it.
    server.execute("INSERT INTO t VALUES ('only-in-wal')")
    server.commit()
    try:
        yield path
    finally:
        server.close()


def test_readonly_helper_refuses_writes(live_database: Path) -> None:
    connection = connect_sqlite_readonly(live_database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM t").fetchone() == (2,)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO t VALUES ('nope')")
    finally:
        connection.close()


def test_readonly_helper_accepts_relative_database_path(
    live_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(live_database.parent)

    connection = connect_sqlite_readonly(Path(live_database.name))
    try:
        assert connection.execute("SELECT COUNT(*) FROM t").fetchone() == (2,)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO t VALUES ('nope')")
    finally:
        connection.close()


def test_readonly_helper_leaves_a_running_servers_wal_alone(live_database: Path) -> None:
    wal = Path(f"{live_database}-wal")
    size_before = wal.stat().st_size
    assert size_before > 0, "the fixture must leave data in the WAL to measure anything"

    connect_sqlite_readonly(live_database).close()

    assert wal.exists(), "the read-only path must not unlink a live WAL"
    assert wal.stat().st_size == size_before, "nor checkpoint it away"


def _connect_and_close_in_another_process(database: Path, *, readonly: bool) -> None:
    """Attach and detach from a genuinely separate process.

    POSIX locks are per-process, so an in-process "intruder" shares the
    server's lock state and cannot reproduce the damage -- measured. The
    corruption needed a second OS process, which is what the backup, doctor
    and share paths are.
    """
    import subprocess
    import sys

    if readonly:
        program = (
            "import sqlite3, pathlib, sys;"
            "p = pathlib.Path(sys.argv[1]);"
            "c = sqlite3.connect(f'{p.as_uri()}?mode=ro', uri=True);"
            "c.execute('PRAGMA query_only=ON');"
            "c.close()"
        )
    else:
        program = (
            "import sqlite3, sys;"
            "c = sqlite3.connect(sys.argv[1]);"
            "c.close()"
        )
    subprocess.run(
        [sys.executable, "-c", program, str(database)],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _drop_the_servers_locks(database: Path) -> None:
    """Reproduce the other half of the chain: the server loses its POSIX locks.

    Opening and closing any descriptor to a file releases every lock the
    process holds on it. That is what the archive fsync walk used to do.
    """
    import os

    for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if path.exists():
            descriptor = os.open(path, os.O_RDONLY)
            os.close(descriptor)


def test_readonly_is_safe_even_after_the_servers_locks_are_lost(
    live_database: Path,
) -> None:
    """Defense in depth: safe even when the other half of the chain fails.

    What is measured and reproducible is the lock loss itself (pinned in
    tests/test_archive_fsync_lock_durability.py). Whether a read-write second
    process then unlinks the sidecars was reported during the incident
    analysis but does NOT reproduce here -- so this file does not assert it.
    It asserts the part that is ours to control: this path leaves the live
    WAL untouched even in the worst state the server can be in.
    """
    wal = Path(f"{live_database}-wal")
    size_before = wal.stat().st_size
    assert size_before > 0

    _drop_the_servers_locks(live_database)
    _connect_and_close_in_another_process(live_database, readonly=True)

    assert wal.exists(), "the read-only path must not remove the WAL even then"
    assert wal.stat().st_size == size_before


def test_snapshot_keeps_wal_only_rows_without_checkpointing(
    live_database: Path,
    tmp_path: Path,
) -> None:
    """The snapshot stays complete after dropping its WAL checkpoint.

    `create_sqlite_snapshot` used to run `PRAGMA wal_checkpoint(PASSIVE)` on
    the source. That is a write against a database another process owns, and
    it is unnecessary: the online backup API copies committed WAL frames.
    """
    destination = tmp_path / "snapshot.sqlite3"

    create_sqlite_snapshot(live_database, destination)

    with sqlite3.connect(destination) as snapshot:
        rows = {row[0] for row in snapshot.execute("SELECT x FROM t")}
    assert rows == {"checkpointed", "only-in-wal"}
