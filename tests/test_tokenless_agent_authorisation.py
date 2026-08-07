"""What a bystander may do to an identity that cannot authenticate itself.

Some agents have no registration token. The human operator's is created that
way — the overseer send path inserts the row directly — and there is no route
to issue one afterwards, because minting a token requires authentication and
authentication requires the token. So a tokenless agent cannot defend itself,
and whatever a peer is allowed to do to it, it cannot undo.

That makes the boundary worth pinning: reversible operations may be authorised
by any participant, irreversible ones by nobody but the target.

Every test uses TWO sessions. A session that registered an agent is *bound* to
it, and a bound session authenticates as that agent without consulting tokens
at all — so a single-session test never reaches the adjacent-agent branch and
passes for the wrong reason whichever way the branch is written.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import text

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session

KEY = "/test/tokenless"


def _data(result):
    return getattr(result, "data", None) or getattr(result, "structured_content", {})


async def _strip_token(agent_name: str) -> None:
    """Make an agent tokenless, the way the overseer identity is created."""
    async with get_session() as session:
        await session.execute(
            text("UPDATE agents SET registration_token = NULL WHERE name = :n"),
            {"n": agent_name},
        )
        await session.commit()


async def _seed(server) -> dict:
    """Create a tokenless 'ghost-1' and a token-holding 'peer-1'."""
    async with Client(server) as setup:
        await setup.call_tool("ensure_project", {"human_key": KEY})
        await setup.call_tool(
            "register_agent",
            {"project_key": KEY, "name": "ghost-1", "program": "probe", "model": "probe"},
        )
        peer = _data(
            await setup.call_tool(
                "register_agent",
                {"project_key": KEY, "name": "peer-1", "program": "probe", "model": "probe"},
            )
        )
    await _strip_token("ghost-1")
    return peer


async def _bind_as_peer(client, peer) -> None:
    """Authenticate this session as peer-1 and nothing else."""
    await client.call_tool(
        "whois",
        {
            "project_key": KEY,
            "agent_name": "peer-1",
            "registration_token": peer["registration_token"],
        },
    )


@pytest.mark.asyncio
async def test_a_peer_may_retire_a_tokenless_agent(isolated_env):
    """The reason adjacent-agent auth exists: clearing out pre-token agents
    without dropping to SQL."""
    server = build_mcp_server()
    peer = await _seed(server)
    async with Client(server) as bystander:
        await _bind_as_peer(bystander, peer)
        assert _data(
            await bystander.call_tool(
                "retire_agent", {"project_key": KEY, "agent_name": "ghost-1"}
            )
        )


@pytest.mark.asyncio
async def test_a_peer_may_put_a_tokenless_agent_back(isolated_env):
    """Retiring must be undoable by whoever could do it.

    Without this, retiring a tokenless agent locked it out permanently: undoing
    demanded the token it does not have, so the reversible half of the pair was
    one-way in exactly the case adjacent-agent auth exists for.
    """
    server = build_mcp_server()
    peer = await _seed(server)
    async with Client(server) as bystander:
        await _bind_as_peer(bystander, peer)
        await bystander.call_tool("retire_agent", {"project_key": KEY, "agent_name": "ghost-1"})
        assert _data(
            await bystander.call_tool(
                "unretire_agent", {"project_key": KEY, "agent_name": "ghost-1"}
            )
        )


@pytest.mark.asyncio
async def test_a_peer_may_not_permanently_delete_a_tokenless_agent(isolated_env):
    """The load-bearing one.

    Deleting an identity destroys its mailbox and its history, and a tokenless
    agent cannot be recreated with them. Allowing a bystander to do that put
    the one identity that cannot defend itself within reach of everyone else.
    """
    server = build_mcp_server()
    peer = await _seed(server)
    async with Client(server) as bystander:
        await _bind_as_peer(bystander, peer)
        with pytest.raises(ToolError) as excinfo:
            await bystander.call_tool(
                "hard_delete_agent",
                {"project_key": KEY, "agent_name": "ghost-1", "confirmation": "I UNDERSTAND"},
            )
        assert "ghost-1" in str(excinfo.value)

    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT name FROM agents WHERE name = :n"), {"n": "ghost-1"}
            )
        ).fetchone()
    assert row is not None, "the identity a bystander may not delete was deleted"


@pytest.mark.asyncio
async def test_an_agent_holding_its_own_token_is_still_deletable(isolated_env):
    """The narrowing must not reach agents that can authenticate."""
    server = build_mcp_server()
    peer = await _seed(server)
    async with Client(server) as client:
        assert _data(
            await client.call_tool(
                "hard_delete_agent",
                {
                    "project_key": KEY,
                    "agent_name": "peer-1",
                    "confirmation": "I UNDERSTAND",
                    "registration_token": peer["registration_token"],
                },
            )
        )
