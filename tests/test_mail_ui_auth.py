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
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

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
from mcp_agent_mail.models import MailUiLocale

BEARER = "mail-ui-gate-bearer"
SECRET = "mail-ui-gate-session-secret-0123456789"
EXPECTED_FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'
    b'  <rect width="64" height="64" rx="14" fill="#17133b"/>\n'
    b'  <g fill="none" stroke-width="4" stroke-linecap="round">\n'
    b'    <path d="M8 47a24 24 0 0 1 48 0" stroke="#ef4444"/>\n'
    b'    <path d="M12 47a20 20 0 0 1 40 0" stroke="#f97316"/>\n'
    b'    <path d="M16 47a16 16 0 0 1 32 0" stroke="#facc15"/>\n'
    b'    <path d="M20 47a12 12 0 0 1 24 0" stroke="#22c55e"/>\n'
    b'    <path d="M24 47a8 8 0 0 1 16 0" stroke="#38bdf8"/>\n'
    b'    <path d="M28 47a4 4 0 0 1 8 0" stroke="#8b5cf6"/>\n'
    b"  </g>\n"
    b"</svg>\n"
)
FAVICON_LINK = '<link rel="icon" href="/iris-rainbow.svg" type="image/svg+xml" sizes="any" />'
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
LOGIN_CSP = (
    "default-src 'self'; script-src 'none'; style-src 'self'; connect-src 'none'; "
    "img-src 'self' data:; font-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

# A route the gate protects and that renders without any project existing, so a
# failure here is the gate's answer and not a missing fixture.
GUARDED = "/mail/api/v1/projects"
PROFILE_PATH = "/mail/api/v1/me/profile"
PASSWORD_PATH = "/mail/api/v1/me/password"
ADMIN_ACCESS_PATH = "/mail/api/v1/admin/access"
COUNTRY_FLAG_FONT_PATH = "/mail/assets/TwemojiCountryFlags.woff2"
COUNTRY_FLAG_FONT_BYTES = b"wOF2test-country-flags"
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
                favicon = await client.get("/iris-rainbow.svg", headers=headers)
                favicon_head = await client.head("/iris-rainbow.svg", headers=headers)

                expected_root_headers = {
                    "cache-control": "no-store, no-transform",
                    "content-security-policy": REACT_CSP,
                    "referrer-policy": "strict-origin-when-cross-origin",
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
            favicon_post = await client.post("/iris-rainbow.svg")
            favicon_directory = await client.get("/iris-rainbow.svg/")
            retired_favicon = await client.get("/favicon.ico")

        for response in (root_post, favicon_post, favicon_directory, retired_favicon):
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
            HTTP_PATH="/iris-rainbow.svg/",
        )
        health_call = {
            "jsonrpc": "2.0",
            "id": "favicon-mcp-health",
            "method": "tools/call",
            "params": {"name": "health_check", "arguments": {}},
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            anonymous_get = await client.get("/iris-rainbow.svg")
            authenticated_post = await client.post(
                "/iris-rainbow.svg",
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


async def _compose_lifetime_refs(
    project_id: int,
    *agent_names: str,
) -> tuple[str, list[dict[str, int | str]]]:
    """Return the immutable project/agent references accepted by typed compose."""
    async with get_session() as session:
        project_generation = str(
            (
                await session.execute(
                    text(
                        "SELECT project_generation FROM projects WHERE id = :project_id"
                    ),
                    {"project_id": project_id},
                )
            ).scalar_one()
        )
        rows = await session.execute(
            text(
                "SELECT id, agent_generation, name FROM agents "
                "WHERE project_id = :project_id"
            ),
            {"project_id": project_id},
        )
        agents_by_name = {
            str(row["name"]): {
                "agent_id": int(row["id"]),
                "expected_agent_generation": str(row["agent_generation"]),
            }
            for row in rows.mappings().all()
        }
    assert set(agent_names) <= set(agents_by_name)
    return project_generation, [agents_by_name[name] for name in agent_names]


async def _publish_inbound_message(
    slug: str,
    *,
    subject: str,
    sender_name: str,
    cross_project: bool = False,
) -> dict[str, int | str]:
    """Publish an inbound message with immutable sender delivery provenance."""
    from mcp_agent_mail.delivery import (
        DeliveryActorSnapshot,
        DeliveryAgentSnapshot,
        DeliveryProjectSnapshot,
        DeliveryRecipientSnapshot,
        MessageDeliveryRequest,
        accept_message_delivery,
        process_message_delivery,
    )
    from mcp_agent_mail.models import Agent, AgentLink, Project

    await ensure_schema()
    async with get_session() as session:
        target_project = Project(slug=slug, human_key=f"/{slug}")
        source_project = (
            Project(slug=f"{slug}-source", human_key=f"/{slug}-source")
            if cross_project
            else target_project
        )
        session.add_all([target_project, source_project] if cross_project else [target_project])
        await session.flush()
        assert target_project.id is not None
        assert source_project.id is not None
        sender = Agent(
            project_id=source_project.id,
            name=sender_name,
            program="pytest",
            model="test",
            contact_policy="open",
        )
        recipient = Agent(
            project_id=target_project.id,
            name=f"{slug}-recipient",
            program="pytest",
            model="test",
            contact_policy="open",
        )
        session.add_all([sender, recipient])
        await session.flush()
        assert sender.id is not None
        assert recipient.id is not None
        if cross_project:
            session.add(
                AgentLink(
                    a_project_id=source_project.id,
                    a_agent_id=sender.id,
                    b_project_id=target_project.id,
                    b_agent_id=recipient.id,
                    status="approved",
                )
            )
        await session.commit()

    target_snapshot = DeliveryProjectSnapshot(
        project_id=target_project.id,
        slug=target_project.slug,
        generation=target_project.project_generation,
    )
    source_snapshot = DeliveryProjectSnapshot(
        project_id=source_project.id,
        slug=source_project.slug,
        generation=source_project.project_generation,
    )
    sender_snapshot = DeliveryAgentSnapshot(
        agent_id=sender.id,
        name=sender.name,
        generation=sender.agent_generation,
        project=source_snapshot,
    )
    recipient_snapshot = DeliveryAgentSnapshot(
        agent_id=recipient.id,
        name=recipient.name,
        generation=recipient.agent_generation,
        project=target_snapshot,
    )
    acceptance = await accept_message_delivery(
        MessageDeliveryRequest(
            target_project=target_snapshot,
            sender=sender_snapshot,
            actor=DeliveryActorSnapshot.agent(sender_snapshot),
            recipients=(DeliveryRecipientSnapshot(kind="to", agent=recipient_snapshot),),
            idempotency_key=f"test-inbound:{slug}",
            subject=subject,
            body_md=f"Body for {subject}",
        )
    )
    processing = await process_message_delivery(acceptance.delivery_id)
    assert processing.status == "published"
    assert processing.message_id is not None
    return {
        "project_id": target_snapshot.project_id,
        "project_generation": target_snapshot.generation,
        "message_id": processing.message_id,
        "sender_id": sender_snapshot.agent_id,
        "sender_generation": sender_snapshot.generation,
        "sender_name": sender_snapshot.name,
        "sender_project_id": source_snapshot.project_id,
        "sender_project_generation": source_snapshot.generation,
        "sender_project_slug": source_snapshot.slug,
    }


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
        "<!doctype html><title>Iris React shell marker</title>"
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
    (assets_root / "TwemojiCountryFlags.woff2").write_bytes(
        COUNTRY_FLAG_FONT_BYTES
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
        assert response.headers["Cache-Control"] == "no-store, no-transform"
        assert response.headers["Content-Security-Policy"] == LOGIN_CSP
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
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
            assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
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
            assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
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
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
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
        assert "Iris React shell marker" in response.text
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
            assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.headers["Content-Type"].startswith("text/html")
        assert "Iris React shell marker" in root.text
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
            login_flag_font = await anonymous_client.get(COUNTRY_FLAG_FONT_PATH)
            login_flag_font_head = await anonymous_client.head(
                COUNTRY_FLAG_FONT_PATH
            )
            protected_runtime = await anonymous_client.get(
                "/mail/assets/legacy.js"
            )
            protected_other_font = await anonymous_client.get(
                "/mail/assets/UnexpectedCountryFlags.woff2"
            )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("legacy-airgap-admin", epoch),
        ) as authenticated_client:
            inbox = await authenticated_client.get("/mail")

        assert login.status_code == 200
        assert login.headers["Cache-Control"] == "no-store, no-transform"
        assert login.headers["Content-Security-Policy"] == LOGIN_CSP
        assert login.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert login.headers["X-Content-Type-Options"] == "nosniff"
        assert login.headers["X-Frame-Options"] == "DENY"
        assert "<title>Sign in · Iris</title>" in login.text
        assert '<span aria-hidden="true">🌈</span> Iris' in login.text
        assert FAVICON_LINK in login.text
        assert 'href="/mail/assets/legacy.css"' in login.text
        assert login.text.count('class="locale-flag ') == len(MailUiLocale) + 1
        assert "/mail/v2" not in login.text

        assert inbox.status_code == 200
        assert inbox.headers["Cache-Control"] == "no-store, no-transform"
        assert inbox.headers["Content-Security-Policy"] == REACT_CSP
        assert "Iris React shell marker" in inbox.text

        assert login_stylesheet.status_code == 200
        assert login_stylesheet_head.status_code == 200
        for response in (login_stylesheet, login_stylesheet_head):
            assert response.headers["Cache-Control"] == "no-cache, no-transform"
            assert response.headers["Content-Type"].startswith("text/css")
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert login_stylesheet.text == "[x-cloak] { display: none !important; }\n"
        assert login_stylesheet_head.content == b""
        immutable = "public, max-age=31536000, immutable, no-transform"
        assert login_flag_font.status_code == 200
        assert login_flag_font.content == COUNTRY_FLAG_FONT_BYTES
        assert login_flag_font_head.status_code == 200
        assert login_flag_font_head.content == b""
        for response in (login_flag_font, login_flag_font_head):
            assert response.headers["Cache-Control"] == immutable
            assert response.headers["Content-Type"].split(";", 1)[0] == "font/woff2"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert protected_runtime.status_code == 401
        assert protected_other_font.status_code == 401

    def test_login_catalog_matches_the_closed_locale_and_flag_contract(self):
        assert tuple(http_module._MAIL_LOGIN_TEXT) == tuple(MailUiLocale)
        assert tuple(http_module._MAIL_LOGIN_LOCALE_PRESENTATION) == tuple(MailUiLocale)
        assert " ".join(
            presentation.flag
            for presentation in http_module._MAIL_LOGIN_LOCALE_PRESENTATION.values()
        ) == (
            "🇸🇦 🇧🇩 🇧🇦 🇨🇿 🇩🇰 🇩🇪 🇬🇷 🇬🇧 🇪🇸 🇮🇷 🇫🇮 🇵🇭 🇫🇷 🇮🇪 🇮🇱 "
            "🇮🇳 🇭🇷 🇭🇺 🇦🇲 🇮🇩 🇮🇸 🇮🇹 🇯🇵 🇰🇷 🇱🇹 🇱🇻 🇲🇾 🇲🇲 🇳🇱 🇳🇴 "
            "🇵🇱 🇵🇹 🇷🇴 🇷🇺 🇸🇰 🇦🇱 🇷🇸 🇸🇪 🇰🇪 🇹🇭 🇹🇷 🇺🇦 🇻🇳 🇹🇼 🇨🇳"
        )
        for text_catalog in http_module._MAIL_LOGIN_TEXT.values():
            assert len(text_catalog) == len(text_catalog._fields) == 8
            assert all(value and value == value.strip() for value in text_catalog)

    @pytest.mark.asyncio
    async def test_login_renders_every_locale_without_script_or_remote_dependency(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            responses = {
                locale: await client.get(
                    "/mail/login",
                    params={"lang": locale.value, "next": "/mail#settings"},
                )
                for locale in MailUiLocale
            }

        for locale, response in responses.items():
            direction = "rtl" if locale in {MailUiLocale.AR, MailUiLocale.FA, MailUiLocale.HE} else "ltr"
            text_catalog = http_module._MAIL_LOGIN_TEXT[locale]
            assert response.status_code == 200
            assert response.headers["Content-Security-Policy"] == LOGIN_CSP
            assert f'<html lang="{locale.value}" dir="{direction}">' in response.text
            assert f"<title>{text_catalog.sign_in} · Iris</title>" in response.text
            assert f'name="lang" value="{locale.value}"' in response.text
            assert response.text.count("hreflang=") == len(MailUiLocale)
            assert "min-h-[44px]" in response.text
            assert "<script" not in response.text
            assert "http://" not in response.text
            assert "https://" not in response.text

    @pytest.mark.asyncio
    async def test_login_language_selection_is_canonical_bounded_and_preserves_next(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            canonical = await client.get(
                "/mail/login?lang=ZH-hAnT&next=%2Fmail%23settings"
            )
            browser = await client.get(
                "/mail/login",
                headers={"Accept-Language": "de;q=0.4, ar-SA;q=0.9"},
            )
            explicit_invalid = await client.get(
                "/mail/login?lang=fr-FR",
                headers={"Accept-Language": "fr"},
            )
            oversized = await client.get(
                "/mail/login",
                headers={
                    "Accept-Language": "fr," + "x" * http_module._MAIL_LOGIN_ACCEPT_LANGUAGE_MAX_BYTES
                },
            )

        assert '<html lang="zh-Hant" dir="ltr">' in canonical.text
        assert 'name="lang" value="zh-Hant"' in canonical.text
        assert (
            'href="/mail/login?lang=fr&amp;next=%2Fmail%23settings"'
            in canonical.text
        )
        assert '<html lang="ar" dir="rtl">' in browser.text
        assert http_module._MAIL_LOGIN_TEXT[MailUiLocale.AR].hint in browser.text
        assert '<html lang="en" dir="ltr">' in explicit_invalid.text
        assert '<html lang="en" dir="ltr">' in oversized.text

    @pytest.mark.asyncio
    async def test_invalid_and_throttled_login_preserve_and_localize_submitted_language(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        await ensure_schema()
        monkeypatch.setattr(webauth, "authenticate", lambda *_args: False)
        headers = {
            "Origin": "http://test",
            "Referer": "http://test/mail/login",
            "Host": "test",
        }
        http_module._login_failures.clear()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                invalid = await client.post(
                    "/mail/login",
                    data={
                        "username": "unknown",
                        "password": "not-the-password",
                        "next": "/mail#settings",
                        "lang": "PL",
                    },
                    headers=headers,
                )
                monkeypatch.setattr(http_module, "_login_throttled", lambda _key: True)
                throttled = await client.post(
                    "/mail/login",
                    data={
                        "username": "unknown",
                        "password": "not-the-password",
                        "next": "//foreign.example/path",
                        "lang": "fr",
                    },
                    headers=headers,
                )
        finally:
            http_module._login_failures.clear()

        assert invalid.status_code == 401
        assert '<html lang="pl" dir="ltr">' in invalid.text
        assert 'name="lang" value="pl"' in invalid.text
        assert 'name="next" value="/mail#settings"' in invalid.text
        assert http_module._MAIL_LOGIN_TEXT[MailUiLocale.PL].invalid_credentials in invalid.text
        assert "not-the-password" not in invalid.text
        assert throttled.status_code == 429
        assert '<html lang="fr" dir="ltr">' in throttled.text
        assert 'name="lang" value="fr"' in throttled.text
        assert 'name="next" value="/mail"' in throttled.text
        assert http_module._MAIL_LOGIN_TEXT[MailUiLocale.FR].throttled in throttled.text

        async with get_session() as session:
            unchanged_locale = (
                await session.execute(
                    text(
                        "SELECT preferred_ui_locale FROM ui_users "
                        "WHERE username = 'unknown'"
                    )
                )
            ).scalar_one_or_none()
        assert unchanged_locale is None

    @pytest.mark.asyncio
    async def test_successful_login_persists_locale_for_the_authenticated_react_session(
        self,
        isolated_env,
        monkeypatch,
    ):
        password = "correct localized login password"
        _settings, app = _build(
            monkeypatch,
            MAIL_UI_SESSION_SECRET=SECRET,
            MAIL_UI_COOKIE_SECURE="false",
        )
        await _make_user("localized-login-user", password=password)
        headers = {
            "Origin": "http://test",
            "Referer": "http://test/mail/login",
            "Host": "test",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            response = await client.post(
                "/mail/login",
                data={
                    "username": "localized-login-user",
                    "password": password,
                    "next": "/mail#settings",
                    "lang": "ZH-hAnT",
                },
                headers=headers,
            )
            preferences = await client.get("/mail/api/v1/me/preferences")

        assert response.status_code == 303
        assert response.headers["Location"] == "/mail#settings"
        assert "set-cookie" in response.headers
        assert preferences.status_code == 200
        assert preferences.json()["stored"]["preferred_ui_locale"] == "zh-Hant"
        assert preferences.json()["effective"]["ui_locale"] == "zh-Hant"

    @pytest.mark.asyncio
    async def test_login_account_replacement_race_fails_closed_without_cookie(
        self,
        isolated_env,
        monkeypatch,
    ):
        from mcp_agent_mail.models import UiUser

        password = "original login account password"
        replacement_password = "replacement login account password"
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        await _make_user("login-replacement-race", password=password)
        replacement_generation = "d" * 64
        replacement_hash = webauth.hash_password(replacement_password)
        real_authenticate = webauth.authenticate
        replacement_done = asyncio.Event()

        async def replace_account_lifetime() -> None:
            try:
                async with get_session() as session:
                    row = (
                        await session.execute(
                            text(
                                "SELECT id FROM ui_users "
                                "WHERE username = 'login-replacement-race'"
                            )
                        )
                    ).one()
                    user_id = int(row[0])
                    await session.execute(
                        text("DELETE FROM ui_users WHERE id = :user_id"),
                        {"user_id": user_id},
                    )
                    session.add(
                        UiUser(
                            id=user_id,
                            username="login-replacement-race",
                            password_hash=replacement_hash,
                            session_generation=replacement_generation,
                        )
                    )
                    await session.commit()
            finally:
                replacement_done.set()

        def authenticate_then_replace(
            username: str,
            presented_password: str,
            stored_password_hash: str | None,
        ) -> bool:
            result = real_authenticate(
                username,
                presented_password,
                stored_password_hash,
            )
            if result:
                asyncio.get_running_loop().create_task(replace_account_lifetime())
            return result

        monkeypatch.setattr(webauth, "authenticate", authenticate_then_replace)
        # The scheduled replacement must complete before the CAS touch starts.
        real_get_session = http_module.get_session
        session_calls = 0

        def delayed_get_session():
            nonlocal session_calls
            session_calls += 1
            if session_calls != 2:
                return real_get_session()

            class _DelayedSession:
                async def __aenter__(self):
                    await replacement_done.wait()
                    return await self._manager.__aenter__()

                async def __aexit__(self, *args):
                    return await self._manager.__aexit__(*args)

                def __init__(self):
                    self._manager = real_get_session()

            return _DelayedSession()

        monkeypatch.setattr(http_module, "get_session", delayed_get_session)
        headers = {
            "Origin": "http://test",
            "Referer": "http://test/mail/login",
            "Host": "test",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/mail/login",
                data={
                    "username": "login-replacement-race",
                    "password": password,
                    "next": "/mail",
                    "lang": "pl",
                },
                headers=headers,
            )

        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        assert http_module._MAIL_LOGIN_TEXT[MailUiLocale.PL].invalid_credentials in response.text
        async with real_get_session() as session:
            replacement = (
                await session.execute(
                    text(
                        "SELECT session_generation, preferred_ui_locale, last_login_ts "
                        "FROM ui_users WHERE username = 'login-replacement-race'"
                    )
                )
            ).one()
        assert replacement == (replacement_generation, "en", None)

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
            country_flag_font = await client.get(COUNTRY_FLAG_FONT_PATH)
            aliased_country_flag_font = await client.get(
                "/mail/assets/%2e%2fTwemojiCountryFlags.woff2"
            )
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
        assert country_flag_font.status_code == 200
        assert country_flag_font.content == COUNTRY_FLAG_FONT_BYTES
        assert country_flag_font.headers["Cache-Control"] == immutable
        assert country_flag_font.headers["Content-Type"].split(";", 1)[0] == "font/woff2"
        assert country_flag_font.headers["X-Content-Type-Options"] == "nosniff"
        assert aliased_country_flag_font.status_code == 404
        assert missing.status_code == 404
        for response in (bare_namespace, encoded_namespace, directory):
            assert response.status_code == 404
            assert "Iris React shell marker" not in response.text
        assert traversal.status_code == 404
        assert "Iris React shell marker" not in traversal.text

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
    async def test_only_legacy_routes_without_exact_successors_remain_404(
        self,
        isolated_env,
        monkeypatch,
        tmp_path,
    ):
        _install_react_dist(monkeypatch, tmp_path)
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, _message_id = await _seed_project(
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
            "/mail/react-cutover-project/inbox/CutoverAgent",
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
            assert "Iris React shell marker" not in response.text, path

    @pytest.mark.asyncio
    async def test_retired_namespaces_are_404_before_authentication(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        retired_paths = [
            "/mail/v2",
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
    async def test_enumerated_upstream_bookmarks_redirect_only_after_rbac(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        visible_id, visible_message = await _seed_project(
            "bookmark-visible",
            subject="Bookmark target",
            agent_name="BookmarkAgent",
            sound="soft",
        )
        _hidden_id, hidden_message = await _seed_project(
            "bookmark-hidden",
            subject="Hidden bookmark target",
            agent_name="HiddenBookmarkAgent",
            sound="low",
        )
        epoch = await _make_user("bookmark-member", role=webauth.ROLE_MEMBER)
        await _assign(
            "bookmark-member",
            visible_id,
            webauth.PROJECT_ROLE_VIEWER,
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("bookmark-member", epoch),
        ) as client:
            projects = await client.get("/mail/projects")
            unified = await client.head("/mail/unified-inbox")
            project = await client.get("/mail/bookmark-visible")
            message = await client.get(
                f"/mail/bookmark-visible/message/{visible_message}"
            )
            search = await client.get(
                "/mail/bookmark-visible/search",
                params={
                    "q": 'release "exact phrase"',
                    "scope": "body",
                    "order": "time",
                },
            )
            hidden = [
                await client.get("/mail/bookmark-hidden"),
                await client.get(
                    f"/mail/bookmark-hidden/message/{hidden_message}"
                ),
                await client.get(
                    "/mail/bookmark-hidden/search",
                    params={"q": "Hidden"},
                ),
                await client.get(
                    f"/mail/bookmark-visible/message/{hidden_message}"
                ),
            ]
            unsafe = await client.post(
                "/mail/bookmark-visible",
                headers=SAME_ORIGIN_HEADERS,
            )

        assert projects.status_code == 307
        assert projects.headers["location"] == "/mail#projects"
        assert unified.status_code == 307
        assert unified.headers["location"] == "/mail#inbox"
        assert project.status_code == 307
        assert project.headers["location"] == f"/mail#inbox?project={visible_id}"
        assert message.status_code == 307
        assert message.headers["location"] == (
            f"/mail#message/{visible_id}/{visible_message}"
        )
        assert search.status_code == 307
        search_location = search.headers["location"]
        assert search_location.startswith("/mail#search?")
        search_params = dict(
            item.split("=", 1)
            for item in search_location.split("?", 1)[1].split("&")
        )
        assert search_params == {
            "q": "release+%22exact+phrase%22",
            "project": str(visible_id),
            "scope": "body",
            "order": "newest",
        }
        for response in [*hidden, unsafe]:
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}

    @pytest.mark.asyncio
    async def test_thread_bookmark_is_exact_encoded_and_non_disclosing(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        visible_id, starter_message_id = await _seed_project(
            "thread-bookmark-visible",
            subject="Visible thread target",
            agent_name="ThreadBookmarkAgent",
            sound="soft",
        )
        _hidden_id, hidden_message_id = await _seed_project(
            "thread-bookmark-hidden",
            subject="Hidden thread target",
            agent_name="HiddenThreadBookmarkAgent",
            sound="low",
        )
        thread_id = "release-\N{GREEK SMALL LETTER ALPHA} beta?#!~*'()"
        boundary_thread_id = "\N{RAINBOW}" * 128
        overlong_thread_id = "\N{RAINBOW}" * 129
        hidden_thread_id = "hidden-thread"
        async with get_session() as session:
            await session.execute(
                text("UPDATE messages SET thread_id = :thread_id WHERE id = :message_id"),
                {"thread_id": thread_id, "message_id": starter_message_id},
            )
            await session.execute(
                text("UPDATE messages SET thread_id = :thread_id WHERE id = :message_id"),
                {"thread_id": hidden_thread_id, "message_id": hidden_message_id},
            )
            sender_id = int(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM agents WHERE project_id = :project_id "
                            "AND name = 'ThreadBookmarkAgent'"
                        ),
                        {"project_id": visible_id},
                    )
                ).scalar_one()
            )
            await session.execute(
                text(
                    "INSERT INTO messages "
                    "(project_id, sender_id, thread_id, subject, body_md, "
                    "importance, ack_required, created_ts, attachments) "
                    "VALUES (:project_id, :sender_id, :thread_id, "
                    "'Unicode boundary', 'Unicode boundary body', "
                    "'normal', 0, datetime('now'), '[]')"
                ),
                {
                    "project_id": visible_id,
                    "sender_id": sender_id,
                    "thread_id": boundary_thread_id,
                },
            )
            await session.commit()

        epoch = await _make_user("thread-bookmark-member", role=webauth.ROLE_MEMBER)
        await _assign(
            "thread-bookmark-member",
            visible_id,
            webauth.PROJECT_ROLE_VIEWER,
        )
        encoded_thread_id = quote(thread_id, safe="-_.!~*'()")
        bookmark_path = (
            f"/mail/thread-bookmark-visible/thread/{encoded_thread_id}"
        )
        boundary_bookmark_path = (
            "/mail/thread-bookmark-visible/thread/"
            + quote(boundary_thread_id, safe="-_.!~*'()")
        )
        overlong_bookmark_path = (
            "/mail/thread-bookmark-visible/thread/"
            + quote(overlong_thread_id, safe="-_.!~*'()")
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as anonymous:
            login_redirect = await anonymous.get(
                bookmark_path,
                headers={"Accept": "text/html"},
            )
            boundary_login_redirect = await anonymous.get(
                boundary_bookmark_path,
                headers={"Accept": "text/html"},
            )
            boundary_login = await anonymous.get(
                boundary_login_redirect.headers["location"]
            )
            overlong_login = await anonymous.get(
                "/mail/login",
                params={"next": overlong_bookmark_path},
            )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("thread-bookmark-member", epoch),
        ) as client:
            encoded = await client.get(bookmark_path)
            numeric_starter = await client.get(
                f"/mail/thread-bookmark-visible/thread/{starter_message_id}"
            )
            bookmark_head = await client.head(bookmark_path)
            hidden = await client.get(
                f"/mail/thread-bookmark-hidden/thread/{hidden_thread_id}"
            )
            missing_project = await client.get(
                "/mail/thread-bookmark-missing/thread/hidden-thread"
            )
            missing_thread = await client.get(
                "/mail/thread-bookmark-visible/thread/not-a-thread"
            )
            unsupported_query = await client.get(
                bookmark_path,
                params={"debug": "1"},
            )
            unicode_boundary = await client.get(
                boundary_bookmark_path
            )
            unicode_overlong = await client.get(overlong_bookmark_path)
            hostile = [
                await client.get(
                    bookmark_path.replace("/release-", "/%72elease-", 1)
                ),
                await client.get(
                    bookmark_path.replace("%CE%B1", "%ce%b1", 1)
                ),
                await client.get("/mail/thread-bookmark-visible/thread/%2E"),
                await client.get("/mail/thread-bookmark-visible/thread/%2E%2E"),
                await client.get("/mail/thread-bookmark-visible/thread/%00"),
                await client.get(
                    "/mail/thread-bookmark-visible/thread/release%2Fphase"
                ),
                await client.get(
                    f"/mail/thread-bookmark-visible/thread/{'x' * 129}"
                ),
                await client.get("/mail/thread-bookmark-visible/thread"),
                await client.get("/mail/thread-bookmark-visible/thread/"),
                await client.get("/mail/thread-bookmark-visible/threads/value"),
                await client.get("/mail/v2/thread/value"),
                await client.post(
                    bookmark_path,
                    headers=SAME_ORIGIN_HEADERS,
                ),
            ]

        assert login_redirect.status_code == 303
        assert login_redirect.headers["location"] == (
            "/mail/login?next=" + quote(bookmark_path, safe="")
        )
        assert boundary_login_redirect.status_code == 303
        assert boundary_login_redirect.headers["location"] == (
            "/mail/login?next=" + quote(boundary_bookmark_path, safe="")
        )
        assert boundary_login.status_code == 200
        assert f'value="{boundary_bookmark_path}"' in boundary_login.text
        assert overlong_login.status_code == 200
        assert 'value="/mail"' in overlong_login.text
        expected_location = (
            f"/mail#thread/{visible_id}/{encoded_thread_id}"
        )
        for response in (encoded, bookmark_head):
            assert response.status_code == 307
            assert response.headers["location"] == expected_location
        assert numeric_starter.status_code == 307
        assert numeric_starter.headers["location"] == (
            f"/mail#thread/{visible_id}/{starter_message_id}"
        )
        assert unicode_boundary.status_code == 307
        assert unicode_boundary.headers["location"] == (
            f"/mail#thread/{visible_id}/"
            + quote(boundary_thread_id, safe="-_.!~*'()")
        )
        for response in (
            hidden,
            missing_project,
            missing_thread,
            unsupported_query,
            unicode_overlong,
            *hostile,
        ):
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}

    @pytest.mark.asyncio
    async def test_dynamic_bookmark_login_is_non_disclosing_and_next_is_bounded(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            visible_shape = await client.get(
                "/mail/does-not-disclose/search?q=needle",
                headers={"Accept": "text/html"},
            )
            rejected = await client.get(
                "/mail/v2",
                headers={"Accept": "text/html"},
            )
            login = await client.get(
                "/mail/login",
                params={
                    "next": "/mail/does-not-disclose/search?q=needle&scope=all"
                },
            )
            unsafe_login = await client.get(
                "/mail/login",
                params={"next": "/mail/v2/settings"},
            )

        assert visible_shape.status_code == 303
        assert "does-not-disclose" in visible_shape.headers["location"]
        assert rejected.status_code == 404
        assert login.status_code == 200
        assert (
            'value="/mail/does-not-disclose/search?q=needle&amp;scope=all"'
            in login.text
        )
        assert 'value="/mail"' in unsafe_login.text

    @pytest.mark.asyncio
    async def test_static_bookmark_rejects_query_and_unmapped_routes_stay_404(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("bookmark-deny-admin")
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("bookmark-deny-admin", epoch),
        ) as client:
            responses = [
                await client.get("/mail/projects?next=https://evil.invalid"),
                await client.get("/mail/unified-inbox?debug=1"),
                await client.get("/mail/v2"),
                await client.get("/mail/v2/"),
                await client.get("/mail/v2/settings"),
                await client.get("/mail/archive"),
                await client.get("/mail/assets"),
                await client.get("/mail/api"),
            ]
        for response in responses:
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}

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
        assert "Iris React shell marker" not in preferences.text
        assert password.status_code == 422
        assert password.headers["Content-Type"].startswith("application/json")
        assert "Iris React shell marker" not in password.text


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
            "/mail/api/v1/projects/{project_id}/agents",
            "/mail/api/v1/reservations",
            "/mail/api/v1/search",
            "/mail/api/v1/deliveries/{delivery_id}",
            "/mail/api/v1/deliveries/{delivery_id}/retry",
            "/mail/api/v1/projects/{project_id}/messages",
            "/mail/api/v1/projects/{project_id}/messages/{message_id}",
            "/mail/api/v1/projects/{project_id}/messages/{message_id}/replies",
            "/mail/api/v1/projects/{project_id}/threads",
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
        assert schema["components"]["schemas"]["MailUiLocale"]["enum"] == [
            locale.value for locale in MailUiLocale
        ]
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
            "/mail/api/v1/projects/{project_id}/agents": (
                "MailUiProjectAgentsResponse"
            ),
            "/mail/api/v1/inbox": "MailUiInboxResponse",
            "/mail/api/v1/reservations": "MailUiReservationsResponse",
            "/mail/api/v1/search": "MailUiSearchResponse",
            "/mail/api/v1/projects/{project_id}/messages/{message_id}": (
                "MailUiMessageDetail"
            ),
            "/mail/api/v1/projects/{project_id}/threads": (
                "MailUiThreadResponse"
            ),
        }
        for path, model_name in typed_get_responses.items():
            assert set(mail_paths[path]) == {"get"}
            assert mail_paths[path]["get"]["responses"]["200"]["content"][
                "application/json"
            ]["schema"] == {"$ref": f"#/components/schemas/{model_name}"}

        thread_parameters = {
            parameter["name"]: parameter
            for parameter in mail_paths[
                "/mail/api/v1/projects/{project_id}/threads"
            ]["get"]["parameters"]
        }
        assert set(thread_parameters) == {"project_id", "thread_id", "limit", "cursor"}
        assert thread_parameters["thread_id"]["in"] == "query"
        assert thread_parameters["thread_id"]["required"] is True

        thread_schema = schema["components"]["schemas"]["MailUiThreadResponse"]
        assert thread_schema["additionalProperties"] is False
        assert set(thread_schema["properties"]) == {
            "subject",
            "items",
            "total",
            "next_cursor",
        }
        assert set(thread_schema["required"]) == {
            "subject",
            "items",
            "total",
            "next_cursor",
        }
        assert thread_schema["properties"]["subject"] == {
            "title": "Subject",
            "type": "string",
        }

        agent_directory = mail_paths[
            "/mail/api/v1/projects/{project_id}/agents"
        ]["get"]
        for status_code in ("401", "403", "404", "409", "500"):
            assert agent_directory["responses"][status_code]["content"][
                "application/json"
            ]["schema"] == {
                "$ref": "#/components/schemas/MailUiDeliveryErrorResponse"
            }
        assert agent_directory["responses"]["422"]["content"]["application/json"][
            "schema"
        ] == {
            "$ref": "#/components/schemas/MailUiDeliveryOrValidationErrorResponse"
        }
        directory_schema = schema["components"]["schemas"][
            "MailUiProjectAgentsResponse"
        ]
        assert directory_schema["additionalProperties"] is False
        assert set(directory_schema["properties"]) == {
            "project_id",
            "project_generation",
            "items",
            "total",
        }
        directory_item_schema = schema["components"]["schemas"][
            "MailUiAgentDirectoryItem"
        ]
        assert directory_item_schema["additionalProperties"] is False
        assert set(directory_item_schema["properties"]) == {
            "agent_id",
            "agent_generation",
            "name",
            "display_name",
            "notify_sound",
        }

        search_operation = mail_paths["/mail/api/v1/search"]["get"]
        assert {
            parameter["name"] for parameter in search_operation["parameters"]
        } == {"q", "project_id", "scope", "order", "limit", "cursor"}
        for status_code in ("401", "403", "404", "503"):
            assert search_operation["responses"][status_code]["content"][
                "application/json"
            ]["schema"] == {
                "$ref": "#/components/schemas/MailUiDeliveryErrorResponse"
            }
        assert search_operation["responses"]["422"]["content"][
            "application/json"
        ]["schema"] == {
            "$ref": "#/components/schemas/MailUiDeliveryOrValidationErrorResponse"
        }
        search_response_schema = schema["components"]["schemas"][
            "MailUiSearchResponse"
        ]
        assert search_response_schema["additionalProperties"] is False
        assert set(search_response_schema["properties"]) == {"items", "next_cursor"}
        search_item_schema = schema["components"]["schemas"]["MailUiSearchItem"]
        assert set(search_item_schema["properties"]) == {
            "id",
            "project_id",
            "project_slug",
            "subject",
            "sender",
            "sender_name",
            "sender_display_name",
            "sender_notify_sound",
            "importance",
            "ack_required",
            "thread_id",
            "reply_to",
            "created_ts",
            "can_reply",
            "snippet",
        }
        assert search_item_schema["properties"]["snippet"]["maxLength"] == 320
        assert not set(search_item_schema["properties"]) & {
            "body_md",
            "to",
            "cc",
            "bcc",
            "recipients",
        }

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
        compose_schema = schema["components"]["schemas"]["MailUiComposeRequest"]
        assert set(compose_schema["properties"]) == {
            "idempotency_key",
            "expected_project_generation",
            "recipients",
            "subject",
            "body_md",
            "thread_id",
        }
        assert compose_schema["properties"]["recipients"]["items"] == {
            "$ref": "#/components/schemas/MailUiComposeRecipient"
        }
        compose_recipient_schema = schema["components"]["schemas"][
            "MailUiComposeRecipient"
        ]
        assert set(compose_recipient_schema["properties"]) == {
            "agent_id",
            "expected_agent_generation",
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
        reply_schema = schema["components"]["schemas"]["MailUiReplyRequest"]
        assert set(reply_schema["properties"]) == {
            "idempotency_key",
            "expected_sender_agent_id",
            "expected_sender_agent_generation",
            "expected_sender_project_id",
            "expected_sender_project_generation",
            "body_md",
        }
        for request_schema_name in (
            "MailUiComposeRecipient",
            "MailUiComposeRequest",
            "MailUiReplyRequest",
        ):
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
            "sender_notify_sound",
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
            "reply_target",
        }
        assert "bcc" not in detail_properties
        assert set(
            schema["components"]["schemas"]["MailUiReplyTarget"]["properties"]
        ) == {
            "agent_id",
            "agent_generation",
            "project_id",
            "project_generation",
            "canonical_name",
        }
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
                        "UPDATE ui_users SET preferred_ui_locale = 'zz' "
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
                    "preferred_ui_locale": " MY-mm ",
                    "preferred_correspondence_locale": " ZH-hAnT ",
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
                "preferred_ui_locale": "my-MM",
                "preferred_correspondence_locale": "zh-Hant",
            },
            "effective": {
                "ui_locale": "my-MM",
                "correspondence_locale": "zh-Hant",
            },
        }
        assert inherited.status_code == 200
        assert inherited.json() == {
            "stored": {
                "preferred_ui_locale": "my-MM",
                "preferred_correspondence_locale": None,
            },
            "effective": {
                "ui_locale": "my-MM",
                "correspondence_locale": "my-MM",
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
            ("preferences-owner", "my-MM", None),
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
            base_url="http://mail.example.test",
            cookies=await _cookie("preferences-proxied", epoch),
        ) as client:
            rejected = await client.patch(
                "/mail/api/v1/me/preferences",
                json={"preferred_ui_locale": "pl"},
                headers={
                    "Origin": "https://mail.example.test",
                    "Referer": "https://mail.example.test/mail/",
                    "X-Forwarded-Proto": "https",
                },
            )

        async with AsyncClient(
            transport=ASGITransport(
                app=cast(Any, proxied_app),
                client=("172.19.0.1", 43112),
            ),
            base_url="http://mail.example.test",
            cookies=await _cookie("preferences-proxied", epoch),
        ) as client:
            response = await client.patch(
                "/mail/api/v1/me/preferences",
                json={"preferred_ui_locale": "pl"},
                headers={
                    "Origin": "https://mail.example.test",
                    "Referer": "https://mail.example.test/mail/",
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
                    {"preferred_ui_locale": "not-a-locale"},
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
    async def test_login_same_origin_reaches_auth_and_foreign_origin_is_rejected(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Browser-shaped login POSTs keep the CSRF boundary without an opaque Origin."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        await _make_user("neutral-user")
        monkeypatch.setattr(webauth, "authenticate", lambda *_args: False)
        http_module._login_failures.clear()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://mail.example.test",
            ) as client:
                login = await client.get("/mail/login")
                same_origin = await client.post(
                    "/mail/login",
                    data={
                        "username": "neutral-user",
                        "password": "incorrect-password",
                        "next": "/mail",
                    },
                    headers={
                        "Origin": "http://mail.example.test",
                        "Referer": "http://mail.example.test/mail/login",
                        "Host": "mail.example.test",
                    },
                )
                foreign = await client.post(
                    "/mail/login",
                    data={
                        "username": "neutral-user",
                        "password": "incorrect-password",
                        "next": "/mail",
                    },
                    headers={
                        "Origin": "https://foreign.example.test",
                        "Referer": "https://foreign.example.test/login",
                        "Host": "mail.example.test",
                    },
                )
        finally:
            http_module._login_failures.clear()

        assert login.status_code == 200
        assert login.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert same_origin.status_code == 401
        assert same_origin.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert foreign.status_code == 403
        assert foreign.json() == {"detail": "Cross-origin request rejected"}

    @pytest.mark.asyncio
    async def test_login_opaque_origin_fails_before_auth_or_account_mutation(
        self,
        isolated_env,
        monkeypatch,
    ):
        """An opaque Origin cannot borrow a valid same-origin Referer."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        password = "correct opaque-origin password"
        await _make_user("opaque-origin-user", password=password)
        authenticate_calls: list[tuple[Any, ...]] = []
        real_authenticate = webauth.authenticate

        def observed_authenticate(*args: Any) -> bool:
            authenticate_calls.append(args)
            return real_authenticate(*args)

        monkeypatch.setattr(webauth, "authenticate", observed_authenticate)
        http_module._login_failures.clear()
        http_module._login_failures["sentinel"] = [123.0]
        throttle_before = {
            key: list(values) for key, values in http_module._login_failures.items()
        }
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://mail.example.test",
            ) as client:
                response = await client.post(
                    "/mail/login",
                    data={
                        "username": "opaque-origin-user",
                        "password": password,
                        "next": "/mail",
                    },
                    headers={
                        "Origin": "null",
                        "Referer": "http://mail.example.test/mail/login",
                        "Host": "mail.example.test",
                    },
                )
            async with get_session() as session:
                last_login_ts = (
                    await session.execute(
                        text(
                            "SELECT last_login_ts FROM ui_users "
                            "WHERE username = 'opaque-origin-user'"
                        )
                    )
                ).scalar_one()
        finally:
            throttle_after = {
                key: list(values) for key, values in http_module._login_failures.items()
            }
            http_module._login_failures.clear()

        assert response.status_code == 403
        assert response.json() == {"detail": "Cross-origin request rejected"}
        assert "set-cookie" not in response.headers
        assert authenticate_calls == []
        assert throttle_after == throttle_before
        assert last_login_ts is None

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
        assert healthy.headers["Content-Security-Policy"] == LOGIN_CSP
        assert healthy.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert healthy.headers["X-Frame-Options"] == "DENY"

    @pytest.mark.asyncio
    async def test_login_rejects_opaque_origin_without_falling_back_to_referer(
        self,
        isolated_env,
        monkeypatch,
    ):
        """An explicit opaque Origin stays fail-closed even with a valid Referer."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        await _make_user("opaque-origin-user")
        authentication_attempted = False

        def authenticate(*_args: object) -> bool:
            nonlocal authentication_attempted
            authentication_attempted = True
            return True

        monkeypatch.setattr(webauth, "authenticate", authenticate)
        http_module._login_failures.clear()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/mail/login",
                    data={
                        "username": "opaque-origin-user",
                        "password": "correct",
                        "next": "/mail",
                    },
                    headers={
                        "Origin": "null",
                        "Referer": "http://test/mail/login",
                        "Host": "test",
                    },
                )
            async with get_session() as session:
                last_login_ts = (
                    await session.execute(
                        text(
                            "SELECT last_login_ts FROM ui_users "
                            "WHERE username = 'opaque-origin-user'"
                        )
                    )
                ).scalar_one()
        finally:
            login_failures = dict(http_module._login_failures)
            http_module._login_failures.clear()

        assert response.status_code == 403
        assert response.json() == {"detail": "Cross-origin request rejected"}
        assert "set-cookie" not in response.headers
        assert authentication_attempted is False
        assert last_login_ts is None
        assert login_failures == {}

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
    async def test_file_reservation_read_exposes_execution_diagnostics_without_capability(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Hook reads distinguish live, terminal, and legacy claims safely."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, _message_id = await _seed_project(
            "execution-reservations",
            subject="Execution diagnostics",
            agent_name="ServiceAgent",
            sound="low",
        )
        active_id = "11111111-1111-4111-8111-111111111111"
        ended_id = "22222222-2222-4222-8222-222222222222"
        async with get_session() as session:
            agent_id = int(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM agents WHERE project_id = :project_id "
                            "AND name = 'ServiceAgent'"
                        ),
                        {"project_id": project_id},
                    )
                ).scalar_one()
            )
            await session.execute(
                text(
                    "INSERT INTO agent_executions "
                    "(id, project_id, agent_id, external_id, client_name, "
                    "execution_token_hash, lifecycle_protocol_version, kind, status, "
                    "task_description, started_ts, last_active_ts) VALUES "
                    "(:id, :project_id, :agent_id, :external_id, 'codex', :token_hash, "
                    "1, 'session', 'active', '', datetime('now', '-5 minutes'), "
                    "datetime('now'))"
                ),
                {
                    "id": active_id,
                    "project_id": project_id,
                    "agent_id": agent_id,
                    "external_id": "live-native-session",
                    "token_hash": "a" * 64,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO agent_executions "
                    "(id, project_id, agent_id, external_id, client_name, "
                    "execution_token_hash, lifecycle_protocol_version, kind, status, "
                    "task_description, started_ts, last_active_ts) VALUES "
                    "(:id, :project_id, :agent_id, :external_id, 'codex', :token_hash, "
                    "1, 'session', 'active', '', datetime('now', '-5 minutes'), "
                    "datetime('now'))"
                ),
                {
                    "id": ended_id,
                    "project_id": project_id,
                    "agent_id": agent_id,
                    "external_id": "ended-native-session",
                    "token_hash": "b" * 64,
                },
            )
            for values in (
                {
                    "agent_id": agent_id,
                    "execution_id": active_id,
                    "origin": "auto",
                    "path": "src/live.py",
                },
                {
                    "agent_id": agent_id,
                    "execution_id": ended_id,
                    "origin": "explicit",
                    "path": "src/ended.py",
                },
                {
                    "agent_id": None,
                    "execution_id": None,
                    "origin": "explicit",
                    "path": "src/legacy.py",
                },
            ):
                await session.execute(
                    text(
                        "INSERT INTO file_reservations "
                        "(project_id, agent_id, execution_id, origin, path_pattern, "
                        "exclusive, reason, created_ts, expires_ts) VALUES "
                        "(:project_id, :agent_id, :execution_id, :origin, :path, 1, '', "
                        "datetime('now'), datetime('now', '+1 hour'))"
                    ),
                    {"project_id": project_id, **values},
                )
            await session.execute(
                text(
                    "UPDATE agent_executions SET status = 'completed', "
                    "last_active_ts = CURRENT_TIMESTAMP, ended_ts = CURRENT_TIMESTAMP "
                    "WHERE id = :execution_id"
                ),
                {"execution_id": ended_id},
            )
            await session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/mail/api/file-reservations",
                params={"project": "execution-reservations"},
                headers={"Authorization": f"Bearer {BEARER}"},
            )

        assert response.status_code == 200
        by_path = {
            item["path_pattern"]: item for item in response.json()["reservations"]
        }
        expected_live = {
            "execution_id": active_id,
            "origin": "auto",
            "execution_status": "active",
            "orphaned": False,
            "legacy_unscoped": False,
        }
        assert {
            key: by_path["src/live.py"][key] for key in expected_live
        } == expected_live
        assert by_path["src/ended.py"]["execution_status"] == "completed"
        assert by_path["src/ended.py"]["orphaned"] is True
        assert by_path["src/legacy.py"]["legacy_unscoped"] is True
        assert by_path["src/legacy.py"]["orphaned"] is True
        for item in by_path.values():
            assert "execution_token" not in item
            assert "execution_token_hash" not in item

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
        assert legacy_page.status_code == 307
        assert legacy_page.headers["location"] == "/mail#inbox"
        assert projects_page.status_code == 307
        assert projects_page.headers["location"] == "/mail#projects"
        assert legacy_api.status_code == 404
        assert legacy_api.json() == {"detail": "Not Found"}

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
        assert member_legacy_projects.status_code == 307
        assert member_legacy_projects.headers["location"] == "/mail#projects"
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
        assert admin_legacy_projects.status_code == 307
        assert admin_legacy_projects.headers["location"] == "/mail#projects"
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
    async def test_operator_composes_into_assigned_projects_and_nowhere_else(
        self,
        isolated_env,
        monkeypatch,
    ):
        """Compose follows the assignment table, not the global role."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        assigned_id, _assigned_message = await _seed_project(
            "compose-scope-assigned",
            subject="Assigned target",
            agent_name="AssignedAgent",
            sound="low",
        )
        unassigned_id, _unassigned_message = await _seed_project(
            "compose-scope-unassigned",
            subject="Unassigned target",
            agent_name="UnassignedAgent",
            sound="low",
        )
        operator_epoch = await _make_user(
            "scoped-operator", role=webauth.ROLE_MEMBER
        )
        await _assign(
            "scoped-operator", assigned_id, webauth.PROJECT_ROLE_OPERATOR
        )
        viewer_epoch = await _make_user("scoped-viewer", role=webauth.ROLE_MEMBER)
        await _assign("scoped-viewer", assigned_id, webauth.PROJECT_ROLE_VIEWER)

        assigned_generation, assigned_refs = await _compose_lifetime_refs(
            assigned_id,
            "AssignedAgent",
        )
        unassigned_generation, unassigned_refs = await _compose_lifetime_refs(
            unassigned_id,
            "UnassignedAgent",
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("scoped-operator", operator_epoch),
        ) as client:
            allowed = await client.post(
                f"/mail/api/v1/projects/{assigned_id}/messages",
                json={
                    "idempotency_key": "scoped-operator-allowed",
                    "expected_project_generation": assigned_generation,
                    "recipients": assigned_refs,
                    "subject": "Operator starts a thread",
                    "body_md": "Authored by a project operator.",
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            out_of_scope = await client.post(
                f"/mail/api/v1/projects/{unassigned_id}/messages",
                json={
                    "idempotency_key": "scoped-operator-out-of-scope",
                    "expected_project_generation": unassigned_generation,
                    "recipients": unassigned_refs,
                    "subject": "Should never be delivered",
                    "body_md": "Operator has no assignment on this project.",
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("scoped-viewer", viewer_epoch),
        ) as client:
            viewer_refused = await client.post(
                f"/mail/api/v1/projects/{assigned_id}/messages",
                json={
                    "idempotency_key": "scoped-viewer-refused",
                    "expected_project_generation": assigned_generation,
                    "recipients": assigned_refs,
                    "subject": "Should never be delivered",
                    "body_md": "A viewer must not author anything.",
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["status"] in {"published", "pending"}
        # An unassigned project must stay indistinguishable from a missing one.
        # Answering 403 here would confirm the project exists to someone with
        # no access to it, which is how project ids leak.
        assert out_of_scope.status_code == 404, out_of_scope.text
        assert out_of_scope.json()["detail"] == {"code": "project_not_found"}
        # The viewer CAN see this project, so 403 is correct and leaks nothing.
        assert viewer_refused.status_code == 403, viewer_refused.text
        assert viewer_refused.json()["detail"] == {"code": "actor_forbidden"}

        # Negative control on the assertion itself: exactly one message was
        # authored, so `allowed` really is the only send that landed.
        async with get_session() as session:
            authored = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM messages "
                        "WHERE subject = 'Should never be delivered'"
                    )
                )
            ).scalar_one()
        assert authored == 0

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
            "/mail/api/v1/search": "aggregate-scoped",
            "/mail/api/v1/admin/access": "admin-only",
            "/mail/api/v1/me/profile": "self-only",
            "/mail/api/v1/me/preferences": "self-only",
            "/mail/api/v1/projects": "aggregate-scoped",
            "/mail/api/v1/projects/{project_id}/agents": "admin-only",
            # Operator or admin, never a plain viewer: a claim carries a
            # path pattern and a reason, and the live set of them
            # describes what is being worked on where.
            "/mail/api/v1/reservations": "aggregate-scoped",
            "/mail/api/v1/deliveries/{delivery_id}": "self-only",
            "/mail/api/v1/projects/{project_id}/messages/{message_id}": (
                "project-guarded"
            ),
            "/mail/api/v1/projects/{project_id}/threads": (
                "project-guarded"
            ),
            "/mail/projects": "legacy-bookmark-redirect",
            "/mail/unified-inbox": "legacy-bookmark-redirect",
            "/mail/api/locks": "retired-404",
            "/mail/api/file-reservations": "service-or-project-scoped",
            "/mail/{project}": "legacy-bookmark-redirect",
            "/mail/{project}/inbox/{agent}": "retired-404",
            "/mail/{project}/message/{mid}": "legacy-bookmark-redirect",
            "/mail/{project}/thread/{thread_id}": "legacy-bookmark-redirect",
            "/mail/{project}/search": "legacy-bookmark-redirect",
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
            "legacy-bookmark-redirect",
            "retired-404",
        }

    @pytest.mark.asyncio
    async def test_options_on_a_retired_path_never_reaches_the_router(
        self, isolated_env, monkeypatch
    ):
        """OPTIONS is filtered by the allowlist, not by the method branch below it.

        The middleware does call `call_next` for OPTIONS, which reads like a
        bypass. It is not: the active-path check runs first, so a retired path
        is already answered, and a legacy bookmark is rejected by the
        GET/HEAD-only guard. This is the least obvious of the reachability
        guarantees and the one that would silently regress if those three
        checks were ever reordered.
        """
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            retired = await client.options("/mail/archive/activity")
            bookmark = await client.options("/mail/projects")
            live = await client.options("/mail/login")

        assert retired.status_code == 404
        assert bookmark.status_code == 404
        # The live path is the control: a 404 here would mean the assertions
        # above passed because everything 404s, which would prove nothing.
        assert live.status_code != 404

    @pytest.mark.asyncio
    async def test_retired_paths_stay_404_with_the_session_gate_disabled(
        self, isolated_env, monkeypatch, tmp_path
    ):
        """Turning the session gate off in development does not restore the old UI.

        That branch exists so a developer can run without cookies, and it
        reaches `call_next` — but only after the same allowlist check. A
        regression here would expose the retired surface on exactly the
        configuration nobody runs in production and therefore nobody watches.
        """
        _install_react_dist(monkeypatch, tmp_path)
        test_settings, _app = _build(
            monkeypatch,
            MAIL_UI_AUTH_ENABLED="false",
            MAIL_UI_SESSION_SECRET="",
        )
        settings = replace(test_settings, environment="development")
        app = build_http_app(settings, build_mcp_server())

        # Disabling the session gate hands the UI to the bearer layer — the
        # middleware says so itself — so every request here carries the bearer.
        # Without it the retired paths would 401 and the case would prove
        # nothing about the allowlist.
        headers = {"Authorization": f"Bearer {BEARER}"}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            retired = await client.get("/mail/archive/activity", headers=headers)
            retired_project = await client.get(
                "/mail/some-project/attachments", headers=headers
            )
            shell = await client.get("/mail", headers=headers)

        assert retired.status_code == 404
        assert retired_project.status_code == 404
        # Control: authenticated to the bearer layer, the shell must be served,
        # otherwise the two 404s above are indistinguishable from a broken app.
        assert shell.status_code == 200



class TestMailUiV1ReadApi:
    """The React read API is typed, project-scoped, and privacy-minimal."""

    @pytest.mark.asyncio
    async def test_search_is_fts_only_visible_plain_and_query_bound(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        visible_id, first_message_id = await _seed_project(
            "search-visible",
            subject="Release rainbow marker",
            agent_name="SearchAgent",
            sound="soft",
        )
        hidden_id, hidden_message_id = await _seed_project(
            "search-hidden",
            subject="Release rainbow hidden",
            agent_name="HiddenSearchAgent",
            sound="low",
        )
        epoch = await _make_user("search-viewer", role=webauth.ROLE_MEMBER)
        await _assign("search-viewer", visible_id, webauth.PROJECT_ROLE_VIEWER)
        async with get_session() as session:
            sender_id = int(
                (
                    await session.execute(
                        text(
                            "SELECT sender_id FROM messages WHERE id = :message_id"
                        ),
                        {"message_id": first_message_id},
                    )
                ).scalar_one()
            )
            await session.execute(
                text(
                    "UPDATE messages SET body_md = :body, "
                    "created_ts = '2040-01-01 00:00:00.000000' "
                    "WHERE id = :message_id"
                ),
                {
                    "message_id": first_message_id,
                    "body": "Visible rainbow <script>alert('no')</script> body.",
                },
            )
            for index in range(1, 4):
                await session.execute(
                    text(
                        "INSERT INTO messages "
                        "(project_id, sender_id, thread_id, subject, body_md, "
                        "importance, ack_required, created_ts, attachments) "
                        "VALUES (:project_id, :sender_id, 'search-thread', :subject, "
                        ":body, 'normal', 0, :created_ts, '[]')"
                    ),
                    {
                        "project_id": visible_id,
                        "sender_id": sender_id,
                        "subject": f"Release rainbow {index}",
                        "body": f"Visible rainbow body number {index}.",
                        "created_ts": f"2040-01-0{index + 1} 00:00:00.000000",
                    },
                )
            await session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("search-viewer", epoch),
        ) as client:
            first = await client.get(
                "/mail/api/v1/search",
                params={
                    "q": 'subject:release "rainbow"',
                    "scope": "all",
                    "order": "newest",
                    "limit": 2,
                },
            )
            second = await client.get(
                "/mail/api/v1/search",
                params={
                    "q": 'subject:release "rainbow"',
                    "scope": "all",
                    "order": "newest",
                    "limit": 2,
                    "cursor": first.json()["next_cursor"],
                },
            )
            relevance = await client.get(
                "/mail/api/v1/search",
                params={"q": "rainbow", "order": "relevance", "limit": 2},
            )
            relevance_baseline = await client.get(
                "/mail/api/v1/search",
                params={"q": "rainbow", "order": "relevance", "limit": 100},
            )
            async with get_session() as session:
                hidden_sender_id = int(
                    (
                        await session.execute(
                            text(
                                "SELECT sender_id FROM messages WHERE id = :message_id"
                            ),
                            {"message_id": hidden_message_id},
                        )
                    ).scalar_one()
                )
                for index in range(20):
                    await session.execute(
                        text(
                            "INSERT INTO messages "
                            "(project_id, sender_id, thread_id, subject, body_md, "
                            "importance, ack_required, created_ts, attachments) "
                            "VALUES (:project_id, :sender_id, 'hidden-corpus', :subject, "
                            ":body, 'normal', 0, :created_ts, '[]')"
                        ),
                        {
                            "project_id": hidden_id,
                            "sender_id": hidden_sender_id,
                            "subject": f"rainbow hidden corpus {index}",
                            "body": ("rainbow " * (index + 1)).strip(),
                            "created_ts": f"2041-01-{index + 1:02d} 00:00:00.000000",
                        },
                    )
                await session.commit()
            relevance_after_hidden = await client.get(
                "/mail/api/v1/search",
                params={"q": "rainbow", "order": "relevance", "limit": 100},
            )
            relevance_second = await client.get(
                "/mail/api/v1/search",
                params={
                    "q": "rainbow",
                    "order": "relevance",
                    "limit": 2,
                    "cursor": relevance.json()["next_cursor"],
                },
            )
            wrong_query_cursor = await client.get(
                "/mail/api/v1/search",
                params={
                    "q": "different",
                    "order": "newest",
                    "cursor": first.json()["next_cursor"],
                },
            )
            hidden_filter = await client.get(
                "/mail/api/v1/search",
                params={"q": "rainbow", "project_id": hidden_id},
            )

        assert first.status_code == 200
        assert second.status_code == 200
        assert relevance.status_code == 200
        assert relevance_baseline.status_code == 200
        assert relevance_after_hidden.status_code == 200
        assert relevance_second.status_code == 200
        assert first.headers["Cache-Control"] == "no-store"
        pages = [first.json(), second.json()]
        assert [len(page["items"]) for page in pages] == [2, 2]
        assert pages[0]["next_cursor"] is not None
        assert pages[1]["next_cursor"] is None
        items = [item for page in pages for item in page["items"]]
        assert len({item["id"] for item in items}) == 4
        assert {item["project_id"] for item in items} == {visible_id}
        assert all(
            set(item)
            == {
                "id",
                "project_id",
                "project_slug",
                "subject",
                "sender",
                "sender_name",
                "sender_display_name",
                "sender_notify_sound",
                "importance",
                "ack_required",
                "thread_id",
                "reply_to",
                "created_ts",
                "can_reply",
                "snippet",
            }
            for item in items
        )
        assert all("<mark>" not in item["snippet"] for item in items)
        assert all(len(item["snippet"]) <= 320 for item in items)
        assert "search-hidden" not in first.text + second.text + relevance.text
        assert "HiddenSearchAgent" not in first.text + second.text + relevance.text
        assert all(
            not set(item) & {"body_md", "to", "cc", "bcc", "recipients"}
            for item in items
        )
        relevance_items = [
            *relevance.json()["items"],
            *relevance_second.json()["items"],
        ]
        assert [item["id"] for item in relevance_after_hidden.json()["items"]] == [
            item["id"] for item in relevance_baseline.json()["items"]
        ]
        assert len({item["id"] for item in relevance_items}) == 4
        assert relevance_second.json()["next_cursor"] is None
        assert wrong_query_cursor.status_code == 422
        assert wrong_query_cursor.json() == {
            "detail": {"code": "invalid_search_query"}
        }
        assert hidden_filter.status_code == 404
        assert hidden_filter.json() == {"detail": {"code": "project_not_found"}}

    @pytest.mark.asyncio
    async def test_search_validation_and_fts_failure_are_stable_and_redacted(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, _message_id = await _seed_project(
            "search-failure",
            subject="Failure marker",
            agent_name="FailureSearchAgent",
            sound="soft",
        )
        epoch = await _make_user("search-failure-admin")
        too_many_tokens = " ".join(f"term{index}" for index in range(33))

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("search-failure-admin", epoch),
        ) as client:
            invalid = [
                await client.get("/mail/api/v1/search", params={"q": "   "}),
                await client.get(
                    "/mail/api/v1/search", params={"q": "private\x00control"}
                ),
                await client.get(
                    "/mail/api/v1/search", params={"q": too_many_tokens}
                ),
                await client.get(
                    "/mail/api/v1/search", params={"q": "rainbow", "limit": 101}
                ),
                await client.get(
                    "/mail/api/v1/search", params={"q": "rainbow", "scope": "raw"}
                ),
            ]
            async with get_session() as session:
                await session.execute(text("DROP TABLE fts_messages"))
                await session.commit()
            unavailable = await client.get(
                "/mail/api/v1/search",
                params={"q": "Failure marker", "project_id": project_id},
            )

        for response in invalid:
            assert response.status_code == 422
            assert "term32" not in response.text
            assert response.headers["Cache-Control"] == "no-store"
        assert unavailable.status_code == 503
        assert unavailable.json() == {"detail": {"code": "search_unavailable"}}
        assert "Failure marker" not in unavailable.text
        assert unavailable.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_search_requires_a_human_session_without_reflecting_query(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/mail/api/v1/search",
                params={"q": "private-query-marker"},
            )
        assert response.status_code == 401
        assert response.json() == {"detail": {"code": "actor_forbidden"}}
        assert "private-query-marker" not in response.text
        assert response.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_admin_agent_directory_is_addressable_minimal_and_deterministic(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, _message_id = await _seed_project(
            "api-v1-agent-directory",
            subject="Directory fixture",
            agent_name="zulu",
            sound="soft",
        )
        admin_epoch = await _make_user("directory-private-human")

        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE agents SET display_name = 'Zulu Person', "
                    "program = 'private-program', model = 'private-model', "
                    "task_description = 'private-task', "
                    "registration_token = 'private-registration-token' "
                    "WHERE project_id = :project_id AND name = 'zulu'"
                ),
                {"project_id": project_id},
            )
            for name, display_name, contact_policy, retired_at in (
                ("bravo", "Bravo Person", "open", None),
                ("Alpha", None, "auto", None),
                ("RetiredAgent", "Retired Person", "open", "2030-01-01 00:00:00"),
                ("BlockedAgent", "Blocked Person", "block_all", None),
                ("HumanOverseer", "Human mailbox", "open", None),
            ):
                await session.execute(
                    text(
                        "INSERT INTO agents "
                        "(project_id, name, program, model, task_description, "
                        "inception_ts, last_active_ts, attachments_policy, "
                        "contact_policy, registration_token, retired_at, display_name) "
                        "VALUES (:project_id, :name, 'secret-program', 'secret-model', "
                        "'secret-task', datetime('now'), datetime('now'), 'auto', "
                        ":contact_policy, 'secret-token', "
                        ":retired_at, :display_name)"
                    ),
                    {
                        "project_id": project_id,
                        "name": name,
                        "contact_policy": contact_policy,
                        "retired_at": retired_at,
                        "display_name": display_name,
                    },
                )
            await session.commit()

        project_generation, recipient_refs = await _compose_lifetime_refs(
            project_id,
            "Alpha",
            "bravo",
            "zulu",
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("directory-private-human", admin_epoch),
        ) as client:
            response = await client.get(
                f"/mail/api/v1/projects/{project_id}/agents"
            )
            legacy = await client.get(
                "/mail/api/projects/api-v1-agent-directory/agents"
            )

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {
            "project_id": project_id,
            "project_generation": project_generation,
            "items": [
                {
                    "agent_id": recipient_refs[0]["agent_id"],
                    "agent_generation": recipient_refs[0][
                        "expected_agent_generation"
                    ],
                    "name": "Alpha",
                    "display_name": None,
                    "notify_sound": None,
                },
                {
                    "agent_id": recipient_refs[1]["agent_id"],
                    "agent_generation": recipient_refs[1][
                        "expected_agent_generation"
                    ],
                    "name": "bravo",
                    "display_name": "Bravo Person",
                    "notify_sound": None,
                },
                {
                    "agent_id": recipient_refs[2]["agent_id"],
                    "agent_generation": recipient_refs[2][
                        "expected_agent_generation"
                    ],
                    "name": "zulu",
                    "display_name": "Zulu Person",
                    # Seeded with sound="soft": the reader needs the word to
                    # synthesise a per-sender tone locally. "Minimal" here means
                    # no program, model, task or token — a presentational
                    # preference the server-rendered UI already handed to this
                    # same authenticated reader does not breach that.
                    "notify_sound": "soft",
                },
            ],
            "total": 3,
        }
        assert all(
            set(item)
            == {
                "agent_id",
                "agent_generation",
                "name",
                "display_name",
                "notify_sound",
            }
            for item in response.json()["items"]
        )
        for private_value in (
            "directory-private-human",
            "private-program",
            "private-model",
            "private-task",
            "private-registration-token",
            "secret-program",
            "secret-model",
            "secret-task",
            "secret-token",
            "RetiredAgent",
            "BlockedAgent",
            "HumanOverseer",
        ):
            assert private_value not in response.text
        assert legacy.status_code == 404
        assert legacy.json() == {"detail": "Not Found"}

    @pytest.mark.asyncio
    async def test_agent_directory_rbac_and_project_lifetime_fail_closed(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        active_id, _active_message = await _seed_project(
            "directory-visible",
            subject="Visible directory",
            agent_name="VisibleDirectoryAgent",
            sound="low",
        )
        hidden_id, _hidden_message = await _seed_project(
            "directory-hidden",
            subject="Hidden directory",
            agent_name="HiddenDirectoryAgent",
            sound="high",
        )
        archived_id, _archived_message = await _seed_project(
            "directory-archived",
            subject="Archived directory",
            agent_name="ArchivedDirectoryAgent",
            sound="click",
        )
        member_epoch = await _make_user(
            "directory-member",
            role=webauth.ROLE_MEMBER,
        )
        admin_epoch = await _make_user("directory-admin")
        await _assign("directory-member", active_id, webauth.PROJECT_ROLE_OPERATOR)
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE projects SET archived_at = datetime('now') "
                    "WHERE id = :project_id"
                ),
                {"project_id": archived_id},
            )
            await session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as anonymous:
            unauthenticated = await anonymous.get(
                f"/mail/api/v1/projects/{active_id}/agents"
            )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("directory-member", member_epoch),
        ) as member:
            assigned_but_not_admin = await member.get(
                f"/mail/api/v1/projects/{active_id}/agents"
            )
            invisible = await member.get(
                f"/mail/api/v1/projects/{hidden_id}/agents"
            )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("directory-admin", admin_epoch),
        ) as admin:
            archived = await admin.get(
                f"/mail/api/v1/projects/{archived_id}/agents"
            )
            missing = await admin.get(
                "/mail/api/v1/projects/999999999/agents"
            )

        assert unauthenticated.status_code == 401
        assert unauthenticated.json() == {"detail": {"code": "actor_forbidden"}}
        for response in (assigned_but_not_admin, invisible):
            assert response.status_code == 403
            assert response.json() == {"detail": {"code": "actor_forbidden"}}
        for response in (archived, missing):
            assert response.status_code == 404
            assert response.json() == {"detail": {"code": "project_not_found"}}
        for response in (
            unauthenticated,
            assigned_but_not_admin,
            invisible,
            archived,
            missing,
        ):
            assert response.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_compose_revalidates_directory_addressability_after_selection(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, _message_id = await _seed_project(
            "directory-address-race",
            subject="Address race fixture",
            agent_name="RaceTarget",
            sound="high",
        )
        admin_epoch = await _make_user("directory-race-admin")
        path = f"/mail/api/v1/projects/{project_id}"
        project_generation, recipient_refs = await _compose_lifetime_refs(
            project_id,
            "RaceTarget",
        )
        payload = {
            "expected_project_generation": project_generation,
            "recipients": recipient_refs,
            "subject": "Must revalidate",
            "body_md": "Do not publish after the recipient becomes unavailable.",
        }

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("directory-race-admin", admin_epoch),
        ) as client:
            before_retire = await client.get(f"{path}/agents")
            async with get_session() as session:
                await session.execute(
                    text(
                        "UPDATE agents SET retired_at = datetime('now') "
                        "WHERE project_id = :project_id AND name = 'RaceTarget'"
                    ),
                    {"project_id": project_id},
                )
                await session.commit()
            retired_send = await client.post(
                f"{path}/messages",
                json={**payload, "idempotency_key": "directory-retired-race"},
                headers=SAME_ORIGIN_HEADERS,
            )

            async with get_session() as session:
                await session.execute(
                    text(
                        "UPDATE agents SET retired_at = NULL, contact_policy = 'open' "
                        "WHERE project_id = :project_id AND name = 'RaceTarget'"
                    ),
                    {"project_id": project_id},
                )
                await session.commit()
            before_block = await client.get(f"{path}/agents")
            async with get_session() as session:
                await session.execute(
                    text(
                        "UPDATE agents SET contact_policy = 'block_all' "
                        "WHERE project_id = :project_id AND name = 'RaceTarget'"
                    ),
                    {"project_id": project_id},
                )
                await session.commit()
            blocked_send = await client.post(
                f"{path}/messages",
                json={**payload, "idempotency_key": "directory-blocked-race"},
                headers=SAME_ORIGIN_HEADERS,
            )
            after_block = await client.get(f"{path}/agents")

        assert [item["name"] for item in before_retire.json()["items"]] == [
            "RaceTarget"
        ]
        assert [item["name"] for item in before_block.json()["items"]] == [
            "RaceTarget"
        ]
        assert after_block.json() == {
            "project_id": project_id,
            "project_generation": project_generation,
            "items": [],
            "total": 0,
        }
        for response in (retired_send, blocked_send):
            assert response.status_code == 409
            assert response.json() == {"detail": {"code": "recipient_unavailable"}}
            assert response.headers["Cache-Control"] == "no-store"

        async with get_session() as session:
            delivery_count = int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM message_deliveries "
                            "WHERE idempotency_key IN "
                            "('directory-retired-race', 'directory-blocked-race')"
                        )
                    )
                ).scalar_one()
            )
        assert delivery_count == 0

    @pytest.mark.asyncio
    async def test_compose_rejects_stale_project_and_recreated_agent_lifetimes(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, _message_id = await _seed_project(
            "directory-lifetime-race",
            subject="Lifetime race fixture",
            agent_name="UnrelatedSender",
            sound="soft",
        )
        admin_epoch = await _make_user("directory-lifetime-admin")
        async with get_session() as session:
            target_id = int(
                (
                    await session.execute(
                        text(
                            "INSERT INTO agents "
                            "(project_id, name, program, model, task_description, "
                            "inception_ts, last_active_ts, attachments_policy, "
                            "contact_policy) VALUES (:project_id, 'RecreatedTarget', "
                            "'test', 'test', 'lifetime target', datetime('now'), "
                            "datetime('now'), 'auto', 'open') RETURNING id"
                        ),
                        {"project_id": project_id},
                    )
                ).scalar_one()
            )
            project_race_id = int(
                (
                    await session.execute(
                        text(
                            "INSERT INTO projects (slug, human_key, created_at) "
                            "VALUES ('recreated-project', '/recreated-project', "
                            "datetime('now')) RETURNING id"
                        )
                    )
                ).scalar_one()
            )
            project_race_agent_id = int(
                (
                    await session.execute(
                        text(
                            "INSERT INTO agents "
                            "(project_id, name, program, model, task_description, "
                            "inception_ts, last_active_ts, attachments_policy, "
                            "contact_policy) VALUES (:project_id, 'ProjectRaceTarget', "
                            "'test', 'test', 'project lifetime target', datetime('now'), "
                            "datetime('now'), 'auto', 'open') RETURNING id"
                        ),
                        {"project_id": project_race_id},
                    )
                ).scalar_one()
            )
            await session.commit()

        agent_project_generation, agent_recipient_refs = await _compose_lifetime_refs(
            project_id,
            "RecreatedTarget",
        )
        stale_project_generation, project_recipient_refs = await _compose_lifetime_refs(
            project_race_id,
            "ProjectRaceTarget",
        )
        assert agent_recipient_refs[0]["agent_id"] == target_id
        assert project_recipient_refs[0]["agent_id"] == project_race_agent_id
        agent_payload = {
            "expected_project_generation": agent_project_generation,
            "recipients": agent_recipient_refs,
            "subject": "Lifetime-safe request",
            "body_md": "This must not cross an identity lifetime.",
        }
        project_payload = {
            "expected_project_generation": stale_project_generation,
            "recipients": project_recipient_refs,
            "subject": "Project lifetime-safe request",
            "body_md": "This must not cross a project lifetime.",
        }
        agent_path = f"/mail/api/v1/projects/{project_id}"
        project_path = f"/mail/api/v1/projects/{project_race_id}"

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("directory-lifetime-admin", admin_epoch),
        ) as client:
            stale_project_directory = await client.get(f"{project_path}/agents")
            stale_agent_directory = await client.get(f"{agent_path}/agents")
            async with get_session() as session:
                await session.execute(
                    text("DELETE FROM agents WHERE id = :agent_id"),
                    {"agent_id": project_race_agent_id},
                )
                await session.execute(
                    text("DELETE FROM projects WHERE id = :project_id"),
                    {"project_id": project_race_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO projects (id, slug, human_key, created_at) "
                        "VALUES (:project_id, 'recreated-project', "
                        "'/recreated-project', datetime('now'))"
                    ),
                    {"project_id": project_race_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO agents "
                        "(id, project_id, name, program, model, task_description, "
                        "inception_ts, last_active_ts, attachments_policy, "
                        "contact_policy) VALUES (:agent_id, :project_id, "
                        "'ProjectRaceTarget', 'test', 'test', 'new project lifetime', "
                        "datetime('now'), datetime('now'), 'auto', 'open')"
                    ),
                    {
                        "agent_id": project_race_agent_id,
                        "project_id": project_race_id,
                    },
                )
                await session.commit()
            current_project_directory = await client.get(f"{project_path}/agents")
            project_recreated = await client.post(
                f"{project_path}/messages",
                json={
                    **project_payload,
                    "idempotency_key": "stale-project-lifetime",
                },
                headers=SAME_ORIGIN_HEADERS,
            )

            async with get_session() as session:
                await session.execute(
                    text("DELETE FROM agents WHERE id = :agent_id"),
                    {"agent_id": target_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO agents "
                        "(id, project_id, name, program, model, task_description, "
                        "inception_ts, last_active_ts, attachments_policy, "
                        "contact_policy) VALUES (:agent_id, :project_id, "
                        "'RecreatedTarget', 'test', 'test', 'new lifetime', "
                        "datetime('now'), datetime('now'), 'auto', 'open')"
                    ),
                    {"agent_id": target_id, "project_id": project_id},
                )
                await session.commit()
            current_agent_directory = await client.get(f"{agent_path}/agents")
            recreated_agent = await client.post(
                f"{agent_path}/messages",
                json={
                    **agent_payload,
                    "idempotency_key": "stale-agent-lifetime",
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        stale_project_payload = stale_project_directory.json()
        current_project_payload = current_project_directory.json()
        stale_project_target = next(
            item
            for item in stale_project_payload["items"]
            if item["name"] == "ProjectRaceTarget"
        )
        current_project_target = next(
            item
            for item in current_project_payload["items"]
            if item["name"] == "ProjectRaceTarget"
        )
        stale_agent_payload = stale_agent_directory.json()
        current_agent_payload = current_agent_directory.json()
        stale_agent_target = next(
            item
            for item in stale_agent_payload["items"]
            if item["name"] == "RecreatedTarget"
        )
        current_agent_target = next(
            item
            for item in current_agent_payload["items"]
            if item["name"] == "RecreatedTarget"
        )
        assert stale_project_payload["project_generation"] == stale_project_generation
        assert (
            current_project_payload["project_generation"]
            != stale_project_payload["project_generation"]
        )
        assert (
            current_project_target["agent_id"]
            == stale_project_target["agent_id"]
            == project_race_agent_id
        )
        assert (
            current_project_target["name"]
            == stale_project_target["name"]
            == "ProjectRaceTarget"
        )
        assert stale_agent_payload["project_generation"] == agent_project_generation
        assert current_agent_payload["project_generation"] == agent_project_generation
        assert (
            current_agent_target["agent_id"]
            == stale_agent_target["agent_id"]
            == target_id
        )
        assert (
            current_agent_target["name"]
            == stale_agent_target["name"]
            == "RecreatedTarget"
        )
        assert (
            current_agent_target["agent_generation"]
            != stale_agent_target["agent_generation"]
        )
        assert project_recreated.status_code == 409
        assert project_recreated.json() == {"detail": {"code": "project_recreated"}}
        assert recreated_agent.status_code == 409
        assert recreated_agent.json() == {"detail": {"code": "recipient_unavailable"}}
        for response in (project_recreated, recreated_agent):
            assert response.headers["Cache-Control"] == "no-store"

        async with get_session() as session:
            delivery_count = int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM message_deliveries "
                            "WHERE idempotency_key IN "
                            "('stale-project-lifetime', 'stale-agent-lifetime')"
                        )
                    )
                ).scalar_one()
            )
        assert delivery_count == 0

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
                f"/mail/api/v1/projects/{visible_id}/threads",
                params={"thread_id": "api-v1-thread"},
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
                f"/mail/api/v1/projects/{visible_id}/threads",
                params={"thread_id": "not-a-thread"},
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
            "sender_notify_sound",
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
        assert detail_payload["reply_target"] is None
        assert "BlindAgent" not in visible_detail.text
        for forbidden in ("bcc", "private/archive.txt", "tracker.invalid", "data_uri"):
            assert forbidden not in visible_detail.text
        assert cross_detail.status_code == 200
        assert "/api-v1-hidden" not in cross_detail.text
        assert thread.status_code == 200
        assert thread.json()["subject"] == "Visible body must stay out of the list"
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
    async def test_thread_api_round_trips_query_ids_and_limits_numeric_starters(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, starter_message_id = await _seed_project(
            "api-v1-thread-identities",
            subject="Numeric starter",
            agent_name="ThreadIdentityAgent",
            sound="soft",
        )
        epoch = await _make_user("api-v1-thread-identity-admin")
        cookie = await _cookie("api-v1-thread-identity-admin", epoch)
        plus_thread_id = f"+{starter_message_id}"
        leading_zero_thread_id = f"0{starter_message_id}"
        unicode_thread_id = str(starter_message_id).translate(
            str.maketrans(
                "0123456789",
                "".join(chr(0x0660 + index) for index in range(10)),
            )
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=cookie,
        ) as client:
            starter = await client.get(
                f"/mail/api/v1/projects/{project_id}/threads",
                params={"thread_id": str(starter_message_id)},
            )
            noncanonical_numeric = [
                await client.get(
                    f"/mail/api/v1/projects/{project_id}/threads",
                    params={"thread_id": candidate},
                )
                for candidate in (
                    plus_thread_id,
                    leading_zero_thread_id,
                    unicode_thread_id,
                )
            ]

        assert starter.status_code == 200
        assert starter.json()["subject"] == "Numeric starter"
        assert starter.json()["total"] == 1
        assert starter.json()["items"][0]["id"] == starter_message_id
        assert starter.json()["items"][0]["thread_id"] is None
        for response in noncanonical_numeric:
            assert response.status_code == 404
            assert response.json() == {"detail": "Thread not found"}

        path_thread_id = "path/\N{GREEK SMALL LETTER BETA} value?#"
        async with get_session() as session:
            sender_id = int(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM agents "
                            "WHERE project_id = :project_id "
                            "AND name = 'ThreadIdentityAgent'"
                        ),
                        {"project_id": project_id},
                    )
                ).scalar_one()
            )
            for index, thread_id in enumerate(
                (plus_thread_id, unicode_thread_id, path_thread_id),
                start=1,
            ):
                await session.execute(
                    text(
                        "INSERT INTO messages "
                        "(project_id, sender_id, thread_id, subject, body_md, "
                        "importance, ack_required, created_ts, attachments) "
                        "VALUES (:project_id, :sender_id, :thread_id, :subject, "
                        ":body, 'normal', 0, :created_ts, '[]')"
                    ),
                    {
                        "project_id": project_id,
                        "sender_id": sender_id,
                        "thread_id": thread_id,
                        "subject": "" if index == 3 else f"Exact thread {index}",
                        "body": f"Exact body {index}",
                        "created_ts": f"2042-01-01 00:00:0{index}.000000",
                    },
                )
            await session.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=cookie,
        ) as client:
            thread_endpoint = f"/mail/api/v1/projects/{project_id}/threads"
            exact_threads = {
                thread_id: await client.get(
                    thread_endpoint,
                    params={"thread_id": thread_id},
                )
                for thread_id in (plus_thread_id, unicode_thread_id, path_thread_id)
            }
            missing_required_thread_id = await client.get(thread_endpoint)
            retired_path_shape = await client.get(
                f"{thread_endpoint}/not-a-thread"
            )
            invalid_ids = [
                await client.get(
                    thread_endpoint,
                    params={"thread_id": "x" * 129},
                ),
                await client.get(
                    thread_endpoint,
                    params={"thread_id": "\x00"},
                ),
            ]

        expected_subjects = {
            plus_thread_id: "Exact thread 1",
            unicode_thread_id: "Exact thread 2",
            path_thread_id: "",
        }
        for thread_id, response in exact_threads.items():
            assert response.status_code == 200
            assert response.json()["subject"] == expected_subjects[thread_id]
            assert response.json()["total"] == 1
            assert response.json()["items"][0]["thread_id"] == thread_id
        assert "thread_id=path%2F" in str(
            exact_threads[path_thread_id].request.url
        )
        assert missing_required_thread_id.status_code == 422
        assert missing_required_thread_id.json()["detail"][0]["loc"] == [
            "query",
            "thread_id",
        ]
        assert retired_path_shape.status_code == 404
        assert retired_path_shape.json() == {"detail": "Not Found"}
        for response in invalid_ids:
            assert response.status_code == 422
            assert response.json() == {"detail": "Invalid thread id."}
        assert http_module._mail_ui_valid_thread_id(".") is False
        assert http_module._mail_ui_valid_thread_id("..") is False

    @pytest.mark.asyncio
    async def test_thread_reads_one_snapshot_while_a_writer_replaces_messages(
        self,
        isolated_env,
        monkeypatch,
    ):
        """COUNT, subject, page, and recipients must share one WAL snapshot."""
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, oldest_message_id = await _seed_project(
            "api-v1-thread-snapshot",
            subject="Snapshot oldest",
            agent_name="SnapshotSender",
            sound="soft",
        )
        async with get_session() as session:
            sender_id = int(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM agents "
                            "WHERE project_id = :project_id AND name = 'SnapshotSender'"
                        ),
                        {"project_id": project_id},
                    )
                ).scalar_one()
            )
            await session.execute(
                text(
                    "UPDATE messages SET thread_id = 'snapshot-thread', "
                    "created_ts = '2040-01-01 00:00:01.000000' "
                    "WHERE id = :message_id"
                ),
                {"message_id": oldest_message_id},
            )
            newest_result = await session.execute(
                text(
                    "INSERT INTO messages "
                    "(project_id, sender_id, thread_id, subject, body_md, importance, "
                    "ack_required, created_ts, attachments) "
                    "VALUES (:project_id, :sender_id, 'snapshot-thread', "
                    "'Snapshot newest', 'Snapshot newest body', 'normal', 0, "
                    "'2040-01-01 00:00:02.000000', '[]') RETURNING id"
                ),
                {"project_id": project_id, "sender_id": sender_id},
            )
            newest_message_id = int(newest_result.scalar_one())
            await session.commit()

        epoch = await _make_user("api-v1-thread-snapshot-admin")
        cookie = await _cookie("api-v1-thread-snapshot-admin", epoch)
        original_get_session = http_module.get_session
        writer_committed = False
        replacement_message_id: int | None = None

        @contextlib.asynccontextmanager
        async def race_after_thread_count(*args: Any, **kwargs: Any):
            async with original_get_session(*args, **kwargs) as session:
                original_execute = session.execute

                async def execute_with_concurrent_writer(
                    statement: Any,
                    parameters: Any = None,
                    **execute_kwargs: Any,
                ) -> Any:
                    nonlocal replacement_message_id, writer_committed
                    result = await original_execute(
                        statement,
                        parameters,
                        **execute_kwargs,
                    )
                    if (
                        not writer_committed
                        and "SELECT COUNT(*) FROM messages m" in str(statement)
                        and isinstance(parameters, dict)
                        and parameters.get("thread_id") == "snapshot-thread"
                    ):
                        writer_committed = True
                        async with get_session() as writer:
                            replacement_result = await writer.execute(
                                text(
                                    "INSERT INTO messages "
                                    "(project_id, sender_id, thread_id, subject, body_md, "
                                    "importance, ack_required, created_ts, attachments) "
                                    "VALUES (:project_id, :sender_id, 'snapshot-thread', "
                                    "'Raced replacement', 'Raced replacement body', "
                                    "'normal', 0, '2039-01-01 00:00:00.000000', '[]') "
                                    "RETURNING id"
                                ),
                                {"project_id": project_id, "sender_id": sender_id},
                            )
                            replacement_message_id = int(
                                replacement_result.scalar_one()
                            )
                            await writer.execute(
                                text(
                                    "DELETE FROM messages "
                                    "WHERE id IN (:oldest_id, :newest_id)"
                                ),
                                {
                                    "oldest_id": oldest_message_id,
                                    "newest_id": newest_message_id,
                                },
                            )
                            await writer.commit()
                    return result

                monkeypatch.setattr(
                    session,
                    "execute",
                    execute_with_concurrent_writer,
                )
                yield session

        monkeypatch.setattr(http_module, "get_session", race_after_thread_count)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=cookie,
        ) as client:
            response = await client.get(
                f"/mail/api/v1/projects/{project_id}/threads",
                params={"thread_id": "snapshot-thread"},
            )

        assert writer_committed is True
        assert response.status_code == 200
        payload = response.json()
        assert payload["subject"] == "Snapshot oldest"
        assert payload["total"] == 2
        assert [item["id"] for item in payload["items"]] == [
            newest_message_id,
            oldest_message_id,
        ]
        assert replacement_message_id is not None
        async with get_session() as session:
            current_rows = (
                await session.execute(
                    text(
                        "SELECT id, subject FROM messages "
                        "WHERE project_id = :project_id "
                        "AND thread_id = 'snapshot-thread'"
                    ),
                    {"project_id": project_id},
                )
            ).all()
        assert current_rows == [(replacement_message_id, "Raced replacement")]

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
                f"/mail/api/v1/projects/{project_id}/threads",
                params={"thread_id": "stable-thread", "limit": 2},
            )
            thread_second = await client.get(
                f"/mail/api/v1/projects/{project_id}/threads",
                params={
                    "thread_id": "stable-thread",
                    "limit": 2,
                    "cursor": thread_first.json()["next_cursor"],
                },
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
        assert thread_first.json()["subject"] == "Page item 0"
        assert thread_second.json()["subject"] == "Page item 0"
        assert thread_first.json()["total"] == 5
        assert thread_second.json()["total"] == 5
        assert not (
            {item["id"] for item in thread_first.json()["items"]}
            & {item["id"] for item in thread_second.json()["items"]}
        )
        for response in [*malformed, *invalid_limits]:
            assert response.status_code == 422
            assert response.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_cursor_message_id_above_sqlite_i64_is_typed_422(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("api-v1-oversized-cursor-admin")
        oversized_cursor = http_module._mail_ui_encode_cursor(
            "2040-02-03 04:05:06.000000",
            9_223_372_036_854_775_808,
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("api-v1-oversized-cursor-admin", epoch),
        ) as client:
            responses = [
                await client.get(
                    "/mail/api/v1/inbox",
                    params={"cursor": oversized_cursor},
                ),
                await client.get(
                    "/mail/api/v1/projects/1/threads",
                    params={
                        "thread_id": "stable-thread",
                        "cursor": oversized_cursor,
                    },
                ),
            ]

        for response in responses:
            assert response.status_code == 422
            assert response.json() == {"detail": "Invalid cursor."}
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
        project_generation, recipient_refs = await _compose_lifetime_refs(
            project_id,
            "ValidationTarget",
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("validation-redaction-admin", epoch),
        ) as client:
            compose = await client.post(
                f"/mail/api/v1/projects/{project_id}/messages",
                json={
                    "idempotency_key": secret_key,
                    "expected_project_generation": project_generation,
                    "recipients": recipient_refs,
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
                    "expected_project_generation": project_generation,
                    "recipients": recipient_refs,
                    "subject": "Valid subject",
                    "body_md": secret_body,
                    "unexpected": secret_body,
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            legacy_name_recipients = await client.post(
                f"/mail/api/v1/projects/{project_id}/messages",
                json={
                    "idempotency_key": secret_key,
                    "expected_project_generation": project_generation,
                    "recipients": ["ValidationTarget"],
                    "subject": "Valid subject",
                    "body_md": secret_body,
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            duplicate_recipient_lifetimes = await client.post(
                f"/mail/api/v1/projects/{project_id}/messages",
                json={
                    "idempotency_key": secret_key,
                    "expected_project_generation": project_generation,
                    "recipients": [recipient_refs[0], recipient_refs[0]],
                    "subject": "Valid subject",
                    "body_md": secret_body,
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        for response in (
            compose,
            reply,
            malformed_reply_path,
            malformed_compose_path,
            legacy_name_recipients,
            duplicate_recipient_lifetimes,
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
            "expected_project_generation": "a" * 64,
            "recipients": [
                {
                    "agent_id": 1,
                    "expected_agent_generation": "b" * 64,
                }
            ],
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
            project_generation, recipient_refs = await _compose_lifetime_refs(
                project_id,
                "FailureTarget",
            )
            valid_payload = {
                **payload,
                "expected_project_generation": project_generation,
                "recipients": recipient_refs,
            }
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
                json=valid_payload,
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
                json=valid_payload,
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
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE ui_users SET preferred_ui_locale = 'my-MM', "
                    "preferred_correspondence_locale = NULL "
                    "WHERE username = 'compose-admin'"
                )
            )
            await session.commit()
        notified: list[str] = []
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO agents "
                    "(project_id, name, program, model, task_description, "
                    "inception_ts, last_active_ts, attachments_policy, contact_policy) "
                    "VALUES (:project_id, 'SecondComposeTarget', 'test', 'test', "
                    "'second recipient', datetime('now'), datetime('now'), "
                    "'auto', 'open')"
                ),
                {"project_id": project_id},
            )
            await session.commit()
        project_generation, recipient_refs = await _compose_lifetime_refs(
            project_id,
            "SecondComposeTarget",
            "ComposeTarget",
        )

        async def record_notification(delivery_id: str) -> None:
            notified.append(delivery_id)

        monkeypatch.setattr(
            http_module,
            "emit_published_delivery_notifications",
            record_notification,
        )
        payload = {
            "idempotency_key": "web-compose-1",
            "expected_project_generation": project_generation,
            "recipients": recipient_refs,
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
            ordered_recipients = list(
                (
                    await session.execute(
                        text(
                            "SELECT agent.name FROM message_delivery_recipients recipient "
                            "JOIN agents agent ON agent.id = recipient.agent_id "
                            "WHERE recipient.delivery_id = :delivery_id "
                            "ORDER BY recipient.ordinal"
                        ),
                        {"delivery_id": first.json()["id"]},
                    )
                ).scalars().all()
            )
        assert row[1] == "HumanOverseer"
        assert "MESSAGE FROM HUMAN OVERSEER" in str(row[0])
        assert "prefers replies in Burmese (my-MM)" in str(row[0])
        assert str(row[0]).endswith("Please inspect the deploy.")
        assert int(row[2]) == delivery_count == 1
        assert ordered_recipients == ["SecondComposeTarget", "ComposeTarget"]
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
        project_generation, recipient_refs = await _compose_lifetime_refs(
            project_id,
            "RetryTarget",
        )
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
                    "expected_project_generation": project_generation,
                    "recipients": recipient_refs,
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
    async def test_reply_fails_closed_without_immutable_delivery_provenance(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        project_id, message_id = await _seed_project(
            "api-v1-legacy-reply",
            subject="Legacy row",
            agent_name="LegacySender",
            sound="soft",
        )
        operator_epoch = await _make_user(
            "legacy-reply-operator",
            role=webauth.ROLE_MEMBER,
        )
        await _assign(
            "legacy-reply-operator",
            project_id,
            webauth.PROJECT_ROLE_OPERATOR,
        )
        path = f"/mail/api/v1/projects/{project_id}/messages/{message_id}"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("legacy-reply-operator", operator_epoch),
        ) as operator:
            detail = await operator.get(path)
            reply = await operator.post(
                f"{path}/replies",
                json={
                    "idempotency_key": "legacy-reply-refused",
                    "expected_sender_agent_id": 1,
                    "expected_sender_agent_generation": "a" * 64,
                    "expected_sender_project_id": project_id,
                    "expected_sender_project_generation": "b" * 64,
                    "body_md": "Must not be routed from mutable rows.",
                },
                headers=SAME_ORIGIN_HEADERS,
            )

        assert detail.status_code == 200
        assert detail.json()["can_reply"] is False
        assert detail.json()["reply_target"] is None
        assert reply.status_code == 409
        assert reply.json() == {
            "detail": {"code": "reply_provenance_unavailable"}
        }

    @pytest.mark.parametrize("cross_project", [False, True])
    @pytest.mark.asyncio
    async def test_reply_rejects_same_id_and_name_from_a_recreated_sender_lifetime(
        self,
        isolated_env,
        monkeypatch,
        cross_project: bool,
    ):
        settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        suffix = "cross" if cross_project else "same"
        inbound = await _publish_inbound_message(
            f"api-v1-reply-lifetime-{suffix}",
            subject="Immutable sender lifetime",
            sender_name="LifetimeTarget",
            cross_project=cross_project,
        )
        project_id = int(inbound["project_id"])
        message_id = int(inbound["message_id"])
        sender_id = int(inbound["sender_id"])
        sender_project_id = int(inbound["sender_project_id"])
        operator_name = f"reply-lifetime-{suffix}"
        operator_epoch = await _make_user(
            operator_name,
            role=webauth.ROLE_MEMBER,
        )
        await _assign(operator_name, project_id, webauth.PROJECT_ROLE_OPERATOR)
        path = f"/mail/api/v1/projects/{project_id}/messages/{message_id}"

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie(operator_name, operator_epoch),
        ) as operator:
            detail_before = await operator.get(path)
            reply_target = detail_before.json()["reply_target"]
            database_url = settings.database.url
            prefix = "sqlite+aiosqlite:///"
            assert database_url.startswith(prefix)
            database_path = Path(database_url.removeprefix(prefix))
            # Reproduce an out-of-band lifetime replacement while preserving
            # the immutable inbound Message row. SQLite enforces its immediate
            # Agent FK before a same-transaction replacement can restore the
            # exact id, so this adversarial setup temporarily disables FK
            # checks on its own raw connection and verifies the final graph.
            with sqlite3.connect(database_path) as connection:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("DELETE FROM agents WHERE id = ?", (sender_id,))
                connection.execute(
                    "INSERT INTO agents "
                    "(id, project_id, name, program, model, task_description, "
                    "inception_ts, last_active_ts, attachments_policy, contact_policy) "
                    "VALUES (?, ?, 'LifetimeTarget', 'pytest', 'test', "
                    "'recreated lifetime', datetime('now'), datetime('now'), "
                    "'auto', 'open')",
                    (sender_id, sender_project_id),
                )
                recreated_generation = str(
                    connection.execute(
                        "SELECT agent_generation FROM agents WHERE id = ?",
                        (sender_id,),
                    ).fetchone()[0]
                )
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
                connection.commit()

            stale = await operator.post(
                f"{path}/replies",
                json={
                    "idempotency_key": f"stale-reply-{suffix}",
                    "expected_sender_agent_id": reply_target["agent_id"],
                    "expected_sender_agent_generation": reply_target[
                        "agent_generation"
                    ],
                    "expected_sender_project_id": reply_target["project_id"],
                    "expected_sender_project_generation": reply_target[
                        "project_generation"
                    ],
                    "body_md": "Do not cross the sender lifetime.",
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            forged_current = await operator.post(
                f"{path}/replies",
                json={
                    "idempotency_key": f"forged-reply-{suffix}",
                    "expected_sender_agent_id": reply_target["agent_id"],
                    "expected_sender_agent_generation": recreated_generation,
                    "expected_sender_project_id": reply_target["project_id"],
                    "expected_sender_project_generation": reply_target[
                        "project_generation"
                    ],
                    "body_md": "Do not retarget the immutable message.",
                },
                headers=SAME_ORIGIN_HEADERS,
            )
            detail_after = await operator.get(path)

        assert detail_before.status_code == detail_after.status_code == 200
        assert reply_target == {
            "agent_id": sender_id,
            "agent_generation": inbound["sender_generation"],
            "project_id": sender_project_id,
            "project_generation": inbound["sender_project_generation"],
            "canonical_name": (
                f"LifetimeTarget@{inbound['sender_project_slug']}"
                if cross_project
                else "LifetimeTarget"
            ),
        }
        assert detail_after.json()["reply_target"] == reply_target
        assert detail_after.json()["can_reply"] is False
        assert detail_after.json()["sender_display_name"] is None
        assert stale.status_code == 409
        assert stale.json() == {"detail": {"code": "reply_target_unavailable"}}
        assert forged_current.status_code == 409
        assert forged_current.json() == {"detail": {"code": "reply_target_changed"}}
        async with get_session() as session:
            authored_count = int(
                (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM message_deliveries "
                            "WHERE actor_kind = 'ui_user'"
                        )
                    )
                ).scalar_one()
            )
        assert authored_count == 0

    @pytest.mark.asyncio
    async def test_operator_reply_is_server_routed_and_viewer_or_foreign_origin_fail(
        self,
        isolated_env,
        monkeypatch,
    ):
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        inbound = await _publish_inbound_message(
            "api-v1-reply",
            subject="Need a human answer",
            sender_name="ReplyTarget",
        )
        project_id = int(inbound["project_id"])
        message_id = int(inbound["message_id"])
        operator_epoch = await _make_user(
            "reply-operator",
            role=webauth.ROLE_MEMBER,
        )
        viewer_epoch = await _make_user("reply-viewer", role=webauth.ROLE_MEMBER)
        await _assign("reply-operator", project_id, webauth.PROJECT_ROLE_OPERATOR)
        await _assign("reply-viewer", project_id, webauth.PROJECT_ROLE_VIEWER)
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE ui_users SET preferred_ui_locale = 'en', "
                    "preferred_correspondence_locale = 'zh-Hant' "
                    "WHERE username = 'reply-operator'"
                )
            )
            await session.commit()
        path = f"/mail/api/v1/projects/{project_id}/messages/{message_id}/replies"

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies=await _cookie("reply-operator", operator_epoch),
        ) as operator:
            detail = await operator.get(
                f"/mail/api/v1/projects/{project_id}/messages/{message_id}"
            )
            reply_target = detail.json()["reply_target"]
            payload = {
                "idempotency_key": "web-reply-1",
                "expected_sender_agent_id": reply_target["agent_id"],
                "expected_sender_agent_generation": reply_target["agent_generation"],
                "expected_sender_project_id": reply_target["project_id"],
                "expected_sender_project_generation": reply_target[
                    "project_generation"
                ],
                "body_md": "Approved.",
            }
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

        assert detail.status_code == 200
        assert detail.json()["can_reply"] is True
        assert reply_target == {
            "agent_id": inbound["sender_id"],
            "agent_generation": inbound["sender_generation"],
            "project_id": inbound["sender_project_id"],
            "project_generation": inbound["sender_project_generation"],
            "canonical_name": "ReplyTarget",
        }
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
                        "sender.name, recipient.name, m.body_md "
                        "FROM messages m "
                        "JOIN agents sender ON sender.id = m.sender_id "
                        "JOIN message_recipients mr ON mr.message_id = m.id "
                        "JOIN agents recipient ON recipient.id = mr.agent_id "
                        "WHERE m.delivery_id = :delivery_id"
                    ),
                    {"delivery_id": response.json()["id"]},
                )
            ).one()
        assert tuple(routed[:6]) == (
            project_id,
            str(message_id),
            message_id,
            "Re: Need a human answer",
            "HumanOverseer",
            "ReplyTarget",
        )
        assert "prefers replies in Traditional Chinese (zh-Hant)" in str(routed[6])
        assert str(routed[6]).endswith("Approved.")

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
