"""Command-line interface surface for developer tooling."""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings
import webbrowser
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Iterable, Iterator, List, Optional, Sequence, cast
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

import click
import httpx
import typer
import uvicorn
from decouple import Config as DecoupleConfig, RepositoryEmpty, RepositoryEnv
from filelock import BaseFileLock, FileLock, Timeout as LockTimeout
from git import Repo
from rich.console import Console
from rich.table import Table
from sqlalchemy import (
    and_,
    asc as _sa_asc,
    bindparam,
    desc as _sa_desc,
    func,
    or_ as _sa_or,
    select as _sa_select,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.sql import ColumnElement

from . import tickets
from .app import (
    _LIKE_ESCAPE_CHAR,
    _canonicalize_project_identifier,
    _extract_like_terms,
    _like_escape,
    _sanitize_fts_query,
    _sender_display_name,
    build_mcp_server,
)
from .config import clear_settings_cache, get_settings
from .db import (
    connect_sqlite_readonly,
    ensure_schema,
    get_immediate_session,
    get_session,
    get_sqlite_sidecar_paths,
    reset_database_state,
)
from .guard import install_guard as install_guard_script, uninstall_guard as uninstall_guard_script
from .http import build_http_app
from .models import (
    Agent,
    FileReservation,
    Message,
    MessageDelivery,
    MessageRecipient,
    Product,
    ProductProjectLink,
    Project,
    Ticket,
    TicketEvent,
    WindowIdentity,
)
from .share import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_THRESHOLD,
    DETACH_ATTACHMENT_THRESHOLD,
    INLINE_ATTACHMENT_THRESHOLD,
    SCRUB_PRESETS,
    VIEWER_SCRUB_PRESETS,
    BundleArtifacts,
    HostingHint,
    ShareExportError,
    SnapshotContext,
    build_bundle_assets,
    copy_viewer_assets,
    create_snapshot_context,
    detect_hosting_hints,
    encrypt_bundle,
    package_directory_as_zip,
    prepare_output_directory,
    resolve_sqlite_database_path,
    sign_manifest,
    summarize_snapshot,
)
from .storage import (
    BackupManifest,
    ProjectArchive,
    _project_archive_lock_path,
    _resolved_git_common_dir,
    _write_json_atomic_sync,
    archive_write_lock,
    ensure_archive,
    inspect_agent_archive_rename,
    migrate_agent_archive,
)
from .utils import (
    package_version,
    parse_client_platform_host_agent_id,
    pid_is_alive as _pid_is_alive,
    safe_build_path_component as _safe_build_path_component,
    slugify,
    validate_agent_name_format,
    validate_client_platform_host_agent_id,
    validate_explicit_agent_id,
)
from .webauth import ProjectRole

# Suppress annoying bleach CSS sanitizer warning from dependencies
warnings.filterwarnings("ignore", category=UserWarning, module="bleach")

# Register cleanup handler to dispose database connections on exit.
# aiosqlite uses background threads that can block Python shutdown if not cleaned up.
# See: https://github.com/Dicklesworthstone/mcp_agent_mail/issues/68
atexit.register(reset_database_state)

console = Console()
DEFAULT_ENV_PATH = Path(".env")
ARCHIVE_DIR_NAME = "archived_mailbox_states"
ARCHIVE_METADATA_FILENAME = "metadata.json"
MAILBOX_DATABASE_FILENAME = "mailbox.sqlite3"
SHARE_MANIFEST_FILENAME = "manifest.json"
SHARE_SIGNATURE_FILENAME = "manifest.sig.json"
UI_SESSIONS_INVALIDATED_MESSAGE = "Existing browser sessions for this user were invalidated."
HUMAN_LOGIN_NAME_HELP = "Human login name"
PROJECT_IDENTIFIER_HELP = "Project slug or human key"
AGENT_NAME_HELP = "Agent name"
MESSAGE_LIMIT_HELP = "Max messages to display"
JSON_OUTPUT_HELP = "Output as JSON for machine parsing."
PROJECT_ID_REQUIRED_MESSAGE = "Project must have an id"
PROJECT_AGENT_IDS_REQUIRED_MESSAGE = "Project and agent must have IDs"
TOOLS_CALL_METHOD = "tools/call"
BUILD_SLOTS_LABEL = "build slots"
BUILD_SLOT_LABEL = "build slot"
ARCHIVE_SNAPSHOT_RELATIVE = Path("snapshot") / MAILBOX_DATABASE_FILENAME
ARCHIVE_STORAGE_DIRNAME = Path("storage_repo")
DEFAULT_ARCHIVE_SCRUB_PRESET = "archive"

_SHARE_BUNDLE_OWNED_FILES = frozenset(
    {
        ".nojekyll",
        "HOW_TO_DEPLOY.md",
        "README.md",
        "_headers",
        "chunks.sha256",
        "index.html",
        MAILBOX_DATABASE_FILENAME,
        "mailbox.sqlite3.config.json",
        SHARE_MANIFEST_FILENAME,
        SHARE_SIGNATURE_FILENAME,
    }
)
_SHARE_BUNDLE_OWNED_DIRECTORIES = frozenset({"attachments", "chunks", "viewer"})


def _cli_sender_display(
    *,
    message_project_id: int | None,
    sender_name: str | None,
    sender_project_id: int | None,
    sender_project_slug: str | None,
) -> str:
    canonical_sender = (sender_name or "").strip()
    if not canonical_sender:
        return "Unknown"
    return _sender_display_name(
        message_project_id=message_project_id,
        sender_name=canonical_sender,
        sender_project_id=sender_project_id,
        sender_project_slug=sender_project_slug,
    )


def _format_cli_timestamp(value: Any) -> str:
    """Render timestamps compactly so important identity columns stay visible."""
    dt = _parse_iso_datetime(value)
    if dt is not None:
        return dt.strftime("%Y-%m-%d %H:%M")
    if hasattr(value, "strftime"):
        with suppress(Exception):
            return value.strftime("%Y-%m-%d %H:%M")
    if hasattr(value, "isoformat"):
        with suppress(Exception):
            return value.isoformat()
    return str(value or "")


def _add_message_sender_column(table: Table) -> None:
    """Keep sender addresses readable in narrow terminals."""
    table.add_column("from", overflow="fold", min_width=15)


def _add_message_timestamp_column(table: Table) -> None:
    """Use a compact timestamp column so sender addresses don't get truncated."""
    table.add_column("created_ts", overflow="fold", max_width=16)


def _new_compact_message_table(title: str) -> Table:
    """Render multi-column message tables cleanly in 80-column terminals."""
    return Table(title=title, show_lines=False, pad_edge=False, collapse_padding=True)


def _extract_jsonrpc_result(payload: Any, *, request_name: str) -> Any:
    """Unwrap FastMCP HTTP JSON-RPC responses or raise a CLI-facing server error."""
    if not isinstance(payload, dict):
        raise click.ClickException(f"{request_name}: invalid server response")
    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "server request failed")
        detail = error.get("data")
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail") or detail
        if detail not in (None, "", message):
            message = f"{message}: {detail}"
        raise click.ClickException(f"{request_name}: {message}")
    result = payload.get("result")
    if not isinstance(result, dict):
        return result
    structured_missing = object()
    structured = result.get("structuredContent", structured_missing)
    if structured is structured_missing:
        structured = result.get("structured_content", structured_missing)
    if structured is not structured_missing:
        if isinstance(structured, dict):
            return structured.get("result", structured)
        return structured
    return result


def _parse_jsonrpc_response(response: Any, *, request_name: str) -> Any:
    """Decode an HTTP JSON-RPC response with a CLI-facing parse error."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        status_suffix = f" HTTP {status_code}" if status_code is not None else " HTTP error"
        raise click.ClickException(f"{request_name}:{status_suffix} from server") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise click.ClickException(f"{request_name}: invalid JSON response from server") from exc
    return _extract_jsonrpc_result(payload, request_name=request_name)


async def _lookup_agent_registration_token(project_human_key: str, agent_name: str) -> str | None:
    """Resolve a locally stored registration token for a project/agent pair."""
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Agent.registration_token)
            .join(Project, cast(ColumnElement[bool], Agent.project_id == Project.id))
            .where(
                cast(ColumnElement[bool], Project.human_key == project_human_key),
                func.lower(Agent.name) == agent_name.lower(),
                cast(ColumnElement[bool], Agent.provisioning_state == "active"),
            )
        )
        token = result.scalar_one_or_none()
    if token is None:
        return None
    normalized_token = str(token).strip()
    return normalized_token or None


async def _lookup_product_registration_token(product_key: str, agent_name: str) -> str | None:
    """Resolve a unique locally stored registration token for a product/agent pair."""
    await ensure_schema()
    async with get_session() as session:
        product = (
            await session.execute(
                select(Product).where(
                    or_(
                        cast(ColumnElement[bool], Product.product_uid == product_key),
                        cast(ColumnElement[bool], Product.name == product_key),
                    )
                )
            )
        ).scalars().first()
        if product is None or product.id is None:
            return None
        token_rows = await session.execute(
            select(Agent.registration_token)
            .join(Project, cast(ColumnElement[bool], Agent.project_id == Project.id))
            .join(ProductProjectLink, cast(ColumnElement[bool], ProductProjectLink.project_id == Project.id))
            .where(
                cast(ColumnElement[bool], ProductProjectLink.product_id == product.id),
                func.lower(Agent.name) == agent_name.lower(),
                cast(ColumnElement[bool], Agent.provisioning_state == "active"),
            )
        )
        tokens = {
            str(token).strip()
            for token in token_rows.scalars().all()
            if str(token or "").strip()
        }
    if len(tokens) != 1:
        return None
    return next(iter(tokens))


async def _resolve_local_product_agents(
    product_key: str,
    agent_name: str,
    registration_token: str | None,
) -> tuple[Product, list[tuple[Project, Agent]], str | None]:
    """Resolve locally authorized product agents using the same token semantics as the server."""
    import hmac as _hmac

    await ensure_schema()
    async with get_session() as session:
        product = (
            await session.execute(
                select(Product).where(
                    or_(
                        cast(ColumnElement[bool], Product.product_uid == product_key),
                        cast(ColumnElement[bool], Product.name == product_key),
                    )
                )
            )
        ).scalars().first()
        if product is None:
            raise ValueError(f"Product '{product_key}' not found")
        assert product.id is not None
        rows = await session.execute(
            select(Project, Agent)
            .join(ProductProjectLink, cast(ColumnElement[bool], ProductProjectLink.project_id == Project.id))
            .join(Agent, cast(ColumnElement[bool], Agent.project_id == Project.id))
            .where(
                cast(ColumnElement[bool], ProductProjectLink.product_id == product.id),
                func.lower(Agent.name) == agent_name.lower(),
                cast(ColumnElement[bool], Agent.provisioning_state == "active"),
            )
        )
        project_agents = list(rows.all())

    effective_token = (registration_token or "").strip() or None
    if effective_token is None:
        unique_tokens = {
            str(agent.registration_token).strip()
            for _project, agent in project_agents
            if str(agent.registration_token or "").strip()
        }
        if len(unique_tokens) == 1:
            effective_token = next(iter(unique_tokens))

    authorized: list[tuple[Project, Agent]] = []
    if effective_token is not None:
        for project, agent in project_agents:
            stored_token = str(agent.registration_token or "").strip()
            if stored_token and _hmac.compare_digest(effective_token, stored_token):
                authorized.append((project, agent))

    return product, authorized, effective_token


def _require_cli_product_auth(command_name: str, product_key: str, agent_name: str, effective_token: str | None) -> str:
    """Require a concrete product auth token for server-first or local fallback reads."""
    if effective_token:
        return effective_token
    raise click.ClickException(
        f"{command_name} requires $AGENT_MAIL_REGISTRATION_TOKEN for agent '{agent_name}' "
        f"or a single unambiguous locally stored token linked to product '{product_key}'."
    )


def _ambient_registration_token() -> str | None:
    """Read a registration capability from process env without an argv flag."""
    value = DecoupleConfig(RepositoryEmpty())(
        "AGENT_MAIL_REGISTRATION_TOKEN",
        default=None,
    )
    normalized = str(value or "").strip()
    return normalized or None


def _run_async(coro: Any) -> Any:
    """Run an async coroutine and ensure database cleanup on exit.

    This wrapper ensures that aiosqlite background threads are properly
    terminated before the CLI exits. Without this, Python's shutdown
    sequence can hang waiting for orphaned threads.

    See: https://github.com/Dicklesworthstone/mcp_agent_mail/issues/68
    """
    try:
        return asyncio.run(coro)
    finally:
        reset_database_state()


app = typer.Typer(help="Developer utilities for Iris, the MCP Agent Mail service.", invoke_without_command=True)


@app.callback()
def _app_callback(ctx: typer.Context) -> None:
    """Default to ``serve-http`` when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        serve_http(host=None, port=None, path=None)

# ty currently struggles to type SQLModel-mapped SQLAlchemy expressions.
# Provide lightweight wrappers to keep type checking focused on our code.
def select(*entities: Any, **kwargs: Any) -> Any:
    return _sa_select(*entities, **kwargs)


def or_(*clauses: Any) -> Any:
    return _sa_or(*clauses)


def asc(value: Any) -> Any:
    return _sa_asc(value)


def desc(value: Any) -> Any:
    return _sa_desc(value)

def _parse_iso_datetime(value: Any) -> datetime | None:
    """Parse ISO-8601 with Z/offset support and normalize to UTC."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

_PREVIEW_FORCE_TOKEN = 0
_PREVIEW_FORCE_LOCK = threading.Lock()

guard_app = typer.Typer(help="Install or remove the Git pre-commit guard")
file_reservations_app = typer.Typer(help="Inspect advisory file_reservations")
acks_app = typer.Typer(help="Review acknowledgement status")
share_app = typer.Typer(help="Export MCP Agent Mail data for static sharing")
config_app = typer.Typer(help="Configure server settings")
archive_app = typer.Typer(help="Archive and restore local mailbox states (lossless disaster-recovery bundles)")

app.add_typer(guard_app, name="guard")
app.add_typer(file_reservations_app, name="file_reservations")
app.add_typer(acks_app, name="acks")
app.add_typer(share_app, name="share")
app.add_typer(config_app, name="config")
app.add_typer(archive_app, name="archive")
mail_app = typer.Typer(help="Mail diagnostics and routing status")
app.add_typer(mail_app, name="mail")
projects_app = typer.Typer(help="Project maintenance utilities")
app.add_typer(projects_app, name="projects")
amctl_app = typer.Typer(help="Build and environment helpers")
app.add_typer(amctl_app, name="amctl")
products_app = typer.Typer(help="Product Bus: manage products and links")
app.add_typer(products_app, name="products")
docs_app = typer.Typer(help="Documentation helpers for agent onboarding")
app.add_typer(docs_app, name="docs")
doctor_app = typer.Typer(help="Diagnose and repair mailbox health issues")
app.add_typer(doctor_app, name="doctor")
ui_users_app = typer.Typer(help="Manage human logins for the /mail web viewer")
app.add_typer(ui_users_app, name="ui-users")
# Read-only on purpose. Writing a ticket records an actor in an append-only audit row, and
# a CLI invocation has no authenticated agent identity to record -- it would land as `cli`
# with no provenance, and the contact-policy check that governs every notification lives in
# the MCP tool bodies rather than in the delivery helper, so a service-layer write would
# skip it silently. Both are fixable; neither is fixed, so there is nothing here that
# mutates.
tickets_app = typer.Typer(help="Inspect epics and tickets (read-only)")
app.add_typer(tickets_app, name="tickets")


async def _ui_users_find_user(session: Any, username: str) -> Any:
    """Load one human UI user in an existing transaction.

    Args:
        session: Open async database session.
        username: Exact login name to find.

    Returns:
        The matching ``UiUser`` row or ``None``.
    """
    from sqlmodel import select

    from .models import UiUser

    result = await session.execute(select(UiUser).where(UiUser.username == username))
    return result.scalars().first()


async def _ui_users_other_admin_count(
    session: Any,
    *,
    user_id: int,
    enabled_only: bool,
) -> int:
    """Count other global administrators in an existing transaction.

    Args:
        session: Open async database session.
        user_id: User excluded from the count.
        enabled_only: Whether disabled administrators should be excluded.

    Returns:
        The number of matching administrator rows.
    """
    from sqlmodel import select

    from .models import UiUser
    from .webauth import ROLE_ADMIN

    statement = select(UiUser).where(UiUser.role == ROLE_ADMIN).where(UiUser.id != user_id)
    if enabled_only:
        statement = statement.where(UiUser.disabled == False)  # noqa: E712
    result = await session.execute(statement)
    return len(result.scalars().all())


async def _ui_users_find_project(
    session: Any,
    identifier: str,
) -> tuple[Any | None, tuple[str, ...]]:
    """Resolve a project slug, canonical key, or repository path.

    Args:
        session: Open async database session.
        identifier: Project slug, human key, or repository path.

    Returns:
        The matching ``Project`` row and an empty ambiguity tuple. When no
        unique match exists, the row is ``None`` and the tuple contains every
        conflicting project slug.
    """
    from sqlmodel import select

    from .models import Project

    raw_identifier = identifier.strip()
    if not raw_identifier:
        return None, ()
    canonical_identifier = raw_identifier
    with suppress(Exception):
        canonical_identifier = await asyncio.to_thread(
            _canonicalize_project_identifier,
            raw_identifier,
        )
    exact_slug_result = await session.execute(
        select(Project).where(Project.slug == raw_identifier)
    )
    exact_slug = exact_slug_result.scalars().first()
    if exact_slug is not None:
        return exact_slug, ()

    human_key_conditions = [
        cast(ColumnElement[bool], Project.human_key == raw_identifier),
    ]
    if canonical_identifier != raw_identifier:
        human_key_conditions.append(
            cast(ColumnElement[bool], Project.human_key == canonical_identifier)
        )
    human_key_result = await session.execute(
        select(Project).where(_sa_or(*human_key_conditions))
    )
    human_key_rows_by_id = {
        int(row.id): row
        for row in human_key_result.scalars().all()
        if row.id is not None
    }
    if len(human_key_rows_by_id) == 1:
        return next(iter(human_key_rows_by_id.values())), ()
    if len(human_key_rows_by_id) > 1:
        return None, tuple(
            sorted(str(row.slug) for row in human_key_rows_by_id.values())
        )

    casefolded_slug_result = await session.execute(
        select(Project).where(func.lower(Project.slug) == raw_identifier.lower())
    )
    casefolded_slug_rows = list(casefolded_slug_result.scalars().all())
    if len(casefolded_slug_rows) == 1:
        return casefolded_slug_rows[0], ()
    if len(casefolded_slug_rows) > 1:
        return None, tuple(sorted(str(row.slug) for row in casefolded_slug_rows))

    canonical_slug = slugify(canonical_identifier)
    canonical_slug_result = await session.execute(
        select(Project).where(Project.slug == canonical_slug)
    )
    return canonical_slug_result.scalars().first(), ()


async def _save_cli_ui_user(username: str, password: str, role: str | None) -> tuple[str, str]:
    from .db import ensure_schema, get_immediate_session
    from .models import UiUser
    from .webauth import DEFAULT_NEW_ROLE, ROLE_ADMIN, hash_password, normalize_ui_user_role

    await ensure_schema()
    async with get_immediate_session() as session:
        result = await session.execute(select(UiUser).where(UiUser.username == username))
        existing = result.scalars().first()
        if existing is None:
            effective = role or DEFAULT_NEW_ROLE
            session.add(UiUser(username=username, password_hash=hash_password(password), role=effective))
            await session.commit()
            return "created", effective
        existing_role = normalize_ui_user_role(existing.role)
        if role is None and existing_role is None:
            return "invalid_global_role", existing.role
        effective = role or existing_role
        assert effective is not None
        if (
            existing.id is not None
            and existing_role == ROLE_ADMIN
            and effective != ROLE_ADMIN
            and await _ui_users_other_admin_count(session, user_id=existing.id, enabled_only=True) == 0
        ):
            return "last_admin", effective
        existing.password_hash = hash_password(password)
        existing.role = effective
        existing.session_epoch = existing.session_epoch + 1
        session.add(existing)
        await session.commit()
        return "updated", effective


@ui_users_app.command("add")
def ui_users_add(
    username: str = typer.Argument(..., help="Login name"),
    role: str = typer.Option(None, "--role", help="Global role: admin or member"),
) -> None:
    """Create a user, or reset an existing user's password.

    Resetting a password bumps ``session_epoch``, which immediately invalidates
    that user's existing browser sessions.
    """
    from .webauth import UI_USER_ROLES, valid_username

    if not valid_username(username):
        typer.secho("Invalid username (1-64 chars, no '|' or '/', no surrounding whitespace)", fg="red")
        raise typer.Exit(code=2)
    if role is not None and role not in UI_USER_ROLES:
        typer.secho(
            f"Invalid role {role!r}; use one of: {', '.join(UI_USER_ROLES)}",
            fg="red",
        )
        raise typer.Exit(code=2)

    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    if not password:
        typer.secho("Empty password", fg="red")
        raise typer.Exit(code=1)

    action, effective = _run_async(_save_cli_ui_user(username, password, role))
    if action == "last_admin":
        typer.secho(
            f"Refused: {username!r} is the last administrator account; changing its role "
            "would eliminate the admin recovery path.",
            fg="red",
        )
        raise typer.Exit(code=1)
    if action == "invalid_global_role":
        typer.secho(
            f"Refused: {username!r} has an invalid global role; pass --role admin or --role member to repair it.",
            fg="red",
        )
        raise typer.Exit(code=1)
    typer.secho(f"{action} user {username!r} with role {effective!r}", fg="green")
    if action == "updated":
        typer.echo(UI_SESSIONS_INVALIDATED_MESSAGE)


@ui_users_app.command("list")
def ui_users_list() -> None:
    """List human logins and their effective project-access summary."""
    from sqlmodel import select

    from .db import ensure_schema, get_session
    from .models import Project, UiProjectAssignment, UiUser
    from .webauth import (
        ROLE_ADMIN,
        ROLE_MEMBER,
        normalize_project_role,
        normalize_ui_user_role,
    )

    async def _list() -> tuple[list[Any], list[Any]]:
        await ensure_schema()
        async with get_session() as session:
            users_result = await session.execute(select(UiUser).order_by(UiUser.username))
            assignments_result = await session.execute(
                select(UiProjectAssignment, Project)
                .join(
                    Project,
                    cast(ColumnElement[bool], UiProjectAssignment.project_id == Project.id),
                )
                .order_by(
                    cast(Any, UiProjectAssignment.user_id),
                    cast(Any, Project.slug),
                )
            )
            return list(users_result.scalars().all()), list(assignments_result.all())

    rows, assignment_rows = _run_async(_list())
    if not rows:
        typer.echo("No human logins yet. Create one with: mcp-agent-mail ui-users add <name> --role admin")
        return
    assignments_by_user: dict[int, list[tuple[str, str]]] = {}
    for assignment, project in assignment_rows:
        normalized_project_role = normalize_project_role(assignment.role)
        role_label = normalized_project_role or f"invalid:{assignment.role}"
        assignments_by_user.setdefault(assignment.user_id, []).append((project.slug, role_label))
    for r in rows:
        state = "disabled" if r.disabled else "enabled"
        last = r.last_login_ts.isoformat(sep=" ", timespec="seconds") if r.last_login_ts else "never"
        normalized_role = normalize_ui_user_role(r.role)
        assignments = assignments_by_user.get(r.id, [])
        valid_assignment_count = sum(
            1 for _project_slug, assignment_role in assignments if not assignment_role.startswith("invalid:")
        )
        if normalized_role == ROLE_ADMIN:
            access = "all projects"
        elif normalized_role == ROLE_MEMBER:
            access = f"{valid_assignment_count} project(s)"
        else:
            access = "none (invalid global role)"
        role_label = normalized_role or f"invalid:{r.role}"
        typer.echo(
            f"{r.username:<24} {role_label:<14} {state:<9} access: {access}; last login: {last}"
        )


@ui_users_app.command("role")
def ui_users_role(
    username: str = typer.Argument(...),
    role: str = typer.Argument(..., help="admin or member"),
) -> None:
    """Change a user's role."""
    from .db import ensure_schema, get_immediate_session
    from .webauth import ROLE_ADMIN, UI_USER_ROLES, normalize_ui_user_role

    if role not in UI_USER_ROLES:
        typer.secho(
            f"Invalid role {role!r}; use one of: {', '.join(UI_USER_ROLES)}",
            fg="red",
        )
        raise typer.Exit(code=2)

    async def _set() -> str:
        await ensure_schema()
        async with get_immediate_session() as session:
            row = await _ui_users_find_user(session, username)
            if row is None:
                return "not_found"
            if normalize_ui_user_role(row.role) == role:
                return "unchanged"
            if (
                row.id is not None
                and normalize_ui_user_role(row.role) == ROLE_ADMIN
                and role != ROLE_ADMIN
                and await _ui_users_other_admin_count(
                    session,
                    user_id=row.id,
                    enabled_only=True,
                )
                == 0
            ):
                return "last_admin"
            row.role = role
            row.session_epoch = row.session_epoch + 1
            session.add(row)
            await session.commit()
        return "ok"

    outcome = _run_async(_set())
    if outcome == "not_found":
        typer.secho(f"No such user {username!r}", fg="red")
        raise typer.Exit(code=1)
    if outcome == "last_admin":
        typer.secho(
            f"Refused: {username!r} is the last administrator account; demoting it "
            "would eliminate the admin recovery path.",
            fg="red",
        )
        raise typer.Exit(code=1)
    if outcome == "unchanged":
        typer.echo(f"{username!r} already has role {role!r}; no sessions were invalidated.")
        return
    typer.secho(f"Set {username!r} role to {role!r}", fg="green")
    typer.echo(UI_SESSIONS_INVALIDATED_MESSAGE)


@ui_users_app.command("remove")
def ui_users_remove(
    username: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Delete a human login and its project assignments permanently."""
    from sqlmodel import select

    from .db import ensure_schema, get_immediate_session
    from .models import UiProjectAssignment
    from .webauth import ROLE_ADMIN, normalize_ui_user_role

    async def _remove() -> str:
        await ensure_schema()
        async with get_immediate_session() as session:
            row = await _ui_users_find_user(session, username)
            if row is None or row.id is None:
                return "not_found"
            if (
                normalize_ui_user_role(row.role) == ROLE_ADMIN
                and await _ui_users_other_admin_count(
                    session,
                    user_id=row.id,
                    enabled_only=True,
                )
                == 0
            ):
                return "last_admin"
            assignments_result = await session.execute(
                select(UiProjectAssignment).where(UiProjectAssignment.user_id == row.id)
            )
            for assignment in assignments_result.scalars().all():
                await session.delete(assignment)
            await session.delete(row)
            await session.commit()
        return "ok"

    if not yes and not typer.confirm(f"Permanently delete human login {username!r}?"):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)

    outcome = _run_async(_remove())
    if outcome == "not_found":
        typer.secho(f"No such user {username!r}", fg="red")
        raise typer.Exit(code=1)
    if outcome == "last_admin":
        typer.secho(
            f"Refused: {username!r} is the last administrator account; deleting it "
            "would eliminate the admin recovery path.",
            fg="red",
        )
        raise typer.Exit(code=1)
    typer.secho(f"Removed user {username!r}", fg="green")


@ui_users_app.command("disable")
def ui_users_disable(username: str = typer.Argument(...)) -> None:
    """Disable an account and immediately terminate its sessions."""
    _ui_users_set_disabled(username, True)


@ui_users_app.command("enable")
def ui_users_enable(username: str = typer.Argument(...)) -> None:
    """Re-enable a disabled account."""
    _ui_users_set_disabled(username, False)


def _ui_users_set_disabled(username: str, disabled: bool) -> None:
    from .db import ensure_schema, get_immediate_session
    from .webauth import ROLE_ADMIN, normalize_ui_user_role

    async def _set() -> str:
        await ensure_schema()
        async with get_immediate_session() as session:
            row = await _ui_users_find_user(session, username)
            if row is None or row.id is None:
                return "not_found"
            if row.disabled == disabled:
                return "unchanged"
            if (
                disabled
                and normalize_ui_user_role(row.role) == ROLE_ADMIN
                and not row.disabled
                and await _ui_users_other_admin_count(
                    session,
                    user_id=row.id,
                    enabled_only=True,
                )
                == 0
            ):
                return "last_admin"
            row.disabled = disabled
            row.session_epoch = row.session_epoch + 1
            session.add(row)
            await session.commit()
        return "ok"

    outcome = _run_async(_set())
    if outcome == "not_found":
        typer.secho(f"No such user {username!r}", fg="red")
        raise typer.Exit(code=1)
    if outcome == "last_admin":
        typer.secho(
            f"Refused: {username!r} is the only enabled admin (would lock everyone out).", fg="red"
        )
        raise typer.Exit(code=1)
    if outcome == "unchanged":
        typer.echo(f"User {username!r} is already {'disabled' if disabled else 'enabled'}.")
        return
    typer.secho(f"{'Disabled' if disabled else 'Enabled'} user {username!r}", fg="green")
    typer.echo(UI_SESSIONS_INVALIDATED_MESSAGE)


async def _change_cli_project_access(username: str, project: str, role: ProjectRole | None) -> tuple[str, str | None]:
    from .ui_access import mutate_ui_project_access
    from .webauth import ROLE_ADMIN, ROLE_MEMBER, normalize_ui_user_role

    await ensure_schema()
    async with get_session() as session:
        user = await _ui_users_find_user(session, username)
        if user is None or user.id is None:
            return "user_not_found", None
        global_role = normalize_ui_user_role(user.role)
        if global_role == ROLE_ADMIN:
            return "global_admin", None
        if global_role != ROLE_MEMBER:
            return "invalid_global_role", None
        project_row, ambiguous_projects = await _ui_users_find_project(session, project)
        if ambiguous_projects:
            return "project_ambiguous", ", ".join(ambiguous_projects)
        if project_row is None or project_row.id is None:
            return "project_not_found", None
        user_id = int(user.id)
        project_id = int(project_row.id)
        account_generation = str(user.session_generation)
        access_version = int(user.session_epoch)
        project_slug = str(project_row.slug)
        project_generation = str(project_row.project_generation)

    async with get_session() as session:
        result = await mutate_ui_project_access(
            session,
            actor_user_id=None,
            actor_account_generation=None,
            expected_actor_session_epoch=None,
            trusted_cli_actor=True,
            target_user_id=user_id,
            project_id=project_id,
            expected_project_generation=project_generation,
            role=role,
            expected_access_version=access_version,
            account_generation=account_generation,
        )
    return ("changed" if result.changed else "unchanged"), project_slug


def _cli_project_access_outcome(username: str, project: str, role: ProjectRole | None) -> tuple[str, str | None]:
    from .ui_access import UiAccessMutationError

    try:
        outcome, project_slug = _run_async(_change_cli_project_access(username, project, role))
    except UiAccessMutationError as exc:
        typer.secho(
            f"Refused: access state changed or is not eligible ({exc.code}). Retry after listing it.",
            fg="red",
        )
        raise typer.Exit(code=1) from exc
    admin_message = (
        f"Refused: {username!r} is an admin and already has global project access."
        if role is not None
        else f"Refused: {username!r} is an admin; project revocation cannot narrow global access."
    )
    messages = {
        "user_not_found": f"No such user {username!r}",
        "project_not_found": f"No such project {project!r}",
        "project_ambiguous": f"Ambiguous project {project!r}; matches slugs: {project_slug}. Use one exact slug.",
        "global_admin": admin_message,
        "invalid_global_role": f"Refused: {username!r} has an invalid global role; repair it with ui-users role.",
    }
    if outcome in messages:
        typer.secho(messages[outcome], fg="red")
        raise typer.Exit(code=1)
    return outcome, project_slug


@ui_users_app.command("grant")
def ui_users_grant(
    username: str = typer.Argument(..., help=HUMAN_LOGIN_NAME_HELP),
    project: str = typer.Argument(..., help="Project slug, human key, or repository path"),
    role: str = typer.Option("viewer", "--role", "-r", help="Project role: viewer or operator"),
) -> None:
    """Grant or replace one member's explicit project role."""
    from .webauth import PROJECT_ROLES, normalize_project_role

    normalized_role = normalize_project_role(role)
    if normalized_role is None:
        typer.secho(
            f"Invalid project role {role!r}; use one of: {', '.join(PROJECT_ROLES)}",
            fg="red",
        )
        raise typer.Exit(code=2)

    outcome, project_slug = _cli_project_access_outcome(username, project, normalized_role)
    if outcome == "unchanged":
        typer.echo(
            f"{username!r} already has {normalized_role!r} access to project {project_slug!r}; "
            "no sessions were invalidated."
        )
        return
    typer.secho(
        f"Granted {normalized_role!r} access to project {project_slug!r} for {username!r}.",
        fg="green",
    )
    typer.echo(UI_SESSIONS_INVALIDATED_MESSAGE)


@ui_users_app.command("revoke")
def ui_users_revoke(
    username: str = typer.Argument(..., help=HUMAN_LOGIN_NAME_HELP),
    project: str = typer.Argument(..., help="Project slug, human key, or repository path"),
) -> None:
    """Revoke one member's explicit access to a project."""
    outcome, project_slug = _cli_project_access_outcome(username, project, None)
    if outcome == "unchanged":
        typer.echo(
            f"{username!r} has no assignment for project {project_slug!r}; "
            "no sessions were invalidated."
        )
        return
    typer.secho(
        f"Revoked access to project {project_slug!r} from {username!r}.",
        fg="green",
    )
    typer.echo(UI_SESSIONS_INVALIDATED_MESSAGE)


@ui_users_app.command("access")
def ui_users_access(username: str = typer.Argument(..., help=HUMAN_LOGIN_NAME_HELP)) -> None:
    """Show one human login's effective project access."""
    from sqlmodel import select

    from .db import ensure_schema, get_session
    from .models import Project, UiProjectAssignment
    from .webauth import ROLE_ADMIN, ROLE_MEMBER, normalize_project_role, normalize_ui_user_role

    async def _access() -> tuple[Any, list[Any]]:
        await ensure_schema()
        async with get_session() as session:
            user = await _ui_users_find_user(session, username)
            if user is None or user.id is None:
                return None, []
            result = await session.execute(
                select(UiProjectAssignment, Project)
                .join(
                    Project,
                    cast(ColumnElement[bool], UiProjectAssignment.project_id == Project.id),
                )
                .where(UiProjectAssignment.user_id == user.id)
                .order_by(cast(Any, Project.slug))
            )
            return user, list(result.all())

    user, assignments = _run_async(_access())
    if user is None:
        typer.secho(f"No such user {username!r}", fg="red")
        raise typer.Exit(code=1)
    state = "disabled" if user.disabled else "enabled"
    global_role = normalize_ui_user_role(user.role)
    if global_role == ROLE_ADMIN:
        typer.echo(f"{username} admin {state}: all projects (global)")
        return
    if global_role != ROLE_MEMBER:
        typer.secho(
            f"{username} invalid:{user.role} {state}: no access (invalid global role)",
            fg="red",
        )
        raise typer.Exit(code=1)
    typer.echo(f"{username} member {state}")
    valid_assignments = 0
    for assignment, project_row in assignments:
        project_role = normalize_project_role(assignment.role)
        if project_role is None:
            typer.echo(f"  {project_row.slug:<32} invalid:{assignment.role} (denied)")
            continue
        valid_assignments += 1
        typer.echo(f"  {project_row.slug:<32} {project_role}")
    if valid_assignments == 0:
        typer.echo("  no project access")


def _canonical_project_path(path: Path) -> Path:
    return Path(_canonicalize_project_identifier(str(path)))


def _resolve_repo_worktree_root(path: Path) -> Path:
    repo = None
    try:
        from git import Repo as _Repo

        repo = _Repo(str(path), search_parent_directories=True)
        return Path(repo.working_tree_dir or str(path))
    except Exception:
        return path
    finally:
        if repo is not None:
            with suppress(Exception):
                repo.close()


async def _get_project_record(identifier: str) -> Project:
    raw_identifier = identifier.strip()
    canonical_identifier = await asyncio.to_thread(_canonicalize_project_identifier, raw_identifier)
    slug = slugify(canonical_identifier)
    await ensure_schema()
    async with get_session() as session:
        stmt = select(Project).where(
            or_(
                cast(ColumnElement[bool], Project.slug == slug),
                cast(ColumnElement[bool], Project.human_key == canonical_identifier),
                cast(ColumnElement[bool], Project.human_key == raw_identifier),
            )
        )
        result = await session.execute(stmt)
        project = result.scalars().first()
        if not project:
            raise ValueError(f"Project '{raw_identifier}' not found")
        return project


async def _get_agent_record(project: Project, agent_name: str) -> Agent:
    if project.id is None:
        raise ValueError("Project must have an id before querying agents")
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(
                and_(
                    cast(ColumnElement[bool], Agent.project_id == project.id),
                    func.lower(Agent.name) == agent_name.lower(),
                    cast(ColumnElement[bool], Agent.provisioning_state == "active"),
                )
            )
        )
        agent = result.scalars().first()
        if not agent:
            raise ValueError(f"Agent '{agent_name}' not registered for project '{project.human_key}'")
        return agent


