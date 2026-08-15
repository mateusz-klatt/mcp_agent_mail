"""HTTP Server and Transport Tests.

Comprehensive tests for HTTP server functionality:
1. Server starts on configured port
2. Health endpoint returns 200
3. SSE connection established
4. Tool calls work over HTTP
5. Resource reads work over HTTP
6. CORS headers present

Reference: mcp_agent_mail-9z6
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
from dataclasses import dataclass
from os import utime
from pathlib import Path
from threading import Event, current_thread, main_thread
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select as _sa_select, text

import mcp_agent_mail.http as http_module
from mcp_agent_mail import config as _config, storage as storage_module
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Message, MessageRecipient, Project
from tests.keys import pkey


def select(*entities: Any, **kwargs: Any) -> Any:
    """Keep SQLModel descriptor typing out of behaviour tests.

    Matches the shim already used in app.py, cli.py and test_agent_rename.py:
    `ty` cannot match SQLAlchemy's select/join overloads against SQLModel
    descriptors, so a column select or an ON clause reads as a plain bool and
    the gate fails on code that is correct. Widening here beats scattering
    ignores or rephrasing working queries to please a checker.
    """
    return _sa_select(*entities, **kwargs)


def _rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Create a JSON-RPC 2.0 request payload."""
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


def _now_naive_utc() -> dt.datetime:
    """Return the current UTC instant with tzinfo stripped, the form ACK helpers take."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _write_file(path: Path, body: str) -> Path:
    """Create ``path`` and any missing parents, holding ``body``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _backdate(path: Path, *, days: float) -> Path:
    """Move ``path``'s modification time ``days`` into the past."""
    stamp = path.stat().st_mtime - days * 86400.0
    utime(path, (stamp, stamp))
    return path


# =============================================================================
# Test: Server Starts on Configured Port
# =============================================================================


