from __future__ import annotations

import contextlib
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.utils import slugify
from tests.keys import pkey

POLICY_AGENT_ONE = "codex-wsl-policy-1"
POLICY_AGENT_TWO = "codex-wsl-policy-2"
POLICY_AGENT_THREE = "codex-wsl-policy-3"


@pytest.mark.asyncio
async def test_contact_blocked_and_contacts_only(isolated_env, monkeypatch):
    # Ensure contact enforcement is enabled (it is by default, but be explicit)
    monkeypatch.setenv("CONTACT_ENFORCEMENT_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        for name in (POLICY_AGENT_ONE, POLICY_AGENT_TWO):
            await client.call_tool(
                "register_agent",
                {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": name},
            )

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

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_ONE},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_TWO},
        )
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
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool("ensure_project", {"human_key": pkey("frontend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_ONE},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Frontend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_TWO},
        )

        await client.call_tool(
            "request_contact",
            {
                "project_key": "Backend",
                "from_agent": POLICY_AGENT_ONE,
                "to_agent": f"project:Frontend#{POLICY_AGENT_TWO}",
            },
        )
        await client.call_tool(
            "respond_contact",
            {
                "project_key": "Frontend",
                "to_agent": POLICY_AGENT_TWO,
                "from_agent": POLICY_AGENT_ONE,
                "from_project": "Backend",
                "accept": True,
            },
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
        inbox_blocks = await client.read_resource(
            f"resource://inbox/{POLICY_AGENT_TWO}?project=Frontend&limit=10"
        )
        raw = inbox_blocks[0].text if inbox_blocks else "{}"
        data = json.loads(raw)
        assert any(item.get("subject") == "XProj" for item in data.get("messages", []))


@pytest.mark.asyncio
async def test_macro_contact_handshake_welcome(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_ONE},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_TWO},
        )

        res = await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": "Backend",
                "requester": POLICY_AGENT_ONE,
                "target": POLICY_AGENT_TWO,
                "reason": "let's sync",
                "auto_accept": True,
                "welcome_subject": "Welcome",
                "welcome_body": "nice to meet you",
            },
        )
        assert res.data.get("request")
        assert res.data.get("response")
        welcome = res.data.get("welcome_message") or {}
        # If the welcome ran, it will have deliveries
        if welcome:
            assert welcome.get("deliveries")


@pytest.mark.asyncio
async def test_macro_contact_handshake_rejects_partial_welcome(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_ONE},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_TWO},
        )

        with pytest.raises(Exception, match="welcome_subject and welcome_body"):
            await client.call_tool(
                "macro_contact_handshake",
                {
                    "project_key": "Backend",
                    "requester": POLICY_AGENT_ONE,
                    "target": POLICY_AGENT_TWO,
                    "auto_accept": True,
                    "welcome_subject": "Welcome only",
                },
            )


@pytest.mark.asyncio
async def test_macro_contact_handshake_rejects_welcome_without_auto_accept(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_ONE},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_TWO},
        )

        with pytest.raises(Exception, match="require auto_accept=True"):
            await client.call_tool(
                "macro_contact_handshake",
                {
                    "project_key": "Backend",
                    "requester": POLICY_AGENT_ONE,
                    "target": POLICY_AGENT_TWO,
                    "welcome_subject": "Welcome",
                    "welcome_body": "hello before approval",
                },
            )

        inbox_blocks = await client.read_resource(
            f"resource://inbox/{POLICY_AGENT_TWO}?project=Backend&limit=10"
        )
        raw = inbox_blocks[0].text if inbox_blocks else "{}"
        data = json.loads(raw)
        assert not data.get("messages")


@pytest.mark.asyncio
async def test_macro_contact_handshake_rejects_same_agent_same_project(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_TWO},
        )

        with pytest.raises(Exception, match="self-contact"):
            await client.call_tool(
                "macro_contact_handshake",
                {
                    "project_key": "Backend",
                    "requester": POLICY_AGENT_TWO,
                    "target": POLICY_AGENT_TWO,
                    "auto_accept": True,
                },
            )

        inbox_blocks = await client.read_resource(
            f"resource://inbox/{POLICY_AGENT_TWO}?project=Backend&limit=10"
        )
        raw = inbox_blocks[0].text if inbox_blocks else "{}"
        data = json.loads(raw)
        assert not data.get("messages")


