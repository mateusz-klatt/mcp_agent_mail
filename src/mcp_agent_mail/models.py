"""SQLModel data models representing agents, messages, projects, and file reservations."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Optional

from sqlalchemy import CheckConstraint, Column, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel


class MailUiLocale(StrEnum):
    """Canonical closed set of human interface and correspondence locales."""

    AR = "ar"
    BN = "bn"
    BS = "bs"
    CS = "cs"
    DA = "da"
    DE = "de"
    EL = "el"
    EN = "en"
    ES = "es"
    FA = "fa"
    FI = "fi"
    FIL = "fil"
    FR = "fr"
    GA = "ga"
    HE = "he"
    HI = "hi"
    HR = "hr"
    HU = "hu"
    HY = "hy"
    ID = "id"
    IS = "is"
    IT = "it"
    JA = "ja"
    KO = "ko"
    LT = "lt"
    LV = "lv"
    MS = "ms"
    MY_MM = "my-MM"
    NL = "nl"
    NO = "no"
    PL = "pl"
    PT = "pt"
    RO = "ro"
    RU = "ru"
    SK = "sk"
    SQ = "sq"
    SR = "sr"
    SV = "sv"
    SW = "sw"
    TH = "th"
    TR = "tr"
    UK = "uk"
    VI = "vi"
    ZH_HANT = "zh-Hant"
    ZH = "zh"

    @classmethod
    def canonicalize(cls, value: str) -> "MailUiLocale | None":
        """Map case-insensitive human input back to its canonical BCP-47 tag."""
        folded = value.strip().casefold()
        return next((locale for locale in cls if locale.value.casefold() == folded), None)


MAIL_UI_LOCALE_VALUES = tuple(locale.value for locale in MailUiLocale)
_MAIL_UI_LOCALE_SQL = ", ".join(repr(value) for value in MAIL_UI_LOCALE_VALUES)
MAIL_UI_LOCALE_ENGLISH_NAMES: dict[MailUiLocale, str] = {
    MailUiLocale.AR: "Arabic",
    MailUiLocale.BN: "Bengali",
    MailUiLocale.BS: "Bosnian",
    MailUiLocale.CS: "Czech",
    MailUiLocale.DA: "Danish",
    MailUiLocale.DE: "German",
    MailUiLocale.EL: "Greek",
    MailUiLocale.EN: "English",
    MailUiLocale.ES: "Spanish",
    MailUiLocale.FA: "Persian",
    MailUiLocale.FI: "Finnish",
    MailUiLocale.FIL: "Filipino",
    MailUiLocale.FR: "French",
    MailUiLocale.GA: "Irish",
    MailUiLocale.HE: "Hebrew",
    MailUiLocale.HI: "Hindi",
    MailUiLocale.HR: "Croatian",
    MailUiLocale.HU: "Hungarian",
    MailUiLocale.HY: "Armenian",
    MailUiLocale.ID: "Indonesian",
    MailUiLocale.IS: "Icelandic",
    MailUiLocale.IT: "Italian",
    MailUiLocale.JA: "Japanese",
    MailUiLocale.KO: "Korean",
    MailUiLocale.LT: "Lithuanian",
    MailUiLocale.LV: "Latvian",
    MailUiLocale.MS: "Malay",
    MailUiLocale.MY_MM: "Burmese",
    MailUiLocale.NL: "Dutch",
    MailUiLocale.NO: "Norwegian",
    MailUiLocale.PL: "Polish",
    MailUiLocale.PT: "Portuguese",
    MailUiLocale.RO: "Romanian",
    MailUiLocale.RU: "Russian",
    MailUiLocale.SK: "Slovak",
    MailUiLocale.SQ: "Albanian",
    MailUiLocale.SR: "Serbian",
    MailUiLocale.SV: "Swedish",
    MailUiLocale.SW: "Swahili",
    MailUiLocale.TH: "Thai",
    MailUiLocale.TR: "Turkish",
    MailUiLocale.UK: "Ukrainian",
    MailUiLocale.VI: "Vietnamese",
    MailUiLocale.ZH_HANT: "Traditional Chinese",
    MailUiLocale.ZH: "Simplified Chinese",
}


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


def _new_execution_id() -> str:
    """Return a canonical UUID for one bounded agent execution."""
    return str(uuid.uuid4())


def _new_delivery_id() -> str:
    """Return a canonical UUID for one durable message-delivery intent."""
    return str(uuid.uuid4())


def _new_discussion_thread_id() -> str:
    """Return an unguessable, immutable identifier for one ticket's discussion.

    A ticket's discussion is ordinary mail, so it needs a ``Message.thread_id``. It must
    NOT be the ticket key: ``thread_id`` (models.py) carries no unique constraint and is
    supplied by the caller, so any agent can send ``thread_id="AM-12"`` before that ticket
    exists and nothing stops them. Binding a ticket's identity to a namespace anyone may
    occupy and nothing reserves is not a collision risk -- it is the absence of a
    reservation. An opaque random id is the reservation.

    The shape stays inside ``validate_thread_id_format`` (ASCII alphanumerics plus
    ``.``, ``_`` and ``-``, at most 128 characters), so it is a legal thread id verbatim.
    """
    return f"tkt-{secrets.token_hex(16)}"


class Project(SQLModel, table=True):
    __tablename__ = "projects"
    __table_args__ = (
        Index("uq_projects_project_uid", "project_uid", unique=True),
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
    # ``project_uid`` is the durable repository identity.  It is nullable only
    # for rows created before this column existed: such rows are claimed lazily
    # after an exact, unambiguous identity match instead of being bulk-merged
    # during migration.
    project_uid: Optional[str] = Field(default=None, max_length=255)
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
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_agent_project_name"),
        CheckConstraint(
            "provisioning_state IN ('provisioning', 'active')",
            name="ck_agents_provisioning_state",
        ),
    )

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
    # A newly inserted mailbox is not addressable until its private credential
    # and Git profile have both been published successfully. Existing databases
    # are backfilled to ``active`` by the additive schema migration.
    provisioning_state: str = Field(
        default="active",
        sa_column=Column(
            String(16),
            nullable=False,
            server_default=text("'active'"),
        ),
    )
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


class AgentExecution(SQLModel, table=True):
    """One session or subagent run owned by a durable Agent identity."""

    __tablename__ = "agent_executions"
    __table_args__ = (
        CheckConstraint(
            "length(id) = 36 "
            "AND substr(id, 9, 1) = '-' "
            "AND substr(id, 14, 1) = '-' "
            "AND substr(id, 19, 1) = '-' "
            "AND substr(id, 24, 1) = '-' "
            "AND lower(id) = id "
            "AND length(replace(id, '-', '')) = 32 "
            "AND replace(id, '-', '') NOT GLOB '*[^0-9a-f]*'",
            name="ck_agent_executions_uuid",
        ),
        CheckConstraint(
            "length(external_id) BETWEEN 1 AND 255 "
            "AND length(trim(external_id)) > 0",
            name="ck_agent_executions_external_id",
        ),
        CheckConstraint(
            "length(client_name) BETWEEN 1 AND 128 "
            "AND length(trim(client_name)) > 0",
            name="ck_agent_executions_client_name",
        ),
        CheckConstraint(
            "length(execution_token_hash) = 64 "
            "AND lower(execution_token_hash) = execution_token_hash "
            "AND execution_token_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_agent_executions_token_hash",
        ),
        CheckConstraint(
            "lifecycle_protocol_version >= 0",
            name="ck_agent_executions_lifecycle_protocol_version",
        ),
        CheckConstraint(
            "turn_id IS NULL OR (length(turn_id) BETWEEN 1 AND 255 "
            "AND length(trim(turn_id)) > 0)",
            name="ck_agent_executions_turn_id",
        ),
        CheckConstraint(
            "agent_type IS NULL OR (length(agent_type) BETWEEN 1 AND 128 "
            "AND length(trim(agent_type)) > 0)",
            name="ck_agent_executions_agent_type",
        ),
        CheckConstraint(
            "model IS NULL OR (length(model) BETWEEN 1 AND 128 "
            "AND length(trim(model)) > 0)",
            name="ck_agent_executions_model",
        ),
        CheckConstraint(
            "permission_mode IS NULL OR (length(permission_mode) BETWEEN 1 AND 64 "
            "AND length(trim(permission_mode)) > 0)",
            name="ck_agent_executions_permission_mode",
        ),
        CheckConstraint(
            "kind IN ('session', 'subagent')",
            name="ck_agent_executions_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'completed', 'failed', 'cancelled', 'expired')",
            name="ck_agent_executions_status",
        ),
        CheckConstraint(
            "(kind = 'session' AND parent_execution_id IS NULL) "
            "OR (kind = 'subagent' AND parent_execution_id IS NOT NULL "
            "    AND parent_execution_id != id)",
            name="ck_agent_executions_kind_parent",
        ),
        CheckConstraint(
            "head_sha IS NULL OR ("
            "length(head_sha) = 40 "
            "AND lower(head_sha) = head_sha "
            "AND head_sha NOT GLOB '*[^0-9a-f]*'"
            ")",
            name="ck_agent_executions_head_sha",
        ),
        CheckConstraint(
            "length(task_description) <= 2048",
            name="ck_agent_executions_task_description",
        ),
        CheckConstraint(
            "(cwd IS NULL OR (length(cwd) BETWEEN 1 AND 2048 AND length(trim(cwd)) > 0)) "
            "AND (repo_root IS NULL OR (length(repo_root) BETWEEN 1 AND 2048 "
            "     AND length(trim(repo_root)) > 0)) "
            "AND (git_common_dir IS NULL OR (length(git_common_dir) BETWEEN 1 AND 2048 "
            "     AND length(trim(git_common_dir)) > 0)) "
            "AND (worktree_path IS NULL OR (length(worktree_path) BETWEEN 1 AND 2048 "
            "     AND length(trim(worktree_path)) > 0)) "
            "AND (branch IS NULL OR (length(branch) BETWEEN 1 AND 512 "
            "     AND length(trim(branch)) > 0))",
            name="ck_agent_executions_workspace_metadata",
        ),
        CheckConstraint(
            "(status = 'active' AND ended_ts IS NULL) "
            "OR (status != 'active' AND ended_ts IS NOT NULL)",
            name="ck_agent_executions_status_end",
        ),
        CheckConstraint(
            "last_active_ts >= started_ts "
            "AND (ended_ts IS NULL OR ended_ts >= last_active_ts)",
            name="ck_agent_executions_timestamps",
        ),
        Index(
            "uq_agent_executions_session_external",
            "agent_id",
            "client_name",
            "external_id",
            unique=True,
            sqlite_where=text("kind = 'session'"),
        ),
        Index(
            "uq_agent_executions_subagent_external",
            "parent_execution_id",
            "client_name",
            "external_id",
            unique=True,
            sqlite_where=text("kind = 'subagent'"),
        ),
        Index(
            "idx_agent_executions_active",
            "project_id",
            "agent_id",
            "last_active_ts",
            sqlite_where=text("status = 'active'"),
        ),
        Index(
            "idx_agent_executions_active_stale",
            "last_active_ts",
            "project_id",
            "id",
            sqlite_where=text("status = 'active'"),
        ),
        Index(
            "idx_agent_executions_project_active_stale",
            "project_id",
            "last_active_ts",
            "id",
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: str = Field(default_factory=_new_execution_id, primary_key=True, max_length=36)
    project_id: int = Field(foreign_key="projects.id", index=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    parent_execution_id: Optional[str] = Field(
        default=None,
        foreign_key="agent_executions.id",
        index=True,
        max_length=36,
    )
    external_id: str = Field(max_length=255)
    client_name: str = Field(max_length=128)
    execution_token_hash: str = Field(
        repr=False,
        sa_column=Column(String(64), nullable=False, unique=True),
    )
    lifecycle_protocol_version: int = Field(default=0)
    turn_id: Optional[str] = Field(default=None, max_length=255)
    agent_type: Optional[str] = Field(default=None, max_length=128)
    model: Optional[str] = Field(default=None, max_length=128)
    permission_mode: Optional[str] = Field(default=None, max_length=64)
    kind: str = Field(default="session", max_length=16)
    status: str = Field(default="active", max_length=16)
    task_description: str = Field(default="", max_length=2048)
    cwd: Optional[str] = Field(default=None, max_length=2048)
    repo_root: Optional[str] = Field(default=None, max_length=2048)
    git_common_dir: Optional[str] = Field(default=None, max_length=2048)
    worktree_path: Optional[str] = Field(default=None, max_length=2048)
    branch: Optional[str] = Field(default=None, max_length=512)
    head_sha: Optional[str] = Field(default=None, max_length=40)
    started_ts: datetime = Field(default_factory=_utcnow_naive)
    last_active_ts: datetime = Field(default_factory=_utcnow_naive)
    ended_ts: Optional[datetime] = Field(default=None)


class BuildSlotArtifactProjection(SQLModel, table=True):
    """Durable outbox row for one terminal execution's build-slot JSON."""

    __tablename__ = "build_slot_artifact_projections"
    __table_args__ = (
        Index(
            "idx_build_slot_artifact_projections_pending",
            "project_id",
            "execution_id",
            sqlite_where=text("reconciled_ts IS NULL"),
        ),
    )

    execution_id: str = Field(
        foreign_key="agent_executions.id",
        primary_key=True,
        max_length=36,
    )
    project_id: int = Field(foreign_key="projects.id")
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    reconciled_ts: Optional[datetime] = Field(default=None)


