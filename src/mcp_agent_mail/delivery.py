"""Durable orchestration for atomic database-and-Git message delivery.

The database owns an immutable delivery intent. A worker claims that intent
with a monotonically fenced lease, publishes one verified immutable Git
document, and only then materializes the human/agent-visible ``Message`` and
``MessageRecipient`` rows in one serialized transaction.

No operation in this module deletes, unlinks, restores, or cleans up files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db import (
    await_database_cleanup_task,
    ensure_schema,
    get_immediate_session,
)
from .models import (
    Agent,
    AgentLink,
    Message,
    MessageDelivery,
    MessageDeliveryRecipient,
    MessageRecipient,
    Project,
    UiProjectAssignment,
    UiUser,
)
from .notify import hub
from .storage import (
    MessageDeliveryPendingError,
    MessageDeliveryPublication,
    MessageDeliveryQuarantinedError,
    MessageDeliveryWorkspaceConflictError,
    emit_notification_signal,
    ensure_archive,
    publish_message_delivery,
)
from .utils import validate_thread_id_format

DeliveryActorKind = Literal["agent", "ui_user", "system"]
DeliveryPurpose = Literal["message", "reply", "contact_request"]
DeliveryRecipientKind = Literal["to", "cc", "bcc"]
DeliveryProcessingStatus = Literal[
    "published",
    "pending",
    "quarantined",
    "busy",
    "deferred",
]

_TOPIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ERROR_LENGTH = 4096


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class DeliveryProjectSnapshot:
    """One exact project-row lifetime supplied by an authenticated caller."""

    project_id: int
    slug: str
    generation: str


@dataclass(frozen=True, slots=True)
class DeliveryAgentSnapshot:
    """One exact agent-row lifetime, including its source project lifetime."""

    agent_id: int
    name: str
    generation: str
    project: DeliveryProjectSnapshot


@dataclass(frozen=True, slots=True)
class DeliveryActorSnapshot:
    """Authenticated principal that authorizes creation of a delivery intent."""

    kind: DeliveryActorKind
    actor_id: int
    name: str
    generation: str | None
    epoch: int | None = None
    source_project: DeliveryProjectSnapshot | None = None

    @classmethod
    def system(cls) -> DeliveryActorSnapshot:
        return cls(
            kind="system",
            actor_id=0,
            name="system",
            generation=None,
        )

    @classmethod
    def agent(cls, identity: DeliveryAgentSnapshot) -> DeliveryActorSnapshot:
        return cls(
            kind="agent",
            actor_id=identity.agent_id,
            name=identity.name,
            generation=identity.generation,
            source_project=identity.project,
        )

    @classmethod
    def ui_user(
        cls,
        *,
        user_id: int,
        username: str,
        generation: str,
        epoch: int,
        source_project: DeliveryProjectSnapshot,
    ) -> DeliveryActorSnapshot:
        return cls(
            kind="ui_user",
            actor_id=user_id,
            name=username,
            generation=generation,
            epoch=epoch,
            source_project=source_project,
        )


@dataclass(frozen=True, slots=True)
class DeliveryRecipientSnapshot:
    """Ordered destination identity and its recipient category."""

    kind: DeliveryRecipientKind
    agent: DeliveryAgentSnapshot


@dataclass(frozen=True, slots=True)
class MessageDeliveryRequest:
    """Canonical input for accepting one durable message-delivery intent."""

    target_project: DeliveryProjectSnapshot
    sender: DeliveryAgentSnapshot
    actor: DeliveryActorSnapshot
    recipients: tuple[DeliveryRecipientSnapshot, ...]
    idempotency_key: str
    subject: str
    body_md: str
    purpose: DeliveryPurpose = "message"
    thread_id: str | None = None
    reply_to_message_id: int | None = None
    topic: str | None = None
    importance: str = "normal"
    ack_required: bool = False
    attachments: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class MessageDeliveryAcceptance:
    delivery_id: str
    state: str
    request_sha256: str
    document_sha256: str
    reused: bool


@dataclass(frozen=True, slots=True)
class MessageDeliveryLease:
    delivery_id: str
    token: str
    fence: int
    expires_ts: datetime
    attempt_count: int


@dataclass(frozen=True, slots=True)
class MessageDeliveryProcessingResult:
    delivery_id: str
    status: DeliveryProcessingStatus
    message_id: int | None = None
    commit_sha: str | None = None
    next_attempt_ts: datetime | None = None
    error: str | None = None
    published_now: bool = False


class MessageDeliveryServiceError(RuntimeError):
    """Base class for deterministic delivery-orchestration failures."""


class MessageDeliveryValidationError(MessageDeliveryServiceError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MessageDeliveryIdempotencyConflictError(MessageDeliveryServiceError):
    def __init__(self, delivery_id: str) -> None:
        super().__init__(
            f"Idempotency key already belongs to delivery {delivery_id} with a different payload"
        )
        self.delivery_id = delivery_id


class MessageDeliveryNotFoundError(MessageDeliveryServiceError):
    def __init__(self, delivery_id: str) -> None:
        super().__init__(f"Message delivery {delivery_id} was not found")
        self.delivery_id = delivery_id


class MessageDeliveryLeaseLostError(MessageDeliveryServiceError):
    def __init__(self, delivery_id: str) -> None:
        super().__init__(f"Message delivery {delivery_id} lease is stale or no longer owned")
        self.delivery_id = delivery_id


class MessageDeliveryTerminalError(MessageDeliveryServiceError):
    def __init__(self, delivery_id: str, reason: str) -> None:
        super().__init__(f"Message delivery {delivery_id} is quarantined: {reason}")
        self.delivery_id = delivery_id
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _NormalizedDeliveryRequest:
    request: MessageDeliveryRequest
    attachments: list[dict[str, Any]]
    request_payload: dict[str, Any]
    request_sha256: str


def _canonical_json(payload: object) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MessageDeliveryValidationError(
            "invalid_json_payload",
            "Attachments and delivery metadata must be finite JSON values",
        ) from exc


def _canonical_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _validate_generation(value: str, field_name: str) -> None:
    if _SHA256_RE.fullmatch(value) is None:
        raise MessageDeliveryValidationError(
            "invalid_generation",
            f"{field_name} must be a lowercase 64-character generation token",
        )


def _project_payload(project: DeliveryProjectSnapshot) -> dict[str, object]:
    return {
        "id": project.project_id,
        "slug": project.slug,
        "generation": project.generation,
    }


def _agent_payload(agent: DeliveryAgentSnapshot) -> dict[str, object]:
    return {
        "id": agent.agent_id,
        "name": agent.name,
        "generation": agent.generation,
        "project": _project_payload(agent.project),
    }


def _actor_payload(actor: DeliveryActorSnapshot) -> dict[str, object | None]:
    return {
        "kind": actor.kind,
        "id": actor.actor_id,
        "name": actor.name,
        "generation": actor.generation,
        "epoch": actor.epoch,
        "source_project": (
            _project_payload(actor.source_project)
            if actor.source_project is not None
            else None
        ),
    }


def _recipient_payload(
    recipient: DeliveryRecipientSnapshot,
    ordinal: int,
) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "kind": recipient.kind,
        "agent": _agent_payload(recipient.agent),
    }


def _normalize_delivery_request(request: MessageDeliveryRequest) -> _NormalizedDeliveryRequest:
    if request.purpose not in {"message", "reply", "contact_request"}:
        raise MessageDeliveryValidationError(
            "invalid_delivery_purpose",
            "Delivery purpose must be message, reply, or contact_request",
        )
    if request.target_project.project_id < 1 or not request.target_project.slug:
        raise MessageDeliveryValidationError("invalid_project", "Target project snapshot is invalid")
    _validate_generation(request.target_project.generation, "target project generation")
    if request.sender.agent_id < 1 or not request.sender.name:
        raise MessageDeliveryValidationError("invalid_sender", "Sender snapshot is invalid")
    _validate_generation(request.sender.generation, "sender generation")
    _validate_generation(request.sender.project.generation, "sender project generation")
    if request.sender.project.project_id < 1 or not request.sender.project.slug:
        raise MessageDeliveryValidationError(
            "invalid_sender_project",
            "Sender source-project snapshot is invalid",
        )

    actor = request.actor
    if actor.kind == "system":
        if (
            actor.actor_id != 0
            or actor.name != "system"
            or actor.generation is not None
            or actor.epoch is not None
            or actor.source_project is not None
        ):
            raise MessageDeliveryValidationError(
                "invalid_system_actor",
                "System actor must use id 0 and contain no identity lifetime",
            )
    elif actor.kind == "agent":
        if actor.actor_id < 1 or not actor.name or actor.generation is None:
            raise MessageDeliveryValidationError("invalid_agent_actor", "Agent actor is invalid")
        _validate_generation(actor.generation, "agent actor generation")
        if actor.epoch is not None or actor.source_project is None:
            raise MessageDeliveryValidationError(
                "invalid_agent_actor",
                "Agent actor requires a source project and no session epoch",
            )
        _validate_generation(actor.source_project.generation, "actor project generation")
        if (
            actor.actor_id != request.sender.agent_id
            or actor.name != request.sender.name
            or actor.generation != request.sender.generation
            or actor.source_project != request.sender.project
        ):
            raise MessageDeliveryValidationError(
                "agent_actor_sender_mismatch",
                "An agent principal may only authorize its own sender identity",
            )
    elif actor.kind == "ui_user":
        if (
            actor.actor_id < 1
            or not actor.name
            or actor.generation is None
            or actor.epoch is None
            or actor.epoch < 1
            or actor.source_project is None
            or actor.source_project != request.sender.project
        ):
            raise MessageDeliveryValidationError(
                "invalid_ui_actor",
                "UI actor requires an account lifetime, positive session epoch, "
                "and the sender mailbox project lifetime",
            )
        _validate_generation(actor.generation, "UI actor generation")
        _validate_generation(actor.source_project.generation, "UI actor project generation")
    else:
        raise MessageDeliveryValidationError("invalid_actor_kind", "Unsupported actor kind")

    idempotency_key = request.idempotency_key.strip()
    if not idempotency_key or len(idempotency_key) > 128:
        raise MessageDeliveryValidationError(
            "invalid_idempotency_key",
            "Idempotency key must contain 1-128 non-whitespace characters",
        )
    subject = _canonical_text(request.subject)
    body_md = _canonical_text(request.body_md)
    if len(subject) > 512:
        raise MessageDeliveryValidationError("subject_too_long", "Subject exceeds 512 characters")
    if len(request.importance) > 16 or not request.importance:
        raise MessageDeliveryValidationError("invalid_importance", "Importance is invalid")
    thread_id = request.thread_id.strip() if request.thread_id is not None else None
    if thread_id is not None and not validate_thread_id_format(thread_id):
        raise MessageDeliveryValidationError("invalid_thread_id", "Thread id is invalid")
    if request.purpose == "reply" and (
        request.actor.kind not in {"agent", "ui_user"} or thread_id is None
    ):
        raise MessageDeliveryValidationError(
            "invalid_reply_shape",
            "Replies require an authenticated sender agent or mailbox operator "
            "and a nonempty thread id",
        )
    topic = request.topic.strip() if request.topic is not None else None
    if topic is not None and (len(topic) > 64 or _TOPIC_RE.fullmatch(topic) is None):
        raise MessageDeliveryValidationError("invalid_topic", "Topic is invalid")
    if request.reply_to_message_id is not None and request.reply_to_message_id < 1:
        raise MessageDeliveryValidationError("invalid_reply_target", "Reply target is invalid")
    if not request.recipients:
        raise MessageDeliveryValidationError("missing_recipients", "At least one recipient is required")
    if request.purpose == "contact_request" and (
        request.actor.kind != "agent"
        or len(request.recipients) != 1
        or request.recipients[0].kind != "to"
    ):
        raise MessageDeliveryValidationError(
            "invalid_contact_request_shape",
            "Contact requests require the sender agent actor and exactly one to recipient",
        )

    recipient_ids: set[int] = set()
    normalized_recipients: list[DeliveryRecipientSnapshot] = []
    for recipient in request.recipients:
        if recipient.kind not in {"to", "cc", "bcc"}:
            raise MessageDeliveryValidationError("invalid_recipient_kind", "Recipient kind is invalid")
        identity = recipient.agent
        if identity.agent_id < 1 or not identity.name:
            raise MessageDeliveryValidationError("invalid_recipient", "Recipient snapshot is invalid")
        if identity.agent_id in recipient_ids:
            raise MessageDeliveryValidationError(
                "duplicate_recipient",
                "Each agent may occur only once across to, cc, and bcc",
            )
        recipient_ids.add(identity.agent_id)
        _validate_generation(identity.generation, "recipient generation")
        _validate_generation(identity.project.generation, "recipient project generation")
        if identity.project != request.target_project:
            raise MessageDeliveryValidationError(
                "recipient_target_mismatch",
                "Every recipient must belong to the target project lifetime",
            )
        normalized_recipients.append(recipient)

    if request.attachments:
        raise MessageDeliveryValidationError(
            "attachments_require_inline_normalization",
            "Delivery accepts no attachment metadata until it has been normalized "
            "into bounded canonical inline bytes",
        )
    attachments: list[dict[str, Any]] = []

    normalized_request = MessageDeliveryRequest(
        target_project=request.target_project,
        sender=request.sender,
        actor=request.actor,
        recipients=tuple(normalized_recipients),
        idempotency_key=idempotency_key,
        subject=subject,
        body_md=body_md,
        purpose=request.purpose,
        thread_id=thread_id,
        reply_to_message_id=request.reply_to_message_id,
        topic=topic,
        importance=request.importance,
        ack_required=bool(request.ack_required),
        attachments=tuple(attachments),
    )
    request_payload: dict[str, Any] = {
        "schema_version": 1,
        "purpose": normalized_request.purpose,
        "target_project": _project_payload(normalized_request.target_project),
        "sender": _agent_payload(normalized_request.sender),
        "actor": _actor_payload(normalized_request.actor),
        "recipients": [
            _recipient_payload(recipient, ordinal)
            for ordinal, recipient in enumerate(normalized_request.recipients)
        ],
        "message": {
            "thread_id": normalized_request.thread_id,
            "reply_to_message_id": normalized_request.reply_to_message_id,
            "topic": normalized_request.topic,
            "subject": normalized_request.subject,
            "body_md": normalized_request.body_md,
            "importance": normalized_request.importance,
            "ack_required": normalized_request.ack_required,
            "attachments": attachments,
        },
    }
    request_sha256 = hashlib.sha256(_canonical_json(request_payload).encode()).hexdigest()
    return _NormalizedDeliveryRequest(
        request=normalized_request,
        attachments=attachments,
        request_payload=request_payload,
        request_sha256=request_sha256,
    )


def _build_archive_document(
    normalized: _NormalizedDeliveryRequest,
    delivery_id: str,
    created_ts: datetime,
) -> tuple[str, str]:
    visible_recipients: list[dict[str, object]] = []
    blind_recipients: list[dict[str, object]] = []
    for ordinal, recipient in enumerate(normalized.request.recipients):
        payload = _recipient_payload(recipient, ordinal)
        if recipient.kind == "bcc":
            blind_recipients.append(payload)
        else:
            visible_recipients.append(payload)
    blind_commitment = hashlib.sha256(
        _canonical_json(blind_recipients).encode()
    ).hexdigest()
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "purpose": normalized.request.purpose,
        "delivery_id": delivery_id,
        "created_ts": created_ts.isoformat(timespec="microseconds") + "Z",
        "request_sha256": normalized.request_sha256,
        "target_project": _project_payload(normalized.request.target_project),
        "sender": _agent_payload(normalized.request.sender),
        "actor": _actor_payload(normalized.request.actor),
        "recipients": visible_recipients,
        "bcc": {
            "count": len(blind_recipients),
            "ordered_snapshot_sha256": blind_commitment,
        },
        "message": {
            "thread_id": normalized.request.thread_id,
            "reply_to_message_id": normalized.request.reply_to_message_id,
            "topic": normalized.request.topic,
            "subject": normalized.request.subject,
            "importance": normalized.request.importance,
            "ack_required": normalized.request.ack_required,
            "attachments": normalized.attachments,
        },
    }
    body = normalized.request.body_md.rstrip("\n")
    archive_document = f"---json\n{_canonical_json(metadata)}\n---\n\n{body}\n"
    return archive_document, hashlib.sha256(archive_document.encode()).hexdigest()


def _extract_archive_metadata(delivery: MessageDelivery) -> dict[str, Any]:
    document = delivery.archive_document
    document_sha256 = hashlib.sha256(document.encode()).hexdigest()
    if document_sha256 != delivery.document_sha256:
        raise MessageDeliveryValidationError(
            "archive_document_hash_mismatch",
            "Immutable delivery document no longer matches its SHA-256",
        )
    prefix = "---json\n"
    separator = "\n---\n\n"
    if not document.startswith(prefix) or separator not in document:
        raise MessageDeliveryValidationError(
            "invalid_archive_document",
            "Immutable delivery document has invalid canonical framing",
        )
    encoded_metadata = document[len(prefix) :].split(separator, 1)[0]
    try:
        metadata = json.loads(encoded_metadata)
    except json.JSONDecodeError as exc:
        raise MessageDeliveryValidationError(
            "invalid_archive_document",
            "Immutable delivery document metadata is invalid JSON",
        ) from exc
    if not isinstance(metadata, dict):
        raise MessageDeliveryValidationError(
            "invalid_archive_document",
            "Immutable delivery document metadata must be an object",
        )
    if metadata.get("delivery_id") != delivery.id:
        raise MessageDeliveryValidationError(
            "archive_delivery_id_mismatch",
            "Immutable delivery document identifies another delivery",
        )
    if metadata.get("purpose") != delivery.delivery_kind:
        raise MessageDeliveryValidationError(
            "archive_delivery_purpose_mismatch",
            "Immutable delivery document purpose does not match its intent",
        )
    return cast(dict[str, Any], metadata)


async def _commit_preserving_cancellation(session: AsyncSession) -> None:
    await await_database_cleanup_task(asyncio.create_task(session.commit()))


async def _load_project_snapshot(
    session: AsyncSession,
    snapshot: DeliveryProjectSnapshot,
    *,
    role: str,
) -> Project:
    project = await session.get(Project, snapshot.project_id)
    if (
        project is None
        or project.slug != snapshot.slug
        or project.project_generation != snapshot.generation
        or project.archived_at is not None
    ):
        role_code = role.casefold().replace(" ", "_")
        raise MessageDeliveryValidationError(
            f"{role_code}_project_lifetime_invalid",
            f"{role.capitalize()} project lifetime is missing, archived, or recreated",
        )
    return project


async def _load_agent_snapshot(
    session: AsyncSession,
    snapshot: DeliveryAgentSnapshot,
    *,
    role: str,
) -> Agent:
    await _load_project_snapshot(session, snapshot.project, role=f"{role} source")
    agent = await session.get(Agent, snapshot.agent_id)
    if (
        agent is None
        or agent.project_id != snapshot.project.project_id
        or agent.name != snapshot.name
        or agent.agent_generation != snapshot.generation
        or agent.provisioning_state != "active"
        or agent.retired_at is not None
    ):
        raise MessageDeliveryValidationError(
            f"{role}_lifetime_invalid",
            f"{role.capitalize()} identity is missing, retired, or recreated",
        )
    return agent


async def _validate_ui_actor(
    session: AsyncSession,
    actor: DeliveryActorSnapshot,
 ) -> UiUser:
    source_project = cast(DeliveryProjectSnapshot, actor.source_project)
    await _load_project_snapshot(session, source_project, role="UI actor source")
    user = await session.get(UiUser, actor.actor_id)
    if (
        user is None
        or user.username != actor.name
        or user.session_generation != actor.generation
        or user.session_epoch != actor.epoch
        or user.disabled
    ):
        raise MessageDeliveryValidationError(
            "ui_actor_lifetime_invalid",
            "UI actor is disabled, revoked, or recreated",
        )
    if user.role == "admin":
        return user
    if user.role != "member":
        raise MessageDeliveryValidationError(
            "ui_actor_role_invalid",
            "UI actor does not have an operator-capable global role",
        )
    assignment_result = await session.execute(
        select(UiProjectAssignment).where(
            cast(Any, UiProjectAssignment.user_id == user.id),
            cast(Any, UiProjectAssignment.project_id == source_project.project_id),
        )
    )
    assignment = assignment_result.scalar_one_or_none()
    if assignment is None or assignment.role != "operator":
        raise MessageDeliveryValidationError(
            "ui_actor_operator_required",
            "UI actor requires an operator assignment for the sender mailbox project",
        )
    return user


async def _has_approved_cross_project_route(
    session: AsyncSession,
    sender: DeliveryAgentSnapshot,
    recipient: DeliveryAgentSnapshot,
    now: datetime,
) -> bool:
    if sender.project.project_id == recipient.project.project_id:
        return True
    link_result = await session.execute(
        select(AgentLink).where(
            cast(Any, AgentLink.a_project_id == sender.project.project_id),
            cast(Any, AgentLink.a_agent_id == sender.agent_id),
            cast(Any, AgentLink.b_project_id == recipient.project.project_id),
            cast(Any, AgentLink.b_agent_id == recipient.agent_id),
            cast(Any, AgentLink.status == "approved"),
            or_(
                cast(Any, AgentLink.expires_ts).is_(None),
                cast(Any, AgentLink.expires_ts) > now,
            ),
        )
    )
    return link_result.scalar_one_or_none() is not None


async def _validate_cross_project_route(
    session: AsyncSession,
    sender: DeliveryAgentSnapshot,
    recipient: DeliveryAgentSnapshot,
    now: datetime,
) -> None:
    if await _has_approved_cross_project_route(session, sender, recipient, now):
        return
    raise MessageDeliveryValidationError(
        "cross_project_route_revoked",
        "Cross-project sender-to-recipient approval is missing or expired",
    )


async def _validate_reply_route(
    session: AsyncSession,
    request: MessageDeliveryRequest,
    recipient: DeliveryAgentSnapshot,
    now: datetime,
) -> None:
    if await _has_approved_cross_project_route(session, request.sender, recipient, now):
        return
    thread_id = cast(str, request.thread_id)
    thread_conditions: list[Any] = [cast(Any, Message.thread_id == thread_id)]
    if thread_id.isascii() and thread_id.isdecimal():
        numeric_seed = int(thread_id)
        if numeric_seed >= 1 and str(numeric_seed) == thread_id:
            thread_conditions.append(
                cast(Any, Message.thread_id).is_(None)
                & cast(Any, Message.id == numeric_seed)
            )
    route_query = select(Message).where(
        cast(Any, Message.project_id == request.sender.project.project_id),
        cast(Any, Message.sender_id == recipient.agent_id),
        or_(*thread_conditions),
    )
    if request.actor.kind == "agent":
        route_query = route_query.join(
            MessageRecipient,
            cast(Any, MessageRecipient.message_id == Message.id),
        ).where(
            cast(Any, MessageRecipient.agent_id == request.sender.agent_id),
        )
    route_result = await session.execute(route_query.limit(1))
    if route_result.scalar_one_or_none() is None:
        raise MessageDeliveryValidationError(
            "reply_route_invalid",
            "Reply lacks an approved route or an exact inbound thread route",
        )


async def _validate_pending_contact_request(
    session: AsyncSession,
    sender: DeliveryAgentSnapshot,
    recipient: DeliveryAgentSnapshot,
    now: datetime,
) -> None:
    link_result = await session.execute(
        select(AgentLink).where(
            cast(Any, AgentLink.a_project_id == sender.project.project_id),
            cast(Any, AgentLink.a_agent_id == sender.agent_id),
            cast(Any, AgentLink.b_project_id == recipient.project.project_id),
            cast(Any, AgentLink.b_agent_id == recipient.agent_id),
            cast(Any, AgentLink.status == "pending"),
            or_(
                cast(Any, AgentLink.expires_ts).is_(None),
                cast(Any, AgentLink.expires_ts) > now,
            ),
        )
    )
    if link_result.scalar_one_or_none() is None:
        raise MessageDeliveryValidationError(
            "contact_request_link_invalid",
            "Contact request requires its exact active pending agent link",
        )


async def _validate_request_lifetimes(
    session: AsyncSession,
    request: MessageDeliveryRequest,
    now: datetime,
) -> None:
    await _load_project_snapshot(
        session,
        request.target_project,
        role="target",
    )
    await _load_agent_snapshot(session, request.sender, role="sender")
    if request.actor.kind == "agent":
        actor_identity = DeliveryAgentSnapshot(
            agent_id=request.actor.actor_id,
            name=request.actor.name,
            generation=cast(str, request.actor.generation),
            project=cast(DeliveryProjectSnapshot, request.actor.source_project),
        )
        await _load_agent_snapshot(session, actor_identity, role="actor")
    elif request.actor.kind == "ui_user":
        ui_user = await _validate_ui_actor(session, request.actor)
        if request.purpose == "message" and ui_user.role != "admin":
            raise MessageDeliveryValidationError(
                "ui_actor_admin_required",
                "Only a global administrator may initiate a non-reply message",
            )

    for recipient in request.recipients:
        recipient_agent = await _load_agent_snapshot(
            session,
            recipient.agent,
            role="recipient",
        )
        if recipient_agent.contact_policy == "block_all":
            raise MessageDeliveryValidationError(
                "recipient_blocked",
                f"Recipient {recipient.agent.name} is not accepting messages",
            )
        if request.purpose == "contact_request":
            await _validate_pending_contact_request(
                session,
                request.sender,
                recipient.agent,
                now,
            )
        elif request.purpose == "reply":
            await _validate_reply_route(
                session,
                request,
                recipient.agent,
                now,
            )
        else:
            await _validate_cross_project_route(
                session,
                request.sender,
                recipient.agent,
                now,
            )

    if request.reply_to_message_id is not None:
        reply_target = await session.get(Message, request.reply_to_message_id)
        if reply_target is None or reply_target.project_id != request.target_project.project_id:
            raise MessageDeliveryValidationError(
                "reply_target_invalid",
                "Reply target is missing or belongs to another target project",
            )


def _snapshot_from_project_payload(payload: object, field_name: str) -> DeliveryProjectSnapshot:
    if not isinstance(payload, dict):
        raise MessageDeliveryValidationError(
            "invalid_archive_document",
            f"Archive {field_name} project snapshot is invalid",
        )
    try:
        return DeliveryProjectSnapshot(
            project_id=int(payload["id"]),
            slug=str(payload["slug"]),
            generation=str(payload["generation"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MessageDeliveryValidationError(
            "invalid_archive_document",
            f"Archive {field_name} project snapshot is incomplete",
        ) from exc


def _request_from_delivery(
    delivery: MessageDelivery,
    recipients: Sequence[MessageDeliveryRecipient],
) -> MessageDeliveryRequest:
    _extract_archive_metadata(delivery)
    target_project = DeliveryProjectSnapshot(
        project_id=delivery.project_id,
        slug=delivery.project_slug_snapshot,
        generation=delivery.project_generation_snapshot,
    )
    sender_project = DeliveryProjectSnapshot(
        project_id=delivery.sender_project_id_snapshot,
        slug=delivery.sender_project_slug_snapshot,
        generation=delivery.sender_project_generation_snapshot,
    )
    actor_project_values = (
        delivery.actor_project_id_snapshot,
        delivery.actor_project_slug_snapshot,
        delivery.actor_project_generation_snapshot,
    )
    if all(value is None for value in actor_project_values):
        actor_source = None
    elif all(value is not None for value in actor_project_values):
        actor_source = DeliveryProjectSnapshot(
            project_id=cast(int, delivery.actor_project_id_snapshot),
            slug=cast(str, delivery.actor_project_slug_snapshot),
            generation=cast(str, delivery.actor_project_generation_snapshot),
        )
    else:
        raise MessageDeliveryValidationError(
            "actor_project_snapshot_incomplete",
            "Delivery actor project snapshot is only partially populated",
        )
    sender = DeliveryAgentSnapshot(
        agent_id=delivery.sender_id,
        name=delivery.sender_name_snapshot,
        generation=delivery.sender_generation_snapshot,
        project=sender_project,
    )
    actor = DeliveryActorSnapshot(
        kind=cast(DeliveryActorKind, delivery.actor_kind),
        actor_id=delivery.actor_id,
        name=delivery.actor_name_snapshot,
        generation=delivery.actor_generation_snapshot,
        epoch=delivery.actor_epoch_snapshot,
        source_project=actor_source,
    )
    recipient_snapshots: list[DeliveryRecipientSnapshot] = []
    for recipient in recipients:
        if recipient.project_id_snapshot != target_project.project_id:
            raise MessageDeliveryValidationError(
                "recipient_project_snapshot_mismatch",
                "Delivery recipient project snapshot differs from its target project",
            )
        recipient_snapshots.append(
            DeliveryRecipientSnapshot(
                kind=cast(DeliveryRecipientKind, recipient.kind),
                agent=DeliveryAgentSnapshot(
                    agent_id=recipient.agent_id,
                    name=recipient.agent_name_snapshot,
                    generation=recipient.agent_generation_snapshot,
                    project=target_project,
                ),
            )
        )
    if not isinstance(delivery.attachments, list) or delivery.attachments:
        raise MessageDeliveryValidationError(
            "unsafe_attachment_metadata",
            "Pending delivery contains attachment metadata that was not normalized inline",
        )
    request = MessageDeliveryRequest(
        target_project=target_project,
        sender=sender,
        actor=actor,
        recipients=tuple(recipient_snapshots),
        idempotency_key=delivery.idempotency_key,
        thread_id=delivery.thread_id,
        reply_to_message_id=delivery.reply_to_message_id,
        topic=delivery.topic,
        subject=delivery.subject,
        body_md=delivery.body_md,
        purpose=cast(DeliveryPurpose, delivery.delivery_kind),
        importance=delivery.importance,
        ack_required=delivery.ack_required,
        attachments=(),
    )
    normalized = _normalize_delivery_request(request)
    if normalized.request != request:
        raise MessageDeliveryValidationError(
            "noncanonical_delivery_snapshot",
            "Delivery database snapshots are not in their canonical form",
        )
    if normalized.request_sha256 != delivery.request_sha256:
        raise MessageDeliveryValidationError(
            "request_hash_mismatch",
            "Delivery request hash does not match its immutable database snapshots",
        )
    expected_document, expected_document_sha256 = _build_archive_document(
        normalized,
        delivery.id,
        delivery.created_ts,
    )
    if (
        expected_document != delivery.archive_document
        or expected_document_sha256 != delivery.document_sha256
    ):
        raise MessageDeliveryValidationError(
            "archive_document_snapshot_mismatch",
            "Immutable delivery document differs from its database snapshots",
        )
    return normalized.request


async def _load_delivery_recipients(
    session: AsyncSession,
    delivery_id: str,
) -> list[MessageDeliveryRecipient]:
    result = await session.execute(
        select(MessageDeliveryRecipient)
        .where(cast(Any, MessageDeliveryRecipient.delivery_id == delivery_id))
        .order_by(cast(Any, MessageDeliveryRecipient.ordinal).asc())
    )
    recipients = list(result.scalars().all())
    if not recipients or [recipient.ordinal for recipient in recipients] != list(
        range(len(recipients))
    ):
        raise MessageDeliveryValidationError(
            "recipient_order_invalid",
            "Delivery recipient snapshots are missing or non-contiguous",
        )
    return recipients


async def _find_idempotent_delivery(
    session: AsyncSession,
    normalized: _NormalizedDeliveryRequest,
) -> MessageDelivery | None:
    actor = normalized.request.actor
    actor_project = actor.source_project
    result = await session.execute(
        select(MessageDelivery).where(
            cast(Any, MessageDelivery.project_id == normalized.request.target_project.project_id),
            cast(
                Any,
                MessageDelivery.project_generation_snapshot
                == normalized.request.target_project.generation,
            ),
            cast(Any, MessageDelivery.actor_kind == actor.kind),
            cast(Any, MessageDelivery.actor_id == actor.actor_id),
            cast(Any, MessageDelivery.actor_project_generation_snapshot).is_(None)
            if actor_project is None
            else cast(
                Any,
                MessageDelivery.actor_project_generation_snapshot
                == actor_project.generation,
            ),
            cast(Any, MessageDelivery.actor_generation_snapshot).is_(actor.generation)
            if actor.generation is None
            else cast(Any, MessageDelivery.actor_generation_snapshot == actor.generation),
            cast(Any, MessageDelivery.idempotency_key == normalized.request.idempotency_key),
        )
    )
    return result.scalar_one_or_none()


def _acceptance_from_delivery(
    delivery: MessageDelivery,
    *,
    reused: bool,
) -> MessageDeliveryAcceptance:
    return MessageDeliveryAcceptance(
        delivery_id=delivery.id,
        state=delivery.state,
        request_sha256=delivery.request_sha256,
        document_sha256=delivery.document_sha256,
        reused=reused,
    )


async def accept_message_delivery(
    request: MessageDeliveryRequest,
    *,
    now: datetime | None = None,
) -> MessageDeliveryAcceptance:
    """Accept or idempotently recover one immutable delivery intent."""
    await ensure_schema()
    normalized = _normalize_delivery_request(request)
    accepted_at = now or _utcnow_naive()
    if accepted_at.tzinfo is not None:
        accepted_at = accepted_at.astimezone(timezone.utc).replace(tzinfo=None)

    try:
        async with get_immediate_session() as session:
            await _validate_request_lifetimes(session, normalized.request, accepted_at)
            existing = await _find_idempotent_delivery(session, normalized)
            if existing is not None:
                if existing.request_sha256 != normalized.request_sha256:
                    raise MessageDeliveryIdempotencyConflictError(existing.id)
                return _acceptance_from_delivery(existing, reused=True)

            delivery_id = str(uuid.uuid4())
            archive_document, document_sha256 = _build_archive_document(
                normalized,
                delivery_id,
                accepted_at,
            )
            actor = normalized.request.actor
            delivery = MessageDelivery(
                id=delivery_id,
                state="pending",
                delivery_kind=normalized.request.purpose,
                project_id=normalized.request.target_project.project_id,
                project_slug_snapshot=normalized.request.target_project.slug,
                project_generation_snapshot=normalized.request.target_project.generation,
                sender_project_id_snapshot=normalized.request.sender.project.project_id,
                sender_project_slug_snapshot=normalized.request.sender.project.slug,
                sender_project_generation_snapshot=(
                    normalized.request.sender.project.generation
                ),
                sender_id=normalized.request.sender.agent_id,
                sender_name_snapshot=normalized.request.sender.name,
                sender_generation_snapshot=normalized.request.sender.generation,
                actor_kind=actor.kind,
                actor_id=actor.actor_id,
                actor_name_snapshot=actor.name,
                actor_project_id_snapshot=(
                    actor.source_project.project_id
                    if actor.source_project is not None
                    else None
                ),
                actor_project_slug_snapshot=(
                    actor.source_project.slug
                    if actor.source_project is not None
                    else None
                ),
                actor_project_generation_snapshot=(
                    actor.source_project.generation
                    if actor.source_project is not None
                    else None
                ),
                actor_generation_snapshot=actor.generation,
                actor_epoch_snapshot=actor.epoch,
                idempotency_key=normalized.request.idempotency_key,
                request_sha256=normalized.request_sha256,
                thread_id=normalized.request.thread_id,
                reply_to_message_id=normalized.request.reply_to_message_id,
                topic=normalized.request.topic,
                subject=normalized.request.subject,
                body_md=normalized.request.body_md,
                importance=normalized.request.importance,
                ack_required=normalized.request.ack_required,
                attachments=normalized.attachments,
                archive_document=archive_document,
                document_sha256=document_sha256,
                created_ts=accepted_at,
                next_attempt_ts=accepted_at,
            )
            session.add(delivery)
            await session.flush()
            for ordinal, recipient in enumerate(normalized.request.recipients):
                session.add(
                    MessageDeliveryRecipient(
                        delivery_id=delivery_id,
                        ordinal=ordinal,
                        kind=recipient.kind,
                        agent_id=recipient.agent.agent_id,
                        agent_name_snapshot=recipient.agent.name,
                        agent_generation_snapshot=recipient.agent.generation,
                        project_id_snapshot=recipient.agent.project.project_id,
                    )
                )
            await _commit_preserving_cancellation(session)
            return _acceptance_from_delivery(delivery, reused=False)
    except IntegrityError:
        # A second process may win the unique idempotency race after our read.
        async with get_immediate_session() as recovery_session:
            existing = await _find_idempotent_delivery(recovery_session, normalized)
            if existing is None:
                raise
            if existing.request_sha256 != normalized.request_sha256:
                raise MessageDeliveryIdempotencyConflictError(existing.id) from None
            return _acceptance_from_delivery(existing, reused=True)


def _assert_current_lease(
    delivery: MessageDelivery,
    lease: MessageDeliveryLease,
    now: datetime,
    *,
    require_unexpired: bool = True,
) -> None:
    if (
        delivery.state != "pending"
        or delivery.lease_token != lease.token
        or delivery.lease_fence != lease.fence
        or delivery.lease_expires_ts is None
        or (require_unexpired and delivery.lease_expires_ts <= now)
    ):
        raise MessageDeliveryLeaseLostError(delivery.id)


async def _quarantine_loaded_delivery(
    session: AsyncSession,
    delivery: MessageDelivery,
    reason: str,
    now: datetime,
) -> None:
    delivery.state = "quarantined"
    delivery.quarantined_ts = now
    delivery.quarantine_reason = reason[:_MAX_ERROR_LENGTH]
    delivery.last_error = reason[:_MAX_ERROR_LENGTH]
    delivery.next_attempt_ts = None
    delivery.lease_token = None
    delivery.lease_expires_ts = None
    session.add(delivery)
    await _commit_preserving_cancellation(session)


async def claim_message_delivery(
    delivery_id: str,
    *,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> MessageDeliveryLease | None:
    """Acquire a due pending intent with a monotonically increasing fence."""
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    await ensure_schema()
    claimed_at = now or _utcnow_naive()
    if claimed_at.tzinfo is not None:
        claimed_at = claimed_at.astimezone(timezone.utc).replace(tzinfo=None)
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, delivery_id)
        if delivery is None:
            raise MessageDeliveryNotFoundError(delivery_id)
        if delivery.state != "pending":
            return None
        if delivery.next_attempt_ts is not None and delivery.next_attempt_ts > claimed_at:
            return None
        if (
            delivery.lease_token is not None
            and delivery.lease_expires_ts is not None
            and delivery.lease_expires_ts > claimed_at
        ):
            return None

        try:
            recipients = await _load_delivery_recipients(session, delivery.id)
            request = _request_from_delivery(delivery, recipients)
            await _validate_request_lifetimes(session, request, claimed_at)
        except MessageDeliveryValidationError as exc:
            await _quarantine_loaded_delivery(session, delivery, f"{exc.code}: {exc}", claimed_at)
            raise MessageDeliveryTerminalError(delivery.id, f"{exc.code}: {exc}") from exc

        token = uuid.uuid4().hex
        delivery.lease_token = token
        delivery.lease_fence += 1
        delivery.lease_expires_ts = claimed_at + timedelta(seconds=lease_seconds)
        delivery.attempt_count += 1
        delivery.last_attempt_ts = claimed_at
        session.add(delivery)
        await _commit_preserving_cancellation(session)
        return MessageDeliveryLease(
            delivery_id=delivery.id,
            token=token,
            fence=delivery.lease_fence,
            expires_ts=delivery.lease_expires_ts,
            attempt_count=delivery.attempt_count,
        )


async def renew_message_delivery_lease(
    lease: MessageDeliveryLease,
    *,
    extend_seconds: int = 60,
    now: datetime | None = None,
) -> MessageDeliveryLease:
    """Extend a currently owned lease without changing its fencing token."""
    if extend_seconds < 1:
        raise ValueError("extend_seconds must be positive")
    renewed_at = now or _utcnow_naive()
    if renewed_at.tzinfo is not None:
        renewed_at = renewed_at.astimezone(timezone.utc).replace(tzinfo=None)
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, lease.delivery_id)
        if delivery is None:
            raise MessageDeliveryNotFoundError(lease.delivery_id)
        _assert_current_lease(delivery, lease, renewed_at)
        current_expiry = cast(datetime, delivery.lease_expires_ts)
        delivery.lease_expires_ts = max(current_expiry, renewed_at) + timedelta(
            seconds=extend_seconds
        )
        session.add(delivery)
        await _commit_preserving_cancellation(session)
        return MessageDeliveryLease(
            delivery_id=lease.delivery_id,
            token=lease.token,
            fence=lease.fence,
            expires_ts=delivery.lease_expires_ts,
            attempt_count=delivery.attempt_count,
        )


async def _checkpoint_publication(
    lease: MessageDeliveryLease,
    publication: MessageDeliveryPublication,
    now: datetime,
) -> None:
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, lease.delivery_id)
        if delivery is None:
            raise MessageDeliveryNotFoundError(lease.delivery_id)
        if delivery.state == "published":
            return
        _assert_current_lease(delivery, lease, now)
        existing_receipt = (
            delivery.archive_relative_path,
            delivery.archive_blob_sha,
            delivery.archive_commit_sha,
        )
        new_receipt = (
            publication.relative_path,
            publication.blob_sha,
            publication.commit_sha,
        )
        if any(value is not None for value in existing_receipt) and existing_receipt != new_receipt:
            await _quarantine_loaded_delivery(
                session,
                delivery,
                "verified Git receipt conflicts with the existing immutable receipt",
                now,
            )
            raise MessageDeliveryTerminalError(
                delivery.id,
                "verified Git receipt conflicts with the existing immutable receipt",
            )
        delivery.archive_relative_path = publication.relative_path
        delivery.archive_blob_sha = publication.blob_sha
        delivery.archive_commit_sha = publication.commit_sha
        session.add(delivery)
        await _commit_preserving_cancellation(session)


def _publication_matches_delivery(
    delivery: MessageDelivery,
    publication: MessageDeliveryPublication,
) -> bool:
    return (
        delivery.document_sha256 == publication.document_sha256
        and delivery.archive_relative_path == publication.relative_path
        and delivery.archive_blob_sha == publication.blob_sha
        and delivery.archive_commit_sha == publication.commit_sha
    )


async def _finalize_message_delivery(
    lease: MessageDeliveryLease,
    publication: MessageDeliveryPublication,
    now: datetime,
) -> MessageDeliveryProcessingResult:
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, lease.delivery_id)
        if delivery is None:
            raise MessageDeliveryNotFoundError(lease.delivery_id)
        if delivery.state == "published":
            if not _publication_matches_delivery(delivery, publication):
                raise MessageDeliveryTerminalError(
                    delivery.id,
                    "published receipt does not match the verified Git publication",
                )
            return MessageDeliveryProcessingResult(
                delivery_id=delivery.id,
                status="published",
                message_id=delivery.message_id,
                commit_sha=delivery.archive_commit_sha,
            )
        if delivery.state == "quarantined":
            raise MessageDeliveryTerminalError(
                delivery.id,
                delivery.quarantine_reason or "unknown quarantine reason",
            )
        _assert_current_lease(delivery, lease, now)
        if not _publication_matches_delivery(delivery, publication):
            raise MessageDeliveryValidationError(
                "publication_receipt_mismatch",
                "Verified publication does not match the delivery receipt checkpoint",
            )

        recipients = await _load_delivery_recipients(session, delivery.id)
        request = _request_from_delivery(delivery, recipients)
        await _validate_request_lifetimes(session, request, now)
        message = Message(
            delivery_id=delivery.id,
            project_id=delivery.project_id,
            sender_id=delivery.sender_id,
            thread_id=delivery.thread_id,
            reply_to=delivery.reply_to_message_id,
            topic=delivery.topic,
            subject=delivery.subject,
            body_md=delivery.body_md,
            importance=delivery.importance,
            ack_required=delivery.ack_required,
            created_ts=delivery.created_ts,
            attachments=list(delivery.attachments),
        )
        session.add(message)
        await session.flush()
        message_id = cast(int, message.id)
        for recipient in recipients:
            session.add(
                MessageRecipient(
                    message_id=message_id,
                    agent_id=recipient.agent_id,
                    kind=recipient.kind,
                )
            )
        await session.flush()

        delivery.state = "published"
        delivery.message_id = message_id
        delivery.published_ts = now
        delivery.next_attempt_ts = None
        delivery.last_error = None
        delivery.lease_token = None
        delivery.lease_expires_ts = None
        session.add(delivery)
        await _commit_preserving_cancellation(session)
        return MessageDeliveryProcessingResult(
            delivery_id=delivery.id,
            status="published",
            message_id=message_id,
            commit_sha=delivery.archive_commit_sha,
            published_now=True,
        )


async def get_message_delivery_status(
    delivery_id: str,
    *,
    now: datetime | None = None,
) -> MessageDeliveryProcessingResult:
    """Read one delivery status without claiming or mutating its lease."""
    await ensure_schema()
    observed_at = now or _utcnow_naive()
    if observed_at.tzinfo is not None:
        observed_at = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, delivery_id)
        if delivery is None:
            raise MessageDeliveryNotFoundError(delivery_id)
        return _processing_result_from_delivery(delivery, now=observed_at)


async def emit_published_delivery_notifications(delivery_id: str) -> None:
    """Emit best-effort private wake hints for one published delivery.

    Callers must invoke this only when ``published_now`` is true.  Keeping the
    transition fact on the processing result prevents an idempotent replay from
    waking the same mailbox again, while still covering a successful retry.
    """
    try:
        # Keep the serialized lifetime snapshot open through every hint.  The
        # transports address agents by mutable name, so releasing the SQLite
        # writer lock after validation would let a deleted/recreated lifetime
        # receive an old subject before the signal is emitted.
        async with get_immediate_session() as session:
            delivery = await session.get(MessageDelivery, delivery_id)
            if (
                delivery is None
                or delivery.state != "published"
                or delivery.message_id is None
            ):
                return
            message = await session.get(Message, delivery.message_id)
            if (
                message is None
                or message.delivery_id != delivery.id
                or message.project_id != delivery.project_id
            ):
                return
            project = await session.get(Project, delivery.project_id)
            if (
                project is None
                or project.slug != delivery.project_slug_snapshot
                or project.project_generation != delivery.project_generation_snapshot
                or project.archived_at is not None
            ):
                return
            recipient_rows = await session.execute(
                select(MessageDeliveryRecipient)
                .where(
                    cast(Any, MessageDeliveryRecipient.delivery_id == delivery.id)
                )
                .order_by(cast(Any, MessageDeliveryRecipient.ordinal))
            )
            recipient_snapshots = list(recipient_rows.scalars().all())
            recipients: list[tuple[str, str, str]] = []
            for recipient in recipient_snapshots:
                target = await session.get(Agent, recipient.agent_id)
                if (
                    target is None
                    or target.project_id != recipient.project_id_snapshot
                    or target.name != recipient.agent_name_snapshot
                    or target.agent_generation != recipient.agent_generation_snapshot
                    or target.retired_at is not None
                ):
                    continue
                recipients.append(
                    (
                        recipient.kind,
                        recipient.agent_name_snapshot,
                        recipient.agent_generation_snapshot,
                    )
                )

            instant_hint = {
                "kind": "message",
                "project": delivery.project_slug_snapshot,
                "id": delivery.message_id,
            }
            for _kind, target_name, target_generation in recipients:
                with suppress(Exception):
                    hub.publish(
                        delivery.project_slug_snapshot,
                        delivery.project_generation_snapshot,
                        target_name,
                        target_generation,
                        {**instant_hint, "agent": target_name},
                    )
            with suppress(Exception):
                hub.publish_project(
                    delivery.project_slug_snapshot,
                    delivery.project_generation_snapshot,
                )

            resolved_settings = get_settings()
            notification_project_slug = delivery.project_slug_snapshot

        # Signal files address a mutable human-readable name.  Emit only an
        # opaque wake outside the serialized DB snapshot: a recipient lifetime
        # that ends in this small gap cannot receive the old subject or sender.
        # The bounded fan-out also keeps an optional slow signals directory off
        # the request and SQLite writer critical paths.
        if resolved_settings.notifications.enabled:
            signal_calls = [
                emit_notification_signal(
                    resolved_settings,
                    notification_project_slug,
                    target_name,
                    None,
                )
                for kind, target_name, _target_generation in recipients
                if kind != "bcc"
            ]
        else:
            signal_calls = []
        if signal_calls:
            with suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(*signal_calls),
                    timeout=1.0,
                )
    except Exception:
        return


async def _current_processing_result(delivery_id: str) -> MessageDeliveryProcessingResult:
    return await get_message_delivery_status(delivery_id)


async def _reconciled_processing_result(
    lease: MessageDeliveryLease,
) -> MessageDeliveryProcessingResult:
    """Re-read a terminal result and identify only this lease's publication."""
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, lease.delivery_id)
        if delivery is None:
            raise MessageDeliveryNotFoundError(lease.delivery_id)
        current = _processing_result_from_delivery(delivery, now=_utcnow_naive())
        if current.status != "published" or delivery.lease_fence != lease.fence:
            return current
        return MessageDeliveryProcessingResult(
            delivery_id=current.delivery_id,
            status=current.status,
            message_id=current.message_id,
            commit_sha=current.commit_sha,
            next_attempt_ts=current.next_attempt_ts,
            error=current.error,
            published_now=True,
        )


