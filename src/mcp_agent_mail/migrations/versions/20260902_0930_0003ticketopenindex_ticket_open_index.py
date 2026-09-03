"""ticketing: rebuild idx_tickets_project_open in the order list_tickets asks for

Revision ID: 0003ticketopenindex
Revises: 0002ticketing
Created: 2026-09-02

The index shipped in 0002ticketing was declared all-ASC
(``project_id, priority, updated_ts``) while ``list_tickets`` orders
``priority ASC, updated_ts DESC, id DESC``. SQLite walks an index to satisfy an
ORDER BY only when the requested directions all match the index or are all
exactly its opposite; mixed directions match neither. So the index built for
that query could not be used by it.

Measured on SQLite 3.50.4, EXPLAIN QUERY PLAN of the canonical order::

    before   SEARCH tickets USING INDEX ix_tickets_project_id (project_id=?)
             USE TEMP B-TREE FOR ORDER BY
    after    SEARCH tickets USING INDEX idx_tickets_project_open (project_id=?)

Two consequences rather than one. The obvious one is the sort. The other is that
the planner's fallback, ``ix_tickets_project_id``, is **not** partial -- so the
``closed_ts IS NULL`` restriction was applied as a filter over closed history
instead of by never indexing it, which is the whole reason the index is partial.

Controls that isolate direction as the cause: the same all-ASC index *is* chosen
for ``priority ASC, updated_ts ASC`` and for ``priority DESC, updated_ts DESC``.
And ``id DESC`` earns its place -- dropping it leaves
``USE TEMP B-TREE FOR LAST TERM OF ORDER BY``.

WHY A REVISION AND NOT JUST THE MODEL CHANGE. ``create_all(checkfirst=True)``
skips an existing table wholesale and will not reconcile its indexes -- the point
0002ticketing's own docstring makes about newly declared indexes. ``tickets``
exists on every deployed database, so the model edit alone would leave the old
shape in place forever and be visible on developer machines only.

DROP-THEN-CREATE, unconditionally, rather than inspecting the stored SQL. The
operation is idempotent by construction, which matters because
``test_alembic_baseline`` replays revisions against a database where the new
shape may already be present. Comparing ``sqlite_master.sql`` texts instead would
make correctness depend on SQLAlchemy rendering DDL byte-identically across
versions, which is not a promise anyone made.

Rebuilding an index is not a table rewrite and holds no long lock; ``tickets``
carries a handful of rows on the only deployment that has it.

ROLLBACK. ``downgrade()`` restores the all-ASC shape so a manual CLI invocation
is correct, but as 0002ticketing's runbook explains, nothing in this repository
invokes ``alembic downgrade``: reverting the image is the real rollback and needs
a re-stamp *before* the container starts::

    sqlite3 <db> "UPDATE alembic_version SET version_num='0002ticketing';"

Leaving the new index in place under the older image is harmless -- it is a
superset-correct index for every query the older code writes, and SQLite never
requires an index to exist.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Index, text

revision: str = "0003ticketopenindex"
down_revision: str | None = "0002ticketing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "tickets"
_INDEX = "idx_tickets_project_open"


def _rebuild(bind: sa.engine.Connection, *, descending: bool) -> None:
    """Drop ``_INDEX`` if present and recreate it in the requested shape.

    The index is spelled out here rather than read from ``SQLModel.metadata`` on
    purpose: a revision must keep producing the shape it was written for even
    after the model moves on again, which is exactly the property 0002ticketing
    could not have (it reconciles whatever the models happen to say) and this one
    can, because it changes one named index rather than creating tables.
    """
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        return  # nothing to rebuild; create_all will produce the current shape

    op.execute(sa.text(f'DROP INDEX IF EXISTS "{_INDEX}"'))

    tail = (text("updated_ts DESC"), text("id DESC")) if descending else (text("updated_ts"),)
    table = sa.Table(
        _TABLE,
        sa.MetaData(),
        sa.Column("project_id", sa.Integer),
        sa.Column("priority", sa.Integer),
        sa.Column("updated_ts", sa.DateTime),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("closed_ts", sa.DateTime),
    )
    Index(
        _INDEX,
        table.c.project_id,
        table.c.priority,
        *tail,
        sqlite_where=text("closed_ts IS NULL"),
    ).create(bind)


def upgrade() -> None:
    """Give the hot query an index it can actually walk."""
    _rebuild(op.get_bind(), descending=True)


def downgrade() -> None:
    """Restore the all-ASC shape 0002ticketing shipped."""
    _rebuild(op.get_bind(), descending=False)
