from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from sqlmodel import select
from typer.testing import CliRunner

from mcp_agent_mail import webauth
from mcp_agent_mail.cli import app
from mcp_agent_mail.db import ensure_schema, get_session, reset_database_state
from mcp_agent_mail.models import Project, UiProjectAssignment, UiUser

T = TypeVar("T")


def _run_database(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run one database coroutine and release its process-global engine.

    Args:
        coroutine: Awaitable database operation.

    Returns:
        The coroutine result.
    """
    try:
        return asyncio.run(coroutine)
    finally:
        reset_database_state()


async def _seed_users_and_projects() -> dict[str, int]:
    """Create deterministic RBAC users and projects for CLI tests.

    Returns:
        Database identifiers keyed by fixture name.
    """
    await ensure_schema()
    async with get_session() as session:
        backend = Project(slug="backend", human_key="/example/backend")
        frontend = Project(slug="frontend", human_key="/example/frontend")
        member = UiUser(
            username="member",
            password_hash="unused",
            role=webauth.ROLE_MEMBER,
            session_epoch=10,
        )
        admin = UiUser(
            username="admin",
            password_hash="unused",
            role=webauth.ROLE_ADMIN,
            session_epoch=20,
        )
        session.add_all([backend, frontend, member, admin])
        await session.commit()
        for row in (backend, frontend, member, admin):
            await session.refresh(row)
        assert backend.id is not None
        assert frontend.id is not None
        assert member.id is not None
        assert admin.id is not None
        return {
            "backend": backend.id,
            "frontend": frontend.id,
            "member": member.id,
            "admin": admin.id,
        }


async def _user_and_assignments(username: str) -> tuple[UiUser, list[UiProjectAssignment]]:
    """Read one user and all of its project assignments.

    Args:
        username: Exact login name.

    Returns:
        The user row and assignments ordered by project identifier.
    """
    await ensure_schema()
    async with get_session() as session:
        user_result = await session.execute(select(UiUser).where(UiUser.username == username))
        user = user_result.scalars().one()
        assert user.id is not None
        assignments_result = await session.execute(
            select(UiProjectAssignment)
            .where(UiProjectAssignment.user_id == user.id)
            .order_by(cast(Any, UiProjectAssignment.project_id))
        )
        return user, list(assignments_result.scalars().all())


def _database_path() -> Path:
    """Return the isolated SQLite file selected by the test fixture.

    Returns:
        Filesystem path from ``DATABASE_URL``.
    """
    prefix = "sqlite+aiosqlite:///"
    database_url = os.environ["DATABASE_URL"]
    assert database_url.startswith(prefix)
    return Path(database_url.removeprefix(prefix))


def test_role_normalizers_fail_closed_for_unknown_and_legacy_global_values() -> None:
    assert webauth.normalize_ui_user_role("admin") == webauth.ROLE_ADMIN
    assert webauth.normalize_ui_user_role("member") == webauth.ROLE_MEMBER
    assert webauth.normalize_ui_user_role("viewer") is None
    assert webauth.normalize_ui_user_role("owner") is None
    assert webauth.normalize_ui_user_role(None) is None

    assert webauth.project_role_allows_view("viewer")
    assert webauth.project_role_allows_view("operator")
    assert not webauth.project_role_allows_view("owner")
    assert not webauth.project_role_allows_operate("viewer")
    assert webauth.project_role_allows_operate("operator")
    assert not webauth.project_role_allows_operate("owner")


def test_schema_migrates_legacy_viewer_once_and_preserves_unknown_role(isolated_env) -> None:
    database_path = _database_path()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE ui_users (
                id INTEGER PRIMARY KEY,
                username VARCHAR(64) NOT NULL,
                password_hash VARCHAR(256) NOT NULL,
                role VARCHAR(16) NOT NULL,
                disabled BOOLEAN NOT NULL,
                session_epoch INTEGER NOT NULL,
                created_ts DATETIME NOT NULL,
                last_login_ts DATETIME
            )
            """
        )
        connection.execute(
            """
            INSERT INTO ui_users
                (id, username, password_hash, role, disabled, session_epoch, created_ts)
            VALUES
                (1, 'legacy', 'unused', 'viewer', 0, 7, CURRENT_TIMESTAMP),
                (2, 'unknown', 'unused', 'owner', 0, 11, CURRENT_TIMESTAMP)
            """
        )
        connection.commit()

    _run_database(ensure_schema())

    with sqlite3.connect(database_path) as connection:
        first_generations = connection.execute(
            "SELECT username, session_generation FROM ui_users ORDER BY username"
        ).fetchall()

    _run_database(ensure_schema())

    with sqlite3.connect(database_path) as connection:
        roles = connection.execute(
            "SELECT username, role, session_epoch FROM ui_users ORDER BY username"
        ).fetchall()
        second_generations = connection.execute(
            "SELECT username, session_generation FROM ui_users ORDER BY username"
        ).fetchall()
        assignment_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ui_project_assignments'"
        ).fetchone()
        assignment_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('ui_project_assignments')")
        }
        assignment_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list('ui_project_assignments')"
        ).fetchall()

    assert roles == [("legacy", "member", 8), ("unknown", "owner", 11)]
    assert first_generations == second_generations
    assert all(
        isinstance(generation, str) and len(generation) == 64
        for _username, generation in first_generations
    )
    assert assignment_table == ("ui_project_assignments",)
    assert "idx_ui_project_assignments_user_project" in assignment_indexes
    assert {row[2] for row in assignment_foreign_keys} == {"projects", "ui_users"}
    assert {row[6] for row in assignment_foreign_keys} == {"CASCADE"}


