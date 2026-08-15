"""What the messaging tools promise: threads, receipts, contact gates, routing.

Every test here drives the public FastMCP surface, because that is the only
place the promises are actually made. Where a string is part of the contract --
an error phrase a caller matches on, a field name, a status value -- it is
written out literally; where a property is merely *implied* by a return value,
this file asserts the property itself instead. So "the auto-contact path does
not queue the payload" is checked by looking in the recipient's inbox for the
subject, not by trusting that an exception implies nothing was written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import func, select as _sa_select

import mcp_agent_mail.delivery as delivery_service
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_db_health_status, get_session
from mcp_agent_mail.models import (
    Agent,
    AgentLink,
    Message,
    MessageDelivery,
    MessageRecipient,
    Project,
)
from mcp_agent_mail.storage import MessageDeliveryPendingError
from tests.keys import pkey


def select(*entities: Any, **kwargs: Any) -> Any:
    """Keep SQLModel descriptor typing out of behaviour tests.

    Matches the shim already used in app.py, cli.py and test_agent_rename.py:
    `ty` cannot match SQLAlchemy's select/join overloads against SQLModel
    descriptors, so a column select or an ON clause reads as a plain bool and
    the gate fails on code that is correct. Widening here beats scattering
    ignores or rephrasing working queries to please a checker.
    """
    return _sa_select(*entities, **kwargs)

# One registration profile for the whole file: nothing here depends on program
# or model, and spelling them out at 40 call sites hides the arguments that do
# matter.
_ENROLMENT = {"program": "codex", "model": "gpt-5"}


@dataclass(frozen=True)
class Workspace:
    """A project as its two callers know it: the human key, and the slug."""

    key: str
    slug: str


@dataclass(frozen=True)
class Mailbox:
    """A registered agent plus everything a later call needs to act as it."""

    workspace: Workspace
    name: str
    token: str

    @property
    def project(self) -> str:
        return self.workspace.key


@pytest.fixture
def server(isolated_env):
    """A server bound to this test's own database and storage root."""
    return build_mcp_server()


async def _call(client: Client, tool: str, **arguments: Any) -> Any:
    """Invoke a tool and hand back its structured payload."""
    return (await client.call_tool(tool, arguments)).data


async def _listing(client: Client, tool: str, **arguments: Any) -> list[dict[str, Any]]:
    """Tools whose payload is a bare JSON array arrive wrapped under ``result``."""
    payload = (await client.call_tool(tool, arguments)).structured_content
    # Loud rather than empty: a tool that answered with no structured content at
    # all is a different failure from one that answered with an empty list, and
    # `or {}` would quietly merge the two.
    assert payload is not None, f"{tool} returned no structured content"
    return payload["result"]


async def _resource(client: Client, uri: str) -> dict[str, Any]:
    blocks = await client.read_resource(uri)
    # read_resource returns text OR blob contents; only the first carries `.text`.
    text = getattr(blocks[0], "text", None)
    assert text is not None, f"{uri} returned binary contents, expected JSON text"
    return json.loads(text or "{}")


async def _workspace(client: Client, label: str) -> Workspace:
    payload = await _call(client, "ensure_project", human_key=pkey(label))
    return Workspace(key=payload["human_key"], slug=payload["slug"])


async def _enrol(client: Client, workspace: Workspace, name: str) -> Mailbox:
    payload = await _call(
        client, "register_agent", project_key=workspace.key, name=name, **_ENROLMENT
    )
    return Mailbox(
        workspace=workspace,
        name=payload["name"],
        token=payload["registration_token"],
    )


async def _link_contacts(client: Client, requester: Mailbox, target: Mailbox) -> None:
    """Approve a contact link in one step, with both sides authenticating."""
    arguments: dict[str, Any] = {
        "project_key": requester.project,
        "requester": requester.name,
        "target": target.name,
        "auto_accept": True,
        "requester_registration_token": requester.token,
        "target_registration_token": target.token,
    }
    if target.project != requester.project:
        arguments["to_project"] = target.project
    await _call(client, "macro_contact_handshake", **arguments)


async def _age_out_approvals_held_by(sender: Mailbox) -> int:
    """Push this sender's outbound approvals past their TTL; return how many.

    Directional on purpose. Expiring every link in the store would work just as
    well for making a send fail, but then a refusal would no longer say *which*
    direction the check consults -- the reverse link is deliberately left live
    so it cannot stand in for the one that lapsed.

    The return value is the control: a helper that quietly matched no rows would
    leave the approval intact and make every test below pass for the opposite
    reason, so each one asserts this count before it asserts anything else.
    """
    stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    touched = 0
    async with get_session() as session:
        keys = {
            project.id: project.human_key
            for project in (await session.execute(select(Project))).scalars().all()
        }
        outbound = {
            agent.id
            for agent in (await session.execute(select(Agent))).scalars().all()
            if agent.name == sender.name and keys.get(agent.project_id) == sender.project
        }
        links = (await session.execute(select(AgentLink))).scalars().all()
        for link in links:
            if link.status != "approved" or link.a_agent_id not in outbound:
                continue
            link.expires_ts = stale
            link.updated_ts = stale
            session.add(link)
            touched += 1
        await session.commit()
    return touched


