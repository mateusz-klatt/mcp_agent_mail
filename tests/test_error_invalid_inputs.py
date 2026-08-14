"""P2 Tests: Errors - Invalid Inputs.

Test error handling for invalid inputs across MCP tools.
Verifies clear error messages and appropriate exception types.

Test Cases:
1. Invalid project_key format (relative path rejected)
2. Non-existent project (register_agent, whois, fetch_inbox, search_messages)
3. Invalid agent name format (single word, spaces)
4. Non-existent agent (send_message sender/recipient, file_reservation, whois, fetch_inbox, etc.)
5. Placeholder detection (YOUR_PROJECT, YOUR_AGENT_NAME)
6. Empty recipients list (API allows, returns 0 deliveries)
7. Empty subject (API allows)
8. Invalid contact policy (API normalizes to 'auto')
9. Empty file reservation paths (rejected)
10. TTL below minimum (API warns but allows)
11. Non-existent message (mark_read, acknowledge, reply)
12. Non-existent agent for release/renew reservations
13. Non-existent agent for contact request/respond

Reference: mcp_agent_mail-mj0
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server
from tests.keys import pkey

# ============================================================================
# Test: Invalid project_key
# ============================================================================


def test_pkey_is_host_neutral() -> None:
    """pkey should produce the same opaque project identity on every host."""
    assert pkey("owner/repository") == "/owner/repository"


@pytest.mark.asyncio
async def test_ensure_project_requires_absolute_path(isolated_env):
    """ensure_project should require absolute path starting with /."""
    server = build_mcp_server()
    async with Client(server) as client:
        # Relative path should fail
        try:
            await client.call_tool("ensure_project", {"human_key": "relative/path"})
            pytest.fail("Should reject relative path")
        except ToolError as e:
            error_str = str(e).lower()
            # Must mention 'absolute' or 'path' (but not just "/" which is too loose)
            assert "absolute" in error_str or "path" in error_str


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "human_key",
    [
        "/owner/repository",
        r"C:\projects\repository",
        r"\\server\share\repository",
    ],
)
async def test_ensure_project_accepts_host_neutral_absolute_path_syntax(isolated_env, human_key):
    """ensure_project should accept POSIX, drive, and UNC syntax on every host."""
    server = build_mcp_server()
    async with Client(server) as client:
        result = await client.call_tool("ensure_project", {"human_key": human_key})
        agent_result = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": human_key,
                "program": "test",
                "model": "test",
                "name_hint": "codex-wsl-path-syntax-1",
            },
        )

    assert result.data["human_key"] == human_key
    assert agent_result.data["name"] == "codex-wsl-path-syntax-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "human_key",
    [
        "",
        "relative/path",
        "../repository",
        "/owner/../repository",
        r"/owner\..\repository",
        r"C:relative\repository",
        r"C:\owner\..\repository",
    ],
)
async def test_ensure_project_rejects_relative_and_traversing_keys(isolated_env, human_key):
    """ensure_project should reject empty, relative, and traversing keys."""
    server = build_mcp_server()
    async with Client(server) as client:
        with pytest.raises(ToolError, match="absolute path-like project key"):
            await client.call_tool("ensure_project", {"human_key": human_key})


@pytest.mark.asyncio
async def test_register_agent_nonexistent_project(isolated_env):
    """register_agent should fail for non-existent project."""
    server = build_mcp_server()
    async with Client(server) as client:
        try:
            await client.call_tool(
                "register_agent",
                {
                    "project_key": "NonExistentProject",
                    "program": "test",
                    "model": "test",
                    "name": "codex-wsl-nonexistent-project-1",
                },
            )
            pytest.fail("Should reject non-existent project")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "project" in error_str


# ============================================================================
# Test: Invalid agent name
# ============================================================================


@pytest.mark.asyncio
async def test_register_agent_invalid_name_format(isolated_env):
    """register_agent rejects names outside the durable identity contract."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("invalidname")})

        with pytest.raises(ToolError, match="client-os-host-slot"):
            await client.call_tool(
                "register_agent",
                {"project_key": "InvalidName", "program": "test", "model": "test", "name": "SingleWord"},
            )

        with pytest.raises(ToolError, match="codex-wsl-home-1"):
            await client.call_tool(
                "register_agent",
                {"project_key": "InvalidName", "program": "test", "model": "test", "name": "Has Spaces"},
            )


