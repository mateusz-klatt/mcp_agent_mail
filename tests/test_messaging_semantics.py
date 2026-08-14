from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import text

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_db_health_status, get_session
from tests.keys import pkey


async def _expire_contact_link(
    project_key: str,
    from_agent: str,
    to_agent: str,
    *,
    target_project_key: str | None = None,
) -> None:
    target_key = target_project_key or project_key
    expired = datetime.now(UTC) - timedelta(minutes=5)
    expired_db = expired.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
    async with get_session() as session:
        await session.execute(
            text(
                """
                UPDATE agent_links
                SET expires_ts = :expired, updated_ts = :expired
                WHERE a_project_id = (SELECT id FROM projects WHERE human_key = :project_key)
                  AND a_agent_id = (
                      SELECT a.id
                      FROM agents a
                      JOIN projects p ON p.id = a.project_id
                      WHERE p.human_key = :project_key AND a.name = :from_agent
                  )
                  AND b_project_id = (SELECT id FROM projects WHERE human_key = :target_project_key)
                  AND b_agent_id = (
                      SELECT a.id
                      FROM agents a
                      JOIN projects p ON p.id = a.project_id
                      WHERE p.human_key = :target_project_key AND a.name = :to_agent
                  )
                """
            ),
            {
                "expired": expired_db,
                "project_key": project_key,
                "from_agent": from_agent,
                "target_project_key": target_key,
                "to_agent": to_agent,
            },
        )
        await session.commit()


@pytest.mark.asyncio
async def test_reply_message_inherits_thread_and_subject_prefix(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "codex-wsl-semantics-2"},
        )
        m1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "codex-wsl-semantics-2",
                "to": ["codex-wsl-semantics-2"],
                "subject": "Plan",
                "body_md": "body",
                "idempotency_key": "semantics-reply-parent",
            },
        )
        msg = (m1.data.get("deliveries") or [{}])[0].get("message", {})
        orig_id = int(msg.get("id"))
        # Reply
        r = await client.call_tool(
            "reply_message",
            {
                "project_key": "Backend",
                "message_id": orig_id,
                "sender_name": "codex-wsl-semantics-2",
                "body_md": "ack",
                "idempotency_key": "semantics-reply-child",
            },
        )
        rdata = r.data
        expected_thread = msg.get("thread_id") or str(orig_id)
        assert rdata.get("thread_id") == expected_thread
        assert str(rdata.get("reply_to")) == str(orig_id)
        # Subject on delivery payload should be prefixed
        deliveries = rdata.get("deliveries") or []
        assert deliveries
        subj = deliveries[0].get("message", {}).get("subject", "")
        assert subj.lower().startswith("re:")


@pytest.mark.asyncio
async def test_reply_message_explicit_empty_to_does_not_restore_default_recipient(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("reply-empty-to")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("reply-empty-to"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        original = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("reply-empty-to"),
                "sender_name": "codex-wsl-semantics-2",
                "to": ["codex-wsl-semantics-2"],
                "subject": "Plan",
                "body_md": "body",
                "idempotency_key": "semantics-empty-reply-parent",
            },
        )
        original_payload = (original.data.get("deliveries") or [{}])[0].get("message", {})
        original_id = int(original_payload["id"])

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": pkey("reply-empty-to"),
                "message_id": original_id,
                "sender_name": "codex-wsl-semantics-2",
                "to": [],
                "body_md": "no recipients on purpose",
                "idempotency_key": "semantics-empty-reply-child",
            },
        )
        assert reply.data["count"] == 0
        assert reply.data["deliveries"] == []
        assert reply.data["thread_id"] == (original_payload.get("thread_id") or str(original_id))
        assert reply.data["reply_to"] == original_id


@pytest.mark.asyncio
async def test_mark_read_then_ack_updates_state(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "codex-wsl-semantics-1"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "codex-wsl-semantics-3"},
        )
        m1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "codex-wsl-semantics-1",
                "to": ["codex-wsl-semantics-3"],
                "subject": "AckPlease",
                "body_md": "hello",
                "ack_required": True,
                "idempotency_key": "semantics-mark-read-seed",
            },
        )
        msg = (m1.data.get("deliveries") or [{}])[0].get("message", {})
        mid = int(msg.get("id"))

        mr = await client.call_tool(
            "mark_message_read",
            {"project_key": "Backend", "agent_name": "codex-wsl-semantics-3", "message_id": mid},
        )
        assert mr.data.get("read") is True and isinstance(mr.data.get("read_at"), str)

        ack = await client.call_tool(
            "acknowledge_message",
            {"project_key": "Backend", "agent_name": "codex-wsl-semantics-3", "message_id": mid},
        )
        assert ack.data.get("acknowledged") is True
        assert isinstance(ack.data.get("acknowledged_at"), str)
        assert isinstance(ack.data.get("read_at"), str)


