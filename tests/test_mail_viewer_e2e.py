"""End-to-end tests for the HTTP mail viewer routes.

Tests all /mail/* endpoints to ensure proper rendering and functionality.
"""

from __future__ import annotations

import base64
import contextlib
import time
from html.parser import HTMLParser

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config, webauth
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.storage import ensure_archive, write_agent_profile

MAIL_UI_TEST_SECRET = "mail-viewer-rbac-session-secret-0123456789"
OVERSEER_UNAVAILABLE_DETAIL = (
    "Human Overseer messaging is temporarily unavailable while atomic archive persistence is implemented"
)


class _NestedAnchorDetector(HTMLParser):
    """Detect invalid nested anchors in rendered template source."""

    def __init__(self) -> None:
        super().__init__()
        self.anchor_depth = 0
        self.found_nested_anchor = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "a":
            self.found_nested_anchor |= self.anchor_depth > 0
            self.anchor_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.anchor_depth > 0:
            self.anchor_depth -= 1


class _ImageSourceCollector(HTMLParser):
    """Collect image sources from server-rendered markup."""

    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        for name, value in attrs:
            if name == "src" and value is not None:
                self.sources.append(value)


def _has_nested_anchor(html: str) -> bool:
    detector = _NestedAnchorDetector()
    detector.feed(html)
    return detector.found_nested_anchor


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


def _rbac_settings(monkeypatch) -> _config.Settings:
    monkeypatch.setenv("MAIL_UI_AUTH_ENABLED", "true")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", MAIL_UI_TEST_SECRET)
    monkeypatch.setenv("MAIL_UI_COOKIE_SECURE", "false")
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "")
    _config.clear_settings_cache()
    return _config.get_settings()


async def _ui_session_cookie(
    settings: _config.Settings,
    *,
    username: str,
    global_role: str,
    project_id: int | None = None,
    project_role: str | None = None,
) -> dict[str, str]:
    from mcp_agent_mail.models import UiProjectAssignment, UiUser

    async with get_session() as session:
        user = UiUser(
            username=username,
            password_hash=webauth.hash_password("not-used-by-this-session-test"),
            role=global_role,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        assert user.id is not None
        session_epoch = int(user.session_epoch)
        session_generation = user.session_generation
        if project_id is not None and project_role is not None:
            session.add(
                UiProjectAssignment(
                    user_id=int(user.id),
                    project_id=project_id,
                    role=project_role,
                )
            )
            await session.commit()

    token = webauth.make_session(
        username,
        epoch=session_epoch,
        generation=session_generation,
        now=time.time(),
        secret=MAIL_UI_TEST_SECRET.encode("utf-8"),
    )
    return {settings.mail_ui.cookie_name: token}


@pytest.mark.asyncio
async def test_mail_admin_render_keeps_all_existing_controls(isolated_env, monkeypatch):
    settings = _rbac_settings(monkeypatch)
    data = await _setup_test_data(settings)
    cookies = await _ui_session_cookie(
        settings,
        username="admin-render",
        global_role=webauth.ROLE_ADMIN,
    )
    app = build_http_app(settings, build_mcp_server())
    message_id = int(data["message_ids"][0])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
    ) as client:
        project = await client.get("/mail/test-proj")
        inbox = await client.get("/mail/test-proj/inbox/BlueLake")
        message = await client.get(f"/mail/test-proj/message/{message_id}")
        compose = await client.get("/mail/test-proj/overseer/compose")

    assert project.status_code == 200
    assert 'aria-label="Send high-priority message as Human Overseer"' in project.text
    assert 'aria-label="Retire agent"' in project.text
    assert "Archive Project" in project.text
    assert inbox.status_code == 200
    assert 'aria-label="Mark All Read"' in inbox.text
    assert '@change="toggleSelection(item.id)"' in inbox.text
    assert message.status_code == 200
    assert 'data-tippy-content="Reply to this message as the Human Overseer"' in message.text
    assert compose.status_code == 503
    assert compose.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL


