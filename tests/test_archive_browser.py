"""Tests for the Git archive browser and visualization routes.

Tests all /mail/archive/* endpoints for proper rendering and functionality.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from mcp_agent_mail import config as _config, webauth
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.storage import ensure_archive, get_commit_detail, write_agent_profile, write_message_bundle


# The viewer is behind a password login by default, and these tests exercise
# what the pages RENDER, not who may see them — the login has its own coverage
# in tests/test_http_auth.py and tests/test_mail_ui_auth*.py. Without this every
# request here answers 401 and the assertions read as broken templates.
#
# Turned off per file rather than in a shared fixture, deliberately: the default
# has to stay ON, because it is what protects a real deployment, and a fixture
# that disabled it globally would also disable it for the tests whose whole
# subject it is.
@pytest.fixture(autouse=True)
def _viewer_auth_disabled(monkeypatch):
    monkeypatch.setenv("MAIL_UI_AUTH_ENABLED", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    yield
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()



def _get_git_head_sha(repo_path: Path) -> str | None:
    """Get the HEAD SHA from a git repository (synchronous helper)."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


async def _setup_archive_with_commits(settings: _config.Settings) -> dict:
    """Create test archive with commits for visualization tests."""
    await ensure_schema()

    # Create project in DB
    async with get_session() as session:
        from sqlalchemy import text

        await session.execute(
            text("INSERT INTO projects (slug, human_key, created_at) VALUES (:slug, :hk, datetime('now'))"),
            {"slug": "archive-test", "hk": "/tmp/archive-test"},
        )
        await session.commit()
        row = await session.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": "archive-test"})
        project_id = row.scalar()

    # Create archive with some commits
    archive = await ensure_archive(settings, "archive-test")

    # Write agent profile (creates a commit)
    await write_agent_profile(
        archive,
        {
            "name": "GreenCastle",
            "program": "claude-code",
            "model": "opus-4",
            "task_description": "Archive testing",
        },
    )

    # Write a message (creates another commit)
    await write_message_bundle(
        archive,
        message={"id": 1, "subject": "Archive Test Message", "created": "2026-01-12T12:00:00"},
        body_md="This is a test message for archive visualization.",
        sender="GreenCastle",
        recipients=["BlueLake"],
    )

    # Get the commit SHA from the archive
    head_sha = _get_git_head_sha(archive.root)

    return {
        "project_id": project_id,
        "project_slug": "archive-test",
        "archive_root": archive.root,
        "head_sha": head_sha,
    }


async def _setup_named_archive(
    settings: _config.Settings,
    *,
    slug: str,
    subject: str,
    agent_name: str,
) -> tuple[int, str | None]:
    """Create one named project archive and return its id and head commit."""
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO projects (slug, human_key, created_at) "
                "VALUES (:slug, :human_key, datetime('now')) RETURNING id"
            ),
            {"slug": slug, "human_key": f"/{slug}"},
        )
        project_id = int(result.scalar_one())
        await session.commit()
    archive = await ensure_archive(settings, slug)
    await write_agent_profile(
        archive,
        {
            "name": agent_name,
            "program": "test",
            "model": "test",
            "task_description": subject,
        },
    )
    await write_message_bundle(
        archive,
        message={"id": project_id, "subject": subject, "created": "2026-01-12T12:00:00"},
        body_md=subject,
        sender=agent_name,
        recipients=[agent_name],
    )
    return project_id, _get_git_head_sha(archive.root)


