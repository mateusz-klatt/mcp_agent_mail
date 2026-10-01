from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastmcp import Client, Context
from sqlalchemy.exc import NoResultFound

from mcp_agent_mail import app as app_module
from mcp_agent_mail.app import (
    ToolExecutionError,
    _enforce_capabilities,
    _iso,
    _latest_filesystem_activity,
    _latest_git_activity,
    _parse_iso,
    _parse_json_safely,
    _reservation_repo_pathspec,
    build_mcp_server,
)
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.models import Agent, Project
from tests.keys import pkey


@pytest.mark.asyncio
async def test_bound_agents_discard_stale_authority_and_keep_other_projects(monkeypatch):
    runtime = app_module._MCPServerRuntime(get_settings())
    project = Project(id=1, slug="current", human_key="/current")
    valid = Agent(id=1, project_id=1, name="valid", program="test", model="test")
    recycled = Agent(id=2, project_id=1, name="recycled", program="test", model="test")
    valid_binding = runtime._session_agent_binding(project, valid)
    stale_agent = replace(runtime._session_agent_binding(project, recycled), agent_generation="old")
    stale_project = replace(valid_binding, project_generation="old")
    missing = replace(valid_binding, agent_id=3)
    other_project = replace(valid_binding, project_id=2)
    bindings = {valid_binding, stale_agent, stale_project, missing, other_project}
    looked_up: set[int] = set()

    async def lookup(requested_project, agent_id):
        assert requested_project is project
        looked_up.add(agent_id)
        if agent_id == 3:
            raise NoResultFound
        return {1: valid, 2: recycled}[agent_id]

    monkeypatch.setattr(app_module, "_get_agent_by_id", lookup)

    resolved = await runtime._resolve_bound_agents_for_project(bindings, project)

    assert resolved == [valid]
    assert looked_up == {1, 2, 3}
    assert bindings == {valid_binding, other_project}


@pytest.mark.asyncio
async def test_bound_agent_resolution_keeps_snapshot_across_await(monkeypatch):
    runtime = app_module._MCPServerRuntime(get_settings())
    project = Project(id=1, slug="current", human_key="/current")
    agents = {
        number: Agent(id=number, project_id=1, name=f"agent-{number}", program="test", model="test")
        for number in (1, 2, 3)
    }
    bindings = {runtime._session_agent_binding(project, agents[number]) for number in (1, 2)}
    later_binding = runtime._session_agent_binding(project, agents[3])
    entered = asyncio.Event()
    resume = asyncio.Event()
    looked_up: set[int] = set()

    async def lookup(requested_project, agent_id):
        assert requested_project is project
        looked_up.add(agent_id)
        entered.set()
        await resume.wait()
        return agents[agent_id]

    monkeypatch.setattr(app_module, "_get_agent_by_id", lookup)

    async with asyncio.TaskGroup() as group:
        resolution = group.create_task(runtime._resolve_bound_agents_for_project(bindings, project))
        await asyncio.wait_for(entered.wait(), timeout=2)
        bindings.clear()
        bindings.add(later_binding)
        resume.set()

    assert {agent.id for agent in resolution.result()} == {1, 2}
    assert looked_up == {1, 2}
    assert bindings == {later_binding}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("known_agent", "stored_token", "provided_token", "authorized"),
    [
        (False, "secret", "secret", False),
        (True, "secret", "secret", True),
        (True, "secret", "wrong", False),
        (True, "secret", None, False),
        (True, "", "secret", False),
    ],
    ids=["unknown-agent", "matching-token", "wrong-token", "missing-token", "empty-stored-token"],
)
async def test_product_agent_token_authority(
    monkeypatch, known_agent, stored_token, provided_token, authorized,
):
    runtime = app_module._MCPServerRuntime(get_settings())
    ctx = cast(Context, SimpleNamespace(session_id="product-auth"))
    project = Project(id=1, slug="requested", human_key="/requested")
    agent = Agent(
        id=1, project_id=1, name="requested-agent", program="test", model="test",
        registration_token=stored_token,
    )
    other_project = Project(id=2, slug="other", human_key="/other")
    other_agent = Agent(id=2, project_id=2, name="other-agent", program="test", model="test")
    runtime._bind_session_agent(ctx, other_project, other_agent)
    other_binding = runtime._session_agent_binding(other_project, other_agent)

    async def lookup(requested_project, requested_name):
        assert requested_project is project
        assert requested_name == agent.name
        return agent if known_agent else None

    monkeypatch.setattr(app_module, "_find_agent_optional", lookup)

    resolved = await runtime._product_agent_for_project(ctx, project, agent.name, provided_token)

    assert runtime._session_current_agents_for(ctx).get(other_project.id) == other_binding
    if authorized:
        assert resolved is agent
        binding = runtime._session_agent_binding(project, agent)
        assert runtime._session_bindings_for(ctx) == {other_binding, binding}
        assert runtime._session_current_agents_for(ctx) == {1: binding, 2: other_binding}
    else:
        assert resolved is None
        assert runtime._session_bindings_for(ctx) == {other_binding}
        assert runtime._session_current_agents_for(ctx) == {2: other_binding}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_count", [1, 2], ids=["unique", "ambiguous"])
