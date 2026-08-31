"""The four ticketing MCP tools, driven through a real client.

These assert the adapter contract only -- authentication, project scoping, payload shape
and the TOON envelope. The domain rules they delegate to live in `test_tickets_service.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.db import ensure_schema, reset_database_state
from tests.keys import pkey

pytestmark = pytest.mark.usefixtures("isolated_env")


async def _call(tool_name: str, args: dict[str, Any]) -> Any:
    async with Client(build_mcp_server()) as client:
        result = await client.call_tool(tool_name, args)
    return result.data


async def _call_raw(tool_name: str, args: dict[str, Any]) -> Any:
    async with Client(build_mcp_server()) as client:
        return await client.call_tool(tool_name, args)


def _boot() -> None:
    clear_settings_cache()
    reset_database_state()
    asyncio.run(ensure_schema())


async def _project_with_agent(
    project_key: str, agent_name: str = "claude-linux-holzera-1"
) -> str:
    """Create a project and a registered agent, returning that agent's token."""
    await _call("ensure_project", {"human_key": project_key})
    registered = await _call(
        "register_agent",
        {
            "project_key": project_key,
            "name": agent_name,
            "program": "claude-code",
            "model": "test",
            "task_description": "ticketing tests",
        },
    )
    return str(registered["registration_token"])