@pytest.mark.asyncio
async def test_macro_contact_handshake_cross_project_welcome(isolated_env):
    backend = "/data/projects/backend"
    frontend = "/data/projects/frontend"
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": backend})
        await client.call_tool("ensure_project", {"human_key": frontend})
        await client.call_tool(
            "register_agent",
            {"project_key": backend, "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_ONE},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": frontend, "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_TWO},
        )

        res = await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": backend,
                "requester": POLICY_AGENT_ONE,
                "target": POLICY_AGENT_TWO,
                "to_project": frontend,
                "auto_accept": True,
                "welcome_subject": "Cross-project welcome",
                "welcome_body": "hello from backend",
            },
        )

        welcome = res.data.get("welcome_message") or {}
        assert welcome.get("deliveries"), res.data.get("welcome_error")

        inbox_blocks = await client.read_resource(
            f"resource://inbox/{POLICY_AGENT_TWO}?project=/data/projects/frontend&limit=10"
        )
        raw = inbox_blocks[0].text if inbox_blocks else "{}"
        data = json.loads(raw)
        assert any(item.get("subject") == "Cross-project welcome" for item in data.get("messages", []))


@pytest.mark.asyncio
async def test_macro_contact_handshake_auto_accept_requires_target_auth(isolated_env):
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": pkey("backend")})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_ONE},
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_TWO},
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]

    async with Client(server) as requester_client:
        res = await requester_client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": "Backend",
                "requester": POLICY_AGENT_ONE,
                "target": POLICY_AGENT_TWO,
                "auto_accept": True,
                "welcome_subject": "Welcome",
                "welcome_body": "this should not send yet",
                "requester_registration_token": green_token,
            },
        )

        assert res.data["request"]["status"] == "pending"
        assert not res.data["response"]
        response_error = res.data["response_error"]
        assert response_error["type"] == "AUTHENTICATION_REQUIRED"
        assert response_error["token_param"] == "target_registration_token"
        assert not res.data["welcome_message"]
        welcome_error = res.data["welcome_error"]
        assert welcome_error["type"] == "CONTACT_APPROVAL_REQUIRED"

        contacts = await requester_client.call_tool(
            "list_contacts",
            {
                "project_key": "Backend",
                "agent_name": POLICY_AGENT_ONE,
                "registration_token": green_token,
            },
        )
        contact_items = contacts.structured_content["result"]
        pending = next(item for item in contact_items if item["to"] == POLICY_AGENT_TWO)
        assert pending["status"] == "pending"
        assert not pending["allows_messaging"]

    async with Client(server) as recipient_client:
        inbox = await recipient_client.call_tool(
            "fetch_inbox",
            {
                "project_key": "Backend",
                "agent_name": POLICY_AGENT_TWO,
                "registration_token": blue_token,
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        subjects = {item["subject"] for item in messages}
        assert f"Contact request from {POLICY_AGENT_ONE}" in subjects
        assert "Welcome" not in subjects


@pytest.mark.asyncio
async def test_macro_contact_handshake_reuses_existing_approval_without_target_auth(isolated_env):
    server = build_mcp_server()
    async with Client(server) as bootstrap_client:
        await bootstrap_client.call_tool("ensure_project", {"human_key": pkey("backend-reuse-approved")})
        green = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("backend-reuse-approved"),
                "program": "codex",
                "model": "gpt-5",
                "name": POLICY_AGENT_ONE,
            },
        )
        blue = await bootstrap_client.call_tool(
            "register_agent",
            {
                "project_key": pkey("backend-reuse-approved"),
                "program": "codex",
                "model": "gpt-5",
                "name": POLICY_AGENT_TWO,
            },
        )
        green_token = green.data["registration_token"]
        blue_token = blue.data["registration_token"]

        await bootstrap_client.call_tool(
            "request_contact",
            {
                "project_key": pkey("backend-reuse-approved"),
                "from_agent": POLICY_AGENT_ONE,
                "to_agent": POLICY_AGENT_TWO,
                "registration_token": green_token,
            },
        )
        await bootstrap_client.call_tool(
            "respond_contact",
            {
                "project_key": pkey("backend-reuse-approved"),
                "to_agent": POLICY_AGENT_TWO,
                "from_agent": POLICY_AGENT_ONE,
                "accept": True,
                "registration_token": blue_token,
            },
        )

    async with Client(server) as requester_client:
        res = await requester_client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": pkey("backend-reuse-approved"),
                "requester": POLICY_AGENT_ONE,
                "target": POLICY_AGENT_TWO,
                "auto_accept": True,
                "welcome_subject": "Welcome back",
                "welcome_body": "existing approval should be enough",
                "requester_registration_token": green_token,
            },
        )

        assert res.data["request"]["status"] == "approved"
        assert res.data["response"]["status"] == "approved"
        assert "response_error" not in res.data
        welcome = res.data["welcome_message"] or {}
        assert welcome.get("deliveries"), res.data.get("welcome_error")

    async with Client(server) as recipient_client:
        inbox = await recipient_client.call_tool(
            "fetch_inbox",
            {
                "project_key": pkey("backend-reuse-approved"),
                "agent_name": POLICY_AGENT_TWO,
                "registration_token": blue_token,
                "include_bodies": True,
            },
        )
        messages = inbox.structured_content["result"]
        assert any(item["subject"] == "Welcome back" for item in messages)


