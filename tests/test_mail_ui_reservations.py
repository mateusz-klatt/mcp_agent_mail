"""The operator's view of live advisory claims.

Three things are worth failing a build over here, and they are the three this
file asserts:

* who may see it at all -- a claim carries a path pattern and a free-text
  reason, and the set of them describes what is being worked on where;
* what "live" means -- SQLite stores these timestamps as text, and the obvious
  SQL comparison silently matches nothing (see `mail_active_file_reservations`);
* how a claim whose owner is gone is described -- none of those states make the
  claim void, and saying otherwise would invite an operator to act on it.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from mcp_agent_mail import webauth
from mcp_agent_mail.db import get_session
from tests.test_mail_ui_auth import (
    SECRET,
    _assign,
    _build,
    _cookie,
    _make_user,
    _seed_project,
)

RESERVATIONS = "/mail/api/v1/reservations"


def _naive(offset: timedelta) -> str:
    """A timestamp in SQLite's own stored shape, not ISO 8601."""
    moment = datetime.now(timezone.utc).replace(tzinfo=None) + offset
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


async def _agent_id(project_id: int, name: str) -> int:
    async with get_session() as session:
        result = await session.execute(
            text("SELECT id FROM agents WHERE project_id = :pid AND name = :name"),
            {"pid": project_id, "name": name},
        )
        return int(result.scalar_one())


async def _add_reservation(
    *,
    project_id: int,
    agent_id: int | None,
    path_pattern: str,
    expires: str,
    created: str | None = None,
    execution_id: str | None = None,
    origin: str = "explicit",
    reason: str = "",
) -> int:
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO file_reservations "
                "(project_id, agent_id, execution_id, origin, path_pattern, "
                " exclusive, reason, created_ts, expires_ts, released_ts, "
                " archive_revision, archive_synced_revision) "
                "VALUES (:pid, :aid, :eid, :origin, :path, 1, :reason, "
                " :created, :expires, NULL, 1, 1)"
            ),
            {
                "pid": project_id,
                "aid": agent_id,
                "eid": execution_id,
                "origin": origin,
                "path": path_pattern,
                "reason": reason,
                "created": created or _naive(timedelta(0)),
                "expires": expires,
            },
        )
        await session.commit()
        result = await session.execute(text("SELECT last_insert_rowid()"))
        return int(result.scalar_one())


async def _add_execution(
    project_id: int, agent_id: int, status: str, *, suffix: str = ""
) -> str:
    # The id column is CHECK-constrained to a UUID; derive one so the
    # fixture stays deterministic across runs.
    execution_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{project_id}/{agent_id}/{status}/{suffix}")
    )
    # The table checks this is a sha256 digest, not merely non-empty: the
    # server stores only the hash of a caller-generated capability token.
    capability = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO agent_executions "
                "(id, project_id, agent_id, external_id, client_name, "
                " execution_token_hash, lifecycle_protocol_version, kind, "
                " status, task_description, started_ts, last_active_ts, "
                " ended_ts) "
                "VALUES (:id, :pid, :aid, :id, 'claude', :capability, 1, 'session', "
                " :status, '', datetime('now'), datetime('now'), :ended)"
            ),
            {
                "id": execution_id,
                "pid": project_id,
                "aid": agent_id,
                "capability": capability,
                "status": status,
                # A terminal status without an end timestamp is refused by
                # `ck_agent_executions_status_end`, and rightly so.
                "ended": None if status == "active" else _naive(timedelta(0)),
            },
        )
        await session.commit()
    return execution_id


async def _client(app, username: str, epoch: int) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies=await _cookie(username, epoch),
    )


