"""Behaviour of the stale-mailbox sweep (`sweep_stale_agents`).

A session that exits without calling ``retire_agent`` leaves its Agent row
marked active for ever.  Enough of those and every broadcast runs contact
approval against dead mailboxes, so the sweep stamps ``retired_at`` on
whatever has been silent for longer than a caller-chosen threshold.

The interesting part of that contract is almost entirely negative -- what the
sweep must refuse to touch -- so these tests are written around the refusals:
the 60-second floor, the caller, other projects, half-provisioned mailboxes,
mailboxes with a session still running, and (for the on-demand tool) mailboxes
still holding a live file reservation.  Each refusal is paired with a control
that *is* retired in the same call, because "nothing happened" is not evidence
that a guard worked.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, cast

from fastmcp import Client
from sqlalchemy import select, update

from mcp_agent_mail import config as config_module
from mcp_agent_mail.app import (
    _EXECUTION_LIFECYCLE_PROTOCOL_VERSION,
    build_mcp_server,
    sweep_stale_agents,
)
from mcp_agent_mail.db import get_session
from mcp_agent_mail.models import Agent, FileReservation
from tests.keys import pkey

# Field names callers read off the sweep result. Fixed by the interface:
# http.py's background worker logs `agent_name`, `project_key` and
# `last_active_ts` straight out of these dicts.
RETIRED_ENTRY_FIELDS = frozenset(
    {"agent_id", "agent_name", "project_id", "project_key", "last_active_ts"}
)
SWEEP_TOOL_FIELDS = frozenset(
    {
        "project_key",
        "requested_by",
        "threshold_seconds",
        "require_no_active_reservations",
        "retired",
        "retired_agents",
        "count",
    }
)

TWO_DAYS = 48 * 3600
SESSION_TOKEN = "b" * 64


def drop_tz(moment: datetime) -> datetime:
    """Render ``moment`` the way SQLite holds it: UTC with the offset stripped."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


@asynccontextmanager
async def mailserver() -> AsyncIterator[Client]:
    """One in-process server plus a connected client, torn down together."""
    async with Client(build_mcp_server()) as client:
        yield client


async def open_project(client: Client, slug: str) -> dict[str, Any]:
    result = await client.call_tool("ensure_project", {"human_key": pkey(slug)})
    return {"key": result.data["human_key"], "id": result.data["id"]}


async def enrol(client: Client, project: dict[str, Any], mailbox: str) -> dict[str, Any]:
    result = await client.call_tool(
        "register_agent",
        {
            "project_key": project["key"],
            "program": "codex",
            "model": "gpt-5",
            "name": mailbox,
            "task_description": "stale-sweep fixture",
        },
    )
    return result.data


async def apply(*statements: Any) -> None:
    """Commit write statements the tools deliberately do not expose."""
    async with get_session() as session:
        for statement in statements:
            await session.execute(statement)
        await session.commit()


async def insert(*rows: Any) -> None:
    """Commit ORM rows the tools deliberately do not expose."""
    async with get_session() as session:
        for row in rows:
            session.add(row)
        await session.commit()


def from_now(seconds: int) -> datetime:
    """Naive-UTC instant ``seconds`` after the wall clock (negative for before)."""
    return drop_tz(datetime.now(timezone.utc)) + timedelta(seconds=seconds)


def idle_since(seconds: int) -> datetime:
    return from_now(-seconds)


def claim(
    project: dict[str, Any],
    agent_id: int,
    path_pattern: str,
    *,
    expires_ts: datetime,
    released_ts: datetime | None = None,
) -> FileReservation:
    return FileReservation(
        project_id=project["id"],
        agent_id=agent_id,
        path_pattern=path_pattern,
        exclusive=True,
        reason=f"fixture claim on {path_pattern}",
        expires_ts=expires_ts,
        released_ts=released_ts,
    )


async def claim_is_still_open(path_pattern: str) -> bool:
    async with get_session() as session:
        row = (
            await session.execute(
                select(FileReservation).where(
                    cast(Any, FileReservation.path_pattern) == path_pattern
                )
            )
        ).scalar_one()
        return row.released_ts is None