@pytest.mark.asyncio
async def test_mail_viewer_render_is_project_scoped_and_read_only(isolated_env, monkeypatch):
    settings = _rbac_settings(monkeypatch)
    data = await _setup_test_data(settings)
    async with get_session() as session:
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO projects (slug, human_key, created_at) "
                "VALUES ('hidden-proj', '/tmp/hidden-proj', datetime('now'))"
            )
        )
        await session.commit()
    cookies = await _ui_session_cookie(
        settings,
        username="viewer-render",
        global_role=webauth.ROLE_MEMBER,
        project_id=int(data["project_id"]),
        project_role=webauth.PROJECT_ROLE_VIEWER,
    )
    app = build_http_app(settings, build_mcp_server())
    message_id = int(data["message_ids"][0])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
    ) as client:
        unified = await client.get("/mail")
        unified_api = await client.get("/mail/api/unified-inbox")
        project = await client.get("/mail/test-proj")
        hidden = await client.get("/mail/hidden-proj")
        inbox = await client.get("/mail/test-proj/inbox/BlueLake")
        message = await client.get(f"/mail/test-proj/message/{message_id}")
        thread = await client.get("/mail/test-proj/thread/thread-1")
        new_compose = await client.get("/mail/test-proj/overseer/compose")
        reply_compose = await client.get(
            "/mail/test-proj/overseer/compose",
            params={"reply_to": message_id},
        )
        archive = await client.get("/mail/archive/guide")

    assert unified.status_code == 200
    assert "test-proj" in unified.text
    assert "hidden-proj" not in unified.text
    assert unified_api.status_code == 200
    assert {item["can_reply"] for item in unified_api.json()["messages"]} == {False}
    assert hidden.status_code == 404
    assert project.status_code == 200
    assert 'aria-label="Send high-priority message as Human Overseer"' not in project.text
    assert 'aria-label="Retire agent"' not in project.text
    assert "Archive Project" not in project.text
    assert inbox.status_code == 200
    assert 'aria-label="Mark All Read"' not in inbox.text
    assert '@change="toggleSelection(item.id)"' not in inbox.text
    assert message.status_code == 200
    assert 'data-tippy-content="Reply to this message as the Human Overseer"' not in message.text
    assert thread.status_code == 200
    assert "/overseer/compose?reply_to=" not in thread.text
    assert new_compose.status_code == 403
    assert reply_compose.status_code == 403
    assert archive.status_code == 200
    assert "Repository Location" not in archive.text


