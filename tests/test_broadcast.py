"""Tests for broadcast topic threads (bd-26w).

Covers:
- Broadcast expansion to all agents (excluding sender)
- Broadcast with explicit recipients errors
- Broadcast respects contact_policy (block_all skipped)
- Topic field stored in messages
- Topic filtering in fetch_inbox
- fetch_topic tool
- Broadcast with topic combined
- Empty project broadcast (no recipients)
"""

from __future__ import annotations

import logging

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey

logger = logging.getLogger(__name__)


def _get_data(result):
    """Extract data dict from tool result (dict-returning tools like send_message)."""
    # FastMCP Client: structured_content["result"] gives plain Python objects
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        sc = result.structured_content.get("result")
        if isinstance(sc, dict):
            return sc
    if hasattr(result, "data") and isinstance(result.data, dict):
        return result.data
    if isinstance(result, dict):
        return result
    return getattr(result, "data", result)


def _get_list(result):
    """Extract list data from tool result (list-returning tools like fetch_inbox).

    Uses structured_content["result"] which returns plain dicts,
    unlike result.data which returns pydantic Root objects.
    """
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        sc = result.structured_content.get("result")
        if isinstance(sc, list):
            return sc
    return list(getattr(result, "data", result))


# ============================================================================
# Helper: register N agents in a project
# ============================================================================


async def _setup_project_with_agents(client, project_key: str, count: int) -> list[str]:
    """Register `count` agents in the project and return their names."""
    await client.call_tool("ensure_project", {"human_key": project_key})
    names = []
    for i in range(count):
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "test-prog",
                "model": "test-model",
                "task_description": f"agent-{i}",
                "name": f"codex-wsl-broadcast-{i + 1}",
            },
        )
        data = _get_data(result)
        names.append(data["name"])
    return names


# ============================================================================
# Broadcast expansion
# ============================================================================


@pytest.mark.asyncio
async def test_broadcast_expands_to_all_agents(isolated_env):
    """broadcast=true with empty 'to' should deliver to all agents except sender."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/bcast"), 4)
        sender = names[0]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/bcast"),
                "sender_name": sender,
                "to": [],
                "subject": "Hello everyone",
                "body_md": "Broadcast test",
                "broadcast": True,
                "idempotency_key": "broadcast-all-agents",
            },
        )
        data = _get_data(result)
        assert "deliveries" in data, f"Expected deliveries in result: {data}"
        payload = data["deliveries"][0]["message"]
        # All 3 other agents should be recipients
        recipients = payload.get("to", [])
        assert len(recipients) == 3, f"Expected 3 recipients, got {len(recipients)}: {recipients}"
        assert sender not in recipients, "Sender should not be in broadcast recipients"
        for name in names[1:]:
            assert name in recipients, f"Expected {name} in recipients"


@pytest.mark.asyncio
async def test_broadcast_with_explicit_recipients_errors(isolated_env):
    """broadcast=true with non-empty 'to' should raise an error."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/bcast-err"), 2)

        with pytest.raises(Exception, match="mutually exclusive"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": pkey("test/bcast-err"),
                    "sender_name": names[0],
                    "to": [names[1]],
                    "subject": "Should fail",
                    "body_md": "Error test",
                    "broadcast": True,
                    "idempotency_key": "broadcast-explicit-recipient-error",
                },
            )


@pytest.mark.asyncio
async def test_broadcast_excludes_sender(isolated_env):
    """Sender should never appear in their own broadcast recipient list."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/bcast-self"), 2)
        sender = names[0]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/bcast-self"),
                "sender_name": sender,
                "to": [],
                "subject": "Self-exclusion test",
                "body_md": "Should not include sender",
                "broadcast": True,
                "idempotency_key": "broadcast-exclude-sender",
            },
        )
        data = _get_data(result)
        payload = data["deliveries"][0]["message"]
        assert sender not in payload["to"]


@pytest.mark.asyncio
async def test_broadcast_respects_block_all_policy(isolated_env):
    """Agents with contact_policy=block_all should be excluded from broadcasts."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/bcast-block"), 3)
        sender = names[0]
        blocked_agent = names[1]

        # Set one agent to block_all
        await client.call_tool(
            "set_contact_policy",
            {
                "project_key": pkey("test/bcast-block"),
                "agent_name": blocked_agent,
                "policy": "block_all",
            },
        )

        result = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/bcast-block"),
                "sender_name": sender,
                "to": [],
                "subject": "Policy test",
                "body_md": "Should skip blocked agent",
                "broadcast": True,
                "idempotency_key": "broadcast-skip-blocked",
            },
        )
        data = _get_data(result)
        payload = data["deliveries"][0]["message"]
        recipients = payload["to"]
        assert blocked_agent not in recipients, "block_all agent should be excluded"
        assert len(recipients) == 1, f"Expected 1 recipient (excluding sender + blocked), got {len(recipients)}"