class BuildSlotArtifactPath(SQLModel, table=True):
    """Exact immutable archive path registered by an execution-owned lease."""

    __tablename__ = "build_slot_artifact_paths"
    __table_args__ = (
        CheckConstraint(
            "length(slot_path_component) BETWEEN 1 AND 80 "
            "AND slot_path_component NOT IN ('.', '..') "
            "AND instr(slot_path_component, '/') = 0 "
            "AND instr(slot_path_component, char(92)) = 0",
            name="ck_build_slot_artifact_paths_component",
        ),
        CheckConstraint(
            "length(slot_name) BETWEEN 1 AND 512 "
            "AND length(trim(slot_name)) > 0",
            name="ck_build_slot_artifact_paths_slot_name",
        ),
        Index(
            "idx_build_slot_artifact_paths_project_execution",
            "project_id",
            "execution_id",
            "slot_path_component",
        ),
    )

    execution_id: str = Field(
        foreign_key="agent_executions.id",
        primary_key=True,
        max_length=36,
    )
    slot_path_component: str = Field(primary_key=True, max_length=80)
    project_id: int = Field(foreign_key="projects.id")
    slot_name: str = Field(max_length=512)
    created_ts: datetime = Field(default_factory=_utcnow_naive)


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
        Index("idx_file_reservations_execution", "execution_id"),
        Index(
            "idx_file_reservations_archive_pending",
            "project_id",
            "id",
            sqlite_where=text(
                "archive_synced_revision < archive_revision"
            ),
        ),
        CheckConstraint(
            "origin IN ('auto', 'explicit')",
            name="ck_file_reservations_origin",
        ),
        CheckConstraint(
            "archive_revision >= 1",
            name="ck_file_reservations_archive_revision",
        ),
        CheckConstraint(
            "archive_synced_revision >= 0 "
            "AND archive_synced_revision <= archive_revision",
            name="ck_file_reservations_archive_synced_revision",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    # Nullable so a reservation can outlive its owning agent — when the agent
    # row is deleted (manual cleanup, project hygiene, etc.) the reservation
    # becomes "orphaned" and must still be discoverable so it can be
    # auto-released by the staleness sweeper instead of pinning the path
    # forever. (#161)
    agent_id: Optional[int] = Field(default=None, foreign_key="agents.id", index=True)
    # Legacy reservations remain nullable. New execution-aware claims bind to
    # the exact run that owns them so sibling runs of one Agent still conflict.
    execution_id: Optional[str] = Field(
        default=None,
        foreign_key="agent_executions.id",
        max_length=36,
    )
    origin: str = Field(default="explicit", max_length=16)
    path_pattern: str = Field(max_length=512)
    exclusive: bool = Field(default=True)
    reason: str = Field(default="", max_length=512)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    expires_ts: datetime
    released_ts: Optional[datetime] = None
    # DB state is authoritative. Every artifact-visible mutation advances
    # ``archive_revision`` in SQLite; publication acknowledges only the exact
    # revision that reached the Git archive. A crash or I/O failure therefore
    # leaves a durable, retryable ``archive_synced_revision < archive_revision``
    # row instead of relying on the old lease TTL to hide stale guard JSON.
    archive_revision: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default="1"),
    )
    archive_synced_revision: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )


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
            f"preferred_ui_locale IN ({_MAIL_UI_LOCALE_SQL})",
            name="ck_ui_users_preferred_ui_locale",
        ),
        CheckConstraint(
            "preferred_correspondence_locale IS NULL "
            f"OR preferred_correspondence_locale IN ({_MAIL_UI_LOCALE_SQL})",
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
        sa_column=Column(String(16), nullable=False, server_default="en"),
    )
    preferred_correspondence_locale: Optional[str] = Field(
        default=None,
        sa_column=Column(String(16), nullable=True),
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


# =================================================================================================
# Ticketing
# =================================================================================================
#
# Design rule, and it is a SQLite rule rather than a taste: a table-level CHECK is a promise that
# its vocabulary is closed forever, because widening one requires ``op.batch_alter_table`` -- a full
# copy of the table that also silently drops dependent triggers and indexes (migrations/env.py:45,
# migrations/script.py.mako:7-8). The ticket table therefore constrains SHAPE only: lengths,
# character classes, temporal ordering, non-negativity -- predicates that are true for any
# vocabulary. Membership (`open` / `in_progress` / `closed`, `epic` / `task` / ...) is a module
# constant in ``tickets.py`` and is enforced on write, exactly as ``ProjectSiblingSuggestion.status``
# (models.py:1139) and ``Agent.contact_policy`` (models.py:243) already are.


class TicketSequence(SQLModel, table=True):
    """Per-project allocator for human-readable ticket keys (``AM-12``).

    A stored counter rather than ``MAX(seq) + 1`` over ``tickets``: SQLite reuses integer row ids
    after a delete, which is why ``MessageDelivery`` carries a UUID beside its rowid
    (models.py:560-562) and why ``Project``/``Agent`` carry random ``*_generation`` columns
    (models.py:180-187, 230-237). A ticket key is quoted into mail subjects, ``topic`` tags,
    reservation ``reason`` strings and commit messages, and neither ``messages`` nor the Git archive
    has any content-rewrite path -- so the number must be minted once and never reissued, even if a
    future retention command deletes closed tickets.

    A dedicated table rather than a column on ``projects``: adding a column to the live ``projects``
    table is an ALTER this design otherwise never needs.

    ``prefix`` is GLOBALLY unique, not unique per project. A key pasted into cross-project mail
    carries no project context, so a per-project prefix would let two of the four projects both mint
    ``AM-1``. Global uniqueness is also the reversible direction: global -> per-project is a
    constraint relaxation and is free; per-project -> global would require renaming keys that are
    already frozen in immutable archive documents.

    ``prefix`` is STORED, never derived from ``Project.slug``. Deriving it would make a mutable field
    load-bearing -- rename the project and every memorised key points at nothing. That is the exact
    failure reasoned about for ``Agent.display_name`` at models.py:258-276.
    """

    __tablename__ = "ticket_sequences"
    __table_args__ = (
        UniqueConstraint("prefix", name="uq_ticket_sequences_prefix"),
        CheckConstraint(
            "length(prefix) BETWEEN 2 AND 12 "
            "AND upper(prefix) = prefix "
            "AND substr(prefix, 1, 1) GLOB '[A-Z]' "
            "AND prefix NOT GLOB '*[^A-Z0-9]*'",
            name="ck_ticket_sequences_prefix",
        ),
        CheckConstraint("next_seq >= 1", name="ck_ticket_sequences_next_seq"),
    )

    project_id: int = Field(foreign_key="projects.id", primary_key=True)
    prefix: str = Field(max_length=12)
    next_seq: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default="1"),
    )
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    updated_ts: datetime = Field(default_factory=_utcnow_naive)


