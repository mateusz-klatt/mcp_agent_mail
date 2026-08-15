"""The contact-permission surface, end to end.

Four tools decide whether one agent may put a message in another's inbox:
``request_contact`` opens a directed, expiring approval request, ``respond_contact``
answers it, ``set_contact_policy`` sets the recipient's standing rule, and
``list_contacts`` reports the state a sender can act on. ``macro_contact_handshake``
composes the first two.

The properties worth guarding here are mostly *negative*, because every one of them
is a way to hand out a permission nobody granted:

* a repeated request must never shorten an active window, and must never downgrade
  an approval back to pending;
* a repeated request must never mint a second notification for a request that is
  still outstanding -- but must notify again once the old one has lapsed;
* a transient publish failure must leave one recoverable delivery intent, not two;
* denial must clear the expiry so no clock can resurrect it;
* an expired approval must stop reading as messageable;
* ``block_all`` must refuse the contact request itself, not only later traffic.

Where a send is used to prove that permission was granted or withheld, it passes
``auto_contact_if_blocked=False``. The server's default is to auto-handshake on a
blocked send, which in a single MCP session that holds both identities silently
approves the link -- so a send that succeeds with the default on proves nothing
about the approval under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import select as _sa_select
from sqlalchemy.orm import aliased

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session
from mcp_agent_mail.models import Agent, AgentLink, MessageDelivery, Project


def select(*entities: Any, **kwargs: Any) -> Any:
    """Keep SQLModel descriptor typing out of behaviour tests.

    Matches the shim already used in app.py, cli.py and test_agent_rename.py:
    `ty` cannot match SQLAlchemy's select/join overloads against SQLModel
    descriptors, so a column select or an ON clause reads as a plain bool and
    the gate fails on code that is correct. Widening here beats scattering
    ignores or rephrasing working queries to please a checker.
    """
    return _sa_select(*entities, **kwargs)

SENDER = "claude-wsl-contactspec-1"
RECEIVER = "claude-wsl-contactspec-2"
SOLO = "claude-wsl-contactspec-3"
NEAR = "claude-wsl-contactspec-4"
FAR = "claude-wsl-contactspec-5"

HOME = "/spec/contact/home"
AWAY = "/spec/contact/away"


# --------------------------------------------------------------------------
# fixtures and database probes
# --------------------------------------------------------------------------


@pytest.fixture
async def mail(isolated_env):
    """One in-process MCP client against a server with a private database."""
    async with Client(build_mcp_server()) as client:
        yield client


async def register(client, project_key: str, name: str) -> str:
    """Create the project if needed, register *name* in it, return its real name."""
    await client.call_tool("ensure_project", {"human_key": project_key})
    registered = await client.call_tool(
        "register_agent",
        {
            "project_key": project_key,
            "program": "test",
            "model": "test",
            "name": name,
        },
    )
    return registered.data["name"]


@pytest.fixture
async def duo(mail):
    """A sender and a receiver sharing one project."""
    return (
        await register(mail, HOME, SENDER),
        await register(mail, HOME, RECEIVER),
    )


def rows(result) -> list[dict]:
    """Rows returned by a list-shaped tool call."""
    payload = result.structured_content
    if isinstance(payload, dict):
        payload = payload.get("result")
    return [row for row in (payload or []) if isinstance(row, dict)]


def subjects(result) -> list[str]:
    """Every subject in an inbox listing, duplicates preserved."""
    return [str(row.get("subject", "")) for row in rows(result)]


async def read_inbox(client, project_key: str, agent_name: str):
    return await client.call_tool(
        "fetch_inbox",
        {
            "project_key": project_key,
            "agent_name": agent_name,
            "include_bodies": True,
        },
    )


async def agent_pk(project_key: str, agent_name: str) -> int | None:
    async with get_session() as session:
        found = await session.execute(
            select(Agent.id)
            .join(Project, Project.id == Agent.project_id)
            .where(Project.human_key == project_key, Agent.name == agent_name)
        )
        return found.scalars().first()


async def policy_of(project_key: str, agent_name: str) -> str | None:
    async with get_session() as session:
        found = await session.execute(
            select(Agent.contact_policy)
            .join(Project, Project.id == Agent.project_id)
            .where(Project.human_key == project_key, Agent.name == agent_name)
        )
        return found.scalars().first()


def _link_query(
    home: str,
    requester: str,
    target: str,
    away: str | None,
):
    side_a = aliased(Agent)
    side_a_project = aliased(Project)
    side_b = aliased(Agent)
    side_b_project = aliased(Project)
    return (
        select(AgentLink)
        .join(side_a_project, side_a_project.id == AgentLink.a_project_id)
        .join(side_a, side_a.id == AgentLink.a_agent_id)
        .join(side_b_project, side_b_project.id == AgentLink.b_project_id)
        .join(side_b, side_b.id == AgentLink.b_agent_id)
        .where(
            side_a_project.human_key == home,
            side_a.name == requester,
            side_b_project.human_key == (away or home),
            side_b.name == target,
        )
    )


async def stored_link(
    home: str,
    requester: str,
    target: str,
    away: str | None = None,
) -> dict | None:
    """The persisted directed link as plain values, or None when absent.

    Timestamps come back as the naive-UTC datetimes the column stores, so a test
    can compare two of them without reparsing anything.
    """
    async with get_session() as session:
        found = await session.execute(
            _link_query(home, requester, target, away)
        )
        link = found.scalars().first()
        if link is None:
            return None
        return {
            "id": link.id,
            "status": link.status,
            "reason": link.reason,
            "created_ts": link.created_ts,
            "updated_ts": link.updated_ts,
            "expires_ts": link.expires_ts,
        }


async def lapse_link(
    home: str,
    requester: str,
    target: str,
    away: str | None = None,
) -> None:
    """Push a link's expiry into the past, as a real clock eventually would."""
    stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    async with get_session() as session:
        found = await session.execute(
            _link_query(home, requester, target, away)
        )
        link = found.scalars().one()
        link.expires_ts = stale
        link.updated_ts = stale
        session.add(link)
        await session.commit()


