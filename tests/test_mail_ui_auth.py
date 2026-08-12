"""The gate in front of /mail, which nothing else touches.

`MailUiAuthMiddleware` arrived with the password login for the viewer and is the
only thing standing between an unauthenticated request and a UI that can delete
projects. It had no coverage of any kind: no test named it, and no test asserted
a 401, 403 or 503 on a `/mail` route — measured both ways, against 28 such
assertions elsewhere in this suite, so the zero was real rather than a pattern
that failed to match.

That absence is load-bearing right now. Every existing `/mail` test predates the
gate and fails against it, so the obvious repair is to switch the gate off for
them. Doing that first would leave the suite with no contact with this code at
all — the incidental coverage removed and the deliberate coverage never written.
These tests are the deliberate coverage, so the switch can be thrown safely.

Each case pins a decision rather than a status code: fail closed when unsigned,
refuse when unauthenticated, and stand aside when explicitly disabled. The codes
are how those decisions are observed, not the point.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from authlib.jose import jwt
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mcp_agent_mail import (
    config as _config,
    db as db_module,
    http as http_module,
    webauth,
)
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app

BEARER = "mail-ui-gate-bearer"
SECRET = "mail-ui-gate-session-secret-0123456789"
EXPECTED_FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'
    b'  <rect width="64" height="64" rx="14" fill="#12242c"/>\n'
    b'  <path d="M17 14h9v14h12V14h9v36h-9V36H26v14h-9z" fill="#f4cf8a"/>\n'
    b"</svg>\n"
)
FAVICON_LINK = '<link rel="icon" href="/favicon.ico" type="image/svg+xml" sizes="any" />'
REACT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; font-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
LEGACY_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
    "img-src 'self' data: blob:; font-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)

# A route the gate protects and that renders without any project existing, so a
# failure here is the gate's answer and not a missing fixture.
GUARDED = "/mail/api/v1/projects"
PROFILE_PATH = "/mail/api/v1/me/profile"
PASSWORD_PATH = "/mail/api/v1/me/password"
ADMIN_ACCESS_PATH = "/mail/api/v1/admin/access"
SAME_ORIGIN_HEADERS = {
    "Origin": "http://test",
    "Referer": "http://test/",
    "Host": "test",
}


def _build(monkeypatch, **env: str):
    monkeypatch.setenv("HTTP_BEARER_TOKEN", BEARER)
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    return settings, build_http_app(settings, build_mcp_server())


async def _get(app, path: str, *, bearer: bool = True):
    """Fetch `path`, with or without the server bearer.

    The flag is the point of several of these cases rather than a convenience:
    a Bearer header short-circuits this middleware entirely, so sending one by
    habit turns a test of the gate into a test of the bearer layer behind it.
    """
    headers = {"Authorization": f"Bearer {BEARER}"} if bearer else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers=headers)


class TestMailUiGate:
    @pytest.mark.asyncio
    async def test_unset_secret_fails_closed(self, isolated_env, monkeypatch):
        """An empty secret cannot sign a cookie, so the UI must not be served.

        This is the shipped default — auth on, secret empty — which means a
        fresh deployment answers 503 here until someone sets the variable. The
        detail line has to name it, or the next person to stand this server up
        reads a 503 as a broken service rather than an unfinished setup.
        """
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET="")
        response = await _get(app, GUARDED)

        assert response.status_code == 503
        assert "MAIL_UI_SESSION_SECRET" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_signed_but_unauthenticated_is_refused(self, isolated_env, monkeypatch):
        """With a secret present the gate works, and a caller without a session
        is turned away — a different answer from the one above, and the
        distinction is the whole reason both cases are here.

        Setting the secret moves the failure from 503 to 401 without changing
        how many requests fail. Anyone repairing the older `/mail` tests will
        see that shift and can easily read it as progress; it is a move from
        "the gate cannot function" to "the gate functioned and said no".
        """
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        response = await _get(app, GUARDED, bearer=False)

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_server_bearer_cannot_read_the_human_mailbox(
        self, isolated_env, monkeypatch
    ):
        """The fleet bearer cannot become an all-project human principal."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        await ensure_schema()
        with_bearer = await _get(app, GUARDED)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            without_bearer = await client.get(GUARDED)

        assert with_bearer.status_code == 401
        assert without_bearer.status_code == 401

    @pytest.mark.asyncio
    async def test_explicitly_disabled_gate_stands_aside(self, isolated_env, monkeypatch):
        """Switching auth off must hand the request to the bearer layer rather
        than keep refusing.

        This is the escape hatch the older `/mail` tests need, and it is worth
        pinning for exactly that reason: those tests will depend on it, so it
        stops being a configuration option and becomes a contract.
        """
        _settings, app = _build(
            monkeypatch, MAIL_UI_AUTH_ENABLED="false", MAIL_UI_SESSION_SECRET=""
        )
        await ensure_schema()
        response = await _get(app, GUARDED)

        assert response.status_code != 503
        assert response.status_code != 401

    @pytest.mark.asyncio
    async def test_login_route_is_reachable_while_the_gate_is_closed(
        self, isolated_env, monkeypatch
    ):
        """The way in cannot sit behind the thing it opens."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        await ensure_schema()
        response = await _get(app, "/mail/login")

        assert response.status_code != 401


class TestPublicRootAndFavicon:
    """The two public entry points stay narrow and outside transport auth."""

    @pytest.mark.asyncio
    async def test_get_and_head_are_public_for_anonymous_bearer_and_jwt(
        self,
        isolated_env,
        monkeypatch,
    ):
        settings, app = _build(
            monkeypatch,
            MAIL_UI_SESSION_SECRET=SECRET,
            HTTP_PATH="/api/",
            HTTP_JWT_ENABLED="true",
            HTTP_JWT_ALGORITHMS="HS256",
            HTTP_JWT_SECRET="public-entrypoint-jwt-secret",
        )
        jwt_token = jwt.encode(
            {"alg": "HS256"},
            {"sub": "favicon-client", settings.http.jwt_role_claim: "reader"},
            settings.http.jwt_secret,
        ).decode("utf-8")
        callers = {
            "anonymous": {},
            "static-bearer": {"Authorization": f"Bearer {BEARER}"},
            "jwt": {"Authorization": f"Bearer {jwt_token}"},
        }
        raw_query = "next=%2Fmail%23settings&plus=a+b&empty=&flag"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for caller, headers in callers.items():
                root = await client.get(f"/?{raw_query}", headers=headers)
                root_head = await client.head(f"/?{raw_query}", headers=headers)
                favicon = await client.get("/favicon.ico", headers=headers)
                favicon_head = await client.head("/favicon.ico", headers=headers)

                expected_root_headers = {
                    "cache-control": "no-store, no-transform",
                    "content-security-policy": REACT_CSP,
                    "referrer-policy": "no-referrer",
                    "x-content-type-options": "nosniff",
                    "x-frame-options": "DENY",
                    "location": f"/mail?{raw_query}",
                    "content-length": "0",
                }
                assert root.status_code == 307, caller
                assert dict(root.headers) == expected_root_headers, caller
                assert root.content == b"", caller
                assert root_head.status_code == 307, caller
                assert dict(root_head.headers) == expected_root_headers, caller
                assert root_head.content == b"", caller

                expected_favicon_headers = {
                    "cache-control": "public, max-age=86400",
                    "content-length": str(len(EXPECTED_FAVICON_SVG)),
                    "x-content-type-options": "nosniff",
                    "content-type": "image/svg+xml",
                }
                assert favicon.status_code == 200, caller
                assert dict(favicon.headers) == expected_favicon_headers, caller
                assert favicon.content == EXPECTED_FAVICON_SVG, caller
                assert favicon_head.status_code == 200, caller
                assert dict(favicon_head.headers) == expected_favicon_headers, caller
                assert favicon_head.content == b"", caller

        passive_svg = EXPECTED_FAVICON_SVG.lower()
        for active_fragment in (
            b"<script",
            b"javascript:",
            b"onload=",
            b"onclick=",
            b"href=",
            b"<foreignobject",
            b"<animate",
            b"<set",
        ):
            assert active_fragment not in passive_svg

    @pytest.mark.asyncio
    async def test_other_methods_and_nearby_paths_still_require_transport_auth(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(
            monkeypatch,
            MAIL_UI_SESSION_SECRET=SECRET,
            HTTP_PATH="/api/",
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            root_post = await client.post("/")
            favicon_post = await client.post("/favicon.ico")
            favicon_directory = await client.get("/favicon.ico/")

        for response in (root_post, favicon_post, favicon_directory):
            assert response.status_code == 401
            assert response.json() == {"detail": "Unauthorized"}
            assert "location" not in response.headers

    @pytest.mark.asyncio
    async def test_api_and_mcp_aliases_keep_their_existing_auth_boundary(
        self,
        isolated_env,
        monkeypatch,
    ):
        settings, app = _build(
            monkeypatch,
            MAIL_UI_SESSION_SECRET=SECRET,
            HTTP_PATH="/api/",
            HTTP_JWT_ENABLED="true",
            HTTP_JWT_ALGORITHMS="HS256",
            HTTP_JWT_SECRET="transport-boundary-jwt-secret",
        )
        jwt_token = jwt.encode(
            {"alg": "HS256"},
            {"sub": "transport-client", settings.http.jwt_role_claim: "reader"},
            settings.http.jwt_secret,
        ).decode("utf-8")
        initialize = {
            "jsonrpc": "2.0",
            "id": "public-entrypoint-auth-check",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "root-favicon-test", "version": "1.0"},
            },
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for path in ("/api/", "/mcp/"):
                anonymous = await client.post(path, json=initialize)
                assert anonymous.status_code == 401, path
                assert anonymous.json() == {"detail": "Unauthorized"}, path

                for headers in (
                    {"Authorization": f"Bearer {BEARER}"},
                    {"Authorization": f"Bearer {jwt_token}"},
                ):
                    authenticated = await client.post(path, headers=headers, json=initialize)
                    assert authenticated.status_code == 200, (path, authenticated.text)

    @pytest.mark.parametrize("configured_path", ["/", "////"])
    @pytest.mark.asyncio
    async def test_root_mcp_mount_is_never_replaced_by_the_redirect(
        self,
        isolated_env,
        monkeypatch,
        configured_path: str,
    ):
        _settings, app = _build(
            monkeypatch,
            MAIL_UI_SESSION_SECRET=SECRET,
            HTTP_PATH=configured_path,
        )
        health_call = {
            "jsonrpc": "2.0",
            "id": "root-mcp-health",
            "method": "tools/call",
            "params": {"name": "health_check", "arguments": {}},
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            anonymous_get = await client.get("/")
            authenticated_post = await client.post(
                "/",
                headers={"Authorization": f"Bearer {BEARER}"},
                json=health_call,
            )

        assert anonymous_get.status_code == 401
        assert "location" not in anonymous_get.headers
        assert authenticated_post.status_code == 200
        assert authenticated_post.json()["result"]["structuredContent"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_favicon_mcp_mount_is_never_replaced_by_public_svg(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(
            monkeypatch,
            MAIL_UI_SESSION_SECRET=SECRET,
            HTTP_PATH="/favicon.ico/",
        )
        health_call = {
            "jsonrpc": "2.0",
            "id": "favicon-mcp-health",
            "method": "tools/call",
            "params": {"name": "health_check", "arguments": {}},
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            anonymous_get = await client.get("/favicon.ico")
            authenticated_post = await client.post(
                "/favicon.ico",
                headers={"Authorization": f"Bearer {BEARER}"},
                json=health_call,
            )

        assert anonymous_get.status_code == 401
        assert anonymous_get.json() == {"detail": "Unauthorized"}
        assert authenticated_post.status_code == 200
        assert authenticated_post.json()["result"]["structuredContent"]["status"] == "ok"

    @pytest.mark.parametrize(
        "relative_path",
        [
            "ui/index.html",
            "src/mcp_agent_mail/templates/base.html",
            "src/mcp_agent_mail/templates/mail_login.html",
            "web/index.html",
        ],
    )
    def test_every_html_source_links_the_public_favicon(self, relative_path: str):
        repository_root = Path(__file__).resolve().parents[1]
        source = (repository_root / relative_path).read_text(encoding="utf-8")

        assert source.count(FAVICON_LINK) == 1


async def _make_user(
    username: str = "operator",
    *,
    role: str = webauth.ROLE_ADMIN,
    password: str = "irrelevant-here",
) -> int:
    """Insert a UiUser and return its session_epoch."""
    from mcp_agent_mail.models import UiUser

    await ensure_schema()
    async with get_session() as session:
        user = UiUser(
            username=username,
            password_hash=webauth.hash_password(password),
            role=role,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return int(user.session_epoch)


async def _bump_epoch(username: str = "operator") -> None:
    """Do what a password change or a disable does: move the epoch on."""
    from sqlmodel import select

    from mcp_agent_mail.models import UiUser

    async with get_session() as session:
        row = (await session.execute(select(UiUser).where(UiUser.username == username))).scalars().first()
        assert row is not None
        row.session_epoch += 1
        session.add(row)
        await session.commit()


async def _user_id(username: str) -> int:
    """Return the database id for a test UI user."""
    async with get_session() as session:
        result = await session.execute(
            text("SELECT id FROM ui_users WHERE username = :username"),
            {"username": username},
        )
        return int(result.scalar_one())


async def _user_auth_state(username: str) -> tuple[str, int, str, bool]:
    """Return the stored hash and all cookie-bound account facts."""
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT password_hash, session_epoch, session_generation, disabled "
                "FROM ui_users WHERE username = :username"
            ),
            {"username": username},
        )
        row = result.one()
    return str(row[0]), int(row[1]), str(row[2]), bool(row[3])


async def _seed_project(
    slug: str,
    *,
    subject: str,
    agent_name: str,
    sound: str,
) -> tuple[int, int]:
    """Create one project, sender, and incoming message for RBAC tests."""
    await ensure_schema()
    async with get_session() as session:
        project_result = await session.execute(
            text(
                "INSERT INTO projects (slug, human_key, created_at) "
                "VALUES (:slug, :human_key, datetime('now')) RETURNING id"
            ),
            {"slug": slug, "human_key": f"/{slug}"},
        )
        project_id = int(project_result.scalar_one())
        agent_result = await session.execute(
            text(
                "INSERT INTO agents (project_id, name, program, model, task_description, "
                "inception_ts, last_active_ts, attachments_policy, contact_policy, notify_sound) "
                "VALUES (:project_id, :name, 'test', 'test', 'rbac', datetime('now'), "
                "datetime('now'), 'auto', 'open', :sound) RETURNING id"
            ),
            {"project_id": project_id, "name": agent_name, "sound": sound},
        )
        agent_id = int(agent_result.scalar_one())
        message_result = await session.execute(
            text(
                "INSERT INTO messages (project_id, sender_id, subject, body_md, importance, "
                "created_ts, ack_required) VALUES (:project_id, :sender_id, :subject, :body, "
                "'normal', datetime('now'), 0) RETURNING id"
            ),
            {
                "project_id": project_id,
                "sender_id": agent_id,
                "subject": subject,
                "body": f"Body for {subject}",
            },
        )
        message_id = int(message_result.scalar_one())
        await session.commit()
        return project_id, message_id


async def _assign(username: str, project_id: int, role: str) -> None:
    """Grant one explicit project role to a test member."""
    user_id = await _user_id(username)
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO ui_project_assignments "
                "(user_id, project_id, role, created_ts, updated_ts) "
                "VALUES (:user_id, :project_id, :role, datetime('now'), datetime('now'))"
            ),
            {
                "user_id": user_id,
                "project_id": project_id,
                "role": role,
            },
        )
        await session.commit()


async def _cookie(username: str, epoch: int) -> dict[str, str]:
    async with get_session() as session:
        result = await session.execute(
            text("SELECT session_generation FROM ui_users WHERE username = :username"),
            {"username": username},
        )
        generation = str(result.scalar_one())
    return {
        "agent_mail_session": webauth.make_session(
            username,
            epoch=epoch,
            generation=generation,
            now=time.time(),
            secret=SECRET.encode("utf-8"),
        )
    }


def _install_react_dist(monkeypatch, tmp_path: Path) -> Path:
    """Point the server at a minimal, production-shaped Vite build tree."""
    dist_root = tmp_path / "ui_dist"
    assets_root = dist_root / "assets"
    assets_root.mkdir(parents=True)
    (dist_root / "index.html").write_text(
        "<!doctype html><title>Hermes React shell marker</title>"
        '<script type="module" src="/mail/assets/index-test.js"></script>'
        '<link rel="stylesheet" href="/mail/assets/index-test.css">',
        encoding="utf-8",
    )
    (assets_root / "index-test.js").write_text(
        'document.documentElement.dataset.shell = "react";\n',
        encoding="utf-8",
    )
    (assets_root / "index-test.css").write_text(
        "html { color-scheme: dark; }\n",
        encoding="utf-8",
    )
    (assets_root / "legacy.js").write_text(
        "window.Alpine = { start() {} };\n",
        encoding="utf-8",
    )
    (assets_root / "legacy.css").write_bytes(b"[x-cloak] { display: none !important; }\n")
    monkeypatch.setattr(http_module, "_mail_react_dist_root", lambda: dist_root)
    return dist_root


class TestMailUiSession:
    """The branches that need a real user, including the one that revokes them.

    Written after `home-win-1` mapped this middleware and pointed out that a
    signed cookie is not sufficient on its own: the row is re-read on every
    request and its `session_epoch` must still match. That check is the whole
    revocation mechanism — there is no server-side session table — so a change
    that dropped it would leave every already-issued cookie valid until it
    expired, and nothing would have failed.
    """

    @pytest.mark.asyncio
    async def test_a_live_session_is_let_through(self, isolated_env, monkeypatch):
        """The positive control the rest of this class depends on.

        Without it, every "refused" below is equally consistent with sessions
        never working at all — which is the failure mode that would make the
        revocation test pass for entirely the wrong reason.
        """
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("operator", epoch),
        ) as client:
            response = await client.get(GUARDED)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_bumping_the_epoch_revokes_a_live_cookie(self, isolated_env, monkeypatch):
        """A password change or a disable must kill sessions already issued.

        The cookie here is the same bytes that just worked: correctly signed,
        well inside its TTL, naming a user who still exists and is not disabled.
        The only thing that moved is the stored epoch — which is exactly what
        happens on the server side when someone's access is withdrawn.
        """
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user()
        cookies = await _cookie("operator", epoch)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
        ) as client:
            before = await client.get(GUARDED)
            await _bump_epoch()
            after = await client.get(GUARDED)

        assert before.status_code == 200
        assert after.status_code == 401

    @pytest.mark.asyncio
    async def test_recreated_username_rejects_cookie_from_previous_account_lifetime(
        self,
        isolated_env,
        monkeypatch,
    ):
        """A deleted and recreated username cannot inherit the old session."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("recreated-admin")
        old_cookie = await _cookie("recreated-admin", epoch)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=old_cookie,
        ) as client:
            before = await client.get(GUARDED)
            async with get_session() as session:
                await session.execute(
                    text("DELETE FROM ui_users WHERE username = :username"),
                    {"username": "recreated-admin"},
                )
                await session.commit()
            await _make_user("recreated-admin")
            after = await client.get(GUARDED)

        assert before.status_code == 200
        assert after.status_code == 401

    @pytest.mark.asyncio
    async def test_login_next_rejects_browser_normalized_backslash_redirect(
        self,
        isolated_env,
        monkeypatch,
    ):
        """A backslash redirect cannot escape the current origin in a browser."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("redirect-admin")

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("redirect-admin", epoch),
        ) as client:
            response = await client.get("/mail/login?next=/%5Cevil.example")

        assert response.status_code == 303
        assert response.headers["location"] == "/mail"
        assert response.headers["Cache-Control"] == "no-store, no-transform"
        assert response.headers["Content-Security-Policy"] == LEGACY_CSP
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Frame-Options"] == "DENY"

    @pytest.mark.asyncio
    async def test_a_member_may_read_aggregate_pages_but_not_administer(self, isolated_env, monkeypatch):
        """A member can open scoped aggregates but cannot perform admin actions."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("reader", role=webauth.ROLE_MEMBER)
        cookies = await _cookie("reader", epoch)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
        ) as client:
            read = await client.get(GUARDED)
            write = await client.post(
                GUARDED,
                headers={"Origin": "http://test", "Referer": "http://test/", "Host": "test"},
            )

        assert read.status_code == 200
        assert write.status_code == 403
        assert "admin" in write.json()["detail"]


