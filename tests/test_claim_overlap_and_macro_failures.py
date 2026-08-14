from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey

OVERLAP_AGENT_ONE = "codex-wsl-overlap-1"
OVERLAP_AGENT_TWO = "codex-wsl-overlap-2"
HANDSHAKE_AGENT_ONE = "codex-wsl-handshake-1"
HANDSHAKE_AGENT_TWO = "codex-wsl-handshake-2"
HANDSHAKE_AGENT_THREE = "codex-wsl-handshake-3"


@pytest.mark.asyncio
async def test_file_reservation_overlap_conflict_path(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": OVERLAP_AGENT_ONE})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": OVERLAP_AGENT_TWO})
        res1 = await client.call_tool("file_reservation_paths", {"project_key": "Backend", "agent_name": OVERLAP_AGENT_ONE, "paths": ["src/**"], "exclusive": True, "ttl_seconds": 3600})
        assert res1.data["granted"]
        res2 = await client.call_tool("file_reservation_paths", {"project_key": "Backend", "agent_name": OVERLAP_AGENT_TWO, "paths": ["src/app.py"], "exclusive": True, "ttl_seconds": 3600})
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
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": HANDSHAKE_AGENT_ONE})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": HANDSHAKE_AGENT_TWO})
        await client.call_tool("register_agent", {"project_key": "Backend", "program": "p", "model": "m", "name": HANDSHAKE_AGENT_THREE})
        reservation = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": HANDSHAKE_AGENT_THREE,
                "paths": [f"agents/{HANDSHAKE_AGENT_TWO}/inbox/*/*/*.md"],
                "exclusive": True,
                "ttl_seconds": 3600,
            },
        )
        assert reservation.data["granted"]
        result = await client.call_tool(
            "macro_contact_handshake",
            {"project_key": "Backend", "requester": HANDSHAKE_AGENT_ONE, "target": HANDSHAKE_AGENT_TWO, "auto_accept": True, "welcome_subject": "Hi", "welcome_body": "Welcome"},
        )
        assert "request" in result.data and "response" in result.data
        welcome_message = result.data["welcome_message"]
        assert welcome_message["count"] == 1
        assert welcome_message["deliveries"][0]["delivery"]["status"] == "published"
        assert result.data.get("welcome_error") is None
