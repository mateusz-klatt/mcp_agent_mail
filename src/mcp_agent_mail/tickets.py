"""Atomic domain operations for epics and tickets.

This module owns every rule the database cannot express, and it is the single place
that owns them: the tables constrain SHAPE (lengths, character classes, temporal
ordering, non-negativity), because widening a table-level CHECK on SQLite means
copying the table. VOCABULARY and every cross-row rule live here.

It follows ``ui_access.py``: no FastMCP, FastAPI or Typer import, ``Literal``-typed
refusal codes on a ``RuntimeError`` subclass, frozen slotted result objects, and
``async def`` functions that take an already-open ``AsyncSession`` so the caller
chooses the transaction. Mutating callers must open that session with
``db.get_immediate_session()``: it issues ``BEGIN IMMEDIATE`` before the first read,
which is what makes read-then-write sequences race-free on this database.

There is deliberately no cache of any kind here. Three uncleared ``lru_cache``
decorators already exist in this codebase and ``tests/conftest.py`` resets only four
named caches by hand; an order-dependent cache is this repository's documented
recurring defect class.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Optional, cast

from sqlalchemy import ColumnElement, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .models import (
    Agent,
    FileReservation,
    Message,
    Project,
    Ticket,
    TicketEvent,
    TicketLink,
    TicketSequence,
    _utcnow_naive,
)

# --------------------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------------------
#
# These tuples ARE the membership rule. The tables check only that a value has the right
# shape, so adding a status, a kind or a relation is an edit here and never a migration.

TICKET_KINDS: Final[tuple[str, ...]] = ("epic", "task", "bug", "chore")
TICKET_STATUSES: Final[tuple[str, ...]] = ("open", "in_progress", "closed")
TICKET_RESOLUTIONS: Final[tuple[str, ...]] = ("done", "wontfix", "duplicate", "obsolete")
TICKET_RELATIONS: Final[tuple[str, ...]] = (
    "blocks",
    "relates",
    "duplicates",
    "decided_by",
    "touches",
)
TICKET_TARGET_KINDS: Final[tuple[str, ...]] = ("ticket", "message", "file_reservation")
TICKET_EVENT_TYPES: Final[tuple[str, ...]] = (
    "created",
    "field_changed",
    "commented",
    "linked",
    "unlinked",
    "closed",
    "reopened",
)
TICKET_ACTOR_KINDS: Final[tuple[str, ...]] = ("agent", "human", "cli", "system")

# `blocked` and `review` are absent on purpose. `blocked` is derivable from an unresolved
# `blocks` edge and storing it too would create a second source of truth that can disagree
# with the first; `review` is a stage this fleet performs by mail. Either can be added by
# extending the tuple above.

#: The terminal status. Physical closure is ``closed_ts IS NOT NULL``; this is its label.
CLOSED_STATUS: Final = "closed"

#: The only relation whose graph must stay acyclic.
_ACYCLIC_RELATION: Final = "blocks"

_MAX_TITLE_LENGTH: Final = 512
_MAX_DESCRIPTION_LENGTH: Final = 65536
_MAX_EXTERNAL_REF_LENGTH: Final = 256
_MAX_TARGET_REF_LENGTH: Final = 128
_MAX_EVENT_VALUE_LENGTH: Final = 1024
_MAX_PREFIX_LENGTH: Final = 12
_PREFIX_TARGET_LENGTH: Final = 4
_MIN_PREFIX_LENGTH: Final = 2

#: Bounded so a pathological namespace cannot spin. 64 is far above any real collision run.
_KEY_ATTEMPT_LIMIT: Final = 64
#: Suffixes tried when a derived prefix is already taken by another project.
_PREFIX_SUFFIXES: Final = tuple(str(digit) for digit in range(2, 10))

#: Exactly the class ``send_message`` accepts for ``topic``, so a key is a legal topic and
#: a legal thread id verbatim, with no escaping anywhere.
_KEY_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

#: Whitespace that ``trim()`` does not remove. SQLite's ``trim`` strips spaces only, so a
#: title of tabs would satisfy the database CHECK while being blank to a reader.
_BLANK = re.compile(r"^\s*$")

TicketErrorCode = Literal[
    "project_not_found",
    "ticket_not_found",
    "parent_not_found",
    "parent_not_an_epic",
    "parent_cross_project",
    "invalid_kind",
    "invalid_status",
    "invalid_resolution",
    "invalid_relation",
    "invalid_target_kind",
    "invalid_actor_kind",
    "invalid_title",
    "invalid_description",
    "invalid_priority",
    "invalid_external_ref",
    "invalid_key",
    "invalid_prefix",
    "invalid_limit",
    "link_target_not_found",
    "link_cycle",
    "link_self",
    "revision_conflict",
    "resolution_required",
    "resolution_not_allowed",
    "key_namespace_exhausted",
    "prefix_unavailable",
]


class TicketError(RuntimeError):
    """A fail-closed ticket operation refusal carrying one stable machine-readable code."""

    def __init__(self, code: TicketErrorCode, detail: str = "") -> None:
        """Create a refusal with a stable code and an optional human detail."""
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class TicketActor:
    """Who is performing an operation, in a form the audit row can store verbatim.

    ``agent`` is the only kind that carries an id, and the database binds the two: an
    ``agent`` event without an agent id, or a non-agent event with one, is refused.
    """

    kind: str
    agent_id: Optional[int] = None
    generation: Optional[str] = None
    label: str = ""

    @staticmethod
    def from_agent(agent: Agent) -> TicketActor:
        """Build an actor from a registered agent, snapshotting its generation."""
        generation = agent.agent_generation
        # An Agent row that predates the generation column reads as a plain `agent` with
        # no snapshot, which the provenance CHECK would refuse. Such an actor is recorded
        # as `system` with its name rather than being rejected: refusing to write history
        # is a worse outcome than recording it with a weaker provenance.
        if not generation:
            return TicketActor(kind="system", label=agent.name[:128])
        return TicketActor(
            kind="agent",
            agent_id=agent.id,
            generation=generation,
            label=(agent.display_name or agent.name)[:128],
        )

    @staticmethod
    def cli() -> TicketActor:
        """The actor for a command-line writer, which is not an Agent."""
        return TicketActor(kind="cli", label="cli")


@dataclass(frozen=True, slots=True)
class TicketUpdate:
    """One requested change set. ``None`` means "leave alone" for every field."""

    title: Optional[str] = None
    description_md: Optional[str] = None
    status_key: Optional[str] = None
    resolution_key: Optional[str] = None
    priority: Optional[int] = None
    kind_key: Optional[str] = None
    assignee_agent_id: Optional[int] = None
    clear_assignee: bool = False
    parent_id: Optional[int] = None
    clear_parent: bool = False


@dataclass(frozen=True, slots=True)
class TicketMutationResult:
    """Outcome of one applied mutation."""

    ticket: Ticket
    changed_fields: tuple[str, ...]
    revision: int


@dataclass(frozen=True, slots=True)
class TicketLinkResult:
    """Outcome of one requested edge."""

    link: TicketLink
    created: bool


def _require(value: str, allowed: tuple[str, ...], code: TicketErrorCode) -> str:
    """Return ``value`` normalized to lower case if it is in ``allowed``, else refuse."""
    candidate = (value or "").strip().lower()
    if candidate not in allowed:
        raise TicketError(code, f"{value!r} is not one of {', '.join(allowed)}")
    return candidate


def normalize_kind(value: str) -> str:
    """Return a valid ticket kind or refuse."""
    return _require(value, TICKET_KINDS, "invalid_kind")


def normalize_status(value: str) -> str:
    """Return a valid ticket status or refuse."""
    return _require(value, TICKET_STATUSES, "invalid_status")


def normalize_resolution(value: str) -> str:
    """Return a valid resolution or refuse."""
    return _require(value, TICKET_RESOLUTIONS, "invalid_resolution")


def normalize_relation(value: str) -> str:
    """Return a valid link relation or refuse."""
    return _require(value, TICKET_RELATIONS, "invalid_relation")


def normalize_target_kind(value: str) -> str:
    """Return a valid link target kind or refuse."""
    return _require(value, TICKET_TARGET_KINDS, "invalid_target_kind")


def normalize_title(value: str) -> str:
    """Return a title that is non-blank to a reader, not merely to ``trim()``.

    SQLite's ``trim`` removes spaces only, so a title made of tabs or newlines satisfies
    ``length(trim(title)) >= 1`` while being blank on screen. Normalizing here is what
    closes that, and it also collapses the whitespace a pasted title usually carries.
    """
    candidate = " ".join((value or "").split())
    if not candidate or _BLANK.match(candidate):
        raise TicketError("invalid_title", "a title cannot be blank")
    if len(candidate) > _MAX_TITLE_LENGTH:
        raise TicketError("invalid_title", f"a title exceeds {_MAX_TITLE_LENGTH} characters")
    return candidate


def normalize_description(value: Optional[str]) -> str:
    """Return a bounded description body."""
    candidate = value or ""
    if len(candidate) > _MAX_DESCRIPTION_LENGTH:
        raise TicketError(
            "invalid_description", f"a description exceeds {_MAX_DESCRIPTION_LENGTH} characters"
        )
    return candidate


def normalize_priority(value: int) -> int:
    """Return a non-negative priority. 0 is most urgent; there is deliberately no ceiling."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TicketError("invalid_priority", "priority must be an integer >= 0")
    return value