async def test_session_agent_fallback_requires_unique_project_authority(monkeypatch, agent_count):
    runtime = app_module._MCPServerRuntime(get_settings())
    ctx = cast(Context, SimpleNamespace(session_id="fallback-auth"))
    project = Project(id=1, slug="requested", human_key="/requested")
    agents = {
        number: Agent(id=number, project_id=1, name=f"agent-{number}", program="test", model="test")
        for number in range(1, agent_count + 1)
    }
    bindings = runtime._session_bindings_for(ctx)
    bindings.update(runtime._session_agent_binding(project, agent) for agent in agents.values())
    other_project = Project(id=2, slug="other", human_key="/other")
    other_agent = Agent(id=3, project_id=2, name="other-agent", program="test", model="test")
    runtime._bind_session_agent(ctx, other_project, other_agent)
    original_bindings = bindings.copy()
    original_current = runtime._session_current_agents_for(ctx).copy()
    looked_up: set[int] = set()

    async def lookup(requested_project, agent_id):
        assert requested_project is project
        looked_up.add(agent_id)
        return agents[agent_id]

    monkeypatch.setattr(app_module, "_get_agent_by_id", lookup)

    resolved = await runtime._resolve_session_agent_for_project(ctx, project)

    assert resolved is (agents[1] if agent_count == 1 else None)
    assert looked_up == set(agents)
    assert bindings == original_bindings
    assert runtime._session_current_agents_for(ctx) == original_current


def test_iso_and_parse_helpers():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert _iso(now).endswith("+00:00")
    assert _iso(now.isoformat()).endswith("+00:00")
    assert _iso("not-iso") == "not-iso"

    parsed = _parse_iso("2025-01-01T00:00:00Z")
    assert parsed is not None
    assert parsed.year == 2025
    assert _parse_iso("bad-value") is None

    raw = '{"a": 1}'
    assert _parse_json_safely(raw) == {"a": 1}
    fenced = """```json\n{\n  \"x\": 2\n}\n```"""
    assert _parse_json_safely(fenced) == {"x": 2}
    noisy = "xxx {\n \"y\": 3\n} yyy"
    assert _parse_json_safely(noisy) == {"y": 3}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('before ```json\n{"value": 1}\n``` after', {"value": 1}),
        ('```\n{"value": 2}\n```', {"value": 2}),
        ('```json\n{"value": 3}', {"value": 3}),
        ('```' + ' ' * 50_000, None),
        ('```json\n[]\n```', None),
    ],
    ids=["surrounded-json", "plain-fence", "unclosed-json", "long-whitespace", "array-rejected"],
)
def test_json_extraction_handles_fences_without_backtracking(payload, expected):
    assert _parse_json_safely(payload) == expected


def test_enforce_capabilities_denied():
    # Minimal stand-in that matches the Context metadata surface
    class DummyCtx:
        def __init__(self):
            self.metadata = {"allowed_capabilities": ["read", "audit"]}

    # Call through and expect a ToolExecutionError with explanatory message
    ctx = cast(Context, DummyCtx())
    with pytest.raises(ToolExecutionError) as exc:
        _enforce_capabilities(ctx, {"write"}, "send_message")
    assert "requires capabilities" in str(exc.value)


def test_latest_filesystem_activity_returns_max(tmp_path) -> None:
    older = tmp_path / "older.txt"
    newer = tmp_path / "newer.txt"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")

    old_ts = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
    new_ts = datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp()
    os.utime(older, (old_ts, old_ts))
    os.utime(newer, (new_ts, new_ts))

    latest = _latest_filesystem_activity([older, newer])

    assert latest is not None
    assert latest == datetime.fromtimestamp(new_ts, tz=timezone.utc)