async def _rbac_cookie(username: str, project_id: int) -> dict[str, str]:
    """Create a member assigned as viewer to one archive project."""
    from mcp_agent_mail.models import UiProjectAssignment, UiUser

    async with get_session() as session:
        user = UiUser(
            username=username,
            password_hash=webauth.hash_password("irrelevant"),
            role=webauth.ROLE_MEMBER,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        assert user.id is not None
        session.add(
            UiProjectAssignment(
                user_id=int(user.id),
                project_id=project_id,
                role=webauth.PROJECT_ROLE_VIEWER,
            )
        )
        await session.commit()
        epoch = int(user.session_epoch)
        generation = user.session_generation
    return {
        "agent_mail_session": webauth.make_session(
            username,
            epoch=epoch,
            generation=generation,
            now=time.time(),
            secret=b"archive-rbac-session-secret-0123456789",
        )
    }


# =============================================================================
# Archive Guide Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_guide(isolated_env):
    """Test GET /mail/archive/guide returns guide page."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/guide")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_member_archive_routes_hide_unassigned_and_mixed_scope_commits(
    isolated_env,
    monkeypatch,
):
    """Archive pages cannot leak another project through alternate read routes."""
    monkeypatch.setenv("MAIL_UI_AUTH_ENABLED", "true")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", "archive-rbac-session-secret-0123456789")
    _config.clear_settings_cache()
    settings = _config.get_settings()
    visible_id, visible_sha = await _setup_named_archive(
        settings,
        slug="archive-visible",
        subject="VISIBLE-ARCHIVE-SUBJECT",
        agent_name="VisibleArchiveAgent",
    )
    _hidden_id, hidden_sha = await _setup_named_archive(
        settings,
        slug="archive-hidden",
        subject="HIDDEN-ARCHIVE-SUBJECT",
        agent_name="HiddenArchiveAgent",
    )
    visible_archive = await ensure_archive(settings, "archive-visible")
    await write_message_bundle(
        visible_archive,
        message={"id": 9001, "subject": "VISIBLE-ONLY-COMMIT", "created": "2026-01-12T13:00:00"},
        body_md="visible only",
        sender="VisibleArchiveAgent",
        recipients=["VisibleArchiveAgent"],
    )
    visible_sha = _get_git_head_sha(visible_archive.root)
    assert visible_sha is not None
    assert hidden_sha is not None
    cookies = await _rbac_cookie("archive-member", visible_id)
    root = Path(settings.storage.root).expanduser().resolve()
    visible_mixed = root / "projects" / "archive-visible" / "messages" / "mixed-scope.txt"
    hidden_mixed = root / "projects" / "archive-hidden" / "messages" / "mixed-scope.txt"
    visible_mixed.parent.mkdir(parents=True, exist_ok=True)
    hidden_mixed.parent.mkdir(parents=True, exist_ok=True)
    visible_mixed.write_text("visible", encoding="utf-8")
    hidden_mixed.write_text("hidden", encoding="utf-8")
    await asyncio.to_thread(
        subprocess.run,
        ["git", "add", visible_mixed.relative_to(root), hidden_mixed.relative_to(root)],
        cwd=root,
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        [
            "git",
            "commit",
            "-m",
            "mail: HiddenLeakedAgent -> VisibleArchiveAgent | MIXED-HIDDEN-METADATA",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    mixed_sha = _get_git_head_sha(root)
    hidden_rename_source = (
        root / "projects" / "archive-hidden" / "messages" / "hidden-rename-source.txt"
    )
    visible_rename_target = (
        root / "projects" / "archive-visible" / "messages" / "renamed-from-hidden.txt"
    )
    hidden_rename_source.write_text("HIDDEN-RENAME-SENTINEL", encoding="utf-8")
    await asyncio.to_thread(
        subprocess.run,
        ["git", "add", hidden_rename_source.relative_to(root)],
        cwd=root,
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        ["git", "commit", "-m", "HIDDEN-RENAME-SOURCE"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        [
            "git",
            "mv",
            str(hidden_rename_source.relative_to(root)),
            str(visible_rename_target.relative_to(root)),
        ],
        cwd=root,
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        ["git", "commit", "-m", "RENAME-HIDDEN-METADATA"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    rename_sha = _get_git_head_sha(root)
    app = build_http_app(settings, build_mcp_server())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
    ) as client:
        hidden_routes = [
            "/mail/archive/browser?project=archive-hidden",
            "/mail/archive/browser/archive-hidden/file?path=agents/HiddenArchiveAgent/profile.json",
            "/mail/archive/browser/archive-hidden/download?path=agents/HiddenArchiveAgent/profile.json",
            "/mail/archive/timeline?project=archive-hidden",
            "/mail/archive/network?project=archive-hidden",
            "/mail/archive/time-travel/snapshot?project=archive-hidden&agent=HiddenArchiveAgent&timestamp=2026-01-12T12:00",
        ]
        hidden_responses = [await client.get(path) for path in hidden_routes]
        hidden_commit = await client.get(f"/mail/archive/commit/{hidden_sha}")
        mixed_commit = await client.get(f"/mail/archive/commit/{mixed_sha}")
        rename_commit = await client.get(f"/mail/archive/commit/{rename_sha}")
        visible_commit = await client.get(f"/mail/archive/commit/{visible_sha}")
        activity = await client.get("/mail/archive/activity")
        guide = await client.get("/mail/archive/guide")
        time_travel = await client.get("/mail/archive/time-travel")
        visible_timeline = await client.get(
            "/mail/archive/timeline?project=archive-visible"
        )
        visible_network = await client.get(
            "/mail/archive/network?project=archive-visible"
        )

    assert all(response.status_code == 404 for response in hidden_responses)
    assert hidden_commit.status_code == 404
    assert mixed_commit.status_code == 404
    assert rename_commit.status_code == 404
    assert "HIDDEN-RENAME-SENTINEL" not in rename_commit.text
    visible_paths_result = await asyncio.to_thread(
        subprocess.run,
        ["git", "show", "--name-only", "--format=", visible_sha],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    visible_paths = visible_paths_result.stdout.splitlines()
    visible_detail = await get_commit_detail(visible_archive.repo, visible_sha)
    detail_paths = [item["path"] for item in visible_detail["files_changed"]]
    assert visible_commit.status_code == 200, {"git": visible_paths, "detail": detail_paths}
    for response in (activity, guide, time_travel, visible_timeline, visible_network):
        assert response.status_code == 200
        assert "archive-hidden" not in response.text
        assert "HIDDEN-ARCHIVE-SUBJECT" not in response.text
        assert "MIXED-HIDDEN-METADATA" not in response.text
        assert "HiddenLeakedAgent" not in response.text
        assert "RENAME-HIDDEN-METADATA" not in response.text
        assert "HIDDEN-RENAME-SENTINEL" not in response.text


@pytest.mark.asyncio
async def test_member_archive_root_commit_uses_empty_tree_for_scope(
    isolated_env,
    monkeypatch,
):
    """A mixed root commit cannot be scoped against the current working tree."""
    monkeypatch.setenv("MAIL_UI_AUTH_ENABLED", "true")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", "archive-rbac-session-secret-0123456789")
    _config.clear_settings_cache()
    settings = _config.get_settings()
    await ensure_schema()
    async with get_session() as session:
        visible_result = await session.execute(
            text(
                "INSERT INTO projects (slug, human_key, created_at) "
                "VALUES ('root-visible', '/root-visible', datetime('now')) RETURNING id"
            )
        )
        visible_id = int(visible_result.scalar_one())
        await session.execute(
            text(
                "INSERT INTO projects (slug, human_key, created_at) "
                "VALUES ('root-hidden', '/root-hidden', datetime('now'))"
            )
        )
        await session.commit()

    root = Path(settings.storage.root).expanduser().resolve()
    visible_file = root / "projects" / "root-visible" / "messages" / "visible.txt"
    hidden_file = root / "projects" / "root-hidden" / "messages" / "hidden.txt"
    visible_file.parent.mkdir(parents=True, exist_ok=True)
    hidden_file.parent.mkdir(parents=True, exist_ok=True)
    visible_file.write_text("visible root content", encoding="utf-8")
    hidden_file.write_text("HIDDEN-ROOT-SENTINEL", encoding="utf-8")
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "archive-test@example.invalid"],
        ["git", "config", "user.name", "Archive Test"],
        ["git", "add", "projects"],
        [
            "git",
            "commit",
            "-m",
            "mail: HiddenRootAgent -> VisibleRootAgent | ROOT-MIXED-HIDDEN-METADATA",
        ],
    ):
        await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    root_sha = _get_git_head_sha(root)
    assert root_sha is not None
    later_file = root / "projects" / "root-visible" / "messages" / "later.txt"
    later_file.write_text("visible later content", encoding="utf-8")
    await asyncio.to_thread(
        subprocess.run,
        ["git", "add", later_file.relative_to(root)],
        cwd=root,
        check=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        ["git", "commit", "-m", "VISIBLE-LATER"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    cookies = await _rbac_cookie("root-archive-member", visible_id)
    app = build_http_app(settings, build_mcp_server())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
    ) as client:
        detail = await client.get(f"/mail/archive/commit/{root_sha}")
        activity = await client.get("/mail/archive/activity")
        timeline = await client.get("/mail/archive/timeline?project=root-visible")
        network = await client.get("/mail/archive/network?project=root-visible")

    assert detail.status_code == 404
    assert "HIDDEN-ROOT-SENTINEL" not in detail.text
    for response in (activity, timeline, network):
        assert response.status_code == 200
        assert "ROOT-MIXED-HIDDEN-METADATA" not in response.text
        assert "HIDDEN-ROOT-SENTINEL" not in response.text
        assert "HiddenRootAgent" not in response.text


# =============================================================================
# Activity/Commits View Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_activity(isolated_env):
    """Test GET /mail/archive/activity returns recent commits."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/activity")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_archive_activity_with_limit(isolated_env):
    """Test activity view with limit parameter."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/activity", params={"limit": 5})
        assert resp.status_code == 200


# =============================================================================
# Commit Detail Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_commit_detail(isolated_env):
    """Test GET /mail/archive/commit/{sha} returns commit detail."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    data = await _setup_archive_with_commits(settings)

    if not data["head_sha"]:
        pytest.skip("No commits in archive")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/mail/archive/commit/{data['head_sha']}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_archive_commit_short_sha(isolated_env):
    """Test commit detail with short SHA."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    data = await _setup_archive_with_commits(settings)

    if not data["head_sha"]:
        pytest.skip("No commits in archive")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        short_sha = data["head_sha"][:7]
        resp = await client.get(f"/mail/archive/commit/{short_sha}")
        # Should work with short SHA
        assert resp.status_code in (200, 404)


@pytest.mark.asyncio
async def test_archive_commit_invalid_sha(isolated_env):
    """Test commit detail with invalid SHA."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/commit/invalidsha123")
        # Should return 404 or error page
        assert resp.status_code in (200, 404, 500)


