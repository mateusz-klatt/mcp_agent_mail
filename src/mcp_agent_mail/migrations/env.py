"""Alembic environment for MCP Agent Mail.

Two entry points, deliberately:

* the ``alembic`` CLI, which builds its own engine from this project's
  ``Settings`` rather than from a URL in ``alembic.ini`` -- there must be one
  source of truth for where the database lives, and it is the application's
  configuration, not a second copy that can drift;
* the application itself, which hands an already-open connection through
  ``config.attributes["connection"]`` so a migration runs inside the same
  transaction and against the same engine as ``ensure_schema``.

SQLite gets ``render_as_batch=True``: it cannot ``ALTER TABLE`` a constraint in
place, and batch mode is Alembic's copy-and-move implementation of exactly the
table rebuild ``db.py`` performs by hand today.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from alembic import context
from sqlmodel import SQLModel

from mcp_agent_mail import models as _models  # noqa: F401  (registers metadata)
from mcp_agent_mail.config import get_settings

# Private on purpose: there is exactly one engine builder in this project and
# a migration must use it, pool tuning, SQLite PRAGMAs and all. A second
# builder here would be a second set of connection semantics.
from mcp_agent_mail.db import _build_engine

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config
target_metadata = SQLModel.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a database.

    Kept working because it is the only way to review, in a change request,
    what a migration will do to production before it does it.
    """
    settings = get_settings()
    context.configure(
        url=settings.database.url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=settings.database.url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = _build_engine(get_settings().database)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    """Run migrations against a live database.

    When the application supplies a connection there is no engine to build and
    no loop to start -- running one here would deadlock inside the caller's
    already-running loop.
    """
    supplied = config.attributes.get("connection")
    if supplied is not None:
        _run(supplied)
        return
    asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