def _first_message(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["deliveries"][0]["message"]


def _subjects(items: list[dict[str, Any]]) -> list[str]:
    return [item["subject"] for item in items]


def _contact_pairs(items: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(item["to"], item["status"]) for item in items}


# ----------------------------------------------------------------------------
# Threading
# ----------------------------------------------------------------------------


async def test_reply_threads_under_its_parent_and_prefixes_the_subject_once(server):
    async with Client(server) as client:
        space = await _workspace(client, "thread-shape")
        author = await _enrol(client, space, "codex-linux-alfa-1")

        parent = await _call(
            client,
            "send_message",
            project_key=space.key,
            sender_name=author.name,
            to=[author.name],
            subject="Migration plan",
            body_md="the original",
            idempotency_key="thread-shape-parent",
        )
        parent_message = _first_message(parent)
        parent_id = int(parent_message["id"])
        thread = parent_message["thread_id"] or str(parent_id)

        first = await _call(
            client,
            "reply_message",
            project_key=space.key,
            message_id=parent_id,
            sender_name=author.name,
            body_md="the first reply",
            idempotency_key="thread-shape-reply-1",
        )
        assert first["thread_id"] == thread
        assert first["reply_to"] == parent_id
        assert _first_message(first)["subject"] == "Re: Migration plan"

        # Replying to the reply must neither fork the thread nor stack a second
        # prefix onto a subject that already carries one.
        second = await _call(
            client,
            "reply_message",
            project_key=space.key,
            message_id=int(_first_message(first)["id"]),
            sender_name=author.name,
            body_md="the second reply",
            idempotency_key="thread-shape-reply-2",
        )
        assert second["thread_id"] == thread
        assert _first_message(second)["subject"] == "Re: Migration plan"


async def test_reply_with_an_explicitly_empty_to_list_reaches_nobody(server):
    async with Client(server) as client:
        space = await _workspace(client, "reply-no-recipients")
        author = await _enrol(client, space, "codex-linux-alfa-1")
        opener = await _enrol(client, space, "codex-linux-bravo-1")

        seed = await _call(
            client,
            "send_message",
            project_key=space.key,
            sender_name=opener.name,
            to=[author.name],
            subject="Question",
            body_md="ping",
            idempotency_key="reply-none-seed",
        )
        seed_message = _first_message(seed)
        seed_id = int(seed_message["id"])

        reply = await _call(
            client,
            "reply_message",
            project_key=space.key,
            message_id=seed_id,
            sender_name=author.name,
            to=[],
            body_md="addressed to nobody on purpose",
            idempotency_key="reply-none-child",
        )
        assert reply["count"] == 0
        assert reply["deliveries"] == []
        assert reply["thread_id"] == (seed_message["thread_id"] or str(seed_id))
        assert reply["reply_to"] == seed_id

        # An empty list is a decision, not an omission: the original sender must
        # not be reinstated as a default recipient behind the caller's back.
        opener_inbox = await _listing(
            client,
            "fetch_inbox",
            project_key=space.key,
            agent_name=opener.name,
            registration_token=opener.token,
            include_bodies=True,
        )
        assert opener_inbox == []


# ----------------------------------------------------------------------------
# Read receipts and acknowledgements
# ----------------------------------------------------------------------------


async def test_reading_and_acknowledging_keep_separate_timestamps(server):
    async with Client(server) as client:
        space = await _workspace(client, "receipts")
        author = await _enrol(client, space, "codex-linux-alfa-1")
        reader = await _enrol(client, space, "codex-linux-bravo-1")

        sent = await _call(
            client,
            "send_message",
            project_key=space.key,
            sender_name=author.name,
            to=[reader.name],
            subject="Please confirm",
            body_md="details inside",
            ack_required=True,
            idempotency_key="receipts-seed",
        )
        message_id = int(_first_message(sent)["id"])

        read = await _call(
            client,
            "mark_message_read",
            project_key=space.key,
            agent_name=reader.name,
            message_id=message_id,
            registration_token=reader.token,
        )
        assert read["message_id"] == message_id
        assert read["read"] is True
        read_at = read["read_at"]
        assert isinstance(read_at, str) and read_at

        acknowledged = await _call(
            client,
            "acknowledge_message",
            project_key=space.key,
            agent_name=reader.name,
            message_id=message_id,
            registration_token=reader.token,
        )
        assert acknowledged["acknowledged"] is True
        assert isinstance(acknowledged["acknowledged_at"], str)
        assert acknowledged["acknowledged_at"]
        # Acknowledging also reports the read receipt, and must not overwrite the
        # earlier one with the time of the acknowledgement.
        assert acknowledged["read_at"] == read_at


async def test_acknowledging_twice_keeps_the_first_timestamp(server):
    async with Client(server) as client:
        space = await _workspace(client, "ack-idempotence")
        author = await _enrol(client, space, "codex-linux-alfa-1")
        reader = await _enrol(client, space, "codex-linux-bravo-1")

        sent = await _call(
            client,
            "send_message",
            project_key=space.key,
            sender_name=author.name,
            to=[reader.name],
            subject="Confirm twice",
            body_md="details inside",
            ack_required=True,
            idempotency_key="ack-idempotence-seed",
        )
        message_id = int(_first_message(sent)["id"])

        acknowledgements = [
            await _call(
                client,
                "acknowledge_message",
                project_key=space.key,
                agent_name=reader.name,
                message_id=message_id,
                registration_token=reader.token,
            )
            for _ in range(2)
        ]
        assert [entry["acknowledged"] for entry in acknowledgements] == [True, True]
        assert acknowledgements[0]["acknowledged_at"] == acknowledgements[1]["acknowledged_at"]
        assert acknowledgements[0]["acknowledged_at"]


# ----------------------------------------------------------------------------
# Sender authentication
# ----------------------------------------------------------------------------


async def test_a_second_session_cannot_send_as_an_agent_without_its_token(server):
    async with Client(server) as enrolment:
        space = await _workspace(enrolment, "sender-auth")
        owner = await _enrol(enrolment, space, "codex-linux-alfa-1")

    forged = {
        "project_key": space.key,
        "sender_name": owner.name,
        "to": [owner.name],
        "subject": "Impersonation attempt",
        "body_md": "written by somebody else",
        "idempotency_key": "sender-auth-forged",
    }
    async with Client(server) as impostor:
        with pytest.raises(ToolError) as refusal:
            await impostor.call_tool("send_message", forged)
        # The parameter name is what the caller has to supply next, so it is the
        # part of the message that is contract.
        assert "sender_token" in str(refusal.value)

    async with Client(server) as owner_session:
        accepted = await _call(
            owner_session,
            "send_message",
            project_key=space.key,
            sender_name=owner.name,
            sender_token=owner.token,
            to=[owner.name],
            subject="Genuine",
            body_md="written by the owner",
            idempotency_key="sender-auth-genuine",
        )
        assert accepted["verified_sender"] is True
        assert accepted["count"] == 1

        inbox = await _listing(
            owner_session,
            "fetch_inbox",
            project_key=space.key,
            agent_name=owner.name,
            registration_token=owner.token,
        )
        # The refused call left nothing behind.
        assert _subjects(inbox) == ["Genuine"]


# ----------------------------------------------------------------------------
# Automatic contact resolution, within one project
# ----------------------------------------------------------------------------


async def test_blocked_send_with_auto_contact_files_a_request_and_drops_the_payload(server):
    async with Client(server) as enrolment:
        space = await _workspace(enrolment, "auto-contact-pending")
        caller = await _enrol(enrolment, space, "codex-linux-alfa-1")
        callee = await _enrol(enrolment, space, "codex-linux-bravo-1")

    # Deliberately a session the callee has never authenticated in: the
    # in-session shortcut is what separates this path from auto-approval.
    async with Client(server) as caller_session:
        with pytest.raises(ToolError) as refusal:
            await caller_session.call_tool(
                "send_message",
                {
                    "project_key": space.key,
                    "sender_name": caller.name,
                    "sender_token": caller.token,
                    "to": [callee.name],
                    "subject": "Design review",
                    "body_md": "please take a look",
                    "auto_contact_if_blocked": True,
                    "idempotency_key": "auto-contact-pending",
                },
            )
        assert f"Pending contact requests were created for: {callee.name}" in str(refusal.value)

        outstanding = await _listing(
            caller_session,
            "list_contacts",
            project_key=space.key,
            agent_name=caller.name,
            registration_token=caller.token,
        )
        assert (callee.name, "pending") in _contact_pairs(outstanding)

    async with Client(server) as callee_session:
        inbox = await _listing(
            callee_session,
            "fetch_inbox",
            project_key=space.key,
            agent_name=callee.name,
            registration_token=callee.token,
            include_bodies=True,
        )
        subjects = _subjects(inbox)
        assert f"Contact request from {caller.name}" in subjects
        # The request is a request, not a delivery: the body must not have been
        # queued behind it.
        assert "Design review" not in subjects


async def test_auto_contact_disabled_leaves_no_request_and_no_notice(server):
    async with Client(server) as enrolment:
        space = await _workspace(enrolment, "auto-contact-off")
        caller = await _enrol(enrolment, space, "codex-linux-alfa-1")
        callee = await _enrol(enrolment, space, "codex-linux-bravo-1")

    async with Client(server) as caller_session:
        with pytest.raises(ToolError) as refusal:
            await caller_session.call_tool(
                "send_message",
                {
                    "project_key": space.key,
                    "sender_name": caller.name,
                    "sender_token": caller.token,
                    "to": [callee.name],
                    "subject": "Design review",
                    "body_md": "please take a look",
                    "auto_contact_if_blocked": False,
                    "idempotency_key": "auto-contact-off",
                },
            )
        # False must override the server default, which creates requests.
        assert "Pending contact requests were created" not in str(refusal.value)

        outstanding = await _listing(
            caller_session,
            "list_contacts",
            project_key=space.key,
            agent_name=caller.name,
            registration_token=caller.token,
        )
        assert (callee.name, "pending") not in _contact_pairs(outstanding)

    async with Client(server) as callee_session:
        inbox = await _listing(
            callee_session,
            "fetch_inbox",
            project_key=space.key,
            agent_name=callee.name,
            registration_token=callee.token,
            include_bodies=True,
        )
        assert f"Contact request from {caller.name}" not in _subjects(inbox)


async def test_auto_contact_approves_in_band_when_both_agents_share_the_session(server):
    async with Client(server) as client:
        space = await _workspace(client, "auto-contact-in-session")
        caller = await _enrol(client, space, "codex-linux-alfa-1")
        callee = await _enrol(client, space, "codex-linux-bravo-1")

        sent = await _call(
            client,
            "send_message",
            project_key=space.key,
            sender_name=caller.name,
            to=[callee.name],
            subject="Same-session handshake",
            body_md="this one goes through",
            auto_contact_if_blocked=True,
            idempotency_key="auto-contact-in-session",
        )
        assert sent["count"] == 1

        contacts = await _listing(
            client, "list_contacts", project_key=space.key, agent_name=caller.name
        )
        assert (callee.name, "approved") in _contact_pairs(contacts)

        inbox = await _listing(
            client,
            "fetch_inbox",
            project_key=space.key,
            agent_name=callee.name,
            include_bodies=True,
        )
        assert "Same-session handshake" in _subjects(inbox)


# ----------------------------------------------------------------------------
# Automatic contact resolution, across projects
# ----------------------------------------------------------------------------


async def test_cross_project_auto_contact_files_an_external_request_and_drops_the_payload(server):
    async with Client(server) as enrolment:
        home = await _workspace(enrolment, "xproj-pending-home")
        away = await _workspace(enrolment, "xproj-pending-away")
        caller = await _enrol(enrolment, home, "codex-linux-alfa-1")
        callee = await _enrol(enrolment, away, "codex-linux-bravo-1")

    async with Client(server) as caller_session:
        with pytest.raises(ToolError) as refusal:
            await caller_session.call_tool(
                "send_message",
                {
                    "project_key": home.key,
                    "sender_name": caller.name,
                    "sender_token": caller.token,
                    "to": [f"{callee.name}@{away.key}"],
                    "subject": "Cross-project ask",
                    "body_md": "we need a link first",
                    "auto_contact_if_blocked": True,
                    "idempotency_key": "xproj-pending",
                },
            )
        assert (
            f"pending external contact requests were created for {callee.name}@{away.key}"
            in str(refusal.value)
        )

        outstanding = await _listing(
            caller_session,
            "list_contacts",
            project_key=home.key,
            agent_name=caller.name,
            registration_token=caller.token,
        )
        assert (callee.name, "pending") in _contact_pairs(outstanding)

    async with Client(server) as callee_session:
        inbox = await _listing(
            callee_session,
            "fetch_inbox",
            project_key=away.key,
            agent_name=callee.name,
            registration_token=callee.token,
            include_bodies=True,
        )
        subjects = _subjects(inbox)
        assert f"Contact request from {caller.name}" in subjects
        assert "Cross-project ask" not in subjects


async def test_cross_project_auto_contact_disabled_leaves_no_request_and_no_notice(server):
    async with Client(server) as enrolment:
        home = await _workspace(enrolment, "xproj-off-home")
        away = await _workspace(enrolment, "xproj-off-away")
        caller = await _enrol(enrolment, home, "codex-linux-alfa-1")
        callee = await _enrol(enrolment, away, "codex-linux-bravo-1")

    async with Client(server) as caller_session:
        with pytest.raises(ToolError) as refusal:
            await caller_session.call_tool(
                "send_message",
                {
                    "project_key": home.key,
                    "sender_name": caller.name,
                    "sender_token": caller.token,
                    "to": [f"{callee.name}@{away.key}"],
                    "subject": "Cross-project ask",
                    "body_md": "we need a link first",
                    "auto_contact_if_blocked": False,
                    "idempotency_key": "xproj-off",
                },
            )
        assert "pending external contact requests were created" not in str(refusal.value)

        outstanding = await _listing(
            caller_session,
            "list_contacts",
            project_key=home.key,
            agent_name=caller.name,
            registration_token=caller.token,
        )
        assert (callee.name, "pending") not in _contact_pairs(outstanding)

    async with Client(server) as callee_session:
        inbox = await _listing(
            callee_session,
            "fetch_inbox",
            project_key=away.key,
            agent_name=callee.name,
            registration_token=callee.token,
            include_bodies=True,
        )
        assert f"Contact request from {caller.name}" not in _subjects(inbox)


async def test_in_session_cross_project_approval_preserves_the_recipient_kind(server):
    async with Client(server) as client:
        home = await _workspace(client, "xproj-kind-home")
        away = await _workspace(client, "xproj-kind-away")
        caller = await _enrol(client, home, "codex-linux-alfa-1")
        hidden = await _enrol(client, away, "codex-linux-bravo-1")

        sent = await _call(
            client,
            "send_message",
            project_key=home.key,
            sender_name=caller.name,
            to=[caller.name],
            bcc=[f"{hidden.name}@{away.key}"],
            subject="Quiet copy",
            body_md="the recipient kind has to survive the handshake",
            auto_contact_if_blocked=True,
            idempotency_key="xproj-kind-bcc",
        )
        assert sent["count"] == 2

        away_inbox = await _listing(
            client,
            "fetch_inbox",
            project_key=away.key,
            agent_name=hidden.name,
            include_bodies=True,
        )
        blind_copy = next(item for item in away_inbox if item["subject"] == "Quiet copy")
        assert blind_copy["kind"] == "bcc"
        assert blind_copy["body_md"] == "the recipient kind has to survive the handshake"

        home_inbox = await _listing(
            client,
            "fetch_inbox",
            project_key=home.key,
            agent_name=caller.name,
            include_bodies=True,
        )
        direct_copy = next(item for item in home_inbox if item["subject"] == "Quiet copy")
        assert direct_copy["kind"] == "to"


# ----------------------------------------------------------------------------
# Contact policy is enforced on replies, and approvals do expire
# ----------------------------------------------------------------------------


async def test_a_reply_cannot_add_a_recipient_that_contacts_only_would_block(server):
    async with Client(server) as client:
        space = await _workspace(client, "reply-contact-policy")
        author = await _enrol(client, space, "codex-linux-alfa-1")
        opener = await _enrol(client, space, "codex-linux-bravo-1")
        gated = await _enrol(client, space, "codex-linux-charlie-1")
        await _call(
            client,
            "set_contact_policy",
            project_key=space.key,
            agent_name=gated.name,
            policy="contacts_only",
        )

        seed = await _call(
            client,
            "send_message",
            project_key=space.key,
            sender_name=opener.name,
            to=[author.name],
            subject="Opening note",
            body_md="start of the thread",
            idempotency_key="reply-contact-policy-seed",
        )
        seed_id = int(_first_message(seed)["id"])

        with pytest.raises(ToolError) as refusal:
            await client.call_tool(
                "reply_message",
                {
                    "project_key": space.key,
                    "message_id": seed_id,
                    "sender_name": author.name,
                    "to": [gated.name],
                    "body_md": "looping in somebody who never approved me",
                    "idempotency_key": "reply-contact-policy-blocked",
                },
            )
        assert f"Contact approval required for recipients: {gated.name}" in str(refusal.value)

        gated_inbox = await _listing(
            client,
            "fetch_inbox",
            project_key=space.key,
            agent_name=gated.name,
            registration_token=gated.token,
        )
        assert gated_inbox == []


async def test_send_refuses_a_local_approval_whose_ttl_has_passed(server):
    async with Client(server) as client:
        space = await _workspace(client, "send-expired-local")
        caller = await _enrol(client, space, "codex-linux-alfa-1")
        gated = await _enrol(client, space, "codex-linux-bravo-1")
        await _call(
            client,
            "set_contact_policy",
            project_key=space.key,
            agent_name=gated.name,
            policy="contacts_only",
        )
        await _link_contacts(client, caller, gated)
        assert await _age_out_approvals_held_by(caller) >= 1

        with pytest.raises(ToolError) as refusal:
            await client.call_tool(
                "send_message",
                {
                    "project_key": space.key,
                    "sender_name": caller.name,
                    "to": [gated.name],
                    "subject": "Lapsed approval",
                    "body_md": "an expired approval must not authorize delivery",
                    "auto_contact_if_blocked": False,
                    "idempotency_key": "send-expired-local",
                },
            )
        assert f"Contact approval required for recipients: {gated.name}" in str(refusal.value)


async def test_reply_refuses_a_local_approval_whose_ttl_has_passed(server):
    async with Client(server) as client:
        space = await _workspace(client, "reply-expired-local")
        author = await _enrol(client, space, "codex-linux-alfa-1")
        opener = await _enrol(client, space, "codex-linux-bravo-1")
        gated = await _enrol(client, space, "codex-linux-charlie-1")
        await _call(
            client,
            "set_contact_policy",
            project_key=space.key,
            agent_name=gated.name,
            policy="contacts_only",
        )
        await _link_contacts(client, author, gated)
        assert await _age_out_approvals_held_by(author) >= 1

        # The gated agent is deliberately absent from this thread, so the
        # thread-participant shortcut cannot stand in for the lapsed approval.
        seed = await _call(
            client,
            "send_message",
            project_key=space.key,
            sender_name=opener.name,
            to=[author.name],
            subject="Opening note",
            body_md="start of the thread",
            idempotency_key="reply-expired-local-seed",
        )
        seed_id = int(_first_message(seed)["id"])

        with pytest.raises(ToolError) as refusal:
            await client.call_tool(
                "reply_message",
                {
                    "project_key": space.key,
                    "message_id": seed_id,
                    "sender_name": author.name,
                    "to": [gated.name],
                    "body_md": "an expired approval must not authorize a reply either",
                    "idempotency_key": "reply-expired-local-child",
                },
            )
        assert f"Contact approval required for recipients: {gated.name}" in str(refusal.value)


async def test_send_refuses_a_cross_project_approval_whose_ttl_has_passed(server):
    async with Client(server) as client:
        home = await _workspace(client, "send-expired-home")
        away = await _workspace(client, "send-expired-away")
        caller = await _enrol(client, home, "codex-linux-alfa-1")
        remote = await _enrol(client, away, "codex-linux-bravo-1")
        await _link_contacts(client, caller, remote)
        assert await _age_out_approvals_held_by(caller) >= 1

        with pytest.raises(ToolError) as refusal:
            await client.call_tool(
                "send_message",
                {
                    "project_key": home.key,
                    "sender_name": caller.name,
                    "sender_token": caller.token,
                    "to": [f"{remote.name}@{away.key}"],
                    "subject": "Stale external approval",
                    "body_md": "an expired link must not route mail across projects",
                    "auto_contact_if_blocked": False,
                    "idempotency_key": "send-expired-cross-project",
                },
            )
        assert (
            f"external recipients missing approved contact links: {remote.name} @ {away.key}"
            in str(refusal.value)
        )


# ----------------------------------------------------------------------------
# Cross-project routing and sender identity
# ----------------------------------------------------------------------------


async def test_reply_routes_to_an_approved_agent_at_project_address(server):
    async with Client(server) as client:
        home = await _workspace(client, "reply-xproj-home")
        away = await _workspace(client, "reply-xproj-away")
        author = await _enrol(client, home, "codex-linux-alfa-1")
        opener = await _enrol(client, home, "codex-linux-bravo-1")
        remote = await _enrol(client, away, "codex-linux-charlie-1")
        await _link_contacts(client, author, remote)

        seed = await _call(
            client,
            "send_message",
            project_key=home.key,
            sender_name=opener.name,
            to=[author.name],
            subject="Opening note",
            body_md="start of the thread",
            idempotency_key="reply-xproj-seed",
        )
        seed_id = int(_first_message(seed)["id"])

        reply = await _call(
            client,
            "reply_message",
            project_key=home.key,
            message_id=seed_id,
            sender_name=author.name,
            to=[f"{remote.name}@{away.key}"],
            body_md="taking this across the project boundary",
            idempotency_key="reply-xproj-child",
        )
        assert [entry["project"] for entry in reply["deliveries"]] == [away.key]

        remote_inbox = await _listing(
            client,
            "fetch_inbox",
            project_key=away.key,
            agent_name=remote.name,
            registration_token=remote.token,
            include_bodies=True,
        )
        landed = [item for item in remote_inbox if item["subject"] == "Re: Opening note"]
        assert [item["body_md"] for item in landed] == [
            "taking this across the project boundary"
        ]
        # Cross-project routing opens sessions against two archives; none may be
        # left checked out.
        assert get_db_health_status()["pool"]["checked_out"] == 0


async def test_cross_project_delivery_credits_the_sender_to_its_own_project(server):
    async with Client(server) as client:
        home = await _workspace(client, "xproj-origin-home")
        away = await _workspace(client, "xproj-origin-away")
        author = await _enrol(client, home, "codex-linux-alfa-1")
        remote = await _enrol(client, away, "codex-linux-bravo-1")
        await _link_contacts(client, author, remote)

        sent = await _call(
            client,
            "send_message",
            project_key=home.key,
            sender_name=author.name,
            sender_token=author.token,
            to=[f"{remote.name}@{away.key}"],
            subject="Origin check",
            body_md="sent from the home project",
            thread_id="XPROJ-ORIGIN-1",
            idempotency_key="xproj-origin",
        )
        landed_id = next(
            entry["message"]["id"]
            for entry in sent["deliveries"]
            if entry["project"] == away.key
        )

        inbox = await _listing(
            client,
            "fetch_inbox",
            project_key=away.key,
            agent_name=remote.name,
            registration_token=remote.token,
            include_bodies=True,
        )
        delivered = next(item for item in inbox if item["id"] == landed_id)
        assert delivered["from"] == author.name
        assert delivered["from_project"] == home.key
        assert delivered["from_address"] == f"project:{home.slug}#{author.name}"

        # The single-message resource must tell the same story as the inbox row.
        detail = await _resource(
            client,
            f"resource://message/{landed_id}?project={away.key}&agent={remote.name}",
        )
        assert detail["from"] == author.name
        assert detail["from_project"] == home.key


async def test_a_local_namesake_neither_owns_the_message_nor_catches_the_reply(server):
    async with Client(server) as client:
        home = await _workspace(client, "namesake-home")
        away = await _workspace(client, "namesake-away")
        author = await _enrol(client, home, "codex-linux-alfa-1")
        remote = await _enrol(client, away, "codex-linux-bravo-1")
        # Same durable name as the real sender, but a different project.
        namesake = await _enrol(client, away, "codex-linux-alfa-1")
        assert namesake.name == author.name
        await _call(
            client,
            "set_contact_policy",
            project_key=away.key,
            agent_name=namesake.name,
            policy="contacts_only",
        )
        await _link_contacts(client, author, remote)

        sent = await _call(
            client,
            "send_message",
            project_key=home.key,
            sender_name=author.name,
            sender_token=author.token,
            to=[f"{remote.name}@{away.key}"],
            subject="Origin check",
            body_md="sent from the home project",
            thread_id="NAMESAKE-1",
            idempotency_key="namesake-send",
        )
        landed_id = next(
            entry["message"]["id"]
            for entry in sent["deliveries"]
            if entry["project"] == away.key
        )

        # The namesake did not send it, so it must not appear in its outbox.
        outbox = await _resource(
            client, f"resource://outbox/{namesake.name}?project={away.key}"
        )
        assert outbox["count"] == 0

        # Nor may replying to the external sender resolve to the local namesake.
        with pytest.raises(ToolError) as refusal:
            await client.call_tool(
                "reply_message",
                {
                    "project_key": away.key,
                    "message_id": landed_id,
                    "sender_name": remote.name,
                    "sender_token": remote.token,
                    "to": [namesake.name],
                    "body_md": "aiming at the lookalike",
                    "idempotency_key": "namesake-lookalike-reply",
                },
            )
        assert f"Contact approval required for recipients: {namesake.name}" in str(refusal.value)

        reply = await _call(
            client,
            "reply_message",
            project_key=away.key,
            message_id=landed_id,
            sender_name=remote.name,
            sender_token=remote.token,
            body_md="answering the sender that actually wrote to me",
            idempotency_key="namesake-true-reply",
        )
        returned = next(
            entry["message"] for entry in reply["deliveries"] if entry["project"] == home.key
        )
        assert returned["from"] == remote.name
        assert returned["from_project"] == away.key
        assert get_db_health_status()["pool"]["checked_out"] == 0


# ----------------------------------------------------------------------------
# Delivery no longer touches the legacy per-agent mailbox tree
# ----------------------------------------------------------------------------


async def test_a_reservation_over_legacy_inbox_paths_does_not_stall_a_send(server):
    async with Client(server) as client:
        home = await _workspace(client, "legacy-send-home")
        away = await _workspace(client, "legacy-send-away")
        author = await _enrol(client, home, "codex-linux-alfa-1")
        remote = await _enrol(client, away, "codex-linux-bravo-1")
        squatter = await _enrol(client, away, "codex-linux-charlie-1")
        await _link_contacts(client, author, remote)

        held = await _call(
            client,
            "file_reservation_paths",
            project_key=away.key,
            agent_name=squatter.name,
            paths=[f"agents/{remote.name}/inbox/*/*/*.md"],
            ttl_seconds=1800,
            exclusive=True,
        )
        assert held["granted"]

        sent = await _call(
            client,
            "send_message",
            project_key=home.key,
            sender_name=author.name,
            sender_token=author.token,
            to=[author.name, f"{remote.name}@{away.key}"],
            subject="Two projects at once",
            body_md="immutable delivery does not go through those paths",
            idempotency_key="legacy-send",
        )
        assert sent["count"] == 2
        assert {entry["project"] for entry in sent["deliveries"]} == {home.key, away.key}
        assert [entry["delivery"]["status"] for entry in sent["deliveries"]] == [
            "published",
            "published",
        ]
        assert "delivery_errors" not in sent


async def test_a_reservation_over_legacy_inbox_paths_does_not_stall_a_reply(server):
    async with Client(server) as client:
        home = await _workspace(client, "legacy-reply-home")
        away = await _workspace(client, "legacy-reply-away")
        author = await _enrol(client, home, "codex-linux-alfa-1")
        remote = await _enrol(client, away, "codex-linux-bravo-1")
        squatter = await _enrol(client, home, "codex-linux-charlie-1")
        await _link_contacts(client, author, remote)

        seed = await _call(
            client,
            "send_message",
            project_key=home.key,
            sender_name=author.name,
            sender_token=author.token,
            to=[f"{remote.name}@{away.key}"],
            subject="Opening note across projects",
            body_md="start of the thread",
            idempotency_key="legacy-reply-seed",
        )
        landed_id = next(
            entry["message"]["id"]
            for entry in seed["deliveries"]
            if entry["project"] == away.key
        )

        held = await _call(
            client,
            "file_reservation_paths",
            project_key=home.key,
            agent_name=squatter.name,
            paths=[f"agents/{author.name}/inbox/*/*/*.md"],
            ttl_seconds=1800,
            exclusive=True,
        )
        assert held["granted"]

        reply = await _call(
            client,
            "reply_message",
            project_key=away.key,
            message_id=landed_id,
            sender_name=remote.name,
            sender_token=remote.token,
            body_md="the reply uses the immutable delivery document",
            idempotency_key="legacy-reply-child",
        )
        assert reply["count"] == 1
        assert reply["deliveries"][0]["project"] == home.key
        assert reply["deliveries"][0]["delivery"]["status"] == "published"
        assert "delivery_errors" not in reply


# ----------------------------------------------------------------------------
# Thread visibility: search and summaries follow the recipient list
# ----------------------------------------------------------------------------

_PRIVATE_THREAD = "SEM-PRIVATE-1"
_PRIVATE_PHRASE = "hydraulic-otter"


async def _stage_private_thread(server) -> tuple[Workspace, Mailbox, Mailbox]:
    """Seed a thread whose only non-sender recipient is on BCC.

    Returns the workspace, the blind-copied recipient, and a registered agent
    that was never party to it.
    """
    async with Client(server) as client:
        space = await _workspace(client, "private-thread")
        author = await _enrol(client, space, "codex-linux-alfa-1")
        blind = await _enrol(client, space, "codex-linux-bravo-1")
        stranger = await _enrol(client, space, "codex-linux-charlie-1")
        await _link_contacts(client, author, blind)
        await _call(
            client,
            "send_message",
            project_key=space.key,
            sender_name=author.name,
            sender_token=author.token,
            to=[author.name],
            bcc=[blind.name],
            subject="Sealed plan",
            body_md=f"the {_PRIVATE_PHRASE} launch sequence",
            thread_id=_PRIVATE_THREAD,
            idempotency_key="private-thread-seed",
        )
    return space, blind, stranger


async def test_a_blind_copied_recipient_can_search_and_summarize_the_thread(server):
    space, blind, _stranger = await _stage_private_thread(server)

    async with Client(server) as blind_session:
        hits = await _listing(
            blind_session,
            "search_messages",
            project_key=space.key,
            query=_PRIVATE_PHRASE,
            agent_name=blind.name,
            registration_token=blind.token,
        )
        assert len(hits) == 1

        summary = await _call(
            blind_session,
            "summarize_thread",
            project_key=space.key,
            thread_id=_PRIVATE_THREAD,
            include_examples=True,
            llm_mode=False,
            agent_name=blind.name,
            registration_token=blind.token,
        )
        assert summary["summary"]["total_messages"] == 1
        assert len(summary["examples"]) == 1


async def test_an_agent_outside_the_thread_can_neither_find_nor_summarize_it(server):
    space, _blind, stranger = await _stage_private_thread(server)

    async with Client(server) as stranger_session:
        hits = await _listing(
            stranger_session,
            "search_messages",
            project_key=space.key,
            query=_PRIVATE_PHRASE,
            agent_name=stranger.name,
            registration_token=stranger.token,
        )
        assert hits == []

        summary = await _call(
            stranger_session,
            "summarize_thread",
            project_key=space.key,
            thread_id=_PRIVATE_THREAD,
            include_examples=True,
            llm_mode=False,
            agent_name=stranger.name,
            registration_token=stranger.token,
        )
        assert summary["summary"]["total_messages"] == 0
        assert summary["examples"] == []


# ----------------------------------------------------------------------------
# A delivery that has not reached the archive is not a message yet
# ----------------------------------------------------------------------------


async def test_a_pending_archive_publication_exposes_no_message_row(server, monkeypatch):
    async def _never_publishes(_archive, delivery_id, *_args, **_kwargs):
        raise MessageDeliveryPendingError(delivery_id, "archive unavailable in this test")

    monkeypatch.setattr(delivery_service, "publish_message_delivery", _never_publishes)

    async with Client(server) as client:
        space = await _workspace(client, "archive-pending")
        author = await _enrol(client, space, "codex-linux-alfa-1")

        sent = await _call(
            client,
            "send_message",
            project_key=space.key,
            sender_name=author.name,
            to=[author.name],
            subject="Held back",
            body_md="this never reaches the archive",
            idempotency_key="archive-pending",
        )
        only = sent["deliveries"][0]
        assert only["delivery"]["status"] == "pending"
        assert only["message"] is None

        async with get_session() as session:
            rows = {}
            for model in (Message, MessageRecipient, MessageDelivery):
                result = await session.execute(select(func.count()).select_from(model))
                rows[model.__name__] = result.scalar_one()
        # The durable intent survives; nothing readable was created alongside it.
        assert rows == {"Message": 0, "MessageRecipient": 0, "MessageDelivery": 1}
