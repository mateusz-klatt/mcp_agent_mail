import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy.exc import IntegrityError

from mcp_agent_mail.app import _compute_project_slug, _resolve_project_identity, build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Project


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return cp.stdout.strip()


def _repo_with_linked_worktree(tmp_path: Path, *, project_uid: str) -> tuple[Path, Path]:
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    (repo / ".agent-mail-project-id").write_text(f"{project_uid}\n", encoding="utf-8")
    _git(repo, "add", "README.md", ".agent-mail-project-id")
    _git(repo, "commit", "-m", "init")

    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", str(worktree), "-b", "feature/worktree")
    return repo, worktree


@pytest.mark.skipif(subprocess.call(["git", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0, reason="git not available")
def test_identity_same_across_worktrees(tmp_path: Path, monkeypatch) -> None:
    # Enable worktrees and choose git-common-dir mode for stable identity across worktrees
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("PROJECT_IDENTITY_MODE", "git-common-dir")
    get_settings.cache_clear()

    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")

    wt = tmp_path / "wt1"
    _git(repo, "worktree", "add", str(wt), "-b", "feature/wt1")

    ident_main = _resolve_project_identity(str(repo))
    ident_wt = _resolve_project_identity(str(wt))
    assert ident_main["project_uid"] == ident_wt["project_uid"]
    assert ident_main["slug"] == ident_wt["slug"]


@pytest.mark.asyncio
async def test_project_uid_joins_linked_worktrees_and_register_lookup(
    isolated_env,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A marker joins DB identity; the first path remains the canonical alias."""
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("PROJECT_IDENTITY_MODE", "dir")
    get_settings.cache_clear()
    repo, worktree = _repo_with_linked_worktree(
        tmp_path,
        project_uid="project-worktree-shared",
    )

    server = build_mcp_server()
    async with Client(server) as client:
        first = await client.call_tool("ensure_project", {"human_key": str(repo)})
        second = await client.call_tool("ensure_project", {"human_key": str(worktree)})

        assert second.data["id"] == first.data["id"]
        assert second.data["project_uid"] == "project-worktree-shared"
        assert second.data["human_key"] == first.data["human_key"] == str(repo)

        registered = await client.call_tool(
            "register_agent",
            {
                "project_key": str(worktree),
                "program": "codex-cli",
                "model": "gpt-5",
                "name": "codex-wsl-test-1",
            },
        )
        assert registered.data["project_id"] == first.data["id"]


@pytest.mark.asyncio
async def test_project_uid_joins_separate_clones_by_normalized_remote(
    isolated_env,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Host-local clone paths are aliases of the same normalized origin."""
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("PROJECT_IDENTITY_MODE", "dir")
    get_settings.cache_clear()

    clones: list[Path] = []
    for name in ("host-a", "host-b"):
        clone = tmp_path / name
        clone.mkdir()
        _git(clone, "init")
        _git(clone, "remote", "add", "origin", "git@github.com:Example/Shared.git")
        clones.append(clone)

    server = build_mcp_server()
    async with Client(server) as client:
        first = await client.call_tool("ensure_project", {"human_key": str(clones[0])})
        second = await client.call_tool("ensure_project", {"human_key": str(clones[1])})

    assert second.data["id"] == first.data["id"]
    assert second.data["project_uid"] == first.data["project_uid"]
    assert second.data["human_key"] == first.data["human_key"] == str(clones[0])


@pytest.mark.asyncio
async def test_different_project_uids_create_distinct_rows(
    isolated_env,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("PROJECT_IDENTITY_MODE", "dir")
    get_settings.cache_clear()

    repos: list[Path] = []
    for name, project_uid in (("one", "project-one"), ("two", "project-two")):
        repo = tmp_path / name
        repo.mkdir()
        _git(repo, "init")
        (repo / ".agent-mail-project-id").write_text(f"{project_uid}\n", encoding="utf-8")
        repos.append(repo)

    server = build_mcp_server()
    async with Client(server) as client:
        first = await client.call_tool("ensure_project", {"human_key": str(repos[0])})
        second = await client.call_tool("ensure_project", {"human_key": str(repos[1])})

    assert first.data["id"] != second.data["id"]
    assert first.data["project_uid"] != second.data["project_uid"]


@pytest.mark.asyncio
async def test_ambiguous_legacy_worktree_rows_fail_closed(
    isolated_env,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Two historical rows are never silently merged under one new UID."""
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("PROJECT_IDENTITY_MODE", "dir")
    get_settings.cache_clear()
    repo, worktree = _repo_with_linked_worktree(
        tmp_path,
        project_uid="project-ambiguous-history",
    )

    await ensure_schema()
    async with get_session() as session:
        main_legacy = Project(slug="legacy-main", human_key=str(repo))
        worktree_legacy = Project(slug="legacy-worktree", human_key=str(worktree))
        session.add(main_legacy)
        session.add(worktree_legacy)
        await session.commit()
        await session.refresh(main_legacy)
        await session.refresh(worktree_legacy)

    server = build_mcp_server()
    async with Client(server) as client:
        claimed = await client.call_tool("ensure_project", {"human_key": str(repo)})
        assert claimed.data["id"] == main_legacy.id
        assert claimed.data["project_uid"] == "project-ambiguous-history"

        with pytest.raises(ToolError, match="refusing to merge existing project history"):
            await client.call_tool("ensure_project", {"human_key": str(worktree)})


@pytest.mark.asyncio
async def test_identity_mode_override_selects_persisted_slug(
    isolated_env,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The per-call mode changes the row written to DB, not just metadata."""
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("PROJECT_IDENTITY_MODE", "dir")
    get_settings.cache_clear()

    repo = tmp_path / "override-repo"
    repo.mkdir()
    _git(repo, "init")
    expected_slug = _compute_project_slug(str(repo), mode_override="git-common-dir")
    assert expected_slug != _compute_project_slug(str(repo), mode_override="dir")

    server = build_mcp_server()
    async with Client(server) as client:
        result = await client.call_tool(
            "ensure_project",
            {"human_key": str(repo), "identity_mode": "git-common-dir"},
        )

    assert result.data["slug"] == expected_slug
    async with get_session() as session:
        persisted = await session.get(Project, result.data["id"])
        assert persisted is not None
        assert persisted.slug == expected_slug
        assert persisted.project_uid == result.data["project_uid"]


@pytest.mark.asyncio
async def test_legacy_project_uid_migration_is_lazy_and_unique(
    isolated_env,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Startup preserves legacy history; first exact use claims it once."""
    monkeypatch.setenv("WORKTREES_ENABLED", "0")
    get_settings.cache_clear()
    database_path = tmp_path / "test.sqlite3"
    legacy_human_key = "/legacy/project"
    with sqlite3.connect(database_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE projects (
                id INTEGER NOT NULL PRIMARY KEY,
                slug VARCHAR(255) NOT NULL UNIQUE,
                human_key VARCHAR(255) NOT NULL,
                project_generation VARCHAR(64),
                created_at DATETIME NOT NULL,
                archived_at DATETIME
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO projects (
                id, slug, human_key, project_generation, created_at, archived_at
            ) VALUES (
                41, 'legacy-project', ?,
                '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
                '2025-01-01 00:00:00', NULL
            )
            """,
            (legacy_human_key,),
        )

    await ensure_schema()
    async with get_session() as session:
        legacy_project = await session.get(Project, 41)
        assert legacy_project is not None
        assert legacy_project.project_uid is None

    server = build_mcp_server()
    async with Client(server) as client:
        claimed = await client.call_tool(
            "ensure_project",
            {"human_key": legacy_human_key},
        )

    assert claimed.data["id"] == 41
    assert claimed.data["project_uid"]

    async with get_session() as session:
        session.add(
            Project(
                slug="duplicate-project-uid",
                human_key="/other/project",
                project_uid=claimed.data["project_uid"],
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