@pytest.mark.asyncio
async def test_acknowledge_idempotent_multiple_calls(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "codex-wsl-semantics-1"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "codex-wsl-semantics-3"},
        )
        m1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "codex-wsl-semantics-1",
                "to": ["codex-wsl-semantics-3"],
                "subject": "AckTwice",
                "body_md": "hello",
                "ack_required": True,
                "idempotency_key": "semantics-ack-twice-seed",
            },
        )
        msg = (m1.data.get("deliveries") or [{}])[0].get("message", {})
        mid = int(msg.get("id"))

        first = await client.call_tool(
            "acknowledge_message",
            {"project_key": "Backend", "agent_name": "codex-wsl-semantics-3", "message_id": mid},
        )
        first_ack_at = first.data.get("acknowledged_at")
        assert first.data.get("acknowledged") is True and isinstance(first_ack_at, str)

        second = await client.call_tool(
            "acknowledge_message",
            {"project_key": "Backend", "agent_name": "codex-wsl-semantics-3", "message_id": mid},
        )
        # Timestamps should remain the same (idempotent)
        assert second.data.get("acknowledged_at") == first_ack_at


@pytest.mark.asyncio
async def test_send_message_requires_sender_token_across_sessions(isolated_env):
    """A fresh session cannot impersonate an existing sender without sender_token."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": pkey("security/spoof-send")})
        sender = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/spoof-send"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        sender_token = sender.data["registration_token"]

    async with Client(server) as attacker_client:
        with pytest.raises(ToolError) as exc_info:
            await attacker_client.call_tool(
                "send_message",
                {
                    "project_key": pkey("security/spoof-send"),
                    "sender_name": "codex-wsl-semantics-1",
                    "to": ["codex-wsl-semantics-1"],
                    "subject": "Forged",
                    "body_md": "This should fail",
                    "idempotency_key": "security-spoof-forged",
                },
            )
        assert "sender_token" in str(exc_info.value)

    async with Client(server) as sender_client:
        result = await sender_client.call_tool(
            "send_message",
                {
                    "project_key": pkey("security/spoof-send"),
                    "sender_name": "codex-wsl-semantics-1",
                    "sender_token": sender_token,
                    "to": ["codex-wsl-semantics-1"],
                    "subject": "Legit",
                    "body_md": "This should succeed",
                    "idempotency_key": "security-spoof-legit",
                },
        )
    assert result.data["verified_sender"] is True
    assert result.data["count"] == 1


@pytest.mark.asyncio
async def test_send_message_auto_contact_requests_pending_approval_without_target_auth(isolated_env):
    """auto_contact_if_blocked should create a pending request, not pretend to auto-approve."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": pkey("security/auto-contact-pending")})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-pending"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-pending"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]

    async with Client(server) as sender_client:
        with pytest.raises(ToolError) as exc_info:
            await sender_client.call_tool(
                "send_message",
                {
                    "project_key": pkey("security/auto-contact-pending"),
                    "sender_name": "codex-wsl-semantics-1",
                    "sender_token": green_token,
                    "to": ["codex-wsl-semantics-2"],
                    "subject": "Need approval",
                    "body_md": "please let me in",
                    "auto_contact_if_blocked": True,
                    "idempotency_key": "security-auto-contact-pending",
                },
            )
        assert "Pending contact requests were created for: codex-wsl-semantics-2" in str(exc_info.value)

        contacts = await sender_client.call_tool(
            "list_contacts",
            {
                "project_key": pkey("security/auto-contact-pending"),
                "agent_name": "codex-wsl-semantics-1",
                "registration_token": green_token,
            },
        )
        contact_items = contacts.structured_content["result"]
        assert any(item["to"] == "codex-wsl-semantics-2" and item["status"] == "pending" for item in contact_items)

    async with Client(server) as recipient_client:
        inbox = await recipient_client.call_tool(
            "fetch_inbox",
            {
                "project_key": pkey("security/auto-contact-pending"),
                "agent_name": "codex-wsl-semantics-2",
                "registration_token": blue_token,
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        assert any(item["subject"] == "Contact request from codex-wsl-semantics-1" for item in messages)


@pytest.mark.asyncio
async def test_send_message_explicit_false_disables_local_auto_contact(isolated_env):
    """Explicit false should override the server default and avoid creating contact requests."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": pkey("security/auto-contact-disabled")})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-disabled"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-disabled"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]

    async with Client(server) as sender_client:
        with pytest.raises(ToolError):
            await sender_client.call_tool(
                "send_message",
                {
                    "project_key": pkey("security/auto-contact-disabled"),
                    "sender_name": "codex-wsl-semantics-1",
                    "sender_token": green_token,
                    "to": ["codex-wsl-semantics-2"],
                    "subject": "No auto contact",
                    "body_md": "stay blocked",
                    "auto_contact_if_blocked": False,
                    "idempotency_key": "security-auto-contact-disabled",
                },
            )

        contacts = await sender_client.call_tool(
            "list_contacts",
            {
                "project_key": pkey("security/auto-contact-disabled"),
                "agent_name": "codex-wsl-semantics-1",
                "registration_token": green_token,
            },
        )
        contact_items = contacts.structured_content["result"]
        assert not any(
            item["to"] == "codex-wsl-semantics-2" and item["status"] == "pending"
            for item in contact_items
        )

    async with Client(server) as recipient_client:
        inbox = await recipient_client.call_tool(
            "fetch_inbox",
            {
                "project_key": pkey("security/auto-contact-disabled"),
                "agent_name": "codex-wsl-semantics-2",
                "registration_token": blue_token,
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        assert not any(item["subject"] == "Contact request from codex-wsl-semantics-1" for item in messages)


@pytest.mark.asyncio
async def test_send_message_auto_contact_auto_approves_when_target_is_authenticated_in_session(isolated_env):
    """Same-session authenticated agents can still use the one-step auto-approval path."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("security/auto-contact-approved")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-approved"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-approved"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )

        result = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("security/auto-contact-approved"),
                "sender_name": "codex-wsl-semantics-1",
                "to": ["codex-wsl-semantics-2"],
                "subject": "Auto approved",
                "body_md": "same session works",
                "auto_contact_if_blocked": True,
                "idempotency_key": "security-auto-contact-approved",
            },
        )
        assert result.data["count"] == 1

        contacts = await client.call_tool(
            "list_contacts",
            {
                "project_key": pkey("security/auto-contact-approved"),
                "agent_name": "codex-wsl-semantics-1",
            },
        )
        contact_items = contacts.structured_content["result"]
        assert any(item["to"] == "codex-wsl-semantics-2" and item["status"] == "approved" for item in contact_items)


