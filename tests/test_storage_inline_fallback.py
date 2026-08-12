from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey


@pytest.mark.asyncio
async def test_convert_images_override_fails_closed(isolated_env, monkeypatch):
    # Force conversion off to exercise inline fallback path
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "x", "model": "y", "name": "BlueLake"},
        )
        body = "Inline ![p](data:image/webp;base64,AAECAwQ=) only"
        with pytest.raises(ToolError, match="bounded canonical inline representation"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "BlueLake",
                    "to": ["BlueLake"],
                    "subject": "InlineOnly",
                    "body_md": body,
                    "convert_images": False,
                    "idempotency_key": "inline-fallback-convert-disabled",
                },
            )

