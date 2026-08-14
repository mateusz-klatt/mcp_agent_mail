from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey

MAILBOX_AGENT = "codex-wsl-mailbox-1"


@pytest.mark.asyncio
async def test_mailbox_with_commits_includes_commit_meta(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": MAILBOX_AGENT},
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": MAILBOX_AGENT,
                "to": [MAILBOX_AGENT],
                "subject": "C1",
                "body_md": "b",
                "idempotency_key": "mailbox-commits-seed",
            },
        )
        blocks = await client.read_resource(
            f"resource://mailbox-with-commits/{MAILBOX_AGENT}?project=Backend&limit=5"
        )
        assert blocks and blocks[0].text
        # Text is JSON; ensure it mentions commit key when present
        assert "messages" in blocks[0].text
