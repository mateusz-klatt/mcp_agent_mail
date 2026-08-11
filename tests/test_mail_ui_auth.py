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

import pytest
from authlib.jose import jwt
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from mcp_agent_mail import config as _config, http as http_module, webauth
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app

BEARER = "mail-ui-gate-bearer"
SECRET = "mail-ui-gate-session-secret-0123456789"

# A route the gate protects and that renders without any project existing, so a
# failure here is the gate's answer and not a missing fixture.
GUARDED = "/mail/archive/guide"


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


async def _make_user(username: str = "operator", *, role: str = webauth.ROLE_ADMIN) -> int:
    """Insert a UiUser and return its session_epoch."""
    from mcp_agent_mail.models import UiUser

    await ensure_schema()
    async with get_session() as session:
        user = UiUser(
            username=username,
            password_hash=webauth.hash_password("irrelevant-here"),
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
            "/mail/projects": "aggregate-scoped",
            "/mail/unified-inbox": "aggregate-scoped",
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
        }
