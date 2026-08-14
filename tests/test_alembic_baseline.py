"""Alembic is introduced into a database that already exists in production.

The whole risk of that operation sits in one branch: where a database gets
stamped when it first comes under Alembic's control. Stamp a pre-existing
database at head and every future migration is skipped against it; stamp a
freshly created one at the baseline and the next revision tries to add columns
that ``create_all`` has already built from current models.

Today the baseline happens to *be* head, so both paths land on the same string
and an end-state assertion would prove nothing. These tests assert the decision
itself.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mcp_agent_mail import db as dbmod


class _RecordingContext:
    """Stands in for ``MigrationContext`` with a known current revision."""

    def __init__(self, current: str | None) -> None:
        self._current = current

    def get_current_revision(self) -> str | None:
        return self._current


def _capture_stamp(monkeypatch: pytest.MonkeyPatch, *, current: str | None) -> list[str]:
    """Record what revision the alignment stamps, without touching a database."""
    stamped: list[str] = []
    monkeypatch.setattr(
        dbmod.MigrationContext,
        "configure",
        staticmethod(lambda _connection: _RecordingContext(current)),
    )
    monkeypatch.setattr(
        dbmod.alembic_command,
        "stamp",
        lambda _config, revision: stamped.append(revision),
    )
    monkeypatch.setattr(dbmod.alembic_command, "upgrade", lambda _config, _revision: None)
    return stamped


def test_a_fresh_database_is_stamped_at_head(monkeypatch: pytest.MonkeyPatch) -> None:
    # `create_all` built it from *current* models, so it already contains
    # everything any existing revision would add. Replaying them would fail.
    stamped = _capture_stamp(monkeypatch, current=None)

    dbmod._align_alembic_version(object(), was_fresh=True)

    assert stamped == ["head"]


def test_a_pre_alembic_database_is_stamped_at_the_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # It predates Alembic and matches the baseline, so every later revision
    # must still apply to it.
    stamped = _capture_stamp(monkeypatch, current=None)

    dbmod._align_alembic_version(object(), was_fresh=False)

    assert stamped == [dbmod._BASELINE_REVISION]


def test_an_already_tracked_database_is_never_restamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Re-stamping would silently move a database's recorded position without
    # running anything, which is how migrations get skipped forever.
    stamped = _capture_stamp(monkeypatch, current=dbmod._BASELINE_REVISION)

    dbmod._align_alembic_version(object(), was_fresh=True)

    assert stamped == []


def _stamp_in(database: Path) -> str | None:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()


async def _build_schema(database: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()
    dbmod.reset_database_state()
    await dbmod.ensure_schema()
    await dbmod.get_engine().dispose()


def test_ensure_schema_brings_a_database_under_alembic_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: a database built by `ensure_schema` is tracked, and re-running is inert."""
    database = tmp_path / "tracked.sqlite3"

    asyncio.run(_build_schema(database, monkeypatch))
    first = _stamp_in(database)
    assert first is not None, "ensure_schema left the database untracked by Alembic"

    asyncio.run(_build_schema(database, monkeypatch))
    assert _stamp_in(database) == first

    # A database that predates Alembic differs only by the absence of the
    # version table; ensure_schema must adopt it rather than rebuild it.
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE alembic_version")
    connection.commit()
    connection.close()
    assert _stamp_in(database) is None

    asyncio.run(_build_schema(database, monkeypatch))
    assert _stamp_in(database) == dbmod._BASELINE_REVISION

    dbmod.reset_database_state()