@pytest.mark.asyncio
async def test_archive_commit_nonexistent_sha(isolated_env):
    """Test commit detail with nonexistent but valid-format SHA."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Valid format but doesn't exist
        resp = await client.get("/mail/archive/commit/0000000000000000000000000000000000000000")
        assert resp.status_code in (200, 404)


# =============================================================================
# Timeline Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_timeline(isolated_env):
    """Test GET /mail/archive/timeline returns timeline visualization."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/timeline")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# =============================================================================
# Browser (Directory Tree) Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_browser(isolated_env):
    """Test GET /mail/archive/browser returns directory browser."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/browser")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_archive_browser_file_content(isolated_env):
    """Test GET /mail/archive/browser/{project}/file returns file content."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Try to get agents/GreenCastle/profile.json
        resp = await client.get(
            "/mail/archive/browser/archive-test/file",
            params={"path": "agents/GreenCastle/profile.json"},
        )
        # Should return content or 404 if not found
        assert resp.status_code in (200, 404)


@pytest.mark.asyncio
async def test_archive_browser_file_nonexistent(isolated_env):
    """Test file browser with nonexistent file."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/browser/archive-test/file",
            params={"path": "nonexistent/file.txt"},
        )
        assert resp.status_code in (200, 404)


@pytest.mark.asyncio
async def test_archive_browser_file_missing_project_preserves_detail(isolated_env):
    """Missing archive projects should return the specific project-not-found error."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/browser/ghost-project/file",
            params={"path": "agents/BlueLake/profile.json"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project archive not found"


@pytest.mark.asyncio
async def test_archive_read_routes_do_not_create_missing_project_archives(isolated_env):
    """Read-only archive routes must not create projects on disk for nonexistent slugs."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    missing_project_dir = Path(settings.storage.root) / "projects" / "ghost-project"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        browser_resp = await client.get("/mail/archive/browser", params={"project": "ghost-project"})
        file_resp = await client.get(
            "/mail/archive/browser/ghost-project/file",
            params={"path": "agents/BlueLake/profile.json"},
        )
        snapshot_resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "ghost-project",
                "agent": "BlueLake",
                "timestamp": "2099-12-31T23:59:59Z",
            },
        )

    assert browser_resp.status_code == 200
    assert file_resp.status_code == 404
    assert snapshot_resp.status_code == 200
    assert missing_project_dir.exists() is False


@pytest.mark.asyncio
async def test_archive_browser_directory_path_traversal_returns_error_page(isolated_env):
    """Directory browser traversal attempts should render an error page, not raise 500."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/browser",
            params={"project": "archive-test", "path": "../../../etc"},
        )

    assert resp.status_code == 200
    assert "Invalid archive path" in resp.text


