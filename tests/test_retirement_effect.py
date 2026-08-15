"""What retiring a mailbox actually does, as opposed to what it answers.

`retire_agent` and `unretire_agent` are one-line wrappers over the same helper;
the only thing that distinguishes them is a boolean:

    retire_agent    _stamp_agent_retirement(..., retired=True)
    unretire_agent  _stamp_agent_retirement(..., retired=False)

Every existing test asserts the RESPONSE — `{"status": "retired"}` or
`{"status": "active"}` — and those strings are written at the call site, next to
the flag. Swap the two flags and the responses keep their own labels while the
effect inverts: retiring reopens a mailbox and unretiring closes it. Nothing in
the suite reads the mailbox afterwards to notice.

These tests read the effect instead: whether mail is refused, and whether the
refusal is refused FOR RETIREMENT rather than for one of the other reasons a
send can fail.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server

KEY = "/test/retirement-effect"
SENDER = "claude-linux-reteffect-sender-1"
MAILBOX = "codex-linux-reteffect-mailbox-1"


def _data(result):
    return getattr(result, "data", None) or getattr(result, "structured_content", {})


async def _seed(server) -> tuple[dict, dict]:
    async with Client(server) as setup:
        await setup.call_tool("ensure_project", {"human_key": KEY})
        sender = _data(
            await setup.call_tool(
                "register_agent",
                {"project_key": KEY, "name": SENDER, "program": "p", "model": "p"},
            )
        )
        mailbox = _data(
            await setup.call_tool(
                "register_agent",
                {"project_key": KEY, "name": MAILBOX, "program": "p", "model": "p"},
            )
        )
    # The contact gate fires BEFORE the retirement gate, so without this the
    # first send is refused for lack of an approved contact and the test reads
    # a refusal that has nothing to do with retirement.
    async with Client(server) as requester:
        await requester.call_tool(
            "whois",
            {
                "project_key": KEY,
                "agent_name": SENDER,
                "registration_token": sender["registration_token"],
            },
        )
        await requester.call_tool(
            "request_contact",
            {"project_key": KEY, "from_agent": SENDER, "to_agent": MAILBOX},
        )
    async with Client(server) as approver:
        await approver.call_tool(
            "whois",
            {
                "project_key": KEY,
                "agent_name": MAILBOX,
                "registration_token": mailbox["registration_token"],
            },
        )
        await approver.call_tool(
            "respond_contact",
            {
                "project_key": KEY,
                "to_agent": MAILBOX,
                "from_agent": SENDER,
                "accept": True,
            },
        )
    return sender, mailbox


async def _send(client, tag: str):
    return await client.call_tool(
        "send_message",
        {
            "project_key": KEY,
            "sender_name": SENDER,
            "to": [MAILBOX],
            "subject": tag,
            "body_md": "body",
            "idempotency_key": f"reteffect-{tag}",
        },
    )


async def _as(server, name: str, token: str):
    client = Client(server)
    await client.__aenter__()
    await client.call_tool(
        "whois", {"project_key": KEY, "agent_name": name, "registration_token": token}
    )
    return client


@pytest.mark.asyncio
async def test_retiring_closes_the_mailbox_and_unretiring_reopens_it(isolated_env):
    """The direction of the flag, read from the mailbox rather than the reply.

    The two assertions are each other's control: if the flags were swapped, the
    first would find an open mailbox where it expects a closed one AND the
    second a closed one where it expects delivery. A single assertion could be
    satisfied by a mailbox that is simply always shut.
    """
    server = build_mcp_server()
    sender, mailbox = await _seed(server)

    owner = await _as(server, MAILBOX, mailbox["registration_token"])
    await owner.call_tool("retire_agent", {"project_key": KEY, "agent_name": MAILBOX})
    await owner.__aexit__(None, None, None)

    writer = await _as(server, SENDER, sender["registration_token"])
    with pytest.raises(Exception) as refused:
        await _send(writer, "while-retired")
    await writer.__aexit__(None, None, None)

    assert "is retired and no longer accepts new messages" in str(refused.value), (
        "a retired mailbox must refuse FOR RETIREMENT; any other refusal means "
        f"this test is reading a different failure; got: {refused.value}"
    )

    owner = await _as(server, MAILBOX, mailbox["registration_token"])
    await owner.call_tool("unretire_agent", {"project_key": KEY, "agent_name": MAILBOX})
    await owner.__aexit__(None, None, None)

    writer = await _as(server, SENDER, sender["registration_token"])
    delivered = _data(await _send(writer, "after-unretire"))
    await writer.__aexit__(None, None, None)

    assert delivered.get("deliveries"), (
        "unretiring must restore delivery, or the pair is one-way and the "
        f"mailbox is stranded; got: {delivered}"
    )
