from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Coroutine
from contextlib import closing
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlmodel import select
from typer.testing import CliRunner

from mcp_agent_mail import webauth
from mcp_agent_mail.cli import app
from mcp_agent_mail.db import ensure_schema, get_session, reset_database_state
from mcp_agent_mail.models import (
    Project,
    UiAccessAuditEvent,
    UiProjectAssignment,
    UiUser,
)
from mcp_agent_mail.ui_access import (
    UiAccessMutationError,
    UiAccessMutationResult,
    UiProfileMutationError,
    UiProfileMutationResult,
    mutate_ui_project_access,
    mutate_ui_user_display_name,
    normalize_ui_display_name,
)

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


async def _access_audit_events() -> list[UiAccessAuditEvent]:
    """Read access audit events in insertion order.

    Returns:
        Every immutable access event ordered by primary key.
    """
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(UiAccessAuditEvent).order_by(cast(Any, UiAccessAuditEvent.id))
        )
        return list(result.scalars().all())


async def _project_by_id(project_id: int) -> Project:
    """Read one project by its stable numeric identifier."""
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        return result.scalars().one()


async def _mutate_access(
    *,
    actor_user_id: int | None,
    actor_account_generation: str | None,
    expected_actor_session_epoch: int | None,
    trusted_cli_actor: bool,
    target_user_id: int,
    project_id: int,
    expected_project_generation: str,
    role: webauth.ProjectRole | None,
    expected_access_version: int,
    account_generation: str,
) -> UiAccessMutationResult:
    """Invoke the shared access domain operation with a fresh session."""
    await ensure_schema()
    async with get_session() as session:
        return await mutate_ui_project_access(
            session,
            actor_user_id=actor_user_id,
            actor_account_generation=actor_account_generation,
            expected_actor_session_epoch=expected_actor_session_epoch,
            trusted_cli_actor=trusted_cli_actor,
            target_user_id=target_user_id,
            project_id=project_id,
            expected_project_generation=expected_project_generation,
            role=role,
            expected_access_version=expected_access_version,
            account_generation=account_generation,
        )


async def _mutate_display_name(
    *,
    target_user_id: int,
    account_generation: str,
    expected_session_epoch: int,
    expected_profile_revision: int,
    display_name: str | None,
) -> UiProfileMutationResult:
    """Invoke the shared profile domain operation with a fresh session."""
    await ensure_schema()
    async with get_session() as session:
        return await mutate_ui_user_display_name(
            session,
            target_user_id=target_user_id,
            account_generation=account_generation,
            expected_session_epoch=expected_session_epoch,
            expected_profile_revision=expected_profile_revision,
            display_name=display_name,
        )


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
        profiles = connection.execute(
            "SELECT username, display_name, profile_revision FROM ui_users ORDER BY username"
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
        audit_table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'ui_access_audit_events'"
        ).fetchone()
        immutable_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'ui_access_audit_events_immutable_%'"
            )
        }

    assert roles == [("legacy", "member", 8), ("unknown", "owner", 11)]
    assert first_generations == second_generations
    assert profiles == [("legacy", None, 1), ("unknown", None, 1)]
    assert all(
        isinstance(generation, str) and len(generation) == 64
        for _username, generation in first_generations
    )
    assert assignment_table == ("ui_project_assignments",)
    assert "idx_ui_project_assignments_user_project" in assignment_indexes
    assert {row[2] for row in assignment_foreign_keys} == {"projects", "ui_users"}
    assert {row[6] for row in assignment_foreign_keys} == {"CASCADE"}
    assert audit_table == ("ui_access_audit_events",)
    assert immutable_triggers == {
        "ui_access_audit_events_immutable_bi",
        "ui_access_audit_events_immutable_bu",
        "ui_access_audit_events_immutable_bd",
    }


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
    audit_events = _run_database(_access_audit_events())
    assert [
        (
            event.actor_user_id,
            event.actor_username_snapshot,
            event.target_user_id,
            event.target_username_snapshot,
            event.project_id,
            event.project_slug_snapshot,
            event.old_role,
            event.new_role,
            event.target_epoch_before,
            event.target_epoch_after,
        )
        for event in audit_events
    ] == [
        (
            None,
            "cli",
            ids["member"],
            "member",
            ids["backend"],
            "backend",
            None,
            "operator",
            10,
            11,
        ),
        (
            None,
            "cli",
            ids["member"],
            "member",
            ids["backend"],
            "backend",
            "operator",
            "viewer",
            11,
            12,
        ),
        (
            None,
            "cli",
            ids["member"],
            "member",
            ids["backend"],
            "backend",
            "viewer",
            None,
            12,
            13,
        ),
    ]


