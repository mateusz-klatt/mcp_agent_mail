"""Rules the database cannot express, and the one it can only express under a lock.

The schema promises shape; `tickets.py` promises vocabulary, cross-row rules and
race-free key allocation. This file exercises the second set.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mcp_agent_mail import tickets
from mcp_agent_mail.db import (
    ensure_schema,
    get_immediate_session,
    get_session,
    reset_database_state,
)
from mcp_agent_mail.models import Agent, Message, Project, Ticket, TicketEvent, TicketSequence
from mcp_agent_mail.tickets import TicketActor, TicketError, TicketUpdate

pytestmark = pytest.mark.usefixtures("isolated_env")

ACTOR = TicketActor.cli()


async def _project(slug: str = "mcp-agent-mail") -> Project:
    async with get_immediate_session() as session:
        project = Project(slug=slug, human_key=f"/owner/{slug}")
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return project


async def _create(project: Project, **kwargs: Any) -> Ticket:
    async with get_immediate_session() as session:
        fresh = await session.get(Project, project.id)
        assert fresh is not None
        ticket = await tickets.create_ticket(session, project=fresh, actor=ACTOR, **kwargs)
        await session.commit()
        return await _load(ticket.key)


async def _load(key: str) -> Ticket:
    async with get_session() as session:
        return await tickets.load_ticket(session, ticket_key=key)


def _run(coroutine: Any) -> Any:
    reset_database_state()

    async def _main() -> Any:
        await ensure_schema()
        try:
            return await coroutine()
        finally:
            await (await _engine()).dispose()

    return asyncio.run(_main())


async def _engine() -> Any:
    from mcp_agent_mail.db import get_engine

    return get_engine()


def test_the_first_ticket_of_a_project_bootstraps_its_sequence() -> None:
    """An allocator written as a single UPDATE...RETURNING fails on exactly this case.

    With no sequence row the UPDATE matches nothing and RETURNING yields nothing at all,
    so the bootstrap INSERT is what makes the first ticket of every project possible.
    """

    async def scenario() -> None:
        project = await _project()
        ticket = await _create(project, title="first")
        assert ticket.key == "MCPA-1"

        async with get_session() as session:
            sequence = await session.get(TicketSequence, project.id)
            assert sequence is not None
            assert sequence.prefix == "MCPA"
            assert sequence.next_seq == 2, "the counter must advance past the number it issued"

    _run(scenario)


def test_keys_are_distinct_under_overlapping_creators() -> None:
    """Serialisation comes from BEGIN IMMEDIATE, not from how the number is computed."""

    async def scenario() -> None:
        project = await _project()
        creators = 12

        async def one(index: int) -> str:
            async with get_immediate_session() as session:
                fresh = await session.get(Project, project.id)
                assert fresh is not None
                ticket = await tickets.create_ticket(
                    session, project=fresh, actor=ACTOR, title=f"concurrent {index}"
                )
                await session.commit()
                return ticket.key

        keys = await asyncio.gather(*(one(index) for index in range(creators)))
        assert len(set(keys)) == creators, f"duplicate keys minted: {sorted(keys)}"
        assert sorted(int(key.split("-")[1]) for key in keys) == list(range(1, creators + 1)), (
            "the sequence must be gap-free as well as collision-free"
        )

        async with get_session() as session:
            sequence = await session.get(TicketSequence, project.id)
            assert sequence is not None
            assert sequence.next_seq == creators + 1

    _run(scenario)


def test_a_rolled_back_creation_returns_its_number_rather_than_burning_it() -> None:
    """Allocation shares the ticket's transaction, which is why the number survives."""

    async def scenario() -> None:
        project = await _project()

        try:
            async with get_immediate_session() as session:
                fresh = await session.get(Project, project.id)
                assert fresh is not None
                await tickets.create_ticket(
                    session, project=fresh, actor=ACTOR, title="doomed"
                )
                raise RuntimeError("caller aborts after allocating")
        except RuntimeError:
            pass

        ticket = await _create(project, title="survivor")
        assert ticket.key == "MCPA-1", "the rolled-back number was burned"

    _run(scenario)


def test_a_key_already_taken_as_a_mail_topic_is_skipped() -> None:
    """The topic namespace predates ticketing and cannot be rewritten afterwards.

    A key colliding with an existing topic would silently adopt unrelated historical
    conversation into the ticket's discussion the first time anyone reads it.
    """

    async def scenario() -> None:
        project = await _project()
        async with get_immediate_session() as session:
            # A message needs a live sender: a trigger enforces it.
            sender = Agent(
                project_id=project.id,
                name="claude-linux-test-1",
                program="claude-code",
                model="test",
                task_description="seeding an older topic",
            )
            session.add(sender)
            await session.flush()
            session.add(
                Message(
                    project_id=project.id,
                    sender_id=sender.id,
                    subject="an older conversation",
                    body_md="",
                    topic="MCPA-1",
                )
            )
            await session.commit()

        ticket = await _create(project, title="skips the occupied string")
        assert ticket.key == "MCPA-2"

    _run(scenario)