def test_latest_filesystem_activity_early_exits_on_recent(tmp_path) -> None:
    # The sweeper only needs to know whether *any* match is recent; once a
    # recent mtime is seen it must stop, not stat the rest of a 56k-file glob
    # expansion on the event loop (#240). Prove the scan stops: the first file
    # is recent (inside the grace window) but the second is *even more* recent.
    # If the scan stopped at the first, the returned max is the first's mtime;
    # if it kept going it would observe the larger second mtime instead.
    now = datetime.now(timezone.utc)
    first_recent = tmp_path / "a_first_recent.txt"
    second_more_recent = tmp_path / "b_more_recent.txt"
    first_recent.write_text("x", encoding="utf-8")
    second_more_recent.write_text("y", encoding="utf-8")

    first_ts = (now - timedelta(seconds=100)).timestamp()
    second_ts = now.timestamp()
    os.utime(first_recent, (first_ts, first_ts))
    os.utime(second_more_recent, (second_ts, second_ts))
    recent_after = now - timedelta(seconds=300)

    latest = _latest_filesystem_activity(
        [first_recent, second_more_recent], recent_after=recent_after
    )

    # Returned the first (recent) mtime, NOT the larger second one -> stopped early.
    assert latest is not None
    assert latest == datetime.fromtimestamp(first_ts, tz=timezone.utc)
    assert latest < datetime.fromtimestamp(second_ts, tz=timezone.utc)

    # And without a recent_after window it must still scan all and return the max.
    assert _latest_filesystem_activity(
        [first_recent, second_more_recent]
    ) == datetime.fromtimestamp(second_ts, tz=timezone.utc)


def test_reservation_repo_pathspec_glob_and_exact(tmp_path) -> None:
    git = pytest.importorskip("git")
    repo = git.Repo.init(tmp_path)
    workspace = Path(tmp_path)

    # Glob pattern -> single `:(glob)` magic pathspec (one rev walk, #240).
    assert (
        _reservation_repo_pathspec(repo, workspace, "frontend/**")
        == ":(glob)frontend/**"
    )
    # Exact path -> plain repo-relative pathspec (no magic needed).
    assert _reservation_repo_pathspec(repo, workspace, "README.md") == "README.md"
    # Virtual namespaces have no git presence.
    assert _reservation_repo_pathspec(repo, workspace, "tool://playwright") is None


def test_latest_git_activity_single_glob_walk(tmp_path) -> None:
    git = pytest.importorskip("git")
    repo = git.Repo.init(tmp_path)

    # Two files under a broad glob (one nested like node_modules), one outside.
    (tmp_path / "frontend" / "deep" / "pkg").mkdir(parents=True)
    (tmp_path / "frontend" / "a.js").write_text("1", encoding="utf-8")
    (tmp_path / "frontend" / "deep" / "pkg" / "b.js").write_text("1", encoding="utf-8")
    (tmp_path / "other.txt").write_text("1", encoding="utf-8")
    repo.index.add(["frontend/a.js", "frontend/deep/pkg/b.js", "other.txt"])
    tree_commit = repo.index.commit("init")

    pathspec = _reservation_repo_pathspec(repo, Path(tmp_path), "frontend/**")
    activity = _latest_git_activity(repo, pathspec)
    assert activity is not None
    assert activity == datetime.fromtimestamp(
        tree_commit.committed_date, tz=timezone.utc
    )

    # A later commit touching ONLY a path outside the glob must not move the
    # reported activity for `frontend/**` (semantic equivalence to per-file max).
    import time

    time.sleep(1.1)
    (tmp_path / "other.txt").write_text("2", encoding="utf-8")
    repo.index.add(["other.txt"])
    repo.index.commit("touch other")
    after = _latest_git_activity(repo, pathspec)
    assert after == datetime.fromtimestamp(tree_commit.committed_date, tz=timezone.utc)


@pytest.mark.asyncio
async def test_tool_metrics_resource_populates_after_calls(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        # call a couple tools to increment metrics
        res = await client.call_tool("health_check", {})
        assert res.data["status"] == "ok"
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})

        # tooling metrics resource
        metrics_blocks = await client.read_resource("resource://tooling/metrics")
        assert metrics_blocks
        assert metrics_blocks[0].text
        # the text is JSON; ensure tools list contains health_check
        assert "health_check" in metrics_blocks[0].text
