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

import contextlib
import time

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config, webauth
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
    async def test_server_bearer_passes_straight_through_the_gate(
        self, isolated_env, monkeypatch
    ):
        """A Bearer header hands the request to the bearer layer untouched.

        This is deliberate — http.py treats it as an API client — but it is the
        single most surprising property of this middleware and nothing recorded
        it. The password login guards browsers; it does not guard the token that
        every agent in the fleet already holds, which is the same reason a
        bearer alone can read every message through the viewer's JSON API.

        Pinned as behaviour rather than endorsed as a design: if it is ever
        narrowed, this test should fail loudly and be rewritten on purpose,
        instead of the change landing unnoticed because no test looked here.
        """
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        await ensure_schema()
        with_bearer = await _get(app, GUARDED)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            without_bearer = await client.get(GUARDED)

        # The bearer is the only variable between these two requests.
        assert with_bearer.status_code == 200
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


def _cookie(username: str, epoch: int) -> dict[str, str]:
    return {
        "agent_mail_session": webauth.make_session(
            username, epoch=epoch, now=time.time(), secret=SECRET.encode("utf-8")
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
            cookies=_cookie("operator", epoch),
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
        cookies = _cookie("operator", epoch)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
        ) as client:
            before = await client.get(GUARDED)
            await _bump_epoch()
            after = await client.get(GUARDED)

        assert before.status_code == 200
        assert after.status_code == 401

    @pytest.mark.asyncio
    async def test_a_viewer_may_read_but_not_act(self, isolated_env, monkeypatch):
        """Role is enforced on unsafe methods only, so both halves are pinned.

        A viewer that could not read would be a different bug from a viewer that
        could write, and only checking the refusal cannot tell them apart.
        """
        _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
        epoch = await _make_user("reader", role=webauth.ROLE_VIEWER)
        cookies = _cookie("reader", epoch)

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