def test_a_key_is_taken_when_no_topic_occupies_it() -> None:
    """Control for the test above: without the planted topic, MCPA-1 is minted."""

    async def scenario() -> None:
        project = await _project()
        ticket = await _create(project, title="nothing in the way")
        assert ticket.key == "MCPA-1"

    _run(scenario)


def test_a_taken_prefix_falls_back_to_a_suffixed_variant() -> None:
    """Prefixes are globally unique, so two projects cannot both mint AM-1."""

    async def scenario() -> None:
        first = await _project(slug="alpha-one")
        second = await _project(slug="alpha-two")

        assert (await _create(first, title="a")).key == "ALPH-1"
        assert (await _create(second, title="b")).key == "ALPH2-1"

    _run(scenario)


def test_a_parent_must_be_an_epic_in_the_same_project() -> None:
    """A rule spanning two rows, which a SQLite CHECK cannot see."""

    async def scenario() -> None:
        project = await _project()
        task = await _create(project, title="an ordinary task")
        epic = await _create(project, title="an epic", kind_key="epic")

        with pytest.raises(TicketError) as refusal:
            await _create(project, title="child of a task", parent_id=task.id)
        assert refusal.value.code == "parent_not_an_epic"

        # Control: the same call under a real epic is accepted.
        child = await _create(project, title="child of an epic", parent_id=epic.id)
        assert child.parent_id == epic.id

    _run(scenario)


def test_blocks_edges_cannot_close_a_cycle() -> None:
    async def scenario() -> None:
        project = await _project()
        first = await _create(project, title="one")
        second = await _create(project, title="two")
        third = await _create(project, title="three")

        async def link(source: Ticket, target: Ticket) -> None:
            async with get_immediate_session() as session:
                fresh_project = await session.get(Project, project.id)
                fresh = await session.get(Ticket, source.id)
                assert fresh is not None and fresh_project is not None
                await tickets.set_ticket_link(
                    session,
                    ticket=fresh,
                    project=fresh_project,
                    actor=ACTOR,
                    relation="blocks",
                    target_kind="ticket",
                    target_ref=target.key,
                )
                await session.commit()

        await link(first, second)
        await link(second, third)

        with pytest.raises(TicketError) as refusal:
            await link(third, first)
        assert refusal.value.code == "link_cycle"

        # Control: a non-cyclic edge across the same chain is accepted.
        await link(first, third)

    _run(scenario)


def test_relinking_the_same_edge_is_idempotent() -> None:
    async def scenario() -> None:
        project = await _project()
        ticket = await _create(project, title="one")

        async def link() -> bool:
            async with get_immediate_session() as session:
                fresh_project = await session.get(Project, project.id)
                fresh = await session.get(Ticket, ticket.id)
                assert fresh is not None and fresh_project is not None
                result = await tickets.set_ticket_link(
                    session,
                    ticket=fresh,
                    project=fresh_project,
                    actor=ACTOR,
                    relation="decided_by",
                    target_kind="message",
                    target_ref="4119",
                )
                await session.commit()
                return result.created

        assert await link() is True
        assert await link() is False, "a repeated edge must not be created twice"

    _run(scenario)


def test_closing_requires_a_resolution_and_reopening_clears_it() -> None:
    async def scenario() -> None:
        project = await _project()
        ticket = await _create(project, title="round trip")

        async def apply(update: TicketUpdate, expected: int | None = None) -> Ticket:
            async with get_immediate_session() as session:
                fresh_project = await session.get(Project, project.id)
                fresh = await session.get(Ticket, ticket.id)
                assert fresh is not None and fresh_project is not None
                await tickets.apply_ticket_update(
                    session,
                    ticket=fresh,
                    project=fresh_project,
                    actor=ACTOR,
                    update=update,
                    expected_revision=expected,
                )
                await session.commit()
                return await _load(ticket.key)

        with pytest.raises(TicketError) as refusal:
            await apply(TicketUpdate(status_key="closed"))
        assert refusal.value.code == "resolution_required"

        closed = await apply(TicketUpdate(status_key="closed", resolution_key="done"))
        assert closed.closed_ts is not None and closed.resolution_key == "done"
        assert closed.updated_ts >= closed.closed_ts, "the database refuses the alternative"

        reopened = await apply(TicketUpdate(status_key="open"))
        assert reopened.closed_ts is None and reopened.resolution_key is None

    _run(scenario)


