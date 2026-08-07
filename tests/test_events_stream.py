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
import json

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.notify import QUEUE_MAXSIZE, NotificationHub, hub

TOKEN = "events-bearer"
PROJECT = "/test/events"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _rpc_tool(name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


async def _subscribed(slug: str, agent: str, count: int = 1) -> None:
    """Block until the hub has registered the expected live subscriptions."""
    for _ in range(500):
        if hub.listener_count(slug, agent) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"no subscription for {agent} after 5s")


def _text(response) -> str:
    """Unwrap the tool's text payload from the JSON-RPC envelope."""
    return response.json()["result"]["content"][0]["text"]


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
    # RBAC defaults to on with the role "reader", so every write here would be
    # 403 — which is what the deployment sets HTTP_RBAC_DEFAULT_ROLE=writer for.
    # These tests exercise delivery, not authorisation; the 401 cases below run
    # before RBAC and are unaffected.
    monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
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


class TestTransport:
    """End to end, through the actual route.

    The test this replaces built its own frames and asserted on them; it would
    have passed with `events_stream` deleted from http.py, which is why nobody
    noticed the endpoint had no coverage at all. Everything below opens a real
    connection and reads what the server sends.
    """

    async def _register(self, client, path: str, name: str) -> dict:
        project = await client.post(
            path, headers=HEADERS, json=_rpc_tool("ensure_project", {"human_key": PROJECT})
        )
        self.slug = json.loads(_text(project))["slug"]
        response = await client.post(
            path,
            headers=HEADERS,
            json=_rpc_tool(
                "register_agent",
                {"project_key": PROJECT, "name": name, "program": "probe", "model": "probe"},
            ),
        )
        return json.loads(_text(response))

    @pytest.mark.asyncio
    async def test_ready_then_a_real_message_then_the_same_message_in_the_inbox(
        self, isolated_env, monkeypatch
    ):
        """The whole contract in one pass: subscribe, catch up, be woken.

        The final assertion is the one that matters — the id in the frame must
        be findable through fetch_inbox. A hint pointing at something the
        mailbox will not return is worse than no hint, because the woken agent
        finds nothing and concludes the channel is noisy.
        """
        settings, app = _build(monkeypatch)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            me = await self._register(client, settings.http.path, "box-1")
            token = me["registration_token"]

            frames: list[str] = []

            async def listen() -> None:
                async with client.stream(
                    "GET",
                    "/events",
                    params={"project": PROJECT, "agent": "box-1"},
                    headers={**HEADERS, "X-Agent-Mail-Registration-Token": token},
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/event-stream")
                    async for line in response.aiter_lines():
                        if line.strip():
                            frames.append(line)
                        if line.startswith("data: "):
                            return

            listener = asyncio.create_task(listen())
            # Wait for the subscription to be live before sending, or the send
            # can land first and there is nobody to publish to — the very race
            # the `: ready` frame exists to let clients close.
            # ASGITransport does not deliver a body incrementally, so no frame
            # is readable until the stream ends — waiting on `: ready` here
            # would hang, and merely waiting on "any output" would let the send
            # race the subscription and pass for the wrong reason. The hub's own
            # registry is the signal that the subscription is live; frame
            # ordering is asserted after the stream completes.
            await _subscribed(self.slug, "box-1")

            sent = await client.post(
                settings.http.path,
                headers=HEADERS,
                json=_rpc_tool(
                    "send_message",
                    {
                        "project_key": PROJECT,
                        "sender_name": "box-1",
                        "sender_token": token,
                        "to": ["box-1"],
                        "subject": "wake me",
                        "body_md": "body",
                    },
                ),
            )
            # send_message answers {count, deliveries, verified_sender, …}; the
            # stored message sits inside the first delivery's payload. Reading
            # a top-level "id" here silently produced a KeyError rather than a
            # wrong answer, which is the good kind of failure.
            sent_id = json.loads(_text(sent))["deliveries"][0]["payload"]["id"]
            await asyncio.wait_for(listener, timeout=10)

            assert frames[0].startswith(": ready"), f"ready must come first: {frames}"
            data = [f for f in frames if f.startswith("data: ")]
            assert data, f"no data frame; got {frames}"
            hint = json.loads(data[0][len("data: "):])
            assert hint["kind"] == "message"
            assert hint["agent"] == "box-1"
            # The join between the two halves of instant delivery: the hint must
            # point at the message that was actually stored, not merely at
            # something. A hint carrying an id the mailbox will not return is
            # worse than no hint — the woken agent finds nothing and learns to
            # distrust the channel.
            assert hint["id"] == sent_id

    @pytest.mark.asyncio
    async def test_a_blind_copy_wakes_its_recipient(self, isolated_env, monkeypatch):
        """BCC is blind between recipients, not between the server and the
        recipient. Withholding the hint would leave a blind-copied agent as the
        only party never told their own mail arrived — and the existing
        filesystem notification does exactly that, which is why this channel
        has its own recipient list."""
        settings, app = _build(monkeypatch)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sender = await self._register(client, settings.http.path, "sender-1")
            hidden = await self._register(client, settings.http.path, "hidden-1")
            await self._register(client, settings.http.path, "visible-1")

            frames: list[str] = []

            async def listen() -> None:
                async with client.stream(
                    "GET",
                    "/events",
                    params={"project": PROJECT, "agent": "hidden-1"},
                    headers={
                        **HEADERS,
                        "X-Agent-Mail-Registration-Token": hidden["registration_token"],
                    },
                ) as response:
                    async for line in response.aiter_lines():
                        if line.strip():
                            frames.append(line)
                        if line.startswith("data: "):
                            return

            listener = asyncio.create_task(listen())
            await _subscribed(self.slug, "hidden-1")

            await client.post(
                settings.http.path,
                headers=HEADERS,
                json=_rpc_tool(
                    "send_message",
                    {
                        "project_key": PROJECT,
                        "sender_name": "sender-1",
                        "sender_token": sender["registration_token"],
                        "to": ["visible-1"],
                        "bcc": ["hidden-1"],
                        "subject": "quietly",
                        "body_md": "body",
                    },
                ),
            )
            await asyncio.wait_for(listener, timeout=10)
            assert any(f.startswith("data: ") for f in frames), f"bcc recipient not woken: {frames}"

    @pytest.mark.asyncio
    async def test_two_connections_for_one_agent_are_both_woken(
        self, isolated_env, monkeypatch
    ):
        """Two sessions on one host share an identity. A single queue between
        them would hand the hint to whichever happened to be waiting and leave
        the other asleep on the same mailbox. The hub-level test covers the
        registry; this covers the route."""
        settings, app = _build(monkeypatch)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            me = await self._register(client, settings.http.path, "box-1")
            token = me["registration_token"]
            seen: list[list[str]] = [[], []]

            async def listen(slot: int) -> None:
                async with client.stream(
                    "GET",
                    "/events",
                    params={"project": PROJECT, "agent": "box-1"},
                    headers={**HEADERS, "X-Agent-Mail-Registration-Token": token},
                ) as response:
                    async for line in response.aiter_lines():
                        if line.strip():
                            seen[slot].append(line)
                        if line.startswith("data: "):
                            return

            listeners = [asyncio.create_task(listen(0)), asyncio.create_task(listen(1))]
            await _subscribed(self.slug, "box-1", count=2)

            await client.post(
                settings.http.path,
                headers=HEADERS,
                json=_rpc_tool(
                    "send_message",
                    {
                        "project_key": PROJECT,
                        "sender_name": "box-1",
                        "sender_token": token,
                        "to": ["box-1"],
                        "subject": "both",
                        "body_md": "body",
                    },
                ),
            )
            await asyncio.wait_for(asyncio.gather(*listeners), timeout=10)
            for slot, got in enumerate(seen):
                assert any(f.startswith("data: ") for f in got), f"listener {slot} not woken: {got}"