@pytest.mark.asyncio
async def test_a_viewer_is_told_nothing_about_claims(isolated_env, monkeypatch):
    """A viewer may read a project's mail without learning what is being edited."""
    _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
    project_id, _message_id = await _seed_project(
        "reservations-viewer", subject="s", agent_name="alpha", sound="soft"
    )
    agent_id = await _agent_id(project_id, "alpha")
    await _add_reservation(
        project_id=project_id,
        agent_id=agent_id,
        path_pattern="src/secret_feature/**",
        expires=_naive(timedelta(hours=1)),
        reason="rewriting the pricing engine",
    )
    epoch = await _make_user("reservations-viewer-human", role=webauth.ROLE_MEMBER)
    await _assign("reservations-viewer-human", project_id, "viewer")

    async with await _client(app, "reservations-viewer-human", epoch) as client:
        unscoped = await client.get(RESERVATIONS)
        scoped = await client.get(RESERVATIONS, params={"project_id": project_id})

    assert unscoped.status_code == 200
    assert unscoped.json() == {"items": [], "next_cursor": None}
    # 404 rather than 403: a viewer must not be able to tell "no such project"
    # apart from "a project you may see but not operate".
    assert scoped.status_code == 404
    assert "secret_feature" not in unscoped.text
    assert "pricing engine" not in scoped.text


@pytest.mark.asyncio
async def test_an_operator_sees_live_claims_in_a_fixed_shape(isolated_env, monkeypatch):
    _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
    project_id, _message_id = await _seed_project(
        "reservations-operator", subject="s", agent_name="alpha", sound="soft"
    )
    agent_id = await _agent_id(project_id, "alpha")
    execution_id = await _add_execution(project_id, agent_id, "active")
    await _add_reservation(
        project_id=project_id,
        agent_id=agent_id,
        path_pattern="src/mcp_agent_mail/http.py",
        expires=_naive(timedelta(hours=1)),
        execution_id=execution_id,
        origin="auto",
        reason="editing",
    )
    epoch = await _make_user("reservations-operator-human", role=webauth.ROLE_MEMBER)
    await _assign("reservations-operator-human", project_id, "operator")

    async with await _client(app, "reservations-operator-human", epoch) as client:
        response = await client.get(RESERVATIONS)

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"items", "next_cursor"}
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert set(item) == {
        "id",
        "project_id",
        "project_slug",
        "holder_name",
        "holder_display_name",
        "path_pattern",
        "exclusive",
        "reason",
        "created_ts",
        "expires_ts",
        "origin",
        "scope_state",
    }
    assert item["holder_name"] == "alpha"
    assert item["path_pattern"] == "src/mcp_agent_mail/http.py"
    assert item["origin"] == "auto"
    assert item["scope_state"] == "execution_scoped"
    # Session topology stays server-side.
    assert "execution_id" not in item
    assert execution_id not in response.text


@pytest.mark.asyncio
async def test_scope_state_names_how_the_owner_stands(isolated_env, monkeypatch):
    """Three states, none of which mean the claim stopped applying."""
    _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
    project_id, _message_id = await _seed_project(
        "reservations-states", subject="s", agent_name="alpha", sound="soft"
    )
    agent_id = await _agent_id(project_id, "alpha")
    live = await _add_execution(project_id, agent_id, "active")
    # "orphaned" is reachable only by transition, never by construction: the
    # database refuses to bind a new claim to an execution that has already
    # ended ("reservation execution binding mismatch"). So this one is taken
    # while its run is alive and the run is ended underneath it, which is
    # exactly how it happens in production.
    doomed = await _add_execution(project_id, agent_id, "active", suffix="doomed")
    expires = _naive(timedelta(hours=1))
    await _add_reservation(
        project_id=project_id,
        agent_id=agent_id,
        path_pattern="scoped/**",
        expires=expires,
        created=_naive(timedelta(minutes=-1)),
        execution_id=live,
    )
    await _add_reservation(
        project_id=project_id,
        agent_id=agent_id,
        path_pattern="orphaned/**",
        expires=expires,
        created=_naive(timedelta(minutes=-2)),
        execution_id=doomed,
    )
    await _add_reservation(
        project_id=project_id,
        agent_id=agent_id,
        path_pattern="legacy/**",
        expires=expires,
        created=_naive(timedelta(minutes=-3)),
        execution_id=None,
    )
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE agent_executions SET status = 'completed', "
                "ended_ts = datetime('now') WHERE id = :id"
            ),
            {"id": doomed},
        )
        await session.commit()

    epoch = await _make_user("reservations-states-human", role=webauth.ROLE_MEMBER)
    await _assign("reservations-states-human", project_id, "operator")

    async with await _client(app, "reservations-states-human", epoch) as client:
        response = await client.get(RESERVATIONS)

    states = {
        item["path_pattern"]: item["scope_state"] for item in response.json()["items"]
    }
    assert states == {
        "scoped/**": "execution_scoped",
        "orphaned/**": "orphaned",
        # The population that must reach zero before the rollout can move from
        # observe to enforce -- which is why it is a distinct state and not
        # folded into "orphaned".
        "legacy/**": "legacy_unscoped",
    }


