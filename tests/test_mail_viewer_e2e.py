"""End-to-end tests for the HTTP mail viewer routes.

Tests all /mail/* endpoints to ensure proper rendering and functionality.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_immediate_session, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.storage import ensure_archive, write_agent_profile


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



async def _setup_test_data(
    settings: _config.Settings,
    *,
    include_cross_project_sender: bool = False,
) -> dict:
    """Create test project, agent, and messages for viewer tests."""
    await ensure_schema()

    # Create project
    async with get_session() as session:
        from sqlalchemy import text

        await session.execute(
            text("INSERT INTO projects (slug, human_key, created_at) VALUES (:slug, :hk, datetime('now'))"),
            {"slug": "test-proj", "hk": "/tmp/test-proj"},
        )
        await session.commit()
        row = await session.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": "test-proj"})
        project_id = row.scalar()

        # Create agent
        await session.execute(
            text(
                "INSERT INTO agents (name, project_id, program, model, task_description, inception_ts, last_active_ts, attachments_policy, contact_policy) "
                "VALUES (:name, :pid, :prog, :model, :task, datetime('now'), datetime('now'), 'auto', 'auto')"
            ),
            {"name": "BlueLake", "pid": project_id, "prog": "claude-code", "model": "opus-4", "task": "Testing"},
        )
        await session.commit()
        row = await session.execute(text("SELECT id FROM agents WHERE name = :name"), {"name": "BlueLake"})
        agent_id = row.scalar()

        # Create messages
        await session.execute(
            text(
                "INSERT INTO messages (project_id, subject, body_md, importance, ack_required, sender_id, thread_id, created_ts) "
                "VALUES (:pid, :subj, :body, :imp, :ack, :sid, :tid, datetime('now'))"
            ),
            {
                "pid": project_id,
                "subj": "Test Message 1",
                "body": "This is a test message body.",
                "imp": "normal",
                "ack": 0,
                "sid": agent_id,
                "tid": "thread-1",
            },
        )
        await session.execute(
            text(
                "INSERT INTO messages (project_id, subject, body_md, importance, ack_required, sender_id, thread_id, created_ts) "
                "VALUES (:pid, :subj, :body, :imp, :ack, :sid, :tid, datetime('now'))"
            ),
            {
                "pid": project_id,
                "subj": "Urgent Alert",
                "body": "This is an urgent message.",
                "imp": "urgent",
                "ack": 1,
                "sid": agent_id,
                "tid": "thread-2",
            },
        )
        await session.commit()

        # Get message IDs
        row = await session.execute(text("SELECT id FROM messages ORDER BY id"))
        message_ids = [r[0] for r in row.fetchall()]

        # Create recipient entries
        for mid in message_ids:
            await session.execute(
                text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (:mid, :aid, :kind)"),
                {"mid": mid, "aid": agent_id, "kind": "to"},
            )
        await session.commit()

        cross_project_message_id = None
        if include_cross_project_sender:
            await session.execute(
                text("INSERT INTO projects (slug, human_key, created_at) VALUES (:slug, :hk, datetime('now'))"),
                {"slug": "source-proj", "hk": "/tmp/source-proj"},
            )
            await session.commit()
            row = await session.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": "source-proj"})
            source_project_id = row.scalar()

            await session.execute(
                text(
                    "INSERT INTO agents (name, project_id, program, model, task_description, inception_ts, last_active_ts, attachments_policy, contact_policy) "
                    "VALUES (:name, :pid, :prog, :model, :task, datetime('now'), datetime('now'), 'auto', 'auto')"
                ),
                {
                    "name": "BlueLake",
                    "pid": source_project_id,
                    "prog": "claude-code",
                    "model": "opus-4",
                    "task": "Cross-project testing",
                },
            )
            await session.commit()
            row = await session.execute(
                text(
                    "SELECT id FROM agents WHERE project_id = :pid AND name = :name"
                ),
                {"pid": source_project_id, "name": "BlueLake"},
            )
            external_agent_id = row.scalar()

            await session.execute(
                text(
                    "INSERT INTO messages (project_id, subject, body_md, importance, ack_required, sender_id, thread_id, created_ts) "
                    "VALUES (:pid, :subj, :body, :imp, :ack, :sid, :tid, datetime('now'))"
                ),
                {
                    "pid": project_id,
                    "subj": "Cross Project Notice",
                    "body": "Sent from another project.",
                    "imp": "high",
                    "ack": 0,
                    "sid": external_agent_id,
                    "tid": "thread-cross",
                },
            )
            await session.commit()
            row = await session.execute(
                text("SELECT id FROM messages WHERE project_id = :pid AND subject = :subj"),
                {"pid": project_id, "subj": "Cross Project Notice"},
            )
            cross_project_message_id = row.scalar()
            await session.execute(
                text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (:mid, :aid, :kind)"),
                {"mid": cross_project_message_id, "aid": agent_id, "kind": "to"},
            )
            await session.commit()

    # Also create archive artifacts
    archive = await ensure_archive(settings, "test-proj")
    await write_agent_profile(
        archive,
        {
            "name": "BlueLake",
            "program": "claude-code",
            "model": "opus-4",
            "task_description": "Testing",
        },
    )

    return {
        "project_id": project_id,
        "project_slug": "test-proj",
        "agent_id": agent_id,
        "agent_name": "BlueLake",
        "message_ids": message_ids,
        "cross_project_message_id": cross_project_message_id,
    }


# =============================================================================
# Unified Inbox Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_unified_inbox_html(isolated_env):
    """Test GET /mail returns HTML unified inbox."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    data = await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        # Should contain some HTML structure
        assert "<html" in resp.text.lower() or "<!doctype" in resp.text.lower()
        assert f"totalMessages: {len(data['message_ids'])}" in resp.text
        assert '<span class="sm:hidden">Auto</span>' in resp.text
        assert 'class="flex min-w-0 flex-1 items-center gap-4 lg:gap-8"' in resp.text
        assert 'class="flex shrink-0 items-center gap-1 sm:gap-2 lg:gap-3"' in resp.text
        assert "min-h-[44px] min-w-[44px]" in resp.text


