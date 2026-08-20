"""A validation failure must not quote a credential back to the caller.

Pydantic reports the offending value, and for an unexpected keyword that
value IS the argument -- so a typo in a token's own field name printed the
token in full. Measured on 2026-08-14: it burned a live registration token,
and it caught three agents in one day because these argument names are long
and similar (`registration_token`, `requester_registration_token`).
"""

from __future__ import annotations

import logging
from io import StringIO
from typing import Any

import pytest
from fastmcp import Client, Context
from pydantic import BaseModel, field_validator

from mcp_agent_mail.app import (
    RECENT_TOOL_USAGE,
    _instrument_tool,
    build_mcp_server,
)
from mcp_agent_mail.config import clear_settings_cache

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
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The exact shape that leaked: the token becomes the offending value."""
    logger_names = (
        "fastmcp.server.mixins.mcp_operations",
        "fastmcp.server.server",
    )
    loggers = [logging.getLogger(name) for name in logger_names]
    previous_levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
        logger.addHandler(caplog.handler)
    try:
        text = await _error_text(
            {
                "project_key": "/owner/repo",
                "agent_name": "someone",
                "registration_tokens": SECRET,  # note the typo
            }
        )
    finally:
        for logger, previous_level in zip(
            loggers,
            previous_levels,
            strict=True,
        ):
            logger.removeHandler(caplog.handler)
            logger.setLevel(previous_level)

    assert SECRET not in text
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    raw_records = repr([dict(record.__dict__) for record in caplog.records])
    assert SECRET not in rendered_logs
    assert SECRET not in raw_records
    assert "arguments redacted" in rendered_logs
    assert "details redacted" in rendered_logs
    # The diagnosis must survive: without the field name the error is useless.
    assert "registration_tokens" in text
    assert "redacted" in text


def test_fastmcp_sensitive_log_filter_is_installed_once(
    isolated_env: Any,
) -> None:
    build_mcp_server()
    build_mcp_server()
    for logger_name in (
        "fastmcp.server.auth.oauth_proxy.proxy",
        "fastmcp.server.mixins.mcp_operations",
        "fastmcp.server.server",
    ):
        installed = [
            log_filter
            for log_filter in logging.getLogger(logger_name).filters
            if getattr(
                log_filter,
                "_mcp_agent_mail_sensitive_log_filter",
                False,
            )
        ]
        assert len(installed) == 1


@pytest.mark.asyncio
async def test_a_token_in_another_fields_payload_is_not_echoed(
    isolated_env: Any,
) -> None:
    """A missing-argument error reports the whole argument dict as its input."""
    text = await _error_text({"registration_token": SECRET})
    assert SECRET not in text
    assert "project_key" in text


@pytest.mark.parametrize(
    "nested_payload",
    [
        {"registration_token": SECRET},
        [{"execution_token": SECRET}],
    ],
)
@pytest.mark.asyncio
async def test_nested_credentials_are_not_echoed(
    isolated_env: Any,
    nested_payload: Any,
) -> None:
    text = await _error_text(
        {
            "project_key": "/owner/repo",
            "agent_name": "someone",
            "other_fields": nested_payload,
        }
    )
    assert SECRET not in text
    assert "***" in text


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


@pytest.mark.asyncio
async def test_ordinary_nested_values_are_still_shown(
    isolated_env: Any,
) -> None:
    text = await _error_text(
        {
            "project_key": "/owner/repo",
            "agent_name": "someone",
            "other_fields": {"ordinary": ["plain-nested-value"]},
        }
    )
    assert "plain-nested-value" in text


@pytest.mark.asyncio
async def test_instrumented_body_validation_never_logs_or_returns_a_credential(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class CredentialProbe(BaseModel):
        value: int
        values: dict[str, int]

        @field_validator("value", mode="before")
        @classmethod
        def reject_with_input(cls, value: Any) -> Any:
            raise ValueError(f"credential rejected: {value}")

    monkeypatch.setenv("TOOLS_LOG_ENABLED", "true")
    monkeypatch.setenv("LOG_RICH_ENABLED", "true")
    clear_settings_cache()
    server = build_mcp_server()

    @server.tool(name="body_validation_credential_probe")
    @_instrument_tool(
        "body_validation_credential_probe",
        cluster="infrastructure",
        project_arg="project_key",
    )
    async def body_validation_credential_probe(
        ctx: Context,
        registration_token: str,
        payload: Any,
        project_key: str,
    ) -> dict[str, bool]:
        invalid_integer: Any = "not-an-integer"
        CredentialProbe.model_validate(
            {
                "value": registration_token,
                "values": {registration_token: invalid_integer},
            }
        )
        return {"accepted": True}

    caplog.set_level(logging.DEBUG)
    broken_stream = StringIO()
    broken_stream.close()
    broken_handler = logging.StreamHandler(broken_stream)
    broken_handler.setLevel(logging.WARNING)
    app_logger = logging.getLogger("mcp_agent_mail.app")
    app_logger.addHandler(broken_handler)
    try:
        async with Client(server) as client:
            with pytest.raises(Exception) as caught:
                await client.call_tool(
                    "body_validation_credential_probe",
                    {
                        "registration_token": SECRET,
                        "payload": {SECRET: "ordinary"},
                        "project_key": SECRET,
                    },
                )
    finally:
        app_logger.removeHandler(broken_handler)

    captured = capsys.readouterr()
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    raw_records = repr([dict(record.__dict__) for record in caplog.records])
    assert SECRET not in str(caught.value)
    assert SECRET not in rendered_logs
    assert SECRET not in raw_records
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert "redacted" in str(caught.value).casefold()
    assert RECENT_TOOL_USAGE[-1][2] != SECRET
