"""HTTP transport helpers wrapping FastMCP with FastAPI."""

# ruff: noqa: RUF001 -- Natural-language catalogs intentionally contain native confusables.

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import contextvars
import functools
import hashlib
import hmac
import importlib
import json
import logging
import math
import re
import threading
import unicodedata
from collections.abc import Callable, MutableMapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, NamedTuple, Protocol, TypedDict, cast
from urllib.parse import (
    parse_qsl,
    quote,
    unquote_to_bytes,
    urlencode,
    urlsplit,
    urlunsplit,
)

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Path as FastApiPath, Query, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from git import NULL_TREE
from markupsafe import Markup, escape as escape_markup
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select, text, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import Receive, Scope, Send

from . import webauth
from .app import (
    _agent_to_dict,
    _expire_stale_file_reservations,
    _format_cross_project_agent_address,
    _reconcile_pending_file_reservation_artifacts,
    _revalidate_agent_lifetime_in_session,
    _sender_display_name,
    _tool_metrics_snapshot,
    build_mcp_server,
    get_project_sibling_data,
    refresh_project_sibling_suggestions,
    sweep_stale_agents,
    update_project_sibling_status,
)
from .config import Settings, get_settings
from .db import ensure_schema, get_immediate_session, get_session
from .delivery import (
    DeliveryActorSnapshot,
    DeliveryAgentSnapshot,
    DeliveryProjectSnapshot,
    DeliveryRecipientSnapshot,
    MessageDeliveryAcceptance,
    MessageDeliveryIdempotencyConflictError,
    MessageDeliveryNotFoundError,
    MessageDeliveryProcessingResult,
    MessageDeliveryRequest,
    MessageDeliveryServiceError,
    MessageDeliveryValidationError,
    accept_message_delivery,
    emit_published_delivery_notifications,
    get_message_delivery_status,
    process_message_delivery,
)
from .models import (
    MAIL_UI_LOCALE_ENGLISH_NAMES,
    Agent,
    FileReservation,
    MailUiLocale,
    Message,
    MessageDelivery,
    Project,
)
from .notify import KEEPALIVE_SECONDS, MAX_STREAM_SECONDS, hub
from .storage import (
    AsyncFileLock,
    ProjectArchive,
    _commit_lock_path,
    _project_archive_lock_path,
    _resolved_git_common_dir,
    _to_thread_cancellation_safe,
    archive_write_lock,
    collect_lock_status,
    ensure_archive,
    get_agent_communication_graph,
    get_archive_tree,
    get_commit_detail,
    get_fd_headroom,
    get_fd_usage,
    get_file_content,
    get_historical_inbox_snapshot,
    get_lock_telemetry,
    get_message_commit_sha,
    get_recent_commits,
    get_repo_cache_stats,
    get_timeline_commits,
    proactive_fd_cleanup,
    write_agent_profile,
)
from .ui_access import (
    UiAccessMutationError,
    UiAccessMutationErrorCode,
    UiProfileMutationError,
    UiProfileMutationErrorCode,
    mutate_ui_project_access,
    mutate_ui_user_display_name,
)


async def _project_slug_from_id(pid: int | None) -> str | None:
    if pid is None:
        return None
    async with get_session() as session:
        row = await session.execute(text("SELECT slug FROM projects WHERE id = :pid"), {"pid": pid})
        res = row.fetchone()
        return res[0] if res and res[0] else None


async def _ensure_ack_escalation_holder(
    *,
    settings: Settings,
    project: Project,
    recipient_agent: Agent,
    claim_name: str,
    now_naive: datetime,
) -> Agent:
    """Return the holder identity for ACK escalation, creating the ops holder if needed.

    When a synthetic holder must be created, the DB insert happens first and the
    archive profile write follows only after the session has closed. This keeps
    the ACK worker out of the DB->archive lock ordering that can deadlock mixed
    HTTP and MCP traffic.
    """
    if project.id is None or recipient_agent.id is None:
        raise ValueError("ACK escalation requires persisted project and Agent rows.")
    archive = await ensure_archive(settings, project.slug)
    holder: Agent | None = None
    created = False

    async with get_immediate_session() as session:
        await _revalidate_agent_lifetime_in_session(
            session,
            project=project,
            agent=recipient_agent,
            action="ACK escalation holder selection",
        )
        holder = (
            await session.execute(
                select(Agent).where(
                    col(Agent.project_id) == project.id,
                    col(Agent.name) == claim_name,
                    col(Agent.provisioning_state) == "active",
                )
            )
        ).scalars().first()
        if holder is None:
            holder = Agent(
                project_id=project.id,
                name=claim_name,
                program="ops",
                model="system",
                task_description="ops-escalation",
                inception_ts=now_naive,
                last_active_ts=now_naive,
                attachments_policy="auto",
                contact_policy="auto",
            )
            session.add(holder)
            await session.flush()
            created = True
        await session.commit()
        await session.refresh(holder)

    if not created:
        return holder

    async with archive_write_lock(archive):
        async with get_session() as session:
            _current_project, current_holder, _current_execution = (
                await _revalidate_agent_lifetime_in_session(
                    session,
                    project=project,
                    agent=holder,
                    action="ACK escalation holder profile publication",
                )
            )
        await write_agent_profile(archive, _agent_to_dict(current_holder))
    return current_holder


async def _create_ack_escalation_reservation(
    *,
    project: Project,
    holder: Agent,
    path_pattern: str,
    exclusive: bool,
    now_naive: datetime,
    ttl_seconds: int,
) -> FileReservation:
    """Persist an ACK claim and publish it through the revision outbox."""
    if project.id is None or holder.id is None:
        raise ValueError("ACK escalation requires persisted project and holder rows.")
    async with get_immediate_session() as session:
        await _revalidate_agent_lifetime_in_session(
            session,
            project=project,
            agent=holder,
            action="ACK escalation reservation",
        )
        reservation = FileReservation(
            project_id=project.id,
            agent_id=holder.id,
            execution_id=None,
            origin="auto",
            path_pattern=path_pattern,
            exclusive=exclusive,
            reason="ack-overdue",
            created_ts=now_naive,
            expires_ts=now_naive + timedelta(seconds=ttl_seconds),
        )
        session.add(reservation)
        await session.commit()
        await session.refresh(reservation)
    await _reconcile_pending_file_reservation_artifacts(project)
    return reservation


def _http_sender_identity(
    *,
    message_project_id: int | None,
    sender_name: str | None,
    sender_project_id: int | None,
    sender_project_human_key: str | None,
    sender_project_slug: str | None,
) -> tuple[str, dict[str, str]]:
    canonical_sender = (sender_name or "").strip() or "Unknown"
    sender_display = _sender_display_name(
        message_project_id=message_project_id,
        sender_name=canonical_sender,
        sender_project_id=sender_project_id,
        sender_project_slug=sender_project_slug,
    )
    metadata: dict[str, str] = {"sender_name": canonical_sender}
    if (
        message_project_id is None
        or sender_project_id is None
        or sender_project_id == message_project_id
    ):
        return sender_display, metadata
    if sender_project_human_key:
        metadata["sender_project"] = sender_project_human_key
    if sender_project_slug:
        metadata["sender_project_slug"] = sender_project_slug
        metadata["sender_address"] = _format_cross_project_agent_address(
            sender_project_slug,
            canonical_sender,
        )
    return sender_display, metadata


_HTTP_MESSAGE_SUBJECT_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _coerce_http_archive_timestamp(created_ts_raw: Any) -> datetime:
    try:
        if isinstance(created_ts_raw, str):
            text_value = (
                created_ts_raw.replace("Z", "+00:00")
                if created_ts_raw.endswith("Z")
                else created_ts_raw
            )
            dt = datetime.fromisoformat(text_value)
        else:
            dt = created_ts_raw
        if not isinstance(dt, datetime):
            raise TypeError("created timestamp must be a datetime")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _build_http_archive_message_filename(created_ts_raw: Any, subject_raw: str, message_id: int) -> tuple[str, str, str]:
    dt = _coerce_http_archive_timestamp(created_ts_raw)
    y_dir = dt.strftime("%Y")
    m_dir = dt.strftime("%m")
    created_iso = dt.strftime("%Y-%m-%dT%H-%M-%SZ")
    subject_slug = (
        _HTTP_MESSAGE_SUBJECT_SLUG_RE.sub("-", subject_raw).strip("-_").lower()[:80]
        or "message"
    )
    return y_dir, m_dir, f"{created_iso}__{subject_slug}__{message_id}.md"


async def _delete_messages_from_archive(
    *,
    settings: Settings,
    project_slug: str,
    messages_to_delete: list[tuple[Any, ...]],
    recip_map: dict[int, list[str]],
    commit_message: str,
) -> int:
    archive = await ensure_archive(settings, project_slug)
    git_paths_removed: list[str] = []
    seen_git_paths: set[str] = set()

    commit_lock_path = _commit_lock_path(archive.repo_root, [])
    async with archive_write_lock(archive), AsyncFileLock(commit_lock_path):
        for mrow in messages_to_delete:
            msg_id = int(mrow[0])
            y_dir, m_dir, filename = _build_http_archive_message_filename(
                mrow[1],
                str(mrow[2] or ""),
                msg_id,
            )
            sender_name = str(mrow[3] or "")

            candidate_dirs = [
                archive.root / "messages" / y_dir / m_dir,
                archive.root / "agents" / sender_name / "outbox" / y_dir / m_dir,
            ]
            for recip_name in recip_map.get(msg_id, []):
                candidate_dirs.append(
                    archive.root / "agents" / recip_name / "inbox" / y_dir / m_dir
                )

            for cdir in candidate_dirs:
                fpath = cdir / filename
                rel = fpath.relative_to(archive.repo_root).as_posix()
                try:
                    await asyncio.to_thread(fpath.unlink)
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
                if rel not in seen_git_paths:
                    seen_git_paths.add(rel)
                    git_paths_removed.append(rel)

        if git_paths_removed:
            await _to_thread_cancellation_safe(
                archive.repo.index.remove,
                git_paths_removed,
                working_tree=False,
            )
            literal_paths = [f":(literal){path}" for path in git_paths_removed]

            def _commit_only_removed_paths() -> None:
                with archive.repo.git.custom_environment(
                    GIT_AUTHOR_NAME=settings.storage.git_author_name,
                    GIT_AUTHOR_EMAIL=settings.storage.git_author_email,
                    GIT_COMMITTER_NAME=settings.storage.git_author_name,
                    GIT_COMMITTER_EMAIL=settings.storage.git_author_email,
                ):
                    archive.repo.git.commit(
                        "--only",
                        "--no-gpg-sign",
                        "--no-verify",
                        "-m",
                        commit_message,
                        "--",
                        *literal_paths,
                    )

            await _to_thread_cancellation_safe(_commit_only_removed_paths)

    return len(git_paths_removed)


__all__ = ["build_http_app", "create_app", "main"]


class _FastMCPHttpApp(Protocol):
    def http_app(self, *args: Any, **kwargs: Any) -> FastAPI: ...


class _FastAPILifespan(Protocol):
    def lifespan(self, app: FastAPI) -> Any: ...


def _expanduser_resolve_path(path: Path) -> Path:
    return path.expanduser().resolve()


def _path_exists(path: Path) -> bool:
    return path.exists()


def _open_git_repo(repo_root: Path):
    from git import Repo as GitRepo

    return GitRepo(str(repo_root))


async def _open_existing_project_archive(settings: Settings, slug: str) -> ProjectArchive | None:
    """Open an existing project archive for read-only routes without creating new directories."""
    repo_root = await asyncio.to_thread(_expanduser_resolve_path, Path(settings.storage.root))
    if not await asyncio.to_thread(_path_exists, repo_root / ".git"):
        return None
    project_root = repo_root / "projects" / slug
    if not await asyncio.to_thread(_path_exists, project_root):
        return None
    repo = await asyncio.to_thread(_open_git_repo, repo_root)
    git_common_dir = await asyncio.to_thread(
        _resolved_git_common_dir,
        repo_root,
        repo,
    )
    return ProjectArchive(
        settings=settings,
        slug=slug,
        root=project_root,
        repo=repo,
        lock_path=_project_archive_lock_path(git_common_dir, slug),
        repo_root=repo_root,
    )


def _collect_retention_quota_report_sync(settings: Settings) -> dict[str, Any]:
    import datetime as _dt
    import fnmatch as _fnmatch

    storage_root = _expanduser_resolve_path(Path(settings.storage.root))
    projects_root = storage_root / "projects"
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
        days=int(settings.retention_max_age_days)
    )
    old_messages = 0
    total_attach_bytes = 0
    per_project_attach: dict[str, int] = {}
    per_project_inbox_counts: dict[str, int] = {}
    ignore_patterns = list(getattr(settings, "retention_ignore_project_patterns", []) or [])

    for proj_dir in projects_root.iterdir() if projects_root.exists() else []:
        if not proj_dir.is_dir():
            continue
        proj_name = proj_dir.name
        if any(_fnmatch.fnmatch(proj_name, pat) for pat in ignore_patterns):
            continue
        msg_root = proj_dir / "messages"
        if msg_root.exists():
            for ydir in msg_root.iterdir():
                for mdir in ydir.iterdir() if ydir.is_dir() else []:
                    for file_path in mdir.iterdir() if mdir.is_dir() else []:
                        if file_path.suffix.lower() != ".md":
                            continue
                        with contextlib.suppress(Exception):
                            ts = _dt.datetime.fromtimestamp(file_path.stat().st_mtime, _dt.timezone.utc)
                            if ts < cutoff:
                                old_messages += 1
        inbox_root = proj_dir / "agents"
        if inbox_root.exists():
            count_inbox = 0
            for inbox_file in inbox_root.rglob("inbox/*/*/*.md"):
                with contextlib.suppress(Exception):
                    if inbox_file.is_file():
                        count_inbox += 1
            per_project_inbox_counts[proj_name] = count_inbox
        att_root = proj_dir / "attachments"
        if att_root.exists():
            for attachment_file in att_root.rglob("*.webp"):
                with contextlib.suppress(Exception):
                    size_bytes = attachment_file.stat().st_size
                    total_attach_bytes += size_bytes
                    per_project_attach[proj_name] = per_project_attach.get(proj_name, 0) + size_bytes

    return {
        "old_messages": old_messages,
        "retention_max_age_days": int(settings.retention_max_age_days),
        "total_attachments_bytes": total_attach_bytes,
        "quota_limit_bytes": int(settings.quota_attachments_limit_bytes),
        "per_project_attach": per_project_attach,
        "per_project_inbox_counts": per_project_inbox_counts,
    }


async def _collect_retention_quota_report(settings: Settings) -> dict[str, Any]:
    return await asyncio.to_thread(_collect_retention_quota_report_sync, settings)


def _collect_archive_guide_stats_sync(settings: Settings) -> dict[str, Any]:
    import subprocess as _subprocess
    from itertools import islice

    storage_root = str(_expanduser_resolve_path(Path(settings.storage.root)))
    repo_root = Path(storage_root)
    total_commits = "0"
    project_count = 0
    repo_size = "0 MB"
    last_commit_time = "Never"

    if _path_exists(repo_root / ".git"):
        repo = None
        try:
            repo = _open_git_repo(repo_root)
            commit_count = sum(1 for _ in repo.iter_commits(max_count=10000))
            total_commits = "10,000+" if commit_count == 10000 else f"{commit_count:,}"
            last_commit = next(repo.iter_commits(max_count=1), None)
            last_commit_time = last_commit.authored_datetime.strftime("%b %d, %Y") if last_commit else "Never"

            projects_dir = repo_root / "projects"
            if projects_dir.exists():
                project_count = sum(1 for p in islice(projects_dir.iterdir(), 100) if p.is_dir())

            try:
                result = _subprocess.run(
                    ["du", "-sh", str(repo_root)],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                repo_size = result.stdout.split()[0] if getattr(result, "returncode", 1) == 0 else "Unknown"
            except (_subprocess.TimeoutExpired, FileNotFoundError, PermissionError, OSError):
                repo_size = "Unknown"
        except Exception:
            pass
        finally:
            if repo is not None:
                repo.close()

    return {
        "storage_root": storage_root,
        "total_commits": total_commits,
        "project_count": project_count,
        "repo_size": repo_size,
        "last_commit_time": last_commit_time,
    }


def _decode_jwt_header_segment(token: str) -> dict[str, object] | None:
    """Return decoded JWT header without verifying signature."""
    try:
        segment = token.split(".", 1)[0]
        padded = segment + "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


_LOGGING_CONFIGURED = False

# Pre-compiled regex patterns for HTTP validators
_SLUG_VALIDATOR_RE = re.compile(r"^[a-z0-9_-]+$", re.IGNORECASE)
_AGENT_NAME_VALIDATOR_RE = re.compile(r"^[A-Za-z0-9]+$")
_TIMESTAMP_VALIDATOR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")

_LIKE_ESCAPE_CHAR = "!"


def _like_escape(term: str) -> str:
    """Escape LIKE wildcards for literal substring matching."""
    return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _configure_logging(settings: Settings) -> None:
    """Initialize structlog and stdlib logging formatting."""
    # Idempotent setup
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
    ]
    if settings.log_json_enabled:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.processors.KeyValueRenderer(key_order=["event", "path", "status"]))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level.upper(), logging.INFO)),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    # Suppress verbose MCP library logging for stateless HTTP sessions
    # "Terminating session: None" is routine for stateless mode and just noise
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

    # Suppress verbose aiosqlite DEBUG logs (functools.partial cursor/operation noise)
    logging.getLogger("aiosqlite").setLevel(logging.INFO)

    # Suppress verbose git library DEBUG logs (Popen commands, platform detection)
    logging.getLogger("git.util").setLevel(logging.INFO)
    logging.getLogger("git.cmd").setLevel(logging.INFO)

    # Suppress filelock DEBUG logs (lock acquire/release routine operations)
    logging.getLogger("filelock").setLevel(logging.INFO)

    # Suppress SSE ping keepalive debug logs (periodic noise every 15s)
    logging.getLogger("sse_starlette.sse").setLevel(logging.INFO)

    # Add filter to suppress verbose tracebacks for expected/recoverable errors
    # FastMCP's tool_manager uses logger.exception() which prints full tracebacks
    # even for expected errors like "agent not found" or "git lock contention".
    # This filter intercepts those and removes the traceback for cleaner logs.
    class ExpectedErrorFilter(logging.Filter):
        """Filter that suppresses tracebacks for expected/recoverable tool errors.

        Expected errors include:
        - ToolExecutionError with recoverable=True
        - Agent not found / project not found
        - Git index.lock contention
        - Resource busy / database lock

        These are normal operational conditions in multi-agent environments
        and don't need full stack traces cluttering the logs.
        """

        # Keywords that indicate an expected/recoverable error
        _EXPECTED_PATTERNS = (
            "not found in project",
            "index.lock",
            "git_index_lock",
            "resource_busy",
            "temporarily locked",
            "recoverable=true",
            "use register_agent",
            "available agents:",
        )

        def filter(self, record: logging.LogRecord) -> bool:
            # Only process records from FastMCP tool_manager with exception info
            if not record.exc_info or record.exc_info[1] is None:
                return True

            exc = record.exc_info[1]
            exc_str = str(exc).lower()

            # Check if this is an expected error based on message content
            is_expected = any(pattern in exc_str for pattern in self._EXPECTED_PATTERNS)

            # Also check for our ToolExecutionError with recoverable flag
            if hasattr(exc, "recoverable") and exc.recoverable:
                is_expected = True

            # Check the cause chain for ToolExecutionError
            cause = getattr(exc, "__cause__", None)
            if cause is not None:
                cause_str = str(cause).lower()
                if any(pattern in cause_str for pattern in self._EXPECTED_PATTERNS):
                    is_expected = True
                if hasattr(cause, "recoverable") and cause.recoverable:
                    is_expected = True

            if is_expected:
                # Clear exc_info to prevent traceback printing, but keep the log message
                record.exc_info = None
                record.exc_text = None
                # Downgrade from ERROR to INFO for expected errors
                if record.levelno >= logging.ERROR:
                    record.levelno = logging.INFO
                    record.levelname = "INFO"

            return bool(super().filter(record))

    # Apply filter to FastMCP's tool_manager logger
    fastmcp_logger = logging.getLogger("fastmcp.tools.tool_manager")
    fastmcp_logger.addFilter(ExpectedErrorFilter())

    # mark configured
    _LOGGING_CONFIGURED = True


# In-process JWKS cache: avoid refetching the JWKS document on every request
# (#212). Keyed by JWKS URL; entries expire after _JWKS_CACHE_TTL_SECONDS.
_JWKS_CACHE_TTL_SECONDS = 300.0
_jwks_cache: dict[str, tuple[float, Any]] = {}
_jwks_cache_lock = threading.Lock()


def clear_jwks_cache() -> None:
    """Clear cached JWKS documents.

    The cache is process-wide, so tests that reuse a URL with different key
    material must reset it just like the settings, database, and repo caches.
    A thread lock is sufficient here because every cache operation is
    synchronous and brief; unlike ``asyncio.Lock`` it is not tied to the event
    loop that happened to make the first request.
    """
    with _jwks_cache_lock:
        _jwks_cache.clear()


async def _fetch_jwks(jwks_url: str, *, force: bool = False):
    """Return a parsed JWKS key set for ``jwks_url``, using a TTL cache.

    On a cache miss/expiry (or when ``force`` is set, e.g. after an unknown
    ``kid``), the document is refetched. On fetch/parse failure the last good
    cached key set (if any) is returned so transient outages don't break auth.
    """
    from time import monotonic

    jose_mod = importlib.import_module("authlib.jose")
    JsonWebKey = jose_mod.JsonWebKey

    now = monotonic()
    with _jwks_cache_lock:
        cached = _jwks_cache.get(jwks_url)
        if cached is not None and not force and (now - cached[0]) < _JWKS_CACHE_TTL_SECONDS:
            return cached[1]

    try:
        httpx = importlib.import_module("httpx")
        AsyncClient = httpx.AsyncClient
        async with AsyncClient(timeout=5) as client:
            jwks = (await client.get(jwks_url)).json()
        key_set = JsonWebKey.import_key_set(jwks)
    except Exception:
        # Fall back to any cached (possibly stale) key set on fetch failure.
        with _jwks_cache_lock:
            cached = _jwks_cache.get(jwks_url)
        return cached[1] if cached is not None else None

    with _jwks_cache_lock:
        _jwks_cache[jwks_url] = (monotonic(), key_set)
    return key_set


def _select_jwks_key(key_set, header: dict, algorithms: list[str]):
    """Resolve the verification key from a JWKS key set by ``kid``.

    Never blindly picks ``keys[0]`` (#211). With a ``kid`` we look it up
    directly; an unknown ``kid`` returns ``None``. Without a ``kid`` this also
    returns ``None`` -- the caller falls back to verifying against each
    algorithm-compatible candidate (see ``_jwks_candidate_keys``) instead.
    """
    kid = header.get("kid")
    if kid:
        with contextlib.suppress(Exception):
            return key_set.find_by_kid(kid)
    return None


def _jwks_candidate_keys(key_set, header: dict, algorithms: list[str]) -> list:
    """Return JWKS keys to try when no ``kid`` is present.

    Filters by signing use and by algorithm compatibility (matching the key's
    declared ``alg`` when present, otherwise the key type implied by the
    configured algorithms). Blind ``keys[0]`` selection is never used.
    """
    alg_set = {str(a) for a in algorithms}
    # Map configured JWS algorithms to acceptable JWK key types.
    kty_for_alg = {
        "HS": "oct", "RS": "RSA", "PS": "RSA",
        "ES": "EC", "Ed": "OKP",
    }
    wanted_kty = {kty_for_alg[a[:2]] for a in alg_set if a[:2] in kty_for_alg}
    candidates = []
    for key in list(getattr(key_set, "keys", []) or []):
        with contextlib.suppress(Exception):
            use = key.tokens.get("use") if hasattr(key, "tokens") else None
            if use not in (None, "sig"):
                continue
            key_alg = key.tokens.get("alg") if hasattr(key, "tokens") else None
            if key_alg is not None and str(key_alg) not in alg_set:
                continue
            kty = getattr(key, "kty", None) or (key.tokens.get("kty") if hasattr(key, "tokens") else None)
            if wanted_kty and kty is not None and kty not in wanted_kty:
                continue
            candidates.append(key)
    return candidates


class Utf8BodyGuardMiddleware:
    """Answer 400, not 500, when a request body is not valid UTF-8.

    The MCP SDK's POST handler raises on an undecodable body and the resulting
    reply is `500 Error handling POST request` (mcp/server/streamable_http.py).
    That is the wrong shape of answer twice over: it blames the server for the
    caller's bytes, and it says nothing about what was wrong. A client that
    encodes its body in a legacy codepage — which is what a shell on a
    non-English Windows does unless its console page is UTF-8 — gets a generic
    server error and no way to reach the real cause. Worse, hook clients that
    swallow errors read it as an empty result, indistinguishable from "nothing
    to report", so a message can be lost with no trace anywhere.

    Written as plain ASGI rather than BaseHTTPMiddleware because it has to touch
    the request body, and buffering through BaseHTTPMiddleware is exactly where
    that class misbehaves. Only POSTs with a declared, modest Content-Length are
    inspected: a streamed or oversized body is passed through untouched rather
    than buffered, and non-POST traffic — including the long-lived SSE GET — is
    never intercepted at all.
    """

    # Comfortably above any JSON-RPC call this server serves; attachments travel
    # as paths, not bytes.
    MAX_INSPECT_BYTES = 4 * 1024 * 1024

    def __init__(self, app: Any) -> None:
        self._app = app

    def _declared_length(self, scope: Scope) -> int | None:
        for key, value in scope.get("headers") or []:
            if key.lower() == b"content-length":
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return
        length = self._declared_length(scope)
        if length is None or length <= 0 or length > self.MAX_INSPECT_BYTES:
            await self._app(scope, receive, send)
            return

        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                await self._app(scope, receive, send)
                return
            chunks.append(message.get("body", b"") or b"")
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)

        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            detail = (
                "Request body is not valid UTF-8 (invalid byte at offset "
                f"{exc.start}). JSON must be UTF-8 encoded; a body produced in a "
                "legacy codepage will fail here. Either send UTF-8, or escape "
                "non-ASCII characters as \\uXXXX, which cannot be mis-encoded."
            )
            response = JSONResponse({"detail": detail}, status_code=status.HTTP_400_BAD_REQUEST)
            await response(scope, receive, send)
            return

        replayed = False

        async def replay() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self._app(scope, replay, send)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self, app: FastAPI, token: str, allow_localhost: bool = False, jwt_enabled: bool = False
    ) -> None:
        super().__init__(app)
        self._token = token
        self._allow_localhost = allow_localhost
        # When JWT auth is also enabled, a static-bearer mismatch must NOT
        # short-circuit before the inner SecurityAndRateLimitMiddleware gets a
        # chance to validate a JWT (#210). In that case we accept any Bearer
        # token here and let the JWT path render the final auth decision.
        self._jwt_enabled = jwt_enabled

    @staticmethod
    def _is_localhost(host: str) -> bool:
        """Check if host is a localhost address, including IPv4-mapped IPv6."""
        if not host:
            return False
        # Standard localhost addresses
        if host in {"127.0.0.1", "::1", "localhost"}:
            return True
        # IPv4-mapped IPv6 address (::ffff:127.0.0.1)
        return bool(host.lower().startswith("::ffff:") and host[7:] == "127.0.0.1")

    @staticmethod
    def _has_forwarded_headers(request: Request) -> bool:
        """Detect proxy-forwarded headers to avoid trusting localhost behind proxies."""
        headers = request.headers
        return any(
            name in headers
            for name in ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "forwarded")
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        if request.method == "OPTIONS":  # allow CORS preflight
            return await call_next(request)
        if request.url.path.startswith("/health/") or request.url.path == "/api/health":
            return await call_next(request)
        # MailUiAuthMiddleware sits OUTSIDE this one and has already rendered a
        # verdict for /mail: either it authenticated a browser session (and set
        # this flag) or it redirected to the login page. Re-checking the bearer
        # here would 401 every logged-in human, since a browser cannot attach an
        # Authorization header to an ordinary navigation.
        if getattr(request.state, "mail_ui_authenticated", False):
            return await call_next(request)
        if _localhost_bypass_allowed(
            request,
            allow_localhost=self._allow_localhost,
        ):
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        expected_header = f"Bearer {self._token}"
        # Use constant-time comparison to prevent timing attacks
        if hmac.compare_digest(auth_header, expected_header):
            return await call_next(request)
        # Static bearer did not match. If JWT auth is enabled, defer to the inner
        # JWT-validating middleware instead of rejecting here, so EITHER a valid
        # static bearer OR a valid JWT is accepted (#210).
        if self._jwt_enabled and auth_header.startswith("Bearer "):
            return await call_next(request)
        return JSONResponse({"detail": "Unauthorized"}, status_code=status.HTTP_401_UNAUTHORIZED)


def _localhost_bypass_allowed(request: Request, *, allow_localhost: bool) -> bool:
    """Return whether this request qualifies for localhost auth bypass."""
    if not allow_localhost:
        return False
    try:
        client_host = request.client.host if request.client else ""
    except Exception:
        client_host = ""
    return BearerAuthMiddleware._is_localhost(client_host) and not BearerAuthMiddleware._has_forwarded_headers(
        request
    )


# Paths under the /mail prefix that must remain reachable without a session,
# otherwise there is no way to obtain one.
_MAIL_LOGIN_PATH = "/mail/login"
_MAIL_LOGOUT_PATH = "/mail/logout"
_MAIL_FILE_RESERVATIONS_API_PATH = "/mail/api/file-reservations"
_MAIL_PROFILE_API_PATH = "/mail/api/v1/me/profile"
_MAIL_PREFERENCES_API_PATH = "/mail/api/v1/me/preferences"
_MAIL_PASSWORD_API_PATH = "/mail/api/v1/me/password"
_MAIL_ADMIN_ACCESS_API_PATH = "/mail/api/v1/admin/access"
_MAIL_ADMIN_ASSIGNMENT_API_PATH = (
    "/mail/api/v1/admin/users/{target_user_id}/projects/{project_id}"
)
# Fail closed until the Human Overseer flow can commit its database rows and
# archive bundle atomically. A successful write to either persistence layer
# must never survive when the other layer fails.
_MAIL_LEGACY_OVERSEER_UNAVAILABLE_DETAIL = (
    "Human Overseer messaging is temporarily unavailable while atomic archive persistence is implemented"
)
_MAIL_ACCOUNT_API_PATHS = frozenset(
    {_MAIL_PROFILE_API_PATH, _MAIL_PREFERENCES_API_PATH, _MAIL_PASSWORD_API_PATH}
)
_MAIL_REACT_BASE_PATH = "/mail"
_MAIL_LOGIN_STYLESHEET_PATH = f"{_MAIL_REACT_BASE_PATH}/assets/legacy.css"
_MAIL_LOGIN_FLAG_FONT_PATH = (
    f"{_MAIL_REACT_BASE_PATH}/assets/TwemojiCountryFlags.woff2"
)
_IRIS_FAVICON_PATH = "/iris-rainbow.svg"
_IRIS_FAVICON_SVG = (
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
_MAIL_HTML_CACHE_CONTROL = "no-store, no-transform"
_MAIL_REACT_INDEX_HEADERS = {
    "Cache-Control": _MAIL_HTML_CACHE_CONTROL,
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; font-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
_MAIL_REACT_ASSET_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable, no-transform",
    "X-Content-Type-Options": "nosniff",
}
_MAIL_REACT_LEGACY_ASSET_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Content-Type-Options": "nosniff",
}
_MAIL_LEGACY_HTML_HEADERS = {
    "Cache-Control": _MAIL_HTML_CACHE_CONTROL,
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data: blob:; font-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
_MAIL_LOGIN_HTML_HEADERS = {
    "Cache-Control": _MAIL_HTML_CACHE_CONTROL,
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'none'; style-src 'self'; connect-src 'none'; "
        "img-src 'self' data:; font-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class _MailLoginText(NamedTuple):
    """Every human-readable string on the public sign-in surface."""

    sign_in: str
    hint: str
    username: str
    password: str
    accounts: str
    invalid_credentials: str
    throttled: str
    language: str


class _MailLoginLocalePresentation(NamedTuple):
    """Flag and self-name shared with the authenticated React locale picker."""

    flag: str
    native_name: str


_MAIL_LOGIN_LOCALE_PRESENTATION: dict[MailUiLocale, _MailLoginLocalePresentation] = {
    MailUiLocale.AR: _MailLoginLocalePresentation("🇸🇦", "العربية"),
    MailUiLocale.BN: _MailLoginLocalePresentation("🇧🇩", "বাংলা"),
    MailUiLocale.BS: _MailLoginLocalePresentation("🇧🇦", "Bosanski"),
    MailUiLocale.CS: _MailLoginLocalePresentation("🇨🇿", "Čeština"),
    MailUiLocale.DA: _MailLoginLocalePresentation("🇩🇰", "Dansk"),
    MailUiLocale.DE: _MailLoginLocalePresentation("🇩🇪", "Deutsch"),
    MailUiLocale.EL: _MailLoginLocalePresentation("🇬🇷", "Ελληνικά"),
    MailUiLocale.EN: _MailLoginLocalePresentation("🇬🇧", "English"),
    MailUiLocale.ES: _MailLoginLocalePresentation("🇪🇸", "Español"),
    MailUiLocale.FA: _MailLoginLocalePresentation("🇮🇷", "فارسی"),
    MailUiLocale.FI: _MailLoginLocalePresentation("🇫🇮", "Suomi"),
    MailUiLocale.FIL: _MailLoginLocalePresentation("🇵🇭", "Filipino"),
    MailUiLocale.FR: _MailLoginLocalePresentation("🇫🇷", "Français"),
    MailUiLocale.GA: _MailLoginLocalePresentation("🇮🇪", "Gaeilge"),
    MailUiLocale.HE: _MailLoginLocalePresentation("🇮🇱", "עברית"),
    MailUiLocale.HI: _MailLoginLocalePresentation("🇮🇳", "हिन्दी"),
    MailUiLocale.HR: _MailLoginLocalePresentation("🇭🇷", "Hrvatski"),
    MailUiLocale.HU: _MailLoginLocalePresentation("🇭🇺", "Magyar"),
    MailUiLocale.HY: _MailLoginLocalePresentation("🇦🇲", "Հայերեն"),
    MailUiLocale.ID: _MailLoginLocalePresentation("🇮🇩", "Bahasa Indonesia"),
    MailUiLocale.IS: _MailLoginLocalePresentation("🇮🇸", "Íslenska"),
    MailUiLocale.IT: _MailLoginLocalePresentation("🇮🇹", "Italiano"),
    MailUiLocale.JA: _MailLoginLocalePresentation("🇯🇵", "日本語"),
    MailUiLocale.KO: _MailLoginLocalePresentation("🇰🇷", "한국어"),
    MailUiLocale.LT: _MailLoginLocalePresentation("🇱🇹", "Lietuvių"),
    MailUiLocale.LV: _MailLoginLocalePresentation("🇱🇻", "Latviešu"),
    MailUiLocale.MS: _MailLoginLocalePresentation("🇲🇾", "Bahasa Melayu"),
    MailUiLocale.MY_MM: _MailLoginLocalePresentation("🇲🇲", "မြန်မာဘာသာ"),
    MailUiLocale.NL: _MailLoginLocalePresentation("🇳🇱", "Nederlands"),
    MailUiLocale.NO: _MailLoginLocalePresentation("🇳🇴", "Norsk"),
    MailUiLocale.PL: _MailLoginLocalePresentation("🇵🇱", "Polski"),
    MailUiLocale.PT: _MailLoginLocalePresentation("🇵🇹", "Português"),
    MailUiLocale.RO: _MailLoginLocalePresentation("🇷🇴", "Română"),
    MailUiLocale.RU: _MailLoginLocalePresentation("🇷🇺", "Русский"),
    MailUiLocale.SK: _MailLoginLocalePresentation("🇸🇰", "Slovenčina"),
    MailUiLocale.SQ: _MailLoginLocalePresentation("🇦🇱", "Shqip"),
    MailUiLocale.SR: _MailLoginLocalePresentation("🇷🇸", "Српски"),
    MailUiLocale.SV: _MailLoginLocalePresentation("🇸🇪", "Svenska"),
    MailUiLocale.SW: _MailLoginLocalePresentation("🇰🇪", "Kiswahili"),
    MailUiLocale.TH: _MailLoginLocalePresentation("🇹🇭", "ไทย"),
    MailUiLocale.TR: _MailLoginLocalePresentation("🇹🇷", "Türkçe"),
    MailUiLocale.UK: _MailLoginLocalePresentation("🇺🇦", "Українська"),
    MailUiLocale.VI: _MailLoginLocalePresentation("🇻🇳", "Tiếng Việt"),
    MailUiLocale.ZH_HANT: _MailLoginLocalePresentation("🇹🇼", "繁體中文"),
    MailUiLocale.ZH: _MailLoginLocalePresentation("🇨🇳", "简体中文"),
}
_MAIL_LOGIN_TEXT: dict[MailUiLocale, _MailLoginText] = {
    MailUiLocale.AR: _MailLoginText(
        "تسجيل الدخول",
        "سجّل الدخول لعرض صندوق بريد Agent Mail الخاص بك.",
        "اسم المستخدم",
        "كلمة المرور",
        "تُنشأ الحسابات على الخادم:",
        "اسم المستخدم أو كلمة المرور غير صحيحة.",
        "محاولات فاشلة كثيرة جدًا. انتظر دقيقة ثم حاول مرة أخرى.",
        "اللغة",
    ),
    MailUiLocale.BN: _MailLoginText(
        "সাইন ইন",
        "আপনার Agent Mail মেইলবক্স দেখতে সাইন ইন করুন।",
        "ব্যবহারকারীর নাম",
        "পাসওয়ার্ড",
        "সার্ভারে অ্যাকাউন্ট তৈরি করা হয়:",
        "ব্যবহারকারীর নাম বা পাসওয়ার্ড সঠিক নয়।",
        "অনেকবার ব্যর্থ চেষ্টা হয়েছে। এক মিনিট অপেক্ষা করে আবার চেষ্টা করুন।",
        "ভাষা",
    ),
    MailUiLocale.BS: _MailLoginText(
        "Prijava",
        "Prijavite se da biste pregledali svoje Agent Mail sanduče.",
        "Korisničko ime",
        "Lozinka",
        "Nalozi se kreiraju na serveru:",
        "Neispravno korisničko ime ili lozinka.",
        "Previše neuspjelih pokušaja. Sačekajte minutu i pokušajte ponovo.",
        "Jezik",
    ),
    MailUiLocale.CS: _MailLoginText(
        "Přihlásit se",
        "Přihlaste se a zobrazte svou schránku Agent Mail.",
        "Uživatelské jméno",
        "Heslo",
        "Účty se vytvářejí na serveru:",
        "Neplatné uživatelské jméno nebo heslo.",
        "Příliš mnoho neúspěšných pokusů. Počkejte minutu a zkuste to znovu.",
        "Jazyk",
    ),
    MailUiLocale.DA: _MailLoginText(
        "Log ind",
        "Log ind for at se din Agent Mail-postkasse.",
        "Brugernavn",
        "Adgangskode",
        "Konti oprettes på serveren:",
        "Ugyldigt brugernavn eller adgangskode.",
        "For mange mislykkede forsøg. Vent et minut, og prøv igen.",
        "Sprog",
    ),
    MailUiLocale.DE: _MailLoginText(
        "Anmelden",
        "Melden Sie sich an, um Ihr Agent-Mail-Postfach anzuzeigen.",
        "Benutzername",
        "Passwort",
        "Konten werden auf dem Server erstellt:",
        "Benutzername oder Passwort ist ungültig.",
        "Zu viele fehlgeschlagene Versuche. Warten Sie eine Minute und versuchen Sie es erneut.",
        "Sprache",
    ),
    MailUiLocale.EL: _MailLoginText(
        "Σύνδεση",
        "Συνδεθείτε για να δείτε το γραμματοκιβώτιό σας στο Agent Mail.",
        "Όνομα χρήστη",
        "Κωδικός πρόσβασης",
        "Οι λογαριασμοί δημιουργούνται στον διακομιστή:",
        "Μη έγκυρο όνομα χρήστη ή κωδικός πρόσβασης.",
        "Πάρα πολλές αποτυχημένες προσπάθειες. Περιμένετε ένα λεπτό και δοκιμάστε ξανά.",
        "Γλώσσα",
    ),
    MailUiLocale.EN: _MailLoginText(
        "Sign in",
        "Sign in to view your Agent Mail mailbox.",
        "Username",
        "Password",
        "Accounts are created on the server:",
        "Invalid username or password.",
        "Too many failed attempts. Wait a minute and try again.",
        "Language",
    ),
    MailUiLocale.ES: _MailLoginText(
        "Iniciar sesión",
        "Inicia sesión para ver tu buzón de Agent Mail.",
        "Nombre de usuario",
        "Contraseña",
        "Las cuentas se crean en el servidor:",
        "El nombre de usuario o la contraseña no son válidos.",
        "Demasiados intentos fallidos. Espera un minuto y vuelve a intentarlo.",
        "Idioma",
    ),
    MailUiLocale.FA: _MailLoginText(
        "ورود",
        "برای مشاهده صندوق پستی Agent Mail خود وارد شوید.",
        "نام کاربری",
        "رمز عبور",
        "حساب‌ها در سرور ایجاد می‌شوند:",
        "نام کاربری یا رمز عبور نامعتبر است.",
        "تعداد تلاش‌های ناموفق بیش از حد است. یک دقیقه صبر کنید و دوباره تلاش کنید.",
        "زبان",
    ),
    MailUiLocale.FI: _MailLoginText(
        "Kirjaudu sisään",
        "Kirjaudu sisään nähdäksesi Agent Mail -postilaatikkosi.",
        "Käyttäjänimi",
        "Salasana",
        "Tilit luodaan palvelimella:",
        "Virheellinen käyttäjänimi tai salasana.",
        "Liian monta epäonnistunutta yritystä. Odota minuutti ja yritä uudelleen.",
        "Kieli",
    ),
    MailUiLocale.FIL: _MailLoginText(
        "Mag-sign in",
        "Mag-sign in upang tingnan ang iyong Agent Mail mailbox.",
        "Username",
        "Password",
        "Ginagawa ang mga account sa server:",
        "Hindi wastong username o password.",
        "Masyadong maraming bigong pagsubok. Maghintay ng isang minuto at subukang muli.",
        "Wika",
    ),
    MailUiLocale.FR: _MailLoginText(
        "Se connecter",
        "Connectez-vous pour consulter votre boîte Agent Mail.",
        "Nom d’utilisateur",
        "Mot de passe",
        "Les comptes sont créés sur le serveur :",
        "Nom d’utilisateur ou mot de passe incorrect.",
        "Trop de tentatives infructueuses. Attendez une minute, puis réessayez.",
        "Langue",
    ),
    MailUiLocale.GA: _MailLoginText(
        "Sínigh isteach",
        "Sínigh isteach chun do bhosca poist Agent Mail a fheiceáil.",
        "Ainm úsáideora",
        "Focal faire",
        "Cruthaítear cuntais ar an bhfreastalaí:",
        "Ainm úsáideora nó focal faire neamhbhailí.",
        "An iomarca iarracht theipthe. Fan nóiméad agus bain triail eile as.",
        "Teanga",
    ),
    MailUiLocale.HE: _MailLoginText(
        "כניסה",
        "יש להיכנס כדי לצפות בתיבת הדואר שלך ב-Agent Mail.",
        "שם משתמש",
        "סיסמה",
        "חשבונות נוצרים בשרת:",
        "שם המשתמש או הסיסמה שגויים.",
        "יותר מדי ניסיונות כושלים. יש להמתין דקה ולנסות שוב.",
        "שפה",
    ),
    MailUiLocale.HI: _MailLoginText(
        "साइन इन करें",
        "अपना Agent Mail मेलबॉक्स देखने के लिए साइन इन करें।",
        "उपयोगकर्ता नाम",
        "पासवर्ड",
        "खाते सर्वर पर बनाए जाते हैं:",
        "उपयोगकर्ता नाम या पासवर्ड अमान्य है।",
        "बहुत अधिक असफल प्रयास हुए। एक मिनट प्रतीक्षा करें और फिर कोशिश करें।",
        "भाषा",
    ),
    MailUiLocale.HR: _MailLoginText(
        "Prijava",
        "Prijavite se kako biste pregledali svoj sandučić Agent Mail.",
        "Korisničko ime",
        "Lozinka",
        "Računi se stvaraju na poslužitelju:",
        "Neispravno korisničko ime ili lozinka.",
        "Previše neuspjelih pokušaja. Pričekajte minutu i pokušajte ponovno.",
        "Jezik",
    ),
    MailUiLocale.HU: _MailLoginText(
        "Bejelentkezés",
        "Jelentkezzen be az Agent Mail-postafiókja megtekintéséhez.",
        "Felhasználónév",
        "Jelszó",
        "A fiókok a kiszolgálón hozhatók létre:",
        "Érvénytelen felhasználónév vagy jelszó.",
        "Túl sok sikertelen próbálkozás. Várjon egy percet, majd próbálja újra.",
        "Nyelv",
    ),
    MailUiLocale.HY: _MailLoginText(
        "Մուտք գործել",
        "Մուտք գործեք՝ ձեր Agent Mail փոստարկղը դիտելու համար։",
        "Օգտանուն",
        "Գաղտնաբառ",
        "Հաշիվները ստեղծվում են սերվերում՝",
        "Սխալ օգտանուն կամ գաղտնաբառ։",
        "Չափազանց շատ անհաջող փորձեր։ Սպասեք մեկ րոպե և կրկին փորձեք։",
        "Լեզու",
    ),
    MailUiLocale.ID: _MailLoginText(
        "Masuk",
        "Masuk untuk melihat kotak surat Agent Mail Anda.",
        "Nama pengguna",
        "Kata sandi",
        "Akun dibuat di server:",
        "Nama pengguna atau kata sandi tidak valid.",
        "Terlalu banyak percobaan gagal. Tunggu satu menit lalu coba lagi.",
        "Bahasa",
    ),
    MailUiLocale.IS: _MailLoginText(
        "Skrá inn",
        "Skráðu þig inn til að skoða Agent Mail-pósthólfið þitt.",
        "Notandanafn",
        "Lykilorð",
        "Reikningar eru stofnaðir á þjóninum:",
        "Ógilt notandanafn eða lykilorð.",
        "Of margar misheppnaðar tilraunir. Bíddu í eina mínútu og reyndu aftur.",
        "Tungumál",
    ),
    MailUiLocale.IT: _MailLoginText(
        "Accedi",
        "Accedi per visualizzare la tua casella di posta Agent Mail.",
        "Nome utente",
        "Password",
        "Gli account vengono creati sul server:",
        "Nome utente o password non validi.",
        "Troppi tentativi non riusciti. Attendi un minuto e riprova.",
        "Lingua",
    ),
    MailUiLocale.JA: _MailLoginText(
        "サインイン",
        "Agent Mail のメールボックスを表示するには、サインインしてください。",
        "ユーザー名",
        "パスワード",
        "アカウントはサーバーで作成します:",
        "ユーザー名またはパスワードが正しくありません。",
        "失敗回数が多すぎます。1 分待ってからもう一度お試しください。",
        "言語",
    ),
    MailUiLocale.KO: _MailLoginText(
        "로그인",
        "Agent Mail 사서함을 보려면 로그인하세요.",
        "사용자 이름",
        "비밀번호",
        "계정은 서버에서 생성합니다:",
        "사용자 이름 또는 비밀번호가 올바르지 않습니다.",
        "실패한 시도가 너무 많습니다. 1분 후 다시 시도하세요.",
        "언어",
    ),
    MailUiLocale.LT: _MailLoginText(
        "Prisijungti",
        "Prisijunkite, kad peržiūrėtumėte savo Agent Mail pašto dėžutę.",
        "Naudotojo vardas",
        "Slaptažodis",
        "Paskyros kuriamos serveryje:",
        "Neteisingas naudotojo vardas arba slaptažodis.",
        "Per daug nesėkmingų bandymų. Palaukite minutę ir bandykite dar kartą.",
        "Kalba",
    ),
    MailUiLocale.LV: _MailLoginText(
        "Pierakstīties",
        "Pierakstieties, lai skatītu savu Agent Mail pastkasti.",
        "Lietotājvārds",
        "Parole",
        "Konti tiek izveidoti serverī:",
        "Nederīgs lietotājvārds vai parole.",
        "Pārāk daudz neveiksmīgu mēģinājumu. Uzgaidiet minūti un mēģiniet vēlreiz.",
        "Valoda",
    ),
    MailUiLocale.MS: _MailLoginText(
        "Log masuk",
        "Log masuk untuk melihat peti mel Agent Mail anda.",
        "Nama pengguna",
        "Kata laluan",
        "Akaun dicipta pada pelayan:",
        "Nama pengguna atau kata laluan tidak sah.",
        "Terlalu banyak percubaan gagal. Tunggu seminit dan cuba lagi.",
        "Bahasa",
    ),
    MailUiLocale.MY_MM: _MailLoginText(
        "ဝင်ရောက်ရန်",
        "သင်၏ Agent Mail စာတိုက်ပုံးကို ကြည့်ရန် ဝင်ရောက်ပါ။",
        "အသုံးပြုသူအမည်",
        "စကားဝှက်",
        "အကောင့်များကို ဆာဗာတွင် ဖန်တီးသည်၊",
        "အသုံးပြုသူအမည် သို့မဟုတ် စကားဝှက် မမှန်ကန်ပါ။",
        "ကြိုးစားမှု မအောင်မြင်သည်မှာ များလွန်းပါသည်။ တစ်မိနစ်စောင့်ပြီး ထပ်မံကြိုးစားပါ။",
        "ဘာသာစကား",
    ),
    MailUiLocale.NL: _MailLoginText(
        "Inloggen",
        "Log in om uw Agent Mail-postvak te bekijken.",
        "Gebruikersnaam",
        "Wachtwoord",
        "Accounts worden op de server aangemaakt:",
        "Ongeldige gebruikersnaam of ongeldig wachtwoord.",
        "Te veel mislukte pogingen. Wacht een minuut en probeer het opnieuw.",
        "Taal",
    ),
    MailUiLocale.NO: _MailLoginText(
        "Logg inn",
        "Logg inn for å se Agent Mail-postboksen din.",
        "Brukernavn",
        "Passord",
        "Kontoer opprettes på serveren:",
        "Ugyldig brukernavn eller passord.",
        "For mange mislykkede forsøk. Vent ett minutt og prøv igjen.",
        "Språk",
    ),
    MailUiLocale.PL: _MailLoginText(
        "Zaloguj się",
        "Zaloguj się, aby wyświetlić swoją skrzynkę Agent Mail.",
        "Nazwa użytkownika",
        "Hasło",
        "Konta tworzy się na serwerze:",
        "Nieprawidłowa nazwa użytkownika lub hasło.",
        "Zbyt wiele nieudanych prób. Poczekaj minutę i spróbuj ponownie.",
        "Język",
    ),
    MailUiLocale.PT: _MailLoginText(
        "Iniciar sessão",
        "Inicie sessão para ver a sua caixa de correio do Agent Mail.",
        "Nome de utilizador",
        "Palavra-passe",
        "As contas são criadas no servidor:",
        "Nome de utilizador ou palavra-passe inválidos.",
        "Demasiadas tentativas sem êxito. Aguarde um minuto e tente novamente.",
        "Idioma",
    ),
    MailUiLocale.RO: _MailLoginText(
        "Autentificare",
        "Autentificați-vă pentru a vedea căsuța poștală Agent Mail.",
        "Nume de utilizator",
        "Parolă",
        "Conturile se creează pe server:",
        "Nume de utilizator sau parolă nevalidă.",
        "Prea multe încercări nereușite. Așteptați un minut și încercați din nou.",
        "Limbă",
    ),
    MailUiLocale.RU: _MailLoginText(
        "Войти",
        "Войдите, чтобы просмотреть свой почтовый ящик Agent Mail.",
        "Имя пользователя",
        "Пароль",
        "Учётные записи создаются на сервере:",
        "Неверное имя пользователя или пароль.",
        "Слишком много неудачных попыток. Подождите минуту и повторите попытку.",
        "Язык",
    ),
    MailUiLocale.SK: _MailLoginText(
        "Prihlásiť sa",
        "Prihláste sa a zobrazte svoju schránku Agent Mail.",
        "Používateľské meno",
        "Heslo",
        "Účty sa vytvárajú na serveri:",
        "Neplatné používateľské meno alebo heslo.",
        "Príliš veľa neúspešných pokusov. Počkajte minútu a skúste to znova.",
        "Jazyk",
    ),
    MailUiLocale.SQ: _MailLoginText(
        "Hyni",
        "Hyni për të parë kutinë tuaj postare Agent Mail.",
        "Emri i përdoruesit",
        "Fjalëkalimi",
        "Llogaritë krijohen në server:",
        "Emër përdoruesi ose fjalëkalim i pavlefshëm.",
        "Shumë përpjekje të dështuara. Prisni një minutë dhe provoni përsëri.",
        "Gjuha",
    ),
    MailUiLocale.SR: _MailLoginText(
        "Пријавите се",
        "Пријавите се да бисте видели своје Agent Mail сандуче.",
        "Корисничко име",
        "Лозинка",
        "Налози се креирају на серверу:",
        "Неисправно корисничко име или лозинка.",
        "Превише неуспелих покушаја. Сачекајте минут и покушајте поново.",
        "Језик",
    ),
    MailUiLocale.SV: _MailLoginText(
        "Logga in",
        "Logga in för att visa din Agent Mail-postlåda.",
        "Användarnamn",
        "Lösenord",
        "Konton skapas på servern:",
        "Ogiltigt användarnamn eller lösenord.",
        "För många misslyckade försök. Vänta en minut och försök igen.",
        "Språk",
    ),
    MailUiLocale.SW: _MailLoginText(
        "Ingia",
        "Ingia ili uone kisanduku chako cha barua cha Agent Mail.",
        "Jina la mtumiaji",
        "Nenosiri",
        "Akaunti zinaundwa kwenye seva:",
        "Jina la mtumiaji au nenosiri si sahihi.",
        "Majaribio mengi sana yameshindwa. Subiri dakika moja kisha ujaribu tena.",
        "Lugha",
    ),
    MailUiLocale.TH: _MailLoginText(
        "ลงชื่อเข้าใช้",
        "ลงชื่อเข้าใช้เพื่อดูกล่องจดหมาย Agent Mail ของคุณ",
        "ชื่อผู้ใช้",
        "รหัสผ่าน",
        "บัญชีสร้างบนเซิร์ฟเวอร์:",
        "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง",
        "มีความพยายามที่ล้มเหลวมากเกินไป โปรดรอหนึ่งนาทีแล้วลองอีกครั้ง",
        "ภาษา",
    ),
    MailUiLocale.TR: _MailLoginText(
        "Oturum aç",
        "Agent Mail posta kutunuzu görüntülemek için oturum açın.",
        "Kullanıcı adı",
        "Parola",
        "Hesaplar sunucuda oluşturulur:",
        "Kullanıcı adı veya parola geçersiz.",
        "Çok fazla başarısız deneme yapıldı. Bir dakika bekleyip yeniden deneyin.",
        "Dil",
    ),
    MailUiLocale.UK: _MailLoginText(
        "Увійти",
        "Увійдіть, щоб переглянути свою поштову скриньку Agent Mail.",
        "Ім’я користувача",
        "Пароль",
        "Облікові записи створюються на сервері:",
        "Неправильне ім’я користувача або пароль.",
        "Забагато невдалих спроб. Зачекайте хвилину й повторіть спробу.",
        "Мова",
    ),
    MailUiLocale.VI: _MailLoginText(
        "Đăng nhập",
        "Đăng nhập để xem hộp thư Agent Mail của bạn.",
        "Tên người dùng",
        "Mật khẩu",
        "Tài khoản được tạo trên máy chủ:",
        "Tên người dùng hoặc mật khẩu không hợp lệ.",
        "Có quá nhiều lần thử thất bại. Hãy đợi một phút rồi thử lại.",
        "Ngôn ngữ",
    ),
    MailUiLocale.ZH_HANT: _MailLoginText(
        "登入",
        "登入以查看您的 Agent Mail 信箱。",
        "使用者名稱",
        "密碼",
        "帳戶由伺服器建立：",
        "使用者名稱或密碼無效。",
        "失敗嘗試次數過多。請稍候一分鐘再試一次。",
        "語言",
    ),
    MailUiLocale.ZH: _MailLoginText(
        "登录",
        "登录以查看您的 Agent Mail 邮箱。",
        "用户名",
        "密码",
        "账户由服务器创建：",
        "用户名或密码无效。",
        "失败尝试次数过多。请等待一分钟后重试。",
        "语言",
    ),
}
_MAIL_LOGIN_RTL_LOCALES = frozenset({MailUiLocale.AR, MailUiLocale.FA, MailUiLocale.HE})
_MAIL_LOGIN_ACCEPT_LANGUAGE_MAX_BYTES = 4096
_MAIL_LOGIN_ACCEPT_LANGUAGE_MAX_RANGES = 32
_MAIL_LOGIN_LANGUAGE_RANGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def _mail_login_accept_language_locale(raw: str) -> MailUiLocale:
    """Resolve a bounded browser language header into the closed locale set."""
    if not raw or len(raw.encode("utf-8", errors="ignore")) > _MAIL_LOGIN_ACCEPT_LANGUAGE_MAX_BYTES:
        return MailUiLocale.EN
    candidates: list[tuple[float, int, MailUiLocale]] = []
    ranges = raw.split(",")
    if len(ranges) > _MAIL_LOGIN_ACCEPT_LANGUAGE_MAX_RANGES:
        return MailUiLocale.EN
    for index, item in enumerate(ranges):
        parts = [part.strip() for part in item.split(";")]
        language_range = parts[0]
        if not _MAIL_LOGIN_LANGUAGE_RANGE_RE.fullmatch(language_range):
            continue
        quality = 1.0
        invalid_quality = False
        for parameter in parts[1:]:
            name, separator, value = parameter.partition("=")
            if name.casefold() != "q" or not separator:
                invalid_quality = True
                break
            try:
                quality = float(value)
            except ValueError:
                invalid_quality = True
                break
            if not math.isfinite(quality) or not 0.0 <= quality <= 1.0:
                invalid_quality = True
                break
        # The range check above pins quality to [0.0, 1.0], so <= 0.0 is the
        # RFC 9110 "not acceptable" q=0 case without a float equality test.
        if invalid_quality or quality <= 0.0:
            continue
        locale = MailUiLocale.canonicalize(language_range)
        folded = language_range.casefold()
        if locale is None and (
            folded.startswith("zh-hant-")
            or folded in {"zh-hant", "zh-tw", "zh-hk", "zh-mo"}
        ):
            locale = MailUiLocale.ZH_HANT
        if locale is None:
            locale = MailUiLocale.canonicalize(language_range.partition("-")[0])
        if locale is not None:
            candidates.append((quality, -index, locale))
    if not candidates:
        return MailUiLocale.EN
    return max(candidates)[2]


def _mail_login_locale(requested: str | None, accept_language: str) -> MailUiLocale:
    """Prefer an explicit canonical tag; invalid explicit input fails to English."""
    if requested is not None:
        return MailUiLocale.canonicalize(requested) or MailUiLocale.EN
    return _mail_login_accept_language_locale(accept_language)


def _mail_login_context(locale: MailUiLocale, next_url: str) -> dict[str, Any]:
    """Build one escaped, server-rendered language choice model for the gate."""
    options: list[dict[str, str | bool]] = []
    for option_locale, presentation in _MAIL_LOGIN_LOCALE_PRESENTATION.items():
        options.append(
            {
                "code": option_locale.value,
                "direction": "rtl" if option_locale in _MAIL_LOGIN_RTL_LOCALES else "ltr",
                "flag": presentation.flag,
                "href": f"{_MAIL_LOGIN_PATH}?{urlencode({'lang': option_locale.value, 'next': next_url})}",
                "is_current": option_locale is locale,
                "native_name": presentation.native_name,
            }
        )
    return {
        "login_direction": "rtl" if locale in _MAIL_LOGIN_RTL_LOCALES else "ltr",
        "login_locale": locale.value,
        "login_locale_options": options,
        "login_locale_presentation": _MAIL_LOGIN_LOCALE_PRESENTATION[locale],
        "login_text": _MAIL_LOGIN_TEXT[locale],
    }
_MAIL_BODY_INLINE_IMAGE_MAX_BYTES = 2 * 1024 * 1024
_MAIL_BODY_INLINE_IMAGE_PREFIXES: dict[str, Callable[[bytes], bool]] = {
    "data:image/png;base64,": lambda raw: raw.startswith(b"\x89PNG\r\n\x1a\n"),
    "data:image/jpeg;base64,": lambda raw: raw.startswith(b"\xff\xd8\xff"),
    "data:image/gif;base64,": lambda raw: raw.startswith((b"GIF87a", b"GIF89a")),
    "data:image/webp;base64,": lambda raw: len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
}
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_OVERSEER_REPLY_PATH_RE = re.compile(r"^/mail/[^/]+/overseer/reply$")
_MAIL_API_REPLY_SHAPE_RE = re.compile(
    r"^/mail/api/v1/projects/[^/]+/messages/[^/]+/replies$"
)
_MAIL_API_COMPOSE_SHAPE_RE = re.compile(
    r"^/mail/api/v1/projects/[^/]+/messages$"
)
_MAIL_API_AGENT_DIRECTORY_SHAPE_RE = re.compile(
    r"^/mail/api/v1/projects/[^/]+/agents$"
)
_MAIL_API_DELIVERY_SHAPE_RE = re.compile(
    r"^/mail/api/v1/deliveries/[^/]+(?:/retry)?$"
)
_MAIL_SEARCH_API_PATH = "/mail/api/v1/search"
_MAIL_LEGACY_PROJECT_PATH_RE = re.compile(
    r"^/mail/(?P<project>[a-z0-9](?:[a-z0-9-]{0,253}[a-z0-9])?)$"
)
_MAIL_LEGACY_MESSAGE_PATH_RE = re.compile(
    r"^/mail/(?P<project>[a-z0-9](?:[a-z0-9-]{0,253}[a-z0-9])?)/message/"
    r"(?P<message_id>[1-9][0-9]{0,18})$"
)
_MAIL_LEGACY_THREAD_PATH_RE = re.compile(
    r"^/mail/(?P<project>[a-z0-9](?:[a-z0-9-]{0,253}[a-z0-9])?)/thread/"
    r"(?P<thread_id>[^/]+)$"
)
_MAIL_UI_NUMERIC_THREAD_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_MAIL_UI_ENCODE_COMPONENT_SAFE = "-_.!~*'()"
_MAIL_LEGACY_SEARCH_PATH_RE = re.compile(
    r"^/mail/(?P<project>[a-z0-9](?:[a-z0-9-]{0,253}[a-z0-9])?)/search$"
)
_MAIL_LEGACY_RESERVED_PROJECT_NAMES = frozenset(
    {
        "api",
        "archive",
        "assets",
        "events",
        "login",
        "logout",
        "projects",
        "unified-inbox",
        "v2",
    }
)
_MAIL_UI_SEARCH_TOKEN_RE = re.compile(
    r'(?:(?P<field>subject|body):)?(?:"(?P<phrase>[^"]+)"|(?P<word>\S+))',
    re.IGNORECASE,
)
_MAIL_UI_SEARCH_MAX_TOKENS = 32
_MAIL_UI_SEARCH_SNIPPET_MAX_LENGTH = 320
_mail_ui_template_user: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "mail_ui_template_user",
    default=None,
)


def _mail_ui_inline_image_source_allowed(value: str) -> bool:
    """Allow only bounded, canonical inline raster image bytes."""
    if not value or value != value.strip():
        return False
    for prefix, signature_matches in _MAIL_BODY_INLINE_IMAGE_PREFIXES.items():
        if not value.startswith(prefix):
            continue
        payload = value.removeprefix(prefix)
        maximum_encoded_length = ((_MAIL_BODY_INLINE_IMAGE_MAX_BYTES + 2) // 3) * 4
        if not payload or len(payload) > maximum_encoded_length:
            return False
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return False
        return (
            0 < len(raw) <= _MAIL_BODY_INLINE_IMAGE_MAX_BYTES
            and base64.b64encode(raw).decode("ascii") == payload
            and signature_matches(raw)
        )
    return False

MailUiGlobalRole = Literal["admin", "member"]
MailUiAssignmentRole = Literal["viewer", "operator"]
MailUiProjectRole = Literal["admin", "viewer", "operator"]
MailUiImportance = Literal["low", "normal", "high", "urgent"]
MailUiSearchScope = Literal["all", "subject", "body"]
MailUiSearchOrder = Literal["relevance", "newest"]


class _MailUiLegacyBookmark(TypedDict):
    """One explicitly supported upstream bookmark shape."""

    kind: Literal["projects", "inbox", "project", "message", "thread", "search"]
    project_slug: str | None
    message_id: int | None
    thread_id: str | None


def _mail_ui_valid_thread_id(thread_id: str) -> bool:
    """Accept one bounded stored thread identifier without control characters."""
    return (
        0 < len(thread_id) <= 128
        and thread_id not in {".", ".."}
        and not any(
            unicodedata.category(character) in {"Cc", "Cs"}
            for character in thread_id
        )
    )


def _mail_ui_thread_starter_message_id(thread_id: str) -> int | None:
    """Return the positive SQLite message id represented by a numeric thread id."""
    if _MAIL_UI_NUMERIC_THREAD_ID_RE.fullmatch(thread_id) is None:
        return None
    message_id = int(thread_id)
    if not 0 < message_id <= 9_223_372_036_854_775_807:
        return None
    return message_id


def _mail_ui_encode_thread_id(thread_id: str) -> str:
    """Match JavaScript ``encodeURIComponent`` for one validated identifier."""
    return quote(thread_id, safe=_MAIL_UI_ENCODE_COMPONENT_SAFE)


def _mail_ui_legacy_bookmark(path: str) -> _MailUiLegacyBookmark | None:
    """Parse only legacy paths with an exact React successor.

    This is intentionally not a catch-all under ``/mail``. In particular the
    temporary development namespace ``/mail/v2`` and all retired APIs,
    archives, assets, agent inboxes, reservations, attachments, and overseer
    forms remain outside the allowlist. The one thread shape below is bounded
    separately and resolves through project RBAC before it can redirect.
    """
    if path == "/mail/projects":
        return {
            "kind": "projects",
            "project_slug": None,
            "message_id": None,
            "thread_id": None,
        }
    if path == "/mail/unified-inbox":
        return {
            "kind": "inbox",
            "project_slug": None,
            "message_id": None,
            "thread_id": None,
        }
    for pattern, kind in (
        (_MAIL_LEGACY_MESSAGE_PATH_RE, "message"),
        (_MAIL_LEGACY_THREAD_PATH_RE, "thread"),
        (_MAIL_LEGACY_SEARCH_PATH_RE, "search"),
        (_MAIL_LEGACY_PROJECT_PATH_RE, "project"),
    ):
        match = pattern.fullmatch(path)
        if match is None:
            continue
        project_slug = match.group("project")
        if project_slug in _MAIL_LEGACY_RESERVED_PROJECT_NAMES:
            return None
        raw_message_id = match.groupdict().get("message_id")
        message_id = int(raw_message_id) if raw_message_id is not None else None
        if message_id is not None and message_id > 9_223_372_036_854_775_807:
            return None
        thread_id = match.groupdict().get("thread_id")
        if thread_id is not None and not _mail_ui_valid_thread_id(thread_id):
            return None
        return {
            "kind": cast(
                Literal[
                    "projects",
                    "inbox",
                    "project",
                    "message",
                    "thread",
                    "search",
                ],
                kind,
            ),
            "project_slug": project_slug,
            "message_id": message_id,
            "thread_id": thread_id,
        }
    return None


def _mail_ui_canonical_legacy_bookmark(
    *,
    raw_path: str,
    decoded_path: str,
) -> _MailUiLegacyBookmark | None:
    """Parse one canonical encoded bookmark from its raw and decoded paths."""
    if not raw_path.isascii():
        return None
    decoded_bookmark = _mail_ui_legacy_bookmark(decoded_path)
    if decoded_bookmark is None:
        return None
    if decoded_bookmark["kind"] == "thread":
        raw_match = _MAIL_LEGACY_THREAD_PATH_RE.fullmatch(raw_path)
        if raw_match is None:
            return None
        project_slug = decoded_bookmark["project_slug"]
        thread_id = decoded_bookmark["thread_id"]
        if project_slug is None or thread_id is None:
            return None
        if raw_match.group("project") != project_slug:
            return None
        if raw_path != f"/mail/{project_slug}/thread/{_mail_ui_encode_thread_id(thread_id)}":
            return None
    else:
        raw_bookmark = _mail_ui_legacy_bookmark(raw_path)
        if raw_bookmark is None or raw_bookmark != decoded_bookmark:
            return None
    return decoded_bookmark


def _mail_ui_request_legacy_bookmark(request: Request) -> _MailUiLegacyBookmark | None:
    """Parse an exact canonical request bookmark after one transport decode."""
    raw_path_value = request.scope.get("raw_path")
    decoded_path = request.scope.get("path")
    if (
        not isinstance(raw_path_value, bytes)
        or not raw_path_value.isascii()
        or not isinstance(decoded_path, str)
    ):
        return None
    return _mail_ui_canonical_legacy_bookmark(
        raw_path=raw_path_value.decode("ascii"),
        decoded_path=decoded_path,
    )


def _mail_ui_decode_raw_path(raw_path: str) -> str | None:
    """Decode one canonical URL path candidate exactly once as strict UTF-8."""
    if not raw_path.isascii():
        return None
    try:
        return unquote_to_bytes(raw_path).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _mail_ui_active_path(path: str) -> bool:
    """Expose one human UI surface and the exact services it consumes."""
    return (
        path in {
            "/mail",
            "/mail/",
            _MAIL_LOGIN_PATH,
            _MAIL_LOGOUT_PATH,
            "/mail/events",
            _MAIL_FILE_RESERVATIONS_API_PATH,
        }
        or path.startswith("/mail/assets/")
        or path == "/mail/api/v1"
        or path.startswith("/mail/api/v1/")
        or _mail_ui_legacy_bookmark(path) is not None
    )


def _mail_ui_uses_typed_domain_errors(path: str) -> bool:
    """Whether authorization failures on ``path`` use stable ``detail.code``."""
    return (
        path in {
            _MAIL_PROFILE_API_PATH,
            _MAIL_ADMIN_ACCESS_API_PATH,
            _MAIL_SEARCH_API_PATH,
        }
        or path.startswith("/mail/api/v1/admin/users/")
        or bool(_MAIL_API_AGENT_DIRECTORY_SHAPE_RE.fullmatch(path))
        or bool(_MAIL_API_DELIVERY_SHAPE_RE.fullmatch(path))
        or bool(_MAIL_API_COMPOSE_SHAPE_RE.fullmatch(path))
        or bool(_MAIL_API_REPLY_SHAPE_RE.fullmatch(path))
    )


def _mail_ui_uses_typed_delivery_errors(path: str) -> bool:
    """Whether ``path`` belongs to the generated delivery API contract."""
    return bool(
        _MAIL_API_DELIVERY_SHAPE_RE.fullmatch(path)
        or _MAIL_API_COMPOSE_SHAPE_RE.fullmatch(path)
        or _MAIL_API_REPLY_SHAPE_RE.fullmatch(path)
    )


def _mail_ui_authorization_detail(path: str, fallback: str) -> str | dict[str, str]:
    """Render middleware authorization failures through the advertised API shape."""
    if _mail_ui_uses_typed_domain_errors(path):
        return {"code": "actor_forbidden"}
    return fallback


def _normalized_http_base_path(path: str) -> str:
    """Return the exact mount prefix used by the configured MCP transport."""
    normalized = path or "/api"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized.rstrip("/") or "/"


def _mail_react_dist_root() -> Path:
    """Return the immutable container/package location of the Vite build."""
    return Path(__file__).resolve().parent / "ui_dist"


def _mail_react_resolve_file(root: Path, relative_path: str) -> Path | None:
    """Resolve one existing regular file without allowing an escape from ``root``."""
    try:
        resolved_root = root.resolve(strict=True)
        candidate = (resolved_root / relative_path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate == resolved_root or not candidate.is_relative_to(resolved_root):
        return None
    return candidate if candidate.is_file() else None


class MailUiSessionPrincipal(TypedDict):
    """Internal identity facts revalidated by every self-service handler."""

    id: int
    username: str
    role: webauth.UiUserRole
    session_epoch: int
    session_generation: str


class MailUiDomainErrorDetail(BaseModel):
    """Stable machine-readable refusal emitted by a typed mutation."""

    model_config = ConfigDict(extra="forbid")

    code: UiAccessMutationErrorCode | UiProfileMutationErrorCode


class MailUiDomainErrorResponse(BaseModel):
    """FastAPI-compatible wrapper retaining the conventional ``detail`` key."""

    model_config = ConfigDict(extra="forbid")

    detail: MailUiDomainErrorDetail


class MailUiDomainOrValidationErrorResponse(BaseModel):
    """A domain refusal or FastAPI request-validation error at HTTP 422."""

    model_config = ConfigDict(extra="forbid")

    detail: MailUiDomainErrorDetail | list[dict[str, Any]]


class MailUiDeliveryErrorDetail(BaseModel):
    """Stable delivery refusal without persisted content or request values."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")


class MailUiDeliveryErrorResponse(BaseModel):
    """FastAPI wrapper for one typed delivery-domain refusal."""

    model_config = ConfigDict(extra="forbid")

    detail: MailUiDeliveryErrorDetail


class MailUiDeliveryOrValidationErrorResponse(BaseModel):
    """A delivery refusal or redacted FastAPI request-validation error."""

    model_config = ConfigDict(extra="forbid")

    detail: MailUiDeliveryErrorDetail | list[dict[str, Any]]


class MailUiProfileResponse(BaseModel):
    """Non-secret account profile for the authenticated human."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    username: str
    display_name: str | None
    global_role: MailUiGlobalRole
    profile_revision: int = Field(ge=1)


class MailUiProfilePatch(BaseModel):
    """Compare-and-swap update of the authenticated human's display label."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(max_length=1024)
    expected_profile_revision: int = Field(ge=1)


class MailUiProfileMutationResponse(BaseModel):
    """Result of one normalized display-name mutation."""

    model_config = ConfigDict(extra="forbid")

    changed: bool
    display_name: str | None
    profile_revision: int = Field(ge=1)


class MailUiAdminAssignmentSummary(BaseModel):
    """One explicit project role belonging to a human account."""

    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(gt=0)
    role: MailUiAssignmentRole


class MailUiAdminUserSummary(BaseModel):
    """Administrative account snapshot containing only access-management facts."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    username: str
    display_name: str | None
    disabled: bool
    global_role: MailUiGlobalRole
    account_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_version: int = Field(ge=1)
    assignments: list[MailUiAdminAssignmentSummary]


class MailUiAdminProjectSummary(BaseModel):
    """Project identity snapshot used to protect assignment mutations from reuse."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    slug: str
    human_key: str
    project_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    archived_at: datetime | None


class MailUiAdminAccessResponse(BaseModel):
    """Consistent user/project/assignment snapshot for the administrator UI."""

    model_config = ConfigDict(extra="forbid")

    users: list[MailUiAdminUserSummary]
    projects: list[MailUiAdminProjectSummary]


class MailUiAdminProjectAccessPut(BaseModel):
    """CAS inputs for grant, replacement, or revocation of one assignment."""

    model_config = ConfigDict(extra="forbid")

    role: MailUiAssignmentRole | None
    expected_access_version: int = Field(ge=1)
    account_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_project_generation: str = Field(pattern=r"^[0-9a-f]{64}$")


class MailUiAdminProjectAccessResponse(BaseModel):
    """Effective assignment and next CAS version after an admin request."""

    model_config = ConfigDict(extra="forbid")

    changed: bool
    role: MailUiAssignmentRole | None
    access_version: int = Field(ge=1)


_MAIL_UI_DOMAIN_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": MailUiDomainErrorResponse},
    status.HTTP_403_FORBIDDEN: {"model": MailUiDomainErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": MailUiDomainErrorResponse},
    status.HTTP_409_CONFLICT: {"model": MailUiDomainErrorResponse},
    status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": MailUiDomainErrorResponse},
}
_MAIL_UI_DOMAIN_MUTATION_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_MAIL_UI_DOMAIN_ERROR_RESPONSES,
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": MailUiDomainOrValidationErrorResponse
    },
}
_MAIL_UI_DELIVERY_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": MailUiDeliveryErrorResponse},
    status.HTTP_403_FORBIDDEN: {"model": MailUiDeliveryErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": MailUiDeliveryErrorResponse},
    status.HTTP_409_CONFLICT: {"model": MailUiDeliveryErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": MailUiDeliveryOrValidationErrorResponse
    },
    status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": MailUiDeliveryErrorResponse},
}
_MAIL_UI_DELIVERY_MUTATION_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_MAIL_UI_DELIVERY_ERROR_RESPONSES,
}
_MAIL_UI_SEARCH_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": MailUiDeliveryErrorResponse},
    status.HTTP_403_FORBIDDEN: {"model": MailUiDeliveryErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": MailUiDeliveryErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": MailUiDeliveryOrValidationErrorResponse
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": MailUiDeliveryErrorResponse},
}


class MailUiStoredPreferences(BaseModel):
    """Language preferences persisted on one human UI account."""

    preferred_ui_locale: MailUiLocale
    preferred_correspondence_locale: MailUiLocale | None


class MailUiEffectivePreferences(BaseModel):
    """Resolved languages after applying the correspondence inheritance rule."""

    ui_locale: MailUiLocale
    correspondence_locale: MailUiLocale


class MailUiPreferencesResponse(BaseModel):
    """Stored and resolved preferences for the authenticated human."""

    stored: MailUiStoredPreferences
    effective: MailUiEffectivePreferences


class MailUiPreferencesPatch(BaseModel):
    """Partial self-service update for the authenticated human's languages."""

    model_config = ConfigDict(extra="forbid")

    preferred_ui_locale: MailUiLocale = MailUiLocale.EN
    preferred_correspondence_locale: MailUiLocale | None = None

    @field_validator("preferred_ui_locale", "preferred_correspondence_locale", mode="before")
    @classmethod
    def canonicalize_locale(cls, value: object) -> object:
        """Canonicalize human-entered locale tags before the closed-set check."""
        if not isinstance(value, str):
            return value
        return MailUiLocale.canonicalize(value) or value


class MailUiPasswordPatch(BaseModel):
    """Bounded secrets for self-service password rotation.

    ``SecretStr`` keeps both fields masked in model representations and logs.
    Deliberately do not canonicalize or trim either value: whitespace and all
    other Unicode code points are valid password material.
    """

    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr = Field(min_length=1, max_length=1024)
    new_password: SecretStr = Field(min_length=15, max_length=1024)

    @field_validator("current_password", "new_password")
    @classmethod
    def require_utf8(cls, value: SecretStr) -> SecretStr:
        """Reject lone surrogates before either secret can reach scrypt."""
        try:
            value.get_secret_value().encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Password must be valid UTF-8 Unicode text.") from None
        return value


def _mail_ui_locale_from_db(value: object) -> MailUiLocale:
    """Return one schema-guarded locale without silently widening corruption."""
    try:
        return MailUiLocale(value)
    except (TypeError, ValueError):
        raise RuntimeError("authenticated human has an invalid locale") from None


class MailUiPasswordChangeResponse(BaseModel):
    """Stable success response for a completed password rotation."""

    changed: Literal[True] = True


class MailUiProjectSummary(BaseModel):
    """One project visible to the authenticated human."""

    model_config = ConfigDict(extra="forbid")

    id: int
    slug: str
    human_key: str
    created_at: datetime
    archived_at: datetime | None
    role: MailUiProjectRole
    can_reply: bool


class MailUiProjectsResponse(BaseModel):
    """Complete, authorization-filtered project list for the React shell."""

    model_config = ConfigDict(extra="forbid")

    items: list[MailUiProjectSummary]
    total: int


class MailUiAgentDirectoryItem(BaseModel):
    """One active agent identity that can receive an administrator message."""

    model_config = ConfigDict(extra="forbid")

    agent_id: int = Field(gt=0)
    agent_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=128)
    display_name: str | None
    # The tone this colleague picked, so a reader can tell who wrote without
    # looking. Only the vocabulary word travels; the browser synthesises the
    # tone locally. Nothing here may become a request to a host a colleague
    # chose — that was the rule when the server-rendered UI grew this, and it
    # survives the move to the React client unchanged.
    notify_sound: str | None = None


class MailUiProjectAgentsResponse(BaseModel):
    """Privacy-minimal recipient directory for one active project."""

    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(gt=0)
    project_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: list[MailUiAgentDirectoryItem]
    total: int = Field(ge=0)


class MailUiMessageSummary(BaseModel):
    """Metadata safe for the unified inbox list.

    Message bodies, recipient lists, and per-recipient read state deliberately
    do not exist on this model.  Keeping the list contract separate from the
    detail contract makes an accidental over-fetch visible in OpenAPI and in
    response validation instead of silently shipping private fields.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    project_id: int
    project_slug: str
    subject: str
    sender: str
    sender_name: str
    sender_display_name: str | None
    importance: MailUiImportance
    ack_required: bool
    thread_id: str | None
    reply_to: int | None
    created_ts: datetime
    can_reply: bool


class MailUiAttachmentMetadata(BaseModel):
    """Non-locating attachment metadata safe to expose to a browser."""

    model_config = ConfigDict(extra="forbid")

    type: str | None
    media_type: str | None
    size_bytes: int | None


class MailUiReplyTarget(BaseModel):
    """Immutable, privacy-minimal destination required to confirm a reply."""

    model_config = ConfigDict(extra="forbid")

    agent_id: int = Field(gt=0)
    agent_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: int = Field(gt=0)
    project_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_name: str = Field(min_length=1, max_length=384)


class MailUiMessageDetail(MailUiMessageSummary):
    """Full message content without blind-copy or storage-location metadata."""

    body_md: str
    to: list[str]
    cc: list[str]
    attachments: list[MailUiAttachmentMetadata]
    reply_target: MailUiReplyTarget | None


class MailUiInboxResponse(BaseModel):
    """A stable page of unified inbox summaries."""

    model_config = ConfigDict(extra="forbid")

    items: list[MailUiMessageSummary]
    total: int
    next_cursor: str | None


class MailUiSearchItem(MailUiMessageSummary):
    """One privacy-minimal full-text result with a plain-text excerpt."""

    snippet: str = Field(max_length=_MAIL_UI_SEARCH_SNIPPET_MAX_LENGTH)


class MailUiSearchResponse(BaseModel):
    """One query-bound keyset page of visible full-text results."""

    model_config = ConfigDict(extra="forbid")

    items: list[MailUiSearchItem]
    next_cursor: str | None


class MailUiThreadResponse(BaseModel):
    """A stable page of full messages from one visible thread."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    items: list[MailUiMessageDetail]
    total: int
    next_cursor: str | None


class MailUiComposeRecipient(BaseModel):
    """One immutable recipient lifetime selected from the typed directory."""

    model_config = ConfigDict(extra="forbid")

    agent_id: int = Field(gt=0)
    expected_agent_generation: str = Field(pattern=r"^[0-9a-f]{64}$")


class MailUiComposeRequest(BaseModel):
    """Strict, idempotent administrator-authored project message."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_project_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    recipients: list[MailUiComposeRecipient] = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=200)
    body_md: str = Field(min_length=1, max_length=50_000)
    thread_id: str | None = Field(default=None, max_length=128)

    @field_validator("idempotency_key", "subject", "body_md")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must contain a non-whitespace character.")
        return value

    @field_validator("recipients")
    @classmethod
    def require_unique_recipient_lifetimes(
        cls,
        value: list[MailUiComposeRecipient],
    ) -> list[MailUiComposeRecipient]:
        if len({recipient.agent_id for recipient in value}) != len(value):
            raise ValueError("Recipients must be unique.")
        return value


class MailUiReplyRequest(BaseModel):
    """Strict reply bound to the original sender and project lifetimes."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_sender_agent_id: int = Field(gt=0)
    expected_sender_agent_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_sender_project_id: int = Field(gt=0)
    expected_sender_project_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_md: str = Field(min_length=1, max_length=50_000)

    @field_validator("idempotency_key", "body_md")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must contain a non-whitespace character.")
        return value


class MailUiDeliveryResponse(BaseModel):
    """One durable delivery outcome safe for browser polling."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    status: Literal["published", "pending", "quarantined", "busy", "deferred"]
    reused: bool
    message_id: int | None
    commit_sha: str | None
    next_attempt_ts: datetime | None


_MAIL_UI_CURSOR_VERSION = 1
_MAIL_UI_CURSOR_MAX_LENGTH = 512
_MAIL_UI_CREATED_TS_KEY_SQL = (
    "CASE "
    "WHEN instr(CAST(m.created_ts AS TEXT), '.') = 0 "
    "THEN CAST(m.created_ts AS TEXT) || '.000000' "
    "ELSE substr(CAST(m.created_ts AS TEXT), 1, "
    "instr(CAST(m.created_ts AS TEXT), '.') - 1) || '.' || "
    "substr(substr(CAST(m.created_ts AS TEXT), "
    "instr(CAST(m.created_ts AS TEXT), '.') + 1) || '000000', 1, 6) "
    "END"
)


async def _mail_ui_begin_read_snapshot(session: AsyncSession) -> None:
    """Start one stable read snapshot without taking SQLite's writer lock.

    Python's SQLite driver uses legacy transaction control, where a ``SELECT``
    does not emit ``BEGIN``.  A sequence of reads can therefore observe several
    database states unless the transaction is started explicitly.  Plain
    ``BEGIN`` remains deferred: the first read pins the WAL snapshot while
    concurrent writers remain free to commit.  Other dialects use their
    portable serializable isolation level to provide the same multi-statement
    snapshot contract.
    """
    if session.get_bind().dialect.name == "sqlite":
        connection = await session.connection()
        await connection.exec_driver_sql("BEGIN")
        return
    await session.connection(
        execution_options={"isolation_level": "SERIALIZABLE"},
    )


def _mail_ui_datetime(value: Any) -> datetime:
    """Normalize one database timestamp to an aware UTC datetime.

    Invalid persisted timestamps are data corruption and intentionally fail the
    request instead of being replaced with the current time, which would make
    keyset pagination duplicate or skip messages without an error.
    """
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        result = datetime.fromisoformat(normalized)
    else:
        raise ValueError("database timestamp is not a datetime")
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _mail_ui_delivery_response(
    acceptance: MessageDeliveryAcceptance,
    processing: MessageDeliveryProcessingResult,
) -> MailUiDeliveryResponse:
    """Combine immutable acceptance facts with the latest processing outcome."""
    return MailUiDeliveryResponse(
        id=acceptance.delivery_id,
        status=processing.status,
        reused=acceptance.reused,
        message_id=processing.message_id,
        commit_sha=processing.commit_sha,
        next_attempt_ts=processing.next_attempt_ts,
    )


def _mail_ui_delivery_status_response(
    processing: MessageDeliveryProcessingResult,
) -> MailUiDeliveryResponse:
    """Render a status poll when no acceptance reuse flag exists."""
    return MailUiDeliveryResponse(
        id=processing.delivery_id,
        status=processing.status,
        reused=True,
        message_id=processing.message_id,
        commit_sha=processing.commit_sha,
        next_attempt_ts=processing.next_attempt_ts,
    )


def _mail_ui_delivery_exception(exc: Exception) -> HTTPException:
    """Translate delivery-domain refusals without exposing persisted content."""
    if isinstance(exc, MessageDeliveryValidationError):
        code = (
            exc.code
            if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", exc.code)
            else "delivery_failed"
        )
        if code in {
            "ui_actor_lifetime_invalid",
            "ui_actor_role_invalid",
            "ui_actor_operator_required",
            "ui_actor_admin_required",
        }:
            status_code = status.HTTP_403_FORBIDDEN
        elif code.endswith("_lifetime_invalid") or code in {
            "reply_route_invalid",
            "cross_project_route_revoked",
            "reply_target_invalid",
            "recipient_blocked",
        }:
            status_code = status.HTTP_409_CONFLICT
        else:
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        return HTTPException(status_code=status_code, detail={"code": code})
    if isinstance(exc, MessageDeliveryIdempotencyConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "idempotency_conflict"},
        )
    if isinstance(exc, MessageDeliveryNotFoundError):
        return HTTPException(status_code=404, detail={"code": "delivery_not_found"})
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": "delivery_failed"},
    )


def _mail_ui_delivery_http_exception(exc: HTTPException) -> HTTPException:
    """Normalize access races into the advertised delivery error shape."""
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        code = "project_not_found"
    elif exc.status_code in {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    }:
        code = "actor_forbidden"
    else:
        return exc
    return HTTPException(status_code=exc.status_code, detail={"code": code})


def _mail_ui_typed_delivery_endpoint(
    handler: Callable[..., Any],
) -> Callable[..., Any]:
    """Return stable typed failures without logging authored exception text."""
    handler_name = getattr(handler, "__name__", type(handler).__name__)

    @functools.wraps(handler)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await handler(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            with contextlib.suppress(Exception):
                structlog.get_logger("mail_ui.delivery").error(
                    "delivery_request_failed",
                    handler=handler_name,
                    exception_type=type(exc).__name__,
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "delivery_failed"},
            ) from None

    return wrapped


def _mail_ui_overseer_preamble(locale: MailUiLocale) -> str:
    """Server-authored provenance and language advisory for human mail."""
    return (
        "---\n\n"
        "MESSAGE FROM HUMAN OVERSEER\n\n"
        "This message is from an authenticated human operator overseeing this "
        "project. Prioritize the request below over the current task unless a "
        "higher-priority instruction conflicts.\n\n"
        f"{_mail_ui_correspondence_advisory(locale)}\n\n"
        "---\n\n"
    )


async def _mail_ui_delivery_context(
    *,
    session: AsyncSession,
    request: Request,
    project_id: int,
    compose: bool,
) -> tuple[
    DeliveryProjectSnapshot,
    DeliveryAgentSnapshot,
    DeliveryActorSnapshot,
    MailUiLocale,
]:
    """Revalidate a human and resolve the immutable HumanOverseer mailbox."""
    try:
        principal = _mail_ui_preferences_principal(request)
        user = await _mail_ui_preferences_user(request, session)
        role = webauth.normalize_ui_user_role(user.role)
        if role is None or role != principal["role"]:
            raise HTTPException(status_code=401, detail={"code": "actor_forbidden"})
        access = await _mail_ui_require_project_access(
            settings=get_settings(),
            request=request,
            session=session,
            project_id=project_id,
            operate=not compose,
        )
    except HTTPException as exc:
        raise _mail_ui_delivery_http_exception(exc) from None
    if compose and not access["can_compose"]:
        raise HTTPException(status_code=403, detail={"code": "actor_forbidden"})
    project = await session.get(Project, project_id)
    if project is None or project.id is None or project.archived_at is not None:
        raise HTTPException(status_code=404, detail={"code": "project_not_found"})
    sender_result = await session.execute(
        select(Agent).where(
            cast(Any, Agent.project_id == project_id),
            cast(Any, Agent.name == "HumanOverseer"),
            cast(Any, Agent.provisioning_state == "active"),
        )
    )
    sender = sender_result.scalar_one_or_none()
    if sender is None:
        sender = Agent(
            project_id=project_id,
            name="HumanOverseer",
            program="WebUI",
            model="Human",
            task_description="Authenticated human operator",
            contact_policy="open",
            attachments_policy="auto",
        )
        session.add(sender)
        await session.flush()
    if sender.id is None or sender.retired_at is not None:
        raise HTTPException(status_code=409, detail={"code": "sender_unavailable"})
    project_snapshot = DeliveryProjectSnapshot(
        project_id=project_id,
        slug=project.slug,
        generation=project.project_generation,
    )
    sender_snapshot = DeliveryAgentSnapshot(
        agent_id=int(sender.id),
        name=sender.name,
        generation=sender.agent_generation,
        project=project_snapshot,
    )
    actor = DeliveryActorSnapshot.ui_user(
        user_id=principal["id"],
        username=principal["username"],
        generation=principal["session_generation"],
        epoch=principal["session_epoch"],
        source_project=project_snapshot,
    )
    ui_locale = _mail_ui_locale_from_db(user.preferred_ui_locale)
    correspondence_locale = _mail_ui_locale_from_db(
        user.preferred_correspondence_locale or ui_locale,
    )
    return project_snapshot, sender_snapshot, actor, correspondence_locale


async def _mail_ui_delivery_recipient(
    *,
    session: AsyncSession,
    agent: Agent,
) -> DeliveryRecipientSnapshot:
    """Build one live target-project agent snapshot for a durable request."""
    project = await session.get(Project, agent.project_id)
    if (
        project is None
        or project.id is None
        or project.archived_at is not None
        or agent.id is None
        or agent.retired_at is not None
    ):
        raise HTTPException(status_code=409, detail={"code": "recipient_unavailable"})
    return DeliveryRecipientSnapshot(
        kind="to",
        agent=DeliveryAgentSnapshot(
            agent_id=int(agent.id),
            name=agent.name,
            generation=agent.agent_generation,
            project=DeliveryProjectSnapshot(
                project_id=int(project.id),
                slug=project.slug,
                generation=project.project_generation,
            ),
        ),
    )


async def _mail_ui_require_owned_delivery(
    *,
    request: Request,
    delivery_id: str,
) -> None:
    """Hide every delivery outside the current human account lifetime."""
    if (
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            delivery_id,
        )
        is None
    ):
        raise HTTPException(status_code=404, detail={"code": "delivery_not_found"})
    try:
        principal = _mail_ui_preferences_principal(request)
        async with get_session() as session:
            await _mail_ui_preferences_user(request, session)
            delivery = await session.get(MessageDelivery, delivery_id)
    except HTTPException as exc:
        raise _mail_ui_delivery_http_exception(exc) from None
    if (
        delivery is None
        or delivery.actor_kind != "ui_user"
        or delivery.actor_id != principal["id"]
        or delivery.actor_generation_snapshot != principal["session_generation"]
        or delivery.actor_epoch_snapshot != principal["session_epoch"]
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "delivery_not_found"},
        )


def _mail_ui_importance(value: Any) -> MailUiImportance:
    """Project legacy free-form importance onto the typed browser vocabulary."""
    normalized = str(value or "normal").strip().casefold()
    if normalized in {"low", "normal", "high", "urgent"}:
        return normalized
    return "normal"


def _mail_ui_encode_cursor(created_ts_key: str, message_id: int) -> str:
    """Encode the exact SQLite ordering key as an opaque URL-safe cursor."""
    payload = json.dumps(
        {"v": _MAIL_UI_CURSOR_VERSION, "created_ts": created_ts_key, "id": message_id},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _mail_ui_decode_cursor(cursor: str) -> tuple[str, int]:
    """Decode and strictly validate one keyset cursor.

    The cursor is not an authorization token.  Project visibility is always
    applied independently in SQL; this validation only prevents malformed
    ordering keys from turning into surprising pages or database expressions.
    """
    if not cursor or len(cursor) > _MAIL_UI_CURSOR_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid cursor.",
        )
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            (cursor + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"v", "created_ts", "id"}:
            raise ValueError("unexpected cursor shape")
        if payload["v"] != _MAIL_UI_CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
        created_ts = payload["created_ts"]
        message_id = payload["id"]
        if not isinstance(created_ts, str) or not created_ts or len(created_ts) > 64:
            raise ValueError("invalid timestamp key")
        _mail_ui_datetime(created_ts)
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or not 0 < message_id <= 9_223_372_036_854_775_807
        ):
            raise ValueError("invalid message id")
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid cursor.",
        ) from None
    return created_ts, message_id


def _mail_ui_invalid_search_query() -> HTTPException:
    """Return the stable typed refusal for a human search expression."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": "invalid_search_query"},
    )


def _mail_ui_compile_search_query(
    raw_query: str,
    scope: MailUiSearchScope,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Compile a bounded literal query into FTS5 syntax.

    Every user term is quoted by the server. The browser therefore receives a
    useful word/phrase search without exposing FTS boolean operators, column
    expressions, or prefix grammar as an executable query language.
    """
    query = raw_query.strip()
    if (
        not query
        or len(query) > 256
        or any(unicodedata.category(character).startswith("C") for character in query)
    ):
        raise _mail_ui_invalid_search_query()

    expressions: list[str] = []
    ranking_terms: list[tuple[str, str]] = []
    token_count = 0
    previous_end = 0
    for match in _MAIL_UI_SEARCH_TOKEN_RE.finditer(query):
        if query[previous_end : match.start()].strip():
            raise _mail_ui_invalid_search_query()
        previous_end = match.end()
        value = match.group("phrase") or match.group("word") or ""
        value = value.strip()
        lexical_tokens = re.findall(r"\w+", value, flags=re.UNICODE)
        if not value or not lexical_tokens:
            raise _mail_ui_invalid_search_query()
        token_count += len(lexical_tokens)
        if token_count > _MAIL_UI_SEARCH_MAX_TOKENS:
            raise _mail_ui_invalid_search_query()

        explicit_field = match.group("field")
        field = explicit_field.casefold() if explicit_field is not None else None
        escaped = value.replace('"', '""')
        literal = f'"{escaped}"'
        if field in {"subject", "body"}:
            expressions.append(f"{field}:{literal}")
            ranking_terms.append((field, value))
        elif scope == "subject":
            expressions.append(f"subject:{literal}")
            ranking_terms.append(("subject", value))
        elif scope == "body":
            expressions.append(f"body:{literal}")
            ranking_terms.append(("body", value))
        else:
            expressions.append(f"(subject:{literal} OR body:{literal})")
            ranking_terms.append(("all", value))

    if query[previous_end:].strip() or not expressions:
        raise _mail_ui_invalid_search_query()
    return " AND ".join(expressions), tuple(ranking_terms)


def _mail_ui_local_search_rank(
    ranking_terms: tuple[tuple[str, str], ...],
) -> tuple[str, dict[str, str]]:
    """Return a row-local relevance score unaffected by invisible corpus rows."""
    score_parts: list[str] = []
    parameters: dict[str, str] = {}
    for index, (field, value) in enumerate(ranking_terms):
        parameter = f"search_rank_term_{index}"
        parameters[parameter] = value

        def occurrences(column: str, bound_parameter: str = parameter) -> str:
            normalized = f"lower(COALESCE({column}, ''))"
            return (
                f"((length({normalized}) - length(replace({normalized}, "
                f"lower(:{bound_parameter}), ''))) / "
                f"max(length(:{bound_parameter}), 1))"
            )

        if field == "subject":
            score_parts.append(f"(4 * {occurrences('m.subject')})")
        elif field == "body":
            score_parts.append(occurrences("m.body_md"))
        else:
            score_parts.append(
                f"((4 * {occurrences('m.subject')}) + {occurrences('m.body_md')})"
            )
    if not score_parts:
        raise _mail_ui_invalid_search_query()
    # The search keyset already sorts ascending. Negating the positive score
    # preserves that shape while ranking more row-local matches first.
    return f"-({' + '.join(score_parts)})", parameters


def _mail_ui_search_fingerprint(
    *,
    fts_query: str,
    project_id: int | None,
    scope: MailUiSearchScope,
    order: MailUiSearchOrder,
) -> str:
    """Bind a cursor to the exact normalized search without storing query text."""
    canonical = json.dumps(
        {
            "order": order,
            "project_id": project_id,
            "query": fts_query,
            "scope": scope,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _mail_ui_encode_search_cursor(
    *,
    fingerprint: str,
    created_ts_key: str,
    message_id: int,
    rank: float | None,
) -> str:
    """Encode one stable search ordering key as a URL-safe cursor."""
    payload = json.dumps(
        {
            "created_ts": created_ts_key,
            "fingerprint": fingerprint,
            "id": message_id,
            "rank": rank,
            "v": _MAIL_UI_CURSOR_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _mail_ui_decode_search_cursor(
    cursor: str,
    *,
    expected_fingerprint: str,
    order: MailUiSearchOrder,
) -> tuple[float | None, str, int]:
    """Validate one query-bound search keyset cursor."""
    if not cursor or len(cursor) > _MAIL_UI_CURSOR_MAX_LENGTH:
        raise _mail_ui_invalid_search_query()
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            (cursor + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "created_ts",
            "fingerprint",
            "id",
            "rank",
            "v",
        }:
            raise ValueError("unexpected cursor shape")
        if payload["v"] != _MAIL_UI_CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
        fingerprint = payload["fingerprint"]
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or not hmac.compare_digest(fingerprint, expected_fingerprint)
        ):
            raise ValueError("cursor belongs to a different query")
        created_ts = payload["created_ts"]
        message_id = payload["id"]
        rank = payload["rank"]
        if not isinstance(created_ts, str) or not created_ts or len(created_ts) > 64:
            raise ValueError("invalid timestamp key")
        _mail_ui_datetime(created_ts)
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise ValueError("invalid message id")
        if order == "relevance":
            if (
                isinstance(rank, bool)
                or not isinstance(rank, (float, int))
                or not math.isfinite(float(rank))
            ):
                raise ValueError("invalid relevance key")
            normalized_rank: float | None = float(rank)
        elif rank is not None:
            raise ValueError("unexpected relevance key")
        else:
            normalized_rank = None
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise _mail_ui_invalid_search_query() from None
    return normalized_rank, created_ts, message_id


def _mail_ui_plain_search_snippet(value: Any) -> str:
    """Return a bounded, control-free, plain-text FTS excerpt."""
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= _MAIL_UI_SEARCH_SNIPPET_MAX_LENGTH:
        return normalized
    return normalized[: _MAIL_UI_SEARCH_SNIPPET_MAX_LENGTH - 1].rstrip() + "…"


def _mail_ui_safe_attachments(value: Any) -> list[MailUiAttachmentMetadata]:
    """Project stored attachment JSON onto a non-locating public shape."""
    raw_items = value
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw_items, list):
        return []

    result: list[MailUiAttachmentMetadata] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        raw_type = raw.get("type")
        raw_media_type = raw.get("media_type")
        raw_size = raw.get("size_bytes", raw.get("bytes"))
        attachment_type = raw_type[:64] if isinstance(raw_type, str) else None
        media_type = raw_media_type[:255] if isinstance(raw_media_type, str) else None
        size_bytes = (
            raw_size
            if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size >= 0
            else None
        )
        result.append(
            MailUiAttachmentMetadata(
                type=attachment_type,
                media_type=media_type,
                size_bytes=size_bytes,
            )
        )
    return result

# Login brute-force throttle, keyed by client address and normalized username. scrypt already caps an
# attacker at roughly 20 guesses/second/core, but that is still ~1.7M/day against
# a weak password, so cap it properly. In-process state is sufficient and correct
# here precisely because this server must run as a single process anyway (stateful
# MCP sessions live in memory too) — there is no second worker to share it with.
_LOGIN_MAX_FAILURES = 8
_LOGIN_WINDOW_SECONDS = 300.0
_login_failures: dict[str, list[float]] = {}

# Password rotation is intentionally stricter than login throttling. A caller
# already has a valid session, so the key is the immutable account lifetime
# rather than an attacker-controlled address or username. Registration is a
# synchronous operation which the endpoint performs before its first await;
# concurrent requests therefore cannot all pass the threshold before yielding.
_PASSWORD_CHANGE_MAX_ATTEMPTS = 5
_PASSWORD_CHANGE_WINDOW_SECONDS = 15 * 60.0
_PASSWORD_CHANGE_MAX_KEYS = 4096
_password_change_attempts: dict[tuple[int, str], list[float]] = {}


def _password_change_clock() -> float:
    """Return a monotonic instant, isolated for deterministic limiter tests."""
    import time

    return time.monotonic()


def _password_change_register_attempt(*, user_id: int, generation: str) -> int | None:
    """Register an attempt or return the seconds until this account may retry."""
    now = _password_change_clock()
    key = (user_id, generation)

    # Never evict a live bucket: doing so would let account churn reset that
    # lifetime's attempt count. At capacity, first reclaim only fully expired
    # buckets. If every slot is live, fail closed until the earliest complete
    # bucket can expire, without inserting a 4097th key.
    if key not in _password_change_attempts and len(_password_change_attempts) >= _PASSWORD_CHANGE_MAX_KEYS:
        stale = [
            candidate
            for candidate, attempts in _password_change_attempts.items()
            if not attempts or now - attempts[-1] >= _PASSWORD_CHANGE_WINDOW_SECONDS
        ]
        for candidate in stale:
            _password_change_attempts.pop(candidate, None)
        if len(_password_change_attempts) >= _PASSWORD_CHANGE_MAX_KEYS:
            earliest_release = min(attempts[-1] for attempts in _password_change_attempts.values())
            remaining = _PASSWORD_CHANGE_WINDOW_SECONDS - (now - earliest_release)
            return max(1, math.ceil(remaining))

    recent = [
        instant
        for instant in _password_change_attempts.get(key, [])
        if now - instant < _PASSWORD_CHANGE_WINDOW_SECONDS
    ]
    if len(recent) >= _PASSWORD_CHANGE_MAX_ATTEMPTS:
        _password_change_attempts[key] = recent
        remaining = _PASSWORD_CHANGE_WINDOW_SECONDS - (now - recent[0])
        return max(1, math.ceil(remaining))

    recent.append(now)
    _password_change_attempts[key] = recent
    return None


def _login_throttled(client: str) -> bool:
    import time

    now = time.time()
    recent = [t for t in _login_failures.get(client, []) if now - t < _LOGIN_WINDOW_SECONDS]
    if recent:
        _login_failures[client] = recent
    else:
        _login_failures.pop(client, None)
    return len(recent) >= _LOGIN_MAX_FAILURES


def _login_record_failure(client: str) -> None:
    import time

    now = time.time()
    bucket = [t for t in _login_failures.get(client, []) if now - t < _LOGIN_WINDOW_SECONDS]
    bucket.append(now)
    _login_failures[client] = bucket
    # Bound the dict so a spray across forged client/account pairs cannot grow it
    # without limit.
    if len(_login_failures) > 4096:
        stale = [k for k, v in _login_failures.items() if not v or now - v[-1] > _LOGIN_WINDOW_SECONDS]
        for k in stale:
            _login_failures.pop(k, None)


def _login_clear_failures(client: str) -> None:
    _login_failures.pop(client, None)


class MailUiAuthMiddleware(BaseHTTPMiddleware):
    """Password-session auth for the ``/mail`` viewer.

    Installed OUTSIDE :class:`BearerAuthMiddleware` so it renders the verdict for
    ``/mail`` first. That ordering is the whole point: a browser cannot attach an
    ``Authorization`` header to a normal navigation, so if the bearer middleware
    saw these requests first every human would get a bare 401 with nowhere to log
    in. Requests outside ``/mail`` are passed straight through untouched — the MCP
    mounts keep their bearer-only behaviour exactly as before.

    Two ways a normal ``/mail`` request may proceed:

    1. A valid session cookie. The user row is re-read on every request, so
       ``disabled`` and ``session_epoch`` changes take effect immediately rather
       than whenever the cookie happens to expire.
    2. The login and logout endpoints themselves.

    The static service bearer is accepted only for the read-only file-reservation
    endpoint used by editor hooks. It cannot read the human mailbox, archive, or
    event stream.

    Anything else is redirected to the login page (for navigations) or answered
    401 (for anything expecting JSON).

    State-changing requests carry two extra requirements, because the UI exposes
    genuinely destructive routes (delete-messages, retire-agent, archive-project,
    overseer/send): the session must belong to an ``admin``, and the request must
    be same-origin. Combined with the ``SameSite=Lax`` cookie, a cross-site POST
    is blocked by the browser and again by the server.
    """

    def __init__(self, app: FastAPI, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    @staticmethod
    def _wants_html(request: Request) -> bool:
        """Whether to answer with a redirect to the login page rather than JSON."""
        if request.method != "GET":
            return False
        return "text/html" in request.headers.get("accept", "")

    def _public_entrypoint(self, request: Request) -> Response | None:
        """Serve only the public root redirect and passive Iris favicon."""
        if request.method not in {"GET", "HEAD"}:
            return None

        path = request.url.path
        mcp_base = _normalized_http_base_path(self._settings.http.path)
        if path == "/" and mcp_base != "/":
            raw_query = cast(bytes, request.scope.get("query_string", b""))
            location = _MAIL_REACT_BASE_PATH
            if raw_query:
                location = f"{location}?{raw_query.decode('latin-1')}"
            return Response(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={**_MAIL_REACT_INDEX_HEADERS, "Location": location},
            )

        if path == _IRIS_FAVICON_PATH and mcp_base not in {"/", _IRIS_FAVICON_PATH}:
            body = _IRIS_FAVICON_SVG if request.method == "GET" else b""
            return Response(
                content=body,
                media_type="image/svg+xml",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Content-Length": str(len(_IRIS_FAVICON_SVG)),
                    "X-Content-Type-Options": "nosniff",
                },
            )
        return None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        public_entrypoint = self._public_entrypoint(request)
        if public_entrypoint is not None:
            return public_entrypoint

        path = request.url.path
        if not (path == "/mail" or path.startswith("/mail/")):
            return await call_next(request)
        raw_legacy_bookmark = _mail_ui_legacy_bookmark(path)
        legacy_bookmark = _mail_ui_request_legacy_bookmark(request)
        if raw_legacy_bookmark is not None and legacy_bookmark is None:
            return JSONResponse(
                {"detail": "Not Found"},
                status_code=status.HTTP_404_NOT_FOUND,
                headers=_MAIL_REACT_INDEX_HEADERS,
            )
        if not _mail_ui_active_path(path):
            return JSONResponse(
                {"detail": "Not Found"},
                status_code=status.HTTP_404_NOT_FOUND,
                headers=_MAIL_REACT_INDEX_HEADERS,
            )
        if legacy_bookmark is not None and request.method not in {"GET", "HEAD"}:
            return JSONResponse(
                {"detail": "Not Found"},
                status_code=status.HTTP_404_NOT_FOUND,
                headers=_MAIL_REACT_INDEX_HEADERS,
            )
        if request.method == "OPTIONS":
            return await call_next(request)

        cfg = self._settings.mail_ui
        if not cfg.enabled:
            if self._settings.environment.strip().lower() not in {"development", "test"}:
                return JSONResponse(
                    {"detail": "Mail UI authentication may only be disabled in development or test."},
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            # Auth explicitly switched off: fall through to the bearer middleware,
            # which is then the only thing standing in front of the UI.
            token = _mail_ui_template_user.set(None)
            try:
                if legacy_bookmark is not None:
                    return await _mail_ui_legacy_bookmark_redirect(
                        settings=self._settings,
                        request=request,
                        bookmark=legacy_bookmark,
                    )
                return await call_next(request)
            finally:
                _mail_ui_template_user.reset(token)
        if not cfg.session_secret:
            # Fail closed. An unset secret cannot sign cookies, and serving the
            # destructive UI unauthenticated is never the safer default.
            return JSONResponse(
                {"detail": "Mail UI authentication is unconfigured (MAIL_UI_SESSION_SECRET is empty)."},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if path in (_MAIL_LOGIN_PATH, _MAIL_LOGOUT_PATH):
            request.state.mail_ui_authenticated = True  # let the bearer layer stand aside
            token = _mail_ui_template_user.set(None)
            try:
                return await call_next(request)
            finally:
                _mail_ui_template_user.reset(token)

        # The standalone login page needs exactly one locally built stylesheet
        # and its exact self-hosted flag font before a browser can possess a
        # session cookie. Keep this exception deliberately narrower than the
        # asset namespace: the legacy runtime JavaScript and every hashed
        # application asset remain session-gated.
        if (
            request.method in {"GET", "HEAD"}
            and path
            in {
                _MAIL_LOGIN_STYLESHEET_PATH,
                _MAIL_LOGIN_FLAG_FONT_PATH,
            }
        ):
            request.state.mail_ui_authenticated = True
            token = _mail_ui_template_user.set(None)
            try:
                return await call_next(request)
            finally:
                _mail_ui_template_user.reset(token)

        token = request.cookies.get(cfg.cookie_name, "")
        user = await _load_session_user(token, settings=self._settings) if token else None

        if user is None:
            static_bearer = self._settings.http.bearer_token or ""
            presented_bearer = request.headers.get("Authorization", "")
            if (
                request.method == "GET"
                and path == _MAIL_FILE_RESERVATIONS_API_PATH
                and bool(static_bearer)
                and hmac.compare_digest(presented_bearer, f"Bearer {static_bearer}")
            ):
                request.state.mail_ui_service_principal = True
                token = _mail_ui_template_user.set(None)
                try:
                    return await call_next(request)
                finally:
                    _mail_ui_template_user.reset(token)
            if self._wants_html(request):
                raw_path = request.scope.get("raw_path")
                target = (
                    raw_path.decode("ascii")
                    if isinstance(raw_path, bytes)
                    and raw_path.isascii()
                    else request.url.path
                )
                if request.url.query:
                    target = f"{target}?{request.url.query}"

                return Response(
                    status_code=status.HTTP_303_SEE_OTHER,
                    headers={
                        **_MAIL_LEGACY_HTML_HEADERS,
                        "Location": f"{_MAIL_LOGIN_PATH}?next={quote(target, safe='')}",
                    },
                )
            return JSONResponse(
                {"detail": _mail_ui_authorization_detail(path, "Unauthorized")},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        if request.method in _UNSAFE_METHODS:
            if not webauth.same_origin(
                request.headers.get("origin", ""),
                request.headers.get("referer", ""),
                request.headers.get("host", ""),
                expected_scheme=request.url.scheme,
            ):
                return JSONResponse(
                    {
                        "detail": _mail_ui_authorization_detail(
                            path,
                            "Cross-origin request rejected",
                        )
                    },
                    status_code=status.HTTP_403_FORBIDDEN,
                )
            if (
                user["role"] != webauth.ROLE_ADMIN
                and path not in _MAIL_ACCOUNT_API_PATHS
                and not _OVERSEER_REPLY_PATH_RE.fullmatch(path)
                and not _MAIL_API_REPLY_SHAPE_RE.fullmatch(path)
                and not (
                    _MAIL_API_DELIVERY_SHAPE_RE.fullmatch(path)
                    and path.endswith("/retry")
                )
            ):
                return JSONResponse(
                    {
                        "detail": _mail_ui_authorization_detail(
                            path,
                            "Forbidden: this action requires the admin role",
                        )
                    },
                    status_code=status.HTTP_403_FORBIDDEN,
                )

        request.state.mail_ui_authenticated = True
        request.state.mail_ui_user = user
        template_user = {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "is_admin": user["role"] == webauth.ROLE_ADMIN,
        }
        token = _mail_ui_template_user.set(template_user)
        try:
            if legacy_bookmark is not None:
                return await _mail_ui_legacy_bookmark_redirect(
                    settings=self._settings,
                    request=request,
                    bookmark=legacy_bookmark,
                )
            return await call_next(request)
        finally:
            _mail_ui_template_user.reset(token)


class MailUiAccountNoStoreMiddleware:
    """Prevent caching of session-bound Mail UI API responses."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path")
        if scope.get("type") != "http" or not (
            isinstance(path, str)
            and (path == "/mail/api" or path.startswith("/mail/api/"))
        ):
            await self._app(scope, receive, send)
            return

        async def send_no_store(message: MutableMapping[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if bytes(name).lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(cast(Any, message))

        await self._app(scope, receive, cast(Send, send_no_store))


async def _load_session_user(
    token: str,
    *,
    settings: Settings,
) -> MailUiSessionPrincipal | None:
    """Resolve a session cookie to a live, enabled user row, or ``None``.

    Re-reads the database on every request rather than trusting the cookie's
    contents. The signed payload carries both ``session_epoch`` and the
    account-lifetime ``session_generation``; if either stored value differs,
    the cookie is stale and is refused even though its signature is still valid
    and it has not expired.
    """
    import time

    verified = webauth.verify_session(
        token, now=time.time(), secret=settings.mail_ui.session_secret.encode("utf-8")
    )
    if verified is None:
        return None
    username, epoch, generation = verified
    try:
        from sqlmodel import select

        from .models import UiUser

        async with get_session() as session:
            result = await session.execute(select(UiUser).where(UiUser.username == username))
            row = result.scalars().first()
            if (
                row is None
                or row.disabled
                or row.session_epoch != epoch
                or not isinstance(row.session_generation, str)
                or not row.session_generation
                or not hmac.compare_digest(row.session_generation, generation)
            ):
                return None
            role = webauth.normalize_ui_user_role(row.role)
            if role is None or row.id is None:
                return None
            return {
                "id": int(row.id),
                "username": row.username,
                "role": role,
                "session_epoch": int(row.session_epoch),
                "session_generation": row.session_generation,
            }
    except Exception:
        # A database hiccup must not be an authentication bypass.
        structlog.get_logger("mail_ui").warning("mail_ui.session_lookup_failed", exc_info=True)
        return None


async def _resolve_mail_project(
    session: AsyncSession,
    identifier: str,
) -> tuple[Any, ...] | None:
    """Resolve a project with slug precedence and ambiguity rejection.

    Args:
        session: Open database session.
        identifier: Project slug or canonical human key from the URL.

    Returns:
        ``(id, slug, human_key, archived_at, project_generation)`` for one
        unambiguous project, or ``None`` when no project or multiple human-key
        matches exist.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id, slug, human_key, archived_at, project_generation FROM projects "
                "WHERE slug = :identifier OR human_key = :identifier ORDER BY id"
            ),
            {"identifier": identifier},
        )
    ).fetchall()
    slug_matches = [row for row in rows if str(row[1]) == identifier]
    if len(slug_matches) == 1:
        return tuple(slug_matches[0])
    human_key_matches = [row for row in rows if str(row[2]) == identifier]
    if len(human_key_matches) == 1:
        return tuple(human_key_matches[0])
    return None


def _mail_ui_request_user(request: Request) -> MailUiSessionPrincipal | None:
    """Return the authenticated human principal stored by the mail middleware.

    Args:
        request: Current HTTP request.

    Returns:
        The normalized principal, or ``None`` outside authenticated UI mode.
    """
    user = getattr(request.state, "mail_ui_user", None)
    return cast(MailUiSessionPrincipal, user) if isinstance(user, dict) else None


def _mail_ui_domain_http_exception(
    *,
    code: UiAccessMutationErrorCode | UiProfileMutationErrorCode,
    status_code: int,
) -> HTTPException:
    """Translate one domain refusal without leaking implementation details."""
    return HTTPException(status_code=status_code, detail={"code": code})


def _mail_ui_profile_http_exception(exc: UiProfileMutationError) -> HTTPException:
    """Map profile-domain refusals onto the stable typed HTTP surface."""
    code = exc.code
    if code == "invalid_display_name":
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    elif code in {"profile_revision_conflict", "compare_and_swap_failed"}:
        status_code = status.HTTP_409_CONFLICT
    elif code == "session_not_fresh":
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    else:
        status_code = status.HTTP_401_UNAUTHORIZED
    return _mail_ui_domain_http_exception(code=code, status_code=status_code)


def _mail_ui_access_http_exception(exc: UiAccessMutationError) -> HTTPException:
    """Map project-access refusals onto stable authorization/CAS statuses."""
    code = exc.code
    if code in {"actor_recreated", "actor_session_epoch_conflict"}:
        status_code = status.HTTP_401_UNAUTHORIZED
    elif code == "actor_forbidden":
        status_code = status.HTTP_403_FORBIDDEN
    elif code in {"target_not_found", "project_not_found"}:
        status_code = status.HTTP_404_NOT_FOUND
    elif code == "invalid_requested_role":
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    elif code in {"actor_contract_invalid", "invalid_existing_role", "session_not_fresh"}:
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    else:
        status_code = status.HTTP_409_CONFLICT
    return _mail_ui_domain_http_exception(code=code, status_code=status_code)


def _mail_ui_require_admin_principal(request: Request) -> MailUiSessionPrincipal:
    """Require a real current administrator session, including in development."""
    principal = _mail_ui_request_user(request)
    if principal is None:
        raise _mail_ui_domain_http_exception(
            code="actor_forbidden",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if principal["role"] != webauth.ROLE_ADMIN:
        raise _mail_ui_domain_http_exception(
            code="actor_forbidden",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return principal


def _mail_ui_profile_principal(request: Request) -> MailUiSessionPrincipal:
    """Require a real human principal through the typed profile error shape."""
    principal = _mail_ui_request_user(request)
    if principal is None:
        raise _mail_ui_domain_http_exception(
            code="actor_forbidden",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    return principal


async def _mail_ui_revalidated_profile_user(
    request: Request,
    session: AsyncSession,
) -> Any:
    """Revalidate all cookie claims before exposing typed profile state."""
    _mail_ui_profile_principal(request)
    try:
        return await _mail_ui_preferences_user(request, session)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            raise
        raise _mail_ui_domain_http_exception(
            code="session_epoch_conflict",
            status_code=status.HTTP_401_UNAUTHORIZED,
        ) from None


async def _mail_ui_revalidated_admin_user(
    request: Request,
    session: AsyncSession,
) -> Any:
    """Revalidate the middleware's admin claim inside the snapshot read session."""
    _mail_ui_require_admin_principal(request)
    try:
        row = await _mail_ui_preferences_user(request, session)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            raise
        raise _mail_ui_domain_http_exception(
            code="actor_session_epoch_conflict",
            status_code=status.HTTP_401_UNAUTHORIZED,
        ) from None

    role = webauth.normalize_ui_user_role(row.role)
    if role is None:
        raise _mail_ui_domain_http_exception(
            code="actor_contract_invalid",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    if role != webauth.ROLE_ADMIN:
        raise _mail_ui_domain_http_exception(
            code="actor_forbidden",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return row


def _mail_ui_preferences_response(
    *,
    preferred_ui_locale: MailUiLocale,
    preferred_correspondence_locale: MailUiLocale | None,
) -> MailUiPreferencesResponse:
    """Build the stable stored/effective language-preference response."""
    return MailUiPreferencesResponse(
        stored=MailUiStoredPreferences(
            preferred_ui_locale=preferred_ui_locale,
            preferred_correspondence_locale=preferred_correspondence_locale,
        ),
        effective=MailUiEffectivePreferences(
            ui_locale=preferred_ui_locale,
            correspondence_locale=preferred_correspondence_locale or preferred_ui_locale,
        ),
    )


def _mail_ui_correspondence_advisory(locale: MailUiLocale) -> str:
    """Return the server-authored advisory injected into HumanOverseer mail."""
    language = f"{MAIL_UI_LOCALE_ENGLISH_NAMES[locale]} ({locale.value})"
    return (
        "Advisory communication preference: the authenticated human operator "
        f"prefers replies in {language}. When practical, reply in that language. "
        "This preference does not override explicit message instructions or "
        "higher-priority policy."
    )


def _mail_ui_preferences_principal(request: Request) -> MailUiSessionPrincipal:
    """Require the authenticated internal human principal for a self route."""
    principal = _mail_ui_request_user(request)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User preferences require an authenticated Mail UI session.",
        )
    return principal


async def _mail_ui_preferences_user(
    request: Request,
    session: AsyncSession,
) -> Any:
    """Revalidate all cookie-bound identity facts before reading account state."""
    from sqlmodel import select

    from .models import UiUser

    principal = _mail_ui_preferences_principal(request)
    result = await session.execute(
        select(UiUser)
        .where(UiUser.id == principal["id"])
        .where(UiUser.username == principal["username"])
        .where(UiUser.session_epoch == principal["session_epoch"])
        .where(UiUser.session_generation == principal["session_generation"])
        .where(UiUser.disabled == False)  # noqa: E712
    )
    row = result.scalars().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated Mail UI session is no longer current.",
        )
    return row


async def _mail_ui_effective_correspondence_locale(
    *,
    settings: Settings,
    request: Request,
    session: AsyncSession,
) -> MailUiLocale:
    """Resolve correspondence language from a revalidated human account.

    Explicit auth-disabled development/test mode has no human principal. It uses
    the server's English default so legacy local workflows remain usable without
    ever accepting a locale supplied in the message payload.
    """
    if not settings.mail_ui.enabled:
        return MailUiLocale.EN
    principal = _mail_ui_preferences_principal(request)
    row = await _mail_ui_preferences_user(request, session)
    role = webauth.normalize_ui_user_role(row.role)
    if role is None or role != principal["role"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated Mail UI session is no longer current.",
        )
    return _mail_ui_locale_from_db(
        row.preferred_correspondence_locale or row.preferred_ui_locale,
    )


async def _mail_ui_preferences_cas_update(
    session: AsyncSession,
    *,
    principal: MailUiSessionPrincipal,
    values: dict[str, Any],
) -> bool:
    """Update the same account lifetime only, even if its primary key was reused."""
    from .models import UiUser

    result = await session.execute(
        update(UiUser)
        .where(cast(Any, UiUser.id == principal["id"]))
        .where(cast(Any, UiUser.username == principal["username"]))
        .where(cast(Any, UiUser.session_epoch == principal["session_epoch"]))
        .where(cast(Any, UiUser.session_generation == principal["session_generation"]))
        .where(cast(Any, UiUser.disabled == False))  # noqa: E712
        .values(**values)
    )
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        await session.rollback()
        return False
    await session.commit()
    return True


def _mail_ui_password_principal(request: Request) -> MailUiSessionPrincipal:
    """Require the authenticated internal human principal for password rotation."""
    principal = _mail_ui_request_user(request)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Password changes require an authenticated Mail UI session.",
        )
    return principal


async def _mail_ui_password_user(
    request: Request,
    session: AsyncSession,
) -> Any:
    """Revalidate every cookie-bound fact before reading a password hash."""
    from sqlmodel import select

    from .models import UiUser

    principal = _mail_ui_password_principal(request)
    result = await session.execute(
        select(UiUser)
        .where(UiUser.id == principal["id"])
        .where(UiUser.username == principal["username"])
        .where(UiUser.session_epoch == principal["session_epoch"])
        .where(UiUser.session_generation == principal["session_generation"])
        .where(UiUser.disabled == False)  # noqa: E712
    )
    row = result.scalars().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated Mail UI session is no longer current.",
        )
    return row


async def _mail_ui_password_cas_update(
    session: AsyncSession,
    *,
    principal: MailUiSessionPrincipal,
    old_password_hash: str,
    new_password_hash: str,
) -> bool:
    """Rotate one current account lifetime and revoke every previous cookie."""
    from .models import UiUser

    result = await session.execute(
        update(UiUser)
        .where(cast(Any, UiUser.id == principal["id"]))
        .where(cast(Any, UiUser.username == principal["username"]))
        .where(cast(Any, UiUser.session_epoch == principal["session_epoch"]))
        .where(cast(Any, UiUser.session_generation == principal["session_generation"]))
        .where(cast(Any, UiUser.password_hash == old_password_hash))
        .where(cast(Any, UiUser.disabled == False))  # noqa: E712
        .values(
            password_hash=new_password_hash,
            session_epoch=principal["session_epoch"] + 1,
        )
    )
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        await session.rollback()
        return False
    await session.commit()
    return True


def _set_mail_ui_session_cookie(
    response: Response,
    *,
    token: str,
    settings: Settings,
) -> None:
    """Attach the one canonical Mail UI session cookie to a response."""
    cfg = settings.mail_ui
    response.set_cookie(
        cfg.cookie_name,
        token,
        max_age=cfg.session_ttl_seconds,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="lax",
        path="/mail",
    )


def _mail_ui_request_is_admin(*, settings: Settings, request: Request) -> bool:
    """Return whether the request has global administrator authority.

    Args:
        settings: Active application settings.
        request: Current HTTP request.

    Returns:
        ``True`` for an administrator or explicit auth-disabled development mode.
    """
    user = _mail_ui_request_user(request)
    return not settings.mail_ui.enabled or bool(user and user["role"] == webauth.ROLE_ADMIN)


def _mail_ui_access_context(
    *,
    settings: Settings,
    request: Request,
    project_id: int,
    project_role: str | None,
) -> dict[str, Any]:
    """Build template permissions for one project.

    Args:
        settings: Active application settings.
        request: Current HTTP request.
        project_id: Database identifier of the project.
        project_role: Normalized assignment role for a member.

    Returns:
        A stable permission mapping consumed by mail templates.
    """
    is_admin = _mail_ui_request_is_admin(settings=settings, request=request)
    can_read = is_admin or webauth.project_role_allows_view(project_role)
    can_reply = is_admin or webauth.project_role_allows_operate(project_role)
    return {
        "project_id": project_id,
        "project_role": project_role,
        "can_read": can_read,
        "can_reply": can_reply,
        "can_compose": is_admin,
        "can_mutate": is_admin,
        "is_admin": is_admin,
    }


async def _mail_ui_visible_project_roles(
    *,
    settings: Settings,
    request: Request,
    session: AsyncSession,
) -> dict[int, str | None]:
    """Return visible project ids and normalized assignment roles.

    Args:
        settings: Active application settings.
        request: Current HTTP request.
        session: Open database session.

    Returns:
        Mapping from visible project id to member role. Administrators and the
        explicit auth-disabled development mode receive every project with a
        ``None`` assignment role.
    """
    user = _mail_ui_request_user(request)
    if not settings.mail_ui.enabled or bool(user and user["role"] == webauth.ROLE_ADMIN):
        rows = await session.execute(text("SELECT id FROM projects"))
        return {int(row[0]): None for row in rows.fetchall()}
    if user is None or user["role"] != webauth.ROLE_MEMBER:
        return {}
    rows = await session.execute(
        text(
            "SELECT project_id, role FROM ui_project_assignments "
            "WHERE user_id = :user_id"
        ),
        {"user_id": int(user["id"])},
    )
    visible: dict[int, str | None] = {}
    for project_id, raw_role in rows.fetchall():
        role = webauth.normalize_project_role(raw_role)
        if role is not None and webauth.project_role_allows_view(role):
            visible[int(project_id)] = role
    return visible


def _mail_ui_legacy_search_hash(
    *,
    bookmark: _MailUiLegacyBookmark,
    project_id: int,
    query_items: list[tuple[str, str]],
) -> str | None:
    """Map one legacy project/search query to the exact typed React route."""
    kind = bookmark["kind"]
    if kind not in {"project", "search"}:
        return None if query_items else ""
    if not query_items:
        return (
            f"#inbox?{urlencode({'project': project_id})}"
            if kind == "project"
            else None
        )
    values: dict[str, str] = {}
    for key, value in query_items:
        if key not in {"q", "scope", "order"} or key in values:
            return None
        values[key] = value
    raw_query = values.get("q", "").strip()
    if not raw_query:
        return None
    raw_scope = values.get("scope", "all")
    scope_mapping: dict[str, MailUiSearchScope] = {
        "all": "all",
        "both": "all",
        "subject": "subject",
        "body": "body",
    }
    scope = scope_mapping.get(raw_scope)
    raw_order = values.get("order", "relevance")
    order_mapping: dict[str, MailUiSearchOrder] = {
        "relevance": "relevance",
        "newest": "newest",
        "time": "newest",
    }
    order = order_mapping.get(raw_order)
    if scope is None or order is None:
        return None
    try:
        _mail_ui_compile_search_query(raw_query, scope)
    except HTTPException:
        return None
    return "#search?" + urlencode(
        {
            "q": raw_query,
            "project": project_id,
            "scope": scope,
            "order": order,
        }
    )


async def _mail_ui_legacy_bookmark_redirect(
    *,
    settings: Settings,
    request: Request,
    bookmark: _MailUiLegacyBookmark,
) -> Response:
    """Redirect a verified upstream bookmark without reviving its old handler."""
    kind = bookmark["kind"]
    query_items = list(request.query_params.multi_items())
    if kind in {"projects", "inbox"}:
        if query_items:
            return JSONResponse(
                {"detail": "Not Found"},
                status_code=status.HTTP_404_NOT_FOUND,
                headers=_MAIL_REACT_INDEX_HEADERS,
            )
        fragment = "#projects" if kind == "projects" else "#inbox"
    else:
        project_slug = bookmark["project_slug"]
        if project_slug is None:
            return JSONResponse(
                {"detail": "Not Found"},
                status_code=status.HTTP_404_NOT_FOUND,
                headers=_MAIL_REACT_INDEX_HEADERS,
            )
        try:
            await ensure_schema()
            async with get_session() as session:
                visible_roles = await _mail_ui_visible_project_roles(
                    settings=settings,
                    request=request,
                    session=session,
                )
                project_row = (
                    await session.execute(
                        text("SELECT id FROM projects WHERE slug = :slug"),
                        {"slug": project_slug},
                    )
                ).first()
                if project_row is None:
                    raise LookupError("project not found")
                project_id = int(project_row[0])
                if project_id not in visible_roles:
                    raise LookupError("project not visible")
                if kind == "message":
                    if query_items:
                        raise LookupError("message bookmark has unsupported query")
                    message_id = bookmark["message_id"]
                    if message_id is None:
                        raise LookupError("message id missing")
                    message_exists = (
                        await session.execute(
                            text(
                                "SELECT 1 FROM messages "
                                "WHERE project_id = :project_id AND id = :message_id"
                            ),
                            {
                                "project_id": project_id,
                                "message_id": message_id,
                            },
                        )
                    ).first()
                    if message_exists is None:
                        raise LookupError("message not found")
                    fragment = f"#message/{project_id}/{message_id}"
                elif kind == "thread":
                    if query_items:
                        raise LookupError("thread bookmark has unsupported query")
                    thread_id = bookmark["thread_id"]
                    if thread_id is None:
                        raise LookupError("thread id missing")
                    starter_message_id = _mail_ui_thread_starter_message_id(thread_id)
                    thread_exists = (
                        await session.execute(
                            text(
                                "SELECT 1 FROM messages m "
                                "WHERE m.project_id = :project_id "
                                "AND (m.thread_id = :thread_id "
                                "OR (:starter_message_id IS NOT NULL "
                                "AND m.id = :starter_message_id)) "
                                "LIMIT 1"
                            ),
                            {
                                "project_id": project_id,
                                "thread_id": thread_id,
                                "starter_message_id": starter_message_id,
                            },
                        )
                    ).first()
                    if thread_exists is None:
                        raise LookupError("thread not found")
                    fragment = f"#thread/{project_id}/{_mail_ui_encode_thread_id(thread_id)}"
                else:
                    search_hash = _mail_ui_legacy_search_hash(
                        bookmark=bookmark,
                        project_id=project_id,
                        query_items=query_items,
                    )
                    if search_hash is None:
                        raise LookupError("unsupported legacy search query")
                    fragment = search_hash
        except Exception:
            return JSONResponse(
                {"detail": "Not Found"},
                status_code=status.HTTP_404_NOT_FOUND,
                headers=_MAIL_REACT_INDEX_HEADERS,
            )
    return Response(
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={
            **_MAIL_REACT_INDEX_HEADERS,
            "Location": f"{_MAIL_REACT_BASE_PATH}{fragment}",
        },
    )


async def _mail_ui_require_project_access(
    *,
    settings: Settings,
    request: Request,
    session: AsyncSession,
    project_id: int,
    operate: bool = False,
) -> dict[str, Any]:
    """Require read or operator access to one project.

    Args:
        settings: Active application settings.
        request: Current HTTP request.
        session: Open database session.
        project_id: Database identifier of the project.
        operate: Require operator-level reply permission when true.

    Returns:
        Template permission mapping for the authorized project.

    Raises:
        HTTPException: With 404 for an invisible project or 403 when a visible
            assignment lacks operator permission.
    """
    visible = await _mail_ui_visible_project_roles(
        settings=settings,
        request=request,
        session=session,
    )
    if project_id not in visible:
        raise HTTPException(status_code=404, detail="Project not found")
    access = _mail_ui_access_context(
        settings=settings,
        request=request,
        project_id=project_id,
        project_role=visible[project_id],
    )
    if operate and not access["can_reply"]:
        raise HTTPException(
            status_code=403,
            detail="Forbidden: this action requires the project operator role",
        )
    return access


def _mail_ui_visible_project_predicate(
    visible_project_ids: list[int],
    *,
    column: str,
    parameter_prefix: str,
) -> tuple[str, dict[str, int]]:
    """Build one bound ``IN`` predicate for an already-authorized project set."""
    if not visible_project_ids:
        return "1 = 0", {}
    parameters = {
        f"{parameter_prefix}_{index}": project_id
        for index, project_id in enumerate(visible_project_ids)
    }
    placeholders = ", ".join(f":{name}" for name in parameters)
    return f"{column} IN ({placeholders})", parameters


def _mail_ui_message_summary_from_row(
    row: Any,
    *,
    settings: Settings,
    request: Request,
    visible_roles: dict[int, str | None],
) -> MailUiMessageSummary:
    """Render a database row through the deliberately lean inbox contract."""
    project_id = int(row["project_id"])
    sender_name = str(row["sender_name"] or "Unknown")
    sender_project_id = (
        int(row["sender_project_id"])
        if row["sender_project_id"] is not None
        else None
    )
    sender_project_slug = (
        str(row["sender_project_slug"])
        if row["sender_project_slug"] is not None
        else None
    )
    sender = _sender_display_name(
        message_project_id=project_id,
        sender_name=sender_name,
        sender_project_id=sender_project_id,
        sender_project_slug=sender_project_slug,
    )
    access = _mail_ui_access_context(
        settings=settings,
        request=request,
        project_id=project_id,
        project_role=visible_roles[project_id],
    )
    return MailUiMessageSummary(
        id=int(row["id"]),
        project_id=project_id,
        project_slug=str(row["project_slug"]),
        subject=str(row["subject"] or "(No subject)"),
        sender=sender,
        sender_name=sender_name,
        sender_display_name=(
            str(row["sender_display_name"])
            if row["sender_display_name"] is not None
            else None
        ),
        importance=_mail_ui_importance(row["importance"]),
        ack_required=bool(row["ack_required"]),
        thread_id=str(row["thread_id"]) if row["thread_id"] is not None else None,
        reply_to=int(row["reply_to"]) if row["reply_to"] is not None else None,
        created_ts=_mail_ui_datetime(row["created_ts"]),
        can_reply=bool(access["can_reply"]),
    )


async def _mail_ui_safe_recipient_map(
    session: AsyncSession,
    message_ids: list[int],
) -> dict[int, dict[str, list[str]]]:
    """Return only TO/CC names for a bounded set of messages.

    BCC rows are removed by the SQL predicate, before Python sees any recipient
    name.  This keeps a future serializer change from turning blind recipients
    into ordinary response data.
    """
    result = {message_id: {"to": [], "cc": []} for message_id in message_ids}
    if not message_ids:
        return result
    parameters = {
        f"recipient_mid_{index}": message_id
        for index, message_id in enumerate(message_ids)
    }
    placeholders = ", ".join(f":{name}" for name in parameters)
    rows = await session.execute(
        text(
            "SELECT mr.message_id, mr.kind, a.name "
            "FROM message_recipients mr "
            "JOIN agents a ON a.id = mr.agent_id "
            f"WHERE mr.message_id IN ({placeholders}) AND mr.kind IN ('to', 'cc') "
            "ORDER BY mr.message_id, CASE mr.kind WHEN 'to' THEN 0 ELSE 1 END, a.name"
        ),
        parameters,
    )
    for message_id, kind, name in rows.fetchall():
        normalized_kind = str(kind)
        target = result.get(int(message_id))
        if target is not None and normalized_kind in {"to", "cc"}:
            target[normalized_kind].append(str(name))
    return result


def _mail_ui_message_detail_from_row(
    row: Any,
    *,
    recipients: dict[str, list[str]],
    settings: Settings,
    request: Request,
    visible_roles: dict[int, str | None],
) -> MailUiMessageDetail:
    """Render one full message while retaining the safe summary projection."""
    summary = _mail_ui_message_summary_from_row(
        row,
        settings=settings,
        request=request,
        visible_roles=visible_roles,
    )
    reply_target = (
        MailUiReplyTarget(
            agent_id=int(row["reply_target_agent_id"]),
            agent_generation=str(row["reply_target_agent_generation"]),
            project_id=int(row["reply_target_project_id"]),
            project_generation=str(row["reply_target_project_generation"]),
            canonical_name=_sender_display_name(
                message_project_id=summary.project_id,
                sender_name=str(row["reply_target_agent_name"]),
                sender_project_id=int(row["reply_target_project_id"]),
                sender_project_slug=str(row["reply_target_project_slug"]),
            ),
        )
        if row["reply_target_agent_id"] is not None
        and str(row["reply_target_agent_name"]) != "HumanOverseer"
        else None
    )
    summary = summary.model_copy(
        update={
            "can_reply": (
                summary.can_reply
                and reply_target is not None
                and bool(row["reply_target_available"])
            )
        }
    )
    return MailUiMessageDetail(
        **summary.model_dump(),
        body_md=str(row["body_md"] or ""),
        to=list(recipients["to"]),
        cc=list(recipients["cc"]),
        attachments=_mail_ui_safe_attachments(row["attachments"]),
        reply_target=reply_target,
    )


def _mail_ui_require_admin_read(*, settings: Settings, request: Request) -> None:
    """Require an administrator for a sensitive read-only endpoint.

    Args:
        settings: Active application settings.
        request: Current HTTP request.

    Raises:
        HTTPException: When an authenticated member attempts the operation.
    """
    if not settings.mail_ui.enabled:
        return
    user = _mail_ui_request_user(request)
    if user is None or user["role"] != webauth.ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Forbidden: this action requires the admin role")


async def _mail_ui_stream_access_valid(
    *,
    settings: Settings,
    session_token: str,
    project_slug: str | None,
    expected_principal: MailUiSessionPrincipal | None,
    expected_project_id: int | None = None,
    expected_project_generation: str | None = None,
) -> bool:
    """Revalidate one exact account and project lifetime for an open stream.

    Args:
        settings: Active application settings.
        session_token: Signed browser session cookie captured at connection time.
        project_slug: Specific project slug, or ``None`` for any visible project.
        expected_principal: Account-lifetime snapshot captured before subscribing.
        expected_project_id: Project row id captured before subscribing.
        expected_project_generation: Immutable project-lifetime generation.

    Returns:
        ``True`` only while the exact account lifetime remains live and its
        assignment still covers the exact project lifetime. A deleted and
        recreated username or project can therefore never inherit an already
        open stream merely by reusing the same human-readable name.
    """
    if settings.mail_ui.enabled:
        user = await _load_session_user(session_token, settings=settings)
        if user is None or expected_principal is None:
            return False
        if (
            user["id"] != expected_principal["id"]
            or user["username"] != expected_principal["username"]
            or user["role"] != expected_principal["role"]
            or user["session_epoch"] != expected_principal["session_epoch"]
            or not hmac.compare_digest(
                user["session_generation"],
                expected_principal["session_generation"],
            )
        ):
            return False
    else:
        user = expected_principal

    if project_slug is None:
        return True
    if expected_project_id is None or expected_project_generation is None:
        return False

    async with get_session() as session:
        project = (
            await session.execute(
                text(
                    "SELECT id, project_generation FROM projects "
                    "WHERE id = :project_id AND slug = :project_slug"
                ),
                {
                    "project_id": expected_project_id,
                    "project_slug": project_slug,
                },
            )
        ).fetchone()
        if (
            project is None
            or int(project[0]) != expected_project_id
            or not hmac.compare_digest(
                str(project[1]),
                expected_project_generation,
            )
        ):
            return False
        if not settings.mail_ui.enabled or bool(
            user and user["role"] == webauth.ROLE_ADMIN
        ):
            return True
        if user is None:
            return False
        rows = await session.execute(
            text(
                "SELECT role FROM ui_project_assignments "
                "WHERE user_id = :user_id AND project_id = :project_id"
            ),
            {
                "user_id": int(user["id"]),
                "project_id": expected_project_id,
            },
        )
        return any(
            webauth.project_role_allows_view(webauth.normalize_project_role(row[0]))
            for row in rows.fetchall()
        )


async def _mail_ui_stream_project_lifetimes(
    *,
    settings: Settings,
    request: Request,
) -> dict[str, tuple[int, str]]:
    """Snapshot every project lifetime visible when a wildcard stream opens."""
    async with get_session() as session:
        visible = await _mail_ui_visible_project_roles(
            settings=settings,
            request=request,
            session=session,
        )
        predicate, params = _mail_ui_visible_project_predicate(
            list(visible),
            column="id",
            parameter_prefix="stream_project",
        )
        rows = await session.execute(
            text(
                "SELECT id, slug, project_generation FROM projects "
                f"WHERE {predicate}"
            ),
            params,
        )
    return {
        str(row[1]): (int(row[0]), str(row[2]))
        for row in rows.fetchall()
    }


async def _agent_stream_lifetime_valid(
    *,
    project_id: int,
    project_slug: str,
    project_generation: str,
    agent_id: int,
    agent_name: str,
    agent_generation: str,
    registration_token: str,
) -> bool:
    """Fail closed unless an agent stream still names the exact DB lifetime."""
    try:
        async with get_session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT p.project_generation, p.archived_at, a.name, "
                        "a.agent_generation, a.registration_token, a.retired_at "
                        "FROM projects p JOIN agents a ON a.project_id = p.id "
                        "WHERE p.id = :project_id AND p.slug = :project_slug "
                        "AND a.id = :agent_id"
                    ),
                    {
                        "project_id": project_id,
                        "project_slug": project_slug,
                        "agent_id": agent_id,
                    },
                )
            ).fetchone()
        return bool(
            row is not None
            and row[1] is None
            and row[5] is None
            and str(row[2]) == agent_name
            and hmac.compare_digest(str(row[0]), project_generation)
            and hmac.compare_digest(str(row[3]), agent_generation)
            and bool(row[4])
            and hmac.compare_digest(str(row[4]), registration_token)
        )
    except Exception:
        # A failed lifetime lookup must close the channel, never preserve it.
        return False


class SecurityAndRateLimitMiddleware(BaseHTTPMiddleware):
    """JWT auth (optional), RBAC, and token-bucket rate limiting.

    - If JWT is enabled, validates Authorization: Bearer <token> using either HMAC secret or JWKS URL.
    - Enforces basic RBAC when enabled: read-only roles may only call whitelisted tools and resource reads.
    - Applies per-endpoint token-bucket limits (tools vs resources) with in-memory or Redis backend.
    """

    def __init__(self, app: FastAPI, settings: Settings):
        super().__init__(app)
        self.settings = settings
        self._jwt_enabled = bool(getattr(settings.http, "jwt_enabled", False))
        self._rbac_enabled = bool(getattr(settings.http, "rbac_enabled", True))
        self._reader_roles = set(getattr(settings.http, "rbac_reader_roles", []) or [])
        self._writer_roles = set(getattr(settings.http, "rbac_writer_roles", []) or [])
        self._readonly_tools = set(getattr(settings.http, "rbac_readonly_tools", []) or [])
        self._default_role = getattr(settings.http, "rbac_default_role", "tools")
        # Token bucket state (memory)
        from time import monotonic

        self._monotonic = monotonic
        self._buckets: dict[str, tuple[float, float]] = {}
        self._last_cleanup = monotonic()
        # Redis client (optional)
        self._redis = None
        if getattr(settings.http, "rate_limit_backend", "memory") == "redis" and getattr(
            settings.http, "rate_limit_redis_url", ""
        ):
            try:
                redis_asyncio = importlib.import_module("redis.asyncio")
                Redis = redis_asyncio.Redis
                self._redis = Redis.from_url(settings.http.rate_limit_redis_url)
            except Exception:
                self._redis = None

    def _cleanup_buckets(self, now: float) -> None:
        """Remove stale buckets to prevent memory leaks."""
        # Evict buckets not accessed in the last hour
        expiration = 3600.0
        cutoff = now - expiration
        # Create list of keys to remove to avoid runtime modification errors during iteration
        to_remove = [k for k, (_, ts) in self._buckets.items() if ts < cutoff]
        for k in to_remove:
            self._buckets.pop(k, None)

    async def _decode_jwt(self, token: str) -> dict | None:
        """Validate and decode JWT, returning claims or None on failure."""
        with contextlib.suppress(Exception):
            jose_mod = importlib.import_module("authlib.jose")
            JsonWebKey = jose_mod.JsonWebKey
            JsonWebToken = jose_mod.JsonWebToken
            algs = list(getattr(self.settings.http, "jwt_algorithms", ["HS256"]))
            jwt = JsonWebToken(algs)
            audience = getattr(self.settings.http, "jwt_audience", None) or None
            issuer = getattr(self.settings.http, "jwt_issuer", None) or None
            jwks_url = getattr(self.settings.http, "jwt_jwks_url", None) or None
            secret = getattr(self.settings.http, "jwt_secret", None) or None

            header = _decode_jwt_header_segment(token)
            if header is None:
                return None
            key = None
            candidate_keys: list = []
            if jwks_url:
                with contextlib.suppress(Exception):
                    key_set = await _fetch_jwks(jwks_url)
                    if key_set is None:
                        return None
                    if header.get("kid"):
                        key = _select_jwks_key(key_set, header, algs)
                        # Unknown kid: the cached JWKS may be stale; force a
                        # refresh once before giving up (#212).
                        if key is None:
                            key_set = await _fetch_jwks(jwks_url, force=True)
                            if key_set is not None:
                                key = _select_jwks_key(key_set, header, algs)
                    else:
                        # No kid: never blind-pick keys[0]. Try every
                        # algorithm-compatible key during verification (#211).
                        candidate_keys = _jwks_candidate_keys(key_set, header, algs)
            elif secret:
                with contextlib.suppress(Exception):
                    key = JsonWebKey.import_key(secret, {"kty": "oct"})
            keys_to_try = candidate_keys if candidate_keys else ([key] if key is not None else [])
            if not keys_to_try:
                return None
            for candidate in keys_to_try:
                with contextlib.suppress(Exception):
                    claims = jwt.decode(token, candidate)
                    if audience:
                        claims.validate_aud(audience)
                    if issuer and str(claims.get("iss") or "") != issuer:
                        continue
                    claims.validate()
                    return dict(claims)
        return None

    @staticmethod
    def _classify_request(path: str, method: str, body_bytes: bytes) -> tuple[str, str | None]:
        """Return (kind, tool_name) where kind is 'tools'|'resources'|'other'."""
        if method.upper() != "POST":
            return "other", None
        if not body_bytes:
            return "other", None
        with contextlib.suppress(Exception):
            import json as _json

            payload = _json.loads(body_bytes)
            rpc_method = str(payload.get("method", ""))
            if rpc_method == "tools/call":
                params = payload.get("params", {}) or {}
                tool_name = params.get("name")
                return "tools", tool_name if isinstance(tool_name, str) else None
            if rpc_method.startswith("resources/"):
                return "resources", None
            return "other", None
        return "other", None

    @staticmethod
    def _coerce_rpm(value: object, default: int) -> int:
        # An explicit 0 disables the limit and must survive (#213); only a
        # missing/None value falls back to the default. Use a None check rather
        # than ``value or default`` (which would turn 0 into ``default``).
        if value is None:
            return default
        # cast, not isinstance: `int()` accepts str, bytes, numbers and anything
        # implementing `__int__`/`__index__`, which the `object` annotation
        # cannot express. An isinstance allowlist here would narrow the
        # behaviour, not just the type — a value that converts fine today would
        # start silently falling back to the default. The suppress below already
        # covers everything that genuinely cannot convert.
        with contextlib.suppress(Exception):
            return int(cast(Any, value))
        return default

    def _rate_limits_for(self, kind: str) -> tuple[int, int]:
        # return (per_minute, burst)
        if kind == "tools":
            rpm = self._coerce_rpm(getattr(self.settings.http, "rate_limit_tools_per_minute", 60), 60)
            burst = int(getattr(self.settings.http, "rate_limit_tools_burst", 0) or 0)
        elif kind == "resources":
            rpm = self._coerce_rpm(getattr(self.settings.http, "rate_limit_resources_per_minute", 120), 120)
            burst = int(getattr(self.settings.http, "rate_limit_resources_burst", 0) or 0)
        else:
            rpm = self._coerce_rpm(getattr(self.settings.http, "rate_limit_per_minute", 60), 60)
            burst = 0
        # rpm <= 0 means "disabled" (handled by _consume_bucket); don't synthesize
        # a positive burst that would re-enable limiting.
        burst = int(burst) if burst > 0 else max(1, rpm)
        return rpm, burst

    async def _consume_bucket(self, key: str, per_minute: int, burst: int) -> bool:
        """Return True if token granted, False if limited."""
        if per_minute <= 0:
            return True
        rate_per_sec = per_minute / 60.0
        now = self._monotonic()

        # Redis backend
        if self._redis is not None:
            try:
                lua = (
                    "local key = KEYS[1]\n"
                    "local now = tonumber(ARGV[1])\n"
                    "local rate = tonumber(ARGV[2])\n"
                    "local burst = tonumber(ARGV[3])\n"
                    "local state = redis.call('HMGET', key, 'tokens', 'ts')\n"
                    "local tokens = tonumber(state[1]) or burst\n"
                    "local ts = tonumber(state[2]) or now\n"
                    "local delta = now - ts\n"
                    "tokens = math.min(burst, tokens + delta * rate)\n"
                    "local allowed = 0\n"
                    "if tokens >= 1 then\n"
                    "  tokens = tokens - 1\n"
                    "  allowed = 1\n"
                    "end\n"
                    "redis.call('HMSET', key, 'tokens', tokens, 'ts', now)\n"
                    "redis.call('EXPIRE', key, math.ceil(burst / math.max(rate, 0.001)))\n"
                    "return allowed\n"
                )
                allowed = await self._redis.eval(lua, 1, f"rl:{key}", now, rate_per_sec, burst)
                return bool(int(allowed or 0) == 1)
            except Exception:
                # Fallback to memory on Redis failure
                pass

        # In-memory token bucket
        tokens, ts = self._buckets.get(key, (float(burst), now))
        elapsed = max(0.0, now - ts)
        tokens = min(float(burst), tokens + elapsed * rate_per_sec)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        tokens -= 1.0
        self._buckets[key] = (tokens, now)
        return True

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        # Perform periodic cleanup of in-memory rate limit buckets
        if self._redis is None:
            now = self._monotonic()
            if now - self._last_cleanup > 60.0:
                self._cleanup_buckets(now)
                self._last_cleanup = now

        # Allow CORS preflight and health endpoints
        if request.method == "OPTIONS" or request.url.path.startswith("/health/") or request.url.path == "/api/health":
            return await call_next(request)

        # Only read/patch body for POST requests. GET (including SSE) must not receive http.request messages.
        body_bytes = b""
        if request.method.upper() == "POST":
            try:
                body_bytes = await request.body()
                body_sent = False

                async def _receive() -> dict:
                    nonlocal body_sent
                    if body_sent:
                        return {"type": "http.request", "body": b"", "more_body": False}
                    body_sent = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}

                cast(Any, request)._receive = _receive
            except Exception:
                body_bytes = b""

        kind, tool_name = self._classify_request(request.url.path, request.method, body_bytes)

        # JWT auth (if enabled)
        if getattr(request.state, "mail_ui_authenticated", False):
            roles = {self._default_role}
        elif self._jwt_enabled:
            auth_header = request.headers.get("Authorization", "")
            # #210: when JWT is enabled, a valid *static* bearer is still accepted
            # as the OR-alternative to a JWT (the outer BearerAuthMiddleware defers
            # Bearer requests here without distinguishing the two). Check it first so
            # static-bearer clients keep working once JWT is turned on; a static
            # bearer is treated exactly as it is when JWT is disabled (default role).
            static_token = getattr(self.settings.http, "bearer_token", "") or ""
            if static_token and hmac.compare_digest(auth_header, f"Bearer {static_token}"):
                roles = {self._default_role}
            else:
                if not auth_header.startswith("Bearer "):
                    return JSONResponse({"detail": "Unauthorized"}, status_code=status.HTTP_401_UNAUTHORIZED)
                token = auth_header.split(" ", 1)[1].strip()
                claims_dict = await self._decode_jwt(token)
                if claims_dict is None:
                    return JSONResponse({"detail": "Unauthorized"}, status_code=status.HTTP_401_UNAUTHORIZED)
                claims = cast(dict[str, Any], claims_dict)
                request.state.jwt_claims = claims
                roles_raw = claims.get(self.settings.http.jwt_role_claim, [])
                if isinstance(roles_raw, str):
                    roles = {roles_raw}
                elif isinstance(roles_raw, (list, tuple)):
                    roles = {str(r) for r in roles_raw}
                else:
                    roles = set()
                if not roles:
                    roles = {self._default_role}
        else:
            roles = {self._default_role}
            # Elevate localhost to writer when unauthenticated localhost is allowed
            if _localhost_bypass_allowed(
                request,
                allow_localhost=bool(getattr(self.settings.http, "allow_localhost_unauthenticated", False)),
            ):
                roles.add("writer")

        # RBAC enforcement (skip for localhost when allowed)
        is_local_ok = _localhost_bypass_allowed(
            request,
            allow_localhost=bool(getattr(self.settings.http, "allow_localhost_unauthenticated", False)),
        )
        if self._rbac_enabled and not is_local_ok and kind in {"tools", "resources"}:
            is_reader = bool(roles & self._reader_roles)
            is_writer = bool(roles & self._writer_roles) or (not roles)
            if kind == "resources":
                pass  # readers allowed
            elif kind == "tools":
                if not tool_name:
                    # Without name, assume write-required to be safe
                    if not is_writer:
                        return JSONResponse({"detail": "Forbidden"}, status_code=status.HTTP_403_FORBIDDEN)
                else:
                    if tool_name in self._readonly_tools:
                        if not is_reader and not is_writer:
                            return JSONResponse({"detail": "Forbidden"}, status_code=status.HTTP_403_FORBIDDEN)
                    else:
                        if not is_writer:
                            return JSONResponse({"detail": "Forbidden"}, status_code=status.HTTP_403_FORBIDDEN)

        # Rate limiting
        if self.settings.http.rate_limit_enabled:
            rpm, burst = self._rate_limits_for(kind)
            identity = request.client.host if request.client else "ip-unknown"
            # Prefer stable subject from JWT if present
            with contextlib.suppress(Exception):
                maybe_claims = getattr(request.state, "jwt_claims", None)
                if isinstance(maybe_claims, dict):
                    sub = maybe_claims.get("sub")
                    if isinstance(sub, str) and sub:
                        identity = f"sub:{sub}"
            endpoint = tool_name or "*"
            key = f"{kind}:{endpoint}:{identity}"
            allowed = await self._consume_bucket(key, rpm, burst)
            if not allowed:
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=status.HTTP_429_TOO_MANY_REQUESTS)

        return await call_next(request)


async def readiness_check() -> None:
    await ensure_schema()
    async with get_session() as session:
        await session.execute(text("SELECT 1"))

    # Fail readiness if FD usage from lockfile leaks is critically high.
    # This gives orchestrators a signal to restart the process before it
    # becomes completely wedged (issue #116).
    current, limit = get_fd_usage()
    if current >= 0 and limit > 0:
        headroom_pct = (limit - current) / limit
        if headroom_pct < 0.10:
            lock_stats = get_lock_telemetry()
            raise RuntimeError(
                f"FD exhaustion imminent: {current}/{limit} FDs in use "
                f"({round(headroom_pct * 100, 1)}% headroom). "
                f"Lock telemetry: {lock_stats}"
            )


def create_app() -> FastAPI:
    """Zero-argument ASGI app factory for ``uvicorn ... --factory`` (#214).

    ``build_http_app`` requires a ``Settings`` argument, so it cannot be used
    directly as a uvicorn ``--factory`` target. This wrapper resolves settings
    from the environment and builds the app, matching the documented command.
    """
    return build_http_app(get_settings())


def build_http_app(settings: Settings, server=None) -> FastAPI:
    # Configure logging once
    _configure_logging(settings)
    if server is None:
        server = build_mcp_server()

    # Build MCP HTTP sub-app with stateless mode for ASGI test transports
    mcp_http_app = cast(_FastMCPHttpApp, server).http_app(
        path="/",
        stateless_http=True,
        json_response=True,
    )

    # Second, STATEFUL MCP sub-app (issue #250): stateless mode creates a new
    # transport per request and never issues an ``Mcp-Session-Id`` header, so
    # session-bound agent authentication (#148) could never persist across
    # HTTP tool calls. Provisioning returns a credential once, while ordinary
    # resume calls rely on that credential or a stateful session binding.
    # A bare flip to ``stateless_http=False`` would break handshake-skipping
    # clients (e.g. ntm's HTTP client), so we mount BOTH: the stateful app at
    # '/mcp' for spec-compliant MCP clients that keep a session, and the
    # stateless app at '/api' (and the configured base) for one-shot clients.
    mcp_stateful_http_app = cast(_FastMCPHttpApp, server).http_app(
        path="/",
        stateless_http=False,
        json_response=True,
    )

    # no-op wrapper removed; using explicit stateless adapter below

    # Background workers lifecycle
    async def _startup() -> None:  # pragma: no cover - service lifecycle
        # Note: no early return here -- the FD health monitor always runs,
        # even when optional workers are disabled by feature flags.

        async def _worker_cleanup() -> None:
            while True:
                try:
                    await ensure_schema()
                    async with get_session() as session:
                        rows = await session.execute(text("SELECT DISTINCT project_id FROM file_reservations"))
                        pids = [r[0] for r in rows.fetchall() if r[0] is not None]
                    released_total = 0
                    for pid in pids:
                        with contextlib.suppress(Exception):
                            stale = await _expire_stale_file_reservations(pid)
                            released_total += len(stale)
                    try:
                        rich_console = importlib.import_module("rich.console")
                        rich_panel = importlib.import_module("rich.panel")
                        Console = rich_console.Console
                        Panel = rich_panel.Panel
                        Console().print(
                            Panel.fit(
                                f"projects_scanned={len(pids)} released={released_total}",
                                title="File Reservations Cleanup",
                                border_style="cyan",
                            )
                        )
                    except Exception:
                        pass
                    with contextlib.suppress(Exception):
                        structlog.get_logger("tasks").info(
                            "file_reservations_cleanup",
                            projects_scanned=len(pids),
                            stale_released=released_total,
                        )
                except Exception:
                    pass
                await asyncio.sleep(settings.file_reservations_cleanup_interval_seconds)

        async def _worker_ack_ttl() -> None:
            import datetime as _dt

            while True:
                try:
                    await ensure_schema()
                    async with get_session() as session:
                        result = await session.execute(
                            text(
                                """
                            SELECT m.id, m.project_id, m.created_ts, mr.agent_id
                            FROM messages m
                            JOIN message_recipients mr ON mr.message_id = m.id
                            WHERE m.ack_required = 1 AND mr.ack_ts IS NULL
                            """
                            )
                        )
                        rows = result.fetchall()
                    now = _dt.datetime.now(_dt.timezone.utc)
                    now_naive = now.replace(tzinfo=None)
                    for mid, project_id, created_ts, agent_id in rows:
                        # Normalize to timezone-aware UTC before arithmetic; SQLite may yield naive datetimes
                        ts = created_ts
                        if getattr(ts, "tzinfo", None) is None or ts.tzinfo.utcoffset(ts) is None:
                            ts = ts.replace(tzinfo=_dt.timezone.utc)
                        else:
                            ts = ts.astimezone(_dt.timezone.utc)
                        age = (now - ts).total_seconds()
                        if age >= settings.ack_ttl_seconds:
                            try:
                                rich_console = importlib.import_module("rich.console")
                                rich_panel = importlib.import_module("rich.panel")
                                rich_text = importlib.import_module("rich.text")
                                Console = rich_console.Console
                                Panel = rich_panel.Panel
                                Text = rich_text.Text
                                con = Console()
                                body = Text.assemble(
                                    ("message_id: ", "cyan"),
                                    (str(mid), "white"),
                                    "\n",
                                    ("agent_id: ", "cyan"),
                                    (str(agent_id), "white"),
                                    "\n",
                                    ("project_id: ", "cyan"),
                                    (str(project_id), "white"),
                                    "\n",
                                    ("age_s: ", "cyan"),
                                    (str(int(age)), "white"),
                                    "\n",
                                    ("ttl_s: ", "cyan"),
                                    (str(settings.ack_ttl_seconds), "white"),
                                )
                                con.print(Panel(body, title="ACK Overdue", border_style="red"))
                            except Exception:
                                print(
                                    f"ack-warning message_id={mid} project_id={project_id} agent_id={agent_id} age_s={int(age)} ttl_s={settings.ack_ttl_seconds}"
                                )
                            with contextlib.suppress(Exception):
                                structlog.get_logger("tasks").warning(
                                    "ack_overdue",
                                    message_id=str(mid),
                                    project_id=str(project_id),
                                    agent_id=str(agent_id),
                                    age_s=int(age),
                                    ttl_s=int(settings.ack_ttl_seconds),
                                )
                            if settings.ack_escalation_enabled:
                                mode = (settings.ack_escalation_mode or "log").lower()
                                if mode == "file_reservation":
                                    try:
                                        y_dir = created_ts.strftime("%Y")
                                        m_dir = created_ts.strftime("%m")
                                        # Resolve the exact project/recipient lifetimes.
                                        async with get_session() as s_lookup:
                                            project_snapshot = await s_lookup.get(
                                                Project,
                                                int(project_id),
                                            )
                                            recipient_snapshot = await s_lookup.get(
                                                Agent,
                                                int(agent_id),
                                            )
                                        if (
                                            project_snapshot is None
                                            or recipient_snapshot is None
                                            or recipient_snapshot.project_id
                                            != project_snapshot.id
                                        ):
                                            raise ValueError(
                                                "ACK escalation project or recipient lifetime no longer exists."
                                            )
                                        recipient_name = recipient_snapshot.name
                                        pattern = (
                                            f"agents/{recipient_name}/inbox/{y_dir}/{m_dir}/*.md"
                                        )
                                        holder = recipient_snapshot
                                        if settings.ack_escalation_claim_holder_name:
                                            claim_name = settings.ack_escalation_claim_holder_name
                                            holder = await _ensure_ack_escalation_holder(
                                                settings=settings,
                                                project=project_snapshot,
                                                recipient_agent=recipient_snapshot,
                                                claim_name=claim_name,
                                                now_naive=now_naive,
                                            )
                                        await _create_ack_escalation_reservation(
                                            project=project_snapshot,
                                            holder=holder,
                                            path_pattern=pattern,
                                            exclusive=settings.ack_escalation_claim_exclusive,
                                            now_naive=now_naive,
                                            ttl_seconds=settings.ack_escalation_claim_ttl_seconds,
                                        )
                                    except Exception:
                                        pass
                except Exception:
                    pass
                await asyncio.sleep(settings.ack_ttl_scan_interval_seconds)

        async def _worker_tool_metrics() -> None:
            log = structlog.get_logger("tool.metrics")
            while True:
                try:
                    snapshot = _tool_metrics_snapshot()
                    if snapshot:
                        log.info("tool_metrics_snapshot", tools=snapshot)
                except Exception:
                    pass
                await asyncio.sleep(max(5, settings.tool_metrics_emit_interval_seconds))

        async def _worker_retention_quota() -> None:
            while True:
                with contextlib.suppress(Exception):
                    report = await _collect_retention_quota_report(settings)
                    structlog.get_logger("maintenance").info(
                        "retention_quota_report",
                        **report,
                    )
                    # Quota alerts
                    limit_b = int(settings.quota_attachments_limit_bytes)
                    inbox_limit = int(settings.quota_inbox_limit_count)
                    if limit_b > 0:
                        for proj, used in report["per_project_attach"].items():
                            if used >= limit_b:
                                structlog.get_logger("maintenance").warning(
                                    "quota_attachments_exceeded", project=proj, used_bytes=used, limit_bytes=limit_b
                                )
                    if inbox_limit > 0:
                        for proj, cnt in report["per_project_inbox_counts"].items():
                            if cnt >= inbox_limit:
                                structlog.get_logger("maintenance").warning(
                                    "quota_inbox_exceeded", project=proj, inbox_count=cnt, limit=inbox_limit
                                )
                await asyncio.sleep(max(60, settings.retention_report_interval_seconds))

        async def _worker_fd_health() -> None:
            """Periodic file descriptor health monitor.

            Checks FD headroom every 30 seconds and proactively cleans up
            resources when headroom drops below safe thresholds. This prevents
            the EMFILE -> socket closed -> unreachable cascade that occurs
            under sustained multi-agent load.

            Also monitors lockfile FD leaks (issue #116) and cleans up
            deleted-but-open .lock file descriptors.

            Thresholds:
            - 30% headroom: warning logged
            - 20% headroom: proactive cleanup triggered (includes lockfile FDs)
            - 15% headroom: error logged, aggressive cleanup
            """
            _fd_logger = structlog.get_logger("fd_health")
            while True:
                try:
                    current, limit = get_fd_usage()
                    if current >= 0 and limit > 0:
                        headroom_pct = (limit - current) / limit
                        cache_stats = get_repo_cache_stats()
                        lock_stats = get_lock_telemetry()

                        if headroom_pct < 0.15:
                            # Critical: aggressive cleanup
                            _fd_logger.error(
                                "fd_health.critical",
                                current_fds=current,
                                fd_limit=limit,
                                headroom_pct=round(headroom_pct * 100, 1),
                                repo_cache=cache_stats,
                                lock_telemetry=lock_stats,
                            )
                            freed = proactive_fd_cleanup(threshold=limit)
                            if freed:
                                _fd_logger.warning(
                                    "fd_health.emergency_cleanup",
                                    freed=freed,
                                    new_headroom=get_fd_headroom(),
                                )
                        elif headroom_pct < 0.20:
                            # Low: proactive cleanup
                            _fd_logger.warning(
                                "fd_health.low",
                                current_fds=current,
                                fd_limit=limit,
                                headroom_pct=round(headroom_pct * 100, 1),
                                repo_cache=cache_stats,
                                lock_telemetry=lock_stats,
                            )
                            freed = proactive_fd_cleanup(threshold=int(limit * 0.25))
                            if freed:
                                _fd_logger.info(
                                    "fd_health.proactive_cleanup",
                                    freed=freed,
                                    new_headroom=get_fd_headroom(),
                                )
                        elif headroom_pct < 0.30:
                            # Warning only
                            _fd_logger.warning(
                                "fd_health.warning",
                                current_fds=current,
                                fd_limit=limit,
                                headroom_pct=round(headroom_pct * 100, 1),
                                repo_cache=cache_stats,
                                lock_telemetry=lock_stats,
                            )
                except Exception:
                    pass
                await asyncio.sleep(30)

        async def _worker_auto_retire_stale_agents() -> None:
            log = structlog.get_logger("maintenance.auto_retire")
            interval = max(60, int(settings.auto_retire_stale_agents_interval_seconds))
            threshold = max(60, int(settings.auto_retire_stale_agents_threshold_seconds))
            while True:
                with contextlib.suppress(Exception):
                    retired = await sweep_stale_agents(threshold_seconds=threshold)
                    if retired:
                        log.info(
                            "auto_retired_stale_agents",
                            count=len(retired),
                            threshold_seconds=threshold,
                            agents=[
                                {
                                    "agent": entry["agent_name"],
                                    "project": entry["project_key"],
                                    "last_active_ts": entry["last_active_ts"],
                                }
                                for entry in retired
                            ],
                        )
                await asyncio.sleep(interval)

        tasks = []
        # FD health monitor always runs - it's critical for preventing EMFILE cascades
        tasks.append(asyncio.create_task(_worker_fd_health()))
        if settings.file_reservations_cleanup_enabled:
            tasks.append(asyncio.create_task(_worker_cleanup()))
        if settings.ack_ttl_enabled:
            tasks.append(asyncio.create_task(_worker_ack_ttl()))
        if settings.tool_metrics_emit_enabled:
            tasks.append(asyncio.create_task(_worker_tool_metrics()))
        if settings.retention_report_enabled or settings.quota_enabled:
            tasks.append(asyncio.create_task(_worker_retention_quota()))
        if settings.auto_retire_stale_agents_enabled:
            tasks.append(asyncio.create_task(_worker_auto_retire_stale_agents()))
        fastapi_app.state._background_tasks = tasks

    async def _shutdown() -> None:  # pragma: no cover - service lifecycle
        tasks = getattr(fastapi_app.state, "_background_tasks", [])
        for task in tasks:
            task.cancel()
        # Await cancelled tasks with a timeout to prevent shutdown hangs
        # (aiosqlite cancellation can block indefinitely)
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait(tasks, timeout=5.0)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan_context(app: FastAPI):
        # Ensure both mounted MCP apps initialize their internal task groups
        # (each http_app() call owns an independent StreamableHTTPSessionManager).
        mcp_lifespan_app = cast(_FastAPILifespan, mcp_http_app)
        mcp_stateful_lifespan_app = cast(_FastAPILifespan, mcp_stateful_http_app)
        async with (
            mcp_lifespan_app.lifespan(mcp_http_app),
            mcp_stateful_lifespan_app.lifespan(mcp_stateful_http_app),
        ):
            await _startup()
            try:
                yield
            finally:
                await _shutdown()

    # Now construct FastAPI with the composed lifespan so ASGI transports run it.
    # Give the app a real title/version so the auto-generated /openapi.json has a
    # proper `info` block (derive the version from installed package metadata,
    # mirroring cli._package_version; never hardcode a value that could drift).
    def _package_version() -> str:
        import importlib.metadata as _importlib_metadata

        try:
            return _importlib_metadata.version("mcp-agent-mail")
        except _importlib_metadata.PackageNotFoundError:  # pragma: no cover - dev installs
            return "0.0.0+local"

    fastapi_app = FastAPI(
        title="MCP Agent Mail",
        version=_package_version(),
        lifespan=lifespan_context,
    )

    # Simple request logging (configurable)
    if settings.http.request_log_enabled:
        import time as _time

        class RequestLoggingMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
                start = _time.time()
                method = request.method
                path = request.url.path
                client = request.client.host if request.client else "-"
                response = None
                exc: BaseException | None = None
                try:
                    response = await call_next(request)
                    return response
                except BaseException as err:
                    exc = err
                    raise
                finally:
                    # Always emit a log line, even when the handler raised (#215).
                    dur_ms = int((_time.time() - start) * 1000)
                    status_code = getattr(response, "status_code", 0) if response is not None else 500
                    with contextlib.suppress(Exception):
                        log = structlog.get_logger("http")
                        if exc is not None:
                            log.error(
                                "request",
                                method=method,
                                path=path,
                                status=status_code,
                                duration_ms=dur_ms,
                                client_ip=client,
                                error=repr(exc),
                            )
                        else:
                            log.info(
                                "request",
                                method=method,
                                path=path,
                                status=status_code,
                                duration_ms=dur_ms,
                                client_ip=client,
                            )
                    try:
                        rich_console = importlib.import_module("rich.console")
                        rich_panel = importlib.import_module("rich.panel")
                        rich_text = importlib.import_module("rich.text")
                        Console = rich_console.Console
                        Panel = rich_panel.Panel
                        Text = rich_text.Text
                        console = Console(width=100)
                        title = Text.assemble(
                            (method, "bold blue"),
                            ("  "),
                            (path, "bold white"),
                            ("  "),
                            (f"{status_code}", "bold green" if 200 <= status_code < 400 else "bold red"),
                            ("  "),
                            (f"{dur_ms}ms", "bold yellow"),
                        )
                        body = Text.assemble(
                            ("client: ", "cyan"),
                            (client, "white"),
                        )
                        if exc is not None:
                            body = Text.assemble(body, "\n", ("error: ", "cyan"), (repr(exc), "red"))
                        console.print(Panel(body, title=title, border_style="dim"))
                    except Exception:
                        suffix = f" error={exc!r}" if exc is not None else ""
                        print(
                            f"http method={method} path={path} status={status_code} ms={dur_ms} client={client}{suffix}"
                        )

        app_any = cast(Any, fastapi_app)
        app_any.add_middleware(RequestLoggingMiddleware)

    # Unified JWT/RBAC and robust rate limiter middleware
    if (
        settings.http.rate_limit_enabled
        or getattr(settings.http, "jwt_enabled", False)
        or getattr(settings.http, "rbac_enabled", True)
    ):
        app_any = cast(Any, fastapi_app)
        app_any.add_middleware(SecurityAndRateLimitMiddleware, settings=settings)
    # Bearer auth for non-localhost only; allow localhost unauth optionally for seamless local dev
    if settings.http.bearer_token:
        from typing import Any as _Any, cast as _cast  # local type-only import
        app_any = _cast(_Any, fastapi_app)
        app_any.add_middleware(
            BearerAuthMiddleware,
            token=settings.http.bearer_token,
            allow_localhost=bool(getattr(settings.http, "allow_localhost_unauthenticated", False)),
            jwt_enabled=bool(getattr(settings.http, "jwt_enabled", False)),
        )

    # Registered AFTER BearerAuthMiddleware, which with Starlette's add_middleware
    # means it wraps it and therefore runs FIRST. That is required, not incidental:
    # it must decide /mail before the bearer layer can 401 a browser that has no
    # way to send an Authorization header. Everything outside /mail passes straight
    # through, so the MCP mounts are unaffected.
    app_any3 = cast(Any, fastapi_app)
    app_any3.add_middleware(MailUiAuthMiddleware, settings=settings)

    # Registered last, so it wraps everything and inspects the body before any
    # layer tries to parse it. An undecodable body is a property of the request,
    # not of who is asking, so this is answered ahead of authentication.
    app_any4 = cast(Any, fastapi_app)
    app_any4.add_middleware(Utf8BodyGuardMiddleware)

    # Registered after the body guard so even authentication, CSRF, rate-limit,
    # validation, and invalid-encoding failures on account-bound endpoints are
    # explicitly non-cacheable.
    app_any5 = cast(Any, fastapi_app)
    app_any5.add_middleware(MailUiAccountNoStoreMiddleware)

    # Optional CORS
    if settings.cors.enabled:
        from typing import Any as _Any, cast as _cast  # local type-only import
        app_any2 = _cast(_Any, fastapi_app)
        app_any2.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors.origins or [],
            allow_credentials=settings.cors.allow_credentials,
            allow_methods=settings.cors.allow_methods or ["*"],
            allow_headers=settings.cors.allow_headers or ["*"],
        )

    # Health endpoints
    @fastapi_app.get("/health/liveness")
    async def liveness() -> JSONResponse:
        return JSONResponse({"status": "alive"})

    @fastapi_app.get("/health/readiness")
    async def readiness() -> JSONResponse:
        try:
            await readiness_check()
        except Exception as exc:
            try:
                rich_console = importlib.import_module("rich.console")
                rich_panel = importlib.import_module("rich.panel")
                Console = rich_console.Console
                Panel = rich_panel.Panel
                Console().print(Panel.fit(str(exc), title="Readiness Error", border_style="red"))
            except Exception:
                pass
            with contextlib.suppress(Exception):
                structlog.get_logger("health").error("readiness_error", error=str(exc))
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        return JSONResponse({"status": "ready"})

    @fastapi_app.get("/api/health")
    async def api_health_bypass() -> JSONResponse:
        """Lightweight health probe that bypasses the MCP transport layer.

        Returns immediately without touching the database or connection pool,
        so it stays responsive even when the MCP ASGI pipeline is saturated
        under heavy multi-agent load.
        """
        return JSONResponse({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

    @fastapi_app.get("/events")
    async def events_stream(request: Request) -> Any:
        """Wake one agent the moment something lands in its mailbox.

        The inbox hook polls: always at session start, then at most once every
        two minutes, and only while the agent is running tools. An *idle*
        session is therefore never reached — which is exactly the case the
        mailbox exists for, an agent waiting on an answer or a human saying
        "stop". This closes that gap without a monitor runtime: a background
        process that exits already re-invokes the agent, so a `curl -N` that
        returns *is* the wake.

        The stream carries one hint and then closes, deliberately. Frames are
        thin — the woken client pulls content through `fetch_inbox`, which
        already enforces who may read what.

        Registered before the MCP mounts below; one of them claims a broad
        prefix and would otherwise shadow this path.
        """
        project_key = (request.query_params.get("project") or "").strip()
        agent_name = (request.query_params.get("agent") or "").strip()
        token = (request.headers.get("x-agent-mail-registration-token") or "").strip()

        # One reply for every failure. The bearer this endpoint sits behind is
        # server-wide, not per project, so a caller who holds it could otherwise
        # sweep names and learn which agents exist and when their mail arrives.
        unauthorized = JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if not project_key or not agent_name or not token:
            return unauthorized

        await ensure_schema()
        async with get_session() as session:
            project_row = await _resolve_mail_project(session, project_key)
            if project_row is None:
                return unauthorized
            row = (
                await session.execute(
                    text(
                        "SELECT a.id, a.name, a.registration_token, a.retired_at, "
                        "a.agent_generation, p.slug, p.project_generation, p.archived_at "
                        "FROM agents a JOIN projects p ON p.id = a.project_id "
                        "WHERE a.project_id = :pid AND lower(a.name) = lower(:a)"
                    ),
                    {"pid": int(project_row[0]), "a": agent_name},
                )
            ).fetchone()

        if row is None or not row[2] or row[3] is not None or row[7] is not None:
            return unauthorized
        # compare_digest, not ==, so a caller cannot narrow the token by timing.
        if not hmac.compare_digest(str(row[2]), token):
            return unauthorized
        agent_id = int(row[0])
        canonical_agent = str(row[1])
        agent_generation = str(row[4])
        project_slug = str(row[5])
        project_generation = str(row[6])
        project_id = int(project_row[0])

        # Subscribe BEFORE the client catches up, not after. The client's
        # contract is: connect, wait for `: ready`, pull the inbox, then wait.
        # Checking first and subscribing second would drop anything that
        # arrived in between — a lost wakeup with no trace on either side.
        queue = hub.subscribe(
            project_slug,
            project_generation,
            canonical_agent,
            agent_generation,
        )

        async def stream() -> Any:
            deadline = asyncio.get_running_loop().time() + MAX_STREAM_SECONDS
            try:
                yield b": ready\n\n"
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        yield b": bye\n\n"
                        return
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=min(KEEPALIVE_SECONDS, remaining)
                        )
                    except asyncio.TimeoutError:
                        if not await _agent_stream_lifetime_valid(
                            project_id=project_id,
                            project_slug=project_slug,
                            project_generation=project_generation,
                            agent_id=agent_id,
                            agent_name=canonical_agent,
                            agent_generation=agent_generation,
                            registration_token=token,
                        ):
                            return
                        yield b": ping\n\n"
                        continue
                    if not await _agent_stream_lifetime_valid(
                        project_id=project_id,
                        project_slug=project_slug,
                        project_generation=project_generation,
                        agent_id=agent_id,
                        agent_name=canonical_agent,
                        agent_generation=agent_generation,
                        registration_token=token,
                    ):
                        return
                    # No `id:` line: that would advertise Last-Event-ID replay
                    # this stream does not have. The mailbox is the log.
                    yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
                    return
            finally:
                # Must run on client disconnect too, or the hub accumulates
                # queues for connections that are long gone and publishes into
                # them forever.
                hub.unsubscribe(
                    project_slug,
                    project_generation,
                    canonical_agent,
                    agent_generation,
                    queue,
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                # Tells nginx not to buffer; harmless elsewhere. Without it a
                # buffering proxy holds the frame and the wake never arrives.
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    def _oauth_metadata_disabled_response() -> JSONResponse:
        return JSONResponse({"mcp_oauth": False}, status_code=404)

    def _register_oauth_metadata_disabled(path: str) -> None:
        async def _oauth_metadata_disabled() -> JSONResponse:
            return _oauth_metadata_disabled_response()

        fastapi_app.add_api_route(path, _oauth_metadata_disabled, methods=["GET"], include_in_schema=False)

    # Thin ASGI wrapper that normalizes Accept / Content-Type headers for
    # MCP clients (some omit Accept entirely) and then delegates to the
    # SDK's native mcp_http_app which properly coordinates server lifecycle,
    # request handling, and session management via StreamableHTTPSessionManager.
    #
    # In production the parent FastAPI lifespan initializes the session manager
    # task group before any requests arrive.  In test environments (httpx
    # ASGITransport) no lifespan events are sent, so the wrapper lazily enters
    # the MCP app's lifespan on first request to avoid "Task group not
    # initialized" errors.
    class _HeaderFixupMCPApp:
        """Normalize headers then delegate to the native MCP HTTP app."""

        def __init__(self, native_app: FastAPI) -> None:
            self._app = native_app
            self._lifespan_entered = False
            self._lifespan_cm: Any = None
            self._lifespan_lock: asyncio.Lock | None = None

        async def _ensure_lifespan(self) -> None:
            """Lazily enter the MCP app's lifespan if not already running.

            This handles test environments where ASGI lifespan events are never
            sent (e.g. httpx ASGITransport).  In production the parent app's
            lifespan context already calls mcp_http_app.lifespan, so the
            session manager's task group will already be initialized and this
            method is a fast no-op.

            Uses double-check locking to prevent concurrent requests from
            entering the lifespan context manager twice.
            """
            if self._lifespan_entered:
                return
            # Lazily create the lock (must be in async context for the
            # correct event loop).
            if self._lifespan_lock is None:
                self._lifespan_lock = asyncio.Lock()
            async with self._lifespan_lock:
                if self._lifespan_entered:
                    return
                # Check if the session manager is already running (production path)
                session_mgr = getattr(self._app.state, "session_manager", None)
                if session_mgr is None:
                    # Try to find it via route endpoint
                    for route in getattr(self._app, "routes", []):
                        endpoint = getattr(route, "endpoint", None)
                        sm = getattr(endpoint, "session_manager", None)
                        if sm is not None:
                            session_mgr = sm
                            break
                if session_mgr is not None and getattr(session_mgr, "_task_group", None) is not None:
                    self._lifespan_entered = True
                    return
                # Enter the MCP app's lifespan (test path)
                mcp_lifespan_app = cast(_FastAPILifespan, self._app)
                self._lifespan_cm = mcp_lifespan_app.lifespan(self._app)
                await self._lifespan_cm.__aenter__()
                self._lifespan_entered = True

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope.get("type") != "http":
                # Delegate non-HTTP scopes (e.g. lifespan) directly
                await self._app(scope, receive, send)
                return

            await self._ensure_lifespan()

            headers = list(scope.get("headers") or [])

            def _has_header(key: bytes) -> bool:
                lk = key.lower()
                return any(h[0].lower() == lk for h in headers)

            # Ensure both JSON and SSE are accepted; httpx defaults no Accept header
            headers = [(k, v) for (k, v) in headers if k.lower() != b"accept"]
            headers.append((b"accept", b"application/json, text/event-stream"))
            if scope.get("method") == "POST" and not _has_header(b"content-type"):
                headers.append((b"content-type", b"application/json"))
            new_scope = dict(scope)
            new_scope["headers"] = headers

            await self._app(new_scope, receive, send)

    # Mount at both '/base' and '/base/' to tolerate either form from clients/tests.
    # Also mount compatibility aliases for both '/api' and '/mcp' regardless of configured base.
    base_no_slash = _normalized_http_base_path(settings.http.path)
    base_with_slash = base_no_slash if base_no_slash == "/" else base_no_slash + "/"
    stateless_app = _HeaderFixupMCPApp(mcp_http_app)
    stateful_app = _HeaderFixupMCPApp(mcp_stateful_http_app)

    # Path -> app mapping (issue #250): the '/mcp' compat alias is the
    # stateful, Mcp-Session-Id-issuing endpoint; '/api' and the configured
    # base stay stateless for handshake-skipping one-shot clients (e.g. ntm).
    # The CONFIGURED base always keeps the legacy stateless behavior, even if
    # an operator points it at '/mcp' — an explicit HTTP_PATH is a promise to
    # existing clients of that deployment, so we never change its semantics.
    def _app_for_mount(path: str) -> _HeaderFixupMCPApp:
        normalized = path.rstrip("/") or "/"
        if normalized == "/mcp" and base_no_slash != "/mcp":
            return stateful_app
        return stateless_app

    mount_paths = [base_no_slash, base_with_slash]
    for compat_base in ("/api", "/mcp"):
        compat_no_slash = compat_base.rstrip("/") or "/"
        compat_with_slash = compat_no_slash if compat_no_slash == "/" else compat_no_slash + "/"
        if compat_no_slash not in mount_paths:
            mount_paths.append(compat_no_slash)
        if compat_with_slash not in mount_paths:
            mount_paths.append(compat_with_slash)

    oauth_metadata_paths: set[str] = set()

    def _add_oauth_metadata_path(path: str) -> None:
        normalized = path.rstrip("/") or "/"
        oauth_metadata_paths.add(normalized)
        if normalized != "/":
            oauth_metadata_paths.add(f"{normalized}/")

    _add_oauth_metadata_path("/.well-known/oauth-authorization-server")
    _add_oauth_metadata_path("/.well-known/oauth-authorization-server/mcp")
    for mount_path in mount_paths:
        normalized = mount_path.rstrip("/") or "/"
        if normalized == "/":
            continue
        _add_oauth_metadata_path(f"{normalized}/.well-known/oauth-authorization-server")
        _add_oauth_metadata_path(f"{normalized}/.well-known/oauth-authorization-server/mcp")
        _add_oauth_metadata_path(f"/.well-known/oauth-authorization-server{normalized}")
    for path in sorted(oauth_metadata_paths):
        _register_oauth_metadata_disabled(path)

    for mount_path in mount_paths:
        with contextlib.suppress(Exception):
            fastapi_app.mount(mount_path, _app_for_mount(mount_path))

    # Expose composed lifespan via router
    fastapi_app.router.lifespan_context = lifespan_context

    # Add direct routes at no-slash base paths to tolerate clients omitting trailing slashes.
    def _register_base_passthrough(base_path_no_slash: str, base_path_with_slash: str) -> None:
        # Dispatch to the same app that is mounted at this base (issue #250:
        # '/mcp' is stateful, everything else stateless).
        target_app = _app_for_mount(base_path_no_slash)

        @fastapi_app.post(base_path_no_slash)
        async def _base_passthrough(request: Request) -> JSONResponse:
            # Re-dispatch to the mounted MCP app by calling it directly
            response_body: dict[str, Any] = {}
            status_code = 200
            headers: dict[str, str] = {}

            async def _send(message: MutableMapping[str, Any]) -> None:
                nonlocal response_body, status_code, headers
                if message.get("type") == "http.response.start":
                    status_code = int(message.get("status", 200))
                    hdrs = message.get("headers") or []
                    for k, v in hdrs:
                        headers[k.decode("latin1")] = v.decode("latin1")
                elif message.get("type") == "http.response.body":
                    body = message.get("body") or b""
                    try:
                        response_body = json.loads(body.decode("utf-8")) if body else {}
                    except Exception:
                        response_body = {}

            # If localhost and allow_localhost_unauthenticated, synthesize Authorization header automatically
            scope = dict(request.scope)
            if _localhost_bypass_allowed(
                request,
                allow_localhost=bool(settings.http.allow_localhost_unauthenticated),
            ):
                scope_headers = list(scope.get("headers") or [])
                has_auth = any(k.lower() == b"authorization" for k, _ in scope_headers)
                if not has_auth and settings.http.bearer_token:
                    scope_headers.append((b"authorization", f"Bearer {settings.http.bearer_token}".encode("latin1")))
                scope["headers"] = scope_headers
            await target_app(
                {**scope, "path": "/"},  # MCP app expects requests at its root
                request.receive,
                _send,
            )
            return JSONResponse(response_body, status_code=status_code, headers=headers)

    passthrough_pairs: list[tuple[str, str]] = [(base_no_slash, base_with_slash)]
    for compat_base in ("/api", "/mcp"):
        compat_no_slash = compat_base.rstrip("/") or "/"
        compat_with_slash = compat_no_slash if compat_no_slash == "/" else compat_no_slash + "/"
        if (compat_no_slash, compat_with_slash) not in passthrough_pairs:
            passthrough_pairs.append((compat_no_slash, compat_with_slash))
    for no_slash, with_slash in passthrough_pairs:
        _register_base_passthrough(no_slash, with_slash)

    # ----- Simple SSR Mail UI -----
    def _register_mail_ui() -> None:
        import bleach
        import markdown2

        try:
            from bleach.css_sanitizer import CSSSanitizer as _CSSSanitizerImport
        except Exception:  # tinycss2 may be missing; degrade gracefully
            _CSSSanitizer = None
        else:
            _CSSSanitizer = _CSSSanitizerImport
        CSSSanitizer = cast(Any, _CSSSanitizer)
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        templates_root = Path(__file__).resolve().parent / "templates"
        env = Environment(
            loader=FileSystemLoader(str(templates_root)),
            autoescape=select_autoescape(["html", "xml"]),
            enable_async=True,
        )
        # HTML sanitizer (allow safe images and limited CSS)
        _css_sanitizer = (
            CSSSanitizer(
                allowed_css_properties=["color", "background-color", "text-align", "text-decoration", "font-weight"]
            )
            if CSSSanitizer
            else None
        )

        _html_cleaner = bleach.Cleaner(
            tags=[
                "a",
                "abbr",
                "acronym",
                "b",
                "blockquote",
                "code",
                "em",
                "i",
                "li",
                "ol",
                "ul",
                "p",
                "pre",
                "strong",
                "table",
                "thead",
                "tbody",
                "tr",
                "th",
                "td",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "hr",
                "br",
                "span",
                "img",
            ],
            attributes={
                "*": ["class"],
                "a": ["href", "title", "rel"],
                "abbr": ["title"],
                "acronym": ["title"],
                "code": ["class"],
                "pre": ["class"],
                "span": ["class", "style"],
                "p": ["class", "style"],
                "table": ["class", "style"],
                "td": ["class", "style"],
                "th": ["class", "style"],
                "img": lambda _tag, attribute, value: (
                    attribute in {"alt", "title", "width", "height", "loading", "decoding", "class"}
                    or (attribute == "src" and _mail_ui_inline_image_source_allowed(value))
                ),
            },
            protocols=["http", "https", "mailto", "data"],
            strip=True,
            css_sanitizer=_css_sanitizer,
        )

        async def _render(name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
            ctx.setdefault("mail_ui_user", _mail_ui_template_user.get())
            ctx.setdefault("mail_ui_access", None)
            tpl = env.get_template(name)
            html = await tpl.render_async(**ctx)
            return HTMLResponse(
                html,
                status_code=status_code,
                headers=_MAIL_LEGACY_HTML_HEADERS,
            )

        def _parse_fts_query(
            raw: str, scope_preference: str | None = None
        ) -> tuple[str, str, str, list[dict[str, str]]]:
            """Return (fts_expression, like_pattern) from a user query.
            Supports subject:foo and body:"multi word" tokens; otherwise defaults to subject/body OR.
            """
            raw = (raw or "").strip()
            if not raw:
                return "", "", "both", []
            scope_pref = scope_preference if scope_preference in {"subject", "body"} else "both"
            # tokens: key:"phrase" | "phrase" | key:word | word
            parts = re.findall(r"\w+:\"[^\"]+\"|\"[^\"]+\"|\w+:[^\s]+|[^\s]+", raw)
            exprs: list[str] = []
            like_terms: list[str] = []
            like_scope = scope_pref
            tokens: list[dict[str, str]] = []

            def _quote(s: str) -> str:
                return '"' + s.replace('"', '""') + '"'

            def _like_escape(term: str) -> str:
                return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")

            for p in parts:
                key = None
                val = p
                if ":" in p and not p.startswith('"'):
                    maybe_key, maybe_val = p.split(":", 1)
                    if maybe_key in {"subject", "body"}:
                        key = maybe_key
                        val = maybe_val
                val = val.strip()
                val_inner = val[1:-1] if val.startswith('"') and val.endswith('"') and len(val) >= 2 else val

                # For LIKE pattern, we want literal matching of the user's term
                like_terms.append(_like_escape(val_inner))

                if key in {"subject", "body"}:
                    exprs.append(f"{key}:{_quote(val_inner)}")
                    tokens.append({"field": key, "value": val_inner})
                else:
                    if scope_pref == "subject":
                        exprs.append(f"subject:{_quote(val_inner)}")
                        tokens.append({"field": "subject", "value": val_inner})
                    elif scope_pref == "body":
                        exprs.append(f"body:{_quote(val_inner)}")
                        tokens.append({"field": "body", "value": val_inner})
                    else:
                        exprs.append(f"(subject:{_quote(val_inner)} OR body:{_quote(val_inner)})")
                        tokens.append({"field": "both", "value": val_inner})
            fts = " AND ".join(exprs) if exprs else ""
            like_pat = "%" + "%".join(like_terms) + "%" if like_terms else ""
            return fts, like_pat, like_scope, tokens

        def _safe_fts_snippet(raw_snippet: object) -> tuple[Markup, int]:
            """Escape agent-controlled FTS text while preserving highlights.

            SQLite's ``snippet`` function returns source text and the two
            caller-supplied highlight tags in one string.  Escaping the whole
            value first, then restoring only exact, attribute-free ``mark``
            tags keeps the highlight useful without trusting message HTML.
            """

            snippet = str(raw_snippet or "")
            hits = snippet.count("<mark>")
            escaped = str(escape_markup(snippet))
            highlighted = escaped.replace("&lt;mark&gt;", "<mark>").replace(
                "&lt;/mark&gt;", "</mark>"
            )
            return Markup(highlighted), hits

        @fastapi_app.get("/mail/api/locks", response_class=JSONResponse)
        async def mail_lock_status(request: Request) -> JSONResponse:
            """Return metadata about active archive locks for observability."""

            settings_local = get_settings()
            _mail_ui_require_admin_read(settings=settings_local, request=request)
            payload = collect_lock_status(settings_local)
            return JSONResponse(payload)

        @fastapi_app.get("/mail/api/file-reservations", response_class=JSONResponse)
        async def mail_active_file_reservations(
            request: Request,
            project: str,
            path: str | None = None,
        ) -> JSONResponse:
            """Active advisory file reservations, optionally narrowed to one path.

            Exists because there is no read-only way to ask this question: the MCP
            surface can create, renew, release and force-release reservations, but
            every one of those mutates state, and the only listing is the HTML
            viewer page. An editor hook that wants to warn "someone else has
            reserved this file" needs a cheap, side-effect-free answer — reserving
            a path in order to discover it was already reserved is precisely the
            collision the feature exists to avoid.

            "Active" means not released and not yet expired.

            The expiry comparison is done in PYTHON, not in SQL, and that is not
            an accident. SQLite stores these timestamps as TEXT in
            ``YYYY-MM-DD HH:MM:SS.ffffff`` form (space separator), while the
            datetime adapter registered in db.py binds a bound parameter as ISO
            8601 with a ``T`` separator. A ``WHERE expires_ts > :now`` therefore
            compares ``'... 13:11:34'`` against ``'...T12:15:00'`` as strings, and
            since ``' '`` (0x20) sorts before ``'T'`` (0x54) the predicate is
            ALWAYS false — the endpoint would return 200 with an empty list
            forever and look like it worked.
            """
            await ensure_schema()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            async with get_session() as session:
                prow = await _resolve_mail_project(session, project)
                if not prow:
                    return JSONResponse({"detail": "Project not found"}, status_code=404)
                if not getattr(request.state, "mail_ui_service_principal", False):
                    await _mail_ui_require_project_access(
                        settings=settings,
                        request=request,
                        session=session,
                        project_id=int(prow[0]),
                    )
                rows = (
                    await session.execute(
                        text(
                            "SELECT c.id, a.name, c.path_pattern, c.exclusive, c.reason, "
                            "c.expires_ts, a.display_name, c.execution_id, c.origin, "
                            "e.status AS execution_status "
                            "FROM file_reservations c LEFT JOIN agents a ON a.id = c.agent_id "
                            "LEFT JOIN agent_executions e ON e.id = c.execution_id "
                            "WHERE c.project_id = :pid AND c.released_ts IS NULL "
                            "ORDER BY c.created_ts DESC"
                        ),
                        {"pid": int(prow[0])},
                    )
                ).fetchall()

            def _still_active(raw: Any) -> bool:
                if isinstance(raw, datetime):
                    return raw.replace(tzinfo=None) > now
                try:
                    return datetime.fromisoformat(str(raw).replace(" ", "T")) > now
                except (TypeError, ValueError):
                    # An unparseable expiry is reported rather than silently
                    # dropped: a warning that turns out to be stale costs a
                    # glance, a reservation that vanishes costs a collision.
                    return True

            # Annotated, not inferred: the value type of a dict literal is the
            # union of ALL its values, so the lone `bool(...)` on `exclusive`
            # below lands in the type of every other key. `path_pattern` is
            # `str` at the source (models.py) and comes from the join's driving
            # table, so it can never actually be a bool — but unannotated, the
            # checker rejects `.rstrip` on it. Narrowing here rather than
            # guarding at the use site: a `isinstance(..., str)` check there
            # would read as defending against data that cannot occur.
            items: list[dict[str, Any]] = [
                {
                    "id": r[0],
                    # LEFT JOIN: a reservation can outlive its owning agent row (#161).
                    "agent": r[1] or "<orphaned>",
                    # Alongside the name, never instead of it: the name is what
                    # has to be typed into `to:`, so a warning showing only the
                    # label would teach the reader an address the server
                    # rejects. Absent when unset rather than echoing the name.
                    **({"display_name": r[6]} if len(r) > 6 and r[6] else {}),
                    "path_pattern": r[2],
                    "exclusive": bool(r[3]),
                    "reason": r[4] or "",
                    "expires_ts": str(r[5]),
                    "execution_id": r[7],
                    "origin": r[8] or "explicit",
                    "execution_status": r[9],
                    "orphaned": r[1] is None
                    or (r[7] is not None and r[9] != "active"),
                    "legacy_unscoped": r[7] is None,
                }
                for r in rows
                if _still_active(r[5])
            ]

            if path:
                # Reservations are glob patterns, so a literal comparison would miss
                # the common "src/**/*.py" case. Match both directions: the queried
                # path against the stored pattern, and the pattern against the path
                # for the plain-prefix style ("src/mcp_agent_mail").
                from fnmatch import fnmatch

                # str.lstrip takes a SET of characters, not a prefix: ".gitignore"
                # would become "gitignore" and ".github/workflows/ci.yml" would
                # become "github/...". Every dot-prefixed path — .github, .claude,
                # .beads, .env — could then never match a stored pattern, so the
                # pre-edit warning was silently dead for exactly the files a
                # coordination layer most wants to protect.
                needle = path[2:] if path.startswith("./") else path.lstrip("/")
                items = [
                    i
                    for i in items
                    if fnmatch(needle, i["path_pattern"])
                    or needle == i["path_pattern"]
                    or needle.startswith(i["path_pattern"].rstrip("/*") + "/")
                ]

            return JSONResponse({"active": len(items), "reservations": items})

        async def _build_unified_inbox_payload(
            *, request: Request, limit: int = 500, include_projects: bool = True
        ) -> dict[str, Any]:
            """Fetch unified inbox data for HTML and JSON consumers."""

            safe_limit = max(1, min(int(limit), 1000))
            messages: list[dict[str, Any]] = []
            projects: list[dict[str, Any]] = []
            total_messages = 0
            visible_roles: dict[int, str | None] = {}

            try:
                await ensure_schema()

                sibling_map: dict[int, dict[str, Any]] = {}
                if include_projects:
                    if _mail_ui_request_is_admin(settings=settings, request=request):
                        await refresh_project_sibling_suggestions()
                    sibling_map = await get_project_sibling_data()

                async with get_session() as session:
                    visible_roles = await _mail_ui_visible_project_roles(
                        settings=settings,
                        request=request,
                        session=session,
                    )
                    visible_ids = sorted(visible_roles)
                    if not visible_ids:
                        return {
                            "messages": [],
                            "projects": [],
                            "agent_sounds": {},
                            "total_messages": 0,
                            "returned_messages": 0,
                            "has_more": False,
                        }
                    visible_params = {
                        f"visible_pid_{index}": project_id
                        for index, project_id in enumerate(visible_ids)
                    }
                    visible_placeholders = ", ".join(f":visible_pid_{index}" for index in range(len(visible_ids)))
                    project_predicate = f"m.project_id IN ({visible_placeholders})"
                    total_result = await session.execute(
                        text(
                            f"""
                            SELECT COUNT(*)
                            FROM messages m
                            JOIN agents sender ON sender.id = m.sender_id
                            JOIN projects p ON p.id = m.project_id
                            WHERE {project_predicate}
                            """
                        ),
                        visible_params,
                    )
                    total_messages = int(total_result.scalar_one())

                    # Fetch recent messages with sender/project and computed recipient list
                    query = text(
                        f"""
                        SELECT
                            m.id,
                            m.subject,
                            m.body_md,
                            LENGTH(COALESCE(m.body_md, '')) AS body_length,
                            m.created_ts,
                            m.importance,
                            m.thread_id,
                            m.project_id AS message_project_id,
                            sender.name AS sender_name,
                            sender.project_id AS sender_project_id,
                            sp.human_key AS sender_project_name,
                            sp.slug AS sender_project_slug,
                            p.slug AS project_slug,
                            p.human_key AS project_name,
                            COALESCE(
                                (
                                    SELECT GROUP_CONCAT(name, ', ')
                                    FROM (
                                        SELECT DISTINCT recip2.name AS name
                                        FROM message_recipients mr2
                                        JOIN agents recip2 ON recip2.id = mr2.agent_id
                                        WHERE mr2.message_id = m.id
                                        ORDER BY name
                                    )
                                ),
                                ''
                            ) AS recipients
                        FROM messages m
                        JOIN agents sender ON m.sender_id = sender.id
                        LEFT JOIN projects sp ON sp.id = sender.project_id
                        JOIN projects p ON m.project_id = p.id
                        WHERE {project_predicate}
                        ORDER BY m.created_ts DESC
                        LIMIT :limit
                        """
                    )

                    rows = await session.execute(query, {**visible_params, "limit": safe_limit})

                    for r in rows.mappings().all():
                        body = r["body_md"] or ""
                        raw_body_length = r["body_length"]
                        body_length = int(raw_body_length) if raw_body_length is not None else len(body)
                        excerpt = body[:150].replace('#', '').replace('*', '').replace('`', '').strip()
                        if body_length > 150:
                            excerpt += "..."

                        created_ts = r["created_ts"]
                        if isinstance(created_ts, str):
                            created_dt = datetime.fromisoformat(created_ts.replace('Z', '+00:00'))
                        else:
                            created_dt = created_ts

                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        else:
                            created_dt = created_dt.astimezone(timezone.utc)

                        now = datetime.now(timezone.utc)
                        delta = now - created_dt

                        if delta.days < 0 or (delta.days == 0 and delta.seconds < 0):
                            created_relative = "Just now"
                        elif delta.days > 365:
                            created_relative = f"{delta.days // 365}y ago"
                        elif delta.days > 30:
                            created_relative = f"{delta.days // 30}mo ago"
                        elif delta.days > 0:
                            created_relative = f"{delta.days}d ago"
                        elif delta.seconds > 3600:
                            created_relative = f"{delta.seconds // 3600}h ago"
                        elif delta.seconds > 60:
                            created_relative = f"{delta.seconds // 60}m ago"
                        else:
                            created_relative = "Just now"

                        sender_display, sender_meta = _http_sender_identity(
                            message_project_id=r["message_project_id"],
                            sender_name=r["sender_name"],
                            sender_project_id=r["sender_project_id"],
                            sender_project_human_key=r["sender_project_name"],
                            sender_project_slug=r["sender_project_slug"],
                        )
                        message_payload = {
                            "id": r["id"],
                            "subject": r["subject"] or "(No subject)",
                            "body_md": body,
                            "body_length": body_length,
                            "excerpt": excerpt,
                            "created_ts": str(r["created_ts"]),
                            "created_full": created_dt.strftime("%B %d, %Y at %I:%M %p"),
                            "created_relative": created_relative,
                            "importance": r["importance"] or "normal",
                            "thread_id": r["thread_id"],
                            "sender": sender_display,
                            "project_slug": r["project_slug"],
                            "project_name": r["project_name"],
                            "recipients": ", ".join(
                                part.strip() for part in (r["recipients"] or "").split(",") if part.strip()
                            ),
                            "read": False,
                            "can_reply": _mail_ui_access_context(
                                settings=settings,
                                request=request,
                                project_id=int(r["message_project_id"]),
                                project_role=visible_roles[int(r["message_project_id"])],
                            )["can_reply"],
                        }
                        message_payload.update(sender_meta)
                        messages.append(message_payload)

                    if include_projects:
                        rows = await session.execute(
                            text("SELECT id, slug, human_key, created_at, archived_at FROM projects ORDER BY created_at DESC")
                        )
                        for r in rows.fetchall():
                            project_id = int(r[0])
                            if project_id not in visible_roles:
                                continue
                            siblings = sibling_map.get(project_id, {"confirmed": [], "suggested": []})
                            access = _mail_ui_access_context(
                                settings=settings,
                                request=request,
                                project_id=project_id,
                                project_role=visible_roles[project_id],
                            )
                            confirmed_siblings = [
                                sibling
                                for sibling in siblings.get("confirmed", [])
                                if int(sibling["peer"]["id"]) in visible_roles
                            ]
                            suggested_siblings = [
                                sibling
                                for sibling in siblings.get("suggested", [])
                                if int(sibling["peer"]["id"]) in visible_roles
                            ]
                            projects.append(
                                {
                                    "id": project_id,
                                    "slug": r[1],
                                    "human_key": r[2],
                                    "created_at": str(r[3]),
                                    "archived_at": str(r[4]) if r[4] else None,
                                    "confirmed_siblings": confirmed_siblings,
                                    "suggested_siblings": suggested_siblings,
                                    "access_role": access["project_role"],
                                    "can_read": access["can_read"],
                                    "can_reply": access["can_reply"],
                                    "can_compose": access["can_compose"],
                                    "can_mutate": access["can_mutate"],
                                }
                            )

            except Exception as exc:  # pragma: no cover - defensive logging
                logging.error("Error fetching unified inbox data", exc_info=True, extra={"error": str(exc)})

            # Who sounds like what, for the chime in base.html. Flattened across
            # every project because this page spans all of them and the chime is
            # keyed by sender name, which is what the message rows carry.
            #
            # Its own query rather than a column on the projects rows above:
            # those rows are per-project and this map is per-agent, and only
            # agents who actually chose a tone belong in it — an empty map and a
            # missing island behave identically in the reader.
            agent_sounds: dict[str, str] = {}
            if visible_roles:
                async with get_session() as session:
                    visible_ids = sorted(visible_roles)
                    sound_params = {
                        f"sound_pid_{index}": project_id
                        for index, project_id in enumerate(visible_ids)
                    }
                    sound_placeholders = ", ".join(f":sound_pid_{index}" for index in range(len(visible_ids)))
                    srows = await session.execute(
                        text(
                            "SELECT name, notify_sound FROM agents "
                            f"WHERE notify_sound IS NOT NULL AND project_id IN ({sound_placeholders})"
                        ),
                        sound_params,
                    )
                    agent_sounds = {r[0]: r[1] for r in srows.fetchall() if r[0] and r[1]}

            return {
                "messages": messages,
                "projects": projects,
                "agent_sounds": agent_sounds,
                "total_messages": total_messages,
                "returned_messages": len(messages),
                "has_more": total_messages > len(messages),
            }

        # ---------------------------------------------------------------
        # Login / logout for the viewer. MailUiAuthMiddleware lets exactly
        # these two paths through without a session; everything else under
        # /mail is gated. See mcp_agent_mail.webauth for the primitives.
        # ---------------------------------------------------------------

        def _safe_next(raw: str) -> str:
            """Redirect sign-in only to the shell or an enumerated bookmark.

            The latter returns through the authenticated middleware, which
            performs the project/message RBAC lookup before redirecting to a
            typed hash route. Unmapped retired routes never become post-login
            compatibility surfaces.
            """
            candidate = (raw or "").strip()
            if (
                not candidate.startswith("/")
                or candidate.startswith("//")
                or "\\" in candidate
                or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
            ):
                return "/mail"
            parsed = urlsplit(candidate)
            if parsed.scheme or parsed.netloc:
                return "/mail"
            if parsed.path in {"/mail", "/mail/"}:
                return urlunsplit(("", "", "/mail", parsed.query, parsed.fragment))
            if parsed.fragment:
                return "/mail"
            decoded_path = _mail_ui_decode_raw_path(parsed.path)
            if decoded_path is None:
                return "/mail"
            bookmark = _mail_ui_canonical_legacy_bookmark(
                raw_path=parsed.path,
                decoded_path=decoded_path,
            )
            if bookmark is None:
                return "/mail"
            try:
                query_items = parse_qsl(
                    parsed.query,
                    keep_blank_values=True,
                    strict_parsing=False,
                    max_num_fields=4,
                )
            except ValueError:
                return "/mail"
            if bookmark["kind"] in {"project", "search"}:
                if (
                    _mail_ui_legacy_search_hash(
                        bookmark=bookmark,
                        project_id=1,
                        query_items=query_items,
                    )
                    is None
                ):
                    return "/mail"
            elif query_items:
                return "/mail"
            return urlunsplit(("", "", parsed.path, parsed.query, ""))

        async def _render_mail_login(
            request: Request,
            *,
            error: str | None,
            next_url: str,
            requested_locale: str | None,
            status_code: int = status.HTTP_200_OK,
        ) -> HTMLResponse:
            locale = _mail_login_locale(
                requested_locale,
                request.headers.get("accept-language", ""),
            )
            response = await _render(
                "mail_login.html",
                error=error,
                next_url=next_url,
                status_code=status_code,
                **_mail_login_context(locale, next_url),
            )
            response.headers.update(_MAIL_LOGIN_HTML_HEADERS)
            return response

        @fastapi_app.get(_MAIL_LOGIN_PATH, response_class=HTMLResponse)
        async def mail_login_form(request: Request) -> HTMLResponse:
            cfg = settings.mail_ui
            # Already signed in? Don't show the form again.
            token = request.cookies.get(cfg.cookie_name, "")
            if token and await _load_session_user(token, settings=settings):
                return HTMLResponse(
                    "", status_code=status.HTTP_303_SEE_OTHER,
                    headers={
                        **_MAIL_LOGIN_HTML_HEADERS,
                        "Location": _safe_next(request.query_params.get("next", "/mail")),
                    },
                )
            return await _render_mail_login(
                request,
                error=None,
                next_url=_safe_next(request.query_params.get("next", "/mail")),
                requested_locale=request.query_params.get("lang"),
            )

        @fastapi_app.post(_MAIL_LOGIN_PATH)
        async def mail_login_submit(request: Request) -> Response:
            cfg = settings.mail_ui
            form = await request.form()
            username = str(form.get("username", "")).strip()
            password = str(form.get("password", ""))
            next_url = _safe_next(str(form.get("next", "/mail")))
            submitted_locale = form.get("lang")
            requested_locale = (
                str(submitted_locale)
                if submitted_locale is not None
                else request.query_params.get("lang")
            )
            locale = _mail_login_locale(
                requested_locale,
                request.headers.get("accept-language", ""),
            )

            # The login form is the one unauthenticated POST under /mail, so the
            # middleware's same-origin check has not run for it. Do it here.
            if not webauth.same_origin(
                request.headers.get("origin", ""),
                request.headers.get("referer", ""),
                request.headers.get("host", ""),
                expected_scheme=request.url.scheme,
            ):
                return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)

            client_ip = request.client.host if request.client else "-"
            throttle_key = f"{client_ip}\0{username.casefold()[:64]}"
            if _login_throttled(throttle_key):
                return await _render_mail_login(
                    request,
                    error=_MAIL_LOGIN_TEXT[locale].throttled,
                    next_url=next_url,
                    requested_locale=locale.value,
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            stored: str | None = None
            row_epoch = 1
            row_generation = ""
            row_disabled = False
            if webauth.valid_username(username):
                from sqlmodel import select as _select

                from .models import UiUser as _UiUser

                async with get_session() as s_login:
                    res = await s_login.execute(_select(_UiUser).where(_UiUser.username == username))
                    row = res.scalars().first()
                    if row is not None:
                        stored = row.password_hash
                        row_epoch = row.session_epoch
                        row_generation = row.session_generation
                        row_disabled = row.disabled

            # authenticate() runs a dummy scrypt for an unknown user, so a bad
            # username and a bad password take the same time and cannot be told
            # apart. A disabled account is checked after, and reports the same
            # generic failure for the same reason.
            if not webauth.authenticate(username, password, stored) or row_disabled:
                _login_record_failure(throttle_key)
                structlog.get_logger("mail_ui").info(
                    "mail_ui.login_failed", username=username[:64], client=client_ip
                )
                return await _render_mail_login(
                    request,
                    error=_MAIL_LOGIN_TEXT[locale].invalid_credentials,
                    next_url=next_url,
                    requested_locale=locale.value,
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )

            _login_clear_failures(throttle_key)
            import time as _time

            token = webauth.make_session(
                username,
                epoch=row_epoch,
                generation=row_generation,
                now=_time.time(),
                secret=cfg.session_secret.encode("utf-8"),
                ttl=float(cfg.session_ttl_seconds),
            )
            async with get_session() as s_touch:
                from .models import UiUser as _UiUser2

                touch_result = await s_touch.execute(
                    update(_UiUser2)
                    .where(cast(Any, _UiUser2.username == username))
                    .where(cast(Any, _UiUser2.session_epoch == row_epoch))
                    .where(cast(Any, _UiUser2.session_generation == row_generation))
                    .where(cast(Any, _UiUser2.disabled == False))  # noqa: E712
                    .values(
                        last_login_ts=datetime.now(timezone.utc).replace(tzinfo=None),
                        preferred_ui_locale=locale.value,
                    )
                )
                if int(getattr(touch_result, "rowcount", 0) or 0) != 1:
                    await s_touch.rollback()
                    structlog.get_logger("mail_ui").warning(
                        "mail_ui.login_lifetime_changed",
                        username=username[:64],
                        client=client_ip,
                    )
                    return await _render_mail_login(
                        request,
                        error=_MAIL_LOGIN_TEXT[locale].invalid_credentials,
                        next_url=next_url,
                        requested_locale=locale.value,
                        status_code=status.HTTP_401_UNAUTHORIZED,
                    )
                await s_touch.commit()

            response = Response(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={**_MAIL_LOGIN_HTML_HEADERS, "Location": next_url},
            )
            _set_mail_ui_session_cookie(response, token=token, settings=settings)
            structlog.get_logger("mail_ui").info("mail_ui.login_ok", username=username, client=client_ip)
            return response

        @fastapi_app.post(_MAIL_LOGOUT_PATH)
        async def mail_logout(request: Request) -> Response:
            if not webauth.same_origin(
                request.headers.get("origin", ""),
                request.headers.get("referer", ""),
                request.headers.get("host", ""),
                expected_scheme=request.url.scheme,
            ):
                return JSONResponse(
                    {"detail": "Cross-origin request rejected"},
                    status_code=status.HTTP_403_FORBIDDEN,
                    headers=_MAIL_LEGACY_HTML_HEADERS,
                )
            cfg = settings.mail_ui
            response = Response(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={**_MAIL_LEGACY_HTML_HEADERS, "Location": _MAIL_LOGIN_PATH},
            )
            response.delete_cookie(cfg.cookie_name, path="/mail")
            return response

        @fastapi_app.api_route(
            _MAIL_LOGOUT_PATH,
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
        async def mail_logout_method_not_allowed() -> Response:
            return Response(
                status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
                headers={**_MAIL_LEGACY_HTML_HEADERS, "Allow": "POST"},
            )

        @fastapi_app.api_route(
            "/mail",
            methods=["GET", "HEAD"],
            response_class=HTMLResponse,
            include_in_schema=False,
        )
        async def mail_react_index() -> FileResponse:
            """Serve the sole authenticated human interface."""
            return _mail_react_index_response()

        @fastapi_app.get("/mail/events")
        async def mail_events_stream(request: Request) -> Any:
            """Tell the open viewer when to refetch, so it stops needing F5.

            Under `/mail/`, so the session cookie the viewer already holds is
            the whole authentication story — which is also why the page can use
            a plain `EventSource`. The agent-facing `/events` needs a
            registration token in a header, and a browser cannot set one; that
            endpoint authenticates *as an agent*, and a person at a keyboard is
            not one.

            Frames carry `{"kind":"changed","project":…}` and nothing else. No
            addressee, no message id, no subject. The page reacts by refetching
            through the ordinary viewer API, which applies exactly the
            authorisation an F5 would — so this says *when* to look, never
            *what* is there, and cannot become a second answer to who may see
            what. That matters most for blind copies: a project-wide feed
            carrying per-recipient frames would end BCC blindness for anyone
            with a viewer session.
            """
            # No project means "any" — the index and the unified inbox span all
            # of them, and those are the pages a person opens first. The frame is
            # the same either way and carries nothing, so the wider scope reveals
            # nothing wider.
            project_key = (request.query_params.get("project") or "").strip()
            session_token = request.cookies.get(settings.mail_ui.cookie_name, "")
            stream_principal = _mail_ui_request_user(request)
            project_lifetimes: dict[str, tuple[int, str]]
            if project_key:
                await ensure_schema()
                async with get_session() as session:
                    row = await _resolve_mail_project(session, project_key)
                    if row is None:
                        return JSONResponse({"detail": "Project not found"}, status_code=404)
                    await _mail_ui_require_project_access(
                        settings=settings,
                        request=request,
                        session=session,
                        project_id=int(row[0]),
                    )
                project_slug = str(row[1])
                project_lifetimes = {
                    project_slug: (int(row[0]), str(row[4])),
                }
                if not await _mail_ui_stream_access_valid(
                    settings=settings,
                    session_token=session_token,
                    project_slug=project_slug,
                    expected_principal=stream_principal,
                    expected_project_id=int(row[0]),
                    expected_project_generation=str(row[4]),
                ):
                    return JSONResponse({"detail": "Forbidden"}, status_code=403)
                queue = hub.subscribe_project(project_slug, str(row[4]))
            else:
                if not await _mail_ui_stream_access_valid(
                    settings=settings,
                    session_token=session_token,
                    project_slug=None,
                    expected_principal=stream_principal,
                ):
                    return JSONResponse({"detail": "Forbidden"}, status_code=403)
                project_slug = None
                project_lifetimes = await _mail_ui_stream_project_lifetimes(
                    settings=settings,
                    request=request,
                )
                queue = hub.subscribe_projects(
                    (slug, generation)
                    for slug, (_project_id, generation) in project_lifetimes.items()
                )

            async def stream() -> Any:
                deadline = asyncio.get_running_loop().time() + MAX_STREAM_SECONDS
                try:
                    yield b": ready\n\n"
                    while True:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            return
                        try:
                            event = await asyncio.wait_for(
                                queue.get(), timeout=min(KEEPALIVE_SECONDS, remaining)
                            )
                        except asyncio.TimeoutError:
                            keepalive_lifetime = (
                                project_lifetimes.get(project_slug)
                                if project_slug is not None
                                else None
                            )
                            if not await _mail_ui_stream_access_valid(
                                settings=settings,
                                session_token=session_token,
                                project_slug=project_slug,
                                expected_principal=stream_principal,
                                expected_project_id=(
                                    keepalive_lifetime[0]
                                    if keepalive_lifetime is not None
                                    else None
                                ),
                                expected_project_generation=(
                                    keepalive_lifetime[1]
                                    if keepalive_lifetime is not None
                                    else None
                                ),
                            ):
                                return
                            yield b": ping\n\n"
                            continue
                        event_project = event.get("project") if isinstance(event, dict) else None
                        if not isinstance(event_project, str):
                            return
                        event_scope = event_project if project_slug is None else project_slug
                        expected_lifetime = project_lifetimes.get(event_scope)
                        event_is_visible = await _mail_ui_stream_access_valid(
                            settings=settings,
                            session_token=session_token,
                            project_slug=event_scope,
                            expected_principal=stream_principal,
                            expected_project_id=(
                                expected_lifetime[0]
                                if expected_lifetime is not None
                                else None
                            ),
                            expected_project_generation=(
                                expected_lifetime[1]
                                if expected_lifetime is not None
                                else None
                            ),
                        )
                        if not event_is_visible:
                            if project_slug is None and await _mail_ui_stream_access_valid(
                                settings=settings,
                                session_token=session_token,
                                project_slug=None,
                                expected_principal=stream_principal,
                            ):
                                continue
                            return
                        # Unlike the agent stream this does NOT close after one
                        # frame: a page stays open and would otherwise have to
                        # reconnect after every message it displays.
                        yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
                finally:
                    if project_slug is None:
                        hub.unsubscribe_projects(
                            (
                                (slug, generation)
                                for slug, (_project_id, generation) in project_lifetimes.items()
                            ),
                            queue,
                        )
                    else:
                        _project_id, project_generation = project_lifetimes[project_slug]
                        hub.unsubscribe_project(
                            project_slug,
                            project_generation,
                            queue,
                        )

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-store",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        @fastapi_app.get("/mail/api/unified-inbox", response_class=JSONResponse)
        async def mail_unified_inbox_api(
            request: Request,
            limit: int = 50000,
            include_projects: bool = False,
        ) -> JSONResponse:
            """Return a bounded message page plus the unbounded inbox total."""

            payload = await _build_unified_inbox_payload(
                request=request,
                limit=limit,
                include_projects=include_projects,
            )
            if not include_projects:
                # Reduce payload size when polling for message updates only
                payload["projects"] = []
            return JSONResponse(payload)

        @fastapi_app.get(
            "/mail/api/v1/projects",
            response_model=MailUiProjectsResponse,
        )
        async def mail_ui_projects_v1(request: Request) -> MailUiProjectsResponse:
            """Return only projects visible to the authenticated human."""
            await ensure_schema()
            async with get_session() as session:
                visible_roles = await _mail_ui_visible_project_roles(
                    settings=settings,
                    request=request,
                    session=session,
                )
                visible_ids = sorted(visible_roles)
                predicate, parameters = _mail_ui_visible_project_predicate(
                    visible_ids,
                    column="p.id",
                    parameter_prefix="projects_v1_pid",
                )
                rows = await session.execute(
                    text(
                        "SELECT p.id, p.slug, p.human_key, p.created_at, p.archived_at "
                        "FROM projects p "
                        f"WHERE {predicate} "
                        "ORDER BY p.created_at DESC, p.id DESC"
                    ),
                    parameters,
                )
                items: list[MailUiProjectSummary] = []
                for row in rows.mappings().all():
                    project_id = int(row["id"])
                    access = _mail_ui_access_context(
                        settings=settings,
                        request=request,
                        project_id=project_id,
                        project_role=visible_roles[project_id],
                    )
                    raw_role = "admin" if access["is_admin"] else access["project_role"]
                    if raw_role not in {"admin", "viewer", "operator"}:
                        raise RuntimeError("visible project has no valid UI role")
                    items.append(
                        MailUiProjectSummary(
                            id=project_id,
                            slug=str(row["slug"]),
                            human_key=str(row["human_key"]),
                            created_at=_mail_ui_datetime(row["created_at"]),
                            archived_at=(
                                _mail_ui_datetime(row["archived_at"])
                                if row["archived_at"] is not None
                                else None
                            ),
                            role=cast(MailUiProjectRole, raw_role),
                            can_reply=bool(access["can_reply"]),
                        )
                    )
            return MailUiProjectsResponse(items=items, total=len(items))

        @fastapi_app.get(
            "/mail/api/v1/projects/{project_id}/agents",
            response_model=MailUiProjectAgentsResponse,
            responses=_MAIL_UI_DELIVERY_ERROR_RESPONSES,
        )
        async def mail_ui_project_agents_v1(
            project_id: Annotated[int, FastApiPath(gt=0)],
            request: Request,
        ) -> MailUiProjectAgentsResponse:
            """Return the active, addressable agents in one active project."""
            await ensure_schema()
            async with get_session() as session:
                await _mail_ui_revalidated_admin_user(request, session)
                visible_roles = await _mail_ui_visible_project_roles(
                    settings=settings,
                    request=request,
                    session=session,
                )
                project = await session.get(Project, project_id)
                if (
                    project_id not in visible_roles
                    or project is None
                    or project.archived_at is not None
                ):
                    raise _mail_ui_domain_http_exception(
                        code="project_not_found",
                        status_code=status.HTTP_404_NOT_FOUND,
                    )
                rows = await session.execute(
                    text(
                        "SELECT id, agent_generation, name, display_name, notify_sound "
                        "FROM agents "
                        "WHERE project_id = :project_id AND retired_at IS NULL "
                        "AND name <> :human_overseer "
                        "AND contact_policy <> 'block_all' "
                        "ORDER BY lower(name), name, id"
                    ),
                    {
                        "project_id": project_id,
                        "human_overseer": "HumanOverseer",
                    },
                )
                items = [
                    MailUiAgentDirectoryItem(
                        agent_id=int(row["id"]),
                        agent_generation=str(row["agent_generation"]),
                        name=str(row["name"]),
                        display_name=(
                            str(row["display_name"])
                            if row["display_name"] is not None
                            else None
                        ),
                        notify_sound=(
                            str(row["notify_sound"])
                            if row["notify_sound"] is not None
                            else None
                        ),
                    )
                    for row in rows.mappings().all()
                ]
            return MailUiProjectAgentsResponse(
                project_id=project_id,
                project_generation=project.project_generation,
                items=items,
                total=len(items),
            )

        @fastapi_app.get(
            "/mail/api/v1/inbox",
            response_model=MailUiInboxResponse,
        )
        async def mail_ui_inbox_v1(
            request: Request,
            project_id: int | None = Query(default=None, gt=0),
            limit: int = Query(default=50, ge=1, le=100),
            cursor: str | None = Query(
                default=None,
                min_length=1,
                max_length=_MAIL_UI_CURSOR_MAX_LENGTH,
            ),
        ) -> MailUiInboxResponse:
            """Return a keyset-paginated inbox without bodies or recipients."""
            cursor_key = _mail_ui_decode_cursor(cursor) if cursor is not None else None
            await ensure_schema()
            async with get_session() as session:
                visible_roles = await _mail_ui_visible_project_roles(
                    settings=settings,
                    request=request,
                    session=session,
                )
                if project_id is not None:
                    if project_id not in visible_roles:
                        raise HTTPException(status_code=404, detail="Project not found")
                    visible_ids = [project_id]
                else:
                    visible_ids = sorted(visible_roles)

                predicate, parameters = _mail_ui_visible_project_predicate(
                    visible_ids,
                    column="m.project_id",
                    parameter_prefix="inbox_v1_pid",
                )
                total_result = await session.execute(
                    text(f"SELECT COUNT(*) FROM messages m WHERE {predicate}"),
                    parameters,
                )
                total = int(total_result.scalar_one())

                cursor_predicate = ""
                page_parameters: dict[str, Any] = {**parameters, "page_limit": limit + 1}
                if cursor_key is not None:
                    cursor_created_ts, cursor_message_id = cursor_key
                    cursor_predicate = (
                        f" AND ({_MAIL_UI_CREATED_TS_KEY_SQL} < :cursor_created_ts "
                        f"OR ({_MAIL_UI_CREATED_TS_KEY_SQL} = :cursor_created_ts "
                        "AND m.id < :cursor_message_id))"
                    )
                    page_parameters.update(
                        {
                            "cursor_created_ts": cursor_created_ts,
                            "cursor_message_id": cursor_message_id,
                        }
                    )

                rows = await session.execute(
                    text(
                        "SELECT m.id, m.project_id, p.slug AS project_slug, m.subject, "
                        "m.importance, m.ack_required, m.thread_id, m.reply_to, "
                        f"m.created_ts, {_MAIL_UI_CREATED_TS_KEY_SQL} AS cursor_created_ts, "
                        "sender.name AS sender_name, sender.display_name AS sender_display_name, "
                        "sender.project_id AS sender_project_id, "
                        "sender_project.slug AS sender_project_slug "
                        "FROM messages m "
                        "JOIN projects p ON p.id = m.project_id "
                        "JOIN agents sender ON sender.id = m.sender_id "
                        "LEFT JOIN projects sender_project ON sender_project.id = sender.project_id "
                        f"WHERE {predicate}{cursor_predicate} "
                        f"ORDER BY {_MAIL_UI_CREATED_TS_KEY_SQL} DESC, m.id DESC "
                        "LIMIT :page_limit"
                    ),
                    page_parameters,
                )
                page_rows = list(rows.mappings().all())
                has_more = len(page_rows) > limit
                response_rows = page_rows[:limit]
                items = [
                    _mail_ui_message_summary_from_row(
                        row,
                        settings=settings,
                        request=request,
                        visible_roles=visible_roles,
                    )
                    for row in response_rows
                ]
                next_cursor = (
                    _mail_ui_encode_cursor(
                        str(response_rows[-1]["cursor_created_ts"]),
                        int(response_rows[-1]["id"]),
                    )
                    if has_more and response_rows
                    else None
                )
            return MailUiInboxResponse(
                items=items,
                total=total,
                next_cursor=next_cursor,
            )

        @fastapi_app.get(
            _MAIL_SEARCH_API_PATH,
            response_model=MailUiSearchResponse,
            responses=_MAIL_UI_SEARCH_ERROR_RESPONSES,
        )
        async def mail_ui_search_v1(
            request: Request,
            q: Annotated[str, Query(min_length=1, max_length=256)],
            project_id: Annotated[int | None, Query(gt=0)] = None,
            scope: Annotated[MailUiSearchScope, Query()] = "all",
            order: Annotated[MailUiSearchOrder, Query()] = "relevance",
            limit: Annotated[int, Query(ge=1, le=100)] = 50,
            cursor: Annotated[
                str | None,
                Query(min_length=1, max_length=_MAIL_UI_CURSOR_MAX_LENGTH),
            ] = None,
        ) -> MailUiSearchResponse:
            """Search only visible messages through bounded SQLite FTS5."""
            fts_query, ranking_terms = _mail_ui_compile_search_query(q, scope)
            fingerprint = _mail_ui_search_fingerprint(
                fts_query=fts_query,
                project_id=project_id,
                scope=scope,
                order=order,
            )
            cursor_key = (
                _mail_ui_decode_search_cursor(
                    cursor,
                    expected_fingerprint=fingerprint,
                    order=order,
                )
                if cursor is not None
                else None
            )
            try:
                await ensure_schema()
                async with get_session() as session:
                    visible_roles = await _mail_ui_visible_project_roles(
                        settings=settings,
                        request=request,
                        session=session,
                    )
                    if project_id is not None:
                        if project_id not in visible_roles:
                            raise HTTPException(
                                status_code=status.HTTP_404_NOT_FOUND,
                                detail={"code": "project_not_found"},
                            )
                        visible_ids = [project_id]
                    else:
                        visible_ids = sorted(visible_roles)

                    predicate, parameters = _mail_ui_visible_project_predicate(
                        visible_ids,
                        column="m.project_id",
                        parameter_prefix="search_v1_pid",
                    )
                    rank_expression, rank_parameters = _mail_ui_local_search_rank(
                        ranking_terms
                    )
                    cursor_predicate = ""
                    page_parameters: dict[str, Any] = {
                        **parameters,
                        **rank_parameters,
                        "fts_query": fts_query,
                        "page_limit": limit + 1,
                    }
                    if cursor_key is not None:
                        cursor_rank, cursor_created_ts, cursor_message_id = cursor_key
                        page_parameters.update(
                            {
                                "cursor_created_ts": cursor_created_ts,
                                "cursor_message_id": cursor_message_id,
                            }
                        )
                        time_keyset = (
                            f"({_MAIL_UI_CREATED_TS_KEY_SQL} < :cursor_created_ts "
                            f"OR ({_MAIL_UI_CREATED_TS_KEY_SQL} = :cursor_created_ts "
                            "AND m.id < :cursor_message_id))"
                        )
                        if order == "relevance":
                            page_parameters["cursor_rank"] = cursor_rank
                            cursor_predicate = (
                                f" AND ({rank_expression} > :cursor_rank "
                                f"OR ({rank_expression} = :cursor_rank AND {time_keyset}))"
                            )
                        else:
                            cursor_predicate = f" AND {time_keyset}"

                    ordering = (
                        f"{rank_expression} ASC, {_MAIL_UI_CREATED_TS_KEY_SQL} DESC, "
                        "m.id DESC"
                        if order == "relevance"
                        else f"{_MAIL_UI_CREATED_TS_KEY_SQL} DESC, m.id DESC"
                    )
                    rows = await session.execute(
                        text(
                            "SELECT m.id, m.project_id, p.slug AS project_slug, m.subject, "
                            "m.importance, m.ack_required, m.thread_id, m.reply_to, "
                            f"m.created_ts, {_MAIL_UI_CREATED_TS_KEY_SQL} AS cursor_created_ts, "
                            f"{rank_expression} AS search_rank, "
                            "snippet(fts_messages, -1, '', '', '…', 24) AS search_snippet, "
                            "sender.name AS sender_name, "
                            "sender.display_name AS sender_display_name, "
                            "sender.project_id AS sender_project_id, "
                            "sender_project.slug AS sender_project_slug "
                            "FROM fts_messages "
                            "JOIN messages m ON m.id = fts_messages.rowid "
                            "JOIN projects p ON p.id = m.project_id "
                            "JOIN agents sender ON sender.id = m.sender_id "
                            "LEFT JOIN projects sender_project "
                            "ON sender_project.id = sender.project_id "
                            f"WHERE {predicate} AND fts_messages MATCH :fts_query"
                            f"{cursor_predicate} ORDER BY {ordering} LIMIT :page_limit"
                        ),
                        page_parameters,
                    )
                    page_rows = list(rows.mappings().all())
                    has_more = len(page_rows) > limit
                    response_rows = page_rows[:limit]
                    items: list[MailUiSearchItem] = []
                    for row in response_rows:
                        summary = _mail_ui_message_summary_from_row(
                            row,
                            settings=settings,
                            request=request,
                            visible_roles=visible_roles,
                        )
                        items.append(
                            MailUiSearchItem(
                                **summary.model_dump(),
                                snippet=_mail_ui_plain_search_snippet(
                                    row["search_snippet"]
                                ),
                            )
                        )
                    next_cursor = (
                        _mail_ui_encode_search_cursor(
                            fingerprint=fingerprint,
                            created_ts_key=str(
                                response_rows[-1]["cursor_created_ts"]
                            ),
                            message_id=int(response_rows[-1]["id"]),
                            rank=(
                                float(response_rows[-1]["search_rank"])
                                if order == "relevance"
                                else None
                            ),
                        )
                        if has_more and response_rows
                        else None
                    )
            except HTTPException:
                raise
            except Exception as exc:
                structlog.get_logger("mail_ui").warning(
                    "mail_ui.search_unavailable",
                    error_type=type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": "search_unavailable"},
                ) from None
            return MailUiSearchResponse(items=items, next_cursor=next_cursor)

        @fastapi_app.get(
            "/mail/api/v1/projects/{project_id}/messages/{message_id}",
            response_model=MailUiMessageDetail,
        )
        async def mail_ui_message_v1(
            project_id: int,
            message_id: int,
            request: Request,
        ) -> MailUiMessageDetail:
            """Return one visible message with TO/CC but never BCC recipients."""
            await ensure_schema()
            async with get_session() as session:
                visible_roles = await _mail_ui_visible_project_roles(
                    settings=settings,
                    request=request,
                    session=session,
                )
                if project_id not in visible_roles:
                    raise HTTPException(status_code=404, detail="Project not found")
                row = (
                    await session.execute(
                        text(
                            "SELECT m.id, m.project_id, p.slug AS project_slug, m.subject, "
                            "m.body_md, m.importance, m.ack_required, m.thread_id, m.reply_to, "
                            "m.created_ts, m.attachments, "
                            "coalesce(delivery.sender_name_snapshot, sender.name) AS sender_name, "
                            "CASE WHEN delivery.id IS NULL OR ("
                            "sender.id = delivery.sender_id "
                            "AND sender.agent_generation = delivery.sender_generation_snapshot "
                            "AND sender.project_id = delivery.sender_project_id_snapshot) "
                            "THEN sender.display_name ELSE NULL END AS sender_display_name, "
                            "coalesce(delivery.sender_project_id_snapshot, sender.project_id) "
                            "AS sender_project_id, "
                            "coalesce(delivery.sender_project_slug_snapshot, sender_project.slug) "
                            "AS sender_project_slug, "
                            "delivery.sender_id AS reply_target_agent_id, "
                            "delivery.sender_name_snapshot AS reply_target_agent_name, "
                            "delivery.sender_generation_snapshot "
                            "AS reply_target_agent_generation, "
                            "delivery.sender_project_id_snapshot AS reply_target_project_id, "
                            "delivery.sender_project_slug_snapshot AS reply_target_project_slug, "
                            "delivery.sender_project_generation_snapshot "
                            "AS reply_target_project_generation, "
                            "CASE WHEN delivery.id IS NOT NULL "
                            "AND sender.id = delivery.sender_id "
                            "AND sender.name = delivery.sender_name_snapshot "
                            "AND sender.agent_generation = delivery.sender_generation_snapshot "
                            "AND sender.project_id = delivery.sender_project_id_snapshot "
                            "AND sender.retired_at IS NULL "
                            "AND sender.contact_policy != 'block_all' "
                            "AND sender_project.id = delivery.sender_project_id_snapshot "
                            "AND sender_project.slug = delivery.sender_project_slug_snapshot "
                            "AND sender_project.project_generation = "
                            "delivery.sender_project_generation_snapshot "
                            "AND sender_project.archived_at IS NULL "
                            "THEN 1 ELSE 0 END AS reply_target_available "
                            "FROM messages m "
                            "JOIN projects p ON p.id = m.project_id "
                            "LEFT JOIN message_deliveries delivery "
                            "ON delivery.id = m.delivery_id "
                            "AND delivery.state = 'published' "
                            "AND delivery.message_id = m.id "
                            "AND delivery.project_id = m.project_id "
                            "AND delivery.project_generation_snapshot = p.project_generation "
                            "LEFT JOIN agents sender ON sender.id = m.sender_id "
                            "LEFT JOIN projects sender_project ON sender_project.id = sender.project_id "
                            "WHERE m.project_id = :project_id AND m.id = :message_id"
                        ),
                        {"project_id": project_id, "message_id": message_id},
                    )
                ).mappings().first()
                if row is None:
                    raise HTTPException(status_code=404, detail="Message not found")
                recipients = await _mail_ui_safe_recipient_map(session, [message_id])
                return _mail_ui_message_detail_from_row(
                    row,
                    recipients=recipients[message_id],
                    settings=settings,
                    request=request,
                    visible_roles=visible_roles,
                )

        @fastapi_app.post(
            "/mail/api/v1/projects/{project_id}/messages",
            response_model=MailUiDeliveryResponse,
            responses=_MAIL_UI_DELIVERY_MUTATION_ERROR_RESPONSES,
        )
        @_mail_ui_typed_delivery_endpoint
        async def mail_ui_compose_v1(
            project_id: Annotated[int, FastApiPath(gt=0)],
            request: Request,
            message: MailUiComposeRequest,
        ) -> MailUiDeliveryResponse:
            """Accept one administrator-authored message as a durable intent."""
            await ensure_schema()
            async with get_immediate_session() as session:
                project_snapshot, sender, actor, locale = await _mail_ui_delivery_context(
                    session=session,
                    request=request,
                    project_id=project_id,
                    compose=True,
                )
                if project_snapshot.generation != message.expected_project_generation:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={"code": "project_recreated"},
                    )
                recipient_ids = [recipient.agent_id for recipient in message.recipients]
                recipient_rows = await session.execute(
                    select(Agent).where(
                        cast(Any, Agent.project_id == project_id),
                        col(Agent.id).in_(recipient_ids),
                        col(Agent.provisioning_state) == "active",
                        col(Agent.retired_at).is_(None),
                        col(Agent.contact_policy) != "block_all",
                        col(Agent.name) != "HumanOverseer",
                    )
                )
                agents_by_id = {
                    int(agent.id): agent
                    for agent in recipient_rows.scalars().all()
                    if agent.id is not None
                }
                if set(agents_by_id) != set(recipient_ids):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={"code": "recipient_unavailable"},
                    )
                recipients_list: list[DeliveryRecipientSnapshot] = []
                for recipient_ref in message.recipients:
                    agent = agents_by_id[recipient_ref.agent_id]
                    if agent.agent_generation != recipient_ref.expected_agent_generation:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={"code": "recipient_unavailable"},
                        )
                    recipients_list.append(
                        await _mail_ui_delivery_recipient(
                            session=session,
                            agent=agent,
                        )
                    )
                recipients = tuple(recipients_list)
                await session.commit()

            delivery_request = MessageDeliveryRequest(
                target_project=project_snapshot,
                sender=sender,
                actor=actor,
                recipients=recipients,
                idempotency_key=message.idempotency_key,
                subject=message.subject,
                body_md=_mail_ui_overseer_preamble(locale) + message.body_md,
                purpose="message",
                thread_id=message.thread_id,
                importance="high",
            )
            try:
                acceptance = await accept_message_delivery(delivery_request)
                processing = await process_message_delivery(acceptance.delivery_id)
            except MessageDeliveryServiceError as exc:
                raise _mail_ui_delivery_exception(exc) from None
            if processing.published_now:
                await emit_published_delivery_notifications(processing.delivery_id)
            return _mail_ui_delivery_response(acceptance, processing)

        @fastapi_app.post(
            "/mail/api/v1/projects/{project_id}/messages/{message_id}/replies",
            response_model=MailUiDeliveryResponse,
            responses=_MAIL_UI_DELIVERY_MUTATION_ERROR_RESPONSES,
        )
        @_mail_ui_typed_delivery_endpoint
        async def mail_ui_reply_v1(
            project_id: Annotated[int, FastApiPath(gt=0)],
            message_id: Annotated[int, FastApiPath(gt=0)],
            request: Request,
            reply: MailUiReplyRequest,
        ) -> MailUiDeliveryResponse:
            """Reply through server-derived routing and immutable thread provenance."""
            await ensure_schema()
            async with get_immediate_session() as session:
                source_project, sender, actor, locale = await _mail_ui_delivery_context(
                    session=session,
                    request=request,
                    project_id=project_id,
                    compose=False,
                )
                original = await session.get(Message, message_id)
                if original is None or original.project_id != project_id:
                    raise HTTPException(status_code=404, detail={"code": "message_not_found"})
                original_delivery = (
                    await session.get(MessageDelivery, original.delivery_id)
                    if original.delivery_id is not None
                    else None
                )
                if (
                    original_delivery is None
                    or original_delivery.state != "published"
                    or original_delivery.message_id != message_id
                    or original_delivery.project_id != source_project.project_id
                    or not hmac.compare_digest(
                        original_delivery.project_generation_snapshot,
                        source_project.generation,
                    )
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "reply_provenance_unavailable"},
                    )
                expected_target = (
                    reply.expected_sender_agent_id,
                    reply.expected_sender_agent_generation,
                    reply.expected_sender_project_id,
                    reply.expected_sender_project_generation,
                )
                immutable_target = (
                    original_delivery.sender_id,
                    original_delivery.sender_generation_snapshot,
                    original_delivery.sender_project_id_snapshot,
                    original_delivery.sender_project_generation_snapshot,
                )
                if (
                    expected_target[0] != immutable_target[0]
                    or expected_target[2] != immutable_target[2]
                    or not hmac.compare_digest(expected_target[1], immutable_target[1])
                    or not hmac.compare_digest(expected_target[3], immutable_target[3])
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "reply_target_changed"},
                    )
                recipient_agent = await session.get(Agent, original_delivery.sender_id)
                target_project_row = await session.get(
                    Project,
                    original_delivery.sender_project_id_snapshot,
                )
                if (
                    recipient_agent is None
                    or target_project_row is None
                    or recipient_agent.name == "HumanOverseer"
                    or recipient_agent.name != original_delivery.sender_name_snapshot
                    or recipient_agent.project_id
                    != original_delivery.sender_project_id_snapshot
                    or not hmac.compare_digest(
                        recipient_agent.agent_generation,
                        original_delivery.sender_generation_snapshot,
                    )
                    or target_project_row.slug
                    != original_delivery.sender_project_slug_snapshot
                    or not hmac.compare_digest(
                        target_project_row.project_generation,
                        original_delivery.sender_project_generation_snapshot,
                    )
                    or recipient_agent.retired_at is not None
                    or recipient_agent.contact_policy == "block_all"
                    or target_project_row.archived_at is not None
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "reply_target_unavailable"},
                    )
                recipient = await _mail_ui_delivery_recipient(
                    session=session,
                    agent=recipient_agent,
                )
                target_project = recipient.agent.project
                thread_id = original_delivery.thread_id or str(original.id)
                original_subject = original_delivery.subject or ""
                subject = (
                    original_subject
                    if original_subject.casefold().startswith("re:")
                    else f"Re: {original_subject}"
                )[:200]
                reply_to_message_id = (
                    message_id
                    if target_project.project_id == source_project.project_id
                    else None
                )
                await session.commit()

            delivery_request = MessageDeliveryRequest(
                target_project=target_project,
                sender=sender,
                actor=actor,
                recipients=(recipient,),
                idempotency_key=reply.idempotency_key,
                subject=subject,
                body_md=_mail_ui_overseer_preamble(locale) + reply.body_md,
                purpose="reply",
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                importance="high",
            )
            try:
                acceptance = await accept_message_delivery(delivery_request)
                processing = await process_message_delivery(acceptance.delivery_id)
            except MessageDeliveryServiceError as exc:
                raise _mail_ui_delivery_exception(exc) from None
            if processing.published_now:
                await emit_published_delivery_notifications(processing.delivery_id)
            return _mail_ui_delivery_response(acceptance, processing)

        @fastapi_app.get(
            "/mail/api/v1/deliveries/{delivery_id}",
            response_model=MailUiDeliveryResponse,
            responses=_MAIL_UI_DELIVERY_ERROR_RESPONSES,
        )
        @_mail_ui_typed_delivery_endpoint
        async def mail_ui_delivery_status_v1(
            delivery_id: str,
            request: Request,
        ) -> MailUiDeliveryResponse:
            """Poll only a delivery owned by the current account lifetime."""
            await ensure_schema()
            await _mail_ui_require_owned_delivery(
                request=request,
                delivery_id=delivery_id,
            )
            try:
                processing = await get_message_delivery_status(delivery_id)
            except MessageDeliveryNotFoundError as exc:
                raise _mail_ui_delivery_exception(exc) from None
            return _mail_ui_delivery_status_response(processing)

        @fastapi_app.post(
            "/mail/api/v1/deliveries/{delivery_id}/retry",
            response_model=MailUiDeliveryResponse,
            responses=_MAIL_UI_DELIVERY_MUTATION_ERROR_RESPONSES,
        )
        @_mail_ui_typed_delivery_endpoint
        async def mail_ui_delivery_retry_v1(
            delivery_id: str,
            request: Request,
        ) -> MailUiDeliveryResponse:
            """Retry one due delivery owned by the current account lifetime."""
            await ensure_schema()
            await _mail_ui_require_owned_delivery(
                request=request,
                delivery_id=delivery_id,
            )
            try:
                processing = await process_message_delivery(delivery_id)
            except MessageDeliveryServiceError as exc:
                raise _mail_ui_delivery_exception(exc) from None
            if processing.published_now:
                await emit_published_delivery_notifications(processing.delivery_id)
            return _mail_ui_delivery_status_response(processing)

        @fastapi_app.get(
            "/mail/api/v1/projects/{project_id}/threads",
            response_model=MailUiThreadResponse,
        )
        async def mail_ui_thread_v1(
            project_id: int,
            request: Request,
            thread_id: str = Query(),
            limit: int = Query(default=50, ge=1, le=100),
            cursor: str | None = Query(
                default=None,
                min_length=1,
                max_length=_MAIL_UI_CURSOR_MAX_LENGTH,
            ),
        ) -> MailUiThreadResponse:
            """Return a bounded, newest-first page from one visible thread."""
            if not _mail_ui_valid_thread_id(thread_id):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Invalid thread id.",
                )
            starter_message_id = _mail_ui_thread_starter_message_id(thread_id)
            cursor_key = _mail_ui_decode_cursor(cursor) if cursor is not None else None
            await ensure_schema()
            async with get_session() as session:
                await _mail_ui_begin_read_snapshot(session)
                visible_roles = await _mail_ui_visible_project_roles(
                    settings=settings,
                    request=request,
                    session=session,
                )
                if project_id not in visible_roles:
                    raise HTTPException(status_code=404, detail="Project not found")
                total_result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM messages m "
                        "WHERE m.project_id = :project_id "
                        "AND (m.thread_id = :thread_id "
                        "OR (:starter_message_id IS NOT NULL "
                        "AND m.id = :starter_message_id))"
                    ),
                    {
                        "project_id": project_id,
                        "thread_id": thread_id,
                        "starter_message_id": starter_message_id,
                    },
                )
                total = int(total_result.scalar_one())
                if total == 0:
                    raise HTTPException(status_code=404, detail="Thread not found")
                subject_result = await session.execute(
                    text(
                        "SELECT m.subject FROM messages m "
                        "WHERE m.project_id = :project_id "
                        "AND (m.thread_id = :thread_id "
                        "OR (:starter_message_id IS NOT NULL "
                        "AND m.id = :starter_message_id)) "
                        f"ORDER BY {_MAIL_UI_CREATED_TS_KEY_SQL} ASC, m.id ASC "
                        "LIMIT 1"
                    ),
                    {
                        "project_id": project_id,
                        "thread_id": thread_id,
                        "starter_message_id": starter_message_id,
                    },
                )
                thread_subject = str(subject_result.scalar_one())

                cursor_predicate = ""
                page_parameters: dict[str, Any] = {
                    "project_id": project_id,
                    "thread_id": thread_id,
                    "starter_message_id": starter_message_id,
                    "page_limit": limit + 1,
                }
                if cursor_key is not None:
                    cursor_created_ts, cursor_message_id = cursor_key
                    cursor_predicate = (
                        f" AND ({_MAIL_UI_CREATED_TS_KEY_SQL} < :cursor_created_ts "
                        f"OR ({_MAIL_UI_CREATED_TS_KEY_SQL} = :cursor_created_ts "
                        "AND m.id < :cursor_message_id))"
                    )
                    page_parameters.update(
                        {
                            "cursor_created_ts": cursor_created_ts,
                            "cursor_message_id": cursor_message_id,
                        }
                    )
                rows = await session.execute(
                    text(
                        "SELECT m.id, m.project_id, p.slug AS project_slug, m.subject, "
                        "m.body_md, m.importance, m.ack_required, m.thread_id, m.reply_to, "
                        f"m.created_ts, {_MAIL_UI_CREATED_TS_KEY_SQL} AS cursor_created_ts, "
                        "m.attachments, "
                        "coalesce(delivery.sender_name_snapshot, sender.name) AS sender_name, "
                        "CASE WHEN delivery.id IS NULL OR ("
                        "sender.id = delivery.sender_id "
                        "AND sender.agent_generation = delivery.sender_generation_snapshot "
                        "AND sender.project_id = delivery.sender_project_id_snapshot) "
                        "THEN sender.display_name ELSE NULL END AS sender_display_name, "
                        "coalesce(delivery.sender_project_id_snapshot, sender.project_id) "
                        "AS sender_project_id, "
                        "coalesce(delivery.sender_project_slug_snapshot, sender_project.slug) "
                        "AS sender_project_slug, "
                        "delivery.sender_id AS reply_target_agent_id, "
                        "delivery.sender_name_snapshot AS reply_target_agent_name, "
                        "delivery.sender_generation_snapshot "
                        "AS reply_target_agent_generation, "
                        "delivery.sender_project_id_snapshot AS reply_target_project_id, "
                        "delivery.sender_project_slug_snapshot AS reply_target_project_slug, "
                        "delivery.sender_project_generation_snapshot "
                        "AS reply_target_project_generation, "
                        "CASE WHEN delivery.id IS NOT NULL "
                        "AND sender.id = delivery.sender_id "
                        "AND sender.name = delivery.sender_name_snapshot "
                        "AND sender.agent_generation = delivery.sender_generation_snapshot "
                        "AND sender.project_id = delivery.sender_project_id_snapshot "
                        "AND sender.retired_at IS NULL "
                        "AND sender.contact_policy != 'block_all' "
                        "AND sender_project.id = delivery.sender_project_id_snapshot "
                        "AND sender_project.slug = delivery.sender_project_slug_snapshot "
                        "AND sender_project.project_generation = "
                        "delivery.sender_project_generation_snapshot "
                        "AND sender_project.archived_at IS NULL "
                        "THEN 1 ELSE 0 END AS reply_target_available "
                        "FROM messages m "
                        "JOIN projects p ON p.id = m.project_id "
                        "LEFT JOIN message_deliveries delivery "
                        "ON delivery.id = m.delivery_id "
                        "AND delivery.state = 'published' "
                        "AND delivery.message_id = m.id "
                        "AND delivery.project_id = m.project_id "
                        "AND delivery.project_generation_snapshot = p.project_generation "
                        "LEFT JOIN agents sender ON sender.id = m.sender_id "
                        "LEFT JOIN projects sender_project ON sender_project.id = sender.project_id "
                        "WHERE m.project_id = :project_id "
                        "AND (m.thread_id = :thread_id "
                        "OR (:starter_message_id IS NOT NULL "
                        "AND m.id = :starter_message_id))"
                        f"{cursor_predicate} "
                        f"ORDER BY {_MAIL_UI_CREATED_TS_KEY_SQL} DESC, m.id DESC "
                        "LIMIT :page_limit"
                    ),
                    page_parameters,
                )
                page_rows = list(rows.mappings().all())
                has_more = len(page_rows) > limit
                response_rows = page_rows[:limit]
                message_ids = [int(row["id"]) for row in response_rows]
                recipients = await _mail_ui_safe_recipient_map(session, message_ids)
                items = [
                    _mail_ui_message_detail_from_row(
                        row,
                        recipients=recipients[int(row["id"])],
                        settings=settings,
                        request=request,
                        visible_roles=visible_roles,
                    )
                    for row in response_rows
                ]
                next_cursor = (
                    _mail_ui_encode_cursor(
                        str(response_rows[-1]["cursor_created_ts"]),
                        int(response_rows[-1]["id"]),
                    )
                    if has_more and response_rows
                    else None
                )
            return MailUiThreadResponse(
                subject=thread_subject,
                items=items,
                total=total,
                next_cursor=next_cursor,
            )

        def _profile_response_for_user(row: Any) -> MailUiProfileResponse:
            """Render one revalidated account without cookie or password material."""
            global_role = webauth.normalize_ui_user_role(row.role)
            if global_role is None or row.id is None:
                raise RuntimeError("authenticated human has invalid profile identity")
            return MailUiProfileResponse(
                id=int(row.id),
                username=str(row.username),
                display_name=(
                    str(row.display_name) if row.display_name is not None else None
                ),
                global_role=global_role,
                profile_revision=int(row.profile_revision),
            )

        @fastapi_app.get(
            _MAIL_PROFILE_API_PATH,
            response_model=MailUiProfileResponse,
            responses=_MAIL_UI_DOMAIN_ERROR_RESPONSES,
        )
        async def mail_ui_profile_get(request: Request) -> MailUiProfileResponse:
            """Return the signed-in human's non-secret profile and global role."""
            await ensure_schema()
            async with get_session() as session:
                row = await _mail_ui_revalidated_profile_user(request, session)
                return _profile_response_for_user(row)

        @fastapi_app.patch(
            _MAIL_PROFILE_API_PATH,
            response_model=MailUiProfileMutationResponse,
            responses=_MAIL_UI_DOMAIN_MUTATION_ERROR_RESPONSES,
        )
        async def mail_ui_profile_patch(
            request: Request,
            profile: MailUiProfilePatch,
        ) -> MailUiProfileMutationResponse:
            """CAS-update only the signed-in human's normalized display name."""
            principal = _mail_ui_profile_principal(request)
            await ensure_schema()
            try:
                # The domain operation owns BEGIN IMMEDIATE and therefore must be
                # the very first database action on this fresh session.
                async with get_session() as session:
                    result = await mutate_ui_user_display_name(
                        session,
                        target_user_id=principal["id"],
                        account_generation=principal["session_generation"],
                        expected_session_epoch=principal["session_epoch"],
                        expected_profile_revision=profile.expected_profile_revision,
                        display_name=profile.display_name,
                    )
            except UiProfileMutationError as exc:
                raise _mail_ui_profile_http_exception(exc) from None
            return MailUiProfileMutationResponse(
                changed=result.changed,
                display_name=result.display_name,
                profile_revision=result.profile_revision,
            )

        @fastapi_app.get(
            _MAIL_ADMIN_ACCESS_API_PATH,
            response_model=MailUiAdminAccessResponse,
            responses=_MAIL_UI_DOMAIN_ERROR_RESPONSES,
        )
        async def mail_ui_admin_access_get(request: Request) -> MailUiAdminAccessResponse:
            """Return one consistent access-management snapshot to an administrator."""
            await ensure_schema()
            async with get_session() as session:
                # This is the first SELECT in the fresh session. It both
                # revalidates every cookie-bound identity claim and establishes
                # the SQLite read snapshot used by all three matrix queries.
                await _mail_ui_revalidated_admin_user(request, session)
                user_rows = (
                    await session.execute(
                        text(
                            "SELECT id, username, display_name, disabled, role, "
                            "session_generation, session_epoch FROM ui_users "
                            "ORDER BY lower(username), id"
                        )
                    )
                ).mappings().all()
                project_rows = (
                    await session.execute(
                        text(
                            "SELECT id, slug, human_key, project_generation, archived_at "
                            "FROM projects ORDER BY lower(slug), id"
                        )
                    )
                ).mappings().all()
                assignment_rows = (
                    await session.execute(
                        text(
                            "SELECT user_id, project_id, role FROM ui_project_assignments "
                            "ORDER BY user_id, project_id"
                        )
                    )
                ).mappings().all()

            user_ids = {int(row["id"]) for row in user_rows}
            project_ids = {int(row["id"]) for row in project_rows}
            assignments_by_user: dict[int, list[MailUiAdminAssignmentSummary]] = {
                user_id: [] for user_id in user_ids
            }
            for row in assignment_rows:
                user_id = int(row["user_id"])
                project_id = int(row["project_id"])
                assignment_role = webauth.normalize_project_role(row["role"])
                if (
                    user_id not in user_ids
                    or project_id not in project_ids
                    or assignment_role is None
                ):
                    raise RuntimeError("invalid persisted UI project assignment")
                assignments_by_user[user_id].append(
                    MailUiAdminAssignmentSummary(
                        project_id=project_id,
                        role=assignment_role,
                    )
                )

            users: list[MailUiAdminUserSummary] = []
            for row in user_rows:
                user_id = int(row["id"])
                global_role = webauth.normalize_ui_user_role(row["role"])
                generation = str(row["session_generation"] or "")
                if global_role is None or re.fullmatch(r"[0-9a-f]{64}", generation) is None:
                    raise RuntimeError("invalid persisted UI user access identity")
                users.append(
                    MailUiAdminUserSummary(
                        id=user_id,
                        username=str(row["username"]),
                        display_name=(
                            str(row["display_name"])
                            if row["display_name"] is not None
                            else None
                        ),
                        disabled=bool(row["disabled"]),
                        global_role=global_role,
                        account_generation=generation,
                        access_version=int(row["session_epoch"]),
                        assignments=assignments_by_user[user_id],
                    )
                )

            projects: list[MailUiAdminProjectSummary] = []
            for row in project_rows:
                generation = str(row["project_generation"] or "")
                if re.fullmatch(r"[0-9a-f]{64}", generation) is None:
                    raise RuntimeError("invalid persisted project access identity")
                projects.append(
                    MailUiAdminProjectSummary(
                        id=int(row["id"]),
                        slug=str(row["slug"]),
                        human_key=str(row["human_key"]),
                        project_generation=generation,
                        archived_at=(
                            _mail_ui_datetime(row["archived_at"])
                            if row["archived_at"] is not None
                            else None
                        ),
                    )
                )
            return MailUiAdminAccessResponse(users=users, projects=projects)

        @fastapi_app.put(
            _MAIL_ADMIN_ASSIGNMENT_API_PATH,
            response_model=MailUiAdminProjectAccessResponse,
            responses=_MAIL_UI_DOMAIN_MUTATION_ERROR_RESPONSES,
        )
        async def mail_ui_admin_assignment_put(
            target_user_id: Annotated[int, FastApiPath(gt=0)],
            project_id: Annotated[int, FastApiPath(gt=0)],
            request: Request,
            mutation: MailUiAdminProjectAccessPut,
        ) -> MailUiAdminProjectAccessResponse:
            """Grant, replace, or revoke one member assignment with full CAS."""
            actor = _mail_ui_require_admin_principal(request)
            await ensure_schema()
            try:
                # Do not pre-read through this session. The domain operation must
                # acquire its own BEGIN IMMEDIATE boundary before every check.
                async with get_session() as session:
                    result = await mutate_ui_project_access(
                        session,
                        actor_user_id=actor["id"],
                        actor_account_generation=actor["session_generation"],
                        expected_actor_session_epoch=actor["session_epoch"],
                        trusted_cli_actor=False,
                        target_user_id=target_user_id,
                        project_id=project_id,
                        expected_project_generation=mutation.expected_project_generation,
                        role=mutation.role,
                        expected_access_version=mutation.expected_access_version,
                        account_generation=mutation.account_generation,
                    )
            except UiAccessMutationError as exc:
                raise _mail_ui_access_http_exception(exc) from None
            return MailUiAdminProjectAccessResponse(
                changed=result.changed,
                role=result.role,
                access_version=result.access_version,
            )

        def _preferences_response_for_user(row: Any) -> MailUiPreferencesResponse:
            """Render a row whose locale integrity is enforced by the schema."""
            return _mail_ui_preferences_response(
                preferred_ui_locale=_mail_ui_locale_from_db(row.preferred_ui_locale),
                preferred_correspondence_locale=(
                    _mail_ui_locale_from_db(row.preferred_correspondence_locale)
                    if row.preferred_correspondence_locale is not None
                    else None
                ),
            )

        @fastapi_app.get(
            _MAIL_PREFERENCES_API_PATH,
            response_model=MailUiPreferencesResponse,
        )
        async def mail_ui_preferences_get(request: Request) -> MailUiPreferencesResponse:
            """Return stored and effective languages for the signed-in human."""
            await ensure_schema()
            async with get_session() as session:
                row = await _mail_ui_preferences_user(request, session)
                return _preferences_response_for_user(row)

        @fastapi_app.patch(
            _MAIL_PREFERENCES_API_PATH,
            response_model=MailUiPreferencesResponse,
        )
        async def mail_ui_preferences_patch(
            request: Request,
            preferences: MailUiPreferencesPatch,
        ) -> MailUiPreferencesResponse:
            """Partially update only the signed-in human's language preferences."""
            if not webauth.same_origin(
                request.headers.get("origin", ""),
                request.headers.get("referer", ""),
                request.headers.get("host", ""),
                expected_scheme=request.url.scheme,
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cross-origin request rejected",
                )

            await ensure_schema()
            async with get_session() as session:
                row = await _mail_ui_preferences_user(request, session)
            changed_fields = preferences.model_fields_set
            values: dict[str, Any] = {}
            if "preferred_ui_locale" in changed_fields:
                values["preferred_ui_locale"] = preferences.preferred_ui_locale
            if "preferred_correspondence_locale" in changed_fields:
                values["preferred_correspondence_locale"] = (
                    preferences.preferred_correspondence_locale
                )

            if values:
                principal = _mail_ui_preferences_principal(request)
                async with get_session() as session:
                    updated = await _mail_ui_preferences_cas_update(
                        session,
                        principal=principal,
                        values=values,
                    )
                if not updated:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Authenticated Mail UI session is no longer current.",
                    )
                async with get_session() as session:
                    row = await _mail_ui_preferences_user(request, session)
            return _preferences_response_for_user(row)

        @fastapi_app.patch(
            _MAIL_PASSWORD_API_PATH,
            response_model=MailUiPasswordChangeResponse,
        )
        async def mail_ui_password_patch(
            request: Request,
            response: Response,
            passwords: MailUiPasswordPatch,
        ) -> MailUiPasswordChangeResponse:
            """Rotate the signed-in human's password and refresh only this session."""
            if not webauth.same_origin(
                request.headers.get("origin", ""),
                request.headers.get("referer", ""),
                request.headers.get("host", ""),
                expected_scheme=request.url.scheme,
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cross-origin request rejected",
                )

            # Do not yield between identifying the limiter key and registering
            # the attempt. Five concurrent requests consume all five slots;
            # another request observes the full bucket immediately.
            principal = _mail_ui_password_principal(request)
            retry_after = _password_change_register_attempt(
                user_id=principal["id"],
                generation=principal["session_generation"],
            )
            if retry_after is not None:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many password change attempts. Try again later.",
                    headers={"Retry-After": str(retry_after)},
                )

            await ensure_schema()
            async with get_session() as session:
                row = await _mail_ui_password_user(request, session)
                old_password_hash = str(row.password_hash)

            current_password = passwords.current_password.get_secret_value()
            new_password = passwords.new_password.get_secret_value()
            current_matches = await asyncio.to_thread(
                webauth.verify_password,
                current_password,
                old_password_hash,
            )
            if not current_matches:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Current password is incorrect.",
                )
            new_password_hash = await asyncio.to_thread(webauth.hash_password, new_password)

            async with get_session() as session:
                changed = await _mail_ui_password_cas_update(
                    session,
                    principal=principal,
                    old_password_hash=old_password_hash,
                    new_password_hash=new_password_hash,
                )
            if not changed:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authenticated Mail UI session is no longer current.",
                )

            import time as _time

            refreshed_token = webauth.make_session(
                principal["username"],
                epoch=principal["session_epoch"] + 1,
                generation=principal["session_generation"],
                now=_time.time(),
                secret=settings.mail_ui.session_secret.encode("utf-8"),
                ttl=float(settings.mail_ui.session_ttl_seconds),
            )
            _set_mail_ui_session_cookie(response, token=refreshed_token, settings=settings)
            structlog.get_logger("mail_ui").info(
                "mail_ui.password_changed",
                username=principal["username"],
            )
            return MailUiPasswordChangeResponse()

        @fastapi_app.post("/mail/api/delete-messages", response_class=JSONResponse)
        async def delete_messages_api(request: Request) -> JSONResponse:
            """Permanently delete messages by ID (cross-project).

            Removes messages from the SQLite database AND deletes the
            corresponding markdown files from the Git archive.
            """
            await ensure_schema()

            try:
                request_body = await request.json()
                message_ids: list[int] = request_body.get("message_ids", [])

                if not message_ids:
                    raise HTTPException(status_code=400, detail="No message IDs provided")

                if len(message_ids) > 500:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Too many messages ({len(message_ids)}). Maximum is 500."
                    )

                deleted_count = 0
                messages_by_project: dict[str, list[tuple[Any, ...]]] = {}
                recip_map: dict[int, list[str]] = {}
                async with get_session() as session:
                    placeholders = ','.join([f':mid{i}' for i in range(len(message_ids))])
                    id_params: dict[str, Any] = {f"mid{i}": mid for i, mid in enumerate(message_ids)}

                    # Fetch message metadata for Git cleanup
                    rows = await session.execute(
                        text(
                            f"""
                            SELECT m.id, m.created_ts, m.subject, s.name AS sender_name,
                                   p.slug AS project_slug
                            FROM messages m
                            JOIN agents s ON s.id = m.sender_id
                            JOIN projects p ON p.id = m.project_id
                            WHERE m.id IN ({placeholders})
                            """
                        ),
                        id_params,
                    )
                    messages_to_delete = [tuple(row) for row in rows.fetchall()]

                    if not messages_to_delete:
                        return JSONResponse({"success": True, "deleted_count": 0})

                    # Collect recipients per message
                    recip_rows = await session.execute(
                        text(
                            f"""
                            SELECT mr.message_id, a.name
                            FROM message_recipients mr
                            JOIN agents a ON a.id = mr.agent_id
                            WHERE mr.message_id IN ({placeholders})
                            """
                        ),
                        id_params,
                    )
                    for rr in recip_rows.fetchall():
                        recip_map.setdefault(int(rr[0]), []).append(rr[1])

                    for mrow in messages_to_delete:
                        slug = str(mrow[4])
                        messages_by_project.setdefault(slug, []).append(mrow)

                    # Delete from SQLite
                    await session.execute(
                        text(f"DELETE FROM message_recipients WHERE message_id IN ({placeholders})"),
                        id_params,
                    )
                    del_result = await session.execute(
                        text(f"DELETE FROM messages WHERE id IN ({placeholders})"),
                        id_params,
                    )
                    deleted_count = int(getattr(del_result, "rowcount", 0) or 0)
                    await session.commit()

                settings = get_settings()
                total_git_files_removed = 0
                for project_slug, proj_msgs in messages_by_project.items():
                    try:
                        total_git_files_removed += await _delete_messages_from_archive(
                            settings=settings,
                            project_slug=project_slug,
                            messages_to_delete=proj_msgs,
                            recip_map=recip_map,
                            commit_message=f"delete: {len(proj_msgs)} message(s) via web UI\n",
                        )
                    except Exception as archive_exc:
                        logging.getLogger(__name__).warning(
                            "Git archive cleanup failed for project %s: %s",
                            project_slug,
                            archive_exc,
                        )

                return JSONResponse({
                    "success": True,
                    "deleted_count": deleted_count,
                    "git_files_removed": total_git_files_removed,
                })

            except HTTPException:
                raise
            except Exception as exc:
                import traceback
                traceback.print_exc()
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to delete messages: {exc!s}"
                ) from exc

        # ---- Agent Retire/Unretire API ----

        @fastapi_app.post("/mail/api/retire-agent", response_class=JSONResponse)
        async def retire_agent_api(request: Request) -> JSONResponse:
            """Retire an agent (soft-delete). Preserves message history but stops new messages."""
            await ensure_schema()
            try:
                body = await request.json()
                agent_id: int | None = body.get("agent_id")
                if agent_id is None:
                    raise HTTPException(status_code=400, detail="agent_id is required")

                async with get_session() as session:
                    from .models import Agent
                    agent = await session.get(Agent, agent_id)
                    if not agent:
                        raise HTTPException(status_code=404, detail="Agent not found")
                    agent.retired_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    session.add(agent)
                    await session.commit()

                return JSONResponse({"success": True, "agent_id": agent_id, "status": "retired"})
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to retire agent: {exc!s}") from exc

        @fastapi_app.post("/mail/api/unretire-agent", response_class=JSONResponse)
        async def unretire_agent_api(request: Request) -> JSONResponse:
            """Restore a retired agent back to active status."""
            await ensure_schema()
            try:
                body = await request.json()
                agent_id: int | None = body.get("agent_id")
                if agent_id is None:
                    raise HTTPException(status_code=400, detail="agent_id is required")

                async with get_session() as session:
                    from .models import Agent
                    agent = await session.get(Agent, agent_id)
                    if not agent:
                        raise HTTPException(status_code=404, detail="Agent not found")
                    agent.retired_at = None
                    session.add(agent)
                    await session.commit()

                return JSONResponse({"success": True, "agent_id": agent_id, "status": "active"})
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to unretire agent: {exc!s}") from exc

        # ---- Project Archive/Unarchive API ----

        @fastapi_app.post("/mail/api/archive-project", response_class=JSONResponse)
        async def archive_project_api(request: Request) -> JSONResponse:
            """Archive a project (soft-delete). Preserves all messages but hides from active lists."""
            await ensure_schema()
            try:
                body = await request.json()
                project_id: int | None = body.get("project_id")
                if project_id is None:
                    raise HTTPException(status_code=400, detail="project_id is required")

                async with get_session() as session:
                    from .models import Project
                    project = await session.get(Project, project_id)
                    if not project:
                        raise HTTPException(status_code=404, detail="Project not found")
                    project.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    session.add(project)
                    await session.commit()

                return JSONResponse({"success": True, "project_id": project_id, "status": "archived"})
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to archive project: {exc!s}") from exc

        @fastapi_app.post("/mail/api/unarchive-project", response_class=JSONResponse)
        async def unarchive_project_api(request: Request) -> JSONResponse:
            """Restore an archived project back to active status."""
            await ensure_schema()
            try:
                body = await request.json()
                project_id: int | None = body.get("project_id")
                if project_id is None:
                    raise HTTPException(status_code=400, detail="project_id is required")

                async with get_session() as session:
                    from .models import Project
                    project = await session.get(Project, project_id)
                    if not project:
                        raise HTTPException(status_code=404, detail="Project not found")
                    project.archived_at = None
                    session.add(project)
                    await session.commit()

                return JSONResponse({"success": True, "project_id": project_id, "status": "active"})
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to unarchive project: {exc!s}") from exc

        @fastapi_app.get("/mail/projects", response_class=HTMLResponse)
        async def mail_projects_list(request: Request) -> HTMLResponse:
            """Projects list view (moved from /mail)"""
            await ensure_schema()
            if _mail_ui_request_is_admin(settings=settings, request=request):
                await refresh_project_sibling_suggestions()
            sibling_map = await get_project_sibling_data()
            async with get_session() as session:
                visible_roles = await _mail_ui_visible_project_roles(
                    settings=settings,
                    request=request,
                    session=session,
                )
                rows = await session.execute(
                    text("SELECT id, slug, human_key, created_at, archived_at FROM projects ORDER BY created_at DESC")
                )
                projects = []
                for r in rows.fetchall():
                    project_id = int(r[0])
                    if project_id not in visible_roles:
                        continue
                    siblings = sibling_map.get(project_id, {"confirmed": [], "suggested": []})
                    access = _mail_ui_access_context(
                        settings=settings,
                        request=request,
                        project_id=project_id,
                        project_role=visible_roles[project_id],
                    )
                    projects.append(
                        {
                            "id": project_id,
                            "slug": r[1],
                            "human_key": r[2],
                            "created_at": str(r[3]),
                            "archived_at": str(r[4]) if r[4] else None,
                            "confirmed_siblings": [
                                sibling
                                for sibling in siblings.get("confirmed", [])
                                if int(sibling["peer"]["id"]) in visible_roles
                            ],
                            "suggested_siblings": [
                                sibling
                                for sibling in siblings.get("suggested", [])
                                if int(sibling["peer"]["id"]) in visible_roles
                            ],
                            "access_role": access["project_role"],
                            "can_read": access["can_read"],
                            "can_reply": access["can_reply"],
                            "can_compose": access["can_compose"],
                            "can_mutate": access["can_mutate"],
                        }
                    )
            return await _render("mail_index.html", projects=projects)

        @fastapi_app.get("/mail/unified-inbox", response_class=HTMLResponse)
        async def unified_inbox_alias(
            request: Request,
            limit: int = 10000,
            filter_importance: str | None = None,
        ) -> HTMLResponse:
            """Render the legacy unified URL through the scoped inbox builder.

            Args:
                request: Current HTTP request.
                limit: Maximum recent messages to load.
                filter_importance: Optional high-priority filter retained for
                    URL compatibility.

            Returns:
                The scoped unified inbox page.
            """
            return await _render_legacy_unified_inbox(
                request=request,
                limit=limit,
                filter_importance=filter_importance,
            )

        def _mail_react_index_response() -> FileResponse:
            """Serve the Vite entry point without allowing account data to be cached."""
            index_file = _mail_react_resolve_file(
                _mail_react_dist_root(),
                "index.html",
            )
            if index_file is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="React Mail UI build is unavailable.",
                )
            return FileResponse(
                index_file,
                media_type="text/html",
                headers=_MAIL_REACT_INDEX_HEADERS,
            )

        @fastapi_app.api_route(
            "/mail/",
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
        async def mail_react_slash_redirect(request: Request) -> Response:
            """Canonicalize the sole React shell URL."""
            location = _MAIL_REACT_BASE_PATH
            if request.url.query:
                location = f"{location}?{request.url.query}"
            return Response(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={**_MAIL_REACT_INDEX_HEADERS, "Location": location},
            )

        @fastapi_app.api_route(
            "/mail/assets/{asset_path:path}",
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
        async def mail_react_asset(asset_path: str) -> FileResponse:
            """Serve only fingerprinted build files physically contained in ``assets``."""
            dist_root = _mail_react_dist_root()
            if _mail_react_resolve_file(dist_root, "index.html") is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="React Mail UI build is unavailable.",
                )

            asset_file = _mail_react_resolve_file(
                dist_root,
                f"assets/{asset_path}",
            )
            try:
                resolved_assets_root = (dist_root / "assets").resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                resolved_assets_root = None
            if asset_file is None or resolved_assets_root is None:
                canonical_asset_path = None
            else:
                try:
                    canonical_asset_path = asset_file.relative_to(
                        resolved_assets_root
                    ).as_posix()
                except ValueError:
                    canonical_asset_path = None
            if (
                asset_file is None
                or resolved_assets_root is None
                or asset_file == resolved_assets_root
                or not asset_file.is_relative_to(resolved_assets_root)
                or canonical_asset_path != asset_path
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="React Mail UI asset not found.",
                )
            if canonical_asset_path == "legacy.js":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="React Mail UI asset not found.",
                )
            asset_headers = (
                _MAIL_REACT_LEGACY_ASSET_HEADERS
                if asset_path == "legacy.css"
                else _MAIL_REACT_ASSET_HEADERS
            )
            return FileResponse(asset_file, headers=asset_headers)

        @fastapi_app.get("/mail/{project}", response_class=HTMLResponse)
        async def mail_project(
            project: str,
            request: Request,
            q: str | None = None,
            scope: str | None = None,
            order: str | None = None,
            boost: int | None = None,
        ) -> HTMLResponse:
            if order not in ("relevance", "time", None):
                order = "relevance"
            await ensure_schema()
            async with get_session() as session:
                prow = await _resolve_mail_project(session, project)
                if not prow:
                    return await _render("error.html", status_code=404, message="Project not found")
                pid = int(prow[0])
                access = await _mail_ui_require_project_access(
                    settings=settings,
                    request=request,
                    session=session,
                    project_id=pid,
                )
                project_archived_at = str(prow[3]) if prow[3] else None
                # display_name is selected because the viewer had no way to show
                # it: the column has existed and been settable via
                # set_agent_display_name all along, and this dict — the only
                # source the project page gets — never carried it. Measured
                # before changing anything: the live page renders five agent
                # cards and the one alias actually set on this server appears
                # zero times on it.
                agents_q = await session.execute(
                    # notify_sound joins display_name here for the same reason it was added:
                    # the column exists, a tool sets it, and this dict is the only
                    # thing the project page ever sees. A preference nobody can
                    # perceive is indistinguishable from one nobody set.
                    text("SELECT id, name, program, model, retired_at, display_name, notify_sound, last_active_ts FROM agents WHERE project_id = :pid ORDER BY name"),
                    {"pid": pid},
                )
                agents = [{"id": r[0], "name": r[1], "program": r[2], "model": r[3], "retired_at": str(r[4]) if r[4] else None, "display_name": r[5], "notify_sound": r[6], "last_active_ts": str(r[7]) if r[7] else None} for r in agents_q.fetchall()]
                matched_messages: list[dict] = []
                if q and q.strip():
                    # Prefer FTS5 when available (fts_messages maintained by triggers)
                    fts_expr, like_pat, like_scope, tokens = _parse_fts_query(q, scope)
                    weights = (0.0, 3.0, 1.0) if (boost or 0) else (0.0, 1.0, 1.0)
                    fts_sql = (
                        "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                        "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                        "m.created_ts, m.importance, m.thread_id, "
                        "snippet(fts_messages, 2, '<mark>', '</mark>', '…', 18) AS body_snippet "
                        "FROM fts_messages "
                        "JOIN messages m ON m.id = fts_messages.rowid "
                        "JOIN agents s ON s.id = m.sender_id "
                        "LEFT JOIN projects sp ON sp.id = s.project_id "
                        "WHERE m.project_id = :pid AND fts_messages MATCH :q "
                        + (
                            "ORDER BY m.created_ts DESC "
                            if (order or "relevance") == "time"
                            else f"ORDER BY bm25(fts_messages, {weights[0]}, {weights[1]}, {weights[2]}) "
                        )
                        + "LIMIT 10000"
                    )
                    try:
                        search = await session.execute(text(fts_sql), {"pid": pid, "q": fts_expr or q})
                        matched_messages = []
                        for r in search.mappings().all():
                            safe_snippet, snippet_hits = _safe_fts_snippet(
                                r["body_snippet"]
                            )
                            sender_display, sender_meta = _http_sender_identity(
                                message_project_id=pid,
                                sender_name=r["sender_name"],
                                sender_project_id=r["sender_project_id"],
                                sender_project_human_key=r["sender_project_name"],
                                sender_project_slug=r["sender_project_slug"],
                            )
                            item = {
                                "id": r["id"],
                                "subject": r["subject"],
                                "sender": sender_display,
                                "created": str(r["created_ts"]),
                                "importance": r["importance"],
                                "thread_id": r["thread_id"],
                                "snippet": safe_snippet,
                                "hits": snippet_hits,
                            }
                            item.update(sender_meta)
                            matched_messages.append(item)
                    except Exception:
                        # Fallback to LIKE if FTS not available
                        if like_scope == "subject":
                            like_sql = (
                                "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                                "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                                "m.created_ts, m.importance, m.thread_id "
                                "FROM messages m JOIN agents s ON s.id = m.sender_id "
                                "LEFT JOIN projects sp ON sp.id = s.project_id "
                                f"WHERE m.project_id = :pid AND m.subject LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                                "ORDER BY m.created_ts DESC LIMIT 10000"
                            )
                        elif like_scope == "body":
                            like_sql = (
                                "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                                "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                                "m.created_ts, m.importance, m.thread_id "
                                "FROM messages m JOIN agents s ON s.id = m.sender_id "
                                "LEFT JOIN projects sp ON sp.id = s.project_id "
                                f"WHERE m.project_id = :pid AND m.body_md LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                                "ORDER BY m.created_ts DESC LIMIT 10000"
                            )
                        else:
                            like_sql = (
                                "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                                "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                                "m.created_ts, m.importance, m.thread_id "
                                "FROM messages m JOIN agents s ON s.id = m.sender_id "
                                "LEFT JOIN projects sp ON sp.id = s.project_id "
                                f"WHERE m.project_id = :pid AND (m.subject LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                                f"OR m.body_md LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}') "
                                "ORDER BY m.created_ts DESC LIMIT 10000"
                            )
                        search = await session.execute(text(like_sql), {"pid": pid, "pat": like_pat or f"%{_like_escape(q)}%"})
                        matched_messages = []
                        for r in search.mappings().all():
                            sender_display, sender_meta = _http_sender_identity(
                                message_project_id=pid,
                                sender_name=r["sender_name"],
                                sender_project_id=r["sender_project_id"],
                                sender_project_human_key=r["sender_project_name"],
                                sender_project_slug=r["sender_project_slug"],
                            )
                            item = {
                                "id": r["id"],
                                "subject": r["subject"],
                                "sender": sender_display,
                                "created": str(r["created_ts"]),
                                "importance": r["importance"],
                                "thread_id": r["thread_id"],
                                "snippet": "",
                                "hits": 0,
                            }
                            item.update(sender_meta)
                            matched_messages.append(item)
            render_context: dict[str, Any] = {
                "project": {
                    "id": pid,
                    "slug": prow[1],
                    "human_key": prow[2],
                    "archived_at": project_archived_at,
                    "can_reply": access["can_reply"],
                    "can_compose": access["can_compose"],
                    "can_mutate": access["can_mutate"],
                },
                "agents": agents,
                "q": q or "",
                "scope": scope or "",
                "order": order or "relevance",
                "boost": bool(boost),
                "mail_ui_access": access,
            }
            if q and q.strip():
                render_context["tokens"] = tokens
                render_context["results"] = matched_messages
            return await _render("mail_project.html", **render_context)

        @fastapi_app.post("/mail/api/projects/{project_id}/siblings/{other_id}", response_class=JSONResponse)
        async def update_project_sibling(project_id: int, other_id: int, request: Request) -> JSONResponse:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            action = str(payload.get("action", "")).lower()
            if action not in {"confirm", "dismiss", "reset"}:
                return JSONResponse({"error": "Invalid action"}, status_code=status.HTTP_400_BAD_REQUEST)

            target_status = {
                "confirm": "confirmed",
                "dismiss": "dismissed",
                "reset": "suggested",
            }[action]

            try:
                suggestion = await update_project_sibling_status(project_id, other_id, target_status)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
            except NoResultFound:
                return JSONResponse({"error": "Project pair not found"}, status_code=status.HTTP_404_NOT_FOUND)
            except Exception as exc:
                structlog.get_logger("sibling").exception(
                    "project_sibling.update_failed",
                    project_id=project_id,
                    other_id=other_id,
                    action=action,
                    error=str(exc),
                )
                return JSONResponse(
                    {"error": "Unable to update sibling status"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            return JSONResponse({"status": suggestion["status"], "suggestion": suggestion})

        async def _render_legacy_unified_inbox(
            request: Request,
            limit: int = 10000,
            filter_importance: str | None = None,
        ) -> HTMLResponse:
            """Unified inbox showing messages from all active agents across all projects."""
            limit = min(max(1, limit), 10000)
            await ensure_schema()
            async with get_session() as session:
                visible_roles = await _mail_ui_visible_project_roles(
                    settings=settings,
                    request=request,
                    session=session,
                )
                visible_ids = sorted(visible_roles)
                visible_params = {
                    f"legacy_visible_pid_{index}": project_id
                    for index, project_id in enumerate(visible_ids)
                }
                visible_placeholders = (
                    ", ".join(f":legacy_visible_pid_{index}" for index in range(len(visible_ids)))
                    if visible_ids
                    else "-1"
                )
                # Get all projects with their agents
                projects_query = await session.execute(
                    text(
                        """
                    SELECT p.id, p.slug, p.human_key,
                           COUNT(DISTINCT a.id) as agent_count,
                           MAX(a.last_active_ts) as last_activity
                    FROM projects p
                    LEFT JOIN agents a ON a.project_id = p.id
                    GROUP BY p.id, p.slug, p.human_key
                    ORDER BY (last_activity IS NULL) ASC, last_activity DESC, p.created_at DESC
                    """
                    )
                )
                # Same dict-literal union as the reservations list above: without
                # this, `sum(p["agent_count"] ...)` at the render call has no
                # matching overload because the inferred value type includes str.
                projects_data: list[dict[str, Any]] = []
                for r in projects_query.fetchall():
                    proj_id = int(r[0])
                    if proj_id not in visible_roles:
                        continue
                    access = _mail_ui_access_context(
                        settings=settings,
                        request=request,
                        project_id=proj_id,
                        project_role=visible_roles[proj_id],
                    )
                    # Get agents for this project
                    agents_query = await session.execute(
                        text(
                            """
                        SELECT a.id, a.name, a.program, a.model, a.last_active_ts,
                               a.notify_sound
                        FROM agents a
                        WHERE a.project_id = :pid
                        ORDER BY a.last_active_ts DESC, a.name ASC
                        """
                        ),
                        {"pid": proj_id},
                    )

                    agents_list = []
                    for ar in agents_query.fetchall():
                        agents_list.append(
                            {
                                "id": int(ar[0]),
                                "name": ar[1],
                                "program": ar[2],
                                "model": ar[3],
                                "last_active": str(ar[4]) if ar[4] else None,
                                # Carried so this page can sound the writer's own
                                # tone. The project page has done so since the
                                # feature landed; this is the page a watcher
                                # actually leaves open, and without the column
                                # every colleague rang identically here — the
                                # feature failing exactly where it was for.
                                "notify_sound": ar[5],
                            }
                        )

                    if agents_list:  # Only include projects with agents
                        projects_data.append(
                            {
                                "id": proj_id,
                                "slug": r[1],
                                "human_key": r[2],
                                "agent_count": int(r[3] or 0),
                                "agents": agents_list,
                                "access_role": access["project_role"],
                                "can_read": access["can_read"],
                                "can_reply": access["can_reply"],
                                "can_compose": access["can_compose"],
                                "can_mutate": access["can_mutate"],
                            }
                        )

                # Get recent messages across all projects with thread information
                # Build WHERE clause safely using parameterized queries
                importance_conditions = [f"m.project_id IN ({visible_placeholders})"]
                query_params = {"lim": limit, **visible_params}

                if filter_importance and filter_importance.lower() in ["urgent", "high"]:
                    importance_conditions.append("m.importance IN ('urgent', 'high')")

                where_clause = "WHERE " + " AND ".join(importance_conditions) if importance_conditions else "WHERE 1=1"

                total_result = await session.execute(
                    text(
                        f"""
                        SELECT COUNT(*)
                        FROM messages m
                        JOIN agents sender ON sender.id = m.sender_id
                        JOIN projects p ON p.id = m.project_id
                        {where_clause}
                        """
                    ),
                    query_params,
                )
                total_messages = int(total_result.scalar_one())

                messages_query = await session.execute(
                    text(
                        f"""
                    SELECT
                        m.id, m.subject, m.body_md, m.created_ts, m.importance, m.thread_id,
                        m.project_id AS message_project_id,
                        p.slug, p.human_key,
                        sender.name as sender_name,
                        sender.project_id AS sender_project_id,
                        sp.human_key AS sender_project_name,
                        sp.slug AS sender_project_slug,
                        COALESCE(
                            (
                                SELECT GROUP_CONCAT(name, ', ')
                                FROM (
                                    SELECT DISTINCT recip2.name AS name
                                    FROM message_recipients mr2
                                    JOIN agents recip2 ON recip2.id = mr2.agent_id
                                    WHERE mr2.message_id = m.id
                                    ORDER BY name
                                )
                            ),
                            ''
                        ) as recipient_names,
                        COUNT(DISTINCT CASE WHEN m2.id IS NOT NULL THEN m2.id END) as thread_count
                    FROM messages m
                    JOIN projects p ON p.id = m.project_id
                    JOIN agents sender ON sender.id = m.sender_id
                    LEFT JOIN projects sp ON sp.id = sender.project_id
                    LEFT JOIN message_recipients mr ON mr.message_id = m.id
                    LEFT JOIN agents recip ON recip.id = mr.agent_id
                    LEFT JOIN messages m2 ON (
                        m.thread_id IS NOT NULL
                        AND m2.thread_id = m.thread_id
                        AND m2.project_id = m.project_id
                        AND m2.id != m.id
                    )
                    {where_clause}
                    GROUP BY m.id, m.subject, m.body_md, m.created_ts, m.importance, m.thread_id,
                             m.project_id, p.slug, p.human_key, sender.name, sender.project_id, sp.human_key, sp.slug
                    ORDER BY m.created_ts DESC
                    LIMIT :lim
                    """
                    ),
                    query_params,
                )

                messages = []
                for r in messages_query.mappings().all():
                    sender_display, sender_meta = _http_sender_identity(
                        message_project_id=r["message_project_id"],
                        sender_name=r["sender_name"],
                        sender_project_id=r["sender_project_id"],
                        sender_project_human_key=r["sender_project_name"],
                        sender_project_slug=r["sender_project_slug"],
                    )
                    item = {
                        "id": int(r["id"]),
                        "subject": r["subject"],
                        "body_md": r["body_md"] or "",
                        "created": str(r["created_ts"]),
                        "importance": r["importance"] or "normal",
                        "thread_id": r["thread_id"],
                        "project_slug": r["slug"],
                        "project_name": r["human_key"],
                        "sender": sender_display,
                        "recipients": r["recipient_names"] or "",
                        "thread_count": int(r["thread_count"] or 0),
                        "can_reply": _mail_ui_access_context(
                            settings=settings,
                            request=request,
                            project_id=int(r["message_project_id"]),
                            project_role=visible_roles[int(r["message_project_id"])],
                        )["can_reply"],
                    }
                    item.update(sender_meta)
                    messages.append(item)

            return await _render(
                "mail_unified_inbox.html",
                projects=projects_data,
                messages=messages,
                total_agents=sum(p["agent_count"] for p in projects_data),
                total_messages=total_messages,
                filter_importance=filter_importance or "",
                # This route does carry agents on each project row, but the map
                # is built the same way as on /mail so the two routes cannot
                # drift into rendering different tones for the same colleague.
                agent_sounds={
                    a["name"]: a["notify_sound"]
                    for p in projects_data
                    for a in p["agents"]
                    if a.get("notify_sound")
                },
            )

        @fastapi_app.get("/mail/{project}/inbox/{agent}", response_class=HTMLResponse)
        async def mail_inbox(
            project: str,
            agent: str,
            request: Request,
            limit: int = 10000,
            page: int = 1,
        ) -> HTMLResponse:
            limit = min(max(1, limit), 10000)
            page = min(max(1, page), 10000)
            await ensure_schema()
            async with get_session() as session:
                prow = await _resolve_mail_project(session, project)
                if not prow:
                    return await _render("error.html", status_code=404, message="Project not found")
                pid = int(prow[0])
                access = await _mail_ui_require_project_access(
                    settings=settings,
                    request=request,
                    session=session,
                    project_id=pid,
                )
                arow = (
                    await session.execute(
                        text("SELECT id, name FROM agents WHERE project_id = :pid AND lower(name) = lower(:name)"),
                        {"pid": pid, "name": agent},
                    )
                ).fetchone()
                if not arow:
                    return await _render("error.html", message="Agent not found")
                offset = max(0, (max(1, page) - 1) * max(1, limit))
                inbox_rows = await session.execute(
                    text(
                        """
                    SELECT
                        m.id,
                        m.subject,
                        s.name AS sender_name,
                        s.project_id AS sender_project_id,
                        sp.human_key AS sender_project_name,
                        sp.slug AS sender_project_slug,
                        m.created_ts,
                        m.importance,
                        m.thread_id,
                        m.ack_required,
                        mr.read_ts,
                        mr.ack_ts
                    FROM messages m
                    JOIN message_recipients mr ON mr.message_id = m.id
                    JOIN agents a ON a.id = mr.agent_id
                    JOIN agents s ON s.id = m.sender_id
                    LEFT JOIN projects sp ON sp.id = s.project_id
                    WHERE m.project_id = :pid AND a.name = :name
                    ORDER BY m.created_ts DESC
                    LIMIT :lim OFFSET :off
                    """
                    ),
                    {"pid": pid, "name": agent, "lim": limit, "off": offset},
                )
                items = []
                for r in inbox_rows.mappings().all():
                    sender_display, sender_meta = _http_sender_identity(
                        message_project_id=pid,
                        sender_name=r["sender_name"],
                        sender_project_id=r["sender_project_id"],
                        sender_project_human_key=r["sender_project_name"],
                        sender_project_slug=r["sender_project_slug"],
                    )
                    read_ts = r["read_ts"]
                    ack_ts = r["ack_ts"]
                    ack_required = bool(r["ack_required"])
                    item = {
                        "id": r["id"],
                        "subject": r["subject"],
                        "sender": sender_display,
                        "created": str(r["created_ts"]),
                        "importance": r["importance"],
                        "thread_id": r["thread_id"],
                        "ack_required": ack_required,
                        "read_ts": str(read_ts) if read_ts else None,
                        "ack_ts": str(ack_ts) if ack_ts else None,
                        "unread": read_ts is None,
                        "needs_ack": ack_required and ack_ts is None,
                        "acked": ack_ts is not None,
                    }
                    item.update(sender_meta)
                    items.append(item)
            return await _render(
                "mail_inbox.html",
                project={"slug": prow[1], "human_key": prow[2]},
                agent=agent,
                items=items,
                page=page,
                limit=limit,
                next_page=page + 1,
                prev_page=page - 1 if page > 1 else None,
                mail_ui_access=access,
            )

        @fastapi_app.get("/mail/{project}/message/{mid}", response_class=HTMLResponse)
        async def mail_message(project: str, mid: int, request: Request) -> HTMLResponse:
            await ensure_schema()
            async with get_session() as session:
                prow = await _resolve_mail_project(session, project)
                if not prow:
                    return await _render("error.html", status_code=404, message="Project not found")
                pid = int(prow[0])
                access = await _mail_ui_require_project_access(
                    settings=settings,
                    request=request,
                    session=session,
                    project_id=pid,
                )
                mrow = (
                    await session.execute(
                        text(
                            """
                            SELECT
                                m.id,
                                m.subject,
                                m.body_md,
                                s.name AS sender_name,
                                -- The sender's chosen alias, for display beside the
                                -- name. NOT to be confused with _sender_display_name()
                                -- below, which builds "name@project-slug" for
                                -- cross-project senders: two unrelated concepts whose
                                -- names differ by an underscore. Both holzera and I
                                -- reached for that function first when asking whether
                                -- the viewer shows an alias, and it does not.
                                s.display_name AS sender_display_name,
                                s.project_id AS sender_project_id,
                                sp.human_key AS sender_project_name,
                                sp.slug AS sender_project_slug,
                                m.created_ts,
                                m.importance,
                                m.thread_id,
                                m.ack_required,
                                m.attachments
                            FROM messages m
                            JOIN agents s ON s.id = m.sender_id
                            LEFT JOIN projects sp ON sp.id = s.project_id
                            WHERE m.project_id = :pid AND m.id = :mid
                            """
                        ),
                        {"pid": pid, "mid": mid},
                    )
                ).mappings().fetchone()
                if not mrow:
                    return await _render("error.html", message="Message not found")
                recs = await session.execute(
                    text(
                        "SELECT a.name, mr.kind, mr.read_ts, mr.ack_ts "
                        "FROM message_recipients mr JOIN agents a ON a.id = mr.agent_id "
                        "WHERE mr.message_id = :mid"
                    ),
                    {"mid": mid},
                )
                recipients = [
                    {
                        "name": r[0],
                        "kind": r[1],
                        "read_ts": str(r[2]) if r[2] else None,
                        "ack_ts": str(r[3]) if r[3] else None,
                    }
                    for r in recs.fetchall()
                ]
                ack_required_msg = bool(mrow["ack_required"])
                ack_count = sum(1 for r in recipients if r["ack_ts"])
                read_count = sum(1 for r in recipients if r["read_ts"])
                ack_summary = {
                    "ack_required": ack_required_msg,
                    "total": len(recipients),
                    "read": read_count,
                    "acked": ack_count,
                }
                # Find thread messages if thread_id is set
                thread_items: list[dict] = []
                th = mrow["thread_id"]
                if isinstance(th, str) and th.strip():
                    th_rows = await session.execute(
                        text(
                            """
                            SELECT
                                m.id,
                                m.subject,
                                s.name AS sender_name,
                                s.project_id AS sender_project_id,
                                sp.human_key AS sender_project_name,
                                sp.slug AS sender_project_slug,
                                m.created_ts
                            FROM messages m
                            JOIN agents s ON s.id = m.sender_id
                            LEFT JOIN projects sp ON sp.id = s.project_id
                            WHERE m.project_id = :pid AND (m.thread_id = :th OR m.id = :id)
                            ORDER BY m.created_ts ASC
                            """
                        ),
                        {"pid": pid, "th": th, "id": mid},
                    )
                    thread_items = []
                    for rr in th_rows.mappings().all():
                        sender_display, sender_meta = _http_sender_identity(
                            message_project_id=pid,
                            sender_name=rr["sender_name"],
                            sender_project_id=rr["sender_project_id"],
                            sender_project_human_key=rr["sender_project_name"],
                            sender_project_slug=rr["sender_project_slug"],
                        )
                        item = {
                            "id": rr["id"],
                            "subject": rr["subject"],
                            "from": sender_display,
                            "created": str(rr["created_ts"]),
                        }
                        item.update(sender_meta)
                        thread_items.append(item)
            # Convert markdown body to HTML for display (server-side render)
            body_html = (
                markdown2.markdown(mrow["body_md"] or "", extras=["fenced-code-blocks", "tables", "strike", "cuddled-lists"])
                if mrow["body_md"]
                else ""
            )
            if body_html:
                body_html = _html_cleaner.clean(body_html)

            # Get commit SHA for provenance badge
            commit_sha = None
            try:
                settings_local = get_settings()
                archive = await ensure_archive(settings_local, prow[1])
                commit_sha = await get_message_commit_sha(archive, mid)
            except Exception:
                pass  # Commit SHA is optional

            sender_display, sender_meta = _http_sender_identity(
                message_project_id=pid,
                sender_name=mrow["sender_name"],
                sender_project_id=mrow["sender_project_id"],
                sender_project_human_key=mrow["sender_project_name"],
                sender_project_slug=mrow["sender_project_slug"],
            )
            # Parse persisted attachments so the message view can render/link
            # them (#220). Stored as a JSON array column.
            message_attachments: list[dict[str, Any]] = []
            try:
                raw_attachments = mrow["attachments"]
                if isinstance(raw_attachments, str):
                    try:
                        parsed_attachments = json.loads(raw_attachments)
                    except json.JSONDecodeError:
                        parsed_attachments = []
                else:
                    parsed_attachments = raw_attachments
                if isinstance(parsed_attachments, list):
                    message_attachments = [a for a in parsed_attachments if isinstance(a, dict)]
            except Exception:
                message_attachments = []

            message_payload = {
                "id": mrow["id"],
                "subject": mrow["subject"],
                "body_md": mrow["body_md"],
                "body_html": body_html,
                "sender": sender_display,
                # Separate key, never folded into "sender". The template shows the
                # alias beside the address rather than in place of it, and merging
                # them here would take that choice away from the template and put
                # an unaddressable string where every other page shows an
                # addressable one.
                "sender_alias": mrow["sender_display_name"],
                "created": str(mrow["created_ts"]),
                "importance": mrow["importance"],
                "thread_id": mrow["thread_id"],
                "attachments": message_attachments,
            }
            message_payload.update(sender_meta)

            return await _render(
                "mail_message.html",
                project={"slug": prow[1], "human_key": prow[2]},
                message=message_payload,
                recipients=recipients,
                ack_summary=ack_summary,
                thread_items=thread_items,
                commit_sha=commit_sha,
                mail_ui_access=access,
            )

        @fastapi_app.post("/mail/{project}/inbox/{agent}/mark-read")
        async def mark_selected_messages_read(project: str, agent: str, request: Request) -> JSONResponse:
            """Mark specific messages as read for an agent."""
            await ensure_schema()

            try:
                # Parse request body
                request_body = await request.json()
                message_ids: list[int] = request_body.get("message_ids", [])

                if not message_ids:
                    raise HTTPException(status_code=400, detail="No message IDs provided")

                # Limit to prevent SQL parameter overflow (SQLite default limit is 999)
                # Also prevents abuse - if someone wants to mark 1000+ messages, use "mark all"
                if len(message_ids) > 500:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Too many messages selected ({len(message_ids)}). Maximum is 500. Use 'Mark All Read' instead."
                    )

                async with get_session() as session:
                    # Get project
                    prow = await _resolve_mail_project(session, project)
                    if not prow:
                        raise HTTPException(status_code=404, detail="Project not found")

                    pid = int(prow[0])

                    # Get agent
                    arow = (
                        await session.execute(
                            text("SELECT id FROM agents WHERE project_id = :pid AND name = :name"),
                            {"pid": pid, "name": agent},
                        )
                    ).fetchone()
                    if not arow:
                        raise HTTPException(status_code=404, detail="Agent not found")

                    aid = int(arow[0])

                    # Mark specific messages as read
                    # Use naive UTC datetime for SQLite compatibility
                    now = datetime.now(timezone.utc).replace(tzinfo=None)

                    # Use IN clause with parameter binding
                    placeholders = ','.join([f':mid{i}' for i in range(len(message_ids))])
                    params = {"aid": aid, "now": now}
                    params.update({f"mid{i}": mid for i, mid in enumerate(message_ids)})

                    result = await session.execute(
                        text(
                            f"""
                            UPDATE message_recipients
                            SET read_ts = :now
                            WHERE agent_id = :aid
                            AND message_id IN ({placeholders})
                            AND read_ts IS NULL
                            """
                        ),
                        params,
                    )
                    await session.commit()

                    count = int(getattr(result, "rowcount", 0) or 0)

                    return JSONResponse({
                        "success": True,
                        "marked_count": count,
                        "requested_count": len(message_ids),
                        "agent": agent,
                        "project": prow[1],
                    })

            except HTTPException:
                raise
            except Exception as exc:
                import traceback
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"Failed to mark messages as read: {exc!s}") from exc

        @fastapi_app.post("/mail/{project}/inbox/{agent}/mark-all-read")
        async def mark_all_messages_read(project: str, agent: str) -> JSONResponse:
            """Mark all messages for an agent as read."""
            await ensure_schema()

            try:
                async with get_session() as session:
                    # Get project
                    prow = await _resolve_mail_project(session, project)
                    if not prow:
                        raise HTTPException(status_code=404, detail="Project not found")

                    pid = int(prow[0])

                    # Get agent
                    arow = (
                        await session.execute(
                            text("SELECT id FROM agents WHERE project_id = :pid AND name = :name"),
                            {"pid": pid, "name": agent},
                        )
                    ).fetchone()
                    if not arow:
                        raise HTTPException(status_code=404, detail="Agent not found")

                    aid = int(arow[0])

                    # Mark all unread messages as read
                    # Use naive UTC datetime for SQLite compatibility
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    result = await session.execute(
                        text(
                            """
                            UPDATE message_recipients
                            SET read_ts = :now
                            WHERE agent_id = :aid
                            AND read_ts IS NULL
                            """
                        ),
                        {"aid": aid, "now": now},
                    )
                    await session.commit()

                    count = int(getattr(result, "rowcount", 0) or 0)

                    return JSONResponse({
                        "success": True,
                        "marked_count": count,
                        "agent": agent,
                        "project": prow[1],
                    })

            except HTTPException:
                raise
            except Exception as exc:
                import traceback
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"Failed to mark messages as read: {exc!s}") from exc

        @fastapi_app.post("/mail/{project}/inbox/{agent}/delete-messages")
        async def delete_selected_messages(project: str, agent: str, request: Request) -> JSONResponse:
            """Permanently delete specific messages for an agent.

            Removes messages from the SQLite database AND deletes the
            corresponding markdown files from the Git archive so that
            messages do not reappear after a refresh or server restart.
            """
            await ensure_schema()

            try:
                request_body = await request.json()
                message_ids: list[int] = request_body.get("message_ids", [])

                if not message_ids:
                    raise HTTPException(status_code=400, detail="No message IDs provided")

                if len(message_ids) > 500:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Too many messages selected ({len(message_ids)}). Maximum is 500."
                    )

                deleted_count = 0
                messages_to_delete: list[tuple[Any, ...]] = []
                recip_map: dict[int, list[str]] = {}
                async with get_session() as session:
                    # Resolve project
                    prow = await _resolve_mail_project(session, project)
                    if not prow:
                        raise HTTPException(status_code=404, detail="Project not found")

                    pid = int(prow[0])
                    project_slug = prow[1]

                    # Resolve agent
                    arow = (
                        await session.execute(
                            text("SELECT id FROM agents WHERE project_id = :pid AND name = :name"),
                            {"pid": pid, "name": agent},
                        )
                    ).fetchone()
                    if not arow:
                        raise HTTPException(status_code=404, detail="Agent not found")

                    # Fetch message metadata before deleting so we can locate Git files
                    placeholders = ','.join([f':mid{i}' for i in range(len(message_ids))])
                    id_params: dict[str, Any] = {"pid": pid}
                    id_params.update({f"mid{i}": mid for i, mid in enumerate(message_ids)})

                    rows = await session.execute(
                        text(
                            f"""
                            SELECT m.id, m.created_ts, m.subject, s.name AS sender_name
                            FROM messages m
                            JOIN agents s ON s.id = m.sender_id
                            WHERE m.project_id = :pid
                            AND m.id IN ({placeholders})
                            """
                        ),
                        id_params,
                    )
                    messages_to_delete = [tuple(row) for row in rows.fetchall()]

                    if not messages_to_delete:
                        return JSONResponse({"success": True, "deleted_count": 0})

                    # Collect recipient names per message for inbox path removal
                    recip_rows = await session.execute(
                        text(
                            f"""
                            SELECT mr.message_id, a.name
                            FROM message_recipients mr
                            JOIN agents a ON a.id = mr.agent_id
                            WHERE mr.message_id IN ({placeholders})
                            """
                        ),
                        {f"mid{i}": mid for i, mid in enumerate(message_ids)},
                    )
                    for rr in recip_rows.fetchall():
                        recip_map.setdefault(int(rr[0]), []).append(rr[1])

                    # Delete from SQLite: recipients first, then messages
                    await session.execute(
                        text(
                            f"DELETE FROM message_recipients WHERE message_id IN ({placeholders})"
                        ),
                        {f"mid{i}": mid for i, mid in enumerate(message_ids)},
                    )
                    del_result = await session.execute(
                        text(
                            f"DELETE FROM messages WHERE project_id = :pid AND id IN ({placeholders})"
                        ),
                        id_params,
                    )
                    deleted_count = int(getattr(del_result, "rowcount", 0) or 0)
                    await session.commit()

                settings = get_settings()
                git_files_removed = 0
                try:
                    git_files_removed = await _delete_messages_from_archive(
                        settings=settings,
                        project_slug=project_slug,
                        messages_to_delete=messages_to_delete,
                        recip_map=recip_map,
                        commit_message=f"delete: {deleted_count} message(s) via web UI\n",
                    )
                except Exception as archive_exc:
                    # Archive operations are best-effort; DB deletion already happened.
                    logging.getLogger(__name__).warning(
                        "Git archive cleanup failed: %s", archive_exc
                    )

                return JSONResponse({
                    "success": True,
                    "deleted_count": deleted_count,
                    "git_files_removed": git_files_removed,
                    "agent": agent,
                    "project": project_slug,
                })

            except HTTPException:
                raise
            except Exception as exc:
                import traceback
                traceback.print_exc()
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to delete messages: {exc!s}"
                ) from exc

        @fastapi_app.get("/mail/{project}/thread/{thread_id}", response_class=HTMLResponse)
        async def mail_thread(project: str, thread_id: str, request: Request) -> HTMLResponse:
            """Display all messages in a thread chronologically (Gmail-style conversation view).

            NOTE: Currently loads ALL messages in thread without pagination.
            For threads with 1000+ messages, consider adding LIMIT/OFFSET pagination.
            """
            await ensure_schema()
            async with get_session() as session:
                # Get project
                prow = await _resolve_mail_project(session, project)
                if not prow:
                    return await _render("error.html", status_code=404, message="Project not found")

                pid = int(prow[0])
                access = await _mail_ui_require_project_access(
                    settings=settings,
                    request=request,
                    session=session,
                    project_id=pid,
                )

                # Get all messages in this thread, ordered chronologically
                # Include messages where thread_id matches OR message id matches (for thread starter)
                try:
                    thread_id_int = int(thread_id)
                    rows = await session.execute(
                        text(
                            """
                            SELECT
                                m.id,
                                m.subject,
                                m.body_md,
                                s.name AS sender_name,
                                s.project_id AS sender_project_id,
                                sp.human_key AS sender_project_name,
                                sp.slug AS sender_project_slug,
                                m.created_ts,
                                m.importance,
                                m.thread_id
                            FROM messages m
                            JOIN agents s ON s.id = m.sender_id
                            LEFT JOIN projects sp ON sp.id = s.project_id
                            WHERE m.project_id = :pid
                            AND (m.thread_id = :tid OR m.id = :tid_int)
                            ORDER BY m.created_ts ASC
                            """
                        ),
                        {"pid": pid, "tid": thread_id, "tid_int": thread_id_int},
                    )
                except ValueError:
                    # Not an integer, just use string thread_id
                    rows = await session.execute(
                        text(
                            """
                            SELECT
                                m.id,
                                m.subject,
                                m.body_md,
                                s.name AS sender_name,
                                s.project_id AS sender_project_id,
                                sp.human_key AS sender_project_name,
                                sp.slug AS sender_project_slug,
                                m.created_ts,
                                m.importance,
                                m.thread_id
                            FROM messages m
                            JOIN agents s ON s.id = m.sender_id
                            LEFT JOIN projects sp ON sp.id = s.project_id
                            WHERE m.project_id = :pid
                            AND m.thread_id = :tid
                            ORDER BY m.created_ts ASC
                            """
                        ),
                        {"pid": pid, "tid": thread_id},
                    )

                messages = []
                for r in rows.mappings().all():
                    # Convert markdown to HTML for each message
                    body_html = ""
                    if r["body_md"]:
                        body_html = markdown2.markdown(
                            r["body_md"],
                            extras=["fenced-code-blocks", "tables", "strike", "cuddled-lists"]
                        )
                        body_html = _html_cleaner.clean(body_html)

                    sender_display, sender_meta = _http_sender_identity(
                        message_project_id=pid,
                        sender_name=r["sender_name"],
                        sender_project_id=r["sender_project_id"],
                        sender_project_human_key=r["sender_project_name"],
                        sender_project_slug=r["sender_project_slug"],
                    )
                    message = {
                        "id": r["id"],
                        "subject": r["subject"],
                        "body_md": r["body_md"],
                        "body_html": body_html,
                        "sender": sender_display,
                        "created": str(r["created_ts"]),
                        "importance": r["importance"],
                        "thread_id": r["thread_id"],
                    }
                    message.update(sender_meta)
                    messages.append(message)

                if not messages:
                    return await _render(
                        "error.html",
                        message=f"No messages found in thread '{thread_id}'. The thread may not exist or all messages may have been deleted."
                    )

                # Get unique subject (use first message's subject, with fallback)
                thread_subject = messages[0]["subject"] if messages and messages[0]["subject"] else f"Thread {thread_id}"

                return await _render(
                    "mail_thread.html",
                    project={"slug": prow[1], "human_key": prow[2]},
                    thread_id=thread_id,
                    thread_subject=thread_subject,
                    messages=messages,
                    message_count=len(messages),
                    mail_ui_access=access,
                )

        # Full-text search UI across subject/body using LIKE fallback (SQLite FTS handled elsewhere)
        @fastapi_app.get("/mail/{project}/search", response_class=HTMLResponse)
        async def mail_search(
            project: str,
            q: str,
            request: Request,
            limit: int = 10000,
            scope: str | None = None,
            order: str | None = None,
            boost: int | None = None,
        ) -> HTMLResponse:
            limit = min(max(1, limit), 10000)
            if order not in ("relevance", "time", None):
                order = "relevance"
            await ensure_schema()
            async with get_session() as session:
                prow = await _resolve_mail_project(session, project)
                if not prow:
                    return await _render("error.html", status_code=404, message="Project not found")
                pid = int(prow[0])
                access = await _mail_ui_require_project_access(
                    settings=settings,
                    request=request,
                    session=session,
                    project_id=pid,
                )
                fts_expr, like_pat, like_scope, tokens = _parse_fts_query(q, scope)
                weights = (0.0, 3.0, 1.0) if (boost or 0) else (0.0, 1.0, 1.0)
                fts_sql = (
                    "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                    "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                    "m.created_ts, m.importance, m.thread_id, "
                    "snippet(fts_messages, 2, '<mark>', '</mark>', '…', 22) AS body_snippet "
                    "FROM fts_messages "
                    "JOIN messages m ON m.id = fts_messages.rowid "
                    "JOIN agents s ON s.id = m.sender_id "
                    "LEFT JOIN projects sp ON sp.id = s.project_id "
                    "WHERE m.project_id = :pid AND fts_messages MATCH :q "
                    + (
                        "ORDER BY m.created_ts DESC "
                        if (order or "relevance") == "time"
                        else f"ORDER BY bm25(fts_messages, {weights[0]}, {weights[1]}, {weights[2]}) "
                    )
                    + "LIMIT :lim"
                )
                try:
                    rows = await session.execute(text(fts_sql), {"pid": pid, "q": fts_expr or q, "lim": limit})
                    results = []
                    for r in rows.mappings().all():
                        safe_snippet, snippet_hits = _safe_fts_snippet(
                            r["body_snippet"]
                        )
                        sender_display, sender_meta = _http_sender_identity(
                            message_project_id=pid,
                            sender_name=r["sender_name"],
                            sender_project_id=r["sender_project_id"],
                            sender_project_human_key=r["sender_project_name"],
                            sender_project_slug=r["sender_project_slug"],
                        )
                        item = {
                            "id": r["id"],
                            "subject": r["subject"],
                            "from": sender_display,
                            "created": str(r["created_ts"]),
                            "importance": r["importance"],
                            "thread_id": r["thread_id"],
                            "snippet": safe_snippet,
                            "hits": snippet_hits,
                        }
                        item.update(sender_meta)
                        results.append(item)
                except Exception:
                    if like_scope == "subject":
                        like_sql = (
                            "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                            "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                            "m.created_ts, m.importance, m.thread_id "
                            "FROM messages m JOIN agents s ON s.id = m.sender_id "
                            "LEFT JOIN projects sp ON sp.id = s.project_id "
                            f"WHERE m.project_id = :pid AND m.subject LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                            "ORDER BY m.created_ts DESC LIMIT :lim"
                        )
                    elif like_scope == "body":
                        like_sql = (
                            "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                            "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                            "m.created_ts, m.importance, m.thread_id "
                            "FROM messages m JOIN agents s ON s.id = m.sender_id "
                            "LEFT JOIN projects sp ON sp.id = s.project_id "
                            f"WHERE m.project_id = :pid AND m.body_md LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                            "ORDER BY m.created_ts DESC LIMIT :lim"
                        )
                    else:
                        like_sql = (
                            "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                            "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                            "m.created_ts, m.importance, m.thread_id "
                            "FROM messages m JOIN agents s ON s.id = m.sender_id "
                            "LEFT JOIN projects sp ON sp.id = s.project_id "
                            f"WHERE m.project_id = :pid AND (m.subject LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                            f"OR m.body_md LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}') "
                            "ORDER BY m.created_ts DESC LIMIT :lim"
                        )
                    rows = await session.execute(
                        text(like_sql), {"pid": pid, "pat": like_pat or f"%{_like_escape(q)}%", "lim": limit}
                    )
                    results = []
                    for r in rows.mappings().all():
                        sender_display, sender_meta = _http_sender_identity(
                            message_project_id=pid,
                            sender_name=r["sender_name"],
                            sender_project_id=r["sender_project_id"],
                            sender_project_human_key=r["sender_project_name"],
                            sender_project_slug=r["sender_project_slug"],
                        )
                        item = {
                            "id": r["id"],
                            "subject": r["subject"],
                            "from": sender_display,
                            "created": str(r["created_ts"]),
                            "importance": r["importance"],
                            "thread_id": r["thread_id"],
                            "snippet": "",
                            "hits": 0,
                        }
                        item.update(sender_meta)
                        results.append(item)
            return await _render(
                "mail_search.html",
                project={"slug": prow[1], "human_key": prow[2]},
                q=q,
                scope=scope or "",
                order=order or "relevance",
                tokens=tokens,
                results=results,
                boost=bool(boost),
                mail_ui_access=access,
            )

        # File reservations and attachments views
        @fastapi_app.get("/mail/{project}/file_reservations", response_class=HTMLResponse)
        async def mail_file_reservations(project: str, request: Request) -> HTMLResponse:
            await ensure_schema()
            async with get_session() as session:
                prow = await _resolve_mail_project(session, project)
                if not prow:
                    return await _render("error.html", status_code=404, message="Project not found")
                pid = int(prow[0])
                access = await _mail_ui_require_project_access(
                    settings=settings,
                    request=request,
                    session=session,
                    project_id=pid,
                )
                # LEFT JOIN so orphaned reservations whose owning agent row
                # has been deleted still surface in the web UI (`a.name` will
                # be NULL — render as "<orphaned>" so operators see them and
                # can act). Matches the model-side LEFT JOIN in
                # _collect_file_reservation_statuses. (#161)
                rows = await session.execute(
                    text(
                        "SELECT c.id, a.name, c.path_pattern, c.exclusive, c.created_ts, c.expires_ts, c.released_ts, c.agent_id FROM file_reservations c LEFT JOIN agents a ON a.id = c.agent_id WHERE c.project_id = :pid ORDER BY c.created_ts DESC"
                    ),
                    {"pid": pid},
                )
                file_reservations = [
                    {
                        "id": r[0],
                        "agent": r[1] if r[1] is not None else "<orphaned>",
                        "agent_id": r[7],
                        "path_pattern": r[2],
                        "exclusive": bool(r[3]),
                        "created": str(r[4]),
                        "expires": str(r[5]) if r[5] else "",
                        "released": str(r[6]) if r[6] else "",
                    }
                    for r in rows.fetchall()
                ]
            return await _render(
                "mail_file_reservations.html",
                project={"slug": prow[1], "human_key": prow[2]},
                file_reservations=file_reservations,
                mail_ui_access=access,
            )

        @fastapi_app.get("/mail/{project}/attachments", response_class=HTMLResponse)
        async def mail_attachments(project: str, request: Request) -> HTMLResponse:
            await ensure_schema()
            async with get_session() as session:
                prow = await _resolve_mail_project(session, project)
                if not prow:
                    return await _render("error.html", status_code=404, message="Project not found")
                pid = int(prow[0])
                access = await _mail_ui_require_project_access(
                    settings=settings,
                    request=request,
                    session=session,
                    project_id=pid,
                )
                rows = await session.execute(
                    text(
                        "SELECT id, subject, created_ts, attachments FROM messages WHERE project_id = :pid AND json_array_length(attachments) > 0 ORDER BY created_ts DESC LIMIT 10000"
                    ),
                    {"pid": pid},
                )
                items = []
                for r in rows.fetchall():
                    attachments: list[dict[str, Any]] = []
                    try:
                        raw = r[3]
                        if isinstance(raw, str):
                            try:
                                parsed = json.loads(raw)
                            except json.JSONDecodeError:
                                parsed = []
                        else:
                            parsed = raw
                        if isinstance(parsed, list):
                            attachments = [a for a in parsed if isinstance(a, dict)]
                    except Exception:
                        attachments = []
                    items.append({"id": r[0], "subject": r[1], "created": str(r[2]), "attachments": attachments})
            return await _render(
                "mail_attachments.html",
                project={"slug": prow[1], "human_key": prow[2]},
                items=items,
                mail_ui_access=access,
            )

        # ========== Human Overseer Routes ==========

        async def _resolve_overseer_reply(
            session: AsyncSession,
            *,
            message_id: int,
            project_id: int,
        ) -> tuple[list[str], str, str]:
            """Resolve immutable recipient, subject, and thread fields for a reply.

            Args:
                session: Open database session containing the message snapshot.
                message_id: Message being answered or followed up.
                project_id: Project that owns the message.

            Returns:
                Recipient names, reply subject, and thread id.

            Raises:
                HTTPException: If the message is missing, retired, or crosses
                    a routing boundary the Web UI cannot verify.
            """
            original = (
                await session.execute(
                    text(
                        "SELECT m.thread_id, m.subject, a.name, a.project_id, a.retired_at "
                        "FROM messages m JOIN agents a ON a.id = m.sender_id "
                        "WHERE m.id = :mid AND m.project_id = :pid"
                    ),
                    {"mid": message_id, "pid": project_id},
                )
            ).fetchone()
            if original is None:
                raise HTTPException(status_code=404, detail="Reply target was not found in this project")
            if int(original[3]) != project_id:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot reply to a cross-project sender until verified routing is available",
                )

            sender_name = str(original[2])
            if sender_name == "HumanOverseer":
                rows = await session.execute(
                    text(
                        "SELECT a.name, a.project_id, a.retired_at "
                        "FROM message_recipients mr JOIN agents a ON a.id = mr.agent_id "
                        "WHERE mr.message_id = :mid AND mr.kind IN ('to', 'cc') "
                        "ORDER BY CASE mr.kind WHEN 'to' THEN 0 ELSE 1 END, a.name"
                    ),
                    {"mid": message_id},
                )
                recipient_rows = rows.fetchall()
                if any(int(row[1]) != project_id for row in recipient_rows):
                    raise HTTPException(
                        status_code=409,
                        detail="Cannot follow up to cross-project recipients until verified routing is available",
                    )
                retired = sorted(
                    {
                        str(row[0])
                        for row in recipient_rows
                        if str(row[0]) != "HumanOverseer" and row[2] is not None
                    }
                )
                if retired:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Cannot follow up to retired recipients: {', '.join(retired)}",
                    )
                recipients = list(
                    dict.fromkeys(
                        str(row[0])
                        for row in recipient_rows
                        if str(row[0]) != "HumanOverseer"
                    )
                )
                if not recipients:
                    raise HTTPException(
                        status_code=409,
                        detail="This Human Overseer message has no addressable recipients to follow up with",
                    )
            else:
                if original[4] is not None:
                    raise HTTPException(status_code=409, detail="Cannot reply to a retired sender")
                recipients = [sender_name]

            original_subject = str(original[1] or "")
            reply_subject = (
                original_subject
                if original_subject.lower().startswith("re:")
                else f"Re: {original_subject}"
            )
            subject = reply_subject[:200]
            thread_id = str(original[0] or message_id)
            return recipients, subject, thread_id

        @fastapi_app.get("/mail/{project}/overseer/compose", response_class=HTMLResponse)
        async def overseer_compose(
            project: str,
            request: Request,
            reply_to: int | None = None,
        ) -> HTMLResponse:
            """Refuse the legacy composer until overseer writes are atomic.

            Args:
                project: Project slug or canonical human key.
                reply_to: Optional message id whose routing and thread fields
                    are resolved by the server.

            Returns:
                The composer, or an explicit error page when the requested
                reply cannot be routed safely.
            """
            async with get_session() as session:
                # Get project
                prow = await _resolve_mail_project(session, project)
                if not prow:
                    return await _render("error.html", status_code=404, message="Project not found")

                # Retired identities remain visible in project history, but they
                # are not addressable from the human compose surface.
                pid = int(prow[0])
                access = await _mail_ui_require_project_access(
                    settings=settings,
                    request=request,
                    session=session,
                    project_id=pid,
                    operate=reply_to is not None,
                )
                if reply_to is None and not access["can_compose"]:
                    raise HTTPException(
                        status_code=403,
                        detail="Forbidden: new messages require the admin role",
                    )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=_MAIL_LEGACY_OVERSEER_UNAVAILABLE_DETAIL,
                )

                agent_rows = await session.execute(
                    text("SELECT name FROM agents WHERE project_id = :pid AND retired_at IS NULL ORDER BY name"),
                    {"pid": pid}
                )
                agents = [{"name": r[0]} for r in agent_rows.fetchall()]

            prefill: dict[str, Any] = {
                "reply_to": None,
                "thread_id": "",
                "subject": "",
                "recipients": [],
            }
            if reply_to is not None:
                async with get_session() as session:
                    try:
                        reply_recipients, reply_subject, reply_thread_id = await _resolve_overseer_reply(
                            session,
                            message_id=reply_to,
                            project_id=pid,
                        )
                    except HTTPException as exc:
                        return await _render(
                            "error.html",
                            status_code=exc.status_code,
                            message=str(exc.detail),
                        )
                prefill = {
                    "reply_to": reply_to,
                    "thread_id": reply_thread_id,
                    "subject": reply_subject,
                    "recipients": reply_recipients,
                }

            return await _render(
                "overseer_compose.html",
                project={"slug": prow[1], "human_key": prow[2]},
                agents=agents,
                prefill=prefill,
                mail_ui_access=access,
            )

        @fastapi_app.post("/mail/{project}/overseer/reply")
        @fastapi_app.post("/mail/{project}/overseer/send")
        async def overseer_send(project: str, request: Request) -> JSONResponse:
            """Refuse legacy overseer writes until DB and archive commits are atomic."""
            reply_endpoint = request.url.path.endswith("/overseer/reply")
            async with get_session() as authorization_session:
                authorization_project = await _resolve_mail_project(authorization_session, project)
                if authorization_project is None:
                    raise HTTPException(status_code=404, detail="Project not found")
                access = await _mail_ui_require_project_access(
                    settings=settings,
                    request=request,
                    session=authorization_session,
                    project_id=int(authorization_project[0]),
                    operate=reply_endpoint,
                )
                if not reply_endpoint and not access["can_compose"]:
                    raise HTTPException(
                        status_code=403,
                        detail="Forbidden: new messages require the admin role",
                    )

            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_MAIL_LEGACY_OVERSEER_UNAVAILABLE_DETAIL,
            )

            await ensure_schema()

            try:
                try:
                    request_body = await request.json()
                except Exception as exc:
                    raise HTTPException(status_code=400, detail="Request body must be a valid JSON object") from exc
                if not isinstance(request_body, dict):
                    raise HTTPException(status_code=400, detail="Request body must be a JSON object")

                raw_reply_to = request_body.get("reply_to")
                if raw_reply_to is not None and (
                    isinstance(raw_reply_to, bool)
                    or not isinstance(raw_reply_to, int)
                    or raw_reply_to <= 0
                ):
                    raise HTTPException(status_code=400, detail="Reply target must be a positive integer or null")
                reply_to: int | None = raw_reply_to
                if reply_endpoint and reply_to is None:
                    raise HTTPException(status_code=400, detail="Reply target is required")
                if not reply_endpoint and reply_to is not None:
                    raise HTTPException(
                        status_code=400,
                        detail="Replies must use the project reply endpoint",
                    )

                raw_body_md = request_body.get("body_md", "")
                if not isinstance(raw_body_md, str):
                    raise HTTPException(status_code=400, detail="Message body must be a string")
                body_md = raw_body_md.strip()
                if not body_md:
                    raise HTTPException(status_code=400, detail="Message body is required")
                if len(body_md) > 50000:
                    raise HTTPException(status_code=400, detail="Message body too long (maximum 50,000 characters)")

                if reply_to is None:
                    raw_recipients = request_body.get("recipients", [])
                    if not isinstance(raw_recipients, list) or any(
                        not isinstance(name, str) or not name.strip()
                        for name in raw_recipients
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="Recipients must be a list of non-empty strings",
                        )
                    recipients = list(dict.fromkeys(raw_recipients))
                    if len(recipients) > 100:
                        raise HTTPException(status_code=400, detail="Too many recipients (maximum 100 agents)")

                    raw_subject = request_body.get("subject", "")
                    if not isinstance(raw_subject, str):
                        raise HTTPException(status_code=400, detail="Subject must be a string")
                    subject = raw_subject.strip()
                    if len(subject) > 200:
                        raise HTTPException(status_code=400, detail="Subject too long (maximum 200 characters)")

                    raw_thread_id = request_body.get("thread_id")
                    if raw_thread_id is not None and not isinstance(raw_thread_id, str):
                        raise HTTPException(status_code=400, detail="Thread ID must be a string or null")
                    thread_id = raw_thread_id.strip() or None if raw_thread_id is not None else None

                    if not recipients:
                        raise HTTPException(status_code=400, detail="At least one recipient is required")
                    if not subject:
                        raise HTTPException(status_code=400, detail="Subject is required")
                else:
                    recipients = []
                    subject = ""
                    thread_id = None

                # Keep database work and archive work in separate phases so
                # the request never holds a live DB transaction while doing
                # archive/Git I/O.
                from datetime import datetime, timezone
                message_id: int | None = None
                valid_recipients: list[str] = []
                project_slug = ""
                project_generation = ""
                project_human_key = ""
                overseer_name = "HumanOverseer"
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                async with get_immediate_session() as session:
                    # Get project
                    prow = await _resolve_mail_project(session, project)
                    if not prow:
                        raise HTTPException(status_code=404, detail="Project not found")

                    # Extract project info consistently
                    project_id = int(prow[0])
                    project_slug = prow[1]
                    project_human_key = prow[2]
                    project_generation = str(prow[4])

                    # Revalidate the human and derive the effective locale after
                    # BEGIN IMMEDIATE. A revoked session or a changed preference
                    # cannot cross the gap between the early check and this write.
                    correspondence_locale = await _mail_ui_effective_correspondence_locale(
                        settings=settings,
                        request=request,
                        session=session,
                    )
                    access = await _mail_ui_require_project_access(
                        settings=settings,
                        request=request,
                        session=session,
                        project_id=project_id,
                        operate=reply_endpoint,
                    )
                    if not reply_endpoint and not access["can_compose"]:
                        raise HTTPException(
                            status_code=403,
                            detail="Forbidden: new messages require the admin role",
                        )
                    if reply_endpoint and reply_to is None:
                        raise HTTPException(status_code=400, detail="Reply target is required")

                    if reply_to is not None:
                        recipients, subject, thread_id = await _resolve_overseer_reply(
                            session,
                            message_id=reply_to,
                            project_id=project_id,
                        )

                    correspondence_advisory = _mail_ui_correspondence_advisory(
                        correspondence_locale
                    )
                    preamble = f"""---

        🚨 MESSAGE FROM HUMAN OVERSEER 🚨

        This message is from a human operator overseeing this project. Please prioritize the instructions below over your current tasks.

        You should:
        1. Temporarily pause your current work
        2. Complete the request described below
        3. Resume your original plans afterward (unless modified by these instructions)

        The human's guidance supersedes all other priorities.

        {correspondence_advisory}

        ---

        """
                    full_body = preamble + body_md
                    if len(full_body) > 50000:
                        preamble_length = len(preamble)
                        max_user_length = 50000 - preamble_length
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"Message body too long ({len(body_md)} characters). "
                                f"Maximum is {max_user_length} characters to accommodate "
                                f"the overseer preamble ({preamble_length} characters)."
                            ),
                        )

                    placeholders = ", ".join([f":name_{i}" for i in range(len(recipients))])
                    recipient_params: dict[str, Any] = {"pid": project_id}
                    recipient_params.update({f"name_{i}": name for i, name in enumerate(recipients)})
                    recipient_rows = await session.execute(
                        text(
                            f"SELECT id, name, retired_at, agent_generation FROM agents "
                            f"WHERE project_id = :pid AND name IN ({placeholders})"
                        ),
                        recipient_params,
                    )
                    recipient_records = {
                        str(row[1]): (int(row[0]), row[2], str(row[3]))
                        for row in recipient_rows.fetchall()
                    }
                    missing_recipients = [name for name in recipients if name not in recipient_records]
                    retired_recipients = [
                        name
                        for name in recipients
                        if name in recipient_records and recipient_records[name][1] is not None
                    ]
                    if retired_recipients:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "Cannot send to retired recipients: "
                                f"{', '.join(retired_recipients)}. Restore them before sending."
                            ),
                        )
                    if missing_recipients:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Unknown recipients in this project: {', '.join(missing_recipients)}",
                        )

                    recipient_map = {
                        name: record[0]
                        for name, record in recipient_records.items()
                    }
                    valid_recipients = list(recipients)

                    # Get or create "HumanOverseer" agent (with race condition protection)
                    overseer_row = (
                        await session.execute(
                            text("SELECT id, name FROM agents WHERE project_id = :pid AND name = :name"),
                            {"pid": project_id, "name": overseer_name}
                        )
                    ).fetchone()

                    if not overseer_row:
                        # Create HumanOverseer agent (use INSERT OR IGNORE to handle race conditions)
                        await session.execute(
                            text("""
                                INSERT OR IGNORE INTO agents (
                                    project_id,
                                    name,
                                    program,
                                    model,
                                    task_description,
                                    contact_policy,
                                    attachments_policy,
                                    inception_ts,
                                    last_active_ts
                                )
                                VALUES (
                                    :pid,
                                    :name,
                                    :program,
                                    :model,
                                    :task,
                                    :policy,
                                    :attachments_policy,
                                    :ts,
                                    :ts
                                )
                            """),
                            {
                                "pid": project_id,
                                "name": overseer_name,
                                "program": "WebUI",
                                "model": "Human",
                                "task": "Human operator providing guidance and oversight to agents",
                                "policy": "open",
                                "attachments_policy": "auto",
                                # Use naive UTC datetime for SQLite compatibility
                                "ts": datetime.now(timezone.utc).replace(tzinfo=None),
                            },
                        )
                        # Fetch the agent (whether we just created it or another request did)
                        overseer_row = (
                            await session.execute(
                                text("SELECT id, name FROM agents WHERE project_id = :pid AND name = :name"),
                                {"pid": project_id, "name": overseer_name}
                            )
                        ).fetchone()

                        if not overseer_row:
                            raise HTTPException(status_code=500, detail="Failed to create HumanOverseer agent")

                    # Extract overseer_id for later use
                    overseer_id = overseer_row[0]

                    result = await session.execute(
                        text("""
                            INSERT INTO messages (project_id, sender_id, subject, body_md, importance, thread_id, reply_to, created_ts, ack_required)
                            VALUES (:pid, :sid, :subj, :body, :imp, :tid, :reply_to, :ts, :ack)
                            RETURNING id
                        """),
                        {
                            "pid": project_id,
                            "sid": overseer_id,
                            "subj": subject,
                            "body": full_body,
                            "imp": "high",  # Always high importance for overseer
                            "tid": thread_id,
                            "reply_to": reply_to,
                            "ts": now,
                            "ack": False
                        }
                    )
                    message_row = result.fetchone()
                    if not message_row:
                        raise HTTPException(status_code=500, detail="Failed to create message")
                    message_id = message_row[0]

                    # Bulk insert all message_recipients (single executemany call)
                    insert_params = [
                        {"mid": message_id, "aid": recipient_map[name], "kind": "to"}
                        for name in valid_recipients
                    ]
                    await session.execute(
                        text("""
                            INSERT INTO message_recipients (message_id, agent_id, kind)
                            VALUES (:mid, :aid, :kind)
                        """),
                        insert_params
                    )

                    # Update HumanOverseer activity timestamp before commit.
                    await session.execute(
                        text("UPDATE agents SET last_active_ts = :ts WHERE id = :id"),
                        {"ts": now, "id": overseer_id}
                    )

                    await session.commit()

                from .storage import ensure_archive, write_message_bundle

                settings_local = get_settings()
                archive = await ensure_archive(settings_local, project_slug)
                message_dict = {
                    "id": message_id,
                    "thread_id": thread_id,
                    "reply_to": reply_to,
                    "project": project_human_key,
                    "project_slug": project_slug,
                    "from": overseer_name,
                    "to": valid_recipients,
                    "cc": [],
                    "bcc": [],
                    "subject": subject,
                    "importance": "high",
                    "ack_required": False,
                    "created": now.isoformat(),
                    "attachments": [],
                }

                try:
                    async with archive_write_lock(archive):
                        await write_message_bundle(
                            archive,
                            message_dict,
                            full_body,
                            overseer_name,
                            valid_recipients,
                            extra_paths=None,
                            commit_text=f"Human Overseer message: {subject}",
                            sender_outbox_name=overseer_name,
                        )
                except Exception as git_error:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to write message to Git archive: {git_error!s}"
                    ) from git_error

                # This path builds its rows by hand rather than going through
                # _deliver_message, so without this the one message type that
                # most needs to arrive at once — a human telling an agent to
                # stop — would be the only one that never wakes anybody.
                # After the archive write, for the same reason as there: a wake
                # pointing at a message the archive rejected is worse than none.
                for _recipient in valid_recipients:
                    with contextlib.suppress(Exception):
                        hub.publish(
                            project_slug,
                            project_generation,
                            _recipient,
                            recipient_records[_recipient][2],
                            {
                                "kind": "message",
                                "project": project_slug,
                                "agent": _recipient,
                                "id": message_id,
                            },
                        )
                # The viewer too. Without this the one message type composed in
                # the browser is the one the browser never sees arrive.
                with contextlib.suppress(Exception):
                    hub.publish_project(project_slug, project_generation)

                return JSONResponse({
                    "success": True,
                    "message_id": message_id,
                    "recipients": valid_recipients,
                    "sent_at": now.isoformat()
                })

            except HTTPException:
                raise
            except Exception as e:
                import traceback
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"Failed to send message: {e!s}") from e

        # ========== Archive Visualization Routes ==========

        async def _visible_archive_projects(request: Request) -> list[dict[str, Any]]:
            """Return archive project picker rows visible to the current principal.

            Args:
                request: Current HTTP request.

            Returns:
                Visible project rows with their template access mappings.
            """
            await ensure_schema()
            async with get_session() as session:
                visible_roles = await _mail_ui_visible_project_roles(
                    settings=settings,
                    request=request,
                    session=session,
                )
                rows = await session.execute(
                    text("SELECT id, slug, human_key FROM projects ORDER BY human_key")
                )
                projects: list[dict[str, Any]] = []
                for row in rows.fetchall():
                    project_id = int(row[0])
                    if project_id not in visible_roles:
                        continue
                    access = _mail_ui_access_context(
                        settings=settings,
                        request=request,
                        project_id=project_id,
                        project_role=visible_roles[project_id],
                    )
                    projects.append(
                        {
                            "id": project_id,
                            "slug": str(row[1]),
                            "human_key": str(row[2]),
                            "access": access,
                        }
                    )
                return projects

        async def _require_archive_project(
            request: Request,
            project_slug: str,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            """Resolve an archive project and enforce project visibility.

            Args:
                request: Current HTTP request.
                project_slug: Canonical archive slug.

            Returns:
                Project metadata and template permission mappings.

            Raises:
                HTTPException: With 404 when the project is absent or invisible.
            """
            await ensure_schema()
            async with get_session() as session:
                row = (
                    await session.execute(
                        text("SELECT id, slug, human_key FROM projects WHERE slug = :slug"),
                        {"slug": project_slug},
                    )
                ).fetchone()
                if row is None:
                    if not settings.mail_ui.enabled:
                        return {
                            "id": -1,
                            "slug": project_slug,
                            "human_key": project_slug,
                        }, _mail_ui_access_context(
                            settings=settings,
                            request=request,
                            project_id=-1,
                            project_role=None,
                        )
                    raise HTTPException(status_code=404, detail="Project not found")
                access = await _mail_ui_require_project_access(
                    settings=settings,
                    request=request,
                    session=session,
                    project_id=int(row[0]),
                )
                return {
                    "id": int(row[0]),
                    "slug": str(row[1]),
                    "human_key": str(row[2]),
                }, access

        async def _scoped_archive_commits(
            request: Request,
            *,
            limit: int,
        ) -> list[dict[str, Any]]:
            """Return recent commits without crossing project assignments.

            Args:
                request: Current HTTP request.
                limit: Maximum number of merged commits.

            Returns:
                Reverse-chronological visible commit metadata.
            """
            settings_local = get_settings()
            repo_root = await asyncio.to_thread(_expanduser_resolve_path, Path(settings_local.storage.root))
            if not await asyncio.to_thread(_path_exists, repo_root / ".git"):
                return []
            repo = None
            try:
                repo = await asyncio.to_thread(_open_git_repo, repo_root)
                if _mail_ui_request_is_admin(settings=settings_local, request=request):
                    return await get_recent_commits(repo, limit=limit)
                projects = await _visible_archive_projects(request)
                prefixes = tuple(f"projects/{project['slug']}/" for project in projects)
                merged: dict[str, dict[str, Any]] = {}
                for project in projects:
                    rows = await get_recent_commits(
                        repo,
                        limit=limit,
                        project_slug=project["slug"],
                    )
                    for row in rows:
                        merged[str(row["sha"])] = row
                scoped = await _filter_archive_commits_to_prefixes(
                    repo,
                    list(merged.values()),
                    prefixes,
                )
                return sorted(
                    scoped,
                    key=lambda item: str(item.get("date", "")),
                    reverse=True,
                )[:limit]
            finally:
                if repo is not None:
                    await asyncio.to_thread(repo.close)

        async def _filter_archive_commits_to_prefixes(
            repo: Any,
            commits: list[dict[str, Any]],
            prefixes: tuple[str, ...],
        ) -> list[dict[str, Any]]:
            """Keep commits whose every changed path belongs to a visible project.

            Args:
                repo: Open Git archive repository.
                commits: Commit metadata containing full SHA values.
                prefixes: Visible project path prefixes.

            Returns:
                Commit metadata safe to expose to the current principal.
            """

            def filter_commits() -> list[dict[str, Any]]:
                scoped: list[dict[str, Any]] = []
                for row in commits:
                    sha = str(row.get("sha", ""))
                    try:
                        commit = repo.commit(sha)
                        diffs = (
                            commit.parents[0].diff(commit)
                            if commit.parents
                            else commit.diff(NULL_TREE)
                        )
                        changed_paths = {
                            str(path).replace("\\", "/")
                            for diff in diffs
                            for path in (diff.a_path, diff.b_path)
                            if path and path != "/dev/null"
                        }
                    except Exception:
                        continue
                    if changed_paths and all(
                        any(path.startswith(prefix) for prefix in prefixes)
                        for path in changed_paths
                    ):
                        scoped.append(row)
                return scoped

            return await asyncio.to_thread(filter_commits)

        def _archive_graph_from_timeline(
            commits: list[dict[str, Any]],
        ) -> dict[str, list[dict[str, Any]]]:
            """Build a communication graph from already scoped timeline rows.

            Args:
                commits: Timeline rows whose changed paths passed visibility checks.

            Returns:
                Graph nodes and edges derived only from the supplied commits.
            """
            agent_stats: dict[str, dict[str, int]] = {}
            connections: dict[tuple[str, str], int] = {}
            for commit in commits:
                sender = commit.get("sender")
                recipients = commit.get("recipients")
                if not isinstance(sender, str) or not isinstance(recipients, list):
                    continue
                sender_stats = agent_stats.setdefault(sender, {"sent": 0, "received": 0})
                sender_stats["sent"] += 1
                for recipient in recipients:
                    if not isinstance(recipient, str) or not recipient:
                        continue
                    recipient_stats = agent_stats.setdefault(
                        recipient,
                        {"sent": 0, "received": 0},
                    )
                    recipient_stats["received"] += 1
                    edge = (sender, recipient)
                    connections[edge] = connections.get(edge, 0) + 1

            nodes = [
                {
                    "id": agent,
                    "label": agent,
                    "sent": stats["sent"],
                    "received": stats["received"],
                    "total": stats["sent"] + stats["received"],
                }
                for agent, stats in agent_stats.items()
            ]
            edges = [
                {"from": sender, "to": recipient, "count": count}
                for (sender, recipient), count in connections.items()
            ]
            return {"nodes": nodes, "edges": edges}

        def _validate_project_slug(slug: str) -> bool:
            """Validate project slug format to prevent path traversal."""

            # Slugs should only contain lowercase letters, numbers, hyphens, underscores
            # No path separators or relative path components
            if not slug:
                return False
            if slug in (".", "..", "/", "\\"):
                return False
            if "/" in slug or "\\" in slug or ".." in slug:
                return False
            # Should match safe slug pattern
            return bool(_SLUG_VALIDATOR_RE.match(slug))

        @fastapi_app.get("/mail/archive/guide", response_class=HTMLResponse)
        async def archive_guide(request: Request) -> HTMLResponse:
            """Display the archive access guide and overview."""
            settings_local = get_settings()
            projects = await _visible_archive_projects(request)
            if _mail_ui_request_is_admin(settings=settings_local, request=request):
                guide_stats = await asyncio.to_thread(_collect_archive_guide_stats_sync, settings_local)
            else:
                commits = await _scoped_archive_commits(request, limit=10000)
                last_commit_time = "Never"
                if commits:
                    with contextlib.suppress(ValueError, TypeError):
                        last_commit_time = datetime.fromisoformat(
                            str(commits[0]["date"]).replace("Z", "+00:00")
                        ).strftime("%b %d, %Y")
                guide_stats = {
                    "storage_root": "Scoped view",
                    "total_commits": f"{len(commits):,}",
                    "project_count": len(projects),
                    "repo_size": "Scoped view",
                    "last_commit_time": last_commit_time,
                }

            return await _render(
                "archive_guide.html",
                storage_root=guide_stats["storage_root"],
                total_commits=guide_stats["total_commits"],
                project_count=guide_stats["project_count"],
                repo_size=guide_stats["repo_size"],
                last_commit_time=guide_stats["last_commit_time"],
                projects=projects,
            )

        @fastapi_app.get("/mail/archive/activity", response_class=HTMLResponse)
        async def archive_activity(request: Request, limit: int = 50) -> HTMLResponse:
            """Display recent commits across all projects."""
            # Validate and cap limit to prevent DoS
            limit = max(1, min(limit, 500))  # Between 1 and 500

            commits = await _scoped_archive_commits(request, limit=limit)
            return await _render("archive_activity.html", commits=commits)

        @fastapi_app.get("/mail/archive/commit/{sha}", response_class=HTMLResponse)
        async def archive_commit(sha: str, request: Request) -> HTMLResponse:
            """Display detailed commit information with diffs."""
            settings = get_settings()
            repo_root = await asyncio.to_thread(_expanduser_resolve_path, Path(settings.storage.root))
            if not await asyncio.to_thread(_path_exists, repo_root / ".git"):
                return await _render("error.html", message="Archive repository not found")

            repo = None
            try:
                repo = await asyncio.to_thread(_open_git_repo, repo_root)
                if not _mail_ui_request_is_admin(settings=settings, request=request):
                    projects = await _visible_archive_projects(request)
                    prefixes = tuple(f"projects/{project['slug']}/" for project in projects)
                    scoped = await _filter_archive_commits_to_prefixes(
                        repo,
                        [{"sha": sha}],
                        prefixes,
                    )
                    if not scoped:
                        return await _render(
                            "error.html",
                            status_code=404,
                            message="Commit not found",
                        )
                commit = await get_commit_detail(repo, sha)
                return await _render("archive_commit.html", commit=commit)
            except ValueError:
                # Validation errors (bad SHA, etc.)
                return await _render("error.html", message="Invalid commit identifier")
            except Exception:
                # Don't leak error details
                return await _render("error.html", message="Commit not found")
            finally:
                if repo is not None:
                    await asyncio.to_thread(repo.close)

        @fastapi_app.get("/mail/archive/timeline", response_class=HTMLResponse)
        async def archive_timeline(request: Request, project: str | None = None) -> HTMLResponse:
            """Display communication timeline with Mermaid.js visualization."""
            # Validate project slug if provided
            if project and not _validate_project_slug(project):
                return await _render("error.html", message="Invalid project identifier")

            settings = get_settings()
            repo_root = await asyncio.to_thread(_expanduser_resolve_path, Path(settings.storage.root))
            if not await asyncio.to_thread(_path_exists, repo_root / ".git"):
                return await _render("error.html", message="Archive repository not found")

            # Default to first project if not specified
            if not project:
                projects = await _visible_archive_projects(request)
                if not projects:
                    return await _render("error.html", message="No projects found")
                project = str(projects[0]["slug"])

            project_data, access = await _require_archive_project(request, project)
            project_name = project_data["human_key"]

            repo = None
            try:
                repo = await asyncio.to_thread(_open_git_repo, repo_root)
                commits = await get_timeline_commits(repo, project, limit=100)
                if not _mail_ui_request_is_admin(settings=settings, request=request):
                    projects = await _visible_archive_projects(request)
                    prefixes = tuple(f"projects/{row['slug']}/" for row in projects)
                    commits = await _filter_archive_commits_to_prefixes(
                        repo,
                        commits,
                        prefixes,
                    )
                return await _render(
                    "archive_timeline.html",
                    commits=commits,
                    project=project,
                    project_name=project_name,
                    mail_ui_access=access,
                )
            finally:
                if repo is not None:
                    await asyncio.to_thread(repo.close)

        @fastapi_app.get("/mail/archive/browser", response_class=HTMLResponse)
        async def archive_browser(
            request: Request,
            project: str | None = None,
            path: str = "",
        ) -> HTMLResponse:
            """Browse archive files and directories."""
            if not project:
                # Show project selector - requires project parameter
                return await _render("error.html", message="Please select a project to browse")

            # Validate project slug
            if not _validate_project_slug(project):
                return await _render("error.html", message="Invalid project identifier")

            _project_data, access = await _require_archive_project(request, project)
            settings = get_settings()
            archive = await _open_existing_project_archive(settings, project)
            if archive is None:
                return await _render("error.html", message="Project archive not found")
            try:
                tree = await get_archive_tree(archive, path)
                return await _render(
                    "archive_browser.html",
                    tree=tree,
                    project=project,
                    path=path,
                    mail_ui_access=access,
                )
            except ValueError:
                return await _render("error.html", message="Invalid archive path")
            finally:
                await asyncio.to_thread(archive.repo.close)

        @fastapi_app.get("/mail/archive/browser/{project}/file")
        async def archive_browser_file(project: str, path: str, request: Request) -> JSONResponse:
            """Get file content from archive."""
            # Validate project slug
            if not _validate_project_slug(project):
                raise HTTPException(status_code=400, detail="Invalid project identifier")

            await _require_archive_project(request, project)
            try:
                settings = get_settings()
                archive = await _open_existing_project_archive(settings, project)
                if archive is None:
                    raise HTTPException(status_code=404, detail="Project archive not found")
                try:
                    content = await get_file_content(archive, path)
                finally:
                    await asyncio.to_thread(archive.repo.close)

                if content is None:
                    raise HTTPException(status_code=404, detail="File not found")

                return JSONResponse(content=content)
            except ValueError as err:
                # Path validation errors
                raise HTTPException(status_code=400, detail="Invalid file path") from err
            except HTTPException:
                raise
            except Exception as err:
                raise HTTPException(status_code=404, detail="File not found") from err

        @fastapi_app.get("/mail/archive/browser/{project}/download")
        async def archive_browser_download(project: str, path: str, request: Request) -> Response:
            """Download a file from the archive as an attachment (#221)."""
            # Validate project slug
            if not _validate_project_slug(project):
                raise HTTPException(status_code=400, detail="Invalid project identifier")

            await _require_archive_project(request, project)
            try:
                settings = get_settings()
                archive = await _open_existing_project_archive(settings, project)
                if archive is None:
                    raise HTTPException(status_code=404, detail="Project archive not found")
                try:
                    content = await get_file_content(archive, path)
                finally:
                    await asyncio.to_thread(archive.repo.close)

                if content is None:
                    raise HTTPException(status_code=404, detail="File not found")

                # Derive a safe download filename from the (already validated)
                # path's basename; strip any directory components and quotes.
                filename = PurePosixPath(path.replace("\\", "/")).name or "download"
                filename = filename.replace('"', "").replace("\r", "").replace("\n", "")
                return Response(
                    content=content,
                    media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            except ValueError as err:
                # Path validation errors
                raise HTTPException(status_code=400, detail="Invalid file path") from err
            except HTTPException:
                raise
            except Exception as err:
                raise HTTPException(status_code=404, detail="File not found") from err

        @fastapi_app.get("/mail/archive/network", response_class=HTMLResponse)
        async def archive_network(request: Request, project: str | None = None) -> HTMLResponse:
            """Display agent communication network graph."""
            # Validate project slug if provided
            if project and not _validate_project_slug(project):
                return await _render("error.html", message="Invalid project identifier")

            settings = get_settings()
            repo_root = await asyncio.to_thread(_expanduser_resolve_path, Path(settings.storage.root))
            if not await asyncio.to_thread(_path_exists, repo_root / ".git"):
                return await _render("error.html", message="Archive repository not found")

            # Default to first project
            if not project:
                projects = await _visible_archive_projects(request)
                if not projects:
                    return await _render("error.html", message="No projects found")
                project = str(projects[0]["slug"])

            project_data, access = await _require_archive_project(request, project)
            project_name = project_data["human_key"]

            repo = None
            try:
                repo = await asyncio.to_thread(_open_git_repo, repo_root)
                if _mail_ui_request_is_admin(settings=settings, request=request):
                    graph = await get_agent_communication_graph(repo, project, limit=200)
                else:
                    projects = await _visible_archive_projects(request)
                    prefixes = tuple(f"projects/{row['slug']}/" for row in projects)
                    timeline = await get_timeline_commits(repo, project, limit=200)
                    scoped_timeline = await _filter_archive_commits_to_prefixes(
                        repo,
                        timeline,
                        prefixes,
                    )
                    graph = _archive_graph_from_timeline(scoped_timeline)
                return await _render(
                    "archive_network.html",
                    graph=graph,
                    project=project,
                    project_name=project_name,
                    mail_ui_access=access,
                )
            finally:
                if repo is not None:
                    await asyncio.to_thread(repo.close)

        @fastapi_app.get("/mail/api/projects/{project}/agents")
        async def api_project_agents(project: str, request: Request) -> JSONResponse:
            """Get list of agents for a project."""
            # Validate project slug
            if not _validate_project_slug(project):
                raise HTTPException(status_code=400, detail="Invalid project identifier")

            async with get_session() as session:
                # Get project ID
                prow = await _resolve_mail_project(session, project)
                if not prow:
                    raise HTTPException(status_code=404, detail="Project not found")
                await _mail_ui_require_project_access(
                    settings=settings,
                    request=request,
                    session=session,
                    project_id=int(prow[0]),
                )

                # Get agents for this project
                agents_result = await session.execute(
                    text("SELECT name FROM agents WHERE project_id = :pid ORDER BY name"),
                    {"pid": prow[0]}
                )
                agents = [r[0] for r in agents_result.fetchall()]

            return JSONResponse({"agents": agents})

        @fastapi_app.get("/mail/archive/time-travel", response_class=HTMLResponse)
        async def archive_time_travel(request: Request) -> HTMLResponse:
            """Display time-travel interface."""
            project_rows = await _visible_archive_projects(request)
            projects = [row["slug"] for row in project_rows]

            return await _render("archive_time_travel.html", projects=projects)

        @fastapi_app.get("/mail/archive/time-travel/snapshot")
        async def archive_time_travel_snapshot(
            project: str,
            agent: str,
            timestamp: str,
            request: Request,
        ) -> JSONResponse:
            """Get historical inbox snapshot."""
            # Validate project slug
            if not _validate_project_slug(project):
                raise HTTPException(status_code=400, detail="Invalid project identifier")

            # Validate agent name (alphanumeric only)
            if not agent or not _AGENT_NAME_VALIDATOR_RE.match(agent):
                raise HTTPException(status_code=400, detail="Invalid agent name format")

            # Validate timestamp format (basic ISO 8601 check)
            if not timestamp or not _TIMESTAMP_VALIDATOR_RE.match(timestamp):
                raise HTTPException(status_code=400, detail="Invalid timestamp format. Use ISO 8601 format (YYYY-MM-DDTHH:MM)")

            await _require_archive_project(request, project)
            try:
                # Get project archive
                settings = get_settings()
                repo = await _open_existing_project_archive(settings, project)
                if repo is None:
                    return JSONResponse({
                        "messages": [],
                        "snapshot_time": None,
                        "commit_sha": None,
                        "requested_time": timestamp,
                        "error": "Project archive not found",
                    })

                try:
                    # Get historical snapshot
                    snapshot = await get_historical_inbox_snapshot(repo, agent, timestamp, limit=200)
                    return JSONResponse(snapshot)
                finally:
                    await asyncio.to_thread(repo.repo.close)

            except Exception as e:
                # Log error but return empty result rather than failing
                structlog.get_logger("archive").warning(
                    "time_travel_failed",
                    project=project,
                    agent=agent,
                    timestamp=timestamp,
                    error=str(e)
                )
                return JSONResponse({
                    "messages": [],
                    "snapshot_time": None,
                    "commit_sha": None,
                    "requested_time": timestamp,
                    "error": f"Unable to retrieve historical snapshot: {e!s}"
                })


    try:
        _register_mail_ui()
    except Exception as exc:
        # templates/Jinja may be missing in some environments; UI remains optional
        with contextlib.suppress(Exception):
            structlog.get_logger("ui").error("ui_init_failed", error=str(exc))
        pass

    @fastapi_app.exception_handler(RequestValidationError)
    async def _redact_mail_ui_validation(
        request: Request,
        exc: RequestValidationError,
    ) -> Response:
        """Keep secrets and authored mail content out of typed 422 responses."""
        path = request.url.path
        redact_input = (
            path in (_MAIL_PASSWORD_API_PATH, _MAIL_SEARCH_API_PATH)
            or bool(_MAIL_API_COMPOSE_SHAPE_RE.fullmatch(path))
            or bool(_MAIL_API_REPLY_SHAPE_RE.fullmatch(path))
        )
        if not redact_input:
            return await request_validation_exception_handler(request, exc)
        detail = [
            {
                key: error[key]
                for key in ("type", "loc", "msg")
                if key in error
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            {"detail": detail},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    # Keep the auto-generated /openapi.json focused on the real API contract.
    # The browser-facing SSR mail UI and its legacy JSON helpers live under the
    # `/mail` prefix; they are registered for humans, not as part of the typed
    # API contract. The versioned self-service API is the deliberate exception:
    # React clients generate types from it, so its request and response schemas
    # must remain visible while every legacy `/mail` route stays hidden.
    from fastapi.openapi.utils import get_openapi as _get_openapi

    def _custom_openapi() -> dict[str, Any]:
        if fastapi_app.openapi_schema:
            return fastapi_app.openapi_schema
        schema = _get_openapi(
            title=fastapi_app.title,
            version=fastapi_app.version,
            openapi_version=fastapi_app.openapi_version,
            description=fastapi_app.description,
            routes=fastapi_app.routes,
        )
        paths = schema.get("paths")
        if isinstance(paths, dict):
            schema["paths"] = {
                path: item
                for path, item in paths.items()
                if path in _MAIL_ACCOUNT_API_PATHS
                or path.startswith("/mail/api/v1/")
                or not (path == "/mail" or path.startswith("/mail/"))
            }
            security_scheme_name = "MailUiSession"
            components = schema.setdefault("components", {})
            if isinstance(components, dict):
                security_schemes = components.setdefault("securitySchemes", {})
                if isinstance(security_schemes, dict):
                    security_schemes[security_scheme_name] = {
                        "type": "apiKey",
                        "in": "cookie",
                        "name": settings.mail_ui.cookie_name,
                    }
            for path, path_item in schema["paths"].items():
                if not path.startswith("/mail/api/v1/") or not isinstance(
                    path_item, dict
                ):
                    continue
                for method, operation in path_item.items():
                    if method not in {
                        "get",
                        "post",
                        "put",
                        "patch",
                        "delete",
                        "options",
                        "head",
                    } or not isinstance(operation, dict):
                        continue
                    operation["security"] = [{security_scheme_name: []}]
        fastapi_app.openapi_schema = schema
        return schema

    # Install the custom generator (FastAPI's documented extension point for
    # overriding the OpenAPI document); cast keeps the bound-method override
    # explicit for the type checker.
    cast(Any, fastapi_app).openapi = _custom_openapi

    return fastapi_app


def main() -> None:
    """Run the HTTP transport using settings-specified host/port."""

    parser = argparse.ArgumentParser(description="Run the MCP Agent Mail HTTP transport")
    parser.add_argument("--host", help="Override HTTP host", default=None)
    parser.add_argument("--port", help="Override HTTP port", type=int, default=None)
    parser.add_argument("--log-level", help="Uvicorn log level", default="info")
    # Be tolerant of extraneous argv when invoked under test runners
    args, _unknown = parser.parse_known_args()

    settings = get_settings()
    host = args.host or settings.http.host
    port = args.port or settings.http.port

    app = build_http_app(settings)
    # Disable WebSockets when running the service directly; HTTP-only transport
    import inspect as _inspect

    _sig = _inspect.signature(uvicorn.run)
    _kwargs: dict[str, Any] = {"host": host, "port": port, "log_level": args.log_level}
    if "ws" in _sig.parameters:
        _kwargs["ws"] = "none"
    if "forwarded_allow_ips" in _sig.parameters:
        _kwargs["forwarded_allow_ips"] = settings.http.forwarded_allow_ips
    uvicorn.run(app, **_kwargs)


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