def normalize_external_ref(value: Optional[str]) -> Optional[str]:
    """Return a bounded upstream reference, treating blank input as absent."""
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) > _MAX_EXTERNAL_REF_LENGTH:
        raise TicketError(
            "invalid_external_ref", f"exceeds {_MAX_EXTERNAL_REF_LENGTH} characters"
        )
    return candidate


def normalize_target_ref(value: str) -> str:
    """Return a bounded link target reference."""
    candidate = (value or "").strip()
    if not candidate:
        raise TicketError("link_target_not_found", "an empty target reference")
    if len(candidate) > _MAX_TARGET_REF_LENGTH:
        raise TicketError(
            "link_target_not_found", f"exceeds {_MAX_TARGET_REF_LENGTH} characters"
        )
    return candidate


def priority_importance(priority: int) -> str:
    """Map a ticket priority onto the mail importance vocabulary."""
    if priority <= 1:
        return "high"
    if priority <= 3:
        return "normal"
    return "low"


def derive_prefix(slug: str) -> str:
    """Derive a key prefix from a project slug.

    ``Project.slug`` is already canonical lowercase ``[a-z0-9-]``. The prefix is stored
    rather than derived on every read, so a later project rename never invalidates a key
    somebody has memorised.
    """
    letters = "".join(character for character in slug.upper() if character.isalnum())
    candidate = letters[:_PREFIX_TARGET_LENGTH]
    if len(candidate) < _MIN_PREFIX_LENGTH:
        raise TicketError(
            "invalid_prefix", f"{slug!r} yields no usable prefix of at least two characters"
        )
    return candidate


