"""Links into the coordination graph, and comments that are ordinary mail.

Slice 4 and slice 5 share one region of `app.py` and one contract: a ticket points at
things this server already owns, and its discussion lives in the mail system rather than
in a private table.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.db import ensure_schema, reset_database_state
from tests.keys import pkey

pytestmark = pytest.mark.usefixtures("isolated_env")

REPORTER = "claude-linux-holzera-1"
ASSIGNEE = "codex-wsl-home-1"


async def _call(tool_name: str, args: dict[str, Any]) -> Any:
    async with Client(build_mcp_server()) as client:
        result = await client.call_tool(tool_name, args)
    return result.data


def _boot() -> None:
    clear_settings_cache()
    reset_database_state()
    asyncio.run(ensure_schema())


async def _register(project_key: str, name: str) -> str:
    registered = await _call(
        "register_agent",
        {
            "project_key": project_key,
            "name": name,
            "program": "claude-code",
            "model": "test",
            "task_description": "ticketing tests",
        },
    )
    return str(registered["registration_token"])


async def _fixture(project_key: str, *, link_contacts: bool = True) -> tuple[str, str]:
    """Create the project and two agents, returning both tokens.

    Contact approval is established explicitly rather than assumed. `comment_ticket` and
    the assignment notice go through `send_message`, so they inherit contact policy in
    full -- which is the point of routing them there, and which means an unlinked pair is
    refused. `test_a_blocked_assignee_...` deliberately skips this step.
    """
    await _call("ensure_project", {"human_key": project_key})
    reporter_token = await _register(project_key, REPORTER)
    assignee_token = await _register(project_key, ASSIGNEE)
    if link_contacts:
        await _call(
            "macro_contact_handshake",
            {
                "project_key": project_key,
                "requester": REPORTER,
                "target": ASSIGNEE,
                "reason": "ticketing tests",
                "auto_accept": True,
                "requester_registration_token": reporter_token,
                "target_registration_token": assignee_token,
            },
        )
    return reporter_token, assignee_token


def test_a_ticket_points_at_the_message_where_it_was_decided() -> None:
    """The edge a general-purpose tracker structurally cannot have."""

    async def scenario() -> None:
        project_key = pkey("links-decided")
        reporter_token, assignee_token = await _fixture(project_key)
        common = {
            "project_key": project_key,
            "agent_name": REPORTER,
            "registration_token": reporter_token,
        }
        decision = await _call(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": REPORTER,
                "registration_token": reporter_token,
                "to": [ASSIGNEE],
                "subject": "we agreed to rotate the key",
                "body_md": "agreed on the call",
                "idempotency_key": "decision-1",
            },
        )
        message_id = decision["deliveries"][0]["message"]["id"]
        ticket = await _call("create_ticket", {**common, "title": "Rotate the signing key"})

        linked = await _call(
            "link_ticket",
            {
                **common,
                "ticket_key": ticket["key"],
                "relation": "decided_by",
                "target_kind": "message",
                "target_ref": str(message_id),
            },
        )
        assert linked["created"] is True

        # Idempotent: the same edge again is not an error and not a duplicate.
        again = await _call(
            "link_ticket",
            {
                **common,
                "ticket_key": ticket["key"],
                "relation": "decided_by",
                "target_kind": "message",
                "target_ref": str(message_id),
            },
        )
        assert again["created"] is False

        fetched = await _call("get_ticket", {**common, "ticket_key": ticket["key"]})
        assert len(fetched["links"]) == 1
        assert fetched["links"][0]["available"] is True
        assert fetched["links"][0]["direction"] == "outgoing"
        assert assignee_token  # the second agent exists, so the send above had a recipient

    _boot()
    asyncio.run(scenario())


def test_a_link_to_a_message_that_does_not_exist_is_refused_at_write_time() -> None:
    """A typo must fail when it is made; only a LATER purge may degrade the pointer."""

    async def scenario() -> None:
        project_key = pkey("links-typo")
        token, _ = await _fixture(project_key)
        common = {
            "project_key": project_key,
            "agent_name": REPORTER,
            "registration_token": token,
        }
        ticket = await _call("create_ticket", {**common, "title": "one"})

        with pytest.raises(ToolError) as refusal:
            await _call(
                "link_ticket",
                {
                    **common,
                    "ticket_key": ticket["key"],
                    "relation": "decided_by",
                    "target_kind": "message",
                    "target_ref": "999999",
                },
            )
        assert "link_target_not_found" in str(refusal.value)

    _boot()
    asyncio.run(scenario())


def test_blocks_stays_acyclic_and_reads_back_from_both_ends() -> None:
    """The reverse direction is derived, never stored, so the two cannot disagree."""

    async def scenario() -> None:
        project_key = pkey("links-cycle")
        token, _ = await _fixture(project_key)
        common = {
            "project_key": project_key,
            "agent_name": REPORTER,
            "registration_token": token,
        }
        first = await _call("create_ticket", {**common, "title": "one"})
        second = await _call("create_ticket", {**common, "title": "two"})

        await _call(
            "link_ticket",
            {
                **common,
                "ticket_key": first["key"],
                "relation": "blocks",
                "target_kind": "ticket",
                "target_ref": second["key"],
            },
        )

        with pytest.raises(ToolError) as refusal:
            await _call(
                "link_ticket",
                {
                    **common,
                    "ticket_key": second["key"],
                    "relation": "blocks",
                    "target_kind": "ticket",
                    "target_ref": first["key"],
                },
            )
        assert "link_cycle" in str(refusal.value)

        # Control: the same pair under a non-acyclic relation is accepted.
        await _call(
            "link_ticket",
            {
                **common,
                "ticket_key": second["key"],
                "relation": "relates",
                "target_kind": "ticket",
                "target_ref": first["key"],
            },
        )

        blocked = await _call("get_ticket", {**common, "ticket_key": second["key"]})
        directions = {(link["direction"], link["relation"]) for link in blocked["links"]}
        assert ("incoming", "blocks") in directions, "what blocks this ticket must be visible"
        assert ("outgoing", "relates") in directions

    _boot()
    asyncio.run(scenario())


def test_a_comment_is_a_message_on_the_opaque_thread() -> None:
    """No private comment table: the discussion is mail, so it reaches inboxes."""

    async def scenario() -> None:
        project_key = pkey("mail-comment")
        reporter_token, assignee_token = await _fixture(project_key)
        common = {
            "project_key": project_key,
            "agent_name": REPORTER,
            "registration_token": reporter_token,
        }
        ticket = await _call(
            "create_ticket",
            {**common, "title": "Needs discussion", "assignee_name": ASSIGNEE, "priority": 0},
        )

        posted = await _call(
            "comment_ticket",
            {
                **common,
                "ticket_key": ticket["key"],
                "body_md": "I started on this.",
                "idempotency_key": "comment-1",
            },
        )
        assert posted["recipients"] == [ASSIGNEE]
        assert posted["thread_id"] == ticket["discussion_thread_id"]
        assert posted["thread_id"] != ticket["key"]

        # It lands in the assignee's inbox, which is the whole point of reusing mail.
        inbox = await _call(
            "fetch_inbox",
            {
                "project_key": project_key,
                "agent_name": ASSIGNEE,
                "registration_token": assignee_token,
                "include_bodies": True,
            },
        )
        subjects = [message["subject"] for message in inbox]
        assert any(ticket["key"] in subject for subject in subjects)

        # And it is tagged with the readable key, so fetch_topic finds it.
        by_topic = await _call(
            "fetch_topic",
            {
                "project_key": project_key,
                "topic_name": ticket["key"],
                "agent_name": REPORTER,
                "registration_token": reporter_token,
            },
        )
        assert len(by_topic) >= 1

        with_discussion = await _call(
            "get_ticket",
            {**common, "ticket_key": ticket["key"], "include_discussion": True, "include_events": True},
        )
        assert [entry["body_md"] for entry in with_discussion["discussion"]] == [
            "I started on this."
        ]
        commented = [
            event for event in with_discussion["events"] if event["event_type"] == "commented"
        ]
        assert len(commented) == 1
        # The durable delivery id, not None. `send_message` returns
        # {"deliveries": [...]}, not a bare message, and reading it wrongly would record
        # every comment as having happened with no way to find it again -- a failure that
        # leaves the event row present and useless.
        assert commented[0]["new_value"], "the commented event lost its delivery id"
        assert commented[0]["new_value"] != "None"

    _boot()
    asyncio.run(scenario())


def test_a_retried_comment_reuses_its_delivery_but_a_new_key_does_not() -> None:
    """The control that proves the idempotency key is the caller's, not content-derived."""

    async def scenario() -> None:
        project_key = pkey("mail-idempotent")
        token, _ = await _fixture(project_key)
        common = {
            "project_key": project_key,
            "agent_name": REPORTER,
            "registration_token": token,
        }
        ticket = await _call(
            "create_ticket", {**common, "title": "retried", "assignee_name": ASSIGNEE}
        )
        body = {"ticket_key": ticket["key"], "body_md": "same words"}

        first = await _call("comment_ticket", {**common, **body, "idempotency_key": "c-1"})
        retry = await _call("comment_ticket", {**common, **body, "idempotency_key": "c-1"})
        def _mid(posted: dict[str, Any]) -> int:
            return int(posted["delivery"]["deliveries"][0]["message"]["id"])

        assert _mid(retry) == _mid(first)

        different = await _call("comment_ticket", {**common, **body, "idempotency_key": "c-2"})
        assert _mid(different) != _mid(first)

        discussion = await _call(
            "get_ticket",
            {**common, "ticket_key": ticket["key"], "include_discussion": True},
        )
        assert len(discussion["discussion"]) == 2, "the retry must not have posted twice"

    _boot()
    asyncio.run(scenario())