@pytest.mark.asyncio
async def test_archive_browser_path_traversal_prevention(isolated_env):
    """Test that path traversal attempts are blocked."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Try path traversal
        resp = await client.get(
            "/mail/archive/browser/archive-test/file",
            params={"path": "../../../etc/passwd"},
        )
        # Should not expose system files - must be error status or empty content
        assert resp.status_code in (200, 400, 403, 404)
        # Even if 200, should not contain password file content
        assert "root:" not in resp.text


# =============================================================================
# Network Graph Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_network(isolated_env):
    """Test GET /mail/archive/network returns agent communication graph."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/network")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_archive_network_empty(isolated_env):
    """Test network graph with no messages."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/network")
        # Should handle empty state gracefully
        assert resp.status_code == 200


# =============================================================================
# Time Travel Tests
# =============================================================================


@pytest.mark.asyncio
async def test_archive_time_travel_page(isolated_env):
    """Test GET /mail/archive/time-travel returns time travel interface."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/archive/time-travel")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_archive_time_travel_snapshot(isolated_env):
    """Test GET /mail/archive/time-travel/snapshot returns historical inbox."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Get snapshot at current time
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "archive-test",
                "agent": "GreenCastle",
                "timestamp": "2099-12-31T23:59:59Z",
            },
        )
        # Should return JSON snapshot
        assert resp.status_code in (200, 404)


@pytest.mark.asyncio
async def test_archive_time_travel_snapshot_invalid_timestamp(isolated_env):
    """Test time travel with invalid timestamp."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "archive-test",
                "agent": "GreenCastle",
                "timestamp": "not-a-timestamp",
            },
        )
        # Should handle invalid timestamp gracefully
        assert resp.status_code in (200, 400, 422)


