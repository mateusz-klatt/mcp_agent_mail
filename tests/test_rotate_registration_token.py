"""An exposed registration token must be replaceable.

Rotation is a caller-journaled compare-and-swap: the server never has to return
the replacement secret, retries recover an ambiguous committed response, and a
credential change revokes every session binding minted by the old value.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache

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


async def _rotate(
    client: Client,
    project: str,
    name: str,
    current: str,
    replacement: str,
) -> dict[str, Any]:
    result = await client.call_tool(
        "rotate_registration_token",
        {
            "project_key": project,
            "agent_name": name,
            "registration_token": current,
            "new_registration_token": replacement,
        },
    )
    return result.data


@pytest.mark.asyncio
async def test_rotation_issues_a_new_token_and_retires_the_old(
    isolated_env: Any,
) -> None:
    project = pkey("rotation")
    name = "claude-linux-alpha-1"
    replacement = "a" * 64
    server = build_mcp_server()
    async with Client(server) as client:
        original = await _register(client, project, name)
        rotated = await _rotate(client, project, name, original, replacement)
        assert rotated == {
            "agent": name,
            "project": project,
            "rotated": True,
            "already_current": False,
        }
        assert original not in str(rotated)
        assert replacement not in str(rotated)

        # Rotation purges even the session that was already authenticated by
        # register_agent; a tokenless call cannot ride that old binding.
        with pytest.raises(ToolError):
            await client.call_tool(
                "whois",
                {"project_key": project, "agent_name": name},
            )

    async with Client(server) as fresh:
        await fresh.call_tool(
            "whois",
            {
                "project_key": project,
                "agent_name": name,
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
                    "agent_name": name,
                    "registration_token": original,
                },
            )


@pytest.mark.asyncio
async def test_rotation_requires_the_token_being_replaced(isolated_env: Any) -> None:
    """A bound session must not let a wrong explicit old token bypass the CAS."""
    project = pkey("rotation-auth")
    name = "claude-linux-beta-1"
    server = build_mcp_server()
    async with Client(server) as bound:
        original = await _register(bound, project, name)
        with pytest.raises(ToolError):
            await _rotate(
                bound,
                project,
                name,
                "not-the-right-token",
                "b" * 64,
            )

    async with Client(server) as fresh:
        # The refusal must not have rotated anything either.
        await fresh.call_tool(
            "whois",
            {
                "project_key": project,
                "agent_name": name,
                "registration_token": original,
            },
        )


@pytest.mark.asyncio
async def test_rotation_replays_the_same_journaled_replacement_idempotently(
    isolated_env: Any,
) -> None:
    project = pkey("rotation-replay")
    name = "codex-linux-replay-1"
    replacement = "c" * 64
    server = build_mcp_server()
    async with Client(server) as client:
        original = await _register(client, project, name)
        first = await _rotate(client, project, name, original, replacement)
        replay = await _rotate(client, project, name, original, replacement)

    assert first["rotated"] is True
    assert first["already_current"] is False
    assert replay["rotated"] is False
    assert replay["already_current"] is True
    assert replacement not in str(first) + str(replay)


@pytest.mark.asyncio
async def test_rotation_rejects_invalid_or_unchanged_replacements_without_mutation(
    isolated_env: Any,
) -> None:
    project = pkey("rotation-replacement-validation")
    name = "codex-linux-validation-1"
    current = "2" * 64
    server = build_mcp_server()
    async with Client(server) as client:
        original = await _register(client, project, name)
        await _rotate(client, project, name, original, current)
        for invalid in ("short", "A" * 64, current):
            with pytest.raises(ToolError) as raised:
                await _rotate(client, project, name, current, invalid)
            assert invalid not in str(raised.value)

    async with Client(server) as fresh:
        await fresh.call_tool(
            "whois",
            {
                "project_key": project,
                "agent_name": name,
                "registration_token": current,
            },
        )


@pytest.mark.asyncio
async def test_two_concurrent_rotations_have_exactly_one_cas_winner(
    isolated_env: Any,
) -> None:
    project = pkey("rotation-race")
    name = "codex-linux-race-1"
    replacements = ("d" * 64, "e" * 64)
    server = build_mcp_server()
    async with Client(server) as client:
        original = await _register(client, project, name)

    async def attempt(replacement: str) -> tuple[str, dict[str, Any] | ToolError]:
        async with Client(server) as client:
            try:
                return replacement, await _rotate(
                    client,
                    project,
                    name,
                    original,
                    replacement,
                )
            except ToolError as exc:
                return replacement, exc

    outcomes = await asyncio.gather(*(attempt(value) for value in replacements))
    successes = [item for item in outcomes if isinstance(item[1], dict)]
    failures = [item for item in outcomes if isinstance(item[1], ToolError)]
    assert len(successes) == 1
    assert len(failures) == 1
    winner = successes[0][0]
    loser = failures[0][0]

    async with Client(server) as fresh:
        await fresh.call_tool(
            "whois",
            {
                "project_key": project,
                "agent_name": name,
                "registration_token": winner,
            },
        )
    for retired in (original, loser):
        async with Client(server) as fresh:
            with pytest.raises(ToolError):
                await fresh.call_tool(
                    "whois",
                    {
                        "project_key": project,
                        "agent_name": name,
                        "registration_token": retired,
                    },
                )


@pytest.mark.asyncio
async def test_rotation_in_another_server_revokes_agent_and_execution_bindings(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_EXECUTION_ENFORCEMENT_MODE", "enforce")
    clear_settings_cache()
    project = pkey("rotation-cross-server")
    name = "codex-linux-cross-server-1"
    replacement = "f" * 64
    first_server = build_mcp_server()
    second_server = build_mcp_server()

    async with Client(first_server) as bound:
        original = await _register(bound, project, name)
        await bound.call_tool(
            "start_agent_execution",
            {
                "project_key": project,
                "agent_name": name,
                "external_id": "rotation-bound-session",
                "client_name": "pytest",
                "execution_token": "1" * 64,
                "lifecycle_protocol_version": 1,
            },
        )

        async with Client(second_server) as rotator:
            await _rotate(rotator, project, name, original, replacement)

        # Different FastMCP instances do not share Python dictionaries. The DB
        # fingerprint must therefore reject the old Agent binding on its own.
        with pytest.raises(ToolError):
            await bound.call_tool(
                "whois",
                {"project_key": project, "agent_name": name},
            )
        await bound.call_tool(
            "whois",
            {
                "project_key": project,
                "agent_name": name,
                "registration_token": replacement,
            },
        )

        # Reauthenticating the Agent must not revive the implicit root execution
        # that was bound under the retired registration credential.
        with pytest.raises(ToolError, match="start_agent_execution"):
            await bound.call_tool(
                "file_reservation_paths",
                {
                    "project_key": project,
                    "agent_name": name,
                    "paths": ["src/after-rotation.py"],
                },
            )


@pytest.mark.asyncio
async def test_rotation_schema_documents_both_secrets_and_a_no_secret_result(
    isolated_env: Any,
) -> None:
    server = build_mcp_server()
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        input_schema = tools["rotate_registration_token"].inputSchema
        assert input_schema["required"] == [
            "project_key",
            "agent_name",
            "registration_token",
            "new_registration_token",
        ]

        blocks = await client.read_resource("resource://tooling/schemas")
        documented = json.loads(blocks[0].text)["tools"][
            "rotate_registration_token"
        ]
        assert documented["required"] == input_schema["required"]
        assert any("64 lowercase hexadecimal" in item for item in documented["constraints"])
        assert any("durably journal" in item for item in documented["constraints"])
        assert any("never contains" in item for item in documented["constraints"])
        assert documented["returns"] == {
            "agent": "str",
            "project": "str",
            "rotated": "bool",
            "already_current": "bool",
        }