@pytest.mark.asyncio
async def test_mail_unified_inbox_api(isolated_env):
    """Test GET /mail/api/unified-inbox returns JSON."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/api/unified-inbox")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["messages"]) == 2
        assert data["total_messages"] == 2
        assert data["returned_messages"] == 2
        assert data["has_more"] is False


@pytest.mark.asyncio
async def test_mail_unified_inbox_api_reports_total_independent_of_page_limit(isolated_env):
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    data = await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/api/unified-inbox?limit=1")

    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["messages"]) == 1
    assert payload["total_messages"] == len(data["message_ids"])
    assert payload["returned_messages"] == 1
    assert payload["has_more"] is True


@pytest.mark.asyncio
async def test_mail_unified_inbox_api_disambiguates_external_sender(isolated_env):
    """Unified inbox JSON should preserve external sender origin."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    data = await _setup_test_data(settings, include_cross_project_sender=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/api/unified-inbox")
        assert resp.status_code == 200
        payload = resp.json()
        message = next(item for item in payload["messages"] if item["id"] == data["cross_project_message_id"])
        assert message["sender"] == "BlueLake@source-proj"
        assert message["sender_project"] == "/tmp/source-proj"
        assert message["sender_address"] == "project:source-proj#BlueLake"


@pytest.mark.asyncio
async def test_mail_unified_inbox_alternate_route(isolated_env):
    """Test GET /mail/unified-inbox alternate route."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/unified-inbox")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# =============================================================================
# Projects List Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_projects_list(isolated_env):
    """Test GET /mail/projects returns project listing."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/projects")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        # Should mention the test project
        assert "test-proj" in resp.text or "test" in resp.text.lower()


# =============================================================================
# Project View Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_project_view(isolated_env):
    """Test GET /mail/{project} returns project view."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "No matching messages" not in resp.text
        assert "Agents accepting mail" in resp.text
        assert "accepting mail" in resp.text
        assert "Last recorded activity:" in resp.text
        assert "Active collaborators" not in resp.text
        assert 'class="hidden min-w-0 items-center gap-2 text-sm lg:flex"' in resp.text
        assert "[overflow-wrap:anywhere]" in resp.text
        assert 'class="flex items-center gap-1 xl:gap-3"' in resp.text
        assert 'class="flex items-center gap-1 xl:gap-2"' in resp.text
        assert '<span class="hidden xl:inline text-sm">Human Overseer</span>' in resp.text


@pytest.mark.asyncio
async def test_mail_project_view_with_search(isolated_env):
    """Test GET /mail/{project}?q=search returns filtered results."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj", params={"q": "definitely-absent-message-token"})
        assert resp.status_code == 200
        assert "No matching messages" in resp.text


