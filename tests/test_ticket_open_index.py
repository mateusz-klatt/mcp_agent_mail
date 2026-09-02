"""``idx_tickets_project_open`` must be walkable by the query it was built for.

An index that the planner declines to use is worse than no index: it costs every
write, and its presence is what stops anyone asking why the read is slow. The
shape shipped in 0002ticketing was declared all-ASC while ``list_tickets`` orders
``priority ASC, updated_ts DESC, id DESC``, and SQLite walks an index for an
ORDER BY only when the directions all match or are all exactly opposite.

So these tests assert the *plan*, not the schema text. A test that only checked
the DDL contained "DESC" would keep passing if the ordering in ``tickets.py``
changed underneath it, which is precisely the drift that produced the defect.
Each test therefore carries its own sabotage control: the old shape is put back
under the same name and the assertion is shown to fail on it.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mcp_agent_mail import db as dbmod

# Mirrors tickets.list_tickets: predicates then `priority ASC, updated_ts DESC, id DESC`.
_CANONICAL = (
    "SELECT * FROM tickets WHERE project_id = 1 AND closed_ts IS NULL "
    "ORDER BY priority ASC, updated_ts DESC, id DESC LIMIT 50"
)
_OLD_SHAPE = (
    "CREATE INDEX idx_tickets_project_open ON tickets "
    "(project_id, priority, updated_ts) WHERE closed_ts IS NULL"
)


async def _build_schema(database: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()
    dbmod.reset_database_state()
    await dbmod.ensure_schema()
    await dbmod.get_engine().dispose()


def _plan(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute("EXPLAIN QUERY PLAN " + _CANONICAL).fetchall()
    finally:
        connection.close()
    return "\n".join(str(row[-1]) for row in rows)


def _index_sql(database: Path) -> str | None:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_tickets_project_open",),
        ).fetchone()
        return row[0] if row else None
    finally:
        connection.close()


def _install_old_shape(database: Path) -> None:
    """Put the 0002ticketing index back, under its own name. The production state."""
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP INDEX IF EXISTS idx_tickets_project_open")
        connection.execute(_OLD_SHAPE)
        connection.commit()
    finally:
        connection.close()


def test_the_hot_query_walks_its_own_index_instead_of_sorting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical `list_tickets` order is served by the index, with no sort."""
    database = tmp_path / "plan.db"
    asyncio.run(_build_schema(database, monkeypatch))
    dbmod.reset_database_state()

    plan = _plan(database)
    assert "idx_tickets_project_open" in plan, plan
    assert "TEMP B-TREE" not in plan, plan

    # Sabotage control. Without it this test would also pass against a planner
    # that never sorts anything, and would not be evidence about direction at all.
    _install_old_shape(database)
    sabotaged = _plan(database)
    assert "TEMP B-TREE" in sabotaged, (
        "the all-ASC shape no longer forces a sort, so this test has stopped "
        f"measuring what it claims to measure: {sabotaged}"
    )
    assert "idx_tickets_project_open" not in sabotaged, sabotaged


def test_an_already_deployed_database_has_the_index_rebuilt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0003 repairs a database that already carries the old shape.

    This is the only case that matters in production and the only one a model
    edit cannot reach: ``create_all(checkfirst=True)`` skips an existing table
    wholesale and will not reconcile its indexes.
    """
    database = tmp_path / "deployed.db"
    asyncio.run(_build_schema(database, monkeypatch))
    dbmod.reset_database_state()

    # Rewind to exactly what production looked like before this revision.
    _install_old_shape(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE alembic_version SET version_num = '0002ticketing'")
        connection.commit()
    finally:
        connection.close()
    assert "DESC" not in (_index_sql(database) or ""), "the rewind did not take"
    assert "TEMP B-TREE" in _plan(database), "the rewind did not restore the defect"

    asyncio.run(_build_schema(database, monkeypatch))
    dbmod.reset_database_state()

    rebuilt = _index_sql(database)
    assert rebuilt is not None and "DESC" in rebuilt, rebuilt
    assert "TEMP B-TREE" not in _plan(database), _plan(database)


def test_the_revision_is_idempotent_when_the_new_shape_is_already_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replaying 0003 over its own result is inert, not an error.

    ``test_alembic_baseline`` replays revisions against databases the current
    models have already built, so a revision that assumed the old shape was
    present would fail there rather than in production.
    """
    database = tmp_path / "replay.db"
    asyncio.run(_build_schema(database, monkeypatch))
    dbmod.reset_database_state()
    before = _index_sql(database)

    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE alembic_version SET version_num = '0002ticketing'")
        connection.commit()
    finally:
        connection.close()

    asyncio.run(_build_schema(database, monkeypatch))
    dbmod.reset_database_state()

    assert _index_sql(database) == before
    assert "TEMP B-TREE" not in _plan(database), _plan(database)