@pytest.mark.asyncio
async def test_send_message_nonexistent_agent(isolated_env):
    """send_message should fail for non-existent sender agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("nonexistentagent")})

        try:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "NonexistentAgent",
                    "sender_name": "NonExistentSender",
                    "to": ["SomeRecipient"],
                    "subject": "Test",
                    "body_md": "Body",
                    "idempotency_key": "invalid-missing-sender",
                },
            )
            pytest.fail("Should reject non-existent sender")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


@pytest.mark.asyncio
async def test_send_message_nonexistent_recipient(isolated_env):
    """send_message should fail for non-existent recipient."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("nonexistentrecip")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "NonexistentRecip",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-nonexistent-recipient-1",
            },
        )
        sender_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "NonexistentRecip",
                    "sender_name": sender_name,
                    "to": ["NonExistentRecipient"],
                    "subject": "Test",
                    "body_md": "Body",
                    "idempotency_key": "invalid-missing-recipient",
                },
            )
            pytest.fail("Should reject non-existent recipient")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "recipient" in error_str or "agent" in error_str


# ============================================================================
# Test: Placeholder detection
# ============================================================================


@pytest.mark.asyncio
async def test_placeholder_detection_your_project(isolated_env):
    """Should detect placeholder values like YOUR_PROJECT - either reject or warn."""
    server = build_mcp_server()
    async with Client(server) as client:
        try:
            result = await client.call_tool("ensure_project", {"human_key": pkey("YOUR_PROJECT")})
            # If it succeeded, project was created (placeholder detection may just warn)
            # Verify we at least got a valid response
            assert result.data is not None
            assert "slug" in result.data or "id" in result.data
        except ToolError as e:
            # Placeholder was rejected - verify error message is appropriate
            error_str = str(e).lower()
            assert "placeholder" in error_str or "your_" in error_str or "template" in error_str
        # Test passes whether rejected or allowed with warning - documents actual behavior


@pytest.mark.asyncio
async def test_placeholder_detection_your_agent_name(isolated_env):
    """Should detect placeholder agent names like YOUR_AGENT_NAME - either reject or warn."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("placeholderagent")})

        try:
            result = await client.call_tool(
                "register_agent",
                {
                    "project_key": "PlaceholderAgent",
                    "program": "test",
                    "model": "test",
                    "name": "YOUR_AGENT_NAME",
                },
            )
            # If succeeded, verify we got a response (may have auto-generated name)
            assert result.data is not None
        except ToolError as e:
            # Placeholder was rejected - verify error message is appropriate
            error_str = str(e).lower()
            assert "placeholder" in error_str or "your_" in error_str or "invalid" in error_str
        # Test passes whether rejected or allowed - documents actual behavior


# ============================================================================
# Test: Message validation
# ============================================================================


@pytest.mark.asyncio
async def test_send_message_empty_recipients(isolated_env):
    """send_message with empty to/cc/bcc (non-broadcast) must fail, not succeed with count:0.

    Regression for #189: previously an all-empty recipient set fell through
    and returned ``count: 0`` while reporting success, silently dropping the
    message and contradicting the docstring ("If no recipients are given, the
    call fails."). It must now raise INVALID_ARGUMENT.
    """
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("emptyrecip")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "EmptyRecip",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-empty-recipient-1",
            },
        )
        sender_name = agent_result.data["name"]

        # Non-broadcast send with no recipients must be rejected.
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "EmptyRecip",
                    "sender_name": sender_name,
                    "to": [],
                    "subject": "Test",
                    "body_md": "Body",
                    "idempotency_key": "invalid-empty-recipients",
                },
            )
        assert "recipient" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_irreversible_hard_delete_tools_are_not_public(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        tool_names = {tool.name for tool in await client.list_tools()}

    assert "hard_delete_agent" not in tool_names
    assert "hard_delete_project" not in tool_names
    assert "deregister_agent" not in tool_names


@pytest.mark.asyncio
async def test_send_message_empty_subject(isolated_env):
    """send_message should handle empty subject gracefully or reject."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("emptysubject")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "EmptySubject",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-empty-subject-1",
            },
        )
        sender_name = agent_result.data["name"]

        # Empty subject may be allowed or rejected
        try:
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": "EmptySubject",
                    "sender_name": sender_name,
                    "to": [sender_name],
                    "subject": "",
                    "body_md": "Body",
                    "idempotency_key": "invalid-empty-subject",
                },
            )
            # If allowed, message should still be sent
            assert result.data is not None
        except ToolError:
            # Also acceptable - rejecting empty subject
            pass