@pytest.mark.asyncio
async def test_mail_project_view_nonexistent(isolated_env):
    """Test GET /mail/{project} with nonexistent project."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/nonexistent-project")
        # Should return 404 or show empty page
        assert resp.status_code in (200, 404)


# =============================================================================
# Agent Inbox Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_agent_inbox(isolated_env):
    """Test GET /mail/{project}/inbox/{agent} returns agent inbox."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/inbox/BlueLake")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        # Should show messages
        assert "Test Message" in resp.text or "message" in resp.text.lower()


@pytest.mark.asyncio
async def test_mail_agent_inbox_pagination(isolated_env):
    """Test inbox pagination with page parameter."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/inbox/BlueLake", params={"page": 1, "limit": 10})
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_mail_agent_inbox_nonexistent_agent(isolated_env):
    """Test inbox for nonexistent agent."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/inbox/NonexistentAgent")
        # Should return 404 or empty inbox
        assert resp.status_code in (200, 404)


# =============================================================================
# Message Detail Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_message_detail(isolated_env):
    """Test GET /mail/{project}/message/{mid} returns message detail."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    data = await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mid = data["message_ids"][0]
        resp = await client.get(f"/mail/test-proj/message/{mid}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        # Should show the message subject
        assert "Test Message" in resp.text or "message" in resp.text.lower()
        assert "<time datetime=" in resp.text


@pytest.mark.asyncio
async def test_mail_message_and_thread_views_disambiguate_external_sender(isolated_env):
    """Dedicated HTML views should show the external sender's origin."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    data = await _setup_test_data(settings, include_cross_project_sender=True)
    cross_message_id = data["cross_project_message_id"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        message_resp = await client.get(f"/mail/test-proj/message/{cross_message_id}")
        assert message_resp.status_code == 200
        assert "BlueLake@source-proj" in message_resp.text

        thread_resp = await client.get("/mail/test-proj/thread/thread-cross")
        assert thread_resp.status_code == 200
        assert "BlueLake@source-proj" in thread_resp.text

        search_resp = await client.get("/mail/test-proj/search", params={"q": "Cross Project"})
        assert search_resp.status_code == 200
        assert "BlueLake@source-proj" in search_resp.text


@pytest.mark.asyncio
async def test_mail_message_detail_nonexistent(isolated_env):
    """Test message detail for nonexistent message ID."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/message/99999")
        # Server may return 200 with "not found" HTML page or 404
        assert resp.status_code in (200, 404)


# =============================================================================
# Mark Read Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_mark_read_single(isolated_env):
    """Test POST /mail/{project}/inbox/{agent}/mark-read marks message as read."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    data = await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mid = data["message_ids"][0]
        # Server expects JSON body
        resp = await client.post(
            "/mail/test-proj/inbox/BlueLake/mark-read",
            json={"message_ids": [mid]},
        )
        # Should redirect or return success
        assert resp.status_code in (200, 302, 303)


@pytest.mark.asyncio
async def test_mail_mark_all_read(isolated_env):
    """Test POST /mail/{project}/inbox/{agent}/mark-all-read marks all as read."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mail/test-proj/inbox/BlueLake/mark-all-read")
        # Should redirect or return success
        assert resp.status_code in (200, 302, 303)


# =============================================================================
# Thread View Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_thread_view(isolated_env):
    """Test GET /mail/{project}/thread/{thread_id} returns thread view."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/thread/thread-1")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert 'class="hidden truncate text-slate-600' in resp.text
        assert "dark:hover:text-primary-400 lg:inline" in resp.text


@pytest.mark.asyncio
async def test_mail_thread_view_nonexistent(isolated_env):
    """Test thread view for nonexistent thread."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/thread/nonexistent-thread")
        # Should return 200 with empty or 404
        assert resp.status_code in (200, 404)


# =============================================================================
# Search Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_search_page(isolated_env):
    """Test GET /mail/{project}/search returns search interface."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Search route may require a query parameter
        resp = await client.get("/mail/test-proj/search", params={"q": ""})
        # Accept 200 (success) or 422 (validation) if route requires non-empty query
        assert resp.status_code in (200, 422)


@pytest.mark.asyncio
async def test_mail_search_with_query(isolated_env):
    """Test search with query parameter."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/search", params={"q": "urgent"})
        assert resp.status_code == 200


# =============================================================================
# File Reservations View Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_file_reservations_view(isolated_env):
    """Test GET /mail/{project}/file_reservations returns reservations view."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/file_reservations")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# =============================================================================