def validate_prefix(value: str) -> str:
    """Return an explicitly supplied prefix in canonical form, or refuse."""
    candidate = (value or "").strip().upper()
    if not (_MIN_PREFIX_LENGTH <= len(candidate) <= _MAX_PREFIX_LENGTH):
        raise TicketError(
            "invalid_prefix",
            f"a prefix must be {_MIN_PREFIX_LENGTH}-{_MAX_PREFIX_LENGTH} characters",
        )
    if not candidate[0].isalpha() or not candidate.isalnum():
        raise TicketError("invalid_prefix", "a prefix must start with a letter and be alphanumeric")
    return candidate


def validate_key(value: str) -> str:
    """Return a ticket key whose shape survives the mail ``topic`` round trip."""
    candidate = (value or "").strip()
    if not (3 <= len(candidate) <= 64) or not _KEY_SHAPE.fullmatch(candidate):
        raise TicketError("invalid_key", f"{value!r} is not a usable ticket key")
    return candidate


async def _prefix_is_taken(session: AsyncSession, prefix: str) -> bool:
    """Report whether any project already owns this globally unique prefix."""
    found = await session.execute(
        select(TicketSequence.project_id).where(TicketSequence.prefix == prefix).limit(1)
    )
    return found.first() is not None


async def _topic_is_occupied(session: AsyncSession, project_id: int, candidate: str) -> bool:
    """Report whether existing mail already uses this string as a topic.

    The topic namespace is free-form and predates ticketing: production carries well over a
    thousand topics already in the PREFIX-WORD shape a ticket key takes. Minting a key that
    collides with one would silently adopt unrelated historical conversation into the
    ticket's discussion the first time anybody reads it, and neither the old topics nor the
    key can be rewritten afterwards. So the allocator skips an occupied string.
    """
    found = await session.execute(
        select(Message.id)
        .where(
            cast(ColumnElement[bool], Message.project_id == project_id),
            func.lower(Message.topic) == candidate.lower(),
        )
        .limit(1)
    )
    return found.first() is not None


