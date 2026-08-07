"""In-process fan-out for instant delivery hints.

Polling is what this replaces. The inbox hook checks on session start and then
at most once every two minutes, and only while the agent is running tools — so
an *idle* session is never reached, which is precisely the case that matters:
an agent waiting on another agent's answer, or a human saying "stop".

The hints carried here are deliberately thin — `{kind, project, agent, id}`.
The woken client pulls the real content through `fetch_inbox`, which already
enforces the mailbox's authorisation rules. Shipping message bodies down this
channel would duplicate that authorisation model for no gain, and would make a
dropped frame a lost message rather than a missed nudge.

Nothing here is durable, and that is the design: the mailbox is the log. A
subscriber that was disconnected, or whose queue overflowed, catches up on its
next `fetch_inbox` and loses nothing. That is why clients must subscribe
*before* they catch up — otherwise a message arriving between the pull and the
subscribe is missed by both.

**Single process only.** Subscribers live in this worker's memory, so a publish
in one worker cannot reach a subscriber in another. The server runs a single
uvicorn worker, which is what makes this correct; running more would silently
deliver hints to some sessions and not others. Anything multi-worker needs a
shared broker here, not a bigger queue.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

# Per connection, not per agent. Two sessions on one host share an identity, and
# a single queue per agent would hand the hint to whichever happened to be
# waiting — waking one and leaving the other asleep on the same mailbox.
QUEUE_MAXSIZE = 32


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Comment frames keep the connection alive through anything that times out an
# idle socket. The CDN in front of this deployment drops at roughly 100s, and
# Apache is configured well above that, so 15s has ample margin at negligible
# cost — it is also what the MCP event stream already uses successfully here.
KEEPALIVE_SECONDS = _float_env("AGENT_MAIL_EVENTS_KEEPALIVE_SECONDS", 15.0)

# An upper bound on a single subscription. Reaching it closes the stream, which
# makes the client's `curl` exit and so wakes the agent for nothing — harmless,
# since it will find an empty inbox and subscribe again, and the same spurious
# wake already happens on any network blip. The bound exists so a connection
# whose peer vanished without a FIN cannot hold a queue forever.
MAX_STREAM_SECONDS = _float_env("AGENT_MAIL_EVENTS_MAX_SECONDS", 3600.0)


def _key(project: str, agent: str) -> tuple[str, str]:
    return (project.strip().lower(), agent.strip().lower())


class NotificationHub:
    """Routes hints to the connections currently listening for an agent."""

    def __init__(self) -> None:
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[dict[str, Any]]]] = {}

    def subscribe(self, project: str, agent: str) -> asyncio.Queue[dict[str, Any]]:
        """Register a connection and hand back the queue it should read."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers.setdefault(_key(project, agent), set()).add(queue)
        return queue

    def unsubscribe(self, project: str, agent: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Drop a connection. Must run in a `finally`, or the hub leaks queues
        for every client that ever disconnected and publishes into them forever."""
        key = _key(project, agent)
        listeners = self._subscribers.get(key)
        if listeners is None:
            return
        listeners.discard(queue)
        if not listeners:
            self._subscribers.pop(key, None)

    def publish(self, project: str, agent: str, event: dict[str, Any]) -> int:
        """Offer a hint to every connection listening for this agent.

        Returns how many took it. Never raises and never blocks: this is called
        from the message-delivery path, where a notification failure must not
        turn a delivered message into an error. A full queue is dropped rather
        than waited on — the client already has a wake pending, and the mailbox
        it will read is authoritative either way.
        """
        listeners = self._subscribers.get(_key(project, agent))
        if not listeners:
            return 0
        delivered = 0
        for queue in tuple(listeners):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue
            except Exception:
                continue
            delivered += 1
        return delivered

    def listener_count(self, project: str, agent: str) -> int:
        return len(self._subscribers.get(_key(project, agent), ()))


hub = NotificationHub()