def _processing_result_from_delivery(
    delivery: MessageDelivery,
    *,
    now: datetime,
) -> MessageDeliveryProcessingResult:
    if delivery.state == "published":
        status: DeliveryProcessingStatus = "published"
    elif delivery.state == "quarantined":
        status = "quarantined"
    elif (
        delivery.lease_token is not None
        and delivery.lease_expires_ts is not None
        and delivery.lease_expires_ts > now
    ):
        status = "busy"
    elif delivery.next_attempt_ts is not None and delivery.next_attempt_ts > now:
        status = "deferred"
    else:
        status = "pending"
    return MessageDeliveryProcessingResult(
        delivery_id=delivery.id,
        status=status,
        message_id=delivery.message_id,
        commit_sha=delivery.archive_commit_sha,
        next_attempt_ts=delivery.next_attempt_ts,
        error=delivery.quarantine_reason or delivery.last_error,
    )


async def _record_processing_failure(
    lease: MessageDeliveryLease,
    error: str,
    *,
    max_attempts: int,
    now: datetime,
    quarantine: bool,
) -> MessageDeliveryProcessingResult:
    reason = error[:_MAX_ERROR_LENGTH]
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, lease.delivery_id)
        if delivery is None:
            raise MessageDeliveryNotFoundError(lease.delivery_id)
        if delivery.state != "pending":
            return _processing_result_from_delivery(delivery, now=now)
        _assert_current_lease(delivery, lease, now, require_unexpired=False)
        should_quarantine = quarantine or delivery.attempt_count >= max_attempts
        if should_quarantine:
            await _quarantine_loaded_delivery(session, delivery, reason, now)
            return MessageDeliveryProcessingResult(
                delivery_id=delivery.id,
                status="quarantined",
                commit_sha=delivery.archive_commit_sha,
                error=reason,
            )

        backoff_seconds = min(300, max(1, 2 ** max(0, delivery.attempt_count - 1)))
        delivery.backoff_seconds = backoff_seconds
        delivery.next_attempt_ts = now + timedelta(seconds=backoff_seconds)
        delivery.last_error = reason
        delivery.lease_token = None
        delivery.lease_expires_ts = None
        session.add(delivery)
        await _commit_preserving_cancellation(session)
        return MessageDeliveryProcessingResult(
            delivery_id=delivery.id,
            status="pending",
            commit_sha=delivery.archive_commit_sha,
            next_attempt_ts=delivery.next_attempt_ts,
            error=reason,
        )


