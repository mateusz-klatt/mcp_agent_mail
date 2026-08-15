"""What multi-thread summarisation must not leak, and where its mode boundary sits.

`test_summarize_threads_extended.py` covers this branch already, but asserts only
that both requested ids come back in `threads[]`. Two properties the code states
about itself are held by nothing:

1. The visibility gate. The multi-thread loop carries this comment:

       # Same visibility gate as the single-thread path: batching several
       # threads into one call must not widen what the caller may read.

   That is a security claim about a batching convenience — exactly the shape
   that gets lost in a rewrite, because dropping `viewer=viewer` makes every
   test that only counts thread ids stay green.

2. The mode boundary. Single- and multi-thread mode return *different shapes*,
   and the switch is `len(thread_ids) == 1` computed after empty entries are
   dropped. So "T1," is one thread, not two. A rewrite reaching for the obvious
   `"," in thread_id` flips that, and no existing test would notice.

The existing test is also named `..._non_llm_mode_and_limit` and passes
`per_thread_limit=2` without asserting anything about the limit. Not fixed here
— named so nobody reads its name as coverage.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server

KEY = "/test/summarize-gate"
OWNER = "claude-linux-summgate-owner-1"
OUTSIDER = "codex-linux-summgate-outsider-1"
SECRET = "the-turbine-bearing-replacement-schedule"


def _data(result):
    return getattr(result, "data", None) or getattr(result, "structured_content", {})


async def _seed(server) -> tuple[dict, dict]:
    async with Client(server) as setup:
        await setup.call_tool("ensure_project", {"human_key": KEY})
        owner = _data(
            await setup.call_tool(
                "register_agent",
                {"project_key": KEY, "name": OWNER, "program": "p", "model": "p"},
            )
        )
        outsider = _data(
            await setup.call_tool(
                "register_agent",
                {"project_key": KEY, "name": OUTSIDER, "program": "p", "model": "p"},
            )
        )
        # One thread both may read, one addressed to the owner alone.
        await setup.call_tool(
            "send_message",
            {
                "project_key": KEY,
                "sender_name": OWNER,
                "to": [OWNER, OUTSIDER],
                "subject": "shared",
                "body_md": "everyone may read this one",
                "thread_id": "OPEN",
                "idempotency_key": "summgate-open-1",
            },
        )
        await setup.call_tool(
            "send_message",
            {
                "project_key": KEY,
                "sender_name": OWNER,
                "to": [OWNER],
                "subject": "private",
                "body_md": f"mentions {SECRET} and nothing else",
                "thread_id": "CLOSED",
                "idempotency_key": "summgate-closed-1",
            },
        )
    return owner, outsider


async def _summarise(client, thread_id: str, agent: str, token: str) -> dict:
    return _data(
        await client.call_tool(
            "summarize_thread",
            {
                "project_key": KEY,
                "thread_id": thread_id,
                "llm_mode": False,
                "agent_name": agent,
                "registration_token": token,
            },
        )
    )


@pytest.mark.asyncio
async def test_batching_threads_does_not_widen_what_a_viewer_may_read(isolated_env):
    """The claim the code makes in a comment and no test held.

    The positive control is in the same test and is what makes the negative
    meaningful: the owner asking for the same two threads DOES see the private
    content, so an empty result for the outsider is the gate working rather than
    the fixture failing to write anything.
    """
    server = build_mcp_server()
    owner, outsider = await _seed(server)

    def _closed(payload: dict) -> dict:
        return next(
            t["summary"] for t in payload["threads"] if t["thread_id"] == "CLOSED"
        )

    async with Client(server) as intruder:
        theirs = _closed(
            await _summarise(intruder, "OPEN,CLOSED", OUTSIDER, outsider["registration_token"])
        )
    async with Client(server) as reader:
        mine = _closed(
            await _summarise(reader, "OPEN,CLOSED", OWNER, owner["registration_token"])
        )

    # Positive control first: the thread must be visible to SOMEBODY, or the
    # refusal below is satisfied by an empty database.
    assert mine["total_messages"] == 1 and OWNER in mine["participants"], (
        f"positive control failed: the owner must see their own thread; got {mine}"
    )
    assert theirs["total_messages"] == 0 and theirs["participants"] == [], (
        "asking for a readable thread and an unreadable one in a single call must "
        f"not surface the unreadable one; got {theirs}"
    )


@pytest.mark.asyncio
async def test_a_trailing_comma_is_still_one_thread(isolated_env):
    """The mode boundary, which decides the response SHAPE.

    Single-thread mode answers {thread_id, summary, examples}; multi-thread mode
    answers {threads[], aggregate}. A caller that appends a separator while
    building a list of one gets a different shape back if the boundary moves,
    and the two existing tests both pass ids without stray separators.
    """
    server = build_mcp_server()
    owner, _ = await _seed(server)

    async with Client(server) as reader:
        plain = await _summarise(reader, "OPEN", OWNER, owner["registration_token"])
        trailing = await _summarise(
            reader, "OPEN,", OWNER, owner["registration_token"]
        )
        spaced = await _summarise(
            reader, " OPEN , ", OWNER, owner["registration_token"]
        )

    assert "thread_id" in plain and "threads" not in plain, (
        f"a single id must answer in single-thread shape; got keys {sorted(plain)}"
    )
    for label, payload in (("trailing comma", trailing), ("padded", spaced)):
        assert "thread_id" in payload and "threads" not in payload, (
            f"{label} still names one thread, so the shape must not change; "
            f"got keys {sorted(payload)}"
        )


@pytest.mark.asyncio
async def test_two_ids_answer_in_multi_thread_shape(isolated_env):
    """Positive control for the boundary test above.

    Without it, a build that answered single-thread shape for *everything* would
    satisfy the assertions above perfectly and destroy the aggregate digest.
    """
    server = build_mcp_server()
    owner, _ = await _seed(server)

    async with Client(server) as reader:
        both = await _summarise(
            reader, "OPEN,CLOSED", OWNER, owner["registration_token"]
        )

    assert "threads" in both and "aggregate" in both, (
        f"two ids must answer in multi-thread shape; got keys {sorted(both)}"
    )
    assert {t.get("thread_id") for t in both["threads"]} == {"OPEN", "CLOSED"}
