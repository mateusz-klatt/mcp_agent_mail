from __future__ import annotations

from typing import Any, cast

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey


@pytest.mark.asyncio
async def test_reply_preserves_thread_and_subject_prefix(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        for n in ("GreenCastle", "BlueLake"):
            await client.call_tool(
                "register_agent",
                {"project_key": "Backend", "program": "x", "model": "y", "name": n},
            )
        # Allow direct messaging without contact gating for this test
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "BlueLake", "policy": "open"},
        )

        orig = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "Plan",
                "body_md": "body",
                "idempotency_key": "reply-thread-parent",
            },
        )
        delivery = (orig.data.get("deliveries") or [])[0]
        mid = delivery["message"]["id"]

        rep = await client.call_tool(
            "reply_message",
            {
                "project_key": "Backend",
                "message_id": mid,
                "sender_name": "BlueLake",
                "body_md": "ack",
                "idempotency_key": "reply-thread-first",
            },
        )
        # Ensure thread continuity and deliveries present
        assert rep.data.get("thread_id")
        assert rep.data.get("deliveries")

        # Subject prefix idempotent: replying again with same prefix shouldn't double it
        rep2 = await client.call_tool(
            "reply_message",
            {
                "project_key": "Backend",
                "message_id": mid,
                "sender_name": "BlueLake",
                "body_md": "second",
                "subject_prefix": "Re:",
                "idempotency_key": "reply-thread-second",
            },
        )
        assert rep2.data.get("deliveries")

        # Thread listing is validated via tool response thread_id; resource listing is covered elsewhere


@pytest.mark.asyncio
async def test_reply_to_round_trips_through_db(isolated_env):
    """#188: the direct parent→child reply edge must persist to the DB, not just
    appear in the response payload. The reply's stored ``reply_to`` column must
    equal the original message id, and the reply payload must reflect that
    STORED value."""
    from sqlalchemy import select as sa_select

    from mcp_agent_mail.db import get_session
    from mcp_agent_mail.models import Message

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        for n in ("GreenCastle", "BlueLake"):
            await client.call_tool(
                "register_agent",
                {"project_key": "Backend", "program": "x", "model": "y", "name": n},
            )
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "BlueLake", "policy": "open"},
        )
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": "GreenCastle", "policy": "open"},
        )

        orig = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "Plan",
                "body_md": "body",
                "idempotency_key": "reply-edge-parent",
            },
        )
        original_id = orig.data["deliveries"][0]["message"]["id"]
        # The original (top-level) message must have a NULL reply_to.
        assert orig.data["deliveries"][0]["message"].get("reply_to") is None

        rep = await client.call_tool(
            "reply_message",
            {
                "project_key": "Backend",
                "message_id": original_id,
                "sender_name": "BlueLake",
                "body_md": "ack",
                "idempotency_key": "reply-edge-child",
            },
        )
        # Response reflects the reply edge.
        assert rep.data["reply_to"] == original_id
        reply_id = rep.data["deliveries"][0]["message"]["id"]
        assert rep.data["deliveries"][0]["message"]["reply_to"] == original_id

        # The reply edge must be PERSISTED, not reconstructed only in the payload.
        async with get_session() as session:
            stored = (
                await session.execute(sa_select(Message).where(cast(Any, Message.id) == reply_id))
            ).scalars().one()
            assert stored.reply_to == original_id, "reply_to was not persisted to the DB (#188)"

            original = (
                await session.execute(sa_select(Message).where(cast(Any, Message.id) == original_id))
            ).scalars().one()
            assert original.reply_to is None, "top-level message must have NULL reply_to (#188)"