@pytest.mark.asyncio
async def test_broadcast_empty_project(isolated_env):
    """Broadcast with only sender registered should produce empty recipients."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/bcast-empty"), 1)
        sender = names[0]

        # broadcast with no other agents - should not error
        result = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/bcast-empty"),
                "sender_name": sender,
                "to": [],
                "subject": "Lonely broadcast",
                "body_md": "No one to receive this",
                "broadcast": True,
                "idempotency_key": "broadcast-empty-project",
            },
        )
        # With no recipients, _deliver_message should not be called (no deliveries)
        data = _get_data(result)
        deliveries = data.get("deliveries", [])
        assert len(deliveries) == 0, f"Expected 0 deliveries for solo agent, got {len(deliveries)}"


# ============================================================================
# Topic storage and filtering
# ============================================================================


@pytest.mark.asyncio
async def test_topic_stored_in_message(isolated_env):
    """Message with topic param should have topic field in DB and payload."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/topic-store"), 2)

        result = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/topic-store"),
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "Architecture discussion",
                "body_md": "Let's discuss",
                "topic": "architecture",
                "idempotency_key": "topic-store-architecture",
            },
        )
        data = _get_data(result)
        payload = data["deliveries"][0]["message"]
        assert payload.get("topic") == "architecture"


@pytest.mark.asyncio
async def test_topic_filtering_inbox(isolated_env):
    """fetch_inbox with topic filter returns only matching messages."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/topic-filter"), 2)
        sender, receiver = names[0], names[1]

        # Send messages with different topics
        await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/topic-filter"),
                "sender_name": sender,
                "to": [receiver],
                "subject": "Topic A",
                "body_md": "Message about blockers",
                "topic": "blockers",
                "idempotency_key": "topic-filter-blockers",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/topic-filter"),
                "sender_name": sender,
                "to": [receiver],
                "subject": "Topic B",
                "body_md": "Message about releases",
                "topic": "releases",
                "idempotency_key": "topic-filter-releases",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/topic-filter"),
                "sender_name": sender,
                "to": [receiver],
                "subject": "No topic",
                "body_md": "Regular message",
                "idempotency_key": "topic-filter-none",
            },
        )

        # Filter by topic=blockers
        result = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": pkey("test/topic-filter"),
                "agent_name": receiver,
                "topic": "blockers",
                "include_bodies": True,
            },
        )
        items = _get_list(result)
        assert len(items) == 1
        assert items[0]["subject"] == "Topic A"
        assert items[0]["topic"] == "blockers"


@pytest.mark.asyncio
async def test_topic_filtering_empty(isolated_env):
    """fetch_inbox with non-existent topic returns empty list."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/topic-empty"), 2)

        await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/topic-empty"),
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "A message",
                "body_md": "body",
                "topic": "existing-topic",
                "idempotency_key": "topic-filter-empty",
            },
        )

        result = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": pkey("test/topic-empty"),
                "agent_name": names[1],
                "topic": "nonexistent",
            },
        )
        items = _get_list(result)
        assert len(items) == 0


