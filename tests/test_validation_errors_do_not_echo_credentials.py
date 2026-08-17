"""A validation failure must not quote a credential back to the caller.

Pydantic reports the offending value, and for an unexpected keyword that
value IS the argument -- so a typo in a token's own field name printed the
token in full. Measured on 2026-08-14: it burned a live registration token,
and it caught three agents in one day because these argument names are long
and similar (`registration_token`, `requester_registration_token`).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server

SECRET = "SUPERSECRET-TOKEN-VALUE-0123456789abcdef"


async def _error_text(arguments: dict[str, Any]) -> str:
    server = build_mcp_server()
    async with Client(server) as client:
        with pytest.raises(Exception) as caught:
            await client.call_tool("whois", arguments)
    return str(caught.value)


@pytest.mark.asyncio
async def test_a_typo_in_the_token_field_does_not_echo_the_token(
    isolated_env: Any,
) -> None:
    """The exact shape that leaked: the token becomes the offending value."""
    text = await _error_text(
        {
            "project_key": "/owner/repo",
            "agent_name": "someone",
            "registration_tokens": SECRET,  # note the typo
        }
    )
    assert SECRET not in text
    # The diagnosis must survive: without the field name the error is useless.
    assert "registration_tokens" in text
    assert "redacted" in text


@pytest.mark.asyncio
async def test_a_token_in_another_fields_payload_is_not_echoed(
    isolated_env: Any,
) -> None:
    """A missing-argument error reports the whole argument dict as its input."""
    text = await _error_text({"registration_token": SECRET})
    assert SECRET not in text
    assert "project_key" in text


@pytest.mark.asyncio
async def test_ordinary_values_are_still_shown(isolated_env: Any) -> None:
    """Negative control: redaction must not degrade into hiding everything.

    Without this, a fix that replaced every input with `<redacted>` would pass
    the two tests above while making every validation error unusable.
    """
    text = await _error_text(
        {
            "project_key": "/owner/repo",
            "agent_name": "someone",
            "nonsense_field": "plain-visible-value",
        }
    )
    assert "nonsense_field" in text
    assert "plain-visible-value" in text