async def process_claimed_message_delivery(
    lease: MessageDeliveryLease,
    *,
    settings: Settings | None = None,
    max_attempts: int = 8,
    now: datetime | None = None,
) -> MessageDeliveryProcessingResult:
    """Publish and finalize one already-claimed delivery lease."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    processed_at = now or _utcnow_naive()
    if processed_at.tzinfo is not None:
        processed_at = processed_at.astimezone(timezone.utc).replace(tzinfo=None)
    resolved_settings = settings or get_settings()

    def _operation_time() -> datetime:
        return processed_at if now is not None else _utcnow_naive()

    try:
        async with get_immediate_session() as load_session:
            delivery = await load_session.get(MessageDelivery, lease.delivery_id)
            if delivery is None:
                raise MessageDeliveryNotFoundError(lease.delivery_id)
            if delivery.state == "published":
                return _processing_result_from_delivery(delivery, now=_operation_time())
            if delivery.state == "quarantined":
                return _processing_result_from_delivery(delivery, now=_operation_time())
            _assert_current_lease(delivery, lease, _operation_time())
            recipients = await _load_delivery_recipients(load_session, delivery.id)
            request = _request_from_delivery(delivery, recipients)
            await _validate_request_lifetimes(load_session, request, _operation_time())
            project_slug = delivery.project_slug_snapshot
            document_bytes = delivery.archive_document.encode()
            document_sha256 = delivery.document_sha256

        archive = await ensure_archive(resolved_settings, project_slug)
        publication = await publish_message_delivery(
            archive,
            lease.delivery_id,
            document_bytes,
            document_sha256,
            lease_fence=lease.fence,
        )
        await _checkpoint_publication(lease, publication, _operation_time())
        return await _finalize_message_delivery(lease, publication, _operation_time())
    except asyncio.CancelledError:
        # The storage primitive waits for its shielded Git thread before this
        # propagates. Leaving the lease intact lets a later fenced claimant
        # reconcile an already-created immutable commit without guessing.
        raise
    except MessageDeliveryQuarantinedError as exc:
        return await _record_processing_failure(
            lease,
            f"archive_quarantined: {exc.reason}",
            max_attempts=max_attempts,
            now=_operation_time(),
            quarantine=True,
        )
    except MessageDeliveryTerminalError:
        return await _current_processing_result(lease.delivery_id)
    except MessageDeliveryValidationError as exc:
        return await _record_processing_failure(
            lease,
            f"{exc.code}: {exc}",
            max_attempts=max_attempts,
            now=_operation_time(),
            quarantine=True,
        )
    except (MessageDeliveryPendingError, MessageDeliveryWorkspaceConflictError) as exc:
        return await _record_processing_failure(
            lease,
            f"archive_pending: {exc}",
            max_attempts=max_attempts,
            now=_operation_time(),
            quarantine=False,
        )
    except MessageDeliveryLeaseLostError:
        raise
    except Exception as exc:
        # A final COMMIT may have succeeded and lost its return value. Re-read
        # in a fresh serialized session before classifying it as retryable.
        current = await _reconciled_processing_result(lease)
        if current.status == "published":
            return current
        if current.status == "quarantined":
            return current
        return await _record_processing_failure(
            lease,
            f"processing_failed: {type(exc).__name__}: {exc}",
            max_attempts=max_attempts,
            now=_operation_time(),
            quarantine=False,
        )


async def process_message_delivery(
    delivery_id: str,
    *,
    settings: Settings | None = None,
    lease_seconds: int = 60,
    max_attempts: int = 8,
) -> MessageDeliveryProcessingResult:
    """Claim one due delivery, or report why no worker action was taken."""
    try:
        lease = await claim_message_delivery(
            delivery_id,
            lease_seconds=lease_seconds,
        )
    except MessageDeliveryTerminalError:
        return await _current_processing_result(delivery_id)
    if lease is None:
        return await _current_processing_result(delivery_id)
    return await process_claimed_message_delivery(
        lease,
        settings=settings,
        max_attempts=max_attempts,
    )