def test_display_name_uses_normalized_profile_cas_without_session_revocation(
    isolated_env,
) -> None:
    ids = _run_database(_seed_users_and_projects())
    original, _assignments = _run_database(_user_and_assignments("member"))

    assert normalize_ui_display_name(None) is None
    assert normalize_ui_display_name(" \t\n ") is None
    assert normalize_ui_display_name("  Mateusz\n  Klatt  ") == "Mateusz Klatt"
    assert normalize_ui_display_name("Zo\u0301łw") == "Zółw"
    with pytest.raises(UiProfileMutationError, match="invalid_display_name"):
        normalize_ui_display_name("x" * 129)
    for unsafe_name in ("hidden\x00suffix", "right-to-left\u202eoverride"):
        with pytest.raises(UiProfileMutationError, match="invalid_display_name"):
            normalize_ui_display_name(unsafe_name)

    changed = _run_database(
        _mutate_display_name(
            target_user_id=ids["member"],
            account_generation=original.session_generation,
            expected_session_epoch=original.session_epoch,
            expected_profile_revision=1,
            display_name="  Mateusz\n  Klatt  ",
        )
    )
    assert changed == UiProfileMutationResult(
        changed=True,
        display_name="Mateusz Klatt",
        profile_revision=2,
    )
    after_change, _assignments = _run_database(_user_and_assignments("member"))
    assert after_change.display_name == "Mateusz Klatt"
    assert after_change.profile_revision == 2
    assert after_change.session_epoch == original.session_epoch

    unchanged = _run_database(
        _mutate_display_name(
            target_user_id=ids["member"],
            account_generation=original.session_generation,
            expected_session_epoch=original.session_epoch,
            expected_profile_revision=2,
            display_name="Mateusz   Klatt",
        )
    )
    assert unchanged == UiProfileMutationResult(
        changed=False,
        display_name="Mateusz Klatt",
        profile_revision=2,
    )

    cleared = _run_database(
        _mutate_display_name(
            target_user_id=ids["member"],
            account_generation=original.session_generation,
            expected_session_epoch=original.session_epoch,
            expected_profile_revision=2,
            display_name=" ",
        )
    )
    assert cleared == UiProfileMutationResult(
        changed=True,
        display_name=None,
        profile_revision=3,
    )
    after_clear, _assignments = _run_database(_user_and_assignments("member"))
    assert after_clear.display_name is None
    assert after_clear.profile_revision == 3
    assert after_clear.session_epoch == original.session_epoch

    with pytest.raises(UiProfileMutationError) as stale_revision:
        _run_database(
            _mutate_display_name(
                target_user_id=ids["member"],
                account_generation=original.session_generation,
                expected_session_epoch=original.session_epoch,
                expected_profile_revision=2,
                display_name="Stale",
            )
        )
    assert stale_revision.value.code == "profile_revision_conflict"

    with pytest.raises(UiProfileMutationError) as recreated:
        _run_database(
            _mutate_display_name(
                target_user_id=ids["member"],
                account_generation="different-account-lifetime",
                expected_session_epoch=original.session_epoch,
                expected_profile_revision=3,
                display_name="Wrong account",
            )
        )
    assert recreated.value.code == "account_recreated"

    with sqlite3.connect(_database_path()) as connection:
        connection.execute(
            "UPDATE ui_users SET session_epoch = session_epoch + 1 WHERE id = ?",
            (ids["member"],),
        )
        connection.commit()
    with pytest.raises(UiProfileMutationError) as stale_session:
        _run_database(
            _mutate_display_name(
                target_user_id=ids["member"],
                account_generation=original.session_generation,
                expected_session_epoch=original.session_epoch,
                expected_profile_revision=3,
                display_name="Must not persist",
            )
        )
    assert stale_session.value.code == "session_epoch_conflict"
    after_stale_session, _assignments = _run_database(_user_and_assignments("member"))
    assert after_stale_session.session_epoch == original.session_epoch + 1
    assert after_stale_session.profile_revision == 3
    assert after_stale_session.display_name is None


