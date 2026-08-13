"""Cutover contract for the removed server-rendered mail viewer."""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config, http as http_module, webauth
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.models import UiUser

SESSION_SECRET = "mail-retirement-session-secret-0123456789"
RETIRED_MAIL_READ_PATHS = (
    "/mail/v2",
    "/mail/v2/",
    "/mail/api/unified-inbox",
    "/mail/api/unified-inbox?limit=1",
    "/mail/test-proj/inbox/BlueLake",
    "/mail/test-proj/inbox/BlueLake?page=1&limit=10",
    "/mail/test-proj/inbox/NonexistentAgent",
    "/mail/test-proj/file_reservations",
    "/mail/test-proj/attachments",
    "/mail/test-proj/overseer/compose",
    "/mail/not-a-project/overseer/compose",
    "/mail/api/locks",
    "/mail/static/legacy.js",
)
RETIRED_MAIL_WRITE_PATHS = (
    "/mail/test-proj/inbox/BlueLake/mark-read",
    "/mail/test-proj/inbox/BlueLake/mark-all-read",
    "/mail/test-proj/overseer/send",
    "/mail/test-proj/overseer/reply",
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


def _assert_not_found(response, *, expect_body: bool) -> None:
    assert response.status_code == 404
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["x-content-type-options"] == "nosniff"
    if expect_body:
        assert response.json() == {"detail": "Not Found"}
    else:
        assert response.content == b""


@pytest.mark.parametrize("authenticated", [False, True], ids=["anonymous", "admin-session"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.asyncio
async def test_retired_mail_read_routes_are_fail_closed(
    isolated_env,
    monkeypatch,
    method: str,
    authenticated: bool,
):
    """Former viewer reads are 404 before authentication or legacy routing."""
    del isolated_env
    monkeypatch.setenv("MAIL_UI_AUTH_ENABLED", "true")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", SESSION_SECRET)
    _config.clear_settings_cache()
    settings = _config.get_settings()
    await ensure_schema()
    cookies = await _authenticated_cookie("mail-retirement-admin") if authenticated else None
    app = build_http_app(settings, build_mcp_server())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
    ) as client:
        for path in RETIRED_MAIL_READ_PATHS:
            response = await client.request(method, path)
            _assert_not_found(response, expect_body=method == "GET")


@pytest.mark.parametrize("authenticated", [False, True], ids=["anonymous", "admin-session"])
@pytest.mark.asyncio
async def test_retired_mail_write_routes_reject_before_body_parsing(
    isolated_env,
    monkeypatch,
    authenticated: bool,
):
    """Former mutations are 404 even with an authenticated malformed request."""
    del isolated_env
    monkeypatch.setenv("MAIL_UI_AUTH_ENABLED", "true")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", SESSION_SECRET)
    _config.clear_settings_cache()
    settings = _config.get_settings()
    await ensure_schema()
    cookies = await _authenticated_cookie("mail-retirement-writer") if authenticated else None
    app = build_http_app(settings, build_mcp_server())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=cookies,
    ) as client:
        for path in RETIRED_MAIL_WRITE_PATHS:
            response = await client.post(
                path,
                content=b"{",
                headers={"content-type": "application/json", "origin": "http://test"},
            )
            _assert_not_found(response, expect_body=True)


@pytest.mark.asyncio
async def test_canonical_mail_path_serves_only_the_react_shell(
    isolated_env,
    monkeypatch,
    tmp_path,
):
    """The canonical `/mail` entry point remains active after legacy retirement."""
    del isolated_env
    monkeypatch.setenv("MAIL_UI_AUTH_ENABLED", "false")
    _config.clear_settings_cache()
    dist_root = tmp_path / "ui_dist"
    dist_root.mkdir()
    (dist_root / "index.html").write_text(
        '<div id="root"></div><script src="/mail/assets/app.js"></script>',
        encoding="utf-8",
    )
    monkeypatch.setattr(http_module, "_mail_react_dist_root", lambda: dist_root)
    app = build_http_app(_config.get_settings(), build_mcp_server())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/mail")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<div id="root"></div>' in response.text
    assert 'src="/mail/assets/' in response.text
