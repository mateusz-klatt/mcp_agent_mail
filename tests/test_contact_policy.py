"""Contact gating: who may write to whom, and what the gate must never leak.

Two mechanisms share this file because they answer the same question from
opposite ends.

``contact_policy`` is the *standing* answer -- ``block_all`` refuses, ``open``
admits, ``contacts_only`` refuses strangers but stands aside for agents already
working together (an approved link, or overlapping file reservations).

``macro_contact_handshake`` is the *negotiated* answer: request, approval and an
optional welcome message in one call. Its failure modes are what most of these
tests are about, because a handshake that half-succeeds is worse than one that
refuses. Approval belongs to the target, so a requester holding only its own
token must not be able to approve on the target's behalf; and every refusal
below is checked twice -- once for the error, once for the target's inbox --
because an error that still delivered the welcome would pass a single-assertion
test.

Registering an agent also authenticates the calling MCP session for it. Several
tests therefore register through one client and then act through a second: the
second session begins as a stranger holding only the tokens it was handed.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError
from mcp.types import TextResourceContents

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.utils import slugify
from tests.keys import pkey

POLICY_AGENT_ONE = "codex-wsl-policy-1"
POLICY_AGENT_TWO = "codex-wsl-policy-2"
POLICY_AGENT_THREE = "codex-wsl-policy-3"


@asynccontextmanager
async def _fresh_session() -> AsyncIterator[Client]:
    """One client session against a newly built server.

    Tests that need to prove authentication is *not* inherited build the server
    themselves and open two sessions on it; everything else uses this.
    """
    async with Client(build_mcp_server()) as client:
        yield client


async def _handshake(
    client: Client,
    project_key: str,
    requester: str,
    target: str,
    **options: Any,
) -> dict[str, Any]:
    """Run ``macro_contact_handshake`` and return its payload.

    The three positional arguments are the ones every call supplies; the
    optional halves of the negotiation -- ``auto_accept``, the welcome pair,
    ``to_project``, tokens -- are what each test is actually varying, so they
    stay visible at the call site.
    """
    result = await client.call_tool(
        "macro_contact_handshake",
        {"project_key": project_key, "requester": requester, "target": target, **options},
    )
    return result.data


async def _request_contact(
    client: Client,
    project_key: str,
    requester: str,
    target: str,
    **options: Any,
) -> dict[str, Any]:
    """Open a contact request from ``requester`` to ``target``."""
    result = await client.call_tool(
        "request_contact",
        {"project_key": project_key, "from_agent": requester, "to_agent": target, **options},
    )
    return result.data


async def _respond_contact(
    client: Client,
    project_key: str,
    target: str,
    requester: str,
    accept: bool,
    **options: Any,
) -> dict[str, Any]:
    """Answer a pending request as ``target``. Argument order mirrors the tool's."""
    result = await client.call_tool(
        "respond_contact",
        {
            "project_key": project_key,
            "to_agent": target,
            "from_agent": requester,
            "accept": accept,
            **options,
        },
    )
    return result.data


async def _contact_entry(
    client: Client,
    project_key: str,
    agent: str,
    token: str,
    peer: str,
) -> dict[str, Any]:
    """The single contact-list row describing ``agent``'s link to ``peer``."""
    contacts = await client.call_tool(
        "list_contacts",
        {"project_key": project_key, "agent_name": agent, "registration_token": token},
    )
    return next(item for item in _rows(contacts) if item["to"] == peer)


async def _enrol(client: Client, project_key: str, name: str) -> str:
    """Register ``name`` under ``project_key`` and return its registration token."""
    registered = await client.call_tool(
        "register_agent",
        {"project_key": project_key, "program": "codex", "model": "gpt-5", "name": name},
    )
    return registered.data["registration_token"]


def _rows(result: CallToolResult) -> list[Any]:
    """The ``result`` list a structured tool response carries.

    Asserted rather than defaulted: a tool that answered with no structured
    content at all is a failure to report, not an empty list to iterate.
    """
    payload = result.structured_content
    assert payload is not None, "expected a structured tool response"
    return payload["result"]


async def _resource_payload(client: Client, uri: str) -> dict[str, Any]:
    """Decode a resource read into its JSON object, or ``{}`` when it is empty.

    Binary blocks are skipped rather than coerced: every resource this suite
    reads is JSON text, so a blob turning up would mean the wrong resource
    answered, and an empty payload fails the caller's assertion clearly.
    """
    blocks = await client.read_resource(uri)
    text = "".join(b.text for b in blocks if isinstance(b, TextResourceContents))
    return json.loads(text or "{}")