def test_access_mutation_serializes_stale_writers_and_audits_real_admin(
    isolated_env,
) -> None:
    ids = _run_database(_seed_users_and_projects())
    member, _assignments = _run_database(_user_and_assignments("member"))
    admin, _assignments = _run_database(_user_and_assignments("admin"))
    backend = _run_database(_project_by_id(ids["backend"]))

    async def _race() -> tuple[
        UiAccessMutationResult | UiAccessMutationError,
        UiAccessMutationResult | UiAccessMutationError,
    ]:
        await ensure_schema()

        async def _writer(
            role: webauth.ProjectRole,
        ) -> UiAccessMutationResult | UiAccessMutationError:
            async with get_session() as session:
                try:
                    return await mutate_ui_project_access(
                        session,
                        actor_user_id=ids["admin"],
                        actor_account_generation=admin.session_generation,
                        expected_actor_session_epoch=admin.session_epoch,
                        trusted_cli_actor=False,
                        target_user_id=ids["member"],
                        project_id=ids["backend"],
                        expected_project_generation=backend.project_generation,
                        role=role,
                        expected_access_version=member.session_epoch,
                        account_generation=member.session_generation,
                    )
                except UiAccessMutationError as exc:
                    return exc

        return await asyncio.gather(_writer("viewer"), _writer("operator"))

    outcomes = _run_database(_race())
    successes = [outcome for outcome in outcomes if isinstance(outcome, UiAccessMutationResult)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, UiAccessMutationError)]
    assert len(successes) == 1
    assert successes[0].changed
    assert successes[0].access_version == 11
    assert len(failures) == 1
    assert failures[0].code == "access_version_conflict"

    after_race, assignments = _run_database(_user_and_assignments("member"))
    assert after_race.session_epoch == 11
    assert len(assignments) == 1
    assert assignments[0].role == successes[0].role
    events = _run_database(_access_audit_events())
    assert len(events) == 1
    assert events[0].actor_user_id == ids["admin"]
    assert events[0].actor_username_snapshot == "admin"
    assert events[0].actor_account_generation_snapshot == admin.session_generation
    assert events[0].actor_session_epoch_snapshot == admin.session_epoch
    assert events[0].target_account_generation == member.session_generation
    assert events[0].project_generation_snapshot == backend.project_generation
    assert events[0].new_role == successes[0].role
    assert (events[0].target_epoch_before, events[0].target_epoch_after) == (10, 11)

    no_op = _run_database(
        _mutate_access(
            actor_user_id=ids["admin"],
            actor_account_generation=admin.session_generation,
            expected_actor_session_epoch=admin.session_epoch,
            trusted_cli_actor=False,
            target_user_id=ids["member"],
            project_id=ids["backend"],
            expected_project_generation=backend.project_generation,
            role=successes[0].role,
            expected_access_version=11,
            account_generation=member.session_generation,
        )
    )
    assert no_op == UiAccessMutationResult(
        changed=False,
        role=successes[0].role,
        access_version=11,
    )
    assert len(_run_database(_access_audit_events())) == 1


def test_archived_project_access_remains_mutable_and_audited(isolated_env) -> None:
    ids = _run_database(_seed_users_and_projects())
    member, _assignments = _run_database(_user_and_assignments("member"))
    admin, _assignments = _run_database(_user_and_assignments("admin"))
    backend = _run_database(_project_by_id(ids["backend"]))
    database_path = _database_path()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE projects SET archived_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ids["backend"],),
        )
        connection.commit()

    access_version = member.session_epoch
    for requested_role, expected_old_role in (
        ("viewer", None),
        ("operator", "viewer"),
        (None, "operator"),
    ):
        result = _run_database(
            _mutate_access(
                actor_user_id=ids["admin"],
                actor_account_generation=admin.session_generation,
                expected_actor_session_epoch=admin.session_epoch,
                trusted_cli_actor=False,
                target_user_id=ids["member"],
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role=requested_role,
                expected_access_version=access_version,
                account_generation=member.session_generation,
            )
        )
        access_version += 1
        assert result == UiAccessMutationResult(
            changed=True,
            role=requested_role,
            access_version=access_version,
        )
        assert _run_database(_access_audit_events())[-1].old_role == expected_old_role

    current, assignments = _run_database(_user_and_assignments("member"))
    assert current.session_epoch == member.session_epoch + 3
    assert assignments == []
    events = _run_database(_access_audit_events())
    assert [(event.old_role, event.new_role) for event in events] == [
        (None, "viewer"),
        ("viewer", "operator"),
        ("operator", None),
    ]
    assert all(
        event.project_generation_snapshot == backend.project_generation
        for event in events
    )

    with sqlite3.connect(database_path) as connection:
        archived_at = connection.execute(
            "SELECT archived_at FROM projects WHERE id = ?",
            (ids["backend"],),
        ).fetchone()
        active_ids = {
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM projects WHERE archived_at IS NULL"
            ).fetchall()
        }
    assert archived_at is not None and archived_at[0] is not None
    assert ids["backend"] not in active_ids
    assert ids["frontend"] in active_ids


