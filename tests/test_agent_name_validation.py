"""P0 regression tests for legacy labels and durable Agent identities.

These tests verify that agent name validation correctly:
1. Keeps legacy adjective+noun helper behavior isolated from mailbox creation
2. Requires explicit client-os-host-slot names for newly created Agents
3. Rejects invalid creation without mutating the Agent table
4. Preserves authentication continuity for existing legacy Agent rows

Reference: mcp_agent_mail-2xf
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlmodel import select

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session
from mcp_agent_mail.models import Agent
from mcp_agent_mail.utils import (
    ADJECTIVES,
    NOUNS,
    generate_agent_name,
    parse_client_platform_host_agent_id,
    sanitize_agent_name,
    validate_agent_name_format,
    validate_client_platform_host_agent_id,
)
from tests.keys import pkey

# ============================================================================
# Unit Tests: validate_agent_name_format()
# ============================================================================


class TestValidateAgentNameFormat:
    """Test the validate_agent_name_format() function from utils.py."""

    def test_valid_names_return_true(self):
        """Valid adjective+noun combinations should return True."""
        valid_names = [
            "GreenLake",
            "BlueDog",
            "RedStone",
            "PurpleBear",
            "WhiteMountain",
            "FrostyDog",
            "SilentCave",
            "BrightForest",
        ]
        for name in valid_names:
            assert validate_agent_name_format(name), f"'{name}' should be valid"

    def test_all_adjective_noun_combinations_are_valid(self):
        """Sample of all possible adjective+noun combinations should be valid."""
        # Test a representative sample (testing all 4278+ would be slow)
        sample_adjectives = list(ADJECTIVES)[:10]
        sample_nouns = list(NOUNS)[:10]
        for adj in sample_adjectives:
            for noun in sample_nouns:
                name = f"{adj}{noun}"
                assert validate_agent_name_format(name), f"'{name}' should be valid"

    def test_case_insensitive_validation(self):
        """Validation should be case-insensitive."""
        # All these variations should be valid
        assert validate_agent_name_format("GreenLake")
        assert validate_agent_name_format("greenlake")
        assert validate_agent_name_format("GREENLAKE")
        assert validate_agent_name_format("gReEnLaKe")
        assert validate_agent_name_format("greenLAKE")

    def test_invalid_names_return_false(self):
        """Invalid names should return False."""
        invalid_names = [
            "",  # Empty
            "   ",  # Whitespace only
            "Green",  # Adjective only
            "Lake",  # Noun only
            "BackendAgent",  # Descriptive
            "CodeMigrator",  # Descriptive
            "claude-code",  # Program name
            "gpt-4",  # Model name
            "user@example.com",  # Email
            "all",  # Broadcast keyword
            "LakeGreen",  # Reversed order
            "GreenGreen",  # Same word twice (adjective)
            "LakeLake",  # Same word twice (noun)
            "GreenLakeBlue",  # Three words
            "123Lake",  # Number prefix
            "Green123",  # Number suffix
            "Green_Lake",  # Underscore
            "Green-Lake",  # Hyphen
            "Green Lake",  # Space
        ]
        for name in invalid_names:
            assert not validate_agent_name_format(name), f"'{name}' should be invalid"

    def test_empty_string_returns_false(self):
        """Empty string should return False."""
        assert not validate_agent_name_format("")

    def test_none_like_empty_returns_false(self):
        """None-like inputs should return False."""
        # Note: The function expects a string, but we test edge cases
        assert not validate_agent_name_format("")
        assert not validate_agent_name_format("   ")

    def test_partial_matches_return_false(self):
        """Partial matches (adjective or noun only) should return False."""
        for adj in list(ADJECTIVES)[:5]:
            assert not validate_agent_name_format(adj), f"Adjective-only '{adj}' should be invalid"
        for noun in list(NOUNS)[:5]:
            assert not validate_agent_name_format(noun), f"Noun-only '{noun}' should be invalid"

    def test_reversed_order_returns_false(self):
        """Noun+adjective (wrong order) should return False."""
        reversed_names = ["LakeGreen", "DogBlue", "StoneRed", "BearPurple"]
        for name in reversed_names:
            assert not validate_agent_name_format(name), f"Reversed '{name}' should be invalid"


class TestValidateClientPlatformHostAgentId:
    """Test the stable client/platform/host/slot identity contract."""

    @pytest.mark.parametrize(
        "name",
        [
            "claude-wsl-home-1",
            "codex-wsl-home-1",
            "cursor-wsl-home-1",
            "copilot-win-home-1",
            "gemini-linux-build-box-7-12",
            "claude-mac-MacBook-Pro.mac-2",
        ],
    )
    def test_accepts_supported_client_identities(self, name: str) -> None:
        assert validate_client_platform_host_agent_id(name)

    @pytest.mark.parametrize(
        "name",
        [
            "claude-wsl-home-0",
            "codex-wsl-home-session",
            "claude-solaris-home-1",
            "unknown-wsl-home-1",
            "CLAUDE-wsl-home-1",
            "claude-WSL-home-1",
            "claude-wsl-1",
            "cx-wsl-home-1",
            "claude-home-wsl-1",
            "home-wsl-claude-1",
            "home-wsl-codex-1",
            "home-wsl-copilot-1",
            "claude-code",
        ],
    )
    def test_rejects_noncanonical_identities(self, name: str) -> None:
        assert not validate_client_platform_host_agent_id(name)

    def test_parser_keeps_the_entire_hyphenated_host(self) -> None:
        assert parse_client_platform_host_agent_id(
            "claude-mac-macbook-pro-mateusza-12"
        ) == ("claude", "mac", "macbook-pro-mateusza", "12")


# ============================================================================
# Unit Tests: generate_agent_name()
# ============================================================================


class TestGenerateAgentName:
    """Test the generate_agent_name() function from utils.py."""

    def test_returns_string(self):
        """generate_agent_name() should return a string."""
        name = generate_agent_name()
        assert isinstance(name, str)
        assert len(name) > 0

    def test_generated_names_are_valid(self):
        """All generated names should pass validation."""
        for _ in range(50):  # Generate 50 random names
            name = generate_agent_name()
            assert validate_agent_name_format(name), f"Generated name '{name}' should be valid"

    def test_generated_names_are_pascalcase(self):
        """Generated names should be in PascalCase format."""
        for _ in range(20):
            name = generate_agent_name()
            # Should start with uppercase
            assert name[0].isupper(), f"'{name}' should start with uppercase"
            # Should contain at least one more uppercase (start of noun)
            upper_count = sum(1 for c in name if c.isupper())
            assert upper_count >= 2, f"'{name}' should have at least 2 uppercase letters"

    def test_generated_names_use_word_lists(self):
        """Generated names should use words from ADJECTIVES and NOUNS lists."""
        adjectives_lower = {a.lower() for a in ADJECTIVES}
        nouns_lower = {n.lower() for n in NOUNS}

        for _ in range(30):
            name = generate_agent_name()
            name_lower = name.lower()
            # Check that name starts with an adjective and ends with a noun
            found_match = False
            for adj in adjectives_lower:
                if name_lower.startswith(adj):
                    remaining = name_lower[len(adj) :]
                    if remaining in nouns_lower:
                        found_match = True
                        break
            assert found_match, f"'{name}' should be composed of adjective+noun from word lists"


# ============================================================================
# Unit Tests: sanitize_agent_name()
# ============================================================================


class TestSanitizeAgentName:
    """Test the sanitize_agent_name() function from utils.py."""

    def test_strips_whitespace(self):
        """Whitespace should be stripped."""
        assert sanitize_agent_name("  GreenLake  ") == "GreenLake"
        assert sanitize_agent_name("\tBlueDog\n") == "BlueDog"

    def test_removes_special_characters(self):
        """Non-alphanumeric characters should be removed."""
        assert sanitize_agent_name("Green-Lake") == "GreenLake"
        assert sanitize_agent_name("Blue_Dog") == "BlueDog"
        assert sanitize_agent_name("Red.Stone") == "RedStone"
        assert sanitize_agent_name("Purple@Bear") == "PurpleBear"

    def test_preserves_alphanumeric(self):
        """Alphanumeric characters should be preserved."""
        assert sanitize_agent_name("GreenLake123") == "GreenLake123"
        assert sanitize_agent_name("Blue2Dog") == "Blue2Dog"

    def test_empty_after_cleanup_returns_none(self):
        """If nothing remains after cleanup, return None."""
        assert sanitize_agent_name("") is None
        assert sanitize_agent_name("   ") is None
        assert sanitize_agent_name("---") is None
        assert sanitize_agent_name("@#$%") is None

    def test_truncates_long_names(self):
        """Names longer than 128 characters should be truncated."""
        long_name = "A" * 200
        result = sanitize_agent_name(long_name)
        assert result is not None
        assert len(result) <= 128

    def test_preserves_case(self):
        """Case should be preserved."""
        assert sanitize_agent_name("greenLake") == "greenLake"
        assert sanitize_agent_name("GREENLAKE") == "GREENLAKE"
        assert sanitize_agent_name("GreenLake") == "GreenLake"


# ============================================================================
# Integration Tests: Agent Registration with Valid Names
# ============================================================================


@pytest.mark.asyncio
async def test_register_agent_requires_explicit_name_without_mutation(isolated_env):
    """Omitting a durable identity must fail without creating an Agent row."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = await client.call_tool(
            "ensure_project", {"human_key": pkey("test/names")}
        )

        with pytest.raises(ToolError, match=r"name\n\s+Missing required argument"):
            await client.call_tool(
                "register_agent",
                {
                    "project_key": pkey("test/names"),
                    "program": "test-program",
                    "model": "test-model",
                },
            )

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Agent).where(Agent.project_id == project.data["id"])
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_register_agent_with_explicit_durable_name(isolated_env):
    """register_agent accepts an explicit client-os-host-slot identity."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/names")})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/names"),
                "program": "test-program",
                "model": "test-model",
                "name": "codex-wsl-names-1",
            },
        )

        assert result.data["name"] == "codex-wsl-names-1"
        assert result.data["registration_token"]


@pytest.mark.asyncio
async def test_register_agent_never_re_echoes_existing_token(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/bootstrap-token")})
        created = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/bootstrap-token"),
                "program": "test-program",
                "model": "test-model",
                "name": "codex-wsl-bootstrap-token-1",
            },
        )
        resumed = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/bootstrap-token"),
                "program": "test-program",
                "model": "test-model",
                "name": "codex-wsl-bootstrap-token-1",
                "registration_token": created.data["registration_token"],
            },
        )
        assert "registration_token" not in resumed.data
        assert resumed.data["registration_token_issued"] is False


@pytest.mark.asyncio
async def test_register_agent_case_insensitive_uniqueness(isolated_env):
    """Agent names should be case-insensitively unique."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/case")})

        # Register with one case
        result1 = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/case"),
                "program": "test",
                "model": "test",
                "name": "codex-wsl-case-1",
            },
        )
        assert result1.data["name"] == "codex-wsl-case-1"

        # Re-register with different case should update, not create new
        result2 = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/case"),
                "program": "test-updated",
                "model": "test-updated",
                "name": "CODEX-WSL-CASE-1",
            },
        )
        # Should return the same agent (same ID), with updated program
        assert result2.data["id"] == result1.data["id"]