def rewind(mailbox: str, stamp: datetime) -> Any:
    return (
        update(Agent)
        .where(cast(Any, Agent.name) == mailbox)
        .values(last_active_ts=stamp)
    )


async def go_quiet(*mailboxes: str, seconds: int) -> datetime:
    """Rewind ``last_active_ts`` so the sweep sees these mailboxes as idle.

    Returns the instant written, so a test can check the sweep reports the
    timestamp that justified the decision rather than the retirement time.
    """
    stamp = idle_since(seconds)
    await apply(*(rewind(mailbox, stamp) for mailbox in mailboxes))
    return stamp


async def snapshot(*mailboxes: str) -> dict[str, dict[str, Any]]:
    """Read the sweep-relevant columns, resolved before the session closes."""
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Agent).where(cast(Any, Agent.name).in_(mailboxes))
            )
        ).scalars().all()
        return {
            row.name: {
                "id": row.id,
                "retired_at": row.retired_at,
                "last_active_ts": row.last_active_ts,
                "provisioning_state": row.provisioning_state,
            }
            for row in rows
        }


async def call_sweep_tool(
    client: Client, project: dict[str, Any], caller: dict[str, Any], **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_key": project["key"],
        "agent_name": caller["name"],
        "registration_token": caller["registration_token"],
    }
    payload.update(overrides)
    result = await client.call_tool("sweep_stale_agents", payload)
    return result.data


# --------------------------------------------------------------------------
# Configuration: the *background* sweep is a policy an operator opts into.
# --------------------------------------------------------------------------


