from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey

CONTACT_AGENT_ONE = "codex-wsl-contacts-1"
CONTACT_AGENT_TWO = "codex-wsl-contacts-2"


@pytest.mark.asyncio
async def test_contact_policy_block_all_blocks_direct_message(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": CONTACT_AGENT_ONE},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": CONTACT_AGENT_TWO},
        )
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": CONTACT_AGENT_TWO, "policy": "block_all"},
        )

        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": CONTACT_AGENT_ONE,
                    "to": [CONTACT_AGENT_TWO],
                    "subject": "Hello",
                    "body_md": "test",
                    "idempotency_key": "contacts-block-all",
                },
            )
        assert "Recipient is not accepting messages" in str(excinfo.value)


@pytest.mark.asyncio
async def test_contacts_only_requires_approval_then_allows(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": CONTACT_AGENT_ONE},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": CONTACT_AGENT_TWO},
        )
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": CONTACT_AGENT_TWO, "policy": "contacts_only"},
        )

        first = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": CONTACT_AGENT_ONE,
                "to": [CONTACT_AGENT_TWO],
                "subject": "Ping",
                "body_md": "x",
                "idempotency_key": "contacts-auto-approval-first",
            },
        )
        deliveries_first = first.data.get("deliveries") or []
        assert deliveries_first and deliveries_first[0]["message"]["subject"] == "Ping"

        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": CONTACT_AGENT_ONE,
                "to": [CONTACT_AGENT_TWO],
                "subject": "AfterApproval",
                "body_md": "y",
                "idempotency_key": "contacts-after-approval",
            },
        )
        deliveries = ok.data.get("deliveries") or []
        assert deliveries and deliveries[0]["message"]["subject"] == "AfterApproval"


@pytest.mark.asyncio
async def test_contact_auto_allows_recent_overlapping_file_reservations(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": CONTACT_AGENT_ONE},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": CONTACT_AGENT_TWO},
        )

        # Overlapping file reservations -> auto allow contact
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": CONTACT_AGENT_ONE,
                "paths": ["src/app.py"],
                "ttl_seconds": 300,
                "exclusive": True,
            },
        )
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": CONTACT_AGENT_TWO,
                "paths": ["src/*.py"],
                "ttl_seconds": 300,
                "exclusive": True,
            },
        )

        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": CONTACT_AGENT_ONE,
                "to": [CONTACT_AGENT_TWO],
                "subject": "OverlapOK",
                "body_md": "z",
                "idempotency_key": "contacts-reservation-overlap",
            },
        )
        deliveries = ok.data.get("deliveries") or []
        assert deliveries and deliveries[0]["message"]["subject"] == "OverlapOK"


@pytest.mark.asyncio
async def test_cross_project_contact_handshake_routes_message(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        # Two projects
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool("ensure_project", {"human_key": pkey("frontend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": CONTACT_AGENT_ONE},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Frontend", "program": "claude", "model": "opus", "name": CONTACT_AGENT_TWO},
        )

        # Request/approve cross-project contact
        req = await client.call_tool(
            "request_contact",
            {
                "project_key": "Backend",
                "from_agent": CONTACT_AGENT_ONE,
                "to_agent": CONTACT_AGENT_TWO,
                "to_project": "Frontend",
            },
        )
        assert req.data.get("status") == "pending"

        resp = await client.call_tool(
            "respond_contact",
            {
                "project_key": "Frontend",
                "to_agent": CONTACT_AGENT_TWO,
                "from_agent": CONTACT_AGENT_ONE,
                "from_project": "Backend",
                "accept": True,
            },
        )
        assert resp.data.get("approved") is True

        # Now route a message from Backend->Frontend
        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": CONTACT_AGENT_ONE,
                "to": [f"project:Frontend#{CONTACT_AGENT_TWO}"],
                "subject": "CrossProject",
                "body_md": "hello",
                "idempotency_key": "contacts-cross-project",
            },
        )
        deliveries = ok.data.get("deliveries") or []
        assert any(d.get("project") in {"Frontend", pkey("frontend")} for d in deliveries)