@pytest.mark.asyncio
async def test_macro_contact_handshake_requires_registered_target(isolated_env):
    backend = "/data/projects/backend"
    frontend = "/data/projects/frontend"
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": backend})
        await client.call_tool("ensure_project", {"human_key": frontend})
        await client.call_tool(
            "register_agent",
            {"project_key": backend, "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_ONE},
        )

        with pytest.raises(ToolError, match="target must self-register"):
            await client.call_tool(
                "macro_contact_handshake",
                {
                    "project_key": backend,
                    "requester": POLICY_AGENT_ONE,
                    "target": POLICY_AGENT_THREE,
                    "to_project": frontend,
                    "auto_accept": True,
                },
            )

        agents_blocks = await client.read_resource(f"resource://agents/{slugify(frontend)}")
        raw = agents_blocks[0].text if agents_blocks else "{}"
        data = json.loads(raw)
        names = {agent.get("name") for agent in data.get("agents", [])}
        assert POLICY_AGENT_THREE not in names


@pytest.mark.asyncio
async def test_request_contact_requires_registered_target(isolated_env):
    backend = "/data/projects/backend"
    frontend = "/data/projects/frontend"
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": backend})
        await client.call_tool("ensure_project", {"human_key": frontend})
        await client.call_tool(
            "register_agent",
            {"project_key": backend, "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_ONE},
        )

        with pytest.raises(ToolError, match="target must self-register"):
            await client.call_tool(
                "request_contact",
                {
                    "project_key": backend,
                    "from_agent": POLICY_AGENT_ONE,
                    "to_agent": POLICY_AGENT_THREE,
                    "to_project": frontend,
                },
            )

        agents_blocks = await client.read_resource(f"resource://agents/{slugify(frontend)}")
        raw = agents_blocks[0].text if agents_blocks else "{}"
        data = json.loads(raw)
        names = {agent.get("name") for agent in data.get("agents", [])}
        assert POLICY_AGENT_THREE not in names


@pytest.mark.asyncio
async def test_send_message_supports_at_address(isolated_env):
    backend = "/data/projects/smartedgar_mcp"
    frontend = "/data/projects/smartedgar_mcp_frontend"
    frontend_slug = slugify(frontend)
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": backend})
        await client.call_tool("ensure_project", {"human_key": frontend})
        await client.call_tool(
            "register_agent",
            {"project_key": backend, "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_ONE},
        )
        await client.call_tool(
            "register_agent",
            {"project_key": frontend, "program": "codex", "model": "gpt-5", "name": POLICY_AGENT_TWO},
        )

        await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": backend,
                "requester": POLICY_AGENT_ONE,
                "target": POLICY_AGENT_TWO,
                "to_project": frontend,
                "auto_accept": True,
            },
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
