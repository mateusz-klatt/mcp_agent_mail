"""The read-only `tickets` CLI group.

Read-only is a decision, not an omission, and one test pins it: writing a ticket records
an actor in an append-only audit row, and a CLI invocation has no authenticated agent
identity to record. The contact-policy check that governs every notification also lives in
the MCP tool bodies rather than in the delivery helper, so a service-layer write would skip
it silently.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest
from typer.testing import CliRunner

from mcp_agent_mail.cli import app
from mcp_agent_mail.db import ensure_schema, get_immediate_session, get_session, reset_database_state
from mcp_agent_mail.models import Agent, Project
from mcp_agent_mail.tickets import TicketActor, TicketUpdate, apply_ticket_update, create_ticket

pytestmark = pytest.mark.usefixtures("isolated_env")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

PROJECT_SLUG = "cli-tickets"
PROJECT_KEY = "/owner/cli-tickets"
AGENT = "claude-linux-holzera-1"


def _strip(text: str) -> str:
    return _ANSI.sub("", text)


def _run(*args: str) -> Any:
    """Invoke with a console wide enough that Rich never folds a line.

    Rich falls back to 80 columns when stdout is not a terminal, so a substring check at
    the default width passes or fails on where a wrap landed rather than on whether the
    text was printed.
    """
    return CliRunner().invoke(app, list(args), env={"COLUMNS": "400"})


def _seed(*, closed_one: bool = False) -> dict[str, str]:
    """Create a project, an agent, an epic, a child task and a loose bug."""

    async def _build() -> dict[str, str]:
        reset_database_state()
        await ensure_schema()
        async with get_immediate_session() as session:
            project = Project(slug=PROJECT_SLUG, human_key=PROJECT_KEY)
            session.add(project)
            await session.flush()
            agent = Agent(
                project_id=project.id,
                name=AGENT,
                program="claude-code",
                model="test",
                task_description="cli tests",
            )
            session.add(agent)
            await session.flush()
            actor = TicketActor.from_agent(agent)

            epic = await create_ticket(
                session, project=project, actor=actor, title="An epic", kind_key="epic"
            )
            child = await create_ticket(
                session,
                project=project,
                actor=actor,
                title="A child task",
                parent_id=epic.id,
                priority=1,
                assignee_agent_id=agent.id,
            )
            loose = await create_ticket(
                session, project=project, actor=actor, title="A loose bug", kind_key="bug"
            )
            if closed_one:
                await apply_ticket_update(
                    session,
                    ticket=loose,
                    project=project,
                    actor=actor,
                    update=TicketUpdate(status_key="closed", resolution_key="wontfix"),
                )
            await session.commit()
            return {"epic": epic.key, "child": child.key, "loose": loose.key}

    keys = asyncio.run(_build())

    async def _dispose() -> None:
        from mcp_agent_mail.db import get_engine

        await get_engine().dispose()

    asyncio.run(_dispose())
    return keys


def test_the_group_exposes_only_read_commands() -> None:
    """No delete, no close, no create. The absence is the contract.

    The suite already pins that irreversible hard-deletes must not exist as CLI commands;
    this extends that to tickets, where removal is a transition to `closed` rather than a
    deletion in the first place.
    """
    result = _run("tickets", "--help")
    assert result.exit_code == 0
    stdout = _strip(result.stdout)
    assert "list" in stdout
    assert "show" in stdout
    for forbidden in ("delete", "create", "close", "assign", "comment", "hard-delete"):
        assert forbidden not in stdout, f"`tickets {forbidden}` must not exist"


def test_list_help_names_every_option_it_promises() -> None:
    """A flag that silently disappears is a contract break the table tests cannot see."""
    result = _run("tickets", "list", "--help")
    assert result.exit_code == 0
    stdout = _strip(result.stdout)
    for flag in ("--status", "--kind", "--assignee", "--epic", "--include-closed", "--limit", "--json"):
        assert flag in stdout, f"{flag} vanished from `tickets list`"


def test_show_help_names_its_options() -> None:
    result = _run("tickets", "show", "--help")
    assert result.exit_code == 0
    assert "--json" in _strip(result.stdout)


def test_list_renders_the_worklist_most_urgent_first() -> None:
    keys = _seed()
    result = _run("tickets", "list", PROJECT_SLUG)
    assert result.exit_code == 0, result.output
    stdout = _strip(result.stdout)
    assert PROJECT_KEY in stdout
    # Priority 1 (the child) must precede the priority-3 rows.
    assert stdout.index(keys["child"]) < stdout.index(keys["epic"])
    assert AGENT in stdout, "the assignee column is not rendering"


def test_closed_tickets_are_excluded_until_asked_for() -> None:
    keys = _seed(closed_one=True)

    default = _run("tickets", "list", PROJECT_SLUG, "--json")
    assert default.exit_code == 0
    open_keys = {row["key"] for row in json.loads(default.stdout)}
    assert keys["loose"] not in open_keys

    everything = _run("tickets", "list", PROJECT_SLUG, "--include-closed", "--json")
    assert everything.exit_code == 0
    rows = {row["key"]: row for row in json.loads(everything.stdout)}
    assert keys["loose"] in rows
    assert rows[keys["loose"]]["resolution"] == "wontfix"


def test_filters_narrow_the_list() -> None:
    keys = _seed()

    by_kind = _run("tickets", "list", PROJECT_SLUG, "--kind", "epic", "--json")
    assert [row["key"] for row in json.loads(by_kind.stdout)] == [keys["epic"]]

    by_epic = _run("tickets", "list", PROJECT_SLUG, "--epic", keys["epic"], "--json")
    assert [row["key"] for row in json.loads(by_epic.stdout)] == [keys["child"]]

    by_assignee = _run("tickets", "list", PROJECT_SLUG, "--assignee", AGENT, "--json")
    assert [row["key"] for row in json.loads(by_assignee.stdout)] == [keys["child"]]


def test_show_renders_the_ticket_and_its_history() -> None:
    keys = _seed()
    result = _run("tickets", "show", keys["child"])
    assert result.exit_code == 0, result.output
    stdout = _strip(result.stdout)
    assert keys["child"] in stdout
    assert "A child task" in stdout
    assert "created" in stdout, "the history table is not rendering"


def test_show_json_carries_the_discussion_thread_id() -> None:
    """The CLI must not present the key as the conversation's identity."""
    keys = _seed()
    result = _run("tickets", "show", keys["epic"], "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    thread = payload["ticket"]["discussion_thread_id"]
    assert thread.startswith("tkt-")
    assert thread != payload["ticket"]["key"]


@pytest.mark.parametrize(
    "args",
    [
        ("tickets", "show", "NOPE-999", "--json"),
        ("tickets", "list", "no-such-project", "--json"),
        ("tickets", "list", PROJECT_SLUG, "--status", "invented", "--json"),
        ("tickets", "list", PROJECT_SLUG, "--limit", "9999", "--json"),
    ],
)
def test_json_failures_emit_exactly_one_error_object_and_exit_one(args: tuple[str, ...]) -> None:
    """Machine-readable failure is part of the contract, not a nicety.

    A caller parsing `--json` must never receive a Rich-formatted sentence, and must be
    able to tell failure from an empty result by the exit code alone.
    """
    _seed()
    result = _run(*args)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"error"}
    assert isinstance(payload["error"], str) and payload["error"]


def test_a_human_failure_is_not_json() -> None:
    """Control for the test above: without --json the same failure reads as prose."""
    _seed()
    result = _run("tickets", "show", "NOPE-999")
    assert result.exit_code == 1
    assert "Failed to show ticket" in _strip(result.stdout)


def test_an_empty_project_lists_nothing_and_still_succeeds() -> None:
    """An empty worklist is a result, not an error -- the distinction the exit code carries."""

    async def _build() -> None:
        reset_database_state()
        await ensure_schema()
        async with get_immediate_session() as session:
            session.add(Project(slug="empty-tickets", human_key="/owner/empty-tickets"))
            await session.commit()
        async with get_session() as session:
            pass
        from mcp_agent_mail.db import get_engine

        await get_engine().dispose()

    asyncio.run(_build())
    result = _run("tickets", "list", "empty-tickets", "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []
