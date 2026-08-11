"""Atomic domain operations for human profiles and project access."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Literal, cast

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .models import (
    Project,
    UiAccessAuditEvent,
    UiProjectAssignment,
    UiUser,
)
from .webauth import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ProjectRole,
    normalize_project_role,
    normalize_ui_user_role,
)

_CLI_ACTOR_USERNAME: Final = "cli"
_MAX_DISPLAY_NAME_LENGTH: Final = 128

UiAccessMutationErrorCode = Literal[
    "actor_contract_invalid",
    "actor_forbidden",
    "actor_recreated",
    "actor_session_epoch_conflict",
    "target_not_found",
    "target_disabled",
    "target_global_admin",
    "target_invalid_global_role",
    "account_recreated",
    "access_version_conflict",
    "project_not_found",
    "project_recreated",
    "invalid_requested_role",
    "invalid_existing_role",
    "session_not_fresh",
    "compare_and_swap_failed",
]

UiProfileMutationErrorCode = Literal[
    "target_not_found",
    "target_disabled",
    "account_recreated",
    "session_epoch_conflict",
    "profile_revision_conflict",
    "invalid_display_name",
    "session_not_fresh",
    "compare_and_swap_failed",
]


class UiAccessMutationError(RuntimeError):
    """A fail-closed human project-access mutation refusal."""

    def __init__(self, code: UiAccessMutationErrorCode) -> None:
        """Create a refusal with one stable machine-readable code."""
        super().__init__(code)
        self.code = code


class UiProfileMutationError(RuntimeError):
    """A fail-closed human profile mutation refusal."""

    def __init__(self, code: UiProfileMutationErrorCode) -> None:
        """Create a refusal with one stable machine-readable code."""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class UiAccessMutationResult:
    """Outcome of one requested project-role mutation."""

    changed: bool
    role: ProjectRole | None
    access_version: int


@dataclass(frozen=True, slots=True)
class UiProfileMutationResult:
    """Outcome of one requested display-name mutation."""

    changed: bool
    display_name: str | None
    profile_revision: int


def normalize_ui_display_name(value: str | None) -> str | None:
    """Normalize a human label, treating blank input as a clear operation.

    Args:
        value: User-supplied display name or ``None``.

    Returns:
        A whitespace-normalized label, or ``None`` when the label is cleared.

    Raises:
        UiProfileMutationError: If the normalized label exceeds 128 characters
            or the runtime value is not a string/``None``.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise UiProfileMutationError("invalid_display_name")
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    if not normalized:
        return None
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise UiProfileMutationError("invalid_display_name")
    if len(normalized) > _MAX_DISPLAY_NAME_LENGTH:
        raise UiProfileMutationError("invalid_display_name")
    return normalized


async def _begin_immediate(
    session: AsyncSession,
    *,
    profile_operation: bool,
) -> None:
    """Acquire SQLite's writer lock before reading mutation preconditions."""
    if (
        session.in_transaction()
        or bool(session.identity_map)
        or bool(session.new)
        or bool(session.dirty)
        or bool(session.deleted)
    ):
        if profile_operation:
            raise UiProfileMutationError("session_not_fresh")
        raise UiAccessMutationError("session_not_fresh")
    connection = await session.connection()
    await connection.exec_driver_sql("BEGIN IMMEDIATE")