# ============================================================================
# Integration Tests: Invalid durable Agent creation
# ============================================================================


@pytest.mark.asyncio
async def test_register_agent_rejects_descriptive_name_without_mutation(isolated_env):
    """A descriptive name is not coerced into a random durable identity."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = await client.call_tool(
            "ensure_project", {"human_key": pkey("test/invalid-descriptive")}
        )

        with pytest.raises(ToolError, match="must match client-os-host-slot"):
            await client.call_tool(
                "register_agent",
                {
                    "project_key": pkey("test/invalid-descriptive"),
                    "program": "test",
                    "model": "test",
                    "name": "BackendHarmonizer",
                },
            )

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Agent).where(Agent.project_id == project.data["id"])
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_register_agent_rejects_program_name_without_mutation(isolated_env):
    """A program name cannot silently create a random Agent mailbox."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = await client.call_tool(
            "ensure_project", {"human_key": pkey("test/invalid-program")}
        )

        with pytest.raises(ToolError, match="must match client-os-host-slot"):
            await client.call_tool(
                "register_agent",
                {
                    "project_key": pkey("test/invalid-program"),
                    "program": "claude-code",
                    "model": "opus",
                    "name": "claude-code",
                },
            )

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Agent).where(Agent.project_id == project.data["id"])
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canonical_name",
    [
        "claude-wsl-home-1",
        "codex-wsl-home-1",
        "copilot-win-home-1",
        "gemini-linux-build-box-7-2",
        "factory-linux-build-box-1",
        "cursor-mac-laptop-1",
        "cline-win-home-2",
        "windsurf-wsl-home-1",
        "opencode-other-lab-3",
    ],
)
async def test_register_agent_preserves_canonical_identity(
    isolated_env,
    canonical_name: str,
):
    """Program/model-name substrings do not replace canonical addresses."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/canonical")})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/canonical"),
                "program": "claude-code",
                "model": "claude-sonnet",
                "name": canonical_name,
            },
        )

        assert result.data["name"] == canonical_name


# ============================================================================
# Integration Tests: Existing legacy Agent authentication
# ============================================================================


@pytest.mark.asyncio
async def test_register_agent_authenticates_existing_legacy_name(isolated_env):
    """Strict creation rules do not orphan an already-provisioned legacy mailbox."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = await client.call_tool(
            "ensure_project", {"human_key": pkey("test/legacy-existing")}
        )

    async with get_session() as session:
        legacy = Agent(
            project_id=project.data["id"],
            name="BlueLake",
            program="legacy-client",
            model="legacy-model",
            registration_token="legacy-registration-token",
        )
        session.add(legacy)
        await session.commit()
        await session.refresh(legacy)
        legacy_id = legacy.id

    async with Client(server) as client:
        result = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/legacy-existing"),
                "program": "codex",
                "model": "gpt-5",
                "name": "BlueLake",
                "registration_token": "legacy-registration-token",
            },
        )

    assert result.data["id"] == legacy_id
    assert result.data["name"] == "BlueLake"
    assert result.data["program"] == "codex"