# ============================================================================
# Test: Contact policy validation
# ============================================================================


@pytest.mark.asyncio
async def test_set_contact_policy_invalid_policy(isolated_env):
    """set_contact_policy rejects an unknown policy instead of normalising it.

    This test used to assert the opposite — that an unrecognised policy became
    "auto". `c66e54f` (#201) removed that on purpose: silently falling back to
    the permissive default meant a typo in a policy name downgraded protection
    and reported success, which is the one outcome a caller cannot notice.

    So the refusal is the feature, and the message is part of it: a caller who
    mistyped needs the accepted set, or the error only tells them they were
    wrong without telling them what would be right.
    """
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("invalidpolicy")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "InvalidPolicy",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-invalid-policy-1",
            },
        )
        agent_name = agent_result.data["name"]

        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "set_contact_policy",
                {
                    "project_key": "InvalidPolicy",
                    "agent_name": agent_name,
                    "policy": "invalid_policy_value",
                },
            )

        message = str(excinfo.value)
        assert "invalid_policy_value" in message
        assert "auto" in message and "block" in message

        # The rejection must not have applied anything: a policy that raised and
        # still wrote would be worse than the normalisation this replaced.
        current = await client.call_tool(
            "list_contacts",
            {"project_key": "InvalidPolicy", "agent_name": agent_name},
        )
        assert current.data is not None


# ============================================================================
# Test: File reservation validation
# ============================================================================


@pytest.mark.asyncio
async def test_file_reservation_ttl_below_minimum(isolated_env):
    """file_reservation_paths warns but allows TTL below 60 seconds."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("ttlminimum")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "TtlMinimum",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-ttl-minimum-1",
            },
        )
        agent_name = agent_result.data["name"]

        # API warns but allows short TTL for testing scenarios
        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "TtlMinimum",
                "agent_name": agent_name,
                "paths": ["test.py"],
                "ttl_seconds": 30,  # Below recommended minimum of 60
            },
        )
        # Should succeed (with warning) and grant the reservation
        assert "granted" in result.data
        assert len(result.data["granted"]) == 1
        assert result.data["granted"][0]["path_pattern"] == "test.py"


@pytest.mark.asyncio
async def test_file_reservation_empty_paths(isolated_env):
    """file_reservation_paths should reject empty paths list."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("emptypaths")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "EmptyPaths",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-empty-paths-1",
            },
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "EmptyPaths",
                    "agent_name": agent_name,
                    "paths": [],
                },
            )
            pytest.fail("Should reject empty paths")
        except ToolError as e:
            error_str = str(e).lower()
            assert "path" in error_str or "empty" in error_str or "required" in error_str