class Ticket(SQLModel, table=True):
    """One tracked unit of work: an epic, a task, a bug, or any future kind.

    There is deliberately no ``epics`` table. An epic is a ticket whose ``kind_key`` is ``'epic'``
    and whose children point at it through ``parent_id``. Two tables would have duplicated every
    link, event, filter and authorization predicate, and would have made a third level (sub-task,
    initiative, spike) a third table. Here a third level is a new member of one module constant.

    Discussion is NOT stored here. A ticket's conversation is ordinary mail whose ``Message.topic``
    equals this ``key``: already indexed (``idx_messages_project_topic``, models.py:533), already
    delivered to inboxes, already carrying read receipts and ACK (models.py:523-524), already
    committed to the Git archive, and already searchable through ``fts_messages`` (db.py:2346). A
    private comment table would have had to grow all of that from scratch and would still have been
    the one unsearchable corpus in a server built for searchable coordination.

    ``key`` is the character class ``send_message`` accepts for ``topic`` (app.py:10546:
    ``[A-Za-z0-9][A-Za-z0-9._-]*``, length <= 64), so every key is a legal ``topic`` and a legal
    ``thread_id`` (models.py:545, 128 chars) verbatim, with no escaping anywhere. The class is
    deliberately a SUPERSET of the shape we generate: it also admits ``bd-10s`` and ``br-abc.1``, so
    a future one-way import is a generator change and never a schema change.

    Uniqueness is enforced twice on purpose. ``uq_tickets_key`` is the exact-match constraint and
    provides the lookup index; ``uq_tickets_key_nocase`` closes a collision the first cannot see --
    ``fetch_topic`` matches case-INSENSITIVELY (app.py:13243), so ``AM-12`` and ``am-12`` would
    otherwise be two tickets with permanently indistinguishable discussions.

    ``closed_ts IS NOT NULL`` is the sole DATABASE-level meaning of "finished", which is why the hot
    worklist indexes are partial on it rather than on a list of status names. The status word is a
    label the service layer keeps in step; the physical invariant is that a resolution and a close
    time are both present or both absent.
    """

    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("key", name="uq_tickets_key"),
        UniqueConstraint("discussion_thread_id", name="uq_tickets_discussion_thread_id"),
        # Expression unique index; a UniqueConstraint cannot express case folding. Same technique as
        # uq_message_deliveries_idempotency (models.py:713-723).
        Index("uq_tickets_key_nocase", text("lower(key)"), unique=True),
        # THE hot query: what is open in this project, most urgent first. Partial so closed history
        # never enters the index, in the idiom of idx_agent_executions_active (models.py:399-405).
        Index(
            "idx_tickets_project_open",
            "project_id",
            "priority",
            "updated_ts",
            sqlite_where=text("closed_ts IS NULL"),
        ),
        Index(
            "idx_tickets_project_assignee_open",
            "project_id",
            "assignee_agent_id",
            "priority",
            sqlite_where=text("closed_ts IS NULL"),
        ),
        Index("idx_tickets_project_status", "project_id", "status_key", "updated_ts"),
        Index("idx_tickets_parent", "parent_id"),
        # SQLite scans the child table once per parent delete when the referencing
        # column is unindexed. `purge_old_messages` deletes messages in bulk, so
        # without this the SET NULL above costs a full `tickets` scan per purged row.
        Index("idx_tickets_origin_message", "origin_message_id"),
        # Makes a later one-way import idempotent: re-importing an upstream issue updates rather
        # than duplicating. Partial, so the overwhelming majority of rows never enter it.
        Index(
            "uq_tickets_project_external_ref",
            "project_id",
            "external_ref",
            unique=True,
            sqlite_where=text("external_ref IS NOT NULL"),
        ),
        # Shape, never vocabulary -- see the section header. The class is exactly app.py:10546's.
        CheckConstraint(
            "length(key) BETWEEN 3 AND 64 "
            "AND substr(key, 1, 1) GLOB '[A-Za-z0-9]' "
            "AND key NOT GLOB '*[^A-Za-z0-9._-]*'",
            name="ck_tickets_key",
        ),
        CheckConstraint(
            "length(kind_key) BETWEEN 1 AND 32 "
            "AND lower(kind_key) = kind_key "
            "AND kind_key NOT GLOB '*[^a-z0-9_]*'",
            name="ck_tickets_kind_key",
        ),
        CheckConstraint(
            "length(status_key) BETWEEN 1 AND 32 "
            "AND lower(status_key) = status_key "
            "AND status_key NOT GLOB '*[^a-z0-9_]*'",
            name="ck_tickets_status_key",
        ),
        CheckConstraint(
            "resolution_key IS NULL OR (length(resolution_key) BETWEEN 1 AND 32 "
            "AND lower(resolution_key) = resolution_key "
            "AND resolution_key NOT GLOB '*[^a-z0-9_]*')",
            name="ck_tickets_resolution_key",
        ),
        # Both halves are needed. SQLite does not enforce ``VARCHAR(n)``, so ``max_length``
        # creates no constraint at all; and ``trim()`` strips spaces only, so a 512-character
        # title with trailing spaces satisfies a trim-only bound while overflowing the
        # declared width. The same doubling is already written out at
        # ck_agent_executions_task_description.
        CheckConstraint(
            "length(trim(title)) >= 1 AND length(title) <= 512",
            name="ck_tickets_title",
        ),
        # A real ceiling on free text, in the idiom of ck_agent_executions_task_description
        # (models.py:357-360): far above any honest description, far below what a runaway writer
        # needs to bloat a single-writer database every other agent's mail waits behind.
        CheckConstraint("length(description_md) <= 65536", name="ck_tickets_description_md"),
        # Open-ended upward on purpose. 0 is most urgent, matching the convention already in
        # .beads/issues.jsonl (priorities 0-4), so an imported priority needs no remapping.
        CheckConstraint("priority >= 0", name="ck_tickets_priority"),
        CheckConstraint("parent_id IS NULL OR parent_id != id", name="ck_tickets_parent_not_self"),
        # The two halves of "finished" cannot drift apart. Vocabulary-free by construction, so
        # renaming or adding a terminal status never touches this table. Mirrors the cross-column
        # shape of ck_agent_executions_status_end (models.py:373-377).
        CheckConstraint(
            "(closed_ts IS NULL AND resolution_key IS NULL) "
            "OR (closed_ts IS NOT NULL AND resolution_key IS NOT NULL)",
            name="ck_tickets_closure",
        ),
        CheckConstraint(
            "external_ref IS NULL OR (length(trim(external_ref)) >= 1 "
            "AND length(external_ref) <= 256)",
            name="ck_tickets_external_ref",
        ),
        CheckConstraint("revision >= 1", name="ck_tickets_revision"),
        CheckConstraint(
            "length(discussion_thread_id) BETWEEN 8 AND 128 "
            "AND discussion_thread_id NOT GLOB '*[^A-Za-z0-9._-]*'",
            name="ck_tickets_discussion_thread_id",
        ),
        # Closing is an update, so a close time later than the last update time describes
        # a sequence that cannot have happened.
        CheckConstraint(
            "updated_ts >= created_ts "
            "AND (closed_ts IS NULL OR (closed_ts >= created_ts AND updated_ts >= closed_ts))",
            name="ck_tickets_timestamps",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    # Stored, never derived. SQLite reuses row ids, so a public identifier can never be a function
    # of the primary key.
    key: str = Field(max_length=64)
    # Vocabulary in tickets.TICKET_KINDS: epic | task | bug | chore.
    kind_key: str = Field(default="task", max_length=32)
    # Self-reference: the task's epic. NULL for an epic and for a loose task. The complementary rule
    # -- a parent must itself be an epic -- spans two rows and lives in tickets.py; SQLite CHECK
    # cannot see another row.
    parent_id: Optional[int] = Field(default=None, foreign_key="tickets.id")
    title: str = Field(max_length=512)
    description_md: str = Field(default="")
    # Vocabulary in tickets.TICKET_STATUSES: open | in_progress | closed.
    status_key: str = Field(default="open", max_length=32, index=True)
    # Vocabulary in tickets.TICKET_RESOLUTIONS: done | wontfix | duplicate | obsolete.
    resolution_key: Optional[str] = Field(default=None, max_length=32)
    priority: int = Field(
        default=3,
        sa_column=Column(Integer, nullable=False, server_default="3"),
    )
    # Nullable for the FileReservation.agent_id reason (models.py:862-866): a ticket must outlive
    # its assignee. An agent that is retired, swept or renamed must not take open work out of the
    # worklist; an assignee whose Agent row is gone reads as unassigned, which is the correct
    # *current* state.
    # ``ondelete`` is part of the table DDL and cannot be added later without a full
    # rebuild, so the promise in this class docstring -- a ticket outlives its assignee --
    # has to be made physical now or not at all. Without it a hard delete of an Agent row
    # would be REFUSED by the foreign key while ``PRAGMA foreign_keys=ON`` is set on every
    # pooled connection (db.py:449), which is the opposite of "reads as unassigned".
    assignee_agent_id: Optional[int] = Field(
        default=None, foreign_key="agents.id", ondelete="SET NULL", index=True
    )
    reporter_agent_id: Optional[int] = Field(
        default=None, foreign_key="agents.id", ondelete="SET NULL"
    )
    # Actor snapshot beside the FK, because not every writer is an Agent: ui_access.py:28 answers the
    # same question with the literal "cli". Without it a CLI-created ticket reads as "created by
    # nobody" forever. Same shape as message_delivery_recipients' name snapshots (models.py:826-828).
    reporter_label: str = Field(default="", max_length=128)
    # The message in which this work was decided; it normally predates the ticket, so the topic
    # convention cannot recover it. ondelete="SET NULL" is load-bearing rather than defensive:
    # purge_old_messages deletes Message rows (app.py:11760-11770) while PRAGMA foreign_keys=ON is
    # set on every pooled connection (db.py:449), and the same command already NULLs the
    # Message.reply_to self-FK for retained replies (app.py:11751-11758) -- i.e. SET NULL is this
    # codebase's own answer to exactly this coupling. The only existing ondelete precedent is
    # models.py:1049-1050.
    origin_message_id: Optional[int] = Field(
        default=None,
        foreign_key="messages.id",
        ondelete="SET NULL",
    )
    # Opaque and IMMUTABLE after creation: changing it would strand every message already
    # committed to the git archive under the old thread. The key stays the readable tag
    # (`Message.topic`); this is the conversation's identity.
    discussion_thread_id: str = Field(
        default_factory=_new_discussion_thread_id, max_length=128, index=True
    )
    external_ref: Optional[str] = Field(default=None, max_length=256)
    # Compare-and-swap token for concurrent editors, in the idiom of ui_users.profile_revision
    # (models.py:1010). Optional on the wire; every write returns the new value.
    revision: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default="1"),
    )
    created_ts: datetime = Field(default_factory=_utcnow_naive)
    updated_ts: datetime = Field(default_factory=_utcnow_naive)
    closed_ts: Optional[datetime] = Field(default=None)


