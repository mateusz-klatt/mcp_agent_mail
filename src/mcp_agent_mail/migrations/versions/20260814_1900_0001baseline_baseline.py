"""baseline: the schema as ``ensure_schema`` leaves it

Revision ID: 0001baseline
Revises:
Created: 2026-08-14

This revision is intentionally empty, and that emptiness is the point.

Alembic is being introduced into a project whose schema already exists in
production and is produced by ``db.ensure_schema()``: ``SQLModel.metadata.
create_all`` followed by three hand-written rebuild steps that SQLite forces
into a specific order (``_migrate_message_delivery_schema``,
``_migrate_agent_executions_schema``, ``_migrate_ui_users_locale_schema``) and
the FTS5 setup. Re-expressing all of that as an initial revision would mean
rewriting, untested, the one code path that every existing database has
already survived -- against live data, for no behavioural gain.

So the baseline names the state rather than building it. Every database, new
or old, reaches this point through ``ensure_schema``; from here on, schema
changes are Alembic revisions and nothing new is added to ``db.py``.

``ensure_schema`` stamps accordingly:

* a database that did not exist before this run is created by ``create_all``
  from *current* models, so it is stamped at **head** -- a later revision that
  adds a column must not run against a table that already has it;
* a database that predates Alembic is stamped at this **baseline**, so every
  revision after it applies in order.

The distinction is the whole reason this file exists.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: ``ensure_schema`` has already produced this state."""


def downgrade() -> None:
    """No-op: there is nothing below the baseline to return to."""