def test_a_reporter_commenting_on_an_unassigned_ticket_addresses_themselves() -> None:
    """Refusing would mean a ticket can only be discussed once somebody else is involved."""

    async def scenario() -> None:
        project_key = pkey("mail-solo")
        token, _ = await _fixture(project_key)
        common = {
            "project_key": project_key,
            "agent_name": REPORTER,
            "registration_token": token,
        }
        ticket = await _call("create_ticket", {**common, "title": "nobody else yet"})

        posted = await _call(
            "comment_ticket",
            {
                **common,
                "ticket_key": ticket["key"],
                "body_md": "note to self",
                "idempotency_key": "solo-1",
            },
        )
        assert posted["recipients"] == [REPORTER]
        assert "notification_error" not in posted

    _boot()
    asyncio.run(scenario())


def test_assignment_notifies_the_assignee_and_a_failure_does_not_undo_it() -> None:
    async def scenario() -> None:
        project_key = pkey("mail-assign")
        reporter_token, assignee_token = await _fixture(project_key)
        common = {
            "project_key": project_key,
            "agent_name": REPORTER,
            "registration_token": reporter_token,
        }
        ticket = await _call("create_ticket", {**common, "title": "to hand over", "priority": 1})

        updated = await _call(
            "update_ticket",
            {**common, "ticket_key": ticket["key"], "assignee_name": ASSIGNEE},
        )
        assert updated["notification"]["delivered"] is True
        assert updated["notification"]["to"] == ASSIGNEE

        inbox = await _call(
            "fetch_inbox",
            {
                "project_key": project_key,
                "agent_name": ASSIGNEE,
                "registration_token": assignee_token,
            },
        )
        # Priority 1 maps to the high band.
        assert any(message["importance"] == "high" for message in inbox)

        # An update that changes nothing else must not re-notify.
        again = await _call(
            "update_ticket",
            {**common, "ticket_key": ticket["key"], "assignee_name": ASSIGNEE},
        )
        assert "notification" not in again
        assert again["changed_fields"] == []

    _boot()
    asyncio.run(scenario())


