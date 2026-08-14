"""Tests for the persistent window-based agent identity system (bd-1tz).

Covers:
- Window identity creation on first registration with MCP_AGENT_MAIL_WINDOW_ID
- Window identity reuse on subsequent registrations
- Priority chain: explicit name > window identity > auto-generate
- Window identity lifecycle (list, rename, expire)
- Edge cases: invalid UUID, multiple agents same window, no env var
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import text

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session
from mcp_agent_mail.utils import validate_client_platform_host_agent_id
from tests.keys import pkey

logger = logging.getLogger(__name__)

WINDOW_CREATED_AGENT = "codex-wsl-window-created-1"
WINDOW_REUSED_AGENT = "codex-wsl-window-reused-2"
WINDOW_MONOTONIC_AGENT = "codex-wsl-window-monotonic-3"
WINDOW_NO_ENV_AGENT = "codex-wsl-window-no-env-4"
WINDOW_INVALID_ENV_AGENT = "codex-wsl-window-invalid-env-5"
WINDOW_PRIORITY_AGENT = "codex-wsl-window-priority-6"
WINDOW_UNIQUE_AGENT_ONE = "codex-wsl-window-unique-7"
WINDOW_UNIQUE_AGENT_TWO = "codex-wsl-window-unique-8"
WINDOW_LIST_AGENT = "codex-wsl-window-list-9"
WINDOW_RENAME_AGENT = "codex-wsl-window-rename-10"
WINDOW_EXPIRE_AGENT = "codex-wsl-window-expire-11"
WINDOW_SHARED_AGENT_ONE = "codex-wsl-window-shared-12"
WINDOW_SHARED_AGENT_TWO = "codex-wsl-window-shared-13"
WINDOW_PERSIST_AGENT = "codex-wsl-window-persist-14"


def _parse_db_datetime(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


# ============================================================================
# Unit Tests: Window Identity Creation and Reuse
# ============================================================================


@pytest.mark.asyncio
async def test_window_id_created_on_first_registration(isolated_env, monkeypatch):
    """A new UUID in MCP_AGENT_MAIL_WINDOW_ID should create a new window identity."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/window")})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "test-program",
                "model": "test-model",
                "name": WINDOW_CREATED_AGENT,
            },
        )

        agent_name = result.data["name"]
        assert agent_name is not None
        assert validate_client_platform_host_agent_id(agent_name)
        # Window identity fields should be present
        assert result.data.get("window_id") == window_uuid
        assert result.data.get("window_display_name") == agent_name
        logger.debug("Window identity created: uuid=%s, name=%s", window_uuid, agent_name)


@pytest.mark.asyncio
async def test_window_id_reused_on_subsequent_registration(isolated_env, monkeypatch):
    """Same UUID should return the same display_name on re-registration."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/window")})

        result1 = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "test-program",
                "model": "test-model",
                "name": WINDOW_REUSED_AGENT,
            },
        )
        name1 = result1.data["name"]

        # Re-register with same window UUID
        result2 = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "test-program-v2",
                "model": "test-model-v2",
                "name": WINDOW_REUSED_AGENT,
            },
        )
        name2 = result2.data["name"]

        assert name1 == name2, "Same window UUID should produce same agent name"
        assert result2.data.get("window_id") == window_uuid
        logger.debug("Window identity reused: uuid=%s, name=%s", window_uuid, name1)


@pytest.mark.asyncio
async def test_window_id_reregister_does_not_shorten_active_expiry(isolated_env, monkeypatch):
    """Refreshing an active window identity should extend from its later expiry, not from now."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    project_key = "/test/window_monotonic"
    registration_token: str
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        registered = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "test-program",
                "model": "test-model",
                "name": WINDOW_MONOTONIC_AGENT,
            },
        )
        registration_token = registered.data["registration_token"]

    async with get_session() as session:
        result = await session.execute(
            text(
                """
                SELECT wi.expires_ts
                FROM window_identities wi
                JOIN projects p ON p.id = wi.project_id
                WHERE p.human_key = :project_key AND wi.window_uuid = :window_uuid
                """
            ),
            {"project_key": project_key, "window_uuid": window_uuid},
        )
        original_expiry = _parse_db_datetime(result.scalar_one())
        extended_expiry = original_expiry + timedelta(days=30)
        await session.execute(
            text(
                """
                UPDATE window_identities
                SET expires_ts = :expires_ts
                WHERE window_uuid = :window_uuid
                  AND project_id = (SELECT id FROM projects WHERE human_key = :project_key)
                """
            ),
            {
                "expires_ts": extended_expiry,
                "window_uuid": window_uuid,
                "project_key": project_key,
            },
        )
        await session.commit()

    async with Client(server) as client:
        await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "test-program-v2",
                "model": "test-model-v2",
                "name": WINDOW_MONOTONIC_AGENT,
                "registration_token": registration_token,
            },
        )

    async with get_session() as session:
        result = await session.execute(
            text(
                """
                SELECT wi.expires_ts
                FROM window_identities wi
                JOIN projects p ON p.id = wi.project_id
                WHERE p.human_key = :project_key AND wi.window_uuid = :window_uuid
                """
            ),
            {"project_key": project_key, "window_uuid": window_uuid},
        )
        refreshed_expiry = _parse_db_datetime(result.scalar_one())

    assert refreshed_expiry >= extended_expiry