@pytest.mark.asyncio
async def test_send_message_auto_contact_requests_cross_project_approval_without_target_auth(isolated_env):
    """Cross-project auto-contact should create a pending request when only the sender is authenticated."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": pkey("security/auto-contact-xproj-backend")})
        await bootstrap_client.call_tool("ensure_project", {"human_key": pkey("security/auto-contact-xproj-frontend")})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-xproj-backend"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-xproj-frontend"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]

    async with Client(server) as sender_client:
        with pytest.raises(ToolError) as exc_info:
            await sender_client.call_tool(
                "send_message",
                {
                    "project_key": pkey("security/auto-contact-xproj-backend"),
                    "sender_name": "codex-wsl-semantics-1",
                    "sender_token": green_token,
                    "to": ["codex-wsl-semantics-2@/security/auto-contact-xproj-frontend"],
                    "subject": "Need cross-project approval",
                    "body_md": "please link us",
                    "auto_contact_if_blocked": True,
                    "idempotency_key": "security-auto-contact-xproj-pending",
                },
            )
        assert (
            "pending external contact requests were created for "
            "codex-wsl-semantics-2@/security/auto-contact-xproj-frontend"
        ) in str(exc_info.value)

        contacts = await sender_client.call_tool(
            "list_contacts",
            {
                "project_key": pkey("security/auto-contact-xproj-backend"),
                "agent_name": "codex-wsl-semantics-1",
                "registration_token": green_token,
            },
        )
        contact_items = contacts.structured_content["result"]
        assert any(item["to"] == "codex-wsl-semantics-2" and item["status"] == "pending" for item in contact_items)

    async with Client(server) as recipient_client:
        inbox = await recipient_client.call_tool(
            "fetch_inbox",
            {
                "project_key": pkey("security/auto-contact-xproj-frontend"),
                "agent_name": "codex-wsl-semantics-2",
                "registration_token": blue_token,
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        assert any(item["subject"] == "Contact request from codex-wsl-semantics-1" for item in messages)


@pytest.mark.asyncio
async def test_send_message_explicit_false_disables_cross_project_auto_contact(isolated_env):
    """Explicit false should prevent the external auto-handshake path from creating requests."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": pkey("security/auto-contact-false-backend")})
        await bootstrap_client.call_tool("ensure_project", {"human_key": pkey("security/auto-contact-false-frontend")})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-false-backend"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-false-frontend"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]

    async with Client(server) as sender_client:
        with pytest.raises(ToolError):
            await sender_client.call_tool(
                "send_message",
                {
                    "project_key": pkey("security/auto-contact-false-backend"),
                    "sender_name": "codex-wsl-semantics-1",
                    "sender_token": green_token,
                    "to": ["codex-wsl-semantics-2@/security/auto-contact-false-frontend"],
                    "subject": "No external auto contact",
                    "body_md": "stay blocked",
                    "auto_contact_if_blocked": False,
                    "idempotency_key": "security-auto-contact-xproj-disabled",
                },
            )

        contacts = await sender_client.call_tool(
            "list_contacts",
            {
                "project_key": pkey("security/auto-contact-false-backend"),
                "agent_name": "codex-wsl-semantics-1",
                "registration_token": green_token,
            },
        )
        contact_items = contacts.structured_content["result"]
        assert not any(
            item["to"] == "codex-wsl-semantics-2" and item["status"] == "pending"
            for item in contact_items
        )

    async with Client(server) as recipient_client:
        inbox = await recipient_client.call_tool(
            "fetch_inbox",
            {
                "project_key": pkey("security/auto-contact-false-frontend"),
                "agent_name": "codex-wsl-semantics-2",
                "registration_token": blue_token,
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        assert not any(item["subject"] == "Contact request from codex-wsl-semantics-1" for item in messages)


@pytest.mark.asyncio
async def test_send_message_cross_project_auto_contact_preserves_recipient_kind(isolated_env):
    """In-session cross-project auto-approval must preserve whether the target was TO/CC/BCC."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("security/auto-contact-kind-backend")})
        await client.call_tool("ensure_project", {"human_key": pkey("security/auto-contact-kind-frontend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-kind-backend"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/auto-contact-kind-frontend"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )

        result = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("security/auto-contact-kind-backend"),
                "sender_name": "codex-wsl-semantics-1",
                "to": ["codex-wsl-semantics-1"],
                "bcc": ["codex-wsl-semantics-2@/security/auto-contact-kind-frontend"],
                "subject": "Cross-project BCC",
                "body_md": "recipient kind must survive auto-approval",
                "auto_contact_if_blocked": True,
                "idempotency_key": "security-auto-contact-kind-bcc",
            },
        )
        assert result.data["count"] == 2

        inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": pkey("security/auto-contact-kind-frontend"),
                "agent_name": "codex-wsl-semantics-2",
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        delivered = next(item for item in messages if item["subject"] == "Cross-project BCC")
        assert delivered["kind"] == "bcc"
        assert delivered["body_md"] == "recipient kind must survive auto-approval"


@pytest.mark.asyncio
async def test_reply_message_enforces_local_contact_policy_for_new_recipient(isolated_env):
    """reply_message should not bypass local contacts_only policy for a newly added recipient."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("security/reply-contact-policy")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/reply-contact-policy"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/reply-contact-policy"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/reply-contact-policy"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-4",
            },
        )
        await client.call_tool(
            "set_contact_policy",
            {
                "project_key": pkey("security/reply-contact-policy"),
                "agent_name": "codex-wsl-semantics-4",
                "policy": "contacts_only",
            },
        )

        seed = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("security/reply-contact-policy"),
                "sender_name": "codex-wsl-semantics-2",
                "to": ["codex-wsl-semantics-1"],
                "subject": "Seed",
                "body_md": "start thread",
                "idempotency_key": "reply-contact-policy-seed",
            },
        )
        seed_id = (seed.data.get("deliveries") or [])[0]["message"]["id"]

        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "reply_message",
                {
                    "project_key": pkey("security/reply-contact-policy"),
                    "message_id": seed_id,
                    "sender_name": "codex-wsl-semantics-1",
                    "to": ["codex-wsl-semantics-4"],
                    "body_md": "looping in a new recipient",
                    "idempotency_key": "reply-contact-policy-blocked",
                },
            )
        assert "Contact approval required for recipients: codex-wsl-semantics-4" in str(exc_info.value)


