"""What a bystander may do to an identity that cannot authenticate itself.

Some agents have no registration token. The human operator's is created that
way — the overseer send path inserts the row directly — and there is no route
to issue one afterwards, because minting a token requires authentication and
authentication requires the token. So a tokenless agent cannot defend itself,
and whatever a peer is allowed to do to it, it cannot undo.

That makes the reversible adjacent-agent authorization boundary worth pinning.
Irreversible identity deletion is intentionally absent from the public API.

Every test uses TWO sessions. A session that registered an agent is *bound* to
it, and a bound session authenticates as that agent without consulting tokens
at all — so a single-session test never reaches the adjacent-agent branch and
passes for the wrong reason whichever way the branch is written.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from sqlalchemy import text

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session

KEY = "/test/tokenless"
TOKENLESS_AGENT = "codex-linux-tokenless-1"
PEER_AGENT = "claude-linux-tokenless-peer-1"


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
    """Create one tokenless durable Agent and one token-holding peer."""
    async with Client(server) as setup:
        await setup.call_tool("ensure_project", {"human_key": KEY})
        await setup.call_tool(
            "register_agent",
            {
                "project_key": KEY,
                "name": TOKENLESS_AGENT,
                "program": "probe",
                "model": "probe",
            },
        )
        peer = _data(
            await setup.call_tool(
                "register_agent",
                {
                    "project_key": KEY,
                    "name": PEER_AGENT,
                    "program": "probe",
                    "model": "probe",
                },
            )
        )
    await _strip_token(TOKENLESS_AGENT)
    return peer


async def _bind_as_peer(client, peer) -> None:
    """Authenticate this session as the token-holding peer and nothing else."""
    await client.call_tool(
        "whois",
        {
            "project_key": KEY,
            "agent_name": PEER_AGENT,
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
                "retire_agent", {"project_key": KEY, "agent_name": TOKENLESS_AGENT}
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
        await bystander.call_tool(
            "retire_agent", {"project_key": KEY, "agent_name": TOKENLESS_AGENT}
        )
        assert _data(
            await bystander.call_tool(
                "unretire_agent", {"project_key": KEY, "agent_name": TOKENLESS_AGENT}
            )
        )
