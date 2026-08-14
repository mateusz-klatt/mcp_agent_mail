"""Restoring an archive bundle must never delete the live database.

`_restore_bundle_into_archive` calls `shutil.rmtree(target_root)` before
cloning the bundle back. In the deployed layout the database lives inside
that root (`STORAGE_ROOT=/data/mailbox`), so a restore would delete the
database the surrounding operation had just rebuilt -- and with more than
one project the second iteration overwrites `.pre-restore`, making it
unrecoverable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from git import Repo

from mcp_agent_mail.storage import _restore_bundle_into_archive


def _archive_with_bundle(tmp_path: Path) -> tuple[Path, Path]:
    """Build an archive root holding one commit, and a bundle of it."""
    root = tmp_path / "mailbox"
    root.mkdir()
    repository = Repo.init(root)
    document = root / "message.md"
    document.write_text("body\n", encoding="utf-8", newline="")
    repository.index.add([str(document)])
    repository.index.commit("seed")
    bundle = tmp_path / "archive.bundle"
    repository.git.bundle("create", str(bundle), "--all")
    repository.close()
    return root, bundle


def _database_at(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (x)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    connection.close()
    return path


def test_restore_refuses_when_the_database_is_inside_the_archive_root(
    tmp_path: Path,
) -> None:
    root, bundle = _archive_with_bundle(tmp_path)
    database = _database_at(root / "storage.sqlite3")

    with pytest.raises(RuntimeError, match="lives inside it"):
        _restore_bundle_into_archive(bundle, root, database)

    assert database.is_file(), "the refusal must happen before anything is removed"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT x FROM t").fetchone() == (1,)


def test_restore_still_works_when_the_database_lives_outside(tmp_path: Path) -> None:
    """Negative control: the guard must not refuse the supported layout.

    Without this, the test above would pass just as happily if the function
    had been changed to refuse every restore.
    """
    root, bundle = _archive_with_bundle(tmp_path)
    outside = tmp_path / "db"
    outside.mkdir()
    database = _database_at(outside / "storage.sqlite3")

    _restore_bundle_into_archive(bundle, root, database)

    assert database.is_file()
    assert (root / "message.md").read_text(encoding="utf-8") == "body\n"


def test_restore_without_a_database_is_unchanged(tmp_path: Path) -> None:
    root, bundle = _archive_with_bundle(tmp_path)

    _restore_bundle_into_archive(bundle, root, None)

    assert (root / "message.md").read_text(encoding="utf-8") == "body\n"