# Attachments View Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_attachments_view(isolated_env):
    """Test GET /mail/{project}/attachments returns attachments browser."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/attachments")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# =============================================================================
# Overseer (Human Sender) Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_overseer_compose(isolated_env):
    """Test GET /mail/{project}/overseer/compose returns compose form."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/overseer/compose")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        # Should have a form
        assert "<form" in resp.text.lower() or "form" in resp.text.lower()


@pytest.mark.asyncio
async def test_mail_overseer_compose_unknown_project_is_not_found(isolated_env):
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/not-a-project/overseer/compose")

    assert resp.status_code == 404
    assert "Project not found" in resp.text


@pytest.mark.asyncio
async def test_mail_overseer_excludes_retired_agents_from_compose(isolated_env):
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    await _setup_test_data(settings)

    async with get_session() as session:
        from sqlalchemy import text

        await session.execute(
            text("UPDATE agents SET retired_at = datetime('now') WHERE name = :name"),
            {"name": "BlueLake"},
        )
        project_id = int(
            (await session.execute(text("SELECT id FROM projects WHERE slug = 'test-proj'"))).scalar_one()
        )
        await session.execute(
            text(
                "INSERT INTO agents (name, project_id, program, model, task_description, inception_ts, "
                "last_active_ts, attachments_policy, contact_policy) "
                "VALUES ('GreenRiver', :pid, 'codex', 'gpt-5', 'Testing', datetime('now'), "
                "datetime('now'), 'auto', 'auto')"
            ),
            {"pid": project_id},
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/overseer/compose")

    assert resp.status_code == 200
    assert 'value="BlueLake"' not in resp.text
    assert 'value="GreenRiver"' in resp.text


@pytest.mark.asyncio
async def test_mail_overseer_local_reply_is_server_derived(isolated_env):
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    data = await _setup_test_data(settings)
    original_id = int(data["message_ids"][0])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        compose = await client.get(
            "/mail/test-proj/overseer/compose",
            params={"reply_to": original_id},
        )
        sent = await client.post(
            "/mail/test-proj/overseer/send",
            json={
                "reply_to": original_id,
                "recipients": ["UntrustedOverride"],
                "subject": "Untrusted override",
                "body_md": "Trusted reply body.",
                "thread_id": "untrusted-thread",
            },
        )

    assert compose.status_code == 200
    assert f"replyTo: {original_id}" in compose.text
    assert 'selectedRecipients: ["BlueLake"]' in compose.text
    assert "Routing is locked to the original sender or recipients" in compose.text
    assert compose.text.count('readonly aria-readonly="true"') == 2
    assert "Array.isArray(result.recipients)" in compose.text
    assert sent.status_code == 200
    assert sent.json()["recipients"] == ["BlueLake"]
    async with get_session() as session:
        from sqlalchemy import text

        row = (
            await session.execute(
                text("SELECT subject, thread_id, reply_to FROM messages WHERE id = :mid"),
                {"mid": sent.json()["message_id"]},
            )
        ).one()
    assert tuple(row) == ("Re: Test Message 1", "thread-1", original_id)


@pytest.mark.asyncio
async def test_mail_overseer_reply_subject_stays_within_composer_limit(isolated_env):
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    data = await _setup_test_data(settings)
    original_id = int(data["message_ids"][0])
    original_subject = "S" * 200

    async with get_session() as session:
        from sqlalchemy import text

        await session.execute(
            text("UPDATE messages SET subject = :subject WHERE id = :mid"),
            {"subject": original_subject, "mid": original_id},
        )
        await session.commit()

    expected_subject = f"Re: {original_subject}"[:200]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        compose = await client.get(
            "/mail/test-proj/overseer/compose",
            params={"reply_to": original_id},
        )
        sent = await client.post(
            "/mail/test-proj/overseer/send",
            json={
                "reply_to": original_id,
                "recipients": {"ignored": True},
                "subject": "ignored" * 100,
                "thread_id": 123,
                "body_md": "Reply body.",
            },
        )

    assert compose.status_code == 200
    assert f'subject: "{expected_subject}"' in compose.text
    assert sent.status_code == 200
    async with get_session() as session:
        from sqlalchemy import text

        stored_subject = (
            await session.execute(
                text("SELECT subject FROM messages WHERE id = :mid"),
                {"mid": sent.json()["message_id"]},
            )
        ).scalar_one()
    assert stored_subject == expected_subject
    assert len(stored_subject) == 200


@pytest.mark.asyncio
async def test_mail_overseer_follow_up_targets_original_recipients_not_itself(isolated_env):
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        outbound = await client.post(
            "/mail/test-proj/overseer/send",
            json={
                "recipients": ["BlueLake"],
                "subject": "Original operator request",
                "body_md": "First instruction.",
            },
        )
        assert outbound.status_code == 200
        original_id = int(outbound.json()["message_id"])

        compose = await client.get(
            "/mail/test-proj/overseer/compose",
            params={"reply_to": original_id},
        )
        follow_up = await client.post(
            "/mail/test-proj/overseer/send",
            json={
                "reply_to": original_id,
                "recipients": ["HumanOverseer"],
                "subject": "Untrusted self-loop",
                "body_md": "Second instruction.",
            },
        )

    assert compose.status_code == 200
    assert "Reply as Human Overseer" in compose.text
    assert 'selectedRecipients: ["BlueLake"]' in compose.text
    assert follow_up.status_code == 200
    assert follow_up.json()["recipients"] == ["BlueLake"]

    async with get_session() as session:
        from sqlalchemy import text

        row = (
            await session.execute(
                text(
                    "SELECT sender.name, recipient.name, m.subject, m.thread_id, m.reply_to "
                    "FROM messages m JOIN agents sender ON sender.id = m.sender_id "
                    "JOIN message_recipients mr ON mr.message_id = m.id "
                    "JOIN agents recipient ON recipient.id = mr.agent_id "
                    "WHERE m.id = :mid"
                ),
                {"mid": follow_up.json()["message_id"]},
            )
        ).one()
    assert tuple(row) == (
        "HumanOverseer",
        "BlueLake",
        "Re: Original operator request",
        str(original_id),
        original_id,
    )


@pytest.mark.asyncio
async def test_mail_overseer_reply_fails_closed_for_cross_project_sender_collision(isolated_env):
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    data = await _setup_test_data(settings, include_cross_project_sender=True)
    external_id = int(data["cross_project_message_id"])

    async with get_session() as session:
        from sqlalchemy import text

        before_count = int((await session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        compose = await client.get(
            "/mail/test-proj/overseer/compose",
            params={"reply_to": external_id},
        )
        sent = await client.post(
            "/mail/test-proj/overseer/send",
            json={
                "reply_to": external_id,
                "recipients": ["BlueLake"],
                "subject": "Unsafe local collision",
                "body_md": "Must not route locally.",
            },
        )

    assert compose.status_code == 409
    assert "Cannot reply to a cross-project sender" in compose.text
    assert sent.status_code == 409
    assert "cross-project sender" in sent.json()["detail"]
    async with get_session() as session:
        from sqlalchemy import text

        after_count = int((await session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one())
    assert after_count == before_count


@pytest.mark.asyncio
async def test_mail_overseer_reply_does_not_prefill_or_send_to_retired_original_sender(isolated_env):
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    data = await _setup_test_data(settings)
    original_id = int(data["message_ids"][0])

    async with get_session() as session:
        from sqlalchemy import text

        await session.execute(
            text("UPDATE agents SET retired_at = datetime('now') WHERE name = 'BlueLake'")
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        compose = await client.get(
            "/mail/test-proj/overseer/compose",
            params={"reply_to": original_id},
        )
        sent = await client.post(
            "/mail/test-proj/overseer/send",
            json={"reply_to": original_id, "body_md": "Must not send."},
        )

    assert compose.status_code == 409
    assert "Cannot reply to a retired sender" in compose.text
    assert sent.status_code == 409
    assert sent.json()["detail"] == "Cannot reply to a retired sender"


@pytest.mark.asyncio
async def test_mail_overseer_rejects_retired_recipient_without_writing_message(isolated_env):
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    await _setup_test_data(settings)

    async with get_session() as session:
        from sqlalchemy import text

        await session.execute(
            text("UPDATE agents SET retired_at = datetime('now') WHERE name = :name"),
            {"name": "BlueLake"},
        )
        project_id = int(
            (await session.execute(text("SELECT id FROM projects WHERE slug = 'test-proj'"))).scalar_one()
        )
        await session.execute(
            text(
                "INSERT INTO agents (name, project_id, program, model, task_description, inception_ts, "
                "last_active_ts, attachments_policy, contact_policy) "
                "VALUES (:name, :pid, 'codex', 'gpt-5', 'Testing', datetime('now'), datetime('now'), 'auto', 'auto')"
            ),
            {"name": "GreenRiver", "pid": project_id},
        )
        before_count = int((await session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one())
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mail/test-proj/overseer/send",
            json={
                "recipients": ["GreenRiver", "BlueLake"],
                "subject": "Must not be delivered",
                "body_md": "Retired identities are not addressable.",
            },
        )

    assert resp.status_code == 409
    assert "retired recipients: BlueLake" in resp.json()["detail"]
    async with get_session() as session:
        from sqlalchemy import text

        after_count = int((await session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one())
    assert after_count == before_count


@pytest.mark.asyncio
async def test_mail_overseer_retire_wins_concurrent_send_without_message_commit(
    isolated_env,
    monkeypatch,
):
    from mcp_agent_mail import http as http_module

    send_transaction_attempted = asyncio.Event()

    @contextlib.asynccontextmanager
    async def observed_send_session():
        send_transaction_attempted.set()
        async with get_immediate_session() as session:
            yield session

    monkeypatch.setattr(http_module, "get_immediate_session", observed_send_session)
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with get_immediate_session() as retire_session:
            from sqlalchemy import text

            before_count = int(
                (await retire_session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one()
            )
            await retire_session.execute(
                text("UPDATE agents SET retired_at = datetime('now') WHERE name = 'BlueLake'")
            )
            send_task = asyncio.create_task(
                client.post(
                    "/mail/test-proj/overseer/send",
                    json={
                        "recipients": ["BlueLake"],
                        "subject": "Concurrent send",
                        "body_md": "Retire already owns the write transaction.",
                    },
                )
            )
            await asyncio.wait_for(send_transaction_attempted.wait(), timeout=1)
            assert not send_task.done()
            await retire_session.commit()
        response = await send_task

    assert response.status_code == 409
    async with get_session() as session:
        from sqlalchemy import text

        after_count = int((await session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one())
    assert after_count == before_count


@pytest.mark.asyncio
async def test_mail_overseer_send(isolated_env):
    """Test POST /mail/{project}/overseer/send sends message."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Server expects JSON body
        resp = await client.post(
            "/mail/test-proj/overseer/send",
            json={
                "recipients": ["BlueLake"],
                "subject": "Test from Overseer",
                "body_md": "This is a test message from the human overseer.",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["recipients"] == ["BlueLake"]


@pytest.mark.asyncio
async def test_mail_overseer_send_missing_fields(isolated_env):
    """Test overseer send with missing required fields."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    invalid_payloads = [
        [],
        {"recipients": "BlueLake", "subject": "Subject", "body_md": "Body"},
        {"recipients": [1], "subject": "Subject", "body_md": "Body"},
        {"recipients": ["BlueLake"], "subject": 1, "body_md": "Body"},
        {"recipients": ["BlueLake"], "subject": "Subject", "body_md": []},
        {"recipients": ["BlueLake"], "subject": "Subject", "body_md": "Body", "thread_id": 1},
        {"recipients": ["BlueLake"], "subject": "Subject", "body_md": "Body", "reply_to": "1"},
        {"recipients": ["BlueLake"]},
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for payload in invalid_payloads:
            response = await client.post("/mail/test-proj/overseer/send", json=payload)
            assert response.status_code == 400, (payload, response.text)
        malformed = await client.post(
            "/mail/test-proj/overseer/send",
            content="{",
            headers={"content-type": "application/json"},
        )
        assert malformed.status_code == 400


# =============================================================================
# XSS Prevention Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_xss_in_search_query(isolated_env):
    """Test that XSS in search query is escaped."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        xss_payload = "<script>alert('xss')</script>"
        resp = await client.get("/mail/test-proj/search", params={"q": xss_payload})
        assert resp.status_code == 200
        # The raw script tag should not appear unescaped
        assert "<script>alert('xss')</script>" not in resp.text


@pytest.mark.asyncio
async def test_mail_xss_in_project_name(isolated_env):
    """Test that XSS in project name path is handled safely."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        xss_payload = "<img src=x onerror=alert('xss')>"
        resp = await client.get(f"/mail/{xss_payload}")
        # Should handle gracefully without executing script
        assert resp.status_code in (200, 404)
        # Regardless of status, should never reflect raw script tag
        assert xss_payload not in resp.text


# =============================================================================
# Lock Status API Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mail_api_locks_empty(isolated_env):
    """Test GET /mail/api/locks with no locks."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await ensure_schema()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/api/locks")
        assert resp.status_code == 200
        data = resp.json()
        assert "locks" in data
        assert isinstance(data["locks"], list)