class TicketLink(SQLModel, table=True):
    """One directed edge from a ticket into the graph this server already owns.

    This is the capability a general-purpose tracker structurally cannot have, because it has
    neither this mail archive nor these file reservations: a ticket can point at the exact delivered
    message where the decision was made and at the file reservation realising it.

    ``target_ref`` is TEXT and carries NO foreign key, deliberately and uniformly. Three targets of
    three different shapes cannot share one integer FK, and a polymorphic triple of nullable FK
    columns would need an "exactly one" CHECK that then forces ``ondelete="CASCADE"`` on the message
    column -- which deletes the whole edge (the relation, the source, the author, the timestamp)
    rather than degrading the pointer. TEXT also survives ``purge_old_messages`` (app.py:11760-11770)
    with no coupling at all. The precedent is ``UiAccessAuditEvent`` (models.py:1056-1127), which
    carries no foreign keys for the same survive-the-referent reason.

    For ``target_kind = 'ticket'`` the ref is the target ticket's ``key``, not its row id: keys are
    globally unique and never rewritten, and a key-addressed edge may legitimately cross projects --
    the case a fleet coordinating four repositories actually hits.

    Existence is verified at write time by ``tickets.py``; a later dangling ref renders as "no longer
    available" rather than as an error.

    ``relation`` and ``target_kind`` are shape-checked strings with no membership CHECK: they are
    server-defined and extensible, and extending them must not be a rewrite of this table.
    """

    __tablename__ = "ticket_links"
    __table_args__ = (
        UniqueConstraint(
            "ticket_id", "relation", "target_kind", "target_ref", name="uq_ticket_links_edge"
        ),
        # The reverse read: "what blocks this ticket".
        Index("idx_ticket_links_target", "target_kind", "target_ref"),
        Index("idx_ticket_links_ticket", "ticket_id", "relation"),
        CheckConstraint(
            "length(relation) BETWEEN 1 AND 32 "
            "AND lower(relation) = relation "
            "AND relation NOT GLOB '*[^a-z_]*'",
            name="ck_ticket_links_relation",
        ),
        CheckConstraint(
            "length(target_kind) BETWEEN 1 AND 32 "
            "AND lower(target_kind) = target_kind "
            "AND target_kind NOT GLOB '*[^a-z_]*'",
            name="ck_ticket_links_target_kind",
        ),
        CheckConstraint(
            "length(trim(target_ref)) >= 1 AND length(target_ref) <= 128",
            name="ck_ticket_links_target_ref",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="tickets.id", index=True)
    # Vocabulary in tickets.TICKET_RELATIONS: blocks | relates | duplicates | decided_by | touches.
    # Inverses are derived at read time.
    relation: str = Field(max_length=32)
    # Vocabulary in tickets.TICKET_TARGET_KINDS: ticket | message | file_reservation.
    # Reserved without any schema change: commit | execution | build_slot | url.
    target_kind: str = Field(max_length=32)
    target_ref: str = Field(max_length=128)
    created_by_agent_id: Optional[int] = Field(
        default=None, foreign_key="agents.id", ondelete="SET NULL"
    )
    created_by_label: str = Field(default="", max_length=128)
    created_ts: datetime = Field(default_factory=_utcnow_naive)


class TicketEvent(SQLModel, table=True):
    """Append-only record of one ticket mutation.

    This table ships in v1 for one reason: history is the only part of the design that CANNOT be
    added retroactively. Every other deferral here -- claims, full-text search, the web UI, a beads
    importer -- is a new table or an additive index later at identical cost. A change log that was
    not written on the day of the change has no source to backfill from.

    It is also what makes the P2 decision safe. Ticket *discussion* is mail and is therefore subject
    to ``purge_old_messages``; a ``commented`` event carrying the message id is a durable
    in-database record that the comment existed, independent of whatever retention later removes.

    No foreign keys and snapshot columns throughout, matching ``UiAccessAuditEvent``
    (models.py:1056-1127) exactly: an audit row must outlive its subject and must read correctly
    without a join.

    Append-only is enforced by the database, not only by the absence of a service-layer update
    path: ``_setup_fts`` installs ``ticket_events_immutable_bu`` and ``..._bd``, in the idiom it
    already uses for ``ui_access_audit_events``. They live there rather than in the ticketing
    Alembic revision for the reason that forbids a data seed in a revision body -- a fresh
    database is stamped at *head* and never replays a revision, so a trigger created only there
    would exist on production and on no developer machine.
    """

    __tablename__ = "ticket_events"
    __table_args__ = (
        Index("idx_ticket_events_ticket_created", "ticket_id", "created_ts"),
        Index("idx_ticket_events_project_created", "project_id", "created_ts"),
        CheckConstraint(
            "length(event_type) BETWEEN 1 AND 32 "
            "AND lower(event_type) = event_type "
            "AND event_type NOT GLOB '*[^a-z_]*'",
            name="ck_ticket_events_event_type",
        ),
        CheckConstraint(
            "field_name IS NULL OR (length(field_name) BETWEEN 1 AND 64 "
            "AND lower(field_name) = field_name "
            "AND field_name NOT GLOB '*[^a-z0-9_]*')",
            name="ck_ticket_events_field_name",
        ),
        CheckConstraint(
            "(old_value IS NULL OR length(old_value) <= 1024) "
            "AND (new_value IS NULL OR length(new_value) <= 1024)",
            name="ck_ticket_events_values",
        ),
        CheckConstraint("revision_after >= 1", name="ck_ticket_events_revision_after"),
        CheckConstraint(
            "length(actor_kind) BETWEEN 1 AND 16 "
            "AND lower(actor_kind) = actor_kind "
            "AND actor_kind NOT GLOB '*[^a-z_]*'",
            name="ck_ticket_events_actor_kind",
        ),
        # A generation snapshot is meaningful only beside the id it disambiguates, and an
        # agent-authored event must carry one.
        #
        # ``IS NOT NULL`` is written out rather than left implied by ``length(...) = 64``,
        # and that is not style. A CHECK is violated only when it evaluates to FALSE:
        # NULL passes. With a non-null id and a null generation, ``length(NULL) = 64``
        # is NULL, so the whole disjunction is NULL and the row is ACCEPTED -- measured
        # here, on the first draft of this constraint. The nearby
        # ck_ui_access_audit_actor_provenance (models.py:1093-1100) is written in that
        # weaker shape and has the same hole; widening it would mean rebuilding a live
        # audit table, so it is reported rather than changed from here.
        CheckConstraint(
            "(actor_agent_id IS NULL AND actor_generation_snapshot IS NULL) "
            "OR (actor_agent_id IS NOT NULL "
            "AND actor_generation_snapshot IS NOT NULL "
            "AND length(actor_generation_snapshot) = 64 "
            "AND actor_generation_snapshot NOT GLOB '*[^0-9a-f]*')",
            name="ck_ticket_events_actor_provenance",
        ),
        # Binds the KIND to the pair above. Without it the table accepts two incoherent
        # audit rows: ``actor_kind='agent'`` carrying no agent at all, and
        # ``actor_kind='cli'`` carrying somebody else's agent id.
        #
        # This is the one place the ticketing schema names a vocabulary member, and the
        # exception is deliberate. The section rule forbids closed vocabularies because
        # widening one costs a table rebuild -- but this does not close a set: a new kind
        # (``scheduler``, ``webhook``) still inserts freely, it merely has to carry no
        # agent id. The only thing frozen is that the kind spelled ``agent`` is the one
        # with an agent id, and that word is not going to be renamed while the table it
        # refers to is called ``agents``. An audit row whose provenance contradicts itself
        # is worth more than that flexibility.
        CheckConstraint(
            "(actor_kind = 'agent') = (actor_agent_id IS NOT NULL)",
            name="ck_ticket_events_actor_kind_binds_agent",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(index=True)
    ticket_key_snapshot: str = Field(max_length=64)
    project_id: int = Field(index=True)
    project_slug_snapshot: str = Field(max_length=255)
    # Vocabulary in tickets.TICKET_EVENT_TYPES:
    # created | field_changed | commented | linked | unlinked | closed | reopened.
    event_type: str = Field(max_length=32)
    field_name: Optional[str] = Field(default=None, max_length=64)
    old_value: Optional[str] = Field(default=None, max_length=1024)
    new_value: Optional[str] = Field(default=None, max_length=1024)
    # Not every writer is an Agent, and "which kind of actor" is not recoverable from a
    # nullable id: a NULL actor_agent_id would conflate the CLI, a human in the web UI, a
    # server-side sweep and a deleted agent into one indistinguishable state. Vocabulary in
    # tickets.TICKET_ACTOR_KINDS: agent | human | cli | system.
    # No default at all, on the lead's decision. An ``agent`` default made the
    # default-constructed event violate the binding CHECK below, and a ``system`` default
    # would merely hide the same question: every writer knows what it is, so it says so.
    actor_kind: str = Field(max_length=16)
    actor_agent_id: Optional[int] = Field(default=None)
    # Distinguishes a recreated agent from its previous lifetime even though SQLite reuses
    # numeric primary keys -- the same reason UiAccessAuditEvent snapshots
    # ``target_account_generation`` (models.py:1084-1087) rather than trusting an id.
    actor_generation_snapshot: Optional[str] = Field(default=None, max_length=64)
    actor_label: str = Field(default="", max_length=128)
    revision_after: int = Field(default=1)
    created_ts: datetime = Field(default_factory=_utcnow_naive)