@pytest.mark.asyncio
async def test_send_message_rejects_expired_local_approved_contact(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = "/security/send-expired-local"
        await client.call_tool("ensure_project", {"human_key": project_key})
        await client.call_tool(
            "register_agent",
            {"project_key": project_key, "program": "codex", "model": "gpt-5", "name": "codex-wsl-semantics-1"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": project_key, "program": "codex", "model": "gpt-5", "name": "codex-wsl-semantics-2"},
        )
        await client.call_tool(
            "set_contact_policy",
            {"project_key": project_key, "agent_name": "codex-wsl-semantics-2", "policy": "contacts_only"},
        )
        await client.call_tool(
            "request_contact",
            {
                "project_key": project_key,
                "from_agent": "codex-wsl-semantics-1",
                "to_agent": "codex-wsl-semantics-2",
            },
        )
        await client.call_tool(
            "respond_contact",
            {
                "project_key": project_key,
                "to_agent": "codex-wsl-semantics-2",
                "from_agent": "codex-wsl-semantics-1",
                "accept": True,
            },
        )
        await _expire_contact_link(project_key, "codex-wsl-semantics-1", "codex-wsl-semantics-2")

        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": "codex-wsl-semantics-1",
                    "to": ["codex-wsl-semantics-2"],
                    "subject": "Stale approval",
                    "body_md": "expired approvals must not authorize delivery",
                    "auto_contact_if_blocked": False,
                    "idempotency_key": "send-expired-local",
                },
            )
        assert "Contact approval required for recipients: codex-wsl-semantics-2" in str(exc_info.value)


