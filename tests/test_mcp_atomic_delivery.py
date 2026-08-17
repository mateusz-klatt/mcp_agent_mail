"""End-to-end MCP coverage for the immutable message-delivery boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from fastmcp import Client
from sqlalchemy import func, select

import mcp_agent_mail.delivery as delivery_service
import mcp_agent_mail.storage as storage
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_immediate_session
from mcp_agent_mail.models import AgentLink, Message, MessageDelivery, Project
from mcp_agent_mail.storage import MessageDeliveryPendingError
from tests.keys import pkey

ATOMIC_SENDER = "codex-wsl-atomic-1"
ATOMIC_RECIPIENT = "codex-wsl-atomic-2"
ATOMIC_OBSERVER = "codex-wsl-atomic-3"


async def _register_open_agent(
    client: Client[Any],
    project_key: str,
    name: str,
) -> dict[str, Any]:
    result = await client.call_tool(
        "register_agent",
        {
            "project_key": project_key,
            "program": "pytest",
            "model": "test",
            "name": name,
        },
    )
    await client.call_tool(
        "set_contact_policy",
        {
            "project_key": project_key,
            "agent_name": name,
            "policy": "open",
        },
    )
    return dict(result.data)


async def _count_rows(model: type[Any]) -> int:
    async with get_immediate_session() as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_send_stays_invisible_until_retry_finalizes_verified_git(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_key = pkey("mcp/atomic-pending")
    server = build_mcp_server()

    async def legacy_call_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("legacy message artifact path was called")

    monkeypatch.setattr(storage, "process_attachments", legacy_call_forbidden)
    monkeypatch.setattr(storage, "write_message_bundle", legacy_call_forbidden)
    original_publisher = delivery_service.publish_message_delivery
    wake_events: list[tuple[str, str]] = []

    def record_agent_wake(
        project_slug: str,
        _project_generation: str,
        agent_name: str,
        _agent_generation: str,
        _payload: Any,
    ) -> None:
        wake_events.append((project_slug, agent_name))

    monkeypatch.setattr(delivery_service.hub, "publish", record_agent_wake)

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        await _register_open_agent(client, project_key, ATOMIC_SENDER)
        await _register_open_agent(client, project_key, ATOMIC_RECIPIENT)

        async def transient_publisher(
            _archive: Any,
            delivery_id: str,
            *_args: Any,
            **_kwargs: Any,
        ) -> Any:
            raise MessageDeliveryPendingError(delivery_id, "injected transient failure")

        monkeypatch.setattr(
            delivery_service,
            "publish_message_delivery",
            transient_publisher,
        )
        first = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": ATOMIC_SENDER,
                "to": [ATOMIC_RECIPIENT],
                "subject": "Atomic boundary",
                "body_md": "not visible before the receipt",
                "idempotency_key": "mcp-pending-1",
            },
        )
        first_delivery = first.data["deliveries"][0]["delivery"]
        assert first_delivery["status"] == "pending"
        assert first.data["deliveries"][0]["message"] is None
        assert await _count_rows(MessageDelivery) == 1
        assert await _count_rows(Message) == 0
        assert wake_events == []

        async with get_immediate_session() as session:
            intent = await session.get(MessageDelivery, first_delivery["id"])
            assert intent is not None
            intent.next_attempt_ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                seconds=1
            )
            session.add(intent)
            await session.commit()

        monkeypatch.setattr(
            delivery_service,
            "publish_message_delivery",
            original_publisher,
        )
        recovered = await client.call_tool(
            "get_message_delivery",
            {
                "project_key": project_key,
                "agent_name": ATOMIC_SENDER,
                "delivery_id": first_delivery["id"],
                "retry_pending": True,
            },
        )
        assert recovered.data["delivery"]["status"] == "published"
        assert recovered.data["message"]["delivery_id"] == first_delivery["id"]
        assert await _count_rows(MessageDelivery) == 1
        assert await _count_rows(Message) == 1
        assert wake_events == [("mcp-atomic-pending", ATOMIC_RECIPIENT)]

        async with get_immediate_session() as session:
            finalized = await session.get(MessageDelivery, first_delivery["id"])
            assert finalized is not None
            assert finalized.message_id == recovered.data["message"]["id"]
            assert finalized.archive_commit_sha == recovered.data["delivery"]["commit_sha"]
            assert finalized.archive_blob_sha is not None
            assert finalized.archive_relative_path == (
                f"projects/mcp-atomic-pending/message_deliveries/{first_delivery['id']}.md"
            )

        repeated = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": ATOMIC_SENDER,
                "to": [ATOMIC_RECIPIENT],
                "subject": "Atomic boundary",
                "body_md": "not visible before the receipt",
                "idempotency_key": "mcp-pending-1",
            },
        )
        repeated_delivery = repeated.data["deliveries"][0]
        assert repeated_delivery["delivery"]["id"] == first_delivery["id"]
        assert repeated_delivery["delivery"]["reused"] is True
        assert repeated_delivery["message"]["id"] == recovered.data["message"]["id"]
        assert await _count_rows(Message) == 1
        assert wake_events == [("mcp-atomic-pending", ATOMIC_RECIPIENT)]


@pytest.mark.asyncio
async def test_local_reply_uses_atomic_parent_edge_and_distinct_idempotency(
    isolated_env: Any,
) -> None:
    project_key = pkey("mcp/atomic-reply")
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        await _register_open_agent(client, project_key, ATOMIC_SENDER)
        await _register_open_agent(client, project_key, ATOMIC_RECIPIENT)

        sent = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": ATOMIC_SENDER,
                "to": [ATOMIC_RECIPIENT],
                "subject": "Plan",
                "body_md": "body",
                "idempotency_key": "mcp-reply-parent",
            },
        )
        original = sent.data["deliveries"][0]["message"]
        with pytest.raises(Exception, match="idempotency_key"):
            await client.call_tool(
                "reply_message",
                {
                    "project_key": project_key,
                    "message_id": original["id"],
                    "sender_name": ATOMIC_RECIPIENT,
                    "body_md": "missing operation key",
                },
            )
        assert await _count_rows(Message) == 1

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": project_key,
                "message_id": original["id"],
                "sender_name": ATOMIC_RECIPIENT,
                "body_md": "ack",
                "idempotency_key": "mcp-reply-child",
            },
        )
        child = reply.data["deliveries"][0]["message"]
        assert child["reply_to"] == original["id"]
        assert child["thread_id"] == str(original["id"])
        assert child["delivery_id"] != original["delivery_id"]

        async with get_immediate_session() as session:
            stored = await session.get(Message, child["id"])
            assert stored is not None
            assert stored.reply_to == original["id"]


@pytest.mark.asyncio
async def test_repeated_pending_contact_request_reuses_one_event_intent(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_key = pkey("mcp/contact-source")
    target_key = pkey("mcp/contact-target")
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": source_key})
        await client.call_tool("ensure_project", {"human_key": target_key})
        await _register_open_agent(client, source_key, ATOMIC_SENDER)
        await _register_open_agent(client, target_key, ATOMIC_RECIPIENT)

        async def transient_publisher(
            _archive: Any,
            delivery_id: str,
            *_args: Any,
            **_kwargs: Any,
        ) -> Any:
            raise MessageDeliveryPendingError(delivery_id, "injected contact failure")

        monkeypatch.setattr(
            delivery_service,
            "publish_message_delivery",
            transient_publisher,
        )
        first = await client.call_tool(
            "request_contact",
            {
                "project_key": source_key,
                "from_agent": ATOMIC_SENDER,
                "to_agent": ATOMIC_RECIPIENT,
                "to_project": target_key,
                "reason": "first immutable reason",
            },
        )
        second = await client.call_tool(
            "request_contact",
            {
                "project_key": source_key,
                "from_agent": ATOMIC_SENDER,
                "to_agent": ATOMIC_RECIPIENT,
                "to_project": target_key,
                "reason": "must not fork the active pending event",
            },
        )

        first_delivery = first.data["notification_message"]["delivery"]
        second_delivery = second.data["notification_message"]["delivery"]
        assert first_delivery["id"] == second_delivery["id"]
        assert first_delivery["status"] == "pending"
        assert second_delivery["reused"] is True
        assert await _count_rows(MessageDelivery) == 1
        assert await _count_rows(Message) == 0

        async with get_immediate_session() as session:
            intent = (
                await session.execute(select(MessageDelivery))
            ).scalar_one()
            assert intent.delivery_kind == "contact_request"
            assert intent.body_md == "first immutable reason"


@pytest.mark.asyncio
async def test_send_requires_idempotency_and_rejects_attachment_options_before_intent(
    isolated_env: Any,
) -> None:
    project_key = pkey("mcp/atomic-validation")
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        await _register_open_agent(client, project_key, ATOMIC_SENDER)
        await _register_open_agent(client, project_key, ATOMIC_RECIPIENT)

        with pytest.raises(Exception, match="idempotency_key"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": ATOMIC_SENDER,
                    "to": [ATOMIC_RECIPIENT],
                    "subject": "missing key",
                    "body_md": "must fail",
                },
            )
        with pytest.raises(Exception, match="attachment_paths and convert_images"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": ATOMIC_SENDER,
                    "to": [ATOMIC_RECIPIENT],
                    "subject": "attachment",
                    "body_md": "must fail",
                    "idempotency_key": "mcp-attachment-reject",
                    "attachment_paths": [],
                },
            )
        with pytest.raises(Exception, match="attachment_paths and convert_images"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": ATOMIC_SENDER,
                    "to": [ATOMIC_RECIPIENT],
                    "subject": "conversion override",
                    "body_md": "must fail",
                    "idempotency_key": "mcp-convert-reject",
                    "convert_images": False,
                },
            )
        assert await _count_rows(MessageDelivery) == 0
        assert await _count_rows(Message) == 0


@pytest.mark.asyncio
async def test_send_rejects_an_unauthenticated_sender_before_accepting_intent(
    isolated_env: Any,
) -> None:
    project_key = pkey("mcp/atomic-auth")
    server = build_mcp_server()
    async with Client(server) as setup_client:
        await setup_client.call_tool("ensure_project", {"human_key": project_key})
        sender = await _register_open_agent(setup_client, project_key, ATOMIC_SENDER)
        await _register_open_agent(setup_client, project_key, ATOMIC_RECIPIENT)

    async with Client(server) as unbound_client:
        with pytest.raises(Exception, match="Invalid registration_token"):
            await unbound_client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": ATOMIC_SENDER,
                    "registration_token": "definitely-wrong",
                    "to": [ATOMIC_RECIPIENT],
                    "subject": "unauthorized",
                    "body_md": "must not persist",
                    "idempotency_key": "mcp-auth-rejected",
                },
            )
        assert await _count_rows(MessageDelivery) == 0

        accepted = await unbound_client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": ATOMIC_SENDER,
                "registration_token": sender["registration_token"],
                "to": [ATOMIC_RECIPIENT],
                "subject": "authorized",
                "body_md": "persist this",
                "idempotency_key": "mcp-auth-accepted",
            },
        )
        assert accepted.data["deliveries"][0]["delivery"]["status"] == "published"


@pytest.mark.asyncio
async def test_cross_project_send_preserves_source_lifetime_and_status_authorization(
    isolated_env: Any,
) -> None:
    source_key = pkey("mcp/send-source")
    target_key = pkey("mcp/send-target")
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": source_key})
        target = await client.call_tool("ensure_project", {"human_key": target_key})
        await _register_open_agent(client, source_key, ATOMIC_SENDER)
        await _register_open_agent(client, target_key, ATOMIC_RECIPIENT)
        await _register_open_agent(client, target_key, ATOMIC_OBSERVER)

        await client.call_tool(
            "request_contact",
            {
                "project_key": source_key,
                "from_agent": ATOMIC_SENDER,
                "to_agent": ATOMIC_RECIPIENT,
                "to_project": target_key,
                "reason": "allow atomic cross-project delivery",
            },
        )
        await client.call_tool(
            "respond_contact",
            {
                "project_key": target_key,
                "to_agent": ATOMIC_RECIPIENT,
                "from_agent": ATOMIC_SENDER,
                "from_project": source_key,
                "accept": True,
            },
        )

        sent = await client.call_tool(
            "send_message",
            {
                "project_key": source_key,
                "sender_name": ATOMIC_SENDER,
                "to": [f"project:{target.data['slug']}#{ATOMIC_RECIPIENT}"],
                "subject": "cross-project",
                "body_md": "source identity must survive",
                "idempotency_key": "mcp-cross-project-1",
            },
        )
        delivered = sent.data["deliveries"][0]
        assert delivered["project"] == target_key
        assert delivered["delivery"]["status"] == "published"
        assert delivered["message"]["from"] == ATOMIC_SENDER
        assert delivered["message"]["from_project"] == source_key
        async with get_immediate_session() as session:
            intent = await session.get(MessageDelivery, delivered["delivery"]["id"])
            assert intent is not None
            assert intent.sender_project_id_snapshot != intent.project_id
            assert intent.sender_name_snapshot == ATOMIC_SENDER

        sender_status = await client.call_tool(
            "get_message_delivery",
            {
                "project_key": source_key,
                "agent_name": ATOMIC_SENDER,
                "delivery_id": delivered["delivery"]["id"],
            },
        )
        recipient_status = await client.call_tool(
            "get_message_delivery",
            {
                "project_key": target_key,
                "agent_name": ATOMIC_RECIPIENT,
                "delivery_id": delivered["delivery"]["id"],
            },
        )
        assert sender_status.data["project"] == target_key
        assert recipient_status.data["project"] == target_key
        assert sender_status.data["delivery"]["status"] == "published"
        assert recipient_status.data["delivery"]["status"] == "published"
        with pytest.raises(Exception, match="was not found"):
            await client.call_tool(
                "get_message_delivery",
                {
                    "project_key": target_key,
                    "agent_name": ATOMIC_OBSERVER,
                    "delivery_id": delivered["delivery"]["id"],
                },
            )


@pytest.mark.asyncio
async def test_bcc_is_private_in_git_but_receives_private_inbox_copy(
    isolated_env: Any,
) -> None:
    project_key = pkey("mcp/atomic-bcc")
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        await _register_open_agent(client, project_key, ATOMIC_SENDER)
        await _register_open_agent(client, project_key, ATOMIC_RECIPIENT)
        await _register_open_agent(client, project_key, ATOMIC_OBSERVER)

        sent = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": ATOMIC_SENDER,
                "to": [ATOMIC_RECIPIENT],
                "bcc": [ATOMIC_OBSERVER],
                "subject": "blind copy",
                "body_md": "private routing",
                "idempotency_key": "mcp-bcc-1",
            },
        )
        delivery_id = sent.data["deliveries"][0]["delivery"]["id"]
        async with get_immediate_session() as session:
            intent = await session.get(MessageDelivery, delivery_id)
            assert intent is not None
            assert ATOMIC_OBSERVER not in intent.archive_document
            assert '"bcc":{"count":1' in intent.archive_document

        inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": project_key,
                "agent_name": ATOMIC_OBSERVER,
                "limit": 10,
            },
        )
        assert inbox.structured_content is not None
        items = inbox.structured_content["result"]
        assert any(item["delivery_id"] == delivery_id for item in items)


@pytest.mark.asyncio
async def test_cross_project_reply_uses_thread_route_without_reverse_contact_grant(
    isolated_env: Any,
) -> None:
    source_key = pkey("mcp/reply-source")
    target_key = pkey("mcp/reply-target")
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": source_key})
        target = await client.call_tool("ensure_project", {"human_key": target_key})
        await _register_open_agent(client, source_key, ATOMIC_SENDER)
        await _register_open_agent(client, target_key, ATOMIC_RECIPIENT)
        await client.call_tool(
            "request_contact",
            {
                "project_key": source_key,
                "from_agent": ATOMIC_SENDER,
                "to_agent": ATOMIC_RECIPIENT,
                "to_project": target_key,
                "reason": "one-way contact grant",
            },
        )
        await client.call_tool(
            "respond_contact",
            {
                "project_key": target_key,
                "to_agent": ATOMIC_RECIPIENT,
                "from_agent": ATOMIC_SENDER,
                "from_project": source_key,
                "accept": True,
            },
        )
        sent = await client.call_tool(
            "send_message",
            {
                "project_key": source_key,
                "sender_name": ATOMIC_SENDER,
                "to": [f"project:{target.data['slug']}#{ATOMIC_RECIPIENT}"],
                "subject": "external thread",
                "body_md": "please reply",
                "idempotency_key": "mcp-external-thread-parent",
            },
        )
        original = sent.data["deliveries"][0]["message"]

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": target_key,
                "message_id": original["id"],
                "sender_name": ATOMIC_RECIPIENT,
                "body_md": "thread-scoped return",
                "idempotency_key": "mcp-external-thread-reply",
            },
        )
        returned = reply.data["deliveries"][0]
        assert returned["project"] == source_key
        assert returned["delivery"]["status"] == "published"
        assert returned["message"]["thread_id"] == str(original["id"])
        # The numeric original belongs to the other project and must never be
        # bound as a target-local reply edge, even if its integer looks valid.
        assert returned["message"]["reply_to"] is None

        async with get_immediate_session() as session:
            intent = await session.get(MessageDelivery, returned["delivery"]["id"])
            assert intent is not None
            assert intent.delivery_kind == "reply"
            assert intent.reply_to_message_id is None
            source_project = (
                await session.execute(
                    select(Project).where(cast(Any, Project.human_key == source_key))
                )
            ).scalar_one()
            target_project = (
                await session.execute(
                    select(Project).where(cast(Any, Project.human_key == target_key))
                )
            ).scalar_one()
            reverse_count = (
                await session.execute(
                    select(func.count())
                    .select_from(AgentLink)
                    .where(
                        AgentLink.a_project_id == target_project.id,
                        AgentLink.b_project_id == source_project.id,
                    )
                )
            ).scalar_one()
            assert reverse_count == 0