def _iso(dt: Optional[datetime]) -> str:
    """Return ISO-8601 in UTC from datetime.

    Naive datetimes (from SQLite) are assumed to be UTC already.
    """
    if dt is None:
        return ""
    # Handle naive datetimes from SQLite (assume UTC)
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _ensure_utc_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime is timezone-aware UTC.

    Naive datetimes (from SQLite) are assumed to be UTC already.
    """
    if dt is None:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _delete_project_archive_tree(storage_root: str, project_slug: str) -> tuple[int, int, list[str]]:
    """Best-effort removal of a project's archive subtree."""
    files_removed = 0
    dirs_removed = 0
    fs_errors: list[str] = []
    try:
        archive_root = Path(storage_root).expanduser().resolve()
        project_dir = archive_root / "projects" / project_slug
        if project_dir.exists():
            for item in project_dir.rglob("*"):
                if item.is_file():
                    files_removed += 1
                elif item.is_dir():
                    dirs_removed += 1
            shutil.rmtree(project_dir)
    except Exception as exc:
        fs_errors.append(str(exc))
    return files_removed, dirs_removed, fs_errors


def _call_cli_product_tool(tool_name: str, request_name: str, arguments: dict[str, Any], *, timeout: float = 5.0) -> Any:
    settings = get_settings()
    server_url = f"http://{settings.http.host}:{settings.http.port}{settings.http.path}"
    headers = {}
    if settings.http.bearer_token:
        headers["Authorization"] = f"Bearer {settings.http.bearer_token}"
    request = {
        "jsonrpc": "2.0",
        "id": "cli-" + request_name.replace(" ", "-"),
        "method": TOOLS_CALL_METHOD,
        "params": {"name": tool_name, "arguments": arguments},
    }
    with httpx.Client(timeout=timeout) as client:
        response = client.post(server_url, json=request, headers=headers)
        return _parse_jsonrpc_response(response, request_name=request_name)


async def _ensure_local_product(key: str, product_key: str | None, name: str | None) -> dict[str, Any]:
    await ensure_schema()
    async with get_session() as session:
        existing = await session.execute(
            select(Product).where(or_(cast(ColumnElement[bool], Product.product_uid == key), cast(ColumnElement[bool], Product.name == key)))
        )
        prod = existing.scalars().first()
        if prod:
            return {"id": prod.id, "product_uid": prod.product_uid, "name": prod.name, "created_at": prod.created_at}
        if product_key and re.fullmatch(r"[A-Fa-f0-9]{8,64}", product_key.strip()):
            uid = product_key.strip().lower()
        else:
            uid = uuid.uuid4().hex[:20]
        display_name = (name or key).strip()
        display_name = " ".join(display_name.split())[:255] or uid
        prod = Product(product_uid=uid, name=display_name)
        session.add(prod)
        await session.commit()
        await session.refresh(prod)
        return {"id": prod.id, "product_uid": prod.product_uid, "name": prod.name, "created_at": prod.created_at}


@products_app.command("ensure")
def products_ensure(
    product_key: Annotated[Optional[str], typer.Argument(help="Product uid or name")] = None,
    name: Annotated[Optional[str], typer.Option("--name", "-n", help="Product display name")] = None,
) -> None:
    """
    Ensure a product exists (creates if missing) and print its identifiers.
    """
    key = (product_key or name or "").strip()
    if not key:
        raise typer.BadParameter("Provide a product_key or --name.")
    # Prefer server tool to ensure consistent uid policy
    resp_data: dict[str, Any] = {}
    try:
        arguments: dict[str, Any] = {}
        if product_key:
            arguments["product_key"] = product_key
        if name:
            arguments["name"] = name
        resp_data = _call_cli_product_tool("ensure_product", "products ensure", arguments) or {}
    except httpx.TransportError:
        resp_data = {}
    if not resp_data:
        # Fallback to local DB with the same strict uid policy
        resp_data = _run_async(_ensure_local_product(key, product_key, name))
    table = Table(title="Product", show_lines=False)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("id", str(resp_data.get("id", "")))
    table.add_row("product_uid", str(resp_data.get("product_uid", "")))
    table.add_row("name", str(resp_data.get("name", "")))
    _created = resp_data.get("created_at", "")
    try:
        if hasattr(_created, "isoformat"):
            _created = _created.isoformat()
    except Exception:
        pass
    table.add_row("created_at", str(_created))
    console.print(table)


@products_app.command("link")
def products_link(
    product_key: Annotated[str, typer.Argument(..., help="Product uid or name")],
    project: Annotated[str, typer.Argument(..., help="Project slug or path")],
) -> None:
    """
    Link a project into a product (idempotent).
    """
    async def _link() -> dict:
        await ensure_schema()
        prod = await _get_product_record(product_key.strip())
        proj = await _get_project_record(project)
        async with get_session() as session:
            existing = await session.execute(
                select(ProductProjectLink).where(
                    and_(cast(ColumnElement[bool], ProductProjectLink.product_id == prod.id), cast(ColumnElement[bool], ProductProjectLink.project_id == proj.id))
                )
            )
            link = existing.scalars().first()
            if link is None:
                assert prod.id is not None
                assert proj.id is not None
                link = ProductProjectLink(product_id=int(prod.id), project_id=int(proj.id))
                session.add(link)
                await session.commit()
                await session.refresh(link)
        return {"product_uid": prod.product_uid, "product_name": prod.name, "project_slug": proj.slug}
    res = _run_async(_link())
    console.print(f"[green]Linked[/] project '{res['project_slug']}' into product '{res['product_name']}' ({res['product_uid']}).")


@products_app.command("status")
def products_status(
    product_key: Annotated[str, typer.Argument(..., help="Product uid or name")],
) -> None:
    """
    Show product metadata and linked projects.
    """
    async def _status() -> tuple[Product, list[Project]]:
        await ensure_schema()
        async with get_session() as session:
            stmt_prod = select(Product).where(or_(cast(ColumnElement[bool], Product.product_uid == product_key), cast(ColumnElement[bool], Product.name == product_key)))
            prod = (await session.execute(stmt_prod)).scalars().first()
            if prod is None:
                raise typer.BadParameter(f"Product '{product_key}' not found.")
            assert prod.id is not None
            rows = await session.execute(
                select(Project).join(ProductProjectLink, cast(ColumnElement[bool], ProductProjectLink.project_id == Project.id)).where(
                    cast(ColumnElement[bool], ProductProjectLink.product_id == prod.id)
                )
            )
            projects = list(rows.scalars().all())
            return prod, projects
    prod, projects = _run_async(_status())
    table = Table(title=f"Product: {prod.name}", show_lines=False)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("id", str(prod.id))
    table.add_row("product_uid", prod.product_uid)
    table.add_row("name", prod.name)
    table.add_row("created_at", _iso(prod.created_at))
    console.print(table)
    pt = Table(title="Linked Projects", show_lines=False)
    pt.add_column("id")
    pt.add_column("slug")
    pt.add_column("human_key")
    for p in projects:
        pt.add_row(str(p.id), p.slug, p.human_key)
    console.print(pt)


async def _product_like_search(session: Any, proj_ids: list[int], query: str, limit: int) -> list[dict[str, Any]]:
    fallback_terms = _extract_like_terms(query)
    if not fallback_terms:
        return []
    clauses: list[str] = []
    params: dict[str, Any] = {"proj_ids": proj_ids, "limit": limit}
    for idx, term in enumerate(fallback_terms):
        key = f"t{idx}"
        params[key] = f"%{_like_escape(term)}%"
        clauses.append(
            f"(m.subject LIKE :{key} ESCAPE '{_LIKE_ESCAPE_CHAR}' OR m.body_md LIKE :{key} ESCAPE '{_LIKE_ESCAPE_CHAR}')"
        )
    where_clause = " AND ".join(clauses)
    result = await session.execute(
        text(
            f"""
            SELECT m.id, m.subject, m.body_md, m.importance, m.ack_required, m.created_ts,
                   m.sender_id, m.thread_id, m.project_id,
                   a.name AS sender_name, a.project_id AS sender_project_id,
                   sp.slug AS sender_project_slug
            FROM messages m
            JOIN agents a ON m.sender_id = a.id
            LEFT JOIN projects sp ON sp.id = a.project_id
            WHERE m.project_id IN :proj_ids AND {where_clause}
            ORDER BY m.created_ts DESC
            LIMIT :limit
            """
        ).bindparams(bindparam("proj_ids", expanding=True)),
        params,
    )
    return [dict(row) for row in result.mappings().all()]