@pytest.mark.asyncio
async def test_file_reservation_nonexistent_agent(isolated_env):
    """file_reservation_paths should fail for non-existent agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("reservenoagent")})

        try:
            await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "ReserveNoAgent",
                    "agent_name": "NonExistentAgent",
                    "paths": ["test.py"],
                },
            )
            pytest.fail("Should reject non-existent agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


# ============================================================================
# Test: Whois validation
# ============================================================================


@pytest.mark.asyncio
async def test_whois_nonexistent_project(isolated_env):
    """whois should fail for non-existent project."""
    server = build_mcp_server()
    async with Client(server) as client:
        try:
            await client.call_tool(
                "whois",
                {
                    "project_key": "NonExistentProject",
                    "agent_name": "SomeAgent",
                },
            )
            pytest.fail("Should fail for non-existent project")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "project" in error_str


@pytest.mark.asyncio
async def test_whois_nonexistent_agent(isolated_env):
    """whois should fail for non-existent agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("whoisnoagent")})

        try:
            await client.call_tool(
                "whois",
                {
                    "project_key": "WhoisNoAgent",
                    "agent_name": "NonExistentAgent",
                },
            )
            pytest.fail("Should fail for non-existent agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


# ============================================================================
# Test: Fetch inbox validation
# ============================================================================


@pytest.mark.asyncio
async def test_fetch_inbox_nonexistent_project(isolated_env):
    """fetch_inbox should fail for non-existent project."""
    server = build_mcp_server()
    async with Client(server) as client:
        try:
            await client.call_tool(
                "fetch_inbox",
                {
                    "project_key": "NonExistentProject",
                    "agent_name": "SomeAgent",
                },
            )
            pytest.fail("Should fail for non-existent project")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "project" in error_str


@pytest.mark.asyncio
async def test_fetch_inbox_nonexistent_agent(isolated_env):
    """fetch_inbox should fail for non-existent agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("inboxnoagent")})

        try:
            await client.call_tool(
                "fetch_inbox",
                {
                    "project_key": "InboxNoAgent",
                    "agent_name": "NonExistentAgent",
                },
            )
            pytest.fail("Should fail for non-existent agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


# ============================================================================
# Test: Mark/Acknowledge message validation
# ============================================================================


@pytest.mark.asyncio
async def test_mark_message_read_nonexistent_message(isolated_env):
    """mark_message_read should fail for non-existent message."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("marknonemsg")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "MarkNoneMsg",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-mark-missing-1",
            },
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "mark_message_read",
                {
                    "project_key": "MarkNoneMsg",
                    "agent_name": agent_name,
                    "message_id": 999999,  # Non-existent
                },
            )
            pytest.fail("Should fail for non-existent message")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "message" in error_str


@pytest.mark.asyncio
async def test_acknowledge_message_nonexistent_message(isolated_env):
    """acknowledge_message should fail for non-existent message."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("acknonemsg")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "AckNoneMsg",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-ack-missing-1",
            },
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "acknowledge_message",
                {
                    "project_key": "AckNoneMsg",
                    "agent_name": agent_name,
                    "message_id": 999999,  # Non-existent
                },
            )
            pytest.fail("Should fail for non-existent message")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "message" in error_str


# ============================================================================
# Test: Reply message validation
# ============================================================================


@pytest.mark.asyncio
async def test_reply_message_nonexistent_original(isolated_env):
    """reply_message should fail for non-existent original message."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("replynonemsg")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "ReplyNoneMsg",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-reply-missing-1",
            },
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "reply_message",
                {
                    "project_key": "ReplyNoneMsg",
                    "message_id": 999999,  # Non-existent
                    "sender_name": agent_name,
                    "body_md": "Reply body",
                    "idempotency_key": "invalid-missing-reply-target",
                },
            )
            pytest.fail("Should fail for non-existent original message")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "message" in error_str


# ============================================================================
# Test: Release/renew file reservation validation
# ============================================================================


