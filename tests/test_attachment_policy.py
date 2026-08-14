from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey


@pytest.mark.asyncio
async def test_attachment_policy_does_not_normalize_inline_markdown(isolated_env, tmp_path: Path, monkeypatch):
    # Ensure images are small enough to inline
    monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "1048576")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        # Register agent with explicit inline policy
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("backend"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-attachment-policy-1",
                "attachments_policy": "inline",
            },
        )
        # Create a tiny inline image as data URI in body
        body = "Here is an image ![pic](data:image/webp;base64,AAECAwQ=)"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("backend"),
                "sender_name": "codex-wsl-attachment-policy-1",
                "to": ["codex-wsl-attachment-policy-1"],
                "subject": "Inline",
                "body_md": body,
                "idempotency_key": "attachment-policy-inline-markdown",
            },
        )
        message = (res.data.get("deliveries") or [{}])[0].get("message", {})
        assert message.get("body_md") == body
        assert message.get("attachments") == []
