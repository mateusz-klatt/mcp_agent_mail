from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey

RESOURCE_AGENT = "codex-wsl-resources-1"


@pytest.mark.asyncio
async def test_core_resources(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": RESOURCE_AGENT},
        )
        msg = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": RESOURCE_AGENT,
                "to": [RESOURCE_AGENT],
                "subject": "R1",
                "body_md": "b",
                "idempotency_key": "more-resources-r1",
            },
        )
        message = (msg.data.get("deliveries") or [{}])[0].get("message", {})
        mid = message.get("id") or 1
        # config
        cfg = await client.read_resource("resource://config/environment")
        assert cfg
        # projects
        projs = await client.read_resource("resource://tooling/projects")
        assert projs
        # project specific
        proj = await client.read_resource("resource://project/backend")
        assert proj
        # message
        mres = await client.read_resource(f"resource://message/{mid}?project=Backend")
        assert mres
        # inbox
        ires = await client.read_resource(
            f"resource://inbox/{RESOURCE_AGENT}?project=Backend&limit=5"
        )
        assert ires
