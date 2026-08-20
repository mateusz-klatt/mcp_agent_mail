"""Test macro_start_session with file_reservation_paths parameter to prevent regression of the shadowing bug."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey

CLAIMS_SESSION_AGENT = "claude-wsl-claims-session-1"
NO_CLAIMS_SESSION_AGENT = "codex-wsl-claims-session-2"
EMPTY_CLAIMS_SESSION_AGENT = "codex-wsl-claims-session-3"
FILTERED_CLAIMS_SESSION_AGENT = "codex-wsl-claims-session-4"
CLAIMS_SESSION_TOKEN = "a" * 64
NO_CLAIMS_SESSION_TOKEN = "b" * 64
EMPTY_CLAIMS_SESSION_TOKEN = "c" * 64
FILTERED_CLAIMS_SESSION_TOKEN = "d" * 64


async def _provision_agent(
    client: Client,
    *,
    project_key: str,
    agent_name: str,
    program: str,
    model: str,
) -> str:
    """Provision the durable mailbox before exercising the session-only macro."""
    await client.call_tool("ensure_project", {"human_key": project_key})
    result = await client.call_tool(
        "register_agent",
        {
            "project_key": project_key,
            "program": program,
            "model": model,
            "name": agent_name,
        },
    )
    return result.data["registration_token"]


@pytest.mark.asyncio
async def test_macro_start_session_with_file_reservation_paths(isolated_env):
    """
    Test macro_start_session WITH file_reservation_paths parameter.

    This test specifically exercises the code path that was broken by the
    parameter shadowing the decorated function.

    The bug was: macro_start_session has a parameter named 'file_reservation_paths' which
    shadowed the file_reservation_paths function.

    The fix: Keep a non-shadowed reference to FastMCP 3's composable function.
    """
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = pkey("test/project")
        registration_token = await _provision_agent(
            client,
            project_key=project_key,
            agent_name=CLAIMS_SESSION_AGENT,
            program="claude-code",
            model="sonnet-4.5",
        )
        res = await client.call_tool(
            "macro_start_session",
            {
                "human_key": project_key,
                "program": "claude-code",
                "model": "sonnet-4.5",
                "agent_name": CLAIMS_SESSION_AGENT,
                "external_id": "claims-session-1",
                "client_name": "claude",
                "execution_token": CLAIMS_SESSION_TOKEN,
                "registration_token": registration_token,
                "task_description": "Testing file reservations functionality",
                "file_reservation_paths": ["src/**/*.py", "tests/**/*.py"],
                "file_reservation_reason": "Testing macro_start_session with file reservations",
                "file_reservation_ttl_seconds": 7200,
                "inbox_limit": 10,
            },
        )

        data = res.data

        # Verify project was created
        assert "project" in data
        assert data["project"]["slug"] == "test-project"
        assert data["project"]["human_key"] == pkey("test/project")

        # Verify agent was registered
        assert "agent" in data
        assert data["agent"]["name"] == CLAIMS_SESSION_AGENT
        assert data["agent"]["program"] == "claude-code"
        assert data["agent"]["model"] == "sonnet-4.5"

        # Verify file reservations were created (this is the critical part!)
        assert "file_reservations" in data
        assert data["file_reservations"] is not None
        assert "granted" in data["file_reservations"]

        # Should have granted reservations for both patterns
        granted_reservations = data["file_reservations"]["granted"]
        assert len(granted_reservations) == 2

        # Verify reservation details
        reservation_paths = {r["path_pattern"] for r in granted_reservations}
        assert "src/**/*.py" in reservation_paths
        assert "tests/**/*.py" in reservation_paths

        for r in granted_reservations:
            assert r["exclusive"] is True
            assert r["reason"] == "Testing macro_start_session with file reservations"
            assert "expires_ts" in r

        # Verify inbox was fetched
        assert "inbox" in data
        assert isinstance(data["inbox"], list)


@pytest.mark.asyncio
async def test_macro_start_session_without_file_reservations_still_works(isolated_env):
    """Verify that macro_start_session still works when file_reservation_paths is omitted."""
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = pkey("test/project2")
        registration_token = await _provision_agent(
            client,
            project_key=project_key,
            agent_name=NO_CLAIMS_SESSION_AGENT,
            program="codex",
            model="gpt-5",
        )
        res = await client.call_tool(
            "macro_start_session",
            {
                "human_key": project_key,
                "program": "codex",
                "model": "gpt-5",
                "agent_name": NO_CLAIMS_SESSION_AGENT,
                "external_id": "claims-session-2",
                "client_name": "codex",
                "execution_token": NO_CLAIMS_SESSION_TOKEN,
                "registration_token": registration_token,
                "task_description": "No file reservations test",
                # file_reservation_paths intentionally omitted
                "inbox_limit": 5,
            },
        )

        data = res.data

        # Verify basic functionality still works
        # endswith: see the note in test_macros.py — the derived slug carries a
        # drive-letter prefix off POSIX, and identity of the project is what this
        # line is for.
        assert data["project"]["slug"].endswith("test-project2")
        assert data["agent"]["name"] == NO_CLAIMS_SESSION_AGENT

        # file_reservations should be empty dict when not requested (not None - function returns {"granted": [], "conflicts": []})
        assert data["file_reservations"] == {"granted": [], "conflicts": []}
        assert len(data["file_reservations"]["granted"]) == 0

        # Inbox should still be fetched
        assert "inbox" in data
        assert isinstance(data["inbox"], list)


@pytest.mark.asyncio
async def test_macro_start_session_rejects_explicit_empty_file_reservation_paths(isolated_env):
    """Explicit empty reservation paths should be validated, not treated as omitted."""
    server = build_mcp_server()
    async with Client(server) as client:
        project_key = pkey("test/project-empty-claims")
        registration_token = await _provision_agent(
            client,
            project_key=project_key,
            agent_name=EMPTY_CLAIMS_SESSION_AGENT,
            program="codex",
            model="gpt-5",
        )
        with pytest.raises(ToolError, match=r"path|empty|required"):
            await client.call_tool(
                "macro_start_session",
                {
                    "human_key": project_key,
                    "program": "codex",
                    "model": "gpt-5",
                    "agent_name": EMPTY_CLAIMS_SESSION_AGENT,
                    "external_id": "claims-session-3",
                    "client_name": "codex",
                    "execution_token": EMPTY_CLAIMS_SESSION_TOKEN,
                    "registration_token": registration_token,
                    "file_reservation_paths": [],
                },
            )


@pytest.mark.asyncio
async def test_macro_start_session_uses_hidden_reservation_helper(
    isolated_env,
    monkeypatch,
):
    """Instance visibility must not break an internal macro dependency."""
    project_key = pkey("test/project-filtered-claims")
    provisioning_server = build_mcp_server()
    async with Client(provisioning_server) as client:
        registration_token = await _provision_agent(
            client,
            project_key=project_key,
            agent_name=FILTERED_CLAIMS_SESSION_AGENT,
            program="codex",
            model="gpt-5",
        )

    monkeypatch.setenv("TOOLS_FILTER_ENABLED", "true")
    monkeypatch.setenv("TOOLS_FILTER_PROFILE", "custom")
    monkeypatch.setenv("TOOLS_FILTER_MODE", "include")
    monkeypatch.setenv("TOOLS_FILTER_CLUSTERS", "workflow_macros")
    from mcp_agent_mail.config import clear_settings_cache

    clear_settings_cache()
    filtered_server = build_mcp_server()
    visible_tools = {tool.name for tool in await filtered_server.list_tools()}
    assert "macro_start_session" in visible_tools
    assert "file_reservation_paths" not in visible_tools

    async with Client(filtered_server) as client:
        result = await client.call_tool(
            "macro_start_session",
            {
                "human_key": project_key,
                "program": "codex",
                "model": "gpt-5",
                "agent_name": FILTERED_CLAIMS_SESSION_AGENT,
                "external_id": "claims-session-filtered",
                "client_name": "codex",
                "execution_token": FILTERED_CLAIMS_SESSION_TOKEN,
                "registration_token": registration_token,
                "file_reservation_paths": ["src/filtered.py"],
            },
        )
    assert [
        reservation["path_pattern"]
        for reservation in result.data["file_reservations"]["granted"]
    ] == ["src/filtered.py"]
