"""The three guards that keep adjacent-agent authorization narrow.

`test_tokenless_agent_authorisation.py` pins that the door OPENS: a bystander may
retire a tokenless agent and put it back. Both of its tests would still pass if
the door were widened to every action, to peers in other projects, or if walking
through it made the bystander *become* the agent. Nothing pinned it shut.

That asymmetry is the dangerous one here. A tokenless agent cannot defend itself
and cannot undo what is done to it, so the value of this branch is entirely in
what it REFUSES. These tests assert the refusals, one per guard in
`_adjacent_cleanup_peer`, plus the non-binding property that `_authenticate_agent`
states in prose and no test held:

    peer = await _adjacent_cleanup_peer(...)
    if peer is not None:
        await ctx.info(...)
        return agent          # <- returns WITHOUT _bind_session_agent

Each test carries its own positive control in the same file — the two tests
already there — so a refusal that fires for the wrong reason (a broken fixture,
a typo'd tool name) cannot masquerade as the guard working.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from sqlalchemy import text

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session

KEY = "/test/tokenless-narrow"
OTHER_KEY = "/test/tokenless-narrow-other"
TOKENLESS_AGENT = "codex-linux-narrow-1"
PEER_AGENT = "claude-linux-narrow-peer-1"
OUTSIDER_AGENT = "claude-linux-narrow-outsider-1"


def _data(result):
    return getattr(result, "data", None) or getattr(result, "structured_content", {})


async def _strip_token(agent_name: str) -> None:
    async with get_session() as session:
        await session.execute(
            text("UPDATE agents SET registration_token = NULL WHERE name = :n"),
            {"n": agent_name},
        )
        await session.commit()


async def _register(client, project_key: str, name: str) -> dict:
    return _data(
        await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "name": name,
                "program": "probe",
                "model": "probe",
            },
        )
    )


async def _seed(server) -> dict:
    """One tokenless agent and one token-holding peer, both in KEY."""
    async with Client(server) as setup:
        await setup.call_tool("ensure_project", {"human_key": KEY})
        await _register(setup, KEY, TOKENLESS_AGENT)
        peer = await _register(setup, KEY, PEER_AGENT)
    await _strip_token(TOKENLESS_AGENT)
    return peer


async def _seed_outsider(server) -> dict:
    """A token-holding agent in a DIFFERENT project."""
    async with Client(server) as setup:
        await setup.call_tool("ensure_project", {"human_key": OTHER_KEY})
        outsider = await _register(setup, OTHER_KEY, OUTSIDER_AGENT)
    return outsider


async def _bind(client, project_key: str, name: str, token: str) -> None:
    """Authenticate this session as one agent and nothing else."""
    await client.call_tool(
        "whois",
        {
            "project_key": project_key,
            "agent_name": name,
            "registration_token": token,
        },
    )


@pytest.mark.asyncio
async def test_a_peer_may_not_speak_as_a_tokenless_agent(isolated_env):
    """Guard one: the action allowlist.

    `_adjacent_cleanup_peer` returns None unless the action is retiring or
    unretiring. Cleaning up somebody's stranded mailbox is not the same as
    using it, and the difference is the whole reason the door is acceptable.
    """
    server = build_mcp_server()
    peer = await _seed(server)
    async with Client(server) as bystander:
        await _bind(bystander, KEY, PEER_AGENT, peer["registration_token"])
        with pytest.raises(Exception) as refused:
            await bystander.call_tool(
                "send_message",
                {
                    "project_key": KEY,
                    "sender_name": TOKENLESS_AGENT,
                    "to": [PEER_AGENT],
                    "subject": "spoken for",
                    "body_md": "sent by a bystander, not by the agent",
                    "idempotency_key": "narrow-speak-as",
                },
            )
    assert "does not have a registration token" in str(refused.value), (
        "sending as a tokenless agent must be refused for lack of ITS credential, "
        f"not for any other reason; got: {refused.value}"
    )
    assert "so send_message cannot be authenticated" in str(refused.value), (
        "the refusal must name the action it refused, or this test cannot tell "
        f"an authentication refusal from an argument-validation error; got: {refused.value}"
    )


@pytest.mark.asyncio
async def test_a_peer_in_another_project_may_not_retire_it(isolated_env):
    """Guard two: the peer is resolved for THIS project.

    Without the scoping, any authenticated session anywhere could retire any
    tokenless identity — and a tokenless identity is exactly the one that
    cannot object.
    """
    server = build_mcp_server()
    await _seed(server)
    outsider = await _seed_outsider(server)
    async with Client(server) as stranger:
        await _bind(
            stranger, OTHER_KEY, OUTSIDER_AGENT, outsider["registration_token"]
        )
        with pytest.raises(Exception) as refused:
            await stranger.call_tool(
                "retire_agent", {"project_key": KEY, "agent_name": TOKENLESS_AGENT}
            )
    assert "does not have a registration token" in str(refused.value), (
        "a session authenticated in another project is not an adjacent peer; "
        f"got: {refused.value}"
    )


@pytest.mark.asyncio
async def test_an_unauthenticated_session_may_not_retire_it(isolated_env):
    """Guard three: there must BE a peer.

    The permissive tests both bind a session first, so neither of them
    distinguishes "a peer authorized this" from "the branch authorizes
    everyone".
    """
    server = build_mcp_server()
    await _seed(server)
    async with Client(server) as anonymous:
        with pytest.raises(Exception) as refused:
            await anonymous.call_tool(
                "retire_agent", {"project_key": KEY, "agent_name": TOKENLESS_AGENT}
            )
    assert "does not have a registration token" in str(refused.value), (
        f"an unbound session has no adjacent peer to authorize it; got: {refused.value}"
    )


@pytest.mark.asyncio
async def test_cleaning_up_a_tokenless_agent_does_not_become_it(isolated_env):
    """The property `_authenticate_agent` states in prose and nothing held.

    'A bystander cleaning up somebody else's stranded mailbox has not become
    them.' The retire path returns the agent WITHOUT binding the session; if a
    rewrite added the bind for symmetry with the two paths above it, the
    bystander would silently gain the ability to act as an identity that cannot
    revoke anything.
    """
    server = build_mcp_server()
    peer = await _seed(server)
    async with Client(server) as bystander:
        await _bind(bystander, KEY, PEER_AGENT, peer["registration_token"])

        # positive control: the door does open for the permitted action
        await bystander.call_tool(
            "retire_agent", {"project_key": KEY, "agent_name": TOKENLESS_AGENT}
        )
        await bystander.call_tool(
            "unretire_agent", {"project_key": KEY, "agent_name": TOKENLESS_AGENT}
        )

        # ...and walking through it did not make this session that agent
        with pytest.raises(Exception) as refused:
            await bystander.call_tool(
                "send_message",
                {
                    "project_key": KEY,
                    "sender_name": TOKENLESS_AGENT,
                    "to": [PEER_AGENT],
                    "subject": "after cleanup",
                    "body_md": "would only arrive if cleanup had bound the session",
                    "idempotency_key": "narrow-after-cleanup",
                },
            )
    assert "does not have a registration token" in str(refused.value), (
        "adjacent cleanup must not bind the session to the cleaned-up agent; "
        f"got: {refused.value}"
    )
