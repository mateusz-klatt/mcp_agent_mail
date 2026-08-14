from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey


@pytest.mark.asyncio
async def test_tooling_resources_and_recent(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("health_check", {})
        # directory
        d = await client.read_resource("resource://tooling/directory")
        assert d and "metrics" in (d[0].text or "")
        # metrics
        m = await client.read_resource("resource://tooling/metrics")
        assert m and "health_check" in (m[0].text or "")
        # capabilities for unknown agent -> []
        c = await client.read_resource("resource://tooling/capabilities/Someone")
        assert c and "[]" in (c[0].text or "[]")
        # recent window
        r = await client.read_resource("resource://tooling/recent/5")
        assert r and "tool" in (r[0].text or "")


@pytest.mark.asyncio
async def test_ack_views_resources(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-tooling-views-1",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "codex-wsl-tooling-views-1",
                "to": ["codex-wsl-tooling-views-1"],
                "subject": "AckReq",
                "body_md": "x",
                "ack_required": True,
                "idempotency_key": "tooling-view-ack-required",
            },
        )
        # Views may be empty/non-empty; ensure they respond with JSON
        for uri in [
            "resource://views/ack-required/codex-wsl-tooling-views-1?project=Backend",
            "resource://views/acks-stale/codex-wsl-tooling-views-1?project=Backend",
            "resource://views/ack-overdue/codex-wsl-tooling-views-1?project=Backend",
            "resource://views/urgent-unread/codex-wsl-tooling-views-1?project=Backend",
            "resource://mailbox/codex-wsl-tooling-views-1?project=Backend",
            "resource://outbox/codex-wsl-tooling-views-1?project=Backend",
        ]:
            blocks = await client.read_resource(uri)
            assert blocks and isinstance(blocks[0].text, str)
