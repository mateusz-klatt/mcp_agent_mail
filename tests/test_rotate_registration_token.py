"""An exposed registration token must be replaceable.

Until 2026-08-14 there was no way to answer an exposed token: the rotation
logic existed in `_ensure_agent_registration_token` behind a `rotate` flag
that no caller ever set, so three agents were told to rotate and none of
them could.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server

from .keys import pkey


async def _register(client: Client, project: str, name: str) -> str:
    await client.call_tool("ensure_project", {"human_key": project})
    result = await client.call_tool(
        "register_agent",
        {
            "project_key": project,
            "program": "test-program",
            "model": "test-model",
            "name": name,
        },
    )
    return result.data["registration_token"]


@pytest.mark.asyncio
async def test_rotation_issues_a_new_token_and_retires_the_old(
    isolated_env: Any,
) -> None:
    project = pkey("rotation")
    server = build_mcp_server()
    async with Client(server) as client:
        original = await _register(client, project, "claude-linux-alpha-1")

        rotated = await client.call_tool(
            "rotate_registration_token",
            {
                "project_key": project,
                "agent_name": "claude-linux-alpha-1",
                "registration_token": original,
            },
        )
        replacement = rotated.data["registration_token"]
        assert replacement and replacement != original

    # Both checks below need a FRESH session. Once a session has authenticated
    # as an agent the server stops demanding the token on later calls, so
    # asserting on the same connection proves nothing about the token at all.
    async with Client(server) as fresh:
        await fresh.call_tool(
            "whois",
            {
                "project_key": project,
                "agent_name": "claude-linux-alpha-1",
                "registration_token": replacement,
            },
        )

    async with Client(server) as fresh:
        # Without this the test would pass for a rotation that merely returned
        # a new string and changed nothing.
        with pytest.raises(ToolError):
            await fresh.call_tool(
                "whois",
                {
                    "project_key": project,
                    "agent_name": "claude-linux-alpha-1",
                    "registration_token": original,
                },
            )


@pytest.mark.asyncio
async def test_rotation_requires_the_token_being_replaced(isolated_env: Any) -> None:
    """Only the holder may rotate: a wrong token must not mint a new one."""
    project = pkey("rotation-auth")
    server = build_mcp_server()
    async with Client(server) as client:
        original = await _register(client, project, "claude-linux-beta-1")

    async with Client(server) as fresh:
        with pytest.raises(ToolError):
            await fresh.call_tool(
                "rotate_registration_token",
                {
                    "project_key": project,
                    "agent_name": "claude-linux-beta-1",
                    "registration_token": "not-the-right-token",
                },
            )

    async with Client(server) as fresh:
        # The refusal must not have rotated anything either.
        await fresh.call_tool(
            "whois",
            {
                "project_key": project,
                "agent_name": "claude-linux-beta-1",
                "registration_token": original,
            },
        )