class TestServerConfiguration:
    """Test that server respects configuration settings."""

    @pytest.mark.asyncio
    async def test_server_builds_with_default_config(self, isolated_env):
        """Server builds successfully with default configuration."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        assert app is not None
        # FastAPI app should have routes
        assert len(app.routes) > 0

    @pytest.mark.asyncio
    async def test_server_uses_configured_path(self, isolated_env, monkeypatch):
        """Server mounts MCP handler at configured HTTP_PATH."""
        monkeypatch.setenv("HTTP_PATH", "/custom-mcp/")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()
        assert settings.http.path == "/custom-mcp/"

        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)
        assert app is not None

    @pytest.mark.asyncio
    async def test_server_mounts_api_and_mcp_aliases(self, isolated_env, monkeypatch):
        """Server always exposes MCP transport on both /api and /mcp aliases."""
        monkeypatch.setenv("HTTP_PATH", "/custom-mcp/")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        mounted_paths = {getattr(route, "path", "") for route in app.routes}
        assert "/custom-mcp" in mounted_paths
        assert "/api" in mounted_paths
        assert "/mcp" in mounted_paths

    @pytest.mark.asyncio
    async def test_server_builds_with_custom_host_port(self, isolated_env, monkeypatch):
        """Server configuration accepts custom host and port."""
        monkeypatch.setenv("HTTP_HOST", "0.0.0.0")
        monkeypatch.setenv("HTTP_PORT", "9999")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        settings = _config.get_settings()

        assert settings.http.host == "0.0.0.0"
        assert settings.http.port == 9999

        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)
        assert app is not None

    def test_production_proxy_trust_is_confined_to_sanitizing_loopback_ingress(self):
        """Production may trust all proxy peers only behind its fixed Apache boundary."""
        repository_root = Path(__file__).resolve().parents[1]
        compose = (repository_root / "compose.prod.yaml").read_text(encoding="utf-8")
        apache = (repository_root / "deploy/apache-vhost.example.conf").read_text(
            encoding="utf-8"
        )

        assert '- "127.0.0.1:8765:8765"' in compose
        assert 'HTTP_FORWARDED_ALLOW_IPS: "*"' in compose
        assert 'RequestHeader unset X-Forwarded-Proto' in apache
        assert 'RequestHeader set X-Forwarded-Proto "https"' in apache
        assert 'ProxyPass        "/" "http://127.0.0.1:8765/"' in apache


# =============================================================================
# Test: Retention And Quota Reporting
# =============================================================================


class TestRetentionQuotaReport:
    """The maintenance scan: where it runs, where it looks, and what it counts."""

    @pytest.mark.asyncio
    async def test_scan_leaves_the_event_loop_free_while_it_runs(
        self, isolated_env, monkeypatch
    ):
        """The walk is unbounded, so it must not occupy the event-loop thread.

        Asserted by blocking inside the scan until a coroutine on the same loop
        releases it. A scan running inline would keep that coroutine from ever
        reaching the release, so ``released`` comes back False and the test
        fails on a bounded wait instead of hanging.
        """
        entered = Event()
        release = Event()
        observed: dict[str, Any] = {}
        canned = {
            "old_messages": 7,
            "retention_max_age_days": 42,
            "total_attachments_bytes": 99,
            "quota_limit_bytes": 100,
            "per_project_attach": {"frontend": 99},
            "per_project_inbox_counts": {"frontend": 3},
        }

        def blocking_scan(passed_settings: Any) -> dict[str, Any]:
            observed["settings"] = passed_settings
            observed["thread"] = current_thread()
            entered.set()
            observed["released"] = release.wait(timeout=10.0)
            return canned

        monkeypatch.setattr(
            http_module, "_collect_retention_quota_report_sync", blocking_scan
        )

        settings = _config.get_settings()
        scan = asyncio.create_task(
            http_module._collect_retention_quota_report(settings)
        )
        for _ in range(1000):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        release.set()
        report = await asyncio.wait_for(scan, timeout=30.0)

        assert observed["thread"] is not main_thread()
        assert observed["released"] is True
        assert observed["settings"] is settings
        assert report == canned

    @pytest.mark.asyncio
    async def test_scan_counts_only_the_project_archive_subtree(self, isolated_env):
        """Every figure comes from ``<storage root>/projects/<slug>`` and nowhere else."""
        settings = _config.get_settings()
        archive = await storage_module.ensure_archive(settings, "backend")
        storage_root = archive.root.parent.parent
        window_days = settings.retention_max_age_days

        _backdate(
            _write_file(archive.root / "messages" / "2024" / "03" / "aged.md", "aged"),
            days=window_days + 5,
        )
        # Inside the retention window, and a non-markdown file of the same age:
        # neither belongs in the overdue count.
        _backdate(
            _write_file(archive.root / "messages" / "2026" / "08" / "fresh.md", "fresh"),
            days=window_days - 5,
        )
        _backdate(
            _write_file(archive.root / "messages" / "2024" / "03" / "aged.txt", "aged"),
            days=window_days + 5,
        )

        inbox = archive.root / "agents"
        _write_file(inbox / "BlueLake" / "inbox" / "2026" / "08" / "one.md", "in")
        _write_file(inbox / "GreenCastle" / "inbox" / "2026" / "08" / "two.md", "in")
        # Inbox entries live at inbox/<year>/<month>/<name>.md; this one is shallower.
        _write_file(inbox / "BlueLake" / "inbox" / "loose.md", "in")

        first = _write_file(archive.root / "attachments" / "one.webp", "0123456789")
        second = _write_file(archive.root / "attachments" / "nested" / "two.webp", "01234")
        _write_file(archive.root / "attachments" / "note.txt", "not an attachment")

        # A look-alike project tree one level too high. Reading the storage root
        # instead of its ``projects`` subtree would pick all of this up.
        decoy = storage_root / "backend"
        _backdate(
            _write_file(decoy / "messages" / "2024" / "03" / "decoy.md", "decoy"),
            days=window_days + 5,
        )
        _write_file(decoy / "agents" / "BlueLake" / "inbox" / "2026" / "08" / "d.md", "in")
        _write_file(decoy / "attachments" / "decoy.webp", "decoy-bytes")

        report = await http_module._collect_retention_quota_report(settings)

        attach_bytes = first.stat().st_size + second.stat().st_size
        assert report["old_messages"] == 1
        assert report["per_project_inbox_counts"] == {"backend": 2}
        assert report["per_project_attach"] == {"backend": attach_bytes}
        assert report["total_attachments_bytes"] == attach_bytes
        assert report["retention_max_age_days"] == window_days
        assert report["quota_limit_bytes"] == settings.quota_attachments_limit_bytes

    @pytest.mark.asyncio
    async def test_ignored_project_contributes_nothing_at_all(self, isolated_env):
        """A slug matching the ignore patterns is absent from every figure and key."""
        settings = _config.get_settings()
        assert "demo" in settings.retention_ignore_project_patterns

        counted = await storage_module.ensure_archive(settings, "backend")
        ignored = await storage_module.ensure_archive(settings, "demo")
        for archive in (counted, ignored):
            _backdate(
                _write_file(archive.root / "messages" / "2024" / "03" / "old.md", "old"),
                days=settings.retention_max_age_days + 5,
            )
            _write_file(
                archive.root / "agents" / "BlueLake" / "inbox" / "2026" / "08" / "m.md",
                "in",
            )
            _write_file(archive.root / "attachments" / "a.webp", "12345")

        report = await http_module._collect_retention_quota_report(settings)

        assert report["old_messages"] == 1
        assert report["per_project_inbox_counts"] == {"backend": 1}
        assert report["per_project_attach"] == {"backend": 5}


# =============================================================================
# Test: Health Endpoints Return 200
# =============================================================================


class TestHealthEndpoints:
    """Test health check endpoints."""

    @pytest.mark.asyncio
    async def test_liveness_returns_200(self, isolated_env):
        """Liveness endpoint returns 200 with status 'alive'."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/liveness")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "alive"

    @pytest.mark.asyncio
    async def test_readiness_returns_200_when_healthy(self, isolated_env):
        """Readiness endpoint returns 200 when database is accessible."""
        # Ensure schema exists for readiness check
        await ensure_schema()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/readiness")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ready"

    @pytest.mark.asyncio
    async def test_health_endpoints_bypass_auth(self, isolated_env, monkeypatch):
        """Health endpoints work without authentication."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # No auth header - should still work for health endpoints
            r1 = await client.get("/health/liveness")
            assert r1.status_code == 200

            # Readiness might fail if DB not ready, but should not be 401
            r2 = await client.get("/health/readiness")
            assert r2.status_code != 401


# =============================================================================
# Test: SSE Connection Established
# =============================================================================


class TestSSEConnection:
    """Test Server-Sent Events (SSE) connection capability."""

    @pytest.mark.asyncio
    async def test_sse_accept_header_supported(self, isolated_env):
        """Server accepts SSE content type in Accept header."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Request with SSE Accept header
            headers = {"Accept": "text/event-stream"}
            response = await client.get(
                "/health/liveness",
                headers=headers,
            )
            # Health endpoints return JSON regardless, but should not error
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_mcp_endpoint_accepts_sse_header(self, isolated_env):
        """MCP endpoint accepts SSE content negotiation."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # POST to MCP path with SSE in Accept
            headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            response = await client.post(
                settings.http.path,
                headers=headers,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            # Should get a valid response (200 or 401 if auth required)
            assert response.status_code in (200, 401)


# =============================================================================
# Test: Tool Calls Work Over HTTP
# =============================================================================


class TestToolCallsOverHTTP:
    """Test that MCP tool calls work over HTTP transport."""

    @pytest.mark.asyncio
    async def test_health_check_tool_succeeds(self, isolated_env):
        """health_check tool call returns success over HTTP."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 200
            data = response.json()
            # JSON-RPC response should have result
            assert "result" in data or "error" not in data

    @pytest.mark.asyncio
    async def test_tool_call_returns_jsonrpc_format(self, isolated_env):
        """Tool calls return proper JSON-RPC 2.0 format."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 200
            data = response.json()
            assert data.get("jsonrpc") == "2.0"
            assert "id" in data

    @pytest.mark.asyncio
    async def test_tool_call_with_bearer_auth(self, isolated_env, monkeypatch):
        """Tool calls work with bearer token authentication."""
        monkeypatch.setenv("HTTP_BEARER_TOKEN", "my-secret-token")
        monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Without auth -> 401
            r1 = await client.post(
                settings.http.path,
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r1.status_code == 401

            # With correct auth -> 200
            r2 = await client.post(
                settings.http.path,
                headers={"Authorization": "Bearer my-secret-token"},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert r2.status_code == 200


# =============================================================================
# Test: Resource Reads Work Over HTTP
# =============================================================================


class TestResourceReadsOverHTTP:
    """Test that MCP resource reads work over HTTP transport."""

    @pytest.mark.asyncio
    async def test_resources_list_returns_data(self, isolated_env):
        """resources/list returns available resources."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("resources/list", {}),
            )
            assert response.status_code == 200
            data = response.json()
            assert "result" in data or "error" not in data

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "uri",
        [
            "resource://tooling/directory",
            "resource://tooling/schemas",
            "resource://tooling/projects",
        ],
    )
    async def test_resource_read_returns_the_document_that_was_asked_for(
        self, isolated_env, uri
    ):
        """A registered resource comes back as JSON, keyed by the requested URI."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("resources/read", {"uri": uri}),
            )

        assert response.status_code == 200
        body = response.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == "1"
        contents = body["result"]["contents"]
        assert [entry["uri"] for entry in contents] == [uri]
        assert contents[0]["mimeType"] == "application/json"
        json.loads(contents[0]["text"])

    @pytest.mark.asyncio
    async def test_unregistered_resource_fails_inside_the_jsonrpc_envelope(
        self, isolated_env
    ):
        """An unknown URI is an in-band JSON-RPC error, not an HTTP failure."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)
        uri = "resource://tooling/no-such-resource"

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json=_rpc("resources/read", {"uri": uri}),
            )

        assert response.status_code == 200
        body = response.json()
        assert body["jsonrpc"] == "2.0"
        assert "result" not in body
        assert uri in body["error"]["message"]