@pytest.mark.asyncio
async def test_register_agent_rejects_malformed_canonical_name(isolated_env):
    """A nearly-canonical new name remains invalid and creates no row."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = await client.call_tool(
            "ensure_project", {"human_key": pkey("test/malformed-canonical")}
        )

        with pytest.raises(ToolError, match="must match client-os-host-slot"):
            await client.call_tool(
                "register_agent",
                {
                    "project_key": pkey("test/malformed-canonical"),
                    "program": "claude-code",
                    "model": "opus",
                    "name": "claude-wsl-home-session",
                },
            )

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Agent).where(Agent.project_id == project.data["id"])
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canonical_name",
    [
        "claude-wsl-home-1",
        "codex-wsl-home-1",
        "copilot-win-home-1",
        "gemini-linux-build-box-7-2",
    ],
)
async def test_register_agent_accepts_each_supported_durable_client(
    isolated_env,
    canonical_name: str,
):
    """Each supported client can own a separate durable mailbox."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/canonical-strict")})

        result = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/canonical-strict"),
                "program": "claude-code",
                "model": "claude-sonnet",
                "name": canonical_name,
            },
        )

        assert result.data["name"] == canonical_name


# ============================================================================
# Integration Tests: create_agent_identity
# ============================================================================


@pytest.mark.asyncio
async def test_create_agent_identity_requires_name_hint_without_mutation(isolated_env):
    """Provisioning cannot invent a durable identity when name_hint is absent."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = await client.call_tool(
            "ensure_project", {"human_key": pkey("test/identity")}
        )

        with pytest.raises(ToolError, match=r"name_hint\n\s+Missing required argument"):
            await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": pkey("test/identity"),
                    "program": "test",
                    "model": "test",
                },
            )

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Agent).where(Agent.project_id == project.data["id"])
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_create_agent_identity_with_durable_hint(isolated_env):
    """create_agent_identity accepts an available client-os-host-slot hint."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/hint")})

        result = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": pkey("test/hint"),
                "program": "test",
                "model": "test",
                "name_hint": "codex-wsl-identity-1",
            },
        )

        assert result.data["name"] == "codex-wsl-identity-1"


