"""A display alias must never become an address.

The whole value of the derived agent name is that it does not move: `to:`
resolves it, conflict warnings print it, the credential store is keyed by it.
An alias exists so a human can read the fleet at a glance, and the moment it
becomes addressable it turns a mutable field load-bearing — rename once and
every memorised address, thread participant and reservation points at nothing.

So the property under test is mostly a negative one, and negatives rot quietly:
nothing fails if `to:` silently starts accepting aliases, it just becomes true.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server

KEY = "/test/aliases"
FIRST_AGENT = "codex-linux-aliases-1"
SECOND_AGENT = "claude-mac-aliases-1"


def _data(result):
    return getattr(result, "data", None) or getattr(result, "structured_content", {})


async def _two_agents(client):
    await client.call_tool("ensure_project", {"human_key": KEY})
    first = _data(
        await client.call_tool(
            "register_agent",
            {
                "project_key": KEY,
                "name": FIRST_AGENT,
                "program": "probe",
                "model": "probe",
            },
        )
    )
    second = _data(
        await client.call_tool(
            "register_agent",
            {
                "project_key": KEY,
                "name": SECOND_AGENT,
                "program": "probe",
                "model": "probe",
            },
        )
    )
    return first, second


@pytest.mark.asyncio
async def test_alias_is_reported_alongside_the_name(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        first, _ = await _two_agents(client)
        result = _data(
            await client.call_tool(
                "set_agent_display_name",
                {
                    "project_key": KEY,
                    "agent_name": FIRST_AGENT,
                    "display_name": "Kitchen Box",
                    "registration_token": first["registration_token"],
                },
            )
        )
        assert result["agent"] == FIRST_AGENT
        assert result["display_name"] == "Kitchen Box"

        seen = _data(
            await client.call_tool(
                "whois",
                {
                    "project_key": KEY,
                    "agent_name": FIRST_AGENT,
                    "registration_token": first["registration_token"],
                },
            )
        )
        # Alongside, never instead: the name is what must be typed into `to:`.
        assert seen["name"] == FIRST_AGENT
        assert seen["display_name"] == "Kitchen Box"


@pytest.mark.asyncio
async def test_alias_is_not_an_address(isolated_env):
    """The load-bearing test. `to:` must keep refusing the alias."""
    server = build_mcp_server()
    async with Client(server) as client:
        first, second = await _two_agents(client)
        await client.call_tool(
            "set_agent_display_name",
            {
                "project_key": KEY,
                "agent_name": FIRST_AGENT,
                "display_name": "Kitchen",
                "registration_token": first["registration_token"],
            },
        )
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "send_message",
                {
                    "project_key": KEY,
                    "sender_name": SECOND_AGENT,
                    "to": ["Kitchen"],
                    "subject": "s",
                    "body_md": "b",
                    "registration_token": second["registration_token"],
                    "idempotency_key": "display-name-not-addressable",
                },
            )
        assert "Kitchen" in str(excinfo.value)


@pytest.mark.asyncio
async def test_alias_may_not_impersonate_another_agents_name(isolated_env):
    """The one genuinely deceptive collision: readers would attribute this
    agent's messages and reservations to the agent it is named after."""
    server = build_mcp_server()
    async with Client(server) as client:
        first, _ = await _two_agents(client)
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "set_agent_display_name",
                {
                    "project_key": KEY,
                    "agent_name": FIRST_AGENT,
                    "display_name": SECOND_AGENT,
                    "registration_token": first["registration_token"],
                },
            )
        assert SECOND_AGENT in str(excinfo.value)


