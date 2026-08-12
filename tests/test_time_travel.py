"""Retirement contract for the removed HTTP time-travel viewer."""

from __future__ import annotations

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config, webauth
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.models import UiUser
from mcp_agent_mail.storage import (
    ensure_archive,
    get_historical_inbox_snapshot,
    write_agent_profile,
    write_message_bundle,
)

SESSION_SECRET = "time-travel-retirement-secret-0123456789"
RETIRED_TIME_TRAVEL_PATHS = (
    "/mail/archive/time-travel",
    "/mail/archive/time-travel/",
    "/mail/archive/time-travel?project=timetravel-test",
    "/mail/archive/time-travel/snapshot",
    (
        "/mail/archive/time-travel/snapshot?project=timetravel-test"
        "&agent=TimeTraveler&timestamp=2099-12-31T23:59:59Z"
    ),
    (
        "/mail/archive/time-travel/snapshot?project=../../../etc/passwd"
        "&agent=invalid%20agent&timestamp=not-a-timestamp"
    ),
    (
        "/mail/archive/time-travel/snapshot?project=%3Cscript%3E"
        "&agent=%3Cimg%20onerror%3Dalert(1)%3E&timestamp=2024-01-01"
    ),
)


async def _authenticated_cookie(username: str) -> dict[str, str]:
    async with get_session() as session:
        user = UiUser(
            username=username,
            password_hash=webauth.hash_password("irrelevant"),
            role=webauth.ROLE_ADMIN,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        epoch = int(user.session_epoch)
        generation = user.session_generation
    return {
        "agent_mail_session": webauth.make_session(
            username,
            epoch=epoch,
            generation=generation,
            now=time.time(),
            secret=SESSION_SECRET.encode(),
        )
    }


@pytest.mark.parametrize("authenticated", [False, True], ids=["anonymous", "admin-session"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.asyncio
async def test_retired_time_travel_routes_are_fail_closed(
    isolated_env,
    monkeypatch,
    method: str,
    authenticated: bool,
):
    """Every former time-travel route is a 404 before parameter validation."""
    del isolated_env
    monkeypatch.setenv("MAIL_UI_AUTH_ENABLED", "true")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", SESSION_SECRET)
    _config.clear_settings_cache()
    settings = _config.get_settings()
    await ensure_schema()
    cookies = await _authenticated_cookie("time-travel-retirement-admin") if authenticated else None
    app = build_http_app(settings, build_mcp_server())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
    ) as client:
        for path in RETIRED_TIME_TRAVEL_PATHS:
            response = await client.request(method, path)
            assert response.status_code == 404, (method, authenticated, path)
            assert response.headers["cache-control"] == "no-store, no-transform"
            assert response.headers["x-content-type-options"] == "nosniff"
            if method == "GET":
                assert response.json() == {"detail": "Not Found"}
            else:
                assert response.content == b""


@pytest.mark.asyncio
async def test_historical_snapshot_storage_helper_remains_available(isolated_env):
    """Historical snapshots remain a storage capability without an HTTP UI."""
    del isolated_env
    archive = await ensure_archive(_config.get_settings(), "time-travel-helper")
    await write_agent_profile(
        archive,
        {
            "name": "TimeTraveler",
            "program": "test",
            "model": "test",
            "task_description": "Direct snapshot contract",
        },
    )
    await write_message_bundle(
        archive,
        message={
            "id": 1,
            "subject": "Snapshot Message",
            "created": "2026-08-12T12:00:00+00:00",
        },
        body_md="Stored directly for the historical snapshot helper.",
        sender="TimeTraveler",
        recipients=["TimeTraveler"],
    )

    try:
        snapshot = await get_historical_inbox_snapshot(
            archive,
            "TimeTraveler",
            "2099-01-01T00:00:00Z",
        )
        invalid = await get_historical_inbox_snapshot(
            archive,
            "TimeTraveler",
            "not-a-timestamp",
        )
    finally:
        await asyncio.to_thread(archive.repo.close)

    assert snapshot["requested_time"] == "2099-01-01T00:00:00Z"
    assert snapshot["resolved_agent_name"] == "TimeTraveler"
    assert snapshot["commit_sha"]
    assert [message["subject"] for message in snapshot["messages"]] == ["Snapshot Message"]
    assert invalid["messages"] == []
    assert invalid["commit_sha"] is None
    assert invalid["error"].startswith("Invalid timestamp format:")