async def _visible_product_search_rows(
    session: Any, rows: list[dict[str, Any]], authorized_map: dict[int, int],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    message_ids = [int(row["id"]) for row in rows]
    recipient_rows = await session.execute(
        select(MessageRecipient.message_id, MessageRecipient.agent_id).where(
            cast(Any, MessageRecipient.message_id).in_(message_ids)
        )
    )
    recipients_by_message: dict[int, set[int]] = {}
    for message_id, recipient_agent_id in recipient_rows.all():
        recipients_by_message.setdefault(int(message_id), set()).add(int(recipient_agent_id))
    visible_rows = []
    for row in rows:
        project_agent_id = authorized_map.get(int(row["project_id"]))
        if project_agent_id is None:
            continue
        if int(row["sender_id"]) == project_agent_id or project_agent_id in recipients_by_message.get(int(row["id"]), set()):
            row["sender_display"] = _cli_sender_display(
                message_project_id=row.get("project_id"),
                sender_name=row.get("sender_name"),
                sender_project_id=row.get("sender_project_id"),
                sender_project_slug=row.get("sender_project_slug"),
            )
            visible_rows.append(row)
    return visible_rows


async def _search_product_locally(
    product_key: str, agent_name: str, effective_token: str, query: str, sanitized_query: str, limit: int,
) -> list[dict[str, Any]]:
    await ensure_schema()
    product, authorized, _ = await _resolve_local_product_agents(product_key, agent_name, effective_token)
    proj_ids = [project.id for project, _agent in authorized if project.id is not None]
    if product.id is None:
        raise typer.BadParameter(f"Product '{product_key}' not found.")
    authorized_map = {
        int(project.id): int(agent.id)
        for project, agent in authorized
        if project.id is not None and agent.id is not None
    }
    if not authorized_map:
        raise click.ClickException(
            f"products search: invalid registration token for agent '{agent_name}' on product '{product_key}'."
        )
    async with get_session() as session:
        if not proj_ids:
            return []
        try:
            result = await session.execute(
                text(
                    """
                    SELECT m.id, m.subject, m.body_md, m.importance, m.ack_required, m.created_ts,
                           m.sender_id, m.thread_id, m.project_id,
                           a.name AS sender_name, a.project_id AS sender_project_id,
                           sp.slug AS sender_project_slug
                    FROM fts_messages
                    JOIN messages m ON fts_messages.rowid = m.id
                    JOIN agents a ON m.sender_id = a.id
                    LEFT JOIN projects sp ON sp.id = a.project_id
                    WHERE m.project_id IN :proj_ids AND fts_messages MATCH :query
                    ORDER BY bm25(fts_messages) ASC
                    LIMIT :limit
                    """
                ).bindparams(bindparam("proj_ids", expanding=True)),
                {"proj_ids": proj_ids, "query": sanitized_query, "limit": limit},
            )
            rows = [dict(row) for row in result.mappings().all()]
        except Exception:
            rows = await _product_like_search(session, proj_ids, query, limit)
        return await _visible_product_search_rows(session, rows, authorized_map)


@products_app.command("search")
def products_search(
    product_key: Annotated[str, typer.Argument(..., help="Product uid or name")],
    query: Annotated[str, typer.Argument(..., help="FTS query")],
    agent: Annotated[
        Optional[str],
        typer.Option("--agent", "-a", envvar="AGENT_NAME", help="Agent name (defaults to $AGENT_NAME)"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Max results",)] = 20,
) -> None:
    """
    Full-text search over messages for all projects linked to a product.
    """
    # Sanitize query before executing
    sanitized_query = _sanitize_fts_query(query)
    if sanitized_query is None:
        console.print(f"[yellow]Query '{query}' cannot produce search results.[/]")
        return
    agent_name = (agent or "").strip()
    if not agent_name:
        raise click.ClickException("products search requires --agent or $AGENT_NAME.")
    effective_token = _require_cli_product_auth(
        "products search",
        product_key,
        agent_name,
        _ambient_registration_token()
        or _run_async(_lookup_product_registration_token(product_key, agent_name)),
    )

    rows: list[dict[str, Any]] | None = None
    try:
        result = _call_cli_product_tool("search_messages_product", "products search", {
            "product_key": product_key,
            "query": query,
            "limit": int(limit),
            "agent_name": agent_name,
            "registration_token": effective_token,
        })
        rows = result if isinstance(result, list) else []
    except httpx.TransportError:
        rows = None

    if rows is None:
        rows = _run_async(_search_product_locally(product_key, agent_name, effective_token, query, sanitized_query, limit))
    if not rows:
        console.print("[yellow]No results.[/]")
        return
    t = _new_compact_message_table(f"Product search: '{query}'")
    t.add_column("project_id")
    t.add_column("id")
    t.add_column("subject")
    _add_message_sender_column(t)
    _add_message_timestamp_column(t)
    for r in rows:
        t.add_row(
            str(r["project_id"]),
            str(r["id"]),
            r["subject"],
            str(r.get("sender_display") or r.get("sender_name") or "Unknown"),
            _format_cli_timestamp(r.get("created_ts")),
        )
    console.print(t)


async def _read_project_product_inbox(
    session: Any, proj: Project, agent: str, authorized_agent_ids: set[int], *,
    limit: int, urgent_only: bool, include_bodies: bool, since_ts: str | None,
) -> list[dict[str, Any]]:
    from sqlalchemy.orm import aliased

    assert proj.id is not None
    agent_row = (
        await session.execute(
            select(Agent).where(
                and_(
                    cast(ColumnElement[bool], Agent.project_id == proj.id),
                    func.lower(Agent.name) == agent.lower(),
                    cast(ColumnElement[bool], Agent.provisioning_state == "active"),
                )
            )
        )
    ).scalars().first()
    if not agent_row:
        return []
    assert agent_row.id is not None
    if int(agent_row.id) not in authorized_agent_ids:
        return []
    sender_alias = aliased(Agent)
    sender_project_alias = aliased(Project)
    stmt = (
        select(Message, MessageRecipient.kind, sender_alias.name, sender_alias.project_id, sender_project_alias.slug)
        .join(MessageRecipient, cast(ColumnElement[bool], MessageRecipient.message_id == Message.id))
        .join(sender_alias, cast(ColumnElement[bool], Message.sender_id == sender_alias.id))
        .outerjoin(sender_project_alias, cast(ColumnElement[bool], sender_alias.project_id == sender_project_alias.id))
        .where(and_(cast(ColumnElement[bool], Message.project_id == proj.id), cast(ColumnElement[bool], MessageRecipient.agent_id == agent_row.id)))
        .order_by(desc(cast(Any, Message.created_ts)))
        .limit(limit)
    )
    if urgent_only:
        stmt = stmt.where(cast(Any, Message.importance).in_(["high", "urgent"]))
    if since_ts:
        parsed = _parse_iso_datetime(since_ts)
        if parsed is not None:
            stmt = stmt.where(Message.created_ts > parsed.replace(tzinfo=None))
    result = await session.execute(stmt)
    items = []
    for msg, kind, sender_name, sender_project_id, sender_project_slug in result.all():
        payload = {
            "id": msg.id,
            "project_id": proj.id,
            "subject": msg.subject,
            "importance": msg.importance,
            "ack_required": msg.ack_required,
            "created_ts": msg.created_ts,
            "from": _cli_sender_display(
                message_project_id=proj.id,
                sender_name=sender_name,
                sender_project_id=sender_project_id,
                sender_project_slug=sender_project_slug,
            ),
            "kind": kind,
        }
        if include_bodies:
            payload["body_md"] = msg.body_md
        items.append(payload)
    return items


async def _read_product_inbox_locally(
    product_key: str, agent: str, effective_token: str, *,
    limit: int, urgent_only: bool, include_bodies: bool, since_ts: str | None,
) -> list[dict[str, Any]]:
    await ensure_schema()
    _product, authorized, _ = await _resolve_local_product_agents(product_key, agent, effective_token)
    authorized_agent_ids = {
        int(agent_record.id) for _project, agent_record in authorized if agent_record.id is not None
    }
    if not authorized_agent_ids:
        raise click.ClickException(
            f"products inbox: invalid registration token for agent '{agent}' on product '{product_key}'."
        )
    async with get_session() as session:
        prod = (await session.execute(select(Product).where(or_(cast(ColumnElement[bool], Product.product_uid == product_key), cast(ColumnElement[bool], Product.name == product_key))))).scalars().first()
        if prod is None:
            return []
        assert prod.id is not None
        proj_rows = await session.execute(
            select(Project).join(ProductProjectLink, cast(ColumnElement[bool], ProductProjectLink.project_id == Project.id)).where(
                cast(ColumnElement[bool], ProductProjectLink.product_id == prod.id)
            )
        )
        items = []
        for proj in proj_rows.scalars().all():
            items.extend(await _read_project_product_inbox(
                session, proj, agent, authorized_agent_ids,
                limit=limit, urgent_only=urgent_only, include_bodies=include_bodies, since_ts=since_ts,
            ))
        items.sort(key=lambda row: row.get("created_ts") or 0, reverse=True)
        return items[: max(0, int(limit))]


@products_app.command("inbox")
def products_inbox(
    product_key: Annotated[str, typer.Argument(..., help="Product uid or name")],
    agent: Annotated[str, typer.Argument(..., help=AGENT_NAME_HELP)],
    limit: Annotated[int, typer.Option("--limit", "-l", help="Max messages",)] = 20,
    urgent_only: Annotated[bool, typer.Option("--urgent-only/--all", help="Only high/urgent")] = False,
    include_bodies: Annotated[bool, typer.Option("--include-bodies/--no-bodies", help="Include body_md")] = False,
    since_ts: Annotated[Optional[str], typer.Option("--since-ts", help="ISO-8601 timestamp filter")] = None,
) -> None:
    """
    Fetch recent inbox messages for an agent across all projects in a product.
    Prefers server tool; falls back to local DB when server is not reachable.
    """
    effective_token = _require_cli_product_auth(
        "products inbox",
        product_key,
        agent,
        _ambient_registration_token()
        or _run_async(_lookup_product_registration_token(product_key, agent)),
    )
    # Try server first
    rows: list[dict[str, Any]] | None = None
    try:
        result = _call_cli_product_tool("fetch_inbox_product", "products inbox", {
            "product_key": product_key,
            "agent_name": agent,
            "limit": int(limit),
            "urgent_only": bool(urgent_only),
            "include_bodies": bool(include_bodies),
            "since_ts": since_ts or "",
            "registration_token": effective_token,
        })
        rows = result if isinstance(result, list) else []
    except httpx.TransportError:
        rows = None
    if rows is None:
        # Fallback: local DB
        rows = _run_async(_read_product_inbox_locally(
            product_key, agent, effective_token,
            limit=limit, urgent_only=urgent_only, include_bodies=include_bodies, since_ts=since_ts,
        ))
    if not rows:
        console.print("[yellow]No messages found.[/]")
        return
    t = _new_compact_message_table(f"Inbox for {agent} in product '{product_key}'")
    t.add_column("project_id")
    t.add_column("id")
    t.add_column("subject")
    _add_message_sender_column(t)
    t.add_column("importance")
    _add_message_timestamp_column(t)
    for r in rows:
        t.add_row(
            str(r.get("project_id", "")),
            str(r.get("id", "")),
            str(r.get("subject", "")),
            str(r.get("from", "")),
            str(r.get("importance", "")),
            _format_cli_timestamp(r.get("created_ts")),
        )
    console.print(t)


@products_app.command("summarize-thread")
def products_summarize_thread(
    product_key: Annotated[str, typer.Argument(..., help="Product uid or name")],
    thread_id: Annotated[str, typer.Argument(..., help="Thread id or key")],
    agent: Annotated[
        Optional[str],
        typer.Option("--agent", "-a", envvar="AGENT_NAME", help="Agent name (defaults to $AGENT_NAME)"),
    ] = None,
    per_thread_limit: Annotated[int, typer.Option("--per-thread-limit", "-n", help="Max messages per thread",)] = 50,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Disable LLM refinement")] = False,
) -> None:
    """
    Summarize a thread across all projects in a product. Prefers server tool; minimal fallback if server is unavailable.
    """
    agent_name = (agent or "").strip()
    if not agent_name:
        raise click.ClickException("products summarize-thread requires --agent or $AGENT_NAME.")
    effective_token = _require_cli_product_auth(
        "products summarize-thread",
        product_key,
        agent_name,
        _ambient_registration_token()
        or _run_async(_lookup_product_registration_token(product_key, agent_name)),
    )
    # Try server
    try:
        result = _call_cli_product_tool("summarize_thread_product", "products summarize-thread", {
            "product_key": product_key,
            "thread_id": thread_id,
            "include_examples": True,
            "llm_mode": (not no_llm),
            "per_thread_limit": int(per_thread_limit),
            "agent_name": agent_name,
            "registration_token": effective_token,
        }, timeout=8.0) or {}
    except httpx.TransportError:
        result = {}
    if not result:
        console.print("[yellow]Server unavailable; summarization requires server tool. Try again when server is running.[/]")
        raise typer.Exit(code=2)
    _print_product_thread_summary(result, thread_id)


def _print_product_thread_summary(result: dict[str, Any], thread_id: str) -> None:
    summary = result.get("summary") or {}
    examples = result.get("examples") or []
    table = Table(title=f"Thread summary: {thread_id}", show_lines=False)
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("participants", ", ".join(summary.get("participants", [])))
    table.add_row("total_messages", str(summary.get("total_messages", "")))
    table.add_row("open_actions", str(summary.get("open_actions", "")))
    table.add_row("done_actions", str(summary.get("done_actions", "")))
    console.print(table)
    if summary.get("key_points"):
        kp = Table(title="Key Points", show_lines=False)
        kp.add_column("point")
        for p in summary["key_points"]:
            kp.add_row(str(p))
        console.print(kp)
    if summary.get("action_items"):
        act = Table(title="Action Items", show_lines=False)
        act.add_column("item")
        for a in summary["action_items"]:
            act.add_row(str(a))
        console.print(act)
    if examples:
        ex = _new_compact_message_table("Examples")
        ex.add_column("id")
        ex.add_column("subject")
        _add_message_sender_column(ex)
        _add_message_timestamp_column(ex)
        for e in examples:
            ex.add_row(
                str(e.get("id", "")),
                str(e.get("subject", "")),
                str(e.get("from", "")),
                _format_cli_timestamp(e.get("created_ts")),
            )
        console.print(ex)


async def _get_product_record(key: str) -> Product:
    """Fetch Product by uid or name."""
    await ensure_schema()
    async with get_session() as session:
        stmt = select(Product).where(or_(cast(ColumnElement[bool], Product.product_uid == key), cast(ColumnElement[bool], Product.name == key)))
        result = await session.execute(stmt)
        prod = result.scalars().first()
        if not prod:
            raise ValueError(f"Product '{key}' not found")
        return prod


_SERVER_LOCK_FILENAME = "server.lock"


def _acquire_server_lock(settings: Any = None) -> BaseFileLock:
    """Acquire an exclusive lock on server.lock inside the resolved STORAGE_ROOT.

    Ensures only one Agent Mail server process can own a given storage root at a
    time.  Uses OS-level file locking (flock/fcntl on Unix, LockFileEx on
    Windows) so the lock is automatically released if the process crashes —
    unlike SoftFileLock which leaves a stale marker file on disk.

    The PID is written into a companion .pid file for diagnostic purposes.

    Returns the held ``FileLock`` so the caller can keep a reference alive
    (preventing GC from closing the file descriptor and releasing the lock).
    Raises ``SystemExit(1)`` if another server already holds the lock.
    """
    if settings is None:
        settings = get_settings()
    storage_root = Path(settings.storage.root).expanduser().resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / _SERVER_LOCK_FILENAME
    lock = FileLock(str(lock_path))
    try:
        lock.acquire(timeout=0)
    except LockTimeout as exc:
        # Try to read the PID from the companion .pid file for a helpful message
        owner_pid = "(unknown)"
        pid_path = storage_root / "server.pid"
        with suppress(OSError):
            owner_pid = pid_path.read_text(encoding="utf-8").strip() or "(unknown)"
        print(
            f"ERROR: Another Agent Mail server is already running for this "
            f"storage root (PID: {owner_pid}). Only one server can own a "
            f"storage root at a time.\n"
            f"  Storage root: {storage_root}\n"
            f"  Lock file:    {lock_path}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    # Write our PID to a companion file for diagnostics (not the lock file
    # itself, which is managed by the OS-level locking mechanism)
    try:
        pid_path = storage_root / "server.pid"
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass  # Non-fatal; the lock itself is what matters
    return lock


@app.command("serve-http")
def serve_http(
    host: Optional[str] = typer.Option(None, help="Host interface for HTTP transport. Defaults to HTTP_HOST setting."),
    port: Optional[int] = typer.Option(None, help="Port for HTTP transport. Defaults to HTTP_PORT setting."),
    path: Optional[str] = typer.Option(None, help="HTTP path where the MCP endpoint is exposed."),
) -> None:
    """Run the MCP server over the Streamable HTTP transport."""
    settings = get_settings()

    # Enforce single-server ownership of the storage root (issue #123)
    server_lock = _acquire_server_lock(settings)
    try:
        resolved_host = host or settings.http.host
        resolved_port = port or settings.http.port
        resolved_path = path or settings.http.path
        effective_settings = replace(
            settings,
            http=replace(settings.http, host=resolved_host, port=resolved_port, path=resolved_path),
        )

        # Display awesome startup banner with database stats
        from . import rich_logger
        rich_logger.display_startup_banner(effective_settings, resolved_host, resolved_port, resolved_path)

        # Reset database state after startup banner to prevent connection leak.
        # The banner's _get_database_stats() uses _run_async() which creates connections
        # on a temporary event loop. When uvicorn starts with its own loop, those
        # connections become orphaned and cause SQLAlchemy GC warnings. Resetting
        # here ensures fresh connections are created on the main event loop.
        reset_database_state()

        server = build_mcp_server()
        app = build_http_app(effective_settings, server)
        # Disable WebSockets: HTTP-only MCP transport. Stay compatible with tests that
        # monkeypatch uvicorn.run without the 'ws' parameter.
        import inspect as _inspect
        _sig = _inspect.signature(uvicorn.run)
        # Uvicorn's access logger includes the raw query string. OAuth callbacks
        # carry short-lived codes and state in that query, so rely on the app's
        # query-redacting request logger instead of emitting an unsafe duplicate.
        _kwargs: dict[str, Any] = {
            "host": resolved_host,
            "port": resolved_port,
            "log_level": "info",
            "access_log": False,
        }
        if "ws" in _sig.parameters:
            _kwargs["ws"] = "none"
        if "forwarded_allow_ips" in _sig.parameters:
            _kwargs["forwarded_allow_ips"] = effective_settings.http.forwarded_allow_ips
        uvicorn.run(app, **_kwargs)
    finally:
        server_lock.release()


@app.command("serve-stdio")
def serve_stdio() -> None:
    """Run the MCP server over stdio transport for CLI integration.

    This transport communicates via stdin/stdout, making it suitable for
    integrations where the host process (e.g., Claude Code) spawns and manages
    the MCP server directly. This enables project-local installation patterns
    without requiring a separate HTTP server.

    Note: All logging is redirected to stderr to avoid corrupting the stdio protocol.
    Tool debug panels are automatically disabled in stdio mode.
    """
    import logging

    # Disable tool debug logging and rich console output - they output to stdout
    # and would corrupt the stdio protocol
    os.environ["TOOLS_LOG_ENABLED"] = "false"
    os.environ["LOG_RICH_ENABLED"] = "false"
    clear_settings_cache()

    root_logger = logging.getLogger()
    previous_handlers = tuple(root_logger.handlers)
    previous_level = root_logger.level

    # Enforce single-server ownership of the storage root (issue #123)
    server_lock = _acquire_server_lock()
    try:
        # Redirect all logging to stderr to avoid corrupting stdio transport
        for handler in tuple(root_logger.handlers):
            root_logger.removeHandler(handler)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            stream=sys.stderr,
        )

        # Print startup message to stderr (stdout is reserved for MCP protocol)
        print("Iris / MCP Agent Mail - Starting stdio transport...", file=sys.stderr)

        server = build_mcp_server()
        server.run(transport="stdio")
    finally:
        # `serve-stdio` normally owns the process for its whole lifetime, but
        # embedded callers and test runners can return from `server.run()`.
        # Restore their logging graph instead of leaving a StreamHandler bound
        # to a capture stream that may already be closed.
        for handler in tuple(root_logger.handlers):
            root_logger.removeHandler(handler)
            if handler not in previous_handlers:
                handler.close()
        for handler in previous_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(previous_level)
        server_lock.release()


def _run_command(command: list[str]) -> None:
    console.print(f"[cyan]$ {' '.join(command)}[/]")
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)


def _identity_migration_server_lock(settings: Any, *, apply: bool) -> BaseFileLock | None:
    """Hold the server lock for an offline migration or report a dry-run warning."""
    storage_root = Path(settings.storage.root).expanduser().resolve()
    if not apply:
        console.print(
            "[yellow]server_state=not lock-verified; dry-run intentionally acquires no locks "
            "and performs no writes. --apply will require the exclusive server lock.[/]"
        )
        return None
    if not storage_root.is_dir():
        raise click.ClickException(
            f"Storage root does not exist; refusing to create it during migration: {storage_root}"
        )
    lock = FileLock(str(storage_root / _SERVER_LOCK_FILENAME))
    try:
        lock.acquire(timeout=0)
    except LockTimeout as exc:
        raise click.ClickException(
            "rename-agent is offline-only: the Agent Mail server and every writer must be "
            "operator-stopped before --apply. The storage server.lock is currently held."
        ) from exc
    return lock


_IDENTITY_PLATFORMS = frozenset({"linux", "wsl", "win", "mac", "other"})
_IDENTITY_CLIENT_ALIASES = {
    "claude": "claude",
    "cc": "claude",
    "codex": "codex",
    "cx": "codex",
    "copilot": "copilot",
    "cp": "copilot",
    "gemini": "gemini",
}


def _positive_identity_slot(value: str) -> bool:
    return bool(
        value.isascii()
        and value.isdigit()
        and not value.startswith("0")
    )


def _legacy_identity_candidates(
    name: str,
) -> list[tuple[str, str, str, str | None]]:
    """Parse supported legacy/order/token shapes for an existing persisted OLD."""
    if not validate_explicit_agent_id(name):
        return []
    candidates: list[tuple[str, str, str, str | None]] = []

    legacy_parts = name.rsplit("-", 2)
    if len(legacy_parts) == 3:
        host, platform, slot = legacy_parts
        if (
            host
            and platform.casefold() in _IDENTITY_PLATFORMS
            and _positive_identity_slot(slot)
        ):
            candidates.append((host, platform.casefold(), slot, None))

    old_order_parts = name.rsplit("-", 3)
    if len(old_order_parts) == 4:
        host, platform, client, slot = old_order_parts
        canonical_client = _IDENTITY_CLIENT_ALIASES.get(client.casefold())
        if (
            host
            and canonical_client is not None
            and platform.casefold() in _IDENTITY_PLATFORMS
            and _positive_identity_slot(slot)
        ):
            candidates.append(
                (host, platform.casefold(), slot, canonical_client)
            )

    return candidates


def _validate_migratable_source_agent_name(name: str) -> bool:
    """Allow only evidenced legacy/transitional or server-generated identities."""
    return validate_agent_name_format(name) or bool(_legacy_identity_candidates(name))


def _validate_identity_rename_pair(old_name: str, new_name: str) -> bool:
    """Require OLD and NEW to represent the same machine/environment/slot."""
    target = parse_client_platform_host_agent_id(new_name)
    if target is None:
        return False
    if validate_agent_name_format(old_name):
        # A server-coerced adjective+noun has no structural host metadata; the
        # DB id, persisted token and matching archive profile are its evidence.
        return True
    target_client, target_platform, target_host, target_slot = target
    return any(
        host.casefold() == target_host.casefold()
        and platform == target_platform
        and slot == target_slot
        and (client is None or client == target_client)
        for host, platform, slot, client in _legacy_identity_candidates(old_name)
    )


@dataclass(frozen=True, slots=True)
class _RenameAgentRow:
    id: int
    name: str
    registration_token: str | None


def _read_agent_rename_database_state(
    settings: Any,
    project_identifier: str,
    old_name: str,
    new_name: str,
) -> tuple[Project, list[_RenameAgentRow]]:
    """Read the existing SQLite state without schema setup or writable PRAGMAs."""
    backend = make_url(settings.database.url).get_backend_name()
    if not backend.startswith("sqlite"):
        raise ValueError(
            f"rename-agent supports SQLite only in this application build (got '{backend}')"
        )
    database_path = resolve_sqlite_database_path(settings.database.url)
    if not database_path.is_file():
        raise ValueError(
            f"Database does not exist; dry-run will not create it: {database_path}"
        )
    wal_path, _shm_path = get_sqlite_sidecar_paths(database_path)
    try:
        wal_size = wal_path.stat().st_size if wal_path.is_file() else 0
    except OSError as exc:
        raise ValueError(
            "Unable to verify the SQLite WAL before the read-only dry-run"
        ) from exc
    if wal_size:
        raise ValueError(
            "Read-only dry-run requires a cleanly checkpointed SQLite database. "
            "The WAL is non-empty; keep every writer stopped and checkpoint it "
            "with an operator-reviewed SQLite backup procedure before retrying."
        )
    raw_identifier = project_identifier.strip()
    canonical_identifier = _canonicalize_project_identifier(raw_identifier)
    project_slug = slugify(canonical_identifier)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        project_row = connection.execute(
            """
            SELECT id, slug, human_key
            FROM projects
            WHERE slug = ? OR human_key = ? OR human_key = ?
            LIMIT 1
            """,
            (project_slug, canonical_identifier, raw_identifier),
        ).fetchone()
        if project_row is None:
            raise ValueError(f"Project '{raw_identifier}' not found")
        agent_rows = connection.execute(
            """
            SELECT id, name, registration_token
            FROM agents
            WHERE project_id = ? AND lower(name) IN (lower(?), lower(?))
            """,
            (int(project_row["id"]), old_name, new_name),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(
            "Read-only dry-run requires an existing, readable Agent Mail schema"
        ) from exc
    finally:
        if connection is not None:
            connection.close()

    project = Project(
        id=int(project_row["id"]),
        slug=str(project_row["slug"]),
        human_key=str(project_row["human_key"]),
    )
    rows = [
        _RenameAgentRow(
            id=int(row["id"]),
            name=str(row["name"]),
            registration_token=(
                str(row["registration_token"])
                if row["registration_token"] is not None
                else None
            ),
        )
        for row in agent_rows
    ]
    return project, rows


def _open_existing_project_archive(
    settings: Any,
    project_slug: str,
) -> ProjectArchive:
    """Open an existing archive without creating a directory, repo or commit."""
    storage_root = Path(settings.storage.root).expanduser().resolve()
    project_root = storage_root / "projects" / project_slug
    if not storage_root.is_dir() or not (storage_root / ".git").is_dir():
        raise ValueError(
            f"Git archive does not exist; migration will not create it: {storage_root}"
        )
    if not project_root.is_dir():
        raise ValueError(
            f"Project archive does not exist; migration will not create it: {project_root}"
        )
    repo = Repo(str(storage_root))
    return ProjectArchive(
        settings=settings,
        slug=project_slug,
        root=project_root,
        repo=repo,
        lock_path=_project_archive_lock_path(
            _resolved_git_common_dir(storage_root, repo),
            project_slug,
        ),
        repo_root=storage_root,
    )


async def _agent_rename_preflight(
    project_identifier: str,
    old_name: str,
    new_name: str,
    *,
    apply: bool,
) -> dict[str, Any]:
    """Resolve the DB and archive state without exposing registration secrets."""
    settings = get_settings()
    if apply:
        project = await _get_project_record(project_identifier)
    else:
        project, readonly_rows = await asyncio.to_thread(
            _read_agent_rename_database_state,
            settings,
            project_identifier,
            old_name,
            new_name,
        )
    if project.id is None:
        raise ValueError(PROJECT_ID_REQUIRED_MESSAGE)
    if apply:
        await ensure_schema()
        async with get_session() as session:
            db_agents = (
                await session.execute(
                    select(Agent).where(
                        cast(ColumnElement[bool], Agent.project_id == project.id),
                        func.lower(Agent.name).in_([old_name.lower(), new_name.lower()]),
                    )
                )
            ).scalars().all()
        rows = [
            _RenameAgentRow(
                id=cast(int, db_agent.id),
                name=db_agent.name,
                registration_token=db_agent.registration_token,
            )
            for db_agent in db_agents
            if db_agent.id is not None
        ]
    else:
        rows = readonly_rows

    source = next((agent for agent in rows if agent.name.casefold() == old_name.casefold()), None)
    target = next((agent for agent in rows if agent.name.casefold() == new_name.casefold()), None)
    if source is not None and target is not None and source.id != target.id:
        raise ValueError(
            f"Target collision: project already contains agent '{target.name}' "
            f"with Agent.id={target.id}"
        )
    if source is None and target is None:
        raise ValueError(
            f"Agent '{old_name}' was not found and no resumable target '{new_name}' exists"
        )

    agent = source or target
    assert agent is not None
    if not str(agent.registration_token or "").strip():
        raise ValueError(
            f"Agent '{agent.name}' has no persisted registration token; refusing to orphan its identity"
        )

    archive = await asyncio.to_thread(
        _open_existing_project_archive,
        settings,
        project.slug,
    )
    try:
        archive_state = await inspect_agent_archive_rename(
            archive,
            old_name,
            new_name,
            agent.id,
        )
    except BaseException:
        with suppress(Exception):
            archive.repo.close()
        raise
    resolved_old_name = str(archive_state["old_directory"])
    return {
        "project": project,
        "archive": archive,
        "agent_id": agent.id,
        "old_name": resolved_old_name,
        "new_name": new_name,
        "database_state": "pending" if source is not None else "already_applied",
        "archive_state": archive_state["state"],
        "registration_token": "persisted (value withheld)",
    }


async def _apply_agent_rename_database(
    project_id: int,
    agent_id: int,
    old_name: str,
    new_name: str,
) -> dict[str, Any]:
    """Rename one Agent row in place and update only matching window labels."""
    await ensure_schema()
    async with get_session() as session:
        agent = await session.get(Agent, agent_id)
        if agent is None or agent.project_id != project_id:
            raise ValueError(f"Agent.id={agent_id} no longer belongs to the requested project")
        persisted_token = str(agent.registration_token or "").strip()
        if not persisted_token:
            raise ValueError("Agent lost its persisted registration token during migration")

        if agent.name.casefold() == new_name.casefold():
            database_state = "already_applied"
        elif agent.name.casefold() == old_name.casefold():
            collision = (
                await session.execute(
                    select(Agent.id).where(
                        cast(ColumnElement[bool], Agent.project_id == project_id),
                        func.lower(Agent.name) == new_name.lower(),
                        cast(ColumnElement[bool], Agent.id != agent_id),
                    )
                )
            ).scalar_one_or_none()
            if collision is not None:
                raise ValueError(
                    f"Target collision appeared during migration: Agent.id={collision} owns '{new_name}'"
                )
            agent.name = new_name
            session.add(agent)
            database_state = "applied"
        else:
            raise ValueError(
                f"Agent.id={agent_id} is now named '{agent.name}', not '{old_name}' or '{new_name}'"
            )

        windows = (
            await session.execute(
                select(WindowIdentity).where(
                    cast(ColumnElement[bool], WindowIdentity.project_id == project_id),
                    func.lower(WindowIdentity.display_name) == old_name.lower(),
                )
            )
        ).scalars().all()
        for identity in windows:
            identity.display_name = new_name
            session.add(identity)
        await session.commit()
        await session.refresh(agent)
        if agent.id != agent_id or agent.name.casefold() != new_name.casefold():
            raise RuntimeError("Agent identity verification failed after database commit")
        if str(agent.registration_token or "").strip() != persisted_token:
            raise RuntimeError("Agent registration token changed during database commit")
        return {
            "state": database_state,
            "agent_id": agent_id,
            "window_identities_updated": len(windows),
            "id_preserved": True,
            "registration_token_preserved": True,
        }


def _validate_agent_rename_request(old_name: str, new_name: str, *, apply: bool, confirm: str | None) -> str:
    if not _validate_migratable_source_agent_name(old_name):
        raise click.ClickException(
            "rename-agent only migrates evidenced host-os-slot, deployed "
            "host-os-client-slot identities (including explicit short-token state), "
            "or server-generated adjective+noun identities; older "
            "pre-platform identities require a separate operator-reviewed migration"
        )
    if old_name.casefold() == new_name.casefold():
        raise click.ClickException("Case-only identity renames are forbidden")
    if not validate_client_platform_host_agent_id(new_name):
        raise click.ClickException("The target is not a canonical client-os-host-slot identity")
    if not _validate_identity_rename_pair(old_name, new_name):
        raise click.ClickException(
            "OLD and NEW must preserve the same host, OS and slot; only the "
            "canonical client segment/order may change"
        )
    expected_confirmation = f"{old_name}=>{new_name}"
    if apply and confirm != expected_confirmation:
        raise click.ClickException(f"--apply requires exact --confirm {expected_confirmation}")
    return expected_confirmation


@app.command("rename-agent")
def rename_agent(
    project: Annotated[str, typer.Argument(..., help=PROJECT_IDENTIFIER_HELP)],
    old_name: Annotated[str, typer.Argument(..., help="Existing legacy Agent.name")],
    new_name: Annotated[str, typer.Argument(..., help="Canonical client-os-host-slot name")],
    apply: Annotated[bool, typer.Option("--apply", help="Apply the offline migration.")] = False,
    confirm: Annotated[
        str | None,
        typer.Option("--confirm", help="Required with --apply; exact value OLD=>NEW."),
    ] = None,
) -> None:
    """Rename one existing Agent in place without forking its identity or history.

    The command is local and offline: it never calls MCP or the web UI. Stop the
    server, watchers and every archive writer, take database/archive backups,
    run the default dry-run, then repeat with ``--apply --confirm OLD=>NEW``.
    """
    expected_confirmation = _validate_agent_rename_request(old_name, new_name, apply=apply, confirm=confirm)

    settings = get_settings()
    backend = make_url(settings.database.url).get_backend_name()
    if not backend.startswith("sqlite"):
        raise click.ClickException(
            f"rename-agent supports SQLite only in this application build (got '{backend}')"
        )
    server_lock = _identity_migration_server_lock(settings, apply=apply)
    preflight: dict[str, Any] | None = None
    try:
        try:
            preflight_coro = _agent_rename_preflight(
                project,
                old_name,
                new_name,
                apply=apply,
            )
            preflight = (
                _run_async(preflight_coro)
                if apply
                else asyncio.run(preflight_coro)
            )
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc

        project_record = cast(Project, preflight["project"])
        console.print(
            f"[bold]{'APPLY PLAN' if apply else 'DRY RUN'}: rename-agent[/]"
        )
        console.print(f"project={project_record.human_key}")
        console.print(f"old_name={preflight['old_name']}")
        console.print(f"new_name={preflight['new_name']}")
        console.print(f"agent_id={preflight['agent_id']}")
        console.print(f"database_state={preflight['database_state']}")
        console.print(f"archive_state={preflight['archive_state']}")
        console.print(f"registration_token={preflight['registration_token']}")
        if server_lock is not None:
            console.print("server_state=operator-stopped (exclusive storage lock verified)")
        console.print("local_state=run migrate-agent-state on each client host after this rename")
        if not apply:
            console.print(
                f"[yellow]No mutation performed. Back up both stores, then use --apply "
                f"--confirm {expected_confirmation} while all writers remain stopped.[/]"
            )
            return

        renamed_at = datetime.now(timezone.utc).isoformat()
        archive = preflight["archive"]
        if project_record.id is None:
            raise click.ClickException("Project lost its id during migration")
        try:
            archive_result = _run_async(
                migrate_agent_archive(
                    archive,
                    str(preflight["old_name"]),
                    new_name,
                    int(preflight["agent_id"]),
                    renamed_at,
                )
            )
            database_result = _run_async(
                _apply_agent_rename_database(
                    project_record.id,
                    int(preflight["agent_id"]),
                    str(preflight["old_name"]),
                    new_name,
                )
            )
        except Exception as exc:
            raise click.ClickException(
                f"Identity migration stopped safely and is resumable with the same command: {exc}"
            ) from exc

        console.print("[green]APPLIED: identity renamed in place.[/]")
        console.print(f"archive_state={archive_result['state']}")
        console.print(f"database_{database_result['state']}")
        console.print(
            "preserved=id, registration_token, messages, reads, acknowledgements, "
            "contacts, and reservations"
        )
        console.print(
            f"window_identities_updated={database_result['window_identities_updated']}"
        )
        console.print(
            f"reservation_records_updated={archive_result['reservation_records_updated']}"
        )
        console.print("next=run migrate-agent-state on each stopped client host before restart")
    finally:
        if server_lock is not None:
            server_lock.release()
        if preflight is not None:
            archive = cast(ProjectArchive, preflight["archive"])
            with suppress(Exception):
                archive.repo.close()


def _agent_state_previous_component(project: str) -> str:
    """Return the lossy component used by previous hook generations."""
    mapped = project.replace("/", "_")
    return "".join(char for char in mapped if char.isascii() and (char.isalnum() or char in "._-"))[:96]


def _agent_state_component(value: str) -> str:
    """Mirror ``am_state_component``: readable prefix plus collision-proof hash."""
    translated = re.sub(r"[^A-Za-z0-9._ -]", "_", value)
    squeezed = re.sub(r"_+", "_", translated)
    prefix = squeezed.replace(" ", "_")[:47]
    if prefix.endswith("_"):
        prefix = prefix[:-1]
    prefix = prefix or "state"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


_TRANSITIONAL_CLIENT_ALIASES = {
    "claude": "cc",
    "codex": "cx",
    "copilot": "cp",
}


def _remove_dead_agent_state_lock(lock_dir: Path, pid_path: Path) -> bool:
    owner = 0
    with suppress(OSError, ValueError):
        owner = int(pid_path.read_text(encoding="utf-8").strip())
    if owner and not _pid_is_alive(owner):
        with suppress(OSError):
            if pid_path.read_text(encoding="utf-8").strip() == str(owner):
                pid_path.unlink()
                lock_dir.rmdir()
                return True
    return False


@contextmanager
def _portable_agent_state_lock(
    target_path: Path,
    *,
    timeout_seconds: float = 10.0,
) -> Iterator[None]:
    """Interoperate with the hooks' portable ``mkdir + pid`` lock protocol."""
    lock_dir = Path(f"{target_path}.lock")
    pid_path = lock_dir / "pid"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            if _remove_dead_agent_state_lock(lock_dir, pid_path):
                continue
            if time.monotonic() >= deadline:
                raise click.ClickException(
                    f"Timed out waiting for Agent Mail state lock: {lock_dir}"
                ) from None
            time.sleep(0.05)
    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        with suppress(OSError):
            lock_dir.rmdir()
        raise
    try:
        yield
    finally:
        with suppress(OSError):
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
                lock_dir.rmdir()


def _agent_state_backup(path: Path, state_dir: Path) -> Path:
    backup_dir = state_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        state_dir.chmod(0o700)
        backup_dir.chmod(0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"{path.name}.{timestamp}.bak"
    shutil.copy2(path, target)
    with suppress(OSError):
        target.chmod(0o600)
    return target


def _atomic_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        with suppress(OSError):
            temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        with suppress(OSError):
            temporary.unlink()


def _load_migrating_agent_credentials(
    credential_path: Path, project: str, old_name: str, new_name: str,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    if not credential_path.is_file():
        raise click.ClickException(f"Credential store does not exist: {credential_path}")
    try:
        credentials = json.loads(credential_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException("Credential store is unreadable or invalid JSON") from exc
    if not isinstance(credentials, dict) or not isinstance(credentials.get(project), dict):
        raise click.ClickException("Credential store has no object for the exact project key")
    project_credentials = cast(dict[str, Any], credentials[project])
    old_token = project_credentials.get(old_name)
    new_token = project_credentials.get(new_name)
    if old_token and new_token:
        raise click.ClickException(
            "Both old and new credential keys exist; refusing to copy a token between identities"
        )
    if old_token is not None and (
        not isinstance(old_token, str) or not old_token.strip()
    ):
        raise click.ClickException("Legacy credential is empty or invalid")
    if new_token is not None and (
        not isinstance(new_token, str) or not new_token.strip()
    ):
        raise click.ClickException("Target credential is empty or invalid")
    if old_token is None and new_token is None:
        raise click.ClickException("Neither the old nor target credential key exists")
    return credentials, project_credentials, old_token


def _agent_granted_paths(
    credentials: dict[str, Any], project: str, client: str, slot: int, resolved_state: Path,
) -> tuple[Path, list[Path]]:
    current_component = _agent_state_component(project)
    previous_component = _agent_state_previous_component(project)
    granted_dir = resolved_state / "granted"
    client_token = client.casefold()
    canonical_granted = granted_dir / f"{current_component}--{client_token}-{slot}"
    previous_granted = granted_dir / f"{previous_component}--{client_token}-{slot}"
    transitional_client = _TRANSITIONAL_CLIENT_ALIASES.get(client_token)
    transitional_granted = (
        granted_dir / f"{current_component}--{transitional_client}-{slot}"
        if transitional_client is not None
        else None
    )
    previous_transitional_granted = (
        granted_dir / f"{previous_component}--{transitional_client}-{slot}"
        if transitional_client is not None
        else None
    )
    legacy_granted = granted_dir / previous_component
    lossy_paths = [previous_granted, legacy_granted]
    if previous_transitional_granted is not None:
        lossy_paths.append(previous_transitional_granted)
    colliding_projects = [
        key
        for key in credentials
        if isinstance(key, str)
        and key != project
        and _agent_state_previous_component(key) == previous_component
    ]
    if colliding_projects and any(path.exists() for path in lossy_paths):
        raise click.ClickException(
            "Legacy granted-name path is ambiguous for colliding project keys; "
            "resolve it manually before migrating either identity"
        )
    granted_candidates = list(
        dict.fromkeys(
            path
            for path in (
                canonical_granted,
                previous_granted,
                transitional_granted,
                previous_transitional_granted,
                legacy_granted,
            )
            if path is not None
        )
    )
    return canonical_granted, granted_candidates


def _read_migrating_granted_names(
    granted_candidates: list[Path], old_name: str, new_name: str,
) -> dict[Path, str]:
    try:
        granted_values = {
            path: path.read_text(encoding="utf-8").strip()
            for path in granted_candidates
            if path.is_file()
        }
    except OSError as exc:
        raise click.ClickException("A granted-name file is unreadable") from exc
    for path, granted_value in granted_values.items():
        if granted_value not in {old_name, new_name}:
            raise click.ClickException(
                f"Granted-name file belongs to a different identity: {path}"
            )
    return granted_values


def _migrate_agent_state_files(
    *,
    project: str,
    old_name: str,
    new_name: str,
    client: str,
    slot: int,
    resolved_state: Path,
    apply: bool,
) -> None:
    """Validate and optionally migrate state while the caller holds apply locks."""
    credential_path = resolved_state / "credentials.json"
    credentials, project_credentials, old_token = _load_migrating_agent_credentials(
        credential_path, project, old_name, new_name,
    )
    canonical_granted, granted_candidates = _agent_granted_paths(credentials, project, client, slot, resolved_state)
    granted_values = _read_migrating_granted_names(granted_candidates, old_name, new_name)
    obsolete_granted = [
        path for path in granted_candidates if path != canonical_granted
    ]
    credential_state = "pending" if old_token is not None else "already_migrated"
    granted_state = (
        "already_migrated"
        if granted_values.get(canonical_granted) == new_name
        and not any(path.exists() for path in obsolete_granted)
        else "pending"
    )
    overall_state = (
        "already_migrated"
        if credential_state == granted_state == "already_migrated"
        else "pending"
    )
    console.print(f"[bold]{'APPLY PLAN' if apply else 'DRY RUN'}: migrate-agent-state[/]")
    console.print(f"project={project}")
    console.print(f"old_name={old_name}")
    console.print(f"new_name={new_name}")
    console.print(f"credential_state={credential_state}")
    console.print(f"granted_state={granted_state}")
    console.print(f"state={overall_state}")
    console.print(f"credential_store={credential_path}")
    console.print("registration_token=value withheld")
    console.print(
        "precondition=server Agent row was already renamed in place; no network request made"
    )
    if not apply or overall_state == "already_migrated":
        return

    try:
        _agent_state_backup(credential_path, resolved_state)
        for path in granted_values:
            _agent_state_backup(path, resolved_state)

        # Publish canonical state first. If the process stops before the token
        # key swap, hooks fail closed because NEW still has no local token; the
        # same command safely completes the remaining phases.
        _atomic_private_text(canonical_granted, new_name)
        for obsolete_path in obsolete_granted:
            if obsolete_path.is_file():
                obsolete_path.unlink()

        if old_token is not None:
            project_credentials.pop(old_name)
            project_credentials[new_name] = old_token
            credentials[project] = project_credentials
            _atomic_private_text(
                credential_path,
                json.dumps(credentials, indent=2, sort_keys=True) + "\n",
            )
    except Exception as exc:
        raise click.ClickException(
            f"Local migration stopped; private backups were retained and retry is safe: {exc}"
        ) from exc
    console.print("[green]APPLIED: local credential key and granted name migrated.[/]")
    console.print("registration_token=value preserved and withheld")


def _resolve_absolute_client_path(value: str | Path, *, setting: str) -> Path:
    """Resolve a user-level client path without accepting CWD-relative state."""
    try:
        candidate = Path(value).expanduser()
    except RuntimeError as exc:
        raise click.ClickException(f"{setting} is not a valid user path") from exc
    if not candidate.is_absolute():
        raise click.ClickException(f"{setting} must be an absolute path")
    return candidate.resolve()


def _global_agent_mail_env_path() -> Path:
    """Locate the user-level hook configuration without consulting repo state."""
    ambient_config = DecoupleConfig(RepositoryEmpty())
    configured_path = str(
        ambient_config(
            "AGENT_MAIL_ENV_FILE",
            default=str(Path.home() / ".agent-mail.env"),
        )
        or ""
    ).strip()
    return _resolve_absolute_client_path(
        configured_path,
        setting="AGENT_MAIL_ENV_FILE",
    )


def _configured_agent_state_directory() -> Path:
    """Resolve private client state from the same global env used by hooks."""
    global_env_path = _global_agent_mail_env_path()
    try:
        repository = RepositoryEnv(str(global_env_path))
    except FileNotFoundError:
        repository = RepositoryEmpty()
    decouple_config = DecoupleConfig(repository)
    configured_state = str(
        decouple_config("AGENT_MAIL_STATE_DIR", default="") or ""
    ).strip()
    if configured_state:
        return _resolve_absolute_client_path(
            configured_state,
            setting="AGENT_MAIL_STATE_DIR",
        )
    ambient_config = DecoupleConfig(RepositoryEmpty())
    state_home = str(
        ambient_config(
            "XDG_STATE_HOME",
            default=str(Path.home() / ".local" / "state"),
        )
    ).strip()
    return _resolve_absolute_client_path(
        Path(state_home) / "agent-mail",
        setting="XDG_STATE_HOME",
    )


@app.command("migrate-agent-state")
def migrate_agent_state(
    project: Annotated[str, typer.Argument(..., help="Exact project key in credentials.json")],
    old_name: Annotated[str, typer.Argument(..., help="Legacy credential key")],
    new_name: Annotated[str, typer.Argument(..., help="Renamed server identity")],
    client: Annotated[str, typer.Option("--client", help="Canonical client token, e.g. codex")],
    slot: Annotated[int, typer.Option("--slot", min=1, help="Stable positive client slot")],
    state_dir: Annotated[
        Path | None,
        typer.Option("--state-dir", help="Agent Mail private state directory"),
    ] = None,
    apply: Annotated[bool, typer.Option("--apply", help="Apply the local state move.")] = False,
    confirm: Annotated[
        str | None,
        typer.Option("--confirm", help="Required with --apply; exact value OLD=>NEW."),
    ] = None,
) -> None:
    """Atomically move one local credential key after the server-side rename."""
    if not _validate_migratable_source_agent_name(old_name):
        raise click.ClickException(
            "migrate-agent-state does not handle arbitrary older pre-platform identities"
        )
    if not validate_client_platform_host_agent_id(new_name):
        raise click.ClickException("The target identity is not canonical")
    if not _validate_identity_rename_pair(old_name, new_name):
        raise click.ClickException(
            "OLD and NEW do not preserve the same host, OS and slot"
        )
    canonical_parts = parse_client_platform_host_agent_id(new_name)
    assert canonical_parts is not None
    expected_client, _expected_platform, _expected_host, expected_slot = canonical_parts
    if expected_client != client.casefold() or expected_slot != str(slot):
        raise click.ClickException("--client/--slot do not match the target identity")
    expected_confirmation = f"{old_name}=>{new_name}"
    if apply and confirm != expected_confirmation:
        raise click.ClickException(
            f"--apply requires exact --confirm {expected_confirmation}"
        )

    resolved_state = (
        _resolve_absolute_client_path(state_dir, setting="--state-dir")
        if state_dir is not None
        else _configured_agent_state_directory()
    )
    credential_path = resolved_state / "credentials.json"
    current_component = _agent_state_component(project)
    canonical_granted = (
        resolved_state
        / "granted"
        / f"{current_component}--{client.casefold()}-{slot}"
    )
    lock_context = (
        _portable_agent_state_lock(credential_path)
        if apply
        else nullcontext()
    )
    with lock_context:
        granted_lock_context = (
            _portable_agent_state_lock(canonical_granted)
            if apply
            else nullcontext()
        )
        with granted_lock_context:
            _migrate_agent_state_files(
                project=project,
                old_name=old_name,
                new_name=new_name,
                client=client,
                slot=slot,
                resolved_state=resolved_state,
                apply=apply,
            )


@app.command("lint")
def lint() -> None:
    """Run Ruff linting with automatic fixes."""
    console.rule("[bold]Running Ruff Lint[/bold]")
    _run_command(["ruff", "check", "--fix", "--unsafe-fixes"])
    console.print("[green]Linting complete.[/]")


@app.command("typecheck")
def typecheck() -> None:
    """Run MyPy type checking."""
    console.rule("[bold]Running Type Checker[/bold]")
    _run_command(["uvx", "ty", "check"])
    console.print("[green]Type check complete.[/]")


@contextmanager
def _share_cli_step(error_template: str, temp_dir: tempfile.TemporaryDirectory[str] | None = None) -> Iterator[None]:
    try:
        yield
    except ShareExportError as exc:
        console.print(error_template.format(error=exc))
        if temp_dir is not None:
            temp_dir.cleanup()
        raise typer.Exit(code=1) from exc


def _print_hosting_hints(hosting_hints: Sequence[HostingHint]) -> None:
    if not hosting_hints:
        console.print("[dim]No hosting targets detected automatically; consult HOW_TO_DEPLOY.md for guidance.[/]")
        return
    table = Table(title="Detected Hosting Targets")
    table.add_column("Host")
    table.add_column("Signals")
    for hint in hosting_hints:
        table.add_row(hint.title, "\n".join(hint.signals))
    console.print(table)


def _print_snapshot_search_status(fts_enabled: bool) -> None:
    if fts_enabled:
        console.print("[green]✓ Built FTS5 index for snapshot search.[/]")
    else:
        console.print("[yellow]FTS5 not available; viewer will fall back to LIKE search.[/]")


def _sign_share_manifest(output_path: Path, signing_key: Path, signing_public_out: Path | None, *, overwrite: bool = False) -> None:
    with _share_cli_step("[red]Manifest signing failed:[/] {error}"):
        public_out_path = _resolve_path(signing_public_out) if signing_public_out else None
        signing_options = {"overwrite": True} if overwrite else {}
        signature_info = sign_manifest(
            output_path / SHARE_MANIFEST_FILENAME, signing_key, output_path,
            public_out=public_out_path, **signing_options,
        )
        console.print(f"[green]✓ Signed manifest (Ed25519, public key {signature_info['public_key']})[/]")


def _print_share_snapshot_summary(
    snapshot_ctx: SnapshotContext, artifacts: BundleArtifacts, inline_threshold: int, detach_threshold: int,
) -> None:
    scrub_summary = snapshot_ctx.scrub_summary
    console.print(
        f"[green]✓ Applied '{scrub_summary.preset}' scrub (pseudonymized {scrub_summary.agents_pseudonymized}/{scrub_summary.agents_total} agents, "
        f"{scrub_summary.secrets_replaced} secret tokens redacted, {scrub_summary.bodies_redacted} bodies replaced).[/]"
    )
    included_projects = ", ".join(record.slug for record in snapshot_ctx.scope.projects)
    console.print(f"[green]✓ Project scope includes: {included_projects or 'none'}[/]")
    att_stats = artifacts.attachments_manifest.get("stats", {})
    console.print(
        "[green]✓ Packaged attachments: "
        f"{att_stats.get('inline', 0)} inline, {att_stats.get('copied', 0)} copied, "
        f"{att_stats.get('externalized', 0)} external, {att_stats.get('missing', 0)} missing "
        f"(inline ≤ {inline_threshold} B, external ≥ {detach_threshold} B).[/]"
    )
    if snapshot_ctx.fts_enabled:
        console.print("[green]✓ Built FTS5 index for full-text viewer search.[/]")
    else:
        console.print("[yellow]Search fallback active (FTS5 unavailable in current sqlite build).[/]")


def _print_share_dry_run(
    snapshot_ctx: SnapshotContext, storage_root: Path, inline_threshold: int, detach_threshold: int,
) -> None:
    summary = summarize_snapshot(
        snapshot_ctx.snapshot_path, storage_root=storage_root,
        inline_threshold=inline_threshold, detach_threshold=detach_threshold,
    )
    console.rule("[bold]Dry-Run Summary[/bold]")
    overview = Table(show_header=False)
    projects_text = ", ".join(project["slug"] for project in summary["projects"]) or "All projects"
    overview.add_row("Projects", projects_text)
    overview.add_row("Messages", str(summary["messages"]))
    overview.add_row("Threads", str(summary["threads"]))
    overview.add_row("FTS Search", "enabled" if snapshot_ctx.fts_enabled else "fallback (LIKE)")
    attachments = summary["attachments"]
    overview.add_row("Attachments", (
        f"total={attachments['total']} inline≤{inline_threshold}B:{attachments['inline_candidates']} "
        f"external≥{detach_threshold}B:{attachments['external_candidates']} missing:{attachments['missing']}"
    ))
    overview.add_row("Largest attachment", f"{attachments['largest_bytes']} bytes" if attachments["largest_bytes"] else "n/a")
    console.print(overview)
    console.rule("Security Checklist")
    scrub_summary = snapshot_ctx.scrub_summary
    checklist = [
        f"Scrub preset: {scrub_summary.preset}",
        f"Agents pseudonymized: {scrub_summary.agents_pseudonymized}/{scrub_summary.agents_total}",
        f"Ack flags cleared: {scrub_summary.ack_flags_cleared}",
        f"Recipients read/ack cleared: {scrub_summary.recipients_cleared}",
        f"File reservations removed: {scrub_summary.file_reservations_removed}",
        f"Agent links removed: {scrub_summary.agent_links_removed}",
        f"Secrets redacted: {scrub_summary.secrets_replaced}",
        f"Bodies redacted: {scrub_summary.bodies_redacted}",
        f"Attachments cleared: {scrub_summary.attachments_cleared}",
    ]
    for item in checklist:
        console.print(f" • {item}")
    console.print()
    console.print(
        "[cyan]Run without --dry-run to generate the bundle. Consider enabling signing ( --signing-key ) and encryption (--age-recipient ) before publishing.[/]"
    )


def _package_share_export(output_path: Path, age_recipients: list[str]) -> None:
    archive_path = output_path.parent / f"{output_path.name}.zip"
    console.print(f"[cyan]Packaging archive:[/] {archive_path}")
    with _share_cli_step("[red]Failed to create ZIP archive:[/] {error}"):
        package_directory_as_zip(output_path, archive_path)
    console.print("[green]✓ Packaged ZIP archive for distribution.[/]")
    if age_recipients:
        with _share_cli_step("[red]Bundle encryption failed:[/] {error}"):
            encrypted_path = encrypt_bundle(archive_path, age_recipients)
            if encrypted_path:
                console.print(f"[green]✓ Encrypted bundle written to {encrypted_path}[/]")


def _prepare_share_output(raw_output: Path, *, dry_run: bool) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        if dry_run:
            temp_dir = tempfile.TemporaryDirectory(prefix="mailbox-share-dry-run-")
            output_path = Path(temp_dir.name)
        else:
            output_path = prepare_output_directory(raw_output)
        return output_path, temp_dir
    except ShareExportError as exc:
        console.print(f"[red]Invalid output directory:[/] {exc}")
        if temp_dir is not None:
            temp_dir.cleanup()
        raise typer.Exit(code=1) from exc


@share_app.command("export")
def share_export(
    output: Annotated[str, typer.Option("--output", "-o", help="Directory where the static bundle should be written.")],
    interactive: Annotated[
        bool,
        typer.Option(
            "--interactive",
            "-i",
            help="Launch an interactive wizard (future enhancement; currently prints guidance).",
        ),
    ] = False,
    projects: Annotated[list[str] | None, typer.Option("--project", "-p", help="Limit export to specific project slugs or human keys.")] = None,
    inline_threshold: Annotated[
        int,
        typer.Option(
            "--inline-threshold",
            help="Inline attachments ≤ this many bytes as data URIs.",
            min=0,
            show_default=True,
        ),
    ] = INLINE_ATTACHMENT_THRESHOLD,
    detach_threshold: Annotated[
        int,
        typer.Option(
            "--detach-threshold",
            help="Mark attachments ≥ this many bytes as external (not bundled).",
            min=0,
            show_default=True,
        ),
    ] = DETACH_ATTACHMENT_THRESHOLD,
    scrub_preset: Annotated[
        str,
        typer.Option(
            "--scrub-preset",
            help="Redaction preset to apply (e.g., standard, strict).",
            case_sensitive=False,
            show_default=True,
        ),
    ] = "standard",
    chunk_threshold: Annotated[
        int,
        typer.Option(
            "--chunk-threshold",
            help="Chunk the SQLite database when it exceeds this size (bytes).",
            min=0,
            show_default=True,
        ),
    ] = DEFAULT_CHUNK_THRESHOLD,
    chunk_size: Annotated[
        int,
        typer.Option(
            "--chunk-size",
            help="Chunk size in bytes when chunking is enabled.",
            min=1024,
            show_default=True,
        ),
    ] = DEFAULT_CHUNK_SIZE,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Generate a security summary without writing bundle artifacts.",
            show_default=True,
        ),
    ] = False,
    zip_bundle: Annotated[
        bool,
        typer.Option(
            "--zip/--no-zip",
            help="Package the exported directory into a ZIP archive (enabled by default).",
            show_default=True,
        ),
    ] = True,
    signing_key: Annotated[Optional[Path], typer.Option("--signing-key", help="Path to Ed25519 signing key (32-byte seed).")]=None,
    signing_public_out: Annotated[Optional[Path], typer.Option("--signing-public-out", help="Write public key to this file after signing.")]=None,
    age_recipients: Annotated[
        Optional[list[str]],
        typer.Option(
            "--age-recipient",
            help="Encrypt the ZIP archive with age using the provided recipient(s). May be passed multiple times.",
        ),
    ] = None,
) -> None:
    """Export the MCP Agent Mail mailbox into a shareable static bundle (snapshot + scaffolding prototype)."""

    age_recipient_list = list(age_recipients or ())
    if projects is None:
        projects = []
    scrub_preset = (scrub_preset or "standard").strip().lower()
    if scrub_preset not in VIEWER_SCRUB_PRESETS:
        console.print(
            "[red]Invalid scrub preset:[/] "
            f"{scrub_preset}. Choose one of: {', '.join(VIEWER_SCRUB_PRESETS)}."
        )
        raise typer.Exit(code=1)
    raw_output = _resolve_path(output)
    output_path, temp_dir = _prepare_share_output(raw_output, dry_run=dry_run)

    console.rule("[bold]Static Mailbox Export[/bold]")

    with _share_cli_step("[red]Failed to resolve SQLite database: {error}[/]", temp_dir):
        database_path = resolve_sqlite_database_path()

    if interactive:
        wizard = _run_share_export_wizard(
            database_path,
            inline_threshold,
            detach_threshold,
            chunk_threshold,
            chunk_size,
            scrub_preset,
        )
        projects = wizard["projects"]
        inline_threshold = wizard["inline_threshold"]
        detach_threshold = wizard["detach_threshold"]
        chunk_threshold = wizard["chunk_threshold"]
        chunk_size = wizard["chunk_size"]
        zip_bundle = wizard["zip_bundle"]
        scrub_preset = wizard["scrub_preset"]

    console.print(f"[cyan]Using database:[/] {database_path}")

    snapshot_path = output_path / MAILBOX_DATABASE_FILENAME
    console.print(f"[cyan]Creating snapshot:[/] {snapshot_path}")

    if detach_threshold <= inline_threshold:
        console.print(
            "[yellow]Adjusting detach threshold to exceed inline threshold to avoid conflicts.[/]"
        )
        detach_threshold = inline_threshold + max(1024, inline_threshold // 2 or 1)

    hosting_hints = detect_hosting_hints(output_path)
    _print_hosting_hints(hosting_hints)

    console.print("[cyan]Applying project filters and scrubbing data...[/]")
    with _share_cli_step("[red]Snapshot preparation failed:[/] {error}", temp_dir):
        snapshot_ctx = create_snapshot_context(
            source_database=database_path,
            snapshot_path=snapshot_path,
            project_filters=projects,
            scrub_preset=scrub_preset,
            purpose="viewer_export",
        )

    scope = snapshot_ctx.scope
    scrub_summary = snapshot_ctx.scrub_summary
    fts_enabled = snapshot_ctx.fts_enabled
    _print_snapshot_search_status(fts_enabled)

    settings = get_settings()
    storage_root = Path(settings.storage.root).expanduser()

    if dry_run:
        _print_share_dry_run(snapshot_ctx, storage_root, inline_threshold, detach_threshold)
        if temp_dir is not None:
            temp_dir.cleanup()
            temp_dir = None
        return

    export_config: dict[str, Any] = {
        "inline_threshold": inline_threshold,
        "detach_threshold": detach_threshold,
        "chunk_threshold": chunk_threshold,
        "chunk_size": chunk_size,
        "scrub_preset": scrub_preset,
        "projects": list(projects),
    }

    console.print("[cyan]Packaging attachments, viewer assets, and manifest...[/]")
    with _share_cli_step("[red]Failed to build bundle assets:[/] {error}"):
        bundle_artifacts = build_bundle_assets(
            snapshot_ctx.snapshot_path,
            output_path,
            storage_root=storage_root,
            inline_threshold=inline_threshold,
            detach_threshold=detach_threshold,
            chunk_threshold=chunk_threshold,
            chunk_size=chunk_size,
            scope=scope,
            project_filters=projects,
            scrub_summary=scrub_summary,
            hosting_hints=hosting_hints,
            fts_enabled=fts_enabled,
            export_config=export_config,
        )
    chunk_manifest = bundle_artifacts.chunk_manifest
    if chunk_manifest:
        console.print(
            f"[cyan]Chunked database into {chunk_manifest['chunk_count']} files of ~{chunk_manifest['chunk_size']//1024} KiB.[/]"
        )


    if signing_key is not None:
        _sign_share_manifest(output_path, signing_key, signing_public_out)

    console.print("[green]✓ Created SQLite snapshot for sharing.[/]")
    _print_share_snapshot_summary(snapshot_ctx, bundle_artifacts, inline_threshold, detach_threshold)
    console.print("[green]✓ Generated manifest, README.md, HOW_TO_DEPLOY.md, and viewer assets.[/]")

    if zip_bundle:
        _package_share_export(output_path, age_recipient_list)

    console.print(
        "[dim]Next steps: flesh out the static SPA (search, thread detail) and tighten signing/encryption defaults per the roadmap.[/]"
    )


def _list_projects_for_wizard(database_path: Path) -> list[tuple[str, str]]:
    projects: list[tuple[str, str]] = []
    conn = connect_sqlite_readonly(database_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT slug, human_key FROM projects ORDER BY slug COLLATE NOCASE").fetchall()
        for row in rows:
            slug = row["slug"] or ""
            human_key = row["human_key"] or ""
            projects.append((slug, human_key))
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return projects


def _parse_positive_int(value: str, default: int) -> int:
    text = value.strip()
    if not text:
        return default
    try:
        result = int(text)
        if result < 0:
            raise ValueError
        return result
    except ValueError:
        console.print(f"[yellow]Invalid number '{value}'. Using default {default}.[/]")
        return default


def _run_share_export_wizard(
    database_path: Path,
    default_inline: int,
    default_detach: int,
    default_chunk_threshold: int,
    default_chunk_size: int,
    default_scrub_preset: str,
) -> dict[str, Any]:
    console.rule("[bold]Share Export Wizard[/bold]")
    projects = _list_projects_for_wizard(database_path)
    if projects:
        console.print("[cyan]Available projects:[/]")
        for slug, human_key in projects:
            console.print(f"  • [bold]{slug}[/] ({human_key})")
    else:
        console.print("[yellow]No projects detected in the database (exporting all projects).[/]")

    project_input = typer.prompt(
        "Enter project slugs or human keys to include (comma separated, leave blank for all)",
        default="",
    )
    selected_projects = [part.strip() for part in project_input.split(",") if part.strip()]

    inline_input = typer.prompt(
        f"Inline attachments threshold in bytes (default {default_inline})",
        default=str(default_inline),
    )
    inline_threshold = _parse_positive_int(inline_input, default_inline)

    detach_input = typer.prompt(
        f"External attachment threshold in bytes (default {default_detach})",
        default=str(default_detach),
    )
    detach_threshold = _parse_positive_int(detach_input, default_detach)

    chunk_threshold_input = typer.prompt(
        f"Chunk database when size exceeds (bytes, default {default_chunk_threshold})",
        default=str(default_chunk_threshold),
    )
    chunk_threshold = _parse_positive_int(chunk_threshold_input, default_chunk_threshold)

    chunk_size_input = typer.prompt(
        f"Chunk size in bytes (default {default_chunk_size})",
        default=str(default_chunk_size),
    )
    chunk_size = _parse_positive_int(chunk_size_input, default_chunk_size)

    console.print("[cyan]Scrub presets:[/]")
    for name in VIEWER_SCRUB_PRESETS:
        config = SCRUB_PRESETS[name]
        console.print(f"  • [bold]{name}[/] - {config['description']}")
    preset_input = typer.prompt(
        f"Scrub preset (default {default_scrub_preset})",
        default=default_scrub_preset,
    )
    preset_value = (preset_input or default_scrub_preset).strip().lower()
    if preset_value not in VIEWER_SCRUB_PRESETS:
        console.print(
            f"[yellow]Unknown preset '{preset_value}'. Using {default_scrub_preset} instead.[/]"
        )
        preset_value = default_scrub_preset

    zip_bundle = typer.confirm("Package the output directory as a .zip archive?", default=True)

    return {
        "projects": selected_projects,
        "inline_threshold": inline_threshold,
        "detach_threshold": detach_threshold,
        "chunk_threshold": chunk_threshold,
        "chunk_size": chunk_size,
        "scrub_preset": preset_value,
        "zip_bundle": zip_bundle,
    }


def _bump_preview_force_token() -> int:
    global _PREVIEW_FORCE_TOKEN
    with _PREVIEW_FORCE_LOCK:
        _PREVIEW_FORCE_TOKEN = (_PREVIEW_FORCE_TOKEN + 1) % (2 ** 63)
        return _PREVIEW_FORCE_TOKEN


def _collect_preview_status(bundle_path: Path) -> dict[str, Any]:
    with _PREVIEW_FORCE_LOCK:
        token = _PREVIEW_FORCE_TOKEN
    bundle_path = bundle_path.resolve()
    entries: list[str] = []
    latest_ns = 0
    manifest_ns = None
    if bundle_path.is_dir():
        files = sorted(
            (path for path in bundle_path.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(bundle_path).as_posix(),
        )
        for path in files:
            stat = path.stat()
            rel = path.relative_to(bundle_path).as_posix()
            entries.append(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}")
            latest_ns = max(latest_ns, stat.st_mtime_ns)
            if rel == SHARE_MANIFEST_FILENAME:
                manifest_ns = stat.st_mtime_ns
    entries.append(f"manual:{token}")
    digest_input = "|".join(entries).encode("utf-8")
    signature = hashlib.sha256(digest_input).hexdigest() if entries else "0"
    payload: dict[str, Any] = {
        "signature": signature,
        "files_indexed": len(entries),
        "last_modified_ns": latest_ns or None,
        "manual_token": token,
    }
    if latest_ns:
        payload["last_modified_iso"] = datetime.fromtimestamp(latest_ns / 1_000_000_000, tz=timezone.utc).isoformat()
    if manifest_ns:
        payload["manifest_ns"] = manifest_ns
        payload["manifest_iso"] = datetime.fromtimestamp(manifest_ns / 1_000_000_000, tz=timezone.utc).isoformat()
    return payload


def _start_preview_server(bundle_path: Path, host: str, port: int) -> ThreadingHTTPServer:
    bundle_path = bundle_path.resolve()
    preview_reload_tag = (
        '<script type="module" src="./preview-reload.js" '
        'data-preview-only></script>'
    )

    class PreviewRequestHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(bundle_path), **kwargs)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            super().end_headers()

        def do_GET(self) -> None:
            if self.path.startswith("/__preview__/status"):
                payload = _collect_preview_status(bundle_path)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            request_path = self.path.partition("?")[0]
            if request_path in {"/viewer/", "/viewer/index.html"}:
                viewer_index = bundle_path / "viewer" / "index.html"
                if viewer_index.is_file():
                    html = viewer_index.read_text(encoding="utf-8")
                    if preview_reload_tag not in html:
                        if "</body>" in html:
                            html = html.replace(
                                "</body>",
                                f"  {preview_reload_tag}\n</body>",
                                1,
                            )
                        else:
                            html = f"{html}\n{preview_reload_tag}\n"
                    data = html.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            # Quiet common noisy requests in preview
            if self.path == "/favicon.ico" or self.path.endswith(".map") or self.path.startswith("/.well-known/"):
                # Return 204 No Content to avoid browser/server 404 noise
                self.send_response(204)
                self.end_headers()
                return
            return super().do_GET()

    server = ThreadingHTTPServer((host, port), PreviewRequestHandler)
    server.daemon_threads = True
    return server


def _share_update_configuration(
    stored: StoredExportConfig, projects: list[str] | None, scrub_preset_override: str | None,
    inline_threshold_override: int | None, detach_threshold_override: int | None,
    chunk_threshold_override: int | None, chunk_size_override: int | None,
) -> StoredExportConfig:
    scrub_preset = (scrub_preset_override or stored.scrub_preset or "standard").strip().lower()
    if scrub_preset not in VIEWER_SCRUB_PRESETS:
        console.print(
            "[red]Invalid scrub preset override:[/] "
            f"{scrub_preset}. Choose one of: {', '.join(VIEWER_SCRUB_PRESETS)}."
        )
        raise typer.Exit(code=1)
    config = StoredExportConfig(
        projects=list(projects) if projects else list(stored.projects),
        scrub_preset=scrub_preset,
        inline_threshold=inline_threshold_override if inline_threshold_override is not None else stored.inline_threshold,
        detach_threshold=detach_threshold_override if detach_threshold_override is not None else stored.detach_threshold,
        chunk_threshold=chunk_threshold_override if chunk_threshold_override is not None else stored.chunk_threshold,
        chunk_size=chunk_size_override if chunk_size_override is not None else stored.chunk_size,
    )
    _validate_share_thresholds(config)
    return config


def _validate_share_thresholds(config: StoredExportConfig) -> None:
    if config.inline_threshold < 0:
        console.print("[red]Inline threshold must be non-negative.[/]")
        raise typer.Exit(code=1)
    if config.detach_threshold < 0:
        console.print("[red]Detach threshold must be non-negative.[/]")
        raise typer.Exit(code=1)
    if config.chunk_threshold < 0:
        console.print("[red]Chunk threshold must be non-negative.[/]")
        raise typer.Exit(code=1)
    if config.chunk_size < 1024:
        console.print("[red]Chunk size must be at least 1024 bytes.[/]")
        raise typer.Exit(code=1)
    if config.detach_threshold <= config.inline_threshold:
        console.print("[yellow]Adjusting detach threshold to exceed inline threshold to avoid conflicts.[/]")
        config.detach_threshold = config.inline_threshold + max(1024, config.inline_threshold // 2 or 1)


def _print_updated_signature_status(bundle_path: Path, signing_key: Path | None, *, existing_signature: bool) -> None:
    if signing_key is not None or not existing_signature:
        return
    if (bundle_path / SHARE_SIGNATURE_FILENAME).exists():
        console.print(
            "[yellow]Existing manifest signature may no longer match. Re-run with --signing-key to refresh it.[/]"
        )
    else:
        console.print(
            "[yellow]Removed stale manifest.sig.json during update. Re-run with --signing-key to refresh the signature.[/]"
        )


def _encrypt_share_update(archive_path: Path | None, age_recipients: list[str]) -> None:
    if not age_recipients:
        return
    if not archive_path:
        console.print("[yellow]Skipped age encryption because --zip was not enabled.[/]")
        return
    console.print("[cyan]Encrypting archive with age...[/]")
    with _share_cli_step("[red]age encryption failed:[/] {error}"):
        encrypted_path = encrypt_bundle(archive_path, age_recipients)
        if encrypted_path:
            console.print(f"[green]✓ Encrypted archive written to {encrypted_path}[/]")


def _print_share_pruned_chunks(bundle_path: Path, sync_result: BundleSyncResult) -> None:
    console.print("[green]✓ Chunk manifest refreshed (mailbox.sqlite3.config.json updated).[/]")
    pruned = [path for path in sync_result.removed_files if path.is_relative_to(bundle_path / "chunks")]
    if pruned:
        console.print(f"[green]✓ Pruned {len(pruned)} stale chunk file(s) during bundle sync.[/]")


@share_app.command("update")
def share_update(
    bundle: Annotated[str, typer.Argument(help="Path to the existing bundle directory (e.g., your GitHub Pages repo).")],
    projects: Annotated[
        list[str] | None,
        typer.Option(
            "--project",
            "-p",
            help="Override project scope for this update (slugs or human keys). May be provided multiple times.",
        ),
    ] = None,
    inline_threshold_override: Annotated[
        Optional[int],
        typer.Option("--inline-threshold", help="Override inline attachment threshold (bytes).", min=0),
    ] = None,
    detach_threshold_override: Annotated[
        Optional[int],
        typer.Option("--detach-threshold", help="Override detach attachment threshold (bytes).", min=0),
    ] = None,
    chunk_threshold_override: Annotated[
        Optional[int],
        typer.Option("--chunk-threshold", help="Override chunking threshold (bytes).", min=0),
    ] = None,
    chunk_size_override: Annotated[
        Optional[int],
        typer.Option("--chunk-size", help="Override chunk size when chunking is enabled.", min=1024),
    ] = None,
    scrub_preset_override: Annotated[
        Optional[str],
        typer.Option(
            "--scrub-preset",
            help="Override scrub preset (standard, strict, ...).",
            case_sensitive=False,
        ),
    ] = None,
    zip_bundle: Annotated[
        bool,
        typer.Option("--zip/--no-zip", help="Package the updated bundle into a ZIP archive.", show_default=True),
    ] = False,
    signing_key: Annotated[Optional[Path], typer.Option("--signing-key", help="Path to Ed25519 signing key (32-byte seed).")]=None,
    signing_public_out: Annotated[Optional[Path], typer.Option("--signing-public-out", help="Write public key to this file after signing.")]=None,
    age_recipients: Annotated[
        Optional[list[str]],
        typer.Option(
            "--age-recipient",
            help="Encrypt the ZIP archive with age using the provided recipient(s). May be passed multiple times.",
        ),
    ] = None,
) -> None:
    """Refresh an existing static mailbox bundle using the previous export settings."""

    age_recipient_list = list(age_recipients or ())
    bundle_path = _resolve_path(bundle)
    if not bundle_path.exists() or not bundle_path.is_dir():
        console.print(f"[red]Bundle path {bundle_path} does not exist or is not a directory.[/]")
        raise typer.Exit(code=1)

    manifest_path = bundle_path / SHARE_MANIFEST_FILENAME
    if not manifest_path.exists():
        console.print(f"[red]manifest.json not found inside {bundle_path}. Are you sure this is a bundle directory?[/]")
        raise typer.Exit(code=1)

    with _share_cli_step("[red]Failed to load existing bundle configuration:[/] {error}"):
        stored_config = _load_bundle_export_config(bundle_path)
    config = _share_update_configuration(
        stored_config, projects, scrub_preset_override, inline_threshold_override,
        detach_threshold_override, chunk_threshold_override, chunk_size_override,
    )
    project_filters = config.projects
    scrub_preset = config.scrub_preset
    inline_threshold = config.inline_threshold
    detach_threshold = config.detach_threshold
    chunk_threshold = config.chunk_threshold
    chunk_size = config.chunk_size

    existing_signature = (bundle_path / SHARE_SIGNATURE_FILENAME).exists()

    console.rule("[bold]Static Mailbox Update[/bold]")

    with _share_cli_step("[red]Failed to resolve SQLite database: {error}[/]"):
        database_path = resolve_sqlite_database_path()

    console.print(f"[cyan]Using database:[/] {database_path}")

    hosting_hints = detect_hosting_hints(bundle_path)
    _print_hosting_hints(hosting_hints)

    chunk_manifest: Optional[dict[str, Any]] = None
    scope = None
    scrub_summary = None
    fts_enabled = False
    sync_result = BundleSyncResult()
    archive_path: Optional[Path] = None
    if zip_bundle:
        archive_path = bundle_path.parent / f"{bundle_path.name}.zip"
        if archive_path.exists():
            console.print(
                f"[red]Archive already exists at {archive_path}. Remove it or specify --no-zip to skip packaging.[/]"
            )
            raise typer.Exit(code=1)

    with tempfile.TemporaryDirectory(prefix="mailbox-share-update-") as temp_dir_name:
        temp_path = Path(temp_dir_name)
        snapshot_path = temp_path / MAILBOX_DATABASE_FILENAME
        console.print(f"[cyan]Creating snapshot:[/] {snapshot_path}")
        with _share_cli_step("[red]Snapshot preparation failed:[/] {error}"):
            snapshot_ctx = create_snapshot_context(
                source_database=database_path,
                snapshot_path=snapshot_path,
                project_filters=project_filters,
                scrub_preset=scrub_preset,
                purpose="viewer_export",
            )

        scope = snapshot_ctx.scope
        scrub_summary = snapshot_ctx.scrub_summary
        fts_enabled = snapshot_ctx.fts_enabled
        _print_snapshot_search_status(fts_enabled)

        settings = get_settings()
        storage_root = Path(settings.storage.root).expanduser()

        export_config = {
            "inline_threshold": inline_threshold,
            "detach_threshold": detach_threshold,
            "chunk_threshold": chunk_threshold,
            "chunk_size": chunk_size,
            "scrub_preset": scrub_preset,
            "projects": project_filters,
        }

        console.print("[cyan]Packaging attachments, viewer assets, and manifest...[/]")
        with _share_cli_step("[red]Failed to build bundle assets:[/] {error}"):
            bundle_artifacts = build_bundle_assets(
                snapshot_ctx.snapshot_path,
                temp_path,
                storage_root=storage_root,
                inline_threshold=inline_threshold,
                detach_threshold=detach_threshold,
                chunk_threshold=chunk_threshold,
                chunk_size=chunk_size,
                scope=scope,
                project_filters=project_filters,
                scrub_summary=scrub_summary,
                hosting_hints=hosting_hints,
                fts_enabled=fts_enabled,
                export_config=export_config,
            )
        chunk_manifest = bundle_artifacts.chunk_manifest
        if chunk_manifest:
            console.print(
                f"[cyan]Chunked database into {chunk_manifest['chunk_count']} files of ~{chunk_manifest['chunk_size']//1024} KiB.[/]"
            )

        if signing_key is not None:
            _sign_share_manifest(temp_path, signing_key, signing_public_out, overwrite=True)

        console.print(f"[cyan]Synchronizing updated bundle into:[/] {bundle_path}")
        with _share_cli_step("[red]Failed to synchronize bundle:[/] {error}"):
            sync_result = _copy_bundle_contents(temp_path, bundle_path)

        if archive_path is not None:
            console.print(f"[cyan]Packaging archive:[/] {archive_path}")
            with (
                _share_cli_step("[red]Failed to create ZIP archive:[/] {error}"),
                tempfile.TemporaryDirectory(prefix="mailbox-share-zip-stage-") as zip_stage_name,
            ):
                zip_stage_path = Path(zip_stage_name)
                _copy_bundle_contents(temp_path, zip_stage_path)
                package_directory_as_zip(zip_stage_path, archive_path)

    assert scope is not None and scrub_summary is not None

    _print_updated_signature_status(bundle_path, signing_key, existing_signature=existing_signature)
    _encrypt_share_update(archive_path, age_recipient_list)

    console.print("[green]✓ Updated SQLite snapshot for sharing.[/]")
    _print_share_snapshot_summary(snapshot_ctx, bundle_artifacts, inline_threshold, detach_threshold)
    if chunk_manifest:
        _print_share_pruned_chunks(bundle_path, sync_result)

    if zip_bundle and archive_path:
        console.print(f"[green]✓ Bundle archive available at {archive_path}[/]")


@dataclass
class _PreviewInputState:
    running: bool = True
    deployment_requested: bool = False

    def handle_key(self, key: str, interrupt_keys: tuple[str, str]) -> None:
        if key in interrupt_keys:
            raise KeyboardInterrupt
        command = key.lower()
        if command == "r":
            token = _bump_preview_force_token()
            console.print(f"[dim]Reload signal sent (token {token}).[/]")
        elif command == "d":
            self.deployment_requested = True
            self.running = False
        elif command == "q":
            self.running = False


def _run_posix_preview_input(thread: threading.Thread, state: _PreviewInputState) -> None:
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        while state.running and thread.is_alive():
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
            if ready:
                state.handle_key(sys.stdin.read(1), ("\x03", "\x04"))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _read_windows_preview_key(msvcrt: Any) -> str | None:
    getwch = getattr(msvcrt, "getwch", None)
    if getwch is not None:
        return getwch()
    getch = getattr(msvcrt, "getch", None)
    if getch is None:
        return None
    raw = getch()
    try:
        return raw.decode("utf-8", "ignore")
    except Exception:
        return str(raw)


def _poll_windows_preview_input(state: _PreviewInputState) -> None:
    import msvcrt

    while getattr(msvcrt, "kbhit", lambda: False)():
        key = _read_windows_preview_key(msvcrt)
        if key is None:
            break
        state.handle_key(key, ("\x03", "\x1a"))
        if not state.running:
            break


def _run_other_preview_input(thread: threading.Thread, state: _PreviewInputState) -> None:
    while state.running and thread.is_alive():
        time.sleep(0.5)
        if os.name == "nt":
            _poll_windows_preview_input(state)


@share_app.command("preview")
def share_preview(
    bundle: Annotated[str, typer.Argument(help="Path to the exported bundle directory.")],
    host: Annotated[str, typer.Option("--host", help="Host interface for the preview server.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port for the preview server.")] = 9000,
    open_browser: Annotated[
        bool,
        typer.Option("--open-browser/--no-open-browser", help="Automatically open the bundle in a browser."),
    ] = False,
) -> None:
    """Serve a static export bundle locally for inspection."""

    bundle_path = _resolve_path(bundle)
    if not bundle_path.exists() or not bundle_path.is_dir():
        console.print(f"[red]Bundle directory not found:[/] {bundle_path}")
        raise typer.Exit(code=1)

    # Ensure latest viewer assets are present (prefer source tree during dev)
    with suppress(Exception):
        copy_viewer_assets(bundle_path)

    server = _start_preview_server(bundle_path, host, port)
    actual_host, actual_port = server.server_address[:2]
    actual_host = actual_host or host

    console.rule("[bold]Static Bundle Preview[/bold]")
    console.print(f"Serving {bundle_path} at http://{actual_host}:{actual_port}/ (Ctrl+C to stop)")
    console.print("[dim]Commands: press 'r' to force refresh, 'd' to deploy now, 'q' to stop.[/]")

    if open_browser:
        with suppress(Exception):
            webbrowser.open(f"http://{actual_host}:{actual_port}/viewer/")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    input_state = _PreviewInputState()
    try:
        if os.name != "nt" and sys.stdin.isatty():
            _run_posix_preview_input(thread, input_state)
        else:
            _run_other_preview_input(thread, input_state)
    except KeyboardInterrupt:
        console.print("\n[dim]Shutting down preview server...[/]")
    finally:
        if not input_state.running:
            console.print("\n[dim]Stopping preview server (requested).[/]")
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        console.print("[green]Preview server stopped.[/]")
        if input_state.deployment_requested:
            # Special exit code indicates deployment requested by user from preview
            raise typer.Exit(code=42)


@share_app.command("verify")
def share_verify(
    bundle: Annotated[str, typer.Argument(help="Path to the exported bundle directory.")],
    public_key: Annotated[
        Optional[str],
        typer.Option(
            "--public-key",
            help="Ed25519 public key (base64) to verify signature. If omitted, uses key from manifest.sig.json.",
        ),
    ] = None,
) -> None:
    """Verify bundle integrity (SRI hashes) and optional Ed25519 signature."""
    from .share import verify_bundle

    bundle_path = _resolve_path(bundle)
    if not bundle_path.exists():
        console.print(f"[red]Bundle directory not found:[/] {bundle_path}")
        raise typer.Exit(code=1)
    if not bundle_path.is_dir():
        console.print(f"[red]Bundle path must be a directory:[/] {bundle_path}")
        raise typer.Exit(code=1)

    console.print(f"[cyan]Verifying bundle:[/] {bundle_path}")

    try:
        result = verify_bundle(bundle_path, public_key=public_key)
        console.print()
        console.print("[green]✓ Bundle verification passed[/]")
        console.print(f"  Bundle: {result['bundle']}")
        console.print(f"  SRI checked: {result['sri_checked']}")
        console.print(f"  Signature checked: {result['signature_checked']}")
        console.print(f"  Signature verified: {result['signature_verified']}")

        if not result["sri_checked"]:
            console.print("[yellow]  Warning: No SRI hashes found in manifest[/]")
        if not result["signature_checked"]:
            console.print("[yellow]  Warning: No signature found (manifest.sig.json)[/]")
        elif not result["signature_verified"]:
            # Without --public-key the only key available is the one travelling
            # inside the artifact being checked, so the check is circular. Say so
            # here: "Signature verified: False" on its own reads as a failure, and
            # the reader who shrugs it off is exactly the reader being attacked.
            console.print(
                "[yellow]  Warning: no --public-key given, so the signature was checked only"
                " against the key the bundle carries.[/]"
            )
            console.print(
                "[yellow]  That detects corruption, not forgery: whoever can rewrite the"
                " manifest can re-sign it with a key of their own and this still passes.[/]"
            )

    except ShareExportError as exc:
        console.print(f"[red]Verification failed:[/] {exc}")
        raise typer.Exit(code=1) from exc


@share_app.command("decrypt")
def share_decrypt(
    encrypted_path: Annotated[str, typer.Argument(help="Path to the age-encrypted file (e.g., bundle.zip.age).")],
    output: Annotated[
        Optional[str],
        typer.Option(
            "--output",
            "-o",
            help="Path where decrypted file should be written. Defaults to encrypted filename with .age removed.",
        ),
    ] = None,
    identity: Annotated[
        Optional[Path],
        typer.Option(
            "--identity",
            "-i",
            help="Path to age identity file (private key). Mutually exclusive with --passphrase.",
        ),
    ] = None,
    passphrase: Annotated[
        bool,
        typer.Option(
            "--passphrase",
            "-p",
            help="Prompt for passphrase interactively. Mutually exclusive with --identity.",
        ),
    ] = False,
) -> None:
    """Decrypt an age-encrypted bundle using identity file or passphrase."""
    from .share import decrypt_with_age

    enc_path = _resolve_path(encrypted_path)
    if not enc_path.exists():
        console.print(f"[red]Encrypted file not found:[/] {enc_path}")
        raise typer.Exit(code=1)
    if not enc_path.is_file():
        console.print(f"[red]Encrypted path must be a file, not a directory:[/] {enc_path}")
        raise typer.Exit(code=1)

    # Auto-determine output path if not provided
    if output is None:
        if enc_path.suffix == ".age":
            out_path = enc_path.with_suffix("")
        else:
            out_path = enc_path.parent / f"{enc_path.stem}_decrypted{enc_path.suffix}"
    else:
        out_path = _resolve_path(output)

    passphrase_text: Optional[str] = None
    if passphrase:
        import getpass

        passphrase_text = getpass.getpass("Enter passphrase: ")
        if not passphrase_text:
            console.print("[red]Passphrase cannot be empty[/]")
            raise typer.Exit(code=1)

    console.print(f"[cyan]Decrypting:[/] {enc_path} → {out_path}")

    try:
        decrypt_with_age(
            enc_path,
            out_path,
            identity=identity,
            passphrase=passphrase_text,
        )
        console.print(f"[green]✓ Decrypted successfully to {out_path}[/]")
    except ShareExportError as exc:
        console.print(f"[red]Decryption failed:[/] {exc}")
        raise typer.Exit(code=1) from exc


@share_app.command("wizard")
def share_wizard() -> None:
    """Launch interactive deployment wizard for GitHub Pages or Cloudflare Pages."""
    console.print("[cyan]Launching deployment wizard...[/]")

    # Import and run the wizard script
    import subprocess
    import sys

    # Try to find wizard script - first check if running from source
    wizard_script = Path(__file__).parent.parent.parent / "scripts" / "share_to_github_pages.py"

    if not wizard_script.exists():
        # If not in source tree, check if it's in the same directory as this module (for editable installs)
        alt_path = Path(__file__).parent / "scripts" / "share_to_github_pages.py"
        if alt_path.exists():
            wizard_script = alt_path
        else:
            console.print("[red]Wizard script not found.[/]")
            console.print("[yellow]Expected locations:[/]")
            console.print(f"  • {wizard_script}")
            console.print(f"  • {alt_path}")
            console.print("\n[yellow]This command only works when running from source.[/]")
            console.print("[cyan]Run the wizard directly:[/] python scripts/share_to_github_pages.py")
            raise typer.Exit(code=1)

    try:
        # Run the wizard script as a subprocess so it can handle its own console interactions
        result = subprocess.run([sys.executable, str(wizard_script)], check=False)
        raise typer.Exit(code=result.returncode)
    except KeyboardInterrupt:
        console.print("\n[yellow]Wizard cancelled by user[/]")
        raise typer.Exit(code=0) from None


def _resolve_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    path = (Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve()
    return path


@dataclass(slots=True)
class StoredExportConfig:
    projects: list[str]
    inline_threshold: int
    detach_threshold: int
    chunk_threshold: int
    chunk_size: int
    scrub_preset: str


@dataclass(slots=True)
class BundleSyncResult:
    removed_files: tuple[Path, ...] = ()
    removed_dirs: tuple[Path, ...] = ()


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _load_bundle_export_config(bundle_dir: Path) -> StoredExportConfig:
    manifest_path = bundle_dir / SHARE_MANIFEST_FILENAME
    if not manifest_path.exists():
        raise ShareExportError(f"manifest.json not found in {bundle_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShareExportError(f"Failed to parse manifest.json: {exc}") from exc

    export_config = manifest.get("export_config", {}) or {}
    attachments_section = manifest.get("attachments", {}) or {}
    attachments_config = attachments_section.get("config", {}) or {}
    project_scope = manifest.get("project_scope", {}) or {}
    scrub_section = manifest.get("scrub", {}) or {}
    database_section = manifest.get("database", {}) or {}

    raw_projects = export_config.get("projects", project_scope.get("requested", []))
    projects = [str(p) for p in raw_projects if isinstance(p, str)]

    scrub_preset = str(export_config.get("scrub_preset") or scrub_section.get("preset") or "standard")

    inline_threshold = _coerce_int(
        _coalesce(export_config.get("inline_threshold"), attachments_config.get("inline_threshold")),
        INLINE_ATTACHMENT_THRESHOLD,
    )
    detach_threshold = _coerce_int(
        _coalesce(export_config.get("detach_threshold"), attachments_config.get("detach_threshold")),
        DETACH_ATTACHMENT_THRESHOLD,
    )
    chunk_threshold = _coerce_int(export_config.get("chunk_threshold"), DEFAULT_CHUNK_THRESHOLD)

    chunk_manifest = database_section.get("chunk_manifest") or {}
    chunk_size = _coerce_int(
        _coalesce(export_config.get("chunk_size"), chunk_manifest.get("chunk_size")),
        DEFAULT_CHUNK_SIZE,
    )

    chunk_config: dict[str, Any] = {}
    chunk_config_path = bundle_dir / "mailbox.sqlite3.config.json"
    if chunk_config_path.exists():
        try:
            chunk_config = json.loads(chunk_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            chunk_config = {}
    if chunk_config:
        chunk_size = _coerce_int(chunk_config.get("chunk_size"), chunk_size)
        chunk_threshold = _coerce_int(chunk_config.get("threshold_bytes"), chunk_threshold)

    return StoredExportConfig(
        projects=projects,
        inline_threshold=inline_threshold,
        detach_threshold=detach_threshold,
        chunk_threshold=chunk_threshold,
        chunk_size=chunk_size,
        scrub_preset=scrub_preset,
    )


def _is_bundle_link(path: Path) -> bool:
    """Return whether *path* is a symlink or a Windows junction."""

    if path.is_symlink():
        return True
    junction_check = getattr(path, "is_junction", None)
    return bool(junction_check is not None and junction_check())


def _require_owned_bundle_type(path: Path, mode: int, bundle_root: Path, role: str, *, directory: bool) -> None:
    relative = path.relative_to(bundle_root).as_posix()
    if _is_bundle_link(path):
        raise ShareExportError(f"Refusing to follow {role} bundle link at {relative}")
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(mode):
        kind = "a directory" if directory else "a regular file"
        raise ShareExportError(f"Expected {role} bundle path {relative} to be {kind}")


def _collect_owned_bundle_tree(tree_root: Path, bundle_root: Path, role: str) -> tuple[set[Path], set[Path]]:
    owned_files: set[Path] = set()
    owned_dirs: set[Path] = {tree_root}
    for current_root, dirnames, filenames in os.walk(tree_root, topdown=True, followlinks=False):
        current_path = Path(current_root)
        for child_name in dirnames:
            child = current_path / child_name
            _require_owned_bundle_type(child, child.lstat().st_mode, bundle_root, role, directory=True)
            owned_dirs.add(child)
        for child_name in filenames:
            child = current_path / child_name
            _require_owned_bundle_type(child, child.lstat().st_mode, bundle_root, role, directory=False)
            owned_files.add(child)
    return owned_files, owned_dirs


def _collect_owned_bundle_paths(
    bundle_root: Path,
    *,
    role: str,
) -> tuple[set[Path], set[Path]]:
    """Inventory regular files and directories owned by the share exporter."""

    owned_files: set[Path] = set()
    owned_dirs: set[Path] = set()

    for filename in _SHARE_BUNDLE_OWNED_FILES:
        path = bundle_root / filename
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        _require_owned_bundle_type(path, mode, bundle_root, role, directory=False)
        owned_files.add(path)

    for dirname in _SHARE_BUNDLE_OWNED_DIRECTORIES:
        tree_root = bundle_root / dirname
        try:
            mode = tree_root.lstat().st_mode
        except FileNotFoundError:
            continue
        _require_owned_bundle_type(tree_root, mode, bundle_root, role, directory=True)
        tree_files, tree_dirs = _collect_owned_bundle_tree(tree_root, bundle_root, role)
        owned_files.update(tree_files)
        owned_dirs.update(tree_dirs)

    return owned_files, owned_dirs


def _prepare_bundle_sync_roots(source: Path, destination: Path) -> None:
    try:
        source_mode = source.lstat().st_mode
    except FileNotFoundError as exc:
        raise ShareExportError(f"Source bundle directory does not exist: {source}") from exc
    if _is_bundle_link(source) or not stat.S_ISDIR(source_mode):
        raise ShareExportError(
            f"Source bundle root must be a real directory, not a link: {source}"
        )

    try:
        destination_mode = destination.lstat().st_mode
    except FileNotFoundError:
        destination.mkdir(parents=True, exist_ok=False)
    else:
        if _is_bundle_link(destination) or not stat.S_ISDIR(destination_mode):
            raise ShareExportError(
                "Destination bundle root must be a real directory, not a link: "
                f"{destination}"
            )


def _validate_bundle_sync_targets(source: Path, destination: Path, source_files: set[Path], desired_dirs: set[Path]) -> None:
    for desired_dir in sorted(desired_dirs, key=lambda path: len(path.parts)):
        try:
            mode = desired_dir.lstat().st_mode
        except FileNotFoundError:
            continue
        if _is_bundle_link(desired_dir) or not stat.S_ISDIR(mode):
            relative = desired_dir.relative_to(destination).as_posix()
            raise ShareExportError(
                f"Cannot replace non-directory destination bundle path {relative}"
            )

    for source_file in source_files:
        relative = source_file.relative_to(source)
        destination_file = destination / relative
        try:
            mode = destination_file.lstat().st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None and (
            _is_bundle_link(destination_file) or not stat.S_ISREG(mode)
        ):
            raise ShareExportError(
                "Cannot replace non-file destination bundle path "
                f"{relative.as_posix()}"
            )
        if _is_bundle_link(source_file):
            raise ShareExportError(
                f"Refusing to follow source bundle link at {relative.as_posix()}"
            )


def _copy_bundle_contents(source: Path, destination: Path) -> BundleSyncResult:
    """Refresh exporter-owned paths while preserving the hosting repository."""

    source = source.expanduser().absolute()
    destination = destination.expanduser().absolute()
    _prepare_bundle_sync_roots(source, destination)
    source_files, source_dirs = _collect_owned_bundle_paths(source, role="source")
    existing_files, existing_dirs = _collect_owned_bundle_paths(destination, role="destination")
    manifest_source = source / SHARE_MANIFEST_FILENAME
    if manifest_source not in source_files:
        raise ShareExportError("Fresh bundle is missing required manifest.json")
    desired_files = {destination / source_file.relative_to(source) for source_file in source_files}
    desired_dirs = {destination / source_dir.relative_to(source) for source_dir in source_dirs}

    # Validate every destination before mutating it. The existing manifest is
    # retained until all refreshed assets have landed and stale owned files
    # have been pruned, so readers never observe a manifest for partial data.
    _validate_bundle_sync_targets(source, destination, source_files, desired_dirs)

    for desired_dir in sorted(desired_dirs, key=lambda path: len(path.parts)):
        desired_dir.mkdir(exist_ok=True)

    for source_file in sorted(source_files - {manifest_source}):
        relative = source_file.relative_to(source)
        shutil.copy2(source_file, destination / relative)

    stale_files = tuple(sorted(existing_files - desired_files))
    for stale_file in stale_files:
        stale_file.unlink()

    removed_dirs: list[Path] = []
    for stale_dir in sorted(
        existing_dirs - desired_dirs,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        with suppress(OSError):
            stale_dir.rmdir()
            removed_dirs.append(stale_dir)

    shutil.copy2(manifest_source, destination / SHARE_MANIFEST_FILENAME)

    return BundleSyncResult(
        removed_files=stale_files,
        removed_dirs=tuple(removed_dirs),
    )


def _looks_like_repository_root(candidate: Path) -> bool:
    """Report whether ``candidate`` is a Git working tree, not merely `.git`-adjacent.

    ``(candidate / ".git").exists()`` is not that question, and the difference is not
    academic: an EMPTY DIRECTORY named ``.git`` satisfies it. One appeared in ``/tmp`` on
    a developer machine, and from then on every ``archive save`` run from a temporary
    directory wrote the operator's mailbox archive into ``/tmp/archived_mailbox_states``
    instead of the intended root -- silently, with a success message naming the wrong path.

    Git's own criterion is used instead: a repository directory contains ``HEAD``, and a
    worktree or submodule is a ``.git`` FILE holding a ``gitdir:`` pointer. ``_resolve_git_dir``
    already understands both shapes, so this only adds the check it does not make.
    """
    git_dir = _resolve_git_dir(candidate)
    return git_dir is not None and (git_dir / "HEAD").exists()


def _detect_project_root() -> Path:
    cwd = Path.cwd().resolve()
    candidates = [cwd, *cwd.parents]
    pyproject_candidate: Path | None = None
    for candidate in candidates:
        if _looks_like_repository_root(candidate):
            return candidate
        if pyproject_candidate is None and (candidate / "pyproject.toml").exists():
            pyproject_candidate = candidate
    if pyproject_candidate is not None:
        return pyproject_candidate
    return cwd


def _archive_states_dir(*, create: bool) -> Path:
    root = _detect_project_root()
    archive_dir = root / ARCHIVE_DIR_NAME
    if create:
        archive_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            archive_dir.chmod(0o700)
    return archive_dir


def _package_version() -> str:
    """Thin alias kept for existing callers; the lookup lives in utils."""
    return package_version()


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    current = float(max(value, 0))
    for unit in units:
        if current < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(current)} {unit}"
            return f"{current:.1f} {unit}"
        current /= 1024.0
    return f"{int(value)} B"


def _resolve_git_dir(repo_path: Path) -> Path | None:
    git_entry = repo_path / ".git"
    if git_entry.is_dir():
        return git_entry
    if not git_entry.is_file():
        return None
    try:
        contents = git_entry.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not contents.lower().startswith("gitdir:"):
        return None
    git_dir_raw = contents.split(":", 1)[1].strip()
    if not git_dir_raw:
        return None
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = (repo_path / git_dir).resolve()
    return git_dir


def _resolve_common_git_dir(git_dir: Path) -> Path:
    common_dir_file = git_dir / "commondir"
    if not common_dir_file.exists():
        return git_dir
    try:
        common_dir_raw = common_dir_file.read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir
    if not common_dir_raw:
        return git_dir
    common_dir = Path(common_dir_raw)
    if not common_dir.is_absolute():
        common_dir = (git_dir / common_dir).resolve()
    return common_dir


def _read_git_reference(common_git_dir: Path, ref_name: str) -> str | None:
    ref_path = common_git_dir / ref_name
    if ref_path.exists():
        with suppress(OSError):
            return ref_path.read_text(encoding="utf-8").strip()
    packed_refs = common_git_dir / "packed-refs"
    if not packed_refs.exists():
        return None
    with suppress(OSError):
        for line in packed_refs.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            commit, ref = line.split(" ", 1)
            if ref.strip() == ref_name:
                return commit.strip()
    return None


def _detect_git_head(repo_path: Path) -> str | None:
    git_dir = _resolve_git_dir(repo_path)
    if git_dir is None:
        return None
    common_git_dir = _resolve_common_git_dir(git_dir)
    try:
        head_contents = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head_contents:
        return None
    if head_contents.startswith("ref:"):
        return _read_git_reference(common_git_dir, head_contents.split(" ", 1)[1].strip())
    return head_contents


def _compose_archive_basename(
    *,
    timestamp: datetime,
    project_filters: Sequence[str],
    scrub_preset: str,
    label: str | None,
) -> str:
    ts_segment = timestamp.strftime("%Y%m%d-%H%M%SZ")
    projects_segment = "-".join(slugify(value) for value in project_filters) if project_filters else "all-projects"
    preset_segment = slugify(scrub_preset)
    segments = ["mailbox-state", ts_segment, projects_segment, preset_segment]
    if label:
        segments.append(slugify(label))
    return "-".join(seg for seg in segments if seg)


def _ensure_unique_archive_path(base_dir: Path, base_name: str) -> Path:
    candidate = base_dir / f"{base_name}.zip"
    counter = 1
    while candidate.exists():
        candidate = base_dir / f"{base_name}-{counter:02d}.zip"
        counter += 1
    return candidate


def _validate_archive_storage_child(child: Path, source_dir: Path, *, directory: bool) -> None:
    relative = child.relative_to(source_dir).as_posix()
    child_mode = child.lstat().st_mode
    if _is_bundle_link(child):
        raise ShareExportError(f"Recovery archive refuses storage link at {relative}")
    is_expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not is_expected_kind(child_mode):
        expected_kind = "a directory" if directory else "a regular file"
        raise ShareExportError(f"Recovery archive expected {expected_kind} at {relative}")


def _collect_archive_storage_paths(
    source_dir: Path,
) -> tuple[Path, tuple[Path, ...], tuple[Path, ...]]:
    """Validate a recovery-archive tree without following links."""

    source_dir = source_dir.expanduser().absolute()
    try:
        source_mode = source_dir.lstat().st_mode
    except FileNotFoundError as exc:
        raise ShareExportError(
            f"Storage root {source_dir} does not exist; nothing to archive."
        ) from exc
    if _is_bundle_link(source_dir) or not stat.S_ISDIR(source_mode):
        raise ShareExportError(
            f"Storage root must be a real directory, not a link: {source_dir}"
        )

    directories: list[Path] = []
    files: list[Path] = []
    for current_root, dirnames, filenames in os.walk(
        source_dir,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current_root)
        for child_name in dirnames:
            child = current_path / child_name
            _validate_archive_storage_child(child, source_dir, directory=True)
            directories.append(child)
        for child_name in filenames:
            child = current_path / child_name
            _validate_archive_storage_child(child, source_dir, directory=False)
            files.append(child)

    return source_dir, tuple(sorted(directories)), tuple(sorted(files))


def _write_directory_to_zip(zip_file: ZipFile, source_dir: Path, arc_prefix: Path) -> None:
    source_dir, directories, files = _collect_archive_storage_paths(source_dir)
    prefix = arc_prefix.as_posix().rstrip("/") + "/"
    zip_file.writestr(prefix, b"")
    for path in directories:
        arcname = (arc_prefix / path.relative_to(source_dir)).as_posix()
        zip_file.writestr(arcname.rstrip("/") + "/", b"")
    for path in files:
        relative = path.relative_to(source_dir).as_posix()
        path_mode = path.lstat().st_mode
        if _is_bundle_link(path) or not stat.S_ISREG(path_mode):
            raise ShareExportError(
                f"Recovery archive storage path changed during packaging: {relative}"
            )
        arcname = (arc_prefix / path.relative_to(source_dir)).as_posix()
        zip_file.write(path, arcname=arcname)


def _load_archive_metadata(zip_path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        with ZipFile(zip_path, "r") as archive, archive.open(ARCHIVE_METADATA_FILENAME) as meta_file:
            data = json.loads(meta_file.read().decode("utf-8"))
            return cast(dict[str, Any], data), None
    except KeyError:
        return {}, f"{ARCHIVE_METADATA_FILENAME} missing"
    except (BadZipFile, OSError, json.JSONDecodeError) as exc:
        return {}, f"Invalid metadata: {exc}"


def _resolve_archive_path(candidate: Path | str) -> Path:
    path = Path(candidate)
    if path.exists():
        return path.resolve()
    archive_dir = _archive_states_dir(create=False)
    fallback = archive_dir / path.name
    if fallback.exists():
        return fallback.resolve()
    raise FileNotFoundError(f"Archive '{candidate}' not found (checked {path} and {fallback}).")


def _next_backup_path(path: Path, timestamp: str) -> Path:
    base = path.with_name(f"{path.name}.backup-{timestamp}")
    candidate = base
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.backup-{timestamp}-{counter:02d}")
        counter += 1
    return candidate


def _resolve_archive_member_path(destination_root: Path, member_name: str) -> Path:
    normalized_name = member_name.rstrip("/")
    if not normalized_name:
        raise ShareExportError("Archive contains an empty member name.")
    if "\\" in normalized_name:
        raise ShareExportError(
            f"Invalid archive member path {member_name!r}: backslashes are not allowed."
        )

    relative_path = PurePosixPath(normalized_name)
    if relative_path.is_absolute() or any(part == ".." for part in relative_path.parts):
        raise ShareExportError(
            f"Invalid archive member path {member_name!r}: directory traversal is not allowed."
        )

    candidate = (destination_root / Path(*relative_path.parts)).resolve()
    try:
        candidate.relative_to(destination_root)
    except ValueError as exc:
        raise ShareExportError(
            f"Invalid archive member path {member_name!r}: directory traversal is not allowed."
        ) from exc
    return candidate


def _extract_archive_safely(zip_path: Path, destination_root: Path) -> None:
    destination_root = destination_root.resolve()
    try:
        with ZipFile(zip_path, "r") as archive:
            for member in archive.infolist():
                target_path = _resolve_archive_member_path(destination_root, member.filename)
                if member.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target_path.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
    except BadZipFile as exc:
        raise ShareExportError(f"Failed to read archive {zip_path}: {exc}") from exc
    except OSError as exc:
        raise ShareExportError(f"Failed to extract archive {zip_path}: {exc}") from exc


def _remove_restore_target(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _rollback_archive_restore(
    *,
    database_path: Path,
    storage_root: Path,
    db_backup: Optional[Path],
    sidecar_backups: Sequence[tuple[Path, Path]],
    storage_backup: Optional[Path],
) -> list[str]:
    rollback_errors: list[str] = []

    def _copy_back(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=False)
        else:
            shutil.copy2(source, destination)

    targets_to_clear = [database_path, storage_root, *(target for target, _backup in sidecar_backups)]
    for target in targets_to_clear:
        try:
            _remove_restore_target(target)
        except OSError as exc:
            rollback_errors.append(f"Failed to clear partial restore target {target}: {exc}")

    restore_pairs: list[tuple[Path, Path]] = []
    if db_backup is not None:
        restore_pairs.append((db_backup, database_path))
    restore_pairs.extend((backup, target) for target, backup in sidecar_backups)
    if storage_backup is not None:
        restore_pairs.append((storage_backup, storage_root))

    for source, destination in restore_pairs:
        try:
            _copy_back(source, destination)
        except OSError as exc:
            rollback_errors.append(f"Failed to restore {destination} from backup {source}: {exc}")

    return rollback_errors


def _create_mailbox_archive(
    *,
    project_filters: Sequence[str],
    scrub_preset: str,
    label: str | None,
    status_message: str = "Creating mailbox archive...",
) -> tuple[Path, dict[str, Any]]:
    settings = get_settings()
    database_path = resolve_sqlite_database_path(settings.database.url)
    storage_root = _resolve_path(settings.storage.root)
    if not storage_root.exists():
        raise ShareExportError(f"Storage root {storage_root} does not exist; cannot archive.")
    archive_dir = _archive_states_dir(create=True)
    timestamp = datetime.now(timezone.utc).replace(microsecond=0)
    base_name = _compose_archive_basename(
        timestamp=timestamp,
        project_filters=project_filters,
        scrub_preset=scrub_preset,
        label=label,
    )
    destination = _ensure_unique_archive_path(archive_dir, base_name)
    status_ctx = console.status(status_message) if status_message else nullcontext()
    with status_ctx, tempfile.TemporaryDirectory(prefix="mailbox-archive-") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        if os.name == "posix":
            temp_dir.chmod(0o700)
        snapshot_path = temp_dir / ARCHIVE_SNAPSHOT_RELATIVE.name
        context = create_snapshot_context(
            source_database=database_path,
            snapshot_path=snapshot_path,
            project_filters=project_filters,
            scrub_preset=scrub_preset,
            purpose="recovery_archive",
        )
        metadata: dict[str, Any] = {
            "version": 1,
            "created_at": timestamp.isoformat(),
            "archive": {
                "filename": destination.name,
                "directory": str(destination.parent),
            },
            "projects_requested": list(project_filters),
            "projects_included": [
                {"slug": record.slug, "human_key": record.human_key}
                for record in context.scope.projects
            ],
            "projects_removed": context.scope.removed_count,
            "scrub_preset": scrub_preset,
            "scrub_summary": asdict(context.scrub_summary),
            "fts_enabled": context.fts_enabled,
            "database": {
                "source_path": str(database_path),
                "snapshot": ARCHIVE_SNAPSHOT_RELATIVE.as_posix(),
                "size_bytes": snapshot_path.stat().st_size,
            },
            "storage": {
                "source_path": str(storage_root),
                "git_head": _detect_git_head(storage_root),
                "archive_dir": ARCHIVE_STORAGE_DIRNAME.as_posix(),
            },
            "label": label or "",
            "tooling": {
                "package": "mcp-agent-mail",
                "version": _package_version(),
                "python": sys.version.split()[0],
            },
            "notes": [
                "Restore with `mcp-agent-mail archive restore {filename}`".format(filename=destination.name)
            ],
        }
        temp_zip_path = temp_dir / "mailbox-state.zip"
        if os.name == "posix":
            descriptor = os.open(
                temp_zip_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.close(descriptor)
        with ZipFile(temp_zip_path, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr(
                ARCHIVE_METADATA_FILENAME,
                json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"),
            )
            archive.write(snapshot_path, arcname=ARCHIVE_SNAPSHOT_RELATIVE.as_posix())
            _write_directory_to_zip(archive, storage_root, ARCHIVE_STORAGE_DIRNAME)
        if os.name == "posix":
            temp_zip_path.chmod(0o600)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(temp_zip_path), str(destination))
        if os.name == "posix":
            destination.chmod(0o600)
    return destination, metadata


@archive_app.command(
    "save",
    help="Create a lossless ZIP that captures the SQLite snapshot and storage repo (default preset keeps ack/read state).",
)
def archive_save_state(
    label: Annotated[
        Optional[str],
        typer.Option("--label", "-l", help="Optional label appended to the archive filename (e.g., nightly, pre-reset)."),
    ] = None,
) -> None:
    try:
        archive_path, metadata = _create_mailbox_archive(
            project_filters=(),
            scrub_preset=DEFAULT_ARCHIVE_SCRUB_PRESET,
            label=label,
            status_message="Creating mailbox archive...",
        )
    except ShareExportError as exc:
        console.print(f"[red]Failed to create mailbox archive:[/] {exc}")
        raise typer.Exit(code=1) from exc
    size_bytes = archive_path.stat().st_size if archive_path.exists() else 0
    projects_desc = metadata.get("projects_requested") or ["all"]
    console.print(f"[green]✓ Mailbox state saved to:[/] {archive_path}")
    console.print(
        f"[dim]Preset:[/] {metadata.get('scrub_preset', DEFAULT_ARCHIVE_SCRUB_PRESET)} | [dim]Projects:[/] {', '.join(projects_desc)} | [dim]Size:[/] {_format_bytes(size_bytes)}"
    )
    console.print(f"[dim]Restore later with:[/] mcp-agent-mail archive restore {archive_path.name}")


@archive_app.command(
    "list",
    help="Show saved mailbox states (with metadata) from the archived_mailbox_states directory.",
)
def archive_list_states(
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", min=0, help="Show only the most recent N archives."),
    ] = 0,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table")] = False,
) -> None:
    archive_dir = _archive_states_dir(create=False)
    if not archive_dir.exists():
        if json_output:
            typer.echo("[]")
            return
        console.print(f"[yellow]Archive directory {archive_dir} does not exist yet.[/]")
        raise typer.Exit(code=0)
    files = sorted(archive_dir.glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        if json_output:
            typer.echo("[]")
            return
        console.print(f"[yellow]No saved mailbox states found under {archive_dir}.[/]")
        raise typer.Exit(code=0)
    if limit > 0:
        files = files[:limit]
    entries: list[dict[str, Any]] = []
    for file_path in files:
        metadata, error = _load_archive_metadata(file_path)
        entry = {
            "file": file_path.name,
            "path": str(file_path),
            "size_bytes": file_path.stat().st_size,
            "created_at": metadata.get("created_at")
            or datetime.fromtimestamp(file_path.stat().st_mtime, timezone.utc).isoformat(),
            "scrub_preset": metadata.get("scrub_preset", ""),
            "projects": metadata.get("projects_requested") or ["all"],
        }
        if error:
            entry["error"] = error
        entries.append(entry)
    if json_output:
        json.dump(entries, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    table = Table(title="Saved Mailbox States", show_lines=False)
    table.add_column("File")
    table.add_column("Created (UTC)")
    table.add_column("Size")
    table.add_column("Preset")
    table.add_column("Projects")
    table.add_column("Notes")
    for entry in entries:
        notes = entry.get("error", "")
        table.add_row(
            entry["file"],
            entry["created_at"],
            _format_bytes(int(entry["size_bytes"])),
            entry.get("scrub_preset", ""),
            ", ".join(entry.get("projects", [])),
            notes,
        )
    console.print(table)
    console.print(f"[dim]Archives live under {archive_dir}. Restore with `mcp-agent-mail archive restore <file>`.[/]")


def _archive_restore_plan(database_path: Path, storage_root: Path, timestamp: str) -> list[str]:
    planned_ops: list[str] = []
    paths = [database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm"), storage_root]
    for path in paths:
        if path.exists():
            planned_ops.append(f"backup {path} -> {_next_backup_path(path, timestamp)}")
    planned_ops.append(f"restore snapshot -> {database_path}")
    planned_ops.append(f"restore storage repo -> {storage_root}")
    return planned_ops


def _confirm_archive_restore(planned_ops: list[str], *, dry_run: bool, force: bool) -> bool:
    if dry_run:
        console.print("[cyan]Dry-run plan:[/]")
        for op in planned_ops:
            console.print(f"  • {op}")
        return False
    if not force:
        console.print("[yellow]The following operations will be performed:[/]")
        for op in planned_ops:
            console.print(f"  • {op}")
        if not typer.confirm("Proceed with restore?", default=False):
            raise typer.Exit(code=1)
    return True


def _report_archive_restore_failure(exc: OSError, rollback_errors: list[str]) -> None:
    console.print(f"[red]Restore failed:[/] {exc}")
    if rollback_errors:
        console.print("[yellow]Rollback encountered issues:[/]")
        for error in rollback_errors:
            console.print(f"  • {error}")
    else:
        console.print("[yellow]Original database and storage were restored from backups.[/]")


def _apply_archive_restore(
    snapshot_src: Path, storage_src: Path, database_path: Path, storage_root: Path, timestamp: str,
) -> list[Path]:
    backup_paths: list[Path] = []
    db_backup: Optional[Path] = None
    sidecar_backups: list[tuple[Path, Path]] = []
    storage_backup: Optional[Path] = None
    if database_path.exists():
        db_backup = _next_backup_path(database_path, timestamp)
        shutil.move(str(database_path), str(db_backup))
        backup_paths.append(db_backup)
    for suffix in ("-wal", "-shm"):
        wal_path = Path(f"{database_path}{suffix}")
        if wal_path.exists():
            wal_backup = _next_backup_path(wal_path, timestamp)
            shutil.move(str(wal_path), str(wal_backup))
            backup_paths.append(wal_backup)
            sidecar_backups.append((wal_path, wal_backup))
    if storage_root.exists():
        storage_backup = _next_backup_path(storage_root, timestamp)
        shutil.move(str(storage_root), str(storage_backup))
        backup_paths.append(storage_backup)
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot_src, database_path)
        storage_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(storage_src, storage_root, dirs_exist_ok=False)
    except OSError as exc:
        rollback_errors = _rollback_archive_restore(
            database_path=database_path,
            storage_root=storage_root,
            db_backup=db_backup,
            sidecar_backups=sidecar_backups,
            storage_backup=storage_backup,
        )
        _report_archive_restore_failure(exc, rollback_errors)
        raise typer.Exit(code=1) from exc
    return backup_paths


@archive_app.command(
    "restore",
    help="Restore a previously saved mailbox state. Existing DB/storage are backed up automatically.",
)
def archive_restore_state(
    archive_file: Annotated[Path, typer.Argument(help="Path or filename of the saved state zip file.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Apply the archive even if backups already exist (still keeps safety backups, just skips the prompt).",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the planned backup/restore steps without touching files (useful for audits).",
        ),
    ] = False,
) -> None:
    try:
        archive_path = _resolve_archive_path(archive_file)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    metadata, meta_error = _load_archive_metadata(archive_path)
    if meta_error:
        console.print(f"[yellow]Warning:[/] {meta_error}")
    try:
        database_path = resolve_sqlite_database_path()
    except ShareExportError as exc:
        console.print(f"[red]Failed to resolve target database path:[/] {exc}")
        raise typer.Exit(code=1) from exc
    settings = get_settings()
    storage_root = _resolve_path(settings.storage.root)
    archive_db_path = metadata.get("database", {}).get("source_path")
    archive_storage_path = metadata.get("storage", {}).get("source_path")
    if archive_db_path and archive_db_path != str(database_path):
        console.print(
            f"[yellow]Archive was created from database {archive_db_path}, current config is {database_path}. Continuing...[/]"
        )
    if archive_storage_path and archive_storage_path != str(storage_root):
        console.print(
            f"[yellow]Archive used storage root {archive_storage_path}, current config is {storage_root}. Continuing...[/]"
        )
    with tempfile.TemporaryDirectory(prefix="mailbox-restore-") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        try:
            _extract_archive_safely(archive_path, temp_dir)
        except ShareExportError as exc:
            console.print(f"[red]Failed to extract archive:[/] {exc}")
            raise typer.Exit(code=1) from exc
        snapshot_src = temp_dir / ARCHIVE_SNAPSHOT_RELATIVE
        storage_src = temp_dir / ARCHIVE_STORAGE_DIRNAME
        if not snapshot_src.exists():
            console.print(f"[red]Snapshot missing inside archive ({ARCHIVE_SNAPSHOT_RELATIVE}).[/]")
            raise typer.Exit(code=1)
        if not storage_src.exists():
            console.print(f"[red]Storage repository missing inside archive ({ARCHIVE_STORAGE_DIRNAME}).[/]")
            raise typer.Exit(code=1)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        planned_ops = _archive_restore_plan(database_path, storage_root, timestamp)
        if not _confirm_archive_restore(planned_ops, dry_run=dry_run, force=force):
            return
        backup_paths = _apply_archive_restore(snapshot_src, storage_src, database_path, storage_root, timestamp)
    console.print(f"[green]✓ Restore complete from {archive_path}.[/]")
    if backup_paths:
        console.print("[dim]Backups preserved at:[/]")
        for path in backup_paths:
            console.print(f"  • {path}")
    console.print(
        f"[dim]Database:[/] {database_path}\n[dim]Storage root:[/] {storage_root}\n[dim]Need to revert? Use the backups above or rerun with another archive."
    )


def _reset_database_files(db_url: str) -> list[Path]:
    database_files: list[Path] = []
    try:
        url = make_url(db_url)
        if url.get_backend_name().startswith("sqlite"):
            database = url.database or ""
            if not database:
                console.print("[yellow]Warning:[/] SQLite database path is empty; nothing to delete.")
            else:
                db_path = _resolve_path(database)
                database_files.append(db_path)
                database_files.append(Path(f"{db_path}-wal"))
                database_files.append(Path(f"{db_path}-shm"))
    except Exception as exc:  # pragma: no cover - defensive
        console.print(f"[red]Failed to parse database URL '{db_url}': {exc}[/]")
    return database_files


def _archive_before_reset(archive_choice: bool | None, *, force: bool) -> None:
    should_archive = archive_choice if archive_choice is not None else None
    archive_mandatory = archive_choice is True or force
    if should_archive is None:
        if force:
            should_archive = True
        else:
            should_archive = typer.confirm("Create a mailbox archive before wiping everything?", default=True)
    if should_archive:
        try:
            archived_state, _ = _create_mailbox_archive(
                project_filters=(),
                scrub_preset=DEFAULT_ARCHIVE_SCRUB_PRESET,
                label="pre-reset",
                status_message="Archiving current mailbox before reset...",
            )
            console.print(f"[green]✓ Saved restore point to:[/] {archived_state}")
            console.print(
                f"[dim]Restore later with:[/] mcp-agent-mail archive restore {archived_state.name}"
            )
        except ShareExportError as exc:
            console.print(f"[red]Failed to create archive:[/] {exc}")
            if archive_mandatory:
                raise typer.Exit(code=1) from exc
            if not typer.confirm("Archive failed. Continue without a backup?", default=False):
                raise typer.Exit(code=1) from exc


def _remove_reset_database_files(database_files: list[Path]) -> list[Path]:
    deleted_db_files: list[Path] = []
    for path in database_files:
        try:
            if path.exists():
                path.unlink()
                deleted_db_files.append(path)
        except Exception as exc:  # pragma: no cover - filesystem failures
            console.print(f"[red]Failed to delete {path}: {exc}[/]")
    return deleted_db_files


def _remove_reset_storage_contents(storage_root: Path) -> list[Path]:
    deleted_storage: list[Path] = []
    if storage_root.exists():
        for child in storage_root.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                deleted_storage.append(child)
            except Exception as exc:  # pragma: no cover
                console.print(f"[red]Failed to remove {child}: {exc}[/]")
    else:
        console.print(f"[yellow]Storage root {storage_root} does not exist; nothing to remove.[/]")
    return deleted_storage


@app.command("clear-and-reset-everything")
def clear_and_reset_everything(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Skip the final destructive confirmation prompt (still asks about creating an archive).",
    ),
    archive_choice: Annotated[
        Optional[bool],
        typer.Option(
            "--archive/--no-archive",
            help="Attempt a pre-reset archive before deleting data (default: prompt when interactive).",
        ),
    ] = None,
) -> None:
    """Delete the SQLite database (including WAL/SHM) and wipe all storage-root contents."""
    settings = get_settings()
    database_files = _reset_database_files(settings.database.url)
    storage_root = _resolve_path(settings.storage.root)
    if not force:
        console.print("[bold yellow]This will irreversibly delete:[/]")
        if database_files:
            for path in database_files:
                console.print(f"  • {path}")
        else:
            console.print("  • (no SQLite files detected)")
        console.print(f"  • All contents inside {storage_root} (including .git)")
        console.print()

    _archive_before_reset(archive_choice, force=force)
    if not force and not typer.confirm("Proceed with destructive reset?", default=False):
        raise typer.Exit(code=1)

    deleted_db_files = _remove_reset_database_files(database_files)
    deleted_storage = _remove_reset_storage_contents(storage_root)

    console.print("[green]✓ Reset complete.[/]")
    if deleted_db_files:
        console.print(f"[dim]Removed database files:[/] {', '.join(str(p) for p in deleted_db_files)}")
    if deleted_storage:
        console.print(f"[dim]Cleared storage root entries:[/] {', '.join(str(p.name) for p in deleted_storage)}")


@app.command("migrate")
def migrate() -> None:
    """Create database schema from SQLModel definitions (pure SQLModel approach)."""
    settings = get_settings()
    with console.status("Creating database schema from models..."):
        # Pure SQLModel: models define schema, create_all() creates tables
        _run_async(ensure_schema(settings))
    console.print("[green]✓ Database schema created from model definitions![/]")
    console.print("[dim]Note: To apply model changes, delete storage.sqlite3 and run this again.[/]")


def _print_projects_json(rows: list[tuple[Project, int]], include_agents: bool) -> None:
    projects_json = []
    for project, agent_count in rows:
        entry = {
            "id": project.id,
            "slug": project.slug,
            "human_key": project.human_key,
            "created_at": project.created_at.isoformat(),
        }
        if include_agents:
            entry["agent_count"] = agent_count
        projects_json.append(entry)
    json.dump(projects_json, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _print_projects_table(rows: list[tuple[Project, int]], include_agents: bool) -> None:
    table = Table(title="Projects", show_lines=False)
    table.add_column("ID")
    table.add_column("Slug")
    table.add_column("Human Key")
    table.add_column("Created")
    if include_agents:
        table.add_column("Agents")
    for project, agent_count in rows:
        row = [str(project.id), project.slug, project.human_key, project.created_at.isoformat()]
        if include_agents:
            row.append(str(agent_count))
        table.add_row(*row)
    console.print(table)


@app.command("list-projects")
def list_projects(
    include_agents: bool = typer.Option(False, help="Include agent counts."),
    json_output: bool = typer.Option(False, "--json", help=JSON_OUTPUT_HELP),
) -> None:
    """List known projects."""

    settings = get_settings()

    async def _collect() -> list[tuple[Project, int]]:
        await ensure_schema(settings)
        async with get_session() as session:
            result = await session.execute(select(Project))
            projects = result.scalars().all()
            rows: list[tuple[Project, int]] = []
            if include_agents:
                for project in projects:
                    count_result = await session.execute(
                        select(func.count(Agent.id)).where(Agent.project_id == project.id)
                    )
                    count = int(count_result.scalar_one())
                    rows.append((project, count))
            else:
                rows = [(project, 0) for project in projects]
            return rows

    try:
        if not json_output:
            with console.status("Collecting project data..."):
                rows = _run_async(_collect())
        else:
            rows = _run_async(_collect())
    except Exception as exc:
        if json_output:
            console.print_json(json.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Failed to list projects:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        _print_projects_json(rows, include_agents)
    else:
        _print_projects_table(rows, include_agents)


@guard_app.command("install")
def guard_install(
    project: str,
    repo: Annotated[Path, typer.Argument(..., help="Path to git repo")],
    prepush: Annotated[bool, typer.Option("--prepush/--no-prepush", help="Also install a pre-push guard.",)] = False,
) -> None:
    """Install the advisory pre-commit guard into the given repository."""

    settings = get_settings()
    if not settings.worktrees_enabled:
        console.print("[yellow]Worktree-friendly features are disabled (WORKTREES_ENABLED=0). Skipping guard install.[/]")
        return
    repo_path = repo.expanduser().resolve()

    async def _run() -> tuple[Project, Path]:
        project_record = await _get_project_record(project)
        hook_path = await install_guard_script(settings, project_record.slug, repo_path)
        if prepush:
            try:
                from .guard import install_prepush_guard as _install_prepush
                await _install_prepush(settings, project_record.slug, repo_path)
            except Exception as exc:
                console.print(f"[yellow]Warning: failed to install pre-push guard: {exc}[/]")
        return project_record, hook_path

    try:
        project_record, hook_path = _run_async(_run())
    except ValueError as exc:  # convert to CLI-friendly error
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]Installed guard for [bold]{project_record.human_key}[/] at {hook_path}.")


def _git_output(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
        return result.stdout.strip()
    except Exception:
        return None


def _resolve_hooks_directory(repo_path: Path) -> Path:
    hooks_path = _git_output(repo_path, "config", "--get", "core.hooksPath")
    if hooks_path:
        if hooks_path.startswith("/") or hooks_path[1:3] in (":\\", ":/"):
            return Path(hooks_path)
        root = _git_output(repo_path, "rev-parse", "--show-toplevel") or str(repo_path)
        return Path(root) / hooks_path
    git_dir = Path(_git_output(repo_path, "rev-parse", "--git-dir") or ".git")
    if not git_dir.is_absolute():
        git_dir = repo_path / git_dir
    return git_dir / "hooks"


@guard_app.command("uninstall")
def guard_uninstall(
    repo: Annotated[Path, typer.Argument(..., help="Path to git repo")],
) -> None:
    """Remove the advisory pre-commit guard from the repository."""

    repo_path = repo.expanduser().resolve()
    removed = _run_async(uninstall_guard_script(repo_path))
    hooks_dir = _resolve_hooks_directory(repo_path)
    pre_commit = hooks_dir / "pre-commit"
    pre_push = hooks_dir / "pre-push"
    if removed:
        console.print(f"[green]Removed guard scripts at {pre_commit} and (if present) {pre_push}.")
    else:
        console.print(f"[yellow]No guard scripts found at {hooks_dir}.")


@file_reservations_app.command("list")
def file_reservations_list(
    project: str = typer.Argument(..., help=PROJECT_IDENTIFIER_HELP),
    active_only: bool = typer.Option(True, help="Show only active file_reservations"),
) -> None:
    """Display advisory file_reservations for a project."""

    async def _run() -> tuple[Project, list[tuple[FileReservation, str]]]:
        project_record = await _get_project_record(project)
        if project_record.id is None:
            raise ValueError(PROJECT_ID_REQUIRED_MESSAGE)
        await ensure_schema()
        async with get_session() as session:
            stmt = select(FileReservation, Agent.name).join(Agent, cast(ColumnElement[bool], FileReservation.agent_id == Agent.id)).where(
                cast(ColumnElement[bool], FileReservation.project_id == project_record.id)
            )
            if active_only:
                stmt = stmt.where(cast(ColumnElement[bool], cast(Any, FileReservation.released_ts).is_(None)))
            stmt = stmt.order_by(asc(cast(Any, FileReservation.expires_ts)))
            rows = [(row[0], row[1]) for row in (await session.execute(stmt)).all()]
        return project_record, rows

    try:
        project_record, rows = _run_async(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    table = Table(title=f"File Reservations for {project_record.human_key}", show_lines=False)
    table.add_column("ID")
    table.add_column("Agent")
    table.add_column("Pattern")
    table.add_column("Exclusive")
    table.add_column("Expires")
    table.add_column("Released")
    for file_reservation, agent_name in rows:
        table.add_row(
            str(file_reservation.id),
            agent_name,
            file_reservation.path_pattern,
            "yes" if file_reservation.exclusive else "no",
            _iso(file_reservation.expires_ts),
            _iso(file_reservation.released_ts) if file_reservation.released_ts else "",
        )
    console.print(table)

@amctl_app.command("env")
def amctl_env(
    project_path: Annotated[Path, typer.Option("--path", "-p", help="Path to repo/worktree",)] = Path(),
    agent: Annotated[Optional[str], typer.Option("--agent", "-a", help="Agent name (defaults to $AGENT_NAME)")] = None,
) -> None:
    """
    Print environment variables useful for build wrappers (slots, caches, artifacts).
    """
    p = _resolve_repo_worktree_root(_canonical_project_path(project_path))
    agent_name = agent or os.environ.get("AGENT_NAME") or "Unknown"
    # Reuse server helper for identity
    from mcp_agent_mail.app import _resolve_project_identity as _resolve_ident
    ident = _resolve_ident(str(p))
    slug = ident["slug"]
    project_uid = ident["project_uid"]
    # Determine branch
    branch = ident.get("branch") or ""
    if not branch:
        repo = None
        try:
            from git import Repo as _Repo
            repo = _Repo(str(p), search_parent_directories=True)
            try:
                branch = repo.active_branch.name
            except Exception:
                branch = repo.git.rev_parse("--abbrev-ref", "HEAD").strip()
        except Exception:
            branch = "unknown"
        finally:
            if repo is not None:
                with suppress(Exception):
                    repo.close()
    # Compute cache key and artifact dir using the same collision-resistant
    # components exported by am-run.
    settings = get_settings()
    cache_key = f"am-cache-{project_uid}-{agent_name}-{branch}"
    archive_root = Path(settings.storage.root).expanduser().resolve() / "projects" / slug
    artifacts_root = _resolved_build_path_within(
        archive_root,
        archive_root / "artifacts",
        label="artifacts",
    )
    artifact_dir = _resolved_build_path_within(
        artifacts_root,
        artifacts_root / _safe_build_path_component(agent_name) / _safe_build_path_component(branch or "unknown"),
        label="artifact",
    )
    # Print as KEY=VALUE lines
    typer.echo(f"SLUG={slug}")
    typer.echo(f"PROJECT_UID={project_uid}")
    typer.echo(f"BRANCH={branch}")
    typer.echo(f"AGENT={agent_name}")
    typer.echo(f"CACHE_KEY={cache_key}")
    typer.echo(f"ARTIFACT_DIR={artifact_dir}")


def _resolved_build_path_within(root: Path, candidate: Path, *, label: str) -> Path:
    """Resolve a build path and reject symlink or traversal escapes."""
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if not resolved_candidate.is_relative_to(resolved_root):
        raise click.ClickException(f"Unsafe {label} path escaped the project archive")
    return resolved_candidate


def _effective_build_slot_ttl_seconds(ttl_seconds: int) -> int:
    """Normalize build-slot TTLs to the same 60-second floor enforced by the server."""
    return max(60, int(ttl_seconds))


def _build_slot_renew_interval_seconds(ttl_seconds: int) -> int:
    """Renew halfway through the effective TTL so leases do not expire on the boundary."""
    return max(1, _effective_build_slot_ttl_seconds(ttl_seconds) // 2)


def _build_checkout_branch(project_path: Path) -> str:
    from git import Repo as GitRepo

    repo = None
    try:
        repo = GitRepo(str(project_path), search_parent_directories=True)
        try:
            return repo.active_branch.name
        except Exception:
            return repo.git.rev_parse("--abbrev-ref", "HEAD").strip()
    except Exception:
        return "unknown"
    finally:
        if repo is not None:
            with suppress(Exception):
                repo.close()


def _is_active_build_lease(data: dict[str, Any], now: datetime) -> bool:
    if data.get("released_ts"):
        return False
    expires = data.get("expires_ts")
    if expires:
        parsed = _parse_iso_datetime(expires)
        if parsed is not None and parsed <= now:
            return False
    return True


def _read_existing_build_lease(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _read_active_build_leases(slot_dir: Path) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    for lease_file in slot_dir.glob("*.json"):
        try:
            data = json.loads(lease_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and _is_active_build_lease(data, now):
                results.append(data)
        except Exception:
            results.append({
                "slot": slot_dir.name, "agent": "<unreadable lease>", "exclusive": True,
                "malformed": True, "lease_file": lease_file.name,
            })
    return results


@dataclass
class _LocalBuildLease:
    archive: ProjectArchive
    archive_root: Path
    slot: str
    agent_name: str
    branch: str
    project_uid: str | None
    execution_id: str
    ttl_seconds: int
    shared: bool

    async def ensure_slot_path(self) -> Path:
        slots_path = self.archive_root / "build_slots"
        slots_root = _resolved_build_path_within(self.archive_root, slots_path, label=BUILD_SLOTS_LABEL)
        await asyncio.to_thread(slots_root.mkdir, parents=True, exist_ok=True)
        slots_root = _resolved_build_path_within(self.archive_root, slots_path, label=BUILD_SLOTS_LABEL)
        slot_dir = _resolved_build_path_within(slots_root, slots_root / _safe_build_path_component(self.slot), label=BUILD_SLOT_LABEL)
        await asyncio.to_thread(slot_dir.mkdir, parents=True, exist_ok=True)
        return _resolved_build_path_within(slots_root, slot_dir, label=BUILD_SLOT_LABEL)

    def lease_path(self, slot_dir: Path) -> Path:
        slots_root = _resolved_build_path_within(self.archive_root, self.archive_root / "build_slots", label=BUILD_SLOTS_LABEL)
        resolved_slot_dir = _resolved_build_path_within(slots_root, slot_dir, label=BUILD_SLOT_LABEL)
        holder = _safe_build_path_component(self.execution_id)
        return _resolved_build_path_within(resolved_slot_dir, resolved_slot_dir / f"{holder}.json", label="build-slot lease")

    def write_release(self, path: Path) -> None:
        now = datetime.now(timezone.utc)
        data = _read_existing_build_lease(path)
        if data is None or not _is_active_build_lease(data, now):
            return
        data.update({"released_ts": now.isoformat(), "expires_ts": now.isoformat()})
        with suppress(Exception):
            _write_json_atomic_sync(path, data)

    def write_renewal(self, path: Path) -> None:
        now = datetime.now(timezone.utc)
        current = _read_existing_build_lease(path) or {}
        if not _is_active_build_lease(current, now):
            return
        current_exp = _parse_iso_datetime(cast(str | None, current.get("expires_ts")))
        base = max(now, current_exp) if current_exp is not None else now
        new_exp = base + timedelta(seconds=self.ttl_seconds)
        current.update({"slot": self.slot, "agent": self.agent_name, "branch": self.branch, "expires_ts": new_exp.isoformat()})
        with suppress(Exception):
            _write_json_atomic_sync(path, current)

    def acquisition_payload(self, current: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
        active = current if current is not None and _is_active_build_lease(current, now) else None
        requested_exp = now + timedelta(seconds=self.ttl_seconds)
        current_exp = _parse_iso_datetime(cast(str | None, active.get("expires_ts"))) if active else None
        return {
            "slot": self.slot, "agent": self.agent_name, "project_uid": self.project_uid,
            "authority": "local", "execution_id": self.execution_id, "branch": self.branch,
            "exclusive": not self.shared,
            "acquired_ts": cast(str, active.get("acquired_ts")) if active is not None and isinstance(active.get("acquired_ts"), str) else now.isoformat(),
            "expires_ts": max(requested_exp, current_exp).isoformat() if current_exp is not None else requested_exp.isoformat(),
        }

    async def acquire(self) -> tuple[list[dict[str, Any]], Path]:
        async with archive_write_lock(self.archive):
            slot_dir = await self.ensure_slot_path()
            active = await asyncio.to_thread(_read_active_build_leases, slot_dir)
            conflicts = [
                entry for entry in active
                if entry.get("execution_id") != self.execution_id
                and ((not self.shared) or entry.get("exclusive", True))
            ]
            lease_path = self.lease_path(slot_dir)
            now = datetime.now(timezone.utc)
            current = await asyncio.to_thread(_read_existing_build_lease, lease_path)
            payload = self.acquisition_payload(current, now)
            with suppress(Exception):
                await asyncio.to_thread(_write_json_atomic_sync, lease_path, payload)
            return conflicts, lease_path

    async def renew(self, path: Path) -> None:
        async with archive_write_lock(self.archive):
            await asyncio.to_thread(self.write_renewal, path)

    async def release(self, path: Path) -> None:
        async with archive_write_lock(self.archive):
            await asyncio.to_thread(self.write_release, path)


@dataclass(repr=False)
class _BuildExecution:
    project_path: Path
    agent_name: str
    slot: str
    branch: str
    execution_id: str
    external_id: str
    execution_token: str
    registration_token: str | None
    server_url: str
    bearer: str
    timeout_seconds: float = 5.0
    started: bool = False
    start_attempted: bool = False
    end_status: str = "cancelled"

    def call(self, request_id: str, tool: str, arguments: dict[str, Any]) -> Any:
        headers = {"Authorization": f"Bearer {self.bearer}"} if self.bearer else {}
        request = {
            "jsonrpc": "2.0", "id": request_id, "method": TOOLS_CALL_METHOD,
            "params": {"name": tool, "arguments": arguments},
        }
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(self.server_url, json=request, headers=headers)
            return _parse_jsonrpc_response(response, request_name=f"am-run {tool}")

    def ensure_project(self) -> None:
        self.call("am-run-ensure", "ensure_project", {"human_key": str(self.project_path)})

    def resolve_registration_token(self) -> None:
        if not self.registration_token:
            self.registration_token = _run_async(_lookup_agent_registration_token(str(self.project_path), self.agent_name))

    def start(self) -> str:
        self.start_attempted = True
        assert self.registration_token is not None
        result = self.call("am-run-execution-start", "start_agent_execution", {
            "project_key": str(self.project_path), "agent_name": self.agent_name,
            "external_id": self.external_id, "client_name": "am-run",
            "execution_token": self.execution_token, "lifecycle_protocol_version": 1,
            "kind": "session", "task_description": f"am-run build slot: {self.slot}"[:2048],
            "cwd": str(self.project_path), "repo_root": str(self.project_path),
            "worktree_path": str(self.project_path), "branch": self.branch or None,
            "registration_token": self.registration_token,
        })
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            raise click.ClickException("am-run start_agent_execution: server response is missing execution id")
        self.started = True
        self.execution_id = cast(str, result["id"])
        return self.execution_id

    def slot_arguments(self) -> dict[str, Any]:
        return {
            "project_key": str(self.project_path), "agent_name": self.agent_name,
            "slot": self.slot, "branch": self.branch or None, "execution_id": self.execution_id,
            "execution_token": self.execution_token, "lifecycle_protocol_version": 1,
            "registration_token": self.registration_token,
        }

    def end(self) -> None:
        if not self.started:
            return
        assert self.registration_token is not None
        self.call("am-run-execution-end", "end_agent_execution", {
            "project_key": str(self.project_path), "agent_name": self.agent_name,
            "execution_id": self.execution_id, "execution_token": self.execution_token,
            "lifecycle_protocol_version": 1, "status": self.end_status,
            "registration_token": self.registration_token,
        })
        self.started = False


@dataclass(repr=False)
class _BuildRunLifecycle:
    execution: _BuildExecution
    local_lease: _LocalBuildLease
    env: dict[str, str]
    worktrees_enabled: bool
    guard_mode: str
    block_on_conflicts: bool
    renew_interval_seconds: int
    slot_acquired: bool = False
    slot_authority: str = "none"
    lease_path: Path | None = None
    renew_thread: threading.Thread | None = None
    renew_stop: threading.Event = field(default_factory=threading.Event)
    renew_warning_emitted: threading.Event = field(default_factory=threading.Event)

    def prepare_without_slots(self) -> None:
        self.execution.resolve_registration_token()
        if not self.execution.registration_token:
            return
        try:
            self.execution.ensure_project()
            self.env["AGENT_EXECUTION_ID"] = self.execution.start()
        except httpx.TransportError:
            # Native execution IDs are exposed only after a confirmed start.
            pass

    def prepare(self) -> None:
        if not self.worktrees_enabled:
            self.prepare_without_slots()
            return
        try:
            self.execution.ensure_project()
        except httpx.TransportError:
            self.prepare_local_slot()
        else:
            self.prepare_server_slot()

    def check_conflicts(self, conflicts: list[dict[str, Any]], *, server: bool) -> None:
        if conflicts and self.guard_mode == "warn":
            advisory = "server advisory" if server else "advisory"
            console.print(f"[yellow]Build slot conflicts ({advisory}, proceeding):[/]")
            for conflict in conflicts:
                console.print(
                    f"  - slot={conflict.get('slot','')} agent={conflict.get('agent','')} "
                    f"branch={conflict.get('branch','')} expires={conflict.get('expires_ts','')}"
                )
        if conflicts and self.block_on_conflicts:
            console.print("[red]Build slot conflicts detected and --block-on-conflicts set; aborting.[/]")
            raise typer.Exit(code=1)

    def prepare_server_slot(self) -> None:
        self.execution.resolve_registration_token()
        if not self.execution.registration_token:
            raise click.ClickException(
                "am-run requires a registered agent with a registration token when the server is reachable. "
                "Register the agent first or set $AGENT_MAIL_REGISTRATION_TOKEN."
            )
        try:
            self.env["AGENT_EXECUTION_ID"] = self.execution.start()
        except httpx.TransportError as exc:
            raise click.ClickException(
                "am-run could not confirm start_agent_execution after the "
                "server became authoritative; refusing a local lease because "
                "the remote start result is ambiguous"
            ) from exc
        try:
            arguments = self.execution.slot_arguments()
            arguments.update({"ttl_seconds": self.local_lease.ttl_seconds, "exclusive": not self.local_lease.shared})
            result = self.execution.call("am-run-acquire", "acquire_build_slot", arguments) or {}
            conflicts = list(result.get("conflicts") or [])
            self.slot_acquired = True
            self.slot_authority = "server"
        except httpx.TransportError as exc:
            raise click.ClickException(
                "am-run could not confirm acquire_build_slot after starting "
                "the server execution; refusing a local lease because the "
                "remote acquisition result is ambiguous"
            ) from exc
        self.check_conflicts(conflicts, server=True)
        self.start_renewer(server=True)

    def prepare_local_slot(self) -> None:
        conflicts, self.lease_path = _run_async(self.local_lease.acquire())
        self.env["AGENT_EXECUTION_ID"] = self.execution.execution_id
        self.check_conflicts(conflicts, server=False)
        self.slot_acquired = True
        self.slot_authority = "local"
        self.start_renewer(server=False)

    def start_renewer(self, *, server: bool) -> None:
        target = self.renew_server_slot if server else self.renew_local_slot
        self.renew_thread = threading.Thread(target=target, name="am-run-renew", daemon=True)
        self.renew_thread.start()

    def renew_server_slot(self) -> None:
        while not self.renew_stop.wait(self.renew_interval_seconds):
            try:
                arguments = self.execution.slot_arguments()
                arguments["extend_seconds"] = self.local_lease.ttl_seconds
                self.execution.call("am-run-renew", "renew_build_slot", arguments)
            except Exception as exc:
                if not self.renew_warning_emitted.is_set():
                    self.renew_warning_emitted.set()
                    console.print(
                        "[yellow]Server build-slot renewal failed; "
                        "keeping server authority and retrying without "
                        f"creating a local lease: {exc}[/]"
                    )

    def renew_local_slot(self) -> None:
        while not self.renew_stop.wait(self.renew_interval_seconds):
            try:
                if self.lease_path:
                    _run_async(self.local_lease.renew(self.lease_path))
            except Exception:
                continue

    def release_slot(self) -> None:
        self.renew_stop.set()
        if self.renew_thread and self.renew_thread.is_alive():
            self.renew_thread.join(timeout=self.execution.timeout_seconds + 1.0)
        if self.slot_authority == "server":
            try:
                self.execution.call("am-run-release", "release_build_slot", self.execution.slot_arguments())
            except Exception as exc:
                console.print(
                    "[yellow]Server build-slot release failed; the execution "
                    f"end/reaper remains authoritative: {exc}[/]"
                )
        elif self.slot_authority == "local" and self.lease_path:
            with suppress(Exception):
                _run_async(self.local_lease.release(self.lease_path))

    def close(self) -> None:
        if self.worktrees_enabled and self.slot_acquired:
            self.release_slot()
        if self.execution.start_attempted and not self.execution.started and self.execution.registration_token:
            # Recover a possibly committed start using the same idempotency key
            # and capability, then end that native execution immediately.
            with suppress(Exception):
                self.execution.start()
        if self.execution.started:
            try:
                self.execution.end()
            except Exception as exc:
                console.print(
                    "[yellow]AgentExecution cleanup will rely on expiry after "
                    f"the server rejected end: {exc}[/]"
                )


@app.command(name="am-run")
def am_run(
    slot: Annotated[str, typer.Argument(help="Build slot name (e.g., frontend-build)")],
    cmd: Annotated[list[str], typer.Argument(help="Command to run")],
    project_path: Annotated[Path, typer.Option("--path", "-p", help="Path to repo/worktree",)] = Path(),
    agent: Annotated[Optional[str], typer.Option("--agent", "-a", help="Agent name (defaults to $AGENT_NAME)")] = None,
    ttl_seconds: Annotated[int, typer.Option("--ttl-seconds", help="Lease TTL seconds (default 3600)")] = 3600,
    shared: Annotated[bool, typer.Option("--shared/--exclusive", help="Shared (non-exclusive) lease",)] = False,
    block_on_conflicts: Annotated[bool, typer.Option("--block-on-conflicts/--no-block-on-conflicts", help="Exit 1 if exclusive conflicts are present")] = False,
) -> None:
    """
    Build wrapper that prepares environment variables and manages a build slot:
    - Acquires the slot (advisory), prints conflicts in warn mode.
    - Renews lease in the background while the child runs.
    - Releases the slot on exit.
    """
    p = _resolve_repo_worktree_root(_canonical_project_path(project_path))
    agent_name = agent or os.environ.get("AGENT_NAME") or "Unknown"
    from mcp_agent_mail.app import _resolve_project_identity as _resolve_ident
    ident = _resolve_ident(str(p))
    slug = ident["slug"]
    project_uid = ident["project_uid"]
    branch = ident.get("branch") or ""
    execution_id = str(uuid.uuid4())
    execution_external_id = f"am-run:{execution_id}"
    execution_token = secrets.token_hex(32)
    if not branch:
        branch = _build_checkout_branch(p)
    settings = get_settings()
    guard_mode = (os.environ.get("AGENT_MAIL_GUARD_MODE", "block") or "block").strip().lower()
    worktrees_enabled = bool(settings.worktrees_enabled)
    server_url = f"http://{settings.http.host}:{settings.http.port}{settings.http.path}"
    bearer = settings.http.bearer_token or ""
    effective_ttl_seconds = _effective_build_slot_ttl_seconds(ttl_seconds)
    renew_interval_seconds = _build_slot_renew_interval_seconds(ttl_seconds)
    archive = _run_async(ensure_archive(settings, slug))
    archive_root = archive.root.resolve()

    local_lease = _LocalBuildLease(
        archive=archive, archive_root=archive_root, slot=slot, agent_name=agent_name,
        branch=branch, project_uid=project_uid, execution_id=execution_id,
        ttl_seconds=effective_ttl_seconds, shared=shared,
    )
    artifacts_root = _resolved_build_path_within(archive_root, archive_root / "artifacts", label="artifacts")
    artifact_dir = _resolved_build_path_within(
        artifacts_root,
        artifacts_root
        / _safe_build_path_component(agent_name)
        / _safe_build_path_component(branch or "unknown"),
        label="artifact",
    )
    env = os.environ.copy()
    env.update({
        "AM_SLOT": slot,
        "SLUG": slug,
        "PROJECT_UID": project_uid or "",
        "BRANCH": branch,
        "AGENT": agent_name,
        "CACHE_KEY": f"am-cache-{project_uid}-{agent_name}-{branch}",
        "ARTIFACT_DIR": str(artifact_dir),
    })
    execution = _BuildExecution(
        project_path=p, agent_name=agent_name, slot=slot, branch=branch,
        execution_id=execution_id, external_id=execution_external_id, execution_token=execution_token,
        registration_token=_ambient_registration_token(), server_url=server_url, bearer=bearer,
    )
    lifecycle = _BuildRunLifecycle(
        execution=execution, local_lease=local_lease, env=env, worktrees_enabled=worktrees_enabled,
        guard_mode=guard_mode, block_on_conflicts=block_on_conflicts, renew_interval_seconds=renew_interval_seconds,
    )
    try:
        lifecycle.prepare()
        console.print(
            f"[cyan]$ <command redacted: {len(cmd)} argv item(s)>[/]  "
            f"[dim](slot={slot})[/]"
        )
        rc = subprocess.run(list(cmd), env=env, check=False).returncode
        execution.end_status = "completed" if rc == 0 else "failed"
    except FileNotFoundError:
        rc = 127
        execution.end_status = "failed"
    finally:
        lifecycle.close()
    if rc != 0:
        raise typer.Exit(code=rc)

@projects_app.command("mark-identity")
def projects_mark_identity(
    project_path: Annotated[Path, typer.Argument(..., help="Path to repo/worktree ('.' for current)")],
) -> None:
    """
    Write a checkout-local project_uid marker and hide it in .git/info/exclude.

    This low-level migration utility derives identity from the checkout. Normal
    mailbox provisioning should use /mcp-agent-mail:onboard, which writes the
    exact project_uid returned by the server when a marker is explicitly
    requested. The marker and its ignore rule are never committed.
    """
    p = _canonical_project_path(project_path)
    root = _resolve_repo_worktree_root(p)
    from mcp_agent_mail.app import _resolve_project_identity as _resolve_ident
    ident = _resolve_ident(str(root))
    uid = ident.get("project_uid") or ""
    if not uid:
        raise typer.BadParameter("Unable to resolve project_uid for this path.")
    marker_path = root / ".agent-mail-project-id"
    if marker_path.exists():
        existing_uid = marker_path.read_text(encoding="utf-8").strip()
        if existing_uid != uid:
            raise typer.BadParameter(
                f"Refusing to replace existing local marker {marker_path} "
                f"({existing_uid!r}) with {uid!r}."
            )
    exclude_raw = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    exclude_path = Path(exclude_raw)
    if not exclude_path.is_absolute():
        exclude_path = root / exclude_path
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    excluded = (
        exclude_path.read_text(encoding="utf-8").splitlines()
        if exclude_path.exists()
        else []
    )
    if ".agent-mail-project-id" not in excluded:
        with exclude_path.open("a", encoding="utf-8") as handle:
            handle.write(".agent-mail-project-id\n")
    if not marker_path.exists():
        marker_path.write_text(uid + "\n", encoding="utf-8")
    console.print(
        f"[green]Wrote local marker[/] {marker_path} with project_uid={uid}; "
        "hidden only through .git/info/exclude."
    )


@projects_app.command("discovery-init")
def projects_discovery_init(
    project_path: Annotated[Path, typer.Argument(..., help="Path to repo/worktree ('.' for current)")],
    product: Annotated[Optional[str], typer.Option("--product", "-P", help="Optional product_uid")] = None,
) -> None:
    """
    Scaffold a discovery YAML file (.agent-mail.yaml) with project_uid (and optional product_uid).
    """
    settings = get_settings()
    p = _canonical_project_path(project_path)
    root = _resolve_repo_worktree_root(p)
    from mcp_agent_mail.app import _resolve_project_identity as _resolve_ident
    ident = _resolve_ident(str(root))
    uid = ident.get("project_uid") or ""
    if not uid:
        raise typer.BadParameter("Unable to resolve project_uid for this path.")
    ypath = root / ".agent-mail.yaml"
    lines = ["# Agent Mail discovery file", f"project_uid: {uid}"]
    if product:
        lines.append(f"product_uid: {product}")
    ypath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]Wrote[/] {ypath}")
    # The discovery YAML is only consulted by identity resolution when
    # WORKTREES_ENABLED=1; otherwise it is inert. Make that explicit so users
    # aren't left with a file that silently does nothing. (#208)
    if not settings.worktrees_enabled:
        console.print(
            "[yellow]Note:[/] WORKTREES_ENABLED is not set, so this discovery file is currently "
            "inert. Set WORKTREES_ENABLED=1 for it to take effect."
        )
@mail_app.command("status")
def mail_status(
    project_path: Annotated[
        Path,
        typer.Argument(..., help="Absolute path to a repo/worktree directory (use '.' for current)."),
    ],
) -> None:
    """
    Print routing diagnostics: gate state, configured identity mode, normalized remote (if any),
    and the slug that would be used for this path.
    """
    settings = get_settings()
    p = _resolve_repo_worktree_root(_canonical_project_path(project_path))
    gate = settings.worktrees_enabled
    mode = (settings.project_identity_mode or "dir").strip().lower()
    remote_name = (settings.project_identity_remote or "origin").strip()
    from mcp_agent_mail.app import _resolve_project_identity as _resolve_ident
    ident = _resolve_ident(str(p))
    normalized_remote = ident.get("normalized_remote")
    slug_value = ident["slug"]

    table = Table(title="Mail routing status", show_lines=False, expand=True)
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    table.add_row("WORKTREES_ENABLED", "true" if gate else "false")
    table.add_row("PROJECT_IDENTITY_MODE", mode or "dir")
    table.add_row("PROJECT_IDENTITY_REMOTE", remote_name)
    table.add_row("normalized_remote", normalized_remote or "")
    table.add_row("slug", slug_value)
    table.add_row("path", ident["human_key"])
    console.print(table)
    typer.echo(f"slug={slug_value}")
    typer.echo(f"path={ident['human_key']}")


@guard_app.command("status")
def guard_status(
    repo: Annotated[Path, typer.Argument(..., help="Path to git repo")],
) -> None:
    """
    Print guard status: gate/mode, resolved hooks directory, and presence of hooks.
    """
    settings = get_settings()
    p = repo.expanduser().resolve()
    gate = settings.worktrees_enabled
    mode = (settings.project_identity_mode or "dir").strip().lower()
    guard_mode = (os.environ.get("AGENT_MAIL_GUARD_MODE", "block") or "block").strip().lower()

    hooks_dir = _resolve_hooks_directory(p)

    pre_commit = hooks_dir / "pre-commit"
    pre_push = hooks_dir / "pre-push"

    table = Table(title="Guard status", show_lines=False)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("WORKTREES_ENABLED", "true" if gate else "false")
    table.add_row("AGENT_MAIL_GUARD_MODE", guard_mode)
    table.add_row("PROJECT_IDENTITY_MODE", mode or "dir")
    table.add_row("hooks_dir", str(hooks_dir))
    table.add_row("pre-commit", "present" if pre_commit.exists() else "missing")
    table.add_row("pre-push", "present" if pre_push.exists() else "missing")
    console.print(table)

class _GuardPathMatcher:
    def __init__(self, repo_root: Path) -> None:
        ignorecase = _git_output(repo_root, "config", "--get", "core.ignorecase")
        self.ignorecase = bool(ignorecase and ignorecase.strip().lower() == "true")
        try:
            from pathspec import PathSpec
        except Exception:
            self.path_spec = None
        else:
            self.path_spec = PathSpec

    def normalize(self, path: str) -> str:
        normalized = path.replace("\\", "/").lstrip("/")
        return normalized.lower() if self.ignorecase else normalized

    def compile(self, pattern: str) -> Any:
        pattern = pattern.lower() if self.ignorecase else pattern
        if self.path_spec is not None:
            try:
                return self.path_spec.from_lines("gitignore", [pattern])
            except Exception:
                return None
        return None

    def match(self, spec: Any, path: str, pattern: str) -> bool:
        import fnmatch

        normalized_path = self.normalize(path)
        normalized_pattern = self.normalize(pattern)
        if spec is not None:
            try:
                return bool(spec.match_file(normalized_path))
            except Exception:
                pass
        return (
            fnmatch.fnmatchcase(normalized_path, normalized_pattern)
            or fnmatch.fnmatchcase(normalized_pattern, normalized_path)
            or normalized_path == normalized_pattern
        )


def _guard_reservation_pattern(data: dict[str, Any], agent_name: str, now: datetime, seen_ids: set[str]) -> str | None:
    reservation_id = data.get("id")
    if reservation_id is not None:
        reservation_key = str(reservation_id)
        if reservation_key in seen_ids:
            return None
        seen_ids.add(reservation_key)
    if data.get("agent") == agent_name or not data.get("exclusive", True):
        return None
    expires = data.get("expires_ts")
    if expires:
        parsed = _parse_iso_datetime(expires)
        if parsed is not None and parsed < now:
            return None
    return (data.get("path_pattern") or "").strip() or None


def _guard_path_conflicts(
    fr_dir: Path, repo_root: Path, paths: list[str], agent_name: str,
) -> list[tuple[str, str, str]]:
    matcher = _GuardPathMatcher(repo_root)
    now = datetime.now(timezone.utc)
    conflicts: list[tuple[str, str, str]] = []
    seen_ids: set[str] = set()
    for candidate in sorted(fr_dir.glob("*.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        pattern = _guard_reservation_pattern(data, agent_name, now, seen_ids)
        if not pattern:
            continue
        spec = matcher.compile(pattern)
        for path_value in paths:
            if matcher.match(spec, path_value, pattern):
                conflicts.append((path_value, data.get("agent", ""), pattern))
    return conflicts


def _guard_stdin_paths(stdin_nul: bool) -> list[str]:
    if not stdin_nul:
        return []
    data = sys.stdin.buffer.read()
    return list(dict.fromkeys(path for path in data.decode("utf-8", "ignore").split("\x00") if path))


@guard_app.command("check")
def guard_check(
    stdin_nul: bool = typer.Option(False, "--stdin-nul", help="Read NUL-delimited paths from STDIN"),
    advisory: bool = typer.Option(False, "--advisory", help="Advisory mode: print conflicts but exit 0"),
    repo: Annotated[Optional[Path], typer.Option("--repo", help="Path to git repo (defaults to detected root)")] = None,
) -> None:
    """
    Check paths (from STDIN when --stdin-nul) against active exclusive file_reservations.

    Unifies guard semantics across hooks and CLI:
    - Normalizes paths to repo-root relative, honoring core.ignorecase
    - Uses Git wildmatch semantics via pathspec when available, with fnmatch fallback
    - Prints conflicts and returns non-zero unless --advisory is set
    """
    settings = get_settings()
    agent_name = os.environ.get("AGENT_NAME")
    if not agent_name:
        console.print("[red]AGENT_NAME environment variable is required.[/]")
        raise typer.Exit(code=1)

    if repo is not None:
        repo_root = repo.expanduser().resolve()
    else:
        guess = _git_output(Path.cwd(), "rev-parse", "--show-toplevel")
        repo_root = Path(guess).expanduser().resolve() if guess else Path.cwd().expanduser().resolve()

    # Map repo path to project archive
    try:
        from mcp_agent_mail.app import _compute_project_slug as _compute_slug
    except Exception:
        console.print("[red]Internal error: cannot import slug helper.[/]")
        raise typer.Exit(code=1) from None
    slug_value = _compute_slug(str(repo_root))
    archive = _run_async(ensure_archive(settings, slug_value))

    # Read NUL-delimited paths from STDIN
    paths = _guard_stdin_paths(stdin_nul)
    if not paths:
        raise typer.Exit(code=0)

    fr_dir = archive.root / "file_reservations"
    if not fr_dir.exists():
        raise typer.Exit(code=0)
    conflicts = _guard_path_conflicts(fr_dir, repo_root, paths, agent_name)

    if conflicts:
        console.print("[red]Exclusive file_reservation conflicts detected:[/]")
        for path_value, agent, pattern in conflicts:
            console.print(f"  - {path_value} matches file_reservation '{pattern}' held by [bold]{agent}[/]")
        if advisory:
            console.print("[yellow]Advisory mode: not blocking (set AGENT_MAIL_GUARD_MODE=block to enforce).[/]")
            raise typer.Exit(code=0)
        else:
            console.print("[yellow]Resolve conflicts or release file_reservations before proceeding.[/]")
            console.print("[dim]Hints: set AGENT_MAIL_GUARD_MODE=warn for advisory, or AGENT_MAIL_BYPASS=1 to bypass in emergencies.[/]")
            raise typer.Exit(code=1)
    raise typer.Exit(code=0)

async def _preflight_project_adoption(session: Any, src: Project, dst: Project) -> None:
    src_agents = [row[0] for row in (await session.execute(select(Agent.name).where(cast(ColumnElement[bool], Agent.project_id == src.id)))).all()]
    dst_agents = [row[0] for row in (await session.execute(select(Agent.name).where(cast(ColumnElement[bool], Agent.project_id == dst.id)))).all()]
    duplicates = sorted(set(src_agents).intersection(set(dst_agents)))
    if duplicates:
        raise typer.BadParameter(f"Agent name conflicts in target project: {', '.join(duplicates)}")
    delivery_history = (
        await session.execute(
            select(MessageDelivery.id).where(_sa_or(
                cast(ColumnElement[bool], MessageDelivery.project_id == src.id),
                cast(ColumnElement[bool], MessageDelivery.sender_project_id_snapshot == src.id),
                and_(
                    cast(ColumnElement[bool], MessageDelivery.actor_kind == "agent"),
                    cast(ColumnElement[bool], MessageDelivery.actor_project_id_snapshot == src.id),
                ),
            )).limit(1)
        )
    ).first()
    if delivery_history is not None:
        raise typer.BadParameter("Source project has immutable message delivery history and cannot be adopted")
    # Keys already quoted in mail/archive and globally unique sequence prefixes
    # cannot be reassigned honestly. Refuse before touching either store.
    ticket_count = (
        await session.execute(select(func.count()).select_from(Ticket).where(cast(ColumnElement[bool], Ticket.project_id == src.id)))
    ).scalar_one()
    if ticket_count:
        raise typer.BadParameter(
            f"Source project has {ticket_count} ticket(s) and cannot be adopted. "
            "Ticket keys are globally unique and are quoted in mail and in the "
            "archive, so they cannot be reissued under another project."
        )


async def _commit_adopted_archive_move(
    dst_archive: ProjectArchive, settings: Any, *, add_relpaths: Sequence[str], remove_relpaths: Sequence[str], message: str,
) -> None:
    from .storage import AsyncFileLock, _commit_lock_path, _to_thread_cancellation_safe

    combined_relpaths = [*remove_relpaths, *add_relpaths]
    if not combined_relpaths:
        return
    commit_lock_path = _commit_lock_path(dst_archive.repo_root, combined_relpaths)
    async with AsyncFileLock(commit_lock_path):
        if remove_relpaths:
            await _to_thread_cancellation_safe(dst_archive.repo.git.rm, "--cached", "--ignore-unmatch", "--", *remove_relpaths)
        if add_relpaths:
            await _to_thread_cancellation_safe(dst_archive.repo.index.add, list(add_relpaths))
        literal_paths = [f":(literal){path}" for path in combined_relpaths]

        def _commit_only_moved_paths() -> None:
            with dst_archive.repo.git.custom_environment(
                GIT_AUTHOR_NAME=settings.storage.git_author_name,
                GIT_AUTHOR_EMAIL=settings.storage.git_author_email,
                GIT_COMMITTER_NAME=settings.storage.git_author_name,
                GIT_COMMITTER_EMAIL=settings.storage.git_author_email,
            ):
                dst_archive.repo.git.commit("--only", "--no-gpg-sign", "--no-verify", "-m", message, "--", *literal_paths)

        await _to_thread_cancellation_safe(_commit_only_moved_paths)


async def _adoption_move_candidates(src_archive: ProjectArchive, dst_archive: ProjectArchive) -> list[tuple[Path, Path]]:
    candidates: list[tuple[Path, Path]] = []
    collisions: list[str] = []
    for path in sorted(src_archive.root.rglob("*"), key=str):
        if not path.is_file() or path.name.endswith((".lock", ".lock.owner.json")):
            continue
        relative = path.relative_to(src_archive.root)
        if relative.parts[0] == "message_deliveries":
            raise typer.BadParameter(
                "Source project contains immutable message delivery artifacts and cannot be adopted"
            )
        destination = dst_archive.root / relative
        if await asyncio.to_thread(destination.exists):
            collisions.append(relative.as_posix())
            continue
        candidates.append((path, destination))
    if collisions:
        preview = ", ".join(collisions[:5])
        suffix = f" (+{len(collisions) - 5} more)" if len(collisions) > 5 else ""
        raise typer.BadParameter(f"Target archive already contains conflicting paths: {preview}{suffix}")
    return candidates


async def _record_adopted_project_alias(dst_archive: ProjectArchive, settings: Any, source_slug: str) -> None:
    from .storage import _commit

    aliases_path = dst_archive.root / "aliases.json"
    try:
        existing: dict[str, Any] = {}
        if await asyncio.to_thread(aliases_path.exists):
            existing = json.loads(await asyncio.to_thread(aliases_path.read_text, encoding="utf-8"))
        former = set(existing.get("former_slugs", []))
        former.add(source_slug)
        existing["former_slugs"] = sorted(former)
        await asyncio.to_thread(aliases_path.write_text, json.dumps(existing, indent=2), "utf-8")
        relative = aliases_path.relative_to(dst_archive.repo_root).as_posix()
        await _commit(dst_archive.repo, settings, f"adopt: record alias for {source_slug}", [relative])
    except Exception as exc:
        console.print(f"[yellow]Warning: failed to write aliases.json: {exc}[/]")


async def _move_adopted_archive(src: Project, dst: Project, src_archive: ProjectArchive, dst_archive: ProjectArchive) -> None:
    settings = get_settings()
    lock_order = tuple(sorted((src_archive, dst_archive), key=lambda archive: str(archive.lock_path)))
    async with archive_write_lock(lock_order[0]), archive_write_lock(lock_order[1]):
        candidates = await _adoption_move_candidates(src_archive, dst_archive)
        moved_relpaths: list[str] = []
        removed_relpaths: list[str] = []
        for source_path, destination in candidates:
            await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(source_path.replace, destination)
            moved_relpaths.append(destination.relative_to(dst_archive.repo_root).as_posix())
            removed_relpaths.append(source_path.relative_to(src_archive.repo_root).as_posix())
        await _commit_adopted_archive_move(
            dst_archive, settings, add_relpaths=moved_relpaths, remove_relpaths=removed_relpaths,
            message=f"adopt: move {src.slug} into {dst.slug}",
        )
        await _record_adopted_project_alias(dst_archive, settings, src.slug)


async def _apply_project_adoption(src: Project, dst: Project, src_archive: ProjectArchive, dst_archive: ProjectArchive) -> None:
    from sqlalchemy import update

    if src.id is None or dst.id is None:
        raise typer.BadParameter("Projects must be persisted (id not null).")
    await ensure_schema()
    # Keep the reserved SQLite writer lock from preflight through the re-key:
    # a newly accepted delivery must not race an already started archive move.
    async with get_immediate_session() as session:
        await _preflight_project_adoption(session, src, dst)
        await _move_adopted_archive(src, dst, src_archive, dst_archive)
        await session.execute(update(Agent).where(cast(ColumnElement[bool], Agent.project_id == src.id)).values(project_id=dst.id))
        await session.execute(update(Message).where(cast(ColumnElement[bool], Message.project_id == src.id)).values(project_id=dst.id))
        await session.execute(update(FileReservation).where(cast(ColumnElement[bool], FileReservation.project_id == src.id)).values(project_id=dst.id))
        await session.commit()


@projects_app.command("adopt")
def projects_adopt(
    source: Annotated[str, typer.Argument(..., help="Old project slug or human key")],
    target: Annotated[str, typer.Argument(..., help="New project slug or project_uid (future)")],
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply", help="Show plan without applying changes.")] = True,
) -> None:
    """
    Plan and optionally apply consolidation of legacy per-worktree projects into a canonical project.
    """
    async def _load(slug_or_key: str) -> Project:
        return await _get_project_record(slug_or_key)

    try:
        async def _both() -> tuple[Project, Project]:
            return await asyncio.gather(_load(source), _load(target))
        src, dst = _run_async(_both())
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc

    if src.id == dst.id:
        console.print("[yellow]Source and target refer to the same project; nothing to do.[/]")
        return

    plan: list[str] = []
    plan.append(f"Source: id={src.id} slug={src.slug} key={src.human_key}")
    plan.append(f"Target: id={dst.id} slug={dst.slug} key={dst.human_key}")

    # Heuristic: same repo if git-common-dir hashes match
    src_gdir = _git_output(Path(src.human_key), "rev-parse", "--git-common-dir")
    dst_gdir = _git_output(Path(dst.human_key), "rev-parse", "--git-common-dir")
    same_repo = bool(src_gdir and dst_gdir and Path(src_gdir).resolve() == Path(dst_gdir).resolve())
    plan.append(f"Same repo (git-common-dir): {'yes' if same_repo else 'no'}")

    if not same_repo:
        console.print("[red]Refusing to adopt: projects do not appear to belong to the same repository.[/]")
        return

    # Describe filesystem moves (archive layout)
    settings = get_settings()
    from .storage import ensure_archive as _ensure_archive
    src_archive = _run_async(_ensure_archive(settings, src.slug))
    dst_archive = _run_async(_ensure_archive(settings, dst.slug))
    plan.append(f"Move Git artifacts: {src_archive.root} -> {dst_archive.root}")
    plan.append("Re-key DB rows: source project_id -> target project_id (messages, agents, file_reservations, etc.)")
    plan.append("Write aliases.json under target 'projects/<slug>/' with former_slugs")

    console.print("[bold]Projects adopt plan (dry-run)[/bold]")
    for line in plan:
        console.print(f"- {line}")

    if dry_run:
        return
    try:
        _run_async(_apply_project_adoption(src, dst, src_archive, dst_archive))
        console.print("[green]Adoption apply completed.[/]")
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc


@file_reservations_app.command("active")
def file_reservations_active(
    project: str = typer.Argument(..., help=PROJECT_IDENTIFIER_HELP),
    limit: int = typer.Option(100, help="Max file_reservations to display"),
) -> None:
    """List active file_reservations with expiry countdowns."""

    async def _run() -> tuple[Project, list[tuple[FileReservation, str]]]:
        project_record = await _get_project_record(project)
        if project_record.id is None:
            raise ValueError(PROJECT_ID_REQUIRED_MESSAGE)
        await ensure_schema()
        async with get_session() as session:
            stmt = (
                select(FileReservation, Agent.name)
                .join(Agent, cast(ColumnElement[bool], FileReservation.agent_id == Agent.id))
                .where(and_(cast(ColumnElement[bool], FileReservation.project_id == project_record.id), cast(ColumnElement[bool], cast(Any, FileReservation.released_ts).is_(None))))
                .order_by(asc(cast(Any, FileReservation.expires_ts)))
                .limit(limit)
            )
            rows = [(row[0], row[1]) for row in (await session.execute(stmt)).all()]
        return project_record, rows

    try:
        project_record, rows = _run_async(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    now = datetime.now(timezone.utc)

    def _fmt_delta(dt: datetime) -> str:
        delta = dt - now
        total = int(delta.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        h, r = divmod(total, 3600)
        m, s = divmod(r, 60)
        return f"{sign}{h:02d}:{m:02d}:{s:02d}"

    table = Table(title=f"Active File Reservations — {project_record.human_key}")
    table.add_column("ID")
    table.add_column("Agent")
    table.add_column("Pattern")
    table.add_column("Exclusive")
    table.add_column("Expires")
    table.add_column("In")

    for file_reservation, agent_name in rows:
        table.add_row(
            str(file_reservation.id),
            agent_name,
            file_reservation.path_pattern,
            "yes" if file_reservation.exclusive else "no",
            _iso(file_reservation.expires_ts),
            _fmt_delta(_ensure_utc_dt(file_reservation.expires_ts) or file_reservation.expires_ts),
        )
    console.print(table)


@file_reservations_app.command("soon")
def file_reservations_soon(
    project: str = typer.Argument(..., help=PROJECT_IDENTIFIER_HELP),
    minutes: int = typer.Option(30, min=1, help="Show file_reservations expiring within N minutes"),
) -> None:
    """Show file_reservations expiring soon to prompt renewals or coordination."""

    async def _run() -> tuple[Project, list[tuple[FileReservation, str]]]:
        project_record = await _get_project_record(project)
        if project_record.id is None:
            raise ValueError(PROJECT_ID_REQUIRED_MESSAGE)
        await ensure_schema()
        async with get_session() as session:
            stmt = (
                select(FileReservation, Agent.name)
                .join(Agent, cast(ColumnElement[bool], FileReservation.agent_id == Agent.id))
                .where(
                    and_(
                        cast(ColumnElement[bool], FileReservation.project_id == project_record.id),
                        cast(ColumnElement[bool], cast(Any, FileReservation.released_ts).is_(None))
                    )
                )
                .order_by(asc(cast(Any, FileReservation.expires_ts)))
            )
            rows = [(row[0], row[1]) for row in (await session.execute(stmt)).all()]
        return project_record, rows

    try:
        project_record, rows = _run_async(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=minutes)
    soon = [(c, a) for (c, a) in rows if (_ensure_utc_dt(c.expires_ts) or c.expires_ts) <= cutoff]

    table = Table(title=f"File Reservations expiring within {minutes}m — {project_record.human_key}", show_lines=False)
    table.add_column("ID")
    table.add_column("Agent")
    table.add_column("Pattern")
    table.add_column("Exclusive")
    table.add_column("Expires")
    table.add_column("In")

    def _fmt_delta(dt: datetime) -> str:
        delta = dt - now
        total = int(delta.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        h, r = divmod(total, 3600)
        m, s = divmod(r, 60)
        return f"{sign}{h:02d}:{m:02d}:{s:02d}"

    for file_reservation, agent_name in soon:
        table.add_row(
            str(file_reservation.id),
            agent_name,
            file_reservation.path_pattern,
            "yes" if file_reservation.exclusive else "no",
            _iso(file_reservation.expires_ts),
            _fmt_delta(_ensure_utc_dt(file_reservation.expires_ts) or file_reservation.expires_ts),
        )
    console.print(table)

@acks_app.command("pending")
def acks_pending(
    project: str = typer.Argument(..., help=PROJECT_IDENTIFIER_HELP),
    agent: str = typer.Argument(..., help=AGENT_NAME_HELP),
    limit: int = typer.Option(20, help=MESSAGE_LIMIT_HELP),
) -> None:
    """List messages that require acknowledgement and are still pending."""

    async def _run() -> tuple[Project, Agent, list[tuple[Message, Any, Any, str]]]:
        project_record = await _get_project_record(project)
        agent_record = await _get_agent_record(project_record, agent)
        if project_record.id is None or agent_record.id is None:
            raise ValueError(PROJECT_AGENT_IDS_REQUIRED_MESSAGE)
        await ensure_schema()
        async with get_session() as session:
            stmt = (
                select(Message, MessageRecipient.read_ts, MessageRecipient.ack_ts, MessageRecipient.kind)
                .join(MessageRecipient, cast(ColumnElement[bool], MessageRecipient.message_id == Message.id))
                .where(
                    and_(
                        cast(ColumnElement[bool], Message.project_id == project_record.id),
                        cast(ColumnElement[bool], MessageRecipient.agent_id == agent_record.id),
                        cast(ColumnElement[bool], cast(Any, Message.ack_required).is_(True)),
                        cast(ColumnElement[bool], cast(Any, MessageRecipient.ack_ts).is_(None))
                    )
                )
                .order_by(desc(cast(Any, Message.created_ts)))
                .limit(limit)
            )
            rows = [(row[0], row[1], row[2], row[3]) for row in (await session.execute(stmt)).all()]
        return project_record, agent_record, rows

    try:
        project_record, agent_record, rows = _run_async(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    table = Table(title=f"Pending ACKs for {agent_record.name} ({project_record.human_key})", show_lines=False)
    table.add_column("Msg ID")
    table.add_column("Thread")
    table.add_column("Subject")
    table.add_column("Kind")
    table.add_column("Created")
    table.add_column("Read")
    table.add_column("Ack Age")

    now = datetime.now(timezone.utc)
    def _age(dt: datetime) -> str:
        # Coerce naive datetimes from SQLite to UTC for arithmetic
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        total = int(delta.total_seconds())
        h, r = divmod(max(total, 0), 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    for message, read_ts, _ack_ts, kind in rows:
        age = _age(message.created_ts)
        table.add_row(
            str(message.id),
            message.thread_id or "",
            message.subject,
            kind,
            _iso(message.created_ts),
            _iso(read_ts) if read_ts else "",
            age,
        )
    console.print(table)


@acks_app.command("remind")
def acks_remind(
    project: str = typer.Argument(..., help=PROJECT_IDENTIFIER_HELP),
    agent: str = typer.Argument(..., help=AGENT_NAME_HELP),
    min_age_minutes: int = typer.Option(30, help="Only show ACK-required older than N minutes"),
    limit: int = typer.Option(50, help=MESSAGE_LIMIT_HELP),
) -> None:
    """Highlight pending acknowledgements older than a threshold."""

    async def _run() -> tuple[Project, Agent, list[tuple[Message, Any, Any, str]]]:
        project_record = await _get_project_record(project)
        agent_record = await _get_agent_record(project_record, agent)
        if project_record.id is None or agent_record.id is None:
            raise ValueError(PROJECT_AGENT_IDS_REQUIRED_MESSAGE)
        await ensure_schema()
        async with get_session() as session:
            stmt = (
                select(Message, MessageRecipient.read_ts, MessageRecipient.ack_ts, MessageRecipient.kind)
                .join(MessageRecipient, cast(ColumnElement[bool], MessageRecipient.message_id == Message.id))
                .where(
                    and_(
                        cast(ColumnElement[bool], Message.project_id == project_record.id),
                        cast(ColumnElement[bool], MessageRecipient.agent_id == agent_record.id),
                        cast(ColumnElement[bool], cast(Any, Message.ack_required).is_(True)),
                        cast(ColumnElement[bool], cast(Any, MessageRecipient.ack_ts).is_(None))
                    )
                )
                .order_by(asc(cast(Any, Message.created_ts)))  # oldest first
                .limit(limit)
            )
            rows = [(row[0], row[1], row[2], row[3]) for row in (await session.execute(stmt)).all()]
        return project_record, agent_record, rows

    try:
        _project_record, agent_record, rows = _run_async(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=min_age_minutes)
    def _aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    stale = [(m, rts, ats, k) for (m, rts, ats, k) in rows if _aware(m.created_ts) <= cutoff]

    table = Table(title=f"ACK Reminders (>{min_age_minutes}m) for {agent_record.name}")
    table.add_column("ID")
    table.add_column("Subject")
    table.add_column("Created")
    table.add_column("Age")
    table.add_column("Kind")
    table.add_column("Read?")

    def _age(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        total = int(delta.total_seconds())
        h, r = divmod(max(total, 0), 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    for msg, read_ts, _ack_ts, kind in stale:
        table.add_row(
            str(msg.id),
            msg.subject,
            _iso(msg.created_ts),
            _age(msg.created_ts),
            kind,
            "yes" if read_ts else "no",
        )
    if not stale:
        console.print("[green]No pending acknowledgements exceed the threshold.[/]")
    else:
        console.print(table)


@acks_app.command("overdue")
def acks_overdue(
    project: str = typer.Argument(..., help=PROJECT_IDENTIFIER_HELP),
    agent: str = typer.Argument(..., help=AGENT_NAME_HELP),
    ttl_minutes: int = typer.Option(60, min=1, help="Only show ACK-required older than N minutes"),
    limit: int = typer.Option(50, help=MESSAGE_LIMIT_HELP),
) -> None:
    """List ack-required messages older than a threshold without acknowledgements."""

    async def _run() -> tuple[Project, Agent, list[tuple[Message, str]]]:
        project_record = await _get_project_record(project)
        agent_record = await _get_agent_record(project_record, agent)
        if project_record.id is None or agent_record.id is None:
            raise ValueError(PROJECT_AGENT_IDS_REQUIRED_MESSAGE)
        await ensure_schema()
        async with get_session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
            stmt = (
                select(Message, MessageRecipient.kind)
                .join(MessageRecipient, cast(ColumnElement[bool], MessageRecipient.message_id == Message.id))
                .where(
                    and_(
                        cast(ColumnElement[bool], Message.project_id == project_record.id),
                        cast(ColumnElement[bool], MessageRecipient.agent_id == agent_record.id),
                        cast(ColumnElement[bool], cast(Any, Message.ack_required).is_(True)),
                        cast(ColumnElement[bool], cast(Any, MessageRecipient.ack_ts).is_(None)),
                        cast(ColumnElement[bool], Message.created_ts <= cutoff)
                    )
                )
                .order_by(asc(cast(Any, Message.created_ts)))
                .limit(limit)
            )
            rows = [(row[0], row[1]) for row in (await session.execute(stmt)).all()]
        return project_record, agent_record, rows

    try:
        project_record, agent_record, rows = _run_async(_run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    table = Table(title=f"ACK Overdue (>{ttl_minutes}m) for {agent_record.name} ({project_record.human_key})")
    table.add_column("ID")
    table.add_column("Subject")
    table.add_column("Created")
    table.add_column("Age")
    table.add_column("Kind")

    now = datetime.now(timezone.utc)
    def _age(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        total = int(delta.total_seconds())
        h, r = divmod(max(total, 0), 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    for msg, kind in rows:
        table.add_row(
            str(msg.id),
            msg.subject,
            _iso(msg.created_ts),
            _age(msg.created_ts),
            kind,
        )
    if not rows:
        console.print("[green]No overdue acknowledgements exceed the threshold.[/]")
    else:
        console.print(table)





@app.command("list-acks")
def list_acks(
    project_key: str = typer.Option(..., "--project", help="Project human key or slug."),
    agent_name: str = typer.Option(..., "--agent", help="Agent name to query."),
    limit: int = typer.Option(20, help="Max messages to show."),
) -> None:
    """List messages requiring acknowledgement for an agent where ack is missing."""

    async def _collect() -> list[tuple[Message, str]]:
        await ensure_schema()
        async with get_session() as session:
            # Resolve project and agent (canonicalize symlink paths)
            try:
                project = await _get_project_record(project_key)
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from exc
            assert project.id is not None
            agent_result = await session.execute(
                select(Agent).where(
                    and_(
                        cast(ColumnElement[bool], Agent.project_id == project.id),
                        func.lower(Agent.name) == agent_name.lower(),
                        cast(
                            ColumnElement[bool],
                            Agent.provisioning_state == "active",
                        ),
                    )
                )
            )
            agent = agent_result.scalars().first()
            if not agent:
                raise typer.BadParameter(f"Agent '{agent_name}' not found in project '{project.human_key}'")
            assert agent.id is not None
            rows = await session.execute(
                select(Message, MessageRecipient.kind)
                .join(MessageRecipient, cast(ColumnElement[bool], MessageRecipient.message_id == Message.id))
                .where(
                    and_(
                        cast(ColumnElement[bool], Message.project_id == project.id),
                        cast(ColumnElement[bool], MessageRecipient.agent_id == agent.id),
                        cast(ColumnElement[bool], cast(Any, Message.ack_required).is_(True)),
                        cast(ColumnElement[bool], cast(Any, MessageRecipient.ack_ts).is_(None))
                    )
                )
                .order_by(desc(cast(Any, Message.created_ts)))
                .limit(limit)
            )
            return [(row[0], row[1]) for row in rows.all()]

    console.rule("[bold blue]Ack-required Messages")
    rows = _run_async(_collect())
    table = Table(title=f"Pending Acks for {agent_name}")
    table.add_column("ID")
    table.add_column("Subject")
    table.add_column("Importance")
    table.add_column("Created")
    for msg, _ in rows:
        table.add_row(str(msg.id or ""), msg.subject, msg.importance, msg.created_ts.isoformat())
    console.print(table)


def _port_config_content(env_path: Path, port: int) -> tuple[str, str]:
    if not env_path.exists():
        return f"HTTP_PORT={port}\n", "Created"
    content = env_path.read_text(encoding="utf-8")
    if re.search(r"^HTTP_PORT=", content, re.MULTILINE):
        return re.sub(r"^HTTP_PORT=.*$", f"HTTP_PORT={port}", content, flags=re.MULTILINE), "Updated"
    if content and not content.endswith("\n"):
        content += "\n"
    return content + f"HTTP_PORT={port}\n", "Added"


@config_app.command("set-port")
def config_set_port(
    port: int = typer.Argument(..., help="HTTP server port number"),
    env_file: Annotated[Optional[Path], typer.Option("--env-file", help="Path to .env file")] = None,
) -> None:
    """Set HTTP_PORT in .env file."""
    if port < 1 or port > 65535:
        console.print(f"[red]Error:[/red] Port must be between 1 and 65535 (got: {port})")
        raise typer.Exit(code=1)

    env_target = env_file if env_file is not None else DEFAULT_ENV_PATH
    env_path = _resolve_path(str(env_target))

    # Ensure parent directory exists
    env_path.parent.mkdir(parents=True, exist_ok=True)

    # Use atomic write pattern: write to temp file, then move
    try:
        new_content, action = _port_config_content(env_path, port)

        # Write to temporary file in same directory (for atomic move)
        temp_fd, temp_path = tempfile.mkstemp(
            dir=env_path.parent, prefix=".env.tmp.", text=True
        )
        try:
            # Write content with secure permissions from the start
            # (best-effort on Windows where Unix permissions don't apply)
            with suppress(OSError, NotImplementedError):
                Path(temp_path).chmod(0o600)

            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                f.write(new_content)

            # Atomic move
            Path(temp_path).replace(env_path)

            # Ensure final permissions are secure (best-effort on Windows)
            with suppress(OSError, NotImplementedError):
                env_path.chmod(0o600)

            console.print(f"[green]✓[/green] {action} HTTP_PORT={port} in {env_path}")
        except (OSError, IOError) as inner_e:
            # Clean up temp file on error
            Path(temp_path).unlink(missing_ok=True)
            raise OSError(f"Failed to write temporary file: {inner_e}") from inner_e

    except PermissionError as e:
        console.print(f"[red]Error:[/red] Permission denied writing to {env_path}")
        raise typer.Exit(code=1) from e
    except OSError as e:
        console.print(f"[red]Error:[/red] Failed to write {env_path}: {e}")
        raise typer.Exit(code=1) from e

    clear_settings_cache()
    console.print("\n[dim]Note: Restart the server for changes to take effect[/dim]")


@config_app.command("show-port")
def config_show_port() -> None:
    """Display the configured HTTP port."""
    settings = get_settings()
    console.print("[cyan]HTTP Server Configuration:[/cyan]")
    console.print(f"  Host: {settings.http.host}")
    console.print(f"  Port: [bold]{settings.http.port}[/bold]")
    console.print(f"  Path: {settings.http.path}")
    console.print(f"\n[dim]Full URL: http://{settings.http.host}:{settings.http.port}{settings.http.path}[/dim]")


# ---------- Documentation helpers ----------

DOC_BLOCK_START = "<!-- MCP_AGENT_MAIL_AND_BEADS_SNIPPET_START -->"
DOC_BLOCK_END = "<!-- MCP_AGENT_MAIL_AND_BEADS_SNIPPET_END -->"
MAIL_SNIPPET_MARKERS = ("<!-- BEGIN_AGENT_MAIL_SNIPPET -->", "<!-- END_AGENT_MAIL_SNIPPET -->")
BEADS_SNIPPET_MARKERS = ("<!-- BEGIN_BEADS_SNIPPET -->", "<!-- END_BEADS_SNIPPET -->")
TARGET_DOC_FILENAMES = {"AGENTS.MD", "CLAUDE.MD"}
SKIP_SCAN_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".tox",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".mcp-agent-mail",
    "node_modules",
    "__pycache__",
    "venv",
    ".venv",
    "dist",
    "build",
    "out",
    "logs",
    "target",
}


@dataclass
class DocCandidate:
    path: Path
    has_snippet: bool


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _extract_readme_section(markers: tuple[str, str]) -> str:
    readme_path = _project_root() / "README.md"
    if not readme_path.exists():
        raise RuntimeError(f"README.md not found at {readme_path}")
    data = readme_path.read_text(encoding="utf-8")
    start_marker, end_marker = markers
    try:
        start_idx = data.index(start_marker) + len(start_marker)
        end_idx = data.index(end_marker, start_idx)
    except ValueError as exc:  # pragma: no cover - defensive branch
        raise RuntimeError(f"Could not locate snippet markers {markers[0]}..{markers[1]} in README.md") from exc
    snippet = data[start_idx:end_idx].strip()
    return _strip_code_block(snippet)


def _strip_code_block(snippet: str) -> str:
    stripped = snippet.strip()
    if stripped.startswith("```"):
        stripped = "\n".join(stripped.splitlines()[1:])
    stripped = stripped.rstrip()
    if stripped.endswith("```"):
        stripped = "\n".join(stripped.splitlines()[:-1])
    return stripped.strip()


def _combined_doc_snippet() -> str:
    mail = _extract_readme_section(MAIL_SNIPPET_MARKERS).strip()
    beads = _extract_readme_section(BEADS_SNIPPET_MARKERS).strip()
    combined = f"{mail}\n\n{beads}".strip()
    return combined + "\n"


def _default_scan_roots() -> list[Path]:
    cwd = Path.cwd().resolve()
    home = Path.home()
    candidates: list[Path] = [cwd]
    if cwd.parent != cwd:
        candidates.append(cwd.parent)
    for rel in ("code", "codes", "projects", "workspace", "repos", "src"):
        candidate = (home / rel).expanduser()
        if candidate.exists():
            candidates.append(candidate)
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen or not resolved.exists() or not resolved.is_dir():
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped or [cwd]


def _iter_doc_files(base: Path, max_depth: int) -> Iterable[Path]:
    origin = base.resolve()
    base_parts = len(origin.parts)

    def _on_error(error: OSError) -> None:  # pragma: no cover - best effort logging
        console.print(f"[yellow]Warning:[/yellow] Skipping {error.filename}: {error.strerror}")

    for dirpath, dirnames, filenames in os.walk(
        origin, topdown=True, followlinks=False, onerror=_on_error
    ):
        current_depth = len(Path(dirpath).parts) - base_parts
        if max_depth >= 0 and current_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in SKIP_SCAN_DIRS]
        for name in filenames:
            if name.upper() in TARGET_DOC_FILENAMES:
                yield Path(dirpath) / name


def _collect_doc_candidates(roots: Sequence[Path], max_depth: int) -> list[DocCandidate]:
    seen: set[Path] = set()
    candidates: list[DocCandidate] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for file_path in _iter_doc_files(root, max_depth):
            resolved = file_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                text = resolved.read_text(encoding="utf-8")
            except OSError as exc:
                console.print(f"[yellow]Warning:[/yellow] Could not read {resolved}: {exc}")
                continue
            candidates.append(DocCandidate(path=resolved, has_snippet=DOC_BLOCK_START in text))
    return sorted(candidates, key=lambda c: str(c.path).lower())


def _append_snippet_to_doc(path: Path, snippet: str, allowed_roots: Sequence[Path]) -> None:
    # Confine writes to the scanned roots so a crafted candidate path cannot
    # redirect the snippet into an unrelated file.
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in allowed_roots):
        raise RuntimeError(f"Refusing to write outside the scanned directories: {resolved}")
    try:
        content = resolved.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - IO failure protection
        raise RuntimeError(f"Failed to read {resolved}: {exc}") from exc
    if content and not content.endswith("\n"):
        content += "\n"
    addition = f"\n{DOC_BLOCK_START}\n\n{snippet}\n\n{DOC_BLOCK_END}\n"
    try:
        resolved.write_text(content + addition, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - IO failure protection
        raise RuntimeError(f"Failed to write {resolved}: {exc}") from exc


def _insert_doc_candidate(
    candidate: DocCandidate,
    snippet: str,
    roots: Sequence[Path],
    *,
    yes: bool,
    dry_run: bool,
) -> tuple[int, int]:
    if candidate.has_snippet:
        console.print(f"[dim]Skipping {candidate.path} (snippet already present).[/dim]")
        return 0, 0
    prompt = f"Insert Agent Mail + Beads snippet into {candidate.path}?"
    if not yes and not typer.confirm(prompt, default=True):
        console.print(f"[yellow]Skipped {candidate.path}[/yellow]")
        return 0, 1
    if dry_run:
        console.print(f"[yellow]Dry run:[/yellow] would insert snippet into {candidate.path}")
        return 0, 0
    _append_snippet_to_doc(candidate.path, snippet, roots)
    console.print(f"[green]Inserted snippet into {candidate.path}[/green]")
    return 1, 0


@docs_app.command("insert-blurbs")
def docs_insert_blurbs(
    scan_dir: Annotated[
        Optional[List[Path]], typer.Option("--scan-dir", "-d", help="Directories to scan (repeatable).")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Automatically confirm insertion for each file.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show actions without modifying files.")] = False,
    max_depth: Annotated[
        int,
        typer.Option(
            "--max-depth",
            min=1,
            help="Maximum directory depth to explore under each scan root (default: 6).",
        ),
    ] = 6,
) -> None:
    """Detect AGENTS.md/CLAUDE.md files and append the latest Agent Mail + Beads blurbs."""

    snippet = _combined_doc_snippet()
    roots = [path.expanduser().resolve() for path in scan_dir if path] if scan_dir else _default_scan_roots()
    roots = [path for path in roots if path.exists() and path.is_dir()]
    if not roots:
        console.print("[red]Error:[/red] No valid scan directories were provided.")
        raise typer.Exit(code=1)

    console.print("[cyan]Scanning for AGENTS.md / CLAUDE.md files in:[/cyan]")
    for root in roots:
        console.print(f"  • {root}")

    candidates = _collect_doc_candidates(roots, max_depth=max_depth)
    if not candidates:
        console.print(
            "[yellow]No AGENTS.md or CLAUDE.md files found. Provide additional roots with --scan-dir.[/yellow]"
        )
        return

    table = Table(title="Detected Agent Instructions", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("File")
    table.add_column("Project")
    table.add_column("Status")
    for idx, candidate in enumerate(candidates, start=1):
        status = "has snippet" if candidate.has_snippet else "needs snippet"
        table.add_row(str(idx), candidate.path.name, str(candidate.path.parent), status)
    console.print(table)

    inserted = 0
    skipped = 0
    for candidate in candidates:
        candidate_inserted, candidate_skipped = _insert_doc_candidate(
            candidate, snippet, roots, yes=yes, dry_run=dry_run
        )
        inserted += candidate_inserted
        skipped += candidate_skipped

    if dry_run:
        console.print("\n[dim]Dry run complete. Rerun without --dry-run to apply the changes.[/dim]")
    else:
        console.print(
            f"\n[cyan]Summary:[/cyan] inserted into {inserted} file(s); skipped {skipped} file(s); "
            "other files already had the snippet."
        )


# =============================================================================
# Doctor Commands - Diagnose and repair mailbox health
# =============================================================================


@dataclass
class DiagnosticResult:
    """Result of a single diagnostic check."""

    name: str
    status: str  # "ok", "warning", "error", "info"
    message: str
    details: list[str] | None = None
    repair_available: bool = False


async def _resolve_doctor_project(project_identifier: str | None) -> Project | None:
    if not project_identifier:
        return None
    return await _get_project_record(project_identifier)


def _doctor_lock_diagnostic(settings: Any, project_slug: str | None) -> DiagnosticResult:
    from .storage import collect_lock_status

    lock_status = collect_lock_status(settings, project_slug=project_slug)
    stale_locks = [
        cast(str, lock.get("path"))
        for lock in lock_status.get("locks", [])
        if lock.get("stale_suspected") and isinstance(lock.get("path"), str)
    ]
    if stale_locks:
        return DiagnosticResult(
            name="Locks", status="warning", message=f"{len(stale_locks)} stale lock(s) found",
            details=[str(lock) for lock in stale_locks], repair_available=True,
        )
    return DiagnosticResult(name="Locks", status="ok", message="No stale locks found")


def _doctor_database_integrity(db_path: Path | None) -> DiagnosticResult:
    if not db_path or not db_path.exists():
        return DiagnosticResult(
            name="Database", status="info", message="No SQLite database found (may be using different backend)",
        )
    try:
        # Diagnostics run against live deployments and must never become a second writer.
        conn = connect_sqlite_readonly(db_path)
        try:
            integrity_result = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        if integrity_result and integrity_result[0] == "ok":
            return DiagnosticResult(name="Database", status="ok", message="Database integrity check passed")
        return DiagnosticResult(
            name="Database", status="error", message="Database integrity check failed",
            details=[str(integrity_result)], repair_available=False,
        )
    except Exception as exc:
        return DiagnosticResult(name="Database", status="error", message=f"Database check failed: {exc}")


async def _doctor_orphan_records(session: Any, project_id: int | None) -> DiagnosticResult:
    if project_id is None:
        result = await session.execute(text("""
            SELECT COUNT(*) FROM message_recipients mr
            WHERE NOT EXISTS (SELECT 1 FROM agents a WHERE a.id = mr.agent_id)
        """))
    else:
        result = await session.execute(text("""
            SELECT COUNT(*)
            FROM message_recipients mr
            JOIN messages m ON m.id = mr.message_id
            WHERE m.project_id = :pid
            AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.id = mr.agent_id)
        """), {"pid": project_id})
    orphan_count = result.scalar() or 0
    if orphan_count > 0:
        return DiagnosticResult(
            name="Orphaned Records", status="warning",
            message=f"{orphan_count} orphaned message recipient(s) found", repair_available=True,
        )
    return DiagnosticResult(name="Orphaned Records", status="ok", message="No orphaned records found")


async def _doctor_fts_index(session: Any, project_id: int | None) -> DiagnosticResult | None:
    if project_id is None:
        result = await session.execute(text("""
            SELECT
                (SELECT COUNT(*) FROM messages) as msg_count,
                (SELECT COUNT(*) FROM fts_messages) as fts_count
        """))
    else:
        result = await session.execute(text("""
            SELECT
                (SELECT COUNT(*) FROM messages WHERE project_id = :pid) as msg_count,
                (
                    SELECT COUNT(*) FROM fts_messages
                    JOIN messages m ON m.id = fts_messages.rowid
                    WHERE m.project_id = :pid
                ) as fts_count
        """), {"pid": project_id})
    counts = result.fetchone()
    if not counts:
        return None
    msg_count, fts_count = counts
    if msg_count == fts_count:
        return DiagnosticResult(name="FTS Index", status="ok", message=f"FTS index synchronized ({msg_count} messages)")
    return DiagnosticResult(
        name="FTS Index", status="warning",
        message=f"FTS index mismatch: {msg_count} messages vs {fts_count} FTS entries", repair_available=True,
    )


async def _doctor_expired_reservations(session: Any, project_id: int | None) -> DiagnosticResult:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expired_conditions = [
        cast(ColumnElement[bool], cast(Any, FileReservation.released_ts).is_(None)),
        cast(ColumnElement[bool], cast(Any, FileReservation.expires_ts) < now),
    ]
    if project_id is not None:
        expired_conditions.append(cast(ColumnElement[bool], FileReservation.project_id == project_id))
    expired_query = select(func.count()).select_from(FileReservation).where(and_(*expired_conditions))
    result = await session.execute(expired_query)
    expired_count = result.scalar() or 0
    if expired_count > 0:
        return DiagnosticResult(
            name="File Reservations", status="info",
            message=f"{expired_count} expired reservation(s) pending cleanup", repair_available=True,
        )
    return DiagnosticResult(name="File Reservations", status="ok", message="No expired reservations")


def _doctor_wal_files(db_path: Path) -> DiagnosticResult:
    orphan_files = [str(path) for path in get_sqlite_sidecar_paths(db_path) if path.exists()]
    if orphan_files:
        return DiagnosticResult(
            name="WAL Files", status="info",
            message=f"{len(orphan_files)} WAL/SHM file(s) present (normal during operation)", details=orphan_files,
        )
    return DiagnosticResult(name="WAL Files", status="ok", message="No orphan WAL/SHM files")


async def _run_doctor_diagnostics(project: str | None) -> list[DiagnosticResult]:
    from .db import get_database_path

    settings = get_settings()
    await ensure_schema()
    project_record = await _resolve_doctor_project(project)
    project_id = project_record.id if project_record is not None else None
    project_slug = project_record.slug if project_record is not None else None
    results = [_doctor_lock_diagnostic(settings, project_slug)]
    db_path = get_database_path(settings)
    results.append(_doctor_database_integrity(db_path))
    async with get_session() as session:
        results.append(await _doctor_orphan_records(session, project_id))
        fts_result = await _doctor_fts_index(session, project_id)
        if fts_result is not None:
            results.append(fts_result)
        results.append(await _doctor_expired_reservations(session, project_id))
    if db_path and db_path.exists():
        results.append(_doctor_wal_files(db_path))
    return results


@doctor_app.command("check")
def doctor_check(
    project: Annotated[
        Optional[str],
        typer.Argument(help="Project slug or human key (optional - checks all if not specified)"),
    ] = None,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed diagnostic output"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Run comprehensive diagnostics on mailbox and agent state.

    Checks:
    - Lock files (stale archive/commit locks)
    - Database integrity (FK constraints, FTS index, orphaned records)
    - Archive-DB synchronization
    - File reservations (expired, conflicts)
    - Attachments (orphaned files/manifests)
    """

    try:
        diagnostics = _run_async(_run_doctor_diagnostics(project))
    except Exception as exc:
        if json_output:
            console.print_json(json.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Error running diagnostics:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        output = {
            "diagnostics": [
                {
                    "name": d.name,
                    "status": d.status,
                    "message": d.message,
                    "details": d.details,
                    "repair_available": d.repair_available,
                }
                for d in diagnostics
            ],
            "summary": {
                "errors": sum(1 for d in diagnostics if d.status == "error"),
                "warnings": sum(1 for d in diagnostics if d.status == "warning"),
                "info": sum(1 for d in diagnostics if d.status == "info"),
                "ok": sum(1 for d in diagnostics if d.status == "ok"),
            },
        }
        console.print_json(json.dumps(output))
        return

    # Rich table output
    console.print("\n[bold cyan]MCP Agent Mail Doctor - Diagnostic Report[/bold cyan]")
    console.print("=" * 50)

    if project:
        console.print(f"Project: {project}\n")

    table = Table(show_header=True)
    table.add_column("Check", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Details")

    status_colors = {
        "ok": "[green]OK[/green]",
        "warning": "[yellow]WARN[/yellow]",
        "error": "[red]ERROR[/red]",
        "info": "[blue]INFO[/blue]",
    }

    for diag in diagnostics:
        status_display = status_colors.get(diag.status, diag.status)
        details = diag.message
        if verbose and diag.details:
            details += "\n" + "\n".join(f"  • {d}" for d in diag.details[:5])
        table.add_row(diag.name, status_display, details)

    console.print(table)

    # Summary
    errors = sum(1 for d in diagnostics if d.status == "error")
    warnings = sum(1 for d in diagnostics if d.status == "warning")
    info = sum(1 for d in diagnostics if d.status == "info")

    console.print()
    if errors > 0 or warnings > 0:
        console.print(f"[bold]Summary:[/bold] {errors} error(s), {warnings} warning(s), {info} info")
        console.print("\nRun [cyan]am doctor repair[/cyan] to fix issues")
    else:
        console.print("[green]All checks passed![/green]")


async def _doctor_heal_locks(settings: Any, project_slug: str | None, repairs: dict[str, Any], *, dry_run: bool) -> None:
    from .storage import heal_archive_locks

    if dry_run:
        console.print("  [dim]Would heal stale locks[/dim]")
        repairs["safe_repairs"].append({"action": "heal_locks", "dry_run": True})
        return
    try:
        lock_result = await heal_archive_locks(settings, project_slug=project_slug)
        locks_removed = lock_result["locks_removed"]
        metadata_removed = lock_result["metadata_removed"]
        if locks_removed:
            console.print(f"  [green]Healed {len(locks_removed)} stale lock(s)[/green]")
        if metadata_removed:
            console.print(f"  [green]Removed {len(metadata_removed)} orphaned lock metadata file(s)[/green]")
        if not locks_removed and not metadata_removed:
            console.print("  [dim]No stale locks to heal[/dim]")
        repairs["safe_repairs"].append({
            "action": "heal_locks", "locks_removed": locks_removed, "metadata_removed": metadata_removed,
        })
    except Exception as exc:
        repairs["errors"].append(f"Lock healing failed: {exc}")
        console.print(f"  [red]Lock healing failed:[/red] {exc}")


async def _doctor_release_expired(project_id: int | None, repairs: dict[str, Any], *, dry_run: bool) -> None:
    from sqlalchemy import update

    async with get_session() as session:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expired_conditions = [
            cast(ColumnElement[bool], cast(Any, FileReservation.released_ts).is_(None)),
            cast(ColumnElement[bool], cast(Any, FileReservation.expires_ts) < now),
        ]
        if project_id is not None:
            expired_conditions.append(cast(ColumnElement[bool], FileReservation.project_id == project_id))
        if dry_run:
            result = await session.execute(select(func.count()).select_from(FileReservation).where(and_(*expired_conditions)))
            count = result.scalar() or 0
            console.print(f"  [dim]Would release {count} expired reservation(s)[/dim]")
            repairs["safe_repairs"].append({"action": "release_expired", "count": count, "dry_run": True})
            return
        result = await session.execute(update(FileReservation).where(and_(*expired_conditions)).values(released_ts=now))
        await session.commit()
        released = int(getattr(result, "rowcount", 0) or 0)
        if released > 0:
            console.print(f"  [green]Released {released} expired reservation(s)[/green]")
        else:
            console.print("  [dim]No expired reservations to release[/dim]")
        repairs["safe_repairs"].append({"action": "release_expired", "released": released})


async def _doctor_delete_orphans(session: Any, project_id: int | None) -> None:
    if project_id is None:
        await session.execute(text("""
            DELETE FROM message_recipients
            WHERE NOT EXISTS (SELECT 1 FROM agents a WHERE a.id = message_recipients.agent_id)
        """))
    else:
        await session.execute(text("""
            DELETE FROM message_recipients
            WHERE message_id IN (SELECT m.id FROM messages m WHERE m.project_id = :pid)
            AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.id = message_recipients.agent_id)
        """), {"pid": project_id})
    await session.commit()


async def _doctor_clean_orphans(project_id: int | None, repairs: dict[str, Any], *, dry_run: bool, yes: bool) -> None:
    async with get_session() as session:
        if project_id is None:
            result = await session.execute(text("""
                SELECT COUNT(*) FROM message_recipients mr
                WHERE NOT EXISTS (SELECT 1 FROM agents a WHERE a.id = mr.agent_id)
            """))
        else:
            result = await session.execute(text("""
                SELECT COUNT(*) FROM message_recipients mr
                JOIN messages m ON m.id = mr.message_id
                WHERE m.project_id = :pid
                AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.id = mr.agent_id)
            """), {"pid": project_id})
        orphan_count = result.scalar() or 0
        if orphan_count <= 0:
            console.print("  [dim]No orphaned records to clean[/dim]")
        elif dry_run:
            console.print(f"  [dim]Would delete {orphan_count} orphaned recipient record(s)[/dim]")
            repairs["data_repairs"].append({"action": "delete_orphans", "count": orphan_count, "dry_run": True})
        elif yes or typer.confirm(f"  Delete {orphan_count} orphaned message recipient record(s)?", default=False):
            await _doctor_delete_orphans(session, project_id)
            console.print(f"  [green]Deleted {orphan_count} orphaned record(s)[/green]")
            repairs["data_repairs"].append({"action": "delete_orphans", "deleted": orphan_count})
        else:
            console.print("  [yellow]Skipped orphan cleanup[/yellow]")
            repairs["data_repairs"].append({"action": "delete_orphans", "skipped": True})


async def _run_doctor_repairs(project: str | None, backup_dir: Path | None, *, dry_run: bool, yes: bool) -> dict[str, Any]:
    from .storage import create_diagnostic_backup

    settings = get_settings()
    await ensure_schema()
    project_record = await _resolve_doctor_project(project)
    project_id = project_record.id if project_record is not None else None
    project_slug = project_record.slug if project_record is not None else None
    repairs: dict[str, Any] = {"backup_path": None, "safe_repairs": [], "data_repairs": [], "errors": []}
    if not dry_run:
        console.print("[cyan]Creating backup before repairs...[/cyan]")
        try:
            backup_path = await create_diagnostic_backup(settings, backup_dir=backup_dir, reason="doctor-repair")
            repairs["backup_path"] = str(backup_path)
            console.print(f"[green]Backup created:[/green] {backup_path}")
        except Exception as exc:
            raise RuntimeError(f"Backup failed: {exc}") from exc

    console.print("\n[bold]Safe Repairs (auto-applied):[/bold]")
    await _doctor_heal_locks(settings, project_slug, repairs, dry_run=dry_run)
    await _doctor_release_expired(project_id, repairs, dry_run=dry_run)
    console.print("\n[bold]Data Repairs (require confirmation):[/bold]")
    await _doctor_clean_orphans(project_id, repairs, dry_run=dry_run, yes=yes)
    return repairs


@doctor_app.command("repair")
def doctor_repair(
    project: Annotated[
        Optional[str],
        typer.Argument(help="Project slug or human key (optional)"),
    ] = None,
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without executing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts"),
    backup_dir: Annotated[
        Optional[Path],
        typer.Option("--backup-dir", help="Directory for backups (default: storage_root/backups)"),
    ] = None,
) -> None:
    """Repair common mailbox issues.

    Semi-automatic mode (default):
    - Auto-fixes safe issues: stale locks, expired file reservations
    - Prompts for confirmation on data-affecting repairs

    Creates a backup before any destructive operation and aborts if backup creation fails.
    """

    try:
        results = _run_async(_run_doctor_repairs(project, backup_dir, dry_run=dry_run, yes=yes))
    except Exception as exc:
        console.print(f"[red]Error during repair:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    # Summary
    console.print("\n[bold]Repair Summary:[/bold]")
    if results.get("backup_path"):
        console.print(f"  Backup: {results['backup_path']}")
    safe_count = len(results.get("safe_repairs", []))
    data_count = len(results.get("data_repairs", []))
    error_count = len(results.get("errors", []))
    console.print(f"  Safe repairs: {safe_count}")
    console.print(f"  Data repairs: {data_count}")
    if error_count > 0:
        console.print(f"  [red]Errors: {error_count}[/red]")
        raise typer.Exit(code=1)


@doctor_app.command("backups")
def doctor_backups(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List available diagnostic backups."""

    async def _run() -> list[dict[str, Any]]:
        from .storage import list_backups

        settings = get_settings()
        return await list_backups(settings)

    try:
        backups = _run_async(_run())
    except Exception as exc:
        if json_output:
            console.print_json(json.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Failed to list backups:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(json.dumps(backups))
        return

    if not backups:
        console.print("[dim]No backups found[/dim]")
        return

    table = Table(title="Available Backups")
    table.add_column("Created", style="cyan")
    table.add_column("Reason")
    table.add_column("Size", justify="right")
    table.add_column("Database", justify="center")
    table.add_column("Bundles", justify="right")
    table.add_column("Path")

    for backup in backups:
        size_mb = (backup.get("size_bytes", 0) / 1024 / 1024)
        table.add_row(
            backup.get("created_at", "")[:19],
            backup.get("reason", ""),
            f"{size_mb:.1f} MB",
            "[green]Yes[/green]" if backup.get("has_database") else "[dim]No[/dim]",
            str(backup.get("bundle_count", 0)),
            backup.get("path", ""),
        )

    console.print(table)


def _validate_doctor_backup_artifact(backup_path: Path, artifact_ref: str) -> None:
    from .storage import _resolve_backup_file_artifact

    try:
        _resolve_backup_file_artifact(backup_path, artifact_ref)
    except FileNotFoundError as exc:
        raise ValueError(f"manifest.json references missing artifact: {artifact_ref}") from exc
    except IsADirectoryError as exc:
        raise ValueError(f"manifest.json artifact is not a file: {artifact_ref}") from exc


def _load_doctor_backup_manifest(backup_path: Path, manifest_path: Path) -> BackupManifest:
    from .storage import _parse_backup_manifest

    try:
        with manifest_path.open(encoding="utf-8") as manifest_file:
            manifest = _parse_backup_manifest(json.load(manifest_file))
        if manifest.database_path is not None:
            _validate_doctor_backup_artifact(backup_path, manifest.database_path)
        for bundle_ref in manifest.project_bundles:
            _validate_doctor_backup_artifact(backup_path, bundle_ref)
        return manifest
    except (OSError, ValueError) as exc:
        console.print(f"[red]Invalid backup manifest:[/red] {exc}")
        raise typer.Exit(code=1) from exc


async def _restore_doctor_backup(backup_path: Path, *, dry_run: bool) -> dict[str, Any]:
    from .db import get_database_path
    from .storage import create_diagnostic_backup, restore_from_backup

    settings = get_settings()
    if dry_run:
        return await restore_from_backup(settings, backup_path, dry_run=True)
    pre_restore_backup: Path | None = None
    current_db_path = get_database_path(settings)
    current_archive_root = await asyncio.to_thread(lambda: Path(settings.storage.root).expanduser().resolve())
    has_current_db = bool(current_db_path and await asyncio.to_thread(current_db_path.exists))
    has_current_archive = await asyncio.to_thread((current_archive_root / ".git").exists)
    if has_current_db or has_current_archive:
        pre_restore_backup = await create_diagnostic_backup(settings, reason="pre-restore")
    restore_result = await restore_from_backup(settings, backup_path, dry_run=False)
    if pre_restore_backup is not None:
        restore_result["pre_restore_backup_path"] = str(pre_restore_backup)
    else:
        restore_result["pre_restore_backup_skipped_reason"] = "no current database or archive found"
    return restore_result


def _print_doctor_restore_preview(result: dict[str, Any]) -> None:
    preview_errors = list(result.get("errors", []))
    if preview_errors:
        console.print("\n[bold red]Dry run found restore blockers:[/bold red]")
    else:
        console.print("\n[bold]Would restore:[/bold]")
    if result.get("would_restore_database"):
        console.print("  - Database")
    for bundle in result.get("would_restore_bundles", []):
        console.print(f"  - Bundle: {bundle}")
    for error in preview_errors:
        console.print(f"  [red]Error:[/red] {error}")
    if preview_errors:
        raise typer.Exit(code=1)


def _print_doctor_restore_result(result: dict[str, Any]) -> None:
    restore_errors = list(result.get("errors", []))
    if restore_errors:
        console.print("\n[bold red]Restore completed with errors:[/bold red]")
    else:
        console.print("\n[bold]Restore complete:[/bold]")
    if result.get("pre_restore_backup_path"):
        console.print(f"  [cyan]Pre-restore backup:[/cyan] {result['pre_restore_backup_path']}")
    elif result.get("pre_restore_backup_skipped_reason"):
        console.print(f"  [dim]Pre-restore backup skipped:[/dim] {result['pre_restore_backup_skipped_reason']}")
    if result.get("database_restored"):
        console.print("  [green]Database restored[/green]")
    for bundle in result.get("bundles_restored", []):
        console.print(f"  [green]Bundle restored:[/green] {bundle}")
    for error in restore_errors:
        console.print(f"  [red]Error:[/red] {error}")
    if restore_errors:
        raise typer.Exit(code=1)


@doctor_app.command("restore")
def doctor_restore(
    backup_path: Annotated[
        Path,
        typer.Argument(help="Path to backup directory to restore from"),
    ],
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview what would be restored"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts"),
) -> None:
    """Restore from a diagnostic backup.

    WARNING: This will overwrite current database and archive.
    A pre-restore backup will be created automatically.
    """
    if not backup_path.exists():
        console.print(f"[red]Backup path not found:[/red] {backup_path}")
        raise typer.Exit(code=1)

    manifest_path = backup_path / SHARE_MANIFEST_FILENAME
    if not manifest_path.exists():
        console.print(f"[red]Invalid backup:[/red] No manifest.json found in {backup_path}")
        raise typer.Exit(code=1)

    manifest = _load_doctor_backup_manifest(backup_path, manifest_path)

    console.print("\n[bold cyan]Restore from Backup[/bold cyan]")
    console.print(f"  Created: {manifest.created_at}")
    console.print(f"  Reason: {manifest.reason}")
    console.print(f"  Has database: {'Yes' if manifest.database_path else 'No'}")
    console.print(f"  Bundles: {len(manifest.project_bundles)}")

    if dry_run:
        console.print("\n[yellow]Dry run - no changes will be made[/yellow]")

    if not dry_run and not yes:
        console.print("\n[red]WARNING:[/red] This will overwrite your current database and archive!")
        if not typer.confirm("Continue with restore?", default=False):
            console.print("[yellow]Restore cancelled[/yellow]")
            return

    try:
        result = _run_async(_restore_doctor_backup(backup_path, dry_run=dry_run))
    except Exception as exc:
        console.print(f"[red]Restore failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if dry_run:
        _print_doctor_restore_preview(result)
    else:
        _print_doctor_restore_result(result)


async def _ticket_rows_with_assignees(session: Any, rows: Sequence[Ticket]) -> list[tuple[Ticket, str | None]]:
    names: dict[int, str] = {}
    wanted = {row.assignee_agent_id for row in rows if row.assignee_agent_id}
    if wanted:
        found = await session.execute(select(Agent.id, Agent.name).where(cast(Any, Agent.id).in_(wanted)))
        names = {row[0]: row[1] for row in found.all()}
    return [(row, names.get(row.assignee_agent_id) if row.assignee_agent_id else None) for row in rows]


@tickets_app.command("list")
def tickets_list(
    project: str = typer.Argument(..., help=PROJECT_IDENTIFIER_HELP),
    status: Optional[str] = typer.Option(None, "--status", help="open | in_progress | closed"),
    kind: Optional[str] = typer.Option(None, "--kind", help="epic | task | bug | chore"),
    assignee: Optional[str] = typer.Option(None, "--assignee", help="Restrict to one agent"),
    epic: Optional[str] = typer.Option(None, "--epic", help="Restrict to one epic's children"),
    include_closed: bool = typer.Option(
        False, "--include-closed", help="Include closed tickets"
    ),
    limit: int = typer.Option(50, "--limit", help="Maximum tickets to display (1-500)"),
    json_output: bool = typer.Option(False, "--json", help=JSON_OUTPUT_HELP),
) -> None:
    """List a project's tickets, most urgent first."""

    async def _collect() -> tuple[Project, list[tuple[Ticket, Optional[str]]]]:
        project_record = await _get_project_record(project)
        async with get_session() as session:
            parent_id: Optional[int] = None
            if epic:
                parent_id = (await tickets.load_ticket(session, ticket_key=epic)).id
            assignee_id: Optional[int] = None
            if assignee:
                found = await session.execute(
                    select(Agent.id).where(
                        cast(ColumnElement[bool], Agent.project_id == project_record.id),
                        cast(ColumnElement[bool], Agent.name == assignee),
                    )
                )
                resolved = found.scalars().first()
                if resolved is None:
                    raise ValueError(f"No agent {assignee!r} in {project_record.human_key}")
                assignee_id = resolved
            rows = await tickets.list_tickets(
                session,
                project_id=cast(int, project_record.id),
                status_filter=status,
                kind_filter=kind,
                assignee_agent_id=assignee_id,
                parent_id=parent_id,
                include_closed=include_closed,
                limit=limit,
            )
            return project_record, await _ticket_rows_with_assignees(session, rows)

    try:
        project_record, rows = _run_async(_collect())
    except Exception as exc:
        if json_output:
            console.print_json(json.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Failed to list tickets:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(
            json.dumps(
                [
                    {**tickets.ticket_to_dict(row), "assignee": assignee_name}
                    for row, assignee_name in rows
                ]
            )
        )
        return

    _print_ticket_list(project_record, rows)


def _print_ticket_list(project_record: Project, rows: list[tuple[Ticket, str | None]]) -> None:
    table = Table(title=f"Tickets — {project_record.human_key}")
    table.add_column("Key")
    table.add_column("Kind")
    table.add_column("Pri")
    table.add_column("Status")
    table.add_column("Assignee")
    table.add_column("Title")
    for row, assignee_name in rows:
        table.add_row(
            row.key,
            row.kind_key,
            str(row.priority),
            row.status_key if row.closed_ts is None else f"{row.status_key} ({row.resolution_key})",
            assignee_name or "-",
            row.title,
        )
    console.print(table)


@tickets_app.command("show")
def tickets_show(
    ticket_key: str = typer.Argument(..., help="Ticket key, e.g. AM-12"),
    json_output: bool = typer.Option(False, "--json", help=JSON_OUTPUT_HELP),
) -> None:
    """Show one ticket with its links and change history."""

    async def _collect() -> dict[str, Any]:
        await ensure_schema()
        async with get_session() as session:
            ticket = await tickets.load_ticket(session, ticket_key=ticket_key)
            outgoing = await tickets.outgoing_links(session, ticket_id=cast(int, ticket.id))
            incoming = await tickets.incoming_links(session, ticket_key=ticket.key)
            events = (
                await session.execute(
                    select(TicketEvent)
                    .where(cast(ColumnElement[bool], TicketEvent.ticket_id == ticket.id))
                    .order_by(cast(Any, TicketEvent.id).asc())
                )
            ).scalars().all()
            return {
                "ticket": tickets.ticket_to_dict(ticket),
                "links": [
                    {
                        "direction": "outgoing",
                        "relation": link.relation,
                        "target_kind": link.target_kind,
                        "target_ref": link.target_ref,
                        "available": await tickets.link_target_exists(
                            session, target_kind=link.target_kind, target_ref=link.target_ref
                        ),
                    }
                    for link in outgoing
                ]
                + [
                    {
                        "direction": "incoming",
                        "relation": link.relation,
                        "target_kind": "ticket",
                        "target_ref": source_key,
                        "available": True,
                    }
                    for source_key, link in incoming
                ],
                "events": [
                    {
                        "event_type": event.event_type,
                        "field_name": event.field_name,
                        "old_value": event.old_value,
                        "new_value": event.new_value,
                        "actor": event.actor_label,
                        "created_ts": _iso(event.created_ts),
                    }
                    for event in events
                ],
            }

    try:
        payload = _run_async(_collect())
    except Exception as exc:
        if json_output:
            console.print_json(json.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Failed to show ticket:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        console.print_json(json.dumps(payload))
        return

    ticket_payload = payload["ticket"]
    table = Table(title=f"{ticket_payload['key']} — {ticket_payload['title']}")
    table.add_column("Field")
    table.add_column("Value")
    for field_name in ("kind", "status", "resolution", "priority", "reporter_label", "revision"):
        table.add_row(field_name, str(ticket_payload.get(field_name)))
    table.add_row("created", ticket_payload["created_ts"])
    table.add_row("updated", ticket_payload["updated_ts"])
    table.add_row("discussion_thread_id", ticket_payload["discussion_thread_id"])
    console.print(table)
    _print_ticket_links(payload["links"])
    _print_ticket_history(payload["events"])


def _print_ticket_links(ticket_links: list[dict[str, Any]]) -> None:
    if ticket_links:
        links = Table(title="Links")
        links.add_column("Direction")
        links.add_column("Relation")
        links.add_column("Target")
        links.add_column("Available")
        for link in ticket_links:
            links.add_row(
                link["direction"],
                link["relation"],
                f"{link['target_kind']}:{link['target_ref']}",
                "yes" if link["available"] else "no",
            )
        console.print(links)


def _print_ticket_history(events: list[dict[str, Any]]) -> None:
    if events:
        history = Table(title="History")
        history.add_column("When")
        history.add_column("Event")
        history.add_column("Field")
        history.add_column("Change")
        history.add_column("Actor")
        for event in events:
            change = ""
            if event["old_value"] is not None or event["new_value"] is not None:
                change = f"{event['old_value']} -> {event['new_value']}"
            history.add_row(
                event["created_ts"],
                event["event_type"],
                event["field_name"] or "-",
                change,
                event["actor"] or "-",
            )
        console.print(history)
if __name__ == "__main__":
    app()