@pytest.mark.asyncio
async def test_mail_operator_render_exposes_reply_only(isolated_env, monkeypatch):
    settings = _rbac_settings(monkeypatch)
    data = await _setup_test_data(settings)
    cookies = await _ui_session_cookie(
        settings,
        username="operator-render",
        global_role=webauth.ROLE_MEMBER,
        project_id=int(data["project_id"]),
        project_role=webauth.PROJECT_ROLE_OPERATOR,
    )
    app = build_http_app(settings, build_mcp_server())
    message_id = int(data["message_ids"][0])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
    ) as client:
        unified_api = await client.get("/mail/api/unified-inbox")
        project = await client.get("/mail/test-proj")
        message = await client.get(f"/mail/test-proj/message/{message_id}")
        thread = await client.get("/mail/test-proj/thread/thread-1")
        new_compose = await client.get("/mail/test-proj/overseer/compose")
        reply_compose = await client.get(
            "/mail/test-proj/overseer/compose",
            params={"reply_to": message_id},
        )
        csrf_headers = {
            "Origin": "http://test",
            "Referer": "http://test/mail/test-proj",
            "Host": "test",
        }
        forbidden_new_message = await client.post(
            "/mail/test-proj/overseer/send",
            json={
                "recipients": ["BlueLake"],
                "subject": "Operator must not compose",
                "body_md": "This request must be rejected.",
            },
            headers=csrf_headers,
        )
        sent_reply = await client.post(
            "/mail/test-proj/overseer/reply",
            json={
                "reply_to": message_id,
                "body_md": "Operator reply through the dedicated endpoint.",
            },
            headers=csrf_headers,
        )

    assert unified_api.status_code == 200
    assert {item["can_reply"] for item in unified_api.json()["messages"]} == {True}
    assert project.status_code == 200
    assert 'aria-label="Send high-priority message as Human Overseer"' not in project.text
    assert 'aria-label="Retire agent"' not in project.text
    assert "Archive Project" not in project.text
    assert message.status_code == 200
    assert 'data-tippy-content="Reply to this message as the Human Overseer"' in message.text
    assert thread.status_code == 200
    assert f"/overseer/compose?reply_to={message_id}" in thread.text
    assert new_compose.status_code == 403
    assert reply_compose.status_code == 503
    assert forbidden_new_message.status_code == 403
    assert sent_reply.status_code == 503
    assert sent_reply.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL


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
        assert 'class="flex min-h-[44px] min-w-[44px] shrink-0 items-center gap-3 group"' in resp.text
        assert 'id="global-header-controls" class="ml-auto flex shrink-0 items-center gap-1 sm:gap-2 lg:gap-3"' in resp.text
        assert "min-h-[44px] min-w-[44px]" in resp.text
        assert '<form action="/mail/logout" method="post" class="shrink-0">' in resp.text
        assert 'type="submit"' in resp.text
        assert 'aria-label="Sign out"' in resp.text
        assert ":aria-label=\"'Sort messages: ' +" in resp.text
        assert ':aria-expanded="sortOpen"' in resp.text
        assert '@keydown.space.prevent="handleMessageClick(msg)"' in resp.text
        assert "sticky top-28 lg:top-16" in resp.text
        assert (
            "fixed inset-x-0 top-[18.5rem] lg:top-56 xl:top-48 z-[60]"
            in resp.text
        )
        assert (
            "sticky top-[17rem] lg:top-48 z-10" in resp.text
        )
        assert "fixed inset-0 top-[18.5rem] lg:top-56 xl:top-48 z-50" in resp.text
        for field_id in (
            "unified-filter-project",
            "unified-filter-sender",
            "unified-filter-recipient",
            "unified-filter-importance",
            "unified-filter-thread",
        ):
            assert f'for="{field_id}"' in resp.text
            assert f'id="{field_id}"' in resp.text
        assert 'id="unified-search"' in resp.text
        assert 'name="query"' in resp.text
        assert 'id="project-filter"' in resp.text
        assert 'name="project_filter"' in resp.text
        assert 'x-data="unifiedInboxManager()" x-init="init()"' not in resp.text
        assert "destroy() {" in resp.text
        assert "this._keydownHandler = null;" in resp.text


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
    from sqlalchemy import text

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
        assert "min-h-[44px] min-w-[44px] p-2.5" in resp.text
        assert "flex min-h-[44px] flex-1 items-center justify-center gap-2 rounded-lg bg-gradient-to-r" in resp.text

        async with get_session() as session:
            await session.execute(
                text("UPDATE agents SET retired_at = datetime('now') WHERE name = 'BlueLake'")
            )
            await session.commit()

        retired = await client.get("/mail/test-proj")
        assert retired.status_code == 200
        assert 'aria-controls="retired-agent-list"' in retired.text
        assert ':aria-expanded="showRetired.toString()"' in retired.text
        assert 'id="retired-agent-list"' in retired.text
        assert "flex min-h-[44px] flex-1 items-center justify-center gap-2 rounded-lg bg-slate-200" in retired.text
        assert "min-h-[44px] min-w-[44px] p-2.5 bg-green-100" in retired.text


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

        results = await client.get("/mail/test-proj", params={"q": "Test Message"})
        assert results.status_code == 200
        assert "Found <span" in results.text
        assert "block overflow-hidden group" in results.text
        assert "flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2" in results.text


@pytest.mark.asyncio
async def test_mail_search_escapes_agent_html_before_restoring_fts_marks(
    isolated_env,
):
    """Search highlights must not turn agent-controlled HTML into active DOM."""
    from sqlalchemy import text

    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    data = await _setup_test_data(settings)

    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO messages "
                "(project_id, subject, body_md, importance, ack_required, "
                "sender_id, thread_id, created_ts) "
                "VALUES (:pid, :subject, :body, 'normal', 0, :sender_id, "
                ":thread_id, datetime('now'))"
            ),
            {
                "pid": data["project_id"],
                "subject": "Search safety",
                "body": '<img src=x onerror=alert(1)> needle <mark>literal</mark>',
                "sender_id": data["agent_id"],
                "thread_id": "search-xss",
            },
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [
            await client.get("/mail/test-proj", params={"q": "needle"}),
            await client.get("/mail/test-proj/search", params={"q": "needle"}),
        ]

    for response in responses:
        assert response.status_code == 200
        assert "<mark>needle</mark>" in response.text
        assert "<img src=x onerror=alert(1)>" not in response.text
        assert "&lt;img src=x onerror=alert(1)&gt;" in response.text


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
        assert "flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between" in resp.text
        assert "line-clamp-2 mb-2 break-words text-lg font-semibold [overflow-wrap:anywhere]" in resp.text
        assert "min-h-[44px] min-w-[44px]" in resp.text
        assert ":aria-label=\"'Open message: ' + item.subject\"" in resp.text
        assert not _has_nested_anchor(resp.text)
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
        assert "inline-flex min-h-[44px] items-center gap-2" in resp.text
        assert (
            "min-w-0 flex-1 text-2xl sm:text-3xl font-bold text-slate-900 "
            "dark:text-white break-words [overflow-wrap:anywhere]" in resp.text
        )
        assert (
            "prose prose-slate dark:prose-invert max-w-none min-w-0 break-words "
            "[overflow-wrap:anywhere]" in resp.text
        )