async def mutate_ui_project_access(
    session: AsyncSession,
    *,
    actor_user_id: int | None,
    actor_account_generation: str | None,
    expected_actor_session_epoch: int | None,
    trusted_cli_actor: bool,
    target_user_id: int,
    project_id: int,
    expected_project_generation: str,
    role: ProjectRole | None,
    expected_access_version: int,
    account_generation: str,
) -> UiAccessMutationResult:
    """Grant, replace, or revoke one explicit project role atomically.

    The function owns the transaction on ``session``. The caller must provide a
    fresh session and must not issue a query first. ``BEGIN IMMEDIATE`` creates a
    serialized read/write boundary; the generation and epoch predicates then
    provide an explicit compare-and-swap guard against stale callers and account
    recreation.

    ``actor_user_id=None`` is accepted only together with
    ``trusted_cli_actor=True`` and no actor cookie claims. Web callers must
    provide all three authenticated administrator claims and leave that marker
    false, so an omitted actor cannot silently inherit CLI authority.

    Args:
        session: Fresh async session whose transaction this operation owns.
        actor_user_id: Global administrator performing the change, or ``None``
            for the trusted local CLI.
        actor_account_generation: Web actor's immutable account-lifetime token.
        expected_actor_session_epoch: Web actor's expected session epoch.
        trusted_cli_actor: Explicit capability marker for a local CLI mutation.
        target_user_id: Member account receiving or losing access.
        project_id: Project whose assignment is changing.
        expected_project_generation: Project row's immutable lifetime token.
        role: ``viewer``/``operator`` to grant, or ``None`` to revoke.
        expected_access_version: Target's expected ``session_epoch``.
        account_generation: Target's immutable account-lifetime token.

    Returns:
        Whether data changed, the effective role, and the current access version.

    Raises:
        UiAccessMutationError: If any authorization, identity, version, or data
            integrity precondition fails.
    """
    requested_role = normalize_project_role(role) if role is not None else None
    if role is not None and requested_role is None:
        raise UiAccessMutationError("invalid_requested_role")

    await _begin_immediate(session, profile_operation=False)
    try:
        actor_username = _CLI_ACTOR_USERNAME
        actor_generation_snapshot: str | None = None
        actor_epoch_snapshot: int | None = None
        if actor_user_id is None:
            if (
                not trusted_cli_actor
                or actor_account_generation is not None
                or expected_actor_session_epoch is not None
            ):
                raise UiAccessMutationError("actor_contract_invalid")
        else:
            if (
                trusted_cli_actor
                or actor_account_generation is None
                or expected_actor_session_epoch is None
            ):
                raise UiAccessMutationError("actor_contract_invalid")
            actor_result = await session.execute(
                select(UiUser)
                .where(UiUser.id == actor_user_id)
                .execution_options(populate_existing=True)
            )
            actor = actor_result.scalars().first()
            if actor is None:
                raise UiAccessMutationError("actor_forbidden")
            if actor.session_generation != actor_account_generation:
                raise UiAccessMutationError("actor_recreated")
            if actor.session_epoch != expected_actor_session_epoch:
                raise UiAccessMutationError("actor_session_epoch_conflict")
            if actor.disabled or normalize_ui_user_role(actor.role) != ROLE_ADMIN:
                raise UiAccessMutationError("actor_forbidden")
            actor_username = actor.username
            actor_generation_snapshot = actor_account_generation
            actor_epoch_snapshot = expected_actor_session_epoch

        target_result = await session.execute(
            select(UiUser)
            .where(UiUser.id == target_user_id)
            .execution_options(populate_existing=True)
        )
        target = target_result.scalars().first()
        if target is None:
            raise UiAccessMutationError("target_not_found")
        if target.disabled:
            raise UiAccessMutationError("target_disabled")
        target_role = normalize_ui_user_role(target.role)
        if target_role == ROLE_ADMIN:
            raise UiAccessMutationError("target_global_admin")
        if target_role != ROLE_MEMBER:
            raise UiAccessMutationError("target_invalid_global_role")
        if target.session_generation != account_generation:
            raise UiAccessMutationError("account_recreated")
        if target.session_epoch != expected_access_version:
            raise UiAccessMutationError("access_version_conflict")

        project_result = await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .execution_options(populate_existing=True)
        )
        project = project_result.scalars().first()
        if project is None:
            raise UiAccessMutationError("project_not_found")
        if project.project_generation != expected_project_generation:
            raise UiAccessMutationError("project_recreated")

        assignment_result = await session.execute(
            select(UiProjectAssignment).where(
                UiProjectAssignment.user_id == target_user_id,
                UiProjectAssignment.project_id == project_id,
            ).execution_options(populate_existing=True)
        )
        assignment = assignment_result.scalars().first()
        old_role = None
        if assignment is not None:
            old_role = normalize_project_role(assignment.role)
            if old_role is None:
                raise UiAccessMutationError("invalid_existing_role")

        if old_role == requested_role:
            await session.commit()
            return UiAccessMutationResult(
                changed=False,
                role=requested_role,
                access_version=expected_access_version,
            )

        next_access_version = expected_access_version + 1
        epoch_update = await session.execute(
            update(UiUser)
            .where(cast(Any, UiUser.id == target_user_id))
            .where(cast(Any, UiUser.session_generation == account_generation))
            .where(cast(Any, UiUser.session_epoch == expected_access_version))
            .where(cast(Any, UiUser.disabled == False))  # noqa: E712
            .where(cast(Any, UiUser.role == ROLE_MEMBER))
            .values(session_epoch=next_access_version)
        )
        if int(getattr(epoch_update, "rowcount", 0) or 0) != 1:
            raise UiAccessMutationError("compare_and_swap_failed")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if requested_role is None:
            if assignment is None:  # Defensive: equality above handles this.
                raise UiAccessMutationError("compare_and_swap_failed")
            await session.delete(assignment)
        elif assignment is None:
            session.add(
                UiProjectAssignment(
                    user_id=target_user_id,
                    project_id=project_id,
                    role=requested_role,
                    created_ts=now,
                    updated_ts=now,
                )
            )
        else:
            assignment.role = requested_role
            assignment.updated_ts = now
            session.add(assignment)

        session.add(
            UiAccessAuditEvent(
                actor_user_id=actor_user_id,
                actor_username_snapshot=actor_username,
                actor_account_generation_snapshot=actor_generation_snapshot,
                actor_session_epoch_snapshot=actor_epoch_snapshot,
                target_user_id=target_user_id,
                target_username_snapshot=target.username,
                target_account_generation=account_generation,
                project_id=project_id,
                project_slug_snapshot=project.slug,
                project_generation_snapshot=expected_project_generation,
                old_role=old_role,
                new_role=requested_role,
                target_epoch_before=expected_access_version,
                target_epoch_after=next_access_version,
                created_ts=now,
            )
        )
        await session.commit()
        return UiAccessMutationResult(
            changed=True,
            role=requested_role,
            access_version=next_access_version,
        )
    except BaseException:
        if session.in_transaction():
            await session.rollback()
        raise


