from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey

MISSING_PROJECT_AGENT = "codex-wsl-missing-1"
UNKNOWN_AGENT = "codex-wsl-ghost-1"
NEGATIVE_SENDER = "codex-wsl-negative-1"
NEGATIVE_RECIPIENT = "codex-wsl-negative-2"


@pytest.mark.asyncio
async def test_invalid_project_or_agent_errors(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        # Missing project — use non-raising MCP call to inspect error payload
        res = await client.call_tool_mcp(
            "register_agent",
            {
                "project_key": "Missing",
                "program": "x",
                "model": "y",
                "name": MISSING_PROJECT_AGENT,
            },
        )
        assert res.isError is True
        # Now create project and try sending from unknown agent
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        res2 = await client.call_tool_mcp(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": UNKNOWN_AGENT,
                "to": [UNKNOWN_AGENT],
                "subject": "x",
                "body_md": "y",
                "idempotency_key": "negative-unknown-sender",
            },
        )
        # Should be error due to unknown agent
        assert res2.isError is True


@pytest.mark.asyncio
async def test_unknown_recipient_reports_structured_error(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": NEGATIVE_SENDER},
        )

        # Unknown recipient returns structured error
        with pytest.raises(ToolError):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": NEGATIVE_SENDER,
                    "to": [NEGATIVE_RECIPIENT],
                    "subject": "Hello",
                    "body_md": "testing unknown recipient",
                    "idempotency_key": "negative-unknown-recipient-raising",
                },
            )

        # A sender never provisions somebody else's durable mailbox. The
        # non-raising transport must expose the same fail-closed result.
        res = await client.call_tool_mcp(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": NEGATIVE_SENDER,
                "to": [NEGATIVE_RECIPIENT],
                "subject": "Hello",
                "body_md": "testing unknown recipient",
                "idempotency_key": "negative-unknown-recipient-mcp",
            },
        )
        assert res.isError is True
        text = " ".join(getattr(c, "text", "") for c in res.content)
        assert NEGATIVE_RECIPIENT in text
        assert "must self-register" in text

        # Register and ensure an alternate separator/case spelling routes through
        # the same sanitized canonical identity.
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": NEGATIVE_RECIPIENT,
            },
        )
        success = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": NEGATIVE_SENDER,
                "to": ["Codex_WSL_Negative_2"],
                "subject": "Hello again",
                "body_md": "now routed",
                "idempotency_key": "negative-sanitized-recipient",
            },
        )
        deliveries = success.data.get("deliveries") or []
        assert deliveries and deliveries[0].get("message", {}).get("subject") == "Hello again"