@pytest.mark.asyncio
async def test_create_agent_identity_rejects_invalid_hint_without_mutation(isolated_env):
    """Invalid hints are rejected instead of being silently randomized."""
    server = build_mcp_server()
    async with Client(server) as client:
        project = await client.call_tool(
            "ensure_project", {"human_key": pkey("test/invalid-hint")}
        )

        with pytest.raises(ToolError, match="must match client-os-host-slot"):
            await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": pkey("test/invalid-hint"),
                    "program": "test",
                    "model": "test",
                    "name_hint": "InvalidDescriptiveName",
                },
            )

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Agent).where(Agent.project_id == project.data["id"])
            )
        ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_create_agent_identity_returns_one_time_token(isolated_env):
    """A fresh durable identity returns the credential needed for persistence."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/token-default")})
        result = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": pkey("test/token-default"),
                "program": "test",
                "model": "test",
                "name_hint": "codex-wsl-token-default-1",
            },
        )
        assert isinstance(result.data["registration_token"], str)
        assert result.data["registration_token"]


@pytest.mark.asyncio
async def test_create_agent_identity_rejects_duplicate_durable_hint(isolated_env):
    """Provisioning is create-only and never mutates an existing durable Agent."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/duplicate-hint")})
        first = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": pkey("test/duplicate-hint"),
                "program": "test",
                "model": "test",
                "name_hint": "codex-wsl-duplicate-1",
            },
        )

        with pytest.raises(ToolError, match="already in use"):
            await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": pkey("test/duplicate-hint"),
                    "program": "changed",
                    "model": "changed",
                    "name_hint": "codex-wsl-duplicate-1",
                },
            )

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Agent).where(Agent.project_id == first.data["project_id"])
            )
        ).scalars().all()
    assert [(agent.id, agent.program, agent.model) for agent in rows] == [
        (first.data["id"], "test", "test")
    ]