async def _inbox_subjects(client: Client, project_key: str, agent: str, token: str) -> set[str]:
    """Subjects sitting in ``agent``'s inbox, read with the agent's own token."""
    inbox = await client.call_tool(
        "fetch_inbox",
        {
            "project_key": project_key,
            "agent_name": agent,
            "registration_token": token,
            "include_bodies": True,
        },
    )
    return {item["subject"] for item in _rows(inbox)}


async def _visible_subjects(client: Client, project_key: str, agent: str) -> set[str]:
    """Subjects in ``agent``'s inbox as the session-authenticated resource sees them."""
    payload = await _resource_payload(
        client, f"resource://inbox/{agent}?project={project_key}&limit=10"
    )
    return {item.get("subject") for item in payload.get("messages", [])}


@pytest.mark.asyncio
async def test_contact_blocked_and_contacts_only(isolated_env, monkeypatch):
    # Ensure contact enforcement is enabled (it is by default, but be explicit)
    monkeypatch.setenv("CONTACT_ENFORCEMENT_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    async with _fresh_session() as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        for name in (POLICY_AGENT_ONE, POLICY_AGENT_TWO):
            await _enrol(client, "Backend", name)

        # Beta blocks all
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": POLICY_AGENT_TWO, "policy": "block_all"},
        )
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": POLICY_AGENT_ONE,
                    "to": [POLICY_AGENT_TWO],
                    "subject": "Hi",
                    "body_md": "ping",
                    "idempotency_key": "contact-policy-block-all",
                },
            )
        assert "Recipient is not accepting messages" in str(excinfo.value)

        # Beta requires contacts_only
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": POLICY_AGENT_TWO, "policy": "contacts_only"},
        )
        r2 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": POLICY_AGENT_ONE,
                "to": [POLICY_AGENT_TWO],
                "subject": "Hi",
                "body_md": "ping",
                "idempotency_key": "contact-policy-contacts-only",
            },
        )
        deliveries = r2.data.get("deliveries") or []
        assert deliveries and deliveries[0]["message"]["subject"] == "Hi"


@pytest.mark.asyncio
async def test_contact_auto_allows_file_reservation_overlap(isolated_env, monkeypatch):
    # contacts_only with overlapping file reservations should auto-allow
    monkeypatch.setenv("CONTACT_ENFORCEMENT_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    async with _fresh_session() as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await _enrol(client, "Backend", POLICY_AGENT_ONE)
        await _enrol(client, "Backend", POLICY_AGENT_TWO)
        await client.call_tool(
            "set_contact_policy",
            {"project_key": "Backend", "agent_name": POLICY_AGENT_TWO, "policy": "contacts_only"},
        )

        # Overlapping file reservations: Alpha holds src/*, Beta holds src/app.py
        g1 = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": POLICY_AGENT_ONE,
                "paths": ["src/*"],
                "ttl_seconds": 600,
                "exclusive": True,
            },
        )
        assert g1.data["granted"]
        g2 = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": POLICY_AGENT_TWO,
                "paths": ["src/app.py"],
                "ttl_seconds": 600,
                "exclusive": True,
            },
        )
        assert g2.data["granted"]

        ok = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": POLICY_AGENT_ONE,
                "to": [POLICY_AGENT_TWO],
                "subject": "Heuristic",
                "body_md": "file reservations overlap allows",
                "idempotency_key": "contact-policy-reservation-overlap",
            },
        )
        assert ok.data.get("deliveries")


@pytest.mark.asyncio
async def test_cross_project_contact_and_delivery(isolated_env):
    async with _fresh_session() as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool("ensure_project", {"human_key": pkey("frontend")})
        await _enrol(client, "Backend", POLICY_AGENT_ONE)
        await _enrol(client, "Frontend", POLICY_AGENT_TWO)

        await _request_contact(
            client, "Backend", POLICY_AGENT_ONE, f"project:Frontend#{POLICY_AGENT_TWO}"
        )
        await _respond_contact(
            client,
            "Frontend",
            POLICY_AGENT_TWO,
            POLICY_AGENT_ONE,
            accept=True,
            from_project="Backend",
        )

        sent = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": POLICY_AGENT_ONE,
                "to": [f"project:Frontend#{POLICY_AGENT_TWO}"],
                "subject": "XProj",
                "body_md": "hello",
                "idempotency_key": "contact-policy-cross-project",
            },
        )
        deliveries = sent.data.get("deliveries") or []
        assert deliveries and any(d.get("project") in {"Frontend", pkey("frontend")} for d in deliveries)

        alternate = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": POLICY_AGENT_ONE,
                "to": [f"{POLICY_AGENT_TWO}@Frontend"],
                "subject": "XProj alternate address",
                "body_md": "hello again",
                "idempotency_key": "contact-policy-cross-project-alternate",
            },
        )
        alternate_deliveries = alternate.data.get("deliveries") or []
        assert alternate_deliveries and any(
            delivery.get("project") in {"Frontend", pkey("frontend")}
            for delivery in alternate_deliveries
        )

        # Verify appears in Frontend inbox
        assert "XProj" in await _visible_subjects(client, "Frontend", POLICY_AGENT_TWO)