def test_a_comment_returns_quickly_enough_to_prove_no_lock_is_held_across_the_send() -> None:
    """A regression that reintroduced holding the write transaction across delivery would
    show up as a stall against busy_timeout=60000, not as a wrong answer."""

    async def scenario() -> None:
        project_key = pkey("mail-timing")
        token, _ = await _fixture(project_key)
        common = {
            "project_key": project_key,
            "agent_name": REPORTER,
            "registration_token": token,
        }
        ticket = await _call(
            "create_ticket", {**common, "title": "timed", "assignee_name": ASSIGNEE}
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        await _call(
            "comment_ticket",
            {
                **common,
                "ticket_key": ticket["key"],
                "body_md": "timing probe",
                "idempotency_key": "timed-1",
            },
        )
        elapsed = loop.time() - started
        assert elapsed < 20, f"comment_ticket took {elapsed:.1f}s, far past any healthy send"

    _boot()
    asyncio.run(scenario())


def test_a_blocked_assignee_leaves_the_ticket_assigned() -> None:
    """The refusal is reported, never fatal to the update.

    An assignment that lands but cannot be announced is a far better outcome than one
    refused because the assignee's mailbox is closed. This also proves the notification
    really does travel the `send_message` path, contact policy and all.
    """

    async def scenario() -> None:
        project_key = pkey("mail-blocked")
        reporter_token, _ = await _fixture(project_key, link_contacts=False)
        common = {
            "project_key": project_key,
            "agent_name": REPORTER,
            "registration_token": reporter_token,
        }
        ticket = await _call("create_ticket", {**common, "title": "handover"})

        updated = await _call(
            "update_ticket",
            {**common, "ticket_key": ticket["key"], "assignee_name": ASSIGNEE},
        )
        assert updated["notification"]["delivered"] is False
        # The row still changed, which is the property that matters.
        assert updated["ticket"]["assignee_agent_id"] is not None
        assert "assignee_agent_id" in updated["changed_fields"]

        current = await _call("get_ticket", {**common, "ticket_key": ticket["key"]})
        assert current["ticket"]["assignee_agent_id"] is not None
    _boot()
    asyncio.run(scenario())