@pytest.mark.asyncio
async def test_reply_message_rejects_expired_local_approved_contact(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = "/security/reply-expired-local"
        await client.call_tool("ensure_project", {"human_key": project_key})
        await client.call_tool(
            "register_agent",
            {"project_key": project_key, "program": "codex", "model": "gpt-5", "name": "codex-wsl-semantics-1"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": project_key, "program": "codex", "model": "gpt-5", "name": "codex-wsl-semantics-2"},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": project_key, "program": "codex", "model": "gpt-5", "name": "codex-wsl-semantics-4"},
        )
        await client.call_tool(
            "set_contact_policy",
            {"project_key": project_key, "agent_name": "codex-wsl-semantics-4", "policy": "contacts_only"},
        )
        await client.call_tool(
            "request_contact",
            {
                "project_key": project_key,
                "from_agent": "codex-wsl-semantics-1",
                "to_agent": "codex-wsl-semantics-4",
            },
        )
        await client.call_tool(
            "respond_contact",
            {
                "project_key": project_key,
                "to_agent": "codex-wsl-semantics-4",
                "from_agent": "codex-wsl-semantics-1",
                "accept": True,
            },
        )
        await _expire_contact_link(project_key, "codex-wsl-semantics-1", "codex-wsl-semantics-4")

        seed = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": "codex-wsl-semantics-2",
                "to": ["codex-wsl-semantics-1"],
                "subject": "Seed",
                "body_md": "start thread before reply check",
                "idempotency_key": "reply-expired-local-seed",
            },
        )
        seed_id = (seed.data.get("deliveries") or [])[0]["message"]["id"]

        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "reply_message",
                {
                    "project_key": project_key,
                    "message_id": seed_id,
                    "sender_name": "codex-wsl-semantics-1",
                    "to": ["codex-wsl-semantics-4"],
                    "body_md": "expired approvals must not authorize replies",
                    "idempotency_key": "reply-expired-local-child",
                },
            )
        assert "Contact approval required for recipients: codex-wsl-semantics-4" in str(exc_info.value)