def test_account_recreation_uses_a_new_session_generation(
    isolated_env,
    monkeypatch,
) -> None:
    _run_database(_seed_users_and_projects())
    original, _assignments = _run_database(_user_and_assignments("member"))
    original_token = webauth.make_session(
        original.username,
        epoch=original.session_epoch,
        generation=original.session_generation,
        now=100.0,
        ttl=1000.0,
        secret=b"test-session-secret",
    )
    monkeypatch.setattr(webauth, "hash_password", lambda _password: "new-hash")
    runner = CliRunner()

    removed = runner.invoke(app, ["ui-users", "remove", "member", "--yes"])
    recreated = runner.invoke(
        app,
        ["ui-users", "add", "member", "--role", "member"],
        input="new-password\nnew-password\n",
    )

    assert removed.exit_code == 0, removed.output
    assert recreated.exit_code == 0, recreated.output
    replacement, _replacement_assignments = _run_database(_user_and_assignments("member"))
    verified = webauth.verify_session(
        original_token,
        now=101.0,
        secret=b"test-session-secret",
    )
    assert verified == (
        original.username,
        original.session_epoch,
        original.session_generation,
    )
    assert replacement.session_generation != original.session_generation
    assert verified[2] != replacement.session_generation


def test_grant_revoke_access_and_list_bump_epoch_only_on_change(isolated_env) -> None:
    ids = _run_database(_seed_users_and_projects())
    runner = CliRunner()

    initial_user, initial_assignments = _run_database(_user_and_assignments("member"))
    assert initial_user.session_epoch == 10
    assert initial_assignments == []

    granted = runner.invoke(
        app,
        ["ui-users", "grant", "member", "backend", "--role", "operator"],
    )
    assert granted.exit_code == 0, granted.output
    after_grant, assignments = _run_database(_user_and_assignments("member"))
    assert after_grant.session_epoch == 11
    assert [(row.role, row.project_id) for row in assignments] == [
        ("operator", ids["backend"])
    ]

    unchanged_grant = runner.invoke(
        app,
        ["ui-users", "grant", "member", "backend", "--role", "operator"],
    )
    assert unchanged_grant.exit_code == 0, unchanged_grant.output
    after_unchanged_grant, _ = _run_database(_user_and_assignments("member"))
    assert after_unchanged_grant.session_epoch == 11

    access = runner.invoke(app, ["ui-users", "access", "member"])
    assert access.exit_code == 0, access.output
    assert "backend" in access.output
    assert "operator" in access.output

    listed = runner.invoke(app, ["ui-users", "list"])
    assert listed.exit_code == 0, listed.output
    assert "member" in listed.output
    assert "1 project(s)" in listed.output
    assert "all projects" in listed.output

    changed_grant = runner.invoke(
        app,
        ["ui-users", "grant", "member", "backend", "--role", "viewer"],
    )
    assert changed_grant.exit_code == 0, changed_grant.output
    after_changed_grant, assignments = _run_database(_user_and_assignments("member"))
    assert after_changed_grant.session_epoch == 12
    assert [row.role for row in assignments] == ["viewer"]

    revoked = runner.invoke(app, ["ui-users", "revoke", "member", "backend"])
    assert revoked.exit_code == 0, revoked.output
    after_revoke, assignments = _run_database(_user_and_assignments("member"))
    assert after_revoke.session_epoch == 13
    assert assignments == []

    unchanged_revoke = runner.invoke(app, ["ui-users", "revoke", "member", "backend"])
    assert unchanged_revoke.exit_code == 0, unchanged_revoke.output
    after_unchanged_revoke, _ = _run_database(_user_and_assignments("member"))
    assert after_unchanged_revoke.session_epoch == 13