def test_background_sweep_is_off_and_conservative_when_nothing_is_configured(
    isolated_env, monkeypatch, tmp_path
):
    for key in (
        "AUTO_RETIRE_STALE_AGENTS_ENABLED",
        "AUTO_RETIRE_STALE_AGENTS_INTERVAL_SECONDS",
        "AUTO_RETIRE_STALE_AGENTS_THRESHOLD_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    # A developer .env in the checkout must not decide the default.
    monkeypatch.setattr(config_module, "_DOTENV_PATH", tmp_path / "absent.env")
    config_module.clear_settings_cache()

    settings = config_module.get_settings()

    assert settings.auto_retire_stale_agents_enabled is False
    assert settings.auto_retire_stale_agents_interval_seconds == 3600
    assert settings.auto_retire_stale_agents_threshold_seconds == 86400


def test_operator_can_switch_the_background_sweep_on(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "_DOTENV_PATH", tmp_path / "absent.env")
    monkeypatch.setenv("AUTO_RETIRE_STALE_AGENTS_ENABLED", "true")
    config_module.clear_settings_cache()

    assert config_module.get_settings().auto_retire_stale_agents_enabled is True


# --------------------------------------------------------------------------
# The helper: who gets retired, and what the caller is told about it.
# --------------------------------------------------------------------------


async def test_sweep_retires_the_idle_mailbox_and_reports_why(isolated_env):
    async with mailserver() as client:
        project = await open_project(client, "sweep-threshold")
        await enrol(client, project, "codex-wsl-idle-1")
        await enrol(client, project, "codex-wsl-busy-1")
        idle_since = await go_quiet("codex-wsl-idle-1", seconds=TWO_DAYS)

        retired = await sweep_stale_agents(threshold_seconds=86400)

        assert [entry["agent_name"] for entry in retired] == ["codex-wsl-idle-1"]
        entry = retired[0]
        assert set(entry) == RETIRED_ENTRY_FIELDS
        assert entry["project_id"] == project["id"]
        assert entry["project_key"] == project["key"]
        # The reported timestamp is the silence that justified the decision,
        # not the moment of retirement.
        assert datetime.fromisoformat(entry["last_active_ts"]) == idle_since.replace(
            tzinfo=timezone.utc
        )

        rows = await snapshot("codex-wsl-idle-1", "codex-wsl-busy-1")
        # Retirement is a stamp, never a delete: both rows are still there.
        assert set(rows) == {"codex-wsl-idle-1", "codex-wsl-busy-1"}
        assert rows["codex-wsl-idle-1"]["retired_at"] is not None
        assert rows["codex-wsl-busy-1"]["retired_at"] is None
        # ... and the evidence of staleness is preserved, not overwritten.
        assert rows["codex-wsl-idle-1"]["last_active_ts"] == idle_since


async def test_a_retired_mailbox_is_never_retired_a_second_time(isolated_env):
    async with mailserver() as client:
        project = await open_project(client, "sweep-idempotent")
        await enrol(client, project, "codex-wsl-once-1")
        await go_quiet("codex-wsl-once-1", seconds=TWO_DAYS)

        first = await sweep_stale_agents(threshold_seconds=86400)
        assert [entry["agent_name"] for entry in first] == ["codex-wsl-once-1"]
        stamped = (await snapshot("codex-wsl-once-1"))["codex-wsl-once-1"]["retired_at"]
        assert stamped is not None

        second = await sweep_stale_agents(threshold_seconds=86400)

        assert second == []
        # The original retirement time survives the second pass unchanged.
        after = (await snapshot("codex-wsl-once-1"))["codex-wsl-once-1"]
        assert after["retired_at"] == stamped


async def test_threshold_is_floored_at_sixty_seconds(isolated_env):
    """A mis-set 0 (or worse) must not retire the whole database on the spot."""
    async with mailserver() as client:
        project = await open_project(client, "sweep-floor")
        await enrol(client, project, "codex-wsl-inside-1")
        await enrol(client, project, "codex-wsl-outside-1")

        moment = drop_tz(datetime.now(timezone.utc))
        await apply(
            rewind("codex-wsl-inside-1", moment - timedelta(seconds=59)),
            rewind("codex-wsl-outside-1", moment - timedelta(seconds=61)),
        )

        retired = await sweep_stale_agents(threshold_seconds=0, now=moment)

        # 61s of silence is past the floor; 59s is not.
        assert [entry["agent_name"] for entry in retired] == ["codex-wsl-outside-1"]
        assert (await snapshot("codex-wsl-inside-1"))["codex-wsl-inside-1"][
            "retired_at"
        ] is None

        # A negative threshold clamps to the same floor rather than wrapping past it.
        assert await sweep_stale_agents(threshold_seconds=-3600, now=moment) == []
        assert (await snapshot("codex-wsl-inside-1"))["codex-wsl-inside-1"][
            "retired_at"
        ] is None


async def test_now_fixes_both_the_cutoff_and_the_retirement_stamp(isolated_env):
    async with mailserver() as client:
        project = await open_project(client, "sweep-clock")
        await enrol(client, project, "codex-wsl-clock-1")
        await go_quiet("codex-wsl-clock-1", seconds=TWO_DAYS)

        # Deliberately offset-bearing and not UTC: an aware `now` is accepted
        # and normalised, and the stamp written is that instant -- an hour
        # behind the wall clock, so "it just used now()" cannot pass.
        chosen = datetime.now(timezone.utc) - timedelta(hours=1)
        aware_elsewhere = chosen.astimezone(timezone(timedelta(hours=5, minutes=30)))

        retired = await sweep_stale_agents(threshold_seconds=86400, now=aware_elsewhere)

        assert [entry["agent_name"] for entry in retired] == ["codex-wsl-clock-1"]
        stamped = (await snapshot("codex-wsl-clock-1"))["codex-wsl-clock-1"]["retired_at"]
        assert stamped == drop_tz(chosen)


async def test_a_mailbox_still_provisioning_is_invisible_to_the_sweep(isolated_env):
    """Retiring a half-registered mailbox would break the registration in flight.

    The row is inserted mid-provisioning rather than demoted into that state:
    a database trigger allows only provisioning -> active, which is exactly why
    the sweep must not be the thing that reaches such a row.
    """
    async with mailserver() as client:
        project = await open_project(client, "sweep-provisioning")
        await enrol(client, project, "codex-wsl-settled-1")
        await insert(
            Agent(
                project_id=project["id"],
                name="codex-wsl-halfborn-1",
                program="codex",
                model="gpt-5",
                task_description="registration still in flight",
                provisioning_state="provisioning",
                registration_token="halfborn-token",
                last_active_ts=idle_since(TWO_DAYS),
            )
        )
        await go_quiet("codex-wsl-settled-1", seconds=TWO_DAYS)

        retired = await sweep_stale_agents(threshold_seconds=86400)

        # The control retires in the same call, so the miss is the state, not the setup.
        assert [entry["agent_name"] for entry in retired] == ["codex-wsl-settled-1"]
        halfborn = (await snapshot("codex-wsl-halfborn-1"))["codex-wsl-halfborn-1"]
        assert halfborn["retired_at"] is None
        assert halfborn["provisioning_state"] == "provisioning"


async def test_a_running_execution_shields_a_quiet_mailbox_until_it_ends(isolated_env):
    """`last_active_ts` only tracks mail; a live session is still a live process."""
    async with mailserver() as client:
        project = await open_project(client, "sweep-execution")
        agent = await enrol(client, project, "codex-wsl-running-1")
        execution = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project["key"],
                "agent_name": agent["name"],
                "external_id": "sweep-shield-session",
                "client_name": "pytest",
                "execution_token": SESSION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_LIFECYCLE_PROTOCOL_VERSION,
                "registration_token": agent["registration_token"],
            },
        )
        await go_quiet("codex-wsl-running-1", seconds=TWO_DAYS)

        # No argument switches this guard off: even the most permissive sweep
        # available (60s threshold, reservations ignored) leaves it alone.
        spared = await sweep_stale_agents(
            threshold_seconds=60, require_no_active_reservations=False
        )
        assert spared == []
        assert (await snapshot("codex-wsl-running-1"))["codex-wsl-running-1"][
            "retired_at"
        ] is None

        await client.call_tool(
            "end_agent_execution",
            {
                "project_key": project["key"],
                "agent_name": agent["name"],
                "execution_id": execution.data["id"],
                "execution_token": SESSION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_LIFECYCLE_PROTOCOL_VERSION,
                "registration_token": agent["registration_token"],
            },
        )
        await go_quiet("codex-wsl-running-1", seconds=TWO_DAYS)

        retired = await sweep_stale_agents(threshold_seconds=60)

        assert [entry["agent_name"] for entry in retired] == ["codex-wsl-running-1"]