@pytest.mark.asyncio
async def test_send_message_rejects_expired_cross_project_approved_contact(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        backend_key = "/security/send-expired-backend"
        ops_key = "/security/send-expired-ops"

        await client.call_tool("ensure_project", {"human_key": backend_key})
        await client.call_tool("ensure_project", {"human_key": ops_key})

        sender = await client.call_tool(
            "register_agent",
            {
                "project_key": backend_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        receiver = await client.call_tool(
            "register_agent",
            {
                "project_key": ops_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        receiver_name = receiver.data["name"]

        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": backend_key,
                "requester": "codex-wsl-semantics-1",
                "target": receiver_name,
                "to_project": ops_key,
                "auto_accept": True,
                "requester_registration_token": sender.data["registration_token"],
                "target_registration_token": receiver.data["registration_token"],
            },
        )
        await _expire_contact_link(
            backend_key,
            "codex-wsl-semantics-1",
            receiver_name,
            target_project_key=ops_key,
        )

        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "send_message",
                {
                    "project_key": backend_key,
                    "sender_name": "codex-wsl-semantics-1",
                    "sender_token": sender.data["registration_token"],
                    "to": [f"{receiver_name}@{ops_key}"],
                    "subject": "Expired external approval",
                    "body_md": "stale external approvals must not route mail",
                    "auto_contact_if_blocked": False,
                    "idempotency_key": "send-expired-cross-project",
                },
            )
        assert f"external recipients missing approved contact links: {receiver_name} @ /security/send-expired-ops" in str(
            exc_info.value
        )


@pytest.mark.asyncio
async def test_reply_message_supports_agent_at_project_external_address(isolated_env):
    """reply_message should route approved cross-project recipients addressed as Agent@project."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("security/reply-xproj-backend")})
        await client.call_tool("ensure_project", {"human_key": pkey("security/reply-xproj-ops")})
        green = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/reply-xproj-backend"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/reply-xproj-backend"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        ops = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/reply-xproj-ops"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-5",
            },
        )
        green_token = green.data["registration_token"]
        ops_token = ops.data["registration_token"]
        ops_name = ops.data["name"]

        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": pkey("security/reply-xproj-backend"),
                "requester": "codex-wsl-semantics-1",
                "target": ops_name,
                "to_project": pkey("security/reply-xproj-ops"),
                "auto_accept": True,
                "requester_registration_token": green_token,
                "target_registration_token": ops_token,
            },
        )

        seed = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("security/reply-xproj-backend"),
                "sender_name": "codex-wsl-semantics-2",
                "to": ["codex-wsl-semantics-1"],
                "subject": "Seed",
                "body_md": "start thread",
                "idempotency_key": "reply-at-project-seed",
            },
        )
        seed_id = (seed.data.get("deliveries") or [])[0]["message"]["id"]

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": pkey("security/reply-xproj-backend"),
                "message_id": seed_id,
                "sender_name": "codex-wsl-semantics-1",
                "to": [f"{ops_name}@/security/reply-xproj-ops"],
                "body_md": "routing externally from a reply",
                "idempotency_key": "reply-at-project-child",
            },
        )
        assert any(delivery["project"] == pkey("security/reply-xproj-ops") for delivery in reply.data["deliveries"])
        assert get_db_health_status()["pool"]["checked_out"] == 0


@pytest.mark.asyncio
async def test_cross_project_sender_identity_does_not_collide_with_same_name_local_agent(isolated_env):
    """Cross-project mail must not leak into a same-name local agent's outbox or contact auto-allow heuristics."""
    server = build_mcp_server()
    async with Client(server) as client:
        backend_key = "/security/xproj-origin-backend"
        ops_key = "/security/xproj-origin-ops"

        await client.call_tool("ensure_project", {"human_key": backend_key})
        await client.call_tool("ensure_project", {"human_key": ops_key})

        source = await client.call_tool(
            "register_agent",
            {
                "project_key": backend_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        receiver = await client.call_tool(
            "register_agent",
            {
                "project_key": ops_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": ops_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        source_token = source.data["registration_token"]
        receiver_token = receiver.data["registration_token"]

        await client.call_tool(
            "set_contact_policy",
            {"project_key": ops_key, "agent_name": "codex-wsl-semantics-1", "policy": "contacts_only"},
        )
        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": backend_key,
                "requester": "codex-wsl-semantics-1",
                "target": "codex-wsl-semantics-2",
                "to_project": ops_key,
                "auto_accept": True,
                "requester_registration_token": source_token,
                "target_registration_token": receiver_token,
            },
        )

        sent = await client.call_tool(
            "send_message",
            {
                "project_key": backend_key,
                "sender_name": "codex-wsl-semantics-1",
                "sender_token": source_token,
                "to": [f"codex-wsl-semantics-2@{ops_key}"],
                "subject": "Cross-project origin",
                "body_md": "hello from the real backend sender",
                "thread_id": "XPROJ-IDENTITY-1",
                "idempotency_key": "cross-project-origin",
            },
        )
        ext_delivery = next(delivery for delivery in sent.data["deliveries"] if delivery["project"] == ops_key)
        ext_payload = ext_delivery["message"]
        ext_message_id = ext_payload["id"]

        inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": ops_key,
                "agent_name": "codex-wsl-semantics-2",
                "registration_token": receiver_token,
                "include_bodies": True,
            },
        )
        delivered = next(item for item in inbox.structured_content["result"] if item["id"] == ext_message_id)
        assert delivered["from"] == "codex-wsl-semantics-1"
        assert delivered["from_project"] == backend_key
        assert delivered["from_address"].endswith("#codex-wsl-semantics-1")

        outbox_blocks = await client.read_resource(
            f"resource://outbox/codex-wsl-semantics-1?project={ops_key}"
        )
        outbox_payload = json.loads(outbox_blocks[0].text or "{}")
        assert outbox_payload["count"] == 0

        msg_blocks = await client.read_resource(
            f"resource://message/{ext_message_id}?project={ops_key}"
            "&agent=codex-wsl-semantics-2"
        )
        msg_payload = json.loads(msg_blocks[0].text or "{}")
        assert msg_payload["from"] == "codex-wsl-semantics-1"
        assert msg_payload["from_project"] == backend_key

        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "reply_message",
                {
                    "project_key": ops_key,
                    "message_id": ext_message_id,
                    "sender_name": "codex-wsl-semantics-2",
                    "sender_token": receiver_token,
                    "to": ["codex-wsl-semantics-1"],
                    "body_md": "trying to loop in the local lookalike",
                    "idempotency_key": "cross-project-local-lookalike-reply",
                },
            )
        assert "Contact approval required for recipients: codex-wsl-semantics-1" in str(exc_info.value)

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": ops_key,
                "message_id": ext_message_id,
                "sender_name": "codex-wsl-semantics-2",
                "sender_token": receiver_token,
                "body_md": "replying to the actual external sender",
                "idempotency_key": "cross-project-actual-sender-reply",
            },
        )
        backend_delivery = next(delivery for delivery in reply.data["deliveries"] if delivery["project"] == backend_key)
        backend_payload = backend_delivery["message"]
        assert backend_payload["from"] == "codex-wsl-semantics-2"
        assert backend_payload["from_project"] == ops_key
        assert get_db_health_status()["pool"]["checked_out"] == 0