async def allocate_ticket_key(
    session: AsyncSession,
    *,
    project: Project,
    prefix_hint: Optional[str] = None,
) -> str:
    """Mint the next key for ``project`` inside the caller's transaction.

    The caller MUST have opened ``session`` with ``db.get_immediate_session()``. The
    serialisation comes from that ``BEGIN IMMEDIATE`` and not from anything here: it takes
    SQLite's RESERVED lock before the first read, so the read-modify-write below cannot
    interleave with another creator's. ``UPDATE ... RETURNING`` is used because it is one
    statement, not because it adds safety.

    Allocating inside the ticket's own transaction also means a rollback returns the number
    rather than burning it, so keys are gap-free as well as collision-free.

    The bootstrap INSERT is not optional. On a project's first ticket there is no row, the
    UPDATE matches nothing, and ``RETURNING`` yields nothing at all -- which is why an
    allocator written as "a single UPDATE ... RETURNING" fails on exactly the first ticket
    of every project.
    """
    project_id = project.id
    if project_id is None:
        raise TicketError("project_not_found", "the project has no id")

    existing = await session.get(TicketSequence, project_id)
    if existing is None:
        base = validate_prefix(prefix_hint) if prefix_hint else derive_prefix(project.slug)
        prefix = base
        if await _prefix_is_taken(session, prefix):
            prefix = ""
            for suffix in _PREFIX_SUFFIXES:
                candidate_prefix = f"{base[: _MAX_PREFIX_LENGTH - len(suffix)]}{suffix}"
                if not await _prefix_is_taken(session, candidate_prefix):
                    prefix = candidate_prefix
                    break
            if not prefix:
                raise TicketError("prefix_unavailable", f"every variant of {base!r} is taken")
        now = _utcnow_naive()
        session.add(
            TicketSequence(
                project_id=project_id, prefix=prefix, next_seq=1, created_ts=now, updated_ts=now
            )
        )
        await session.flush()

    for _ in range(_KEY_ATTEMPT_LIMIT):
        # RETURNING reports POST-update values, so the number just allocated is next_seq - 1.
        row = (
            await session.execute(
                text(
                    "UPDATE ticket_sequences SET next_seq = next_seq + 1, updated_ts = :now"
                    " WHERE project_id = :pid RETURNING prefix, next_seq - 1"
                ),
                {"now": _utcnow_naive(), "pid": project_id},
            )
        ).first()
        if row is None:  # pragma: no cover - the bootstrap above guarantees a row
            raise TicketError("prefix_unavailable", "the sequence row vanished mid-transaction")
        candidate = f"{row[0]}-{row[1]}"
        if await _topic_is_occupied(session, project_id, candidate):
            continue
        return candidate

    raise TicketError(
        "key_namespace_exhausted",
        f"{_KEY_ATTEMPT_LIMIT} consecutive candidates were already taken as mail topics",
    )