def test_access_mutation_fail_closed_and_audit_rows_are_immutable(isolated_env) -> None:
    ids = _run_database(_seed_users_and_projects())
    member, _assignments = _run_database(_user_and_assignments("member"))
    admin, _assignments = _run_database(_user_and_assignments("admin"))
    backend = _run_database(_project_by_id(ids["backend"]))

    granted = _run_database(
        _mutate_access(
            actor_user_id=ids["admin"],
            actor_account_generation=admin.session_generation,
            expected_actor_session_epoch=admin.session_epoch,
            trusted_cli_actor=False,
            target_user_id=ids["member"],
            project_id=ids["backend"],
            expected_project_generation=backend.project_generation,
            role="viewer",
            expected_access_version=member.session_epoch,
            account_generation=member.session_generation,
        )
    )
    assert granted.access_version == 11

    with pytest.raises(UiAccessMutationError) as stale_version:
        _run_database(
            _mutate_access(
                actor_user_id=ids["admin"],
                actor_account_generation=admin.session_generation,
                expected_actor_session_epoch=admin.session_epoch,
                trusted_cli_actor=False,
                target_user_id=ids["member"],
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role="operator",
                expected_access_version=10,
                account_generation=member.session_generation,
            )
        )
    assert stale_version.value.code == "access_version_conflict"

    with pytest.raises(UiAccessMutationError) as recreated:
        _run_database(
            _mutate_access(
                actor_user_id=ids["admin"],
                actor_account_generation=admin.session_generation,
                expected_actor_session_epoch=admin.session_epoch,
                trusted_cli_actor=False,
                target_user_id=ids["member"],
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role="operator",
                expected_access_version=11,
                account_generation="different-account-lifetime",
            )
        )
    assert recreated.value.code == "account_recreated"

    with pytest.raises(UiAccessMutationError) as forbidden_actor:
        _run_database(
            _mutate_access(
                actor_user_id=ids["member"],
                actor_account_generation=member.session_generation,
                expected_actor_session_epoch=11,
                trusted_cli_actor=False,
                target_user_id=ids["member"],
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role="operator",
                expected_access_version=11,
                account_generation=member.session_generation,
            )
        )
    assert forbidden_actor.value.code == "actor_forbidden"

    with pytest.raises(UiAccessMutationError) as global_admin:
        _run_database(
            _mutate_access(
                actor_user_id=None,
                actor_account_generation=None,
                expected_actor_session_epoch=None,
                trusted_cli_actor=True,
                target_user_id=ids["admin"],
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role="viewer",
                expected_access_version=admin.session_epoch,
                account_generation=admin.session_generation,
            )
        )
    assert global_admin.value.code == "target_global_admin"

    with pytest.raises(UiAccessMutationError) as missing_target:
        _run_database(
            _mutate_access(
                actor_user_id=ids["admin"],
                actor_account_generation=admin.session_generation,
                expected_actor_session_epoch=admin.session_epoch,
                trusted_cli_actor=False,
                target_user_id=999_999,
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role="operator",
                expected_access_version=1,
                account_generation="missing",
            )
        )
    assert missing_target.value.code == "target_not_found"

    with pytest.raises(UiAccessMutationError) as missing_project:
        _run_database(
            _mutate_access(
                actor_user_id=ids["admin"],
                actor_account_generation=admin.session_generation,
                expected_actor_session_epoch=admin.session_epoch,
                trusted_cli_actor=False,
                target_user_id=ids["member"],
                project_id=999_999,
                expected_project_generation=backend.project_generation,
                role="operator",
                expected_access_version=11,
                account_generation=member.session_generation,
            )
        )
    assert missing_project.value.code == "project_not_found"

    database_path = _database_path()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE ui_users SET disabled = 1 WHERE id = ?",
            (ids["member"],),
        )
        connection.commit()

    with pytest.raises(UiAccessMutationError) as disabled_target:
        _run_database(
            _mutate_access(
                actor_user_id=ids["admin"],
                actor_account_generation=admin.session_generation,
                expected_actor_session_epoch=admin.session_epoch,
                trusted_cli_actor=False,
                target_user_id=ids["member"],
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role="operator",
                expected_access_version=11,
                account_generation=member.session_generation,
            )
        )
    assert disabled_target.value.code == "target_disabled"

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE ui_users SET disabled = 0 WHERE id = ?",
            (ids["member"],),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE ui_access_audit_events SET new_role = 'operator'"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM ui_access_audit_events")
        connection.rollback()

    current, assignments = _run_database(_user_and_assignments("member"))
    assert current.session_epoch == 11
    assert [assignment.role for assignment in assignments] == ["viewer"]
    assert len(_run_database(_access_audit_events())) == 1

    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM ui_users WHERE id = ?", (ids["member"],))
        connection.execute("DELETE FROM projects WHERE id = ?", (ids["backend"],))
        connection.commit()
        preserved_audit = connection.execute(
            "SELECT target_username_snapshot, project_slug_snapshot "
            "FROM ui_access_audit_events"
        ).fetchall()
    assert preserved_audit == [("member", "backend")]