async def test_exclude_agent_id_spares_a_mailbox_that_is_itself_stale(isolated_env):
    """Asserted on the helper, where nothing else can be refreshing the survivor."""
    async with mailserver() as client:
        project = await open_project(client, "sweep-exclusion")
        await enrol(client, project, "codex-wsl-spared-1")
        await enrol(client, project, "codex-wsl-taken-1")
        await go_quiet("codex-wsl-spared-1", "codex-wsl-taken-1", seconds=TWO_DAYS)
        spared_id = (await snapshot("codex-wsl-spared-1"))["codex-wsl-spared-1"]["id"]

        retired = await sweep_stale_agents(
            threshold_seconds=86400, exclude_agent_id=spared_id
        )

        assert [entry["agent_name"] for entry in retired] == ["codex-wsl-taken-1"]
        assert (await snapshot("codex-wsl-spared-1"))["codex-wsl-spared-1"][
            "retired_at"
        ] is None


async def test_background_sweep_does_not_consult_file_reservations(isolated_env):
    """The helper's default is off; http.py's worker passes only a threshold."""
    async with mailserver() as client:
        project = await open_project(client, "sweep-unguarded")
        await enrol(client, project, "codex-wsl-unguarded-1")
        await go_quiet("codex-wsl-unguarded-1", seconds=TWO_DAYS)
        holder_id = (await snapshot("codex-wsl-unguarded-1"))["codex-wsl-unguarded-1"]["id"]
        await insert(
            claim(
                project,
                holder_id,
                "src/unguarded.py",
                expires_ts=from_now(3600),
            )
        )

        retired = await sweep_stale_agents(threshold_seconds=60)

        assert [entry["agent_name"] for entry in retired] == ["codex-wsl-unguarded-1"]


# --------------------------------------------------------------------------
# The on-demand tool: same engine, stricter defaults, an authenticated caller.
# --------------------------------------------------------------------------