def test_the_four_tools_are_registered_unconditionally() -> None:
    """Not behind a settings flag.

    Eight existing tools vanish on a default install because WORKTREES_ENABLED defaults to
    False, and a headline feature nobody can find has not shipped.
    """

    async def scenario() -> None:
        async with Client(build_mcp_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert {"create_ticket", "get_ticket", "list_tickets", "update_ticket"} <= names

    _boot()
    asyncio.run(scenario())


def test_a_ticket_round_trips_through_create_and_get() -> None:
    async def scenario() -> None:
        project_key = pkey("tickets-round-trip")
        token = await _project_with_agent(project_key)

        created = await _call(
            "create_ticket",
            {
                "project_key": project_key,
                "agent_name": "claude-linux-holzera-1",
                "title": "Rotate the signing key",
                "kind": "task",
                "priority": 1,
                "registration_token": token,
            },
        )
        assert created["status"] == "open"
        assert created["priority"] == 1
        assert created["key"].endswith("-1")
        assert created["discussion_thread_id"].startswith("tkt-")
        assert created["discussion_thread_id"] != created["key"]

        fetched = await _call(
            "get_ticket",
            {
                "project_key": project_key,
                "agent_name": "claude-linux-holzera-1",
                "ticket_key": created["key"].lower(),  # keys match case-insensitively
                "include_events": True,
                "registration_token": token,
            },
        )
        assert fetched["ticket"]["key"] == created["key"]
        assert fetched["links"] == []
        assert [event["event_type"] for event in fetched["events"]] == ["created"]

    _boot()
    asyncio.run(scenario())


def test_list_tickets_survives_the_toon_envelope() -> None:
    """The one assertion that catches a `list[dict]` return annotation.

    A list-returning tool must be annotated `-> ToonableList`. With a bare list
    annotation, `format="toon"` puts a dict envelope where the list belongs and breaks the
    tool's own declared output schema.
    """

    async def scenario() -> None:
        project_key = pkey("tickets-toon")
        token = await _project_with_agent(project_key)
        for index in range(3):
            await _call(
                "create_ticket",
                {
                    "project_key": project_key,
                    "agent_name": "claude-linux-holzera-1",
                    "title": f"ticket {index}",
                    "priority": index,
                    "registration_token": token,
                },
            )

        plain = await _call_raw(
            "list_tickets",
            {
                "project_key": project_key,
                "agent_name": "claude-linux-holzera-1",
                "registration_token": token,
            },
        )
        rows = plain.structured_content["result"]
        assert [row["priority"] for row in rows] == [0, 1, 2], "canonical order is priority ASC"

        toon = await _call_raw(
            "list_tickets",
            {
                "project_key": project_key,
                "agent_name": "claude-linux-holzera-1",
                "registration_token": token,
                "format": "toon",
            },
        )
        assert toon.is_error is False

    _boot()
    asyncio.run(scenario())


def test_closed_tickets_leave_the_worklist_unless_asked_for() -> None:
    async def scenario() -> None:
        project_key = pkey("tickets-closed")
        token = await _project_with_agent(project_key)
        common = {
            "project_key": project_key,
            "agent_name": "claude-linux-holzera-1",
            "registration_token": token,
        }
        first = await _call("create_ticket", {**common, "title": "will close"})
        await _call("create_ticket", {**common, "title": "stays open"})

        updated = await _call(
            "update_ticket",
            {**common, "ticket_key": first["key"], "status": "closed", "resolution": "done"},
        )
        assert updated["ticket"]["is_closed"] is True
        assert "status_key" in updated["changed_fields"]
        assert updated["revision"] == 2

        open_only = await _call("list_tickets", common)
        assert [row["title"] for row in open_only] == ["stays open"]

        everything = await _call("list_tickets", {**common, "include_closed": True})
        assert len(everything) == 2

    _boot()
    asyncio.run(scenario())


def test_closing_without_a_resolution_is_refused_with_its_domain_code() -> None:
    async def scenario() -> None:
        project_key = pkey("tickets-refusal")
        token = await _project_with_agent(project_key)
        common = {
            "project_key": project_key,
            "agent_name": "claude-linux-holzera-1",
            "registration_token": token,
        }
        ticket = await _call("create_ticket", {**common, "title": "needs a resolution"})

        with pytest.raises(Exception) as refusal:
            await _call("update_ticket", {**common, "ticket_key": ticket["key"], "status": "closed"})
        assert "resolution_required" in str(refusal.value)

        # Control: the same call with a resolution is accepted.
        done = await _call(
            "update_ticket",
            {**common, "ticket_key": ticket["key"], "status": "closed", "resolution": "done"},
        )
        assert done["ticket"]["resolution"] == "done"

    _boot()
    asyncio.run(scenario())


def test_a_stale_expected_revision_is_refused() -> None:
    async def scenario() -> None:
        project_key = pkey("tickets-cas")
        token = await _project_with_agent(project_key)
        common = {
            "project_key": project_key,
            "agent_name": "claude-linux-holzera-1",
            "registration_token": token,
        }
        ticket = await _call("create_ticket", {**common, "title": "contended"})

        await _call(
            "update_ticket",
            {**common, "ticket_key": ticket["key"], "title": "first writer", "expected_revision": 1},
        )
        with pytest.raises(Exception) as refusal:
            await _call(
                "update_ticket",
                {
                    **common,
                    "ticket_key": ticket["key"],
                    "title": "stale writer",
                    "expected_revision": 1,
                },
            )
        assert "revision_conflict" in str(refusal.value)

        current = await _call("get_ticket", {**common, "ticket_key": ticket["key"]})
        assert current["ticket"]["title"] == "first writer"

    _boot()
    asyncio.run(scenario())


def test_a_ticket_in_another_project_is_not_readable_even_with_its_global_key() -> None:
    """Keys are globally unique, but a key is a label and not an authorization.

    The refusal is NOT_FOUND rather than FORBIDDEN on purpose: confirming that a key exists
    in a project the caller cannot see is itself a disclosure.
    """

    async def scenario() -> None:
        alpha = pkey("tickets-alpha")
        beta = pkey("tickets-beta")
        alpha_token = await _project_with_agent(alpha)
        beta_token = await _project_with_agent(beta, agent_name="claude-mac-laptop-1")

        secret = await _call(
            "create_ticket",
            {
                "project_key": alpha,
                "agent_name": "claude-linux-holzera-1",
                "title": "only alpha may see this",
                "registration_token": alpha_token,
            },
        )

        with pytest.raises(Exception) as refusal:
            await _call(
                "get_ticket",
                {
                    "project_key": beta,
                    "agent_name": "claude-mac-laptop-1",
                    "ticket_key": secret["key"],
                    "registration_token": beta_token,
                },
            )
        assert "ticket_not_found" in str(refusal.value) or "No ticket" in str(refusal.value)

        # Control: the owning project reads it back fine, so the refusal is the scoping.
        mine = await _call(
            "get_ticket",
            {
                "project_key": alpha,
                "agent_name": "claude-linux-holzera-1",
                "ticket_key": secret["key"],
                "registration_token": alpha_token,
            },
        )
        assert mine["ticket"]["key"] == secret["key"]

    _boot()
    asyncio.run(scenario())


def test_a_wrong_registration_token_is_refused_for_every_tool() -> None:
    async def scenario() -> None:
        project_key = pkey("tickets-auth")
        token = await _project_with_agent(project_key)
        ticket = await _call(
            "create_ticket",
            {
                "project_key": project_key,
                "agent_name": "claude-linux-holzera-1",
                "title": "guarded",
                "registration_token": token,
            },
        )
        wrong = "0" * 43
        calls = {
            "create_ticket": {"title": "should not exist"},
            "get_ticket": {"ticket_key": ticket["key"]},
            "list_tickets": {},
            "update_ticket": {"ticket_key": ticket["key"], "title": "nope"},
        }
        for tool_name, extra in calls.items():
            with pytest.raises(ToolError) as refusal:
                await _call(
                    tool_name,
                    {
                        "project_key": project_key,
                        "agent_name": "claude-linux-holzera-1",
                        "registration_token": wrong,
                        **extra,
                    },
                )
            # Named, so a refusal for some unrelated reason cannot pass as an auth check.
            assert "token" in str(refusal.value).lower(), f"{tool_name}: {refusal.value}"

    _boot()
    asyncio.run(scenario())


def test_an_epic_takes_children_and_a_task_does_not() -> None:
    async def scenario() -> None:
        project_key = pkey("tickets-epic")
        token = await _project_with_agent(project_key)
        common = {
            "project_key": project_key,
            "agent_name": "claude-linux-holzera-1",
            "registration_token": token,
        }
        epic = await _call("create_ticket", {**common, "title": "an epic", "kind": "epic"})
        task = await _call("create_ticket", {**common, "title": "a task"})

        child = await _call(
            "create_ticket", {**common, "title": "a child", "parent_key": epic["key"]}
        )
        assert child["parent_id"] is not None

        with pytest.raises(Exception) as refusal:
            await _call(
                "create_ticket", {**common, "title": "orphan", "parent_key": task["key"]}
            )
        assert "parent_not_an_epic" in str(refusal.value)

        children = await _call("list_tickets", {**common, "parent_key": epic["key"]})
        assert [row["title"] for row in children] == ["a child"]

    _boot()
    asyncio.run(scenario())


def test_an_unknown_assignee_is_refused_rather_than_silently_dropped() -> None:
    async def scenario() -> None:
        project_key = pkey("tickets-assignee")
        token = await _project_with_agent(project_key)
        common = {
            "project_key": project_key,
            "agent_name": "claude-linux-holzera-1",
            "registration_token": token,
        }
        with pytest.raises(Exception) as refusal:
            await _call(
                "create_ticket",
                {**common, "title": "assigned to nobody", "assignee_name": "ghost-agent-9"},
            )
        assert "ghost-agent-9" in str(refusal.value)

        # Control: assigning to a real agent works and shows up in the filter.
        assigned = await _call(
            "create_ticket",
            {**common, "title": "assigned", "assignee_name": "claude-linux-holzera-1"},
        )
        assert assigned["assignee_agent_id"] is not None
        mine = await _call(
            "list_tickets", {**common, "assignee_name": "claude-linux-holzera-1"}
        )
        assert [row["key"] for row in mine] == [assigned["key"]]

    _boot()
    asyncio.run(scenario())