def test_a_stale_revision_is_refused_rather_than_overwriting() -> None:
    async def scenario() -> None:
        project = await _project()
        ticket = await _create(project, title="contended")

        async def retitle(title: str, expected: int) -> None:
            async with get_immediate_session() as session:
                fresh_project = await session.get(Project, project.id)
                fresh = await session.get(Ticket, ticket.id)
                assert fresh is not None and fresh_project is not None
                await tickets.apply_ticket_update(
                    session,
                    ticket=fresh,
                    project=fresh_project,
                    actor=ACTOR,
                    update=TicketUpdate(title=title),
                    expected_revision=expected,
                )
                await session.commit()

        await retitle("first writer wins", expected=1)

        with pytest.raises(TicketError) as refusal:
            await retitle("second writer had a stale read", expected=1)
        assert refusal.value.code == "revision_conflict"

        assert (await _load(ticket.key)).title == "first writer wins"

    _run(scenario)


def test_an_oversized_change_is_marked_not_silently_truncated() -> None:
    """A description holds 65536 characters and an event value holds 1024.

    A change log that quietly cuts what changed is worse than one that says so.
    """

    async def scenario() -> None:
        project = await _project()
        ticket = await _create(project, title="long body")

        async with get_immediate_session() as session:
            fresh_project = await session.get(Project, project.id)
            fresh = await session.get(Ticket, ticket.id)
            assert fresh is not None and fresh_project is not None
            await tickets.apply_ticket_update(
                session,
                ticket=fresh,
                project=fresh_project,
                actor=ACTOR,
                update=TicketUpdate(description_md="x" * 5000),
            )
            await session.commit()

        async with get_session() as session:
            from sqlmodel import select

            rows = (
                await session.execute(
                    select(TicketEvent).where(TicketEvent.field_name == "description_md")
                )
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].new_value is not None
        assert len(rows[0].new_value) <= 1024
        assert "not kept" in rows[0].new_value, "truncation must be visible, not silent"

    _run(scenario)


def test_a_blank_title_of_tabs_is_refused_even_though_sqlite_trim_would_pass_it() -> None:
    """SQLite's trim() strips spaces only, so the database CHECK alone is not enough."""

    async def scenario() -> None:
        project = await _project()
        with pytest.raises(TicketError) as refusal:
            await _create(project, title="\t\n  ")
        assert refusal.value.code == "invalid_title"

        # Control: ordinary surrounding whitespace is normalized, not refused.
        ticket = await _create(project, title="  a   real   title  ")
        assert ticket.title == "a real title"

    _run(scenario)


def test_vocabulary_is_refused_by_the_service_not_by_the_table() -> None:
    async def scenario() -> None:
        project = await _project()
        with pytest.raises(TicketError) as refusal:
            await _create(project, title="x", kind_key="saga")
        assert refusal.value.code == "invalid_kind"

    _run(scenario)


@pytest.mark.parametrize(
    ("priority", "importance"),
    [(0, "high"), (1, "high"), (2, "normal"), (3, "normal"), (4, "low"), (9, "low")],
)
def test_priority_maps_onto_the_mail_importance_vocabulary(priority: int, importance: str) -> None:
    assert tickets.priority_importance(priority) == importance


def test_a_ticket_gets_an_opaque_immutable_discussion_thread() -> None:
    """The key is a tag; the thread id is the conversation's identity.

    `Message.thread_id` carries no unique constraint and is supplied by the caller, so any
    agent can send `thread_id="AM-12"` before that ticket exists and nothing stops them.
    Binding a ticket's discussion to a namespace anyone may occupy is not a collision risk,
    it is the absence of a reservation.
    """

    async def scenario() -> None:
        project = await _project()
        first = await _create(project, title="one")
        second = await _create(project, title="two")

        assert first.discussion_thread_id != first.key
        assert first.discussion_thread_id != second.discussion_thread_id
        assert first.discussion_thread_id.startswith("tkt-")
        # Must be a legal Message.thread_id verbatim, with no escaping anywhere.
        from mcp_agent_mail.utils import validate_thread_id_format

        assert validate_thread_id_format(first.discussion_thread_id)

        async with get_session() as session:
            resolved = await tickets.resolve_discussion_thread(session, ticket_key=first.key)
        assert resolved == first.discussion_thread_id

    _run(scenario)


def test_the_service_normalizes_rather_than_relying_on_the_length_check() -> None:
    """Trailing whitespace is stripped, so the stored value cannot overflow the width.

    The database CHECK carries both `length(trim(title)) >= 1` and `length(title) <= 512`
    because SQLite enforces no VARCHAR width and `trim()` strips spaces only. This layer
    is what makes the second half unreachable in practice: it normalizes first, so a
    512-character title with trailing spaces is stored as 512 characters rather than
    refused. Over the limit *after* normalizing is still a refusal.
    """

    async def scenario() -> None:
        project = await _project()

        ticket = await _create(project, title=("x" * 512) + "   ")
        assert len(ticket.title) == 512, "trailing whitespace must not reach the column"

        with pytest.raises(TicketError) as refusal:
            await _create(project, title="y" * 513)
        assert refusal.value.code == "invalid_title"

    _run(scenario)
