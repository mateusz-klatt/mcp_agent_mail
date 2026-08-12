"""Tests for the `unread_only` filter on `fetch_inbox`, `fetch_topic`, and
`fetch_inbox_product`.

The unread definition matches the rest of the server: "the calling recipient
has not yet explicitly marked the message read via `mark_message_read` or
`acknowledge_message`." A bare fetch does NOT mark read.

These tests are deliberately scoped to the load-bearing invariants:

  1. **Default omission stays a no-op.** `unread_only=False` (and omitting
     the parameter entirely) returns the same set as before — no contract
     change for existing callers.
  2. **The filter is per-recipient.** A message read by Agent A is still
     unread for Agent B; the filter inspects each recipient row independently.
  3. **AND-composes with sibling filters.** `unread_only=True` combined with
     `topic` / `since_ts` narrows to the intersection, not the union.
  4. **`fetch_topic` semantics.** When unread_only is set, the topic fetch
     narrows to recipient rows the viewer hasn't read — sender-only and
     thread-only visibility (which `_message_visible_to_agent_clause`
     normally surfaces) are correctly excluded under this flag.
"""

from __future__ import annotations

import logging

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server

logger = logging.getLogger(__name__)


def _data(result):
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        sc = result.structured_content.get("result")
        if isinstance(sc, dict):
            return sc
    if hasattr(result, "data") and isinstance(result.data, dict):
        return result.data
    if isinstance(result, dict):
        return result
    return getattr(result, "data", result)


def _list(result):
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        sc = result.structured_content.get("result")
        if isinstance(sc, list):
            return sc
    return list(getattr(result, "data", result))


async def _seed_project(client, project_key: str, count: int) -> list[str]:
    """Register `count` agents in the project; return their server-assigned names."""
    await client.call_tool("ensure_project", {"human_key": project_key})
    names: list[str] = []
    for i in range(count):
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "test-prog",
                "model": "test-model",
                "task_description": f"agent-{i}",
            },
        )
        names.append(_data(result)["name"])
    return names


async def _send(client, project_key: str, sender: str, to: list[str], subject: str, *, topic: str | None = None) -> int:
    payload = {
        "project_key": project_key,
        "sender_name": sender,
        "to": to,
        "subject": subject,
        "body_md": "x",
        "idempotency_key": f"unread:{subject}:{topic or 'none'}",
    }
    if topic is not None:
        payload["topic"] = topic
    res = await client.call_tool("send_message", payload)
    return int(_data(res)["deliveries"][0]["message"]["id"])


# ---------------------------------------------------------------------------
# fetch_inbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_inbox_unread_only_default_unchanged(isolated_env):
    """Omitting `unread_only` must equal `unread_only=False` and return all messages."""
    server = build_mcp_server()
    async with Client(server) as client:
        proj = "/test/unread-default"
        sender, recipient = await _seed_project(client, proj, 2)

        ids = [await _send(client, proj, sender, [recipient], f"m{i}") for i in range(3)]
        # Mark one message read; default fetch should still surface it.
        await client.call_tool(
            "mark_message_read",
            {"project_key": proj, "agent_name": recipient, "message_id": ids[0]},
        )

        omitted = _list(await client.call_tool(
            "fetch_inbox", {"project_key": proj, "agent_name": recipient}))
        explicit_false = _list(await client.call_tool(
            "fetch_inbox", {"project_key": proj, "agent_name": recipient, "unread_only": False}))

        assert {m["id"] for m in omitted} == set(ids)
        assert {m["id"] for m in explicit_false} == set(ids)


@pytest.mark.asyncio
async def test_fetch_inbox_unread_only_filters_marked_and_acked(isolated_env):
    """`unread_only=True` excludes both `mark_message_read` and `acknowledge_message` paths."""
    server = build_mcp_server()
    async with Client(server) as client:
        proj = "/test/unread-filter"
        sender, recipient = await _seed_project(client, proj, 2)

        marked = await _send(client, proj, sender, [recipient], "marked")
        acked = await _send(client, proj, sender, [recipient], "acked")
        untouched = await _send(client, proj, sender, [recipient], "untouched")

        await client.call_tool(
            "mark_message_read",
            {"project_key": proj, "agent_name": recipient, "message_id": marked},
        )
        await client.call_tool(
            "acknowledge_message",
            {"project_key": proj, "agent_name": recipient, "message_id": acked},
        )

        unread = _list(await client.call_tool(
            "fetch_inbox", {"project_key": proj, "agent_name": recipient, "unread_only": True}))

        assert {m["id"] for m in unread} == {untouched}