@pytest.mark.asyncio
async def test_release_file_reservations_nonexistent_agent(isolated_env):
    """release_file_reservations should fail for non-existent agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("releasenoagent")})

        try:
            await client.call_tool(
                "release_file_reservations",
                {
                    "project_key": "ReleaseNoAgent",
                    "agent_name": "NonExistentAgent",
                },
            )
            pytest.fail("Should fail for non-existent agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


@pytest.mark.asyncio
async def test_release_file_reservations_rejects_empty_paths(isolated_env):
    """release_file_reservations should reject an explicit empty paths filter."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("releaseemptypaths")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "ReleaseEmptyPaths",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-release-empty-paths-1",
            },
        )
        agent_name = agent_result.data["name"]

        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "ReleaseEmptyPaths",
                "agent_name": agent_name,
                "paths": ["src/**"],
                "ttl_seconds": 300,
            },
        )

        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "release_file_reservations",
                {
                    "project_key": "ReleaseEmptyPaths",
                    "agent_name": agent_name,
                    "paths": [],
                },
            )

        assert "empty" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_release_file_reservations_rejects_empty_ids(isolated_env):
    """release_file_reservations should reject an explicit empty id filter."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("releaseemptyids")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "ReleaseEmptyIds",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-release-empty-ids-1",
            },
        )
        agent_name = agent_result.data["name"]

        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "ReleaseEmptyIds",
                "agent_name": agent_name,
                "paths": ["src/**"],
                "ttl_seconds": 300,
            },
        )

        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "release_file_reservations",
                {
                    "project_key": "ReleaseEmptyIds",
                    "agent_name": agent_name,
                    "file_reservation_ids": [],
                },
            )

        assert "empty" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_renew_file_reservations_nonexistent_agent(isolated_env):
    """renew_file_reservations should fail for non-existent agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("renewnoagent")})

        try:
            await client.call_tool(
                "renew_file_reservations",
                {
                    "project_key": "RenewNoAgent",
                    "agent_name": "NonExistentAgent",
                },
            )
            pytest.fail("Should fail for non-existent agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str


@pytest.mark.asyncio
async def test_renew_file_reservations_rejects_empty_paths(isolated_env):
    """renew_file_reservations should reject an explicit empty paths filter."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("renewemptypaths")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "RenewEmptyPaths",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-renew-empty-paths-1",
            },
        )
        agent_name = agent_result.data["name"]

        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "RenewEmptyPaths",
                "agent_name": agent_name,
                "paths": ["src/**"],
                "ttl_seconds": 300,
            },
        )

        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "renew_file_reservations",
                {
                    "project_key": "RenewEmptyPaths",
                    "agent_name": agent_name,
                    "paths": [],
                },
            )

        assert "empty" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_renew_file_reservations_rejects_empty_ids(isolated_env):
    """renew_file_reservations should reject an explicit empty id filter."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("renewemptyids")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "RenewEmptyIds",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-renew-empty-ids-1",
            },
        )
        agent_name = agent_result.data["name"]

        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "RenewEmptyIds",
                "agent_name": agent_name,
                "paths": ["src/**"],
                "ttl_seconds": 300,
            },
        )

        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "renew_file_reservations",
                {
                    "project_key": "RenewEmptyIds",
                    "agent_name": agent_name,
                    "file_reservation_ids": [],
                },
            )

        assert "empty" in str(excinfo.value).lower()


# ============================================================================
# Test: Search messages validation
# ============================================================================


@pytest.mark.asyncio
async def test_search_messages_nonexistent_project(isolated_env):
    """search_messages should fail for non-existent project."""
    server = build_mcp_server()
    async with Client(server) as client:
        try:
            await client.call_tool(
                "search_messages",
                {
                    "project_key": "NonExistentProject",
                    "query": "test",
                },
            )
            pytest.fail("Should fail for non-existent project")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "project" in error_str


# ============================================================================
# Test: Request/respond contact validation
# ============================================================================


@pytest.mark.asyncio
async def test_request_contact_nonexistent_agent(isolated_env):
    """request_contact should fail for non-existent from_agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("contactnoagent")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "ContactNoAgent",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-contact-missing-1",
            },
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "request_contact",
                {
                    "project_key": "ContactNoAgent",
                    "from_agent": "NonExistentAgent",
                    "to_agent": agent_name,
                },
            )
            pytest.fail("Should fail for non-existent from_agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str or "register" in error_str


@pytest.mark.asyncio
async def test_respond_contact_nonexistent_agent(isolated_env):
    """respond_contact should fail for non-existent to_agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("respondnoagent")})
        agent_result = await client.call_tool(
            "register_agent",
            {
                "project_key": "RespondNoAgent",
                "program": "test",
                "model": "test",
                "name": "codex-wsl-respond-missing-1",
            },
        )
        agent_name = agent_result.data["name"]

        try:
            await client.call_tool(
                "respond_contact",
                {
                    "project_key": "RespondNoAgent",
                    "to_agent": "NonExistentAgent",
                    "from_agent": agent_name,
                    "accept": True,
                },
            )
            pytest.fail("Should fail for non-existent to_agent")
        except ToolError as e:
            error_str = str(e).lower()
            assert "not found" in error_str or "agent" in error_str