def test_mutators_reject_stale_identity_maps_and_implicit_cli_authority(
    isolated_env,
) -> None:
    ids = _run_database(_seed_users_and_projects())
    member, _assignments = _run_database(_user_and_assignments("member"))
    admin, _assignments = _run_database(_user_and_assignments("admin"))
    backend = _run_database(_project_by_id(ids["backend"]))
    database_path = _database_path()

    async def _stale_access_session() -> str:
        await ensure_schema()
        async with get_session() as session:
            result = await session.execute(
                select(UiUser).where(UiUser.id == ids["admin"])
            )
            cached_actor = result.scalars().one()
            await session.commit()
            assert cached_actor.role == webauth.ROLE_ADMIN
            assert bool(session.identity_map)
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE ui_users SET role = 'member', session_epoch = session_epoch + 1 "
                    "WHERE id = ?",
                    (ids["admin"],),
                )
                connection.commit()
            with pytest.raises(UiAccessMutationError) as refused:
                await mutate_ui_project_access(
                    session,
                    actor_user_id=ids["admin"],
                    actor_account_generation=admin.session_generation,
                    expected_actor_session_epoch=admin.session_epoch,
                    trusted_cli_actor=False,
                    target_user_id=ids["member"],
                    project_id=ids["backend"],
                    expected_project_generation=backend.project_generation,
                    role="viewer",
                    expected_access_version=member.session_epoch,
                    account_generation=member.session_generation,
                )
            return refused.value.code

    async def _stale_profile_session() -> str:
        await ensure_schema()
        async with get_session() as session:
            result = await session.execute(
                select(UiUser).where(UiUser.id == ids["member"])
            )
            cached_target = result.scalars().one()
            await session.commit()
            assert cached_target.display_name is None
            assert bool(session.identity_map)
            with pytest.raises(UiProfileMutationError) as refused:
                await mutate_ui_user_display_name(
                    session,
                    target_user_id=ids["member"],
                    account_generation=member.session_generation,
                    expected_session_epoch=member.session_epoch,
                    expected_profile_revision=member.profile_revision,
                    display_name="Must not persist",
                )
            return refused.value.code

    assert _run_database(_stale_access_session()) == "session_not_fresh"
    assert _run_database(_stale_profile_session()) == "session_not_fresh"

    with pytest.raises(UiAccessMutationError) as implicit_cli:
        _run_database(
            _mutate_access(
                actor_user_id=None,
                actor_account_generation=None,
                expected_actor_session_epoch=None,
                trusted_cli_actor=False,
                target_user_id=ids["member"],
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role="viewer",
                expected_access_version=member.session_epoch,
                account_generation=member.session_generation,
            )
        )
    assert implicit_cli.value.code == "actor_contract_invalid"
    current, assignments = _run_database(_user_and_assignments("member"))
    assert current.session_epoch == member.session_epoch
    assert assignments == []
    assert _run_database(_access_audit_events()) == []


def test_web_actor_generation_and_epoch_are_authoritative_cas_inputs(
    isolated_env,
) -> None:
    ids = _run_database(_seed_users_and_projects())
    member, _assignments = _run_database(_user_and_assignments("member"))
    admin, _assignments = _run_database(_user_and_assignments("admin"))
    backend = _run_database(_project_by_id(ids["backend"]))
    database_path = _database_path()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE ui_users SET session_epoch = session_epoch + 1 WHERE id = ?",
            (ids["admin"],),
        )
        connection.commit()
    with pytest.raises(UiAccessMutationError) as stale_actor:
        _run_database(
            _mutate_access(
                actor_user_id=ids["admin"],
                actor_account_generation=admin.session_generation,
                expected_actor_session_epoch=admin.session_epoch,
                trusted_cli_actor=False,
                target_user_id=ids["member"],
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role="viewer",
                expected_access_version=member.session_epoch,
                account_generation=member.session_generation,
            )
        )
    assert stale_actor.value.code == "actor_session_epoch_conflict"

    replacement_generation = "f" * 64
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM ui_users WHERE id = ?", (ids["admin"],))
        connection.execute(
            """
            INSERT INTO ui_users (
                id, username, password_hash, role, disabled, session_epoch,
                session_generation, display_name, profile_revision,
                preferred_ui_locale, preferred_correspondence_locale,
                created_ts, last_login_ts
            ) VALUES (?, 'recreated-admin', 'unused', 'admin', 0, 20, ?, NULL, 1,
                      'en', NULL, CURRENT_TIMESTAMP, NULL)
            """,
            (ids["admin"], replacement_generation),
        )
        connection.commit()
    with pytest.raises(UiAccessMutationError) as recreated_actor:
        _run_database(
            _mutate_access(
                actor_user_id=ids["admin"],
                actor_account_generation=admin.session_generation,
                expected_actor_session_epoch=admin.session_epoch,
                trusted_cli_actor=False,
                target_user_id=ids["member"],
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role="viewer",
                expected_access_version=member.session_epoch,
                account_generation=member.session_generation,
            )
        )
    assert recreated_actor.value.code == "actor_recreated"
    assert _run_database(_access_audit_events()) == []


