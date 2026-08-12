"""SQLModel data models representing agents, messages, projects, and file reservations."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import CheckConstraint, Column, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel


def _utcnow_naive() -> datetime:
    """Return current UTC time as a naive datetime for SQLite compatibility.

    SQLite stores datetimes without timezone info. Using naive UTC datetimes
    throughout ensures consistent comparisons and avoids 'can't compare
    offset-naive and offset-aware datetimes' errors in SQLAlchemy ORM evaluator.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_session_generation() -> str:
    """Return an unpredictable identifier for one human account lifetime."""
    return secrets.token_hex(32)


def _new_project_generation() -> str:
    """Return an unpredictable identifier for one project-row lifetime."""
    return secrets.token_hex(32)


def _new_agent_generation() -> str:
    """Return an unpredictable identifier for one agent-row lifetime."""
    return secrets.token_hex(32)


def _new_delivery_id() -> str:
    """Return a canonical UUID for one durable message-delivery intent."""
    return str(uuid.uuid4())


class Project(SQLModel, table=True):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "length(slug) BETWEEN 1 AND 255 "
            "AND lower(slug) = slug "
            "AND slug NOT GLOB '*[^a-z0-9-]*' "
            "AND substr(slug, 1, 1) GLOB '[a-z0-9]' "
            "AND substr(slug, -1, 1) GLOB '[a-z0-9]'",
            name="ck_projects_canonical_slug",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True, max_length=255)
    human_key: str = Field(max_length=255, index=True)
    project_generation: str = Field(
        default_factory=_new_project_generation,
        sa_column=Column(
            String(64),
            nullable=False,
            server_default=text("(lower(hex(randomblob(32))))"),
        ),
    )
    created_at: datetime = Field(default_factory=_utcnow_naive)
    archived_at: Optional[datetime] = Field(default=None)

class Product(SQLModel, table=True):
    """Logical grouping across multiple repositories for product-wide inbox/search and threads."""

    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("product_uid", name="uq_product_uid"), UniqueConstraint("name", name="uq_product_name"))

    id: Optional[int] = Field(default=None, primary_key=True)
    product_uid: str = Field(index=True, max_length=64)
    name: str = Field(index=True, max_length=255)
    created_at: datetime = Field(default_factory=_utcnow_naive)

