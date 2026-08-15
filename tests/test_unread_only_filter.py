"""Behaviour of the ``unread_only`` switch on ``fetch_inbox`` and ``fetch_topic``.

"Unread" is a property of a *recipient row*, not of a message: ``_list_inbox``
narrows on ``MessageRecipient.read_ts IS NULL`` for the calling agent's own row,
and ``fetch_topic`` joins a viewer-scoped ``MessageRecipient`` alias before
applying the same predicate. Everything below is derived from those two clauses
and from their callers -- notably ``resource://views/urgent-unread/{agent}``,
which is the reason the narrowing happens *before* ``LIMIT`` rather than after.

What is asserted here, grouped by the clause it protects:

* the predicate is opt-in and costs nothing when it is off (payload shape and
  contents are byte-identical whether it is omitted or passed as ``False``);
* both write paths that stamp ``read_ts`` -- ``mark_message_read`` and
  ``acknowledge_message`` -- retire a message from the unread set, and nothing
  else does, in particular not reading the mailbox;
* the predicate is scoped to one recipient row, so one reader cannot consume
  another reader's unread state;
* it intersects with the sibling filters (``topic``, ``urgent_only``) instead of
  widening them;
* it runs inside the query, so a bounded fetch still returns a full page;
* on ``fetch_topic`` it *narrows* the default project-wide result set down to
  rows the viewer actually received and has not read.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import uuid4

from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server

URGENT = "urgent"
ROUTINE = "normal"


def _unwrap(result: Any) -> Any:
    """Recover a tool's Python return value from a FastMCP call result.

    The structured payload is preferred over ``.data``: FastMCP re-validates a
    list return into a ``Root`` wrapper model there, and these tests want plain
    dicts. A tool returning a mapping puts it at the top level; a tool returning
    a sequence gets it nested under ``"result"``.
    """
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        nested = structured.get("result")
        return nested if isinstance(nested, (list, dict)) else structured
    data = getattr(result, "data", None)
    return result if data is None else data


def _ids(rows: list[dict[str, Any]]) -> list[int]:
    """Message ids in the order the server returned them."""
    return [int(row["id"]) for row in rows]


class Mailroom:
    """Thin driver over the public tool surface for one project.

    Tests address agents by a readable role ("author", "reader", ...); the
    class keeps the mapping from role to the server-assigned mailbox name so
    that no test has to care about the ``client-os-host-slot`` naming rule.
    """

    def __init__(self, client: Client, project_key: str) -> None:
        self._client = client
        self.project_key = project_key
        self._names: dict[str, str] = {}

    async def enrol(self, *roles: str) -> None:
        for slot, role in enumerate(roles, start=1):
            reply = _unwrap(
                await self._client.call_tool(
                    "register_agent",
                    {
                        "project_key": self.project_key,
                        "program": "pytest",
                        "model": "none",
                        "name": f"claude-wsl-iris-{slot}",
                        "task_description": role,
                    },
                )
            )
            self._names[role] = reply["name"]

    def name_of(self, role: str) -> str:
        return self._names[role]

    async def post(
        self,
        *,
        author: str,
        addressed_to: list[str],
        subject: str,
        topic: str | None = None,
        importance: str = ROUTINE,
    ) -> int:
        """Send one message; return the persisted message id."""
        request: dict[str, Any] = {
            "project_key": self.project_key,
            "sender_name": self.name_of(author),
            "to": [self.name_of(role) for role in addressed_to],
            "subject": subject,
            "body_md": "body",
            "importance": importance,
            "idempotency_key": uuid4().hex,
        }
        if topic is not None:
            request["topic"] = topic
        reply = _unwrap(await self._client.call_tool("send_message", request))
        return int(reply["deliveries"][0]["message"]["id"])

    async def inbox(self, role: str, **filters: Any) -> list[dict[str, Any]]:
        request = {
            "project_key": self.project_key,
            "agent_name": self.name_of(role),
            **filters,
        }
        return list(_unwrap(await self._client.call_tool("fetch_inbox", request)))

    async def by_topic(self, topic_name: str, **filters: Any) -> list[dict[str, Any]]:
        request = {
            "project_key": self.project_key,
            "topic_name": topic_name,
            **filters,
        }
        if "viewer" in request:
            request["agent_name"] = self.name_of(request.pop("viewer"))
        return list(_unwrap(await self._client.call_tool("fetch_topic", request)))

    async def stamp_read(self, role: str, message_id: int) -> None:
        await self._client.call_tool(
            "mark_message_read",
            {
                "project_key": self.project_key,
                "agent_name": self.name_of(role),
                "message_id": message_id,
            },
        )

    async def stamp_acknowledged(self, role: str, message_id: int) -> None:
        await self._client.call_tool(
            "acknowledge_message",
            {
                "project_key": self.project_key,
                "agent_name": self.name_of(role),
                "message_id": message_id,
            },
        )


@contextlib.asynccontextmanager
async def mailroom(project_key: str, *roles: str):
    """Boot a server, open a project, and enrol one agent per named role."""
    async with Client(build_mcp_server()) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        room = Mailroom(client, project_key)
        await room.enrol(*roles)
        yield room


# --- the switch is opt-in -------------------------------------------------


async def test_leaving_the_switch_off_is_indistinguishable_from_passing_false(
    isolated_env,
):
    """An existing caller sees the same rows, and the same fields, either way.

    The parameter defaults to ``False``; a client that never learns it exists
    and a polling client that spells it out must observe one behaviour. Read
    messages stay in both results -- turning the flag off is not a soft filter.
    """
    async with mailroom("/iris/unread-off", "author", "reader") as room:
        posted = [
            await room.post(author="author", addressed_to=["reader"], subject=f"note-{n}")
            for n in range(3)
        ]
        await room.stamp_read("reader", posted[0])

        implied = await room.inbox("reader")
        spelled_out = await room.inbox("reader", unread_only=False)

        assert set(_ids(implied)) == set(posted), "read mail must survive the default fetch"
        assert _ids(spelled_out) == _ids(implied)
        assert [sorted(row) for row in spelled_out] == [sorted(row) for row in implied], (
            "unread_only=False must not alter the payload's field set"
        )


# --- what does and does not retire a message from the unread set ----------


async def test_both_read_receipt_paths_retire_a_message_from_the_unread_set(
    isolated_env,
):
    """``mark_message_read`` and ``acknowledge_message`` both stamp ``read_ts``.

    ``acknowledge_message`` writes ``read_ts`` as well as ``ack_ts``, so an
    acknowledged message must not come back as unread even though the caller
    never invoked the read tool on it.
    """
    async with mailroom("/iris/unread-receipts", "author", "reader") as room:
        via_read = await room.post(author="author", addressed_to=["reader"], subject="read")
        via_ack = await room.post(author="author", addressed_to=["reader"], subject="ack")
        never_touched = await room.post(
            author="author", addressed_to=["reader"], subject="fresh"
        )

        await room.stamp_read("reader", via_read)
        await room.stamp_acknowledged("reader", via_ack)

        surviving = _ids(await room.inbox("reader", unread_only=True))

        assert surviving == [never_touched]
        assert via_read not in surviving, "mark_message_read must clear the unread flag"
        assert via_ack not in surviving, "acknowledge_message must clear it too"


async def test_reading_the_mailbox_does_not_itself_consume_unread_state(isolated_env):
    """A bare fetch is a pure read: it must not stamp ``read_ts``.

    This is the invariant that makes ``unread_only=True`` usable as a polling
    filter at all -- if fetching marked mail read, the second poll of an
    unchanged mailbox would come back empty and the message would be lost.
    """
    async with mailroom("/iris/unread-nonmutating", "author", "reader") as room:
        delivered = [
            await room.post(author="author", addressed_to=["reader"], subject=f"poll-{n}")
            for n in range(2)
        ]

        first_poll = _ids(await room.inbox("reader", unread_only=True))
        await room.inbox("reader")  # a default fetch in between must not count either
        second_poll = _ids(await room.inbox("reader", unread_only=True))

        assert set(first_poll) == set(delivered)
        assert second_poll == first_poll, "fetching must leave read state untouched"


async def test_one_recipient_reading_leaves_the_other_recipients_row_unread(
    isolated_env,
):
    """``read_ts`` lives on the per-recipient row, so it cannot leak sideways.

    The same message is delivered to two agents. One reads it. The predicate
    must consult each agent's own row, so the message disappears for the reader
    and stays for the other recipient.
    """
    async with mailroom("/iris/unread-scope", "author", "reader", "bystander") as room:
        shared = await room.post(
            author="author", addressed_to=["reader", "bystander"], subject="shared"
        )
        await room.stamp_read("reader", shared)

        assert _ids(await room.inbox("reader", unread_only=True)) == []
        assert _ids(await room.inbox("bystander", unread_only=True)) == [shared]


# --- composition with the sibling filters ---------------------------------


async def test_unread_and_topic_intersect_rather_than_widen(isolated_env):
    """Both predicates are ``WHERE`` clauses on one statement: it is an AND.

    Three candidates isolate the two ways an OR would show up -- an unread
    message under a different topic, and a read message under the requested
    topic. Neither may appear.
    """
    async with mailroom("/iris/unread-topic", "author", "reader") as room:
        wanted = await room.post(
            author="author", addressed_to=["reader"], subject="a", topic="deploy"
        )
        right_topic_but_read = await room.post(
            author="author", addressed_to=["reader"], subject="b", topic="deploy"
        )
        unread_but_wrong_topic = await room.post(
            author="author", addressed_to=["reader"], subject="c", topic="design"
        )
        await room.stamp_read("reader", right_topic_but_read)

        narrowed = _ids(await room.inbox("reader", topic="deploy", unread_only=True))

        assert narrowed == [wanted]
        assert right_topic_but_read not in narrowed, "unread_only must still apply"
        assert unread_but_wrong_topic not in narrowed, "topic must still apply"


async def test_unread_and_urgent_only_intersect_rather_than_widen(isolated_env):
    """The urgent-unread resource view depends on this pair composing.

    ``resource://views/urgent-unread/{agent}`` calls ``_list_inbox`` with both
    flags set and reports the result as a count of things needing attention, so
    a routine unread message or an urgent one already read must not inflate it.
    """
    async with mailroom("/iris/unread-urgent", "author", "reader") as room:
        urgent_unread = await room.post(
            author="author", addressed_to=["reader"], subject="a", importance=URGENT
        )
        urgent_read = await room.post(
            author="author", addressed_to=["reader"], subject="b", importance=URGENT
        )
        routine_unread = await room.post(
            author="author", addressed_to=["reader"], subject="c", importance=ROUTINE
        )
        await room.stamp_read("reader", urgent_read)

        narrowed = _ids(await room.inbox("reader", urgent_only=True, unread_only=True))

        assert narrowed == [urgent_unread]
        assert urgent_read not in narrowed, "unread_only must still apply"
        assert routine_unread not in narrowed, "urgent_only must still apply"


# --- the predicate runs inside the query, not over its output -------------


async def test_a_bounded_unread_fetch_still_returns_a_full_page(isolated_env):
    """Narrowing must happen before ``LIMIT``, not after it.

    Six messages: the three newest have been read. A caller asking for three
    unread ones must get three. If the limit were applied first and the unread
    predicate second, the newest three would fill the page and then be filtered
    away, handing back nothing while three unread messages still existed.
    """
    async with mailroom("/iris/unread-paging", "author", "reader") as room:
        older_unread = [
            await room.post(author="author", addressed_to=["reader"], subject=f"old-{n}")
            for n in range(3)
        ]
        newer_read = [
            await room.post(author="author", addressed_to=["reader"], subject=f"new-{n}")
            for n in range(3)
        ]
        for message_id in newer_read:
            await room.stamp_read("reader", message_id)

        page = _ids(await room.inbox("reader", limit=3, unread_only=True))

        assert len(page) == 3, "a full page of unread mail was available"
        assert set(page) == set(older_unread)


async def test_unread_results_keep_the_newest_first_ordering(isolated_env):
    """``unread_only`` adds a predicate; it must not disturb ``ORDER BY``.

    Pollers rely on the first element being the most recent message.
    """
    async with mailroom("/iris/unread-order", "author", "reader") as room:
        chronological = [
            await room.post(author="author", addressed_to=["reader"], subject=f"seq-{n}")
            for n in range(4)
        ]

        assert _ids(await room.inbox("reader", unread_only=True)) == list(
            reversed(chronological)
        )


# --- fetch_topic ----------------------------------------------------------


async def test_topic_fetch_is_project_wide_until_unread_only_is_set(isolated_env):
    """The default topic fetch has no viewer filter -- naming one changes nothing.

    ``fetch_topic`` authenticates when ``agent_name`` is supplied, but with
    ``unread_only`` off it still returns every message carrying the topic,
    including one the caller was never a recipient of. This is the baseline the
    next test narrows away from.
    """
    async with mailroom("/iris/topic-wide", "author", "viewer", "stranger") as room:
        to_viewer = await room.post(
            author="author", addressed_to=["viewer"], subject="a", topic="release"
        )
        to_stranger = await room.post(
            author="author", addressed_to=["stranger"], subject="b", topic="release"
        )

        anonymous = _ids(await room.by_topic("release"))
        as_viewer = _ids(await room.by_topic("release", viewer="viewer"))

        assert set(anonymous) == {to_viewer, to_stranger}
        assert as_viewer == anonymous, "agent_name alone must not narrow the result"


async def test_topic_fetch_with_unread_only_keeps_only_the_viewers_unread_rows(
    isolated_env,
):
    """Under the flag, the join to the viewer's recipient row does the filtering.

    Three same-topic messages separate the two exclusions: one the viewer
    received and read, and one the viewer never received at all. "Unread" is
    undefined without a recipient row, so the second is dropped rather than
    treated as unread -- the flag narrows the project-wide default, it does not
    reinterpret it.
    """
    async with mailroom("/iris/topic-unread", "author", "viewer", "stranger") as room:
        received_and_read = await room.post(
            author="author", addressed_to=["viewer"], subject="a", topic="release"
        )
        never_received = await room.post(
            author="author", addressed_to=["stranger"], subject="b", topic="release"
        )
        received_and_unread = await room.post(
            author="author", addressed_to=["viewer"], subject="c", topic="release"
        )
        await room.stamp_read("viewer", received_and_read)

        narrowed = _ids(
            await room.by_topic("release", viewer="viewer", unread_only=True)
        )

        assert narrowed == [received_and_unread]
        assert received_and_read not in narrowed, "a read recipient row must drop out"
        assert never_received not in narrowed, "a message with no viewer row must drop out"


async def test_topic_fetch_unread_only_ignores_the_viewers_own_sent_mail(isolated_env):
    """A message the viewer sent but did not receive has no recipient row.

    The sender is not a recipient of their own message, so under ``unread_only``
    the viewer's outgoing mail is excluded -- even though the default fetch,
    being project-wide, would have shown it.
    """
    async with mailroom("/iris/topic-sender", "viewer", "other") as room:
        sent_by_viewer = await room.post(
            author="viewer", addressed_to=["other"], subject="a", topic="release"
        )
        sent_to_viewer = await room.post(
            author="other", addressed_to=["viewer"], subject="b", topic="release"
        )

        assert sent_by_viewer in _ids(await room.by_topic("release"))
        assert _ids(await room.by_topic("release", viewer="viewer", unread_only=True)) == [
            sent_to_viewer
        ]