@pytest.mark.asyncio
async def test_macro_contact_handshake_welcome(isolated_env):
    async with _fresh_session() as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await _enrol(client, "Backend", POLICY_AGENT_ONE)
        await _enrol(client, "Backend", POLICY_AGENT_TWO)

        res = await _handshake(
            client,
            "Backend",
            POLICY_AGENT_ONE,
            POLICY_AGENT_TWO,
            reason="let's sync",
            auto_accept=True,
            welcome_subject="Welcome",
            welcome_body="nice to meet you",
        )
        assert res.get("request")
        assert res.get("response")
        welcome = res.get("welcome_message") or {}
        # If the welcome ran, it will have deliveries
        if welcome:
            assert welcome.get("deliveries")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supplied", "withheld"),
    [("welcome_subject", "welcome_body"), ("welcome_body", "welcome_subject")],
    ids=["subject-without-body", "body-without-subject"],
)
async def test_handshake_refuses_half_a_welcome(isolated_env, supplied: str, withheld: str):
    """A welcome needs both halves, in either direction.

    Parametrised because the check is a symmetry (``one is None`` must equal
    ``the other is None``); testing a single direction would pass against an
    implementation that only ever noticed a missing body.
    """
    async with _fresh_session() as client:
        await client.call_tool("ensure_project", {"human_key": pkey("halfwelcome")})
        await _enrol(client, "halfwelcome", POLICY_AGENT_ONE)
        await _enrol(client, "halfwelcome", POLICY_AGENT_TWO)

        half = {supplied: f"only the {supplied}"}
        assert withheld not in half

        with pytest.raises(ToolError, match="welcome_subject and welcome_body"):
            await _handshake(
                client,
                "halfwelcome",
                POLICY_AGENT_ONE,
                POLICY_AGENT_TWO,
                auto_accept=True,
                **half,
            )


@pytest.mark.asyncio
async def test_handshake_refuses_welcome_before_approval(isolated_env):
    """Without ``auto_accept`` the macro cannot hold a welcome until approval, so it refuses.

    The inbox check is the point: a refusal that had already queued the welcome
    would satisfy ``pytest.raises`` on its own.
    """
    async with _fresh_session() as client:
        await client.call_tool("ensure_project", {"human_key": pkey("prematurewelcome")})
        await _enrol(client, "prematurewelcome", POLICY_AGENT_ONE)
        await _enrol(client, "prematurewelcome", POLICY_AGENT_TWO)

        with pytest.raises(ToolError, match="require auto_accept=True"):
            await _handshake(
                client,
                "prematurewelcome",
                POLICY_AGENT_ONE,
                POLICY_AGENT_TWO,
                welcome_subject="Greetings",
                welcome_body="sent before anyone approved it",
            )

        assert not await _visible_subjects(client, "prematurewelcome", POLICY_AGENT_TWO), (
            "the refused welcome must not have been delivered anyway"
        )


@pytest.mark.asyncio
async def test_handshake_refuses_self_contact(isolated_env):
    """An agent contacting itself in its own project is refused, and sends nothing.

    Self-messaging already works without a contact link, so approving one here
    would be a no-op that still writes an intro message into the mailbox.
    """
    async with _fresh_session() as client:
        await client.call_tool("ensure_project", {"human_key": pkey("selfcontact")})
        await _enrol(client, "selfcontact", POLICY_AGENT_TWO)

        with pytest.raises(ToolError, match="self-contact"):
            await _handshake(
                client,
                "selfcontact",
                POLICY_AGENT_TWO,
                POLICY_AGENT_TWO,
                auto_accept=True,
            )

        assert not await _visible_subjects(client, "selfcontact", POLICY_AGENT_TWO), (
            "a refused self-handshake must leave the mailbox untouched"
        )


