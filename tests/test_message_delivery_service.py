"""Service tests for durable accept, fenced processing, and atomic visibility."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from sqlalchemy import delete, func, select, text
from sqlmodel import col

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_immediate_session
from mcp_agent_mail.delivery import (
    DeliveryActorSnapshot,
    DeliveryAgentSnapshot,
    DeliveryProjectSnapshot,
    DeliveryRecipientSnapshot,
    MessageDeliveryIdempotencyConflictError,
    MessageDeliveryLease,
    MessageDeliveryLeaseLostError,
    MessageDeliveryNotFoundError,
    MessageDeliveryRequest,
    MessageDeliveryTerminalError,
    MessageDeliveryValidationError,
    accept_message_delivery,
    claim_message_delivery,
    get_message_delivery_status,
    process_claimed_message_delivery,
    process_message_delivery,
    renew_message_delivery_lease,
)
from mcp_agent_mail.models import (
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
from mcp_agent_mail.storage import (
    MessageDeliveryPendingError,
    MessageDeliveryQuarantinedError,
)

BASE_TIME = datetime(2030, 1, 2, 3, 4, 5)


@dataclass(frozen=True, slots=True)
class _SeededDelivery:
    target: DeliveryProjectSnapshot
    source: DeliveryProjectSnapshot
    sender: DeliveryAgentSnapshot
    to: DeliveryAgentSnapshot
    cc: DeliveryAgentSnapshot
    bcc: DeliveryAgentSnapshot

    def request(
        self,
        idempotency_key: str,
        *,
        actor: DeliveryActorSnapshot | None = None,
    ) -> MessageDeliveryRequest:
        return MessageDeliveryRequest(
            target_project=self.target,
            sender=self.sender,
            actor=actor or DeliveryActorSnapshot.agent(self.sender),
            recipients=(
                DeliveryRecipientSnapshot(kind="to", agent=self.to),
                DeliveryRecipientSnapshot(kind="cc", agent=self.cc),
                DeliveryRecipientSnapshot(kind="bcc", agent=self.bcc),
            ),
            idempotency_key=idempotency_key,
            thread_id="HERMES-ATOMIC-DELIVERY",
            topic="delivery",
            subject="Atomic hello",
            body_md="Verified body",
            importance="high",
            ack_required=True,
        )


def _project_snapshot(project: Project) -> DeliveryProjectSnapshot:
    assert project.id is not None
    return DeliveryProjectSnapshot(
        project_id=project.id,
        slug=project.slug,
        generation=project.project_generation,
    )


def _agent_snapshot(
    agent: Agent,
    project: DeliveryProjectSnapshot,
) -> DeliveryAgentSnapshot:
    assert agent.id is not None
    return DeliveryAgentSnapshot(
        agent_id=agent.id,
        name=agent.name,
        generation=agent.agent_generation,
        project=project,
    )


async def _seed_identities(*, cross_project: bool = False) -> _SeededDelivery:
    await ensure_schema()
    async with get_immediate_session() as session:
        source_project = Project(slug="delivery-source", human_key="delivery-source")
        session.add(source_project)
        if cross_project:
            target_project = Project(slug="delivery-target", human_key="delivery-target")
            session.add(target_project)
        else:
            target_project = source_project
        await session.flush()
        assert source_project.id is not None
        assert target_project.id is not None

        sender = Agent(
            project_id=source_project.id,
            name="SenderAgent",
            program="pytest",
            model="test",
            contact_policy="open",
        )
        to_agent = Agent(
            project_id=target_project.id,
            name="VisibleTo",
            program="pytest",
            model="test",
            contact_policy="open",
        )
        cc_agent = Agent(
            project_id=target_project.id,
            name="VisibleCc",
            program="pytest",
            model="test",
            contact_policy="open",
        )
        bcc_agent = Agent(
            project_id=target_project.id,
            name="SecretBcc",
            program="pytest",
            model="test",
            contact_policy="open",
        )
        session.add_all([sender, to_agent, cc_agent, bcc_agent])
        await session.flush()

        if cross_project:
            assert sender.id is not None
            for recipient in (to_agent, cc_agent, bcc_agent):
                assert recipient.id is not None
                session.add(
                    AgentLink(
                        a_project_id=source_project.id,
                        a_agent_id=sender.id,
                        b_project_id=target_project.id,
                        b_agent_id=recipient.id,
                        status="approved",
                    )
                )
        await session.commit()

    source_snapshot = _project_snapshot(source_project)
    target_snapshot = _project_snapshot(target_project)
    return _SeededDelivery(
        source=source_snapshot,
        target=target_snapshot,
        sender=_agent_snapshot(sender, source_snapshot),
        to=_agent_snapshot(to_agent, target_snapshot),
        cc=_agent_snapshot(cc_agent, target_snapshot),
        bcc=_agent_snapshot(bcc_agent, target_snapshot),
    )


async def _row_count(model: type[Any]) -> int:
    async with get_immediate_session() as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


async def _set_route_status(
    seeded: _SeededDelivery,
    recipient: DeliveryAgentSnapshot,
    status: str,
) -> None:
    async with get_immediate_session() as session:
        result = await session.execute(
            select(AgentLink).where(
                col(AgentLink.a_project_id) == seeded.source.project_id,
                col(AgentLink.a_agent_id) == seeded.sender.agent_id,
                col(AgentLink.b_project_id) == recipient.project.project_id,
                col(AgentLink.b_agent_id) == recipient.agent_id,
            )
        )
        link = result.scalar_one()
        link.status = status
        session.add(link)
        await session.commit()


async def _seed_inbound_message(
    seeded: _SeededDelivery,
    *,
    thread_id: str | None,
    project: DeliveryProjectSnapshot | None = None,
    sender: DeliveryAgentSnapshot | None = None,
    recipient: DeliveryAgentSnapshot | None = None,
    include_recipient: bool = True,
) -> int:
    async with get_immediate_session() as session:
        message = Message(
            project_id=(project or seeded.source).project_id,
            sender_id=(sender or seeded.to).agent_id,
            thread_id=thread_id,
            subject="Inbound thread seed",
            body_md="Inbound body",
            created_ts=BASE_TIME - timedelta(minutes=1),
        )
        session.add(message)
        await session.flush()
        assert message.id is not None
        if include_recipient:
            session.add(
                MessageRecipient(
                    message_id=message.id,
                    agent_id=(recipient or seeded.sender).agent_id,
                    kind="to",
                )
            )
        await session.commit()
        return message.id


@pytest.mark.asyncio
async def test_purge_old_messages_detaches_retained_replies_and_skips_pending_targets(
    isolated_env: Any,
) -> None:
    seeded = await _seed_identities(cross_project=True)
    await _set_route_status(seeded, seeded.to, "blocked")
    await _seed_inbound_message(
        seeded,
        thread_id="purge-pending-target",
    )
    old_time = datetime(2000, 1, 1)
    async with get_immediate_session() as session:
        purge_parent = Message(
            project_id=seeded.target.project_id,
            sender_id=seeded.sender.agent_id,
            thread_id="purge-retained-child",
            subject="Purge parent",
            body_md="Old and eligible.",
            created_ts=old_time,
        )
        protected_target = Message(
            project_id=seeded.target.project_id,
            sender_id=seeded.sender.agent_id,
            thread_id="purge-pending-target",
            subject="Protected pending target",
            body_md="Old but protected.",
            created_ts=old_time,
        )
        session.add_all([purge_parent, protected_target])
        await session.flush()
        assert purge_parent.id is not None
        assert protected_target.id is not None
        retained_child = Message(
            project_id=seeded.target.project_id,
            sender_id=seeded.to.agent_id,
            thread_id="purge-retained-child",
            reply_to=purge_parent.id,
            subject="Retained child",
            body_md="New enough to retain.",
            created_ts=datetime.now(),
        )
        session.add(retained_child)
        await session.flush()
        assert retained_child.id is not None
        purge_parent_id = purge_parent.id
        protected_target_id = protected_target.id
        retained_child_id = retained_child.id
        await session.commit()

    await accept_message_delivery(
        replace(
            seeded.request("purge-pending-target"),
            purpose="reply",
            thread_id="purge-pending-target",
            reply_to_message_id=protected_target_id,
            recipients=(DeliveryRecipientSnapshot(kind="to", agent=seeded.to),),
        ),
        now=BASE_TIME,
    )

    server = build_mcp_server()
    async with Client(server) as client:
        purged = await client.call_tool(
            "purge_old_messages",
            {
                "project_key": seeded.target.slug,
                "max_age_days": 1,
                "dry_run": False,
            },
        )

    assert purged.data["messages_affected"] == 1
    async with get_immediate_session() as session:
        assert await session.get(Message, purge_parent_id) is None
        assert await session.get(Message, protected_target_id) is not None
        retained = await session.get(Message, retained_child_id)
        assert retained is not None
        assert retained.reply_to is None
        foreign_keys = await session.execute(text("PRAGMA foreign_key_check"))
        assert foreign_keys.all() == []


@pytest.mark.asyncio
async def test_accept_is_idempotent_and_pending_is_invisible(isolated_env: Any) -> None:
    seeded = await _seed_identities()
    request = seeded.request("same-key")

    first = await accept_message_delivery(request, now=BASE_TIME)
    second = await accept_message_delivery(request, now=BASE_TIME + timedelta(seconds=1))

    assert first.delivery_id == second.delivery_id
    assert first.reused is False
    assert second.reused is True
    assert await _row_count(MessageDelivery) == 1
    assert await _row_count(Message) == 0
    with pytest.raises(MessageDeliveryIdempotencyConflictError):
        await accept_message_delivery(
            replace(request, body_md="different canonical payload"),
            now=BASE_TIME + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_concurrent_accept_and_claim_have_one_winner(isolated_env: Any) -> None:
    seeded = await _seed_identities()
    request = seeded.request("concurrent-key")

    first, second = await asyncio.gather(
        accept_message_delivery(request, now=BASE_TIME),
        accept_message_delivery(request, now=BASE_TIME),
    )
    assert first.delivery_id == second.delivery_id
    assert {first.reused, second.reused} == {False, True}

    first_lease, second_lease = await asyncio.gather(
        claim_message_delivery(first.delivery_id, now=BASE_TIME),
        claim_message_delivery(first.delivery_id, now=BASE_TIME),
    )
    assert sum(lease is not None for lease in (first_lease, second_lease)) == 1


@pytest.mark.asyncio
async def test_stale_fence_cannot_renew_after_takeover(isolated_env: Any) -> None:
    seeded = await _seed_identities()
    accepted = await accept_message_delivery(
        seeded.request("stale-fence"),
        now=BASE_TIME,
    )
    first = await claim_message_delivery(
        accepted.delivery_id,
        lease_seconds=1,
        now=BASE_TIME,
    )
    assert first is not None
    second = await claim_message_delivery(
        accepted.delivery_id,
        lease_seconds=60,
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert second is not None
    assert second.fence == first.fence + 1
    with pytest.raises(MessageDeliveryLeaseLostError):
        await renew_message_delivery_lease(
            first,
            now=BASE_TIME + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_only_winning_lease_reports_the_publication_transition(
    isolated_env: Any,
) -> None:
    seeded = await _seed_identities()
    accepted = await accept_message_delivery(
        seeded.request("published-now-fence"),
        now=BASE_TIME,
    )
    expired = await claim_message_delivery(
        accepted.delivery_id,
        lease_seconds=1,
        now=BASE_TIME,
    )
    assert expired is not None
    winner = await claim_message_delivery(
        accepted.delivery_id,
        lease_seconds=60,
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert winner is not None

    published = await process_claimed_message_delivery(
        winner,
        settings=get_settings(),
        now=BASE_TIME + timedelta(seconds=2),
    )
    stale_observation = await process_claimed_message_delivery(
        expired,
        settings=get_settings(),
        now=BASE_TIME + timedelta(seconds=3),
    )

    assert published.status == stale_observation.status == "published"
    assert published.published_now is True
    assert stale_observation.published_now is False


@pytest.mark.asyncio
async def test_cross_project_accept_publish_and_finalize(isolated_env: Any) -> None:
    seeded = await _seed_identities(cross_project=True)
    accepted = await accept_message_delivery(
        seeded.request("cross-project"),
        now=BASE_TIME,
    )
    assert await _row_count(Message) == 0

    lease = await claim_message_delivery(accepted.delivery_id, now=BASE_TIME)
    assert lease is not None
    result = await process_claimed_message_delivery(
        lease,
        settings=get_settings(),
        now=BASE_TIME,
    )
    assert result.status == "published"
    assert result.published_now is True
    assert result.message_id is not None

    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, accepted.delivery_id)
        message = await session.get(Message, result.message_id)
        assert delivery is not None
        assert message is not None
        assert delivery.state == "published"
        assert delivery.sender_project_id_snapshot == seeded.source.project_id
        assert delivery.sender_project_slug_snapshot == seeded.source.slug
        assert delivery.sender_project_generation_snapshot == seeded.source.generation
        assert delivery.actor_project_id_snapshot == seeded.source.project_id
        assert delivery.actor_project_slug_snapshot == seeded.source.slug
        assert delivery.actor_project_generation_snapshot == seeded.source.generation
        assert message.delivery_id == delivery.id
        assert message.project_id == seeded.target.project_id
        assert message.sender_id == seeded.sender.agent_id
        recipients_result = await session.execute(
            select(MessageRecipient).where(
                col(MessageRecipient.message_id) == result.message_id
            )
        )
        assert {(row.agent_id, row.kind) for row in recipients_result.scalars()} == {
            (seeded.to.agent_id, "to"),
            (seeded.cc.agent_id, "cc"),
            (seeded.bcc.agent_id, "bcc"),
        }
    repeated = await process_message_delivery(accepted.delivery_id, settings=get_settings())
    assert repeated.status == "published"
    assert repeated.published_now is False
    assert await _row_count(Message) == 1


@pytest.mark.asyncio
async def test_external_wake_is_content_free_and_runs_after_lifetime_snapshot(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_agent_mail import delivery as delivery_module

    seeded = await _seed_identities(cross_project=True)
    accepted = await accept_message_delivery(
        seeded.request("content-free-wake"),
        now=BASE_TIME,
    )
    lease = await claim_message_delivery(accepted.delivery_id, now=BASE_TIME)
    assert lease is not None
    published = await process_claimed_message_delivery(
        lease,
        settings=get_settings(),
        now=BASE_TIME,
    )
    assert published.published_now is True

    settings = get_settings()
    enabled_settings = replace(
        settings,
        notifications=replace(settings.notifications, enabled=True),
    )
    monkeypatch.setattr(delivery_module, "get_settings", lambda: enabled_settings)
    observed: list[tuple[str, Any]] = []

    async def record_signal(
        _settings: Any,
        _project_slug: str,
        agent_name: str,
        metadata: Any,
    ) -> bool:
        # Acquiring a second BEGIN IMMEDIATE here proves the lifetime snapshot
        # was released before any potentially slow filesystem notification.
        async with get_immediate_session() as session:
            await session.commit()
        observed.append((agent_name, metadata))
        return True

    monkeypatch.setattr(delivery_module, "emit_notification_signal", record_signal)

    await delivery_module.emit_published_delivery_notifications(accepted.delivery_id)

    assert observed == [(seeded.to.name, None), (seeded.cc.name, None)]


@pytest.mark.asyncio
async def test_notification_skips_reused_numeric_message_identity(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_agent_mail import delivery as delivery_module

    seeded = await _seed_identities(cross_project=True)
    accepted = await accept_message_delivery(
        seeded.request("reused-message-id-notification"),
        now=BASE_TIME,
    )
    lease = await claim_message_delivery(accepted.delivery_id, now=BASE_TIME)
    assert lease is not None
    published = await process_claimed_message_delivery(
        lease,
        settings=get_settings(),
        now=BASE_TIME,
    )
    assert published.message_id is not None

    async with get_immediate_session() as session:
        original = await session.get(Message, published.message_id)
        assert original is not None
        await session.execute(
            delete(MessageRecipient).where(
                col(MessageRecipient.message_id) == published.message_id
            )
        )
        await session.delete(original)
        await session.flush()
        session.add(
            Message(
                id=published.message_id,
                project_id=seeded.target.project_id,
                sender_id=seeded.sender.agent_id,
                subject="Unrelated replacement",
                body_md="Different message lifetime",
                created_ts=BASE_TIME + timedelta(seconds=1),
            )
        )
        await session.commit()

    emitted: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        delivery_module.hub,
        "publish",
        lambda *args: emitted.append(args),
    )
    monkeypatch.setattr(
        delivery_module.hub,
        "publish_project",
        lambda *args: emitted.append(args),
    )

    await delivery_module.emit_published_delivery_notifications(accepted.delivery_id)

    assert emitted == []


@pytest.mark.asyncio
async def test_cross_project_agent_reply_uses_exact_inbound_thread_route(
    isolated_env: Any,
) -> None:
    seeded = await _seed_identities(cross_project=True)
    await _set_route_status(seeded, seeded.to, "blocked")
    await _seed_inbound_message(seeded, thread_id="reply-thread")
    request = replace(
        seeded.request("thread-reply"),
        purpose="reply",
        thread_id="reply-thread",
        recipients=(DeliveryRecipientSnapshot(kind="to", agent=seeded.to),),
    )

    accepted = await accept_message_delivery(request, now=BASE_TIME)
    lease = await claim_message_delivery(accepted.delivery_id, now=BASE_TIME)
    assert lease is not None
    result = await process_claimed_message_delivery(
        lease,
        settings=get_settings(),
        now=BASE_TIME,
    )
    assert result.status == "published"
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, accepted.delivery_id)
        assert delivery is not None
        assert delivery.delivery_kind == "reply"
        assert '"purpose":"reply"' in delivery.archive_document


@pytest.mark.asyncio
async def test_reply_rejects_forged_thread_numeric_collision_and_nonrecipient(
    isolated_env: Any,
) -> None:
    seeded = await _seed_identities(cross_project=True)
    await _set_route_status(seeded, seeded.to, "blocked")
    await _seed_inbound_message(seeded, thread_id="real-thread")
    base_request = replace(
        seeded.request("reply-invalid"),
        purpose="reply",
        recipients=(DeliveryRecipientSnapshot(kind="to", agent=seeded.to),),
    )

    with pytest.raises(MessageDeliveryValidationError) as forged:
        await accept_message_delivery(
            replace(base_request, thread_id="forged-thread"),
            now=BASE_TIME,
        )
    assert forged.value.code == "reply_route_invalid"

    unrelated_id = await _seed_inbound_message(
        seeded,
        thread_id=None,
        project=seeded.target,
    )
    with pytest.raises(MessageDeliveryValidationError) as numeric_collision:
        await accept_message_delivery(
            replace(
                base_request,
                idempotency_key="reply-numeric-collision",
                thread_id=str(unrelated_id),
            ),
            now=BASE_TIME,
        )
    assert numeric_collision.value.code == "reply_route_invalid"

    await _seed_inbound_message(
        seeded,
        thread_id="sender-not-recipient",
        recipient=seeded.cc,
    )
    with pytest.raises(MessageDeliveryValidationError) as sender_not_recipient:
        await accept_message_delivery(
            replace(
                base_request,
                idempotency_key="reply-sender-not-recipient",
                thread_id="sender-not-recipient",
            ),
            now=BASE_TIME,
        )
    assert sender_not_recipient.value.code == "reply_route_invalid"

    await _set_route_status(seeded, seeded.cc, "blocked")
    with pytest.raises(MessageDeliveryValidationError) as extra_external:
        await accept_message_delivery(
            replace(
                base_request,
                idempotency_key="reply-extra-external",
                thread_id="real-thread",
                recipients=(
                    DeliveryRecipientSnapshot(kind="to", agent=seeded.to),
                    DeliveryRecipientSnapshot(kind="cc", agent=seeded.cc),
                ),
            ),
            now=BASE_TIME,
        )
    assert extra_external.value.code == "reply_route_invalid"


@pytest.mark.asyncio
async def test_deleted_inbound_reply_source_quarantines_before_claim(
    isolated_env: Any,
) -> None:
    seeded = await _seed_identities(cross_project=True)
    await _set_route_status(seeded, seeded.to, "blocked")
    source_message_id = await _seed_inbound_message(
        seeded,
        thread_id="deleted-reply-source",
    )
    accepted = await accept_message_delivery(
        replace(
            seeded.request("deleted-reply-source"),
            purpose="reply",
            thread_id="deleted-reply-source",
            recipients=(DeliveryRecipientSnapshot(kind="to", agent=seeded.to),),
        ),
        now=BASE_TIME,
    )

    # A normal application connection cannot delete this route source while a
    # recipient FK protects it. Reproduce external corruption so claim-time
    # validation still proves the accepted route fails closed if that source
    # disappears outside the application.
    database_url = get_settings().database.url
    prefix = "sqlite+aiosqlite:///"
    assert database_url.startswith(prefix)
    database_path = Path(database_url.removeprefix(prefix))
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="FOREIGN KEY",
        ):
            connection.execute(
                "DELETE FROM messages WHERE id = ?",
                (source_message_id,),
            )
        connection.rollback()
        connection.execute("PRAGMA foreign_keys=OFF")
        deleted_recipient = connection.execute(
            "DELETE FROM message_recipients WHERE message_id = ?",
            (source_message_id,),
        )
        deleted_message = connection.execute(
            "DELETE FROM messages WHERE id = ?",
            (source_message_id,),
        )
        assert deleted_recipient.rowcount == 1
        assert deleted_message.rowcount == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.commit()

    with pytest.raises(MessageDeliveryTerminalError):
        await claim_message_delivery(
            accepted.delivery_id,
            now=BASE_TIME + timedelta(seconds=1),
        )
    status = await get_message_delivery_status(
        accepted.delivery_id,
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert status.status == "quarantined"
    assert "reply_route_invalid" in (status.error or "")


@pytest.mark.asyncio
async def test_git_return_loss_is_recovered_without_duplicate_message(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_identities()
    accepted = await accept_message_delivery(
        seeded.request("git-return-loss"),
        now=BASE_TIME,
    )
    first_lease = await claim_message_delivery(accepted.delivery_id, now=BASE_TIME)
    assert first_lease is not None

    from mcp_agent_mail import delivery as delivery_module

    original_publish = delivery_module.publish_message_delivery

    async def publish_then_lose_return(*args: Any, **kwargs: Any) -> Any:
        await original_publish(*args, **kwargs)
        raise MessageDeliveryPendingError(accepted.delivery_id, "injected return loss")

    monkeypatch.setattr(delivery_module, "publish_message_delivery", publish_then_lose_return)
    first_result = await process_claimed_message_delivery(
        first_lease,
        settings=get_settings(),
        now=BASE_TIME,
    )
    assert first_result.status == "pending"
    assert await _row_count(Message) == 0

    monkeypatch.setattr(delivery_module, "publish_message_delivery", original_publish)
    second_lease = await claim_message_delivery(
        accepted.delivery_id,
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert second_lease is not None
    second_result = await process_claimed_message_delivery(
        second_lease,
        settings=get_settings(),
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert second_result.status == "published"
    assert await _row_count(Message) == 1


@pytest.mark.asyncio
async def test_finalization_failure_retries_from_receipt_checkpoint(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_identities()
    accepted = await accept_message_delivery(
        seeded.request("finalization-retry"),
        now=BASE_TIME,
    )
    first_lease = await claim_message_delivery(accepted.delivery_id, now=BASE_TIME)
    assert first_lease is not None

    from mcp_agent_mail import delivery as delivery_module

    original_commit = delivery_module._commit_preserving_cancellation
    calls = 0

    async def fail_finalization_commit(session: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected finalization commit failure")
        await original_commit(session)

    monkeypatch.setattr(
        delivery_module,
        "_commit_preserving_cancellation",
        fail_finalization_commit,
    )
    first_result = await process_claimed_message_delivery(
        first_lease,
        settings=get_settings(),
        now=BASE_TIME,
    )
    assert first_result.status == "pending"
    assert await _row_count(Message) == 0

    second_lease = await claim_message_delivery(
        accepted.delivery_id,
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert second_lease is not None
    second_result = await process_claimed_message_delivery(
        second_lease,
        settings=get_settings(),
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert second_result.status == "published"
    assert calls == 6
    assert await _row_count(Message) == 1


@pytest.mark.asyncio
async def test_finalization_commit_return_loss_preserves_first_publish_signal(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_identities()
    accepted = await accept_message_delivery(
        seeded.request("finalization-return-loss"),
        now=BASE_TIME,
    )
    lease = await claim_message_delivery(accepted.delivery_id, now=BASE_TIME)
    assert lease is not None

    from mcp_agent_mail import delivery as delivery_module

    original_commit = delivery_module._commit_preserving_cancellation
    calls = 0

    async def lose_final_commit_return(session: Any) -> None:
        nonlocal calls
        calls += 1
        await original_commit(session)
        if calls == 2:
            raise RuntimeError("injected finalization return loss")

    monkeypatch.setattr(
        delivery_module,
        "_commit_preserving_cancellation",
        lose_final_commit_return,
    )
    result = await process_claimed_message_delivery(
        lease,
        settings=get_settings(),
        now=BASE_TIME,
    )
    replay = await process_message_delivery(
        accepted.delivery_id,
        settings=get_settings(),
    )

    assert result.status == replay.status == "published"
    assert result.published_now is True
    assert replay.published_now is False
    assert await _row_count(Message) == 1


@pytest.mark.asyncio
async def test_storage_quarantine_is_terminal_and_never_visible(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_identities()
    accepted = await accept_message_delivery(
        seeded.request("quarantine"),
        now=BASE_TIME,
    )
    lease = await claim_message_delivery(accepted.delivery_id, now=BASE_TIME)
    assert lease is not None

    from mcp_agent_mail import delivery as delivery_module

    async def reject_publication(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise MessageDeliveryQuarantinedError(
            accepted.delivery_id,
            f"projects/{seeded.target.slug}/message_deliveries/{accepted.delivery_id}.md",
            "injected immutable mismatch",
            expected_sha256=accepted.document_sha256,
        )

    monkeypatch.setattr(delivery_module, "publish_message_delivery", reject_publication)
    result = await process_claimed_message_delivery(
        lease,
        settings=get_settings(),
        now=BASE_TIME,
    )
    assert result.status == "quarantined"
    assert "injected immutable mismatch" in (result.error or "")
    assert await _row_count(Message) == 0
    status = await get_message_delivery_status(accepted.delivery_id, now=BASE_TIME)
    assert status.status == "quarantined"
    assert status.error == result.error


@pytest.mark.asyncio
async def test_status_reports_missing_and_pending_without_claiming(isolated_env: Any) -> None:
    with pytest.raises(MessageDeliveryNotFoundError):
        await get_message_delivery_status(str(uuid.uuid4()), now=BASE_TIME)

    seeded = await _seed_identities()
    accepted = await accept_message_delivery(
        seeded.request("status-pending"),
        now=BASE_TIME,
    )
    status = await get_message_delivery_status(accepted.delivery_id, now=BASE_TIME)
    assert status.status == "pending"
    assert status.message_id is None
    assert status.commit_sha is None


@pytest.mark.asyncio
async def test_contact_request_requires_exact_live_pending_link_and_publishes(
    isolated_env: Any,
) -> None:
    seeded = await _seed_identities(cross_project=True)
    contact_request = replace(
        seeded.request("contact-request"),
        purpose="contact_request",
        recipients=(DeliveryRecipientSnapshot(kind="to", agent=seeded.to),),
    )

    with pytest.raises(MessageDeliveryValidationError) as approved_link:
        await accept_message_delivery(contact_request, now=BASE_TIME)
    assert approved_link.value.code == "contact_request_link_invalid"

    async with get_immediate_session() as session:
        result = await session.execute(
            select(AgentLink).where(
                col(AgentLink.a_agent_id) == seeded.sender.agent_id,
                col(AgentLink.b_agent_id) == seeded.to.agent_id,
            )
        )
        link = result.scalar_one()
        link.status = "pending"
        link.expires_ts = BASE_TIME - timedelta(seconds=1)
        session.add(link)
        await session.commit()

    with pytest.raises(MessageDeliveryValidationError) as expired_link:
        await accept_message_delivery(contact_request, now=BASE_TIME)
    assert expired_link.value.code == "contact_request_link_invalid"

    async with get_immediate_session() as session:
        result = await session.execute(
            select(AgentLink).where(
                col(AgentLink.a_agent_id) == seeded.sender.agent_id,
                col(AgentLink.b_agent_id) == seeded.to.agent_id,
            )
        )
        link = result.scalar_one()
        link.expires_ts = BASE_TIME + timedelta(minutes=5)
        session.add(link)
        await session.commit()

    accepted = await accept_message_delivery(contact_request, now=BASE_TIME)
    lease = await claim_message_delivery(accepted.delivery_id, now=BASE_TIME)
    assert lease is not None
    published = await process_claimed_message_delivery(
        lease,
        settings=get_settings(),
        now=BASE_TIME,
    )
    assert published.status == "published"
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, accepted.delivery_id)
        assert delivery is not None
        assert delivery.delivery_kind == "contact_request"
        assert '"purpose":"contact_request"' in delivery.archive_document


@pytest.mark.asyncio
async def test_contact_request_shape_rejects_non_agent_or_extra_recipient(
    isolated_env: Any,
) -> None:
    seeded = await _seed_identities(cross_project=True)
    request = seeded.request("contact-shape")
    with pytest.raises(MessageDeliveryValidationError) as extra_recipient:
        await accept_message_delivery(
            replace(request, purpose="contact_request"),
            now=BASE_TIME,
        )
    assert extra_recipient.value.code == "invalid_contact_request_shape"
    with pytest.raises(MessageDeliveryValidationError) as system_actor:
        await accept_message_delivery(
            replace(
                request,
                purpose="contact_request",
                actor=DeliveryActorSnapshot.system(),
                recipients=(DeliveryRecipientSnapshot(kind="to", agent=seeded.to),),
            ),
            now=BASE_TIME,
        )
    assert system_actor.value.code == "invalid_contact_request_shape"


@pytest.mark.asyncio
async def test_hostile_trigger_valid_archive_divergence_quarantines_before_git(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_identities()
    request = seeded.request("hostile-archive")
    from mcp_agent_mail import delivery as delivery_module

    normalized = delivery_module._normalize_delivery_request(request)
    delivery_id = str(uuid.uuid4())
    hostile_document = (
        "---json\n"
        + json.dumps(
            {
                "delivery_id": delivery_id,
                "purpose": "message",
                "subject": "forged independently of the database snapshots",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n---\n\nforged body\n"
    )
    delivery = MessageDelivery(
        id=delivery_id,
        state="pending",
        delivery_kind="message",
        project_id=seeded.target.project_id,
        project_slug_snapshot=seeded.target.slug,
        project_generation_snapshot=seeded.target.generation,
        sender_project_id_snapshot=seeded.source.project_id,
        sender_project_slug_snapshot=seeded.source.slug,
        sender_project_generation_snapshot=seeded.source.generation,
        sender_id=seeded.sender.agent_id,
        sender_name_snapshot=seeded.sender.name,
        sender_generation_snapshot=seeded.sender.generation,
        actor_kind="agent",
        actor_id=seeded.sender.agent_id,
        actor_name_snapshot=seeded.sender.name,
        actor_project_id_snapshot=seeded.source.project_id,
        actor_project_slug_snapshot=seeded.source.slug,
        actor_project_generation_snapshot=seeded.source.generation,
        actor_generation_snapshot=seeded.sender.generation,
        actor_epoch_snapshot=None,
        idempotency_key=request.idempotency_key,
        request_sha256=normalized.request_sha256,
        thread_id=request.thread_id,
        reply_to_message_id=request.reply_to_message_id,
        topic=request.topic,
        subject=request.subject,
        body_md=request.body_md,
        importance=request.importance,
        ack_required=request.ack_required,
        attachments=[],
        archive_document=hostile_document,
        document_sha256=hashlib.sha256(hostile_document.encode()).hexdigest(),
        created_ts=BASE_TIME,
        lease_token="hostile-lease",
        lease_fence=1,
        lease_expires_ts=BASE_TIME + timedelta(minutes=1),
        attempt_count=1,
        next_attempt_ts=BASE_TIME,
        last_attempt_ts=BASE_TIME,
    )
    async with get_immediate_session() as session:
        session.add(delivery)
        await session.flush()
        for ordinal, recipient in enumerate(request.recipients):
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
        await session.commit()

    publish_calls = 0

    async def forbidden_publish(*args: Any, **kwargs: Any) -> Any:
        nonlocal publish_calls
        del args, kwargs
        publish_calls += 1
        raise AssertionError("Git publication must not run for a divergent archive")

    monkeypatch.setattr(delivery_module, "publish_message_delivery", forbidden_publish)
    result = await process_claimed_message_delivery(
        MessageDeliveryLease(
            delivery_id=delivery_id,
            token="hostile-lease",
            fence=1,
            expires_ts=BASE_TIME + timedelta(minutes=1),
            attempt_count=1,
        ),
        settings=get_settings(),
        now=BASE_TIME,
    )
    assert result.status == "quarantined"
    assert "archive_document_snapshot_mismatch" in (result.error or "")
    assert publish_calls == 0
    assert await _row_count(Message) == 0


@pytest.mark.asyncio
async def test_bcc_order_is_committed_without_disclosing_identity(isolated_env: Any) -> None:
    seeded = await _seed_identities()
    accepted = await accept_message_delivery(
        seeded.request("bcc-confidentiality"),
        now=BASE_TIME,
    )
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, accepted.delivery_id)
        assert delivery is not None
        assert seeded.bcc.name not in delivery.archive_document
        assert '"bcc":{"count":1,"ordered_snapshot_sha256":' in delivery.archive_document
        rows = await session.execute(
            select(MessageDeliveryRecipient)
            .where(
                col(MessageDeliveryRecipient.delivery_id) == accepted.delivery_id
            )
            .order_by(col(MessageDeliveryRecipient.ordinal))
        )
        ordered = list(rows.scalars())
        assert [(row.ordinal, row.kind, row.agent_name_snapshot) for row in ordered] == [
            (0, "to", seeded.to.name),
            (1, "cc", seeded.cc.name),
            (2, "bcc", seeded.bcc.name),
        ]


@pytest.mark.asyncio
async def test_stale_identity_snapshot_is_rejected_before_intent(isolated_env: Any) -> None:
    seeded = await _seed_identities()
    recreated_sender = replace(seeded.sender, generation="f" * 64)
    request = replace(
        seeded.request("stale-identity"),
        sender=recreated_sender,
        actor=DeliveryActorSnapshot.agent(recreated_sender),
    )
    with pytest.raises(MessageDeliveryValidationError) as captured:
        await accept_message_delivery(request, now=BASE_TIME)
    assert captured.value.code == "sender_lifetime_invalid"
    assert await _row_count(MessageDelivery) == 0


@pytest.mark.asyncio
async def test_recipient_retirement_quarantines_before_claim(isolated_env: Any) -> None:
    seeded = await _seed_identities()
    accepted = await accept_message_delivery(
        seeded.request("retired-recipient"),
        now=BASE_TIME,
    )
    async with get_immediate_session() as session:
        recipient = await session.get(Agent, seeded.to.agent_id)
        assert recipient is not None
        recipient.retired_at = BASE_TIME
        session.add(recipient)
        await session.commit()

    with pytest.raises(MessageDeliveryTerminalError):
        await claim_message_delivery(
            accepted.delivery_id,
            now=BASE_TIME + timedelta(seconds=1),
        )
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, accepted.delivery_id)
        assert delivery is not None
        assert delivery.state == "quarantined"
    assert await _row_count(Message) == 0


@pytest.mark.asyncio
async def test_ui_session_revocation_quarantines_pending_delivery(isolated_env: Any) -> None:
    seeded = await _seed_identities()
    async with get_immediate_session() as session:
        user = UiUser(
            username="operator-user",
            password_hash="not-used-in-service-test",
            role="admin",
            session_epoch=1,
        )
        session.add(user)
        await session.flush()
        assert user.id is not None
        session.add(
            UiProjectAssignment(
                user_id=user.id,
                project_id=seeded.target.project_id,
                role="operator",
            )
        )
        await session.commit()
    actor = DeliveryActorSnapshot.ui_user(
        user_id=user.id,
        username=user.username,
        generation=user.session_generation,
        epoch=user.session_epoch,
        source_project=seeded.source,
    )
    accepted = await accept_message_delivery(
        seeded.request("ui-revocation", actor=actor),
        now=BASE_TIME,
    )

    async with get_immediate_session() as session:
        current_user = await session.get(UiUser, user.id)
        assert current_user is not None
        current_user.session_epoch += 1
        session.add(current_user)
        await session.commit()
    with pytest.raises(MessageDeliveryTerminalError):
        await claim_message_delivery(
            accepted.delivery_id,
            now=BASE_TIME + timedelta(seconds=1),
        )


@pytest.mark.asyncio
async def test_source_operator_can_send_cross_project_thread_reply(
    isolated_env: Any,
) -> None:
    seeded = await _seed_identities(cross_project=True)
    await _set_route_status(seeded, seeded.to, "blocked")
    await _seed_inbound_message(
        seeded,
        thread_id="operator-reply",
        include_recipient=False,
    )
    async with get_immediate_session() as session:
        user = UiUser(
            username="source-operator",
            password_hash="not-used-in-service-test",
            role="member",
            session_epoch=1,
        )
        session.add(user)
        await session.flush()
        assert user.id is not None
        session.add(
            UiProjectAssignment(
                user_id=user.id,
                project_id=seeded.source.project_id,
                role="operator",
            )
        )
        await session.commit()
    actor = DeliveryActorSnapshot.ui_user(
        user_id=user.id,
        username=user.username,
        generation=user.session_generation,
        epoch=user.session_epoch,
        source_project=seeded.source,
    )
    request = replace(
        seeded.request("operator-cross-project-reply", actor=actor),
        purpose="reply",
        thread_id="operator-reply",
        recipients=(DeliveryRecipientSnapshot(kind="to", agent=seeded.to),),
    )

    accepted = await accept_message_delivery(request, now=BASE_TIME)
    lease = await claim_message_delivery(accepted.delivery_id, now=BASE_TIME)
    assert lease is not None
    result = await process_claimed_message_delivery(
        lease,
        settings=get_settings(),
        now=BASE_TIME,
    )
    assert result.status == "published"
    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, accepted.delivery_id)
        assert delivery is not None
        assert delivery.actor_project_id_snapshot == seeded.source.project_id
        assert delivery.actor_project_generation_snapshot == seeded.source.generation


@pytest.mark.asyncio
async def test_source_operator_cannot_initiate_normal_message(isolated_env: Any) -> None:
    seeded = await _seed_identities(cross_project=True)
    async with get_immediate_session() as session:
        user = UiUser(
            username="message-operator",
            password_hash="not-used-in-service-test",
            role="member",
            session_epoch=1,
        )
        session.add(user)
        await session.flush()
        assert user.id is not None
        session.add(
            UiProjectAssignment(
                user_id=user.id,
                project_id=seeded.source.project_id,
                role="operator",
            )
        )
        await session.commit()
    actor = DeliveryActorSnapshot.ui_user(
        user_id=user.id,
        username=user.username,
        generation=user.session_generation,
        epoch=user.session_epoch,
        source_project=seeded.source,
    )

    with pytest.raises(MessageDeliveryValidationError) as denied:
        await accept_message_delivery(
            seeded.request("operator-normal-message", actor=actor),
            now=BASE_TIME,
        )
    assert denied.value.code == "ui_actor_admin_required"


@pytest.mark.asyncio
async def test_unnormalized_attachments_fail_closed(isolated_env: Any) -> None:
    seeded = await _seed_identities()
    request = replace(
        seeded.request("unsafe-attachment"),
        attachments=({"path": "../../secret.png", "policy": "file"},),
    )
    with pytest.raises(MessageDeliveryValidationError) as captured:
        await accept_message_delivery(request, now=BASE_TIME)
    assert captured.value.code == "attachments_require_inline_normalization"
    assert await _row_count(MessageDelivery) == 0


@pytest.mark.asyncio
async def test_cancellation_leaves_fenced_lease_for_recovery(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed_identities()
    accepted = await accept_message_delivery(
        seeded.request("cancelled-worker"),
        now=BASE_TIME,
    )
    lease = await claim_message_delivery(accepted.delivery_id, now=BASE_TIME)
    assert lease is not None

    from mcp_agent_mail import delivery as delivery_module

    started = asyncio.Event()
    never_complete = asyncio.Event()

    async def blocked_publish(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        started.set()
        await never_complete.wait()

    monkeypatch.setattr(delivery_module, "publish_message_delivery", blocked_publish)
    task = asyncio.create_task(
        process_claimed_message_delivery(
            lease,
            settings=get_settings(),
            now=BASE_TIME,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with get_immediate_session() as session:
        delivery = await session.get(MessageDelivery, accepted.delivery_id)
        assert delivery is not None
        assert delivery.state == "pending"
        assert delivery.lease_token == lease.token
        assert delivery.lease_fence == lease.fence
    assert await _row_count(Message) == 0