@pytest.mark.asyncio
async def test_window_id_without_env_var(isolated_env, monkeypatch):
    """Without MCP_AGENT_MAIL_WINDOW_ID, behavior should be unchanged (no window fields)."""
    monkeypatch.delenv("MCP_AGENT_MAIL_WINDOW_ID", raising=False)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/window")})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "test-program",
                "model": "test-model",
                "name": WINDOW_NO_ENV_AGENT,
            },
        )

        assert result.data["name"] is not None
        assert "window_id" not in result.data
        assert "window_display_name" not in result.data


@pytest.mark.asyncio
async def test_window_id_invalid_format(isolated_env, monkeypatch):
    """Non-UUID value for MCP_AGENT_MAIL_WINDOW_ID should fall back to auto-generate."""
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", "not-a-valid-uuid")

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/window")})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "test-program",
                "model": "test-model",
                "name": WINDOW_INVALID_ENV_AGENT,
            },
        )

        # Should still work but without window identity
        assert result.data["name"] is not None
        assert validate_client_platform_host_agent_id(result.data["name"])
        assert "window_id" not in result.data


# ============================================================================
# Priority Chain Tests
# ============================================================================


@pytest.mark.asyncio
async def test_explicit_name_takes_priority_over_window(isolated_env, monkeypatch):
    """Priority 1: Explicit name should override window identity."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/window")})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "test-program",
                "model": "test-model",
                "name": WINDOW_PRIORITY_AGENT,
            },
        )

        assert result.data["name"] == WINDOW_PRIORITY_AGENT
        # Window identity should still be created for tracking
        assert result.data.get("window_id") == window_uuid
        logger.debug(
            "Explicit name priority: name=%s, window_id=%s",
            result.data["name"],
            result.data.get("window_id"),
        )


@pytest.mark.asyncio
async def test_window_display_name_unique_per_project(isolated_env, monkeypatch):
    """Different windows in the same project should get different names."""
    uuid1 = str(uuid.uuid4())
    uuid2 = str(uuid.uuid4())

    from mcp_agent_mail.config import clear_settings_cache

    server = build_mcp_server()

    # First window
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", uuid1)
    clear_settings_cache()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/window")})
        result1 = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "test",
                "model": "test",
                "name": WINDOW_UNIQUE_AGENT_ONE,
            },
        )

    # Second window
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", uuid2)
    clear_settings_cache()
    async with Client(server) as client:
        result2 = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "test",
                "model": "test",
                "name": WINDOW_UNIQUE_AGENT_TWO,
            },
        )

    assert result1.data["name"] != result2.data["name"]


# ============================================================================
# Window Identity Management Tools
# ============================================================================


@pytest.mark.asyncio
async def test_list_window_identities(isolated_env, monkeypatch):
    """list_window_identities should return active window identities."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/window")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "test",
                "model": "test",
                "name": WINDOW_LIST_AGENT,
            },
        )

        result = await client.call_tool(
            "list_window_identities",
            {"project_key": pkey("test/window")},
        )

        assert result.data["count"] >= 1
        identities = result.data["identities"]
        found = [i for i in identities if i["window_uuid"] == window_uuid]
        assert len(found) == 1
        assert found[0]["display_name"] is not None


