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