@pytest.mark.asyncio
async def test_invalid_topic_rejected(isolated_env):
    """Topic with invalid characters should be rejected."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/topic-invalid"), 2)

        # FastMCP may raise or return error-flagged result depending on version
        try:
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": pkey("test/topic-invalid"),
                    "sender_name": names[0],
                    "to": [names[1]],
                    "subject": "Bad topic",
                    "body_md": "body",
                    "topic": "spaces not allowed",
                    "idempotency_key": "topic-invalid-spaces",
                },
            )
        except Exception as exc:
            rejected = "INVALID_TOPIC" in str(exc) or "topic" in str(exc).lower()
            detail = f"Expected topic validation error, got: {exc}"
        else:
            # If no exception, check is_error flag or error in content
            rejected = getattr(result, "is_error", False) or "INVALID_TOPIC" in str(result)
            detail = f"Expected INVALID_TOPIC error, got: {result}"
        assert rejected, detail


@pytest.mark.asyncio
async def test_topic_allows_dotted_bead_ids(isolated_env):
    """Dotted beads_rust hierarchical IDs (e.g. ``br-abc.1``) are valid topics (issue #165)."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/topic-dotted"), 2)

        result = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/topic-dotted"),
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "Child bead coordination",
                "body_md": "Working on the child bead.",
                "topic": "br-abc.1",
                "idempotency_key": "topic-dotted-bead",
            },
        )
        data = _get_data(result)
        payload = data["deliveries"][0]["message"]
        assert payload.get("topic") == "br-abc.1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_topic",
    [".", "..", "../foo", ".hidden", "a/b", "spaces not allowed", "-leading-hyphen", "_leading_underscore"],
)
async def test_topic_rejects_traversal_and_unsafe_shapes(isolated_env, bad_topic):
    """Path-traversal / dotfile / leading-non-alphanumeric topics are rejected (issue #165 guard)."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/topic-traversal"), 2)

        try:
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": pkey("test/topic-traversal"),
                    "sender_name": names[0],
                    "to": [names[1]],
                    "subject": "Bad topic",
                    "body_md": "body",
                    "topic": bad_topic,
                    "idempotency_key": "topic-traversal-invalid",
                },
            )
        except Exception as exc:
            rejected = "INVALID_TOPIC" in str(exc) or "topic" in str(exc).lower()
            detail = f"Expected topic validation error for {bad_topic!r}, got: {exc}"
        else:
            rejected = getattr(result, "is_error", False) or "INVALID_TOPIC" in str(result)
            detail = f"Expected INVALID_TOPIC error for {bad_topic!r}, got: {result}"
        assert rejected, detail


# ============================================================================
# fetch_topic tool
# ============================================================================


@pytest.mark.asyncio
async def test_fetch_topic_tool(isolated_env):
    """fetch_topic returns all messages with matching topic across all senders."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/fetch-topic"), 3)

        # Two different senders send messages with the same topic
        await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/fetch-topic"),
                "sender_name": names[0],
                "to": [names[2]],
                "subject": "From agent 0",
                "body_md": "First message",
                "topic": "standup",
                "idempotency_key": "fetch-topic-first-sender",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/fetch-topic"),
                "sender_name": names[1],
                "to": [names[2]],
                "subject": "From agent 1",
                "body_md": "Second message",
                "topic": "standup",
                "idempotency_key": "fetch-topic-second-sender",
            },
        )
        # Different topic (should not appear)
        await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/fetch-topic"),
                "sender_name": names[0],
                "to": [names[1]],
                "subject": "Different topic",
                "body_md": "Not standup",
                "topic": "other",
                "idempotency_key": "fetch-topic-other",
            },
        )

        result = await client.call_tool(
            "fetch_topic",
            {
                "project_key": pkey("test/fetch-topic"),
                "topic_name": "standup",
            },
        )
        items = _get_list(result)
        assert len(items) == 2
        subjects = {m["subject"] for m in items}
        assert "From agent 0" in subjects
        assert "From agent 1" in subjects


@pytest.mark.asyncio
async def test_broadcast_with_topic(isolated_env):
    """Broadcast + topic: all agents receive topic-tagged message."""
    server = build_mcp_server()
    async with Client(server) as client:
        names = await _setup_project_with_agents(client, pkey("test/bcast-topic"), 3)
        sender = names[0]

        result = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/bcast-topic"),
                "sender_name": sender,
                "to": [],
                "subject": "Daily standup",
                "body_md": "What are you working on?",
                "broadcast": True,
                "topic": "standup",
                "idempotency_key": "broadcast-with-topic",
            },
        )
        data = _get_data(result)
        payload = data["deliveries"][0]["message"]
        assert payload.get("topic") == "standup"
        assert len(payload["to"]) == 2  # 3 agents minus sender

        # Verify message visible via fetch_topic
        topic_result = await client.call_tool(
            "fetch_topic",
            {
                "project_key": pkey("test/bcast-topic"),
                "topic_name": "standup",
            },
        )
        items = _get_list(topic_result)
        assert len(items) == 1
        assert items[0]["subject"] == "Daily standup"
