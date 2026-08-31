"""Database-level guarantees of the ticketing tables.

These assert the promises SQLite itself keeps, which is a smaller set than the
ticketing rules as a whole: the table constrains SHAPE (lengths, character
classes, temporal ordering, non-negativity) and never VOCABULARY, because
widening a table-level CHECK on SQLite means ``op.batch_alter_table`` -- a full
copy of the table that also drops dependent triggers and indexes. Membership
(``open`` / ``task`` / ``done``) lives in ``tickets.py`` and is exercised there.

Every rejection below is paired with an accepting control. A CHECK that rejects
everything passes a rejection test just as happily as a correct one.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from mcp_agent_mail.db import ensure_schema, get_engine, reset_database_state

pytestmark = pytest.mark.usefixtures("isolated_env")

_TICKETING_TABLES = ("ticket_sequences", "tickets", "ticket_links", "ticket_events")


def _schema() -> None:
    reset_database_state()
    asyncio.run(ensure_schema())
    asyncio.run(get_engine().dispose())


def _sqlite_path() -> str:
    url = get_engine().url
    assert url.database, "the test engine is not backed by a file"
    return url.database


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_sqlite_path())
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _seed_project(connection: sqlite3.Connection, slug: str = "proj") -> int:
    """Insert the one row tickets cannot exist without, and return its id."""
    values: dict[str, Any] = {
        "slug": slug,
        "human_key": f"/owner/{slug}",
        "created_at": "2026-08-31 00:00:00",
        # 64 lowercase hex characters, enforced by the projects_generation_guard_bi
        # trigger rather than by a CHECK.
        "project_generation": (slug.encode().hex() * 64)[:64],
        "project_uid": f"uid-{slug}",
    }
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO projects ({names}) VALUES ({marks})", tuple(values.values())
    )
    connection.commit()
    return int(cursor.lastrowid or 0)


def _insert_ticket(connection: sqlite3.Connection, project_id: int, **overrides: Any) -> None:
    row: dict[str, Any] = {
        "project_id": project_id,
        "key": "AM-1",
        "kind_key": "task",
        "title": "a title",
        "description_md": "",
        "status_key": "open",
        "priority": 3,
        "reporter_label": "cli",
        # Python-side default_factory, so a raw INSERT must supply it: the column is NOT
        # NULL and carries no server default.
        "discussion_thread_id": "tkt-"
        + (overrides.get("key", "AM-1").encode().hex() * 8)[:32],
        "revision": 1,
        "created_ts": "2026-08-31 00:00:00",
        "updated_ts": "2026-08-31 00:00:00",
    }
    row.update(overrides)
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    connection.execute(f"INSERT INTO tickets ({names}) VALUES ({marks})", tuple(row.values()))
    connection.commit()


def test_ticketing_tables_and_indexes_exist() -> None:
    _schema()
    connection = _connect()
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for name in _TICKETING_TABLES:
            assert name in tables, f"{name} was not created"
    finally:
        connection.close()


def test_hot_worklist_index_stays_partial() -> None:
    """A partial index that silently became total passes every functional test.

    ``idx_tickets_project_open`` exists so closed history never enters the hot
    worklist. Losing the ``WHERE`` clause would keep every query correct and make
    the index grow without bound, so nothing but the DDL itself catches it.
    """
    _schema()
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_tickets_project_open'"
        ).fetchone()
        assert row is not None, "idx_tickets_project_open is missing"
        assert "WHERE closed_ts IS NULL" in row[0]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "key",
    [
        "AM-1",  # what we mint
        "AM-12345",
        "bd-10s",  # a real beads id: lowercase, ends in a letter
        "br-abc.1",  # a hierarchical beads_rust id, dot included
        "mcp_agent_mail-123",  # underscores are inside the topic character class
    ],
)
def test_ck_tickets_key_admits_every_shape_a_future_import_needs(key: str) -> None:
    """The class is deliberately a SUPERSET of what the allocator generates.

    It is exactly the class ``send_message`` accepts for ``topic`` (app.py:10546),
    so every key is a legal topic and thread id verbatim. Admitting ``bd-10s``
    and ``br-abc.1`` today is what keeps a one-way importer a generator change
    later rather than a schema change.
    """
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        _insert_ticket(connection, project_id, key=key)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "key",
    [
        "../etc",  # a path traversal must never become a key
        ".hidden-1",  # the first character must be alphanumeric
        "-leading",
        "AM 1",  # a space would break the topic round-trip
        "AB",  # below the three-character floor
        "A" * 65,  # above the 64-character topic ceiling
    ],
)
def test_ck_tickets_key_refuses_shapes_that_would_break_the_topic_round_trip(key: str) -> None:
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(connection, project_id, key=key)
    finally:
        connection.close()


def test_key_uniqueness_is_case_insensitive() -> None:
    """``fetch_topic`` matches case-insensitively (app.py:13243).

    Without the expression index, ``AM-12`` and ``am-12`` would be two tickets
    whose discussions are permanently indistinguishable.
    """
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        _insert_ticket(connection, project_id, key="AM-12")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(connection, project_id, key="am-12")

        # Control: a genuinely different key in the same project is accepted, so
        # the rejection above is the case fold and not a blanket refusal.
        _insert_ticket(connection, project_id, key="AM-13")
    finally:
        connection.close()


def test_closure_halves_cannot_drift_apart() -> None:
    """``closed_ts`` and ``resolution_key`` are present together or absent together.

    Vocabulary-free by construction, so renaming or adding a terminal status
    never touches this table.
    """
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(
                connection, project_id, key="AM-1", closed_ts="2026-08-31 01:00:00"
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(connection, project_id, key="AM-2", resolution_key="done")

        # Both controls: neither half, and both halves.
        _insert_ticket(connection, project_id, key="AM-3")
        _insert_ticket(
            connection,
            project_id,
            key="AM-4",
            closed_ts="2026-08-31 01:00:00",
            # Closing is an update, so the update time moves with it; see
            # test_close_time_cannot_be_later_than_the_last_update.
            updated_ts="2026-08-31 01:00:00",
            resolution_key="done",
        )
    finally:
        connection.close()


def test_timestamps_cannot_run_backwards() -> None:
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(
                connection, project_id, key="AM-1", updated_ts="2026-08-30 00:00:00"
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(
                connection,
                project_id,
                key="AM-2",
                closed_ts="2026-08-30 00:00:00",
                resolution_key="done",
            )
        # Control: equal timestamps are the ordinary case at creation.
        _insert_ticket(connection, project_id, key="AM-3")
    finally:
        connection.close()


def test_priority_is_open_ended_upward_but_never_negative() -> None:
    """A ``BETWEEN`` ceiling would need a table rewrite the first time anyone
    wants a sixth band, so only the floor is a database promise."""
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(connection, project_id, key="AM-1", priority=-1)
        _insert_ticket(connection, project_id, key="AM-2", priority=0)
        _insert_ticket(connection, project_id, key="AM-3", priority=99)
    finally:
        connection.close()


def test_a_ticket_cannot_be_its_own_parent() -> None:
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        _insert_ticket(connection, project_id, key="AM-1")
        row_id = connection.execute("SELECT id FROM tickets WHERE key='AM-1'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE tickets SET parent_id = id WHERE id = ?", (row_id,))
            connection.commit()
        connection.rollback()
        # Control: pointing at a different ticket is accepted. The cross-row rule
        # that a parent must itself be an epic spans two rows and lives in
        # tickets.py; a SQLite CHECK cannot see another row.
        _insert_ticket(connection, project_id, key="AM-2", parent_id=row_id)
    finally:
        connection.close()


def test_title_must_not_be_blank_after_trimming() -> None:
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(connection, project_id, key="AM-1", title="   ")
        _insert_ticket(connection, project_id, key="AM-2", title=" a ")
    finally:
        connection.close()


def test_sequence_prefix_is_uppercase_and_globally_unique() -> None:
    """Global, not per-project.

    A key pasted into cross-project mail carries no project context, so a
    per-project prefix would let two projects both mint ``AM-1``. Global is also
    the reversible direction: relaxing it later is free, tightening it would mean
    renaming keys already frozen in immutable archive documents.
    """
    _schema()
    connection = _connect()
    try:
        first = _seed_project(connection, slug="alpha")
        second = _seed_project(connection, slug="beta")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ticket_sequences (project_id, prefix, next_seq, created_ts, updated_ts)"
                " VALUES (?, 'am', 1, '2026-08-31 00:00:00', '2026-08-31 00:00:00')",
                (first,),
            )
            connection.commit()
        connection.rollback()

        connection.execute(
            "INSERT INTO ticket_sequences (project_id, prefix, next_seq, created_ts, updated_ts)"
            " VALUES (?, 'AM', 1, '2026-08-31 00:00:00', '2026-08-31 00:00:00')",
            (first,),
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO ticket_sequences (project_id, prefix, next_seq, created_ts, updated_ts)"
                " VALUES (?, 'AM', 1, '2026-08-31 00:00:00', '2026-08-31 00:00:00')",
                (second,),
            )
            connection.commit()
        connection.rollback()

        # Control: a different prefix in the second project is accepted.
        connection.execute(
            "INSERT INTO ticket_sequences (project_id, prefix, next_seq, created_ts, updated_ts)"
            " VALUES (?, 'BETA', 1, '2026-08-31 00:00:00', '2026-08-31 00:00:00')",
            (second,),
        )
        connection.commit()
    finally:
        connection.close()


def test_link_edges_are_unique_and_survive_a_purged_referent() -> None:
    """``target_ref`` is TEXT with no foreign key, deliberately.

    ``purge_old_messages`` deletes ``Message`` rows while foreign keys are
    enforced on every pooled connection, so a real FK here would either cascade
    the whole edge away or block the purge. A dangling ref must degrade to "no
    longer available", never to a deleted edge.
    """
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        _insert_ticket(connection, project_id, key="AM-1")
        ticket_id = connection.execute("SELECT id FROM tickets WHERE key='AM-1'").fetchone()[0]

        def link(relation: str, kind: str, ref: str) -> None:
            connection.execute(
                "INSERT INTO ticket_links"
                " (ticket_id, relation, target_kind, target_ref, created_by_label, created_ts)"
                " VALUES (?, ?, ?, ?, 'cli', '2026-08-31 00:00:00')",
                (ticket_id, relation, kind, ref),
            )
            connection.commit()

        # A message id that does not exist: accepted, because the edge must
        # outlive its referent.
        link("decided_by", "message", "999999")

        with pytest.raises(sqlite3.IntegrityError):
            link("decided_by", "message", "999999")
        connection.rollback()

        # Control: same target, different relation, is a different edge.
        link("relates", "message", "999999")
    finally:
        connection.close()


def _insert_event(connection: sqlite3.Connection, project_id: int, **overrides: Any) -> None:
    row: dict[str, Any] = {
        "ticket_id": 1,
        "ticket_key_snapshot": "AM-1",
        "project_id": project_id,
        "project_slug_snapshot": "proj",
        "event_type": "created",
        "actor_kind": "cli",
        "actor_label": "cli",
        "revision_after": 1,
        "created_ts": "2026-08-31 00:00:00",
    }
    row.update(overrides)
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    connection.execute(f"INSERT INTO ticket_events ({names}) VALUES ({marks})", tuple(row.values()))
    connection.commit()


def test_event_actor_provenance_is_coupled_to_an_id() -> None:
    """A generation snapshot is meaningful only beside the id it disambiguates.

    SQLite reuses numeric primary keys, so an agent id alone cannot distinguish a
    recreated agent from its previous lifetime -- the reason UiAccessAuditEvent
    snapshots a generation too.
    """
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        generation = "a" * 64

        # A generation with no id it could disambiguate.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(connection, project_id, actor_generation_snapshot=generation)
        # An agent id with no generation.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(connection, project_id, actor_kind="agent", actor_agent_id=7)
        # A generation of the wrong shape.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(
                connection,
                project_id,
                actor_kind="agent",
                actor_agent_id=7,
                actor_generation_snapshot="nothex" * 10,
            )

        # A kind that contradicts its own provenance, in both directions.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(connection, project_id, actor_kind="agent")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(
                connection,
                project_id,
                actor_kind="cli",
                actor_agent_id=7,
                actor_generation_snapshot=generation,
            )

        # Both controls: neither half (a CLI writer), and both halves (an agent).
        _insert_event(connection, project_id)
        _insert_event(
            connection,
            project_id,
            actor_kind="agent",
            actor_agent_id=7,
            actor_generation_snapshot=generation,
        )
    finally:
        connection.close()


def test_event_actor_kind_is_shape_checked_not_vocabulary_checked() -> None:
    """Membership lives in ``tickets.py``; the table promises only the shape.

    Widening a table-level CHECK on SQLite means copying the table, so a future
    actor kind must not be a migration.
    """
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(connection, project_id, actor_kind="CLI")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_event(connection, project_id, actor_kind="")
        # Control: a kind the current vocabulary does not yet contain is accepted by
        # the database. That is the point -- the tuple in tickets.py is what closes it.
        _insert_event(connection, project_id, actor_kind="scheduler")
    finally:
        connection.close()


def test_ticket_events_are_immutable_in_the_database() -> None:
    """Append-only enforced by trigger, not only by the absence of an update path.

    The triggers are installed by `_setup_fts`, which runs on every start of every
    database. Putting them in the Alembic revision instead would have created them
    on production and on no developer machine, because a fresh database is stamped
    at head and never replays a revision.
    """
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        _insert_event(connection, project_id)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE ticket_events SET event_type = 'closed'")
            connection.commit()
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM ticket_events")
            connection.commit()
        connection.rollback()

        # Control: appending another row is exactly what must still work.
        _insert_event(connection, project_id, event_type="closed")
        assert connection.execute("SELECT count(*) FROM ticket_events").fetchone()[0] == 2
    finally:
        connection.close()


def _send_ledger_back(connection: sqlite3.Connection) -> None:
    """Rewind the stamp so `upgrade head` replays 0002 instead of no-opping."""
    connection.execute("UPDATE alembic_version SET version_num = '0001baseline'")
    connection.commit()


def _indexes_on(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    return dict(
        connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name = ?",
            (table,),
        ).fetchall()
    )


def test_revision_replays_cleanly_while_an_expression_index_is_still_present() -> None:
    """The branch that actually broke, and the reason the loop cannot use the obvious API.

    Measured on SQLAlchemy 2.x against SQLite: ``Inspector.get_indexes`` SKIPS the
    expression index ``uq_tickets_key_nocase`` (``lower(key)``) with a SAWarning and
    omits it from the result, while ``dialect.has_index`` returns False for that same
    index -- so ``Index.create(checkfirst=True)`` would not have saved it either. A loop
    that trusts either one tries to create an index that is already there and raises
    ``index uq_tickets_key_nocase already exists``, taking down every entry point that
    calls ``ensure_schema``.

    So the expression index is deliberately left IN PLACE here while an ordinary one is
    removed: dropping both would let a naive loop succeed, because creating a genuinely
    absent index is correct. This test is worthless unless it fails against the naive
    implementation, and it does.
    """
    _schema()
    ordinary = "idx_tickets_project_open"
    expression = "uq_tickets_key_nocase"

    connection = _connect()
    try:
        connection.execute(f"DROP INDEX {ordinary}")
        _send_ledger_back(connection)
        before = _indexes_on(connection, "tickets")
        assert ordinary not in before
        assert expression in before, "the expression index must remain, or this proves nothing"
    finally:
        connection.close()

    _schema()  # must not raise

    connection = _connect()
    try:
        after = _indexes_on(connection, "tickets")
        assert ordinary in after, "the ordinary index was not restored"
        assert "WHERE closed_ts IS NULL" in after[ordinary], "restored without its predicate"
        assert expression in after
    finally:
        connection.close()


def test_revision_restores_indexes_dropped_from_an_existing_table() -> None:
    """``create_all(checkfirst=True)`` skips an EXISTING table wholesale.

    It will not add a missing index to one, so on any database that already has
    `tickets` the revision's index loop is the sole repair path.
    """
    _schema()
    ordinary = "idx_tickets_project_open"
    expression = "uq_tickets_key_nocase"

    connection = _connect()
    try:
        connection.execute(f"DROP INDEX {ordinary}")
        connection.execute(f"DROP INDEX {expression}")
        _send_ledger_back(connection)
    finally:
        connection.close()

    _schema()

    connection = _connect()
    try:
        rows = _indexes_on(connection, "tickets")
        assert ordinary in rows, "the ordinary index was not restored"
        assert expression in rows, "the expression index was not restored"
        assert "WHERE closed_ts IS NULL" in rows[ordinary]
        assert "lower(key)" in rows[expression]
    finally:
        connection.close()


def test_keys_collide_across_projects_because_uniqueness_is_global() -> None:
    """The headline consequence of the global-key decision, asserted across two projects.

    A key pasted into cross-project mail carries no project context, so two projects
    minting `AM-12` would make "blocked on AM-12" ambiguous. Global is also the
    reversible direction: relaxing to per-project later is free, tightening would mean
    renaming keys already frozen in immutable archive documents.
    """
    _schema()
    connection = _connect()
    try:
        alpha = _seed_project(connection, slug="alpha")
        beta = _seed_project(connection, slug="beta")

        _insert_ticket(connection, alpha, key="AM-12")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(connection, beta, key="AM-12")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(connection, beta, key="am-12")

        # Control: a different key in the second project is accepted, so the rejections
        # above are the global constraint and not a per-project write failure.
        _insert_ticket(connection, beta, key="BETA-1")
    finally:
        connection.close()


def test_close_time_cannot_be_later_than_the_last_update() -> None:
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(
                connection,
                project_id,
                key="AM-1",
                closed_ts="2026-08-31 02:00:00",
                resolution_key="done",
                updated_ts="2026-08-31 01:00:00",
            )
        # Control: closing at the moment of the update is the ordinary case.
        _insert_ticket(
            connection,
            project_id,
            key="AM-2",
            closed_ts="2026-08-31 01:00:00",
            resolution_key="done",
            updated_ts="2026-08-31 01:00:00",
        )
    finally:
        connection.close()


def test_title_length_is_bounded_independently_of_trim() -> None:
    """The half of the CHECK the service layer normally makes unreachable.

    SQLite enforces no VARCHAR width, so `max_length=512` creates no constraint at all,
    and `trim()` strips spaces only. A writer that bypasses `tickets.normalize_title`
    could otherwise store 512 characters plus trailing spaces in a 512-wide column.
    """
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(connection, project_id, key="AM-1", title=("x" * 512) + "   ")
        # Control: exactly at the limit is accepted, so the rejection is the overflow.
        _insert_ticket(connection, project_id, key="AM-2", title="x" * 512)
    finally:
        connection.close()


def test_a_discussion_thread_is_unique_and_shape_checked() -> None:
    """The reservation the mail system does not provide for `thread_id` itself."""
    _schema()
    connection = _connect()
    try:
        project_id = _seed_project(connection)
        _insert_ticket(connection, project_id, key="AM-1")
        thread = connection.execute(
            "SELECT discussion_thread_id FROM tickets WHERE key='AM-1'"
        ).fetchone()[0]

        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(connection, project_id, key="AM-2", discussion_thread_id=thread)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(connection, project_id, key="AM-3", discussion_thread_id="has spaces!!")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ticket(connection, project_id, key="AM-4", discussion_thread_id="short")

        # Control: a distinct, well-shaped id is accepted.
        _insert_ticket(connection, project_id, key="AM-5", discussion_thread_id="tkt-" + "b" * 32)
    finally:
        connection.close()