class TestMailUiLogout:
    """Logout is an explicit same-origin POST, never a stored-image gadget."""

    @pytest.mark.asyncio
    async def test_get_and_head_are_method_denied_without_clearing_cookie(
        self,
        isolated_env,
        monkeypatch,
    ):
        settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("logout-method-admin")

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("logout-method-admin", epoch),
        ) as client:
            responses = [
                await client.get("/mail/logout"),
                await client.head("/mail/logout"),
            ]

        for response in responses:
            assert response.status_code == 405
            assert response.headers["Allow"] == "POST"
            assert response.headers["Cache-Control"] == "no-store, no-transform"
            assert response.headers["Content-Security-Policy"] == LEGACY_CSP
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert "set-cookie" not in response.headers
        assert settings.mail_ui.cookie_name not in responses[0].cookies

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param({}, id="missing-origin-and-referer"),
            pytest.param(
                {
                    "Origin": "https://evil.example",
                    "Referer": "https://evil.example/",
                    "Host": "test",
                },
                id="foreign-origin",
            ),
            pytest.param(
                {"Origin": "https://test", "Host": "test"},
                id="cross-scheme-origin",
            ),
            pytest.param(
                {"Origin": "not-a-url://test", "Host": "test"},
                id="invalid-origin-scheme",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_rejected_post_never_clears_cookie(
        self,
        isolated_env,
        monkeypatch,
        headers: dict[str, str],
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/mail/logout", headers=headers)

        assert response.status_code == 403
        assert response.json() == {"detail": "Cross-origin request rejected"}
        assert response.headers["Cache-Control"] == "no-store, no-transform"
        assert response.headers["Content-Security-Policy"] == LEGACY_CSP
        assert "set-cookie" not in response.headers

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param(
                {"Origin": "http://test", "Host": "test"},
                id="origin",
            ),
            pytest.param(
                {"Referer": "http://test/mail", "Host": "test"},
                id="referer-fallback",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_same_origin_post_clears_cookie_and_redirects(
        self,
        isolated_env,
        monkeypatch,
        headers: dict[str, str],
    ):
        settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/mail/logout", headers=headers)

        assert response.status_code == 303
        assert response.headers["Location"] == "/mail/login"
        assert response.headers["Cache-Control"] == "no-store, no-transform"
        assert response.headers["Content-Security-Policy"] == LEGACY_CSP
        set_cookie = response.headers["set-cookie"].lower()
        assert settings.mail_ui.cookie_name in set_cookie
        assert "max-age=0" in set_cookie
        assert "path=/mail" in set_cookie


class TestMailInlineImagePolicy:
    """Stored Markdown cannot initiate requests; only bounded raster bytes survive."""

    @staticmethod
    def _inline(mime: str, raw: bytes) -> str:
        return f"data:image/{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    @pytest.mark.parametrize(
        ("mime", "raw"),
        [
            pytest.param("png", b"\x89PNG\r\n\x1a\nrest", id="png"),
            pytest.param("jpeg", b"\xff\xd8\xffrest", id="jpeg"),
            pytest.param("gif", b"GIF87arest", id="gif87a"),
            pytest.param("gif", b"GIF89arest", id="gif89a"),
            pytest.param("webp", b"RIFFxxxxWEBPrest", id="webp"),
        ],
    )
    def test_accepts_canonical_bounded_raster_bytes(self, mime: str, raw: bytes):
        assert http_module._mail_ui_inline_image_source_allowed(self._inline(mime, raw)) is True

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("", id="empty"),
            pytest.param("/mail/logout", id="same-origin"),
            pytest.param("relative.png", id="relative"),
            pytest.param("blob:http://test/id", id="blob"),
            pytest.param("https://tracker.invalid/pixel.png", id="remote"),
            pytest.param("data:image/png;base64,%%%%", id="invalid-base64"),
            pytest.param(" data:image/png;base64,iVBORw0KGgo=", id="whitespace"),
            pytest.param("data:image/PNG;base64,iVBORw0KGgo=", id="uppercase-mime"),
            pytest.param("data:image/png;base64,R0lGODlh", id="mime-mismatch"),
            pytest.param("data:image/webp;base64,UklGRldFQlA=", id="short-webp"),
            pytest.param("data:image/webp;base64,UklGRnh4eHhOT1BFcmVzdA==", id="wrong-webp-marker"),
            pytest.param("data:image/gif;base64,R0lGODlheB==", id="noncanonical-base64"),
            pytest.param(
                "data:image/png;base64,"
                + "A" * (((http_module._MAIL_BODY_INLINE_IMAGE_MAX_BYTES + 2) // 3) * 4 + 1),
                id="oversized",
            ),
        ],
    )
    def test_rejects_unsafe_or_noncanonical_sources(self, source: str):
        assert http_module._mail_ui_inline_image_source_allowed(source) is False


class TestMailReactShell:
    """The React cutover has one shell URL and one contained asset namespace."""

    @pytest.mark.asyncio
    async def test_authenticated_trailing_slash_redirects_to_canonical_mail_and_keeps_query(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("react-redirect-admin")

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("react-redirect-admin", epoch),
        ) as client:
            responses = [
                await client.get("/mail/?tab=projects"),
                await client.head("/mail/?tab=projects"),
            ]

        for response in responses:
            assert response.status_code == 307
            assert response.headers["location"] == "/mail?tab=projects"
            assert response.headers["Cache-Control"] == "no-store, no-transform"
            assert response.headers["Content-Security-Policy"] == REACT_CSP
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.content == b""

    @pytest.mark.asyncio
    async def test_anonymous_navigation_redirects_to_login_with_exact_next(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/mail?tab=inbox&filter=high",
                headers={"Accept": "text/html"},
            )

        assert response.status_code == 303
        assert response.headers["location"] == (
            "/mail/login?next=%2Fmail%3Ftab%3Dinbox%26filter%3Dhigh"
        )
        assert response.headers["Cache-Control"] == "no-store, no-transform"
        assert response.headers["Content-Security-Policy"] == LEGACY_CSP
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"

    @pytest.mark.parametrize(
        ("username", "global_role", "project_role"),
        [
            pytest.param("react-admin", webauth.ROLE_ADMIN, None, id="admin"),
            pytest.param(
                "react-viewer",
                webauth.ROLE_MEMBER,
                webauth.PROJECT_ROLE_VIEWER,
                id="viewer",
            ),
            pytest.param(
                "react-operator",
                webauth.ROLE_MEMBER,
                webauth.PROJECT_ROLE_OPERATOR,
                id="operator",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_every_authenticated_human_role_can_open_the_shell(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
        username: str,
        global_role: str,
        project_role: str | None,
    ):
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user(username, role=global_role)
        if project_role is not None:
            project_id, _message_id = await _seed_project(
                f"{username}-project",
                subject="React role shell",
                agent_name=f"{username}-agent",
                sound="soft",
            )
            await _assign(username, project_id, project_role)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            response = await client.get("/mail")

        assert response.status_code == 200
        assert "Hermes React shell marker" in response.text
        assert "Project not found" not in response.text

    @pytest.mark.asyncio
    async def test_canonical_root_serves_non_cacheable_csp_protected_index(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("react-deep-admin")
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("react-deep-admin", epoch),
        ) as client:
            root = await client.get("/mail")
            root_head = await client.head("/mail")

        for response in (root, root_head):
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store, no-transform"
            assert response.headers["Content-Security-Policy"] == REACT_CSP
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.headers["Content-Type"].startswith("text/html")
        assert "Hermes React shell marker" in root.text
        assert root_head.content == b""

    @pytest.mark.asyncio
    async def test_login_is_public_and_uses_the_canonical_asset_namespace(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("legacy-airgap-admin")
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as anonymous_client:
            login = await anonymous_client.get("/mail/login")
            login_stylesheet = await anonymous_client.get(
                "/mail/assets/legacy.css"
            )
            login_stylesheet_head = await anonymous_client.head(
                "/mail/assets/legacy.css"
            )
            protected_runtime = await anonymous_client.get(
                "/mail/assets/legacy.js"
            )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("legacy-airgap-admin", epoch),
        ) as authenticated_client:
            inbox = await authenticated_client.get("/mail")

        assert login.status_code == 200
        assert login.headers["Cache-Control"] == "no-store, no-transform"
        assert login.headers["Content-Security-Policy"] == LEGACY_CSP
        assert login.headers["Referrer-Policy"] == "no-referrer"
        assert login.headers["X-Content-Type-Options"] == "nosniff"
        assert login.headers["X-Frame-Options"] == "DENY"
        assert 'href="/mail/assets/legacy.css"' in login.text
        assert "/mail/v2" not in login.text

        assert inbox.status_code == 200
        assert inbox.headers["Cache-Control"] == "no-store, no-transform"
        assert inbox.headers["Content-Security-Policy"] == REACT_CSP
        assert "Hermes React shell marker" in inbox.text

        assert login_stylesheet.status_code == 200
        assert login_stylesheet_head.status_code == 200
        for response in (login_stylesheet, login_stylesheet_head):
            assert response.headers["Cache-Control"] == "no-cache, no-transform"
            assert response.headers["Content-Type"].startswith("text/css")
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert login_stylesheet.text == "[x-cloak] { display: none !important; }\n"
        assert login_stylesheet_head.content == b""
        assert protected_runtime.status_code == 401

    @pytest.mark.asyncio
    async def test_assets_are_typed_immutable_and_cannot_escape_assets_root(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("react-assets-admin")

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("react-assets-admin", epoch),
        ) as client:
            javascript = await client.get("/mail/assets/index-test.js")
            stylesheet = await client.get("/mail/assets/index-test.css")
            legacy_javascript = await client.get("/mail/assets/legacy.js")
            aliased_legacy_javascript = await client.get(
                "/mail/assets/%2e%2flegacy.js"
            )
            legacy_stylesheet = await client.get("/mail/assets/legacy.css")
            missing = await client.get("/mail/assets/not-built.js")
            bare_namespace = await client.get("/mail/assets")
            encoded_namespace = await client.get("/mail/%61ssets")
            directory = await client.get("/mail/assets/")
            traversal = await client.get(
                "/mail/assets/%2e%2e%2findex.html",
            )

        immutable = "public, max-age=31536000, immutable, no-transform"
        assert javascript.status_code == 200
        assert javascript.headers["Cache-Control"] == immutable
        assert javascript.headers["Content-Type"].split(";", 1)[0] in {
            "application/javascript",
            "text/javascript",
        }
        assert stylesheet.status_code == 200
        assert stylesheet.headers["Cache-Control"] == immutable
        assert stylesheet.headers["Content-Type"].startswith("text/css")
        assert legacy_javascript.status_code == 404
        assert aliased_legacy_javascript.status_code == 404
        assert legacy_stylesheet.status_code == 200
        assert legacy_stylesheet.headers["Cache-Control"] == "no-cache, no-transform"
        assert legacy_stylesheet.headers["X-Content-Type-Options"] == "nosniff"
        assert missing.status_code == 404
        for response in (bare_namespace, encoded_namespace, directory):
            assert response.status_code == 404
            assert "Hermes React shell marker" not in response.text
        assert traversal.status_code == 404
        assert "Hermes React shell marker" not in traversal.text

    @pytest.mark.asyncio
    async def test_asset_symlinks_cannot_escape_the_build_tree(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        dist_root = _install_react_dist(monkeypatch, tmp_path)
        outside_file = tmp_path / "outside-secret.js"
        outside_file.write_text("outside-file-secret", encoding="utf-8")
        outside_directory = tmp_path / "outside-assets"
        outside_directory.mkdir()
        (outside_directory / "secret.js").write_text(
            "outside-directory-secret",
            encoding="utf-8",
        )
        try:
            (dist_root / "assets" / "outside-file.js").symlink_to(outside_file)
            (dist_root / "assets" / "outside-directory").symlink_to(
                outside_directory,
                target_is_directory=True,
            )
        except OSError as exc:
            pytest.skip(f"This platform cannot create test symlinks: {exc}")

        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("react-symlink-admin")
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("react-symlink-admin", epoch),
        ) as client:
            file_escape = await client.get("/mail/assets/outside-file.js")
            directory_escape = await client.get(
                "/mail/assets/outside-directory/secret.js",
            )

        for response in (file_escape, directory_escape):
            assert response.status_code == 404
            assert "outside-file-secret" not in response.text
            assert "outside-directory-secret" not in response.text

    @pytest.mark.asyncio
    async def test_missing_build_is_explicit_503_for_root_deep_and_assets(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        missing_dist = tmp_path / "missing-ui-dist"
        monkeypatch.setattr(http_module, "_mail_react_dist_root", lambda: missing_dist)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("react-missing-admin")

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("react-missing-admin", epoch),
        ) as client:
            root = await client.get("/mail")
            asset = await client.get("/mail/assets/index-missing.js")
            trailing_slash = await client.get("/mail/")
            retired = await client.get("/mail/v2")

        for response in (root, asset):
            assert response.status_code == 503
            assert response.json() == {"detail": "React Mail UI build is unavailable."}
        assert "Project not found" not in root.text
        assert trailing_slash.status_code == 307
        assert trailing_slash.headers["location"] == "/mail"
        assert retired.status_code == 404
        assert "React Mail UI build is unavailable." not in retired.text

    @pytest.mark.asyncio
    async def test_versioned_and_legacy_html_routes_are_retired_with_404(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, message_id = await _seed_project(
            "react-cutover-project",
            subject="Legacy route sentinel",
            agent_name="CutoverAgent",
            sound="soft",
        )
        epoch = await _make_user("react-cutover-admin")
        legacy_get_paths = [
            "/mail/v2",
            "/mail/v2/",
            "/mail/v2/settings",
            "/mail/v2/assets/index-test.js",
            "/mail/projects",
            "/mail/unified-inbox",
            "/mail/react-cutover-project",
            "/mail/react-cutover-project/inbox/CutoverAgent",
            f"/mail/react-cutover-project/message/{message_id}",
            f"/mail/react-cutover-project/thread/{message_id}",
            "/mail/react-cutover-project/search?q=sentinel",
            "/mail/react-cutover-project/file_reservations",
            "/mail/react-cutover-project/attachments",
            "/mail/react-cutover-project/overseer/compose",
            "/mail/archive/guide",
            "/mail/archive/activity",
            "/mail/archive/commit/deadbeef",
            "/mail/archive/timeline",
            "/mail/archive/browser",
            "/mail/archive/browser/react-cutover-project/file?path=index.html",
            "/mail/archive/browser/react-cutover-project/download?path=index.html",
            "/mail/archive/network",
            "/mail/archive/time-travel",
            "/mail/archive/time-travel/snapshot",
            "/mail/api/unified-inbox",
            "/mail/api/locks",
            "/mail/api/projects/react-cutover-project/agents",
        ]
        legacy_post_paths = [
            "/mail/api/delete-messages",
            "/mail/api/retire-agent",
            "/mail/api/unretire-agent",
            "/mail/api/archive-project",
            "/mail/api/unarchive-project",
            f"/mail/api/projects/{project_id}/siblings/{project_id}",
            "/mail/react-cutover-project/inbox/CutoverAgent/mark-read",
            "/mail/react-cutover-project/inbox/CutoverAgent/mark-all-read",
            "/mail/react-cutover-project/inbox/CutoverAgent/delete-messages",
            "/mail/react-cutover-project/overseer/send",
            "/mail/react-cutover-project/overseer/reply",
        ]

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("react-cutover-admin", epoch),
        ) as client:
            get_responses = [await client.get(path) for path in legacy_get_paths]
            post_responses = [
                await client.post(path, headers=SAME_ORIGIN_HEADERS)
                for path in legacy_post_paths
            ]

        for path, response in zip(
            [*legacy_get_paths, *legacy_post_paths],
            [*get_responses, *post_responses],
            strict=True,
        ):
            assert response.status_code == 404, path
            assert "Hermes React shell marker" not in response.text, path

    @pytest.mark.asyncio
    async def test_retired_namespaces_are_404_before_authentication(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        retired_paths = [
            "/mail/v2",
            "/mail/projects",
            "/mail/archive/guide",
            "/mail/api/unified-inbox",
        ]

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            anonymous = [await client.get(path) for path in retired_paths]
            service_bearer = [
                await client.get(
                    path,
                    headers={"Authorization": f"Bearer {BEARER}"},
                )
                for path in retired_paths
            ]

        for path, response in zip(
            [*retired_paths, *retired_paths],
            [*anonymous, *service_bearer],
            strict=True,
        ):
            assert response.status_code == 404, path
            assert response.json() == {"detail": "Not Found"}, path

    @pytest.mark.asyncio
    async def test_versioned_account_apis_are_never_captured_by_the_spa(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("react-api-member", role=webauth.ROLE_MEMBER)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("react-api-member", epoch),
        ) as client:
            preferences = await client.get("/mail/api/v1/me/preferences")
            password = await client.patch(
                "/mail/api/v1/me/password",
                json={},
                headers=SAME_ORIGIN_HEADERS,
            )

        assert preferences.status_code == 200
        assert preferences.headers["Content-Type"].startswith("application/json")
        assert "Hermes React shell marker" not in preferences.text
        assert password.status_code == 422
        assert password.headers["Content-Type"].startswith("application/json")
        assert "Hermes React shell marker" not in password.text


class TestMailUiPreferences:
    """Per-human locale state stays self-only, canonical, and migration-safe."""

    def test_custom_openapi_exposes_only_the_typed_v1_apis(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Codegen sees typed React schemas without publishing legacy routes."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)

        schema = app.openapi()
        mail_paths = {
            path: item
            for path, item in schema["paths"].items()
            if path == "/mail" or path.startswith("/mail/")
        }

        assert set(mail_paths) == {
            "/mail/api/v1/admin/access",
            "/mail/api/v1/admin/users/{target_user_id}/projects/{project_id}",
            "/mail/api/v1/inbox",
            "/mail/api/v1/me/password",
            "/mail/api/v1/me/preferences",
            "/mail/api/v1/me/profile",
            "/mail/api/v1/projects",
            "/mail/api/v1/deliveries/{delivery_id}",
            "/mail/api/v1/deliveries/{delivery_id}/retry",
            "/mail/api/v1/projects/{project_id}/messages",
            "/mail/api/v1/projects/{project_id}/messages/{message_id}",
            "/mail/api/v1/projects/{project_id}/messages/{message_id}/replies",
            "/mail/api/v1/projects/{project_id}/threads/{thread_id}",
        }
        assert schema["components"]["securitySchemes"]["MailUiSession"] == {
            "type": "apiKey",
            "in": "cookie",
            "name": _settings.mail_ui.cookie_name,
        }
        for path_item in mail_paths.values():
            for method, operation in path_item.items():
                if method in {"get", "post", "put", "patch", "delete"}:
                    assert operation["security"] == [{"MailUiSession": []}]
        operations = mail_paths["/mail/api/v1/me/preferences"]
        assert set(operations) == {"get", "patch"}
        assert operations["patch"]["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/MailUiPreferencesPatch"
        }
        for method in ("get", "patch"):
            assert operations[method]["responses"]["200"]["content"]["application/json"][
                "schema"
            ] == {"$ref": "#/components/schemas/MailUiPreferencesResponse"}
        assert schema["components"]["schemas"]["MailUiPreferencesPatch"][
            "additionalProperties"
        ] is False
        password_operations = mail_paths["/mail/api/v1/me/password"]
        assert set(password_operations) == {"patch"}
        assert password_operations["patch"]["requestBody"]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/MailUiPasswordPatch"}
        assert password_operations["patch"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiPasswordChangeResponse"}
        password_schema = schema["components"]["schemas"]["MailUiPasswordPatch"]
        assert password_schema["additionalProperties"] is False
        assert password_schema["properties"]["current_password"] == {
            "type": "string",
            "format": "password",
            "writeOnly": True,
            "minLength": 1,
            "maxLength": 1024,
            "title": "Current Password",
        }
        assert password_schema["properties"]["new_password"] == {
            "type": "string",
            "format": "password",
            "writeOnly": True,
            "minLength": 15,
            "maxLength": 1024,
            "title": "New Password",
        }

        profile_operations = mail_paths["/mail/api/v1/me/profile"]
        assert set(profile_operations) == {"get", "patch"}
        assert profile_operations["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiProfileResponse"}
        assert profile_operations["patch"]["requestBody"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiProfilePatch"}
        assert profile_operations["patch"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {
            "$ref": "#/components/schemas/MailUiProfileMutationResponse"
        }

        admin_access = mail_paths["/mail/api/v1/admin/access"]
        assert set(admin_access) == {"get"}
        assert admin_access["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiAdminAccessResponse"}
        assignment_operations = mail_paths[
            "/mail/api/v1/admin/users/{target_user_id}/projects/{project_id}"
        ]
        assert set(assignment_operations) == {"put"}
        assert assignment_operations["put"]["requestBody"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiAdminProjectAccessPut"}
        assert assignment_operations["put"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {
            "$ref": "#/components/schemas/MailUiAdminProjectAccessResponse"
        }
        assert assignment_operations["put"]["responses"]["409"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiDomainErrorResponse"}
        assert assignment_operations["put"]["responses"]["422"]["content"][
            "application/json"
        ]["schema"] == {
            "$ref": "#/components/schemas/MailUiDomainOrValidationErrorResponse"
        }
        assert profile_operations["patch"]["responses"]["422"]["content"][
            "application/json"
        ]["schema"] == {
            "$ref": "#/components/schemas/MailUiDomainOrValidationErrorResponse"
        }

        profile_schema = schema["components"]["schemas"]["MailUiProfileResponse"]
        assert set(profile_schema["properties"]) == {
            "id",
            "username",
            "display_name",
            "global_role",
            "profile_revision",
        }
        admin_user_schema = schema["components"]["schemas"]["MailUiAdminUserSummary"]
        assert set(admin_user_schema["properties"]) == {
            "id",
            "username",
            "display_name",
            "disabled",
            "global_role",
            "account_generation",
            "access_version",
            "assignments",
        }
        assert not set(admin_user_schema["properties"]) & {
            "password_hash",
            "session_generation",
            "last_login_ts",
        }

        typed_get_responses = {
            "/mail/api/v1/projects": "MailUiProjectsResponse",
            "/mail/api/v1/inbox": "MailUiInboxResponse",
            "/mail/api/v1/projects/{project_id}/messages/{message_id}": (
                "MailUiMessageDetail"
            ),
            "/mail/api/v1/projects/{project_id}/threads/{thread_id}": (
                "MailUiThreadResponse"
            ),
        }
        for path, model_name in typed_get_responses.items():
            assert set(mail_paths[path]) == {"get"}
            assert mail_paths[path]["get"]["responses"]["200"]["content"][
                "application/json"
            ]["schema"] == {"$ref": f"#/components/schemas/{model_name}"}

        delivery_status = mail_paths["/mail/api/v1/deliveries/{delivery_id}"]
        assert set(delivery_status) == {"get"}
        assert delivery_status["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiDeliveryResponse"}
        for status_code in ("401", "403", "404", "409", "500"):
            assert delivery_status["get"]["responses"][status_code]["content"][
                "application/json"
            ]["schema"] == {
                "$ref": "#/components/schemas/MailUiDeliveryErrorResponse"
            }
        assert delivery_status["get"]["responses"]["422"]["content"][
            "application/json"
        ]["schema"] == {
            "$ref": "#/components/schemas/MailUiDeliveryOrValidationErrorResponse"
        }
        delivery_retry = mail_paths[
            "/mail/api/v1/deliveries/{delivery_id}/retry"
        ]
        assert set(delivery_retry) == {"post"}
        assert delivery_retry["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiDeliveryResponse"}
        assert delivery_retry["post"]["responses"]["422"]["content"][
            "application/json"
        ]["schema"] == {
            "$ref": "#/components/schemas/MailUiDeliveryOrValidationErrorResponse"
        }
        compose_operation = mail_paths[
            "/mail/api/v1/projects/{project_id}/messages"
        ]
        assert set(compose_operation) == {"post"}
        assert compose_operation["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiComposeRequest"}
        assert compose_operation["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiDeliveryResponse"}
        assert compose_operation["post"]["responses"]["422"]["content"][
            "application/json"
        ]["schema"] == {
            "$ref": "#/components/schemas/MailUiDeliveryOrValidationErrorResponse"
        }
        reply_operation = mail_paths[
            "/mail/api/v1/projects/{project_id}/messages/{message_id}/replies"
        ]
        assert set(reply_operation) == {"post"}
        assert reply_operation["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiReplyRequest"}
        assert reply_operation["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/MailUiDeliveryResponse"}
        assert reply_operation["post"]["responses"]["422"]["content"][
            "application/json"
        ]["schema"] == {
            "$ref": "#/components/schemas/MailUiDeliveryOrValidationErrorResponse"
        }
        for request_schema_name in ("MailUiComposeRequest", "MailUiReplyRequest"):
            assert schema["components"]["schemas"][request_schema_name][
                "additionalProperties"
            ] is False

        summary_properties = set(
            schema["components"]["schemas"]["MailUiMessageSummary"]["properties"]
        )
        assert summary_properties == {
            "id",
            "project_id",
            "project_slug",
            "subject",
            "sender",
            "sender_name",
            "sender_display_name",
            "importance",
            "ack_required",
            "thread_id",
            "reply_to",
            "created_ts",
            "can_reply",
        }
        assert not summary_properties & {
            "body_md",
            "to",
            "cc",
            "bcc",
            "recipients",
            "read",
            "read_ts",
        }
        assert schema["components"]["schemas"]["MailUiMessageSummary"][
            "properties"
        ]["importance"]["enum"] == ["low", "normal", "high", "urgent"]
        detail_properties = set(
            schema["components"]["schemas"]["MailUiMessageDetail"]["properties"]
        )
        assert detail_properties == summary_properties | {
            "body_md",
            "to",
            "cc",
            "attachments",
        }
        assert "bcc" not in detail_properties
        assert set(
            schema["components"]["schemas"]["MailUiAttachmentMetadata"]["properties"]
        ) == {"type", "media_type", "size_bytes"}
        assert "/mail/api/unified-inbox" not in schema["paths"]

    @pytest.mark.asyncio
    async def test_schema_defaults_and_locale_guards_are_idempotent(
        self,
        isolated_env,
    ):
        """Raw inserts get the DB default and rerunning startup DDL remains safe."""
        await ensure_schema()
        engine = db_module.get_engine()
        async with engine.begin() as connection:
            await connection.run_sync(db_module._setup_fts)
            await connection.run_sync(db_module._setup_fts)

        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO ui_users "
                    "(username, password_hash, role, disabled, session_epoch, "
                    "session_generation, created_ts) "
                    "VALUES ('raw-defaults', 'unused', 'member', 0, 1, "
                    ":session_generation, datetime('now'))"
                ),
                {"session_generation": "a" * 64},
            )
            await session.commit()
            row = (
                await session.execute(
                    text(
                        "SELECT preferred_ui_locale, preferred_correspondence_locale "
                        "FROM ui_users WHERE username = 'raw-defaults'"
                    )
                )
            ).one()
            columns = {
                str(column[1]): column
                for column in (
                    await session.execute(text("PRAGMA table_info(ui_users)"))
                ).fetchall()
            }

        assert tuple(row) == ("en", None)
        assert int(columns["preferred_ui_locale"][3]) == 1
        assert str(columns["preferred_ui_locale"][4]).strip("'") == "en"
        assert int(columns["preferred_correspondence_locale"][3]) == 0

        async with get_session() as session:
            with pytest.raises(IntegrityError, match="invalid preferred_ui_locale"):
                await session.execute(
                    text(
                        "UPDATE ui_users SET preferred_ui_locale = 'fr' "
                        "WHERE username = 'raw-defaults'"
                    )
                )

    @pytest.mark.asyncio
    async def test_get_returns_stored_and_effective_defaults(self, isolated_env, monkeypatch):
        """A member sees only their account defaults and correspondence inherits UI."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("preferences-member", role=webauth.ROLE_MEMBER)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("preferences-member", epoch),
        ) as client:
            response = await client.get("/mail/api/v1/me/preferences")

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {
            "stored": {
                "preferred_ui_locale": "en",
                "preferred_correspondence_locale": None,
            },
            "effective": {
                "ui_locale": "en",
                "correspondence_locale": "en",
            },
        }

    @pytest.mark.asyncio
    async def test_patch_is_partial_canonicalized_and_self_only(self, isolated_env, monkeypatch):
        """One member can update only their row; null correspondence means inherit."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("preferences-owner", role=webauth.ROLE_MEMBER)
        await _make_user("preferences-other", role=webauth.ROLE_MEMBER)
        headers = {
            "Origin": "http://test",
            "Referer": "http://test/",
            "Host": "test",
        }

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("preferences-owner", epoch),
        ) as client:
            both = await client.patch(
                "/mail/api/v1/me/preferences",
                json={
                    "preferred_ui_locale": " PL ",
                    "preferred_correspondence_locale": " EN ",
                },
                headers=headers,
            )
            inherited = await client.patch(
                "/mail/api/v1/me/preferences",
                json={"preferred_correspondence_locale": None},
                headers=headers,
            )

        assert both.status_code == 200
        assert both.headers["Cache-Control"] == "no-store"
        assert inherited.headers["Cache-Control"] == "no-store"
        assert both.json() == {
            "stored": {
                "preferred_ui_locale": "pl",
                "preferred_correspondence_locale": "en",
            },
            "effective": {
                "ui_locale": "pl",
                "correspondence_locale": "en",
            },
        }
        assert inherited.status_code == 200
        assert inherited.json() == {
            "stored": {
                "preferred_ui_locale": "pl",
                "preferred_correspondence_locale": None,
            },
            "effective": {
                "ui_locale": "pl",
                "correspondence_locale": "pl",
            },
        }

        async with get_session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT username, preferred_ui_locale, "
                        "preferred_correspondence_locale FROM ui_users "
                        "ORDER BY username"
                    )
                )
            ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("preferences-other", "en", None),
            ("preferences-owner", "pl", None),
        ]

    @pytest.mark.asyncio
    async def test_patch_accepts_https_origin_from_trusted_container_proxy(
        self,
        isolated_env,
        monkeypatch,
    ):
        """A trusted Docker-gateway proxy must restore HTTPS before the CSRF check."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("preferences-proxied", role=webauth.ROLE_MEMBER)
        default_proxy_app = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
        proxied_app = ProxyHeadersMiddleware(app, trusted_hosts="*")

        async with AsyncClient(
            transport=ASGITransport(
                app=cast(Any, default_proxy_app),
                client=("172.19.0.1", 43112),
            ),
            base_url="http://hermes.klatt.ie",
            cookies=await _cookie("preferences-proxied", epoch),
        ) as client:
            rejected = await client.patch(
                "/mail/api/v1/me/preferences",
                json={"preferred_ui_locale": "pl"},
                headers={
                    "Origin": "https://hermes.klatt.ie",
                    "Referer": "https://hermes.klatt.ie/mail/",
                    "X-Forwarded-Proto": "https",
                },
            )

        async with AsyncClient(
            transport=ASGITransport(
                app=cast(Any, proxied_app),
                client=("172.19.0.1", 43112),
            ),
            base_url="http://hermes.klatt.ie",
            cookies=await _cookie("preferences-proxied", epoch),
        ) as client:
            response = await client.patch(
                "/mail/api/v1/me/preferences",
                json={"preferred_ui_locale": "pl"},
                headers={
                    "Origin": "https://hermes.klatt.ie",
                    "Referer": "https://hermes.klatt.ie/mail/",
                    "X-Forwarded-Proto": "https",
                },
            )

        assert rejected.status_code == 403
        assert response.status_code == 200
        assert response.json()["stored"]["preferred_ui_locale"] == "pl"

    @pytest.mark.asyncio
    async def test_patch_rejects_null_ui_unknown_locale_and_extra_fields(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Invalid patches fail before touching the stored account."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("preferences-validation", role=webauth.ROLE_MEMBER)
        headers = {
            "Origin": "http://test",
            "Referer": "http://test/",
            "Host": "test",
        }

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("preferences-validation", epoch),
        ) as client:
            responses = [
                await client.patch(
                    "/mail/api/v1/me/preferences",
                    json=payload,
                    headers=headers,
                )
                for payload in (
                    {"preferred_ui_locale": None},
                    {"preferred_ui_locale": "fr"},
                    {"preferred_ui_locale": "pl", "username": "someone-else"},
                )
            ]
            unchanged = await client.get("/mail/api/v1/me/preferences")

        assert [response.status_code for response in responses] == [422, 422, 422]
        assert unchanged.json()["stored"] == {
            "preferred_ui_locale": "en",
            "preferred_correspondence_locale": None,
        }

    @pytest.mark.asyncio
    async def test_patch_rejects_cross_origin(self, isolated_env, monkeypatch):
        """The self-only write keeps the same-origin boundary used by every UI mutation."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("preferences-origin", role=webauth.ROLE_MEMBER)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("preferences-origin", epoch),
        ) as client:
            rejected = await client.patch(
                "/mail/api/v1/me/preferences",
                json={"preferred_ui_locale": "pl"},
                headers={
                    "Origin": "https://evil.example",
                    "Referer": "https://evil.example/",
                    "Host": "test",
                },
            )
            unchanged = await client.get("/mail/api/v1/me/preferences")

        assert rejected.status_code == 403
        assert rejected.headers["Cache-Control"] == "no-store"
        assert unchanged.json()["stored"]["preferred_ui_locale"] == "en"

    @pytest.mark.asyncio
    async def test_get_revalidates_epoch_after_middleware_authentication(
        self,
        isolated_env,
        monkeypatch,
    ):
        """An epoch bump in the middleware-to-handler gap invalidates the read."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("preferences-epoch-race", role=webauth.ROLE_MEMBER)
        original = http_module._mail_ui_preferences_user
        raced = False

        async def bump_epoch_before_read(request, session):
            nonlocal raced
            if not raced:
                raced = True
                async with get_session() as competing_session:
                    await competing_session.execute(
                        text(
                            "UPDATE ui_users SET session_epoch = session_epoch + 1 "
                            "WHERE username = 'preferences-epoch-race'"
                        )
                    )
                    await competing_session.commit()
            return await original(request, session)

        monkeypatch.setattr(http_module, "_mail_ui_preferences_user", bump_epoch_before_read)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("preferences-epoch-race", epoch),
        ) as client:
            response = await client.get("/mail/api/v1/me/preferences")

        assert raced is True
        assert response.status_code == 401
        assert "no longer current" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_patch_cas_cannot_modify_recreated_account_with_reused_primary_key(
        self,
        isolated_env,
        monkeypatch,
    ):
        """A replacement committed after revalidation is excluded by generation CAS."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "preferences-recreated-race"
        epoch = await _make_user(username, role=webauth.ROLE_MEMBER)
        user_id = await _user_id(username)
        original = http_module._mail_ui_preferences_cas_update
        replacement_generation = "b" * 64
        raced = False

        async def replace_before_cas(session, *, principal, values):
            nonlocal raced
            if not raced:
                raced = True
                async with get_session() as competing_session:
                    await competing_session.execute(
                        text("DELETE FROM ui_users WHERE id = :user_id"),
                        {"user_id": user_id},
                    )
                    await competing_session.execute(
                        text(
                            "INSERT INTO ui_users "
                            "(id, username, password_hash, role, disabled, session_epoch, "
                            "session_generation, preferred_ui_locale, "
                            "preferred_correspondence_locale, created_ts) "
                            "VALUES (:user_id, :username, 'replacement-password', 'member', "
                            "0, :epoch, :generation, 'en', NULL, datetime('now'))"
                        ),
                        {
                            "user_id": user_id,
                            "username": username,
                            "epoch": epoch,
                            "generation": replacement_generation,
                        },
                    )
                    await competing_session.commit()
            return await original(session, principal=principal, values=values)

        monkeypatch.setattr(
            http_module,
            "_mail_ui_preferences_cas_update",
            replace_before_cas,
        )
        headers = {
            "Origin": "http://test",
            "Referer": "http://test/",
            "Host": "test",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            response = await client.patch(
                "/mail/api/v1/me/preferences",
                json={"preferred_ui_locale": "pl"},
                headers=headers,
            )

        async with get_session() as session:
            replacement = (
                await session.execute(
                    text(
                        "SELECT session_generation, preferred_ui_locale "
                        "FROM ui_users WHERE id = :user_id"
                    ),
                    {"user_id": user_id},
                )
            ).one()

        assert raced is True
        assert response.status_code == 401
        assert tuple(replacement) == (replacement_generation, "en")

    @pytest.mark.asyncio
    async def test_patch_cas_rejects_epoch_bump_after_revalidation(
        self,
        isolated_env,
        monkeypatch,
    ):
        """An epoch bump immediately before CAS prevents the stale preference write."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "preferences-patch-epoch-race"
        epoch = await _make_user(username, role=webauth.ROLE_MEMBER)
        original = http_module._mail_ui_preferences_cas_update
        raced = False

        async def bump_epoch_before_cas(session, *, principal, values):
            nonlocal raced
            if not raced:
                raced = True
                async with get_session() as competing_session:
                    await competing_session.execute(
                        text(
                            "UPDATE ui_users SET session_epoch = session_epoch + 1 "
                            "WHERE username = :username"
                        ),
                        {"username": username},
                    )
                    await competing_session.commit()
            return await original(session, principal=principal, values=values)

        monkeypatch.setattr(
            http_module,
            "_mail_ui_preferences_cas_update",
            bump_epoch_before_cas,
        )
        headers = {
            "Origin": "http://test",
            "Referer": "http://test/",
            "Host": "test",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            response = await client.patch(
                "/mail/api/v1/me/preferences",
                json={"preferred_ui_locale": "pl"},
                headers=headers,
            )

        async with get_session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT session_epoch, preferred_ui_locale "
                        "FROM ui_users WHERE username = :username"
                    ),
                    {"username": username},
                )
            ).one()

        assert raced is True
        assert response.status_code == 401
        assert tuple(row) == (epoch + 1, "en")

    @pytest.mark.asyncio
    async def test_auth_disabled_mode_has_no_implicit_self(self, isolated_env, monkeypatch):
        """Development bearer access cannot be mistaken for a human preference owner."""
        _settings, app = _build(
            monkeypatch,
            MAIL_UI_AUTH_ENABLED="false",
            MAIL_UI_SESSION_SECRET="",
        )
        await ensure_schema()
        headers = {
            "Authorization": f"Bearer {BEARER}",
            "Origin": "http://test",
            "Referer": "http://test/",
            "Host": "test",
        }

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            read = await client.get(
                "/mail/api/v1/me/preferences",
                headers={"Authorization": f"Bearer {BEARER}"},
            )
            profile_read = await client.get(
                PROFILE_PATH,
                headers={"Authorization": f"Bearer {BEARER}"},
            )
            write = await client.patch(
                "/mail/api/v1/me/preferences",
                json={"preferred_ui_locale": "pl"},
                headers=headers,
            )
            password_write = await client.patch(
                PASSWORD_PATH,
                json={
                    "current_password": "irrelevant-here",
                    "new_password": "valid replacement password",
                },
                headers=headers,
            )
            profile_write = await client.patch(
                PROFILE_PATH,
                json={"display_name": "No owner", "expected_profile_revision": 1},
                headers=headers,
            )

        assert read.status_code == 401
        assert profile_read.status_code == 401
        assert profile_read.json() == {"detail": {"code": "actor_forbidden"}}
        assert write.status_code == 401
        assert password_write.status_code == 401
        assert profile_write.status_code == 401
        assert profile_write.json() == {"detail": {"code": "actor_forbidden"}}
        assert password_write.headers["Cache-Control"] == "no-store"
        assert profile_write.headers["Cache-Control"] == "no-store"


class TestMailUiProfile:
    """Display-name self service is normalized, CAS guarded, and self-only."""

    @pytest.mark.asyncio
    async def test_get_and_patch_normalize_without_revoking_the_session(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("profile-owner", role=webauth.ROLE_MEMBER)
        await _make_user("profile-other", role=webauth.ROLE_MEMBER)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("profile-owner", epoch),
        ) as client:
            before = await client.get(PROFILE_PATH)
            changed = await client.patch(
                PROFILE_PATH,
                json={
                    "display_name": "  Mateusz\t  Klatt  ",
                    "expected_profile_revision": 1,
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            unchanged = await client.patch(
                PROFILE_PATH,
                json={
                    "display_name": "Mateusz Klatt",
                    "expected_profile_revision": 2,
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            after = await client.get(PROFILE_PATH)

        assert before.status_code == 200
        assert before.json() == {
            "id": await _user_id("profile-owner"),
            "username": "profile-owner",
            "display_name": None,
            "global_role": "member",
            "profile_revision": 1,
        }
        assert changed.json() == {
            "changed": True,
            "display_name": "Mateusz Klatt",
            "profile_revision": 2,
        }
        assert unchanged.json() == {
            "changed": False,
            "display_name": "Mateusz Klatt",
            "profile_revision": 2,
        }
        assert after.json()["display_name"] == "Mateusz Klatt"
        assert all(
            response.headers["Cache-Control"] == "no-store"
            for response in (before, changed, unchanged, after)
        )
        async with get_session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT username, display_name, profile_revision, session_epoch "
                        "FROM ui_users ORDER BY username"
                    )
                )
            ).all()
        assert [tuple(row) for row in rows] == [
            ("profile-other", None, 1, 1),
            ("profile-owner", "Mateusz Klatt", 2, epoch),
        ]

    @pytest.mark.asyncio
    async def test_patch_returns_stable_conflict_and_validation_codes(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("profile-conflict", role=webauth.ROLE_MEMBER)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("profile-conflict", epoch),
        ) as client:
            first = await client.patch(
                PROFILE_PATH,
                json={"display_name": "Alicja", "expected_profile_revision": 1},
                headers=SAME_ORIGIN_HEADERS,
            )
            stale = await client.patch(
                PROFILE_PATH,
                json={"display_name": "Alice", "expected_profile_revision": 1},
                headers=SAME_ORIGIN_HEADERS,
            )
            invalid = await client.patch(
                PROFILE_PATH,
                json={"display_name": "invalid\u0000name", "expected_profile_revision": 2},
                headers=SAME_ORIGIN_HEADERS,
            )
            extra = await client.patch(
                PROFILE_PATH,
                json={
                    "display_name": "Alice",
                    "expected_profile_revision": 2,
                    "username": "someone-else",
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            cross_origin = await client.patch(
                PROFILE_PATH,
                json={"display_name": "Mallory", "expected_profile_revision": 2},
                headers={"Origin": "https://evil.example", "Host": "test"},
            )

        assert first.status_code == 200
        assert stale.status_code == 409
        assert stale.json() == {"detail": {"code": "profile_revision_conflict"}}
        assert invalid.status_code == 422
        assert invalid.json() == {"detail": {"code": "invalid_display_name"}}
        assert extra.status_code == 422
        assert cross_origin.status_code == 403
        assert cross_origin.json() == {"detail": {"code": "actor_forbidden"}}
        assert all(
            response.headers["Cache-Control"] == "no-store"
            for response in (stale, invalid, extra, cross_origin)
        )


class TestMailUiAdminAccess:
    """The admin matrix is least-privilege and drives atomic assignment CAS."""

    @pytest.mark.asyncio
    async def test_snapshot_and_assignment_lifecycle_are_typed_and_audited(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        admin_epoch = await _make_user("access-admin", role=webauth.ROLE_ADMIN)
        member_epoch = await _make_user("access-member", role=webauth.ROLE_MEMBER)
        project_id, _message_id = await _seed_project(
            "access-project",
            subject="Access target",
            agent_name="AccessAgent",
            sound="soft",
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("access-admin", admin_epoch),
        ) as client:
            snapshot = await client.get(ADMIN_ACCESS_PATH)
            payload = snapshot.json()
            member = next(item for item in payload["users"] if item["username"] == "access-member")
            project = next(item for item in payload["projects"] if item["id"] == project_id)
            assignment_path = (
                f"/mail/api/v1/admin/users/{member['id']}/projects/{project_id}"
            )
            grant = await client.put(
                assignment_path,
                json={
                    "role": "viewer",
                    "expected_access_version": member["access_version"],
                    "account_generation": member["account_generation"],
                    "expected_project_generation": project["project_generation"],
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            no_op = await client.put(
                assignment_path,
                json={
                    "role": "viewer",
                    "expected_access_version": grant.json()["access_version"],
                    "account_generation": member["account_generation"],
                    "expected_project_generation": project["project_generation"],
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            replace_role = await client.put(
                assignment_path,
                json={
                    "role": "operator",
                    "expected_access_version": no_op.json()["access_version"],
                    "account_generation": member["account_generation"],
                    "expected_project_generation": project["project_generation"],
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            revoke = await client.put(
                assignment_path,
                json={
                    "role": None,
                    "expected_access_version": replace_role.json()["access_version"],
                    "account_generation": member["account_generation"],
                    "expected_project_generation": project["project_generation"],
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        assert snapshot.status_code == 200
        assert snapshot.headers["Cache-Control"] == "no-store"
        assert set(payload) == {"users", "projects"}
        assert set(member) == {
            "id",
            "username",
            "display_name",
            "disabled",
            "global_role",
            "account_generation",
            "access_version",
            "assignments",
        }
        assert not set(member) & {"password_hash", "session_generation", "last_login_ts"}
        assert grant.json() == {
            "changed": True,
            "role": "viewer",
            "access_version": member_epoch + 1,
        }
        assert no_op.json() == {
            "changed": False,
            "role": "viewer",
            "access_version": member_epoch + 1,
        }
        assert replace_role.json() == {
            "changed": True,
            "role": "operator",
            "access_version": member_epoch + 2,
        }
        assert revoke.json() == {
            "changed": True,
            "role": None,
            "access_version": member_epoch + 3,
        }
        assert all(
            response.headers["Cache-Control"] == "no-store"
            for response in (grant, no_op, replace_role, revoke)
        )
        async with get_session() as session:
            assignment_count = int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM ui_project_assignments "
                            "WHERE user_id = :user_id AND project_id = :project_id"
                        ),
                        {"user_id": member["id"], "project_id": project_id},
                    )
                ).scalar_one()
            )
            audit_rows = (
                await session.execute(
                    text(
                        "SELECT actor_user_id, target_epoch_before, target_epoch_after, "
                        "old_role, new_role FROM ui_access_audit_events "
                        "WHERE target_user_id = :user_id ORDER BY id"
                    ),
                    {"user_id": member["id"]},
                )
            ).all()
        assert assignment_count == 0
        assert [tuple(row)[1:] for row in audit_rows] == [
            (member_epoch, member_epoch + 1, None, "viewer"),
            (member_epoch + 1, member_epoch + 2, "viewer", "operator"),
            (member_epoch + 2, member_epoch + 3, "operator", None),
        ]
        assert {int(row[0]) for row in audit_rows} == {await _user_id("access-admin")}

    @pytest.mark.asyncio
    async def test_member_auth_disabled_and_stale_cas_fail_closed(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        admin_epoch = await _make_user("access-errors-admin", role=webauth.ROLE_ADMIN)
        member_epoch = await _make_user("access-errors-member", role=webauth.ROLE_MEMBER)
        project_id, _message_id = await _seed_project(
            "access-errors-project",
            subject="Access errors target",
            agent_name="AccessErrorsAgent",
            sound="click",
        )
        member_cookies = await _cookie("access-errors-member", member_epoch)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=member_cookies,
        ) as member_client:
            member_get = await member_client.get(ADMIN_ACCESS_PATH)
            member_put = await member_client.put(
                f"/mail/api/v1/admin/users/{await _user_id('access-errors-member')}"
                f"/projects/{project_id}",
                json={},
                headers=SAME_ORIGIN_HEADERS,
            )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("access-errors-admin", admin_epoch),
        ) as admin_client:
            snapshot = (await admin_client.get(ADMIN_ACCESS_PATH)).json()
            target = next(
                item for item in snapshot["users"] if item["username"] == "access-errors-member"
            )
            project = next(item for item in snapshot["projects"] if item["id"] == project_id)
            stale = await admin_client.put(
                f"/mail/api/v1/admin/users/{target['id']}/projects/{project_id}",
                json={
                    "role": "viewer",
                    "expected_access_version": target["access_version"] + 1,
                    "account_generation": target["account_generation"],
                    "expected_project_generation": project["project_generation"],
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            injected_actor = await admin_client.put(
                f"/mail/api/v1/admin/users/{target['id']}/projects/{project_id}",
                json={
                    "role": "viewer",
                    "expected_access_version": target["access_version"],
                    "account_generation": target["account_generation"],
                    "expected_project_generation": project["project_generation"],
                    "actor_user_id": target["id"],
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            cross_origin = await admin_client.put(
                f"/mail/api/v1/admin/users/{target['id']}/projects/{project_id}",
                json={
                    "role": "viewer",
                    "expected_access_version": target["access_version"],
                    "account_generation": target["account_generation"],
                    "expected_project_generation": project["project_generation"],
                },
                headers={"Origin": "https://evil.example", "Host": "test"},
            )

        assert member_get.status_code == 403
        assert member_get.json() == {"detail": {"code": "actor_forbidden"}}
        assert member_put.status_code == 403
        assert member_put.json() == {"detail": {"code": "actor_forbidden"}}
        assert stale.status_code == 409
        assert stale.json() == {"detail": {"code": "access_version_conflict"}}
        assert injected_actor.status_code == 422
        assert cross_origin.status_code == 403
        assert cross_origin.json() == {"detail": {"code": "actor_forbidden"}}
        assert all(
            response.headers["Cache-Control"] == "no-store"
            for response in (member_get, member_put, stale, injected_actor, cross_origin)
        )

        _settings, auth_disabled_app = _build(
            monkeypatch,
            MAIL_UI_AUTH_ENABLED="false",
            MAIL_UI_SESSION_SECRET="",
        )
        async with AsyncClient(
            transport=ASGITransport(app=auth_disabled_app),
            base_url="http://test",
        ) as client:
            auth_disabled = await client.get(
                ADMIN_ACCESS_PATH,
                headers={"Authorization": f"Bearer {BEARER}"},
            )
        assert auth_disabled.status_code == 401
        assert auth_disabled.json() == {"detail": {"code": "actor_forbidden"}}
        assert auth_disabled.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_snapshot_revalidates_admin_after_middleware_race(
        self,
        isolated_env,
        monkeypatch,
    ):
        """A concurrent demotion cannot reuse a stale middleware admin claim."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        admin_epoch = await _make_user("snapshot-race-admin", role=webauth.ROLE_ADMIN)
        admin_id = await _user_id("snapshot-race-admin")
        await _seed_project(
            "snapshot-race-secret",
            subject="Must not leak",
            agent_name="SnapshotRaceAgent",
            sound="high",
        )

        original_revalidate = http_module._mail_ui_preferences_user
        raced = False

        async def demote_before_revalidation(request, session):
            nonlocal raced
            if not raced:
                raced = True
                async with get_session() as writer:
                    await writer.execute(
                        text("UPDATE ui_users SET role = 'member' WHERE id = :user_id"),
                        {"user_id": admin_id},
                    )
                    await writer.commit()
            return await original_revalidate(request, session)

        monkeypatch.setattr(
            http_module,
            "_mail_ui_preferences_user",
            demote_before_revalidation,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("snapshot-race-admin", admin_epoch),
        ) as client:
            response = await client.get(ADMIN_ACCESS_PATH)

        assert raced is True
        assert response.status_code == 403
        assert response.json() == {"detail": {"code": "actor_forbidden"}}
        assert response.headers["Cache-Control"] == "no-store"
        assert "snapshot-race-secret" not in response.text


class TestMailUiPasswordChange:
    """Password rotation is self-only, revoking, bounded, and race-safe."""

    @pytest.fixture(autouse=True)
    def _clear_password_change_attempts(self):
        http_module._password_change_attempts.clear()
        try:
            yield
        finally:
            http_module._password_change_attempts.clear()

    @pytest.mark.asyncio
    async def test_anonymous_request_is_401_and_non_cacheable(
        self,
        isolated_env,
        monkeypatch,
    ):
        """The typed route never exposes validation details before authentication."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.patch(
                PASSWORD_PATH,
                json={
                    "current_password": "anonymous current secret",
                    "new_password": "anonymous replacement secret",
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store"
        assert "anonymous" not in response.text

    @pytest.mark.asyncio
    async def test_success_rotates_hash_revokes_other_session_and_refreshes_caller(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Only the requesting browser receives a cookie for the incremented epoch."""
        settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-member"
        current_password = "current password with spaces"
        new_password = "nowe hasło 🔐 with spaces"
        epoch = await _make_user(
            username,
            role=webauth.ROLE_MEMBER,
            password=current_password,
        )
        original_cookie = await _cookie(username, epoch)

        async with (
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                cookies=original_cookie,
            ) as caller,
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                cookies=original_cookie,
            ) as other_session,
        ):
            changed = await caller.patch(
                PASSWORD_PATH,
                json={
                    "current_password": current_password,
                    "new_password": new_password,
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            # `_cookie()` supplies a bare test-client cookie (Path=/), whereas
            # browsers obtain the real cookie from login with Path=/mail. Drop
            # that test-only root cookie so the refreshed /mail cookie is the
            # sole value, exactly as it is in production.
            caller.cookies.clear()
            caller.cookies.set(
                settings.mail_ui.cookie_name,
                changed.cookies[settings.mail_ui.cookie_name],
                path="/mail",
            )
            refreshed = await caller.get("/mail/api/v1/me/preferences")
            stale = await other_session.patch(
                PASSWORD_PATH,
                json={
                    "current_password": current_password,
                    "new_password": "another valid replacement password",
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        stored_hash, stored_epoch, _generation, disabled = await _user_auth_state(username)
        assert changed.status_code == 200
        assert changed.json() == {"changed": True}
        assert changed.headers["Cache-Control"] == "no-store"
        set_cookie = changed.headers["set-cookie"].lower()
        assert f"max-age={settings.mail_ui.session_ttl_seconds}" in set_cookie
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        assert "path=/mail" in set_cookie
        assert ("secure" in set_cookie) is settings.mail_ui.cookie_secure
        assert stored_epoch == epoch + 1
        assert disabled is False
        assert webauth.verify_password(new_password, stored_hash) is True
        assert webauth.verify_password(current_password, stored_hash) is False
        assert refreshed.status_code == 200
        assert refreshed.headers["Cache-Control"] == "no-store"
        assert stale.status_code == 401
        assert stale.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_wrong_current_password_is_400_and_keeps_state_and_cookie(
        self,
        isolated_env,
        monkeypatch,
    ):
        """A wrong current secret neither mutates the account nor signs a cookie."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-wrong-current"
        current_password = "the actual current password"
        wrong_password = "wrong secret must stay private"
        epoch = await _make_user(username, password=current_password)
        before = await _user_auth_state(username)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            response = await client.patch(
                PASSWORD_PATH,
                json={
                    "current_password": wrong_password,
                    "new_password": "a valid replacement password",
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            still_live = await client.get("/mail/api/v1/me/preferences")

        assert response.status_code == 400
        assert response.json() == {"detail": "Current password is incorrect."}
        assert wrong_password not in response.text
        assert "set-cookie" not in response.headers
        assert response.headers["Cache-Control"] == "no-store"
        assert still_live.status_code == 200
        assert await _user_auth_state(username) == before

    @pytest.mark.asyncio
    async def test_cross_origin_is_403_without_consuming_attempt_or_mutating_state(
        self,
        isolated_env,
        monkeypatch,
    ):
        """The middleware rejects CSRF before limiter registration or scrypt work."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-cross-origin"
        epoch = await _make_user(username, password="cross origin current")
        before = await _user_auth_state(username)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            response = await client.patch(
                PASSWORD_PATH,
                json={
                    "current_password": "cross origin current",
                    "new_password": "cross origin replacement",
                },
                headers={
                    "Origin": "https://evil.example",
                    "Referer": "https://evil.example/",
                    "Host": "test",
                },
            )

        assert response.status_code == 403
        assert response.headers["Cache-Control"] == "no-store"
        assert await _user_auth_state(username) == before
        assert http_module._password_change_attempts == {}

    @pytest.mark.asyncio
    async def test_validation_is_422_forbidden_extra_and_never_reflects_secrets(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Every invalid body is sanitized before it reaches the client or limiter."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-validation"
        epoch = await _make_user(username)
        too_long_current = "current-private-" + ("c" * 1010)
        too_short_new = "short-private"
        too_long_new = "new-private-" + ("n" * 1014)
        extra_secret = "extra-private-value"
        payloads = [
            {"current_password": "", "new_password": "valid replacement password"},
            {
                "current_password": too_long_current,
                "new_password": "valid replacement password",
            },
            {"current_password": "irrelevant-here", "new_password": too_short_new},
            {"current_password": "irrelevant-here", "new_password": too_long_new},
            {
                "current_password": "irrelevant-here",
                "new_password": "valid replacement password",
                "repeated_password": extra_secret,
            },
        ]

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            responses = [
                await client.patch(PASSWORD_PATH, json=payload, headers=SAME_ORIGIN_HEADERS)
                for payload in payloads
            ]

        assert [response.status_code for response in responses] == [422] * len(payloads)
        assert all(response.headers["Cache-Control"] == "no-store" for response in responses)
        combined = "\n".join(response.text for response in responses)
        for secret in (too_long_current, too_short_new, too_long_new, extra_secret):
            assert secret not in combined
        assert http_module._password_change_attempts == {}

    @pytest.mark.asyncio
    async def test_lone_surrogates_in_either_secret_are_sanitized_422_before_scrypt(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Python-only Unicode surrogates cannot escape validation into UTF-8 hashing."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-surrogate-validation"
        epoch = await _make_user(username)
        current_surrogate = "current-" + chr(0xD800) + "-private"
        new_surrogate = "new-password-" + chr(0xDFFF) + "-private"

        async def forbidden_to_thread(*_args, **_kwargs):
            pytest.fail("invalid Unicode must not reach password verification or hashing")

        monkeypatch.setattr(http_module.asyncio, "to_thread", forbidden_to_thread)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            current_response = await client.patch(
                PASSWORD_PATH,
                content=(
                    b'{"current_password":"current-\\ud800-private",'
                    b'"new_password":"valid replacement password"}'
                ),
                headers={**SAME_ORIGIN_HEADERS, "Content-Type": "application/json"},
            )
            new_response = await client.patch(
                PASSWORD_PATH,
                content=(
                    b'{"current_password":"irrelevant-here",'
                    b'"new_password":"new-password-\\udfff-private"}'
                ),
                headers={**SAME_ORIGIN_HEADERS, "Content-Type": "application/json"},
            )

        assert current_response.status_code == 422
        assert new_response.status_code == 422
        assert current_response.headers["Cache-Control"] == "no-store"
        assert new_response.headers["Cache-Control"] == "no-store"
        assert current_surrogate not in current_response.text
        assert new_surrogate not in new_response.text
        assert http_module._password_change_attempts == {}

    @pytest.mark.parametrize(
        ("username", "current_password", "new_password"),
        [
            ("password-minimum", "x", "🔐" * 15),
            ("password-maximum", "c" * 1024, "n" * 1024),
            (
                "password-whitespace",
                " current password is not trimmed ",
                " new password is not trimmed either ",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_unicode_length_boundaries_and_whitespace_are_exact(
        self,
        isolated_env,
        monkeypatch,
        username,
        current_password,
        new_password,
    ):
        """Lengths count Unicode characters and neither secret is normalized."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user(username, password=current_password)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            response = await client.patch(
                PASSWORD_PATH,
                json={
                    "current_password": current_password,
                    "new_password": new_password,
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        stored_hash, stored_epoch, _generation, _disabled = await _user_auth_state(username)
        assert response.status_code == 200
        assert stored_epoch == epoch + 1
        assert webauth.verify_password(new_password, stored_hash) is True
        if new_password != new_password.strip():
            assert webauth.verify_password(new_password.strip(), stored_hash) is False

    @pytest.mark.asyncio
    async def test_verify_and_hash_are_offloaded_outside_every_database_session(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Both scrypt calls run through to_thread with no read or write session open."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-offload"
        current_password = "offload current password"
        epoch = await _make_user(username, password=current_password)
        original_get_session = http_module.get_session
        session_depth = 0
        offloaded = []

        @contextlib.asynccontextmanager
        async def tracked_get_session():
            nonlocal session_depth
            async with original_get_session() as session:
                session_depth += 1
                try:
                    yield session
                finally:
                    session_depth -= 1

        async def observed_to_thread(function, *args, **kwargs):
            assert session_depth == 0
            offloaded.append(function)
            return function(*args, **kwargs)

        monkeypatch.setattr(http_module, "get_session", tracked_get_session)
        monkeypatch.setattr(http_module.asyncio, "to_thread", observed_to_thread)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            response = await client.patch(
                PASSWORD_PATH,
                json={
                    "current_password": current_password,
                    "new_password": "offloaded replacement password",
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        assert response.status_code == 200
        assert offloaded == [webauth.verify_password, webauth.hash_password]

    @pytest.mark.asyncio
    async def test_limiter_is_per_account_lifetime_expires_and_sets_retry_after(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Five attempts are admitted; the sixth is blocked without affecting peers."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch_a = await _make_user("password-limited-a")
        epoch_b = await _make_user("password-limited-b")
        now = [100.0]
        monkeypatch.setattr(http_module, "_password_change_clock", lambda: now[0])
        monkeypatch.setattr(webauth, "verify_password", lambda _password, _stored: False)

        async with (
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                cookies=await _cookie("password-limited-a", epoch_a),
            ) as client_a,
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                cookies=await _cookie("password-limited-b", epoch_b),
            ) as client_b,
        ):
            attempts = [
                await client_a.patch(
                    PASSWORD_PATH,
                    json={
                        "current_password": "wrong password",
                        "new_password": "valid replacement password",
                    },
                    headers=SAME_ORIGIN_HEADERS,
                )
                for _ in range(http_module._PASSWORD_CHANGE_MAX_ATTEMPTS)
            ]
            blocked = await client_a.patch(
                PASSWORD_PATH,
                json={
                    "current_password": "wrong password",
                    "new_password": "valid replacement password",
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            peer = await client_b.patch(
                PASSWORD_PATH,
                json={
                    "current_password": "wrong password",
                    "new_password": "valid replacement password",
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            now[0] += http_module._PASSWORD_CHANGE_WINDOW_SECONDS
            expired = await client_a.patch(
                PASSWORD_PATH,
                json={
                    "current_password": "wrong password",
                    "new_password": "valid replacement password",
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        assert [response.status_code for response in attempts] == [400] * 5
        assert blocked.status_code == 429
        assert blocked.headers["Retry-After"] == "900"
        assert blocked.headers["Cache-Control"] == "no-store"
        assert peer.status_code == 400
        assert expired.status_code == 400

    @pytest.mark.asyncio
    async def test_limiter_registers_all_five_concurrent_attempts_before_scrypt_await(
        self,
        isolated_env,
        monkeypatch,
    ):
        """A sixth request is blocked while five admitted verifications are suspended."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-concurrent-limit"
        epoch = await _make_user(username)
        started = 0
        all_started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_to_thread(function, *args, **kwargs):
            nonlocal started
            if function is webauth.verify_password:
                started += 1
                if started == http_module._PASSWORD_CHANGE_MAX_ATTEMPTS:
                    all_started.set()
                await release.wait()
                return False
            return function(*args, **kwargs)

        monkeypatch.setattr(http_module.asyncio, "to_thread", blocked_to_thread)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            admitted_tasks = [
                asyncio.create_task(
                    client.patch(
                        PASSWORD_PATH,
                        json={
                            "current_password": "wrong password",
                            "new_password": "valid replacement password",
                        },
                        headers=SAME_ORIGIN_HEADERS,
                    )
                )
                for _ in range(http_module._PASSWORD_CHANGE_MAX_ATTEMPTS)
            ]
            try:
                await asyncio.wait_for(all_started.wait(), timeout=5)
                blocked = await client.patch(
                    PASSWORD_PATH,
                    json={
                        "current_password": "wrong password",
                        "new_password": "valid replacement password",
                    },
                    headers=SAME_ORIGIN_HEADERS,
                )
            finally:
                release.set()
            admitted = await asyncio.gather(*admitted_tasks)

        assert started == http_module._PASSWORD_CHANGE_MAX_ATTEMPTS
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) in range(1, 901)
        assert [response.status_code for response in admitted] == [400] * 5

    def test_limiter_key_includes_account_generation(self, isolated_env, monkeypatch):
        """A recreated lifetime cannot inherit the previous account's throttle bucket."""
        now = [50.0]
        monkeypatch.setattr(http_module, "_password_change_clock", lambda: now[0])
        for _ in range(http_module._PASSWORD_CHANGE_MAX_ATTEMPTS):
            assert (
                http_module._password_change_register_attempt(
                    user_id=17,
                    generation="old-generation",
                )
                is None
            )
        assert http_module._password_change_register_attempt(
            user_id=17,
            generation="old-generation",
        ) == 900
        assert (
            http_module._password_change_register_attempt(
                user_id=17,
                generation="new-generation",
            )
            is None
        )

    def test_limiter_has_a_fail_closed_hard_cap_for_fresh_account_keys(
        self,
        isolated_env,
        monkeypatch,
    ):
        """A 4097th live lifetime is rejected without evicting or growing state."""
        now = [75.0]
        monkeypatch.setattr(http_module, "_password_change_clock", lambda: now[0])

        for user_id in range(http_module._PASSWORD_CHANGE_MAX_KEYS):
            assert (
                http_module._password_change_register_attempt(
                    user_id=user_id,
                    generation=f"generation-{user_id}",
                )
                is None
            )

        assert len(http_module._password_change_attempts) == http_module._PASSWORD_CHANGE_MAX_KEYS
        blocked = http_module._password_change_register_attempt(
            user_id=http_module._PASSWORD_CHANGE_MAX_KEYS,
            generation="fresh-over-capacity",
        )
        assert blocked == 900
        assert len(http_module._password_change_attempts) == http_module._PASSWORD_CHANGE_MAX_KEYS
        assert (
            http_module._PASSWORD_CHANGE_MAX_KEYS,
            "fresh-over-capacity",
        ) not in http_module._password_change_attempts

        # The oldest live bucket remains intact and continues accumulating its
        # own attempts; capacity pressure cannot reset its throttle history.
        for _ in range(http_module._PASSWORD_CHANGE_MAX_ATTEMPTS - 1):
            assert (
                http_module._password_change_register_attempt(
                    user_id=0,
                    generation="generation-0",
                )
                is None
            )
        assert http_module._password_change_register_attempt(
            user_id=0,
            generation="generation-0",
        ) == 900

        # Once every old bucket is fully expired, deterministic stale cleanup
        # creates exactly one slot for the formerly blocked lifetime.
        now[0] += http_module._PASSWORD_CHANGE_WINDOW_SECONDS
        assert (
            http_module._password_change_register_attempt(
                user_id=http_module._PASSWORD_CHANGE_MAX_KEYS,
                generation="fresh-over-capacity",
            )
            is None
        )
        assert len(http_module._password_change_attempts) == 1

    @pytest.mark.asyncio
    async def test_epoch_race_after_verification_loses_cas_with_401(
        self,
        isolated_env,
        monkeypatch,
    ):
        """An epoch bump in the verification-to-write gap wins over this request."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-epoch-race"
        current_password = "epoch race current password"
        epoch = await _make_user(username, password=current_password)
        original = http_module._mail_ui_password_cas_update
        raced = False

        async def bump_epoch_before_cas(
            session,
            *,
            principal,
            old_password_hash,
            new_password_hash,
        ):
            nonlocal raced
            if not raced:
                raced = True
                async with get_session() as competing_session:
                    await competing_session.execute(
                        text(
                            "UPDATE ui_users SET session_epoch = session_epoch + 1 "
                            "WHERE username = :username"
                        ),
                        {"username": username},
                    )
                    await competing_session.commit()
            return await original(
                session,
                principal=principal,
                old_password_hash=old_password_hash,
                new_password_hash=new_password_hash,
            )

        monkeypatch.setattr(
            http_module,
            "_mail_ui_password_cas_update",
            bump_epoch_before_cas,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            response = await client.patch(
                PASSWORD_PATH,
                json={
                    "current_password": current_password,
                    "new_password": "epoch race requested replacement",
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        stored_hash, stored_epoch, _generation, _disabled = await _user_auth_state(username)
        assert raced is True
        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store"
        assert "set-cookie" not in response.headers
        assert stored_epoch == epoch + 1
        assert webauth.verify_password(current_password, stored_hash) is True

    @pytest.mark.asyncio
    async def test_password_hash_race_loses_cas_without_overwriting_competing_change(
        self,
        isolated_env,
        monkeypatch,
    ):
        """The old hash is part of CAS even when all cookie facts remain unchanged."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-hash-race"
        current_password = "hash race current password"
        competing_password = "hash race competing password"
        requested_password = "hash race requested password"
        epoch = await _make_user(username, password=current_password)
        competing_hash = webauth.hash_password(competing_password)
        original = http_module._mail_ui_password_cas_update

        async def replace_hash_before_cas(
            session,
            *,
            principal,
            old_password_hash,
            new_password_hash,
        ):
            async with get_session() as competing_session:
                await competing_session.execute(
                    text(
                        "UPDATE ui_users SET password_hash = :password_hash "
                        "WHERE username = :username"
                    ),
                    {"password_hash": competing_hash, "username": username},
                )
                await competing_session.commit()
            return await original(
                session,
                principal=principal,
                old_password_hash=old_password_hash,
                new_password_hash=new_password_hash,
            )

        monkeypatch.setattr(
            http_module,
            "_mail_ui_password_cas_update",
            replace_hash_before_cas,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            response = await client.patch(
                PASSWORD_PATH,
                json={
                    "current_password": current_password,
                    "new_password": requested_password,
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        stored_hash, stored_epoch, _generation, _disabled = await _user_auth_state(username)
        assert response.status_code == 401
        assert stored_epoch == epoch
        assert webauth.verify_password(competing_password, stored_hash) is True
        assert webauth.verify_password(requested_password, stored_hash) is False

    @pytest.mark.asyncio
    async def test_disable_race_loses_cas_without_reenabling_or_changing_password(
        self,
        isolated_env,
        monkeypatch,
    ):
        """The enabled predicate makes a concurrent account disable authoritative."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-disabled-race"
        current_password = "disabled race current password"
        requested_password = "disabled race requested password"
        epoch = await _make_user(username, password=current_password)
        original = http_module._mail_ui_password_cas_update

        async def disable_before_cas(
            session,
            *,
            principal,
            old_password_hash,
            new_password_hash,
        ):
            async with get_session() as competing_session:
                await competing_session.execute(
                    text(
                        "UPDATE ui_users SET disabled = 1 "
                        "WHERE username = :username"
                    ),
                    {"username": username},
                )
                await competing_session.commit()
            return await original(
                session,
                principal=principal,
                old_password_hash=old_password_hash,
                new_password_hash=new_password_hash,
            )

        monkeypatch.setattr(
            http_module,
            "_mail_ui_password_cas_update",
            disable_before_cas,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            response = await client.patch(
                PASSWORD_PATH,
                json={
                    "current_password": current_password,
                    "new_password": requested_password,
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        stored_hash, stored_epoch, _generation, disabled = await _user_auth_state(username)
        assert response.status_code == 401
        assert stored_epoch == epoch
        assert disabled is True
        assert webauth.verify_password(current_password, stored_hash) is True
        assert webauth.verify_password(requested_password, stored_hash) is False

    @pytest.mark.asyncio
    async def test_recreated_account_with_reused_primary_key_cannot_be_modified(
        self,
        isolated_env,
        monkeypatch,
    ):
        """The generation predicate protects a replacement created during rotation."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-recreated-race"
        current_password = "original account current password"
        replacement_password = "replacement account current password"
        requested_password = "requested password for old account"
        epoch = await _make_user(username, password=current_password)
        user_id = await _user_id(username)
        replacement_generation = "c" * 64
        replacement_hash = webauth.hash_password(replacement_password)
        original = http_module._mail_ui_password_cas_update

        async def recreate_before_cas(
            session,
            *,
            principal,
            old_password_hash,
            new_password_hash,
        ):
            async with get_session() as competing_session:
                await competing_session.execute(
                    text("DELETE FROM ui_users WHERE id = :user_id"),
                    {"user_id": user_id},
                )
                await competing_session.execute(
                    text(
                        "INSERT INTO ui_users "
                        "(id, username, password_hash, role, disabled, session_epoch, "
                        "session_generation, preferred_ui_locale, "
                        "preferred_correspondence_locale, created_ts) "
                        "VALUES (:user_id, :username, :password_hash, 'member', 0, "
                        ":epoch, :generation, 'en', NULL, datetime('now'))"
                    ),
                    {
                        "user_id": user_id,
                        "username": username,
                        "password_hash": replacement_hash,
                        "epoch": epoch,
                        "generation": replacement_generation,
                    },
                )
                await competing_session.commit()
            return await original(
                session,
                principal=principal,
                old_password_hash=old_password_hash,
                new_password_hash=new_password_hash,
            )

        monkeypatch.setattr(
            http_module,
            "_mail_ui_password_cas_update",
            recreate_before_cas,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(username, epoch),
        ) as client:
            response = await client.patch(
                PASSWORD_PATH,
                json={
                    "current_password": current_password,
                    "new_password": requested_password,
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        stored_hash, stored_epoch, generation, _disabled = await _user_auth_state(username)
        assert response.status_code == 401
        assert stored_epoch == epoch
        assert generation == replacement_generation
        assert webauth.verify_password(replacement_password, stored_hash) is True
        assert webauth.verify_password(requested_password, stored_hash) is False

    @pytest.mark.asyncio
    async def test_two_concurrent_valid_rotations_have_one_winner_and_one_401(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Two sessions can verify the old hash, but only one exact CAS may commit."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        username = "password-concurrent-cas"
        current_password = "concurrent CAS current password"
        replacements = (
            "concurrent CAS replacement alpha",
            "concurrent CAS replacement bravo",
        )
        epoch = await _make_user(username, password=current_password)
        cookie = await _cookie(username, epoch)
        original = http_module._mail_ui_password_cas_update
        arrived = 0
        both_ready = asyncio.Event()

        async def synchronize_cas(
            session,
            *,
            principal,
            old_password_hash,
            new_password_hash,
        ):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=5)
            return await original(
                session,
                principal=principal,
                old_password_hash=old_password_hash,
                new_password_hash=new_password_hash,
            )

        monkeypatch.setattr(
            http_module,
            "_mail_ui_password_cas_update",
            synchronize_cas,
        )
        async with (
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                cookies=cookie,
            ) as first,
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                cookies=cookie,
            ) as second,
        ):
            responses = await asyncio.gather(
                first.patch(
                    PASSWORD_PATH,
                    json={
                        "current_password": current_password,
                        "new_password": replacements[0],
                    },
                    headers=SAME_ORIGIN_HEADERS,
                ),
                second.patch(
                    PASSWORD_PATH,
                    json={
                        "current_password": current_password,
                        "new_password": replacements[1],
                    },
                    headers=SAME_ORIGIN_HEADERS,
                ),
            )

        stored_hash, stored_epoch, _generation, _disabled = await _user_auth_state(username)
        assert arrived == 2
        assert sorted(response.status_code for response in responses) == [200, 401]
        assert stored_epoch == epoch + 1
        assert sum(webauth.verify_password(password, stored_hash) for password in replacements) == 1


class TestMailUiRbacSurface:
    """Project assignments scope every human mailbox read and reply boundary."""

    @pytest.mark.asyncio
    async def test_auth_disabled_is_limited_to_test_and_development(self, isolated_env, monkeypatch):
        """Production cannot expose the UI by disabling its session gate."""
        test_settings, _app = _build(
            monkeypatch,
            MAIL_UI_AUTH_ENABLED="false",
            MAIL_UI_SESSION_SECRET="",
        )
        settings = replace(test_settings, environment="production")
        app = build_http_app(settings, build_mcp_server())
        response = await _get(app, GUARDED)

        assert response.status_code == 503
        assert "development or test" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_login_throttle_isolated_by_username_behind_one_proxy(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Failures for one account do not lock every user behind one proxy."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        await _make_user("throttled-user")
        await _make_user("healthy-user")
        monkeypatch.setattr(
            webauth,
            "authenticate",
            lambda username, password, stored: stored is not None and password == "correct",
        )
        headers = {
            "Origin": "http://test",
            "Referer": "http://test/",
            "Host": "test",
        }
        http_module._login_failures.clear()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                failures = [
                    await client.post(
                        "/mail/login",
                        data={
                            "username": "throttled-user",
                            "password": "wrong",
                            "next": "/mail",
                        },
                        headers=headers,
                    )
                    for _ in range(http_module._LOGIN_MAX_FAILURES)
                ]
                blocked = await client.post(
                    "/mail/login",
                    data={
                        "username": "throttled-user",
                        "password": "correct",
                        "next": "/mail",
                    },
                    headers=headers,
                )
                healthy = await client.post(
                    "/mail/login",
                    data={
                        "username": "healthy-user",
                        "password": "correct",
                        "next": "/mail",
                    },
                    headers=headers,
                )
        finally:
            http_module._login_failures.clear()

        assert all(response.status_code == 401 for response in failures)
        assert blocked.status_code == 429
        assert healthy.status_code == 303
        assert healthy.headers["Cache-Control"] == "no-store, no-transform"
        assert healthy.headers["Content-Security-Policy"] == LEGACY_CSP
        assert healthy.headers["Referrer-Policy"] == "no-referrer"
        assert healthy.headers["X-Frame-Options"] == "DENY"

    @pytest.mark.asyncio
    async def test_static_bearer_is_limited_to_file_reservation_reads(
        self,
        isolated_env,
        monkeypatch,
    ):
        """The service bearer has one exact read endpoint and no mailbox access."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        await _seed_project(
            "service-project",
            subject="Service-only",
            agent_name="ServiceAgent",
            sound="low",
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            reservations = await client.get(
                "/mail/api/file-reservations",
                params={"project": "service-project"},
                headers={"Authorization": f"Bearer {BEARER}"},
            )
            mailbox = await client.get(
                "/mail/api/unified-inbox",
                headers={"Authorization": f"Bearer {BEARER}"},
            )
            wrong_token = await client.get(
                "/mail/api/file-reservations",
                params={"project": "service-project"},
                headers={"Authorization": "Bearer wrong"},
            )

        assert reservations.status_code == 200
        assert mailbox.status_code == 404
        assert mailbox.json() == {"detail": "Not Found"}
        assert wrong_token.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_jwt_is_not_the_file_reservation_service_principal(
        self,
        isolated_env,
        monkeypatch,
    ):
        """JWT transport auth cannot cross into the static-bearer UI exception."""
        settings, app = _build(
            monkeypatch,
            MAIL_UI_SESSION_SECRET=SECRET,
            HTTP_JWT_ENABLED="true",
            HTTP_JWT_ALGORITHMS="HS256",
            HTTP_JWT_SECRET="jwt-service-boundary-secret",
        )
        await _seed_project(
            "jwt-project",
            subject="JWT boundary",
            agent_name="JwtAgent",
            sound="low",
        )
        token = jwt.encode(
            {"alg": "HS256"},
            {"sub": "jwt-client", settings.http.jwt_role_claim: "reader"},
            settings.http.jwt_secret,
        ).decode("utf-8")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/mail/api/file-reservations",
                params={"project": "jwt-project"},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_human_session_remains_valid_when_transport_jwt_is_enabled(
        self,
        isolated_env,
        monkeypatch,
    ):
        """JWT transport enforcement must not re-authenticate a verified UI cookie."""
        _settings, app = _build(
            monkeypatch,
            MAIL_UI_SESSION_SECRET=SECRET,
            HTTP_JWT_ENABLED="true",
            HTTP_JWT_ALGORITHMS="HS256",
            HTTP_JWT_SECRET="jwt-cookie-boundary-secret",
        )
        epoch = await _make_user("jwt-human", role=webauth.ROLE_ADMIN)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("jwt-human", epoch),
        ) as client:
            response = await client.get(GUARDED)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_typed_member_aggregates_filter_messages_and_projects_while_legacy_is_404(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        """Typed aggregates stay scoped while every retired aggregate is 404."""
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        visible_id, _visible_message = await _seed_project(
            "visible-project",
            subject="VISIBLE-SUBJECT",
            agent_name="VisibleAgent",
            sound="high",
        )
        hidden_id, _hidden_message = await _seed_project(
            "hidden-project",
            subject="HIDDEN-SUBJECT",
            agent_name="HiddenAgent",
            sound="low",
        )
        epoch = await _make_user("scoped-member", role=webauth.ROLE_MEMBER)
        await _assign("scoped-member", visible_id, webauth.PROJECT_ROLE_VIEWER)
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO project_sibling_suggestions "
                    "(project_a_id, project_b_id, score, status, rationale, created_ts, evaluated_ts) "
                    "VALUES (:visible_id, :hidden_id, 1.0, 'confirmed', 'must not leak', "
                    "datetime('now'), datetime('now'))"
                ),
                {"visible_id": visible_id, "hidden_id": hidden_id},
            )
            await session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("scoped-member", epoch),
        ) as client:
            inbox_response = await client.get("/mail/api/v1/inbox")
            projects_response = await client.get("/mail/api/v1/projects")
            root_page = await client.get("/mail")
            legacy_page = await client.get("/mail/unified-inbox")
            projects_page = await client.get("/mail/projects")
            legacy_api = await client.get(
                "/mail/api/unified-inbox",
                params={"include_projects": "true"},
            )

        inbox = inbox_response.json()
        assert inbox_response.status_code == 200
        assert inbox_response.headers["Cache-Control"] == "no-store"
        assert inbox["total"] == 1
        assert [message["subject"] for message in inbox["items"]] == [
            "VISIBLE-SUBJECT"
        ]
        assert inbox["items"][0]["can_reply"] is False

        projects = projects_response.json()
        assert projects_response.status_code == 200
        assert projects_response.headers["Cache-Control"] == "no-store"
        assert projects["total"] == 1
        assert [project["slug"] for project in projects["items"]] == [
            "visible-project"
        ]

        assert root_page.status_code == 200
        assert root_page.headers["Content-Security-Policy"] == REACT_CSP
        assert "HIDDEN-SUBJECT" not in root_page.text
        assert "hidden-project" not in root_page.text
        for response in (legacy_page, projects_page, legacy_api):
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}

    @pytest.mark.asyncio
    async def test_cutover_shell_and_typed_projects_never_refresh_sibling_profiles(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        """Read-only React surfaces never trigger profiling, writes, or LLM cost."""
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, _message_id = await _seed_project(
            "sibling-refresh-project",
            subject="Sibling refresh boundary",
            agent_name="SiblingAgent",
            sound="low",
        )
        member_epoch = await _make_user("sibling-member", role=webauth.ROLE_MEMBER)
        admin_epoch = await _make_user("sibling-admin", role=webauth.ROLE_ADMIN)
        await _assign("sibling-member", project_id, webauth.PROJECT_ROLE_VIEWER)
        refresh_calls: list[None] = []

        async def observed_refresh() -> None:
            refresh_calls.append(None)

        monkeypatch.setattr(http_module, "refresh_project_sibling_suggestions", observed_refresh)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("sibling-member", member_epoch),
        ) as member_client:
            member_root = await member_client.get("/mail")
            member_projects = await member_client.get("/mail/api/v1/projects")
            member_legacy_projects = await member_client.get("/mail/projects")

        assert member_root.status_code == 200
        assert member_projects.status_code == 200
        assert member_legacy_projects.status_code == 404
        assert refresh_calls == []

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("sibling-admin", admin_epoch),
        ) as admin_client:
            admin_root = await admin_client.get("/mail")
            admin_projects = await admin_client.get("/mail/api/v1/projects")
            admin_legacy_projects = await admin_client.get("/mail/projects")

        assert admin_root.status_code == 200
        assert admin_projects.status_code == 200
        assert admin_legacy_projects.status_code == 404
        assert refresh_calls == []

    @pytest.mark.asyncio
    async def test_missing_assignment_is_404_and_viewer_cannot_reply(
        self,
        isolated_env,
        monkeypatch,
    ):
        """All retired project pages are 404 regardless of former visibility."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        visible_id, visible_message = await _seed_project(
            "viewer-project",
            subject="Viewer message",
            agent_name="ViewerAgent",
            sound="high",
        )
        _hidden_id, hidden_message = await _seed_project(
            "no-access-project",
            subject="Hidden message",
            agent_name="HiddenAgent",
            sound="low",
        )
        epoch = await _make_user("viewer-member", role=webauth.ROLE_MEMBER)
        await _assign("viewer-member", visible_id, webauth.PROJECT_ROLE_VIEWER)
        headers = {"Origin": "http://test", "Referer": "http://test/", "Host": "test"}

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("viewer-member", epoch),
        ) as client:
            hidden_routes = [
                "/mail/no-access-project",
                f"/mail/no-access-project/message/{hidden_message}",
                "/mail/no-access-project/search?q=Hidden",
                "/mail/no-access-project/attachments",
                "/mail/no-access-project/file_reservations",
                "/mail/no-access-project/inbox/HiddenAgent",
                "/mail/no-access-project/thread/1",
                "/mail/api/projects/no-access-project/agents",
            ]
            hidden_responses = [await client.get(path) for path in hidden_routes]
            compose = await client.get(
                "/mail/viewer-project/overseer/compose",
                params={"reply_to": visible_message},
            )
            reply = await client.post(
                "/mail/viewer-project/overseer/reply",
                json={"reply_to": visible_message, "body_md": "Denied"},
                headers=headers,
            )

        assert all(response.status_code == 404 for response in hidden_responses)
        assert compose.status_code == 404
        assert compose.json() == {"detail": "Not Found"}
        assert reply.status_code == 404
        assert reply.json() == {"detail": "Not Found"}

    @pytest.mark.asyncio
    async def test_operator_reply_metadata_uses_typed_api_while_legacy_routes_are_404(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Typed metadata survives while every retired writer fails closed."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        collision_id, _collision_message = await _seed_project(
            "operator-collision",
            subject="Collision target",
            agent_name="CollisionAgent",
            sound="low",
        )
        async with get_session() as session:
            await session.execute(
                text("UPDATE projects SET human_key = 'operator-project' WHERE id = :pid"),
                {"pid": collision_id},
            )
            await session.commit()
        project_id, message_id = await _seed_project(
            "operator-project",
            subject="Operator target",
            agent_name="OperatorAgent",
            sound="high",
        )
        epoch = await _make_user("project-operator", role=webauth.ROLE_MEMBER)
        await _assign("project-operator", project_id, webauth.PROJECT_ROLE_OPERATOR)
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE ui_users SET preferred_ui_locale = 'pl', "
                    "preferred_correspondence_locale = NULL "
                    "WHERE username = 'project-operator'"
                )
            )
            await session.commit()
        headers = {"Origin": "http://test", "Referer": "http://test/", "Host": "test"}

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("project-operator", epoch),
        ) as client:
            compose_reply = await client.get(
                "/mail/operator-project/overseer/compose",
                params={"reply_to": message_id},
            )
            compose_new = await client.get("/mail/operator-project/overseer/compose")
            typed_inbox = await client.get("/mail/api/v1/inbox")
            arbitrary_send = await client.post(
                "/mail/operator-project/overseer/send",
                json={"recipients": ["OperatorAgent"], "subject": "No", "body_md": "No"},
                headers=headers,
            )
            reply = await client.post(
                "/mail/operator-project/overseer/reply",
                json={
                    "reply_to": message_id,
                    "body_md": "Approved reply",
                    "recipients": ["Ignored"],
                    "subject": "Ignored",
                    "thread_id": "ignored",
                    "preferred_correspondence_locale": "en",
                },
                headers=headers,
            )

        assert compose_reply.status_code == 404
        assert compose_reply.json() == {"detail": "Not Found"}
        assert compose_new.status_code == 404
        assert compose_new.json() == {"detail": "Not Found"}
        assert typed_inbox.status_code == 200
        operator_message = next(
            item for item in typed_inbox.json()["items"] if item["id"] == message_id
        )
        assert operator_message["can_reply"] is True
        assert arbitrary_send.status_code == 404
        assert arbitrary_send.json() == {"detail": "Not Found"}
        assert reply.status_code == 404
        assert reply.json() == {"detail": "Not Found"}

    @pytest.mark.asyncio
    async def test_legacy_overseer_hold_precedes_database_and_archive_writes(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Retired POST routes return 404 before either persistence layer is touched."""
        from mcp_agent_mail import storage as storage_module

        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, message_id = await _seed_project(
            "operator-revocation-race",
            subject="Revocation target",
            agent_name="RevocationAgent",
            sound="high",
        )
        epoch = await _make_user("legacy-overseer-admin", role=webauth.ROLE_ADMIN)
        archive_write_calls = 0

        async def forbidden_archive_write(*args, **kwargs):
            del args, kwargs
            nonlocal archive_write_calls
            archive_write_calls += 1
            raise AssertionError("legacy overseer hold must precede archive persistence")

        monkeypatch.setattr(storage_module, "write_message_bundle", forbidden_archive_write)
        async with get_session() as session:
            before_count = int(
                (
                    await session.execute(
                        text("SELECT COUNT(*) FROM messages WHERE project_id = :project_id"),
                        {"project_id": project_id},
                    )
                ).scalar_one()
            )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("legacy-overseer-admin", epoch),
        ) as client:
            send_response = await client.post(
                "/mail/operator-revocation-race/overseer/send",
                json={
                    "recipients": ["RevocationAgent"],
                    "subject": "Must not be sent",
                    "body_md": "Must not be sent",
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            reply_response = await client.post(
                "/mail/operator-revocation-race/overseer/reply",
                json={"reply_to": message_id, "body_md": "Must not be sent"},
                headers=SAME_ORIGIN_HEADERS,
            )

        assert send_response.status_code == 404
        assert send_response.json() == {"detail": "Not Found"}
        assert reply_response.status_code == 404
        assert reply_response.json() == {"detail": "Not Found"}
        assert archive_write_calls == 0
        async with get_session() as session:
            after_count = int(
                (
                    await session.execute(
                        text("SELECT COUNT(*) FROM messages WHERE project_id = :project_id"),
                        {"project_id": project_id},
                    )
                ).scalar_one()
            )
        assert after_count == before_count

    @pytest.mark.asyncio
    async def test_ambiguous_human_project_key_fails_closed(
        self,
        isolated_env,
        monkeypatch,
    ):
        """A non-unique human key cannot select an arbitrary project row."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        first_id, _first_message = await _seed_project(
            "ambiguous-first",
            subject="First ambiguous project",
            agent_name="FirstAgent",
            sound="low",
        )
        second_id, _second_message = await _seed_project(
            "ambiguous-second",
            subject="Second ambiguous project",
            agent_name="SecondAgent",
            sound="high",
        )
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE projects SET human_key = 'shared-human-key' "
                    "WHERE id IN (:first_id, :second_id)"
                ),
                {"first_id": first_id, "second_id": second_id},
            )
            await session.commit()
        epoch = await _make_user("ambiguity-admin")

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("ambiguity-admin", epoch),
        ) as client:
            response = await client.get("/mail/shared-human-key")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_wildcard_sse_drops_hidden_events_and_revalidates_access(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Wildcard events disclose only projects still visible to the session."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        visible_id, _message_id = await _seed_project(
            "event-visible",
            subject="Visible event",
            agent_name="EventAgent",
            sound="high",
        )
        await _seed_project(
            "event-hidden",
            subject="Hidden event",
            agent_name="HiddenEventAgent",
            sound="low",
        )
        epoch = await _make_user("event-member", role=webauth.ROLE_MEMBER)
        await _assign("event-member", visible_id, webauth.PROJECT_ROLE_VIEWER)
        queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        queue.put_nowait({"kind": "changed", "project": "event-hidden"})
        queue.put_nowait({"kind": "changed", "project": "event-visible"})
        revalidations: list[str | None] = []
        original_revalidate = http_module._mail_ui_stream_access_valid

        async def observed_revalidate(**kwargs):
            revalidations.append(kwargs["project_slug"])
            return await original_revalidate(**kwargs)

        monkeypatch.setattr(http_module.hub, "subscribe_projects", lambda _projects: queue)
        monkeypatch.setattr(
            http_module.hub,
            "unsubscribe_projects",
            lambda _projects, _queue: None,
        )
        monkeypatch.setattr(http_module, "MAX_STREAM_SECONDS", 0.03)
        monkeypatch.setattr(http_module, "KEEPALIVE_SECONDS", 0.005)
        monkeypatch.setattr(http_module, "_mail_ui_stream_access_valid", observed_revalidate)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("event-member", epoch),
        ) as client:
            response = await client.get("/mail/events")

        assert response.status_code == 200
        assert "event-visible" in response.text
        assert "event-hidden" not in response.text
        assert "event-hidden" in revalidations
        assert "event-visible" in revalidations
        assert None in revalidations

    def test_every_mail_read_route_has_an_explicit_security_class(self, isolated_env, monkeypatch):
        """A new mail GET route cannot land without an explicit RBAC classification."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        classification = {
            "/mail/login": "public-session",
            "/mail/logout": "public-method-denied",
            "/mail": "canonical-shell",
            "/mail/": "canonical-slash-redirect",
            "/mail/assets/{asset_path:path}": "session-static-asset",
            "/mail/events": "aggregate-scoped",
            "/mail/api/unified-inbox": "retired-404",
            "/mail/api/v1/inbox": "aggregate-scoped",
            "/mail/api/v1/admin/access": "admin-only",
            "/mail/api/v1/me/profile": "self-only",
            "/mail/api/v1/me/preferences": "self-only",
            "/mail/api/v1/projects": "aggregate-scoped",
            "/mail/api/v1/deliveries/{delivery_id}": "self-only",
            "/mail/api/v1/projects/{project_id}/messages/{message_id}": (
                "project-guarded"
            ),
            "/mail/api/v1/projects/{project_id}/threads/{thread_id}": (
                "project-guarded"
            ),
            "/mail/projects": "retired-404",
            "/mail/unified-inbox": "retired-404",
            "/mail/api/locks": "retired-404",
            "/mail/api/file-reservations": "service-or-project-scoped",
            "/mail/{project}": "retired-404",
            "/mail/{project}/inbox/{agent}": "retired-404",
            "/mail/{project}/message/{mid}": "retired-404",
            "/mail/{project}/thread/{thread_id}": "retired-404",
            "/mail/{project}/search": "retired-404",
            "/mail/{project}/file_reservations": "retired-404",
            "/mail/{project}/attachments": "retired-404",
            "/mail/{project}/overseer/compose": "retired-404",
            "/mail/archive/guide": "retired-404",
            "/mail/archive/activity": "retired-404",
            "/mail/archive/commit/{sha}": "retired-404",
            "/mail/archive/timeline": "retired-404",
            "/mail/archive/browser": "retired-404",
            "/mail/archive/browser/{project}/file": "retired-404",
            "/mail/archive/browser/{project}/download": "retired-404",
            "/mail/archive/network": "retired-404",
            "/mail/api/projects/{project}/agents": "retired-404",
            "/mail/archive/time-travel": "retired-404",
            "/mail/archive/time-travel/snapshot": "retired-404",
        }
        actual = {
            route.path
            for route in app.routes
            if route.path.startswith("/mail")
            and bool((getattr(route, "methods", set()) or set()) & {"GET", "HEAD"})
        }

        assert actual == set(classification)
        assert set(classification.values()) <= {
            "public-session",
            "public-method-denied",
            "aggregate-scoped",
            "admin-only",
            "service-or-project-scoped",
            "project-guarded",
            "project-role-guarded",
            "project-query-guarded",
            "self-only",
            "canonical-shell",
            "canonical-slash-redirect",
            "session-static-asset",
            "retired-404",
        }


class TestMailUiV1ReadApi:
    """The React read API is typed, project-scoped, and privacy-minimal."""

    @pytest.mark.asyncio
    async def test_visibility_summary_detail_and_bcc_contract(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        visible_id, visible_message_id = await _seed_project(
            "api-v1-visible",
            subject="Visible body must stay out of the list",
            agent_name="VisibleSender",
            sound="high",
        )
        hidden_id, hidden_message_id = await _seed_project(
            "api-v1-hidden",
            subject="Hidden project message",
            agent_name="HiddenSender",
            sound="low",
        )
        epoch = await _make_user("api-v1-viewer", role=webauth.ROLE_MEMBER)
        await _assign("api-v1-viewer", visible_id, webauth.PROJECT_ROLE_VIEWER)

        async with get_session() as session:
            hidden_sender_id = int(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM agents "
                            "WHERE project_id = :project_id AND name = 'HiddenSender'"
                        ),
                        {"project_id": hidden_id},
                    )
                ).scalar_one()
            )
            recipient_ids: dict[str, int] = {}
            for name in ("ToAgent", "CcAgent", "BlindAgent"):
                recipient_ids[name] = int(
                    (
                        await session.execute(
                            text(
                                "INSERT INTO agents "
                                "(project_id, name, program, model, task_description, "
                                "inception_ts, last_active_ts, attachments_policy, contact_policy) "
                                "VALUES (:project_id, :name, 'test', 'test', 'recipient', "
                                "datetime('now'), datetime('now'), 'auto', 'open') "
                                "RETURNING id"
                            ),
                            {"project_id": visible_id, "name": name},
                        )
                    ).scalar_one()
                )
            await session.execute(
                text(
                    "UPDATE messages SET thread_id = 'api-v1-thread', "
                    "importance = 'legacy-free-form', attachments = :attachments "
                    "WHERE id = :message_id"
                ),
                {
                    "message_id": visible_message_id,
                    "attachments": (
                        '[{"type":"file","media_type":"text/plain","bytes":42,'
                        '"path":"private/archive.txt","url":"https://tracker.invalid",'
                        '"data_uri":"data:text/plain;base64,c2VjcmV0"}]'
                    ),
                },
            )
            for kind, name in (
                ("to", "ToAgent"),
                ("cc", "CcAgent"),
                ("bcc", "BlindAgent"),
            ):
                await session.execute(
                    text(
                        "INSERT INTO message_recipients (message_id, agent_id, kind) "
                        "VALUES (:message_id, :agent_id, :kind)"
                    ),
                    {
                        "message_id": visible_message_id,
                        "agent_id": recipient_ids[name],
                        "kind": kind,
                    },
                )
            cross_project_message_id = int(
                (
                    await session.execute(
                        text(
                            "INSERT INTO messages "
                            "(project_id, sender_id, thread_id, subject, body_md, importance, "
                            "ack_required, created_ts, attachments) "
                            "VALUES (:project_id, :sender_id, 'api-v1-thread', :subject, :body, "
                            "'high', 1, '2030-01-01 00:00:00.000000', '[]') RETURNING id"
                        ),
                        {
                            "project_id": visible_id,
                            "sender_id": hidden_sender_id,
                            "subject": "Cross-project sender",
                            "body": "Visible message, invisible sender project.",
                        },
                    )
                ).scalar_one()
            )
            await session.commit()

        cookie = await _cookie("api-v1-viewer", epoch)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=cookie,
        ) as client:
            projects = await client.get("/mail/api/v1/projects")
            inbox = await client.get("/mail/api/v1/inbox")
            visible_detail = await client.get(
                f"/mail/api/v1/projects/{visible_id}/messages/{visible_message_id}"
            )
            cross_detail = await client.get(
                f"/mail/api/v1/projects/{visible_id}/messages/{cross_project_message_id}"
            )
            thread = await client.get(
                f"/mail/api/v1/projects/{visible_id}/threads/api-v1-thread"
            )
            hidden_inbox = await client.get(
                "/mail/api/v1/inbox",
                params={"project_id": hidden_id},
            )
            hidden_detail = await client.get(
                f"/mail/api/v1/projects/{hidden_id}/messages/{hidden_message_id}"
            )
            missing_detail = await client.get(
                f"/mail/api/v1/projects/{visible_id}/messages/999999999"
            )
            missing_thread = await client.get(
                f"/mail/api/v1/projects/{visible_id}/threads/not-a-thread"
            )

        assert projects.status_code == 200
        assert projects.json() == {
            "items": [
                {
                    "id": visible_id,
                    "slug": "api-v1-visible",
                    "human_key": "/api-v1-visible",
                    "created_at": projects.json()["items"][0]["created_at"],
                    "archived_at": None,
                    "role": "viewer",
                    "can_reply": False,
                }
            ],
            "total": 1,
        }
        assert "/api-v1-hidden" not in projects.text

        assert inbox.status_code == 200
        inbox_payload = inbox.json()
        assert inbox_payload["total"] == 2
        assert inbox_payload["next_cursor"] is None
        assert {item["project_id"] for item in inbox_payload["items"]} == {visible_id}
        summary_keys = {
            "id",
            "project_id",
            "project_slug",
            "subject",
            "sender",
            "sender_name",
            "sender_display_name",
            "importance",
            "ack_required",
            "thread_id",
            "reply_to",
            "created_ts",
            "can_reply",
        }
        assert all(set(item) == summary_keys for item in inbox_payload["items"])
        assert all(item["can_reply"] is False for item in inbox_payload["items"])
        cross_summary = next(
            item for item in inbox_payload["items"] if item["id"] == cross_project_message_id
        )
        assert cross_summary["sender"] == "HiddenSender@api-v1-hidden"
        assert cross_summary["sender_name"] == "HiddenSender"
        assert "/api-v1-hidden" not in inbox.text
        assert all(
            not set(item) & {"body_md", "recipients", "read", "read_ts", "bcc"}
            for item in inbox_payload["items"]
        )
        assert "Body for Visible body must stay out of the list" not in inbox.text
        assert "BlindAgent" not in inbox.text

        assert visible_detail.status_code == 200
        detail_payload = visible_detail.json()
        assert detail_payload["body_md"] == "Body for Visible body must stay out of the list"
        assert detail_payload["importance"] == "normal"
        assert detail_payload["to"] == ["ToAgent"]
        assert detail_payload["cc"] == ["CcAgent"]
        assert detail_payload["attachments"] == [
            {"type": "file", "media_type": "text/plain", "size_bytes": 42}
        ]
        assert "BlindAgent" not in visible_detail.text
        for forbidden in ("bcc", "private/archive.txt", "tracker.invalid", "data_uri"):
            assert forbidden not in visible_detail.text
        assert cross_detail.status_code == 200
        assert "/api-v1-hidden" not in cross_detail.text
        assert thread.status_code == 200
        assert thread.json()["total"] == 2
        assert "BlindAgent" not in thread.text
        assert "bcc" not in thread.text

        for response in (
            projects,
            inbox,
            visible_detail,
            cross_detail,
            thread,
            hidden_inbox,
            hidden_detail,
            missing_detail,
            missing_thread,
        ):
            assert response.headers["Cache-Control"] == "no-store"
        assert hidden_inbox.status_code == 404
        assert hidden_detail.status_code == 404
        assert missing_detail.status_code == 404
        assert missing_thread.status_code == 404

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as anonymous:
            unauthorized = await anonymous.get("/mail/api/v1/projects")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_same_timestamp_keyset_pagination_is_stable_and_validated(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, first_message_id = await _seed_project(
            "api-v1-pagination",
            subject="Page item 0",
            agent_name="PageSender",
            sound="soft",
        )
        async with get_session() as session:
            sender_id = int(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM agents "
                            "WHERE project_id = :project_id AND name = 'PageSender'"
                        ),
                        {"project_id": project_id},
                    )
                ).scalar_one()
            )
            await session.execute(
                text(
                    "UPDATE messages SET thread_id = 'stable-thread', "
                    "created_ts = '2040-02-03 04:05:06.000000' WHERE id = :message_id"
                ),
                {"message_id": first_message_id},
            )
            for index in range(1, 5):
                await session.execute(
                    text(
                        "INSERT INTO messages "
                        "(project_id, sender_id, thread_id, subject, body_md, importance, "
                        "ack_required, created_ts, attachments) "
                        "VALUES (:project_id, :sender_id, 'stable-thread', :subject, :body, "
                        "'normal', 0, :created_ts, '[]')"
                    ),
                    {
                        "project_id": project_id,
                        "sender_id": sender_id,
                        "subject": f"Page item {index}",
                        "body": f"Body {index}",
                        # The highest id deliberately uses the fraction-less
                        # raw-SQL form.  It represents the same instant as the
                        # fixed-width values and must therefore win by id, not
                        # sort behind them because its string is shorter.
                        "created_ts": (
                            "2040-02-03 04:05:06"
                            if index == 4
                            else "2040-02-03 04:05:06.000000"
                        ),
                    },
                )
            await session.commit()

        epoch = await _make_user("api-v1-pagination-admin")
        cookie = await _cookie("api-v1-pagination-admin", epoch)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=cookie,
        ) as client:
            first = await client.get(
                "/mail/api/v1/inbox",
                params={"project_id": project_id, "limit": 2},
            )
            repeated = await client.get(
                "/mail/api/v1/inbox",
                params={"project_id": project_id, "limit": 2},
            )
            second = await client.get(
                "/mail/api/v1/inbox",
                params={
                    "project_id": project_id,
                    "limit": 2,
                    "cursor": first.json()["next_cursor"],
                },
            )
            third = await client.get(
                "/mail/api/v1/inbox",
                params={
                    "project_id": project_id,
                    "limit": 2,
                    "cursor": second.json()["next_cursor"],
                },
            )
            thread_first = await client.get(
                f"/mail/api/v1/projects/{project_id}/threads/stable-thread",
                params={"limit": 2},
            )
            thread_second = await client.get(
                f"/mail/api/v1/projects/{project_id}/threads/stable-thread",
                params={"limit": 2, "cursor": thread_first.json()["next_cursor"]},
            )
            malformed = [
                await client.get("/mail/api/v1/inbox", params={"cursor": cursor})
                for cursor in ("not-base64!", "e30", "W10")
            ]
            invalid_limits = [
                await client.get("/mail/api/v1/inbox", params={"limit": value})
                for value in (0, 101)
            ]

        assert first.status_code == 200
        assert repeated.json() == first.json()
        pages = [first.json(), second.json(), third.json()]
        assert [page["total"] for page in pages] == [5, 5, 5]
        assert [len(page["items"]) for page in pages] == [2, 2, 1]
        assert pages[0]["next_cursor"] is not None
        assert pages[1]["next_cursor"] is not None
        assert pages[2]["next_cursor"] is None
        paged_ids = [item["id"] for page in pages for item in page["items"]]
        assert paged_ids == sorted(paged_ids, reverse=True)
        assert len(paged_ids) == len(set(paged_ids)) == 5
        assert thread_first.status_code == 200
        assert thread_second.status_code == 200
        assert thread_first.json()["total"] == 5
        assert thread_second.json()["total"] == 5
        assert not (
            {item["id"] for item in thread_first.json()["items"]}
            & {item["id"] for item in thread_second.json()["items"]}
        )
        for response in [*malformed, *invalid_limits]:
            assert response.status_code == 422
            assert response.headers["Cache-Control"] == "no-store"


class TestMailUiV1DeliveryApi:
    """Human writes enter the same durable, idempotent delivery state machine."""

    @pytest.mark.asyncio
    async def test_authored_delivery_validation_never_reflects_request_values(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, message_id = await _seed_project(
            "api-v1-validation-redaction",
            subject="Validation source",
            agent_name="ValidationTarget",
            sound="soft",
        )
        epoch = await _make_user("validation-redaction-admin")
        secret_body = "do-not-reflect-this-authored-body"
        secret_key = "do-not-reflect-this-idempotency-key"

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("validation-redaction-admin", epoch),
        ) as client:
            compose = await client.post(
                f"/mail/api/v1/projects/{project_id}/messages",
                json={
                    "idempotency_key": secret_key,
                    "recipients": ["ValidationTarget"],
                    "subject": "Valid subject",
                    "body_md": secret_body,
                    "unexpected": secret_body,
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            reply = await client.post(
                f"/mail/api/v1/projects/{project_id}/messages/{message_id}/replies",
                json={
                    "idempotency_key": secret_key,
                    "body_md": secret_body,
                    "unexpected": secret_body,
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            malformed_reply_path = await client.post(
                "/mail/api/v1/projects/not-an-id/messages/0/replies",
                json={
                    "idempotency_key": secret_key,
                    "body_md": secret_body,
                    "unexpected": secret_body,
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            malformed_compose_path = await client.post(
                "/mail/api/v1/projects/not-an-id/messages",
                json={
                    "idempotency_key": secret_key,
                    "recipients": ["ValidationTarget"],
                    "subject": "Valid subject",
                    "body_md": secret_body,
                    "unexpected": secret_body,
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        for response in (
            compose,
            reply,
            malformed_reply_path,
            malformed_compose_path,
        ):
            assert response.status_code == 422
            assert response.headers["Cache-Control"] == "no-store"
            assert secret_body not in response.text
            assert secret_key not in response.text
            for error in response.json()["detail"]:
                assert set(error) <= {"type", "loc", "msg"}

    @pytest.mark.asyncio
    async def test_delivery_access_and_internal_failures_keep_typed_safe_shape(
        self,
        isolated_env,
        monkeypatch,
        caplog,
        capsys,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("delivery-failure-admin")
        payload = {
            "idempotency_key": "typed-failure-1",
            "recipients": ["Nobody"],
            "subject": "Never persisted",
            "body_md": "Never persisted.",
        }
        secret = "do-not-log-this-authored-exception"

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("delivery-failure-admin", epoch),
        ) as client:
            missing = await client.post(
                "/mail/api/v1/projects/999999/messages",
                json=payload,
                headers=SAME_ORIGIN_HEADERS,
            )

            async def fail_acceptance(_request: Any):
                raise RuntimeError(secret)

            project_id, _message_id = await _seed_project(
                "typed-internal-failure",
                subject="Existing message",
                agent_name="FailureTarget",
                sound="soft",
            )
            original_preferences_user = http_module._mail_ui_preferences_user

            async def stale_preferences_user(*_args: Any, **_kwargs: Any):
                raise http_module.HTTPException(
                    status_code=401,
                    detail=secret,
                )

            monkeypatch.setattr(
                http_module,
                "_mail_ui_preferences_user",
                stale_preferences_user,
            )
            revoked = await client.post(
                f"/mail/api/v1/projects/{project_id}/messages",
                json={**payload, "recipients": ["FailureTarget"]},
                headers=SAME_ORIGIN_HEADERS,
            )
            monkeypatch.setattr(
                http_module,
                "_mail_ui_preferences_user",
                original_preferences_user,
            )
            monkeypatch.setattr(
                http_module,
                "accept_message_delivery",
                fail_acceptance,
            )
            failed = await client.post(
                f"/mail/api/v1/projects/{project_id}/messages",
                json={**payload, "recipients": ["FailureTarget"]},
                headers=SAME_ORIGIN_HEADERS,
            )

        captured = capsys.readouterr()
        assert missing.status_code == 404
        assert missing.json() == {"detail": {"code": "project_not_found"}}
        assert revoked.status_code == 401
        assert revoked.json() == {"detail": {"code": "actor_forbidden"}}
        assert failed.status_code == 500
        assert failed.json() == {"detail": {"code": "delivery_failed"}}
        assert secret not in failed.text
        assert secret not in caplog.text
        assert secret not in captured.out
        assert secret not in captured.err

    @pytest.mark.asyncio
    async def test_admin_compose_is_published_once_and_status_is_account_scoped(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, _message_id = await _seed_project(
            "api-v1-compose",
            subject="Existing message",
            agent_name="ComposeTarget",
            sound="soft",
        )
        admin_epoch = await _make_user("compose-admin")
        other_epoch = await _make_user("compose-other")
        notified: list[str] = []

        async def record_notification(delivery_id: str) -> None:
            notified.append(delivery_id)

        monkeypatch.setattr(
            http_module,
            "emit_published_delivery_notifications",
            record_notification,
        )
        payload = {
            "idempotency_key": "web-compose-1",
            "recipients": ["ComposeTarget"],
            "subject": "Durable human request",
            "body_md": "Please inspect the deploy.",
            "thread_id": "human-thread-1",
        }

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("compose-admin", admin_epoch),
        ) as client:
            first = await client.post(
                f"/mail/api/v1/projects/{project_id}/messages",
                json=payload,
                headers=SAME_ORIGIN_HEADERS,
            )
            repeated = await client.post(
                f"/mail/api/v1/projects/{project_id}/messages",
                json=payload,
                headers=SAME_ORIGIN_HEADERS,
            )
            conflict = await client.post(
                f"/mail/api/v1/projects/{project_id}/messages",
                json={**payload, "body_md": "A different request."},
                headers=SAME_ORIGIN_HEADERS,
            )
            own_status = await client.get(
                f"/mail/api/v1/deliveries/{first.json()['id']}"
            )
            retried = await client.post(
                f"/mail/api/v1/deliveries/{first.json()['id']}/retry",
                json={},
                headers=SAME_ORIGIN_HEADERS,
            )

        assert first.status_code == repeated.status_code == 200
        assert first.json()["status"] == "published"
        assert first.json()["reused"] is False
        assert repeated.json() == {**first.json(), "reused": True}
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": {"code": "idempotency_conflict"}}
        assert own_status.status_code == 200
        assert own_status.json() == {**first.json(), "reused": True}
        assert retried.status_code == 200
        assert retried.json() == {**first.json(), "reused": True}
        assert "error" not in first.json()
        for response in (first, repeated, conflict, own_status, retried):
            assert response.headers["Cache-Control"] == "no-store"

        async with get_session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT m.body_md, sender.name, COUNT(*) OVER () "
                        "FROM messages m JOIN agents sender ON sender.id = m.sender_id "
                        "WHERE m.delivery_id = :delivery_id"
                    ),
                    {"delivery_id": first.json()["id"]},
                )
            ).one()
            delivery_count = int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM message_deliveries "
                            "WHERE idempotency_key = 'web-compose-1'"
                        )
                    )
                ).scalar_one()
            )
        assert row[1] == "HumanOverseer"
        assert "MESSAGE FROM HUMAN OVERSEER" in str(row[0])
        assert str(row[0]).endswith("Please inspect the deploy.")
        assert int(row[2]) == delivery_count == 1
        assert notified == [first.json()["id"]]

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("compose-other", other_epoch),
        ) as other:
            hidden = await other.get(
                f"/mail/api/v1/deliveries/{first.json()['id']}"
            )
            hidden_retry = await other.post(
                f"/mail/api/v1/deliveries/{first.json()['id']}/retry",
                json={},
                headers=SAME_ORIGIN_HEADERS,
            )
        assert hidden.status_code == 404
        assert hidden.json() == {"detail": {"code": "delivery_not_found"}}
        assert hidden_retry.status_code == 404
        assert hidden_retry.json() == {"detail": {"code": "delivery_not_found"}}

    @pytest.mark.asyncio
    async def test_pending_compose_notifies_once_when_retry_publishes(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, _message_id = await _seed_project(
            "api-v1-retry-notification",
            subject="Existing message",
            agent_name="RetryTarget",
            sound="soft",
        )
        epoch = await _make_user("retry-notification-admin")
        real_process = http_module.process_message_delivery
        deferred_once = False

        async def defer_first_processing(delivery_id: str):
            nonlocal deferred_once
            if not deferred_once:
                deferred_once = True
                return http_module.MessageDeliveryProcessingResult(
                    delivery_id=delivery_id,
                    status="pending",
                )
            return await real_process(delivery_id)

        notified: list[str] = []

        async def record_notification(delivery_id: str) -> None:
            notified.append(delivery_id)

        monkeypatch.setattr(
            http_module,
            "process_message_delivery",
            defer_first_processing,
        )
        monkeypatch.setattr(
            http_module,
            "emit_published_delivery_notifications",
            record_notification,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("retry-notification-admin", epoch),
        ) as client:
            accepted = await client.post(
                f"/mail/api/v1/projects/{project_id}/messages",
                json={
                    "idempotency_key": "web-retry-notification-1",
                    "recipients": ["RetryTarget"],
                    "subject": "Retry this durable intent",
                    "body_md": "Publish on retry.",
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            retried = await client.post(
                f"/mail/api/v1/deliveries/{accepted.json()['id']}/retry",
                json={},
                headers=SAME_ORIGIN_HEADERS,
            )
            replay = await client.post(
                f"/mail/api/v1/deliveries/{accepted.json()['id']}/retry",
                json={},
                headers=SAME_ORIGIN_HEADERS,
            )

        assert accepted.status_code == 200
        assert accepted.json()["status"] == "pending"
        assert retried.status_code == replay.status_code == 200
        assert retried.json()["status"] == replay.json()["status"] == "published"
        assert notified == [accepted.json()["id"]]

    @pytest.mark.asyncio
    async def test_operator_reply_is_server_routed_and_viewer_or_foreign_origin_fail(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, message_id = await _seed_project(
            "api-v1-reply",
            subject="Need a human answer",
            agent_name="ReplyTarget",
            sound="high",
        )
        operator_epoch = await _make_user(
            "reply-operator",
            role=webauth.ROLE_MEMBER,
        )
        viewer_epoch = await _make_user("reply-viewer", role=webauth.ROLE_MEMBER)
        await _assign("reply-operator", project_id, webauth.PROJECT_ROLE_OPERATOR)
        await _assign("reply-viewer", project_id, webauth.PROJECT_ROLE_VIEWER)
        path = f"/mail/api/v1/projects/{project_id}/messages/{message_id}/replies"
        payload = {"idempotency_key": "web-reply-1", "body_md": "Approved."}

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("reply-operator", operator_epoch),
        ) as operator:
            response = await operator.post(
                path,
                json=payload,
                headers=SAME_ORIGIN_HEADERS,
            )
            foreign = await operator.post(
                path,
                json={**payload, "idempotency_key": "foreign-origin"},
                headers={"Origin": "https://evil.example", "Host": "test"},
            )
            retry = await operator.post(
                f"/mail/api/v1/deliveries/{response.json()['id']}/retry",
                json={},
                headers=SAME_ORIGIN_HEADERS,
            )

        assert response.status_code == 200
        assert response.json()["status"] == "published"
        assert response.json()["message_id"] is not None
        assert retry.status_code == 200
        assert retry.json()["status"] == "published"
        assert foreign.status_code == 403
        assert foreign.json() == {"detail": {"code": "actor_forbidden"}}

        async with get_session() as session:
            routed = (
                await session.execute(
                    text(
                        "SELECT m.project_id, m.thread_id, m.reply_to, m.subject, "
                        "sender.name, recipient.name "
                        "FROM messages m "
                        "JOIN agents sender ON sender.id = m.sender_id "
                        "JOIN message_recipients mr ON mr.message_id = m.id "
                        "JOIN agents recipient ON recipient.id = mr.agent_id "
                        "WHERE m.delivery_id = :delivery_id"
                    ),
                    {"delivery_id": response.json()["id"]},
                )
            ).one()
        assert tuple(routed) == (
            project_id,
            str(message_id),
            message_id,
            "Re: Need a human answer",
            "HumanOverseer",
            "ReplyTarget",
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("reply-viewer", viewer_epoch),
        ) as viewer:
            denied = await viewer.post(
                path,
                json={**payload, "idempotency_key": "viewer-denied"},
                headers=SAME_ORIGIN_HEADERS,
            )
        assert denied.status_code == 403
        assert denied.json() == {"detail": {"code": "actor_forbidden"}}