@pytest.mark.asyncio
async def test_send_message_does_not_write_legacy_mailbox_artifacts(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        backend_key = "/security/send-partial-backend"
        ops_key = "/security/send-partial-ops"

        await client.call_tool("ensure_project", {"human_key": backend_key})
        await client.call_tool("ensure_project", {"human_key": ops_key})

        sender = await client.call_tool(
            "register_agent",
            {
                "project_key": backend_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        receiver = await client.call_tool(
            "register_agent",
            {
                "project_key": ops_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": ops_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-3",
            },
        )
        receiver_name = receiver.data["name"]

        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": backend_key,
                "requester": "codex-wsl-semantics-1",
                "target": receiver_name,
                "to_project": ops_key,
                "auto_accept": True,
                "requester_registration_token": sender.data["registration_token"],
                "target_registration_token": receiver.data["registration_token"],
            },
        )

        reservation = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": ops_key,
                "agent_name": "codex-wsl-semantics-3",
                "paths": [f"agents/{receiver_name}/inbox/*/*/*.md"],
                "ttl_seconds": 1800,
                "exclusive": True,
            },
        )
        assert reservation.data["granted"]

        sent = await client.call_tool(
            "send_message",
            {
                "project_key": backend_key,
                "sender_name": "codex-wsl-semantics-1",
                "sender_token": sender.data["registration_token"],
                "to": ["codex-wsl-semantics-1", f"{receiver_name}@{ops_key}"],
                "subject": "Atomic multi-project delivery",
                "body_md": "legacy mailbox reservations do not govern immutable delivery",
                "idempotency_key": "send-partial-external",
            },
        )

        assert sent.data["count"] == 2
        assert {delivery["project"] for delivery in sent.data["deliveries"]} == {
            backend_key,
            ops_key,
        }
        assert not sent.data.get("delivery_errors")
        assert all(
            delivery["delivery"]["status"] == "published"
            for delivery in sent.data["deliveries"]
        )


@pytest.mark.asyncio
async def test_reply_message_does_not_write_legacy_mailbox_artifacts(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        backend_key = "/security/reply-failure-backend"
        ops_key = "/security/reply-failure-ops"

        await client.call_tool("ensure_project", {"human_key": backend_key})
        await client.call_tool("ensure_project", {"human_key": ops_key})

        sender = await client.call_tool(
            "register_agent",
            {
                "project_key": backend_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        receiver = await client.call_tool(
            "register_agent",
            {
                "project_key": ops_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": backend_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-3",
            },
        )
        receiver_name = receiver.data["name"]

        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": backend_key,
                "requester": "codex-wsl-semantics-1",
                "target": receiver_name,
                "to_project": ops_key,
                "auto_accept": True,
                "requester_registration_token": sender.data["registration_token"],
                "target_registration_token": receiver.data["registration_token"],
            },
        )

        seed = await client.call_tool(
            "send_message",
            {
                "project_key": backend_key,
                "sender_name": "codex-wsl-semantics-1",
                "sender_token": sender.data["registration_token"],
                "to": [f"{receiver_name}@{ops_key}"],
                "subject": "Seed external thread",
                "body_md": "start the thread externally",
                "idempotency_key": "reply-failure-seed",
            },
        )
        ops_delivery = next(
            delivery for delivery in (seed.data.get("deliveries") or []) if delivery["project"] == ops_key
        )
        ops_message_id = ops_delivery["message"]["id"]

        reservation = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": backend_key,
                "agent_name": "codex-wsl-semantics-3",
                "paths": ["agents/codex-wsl-semantics-1/inbox/*/*/*.md"],
                "ttl_seconds": 1800,
                "exclusive": True,
            },
        )
        assert reservation.data["granted"]

        reply = await client.call_tool(
            "reply_message",
            {
                "project_key": ops_key,
                "message_id": ops_message_id,
                "sender_name": receiver_name,
                "sender_token": receiver.data["registration_token"],
                "body_md": "this reply uses the immutable delivery document",
                "idempotency_key": "reply-failure-child",
            },
        )

        assert reply.data["count"] == 1
        assert reply.data["deliveries"][0]["project"] == backend_key
        assert reply.data["deliveries"][0]["delivery"]["status"] == "published"
        assert not reply.data.get("delivery_errors")