@pytest.mark.asyncio
async def test_mail_message_remote_images_are_not_active_resources(isolated_env):
    """Agent-authored Markdown cannot make a human browser call a remote pixel."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    data = await _setup_test_data(settings)
    message_id = data["message_ids"][0]
    inline_png = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\nrest"
    ).decode("ascii")

    async with get_session() as session:
        from sqlalchemy import text

        await session.execute(
            text("UPDATE messages SET body_md = :body WHERE id = :message_id"),
            {
                "message_id": message_id,
                "body": (
                    "![remote](https://tracker.example/pixel.png) "
                    "![protocol-relative](//tracker.example/pixel.png) "
                    "![backslash](/\\tracker.example/markdown.png) "
                    '<img src="/\\tracker.example/raw.png" alt="raw backslash"> '
                    '<img src="/&#9;/tracker.example/tab.png" alt="raw tab"> '
                    '<img src="/&#10;/tracker.example/lf.png" alt="raw newline"> '
                    '<img src="/&#13;/tracker.example/cr.png" alt="raw carriage return"> '
                    '<img src="/%5Ctracker.example/encoded.png" alt="encoded backslash"> '
                    "![local](/mail/static/local.png) "
                    "![logout](/mail/logout) "
                    f'![inline]({inline_png}) '
                    "[ordinary link](https://docs.example/read)"
                ),
            },
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/mail/test-proj/message/{message_id}")

    collector = _ImageSourceCollector()
    collector.feed(response.text)
    assert response.status_code == 200
    assert collector.sources == [inline_png]
    assert all("tracker.example" not in source for source in collector.sources)
    assert "img-src 'self' data: blob:" in response.headers["Content-Security-Policy"]
    assert "https://docs.example/read" in response.text


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
    """The legacy composer is unavailable and renders no active controls."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/test-proj/overseer/compose")
        assert resp.status_code == 503
        assert resp.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
        assert "<form" not in resp.text.lower()
        assert "sendEndpoint" not in resp.text


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
async def test_mail_overseer_compose_hold_does_not_expose_retired_agents(isolated_env):
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

    assert resp.status_code == 503
    assert resp.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    assert 'value="BlueLake"' not in resp.text
    assert 'value="GreenRiver"' not in resp.text