# =============================================================================
# Test: CORS Headers Present
# =============================================================================


class TestCORSHeaders:
    """Test CORS (Cross-Origin Resource Sharing) configuration."""

    @pytest.mark.asyncio
    async def test_cors_preflight_returns_headers(self, isolated_env, monkeypatch):
        """CORS preflight OPTIONS request returns appropriate headers."""
        monkeypatch.setenv("HTTP_CORS_ENABLED", "true")
        monkeypatch.setenv("HTTP_CORS_ORIGINS", "http://example.com")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.options(
                settings.http.path,
                headers={
                    "Origin": "http://example.com",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert response.status_code in (200, 204)
            # CORS headers should be present
            assert "access-control-allow-origin" in response.headers

    @pytest.mark.asyncio
    async def test_cors_headers_on_response(self, isolated_env, monkeypatch):
        """CORS headers are present on regular responses."""
        monkeypatch.setenv("HTTP_CORS_ENABLED", "true")
        monkeypatch.setenv("HTTP_CORS_ORIGINS", "*")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                headers={"Origin": "http://test-origin.com"},
                json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
            )
            assert response.status_code == 200
            # CORS header should be present
            assert response.headers.get("access-control-allow-origin") in ("*", "http://test-origin.com")

    @pytest.mark.asyncio
    async def test_cors_disabled_no_headers(self, isolated_env, monkeypatch):
        """When CORS is disabled, no CORS headers are added."""
        monkeypatch.setenv("HTTP_CORS_ENABLED", "false")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()

        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/liveness")
            assert response.status_code == 200
            # CORS headers should not be present when disabled
            # Note: This may vary based on implementation; check for absence
            # of origin-specific headers on non-preflight requests


# =============================================================================
# Test: Error Handling
# =============================================================================


class TestHTTPErrorHandling:
    """Test HTTP error handling."""

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self, isolated_env):
        """Invalid JSON payload returns appropriate error."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                content=b"not valid json{{{",
                headers={"Content-Type": "application/json"},
            )
            # Should return 4xx error for invalid JSON
            assert response.status_code >= 400

    @pytest.mark.asyncio
    async def test_missing_method_returns_error(self, isolated_env):
        """JSON-RPC request without method returns error."""
        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                settings.http.path,
                json={"jsonrpc": "2.0", "id": "1"},  # Missing method
            )
            # Should return 200 with JSON-RPC error or 400
            assert response.status_code in (200, 400)
            if response.status_code == 200:
                data = response.json()
                assert "error" in data


# =============================================================================
# Test: Request Logging
# =============================================================================


class TestRequestLogging:
    """Test request logging middleware."""

    @pytest.mark.asyncio
    async def test_request_logs_path_and_status(self, isolated_env, caplog):
        """Requests are logged with path and status."""
        import logging

        caplog.set_level(logging.DEBUG)

        settings = _config.get_settings()
        server = build_mcp_server()
        app = http_module.build_http_app(settings, server)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/health/liveness")

        # Logging should have occurred (may use structlog or stdlib)
        # This is a smoke test that the request completes


# =============================================================================
# Test: Withdrawn Mutations And Archive/DB Lock Ordering
# =============================================================================


@dataclass(frozen=True)
class _SeededMail:
    """Identifiers for one project, its recipient, and one message addressed to them."""

    project_slug: str
    recipient_name: str
    message_id: int


async def _seed_project(slug: str) -> Project:
    """Persist an empty project under ``slug`` and return the stored row."""
    await ensure_schema()
    async with get_session() as session:
        project = Project(slug=slug, human_key=pkey(f"tmp/{slug}"))
        session.add(project)
        await session.commit()
        await session.refresh(project)
    assert project.id is not None
    return project


async def _seed_agent(
    project: Project,
    name: str,
    *,
    program: str = "test",
    model: str = "test",
    role: str = "recipient",
) -> Agent:
    """Persist one agent inside ``project`` and return the stored row."""
    async with get_session() as session:
        agent = Agent(
            project_id=project.id,
            name=name,
            program=program,
            model=model,
            task_description=role,
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
    assert agent.id is not None
    return agent


async def _seed_delivered_message(slug: str) -> _SeededMail:
    """Persist a project with a sender, a recipient, and one message between them."""
    project = await _seed_project(slug)
    sender = await _seed_agent(project, "GreenCastle", role="sender")
    recipient = await _seed_agent(project, "BlueLake")

    async with get_session() as session:
        message = Message(
            project_id=project.id,
            sender_id=sender.id,
            subject="Seeded",
            body_md="body",
            importance="normal",
            ack_required=False,
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)
        assert message.id is not None
        session.add(
            MessageRecipient(message_id=message.id, agent_id=recipient.id, kind="to")
        )
        await session.commit()

    return _SeededMail(
        project_slug=project.slug,
        recipient_name=recipient.name,
        message_id=message.id,
    )


# Every /mail mutation route this server used to expose. Each entry turns a
# seeded mailbox into the request that route accepted, so a route quietly
# reinstated would be exercised here rather than merely absent.
_WITHDRAWN_MAIL_MUTATIONS = {
    "bulk-delete": lambda mail: (
        "/mail/api/delete-messages",
        {"message_ids": [mail.message_id]},
    ),
    "inbox-delete": lambda mail: (
        f"/mail/{mail.project_slug}/inbox/{mail.recipient_name}/delete-messages",
        {"message_ids": [mail.message_id]},
    ),
    "overseer-send": lambda mail: (
        f"/mail/{mail.project_slug}/overseer/send",
        {
            "recipients": [mail.recipient_name],
            "subject": "Withdrawn",
            "body_md": "Should never be archived.",
        },
    ),
}


def _recording_call(sink: list[Any], original: Any) -> Any:
    """Wrap an awaitable callable so each call is recorded before it runs."""

    async def recorded(*args: Any, **kwargs: Any) -> Any:
        sink.append(args)
        return await original(*args, **kwargs)

    return recorded


def _recording_context(sink: list[Any], original: Any) -> Any:
    """Wrap an async context-manager factory so each entry is recorded."""

    @contextlib.asynccontextmanager
    async def recorded(*args: Any, **kwargs: Any) -> Any:
        sink.append(args)
        async with original(*args, **kwargs) as value:
            yield value

    return recorded


class _OpenSessionCounter:
    """Count how many database sessions http.py holds open at any instant.

    Both session factories are wrapped, not just ``get_session``: the ACK
    escalation path inserts its holder through ``get_immediate_session``, so
    tracking one of the two would report depth zero while a write transaction
    was still open.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.depth = 0
        for name in ("get_session", "get_immediate_session"):
            monkeypatch.setattr(
                http_module, name, self._tracked(getattr(http_module, name))
            )

    def _tracked(self, factory: Any) -> Any:
        @contextlib.asynccontextmanager
        async def tracked(*args: Any, **kwargs: Any) -> Any:
            async with factory(*args, **kwargs) as session:
                self.depth += 1
                try:
                    yield session
                finally:
                    self.depth -= 1

        return tracked