@pytest.mark.asyncio
async def test_search_and_summarize_thread_respect_recipient_visibility(isolated_env):
    """Only senders/recipients, including BCC, can discover a private thread."""
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": pkey("security/private-thread")})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/private-thread"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-1",
            },
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/private-thread"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-2",
            },
        )
        purple = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("security/private-thread"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-semantics-4",
            },
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]
        purple_token = purple.data["registration_token"]

    async with Client(server) as sender_client:
        await sender_client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": pkey("security/private-thread"),
                "requester": "codex-wsl-semantics-1",
                "target": "codex-wsl-semantics-2",
                "auto_accept": True,
                "requester_registration_token": green_token,
                "target_registration_token": blue_token,
            },
        )
        await sender_client.call_tool(
            "send_message",
            {
                "project_key": pkey("security/private-thread"),
                "sender_name": "codex-wsl-semantics-1",
                "sender_token": green_token,
                "to": ["codex-wsl-semantics-1"],
                "bcc": ["codex-wsl-semantics-2"],
                "subject": "Private plan",
                "body_md": "ultra-secret launch sequence",
                "thread_id": "SEC-THREAD-1",
                "idempotency_key": "private-thread-bcc",
            },
        )

    async with Client(server) as bcc_client:
        search_result = await bcc_client.call_tool(
            "search_messages",
            {
                "project_key": pkey("security/private-thread"),
                "query": "ultra-secret",
                "agent_name": "codex-wsl-semantics-2",
                "registration_token": blue_token,
            },
        )
        assert len(search_result.structured_content["result"]) == 1

        summary_result = await bcc_client.call_tool(
            "summarize_thread",
            {
                "project_key": pkey("security/private-thread"),
                "thread_id": "SEC-THREAD-1",
                "include_examples": True,
                "llm_mode": False,
                "agent_name": "codex-wsl-semantics-2",
                "registration_token": blue_token,
            },
        )
        assert summary_result.data["summary"]["total_messages"] == 1
        assert len(summary_result.data["examples"]) == 1

    async with Client(server) as outsider_client:
        search_result = await outsider_client.call_tool(
            "search_messages",
            {
                "project_key": pkey("security/private-thread"),
                "query": "ultra-secret",
                "agent_name": "codex-wsl-semantics-4",
                "registration_token": purple_token,
            },
        )
        assert search_result.structured_content["result"] == []

        summary_result = await outsider_client.call_tool(
            "summarize_thread",
            {
                "project_key": pkey("security/private-thread"),
                "thread_id": "SEC-THREAD-1",
                "include_examples": True,
                "llm_mode": False,
                "agent_name": "codex-wsl-semantics-4",
                "registration_token": purple_token,
            },
        )
        assert summary_result.data["summary"]["total_messages"] == 0
        assert summary_result.data["examples"] == []


@pytest.mark.asyncio
async def test_send_message_keeps_db_invisible_when_archive_publish_is_pending(
    isolated_env,
    monkeypatch,
):
    """A transient Git failure keeps the durable intent but exposes no message."""
    from sqlalchemy import func, select as sa_select

    import mcp_agent_mail.delivery as delivery_service
    from mcp_agent_mail.models import Message, MessageDelivery, MessageRecipient
    from mcp_agent_mail.storage import MessageDeliveryPendingError

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": "codex-wsl-semantics-2"},
        )

        async def _boom(_archive, delivery_id, *_args, **_kwargs):
            raise MessageDeliveryPendingError(delivery_id, "simulated archive publish failure")

        monkeypatch.setattr(delivery_service, "publish_message_delivery", _boom)

        result = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "codex-wsl-semantics-2",
                "to": ["codex-wsl-semantics-2"],
                "subject": "Plan",
                "body_md": "body",
                "idempotency_key": "archive-publish-pending",
            },
        )
        delivery = result.data["deliveries"][0]
        assert delivery["delivery"]["status"] == "pending"
        assert delivery["message"] is None

        async with get_session() as session:
            msg_count = (
                await session.execute(sa_select(func.count()).select_from(Message))
            ).scalar_one()
            rec_count = (
                await session.execute(sa_select(func.count()).select_from(MessageRecipient))
            ).scalar_one()
            delivery_count = (
                await session.execute(sa_select(func.count()).select_from(MessageDelivery))
            ).scalar_one()
        assert msg_count == 0
        assert rec_count == 0
        assert delivery_count == 1
