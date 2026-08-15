"""Whose thread a resource read returns, and whom it may be read as.

`resource://thread/{id}` already has its stateless refusal pinned — it sits in
the `private_reads` list in `test_mcp_resources.py`, alongside every other
private resource. Two properties past that point are not:

* the `agent` query parameter **selects** one of this session's bindings; it
  does not create one. A session bound to A naming B must be refused, or the
  parameter becomes a way to read as anyone whose name you can guess.
* the read is still gated on the viewer. Naming yourself correctly does not
  widen what you may see, so a thread you are not a recipient of comes back
  empty rather than populated.

Both are one-line changes away in the resource body — `requested_agent=agent`
and `viewer=viewer` — and the two happy-path tests in `test_mcp_resources.py`
return a populated thread either way.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server

KEY = "/test/thread-resource-identity"
READER = "claude-linux-thresid-reader-1"
OTHER = "codex-linux-thresid-other-1"


def _data(result):
    return getattr(result, "data", None) or getattr(result, "structured_content", {})


def _payload(blocks) -> dict:
    """resources/read answers a list of content blocks, not a dict."""
    return json.loads(blocks[0].text)


async def _seed(server) -> tuple[dict, dict]:
    async with Client(server) as setup:
        await setup.call_tool("ensure_project", {"human_key": KEY})
        reader = _data(
            await setup.call_tool(
                "register_agent",
                {"project_key": KEY, "name": READER, "program": "p", "model": "p"},
            )
        )
        other = _data(
            await setup.call_tool(
                "register_agent",
                {"project_key": KEY, "name": OTHER, "program": "p", "model": "p"},
            )
        )
        # A thread the reader is part of, and one addressed to the other alone.
        await setup.call_tool(
            "send_message",
            {
                "project_key": KEY,
                "sender_name": OTHER,
                "to": [READER],
                "subject": "shared",
                "body_md": "addressed to the reader",
                "thread_id": "SHARED",
                "idempotency_key": "thresid-shared-1",
            },
        )
        await setup.call_tool(
            "send_message",
            {
                "project_key": KEY,
                "sender_name": OTHER,
                "to": [OTHER],
                "subject": "theirs",
                "body_md": "addressed to nobody else",
                "thread_id": "THEIRS",
                "idempotency_key": "thresid-theirs-1",
            },
        )
    return reader, other


async def _bind(client, name: str, token: str) -> None:
    await client.call_tool(
        "whois",
        {"project_key": KEY, "agent_name": name, "registration_token": token},
    )


@pytest.mark.asyncio
async def test_the_agent_parameter_cannot_name_someone_else(isolated_env):
    """It selects a binding this session already holds; it does not mint one.

    The positive control is the first read: the same session, same URI shape,
    same thread — only the name differs. Without it, a refusal caused by a
    malformed URI would read as the check working.
    """
    server = build_mcp_server()
    reader, other = await _seed(server)

    async with Client(server) as session:
        await _bind(session, READER, reader["registration_token"])

        mine = _payload(
            await session.read_resource(
                f"resource://thread/SHARED?project={KEY}&agent={READER}"
            )
        )
        assert mine["messages"], (
            "positive control failed: a session must be able to read its own "
            f"thread as itself; got {mine}"
        )

        with pytest.raises(Exception) as refused:
            await session.read_resource(
                f"resource://thread/SHARED?project={KEY}&agent={OTHER}"
            )
    assert "already authenticated in this MCP session" in str(refused.value), (
        "naming an agent this session is not bound to must be refused, not "
        f"silently honoured; got: {refused.value}"
    )
    assert OTHER in str(refused.value), (
        f"the refusal must name the agent it refused to act as; got: {refused.value}"
    )


@pytest.mark.asyncio
async def test_naming_yourself_does_not_widen_what_you_may_read(isolated_env):
    """Authentication is not authorisation.

    A correctly named, correctly bound viewer still only sees threads they are
    a party to. The existing happy-path tests read a thread the caller IS in,
    so they cannot tell a viewer-gated read from an ungated one.
    """
    server = build_mcp_server()
    reader, other = await _seed(server)

    async with Client(server) as session:
        await _bind(session, READER, reader["registration_token"])
        theirs = _payload(
            await session.read_resource(
                f"resource://thread/THEIRS?project={KEY}&agent={READER}"
            )
        )
        mine = _payload(
            await session.read_resource(
                f"resource://thread/SHARED?project={KEY}&agent={READER}"
            )
        )

    assert mine["messages"], "positive control: the reader's own thread must be visible"
    assert theirs["messages"] == [], (
        "a thread the viewer is not a party to must come back empty, not "
        f"populated; got {theirs}"
    )
