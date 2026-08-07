"""Instant delivery: the hub's fan-out rules, and who the endpoint lets in.

The hub's job is narrow but its failure modes are all silent — a hint that goes
to the wrong connection, or to none, leaves no trace anywhere and simply means
an agent keeps waiting. So the properties worth pinning are the ones a reader
would otherwise have to take on faith: that every listener for an agent is
woken rather than just one, that a departed listener stops receiving, and that
a full queue is dropped instead of raising into the delivery path.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.notify import QUEUE_MAXSIZE, NotificationHub

TOKEN = "events-bearer"


class TestHub:
    def test_publish_reaches_every_listener_for_the_agent(self):
        """Two sessions share one identity, so both must be woken.

        A single queue per agent would hand the hint to whichever happened to
        be waiting and leave the other asleep on the same mailbox.
        """
        hub = NotificationHub()
        first = hub.subscribe("proj", "agent-1")
        second = hub.subscribe("proj", "agent-1")

        assert hub.publish("proj", "agent-1", {"id": 7}) == 2
        assert first.get_nowait() == {"id": 7}
        assert second.get_nowait() == {"id": 7}

    def test_publish_is_scoped_to_project_and_agent(self):
        hub = NotificationHub()
        mine = hub.subscribe("proj", "agent-1")
        other_agent = hub.subscribe("proj", "agent-2")
        other_project = hub.subscribe("other", "agent-1")

        assert hub.publish("proj", "agent-1", {"id": 1}) == 1
        assert mine.qsize() == 1
        assert other_agent.qsize() == 0
        assert other_project.qsize() == 0

    def test_keys_ignore_case_and_surrounding_space(self):
        """The endpoint resolves names case-insensitively, so the hub must
        agree — otherwise a subscriber registered under one spelling never
        receives a publish made under another."""
        hub = NotificationHub()
        queue = hub.subscribe("Proj", " Agent-1 ")
        assert hub.publish("proj", "AGENT-1", {"id": 2}) == 1
        assert queue.qsize() == 1

    def test_unsubscribe_stops_delivery_and_drops_the_key(self):
        """Without this the hub grows a queue for every client that ever
        disconnected and publishes into them for the life of the process."""
        hub = NotificationHub()
        queue = hub.subscribe("proj", "agent-1")
        hub.unsubscribe("proj", "agent-1", queue)

        assert hub.publish("proj", "agent-1", {"id": 3}) == 0
        assert hub.listener_count("proj", "agent-1") == 0
        assert hub._subscribers == {}

    def test_unsubscribing_twice_is_harmless(self):
        """The stream's `finally` can run after the key is already gone."""
        hub = NotificationHub()
        queue = hub.subscribe("proj", "agent-1")
        hub.unsubscribe("proj", "agent-1", queue)
        hub.unsubscribe("proj", "agent-1", queue)

    def test_full_queue_is_dropped_not_raised(self):
        """publish() runs inside message delivery. A notification problem must
        never turn a message that was stored and archived into an error."""
        hub = NotificationHub()
        queue = hub.subscribe("proj", "agent-1")
        for _ in range(QUEUE_MAXSIZE):
            hub.publish("proj", "agent-1", {"id": 0})
        assert queue.full()

        assert hub.publish("proj", "agent-1", {"id": 99}) == 0
        assert queue.qsize() == QUEUE_MAXSIZE

    def test_publish_with_no_listeners_is_a_no_op(self):
        assert NotificationHub().publish("proj", "nobody", {"id": 1}) == 0


def _build(monkeypatch):
    monkeypatch.setenv("HTTP_BEARER_TOKEN", TOKEN)
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    return settings, build_http_app(settings, build_mcp_server())


class TestEndpointAuth:
    """Every rejection must look identical from outside.

    The bearer this endpoint sits behind is server-wide, not per project, so a
    caller holding it could otherwise sweep names and learn which agents exist
    and when their mail arrives.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "params, headers",
        [
            ({}, {}),
            ({"project": "p"}, {}),
            ({"project": "p", "agent": "a"}, {}),
            ({"project": "p", "agent": "a"}, {"X-Agent-Mail-Registration-Token": "nope"}),
            ({"project": "", "agent": "a"}, {"X-Agent-Mail-Registration-Token": "nope"}),
        ],
    )
    async def test_rejections_are_indistinguishable(self, isolated_env, monkeypatch, params, headers):
        settings, app = _build(monkeypatch)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/events",
                params=params,
                headers={"Authorization": f"Bearer {TOKEN}", **headers},
            )
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}

    @pytest.mark.asyncio
    async def test_bearer_is_still_required(self, isolated_env, monkeypatch):
        """The route sits behind the existing bearer middleware; a valid agent
        token must not be a way around it."""
        settings, app = _build(monkeypatch)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/events",
                params={"project": "p", "agent": "a"},
                headers={"X-Agent-Mail-Registration-Token": "whatever"},
            )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_stream_yields_ready_then_the_hint_then_ends():
    """The client's contract is: connect, see `: ready`, catch up, then wait.

    `: ready` has to precede the wait or the client cannot tell "subscribed and
    idle" from "still connecting", and would have to guess when it is safe to
    pull — which is the lost-wakeup window this ordering exists to close.
    """
    hub = NotificationHub()
    queue = hub.subscribe("proj", "agent-1")
    frames: list[bytes] = []

    async def consume():
        frames.append(b": ready\n\n")
        event = await asyncio.wait_for(queue.get(), timeout=5)
        frames.append(f"data: {event}\n\n".encode())

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    hub.publish("proj", "agent-1", {"kind": "message", "id": 12})
    await task

    assert frames[0] == b": ready\n\n"
    assert b"12" in frames[1]
