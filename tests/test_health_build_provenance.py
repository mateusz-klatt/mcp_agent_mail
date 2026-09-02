"""A production instance that cannot name its commit must not report ready.

The defect these tests exist for is a deployment mistake, not a code one:
``compose.prod.yaml`` documents the build command that supplies
``MCP_AGENT_MAIL_BUILD_COMMIT``, and a plain ``docker compose build`` silently
produces an image whose ``git_sha`` is ``null``. It happened twice in one day.
Documentation that has already failed is not the fix, so the condition is
asserted by the container's own healthcheck (``Dockerfile`` points HEALTHCHECK
at ``/health/readiness``) and a deploy that waits for health stops on it.

Each test carries the control that the one beside it would otherwise be missing:
a gate that always failed would satisfy the refusal on its own, and a gate that
never fired would satisfy both permissive cases. The three together pin the
condition to exactly ``production AND no commit``.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config, http as http_module
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema

_SHA = "f" * 40


async def _readiness(monkeypatch, *, environment: str, build_commit: str):
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    monkeypatch.setenv("MCP_AGENT_MAIL_BUILD_COMMIT", build_commit)
    _config.clear_settings_cache()
    await ensure_schema()
    settings = _config.get_settings()
    app = http_module.build_http_app(settings, build_mcp_server())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/health/readiness")


@pytest.mark.asyncio
async def test_production_without_a_build_commit_is_not_ready(isolated_env, monkeypatch):
    response = await _readiness(monkeypatch, environment="production", build_commit="")

    assert response.status_code == 503
    # The message has to say what to do, because the reflex on an unhealthy
    # container is to restart it, and a restart cannot clear this.
    detail = response.json()["detail"]
    assert "MCP_AGENT_MAIL_BUILD_COMMIT" in detail
    assert "restart" in detail.lower()


@pytest.mark.asyncio
async def test_production_with_a_build_commit_is_ready(isolated_env, monkeypatch):
    """Control. Without it, a gate that refused everything would pass the test above."""
    response = await _readiness(monkeypatch, environment="production", build_commit=_SHA)

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    # Health answers unauthenticated callers, so provenance is asserted, not published.
    assert _SHA not in response.text


@pytest.mark.asyncio
async def test_a_non_production_environment_without_a_commit_is_ready(
    isolated_env,
    monkeypatch,
):
    """Control. Developer machines and CI build without the argument by design."""
    response = await _readiness(monkeypatch, environment="development", build_commit="")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