@pytest.mark.asyncio
async def test_handshake_delivers_cross_project_welcome(isolated_env):
    """Across projects, an auto-accepted handshake carries its welcome through.

    Both agents were registered on this session, so the macro can authenticate
    each of them without being handed a token -- which is what separates this
    from the token test below.
    """
    origin = "/data/projects/backend"
    destination = "/data/projects/frontend"
    async with _fresh_session() as client:
        await client.call_tool("ensure_project", {"human_key": origin})
        await client.call_tool("ensure_project", {"human_key": destination})
        await _enrol(client, origin, POLICY_AGENT_ONE)
        await _enrol(client, destination, POLICY_AGENT_TWO)

        outcome = await _handshake(
            client,
            origin,
            POLICY_AGENT_ONE,
            POLICY_AGENT_TWO,
            to_project=destination,
            auto_accept=True,
            welcome_subject="Cross-project welcome",
            welcome_body="hello from the origin project",
        )

        welcome = outcome.get("welcome_message") or {}
        assert welcome.get("deliveries"), outcome.get("welcome_error")

        landed = await _visible_subjects(client, destination, POLICY_AGENT_TWO)
        assert "Cross-project welcome" in landed, (
            f"welcome never reached the destination inbox; saw {landed!r}"
        )


@pytest.mark.asyncio
async def test_auto_accept_needs_the_targets_own_credential(isolated_env):
    """A requester cannot approve on the target's behalf using only its own token.

    Everything downstream of approval has to stay unbuilt: the link stays
    ``pending``, ``list_contacts`` reports it as not yet permitting messages,
    and -- the assertion that matters most -- the welcome is absent from the
    target's inbox while the contact-request notice is present. Checking only
    the error would not distinguish "refused" from "refused, then sent anyway".
    """
    project = "targetauth"
    welcome_line = "Premature welcome"
    mail_server = build_mcp_server()

    async with Client(mail_server) as bootstrap:
        await bootstrap.call_tool("ensure_project", {"human_key": pkey(project)})
        requester_token = await _enrol(bootstrap, project, POLICY_AGENT_ONE)
        target_token = await _enrol(bootstrap, project, POLICY_AGENT_TWO)

    # A brand-new session: it holds no authentication from the registrations above.
    async with Client(mail_server) as as_requester:
        outcome = await _handshake(
            as_requester,
            project,
            POLICY_AGENT_ONE,
            POLICY_AGENT_TWO,
            auto_accept=True,
            welcome_subject=welcome_line,
            welcome_body="this must not be delivered yet",
            requester_registration_token=requester_token,
        )

        assert outcome["request"]["status"] == "pending"
        assert not outcome["response"]
        refusal = outcome["response_error"]
        assert refusal["type"] == "AUTHENTICATION_REQUIRED"
        assert refusal["token_param"] == "target_registration_token", (
            "the refusal must name the credential that was missing, not a generic one"
        )
        assert not outcome["welcome_message"]
        assert outcome["welcome_error"]["type"] == "CONTACT_APPROVAL_REQUIRED"

        entry = await _contact_entry(
            as_requester, project, POLICY_AGENT_ONE, requester_token, POLICY_AGENT_TWO
        )
        assert entry["status"] == "pending"
        assert not entry["allows_messaging"], (
            "a pending link must not be reported as permitting messages"
        )

    async with Client(mail_server) as as_target:
        subjects = await _inbox_subjects(as_target, project, POLICY_AGENT_TWO, target_token)
        assert f"Contact request from {POLICY_AGENT_ONE}" in subjects
        assert welcome_line not in subjects, "the welcome must wait for a real approval"