@pytest.mark.asyncio
async def test_expiry_is_compared_correctly_and_never_hides_a_claim(
    isolated_env, monkeypatch
):
    """The predicate that used to match nothing, and the row that must survive it."""
    _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
    project_id, _message_id = await _seed_project(
        "reservations-expiry", subject="s", agent_name="alpha", sound="soft"
    )
    agent_id = await _agent_id(project_id, "alpha")
    await _add_reservation(
        project_id=project_id,
        agent_id=agent_id,
        path_pattern="live/**",
        expires=_naive(timedelta(hours=1)),
    )
    await _add_reservation(
        project_id=project_id,
        agent_id=agent_id,
        path_pattern="expired/**",
        expires=_naive(timedelta(hours=-1)),
    )
    await _add_reservation(
        project_id=project_id,
        agent_id=agent_id,
        path_pattern="unreadable/**",
        expires="not-a-timestamp",
    )
    epoch = await _make_user("reservations-expiry-human", role=webauth.ROLE_MEMBER)
    await _assign("reservations-expiry-human", project_id, "operator")

    async with await _client(app, "reservations-expiry-human", epoch) as client:
        response = await client.get(RESERVATIONS)

    items = {item["path_pattern"]: item for item in response.json()["items"]}
    # If the comparison regressed to a naive `expires_ts > :now` bind, this is
    # the assertion that fails: the predicate becomes always-false and the
    # endpoint returns an empty list that looks like a working answer.
    assert "live/**" in items
    assert "expired/**" not in items
    # An unparseable expiry is reported rather than dropped: a warning that
    # turns out to be stale costs a glance, a claim that vanishes costs a
    # collision.
    assert "unreadable/**" in items
    assert items["unreadable/**"]["expires_ts"] is None
    assert items["live/**"]["expires_ts"] is not None


@pytest.mark.asyncio
async def test_the_page_is_a_keyset_and_does_not_repeat_or_skip(
    isolated_env, monkeypatch
):
    _settings, app = _build(monkeypatch, MAIL_UI_SESSION_SECRET=SECRET)
    project_id, _message_id = await _seed_project(
        "reservations-paging", subject="s", agent_name="alpha", sound="soft"
    )
    agent_id = await _agent_id(project_id, "alpha")
    expires = _naive(timedelta(hours=1))
    for index in range(5):
        await _add_reservation(
            project_id=project_id,
            agent_id=agent_id,
            path_pattern=f"path/{index}/**",
            expires=expires,
            created=_naive(timedelta(minutes=-index)),
        )
    epoch = await _make_user("reservations-paging-human", role=webauth.ROLE_MEMBER)
    await _assign("reservations-paging-human", project_id, "operator")

    seen: list[str] = []
    async with await _client(app, "reservations-paging-human", epoch) as client:
        cursor: str | None = None
        for _ in range(5):
            params: dict[str, str | int] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            page = await client.get(RESERVATIONS, params=params)
            assert page.status_code == 200
            body = page.json()
            seen.extend(item["path_pattern"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

    assert cursor is None, "pagination did not terminate"
    assert seen == [f"path/{index}/**" for index in range(5)]
    assert len(seen) == len(set(seen))
