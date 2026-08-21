"""`health_check` must fail when the database cannot be read.

It used to return a hardcoded "ok" without touching the database. During the
three corruptions on 2026-08-14 it stayed green for hours while mail
publishing was dead, and an agent used it to tell the operator production was
fine. A probe that cannot fail is not a probe.
"""

from __future__ import annotations

from typing import Any

import fastmcp
import pytest
from fastmcp import Client

from mcp_agent_mail import __version__, app as app_module
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache

BUILD_SHA = "a" * 40
NEXT_BUILD_SHA = "b" * 40


def _expected_build(git_sha: str | None) -> dict[str, str | None]:
    return {
        "application_version": __version__,
        "fastmcp_version": fastmcp.__version__,
        "git_sha": git_sha,
    }


@pytest.mark.asyncio
async def test_health_check_is_ok_when_the_database_answers(isolated_env: Any) -> None:
    server = build_mcp_server()
    async with Client(server) as client:
        assert client.initialize_result is not None
        assert client.initialize_result.serverInfo.version == __version__
        result = await client.call_tool("health_check", {})
    assert result.data["status"] == "ok"
    assert "database" not in result.data
    assert result.data["build"] == _expected_build(None)
    assert result.data["version"] == __version__
    assert "commit" not in result.data


@pytest.mark.asyncio
async def test_health_check_is_degraded_when_the_read_fails(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: without this, the test above passes for a hardcoded 'ok'.

    The failure is injected at the session, which is what the probe actually
    depends on -- a corrupt database surfaces there as a DatabaseError.
    """

    class _Unreadable:
        async def __aenter__(self) -> Any:
            raise sqlite_database_error("database disk image is malformed")

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    def sqlite_database_error(message: str) -> Exception:
        import sqlite3

        return sqlite3.DatabaseError(message)

    monkeypatch.setattr(app_module, "get_session", lambda: _Unreadable())
    monkeypatch.setenv("MCP_AGENT_MAIL_BUILD_COMMIT", BUILD_SHA)
    clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        result = await client.call_tool("health_check", {})

    assert result.data["status"] == "degraded"
    assert "DatabaseError" in result.data["database"]
    # The reason must not carry the message: it can name filesystem paths.
    assert "malformed" not in result.data["database"]
    assert result.data["build"] == _expected_build(BUILD_SHA)
    assert result.data["commit"] == BUILD_SHA


@pytest.mark.asyncio
async def test_health_check_build_identity_is_frozen_when_the_server_is_built(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_AGENT_MAIL_BUILD_COMMIT", BUILD_SHA)
    clear_settings_cache()
    server = build_mcp_server()

    # Runtime environment mutation must not rewrite the identity of an already
    # constructed server.
    monkeypatch.setenv("MCP_AGENT_MAIL_BUILD_COMMIT", NEXT_BUILD_SHA)
    async with Client(server) as client:
        result = await client.call_tool("health_check", {})

    assert result.data["build"] == _expected_build(BUILD_SHA)
    assert result.data["commit"] == BUILD_SHA