def _event(
    ticket: Ticket,
    project: Project,
    actor: TicketActor,
    event_type: str,
    *,
    field_name: Optional[str] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
) -> TicketEvent:
    """Build one append-only history row, with every value bounded.

    Oversized values are recorded as an explicit marker rather than silently truncated: a
    description may be 65536 characters while an event value holds 1024, and a change log
    that quietly cuts what changed is worse than one that says it did not keep it.
    """

    def bound(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if len(value) <= _MAX_EVENT_VALUE_LENGTH:
            return value
        head = value[: _MAX_EVENT_VALUE_LENGTH - 32].rstrip()
        return f"{head}… [+{len(value) - len(head)} chars not kept]"

    if event_type not in TICKET_EVENT_TYPES:
        raise TicketError("invalid_relation", f"{event_type!r} is not a ticket event type")
    if actor.kind not in TICKET_ACTOR_KINDS:
        raise TicketError("invalid_actor_kind", f"{actor.kind!r} is not an actor kind")

    return TicketEvent(
        ticket_id=cast(int, ticket.id),
        ticket_key_snapshot=ticket.key,
        project_id=cast(int, project.id),
        project_slug_snapshot=project.slug,
        event_type=event_type,
        field_name=field_name,
        old_value=bound(old_value),
        new_value=bound(new_value),
        actor_kind=actor.kind,
        actor_agent_id=actor.agent_id,
        actor_generation_snapshot=actor.generation,
        actor_label=actor.label[:128],
        revision_after=ticket.revision,
        created_ts=_utcnow_naive(),
    )


def record_ticket_event(
    session: AsyncSession,
    *,
    ticket: Ticket,
    project: Project,
    actor: TicketActor,
    event_type: str,
    field_name: Optional[str] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
) -> TicketEvent:
    """Append one history row to the caller's transaction and return it."""
    row = _event(
        ticket,
        project,
        actor,
        event_type,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
    )
    session.add(row)
    return row


async def _load_parent(session: AsyncSession, project_id: int, parent_id: int) -> Ticket:
    """Return the epic a ticket may hang under, enforcing the two cross-row rules."""
    parent = await session.get(Ticket, parent_id)
    if parent is None:
        raise TicketError("parent_not_found", f"no ticket with id {parent_id}")
    if parent.project_id != project_id:
        raise TicketError("parent_cross_project", "a parent must live in the same project")
    if parent.kind_key != "epic":
        raise TicketError("parent_not_an_epic", f"{parent.key} is a {parent.kind_key}")
    return parent


async def create_ticket(
    session: AsyncSession,
    *,
    project: Project,
    actor: TicketActor,
    title: str,
    kind_key: str = "task",
    description_md: str = "",
    priority: int = 3,
    parent_id: Optional[int] = None,
    assignee_agent_id: Optional[int] = None,
    reporter_agent_id: Optional[int] = None,
    external_ref: Optional[str] = None,
    origin_message_id: Optional[int] = None,
    key_prefix: Optional[str] = None,
) -> Ticket:
    """Create one ticket and its ``created`` event inside the caller's transaction.

    The caller must be holding a ``db.get_immediate_session()``; the key allocation is a
    read-then-write and is only race-free under that lock.
    """
    kind = normalize_kind(kind_key)
    clean_title = normalize_title(title)
    body = normalize_description(description_md)
    urgency = normalize_priority(priority)
    upstream = normalize_external_ref(external_ref)

    if parent_id is not None:
        await _load_parent(session, cast(int, project.id), parent_id)

    key = await allocate_ticket_key(session, project=project, prefix_hint=key_prefix)
    now = _utcnow_naive()
    ticket = Ticket(
        project_id=cast(int, project.id),
        key=key,
        kind_key=kind,
        parent_id=parent_id,
        title=clean_title,
        description_md=body,
        status_key="open",
        resolution_key=None,
        priority=urgency,
        assignee_agent_id=assignee_agent_id,
        reporter_agent_id=reporter_agent_id,
        reporter_label=actor.label[:128],
        origin_message_id=origin_message_id,
        external_ref=upstream,
        revision=1,
        created_ts=now,
        updated_ts=now,
    )
    session.add(ticket)
    await session.flush()
    record_ticket_event(
        session,
        ticket=ticket,
        project=project,
        actor=actor,
        event_type="created",
        new_value=clean_title,
    )
    return ticket


async def load_ticket(session: AsyncSession, *, ticket_key: str) -> Ticket:
    """Return one ticket by its globally unique key, matched case-insensitively."""
    key = validate_key(ticket_key)
    found = await session.execute(
        select(Ticket).where(func.lower(Ticket.key) == key.lower())
    )
    ticket = found.scalars().first()
    if ticket is None:
        raise TicketError("ticket_not_found", key)
    return ticket


async def list_tickets(
    session: AsyncSession,
    *,
    project_id: int,
    status_filter: Optional[str] = None,
    kind_filter: Optional[str] = None,
    assignee_agent_id: Optional[int] = None,
    parent_id: Optional[int] = None,
    include_closed: bool = False,
    limit: int = 50,
) -> list[Ticket]:
    """Return one project's worklist in the canonical order.

    Canonical order is ``priority ASC, updated_ts DESC, id DESC``: most urgent first, most
    recently touched first within a band, and the row id as a stable tie-breaker so
    pagination cannot repeat or skip a ticket when two share a timestamp.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 500):
        raise TicketError("invalid_limit", "limit must be between 1 and 500")

    predicates: list[ColumnElement[bool]] = [
        cast(ColumnElement[bool], Ticket.project_id == project_id)
    ]
    if status_filter is not None:
        predicates.append(
            cast(ColumnElement[bool], Ticket.status_key == normalize_status(status_filter))
        )
    if kind_filter is not None:
        predicates.append(cast(ColumnElement[bool], Ticket.kind_key == normalize_kind(kind_filter)))
    if assignee_agent_id is not None:
        predicates.append(
            cast(ColumnElement[bool], Ticket.assignee_agent_id == assignee_agent_id)
        )
    if parent_id is not None:
        predicates.append(cast(ColumnElement[bool], Ticket.parent_id == parent_id))
    if not include_closed:
        predicates.append(cast(ColumnElement[bool], cast(Any, Ticket.closed_ts).is_(None)))

    found = await session.execute(
        select(Ticket)
        .where(*predicates)
        .order_by(
            cast(Any, Ticket.priority).asc(),
            cast(Any, Ticket.updated_ts).desc(),
            cast(Any, Ticket.id).desc(),
        )
        .limit(limit)
    )
    return list(found.scalars().all())


def _closure_change(
    ticket: Ticket, status: Optional[str], resolution: Optional[str]
) -> tuple[Optional[str], Optional[datetime]]:
    """Return the resolution and close time implied by a requested status transition.

    The database promises only that the two move together. Which resolution is required,
    and that reopening clears both, is this layer's rule.
    """
    if status is None:
        if resolution is not None and ticket.closed_ts is None:
            raise TicketError("resolution_not_allowed", "an open ticket carries no resolution")
        return (resolution or ticket.resolution_key, ticket.closed_ts)
    if status == CLOSED_STATUS:
        chosen = resolution or ticket.resolution_key
        if not chosen:
            raise TicketError("resolution_required", "closing requires a resolution")
        return (normalize_resolution(chosen), ticket.closed_ts or _utcnow_naive())
    if resolution is not None:
        raise TicketError("resolution_not_allowed", "a non-terminal status carries no resolution")
    return (None, None)


async def apply_ticket_update(
    session: AsyncSession,
    *,
    ticket: Ticket,
    project: Project,
    actor: TicketActor,
    update: TicketUpdate,
    expected_revision: Optional[int] = None,
) -> TicketMutationResult:
    """Apply one change set, appending one event per changed field.

    ``expected_revision`` is a compare-and-swap token: supplying the revision the caller
    last saw makes a concurrent edit fail loudly instead of silently overwriting.
    """
    if expected_revision is not None and expected_revision != ticket.revision:
        raise TicketError(
            "revision_conflict",
            f"expected revision {expected_revision}, found {ticket.revision}",
        )

    status = normalize_status(update.status_key) if update.status_key is not None else None
    resolution = (
        normalize_resolution(update.resolution_key) if update.resolution_key is not None else None
    )
    new_resolution, new_closed_ts = _closure_change(ticket, status, resolution)

    if update.parent_id is not None:
        await _load_parent(session, ticket.project_id, update.parent_id)
        if update.parent_id == ticket.id:
            raise TicketError("link_self", "a ticket cannot be its own parent")

    pending: list[tuple[str, Optional[str], Optional[str], Any]] = []

    def stage(field: str, current: Any, proposed: Any) -> None:
        if proposed == current:
            return
        pending.append(
            (
                field,
                None if current is None else str(current),
                None if proposed is None else str(proposed),
                proposed,
            )
        )

    if update.title is not None:
        stage("title", ticket.title, normalize_title(update.title))
    if update.description_md is not None:
        stage("description_md", ticket.description_md, normalize_description(update.description_md))
    if update.priority is not None:
        stage("priority", ticket.priority, normalize_priority(update.priority))
    if update.kind_key is not None:
        stage("kind_key", ticket.kind_key, normalize_kind(update.kind_key))
    if status is not None:
        stage("status_key", ticket.status_key, status)
    if update.clear_assignee:
        stage("assignee_agent_id", ticket.assignee_agent_id, None)
    elif update.assignee_agent_id is not None:
        stage("assignee_agent_id", ticket.assignee_agent_id, update.assignee_agent_id)
    if update.clear_parent:
        stage("parent_id", ticket.parent_id, None)
    elif update.parent_id is not None:
        stage("parent_id", ticket.parent_id, update.parent_id)
    stage("resolution_key", ticket.resolution_key, new_resolution)

    if not pending and new_closed_ts == ticket.closed_ts:
        return TicketMutationResult(ticket=ticket, changed_fields=(), revision=ticket.revision)

    was_closed = ticket.closed_ts is not None
    for field, _old, _new, value in pending:
        setattr(ticket, field, value)
    ticket.closed_ts = new_closed_ts
    ticket.revision += 1
    # Closing is an update, so the update time must never trail the close time -- the
    # database refuses the alternative.
    ticket.updated_ts = max(_utcnow_naive(), new_closed_ts or _utcnow_naive())
    session.add(ticket)
    await session.flush()

    for field, old_value, new_value, _value in pending:
        record_ticket_event(
            session,
            ticket=ticket,
            project=project,
            actor=actor,
            event_type="field_changed",
            field_name=field,
            old_value=old_value,
            new_value=new_value,
        )

    is_closed = ticket.closed_ts is not None
    if is_closed and not was_closed:
        record_ticket_event(
            session,
            ticket=ticket,
            project=project,
            actor=actor,
            event_type="closed",
            new_value=ticket.resolution_key,
        )
    elif was_closed and not is_closed:
        record_ticket_event(
            session, ticket=ticket, project=project, actor=actor, event_type="reopened"
        )

    return TicketMutationResult(
        ticket=ticket,
        changed_fields=tuple(field for field, _o, _n, _v in pending),
        revision=ticket.revision,
    )


async def _blocks_would_cycle(session: AsyncSession, source_key: str, target_key: str) -> bool:
    """Report whether adding ``source blocks target`` closes a cycle.

    A reachability walk followed by an insert is a read-then-write, so this must run under
    the caller's ``BEGIN IMMEDIATE`` like every other mutation here.
    """
    if source_key.lower() == target_key.lower():
        return True
    seen: set[str] = {source_key.lower()}
    frontier = [target_key]
    while frontier:
        current = frontier.pop()
        if current.lower() in seen:
            continue
        seen.add(current.lower())
        found = await session.execute(
            select(TicketLink.target_ref)
            .join(Ticket, cast(ColumnElement[bool], Ticket.id == TicketLink.ticket_id))
            .where(
                func.lower(Ticket.key) == current.lower(),
                cast(ColumnElement[bool], TicketLink.relation == _ACYCLIC_RELATION),
                cast(ColumnElement[bool], TicketLink.target_kind == "ticket"),
            )
        )
        for (reference,) in found.all():
            if reference.lower() == source_key.lower():
                return True
            frontier.append(reference)
    return False


async def set_ticket_link(
    session: AsyncSession,
    *,
    ticket: Ticket,
    project: Project,
    actor: TicketActor,
    relation: str,
    target_kind: str,
    target_ref: str,
) -> TicketLinkResult:
    """Create one edge, idempotently.

    Re-linking is idempotent by an explicit ``IntegrityError`` rollback and re-read, not by
    the unique constraint alone: an ``IntegrityError`` poisons the SQLAlchemy transaction
    and is not an ``OperationalError``, so the repository's lock-retry decorator never sees
    it. This is the house idiom, used at seven call sites in ``app.py``.
    """
    edge_relation = normalize_relation(relation)
    edge_kind = normalize_target_kind(target_kind)
    reference = normalize_target_ref(target_ref)

    if edge_kind == "ticket":
        target = await load_ticket(session, ticket_key=reference)
        if target.id == ticket.id:
            raise TicketError("link_self", "a ticket cannot link to itself")
        # The key, never the row id: keys are globally unique and never rewritten, and a
        # key-addressed edge may legitimately cross projects.
        reference = target.key
        if edge_relation == _ACYCLIC_RELATION and await _blocks_would_cycle(
            session, ticket.key, reference
        ):
            raise TicketError("link_cycle", f"{ticket.key} blocks {reference} closes a cycle")

    elif not await link_target_exists(session, target_kind=edge_kind, target_ref=reference):
        # Checked here and not at read time: a typo must fail at the moment it is made,
        # while a referent purged later must degrade to "no longer available".
        raise TicketError(
            "link_target_not_found", f"no {edge_kind} matching {reference!r}"
        )

    existing = await session.execute(
        select(TicketLink).where(
            cast(ColumnElement[bool], TicketLink.ticket_id == ticket.id),
            cast(ColumnElement[bool], TicketLink.relation == edge_relation),
            cast(ColumnElement[bool], TicketLink.target_kind == edge_kind),
            cast(ColumnElement[bool], TicketLink.target_ref == reference),
        )
    )
    found = existing.scalars().first()
    if found is not None:
        return TicketLinkResult(link=found, created=False)

    link = TicketLink(
        ticket_id=cast(int, ticket.id),
        relation=edge_relation,
        target_kind=edge_kind,
        target_ref=reference,
        created_by_agent_id=actor.agent_id,
        created_by_label=actor.label[:128],
        created_ts=_utcnow_naive(),
    )
    session.add(link)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise TicketError(
            "link_target_not_found",
            "the edge was created concurrently; re-read the ticket and retry",
        ) from None

    record_ticket_event(
        session,
        ticket=ticket,
        project=project,
        actor=actor,
        event_type="linked",
        field_name=edge_relation,
        new_value=f"{edge_kind}:{reference}",
    )
    return TicketLinkResult(link=link, created=True)


async def resolve_discussion_thread(session: AsyncSession, *, ticket_key: str) -> str:
    """Return the opaque thread id a ticket's discussion lives under.

    The lookup belongs here rather than at each surface because the mapping is the whole
    point of the decision: the readable key is a TAG (`Message.topic`), and the thread id
    is the conversation's IDENTITY. `Message.thread_id` carries no unique constraint and is
    supplied by the caller, so a key used as a thread id would be a name anyone could
    occupy first. The thread id is immutable once minted -- rewriting it would strand every
    message already committed to the git archive under the old one.
    """
    ticket = await load_ticket(session, ticket_key=ticket_key)
    return ticket.discussion_thread_id


async def link_target_exists(
    session: AsyncSession, *, target_kind: str, target_ref: str
) -> bool:
    """Report whether a link target is resolvable right now.

    Existence is checked at write time and re-checked at read time, but a dangling
    reference is never an error: `target_ref` is TEXT with no foreign key precisely so an
    edge outlives its referent. A purged message leaves the relation, the author and the
    timestamp intact and only the pointer stops resolving.
    """
    if target_kind == "ticket":
        found = await session.execute(
            select(Ticket.id).where(func.lower(Ticket.key) == target_ref.lower()).limit(1)
        )
        return found.first() is not None
    if target_kind == "message":
        if not target_ref.isdigit():
            return False
        found = await session.execute(
            select(Message.id)
            .where(cast(ColumnElement[bool], Message.id == int(target_ref)))
            .limit(1)
        )
        return found.first() is not None
    if target_kind == "file_reservation":
        if not target_ref.isdigit():
            return False
        found = await session.execute(
            select(FileReservation.id)
            .where(cast(ColumnElement[bool], FileReservation.id == int(target_ref)))
            .limit(1)
        )
        return found.first() is not None
    return False


async def outgoing_links(session: AsyncSession, *, ticket_id: int) -> list[TicketLink]:
    """Return the edges this ticket declares."""
    found = await session.execute(
        select(TicketLink)
        .where(cast(ColumnElement[bool], TicketLink.ticket_id == ticket_id))
        .order_by(cast(Any, TicketLink.id).asc())
    )
    return list(found.scalars().all())


async def incoming_links(session: AsyncSession, *, ticket_key: str) -> list[tuple[str, TicketLink]]:
    """Return the edges other tickets declare AT this one, each with its source key.

    This is the half a tracker without a graph cannot answer: "what blocks this". The
    reverse direction is derived at read time rather than stored, so there is only ever one
    row per edge and the two directions cannot disagree.
    """
    found = await session.execute(
        select(Ticket.key, TicketLink)
        .join(Ticket, cast(ColumnElement[bool], Ticket.id == TicketLink.ticket_id))
        .where(
            cast(ColumnElement[bool], TicketLink.target_kind == "ticket"),
            func.lower(TicketLink.target_ref) == ticket_key.lower(),
        )
        .order_by(cast(Any, TicketLink.id).asc())
    )
    return [(row[0], row[1]) for row in found.all()]


def ticket_to_dict(ticket: Ticket) -> dict[str, Any]:
    """Render one ticket as the payload shape every surface returns."""
    return {
        "key": ticket.key,
        "project_id": ticket.project_id,
        "kind": ticket.kind_key,
        "title": ticket.title,
        "description_md": ticket.description_md,
        "status": ticket.status_key,
        "resolution": ticket.resolution_key,
        "priority": ticket.priority,
        "parent_id": ticket.parent_id,
        "assignee_agent_id": ticket.assignee_agent_id,
        "reporter_agent_id": ticket.reporter_agent_id,
        "reporter_label": ticket.reporter_label,
        "origin_message_id": ticket.origin_message_id,
        "discussion_thread_id": ticket.discussion_thread_id,
        "external_ref": ticket.external_ref,
        "revision": ticket.revision,
        "created_ts": ticket.created_ts.isoformat(),
        "updated_ts": ticket.updated_ts.isoformat(),
        "closed_ts": ticket.closed_ts.isoformat() if ticket.closed_ts else None,
        "is_closed": ticket.closed_ts is not None,
    }