@pytest.mark.asyncio
async def test_auto_accept_reuses_a_standing_approval(isolated_env):
    """Once contact is already approved, the target's token is no longer needed.

    The mirror of the test above: the same call, from a session with the same
    single token, now succeeds -- because approval already exists rather than
    because the check was skipped. ``response_error`` must be absent entirely,
    not merely falsy, since a present-but-empty error would mean the macro took
    the failure path and papered over it.
    """
    project = pkey("standing-approval")
    welcome_line = "Second time around"
    mail_server = build_mcp_server()

    async with Client(mail_server) as bootstrap:
        await bootstrap.call_tool("ensure_project", {"human_key": project})
        requester_token = await _enrol(bootstrap, project, POLICY_AGENT_ONE)
        target_token = await _enrol(bootstrap, project, POLICY_AGENT_TWO)

        # Approval the ordinary way: each side acts under its own credential.
        await _request_contact(
            bootstrap,
            project,
            POLICY_AGENT_ONE,
            POLICY_AGENT_TWO,
            registration_token=requester_token,
        )
        await _respond_contact(
            bootstrap,
            project,
            POLICY_AGENT_TWO,
            POLICY_AGENT_ONE,
            accept=True,
            registration_token=target_token,
        )

    async with Client(mail_server) as as_requester:
        outcome = await _handshake(
            as_requester,
            project,
            POLICY_AGENT_ONE,
            POLICY_AGENT_TWO,
            auto_accept=True,
            welcome_subject=welcome_line,
            welcome_body="the standing approval should carry this through",
            requester_registration_token=requester_token,
        )

        assert outcome["request"]["status"] == "approved"
        assert outcome["response"]["status"] == "approved"
        assert "response_error" not in outcome
        welcome = outcome["welcome_message"] or {}
        assert welcome.get("deliveries"), outcome.get("welcome_error")

    async with Client(mail_server) as as_target:
        subjects = await _inbox_subjects(as_target, project, POLICY_AGENT_TWO, target_token)
        assert welcome_line in subjects


@pytest.mark.asyncio
async def test_macro_contact_handshake_requires_registered_target(isolated_env):
    backend = "/data/projects/backend"
    frontend = "/data/projects/frontend"
    async with _fresh_session() as client:
        await client.call_tool("ensure_project", {"human_key": backend})
        await client.call_tool("ensure_project", {"human_key": frontend})
        await _enrol(client, backend, POLICY_AGENT_ONE)

        with pytest.raises(ToolError, match="target must self-register"):
            await _handshake(
                client,
                backend,
                POLICY_AGENT_ONE,
                POLICY_AGENT_THREE,
                to_project=frontend,
                auto_accept=True,
            )

        data = await _resource_payload(client, f"resource://agents/{slugify(frontend)}")
        names = {agent.get("name") for agent in data.get("agents", [])}
        assert POLICY_AGENT_THREE not in names


@pytest.mark.asyncio
async def test_request_contact_refuses_an_unregistered_target(isolated_env):
    """``request_contact`` will not conjure the mailbox it is asked to contact.

    The roster check is the real assertion. Refusing the request but leaving a
    placeholder row behind would hand the requester an addressable name that
    nobody is reading -- the same silent loss the routing suite guards against
    from the delivery side.
    """
    origin = "/data/projects/backend"
    destination = "/data/projects/frontend"
    async with _fresh_session() as client:
        await client.call_tool("ensure_project", {"human_key": origin})
        await client.call_tool("ensure_project", {"human_key": destination})
        await _enrol(client, origin, POLICY_AGENT_ONE)

        with pytest.raises(ToolError, match="target must self-register"):
            await _request_contact(
                client,
                origin,
                POLICY_AGENT_ONE,
                POLICY_AGENT_THREE,
                to_project=destination,
            )

        roster = await _resource_payload(client, f"resource://agents/{slugify(destination)}")
        enrolled = {agent.get("name") for agent in roster.get("agents", [])}
        assert POLICY_AGENT_THREE not in enrolled, (
            f"a refused contact request must not provision the target; roster={enrolled!r}"
        )


@pytest.mark.asyncio
async def test_send_message_supports_at_address(isolated_env):
    backend = "/data/projects/smartedgar_mcp"
    frontend = "/data/projects/smartedgar_mcp_frontend"
    frontend_slug = slugify(frontend)
    async with _fresh_session() as client:
        await client.call_tool("ensure_project", {"human_key": backend})
        await client.call_tool("ensure_project", {"human_key": frontend})
        await _enrol(client, backend, POLICY_AGENT_ONE)
        await _enrol(client, frontend, POLICY_AGENT_TWO)

        await _handshake(
            client,
            backend,
            POLICY_AGENT_ONE,
            POLICY_AGENT_TWO,
            to_project=frontend,
            auto_accept=True,
        )

        response = await client.call_tool(
            "send_message",
            {
                "project_key": backend,
                "sender_name": POLICY_AGENT_ONE,
                "to": [f"{POLICY_AGENT_TWO}@{frontend_slug}"],
                "subject": "AT Route",
                "body_md": "hello",
                "idempotency_key": "contact-policy-at-address",
            },
        )
        deliveries = response.data.get("deliveries") or []
        assert deliveries and any(item.get("project") == frontend for item in deliveries)