@pytest.mark.asyncio
async def test_rename_window(isolated_env, monkeypatch):
    """rename_window should update the display name."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/window")})
        reg = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "test",
                "model": "test",
                "name": WINDOW_RENAME_AGENT,
            },
        )
        old_name = reg.data["name"]

        result = await client.call_tool(
            "rename_window",
            {
                "project_key": pkey("test/window"),
                "window_uuid": window_uuid,
                "new_display_name": "SilverFox",
            },
        )

        assert result.data["display_name"] == "SilverFox"
        assert result.data["old_display_name"] == old_name


@pytest.mark.asyncio
async def test_expire_window(isolated_env, monkeypatch):
    """expire_window should mark the identity as expired."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/window")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "test",
                "model": "test",
                "name": WINDOW_EXPIRE_AGENT,
            },
        )

        result = await client.call_tool(
            "expire_window",
            {
                "project_key": pkey("test/window"),
                "window_uuid": window_uuid,
            },
        )

        assert result.data["expired"] is True

        # After expiry, list should not include it
        list_result = await client.call_tool(
            "list_window_identities",
            {"project_key": pkey("test/window")},
        )
        found = [i for i in list_result.data["identities"] if i["window_uuid"] == window_uuid]
        assert len(found) == 0


# ============================================================================
# Integration Tests
# ============================================================================


@pytest.mark.asyncio
async def test_multiple_agents_same_window(isolated_env, monkeypatch):
    """Multiple agents registered with the same window UUID should share identity."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/window")})

        # Register first agent (auto-generated name from window)
        r1 = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "agent-1",
                "model": "model-1",
                "name": WINDOW_SHARED_AGENT_ONE,
            },
        )

        # Register second agent with explicit name but same window
        r2 = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "agent-2",
                "model": "model-2",
                "name": WINDOW_SHARED_AGENT_TWO,
            },
        )

        # Both should reference the same window identity
        assert r1.data.get("window_id") == window_uuid
        assert r2.data.get("window_id") == window_uuid


@pytest.mark.asyncio
async def test_window_persists_but_cannot_authenticate_a_new_session(isolated_env, monkeypatch):
    """A server-global window UUID is history, never authentication proof."""
    window_uuid = str(uuid.uuid4())
    monkeypatch.setenv("MCP_AGENT_MAIL_WINDOW_ID", window_uuid)

    from mcp_agent_mail.config import clear_settings_cache
    clear_settings_cache()

    server = build_mcp_server()

    # First session
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/window")})
        r1 = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "session-1",
                "model": "test",
                "name": WINDOW_PERSIST_AGENT,
            },
        )
        name1 = r1.data["name"]
        registration_token = r1.data["registration_token"]

    # A separate session sees the same server-global WindowIdentity mapping,
    # but must still prove the durable Agent credential. This is especially
    # important for HTTP, where unrelated clients share one server process.
    async with Client(server) as client:
        with pytest.raises(ToolError, match="requires registration_token"):
            await client.call_tool(
                "register_agent",
                {
                    "project_key": pkey("test/window"),
                    "program": "session-2",
                    "model": "test",
                    "name": WINDOW_PERSIST_AGENT,
                },
            )

        r2 = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/window"),
                "program": "session-2",
                "model": "test",
                "name": WINDOW_PERSIST_AGENT,
                "registration_token": registration_token,
            },
        )
        name2 = r2.data["name"]

    assert name1 == name2, "Window identity should persist across sessions"
    assert r2.data["window_id"] == window_uuid