@pytest.mark.asyncio
async def test_alias_may_not_duplicate_another_agents_alias(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        first, second = await _two_agents(client)
        await client.call_tool(
            "set_agent_display_name",
            {
                "project_key": KEY,
                "agent_name": FIRST_AGENT,
                "display_name": "Kitchen",
                "registration_token": first["registration_token"],
            },
        )
        with pytest.raises(ToolError):
            await client.call_tool(
                "set_agent_display_name",
                {
                    "project_key": KEY,
                    "agent_name": SECOND_AGENT,
                    "display_name": "kitchen",
                    "registration_token": second["registration_token"],
                },
            )


@pytest.mark.asyncio
async def test_an_agent_may_re_set_its_own_alias(isolated_env):
    """Re-sending the same label must not collide with itself."""
    server = build_mcp_server()
    async with Client(server) as client:
        first, _ = await _two_agents(client)
        args = {
            "project_key": KEY,
            "agent_name": FIRST_AGENT,
            "display_name": "Kitchen",
            "registration_token": first["registration_token"],
        }
        await client.call_tool("set_agent_display_name", args)
        result = _data(await client.call_tool("set_agent_display_name", args))
        assert result["display_name"] == "Kitchen"


@pytest.mark.asyncio
async def test_control_characters_are_stripped(isolated_env):
    """A newline in an alias turns one line of a warning into two, and lets the
    label forge the line that follows it."""
    server = build_mcp_server()
    async with Client(server) as client:
        first, _ = await _two_agents(client)
        result = _data(
            await client.call_tool(
                "set_agent_display_name",
                {
                    "project_key": KEY,
                    "agent_name": FIRST_AGENT,
                    "display_name": "Kitchen\nBox\r\t",
                    "registration_token": first["registration_token"],
                },
            )
        )
        assert "\n" not in result["display_name"]
        assert "\r" not in result["display_name"]
        assert "\t" not in result["display_name"]


@pytest.mark.asyncio
async def test_empty_clears_the_alias(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        first, _ = await _two_agents(client)
        token = first["registration_token"]
        await client.call_tool(
            "set_agent_display_name",
            {"project_key": KEY, "agent_name": FIRST_AGENT,
             "display_name": "Kitchen", "registration_token": token},
        )
        result = _data(
            await client.call_tool(
                "set_agent_display_name",
                {"project_key": KEY, "agent_name": FIRST_AGENT,
                 "display_name": "", "registration_token": token},
            )
        )
        assert result["display_name"] is None

        seen = _data(
            await client.call_tool(
                "whois",
                {
                    "project_key": KEY,
                    "agent_name": FIRST_AGENT,
                    "registration_token": token,
                },
            )
        )
        # Absent rather than empty, so no consumer has to tell "" from unset.
        assert not seen.get("display_name")


@pytest.mark.asyncio
async def test_a_wrong_token_cannot_rename_someone_else(isolated_env):
    """An agent renames itself and nobody else.

    Two sessions, deliberately. A single session that registered both agents is
    *bound* to both, and a bound session is proof of identity on its own — the
    token is not consulted at all. Testing this in one session therefore proves
    nothing about tokens and passes for the wrong reason, which is how the
    first version of this test reported a hijack that was really just the
    session acting as itself.
    """
    server = build_mcp_server()
    async with Client(server) as client:
        _, second = await _two_agents(client)

    async with Client(server) as attacker:
        with pytest.raises(ToolError) as excinfo:
            await attacker.call_tool(
                "set_agent_display_name",
                {
                    "project_key": KEY,
                    "agent_name": FIRST_AGENT,
                    "display_name": "Hijacked",
                    "registration_token": second["registration_token"],
                },
            )
        assert "Invalid registration_token" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Automatic friendly aliases at provisioning (docs/roadmap-next.md).
#
# The generator returns to production here and ONLY here: the presentation
# field of the insert winner. These tests pin the boundary from both sides —
# a new Agent always has a readable label, and nothing about authentication,
# resume or explicit labels is ever overwritten by the generator.
# ---------------------------------------------------------------------------

THIRD_AGENT = "codex-win-aliases-1"


@pytest.mark.asyncio
async def test_new_agent_gets_a_generated_alias(isolated_env):
    """Omitting display_name at registration yields an adjective+noun alias."""
    from mcp_agent_mail.utils import validate_agent_name_format

    server = build_mcp_server()
    async with Client(server) as client:
        first, second = await _two_agents(client)
    for created in (first, second):
        alias = created.get("display_name")
        assert alias, f"expected a generated alias, got {alias!r}"
        assert validate_agent_name_format(alias), alias
        assert alias.lower() != created["name"].lower()


@pytest.mark.asyncio
async def test_explicit_display_name_at_registration_wins(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": KEY})
        created = _data(
            await client.call_tool(
                "register_agent",
                {
                    "project_key": KEY,
                    "name": THIRD_AGENT,
                    "program": "probe",
                    "model": "probe",
                    "display_name": "Custom Label 42",
                },
            )
        )
    assert created["display_name"] == "Custom Label 42"


@pytest.mark.asyncio
async def test_reregistration_never_touches_the_alias(isolated_env):
    """The generator runs for the insert winner only — resume regenerates nothing.

    A cleared alias staying cleared is the strongest form of the property: if
    any authentication or update path re-ran the generator, this NULL would be
    exactly where the regression shows up.
    """
    server = build_mcp_server()
    async with Client(server) as client:
        first, _ = await _two_agents(client)
        assert first.get("display_name")
        await client.call_tool(
            "set_agent_display_name",
            {
                "project_key": KEY,
                "agent_name": FIRST_AGENT,
                "display_name": None,
                "registration_token": first["registration_token"],
            },
        )
        again = _data(
            await client.call_tool(
                "register_agent",
                {
                    "project_key": KEY,
                    "name": FIRST_AGENT,
                    "program": "probe",
                    "model": "probe",
                    "registration_token": first["registration_token"],
                    "display_name": "MustNotLand",
                },
            )
        )
    assert again.get("display_name") is None


@pytest.mark.asyncio
async def test_generated_alias_skips_taken_labels(isolated_env, monkeypatch):
    """A colliding generated candidate is retried, not inserted twice."""
    import mcp_agent_mail.app as app_module

    server = build_mcp_server()
    async with Client(server) as client:
        first, _ = await _two_agents(client)
        taken = first["display_name"]
        assert taken
        candidates = iter([taken, taken, "RedFox"])
        monkeypatch.setattr(
            app_module, "generate_agent_name", lambda: next(candidates)
        )
        created = _data(
            await client.call_tool(
                "register_agent",
                {
                    "project_key": KEY,
                    "name": THIRD_AGENT,
                    "program": "probe",
                    "model": "probe",
                },
            )
        )
    assert created["display_name"] == "RedFox"


@pytest.mark.asyncio
async def test_explicit_alias_may_not_take_anothers_name_at_registration(isolated_env):
    """The set_agent_display_name clash rule applies at provisioning too."""
    server = build_mcp_server()
    async with Client(server) as client:
        await _two_agents(client)
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "register_agent",
                {
                    "project_key": KEY,
                    "name": THIRD_AGENT,
                    "program": "probe",
                    "model": "probe",
                    "display_name": FIRST_AGENT,
                },
            )
        assert "already taken" in str(excinfo.value)