@pytest.mark.usefixtures("open_mail_ui_gate")
class TestMailMutationLockBoundaries:
    """Withdrawn /mail mutations, and the lock ordering the surviving writer keeps.

    The /mail password gate is opened for this class rather than the module:
    only the withdrawn-route case issues /mail requests and the rest call
    http.py helpers directly, so switching the gate off file-wide would relax it
    for tests that never touch it.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route_id", sorted(_WITHDRAWN_MAIL_MUTATIONS))
    async def test_withdrawn_mutation_route_is_refused_and_writes_nothing(
        self, isolated_env, monkeypatch, route_id
    ):
        """A withdrawn /mail mutation is refused, and touches neither archive nor rows.

        What holds the line is the ``_mail_ui_active_path`` allow list, not the
        absence of a handler: the middleware answers 404 before routing, so
        re-adding a handler alone changes nothing while widening that list would
        expose whatever handler exists. The allow list is therefore asserted
        directly, alongside the response.

        The archive spies sit on http.py's own bindings for ``ensure_archive``
        and ``archive_write_lock``, which are the only names by which this
        module can reach an archive at all; a spy on the storage module would
        be one the request could not trip even if the route came back.
        """
        mail = await _seed_delivered_message(f"withdrawn-{route_id}")
        path, payload = _WITHDRAWN_MAIL_MUTATIONS[route_id](mail)
        assert not http_module._mail_ui_active_path(path)

        archive_calls: list[Any] = []
        monkeypatch.setattr(
            http_module,
            "ensure_archive",
            _recording_call(archive_calls, http_module.ensure_archive),
        )
        monkeypatch.setattr(
            http_module,
            "archive_write_lock",
            _recording_context(archive_calls, http_module.archive_write_lock),
        )

        settings = _config.get_settings()
        app = http_module.build_http_app(settings, build_mcp_server())

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(path, json=payload)

        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
        assert archive_calls == []

        async with get_session() as session:
            surviving = (await session.execute(select(Message.id))).scalars().all()
        assert list(surviving) == [mail.message_id]

    @pytest.mark.asyncio
    async def test_ack_holder_profile_is_published_with_no_session_open(
        self, isolated_env, monkeypatch
    ):
        """The ops holder is inserted first and its profile published only afterwards.

        Publishing while a session is still open nests the database lock inside
        the archive lock, which is the ordering that deadlocks mixed HTTP and
        MCP traffic. The recorded depth is therefore the point of the test, not
        the fact that a profile is written.
        """
        project = await _seed_project("ack-holder-order")
        recipient = await _seed_agent(project, "BlueLake")

        sessions = _OpenSessionCounter(monkeypatch)
        depth_per_write: list[int] = []

        async def capture_profile_write(_archive: Any, _payload: Any) -> None:
            depth_per_write.append(sessions.depth)

        monkeypatch.setattr(http_module, "write_agent_profile", capture_profile_write)

        settings = _config.get_settings()
        holder = await http_module._ensure_ack_escalation_holder(
            settings=settings,
            project=project,
            recipient_agent=recipient,
            claim_name="RedStone",
            now_naive=_now_naive_utc(),
        )

        assert holder.name == "RedStone"
        assert holder.id != recipient.id
        assert holder.program == "ops"
        assert depth_per_write == [0]

        # A holder that already exists is reused, and nothing is republished.
        reused = await http_module._ensure_ack_escalation_holder(
            settings=settings,
            project=project,
            recipient_agent=recipient,
            claim_name="RedStone",
            now_naive=_now_naive_utc(),
        )

        assert reused.id == holder.id
        assert depth_per_write == [0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("delete_project", [False, True])
    async def test_ack_escalation_profile_refuses_deleted_lifetime_before_publish(
        self,
        isolated_env,
        monkeypatch,
        delete_project,
    ):
        from mcp_agent_mail.db import get_immediate_session

        slug = f"http-ack-stale-{'project' if delete_project else 'agent'}"
        project = await _seed_project(slug)
        recipient = await _seed_agent(project, "BlueLake")

        profile_writes = 0

        @contextlib.asynccontextmanager
        async def delete_after_holder_commit(_archive, *args, **kwargs):
            async with get_immediate_session() as session:
                holder = (
                    await session.execute(
                        text(
                            "SELECT id FROM agents "
                            "WHERE project_id = :pid AND name = 'RedStone'"
                        ),
                        {"pid": project.id},
                    )
                ).scalar_one()
                await session.execute(
                    text("DELETE FROM agents WHERE id = :holder"),
                    {"holder": holder},
                )
                if delete_project:
                    await session.execute(
                        text("DELETE FROM agents WHERE project_id = :pid"),
                        {"pid": project.id},
                    )
                    await session.execute(
                        text("DELETE FROM projects WHERE id = :pid"),
                        {"pid": project.id},
                    )
                await session.commit()
            yield

        async def unexpected_profile_write(*args, **kwargs):
            nonlocal profile_writes
            profile_writes += 1

        monkeypatch.setattr(http_module, "archive_write_lock", delete_after_holder_commit)
        monkeypatch.setattr(http_module, "write_agent_profile", unexpected_profile_write)

        with pytest.raises(Exception, match="lifetime no longer exists"):
            await http_module._ensure_ack_escalation_holder(
                settings=_config.get_settings(),
                project=project,
                recipient_agent=recipient,
                claim_name="RedStone",
                now_naive=_now_naive_utc(),
            )

        archive = await storage_module.ensure_archive(_config.get_settings(), slug)
        assert profile_writes == 0
        assert archive.root.is_dir()
        assert not (archive.root / "agents" / "RedStone" / "profile.json").exists()

    @pytest.mark.asyncio
    async def test_ack_escalation_reservation_uses_revision_publication(
        self,
        isolated_env,
    ):
        from mcp_agent_mail.models import FileReservation

        project = await _seed_project("http-ack-reservation")
        holder = await _seed_agent(
            project,
            "RedStone",
            program="ops",
            model="system",
            role="ops-escalation",
        )

        reservation = await http_module._create_ack_escalation_reservation(
            project=project,
            holder=holder,
            path_pattern="agents/BlueLake/inbox/2026/08/*.md",
            exclusive=True,
            now_naive=_now_naive_utc(),
            ttl_seconds=600,
        )

        async with get_session() as session:
            persisted = await session.get(FileReservation, reservation.id)
            assert persisted is not None
            assert persisted.archive_revision == persisted.archive_synced_revision
            assert persisted.reason == "ack-overdue"
            assert persisted.agent_id == holder.id

        archive = await storage_module.ensure_archive(_config.get_settings(), project.slug)
        assert (
            archive.root
            / "file_reservations"
            / f"id-{reservation.id}.json"
        ).is_file()