async def test_on_demand_sweep_stays_in_the_callers_project_and_spares_the_caller(
    isolated_env,
):
    async with mailserver() as client:
        home = await open_project(client, "sweep-tool/home")
        away = await open_project(client, "sweep-tool/away")
        caller = await enrol(client, home, "codex-wsl-caller-1")
        await enrol(client, home, "codex-wsl-neighbour-1")
        await enrol(client, home, "codex-wsl-neighbour-2")
        await enrol(client, away, "codex-wsl-stranger-1")
        await go_quiet(
            "codex-wsl-caller-1",
            "codex-wsl-neighbour-1",
            "codex-wsl-neighbour-2",
            "codex-wsl-stranger-1",
            seconds=TWO_DAYS,
        )

        data = await call_sweep_tool(client, home, caller, threshold_seconds=0)

        assert set(data) >= SWEEP_TOOL_FIELDS
        assert data["project_key"] == home["key"]
        assert data["requested_by"] == caller["name"]
        # The clamped value is echoed back, so the caller learns 0 was refused.
        assert data["threshold_seconds"] == 60
        assert data["require_no_active_reservations"] is True
        # Ordered by agent id within the project.
        assert data["retired"] == ["codex-wsl-neighbour-1", "codex-wsl-neighbour-2"]
        assert data["count"] == 2
        assert [entry["agent_name"] for entry in data["retired_agents"]] == data["retired"]

        rows = await snapshot("codex-wsl-caller-1", "codex-wsl-stranger-1")
        assert rows["codex-wsl-caller-1"]["retired_at"] is None
        assert rows["codex-wsl-stranger-1"]["retired_at"] is None


async def test_only_a_live_reservation_shields_its_owner_from_the_on_demand_sweep(
    isolated_env,
):
    async with mailserver() as client:
        project = await open_project(client, "sweep-claims")
        caller = await enrol(client, project, "codex-wsl-claims-caller-1")
        await enrol(client, project, "codex-wsl-holder-live-1")
        await enrol(client, project, "codex-wsl-holder-expired-1")
        await enrol(client, project, "codex-wsl-holder-released-1")
        await go_quiet(
            "codex-wsl-claims-caller-1",
            "codex-wsl-holder-live-1",
            "codex-wsl-holder-expired-1",
            "codex-wsl-holder-released-1",
            seconds=TWO_DAYS,
        )

        holders = await snapshot(
            "codex-wsl-holder-live-1",
            "codex-wsl-holder-expired-1",
            "codex-wsl-holder-released-1",
        )
        await insert(
            claim(
                project,
                holders["codex-wsl-holder-live-1"]["id"],
                "src/live.py",
                expires_ts=from_now(3600),
            ),
            claim(
                project,
                holders["codex-wsl-holder-expired-1"]["id"],
                "src/lapsed.py",
                expires_ts=from_now(-3600),
            ),
            claim(
                project,
                holders["codex-wsl-holder-released-1"]["id"],
                "src/handed-back.py",
                expires_ts=from_now(3600),
                released_ts=from_now(-300),
            ),
        )

        guarded = await call_sweep_tool(client, project, caller, threshold_seconds=60)

        # Only an unreleased, unexpired claim counts as active.
        assert guarded["retired"] == [
            "codex-wsl-holder-expired-1",
            "codex-wsl-holder-released-1",
        ]
        assert (await snapshot("codex-wsl-holder-live-1"))["codex-wsl-holder-live-1"][
            "retired_at"
        ] is None

        forced = await call_sweep_tool(
            client,
            project,
            caller,
            threshold_seconds=60,
            require_no_active_reservations=False,
        )

        assert forced["require_no_active_reservations"] is False
        assert forced["retired"] == ["codex-wsl-holder-live-1"]

        repeated = await call_sweep_tool(
            client,
            project,
            caller,
            threshold_seconds=60,
            require_no_active_reservations=False,
        )
        assert repeated["retired"] == []
        assert repeated["count"] == 0

        # Retiring the owner is not reservation cleanup: the claim still stands.
        assert await claim_is_still_open("src/live.py") is True