class ProductProjectLink(SQLModel, table=True):
    """Associates a Project with a Product (many-to-many via link table)."""

    __tablename__ = "product_project_links"
    __table_args__ = (
        UniqueConstraint("product_id", "project_id", name="uq_product_project"),
        Index("idx_product_project", "product_id", "project_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="products.id", index=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    created_at: datetime = Field(default_factory=_utcnow_naive)


class Agent(SQLModel, table=True):
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_agent_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    name: str = Field(index=True, max_length=128)
    agent_generation: str = Field(
        default_factory=_new_agent_generation,
        sa_column=Column(
            String(64),
            nullable=False,
            server_default=text("(lower(hex(randomblob(32))))"),
        ),
    )
    program: str = Field(max_length=128)
    model: str = Field(max_length=128)
    task_description: str = Field(default="", max_length=2048)
    inception_ts: datetime = Field(default_factory=_utcnow_naive)
    last_active_ts: datetime = Field(default_factory=_utcnow_naive)
    attachments_policy: str = Field(default="auto", max_length=16)
    contact_policy: str = Field(default="auto", max_length=16)  # open | auto | contacts_only | block_all
    registration_token: Optional[str] = Field(default=None, max_length=64, index=True)
    retired_at: Optional[datetime] = Field(default=None)
    # A human-chosen label, shown alongside `name` and never instead of it.
    #
    # `name` stays the identity: it is what `to:` resolves, what conflict
    # messages print, and what the credential store is keyed by. Letting a
    # display label be addressable would make a mutable field load-bearing —
    # rename once and every memorised address, thread participant and
    # reservation points at nothing — which is the exact failure that keeping
    # the derived name immutable was meant to prevent.
    #
    # Nullable, and null means "no alias": render the name unadorned rather
    # than inventing one.
    display_name: Optional[str] = Field(default=None, max_length=128)
    # A name from a fixed vocabulary, never a frequency and never a URL. A URL
    # would let any agent point the operator's browser at a host of its choosing
    # — a tracking pixel with a volume control — and a raw frequency invites 1 Hz
    # and 20 kHz, which are silence and pain rather than notification. The closed
    # set is what makes this field safe to render without asking anything of the
    # reader.
    notify_sound: Optional[str] = Field(default=None, max_length=32)


class MessageRecipient(SQLModel, table=True):
    __tablename__ = "message_recipients"
    __table_args__ = (
        Index("idx_message_recipients_agent_message", "agent_id", "message_id"),
    )

    message_id: int = Field(foreign_key="messages.id", primary_key=True)
    agent_id: int = Field(foreign_key="agents.id", primary_key=True)
    kind: str = Field(max_length=8, default="to")
    read_ts: Optional[datetime] = Field(default=None)
    ack_ts: Optional[datetime] = Field(default=None)


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_messages_project_created", "project_id", "created_ts"),
        Index("idx_messages_project_sender_created", "project_id", "sender_id", "created_ts"),
        Index("idx_messages_project_topic", "project_id", "topic"),
        Index(
            "uq_messages_delivery",
            "delivery_id",
            unique=True,
            sqlite_where=text("delivery_id IS NOT NULL"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    sender_id: int = Field(foreign_key="agents.id", index=True)
    thread_id: Optional[str] = Field(default=None, index=True, max_length=128)
    # Direct parent→child reply edge (the specific message this one replies to),
    # distinct from `thread_id` which groups a whole conversation. Nullable: a
    # top-level message replies to nothing. (#188)
    reply_to: Optional[int] = Field(default=None, foreign_key="messages.id", index=True)
    topic: Optional[str] = Field(default=None, max_length=64)
    subject: str = Field(max_length=512)
    body_md: str
    importance: str = Field(default="normal", max_length=16)
    ack_required: bool = Field(default=False)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    attachments: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    # Durable delivery lifetime, unlike the reusable SQLite integer row id.
    # Legacy/imported messages have no delivery identity.
    delivery_id: Optional[str] = Field(default=None, max_length=36)


class MessageDelivery(SQLModel, table=True):
    """Durable, immutable intent for publishing one message archive bundle."""

    __tablename__ = "message_deliveries"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'published', 'quarantined')",
            name="ck_message_deliveries_state",
        ),
        CheckConstraint(
            "delivery_kind IN ('message', 'reply', 'contact_request')",
            name="ck_message_deliveries_kind",
        ),
        CheckConstraint(
            "(delivery_kind != 'contact_request' OR actor_kind = 'agent') "
            "AND (delivery_kind != 'reply' "
            "     OR (actor_kind IN ('agent', 'ui_user') "
            "         AND thread_id IS NOT NULL "
            "         AND length(trim(thread_id)) > 0))",
            name="ck_message_deliveries_kind_shape",
        ),
        CheckConstraint(
            "length(id) = 36 "
            "AND substr(id, 9, 1) = '-' "
            "AND substr(id, 14, 1) = '-' "
            "AND substr(id, 19, 1) = '-' "
            "AND substr(id, 24, 1) = '-' "
            "AND lower(id) = id "
            "AND length(replace(id, '-', '')) = 32 "
            "AND replace(id, '-', '') NOT GLOB '*[^0-9a-f]*'",
            name="ck_message_deliveries_uuid",
        ),
        CheckConstraint(
            "length(project_generation_snapshot) = 64 "
            "AND project_generation_snapshot NOT GLOB '*[^0-9a-f]*' "
            "AND length(sender_project_generation_snapshot) = 64 "
            "AND sender_project_generation_snapshot NOT GLOB '*[^0-9a-f]*' "
            "AND length(sender_generation_snapshot) = 64 "
            "AND sender_generation_snapshot NOT GLOB '*[^0-9a-f]*' "
            "AND (actor_project_generation_snapshot IS NULL "
            "     OR (length(actor_project_generation_snapshot) = 64 "
            "         AND actor_project_generation_snapshot NOT GLOB '*[^0-9a-f]*')) "
            "AND (actor_generation_snapshot IS NULL "
            "     OR (length(actor_generation_snapshot) = 64 "
            "         AND actor_generation_snapshot NOT GLOB '*[^0-9a-f]*'))",
            name="ck_message_deliveries_identity_generations",
        ),
        CheckConstraint(
            "actor_kind IN ('agent', 'ui_user', 'system') "
            "AND ((actor_kind = 'agent' "
            "      AND actor_id > 0 "
            "      AND actor_project_id_snapshot > 0 "
            "      AND actor_project_slug_snapshot IS NOT NULL "
            "      AND actor_project_generation_snapshot IS NOT NULL "
            "      AND actor_generation_snapshot IS NOT NULL "
            "      AND actor_id = sender_id "
            "      AND actor_name_snapshot = sender_name_snapshot "
            "      AND actor_project_id_snapshot = sender_project_id_snapshot "
            "      AND actor_project_slug_snapshot = sender_project_slug_snapshot "
            "      AND actor_project_generation_snapshot = sender_project_generation_snapshot "
            "      AND actor_generation_snapshot = sender_generation_snapshot "
            "      AND actor_epoch_snapshot IS NULL) "
            " OR  (actor_kind = 'ui_user' "
            "      AND actor_id > 0 "
            "      AND actor_project_id_snapshot > 0 "
            "      AND actor_project_slug_snapshot IS NOT NULL "
            "      AND actor_project_generation_snapshot IS NOT NULL "
            "      AND actor_project_id_snapshot = sender_project_id_snapshot "
            "      AND actor_project_slug_snapshot = sender_project_slug_snapshot "
            "      AND actor_project_generation_snapshot = sender_project_generation_snapshot "
            "      AND actor_generation_snapshot IS NOT NULL "
            "      AND actor_epoch_snapshot >= 1) "
            " OR  (actor_kind = 'system' "
            "      AND actor_id = 0 "
            "      AND actor_name_snapshot = 'system' "
            "      AND actor_project_id_snapshot IS NULL "
            "      AND actor_project_slug_snapshot IS NULL "
            "      AND actor_project_generation_snapshot IS NULL "
            "      AND actor_generation_snapshot IS NULL "
            "      AND actor_epoch_snapshot IS NULL))",
            name="ck_message_deliveries_actor_provenance",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0 "
            "AND length(request_sha256) = 64 "
            "AND request_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_message_deliveries_idempotency",
        ),
        CheckConstraint(
            "length(archive_document) > 0 "
            "AND length(document_sha256) = 64 "
            "AND document_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_message_deliveries_archive_hash",
        ),
        CheckConstraint(
            "lease_fence >= 0 AND attempt_count >= 0 AND backoff_seconds >= 0 "
            "AND ((lease_token IS NULL AND lease_expires_ts IS NULL) "
            "     OR (lease_token IS NOT NULL "
            "         AND length(trim(lease_token)) > 0 "
            "         AND lease_fence >= 1 "
            "         AND lease_expires_ts IS NOT NULL))",
            name="ck_message_deliveries_lease",
        ),
        CheckConstraint(
            "(state = 'pending' "
            " AND message_id IS NULL "
            " AND published_ts IS NULL "
            " AND quarantined_ts IS NULL "
            " AND quarantine_reason IS NULL "
            " AND next_attempt_ts IS NOT NULL) "
            "OR (state = 'published' "
            " AND message_id IS NOT NULL "
            " AND published_ts IS NOT NULL "
            " AND archive_commit_sha IS NOT NULL "
            " AND archive_relative_path IS NOT NULL "
            " AND archive_blob_sha IS NOT NULL "
            " AND quarantined_ts IS NULL "
            " AND quarantine_reason IS NULL "
            " AND next_attempt_ts IS NULL "
            " AND lease_token IS NULL "
            " AND lease_expires_ts IS NULL) "
            "OR (state = 'quarantined' "
            " AND message_id IS NULL "
            " AND published_ts IS NULL "
            " AND quarantined_ts IS NOT NULL "
            " AND quarantine_reason IS NOT NULL "
            " AND length(trim(quarantine_reason)) > 0 "
            " AND next_attempt_ts IS NULL "
            " AND lease_token IS NULL "
            " AND lease_expires_ts IS NULL)",
            name="ck_message_deliveries_state_fields",
        ),
        CheckConstraint(
            "(archive_relative_path IS NULL "
            " AND archive_blob_sha IS NULL "
            " AND archive_commit_sha IS NULL) "
            "OR (archive_commit_sha IS NOT NULL "
            "    AND length(archive_commit_sha) IN (40, 64) "
            "    AND archive_commit_sha NOT GLOB '*[^0-9a-f]*' "
            "    AND archive_relative_path IS NOT NULL "
            "    AND archive_relative_path = "
            "        'projects/' || project_slug_snapshot || "
            "        '/message_deliveries/' || id || '.md' "
            "    AND archive_blob_sha IS NOT NULL "
            "    AND length(archive_blob_sha) IN (40, 64) "
            "    AND archive_blob_sha NOT GLOB '*[^0-9a-f]*')",
            name="ck_message_deliveries_receipt",
        ),
        Index(
            "uq_message_deliveries_idempotency",
            "project_id",
            "project_generation_snapshot",
            "actor_kind",
            "actor_id",
            text("coalesce(actor_generation_snapshot, '')"),
            text("coalesce(actor_project_generation_snapshot, '')"),
            "idempotency_key",
            unique=True,
        ),
        Index(
            "idx_message_deliveries_due",
            "next_attempt_ts",
            "lease_expires_ts",
            sqlite_where=text("state = 'pending'"),
        ),
        Index(
            "idx_message_deliveries_project_created",
            "project_id",
            "created_ts",
        ),
        Index(
            "idx_message_deliveries_reply_pending",
            "reply_to_message_id",
            "state",
        ),
    )

    id: str = Field(default_factory=_new_delivery_id, primary_key=True, max_length=36)
    state: str = Field(default="pending", max_length=16)
    delivery_kind: str = Field(default="message", max_length=32)
    project_id: int
    project_slug_snapshot: str = Field(max_length=255)
    project_generation_snapshot: str = Field(max_length=64)
    sender_project_id_snapshot: int
    sender_project_slug_snapshot: str = Field(max_length=255)
    sender_project_generation_snapshot: str = Field(max_length=64)
    sender_id: int
    sender_name_snapshot: str = Field(max_length=128)
    sender_generation_snapshot: str = Field(max_length=64)
    actor_kind: str = Field(max_length=16)
    actor_id: int = Field(default=0)
    actor_name_snapshot: str = Field(max_length=128)
    actor_project_id_snapshot: Optional[int] = Field(default=None)
    actor_project_slug_snapshot: Optional[str] = Field(default=None, max_length=255)
    actor_project_generation_snapshot: Optional[str] = Field(default=None, max_length=64)
    actor_generation_snapshot: Optional[str] = Field(default=None, max_length=64)
    actor_epoch_snapshot: Optional[int] = Field(default=None)
    idempotency_key: str = Field(max_length=128)
    request_sha256: str = Field(max_length=64)
    thread_id: Optional[str] = Field(default=None, max_length=128)
    reply_to_message_id: Optional[int] = Field(default=None)
    topic: Optional[str] = Field(default=None, max_length=64)
    subject: str = Field(max_length=512)
    body_md: str
    importance: str = Field(default="normal", max_length=16)
    ack_required: bool = Field(default=False)
    attachments: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default="[]"),
    )
    archive_document: str
    document_sha256: str = Field(max_length=64)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    lease_token: Optional[str] = Field(default=None, max_length=128)
    lease_fence: int = Field(default=0)
    lease_expires_ts: Optional[datetime] = Field(default=None)
    attempt_count: int = Field(default=0)
    backoff_seconds: int = Field(default=0)
    next_attempt_ts: Optional[datetime] = Field(default_factory=_utcnow_naive)
    last_attempt_ts: Optional[datetime] = Field(default=None)
    last_error: Optional[str] = Field(default=None)
    archive_relative_path: Optional[str] = Field(default=None, max_length=1024)
    archive_blob_sha: Optional[str] = Field(default=None, max_length=64)
    archive_commit_sha: Optional[str] = Field(default=None, max_length=64)
    message_id: Optional[int] = Field(default=None)
    published_ts: Optional[datetime] = Field(default=None)
    quarantined_ts: Optional[datetime] = Field(default=None)
    quarantine_reason: Optional[str] = Field(default=None)