async def mutate_ui_user_display_name(
    session: AsyncSession,
    *,
    target_user_id: int,
    account_generation: str,
    expected_session_epoch: int,
    expected_profile_revision: int,
    display_name: str | None,
) -> UiProfileMutationResult:
    """Update one current account lifetime's human-readable label atomically.

    Display-name changes use their own revision and intentionally do not bump
    ``session_epoch``: changing presentation metadata must not revoke browser
    sessions or create a false authorization event.

    Args:
        session: Fresh async session whose transaction this operation owns.
        target_user_id: Account changing its display name.
        account_generation: Immutable account-lifetime token.
        expected_session_epoch: Browser session's expected authorization epoch.
        expected_profile_revision: Client's expected profile revision.
        display_name: New label; blank/``None`` clears it.

    Returns:
        Whether data changed, the normalized label, and current revision.

    Raises:
        UiProfileMutationError: If validation, identity, or version checks fail.
    """
    normalized_display_name = normalize_ui_display_name(display_name)
    await _begin_immediate(session, profile_operation=True)
    try:
        target_result = await session.execute(
            select(UiUser)
            .where(UiUser.id == target_user_id)
            .execution_options(populate_existing=True)
        )
        target = target_result.scalars().first()
        if target is None:
            raise UiProfileMutationError("target_not_found")
        if target.disabled:
            raise UiProfileMutationError("target_disabled")
        if target.session_generation != account_generation:
            raise UiProfileMutationError("account_recreated")
        if target.session_epoch != expected_session_epoch:
            raise UiProfileMutationError("session_epoch_conflict")
        if target.profile_revision != expected_profile_revision:
            raise UiProfileMutationError("profile_revision_conflict")

        if target.display_name == normalized_display_name:
            await session.commit()
            return UiProfileMutationResult(
                changed=False,
                display_name=normalized_display_name,
                profile_revision=expected_profile_revision,
            )

        next_profile_revision = expected_profile_revision + 1
        profile_update = await session.execute(
            update(UiUser)
            .where(cast(Any, UiUser.id == target_user_id))
            .where(cast(Any, UiUser.session_generation == account_generation))
            .where(cast(Any, UiUser.session_epoch == expected_session_epoch))
            .where(cast(Any, UiUser.profile_revision == expected_profile_revision))
            .where(cast(Any, UiUser.disabled == False))  # noqa: E712
            .values(
                display_name=normalized_display_name,
                profile_revision=next_profile_revision,
            )
        )
        if int(getattr(profile_update, "rowcount", 0) or 0) != 1:
            raise UiProfileMutationError("compare_and_swap_failed")
        await session.commit()
        return UiProfileMutationResult(
            changed=True,
            display_name=normalized_display_name,
            profile_revision=next_profile_revision,
        )
    except BaseException:
        if session.in_transaction():
            await session.rollback()
        raise