@pytest.mark.asyncio
async def test_mail_overseer_hold_precedes_reply_derivation(isolated_env):
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    data = await _setup_test_data(settings)
    original_id = int(data["message_ids"][0])
    async with get_session() as session:
        from sqlalchemy import text

        before_count = int((await session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        compose = await client.get(
            "/mail/test-proj/overseer/compose",
            params={"reply_to": original_id},
        )
        sent = await client.post(
            "/mail/test-proj/overseer/reply",
            json={
                "reply_to": original_id,
                "recipients": ["UntrustedOverride"],
                "subject": "Untrusted override",
                "body_md": "Trusted reply body.",
                "thread_id": "untrusted-thread",
            },
        )

    assert compose.status_code == 503
    assert compose.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    assert sent.status_code == 503
    assert sent.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    async with get_session() as session:
        from sqlalchemy import text

        after_count = int((await session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one())
    assert after_count == before_count


@pytest.mark.asyncio
async def test_mail_overseer_hold_does_not_mutate_original_subject(isolated_env):
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

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        compose = await client.get(
            "/mail/test-proj/overseer/compose",
            params={"reply_to": original_id},
        )
        sent = await client.post(
            "/mail/test-proj/overseer/reply",
            json={
                "reply_to": original_id,
                "recipients": {"ignored": True},
                "subject": "ignored" * 100,
                "thread_id": 123,
                "body_md": "Reply body.",
            },
        )

    assert compose.status_code == 503
    assert compose.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    assert sent.status_code == 503
    assert sent.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    async with get_session() as session:
        from sqlalchemy import text

        stored_subject = (
            await session.execute(
                text("SELECT subject FROM messages WHERE id = :mid"),
                {"mid": original_id},
            )
        ).scalar_one()
    assert stored_subject == original_subject
    assert len(stored_subject) == 200


@pytest.mark.asyncio
async def test_mail_overseer_hold_blocks_new_messages_and_follow_ups(isolated_env):
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    data = await _setup_test_data(settings)
    original_id = int(data["message_ids"][0])

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
        compose = await client.get(
            "/mail/test-proj/overseer/compose",
            params={"reply_to": original_id},
        )
        follow_up = await client.post(
            "/mail/test-proj/overseer/reply",
            json={
                "reply_to": original_id,
                "recipients": ["HumanOverseer"],
                "subject": "Untrusted self-loop",
                "body_md": "Second instruction.",
            },
        )

    assert outbound.status_code == 503
    assert outbound.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    assert compose.status_code == 503
    assert compose.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    assert follow_up.status_code == 503
    assert follow_up.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL


@pytest.mark.asyncio
async def test_mail_overseer_hold_precedes_cross_project_reply_resolution(isolated_env):
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
            "/mail/test-proj/overseer/reply",
            json={
                "reply_to": external_id,
                "recipients": ["BlueLake"],
                "subject": "Unsafe local collision",
                "body_md": "Must not route locally.",
            },
        )

    assert compose.status_code == 503
    assert compose.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    assert sent.status_code == 503
    assert sent.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    async with get_session() as session:
        from sqlalchemy import text

        after_count = int((await session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one())
    assert after_count == before_count


@pytest.mark.asyncio
async def test_mail_overseer_hold_precedes_retired_sender_resolution(isolated_env):
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
            "/mail/test-proj/overseer/reply",
            json={"reply_to": original_id, "body_md": "Must not send."},
        )

    assert compose.status_code == 503
    assert compose.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    assert sent.status_code == 503
    assert sent.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL


@pytest.mark.asyncio
async def test_mail_overseer_hold_blocks_retired_recipient_before_writing_message(isolated_env):
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

    assert resp.status_code == 503
    assert resp.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    async with get_session() as session:
        from sqlalchemy import text

        after_count = int((await session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one())
    assert after_count == before_count


@pytest.mark.asyncio
async def test_mail_overseer_hold_never_opens_immediate_write_transaction(
    isolated_env,
    monkeypatch,
):
    from mcp_agent_mail import http as http_module

    immediate_session_calls = 0

    def forbidden_immediate_session():
        nonlocal immediate_session_calls
        immediate_session_calls += 1
        raise AssertionError("legacy overseer hold must precede the write transaction")

    monkeypatch.setattr(http_module, "get_immediate_session", forbidden_immediate_session)
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    await _setup_test_data(settings)

    async with get_session() as session:
        from sqlalchemy import text

        before_count = int((await session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/mail/test-proj/overseer/send",
            json={
                "recipients": ["BlueLake"],
                "subject": "Blocked send",
                "body_md": "The write transaction must never open.",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
    assert immediate_session_calls == 0
    async with get_session() as session:
        from sqlalchemy import text

        after_count = int((await session.execute(text("SELECT COUNT(*) FROM messages"))).scalar_one())
    assert after_count == before_count


@pytest.mark.asyncio
async def test_mail_overseer_send(isolated_env):
    """The legacy send endpoint is held until archive writes are atomic."""
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    await _setup_test_data(settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mail/test-proj/overseer/send",
            json={
                "recipients": ["BlueLake"],
                "subject": "Test from Overseer",
                "body_md": "This is a test message from the human overseer.",
            },
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL


@pytest.mark.asyncio
async def test_mail_overseer_hold_precedes_request_body_parsing(isolated_env):
    """Even malformed bodies cannot enter the dormant legacy implementation."""
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
            assert response.status_code == 503, (payload, response.text)
            assert response.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL
        malformed = await client.post(
            "/mail/test-proj/overseer/send",
            content="{",
            headers={"content-type": "application/json"},
        )
        assert malformed.status_code == 503
        assert malformed.json()["detail"] == OVERSEER_UNAVAILABLE_DETAIL


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