async def make_delivery_due(delivery_id: str) -> None:
    """Make a pending delivery eligible for its next publish attempt now."""
    async with get_session() as session:
        delivery = await session.get(MessageDelivery, delivery_id)
        assert delivery is not None, f"no delivery row for {delivery_id}"
        delivery.next_attempt_ts = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=1
        )
        session.add(delivery)
        await session.commit()


def parse_iso(value: str) -> datetime:
    """A tool's ISO timestamp as a naive-UTC datetime, comparable with the column."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


# --------------------------------------------------------------------------
# opening a request
# --------------------------------------------------------------------------


async def test_request_creates_a_pending_link_and_names_both_ends(mail, duo):
    sender, receiver = duo

    opened = await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "reason": "pairing on the parser",
            "ttl_seconds": 3600,
        },
    )

    assert opened.data["status"] == "pending"
    assert opened.data["from"] == sender
    assert opened.data["to"] == receiver
    assert opened.data["from_project"] == HOME
    assert opened.data["to_project"] == HOME

    link = await stored_link(HOME, sender, receiver)
    assert link is not None, "the request must persist a link"
    assert link["status"] == "pending"
    assert link["reason"] == "pairing on the parser"
    assert parse_iso(opened.data["expires_ts"]) == link["expires_ts"]

    # The link is directed: nothing is created in the reverse direction.
    assert await stored_link(HOME, receiver, sender) is None


async def test_request_notifies_the_target_and_demands_an_ack(mail, duo):
    sender, receiver = duo

    opened = await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "reason": "need write access to the same branch",
        },
    )

    notification = opened.data["notification_message"]
    assert notification["delivery"]["status"] == "published"
    posted = notification["message"]
    assert posted["subject"] == f"Contact request from {sender}"
    assert posted["body_md"] == "need write access to the same branch"
    assert posted["ack_required"] is True

    delivered = await read_inbox(mail, HOME, receiver)
    assert subjects(delivered) == [f"Contact request from {sender}"]

    # The requester does not notify itself.
    assert subjects(await read_inbox(mail, HOME, sender)) == []


async def test_request_without_a_reason_still_explains_itself(mail, duo):
    sender, receiver = duo

    opened = await mail.call_tool(
        "request_contact",
        {"project_key": HOME, "from_agent": sender, "to_agent": receiver},
    )

    body = opened.data["notification_message"]["message"]["body_md"]
    assert sender in body and receiver in body


async def test_request_window_matches_the_requested_ttl(mail, duo):
    sender, receiver = duo
    week = 7 * 24 * 3600

    before = datetime.now(UTC).replace(tzinfo=None)
    await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "ttl_seconds": week,
        },
    )
    after = datetime.now(UTC).replace(tzinfo=None)

    link = await stored_link(HOME, sender, receiver)
    assert link is not None
    assert link["expires_ts"] is not None
    assert before + timedelta(seconds=week) <= link["expires_ts"]
    assert link["expires_ts"] <= after + timedelta(seconds=week)


async def test_request_lifts_a_sub_minute_ttl_to_the_one_minute_floor(mail, duo):
    sender, receiver = duo

    before = datetime.now(UTC).replace(tzinfo=None)
    await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "ttl_seconds": 1,
        },
    )

    link = await stored_link(HOME, sender, receiver)
    assert link is not None
    assert link["expires_ts"] is not None
    assert link["expires_ts"] >= before + timedelta(seconds=60)


# --------------------------------------------------------------------------
# self-contact
# --------------------------------------------------------------------------


async def test_request_refuses_self_contact_and_writes_nothing(mail):
    solo = await register(mail, HOME, SOLO)

    with pytest.raises(ToolError, match="self-contact"):
        await mail.call_tool(
            "request_contact",
            {"project_key": HOME, "from_agent": solo, "to_agent": solo},
        )

    assert await agent_pk(HOME, solo) is not None, "the agent itself must survive"
    assert await stored_link(HOME, solo, solo) is None
    assert subjects(await read_inbox(mail, HOME, solo)) == []


async def test_respond_refuses_self_contact_and_writes_nothing(mail):
    solo = await register(mail, HOME, SOLO)

    with pytest.raises(ToolError, match="self-contact"):
        await mail.call_tool(
            "respond_contact",
            {
                "project_key": HOME,
                "to_agent": solo,
                "from_agent": solo,
                "accept": True,
            },
        )

    assert await agent_pk(HOME, solo) is not None
    assert await stored_link(HOME, solo, solo) is None


# --------------------------------------------------------------------------
# repeating a request
# --------------------------------------------------------------------------


async def test_repeat_request_never_shortens_an_open_window(mail, duo):
    sender, receiver = duo

    first = await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "ttl_seconds": 3600,
        },
    )
    opened = await stored_link(HOME, sender, receiver)
    assert opened is not None and opened["expires_ts"] is not None

    second = await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "ttl_seconds": 60,
        },
    )

    assert first.data["status"] == "pending"
    assert second.data["status"] == "pending"
    assert parse_iso(second.data["expires_ts"]) >= parse_iso(first.data["expires_ts"])

    still_open = await stored_link(HOME, sender, receiver)
    assert still_open is not None
    assert still_open["id"] == opened["id"], "the same link must be reused"
    assert still_open["status"] == "pending"
    assert still_open["expires_ts"] == opened["expires_ts"]


async def test_repeat_request_does_not_mint_a_second_notification(mail, duo):
    sender, receiver = duo
    payload = {"project_key": HOME, "from_agent": sender, "to_agent": receiver}

    await mail.call_tool("request_contact", payload)
    repeated = await mail.call_tool("request_contact", payload)

    assert "notification_message" not in repeated.data
    assert subjects(await read_inbox(mail, HOME, receiver)) == [
        f"Contact request from {sender}"
    ]


async def test_request_notifies_again_once_the_window_has_lapsed(mail, duo):
    sender, receiver = duo

    await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "ttl_seconds": 60,
        },
    )
    await lapse_link(HOME, sender, receiver)

    renewed = await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "ttl_seconds": 3600,
        },
    )

    assert renewed.data["status"] == "pending"
    assert (
        renewed.data["notification_message"]["message"]["subject"]
        == f"Contact request from {sender}"
    )
    assert subjects(await read_inbox(mail, HOME, receiver)) == [
        f"Contact request from {sender}",
        f"Contact request from {sender}",
    ]

    revived = await stored_link(HOME, sender, receiver)
    assert revived is not None
    assert revived["status"] == "pending"
    assert revived["expires_ts"] is not None
    assert revived["expires_ts"] > datetime.now(UTC).replace(tzinfo=None)


async def test_repeat_request_leaves_a_live_approval_approved(mail, duo):
    sender, receiver = duo

    await mail.call_tool(
        "request_contact",
        {"project_key": HOME, "from_agent": sender, "to_agent": receiver},
    )
    await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": True,
        },
    )
    approved = await stored_link(HOME, sender, receiver)
    assert approved is not None and approved["expires_ts"] is not None

    repeated = await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "reason": "asking again by mistake",
            "ttl_seconds": 60,
        },
    )

    assert repeated.data["status"] == "approved", "a request must not undo an approval"
    assert repeated.data["expires_ts"] is not None
    assert "notification_message" not in repeated.data

    unchanged = await stored_link(HOME, sender, receiver)
    assert unchanged is not None
    assert unchanged["status"] == "approved"
    assert unchanged["expires_ts"] == approved["expires_ts"]

    assert subjects(await read_inbox(mail, HOME, receiver)) == [
        f"Contact request from {sender}"
    ]


# --------------------------------------------------------------------------
# the notification is a durable intent, not a side effect
# --------------------------------------------------------------------------


async def test_a_stalled_notification_is_resumed_rather_than_duplicated(
    mail, duo, monkeypatch
):
    """A publish that fails transiently leaves exactly one recoverable intent.

    The intro message is keyed on the link's identity and its immutable pending
    timestamp, so the retry that a caller naturally makes -- asking again --
    finishes the delivery already on record instead of queueing a second one.
    """
    from mcp_agent_mail import delivery as delivery_module
    from mcp_agent_mail.storage import MessageDeliveryPendingError

    sender, receiver = duo
    real_publisher = delivery_module.publish_message_delivery
    subject = f"Contact request from {sender}"

    async def stalls(archive, delivery_id, *args, **kwargs):
        raise MessageDeliveryPendingError(delivery_id, "storage briefly unavailable")

    monkeypatch.setattr(delivery_module, "publish_message_delivery", stalls)

    stalled = await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "reason": "first attempt cannot publish",
        },
    )

    assert stalled.data["status"] == "pending"
    stalled_delivery = stalled.data["notification_message"]["delivery"]
    assert stalled_delivery["status"] == "pending"
    assert stalled.data["notification_message"]["message"] is None
    assert subject not in subjects(await read_inbox(mail, HOME, receiver))

    await make_delivery_due(stalled_delivery["id"])
    monkeypatch.setattr(delivery_module, "publish_message_delivery", real_publisher)

    resumed = await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "reason": "second attempt after storage recovered",
        },
    )

    resumed_notification = resumed.data["notification_message"]
    assert resumed.data["status"] == "pending"
    assert resumed_notification["delivery"]["id"] == stalled_delivery["id"]
    assert resumed_notification["delivery"]["status"] == "published"
    assert resumed_notification["message"]["subject"] == subject
    assert subjects(await read_inbox(mail, HOME, receiver)) == [subject]


# --------------------------------------------------------------------------
# answering a request
# --------------------------------------------------------------------------


async def test_acceptance_approves_the_link_and_dates_it(mail, duo):
    sender, receiver = duo

    await mail.call_tool(
        "request_contact",
        {"project_key": HOME, "from_agent": sender, "to_agent": receiver},
    )
    accepted = await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": True,
            "ttl_seconds": 3600,
        },
    )

    assert accepted.data["approved"] is True
    assert accepted.data["updated"] == 1
    assert accepted.data["from"] == sender
    assert accepted.data["to"] == receiver

    link = await stored_link(HOME, sender, receiver)
    assert link is not None
    assert link["status"] == "approved"
    assert link["expires_ts"] == parse_iso(accepted.data["expires_ts"])


async def test_denial_blocks_the_link_and_drops_its_expiry(mail, duo):
    sender, receiver = duo

    await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": sender,
            "to_agent": receiver,
            "ttl_seconds": 3600,
        },
    )
    refused = await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": False,
        },
    )

    assert refused.data["approved"] is False
    assert refused.data["expires_ts"] is None

    link = await stored_link(HOME, sender, receiver)
    assert link is not None
    assert link["status"] == "blocked"
    # A denial that kept an expiry would quietly become an approval-shaped row
    # the moment some later code read "not expired" as "still fine".
    assert link["expires_ts"] is None


async def test_repeat_acceptance_never_shortens_a_live_approval(mail, duo):
    sender, receiver = duo

    await mail.call_tool(
        "request_contact",
        {"project_key": HOME, "from_agent": sender, "to_agent": receiver},
    )
    first = await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": True,
            "ttl_seconds": 3600,
        },
    )
    approved = await stored_link(HOME, sender, receiver)
    assert approved is not None

    second = await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": True,
            "ttl_seconds": 60,
        },
    )

    assert second.data["approved"] is True
    assert parse_iso(second.data["expires_ts"]) >= parse_iso(first.data["expires_ts"])

    unchanged = await stored_link(HOME, sender, receiver)
    assert unchanged is not None
    assert unchanged["status"] == "approved"
    assert unchanged["expires_ts"] == approved["expires_ts"]


async def test_answering_an_unasked_request_only_writes_on_acceptance(mail, duo):
    sender, receiver = duo

    refused = await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": False,
        },
    )
    assert refused.data["updated"] == 0
    assert await stored_link(HOME, sender, receiver) is None

    granted = await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": True,
        },
    )
    assert granted.data["updated"] == 1
    link = await stored_link(HOME, sender, receiver)
    assert link is not None
    assert link["status"] == "approved"


# --------------------------------------------------------------------------
# what the permission actually buys
# --------------------------------------------------------------------------


async def send(client, project_key, sender, receiver, key, *, auto_contact=False):
    return await client.call_tool(
        "send_message",
        {
            "project_key": project_key,
            "sender_name": sender,
            "to": [receiver],
            "subject": f"probe {key}",
            "body_md": "probe body",
            "idempotency_key": key,
            "auto_contact_if_blocked": auto_contact,
        },
    )


async def test_approval_is_what_lets_the_message_through(mail, duo):
    sender, receiver = duo
    await mail.call_tool(
        "set_contact_policy",
        {"project_key": HOME, "agent_name": receiver, "policy": "contacts_only"},
    )

    # Before approval, with the auto-handshake rescue switched off.
    with pytest.raises(ToolError, match="Contact approval required"):
        await send(mail, HOME, sender, receiver, "contactspec-before")

    await mail.call_tool(
        "request_contact",
        {"project_key": HOME, "from_agent": sender, "to_agent": receiver},
    )
    await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": True,
        },
    )

    delivered = await send(mail, HOME, sender, receiver, "contactspec-after")
    assert delivered.data["count"] == 1
    assert delivered.data["deliveries"][0]["message"]["subject"] == "probe contactspec-after"
    assert "probe contactspec-after" in subjects(await read_inbox(mail, HOME, receiver))


async def test_a_denied_sender_stays_out(mail, duo):
    sender, receiver = duo
    await mail.call_tool(
        "set_contact_policy",
        {"project_key": HOME, "agent_name": receiver, "policy": "contacts_only"},
    )
    await mail.call_tool(
        "request_contact",
        {"project_key": HOME, "from_agent": sender, "to_agent": receiver},
    )
    await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": False,
        },
    )

    with pytest.raises(ToolError) as refused:
        await send(mail, HOME, sender, receiver, "contactspec-denied")
    assert "Contact approval required" in str(refused.value)
    assert receiver in str(refused.value)

    assert "probe contactspec-denied" not in subjects(
        await read_inbox(mail, HOME, receiver)
    )


async def test_an_expired_approval_stops_letting_messages_through(mail, duo):
    sender, receiver = duo
    await mail.call_tool(
        "set_contact_policy",
        {"project_key": HOME, "agent_name": receiver, "policy": "contacts_only"},
    )
    await mail.call_tool(
        "request_contact",
        {"project_key": HOME, "from_agent": sender, "to_agent": receiver},
    )
    await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": True,
        },
    )
    await lapse_link(HOME, sender, receiver)

    with pytest.raises(ToolError, match="Contact approval required"):
        await send(mail, HOME, sender, receiver, "contactspec-lapsed")


async def test_open_policy_takes_messages_from_a_stranger(mail, duo):
    sender, receiver = duo

    set_open = await mail.call_tool(
        "set_contact_policy",
        {"project_key": HOME, "agent_name": receiver, "policy": "open"},
    )
    assert set_open.data == {"agent": receiver, "policy": "open"}
    assert await policy_of(HOME, receiver) == "open"

    delivered = await send(mail, HOME, sender, receiver, "contactspec-open")
    assert delivered.data["count"] == 1
    assert "probe contactspec-open" in subjects(await read_inbox(mail, HOME, receiver))
    assert await stored_link(HOME, sender, receiver) is None, (
        "an open recipient needs no contact link at all"
    )


async def test_block_all_refuses_the_request_itself_not_only_the_traffic(mail, duo):
    sender, receiver = duo
    await mail.call_tool(
        "set_contact_policy",
        {"project_key": HOME, "agent_name": receiver, "policy": "block_all"},
    )
    assert await policy_of(HOME, receiver) == "block_all"

    with pytest.raises(ToolError, match="not accepting messages"):
        await mail.call_tool(
            "request_contact",
            {"project_key": HOME, "from_agent": sender, "to_agent": receiver},
        )

    with pytest.raises(ToolError, match="not accepting messages"):
        await send(mail, HOME, sender, receiver, "contactspec-blockall")

    assert subjects(await read_inbox(mail, HOME, receiver)) == []


async def test_contact_policy_persists_and_can_be_replaced(mail):
    lone = await register(mail, HOME, SOLO)

    for chosen in ("contacts_only", "block_all", "auto", "open"):
        applied = await mail.call_tool(
            "set_contact_policy",
            {"project_key": HOME, "agent_name": lone, "policy": chosen},
        )
        assert applied.data["policy"] == chosen
        assert await policy_of(HOME, lone) == chosen


async def test_an_unknown_policy_is_refused_rather_than_coerced(mail):
    lone = await register(mail, HOME, SOLO)
    await mail.call_tool(
        "set_contact_policy",
        {"project_key": HOME, "agent_name": lone, "policy": "block_all"},
    )

    with pytest.raises(ToolError, match="Unknown contact policy"):
        await mail.call_tool(
            "set_contact_policy",
            {"project_key": HOME, "agent_name": lone, "policy": "block"},
        )

    # Silently reading "block" as "auto" would open a mailbox its owner shut.
    assert await policy_of(HOME, lone) == "block_all"


# --------------------------------------------------------------------------
# what the sender can see
# --------------------------------------------------------------------------


async def test_list_contacts_reports_a_live_approval_as_messageable(mail, duo):
    sender, receiver = duo

    await mail.call_tool(
        "request_contact",
        {"project_key": HOME, "from_agent": sender, "to_agent": receiver},
    )
    await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": True,
        },
    )

    listed = rows(
        await mail.call_tool(
            "list_contacts", {"project_key": HOME, "agent_name": sender}
        )
    )
    assert len(listed) == 1
    entry = listed[0]
    assert entry["to"] == receiver
    assert entry["to_project"] == HOME
    assert entry["status"] == "approved"
    assert entry["is_expired"] is False
    assert entry["allows_messaging"] is True

    # The listing follows the link's direction: the approver holds no link of
    # its own and must not inherit the requester's.
    assert (
        rows(
            await mail.call_tool(
                "list_contacts", {"project_key": HOME, "agent_name": receiver}
            )
        )
        == []
    )


async def test_list_contacts_stops_calling_a_lapsed_approval_messageable(mail, duo):
    sender, receiver = duo

    await mail.call_tool(
        "request_contact",
        {"project_key": HOME, "from_agent": sender, "to_agent": receiver},
    )
    await mail.call_tool(
        "respond_contact",
        {
            "project_key": HOME,
            "to_agent": receiver,
            "from_agent": sender,
            "accept": True,
        },
    )
    await lapse_link(HOME, sender, receiver)

    entry = rows(
        await mail.call_tool(
            "list_contacts", {"project_key": HOME, "agent_name": sender}
        )
    )[0]
    assert entry["status"] == "approved", "the stored answer is unchanged"
    assert entry["is_expired"] is True
    assert entry["allows_messaging"] is False


async def test_list_contacts_never_calls_a_pending_request_messageable(mail, duo):
    sender, receiver = duo

    await mail.call_tool(
        "request_contact",
        {"project_key": HOME, "from_agent": sender, "to_agent": receiver},
    )

    entry = rows(
        await mail.call_tool(
            "list_contacts", {"project_key": HOME, "agent_name": sender}
        )
    )[0]
    assert entry["status"] == "pending"
    assert entry["is_expired"] is False
    assert entry["allows_messaging"] is False


# --------------------------------------------------------------------------
# across projects
# --------------------------------------------------------------------------


@pytest.fixture
async def across(mail):
    """A requester in HOME and a target in AWAY."""
    return (
        await register(mail, HOME, NEAR),
        await register(mail, AWAY, FAR),
    )


async def test_a_request_can_cross_projects(mail, across):
    near, far = across

    opened = await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": near,
            "to_agent": far,
            "to_project": AWAY,
            "reason": "shared release branch",
        },
    )

    assert opened.data["status"] == "pending"
    assert opened.data["from_project"] == HOME
    assert opened.data["to_project"] == AWAY

    link = await stored_link(HOME, near, far, AWAY)
    assert link is not None
    assert link["status"] == "pending"

    # The request is addressed to the far project, not to a same-named local.
    assert await stored_link(HOME, near, far) is None


async def test_list_contacts_names_the_far_project(mail, across):
    near, far = across

    await mail.call_tool(
        "request_contact",
        {
            "project_key": HOME,
            "from_agent": near,
            "to_agent": far,
            "to_project": AWAY,
        },
    )

    entry = rows(
        await mail.call_tool(
            "list_contacts", {"project_key": HOME, "agent_name": near}
        )
    )[0]
    assert entry["to"] == far
    assert entry["to_project"] == AWAY
    assert entry["status"] == "pending"
    assert entry["allows_messaging"] is False


# --------------------------------------------------------------------------
# the macro
# --------------------------------------------------------------------------


async def test_handshake_without_auto_accept_only_asks(mail, duo):
    sender, receiver = duo

    shaken = await mail.call_tool(
        "macro_contact_handshake",
        {
            "project_key": HOME,
            "requester": sender,
            "target": receiver,
            "reason": "may I write here",
            "auto_accept": False,
        },
    )

    assert shaken.data["request"]["status"] == "pending"
    assert shaken.data["response"] is None, "nothing may answer on the target's behalf"
    assert shaken.data["welcome_message"] is None

    link = await stored_link(HOME, sender, receiver)
    assert link is not None
    assert link["status"] == "pending"
    assert subjects(await read_inbox(mail, HOME, receiver)) == [
        f"Contact request from {sender}"
    ]


async def test_handshake_with_auto_accept_approves_without_an_intro(mail, duo):
    sender, receiver = duo

    shaken = await mail.call_tool(
        "macro_contact_handshake",
        {
            "project_key": HOME,
            "requester": sender,
            "target": receiver,
            "auto_accept": True,
        },
    )

    assert shaken.data["request"]["status"] == "approved"
    assert shaken.data["response"]["status"] == "approved"

    link = await stored_link(HOME, sender, receiver)
    assert link is not None
    assert link["status"] == "approved"
    assert link["expires_ts"] is not None

    # The same-project fast path approves directly, so nobody is asked to read
    # a request that has already been granted.
    assert subjects(await read_inbox(mail, HOME, receiver)) == []


async def test_repeat_auto_accept_handshake_never_shortens_the_approval(mail, duo):
    sender, receiver = duo
    payload = {
        "project_key": HOME,
        "requester": sender,
        "target": receiver,
        "auto_accept": True,
    }

    first = await mail.call_tool(
        "macro_contact_handshake", {**payload, "ttl_seconds": 3600}
    )
    approved = await stored_link(HOME, sender, receiver)
    assert approved is not None

    second = await mail.call_tool(
        "macro_contact_handshake", {**payload, "ttl_seconds": 60}
    )

    assert first.data["response"]["status"] == "approved"
    assert second.data["response"]["status"] == "approved"
    assert parse_iso(second.data["response"]["expires_ts"]) >= parse_iso(
        first.data["response"]["expires_ts"]
    )

    unchanged = await stored_link(HOME, sender, receiver)
    assert unchanged is not None
    assert unchanged["status"] == "approved"
    assert unchanged["expires_ts"] == approved["expires_ts"]