def test_admin_project_grant_is_refused_as_redundant(isolated_env) -> None:
    _run_database(_seed_users_and_projects())

    result = CliRunner().invoke(
        app,
        ["ui-users", "grant", "admin", "backend", "--role", "viewer"],
    )

    assert result.exit_code == 1
    assert "global project access" in result.output
    admin, assignments = _run_database(_user_and_assignments("admin"))
    assert admin.session_epoch == 20
    assert assignments == []


def test_global_role_changes_bump_session_epoch(isolated_env) -> None:
    _run_database(_seed_users_and_projects())
    runner = CliRunner()

    promoted = runner.invoke(app, ["ui-users", "role", "member", "admin"])
    assert promoted.exit_code == 0, promoted.output
    promoted_user, _ = _run_database(_user_and_assignments("member"))
    assert promoted_user.role == webauth.ROLE_ADMIN
    assert promoted_user.session_epoch == 11

    demoted = runner.invoke(app, ["ui-users", "role", "member", "member"])
    assert demoted.exit_code == 0, demoted.output
    demoted_user, _ = _run_database(_user_and_assignments("member"))
    assert demoted_user.role == webauth.ROLE_MEMBER
    assert demoted_user.session_epoch == 12


def test_last_enabled_admin_is_protected_when_another_admin_is_disabled(
    isolated_env,
    monkeypatch,
) -> None:
    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            session.add(
                UiUser(
                    username="active-admin",
                    password_hash="old-hash",
                    role=webauth.ROLE_ADMIN,
                    session_epoch=4,
                )
            )
            session.add(
                UiUser(
                    username="disabled-admin",
                    password_hash="unused",
                    role=webauth.ROLE_ADMIN,
                    disabled=True,
                )
            )
            await session.commit()

    _run_database(_seed())
    runner = CliRunner()

    demote = runner.invoke(app, ["ui-users", "role", "active-admin", "member"])
    remove = runner.invoke(app, ["ui-users", "remove", "active-admin", "--yes"])
    disable = runner.invoke(app, ["ui-users", "disable", "active-admin"])
    monkeypatch.setattr(webauth, "hash_password", lambda _password: "new-hash")
    reset_and_demote = runner.invoke(
        app,
        ["ui-users", "add", "active-admin", "--role", "member"],
        input="new-password\nnew-password\n",
    )

    for result in (demote, remove, disable, reset_and_demote):
        assert result.exit_code == 1, result.output
        assert "admin recovery path" in result.output or "lock everyone out" in result.output
    active_admin, _ = _run_database(_user_and_assignments("active-admin"))
    assert active_admin.role == webauth.ROLE_ADMIN
    assert active_admin.session_epoch == 4
    assert active_admin.password_hash == "old-hash"


def test_disabled_last_admin_is_retained_as_a_recovery_account(isolated_env) -> None:
    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            session.add(
                UiUser(
                    username="disabled-recovery-admin",
                    password_hash="unused",
                    role=webauth.ROLE_ADMIN,
                    disabled=True,
                )
            )
            await session.commit()

    _run_database(_seed())

    result = CliRunner().invoke(
        app,
        ["ui-users", "remove", "disabled-recovery-admin", "--yes"],
    )

    assert result.exit_code == 1, result.output
    assert "last administrator account" in result.output
    assert "enabled admin" not in result.output


def test_password_reset_rejects_unknown_role_without_explicit_repair(
    isolated_env,
    monkeypatch,
) -> None:
    async def _seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            session.add(
                UiUser(
                    username="broken",
                    password_hash="old-hash",
                    role="owner",
                    session_epoch=9,
                )
            )
            await session.commit()

    _run_database(_seed())
    monkeypatch.setattr(webauth, "hash_password", lambda _password: "new-hash")

    result = CliRunner().invoke(
        app,
        ["ui-users", "add", "broken"],
        input="new-password\nnew-password\n",
    )

    assert result.exit_code == 1, result.output
    assert "invalid global role" in result.output
    broken, assignments = _run_database(_user_and_assignments("broken"))
    assert broken.role == "owner"
    assert broken.password_hash == "old-hash"
    assert broken.session_epoch == 9
    assert assignments == []

    repaired = CliRunner().invoke(
        app,
        ["ui-users", "add", "broken", "--role", "member"],
        input="new-password\nnew-password\n",
    )
    assert repaired.exit_code == 0, repaired.output
    repaired_user, _ = _run_database(_user_and_assignments("broken"))
    assert repaired_user.role == webauth.ROLE_MEMBER
    assert repaired_user.password_hash == "new-hash"
    assert repaired_user.session_epoch == 10