# ============================================================================
# Integration Tests: Message Sending with Agent Names
# ============================================================================


@pytest.mark.asyncio
async def test_send_message_validates_recipient_names(isolated_env):
    """send_message should validate recipient agent names exist."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/msg")})

        # Register sender
        sender_result = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/msg"),
                "program": "test",
                "model": "test",
                "name": "codex-wsl-msg-1",
            },
        )
        sender_name = sender_result.data["name"]

        # Try to send to non-existent recipient - use a valid-format name that doesn't exist
        with pytest.raises(Exception) as exc_info:
            await client.call_tool(
                "send_message",
                {
                    "project_key": pkey("test/msg"),
                    "sender_name": sender_name,
                    "to": ["claude-wsl-missing-1"],
                    "subject": "Test",
                    "body_md": "Test message",
                    "idempotency_key": "agent-name-missing-recipient",
                },
            )

        error_msg = str(exc_info.value).lower()
        # Error should indicate the agent was not found
        assert "not found" in error_msg or "not registered" in error_msg or "available" in error_msg


@pytest.mark.asyncio
async def test_send_message_with_valid_agents(isolated_env):
    """send_message should work with valid, registered agent names."""
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("test/valid")})

        # Register sender and recipient
        sender_result = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/valid"),
                "program": "test",
                "model": "test",
                "name": "codex-wsl-valid-1",
            },
        )
        sender_name = sender_result.data["name"]

        recipient_result = await client.call_tool(
            "register_agent",
            {
                "project_key": pkey("test/valid"),
                "program": "test",
                "model": "test",
                "name": "claude-wsl-valid-1",
            },
        )
        recipient_name = recipient_result.data["name"]

        # Send message should succeed
        result = await client.call_tool(
            "send_message",
            {
                "project_key": pkey("test/valid"),
                "sender_name": sender_name,
                "to": [recipient_name],
                "subject": "Test Message",
                "body_md": "Testing valid agent names",
                "idempotency_key": "agent-name-valid-delivery",
            },
        )

        assert result.data["count"] == 1
        assert len(result.data["deliveries"]) == 1


@pytest.mark.asyncio
async def test_send_message_preserves_canonical_recipient_for_to_cc_and_bcc(isolated_env):
    """Canonical client identities route through every recipient field."""
    project_key = pkey("test/canonical-recipient")
    sender_name = "codex-linux-home-1"
    recipient_name = "claude-linux-holzera-1"
    missing_name = "claude-linux-missing-99"
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "codex",
                "model": "gpt-5",
                "name": sender_name,
            },
        )
        recipient = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "claude",
                "model": "claude-sonnet",
                "name": recipient_name,
            },
        )
        recipient_id = recipient.data["id"]
        project_id = recipient.data["project_id"]

        for field in ("to", "cc", "bcc"):
            recipients = {"to": [], field: [recipient_name]}
            result = await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": sender_name,
                    **recipients,
                    "subject": f"Canonical recipient via {field}",
                    "body_md": "Regression coverage for canonical routing.",
                    "idempotency_key": f"canonical-recipient-{field}",
                },
            )
            assert result.data["count"] == 1

        with pytest.raises(ToolError, match="not registered"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": sender_name,
                    "to": [missing_name],
                    "subject": "Unknown canonical recipient",
                    "body_md": "This delivery must still fail at recipient lookup.",
                    "idempotency_key": "canonical-recipient-missing",
                },
            )

        with pytest.raises(ToolError, match="not found"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": project_key,
                    "sender_name": missing_name,
                    "to": [recipient_name],
                    "subject": "Unknown canonical sender",
                    "body_md": "This delivery must still fail at sender lookup.",
                    "idempotency_key": "canonical-sender-missing",
                },
            )

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Agent).where(
                    Agent.project_id == project_id,
                    Agent.name == recipient_name,
                )
            )
        ).scalars().all()

    assert [agent.id for agent in rows] == [recipient_id]


# ============================================================================
# Edge Case Tests
# ============================================================================


class TestAgentNameEdgeCases:
    """Test edge cases in agent name handling."""

    def test_adjectives_are_non_empty(self):
        """ADJECTIVES list should be non-empty."""
        assert len(list(ADJECTIVES)) > 0

    def test_nouns_are_non_empty(self):
        """NOUNS list should be non-empty."""
        assert len(list(NOUNS)) > 0

    def test_all_adjectives_are_capitalized(self):
        """All adjectives should be capitalized."""
        for adj in ADJECTIVES:
            assert adj[0].isupper(), f"Adjective '{adj}' should start with uppercase"

    def test_all_nouns_are_capitalized(self):
        """All nouns should be capitalized."""
        for noun in NOUNS:
            assert noun[0].isupper(), f"Noun '{noun}' should start with uppercase"

    def test_no_duplicate_adjectives(self):
        """ADJECTIVES should have no duplicates (case-insensitive)."""
        adj_lower = [a.lower() for a in ADJECTIVES]
        assert len(adj_lower) == len(set(adj_lower)), "Duplicate adjectives found"

    def test_no_duplicate_nouns(self):
        """NOUNS should have no duplicates (case-insensitive)."""
        nouns_lower = [n.lower() for n in NOUNS]
        assert len(nouns_lower) == len(set(nouns_lower)), "Duplicate nouns found"

    def test_namespace_size(self):
        """Verify the namespace is large enough for practical use."""
        num_adjectives = len(list(ADJECTIVES))
        num_nouns = len(list(NOUNS))
        namespace_size = num_adjectives * num_nouns
        # Should have at least 4000 combinations (62 x 69 = 4278 per the comment)
        assert namespace_size >= 4000, f"Namespace too small: {namespace_size}"
