"""ticketing: bring the Alembic ledger up to the ticketing tables

Revision ID: 0002ticketing
Revises: 0001baseline
Created: 2026-08-31

Reconciliation, not creation, and the distinction is structural rather than a
workaround for this one change.

``ensure_schema`` runs ``SQLModel.metadata.create_all`` (db.py:1422) and only
afterwards ``alembic upgrade head`` (db.py:1445-1448 -> db.py:1386). So on every
database that is not brand new -- production included, stamped ``0001baseline``
-- these four tables already exist by the time this body runs. A bare
``op.create_table`` would raise ``table tickets already exists``; the
``create_all`` commit sits in its own transaction and would *not* roll back;
``alembic_version`` would stay at the baseline; and ``ensure_schema`` would
raise on every one of the ~30 entry points that call it, identically on every
restart. ``@retry_on_db_lock`` would not intervene, because ``_is_lock_error``
(db.py:218-230) matches only locked / busy / unable-to-open / disk-i-o.

While ``create_all`` runs first, *every* table-creating revision from here on
must be a no-op when the table is already present.

The index loop is not decoration. ``create_all(checkfirst=True)`` skips an
EXISTING table wholesale and will **not** add a newly model-declared ``Index``
to it, so an index added to a shipped table would silently never exist in
production. db.py:2559-2593 is thirteen hand-written ``CREATE INDEX IF NOT
EXISTS`` statements standing as proof of what happens when nobody notices.

No data seed may ever live in a revision body. On a fresh database ``was_fresh``
is True, db.py:1385 stamps *head*, and the subsequent ``upgrade head`` is a
no-op -- so a seed placed here would run on production and on no developer
machine. This revision seeds nothing; the ticket vocabularies are module
constants in ``tickets.py``.

ROLLBACK RUNBOOK. ``downgrade()`` is not the rollback story. Nothing in this
repository invokes ``alembic downgrade``; ``alembic_command`` appears in ``src/``
at exactly two lines, db.py:1385 and db.py:1386. Once this revision has stamped
a database, reverting to an image whose ``versions/`` directory lacks it makes
``_align_alembic_version`` skip the stamp (a version row exists) and then raise
``CommandError: Can't locate revision identified by '0002ticketing'`` -- on every
entry point, on every restart. On a fleet whose deploy is a container restart and
whose reflex on a bad deploy is an image revert, that is the more probable
outage of the two.

    Before deploy, record the stamp and take the repo's own backup::

        sqlite3 -readonly <db> "SELECT version_num FROM alembic_version;"

    To roll back, either redeploy the ticketing image (this revision is a no-op
    the second time), or revert the image and re-stamp *before* starting the
    container::

        sqlite3 <db> "UPDATE alembic_version SET version_num='0001baseline';"

    The orphan ticketing tables are harmless: ``create_all`` never drops, and
    nothing in the older image reads them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel import SQLModel

from mcp_agent_mail import models as _models  # noqa: F401  (registers metadata)

revision: str = "0002ticketing"
down_revision: str | None = "0001baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Creation order: a parent before anything that references it.
_TICKETING_TABLES: tuple[str, ...] = (
    "ticket_sequences",
    "tickets",
    "ticket_links",
    "ticket_events",
)


def _existing_index_names(bind: sa.engine.Connection, table_name: str) -> set[str]:
    """Return the index names actually present on ``table_name``.

    ``Inspector.get_indexes`` is not usable here on its own, and neither is the
    obvious alternative. Measured on SQLAlchemy 2.x against SQLite: for the
    expression index ``uq_tickets_key_nocase`` (``lower(key)``) the inspector
    emits ``SAWarning: Skipped unsupported reflection of expression-based index``
    and **omits it from the result**, while ``dialect.has_index`` returns
    ``False`` for that same index -- so ``Index.create(bind, checkfirst=True)``
    would try to create it a second time and raise ``index ... already exists``.

    The catalog is the only source that answers correctly, which is also why
    db.py:2559-2593 writes its thirteen index statements by hand.
    """
    if bind.dialect.name == "sqlite":
        rows = bind.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = '%s'"
            % table_name.replace("'", "''")
        ).fetchall()
        return {row[0] for row in rows}
    return {
        name
        for name in (index["name"] for index in sa.inspect(bind).get_indexes(table_name))
        if name is not None
    }


def upgrade() -> None:
    """Create whatever ``create_all`` has not already created, then reconcile indexes."""
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names())

    missing = [SQLModel.metadata.tables[name] for name in _TICKETING_TABLES if name not in present]
    if missing:
        SQLModel.metadata.create_all(bind, tables=missing, checkfirst=False)

    for name in _TICKETING_TABLES:
        if name not in present:
            continue  # just created above, its indexes came with it
        existing = _existing_index_names(bind, name)
        for index in SQLModel.metadata.tables[name].indexes:
            if index.name not in existing:
                index.create(bind)


def downgrade() -> None:
    """Unreachable from the runtime; correct for a manual CLI invocation.

    Reverse foreign-key order. See the rollback runbook in the module docstring
    for what to do instead when an already-stamped database must go back.
    """
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names())
    for name in reversed(_TICKETING_TABLES):
        if name in present:
            op.drop_table(name)
