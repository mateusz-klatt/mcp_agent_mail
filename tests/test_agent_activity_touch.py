"""Authenticating counts as being alive.

`last_active_ts` was refreshed when an agent registered or sent a message, and
not when it authenticated to reserve, renew or read. An agent that spends an
afternoon filing reservations and never speaks therefore drifts toward looking
abandoned to the reservation sweeper, which weighs that field.

The sweeper is forgiving — it also considers recent mail, filesystem and git
activity, and only acts when none of them is present — so this was never a daily
failure. It was a field that did not mean what its name says, which is the kind
of thing that is only ever discovered while chasing something else.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from fastmcp import Client

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import get_session
from sqlalchemy import text

KEY = "/test/activity"


def _data(result):
    return getattr(result, "data", None) or getattr(result, "structured_content", {})


async def _last_active(name: str) -> str:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT last_active_ts FROM agents WHERE name = :n"), {"n": name}
            )
        ).fetchone()
    return str(row[0])


async def _backdate(name: str, iso: str) -> None:
    """Age an agent so the throttle cannot suppress the next refresh."""
    async with get_session() as session:
        await session.execute(
            text("UPDATE agents SET last_active_ts = :t WHERE name = :n"),
            {"t": iso, "n": name},
        )
        await session.commit()


@pytest.fixture
def server(isolated_env, monkeypatch):
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    return build_mcp_server()


@pytest.mark.asyncio
async def test_authenticating_refreshes_activity(server):
    """A reservation call is the case that matters: it is what a working agent
    does for long stretches without ever sending a message."""
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": KEY})
        me = _data(
            await client.call_tool(
                "register_agent",
                {"project_key": KEY, "name": "worker-1", "program": "probe", "model": "probe"},
            )
        )
        token = me["registration_token"]

        await _backdate("worker-1", "2020-01-01 00:00:00")
        stale = await _last_active("worker-1")
        assert stale.startswith("2020")

        # A second session, so this goes through token verification rather than
        # the session binding the registration above created.
        async with Client(server) as other:
            await other.call_tool(
                "file_reservation_paths",
                {
                    "project_key": KEY,
                    "agent_name": "worker-1",
                    "registration_token": token,
                    "paths": ["src/thing.py"],
                    "ttl_seconds": 60,
                },
            )

        assert not (await _last_active("worker-1")).startswith("2020")


@pytest.mark.asyncio
async def test_a_rejected_call_does_not_count_as_activity(server):
    """Otherwise anyone holding the server bearer could keep another agent's
    reservations alive indefinitely by failing to authenticate as it."""
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": KEY})
        await client.call_tool(
            "register_agent",
            {"project_key": KEY, "name": "worker-1", "program": "probe", "model": "probe"},
        )

        await _backdate("worker-1", "2020-01-01 00:00:00")
        async with Client(server) as attacker:
            with contextlib.suppress(Exception):
                await attacker.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": KEY,
                        "agent_name": "worker-1",
                        "registration_token": "wrong",
                        "paths": ["src/thing.py"],
                        "ttl_seconds": 60,
                    },
                )
        assert (await _last_active("worker-1")).startswith("2020")


@pytest.mark.asyncio
async def test_the_throttle_actually_suppresses_writes(server):
    """The throttle exists because the hooks fire twice per edit. A version that
    wrote on every call would pass the test above and quietly triple the write
    load, so assert the suppression rather than trusting the constant."""
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": KEY})
        me = _data(
            await client.call_tool(
                "register_agent",
                {"project_key": KEY, "name": "worker-1", "program": "probe", "model": "probe"},
            )
        )
        token = me["registration_token"]

        async with Client(server) as other:
            async def reserve(path: str) -> None:
                await other.call_tool(
                    "file_reservation_paths",
                    {
                        "project_key": KEY,
                        "agent_name": "worker-1",
                        "registration_token": token,
                        "paths": [path],
                        "ttl_seconds": 60,
                    },
                )

            await _backdate("worker-1", "2020-01-01 00:00:00")
            await reserve("a.py")
            first = await _last_active("worker-1")
            assert not first.startswith("2020"), "the first call must refresh"

            # Immediately again: inside the throttle window, so the stored value
            # must be untouched — not merely close, identical.
            await reserve("b.py")
            assert await _last_active("worker-1") == first


@pytest.mark.asyncio
async def test_an_aware_timestamp_does_not_break_authentication(server):
    """The shape that actually broke, and the one the tests above could not see.

    `_backdate` above writes a naive string, so every assertion in this file
    exercised naive-minus-naive and passed. The column is *declared* naive, but a
    row written from an offset-bearing ISO string — `datetime.now(timezone.utc)
    .isoformat()`, which is what tests/test_server.py does and what any restore
    or import would produce — is handed back **aware**, and the throttle then
    subtracts the two flavours: TypeError.

    It surfaced two files away, as `send_message` failing with "Argument type
    mismatch", because this runs inside `_authenticate_agent` — so a bookkeeping
    error was reported to the caller as a rejected tool call. The axis that
    matters here is the tzinfo flavour of the stored value, not its age.
    """
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": KEY})
        me = _data(
            await client.call_tool(
                "register_agent",
                {"project_key": KEY, "name": "worker-1", "program": "probe", "model": "probe"},
            )
        )
        token = me["registration_token"]

        # Offset-bearing, and old enough that the throttle cannot short-circuit
        # past the subtraction and hide the defect.
        from datetime import datetime, timedelta, timezone

        await _backdate(
            "worker-1", (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        )

        async with Client(server) as other:
            await other.call_tool(
                "file_reservation_paths",
                {
                    "project_key": KEY,
                    "agent_name": "worker-1",
                    "registration_token": token,
                    "paths": ["src/thing.py"],
                    "ttl_seconds": 60,
                },
            )

        # Refreshed, not merely "did not raise": a version that swallowed the
        # TypeError in the surrounding suppress() would also not raise, and would
        # leave the field stale forever.
        refreshed = await _last_active("worker-1")
        assert (datetime.now(timezone.utc) - datetime.fromisoformat(refreshed).replace(
            tzinfo=timezone.utc
        )).total_seconds() < 120, f"last_active_ts not refreshed: {refreshed}"