def test_assignment_lifecycle_follows_user_and_project_deletion(isolated_env) -> None:
    ids = _run_database(_seed_users_and_projects())

    async def _assign() -> None:
        await ensure_schema()
        async with get_session() as session:
            session.add(
                UiProjectAssignment(
                    user_id=ids["member"],
                    project_id=ids["backend"],
                    role=webauth.PROJECT_ROLE_VIEWER,
                )
            )
            session.add(
                UiProjectAssignment(
                    user_id=ids["admin"],
                    project_id=ids["frontend"],
                    role=webauth.PROJECT_ROLE_OPERATOR,
                )
            )
            await session.commit()

    _run_database(_assign())
    database_path = _database_path()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM projects WHERE id = ?", (ids["backend"],))
        connection.execute("DELETE FROM ui_users WHERE id = ?", (ids["admin"],))
        connection.commit()
        remaining = connection.execute(
            "SELECT user_id, project_id FROM ui_project_assignments"
        ).fetchall()

    assert remaining == []


def test_assignment_triggers_reject_orphan_insert_and_update(isolated_env) -> None:
    ids = _run_database(_seed_users_and_projects())
    database_path = _database_path()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO ui_project_assignments
                (user_id, project_id, role, created_ts, updated_ts)
            VALUES (?, ?, 'viewer', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (ids["member"], ids["backend"]),
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="user does not exist"):
            connection.execute(
                """
                INSERT INTO ui_project_assignments
                    (user_id, project_id, role, created_ts, updated_ts)
                VALUES (999999, ?, 'viewer', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (ids["backend"],),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="project does not exist"):
            connection.execute(
                """
                INSERT INTO ui_project_assignments
                    (user_id, project_id, role, created_ts, updated_ts)
                VALUES (?, 999999, 'viewer', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (ids["member"],),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="user does not exist"):
            connection.execute(
                "UPDATE ui_project_assignments SET user_id = 999999 "
                "WHERE user_id = ? AND project_id = ?",
                (ids["member"], ids["backend"]),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="project does not exist"):
            connection.execute(
                "UPDATE ui_project_assignments SET project_id = 999999 "
                "WHERE user_id = ? AND project_id = ?",
                (ids["member"], ids["backend"]),
            )
        connection.rollback()


def test_project_lookup_precedence_and_ambiguous_human_key_rejection(
    isolated_env,
) -> None:
    ids = _run_database(_seed_users_and_projects())

    async def _seed_collisions() -> dict[str, int]:
        await ensure_schema()
        async with get_session() as session:
            preferred = Project(slug="preferred", human_key="/example/preferred")
            colliding_human_key = Project(slug="other", human_key="preferred")
            casefolded_slug = Project(slug="casefolded", human_key="/example/casefolded")
            exact_key_owner = Project(slug="exact-key-owner", human_key="CASEFOLDED")
            ambiguous_one = Project(slug="ambiguous-one", human_key="/shared/project")
            ambiguous_two = Project(slug="ambiguous-two", human_key="/shared/project")
            session.add_all(
                [
                    preferred,
                    colliding_human_key,
                    casefolded_slug,
                    exact_key_owner,
                    ambiguous_one,
                    ambiguous_two,
                ]
            )
            await session.commit()
            for row in (
                preferred,
                colliding_human_key,
                casefolded_slug,
                exact_key_owner,
                ambiguous_one,
                ambiguous_two,
            ):
                await session.refresh(row)
                assert row.id is not None
            return {
                "preferred": cast(int, preferred.id),
                "other": cast(int, colliding_human_key.id),
                "casefolded": cast(int, casefolded_slug.id),
                "exact_key_owner": cast(int, exact_key_owner.id),
            }

    collision_ids = _run_database(_seed_collisions())
    runner = CliRunner()

    exact_slug = runner.invoke(
        app,
        ["ui-users", "grant", "member", "preferred", "--role", "viewer"],
    )
    exact_human_key = runner.invoke(
        app,
        ["ui-users", "grant", "member", "CASEFOLDED", "--role", "operator"],
    )
    ambiguous_key = runner.invoke(
        app,
        ["ui-users", "grant", "member", "/shared/project", "--role", "operator"],
    )

    assert exact_slug.exit_code == 0, exact_slug.output
    assert exact_human_key.exit_code == 0, exact_human_key.output
    assert ambiguous_key.exit_code == 1, ambiguous_key.output
    assert "Ambiguous project" in ambiguous_key.output
    assert "ambiguous-one" in ambiguous_key.output
    assert "ambiguous-two" in ambiguous_key.output
    member, assignments = _run_database(_user_and_assignments("member"))
    assert member.session_epoch == 12
    assert {(row.project_id, row.role) for row in assignments} == {
        (collision_ids["preferred"], "viewer"),
        (collision_ids["exact_key_owner"], "operator"),
    }
    assert collision_ids["other"] not in {row.project_id for row in assignments}
    assert collision_ids["casefolded"] not in {row.project_id for row in assignments}
    assert ids["backend"] not in {row.project_id for row in assignments}
