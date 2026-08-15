"""Who may archive a project, and — the part nothing held — who may not.

A project has no administrator of its own, so `_authenticate_project_admin`
lets any of its token-bearing agents stand for it. That is a deliberate and
reasonable design, and it rests entirely on one clause:

    select(Agent).where(
        Agent.project_id == project.id,          # <- this one
        Agent.registration_token.isnot(None),
        Agent.provisioning_state == "active",
    )

Drop it and every registration token in the installation becomes an
administration token for every project. `archive_project` and
`unarchive_project` are its only two callers, so that clause is the whole
tenancy boundary for project-level administration.

No test named `_authenticate_project_admin` before this file, and none reached
it through either tool with a foreign credential. The tests here are negative
by design: each asserts a refusal, and each is paired with a positive control in
the same test so that a refusal arriving for an unrelated reason — a bad
argument, a missing project — cannot pass for the boundary holding.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server

MINE = "/test/admin-scope-mine"
THEIRS = "/test/admin-scope-theirs"
MY_AGENT = "claude-linux-adminscope-1"
THEIR_AGENT = "codex-linux-adminscope-outsider-1"


def _data(result):
    return getattr(result, "data", None) or getattr(result, "structured_content", {})


async def _seed(server) -> tuple[dict, dict]:
    """One token-bearing agent in each of two projects."""
    async with Client(server) as setup:
        await setup.call_tool("ensure_project", {"human_key": MINE})
        await setup.call_tool("ensure_project", {"human_key": THEIRS})
        mine = _data(
            await setup.call_tool(
                "register_agent",
                {
                    "project_key": MINE,
                    "name": MY_AGENT,
                    "program": "probe",
                    "model": "probe",
                },
            )
        )
        theirs = _data(
            await setup.call_tool(
                "register_agent",
                {
                    "project_key": THEIRS,
                    "name": THEIR_AGENT,
                    "program": "probe",
                    "model": "probe",
                },
            )
        )
    return mine, theirs


@pytest.mark.asyncio
async def test_a_foreign_token_cannot_archive_a_project(isolated_env):
    """The tenancy boundary. Everything else in this file is scaffolding.

    A token minted for one project must not administer another. Each call runs
    in its own fresh session so that neither is admitted by the session-binding
    branch, which would make the token irrelevant and the test vacuous.
    """
    server = build_mcp_server()
    mine, theirs = await _seed(server)

    async with Client(server) as intruder:
        with pytest.raises(Exception) as refused:
            await intruder.call_tool(
                "archive_project",
                {"project_key": MINE, "registration_token": theirs["registration_token"]},
            )
    assert "Invalid registration_token" in str(refused.value), (
        "a token from another project must be refused as invalid, not accepted "
        f"and not refused for some unrelated reason; got: {refused.value}"
    )

    # Positive control, same tool, same project, same session shape: the only
    # thing that changed is whose token it is. Without this, the refusal above
    # would also be produced by a broken tool name or an unusable project.
    async with Client(server) as owner:
        assert _data(
            await owner.call_tool(
                "archive_project",
                {"project_key": MINE, "registration_token": mine["registration_token"]},
            )
        ), "the project's own agent must be able to archive it"


@pytest.mark.asyncio
async def test_a_foreign_token_cannot_unarchive_a_project(isolated_env):
    """The other caller. Archiving and unarchiving must share one boundary.

    Guarding only the archiving half would leave a project archivable by its
    owner and restorable by a stranger, which is worse than either door being
    open on its own.
    """
    server = build_mcp_server()
    mine, theirs = await _seed(server)

    async with Client(server) as owner:
        await owner.call_tool(
            "archive_project",
            {"project_key": MINE, "registration_token": mine["registration_token"]},
        )

    async with Client(server) as intruder:
        with pytest.raises(Exception) as refused:
            await intruder.call_tool(
                "unarchive_project",
                {"project_key": MINE, "registration_token": theirs["registration_token"]},
            )
    assert "Invalid registration_token" in str(refused.value), (
        f"a foreign token must not restore someone else's project; got: {refused.value}"
    )

    async with Client(server) as owner:
        assert _data(
            await owner.call_tool(
                "unarchive_project",
                {"project_key": MINE, "registration_token": mine["registration_token"]},
            )
        ), "the project's own agent must be able to restore it"


@pytest.mark.asyncio
async def test_no_token_is_refused_by_name(isolated_env):
    """Absence and mismatch must be distinguishable in the code, not to the caller.

    The refusal for a missing token names the parameter, so an agent that simply
    forgot it learns what to send. The refusal for a wrong token does not say
    which of the project's agents it failed to match — with one candidate or
    fifty, the answer is the same sentence.
    """
    server = build_mcp_server()
    await _seed(server)

    async with Client(server) as anonymous:
        with pytest.raises(Exception) as refused:
            await anonymous.call_tool("archive_project", {"project_key": MINE})
    assert "requires registration_token" in str(refused.value), (
        f"a missing token must be named as missing; got: {refused.value}"
    )
    assert MY_AGENT not in str(refused.value), (
        "the refusal must not enumerate which agents hold tokens in this project; "
        f"got: {refused.value}"
    )
