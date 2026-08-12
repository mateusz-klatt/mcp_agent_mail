"""Retirement contract for the removed server-rendered archive browser."""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config, webauth
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.models import UiUser
from mcp_agent_mail.storage import ensure_archive, get_commit_detail

SESSION_SECRET = "archive-retirement-session-secret-0123456789"
RETIRED_ARCHIVE_PATHS = (
    "/mail/archive/guide",
    "/mail/archive/activity?limit=5",
    "/mail/archive/commit/deadbeef",
    "/mail/archive/timeline?project=retired-project",
    "/mail/archive/browser?project=retired-project&path=messages",
    "/mail/archive/browser/retired-project/file?path=messages/1.md",
    "/mail/archive/browser/retired-project/download?path=messages/1.md",
    "/mail/archive/network?project=retired-project",
    "/mail/archive/time-travel",
    ("/mail/archive/time-travel/snapshot?project=retired-project&agent=RetiredAgent&timestamp=2026-08-12T12:00"),
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
async def test_retired_archive_routes_are_fail_closed(
    isolated_env,
    monkeypatch,
    method: str,
    authenticated: bool,
):
    """Every former archive UI route is a 404 before authentication or routing."""
    del isolated_env
    monkeypatch.setenv("MAIL_UI_AUTH_ENABLED", "true")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", SESSION_SECRET)
    _config.clear_settings_cache()
    settings = _config.get_settings()
    await ensure_schema()
    cookies = await _authenticated_cookie("archive-retirement-admin") if authenticated else None
    app = build_http_app(settings, build_mcp_server())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
    ) as client:
        for path in RETIRED_ARCHIVE_PATHS:
            response = await client.request(method, path)
            assert response.status_code == 404, (method, authenticated, path)
            assert response.headers["cache-control"] == "no-store, no-transform"
            assert response.headers["x-content-type-options"] == "nosniff"
            if method == "GET":
                assert response.json() == {"detail": "Not Found"}
            else:
                assert response.content == b""


@pytest.mark.asyncio
async def test_get_commit_detail_bounds_non_utf8_diff(isolated_env):
    """The storage helper remains byte-bounded and UTF-8 safe without the HTTP UI."""
    del isolated_env
    archive = await ensure_archive(_config.get_settings(), "archive-helper")
    invalid_path = archive.root / "messages" / "a-invalid-utf8.txt"
    large_path = archive.root / "messages" / "z-large-preview.txt"
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_bytes(b"small file\n\xff\xfe invalid utf8\n")
    large_path.write_bytes((b"Z" * 16_384) + b"\n")
    relative_paths = [
        str(invalid_path.relative_to(archive.repo_root)),
        str(large_path.relative_to(archive.repo_root)),
    ]

    try:
        await asyncio.to_thread(
            subprocess.run,
            ["git", "add", "--", *relative_paths],
            cwd=archive.repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        await asyncio.to_thread(
            subprocess.run,
            ["git", "commit", "-m", "archive: bounded non-UTF8 preview"],
            cwd=archive.repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        head = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "HEAD"],
            cwd=archive.repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert isinstance(head.stdout, str)
        detail = await get_commit_detail(
            archive.repo,
            head.stdout.strip(),
            max_diff_size=512,
        )
    finally:
        await asyncio.to_thread(archive.repo.close)

    assert detail["diff_truncated"] is True
    assert detail["diff_limit_bytes"] == 512
    assert len(detail["diff"].encode("utf-8")) <= 512
    assert "a-invalid-utf8.txt" in detail["diff"]
    assert "\ufffd\ufffd invalid utf8" in detail["diff"]
    assert "z-large-preview.txt" not in detail["diff"]
