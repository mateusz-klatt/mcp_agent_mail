from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey


@pytest.mark.asyncio
async def test_file_reservation_overlap_conflict_path(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": "GreenCastle"})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": "BlueLake"})
        res1 = await client.call_tool("file_reservation_paths", {"project_key": "Backend", "agent_name": "GreenCastle", "paths": ["src/**"], "exclusive": True, "ttl_seconds": 3600})
        assert res1.data["granted"]
        res2 = await client.call_tool("file_reservation_paths", {"project_key": "Backend", "agent_name": "BlueLake", "paths": ["src/app.py"], "exclusive": True, "ttl_seconds": 3600})
        # Advisory model: still granted but conflicts populated
        assert res2.data["granted"] and res2.data["conflicts"]


@pytest.mark.asyncio
async def test_macro_contact_handshake_ignores_legacy_mailbox_reservations(
    isolated_env,
    monkeypatch,
):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": "RedStone"})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": "WhiteCat"})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": "BlueLake"})
        reservation = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": "BlueLake",
                "paths": ["agents/WhiteCat/inbox/*/*/*.md"],
                "exclusive": True,
                "ttl_seconds": 3600,
            },
        )
        assert reservation.data["granted"]
        result = await client.call_tool(
            "macro_contact_handshake",
            {"project_key": "Backend", "requester": "RedStone", "target": "WhiteCat", "auto_accept": True, "welcome_subject": "Hi", "welcome_body": "Welcome"},
        )
        assert "request" in result.data and "response" in result.data
        welcome_message = result.data["welcome_message"]
        assert welcome_message["count"] == 1
        assert welcome_message["deliveries"][0]["delivery"]["status"] == "published"
        assert result.data.get("welcome_error") is None