def test_project_generation_migrates_and_is_immutable(isolated_env) -> None:
    database_path = _database_path()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                slug VARCHAR(255) NOT NULL,
                human_key VARCHAR(255) NOT NULL,
                created_at DATETIME NOT NULL,
                archived_at DATETIME
            )
            """
        )
        connection.execute(
            "INSERT INTO projects (id, slug, human_key, created_at) "
            "VALUES (1, 'legacy', '/legacy', CURRENT_TIMESTAMP)"
        )
        connection.commit()

    _run_database(ensure_schema())
    with sqlite3.connect(database_path) as connection:
        migrated_generation = connection.execute(
            "SELECT project_generation FROM projects WHERE id = 1"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO projects (id, slug, human_key, created_at) "
            "VALUES (2, 'raw-after-migration', '/raw', CURRENT_TIMESTAMP)"
        )
        connection.commit()
        raw_generation = connection.execute(
            "SELECT project_generation FROM projects WHERE id = 2"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE projects SET project_generation = ? WHERE id = 1",
                ("a" * 64,),
            )
        connection.rollback()

    _run_database(ensure_schema())
    with sqlite3.connect(database_path) as connection:
        migrated_again = connection.execute(
            "SELECT project_generation FROM projects WHERE id = 1"
        ).fetchone()[0]
    assert isinstance(migrated_generation, str) and len(migrated_generation) == 64
    assert isinstance(raw_generation, str) and len(raw_generation) == 64
    assert raw_generation != migrated_generation
    assert migrated_again == migrated_generation


@pytest.mark.parametrize("entity", ["project", "ui-user"])
@pytest.mark.parametrize("collision_kind", ["primary-key", "unique-key"])
@pytest.mark.parametrize(
    "replacement_syntax",
    ["insert-or-replace", "replace"],
)
@pytest.mark.parametrize("recursive_triggers", [False, True], ids=["recursive-off", "recursive-on"])
def test_identity_collision_guards_block_replace_before_assignment_transplant(
    isolated_env,
    entity: str,
    collision_kind: str,
    replacement_syntax: str,
    recursive_triggers: bool,
) -> None:
    ids = _run_database(_seed_users_and_projects())
    database_path = _database_path()

    if replacement_syntax == "insert-or-replace":
        project_statement = (
            "INSERT OR REPLACE INTO projects "
            "(id, slug, human_key, project_generation, created_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)"
        )
        user_statement = (
            "INSERT OR REPLACE INTO ui_users "
            "(id, username, password_hash, role, disabled, session_epoch, "
            "session_generation, display_name, profile_revision, preferred_ui_locale, "
            "preferred_correspondence_locale, created_ts, last_login_ts) "
            "VALUES (?, ?, 'replacement-hash', 'member', 0, 77, ?, NULL, 1, "
            "'en', NULL, CURRENT_TIMESTAMP, NULL)"
        )
    else:
        project_statement = (
            "REPLACE INTO projects "
            "(id, slug, human_key, project_generation, created_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)"
        )
        user_statement = (
            "REPLACE INTO ui_users "
            "(id, username, password_hash, role, disabled, session_epoch, "
            "session_generation, display_name, profile_revision, preferred_ui_locale, "
            "preferred_correspondence_locale, created_ts, last_login_ts) "
            "VALUES (?, ?, 'replacement-hash', 'member', 0, 77, ?, NULL, 1, "
            "'en', NULL, CURRENT_TIMESTAMP, NULL)"
        )

    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ui_project_assignments
                (user_id, project_id, role, created_ts, updated_ts)
            VALUES (?, ?, 'viewer', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (ids["member"], ids["backend"]),
        )
        connection.commit()
        connection.execute(
            "PRAGMA recursive_triggers=ON"
            if recursive_triggers
            else "PRAGMA recursive_triggers=OFF"
        )
        assert connection.execute("PRAGMA recursive_triggers").fetchone() == (
            int(recursive_triggers),
        )

        project_before = connection.execute(
            "SELECT id, slug, human_key, project_generation FROM projects WHERE id = ?",
            (ids["backend"],),
        ).fetchone()
        user_before = connection.execute(
            "SELECT id, username, password_hash, session_epoch, session_generation "
            "FROM ui_users WHERE id = ?",
            (ids["member"],),
        ).fetchone()
        assignment_before = connection.execute(
            "SELECT user_id, project_id, role FROM ui_project_assignments"
        ).fetchall()

        if entity == "project":
            replacement_id = (
                ids["backend"] if collision_kind == "primary-key" else 9_000_001
            )
            replacement_slug = (
                "hostile-project" if collision_kind == "primary-key" else "backend"
            )
            with pytest.raises(sqlite3.IntegrityError, match="projects identity collision"):
                connection.execute(
                    project_statement,
                    (
                        replacement_id,
                        replacement_slug,
                        "/hostile/project",
                        "e" * 64,
                    ),
                )
        else:
            replacement_id = (
                ids["member"] if collision_kind == "primary-key" else 9_000_001
            )
            replacement_username = (
                "hostile-user" if collision_kind == "primary-key" else "member"
            )
            with pytest.raises(sqlite3.IntegrityError, match="ui_users identity collision"):
                connection.execute(
                    user_statement,
                    (replacement_id, replacement_username, "e" * 64),
                )
        connection.rollback()

        assert connection.execute(
            "SELECT id, slug, human_key, project_generation FROM projects WHERE id = ?",
            (ids["backend"],),
        ).fetchone() == project_before
        assert connection.execute(
            "SELECT id, username, password_hash, session_epoch, session_generation "
            "FROM ui_users WHERE id = ?",
            (ids["member"],),
        ).fetchone() == user_before
        assert connection.execute(
            "SELECT user_id, project_id, role FROM ui_project_assignments"
        ).fetchall() == assignment_before

        if entity == "project":
            connection.execute(
                project_statement,
                (9_000_002, "control-project", "/control/project", "d" * 64),
            )
            control_identity = connection.execute(
                "SELECT id, slug, project_generation FROM projects WHERE id = 9000002"
            ).fetchone()
            assert control_identity == (9_000_002, "control-project", "d" * 64)
        else:
            connection.execute(
                user_statement,
                (9_000_002, "control-user", "d" * 64),
            )
            control_identity = connection.execute(
                "SELECT id, username, session_generation FROM ui_users WHERE id = 9000002"
            ).fetchone()
            assert control_identity == (9_000_002, "control-user", "d" * 64)
        connection.commit()

        assert connection.execute(
            "SELECT user_id, project_id, role FROM ui_project_assignments"
        ).fetchall() == assignment_before


@pytest.mark.parametrize("entity", ["project", "ui-user"])
@pytest.mark.parametrize("recursive_triggers", [False, True], ids=["recursive-off", "recursive-on"])
def test_identity_collision_guards_allow_explicit_recreation_as_fresh_lifetime(
    isolated_env,
    entity: str,
    recursive_triggers: bool,
) -> None:
    ids = _run_database(_seed_users_and_projects())
    member, _assignments = _run_database(_user_and_assignments("member"))
    backend = _run_database(_project_by_id(ids["backend"]))
    database_path = _database_path()
    replacement_generation = "c" * 64

    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "PRAGMA recursive_triggers=ON"
            if recursive_triggers
            else "PRAGMA recursive_triggers=OFF"
        )
        if entity == "project":
            connection.execute("DELETE FROM projects WHERE id = ?", (ids["backend"],))
            connection.execute(
                "INSERT INTO projects "
                "(id, slug, human_key, project_generation, created_at) "
                "VALUES (?, 'backend', '/example/recreated', ?, CURRENT_TIMESTAMP)",
                (ids["backend"], replacement_generation),
            )
        else:
            connection.execute("DELETE FROM ui_users WHERE id = ?", (ids["member"],))
            connection.execute(
                """
                INSERT INTO ui_users (
                    id, username, password_hash, role, disabled, session_epoch,
                    session_generation, display_name, profile_revision,
                    preferred_ui_locale, preferred_correspondence_locale,
                    created_ts, last_login_ts
                ) VALUES (?, 'member', 'replacement-hash', 'member', 0, 10, ?, NULL, 1,
                          'en', NULL, CURRENT_TIMESTAMP, NULL)
                """,
                (ids["member"], replacement_generation),
            )
        connection.commit()

    if entity == "project":
        recreated_project = _run_database(_project_by_id(ids["backend"]))
        assert recreated_project.project_generation == replacement_generation
        assert recreated_project.project_generation != backend.project_generation
        with pytest.raises(UiAccessMutationError) as stale_project:
            _run_database(
                _mutate_access(
                    actor_user_id=None,
                    actor_account_generation=None,
                    expected_actor_session_epoch=None,
                    trusted_cli_actor=True,
                    target_user_id=ids["member"],
                    project_id=ids["backend"],
                    expected_project_generation=backend.project_generation,
                    role="viewer",
                    expected_access_version=member.session_epoch,
                    account_generation=member.session_generation,
                )
            )
        assert stale_project.value.code == "project_recreated"
    else:
        recreated_user, assignments = _run_database(_user_and_assignments("member"))
        assert recreated_user.session_generation == replacement_generation
        assert recreated_user.session_generation != member.session_generation
        assert assignments == []
        with pytest.raises(UiProfileMutationError) as stale_account:
            _run_database(
                _mutate_display_name(
                    target_user_id=ids["member"],
                    account_generation=member.session_generation,
                    expected_session_epoch=member.session_epoch,
                    expected_profile_revision=member.profile_revision,
                    display_name="Must not persist",
                )
            )
        assert stale_account.value.code == "account_recreated"


def test_project_delete_recreate_same_id_rejects_stale_assignment_request(
    isolated_env,
) -> None:
    ids = _run_database(_seed_users_and_projects())
    member, _assignments = _run_database(_user_and_assignments("member"))
    backend = _run_database(_project_by_id(ids["backend"]))
    database_path = _database_path()

    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM projects WHERE id = ?", (ids["backend"],))
        connection.execute(
            "INSERT INTO projects (id, slug, human_key, created_at) "
            "VALUES (?, 'backend-recreated', '/example/recreated', CURRENT_TIMESTAMP)",
            (ids["backend"],),
        )
        connection.commit()
        replacement_generation = connection.execute(
            "SELECT project_generation FROM projects WHERE id = ?",
            (ids["backend"],),
        ).fetchone()[0]
    assert replacement_generation != backend.project_generation

    with pytest.raises(UiAccessMutationError) as stale_project:
        _run_database(
            _mutate_access(
                actor_user_id=None,
                actor_account_generation=None,
                expected_actor_session_epoch=None,
                trusted_cli_actor=True,
                target_user_id=ids["member"],
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role="viewer",
                expected_access_version=member.session_epoch,
                account_generation=member.session_generation,
            )
        )
    assert stale_project.value.code == "project_recreated"
    current, assignments = _run_database(_user_and_assignments("member"))
    assert current.session_epoch == member.session_epoch
    assert assignments == []
    assert _run_database(_access_audit_events()) == []


def test_audit_rejects_replace_with_recursive_triggers_off_and_rolls_back(
    isolated_env,
) -> None:
    ids = _run_database(_seed_users_and_projects())
    member, _assignments = _run_database(_user_and_assignments("member"))
    admin, _assignments = _run_database(_user_and_assignments("admin"))
    backend = _run_database(_project_by_id(ids["backend"]))

    granted = _run_database(
        _mutate_access(
            actor_user_id=ids["admin"],
            actor_account_generation=admin.session_generation,
            expected_actor_session_epoch=admin.session_epoch,
            trusted_cli_actor=False,
            target_user_id=ids["member"],
            project_id=ids["backend"],
            expected_project_generation=backend.project_generation,
            role="viewer",
            expected_access_version=member.session_epoch,
            account_generation=member.session_generation,
        )
    )
    assert granted.access_version == 11
    audit_event = _run_database(_access_audit_events())[0]
    assert audit_event.id is not None

    database_path = _database_path()
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA recursive_triggers=OFF")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE ui_access_audit_events SET new_role = 'operator' WHERE id = ?",
                (audit_event.id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM ui_access_audit_events WHERE id = ?",
                (audit_event.id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="id collision"):
            connection.execute(
                "INSERT OR REPLACE INTO ui_access_audit_events "
                "SELECT * FROM ui_access_audit_events WHERE id = ?",
                (audit_event.id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="id collision"):
            connection.execute(
                "REPLACE INTO ui_access_audit_events "
                "SELECT * FROM ui_access_audit_events WHERE id = ?",
                (audit_event.id,),
            )
        connection.rollback()
        preserved = connection.execute(
            "SELECT old_role, new_role, target_epoch_before, target_epoch_after "
            "FROM ui_access_audit_events WHERE id = ?",
            (audit_event.id,),
        ).fetchone()
        connection.execute(
            """
            CREATE TRIGGER force_test_audit_failure
            BEFORE INSERT ON ui_access_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'forced audit failure');
            END
            """
        )
        connection.commit()
    assert preserved == (None, "viewer", 10, 11)

    with pytest.raises(SAIntegrityError, match="forced audit failure"):
        _run_database(
            _mutate_access(
                actor_user_id=ids["admin"],
                actor_account_generation=admin.session_generation,
                expected_actor_session_epoch=admin.session_epoch,
                trusted_cli_actor=False,
                target_user_id=ids["member"],
                project_id=ids["backend"],
                expected_project_generation=backend.project_generation,
                role="operator",
                expected_access_version=11,
                account_generation=member.session_generation,
            )
        )
    current, assignments = _run_database(_user_and_assignments("member"))
    assert current.session_epoch == 11
    assert [assignment.role for assignment in assignments] == ["viewer"]
    assert len(_run_database(_access_audit_events())) == 1


def test_profile_compare_and_swap_serializes_concurrent_writers(isolated_env) -> None:
    ids = _run_database(_seed_users_and_projects())
    member, _assignments = _run_database(_user_and_assignments("member"))

    async def _race() -> tuple[
        UiProfileMutationResult | UiProfileMutationError,
        UiProfileMutationResult | UiProfileMutationError,
    ]:
        await ensure_schema()

        async def _writer(
            display_name: str,
        ) -> UiProfileMutationResult | UiProfileMutationError:
            async with get_session() as session:
                try:
                    return await mutate_ui_user_display_name(
                        session,
                        target_user_id=ids["member"],
                        account_generation=member.session_generation,
                        expected_session_epoch=member.session_epoch,
                        expected_profile_revision=member.profile_revision,
                        display_name=display_name,
                    )
                except UiProfileMutationError as exc:
                    return exc

        return await asyncio.gather(_writer("Alice"), _writer("Alicja"))

    outcomes = _run_database(_race())
    successes = [outcome for outcome in outcomes if isinstance(outcome, UiProfileMutationResult)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, UiProfileMutationError)]
    assert len(successes) == 1
    assert successes[0].changed
    assert successes[0].profile_revision == 2
    assert len(failures) == 1
    assert failures[0].code == "profile_revision_conflict"
    current, _assignments = _run_database(_user_and_assignments("member"))
    assert current.display_name == successes[0].display_name
    assert current.profile_revision == 2
    assert current.session_epoch == member.session_epoch


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
