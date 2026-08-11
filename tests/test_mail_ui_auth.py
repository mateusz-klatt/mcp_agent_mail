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
import contextlib
import time
from dataclasses import replace
from pathlib import Path

import pytest
from authlib.jose import jwt
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mcp_agent_mail import config as _config, db as db_module, http as http_module, webauth
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

# A route the gate protects and that renders without any project existing, so a
# failure here is the gate's answer and not a missing fixture.
GUARDED = "/mail/archive/guide"
PASSWORD_PATH = "/mail/api/v1/me/password"
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
        raw_query = "next=%2Fmail%2Fv2%2Fsettings&plus=a+b&empty=&flag"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for caller, headers in callers.items():
                root = await client.get(f"/?{raw_query}", headers=headers)
                root_head = await client.head(f"/?{raw_query}", headers=headers)
                favicon = await client.get("/favicon.ico", headers=headers)
                favicon_head = await client.head("/favicon.ico", headers=headers)

                expected_root_headers = {
                    "cache-control": "no-store",
                    "location": f"/mail/v2/?{raw_query}",
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
        '<script type="module" src="/mail/v2/assets/index-test.js"></script>'
        '<link rel="stylesheet" href="/mail/v2/assets/index-test.css">',
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


class TestMailReactShell:
    """The Vite shell is session-gated, non-cacheable, and path-contained."""

    @pytest.mark.asyncio
    async def test_authenticated_base_redirects_to_canonical_slash_and_keeps_query(
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
            response = await client.get("/mail/v2?tab=projects")

        assert response.status_code == 307
        assert response.headers["location"] == "/mail/v2/?tab=projects"

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
                "/mail/v2/?tab=inbox&filter=high",
                headers={"Accept": "text/html"},
            )

        assert response.status_code == 303
        assert response.headers["location"] == (
            "/mail/login?next=%2Fmail%2Fv2%2F%3Ftab%3Dinbox%26filter%3Dhigh"
        )

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
            response = await client.get("/mail/v2/")

        assert response.status_code == 200
        assert "Hermes React shell marker" in response.text
        assert "Project not found" not in response.text

    @pytest.mark.asyncio
    async def test_root_and_deep_link_serve_non_cacheable_csp_protected_index(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("react-deep-admin")
        expected_csp = (
            "default-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("react-deep-admin", epoch),
        ) as client:
            root = await client.get("/mail/v2/")
            deep = await client.get("/mail/v2/settings/profile?tab=password")

        for response in (root, deep):
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Content-Security-Policy"] == expected_csp
            assert response.headers["Content-Type"].startswith("text/html")
            assert "Hermes React shell marker" in response.text

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
            javascript = await client.get("/mail/v2/assets/index-test.js")
            stylesheet = await client.get("/mail/v2/assets/index-test.css")
            missing = await client.get("/mail/v2/assets/not-built.js")
            bare_namespace = await client.get("/mail/v2/assets")
            encoded_namespace = await client.get("/mail/v2/%61ssets")
            directory = await client.get("/mail/v2/assets/")
            traversal = await client.get(
                "/mail/v2/assets/%2e%2e%2findex.html",
            )

        immutable = "public, max-age=31536000, immutable"
        assert javascript.status_code == 200
        assert javascript.headers["Cache-Control"] == immutable
        assert javascript.headers["Content-Type"].split(";", 1)[0] in {
            "application/javascript",
            "text/javascript",
        }
        assert stylesheet.status_code == 200
        assert stylesheet.headers["Cache-Control"] == immutable
        assert stylesheet.headers["Content-Type"].startswith("text/css")
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
            file_escape = await client.get("/mail/v2/assets/outside-file.js")
            directory_escape = await client.get(
                "/mail/v2/assets/outside-directory/secret.js",
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
            root = await client.get("/mail/v2/")
            deep = await client.get("/mail/v2/settings")
            asset = await client.get("/mail/v2/assets/index-missing.js")

        for response in (root, deep, asset):
            assert response.status_code == 503
            assert response.json() == {"detail": "React Mail UI build is unavailable."}
        assert "Project not found" not in root.text

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

    def test_custom_openapi_exposes_only_the_typed_self_service_apis(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Codegen sees GET/PATCH schemas without publishing legacy mail routes."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)

        schema = app.openapi()
        mail_paths = {
            path: item
            for path, item in schema["paths"].items()
            if path == "/mail" or path.startswith("/mail/")
        }

        assert set(mail_paths) == {
            "/mail/api/v1/me/password",
            "/mail/api/v1/me/preferences",
        }
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
                    "'generation', datetime('now'))"
                )
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
        replacement_generation = "replacement-generation"
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

        assert read.status_code == 401
        assert write.status_code == 401
        assert password_write.status_code == 401
        assert password_write.headers["Cache-Control"] == "no-store"


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
        replacement_generation = "replacement-password-generation"
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
        assert mailbox.status_code == 401
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
    async def test_member_aggregates_filter_counts_messages_projects_sounds_and_siblings(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Every aggregate payload is derived only from assigned projects."""
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
            payload_response = await client.get("/mail/api/unified-inbox", params={"include_projects": "true"})
            root_page = await client.get("/mail")
            legacy_page = await client.get("/mail/unified-inbox")
            projects_page = await client.get("/mail/projects")

        payload = payload_response.json()
        assert payload_response.status_code == 200
        assert payload["total_messages"] == 1
        assert [message["subject"] for message in payload["messages"]] == ["VISIBLE-SUBJECT"]
        assert payload["messages"][0]["can_reply"] is False
        assert [project["slug"] for project in payload["projects"]] == ["visible-project"]
        assert payload["agent_sounds"] == {"VisibleAgent": "high"}
        for response in (root_page, legacy_page, projects_page):
            assert response.status_code == 200
            assert "HIDDEN-SUBJECT" not in response.text
            assert "hidden-project" not in response.text

    @pytest.mark.asyncio
    async def test_only_admin_requests_refresh_cross_project_sibling_profiles(
        self,
        isolated_env,
        monkeypatch,
    ):
        """A member read cannot trigger global profiling, writes, or LLM cost."""
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
            member_projects = await member_client.get("/mail/projects")

        assert member_root.status_code == 200
        assert member_projects.status_code == 200
        assert refresh_calls == []

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("sibling-admin", admin_epoch),
        ) as admin_client:
            admin_root = await admin_client.get("/mail")
            admin_projects = await admin_client.get("/mail/projects")

        assert admin_root.status_code == 200
        assert admin_projects.status_code == 200
        assert len(refresh_calls) == 2

    @pytest.mark.asyncio
    async def test_missing_assignment_is_404_and_viewer_cannot_reply(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Invisible projects are 404 while an insufficient visible role is 403."""
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
        assert compose.status_code == 403
        assert reply.status_code == 403

    @pytest.mark.asyncio
    async def test_operator_can_only_use_the_server_derived_reply_endpoint(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Operators may reply inside assignments but cannot compose arbitrary mail."""
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
                },
                headers=headers,
            )

        assert compose_reply.status_code == 200
        assert compose_new.status_code == 403
        assert arbitrary_send.status_code == 403
        assert reply.status_code == 200
        assert reply.json()["recipients"] == ["OperatorAgent"]

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

        monkeypatch.setattr(http_module.hub, "subscribe_project", lambda _slug: queue)
        monkeypatch.setattr(http_module.hub, "unsubscribe_project", lambda _slug, _queue: None)
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
            "/mail/logout": "public-session",
            "/mail": "aggregate-scoped",
            "/mail/events": "aggregate-scoped",
            "/mail/api/unified-inbox": "aggregate-scoped",
            "/mail/api/v1/me/preferences": "self-only",
            "/mail/projects": "aggregate-scoped",
            "/mail/unified-inbox": "aggregate-scoped",
            "/mail/v2": "session-shell",
            "/mail/v2/": "session-shell",
            "/mail/v2/assets/{asset_path:path}": "session-static-asset",
            "/mail/v2/{spa_path:path}": "session-shell",
            "/mail/api/locks": "admin-only",
            "/mail/api/file-reservations": "service-or-project-scoped",
            "/mail/{project}": "project-guarded",
            "/mail/{project}/inbox/{agent}": "project-guarded",
            "/mail/{project}/message/{mid}": "project-guarded",
            "/mail/{project}/thread/{thread_id}": "project-guarded",
            "/mail/{project}/search": "project-guarded",
            "/mail/{project}/file_reservations": "project-guarded",
            "/mail/{project}/attachments": "project-guarded",
            "/mail/{project}/overseer/compose": "project-role-guarded",
            "/mail/archive/guide": "aggregate-scoped",
            "/mail/archive/activity": "aggregate-scoped",
            "/mail/archive/commit/{sha}": "aggregate-scoped",
            "/mail/archive/timeline": "project-query-guarded",
            "/mail/archive/browser": "project-query-guarded",
            "/mail/archive/browser/{project}/file": "project-guarded",
            "/mail/archive/browser/{project}/download": "project-guarded",
            "/mail/archive/network": "project-query-guarded",
            "/mail/api/projects/{project}/agents": "project-guarded",
            "/mail/archive/time-travel": "aggregate-scoped",
            "/mail/archive/time-travel/snapshot": "project-query-guarded",
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
            "aggregate-scoped",
            "admin-only",
            "service-or-project-scoped",
            "project-guarded",
            "project-role-guarded",
            "project-query-guarded",
            "self-only",
            "session-shell",
            "session-static-asset",
        }
