from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlmodel import select

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.db import get_session
from mcp_agent_mail.models import FileReservation
from tests.keys import pkey

START_EXECUTION_TOKEN = "1" * 64
THREAD_EXECUTION_TOKEN = "2" * 64
CYCLE_EXECUTION_TOKEN = "3" * 64
RENEW_EXECUTION_TOKEN = "4" * 64


@pytest.mark.asyncio
async def test_macro_start_session(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        provisioned = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("backend"),
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-macro-start-1",
            },
        )
        res = await client.call_tool(
            "macro_start_session",
            {
                "human_key": pkey("backend"),
                "program": "codex",
                "model": "gpt-5",
                "task_description": "macro",
                "agent_name": "codex-wsl-macro-start-1",
                "external_id": "macro-start-session-1",
                "client_name": "codex",
                "execution_token": START_EXECUTION_TOKEN,
                "registration_token": provisioned.data["registration_token"],
                "inbox_limit": 5,
            },
        )
        data = res.data
        # endswith, not ==: the slug is derived from the project key, and the
        # key is absolute, so off POSIX it carries a drive letter that shows up
        # as a "c-" prefix. The assertion is here to say "the macro returned the
        # project I asked for", which the suffix establishes; the exact prefix is
        # a property of the platform, not of the macro.
        assert data["project"]["slug"].endswith("backend")
        assert data["agent"]["name"] == "codex-wsl-macro-start-1"
        assert data["execution"]["external_id"] == "macro-start-session-1"
        assert data["execution"]["kind"] == "session"
        assert data["execution"]["status"] == "active"
        assert data["execution"]["lifecycle_protocol_version"] == 1
        assert "execution_token" not in data["execution"]
        assert "registration_token" not in data
        assert "file_reservations" in data and "inbox" in data


@pytest.mark.asyncio
async def test_macro_prepare_thread(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-macro-thread-1",
            },
        )
        m1 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "codex-wsl-macro-thread-1",
                "to": ["codex-wsl-macro-thread-1"],
                "subject": "T",
                "body_md": "b",
                "thread_id": "TKT-1",
                "idempotency_key": "macro-prepare-thread-seed",
            },
        )
        _ = m1.data
        prep = await client.call_tool(
            "macro_prepare_thread",
            {
                "project_key": "Backend",
                "thread_id": "TKT-1",
                "program": "codex",
                "model": "gpt-5",
                "agent_name": "codex-wsl-macro-thread-1",
                "external_id": "macro-prepare-thread-1",
                "client_name": "codex",
                "execution_token": THREAD_EXECUTION_TOKEN,
                "include_examples": True,
                "inbox_limit": 5,
            },
        )
        pdata = prep.data
        assert pdata["thread"]["thread_id"] == "TKT-1"
        assert "summary" in pdata["thread"]
        assert pdata["execution"]["external_id"] == "macro-prepare-thread-1"
        assert pdata["execution"]["lifecycle_protocol_version"] == 1
        assert "execution_token" not in pdata["execution"]
        assert "registration_token" not in pdata


@pytest.mark.asyncio
async def test_macro_file_reservation_cycle(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        registered = await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-macro-cycle-1",
            },
        )
        execution = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": "Backend",
                "agent_name": "codex-wsl-macro-cycle-1",
                "external_id": "macro-reservation-cycle-1",
                "client_name": "codex",
                "execution_token": CYCLE_EXECUTION_TOKEN,
                "lifecycle_protocol_version": 1,
                "registration_token": registered.data["registration_token"],
            },
        )
        assert execution.data["lifecycle_protocol_version"] == 1
        assert "execution_token" not in execution.data
        res = await client.call_tool(
            "macro_file_reservation_cycle",
            {
                "project_key": "Backend",
                "agent_name": "codex-wsl-macro-cycle-1",
                "paths": ["src/*.py"],
                "ttl_seconds": 60,
                "exclusive": True,
                "auto_release": True,
                "execution_id": execution.data["id"],
                "execution_token": CYCLE_EXECUTION_TOKEN,
            },
        )
        data = res.data
        granted = data["file_reservations"]["granted"]
        assert [item["execution_id"] for item in granted] == [execution.data["id"]]
        assert [item["origin"] for item in granted] == ["auto"]
    assert data.get("released") is not None


@pytest.mark.asyncio
async def test_renew_file_reservations_extends_expiry(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        registered = await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-macro-renew-1",
            },
        )
        execution = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": "Backend",
                "agent_name": "codex-wsl-macro-renew-1",
                "external_id": "macro-renew-session-1",
                "client_name": "codex",
                "execution_token": RENEW_EXECUTION_TOKEN,
                "lifecycle_protocol_version": 1,
                "registration_token": registered.data["registration_token"],
            },
        )
        assert execution.data["lifecycle_protocol_version"] == 1
        assert "execution_token" not in execution.data
        g = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": "codex-wsl-macro-renew-1",
                "paths": ["src/app.py"],
                "ttl_seconds": 60,
                "exclusive": True,
                "execution_id": execution.data["id"],
                "execution_token": RENEW_EXECUTION_TOKEN,
            },
        )
        assert g.data["granted"]
        assert g.data["granted"][0]["origin"] == "auto"
        r = await client.call_tool(
            "renew_file_reservations",
            {
                "project_key": "Backend",
                "agent_name": "codex-wsl-macro-renew-1",
                "paths": ["src/app.py"],
                "extend_seconds": 600,
                "execution_id": execution.data["id"],
                "execution_token": RENEW_EXECUTION_TOKEN,
            },
        )
        assert r.data.get("renewed", 0) >= 1


@pytest.mark.asyncio
async def test_reservation_enforcement_requires_execution_without_mutation(
    isolated_env,
    monkeypatch,
):
    """Enforce mode rejects an unscoped reservation rather than inventing ownership."""
    monkeypatch.setenv("AGENT_EXECUTION_ENFORCEMENT_MODE", "enforce")
    clear_settings_cache()
    try:
        server = build_mcp_server()
        async with Client(server) as client:
            project = await client.call_tool(
                "ensure_project", {"human_key": pkey("enforced")}
            )
            await client.call_tool(
                "register_agent",
                {
                    "project_key": pkey("enforced"),
                    "program": "codex",
                    "model": "gpt-5",
                    "name": "codex-wsl-enforced-1",
                },
            )

            with pytest.raises(ToolError, match="Call start_agent_execution first"):
                await client.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": pkey("enforced"),
                        "agent_name": "codex-wsl-enforced-1",
                        "paths": ["src/unscoped.py"],
                    },
                )

        async with get_session() as session:
            rows = (
                await session.execute(
                    select(FileReservation).where(
                        FileReservation.project_id == project.data["id"]
                    )
                )
            ).scalars().all()
        assert rows == []
    finally:
        clear_settings_cache()