@pytest.mark.asyncio
async def test_archive_time_travel_snapshot_past_timestamp(isolated_env):
    """Test time travel with timestamp before any commits."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Very old timestamp
        resp = await client.get(
            "/mail/archive/time-travel/snapshot",
            params={
                "project": "archive-test",
                "agent": "GreenCastle",
                "timestamp": "2000-01-01T00:00:00Z",
            },
        )
        # Should return empty or appropriate response
        assert resp.status_code in (200, 404)


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================


@pytest.mark.asyncio
async def test_archive_routes_no_projects(isolated_env):
    """Test archive routes when no projects exist."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # All routes should handle empty state gracefully
        routes = [
            "/mail/archive/guide",
            "/mail/archive/activity",
            "/mail/archive/timeline",
            "/mail/archive/browser",
            "/mail/archive/network",
            "/mail/archive/time-travel",
        ]
        for route in routes:
            resp = await client.get(route)
            assert resp.status_code in (200, 404), f"Route {route} failed with {resp.status_code}"


@pytest.mark.asyncio
async def test_archive_commit_xss_in_sha(isolated_env):
    """Test that XSS in SHA parameter is escaped."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_archive_with_commits(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Keep the payload in one path segment so this exercises the commit
        # route, rather than falling through to the platform's static-file path
        # parser because of the slash in a closing ``</script>`` tag.
        xss = "<img src=x onerror=alert('xss')>"
        resp = await client.get(f"/mail/archive/commit/{xss}")
        # Should not execute script
        assert resp.status_code in (200, 400, 404)
        # Regardless of status, should never reflect raw script tag
        assert xss not in resp.text