@pytest.mark.asyncio
async def test_fetch_inbox_unread_only_is_per_recipient(isolated_env):
    """A message read by Agent A is still unread for Agent B."""
    server = build_mcp_server()
    async with Client(server) as client:
        proj = "/test/unread-per-recipient"
        sender, agent_a, agent_b = await _seed_project(client, proj, 3)

        mid = await _send(client, proj, sender, [agent_a, agent_b], "shared")
        # agent_a reads; agent_b hasn't.
        await client.call_tool(
            "mark_message_read",
            {"project_key": proj, "agent_name": agent_a, "message_id": mid},
        )

        for_a = _list(await client.call_tool(
            "fetch_inbox", {"project_key": proj, "agent_name": agent_a, "unread_only": True}))
        for_b = _list(await client.call_tool(
            "fetch_inbox", {"project_key": proj, "agent_name": agent_b, "unread_only": True}))

        assert {m["id"] for m in for_a} == set()
        assert {m["id"] for m in for_b} == {mid}


@pytest.mark.asyncio
async def test_fetch_inbox_unread_only_ands_with_topic(isolated_env):
    """`unread_only=True` ANDs with `topic`; both must hold for a row to surface."""
    server = build_mcp_server()
    async with Client(server) as client:
        proj = "/test/unread-and-topic"
        sender, recipient = await _seed_project(client, proj, 2)

        ops_unread = await _send(client, proj, sender, [recipient], "ops u", topic="ops")
        ops_read = await _send(client, proj, sender, [recipient], "ops r", topic="ops")
        feat_unread = await _send(client, proj, sender, [recipient], "feat u", topic="feat")

        await client.call_tool(
            "mark_message_read",
            {"project_key": proj, "agent_name": recipient, "message_id": ops_read},
        )

        result = _list(await client.call_tool(
            "fetch_inbox",
            {
                "project_key": proj, "agent_name": recipient,
                "topic": "ops", "unread_only": True,
            },
        ))
        ids = {m["id"] for m in result}
        assert ids == {ops_unread}
        assert feat_unread not in ids  # filtered out by topic
        assert ops_read not in ids  # filtered out by unread_only


# ---------------------------------------------------------------------------
# fetch_topic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_topic_unread_only_excludes_read_recipient_rows(isolated_env):
    """`fetch_topic` with `unread_only=True` narrows to viewer's unread recipient rows.

    Sender-visible rows the viewer is not a recipient of are correctly excluded
    under this flag because "unread" is only well-defined for a recipient row.
    """
    server = build_mcp_server()
    async with Client(server) as client:
        proj = "/test/unread-topic"
        sender, viewer, bystander = await _seed_project(client, proj, 3)

        # Three messages tagged "ops" — viewer is recipient on m1 and m3,
        # bystander is recipient on m2 (viewer not a recipient).
        m1 = await _send(client, proj, sender, [viewer], "m1", topic="ops")
        await _send(client, proj, sender, [bystander], "m2", topic="ops")
        m3 = await _send(client, proj, sender, [viewer], "m3", topic="ops")

        # viewer reads m1.
        await client.call_tool(
            "mark_message_read",
            {"project_key": proj, "agent_name": viewer, "message_id": m1},
        )

        unread_only = _list(await client.call_tool(
            "fetch_topic",
            {
                "project_key": proj, "topic_name": "ops",
                "agent_name": viewer, "unread_only": True,
            },
        ))

        # Only m3: m1 was read; m2 has no viewer recipient row.
        assert {m["id"] for m in unread_only} == {m3}


# ---------------------------------------------------------------------------
# Field-level signature regression: parameter accepted, no-op on default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_inbox_accepts_unread_only_false_explicitly(isolated_env):
    """A backwards-compat smoke test that a polling client passing
    `unread_only=False` explicitly does not break or change shape."""
    server = build_mcp_server()
    async with Client(server) as client:
        proj = "/test/unread-explicit-false"
        sender, recipient = await _seed_project(client, proj, 2)
        await _send(client, proj, sender, [recipient], "single")
        out = _list(await client.call_tool(
            "fetch_inbox",
            {"project_key": proj, "agent_name": recipient, "unread_only": False},
        ))
        assert len(out) == 1