class MessageDeliveryRecipient(SQLModel, table=True):
    """Ordered recipient identity snapshot owned by a delivery intent."""

    __tablename__ = "message_delivery_recipients"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_message_delivery_recipients_ordinal"),
        CheckConstraint(
            "kind IN ('to', 'cc', 'bcc')",
            name="ck_message_delivery_recipients_kind",
        ),
        CheckConstraint(
            "length(agent_generation_snapshot) = 64 "
            "AND agent_generation_snapshot NOT GLOB '*[^0-9a-f]*'",
            name="ck_message_delivery_recipients_generation",
        ),
        UniqueConstraint(
            "delivery_id",
            "agent_id",
            name="uq_message_delivery_recipients_agent",
        ),
        Index(
            "idx_message_delivery_recipients_agent",
            "agent_id",
            "delivery_id",
        ),
    )

    delivery_id: str = Field(foreign_key="message_deliveries.id", primary_key=True, max_length=36)
    ordinal: int = Field(primary_key=True)
    kind: str = Field(max_length=8)
    agent_id: int
    agent_name_snapshot: str = Field(max_length=128)
    agent_generation_snapshot: str = Field(max_length=64)
    project_id_snapshot: int


class FileReservation(SQLModel, table=True):
    __tablename__ = "file_reservations"
    __table_args__ = (
        Index("idx_file_reservations_project_released_expires", "project_id", "released_ts", "expires_ts"),
        Index("idx_file_reservations_project_agent_released", "project_id", "agent_id", "released_ts"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    # Nullable so a reservation can outlive its owning agent — when the agent
    # row is deleted (manual cleanup, project hygiene, etc.) the reservation
    # becomes "orphaned" and must still be discoverable so it can be
    # auto-released by the staleness sweeper instead of pinning the path
    # forever. (#161)
    agent_id: Optional[int] = Field(default=None, foreign_key="agents.id", index=True)
    path_pattern: str = Field(max_length=512)
    exclusive: bool = Field(default=True)
    reason: str = Field(default="", max_length=512)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    expires_ts: datetime
    released_ts: Optional[datetime] = None


class AgentLink(SQLModel, table=True):
    """Directed contact link request from agent A to agent B.

    When approved, messages may be sent cross-project between A and B.
    """

    __tablename__ = "agent_links"
    __table_args__ = (UniqueConstraint("a_project_id", "a_agent_id", "b_project_id", "b_agent_id", name="uq_agentlink_pair"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    a_project_id: int = Field(foreign_key="projects.id", index=True)
    a_agent_id: int = Field(foreign_key="agents.id", index=True)
    b_project_id: int = Field(foreign_key="projects.id", index=True)
    b_agent_id: int = Field(foreign_key="agents.id", index=True)
    status: str = Field(default="pending", max_length=16)  # pending | approved | blocked
    reason: str = Field(default="", max_length=512)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    updated_ts: datetime = Field(default_factory=_utcnow_naive)
    expires_ts: Optional[datetime] = None


class WindowIdentity(SQLModel, table=True):
    """Persistent window-based agent identity tied to a tmux/terminal window.

    Agents that share the same window_uuid within a project share a persistent
    identity that survives session restarts, eliminating per-session registration
    overhead and enabling tracking of which window/pane is doing what.
    """

    __tablename__ = "window_identities"
    __table_args__ = (
        UniqueConstraint("project_id", "window_uuid", name="uq_window_identity_project_uuid"),
        Index("idx_window_identities_project_active", "project_id", "expires_ts"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    window_uuid: str = Field(max_length=64, index=True)
    display_name: str = Field(max_length=128)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    last_active_ts: datetime = Field(default_factory=_utcnow_naive)
    expires_ts: Optional[datetime] = Field(default=None)


class MessageSummary(SQLModel, table=True):
    """Stored on-demand project-wide message summary."""

    __tablename__ = "message_summaries"
    __table_args__ = (
        Index("idx_summaries_project_end", "project_id", "end_ts"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    summary_text: str
    start_ts: datetime
    end_ts: datetime
    source_message_count: int = Field(default=0)
    source_thread_ids: str = Field(default="[]")  # JSON array of thread IDs
    llm_model: Optional[str] = Field(default=None, max_length=128)
    cost_usd: Optional[float] = Field(default=None)
    created_ts: datetime = Field(default_factory=_utcnow_naive)


class UiUser(SQLModel, table=True):
    """A human login for the ``/mail`` web viewer.

    Deliberately separate from :class:`Agent`: agents authenticate with a bearer
    token or a per-agent registration token over MCP, humans authenticate with a
    password in a browser. Conflating them would give every registered agent a
    way into the destructive UI routes.

    ``session_epoch`` is bumped whenever authentication or authorization state
    changes. ``session_generation`` is immutable for one account lifetime and
    changes when a deleted username is recreated. Both values are embedded in
    the signed session cookie and compared on every request, so state changes
    and account replacement revoke live sessions without a server-side session
    table (see :mod:`mcp_agent_mail.webauth`).
    """

    __tablename__ = "ui_users"
    __table_args__ = (
        CheckConstraint(
            "display_name IS NULL OR (length(trim(display_name)) > 0 "
            "AND length(display_name) <= 128)",
            name="ck_ui_users_display_name",
        ),
        CheckConstraint(
            "profile_revision >= 1",
            name="ck_ui_users_profile_revision",
        ),
        CheckConstraint(
            "preferred_ui_locale IN ('en', 'pl')",
            name="ck_ui_users_preferred_ui_locale",
        ),
        CheckConstraint(
            "preferred_correspondence_locale IS NULL "
            "OR preferred_correspondence_locale IN ('en', 'pl')",
            name="ck_ui_users_preferred_correspondence_locale",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True, max_length=64)
    password_hash: str = Field(max_length=256)
    role: str = Field(default="member", max_length=16)
    disabled: bool = Field(default=False)
    session_epoch: int = Field(default=1)
    session_generation: str = Field(default_factory=_new_session_generation, max_length=64)
    display_name: Optional[str] = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
    )
    profile_revision: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default="1"),
    )
    preferred_ui_locale: str = Field(
        default="en",
        sa_column=Column(String(2), nullable=False, server_default="en"),
    )
    preferred_correspondence_locale: Optional[str] = Field(
        default=None,
        sa_column=Column(String(2), nullable=True),
    )
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    last_login_ts: Optional[datetime] = Field(default=None)


class UiProjectAssignment(SQLModel, table=True):
    """A human user's explicit role within one project.

    Global administrators do not require assignment rows. A global member has
    no project access unless a row exists, and the row's role determines whether
    that access is read-only or permits operator actions.
    """

    __tablename__ = "ui_project_assignments"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "project_id",
            name="uq_ui_project_assignment_user_project",
        ),
        Index(
            "idx_ui_project_assignments_user_project",
            "user_id",
            "project_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="ui_users.id", ondelete="CASCADE", index=True)
    project_id: int = Field(foreign_key="projects.id", ondelete="CASCADE", index=True)
    role: str = Field(default="viewer", max_length=16)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    updated_ts: datetime = Field(default_factory=_utcnow_naive)


class UiAccessAuditEvent(SQLModel, table=True):
    """Immutable record of one effective human project-access change.

    The identifiers deliberately are snapshots rather than foreign keys. An
    administrator may later remove an account or archive/delete a project, but
    those lifecycle operations must never erase or rewrite the security audit.
    The account generation distinguishes a recreated username from its previous
    lifetime even if SQLite reuses the numeric primary key.
    """

    __tablename__ = "ui_access_audit_events"
    __table_args__ = (
        CheckConstraint(
            "old_role IS NULL OR old_role IN ('viewer', 'operator')",
            name="ck_ui_access_audit_old_role",
        ),
        CheckConstraint(
            "new_role IS NULL OR new_role IN ('viewer', 'operator')",
            name="ck_ui_access_audit_new_role",
        ),
        CheckConstraint(
            "old_role IS NOT new_role",
            name="ck_ui_access_audit_effective_change",
        ),
        CheckConstraint(
            "target_epoch_after = target_epoch_before + 1",
            name="ck_ui_access_audit_epoch_step",
        ),
        CheckConstraint(
            "length(target_account_generation) = 64",
            name="ck_ui_access_audit_target_generation",
        ),
        CheckConstraint(
            "length(project_generation_snapshot) = 64",
            name="ck_ui_access_audit_project_generation",
        ),
        CheckConstraint(
            "(actor_user_id IS NULL AND actor_account_generation_snapshot IS NULL "
            "AND actor_session_epoch_snapshot IS NULL) OR "
            "(actor_user_id IS NOT NULL "
            "AND length(actor_account_generation_snapshot) = 64 "
            "AND actor_session_epoch_snapshot >= 1)",
            name="ck_ui_access_audit_actor_provenance",
        ),
        Index(
            "idx_ui_access_audit_target_created",
            "target_user_id",
            "created_ts",
        ),
        Index(
            "idx_ui_access_audit_project_created",
            "project_id",
            "created_ts",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    actor_user_id: Optional[int] = Field(default=None, index=True)
    actor_username_snapshot: str = Field(max_length=64)
    actor_account_generation_snapshot: Optional[str] = Field(default=None, max_length=64)
    actor_session_epoch_snapshot: Optional[int] = Field(default=None)
    target_user_id: int = Field(index=True)
    target_username_snapshot: str = Field(max_length=64)
    target_account_generation: str = Field(max_length=64)
    project_id: int = Field(index=True)
    project_slug_snapshot: str = Field(max_length=255)
    project_generation_snapshot: str = Field(max_length=64)
    old_role: Optional[str] = Field(default=None, max_length=16)
    new_role: Optional[str] = Field(default=None, max_length=16)
    target_epoch_before: int
    target_epoch_after: int
    created_ts: datetime = Field(default_factory=_utcnow_naive)


class ProjectSiblingSuggestion(SQLModel, table=True):
    """LLM-ranked sibling project suggestion (undirected pair)."""

    __tablename__ = "project_sibling_suggestions"
    __table_args__ = (UniqueConstraint("project_a_id", "project_b_id", name="uq_project_sibling_pair"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    project_a_id: int = Field(foreign_key="projects.id", index=True)
    project_b_id: int = Field(foreign_key="projects.id", index=True)
    score: float = Field(default=0.0)
    status: str = Field(default="suggested", max_length=16)  # suggested | confirmed | dismissed
    rationale: str = Field(default="", max_length=4096)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    evaluated_ts: datetime = Field(default_factory=_utcnow_naive)
    confirmed_ts: Optional[datetime] = Field(default=None)
    dismissed_ts: Optional[datetime] = Field(default=None)
