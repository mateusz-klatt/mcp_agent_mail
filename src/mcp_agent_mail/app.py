"""Application factory for the MCP Agent Mail server."""
# ruff: noqa: I001, A002

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import functools
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, AsyncContextManager, Callable, Optional, Union, cast
from urllib.parse import parse_qsl
import uuid

import fastmcp
from fastmcp import Context, FastMCP
from fastmcp.exceptions import (
    ToolError as _FastMCPToolError,
    ValidationError as _FastMCPValidationError,
)
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.server.middleware import Middleware
from pydantic import ValidationError
from git import Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError
from sqlalchemy import and_ as _sa_and, asc as _sa_asc, bindparam, delete as _sa_delete, desc as _sa_desc, exists as _sa_exists, func, or_ as _sa_or, select as _sa_select, text, update as _sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError, NoResultFound, OperationalError, TimeoutError as SATimeoutError
from sqlalchemy.orm import aliased

from . import rich_logger
from .config import Settings, get_settings
from .db import (
    await_database_cleanup_task,
    ensure_schema,
    get_engine,
    get_immediate_session,
    get_query_tracker,
    get_session,
    init_engine,
    retry_on_db_lock,
    start_query_tracking,
    stop_query_tracking,
)
from .delivery import (
    DeliveryActorSnapshot,
    DeliveryAgentSnapshot,
    DeliveryPurpose,
    DeliveryProjectSnapshot,
    DeliveryRecipientSnapshot,
    MessageDeliveryIdempotencyConflictError,
    MessageDeliveryNotFoundError,
    MessageDeliveryProcessingResult,
    MessageDeliveryRequest,
    MessageDeliveryTerminalError,
    MessageDeliveryValidationError,
    accept_message_delivery,
    emit_published_delivery_notifications,
    get_message_delivery_status,
    process_message_delivery,
)
from .guard import install_guard as install_guard_script, uninstall_guard as uninstall_guard_script
from .llm import complete_system_user
from .models import (
    Agent,
    AgentExecution,
    AgentLink,
    BuildSlotArtifactPath,
    BuildSlotArtifactProjection,
    FileReservation,
    Message,
    MessageDelivery,
    MessageDeliveryRecipient,
    MessageRecipient,
    MessageSummary,
    Project,
    ProjectSiblingSuggestion,
    Product,
    ProductProjectLink,
    WindowIdentity,
)
from .storage import (
    _write_json_atomic_sync,
    GitIndexLockError,
    ProjectArchive,
    archive_write_lock,
    clear_notification_signal,
    clear_repo_cache,
    collect_lock_status,
    ensure_archive,
    get_identity_rename_tombstone,
    heal_archive_locks,
    write_agent_profile,
    write_file_reservation_records,
)
from .utils import (
    generate_agent_name,
    package_version,
    safe_build_path_component,
    sanitize_agent_name,
    slugify,
    validate_agent_name_format,
    validate_client_platform_host_agent_id,
    validate_thread_id_format,
)

PathSpec: Any
try:
    from pathspec import PathSpec as _PathSpec
    PathSpec = _PathSpec
except Exception:  # pragma: no cover - optional dependency fallback
    PathSpec = None

logger = logging.getLogger(__name__)

_EXECUTION_LIFECYCLE_PROTOCOL_VERSION = 1
_EXECUTION_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")
_REGISTRATION_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")
_REDACTED_TOOL_ARGUMENT = "***"
_SENSITIVE_TOOL_LOG_KEY_FRAGMENTS = (
    "bearer",
    "capability",
    "credential",
    "password",
    "secret",
    "token",
)


@dataclass(frozen=True, slots=True)
class _SessionAgentBinding:
    """One credential-versioned Agent authorization held by an MCP session."""

    project_id: int
    project_generation: str
    agent_id: int
    agent_generation: str
    registration_token_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class _SessionExecutionBinding:
    """One implicit root execution tied to the Agent credential that bound it."""

    project_generation: str
    agent_id: int
    agent_generation: str
    registration_token_fingerprint: str | None
    execution_id: str


def _registration_token_fingerprint(token: str | None) -> str | None:
    """Return a non-secret version key for an Agent registration capability."""
    normalized = (token or "").strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _redact_tool_log_value(value: Any) -> Any:
    """Return a recursively redacted copy suitable only for diagnostic logs."""
    sensitive_values: set[str] = set()

    def _is_sensitive_key(key: Any) -> bool:
        return any(
            fragment in str(key).casefold()
            for fragment in _SENSITIVE_TOOL_LOG_KEY_FRAGMENTS
        )

    def _collect(item: Any, *, sensitive_context: bool = False) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                _collect(
                    nested,
                    sensitive_context=(
                        sensitive_context or _is_sensitive_key(key)
                    ),
                )
            return
        if isinstance(item, (list, tuple, set, frozenset)):
            for nested in item:
                _collect(nested, sensitive_context=sensitive_context)
            return
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            with suppress(Exception):
                _collect(
                    model_dump(),
                    sensitive_context=sensitive_context,
                )
            return
        if sensitive_context and isinstance(item, str) and item:
            sensitive_values.add(item)

    def _redact(item: Any) -> Any:
        if isinstance(item, Mapping):
            redacted_mapping: dict[Any, Any] = {}
            for key, nested in item.items():
                safe_key = (
                    _REDACTED_TOOL_ARGUMENT
                    if isinstance(key, str)
                    and any(secret in key for secret in sensitive_values)
                    else key
                )
                redacted_mapping[safe_key] = (
                    _REDACTED_TOOL_ARGUMENT
                    if _is_sensitive_key(key)
                    else _redact(nested)
                )
            return redacted_mapping
        if isinstance(item, list):
            return [_redact(nested) for nested in item]
        if isinstance(item, tuple):
            return tuple(_redact(nested) for nested in item)
        if isinstance(item, (set, frozenset)):
            return [_redact(nested) for nested in item]
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            with suppress(Exception):
                return _redact(model_dump())
        if isinstance(item, str) and any(
            secret in item for secret in sensitive_values
        ):
            return _REDACTED_TOOL_ARGUMENT
        return item

    _collect(value)
    return _redact(value)


class _FastMCPSensitiveLogFilter(logging.Filter):
    """Keep FastMCP internals from logging raw MCP tool arguments.

    FastMCP 3 logs the complete argument mapping at DEBUG and includes
    Pydantic's offending input in its validation WARNING before application
    middleware gets a chance to sanitize the client-facing error.  Both paths
    can contain registration or execution capabilities, so sanitize the
    records at their source logger for every transport.
    """

    _mcp_agent_mail_sensitive_log_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "fastmcp.server.auth.oauth_proxy.proxy":
            # OAuth transactions, authorization codes, upstream errors, and
            # callback state are all capabilities or untrusted IdP input. Keep
            # the severity signal while discarding every dynamic detail so a
            # future FastMCP message template cannot reopen the leak.
            record.msg = "FastMCP OAuth proxy event (details redacted)"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            return True
        message_template = record.msg
        if not isinstance(message_template, str):
            return True
        if message_template.endswith(
            "Handler called: call_tool %s with %s"
        ):
            record.msg = "FastMCP tool call received (arguments redacted)"
            record.args = ()
            return True
        if message_template == "Invalid arguments for tool %r: %s":
            record_args = record.args
            if isinstance(record_args, tuple) and record_args:
                record.msg = "Invalid arguments for tool %r (details redacted)"
                record.args = (record_args[0],)
            else:
                record.msg = "Invalid tool arguments (details redacted)"
                record.args = ()
        return True


def _install_fastmcp_sensitive_log_filter() -> None:
    """Install the credential filter once on both FastMCP tool loggers."""

    for logger_name in (
        "fastmcp.server.auth.oauth_proxy.proxy",
        "fastmcp.server.mixins.mcp_operations",
        "fastmcp.server.server",
    ):
        fastmcp_logger = logging.getLogger(logger_name)
        if any(
            getattr(existing, "_mcp_agent_mail_sensitive_log_filter", False)
            for existing in fastmcp_logger.filters
        ):
            continue
        fastmcp_logger.addFilter(_FastMCPSensitiveLogFilter())


def _absolute_project_key_path(value: str) -> PurePosixPath | PureWindowsPath | None:
    """Parse a traversal-free absolute project key without using host semantics."""
    if not value:
        return None
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if ".." in posix_path.parts or ".." in windows_path.parts:
        return None
    if value.startswith("/") and posix_path.is_absolute():
        return posix_path
    if windows_path.is_absolute():
        return windows_path
    if posix_path.is_absolute():
        return posix_path
    return None


def _is_absolute_project_key(value: str) -> bool:
    """Return whether ``value`` is an absolute, traversal-free project key."""
    return _absolute_project_key_path(value) is not None


# ty currently struggles to type SQLModel-mapped SQLAlchemy expressions.
# Provide lightweight wrappers to keep type checking focused on our code.
def select(*entities: Any, **kwargs: Any) -> Any:
    return _sa_select(*entities, **kwargs)


def update(*args: Any, **kwargs: Any) -> Any:
    return _sa_update(*args, **kwargs)


def delete(*args: Any, **kwargs: Any) -> Any:
    return _sa_delete(*args, **kwargs)


def or_(*clauses: Any) -> Any:
    return _sa_or(*clauses)


def and_(*clauses: Any) -> Any:
    return _sa_and(*clauses)


def exists(*args: Any, **kwargs: Any) -> Any:
    return _sa_exists(*args, **kwargs)


def asc(value: Any) -> Any:
    return _sa_asc(value)


def desc(value: Any) -> Any:
    return _sa_desc(value)


@contextlib.contextmanager
def _git_repo(path: str | Path, search_parent_directories: bool = True) -> Any:
    """Context manager for GitPython Repo that ensures proper cleanup.

    GitPython's Repo object opens file handles for index, config, and other files.
    Without explicit cleanup, these accumulate and cause "too many open files" errors
    under heavy load. This context manager ensures repo.close() is always called.

    Usage:
        with _git_repo("/path/to/project") as repo:
            branch = repo.active_branch.name
    """
    repo = None
    try:
        repo = Repo(path, search_parent_directories=search_parent_directories)
        yield repo
    finally:
        if repo is not None:
            with suppress(Exception):
                repo.close()

TOOL_METRICS: defaultdict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "errors": 0})
TOOL_CLUSTER_MAP: dict[str, str] = {}
TOOL_METADATA: dict[str, dict[str, Any]] = {}

RECENT_TOOL_USAGE: deque[tuple[datetime, str, Optional[str], Optional[str]]] = deque(maxlen=4096)

# Return type for a tool that yields a list AND accepts `format`.
#
# `_apply_tool_output_format` replaces the return value with the TOON envelope
# `{format, data, meta}` — an object. FastMCP derives each tool's output schema
# from its return annotation, so a tool annotated `list[dict[str, Any]]` declares
# `{"type": "array"}` and the envelope fails validation. The call does not degrade
# to JSON; it errors out entirely, and the caller gets no result at all.
#
# That made `format="toon"` unusable on exactly the tools an agent reads most —
# fetch_inbox, fetch_topic, fetch_summary, list_contacts — while it worked on every
# object-returning tool, which is why it looked like it worked. Worse, the wrapper
# also fires on `settings.toon_default_format`, so setting TOON_DEFAULT_FORMAT=toon
# server-side would have made the mailbox unreadable for every agent at once.
#
# Widening the annotation keeps both shapes valid instead of silently dropping the
# requested format: a caller asking for TOON gets TOON, a caller asking for nothing
# still gets a list, and both satisfy the declared schema.
ToonableList = Union[list[dict[str, Any]], dict[str, Any]]
JsonArrayResource = Union[ResourceResult, dict[str, Any]]

# Tools that are safe to auto-retry after transient OS-level FD exhaustion (EMFILE).
# Keep this list conservative: do NOT include tools like send_message that can create
# duplicate side effects if re-run after a partial success.
_EMFILE_RETRY_TOOLS: frozenset[str] = frozenset(
    {
        "ensure_project",
        "register_agent",
        "create_agent_identity",
        "fetch_inbox",
        "search_messages",
        "search_messages_product",
        "list_contacts",
        "whois",
    }
)

CLUSTER_SETUP = "infrastructure"
CLUSTER_IDENTITY = "identity"
CLUSTER_MESSAGING = "messaging"
CLUSTER_CONTACT = "contact"
CLUSTER_SEARCH = "search"
CLUSTER_FILE_RESERVATIONS = "file_reservations"
CLUSTER_MACROS = "workflow_macros"
CLUSTER_BUILD_SLOTS = "build_slots"
CLUSTER_PRODUCT = "product_bus"

# Keep crash recovery memory and lock hold time independent of audit history.
# A pass may drain several batches, but every DB read and artifact projection
# has a fixed upper bound.
_EXECUTION_REAPER_BATCH_SIZE = 256
_FILE_RESERVATION_ARCHIVE_BATCH_SIZE = 256
_BUILD_SLOT_RECONCILIATION_BATCH_SIZE = 256

# -------------------------------------------------------------------------------------------------
# Tool Filtering: Predefined profiles for context reduction
# -------------------------------------------------------------------------------------------------
# Each profile maps to a set of clusters or specific tools to include.
# Using profiles can reduce context overhead by up to ~70% for minimal workflows.
#
# Profile definitions:
#   - full: All tools (default, no filtering)
#   - core: Essential tools for typical agent workflows
#   - minimal: Bare minimum for simple message passing
#   - messaging: Focus on messaging without file reservations
#   - custom: User-defined via TOOLS_FILTER_CLUSTERS/TOOLS_FILTER_TOOLS

TOOL_FILTER_PROFILES: dict[str, dict[str, list[str] | set[str]]] = {
    "full": {
        "clusters": [],  # Empty = all clusters
        "tools": [],
    },
    "core": {
        "clusters": [CLUSTER_IDENTITY, CLUSTER_MESSAGING, CLUSTER_FILE_RESERVATIONS, CLUSTER_MACROS],
        "tools": ["health_check", "ensure_project"],
    },
    "minimal": {
        "clusters": [],
        "tools": [
            "health_check",
            "ensure_project",
            "register_agent",
            "send_message",
            "fetch_inbox",
            "acknowledge_message",
        ],
    },
    "messaging": {
        "clusters": [CLUSTER_IDENTITY, CLUSTER_MESSAGING, CLUSTER_CONTACT],
        "tools": ["health_check", "ensure_project", "search_messages"],
    },
}

def _should_expose_tool(tool_name: str, cluster: str, settings: Settings) -> bool:
    """Determine if a tool should be exposed based on filter settings.

    Returns True if the tool should be registered, False if it should be hidden.
    This is evaluated once at server startup, not per-request.
    """
    filter_cfg = settings.tool_filter
    if not filter_cfg.enabled:
        return True  # No filtering, expose all tools

    profile = filter_cfg.profile

    # Custom profile: use explicit clusters/tools from settings
    if profile == "custom":
        clusters_list = filter_cfg.clusters
        tools_list = filter_cfg.tools
        mode = filter_cfg.mode

        # If no explicit filters, expose all
        if not clusters_list and not tools_list:
            return True

        in_cluster = cluster in clusters_list if clusters_list else False
        in_tools = tool_name in tools_list if tools_list else False

        if mode == "include":
            return in_cluster or in_tools
        else:  # exclude
            return not (in_cluster or in_tools)

    # Predefined profile
    if profile == "full":
        return True

    profile_def = TOOL_FILTER_PROFILES.get(profile)
    if not profile_def:
        return True  # Unknown profile, default to exposing

    profile_clusters = profile_def.get("clusters", [])
    profile_tools = profile_def.get("tools", [])

    # If profile_clusters is empty for that profile, only check tools
    if profile_clusters and cluster in profile_clusters:
        return True
    if profile_tools and tool_name in profile_tools:
        return True

    # For profiles with explicit lists, if tool not in any list, don't expose
    return not (profile_clusters or profile_tools)


def _tool_visible_for_settings(tool_name: str, settings: Settings) -> bool:
    return _should_expose_tool(
        tool_name,
        TOOL_CLUSTER_MAP.get(tool_name, "unclassified"),
        settings,
    )


class ToolExecutionError(Exception):
    def __init__(self, error_type: str, message: str, *, recoverable: bool = True, data: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.error_type = error_type
        self.recoverable = recoverable
        self.data = data or {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "type": self.error_type,
                "message": str(self),
                "recoverable": self.recoverable,
                "data": self.data,
            }
        }


def _legacy_execution_rollout_allowed(settings: Settings) -> bool:
    return settings.agent_execution_enforcement_mode == "observe"


def _execution_protocol_warning(version: int) -> str | None:
    if version == _EXECUTION_LIFECYCLE_PROTOCOL_VERSION:
        return None
    return (
        "execution_protocol_upgrade_required: expected lifecycle_protocol_version="
        f"{_EXECUTION_LIFECYCLE_PROTOCOL_VERSION}, received {version}."
    )


def _validate_execution_protocol(
    version: int | None,
    *,
    settings: Settings,
) -> tuple[int, str | None]:
    normalized = 0 if version is None else int(version)
    if normalized < 0:
        raise ToolExecutionError(
            "INVALID_EXECUTION_PROTOCOL",
            "lifecycle_protocol_version must be a non-negative integer.",
            recoverable=False,
            data={"lifecycle_protocol_version": version},
        )
    warning = _execution_protocol_warning(normalized)
    if warning is not None and not _legacy_execution_rollout_allowed(settings):
        raise ToolExecutionError(
            "UNSUPPORTED_EXECUTION_PROTOCOL",
            warning,
            recoverable=True,
            data={
                "supported": [_EXECUTION_LIFECYCLE_PROTOCOL_VERSION],
                "received": normalized,
            },
        )
    return normalized, warning


def _record_tool_error(tool_name: str, exc: Exception) -> None:
    logger.warning(
        "tool_error",
        extra={
            "tool": tool_name,
            "error": type(exc).__name__,
            "error_message": str(exc),
        },
    )


def _register_tool(name: str, metadata: dict[str, Any]) -> None:
    TOOL_CLUSTER_MAP[name] = metadata["cluster"]
    TOOL_METADATA[name] = metadata


def _bind_arguments(signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]) -> inspect.BoundArguments:
    try:
        return signature.bind_partial(*args, **kwargs)
    except TypeError:
        return signature.bind(*args, **kwargs)


def _extract_argument(bound: inspect.BoundArguments, name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    value = bound.arguments.get(name)
    if value is None:
        return None
    return str(value)


def _enforce_capabilities(ctx: Context, required: set[str], tool_name: str) -> None:
    if not required:
        return
    metadata = getattr(ctx, "metadata", {}) or {}
    allowed = metadata.get("allowed_capabilities")
    if allowed is None:
        return
    allowed_set = {str(item) for item in allowed}
    if allowed_set and not required.issubset(allowed_set):
        missing = sorted(required - allowed_set)
        raise ToolExecutionError(
            "CAPABILITY_DENIED",
            f"Tool '{tool_name}' requires capabilities {missing} (allowed={sorted(allowed_set)}).",
            recoverable=False,
            data={"required": missing, "allowed": sorted(allowed_set)},
        )


def _record_recent(tool_name: str, project: Optional[str], agent: Optional[str]) -> None:
    RECENT_TOOL_USAGE.append((datetime.now(timezone.utc), tool_name, project, agent))


def _instrument_tool(
    tool_name: str,
    *,
    cluster: str,
    capabilities: Optional[set[str]] = None,
    complexity: str = "medium",
    agent_arg: Optional[str] = None,
    project_arg: Optional[str] = None,
) -> Callable[[Any], Any]:
    meta = {
        "cluster": cluster,
        "capabilities": sorted(capabilities or {cluster}),
        "complexity": complexity,
        "agent_arg": agent_arg,
        "project_arg": project_arg,
    }
    _register_tool(tool_name, meta)

    def decorator(func: Any) -> Any:
        signature = inspect.signature(func)

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start_time = time.perf_counter()

            metrics = TOOL_METRICS[tool_name]
            metrics["calls"] += 1
            bound = _bind_arguments(signature, args, kwargs)
            ctx = bound.arguments.get("ctx")
            format_value = bound.arguments.get("format")
            raw_log_arguments = {
                key: value
                for key, value in bound.arguments.items()
                if key != "ctx"
            }
            safe_log_context = cast(
                dict[str, Any],
                _redact_tool_log_value(
                    {
                        "arguments": raw_log_arguments,
                        "project": _extract_argument(bound, project_arg),
                        "agent": _extract_argument(bound, agent_arg),
                    }
                ),
            )
            clean_kwargs = cast(
                dict[str, Any],
                safe_log_context["arguments"],
            )
            project_value = cast(Optional[str], safe_log_context["project"])
            agent_value = cast(Optional[str], safe_log_context["agent"])
            # Pre-validate the output `format` BEFORE running the wrapped tool
            # (issue #177). Previously an invalid format was only caught while
            # encoding the result, so the tool's side effects (e.g. sending a
            # message) had already happened before the request was rejected.
            if format_value is not None:
                _normalized_fmt, _fmt_ok = _normalize_output_format(format_value)
                if not _fmt_ok:
                    metrics["errors"] += 1
                    _fmt_exc = ToolExecutionError(
                        "INVALID_ARGUMENT",
                        "Invalid format value. Expected 'json' or 'toon'.",
                        recoverable=True,
                        data={
                            "tool": tool_name,
                            "argument": "format",
                            "provided": clean_kwargs.get("format"),
                        },
                    )
                    # This validation runs before the try/finally, so emit the
                    # structured error log here rather than silently skipping the
                    # instrumentation every other tool-error path goes through (#177).
                    _record_tool_error(tool_name, _fmt_exc)
                    raise _fmt_exc
            if isinstance(ctx, Context) and meta["capabilities"]:
                required_caps = set(cast(list[str], meta["capabilities"]))
                _enforce_capabilities(ctx, required_caps, tool_name)

            # Rich logging: Log tool call start if enabled
            settings = get_settings()
            log_enabled = settings.tools_log_enabled
            log_ctx = None
            query_tracker = get_query_tracker()
            tracker_token = None

            if query_tracker is None and settings.instrumentation_enabled:
                query_tracker, tracker_token = start_query_tracking(
                    slow_ms=float(settings.instrumentation_slow_query_ms),
                )

            if log_enabled:
                try:
                    log_ctx = rich_logger.ToolCallContext(
                        tool_name=tool_name,
                        args=[],
                        kwargs=clean_kwargs,
                        project=project_value,
                        agent=agent_value,
                        start_time=start_time,
                    )
                    rich_logger.log_tool_call_start(log_ctx)
                except Exception:
                    # Logging errors should not break tool execution
                    log_ctx = None

            result = None
            error = None
            pending_validation_error: _FastMCPToolError | None = None
            try:
                try:
                    result = await func(*args, **kwargs)
                except OSError as exc:
                    # Best-effort recovery for EMFILE on safe/idempotent tools.
                    import errno

                    if exc.errno == errno.EMFILE and tool_name in _EMFILE_RETRY_TOOLS:
                        with suppress(Exception):
                            clear_repo_cache()
                        with suppress(Exception):
                            import gc

                            gc.collect()
                        await asyncio.sleep(0.05)
                        result = await func(*args, **kwargs)
                    else:
                        raise
                if format_value is not None or settings.output_format_default or settings.toon_default_format:
                    result = await _apply_tool_output_format(
                        result,
                        ctx=ctx if isinstance(ctx, Context) else None,
                        tool_name=tool_name,
                        settings=settings,
                        format_value=format_value,
                    )
            except ToolExecutionError as exc:
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                error = exc
                raise
            except NoResultFound as exc:
                # Handle agent/project not found errors with helpful messages
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                wrapped_exc = ToolExecutionError(
                    "NOT_FOUND",
                    str(exc),  # Use the original helpful error message
                    recoverable=True,
                    data={"tool": tool_name},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except ValidationError as exc:
                # Pydantic validation raised inside a tool body is a ValueError,
                # so it must be handled before the generic ValueError branch.
                # Do not log or re-raise while the raw exception is active. A
                # broken logging handler calls logging.handleError(), which
                # prints the active exception context to stderr and would expose
                # Pydantic's credential-bearing input even when the LogRecord is
                # sanitized. Defer the safe error until after this except block
                # and the instrumentation finally block have completed.
                metrics["errors"] += 1
                safe_arguments = {
                    key: value
                    for key, value in bound.arguments.items()
                    if key != "ctx"
                }
                safe_exc = _FastMCPToolError(
                    _redacted_validation_message(
                        tool_name,
                        exc,
                        safe_arguments,
                    )
                )
                error = safe_exc
                pending_validation_error = safe_exc
            except ValueError as exc:
                # Invalid argument value
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                wrapped_exc = ToolExecutionError(
                    "INVALID_ARGUMENT",
                    f"Invalid argument value: {exc}. Check that all parameters have valid values.",
                    recoverable=True,
                    data={"tool": tool_name, "error_detail": str(exc)},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except TypeError as exc:
                # Wrong argument type
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                error_msg = str(exc)
                # Try to extract helpful info from TypeError
                hint = ""
                if "got an unexpected keyword argument" in error_msg:
                    hint = " Check parameter names for typos."
                elif "missing" in error_msg and "required" in error_msg:
                    hint = " Ensure all required parameters are provided."
                elif "NoneType" in error_msg:
                    hint = " A required value was None/null."
                wrapped_exc = ToolExecutionError(
                    "TYPE_ERROR",
                    f"Argument type mismatch: {exc}.{hint}",
                    recoverable=True,
                    data={"tool": tool_name, "error_detail": str(exc)},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except KeyError as exc:
                # Missing key/field
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                wrapped_exc = ToolExecutionError(
                    "MISSING_FIELD",
                    f"Missing required field: {exc}. Ensure all required parameters are provided.",
                    recoverable=True,
                    data={"tool": tool_name, "missing_field": str(exc)},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except SATimeoutError as exc:
                # SQLAlchemy pool timeout (QueuePool exhausted)
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                db_settings = settings.database
                wrapped_exc = ToolExecutionError(
                    "DATABASE_POOL_EXHAUSTED",
                    "Database connection pool exhausted. Reduce concurrency or increase pool settings.",
                    recoverable=True,
                    data={
                        "tool": tool_name,
                        "pool_size": db_settings.pool_size,
                        "max_overflow": db_settings.max_overflow,
                        "pool_timeout": db_settings.pool_timeout,
                        "error_detail": str(exc),
                    },
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except TimeoutError as exc:
                # Timeout (database lock, network, etc.)
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                wrapped_exc = ToolExecutionError(
                    "TIMEOUT",
                    f"Operation timed out: {exc}. The server may be under heavy load. Try again in a moment.",
                    recoverable=True,
                    data={"tool": tool_name, "error_detail": str(exc)},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except GitIndexLockError as exc:
                # Git index.lock contention (concurrent git operations)
                # This is an expected error in multi-agent environments
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                wrapped_exc = ToolExecutionError(
                    "GIT_INDEX_LOCK",
                    f"Git repository is temporarily locked by another operation. "
                    f"This is normal in multi-agent environments. "
                    f"Wait a moment and retry. (Attempted {exc.attempts} times before giving up)",
                    recoverable=True,
                    data={
                        "tool": tool_name,
                        "lock_path": str(exc.lock_path),
                        "attempts": exc.attempts,
                    },
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            except OSError as exc:
                # Handle file descriptor exhaustion (EMFILE) with cache cleanup
                import errno
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                if exc.errno == errno.EMFILE:
                    # Clear repo cache to free file handles and allow recovery
                    cleared = clear_repo_cache()
                    wrapped_exc = ToolExecutionError(
                        "RESOURCE_EXHAUSTED",
                        f"Too many open files. Freed {cleared} cached repos. Retry the operation.",
                        recoverable=True,
                        data={"tool": tool_name, "freed_repos": cleared, "error_detail": str(exc)},
                    )
                else:
                    wrapped_exc = ToolExecutionError(
                        "OS_ERROR",
                        f"OS error: {exc}",
                        recoverable=False,
                        data={"tool": tool_name, "errno": exc.errno, "error_detail": str(exc)},
                    )
                error = wrapped_exc
                raise wrapped_exc from exc
            except Exception as exc:
                # Catch-all for unexpected errors - provide helpful categorization
                metrics["errors"] += 1
                _record_tool_error(tool_name, exc)
                error_type = type(exc).__name__
                error_msg = str(exc)

                # Try to categorize common error patterns
                if "database" in error_msg.lower() or "sqlite" in error_msg.lower():
                    error_category = "DATABASE_ERROR"
                    friendly_msg = "A database error occurred. This may be a transient issue - try again."
                    recoverable = True
                elif "lock" in error_msg.lower() or "busy" in error_msg.lower():
                    error_category = "RESOURCE_BUSY"
                    friendly_msg = "Resource is temporarily busy. Wait a moment and try again."
                    recoverable = True
                elif "permission" in error_msg.lower() or "access" in error_msg.lower():
                    error_category = "PERMISSION_ERROR"
                    friendly_msg = f"Access denied: {error_msg}"
                    recoverable = False
                elif "connection" in error_msg.lower() or "network" in error_msg.lower():
                    error_category = "CONNECTION_ERROR"
                    friendly_msg = "Connection error occurred. Check network and try again."
                    recoverable = True
                else:
                    error_category = "UNHANDLED_EXCEPTION"
                    friendly_msg = f"Unexpected error ({error_type}): {error_msg}"
                    recoverable = False

                wrapped_exc = ToolExecutionError(
                    error_category,
                    friendly_msg,
                    recoverable=recoverable,
                    data={"tool": tool_name, "original_error": error_type, "error_detail": error_msg},
                )
                error = wrapped_exc
                raise wrapped_exc from exc
            finally:
                _record_recent(tool_name, project_value, agent_value)

                query_stats = None
                if query_tracker is not None:
                    query_stats = query_tracker.to_dict()

                if query_stats and settings.instrumentation_enabled:
                    logger.info(
                        "tool_query_stats",
                        extra={
                            "tool": tool_name,
                            "project": project_value,
                            "agent": agent_value,
                            "queries": query_stats.get("total", 0),
                            "query_time_ms": query_stats.get("total_time_ms", 0.0),
                            "per_table": query_stats.get("per_table", {}),
                            "slow_query_ms": query_stats.get("slow_query_ms"),
                        },
                    )

                # Rich logging: Log tool call end if enabled
                if log_ctx is not None:
                    try:
                        log_ctx.end_time = time.perf_counter()
                        log_ctx.result = _redact_tool_log_value(result)
                        log_ctx.error = error
                        log_ctx.success = error is None
                        if query_stats:
                            log_ctx.query_stats = query_stats
                        rich_logger.log_tool_call_end(log_ctx)
                    except Exception:
                        # Logging errors should not suppress original exceptions
                        pass

                if tracker_token is not None:
                    stop_query_tracking(tracker_token)

            if pending_validation_error is not None:
                _record_tool_error(tool_name, pending_validation_error)
                raise pending_validation_error from None

            return result

        # Preserve annotations so FastMCP can infer output schema
        with suppress(Exception):
            wrapper.__annotations__ = getattr(func, "__annotations__", {})
        return wrapper

    return decorator


def _tool_metrics_snapshot() -> list[dict[str, Any]]:
    snapshot = []
    for name, data in sorted(TOOL_METRICS.items()):
        metadata = TOOL_METADATA.get(name, {})
        snapshot.append(
            {
                "name": name,
                "calls": data["calls"],
                "errors": data["errors"],
                "cluster": TOOL_CLUSTER_MAP.get(name, "unclassified"),
                "capabilities": metadata.get("capabilities", []),
                "complexity": metadata.get("complexity", "unknown"),
            }
        )
    return snapshot


@functools.lru_cache(maxsize=1)
def _load_capabilities_mapping() -> list[dict[str, Any]]:
    mapping_path = Path(__file__).resolve().parent.parent.parent / "deploy" / "capabilities" / "agent_capabilities.json"
    if not mapping_path.exists():
        return []
    try:
        data = json.loads(mapping_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("capability_mapping.load_failed", extra={"error": str(exc)})
        return []
    agents = data.get("agents", [])
    if not isinstance(agents, list):
        return []
    normalized: list[dict[str, Any]] = []
    for entry in agents:
        if not isinstance(entry, dict):
            continue
        normalized.append(entry)
    return normalized


def _capabilities_for(agent: Optional[str], project: Optional[str]) -> list[str]:
    mapping = _load_capabilities_mapping()
    caps: set[str] = set()
    for entry in mapping:
        entry_agent = entry.get("name")
        entry_project = entry.get("project")
        if agent and entry_agent != agent:
            continue
        if project and entry_project != project:
            continue
        for item in entry.get("capabilities", []):
            if isinstance(item, str):
                caps.add(item)
    return sorted(caps)


def _lifespan_factory(settings: Settings) -> Callable[[FastMCP], AsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastMCP) -> AsyncIterator[None]:
        init_engine(settings)
        heal_summary = await heal_archive_locks(settings)
        if heal_summary.get("locks_removed") or heal_summary.get("metadata_removed"):
            logger.info(
                "archive.healed_on_startup",
                extra={
                    "locks_scanned": heal_summary.get("locks_scanned", 0),
                    "locks_removed": len(heal_summary.get("locks_removed", [])),
                    "metadata_removed": len(heal_summary.get("metadata_removed", [])),
                },
            )
        await ensure_schema(settings)
        execution_reaper_task: asyncio.Task[None] | None = None
        if settings.agent_execution_reaper_enabled:
            execution_reaper_task = asyncio.create_task(
                _agent_execution_reaper_worker(settings),
                name="agent-execution-reaper",
            )
        try:
            yield
        finally:
            if execution_reaper_task is not None:
                execution_reaper_task.cancel()
                with suppress(asyncio.CancelledError):
                    await execution_reaper_task
            # suppress(Exception) leaves CancelledError free to propagate; the
            # finally still clears the repo cache before cancellation re-raises.
            try:
                with suppress(Exception):
                    engine = get_engine()
                    dispose_task = asyncio.create_task(
                        engine.dispose()
                    )
                    await await_database_cleanup_task(dispose_task)
            finally:
                with suppress(BaseException):
                    clear_repo_cache()

    return lifespan


def _iso(dt: Any) -> str:
    """Return ISO-8601 in UTC from datetime or best-effort from string.

    Accepts datetime or ISO-like string; falls back to str(dt) if unknown.
    Naive datetimes (from SQLite) are assumed to be UTC already.
    """
    try:
        if isinstance(dt, str):
            try:
                parsed = datetime.fromisoformat(dt)
                # Handle naive parsed datetimes (assume UTC)
                if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).isoformat()
            except Exception:
                return dt
        if hasattr(dt, "astimezone"):
            # Handle naive datetimes from SQLite (assume UTC)
            if getattr(dt, "tzinfo", None) is None or dt.tzinfo.utcoffset(dt) is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        return str(dt)
    except Exception:
        return str(dt)


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Return a timezone-aware UTC datetime."""
    if dt is None:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _naive_utc(dt: Optional[datetime] = None) -> datetime:
    """Return a naive UTC datetime for SQLite comparisons.

    SQLite stores datetimes without timezone info. When comparing Python
    datetime objects with SQLite DATETIME columns via SQLAlchemy, both must
    be naive to avoid 'can't compare offset-naive and offset-aware datetimes'.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        # Convert to UTC first, then strip timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _max_datetime(*timestamps: Optional[datetime]) -> Optional[datetime]:
    values = [ts for ts in timestamps if ts is not None]
    if not values:
        return None
    return max(values)


_TRUE_FLAG_VALUES: tuple[str, ...] = ("1", "true", "yes", "on", "y")
_FALSE_FLAG_VALUES: tuple[str, ...] = ("0", "false", "no", "off", "n")


def _split_slug_and_query(raw_value: str) -> tuple[str, dict[str, str]]:
    slug, _, query_string = raw_value.partition("?")
    if not query_string:
        return slug, {}
    params = dict(parse_qsl(query_string, keep_blank_values=True))
    return slug, params


def _coerce_flag_to_bool(value: str, *, default: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_FLAG_VALUES:
        return True
    if normalized in _FALSE_FLAG_VALUES:
        return False
    return default


_OUTPUT_FORMAT_AUTO_VALUES: frozenset[str] = frozenset({"", "auto", "default", "none", "null"})
_OUTPUT_FORMAT_ALIASES: dict[str, str] = {
    "application/json": "json",
    "text/json": "json",
    "application/toon": "toon",
    "text/toon": "toon",
}
_TOON_STATS_TOKENS_RE = re.compile(
    "Token estimates:\\s*~(?P<json>\\d+)\\s*\\(JSON\\)\\s*(?:->|\\u2192)\\s*~(?P<toon>\\d+)\\s*\\(TOON\\)"
)
_TOON_STATS_SAVED_RE = re.compile(r"Saved\\s*~(?P<saved>\\d+)\\s*tokens\\s*\\((?P<percent>-?\\d+(?:\\.\\d+)?)%\\)")


@dataclass(frozen=True, slots=True)
class _OutputFormatDecision:
    resolved: str
    source: str
    requested: Optional[str]


def _normalize_output_format(value: Any) -> tuple[Optional[str], bool]:
    if value is None:
        return None, True
    text = str(value).strip().lower()
    if text in _OUTPUT_FORMAT_AUTO_VALUES:
        return None, True
    if text in _OUTPUT_FORMAT_ALIASES:
        text = _OUTPUT_FORMAT_ALIASES[text]
    if text in {"json", "toon"}:
        return text, True
    return None, False


def _resolve_output_format(value: Any, settings: Settings) -> _OutputFormatDecision:
    normalized, ok = _normalize_output_format(value)
    if value is not None and not ok:
        raise ValueError(f"Invalid format '{value}'. Expected 'json' or 'toon'.")
    if normalized:
        return _OutputFormatDecision(resolved=normalized, source="param", requested=normalized)

    default_raw = settings.output_format_default or settings.toon_default_format
    default_normalized, ok = _normalize_output_format(default_raw)
    if default_raw and not ok:
        logger.warning(
            "Invalid output format default; falling back to json",
            extra={"value": default_raw},
        )
    if default_normalized:
        return _OutputFormatDecision(resolved=default_normalized, source="default", requested=default_normalized)
    return _OutputFormatDecision(resolved="json", source="implicit", requested=None)


def _truncate_text(value: str, *, limit: int = 2000) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...(+{len(value) - limit} chars)"


# Hard cap on every --help / --version probe used to identify the TOON
# encoder. 5s is generous for any well-behaved CLI but short enough that a
# hung or hostile binary can't wedge mcp_agent_mail (the encoder request
# degrades cleanly to JSON when identification times out). Exposed as a
# module attribute so tests can override it without forking the function.
_TOON_IDENT_TIMEOUT_SECONDS = 5.0


@functools.lru_cache(maxsize=32)
def _looks_like_toon_rust_encoder(exe: str) -> bool:
    """
    Best-effort guardrail to prevent accidentally using non-toon_rust encoders
    (e.g. the Node.js `toon` CLI or coreutils `tr`).

    Identification uses toon_rust's help and version banners, which are stable
    across installs. Banner identification is *authoritative* — a binary named
    "toon" that identifies itself as toon_rust through its help/version output
    is accepted (issue #163: `cargo install tru` ships a binary named `toon`,
    so a basename-only rejection breaks every local install).

    Lookalikes (the Node.js `toon` CLI, coreutils `tr`, etc.) are still kept
    out: those binaries print neither the toon_rust help marker
    ("reference implementation in rust") nor a toon_rust version banner
    ("tru " / "toon_rust "), so they cannot pass either signal.
    """
    # text=True + errors="replace": subprocess decodes child output via the
    # locale-preferred encoding (UTF-8 on modern Linux, ASCII under LC_ALL=C).
    # A banner with stray high bytes would otherwise raise UnicodeDecodeError,
    # which is *not* an OSError and would escape this guardrail entirely —
    # surfacing as a 500-class error on the TOON encode path instead of a
    # clean reject. errors="replace" preserves the identification substrings
    # we look for without crashing on a non-UTF-8 locale.
    #
    # timeout: bounded via the module-level _TOON_IDENT_TIMEOUT_SECONDS so a
    # hung binary can't wedge mcp_agent_mail. TimeoutExpired is *not* an
    # OSError subclass, so it must be caught explicitly.
    try:
        help_result = subprocess.run(
            [exe, "--help"],
            text=True,
            errors="replace",
            capture_output=True,
            check=False,
            timeout=_TOON_IDENT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    help_text = (help_result.stdout or "") + "\n" + (help_result.stderr or "")
    if "reference implementation in rust" in help_text.lower():
        return True

    try:
        ver_result = subprocess.run(
            [exe, "--version"],
            text=True,
            errors="replace",
            capture_output=True,
            check=False,
            timeout=_TOON_IDENT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    ver_text = ((ver_result.stdout or "") + (ver_result.stderr or "")).strip().lower()
    return ver_text.startswith("tru ") or ver_text.startswith("toon_rust ")


def _toon_command(settings: Settings) -> list[str]:
    raw = (settings.toon_bin or "tru").strip()
    if not raw:
        return ["tru"]
    try:
        cmd = shlex.split(raw)
    except ValueError:
        cmd = [raw]

    # Enforce toon_rust-only encoder usage (never the Node.js `toon` CLI).
    if cmd:
        exe = cmd[0]
        if not _looks_like_toon_rust_encoder(exe):
            raise ValueError(
                f"TOON_BIN resolved to {exe!r}, which does not look like toon_rust "
                f"(expected tru). Refusing to run a non-toon_rust encoder."
            )
    return cmd


def _run_toon_encode(json_payload: str, settings: Settings) -> subprocess.CompletedProcess[str]:
    cmd = [*_toon_command(settings), "--encode"]
    if settings.toon_stats_enabled:
        cmd.append("--stats")
    return subprocess.run(
        cmd,
        input=json_payload,
        text=True,
        capture_output=True,
        check=False,
    )


def _parse_toon_stats(stderr: str) -> Optional[dict[str, Any]]:
    stats: dict[str, Any] = {}
    tokens_match = _TOON_STATS_TOKENS_RE.search(stderr)
    if tokens_match:
        stats["json_tokens"] = int(tokens_match.group("json"))
        stats["toon_tokens"] = int(tokens_match.group("toon"))
    saved_match = _TOON_STATS_SAVED_RE.search(stderr)
    if saved_match:
        stats["saved_tokens"] = int(saved_match.group("saved"))
        stats["saved_percent"] = float(saved_match.group("percent"))
    return stats or None


def _json_fallback(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _dump_json_compact(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_fallback)


def _encode_payload_to_toon_sync(
    payload: Any,
    *,
    settings: Settings,
    tool_name: str,
    source: str,
    requested: str,
) -> dict[str, Any]:
    try:
        json_payload = _dump_json_compact(payload)
    except Exception as exc:
        return {
            "format": "json",
            "data": payload,
            "meta": {
                "requested": requested,
                "source": source,
                "toon_error": f"json serialization failed: {exc}",
            },
        }
    try:
        result = _run_toon_encode(json_payload, settings)
    except ValueError as exc:
        return {
            "format": "json",
            "data": payload,
            "meta": {
                "requested": requested,
                "source": source,
                "toon_error": str(exc),
            },
        }
    except FileNotFoundError as exc:
        return {
            "format": "json",
            "data": payload,
            "meta": {
                "requested": requested,
                "source": source,
                "toon_error": f"TOON encoder not found: {exc}",
            },
        }
    except OSError as exc:
        return {
            "format": "json",
            "data": payload,
            "meta": {
                "requested": requested,
                "source": source,
                "toon_error": f"TOON encoder failed: {exc}",
            },
        }

    if result.returncode != 0:
        return {
            "format": "json",
            "data": payload,
            "meta": {
                "requested": requested,
                "source": source,
                "toon_error": f"TOON encoder exited with {result.returncode}",
                "toon_stderr": _truncate_text(result.stderr or ""),
            },
        }

    toon_text = (result.stdout or "").rstrip("\n")
    try:
        encoder = _toon_command(settings)[0]
    except Exception:
        encoder = "tru"
    meta: dict[str, Any] = {
        "requested": requested,
        "source": source,
        "encoder": encoder,
    }
    stats = _parse_toon_stats(result.stderr or "")
    if stats:
        meta["toon_stats"] = stats
    elif settings.toon_stats_enabled and result.stderr:
        meta["toon_stats_raw"] = _truncate_text(result.stderr)
    return {
        "format": "toon",
        "data": toon_text,
        "meta": meta,
    }


def _extract_structured_payload(result: Any) -> tuple[Any, Optional[Callable[[Any], None]]]:
    if hasattr(result, "structured_content"):
        try:
            payload = result.structured_content

            def _setter(value: Any) -> None:
                result.structured_content = value
                if hasattr(result, "data"):
                    with suppress(Exception):
                        result.data = value

            return payload, _setter
        except Exception:
            return result, None
    if isinstance(result, dict) and "structured_content" in result:
        payload = result.get("structured_content")

        def _setter(value: Any) -> None:
            result["structured_content"] = value

        return payload, _setter
    return result, None


async def _apply_tool_output_format(
    result: Any,
    *,
    ctx: Optional[Context],
    tool_name: str,
    settings: Settings,
    format_value: Any,
) -> Any:
    decision = _resolve_output_format(format_value, settings)
    if decision.resolved != "toon":
        return result

    payload, setter = _extract_structured_payload(result)
    if payload is None:
        return result

    formatted = await asyncio.to_thread(
        _encode_payload_to_toon_sync,
        payload,
        settings=settings,
        tool_name=tool_name,
        source=decision.source,
        requested=decision.requested or "toon",
    )

    # The encoder failed, so hand back exactly what the caller would have got
    # without asking for TOON. Every failure path in _encode_payload_to_toon_sync
    # already gives up on encoding — it returns {"format": "json", "data": payload,
    # meta.toon_error} — but the envelope was applied anyway, which made the damage
    # unconditional while the benefit stayed conditional.
    #
    # That is not cosmetic. `data` holds the payload as an object, and the caller
    # reads fields off the top level, so wrapping moves every one of them out of
    # reach. register_agent stops carrying `registration_token` and `name`, and
    # session_start.sh exits at its name check without a word — while the agent it
    # just created still exists server-side. The machine cannot register again
    # (register_agent for an existing identity needs the token it never received)
    # and nothing anywhere says so. It has burned its own name, permanently, at
    # rc=0. Measured end-to-end by laptop-mac-1 against a throwaway server.
    #
    # TOON_BIN defaults to "tru", which is not installed on an ordinary host, so
    # this is the DEFAULT outcome of switching the feature on — not an edge case.
    #
    # The diagnostic is not dropped, only moved. meta.toon_error was addressed to
    # a caller who cannot act on it — an agent cannot install an encoder on the
    # server — so it goes to the log, where the one person who can fix it will
    # look. The caller gets the answer it asked a question to receive.
    _toon_error = (
        (formatted.get("meta") or {}).get("toon_error")
        if isinstance(formatted, dict)
        else None
    )
    if _toon_error:
        logger.warning(
            "toon_encode.failed",
            extra={"tool": tool_name, "error": str(_toon_error), "source": decision.source},
        )
        return result

    if setter is not None:
        try:
            setter(formatted)
            return result
        except Exception:
            return formatted
    return formatted


def _apply_resource_output_format(
    payload: Any,
    *,
    settings: Settings,
    resource_name: str,
    format_value: Any,
) -> Any:
    decision = _resolve_output_format(format_value, settings)
    if decision.resolved != "toon":
        # FastMCP 3 treats a bare list returned by a resource template as a
        # list of separate ResourceContent items. Our list-shaped resources are
        # one JSON document, so make that wire contract explicit.
        if isinstance(payload, list):
            return ResourceResult(
                contents=[
                    ResourceContent(payload, mime_type="application/json")
                ]
            )
        return payload
    return _encode_payload_to_toon_sync(
        payload,
        settings=settings,
        tool_name=resource_name,
        source=decision.source,
        requested=decision.requested or "toon",
    )


def _extract_format_param(params: dict[str, Any]) -> Optional[str]:
    raw = params.get("format")
    if isinstance(raw, list):
        return raw[0] if raw else None
    return cast(Optional[str], raw)


def _require_project_resource_param(project: Optional[str], *, resource_name: str) -> str:
    if project is None or not project.strip():
        raise ValueError(f"project parameter is required for {resource_name}")
    return project


@dataclass(slots=True)
class FileReservationStatus:
    reservation: FileReservation
    # None when the reservation is orphaned (owning agent row deleted, or
    # agent_id is NULL). Downstream code must treat agent=None as
    # "perpetually inactive, no mail/activity signal possible" and is
    # responsible for ASCII-safe rendering of the missing name. (#161)
    agent: Optional[Agent]
    stale: bool
    stale_reasons: list[str]
    last_agent_activity: Optional[datetime]
    execution_id: Optional[str]
    execution_status: Optional[str]
    execution_parent_id: Optional[str]
    ancestor_execution_ids: list[str]
    orphaned: bool
    legacy_unscoped: bool
    last_execution_activity: Optional[datetime]
    last_mail_activity: Optional[datetime]
    last_fs_activity: Optional[datetime]
    last_git_activity: Optional[datetime]


_GLOB_MARKERS: tuple[str, ...] = ("*", "?", "[")

# Virtual namespace prefixes for non-filesystem reservations (bd-14z)
_VIRTUAL_NS_PREFIXES: tuple[str, ...] = ("tool://", "resource://", "service://")


def _is_virtual_namespace(pattern: str) -> bool:
    """Check if a reservation pattern uses a virtual namespace (not a filesystem path)."""
    return any(pattern.startswith(prefix) for prefix in _VIRTUAL_NS_PREFIXES)


def _contains_glob(pattern: str) -> bool:
    return any(marker in pattern for marker in _GLOB_MARKERS)


def _normalize_pattern(pattern: str) -> str:
    if _is_virtual_namespace(pattern):
        return pattern.strip()
    return pattern.lstrip("/").strip()


def _collect_matching_paths(base: Path, pattern: str) -> list[Path]:
    if _is_virtual_namespace(pattern):
        return []  # Virtual namespaces have no filesystem presence
    if not base.exists():
        return []
    normalized = _normalize_pattern(pattern)
    if not normalized:
        return []
    if _contains_glob(normalized):
        return list(base.glob(normalized))
    candidate = base / normalized
    if not candidate.exists():
        return []
    return [candidate]


def _latest_filesystem_activity(
    paths: Sequence[Path], *, recent_after: Optional[datetime] = None
) -> Optional[datetime]:
    """Latest mtime across ``paths``.

    When ``recent_after`` is supplied, returns as soon as an mtime at or after
    it is observed: the reservation is provably non-stale at that point, so
    continuing to stat the rest (potentially tens of thousands of files under a
    broad glob like ``frontend/**`` over ``node_modules``) only to refine an
    answer that is already "recent" would needlessly block the event loop
    (#240). The returned value is still the max seen so far, so an early return
    never reports an mtime older than the true latest.
    """
    latest: Optional[datetime] = None
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if latest is None or mtime > latest:
            latest = mtime
        if recent_after is not None and mtime >= recent_after:
            return latest
    return latest


def _reservation_repo_pathspec(
    repo: Repo, workspace: Path, pattern: str
) -> Optional[str]:
    """Build a single repo-root-relative git pathspec for a reservation pattern.

    Glob patterns become ``:(glob)`` magic pathspecs so one ``git rev-list``
    walk covers the whole reservation tree, instead of forking ``git`` once per
    glob-matched file (the #240 outage: ``frontend/**`` over ``node_modules``
    expanded to ~56k files → ~56k sequential forks per cleanup tick). Returns
    ``None`` for virtual namespaces (no filesystem/git presence) or when the
    workspace cannot be located inside the repo.
    """
    if _is_virtual_namespace(pattern):
        return None
    normalized = _normalize_pattern(pattern)
    if not normalized:
        return None
    try:
        repo_root = Path(repo.working_tree_dir or "").resolve()
        workspace_rel = workspace.resolve().relative_to(repo_root).as_posix()
    except Exception:
        return None
    rel = normalized if workspace_rel in ("", ".") else f"{workspace_rel}/{normalized}"
    rel = rel.replace("\\", "/")
    # `:(glob)` is required for `**`/`*` to span directory boundaries the same
    # way the pathlib glob expansion (`_collect_matching_paths`) does.
    return f":(glob){rel}" if _contains_glob(normalized) else rel


def _latest_git_activity(repo: Optional[Repo], pathspec: Optional[str]) -> Optional[datetime]:
    """Latest commit time touching the reservation tree, via a SINGLE rev walk.

    ``git rev-list --max-count=1 -- <pathspec>`` returns the most recent commit
    touching any path under the (glob) pathspec; its committed time equals the
    max across every matched file — identical to the old per-file ``max(...)``
    but with one ``git`` fork instead of one per matched file (#240).
    """
    if repo is None or not pathspec:
        return None
    try:
        commit = next(repo.iter_commits(paths=pathspec, max_count=1))
    except StopIteration:
        return None
    except Exception:
        return None
    return datetime.fromtimestamp(commit.committed_date, tz=timezone.utc)


def _compute_reservation_activity(
    workspace: Optional[Path],
    repo: Optional[Repo],
    pattern: str,
    *,
    recent_after: Optional[datetime],
) -> tuple[bool, Optional[datetime], Optional[datetime]]:
    """Blocking filesystem+git activity probe for one reservation.

    Returns ``(matched, fs_activity, git_activity)``. Kept fully synchronous so
    the async sweeper can run it via ``asyncio.to_thread`` — even a pathological
    workspace (huge glob expansion) then stays off the event loop entirely
    (#240).
    """
    if workspace is None:
        return False, None, None
    matches = _collect_matching_paths(workspace, pattern)
    if not matches:
        return False, None, None
    fs_activity = _latest_filesystem_activity(matches, recent_after=recent_after)
    git_pathspec = _reservation_repo_pathspec(repo, workspace, pattern) if repo is not None else None
    git_activity = _latest_git_activity(repo, git_pathspec)
    return True, fs_activity, git_activity


def _project_workspace_path(project: Project) -> Optional[Path]:
    try:
        candidate = Path(project.human_key).expanduser()
    except Exception:
        return None
    with suppress(OSError):
        if candidate.exists():
            return candidate
    return None


def _open_repo_if_available(workspace: Optional[Path]) -> Optional[Repo]:
    if workspace is None:
        return None
    try:
        repo = Repo(workspace, search_parent_directories=True)
    except (InvalidGitRepositoryError, NoSuchPathError):
        return None
    except Exception:
        return None
    try:
        root = Path(repo.working_tree_dir or "")
    except Exception:
        # Close repo before returning None to avoid file handle leak
        with suppress(Exception):
            repo.close()
        return None
    with suppress(Exception):
        workspace.resolve().relative_to(root.resolve())
        return repo
    # Close repo before returning None to avoid file handle leak
    with suppress(Exception):
        repo.close()
    return None


def _parse_json_safely(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction supporting code fences and stray text.

    Returns parsed dict on success, otherwise None.
    """
    import json as _json
    import re as _re

    try:
        parsed = _json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    # Code fence block
    m = _re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if m:
        inner = m.group(1)
        try:
            parsed = _json.loads(inner)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    # Braces slice heuristic
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = text[start : end + 1]
        try:
            parsed = _json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _parse_iso(raw_value: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 timestamps, accepting a trailing 'Z' as UTC.

    Returns None when parsing fails.
    """
    if raw_value is None:
        return None
    s = raw_value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _validate_iso_timestamp(raw_value: Optional[str], param_name: str = "timestamp") -> Optional[datetime]:
    """Parse and validate an ISO-8601 timestamp, raising helpful error on failure.

    Unlike _parse_iso which silently returns None on failure, this function
    raises a descriptive ToolExecutionError to help agents understand what
    format is expected.

    Parameters
    ----------
    raw_value : Optional[str]
        The timestamp string to parse.
    param_name : str
        The parameter name to include in error messages.

    Returns
    -------
    Optional[datetime]
        Parsed datetime, or None if raw_value was None/empty.

    Raises
    ------
    ToolExecutionError
        If the value is provided but cannot be parsed as ISO-8601.
    """
    if raw_value is None:
        return None
    s = raw_value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        raise ToolExecutionError(
            error_type="INVALID_TIMESTAMP",
            message=(
                f"Invalid {param_name} format: '{raw_value}'. "
                f"Expected ISO-8601 format like '2025-01-15T10:30:00+00:00' or '2025-01-15T10:30:00Z'. "
                f"Common mistakes: missing timezone (add +00:00 or Z), using slashes instead of dashes, "
                f"or using 12-hour format without AM/PM."
            ),
            recoverable=True,
            data={"provided": raw_value, "expected_format": "YYYY-MM-DDTHH:MM:SS+HH:MM"},
        ) from None


def _validate_limit(limit: int, *, param_name: str = "limit", max_limit: int = 1000) -> int:
    """Validate and clamp a result-set `limit` to shared bounds.

    Rejects values below 1 with a recoverable ``INVALID_LIMIT`` error and clamps
    values above ``max_limit`` (default 1000). This is the single source of truth
    for limit bounds shared across messaging/search tools and the resource
    handlers so they cannot diverge (issues #178, #191, #202).
    """
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ToolExecutionError(
            error_type="INVALID_LIMIT",
            message=f"{param_name} must be an integer, got {limit!r}.",
            recoverable=True,
            data={"provided": limit, "min": 1, "max": max_limit},
        )
    if limit < 1:
        raise ToolExecutionError(
            error_type="INVALID_LIMIT",
            message=f"{param_name} must be at least 1, got {limit}. Use a positive integer.",
            recoverable=True,
            data={"provided": limit, "min": 1, "max": max_limit},
        )
    if limit > max_limit:
        return max_limit
    return limit


def _parse_resource_limit(
    parsed: dict[str, list[str]],
    *,
    default: int,
    key: str = "limit",
    max_limit: int = 1000,
) -> int:
    """Parse and validate a resource query-string ``limit`` value.

    Resource handlers historically did ``limit = int(parsed["limit"][0])``
    inside ``suppress(Exception)`` with no bounds, so a malformed or negative
    value was silently ignored (falling back to an unbounded ``.limit(limit)``
    against the DB). Route every resource limit through the same
    ``_validate_limit`` bounds the tools enforce so the resource and tool
    surfaces cannot diverge (issue #178).
    """
    raw = parsed.get(key)
    if not raw:
        return _validate_limit(default, param_name=key, max_limit=max_limit)
    try:
        candidate = int(raw[0])
    except (TypeError, ValueError):
        raise ToolExecutionError(
            error_type="INVALID_LIMIT",
            message=f"{key} must be an integer, got {raw[0]!r}.",
            recoverable=True,
            data={"provided": raw[0], "min": 1, "max": max_limit},
        ) from None
    return _validate_limit(candidate, param_name=key, max_limit=max_limit)


def _validate_program_model(program: str, model: str) -> None:
    """Validate that program and model are non-empty strings.

    Raises
    ------
    ToolExecutionError
        If program or model is empty or whitespace-only.
    """
    if not program or not program.strip():
        raise ToolExecutionError(
            error_type="EMPTY_PROGRAM",
            message=(
                "program cannot be empty. Provide the name of your AI coding tool "
                "(e.g., 'claude-code', 'codex-cli', 'cursor', 'cline')."
            ),
            recoverable=True,
            data={"provided": program},
        )
    if not model or not model.strip():
        raise ToolExecutionError(
            error_type="EMPTY_MODEL",
            message=(
                "model cannot be empty. Provide the underlying model identifier "
                "(e.g., 'claude-opus-4.5', 'gpt-4-turbo', 'claude-sonnet-4')."
            ),
            recoverable=True,
            data={"provided": model},
        )


def _validate_thread_id(raw_value: Optional[str]) -> Optional[str]:
    """Normalize and validate a thread_id used for DB indexing and thread digests."""
    if raw_value is None:
        return None
    thread = raw_value.strip()
    if not thread:
        return None
    if not validate_thread_id_format(thread):
        raise ToolExecutionError(
            error_type="INVALID_THREAD_ID",
            message=(
                f"Invalid thread_id: '{raw_value}'. Thread IDs must start with an alphanumeric character and "
                "contain only letters, numbers, '.', '_', or '-' (max 128). "
                "Examples: 'TKT-123', 'bd-42', 'feature-xyz'."
            ),
            recoverable=True,
            data={"provided": raw_value, "examples": ["TKT-123", "bd-42", "feature-xyz"]},
        )
    return thread


# Patterns that are unsearchable in FTS5 - return None to signal "no results"
_FTS5_UNSEARCHABLE_PATTERNS = frozenset({"*", "**", "***", ".", "..", "...", "?", "??", "???", ""})
_LIKE_FALLBACK_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}")
_LIKE_FALLBACK_STOPWORDS = frozenset({"AND", "OR", "NOT", "NEAR"})

# Regex to detect hyphenated tokens that need quoting for FTS5
# Matches: POL-358, FEAT-123, foo-bar-baz, A-1
# Does not match: already-in-quotes, has spaces, etc.
_FTS5_HYPHENATED_TOKEN_RE = re.compile(r"(?<!\")([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)(?!\")")


def _quote_hyphenated_tokens(query: str) -> str:
    """Quote hyphenated tokens in an FTS5 query to treat hyphens as literals.

    FTS5 interprets hyphens as syntax operators. This function detects
    hyphenated tokens (like POL-358, FEAT-123) that are not already quoted
    and wraps them in double quotes for literal matching.

    Parameters
    ----------
    query : str
        The FTS5 query string.

    Returns
    -------
    str
        The query with hyphenated tokens quoted.

    Examples
    --------
    >>> _quote_hyphenated_tokens("POL-358")
    '"POL-358"'
    >>> _quote_hyphenated_tokens("search for FEAT-123 and bd-42")
    'search for "FEAT-123" and "bd-42"'
    >>> _quote_hyphenated_tokens('"already-quoted"')
    '"already-quoted"'
    """
    if not query or "-" not in query:
        return query

    # Don't modify queries that are entirely within quotes
    if query.startswith('"') and query.endswith('"') and query.count('"') == 2:
        return query

    # Replace unquoted hyphenated tokens with quoted versions
    return _FTS5_HYPHENATED_TOKEN_RE.sub(r'"\1"', query)


_LIKE_ESCAPE_CHAR = "!"


def _like_escape(term: str) -> str:
    """Escape LIKE wildcards for literal substring matching."""
    return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _extract_like_terms(query: str, *, max_terms: int = 5) -> list[str]:
    """Extract LIKE fallback terms from a raw search query."""
    if not query:
        return []
    terms: list[str] = []
    for token in _LIKE_FALLBACK_TOKEN_RE.findall(query):
        if len(token) < 2:
            continue
        if token.upper() in _LIKE_FALLBACK_STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
        if len(terms) >= max_terms:
            break
    return terms


def _sanitize_fts_query(query: str) -> str | None:
    """Sanitize an FTS5 query string, fixing common issues where possible.

    SQLite FTS5 has specific syntax requirements. This function attempts to
    fix common mistakes rather than throwing errors. Returns None when the
    query cannot produce meaningful results (caller should return empty list).

    Fixes applied:
    - Strips whitespace
    - Removes leading bare `*` (keeps `term*` prefix patterns)
    - Converts unsearchable patterns to None (empty results)
    - Quotes hyphenated tokens (e.g., POL-358 → "POL-358") to prevent FTS5
      from interpreting the hyphen as a syntax operator

    Parameters
    ----------
    query : str
        The FTS5 query string to sanitize.

    Returns
    -------
    str | None
        The sanitized query string, or None if the query cannot produce results.
        When None is returned, the caller should return an empty result list
        instead of executing the query.
    """
    if not query:
        return None

    trimmed = query.strip()

    if not trimmed:
        return None

    # Check for bare patterns that can't match anything meaningful in FTS5
    if trimmed in _FTS5_UNSEARCHABLE_PATTERNS:
        return None

    # Bare boolean operators without terms - can't search
    upper_trimmed = trimmed.upper()
    if upper_trimmed in {"AND", "OR", "NOT"}:
        return None

    # FTS5 doesn't support leading wildcards (*foo), only trailing (foo*).
    # Strip leading "*" regardless of what follows: "*foo" -> "foo", "* bar" -> "bar"
    if trimmed.startswith("*"):
        if len(trimmed) == 1:
            return None
        # Strip leading "*" (and any following whitespace) and recurse
        return _sanitize_fts_query(trimmed[1:].lstrip())

    # Fix trailing lone asterisks that aren't part of prefix patterns
    # e.g., "foo *" -> "foo"
    if trimmed.endswith(" *"):
        trimmed = trimmed[:-2].rstrip()
        if not trimmed:
            return None

    # Multiple consecutive spaces -> single space
    trimmed = re.sub(r" {2,}", " ", trimmed)

    # Quote hyphenated tokens to prevent FTS5 from interpreting hyphens as operators
    # e.g., "POL-358" would otherwise fail with "no such column: 358"
    trimmed = _quote_hyphenated_tokens(trimmed)

    return trimmed if trimmed else None


def _rich_error_panel(title: str, payload: dict[str, Any]) -> None:
    """Render a compact JSON error panel if Rich is available and tools logging is enabled."""
    try:
        if not get_settings().tools_log_enabled:
            return
        import importlib as _imp
        _rc = _imp.import_module("rich.console")
        _rj = _imp.import_module("rich.json")
        Console = _rc.Console
        JSON = _rj.JSON
        Console().print(JSON.from_data({"title": title, **payload}))
    except Exception:
        return


def _project_to_dict(project: Project) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": project.id,
        "slug": project.slug,
        "human_key": project.human_key,
        "project_uid": project.project_uid,
        "created_at": _iso(project.created_at),
    }
    if getattr(project, "archived_at", None) is not None:
        d["archived_at"] = _iso(project.archived_at)
    return d


def _agent_to_dict(agent: Agent) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": agent.id,
        "name": agent.name,
        "program": agent.program,
        "model": agent.model,
        "task_description": agent.task_description,
        "inception_ts": _iso(agent.inception_ts),
        "last_active_ts": _iso(agent.last_active_ts),
        "project_id": agent.project_id,
        "attachments_policy": getattr(agent, "attachments_policy", "auto"),
    }
    if getattr(agent, "retired_at", None) is not None:
        d["retired_at"] = _iso(agent.retired_at)
    # Emitted only when set, so no consumer has to distinguish "" from absent.
    # This function is the funnel for register_agent, whois, list_contacts, the
    # agents resource and the archived profile, so one line here reaches every
    # place an agent is described.
    if getattr(agent, "display_name", None):
        d["display_name"] = agent.display_name
    if getattr(agent, "notify_sound", None):
        d["notify_sound"] = agent.notify_sound
    return d


def _agent_execution_to_dict(
    execution: AgentExecution,
    *,
    ancestor_execution_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Serialize an execution lifetime without exposing ORM implementation details."""
    return {
        "id": execution.id,
        "project_id": execution.project_id,
        "agent_id": execution.agent_id,
        "parent_execution_id": execution.parent_execution_id,
        "ancestor_execution_ids": list(ancestor_execution_ids),
        "external_id": execution.external_id,
        "client_name": execution.client_name,
        "turn_id": execution.turn_id,
        "agent_type": execution.agent_type,
        "model": execution.model,
        "permission_mode": execution.permission_mode,
        "lifecycle_protocol_version": execution.lifecycle_protocol_version,
        "kind": execution.kind,
        "status": execution.status,
        "task_description": execution.task_description,
        "cwd": execution.cwd,
        "repo_root": execution.repo_root,
        "git_common_dir": execution.git_common_dir,
        "worktree_path": execution.worktree_path,
        "branch": execution.branch,
        "head_sha": execution.head_sha,
        "started_ts": _iso(execution.started_ts),
        "last_active_ts": _iso(execution.last_active_ts),
        "ended_ts": _iso(execution.ended_ts),
    }


def _bounded_execution_text(
    field: str,
    value: str | None,
    max_length: int,
    *,
    required: bool = False,
) -> str | None:
    """Normalize execution metadata before it reaches DB CHECK constraints."""
    if value is None:
        normalized = None
    else:
        normalized = value.strip()
        if not normalized:
            normalized = None
    if required and normalized is None:
        raise ToolExecutionError(
            "INVALID_EXECUTION",
            f"{field} must be non-empty.",
            data={"field": field},
        )
    if normalized is not None and len(normalized) > max_length:
        raise ToolExecutionError(
            "EXECUTION_METADATA_TOO_LONG",
            f"{field} exceeds the maximum length of {max_length} characters.",
            data={"field": field, "max_length": max_length},
        )
    return normalized


def _message_to_dict(message: Message, include_body: bool = True) -> dict[str, Any]:
    data = {
        "id": message.id,
        "delivery_id": message.delivery_id,
        "project_id": message.project_id,
        "sender_id": message.sender_id,
        "thread_id": message.thread_id,
        # #188: surface the persisted parent→child reply edge so the response
        # reflects STORED data, not a value reconstructed only in the response.
        "reply_to": message.reply_to,
        "topic": message.topic,
        "subject": message.subject,
        "importance": message.importance,
        "ack_required": message.ack_required,
        "created_ts": _iso(message.created_ts),
        "attachments": message.attachments,
    }
    if include_body:
        data["body_md"] = message.body_md
    return data


def _public_runtime_descriptor(settings: Settings) -> dict[str, Any]:
    """Return only non-secret runtime coordinates safe for public diagnostics.

    `version` is here because its absence had a measurable cost: with no
    application version on any public probe, a deploy audit fell back to
    MCP `serverInfo.version` -- which reports FastMCP's version, not ours --
    and read a current production as a stale one.
    """
    descriptor: dict[str, Any] = {
        "version": package_version(),
        "environment": settings.environment,
        "http_host": settings.http.host,
        "http_port": settings.http.port,
        "http_path": settings.http.path,
    }
    commit = settings.build_commit
    if commit:
        descriptor["commit"] = commit
    return descriptor


def _authenticated_build_descriptor(settings: Settings) -> dict[str, str | None]:
    """Return immutable build identity for authenticated diagnostics."""
    return {
        "application_version": package_version(),
        "fastmcp_version": fastmcp.__version__,
        "git_sha": settings.build_commit,
    }


def _format_cross_project_agent_address(project_slug: str, agent_name: str) -> str:
    return f"project:{project_slug}#{agent_name}"


def _sender_display_name(
    *,
    message_project_id: int | None,
    sender_name: str,
    sender_project_id: int | None,
    sender_project_slug: str | None,
) -> str:
    if (
        message_project_id is None
        or sender_project_id is None
        or sender_project_id == message_project_id
        or not sender_project_slug
    ):
        return sender_name
    return f"{sender_name}@{sender_project_slug}"


def _apply_sender_identity(
    payload: dict[str, Any],
    *,
    message_project_id: int | None,
    sender_name: str,
    sender_project_id: int | None,
    sender_project_human_key: str | None,
    sender_project_slug: str | None,
) -> None:
    payload["from"] = sender_name
    if (
        message_project_id is None
        or sender_project_id is None
        or sender_project_id == message_project_id
    ):
        return
    if sender_project_human_key:
        payload["from_project"] = sender_project_human_key
    if sender_project_slug:
        payload["from_project_slug"] = sender_project_slug
        payload["from_address"] = _format_cross_project_agent_address(
            sender_project_slug,
            sender_name,
        )


def _normalize_git_remote(url: Optional[str]) -> Optional[str]:
    """Normalize a git remote URL to a privacy-safe ``host/owner/repo`` string.

    Single shared implementation used by every identity call site so that the
    project slug and project UID can never disagree. Supports SCP-like
    (``git@host:owner/repo.git``) and URL forms, strips a trailing ``.git``,
    collapses duplicate slashes, and keeps the LAST two path segments so nested
    group paths (e.g. GitLab subgroups ``group/subgroup/repo``) normalize to the
    actual ``owner/repo`` rather than the top-level group.
    """
    if not url:
        return None
    u = url.strip()
    try:
        host = ""
        path = ""
        # SCP-like: git@host:owner/repo.git
        if "@" in u and ":" in u and not u.startswith(("http://", "https://", "ssh://", "git://")):
            at_pos = u.find("@")
            colon_pos = u.find(":", at_pos + 1)
            if colon_pos != -1:
                host = u[at_pos + 1 : colon_pos]
                path = u[colon_pos + 1 :]
        else:
            from urllib.parse import urlparse as _urlparse

            pr = _urlparse(u)
            host = pr.hostname or ""
            # Some ssh URLs include port; urlparse drops it from hostname already.
            path = pr.path or ""
        host = host.lower()
        if not host:
            return None
        path = path.lstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        # collapse duplicate slashes
        while "//" in path:
            path = path.replace("//", "/")
        parts = [seg for seg in path.split("/") if seg]
        if len(parts) < 2:
            return None
        # Keep the last two segments (owner/repo); supports nested group paths.
        owner, repo_name = parts[-2].lower(), parts[-1].lower()
        return f"{host}/{owner}/{repo_name}"
    except Exception:
        return None


def _compute_project_slug(human_key: str, mode_override: Optional[str] = None) -> str:
    """
    Compute the project slug with strict backward compatibility by default.
    When worktree-friendly behavior is enabled, we still default to 'dir' mode
    until additional identity modes are implemented.
    """
    settings = get_settings()
    # Gate: preserve existing behavior unless explicitly enabled
    if not settings.worktrees_enabled:
        return slugify(human_key)
    # Helpers for identity modes (privacy-safe)
    def _short_sha1(text: str, n: int = 10) -> str:
        return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:n]

    # Delegate to the single shared normalizer so slug/uid can never diverge.
    _norm_remote = _normalize_git_remote

    # A per-call override (e.g. ensure_project(identity_mode=...)) wins over the
    # process-wide settings default so callers can opt a single project into a
    # different identity scheme.
    mode = ((mode_override or settings.project_identity_mode) or "dir").strip().lower()
    # Mode: git-remote
    if mode == "git-remote":
        try:
            # Attempt to use GitPython for robustness across worktrees
            with _git_repo(human_key) as repo:
                remote_name = settings.project_identity_remote or "origin"
                remote_url: str | None = None
                # Prefer 'git remote get-url' to support multiple urls/rewrite rules
                try:
                    remote_url = repo.git.remote("get-url", remote_name).strip() or None
                except Exception:
                    # Fallback: use config if available
                    try:
                        remote = next((r for r in repo.remotes if r.name == remote_name), None)
                        if remote and remote.urls:
                            remote_url = next(iter(remote.urls), None)
                    except Exception:
                        remote_url = None
                normalized = _norm_remote(remote_url)
                if normalized:
                    base = normalized.rsplit("/", 1)[-1] or "repo"
                    canonical = normalized  # privacy-safe canonical string
                    return f"{base}-{_short_sha1(canonical)}"
        except (InvalidGitRepositoryError, NoSuchPathError, Exception):
            # Non-git directory or error; fall through to fallback
            pass
        # Fallback to dir behavior if we cannot resolve a normalized remote
        return slugify(human_key)

    # Mode: git-toplevel
    if mode == "git-toplevel":
        try:
            with _git_repo(human_key) as repo:
                top = repo.git.rev_parse("--show-toplevel").strip()
                if top:
                    from pathlib import Path as _P

                    top_real = str(_P(top).resolve())
                    base = _P(top_real).name or "repo"
                    return f"{base}-{_short_sha1(top_real)}"
        except (InvalidGitRepositoryError, NoSuchPathError, Exception):
            return slugify(human_key)
        return slugify(human_key)

    # Mode: git-common-dir
    if mode == "git-common-dir":
        try:
            with _git_repo(human_key) as repo:
                # Prefer GitPython's common_dir which normalizes worktree paths
                try:
                    gdir = getattr(repo, "common_dir", None)
                except Exception:
                    gdir = None
                if not gdir:
                    gdir = repo.git.rev_parse("--git-common-dir").strip()
                if gdir:
                    from pathlib import Path as _P

                    # rev-parse may return a relative path (e.g. ".git"); anchor it
                    # to the repo working tree so the slug does not depend on CWD.
                    gdir_path = _P(gdir)
                    if not gdir_path.is_absolute():
                        gdir_path = _P(repo.working_tree_dir or human_key) / gdir_path
                    gdir_real = str(gdir_path.resolve())
                    base = "repo"
                    return f"{base}-{_short_sha1(gdir_real)}"
        except (InvalidGitRepositoryError, NoSuchPathError, Exception):
            return slugify(human_key)
        return slugify(human_key)

    # Default and 'dir' mode: strict back-compat
    return slugify(human_key)


def _project_lookup_base_dir() -> Path:
    """Return the best available logical cwd for project-key normalization."""
    cwd_path = Path.cwd()
    raw_pwd = os.environ.get("PWD", "").strip()
    if raw_pwd:
        try:
            pwd_path = Path(raw_pwd).expanduser()
        except Exception:
            pwd_path = None
        else:
            if pwd_path.is_absolute():
                with suppress(OSError):
                    if pwd_path.exists() and pwd_path.samefile(cwd_path):
                        return pwd_path
    return cwd_path


def _canonicalize_project_identifier(identifier: str) -> str:
    """Normalize path-like project identifiers without collapsing symlink identities."""
    absolute_key = _absolute_project_key_path(identifier)
    if absolute_key is not None:
        return str(absolute_key)
    try:
        candidate = Path(identifier).expanduser()
    except Exception:
        return identifier
    looks_like_path = candidate.is_absolute() or identifier.startswith(("~", ".", "..")) or any(
        sep in identifier for sep in ("/", "\\")
    )
    if not looks_like_path:
        return identifier
    absolute_candidate = candidate if candidate.is_absolute() else _project_lookup_base_dir() / candidate
    return os.path.normpath(str(absolute_candidate))


def _delete_tree_with_counts(root: Path) -> tuple[int, int]:
    """Delete a directory tree and return the number of nested files/directories removed."""
    if not root.exists():
        return 0, 0
    files_removed = 0
    dirs_removed = 0
    for item in root.rglob("*"):
        if item.is_file():
            files_removed += 1
        elif item.is_dir():
            dirs_removed += 1
    shutil.rmtree(root)
    return files_removed, dirs_removed


def _delete_project_archive_tree(storage_root: str, project_slug: str) -> tuple[int, int]:
    """Delete a project's archive subtree without blocking the event loop."""
    archive_root = Path(storage_root).expanduser().resolve()
    project_dir = archive_root / "projects" / project_slug
    return _delete_tree_with_counts(project_dir)


_VALID_IDENTITY_MODES = ("dir", "git-remote", "git-common-dir", "git-toplevel")


def _resolve_project_identity(
    human_key: str, identity_mode: Optional[str] = None
) -> dict[str, Any]:
    """
    Resolve identity details for a given human_key path.
    Returns: { slug, identity_mode_used, canonical_path, human_key,
               repo_root, git_common_dir, branch, worktree_name,
               core_ignorecase, normalized_remote, project_uid }
    Writes a private marker under .git/agent-mail/project-id when WORKTREES_ENABLED=1
    and no marker exists yet.

    `identity_mode`, when provided, overrides the process-wide
    ``project_identity_mode`` setting for this single resolution (one of
    ``dir``, ``git-remote``, ``git-common-dir``, ``git-toplevel``).
    """
    settings_local = get_settings()
    mode_override = (identity_mode or "").strip().lower() or None
    if mode_override is not None and mode_override not in _VALID_IDENTITY_MODES:
        raise ValueError(
            f"identity_mode must be one of {_VALID_IDENTITY_MODES}, got: '{identity_mode}'."
        )
    mode_config = (mode_override or settings_local.project_identity_mode or "dir").strip().lower()
    mode_used = "dir" if not settings_local.worktrees_enabled else mode_config
    target_path = _canonicalize_project_identifier(human_key)

    if not settings_local.worktrees_enabled:
        # Keep default behavior lightweight when worktree features are disabled.
        # (Avoid touching GitPython / spawning git subprocesses unnecessarily.)
        slug_value = _compute_project_slug(target_path)
        try:
            project_uid = hashlib.sha1(
                target_path.encode("utf-8"), usedforsecurity=False
            ).hexdigest()[:20]
        except Exception:
            project_uid = str(uuid.uuid4())
        return {
            "slug": slug_value,
            "identity_mode_used": "dir",
            "canonical_path": target_path,
            "human_key": target_path,
            "repo_root": None,
            "git_common_dir": None,
            "branch": None,
            "worktree_name": None,
            "core_ignorecase": None,
            "normalized_remote": None,
            "project_uid": project_uid,
            "discovery": None,
        }

    repo_root: Optional[str] = None
    git_common_dir: Optional[str] = None
    branch: Optional[str] = None
    default_branch: Optional[str] = None
    worktree_name: Optional[str] = None
    core_ignorecase: Optional[bool] = None
    normalized_remote: Optional[str] = None
    canonical_path: str = target_path

    # Delegate to the single shared normalizer so slug/uid can never diverge.
    _norm_remote = _normalize_git_remote

    # Discovery YAML: optional override
    def _read_discovery_yaml(base_dir: str) -> dict[str, Any]:
        try:
            ypath = Path(base_dir) / ".agent-mail.yaml"
            if not ypath.exists():
                return {}
            # Prefer PyYAML when available for robust parsing; fallback to minimal parser
            try:
                import yaml as _yaml
                loaded = _yaml.safe_load(ypath.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    # Keep only known keys to avoid surprises
                    allowed = {"project_uid", "product_uid"}
                    return {k: str(v) for k, v in loaded.items() if k in allowed and isinstance(v, (str, int))}
                return {}
            except Exception:
                data = {}
                for line in ypath.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or ":" not in s:
                        continue
                    key, value = s.split(":", 1)
                    k = key.strip()
                    if k not in {"project_uid", "product_uid"}:
                        continue
                    # strip inline comments
                    v = value.split("#", 1)[0].strip().strip("'\"")
                    if v:
                        data[k] = v
                return data
        except Exception:
            return {}

    try:
        with _git_repo(target_path) as repo:
            repo_root = str(Path(repo.working_tree_dir or "").resolve())
            try:
                git_common_dir = repo.git.rev_parse("--git-common-dir").strip()
            except Exception:
                git_common_dir = None
            try:
                branch = repo.active_branch.name
            except Exception:
                try:
                    branch = repo.git.rev_parse("--abbrev-ref", "HEAD").strip()
                except Exception:
                    branch = None
            try:
                worktree_name = Path(repo.working_tree_dir or "").name or None
            except Exception:
                worktree_name = None
            try:
                core_ic = repo.config_reader().get_value("core", "ignorecase", "false")
                core_ignorecase = str(core_ic).strip().lower() == "true"
            except Exception:
                core_ignorecase = None
            remote_name = settings_local.project_identity_remote or "origin"
            remote_url_local: Optional[str] = None
            try:
                remote_url_local = repo.git.remote("get-url", remote_name).strip() or None
            except Exception:
                try:
                    r = next((r for r in repo.remotes if r.name == remote_name), None)
                    if r and r.urls:
                        remote_url_local = next(iter(r.urls), None)
                except Exception:
                    remote_url_local = None
            normalized_remote = _norm_remote(remote_url_local)
            try:
                sym = repo.git.symbolic_ref(
                    f"refs/remotes/{settings_local.project_identity_remote or 'origin'}/HEAD"
                ).strip()
                if sym.startswith("refs/remotes/"):
                    default_branch = sym.rsplit("/", 1)[-1]
            except Exception:
                default_branch = "main"
    except (InvalidGitRepositoryError, NoSuchPathError, Exception):
        pass  # Non-git directory; continue with fallback values

    # Resolve git_common_dir to an absolute path. `git rev-parse --git-common-dir`
    # often returns a RELATIVE path (e.g. ".git") which, if resolved against the
    # process CWD, makes the project identity depend on the caller's CWD. Anchor
    # it to repo_root (mirrors the marker_private normalization below) so the UID
    # is stable regardless of CWD.
    git_common_dir_abs: Optional[str] = None
    if git_common_dir:
        try:
            gcd_path = Path(git_common_dir)
            if not gcd_path.is_absolute():
                gcd_path = Path(repo_root or target_path) / gcd_path
            git_common_dir_abs = str(gcd_path.resolve())
        except Exception:
            git_common_dir_abs = None

    if mode_used == "git-remote" and normalized_remote:
        canonical_path = normalized_remote
    elif mode_used == "git-toplevel" and repo_root:
        canonical_path = repo_root
    elif mode_used == "git-common-dir" and git_common_dir_abs:
        canonical_path = git_common_dir_abs
    else:
        canonical_path = target_path

    # Compute project_uid via precedence:
    # worktree marker -> discovery yaml -> private marker -> remote fingerprint -> git-common-dir hash -> dir hash
    marker_committed: Optional[Path] = Path(repo_root or "") / ".agent-mail-project-id" if repo_root else None
    marker_private: Optional[Path] = Path(git_common_dir or "") / "agent-mail" / "project-id" if git_common_dir else None
    # Normalize marker_private to absolute if git_common_dir is relative (common for non-linked worktrees)
    if marker_private is not None and not marker_private.is_absolute():
        try:
            base = Path(repo_root or target_path)
            marker_private = (base / marker_private).resolve()
        except Exception:
            pass
    discovery: dict[str, Any] = _read_discovery_yaml(repo_root or target_path)
    project_uid: Optional[str] = None
    try:
        if marker_committed and marker_committed.exists():
            project_uid = (marker_committed.read_text(encoding="utf-8").strip() or None)
    except Exception:
        project_uid = None
    if not project_uid:
        # Discovery yaml override
        uid = str(discovery.get("project_uid", "")).strip() if discovery else ""
        if uid:
            project_uid = uid
    if not project_uid:
        try:
            if marker_private and marker_private.exists():
                project_uid = (marker_private.read_text(encoding="utf-8").strip() or None)
        except Exception:
            project_uid = None
    if not project_uid:
        # Remote fingerprint
        remote_uid: Optional[str] = None
        try:
            if normalized_remote:
                fingerprint = f"{normalized_remote}@{default_branch or 'main'}"
                remote_uid = hashlib.sha1(
                    fingerprint.encode("utf-8"), usedforsecurity=False
                ).hexdigest()[:20]
        except Exception:
            remote_uid = None
        if remote_uid:
            project_uid = remote_uid
    if not project_uid and git_common_dir_abs:
        try:
            project_uid = hashlib.sha1(
                git_common_dir_abs.encode("utf-8"), usedforsecurity=False
            ).hexdigest()[:20]
        except Exception:
            project_uid = None
    if not project_uid:
        try:
            project_uid = hashlib.sha1(
                target_path.encode("utf-8"), usedforsecurity=False
            ).hexdigest()[:20]
        except Exception:
            project_uid = str(uuid.uuid4())

    # Write private marker if gated and we have a git common dir
    if settings_local.worktrees_enabled and marker_private and not marker_private.exists():
        try:
            marker_private.parent.mkdir(parents=True, exist_ok=True)
            marker_private.write_text(project_uid + "\n", encoding="utf-8")
        except Exception:
            pass

    slug_value = _compute_project_slug(target_path, mode_override=mode_override)
    payload = {
        "slug": slug_value,
        "identity_mode_used": mode_used,
        "canonical_path": canonical_path,
        "human_key": target_path,
        "repo_root": repo_root,
        "git_common_dir": git_common_dir,
        "branch": branch,
        "worktree_name": worktree_name,
        "core_ignorecase": core_ignorecase,
        "normalized_remote": normalized_remote,
        "project_uid": project_uid,
        "discovery": discovery or None,
    }
    # Rich-styled identity decision logging (optional)
    try:
        if get_settings().tools_log_enabled:
            from rich.console import Console as _Console  # local import to avoid global dependency
            from rich.table import Table as _Table
            console = _Console()
            table = _Table(title="Identity Resolution", show_header=True, header_style="bold white on blue")
            table.add_column("Field", style="bold cyan")
            table.add_column("Value")
            table.add_row("Mode", str(payload["identity_mode_used"] or "dir"))
            table.add_row("Slug", str(payload["slug"]))
            table.add_row("Canonical", str(payload["canonical_path"]))
            table.add_row("Repo Root", str(payload["repo_root"] or ""))
            table.add_row("Git Common Dir", str(payload["git_common_dir"] or ""))
            table.add_row("Branch", str(payload["branch"] or ""))
            table.add_row("Worktree", str(payload["worktree_name"] or ""))
            table.add_row("Ignorecase", str(payload["core_ignorecase"]))
            table.add_row("Normalized Remote", str(payload["normalized_remote"] or ""))
            table.add_row("Project UID", str(payload["project_uid"] or ""))
            console.print(table)
    except Exception:
        # Never fail due to logging
        pass
    return payload


def _normalize_project_human_key(human_key: str) -> str:
    absolute_key = _absolute_project_key_path(human_key)
    if absolute_key is not None:
        return str(absolute_key)
    return os.path.normpath(human_key)


def _validated_project_uid(value: Any) -> str:
    """Return a DB-safe durable project UID or reject the identity source."""
    if not isinstance(value, str):
        raise ValueError("Resolved project_uid must be a string.")
    project_uid = value.strip()
    if not project_uid:
        raise ValueError("Resolved project_uid cannot be empty.")
    if len(project_uid) > 255:
        raise ValueError("Resolved project_uid cannot exceed 255 characters.")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in project_uid):
        raise ValueError("Resolved project_uid cannot contain whitespace or control characters.")
    return project_uid


def _project_identity_conflict(
    identity: dict[str, Any],
    projects: Sequence[Project],
    *,
    reason: str,
) -> ToolExecutionError:
    """Build the fail-closed error for an ambiguous durable identity."""
    distinct: dict[int | str, Project] = {}
    for project in projects:
        key: int | str = project.id if project.id is not None else f"new:{id(project)}"
        distinct[key] = project
    return ToolExecutionError(
        "PROJECT_IDENTITY_CONFLICT",
        "Project identity is ambiguous; refusing to merge existing project history automatically.",
        recoverable=False,
        data={
            "reason": reason,
            "resolved_project_uid": identity["project_uid"],
            "resolved_slug": identity["slug"],
            "resolved_human_key": identity["human_key"],
            "conflicting_projects": [
                {
                    "id": project.id,
                    "project_uid": project.project_uid,
                    "slug": project.slug,
                    "human_key": project.human_key,
                }
                for project in distinct.values()
            ],
        },
    )


async def _ensure_project(
    human_key: str,
    identity_mode: Optional[str] = None,
) -> Project:
    await ensure_schema()
    # Identity must be resolved before touching the DB.  In particular, a
    # per-call identity_mode override changes which durable Project row is
    # selected; it is not merely response metadata.
    normalized_human_key = _normalize_project_human_key(human_key)
    identity = await asyncio.to_thread(
        _resolve_project_identity,
        normalized_human_key,
        identity_mode,
    )
    project_uid = _validated_project_uid(identity.get("project_uid"))
    identity["project_uid"] = project_uid
    resolved_human_key = str(identity["human_key"])
    resolved_slug = str(identity["slug"])

    for attempt in range(6):
        try:
            async with get_session() as session:
                uid_projects = (
                    await session.execute(
                        select(Project).where(
                            cast(Any, Project.project_uid) == project_uid
                        )
                    )
                ).scalars().all()
                human_projects = (
                    await session.execute(
                        select(Project).where(
                            cast(Any, Project.human_key) == resolved_human_key
                        )
                    )
                ).scalars().all()
                slug_project = (
                    await session.execute(
                        select(Project).where(
                            cast(Any, Project.slug) == resolved_slug
                        )
                    )
                ).scalars().first()

                if len(uid_projects) > 1 or len(human_projects) > 1:
                    raise _project_identity_conflict(
                        identity,
                        [*uid_projects, *human_projects],
                        reason="multiple database rows match one durable identity",
                    )

                uid_project = uid_projects[0] if uid_projects else None
                human_project = human_projects[0] if human_projects else None

                if uid_project is not None:
                    aliases = [
                        candidate
                        for candidate in (human_project, slug_project)
                        if candidate is not None and candidate.id != uid_project.id
                    ]
                    if aliases:
                        raise _project_identity_conflict(
                            identity,
                            [uid_project, *aliases],
                            reason="durable UID and path/slug resolve to different rows",
                        )
                    return uid_project

                if human_project is not None:
                    if slug_project is not None and slug_project.id != human_project.id:
                        raise _project_identity_conflict(
                            identity,
                            [human_project, slug_project],
                            reason="path and slug resolve to different legacy rows",
                        )
                    if human_project.project_uid is not None:
                        # The UID, path and slug reads above are separate SQL
                        # statements.  A concurrent creator can commit after
                        # the UID read but before the path read, so this branch
                        # may observe the newly-created canonical row even
                        # though ``uid_projects`` was empty.  Exact UID equality
                        # is the idempotent success case; only a different UID
                        # is an identity conflict.
                        if human_project.project_uid == project_uid:
                            return human_project
                        raise _project_identity_conflict(
                            identity,
                            [human_project],
                            reason="path is already bound to a different durable UID",
                        )
                    # Safe lazy migration: only the one exact normalized legacy
                    # path is claimed.  No historical rows are bulk-merged.
                    human_project.project_uid = project_uid
                    session.add(human_project)
                    try:
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                        if attempt >= 5:
                            raise
                        await asyncio.sleep(0)
                        continue
                    await session.refresh(human_project)
                    return human_project

                if slug_project is not None:
                    if slug_project.project_uid is not None:
                        # As above, an exact UID can become visible between the
                        # earlier UID lookup and this slug lookup.  Treat the
                        # same durable identity as the concurrent winner.
                        if slug_project.project_uid == project_uid:
                            return slug_project
                        raise _project_identity_conflict(
                            identity,
                            [slug_project],
                            reason="slug is already bound to a different durable UID",
                        )
                    # A legacy worktree row can be claimed by slug only when its
                    # own persisted path independently resolves to this exact
                    # UID.  If that path is no longer inspectable we fail closed.
                    legacy_identity = await asyncio.to_thread(
                        _resolve_project_identity,
                        slug_project.human_key,
                        identity_mode,
                    )
                    legacy_uid = _validated_project_uid(legacy_identity.get("project_uid"))
                    if legacy_uid != project_uid:
                        raise _project_identity_conflict(
                            identity,
                            [slug_project],
                            reason="legacy slug cannot be proven to represent this durable UID",
                        )
                    slug_project.project_uid = project_uid
                    session.add(slug_project)
                    try:
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                        if attempt >= 5:
                            raise
                        await asyncio.sleep(0)
                        continue
                    await session.refresh(slug_project)
                    return slug_project

                project = Project(
                    slug=resolved_slug,
                    human_key=resolved_human_key,
                    project_uid=project_uid,
                )
                session.add(project)
                try:
                    await session.commit()
                except IntegrityError:
                    # Concurrent callers are re-evaluated against all three
                    # identity dimensions on the next iteration.
                    await session.rollback()
                    if attempt >= 5:
                        raise
                    await asyncio.sleep(0)
                    continue
                await session.refresh(project)
                return project
        except OperationalError as exc:
            error_msg = str(exc).lower()
            is_lock_error = any(phrase in error_msg for phrase in ("database is locked", "database is busy", "locked"))
            if not is_lock_error or attempt >= 5:
                raise
            await asyncio.sleep(min(0.05 * (2**attempt), 0.5))

    raise RuntimeError("ensure_project retry loop exited unexpectedly")

    # -- Identity inspection resource is registered inside build_mcp_server below


# --- Smart lookup helpers with fuzzy matching and suggestions -----------------------------------


def _similarity_score(a: str, b: str) -> float:
    """Compute similarity score between two strings (0.0 to 1.0)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


async def _find_similar_projects(identifier: str, limit: int = 5, min_score: float = 0.4) -> list[tuple[str, str, float]]:
    """Find projects with similar slugs/names. Returns list of (slug, human_key, score)."""
    slug = slugify(identifier)
    suggestions: list[tuple[str, str, float]] = []
    async with get_session() as session:
        result = await session.execute(select(Project))
        projects = result.scalars().all()
        for p in projects:
            # Check both slug and human_key similarity
            slug_score = _similarity_score(slug, p.slug)
            key_score = _similarity_score(identifier, p.human_key) if p.human_key else 0.0
            best_score = max(slug_score, key_score)
            if best_score >= min_score:
                suggestions.append((p.slug, p.human_key, best_score))
    suggestions.sort(key=lambda x: x[2], reverse=True)
    return suggestions[:limit]


async def _find_similar_agents(project: Project, name: str, limit: int = 5, min_score: float = 0.4) -> list[tuple[str, float]]:
    """Find agents with similar names in the project. Returns list of (name, score)."""
    suggestions: list[tuple[str, float]] = []
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(
                cast(Any, Agent.project_id == project.id),
                cast(Any, Agent.provisioning_state == "active"),
            )
        )
        agents = result.scalars().all()
        for a in agents:
            score = _similarity_score(name, a.name)
            if score >= min_score:
                suggestions.append((a.name, score))
    suggestions.sort(key=lambda x: x[1], reverse=True)
    return suggestions[:limit]


async def _list_project_agents(project: Project, limit: int = 10) -> list[str]:
    """List agent names in a project."""
    async with get_session() as session:
        result = await session.execute(
            select(Agent.name)
            .where(
                cast(Any, Agent.project_id == project.id),
                cast(Any, Agent.provisioning_state == "active"),
            )
            .limit(limit)
        )
        return [row[0] for row in result.all()]


async def _get_project_by_identifier(identifier: str) -> Project:
    """Get project by identifier with helpful error messages and suggestions."""
    await ensure_schema()

    # Validate input
    if not identifier or not identifier.strip():
        raise ToolExecutionError(
            "INVALID_ARGUMENT",
            "Project identifier cannot be empty. Provide a project path like '/data/projects/myproject' or a slug like 'myproject'.",
            recoverable=True,
            data={"parameter": "project_key", "provided": repr(identifier)},
        )

    raw_identifier = identifier.strip()
    canonical_identifier = await asyncio.to_thread(_canonicalize_project_identifier, raw_identifier)

    # Detect common placeholder patterns - these indicate unconfigured hooks/settings
    _placeholder_patterns = [
        "YOUR_PROJECT",
        "YOUR_PROJECT_PATH",
        "YOUR_PROJECT_KEY",
        "PLACEHOLDER",
        "<PROJECT>",
        "{PROJECT}",
        "$PROJECT",
    ]
    identifier_upper = raw_identifier.upper()
    for pattern in _placeholder_patterns:
        if pattern in identifier_upper or identifier_upper == pattern:
            raise ToolExecutionError(
                "CONFIGURATION_ERROR",
                f"Detected placeholder value '{identifier}' instead of a real project path. "
                f"This typically means a hook or integration script hasn't been configured yet. "
                f"Replace placeholder values in your .claude/settings.json or environment variables "
                f"with actual project paths like '/Users/you/projects/myproject'.",
                recoverable=True,
                data={
                    "parameter": "project_key",
                    "provided": identifier,
                    "detected_placeholder": pattern,
                    "fix_hint": "Update AGENT_MAIL_PROJECT or project_key in your configuration",
                },
            )

    identity: dict[str, Any] | None = None
    if _is_absolute_project_key(raw_identifier):
        identity = await asyncio.to_thread(
            _resolve_project_identity,
            canonical_identifier,
        )
        identity["project_uid"] = _validated_project_uid(identity.get("project_uid"))
        slug = str(identity["slug"])
    else:
        slug = slugify(canonical_identifier)

    async with get_session() as session:
        if identity is not None:
            project_uid = str(identity["project_uid"])
            uid_projects = (
                await session.execute(
                    select(Project).where(
                        cast(Any, Project.project_uid) == project_uid
                    )
                )
            ).scalars().all()
            human_projects = (
                await session.execute(
                    select(Project).where(
                        or_(
                            cast(Any, Project.human_key) == canonical_identifier,
                            cast(Any, Project.human_key) == raw_identifier,
                        )
                    )
                )
            ).scalars().all()
            slug_project = (
                await session.execute(
                    select(Project).where(cast(Any, Project.slug) == slug)
                )
            ).scalars().first()

            if len(uid_projects) > 1 or len(human_projects) > 1:
                raise _project_identity_conflict(
                    identity,
                    [*uid_projects, *human_projects],
                    reason="multiple database rows match one durable identity",
                )

            uid_project = uid_projects[0] if uid_projects else None
            human_project = human_projects[0] if human_projects else None
            if uid_project is not None:
                aliases = [
                    candidate
                    for candidate in (human_project, slug_project)
                    if candidate is not None and candidate.id != uid_project.id
                ]
                if aliases:
                    raise _project_identity_conflict(
                        identity,
                        [uid_project, *aliases],
                        reason="durable UID and path/slug resolve to different rows",
                    )
                return uid_project

            if human_project is not None:
                if slug_project is not None and slug_project.id != human_project.id:
                    raise _project_identity_conflict(
                        identity,
                        [human_project, slug_project],
                        reason="path and slug resolve to different legacy rows",
                    )
                if human_project.project_uid is not None:
                    raise _project_identity_conflict(
                        identity,
                        [human_project],
                        reason="path is already bound to a different durable UID",
                    )
                human_project.project_uid = project_uid
                session.add(human_project)
                try:
                    await session.commit()
                except IntegrityError as exc:
                    await session.rollback()
                    raise _project_identity_conflict(
                        identity,
                        [human_project],
                        reason="legacy path was concurrently bound to another project",
                    ) from exc
                await session.refresh(human_project)
                return human_project

            if slug_project is not None:
                if slug_project.project_uid is not None:
                    raise _project_identity_conflict(
                        identity,
                        [slug_project],
                        reason="slug is already bound to a different durable UID",
                    )
                legacy_identity = await asyncio.to_thread(
                    _resolve_project_identity,
                    slug_project.human_key,
                )
                legacy_uid = _validated_project_uid(legacy_identity.get("project_uid"))
                if legacy_uid != project_uid:
                    raise _project_identity_conflict(
                        identity,
                        [slug_project],
                        reason="legacy slug cannot be proven to represent this durable UID",
                    )
                slug_project.project_uid = project_uid
                session.add(slug_project)
                try:
                    await session.commit()
                except IntegrityError as exc:
                    await session.rollback()
                    raise _project_identity_conflict(
                        identity,
                        [slug_project],
                        reason="legacy slug was concurrently bound to another project",
                    ) from exc
                await session.refresh(slug_project)
                return slug_project
        else:
            result = await session.execute(
                select(Project).where(
                    or_(
                        cast(Any, Project.project_uid) == raw_identifier,
                        cast(Any, Project.slug) == slug,
                        cast(Any, Project.human_key) == canonical_identifier,
                        cast(Any, Project.human_key) == raw_identifier,
                    )
                )
            )
            projects = result.scalars().all()
            if len(projects) > 1:
                # A slug/UID identifier must never choose an arbitrary row.
                generic_identity = {
                    "project_uid": raw_identifier,
                    "slug": slug,
                    "human_key": raw_identifier,
                }
                raise _project_identity_conflict(
                    generic_identity,
                    projects,
                    reason="identifier matches multiple project rows",
                )
            if projects:
                return projects[0]

    # Project not found - provide helpful suggestions
    suggestions = await _find_similar_projects(raw_identifier)

    if suggestions:
        suggestion_text = ", ".join([f"'{s[0]}'" for s in suggestions[:3]])
        raise ToolExecutionError(
            "NOT_FOUND",
            f"Project '{raw_identifier}' not found. Did you mean: {suggestion_text}? "
            f"Use ensure_project to create a new project, or check spelling.",
            recoverable=True,
            data={
                "identifier": raw_identifier,
                "slug_searched": slug,
                "suggestions": [{"slug": s[0], "human_key": s[1], "score": round(s[2], 2)} for s in suggestions],
            },
        )
    else:
        raise ToolExecutionError(
            "NOT_FOUND",
            f"Project '{raw_identifier}' not found and no similar projects exist. "
            f"Use ensure_project to create a new project first. "
            f"Example: ensure_project(human_key='/path/to/your/project')",
            recoverable=True,
            data={"identifier": raw_identifier, "slug_searched": slug},
        )


async def _get_project_by_id(project_id: int) -> Project:
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalars().first()
        if not project:
            raise NoResultFound(f"Project id '{project_id}' not found.")
        return project


# --- Common mistake detection helpers --------------------------------------------------------

# Known program names that agents might mistakenly use as agent names
_KNOWN_PROGRAM_NAMES: frozenset[str] = frozenset({
    "agy", "claude-code", "claude", "codex-cli", "codex", "cursor", "grok",
    "windsurf", "cline", "aider", "copilot", "github-copilot", "gemini-cli",
    "gemini", "kimi", "opencode", "vscode", "neovim", "vim", "emacs", "zed",
    "continue",
})

# Known model name patterns that agents might mistakenly use as agent names
_MODEL_NAME_PATTERNS: tuple[str, ...] = (
    "gpt-", "gpt4", "gpt3", "claude-", "opus", "sonnet", "haiku",
    "gemini-", "llama", "mistral", "codestral", "o1-", "o3-",
)


def _looks_like_program_name(value: str) -> bool:
    """Check if value looks like a program name (not a valid agent name)."""
    v = value.lower().strip()
    return v in _KNOWN_PROGRAM_NAMES


def _looks_like_model_name(value: str) -> bool:
    """Check if value looks like a model name (not a valid agent name)."""
    v = value.lower().strip()
    return any(p in v for p in _MODEL_NAME_PATTERNS)


def _looks_like_email(value: str) -> bool:
    """Check if value looks like an email address."""
    return "@" in value and "." in value.split("@")[-1]


def _looks_like_broadcast(value: str) -> bool:
    """Check if value looks like a broadcast attempt."""
    v = value.lower().strip()
    return v in {"all", "*", "everyone", "broadcast", "@all", "@everyone"}


def _looks_like_descriptive_name(value: str) -> bool:
    """Check if value looks like a role label instead of a stable host identity."""
    v = value.lower()
    # Common suffixes for descriptive agent names
    descriptive_patterns = (
        "agent", "bot", "assistant", "helper", "manager", "coordinator",
        "developer", "engineer", "migrator", "refactorer", "fixer",
        "harmonizer", "integrator", "optimizer", "analyzer", "worker",
    )
    return any(v.endswith(p) for p in descriptive_patterns)


def _looks_like_unix_username(value: str) -> bool:
    """
    Check if value looks like a Unix username rather than a durable Agent id.

    This helps detect when hooks or scripts pass $USER instead of the actual agent name.
    Unix usernames are commonly one short lowercase alphanumeric component,
    whereas canonical durable Agent ids have four explicit hyphenated parts.
    """
    v = value.strip()
    if not v:
        return False

    # A lowercase single word cannot be a canonical client-os-host-slot id.
    return v.islower() and v.isalnum() and 2 <= len(v) <= 16


def _detect_agent_name_mistake(value: str) -> tuple[str, str] | None:
    """
    Detect common mistakes when agents provide invalid agent names.
    Returns (mistake_type, helpful_message) or None if no obvious mistake detected.
    """
    if validate_client_platform_host_agent_id(value):
        return None
    if _looks_like_program_name(value):
        return (
            "PROGRAM_NAME_AS_AGENT",
            f"'{value}' looks like a program name, not an agent name. "
            "Use the 'program' parameter for program names and pass a durable "
            "client-os-host-slot name such as 'codex-wsl-home-1'."
        )
    if _looks_like_model_name(value):
        return (
            "MODEL_NAME_AS_AGENT",
            f"'{value}' looks like a model name, not an agent name. "
            "Use the 'model' parameter for model names and pass a durable "
            "client-os-host-slot name such as 'claude-linux-ci-1'."
        )
    if _looks_like_email(value):
        return (
            "EMAIL_AS_AGENT",
            f"'{value}' looks like an email address. Agent names are durable "
            "client-os-host-slot identifiers such as 'codex-wsl-home-1', not "
            "email addresses. Check the 'to' parameter format."
        )
    if _looks_like_broadcast(value):
        return (
            "BROADCAST_ATTEMPT",
            f"'{value}' looks like a broadcast attempt. Agent Mail doesn't support broadcasting to all agents. "
            f"List specific recipient agent names in the 'to' parameter."
        )
    if _looks_like_descriptive_name(value):
        return (
            "DESCRIPTIVE_NAME",
            f"'{value}' looks like a descriptive role name. New Agent names must be "
            "stable client-os-host-slot identities, not task descriptions."
        )
    if _looks_like_unix_username(value):
        return (
            "UNIX_USERNAME_AS_AGENT",
            f"'{value}' looks like a Unix username (possibly from $USER environment variable). "
            "Agent names must be explicit client-os-host-slot identities. "
            "To find an existing durable Agent, check its provisioning record or use "
            f"resource://agents/{{project_key}} to list all registered agents in this project."
        )
    return None


def _recipient_agent_fragment(value: str) -> str:
    """Extract the Agent part of either supported qualified recipient syntax."""
    candidate = value.strip()
    if candidate.startswith("project:") and "#" in candidate:
        _project_prefix, _separator, agent_name = candidate.partition("#")
        return agent_name.strip()
    if "@" in candidate:
        agent_name, _separator, project_identifier = candidate.partition("@")
        if agent_name.strip() and project_identifier.strip():
            return agent_name.strip()
    return candidate


def _detect_suspicious_file_reservation(pattern: str) -> str | None:
    """
    Detect suspicious file reservation patterns that might be too broad.
    Returns a warning message or None if the pattern looks reasonable.
    """
    p = pattern.strip()

    # Virtual namespace patterns are always valid (bd-14z)
    if _is_virtual_namespace(p):
        return None

    # Catch overly broad patterns
    if p in ("*", "**", "**/*", "**/**", "."):
        return (
            f"Pattern '{p}' is too broad and would reserve the entire project. "
            f"Use more specific patterns like 'src/api/*.py' or 'lib/auth/**'."
        )

    # Catch absolute paths when relative expected
    if p.startswith("/") and not p.startswith("//"):
        return (
            f"Pattern '{p}' looks like an absolute path. File reservation patterns should be "
            f"project-relative (e.g., 'src/module.py' not '/full/path/src/module.py')."
        )

    # Warn about very short patterns that might be unintentionally broad
    if len(p) <= 2 and "*" in p:
        return (
            f"Pattern '{p}' is very short and may match more files than intended. "
            f"Consider using a more specific pattern."
        )

    return None


# --- Project sibling suggestion helpers -----------------------------------------------------

_PROJECT_PROFILE_FILENAMES: tuple[str, ...] = (
    "README.md",
    "Readme.md",
    "readme.md",
    "AGENTS.md",
    "CLAUDE.md",
    "Claude.md",
    "agents/README.md",
    "docs/README.md",
    "docs/overview.md",
)
_PROJECT_PROFILE_MAX_TOTAL_CHARS = 6000
_PROJECT_PROFILE_PER_FILE_CHARS = 1800
_PROJECT_SIBLING_REFRESH_TTL = timedelta(hours=12)
_PROJECT_SIBLING_REFRESH_LIMIT = 3
_PROJECT_SIBLING_MIN_SUGGESTION_SCORE = 0.92


def _canonical_project_pair(a_id: int, b_id: int) -> tuple[int, int]:
    if a_id == b_id:
        raise ValueError("Project pair must reference distinct projects.")
    return (a_id, b_id) if a_id < b_id else (b_id, a_id)


@asynccontextmanager
async def _archive_write_lock(archive: ProjectArchive, *, timeout_seconds: float = 60.0) -> AsyncIterator[None]:
    try:
        async with archive_write_lock(archive, timeout_seconds=timeout_seconds):
            yield
    except TimeoutError as exc:
        raise ToolExecutionError(
            "ARCHIVE_LOCK_TIMEOUT",
            (
                f"Archive lock busy for project '{archive.slug}' at '{archive.lock_path}'. "
                f"Timed out after {timeout_seconds:.1f}s. "
                "Inspect running agents or call collect_lock_status to clear stale locks."
            ),
            recoverable=True,
            data={
                "project_slug": archive.slug,
                "lock_path": str(archive.lock_path),
                "timeout_seconds": timeout_seconds,
            },
        ) from exc


async def _revalidate_project_lifetime_in_session(
    session: Any,
    *,
    project: Project,
    action: str,
) -> Project:
    """Return the exact project row or reject a stale pre-delete snapshot."""
    if project.id is None:
        raise ValueError("Project must have an id before lifetime revalidation.")
    current_project = await session.get(Project, project.id)
    if (
        current_project is None
        or current_project.slug != project.slug
        or current_project.project_generation != project.project_generation
    ):
        raise ToolExecutionError(
            "PROJECT_IDENTITY_STALE",
            f"The exact project lifetime no longer exists; refusing {action}.",
            recoverable=True,
            data={"project_key": project.human_key, "action": action},
        )
    return current_project


async def _revalidate_agent_lifetime_in_session(
    session: Any,
    *,
    project: Project,
    agent: Agent,
    action: str,
    execution: AgentExecution | None = None,
    require_active_execution: bool = False,
    touch_execution_ts: datetime | None = None,
) -> tuple[Project, Agent, AgentExecution | None]:
    """Revalidate immutable project, Agent and optional execution lifetimes."""
    current_project = await _revalidate_project_lifetime_in_session(
        session,
        project=project,
        action=action,
    )
    if agent.id is None:
        raise ValueError("Agent must have an id before lifetime revalidation.")
    current_agent = await session.get(Agent, agent.id)
    if (
        current_agent is None
        or current_agent.project_id != project.id
        or current_agent.name != agent.name
        or current_agent.agent_generation != agent.agent_generation
    ):
        raise ToolExecutionError(
            "AGENT_IDENTITY_STALE",
            f"The exact Agent lifetime no longer exists; refusing {action}.",
            recoverable=True,
            data={
                "project_key": project.human_key,
                "agent_name": agent.name,
                "action": action,
            },
        )

    current_execution: AgentExecution | None = None
    if execution is not None:
        current_execution = await session.get(AgentExecution, execution.id)
        if (
            current_execution is None
            or current_execution.project_id != project.id
            or current_execution.agent_id != agent.id
        ):
            raise ToolExecutionError(
                "EXECUTION_OWNERSHIP_MISMATCH",
                f"The exact execution lifetime no longer belongs to the Agent; refusing {action}.",
                recoverable=False,
                data={"execution_id": execution.id, "action": action},
            )
        if require_active_execution and current_execution.status != "active":
            raise ToolExecutionError(
                "EXECUTION_NOT_ACTIVE",
                f"Agent execution '{execution.id}' is '{current_execution.status}', not active.",
                recoverable=True,
                data={
                    "execution_id": execution.id,
                    "status": current_execution.status,
                    "action": action,
                },
            )
        if touch_execution_ts is not None and current_execution.status == "active":
            current_execution.last_active_ts = touch_execution_ts
            session.add(current_execution)
    return current_project, current_agent, current_execution


async def _revalidate_agent_profile_lifetime(
    *,
    project: Project,
    agent: Agent,
) -> Agent:
    """Return the current row only while the exact project/Agent lifetime exists.

    Callers invoke this while holding the project's archive lock immediately
    before publishing ``profile.json``. Revalidating the opaque project and
    Agent generations prevents a queued DB-first writer from publishing an
    artifact for a superseded lifetime during controlled maintenance.
    """
    if project.id is None or agent.id is None:
        raise ValueError("Project and Agent must have ids before publishing a profile.")
    async with get_session() as session:
        _current_project, current_agent, _current_execution = (
            await _revalidate_agent_lifetime_in_session(
                session,
                project=project,
                agent=agent,
                action="archive profile publication",
            )
        )
        return current_agent


async def _rollback_created_agent_lifetime(
    *,
    project: Project,
    agent: Agent,
) -> None:
    """Remove only the exact just-created lifetime after profile publication fails."""
    if project.id is None or agent.id is None:
        return
    async with get_immediate_session() as session:
        current_project = await session.get(Project, project.id)
        current_agent = await session.get(Agent, agent.id)
        if (
            current_project is None
            or current_project.project_generation != project.project_generation
            or current_agent is None
            or current_agent.project_id != project.id
            or current_agent.agent_generation != agent.agent_generation
            or current_agent.provisioning_state != "provisioning"
        ):
            return
        await session.delete(current_agent)
        await session.commit()


async def _activate_provisioned_agent_lifetime(
    *,
    project: Project,
    agent: Agent,
) -> Agent:
    """Publish one exact Agent lifetime after its token and profile exist."""
    if project.id is None or agent.id is None:
        raise ValueError("Project and Agent must have ids before activation.")
    async with get_immediate_session() as session:
        current_project = await session.get(Project, project.id)
        current_agent = await session.get(Agent, agent.id)
        if (
            current_project is None
            or current_project.slug != project.slug
            or current_project.project_generation != project.project_generation
            or current_agent is None
            or current_agent.project_id != project.id
            or current_agent.name != agent.name
            or current_agent.agent_generation != agent.agent_generation
        ):
            raise ToolExecutionError(
                "AGENT_IDENTITY_STALE",
                "The exact provisioning Agent lifetime no longer exists.",
                recoverable=True,
                data={"project_key": project.human_key, "agent_name": agent.name},
            )
        if current_agent.provisioning_state != "provisioning":
            raise ToolExecutionError(
                "AGENT_PROVISIONING_STATE_INVALID",
                "The Agent lifetime is no longer awaiting activation.",
                recoverable=False,
                data={
                    "project_key": project.human_key,
                    "agent_name": agent.name,
                    "provisioning_state": current_agent.provisioning_state,
                },
            )
        if not (current_agent.registration_token or "").strip():
            raise ToolExecutionError(
                "AGENT_PROVISIONING_TOKEN_MISSING",
                "The Agent cannot be activated without its registration credential.",
                recoverable=False,
                data={"project_key": project.human_key, "agent_name": agent.name},
            )
        current_agent.provisioning_state = "active"
        session.add(current_agent)
        await session.commit()
        return current_agent


async def _read_file_preview(path: Path, *, max_chars: int) -> str:
    def _read() -> str:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                data = handle.read(max_chars + 1024)
        except Exception:
            return ""
        return (data or "").strip()[:max_chars]

    return await asyncio.to_thread(_read)


def _collect_project_profile_candidates(base_path: Path) -> list[tuple[str, Path]]:
    if not base_path.exists():
        return []

    candidates: list[tuple[str, Path]] = []
    seen_files: set[tuple[int, int]] = set()
    for rel_name in _PROJECT_PROFILE_FILENAMES:
        candidate = base_path / rel_name
        try:
            stat_result = candidate.stat()
        except Exception:
            continue
        if not stat.S_ISREG(stat_result.st_mode):
            continue
        file_key = (stat_result.st_dev, stat_result.st_ino)
        if file_key in seen_files:
            continue
        seen_files.add(file_key)
        candidates.append((rel_name, candidate))
    return candidates


async def _build_project_profile(
    project: Project,
    agent_names: list[str],
) -> str:
    pieces: list[str] = [
        f"Identifier: {project.human_key}",
        f"Slug: {project.slug}",
        f"Agents: {', '.join(agent_names) if agent_names else 'None registered'}",
    ]

    base_path = Path(project.human_key)
    total_chars = 0
    profile_candidates = await asyncio.to_thread(_collect_project_profile_candidates, base_path)
    for rel_name, candidate in profile_candidates:
        preview = await _read_file_preview(candidate, max_chars=_PROJECT_PROFILE_PER_FILE_CHARS)
        if not preview:
            continue
        pieces.append(f"===== {rel_name} =====\n{preview}")
        total_chars += len(preview)
        if total_chars >= _PROJECT_PROFILE_MAX_TOTAL_CHARS:
            break
    return "\n\n".join(pieces)


def _heuristic_project_similarity(project_a: Project, project_b: Project) -> tuple[float, str]:
    # CRITICAL: Projects with identical human_key are the SAME project, not siblings
    # This should be filtered earlier, but adding safeguard here
    if project_a.human_key == project_b.human_key:
        return 0.0, "ERROR: Identical human_key - these are the SAME project, not siblings"

    slug_ratio = SequenceMatcher(None, project_a.slug, project_b.slug).ratio()
    human_ratio = SequenceMatcher(None, project_a.human_key, project_b.human_key).ratio()
    shared_prefix = 0.0
    try:
        prefix_a = Path(project_a.human_key).name.lower()
        prefix_b = Path(project_b.human_key).name.lower()
        shared_prefix = SequenceMatcher(None, prefix_a, prefix_b).ratio()
    except Exception:
        shared_prefix = 0.0

    score = max(slug_ratio, human_ratio, shared_prefix)
    reasons: list[str] = []
    if slug_ratio > 0.6:
        reasons.append(f"Slugs are similar ({slug_ratio:.2f})")
    if human_ratio > 0.6:
        reasons.append(f"Human keys align ({human_ratio:.2f})")
    parent_a = Path(project_a.human_key).parent
    parent_b = Path(project_b.human_key).parent
    if parent_a == parent_b:
        score = max(score, 0.85)
        reasons.append("Projects share the same parent directory")
    if not reasons:
        reasons.append("Heuristic comparison found limited overlap; treating as weak relation")
    return min(max(score, 0.0), 1.0), ", ".join(reasons)


async def _score_project_pair(
    project_a: Project,
    profile_a: str,
    project_b: Project,
    profile_b: str,
) -> tuple[float, str]:
    settings = get_settings()
    heuristic_score, heuristic_reason = _heuristic_project_similarity(project_a, project_b)

    if not settings.llm.enabled:
        return heuristic_score, heuristic_reason

    system_prompt = (
        "You are an expert analyst who maps whether two software projects are tightly related parts "
        "of the same overall product. Score relationship strength from 0.0 (unrelated) to 1.0 "
        "(same initiative with tightly coupled scope)."
    )
    user_prompt = (
        "Return strict JSON with keys: score (float 0-1), rationale (<=120 words).\n"
        "Focus on whether these projects represent collaborating slices of the same product.\n\n"
        f"Project A Profile:\n{profile_a}\n\nProject B Profile:\n{profile_b}"
    )

    try:
        completion = await complete_system_user(system_prompt, user_prompt, max_tokens=400)
        payload = completion.content.strip()
        data = json.loads(payload)
        score = float(data.get("score", heuristic_score))
        rationale = str(data.get("rationale", "")).strip() or heuristic_reason
        return min(max(score, 0.0), 1.0), rationale
    except Exception as exc:
        logger.debug("project_sibling.llm_failed", exc_info=exc)
        return heuristic_score, heuristic_reason + " (LLM fallback)"


async def refresh_project_sibling_suggestions(*, max_pairs: int = _PROJECT_SIBLING_REFRESH_LIMIT) -> None:
    await ensure_schema()
    async with get_session() as session:
        projects = (await session.execute(select(Project))).scalars().all()
        if len(projects) < 2:
            return

        agents_rows = await session.execute(
            select(Agent.project_id, Agent.name).where(
                cast(Any, Agent.provisioning_state == "active")
            )
        )
        agent_map: dict[int, list[str]] = defaultdict(list)
        for proj_id, name in agents_rows.fetchall():
            agent_map[int(proj_id)].append(name)

        existing_rows = (await session.execute(select(ProjectSiblingSuggestion))).scalars().all()
        existing_map: dict[tuple[int, int], ProjectSiblingSuggestion] = {}
        for suggestion in existing_rows:
            pair = _canonical_project_pair(suggestion.project_a_id, suggestion.project_b_id)
            existing_map[pair] = suggestion

        now = datetime.now(timezone.utc)
        naive_now = _naive_utc(now)
        to_evaluate: list[tuple[Project, Project, ProjectSiblingSuggestion | None]] = []
        for idx, project_a in enumerate(projects):
            if project_a.id is None:
                continue
            for project_b in projects[idx + 1 :]:
                if project_b.id is None:
                    continue

                # CRITICAL: Skip projects with identical human_key - they're the SAME project, not siblings
                # Two agents in /data/projects/smartedgar_mcp are on the SAME project
                # Siblings would be different directories like /data/projects/smartedgar_mcp_frontend
                if project_a.human_key == project_b.human_key:
                    continue

                pair = _canonical_project_pair(project_a.id, project_b.id)
                suggestion = existing_map.get(pair)
                if suggestion is None:
                    to_evaluate.append((project_a, project_b, None))
                else:
                    eval_ts = suggestion.evaluated_ts
                    # Normalize to timezone-aware UTC before arithmetic; SQLite may return naive datetimes
                    if eval_ts is not None:
                        if eval_ts.tzinfo is None or eval_ts.tzinfo.utcoffset(eval_ts) is None:
                            eval_ts = eval_ts.replace(tzinfo=timezone.utc)
                        else:
                            eval_ts = eval_ts.astimezone(timezone.utc)
                        age = now - eval_ts
                    else:
                        age = _PROJECT_SIBLING_REFRESH_TTL
                    if suggestion.status == "dismissed" and age < timedelta(days=7):
                        continue
                    if age >= _PROJECT_SIBLING_REFRESH_TTL and len(to_evaluate) < max_pairs:
                        to_evaluate.append((project_a, project_b, suggestion))
                if len(to_evaluate) >= max_pairs:
                    break

        if not to_evaluate:
            return

        updated = False
        for project_a, project_b, suggestion in to_evaluate[:max_pairs]:
            profile_a = await _build_project_profile(project_a, agent_map.get(project_a.id or -1, []))
            profile_b = await _build_project_profile(project_b, agent_map.get(project_b.id or -1, []))
            score, rationale = await _score_project_pair(project_a, profile_a, project_b, profile_b)

            pair = _canonical_project_pair(project_a.id or 0, project_b.id or 0)
            record = existing_map.get(pair) if suggestion is None else suggestion
            if record is None:
                record = ProjectSiblingSuggestion(
                    project_a_id=pair[0],
                    project_b_id=pair[1],
                    score=score,
                    rationale=rationale,
                    status="suggested",
                )
                session.add(record)
                existing_map[pair] = record
            else:
                record.score = score
                record.rationale = rationale
                # Preserve user decisions
                if record.status not in {"confirmed", "dismissed"}:
                    record.status = "suggested"
            record.evaluated_ts = naive_now
            updated = True

        if updated:
            await session.commit()


async def get_project_sibling_data() -> dict[int, dict[str, list[dict[str, Any]]]]:
    await ensure_schema()
    async with get_session() as session:
        rows = await session.execute(
            text(
                """
                SELECT s.id, s.project_a_id, s.project_b_id, s.score, s.status, s.rationale,
                       s.evaluated_ts, pa.slug AS slug_a, pa.human_key AS human_a,
                       pb.slug AS slug_b, pb.human_key AS human_b
                FROM project_sibling_suggestions s
                JOIN projects pa ON pa.id = s.project_a_id
                JOIN projects pb ON pb.id = s.project_b_id
                ORDER BY s.score DESC
                """
            )
        )
        result_map: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"confirmed": [], "suggested": []})

        for row in rows.fetchall():
            suggestion_id = int(row[0])
            a_id = int(row[1])
            b_id = int(row[2])
            entry_base = {
                "suggestion_id": suggestion_id,
                "score": float(row[3] or 0.0),
                "status": row[4],
                "rationale": row[5] or "",
                "evaluated_ts": str(row[6]) if row[6] else None,
            }
            a_info = {"id": a_id, "slug": row[7], "human_key": row[8]}
            b_info = {"id": b_id, "slug": row[9], "human_key": row[10]}

            for current, other in ((a_info, b_info), (b_info, a_info)):
                bucket = result_map[current["id"]]
                entry = {**entry_base, "peer": other}
                if entry["status"] == "confirmed":
                    bucket["confirmed"].append(entry)
                elif entry["status"] != "dismissed" and float(cast(float, entry_base["score"])) >= _PROJECT_SIBLING_MIN_SUGGESTION_SCORE:
                    bucket["suggested"].append(entry)

        return result_map


async def update_project_sibling_status(project_id: int, other_id: int, status: str) -> dict[str, Any]:
    normalized_status = status.lower()
    if normalized_status not in {"confirmed", "dismissed", "suggested"}:
        raise ValueError("Invalid status")

    await ensure_schema()
    async with get_session() as session:
        pair = _canonical_project_pair(project_id, other_id)
        suggestion = (
            await session.execute(
                select(ProjectSiblingSuggestion).where(
                    ProjectSiblingSuggestion.project_a_id == pair[0],
                    ProjectSiblingSuggestion.project_b_id == pair[1],
                )
            )
        ).scalars().first()

        if suggestion is None:
            # Create a baseline suggestion via refresh for this specific pair
            project_a_obj = await session.get(Project, pair[0])
            project_b_obj = await session.get(Project, pair[1])
            projects = [proj for proj in (project_a_obj, project_b_obj) if proj is not None]
            if len(projects) != 2:
                raise NoResultFound("Project pair not found")
            project_map = {proj.id: proj for proj in projects if proj.id is not None}
            agents_rows = await session.execute(
                select(Agent.project_id, Agent.name).where(
                    or_(Agent.project_id == pair[0], cast(Any, Agent.project_id) == pair[1]),
                    cast(Any, Agent.provisioning_state == "active"),
                )
            )
            agent_map: dict[int, list[str]] = defaultdict(list)
            for proj_id, name in agents_rows.fetchall():
                agent_map[int(proj_id)].append(name)
            profile_a = await _build_project_profile(project_map[pair[0]], agent_map.get(pair[0], []))
            profile_b = await _build_project_profile(project_map[pair[1]], agent_map.get(pair[1], []))
            score, rationale = await _score_project_pair(project_map[pair[0]], profile_a, project_map[pair[1]], profile_b)
            suggestion = ProjectSiblingSuggestion(
                project_a_id=pair[0],
                project_b_id=pair[1],
                score=score,
                rationale=rationale,
                status="suggested",
            )
            session.add(suggestion)
            await session.flush()

        now = datetime.now(timezone.utc)
        naive_now = _naive_utc(now)
        suggestion.status = normalized_status
        suggestion.evaluated_ts = naive_now
        if normalized_status == "confirmed":
            suggestion.confirmed_ts = naive_now
            suggestion.dismissed_ts = None
        elif normalized_status == "dismissed":
            suggestion.dismissed_ts = naive_now
            suggestion.confirmed_ts = None

        await session.commit()

        project_a_obj = await session.get(Project, suggestion.project_a_id)
        project_b_obj = await session.get(Project, suggestion.project_b_id)
        project_lookup = {
            proj.id: proj
            for proj in (project_a_obj, project_b_obj)
            if proj is not None and proj.id is not None
        }

        def _project_payload(proj_id: int) -> dict[str, Any]:
            proj = project_lookup.get(proj_id)
            if proj is None:
                return {"id": proj_id, "slug": "", "human_key": ""}
            return {"id": proj.id, "slug": proj.slug, "human_key": proj.human_key}

        return {
            "id": suggestion.id,
            "status": suggestion.status,
            "score": suggestion.score,
            "rationale": suggestion.rationale,
            "project_a": _project_payload(suggestion.project_a_id),
            "project_b": _project_payload(suggestion.project_b_id),
            "evaluated_ts": str(suggestion.evaluated_ts) if suggestion.evaluated_ts else None,
        }


async def _agent_name_exists(project: Project, name: str) -> bool:
    if project.id is None:
        raise ValueError("Project must have an id before querying agents.")
    async with get_session() as session:
        result = await session.execute(
            select(Agent.id).where(Agent.project_id == project.id, func.lower(Agent.name) == name.lower())
        )
        return result.first() is not None


async def _get_window_identity(
    project: Project,
    window_uuid: str,
) -> Optional[WindowIdentity]:
    """Look up an existing, non-expired window identity."""
    if project.id is None:
        return None
    await ensure_schema()
    now = _naive_utc()
    async with get_session() as session:
        result = await session.execute(
            select(WindowIdentity).where(
                cast(Any, WindowIdentity.project_id == project.id),
                cast(Any, func.lower(WindowIdentity.window_uuid) == window_uuid.lower()),
                or_(cast(Any, WindowIdentity.expires_ts).is_(None), cast(Any, WindowIdentity.expires_ts) > now),
            )
        )
        return result.scalars().first()


async def _create_window_identity(
    project: Project,
    window_uuid: str,
    display_name: str,
    ttl_days: int = 30,
) -> WindowIdentity:
    """Create a new window identity record.

    Handles concurrent creation gracefully: if another caller inserts the same
    (project_id, window_uuid) first, we catch the IntegrityError and return
    the existing record instead of crashing.
    """
    if project.id is None:
        raise ValueError("Project must have an id before creating window identities.")
    await ensure_schema()
    now = _naive_utc()
    expires = now + timedelta(days=ttl_days)
    async with get_session() as session:
        identity = WindowIdentity(
            project_id=project.id,
            window_uuid=window_uuid,
            display_name=display_name,
            created_ts=now,
            last_active_ts=now,
            expires_ts=expires,
        )
        session.add(identity)
        try:
            await session.commit()
            await session.refresh(identity)
            return identity
        except IntegrityError:
            await session.rollback()
            # Concurrent insert won the race — fetch the existing record
            existing = await _get_window_identity(project, window_uuid)
            if existing is not None:
                return existing
            raise  # Should not happen, but don't swallow unexpected errors


async def _touch_window_identity(
    identity: WindowIdentity,
    ttl_days: int = 30,
) -> None:
    """Update last_active_ts and extend expiry for a window identity."""
    now = _naive_utc()
    async with get_session() as session:
        db_identity = await session.get(WindowIdentity, identity.id)
        if db_identity:
            db_identity.last_active_ts = now
            base = max(now, db_identity.expires_ts) if db_identity.expires_ts is not None else now
            db_identity.expires_ts = base + timedelta(days=ttl_days)
            session.add(db_identity)
            await session.commit()
            identity.last_active_ts = db_identity.last_active_ts
            identity.expires_ts = db_identity.expires_ts


def _validate_window_uuid(value: str) -> bool:
    """Validate that a string looks like a UUID."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


async def _generate_unique_agent_name(
    project: Project,
    settings: Settings,
    name_hint: Optional[str] = None,
) -> str:
    archive = await ensure_archive(settings, project.slug)

    async def available(candidate: str) -> bool:
        if await _agent_name_exists(project, candidate):
            return False
        # The database and permanent rename tombstones own identity.  A profile
        # is a projection and can outlive a failed pre-activation attempt; it
        # must not burn the durable name after the provisioning row rolls back.
        return await get_identity_rename_tombstone(archive, candidate) is None

    if not name_hint:
        raise ToolExecutionError(
            "NAME_REQUIRED",
            "A durable Agent requires an explicit client-os-host-slot identity.",
            data={"field": "name"},
        )
    candidate = name_hint.strip()
    if not validate_client_platform_host_agent_id(candidate):
        raise ToolExecutionError(
            "INVALID_DURABLE_AGENT_NAME",
            (
                f"New durable Agent '{candidate}' must match client-os-host-slot, "
                "for example 'codex-wsl-home-1'."
            ),
            data={"provided_name": candidate},
        )
    if not await available(candidate):
        raise ToolExecutionError(
            "AGENT_ALREADY_EXISTS",
            f"Agent identity '{candidate}' is already in use.",
            data={"provided_name": candidate},
        )
    return candidate


_GENERATED_ALIAS_ATTEMPTS = 12

# Ordered, closed vocabulary shared by automatic assignment and the public
# setter. The ordinal is project-local: global Agent ids are intentionally not
# part of a human-audible preference, because gaps created in unrelated
# projects would make the first two colleagues in one project collide.
NOTIFY_SOUND_NAMES: tuple[str, ...] = (
    "chime",
    "low",
    "high",
    "soft",
    "click",
    "double",
    "rising",
    "falling",
    "knock",
    "pulse",
    "bell",
    "sparkle",
)


async def _resolve_new_agent_display_name(
    session: AsyncSession,
    project_id: int,
    canonical_name: str,
    explicit_label: str | None,
) -> str | None:
    """Pick the display alias for a brand-new Agent row, inside its insert tx.

    An explicitly supplied label wins and is validated with the same clash rule
    as ``set_agent_display_name``: it may not equal another agent's name or
    alias in this project (case-insensitive). A generated adjective+noun
    candidate that collides is simply retried; after
    ``_GENERATED_ALIAS_ATTEMPTS`` misses the row is created without an alias,
    because the alias is presentation only and must never fail provisioning.
    Runs inside the caller's IMMEDIATE transaction, so the check-then-insert
    cannot race another writer.
    """
    clash_sql = text(
        "SELECT name FROM agents WHERE project_id = :pid "
        "AND lower(name) != lower(:own) "
        "AND (lower(name) = lower(:label) "
        "OR lower(COALESCE(display_name, '')) = lower(:label)) LIMIT 1"
    )
    if explicit_label is not None:
        clash = (
            await session.execute(
                clash_sql,
                {"pid": project_id, "own": canonical_name, "label": explicit_label},
            )
        ).fetchone()
        if clash is not None:
            raise ToolExecutionError(
                error_type="INVALID_ARGUMENT",
                message=(
                    f"Display name {explicit_label!r} is already taken by agent "
                    f"{clash[0]!r} in this project, as its name or its alias."
                ),
                recoverable=True,
                data={
                    "argument": "display_name",
                    "provided": explicit_label,
                    "conflicts_with": clash[0],
                },
            )
        return explicit_label
    for _ in range(_GENERATED_ALIAS_ATTEMPTS):
        candidate = generate_agent_name()
        clash = (
            await session.execute(
                clash_sql,
                {"pid": project_id, "own": canonical_name, "label": candidate},
            )
        ).fetchone()
        if clash is None:
            return candidate
    return None


async def _resolve_new_agent_notify_sound(
    session: AsyncSession,
    project_id: int,
) -> str:
    """Assign a stable default from the dense Agent order in one project."""
    result = await session.execute(
        select(Agent.id)
        .where(cast(Any, Agent.project_id == project_id))
        .order_by(cast(Any, Agent.id))
    )
    project_position = len(result.scalars().all())
    return NOTIFY_SOUND_NAMES[project_position % len(NOTIFY_SOUND_NAMES)]


def _sanitize_display_name_argument(value: str | None) -> str | None:
    """Normalize a caller-supplied display label; empty collapses to None.

    Mirrors ``set_agent_display_name``: control characters would corrupt every
    rendering of the label, and 128 characters is the column contract.
    """
    label = (value or "").strip()
    label = "".join(ch for ch in label if ch.isprintable())
    if len(label) > 128:
        label = label[:128].rstrip()
    return label or None


async def _create_agent_record(
    project: Project,
    name: str,
    program: str,
    model: str,
    task_description: str,
    registration_token: str,
    attachments_policy: str,
    display_name: str | None = None,
) -> Agent:
    if project.id is None:
        raise ValueError("Project must have an id before creating agents.")
    await ensure_schema()
    async with get_immediate_session() as session:
        await _revalidate_project_lifetime_in_session(
            session,
            project=project,
            action="Agent creation",
        )
        alias = await _resolve_new_agent_display_name(
            session, project.id, name, display_name
        )
        notify_sound = await _resolve_new_agent_notify_sound(session, project.id)
        agent = Agent(
            project_id=project.id,
            name=name,
            program=program,
            model=model,
            task_description=task_description,
            attachments_policy=attachments_policy,
            registration_token=registration_token,
            provisioning_state="provisioning",
            display_name=alias,
            notify_sound=notify_sound,
        )
        session.add(agent)
        await session.commit()
        return agent


async def _get_or_create_agent(
    project: Project,
    name: Optional[str],
    program: str,
    model: str,
    task_description: str,
    settings: Settings,
    *,
    registration_token_on_create: str | None = None,
    attachments_policy: str | None = None,
    update_existing: bool = True,
    expected_existing_agent_id: int | None = None,
    expected_existing_agent_generation: str | None = None,
    expected_project_generation: str | None = None,
    display_name: str | None = None,
) -> tuple[Agent, bool]:
    if project.id is None:
        raise ValueError("Project must have an id before creating agents.")
    archive = await ensure_archive(settings, project.slug)

    async def reject_renamed_identity(requested_name: str) -> None:
        tombstone = await get_identity_rename_tombstone(archive, requested_name)
        if tombstone is None:
            return
        replacement = str(tombstone["new_name"])
        raise ToolExecutionError(
            "IDENTITY_RENAMED",
            (
                f"IDENTITY_RENAMED: Agent identity '{requested_name}' was permanently renamed "
                f"to '{replacement}'. Use the new identity and migrate the existing local "
                "credential key; the old address will never be registered again."
            ),
            recoverable=True,
            data={
                "old_name": str(tombstone["old_name"]),
                "new_name": replacement,
                "agent_id": tombstone["agent_id"],
            },
        )

    if name is None or not name.strip():
        raise ToolExecutionError(
            "NAME_REQUIRED",
            (
                "A durable Agent requires an explicit stable name. Random "
                "adjective+noun identities are no longer generated; use a "
                "client-os-host-slot identity such as 'codex-wsl-home-1'."
            ),
            recoverable=True,
            data={"field": "name"},
        )
    requested_name = name.strip()
    await reject_renamed_identity(requested_name)
    if not validate_thread_id_format(requested_name):
        raise ToolExecutionError(
            "INVALID_AGENT_NAME",
            (
                f"Invalid agent name '{requested_name}'. Use a stable explicit "
                "identifier containing '-', '_' or '.', such as "
                "'codex-wsl-home-1'."
            ),
            recoverable=True,
            data={
                "provided_name": requested_name,
                "valid_examples": ["codex-wsl-home-1", "claude-linux-ci-1"],
            },
        )

    desired_name = requested_name
    explicit_name_used = True
    window_uuid = getattr(settings, "window_identity_uuid", "") or ""
    ttl_days = getattr(settings, "window_identity_ttl_days", 30)
    window_identity: Optional[WindowIdentity] = None
    await reject_renamed_identity(desired_name)
    await ensure_schema()
    newly_created = False
    existing_update_pending = False
    async with get_immediate_session() as session:
        current_project = await session.get(Project, project.id)
        if (
            current_project is None
            or current_project.slug != project.slug
            or current_project.project_generation != project.project_generation
            or (
                expected_project_generation is not None
                and current_project.project_generation
                != expected_project_generation
            )
        ):
            raise ToolExecutionError(
                "PROJECT_IDENTITY_STALE",
                "The authenticated project lifetime no longer exists.",
                recoverable=True,
                data={"project_key": project.human_key},
            )
        for _attempt in range(5):
            # Use case-insensitive matching to be consistent with _agent_name_exists() and _get_agent()
            result = await session.execute(
                select(Agent).where(
                    cast(Any, Agent.project_id == project.id),
                    cast(Any, func.lower(Agent.name) == desired_name.lower()),
                )
            )
            agent = result.scalars().first()
            if agent:
                if (
                    expected_existing_agent_id is not None
                    and agent.id != expected_existing_agent_id
                ) or (
                    expected_existing_agent_generation is not None
                    and agent.agent_generation
                    != expected_existing_agent_generation
                ):
                    raise RuntimeError(
                        f"Agent lifetime '{desired_name}' changed while registration was authenticated."
                    )
                if not update_existing:
                    return agent, False
                existing_update_pending = True
                break

            if not validate_client_platform_host_agent_id(desired_name):
                raise ToolExecutionError(
                    "INVALID_DURABLE_AGENT_NAME",
                    (
                        f"New durable Agent '{desired_name}' must match "
                        "client-os-host-slot, for example 'codex-wsl-home-1'. "
                        "Existing legacy identities remain authenticatable."
                    ),
                    recoverable=True,
                    data={"provided_name": desired_name},
                )

            alias = await _resolve_new_agent_display_name(
                session, project.id, desired_name, display_name
            )
            notify_sound = await _resolve_new_agent_notify_sound(
                session, project.id
            )
            candidate = Agent(
                project_id=project.id,
                name=desired_name,
                program=program,
                model=model,
                task_description=task_description,
                attachments_policy=attachments_policy or "auto",
                registration_token=registration_token_on_create,
                provisioning_state="provisioning",
                display_name=alias,
                notify_sound=notify_sound,
            )
            if (
                expected_existing_agent_id is not None
                or expected_existing_agent_generation is not None
            ):
                raise NoResultFound(
                    f"Authenticated agent id '{expected_existing_agent_id}' no longer exists."
                )
            session.add(candidate)
            try:
                await session.commit()
                agent = candidate
                newly_created = True
                break
            except IntegrityError:
                await session.rollback()
                with suppress(Exception):
                    session.expunge(candidate)

                if explicit_name_used:
                    # Another concurrent call created this identity; treat as idempotent update.
                    result = await session.execute(
                        select(Agent).where(
                            cast(Any, Agent.project_id == project.id),
                            cast(Any, func.lower(Agent.name) == desired_name.lower()),
                        )
                    )
                    agent = result.scalars().first()
                    if agent is None:
                        raise
                    if (
                        expected_existing_agent_id is not None
                        and agent.id != expected_existing_agent_id
                    ) or (
                        expected_existing_agent_generation is not None
                        and agent.agent_generation
                        != expected_existing_agent_generation
                    ):
                        raise RuntimeError(
                            f"Agent lifetime '{desired_name}' changed while registration was authenticated."
                        ) from None
                    if not update_existing:
                        return agent, False
                    existing_update_pending = True
                    break

                raise
        else:
            raise RuntimeError("Failed to create a unique agent after multiple retries.")
    try:
        # Associate explicit-name agents with their optional window identity
        # before profile publication and activation.  This belongs inside the
        # provisioning failure boundary: a failed window lookup must not leave
        # an undiscoverable row holding the durable name and its one-time token.
        if (
            window_uuid
            and _validate_window_uuid(window_uuid)
            and window_identity is None
            and explicit_name_used
        ):
            window_identity = await _get_window_identity(project, window_uuid)
            if window_identity is None:
                window_identity = await _create_window_identity(
                    project,
                    window_uuid,
                    agent.name,
                    ttl_days,
                )
            else:
                await _touch_window_identity(window_identity, ttl_days)

        async with _archive_write_lock(archive):
            if existing_update_pending:
                # Keep an authenticated metadata update and its Git profile in
                # one failure boundary.  The immediate transaction is not
                # committed until profile publication succeeds, so a failed
                # publication leaves both stores on the previous version.
                async with get_immediate_session() as session:
                    _db_project, db_agent, _db_execution = (
                        await _revalidate_agent_lifetime_in_session(
                            session,
                            project=project,
                            agent=agent,
                            action="authenticated Agent profile update",
                        )
                    )
                    previous_agent_dict = _agent_to_dict(db_agent)
                    db_agent.program = program
                    db_agent.model = model
                    db_agent.task_description = task_description
                    if attachments_policy is not None:
                        db_agent.attachments_policy = attachments_policy
                    db_agent.last_active_ts = _naive_utc()
                    session.add(db_agent)
                    agent_dict = _agent_to_dict(db_agent)
                    if window_identity is not None:
                        for profile in (previous_agent_dict, agent_dict):
                            profile["window_id"] = window_identity.window_uuid
                            profile["window_display_name"] = (
                                window_identity.display_name
                            )
                    try:
                        await write_agent_profile(archive, agent_dict)
                        await session.commit()
                    except Exception:
                        # write_agent_profile commits independently of SQLite.
                        # Restore the prior projection if a later DB step fails;
                        # a pre-write failure makes this an idempotent rewrite.
                        with suppress(Exception):
                            await write_agent_profile(archive, previous_agent_dict)
                        raise
                    agent = db_agent
            else:
                profile_agent = await _revalidate_agent_profile_lifetime(
                    project=project,
                    agent=agent,
                )
                agent_dict = _agent_to_dict(profile_agent)
                if window_identity is not None:
                    agent_dict["window_id"] = window_identity.window_uuid
                    agent_dict["window_display_name"] = window_identity.display_name
                await write_agent_profile(archive, agent_dict)
        if newly_created:
            agent = await _activate_provisioned_agent_lifetime(
                project=project,
                agent=agent,
            )
    except Exception:
        # Roll back the DB record if the archive write fails and we just
        # created the agent.  This keeps the two stores consistent so the
        # caller doesn't receive an error while the agent already exists in
        # the DB (issue #121).
        if newly_created:
            with suppress(Exception):
                await _rollback_created_agent_lifetime(
                    project=project,
                    agent=agent,
                )
        raise
    return agent, newly_created


async def _get_agent(project: Project, name: str) -> Agent:
    """Get agent by name with helpful error messages and suggestions."""
    await ensure_schema()

    # Validate input
    if not name or not name.strip():
        raise ToolExecutionError(
            "INVALID_ARGUMENT",
            f"Agent name cannot be empty. Provide a valid agent name for project '{project.human_key}'.",
            recoverable=True,
            data={"parameter": "agent_name", "provided": repr(name), "project": project.slug},
        )

    # Detect placeholder values (indicates unconfigured hooks/settings)
    _agent_placeholder_patterns = [
        "YOUR_AGENT",
        "YOUR_AGENT_NAME",
        "AGENT_NAME",
        "PLACEHOLDER",
        "<AGENT>",
        "{AGENT}",
        "$AGENT",
    ]
    name_upper = name.upper().strip()
    for pattern in _agent_placeholder_patterns:
        if pattern in name_upper or name_upper == pattern:
            raise ToolExecutionError(
                "CONFIGURATION_ERROR",
                f"Detected placeholder value '{name}' instead of a real agent name. "
                f"This typically means a hook or integration script hasn't been configured yet. "
                "Replace placeholder values with your durable Agent name "
                "(for example, 'codex-wsl-home-1').",
                recoverable=True,
                data={
                    "parameter": "agent_name",
                    "provided": name,
                    "detected_placeholder": pattern,
                    "fix_hint": "Update AGENT_MAIL_AGENT or agent_name in your configuration",
                },
            )

    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(
                Agent.project_id == project.id,
                func.lower(Agent.name) == name.lower(),
                Agent.provisioning_state == "active",
            )
        )
        agent = result.scalars().first()
        if agent:
            return agent

    # Agent not found - provide helpful suggestions
    suggestions = await _find_similar_agents(project, name)
    available_agents = await _list_project_agents(project)

    # Check for common mistakes (Unix username, program name, etc.)
    mistake = _detect_agent_name_mistake(name)
    mistake_hint = ""
    if mistake:
        mistake_hint = f"\n\nHINT: {mistake[1]}"

    if suggestions:
        # Found similar names - probably a typo
        suggestion_text = ", ".join([f"'{s[0]}'" for s in suggestions[:3]])
        raise ToolExecutionError(
            mistake[0] if mistake else "NOT_FOUND",
            f"Agent '{name}' not found in project '{project.human_key}'. Did you mean: {suggestion_text}? "
            f"Agent names are case-insensitive but must match exactly.{mistake_hint}",
            recoverable=True,
            data={
                "agent_name": name,
                "project": project.slug,
                "suggestions": [{"name": s[0], "score": round(s[1], 2)} for s in suggestions],
                "available_agents": available_agents,
                "mistake_type": mistake[0] if mistake else None,
            },
        )
    elif available_agents:
        # No similar names but project has agents
        agents_list = ", ".join([f"'{a}'" for a in available_agents[:5]])
        more_text = f" and {len(available_agents) - 5} more" if len(available_agents) > 5 else ""
        raise ToolExecutionError(
            mistake[0] if mistake else "NOT_FOUND",
            f"Agent '{name}' not found in project '{project.human_key}'. "
            f"Available agents: {agents_list}{more_text}. "
            "Only a durable parent client or operator may provision a missing "
            f"mailbox with register_agent; native subagents report through their parent.{mistake_hint}",
            recoverable=True,
            data={
                "agent_name": name,
                "project": project.slug,
                "available_agents": available_agents,
                "mistake_type": mistake[0] if mistake else None,
            },
        )
    else:
        # Project has no agents
        raise ToolExecutionError(
            mistake[0] if mistake else "NOT_FOUND",
            f"Agent '{name}' not found. Project '{project.human_key}' has no registered agents yet. "
            "A durable parent client or operator must provision an explicit "
            "mailbox first; native subagents report through their parent. "
            f"Example: register_agent(project_key='{project.slug}', program='claude-code', "
            f"model='opus-4', name='claude-linux-ci-1'){mistake_hint}",
            recoverable=True,
            data={"agent_name": name, "project": project.slug, "available_agents": [], "mistake_type": mistake[0] if mistake else None},
        )


async def _find_agent_optional(project: Project, name: str | None) -> Agent | None:
    """Return an agent by name without raising when it does not exist."""
    if project.id is None or not name or not name.strip():
        return None
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(
                cast(Any, Agent.project_id == project.id),
                cast(Any, func.lower(Agent.name) == name.lower()),
                cast(Any, Agent.provisioning_state == "active"),
            )
        )
        return result.scalars().first()


def _target_registration_required_error(
    project: Project,
    target_name: str,
) -> ToolExecutionError:
    """Explain the ownership boundary for a missing contact recipient."""
    return ToolExecutionError(
        "TARGET_NOT_REGISTERED",
        f"Target Agent '{target_name}' is not registered in project "
        f"'{project.human_key}'. The target must self-register, or an operator "
        "must explicitly provision its durable mailbox, before another Agent "
        "can request contact.",
        recoverable=True,
        data={
            "agent_name": target_name,
            "project": project.slug,
            "required_action": "target_self_register_or_operator_provision",
        },
    )


async def _ensure_agent_registration_token(
    agent: Agent,
    *,
    project: Project | None = None,
) -> tuple[Agent, str]:
    """Atomically ensure an agent has one stable registration token.

    Concurrent session starts may all observe an identity whose token has not
    been initialized yet.  An IMMEDIATE transaction serializes initialization
    and revalidates the immutable Agent generation before reading or returning
    any credential.  A deleted-and-recreated row can therefore never disclose
    its token to a caller authenticated for the predecessor lifetime.
    """
    if agent.id is None:
        raise ValueError("Agent must have an id before ensuring a registration token.")

    candidate = secrets.token_urlsafe(32)
    async with get_immediate_session() as session:
        db_agent = await session.get(Agent, agent.id)
        if (
            db_agent is None
            or db_agent.project_id != agent.project_id
            or db_agent.name != agent.name
            or db_agent.agent_generation != agent.agent_generation
        ):
            raise ToolExecutionError(
                "AGENT_IDENTITY_STALE",
                "The exact Agent lifetime no longer exists; refusing registration-token access.",
                recoverable=True,
                data={"agent_name": agent.name},
            )
        if project is not None:
            await _revalidate_project_lifetime_in_session(
                session,
                project=project,
                action="registration-token access",
            )
        if not db_agent.registration_token:
            db_agent.registration_token = candidate
            session.add(db_agent)
        await session.commit()
        await session.refresh(db_agent)
        token = db_agent.registration_token
        if not token:
            raise RuntimeError(f"Agent id '{agent.id}' still has no registration token.")
        return db_agent, token


async def _rotate_agent_registration_token(
    agent: Agent,
    *,
    project: Project,
    registration_token: str,
    new_registration_token: str,
) -> tuple[Agent, bool]:
    """CAS one exact Agent lifetime to a caller-generated replacement token.

    The caller owns the replacement so it can journal the value before the RPC.
    Treating an already-current replacement as success makes an ambiguous lost
    response safely retryable without ever returning a credential from the
    server. ``BEGIN IMMEDIATE`` serializes the comparison and update on SQLite.
    """
    if agent.id is None:
        raise ValueError("Agent must have an id before rotating a registration token.")
    if not _REGISTRATION_TOKEN_PATTERN.fullmatch(new_registration_token):
        raise ToolExecutionError(
            "INVALID_NEW_REGISTRATION_TOKEN",
            "new_registration_token must be exactly 64 lowercase hexadecimal characters.",
            recoverable=True,
            data={"field": "new_registration_token"},
        )
    if not registration_token:
        raise ToolExecutionError(
            "AUTHENTICATION_REQUIRED",
            f"rotate_registration_token requires registration_token for agent '{agent.name}'.",
            recoverable=True,
            data={
                "agent_name": agent.name,
                "project_key": project.human_key,
                "token_param": "registration_token",
            },
        )
    if hmac.compare_digest(registration_token, new_registration_token):
        raise ToolExecutionError(
            "INVALID_NEW_REGISTRATION_TOKEN",
            "new_registration_token must differ from registration_token.",
            recoverable=True,
            data={"field": "new_registration_token"},
        )

    async with get_immediate_session() as session:
        await _revalidate_project_lifetime_in_session(
            session,
            project=project,
            action="registration-token rotation",
        )
        db_agent = await session.get(Agent, agent.id)
        if (
            db_agent is None
            or db_agent.project_id != agent.project_id
            or db_agent.name != agent.name
            or db_agent.agent_generation != agent.agent_generation
        ):
            raise ToolExecutionError(
                "AGENT_IDENTITY_STALE",
                "The exact Agent lifetime no longer exists; refusing registration-token rotation.",
                recoverable=True,
                data={"agent_name": agent.name},
            )

        stored_token = (db_agent.registration_token or "").strip()
        if not stored_token:
            raise ToolExecutionError(
                "AUTHENTICATION_REQUIRED",
                f"Agent '{agent.name}' does not have a registration token to rotate.",
                recoverable=True,
                data={
                    "agent_name": agent.name,
                    "project_key": project.human_key,
                    "token_param": "registration_token",
                },
            )

        already_current = hmac.compare_digest(stored_token, new_registration_token)
        if not already_current:
            if not hmac.compare_digest(stored_token, registration_token):
                raise ToolExecutionError(
                    "AUTHENTICATION_REQUIRED",
                    f"Invalid registration_token for agent '{agent.name}'.",
                    recoverable=True,
                    data={
                        "agent_name": agent.name,
                        "project_key": project.human_key,
                        "token_param": "registration_token",
                    },
                )
            db_agent.registration_token = new_registration_token
            session.add(db_agent)

        await session.commit()
        await session.refresh(db_agent)
        return db_agent, already_current


def _message_visible_to_agent_clause(agent_id: int) -> Any:
    """Return a SQL clause limiting messages to sender-visible or recipient-visible rows."""
    return or_(
        cast(Any, Message.sender_id) == agent_id,
        exists(
            select(MessageRecipient.message_id).where(
                cast(Any, MessageRecipient.message_id == Message.id),
                cast(Any, MessageRecipient.agent_id == agent_id),
            )
        ),
    )


def _active_approved_agent_link_clause(now: datetime | None = None) -> Any:
    """Return a SQL clause for approved contact links whose TTL has not expired."""
    naive_now = _naive_utc(now or datetime.now(timezone.utc))
    return and_(
        cast(Any, AgentLink.status == "approved"),
        or_(
            cast(Any, AgentLink.expires_ts).is_(None),
            cast(Any, AgentLink.expires_ts) > naive_now,
        ),
    )


def _agent_link_is_expired(link: AgentLink, now: datetime | None = None) -> bool:
    """Return whether a pending/approved contact link has expired."""
    if link.expires_ts is None or link.status not in {"approved", "pending"}:
        return False
    naive_now = _naive_utc(now or datetime.now(timezone.utc))
    return link.expires_ts <= naive_now


async def _get_agents_batch(project: Project, names: Sequence[str]) -> dict[str, Agent]:
    """Batch lookup agents by name with `_get_agent`-equivalent error reporting."""
    await ensure_schema()
    if not names:
        return {}
    if project.id is None:
        raise ValueError("Project must have an id before querying agents.")

    lowered_names: list[str] = []
    seen: set[str] = set()
    for name in names:
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        lowered_names.append(lowered)

    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(
                Agent.project_id == project.id,
                func.lower(Agent.name).in_(lowered_names),
                Agent.provisioning_state == "active",
            )
        )
        agents = result.scalars().all()

    by_lower = {agent.name.lower(): agent for agent in agents}
    resolved: dict[str, Agent] = {}
    missing: list[str] = []
    for name in names:
        agent = by_lower.get(name.lower())
        if agent is None:
            missing.append(name)
        else:
            resolved[name] = agent

    if missing:
        # Reuse the exact error logic from _get_agent for the first missing entry.
        await _get_agent(project, missing[0])

    return resolved


async def _get_agents_batch_lenient(project: Project, names: Sequence[str]) -> dict[str, Agent]:
    """Batch lookup agents by name, silently skipping missing agents.

    Unlike _get_agents_batch, this does NOT raise errors for missing agents.
    Use this for contact policy enforcement where missing recipients should
    be skipped rather than treated as errors.

    Parameters
    ----------
    project : Project
        The project to look up agents in.
    names : Sequence[str]
        Agent names to look up.

    Returns
    -------
    dict[str, Agent]
        Mapping from original name to Agent. Missing agents are omitted.
    """
    await ensure_schema()
    if not names:
        return {}
    if project.id is None:
        return {}

    # Deduplicate and lowercase for efficient IN query
    lowered_names: list[str] = []
    seen: set[str] = set()
    for name in names:
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        lowered_names.append(lowered)

    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(
                Agent.project_id == project.id,
                func.lower(Agent.name).in_(lowered_names),
                Agent.provisioning_state == "active",
            )
        )
        agents = result.scalars().all()

    # Build lookup by lowercase name
    by_lower = {agent.name.lower(): agent for agent in agents}

    # Resolve original names to agents (preserving original case in keys)
    resolved: dict[str, Agent] = {}
    for name in names:
        agent = by_lower.get(name.lower())
        if agent is not None:
            resolved[name] = agent

    return resolved


async def _create_file_reservation(
    project: Project,
    agent: Agent,
    execution: AgentExecution,
    path: str,
    exclusive: bool,
    reason: str,
    ttl_seconds: int,
) -> FileReservation:
    if project.id is None or agent.id is None:
        raise ValueError("Project and agent must have ids before creating file_reservations.")
    expires = _naive_utc() + timedelta(seconds=ttl_seconds)
    await ensure_schema()
    async with get_session() as session:
        file_reservation = FileReservation(
            project_id=project.id,
            agent_id=agent.id,
            execution_id=execution.id,
            path_pattern=path,
            exclusive=exclusive,
            reason=reason,
            expires_ts=expires,
        )
        session.add(file_reservation)
        await session.commit()
        await session.refresh(file_reservation)
    await _reconcile_pending_file_reservation_artifacts(project)
    return file_reservation


def _file_reservation_payload(
    project: Project,
    reservation: FileReservation,
    # Agent is Optional because expire/release records may carry None when the
    # reservation is orphaned (owning Agent row deleted). The payload emits
    # agent=null in that case and falls back to `reservation.agent_id` for
    # forensics. (#161)
    agent: Optional[Agent],
    *,
    branch: Optional[str] = None,
    worktree: Optional[str] = None,
    reason_override: Optional[str] = None,
    execution_status: Optional[str] = None,
    ancestor_execution_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Build a normalized payload for Git archive file_reservation records.

    If released_ts is set, clamp expires_ts to released_ts so client-side guards
    treat the reservation as inactive even if the original expiry was later.
    """
    released_dt = _ensure_utc(reservation.released_ts)
    expires_dt = _ensure_utc(reservation.expires_ts)
    if released_dt and expires_dt:
        if released_dt < expires_dt:
            expires_dt = released_dt
    elif released_dt and expires_dt is None:
        expires_dt = released_dt

    payload: dict[str, Any] = {
        "id": reservation.id,
        "project": project.human_key,
        # `agent` is None when the reservation is orphaned (#161). Emit null in
        # JSON and keep `agent_id` for forensics so sweepers / dashboards can
        # still link the row back to the deleted Agent row.
        "agent": agent.name if agent is not None else None,
        "agent_id": reservation.agent_id,
        "execution_id": reservation.execution_id,
        "ancestor_execution_ids": list(ancestor_execution_ids),
        "execution_status": execution_status,
        "origin": reservation.origin,
        "orphaned": agent is None
        or (
            reservation.execution_id is not None
            and execution_status != "active"
        ),
        "legacy_unscoped": reservation.execution_id is None,
        "path_pattern": reservation.path_pattern,
        "exclusive": reservation.exclusive,
        "reason": reason_override if reason_override is not None else reservation.reason,
        "created_ts": _iso(reservation.created_ts),
        "expires_ts": _iso(expires_dt) if expires_dt else _iso(reservation.expires_ts),
        "archive_revision": reservation.archive_revision,
    }
    if released_dt is not None:
        payload["released_ts"] = _iso(released_dt)
    if branch:
        payload["branch"] = branch
    if worktree:
        payload["worktree"] = worktree
    return payload


async def _write_file_reservation_records(
    project: Project,
    # Optional[Agent] in each tuple: expired-pair records may carry None when
    # the reservation outlived its owning Agent row. (#161)
    records: Sequence[tuple[FileReservation, Optional[Agent]]],
    *,
    archive: ProjectArchive | None = None,
    archive_locked: bool = False,
    reason_override: Optional[str] = None,
    branch_override: Optional[str] = None,
    worktree_override: Optional[str] = None,
) -> None:
    if not records:
        return
    if archive_locked and archive is None:
        raise ValueError("archive_locked=True requires a provided archive")
    settings = get_settings()
    target_archive = archive or await ensure_archive(settings, project.slug)

    execution_by_id: dict[str, AgentExecution] = {}
    referenced_execution_ids = [
        reservation.execution_id
        for reservation, _agent in records
        if reservation.execution_id is not None
    ]
    if referenced_execution_ids:
        if project.id is None:
            raise ValueError("Project must have an id before writing reservation records.")
        async with get_session() as session:
            executions = await _load_execution_lineage_rows(
                session,
                referenced_execution_ids,
                project_id=project.id,
            )
        execution_by_id = {execution.id: execution for execution in executions}

    async def _write_all() -> None:
        payloads: list[dict[str, Any]] = []
        for reservation, agent in records:
            execution = (
                execution_by_id.get(reservation.execution_id)
                if reservation.execution_id is not None
                else None
            )
            payloads.append(
                _file_reservation_payload(
                    project,
                    reservation,
                    agent,
                    branch=(
                        branch_override
                        or (execution.branch if execution is not None else None)
                    ),
                    worktree=(
                        worktree_override
                        or (
                            execution.worktree_path
                            if execution is not None
                            else None
                        )
                    ),
                    reason_override=reason_override,
                    execution_status=(execution.status if execution is not None else None),
                    ancestor_execution_ids=(
                        _execution_ancestor_ids(
                            list(execution_by_id.values()), execution
                        )
                        if execution is not None
                        else ()
                    ),
                )
            )
        await write_file_reservation_records(target_archive, payloads)

    if archive_locked:
        await _write_all()
        return

    async with _archive_write_lock(target_archive):
        await _write_all()


async def _ack_file_reservation_archive_revisions(
    revisions: Sequence[tuple[int, int]],
) -> int:
    """Acknowledge only reservation revisions that still match the written snapshot."""
    if not revisions:
        return 0
    acknowledged = 0
    async with get_immediate_session() as session:
        for reservation_id, archive_revision in revisions:
            result = await session.execute(
                update(FileReservation)
                .where(
                    cast(Any, FileReservation.id) == reservation_id,
                    cast(Any, FileReservation.archive_revision)
                    == archive_revision,
                    cast(Any, FileReservation.archive_synced_revision)
                    < archive_revision,
                )
                .values(archive_synced_revision=archive_revision)
            )
            acknowledged += int(getattr(result, "rowcount", 0) or 0)
        await session.commit()
    return acknowledged


async def _reconcile_pending_file_reservation_artifacts(
    project: Project,
    *,
    archive: ProjectArchive | None = None,
    archive_locked: bool = False,
) -> int:
    """Publish and exactly acknowledge all pending reservation artifact revisions.

    The archive lock serializes writers, while the conditional DB acknowledgement
    prevents a concurrent mutation from being falsely marked as published. If a
    mutation wins between the snapshot and acknowledgement, the loop reloads and
    writes its newer revision before returning.
    """
    if project.id is None:
        raise ValueError("Project must have an id before reconciling file reservations.")
    if archive_locked and archive is None:
        raise ValueError("archive_locked=True requires a provided archive")
    target_archive = archive or await ensure_archive(get_settings(), project.slug)

    async def _reconcile_locked() -> int:
        acknowledged_total = 0
        revision_races = 0
        while revision_races < 8:
            async with get_session() as session:
                await _revalidate_project_lifetime_in_session(
                    session,
                    project=project,
                    action="file-reservation archive reconciliation",
                )
                rows = (
                    await session.execute(
                        select(FileReservation, Agent)
                        .outerjoin(
                            Agent,
                            cast(Any, FileReservation.agent_id) == Agent.id,
                        )
                        .where(
                            cast(Any, FileReservation.project_id) == project.id,
                            cast(Any, FileReservation.archive_synced_revision)
                            < cast(Any, FileReservation.archive_revision),
                        )
                        .order_by(asc(cast(Any, FileReservation.id)))
                        .limit(_FILE_RESERVATION_ARCHIVE_BATCH_SIZE)
                    )
                ).all()
            records = [
                cast(tuple[FileReservation, Optional[Agent]], row) for row in rows
            ]
            if not records:
                return acknowledged_total
            revisions = [
                (reservation.id, reservation.archive_revision)
                for reservation, _agent in records
                if reservation.id is not None
            ]
            await _write_file_reservation_records(
                project,
                records,
                archive=target_archive,
                archive_locked=True,
            )
            acknowledged = await _ack_file_reservation_archive_revisions(
                revisions
            )
            acknowledged_total += acknowledged
            if acknowledged == len(revisions):
                revision_races = 0
                continue
            revision_races += 1

        raise RuntimeError(
            "File reservation artifacts changed continuously during reconciliation; "
            "pending DB revisions were preserved for retry."
        )

    if archive_locked:
        return await _reconcile_locked()
    async with _archive_write_lock(target_archive):
        return await _reconcile_locked()


async def _collect_file_reservation_statuses(
    project: Project,
    *,
    include_released: bool = False,
    now: Optional[datetime] = None,
) -> list[FileReservationStatus]:
    if project.id is None:
        return []
    await ensure_schema()
    moment = now or datetime.now(timezone.utc)
    settings = get_settings()
    inactivity_seconds = max(0, int(settings.file_reservation_inactivity_seconds))
    activity_grace = max(0, int(settings.file_reservation_activity_grace_seconds))

    async with get_session() as session:
        stmt = (
            select(FileReservation, Agent, AgentExecution)
            # LEFT JOIN so orphaned reservations (agent row deleted or agent_id
            # is NULL) are still surfaced; the staleness sweeper then has a
            # chance to auto-release them instead of letting them pin the
            # path forever. (#161)
            .outerjoin(Agent, cast(Any, FileReservation.agent_id) == Agent.id)
            .outerjoin(
                AgentExecution,
                cast(Any, FileReservation.execution_id) == AgentExecution.id,
            )
            .where(FileReservation.project_id == project.id)
            .order_by(asc(FileReservation.created_ts))
        )
        if not include_released:
            stmt = stmt.where(cast(Any, FileReservation.released_ts).is_(None))
        result = await session.execute(stmt)
        rows = result.all()
        if not rows:
            return []
        project_executions = await _load_execution_lineage_rows(
            session,
            [
                execution.id
                for _reservation, _agent, execution in rows
                if execution is not None
            ],
            project_id=project.id,
        )
        agent_ids = [
            agent.id
            for _, agent, _execution in rows
            if agent is not None and agent.id is not None
        ]
        send_map: dict[int, Optional[datetime]] = {}
        ack_map: dict[int, Optional[datetime]] = {}
        read_map: dict[int, Optional[datetime]] = {}
        if agent_ids:
            send_result = await session.execute(
                select(Message.sender_id, func.max(Message.created_ts))
                .where(
                    cast(Any, Message.project_id) == project.id,
                    cast(Any, Message.sender_id).in_(agent_ids),
                )
                .group_by(Message.sender_id)
            )
            send_map = {row[0]: _ensure_utc(row[1]) for row in send_result}
            ack_result = await session.execute(
                select(MessageRecipient.agent_id, func.max(MessageRecipient.ack_ts))
                .join(Message, MessageRecipient.message_id == Message.id)
                .where(
                    cast(Any, Message.project_id) == project.id,
                    cast(Any, MessageRecipient.agent_id).in_(agent_ids),
                    cast(Any, MessageRecipient.ack_ts).is_not(None),
                )
                .group_by(MessageRecipient.agent_id)
            )
            ack_map = {row[0]: _ensure_utc(row[1]) for row in ack_result}
            read_result = await session.execute(
                select(MessageRecipient.agent_id, func.max(MessageRecipient.read_ts))
                .join(Message, MessageRecipient.message_id == Message.id)
                .where(
                    cast(Any, Message.project_id) == project.id,
                    cast(Any, MessageRecipient.agent_id).in_(agent_ids),
                    cast(Any, MessageRecipient.read_ts).is_not(None),
                )
                .group_by(MessageRecipient.agent_id)
            )
            read_map = {row[0]: _ensure_utc(row[1]) for row in read_result}

    workspace = _project_workspace_path(project)
    repo = _open_repo_if_available(workspace) if workspace is not None else None

    statuses: list[FileReservationStatus] = []
    try:
        for reservation, agent, execution in rows:
            # Orphaned reservation: agent row is gone (or never existed).
            # Treat as perpetually inactive with no mail signal so the sweeper
            # auto-releases it; tag the reasons so callers can distinguish
            # `agent_missing` (NULL agent_id, never resolvable) from
            # `agent_unresolved` (had an id but row was deleted). (#161)
            agent_orphaned = agent is None
            if agent_orphaned:
                agent_id = None
                agent_last_active = None
                last_mail = None
            else:
                agent_id = agent.id or -1
                agent_last_active = _ensure_utc(agent.last_active_ts)
                last_mail = _max_datetime(
                    send_map.get(agent_id), ack_map.get(agent_id), read_map.get(agent_id)
                )
            execution_scoped = reservation.execution_id is not None
            execution_missing = execution_scoped and execution is None
            execution_status = execution.status if execution is not None else None
            execution_parent_id = (
                execution.parent_execution_id if execution is not None else None
            )
            execution_ancestor_ids = (
                _execution_ancestor_ids(
                    project_executions,
                    execution,
                )
                if execution is not None
                else []
            )
            execution_last_active = (
                _ensure_utc(execution.last_active_ts)
                if execution is not None
                else None
            )

            matched = False
            fs_activity: Optional[datetime] = None
            git_activity: Optional[datetime] = None

            if workspace is not None:
                # Offload the blocking filesystem+git probe to a thread so a
                # broad glob reservation can never starve the event loop, and
                # use a single glob-pathspec rev walk instead of one git fork
                # per matched file (#240).
                recent_after = moment - timedelta(seconds=activity_grace)
                matched, fs_activity, git_activity = await asyncio.to_thread(
                    _compute_reservation_activity,
                    workspace,
                    repo,
                    reservation.path_pattern,
                    recent_after=recent_after,
                )

            agent_inactive = (
                agent_last_active is None or (moment - agent_last_active).total_seconds() > inactivity_seconds
            )
            execution_inactive = (
                execution_last_active is None
                or (moment - execution_last_active).total_seconds()
                > inactivity_seconds
            )
            recent_mail = last_mail is not None and (moment - last_mail).total_seconds() <= activity_grace
            recent_fs = fs_activity is not None and (moment - fs_activity).total_seconds() <= activity_grace
            recent_git = git_activity is not None and (moment - git_activity).total_seconds() <= activity_grace

            if execution_scoped:
                stale = bool(
                    reservation.released_ts is None
                    and (
                        execution_missing
                        or execution_status != "active"
                        or (execution_inactive and not (recent_fs or recent_git))
                    )
                )
            else:
                stale = bool(
                    reservation.released_ts is None
                    and (
                        agent_orphaned
                        or (
                            agent_inactive
                            and not (recent_mail or recent_fs or recent_git)
                        )
                    )
                )
            reasons: list[str] = []
            if execution_scoped:
                if execution_missing:
                    reasons.append("execution_unresolved")
                elif execution_status != "active":
                    reasons.append(f"execution_{execution_status}")
                elif execution_inactive:
                    reasons.append(f"execution_inactive>{inactivity_seconds}s")
                else:
                    reasons.append("execution_recently_active")
                reasons.append("mail_activity_not_execution_signal")
            elif agent_orphaned:
                # Distinguish never-had-owner from owner-was-deleted; both are
                # terminal for the reservation but each tells a different
                # operational story (config bug vs cleanup hygiene).
                if reservation.agent_id is None:
                    reasons.append("agent_missing")
                else:
                    reasons.append("agent_unresolved")
            elif agent_inactive:
                reasons.append(f"agent_inactive>{inactivity_seconds}s")
            else:
                reasons.append("agent_recently_active")
            if execution_scoped:
                pass
            elif agent_orphaned:
                reasons.append("no_mail_activity_possible")
            elif recent_mail:
                reasons.append("mail_activity_recent")
            else:
                reasons.append(f"no_recent_mail_activity>{activity_grace}s")
            if matched:
                if recent_fs:
                    reasons.append("filesystem_activity_recent")
                else:
                    reasons.append(f"no_recent_filesystem_activity>{activity_grace}s")
                if recent_git:
                    reasons.append("git_activity_recent")
                else:
                    reasons.append(f"no_recent_git_activity>{activity_grace}s")
            else:
                reasons.append("path_pattern_unmatched")

            statuses.append(
                FileReservationStatus(
                    reservation=reservation,
                    agent=agent,
                    stale=stale,
                    stale_reasons=reasons,
                    last_agent_activity=agent_last_active,
                    execution_id=reservation.execution_id,
                    execution_status=execution_status,
                    execution_parent_id=execution_parent_id,
                    ancestor_execution_ids=execution_ancestor_ids,
                    orphaned=agent_orphaned
                    or (execution_scoped and execution_status != "active"),
                    legacy_unscoped=not execution_scoped,
                    last_execution_activity=execution_last_active,
                    last_mail_activity=last_mail,
                    last_fs_activity=fs_activity,
                    last_git_activity=git_activity,
                )
            )
    finally:
        # Cleanup: close repo if we opened one
        if repo is not None:
            with suppress(Exception):
                repo.close()
    return statuses


async def sweep_stale_agents(
    *,
    threshold_seconds: int,
    project_id: Optional[int] = None,
    exclude_agent_id: Optional[int] = None,
    require_no_active_reservations: bool = False,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Mark agents inactive for `threshold_seconds` as retired.

    A "long-running multi-agent project" can accumulate dozens of
    'active' agents whose sessions ended without an explicit
    `retire_agent` call. After ~30+ such accumulators, every new
    `send_message` with `broadcast=true` triggers `contact_approval`
    for the dead agents and silently fails delivery (issue #149).

    This sweep retires those agents (`retired_at = now`) so the
    contact-wall stops piling up. It is conservative:

    - Skips agents that are already retired.
    - Skips agents whose `last_active_ts` is within the threshold.
    - Optionally scopes to a single project_id.
    - Optionally excludes one agent (used by the on-demand tool so callers
      cannot retire their own authenticated identity).
    - Optionally skips agents with unexpired file reservations.
    - Always skips durable Agents that own an active AgentExecution. Execution
      heartbeat state is authoritative for process liveness.

    Returns one dict per retired agent, with project/agent identifiers
    plus the `last_active_ts` that triggered retirement, so the caller
    (background worker, CLI, or test) can log the action.
    """
    await ensure_schema()
    threshold = max(60, int(threshold_seconds))
    current = now or datetime.now(timezone.utc)
    cutoff_naive = _naive_utc(current) - timedelta(seconds=threshold)
    naive_now = _naive_utc(current)

    retired: list[dict[str, Any]] = []
    # BEGIN IMMEDIATE serializes the optional active-reservation check with
    # retirement. Without it, a reservation could be created after the check
    # but before the agent is retired, violating the caller's safety gate.
    async with get_immediate_session() as session:
        stmt = select(Agent, Project).join(Project, cast(Any, Agent.project_id) == Project.id).where(
            cast(Any, Agent.provisioning_state == "active"),
            cast(Any, Agent.retired_at).is_(None),
            cast(Any, Agent.last_active_ts) < cutoff_naive,
        )
        active_execution = (
            select(AgentExecution.id)
            .where(
                cast(Any, AgentExecution.agent_id) == Agent.id,
                cast(Any, AgentExecution.status) == "active",
            )
            .correlate(Agent)
        )
        stmt = stmt.where(~exists(active_execution))
        if project_id is not None:
            stmt = stmt.where(cast(Any, Agent.project_id) == project_id)
        if exclude_agent_id is not None:
            stmt = stmt.where(cast(Any, Agent.id) != exclude_agent_id)
        if require_no_active_reservations:
            active_reservation = (
                select(FileReservation.id)
                .where(
                    cast(Any, FileReservation.agent_id) == Agent.id,
                    cast(Any, FileReservation.released_ts).is_(None),
                    cast(Any, FileReservation.expires_ts) > naive_now,
                )
                .correlate(Agent)
            )
            stmt = stmt.where(~exists(active_reservation))
        stmt = stmt.order_by(cast(Any, Project.id), cast(Any, Agent.id))
        result = await session.execute(stmt)
        candidates: list[tuple[Agent, Project]] = [
            cast(tuple[Agent, Project], row) for row in result.all()
        ]
        if not candidates:
            return retired
        for agent, project in candidates:
            agent.retired_at = naive_now
            session.add(agent)
            retired.append(
                {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "project_id": project.id,
                    "project_key": project.human_key,
                    "last_active_ts": _iso(agent.last_active_ts),
                }
            )
        await session.commit()
    return retired


async def _expire_stale_file_reservations(
    project_id: int,
    *,
    archive: ProjectArchive | None = None,
    archive_locked: bool = False,
) -> list[FileReservationStatus]:
    await ensure_schema()
    now = datetime.now(timezone.utc)
    naive_now = _naive_utc(now)  # Compute once for consistency and efficiency

    project: Optional[Project] = None
    async with get_session() as session:
        project = await session.get(Project, project_id)
    if project is None:
        return []

    expired_pairs: list[tuple[FileReservation, Optional[Agent]]] = []
    # Release any entries whose TTL has already elapsed.
    # Use BEGIN IMMEDIATE so the release is immediately visible to
    # subsequent reserve calls on other connections (#130).
    async with get_immediate_session() as session:
        expired_rows = await session.execute(
            select(FileReservation, Agent)
            # LEFT JOIN — orphaned reservations whose owning agent has been
            # deleted must still expire on schedule, not pin the path. (#161)
            .outerjoin(Agent, cast(Any, FileReservation.agent_id) == Agent.id)
            .where(
                cast(Any, FileReservation.project_id) == project_id,
                cast(Any, FileReservation.released_ts).is_(None),
                cast(Any, FileReservation.expires_ts) < naive_now,  # SQLite needs naive datetime
            )
        )
        expired_pairs = [cast(tuple[FileReservation, Optional[Agent]], row) for row in expired_rows.all()]
        if expired_pairs:
            await session.execute(
                update(FileReservation)
                .where(
                    cast(Any, FileReservation.project_id) == project_id,
                    cast(Any, FileReservation.released_ts).is_(None),
                    cast(Any, FileReservation.expires_ts) < naive_now,  # SQLite needs naive datetime
                )
                .values(released_ts=naive_now)  # Use naive UTC for SQLite compatibility
            )
            await session.commit()
    statuses = await _collect_file_reservation_statuses(project, include_released=False, now=now)
    stale_statuses = [
        status
        for status in statuses
        if status.stale
        and status.reservation.origin == "auto"
        and status.reservation.id is not None
    ]
    stale_ids = [cast(int, status.reservation.id) for status in stale_statuses]
    if stale_ids:
        async with get_immediate_session() as session:
            await session.execute(
                update(FileReservation)
                .where(
                    cast(Any, FileReservation.project_id) == project_id,
                    cast(Any, FileReservation.id).in_(stale_ids),
                    cast(Any, FileReservation.released_ts).is_(None),
                )
                .values(released_ts=naive_now)  # Use naive UTC for SQLite compatibility
            )
            await session.commit()

        for status in stale_statuses:
            status.reservation.released_ts = naive_now

    # Reconcile even when this sweep did not release a new row. A previous
    # post-commit archive failure remains visible as a pending DB revision and
    # the next ordinary sweep must repair it rather than wait for the old TTL.
    await _reconcile_pending_file_reservation_artifacts(
        project,
        archive=archive,
        archive_locked=archive_locked,
    )

    return stale_statuses


def _file_reservations_conflict(
    existing: FileReservation,
    candidate_path: str,
    candidate_exclusive: bool,
    candidate_execution_id: str | None,
    candidate_agent_id: int,
    compatible_execution_ids: set[str] | None = None,
) -> bool:
    if existing.released_ts is not None:
        return False
    compatible_ids = compatible_execution_ids or set()
    if candidate_execution_id is not None and existing.execution_id in {
        candidate_execution_id,
        *compatible_ids,
    }:
        return False
    if (
        candidate_execution_id is None
        and existing.execution_id is None
        and existing.agent_id == candidate_agent_id
    ):
        return False
    if not existing.exclusive and not candidate_exclusive:
        return False
    # Virtual namespace reservations use exact-match only (bd-14z)
    candidate_virtual = _is_virtual_namespace(candidate_path)
    existing_virtual = _is_virtual_namespace(existing.path_pattern)
    if candidate_virtual or existing_virtual:
        # Virtual vs filesystem never conflict; virtual vs virtual = exact match
        if candidate_virtual != existing_virtual:
            return False
        return candidate_path.strip() == existing.path_pattern.strip()
    # Git wildmatch semantics; treat inputs as repo-root relative forward-slash paths
    def _normalize(p: str) -> str:
        return p.replace("\\", "/").lstrip("/")
    candidate_norm = _normalize(candidate_path)
    existing_norm = _normalize(existing.path_pattern)
    # If either side is a glob, treat both as patterns and check for overlap conservatively
    if _contains_glob(candidate_norm) or _contains_glob(existing_norm):
        return _patterns_overlap(existing_norm, candidate_norm)
    if PathSpec is not None:
        spec = _compile_pathspec(_normalize_pathspec_pattern(existing.path_pattern))
        if spec is not None:
            return spec.match_file(candidate_norm)
    # Fallback to conservative fnmatch if pathspec not available
    a = candidate_norm
    b = existing_norm
    return fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a) or (a == b)


def _normalize_pathspec_pattern(pattern: str) -> str:
    """Normalize a pattern for PathSpec caching (slash normalization + leading slash strip)."""
    if _is_virtual_namespace(pattern):
        return pattern  # Preserve virtual namespace scheme
    return pattern.replace("\\", "/").lstrip("/")


@functools.lru_cache(maxsize=1024)
def _compile_pathspec(pattern: str) -> "PathSpec | None":
    """Compile a PathSpec from a normalized pattern with LRU caching.

    Returns None if PathSpec is not available.
    """
    if PathSpec is None:
        return None
    return PathSpec.from_lines("gitignore", [pattern])


def _patterns_overlap(a: str, b: str) -> bool:
    # Overlap if any file could be matched by both patterns (approximate by cross-matching)
    a_norm = _normalize_pathspec_pattern(a)
    b_norm = _normalize_pathspec_pattern(b)

    a_spec = _compile_pathspec(a_norm)
    b_spec = _compile_pathspec(b_norm)

    if a_spec is not None and b_spec is not None:
        # Heuristic: check direct cross-matches on normalized patterns
        return a_spec.match_file(b_norm) or b_spec.match_file(a_norm)
    # Fallback approximate
    return fnmatch.fnmatchcase(a_norm, b_norm) or fnmatch.fnmatchcase(b_norm, a_norm) or (a_norm == b_norm)


def _file_reservations_patterns_overlap(paths_a: Sequence[str], paths_b: Sequence[str]) -> bool:
    for pa in paths_a:
        for pb in paths_b:
            if _patterns_overlap(pa, pb):
                return True
    return False


_ARCHIVE_PATH_PREFIXES: tuple[str, ...] = (
    "agents/",
    "messages/",
    "attachments/",
    "threads/",
    "file_reservations/",
)


def _looks_like_archive_path(pattern: str) -> bool:
    """Return True if a reservation pattern targets archive paths (agents/, messages/, attachments/...)."""
    normalized = (pattern or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    return normalized.startswith(_ARCHIVE_PATH_PREFIXES)


def _build_reservation_union_spec(
    existing_reservations: list[tuple["FileReservation", str]],
    exclude_execution_id: str | None,
    exclude_legacy_agent_id: int,
    candidate_exclusive: bool,
    compatible_execution_ids: set[str] | None = None,
) -> "PathSpec | None":
    """Build a union PathSpec matching ANY potentially conflicting reservation pattern.

    This enables O(n+m) conflict detection instead of O(n*m) by quickly identifying
    which candidate paths MIGHT conflict with existing reservations.

    Parameters
    ----------
    existing_reservations : list[tuple[FileReservation, str]]
        List of (reservation, holder_name) tuples to check against.
    exclude_execution_id : str | None
        Execution ID to exclude, or None for an observed legacy caller.
    candidate_exclusive : bool
        Whether the candidate reservation is exclusive.

    Returns
    -------
    PathSpec | None
        A union PathSpec matching any potentially conflicting pattern, or None if
        no patterns qualify or PathSpec is unavailable.

    Notes
    -----
    A reservation is potentially conflicting if:
    - It is not released (released_ts is None)
    - It belongs to a different execution
    - Either the existing or candidate reservation is exclusive
    """
    if PathSpec is None:
        return None

    patterns: list[str] = []
    compatible_ids = compatible_execution_ids or set()
    for record, _ in existing_reservations:
        # Skip released reservations
        if record.released_ts is not None:
            continue
        # Skip only this execution's own reservations. Sibling executions of
        # the same durable Agent must still observe one another's conflicts.
        if (
            exclude_execution_id is not None
            and record.execution_id in {exclude_execution_id, *compatible_ids}
        ):
            continue
        if (
            exclude_execution_id is None
            and record.execution_id is None
            and record.agent_id == exclude_legacy_agent_id
        ):
            continue
        # Skip non-exclusive if candidate is also non-exclusive
        if not record.exclusive and not candidate_exclusive:
            continue
        # Skip virtual namespace patterns (they use exact-match, not pathspec) (bd-14z)
        if _is_virtual_namespace(record.path_pattern):
            continue
        # Add normalized pattern
        patterns.append(_normalize_pathspec_pattern(record.path_pattern))

    if not patterns:
        return None

    # Build union PathSpec matching ANY of these patterns
    return PathSpec.from_lines("gitignore", patterns)


async def _list_inbox(
    project: Project,
    agent: Agent,
    limit: int,
    urgent_only: bool,
    include_bodies: bool,
    since_ts: Optional[str],
    topic: Optional[str] = None,
    unread_only: bool = False,
) -> list[dict[str, Any]]:
    if project.id is None or agent.id is None:
        raise ValueError("Project and agent must have ids before listing inbox.")
    # Defense in depth (issue #178): never pass an out-of-bounds limit to the
    # DB query, regardless of which caller (tool or resource) supplied it.
    limit = _validate_limit(limit)
    sender_alias = aliased(Agent)
    sender_project_alias = aliased(Project)
    await ensure_schema()
    async with get_session() as session:
        stmt = (
            select(
                Message,
                MessageRecipient.kind,
                sender_alias.name,
                sender_project_alias.id,
                sender_project_alias.human_key,
                sender_project_alias.slug,
            )
            .join(MessageRecipient, MessageRecipient.message_id == Message.id)
            .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
            .join(sender_project_alias, cast(Any, sender_alias.project_id == sender_project_alias.id))
            .where(
                cast(Any, Message.project_id) == project.id,
                MessageRecipient.agent_id == agent.id,
            )
            .order_by(desc(Message.created_ts))
            .limit(limit)
        )
        if urgent_only:
            stmt = stmt.where(cast(Any, Message.importance).in_(["high", "urgent"]))
        if since_ts:
            since_dt = _parse_iso(since_ts)
            if since_dt:
                stmt = stmt.where(Message.created_ts > _naive_utc(since_dt))
        if topic:
            stmt = stmt.where(cast(Any, func.lower(Message.topic)) == topic.lower())
        if unread_only:
            # Per-recipient read state: the existing JOIN already scopes
            # MessageRecipient to this agent, so a NULL read_ts is exactly
            # "this recipient has not been marked read." A bare fetch_inbox
            # call does NOT mark messages read; only mark_message_read /
            # acknowledge_message do. The supporting index
            # idx_message_recipients_agent_message keeps the JOIN cheap.
            stmt = stmt.where(cast(Any, MessageRecipient.read_ts).is_(None))
        result = await session.execute(stmt)
        rows = result.all()
    messages: list[dict[str, Any]] = []
    for message, recipient_kind, sender_name, sender_project_id, sender_project_human_key, sender_project_slug in rows:
        payload = _message_to_dict(message, include_body=include_bodies)
        _apply_sender_identity(
            payload,
            message_project_id=message.project_id,
            sender_name=sender_name,
            sender_project_id=sender_project_id,
            sender_project_human_key=sender_project_human_key,
            sender_project_slug=sender_project_slug,
        )
        payload["kind"] = recipient_kind
        messages.append(payload)
    return messages


async def _list_outbox(
    project: Project,
    agent: Agent,
    limit: int,
    include_bodies: bool,
    since_ts: Optional[str],
) -> list[dict[str, Any]]:
    """List messages sent by the agent (their outbox)."""
    if project.id is None or agent.id is None:
        raise ValueError("Project and agent must have ids before listing outbox.")
    # Defense in depth (issue #178): bound the limit before the DB query.
    limit = _validate_limit(limit)
    await ensure_schema()
    messages: list[dict[str, Any]] = []
    async with get_session() as session:
        stmt = (
            select(Message)
            .where(Message.project_id == project.id, Message.sender_id == agent.id)
            .order_by(desc(Message.created_ts))
            .limit(limit)
        )
        if since_ts:
            since_dt = _parse_iso(since_ts)
            if since_dt:
                stmt = stmt.where(Message.created_ts > _naive_utc(since_dt))
        result = await session.execute(stmt)
        message_rows = result.scalars().all()

        if not message_rows:
            return messages

        # Batch fetch all recipients for all messages in one query (N+1 elimination)
        message_ids = [msg.id for msg in message_rows if msg.id is not None]
        if not message_ids:
            message_ids = []
        recs_stmt = (
            select(MessageRecipient.message_id, MessageRecipient.kind, Agent.name)
            .join(Agent, MessageRecipient.agent_id == Agent.id)
            .where(cast(Any, MessageRecipient.message_id).in_(message_ids))
        )
        recs_result = await session.execute(recs_stmt)
        all_recipients = recs_result.all()

        # Group recipients by message_id
        recipients_by_msg: dict[int, dict[str, list[str]]] = {}
        for msg_id, kind, name in all_recipients:
            if msg_id not in recipients_by_msg:
                recipients_by_msg[msg_id] = {"to": [], "cc": [], "bcc": []}
            if kind in ("to", "cc", "bcc"):
                recipients_by_msg[msg_id][kind].append(name)

        # Build output
        for msg in message_rows:
            if msg.id is None:
                continue
            payload = _message_to_dict(msg, include_body=include_bodies)
            payload["from"] = agent.name
            rec_data = recipients_by_msg.get(msg.id, {"to": [], "cc": [], "bcc": []})
            payload["to"] = rec_data["to"]
            payload["cc"] = rec_data["cc"]
            payload["bcc"] = rec_data["bcc"]
            messages.append(payload)
    return messages


async def _commit_info_for_message(settings: Settings, project: Project, message: Message) -> dict[str, Any] | None:
    """Fetch commit metadata from the message's immutable delivery receipt."""
    if message.id is None or message.delivery_id is None:
        return None
    async with get_session() as session:
        delivery = await session.get(MessageDelivery, message.delivery_id)
    if (
        delivery is None
        or delivery.state != "published"
        or delivery.message_id != message.id
        or delivery.project_id != project.id
        or delivery.project_generation_snapshot != project.project_generation
        or delivery.archive_relative_path is None
        or delivery.archive_commit_sha is None
    ):
        return None

    archive = await ensure_archive(settings, project.slug)
    relpath = delivery.archive_relative_path
    commit_sha = delivery.archive_commit_sha

    def _lookup() -> dict[str, Any] | None:
        try:
            commit = archive.repo.commit(commit_sha)
        except Exception:
            return None
        data: dict[str, Any] = {
            "delivery_id": delivery.id,
            "hexsha": commit.hexsha[:12],
            "summary": commit.summary,
            "authored_ts": _iso(datetime.fromtimestamp(commit.authored_date, tz=timezone.utc)),
        }
        try:
            stats = commit.stats.files.get(relpath, None)
            if stats:
                data["insertions"] = int(stats.get("insertions", 0))
                data["deletions"] = int(stats.get("deletions", 0))
        except Exception:
            pass
        # Attach concise diff summary (hunks count + first N +/- lines)
        try:
            parent = commit.parents[0] if commit.parents else None
            hunks = 0
            excerpt: list[str] = []
            if parent is not None:
                diffs = parent.diff(commit, paths=[relpath], create_patch=True)
                for d in diffs:
                    try:
                        raw_diff = d.diff
                        patch = raw_diff.decode("utf-8", "ignore") if isinstance(raw_diff, bytes) else str(raw_diff or "")
                    except Exception:
                        patch = ""
                    for line in patch.splitlines():
                        if line.startswith("@@"):
                            hunks += 1
                        if line.startswith("+") or line.startswith("-"):
                            # skip file header lines like +++/---
                            if line.startswith("+++") or line.startswith("---"):
                                continue
                            excerpt.append(line[:200])
                            if len(excerpt) >= 12:
                                break
                    if len(excerpt) >= 12:
                        break
            data["diff_summary"] = {"hunks": hunks, "excerpt": excerpt}
        except Exception:
            pass
        return data

    return await asyncio.to_thread(_lookup)


def _summarize_messages(messages: Sequence[tuple[Message, str]]) -> dict[str, Any]:
    participants: set[str] = set()
    key_points: list[str] = []
    action_items: list[str] = []
    open_actions = 0
    done_actions = 0
    mentions: dict[str, int] = {}
    code_references: set[str] = set()
    keywords = ("TODO", "ACTION", "FIXME", "NEXT", "BLOCKED")

    def _record_mentions(text: str) -> None:
        # very lightweight @mention parser
        for token in text.split():
            if token.startswith("@") and len(token) > 1:
                name = token[1:].strip(".,:;()[]{}")
                if name:
                    mentions[name] = mentions.get(name, 0) + 1

    def _maybe_code_ref(text: str) -> None:
        # capture backtick-enclosed references that look like files/paths
        start = 0
        while True:
            i = text.find("`", start)
            if i == -1:
                break
            j = text.find("`", i + 1)
            if j == -1:
                break
            snippet = text[i + 1 : j].strip()
            if ("/" in snippet or ".py" in snippet or ".ts" in snippet or ".md" in snippet) and (1 <= len(snippet) <= 120):
                code_references.add(snippet)
            start = j + 1

    for message, sender_name in messages:
        participants.add(sender_name)
        for line in message.body_md.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            _record_mentions(stripped)
            _maybe_code_ref(stripped)
            # bullet points and ordered lists → key points
            if stripped.startswith(('-', '*', '+')) or stripped[:2] in {"1.", "2.", "3.", "4.", "5."}:
                # normalize checkbox bullets to plain text for key points
                normalized = stripped
                if normalized.startswith(('- [ ]', '- [x]', '- [X]')):
                    normalized = normalized.split(']', 1)[-1].strip()
                key_points.append(normalized.lstrip("-+* "))
            # checkbox TODOs
            if stripped.startswith(('- [ ]', '* [ ]', '+ [ ]')):
                open_actions += 1
                action_items.append(stripped)
                continue
            if stripped.startswith(('- [x]', '- [X]', '* [x]', '* [X]', '+ [x]', '+ [X]')):
                done_actions += 1
                action_items.append(stripped)
                continue
            # keyword-based action detection
            upper = stripped.upper()
            if any(token in upper for token in keywords):
                action_items.append(stripped)

    # Sort mentions by frequency desc
    sorted_mentions = sorted(mentions.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    summary: dict[str, Any] = {
        "participants": sorted(participants),
        "key_points": key_points[:10],
        "action_items": action_items[:10],
        "total_messages": len(messages),
        "open_actions": open_actions,
        "done_actions": done_actions,
        "mentions": [{"name": name, "count": count} for name, count in sorted_mentions],
    }
    if code_references:
        summary["code_references"] = sorted(code_references)[:10]
    return summary


async def _get_thread_external_participants(
    project: Project,
    viewer: Agent,
    thread_id: str,
) -> dict[tuple[int, str], tuple[Project, str]]:
    if project.id is None or viewer.id is None:
        return {}
    await ensure_schema()
    sender_alias = aliased(Agent)
    sender_project_alias = aliased(Project)
    try:
        seed_id = int(thread_id)
    except (TypeError, ValueError):
        seed_id = None
    criteria: list[Any] = [cast(Any, Message.thread_id) == thread_id]
    if seed_id is not None:
        criteria.append(cast(Any, Message.id) == seed_id)
    async with get_session() as session:
        stmt = (
            select(
                sender_alias.name,
                sender_project_alias.id,
                sender_project_alias.human_key,
                sender_project_alias.slug,
                sender_project_alias.project_generation,
            )
            .select_from(Message)
            .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
            .join(sender_project_alias, cast(Any, sender_alias.project_id == sender_project_alias.id))
            .where(
                cast(Any, Message.project_id) == project.id,
                or_(*criteria),
                _message_visible_to_agent_clause(viewer.id),
            )
            .limit(500)
        )
        rows = (await session.execute(stmt)).all()

    participants: dict[tuple[int, str], tuple[Project, str]] = {}
    for (
        sender_name,
        sender_project_id,
        sender_project_human_key,
        sender_project_slug,
        sender_project_generation,
    ) in rows:
        if not sender_name or sender_project_id is None or sender_project_id == project.id:
            continue
        participants[(int(sender_project_id), sender_name.lower())] = (
            Project(
                id=int(sender_project_id),
                human_key=sender_project_human_key or sender_project_slug or "",
                slug=sender_project_slug or "",
                project_generation=sender_project_generation,
            ),
            sender_name,
        )
    return participants


async def _compute_thread_summary(
    project: Project,
    thread_id: str,
    include_examples: bool,
    llm_mode: bool,
    llm_model: Optional[str],
    *,
    per_thread_limit: Optional[int] = None,
    viewer_agent: Agent | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    if project.id is None:
        raise ValueError("Project must have an id before summarizing threads.")
    await ensure_schema()
    sender_alias = aliased(Agent)
    sender_project_alias = aliased(Project)
    try:
        message_id = int(thread_id)
    except ValueError:
        message_id = None
    criteria: list[Any] = [cast(Any, Message.thread_id) == thread_id]
    if message_id is not None:
        criteria.append(cast(Any, Message.id) == message_id)
    async with get_session() as session:
        stmt = (
            select(Message, sender_alias.name, sender_project_alias.id, sender_project_alias.slug)
            .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
            .join(sender_project_alias, cast(Any, sender_alias.project_id == sender_project_alias.id))
            .where(cast(Any, Message.project_id) == project.id, or_(*criteria))
            .order_by(asc(cast(Any, Message.created_ts)))
        )
        if viewer_agent is not None:
            if viewer_agent.id is None:
                raise ValueError("Viewer agent must have an id before summarizing visible threads.")
            stmt = stmt.where(_message_visible_to_agent_clause(viewer_agent.id))
        if per_thread_limit:
            stmt = stmt.limit(per_thread_limit)
        result = await session.execute(stmt)
        raw_rows = result.all()
    rows = [
        (
            row[0],
            _sender_display_name(
                message_project_id=row[0].project_id,
                sender_name=row[1],
                sender_project_id=row[2],
                sender_project_slug=row[3],
            ),
        )
        for row in raw_rows
    ]
    summary = _summarize_messages(rows)
    heuristic_key_points = list(summary.get("key_points", []))

    if llm_mode and get_settings().llm.enabled:
        try:
            excerpts: list[str] = []
            for message, sender_name in rows[:15]:
                excerpts.append(f"- {sender_name}: {message.subject}\n{message.body_md[:800]}")
            if excerpts:
                system = (
                    "You are a senior engineer. Produce a concise JSON summary with keys: "
                    "participants[], key_points[], action_items[], mentions[{name,count}], code_references[], "
                    "total_messages, open_actions, done_actions. Derive from the given thread excerpts."
                )
                user = "\n\n".join(excerpts)
                llm_resp = await complete_system_user(system, user, model=llm_model)
                parsed = _parse_json_safely(llm_resp.content)
                if parsed:
                    for key in (
                        "participants",
                        "key_points",
                        "action_items",
                        "mentions",
                        "code_references",
                        "total_messages",
                        "open_actions",
                        "done_actions",
                    ):
                        value = parsed.get(key)
                        if value:
                            summary[key] = value
                    if heuristic_key_points and isinstance(summary.get("key_points"), list):
                        keywords = ("TODO", "ACTION", "FIXME", "NEXT", "BLOCKED")
                        extra = [
                            kp for kp in heuristic_key_points
                            if any(token in str(kp).upper() for token in keywords)
                        ]
                        if extra:
                            merged: list[str] = []
                            for item in summary["key_points"] + extra:
                                if item not in merged:
                                    merged.append(item)
                            summary["key_points"] = merged[:10]
        except Exception as e:
            logger.debug("thread_summary.llm_skipped", extra={"thread_id": thread_id, "error": str(e)})

    examples: list[dict[str, Any]] = []
    if include_examples:
        for message, sender_name in rows[:3]:
            examples.append(
                {
                    "id": message.id,
                    "subject": message.subject,
                    "from": sender_name,
                    "created_ts": _iso(message.created_ts),
                }
            )
    return summary, examples, len(rows)


async def _get_message(project: Project, message_id: int) -> Message:
    if project.id is None:
        raise ValueError("Project must have an id before reading messages.")
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Message).where(Message.project_id == project.id, Message.id == message_id)
        )
        message = result.scalars().first()
        if not message:
            raise NoResultFound(f"Message '{message_id}' not found for project '{project.human_key}'.")
        return message


async def _get_visible_message(project: Project, agent: Agent, message_id: int) -> Message:
    """Return a message only when it is visible to the authenticated agent."""
    if project.id is None or agent.id is None:
        raise ValueError("Project and agent must have ids before reading visible messages.")
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Message).where(
                cast(Any, Message.project_id == project.id),
                cast(Any, Message.id == message_id),
                _message_visible_to_agent_clause(agent.id),
            )
        )
        message = result.scalars().first()
        if not message:
            raise NoResultFound(
                f"Message '{message_id}' not found or not visible to agent '{agent.name}' "
                f"in project '{project.human_key}'."
            )
        return message


async def _get_agent_by_id(project: Project, agent_id: int) -> Agent:
    if project.id is None:
        raise ValueError("Project must have an id before querying agents.")
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(
                Agent.project_id == project.id,
                Agent.id == agent_id,
                Agent.provisioning_state == "active",
            )
        )
        agent = result.scalars().first()
        if not agent:
            raise NoResultFound(f"Agent id '{agent_id}' not found for project '{project.human_key}'.")
        return agent


async def _get_agent_any_project_by_id(agent_id: int) -> Agent:
    await ensure_schema()
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.provisioning_state == "active",
            )
        )
        agent = result.scalars().first()
        if not agent:
            raise NoResultFound(f"Agent id '{agent_id}' not found.")
        return agent


async def _update_recipient_timestamp(
    agent: Agent,
    message_id: int,
    field: str,
) -> Optional[datetime]:
    if agent.id is None:
        raise ValueError("Agent must have an id before updating message state.")
    now = datetime.now(timezone.utc)
    naive_now = _naive_utc(now)  # Use naive UTC for SQLite compatibility
    # Already `Any` — `getattr` on a non-literal name cannot be resolved to a
    # column type. The `cast(Any, ...)` used elsewhere in this file for
    # SQLAlchemy columns would be a no-op here, so it is omitted rather than
    # written for symmetry: a redundant cast reads as "this needed widening".
    field_col = getattr(MessageRecipient, field)
    async with get_session() as session:
        # Single atomic conditional update (issue #187): guard on the column
        # being NULL so concurrent mark-read/ack calls cannot both win the
        # race. RETURNING tells us whether *this* statement applied.
        stmt = (
            update(MessageRecipient)
            .where(
                MessageRecipient.message_id == message_id,
                MessageRecipient.agent_id == agent.id,
                field_col.is_(None),
            )
            .values({field: naive_now})
            .returning(field_col)
        )
        result = await session.execute(stmt)
        applied = result.first()
        await session.commit()
        if applied is not None:
            # We won the race and set the timestamp.
            return naive_now
        # No row updated: either the recipient row is absent, or the field was
        # already set by a prior (possibly concurrent) call. Re-read to tell
        # those apart and return the existing value idempotently.
        result_sel = await session.execute(
            select(field_col).where(
                cast(Any, MessageRecipient.message_id == message_id),
                cast(Any, MessageRecipient.agent_id == agent.id),
            )
        )
        existing = result_sel.first()
        if existing is None:
            return None
        return existing[0]


def _execution_children_by_parent(
    executions: Sequence[AgentExecution],
) -> defaultdict[str, list[AgentExecution]]:
    """Index an execution forest once for bounded descendant walks."""
    children: defaultdict[str, list[AgentExecution]] = defaultdict(list)
    for execution in executions:
        if execution.parent_execution_id:
            children[execution.parent_execution_id].append(execution)
    return children


def _execution_descendants_from_children(
    children: dict[str, list[AgentExecution]],
    root_id: str,
    *,
    active_only: bool,
) -> list[AgentExecution]:
    """Return descendants deepest-first without rebuilding the child map."""
    ordered: list[AgentExecution] = []
    visited: set[str] = {root_id}

    def visit(parent_id: str) -> None:
        for child in children.get(parent_id, []):
            if child.id in visited:
                continue
            visited.add(child.id)
            visit(child.id)
            if not active_only or child.status == "active":
                ordered.append(child)

    visit(root_id)
    return ordered


def _execution_descendants_all_child_first(
    executions: Sequence[AgentExecution],
    root_id: str,
) -> list[AgentExecution]:
    """Return every descendant deepest-first, tolerating corrupt cycles."""
    return _execution_descendants_from_children(
        _execution_children_by_parent(executions),
        root_id,
        active_only=False,
    )


def _execution_descendants_child_first(
    executions: Sequence[AgentExecution],
    root_id: str,
) -> list[AgentExecution]:
    """Return active descendants deepest-first, tolerating corrupt cycles."""
    return _execution_descendants_from_children(
        _execution_children_by_parent(executions),
        root_id,
        active_only=True,
    )


def _execution_subtree_latest_activity(
    execution_id: str,
    *,
    by_id: dict[str, AgentExecution],
    children: dict[str, list[AgentExecution]],
    memo: dict[str, datetime],
    visiting: set[str],
    cycle_sentinel: datetime,
) -> datetime:
    """Return the latest heartbeat in one active subtree, memoized per forest."""
    cached = memo.get(execution_id)
    if cached is not None:
        return cached
    execution = by_id[execution_id]
    if execution_id in visiting:
        # Corrupt cycles are fail-safe: keep the lifetime active instead of
        # looping or terminalizing an ambiguous tree.
        return cycle_sentinel
    visiting.add(execution_id)
    latest = _naive_utc(execution.last_active_ts)
    for child in children.get(execution_id, []):
        if child.status != "active":
            continue
        latest = max(
            latest,
            _execution_subtree_latest_activity(
                child.id,
                by_id=by_id,
                children=children,
                memo=memo,
                visiting=visiting,
                cycle_sentinel=cycle_sentinel,
            ),
        )
    visiting.remove(execution_id)
    memo[execution_id] = latest
    return latest


def _execution_ancestor_ids(
    executions: Sequence[AgentExecution],
    execution: AgentExecution,
) -> list[str]:
    """Return root-to-parent execution ids while tolerating corrupt cycles."""
    by_id = {item.id: item for item in executions}
    reversed_ids: list[str] = []
    seen: set[str] = {execution.id}
    parent_id = execution.parent_execution_id
    while parent_id is not None and parent_id not in seen:
        seen.add(parent_id)
        reversed_ids.append(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            break
        parent_id = parent.parent_execution_id
    reversed_ids.reverse()
    return reversed_ids


async def _load_execution_lineage_rows(
    session: Any,
    execution_ids: Sequence[str],
    *,
    project_id: int,
) -> list[AgentExecution]:
    """Load only requested executions and their recursive parent chains."""
    normalized_ids = sorted(set(execution_ids))
    if not normalized_ids:
        return []
    lineage = (
        select(
            AgentExecution.id,
            AgentExecution.parent_execution_id,
        )
        .where(
            cast(Any, AgentExecution.project_id) == project_id,
            cast(Any, AgentExecution.id).in_(normalized_ids),
        )
        .cte("execution_lineage", recursive=True)
    )
    parent = aliased(AgentExecution)
    lineage = lineage.union(
        select(parent.id, parent.parent_execution_id)
        .join(
            lineage,
            cast(Any, parent.id) == lineage.c.parent_execution_id,
        )
        .where(cast(Any, parent.project_id) == project_id)
    )
    return list(
        (
            await session.execute(
                select(AgentExecution).where(
                    cast(Any, AgentExecution.id).in_(select(lineage.c.id))
                )
            )
        ).scalars().all()
    )


async def _load_execution_descendant_rows(
    session: Any,
    root_ids: Sequence[str],
    *,
    project_id: int,
    active_only: bool,
) -> list[AgentExecution]:
    """Load a bounded root set and its recursive descendants in one query."""
    normalized_ids = sorted(set(root_ids))
    if not normalized_ids:
        return []
    descendants = (
        select(
            AgentExecution.id,
            AgentExecution.parent_execution_id,
        )
        .where(
            cast(Any, AgentExecution.project_id) == project_id,
            cast(Any, AgentExecution.id).in_(normalized_ids),
        )
        .cte("execution_descendants", recursive=True)
    )
    child = aliased(AgentExecution)
    recursive_term = (
        select(child.id, child.parent_execution_id)
        .join(
            descendants,
            cast(Any, child.parent_execution_id) == descendants.c.id,
        )
        .where(cast(Any, child.project_id) == project_id)
    )
    if active_only:
        recursive_term = recursive_term.where(
            cast(Any, child.status) == "active"
        )
    descendants = descendants.union(recursive_term)
    stmt = select(AgentExecution).where(
        cast(Any, AgentExecution.id).in_(select(descendants.c.id))
    )
    if active_only:
        stmt = stmt.where(cast(Any, AgentExecution.status) == "active")
    return list((await session.execute(stmt)).scalars().all())


async def _load_execution_ancestor_ids(execution: AgentExecution) -> list[str]:
    """Load lineage for one execution without exposing capability material."""
    async with get_session() as session:
        rows = await _load_execution_lineage_rows(
            session,
            [execution.id],
            project_id=execution.project_id,
        )
    return _execution_ancestor_ids(rows, execution)


async def _register_execution_build_slot_artifact_path(
    project: Project,
    execution: AgentExecution,
    *,
    slot_name: str,
    slot_path_component: str,
) -> None:
    """Register an exact lease path before its JSON artifact can be written."""
    if project.id is None:
        raise ValueError("Project must have an id before registering a build slot.")
    async with get_immediate_session() as session:
        current_execution = await session.get(AgentExecution, execution.id)
        if (
            current_execution is None
            or current_execution.project_id != project.id
            or current_execution.status != "active"
        ):
            raise ToolExecutionError(
                "EXECUTION_NOT_ACTIVE",
                "Cannot publish a build-slot artifact for a terminal execution.",
                recoverable=False,
                data={"execution_id": execution.id},
            )
        key = (execution.id, slot_path_component)
        existing = await session.get(BuildSlotArtifactPath, key)
        if existing is None:
            session.add(
                BuildSlotArtifactPath(
                    execution_id=execution.id,
                    project_id=project.id,
                    slot_name=slot_name,
                    slot_path_component=slot_path_component,
                )
            )
        elif (
            existing.project_id != project.id
            or existing.slot_name != slot_name
        ):
            raise ToolExecutionError(
                "BUILD_SLOT_ARTIFACT_PATH_MISMATCH",
                "The immutable build-slot artifact path has different metadata.",
                recoverable=False,
                data={
                    "execution_id": execution.id,
                    "slot": slot_name,
                },
            )
        await session.commit()


async def _release_build_slot_artifacts_for_executions(
    project: Project,
    execution_ids: set[str],
    released_at: datetime,
    *,
    archive: ProjectArchive | None = None,
    archive_locked: bool = False,
) -> int:
    """Soft-release build-slot leases owned by terminal execution scopes."""
    if not execution_ids:
        return 0
    resolved_archive = archive or await ensure_archive(get_settings(), project.slug)
    build_slots_root = resolved_archive.root / "build_slots"
    if project.id is None:
        raise ValueError("Project must have an id before reconciling build slots.")
    async with get_session() as session:
        artifact_paths = list(
            (
                await session.execute(
                    select(BuildSlotArtifactPath).where(
                        cast(Any, BuildSlotArtifactPath.project_id)
                        == project.id,
                        cast(Any, BuildSlotArtifactPath.execution_id).in_(
                            execution_ids
                        ),
                    )
                )
            ).scalars().all()
        )

    def update_files() -> int:
        if not artifact_paths or not build_slots_root.is_dir():
            return 0
        released = 0
        failures: list[str] = []
        released_iso = _iso(released_at)
        for artifact_path in artifact_paths:
            holder_file = (
                f"{safe_build_path_component(artifact_path.execution_id)}.json"
            )
            lease_path = (
                build_slots_root
                / artifact_path.slot_path_component
                / holder_file
            )
            if not lease_path.is_file():
                continue
            try:
                data = json.loads(lease_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("build-slot lease is not a JSON object")
                if data.get("execution_id") != artifact_path.execution_id:
                    raise ValueError(
                        "build-slot filename does not match execution_id"
                    )
                if data.get("slot") != artifact_path.slot_name:
                    raise ValueError("build-slot path does not match slot name")
                if data.get("released_ts"):
                    continue
                data["released_ts"] = released_iso
                data["expires_ts"] = released_iso
                _write_json_atomic_sync(lease_path, data)
                released += 1
            except (OSError, ValueError, TypeError) as exc:
                failures.append(f"{lease_path}: {exc}")
                logger.exception(
                    "build_slot.execution_release_failed",
                    extra={"lease_path": str(lease_path)},
                )
        if failures:
            raise RuntimeError(
                "Failed to reconcile build-slot artifact(s): "
                + "; ".join(failures[:5])
            )
        return released

    if archive_locked:
        return await asyncio.to_thread(update_files)
    async with _archive_write_lock(resolved_archive):
        return await asyncio.to_thread(update_files)


async def _ack_execution_build_slot_reconciliation(
    execution_ids: set[str],
    reconciled_at: datetime,
) -> int:
    """Acknowledge successful terminal build-slot projection in the DB outbox."""
    if not execution_ids:
        return 0
    async with get_immediate_session() as session:
        result = await session.execute(
            update(BuildSlotArtifactProjection)
            .where(
                cast(
                    Any,
                    BuildSlotArtifactProjection.execution_id,
                ).in_(execution_ids),
                cast(
                    Any,
                    BuildSlotArtifactProjection.reconciled_ts,
                ).is_(None),
            )
            .values(reconciled_ts=reconciled_at)
        )
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def _reconcile_terminal_execution_build_slots(
    *,
    project_id: int | None,
    released_at: datetime,
) -> tuple[int, list[str]]:
    """Project one bounded batch of the terminal-execution DB outbox."""
    async with get_session() as session:
        stmt = select(
            BuildSlotArtifactProjection.project_id,
            BuildSlotArtifactProjection.execution_id,
        ).where(
            cast(
                Any,
                BuildSlotArtifactProjection.reconciled_ts,
            ).is_(None),
        )
        if project_id is not None:
            stmt = stmt.where(
                cast(Any, BuildSlotArtifactProjection.project_id) == project_id
            )
        stmt = stmt.order_by(
            asc(cast(Any, BuildSlotArtifactProjection.project_id)),
            asc(cast(Any, BuildSlotArtifactProjection.execution_id)),
        ).limit(_BUILD_SLOT_RECONCILIATION_BATCH_SIZE)
        terminal_rows = list((await session.execute(stmt)).all())
        terminal_by_project: defaultdict[int, set[str]] = defaultdict(set)
        for terminal_project_id, execution_id in terminal_rows:
            terminal_by_project[int(terminal_project_id)].add(str(execution_id))
        projects = {
            int(project.id): project
            for project in (
                await session.execute(
                    select(Project).where(
                        cast(Any, Project.id).in_(terminal_by_project)
                    )
                )
            ).scalars().all()
            if project.id is not None
        }

    released = 0
    warnings: list[str] = []
    for terminal_project_id, execution_ids in terminal_by_project.items():
        project = projects.get(terminal_project_id)
        if project is None:
            continue
        try:
            released += await _release_build_slot_artifacts_for_executions(
                project,
                execution_ids,
                released_at,
            )
            await _ack_execution_build_slot_reconciliation(
                execution_ids,
                released_at,
            )
        except Exception as exc:
            warnings.append(f"project {terminal_project_id} build slots: {exc}")
            logger.exception(
                "execution_reaper.build_slot_reconcile_failed",
                extra={"project_id": terminal_project_id},
            )
    return released, warnings


async def expire_stale_agent_executions(
    threshold_seconds: int,
    *,
    project_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Expire stale execution trees and release only their reservations."""
    if threshold_seconds < 1:
        raise ValueError("threshold_seconds must be positive.")
    effective_now = _naive_utc(now or datetime.now(timezone.utc))
    cutoff = effective_now - timedelta(seconds=threshold_seconds)
    expired_ids: list[str] = []
    expired_by_project: defaultdict[int, set[str]] = defaultdict(set)
    released_count = 0
    await ensure_schema()
    async with get_immediate_session() as session:
        closed: set[str] = set()
        cursor: tuple[datetime, int, str] | None = None
        while True:
            stmt = select(AgentExecution).where(
                cast(Any, AgentExecution.status) == "active",
                cast(Any, AgentExecution.last_active_ts) <= cutoff,
            )
            if project_id is not None:
                stmt = stmt.where(
                    cast(Any, AgentExecution.project_id) == project_id
                )
            if cursor is not None:
                cursor_ts, cursor_project_id, cursor_id = cursor
                stmt = stmt.where(
                    or_(
                        cast(Any, AgentExecution.last_active_ts) > cursor_ts,
                        and_(
                            cast(Any, AgentExecution.last_active_ts)
                            == cursor_ts,
                            cast(Any, AgentExecution.project_id)
                            > cursor_project_id,
                        ),
                        and_(
                            cast(Any, AgentExecution.last_active_ts)
                            == cursor_ts,
                            cast(Any, AgentExecution.project_id)
                            == cursor_project_id,
                            cast(Any, AgentExecution.id) > cursor_id,
                        ),
                    )
                )
            stmt = stmt.order_by(
                asc(cast(Any, AgentExecution.last_active_ts)),
                asc(cast(Any, AgentExecution.project_id)),
                asc(cast(Any, AgentExecution.id)),
            ).limit(_EXECUTION_REAPER_BATCH_SIZE)
            candidates = list((await session.execute(stmt)).scalars().all())
            if not candidates:
                break
            last_candidate = candidates[-1]
            cursor = (
                _naive_utc(last_candidate.last_active_ts),
                last_candidate.project_id,
                last_candidate.id,
            )
            candidates_by_project: defaultdict[int, list[AgentExecution]] = (
                defaultdict(list)
            )
            for candidate in candidates:
                candidates_by_project[candidate.project_id].append(candidate)

            for candidate_project_id, project_candidates in (
                candidates_by_project.items()
            ):
                rows = await _load_execution_descendant_rows(
                    session,
                    [candidate.id for candidate in project_candidates],
                    project_id=candidate_project_id,
                    active_only=True,
                )
                by_id = {execution.id: execution for execution in rows}
                children = _execution_children_by_parent(rows)
                latest_activity: dict[str, datetime] = {}
                visiting: set[str] = set()

                for root in project_candidates:
                    if (
                        root.id in closed
                        or root.status != "active"
                        or _execution_subtree_latest_activity(
                            root.id,
                            by_id=by_id,
                            children=children,
                            memo=latest_activity,
                            visiting=visiting,
                            cycle_sentinel=effective_now,
                        )
                        > cutoff
                    ):
                        continue
                    tree = [
                        *_execution_descendants_from_children(
                            children,
                            root.id,
                            active_only=True,
                        ),
                        root,
                    ]
                    tree_ids = [
                        execution.id
                        for execution in tree
                        if execution.id not in closed
                        and execution.status == "active"
                    ]
                    if tree_ids:
                        released = await session.execute(
                            update(FileReservation)
                            .where(
                                cast(Any, FileReservation.execution_id).in_(
                                    tree_ids
                                ),
                                cast(Any, FileReservation.origin) == "auto",
                                cast(Any, FileReservation.released_ts).is_(None),
                            )
                            .values(released_ts=effective_now)
                        )
                        released_count += int(
                            getattr(released, "rowcount", 0) or 0
                        )
                        await session.flush()
                    for execution in tree:
                        if (
                            execution.id in closed
                            or execution.status != "active"
                        ):
                            continue
                        execution.status = "expired"
                        execution.last_active_ts = effective_now
                        execution.ended_ts = effective_now
                        session.add(execution)
                        # The storage trigger enforces child-first
                        # terminalization.
                        await session.flush()
                        closed.add(execution.id)
                        expired_ids.append(execution.id)
                        expired_by_project[execution.project_id].add(
                            execution.id
                        )
        await session.commit()
    released_build_slots = 0
    archive_warnings: list[str] = []
    # A sweep is also the durable retry boundary for any earlier reservation
    # publication failure, even when there is no newly stale execution now.
    async with get_session() as session:
        pending_projects_stmt = select(FileReservation.project_id).where(
            cast(Any, FileReservation.archive_synced_revision)
            < cast(Any, FileReservation.archive_revision)
        )
        if project_id is not None:
            pending_projects_stmt = pending_projects_stmt.where(
                cast(Any, FileReservation.project_id) == project_id
            )
        pending_project_ids = {
            int(value)
            for value in (
                await session.execute(
                    pending_projects_stmt.distinct()
                    .order_by(asc(cast(Any, FileReservation.project_id)))
                    .limit(_FILE_RESERVATION_ARCHIVE_BATCH_SIZE)
                )
            ).scalars().all()
        }
    projects_to_reconcile = pending_project_ids | set(expired_by_project)
    for expired_project_id in sorted(projects_to_reconcile):
        async with get_session() as session:
            project = await session.get(Project, expired_project_id)
        if project is None:
            continue
        try:
            await _reconcile_pending_file_reservation_artifacts(project)
        except Exception as exc:
            archive_warnings.append(
                f"project {expired_project_id} reservations: {exc}"
            )
            logger.exception(
                "execution_reaper.reservation_archive_failed",
                extra={"project_id": expired_project_id},
            )
    # Terminal execution projections are the durable build-slot outbox. Each
    # pass addresses one fixed batch through immutable DB path registrations
    # and marks only a successfully reconciled lifetime, so history size
    # cannot inflate a query or force an archive-wide JSON scan.
    reconciled_build_slots, build_slot_warnings = (
        await _reconcile_terminal_execution_build_slots(
            project_id=project_id,
            released_at=effective_now,
        )
    )
    released_build_slots += reconciled_build_slots
    archive_warnings.extend(build_slot_warnings)
    return {
        "expired": len(expired_ids),
        "execution_ids": expired_ids,
        "released_reservations": released_count,
        "released_build_slots": released_build_slots,
        "expired_at": _iso(effective_now),
        "archive_warnings": archive_warnings,
    }


async def _agent_execution_reaper_worker(settings: Settings) -> None:
    """Continuously expire crashed execution trees for every transport.

    The worker belongs to the FastMCP lifespan rather than the HTTP wrapper so
    stdio, in-memory, and HTTP deployments all receive the same crash cleanup.
    It is deliberately independent from observe/enforce rollout mode: rollout
    changes whether an unscoped claim may be created, not whether a known
    execution lifetime is eventually closed.
    """
    interval_seconds = max(1, settings.agent_execution_reaper_interval_seconds)
    threshold_seconds = max(1, settings.agent_execution_reaper_threshold_seconds)
    while True:
        try:
            report = await expire_stale_agent_executions(threshold_seconds)
            if report["expired"] or report["archive_warnings"]:
                logger.info(
                    "agent_execution.reaper",
                    extra={
                        "expired": report["expired"],
                        "released_reservations": report["released_reservations"],
                        "released_build_slots": report["released_build_slots"],
                        "archive_warnings": report["archive_warnings"],
                        "threshold_seconds": threshold_seconds,
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "agent_execution.reaper_failed",
                extra={"threshold_seconds": threshold_seconds},
            )
        await asyncio.sleep(interval_seconds)


_CREDENTIAL_ARGUMENT_PATTERN = re.compile(
    r"token|secret|credential|password|bearer", re.IGNORECASE
)


def _redacted_validation_message(
    tool_name: str,
    error: ValidationError,
    arguments: Mapping[str, Any],
) -> str:
    """Render a validation failure without echoing anything credential-shaped.

    Pydantic reports the offending value, and for an unexpected keyword that
    value IS the argument. So a typo in a token's own field name -- writing
    `registration_tokens` instead of `registration_token` -- prints the token
    in full and untruncated. Measured on 2026-08-14; it burned a live token,
    and it hit three agents in one day because the argument names are long
    and similar.

    Field name, message and error type are kept: they are what makes the
    error actionable, and none of them carry the value. An input is echoed
    only when neither its own name nor any credential-named argument's value
    could be hiding in it.
    """
    secrets_in_call: set[str] = set()

    def _collect_credential_values(
        value: Any,
        *,
        credential_context: bool = False,
    ) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                _collect_credential_values(
                    item,
                    credential_context=(
                        credential_context
                        or bool(
                            _CREDENTIAL_ARGUMENT_PATTERN.search(str(key))
                        )
                    ),
                )
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                _collect_credential_values(
                    item,
                    credential_context=credential_context,
                )
            return
        if credential_context and isinstance(value, str) and value:
            secrets_in_call.add(value)

    _collect_credential_values(arguments)

    def _safe_input(location: tuple[Any, ...], value: Any) -> str:
        if any(_CREDENTIAL_ARGUMENT_PATTERN.search(str(part)) for part in location):
            return "<redacted>"
        rendered = repr(_redact_tool_log_value(value))
        if any(secret in rendered for secret in secrets_in_call):
            return "<redacted>"
        return rendered if len(rendered) <= 120 else f"{rendered[:117]}..."

    def _safe_message(location: tuple[Any, ...], value: Any) -> str:
        if any(
            _CREDENTIAL_ARGUMENT_PATTERN.search(str(part))
            for part in location
        ):
            return "Invalid credential value"
        rendered = str(value or "invalid")
        for secret in sorted(secrets_in_call, key=len, reverse=True):
            rendered = rendered.replace(secret, "<redacted>")
        return rendered

    def _safe_location_part(value: Any) -> str:
        rendered = str(value)
        for secret in sorted(secrets_in_call, key=len, reverse=True):
            rendered = rendered.replace(secret, "<redacted>")
        return rendered if len(rendered) <= 120 else f"{rendered[:117]}..."

    # Pydantic's own layout is kept -- location on its own line, message
    # indented beneath it -- because callers and tests read these errors and
    # only the VALUE needed to change here.
    lines = [f"{len(error.errors())} validation error(s) for {tool_name}"]
    for entry in error.errors():
        location = tuple(entry.get("loc", ()))
        lines.append(
            ".".join(_safe_location_part(part) for part in location)
            or "<call>"
        )
        lines.append(
            f"  {_safe_message(location, entry.get('msg'))} "
            f"[type={entry.get('type', 'unknown')}, input={_safe_input(location, entry.get('input'))}]"
        )
    return "\n".join(lines)


class _CredentialSafeValidationErrors(Middleware):
    """Keep argument validation failures from quoting credentials back."""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        try:
            return await call_next(context)
        except _FastMCPValidationError as exc:
            arguments = getattr(context.message, "arguments", None) or {}
            validation_cause = exc.__cause__
            if not isinstance(validation_cause, ValidationError):
                raise _FastMCPToolError("Invalid tool arguments.") from None
            raise _FastMCPToolError(
                _redacted_validation_message(
                    getattr(context.message, "name", "<tool>"),
                    validation_cause,
                    arguments,
                )
            ) from None
        except ValidationError as exc:
            arguments = getattr(context.message, "arguments", None) or {}
            raise _FastMCPToolError(
                _redacted_validation_message(
                    getattr(context.message, "name", "<tool>"),
                    exc,
                    arguments,
                )
            ) from None


def build_mcp_server() -> FastMCP:
    """Create and configure the FastMCP server instance."""
    _install_fastmcp_sensitive_log_filter()
    settings: Settings = get_settings()
    lifespan = _lifespan_factory(settings)

    instructions = (
        "You are the MCP Agent Mail coordination server. "
        "Provide message routing, coordination tooling, and project context to cooperating agents. "
        "Outputs are JSON by default; pass format='toon' (or set MCP_AGENT_MAIL_OUTPUT_FORMAT=toon) to receive "
        "{format:'toon', data:'<TOON>'}."
    )

    mcp = FastMCP(
        name="mcp-agent-mail",
        version=package_version(),
        instructions=instructions,
        lifespan=lifespan,
    )
    mcp.add_middleware(_CredentialSafeValidationErrors())
    file_reservation_paths_direct: (
        Callable[..., Awaitable[dict[str, Any]]] | None
    ) = None

    # Session bindings are keyed by `ctx.session_id` (the FastMCP-assigned
    # ID derived from the `mcp-session-id` header for HTTP transport, or a
    # generated UUID stored on the in-memory session for stdio). Earlier
    # revisions keyed by `ctx.session` directly via `WeakKeyDictionary`,
    # which works for stdio (one persistent session object per process)
    # but breaks under `serve-http`: each request gets a fresh session
    # object, the WeakKeyDictionary entry is GC'd as soon as the request
    # returns, and the next call sees no bindings — so adjacent-agent
    # auth always fell back to the AUTHENTICATION_REQUIRED error
    # described in issue #148.
    #
    # Switch to `dict[str, ...]` keyed by the stable session ID, with a
    # last-access timestamp and an expiry sweep on each lookup so an
    # unbounded HTTP server can't accumulate session bindings forever.
    session_binding_ttl_seconds: float = max(
        60.0, float(getattr(settings, "session_binding_ttl_seconds", 24 * 3600))
    )
    # Numeric SQLite ids are recyclable after hard deletion. Every in-memory
    # binding therefore carries the immutable project/Agent row generations.
    # It also carries a one-way fingerprint of the current registration token:
    # a rotation in another worker changes the DB-derived key immediately, so
    # worker-local session state cannot keep the retired credential authorized.
    session_agent_bindings: dict[str, set[_SessionAgentBinding]] = {}
    session_current_agents: dict[str, dict[int, _SessionAgentBinding]] = {}
    session_current_executions: dict[
        str, dict[int, _SessionExecutionBinding]
    ] = {}
    session_binding_last_access: dict[str, float] = {}

    async def _ctx_info_safe(ctx: Context, message: str) -> None:
        try:
            await ctx.info(message)
        except Exception:
            # Context may not be available outside of a request; ignore logging
            return

    def _session_agent_binding(
        project: Project,
        agent: Agent,
    ) -> _SessionAgentBinding:
        if project.id is None or agent.id is None:
            raise ValueError("Project and agent must have ids before binding MCP sessions.")
        return _SessionAgentBinding(
            project_id=project.id,
            project_generation=project.project_generation,
            agent_id=agent.id,
            agent_generation=agent.agent_generation,
            registration_token_fingerprint=_registration_token_fingerprint(
                agent.registration_token
            ),
        )

    def _session_binding_key(ctx: Context) -> str:
        # `ctx.session_id` is a stable identifier across requests for both
        # HTTP (mcp-session-id header) and stdio (uuid stored on the
        # in-memory session). Falls through to a generated UUID cached on
        # the ctx itself if the transport doesn't surface one — caching
        # is required because `_bind_session_agent` calls into both
        # `_session_bindings_for` and `_session_current_agents_for`, and
        # without a stable per-ctx key those two helpers would write to
        # different orphan UUIDs and the binding would be invisible to
        # the next `_session_is_bound_to_agent` lookup.
        try:
            session_id = ctx.session_id
        except Exception:
            session_id = ""
        if session_id:
            return session_id
        cached = getattr(ctx, "_mcp_agent_mail_orphan_key", None)
        if isinstance(cached, str):
            return cached
        orphan_key = f"orphan:{uuid.uuid4()}"
        # ctx refuses attribute assignment; multi-lookup consistency
        # degrades to "best effort" but a single lookup still works.
        #
        # `setattr`, not a dotted assignment with a suppression comment: this is
        # a deliberate monkey-patch onto a third-party Context that declares no
        # such attribute, so there is no annotation that makes the dotted form
        # correct. The previous `# type: ignore[attr-defined]` was in mypy's
        # dialect, which the type gate does not read — it looked like a
        # considered suppression and silenced nothing. `getattr` is already
        # used to read it back four lines above; this just matches.
        #
        # The B010 suppression below is deliberate: that rule's premise is
        # "no safer than normal property access", and here the premise is false
        # — the dotted form is exactly what the type gate rejects, because
        # Context declares no such attribute. The two gates disagree about this
        # line and this is the only form that satisfies both.
        #
        # (Written without the directive word at the start of a line: ruff reads
        # a comment opening with that token as a real blanket directive, so an
        # explanation of a suppression becomes a second, wider suppression.)
        with suppress(Exception):
            setattr(ctx, "_mcp_agent_mail_orphan_key", orphan_key)  # noqa: B010
        return orphan_key

    def _prune_expired_session_bindings(now: float) -> None:
        if not session_binding_last_access:
            return
        expired = [
            key
            for key, last in session_binding_last_access.items()
            if now - last > session_binding_ttl_seconds
        ]
        for key in expired:
            session_binding_last_access.pop(key, None)
            session_agent_bindings.pop(key, None)
            session_current_agents.pop(key, None)
            session_current_executions.pop(key, None)

    def _touch_session_binding(key: str) -> None:
        now = time.monotonic()
        _prune_expired_session_bindings(now)
        session_binding_last_access[key] = now

    def _session_bindings_for(
        ctx: Context,
    ) -> set[_SessionAgentBinding]:
        key = _session_binding_key(ctx)
        _touch_session_binding(key)
        bindings = session_agent_bindings.get(key)
        if bindings is None:
            bindings = set()
            session_agent_bindings[key] = bindings
        return bindings

    def _session_current_agents_for(
        ctx: Context,
    ) -> dict[int, _SessionAgentBinding]:
        key = _session_binding_key(ctx)
        _touch_session_binding(key)
        current_agents = session_current_agents.get(key)
        if current_agents is None:
            current_agents = {}
            session_current_agents[key] = current_agents
        return current_agents

    def _session_current_executions_for(
        ctx: Context,
    ) -> dict[int, _SessionExecutionBinding]:
        key = _session_binding_key(ctx)
        _touch_session_binding(key)
        current_executions = session_current_executions.get(key)
        if current_executions is None:
            current_executions = {}
            session_current_executions[key] = current_executions
        return current_executions

    def _bind_session_agent(ctx: Context, project: Project, agent: Agent) -> None:
        binding = _session_agent_binding(project, agent)
        bindings = _session_bindings_for(ctx)
        current_agents = _session_current_agents_for(ctx)
        # Keep at most one credential version for an exact Agent lifetime in a
        # session. This bounds stale entries after reauthentication following a
        # rotation and makes the set itself describe current authority only.
        bindings.difference_update(
            {
                existing
                for existing in bindings
                if existing.project_id == binding.project_id
                and existing.project_generation == binding.project_generation
                and existing.agent_id == binding.agent_id
                and existing.agent_generation == binding.agent_generation
            }
        )
        bindings.add(binding)
        current_agents[binding.project_id] = binding

    def _bind_session_execution(
        ctx: Context,
        project: Project,
        agent: Agent,
        execution: AgentExecution,
    ) -> None:
        binding = _session_agent_binding(project, agent)
        if (
            execution.project_id != binding.project_id
            or execution.agent_id != binding.agent_id
        ):
            raise ValueError("Agent execution does not belong to the authenticated project and agent.")
        _bind_session_agent(ctx, project, agent)
        # One MCP session may host a root execution and explicit subagent
        # lifetimes. Only the root is eligible for implicit resolution; a
        # child must always pass execution_id and must not steal the root slot.
        if execution.kind == "session":
            _session_current_executions_for(ctx)[binding.project_id] = (
                _SessionExecutionBinding(
                    project_generation=binding.project_generation,
                    agent_id=binding.agent_id,
                    agent_generation=binding.agent_generation,
                    registration_token_fingerprint=(
                        binding.registration_token_fingerprint
                    ),
                    execution_id=execution.id,
                )
            )

    def _session_execution_id(
        ctx: Context,
        project: Project,
        agent: Agent,
    ) -> str | None:
        if project.id is None:
            return None
        current = _session_current_executions_for(ctx)
        binding = current.get(project.id)
        if binding is None:
            return None
        expected = _session_agent_binding(project, agent)
        if (
            binding.project_generation != expected.project_generation
            or binding.agent_id != expected.agent_id
            or binding.agent_generation != expected.agent_generation
            or binding.registration_token_fingerprint
            != expected.registration_token_fingerprint
        ):
            current.pop(project.id, None)
            return None
        return binding.execution_id

    def _clear_session_execution(
        ctx: Context,
        project: Project,
        execution_id: str,
    ) -> None:
        if project.id is None:
            return
        current = _session_current_executions_for(ctx)
        binding = current.get(project.id)
        if binding is not None and binding.execution_id == execution_id:
            current.pop(project.id, None)

    def _clear_execution_bindings(execution_ids: set[str]) -> None:
        if not execution_ids:
            return
        for current in session_current_executions.values():
            for project_id, binding in list(current.items()):
                execution_id = binding.execution_id
                if execution_id in execution_ids:
                    current.pop(project_id, None)

    def _invalidate_session_bindings(
        project: Project,
        agent: Agent | None = None,
    ) -> None:
        """Drop bindings for one exact project or Agent lifetime in this worker."""
        if project.id is None:
            return
        exact_agent_key: tuple[int, str] | None = None
        if agent is not None:
            if agent.id is None:
                return
            exact_agent_key = (agent.id, agent.agent_generation)

        for session_key, bindings in session_agent_bindings.items():
            removed_bindings = {
                binding
                for binding in bindings
                if binding.project_id == project.id
                and binding.project_generation == project.project_generation
                and (
                    exact_agent_key is None
                    or (binding.agent_id, binding.agent_generation)
                    == exact_agent_key
                )
            }
            bindings.difference_update(removed_bindings)
            current_agents = session_current_agents.get(session_key)
            if current_agents is not None:
                current_agent = current_agents.get(project.id)
                if (
                    current_agent is not None
                    and current_agent.project_generation
                    == project.project_generation
                    and (
                        exact_agent_key is None
                        or (
                            current_agent.agent_id,
                            current_agent.agent_generation,
                        )
                        == exact_agent_key
                    )
                ):
                    current_agents.pop(project.id, None)
            current_executions = session_current_executions.get(session_key)
            if current_executions is not None:
                current_execution = current_executions.get(project.id)
                if (
                    current_execution is not None
                    and current_execution.project_generation
                    == project.project_generation
                    and (
                        exact_agent_key is None
                        or (
                            current_execution.agent_id,
                            current_execution.agent_generation,
                        )
                        == exact_agent_key
                    )
                ):
                    current_executions.pop(project.id, None)

    def _session_is_bound_to_agent(ctx: Context, project: Project, agent: Agent) -> bool:
        if project.id is None or agent.id is None:
            return False
        expected = _session_agent_binding(project, agent)
        bindings = _session_bindings_for(ctx)
        bindings.difference_update(
            {
                binding
                for binding in bindings
                if binding.project_id == expected.project_id
                and binding.project_generation == expected.project_generation
                and binding.agent_id == expected.agent_id
                and binding.agent_generation == expected.agent_generation
                and binding.registration_token_fingerprint
                != expected.registration_token_fingerprint
            }
        )
        return expected in bindings

    async def _resolve_session_agent_for_project(
        ctx: Context,
        project: Project,
    ) -> Agent | None:
        if project.id is None:
            return None
        current_agents = _session_current_agents_for(ctx)
        current_agent = current_agents.get(project.id)
        if current_agent is not None:
            if current_agent.project_generation == project.project_generation:
                try:
                    resolved = await _get_agent_by_id(project, current_agent.agent_id)
                except NoResultFound:
                    resolved = None
                if (
                    resolved is not None
                    and _session_agent_binding(project, resolved) == current_agent
                ):
                    return resolved
            current_agents.pop(project.id, None)
            _session_current_executions_for(ctx).pop(project.id, None)

        bindings = _session_bindings_for(ctx)
        resolved_agents: list[Agent] = []
        for binding in list(bindings):
            if binding.project_id != project.id:
                continue
            if binding.project_generation != project.project_generation:
                bindings.discard(binding)
                continue
            try:
                resolved = await _get_agent_by_id(project, binding.agent_id)
            except NoResultFound:
                bindings.discard(binding)
                continue
            if _session_agent_binding(project, resolved) != binding:
                bindings.discard(binding)
                continue
            resolved_agents.append(resolved)
        if len(resolved_agents) == 1:
            return resolved_agents[0]
        return None

    async def _resolve_agent_execution(
        ctx: Context,
        project: Project,
        agent: Agent,
        execution_id: str | None,
        execution_token: str | None,
        *,
        action: str,
        required: bool = True,
        require_active: bool = True,
        allow_authenticated_owner_recovery: bool = False,
        require_active_capability: bool = False,
        touch_activity: bool = True,
    ) -> AgentExecution | None:
        """Resolve an explicit or session-bound execution and enforce ownership."""
        if project.id is None or agent.id is None:
            raise ValueError("Project and agent must have ids before resolving an execution.")
        resolved_id = execution_id.strip() if execution_id else None
        explicit_resolution = resolved_id is not None
        if resolved_id is None:
            resolved_id = _session_execution_id(ctx, project, agent)
        if resolved_id is None:
            if not required:
                return None
            raise ToolExecutionError(
                "EXECUTION_REQUIRED",
                (
                    f"{action} requires execution_id or an active AgentExecution bound "
                    "to this MCP session. Call start_agent_execution first."
                ),
                recoverable=True,
                data={
                    "project_key": project.human_key,
                    "agent_name": agent.name,
                    "action": action,
                },
            )
        async with get_session() as session:
            execution = await session.get(AgentExecution, resolved_id)
        if execution is None:
            raise ToolExecutionError(
                "EXECUTION_NOT_FOUND",
                f"Agent execution '{resolved_id}' was not found.",
                recoverable=True,
                data={"execution_id": resolved_id, "action": action},
            )
        if execution.project_id != project.id or execution.agent_id != agent.id:
            raise ToolExecutionError(
                "EXECUTION_OWNERSHIP_MISMATCH",
                (
                    f"Agent execution '{resolved_id}' does not belong to agent "
                    f"'{agent.name}' in project '{project.human_key}'."
                ),
                recoverable=False,
                data={"execution_id": resolved_id, "action": action},
            )
        is_exact_session_binding = (
            _session_execution_id(ctx, project, agent) == execution.id
        )
        token_matches = bool(
            execution_token
            and hmac.compare_digest(
                hashlib.sha256(execution_token.encode("utf-8")).hexdigest(),
                execution.execution_token_hash,
            )
        )
        owner_recovery_allowed = (
            allow_authenticated_owner_recovery and execution.status != "active"
        )
        if (
            execution.status == "active"
            and require_active_capability
            and not token_matches
        ) or (
            explicit_resolution
            and not is_exact_session_binding
            and not owner_recovery_allowed
            and not token_matches
        ):
            raise ToolExecutionError(
                "EXECUTION_CAPABILITY_MISMATCH",
                f"Invalid execution_token for execution '{execution.id}'.",
                recoverable=False,
                data={"execution_id": execution.id, "action": action},
            )
        if require_active and execution.status != "active":
            _clear_session_execution(ctx, project, execution.id)
            raise ToolExecutionError(
                "EXECUTION_NOT_ACTIVE",
                f"Agent execution '{resolved_id}' is '{execution.status}', not active.",
                recoverable=True,
                data={
                    "execution_id": resolved_id,
                    "status": execution.status,
                    "action": action,
                },
            )
        if execution.status == "active" and touch_activity:
            touched_at = _naive_utc()
            async with get_session() as session:
                await session.execute(
                    update(AgentExecution)
                    .where(
                        cast(Any, AgentExecution.id) == execution.id,
                        cast(Any, AgentExecution.status) == "active",
                    )
                    .values(last_active_ts=touched_at)
                )
                await session.commit()
            execution.last_active_ts = touched_at
            _bind_session_execution(ctx, project, agent, execution)
        return execution

    # Authenticating IS activity, and until now it did not count as any.
    #
    # last_active_ts was refreshed when an agent registered or sent a message,
    # but not when it authenticated to file, renew or read anything — so a
    # session that spends half an hour reserving files and never speaks looks,
    # to the reservation sweeper, exactly like one that went away. The sweeper
    # is forgiving (it also weighs recent mail, filesystem and git activity, and
    # only acts when none of them is present), so this was a narrow hole rather
    # than a daily one. It is still a field whose name promised something it did
    # not deliver.
    #
    # Throttled, because the alternative is a write on every hook invocation and
    # the hooks fire twice per edit. A minute of granularity is far below the
    # 1800s the sweeper compares against.
    _ACTIVITY_TOUCH_SECONDS = 60

    async def _touch_agent_activity(agent: Agent) -> None:
        if agent.id is None:
            return
        # Never let bookkeeping fail a call that already succeeded. The guard
        # covers the comparison too, and that is not belt-and-braces: the
        # comparison is the part that actually raised. `last_active_ts` is
        # declared naive, but a row written from an offset-bearing ISO string
        # (`datetime.now(timezone.utc).isoformat()`) is handed back *aware*, and
        # subtracting the two flavours is a TypeError. It surfaced as a failed
        # `send_message` — because this runs inside `_authenticate_agent`, so a
        # bookkeeping error was being reported as a rejected tool call.
        #
        # It logs rather than passing, and the difference is not cosmetic. Widening
        # the guard over the comparison also made two tests in test_server.py go
        # green *without* the fix below, because a swallowed TypeError looks
        # exactly like a throttled call: nothing changes either way. A silent
        # except here would have converted a visible failure into an invisible
        # one — the field would simply stop advancing, and the sweeper that reads
        # it would start retiring agents that are working.
        try:
            now = _naive_utc()
            previous = getattr(agent, "last_active_ts", None)
            if previous is not None:
                # The file's own idiom, and the reason it exists.
                previous = _naive_utc(previous)
                if (now - previous).total_seconds() < _ACTIVITY_TOUCH_SECONDS:
                    return
            async with get_immediate_session() as session:
                db_agent = await session.get(Agent, agent.id)
                if (
                    db_agent is not None
                    and db_agent.project_id == agent.project_id
                    and db_agent.name == agent.name
                    and db_agent.agent_generation == agent.agent_generation
                ):
                    db_agent.last_active_ts = now
                    session.add(db_agent)
                    await session.commit()
            agent.last_active_ts = now
        # Broad on purpose: see the guard above. Bookkeeping must never fail a
        # call that already succeeded — but it says so in the log rather than
        # vanishing. This carried a `noqa: BLE001` until ruff removed it as
        # unused (BLE is not in this project's select list); the reason it
        # existed outlives the directive.
        except Exception as exc:
            logger.warning(
                "activity_touch.failed",
                extra={"agent": getattr(agent, "name", None), "error": str(exc)},
            )

    async def _authenticate_agent(
        ctx: Context,
        project: Project,
        agent_name: str,
        provided_token: Optional[str],
        *,
        token_param: str,
        action: str,
    ) -> Agent:
        agent = await _get_agent(project, agent_name)
        if _session_is_bound_to_agent(ctx, project, agent):
            _bind_session_agent(ctx, project, agent)
            await _touch_agent_activity(agent)
            return agent

        stored_token = (agent.registration_token or "").strip()
        if not stored_token:
            # Adjacent-agent auth for legacy tokenless agents: a bystander in
            # the same project may retire one, and may put it back. This
            # unsticks cleanup of pre-token agents without direct SQL surgery.
            # All other actions continue to require the target's own token.
            #
            # unretire_agent was NOT here, which made the "reversible" half
            # one-way in exactly the same case: retiring a tokenless agent
            # locked it out, since putting it back demanded the token it does
            # not have. Cleanup that cannot be undone is not cleanup.
            if action in ("retire_agent", "unretire_agent"):
                peer = await _resolve_session_agent_for_project(ctx, project)
                if peer is not None and peer.id != agent.id:
                    await ctx.info(
                        f"{action}: authorizing cleanup of tokenless legacy agent "
                        f"'{agent.name}' via adjacent agent '{peer.name}' in project "
                        f"'{project.human_key}'."
                    )
                    return agent
            raise ToolExecutionError(
                "AUTHENTICATION_REQUIRED",
                (
                    f"Agent '{agent.name}' does not have a registration token, so {action} cannot be authenticated. "
                    "Re-register or mint a token locally before retrying, or run this call from an MCP session "
                    "already authenticated as another agent in the same project (adjacent-agent auth is permitted "
                    "only for retire_agent and unretire_agent on tokenless legacy agents)."
                ),
                recoverable=True,
                data={"agent_name": agent.name, "project_key": project.human_key, "action": action},
            )
        if not provided_token:
            raise ToolExecutionError(
                "AUTHENTICATION_REQUIRED",
                (
                    f"{action} requires {token_param} for agent '{agent.name}', unless this MCP session has already "
                    "authenticated as that agent."
                ),
                recoverable=True,
                data={"agent_name": agent.name, "project_key": project.human_key, "token_param": token_param},
            )
        if not hmac.compare_digest(provided_token, stored_token):
            raise ToolExecutionError(
                "AUTHENTICATION_REQUIRED",
                f"Invalid {token_param} for agent '{agent.name}'.",
                recoverable=True,
                data={"agent_name": agent.name, "project_key": project.human_key, "token_param": token_param},
            )

        _bind_session_agent(ctx, project, agent)
        await _touch_agent_activity(agent)
        return agent

    async def _register_or_authenticate_agent(
        ctx: Context,
        project: Project,
        name: str | None,
        program: str,
        model: str,
        task_description: str,
        registration_token: str | None,
        *,
        action: str,
        allow_create: bool,
        attachments_policy: str | None = None,
        display_name: str | None = None,
    ) -> tuple[Agent, bool]:
        """Create an identity or authenticate before updating an existing one.

        The registration token is part of the unique-row INSERT.  Therefore a
        concurrent loser can identify the database winner but can neither
        receive its token nor update its profile without authenticating first.
        """
        if not allow_create:
            existing = await _find_agent_optional(project, name or "")
            if existing is None:
                raise ToolExecutionError(
                    "BOOTSTRAP_TOKEN_HANDOFF_REQUIRED",
                    (
                        f"Agent '{name}' does not exist. Provision it first with register_agent "
                        "or create_agent_identity, persist the one-time credential outside the "
                        "MCP transcript, then retry this session macro."
                    ),
                    recoverable=True,
                    data={
                        "agent_name": name,
                        "project_key": project.human_key,
                        "required_action": "provision_agent",
                    },
                )
            authenticated = await _authenticate_agent(
                ctx,
                project,
                existing.name,
                registration_token,
                token_param="registration_token",
                action=action,
            )
            if authenticated.id is None:
                raise NoResultFound(f"Agent '{authenticated.name}' no longer exists.")
            updated, recreated = await _get_or_create_agent(
                project,
                authenticated.name,
                program,
                model,
                task_description,
                settings,
                attachments_policy=attachments_policy,
                update_existing=True,
                expected_existing_agent_id=authenticated.id,
                expected_existing_agent_generation=authenticated.agent_generation,
                expected_project_generation=project.project_generation,
            )
            if recreated:
                raise RuntimeError(
                    f"Agent identity '{authenticated.name}' was recreated during authenticated registration."
                )
            return updated, False

        candidate_token = secrets.token_urlsafe(32)
        agent, newly_created = await _get_or_create_agent(
            project,
            name,
            program,
            model,
            task_description,
            settings,
            registration_token_on_create=candidate_token,
            attachments_policy=attachments_policy,
            update_existing=False,
            display_name=display_name,
        )
        if newly_created:
            return agent, True

        authenticated = await _authenticate_agent(
            ctx,
            project,
            agent.name,
            registration_token,
            token_param="registration_token",
            action=action,
        )
        if authenticated.id is None:
            raise NoResultFound(f"Agent '{authenticated.name}' no longer exists.")
        updated, recreated = await _get_or_create_agent(
            project,
            authenticated.name,
            program,
            model,
            task_description,
            settings,
            attachments_policy=attachments_policy,
            update_existing=True,
            expected_existing_agent_id=authenticated.id,
            expected_existing_agent_generation=authenticated.agent_generation,
            expected_project_generation=project.project_generation,
        )
        if recreated:
            raise RuntimeError(
                f"Agent identity '{authenticated.name}' was recreated during authenticated registration."
            )
        return updated, False

    async def _resolve_authenticated_agent(
        ctx: Context,
        project: Project,
        *,
        agent_name: Optional[str],
        provided_token: Optional[str],
        token_param: str,
        action: str,
    ) -> Agent:
        if agent_name:
            return await _authenticate_agent(
                ctx,
                project,
                agent_name,
                provided_token,
                token_param=token_param,
                action=action,
            )

        agent = await _resolve_session_agent_for_project(ctx, project)
        if agent is not None:
            return agent

        raise ToolExecutionError(
            "AUTHENTICATION_REQUIRED",
            (
                f"{action} requires an authenticated agent for project '{project.human_key}'. "
                "Provide agent_name plus registration_token, or authenticate in this session first."
            ),
            recoverable=True,
            data={"project_key": project.human_key, "token_param": token_param},
        )

    async def _authenticate_project_admin(
        ctx: Context,
        project: Project,
        provided_token: Optional[str],
        *,
        action: str,
    ) -> Agent:
        agent = await _resolve_session_agent_for_project(ctx, project)
        if agent is not None:
            return agent

        if project.id is None:
            raise ValueError("Project must have an id before authenticating project-scoped actions.")

        async with get_session() as session:
            agents_result = await session.execute(
                select(Agent).where(
                    cast(Any, Agent.project_id) == project.id,
                    cast(Any, Agent.registration_token).isnot(None),
                    cast(Any, Agent.provisioning_state == "active"),
                )
            )
            token_agents = agents_result.scalars().all()

        if not token_agents:
            raise ToolExecutionError(
                "AUTHENTICATION_REQUIRED",
                (
                    f"{action} requires a project registration token, but project '{project.human_key}' has no "
                    "token-bearing agents. Register or create an agent identity first."
                ),
                recoverable=True,
                data={"project_key": project.human_key, "action": action},
            )
        if not provided_token:
            raise ToolExecutionError(
                "AUTHENTICATION_REQUIRED",
                f"{action} requires registration_token matching a registered agent in project '{project.human_key}'.",
                recoverable=True,
                data={"project_key": project.human_key, "token_param": "registration_token"},
            )

        for token_agent in token_agents:
            if token_agent.registration_token and hmac.compare_digest(provided_token, token_agent.registration_token):
                _bind_session_agent(ctx, project, token_agent)
                return token_agent

        raise ToolExecutionError(
            "AUTHENTICATION_REQUIRED",
            f"Invalid registration_token for project '{project.human_key}'.",
            recoverable=True,
            data={"project_key": project.human_key, "token_param": "registration_token"},
        )

    async def _authenticate_product_agents(
        ctx: Context,
        product_key: str,
        *,
        agent_name: Optional[str],
        provided_token: Optional[str],
        token_param: str,
        action: str,
    ) -> tuple[Product, list[Project], list[tuple[Project, Agent]]]:
        await ensure_schema()
        async with get_session() as session:
            product = await _get_product_by_key(session, product_key.strip())
            if product is None:
                raise ToolExecutionError("NOT_FOUND", f"Product '{product_key}' not found.", recoverable=True)
            project_rows = await session.execute(
                select(Project)
                .join(ProductProjectLink, cast(Any, ProductProjectLink.project_id) == Project.id)
                .where(cast(Any, ProductProjectLink.product_id) == cast(Any, product.id))
            )
            projects = list(project_rows.scalars().all())

        authorized: list[tuple[Project, Agent]] = []
        for project in projects:
            if agent_name:
                agent = await _find_agent_optional(project, agent_name)
                if agent is None:
                    continue
                if _session_is_bound_to_agent(ctx, project, agent):
                    _bind_session_agent(ctx, project, agent)
                    authorized.append((project, agent))
                    continue
                stored_token = (agent.registration_token or "").strip()
                if stored_token and provided_token and hmac.compare_digest(provided_token, stored_token):
                    _bind_session_agent(ctx, project, agent)
                    authorized.append((project, agent))
                    continue
            else:
                session_agent = await _resolve_session_agent_for_project(ctx, project)
                if session_agent is not None:
                    authorized.append((project, session_agent))

        if authorized:
            return product, projects, authorized

        raise ToolExecutionError(
            "AUTHENTICATION_REQUIRED",
            (
                f"{action} requires an authenticated agent on at least one project linked to product '{product_key}'. "
                "Provide agent_name plus registration_token, or authenticate an agent in this MCP session first."
            ),
            recoverable=True,
            data={"product_key": product_key, "agent_name": agent_name, "token_param": token_param},
        )

    def _project_delivery_snapshot(project: Project) -> DeliveryProjectSnapshot:
        if project.id is None or not project.project_generation:
            raise RuntimeError("Project lifetime is incomplete.")
        return DeliveryProjectSnapshot(
            project_id=project.id,
            slug=project.slug,
            generation=project.project_generation,
        )

    def _agent_delivery_snapshot(
        agent: Agent,
        source_project: Project,
    ) -> DeliveryAgentSnapshot:
        if agent.id is None or not agent.agent_generation:
            raise RuntimeError("Agent lifetime is incomplete.")
        return DeliveryAgentSnapshot(
            agent_id=agent.id,
            name=agent.name,
            generation=agent.agent_generation,
            project=_project_delivery_snapshot(source_project),
        )

    def _delivery_status_payload(
        result: MessageDeliveryProcessingResult,
        *,
        reused: bool | None,
        request_sha256: str,
        document_sha256: str,
    ) -> dict[str, Any]:
        payload = {
            "id": result.delivery_id,
            "status": result.status,
            "message_id": result.message_id,
            "commit_sha": result.commit_sha,
            "next_attempt_ts": (
                _iso(result.next_attempt_ts)
                if result.next_attempt_ts is not None
                else None
            ),
            "error": result.error,
            "request_sha256": request_sha256,
            "document_sha256": document_sha256,
        }
        if reused is not None:
            payload["reused"] = reused
        return payload

    def _internal_delivery_idempotency_key(
        event_name: str,
        payload: dict[str, Any],
    ) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return f"internal:{event_name}:{hashlib.sha256(canonical).hexdigest()}"

    async def _deliver_message(
        ctx: Context,
        tool_name: str,
        project: Project,
        sender: Agent,
        to_names: Sequence[str],
        cc_names: Sequence[str],
        bcc_names: Sequence[str],
        subject: str,
        body_md: str,
        attachment_paths: Sequence[str] | None,
        convert_images_override: Optional[bool],
        importance: str,
        ack_required: bool,
        thread_id: Optional[str],
        idempotency_key: str,
        topic: Optional[str] = None,
        reply_to: Optional[int] = None,
        purpose: DeliveryPurpose = "message",
    ) -> dict[str, Any]:
        """Accept, publish, and finalize one immutable message delivery."""
        if attachment_paths is not None or convert_images_override is not None:
            raise ToolExecutionError(
                "ATTACHMENTS_NOT_SUPPORTED",
                "attachment_paths and convert_images are disabled until attachments "
                "have a bounded canonical inline representation.",
                recoverable=True,
                data={
                    "attachment_paths_provided": attachment_paths is not None,
                    "convert_images_provided": convert_images_override is not None,
                },
            )
        if not to_names and not cc_names and not bcc_names:
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "At least one recipient must be specified.",
                recoverable=True,
                data={"argument": "to"},
            )

        # Resolve canonical identities first, then deduplicate by immutable row
        # id. Name-only deduplication is insufficient because lookups are case
        # insensitive and ``BlueLake``/``bluelake`` name the same agent.
        combined_names = [*to_names, *cc_names, *bcc_names]
        agent_map = await _get_agents_batch(project, combined_names)
        recipient_groups: dict[str, list[Agent]] = {"to": [], "cc": [], "bcc": []}
        claimed_ids: set[int] = set()
        for kind, names in (
            ("to", to_names),
            ("cc", cc_names),
            ("bcc", bcc_names),
        ):
            for name in names:
                agent = agent_map[name]
                if agent.id is None:
                    raise RuntimeError("Recipient lifetime is incomplete.")
                if agent.id in claimed_ids:
                    continue
                claimed_ids.add(agent.id)
                recipient_groups[kind].append(agent)

        to_agents = recipient_groups["to"]
        cc_agents = recipient_groups["cc"]
        bcc_agents = recipient_groups["bcc"]
        sender_project = (
            project
            if sender.project_id == project.id
            else await _get_project_by_id(sender.project_id)
        )
        target_snapshot = _project_delivery_snapshot(project)
        sender_snapshot = _agent_delivery_snapshot(sender, sender_project)
        recipients = tuple(
            DeliveryRecipientSnapshot(
                kind=kind,
                agent=_agent_delivery_snapshot(agent, project),
            )
            for kind, agents in (
                ("to", to_agents),
                ("cc", cc_agents),
                ("bcc", bcc_agents),
            )
            for agent in agents
        )

        try:
            acceptance = await accept_message_delivery(
                MessageDeliveryRequest(
                    target_project=target_snapshot,
                    sender=sender_snapshot,
                    actor=DeliveryActorSnapshot.agent(sender_snapshot),
                    recipients=recipients,
                    idempotency_key=idempotency_key,
                    subject=subject,
                    body_md=body_md,
                    thread_id=thread_id,
                    reply_to_message_id=reply_to,
                    topic=topic,
                    importance=importance,
                    ack_required=ack_required,
                    attachments=(),
                    purpose=purpose,
                )
            )
            processing = await process_message_delivery(acceptance.delivery_id)
        except MessageDeliveryIdempotencyConflictError as exc:
            raise ToolExecutionError(
                "IDEMPOTENCY_CONFLICT",
                str(exc),
                recoverable=False,
                data={"delivery_id": exc.delivery_id, "idempotency_key": idempotency_key},
            ) from exc
        except MessageDeliveryValidationError as exc:
            raise ToolExecutionError(
                exc.code.upper(),
                str(exc),
                recoverable=True,
                data={"delivery_code": exc.code},
            ) from exc
        except (MessageDeliveryNotFoundError, MessageDeliveryTerminalError) as exc:
            raise ToolExecutionError(
                "DELIVERY_FAILED",
                str(exc),
                recoverable=False,
            ) from exc

        delivery_payload = _delivery_status_payload(
            processing,
            reused=acceptance.reused,
            request_sha256=acceptance.request_sha256,
            document_sha256=acceptance.document_sha256,
        )
        if processing.status != "published" or processing.message_id is None:
            await ctx.info(
                f"{tool_name}: delivery {processing.delivery_id} accepted with "
                f"status={processing.status}; no message is visible yet."
            )
            return {"delivery": delivery_payload, "message": None}

        async with get_session() as session:
            message = await session.get(Message, processing.message_id)
        if message is None or message.delivery_id != processing.delivery_id:
            raise RuntimeError("Published delivery does not resolve to its bound message.")

        message_payload = _message_to_dict(message)
        message_payload.update(
            {
                "to": [agent.name for agent in to_agents],
                "cc": [agent.name for agent in cc_agents],
                "bcc": [agent.name for agent in bcc_agents],
            }
        )
        _apply_sender_identity(
            message_payload,
            message_project_id=message.project_id,
            sender_name=sender.name,
            sender_project_id=sender_project.id,
            sender_project_human_key=sender_project.human_key,
            sender_project_slug=sender_project.slug,
        )

        resolved_settings = get_settings()
        window_uuid = getattr(resolved_settings, "window_identity_uuid", "") or ""
        if window_uuid and _validate_window_uuid(window_uuid):
            window_identity = await _get_window_identity(project, window_uuid)
            if window_identity is not None:
                message_payload["window_id"] = window_identity.window_uuid
                message_payload["window_display_name"] = window_identity.display_name

        if processing.published_now:
            await emit_published_delivery_notifications(processing.delivery_id)

        await ctx.info(
            f"{tool_name}: published message {message.id} from {sender.name} "
            f"as delivery {processing.delivery_id}."
        )
        return {"delivery": delivery_payload, "message": message_payload}

    def _extract_delivery_error_payload(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        error_payload = payload.get("error")
        if not isinstance(error_payload, dict):
            return None
        return dict(error_payload)

    def _with_delivery_project(error_payload: dict[str, Any], project: Project) -> dict[str, Any]:
        payload = dict(error_payload)
        payload.setdefault("project", project.human_key)
        return payload

    def _delivery_failure_from_exception(project: Project, exc: Exception) -> dict[str, Any]:
        if isinstance(exc, ToolExecutionError):
            return _with_delivery_project(exc.to_payload()["error"], project)
        message = str(exc).strip() or f"Failed to deliver message to project '{project.human_key}'."
        return _with_delivery_project({"type": "DELIVERY_FAILED", "message": message}, project)

    def _contact_targets_same_identity(
        source_project: Project,
        source_agent: Agent,
        target_project: Project,
        target_agent: Agent,
    ) -> bool:
        if (
            source_project.id is not None
            and target_project.id is not None
            and source_agent.id is not None
            and target_agent.id is not None
        ):
            return source_project.id == target_project.id and source_agent.id == target_agent.id
        return (
            source_project.human_key == target_project.human_key
            and source_agent.name.casefold() == target_agent.name.casefold()
        )

    def _raise_if_self_contact(
        source_project: Project,
        source_agent: Agent,
        target_project: Project,
        target_agent: Agent,
        *,
        action: str,
    ) -> None:
        if not _contact_targets_same_identity(source_project, source_agent, target_project, target_agent):
            return
        raise ToolExecutionError(
            "INVALID_ARGUMENT",
            f"{action} does not allow self-contact within the same project. Self-messaging already works without contact approval.",
            recoverable=True,
            data={"project_key": source_project.human_key, "agent_name": source_agent.name},
        )

    def _collect_delivery_result(
        deliveries: list[dict[str, Any]],
        delivery_errors: list[dict[str, Any]],
        project: Project,
        payload: dict[str, Any],
    ) -> None:
        error_payload = _extract_delivery_error_payload(payload)
        if error_payload is not None:
            delivery_errors.append(_with_delivery_project(error_payload, project))
            return
        deliveries.append({"project": project.human_key, **payload})

    def _summarize_delivery_failures(
        delivery_errors: Sequence[dict[str, Any]],
        *,
        summary_message: str,
    ) -> dict[str, Any]:
        if len(delivery_errors) == 1:
            return dict(delivery_errors[0])
        return {
            "type": "DELIVERY_FAILED",
            "message": summary_message,
            "errors": [dict(error) for error in delivery_errors],
        }

    async def _contact_request_notification_exists(
        project: Project,
        sender: Agent,
        recipient: Agent,
    ) -> bool:
        if project.id is None or sender.id is None or recipient.id is None:
            return False
        subject = f"Contact request from {sender.name}"
        async with get_session() as session:
            existing = await session.execute(
                select(Message.id)
                .join(MessageRecipient, cast(Any, MessageRecipient.message_id) == Message.id)
                .where(
                    cast(Any, Message.project_id) == project.id,
                    cast(Any, Message.sender_id) == sender.id,
                    cast(Any, Message.subject) == subject,
                    cast(Any, MessageRecipient.agent_id) == recipient.id,
                )
                .limit(1)
            )
            return existing.first() is not None

    @mcp.tool(name="health_check", description="Return basic readiness information for the Agent Mail server.")
    @_instrument_tool("health_check", cluster=CLUSTER_SETUP, capabilities={"infrastructure"}, complexity="low")
    async def health_check(ctx: Context, format: Optional[str] = None) -> dict[str, Any]:
        """
        Quick readiness probe for agents and orchestrators.

        When to use
        -----------
        - Before starting a workflow, to ensure the coordination server is reachable
          and configured (right environment, host/port, DB wiring).
        - During incident triage to print basic diagnostics to logs via `ctx.info`.

        What it checks vs what it does not
        ----------------------------------
        - Reports current environment and HTTP binding details.
        - Reads one indexed row from the database. `SELECT MAX(id) FROM
          messages` walks the table's own b-tree, which is the tree that went
          bad in all three corruptions on 2026-08-14 -- and while it failed for
          hours this probe reported `ok`, because it touched nothing. It costs
          ~5 ms on a 19 MB database, against ~150 ms for `PRAGMA quick_check`,
          which is too slow for something orchestrators call before every
          workflow. Note that `COUNT(*)` is NOT a substitute: it can be served
          from a different index and passed throughout that outage.
        - Deliberately omits the configured database URL because connection URLs
          may contain credentials or sensitive filesystem locations, and reports
          only the exception TYPE for the same reason.
        - Does not perform deep dependency health checks beyond that one read.

        Returns
        -------
        dict
            {
              "status": "ok" | "degraded",
              "build": {
                "application_version": str,
                "fastmcp_version": str,
                "git_sha": str | null
              },
              "environment": str,
              "http_host": str,
              "http_port": int,
              "http_path": str
            }

        Examples
        --------
        JSON-RPC (generic MCP client):
        ```json
        {"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"health_check","arguments":{}}}
        ```

        Typical agent usage (pseudocode):
        - Call `health_check`.
        - If status != ok, sleep/retry with backoff and log `environment`/`http_host`/`http_port`.
        """
        await ctx.info("Running health check.")
        database_status: str | None = None
        try:
            async with get_session() as session:
                await session.execute(text("SELECT MAX(id) FROM messages"))
        except Exception as exc:
            # The class of failure matters more than the instance, and the
            # message can name filesystem paths, so report the type only.
            database_status = type(exc).__name__

        if database_status is None:
            return {
                "status": "ok",
                "build": _authenticated_build_descriptor(settings),
                **_public_runtime_descriptor(settings),
            }
        return {
            "status": "degraded",
            "database": f"unreadable ({database_status})",
            "build": _authenticated_build_descriptor(settings),
            **_public_runtime_descriptor(settings),
        }

    @mcp.tool(name="ensure_project")
    @_instrument_tool("ensure_project", cluster=CLUSTER_SETUP, capabilities={"infrastructure", "storage"}, complexity="low", project_arg="human_key")
    async def ensure_project(
        ctx: Context,
        human_key: str,
        identity_mode: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Idempotently create or ensure a project exists for the given human key.

        When to use
        -----------
        - First call in a workflow targeting a new repository identity.
        - As a guard before registering agents or sending messages.

        How it works
        ------------
        - Validates that `human_key` is an absolute path-like project key. For a
          shared Git repository, normalize its origin to a synthetic key such as
          ``/owner/repository`` and use that same key on every host. A local
          checkout path is also accepted so the identity resolver can inspect
          its marker/discovery/remote metadata.
        - Resolves the durable `project_uid` before any DB lookup. Committed
          markers, discovery YAML, and normalized Git remotes therefore join
          linked worktrees and clones even when their local paths differ.
        - Ensures DB row exists and that the on-disk archive is initialized
          (e.g., `messages/`, `agents/`, `file_reservations/` directories).

        CRITICAL: Project Identity Rules
        ---------------------------------
        - The `human_key` MUST be an absolute path-like project key. Prefer the
          normalized remote key (for example ``/owner/repository``) across hosts;
          never make a checkout-specific path the shared identity by convention.
        - A durable marker/discovery/remote identity is authoritative across
          worktrees and hosts. Without one, normalized directory identity is used.

        Parameters
        ----------
        human_key : str
            An absolute path-like project key (for example ``/owner/backend``),
            normally derived from the normalized Git origin. This MUST be an
            absolute path, not a relative path or arbitrary slug, but it does not
            need to exist on the local filesystem. Passing a checkout path is
            useful only when the server must resolve its Git identity metadata.
            The first resolved path is retained as the project's canonical human
            alias; subsequent equivalent worktrees return that same DB row.
        identity_mode : str, optional
            Per-call override of the server's PROJECT_IDENTITY_MODE setting; one of
            "dir", "git-remote", "git-common-dir", "git-toplevel". Only takes effect when
            worktree-friendly identity is enabled (WORKTREES_ENABLED=1).

        Returns
        -------
        dict
            Minimal project descriptor: { id, project_uid, slug, human_key, created_at }.

        Examples
        --------
        JSON-RPC:
        ```json
        {
          "jsonrpc": "2.0",
          "id": "2",
          "method": "tools/call",
          "params": {"name": "ensure_project", "arguments": {"human_key": "/owner/backend"}}
        }
        ```

        Common mistakes
        ---------------
        - Passing a relative path (e.g., "./backend") instead of an absolute path
        - Using a checkout-specific path on one host and a normalized remote key
          on another without a shared marker/project UID
        - Creating separate project keys for the same Git origin

        Idempotency
        -----------
        - Safe to call multiple times. If the project already exists, the existing
          record is returned and the archive is ensured on disk (no destructive changes).
        """
        # Validate that human_key is an absolute path-like project key (cross-platform).
        # It need not exist on disk - it is an opaque project KEY, not a filesystem probe.
        if not _is_absolute_project_key(human_key):
            raise ValueError(
                f"human_key must be an absolute path-like project key, got: '{human_key}'. "
                "Use the normalized Git-origin key (for example '/owner/backend') "
                "or an absolute checkout path when resolving local Git metadata."
            )

        await _ctx_info_safe(ctx, f"Ensuring project for key '{human_key}'.")
        project = await _ensure_project(human_key, identity_mode=identity_mode)
        await ensure_archive(settings, project.slug)
        payload = _project_to_dict(project)
        # Worktree identity metadata is opt-in to keep default calls lightweight and stable.
        if settings.worktrees_enabled:
            identity_payload = await asyncio.to_thread(
                _resolve_project_identity,
                human_key,
                identity_mode,
            )
            payload["identity"] = identity_payload
            for key in (
                "identity_mode_used",
                "canonical_path",
                "repo_root",
                "git_common_dir",
                "branch",
                "worktree_name",
                "core_ignorecase",
                "normalized_remote",
                "project_uid",
            ):
                payload[key] = identity_payload.get(key)
        return payload

    @mcp.tool(name="register_agent")
    @_instrument_tool("register_agent", cluster=CLUSTER_IDENTITY, capabilities={"identity"}, agent_arg="name", project_arg="project_key")
    async def register_agent(
        ctx: Context,
        project_key: str,
        program: str,
        model: str,
        name: str,
        task_description: str = "",
        attachments_policy: str = "auto",
        registration_token: Optional[str] = None,
        display_name: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Create or update an agent identity within a project and persist its profile to Git.

        When to use
        -----------
        - At the start of a coding session by any automated agent.
        - To update an existing agent's program/model/task metadata and bump last_active.

        Semantics
        ---------
        - `name` is required and identifies a durable mailbox/persona.
        - Creation is authorized by the MCP transport's bearer/JWT/RBAC policy;
          a structurally valid name is an identifier, never an authenticator.
        - Reusing the same explicit `name` updates the profile (program/model/task) and
          refreshes `last_active_ts` only after registration-token/session authentication.
        - A `profile.json` file is written under `agents/<Name>/` in the project archive.

        Agent Identity
        ---------------
        Pass a stable ID such as ``codex-wsl-home-1``. Execution lifetimes and
        subagents belong in ``start_agent_execution``, not separate Agent rows.

        Parameters
        ----------
        project_key : str
            The same human key you passed to `ensure_project` (or equivalent identifier).
        program : str
            The agent program (e.g., "codex-cli", "claude-code").
        model : str
            The underlying model (e.g., "gpt5-codex", "opus-4.1").
        name : str
            Required stable ``client-os-host-slot`` identity, for example
            ``codex-wsl-home-1`` or ``claude-linux-ci-1``.
            Names are unique per project; passing the same name updates the profile.
        task_description : str
            Short description of current focus (shows up in directory listings).
        display_name : Optional[str]
            Human-readable label for a NEWLY provisioned Agent. When omitted, a
            friendly adjective+noun alias (for example ``BlueCastle``) is
            generated automatically for the insert winner. The alias is
            presentation only — it is never an address, never a credential, and
            authentication or profile updates of an existing Agent never touch
            it (use ``set_agent_display_name`` to change it later).
        Returns
        -------
        dict
            { id, name, program, model, task_description, inception_ts, last_active_ts, project_id }

        Examples
        --------
        Register a durable identity:
        ```json
        {"jsonrpc":"2.0","id":"4","method":"tools/call","params":{"name":"register_agent","arguments":{
          "project_key":"/data/projects/backend","program":"claude-code","model":"opus-4.1","name":"claude-linux-ci-1","task_description":"Navbar redesign"
        }}}
        ```

        Pitfalls
        --------
        - New names MUST match ``client-os-host-slot``; no fallback name is generated.
        - Names are case-insensitive unique. Resume the same durable identity with
          its registration token instead of creating a replacement Agent.
        - Use the same `project_key` consistently across cooperating agents.
        """
        _validate_program_model(program, model)
        if name is None or not name.strip():
            raise ToolExecutionError(
                "NAME_REQUIRED",
                "register_agent requires an explicit durable name.",
                recoverable=True,
                data={"field": "name"},
            )
        project = await _get_project_by_identifier(project_key)
        if settings.tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                c = Console()
                c.print(Panel(f"project=[bold]{project.human_key}[/]\nname=[bold]{name or '(required)'}[/]\nprogram={program}\nmodel={model}", title="tool: register_agent", border_style="green"))
            except Exception:
                pass
        # sanitize attachments policy
        ap = (attachments_policy or "auto").lower()
        if ap not in {"auto", "inline", "file"}:
            ap = "auto"
        agent, newly_created = await _register_or_authenticate_agent(
            ctx,
            project,
            name,
            program,
            model,
            task_description,
            registration_token,
            action="register_agent for an existing identity",
            allow_create=True,
            attachments_policy=ap,
            display_name=_sanitize_display_name_argument(display_name),
        )
        if newly_created:
            # Provisioning inserted the one-time credential before publishing
            # the exact DB-backed profile and activating the Agent.  Do not add
            # another fallible DB step after activation but before handoff: an
            # error there would strand a valid credential the caller never saw.
            token = (agent.registration_token or "").strip()
            if not token:
                raise RuntimeError(
                    f"Provisioned Agent id '{agent.id}' has no registration token."
                )
        if newly_created:
            with suppress(Exception):
                _bind_session_agent(ctx, project, agent)
        else:
            _bind_session_agent(ctx, project, agent)
        await _ctx_info_safe(
            ctx,
            f"Registered agent '{agent.name}' for project '{project.human_key}'.",
        )
        result = _agent_to_dict(agent)
        if newly_created:
            result["registration_token"] = token
        else:
            result["registration_token_issued"] = False
        # Enrich with window identity info if MCP_AGENT_MAIL_WINDOW_ID is set.
        # NOTE: _get_or_create_agent already resolved this for the archive profile,
        # but propagating it via return type would churn 8+ callers for a cold-path query.
        window_uuid = getattr(settings, "window_identity_uuid", "") or ""
        if window_uuid and _validate_window_uuid(window_uuid):
            try:
                wi = await _get_window_identity(project, window_uuid)
            except Exception:
                # Window metadata is optional response enrichment.  The exact
                # association was already included in provisioning/profile
                # publication; a read failure must not burn token handoff.
                wi = None
            if wi is not None:
                result["window_id"] = wi.window_uuid
                result["window_display_name"] = wi.display_name
        return result

    @mcp.tool(name="start_agent_execution")
    @_instrument_tool(
        "start_agent_execution",
        cluster=CLUSTER_IDENTITY,
        capabilities={"identity", "repository"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def start_agent_execution(
        ctx: Context,
        project_key: str,
        agent_name: str,
        external_id: str,
        client_name: str,
        execution_token: str,
        lifecycle_protocol_version: Optional[int] = None,
        kind: str = "session",
        parent_execution_id: Optional[str] = None,
        parent_execution_token: Optional[str] = None,
        turn_id: Optional[str] = None,
        agent_type: Optional[str] = None,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
        task_description: str = "",
        cwd: Optional[str] = None,
        repo_root: Optional[str] = None,
        git_common_dir: Optional[str] = None,
        worktree_path: Optional[str] = None,
        branch: Optional[str] = None,
        head_sha: Optional[str] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Start and bind one session/subagent lifetime to a durable Agent."""
        project = await _get_project_by_identifier(project_key)
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="start_agent_execution",
        )
        if project.id is None or agent.id is None:
            raise ValueError("Project and agent must have ids before starting an execution.")

        normalized_external_id = cast(
            str,
            _bounded_execution_text(
                "external_id", external_id, 255, required=True
            ),
        )
        normalized_client_name = cast(
            str,
            _bounded_execution_text(
                "client_name", client_name, 128, required=True
            ),
        )
        normalized_kind = kind.strip().lower()
        if _EXECUTION_TOKEN_PATTERN.fullmatch(execution_token) is None:
            raise ToolExecutionError(
                "INVALID_EXECUTION_TOKEN",
                "execution_token must be exactly 64 lowercase hexadecimal characters.",
                recoverable=False,
            )
        requested_protocol_version, protocol_warning = _validate_execution_protocol(
            lifecycle_protocol_version,
            settings=get_settings(),
        )
        execution_token_hash = hashlib.sha256(
            execution_token.encode("utf-8")
        ).hexdigest()
        if normalized_kind not in {"session", "subagent"}:
            raise ToolExecutionError(
                "INVALID_EXECUTION_KIND",
                "kind must be 'session' or 'subagent'.",
                data={"kind": kind},
            )

        normalized_parent_id = _bounded_execution_text(
            "parent_execution_id", parent_execution_id, 36
        )
        normalized_turn_id = _bounded_execution_text("turn_id", turn_id, 255)
        normalized_agent_type = _bounded_execution_text(
            "agent_type", agent_type, 128
        )
        normalized_model = _bounded_execution_text("model", model, 128)
        normalized_permission_mode = _bounded_execution_text(
            "permission_mode", permission_mode, 64
        )
        normalized_task = _bounded_execution_text(
            "task_description", task_description, 2048
        ) or ""
        normalized_cwd = _bounded_execution_text("cwd", cwd, 2048)
        normalized_repo_root = _bounded_execution_text(
            "repo_root", repo_root, 2048
        )
        normalized_git_common_dir = _bounded_execution_text(
            "git_common_dir", git_common_dir, 2048
        )
        normalized_worktree_path = _bounded_execution_text(
            "worktree_path", worktree_path, 2048
        )
        normalized_branch = _bounded_execution_text("branch", branch, 512)
        normalized_head = _bounded_execution_text("head_sha", head_sha, 40)
        if normalized_kind == "session" and normalized_parent_id is not None:
            raise ToolExecutionError(
                "INVALID_PARENT_EXECUTION",
                "A session execution cannot have a parent_execution_id.",
                data={"parent_execution_id": normalized_parent_id},
            )
        if normalized_kind == "subagent" and normalized_parent_id is None:
            raise ToolExecutionError(
                "PARENT_EXECUTION_REQUIRED",
                "A subagent execution requires an active parent_execution_id.",
                data={"kind": normalized_kind},
            )
        if normalized_head is not None:
            normalized_head = normalized_head.lower()
            if not re.fullmatch(r"[0-9a-f]{40}", normalized_head):
                raise ToolExecutionError(
                    "INVALID_HEAD_SHA",
                    "head_sha must be exactly 40 hexadecimal characters.",
                    data={"head_sha": head_sha},
                )

        now = _naive_utc()
        reused = False
        async with get_immediate_session() as session:
            db_project = await session.get(Project, project.id)
            db_agent = await session.get(Agent, agent.id)
            if (
                db_project is None
                or db_project.project_generation != project.project_generation
                or db_agent is None
                or db_agent.project_id != project.id
                or db_agent.agent_generation != agent.agent_generation
                or db_agent.provisioning_state != "active"
            ):
                raise ToolExecutionError(
                    "AGENT_IDENTITY_STALE",
                    "The authenticated project or Agent lifetime no longer exists.",
                    recoverable=True,
                    data={"project_key": project.human_key, "agent_name": agent.name},
                )
            if db_agent.retired_at is not None:
                raise ToolExecutionError(
                    "AGENT_RETIRED",
                    f"Agent '{agent.name}' is retired and cannot start an execution.",
                    recoverable=False,
                    data={"project_key": project.human_key, "agent_name": agent.name},
                )
            parent: AgentExecution | None = None
            if normalized_parent_id is not None:
                parent = await session.get(AgentExecution, normalized_parent_id)
                if (
                    parent is None
                    or parent.project_id != project.id
                    or parent.agent_id != agent.id
                ):
                    raise ToolExecutionError(
                        "INVALID_PARENT_EXECUTION",
                        "Parent execution must belong to the same project and Agent.",
                        data={"parent_execution_id": normalized_parent_id},
                    )
                if parent.status != "active":
                    raise ToolExecutionError(
                        "PARENT_EXECUTION_NOT_ACTIVE",
                        f"Parent execution '{parent.id}' is '{parent.status}', not active.",
                        data={"parent_execution_id": parent.id, "status": parent.status},
                    )
                parent_is_bound = (
                    _session_execution_id(ctx, project, agent) == parent.id
                )
                if not parent_is_bound and (
                    not parent_execution_token
                    or not hmac.compare_digest(
                        hashlib.sha256(
                            parent_execution_token.encode("utf-8")
                        ).hexdigest(),
                        parent.execution_token_hash,
                    )
                ):
                    raise ToolExecutionError(
                        "EXECUTION_CAPABILITY_MISMATCH",
                        f"Invalid parent_execution_token for execution '{parent.id}'.",
                        recoverable=False,
                        data={"parent_execution_id": parent.id},
                    )

            existing_stmt = select(AgentExecution).where(
                cast(Any, AgentExecution.client_name) == normalized_client_name,
                cast(Any, AgentExecution.external_id) == normalized_external_id,
                cast(Any, AgentExecution.kind) == normalized_kind,
            )
            if normalized_kind == "session":
                existing_stmt = existing_stmt.where(
                    cast(Any, AgentExecution.agent_id) == agent.id,
                    cast(Any, AgentExecution.parent_execution_id).is_(None),
                )
            else:
                existing_stmt = existing_stmt.where(
                    cast(Any, AgentExecution.parent_execution_id)
                    == normalized_parent_id
                )
            existing_result = await session.execute(existing_stmt)
            execution = existing_result.scalars().first()
            if execution is not None:
                if not hmac.compare_digest(
                    execution_token_hash, execution.execution_token_hash
                ):
                    raise ToolExecutionError(
                        "EXECUTION_CAPABILITY_MISMATCH",
                        "Idempotent start requires the original execution_token.",
                        recoverable=False,
                        data={"execution_id": execution.id},
                    )
                if execution.status != "active":
                    raise ToolExecutionError(
                        "EXECUTION_ALREADY_ENDED",
                        (
                            f"Execution external_id '{normalized_external_id}' already ended "
                            f"as '{execution.status}' and cannot be reactivated."
                        ),
                        recoverable=False,
                        data=_agent_execution_to_dict(execution),
                    )
                immutable_existing = (
                    execution.project_id,
                    execution.agent_id,
                    execution.kind,
                    execution.parent_execution_id,
                    execution.client_name,
                    execution.turn_id if execution.kind == "subagent" else None,
                    execution.agent_type if execution.kind == "subagent" else None,
                )
                immutable_requested = (
                    project.id,
                    agent.id,
                    normalized_kind,
                    normalized_parent_id,
                    normalized_client_name,
                    normalized_turn_id if normalized_kind == "subagent" else None,
                    normalized_agent_type if normalized_kind == "subagent" else None,
                )
                if immutable_existing != immutable_requested:
                    raise ToolExecutionError(
                        "EXECUTION_CONFLICT",
                        "An active execution with this external_id has different immutable identity metadata.",
                        recoverable=False,
                        data={"execution_id": execution.id, "external_id": normalized_external_id},
                    )
                reused = True
                execution.task_description = normalized_task
                execution.lifecycle_protocol_version = max(
                    execution.lifecycle_protocol_version,
                    requested_protocol_version,
                )
                execution.model = normalized_model
                execution.permission_mode = normalized_permission_mode
                execution.cwd = normalized_cwd
                execution.repo_root = normalized_repo_root
                execution.git_common_dir = normalized_git_common_dir
                execution.worktree_path = normalized_worktree_path
                execution.branch = normalized_branch
                execution.head_sha = normalized_head
                execution.last_active_ts = now
            else:
                token_owner = (
                    await session.execute(
                        select(AgentExecution.id).where(
                            cast(Any, AgentExecution.execution_token_hash)
                            == execution_token_hash
                        )
                    )
                ).scalar_one_or_none()
                if token_owner is not None:
                    raise ToolExecutionError(
                        "EXECUTION_CAPABILITY_REUSED",
                        "execution_token is already assigned to a different execution lifetime.",
                        recoverable=False,
                    )
                execution = AgentExecution(
                    id=str(uuid.uuid4()),
                    project_id=project.id,
                    agent_id=agent.id,
                    parent_execution_id=normalized_parent_id,
                    external_id=normalized_external_id,
                    client_name=normalized_client_name,
                    execution_token_hash=execution_token_hash,
                    lifecycle_protocol_version=requested_protocol_version,
                    kind=normalized_kind,
                    status="active",
                    turn_id=normalized_turn_id,
                    agent_type=normalized_agent_type,
                    model=normalized_model,
                    permission_mode=normalized_permission_mode,
                    task_description=normalized_task,
                    cwd=normalized_cwd,
                    repo_root=normalized_repo_root,
                    git_common_dir=normalized_git_common_dir,
                    worktree_path=normalized_worktree_path,
                    branch=normalized_branch,
                    head_sha=normalized_head,
                    started_ts=now,
                    last_active_ts=now,
                )
            session.add(execution)
            await session.commit()
            await session.refresh(execution)

        _bind_session_execution(ctx, project, agent, execution)
        ancestor_ids = await _load_execution_ancestor_ids(execution)
        response = {
            **_agent_execution_to_dict(
                execution,
                ancestor_execution_ids=ancestor_ids,
            ),
            "reused": reused,
        }
        if protocol_warning is not None:
            response["warnings"] = [protocol_warning]
        return response

    @mcp.tool(name="heartbeat_agent_execution")
    @_instrument_tool(
        "heartbeat_agent_execution",
        cluster=CLUSTER_IDENTITY,
        capabilities={"identity", "repository"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def heartbeat_agent_execution(
        ctx: Context,
        project_key: str,
        agent_name: str,
        execution_id: Optional[str] = None,
        execution_token: Optional[str] = None,
        lifecycle_protocol_version: Optional[int] = None,
        task_description: Optional[str] = None,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
        cwd: Optional[str] = None,
        repo_root: Optional[str] = None,
        git_common_dir: Optional[str] = None,
        worktree_path: Optional[str] = None,
        branch: Optional[str] = None,
        head_sha: Optional[str] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Heartbeat an active execution and optionally refresh observed metadata."""
        project = await _get_project_by_identifier(project_key)
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="heartbeat_agent_execution",
        )
        requested_protocol_version, protocol_warning = _validate_execution_protocol(
            lifecycle_protocol_version,
            settings=get_settings(),
        )
        execution = await _resolve_agent_execution(
            ctx,
            project,
            agent,
            execution_id,
            execution_token,
            action="heartbeat_agent_execution",
            touch_activity=False,
        )
        assert execution is not None
        normalized_head = _bounded_execution_text("head_sha", head_sha, 40)
        if normalized_head is not None:
            normalized_head = normalized_head.lower()
        if normalized_head is not None and not re.fullmatch(r"[0-9a-f]{40}", normalized_head):
            raise ToolExecutionError(
                "INVALID_HEAD_SHA",
                "head_sha must be exactly 40 hexadecimal characters.",
                data={"head_sha": head_sha},
            )
        updates: dict[str, str | None] = {}
        if task_description is not None:
            updates["task_description"] = (
                _bounded_execution_text(
                    "task_description", task_description, 2048
                )
                or ""
            )
        for field_name, value, max_length in (
            ("model", model, 128),
            ("permission_mode", permission_mode, 64),
            ("cwd", cwd, 2048),
            ("repo_root", repo_root, 2048),
            ("git_common_dir", git_common_dir, 2048),
            ("worktree_path", worktree_path, 2048),
            ("branch", branch, 512),
        ):
            if value is not None:
                updates[field_name] = _bounded_execution_text(
                    field_name, value, max_length
                )
        if head_sha is not None:
            updates["head_sha"] = normalized_head
        async with get_immediate_session() as session:
            db_execution = await session.get(AgentExecution, execution.id)
            if db_execution is None:
                raise ToolExecutionError("EXECUTION_NOT_FOUND", "Execution no longer exists.")
            if db_execution.status != "active":
                raise ToolExecutionError(
                    "EXECUTION_NOT_ACTIVE",
                    f"Agent execution '{db_execution.id}' is '{db_execution.status}', not active.",
                    data={"execution_id": db_execution.id, "status": db_execution.status},
                )
            for field_name, value in updates.items():
                setattr(db_execution, field_name, value)
            db_execution.last_active_ts = _naive_utc()
            db_execution.lifecycle_protocol_version = max(
                db_execution.lifecycle_protocol_version,
                requested_protocol_version,
            )
            session.add(db_execution)
            await session.commit()
            await session.refresh(db_execution)
        _bind_session_execution(ctx, project, agent, db_execution)
        response = _agent_execution_to_dict(
            db_execution,
            ancestor_execution_ids=await _load_execution_ancestor_ids(db_execution),
        )
        if protocol_warning is not None:
            response["warnings"] = [protocol_warning]
        return response

    @mcp.tool(name="end_agent_execution")
    @_instrument_tool(
        "end_agent_execution",
        cluster=CLUSTER_IDENTITY,
        capabilities={"identity", "file_reservations"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def end_agent_execution(
        ctx: Context,
        project_key: str,
        agent_name: str,
        status: str = "completed",
        execution_id: Optional[str] = None,
        execution_token: Optional[str] = None,
        lifecycle_protocol_version: Optional[int] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """End an execution tree and atomically release only its claims."""
        terminal_status = status.strip().lower()
        requested_protocol_version, protocol_warning = _validate_execution_protocol(
            lifecycle_protocol_version,
            settings=get_settings(),
        )
        if terminal_status not in {"completed", "failed", "cancelled"}:
            raise ToolExecutionError(
                "INVALID_EXECUTION_STATUS",
                "status must be completed, failed, or cancelled.",
                data={"status": status},
            )
        project = await _get_project_by_identifier(project_key)
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="end_agent_execution",
        )
        execution = await _resolve_agent_execution(
            ctx,
            project,
            agent,
            execution_id,
            execution_token,
            action="end_agent_execution",
            require_active=False,
            allow_authenticated_owner_recovery=True,
            require_active_capability=True,
            touch_activity=False,
        )
        assert execution is not None

        async def _reconcile_terminal_retry(
            terminal_execution: AgentExecution,
        ) -> dict[str, Any]:
            """Repair post-commit archive artifacts on an idempotent end retry."""
            async with get_session() as session:
                execution_rows = await _load_execution_descendant_rows(
                    session,
                    [terminal_execution.id],
                    project_id=cast(int, project.id),
                    active_only=False,
                )
                lineage_rows = await _load_execution_lineage_rows(
                    session,
                    [terminal_execution.id],
                    project_id=cast(int, project.id),
                )
                terminal_descendants = [
                    descendant
                    for descendant in _execution_descendants_all_child_first(
                        execution_rows,
                        terminal_execution.id,
                    )
                    if descendant.status != "active"
                ]
                descendant_ids = [
                    descendant.id for descendant in terminal_descendants
                ]
                terminal_ids = {terminal_execution.id, *descendant_ids}

            _clear_execution_bindings(terminal_ids)
            archive_warnings: list[str] = []
            reconciled_reservation_artifacts = 0
            try:
                reconciled_reservation_artifacts = (
                    await _reconcile_pending_file_reservation_artifacts(project)
                )
            except Exception as exc:
                archive_warnings.append(f"reservations: {exc}")
                logger.exception(
                    "execution_end.retry_reservation_archive_failed",
                    extra={"execution_id": terminal_execution.id},
                )

            released_at = (
                _ensure_utc(terminal_execution.ended_ts)
                or datetime.now(timezone.utc)
            )
            released_build_slots = 0
            try:
                released_build_slots = (
                    await _release_build_slot_artifacts_for_executions(
                        project,
                        terminal_ids,
                        released_at,
                    )
                )
                await _ack_execution_build_slot_reconciliation(
                    terminal_ids,
                    released_at,
                )
            except Exception as exc:
                archive_warnings.append(f"build slots: {exc}")
                logger.exception(
                    "execution_end.retry_build_slot_archive_failed",
                    extra={"execution_id": terminal_execution.id},
                )

            ancestor_ids = _execution_ancestor_ids(
                lineage_rows,
                terminal_execution,
            )
            retry_payload: dict[str, Any] = {
                "execution": _agent_execution_to_dict(
                    terminal_execution,
                    ancestor_execution_ids=ancestor_ids,
                ),
                "already_ended": True,
                "descendants_ended": 0,
                "descendant_execution_ids": descendant_ids,
                "released_reservations": 0,
                "reconciled_reservation_artifacts": (
                    reconciled_reservation_artifacts
                ),
                "released_build_slots": released_build_slots,
            }
            if archive_warnings:
                retry_payload["archive_warning"] = "; ".join(archive_warnings)
            if protocol_warning is not None:
                retry_payload["warnings"] = [protocol_warning]
            return retry_payload

        if execution.status != "active":
            if execution.status != terminal_status:
                raise ToolExecutionError(
                    "EXECUTION_TERMINAL_CONFLICT",
                    (
                        f"Execution '{execution.id}' already ended as '{execution.status}', "
                        f"not '{terminal_status}'."
                    ),
                    recoverable=False,
                    data=_agent_execution_to_dict(execution),
                )
            return await _reconcile_terminal_retry(execution)

        now = _naive_utc()
        released_reservations: list[FileReservation] = []
        descendant_ids: list[str] = []
        already_ended_after_lock = False
        async with get_immediate_session() as session:
            db_execution = await session.get(AgentExecution, execution.id)
            if db_execution is None:
                raise ToolExecutionError("EXECUTION_NOT_FOUND", "Execution no longer exists.")
            if db_execution.status != "active":
                if db_execution.status != terminal_status:
                    raise ToolExecutionError(
                        "EXECUTION_TERMINAL_CONFLICT",
                        f"Execution '{db_execution.id}' already ended as '{db_execution.status}'.",
                        recoverable=False,
                    )
                execution = db_execution
                already_ended_after_lock = True
            else:
                db_execution.lifecycle_protocol_version = max(
                    db_execution.lifecycle_protocol_version,
                    requested_protocol_version,
                )
                execution_rows = await _load_execution_descendant_rows(
                    session,
                    [db_execution.id],
                    project_id=cast(int, project.id),
                    active_only=True,
                )
                descendants = _execution_descendants_child_first(
                    execution_rows, db_execution.id
                )
                descendant_ids = [item.id for item in descendants]
                ending_ids = [*descendant_ids, db_execution.id]
                reservation_result = await session.execute(
                    select(FileReservation).where(
                        cast(Any, FileReservation.execution_id).in_(ending_ids),
                        cast(Any, FileReservation.origin) == "auto",
                        cast(Any, FileReservation.released_ts).is_(None),
                    )
                )
                released_reservations = list(reservation_result.scalars().all())
                for reservation in released_reservations:
                    reservation.released_ts = now
                    session.add(reservation)
                # Storage refuses to terminalize an execution while any of its
                # active claims remain. Flush claims before the first child.
                await session.flush()
                for descendant in descendants:
                    descendant.status = "cancelled"
                    descendant.last_active_ts = now
                    descendant.ended_ts = now
                    session.add(descendant)
                    # The parent trigger requires strict child-first order; do
                    # not leave ORM statement ordering to chance.
                    await session.flush()
                db_execution.status = terminal_status
                db_execution.last_active_ts = now
                db_execution.ended_ts = now
                session.add(db_execution)
                await session.flush()
                await session.commit()
                await session.refresh(db_execution)
                execution = db_execution

        if already_ended_after_lock:
            return await _reconcile_terminal_retry(execution)

        ending_id_set = {execution.id, *descendant_ids}
        _clear_execution_bindings(ending_id_set)
        archive_warning: str | None = None
        try:
            await _reconcile_pending_file_reservation_artifacts(project)
        except Exception as exc:
            archive_warning = str(exc)
            logger.exception(
                "execution_end.reservation_archive_failed",
                extra={"execution_id": execution.id},
            )
        released_build_slots = 0
        try:
            released_build_slots = await _release_build_slot_artifacts_for_executions(
                project,
                ending_id_set,
                now,
            )
            await _ack_execution_build_slot_reconciliation(
                ending_id_set,
                now,
            )
        except Exception as exc:
            build_slot_warning = str(exc)
            archive_warning = (
                f"{archive_warning}; build slots: {build_slot_warning}"
                if archive_warning
                else f"build slots: {build_slot_warning}"
            )
            logger.exception(
                "execution_end.build_slot_archive_failed",
                extra={"execution_id": execution.id},
            )
        ancestor_ids = await _load_execution_ancestor_ids(execution)
        payload: dict[str, Any] = {
            "execution": _agent_execution_to_dict(
                execution,
                ancestor_execution_ids=ancestor_ids,
            ),
            "already_ended": False,
            "descendants_ended": len(descendant_ids),
            "descendant_execution_ids": descendant_ids,
            "released_reservations": len(released_reservations),
            "released_build_slots": released_build_slots,
        }
        if archive_warning is not None:
            payload["archive_warning"] = archive_warning
        if protocol_warning is not None:
            payload["warnings"] = [protocol_warning]
        return payload

    @mcp.tool(name="list_agent_executions")
    @_instrument_tool(
        "list_agent_executions",
        cluster=CLUSTER_IDENTITY,
        capabilities={"identity"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def list_agent_executions(
        ctx: Context,
        project_key: str,
        agent_name: str,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> ToonableList:
        """List execution audit rows for one authenticated durable Agent."""
        project = await _get_project_by_identifier(project_key)
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="list_agent_executions",
        )
        stmt = select(AgentExecution).where(
            cast(Any, AgentExecution.project_id) == project.id,
            cast(Any, AgentExecution.agent_id) == agent.id,
        )
        if status is not None:
            normalized_status = status.strip().lower()
            if normalized_status not in {"active", "completed", "failed", "cancelled", "expired"}:
                raise ToolExecutionError("INVALID_EXECUTION_STATUS", "Invalid execution status filter.")
            stmt = stmt.where(cast(Any, AgentExecution.status) == normalized_status)
        if kind is not None:
            normalized_kind = kind.strip().lower()
            if normalized_kind not in {"session", "subagent"}:
                raise ToolExecutionError("INVALID_EXECUTION_KIND", "Invalid execution kind filter.")
            stmt = stmt.where(cast(Any, AgentExecution.kind) == normalized_kind)
        stmt = stmt.order_by(desc(cast(Any, AgentExecution.started_ts))).limit(
            max(1, min(500, int(limit)))
        )
        async with get_session() as session:
            rows = list((await session.execute(stmt)).scalars().all())
            lineage_rows = await _load_execution_lineage_rows(
                session,
                [item.id for item in rows],
                project_id=cast(int, project.id),
            )
        return [
            _agent_execution_to_dict(
                item,
                ancestor_execution_ids=_execution_ancestor_ids(lineage_rows, item),
            )
            for item in rows
        ]

    @mcp.tool(
        name="retire_agent",
        description="Soft-delete an agent: mark it as retired so it stops accepting new messages while preserving message history. "
        "Retired agents are hidden from active agent lists but visible in 'all agents' views.",
    )
    @_instrument_tool("retire_agent", cluster=CLUSTER_IDENTITY, capabilities={"identity"}, agent_arg="agent_name", project_arg="project_key")
    async def retire_agent(
        ctx: Context,
        project_key: str,
        agent_name: str,
        registration_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Retire an agent (soft-delete). The agent stops accepting new messages but message history is preserved."""
        project = await _get_project_by_identifier(project_key)
        if not project:
            raise ValueError(f"Project '{project_key}' not found")

        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="retire_agent",
        )

        async with get_immediate_session() as session:
            _db_project, db_agent, _db_execution = (
                await _revalidate_agent_lifetime_in_session(
                    session,
                    project=project,
                    agent=agent,
                    action="retire_agent",
                )
            )
            db_agent.retired_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(db_agent)
            await session.commit()

        await ctx.info(f"Retired agent '{agent_name}' from project '{project.human_key}'. Message history preserved.")
        return {
            "status": "retired",
            "agent_name": agent_name,
            "project_key": project_key,
        }

    @mcp.tool(
        name="sweep_stale_agents",
        description=(
            "Retire abandoned agents in the caller's project using the server's conservative inactivity heuristic. "
            "The caller is never retired, the threshold has a 60-second floor, and active file reservations block "
            "retirement by default."
        ),
    )
    @_instrument_tool(
        "sweep_stale_agents",
        cluster=CLUSTER_IDENTITY,
        capabilities={"identity", "file_reservations"},
        agent_arg="agent_name",
        project_arg="project_key",
    )
    async def sweep_stale_agents_tool(
        ctx: Context,
        project_key: str,
        agent_name: str,
        threshold_seconds: int = 86400,
        require_no_active_reservations: bool = True,
        registration_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Retire stale project agents on demand without target-token custody."""
        project = await _get_project_by_identifier(project_key)
        actor = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="sweep_stale_agents",
        )
        if project.id is None or actor.id is None:
            raise ValueError("Project and caller must have ids before sweeping stale agents.")

        effective_threshold = max(60, int(threshold_seconds))
        retired = await sweep_stale_agents(
            threshold_seconds=effective_threshold,
            project_id=project.id,
            exclude_agent_id=actor.id,
            require_no_active_reservations=require_no_active_reservations,
        )
        retired_names = [entry["agent_name"] for entry in retired]
        await ctx.info(f"Retired {len(retired_names)} stale agent(s) in project '{project.human_key}'.")
        return {
            "project_key": project.human_key,
            "requested_by": actor.name,
            "threshold_seconds": effective_threshold,
            "require_no_active_reservations": require_no_active_reservations,
            "retired": retired_names,
            "retired_agents": retired,
            "count": len(retired_names),
        }

    @mcp.tool(
        name="unretire_agent",
        description="Restore a retired agent back to active status. The agent will resume accepting new messages.",
    )
    @_instrument_tool("unretire_agent", cluster=CLUSTER_IDENTITY, capabilities={"identity"}, agent_arg="agent_name", project_arg="project_key")
    async def unretire_agent(
        ctx: Context,
        project_key: str,
        agent_name: str,
        registration_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Restore a retired agent back to active status."""
        project = await _get_project_by_identifier(project_key)
        if not project:
            raise ValueError(f"Project '{project_key}' not found")

        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="unretire_agent",
        )

        async with get_immediate_session() as session:
            _db_project, db_agent, _db_execution = (
                await _revalidate_agent_lifetime_in_session(
                    session,
                    project=project,
                    agent=agent,
                    action="unretire_agent",
                )
            )
            db_agent.retired_at = None
            session.add(db_agent)
            await session.commit()

        await ctx.info(f"Restored agent '{agent_name}' in project '{project.human_key}' to active status.")
        return {
            "status": "active",
            "agent_name": agent_name,
            "project_key": project_key,
        }

    @mcp.tool(
        name="archive_project",
        description="Soft-delete a project: mark it as archived so it is hidden from active project lists. "
        "All messages are preserved and the project can be restored with unarchive_project.",
    )
    @_instrument_tool("archive_project", cluster=CLUSTER_SETUP, capabilities={"infrastructure"}, project_arg="project_key")
    async def archive_project(
        ctx: Context,
        project_key: str,
        registration_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Archive a project (soft-delete). Hides from active lists, preserves all messages."""
        project = await _get_project_by_identifier(project_key)
        if not project:
            raise ValueError(f"Project '{project_key}' not found")

        await _authenticate_project_admin(
            ctx,
            project,
            registration_token,
            action="archive_project",
        )

        async with get_immediate_session() as session:
            db_project = await _revalidate_project_lifetime_in_session(
                session,
                project=project,
                action="archive_project",
            )
            db_project.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(db_project)
            await session.commit()

        await ctx.info(f"Archived project '{project.human_key}'. All messages preserved.")
        return {
            "status": "archived",
            "project_key": project_key,
            "slug": project.slug,
        }

    @mcp.tool(
        name="unarchive_project",
        description="Restore an archived project back to active status.",
    )
    @_instrument_tool("unarchive_project", cluster=CLUSTER_SETUP, capabilities={"infrastructure"}, project_arg="project_key")
    async def unarchive_project(
        ctx: Context,
        project_key: str,
        registration_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Restore an archived project back to active status."""
        project = await _get_project_by_identifier(project_key)
        if not project:
            raise ValueError(f"Project '{project_key}' not found")

        await _authenticate_project_admin(
            ctx,
            project,
            registration_token,
            action="unarchive_project",
        )

        async with get_immediate_session() as session:
            db_project = await _revalidate_project_lifetime_in_session(
                session,
                project=project,
                action="unarchive_project",
            )
            db_project.archived_at = None
            session.add(db_project)
            await session.commit()

        await ctx.info(f"Restored project '{project.human_key}' to active status.")
        return {
            "status": "active",
            "project_key": project_key,
            "slug": project.slug,
        }

    @mcp.tool(name="whois")
    @_instrument_tool("whois", cluster=CLUSTER_IDENTITY, capabilities={"identity", "audit"}, project_arg="project_key", agent_arg="agent_name")
    async def whois(
        ctx: Context,
        project_key: str,
        agent_name: str,
        registration_token: Optional[str] = None,
        include_recent_commits: bool = True,
        commit_limit: int = 5,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Return enriched profile details for an agent, optionally including recent archive commits.

        Discovery
        ---------
        To discover available agent names, use: resource://agents/{project_key}
        Agent names are NOT the same as program names or user names.

        Parameters
        ----------
        project_key : str
            Project slug or human key.
        agent_name : str
            Agent name to look up (use resource://agents/{project_key} to discover names).
        include_recent_commits : bool
            If true, include latest commits touching the project archive authored by the configured git author.
        commit_limit : int
            Maximum number of recent commits to include.

        Returns
        -------
        dict
            Agent profile augmented with { recent_commits: [{hexsha, summary, authored_ts}] } when requested.
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="whois",
        )
        profile = _agent_to_dict(agent)
        recent: list[dict[str, Any]] = []
        if include_recent_commits:
            archive = await ensure_archive(settings, project.slug)
            repo: Repo = archive.repo
            try:
                # Limit to archive path; extract last commits
                count = max(1, min(50, commit_limit))
                for commit in repo.iter_commits(paths=["."], max_count=count):
                    recent.append(
                        {
                            "hexsha": commit.hexsha[:12],
                            "summary": commit.summary,
                            "authored_ts": _iso(datetime.fromtimestamp(commit.authored_date, tz=timezone.utc)),
                        }
                    )
            except Exception:
                pass
        profile["recent_commits"] = recent
        await ctx.info(f"whois for '{agent_name}' in '{project.human_key}' returned {len(recent)} commits")
        return profile

    @mcp.tool(name="rotate_registration_token")
    @_instrument_tool("rotate_registration_token", cluster=CLUSTER_IDENTITY, capabilities={"identity"}, agent_arg="agent_name", project_arg="project_key")
    async def rotate_registration_token(
        ctx: Context,
        project_key: str,
        agent_name: str,
        registration_token: str,
        new_registration_token: str,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Replace this agent's registration token with a caller-journaled value.

        When to use
        -----------
        - The current token has been exposed: printed to a transcript, pasted
          into a log, echoed by an error. Until 2026-08-14 there was no way to
          answer that, so an exposed token stayed valid forever.

        How it works
        ------------
        - The caller securely generates and journals ``new_registration_token``
          before this RPC. The server compares the current token and writes the
          replacement inside one SQLite ``BEGIN IMMEDIATE`` transaction.
        - Retrying the same old/new pair after an ambiguous disconnect succeeds
          with ``already_current=true`` when the replacement already committed.
        - Existing MCP session and implicit-execution bindings are invalidated.
          Other workers reject them on their next protected call because every
          binding includes the current token's SHA-256 fingerprint.
        - No credential is returned. Use the supported ``agent_mail_setup.sh
          rotate-token`` flow so the private client store and remote CAS recover
          safely across process or network interruption.

        Returns
        -------
        dict
            { "agent": str, "project": str, "rotated": bool,
              "already_current": bool }
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _get_agent(project, agent_name)
        rotated_agent, already_current = await _rotate_agent_registration_token(
            agent,
            project=project,
            registration_token=registration_token,
            new_registration_token=new_registration_token,
        )
        # Purge this worker synchronously after the durable commit. A different
        # worker has its own maps, but its next DB-loaded Agent produces a new
        # fingerprint and therefore cannot match an old binding.
        _invalidate_session_bindings(project, rotated_agent)
        await _touch_agent_activity(rotated_agent)
        await ctx.info(
            f"rotation {'recovered' if already_current else 'committed'} for "
            f"registration token of '{rotated_agent.name}' in '{project.human_key}'"
        )
        return {
            "agent": rotated_agent.name,
            "project": project.human_key,
            "rotated": not already_current,
            "already_current": already_current,
        }

    @mcp.tool(name="create_agent_identity")
    @_instrument_tool("create_agent_identity", cluster=CLUSTER_IDENTITY, capabilities={"identity"}, agent_arg="name_hint", project_arg="project_key")
    async def create_agent_identity(
        ctx: Context,
        project_key: str,
        program: str,
        model: str,
        name_hint: str,
        task_description: str = "",
        attachments_policy: str = "auto",
        display_name: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Create a new, unique agent identity and persist its profile to Git.

        How this differs from `register_agent`
        --------------------------------------
        - Always creates a new durable identity (never updates an existing one).
        - `name_hint` is required, must be a stable explicit identity, and must be available.

        CRITICAL: Agent Naming Rules
        -----------------------------
        - Durable Agent names are explicit stable addresses such as ``codex-wsl-home-1``.
        - Use ``start_agent_execution`` for sessions and temporary workers.

        When to use
        -----------
        - Provisioning a new durable mailbox/persona that must not overwrite an existing profile.

        Returns
        -------
        dict
            { id, name, program, model, task_description, inception_ts, last_active_ts, project_id,
              registration_token? }

        Examples
        --------
        With valid name hint:
        ```json
        {"jsonrpc":"2.0","id":"c1","method":"tools/call","params":{"name":"create_agent_identity","arguments":{
          "project_key":"/data/projects/backend","program":"codex-cli","model":"gpt5-codex","name_hint":"codex-linux-ci-1",
          "task_description":"DB migration spike"
        }}}
        ```

        This provisioning tool returns the newly minted token exactly once.
        Persist it in the client's private credential store before ending the
        session; ordinary register/resume calls never echo an existing token:
        ```json
        {"jsonrpc":"2.0","id":"c3","method":"tools/call","params":{"name":"create_agent_identity","arguments":{
          "project_key":"/data/projects/backend","program":"codex-cli","model":"gpt5",
          "name_hint":"codex-linux-review-1"
        }}}
        ```
        """
        _validate_program_model(program, model)
        if not name_hint.strip():
            raise ToolExecutionError(
                "NAME_REQUIRED",
                "create_agent_identity requires an explicit durable name_hint.",
                recoverable=True,
                data={"field": "name_hint"},
            )
        project = await _get_project_by_identifier(project_key)
        unique_name = await _generate_unique_agent_name(
            project, settings, name_hint.strip()
        )
        # Resolve the archive once and reuse it for the profile publication.
        archive = await ensure_archive(settings, project.slug)
        ap = (attachments_policy or "auto").lower()
        if ap not in {"auto", "inline", "file"}:
            ap = "auto"
        token = secrets.token_urlsafe(32)
        agent = await _create_agent_record(
            project,
            unique_name,
            program,
            model,
            task_description,
            token,
            ap,
            display_name=_sanitize_display_name_argument(display_name),
        )
        try:
            async with _archive_write_lock(archive):
                profile_agent = await _revalidate_agent_profile_lifetime(
                    project=project,
                    agent=agent,
                )
                await write_agent_profile(archive, _agent_to_dict(profile_agent))
            agent = await _activate_provisioned_agent_lifetime(
                project=project,
                agent=agent,
            )
        except Exception:
            # Roll back the DB record so the caller doesn't get an error
            # while the agent already exists in the database (issue #121).
            with suppress(Exception):
                await _rollback_created_agent_lifetime(
                    project=project,
                    agent=agent,
                )
            raise
        with suppress(Exception):
            _bind_session_agent(ctx, project, agent)
        await _ctx_info_safe(
            ctx,
            f"Created new agent identity '{agent.name}' for project '{project.human_key}'.",
        )
        result = _agent_to_dict(agent)
        result["registration_token"] = token
        return result

    @mcp.tool(name="list_window_identities")
    @_instrument_tool(
        "list_window_identities",
        cluster=CLUSTER_IDENTITY,
        capabilities={"identity"},
        project_arg="project_key",
        complexity="low",
    )
    async def list_window_identities(
        ctx: Context,
        project_key: str,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        List active window identities for a project.

        Returns all non-expired window identities with their display names,
        last activity timestamps, and age.

        Parameters
        ----------
        project_key : str
            Project identifier.

        Returns
        -------
        dict
            { identities: [{ id, window_uuid, display_name, created_ts, last_active_ts, expires_ts }] }
        """
        project = await _get_project_by_identifier(project_key)
        await ensure_schema()
        now = _naive_utc()
        async with get_session() as session:
            result = await session.execute(
                select(WindowIdentity).where(
                    cast(Any, WindowIdentity.project_id == project.id),
                    or_(cast(Any, WindowIdentity.expires_ts).is_(None), cast(Any, WindowIdentity.expires_ts) > now),
                )
            )
            identities = result.scalars().all()
        items = []
        for wi in identities:
            items.append({
                "id": wi.id,
                "window_uuid": wi.window_uuid,
                "display_name": wi.display_name,
                "created_ts": _iso(wi.created_ts),
                "last_active_ts": _iso(wi.last_active_ts),
                "expires_ts": _iso(wi.expires_ts) if wi.expires_ts else None,
                "age_days": (now - wi.created_ts).days if wi.created_ts else None,
            })
        return {"identities": items, "count": len(items)}

    @mcp.tool(name="rename_window")
    @_instrument_tool(
        "rename_window",
        cluster=CLUSTER_IDENTITY,
        capabilities={"identity", "write"},
        project_arg="project_key",
    )
    async def rename_window(
        ctx: Context,
        project_key: str,
        window_uuid: str,
        new_display_name: str,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Update the display name of a window identity.

        Parameters
        ----------
        project_key : str
            Project identifier.
        window_uuid : str
            The UUID of the window identity to rename.
        new_display_name : str
            New display name (must be a valid adjective+noun agent name).

        Returns
        -------
        dict
            Updated window identity record.
        """
        if not _validate_window_uuid(window_uuid):
            raise ToolExecutionError(
                "INVALID_WINDOW_UUID",
                f"Invalid window UUID format: '{window_uuid}'.",
                recoverable=True,
            )
        sanitized = sanitize_agent_name(new_display_name)
        if not sanitized or not validate_agent_name_format(sanitized):
            raise ToolExecutionError(
                "INVALID_DISPLAY_NAME",
                f"Display name must be a valid adjective+noun combination (e.g., 'BlueLake'). Got: '{new_display_name}'.",
                recoverable=True,
            )
        project = await _get_project_by_identifier(project_key)
        await ensure_schema()
        now = _naive_utc()
        async with get_session() as session:
            result = await session.execute(
                select(WindowIdentity).where(
                    cast(Any, WindowIdentity.project_id == project.id),
                    cast(Any, func.lower(WindowIdentity.window_uuid) == window_uuid.lower()),
                    or_(cast(Any, WindowIdentity.expires_ts).is_(None), cast(Any, WindowIdentity.expires_ts) > now),
                )
            )
            wi = result.scalars().first()
            if not wi:
                raise ToolExecutionError(
                    "WINDOW_NOT_FOUND",
                    f"No active window identity found for UUID '{window_uuid}'.",
                    recoverable=True,
                )
            old_name = wi.display_name
            wi.display_name = sanitized
            wi.last_active_ts = now
            session.add(wi)
            await session.commit()
            await session.refresh(wi)
        await ctx.info(f"Renamed window '{window_uuid}' from '{old_name}' to '{sanitized}'.")
        return {
            "id": wi.id,
            "window_uuid": wi.window_uuid,
            "display_name": wi.display_name,
            "old_display_name": old_name,
            "last_active_ts": _iso(wi.last_active_ts),
        }

    @mcp.tool(name="expire_window")
    @_instrument_tool(
        "expire_window",
        cluster=CLUSTER_IDENTITY,
        capabilities={"identity", "write"},
        project_arg="project_key",
    )
    async def expire_window(
        ctx: Context,
        project_key: str,
        window_uuid: str,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Mark a window identity as expired.

        Parameters
        ----------
        project_key : str
            Project identifier.
        window_uuid : str
            The UUID of the window identity to expire.

        Returns
        -------
        dict
            { window_uuid, expired: bool, expired_at }
        """
        if not _validate_window_uuid(window_uuid):
            raise ToolExecutionError(
                "INVALID_WINDOW_UUID",
                f"Invalid window UUID format: '{window_uuid}'.",
                recoverable=True,
            )
        project = await _get_project_by_identifier(project_key)
        await ensure_schema()
        now = _naive_utc()
        async with get_session() as session:
            result = await session.execute(
                select(WindowIdentity).where(
                    cast(Any, WindowIdentity.project_id == project.id),
                    cast(Any, func.lower(WindowIdentity.window_uuid) == window_uuid.lower()),
                    or_(cast(Any, WindowIdentity.expires_ts).is_(None), cast(Any, WindowIdentity.expires_ts) > now),
                )
            )
            wi = result.scalars().first()
            if not wi:
                raise ToolExecutionError(
                    "WINDOW_NOT_FOUND",
                    f"No active window identity found for UUID '{window_uuid}'.",
                    recoverable=True,
                )
            wi.expires_ts = now
            session.add(wi)
            await session.commit()
            await session.refresh(wi)
        await ctx.info(f"Expired window identity '{wi.display_name}' ({window_uuid}).")
        return {
            "window_uuid": wi.window_uuid,
            "display_name": wi.display_name,
            "expired": True,
            "expired_at": _iso(now),
        }

    @mcp.tool(name="send_message")
    @_instrument_tool(
        "send_message",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "write"},
        project_arg="project_key",
        agent_arg="sender_name",
    )
    @retry_on_db_lock(max_retries=3, base_delay=0.05, max_delay=0.5)
    async def send_message(
        ctx: Context,
        project_key: str,
        sender_name: str,
        to: list[str],
        subject: str,
        body_md: str,
        idempotency_key: str,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        attachment_paths: Optional[list[str]] = None,
        convert_images: Optional[bool] = None,
        importance: str = "normal",
        ack_required: bool = False,
        thread_id: Optional[str] = None,
        broadcast: bool = False,
        topic: Optional[str] = None,
        auto_contact_if_blocked: Optional[bool] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Send one idempotent Markdown message through atomic delivery.

        Discovery
        ---------
        To discover available agent names for recipients, use: resource://agents/{project_key}
        Agent names are NOT the same as program names or user names.

        What this does
        --------------
        - Accepts one immutable delivery intent under the required caller key
        - Publishes one verified Git document for that delivery
        - Makes the database message visible only after Git publication finalizes
        - Returns a typed delivery status plus the published message, if available

        Parameters
        ----------
        project_key : str
            Project identifier (same used with `ensure_project`/`register_agent`).
        sender_name : str
            Must match an agent registered in the project.
        to : list[str]
            Primary recipients (agent names). At least one of to/cc/bcc must be non-empty.
        subject : str
            Short subject line that will be visible in inbox/outbox and search results.
        body_md : str
            GitHub-Flavored Markdown body.
        idempotency_key : str
            Required 1-128 character operation key. Retry the same key and payload
            to recover the same delivery after an ambiguous disconnect.
        cc, bcc : Optional[list[str]]
            Additional recipients by name.
        attachment_paths : Optional[list[str]]
            Reserved for a future canonical inline attachment representation. Any
            supplied value currently fails before a delivery intent is accepted.
        convert_images : Optional[bool]
            Reserved for future canonical inline normalization. Any supplied value,
            including false, currently fails before intent acceptance.
        importance : str
            One of {"low","normal","high","urgent"} (free form tolerated; used by filters).
        ack_required : bool
            If true, recipients should call `acknowledge_message` after reading.
        thread_id : Optional[str]
            If provided, message will be associated with an existing thread.
        broadcast : bool
            If true and `to` is empty, expand recipients to all registered agents in the
            project (excluding the sender). Mutually exclusive with explicit `to` recipients.
            Respects contact_policy settings — agents with block_all are skipped.
        topic : Optional[str]
            Optional topic tag (max 64 chars). Must start with a letter or digit and may
            otherwise contain alphanumerics, '.', '_', or '-' — so beads_rust hierarchical
            IDs like ``br-abc.1`` can be used verbatim. Stored on the message for topic-based
            filtering via fetch_inbox(topic=...) or fetch_topic().
        auto_contact_if_blocked : Optional[bool]
            When ``True`` (and contact policy blocks delivery to one or more recipients), the
            server will attempt to resolve the block automatically:

            - If the recipient is already authenticated in the **same MCP session**, run
              ``macro_contact_handshake(..., auto_accept=True)`` to approve the link in-band.
              The current send proceeds normally and the message is delivered.
            - Otherwise, fall back to creating a **pending** ``request_contact`` aimed at the
              recipient. This call then **fails loud** with ``CONTACT_REQUIRED`` carrying
              ``auto_contact_requested`` in ``data``. **The message body is not queued** —
              once the recipient approves the contact (``respond_contact(..., accept=True)``),
              the sender must re-call ``send_message`` to actually deliver the payload.

            Defaults to the server-wide ``MESSAGING_AUTO_HANDSHAKE_ON_BLOCK`` setting (true
            unless overridden). The pending-request TTL is governed by
            ``CONTACT_PENDING_TTL_SECONDS`` (default 7 days, separate from the in-session
            auto-approval TTL ``CONTACT_AUTO_TTL_SECONDS``).
        registration_token : Optional[str]
            Durable mailbox credential for ``sender_name``. It may be omitted when
            this MCP session has already authenticated as that Agent.

        Returns
        -------
        dict
            {
              "deliveries": [ { "project": str, "delivery": {...}, "message": {...} | null } ],
              "count": int
            }

        Edge cases
        ----------
        - If no recipients are given, the call fails.
        - Unknown recipient names fail fast. The recipient must self-register, or an
          operator must explicitly provision its durable mailbox, before delivery.
        - Pending/deferred deliveries have `message: null`; use `get_message_delivery`.

        Do / Don't
        ----------
        Do:
        - Keep subjects concise and specific (aim for ≤ 80 characters).
        - Use `thread_id` (or `reply_message`) to keep related discussion in a single thread.
        - Address only relevant recipients; use CC/BCC sparingly and intentionally.
        - Retry with exactly the same idempotency key and canonical payload.

        Don't:
        - Supply attachment options until canonical inline normalization is available.
        - Change topics mid-thread—start a new thread for a new subject.
        - Broadcast to "all" agents unnecessarily—target just the agents who need to act.

        Examples
        --------
        1) Simple message:
        ```json
        {"jsonrpc":"2.0","id":"5","method":"tools/call","params":{"name":"send_message","arguments":{
          "project_key":"/owner/backend","sender_name":"codex-wsl-home-1","to":["claude-linux-ci-1"],
          "subject":"Plan for /api/users","body_md":"See below.",
          "idempotency_key":"plan-users-2026-08-12-01"
        }}}
        ```
        """
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 128:
            raise ToolExecutionError(
                "INVALID_IDEMPOTENCY_KEY",
                "idempotency_key must contain 1-128 non-whitespace characters.",
                recoverable=True,
                data={"argument": "idempotency_key"},
            )
        if attachment_paths is not None or convert_images is not None:
            raise ToolExecutionError(
                "ATTACHMENTS_NOT_SUPPORTED",
                "attachment_paths and convert_images are disabled until attachments "
                "have a bounded canonical inline representation.",
                recoverable=True,
                data={
                    "attachment_paths_provided": attachment_paths is not None,
                    "convert_images_provided": convert_images is not None,
                },
            )

        project = await _get_project_by_identifier(project_key)

        # Validate topic format if provided.
        #
        # Topics must start with an alphanumeric character and may then contain
        # alphanumerics plus '.', '_', '-'. Allowing dots lets agents use
        # beads_rust hierarchical IDs (e.g. ``br-abc.1``) verbatim as topics
        # without mangling. The leading-alphanumeric anchor rejects traversal
        # shapes like ``.``, ``..``, ``../foo`` and dotfiles. Topics are only
        # ever stored as a DB column value, used in an index, and displayed —
        # they are NEVER used to build filesystem paths (thread_id/message_id
        # are the path components) — so dots are safe here, but the anchor is
        # kept as defense-in-depth regardless.
        if topic is not None:
            import re as _re
            topic = topic.strip()
            if not topic or len(topic) > 64 or not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", topic):
                raise ToolExecutionError(
                    "INVALID_TOPIC",
                    "Topic must be 1-64 characters, start with a letter or digit, and "
                    f"contain only alphanumerics, '.', '_', or '-'. Got: {topic!r}",
                    recoverable=True,
                    data={"argument": "topic", "provided": topic},
                )

        # Broadcast expansion: expand to = all agents in project (excluding sender)
        if broadcast:
            if to and any(t.strip() for t in to):
                raise ToolExecutionError(
                    "INVALID_ARGUMENT",
                    "broadcast=true and explicit 'to' recipients are mutually exclusive. "
                    "Set broadcast=true with an empty 'to' list, or provide explicit recipients without broadcast.",
                    recoverable=True,
                    data={"argument": "broadcast"},
                )
            await ensure_schema()
            async with get_session() as _bcast_session:
                _bcast_cutoff = _naive_utc() - timedelta(days=30)
                _bcast_result = await _bcast_session.execute(
                    select(Agent.name, Agent.contact_policy, Agent.retired_at).where(
                        cast(Any, Agent.project_id == project.id),
                        cast(Any, Agent.provisioning_state == "active"),
                        cast(Any, Agent.last_active_ts > _bcast_cutoff),
                    )
                )
                _bcast_rows = _bcast_result.all()
            sender_lower = sender_name.lower().strip()
            to = [
                row[0] for row in _bcast_rows
                if row[0].lower() != sender_lower
                and (row[1] or "auto").lower() != "block_all"
                and row[2] is None  # skip retired agents
            ]
            if not to:
                await ctx.info("[warn] Broadcast: no eligible recipients found (sender is the only active agent).")

        # Normalize 'to' parameter - accept single string and convert to list
        if isinstance(to, str):
            to = [to]
        if not isinstance(to, list):
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "'to' must be a list of durable Agent names (for example, "
                "['claude-linux-ci-1']) or a single Agent name string. "
                f"Received: {type(to).__name__}",
                recoverable=True,
                data={"argument": "to", "received_type": type(to).__name__},
            )

        # Check for common recipient mistakes and provide helpful guidance
        for recipient in to:
            if not isinstance(recipient, str):
                raise ToolExecutionError(
                    "INVALID_ARGUMENT",
                    f"Each recipient in 'to' must be a string (agent name). Got: {type(recipient).__name__}",
                    recoverable=True,
                    data={"argument": "to", "invalid_item": repr(recipient)},
                )
            mistake = _detect_agent_name_mistake(
                _recipient_agent_fragment(recipient)
            )
            if mistake:
                raise ToolExecutionError(
                    mistake[0],
                    f"Invalid recipient '{recipient}': {mistake[1]}",
                    recoverable=True,
                    data={
                        "recipient": recipient,
                        "hint": (
                            "Use a durable client-os-host-slot Agent name, "
                            "not a program or model name."
                        ),
                    },
                )

        # Normalize cc/bcc inputs and validate types for friendlier UX
        if isinstance(cc, str):
            cc = [cc]
        if isinstance(bcc, str):
            bcc = [bcc]
        if cc is not None and not isinstance(cc, list):
            await ctx.error("INVALID_ARGUMENT: cc must be a list of strings or a single string.")
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "cc must be a list of strings or a single string.",
                recoverable=True,
                data={"argument": "cc"},
            )
        if bcc is not None and not isinstance(bcc, list):
            await ctx.error("INVALID_ARGUMENT: bcc must be a list of strings or a single string.")
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "bcc must be a list of strings or a single string.",
                recoverable=True,
                data={"argument": "bcc"},
            )
        if cc is not None and any(not isinstance(x, str) for x in cc):
            await ctx.error("INVALID_ARGUMENT: cc items must be strings (agent names).")
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "cc items must be strings (agent names).",
                recoverable=True,
                data={"argument": "cc"},
            )
        if bcc is not None and any(not isinstance(x, str) for x in bcc):
            await ctx.error("INVALID_ARGUMENT: bcc items must be strings (agent names).")
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "bcc items must be strings (agent names).",
                recoverable=True,
                data={"argument": "bcc"},
            )

        # Reject empty-recipient sends for non-broadcast messages.
        #
        # Without this guard a non-broadcast send with empty to/cc/bcc falls
        # through every downstream step and returns ``count: 0`` while
        # reporting success — silently dropping the message and contradicting
        # the docstring ("If no recipients are given, the call fails."). The
        # broadcast path is intentionally exempt: ``broadcast=true`` with no
        # eligible recipients is a legitimately-empty result (sender is the
        # only active agent) and is already surfaced via ctx.info above. (#189)
        if not broadcast and not any(
            (r or "").strip() for r in ((to or []) + (cc or []) + (bcc or []))
        ):
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "send_message requires at least one recipient in to/cc/bcc "
                "(or broadcast=true).",
                recoverable=True,
                data={"argument": "to"},
            )

        # Self-send detection: warn if sender is sending to themselves
        sender_lower = sender_name.lower().strip()
        all_recipients = (to or []) + (cc or []) + (bcc or [])
        self_send_matches = [r for r in all_recipients if r.lower().strip() == sender_lower]
        if self_send_matches:
            await ctx.info(
                f"[note] You ({sender_name}) are sending a message to yourself. "
                f"This is allowed but usually not intended. To communicate with other agents, "
                "use their durable Agent names (for example, 'claude-linux-ci-1'). To discover agents, "
                f"use resource://agents/{project_key}."
            )

        # Subject length warning: warn if subject is too long (will be truncated in DB)
        if len(subject) > 200:
            await ctx.info(
                f"[warn] Subject is {len(subject)} characters (max recommended: 80, truncated at 200). "
                f"Long subjects may be truncated in search results. Consider moving details to the message body."
            )
            subject = subject[:200]

        thread_id = _validate_thread_id(thread_id)

        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                _rt = _imp.import_module("rich.text")
                Console = _rc.Console
                Panel = _rp.Panel
                Text = _rt.Text
                c = Console()
                title = f"tool: send_message — to={len(to)} cc={len(cc or [])} bcc={len(bcc or [])}"
                body = Text.assemble(
                    ("project: ", "cyan"), (project.human_key, "white"), "\n",
                    ("sender: ", "cyan"), (sender_name, "white"), "\n",
                    ("subject: ", "cyan"), (subject[:120], "white"),
                )
                c.print(Panel(body, title=title, border_style="green"))
            except Exception:
                logger.debug("Failed to log send_message call with rich console", exc_info=True)
        sender = await _authenticate_agent(
            ctx,
            project,
            sender_name,
            registration_token,
            token_param="registration_token",
            action="send_message",
        )
        verified_sender = True
        # Enforce contact policies (per-recipient) with auto-allow heuristics
        settings_local = get_settings()
        if settings_local.contact_enforcement_enabled:
            # allow replies always; if thread present and recipient already on thread, allow
            auto_ok_names: set[str] = set()
            if thread_id:
                try:
                    thread_rows: list[tuple[Message, str, int]]
                    sender_alias = aliased(Agent)
                    # Build criteria: thread_id match or numeric id seed
                    criteria: list[Any] = [cast(Any, Message.thread_id) == thread_id]
                    try:
                        seed_id = int(thread_id)
                        criteria.append(cast(Any, Message.id) == seed_id)
                    except (ValueError, TypeError):
                        pass  # thread_id is not numeric — expected for UUID-style IDs
                    async with get_session() as s:
                        stmt = (
                            select(Message, sender_alias.name, sender_alias.project_id)
                            .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
                            .where(
                                cast(Any, Message.project_id) == project.id,
                                or_(*criteria),
                                _message_visible_to_agent_clause(sender.id or 0),
                            )
                            .limit(500)
                        )
                        thread_rows = [(row[0], row[1], row[2]) for row in (await s.execute(stmt)).all()]
                        # Keep every thread-participant query inside the managed session.
                        participants: set[str] = {
                            n for _m, n, sender_project_id in thread_rows if n and sender_project_id == project.id
                        }
                        message_ids = [m.id for m, _n, _sender_project_id in thread_rows if m.id is not None]
                        if message_ids:
                            recipient_rows = await s.execute(
                                select(Agent.name)
                                .join(MessageRecipient, cast(Any, MessageRecipient.agent_id) == Agent.id)
                                .where(cast(Any, MessageRecipient.message_id).in_(message_ids))
                            )
                            participants.update({row[0] for row in recipient_rows.all() if row[0]})
                    auto_ok_names.update(participants)
                except Exception:
                    logger.exception("Failed to fetch thread participants for contact auto-allow (thread_id=%s)", thread_id)
            # allow recent overlapping file_reservations contact (shared surfaces) by default
            # best-effort: if both agents hold any file_reservation currently active, auto allow
            now_utc = datetime.now(timezone.utc)
            try:
                async with get_session() as s2:
                    file_reservation_rows = await s2.execute(
                        select(FileReservation, Agent.name)
                        .join(Agent, cast(Any, FileReservation.agent_id) == Agent.id)
                        .where(FileReservation.project_id == project.id, cast(Any, FileReservation.released_ts).is_(None), cast(Any, FileReservation.expires_ts) > _naive_utc(now_utc))
                    )
                    name_to_file_reservations: dict[str, list[str]] = {}
                    for c, nm in file_reservation_rows.all():
                        name_to_file_reservations.setdefault(nm, []).append(c.path_pattern)
                sender_file_reservations = name_to_file_reservations.get(sender.name, [])
                for nm in to + (cc or []) + (bcc or []):
                    # Always allow self-messages
                    if nm == sender.name:
                        continue
                    their = name_to_file_reservations.get(nm, [])
                    if sender_file_reservations and their and _file_reservations_patterns_overlap(sender_file_reservations, their):
                        auto_ok_names.add(nm)
            except Exception:
                logger.exception("Failed to check file reservation overlap for contact auto-allow")
            # For each recipient, require link unless policy/open or in auto_ok
            blocked_recipients: list[str] = []
            # Batch-fetch all recipient agents in a single query (eliminates N+1)
            all_recipient_names = list(set(to + (cc or []) + (bcc or [])))
            recipient_agents = await _get_agents_batch_lenient(project, all_recipient_names)
            async with get_session() as s3:
                recent_ok_names: set[str] = set()
                ttl = timedelta(seconds=int(settings_local.contact_auto_ttl_seconds))
                since_dt = now_utc - ttl
                # Batch fetch recent contacts (sender -> recipients and recipients -> sender)
                try:
                    recipient_name_filter = list(all_recipient_names)
                    if recipient_name_filter:
                        sent_stmt = (
                            select(Agent.name)
                            .join(MessageRecipient, cast(Any, MessageRecipient.agent_id) == Agent.id)
                            .join(Message, cast(Any, MessageRecipient.message_id) == Message.id)
                            .where(
                                cast(Any, Message.project_id) == project.id,
                                cast(Any, Message.sender_id) == sender.id,
                                cast(Any, Message.created_ts) > _naive_utc(since_dt),
                                cast(Any, Agent.name).in_(recipient_name_filter),
                            )
                        )
                        sent_rows = await s3.execute(sent_stmt)
                        recent_ok_names.update({row[0] for row in sent_rows.all() if row[0]})

                        sender_alias2 = aliased(Agent)
                        recv_stmt = (
                            select(sender_alias2.name)
                            .join(Message, cast(Any, Message.sender_id) == sender_alias2.id)
                            .join(MessageRecipient, cast(Any, MessageRecipient.message_id) == Message.id)
                            .where(
                                cast(Any, Message.project_id) == project.id,
                                cast(Any, MessageRecipient.agent_id) == sender.id,
                                cast(Any, Message.created_ts) > _naive_utc(since_dt),
                                cast(Any, sender_alias2.name).in_(recipient_name_filter),
                            )
                        )
                        recv_rows = await s3.execute(recv_stmt)
                        recent_ok_names.update({row[0] for row in recv_rows.all() if row[0]})
                except Exception:
                    logger.exception("Failed to batch fetch recent contacts for auto-allow heuristics")
                    recent_ok_names = set()
                # Batch fetch approved agent links for these recipients
                approved_link_ids: set[int] = set()
                try:
                    recipient_ids = [rec.id for rec in recipient_agents.values() if rec is not None and rec.id is not None]
                    if recipient_ids:
                        link_rows = await s3.execute(
                            select(AgentLink.b_agent_id)
                            .where(
                                cast(Any, AgentLink.a_project_id) == project.id,
                                cast(Any, AgentLink.a_agent_id) == sender.id,
                                cast(Any, AgentLink.b_project_id) == project.id,
                                _active_approved_agent_link_clause(now_utc),
                                cast(Any, AgentLink.b_agent_id).in_(recipient_ids),
                            )
                        )
                        approved_link_ids.update({row[0] for row in link_rows.all() if row and row[0] is not None})
                except Exception:
                    logger.exception("Failed to batch fetch approved agent links")
                    approved_link_ids = set()

                # PR #138 Bug 1: also gather names for which this sender has any
                # approved cross-project AgentLink. When a bare name routes to
                # another project (handled later in _route), the local-side
                # contact-policy enforcement must not loud-fail on a same-named
                # local shadow — the cross-project link is the explicit approval.
                cross_project_approved_names: set[str] = set()
                try:
                    xp_link_rows = await s3.execute(
                        select(Agent.name)
                        .join(AgentLink, cast(Any, AgentLink.b_agent_id) == Agent.id)
                        .where(
                            cast(Any, AgentLink.a_project_id) == project.id,
                            cast(Any, AgentLink.a_agent_id) == sender.id,
                            cast(Any, AgentLink.b_project_id) != project.id,
                            _active_approved_agent_link_clause(now_utc),
                        )
                    )
                    for (xp_name,) in xp_link_rows.all():
                        nm_str = (xp_name or "").strip()
                        if not nm_str:
                            continue
                        cross_project_approved_names.add(nm_str.lower())
                        sanitized_xp = sanitize_agent_name(nm_str) or nm_str
                        cross_project_approved_names.add(sanitized_xp.lower())
                except Exception:
                    logger.exception("Failed to batch fetch cross-project agent links for policy bypass")
                    cross_project_approved_names = set()

                for nm in to + (cc or []) + (bcc or []):
                    if nm in auto_ok_names:
                        continue
                    # PR #138 Bug 1: name resolves cross-project via approved link;
                    # the local contact policy is irrelevant since delivery routes
                    # to the other project's recipient (handled in _route below).
                    nm_keys = {(nm or "").strip().lower()}
                    sanitized_nm = sanitize_agent_name(nm or "") or ""
                    if sanitized_nm:
                        nm_keys.add(sanitized_nm.lower())
                    if nm_keys & cross_project_approved_names:
                        continue
                    # recipient lookup (from batch-fetched dict)
                    rec = recipient_agents.get(nm)
                    if rec is None:
                        continue
                    # Reject messages to retired agents
                    if getattr(rec, "retired_at", None) is not None:
                        raise ToolExecutionError(
                            "AGENT_RETIRED",
                            f"Agent '{nm}' is retired and no longer accepts new messages. "
                            "Use unretire_agent to restore it first.",
                            recoverable=True,
                            data={"agent_name": nm, "retired_at": _iso(rec.retired_at)},
                        )
                    rec_policy = getattr(rec, "contact_policy", "auto").lower()
                    # allow self always
                    if rec.name == sender.name:
                        continue
                    if rec_policy == "open":
                        continue
                    if rec_policy == "block_all":
                        await ctx.error("CONTACT_BLOCKED: Recipient is not accepting messages.")
                        raise ToolExecutionError(
                            "CONTACT_BLOCKED",
                            "Recipient is not accepting messages.",
                            recoverable=True,
                        )
                    # contacts_only or auto -> must have approved link or prior contact within TTL
                    recent_ok = rec.name in recent_ok_names
                    if rec_policy == "auto" and recent_ok:
                        continue
                    # check approved AgentLink (local project)
                    if rec.id is not None and rec.id in approved_link_ids:
                        continue
                    # Contact policy must be enforced regardless of ack_required flag.
                    blocked_recipients.append(rec.name)

            if blocked_recipients:
                remedies = [
                    "Call request_contact(project_key, from_agent, to_agent, registration_token=...) to create a pending approval request",
                    "Have the recipient approve it with respond_contact(project_key, to_agent, from_agent, accept=True, registration_token=...)",
                    "Use macro_contact_handshake(..., auto_accept=True, requester_registration_token=..., target_registration_token=...) only when both agents can authenticate in the same MCP session",
                ]
                auto_requested: list[str] = []
                auto_approved: list[str] = []
                # Respect explicit flag or server default ergonomics
                effective_auto_contact = (
                    bool(getattr(settings_local, "messaging_auto_handshake_on_block", True))
                    if auto_contact_if_blocked is None
                    else auto_contact_if_blocked
                )
                if effective_auto_contact:
                    try:
                        for nm in list(dict.fromkeys(blocked_recipients)):
                            rec = recipient_agents.get(nm)
                            if rec is None:
                                continue
                            try:
                                if _session_is_bound_to_agent(ctx, project, rec):
                                    await macro_contact_handshake(
                                        ctx=ctx,
                                        project_key=project.human_key,
                                        requester=sender.name,
                                        target=nm,
                                        reason="in-session auto-approval by send_message",
                                        auto_accept=True,
                                        ttl_seconds=int(settings_local.contact_auto_ttl_seconds),
                                        format="json",
                                    )
                                    auto_approved.append(nm)
                                else:
                                    # Pending fallback path — async human may take days to approve,
                                    # so use the longer pending TTL (default 7 days) rather than
                                    # the in-session auto-approval TTL (default 24h).
                                    await request_contact(
                                        ctx=ctx,
                                        project_key=project.human_key,
                                        from_agent=sender.name,
                                        to_agent=nm,
                                        reason="auto contact request created by send_message",
                                        ttl_seconds=int(settings_local.contact_pending_ttl_seconds),
                                        format="json",
                                    )
                                    auto_requested.append(nm)
                            except Exception:
                                logger.exception("Failed to auto-resolve contact for recipient %r", nm)

                        if settings_local.contact_auto_retry_enabled and auto_approved:
                            blocked_recipients = []
                            # Re-fetch recipient agents in batch for re-evaluation
                            recipient_agents_retry = await _get_agents_batch_lenient(project, all_recipient_names)
                            async with get_session() as s3b:
                                for nm in to + (cc or []) + (bcc or []):
                                    rec = recipient_agents_retry.get(nm)
                                    if rec is None:
                                        continue
                                    if rec.name == sender.name:
                                        continue
                                    rec_policy = getattr(rec, "contact_policy", "auto").lower()
                                    if rec_policy == "open":
                                        continue
                                    # After auto-approval, link should exist; double-check
                                    link = await s3b.execute(
                                        select(AgentLink)
                                        .where(
                                            cast(Any, AgentLink.a_project_id) == project.id,
                                            cast(Any, AgentLink.a_agent_id) == sender.id,
                                            cast(Any, AgentLink.b_project_id) == project.id,
                                            cast(Any, AgentLink.b_agent_id) == rec.id,
                                            _active_approved_agent_link_clause(),
                                        )
                                        .limit(1)
                                    )
                                    if link.first() is None:
                                        blocked_recipients.append(rec.name)
                    except Exception:
                        logger.exception("Failed to auto-resolve contacts or re-evaluate recipients after in-session approvals")
                if blocked_recipients:
                    err_type: str = "CONTACT_REQUIRED"
                    blocked_sorted = sorted(set(blocked_recipients))
                    recipient_list = ", ".join(blocked_sorted)
                    sample_target = blocked_sorted[0]
                    project_expr = repr(project.human_key)
                    sender_expr = repr(sender.name)
                    target_expr = repr(sample_target)
                    err_msg_parts = [
                        f"Contact approval required for recipients: {recipient_list}.",
                        (
                            "Before retrying, create a pending request with "
                            f"`request_contact(project_key={project_expr}, from_agent={sender_expr}, "
                            f"to_agent={target_expr})`, then have the recipient approve it with "
                            f"`respond_contact(project_key={project_expr}, to_agent={target_expr}, "
                            f"from_agent={sender_expr}, accept=True)`."
                        ),
                        "Alternatively, send your message inside a recent thread that already includes them by reusing its thread_id.",
                    ]
                    if auto_requested:
                        err_msg_parts.append(
                            "Pending contact requests were created for: "
                            + ", ".join(sorted(set(auto_requested)))
                            + ". Wait for approval before retrying."
                        )
                    if auto_approved:
                        err_msg_parts.append(
                            "In-session auto-approvals already ran for: "
                            + ", ".join(sorted(set(auto_approved)))
                            + ". Any remaining blocked recipients still need explicit approval."
                        )
                    err_msg: str = " ".join(err_msg_parts)
                    err_data: dict[str, Any] = {
                        "recipients_blocked": sorted(set(blocked_recipients)),
                        "remedies": remedies,
                        "auto_contact_requested": sorted(set(auto_requested)),
                        "auto_contact_auto_approved": sorted(set(auto_approved)),
                    }
                    # Provide actionable sample calls
                    try:
                        if blocked_recipients:
                            examples: list[dict[str, Any]] = []
                            for nm in blocked_recipients[:3]:
                                examples.append(
                                    {
                                        "tool": "request_contact",
                                        "arguments": {
                                            "project_key": project.human_key,
                                            "from_agent": sender.name,
                                            "to_agent": nm,
                                            "ttl_seconds": int(settings_local.contact_pending_ttl_seconds),
                                        },
                                    }
                                )
                            err_data["suggested_tool_calls"] = examples
                    except Exception:
                        logger.exception("Failed to build suggestion examples for blocked recipients")
                    await ctx.error(f"{err_type}: {err_msg}")
                    raise ToolExecutionError(
                        err_type,
                        err_msg,
                        recoverable=True,
                        data=err_data,
                    )
        # Split recipients into local vs external (approved links)
        local_to: list[str] = []
        local_cc: list[str] = []
        local_bcc: list[str] = []
        external: dict[int, dict[str, Any]] = {}
        thread_external_participants = (
            await _get_thread_external_participants(project, sender, thread_id)
            if thread_id
            else {}
        )

        async with get_session() as sx:
            # Preload local agent names (normalized -> canonical stored name)
            existing = await sx.execute(
                select(Agent.name).where(
                    Agent.project_id == project.id,
                    Agent.provisioning_state == "active",
                )
            )
            local_lookup: dict[str, str] = {}
            for row in existing.fetchall():
                canonical_name = (row[0] or "").strip()
                if not canonical_name:
                    continue
                sanitized_canonical = sanitize_agent_name(canonical_name) or canonical_name
                for key in {canonical_name.lower(), sanitized_canonical.lower()}:
                    local_lookup.setdefault(key, canonical_name)

            # PR #138 Bug 1 fix: pre-fetch approved CROSS-project AgentLinks for
            # this sender. When a bare recipient name has BOTH a local agent and
            # an approved cross-project link (e.g. a stale shadow agent left
            # over from a prior auto_contact_if_blocked + handshake cycle), we
            # prefer the cross-project route — the explicit prior approval is
            # the load-bearing signal of intent, and silently delivering to a
            # local shadow has caused message loss in production.
            #
            # Only cross-project links go in this lookup. Same-project links
            # are handled by the existing local-resolution path (and the
            # existing AgentLink-based fallback further down) — including them
            # here would cause messages to land in the `external` bucket keyed
            # by the sender's own project, which downstream code does not
            # expect.
            agent_link_lookup: dict[str, tuple[Project, Agent]] = {}
            if sender.id is not None and project.id is not None:
                link_rows = await sx.execute(
                    select(AgentLink, Project, Agent)
                    .join(Project, Project.id == AgentLink.b_project_id)
                    .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                    .where(
                        cast(Any, AgentLink.a_project_id) == project.id,
                        cast(Any, AgentLink.a_agent_id) == sender.id,
                        cast(Any, AgentLink.b_project_id) != project.id,
                        _active_approved_agent_link_clause(),
                    )
                )
                for _link, b_project, b_agent in link_rows.all():
                    canonical = (b_agent.name or "").strip()
                    if not canonical:
                        continue
                    sanitized_b = sanitize_agent_name(canonical) or canonical
                    for key in {canonical.lower(), sanitized_b.lower()}:
                        agent_link_lookup.setdefault(key, (b_project, b_agent))

            sender_candidate_keys = {
                key.lower()
                for key in (
                    (sender.name or "").strip(),
                    sanitize_agent_name(sender.name or "") or "",
                )
                if key
            }

            def _normalize(value: str) -> tuple[str, set[str], Optional[str]]:
                """Trim input, derive comparable lowercase keys, and canonical lookup token."""
                trimmed = (value or "").strip()
                sanitized = sanitize_agent_name(trimmed)
                keys: set[str] = set()
                if trimmed:
                    keys.add(trimmed.lower())
                if sanitized:
                    keys.add(sanitized.lower())
                # Preserve the exact durable identity for DB lookup.  The
                # sanitized spelling is only an alias candidate; notably,
                # sanitize_agent_name() removes hyphens from canonical
                # client-os-host-slot names.
                canonical = trimmed if trimmed else None
                return trimmed or value, keys, canonical

            unknown_local: dict[str, set[str]] = defaultdict(set)
            unknown_external: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

            class _ContactBlocked(Exception):
                pass

            async def _route(name_list: list[str], kind: str) -> None:
                for raw in name_list:
                    candidate = raw or ""
                    explicit_override = False
                    target_project_override: Project | None = None
                    target_project_label: str | None = None
                    agent_fragment = candidate

                    # Explicit external addressing: project:<slug-or-key>#<AgentName>
                    if candidate.startswith("project:") and "#" in candidate:
                        explicit_override = True
                        parsed_project_label: str | None = None
                        try:
                            _, rest = candidate.split(":", 1)
                            slug_part, agent_part = rest.split("#", 1)
                            parsed_project_label = slug_part.strip() or None
                            target_project_override = await _get_project_by_identifier(parsed_project_label or "")
                            target_project_label = target_project_override.human_key or target_project_override.slug
                            agent_fragment = agent_part
                        except Exception:
                            logger.debug("Failed to parse explicit external address: %s", candidate, exc_info=True)
                            label = parsed_project_label or "(invalid project)"
                            unknown_external[label][candidate.strip() or candidate].add(kind)
                            continue

                    # Alternate explicit format: <AgentName>@<project-identifier>
                    if not explicit_override and "@" in candidate:
                        name_part, project_part = candidate.split("@", 1)
                        if name_part.strip() and project_part.strip():
                            try:
                                target_project_override = await _get_project_by_identifier(project_part.strip())
                                target_project_label = target_project_override.human_key or target_project_override.slug
                                agent_fragment = name_part
                                explicit_override = True
                            except Exception:
                                logger.debug("Failed to resolve external project %r for %r", project_part.strip(), name_part, exc_info=True)
                                label = project_part.strip() or "(invalid project)"
                                unknown_external[label][candidate.strip() or candidate].add(kind)
                                continue

                    display_value, key_candidates, canonical = _normalize(agent_fragment)
                    if not key_candidates or not canonical:
                        if explicit_override:
                            label = target_project_label or "(unknown project)"
                            unknown_external[label][candidate.strip() or candidate].add(kind)
                        else:
                            unknown_local[candidate.strip() or candidate].add(kind)
                        continue

                    # Always allow self-send (local context only)
                    if not explicit_override and sender_candidate_keys.intersection(key_candidates):
                        if kind == "to":
                            local_to.append(sender.name)
                        elif kind == "cc":
                            local_cc.append(sender.name)
                        else:
                            local_bcc.append(sender.name)
                        continue

                    if not explicit_override:
                        resolved_local = None
                        for key in key_candidates:
                            resolved_local = local_lookup.get(key)
                            if resolved_local:
                                break
                        cross_link_match: tuple[Project, Agent] | None = None
                        for key in key_candidates:
                            cross_link_match = agent_link_lookup.get(key)
                            if cross_link_match:
                                break
                        if cross_link_match is not None:
                            # PR #138 Bug 1: an approved cross-project AgentLink
                            # is the load-bearing signal of intent. Prefer it
                            # over both a (possibly stale-shadow) local agent
                            # and the DB-side AgentLink fallback below — that
                            # fallback only matches `func.lower(Agent.name) ==
                            # canonical.lower()` and would miss legacy / non-
                            # sanitized agent names that the pre-fetch finds
                            # via the alternate sanitized-form key.
                            target_project_xp, target_agent_xp = cross_link_match
                            pol = (getattr(target_agent_xp, "contact_policy", "auto") or "auto").lower()
                            if pol == "block_all":
                                await ctx.error("CONTACT_BLOCKED: Recipient is not accepting messages.")
                                raise _ContactBlocked()
                            bucket = external.setdefault(
                                target_project_xp.id or 0,
                                {"project": target_project_xp, "to": [], "cc": [], "bcc": []},
                            )
                            bucket[kind].append(target_agent_xp.name)
                            continue
                        if resolved_local:
                            # Local-only (no cross-project link): route locally as before.
                            if kind == "to":
                                local_to.append(resolved_local)
                            elif kind == "cc":
                                local_cc.append(resolved_local)
                            else:
                                local_bcc.append(resolved_local)
                            continue

                    lookup_value = canonical.lower()
                    rows = None
                    if explicit_override and target_project_override is not None:
                        rows = await sx.execute(
                            select(AgentLink, Project, Agent)
                            .join(Project, Project.id == AgentLink.b_project_id)
                            .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                            .where(
                                cast(Any, AgentLink.a_project_id) == project.id,
                                cast(Any, AgentLink.a_agent_id) == sender.id,
                                _active_approved_agent_link_clause(),
                                cast(Any, Project.id == target_project_override.id),
                                cast(Any, func.lower(Agent.name) == lookup_value),
                            )
                            .limit(1)
                        )
                    else:
                        rows = await sx.execute(
                            select(AgentLink, Project, Agent)
                            .join(Project, Project.id == AgentLink.b_project_id)
                            .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                            .where(
                                cast(Any, AgentLink.a_project_id) == project.id,
                                cast(Any, AgentLink.a_agent_id) == sender.id,
                                _active_approved_agent_link_clause(),
                                cast(Any, func.lower(Agent.name) == lookup_value),
                            )
                            .limit(1)
                        )

                    rec = rows.first() if rows else None
                    if rec:
                        _link, target_project, target_agent = rec
                        pol = (getattr(target_agent, "contact_policy", "auto") or "auto").lower()
                        if pol == "block_all":
                            await ctx.error("CONTACT_BLOCKED: Recipient is not accepting messages.")
                            raise _ContactBlocked()
                        bucket = external.setdefault(
                            target_project.id or 0,
                            {"project": target_project, "to": [], "cc": [], "bcc": []},
                        )
                        bucket[kind].append(target_agent.name)
                        continue

                    if explicit_override and target_project_override is not None:
                        thread_participant = thread_external_participants.get(
                            (target_project_override.id or 0, lookup_value)
                        )
                        if thread_participant is not None:
                            target_project, participant_name = thread_participant
                            bucket = external.setdefault(
                                target_project.id or 0,
                                {"project": target_project, "to": [], "cc": [], "bcc": []},
                            )
                            bucket[kind].append(participant_name)
                            continue

                    if explicit_override:
                        label = target_project_label or "(unknown project)"
                        unknown_external[label][display_value or candidate.strip() or candidate].add(kind)
                    else:
                        unknown_local[display_value or candidate.strip() or candidate].add(kind)

            try:
                await _route(to, "to")
                await _route(cc or [], "cc")
                await _route(bcc or [], "bcc")
            except _ContactBlocked as err:
                raise ToolExecutionError(
                    "CONTACT_BLOCKED",
                    "Recipient is not accepting messages.",
                    recoverable=True,
                ) from err

            if unknown_local or unknown_external:
                # Attempt cross-project handshakes for unknown external recipients if allowed
                approved_external_routes: list[tuple[str, str]] = []
                attempted_external: list[str] = []
                requested_external: list[str] = []
                try:
                    effective_auto_contact = (
                        bool(getattr(settings_local, "messaging_auto_handshake_on_block", True))
                        if auto_contact_if_blocked is None
                        else auto_contact_if_blocked
                    )
                    if effective_auto_contact and unknown_external:
                        # Iterate over a copy since we may mutate/resolve entries
                        for label, pending_names in list(unknown_external.items()):
                            try:
                                target_proj = await _get_project_by_identifier(label)
                            except Exception:
                                logger.debug("Failed to resolve external project %r for handshake", label, exc_info=True)
                                continue
                            target_project_ref = target_proj.human_key or target_proj.slug or label
                            for nm, route_kinds in list(pending_names.items()):
                                display_target = f"{nm}@{target_project_ref}"
                                try:
                                    target_agent = await _find_agent_optional(target_proj, nm)
                                    if target_agent is not None and _session_is_bound_to_agent(ctx, target_proj, target_agent):
                                        await macro_contact_handshake(
                                            ctx=ctx,
                                            project_key=project.human_key,
                                            requester=sender.name,
                                            target=nm,
                                            to_project=target_proj.human_key or target_proj.slug,
                                            reason="in-session auto-approval by send_message",
                                            auto_accept=True,
                                            ttl_seconds=int(settings_local.contact_auto_ttl_seconds),
                                            format="json",
                                        )
                                        attempted_external.append(display_target)
                                        for route_kind in sorted(route_kinds):
                                            approved_external_routes.append((display_target, route_kind))
                                    else:
                                        # Pending fallback path — async human may take days to approve,
                                        # so use the longer pending TTL (default 7 days) rather than
                                        # the in-session auto-approval TTL (default 24h).
                                        await request_contact(
                                            ctx=ctx,
                                            project_key=project.human_key,
                                            from_agent=sender.name,
                                            to_agent=nm,
                                            to_project=target_proj.human_key or target_proj.slug,
                                            reason="auto contact request created by send_message",
                                            ttl_seconds=int(settings_local.contact_pending_ttl_seconds),
                                            format="json",
                                        )
                                        requested_external.append(display_target)
                                except Exception:
                                    logger.exception("Failed to auto-resolve contact for external recipient %r@%r", nm, label)
                        # Re-route any that were approved in-session
                        if approved_external_routes:
                            from contextlib import suppress
                            with suppress(_ContactBlocked):
                                for item, route_kind in approved_external_routes:
                                    await _route([item], route_kind)
                            # Purge unknown_external entries that now have approved links
                            try:
                                async with get_session() as scheck:
                                    for label, pending_names in list(unknown_external.items()):
                                        try:
                                            tproj = await _get_project_by_identifier(label)
                                        except Exception:
                                            logger.debug("Failed to verify approved links for project %r", label, exc_info=True)
                                            continue
                                        remaining: dict[str, set[str]] = {}
                                        for nm, route_kinds in list(pending_names.items()):
                                            lookup_value = (nm or "").strip().lower()
                                            rows = await scheck.execute(
                                                select(AgentLink, Project, Agent)
                                                .join(Project, Project.id == AgentLink.b_project_id)
                                                .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                                                .where(
                                                    cast(Any, AgentLink.a_project_id) == project.id,
                                                    cast(Any, AgentLink.a_agent_id) == sender.id,
                                                    _active_approved_agent_link_clause(),
                                                    cast(Any, Project.id == tproj.id),
                                                    cast(Any, func.lower(Agent.name) == lookup_value),
                                                )
                                                .limit(1)
                                            )
                                            if rows.first() is None:
                                                remaining[nm] = route_kinds
                                        if remaining:
                                            unknown_external[label] = remaining
                                        else:
                                            unknown_external.pop(label, None)
                            except Exception:
                                logger.exception("Failed to purge resolved unknown_external entries after in-session approvals")
                except Exception:
                    logger.exception("Failed to auto-resolve contact for unknown external recipients")
                # If everything resolved after auto-actions, skip error path
                still_unknown = bool(unknown_local) or any(v for v in unknown_external.values())
                if not still_unknown:
                    # All unknowns were resolved; continue to delivery
                    pass
                else:
                    parts: list[str] = []
                data_payload: dict[str, Any] = {}
                if still_unknown and unknown_local:
                    missing_local = sorted({name for name in unknown_local if name})
                    parts.append(
                        f"local recipients {', '.join(missing_local)} are not registered in project '{project.human_key}'"
                    )
                    data_payload["unknown_local"] = missing_local
                if still_unknown and unknown_external:
                    formatted_external = {
                        label: sorted({name for name in names if name})
                        for label, names in unknown_external.items()
                    }
                    ext_parts = [
                        f"{', '.join(names)} @ {label}"
                        for label, names in sorted(formatted_external.items())
                        if names
                    ]
                    if ext_parts:
                        parts.append(
                            "external recipients missing approved contact links: " + "; ".join(ext_parts)
                        )
                    data_payload["unknown_external"] = formatted_external
                # Include auto actions we tried
                if still_unknown and attempted_external:
                    data_payload["auto_contact_attempted_external"] = attempted_external
                if still_unknown and requested_external:
                    data_payload["auto_contact_requested_external"] = requested_external
                if still_unknown:
                    hint_parts = [
                        f"Use resource://agents/{project.slug} to list registered agents."
                    ]
                    required_actions: list[str] = []
                    if unknown_local:
                        hint_parts.append(
                            "A missing local recipient must self-register, or an "
                            "operator must explicitly provision its durable mailbox, "
                            "before delivery."
                        )
                        required_actions.append(
                            "target_self_register_or_operator_provision"
                        )
                    if unknown_external:
                        hint_parts.append(
                            "Verify each external target is already registered in its "
                            "own project, then request contact; request_contact never "
                            "provisions the target mailbox."
                        )
                        required_actions.append(
                            "verify_target_registration_then_request_contact"
                        )
                    hint = " ".join(hint_parts)
                    if requested_external:
                        parts.append(
                            "pending external contact requests were created for "
                            + ", ".join(sorted(set(requested_external)))
                        )
                    parts.append(hint)
                    message = "Unable to send message — " + "; ".join(parts)
                    data_payload["hint"] = hint
                    data_payload["required_actions"] = required_actions
                    await ctx.error(f"RECIPIENT_NOT_FOUND: {message}")
                    raise ToolExecutionError(
                        "RECIPIENT_NOT_FOUND",
                        message,
                        recoverable=True,
                        data=data_payload,
                    )

        deliveries: list[dict[str, Any]] = []
        delivery_errors: list[dict[str, Any]] = []
        # Local deliver if any
        if local_to or local_cc or local_bcc:
            payload_local = await _deliver_message(
                ctx,
                "send_message",
                project,
                sender,
                local_to,
                local_cc,
                local_bcc,
                subject,
                body_md,
                attachment_paths,
                convert_images,
                importance,
                ack_required,
                thread_id,
                idempotency_key,
                topic=topic,
            )
            _collect_delivery_result(deliveries, delivery_errors, project, payload_local)
        # External per-target project deliver using the original sender identity.
        for _pid, group in external.items():
            p: Project = group["project"]
            try:
                payload_ext = await _deliver_message(
                    ctx,
                    "send_message",
                    p,
                    sender,
                    group.get("to", []),
                    group.get("cc", []),
                    group.get("bcc", []),
                    subject,
                    body_md,
                    attachment_paths,
                    convert_images,
                    importance,
                    ack_required,
                    thread_id,
                    idempotency_key,
                    topic=topic,
                )
                _collect_delivery_result(deliveries, delivery_errors, p, payload_ext)
            except Exception as exc:
                logger.exception("Failed to deliver message to external project %r", p.human_key)
                delivery_errors.append(_delivery_failure_from_exception(p, exc))
                continue

        if not deliveries and delivery_errors:
            return {
                "error": _summarize_delivery_failures(
                    delivery_errors,
                    summary_message="Message delivery failed for all target projects.",
                )
            }
        result: dict[str, Any] = {"deliveries": deliveries, "count": len(deliveries), "verified_sender": verified_sender}
        if delivery_errors:
            result["delivery_errors"] = delivery_errors
        return result

    @mcp.tool(name="get_message_delivery")
    @_instrument_tool(
        "get_message_delivery",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "read"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def get_message_delivery(
        ctx: Context,
        project_key: str,
        agent_name: str,
        delivery_id: str,
        retry_pending: bool = False,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Read an authorized delivery status and optionally retry due work.

        The caller must be the exact sender lifetime or one of the exact target
        recipient lifetimes. ``retry_pending`` is restricted to the sender; it
        never creates a new intent and is safe after an ambiguous disconnect.
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="get_message_delivery",
        )
        if agent.id is None:
            raise RuntimeError("Authenticated agent lifetime is incomplete.")

        async with get_session() as session:
            delivery = await session.get(MessageDelivery, delivery_id)
            sender_authorized = bool(
                delivery is not None
                and delivery.sender_id == agent.id
                and delivery.sender_generation_snapshot == agent.agent_generation
                and delivery.sender_project_id_snapshot == project.id
                and delivery.sender_project_generation_snapshot
                == project.project_generation
            )
            recipient_authorized = False
            if (
                delivery is not None
                and delivery.project_id == project.id
                and delivery.project_generation_snapshot == project.project_generation
            ):
                recipient_result = await session.execute(
                    select(MessageDeliveryRecipient.delivery_id).where(
                        cast(Any, MessageDeliveryRecipient.delivery_id == delivery.id),
                        cast(Any, MessageDeliveryRecipient.agent_id == agent.id),
                        cast(
                            Any,
                            MessageDeliveryRecipient.agent_generation_snapshot
                            == agent.agent_generation,
                        ),
                        cast(
                            Any,
                            MessageDeliveryRecipient.project_id_snapshot == project.id,
                        ),
                    )
                )
                recipient_authorized = recipient_result.first() is not None

            if delivery is None or not (sender_authorized or recipient_authorized):
                raise ToolExecutionError(
                    "NOT_FOUND",
                    f"Message delivery '{delivery_id}' was not found.",
                    recoverable=True,
                    data={"delivery_id": delivery_id},
                )
            request_sha256 = delivery.request_sha256
            document_sha256 = delivery.document_sha256
            target_project = await session.get(Project, delivery.project_id)
            if (
                target_project is None
                or target_project.project_generation
                != delivery.project_generation_snapshot
            ):
                raise RuntimeError("Delivery target project lifetime is unavailable.")
            target_project_key = target_project.human_key

        if retry_pending:
            if not sender_authorized:
                raise ToolExecutionError(
                    "FORBIDDEN",
                    "Only the authenticated sender may retry a pending delivery.",
                    recoverable=False,
                )
            processing = await process_message_delivery(delivery_id)
            if processing.published_now:
                await emit_published_delivery_notifications(delivery_id)
        else:
            processing = await get_message_delivery_status(delivery_id)

        message_payload: dict[str, Any] | None = None
        if processing.status == "published" and processing.message_id is not None:
            async with get_session() as session:
                message = await session.get(Message, processing.message_id)
            if message is not None and message.delivery_id == delivery_id:
                message_payload = _message_to_dict(message)

        return {
            "project": target_project_key,
            "delivery": _delivery_status_payload(
                processing,
                reused=None,
                request_sha256=request_sha256,
                document_sha256=document_sha256,
            ),
            "message": message_payload,
        }

    @mcp.tool(
        name="purge_old_messages",
        description="Delete messages older than the configured retention period. "
        "Defaults to retention_max_age_days from config (180 days). "
        "Returns count of messages purged.",
    )
    @_instrument_tool(
        "purge_old_messages",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "write"},
        project_arg="project_key",
    )
    async def purge_old_messages(
        ctx: Context,
        project_key: str,
        max_age_days: Optional[int] = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Purge messages older than max_age_days."""
        project = await _get_project_by_identifier(project_key)
        if not project:
            raise ValueError(f"Project '{project_key}' not found")

        age_limit = max_age_days if max_age_days is not None else settings.retention_max_age_days
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_limit)

        async with get_session() as session:
            pending_reply_targets = (
                select(MessageDelivery.reply_to_message_id)
                .where(
                    cast(Any, MessageDelivery.state) == "pending",
                    cast(Any, MessageDelivery.reply_to_message_id).is_not(None),
                )
                .scalar_subquery()
            )
            stale_filter = [
                Message.project_id == project.id,
                Message.created_ts < cutoff,
                cast(Any, Message.id).not_in(pending_reply_targets),
            ]
            count_result = await session.execute(
                select(func.count()).select_from(Message).where(*stale_filter)
            )
            count = count_result.scalar() or 0

            if not dry_run and count > 0:
                stale_ids = select(Message.id).where(*stale_filter).scalar_subquery()
                # Retained replies remain grouped by thread_id but lose the
                # direct edge to the deleted message lifetime.
                await session.execute(
                    update(Message)
                    .where(
                        cast(Any, Message.reply_to).in_(stale_ids),
                        cast(Any, Message.id).not_in(stale_ids),
                    )
                    .values(reply_to=None)
                )
                await session.execute(
                    delete(MessageRecipient).where(
                        cast(Any, MessageRecipient.message_id).in_(stale_ids)
                    )
                )
                await session.flush()
                await session.execute(
                    delete(Message).where(cast(Any, Message.id).in_(stale_ids))
                )
                await session.flush()
                await session.commit()

        status = "purged" if not dry_run else "dry_run"
        await ctx.info(f"purge_old_messages: {status}, {count} messages affected (cutoff={cutoff.isoformat()})")
        return {
            "status": status,
            "messages_affected": count,
            "cutoff_date": cutoff.isoformat(),
            "max_age_days": age_limit,
        }

    @mcp.tool(name="reply_message")
    @_instrument_tool(
        "reply_message",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "write"},
        project_arg="project_key",
        agent_arg="sender_name",
    )
    async def reply_message(
        ctx: Context,
        project_key: str,
        message_id: int,
        sender_name: str,
        body_md: str,
        idempotency_key: str,
        to: Optional[list[str]] = None,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        subject_prefix: str = "Re:",
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Reply to an existing message, preserving or establishing a thread.

        Behavior
        --------
        - Inherits original `importance` and `ack_required` flags
        - `thread_id` is taken from the original message if present; otherwise, the original id is used
        - Subject is prefixed with `subject_prefix` if not already present
        - Defaults `to` to the original sender if not explicitly provided
        - Uses an exact thread-scoped return route for external participants; it
          never creates a reverse general-purpose contact approval

        Parameters
        ----------
        project_key : str
            Project identifier.
        message_id : int
            The id of the message you are replying to.
        sender_name : str
            Your agent name (must be registered in the project).
        body_md : str
            Reply body in Markdown.
        idempotency_key : str
            Required 1-128 character operation key. Retry the same key and payload
            to recover the same delivery.
        to, cc, bcc : Optional[list[str]]
            Recipients by agent name. If omitted, `to` defaults to original sender.
        subject_prefix : str
            Prefix to apply (default "Re:"). Case-insensitive idempotent.
        registration_token : Optional[str]
            Durable mailbox credential for ``sender_name``. It may be omitted when
            this MCP session has already authenticated as that Agent.

        Do / Don't
        ----------
        Do:
        - Keep the subject focused; avoid topic drift within a thread.
        - Reply to the original sender unless new stakeholders are strictly required.
        - Preserve importance/ack flags from the original unless there is a clear reason to change.
        - Use CC for FYI only; BCC sparingly and with intention.

        Don't:
        - Change `thread_id` when continuing the same discussion.
        - Escalate to many recipients; prefer targeted replies and start a new thread for new topics.
        - Attempt attachment mutation; replies accept the Markdown body only.

        Returns
        -------
        dict
            Thread metadata plus ``deliveries[]`` entries shaped as
            ``{project, delivery, message}``. Message is null until published.

        Examples
        --------
        Minimal reply to original sender:
        If the caller has not already authenticated as `sender_name` in this MCP session,
        include `registration_token`.

        ```json
        {"jsonrpc":"2.0","id":"6","method":"tools/call","params":{"name":"reply_message","arguments":{
          "project_key":"/owner/backend","message_id":1234,"sender_name":"codex-wsl-home-1",
          "body_md":"Questions about the migration plan...",
          "idempotency_key":"reply-1234-01","registration_token":"<registration_token>"
        }}}
        ```

        Reply with explicit recipients and CC:
        ```json
        {"jsonrpc":"2.0","id":"6c","method":"tools/call","params":{"name":"reply_message","arguments":{
          "project_key":"/owner/backend","message_id":1234,"sender_name":"codex-wsl-home-1",
          "body_md":"Looping ops.","to":["claude-linux-ci-1"],"cc":["gemini-linux-qa-1"],"subject_prefix":"RE:",
          "idempotency_key":"reply-1234-02","registration_token":"<registration_token>"
        }}}
        ```
        """
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 128:
            raise ToolExecutionError(
                "INVALID_IDEMPOTENCY_KEY",
                "idempotency_key must contain 1-128 non-whitespace characters.",
                recoverable=True,
                data={"argument": "idempotency_key"},
            )

        project = await _get_project_by_identifier(project_key)
        sender = await _authenticate_agent(
            ctx,
            project,
            sender_name,
            registration_token,
            token_param="registration_token",
            action="reply_message",
        )
        settings_local = get_settings()
        original = await _get_visible_message(project, sender, message_id)
        original_sender = await _get_agent_any_project_by_id(original.sender_id)
        original_sender_project = await _get_project_by_id(original_sender.project_id)
        thread_key = original.thread_id or str(original.id)
        subject_prefix_clean = subject_prefix.strip()
        base_subject = original.subject
        if subject_prefix_clean and base_subject.lower().startswith(subject_prefix_clean.lower()):
            reply_subject = base_subject
        else:
            reply_subject = f"{subject_prefix_clean} {base_subject}".strip()
        # When replying to your own outbound message, default `to` to the
        # original recipients instead of yourself (avoids self-reply loop).
        if to is None and original.sender_id == sender.id:
            async with get_session() as _rsl_sx:
                _rsl_result = await _rsl_sx.execute(
                    # Use the local ``select`` wrapper (not ``_sa_select``) so ty
                    # doesn't trip over SQLAlchemy's multi-entity overloads on
                    # SQLModel-mapped columns. See the wrapper definition above.
                    select(Agent.name, Agent.project_id, MessageRecipient.kind)
                    .join(Agent, MessageRecipient.agent_id == Agent.id)
                    .where(
                        cast(Any, MessageRecipient.message_id) == original.id,
                        cast(Any, MessageRecipient.kind) == "to",
                    )
                )
                _rsl_rows = _rsl_result.all()
            if _rsl_rows:
                _rsl_targets: list[str] = []
                for _rsl_name, _rsl_proj_id, _ in _rsl_rows:
                    if _rsl_proj_id == project.id:
                        _rsl_targets.append(_rsl_name)
                    else:
                        _rsl_proj = await _get_project_by_id(_rsl_proj_id)
                        _rsl_targets.append(_format_cross_project_agent_address(_rsl_proj.slug, _rsl_name))
                to_names = _rsl_targets
            else:
                # Fallback: no "to" recipients found (shouldn't happen), use original sender
                to_names = [original_sender.name]
        else:
            default_reply_target = (
                original_sender.name
                if original_sender.project_id == project.id
                else _format_cross_project_agent_address(original_sender_project.slug, original_sender.name)
            )
            to_names = [default_reply_target] if to is None else to
        cc_list = cc or []
        bcc_list = bcc or []

        local_to: list[str] = []
        local_cc: list[str] = []
        local_bcc: list[str] = []
        external: dict[int, dict[str, Any]] = {}
        unknown_local: set[str] = set()
        unknown_external: dict[str, set[str]] = defaultdict(set)
        thread_external_participants = await _get_thread_external_participants(project, sender, thread_key)

        async with get_session() as sx:
            existing = await sx.execute(
                select(Agent.name).where(
                    Agent.project_id == project.id,
                    Agent.provisioning_state == "active",
                )
            )
            local_lookup: dict[str, str] = {}
            for row in existing.fetchall():
                canonical_name = (row[0] or "").strip()
                if not canonical_name:
                    continue
                sanitized_canonical = sanitize_agent_name(canonical_name) or canonical_name
                for key in {canonical_name.lower(), sanitized_canonical.lower()}:
                    local_lookup.setdefault(key, canonical_name)

            sender_candidate_keys = {
                key.lower()
                for key in (
                    (sender.name or "").strip(),
                    sanitize_agent_name(sender.name or "") or "",
                )
                if key
            }

            class _ContactBlocked(Exception):
                pass

            def _normalize(value: str) -> tuple[str, set[str], Optional[str]]:
                trimmed = (value or "").strip()
                sanitized = sanitize_agent_name(trimmed)
                keys: set[str] = set()
                if trimmed:
                    keys.add(trimmed.lower())
                if sanitized:
                    keys.add(sanitized.lower())
                # Preserve the exact durable identity for DB lookup.  The
                # sanitized spelling is only an alias candidate; notably,
                # sanitize_agent_name() removes hyphens from canonical
                # client-os-host-slot names.
                canonical = trimmed if trimmed else None
                return trimmed or value, keys, canonical

            async def _route(name_list: list[str], kind: str) -> None:
                for raw in name_list:
                    candidate = raw or ""
                    explicit_override = False
                    target_project_override: Project | None = None
                    target_project_label: str | None = None
                    agent_fragment = candidate

                    if candidate.startswith("project:") and "#" in candidate:
                        parsed_project_label: str | None = None
                        try:
                            explicit_override = True
                            _, rest = candidate.split(":", 1)
                            slug_part, agent_part = rest.split("#", 1)
                            parsed_project_label = slug_part.strip() or None
                            target_project_override = await _get_project_by_identifier(parsed_project_label or "")
                            target_project_label = target_project_override.human_key or target_project_override.slug
                            agent_fragment = agent_part
                        except Exception:
                            label = parsed_project_label or "(invalid project)"
                            unknown_external[label].add(candidate.strip() or candidate)
                            continue

                    if not explicit_override and "@" in candidate:
                        name_part, project_part = candidate.split("@", 1)
                        if name_part.strip() and project_part.strip():
                            try:
                                target_project_override = await _get_project_by_identifier(project_part.strip())
                                target_project_label = target_project_override.human_key or target_project_override.slug
                                agent_fragment = name_part
                                explicit_override = True
                            except Exception:
                                label = project_part.strip() or "(invalid project)"
                                unknown_external[label].add(candidate.strip() or candidate)
                                continue

                    display_value, key_candidates, canonical = _normalize(agent_fragment)
                    if not key_candidates or not canonical:
                        if explicit_override:
                            label = target_project_label or "(unknown project)"
                            unknown_external[label].add(candidate.strip() or candidate)
                        else:
                            unknown_local.add(candidate.strip() or candidate)
                        continue

                    if not explicit_override and sender_candidate_keys.intersection(key_candidates):
                        if kind == "to":
                            local_to.append(sender.name)
                        elif kind == "cc":
                            local_cc.append(sender.name)
                        else:
                            local_bcc.append(sender.name)
                        continue

                    if not explicit_override:
                        resolved_local = None
                        for key in key_candidates:
                            resolved_local = local_lookup.get(key)
                            if resolved_local:
                                break
                        if resolved_local:
                            if kind == "to":
                                local_to.append(resolved_local)
                            elif kind == "cc":
                                local_cc.append(resolved_local)
                            else:
                                local_bcc.append(resolved_local)
                            continue

                    lookup_value = canonical.lower()
                    rows = None
                    if explicit_override and target_project_override is not None:
                        rows = await sx.execute(
                            select(AgentLink, Project, Agent)
                            .join(Project, Project.id == AgentLink.b_project_id)
                            .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                            .where(
                                cast(Any, AgentLink.a_project_id) == project.id,
                                cast(Any, AgentLink.a_agent_id) == sender.id,
                                _active_approved_agent_link_clause(),
                                cast(Any, Project.id == target_project_override.id),
                                cast(Any, func.lower(Agent.name) == lookup_value),
                            )
                            .limit(1)
                        )
                    else:
                        rows = await sx.execute(
                            select(AgentLink, Project, Agent)
                            .join(Project, Project.id == AgentLink.b_project_id)
                            .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                            .where(
                                cast(Any, AgentLink.a_project_id) == project.id,
                                cast(Any, AgentLink.a_agent_id) == sender.id,
                                _active_approved_agent_link_clause(),
                                cast(Any, func.lower(Agent.name) == lookup_value),
                            )
                            .limit(1)
                        )
                    rec = rows.first()
                    if rec:
                        _link, target_project, target_agent = rec
                        recipient_policy = (getattr(target_agent, "contact_policy", "auto") or "auto").lower()
                        if recipient_policy == "block_all":
                            await ctx.error("CONTACT_BLOCKED: Recipient is not accepting messages.")
                            raise _ContactBlocked()
                        bucket = external.setdefault(target_project.id or 0, {"project": target_project, "to": [], "cc": [], "bcc": []})
                        bucket[kind].append(target_agent.name)
                    else:
                        if explicit_override and target_project_override is not None:
                            thread_participant = thread_external_participants.get(
                                (target_project_override.id or 0, lookup_value)
                            )
                            if thread_participant is not None:
                                target_project, participant_name = thread_participant
                                bucket = external.setdefault(
                                    target_project.id or 0,
                                    {"project": target_project, "to": [], "cc": [], "bcc": []},
                                )
                                bucket[kind].append(participant_name)
                                continue
                        if explicit_override:
                            label = target_project_label or "(unknown project)"
                            unknown_external[label].add(display_value or candidate.strip() or candidate)
                        else:
                            unknown_local.add(display_value or candidate.strip() or candidate)

            try:
                await _route(to_names, "to")
                await _route(cc_list, "cc")
                await _route(bcc_list, "bcc")
            except _ContactBlocked:
                return {"error": {"type": "CONTACT_BLOCKED", "message": "Recipient is not accepting messages."}}

        if unknown_local or unknown_external:
            parts: list[str] = []
            err_data: dict[str, Any] = {}
            if unknown_local:
                missing_local = sorted({name for name in unknown_local if name})
                parts.append(
                    f"local recipients {', '.join(missing_local)} are not registered in project '{project.human_key}'"
                )
                err_data["unknown_local"] = missing_local
            if unknown_external:
                formatted_external = {
                    label: sorted({name for name in names if name})
                    for label, names in unknown_external.items()
                }
                ext_parts = [
                    f"{', '.join(names)} @ {label}"
                    for label, names in sorted(formatted_external.items())
                    if names
                ]
                if ext_parts:
                    parts.append("external recipients missing approved contact links: " + "; ".join(ext_parts))
                err_data["unknown_external"] = formatted_external
            hint = f"Use resource://agents/{project.slug} to list registered agents, or request_contact(...) to create a cross-project link first."
            parts.append(hint)
            message = "Unable to send reply — " + "; ".join(parts)
            err_data["hint"] = hint
            raise ToolExecutionError("RECIPIENT_NOT_FOUND", message, recoverable=True, data=err_data)

        if settings_local.contact_enforcement_enabled:
            auto_ok_names: set[str] = set()
            try:
                sender_alias = aliased(Agent)
                criteria: list[Any] = [cast(Any, Message.thread_id) == thread_key]
                try:
                    seed_id = int(thread_key)
                    criteria.append(cast(Any, Message.id) == seed_id)
                except (ValueError, TypeError):
                    pass
                async with get_session() as s_contact:
                    stmt = (
                        select(Message, sender_alias.name, sender_alias.project_id)
                        .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
                        .where(
                            cast(Any, Message.project_id) == project.id,
                            or_(*criteria),
                            _message_visible_to_agent_clause(sender.id or 0),
                        )
                        .limit(500)
                    )
                    thread_rows = [(row[0], row[1], row[2]) for row in (await s_contact.execute(stmt)).all()]
                    participants: set[str] = {
                        n for _m, n, sender_project_id in thread_rows if n and sender_project_id == project.id
                    }
                    message_ids = [m.id for m, _n, _sender_project_id in thread_rows if m.id is not None]
                    if message_ids:
                        recipient_rows = await s_contact.execute(
                            select(Agent.name)
                            .join(MessageRecipient, cast(Any, MessageRecipient.agent_id) == Agent.id)
                            .where(cast(Any, MessageRecipient.message_id).in_(message_ids))
                        )
                        participants.update({row[0] for row in recipient_rows.all() if row[0]})
                    auto_ok_names.update(participants)
            except Exception:
                logger.exception("Failed to fetch thread participants for reply contact auto-allow (thread_id=%s)", thread_key)

            blocked_recipients: list[str] = []
            all_local_names = list(dict.fromkeys(local_to + local_cc + local_bcc))
            recipient_agents = await _get_agents_batch_lenient(project, all_local_names)
            now_utc = datetime.now(timezone.utc)
            ttl = timedelta(seconds=int(settings_local.contact_auto_ttl_seconds))
            since_dt = now_utc - ttl
            async with get_session() as s_contact:
                recent_ok_names: set[str] = set()
                try:
                    if all_local_names:
                        sent_stmt = (
                            select(Agent.name)
                            .join(MessageRecipient, cast(Any, MessageRecipient.agent_id) == Agent.id)
                            .join(Message, cast(Any, MessageRecipient.message_id) == Message.id)
                            .where(
                                cast(Any, Message.project_id) == project.id,
                                cast(Any, Message.sender_id) == sender.id,
                                cast(Any, Message.created_ts) > _naive_utc(since_dt),
                                cast(Any, Agent.name).in_(all_local_names),
                            )
                        )
                        sent_rows = await s_contact.execute(sent_stmt)
                        recent_ok_names.update({row[0] for row in sent_rows.all() if row[0]})

                        sender_alias2 = aliased(Agent)
                        recv_stmt = (
                            select(sender_alias2.name)
                            .join(Message, cast(Any, Message.sender_id) == sender_alias2.id)
                            .join(MessageRecipient, cast(Any, MessageRecipient.message_id) == Message.id)
                            .where(
                                cast(Any, Message.project_id) == project.id,
                                cast(Any, MessageRecipient.agent_id) == sender.id,
                                cast(Any, Message.created_ts) > _naive_utc(since_dt),
                                cast(Any, sender_alias2.name).in_(all_local_names),
                            )
                        )
                        recv_rows = await s_contact.execute(recv_stmt)
                        recent_ok_names.update({row[0] for row in recv_rows.all() if row[0]})
                except Exception:
                    logger.exception("Failed to batch fetch recent contacts for reply auto-allow heuristics")
                    recent_ok_names = set()

                approved_link_ids: set[int] = set()
                try:
                    recipient_ids = [rec.id for rec in recipient_agents.values() if rec is not None and rec.id is not None]
                    if recipient_ids:
                        link_rows = await s_contact.execute(
                            select(AgentLink.b_agent_id)
                            .where(
                                cast(Any, AgentLink.a_project_id) == project.id,
                                cast(Any, AgentLink.a_agent_id) == sender.id,
                                cast(Any, AgentLink.b_project_id) == project.id,
                                _active_approved_agent_link_clause(now_utc),
                                cast(Any, AgentLink.b_agent_id).in_(recipient_ids),
                            )
                        )
                        approved_link_ids.update({row[0] for row in link_rows.all() if row and row[0] is not None})
                except Exception:
                    logger.exception("Failed to batch fetch approved agent links for reply_message")

                for nm in local_to + local_cc + local_bcc:
                    if nm in auto_ok_names:
                        continue
                    rec = recipient_agents.get(nm)
                    if rec is None or rec.name == sender.name:
                        continue
                    if getattr(rec, "retired_at", None) is not None:
                        raise ToolExecutionError(
                            "AGENT_RETIRED",
                            f"Agent '{nm}' is retired and no longer accepts new messages. "
                            "Use unretire_agent to restore it first.",
                            recoverable=True,
                            data={"agent_name": nm, "retired_at": _iso(rec.retired_at)},
                        )
                    rec_policy = getattr(rec, "contact_policy", "auto").lower()
                    if rec_policy == "open":
                        continue
                    if rec_policy == "block_all":
                        raise ToolExecutionError(
                            "CONTACT_BLOCKED",
                            "Recipient is not accepting messages.",
                            recoverable=True,
                        )
                    if rec_policy == "auto" and rec.name in recent_ok_names:
                        continue
                    if rec.id is not None and rec.id in approved_link_ids:
                        continue
                    blocked_recipients.append(rec.name)

            if blocked_recipients:
                blocked_sorted = sorted(set(blocked_recipients))
                recipient_list = ", ".join(blocked_sorted)
                sample_target = blocked_sorted[0]
                project_expr = repr(project.human_key)
                sender_expr = repr(sender.name)
                target_expr = repr(sample_target)
                err_msg = (
                    f"Contact approval required for recipients: {recipient_list}. "
                    f"Before retrying, create a pending request with "
                    f"`request_contact(project_key={project_expr}, from_agent={sender_expr}, to_agent={target_expr})`, "
                    f"then have the recipient approve it with "
                    f"`respond_contact(project_key={project_expr}, to_agent={target_expr}, from_agent={sender_expr}, accept=True)`."
                )
                raise ToolExecutionError(
                    "CONTACT_REQUIRED",
                    err_msg,
                    recoverable=True,
                    data={"recipients_blocked": blocked_sorted},
                )

        deliveries: list[dict[str, Any]] = []
        delivery_errors: list[dict[str, Any]] = []
        if local_to or local_cc or local_bcc:
            payload_local = await _deliver_message(
                ctx,
                "reply_message",
                project,
                sender,
                local_to,
                local_cc,
                local_bcc,
                reply_subject,
                body_md,
                None,
                None,
                importance=original.importance,
                ack_required=original.ack_required,
                thread_id=thread_key,
                idempotency_key=idempotency_key,
                topic=original.topic,
                reply_to=original.id,
                purpose="reply",
            )
            _collect_delivery_result(deliveries, delivery_errors, project, payload_local)

        for _pid, group in external.items():
            target_project: Project = group["project"]
            try:
                payload_ext = await _deliver_message(
                    ctx,
                    "reply_message",
                    target_project,
                    sender,
                    group.get("to", []),
                    group.get("cc", []),
                    group.get("bcc", []),
                    reply_subject,
                    body_md,
                    None,
                    None,
                    importance=original.importance,
                    ack_required=original.ack_required,
                    thread_id=thread_key,
                    idempotency_key=idempotency_key,
                    topic=original.topic,
                    reply_to=None,
                    purpose="reply",
                )
                _collect_delivery_result(deliveries, delivery_errors, target_project, payload_ext)
            except Exception as exc:
                logger.exception("Failed to deliver reply to external project %r", target_project.human_key)
                delivery_errors.append(_delivery_failure_from_exception(target_project, exc))
                continue

        if not deliveries:
            payload: dict[str, Any] = {
                "thread_id": thread_key,
                "reply_to": message_id,
                "deliveries": [],
                "count": 0,
            }
            if delivery_errors:
                payload["error"] = _summarize_delivery_failures(
                    delivery_errors,
                    summary_message="Reply delivery failed for all target projects.",
                )
            return payload

        primary_payload: dict[str, Any] = {
            "thread_id": thread_key,
            "reply_to": message_id,
            "deliveries": deliveries,
            "count": len(deliveries),
        }
        if delivery_errors:
            primary_payload["delivery_errors"] = delivery_errors
        return primary_payload

    @mcp.tool(name="request_contact")
    @_instrument_tool(
        "request_contact",
        cluster=CLUSTER_CONTACT,
        capabilities={"contact"},
        project_arg="project_key",
        agent_arg="from_agent",
    )
    @retry_on_db_lock(max_retries=3, base_delay=0.05, max_delay=0.5)
    async def request_contact(
        ctx: Context,
        project_key: str,
        from_agent: str,
        to_agent: str,
        to_project: Optional[str] = None,
        reason: str = "",
        ttl_seconds: int = 7 * 24 * 3600,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Request contact approval to message another agent.

        Creates (or refreshes) a pending AgentLink and sends a small ack_required intro message.

        Discovery
        ---------
        To discover available agent names, use: resource://agents/{project_key}
        Agent names are NOT the same as program names or user names.

        Parameters
        ----------
        project_key : str
            Project slug or human key.
        from_agent : str
            Your agent name (must be registered in the project).
        to_agent : str
            Registered target Agent name (use resource://agents/{project_key} to
            discover names). A requester never creates the target's mailbox: the
            target must self-register, or an operator must explicitly provision it.
        to_project : Optional[str]
            Target project if different from your project (cross-project coordination).
        reason : str
            Optional explanation for the contact request.
        ttl_seconds : int
            Time to live for the contact approval request (default: 7 days).
        """
        project = await _get_project_by_identifier(project_key)
        a = await _authenticate_agent(
            ctx,
            project,
            from_agent,
            registration_token,
            token_param="registration_token",
            action="request_contact",
        )
        # Allow explicit external addressing in to_agent as project:<slug>#<Name>
        target_project = project
        target_name = to_agent
        if to_project:
            target_project = await _get_project_by_identifier(to_project)
        elif to_agent.startswith("project:") and "#" in to_agent:
            try:
                _, rest = to_agent.split(":", 1)
                slug_part, agent_part = rest.split("#", 1)
                target_project = await _get_project_by_identifier(slug_part)
                target_name = agent_part.strip()
            except Exception:
                target_project = project
                target_name = to_agent
        try:
            b = await _get_agent(target_project, target_name)
        except (NoResultFound, ToolExecutionError) as exc:
            is_not_found = isinstance(exc, NoResultFound) or (
                isinstance(exc, ToolExecutionError) and exc.error_type == "NOT_FOUND"
            )
            if is_not_found:
                raise _target_registration_required_error(
                    target_project,
                    target_name,
                ) from exc
            raise
        _raise_if_self_contact(
            project,
            a,
            target_project,
            b,
            action="request_contact",
        )
        # Warn on TTL auto-correction
        if ttl_seconds < 60:
            await ctx.info(
                f"[warn] ttl_seconds={ttl_seconds} is below minimum (60s); auto-correcting to 60 seconds."
            )
        now = datetime.now(timezone.utc)
        naive_now = _naive_utc(now)
        exp = naive_now + timedelta(seconds=max(60, ttl_seconds))
        result_expires: datetime | None = exp
        should_notify = False
        result_status = "pending"
        async with get_session() as s:
            # upsert link
            existing = await s.execute(
                select(AgentLink).where(
                    cast(Any, AgentLink.a_project_id) == project.id,
                    cast(Any, AgentLink.a_agent_id) == a.id,
                    cast(Any, AgentLink.b_project_id) == target_project.id,
                    cast(Any, AgentLink.b_agent_id) == b.id,
                )
            )
            link = existing.scalars().first()
            if link:
                previous_status = link.status
                is_active_approved = previous_status == "approved" and (
                    link.expires_ts is None or link.expires_ts > naive_now
                )
                is_active_pending = previous_status == "pending" and (
                    link.expires_ts is None or link.expires_ts > naive_now
                )
                if is_active_approved:
                    link.reason = reason
                    link.updated_ts = naive_now
                    result_status = "approved"
                    should_notify = False
                    if link.expires_ts is None:
                        result_expires = None
                    else:
                        link.expires_ts = max(link.expires_ts, exp)
                        result_expires = link.expires_ts
                elif is_active_pending:
                    # Keep the pending event's content and timestamp immutable.
                    # Its timestamp is the deterministic delivery idempotency
                    # component, so a retry cannot create a second intro intent.
                    result_status = "pending"
                    should_notify = False
                    if link.expires_ts is None:
                        link.expires_ts = exp
                    else:
                        link.expires_ts = max(link.expires_ts, exp)
                    result_expires = link.expires_ts
                else:
                    link.status = "pending"
                    link.reason = reason
                    link.updated_ts = naive_now
                    link.expires_ts = exp
                    result_expires = exp
                    should_notify = previous_status != "pending" or not is_active_pending
                s.add(link)
            else:
                link = AgentLink(
                    a_project_id=project.id or 0,
                    a_agent_id=a.id or 0,
                    b_project_id=target_project.id or 0,
                    b_agent_id=b.id or 0,
                    status="pending",
                    reason=reason,
                    created_ts=naive_now,
                    updated_ts=naive_now,
                    expires_ts=exp,
                )
                s.add(link)
                should_notify = True
            try:
                await s.commit()
            except IntegrityError:
                # Another concurrent request created the link. Treat this as an idempotent refresh.
                await s.rollback()
                existing = await s.execute(
                    select(AgentLink).where(
                        cast(Any, AgentLink.a_project_id) == project.id,
                        cast(Any, AgentLink.a_agent_id) == a.id,
                        cast(Any, AgentLink.b_project_id) == target_project.id,
                        cast(Any, AgentLink.b_agent_id) == b.id,
                    )
                )
                link = existing.scalars().first()
                if link is None:
                    raise
                previous_status = link.status
                is_active_approved = previous_status == "approved" and (
                    link.expires_ts is None or link.expires_ts > naive_now
                )
                is_active_pending = previous_status == "pending" and (
                    link.expires_ts is None or link.expires_ts > naive_now
                )
                if is_active_approved:
                    link.reason = reason
                    link.updated_ts = naive_now
                    result_status = "approved"
                    should_notify = False
                    if link.expires_ts is None:
                        result_expires = None
                    else:
                        link.expires_ts = max(link.expires_ts, exp)
                        result_expires = link.expires_ts
                elif is_active_pending:
                    result_status = "pending"
                    should_notify = False
                    if link.expires_ts is None:
                        link.expires_ts = exp
                    else:
                        link.expires_ts = max(link.expires_ts, exp)
                    result_expires = link.expires_ts
                else:
                    link.status = "pending"
                    link.reason = reason
                    link.updated_ts = naive_now
                    link.expires_ts = exp
                    result_expires = exp
                    should_notify = previous_status != "pending" or not is_active_pending
                s.add(link)
                await s.commit()

        subject = f"Contact request from {a.name}"
        body = link.reason or f"{a.name} requests permission to contact {b.name}."
        if result_status == "pending" and not should_notify:
            should_notify = not await _contact_request_notification_exists(target_project, a, b)

        notification_message: dict[str, Any] | None = None
        notification_error: dict[str, Any] | None = None
        if result_status == "pending" and should_notify:
            if link.id is None:
                raise RuntimeError("Pending contact link has no durable identity.")
            contact_event_key = (
                f"contact-request:{link.id}:"
                f"{link.updated_ts.isoformat(timespec='microseconds')}"
            )
            # Send an intro message with ack_required.
            notification_payload = await _deliver_message(
                ctx,
                "request_contact",
                target_project,
                a,
                [b.name],
                [],
                [],
                subject,
                body,
                None,
                None,
                importance="normal",
                ack_required=True,
                thread_id=None,
                idempotency_key=contact_event_key,
                purpose="contact_request",
            )
            error_payload = _extract_delivery_error_payload(notification_payload)
            if error_payload is not None:
                notification_error = _with_delivery_project(error_payload, target_project)
            else:
                notification_message = notification_payload

        result: dict[str, Any] = {
            "from": a.name,
            "from_project": project.human_key,
            "to": b.name,
            "to_project": target_project.human_key,
            "status": result_status,
            "expires_ts": _iso(result_expires) if result_expires is not None else None,
        }
        if notification_message is not None:
            result["notification_message"] = notification_message
        if notification_error is not None:
            result["notification_error"] = notification_error
        return result

    @mcp.tool(name="respond_contact")
    @_instrument_tool(
        "respond_contact",
        cluster=CLUSTER_CONTACT,
        capabilities={"contact"},
        project_arg="project_key",
        agent_arg="to_agent",
    )
    @retry_on_db_lock(max_retries=3, base_delay=0.05, max_delay=0.5)
    async def respond_contact(
        ctx: Context,
        project_key: str,
        to_agent: str,
        from_agent: str,
        accept: bool,
        ttl_seconds: int = 30 * 24 * 3600,
        from_project: Optional[str] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Approve or deny a contact request."""
        project = await _get_project_by_identifier(project_key)
        # Resolve remote requestor project if provided
        a_project = project if not from_project else await _get_project_by_identifier(from_project)
        a = await _get_agent(a_project, from_agent)
        b = await _authenticate_agent(
            ctx,
            project,
            to_agent,
            registration_token,
            token_param="registration_token",
            action="respond_contact",
        )
        _raise_if_self_contact(
            a_project,
            a,
            project,
            b,
            action="respond_contact",
        )
        # Warn on TTL auto-correction
        if accept and ttl_seconds < 60:
            await ctx.info(
                f"[warn] ttl_seconds={ttl_seconds} is below minimum (60s); auto-correcting to 60 seconds."
            )
        now = datetime.now(timezone.utc)
        naive_now = _naive_utc(now)
        approved_exp = naive_now + timedelta(seconds=max(60, ttl_seconds))
        exp = approved_exp if accept else None
        result_expires: datetime | None = exp
        updated = 0
        async with get_session() as s:
            existing = await s.execute(
                select(AgentLink).where(
                    cast(Any, AgentLink.a_project_id) == a_project.id,
                    cast(Any, AgentLink.a_agent_id) == a.id,
                    cast(Any, AgentLink.b_project_id) == project.id,
                    cast(Any, AgentLink.b_agent_id) == b.id,
                )
            )
            link = existing.scalars().first()
            if link:
                link.updated_ts = naive_now
                if accept:
                    is_active_approved = link.status == "approved" and (
                        link.expires_ts is None or link.expires_ts > naive_now
                    )
                    link.status = "approved"
                    if is_active_approved:
                        if link.expires_ts is None:
                            result_expires = None
                        else:
                            link.expires_ts = max(link.expires_ts, approved_exp)
                            result_expires = link.expires_ts
                    else:
                        link.expires_ts = exp
                        result_expires = exp
                else:
                    link.status = "blocked"
                    link.expires_ts = None
                    result_expires = None
                s.add(link)
                updated = 1
            else:
                if accept:
                    if a_project.id is None or a.id is None or project.id is None or b.id is None:
                        raise ValueError("Projects and agents must have ids before creating contact links.")
                    s.add(AgentLink(
                        a_project_id=a_project.id,
                        a_agent_id=a.id,
                        b_project_id=project.id,
                        b_agent_id=b.id,
                        status="approved",
                        reason="",
                        created_ts=naive_now,
                        updated_ts=naive_now,
                        expires_ts=exp,
                    ))
                    updated = 1
            await s.commit()
        await ctx.info(f"Contact {'approved' if accept else 'denied'}: {from_agent} -> {to_agent}")
        return {
            "from": from_agent,
            "to": to_agent,
            "approved": bool(accept),
            "expires_ts": _iso(result_expires) if result_expires is not None else None,
            "updated": updated,
        }

    @mcp.tool(name="list_contacts")
    @_instrument_tool(
        "list_contacts",
        cluster=CLUSTER_CONTACT,
        capabilities={"contact", "audit"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def list_contacts(
        ctx: Context,
        project_key: str,
        agent_name: str,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> ToonableList:
        """List contact links for an agent in a project."""
        project = await _get_project_by_identifier(project_key)
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="list_contacts",
        )
        out: list[dict[str, Any]] = []
        now_utc = datetime.now(timezone.utc)
        async with get_session() as s:
            rows = await s.execute(
                select(AgentLink, Project.human_key, Agent.name)
                .join(Project, Project.id == AgentLink.b_project_id)
                .join(Agent, cast(Any, Agent.id == AgentLink.b_agent_id))
                .where(cast(Any, AgentLink.a_project_id) == project.id, cast(Any, AgentLink.a_agent_id) == agent.id)
                .order_by(desc(cast(Any, AgentLink.updated_ts)), desc(cast(Any, AgentLink.id)))
            )
            for link, target_project_key, name in rows.all():
                is_expired = _agent_link_is_expired(link, now_utc)
                out.append({
                    "to": name,
                    "to_project": target_project_key,
                    "status": link.status,
                    "is_expired": is_expired,
                    "allows_messaging": link.status == "approved" and not is_expired,
                    "reason": link.reason,
                    "updated_ts": _iso(link.updated_ts),
                    "expires_ts": _iso(link.expires_ts) if link.expires_ts else None,
                })
        return out

    @mcp.tool(name="set_agent_display_name")
    @_instrument_tool(
        "set_agent_display_name",
        cluster=CLUSTER_CONTACT,
        capabilities={"configure"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def set_agent_display_name(
        ctx: Context,
        project_key: str,
        agent_name: str,
        display_name: Optional[str] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Give this agent a human-readable label, shown alongside its name.

        The label is for display only. It is NOT an address: `to:`, `cc:` and
        every other recipient field still take `agent_name`, which does not
        change. That separation is the point — a derived name that never moves
        cannot orphan a mailbox, a credential or a reservation, and an alias
        that cannot be addressed cannot make a mutable field load-bearing.

        Authenticated by the caller's own registration token, so an agent can
        rename itself and nobody else.

        Parameters
        ----------
        display_name : Optional[str]
            The label. Pass null or an empty string to clear it and go back to
            showing the plain name.

        Returns
        -------
        dict
            `{agent, display_name}` — the canonical name and the label now set.
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="set_agent_display_name",
        )

        label = (display_name or "").strip()
        # Control characters would corrupt every rendering of this — a newline
        # alone turns one line of a conflict warning into two and lets an alias
        # forge the line that follows it.
        label = "".join(ch for ch in label if ch.isprintable())
        if len(label) > 128:
            label = label[:128].rstrip()

        if label:
            async with get_session() as s:
                clash = (
                    await s.execute(
                        text(
                            "SELECT name FROM agents WHERE project_id = :pid "
                            "AND id != :aid AND (lower(name) = lower(:label) "
                            "OR lower(COALESCE(display_name, '')) = lower(:label)) LIMIT 1"
                        ),
                        {"pid": project.id, "aid": agent.id, "label": label},
                    )
                ).fetchone()
            # An alias equal to another agent's NAME is the one genuinely
            # deceptive case: readers would attribute this agent's messages and
            # reservations to that one. Duplicate aliases are merely confusing,
            # but rejecting both costs a single query and keeps the door open
            # if addressing is ever revisited.
            if clash is not None:
                raise ToolExecutionError(
                    error_type="INVALID_ARGUMENT",
                    message=(
                        f"Display name {label!r} is already taken by agent {clash[0]!r} "
                        "in this project, as its name or its alias."
                    ),
                    recoverable=True,
                    data={"argument": "display_name", "provided": label, "conflicts_with": clash[0]},
                )

        async with get_session() as s:
            db_agent = await s.get(Agent, agent.id)
            if db_agent is not None:
                db_agent.display_name = label or None
                s.add(db_agent)
                await s.commit()
        return {"agent": agent.name, "display_name": label or None}

    @mcp.tool(name="set_agent_notify_sound")
    @_instrument_tool(
        "set_agent_notify_sound",
        cluster=CLUSTER_CONTACT,
        capabilities={"configure"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def set_agent_notify_sound(
        ctx: Context,
        project_key: str,
        agent_name: str,
        notify_sound: Optional[str] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Choose the tone the mail viewer plays when this agent's message arrives.

        A name from a fixed set, not a frequency and not a URL. The browser
        synthesises the tone itself, so setting this cannot cause a request to a
        host anyone chose, and cannot carry a 20 kHz value into somebody's
        headphones. The same reasoning as `display_name`: a field a colleague
        sets and a human hears has to be safe without the human vetting it.

        This is a preference, not an identity. It changes nothing about
        addressing, authorisation or delivery — a message sounds different and is
        otherwise the same message.

        Authenticated by the caller's own registration token, so an agent can
        pick its own tone and nobody else's.

        Parameters
        ----------
        notify_sound : Optional[str]
            One of: chime, low, high, soft, click, double, rising, falling,
            knock, pulse, bell, sparkle. Pass null or an empty string to clear
            it and fall back to the viewer's default tone.

        Returns
        -------
        dict
            `{agent, notify_sound, available}` — what is set now, and the whole
            vocabulary, so a caller never has to guess the valid values or read
            this docstring twice.
        """
        project = await _get_project_by_identifier(project_key)
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="set_agent_notify_sound",
        )

        choice = (notify_sound or "").strip().lower()
        if choice and choice not in NOTIFY_SOUND_NAMES:
            # Named explicitly rather than silently ignored: a tone that does not
            # play is indistinguishable from a viewer with sound switched off,
            # and the agent would have no way to tell which it was looking at.
            raise ToolExecutionError(
                error_type="INVALID_NOTIFY_SOUND",
                message=(
                    f"Unknown notify_sound {choice!r}. "
                    f"Expected one of: {', '.join(NOTIFY_SOUND_NAMES)}."
                ),
            )

        async with get_session() as s:
            db_agent = await s.get(Agent, agent.id)
            if db_agent is not None:
                db_agent.notify_sound = choice or None
                s.add(db_agent)
                await s.commit()
        return {
            "agent": agent.name,
            "notify_sound": choice or None,
            "available": list(NOTIFY_SOUND_NAMES),
        }

    @mcp.tool(name="set_contact_policy")
    @_instrument_tool(
        "set_contact_policy",
        cluster=CLUSTER_CONTACT,
        capabilities={"contact", "configure"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def set_contact_policy(
        ctx: Context,
        project_key: str,
        agent_name: str,
        policy: str,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Set contact policy for an agent: open | auto | contacts_only | block_all."""
        project = await _get_project_by_identifier(project_key)
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="set_contact_policy",
        )
        pol = (policy or "auto").lower()
        valid_policies = {"open", "auto", "contacts_only", "block_all"}
        if pol not in valid_policies:
            # Reject unknown policies rather than silently coercing to "auto"
            # (issue #201): coercing e.g. "block" -> "auto" weakens protection.
            await ctx.error(
                f"INVALID_ARGUMENT: unknown contact policy {policy!r}. "
                f"Expected one of: {', '.join(sorted(valid_policies))}."
            )
            raise ToolExecutionError(
                error_type="INVALID_ARGUMENT",
                message=(
                    f"Unknown contact policy {policy!r}. "
                    f"Expected one of: {', '.join(sorted(valid_policies))}."
                ),
                recoverable=True,
                data={"argument": "policy", "provided": policy, "valid": sorted(valid_policies)},
            )
        async with get_session() as s:
            db_agent = await s.get(Agent, agent.id)
            if db_agent:
                db_agent.contact_policy = pol
                s.add(db_agent)
                await s.commit()
        return {"agent": agent.name, "policy": pol}

    @mcp.tool(name="fetch_inbox")
    @_instrument_tool(
        "fetch_inbox",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "read"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def fetch_inbox(
        ctx: Context,
        project_key: str,
        agent_name: str,
        limit: int = 20,
        urgent_only: bool = False,
        include_bodies: bool = False,
        since_ts: Optional[str] = None,
        topic: Optional[str] = None,
        unread_only: bool = False,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> ToonableList:
        """
        Retrieve recent messages for an agent without mutating read/ack state.

        Filters
        -------
        - `urgent_only`: only messages with importance in {high, urgent}
        - `since_ts`: ISO-8601 timestamp string; messages strictly newer than this are returned
        - `limit`: max number of messages (default 20)
        - `include_bodies`: include full Markdown bodies in the payloads
        - `topic`: filter to messages with this topic tag
        - `unread_only`: when True, restrict to messages this recipient has not
          yet explicitly marked read via `mark_message_read` or
          `acknowledge_message`. Per-recipient: a message read by Agent A is
          still unread for Agent B. A bare `fetch_inbox` call does NOT mark
          messages read; this filter inspects existing read state without
          mutating it.

        Usage patterns
        --------------
        - Poll after each editing step in an agent loop to pick up coordination messages.
        - Use `since_ts` with the timestamp from your last poll for efficient incremental fetches.
        - Use `unread_only=True` from polling agents (Claude Code, Codex, etc.) to skip
          messages the agent has already acknowledged — cuts token-burn at scale by
          avoiding re-running prompt context against already-handled mail.
        - Combine with `acknowledge_message` if `ack_required` is true.

        Returns
        -------
        list[dict]
            Each message includes: { id, subject, from, created_ts, importance, ack_required, kind, [body_md] }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"7","method":"tools/call","params":{"name":"fetch_inbox","arguments":{
          "project_key":"/owner/backend","agent_name":"codex-wsl-home-1","since_ts":"2025-10-23T00:00:00+00:00"
        }}}
        ```
        """
        # Validate limit parameter bounds (shared with search_messages, the
        # product tools, and the resource handlers via _validate_limit).
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 1000:
            await ctx.info(f"[warn] limit={limit} is very large; capping at 1000 to prevent performance issues.")
        limit = _validate_limit(limit)

        # Validate since_ts format upfront with helpful error message
        _validate_iso_timestamp(since_ts, "since_ts")

        settings = get_settings()
        if settings.tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel.fit(f"project={project_key}\nagent={agent_name}\nlimit={limit}\nurgent_only={urgent_only}", title="tool: fetch_inbox", border_style="green"))
            except Exception:
                pass
        try:
            project = await _get_project_by_identifier(project_key)
            agent = await _authenticate_agent(
                ctx,
                project,
                agent_name,
                registration_token,
                token_param="registration_token",
                action="fetch_inbox",
            )
            items = await _list_inbox(
                project,
                agent,
                limit,
                urgent_only,
                include_bodies,
                since_ts,
                topic=topic,
                unread_only=unread_only,
            )
            if settings.notifications.enabled:
                with suppress(Exception):
                    await clear_notification_signal(settings, project.slug, agent.name)
            await ctx.info(
                f"Fetched {len(items)} messages for '{agent.name}'. "
                f"urgent_only={urgent_only} unread_only={unread_only}"
            )
            return items
        except Exception as exc:
            _rich_error_panel("fetch_inbox", {"error": str(exc)})
            raise

    @mcp.tool(name="fetch_topic")
    @_instrument_tool(
        "fetch_topic",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "read"},
        project_arg="project_key",
    )
    async def fetch_topic(
        ctx: Context,
        project_key: str,
        topic_name: str,
        limit: int = 50,
        include_bodies: bool = True,
        since_ts: Optional[str] = None,
        agent_name: Optional[str] = None,
        unread_only: bool = False,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> ToonableList:
        """
        Fetch all messages in a project with a given topic tag, regardless of recipient.

        Parameters
        ----------
        project_key : str
            Project identifier.
        topic_name : str
            The topic tag to filter by (case-insensitive).
        limit : int
            Max number of messages to return (default 50).
        include_bodies : bool
            Include full Markdown bodies in the payloads (default true).
        since_ts : Optional[str]
            ISO-8601 timestamp; only messages newer than this are returned.
        unread_only : bool
            When True, restrict to messages where the viewer has a recipient
            row that has not been explicitly marked read. This narrows beyond
            the default sender-or-recipient visibility — messages the viewer
            sent (but is not a recipient of) and broadcast/thread-visible
            messages where the viewer has no MessageRecipient row are
            excluded under this flag, because "unread" is only well-defined
            for a recipient row. A bare `fetch_topic` call does NOT mark
            messages read.

        Returns
        -------
        list[dict]
            Each message includes: { id, subject, from, created_ts, importance, topic, [body_md] }
        """
        _validate_iso_timestamp(since_ts, "since_ts")
        project = await _get_project_by_identifier(project_key)
        # Authentication is only required when unread_only=True (which needs a
        # viewer's recipient rows). A bare fetch_topic call is project-scoped —
        # it returns all messages with the given topic regardless of who sent or
        # received them, so no per-agent visibility filter is applied.
        # Bound as an int here rather than kept as `Agent | None` and dereferenced
        # 60 lines below: the only reader is the unread_only branch, which cannot
        # run unless this `if` took the resolving path, but that reasoning lives
        # in two places the checker cannot connect. Narrowing at the use site
        # would need an `if viewer is None` arm that can never be taken —
        # a branch that reads like a safety check while defending against
        # nothing (_resolve_authenticated_agent returns Agent or raises).
        viewer_id = 0
        if unread_only or agent_name:
            viewer = await _resolve_authenticated_agent(
                ctx,
                project,
                agent_name=agent_name,
                provided_token=registration_token,
                token_param="registration_token",
                action="fetch_topic",
            )
            viewer_id = viewer.id or 0
        if not topic_name or not topic_name.strip():
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "topic_name must be a non-empty string.",
                recoverable=True,
                data={"argument": "topic_name"},
            )
        if limit < 1:
            limit = 1
        if limit > 1000:
            limit = 1000
        sender_alias = aliased(Agent)
        sender_project_alias = aliased(Project)
        await ensure_schema()
        async with get_session() as session:
            stmt = (
                select(
                    Message,
                    sender_alias.name,
                    sender_project_alias.id,
                    sender_project_alias.human_key,
                    sender_project_alias.slug,
                )
                .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
                .join(sender_project_alias, cast(Any, sender_alias.project_id == sender_project_alias.id))
                .where(
                    cast(Any, Message.project_id) == project.id,
                    cast(Any, func.lower(Message.topic)) == topic_name.strip().lower(),
                )
                .order_by(desc(Message.created_ts))
                .limit(limit)
            )
            if since_ts:
                since_dt = _parse_iso(since_ts)
                if since_dt:
                    stmt = stmt.where(Message.created_ts > _naive_utc(since_dt))
            if unread_only:
                # Narrow to recipient rows the viewer has not marked read.
                # The JOIN on MessageRecipient already restricts to messages
                # where the viewer has a recipient row, so no additional
                # _message_visible_to_agent_clause is needed here.
                viewer_recipient = aliased(MessageRecipient)
                stmt = (
                    stmt.join(
                        viewer_recipient,
                        cast(Any, viewer_recipient.message_id) == Message.id,
                    )
                    .where(
                        cast(Any, viewer_recipient.agent_id) == viewer_id,
                        cast(Any, viewer_recipient.read_ts).is_(None),
                    )
                )
            result = await session.execute(stmt)
            rows = result.all()
        messages: list[dict[str, Any]] = []
        for message, sender_name, sender_project_id, sender_project_human_key, sender_project_slug in rows:
            payload = _message_to_dict(message, include_body=include_bodies)
            _apply_sender_identity(
                payload,
                message_project_id=message.project_id,
                sender_name=sender_name,
                sender_project_id=sender_project_id,
                sender_project_human_key=sender_project_human_key,
                sender_project_slug=sender_project_slug,
            )
            messages.append(payload)
        await ctx.info(
            f"Fetched {len(messages)} messages with topic '{topic_name}'. "
            f"unread_only={unread_only}"
        )
        return messages

    @mcp.tool(name="mark_message_read")
    @_instrument_tool(
        "mark_message_read",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "read"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def mark_message_read(
        ctx: Context,
        project_key: str,
        agent_name: str,
        message_id: int,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Mark a specific message as read for the given agent.

        Notes
        -----
        - Read receipts are per-recipient; this only affects the specified agent.
        - This does not send an acknowledgement; use `acknowledge_message` for that.
        - Safe to call multiple times; later calls return the original timestamp.

        Idempotency
        -----------
        - If `mark_message_read` has already been called earlier for the same (agent, message),
          the original timestamp is returned and no error is raised.

        Returns
        -------
        dict
            { message_id, read: bool, read_at: iso8601 | null }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"8","method":"tools/call","params":{"name":"mark_message_read","arguments":{
          "project_key":"/owner/backend","agent_name":"codex-wsl-home-1","message_id":1234
        }}}
        ```
        """
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel.fit(f"project={project_key}\nagent={agent_name}\nmessage_id={message_id}", title="tool: mark_message_read", border_style="green"))
            except Exception:
                pass
        try:
            project = await _get_project_by_identifier(project_key)
            agent = await _authenticate_agent(
                ctx,
                project,
                agent_name,
                registration_token,
                token_param="registration_token",
                action="mark_message_read",
            )
            await _get_visible_message(project, agent, message_id)
            read_ts = await _update_recipient_timestamp(agent, message_id, "read_ts")
            await ctx.info(f"Marked message {message_id} read for '{agent.name}'.")
            return {"message_id": message_id, "read": bool(read_ts), "read_at": _iso(read_ts) if read_ts else None}
        except Exception as exc:
            if get_settings().tools_log_enabled:
                try:
                    from rich.console import Console
                    from rich.json import JSON

                    Console().print(JSON.from_data({"error": str(exc)}))
                except Exception:
                    pass
            raise

    @mcp.tool(name="acknowledge_message")
    @_instrument_tool(
        "acknowledge_message",
        cluster=CLUSTER_MESSAGING,
        capabilities={"messaging", "ack"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def acknowledge_message(
        ctx: Context,
        project_key: str,
        agent_name: str,
        message_id: int,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Acknowledge a message addressed to an agent (and mark as read).

        Behavior
        --------
        - Sets both read_ts and ack_ts for the (agent, message) pairing
        - Safe to call multiple times; subsequent calls will return the prior timestamps

        Idempotency
        -----------
        - If acknowledgement already exists, the previous timestamps are preserved and returned.

        When to use
        -----------
        - Respond to messages with `ack_required=true` to signal explicit receipt.
        - Agents can treat an acknowledgement as a lightweight, non-textual reply.

        Returns
        -------
        dict
            { message_id, acknowledged: bool, acknowledged_at: iso8601 | null, read_at: iso8601 | null }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"9","method":"tools/call","params":{"name":"acknowledge_message","arguments":{
          "project_key":"/owner/backend","agent_name":"codex-wsl-home-1","message_id":1234
        }}}
        ```
        """
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel.fit(f"project={project_key}\nagent={agent_name}\nmessage_id={message_id}", title="tool: acknowledge_message", border_style="green"))
            except Exception:
                pass
        try:
            project = await _get_project_by_identifier(project_key)
            agent = await _authenticate_agent(
                ctx,
                project,
                agent_name,
                registration_token,
                token_param="registration_token",
                action="acknowledge_message",
            )
            await _get_visible_message(project, agent, message_id)
            read_ts = await _update_recipient_timestamp(agent, message_id, "read_ts")
            ack_ts = await _update_recipient_timestamp(agent, message_id, "ack_ts")
            await ctx.info(f"Acknowledged message {message_id} for '{agent.name}'.")
            return {
                "message_id": message_id,
                "acknowledged": bool(ack_ts),
                "acknowledged_at": _iso(ack_ts) if ack_ts else None,
                "read_at": _iso(read_ts) if read_ts else None,
            }
        except Exception as exc:
            if get_settings().tools_log_enabled:
                try:
                    import importlib as _imp
                    _rc = _imp.import_module("rich.console")
                    _rj = _imp.import_module("rich.json")
                    Console = _rc.Console
                    JSON = _rj.JSON
                    Console().print(JSON.from_data({"error": str(exc)}))
                except Exception:
                    pass
            raise

    @mcp.tool(name="macro_start_session")
    @_instrument_tool(
        "macro_start_session",
        cluster=CLUSTER_MACROS,
        capabilities={"workflow", "messaging", "file_reservations", "identity"},
        project_arg="human_key",
        agent_arg="agent_name",
    )
    async def macro_start_session(
        ctx: Context,
        human_key: str,
        program: str,
        model: str,
        agent_name: str,
        external_id: str,
        client_name: str,
        execution_token: str,
        task_description: str = "",
        registration_token: Optional[str] = None,
        file_reservation_paths: Optional[list[str]] = None,
        file_reservation_reason: str = "macro-session",
        file_reservation_ttl_seconds: int = 3600,
        inbox_limit: int = 10,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Macro helper that boots a project session: ensure project, register agent,
        optionally file_reservation paths, and fetch the latest inbox snapshot.
        """
        _validate_program_model(program, model)
        if not agent_name.strip():
            raise ToolExecutionError(
                "NAME_REQUIRED",
                "macro_start_session requires an explicit durable agent_name.",
                recoverable=True,
                data={"field": "agent_name"},
            )
        get_settings()
        project = await _ensure_project(human_key)
        agent, _newly_created = await _register_or_authenticate_agent(
            ctx,
            project,
            agent_name,
            program,
            model,
            task_description,
            registration_token,
            action="macro_start_session for an existing identity",
            allow_create=False,
        )
        agent, _ = await _ensure_agent_registration_token(
            agent,
            project=project,
        )
        _bind_session_agent(ctx, project, agent)

        execution_result = await start_agent_execution(
            ctx=ctx,
            project_key=project.human_key,
            agent_name=agent.name,
            external_id=external_id,
            client_name=client_name,
            execution_token=execution_token,
            lifecycle_protocol_version=_EXECUTION_LIFECYCLE_PROTOCOL_VERSION,
            kind="session",
            task_description=task_description,
            format="json",
        )

        file_reservations_result: Optional[dict[str, Any]] = None
        if file_reservation_paths is not None:
            reservation_function = file_reservation_paths_direct
            if reservation_function is None:
                raise RuntimeError("file_reservation_paths tool is not registered")
            file_reservations_result = await reservation_function(
                ctx=ctx,
                project_key=project.human_key,
                agent_name=agent.name,
                paths=file_reservation_paths,
                ttl_seconds=file_reservation_ttl_seconds,
                exclusive=True,
                reason=file_reservation_reason,
                lifecycle_protocol_version=_EXECUTION_LIFECYCLE_PROTOCOL_VERSION,
                format="json",
            )

        inbox_items = await _list_inbox(
            project,
            agent,
            inbox_limit,
            urgent_only=False,
            include_bodies=False,
            since_ts=None,
        )
        await ctx.info(
            f"macro_start_session prepared agent '{agent.name}' on project '{project.human_key}' "
            f"(file_reservations={len(file_reservations_result['granted']) if file_reservations_result else 0})."
        )
        return {
            "project": _project_to_dict(project),
            "agent": _agent_to_dict(agent),
            "execution": execution_result,
            "file_reservations": file_reservations_result or {"granted": [], "conflicts": []},
            "inbox": inbox_items,
        }

    @mcp.tool(name="macro_prepare_thread")
    @_instrument_tool(
        "macro_prepare_thread",
        cluster=CLUSTER_MACROS,
        capabilities={"workflow", "messaging", "summarization"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def macro_prepare_thread(
        ctx: Context,
        project_key: str,
        thread_id: str,
        program: str,
        model: str,
        agent_name: str,
        external_id: str,
        client_name: str,
        execution_token: str,
        registration_token: Optional[str] = None,
        task_description: str = "",
        include_examples: bool = True,
        inbox_limit: int = 10,
        include_inbox_bodies: bool = False,
        llm_mode: bool = True,
        llm_model: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Macro helper that aligns an already provisioned agent with an existing thread,
        summarising the thread, and fetching recent inbox context.
        """
        if not agent_name.strip():
            raise ToolExecutionError(
                "NAME_REQUIRED",
                "macro_prepare_thread requires an explicit durable agent_name.",
                recoverable=True,
                data={"field": "agent_name"},
            )
        get_settings()
        project = await _get_project_by_identifier(project_key)
        _validate_program_model(program, model)
        agent, _newly_created = await _register_or_authenticate_agent(
            ctx,
            project,
            agent_name,
            program,
            model,
            task_description,
            registration_token,
            action="macro_prepare_thread for an existing identity",
            allow_create=False,
        )
        agent, _token = await _ensure_agent_registration_token(
            agent,
            project=project,
        )
        _bind_session_agent(ctx, project, agent)

        execution_result = await start_agent_execution(
            ctx=ctx,
            project_key=project.human_key,
            agent_name=agent.name,
            external_id=external_id,
            client_name=client_name,
            execution_token=execution_token,
            lifecycle_protocol_version=_EXECUTION_LIFECYCLE_PROTOCOL_VERSION,
            kind="session",
            task_description=task_description,
            format="json",
        )

        inbox_items = await _list_inbox(
            project,
            agent,
            inbox_limit,
            urgent_only=False,
            include_bodies=include_inbox_bodies,
            since_ts=None,
        )
        summary, examples, total_messages = await _compute_thread_summary(
            project,
            thread_id,
            include_examples,
            llm_mode,
            llm_model,
            viewer_agent=agent,
        )
        await ctx.info(
            f"macro_prepare_thread prepared agent '{agent.name}' for thread '{thread_id}' "
            f"on project '{project.human_key}' (messages={total_messages})."
        )
        return {
            "project": _project_to_dict(project),
            "agent": _agent_to_dict(agent),
            "execution": execution_result,
            "thread": {"thread_id": thread_id, "summary": summary, "examples": examples, "total_messages": total_messages},
            "inbox": inbox_items,
        }

    @mcp.tool(name="macro_file_reservation_cycle")
    @_instrument_tool(
        "macro_file_reservation_cycle",
        cluster=CLUSTER_MACROS,
        capabilities={"workflow", "file_reservations", "repository"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def macro_file_reservation_cycle(
        ctx: Context,
        project_key: str,
        agent_name: str,
        paths: list[str],
        ttl_seconds: int = 3600,
        exclusive: bool = True,
        reason: str = "macro-file_reservation",
        auto_release: bool = False,
        execution_id: Optional[str] = None,
        execution_token: Optional[str] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Reserve a set of file paths and optionally release them at the end of the workflow."""

        file_reservations_result = await file_reservation_paths(
            ctx=ctx,
            project_key=project_key,
            agent_name=agent_name,
            paths=paths,
            ttl_seconds=ttl_seconds,
            exclusive=exclusive,
            reason=reason,
            execution_id=execution_id,
            execution_token=execution_token,
            lifecycle_protocol_version=_EXECUTION_LIFECYCLE_PROTOCOL_VERSION,
            registration_token=registration_token,
            format="json",
        )

        release_result = None
        if auto_release:
            # Release ONLY the reservations this macro freshly granted, by id. Releasing
            # by `paths` (or including reused ids) would tear down reservations the agent
            # already held before this call. (#196)
            granted_entries = file_reservations_result.get("granted") or []
            newly_granted_ids = [
                entry["id"]
                for entry in granted_entries
                if entry.get("id") is not None and not entry.get("reused", False)
            ]
            if newly_granted_ids:
                release_result = await release_file_reservations_tool(
                    ctx=ctx,
                    project_key=project_key,
                    agent_name=agent_name,
                    file_reservation_ids=newly_granted_ids,
                    execution_id=execution_id,
                    execution_token=execution_token,
                    lifecycle_protocol_version=_EXECUTION_LIFECYCLE_PROTOCOL_VERSION,
                    registration_token=registration_token,
                    format="json",
                )
            else:
                # Nothing new to release (all reservations pre-existed); skip the call.
                release_result = {"released": [], "skipped": "no_newly_granted_reservations"}

        await ctx.info(
            f"macro_file_reservation_cycle issued {len(file_reservations_result['granted'])} file_reservation(s) for '{agent_name}' on '{project_key}'" +
            (" and released them immediately." if auto_release else ".")
        )
        return {
            "file_reservations": file_reservations_result,
            "released": release_result,
        }

    @mcp.tool(name="macro_contact_handshake")
    @_instrument_tool(
        "macro_contact_handshake",
        cluster=CLUSTER_MACROS,
        capabilities={"workflow", "contact", "messaging"},
        project_arg="project_key",
        agent_arg="requester",
    )
    @retry_on_db_lock(max_retries=3, base_delay=0.05, max_delay=0.5)
    async def macro_contact_handshake(
        ctx: Context,
        project_key: str,
        requester: Optional[str] = None,
        target: Optional[str] = None,
        reason: str = "",
        ttl_seconds: int = 7 * 24 * 3600,
        auto_accept: bool = False,
        welcome_subject: Optional[str] = None,
        welcome_body: Optional[str] = None,
        to_project: Optional[str] = None,
        requester_registration_token: Optional[str] = None,
        target_registration_token: Optional[str] = None,
        # Aliases for compatibility
        agent_name: Optional[str] = None,
        to_agent: Optional[str] = None,
        thread_id: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Request contact with an already registered target.

        The macro never creates the target's durable mailbox. The target must
        self-register, or an operator must explicitly provision it, before the
        handshake begins.
        """

        # Resolve aliases
        real_requester = (requester or agent_name or "").strip()
        real_target = (target or to_agent or "").strip()
        target_project_key = (to_project or "").strip()
        if welcome_subject is not None:
            welcome_subject = welcome_subject.strip()
            if not welcome_subject:
                raise ToolExecutionError(
                    "INVALID_ARGUMENT",
                    "welcome_subject cannot be blank when provided.",
                    recoverable=True,
                    data={"argument": "welcome_subject"},
                )
        if welcome_body is not None and not welcome_body.strip():
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "welcome_body cannot be blank when provided.",
                recoverable=True,
                data={"argument": "welcome_body"},
            )
        if (welcome_subject is None) != (welcome_body is None):
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "welcome_subject and welcome_body must be provided together.",
                recoverable=True,
                data={
                    "welcome_subject_provided": welcome_subject is not None,
                    "welcome_body_provided": welcome_body is not None,
                },
            )
        if welcome_subject is not None and not auto_accept:
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "welcome_subject and welcome_body require auto_accept=True because the macro cannot defer a welcome until manual approval completes.",
                recoverable=True,
                data={"auto_accept": auto_accept},
            )
        if not real_requester or not real_target:
            # Best-effort inference to honor "obvious intent"
            try:
                project = await _get_project_by_identifier(project_key)
                # If requester missing and exactly one agent exists in project, assume that one
                if not real_requester and project.id is not None:
                    async with get_session() as s:
                        rows = await s.execute(
                            select(Agent.name).where(
                                cast(Any, Agent.project_id) == project.id,
                                cast(Any, Agent.provisioning_state == "active"),
                            )
                        )
                        names = [str(row[0]).strip() for row in rows.fetchall() if (row and row[0])]
                    if len(names) == 1:
                        real_requester = names[0]
                # If target missing and exactly two agents exist, infer the other
                if not real_target and project.id is not None:
                    async with get_session() as s2:
                        rows2 = await s2.execute(
                            select(Agent.name).where(
                                cast(Any, Agent.project_id) == project.id,
                                cast(Any, Agent.provisioning_state == "active"),
                            )
                        )
                        names2 = [str(row[0]).strip() for row in rows2.fetchall() if (row and row[0])]
                    if real_requester and len(names2) == 2 and real_requester in names2:
                        real_target = next((n for n in names2 if n != real_requester), real_target)
            except Exception:
                pass
        if not real_requester or not real_target:
            raise ToolExecutionError(
                "INVALID_ARGUMENT",
                "macro_contact_handshake requires requester/agent_name and target/to_agent",
                recoverable=True,
                data={
                    "requester": real_requester or requester,
                    "agent_name": agent_name,
                    "target": real_target or target,
                    "to_agent": to_agent,
                    "suggested_tool_calls": [
                        {
                            "tool": "macro_contact_handshake",
                            "arguments": {
                                "project_key": project_key,
                                "requester": real_requester or "<your_agent>",
                                "target": real_target or "<their_agent>",
                                "auto_accept": True,
                                "ttl_seconds": ttl_seconds,
                            },
                        }
                    ],
                },
            )

        # Resolve the source lifetime on every path. Alias inference and the
        # same-project fast path used to be the only branches assigning this
        # local, so an explicit cross-project handshake could approve contact
        # and then fail while deriving the welcome idempotency key.
        project = await _get_project_by_identifier(project_key)

        # Fast path: for same-project auto-accept handshakes (used heavily by send_message),
        # approve the AgentLink directly without generating extra "intro" messages.
        if auto_accept and not target_project_key and not (welcome_subject and welcome_body):
            a = await _authenticate_agent(
                ctx,
                project,
                real_requester,
                requester_registration_token,
                token_param="requester_registration_token",
                action="macro_contact_handshake requester approval",
            )
            try:
                b = await _authenticate_agent(
                    ctx,
                    project,
                    real_target,
                    target_registration_token,
                    token_param="target_registration_token",
                    action="macro_contact_handshake target approval",
                )
            except (NoResultFound, ToolExecutionError) as exc:
                is_not_found = isinstance(exc, NoResultFound) or (
                    isinstance(exc, ToolExecutionError) and exc.error_type == "NOT_FOUND"
                )
                if is_not_found:
                    raise _target_registration_required_error(
                        project,
                        real_target,
                    ) from exc
                raise
            _raise_if_self_contact(
                project,
                a,
                project,
                b,
                action="macro_contact_handshake",
            )

            if ttl_seconds < 60:
                await ctx.info(
                    f"[warn] ttl_seconds={ttl_seconds} is below minimum (60s); auto-correcting to 60 seconds."
                )
            now = datetime.now(timezone.utc)
            naive_now = _naive_utc(now)
            exp = naive_now + timedelta(seconds=max(60, ttl_seconds))
            result_expires: datetime | None = exp

            async with get_session() as s:
                existing = await s.execute(
                    select(AgentLink).where(
                        cast(Any, AgentLink.a_project_id) == project.id,
                        cast(Any, AgentLink.a_agent_id) == a.id,
                        cast(Any, AgentLink.b_project_id) == project.id,
                        cast(Any, AgentLink.b_agent_id) == b.id,
                    )
                )
                link = existing.scalars().first()
                if link:
                    link.reason = reason
                    link.updated_ts = naive_now
                    is_active_approved = link.status == "approved" and (
                        link.expires_ts is None or link.expires_ts > naive_now
                    )
                    link.status = "approved"
                    if is_active_approved:
                        if link.expires_ts is None:
                            result_expires = None
                        else:
                            link.expires_ts = max(link.expires_ts, exp)
                            result_expires = link.expires_ts
                    else:
                        link.expires_ts = exp
                        result_expires = exp
                    s.add(link)
                else:
                    link = AgentLink(
                        a_project_id=project.id or 0,
                        a_agent_id=a.id or 0,
                        b_project_id=project.id or 0,
                        b_agent_id=b.id or 0,
                        status="approved",
                        reason=reason,
                        created_ts=naive_now,
                        updated_ts=naive_now,
                        expires_ts=exp,
                    )
                    s.add(link)
                try:
                    await s.commit()
                except IntegrityError:
                    # Another concurrent handshake created the link; treat as idempotent approval.
                    await s.rollback()
                    existing = await s.execute(
                        select(AgentLink).where(
                            cast(Any, AgentLink.a_project_id) == project.id,
                            cast(Any, AgentLink.a_agent_id) == a.id,
                            cast(Any, AgentLink.b_project_id) == project.id,
                            cast(Any, AgentLink.b_agent_id) == b.id,
                        )
                    )
                    link = existing.scalars().first()
                    if link is None:
                        raise
                    link.reason = reason
                    link.updated_ts = naive_now
                    is_active_approved = link.status == "approved" and (
                        link.expires_ts is None or link.expires_ts > naive_now
                    )
                    link.status = "approved"
                    if is_active_approved:
                        if link.expires_ts is None:
                            result_expires = None
                        else:
                            link.expires_ts = max(link.expires_ts, exp)
                            result_expires = link.expires_ts
                    else:
                        link.expires_ts = exp
                        result_expires = exp
                    s.add(link)
                    await s.commit()

            approved_payload = {
                "from": a.name,
                "from_project": project.human_key,
                "to": b.name,
                "to_project": project.human_key,
                "status": "approved",
                "expires_ts": _iso(result_expires) if result_expires is not None else None,
            }
            return {"request": approved_payload, "response": approved_payload, "welcome_message": None}

        request_result = await request_contact(
            ctx=ctx,
            project_key=project_key,
            from_agent=real_requester,
            to_agent=real_target,
            to_project=target_project_key,
            reason=reason,
            ttl_seconds=ttl_seconds,
            registration_token=requester_registration_token,
            format="json",
        )
        request_status = str(request_result.get("status") or "").lower()

        response_result = None
        response_error: dict[str, Any] | None = None
        if auto_accept:
            if request_status == "approved":
                response_result = request_result
            else:
                response_project = await _get_project_by_identifier(target_project_key or project_key)
                response_agent = await _get_agent(response_project, real_target)
                target_auth_token = target_registration_token
                if target_auth_token is None and not _session_is_bound_to_agent(ctx, response_project, response_agent):
                    response_error = {
                        "type": "AUTHENTICATION_REQUIRED",
                        "message": (
                            "auto_accept requires target_registration_token unless this MCP session "
                            "has already authenticated as the target agent."
                        ),
                        "project_key": response_project.human_key,
                        "agent_name": response_agent.name,
                        "token_param": "target_registration_token",
                    }
                else:
                    response_result = await respond_contact(
                        ctx=ctx,
                        project_key=target_project_key or project_key,
                        to_agent=real_target,
                        from_agent=real_requester,
                        accept=True,
                        ttl_seconds=ttl_seconds,
                        from_project=project_key if target_project_key else None,
                        registration_token=target_auth_token,
                        format="json",
                    )

        welcome_message = None
        welcome_error: dict[str, Any] | None = None
        if welcome_subject and welcome_body:
            welcome_project = await _get_project_by_identifier(target_project_key or project_key)
            if response_error is not None:
                welcome_error = {
                    "type": "CONTACT_APPROVAL_REQUIRED",
                    "message": "welcome skipped because auto_accept did not complete; the contact request remains pending.",
                    "project_key": welcome_project.human_key,
                    "agent_name": real_target,
                }
            else:
                try:
                    welcome_recipients = [real_target] if not target_project_key else [f"{real_target}@{target_project_key}"]
                    welcome_idempotency_key = _internal_delivery_idempotency_key(
                        "contact-welcome",
                        {
                            "source_project": project.human_key,
                            "source_agent": real_requester,
                            "target_project": welcome_project.human_key,
                            "target_agent": real_target,
                            "subject": welcome_subject,
                            "body_md": welcome_body,
                            "thread_id": thread_id,
                        },
                    )
                    welcome_payload = await send_message(
                        ctx=ctx,
                        project_key=project_key,
                        sender_name=real_requester,
                        to=welcome_recipients,
                        subject=welcome_subject,
                        body_md=welcome_body,
                        thread_id=thread_id,
                        idempotency_key=welcome_idempotency_key,
                        registration_token=requester_registration_token,
                        format="json",
                    )
                    error_payload = _extract_delivery_error_payload(welcome_payload)
                    if error_payload is not None:
                        welcome_error = _with_delivery_project(error_payload, welcome_project)
                    else:
                        welcome_message = welcome_payload
                except Exception as exc:
                    # surface but do not abort handshake
                    await ctx.debug(f"macro_contact_handshake failed to send welcome: {exc}")
                    welcome_error = _delivery_failure_from_exception(welcome_project, exc)

        result = {
            "request": request_result,
            "response": response_result,
            "welcome_message": welcome_message,
        }
        if response_error is not None:
            result["response_error"] = response_error
        if welcome_error is not None:
            result["welcome_error"] = welcome_error
        return result

    @mcp.tool(name="search_messages")
    @_instrument_tool("search_messages", cluster=CLUSTER_SEARCH, capabilities={"search"}, project_arg="project_key")
    async def search_messages(
        ctx: Context,
        project_key: str,
        query: str,
        limit: int = 20,
        agent_name: Optional[str] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> Any:
        """
        Full-text search over subject and body for a project.

        Tips
        ----
        - SQLite FTS5 syntax supported: phrases ("build plan"), prefix (mig*), boolean (plan AND users)
        - Results are ordered by bm25 score (best matches first)
        - Limit defaults to 20; raise for broad queries

        Query examples
        ---------------
        - Phrase search: `"build plan"`
        - Prefix: `migrat*`
        - Boolean: `plan AND users`
        - Require urgent: `urgent AND deployment`

        Parameters
        ----------
        project_key : str
            Project identifier.
        query : str
            FTS5 query string.
        limit : int
            Max results to return.

        Returns
        -------
        list[dict]
            Each entry: { id, subject, importance, ack_required, created_ts, thread_id, from }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"10","method":"tools/call","params":{"name":"search_messages","arguments":{
          "project_key":"/abs/path/backend","query":"\"build plan\" AND users", "limit": 50
        }}}
        ```
        """
        # Apply the shared limit bounds (issue #191) so search_messages matches
        # fetch_inbox: reject limit<1, clamp >1000.
        limit = _validate_limit(limit)
        project = await _get_project_by_identifier(project_key)
        viewer = await _resolve_authenticated_agent(
            ctx,
            project,
            agent_name=agent_name,
            provided_token=registration_token,
            token_param="registration_token",
            action="search_messages",
        )
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                _rt = _imp.import_module("rich.text")
                Console = _rc.Console
                Panel = _rp.Panel
                Text = _rt.Text
                cons = Console()
                body = Text.assemble(
                    ("project: ", "cyan"), (project.human_key, "white"), "\n",
                    ("query: ", "cyan"), (query[:200], "white"), "\n",
                    ("limit: ", "cyan"), (str(limit), "white"),
                )
                cons.print(Panel(body, title="tool: search_messages", border_style="green"))
            except Exception:
                pass
        if project.id is None:
            raise ValueError("Project must have an id before searching messages.")

        # Sanitize the FTS query - returns None if query can't produce results
        sanitized_query = _sanitize_fts_query(query)
        if sanitized_query is None:
            await ctx.info(f"Search query '{query}' is not searchable, returning empty results.")
            try:
                from fastmcp.tools import ToolResult
                return ToolResult(structured_content={"result": []})
            except Exception:
                return []

        await ensure_schema()
        rows: list[Any] = []
        fts_failed = False
        fts_error_msg: str | None = None
        try:
            async with get_session() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT m.id, m.subject, m.body_md, m.importance, m.ack_required, m.created_ts,
                               m.thread_id, a.name AS sender_name,
                               sp.id AS sender_project_id, sp.human_key AS sender_project, sp.slug AS sender_project_slug
                        FROM fts_messages
                        JOIN messages m ON fts_messages.rowid = m.id
                        JOIN agents a ON m.sender_id = a.id
                        JOIN projects sp ON a.project_id = sp.id
                        WHERE m.project_id = :project_id
                          AND (
                                m.sender_id = :agent_id
                                OR EXISTS (
                                    SELECT 1
                                    FROM message_recipients mr
                                    WHERE mr.message_id = m.id
                                      AND mr.agent_id = :agent_id
                                )
                          )
                          AND fts_messages MATCH :query
                        ORDER BY bm25(fts_messages) ASC
                        LIMIT :limit
                        """
                    ),
                    {"project_id": project.id, "agent_id": viewer.id, "query": sanitized_query, "limit": limit},
                )
                rows = list(result.mappings().all())
        except Exception as fts_err:
            # FTS query syntax error - flag for fallback instead of crashing
            fts_failed = True
            fts_error_msg = str(fts_err)
            logger.warning("FTS query failed, attempting LIKE fallback", extra={"query": sanitized_query, "error": fts_error_msg})

        # Handle FTS failure with LIKE fallback (using a fresh session)
        if fts_failed:
            fallback_terms = _extract_like_terms(query)
            if not fallback_terms:
                await ctx.info(f"Search query '{query}' could not be executed (FTS syntax issue), returning empty results.")
                rows = []
            else:
                clauses = []
                params: dict[str, Any] = {"project_id": project.id, "agent_id": viewer.id, "limit": limit}
                for idx, term in enumerate(fallback_terms):
                    key = f"t{idx}"
                    params[key] = f"%{_like_escape(term)}%"
                    clauses.append(
                        f"(m.subject LIKE :{key} ESCAPE '{_LIKE_ESCAPE_CHAR}' OR m.body_md LIKE :{key} ESCAPE '{_LIKE_ESCAPE_CHAR}')"
                    )
                where_clause = " AND ".join(clauses)
                async with get_session() as session:
                    result = await session.execute(
                        text(
                            f"""
                            SELECT m.id, m.subject, m.body_md, m.importance, m.ack_required, m.created_ts,
                                   m.thread_id, a.name AS sender_name,
                                   sp.id AS sender_project_id, sp.human_key AS sender_project, sp.slug AS sender_project_slug
                            FROM messages m
                            JOIN agents a ON m.sender_id = a.id
                            JOIN projects sp ON a.project_id = sp.id
                            WHERE m.project_id = :project_id
                              AND (
                                    m.sender_id = :agent_id
                                    OR EXISTS (
                                        SELECT 1
                                        FROM message_recipients mr
                                        WHERE mr.message_id = m.id
                                          AND mr.agent_id = :agent_id
                                    )
                              )
                              AND {where_clause}
                            ORDER BY m.created_ts DESC
                            LIMIT :limit
                            """
                        ),
                        params,
                    )
                    rows = list(result.mappings().all())
                await ctx.info(
                    f"FTS query failed; used LIKE fallback with {len(fallback_terms)} term(s), returned {len(rows)} result(s)."
                )

        await ctx.info(f"Search '{query}' returned {len(rows)} messages for project '{project.human_key}'.")
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel(f"results={len(rows)}", title="tool: search_messages — done", border_style="green"))
            except Exception:
                pass
        items: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "id": row["id"],
                "subject": row["subject"],
                "importance": row["importance"],
                "ack_required": row["ack_required"],
                "created_ts": _iso(row["created_ts"]),
                "thread_id": row["thread_id"],
            }
            _apply_sender_identity(
                item,
                message_project_id=project.id,
                sender_name=row["sender_name"],
                sender_project_id=row["sender_project_id"],
                sender_project_human_key=row["sender_project"],
                sender_project_slug=row["sender_project_slug"],
            )
            items.append(item)
        try:
            from fastmcp.tools import ToolResult
            return ToolResult(structured_content={"result": items})
        except Exception:
            return items

    @mcp.tool(name="summarize_thread")
    @_instrument_tool("summarize_thread", cluster=CLUSTER_SEARCH, capabilities={"summarization", "search"}, project_arg="project_key")
    async def summarize_thread(
        ctx: Context,
        project_key: str,
        thread_id: str,
        include_examples: bool = False,
        llm_mode: bool = True,
        llm_model: Optional[str] = None,
        per_thread_limit: int = 50,
        agent_name: Optional[str] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Extract participants, key points, and action items for one or more threads.

        Single-thread mode (thread_id is a single ID):
        - Returns detailed summary with optional example messages
        - Response: { thread_id, summary: {participants[], key_points[], action_items[]}, examples[] }

        Multi-thread mode (thread_id is comma-separated IDs like "TKT-1,TKT-2,TKT-3"):
        - Returns aggregate digest across all threads
        - Response: { threads: [{thread_id, summary}], aggregate: {top_mentions[], key_points[], action_items[]} }

        Parameters
        ----------
        project_key : str
            Project identifier.
        thread_id : str
            Single thread ID for detailed summary, OR comma-separated IDs for aggregate digest.
        include_examples : bool
            If true (single-thread mode only), include up to 3 sample messages.
        llm_mode : bool
            If true and LLM is enabled, refine the summary with AI.
        llm_model : Optional[str]
            Override model name for the LLM call.
        per_thread_limit : int
            Max messages to consider per thread (multi-thread mode).

        Examples
        --------
        Single thread:
        ```json
        {"thread_id": "TKT-123", "include_examples": true}
        ```

        Multiple threads:
        ```json
        {"thread_id": "TKT-1,TKT-2,TKT-3"}
        ```
        """
        # Detect multi-thread mode by checking for comma-separated IDs
        thread_ids = [t.strip() for t in thread_id.split(",") if t.strip()]
        project = await _get_project_by_identifier(project_key)
        viewer = await _resolve_authenticated_agent(
            ctx,
            project,
            agent_name=agent_name,
            provided_token=registration_token,
            token_param="registration_token",
            action="summarize_thread",
        )

        if len(thread_ids) == 1:
            # Single-thread mode: detailed summary with examples
            summary, examples, total_messages = await _compute_thread_summary(
                project,
                thread_ids[0],
                include_examples,
                llm_mode,
                llm_model,
                viewer_agent=viewer,
            )
            await ctx.info(
                f"Summarized thread '{thread_ids[0]}' for project '{project.human_key}' with {total_messages} messages"
            )
            return {"thread_id": thread_ids[0], "summary": summary, "examples": examples}

        # Multi-thread mode: aggregate digest
        if project.id is None:
            raise ValueError("Project must have an id before summarizing threads.")
        await ensure_schema()

        sender_alias = aliased(Agent)
        sender_project_alias = aliased(Project)
        all_mentions: dict[str, int] = {}
        all_actions: list[str] = []
        all_points: list[str] = []
        thread_summaries: list[dict[str, Any]] = []

        async with get_session() as session:
            for tid in thread_ids:
                try:
                    seed_id = int(tid)
                except ValueError:
                    seed_id = None
                criteria = [cast(Any, Message.thread_id) == tid]
                if seed_id is not None:
                    criteria.append(cast(Any, Message.id) == seed_id)
                stmt = (
                    select(Message, sender_alias.name, sender_project_alias.id, sender_project_alias.slug)
                    .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
                    .join(sender_project_alias, cast(Any, sender_alias.project_id == sender_project_alias.id))
                    .where(
                        cast(Any, Message.project_id) == project.id,
                        or_(*criteria),
                        _message_visible_to_agent_clause(viewer.id or 0),
                    )
                    .order_by(asc(cast(Any, Message.created_ts)))
                    .limit(per_thread_limit)
                )
                raw_rows = (await session.execute(stmt)).all()
                rows = [
                    (
                        row[0],
                        _sender_display_name(
                            message_project_id=row[0].project_id,
                            sender_name=row[1],
                            sender_project_id=row[2],
                            sender_project_slug=row[3],
                        ),
                    )
                    for row in raw_rows
                ]
                summary = _summarize_messages(rows)
                # accumulate
                for m in summary.get("mentions", []):
                    name = str(m.get("name", "")).strip()
                    if not name:
                        continue
                    all_mentions[name] = all_mentions.get(name, 0) + int(m.get("count", 0) or 0)
                all_actions.extend(summary.get("action_items", []))
                all_points.extend(summary.get("key_points", []))
                thread_summaries.append({"thread_id": tid, "summary": summary})

        # Lightweight heuristic digest
        top_mentions = sorted(all_mentions.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        aggregate = {
            "top_mentions": [{"name": n, "count": c} for n, c in top_mentions],
            "action_items": all_actions[:25],
            "key_points": all_points[:25],
        }

        # Optional LLM refinement
        if llm_mode and get_settings().llm.enabled and thread_summaries:
            try:
                # Compose compact context combining per-thread key points & actions only
                parts: list[str] = []
                for item in thread_summaries[:8]:
                    s = item["summary"]
                    parts.append(
                        "\n".join(
                            [
                                f"# Thread {item['thread_id']}",
                                "## Key Points",
                                *[f"- {p}" for p in s.get("key_points", [])[:6]],
                                "## Actions",
                                *[f"- {a}" for a in s.get("action_items", [])[:6]],
                            ]
                        )
                    )
                system = (
                    "You are a senior engineer producing a crisp digest across threads. "
                    "Return JSON: { threads: [{thread_id, key_points[], actions[]}], aggregate: {top_mentions[], key_points[], action_items[]} }."
                )
                user = "\n\n".join(parts)
                llm_resp = await complete_system_user(system, user, model=llm_model)
                parsed = _parse_json_safely(llm_resp.content)
                if parsed:
                    agg = parsed.get("aggregate") or {}
                    if agg:
                        for k in ("top_mentions", "key_points", "action_items"):
                            v = agg.get(k)
                            if v:
                                aggregate[k] = v
                    # Replace per-thread summaries' key aggregates if returned
                    revised_threads = []
                    threads_payload = parsed.get("threads") or []
                    if threads_payload:
                        mapping = {str(t.get("thread_id")): t for t in threads_payload}
                        for item in thread_summaries:
                            tid = str(item["thread_id"])
                            if tid in mapping:
                                s = item["summary"].copy()
                                tdata = mapping[tid]
                                if tdata.get("key_points"):
                                    s["key_points"] = tdata["key_points"]
                                if tdata.get("actions"):
                                    s["action_items"] = tdata["actions"]
                                revised_threads.append({"thread_id": item["thread_id"], "summary": s})
                            else:
                                revised_threads.append(item)
                        thread_summaries = revised_threads
            except Exception as e:
                await ctx.debug(f"summarize_thread.llm_skipped: {e}")

        await ctx.info(f"Summarized {len(thread_ids)} thread(s) for project '{project.human_key}'.")
        return {"threads": thread_summaries, "aggregate": aggregate}

    # ── On-demand project-wide summarization (bd-1ia) ────────────────────

    @mcp.tool(name="summarize_recent")
    @_instrument_tool(
        "summarize_recent",
        cluster=CLUSTER_SEARCH,
        capabilities={"summarization", "search"},
        project_arg="project_key",
    )
    async def summarize_recent(
        ctx: Context,
        project_key: str,
        since_hours: float = 1.0,
        llm_mode: bool = True,
        llm_model: Optional[str] = None,
        max_messages: int = 500,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Summarize all recent project messages within a time window.

        Fetches messages from the last ``since_hours`` hours, groups them by
        thread, and produces a combined project-wide summary.  Results are
        stored in the ``message_summaries`` table for fast retrieval via
        ``fetch_summary``.

        Idempotent: if a summary already exists for the same time window
        (within 5-minute tolerance) it is returned from cache.

        Parameters
        ----------
        project_key : str
            Project identifier (slug or human key).
        since_hours : float
            How far back to look (default 1 hour).
        llm_mode : bool
            Use LLM to refine the summary (default True).
        llm_model : str, optional
            Override LLM model name.
        max_messages : int
            Maximum messages to include (default 500, capped at 500).
        format : str, optional
            Output format (json or toon).
        """
        import json as _json

        project = await _get_project_by_identifier(project_key)
        if project.id is None:
            raise ToolExecutionError("PROJECT_NOT_FOUND", "Project has no id.", recoverable=True)

        max_messages = min(max_messages, 500)
        now = _naive_utc()
        window_start = now - timedelta(hours=since_hours)

        # ── Idempotency: check for cached summary within 5-min tolerance ──
        await ensure_schema()
        tolerance = timedelta(minutes=5)
        async with get_session() as session:
            cached_stmt = (
                select(MessageSummary)
                .where(
                    cast(Any, MessageSummary.project_id) == project.id,
                    cast(Any, MessageSummary.start_ts) >= (window_start - tolerance),
                    cast(Any, MessageSummary.start_ts) <= (window_start + tolerance),
                    cast(Any, MessageSummary.end_ts) >= (now - tolerance),
                    cast(Any, MessageSummary.end_ts) <= (now + tolerance),
                )
                .order_by(desc(cast(Any, MessageSummary.created_ts)))
                .limit(1)
            )
            cached_result = await session.execute(cached_stmt)
            cached = cached_result.scalars().first()
            if cached:
                await ctx.info(f"Returning cached summary (id={cached.id}, created={_iso(cached.created_ts)}).")
                return {
                    "id": cached.id,
                    "cached": True,
                    "summary_text": cached.summary_text,
                    "start_ts": _iso(cached.start_ts),
                    "end_ts": _iso(cached.end_ts),
                    "source_message_count": cached.source_message_count,
                    "source_thread_ids": _json.loads(cached.source_thread_ids),
                    "llm_model": cached.llm_model,
                    "cost_usd": cached.cost_usd,
                    "created_ts": _iso(cached.created_ts),
                }

        # ── Fetch messages in window ──
        sender_alias = aliased(Agent)
        sender_project_alias = aliased(Project)
        async with get_session() as session:
            stmt = (
                select(Message, sender_alias.name, sender_project_alias.id, sender_project_alias.slug)
                .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
                .join(sender_project_alias, cast(Any, sender_alias.project_id == sender_project_alias.id))
                .where(
                    cast(Any, Message.project_id) == project.id,
                    cast(Any, Message.created_ts) >= window_start,
                )
                .order_by(asc(cast(Any, Message.created_ts)))
                .limit(max_messages)
            )
            result = await session.execute(stmt)
            raw_rows = result.all()
        rows = [
            (
                row[0],
                _sender_display_name(
                    message_project_id=row[0].project_id,
                    sender_name=row[1],
                    sender_project_id=row[2],
                    sender_project_slug=row[3],
                ),
            )
            for row in raw_rows
        ]

        if not rows:
            await ctx.info(f"No messages in the last {since_hours}h for project '{project.human_key}'.")
            return {
                "id": None,
                "cached": False,
                "summary_text": f"No activity in the last {since_hours} hours.",
                "start_ts": _iso(window_start),
                "end_ts": _iso(now),
                "source_message_count": 0,
                "source_thread_ids": [],
                "llm_model": None,
                "cost_usd": None,
                "created_ts": _iso(now),
            }

        truncated = len(raw_rows) >= max_messages

        # ── Group by thread ──
        threads: dict[str, list[tuple[Message, str]]] = {}
        for msg, sender in rows:
            tid = msg.thread_id or f"msg-{msg.id}"
            threads.setdefault(tid, []).append((msg, sender))

        thread_ids_list = sorted(threads.keys())

        # ── Heuristic summary per thread ──
        all_summaries: list[dict[str, Any]] = []
        for tid, thread_msgs in threads.items():
            s = _summarize_messages(thread_msgs)
            s["thread_id"] = tid
            s["message_count"] = len(thread_msgs)
            all_summaries.append(s)

        # ── Combine into project-wide summary ──
        all_participants: set[str] = set()
        all_key_points: list[str] = []
        all_action_items: list[str] = []
        total_open = 0
        total_done = 0
        for s in all_summaries:
            all_participants.update(s.get("participants", []))
            all_key_points.extend(s.get("key_points", []))
            all_action_items.extend(s.get("action_items", []))
            total_open += s.get("open_actions", 0)
            total_done += s.get("done_actions", 0)

        combined = {
            "participants": sorted(all_participants),
            "key_points": all_key_points[:20],
            "action_items": all_action_items[:20],
            "total_messages": len(rows),
            "total_threads": len(threads),
            "open_actions": total_open,
            "done_actions": total_done,
        }
        if truncated:
            combined["truncated"] = True
            combined["truncation_note"] = f"Limited to {max_messages} most recent messages."

        summary_text = _json.dumps(combined)
        cost_usd: Optional[float] = None
        used_model: Optional[str] = None

        # ── LLM refinement ──
        if llm_mode and get_settings().llm.enabled and rows:
            try:
                excerpts: list[str] = []
                for msg, sender in rows[:30]:
                    tid = msg.thread_id or f"msg-{msg.id}"
                    excerpts.append(f"[{tid}] {sender}: {msg.subject}\n{msg.body_md[:400]}")
                system = (
                    "You are a senior engineering lead. Summarize the following project messages "
                    "from the given time window into a concise JSON with keys: "
                    "key_decisions[], blockers_resolved[], work_completed[], open_questions[], "
                    "participants[], total_messages (int), total_threads (int). "
                    "Be specific and actionable."
                )
                user = f"Time window: last {since_hours}h\n\n" + "\n\n".join(excerpts)
                llm_resp = await complete_system_user(system, user, model=llm_model)
                used_model = llm_resp.model
                cost_usd = getattr(llm_resp, "estimated_cost_usd", None)
                parsed = _parse_json_safely(llm_resp.content)
                if parsed:
                    # Preserve heuristic counts but use LLM text
                    parsed["total_messages"] = len(rows)
                    parsed["total_threads"] = len(threads)
                    if truncated:
                        parsed["truncated"] = True
                    summary_text = _json.dumps(parsed)
            except Exception as e:
                await ctx.debug(f"summarize_recent.llm_skipped: {e}")

        # ── Store summary ──
        async with get_session() as session:
            summary_row = MessageSummary(
                project_id=project.id,
                summary_text=summary_text,
                start_ts=window_start,
                end_ts=now,
                source_message_count=len(rows),
                source_thread_ids=_json.dumps(thread_ids_list),
                llm_model=used_model,
                cost_usd=cost_usd,
            )
            session.add(summary_row)
            await session.commit()
            await session.refresh(summary_row)

        await ctx.info(
            f"Summarized {len(rows)} messages across {len(threads)} threads "
            f"for project '{project.human_key}' (id={summary_row.id})."
        )
        return {
            "id": summary_row.id,
            "cached": False,
            "summary_text": summary_text,
            "start_ts": _iso(window_start),
            "end_ts": _iso(now),
            "source_message_count": len(rows),
            "source_thread_ids": thread_ids_list,
            "llm_model": used_model,
            "cost_usd": cost_usd,
            "created_ts": _iso(summary_row.created_ts),
        }

    @mcp.tool(name="fetch_summary")
    @_instrument_tool(
        "fetch_summary",
        cluster=CLUSTER_SEARCH,
        capabilities={"summarization", "read"},
        project_arg="project_key",
    )
    async def fetch_summary(
        ctx: Context,
        project_key: str,
        since_hours: float = 24.0,
        limit: int = 5,
        format: Optional[str] = None,
    ) -> ToonableList:
        """Retrieve stored project-wide summaries.

        Parameters
        ----------
        project_key : str
            Project identifier.
        since_hours : float
            Return summaries whose end_ts is within this window (default 24h).
        limit : int
            Maximum summaries to return (default 5).
        format : str, optional
            Output format.
        """
        import json as _json

        project = await _get_project_by_identifier(project_key)
        if project.id is None:
            raise ToolExecutionError("PROJECT_NOT_FOUND", "Project has no id.", recoverable=True)

        cutoff = _naive_utc() - timedelta(hours=since_hours)
        await ensure_schema()
        async with get_session() as session:
            stmt = (
                select(MessageSummary)
                .where(
                    cast(Any, MessageSummary.project_id) == project.id,
                    cast(Any, MessageSummary.end_ts) >= cutoff,
                )
                .order_by(desc(cast(Any, MessageSummary.created_ts)))
                .limit(limit)
            )
            result = await session.execute(stmt)
            summaries = result.scalars().all()

        items: list[dict[str, Any]] = []
        for s in summaries:
            items.append({
                "id": s.id,
                "summary_text": s.summary_text,
                "start_ts": _iso(s.start_ts),
                "end_ts": _iso(s.end_ts),
                "source_message_count": s.source_message_count,
                "source_thread_ids": _json.loads(s.source_thread_ids),
                "llm_model": s.llm_model,
                "cost_usd": s.cost_usd,
                "created_ts": _iso(s.created_ts),
            })

        await ctx.info(f"Fetched {len(items)} stored summaries for project '{project.human_key}'.")
        return items

    def _resolve_code_repo_path(code_repo_path: str) -> Path:
        return Path(code_repo_path).expanduser().resolve()

    @mcp.tool(name="install_precommit_guard")
    @_instrument_tool("install_precommit_guard", cluster=CLUSTER_SETUP, capabilities={"infrastructure", "repository"}, project_arg="project_key")
    async def install_precommit_guard(
        ctx: Context,
        project_key: str,
        code_repo_path: str,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        if not settings.worktrees_enabled:
            await ctx.info("Worktree-friendly features are disabled (WORKTREES_ENABLED=0). Skipping guard install.")
            return {"hook": ""}
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel.fit(f"project={project_key}\nrepo={code_repo_path}", title="tool: install_precommit_guard", border_style="green"))
            except Exception:
                pass
        project = await _get_project_by_identifier(project_key)
        repo_path = await asyncio.to_thread(_resolve_code_repo_path, code_repo_path)
        hook_path = await install_guard_script(settings, project.slug, repo_path)
        await _ctx_info_safe(ctx, f"Installed pre-commit guard for project '{project.human_key}' at {hook_path}.")
        return {"hook": str(hook_path)}

    @mcp.tool(name="uninstall_precommit_guard")
    @_instrument_tool("uninstall_precommit_guard", cluster=CLUSTER_SETUP, capabilities={"infrastructure", "repository"})
    async def uninstall_precommit_guard(
        ctx: Context,
        code_repo_path: str,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        if get_settings().tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                Console().print(Panel.fit(f"repo={code_repo_path}", title="tool: uninstall_precommit_guard", border_style="green"))
            except Exception:
                pass
        repo_path = await asyncio.to_thread(_resolve_code_repo_path, code_repo_path)
        removed = await uninstall_guard_script(repo_path)
        if removed:
            await _ctx_info_safe(ctx, f"Removed pre-commit guard at {repo_path / '.git/hooks/pre-commit'}.")
        else:
            await _ctx_info_safe(ctx, f"No pre-commit guard to remove at {repo_path / '.git/hooks/pre-commit'}.")
        return {"removed": removed}

    @mcp.tool(name="file_reservation_paths")
    @_instrument_tool("file_reservation_paths", cluster=CLUSTER_FILE_RESERVATIONS, capabilities={"file_reservations", "repository"}, project_arg="project_key", agent_arg="agent_name")
    async def file_reservation_paths(
        ctx: Context,
        project_key: str,
        agent_name: str,
        paths: list[str],
        ttl_seconds: int = 3600,
        exclusive: bool = True,
        reason: str = "",
        origin: Optional[str] = None,
        execution_id: Optional[str] = None,
        execution_token: Optional[str] = None,
        lifecycle_protocol_version: Optional[int] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Request advisory file reservations (leases) on project-relative paths/globs.

        Semantics
        ---------
        - Conflicts are reported if an overlapping active exclusive reservation exists held by another agent
        - Glob matching is symmetric (`fnmatchcase(a,b)` or `fnmatchcase(b,a)`), including exact matches
        - When granted, a JSON artifact is written under `file_reservations/<sha1(path)>.json` and the DB is updated
        - TTL must be >= 60 seconds (enforced by the server settings/policy)
        - Server-side enforcement (if enabled) only checks reservations that target mail archive paths
          such as `agents/`, `messages/`, or `attachments/`; code repo enforcement is via the pre-commit guard

        Do / Don't
        ----------
        Do:
        - Reserve files before starting edits to signal intent to other agents.
        - Use specific, minimal patterns (e.g., `app/api/*.py`) instead of broad globs.
        - Set a realistic TTL and renew with `renew_file_reservations` if you need more time.

        Don't:
        - Reserve the entire repository or very broad patterns (e.g., `**/*`) unless absolutely necessary.
        - Hold long-lived exclusive reservations when you are not actively editing.
        - Ignore conflicts; resolve them by coordinating with holders or waiting for expiry.

        Parameters
        ----------
        project_key : str
        agent_name : str
        paths : list[str]
            File paths or glob patterns relative to the project workspace (e.g., "app/api/*.py").
        ttl_seconds : int
            Time to live for the file_reservation; expired file_reservations are auto-released.
        exclusive : bool
            If true, exclusive intent; otherwise shared/observe-only.
        reason : str
            Optional explanation (helps humans reviewing Git artifacts).

        Returns
        -------
        dict
            { granted: [{id, path_pattern, exclusive, reason, expires_ts}], conflicts: [{path, holders: [...]}] }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"12","method":"tools/call","params":{"name":"file_reservation_paths","arguments":{
          "project_key":"/owner/backend","agent_name":"codex-wsl-home-1","paths":["app/api/*.py"],
          "ttl_seconds":7200,"exclusive":true,"reason":"migrations"
        }}}
        ```
        """
        # Validate paths is not empty
        if not paths:
            raise ToolExecutionError(
                error_type="EMPTY_PATHS",
                message=(
                    "paths list cannot be empty. Provide at least one file path or glob pattern "
                    "to reserve (e.g., ['src/api/*.py', 'config/settings.yaml'])."
                ),
                recoverable=True,
                data={"provided": paths},
            )

        # Warn on very short TTL (but still allow it for testing scenarios)
        if ttl_seconds < 60:
            await ctx.info(
                f"[warn] ttl_seconds={ttl_seconds} is below recommended minimum (60s). "
                f"Very short TTLs may cause unexpected expiry during processing."
            )

        project = await _get_project_by_identifier(project_key)
        settings = get_settings()
        if settings.tools_log_enabled:
            try:
                import importlib as _imp
                _rc = _imp.import_module("rich.console")
                _rp = _imp.import_module("rich.panel")
                Console = _rc.Console
                Panel = _rp.Panel
                c = Console()
                c.print(Panel("\n".join(paths), title=f"tool: file_reservation_paths — agent={agent_name} ttl={ttl_seconds}s", border_style="green"))
            except Exception:
                pass
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="file_reservation_paths",
        )
        legacy_observe = _legacy_execution_rollout_allowed(settings)
        execution = await _resolve_agent_execution(
            ctx,
            project,
            agent,
            execution_id,
            execution_token,
            action="file_reservation_paths",
            required=not legacy_observe,
            touch_activity=False,
        )
        _requested_protocol_version, protocol_warning = _validate_execution_protocol(
            lifecycle_protocol_version,
            settings=settings,
        )
        normalized_origin = (origin or "auto").strip().lower()
        if normalized_origin not in {"auto", "explicit"}:
            raise ToolExecutionError(
                "INVALID_RESERVATION_ORIGIN",
                "origin must be 'auto' or 'explicit'.",
                data={"origin": origin},
            )
        if project.id is None:
            raise ValueError("Project must have an id before reserving file paths.")
        ancestor_execution_ids = (
            await _load_execution_ancestor_ids(execution)
            if execution is not None
            else []
        )
        compatible_execution_ids = set(ancestor_execution_ids)
        stale_auto_releases = await _expire_stale_file_reservations(project.id)
        if stale_auto_releases:
            summary = ", ".join(
                f"{status.agent.name if status.agent is not None else '<orphaned>'}:{status.reservation.path_pattern}"
                for status in stale_auto_releases[:5]
            )
            extra = f" ({summary})" if summary else ""
            await ctx.info(f"Auto-released {len(stale_auto_releases)} stale file_reservation(s){extra}.")
        project_id = project.id
        # Validate path patterns and warn on suspicious patterns
        for pattern in paths:
            warning = _detect_suspicious_file_reservation(pattern)
            if warning:
                await ctx.info(f"[warn] {warning}")

        granted: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        archive = await ensure_archive(settings, project.slug)
        ctx_branch: Optional[str] = None
        ctx_worktree: Optional[str] = None
        try:
            with _git_repo(project.human_key) as repo:
                try:
                    ctx_branch = repo.active_branch.name
                except Exception:
                    try:
                        ctx_branch = repo.git.rev_parse("--abbrev-ref", "HEAD").strip()
                    except Exception:
                        ctx_branch = None
                try:
                    ctx_worktree = Path(repo.working_tree_dir or "").name or None
                except Exception:
                    ctx_worktree = None
        except Exception:
            pass
        async with _archive_write_lock(archive):
            # Use BEGIN IMMEDIATE to acquire a fresh WAL snapshot, preventing
            # stale reads that cause duplicate exclusive holders (#129) and
            # phantom conflicts after release (#130).  The entire read-check-
            # write cycle runs inside a single IMMEDIATE transaction so that
            # concurrent callers are serialised at the SQLite lock level.
            async with get_immediate_session() as session:
                await _revalidate_agent_lifetime_in_session(
                    session,
                    project=project,
                    agent=agent,
                    execution=execution,
                    require_active_execution=execution is not None,
                    touch_execution_ts=(
                        _naive_utc() if execution is not None else None
                    ),
                    action="file_reservation_paths",
                )
                existing_rows = await session.execute(
                    select(FileReservation, Agent.name)
                    .join(Agent, cast(Any, FileReservation.agent_id) == Agent.id)
                    .where(
                        cast(Any, FileReservation.project_id) == project_id,
                        cast(Any, FileReservation.released_ts).is_(None),
                        cast(Any, FileReservation.expires_ts) > _naive_utc(),
                    )
                )
                existing_reservations = [(row[0], row[1]) for row in existing_rows.all()]

                # Build union PathSpec for fast conflict pre-filtering (O(n+m) instead of O(n*m))
                union_spec = _build_reservation_union_spec(
                    existing_reservations,
                    execution.id if execution is not None else None,
                    cast(int, agent.id),
                    exclusive,
                    compatible_execution_ids,
                )

                # Pre-compute which paths might conflict using the union spec
                potentially_conflicting_paths: set[str] = set()
                if union_spec is not None:
                    # Normalize paths for matching (same normalization as pattern matching)
                    normalized_paths = [_normalize_pathspec_pattern(p) for p in paths]
                    # Match all normalized paths against union in a single pass
                    matching_normalized = set(union_spec.match_files(normalized_paths))
                    # Build set of original paths that might conflict
                    for orig_path, norm_path in zip(paths, normalized_paths, strict=True):
                        # `match_files` only treats the candidate as a concrete FILE matched
                        # against existing patterns, so it cannot detect reverse-glob conflicts
                        # (a candidate glob like "app/**" enclosing an existing literal like
                        # "app/models/user.py"). Always defer globbed candidates to the detailed
                        # _file_reservations_conflict check, which is symmetric. (#193)
                        if norm_path in matching_normalized or _contains_glob(norm_path):
                            potentially_conflicting_paths.add(orig_path)
                else:
                    # Fallback: all paths potentially conflict (PathSpec unavailable)
                    potentially_conflicting_paths = set(paths)

                for path in paths:
                    conflicting_holders: list[dict[str, Any]] = []
                    existing_self_reservation = next(
                        (
                            file_reservation_record
                            for file_reservation_record, _holder_name in existing_reservations
                            if (
                                (
                                    execution is not None
                                    and file_reservation_record.execution_id
                                    == execution.id
                                )
                                or (
                                    execution is None
                                    and file_reservation_record.execution_id is None
                                    and file_reservation_record.agent_id == agent.id
                                )
                            )
                            and file_reservation_record.path_pattern == path
                        ),
                        None,
                    )

                    # Fast path: skip detailed check if path cannot conflict with any reservation
                    if path in potentially_conflicting_paths:
                        # Slow path: detailed attribution for potentially conflicting paths only
                        for file_reservation_record, holder_name in existing_reservations:
                            if _file_reservations_conflict(
                                file_reservation_record,
                                path,
                                exclusive,
                                execution.id if execution is not None else None,
                                cast(int, agent.id),
                                compatible_execution_ids,
                            ):
                                conflicting_holders.append(
                                    {
                                        "agent": holder_name,
                                        "execution_id": file_reservation_record.execution_id,
                                        "origin": file_reservation_record.origin,
                                        "path_pattern": file_reservation_record.path_pattern,
                                        "exclusive": file_reservation_record.exclusive,
                                        "expires_ts": _iso(file_reservation_record.expires_ts),
                                    }
                                )

                    if conflicting_holders:
                        # Advisory model: still grant the file_reservation but surface conflicts
                        conflicts.append({"path": path, "holders": conflicting_holders})
                    requested_exp = _naive_utc() + timedelta(seconds=ttl_seconds)
                    # Track whether this reservation already existed for this agent so
                    # callers (e.g. macro auto-release) can avoid releasing a reservation
                    # the agent held before this call. (#196)
                    reused_existing = existing_self_reservation is not None
                    if existing_self_reservation is not None:
                        current_exp = existing_self_reservation.expires_ts
                        if getattr(current_exp, "tzinfo", None) is not None:
                            current_exp = _naive_utc(current_exp)
                        existing_self_reservation.exclusive = exclusive
                        if normalized_origin == "explicit":
                            existing_self_reservation.origin = "explicit"
                        if reason or not existing_self_reservation.reason:
                            existing_self_reservation.reason = reason
                        existing_self_reservation.expires_ts = max(requested_exp, current_exp)
                        file_reservation = existing_self_reservation
                        session.add(file_reservation)
                    else:
                        # Create reservation inline within the IMMEDIATE transaction
                        # (instead of _create_file_reservation which opens its own session)
                        file_reservation = FileReservation(
                            project_id=project.id,
                            agent_id=agent.id,
                            execution_id=execution.id if execution is not None else None,
                            origin=normalized_origin,
                            path_pattern=path,
                            exclusive=exclusive,
                            reason=reason,
                            expires_ts=requested_exp,
                        )
                        session.add(file_reservation)
                        await session.flush()  # Assigns id without committing
                    granted.append(
                        {
                            "id": file_reservation.id,
                            "execution_id": file_reservation.execution_id,
                            "ancestor_execution_ids": ancestor_execution_ids,
                            "origin": file_reservation.origin,
                            "legacy_unscoped": execution is None,
                            "orphaned": False,
                            "path_pattern": file_reservation.path_pattern,
                            "exclusive": file_reservation.exclusive,
                            "reason": file_reservation.reason,
                            "expires_ts": _iso(file_reservation.expires_ts),
                            "reused": reused_existing,
                        }
                    )
                    existing_reservations.append((file_reservation, agent.name))
                # Commit all reservations atomically within the IMMEDIATE tx
                await session.commit()
            if granted:
                reservation_ids = [
                    int(item["id"])
                    for item in granted
                    if item.get("id") is not None
                ]
                async with get_session() as version_session:
                    current_rows = (
                        await version_session.execute(
                            select(FileReservation, Agent)
                            .outerjoin(
                                Agent,
                                cast(Any, FileReservation.agent_id) == Agent.id,
                            )
                            .where(
                                cast(Any, FileReservation.id).in_(
                                    reservation_ids
                                )
                            )
                            .order_by(asc(cast(Any, FileReservation.id)))
                        )
                    ).all()
                records = [
                    cast(tuple[FileReservation, Optional[Agent]], row)
                    for row in current_rows
                ]
                revisions = [
                    (reservation.id, reservation.archive_revision)
                    for reservation, _agent in records
                    if reservation.id is not None
                ]
                # DB is authoritative. A failed or partial archive publication
                # leaves these exact revisions pending for the next ordinary
                # operation/reaper instead of deleting ownership state while a
                # guard artifact may already have reached disk or Git.
                await _write_file_reservation_records(
                    project,
                    records,
                    archive=archive,
                    archive_locked=True,
                    branch_override=ctx_branch,
                    worktree_override=ctx_worktree,
                )
                acknowledged = await _ack_file_reservation_archive_revisions(
                    revisions
                )
                if acknowledged != len(revisions):
                    await _reconcile_pending_file_reservation_artifacts(
                        project,
                        archive=archive,
                        archive_locked=True,
                    )
        await ctx.info(f"Issued {len(granted)} file_reservations for '{agent.name}'. Conflicts: {len(conflicts)}")
        # Surface per-call enforcement mode so wrappers (e.g. ntm's `lock`
        # subcommand) can warn the operator that code-repo paths are
        # advisory-only. Server-side conflict detection only fully covers
        # mail-archive paths (see `_looks_like_archive_path` and the
        # docstring); code-repo paths rely on the pre-commit guard as the
        # authoritative gate. Without an explicit signal in the JSON
        # response, downstream tools have no programmatic way to detect
        # the advisory-only mode short of parsing the docstring. (#162)
        warnings_list: list[str] = []
        if execution is None:
            warnings_list.append(
                "execution_required_after_rollout: reservation was accepted as a legacy "
                "unscoped claim because AGENT_EXECUTION_ENFORCEMENT_MODE=observe; "
                "start_agent_execution and pass execution_id before enforce mode is enabled."
            )
        if protocol_warning is not None:
            warnings_list.append(protocol_warning)
        advisory_only_paths = [p for p in paths if not _looks_like_archive_path(p)]
        if advisory_only_paths:
            warnings_list.append(
                "enforcement_off_for_code_paths: "
                f"{len(advisory_only_paths)} of {len(paths)} reserved paths are "
                "code-repo paths; server-side exclusivity is advisory only. "
                "Install the pre-commit guard via `install_precommit_guard` for "
                "the authoritative reservation gate."
            )
        return {
            "granted": granted,
            "conflicts": conflicts,
            "warnings": warnings_list,
            "execution_id": execution.id if execution is not None else None,
            "ancestor_execution_ids": ancestor_execution_ids,
            "legacy_unscoped": execution is None,
        }

    # FastMCP 3 decorators return the original function for safe composition.
    # Keep a non-shadowed reference so the macro continues to work even when
    # the public helper is hidden by an instance-scoped tool filter.
    file_reservation_paths_direct = file_reservation_paths

    @mcp.tool(name="release_file_reservations")
    @_instrument_tool("release_file_reservations", cluster=CLUSTER_FILE_RESERVATIONS, capabilities={"file_reservations"}, project_arg="project_key", agent_arg="agent_name")
    async def release_file_reservations_tool(
        ctx: Context,
        project_key: str,
        agent_name: str,
        paths: Optional[list[str]] = None,
        file_reservation_ids: Optional[list[int]] = None,
        execution_id: Optional[str] = None,
        execution_token: Optional[str] = None,
        lifecycle_protocol_version: Optional[int] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Release active file reservations held by an agent.

        Behavior
        --------
        - If both `paths` and `file_reservation_ids` are omitted, all active reservations for the agent are released
        - Otherwise, restricts release to matching ids and/or path patterns
        - JSON artifacts stay in Git for audit; DB records get `released_ts`

        Returns
        -------
        dict
            { released: int, released_at: iso8601 }

        Idempotency
        -----------
        - Safe to call repeatedly. Releasing an already-released (or non-existent) reservation is a no-op.

        Examples
        --------
        Release all active reservations for agent:
        ```json
        {"jsonrpc":"2.0","id":"13","method":"tools/call","params":{"name":"release_file_reservations","arguments":{
          "project_key":"/owner/backend","agent_name":"codex-wsl-home-1"
        }}}
        ```

        Release by ids:
        ```json
        {"jsonrpc":"2.0","id":"14","method":"tools/call","params":{"name":"release_file_reservations","arguments":{
          "project_key":"/owner/backend","agent_name":"codex-wsl-home-1","file_reservation_ids":[101,102]
        }}}
        ```
        """
        if paths == []:
            raise ToolExecutionError(
                error_type="EMPTY_PATHS",
                message="paths cannot be an empty list. Omit paths to release all active reservations, or provide at least one path pattern.",
                recoverable=True,
                data={"provided": paths},
            )
        if file_reservation_ids == []:
            raise ToolExecutionError(
                error_type="EMPTY_IDS",
                message="file_reservation_ids cannot be an empty list. Omit file_reservation_ids to release all active reservations, or provide at least one reservation id.",
                recoverable=True,
                data={"provided": file_reservation_ids},
            )
        if get_settings().tools_log_enabled:
            try:
                from rich.console import Console
                from rich.panel import Panel

                details = [
                    f"project={project_key}",
                    f"agent={agent_name}",
                    f"paths={len(paths or [])}",
                    f"ids={len(file_reservation_ids or [])}",
                ]
                Console().print(Panel.fit("\n".join(details), title="tool: release_file_reservations", border_style="green"))
            except Exception:
                pass
        try:
            project = await _get_project_by_identifier(project_key)
            legacy_observe = _legacy_execution_rollout_allowed(get_settings())
            agent = await _authenticate_agent(
                ctx,
                project,
                agent_name,
                registration_token,
                token_param="registration_token",
                action="release_file_reservations",
            )
            execution = await _resolve_agent_execution(
                ctx,
                project,
                agent,
                execution_id,
                execution_token,
                action="release_file_reservations",
                required=not legacy_observe,
                require_active=False,
                touch_activity=False,
            )
            _requested_protocol_version, protocol_warning = _validate_execution_protocol(
                lifecycle_protocol_version,
                settings=get_settings(),
            )
            if project.id is None or agent.id is None:
                raise ValueError("Project and agent must have ids before releasing file_reservations.")
            await ensure_schema()
            now = datetime.now(timezone.utc)
            naive_now = _naive_utc(now)  # Compute once for consistency
            reservations: list[FileReservation] = []
            # Use BEGIN IMMEDIATE so the release is immediately visible to
            # subsequent reserve calls on other connections (#130).
            async with get_immediate_session() as session:
                await _revalidate_agent_lifetime_in_session(
                    session,
                    project=project,
                    agent=agent,
                    execution=execution,
                    require_active_execution=False,
                    touch_execution_ts=(
                        naive_now if execution is not None else None
                    ),
                    action="release_file_reservations",
                )
                select_stmt = (
                    select(FileReservation)
                    .where(
                        cast(Any, FileReservation.project_id) == project.id,
                        cast(Any, FileReservation.agent_id) == agent.id,
                        (
                            cast(Any, FileReservation.execution_id) == execution.id
                            if execution is not None
                            else cast(Any, FileReservation.execution_id).is_(None)
                        ),
                        cast(Any, FileReservation.released_ts).is_(None),
                        or_(
                            cast(Any, FileReservation.expires_ts).is_(None),
                            cast(Any, FileReservation.expires_ts) > naive_now,
                        ),
                    )
                )
                if file_reservation_ids:
                    select_stmt = select_stmt.where(cast(Any, FileReservation.id).in_(file_reservation_ids))
                if paths:
                    select_stmt = select_stmt.where(cast(Any, FileReservation.path_pattern).in_(paths))
                result = await session.execute(select_stmt)
                reservations = list(result.scalars().all())
                if reservations:
                    ids = [res.id for res in reservations if res.id is not None]
                    if ids:
                        await session.execute(
                            update(FileReservation)
                            .where(
                                cast(Any, FileReservation.project_id) == project.id,
                                cast(Any, FileReservation.agent_id) == agent.id,
                                (
                                    cast(Any, FileReservation.execution_id)
                                    == execution.id
                                    if execution is not None
                                    else cast(Any, FileReservation.execution_id).is_(None)
                                ),
                                cast(Any, FileReservation.released_ts).is_(None),
                                cast(Any, FileReservation.id).in_(ids),
                            )
                            .values(released_ts=naive_now)  # Use naive UTC for SQLite compatibility
                        )
                await session.commit()
            affected = len(reservations)
            for reservation in reservations:
                reservation.released_ts = naive_now
            await _reconcile_pending_file_reservation_artifacts(project)
            await ctx.info(f"Released {affected} file_reservations for '{agent.name}'.")
            response: dict[str, Any] = {
                "released": affected,
                "released_at": _iso(now),
                "execution_id": execution.id if execution is not None else None,
            }
            response_warnings: list[str] = []
            if execution is None:
                response_warnings.append(
                    "execution_required_after_rollout: only legacy unscoped reservations were released."
                )
            if protocol_warning is not None:
                response_warnings.append(protocol_warning)
            if response_warnings:
                response["warnings"] = response_warnings
            return response
        except Exception as exc:
            if get_settings().tools_log_enabled:
                try:
                    import importlib as _imp
                    _rc = _imp.import_module("rich.console")
                    _rj = _imp.import_module("rich.json")
                    Console = _rc.Console
                    JSON = _rj.JSON
                    Console().print(JSON.from_data({"error": str(exc)}))
                except Exception:
                    pass
            raise

    @mcp.tool(name="force_release_file_reservation")
    @_instrument_tool(
        "force_release_file_reservation",
        cluster=CLUSTER_FILE_RESERVATIONS,
        capabilities={"file_reservations", "repository"},
        project_arg="project_key",
        agent_arg="agent_name",
    )
    async def force_release_file_reservation(
        ctx: Context,
        project_key: str,
        agent_name: str,
        file_reservation_id: int,
        notify_previous: bool = True,
        note: str = "",
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """Recover one stale claim owned by this authenticated durable Agent."""
        project = await _get_project_by_identifier(project_key)
        actor = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="force_release_file_reservation",
        )
        if project.id is None:
            raise ValueError("Project must have an id before releasing file_reservations.")

        await ensure_schema()
        await _reconcile_pending_file_reservation_artifacts(project)
        async with get_session() as session:
            result = await session.execute(
                select(FileReservation, Agent)
                .join(Agent, cast(Any, FileReservation.agent_id) == Agent.id)
                .where(
                    cast(Any, FileReservation.id) == file_reservation_id,
                    cast(Any, FileReservation.project_id) == project.id,
                )
            )
            row = result.first()
        if not row:
            raise ToolExecutionError(
                "NOT_FOUND",
                f"File reservation id={file_reservation_id} not found for project '{project.human_key}'.",
                recoverable=True,
                data={"file_reservation_id": file_reservation_id},
            )

        reservation, holder = row
        if actor.id != holder.id:
            raise ToolExecutionError(
                "RESERVATION_OWNERSHIP_MISMATCH",
                "An Agent may force-release only its own stale reservation.",
                recoverable=False,
                data={
                    "file_reservation_id": file_reservation_id,
                    "owner_agent_id": holder.id,
                    "actor_agent_id": actor.id,
                },
            )
        if reservation.released_ts is not None:
            return {
                "released": 0,
                "released_at": _iso(reservation.released_ts),
                "already_released": True,
            }

        now = datetime.now(timezone.utc)
        naive_now = _naive_utc(now)
        if reservation.expires_ts is not None and reservation.expires_ts <= naive_now:
            # Normalize TTL-expired reservations to released before applying stale-activity heuristics.
            async with get_immediate_session() as session:
                await _revalidate_agent_lifetime_in_session(
                    session,
                    project=project,
                    agent=actor,
                    action="force_release_file_reservation",
                )
                await session.execute(
                    update(FileReservation)
                    .where(
                        cast(Any, FileReservation.id) == file_reservation_id,
                        cast(Any, FileReservation.project_id) == project.id,
                        cast(Any, FileReservation.released_ts).is_(None),
                        cast(Any, FileReservation.expires_ts) <= naive_now,
                    )
                    .values(released_ts=naive_now)
                )
                await session.commit()

            reservation.released_ts = naive_now
            await _reconcile_pending_file_reservation_artifacts(project)
            return {
                "released": 0,
                "released_at": _iso(naive_now),
                "already_released": True,
                "expired": True,
            }

        statuses = await _collect_file_reservation_statuses(project, include_released=False)
        target_status = next((status for status in statuses if status.reservation.id == reservation.id), None)
        if target_status is None:
            raise ToolExecutionError(
                "NOT_FOUND",
                "Unable to evaluate reservation status; it may have been released concurrently.",
                recoverable=True,
                data={"file_reservation_id": file_reservation_id},
            )

        if not target_status.stale:
            raise ToolExecutionError(
                "RESERVATION_ACTIVE",
                "Reservation still shows recent activity; refusing forced release.",
                recoverable=True,
                data={
                    "file_reservation_id": file_reservation_id,
                    "execution_id": target_status.execution_id,
                    "execution_status": target_status.execution_status,
                    "last_execution_activity_ts": _iso(
                        target_status.last_execution_activity
                    )
                    if target_status.last_execution_activity
                    else None,
                    "stale_reasons": target_status.stale_reasons,
                },
            )

        # Use BEGIN IMMEDIATE so the forced release is immediately visible (#130).
        async with get_immediate_session() as session:
            await _revalidate_agent_lifetime_in_session(
                session,
                project=project,
                agent=actor,
                action="force_release_file_reservation",
            )
            await session.execute(
                update(FileReservation)
                .where(
                    cast(Any, FileReservation.id) == file_reservation_id,
                    cast(Any, FileReservation.released_ts).is_(None),
                )
                .values(released_ts=naive_now)  # Use naive UTC for SQLite compatibility
            )
            await session.commit()

        reservation.released_ts = naive_now
        await _reconcile_pending_file_reservation_artifacts(project)
        settings = get_settings()
        grace_seconds = int(settings.file_reservation_activity_grace_seconds)
        inactivity_seconds = int(settings.file_reservation_inactivity_seconds)

        summary = {
            "id": reservation.id,
            "agent": holder.name,
            "execution_id": target_status.execution_id,
            "execution_status": target_status.execution_status,
            "ancestor_execution_ids": target_status.ancestor_execution_ids,
            "origin": reservation.origin,
            "orphaned": target_status.orphaned,
            "legacy_unscoped": target_status.legacy_unscoped,
            "path_pattern": reservation.path_pattern,
            "exclusive": reservation.exclusive,
            "reason": reservation.reason,
            "created_ts": _iso(reservation.created_ts),
            "expires_ts": _iso(reservation.expires_ts),
            "released_ts": _iso(reservation.released_ts),
            "stale_reasons": target_status.stale_reasons,
            "last_agent_activity_ts": _iso(target_status.last_agent_activity) if target_status.last_agent_activity else None,
            "last_execution_activity_ts": _iso(target_status.last_execution_activity) if target_status.last_execution_activity else None,
            "last_mail_activity_ts": _iso(target_status.last_mail_activity) if target_status.last_mail_activity else None,
            "last_filesystem_activity_ts": _iso(target_status.last_fs_activity) if target_status.last_fs_activity else None,
            "last_git_activity_ts": _iso(target_status.last_git_activity) if target_status.last_git_activity else None,
        }

        await ctx.info(
            f"Force released reservation {file_reservation_id} held by '{holder.name}' on '{reservation.path_pattern}'."
        )

        notified = False
        notification_error: dict[str, Any] | None = None
        if notify_previous and holder.name != actor.name:
            reasons_md = "\n".join(f"- {reason}" for reason in target_status.stale_reasons)
            extras: list[str] = []
            if target_status.last_agent_activity:
                delta = now - target_status.last_agent_activity
                extras.append(f"last agent activity ≈ {int(delta.total_seconds() // 60)} minutes ago")
            if target_status.last_mail_activity:
                delta = now - target_status.last_mail_activity
                extras.append(f"last mail activity ≈ {int(delta.total_seconds() // 60)} minutes ago")
            if target_status.last_fs_activity:
                delta = now - target_status.last_fs_activity
                extras.append(f"last filesystem touch ≈ {int(delta.total_seconds() // 60)} minutes ago")
            if target_status.last_git_activity:
                delta = now - target_status.last_git_activity
                extras.append(f"last git commit ≈ {int(delta.total_seconds() // 60)} minutes ago")
            extras.append(f"inactivity threshold={inactivity_seconds}s grace={grace_seconds}s")
            extra_md = "\n".join(f"- {line}" for line in extras if line)
            body_lines = [
                f"Hi {holder.name},",
                "",
                f"I released your file reservation on `{reservation.path_pattern}` because it looked abandoned.",
                "",
                "Observed signals:",
                reasons_md or "- (none)",
            ]
            if extra_md:
                body_lines.extend(["", "Details:", extra_md])
            if note:
                body_lines.extend(["", f"Additional note from {actor.name}:", note.strip()])
            body_lines.extend(
                [
                    "",
                    "If you still need this reservation, please re-acquire it via `file_reservation_paths`.",
                ]
            )
            try:
                release_idempotency_key = _internal_delivery_idempotency_key(
                    "file-reservation-release",
                    {
                        "project": project.human_key,
                        "reservation_id": file_reservation_id,
                        "released_ts": _iso(reservation.released_ts),
                        "actor": actor.name,
                        "holder": holder.name,
                    },
                )
                notification_payload = await send_message(
                    ctx=ctx,
                    project_key=project_key,
                    sender_name=agent_name,
                    registration_token=registration_token,
                    to=[holder.name],
                    subject=f"[file-reservations] Released stale lock on {reservation.path_pattern}",
                    body_md="\n".join(body_lines),
                    idempotency_key=release_idempotency_key,
                    format="json",
                )
                error_payload = _extract_delivery_error_payload(notification_payload)
                if error_payload is not None:
                    notification_error = _with_delivery_project(error_payload, project)
                else:
                    notified = True
            except Exception as exc:
                notified = False
                notification_error = _delivery_failure_from_exception(project, exc)

        summary["notified"] = notified
        if notification_error is not None:
            summary["notification_error"] = notification_error
        return {"released": 1, "released_at": _iso(now), "reservation": summary}
    @mcp.tool(name="renew_file_reservations")
    @_instrument_tool("renew_file_reservations", cluster=CLUSTER_FILE_RESERVATIONS, capabilities={"file_reservations"}, project_arg="project_key", agent_arg="agent_name")
    async def renew_file_reservations(
        ctx: Context,
        project_key: str,
        agent_name: str,
        extend_seconds: int = 1800,
        paths: Optional[list[str]] = None,
        file_reservation_ids: Optional[list[int]] = None,
        execution_id: Optional[str] = None,
        execution_token: Optional[str] = None,
        lifecycle_protocol_version: Optional[int] = None,
        registration_token: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Extend expiry for active file reservations held by an agent without reissuing them.

        Parameters
        ----------
        project_key : str
            Project slug or human key.
        agent_name : str
            Agent identity who owns the reservations.
        extend_seconds : int
            Seconds to extend from the later of now or current expiry (min 60s).
        paths : Optional[list[str]]
            Restrict renewals to matching path patterns.
        file_reservation_ids : Optional[list[int]]
            Restrict renewals to matching reservation ids.

        Returns
        -------
        dict
            { renewed: int, file_reservations: [{id, path_pattern, old_expires_ts, new_expires_ts}] }
        """
        if paths == []:
            raise ToolExecutionError(
                error_type="EMPTY_PATHS",
                message="paths cannot be an empty list. Omit paths to renew all active reservations, or provide at least one path pattern.",
                recoverable=True,
                data={"provided": paths},
            )
        if file_reservation_ids == []:
            raise ToolExecutionError(
                error_type="EMPTY_IDS",
                message="file_reservation_ids cannot be an empty list. Omit file_reservation_ids to renew all active reservations, or provide at least one reservation id.",
                recoverable=True,
                data={"provided": file_reservation_ids},
            )
        if get_settings().tools_log_enabled:
            try:
                from rich.console import Console
                from rich.panel import Panel

                meta = [
                    f"project={project_key}",
                    f"agent={agent_name}",
                    f"extend={extend_seconds}s",
                    f"paths={len(paths or [])}",
                    f"ids={len(file_reservation_ids or [])}",
                ]
                Console().print(Panel.fit("\n".join(meta), title="tool: renew_file_reservations", border_style="green"))
            except Exception:
                pass
        project = await _get_project_by_identifier(project_key)
        legacy_observe = _legacy_execution_rollout_allowed(get_settings())
        agent = await _authenticate_agent(
            ctx,
            project,
            agent_name,
            registration_token,
            token_param="registration_token",
            action="renew_file_reservations",
        )
        execution = await _resolve_agent_execution(
            ctx,
            project,
            agent,
            execution_id,
            execution_token,
            action="renew_file_reservations",
            required=not legacy_observe,
            touch_activity=False,
        )
        _requested_protocol_version, protocol_warning = _validate_execution_protocol(
            lifecycle_protocol_version,
            settings=get_settings(),
        )
        if project.id is None or agent.id is None:
            raise ValueError("Project and agent must have ids before renewing file_reservations.")
        await ensure_schema()
        now = datetime.now(timezone.utc)
        bump = max(60, int(extend_seconds))
        stale_auto_releases = await _expire_stale_file_reservations(project.id)
        if stale_auto_releases:
            summary = ", ".join(
                f"{status.agent.name if status.agent is not None else '<orphaned>'}:{status.reservation.path_pattern}"
                for status in stale_auto_releases[:5]
            )
            extra = f" ({summary})" if summary else ""
            await ctx.info(f"Auto-released {len(stale_auto_releases)} stale file_reservation(s){extra}.")

        # Use a single IMMEDIATE session for the read + write so the
        # renewal is atomic and immediately visible to other connections.
        async with get_immediate_session() as session:
            await _revalidate_agent_lifetime_in_session(
                session,
                project=project,
                agent=agent,
                execution=execution,
                require_active_execution=execution is not None,
                touch_execution_ts=(
                    _naive_utc(now) if execution is not None else None
                ),
                action="renew_file_reservations",
            )
            stmt = (
                select(FileReservation)
                .where(
                    cast(Any, FileReservation.project_id) == project.id,
                    cast(Any, FileReservation.agent_id) == agent.id,
                    (
                        cast(Any, FileReservation.execution_id) == execution.id
                        if execution is not None
                        else cast(Any, FileReservation.execution_id).is_(None)
                    ),
                    cast(Any, FileReservation.released_ts).is_(None),
                    cast(Any, FileReservation.expires_ts) > _naive_utc(now),
                )
                .order_by(asc(cast(Any, FileReservation.expires_ts)))
            )
            if file_reservation_ids:
                stmt = stmt.where(cast(Any, FileReservation.id).in_(file_reservation_ids))
            if paths:
                stmt = stmt.where(cast(Any, FileReservation.path_pattern).in_(paths))
            result = await session.execute(stmt)
            file_reservations: list[FileReservation] = list(result.scalars().all())

            if not file_reservations:
                await session.commit()
                await ctx.info(f"No active file_reservations to renew for '{agent.name}'.")
                empty_response: dict[str, Any] = {
                    "renewed": 0,
                    "execution_id": execution.id if execution is not None else None,
                    "file_reservations": [],
                }
                empty_warnings: list[str] = []
                if execution is None:
                    empty_warnings.append(
                        "execution_required_after_rollout: only legacy unscoped reservations were renewed."
                    )
                if protocol_warning is not None:
                    empty_warnings.append(protocol_warning)
                if empty_warnings:
                    empty_response["warnings"] = empty_warnings
                return empty_response

            updated: list[dict[str, Any]] = []
            for file_reservation in file_reservations:
                old_exp = file_reservation.expires_ts
                if getattr(old_exp, "tzinfo", None) is None:
                    from datetime import timezone as _tz
                    old_exp = old_exp.replace(tzinfo=_tz.utc)
                base = old_exp if old_exp > now else now
                # Convert to naive UTC for SQLite compatibility
                file_reservation.expires_ts = _naive_utc(base + timedelta(seconds=bump))
                session.add(file_reservation)
                updated.append(
                    {
                        "id": file_reservation.id,
                        "execution_id": file_reservation.execution_id,
                        "path_pattern": file_reservation.path_pattern,
                        "old_expires_ts": _iso(old_exp),
                        "new_expires_ts": _iso(file_reservation.expires_ts),
                    }
                )
            await session.commit()

        # Publish the exact committed revisions. A failed write leaves each
        # renewal pending in DB for the next ordinary sweep or operation.
        await _reconcile_pending_file_reservation_artifacts(project)
        await ctx.info(f"Renewed {len(updated)} file_reservation(s) for '{agent.name}'.")
        response: dict[str, Any] = {
            "renewed": len(updated),
            "execution_id": execution.id if execution is not None else None,
            "file_reservations": updated,
        }
        response_warnings: list[str] = []
        if execution is None:
            response_warnings.append(
                "execution_required_after_rollout: only legacy unscoped reservations were renewed."
            )
        if protocol_warning is not None:
            response_warnings.append(protocol_warning)
        if response_warnings:
            response["warnings"] = response_warnings
        return response

    # --- Build slots (coarse concurrency control) --------------------------------------------
    # Only registered when WORKTREES_ENABLED=1 to reduce token overhead for single-worktree setups

    if settings.worktrees_enabled:
        def _slot_dir(archive: ProjectArchive, slot: str) -> Path:
            safe = safe_build_path_component(slot)
            return archive.root / "build_slots" / safe

        def _compute_branch(path: str) -> Optional[str]:
            try:
                with _git_repo(path) as repo:
                    try:
                        return repo.active_branch.name
                    except Exception:
                        return repo.git.rev_parse("--abbrev-ref", "HEAD").strip()
            except Exception:
                return None

        def _is_active_build_slot_lease(data: dict[str, Any], now: datetime) -> bool:
            if data.get("released_ts"):
                return False
            exp = data.get("expires_ts")
            if exp:
                try:
                    if datetime.fromisoformat(exp) <= now:
                        return False
                except Exception:
                    pass
            return True

        def _read_active_slots(slot_path: Path, now: datetime) -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            if not slot_path.exists():
                return results
            for f in slot_path.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and _is_active_build_slot_lease(cast(dict[str, Any], data), now):
                        results.append(cast(dict[str, Any], data))
                except Exception:
                    results.append(
                        {
                            "slot": slot_path.name,
                            "agent": "<unreadable lease>",
                            "exclusive": True,
                            "malformed": True,
                            "lease_file": f.name,
                        }
                    )
            return results

        def _read_build_slot_lease(lease_path: Path) -> dict[str, Any]:
            try:
                return cast(dict[str, Any], json.loads(lease_path.read_text(encoding="utf-8")))
            except Exception:
                return {}

        def _read_existing_build_slot_lease(lease_path: Path) -> dict[str, Any] | None:
            try:
                if not lease_path.is_file():
                    return None
                data = json.loads(lease_path.read_text(encoding="utf-8"))
            except Exception:
                return None
            if not isinstance(data, dict):
                return None
            return cast(dict[str, Any], data)

        def _build_slot_holder_id(
            agent: Agent,
            branch: str | None,
            execution: AgentExecution | None,
        ) -> str:
            """Return the execution key or the documented observe-mode legacy key."""
            if execution is not None:
                return safe_build_path_component(execution.id)
            return safe_build_path_component(
                f"{agent.name}__{branch or 'unknown'}__{agent.agent_generation}"
            )

        def _build_slot_lease_matches_lifetime(
            data: dict[str, Any],
            *,
            project: Project,
            agent: Agent,
            execution: AgentExecution | None,
            branch: str | None,
        ) -> bool:
            """Return whether a lease belongs to this exact immutable lifetime."""
            if (
                data.get("agent_id") != agent.id
                or data.get("agent_generation") != agent.agent_generation
                or data.get("project_generation") != project.project_generation
            ):
                return False
            if execution is not None:
                return data.get("execution_id") == execution.id
            return (
                data.get("execution_id") is None
                and data.get("agent") == agent.name
                and data.get("branch") == branch
            )

        def _build_slot_rollout_response(
            payload: dict[str, Any],
            *,
            action: str,
            execution: AgentExecution | None,
            lifecycle_protocol_version: int,
            protocol_warning: str | None,
        ) -> dict[str, Any]:
            """Attach explicit rollout ownership/protocol diagnostics."""
            payload["execution_id"] = execution.id if execution is not None else None
            payload["legacy_unscoped"] = execution is None
            payload["lifecycle_protocol_version"] = lifecycle_protocol_version
            warnings: list[str] = []
            if execution is None:
                warnings.append(
                    "execution_required_after_rollout: "
                    f"{action} accepted a legacy unscoped build-slot lease in observe mode."
                )
            if protocol_warning is not None:
                warnings.append(protocol_warning)
            if warnings:
                payload["warnings"] = warnings
            return payload

        async def _revalidate_build_slot_lifetime(
            execution: AgentExecution | None,
            project: Project,
            agent: Agent,
            *,
            action: str,
            require_active_execution: bool,
        ) -> None:
            """Recheck exact ownership/liveness while the archive lock is held.

            Ending an execution commits its terminal database state before it
            waits for the archive lock to release slot artifacts.  Acquiring
            the archive lock first and then taking an IMMEDIATE transaction
            gives both interleavings a safe outcome: either this check observes
            the terminal row and refuses the write, or the end operation runs
            second and releases the newly written lease before returning.
            """
            async with get_immediate_session() as session:
                await _revalidate_agent_lifetime_in_session(
                    session,
                    project=project,
                    agent=agent,
                    execution=execution,
                    require_active_execution=require_active_execution,
                    touch_execution_ts=(
                        _naive_utc() if execution is not None else None
                    ),
                    action=action,
                )
                await session.commit()

        @mcp.tool(name="acquire_build_slot")
        @_instrument_tool("acquire_build_slot", cluster=CLUSTER_BUILD_SLOTS, capabilities={"build"}, project_arg="project_key", agent_arg="agent_name")
        async def acquire_build_slot(
            ctx: Context,
            project_key: str,
            agent_name: str,
            slot: str,
            branch: Optional[str] = None,
            ttl_seconds: int = 3600,
            exclusive: bool = True,
            execution_id: Optional[str] = None,
            execution_token: Optional[str] = None,
            lifecycle_protocol_version: Optional[int] = None,
            registration_token: Optional[str] = None,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Acquire a build slot (advisory), optionally exclusive. Returns conflicts when another holder is active.
            """
            project = await _get_project_by_identifier(project_key)
            agent = await _authenticate_agent(
                ctx,
                project,
                agent_name,
                registration_token,
                token_param="registration_token",
                action="acquire_build_slot",
            )
            legacy_observe = _legacy_execution_rollout_allowed(settings)
            execution = await _resolve_agent_execution(
                ctx,
                project,
                agent,
                execution_id,
                execution_token,
                action="acquire_build_slot",
                required=not legacy_observe,
                touch_activity=False,
            )
            requested_protocol_version, protocol_warning = (
                _validate_execution_protocol(
                    lifecycle_protocol_version,
                    settings=settings,
                )
            )
            archive = await ensure_archive(settings, project.slug)
            now = datetime.now(timezone.utc)
            holder_branch = (branch or "").strip() or await asyncio.to_thread(_compute_branch, project.human_key)
            conflicts: list[dict[str, Any]] = []
            holder_id = _build_slot_holder_id(agent, holder_branch, execution)
            async with _archive_write_lock(archive):
                # Serialize slot lease reads and writes so concurrent holders observe
                # the latest lease state and never parse partially written JSON.
                await _revalidate_build_slot_lifetime(
                    execution,
                    project,
                    agent,
                    action="acquire_build_slot",
                    require_active_execution=execution is not None,
                )
                slot_path = _slot_dir(archive, slot)
                await asyncio.to_thread(slot_path.mkdir, parents=True, exist_ok=True)
                active = await asyncio.to_thread(_read_active_slots, slot_path, now)
                lease_path = slot_path / f"{holder_id}.json"
                current = await asyncio.to_thread(_read_existing_build_slot_lease, lease_path)

                for entry in active:
                    same_holder = _build_slot_lease_matches_lifetime(
                        entry,
                        project=project,
                        agent=agent,
                        execution=execution,
                        branch=holder_branch,
                    )
                    if same_holder:
                        continue
                    if exclusive or entry.get("exclusive", True):
                        conflicts.append(entry)
                active_current = (
                    current
                    if current is not None
                    and _is_active_build_slot_lease(current, now)
                    and _build_slot_lease_matches_lifetime(
                        current,
                        project=project,
                        agent=agent,
                        execution=execution,
                        branch=holder_branch,
                    )
                    else None
                )
                requested_exp = now + timedelta(seconds=max(ttl_seconds, 60))
                current_exp = None
                if active_current is not None:
                    current_exp = _ensure_utc(_parse_iso(cast(Optional[str], active_current.get("expires_ts"))))
                payload = {
                    "slot": slot,
                    "agent": agent.name,
                    "agent_id": agent.id,
                    "agent_generation": agent.agent_generation,
                    "project_generation": project.project_generation,
                    "authority": "server",
                    "execution_id": execution.id if execution is not None else None,
                    "legacy_unscoped": execution is None,
                    "lifecycle_protocol_version": requested_protocol_version,
                    "branch": holder_branch,
                    "exclusive": exclusive,
                    "acquired_ts": cast(str, active_current.get("acquired_ts")) if active_current is not None and isinstance(active_current.get("acquired_ts"), str) else _iso(now),
                    "expires_ts": _iso(max(requested_exp, current_exp) if current_exp is not None else requested_exp),
                }
                if execution is not None:
                    await _register_execution_build_slot_artifact_path(
                        project,
                        execution,
                        slot_name=slot,
                        slot_path_component=safe_build_path_component(slot),
                    )
                await asyncio.to_thread(
                    _write_json_atomic_sync,
                    lease_path,
                    payload,
                )
            if conflicts:
                await ctx.info(f"Build slot conflicts for '{slot}': {len(conflicts)}")
            return _build_slot_rollout_response(
                {"granted": payload, "conflicts": conflicts},
                action="acquire_build_slot",
                execution=execution,
                lifecycle_protocol_version=requested_protocol_version,
                protocol_warning=protocol_warning,
            )

        @mcp.tool(name="renew_build_slot")
        @_instrument_tool("renew_build_slot", cluster=CLUSTER_BUILD_SLOTS, capabilities={"build"}, project_arg="project_key", agent_arg="agent_name")
        async def renew_build_slot(
            ctx: Context,
            project_key: str,
            agent_name: str,
            slot: str,
            branch: Optional[str] = None,
            extend_seconds: int = 1800,
            execution_id: Optional[str] = None,
            execution_token: Optional[str] = None,
            lifecycle_protocol_version: Optional[int] = None,
            registration_token: Optional[str] = None,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Extend expiry for an existing build slot lease. No-op if missing.
            """
            project = await _get_project_by_identifier(project_key)
            agent = await _authenticate_agent(
                ctx,
                project,
                agent_name,
                registration_token,
                token_param="registration_token",
                action="renew_build_slot",
            )
            legacy_observe = _legacy_execution_rollout_allowed(settings)
            execution = await _resolve_agent_execution(
                ctx,
                project,
                agent,
                execution_id,
                execution_token,
                action="renew_build_slot",
                required=not legacy_observe,
                touch_activity=False,
            )
            requested_protocol_version, protocol_warning = (
                _validate_execution_protocol(
                    lifecycle_protocol_version,
                    settings=settings,
                )
            )
            archive = await ensure_archive(settings, project.slug)
            now = datetime.now(timezone.utc)
            holder_branch = (branch or "").strip() or await asyncio.to_thread(
                _compute_branch, project.human_key
            )
            holder_id = _build_slot_holder_id(agent, holder_branch, execution)
            async with _archive_write_lock(archive):
                await _revalidate_build_slot_lifetime(
                    execution,
                    project,
                    agent,
                    action="renew_build_slot",
                    require_active_execution=execution is not None,
                )
                slot_path = _slot_dir(archive, slot)
                lease_path = slot_path / f"{holder_id}.json"
                current = await asyncio.to_thread(_read_existing_build_slot_lease, lease_path)
                if current is None or not _is_active_build_slot_lease(current, now):
                    return _build_slot_rollout_response(
                        {"renewed": False, "expires_ts": None},
                        action="renew_build_slot",
                        execution=execution,
                        lifecycle_protocol_version=requested_protocol_version,
                        protocol_warning=protocol_warning,
                    )
                if not _build_slot_lease_matches_lifetime(
                    current,
                    project=project,
                    agent=agent,
                    execution=execution,
                    branch=holder_branch,
                ):
                    raise ToolExecutionError(
                        "BUILD_SLOT_OWNERSHIP_MISMATCH",
                        "The build-slot lease belongs to a different Agent lifetime.",
                        recoverable=False,
                        data={"slot": slot, "action": "renew_build_slot"},
                    )
                current_exp = _ensure_utc(_parse_iso(cast(Optional[str], current.get("expires_ts"))))
                base = max(now, current_exp) if current_exp is not None else now
                new_exp = _iso(base + timedelta(seconds=max(extend_seconds, 60)))
                current.update(
                    {
                        "slot": slot,
                        "agent": agent.name,
                        "agent_id": agent.id,
                        "agent_generation": agent.agent_generation,
                        "project_generation": project.project_generation,
                        "authority": "server",
                        "execution_id": execution.id if execution is not None else None,
                        "legacy_unscoped": execution is None,
                        "lifecycle_protocol_version": requested_protocol_version,
                        "branch": holder_branch,
                        "expires_ts": new_exp,
                    }
                )
                await asyncio.to_thread(
                    _write_json_atomic_sync,
                    lease_path,
                    current,
                )
            return _build_slot_rollout_response(
                {"renewed": True, "expires_ts": new_exp},
                action="renew_build_slot",
                execution=execution,
                lifecycle_protocol_version=requested_protocol_version,
                protocol_warning=protocol_warning,
            )

        @mcp.tool(name="release_build_slot")
        @_instrument_tool("release_build_slot", cluster=CLUSTER_BUILD_SLOTS, capabilities={"build"}, project_arg="project_key", agent_arg="agent_name")
        async def release_build_slot(
            ctx: Context,
            project_key: str,
            agent_name: str,
            slot: str,
            branch: Optional[str] = None,
            execution_id: Optional[str] = None,
            execution_token: Optional[str] = None,
            lifecycle_protocol_version: Optional[int] = None,
            registration_token: Optional[str] = None,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Mark an active slot lease as released (non-destructive; keeps JSON with released_ts).
            """
            project = await _get_project_by_identifier(project_key)
            agent = await _authenticate_agent(
                ctx,
                project,
                agent_name,
                registration_token,
                token_param="registration_token",
                action="release_build_slot",
            )
            legacy_observe = _legacy_execution_rollout_allowed(settings)
            execution = await _resolve_agent_execution(
                ctx,
                project,
                agent,
                execution_id,
                execution_token,
                action="release_build_slot",
                required=not legacy_observe,
                touch_activity=False,
            )
            requested_protocol_version, protocol_warning = (
                _validate_execution_protocol(
                    lifecycle_protocol_version,
                    settings=settings,
                )
            )
            archive = await ensure_archive(settings, project.slug)
            now = datetime.now(timezone.utc)
            holder_branch = None
            if execution is None:
                holder_branch = (branch or "").strip() or await asyncio.to_thread(
                    _compute_branch,
                    project.human_key,
                )
            holder_id = _build_slot_holder_id(agent, holder_branch, execution)
            async with _archive_write_lock(archive):
                await _revalidate_build_slot_lifetime(
                    execution,
                    project,
                    agent,
                    action="release_build_slot",
                    require_active_execution=False,
                )
                slot_path = _slot_dir(archive, slot)
                lease_path = slot_path / f"{holder_id}.json"
                data = await asyncio.to_thread(_read_existing_build_slot_lease, lease_path)
                if data is None or not _is_active_build_slot_lease(data, now):
                    return _build_slot_rollout_response(
                        {"released": False, "released_at": _iso(now)},
                        action="release_build_slot",
                        execution=execution,
                        lifecycle_protocol_version=requested_protocol_version,
                        protocol_warning=protocol_warning,
                    )
                if not _build_slot_lease_matches_lifetime(
                    data,
                    project=project,
                    agent=agent,
                    execution=execution,
                    branch=holder_branch,
                ):
                    raise ToolExecutionError(
                        "BUILD_SLOT_OWNERSHIP_MISMATCH",
                        "The build-slot lease belongs to a different Agent lifetime.",
                        recoverable=False,
                        data={"slot": slot, "action": "release_build_slot"},
                    )
                data.update({"released_ts": _iso(now), "expires_ts": _iso(now)})
                await asyncio.to_thread(
                    _write_json_atomic_sync,
                    lease_path,
                    data,
                )
            return _build_slot_rollout_response(
                {"released": True, "released_at": _iso(now)},
                action="release_build_slot",
                execution=execution,
                lifecycle_protocol_version=requested_protocol_version,
                protocol_warning=protocol_warning,
            )

    def _read_environment_resource(format: Optional[str] = None) -> dict[str, Any]:
        """
        Inspect the server's current environment and HTTP settings.

        When to use
        -----------
        - Debugging client connection issues (wrong host/port/path).
        - Verifying which environment (dev/stage/prod) the server is running in.

        Notes
        -----
        - This surfaces configuration only; it does not perform live health checks.

        Returns
        -------
        dict
            {
              "environment": str,
              "http": { "host": str, "port": int, "path": str }
            }

        Example (JSON-RPC)
        ------------------
        ```json
        {"jsonrpc":"2.0","id":"r1","method":"resources/read","params":{"uri":"resource://config/environment"}}
        ```
        """
        public_runtime = _public_runtime_descriptor(settings)
        payload = {
            "environment": public_runtime["environment"],
            "http": {
                "host": public_runtime["http_host"],
                "port": public_runtime["http_port"],
                "path": public_runtime["http_path"],
            },
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://config/environment",
            format_value=format,
        )

    @mcp.resource("resource://config/environment", mime_type="application/json")
    def environment_resource_exact() -> dict[str, Any]:
        return _read_environment_resource()

    @mcp.resource("resource://config/environment{?format}", mime_type="application/json")
    def environment_resource(format: Optional[str] = None) -> dict[str, Any]:
        return _read_environment_resource(format)

    # --- Product Bus (Phase 2): ensure/link/search/resources ---------------------------------

    async def _get_product_by_key(session, key: str) -> Optional[Product]:
        # Key may match product_uid or name (case-sensitive by default)
        stmt = select(Product).where(cast(Any, (Product.product_uid == key) | (Product.name == key)))
        res = await session.execute(stmt)
        return res.scalars().first()

    if settings.worktrees_enabled:
        @mcp.tool(name="ensure_product")
        @_instrument_tool("ensure_product", cluster=CLUSTER_PRODUCT, capabilities={"product"})
        async def ensure_product_tool(
            ctx: Context,
            product_key: Optional[str] = None,
            name: Optional[str] = None,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Ensure a Product exists. If not, create one.

            - product_key may be a product_uid or a name
            - If both are absent, error
            """
            await ensure_schema()
            key_raw = (product_key or name or "").strip()
            if not key_raw:
                raise ToolExecutionError("INVALID_ARGUMENT", "Provide product_key or name.")
            async with get_session() as session:
                prod = await _get_product_by_key(session, key_raw)
                if prod is None:
                    # Create with strict uid pattern; otherwise generate uid and normalize name
                    import uuid as _uuid
                    import re as _re
                    uid_pattern = _re.compile(r"^[A-Fa-f0-9]{8,64}$")
                    if product_key and uid_pattern.fullmatch(product_key.strip()):
                        uid = product_key.strip().lower()
                    else:
                        uid = _uuid.uuid4().hex[:20]
                    display_name = (name or key_raw).strip()
                    # Collapse internal whitespace and cap length
                    display_name = " ".join(display_name.split())[:255] or uid
                    prod = Product(product_uid=uid, name=display_name)
                    session.add(prod)
                    await session.commit()
                    await session.refresh(prod)
            return {"id": prod.id, "product_uid": prod.product_uid, "name": prod.name, "created_at": _iso(prod.created_at)}
    else:
        async def ensure_product_tool(
            ctx: Context,
            product_key: Optional[str] = None,
            name: Optional[str] = None,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            raise ToolExecutionError("FEATURE_DISABLED", "Product Bus is disabled. Enable WORKTREES_ENABLED to use this tool.")

    if settings.worktrees_enabled:
        @mcp.tool(name="products_link")
        @_instrument_tool("products_link", cluster=CLUSTER_PRODUCT, capabilities={"product"}, project_arg="project_key")
        async def products_link_tool(
            ctx: Context,
            product_key: str,
            project_key: str,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Link a project into a product (idempotent).
            """
            await ensure_schema()
            async with get_session() as session:
                prod = await _get_product_by_key(session, product_key.strip())
                if prod is None:
                    raise ToolExecutionError("NOT_FOUND", f"Product '{product_key}' not found.", recoverable=True)
                # Resolve project
                project = await _get_project_by_identifier(project_key)
                if project.id is None:
                    raise ToolExecutionError("NOT_FOUND", f"Project '{project_key}' not found.", recoverable=True)
                # Link if missing
                existing = await session.execute(
                    select(ProductProjectLink).where(
                        cast(Any, ProductProjectLink.product_id) == cast(Any, prod.id),
                        cast(Any, ProductProjectLink.project_id) == cast(Any, project.id),
                    )
                )
                link = existing.scalars().first()
                if link is None:
                    link = ProductProjectLink(product_id=int(cast(int, prod.id)), project_id=int(project.id))
                    session.add(link)
                    await session.commit()
                    await session.refresh(link)
                return {
                    "product": {"id": prod.id, "product_uid": prod.product_uid, "name": prod.name},
                    "project": {"id": project.id, "slug": project.slug, "human_key": project.human_key},
                    "linked": True,
                }
    else:
        async def products_link_tool(
            ctx: Context,
            product_key: str,
            project_key: str,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            raise ToolExecutionError("FEATURE_DISABLED", "Product Bus is disabled. Enable WORKTREES_ENABLED to use this tool.")

    if settings.worktrees_enabled:
        @mcp.resource("resource://product/{key}{?format}", mime_type="application/json")
        async def product_resource(key: str, format: Optional[str] = None) -> dict[str, Any]:
            """
            Inspect product and list linked projects.
            """
            # Async like every other DB-backed resource. The previous sync
            # variant bridged into a worker thread's private event loop while
            # blocking the serving loop's thread on a queue; the cached engine
            # is bound to the serving loop, so the worker could deadlock the
            # whole process (observed as multi-hour CI unit-suite hangs).
            key, query_params = _split_slug_and_query(key)
            format_value = format or query_params.get("format")
            await ensure_schema()
            async with get_session() as session:
                prod = await _get_product_by_key(session, key.strip())
                if prod is None:
                    raise ToolExecutionError("NOT_FOUND", f"Product '{key}' not found.", recoverable=True)
                proj_rows = await session.execute(
                    select(Project).join(ProductProjectLink, cast(Any, ProductProjectLink.project_id) == Project.id).where(
                        cast(Any, ProductProjectLink.product_id) == cast(Any, prod.id)
                    )
                )
                projects = [
                    {"id": p.id, "slug": p.slug, "human_key": p.human_key, "created_at": _iso(p.created_at)}
                    for p in proj_rows.scalars().all()
                ]
                payload = {
                    "id": prod.id,
                    "product_uid": prod.product_uid,
                    "name": prod.name,
                    "created_at": _iso(prod.created_at),
                    "projects": projects,
                }
            return _apply_resource_output_format(
                payload,
                settings=settings,
                resource_name="resource://product/{key}",
                format_value=format_value,
            )

    if settings.worktrees_enabled:
        @mcp.tool(name="search_messages_product")
        @_instrument_tool("search_messages_product", cluster=CLUSTER_PRODUCT, capabilities={"search"})
        async def search_messages_product(
            ctx: Context,
            product_key: str,
            query: str,
            limit: int = 20,
            agent_name: Optional[str] = None,
            registration_token: Optional[str] = None,
            format: Optional[str] = None,
        ) -> Any:
            """
            Full-text search across all projects linked to a product.
            """
            # Shared limit bounds (issue #202): reject limit<1, clamp >1000.
            limit = _validate_limit(limit)
            # Sanitize the FTS query first
            sanitized_query = _sanitize_fts_query(query)
            if sanitized_query is None:
                await ctx.info(f"Search query '{query}' is not searchable, returning empty results.")
                try:
                    from fastmcp.tools import ToolResult
                    return ToolResult(structured_content={"result": []})
                except Exception:
                    return []

            await ensure_schema()
            _product, _projects, authorized = await _authenticate_product_agents(
                ctx,
                product_key,
                agent_name=agent_name,
                provided_token=registration_token,
                token_param="registration_token",
                action="search_messages_product",
            )
            proj_ids = [project.id for project, _agent in authorized if project.id is not None]
            if not proj_ids:
                return []
            authorized_map = {
                project.id: agent.id
                for project, agent in authorized
                if project.id is not None and agent.id is not None
            }
            rows: list[Any] = []
            async with get_session() as session:
                # FTS search limited to projects in proj_ids
                try:
                    result = await session.execute(
                        text(
                            """
                            SELECT m.id, m.subject, m.body_md, m.importance, m.ack_required, m.created_ts,
                                   m.sender_id,
                                   m.thread_id, a.name AS sender_name, m.project_id,
                                   sp.id AS sender_project_id, sp.human_key AS sender_project, sp.slug AS sender_project_slug
                            FROM fts_messages
                            JOIN messages m ON fts_messages.rowid = m.id
                            JOIN agents a ON m.sender_id = a.id
                            JOIN projects sp ON a.project_id = sp.id
                            WHERE m.project_id IN :proj_ids AND fts_messages MATCH :query
                            ORDER BY bm25(fts_messages) ASC
                            LIMIT :limit
                            """
                        ).bindparams(bindparam("proj_ids", expanding=True)),
                        {"proj_ids": proj_ids, "query": sanitized_query, "limit": limit},
                    )
                    rows = list(result.mappings().all())
                except Exception as fts_err:
                    logger.warning("FTS product query failed, returning empty results", extra={"query": sanitized_query, "error": str(fts_err)})
                    fallback_terms = _extract_like_terms(query)
                    if not fallback_terms:
                        rows = []
                    else:
                        clauses = []
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
                                       m.sender_id,
                                       m.thread_id, a.name AS sender_name, m.project_id,
                                       sp.id AS sender_project_id, sp.human_key AS sender_project, sp.slug AS sender_project_slug
                                FROM messages m
                                JOIN agents a ON m.sender_id = a.id
                                JOIN projects sp ON a.project_id = sp.id
                                WHERE m.project_id IN :proj_ids AND {where_clause}
                                ORDER BY m.created_ts DESC
                                LIMIT :limit
                                """
                            ).bindparams(bindparam("proj_ids", expanding=True)),
                            params,
                        )
                        rows = list(result.mappings().all())
            visible_rows = rows
            if rows:
                message_ids = [int(row["id"]) for row in rows]
                recipients_by_message: dict[int, set[int]] = {}
                async with get_session() as session:
                    recipient_rows = await session.execute(
                        select(MessageRecipient.message_id, MessageRecipient.agent_id).where(
                            cast(Any, MessageRecipient.message_id).in_(message_ids)
                        )
                    )
                    for message_id, recipient_agent_id in recipient_rows.all():
                        recipients_by_message.setdefault(int(message_id), set()).add(int(recipient_agent_id))
                visible_rows = []
                for row in rows:
                    project_agent_id = authorized_map.get(int(row["project_id"]))
                    if project_agent_id is None:
                        continue
                    if int(row["sender_id"]) == project_agent_id or project_agent_id in recipients_by_message.get(int(row["id"]), set()):
                        visible_rows.append(row)
            items: list[dict[str, Any]] = []
            for row in visible_rows:
                item = {
                    "id": row["id"],
                    "subject": row["subject"],
                    "importance": row["importance"],
                    "ack_required": row["ack_required"],
                    "created_ts": _iso(row["created_ts"]),
                    "thread_id": row["thread_id"],
                    "project_id": row["project_id"],
                }
                _apply_sender_identity(
                    item,
                    message_project_id=row["project_id"],
                    sender_name=row["sender_name"],
                    sender_project_id=row["sender_project_id"],
                    sender_project_human_key=row["sender_project"],
                    sender_project_slug=row["sender_project_slug"],
                )
                items.append(item)
            try:
                from fastmcp.tools import ToolResult
                return ToolResult(structured_content={"result": items})
            except Exception:
                return items
    else:
        async def search_messages_product(
            ctx: Context,
            product_key: str,
            query: str,
            limit: int = 20,
            agent_name: Optional[str] = None,
            registration_token: Optional[str] = None,
            format: Optional[str] = None,
        ) -> Any:
            raise ToolExecutionError("FEATURE_DISABLED", "Product Bus is disabled. Enable WORKTREES_ENABLED to use this tool.")

    if settings.worktrees_enabled:
        @mcp.tool(name="fetch_inbox_product")
        @_instrument_tool("fetch_inbox_product", cluster=CLUSTER_PRODUCT, capabilities={"messaging", "read"})
        async def fetch_inbox_product(
            ctx: Context,
            product_key: str,
            agent_name: str,
            limit: int = 20,
            urgent_only: bool = False,
            include_bodies: bool = False,
            since_ts: Optional[str] = None,
            unread_only: bool = False,
            registration_token: Optional[str] = None,
            format: Optional[str] = None,
        ) -> ToonableList:
            """
            Retrieve recent messages for an agent across all projects linked to a product (non-mutating).

            `unread_only=True` filters each per-project fetch to recipient rows the agent
            has not explicitly marked read; especially load-bearing for product-wide
            polling where the cross-project token cost compounds.
            """
            # Shared limit bounds (issue #202): reject limit<1, clamp >1000.
            limit = _validate_limit(limit)
            _product, _projects, authorized = await _authenticate_product_agents(
                ctx,
                product_key,
                agent_name=agent_name,
                provided_token=registration_token,
                token_param="registration_token",
                action="fetch_inbox_product",
            )
            messages: list[dict[str, Any]] = []
            for project, ag in authorized:
                proj_items = await _list_inbox(
                    project,
                    ag,
                    limit,
                    urgent_only,
                    include_bodies,
                    since_ts,
                    unread_only=unread_only,
                )
                for item in proj_items:
                    item["project_id"] = item.get("project_id") or project.id
                    messages.append(item)
            # Sort by created_ts desc and trim to limit
            def _dt_key(it: dict[str, Any]) -> float:
                ts = _parse_iso(str(it.get("created_ts") or ""))
                return ts.timestamp() if ts else 0.0
            messages.sort(key=_dt_key, reverse=True)
            return messages[: max(0, int(limit))]
    else:
        async def fetch_inbox_product(
            ctx: Context,
            product_key: str,
            agent_name: str,
            limit: int = 20,
            urgent_only: bool = False,
            include_bodies: bool = False,
            since_ts: Optional[str] = None,
            unread_only: bool = False,
            registration_token: Optional[str] = None,
            format: Optional[str] = None,
        ) -> ToonableList:
            raise ToolExecutionError("FEATURE_DISABLED", "Product Bus is disabled. Enable WORKTREES_ENABLED to use this tool.")

    if settings.worktrees_enabled:
        @mcp.tool(name="summarize_thread_product")
        @_instrument_tool("summarize_thread_product", cluster=CLUSTER_PRODUCT, capabilities={"summarization", "search"})
        async def summarize_thread_product(
            ctx: Context,
            product_key: str,
            thread_id: str,
            include_examples: bool = False,
            llm_mode: bool = True,
            llm_model: Optional[str] = None,
            per_thread_limit: Optional[int] = None,
            agent_name: Optional[str] = None,
            registration_token: Optional[str] = None,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            """
            Summarize a thread (by id or thread key) across all projects linked to a product.
            """
            await ensure_schema()
            sender_alias = aliased(Agent)
            sender_project_alias = aliased(Project)
            try:
                seed_id = int(thread_id)
            except ValueError:
                seed_id = None
            criteria: list[Any] = [cast(Any, Message.thread_id) == thread_id]
            if seed_id is not None:
                criteria.append(cast(Any, Message.id) == seed_id)

            _product, _projects, authorized = await _authenticate_product_agents(
                ctx,
                product_key,
                agent_name=agent_name,
                provided_token=registration_token,
                token_param="registration_token",
                action="summarize_thread_product",
            )
            visibility_clauses = [
                and_(
                    cast(Any, Message.project_id) == project.id,
                    _message_visible_to_agent_clause(agent.id or 0),
                )
                for project, agent in authorized
                if project.id is not None and agent.id is not None
            ]
            if not visibility_clauses:
                return {"thread_id": thread_id, "summary": {"participants": [], "key_points": [], "action_items": [], "total_messages": 0}, "examples": []}

            async with get_session() as session:
                stmt = (
                    select(Message, sender_alias.name, sender_project_alias.id, sender_project_alias.slug)
                    .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
                    .join(sender_project_alias, cast(Any, sender_alias.project_id == sender_project_alias.id))
                    .where(or_(*cast(Any, criteria)), or_(*visibility_clauses))
                    .order_by(asc(cast(Any, Message.created_ts)))
                )
                if per_thread_limit:
                    stmt = stmt.limit(per_thread_limit)
                raw_rows = (await session.execute(stmt)).all()
            rows = [
                (
                    row[0],
                    _sender_display_name(
                        message_project_id=row[0].project_id,
                        sender_name=row[1],
                        sender_project_id=row[2],
                        sender_project_slug=row[3],
                    ),
                )
                for row in raw_rows
            ]
            summary = _summarize_messages(rows)
            heuristic_key_points = list(summary.get("key_points", []))

            # Optional LLM refinement (same as project-level)
            if llm_mode and get_settings().llm.enabled:
                try:
                    excerpts: list[str] = []
                    for message, sender_name in rows[:15]:
                        excerpts.append(f"- {sender_name}: {message.subject}\n{message.body_md[:800]}")
                    if excerpts:
                        system = (
                            "You are a senior engineer. Produce a concise JSON summary with keys: "
                            "participants[], key_points[], action_items[], mentions[{name,count}], code_references[], "
                            "total_messages, open_actions, done_actions. Derive from the given thread excerpts."
                        )
                        user = "\n\n".join(excerpts)
                        llm_resp = await complete_system_user(system, user, model=llm_model)
                        parsed = _parse_json_safely(llm_resp.content)
                        if parsed:
                            for key in (
                                "participants",
                                "key_points",
                                "action_items",
                                "mentions",
                                "code_references",
                                "total_messages",
                                "open_actions",
                                "done_actions",
                            ):
                                value = parsed.get(key)
                                if value:
                                    summary[key] = value
                            if heuristic_key_points and isinstance(summary.get("key_points"), list):
                                keywords = ("TODO", "ACTION", "FIXME", "NEXT", "BLOCKED")
                                extra = [
                                    kp for kp in heuristic_key_points
                                    if any(token in str(kp).upper() for token in keywords)
                                ]
                                if extra:
                                    merged: list[str] = []
                                    for item in summary["key_points"] + extra:
                                        if item not in merged:
                                            merged.append(item)
                                    summary["key_points"] = merged[:10]
                except Exception as e:
                    await ctx.debug(f"summarize_thread_product.llm_skipped: {e}")

            examples: list[dict[str, Any]] = []
            if include_examples:
                for message, sender_name in rows[:3]:
                    examples.append(
                        {
                            "id": message.id,
                            "subject": message.subject,
                            "from": sender_name,
                            "created_ts": _iso(message.created_ts),
                        }
                    )
            await ctx.info(f"Summarized thread '{thread_id}' across product '{product_key}' with {len(rows)} messages")
            return {"thread_id": thread_id, "summary": summary, "examples": examples}
    else:
        async def summarize_thread_product(
            ctx: Context,
            product_key: str,
            thread_id: str,
            include_examples: bool = False,
            llm_mode: bool = True,
            llm_model: Optional[str] = None,
            per_thread_limit: Optional[int] = None,
            agent_name: Optional[str] = None,
            registration_token: Optional[str] = None,
            format: Optional[str] = None,
        ) -> dict[str, Any]:
            raise ToolExecutionError("FEATURE_DISABLED", "Product Bus is disabled. Enable WORKTREES_ENABLED to use this tool.")
    if settings.worktrees_enabled:
        def _render_identity_resource_payload(
            project_identifier: Optional[str],
            *,
            format_value: Optional[str],
            resource_name: str,
        ) -> dict[str, Any]:
            if not project_identifier:
                raise ValueError("project parameter is required for identity resource")
            target_path = _canonicalize_project_identifier(project_identifier)
            payload = _resolve_project_identity(target_path)
            return _apply_resource_output_format(
                payload,
                settings=settings,
                resource_name=resource_name,
                format_value=format_value,
            )

        @mcp.resource("resource://identity/{project}{?format}", mime_type="application/json")
        def identity_resource(project: str, format: Optional[str] = None) -> dict[str, Any]:
            """
            Inspect identity resolution for a given project path. Returns the slug actually used,
            the identity mode in effect, canonical path for the selected mode, and git repo facts.
            """
            raw_path, query_params = _split_slug_and_query(project)
            format_value = format or query_params.get("format")
            return _render_identity_resource_payload(
                raw_path,
                format_value=format_value,
                resource_name="resource://identity/{project}",
            )

    def _read_tooling_directory_resource(format: Optional[str] = None) -> dict[str, Any]:
        """
        Provide a clustered view of exposed MCP tools to combat option overload.

        The directory groups tools by workflow, outlines primary use cases,
        highlights nearby alternatives, and shares starter playbooks so agents
        can focus on the verbs relevant to their immediate task.
        """

        clusters = [
            {
                "name": "Infrastructure & Workspace Setup",
                "purpose": "Bootstrap coordination and guardrails before agents begin editing.",
                "tools": [
                    {
                        "name": "health_check",
                        "summary": "Report environment and HTTP wiring so orchestrators confirm connectivity.",
                        "use_when": "Beginning a session or during incident response triage.",
                        "related": ["ensure_project"],
                        "expected_frequency": "Once per agent session or when connectivity is in doubt.",
                        "required_capabilities": ["infrastructure"],
                        "usage_examples": [{"hint": "Pre-flight", "sample": "health_check()"}],
                    },
                    {
                        "name": "ensure_project",
                        "summary": "Ensure project slug, schema, and archive exist for a shared repo identifier.",
                        "use_when": "First call against a repo or when switching projects.",
                        "related": ["register_agent", "file_reservation_paths"],
                        "expected_frequency": "Whenever a new repo/path is encountered.",
                        "required_capabilities": ["infrastructure", "storage"],
                        "usage_examples": [{"hint": "First action", "sample": "ensure_project(human_key='/owner/backend')"}],
                    },
                    {
                        "name": "archive_project",
                        "summary": "Reversibly hide a project from active listings while preserving all history.",
                        "use_when": "A project is dormant and should leave the active workspace roster.",
                        "related": ["unarchive_project", "ensure_project"],
                        "expected_frequency": "Rare project lifecycle maintenance.",
                        "required_capabilities": ["infrastructure"],
                        "usage_examples": [{"hint": "Archive", "sample": "archive_project(project_key='/owner/backend', registration_token='<project agent token>')"}],
                    },
                    {
                        "name": "unarchive_project",
                        "summary": "Restore an archived project to active listings.",
                        "use_when": "Coordination resumes for a previously archived project.",
                        "related": ["archive_project"],
                        "expected_frequency": "Rare project lifecycle maintenance.",
                        "required_capabilities": ["infrastructure"],
                        "usage_examples": [{"hint": "Restore", "sample": "unarchive_project(project_key='/owner/backend', registration_token='<project agent token>')"}],
                    },
                    {
                        "name": "install_precommit_guard",
                        "summary": "Install Git pre-commit hook that enforces advisory file_reservations locally.",
                        "use_when": "Onboarding a repository into coordinated mode.",
                        "related": ["file_reservation_paths", "uninstall_precommit_guard"],
                        "expected_frequency": "Infrequent—per repository setup.",
                        "required_capabilities": ["repository", "filesystem"],
                        "usage_examples": [{"hint": "Onboard", "sample": "install_precommit_guard(project_key='backend', code_repo_path='~/repo')"}],
                    },
                    {
                        "name": "uninstall_precommit_guard",
                        "summary": "Remove the advisory pre-commit hook from a repo.",
                        "use_when": "Decommissioning or debugging the guard hook.",
                        "related": ["install_precommit_guard"],
                        "expected_frequency": "Rare; only when disabling guard enforcement.",
                        "required_capabilities": ["repository"],
                        "usage_examples": [{"hint": "Cleanup", "sample": "uninstall_precommit_guard(code_repo_path='~/repo')"}],
                    },
                ],
            },
            {
                "name": "Identity & Directory",
                "purpose": "Provision durable mailboxes, resume their metadata, and inspect directory state.",
                "tools": [
                    {
                        "name": "register_agent",
                        "summary": "Upsert an agent profile and refresh last_active_ts for a known persona.",
                        "use_when": "Resuming an identity or updating program/model/task metadata.",
                        "related": ["create_agent_identity", "whois"],
                        "expected_frequency": "At the start of each automated work session.",
                        "required_capabilities": ["identity"],
                        "usage_examples": [{"hint": "Resume durable identity", "sample": "register_agent(project_key='/owner/backend', program='codex', model='gpt5', name='codex-wsl-home-1', registration_token='<private token>')"}],
                    },
                    {
                        "name": "create_agent_identity",
                        "summary": "Provision a new durable Agent from a required stable identity.",
                        "use_when": "Provisioning a new durable mailbox/persona that must not overwrite an existing profile.",
                        "related": ["register_agent"],
                        "expected_frequency": "Infrequent durable identity provisioning.",
                        "required_capabilities": ["identity"],
                        "usage_examples": [{"hint": "Provision durable identity", "sample": "create_agent_identity(project_key='/owner/backend', name_hint='codex-linux-ci-1', program='codex', model='gpt5')"}],
                    },
                    {
                        "name": "rotate_registration_token",
                        "summary": "CAS a journaled replacement credential and revoke prior MCP session bindings.",
                        "use_when": "A durable Agent credential may have been exposed or requires planned rotation.",
                        "related": ["register_agent", "whois"],
                        "expected_frequency": "Rare security maintenance; use the supported rotate-token client flow.",
                        "required_capabilities": ["identity"],
                        "usage_examples": [
                            {
                                "hint": "Use the crash-safe client",
                                "sample": "agent_mail_setup.sh rotate-token codex 1 --project-key /owner/backend",
                            }
                        ],
                    },
                    {
                        "name": "retire_agent",
                        "summary": "Reversibly remove a durable mailbox from active routing while preserving its history.",
                        "use_when": "A durable mailbox is no longer in use and should leave the active roster.",
                        "related": ["unretire_agent", "sweep_stale_agents"],
                        "expected_frequency": "Rare lifecycle maintenance.",
                        "required_capabilities": ["identity"],
                        "usage_examples": [{"hint": "Retire", "sample": "retire_agent(project_key='/owner/backend', agent_name='codex-linux-ci-1', registration_token='<private token>')"}],
                    },
                    {
                        "name": "unretire_agent",
                        "summary": "Restore a previously retired durable mailbox to active routing.",
                        "use_when": "A known mailbox resumes service after reversible retirement.",
                        "related": ["retire_agent"],
                        "expected_frequency": "Rare lifecycle maintenance.",
                        "required_capabilities": ["identity"],
                        "usage_examples": [{"hint": "Restore", "sample": "unretire_agent(project_key='/owner/backend', agent_name='codex-linux-ci-1', registration_token='<private token>')"}],
                    },
                    {
                        "name": "whois",
                        "summary": "Return enriched profile info plus recent archive commits for an agent.",
                        "use_when": "Dashboarding, routing coordination messages, or auditing activity.",
                        "related": ["register_agent"],
                        "expected_frequency": "Ad hoc when context about an agent is required.",
                        "required_capabilities": ["identity", "audit"],
                        "usage_examples": [{"hint": "Directory lookup", "sample": "whois(project_key='/owner/backend', agent_name='codex-wsl-home-1', registration_token='<private token>')"}],
                    },
                    {
                        "name": "set_contact_policy",
                        "summary": "Set inbound contact policy (open, auto, contacts_only, block_all).",
                        "use_when": "Adjusting how permissive an agent is about unsolicited messages.",
                        "related": ["request_contact", "respond_contact"],
                        "expected_frequency": "Occasional configuration change.",
                        "required_capabilities": ["contact"],
                        "usage_examples": [{"hint": "Restrict inbox", "sample": "set_contact_policy(project_key='/owner/backend', agent_name='codex-wsl-home-1', policy='contacts_only', registration_token='<private token>')"}],
                    },
                ],
            },
            {
                "name": "Messaging Lifecycle",
                "purpose": "Send, receive, and acknowledge threaded Markdown mail.",
                "tools": [
                    {
                        "name": "send_message",
                        "summary": "Accept and publish an idempotent message through the atomic Git-to-database boundary.",
                        "use_when": "Starting new threads or broadcasting plans across projects.",
                        "related": ["reply_message", "request_contact"],
                        "expected_frequency": "Frequent—core write operation.",
                        "required_capabilities": ["messaging"],
                        "usage_examples": [{"hint": "New plan", "sample": "send_message(project_key='backend', sender_name='codex-wsl-home-1', to=['claude-linux-ci-1'], subject='Plan', body_md='...', idempotency_key='plan-01', registration_token='<registration token>')"}],
                    },
                    {
                        "name": "reply_message",
                        "summary": "Atomically reply within a thread, including exact cross-project return routes.",
                        "use_when": "Continuing discussions or acknowledging decisions.",
                        "related": ["send_message"],
                        "expected_frequency": "Frequent when collaborating inside a thread.",
                        "required_capabilities": ["messaging"],
                        "usage_examples": [{"hint": "Thread reply", "sample": "reply_message(project_key='backend', message_id=42, sender_name='codex-wsl-home-1', body_md='Got it!', idempotency_key='reply-42-01', registration_token='<registration token>')"}],
                    },
                    {
                        "name": "get_message_delivery",
                        "summary": "Read an authorized delivery state and optionally retry due pending work.",
                        "use_when": "Recovering after an ambiguous send/reply result or tracking a pending Git publication.",
                        "related": ["send_message", "reply_message"],
                        "expected_frequency": "After a pending/deferred response or network disconnect.",
                        "required_capabilities": ["messaging", "read"],
                        "usage_examples": [{"hint": "Recover", "sample": "get_message_delivery(project_key='backend', agent_name='codex-wsl-home-1', delivery_id='<uuid>', retry_pending=True)"}],
                    },
                    {
                        "name": "fetch_inbox",
                        "summary": "Poll recent messages for an agent with filters (urgent_only, since_ts).",
                        "use_when": "After each work unit to ingest coordination updates.",
                        "related": ["mark_message_read", "acknowledge_message"],
                        "expected_frequency": "Frequent polling in agent loops.",
                        "required_capabilities": ["messaging", "read"],
                        "usage_examples": [{"hint": "Poll", "sample": "fetch_inbox(project_key='backend', agent_name='codex-wsl-home-1', since_ts='2025-10-24T00:00:00Z')"}],
                    },
                    {
                        "name": "mark_message_read",
                        "summary": "Record read_ts for FYI messages without sending acknowledgements.",
                        "use_when": "Clearing inbox notifications once reviewed.",
                        "related": ["acknowledge_message"],
                        "expected_frequency": "Whenever FYI mail is processed.",
                        "required_capabilities": ["messaging", "read"],
                        "usage_examples": [{"hint": "Read receipt", "sample": "mark_message_read(project_key='backend', agent_name='codex-wsl-home-1', message_id=42)"}],
                    },
                    {
                        "name": "acknowledge_message",
                        "summary": "Set read_ts and ack_ts so senders know action items landed.",
                        "use_when": "Responding to ack_required messages.",
                        "related": ["mark_message_read"],
                        "expected_frequency": "Each time a message requests acknowledgement.",
                        "required_capabilities": ["messaging", "ack"],
                        "usage_examples": [{"hint": "Ack", "sample": "acknowledge_message(project_key='backend', agent_name='codex-wsl-home-1', message_id=42)"}],
                    },
                ],
            },
            {
                "name": "Contact Governance",
                "purpose": "Manage messaging permissions when policies are not open by default.",
                "tools": [
                    {
                        "name": "request_contact",
                        "summary": "Create or refresh a pending AgentLink to an already registered target and notify it with an ack_required intro.",
                        "use_when": "Requesting permission before messaging another registered Agent; it never provisions the target mailbox.",
                        "related": ["respond_contact", "set_contact_policy"],
                        "expected_frequency": "Occasional—when new communication lines are needed.",
                        "required_capabilities": ["contact"],
                        "usage_examples": [{"hint": "Ask permission", "sample": "request_contact(project_key='backend', from_agent='codex-wsl-home-1', to_agent='claude-linux-ci-1', registration_token='<requester token>')"}],
                    },
                    {
                        "name": "respond_contact",
                        "summary": "Approve or block a pending contact request, optionally setting expiry.",
                        "use_when": "Granting or revoking messaging permissions.",
                        "related": ["request_contact"],
                        "expected_frequency": "As often as requests arrive.",
                        "required_capabilities": ["contact"],
                        "usage_examples": [{"hint": "Approve", "sample": "respond_contact(project_key='backend', to_agent='claude-linux-ci-1', from_agent='codex-wsl-home-1', accept=True, registration_token='<target token>')"}],
                    },
                    {
                        "name": "list_contacts",
                        "summary": "List outbound contact links with target projects and audit flags for expiry/messageability.",
                        "use_when": "Auditing who an agent may message or rotating expiring approvals.",
                        "related": ["request_contact", "respond_contact"],
                        "expected_frequency": "Periodic audits or dashboards.",
                        "required_capabilities": ["contact", "audit"],
                        "usage_examples": [{"hint": "Audit", "sample": "list_contacts(project_key='backend', agent_name='codex-wsl-home-1')"}],
                    },
                ],
            },
            {
                "name": "Search & Summaries",
                "purpose": "Surface signal from large mailboxes and compress long threads.",
                "tools": [
                    {
                        "name": "search_messages",
                        "summary": "Run FTS5 queries across subject/body text to locate relevant threads.",
                        "use_when": "Triage or gathering context before editing.",
                        "related": ["fetch_inbox", "summarize_thread"],
                        "expected_frequency": "Regular during investigation phases.",
                        "required_capabilities": ["search"],
                        "usage_examples": [{"hint": "FTS", "sample": "search_messages(project_key='backend', query='\"build plan\" AND users', limit=20, agent_name='codex-wsl-home-1', registration_token='<agent token>')"}],
                    },
                    {
                        "name": "summarize_thread",
                        "summary": "Extract participants, key points, and action items for one or more threads.",
                        "use_when": "Briefing new agents on long discussions, closing loops, or producing digests.",
                        "related": ["search_messages"],
                        "expected_frequency": "When threads exceed quick skim length or at cadence checkpoints.",
                        "required_capabilities": ["search", "summarization"],
                        "usage_examples": [
                            {"hint": "Single thread", "sample": "summarize_thread(project_key='backend', thread_id='TKT-123', include_examples=True, agent_name='codex-wsl-home-1', registration_token='<agent token>')"},
                            {"hint": "Multi-thread digest", "sample": "summarize_thread(project_key='backend', thread_id='TKT-123,UX-42,BUG-99', agent_name='codex-wsl-home-1', registration_token='<agent token>')"},
                        ],
                    },
                ],
            },
            {
                "name": "File Reservations & Workspace Guardrails",
                "purpose": "Coordinate file/glob ownership to avoid overwriting concurrent work.",
                "tools": [
                    {
                        "name": "file_reservation_paths",
                        "summary": "Issue advisory file_reservations with overlap detection and Git artifacts.",
                        "use_when": "Before touching high-traffic surfaces or long-lived refactors.",
                        "related": ["release_file_reservations", "renew_file_reservations"],
                        "expected_frequency": "Whenever starting work on contested surfaces.",
                        "required_capabilities": ["file_reservations", "repository"],
                        "usage_examples": [{"hint": "Lock file", "sample": "file_reservation_paths(project_key='backend', agent_name='codex-wsl-home-1', paths=['src/app.py'], ttl_seconds=7200, execution_id='<uuid>', execution_token='<capability>', lifecycle_protocol_version=1)"}],
                    },
                    {
                        "name": "release_file_reservations",
                        "summary": "Release active file_reservations (fully or by subset) and stamp released_ts.",
                        "use_when": "Finishing work so surfaces become available again.",
                        "related": ["file_reservation_paths", "renew_file_reservations"],
                        "expected_frequency": "Each time work on a surface completes.",
                        "required_capabilities": ["file_reservations"],
                        "usage_examples": [{"hint": "Unlock", "sample": "release_file_reservations(project_key='backend', agent_name='codex-wsl-home-1', paths=['src/app.py'], execution_id='<uuid>', execution_token='<capability>', lifecycle_protocol_version=1)"}],
                    },
                    {
                        "name": "renew_file_reservations",
                        "summary": "Extend file_reservation expiry windows without allocating new file_reservation IDs.",
                        "use_when": "Long-running work needs more time but should retain ownership.",
                        "related": ["file_reservation_paths", "release_file_reservations"],
                        "expected_frequency": "Periodically during multi-hour work items.",
                        "required_capabilities": ["file_reservations"],
                        "usage_examples": [{"hint": "Extend", "sample": "renew_file_reservations(project_key='backend', agent_name='codex-wsl-home-1', extend_seconds=1800, execution_id='<uuid>', execution_token='<capability>', lifecycle_protocol_version=1)"}],
                    },
                ],
            },
            {
                "name": "Workflow Macros",
                "purpose": "Opinionated orchestrations that compose multiple primitives for smaller agents.",
                "tools": [
                    {
                        "name": "macro_start_session",
                        "summary": "Ensure project, register/update agent, optionally file_reservation surfaces, and return inbox context.",
                        "use_when": "Kickstarting a focused work session with one call.",
                        "related": ["ensure_project", "register_agent", "file_reservation_paths", "fetch_inbox"],
                        "expected_frequency": "At the beginning of each autonomous session.",
                        "required_capabilities": ["workflow", "messaging", "file_reservations", "identity"],
                        "usage_examples": [{"hint": "Bootstrap", "sample": "macro_start_session(human_key='/abs/path/backend', program='codex', model='gpt5', agent_name='codex-wsl-home-1', external_id='<native-session-id>', client_name='codex', execution_token='<64-hex-token>', file_reservation_paths=['src/api/*.py'])"}],
                    },
                    {
                        "name": "macro_prepare_thread",
                        "summary": "Register or refresh an agent, summarise a thread, and fetch inbox context in one call.",
                        "use_when": "Briefing a helper before joining an ongoing discussion.",
                        "related": ["register_agent", "summarize_thread", "fetch_inbox"],
                        "expected_frequency": "Whenever onboarding a new contributor to an active thread.",
                        "required_capabilities": ["workflow", "messaging", "summarization"],
                        "usage_examples": [{"hint": "Join thread", "sample": "macro_prepare_thread(project_key='backend', thread_id='TKT-123', program='codex', model='gpt5', agent_name='codex-wsl-home-1', external_id='<native-session-id>', client_name='codex', execution_token='<64-hex-token>')"}],
                    },
                    {
                        "name": "macro_file_reservation_cycle",
                        "summary": "FileReservation a set of paths and optionally release them once work is complete.",
                        "use_when": "Wrapping a focused edit cycle that needs advisory locks.",
                        "related": ["file_reservation_paths", "release_file_reservations", "renew_file_reservations"],
                        "expected_frequency": "Per guarded work block.",
                        "required_capabilities": ["workflow", "file_reservations", "repository"],
                        "usage_examples": [{"hint": "FileReservation & release", "sample": "macro_file_reservation_cycle(project_key='backend', agent_name='codex-wsl-home-1', paths=['src/app.py'], auto_release=true, execution_id='<uuid>', execution_token='<capability>')"}],
                    },
                    {
                        "name": "macro_contact_handshake",
                        "summary": "Request contact approval between registered Agents, optionally auto-accept, and send a welcome message.",
                        "use_when": "Connecting two existing durable mailboxes that lack permissions; it never provisions either identity.",
                        "related": ["request_contact", "respond_contact", "send_message"],
                        "expected_frequency": "When onboarding new agent pairs.",
                        "required_capabilities": ["workflow", "contact", "messaging"],
                        "usage_examples": [{"hint": "Automated handshake", "sample": "macro_contact_handshake(project_key='backend', requester='codex-wsl-home-1', target='claude-linux-ci-1', auto_accept=true, requester_registration_token='<requester token>', target_registration_token='<target token>', welcome_subject='Hello', welcome_body='Excited to collaborate!')"}],
                    },
                ],
            },
        ]

        visible_clusters: list[dict[str, Any]] = []
        for cluster in clusters:
            visible_tools: list[dict[str, Any]] = []
            for tool_entry in cluster["tools"]:
                tool_dict = cast(dict[str, Any], tool_entry)
                tool_name = str(tool_dict.get("name", ""))
                if not _tool_visible_for_settings(tool_name, settings):
                    continue
                related = tool_dict.get("related")
                if isinstance(related, list):
                    tool_dict["related"] = [
                        related_name
                        for related_name in related
                        if _tool_visible_for_settings(str(related_name), settings)
                    ]
                meta = TOOL_METADATA.get(tool_name)
                if not meta:
                    visible_tools.append(tool_dict)
                    continue
                tool_dict["capabilities"] = meta["capabilities"]
                tool_dict.setdefault("complexity", meta["complexity"])
                if "required_capabilities" in tool_dict:
                    tool_dict["required_capabilities"] = meta["capabilities"]
                visible_tools.append(tool_dict)
            if visible_tools:
                cluster["tools"] = visible_tools
                visible_clusters.append(cluster)
        clusters = visible_clusters

        playbooks = [
            {
                "workflow": "Kick off new agent session (macro)",
                "sequence": ["health_check", "macro_start_session", "summarize_thread"],
            },
            {
                "workflow": "Kick off new agent session (manual)",
                "sequence": ["health_check", "ensure_project", "register_agent", "fetch_inbox"],
            },
            {
                "workflow": "Start focused refactor",
                "sequence": ["ensure_project", "file_reservation_paths", "send_message", "fetch_inbox", "acknowledge_message"],
            },
            {
                "workflow": "Join existing discussion",
                "sequence": ["macro_prepare_thread", "reply_message", "acknowledge_message"],
            },
            {
                "workflow": "Manage contact approvals",
                "sequence": ["set_contact_policy", "request_contact", "respond_contact", "send_message"],
            },
        ]
        playbooks = [
            playbook
            for playbook in playbooks
            if all(
                _tool_visible_for_settings(str(tool_name), settings)
                for tool_name in playbook["sequence"]
            )
        ]

        default_format = settings.output_format_default or settings.toon_default_format or "json"
        payload = {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "metrics_uri": "resource://tooling/metrics",
            "output_formats": {
                "default": default_format,
                "tool_param": "format",
                "resource_query": "format",
                "values": ["json", "toon"],
                "toon_envelope": {"format": "toon", "data": "<TOON>", "meta": {"requested": "toon"}},
            },
            "clusters": clusters,
            "playbooks": playbooks,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/directory",
            format_value=format,
        )

    @mcp.resource("resource://tooling/directory", mime_type="application/json")
    def tooling_directory_resource_exact() -> dict[str, Any]:
        return _read_tooling_directory_resource()

    @mcp.resource("resource://tooling/directory{?format}", mime_type="application/json")
    def tooling_directory_resource(format: Optional[str] = None) -> dict[str, Any]:
        return _read_tooling_directory_resource(format)

    def _read_tooling_schemas_resource(format: Optional[str] = None) -> dict[str, Any]:
        """Expose JSON-like parameter schemas for tools/macros to prevent drift.

        This is a lightweight, hand-maintained view focusing on the most error-prone
        parameters and accepted aliases to guide clients.
        """
        default_format = settings.output_format_default or settings.toon_default_format or "json"
        payload = {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "global_optional": ["format"],
            "output_formats": {
                "default": default_format,
                "tool_param": "format",
                "resource_query": "format",
                "values": ["json", "toon"],
                "toon_envelope": {"format": "toon", "data": "<TOON>", "meta": {"requested": "toon"}},
            },
            "tools": {
                "send_message": {
                    "required": ["project_key", "sender_name", "to", "subject", "body_md", "idempotency_key"],
                    "optional": ["cc", "bcc", "attachment_paths", "convert_images", "importance", "ack_required", "thread_id", "auto_contact_if_blocked", "registration_token"],
                    "shapes": {
                        "to": "list[str]",
                        "cc": "list[str] | str",
                        "bcc": "list[str] | str",
                        "importance": "low|normal|high|urgent",
                        "auto_contact_if_blocked": "bool",
                        "attachment_paths": "reserved; any supplied value fails closed",
                        "convert_images": "reserved; any supplied value fails closed",
                    },
                },
                "reply_message": {
                    "required": ["project_key", "message_id", "sender_name", "body_md", "idempotency_key"],
                    "optional": ["to", "cc", "bcc", "subject_prefix", "registration_token"],
                },
                "rotate_registration_token": {
                    "required": [
                        "project_key",
                        "agent_name",
                        "registration_token",
                        "new_registration_token",
                    ],
                    "constraints": [
                        "new_registration_token must be exactly 64 lowercase hexadecimal characters",
                        "the caller must durably journal new_registration_token before the call",
                        "the result never contains registration_token or new_registration_token",
                    ],
                    "returns": {
                        "agent": "str",
                        "project": "str",
                        "rotated": "bool",
                        "already_current": "bool",
                    },
                },
                "macro_contact_handshake": {
                    "required": ["project_key", "requester|agent_name", "target|to_agent"],
                    "optional": [
                        "reason",
                        "ttl_seconds",
                        "auto_accept",
                        "welcome_subject",
                        "welcome_body",
                        "requester_registration_token",
                        "target_registration_token",
                    ],
                    "constraints": [
                        "target must already be registered; the macro never creates a durable Agent"
                    ],
                    "aliases": {
                        "requester": ["agent_name"],
                        "target": ["to_agent"],
                    },
                },
            },
        }
        tool_schemas = payload.get("tools")
        if isinstance(tool_schemas, dict):
            payload["tools"] = {
                tool_name: schema
                for tool_name, schema in tool_schemas.items()
                if _tool_visible_for_settings(str(tool_name), settings)
            }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/schemas",
            format_value=format,
        )

    @mcp.resource("resource://tooling/schemas", mime_type="application/json")
    def tooling_schemas_resource_exact() -> dict[str, Any]:
        return _read_tooling_schemas_resource()

    @mcp.resource("resource://tooling/schemas{?format}", mime_type="application/json")
    def tooling_schemas_resource(format: Optional[str] = None) -> dict[str, Any]:
        return _read_tooling_schemas_resource(format)

    def _read_tooling_metrics_resource(format: Optional[str] = None) -> dict[str, Any]:
        """Expose aggregated tool call/error counts for analysis."""
        payload = {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "tools": _tool_metrics_snapshot(),
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/metrics",
            format_value=format,
        )

    @mcp.resource("resource://tooling/metrics", mime_type="application/json")
    def tooling_metrics_resource_exact() -> dict[str, Any]:
        return _read_tooling_metrics_resource()

    @mcp.resource("resource://tooling/metrics{?format}", mime_type="application/json")
    def tooling_metrics_resource(format: Optional[str] = None) -> dict[str, Any]:
        return _read_tooling_metrics_resource(format)

    def _read_tooling_locks_resource(format: Optional[str] = None) -> dict[str, Any]:
        """Return lock metadata from the shared archive storage."""

        settings_local = get_settings()
        payload = collect_lock_status(settings_local)
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/locks",
            format_value=format,
        )

    @mcp.resource("resource://tooling/locks", mime_type="application/json")
    def tooling_locks_resource_exact() -> dict[str, Any]:
        return _read_tooling_locks_resource()

    @mcp.resource("resource://tooling/locks{?format}", mime_type="application/json")
    def tooling_locks_resource(format: Optional[str] = None) -> dict[str, Any]:
        return _read_tooling_locks_resource(format)

    @mcp.resource("resource://tooling/capabilities/{agent}{?project,format}", mime_type="application/json")
    def tooling_capabilities_resource(
        agent: str,
        project: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        # Parse query embedded in agent path if present (robust to FastMCP variants)
        format_value = format
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass
        caps = _capabilities_for(agent, project)
        payload = {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "agent": agent,
            "project": project,
            "capabilities": caps,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/capabilities/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://tooling/recent/{window_seconds}{?agent,project,format}", mime_type="application/json")
    def tooling_recent_resource(
        window_seconds: str,
        agent: Optional[str] = None,
        project: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        # Allow query string to be embedded in the path segment per some transports
        format_value = format
        if "?" in window_seconds:
            seg, _, qs = window_seconds.partition("?")
            window_seconds = seg
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                agent = agent or (parsed.get("agent") or [None])[0]
                project = project or (parsed.get("project") or [None])[0]
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass
        try:
            win = int(window_seconds)
        except Exception:
            win = 60
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, win))
        entries: list[dict[str, Any]] = []
        for ts, tool_name, proj, ag in list(RECENT_TOOL_USAGE):
            if ts < cutoff:
                continue
            if project and proj != project:
                continue
            if agent and ag != agent:
                continue

            record = {
                "timestamp": _iso(ts),
                "tool": tool_name,
                "project": proj,
                "agent": ag,
                "cluster": TOOL_CLUSTER_MAP.get(tool_name, "unclassified"),
            }
            entries.append(record)
        payload = {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "window_seconds": win,
            "count": len(entries),
            "entries": entries,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://tooling/recent/{window_seconds}",
            format_value=format_value,
        )

    async def _read_projects_resource(
        format: Optional[str] = None,
    ) -> JsonArrayResource:
        """
        List all projects known to the server in creation order.

        When to use
        -----------
        - Discover available projects when a user provides only an agent name.
        - Build UIs that let operators switch context between projects.

        Returns
        -------
        list[dict]
            Each: { id, slug, human_key, created_at }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r2","method":"resources/read","params":{"uri":"resource://tooling/projects"}}
        ```
        """
        settings = get_settings()
        await ensure_schema(settings)
        # Build ignore matcher for test/demo projects
        import fnmatch as _fnmatch
        ignore_patterns = set(getattr(settings, "retention_ignore_project_patterns", []) or [])
        async with get_session() as session:
            result = await session.execute(select(Project).order_by(asc(cast(Any, Project.created_at))))
            projects = result.scalars().all()
            def _is_ignored(name: str) -> bool:
                return any(_fnmatch.fnmatch(name, pat) for pat in ignore_patterns)
            filtered = [p for p in projects if not (_is_ignored(p.slug) or _is_ignored(p.human_key))]
            payload = [_project_to_dict(project) for project in filtered]
            return _apply_resource_output_format(
                payload,
                settings=settings,
                resource_name="resource://tooling/projects",
                format_value=format,
            )

    @mcp.resource("resource://tooling/projects", mime_type="application/json")
    async def projects_resource_exact() -> JsonArrayResource:
        return await _read_projects_resource()

    @mcp.resource("resource://tooling/projects{?format}", mime_type="application/json")
    async def projects_resource(
        format: Optional[str] = None,
    ) -> JsonArrayResource:
        return await _read_projects_resource(format)

    @mcp.resource("resource://project/{slug}{?format}", mime_type="application/json")
    async def project_detail(slug: str, format: Optional[str] = None) -> dict[str, Any]:
        """
        Fetch a project and its agents by project slug or human key.

        When to use
        -----------
        - Populate an "LDAP-like" directory for agents in tooling UIs.
        - Determine available agent identities and their metadata before addressing mail.

        Parameters
        ----------
        slug : str
            Project slug (or human key; both resolve to the same target).

        Returns
        -------
        dict
            Project descriptor including { agents: [...] } with agent profiles.

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r3","method":"resources/read","params":{"uri":"resource://project/backend-abc123"}}
        ```
        """
        slug_value, query_params = _split_slug_and_query(slug)
        format_value = format or query_params.get("format")
        project = await _get_project_by_identifier(slug_value)
        await ensure_schema()
        async with get_session() as session:
            result = await session.execute(
                select(Agent).where(
                    cast(Any, Agent.project_id == project.id),
                    cast(Any, Agent.provisioning_state == "active"),
                )
            )
            agents = result.scalars().all()
        payload = {
            **_project_to_dict(project),
            "agents": [_agent_to_dict(agent) for agent in agents],
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://project/{slug}",
            format_value=format_value,
        )

    @mcp.resource("resource://agents/{project_key}{?format}", mime_type="application/json")
    async def agents_directory(project_key: str, format: Optional[str] = None) -> dict[str, Any]:
        """
        List all registered agents in a project for easy agent discovery.

        This is the recommended way to discover other agents working on a project.

        When to use
        -----------
        - At the start of a coding session to see who else is working on the project.
        - Before sending messages to discover available recipients.
        - To check if a specific agent is registered before attempting contact.

        Parameters
        ----------
        project_key : str
            Project slug or human key (both work).

        Returns
        -------
        dict
            {
              "project": { "slug": "...", "human_key": "..." },
              "agents": [
                {
                  "name": "BackendDev",
                  "program": "claude-code",
                  "model": "sonnet-4.5",
                  "task_description": "API development",
                  "inception_ts": "2025-10-25T...",
                  "last_active_ts": "2025-10-25T...",
                  "unread_count": 3
                },
                ...
              ]
            }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r5","method":"resources/read","params":{"uri":"resource://agents/backend-abc123"}}
        ```

        Notes
        -----
        - Agent names are NOT the same as your program name or user name.
        - Use the returned names when calling tools like whois(), request_contact(), send_message().
        - Agents in different projects cannot see each other - project isolation is enforced.
        """
        key_value, query_params = _split_slug_and_query(project_key)
        format_value = format or query_params.get("format")
        project = await _get_project_by_identifier(key_value)
        await ensure_schema()

        async with get_session() as session:
            # Get all agents in the project
            result = await session.execute(
                select(Agent)
                .where(
                    cast(Any, Agent.project_id == project.id),
                    cast(Any, Agent.provisioning_state == "active"),
                )
                .order_by(desc(cast(Any, Agent.last_active_ts)))
            )
            agents = result.scalars().all()

            # Get unread message counts for all agents in one query
            unread_counts_stmt = (
                select(
                    MessageRecipient.agent_id,
                    func.count(cast(Any, MessageRecipient.message_id)).label("unread_count"),
                )
                .where(
                    cast(Any, MessageRecipient.read_ts).is_(None),
                    cast(Any, MessageRecipient.agent_id).in_([agent.id for agent in agents]),
                )
                .group_by(MessageRecipient.agent_id)
            )
            unread_counts_result = await session.execute(unread_counts_stmt)
            unread_counts_map = {row.agent_id: row.unread_count for row in unread_counts_result}

            # Build agent data with unread counts, separating active and retired
            agent_data = []
            retired_agent_data = []
            for agent in agents:
                agent_dict = _agent_to_dict(agent)
                agent_dict["unread_count"] = unread_counts_map.get(agent.id, 0)
                if getattr(agent, "retired_at", None) is not None:
                    retired_agent_data.append(agent_dict)
                else:
                    agent_data.append(agent_dict)

        payload = {
            "project": {
                "slug": project.slug,
                "human_key": project.human_key,
            },
            "agents": agent_data,
            "retired_agents": retired_agent_data,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://agents/{project_key}",
            format_value=format_value,
        )

    @mcp.resource("resource://file_reservations/{slug}{?active_only,format}", mime_type="application/json")
    async def file_reservations_resource(
        slug: str,
        active_only: bool = True,
        format: Optional[str] = None,
    ) -> JsonArrayResource:
        """
        List file_reservations for a project, optionally filtering to active-only.

        Why this exists
        ---------------
        - File reservations communicate edit intent and reduce collisions across agents.
        - Surfacing them helps humans review ongoing work and resolve contention.

        Parameters
        ----------
        slug : str
            Project slug or human key.
        active_only : bool
            If true (default), only returns file_reservations with no `released_ts`.

        Returns
        -------
        list[dict]
            Each file_reservation with { id, agent, path_pattern, exclusive, reason, created_ts, expires_ts, released_ts }

        Example
        -------
        ```json
        {"jsonrpc":"2.0","id":"r4","method":"resources/read","params":{"uri":"resource://file_reservations/backend-abc123?active_only=true"}}
        ```

        Also see all historical (including released) file_reservations:
        ```json
        {"jsonrpc":"2.0","id":"r4b","method":"resources/read","params":{"uri":"resource://file_reservations/backend-abc123?active_only=false"}}
        ```
        """
        slug_value, query_params = _split_slug_and_query(slug)
        format_value = format or query_params.get("format")
        if "active_only" in query_params:
            active_only = _coerce_flag_to_bool(query_params["active_only"], default=active_only)

        project = await _get_project_by_identifier(slug_value)
        await ensure_schema()
        if project.id is None:
            raise ValueError("Project must have an id before listing file_reservations.")

        await _expire_stale_file_reservations(project.id)
        statuses = await _collect_file_reservation_statuses(project, include_released=not active_only)

        payload: list[dict[str, Any]] = []
        for status in statuses:
            reservation = status.reservation
            if active_only and reservation.released_ts is not None:
                continue
            payload.append(
                {
                    "id": reservation.id,
                    # `agent` is None when the reservation is orphaned (owning
                    # agent row deleted or agent_id NULL). Callers should fall
                    # back to `agent_id` for debugging. (#161)
                    "agent": status.agent.name if status.agent is not None else None,
                    "agent_id": reservation.agent_id,
                    "execution_id": status.execution_id,
                    "execution_status": status.execution_status,
                    "execution_parent_id": status.execution_parent_id,
                    "ancestor_execution_ids": status.ancestor_execution_ids,
                    "origin": reservation.origin,
                    "orphaned": status.orphaned,
                    "legacy_unscoped": status.legacy_unscoped,
                    "path_pattern": reservation.path_pattern,
                    "exclusive": reservation.exclusive,
                    "reason": reservation.reason,
                    "created_ts": _iso(reservation.created_ts),
                    "expires_ts": _iso(reservation.expires_ts),
                    "released_ts": _iso(reservation.released_ts) if reservation.released_ts else None,
                    "stale": status.stale,
                    "stale_reasons": status.stale_reasons,
                    "last_agent_activity_ts": _iso(status.last_agent_activity) if status.last_agent_activity else None,
                    "last_execution_activity_ts": _iso(status.last_execution_activity) if status.last_execution_activity else None,
                    "last_mail_activity_ts": _iso(status.last_mail_activity) if status.last_mail_activity else None,
                    "last_filesystem_activity_ts": _iso(status.last_fs_activity) if status.last_fs_activity else None,
                    "last_git_activity_ts": _iso(status.last_git_activity) if status.last_git_activity else None,
                }
            )
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://file_reservations/{slug}",
            format_value=format_value,
        )

    async def _resolve_private_resource_agent(
        ctx: Context,
        project: Project,
        *,
        requested_agent: str | None,
        action: str,
        stateless_tool: str,
    ) -> Agent:
        """Authorize a private resource from an existing MCP session binding.

        Registration capabilities are deliberately excluded from resource URIs:
        URIs are routinely copied into logs, history, telemetry, and prompts. A
        stateless caller must use the equivalent tool, whose token is carried in
        structured tool arguments instead. ``requested_agent`` is only an
        assertion/selector among identities already bound to this exact session;
        it can never establish a new binding.
        """
        if requested_agent is not None:
            viewer = await _get_agent(project, requested_agent)
            if not _session_is_bound_to_agent(ctx, project, viewer):
                raise ToolExecutionError(
                    "AUTHENTICATION_REQUIRED",
                    (
                        f"{action} requires agent '{viewer.name}' to be already authenticated in this MCP session. "
                        f"Registration tokens are not accepted in resource URIs; stateless callers must use "
                        f"the {stateless_tool} tool."
                    ),
                    recoverable=True,
                    data={
                        "project_key": project.human_key,
                        "agent_name": viewer.name,
                        "action": action,
                        "stateless_tool": stateless_tool,
                    },
                )
        else:
            viewer = await _resolve_session_agent_for_project(ctx, project)
            if viewer is None:
                raise ToolExecutionError(
                    "AUTHENTICATION_REQUIRED",
                    (
                        f"{action} requires an Agent already authenticated in this MCP session. "
                        f"Registration tokens are not accepted in resource URIs; stateless callers must use "
                        f"the {stateless_tool} tool."
                    ),
                    recoverable=True,
                    data={
                        "project_key": project.human_key,
                        "action": action,
                        "stateless_tool": stateless_tool,
                    },
                )
        await _touch_agent_activity(viewer)
        return viewer

    @mcp.resource("resource://message/{message_id}{?project,agent,format}", mime_type="application/json")
    async def message_resource(
        ctx: Context,
        message_id: str,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Read a single message by id within a project.

        When to use
        -----------
        - Fetch the canonical body/metadata for rendering in a client after list/search.
        - Retrieve attachments and full details for a given message id.

        Parameters
        ----------
        message_id : str
            Numeric id as a string.
        project : str
            Project slug or human key (required for disambiguation).

        Common mistakes
        ---------------
        - Omitting `project` when a message id might exist in multiple projects.

        Returns
        -------
        dict
            Full message payload including body and sender name.

        Example
        -------
        The caller must already be authenticated as the viewer in this MCP
        session. The optional `agent` query parameter selects/asserts one of
        this session's existing bindings. Stateless callers must use the
        `search_messages` tool; registration tokens are never accepted in a
        resource URI.

        ```json
        {"jsonrpc":"2.0","id":"r5","method":"resources/read","params":{"uri":"resource://message/1234?project=/owner/backend&agent=codex-wsl-home-1"}}
        ```
        """
        # Support toolkits that pass query in the template segment
        format_value = format
        if "?" in message_id:
            id_part, _, qs = message_id.partition("?")
            message_id = id_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                if agent is None and parsed.get("agent"):
                    agent = parsed["agent"][0]
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass
        project_obj = await _get_project_by_identifier(
            _require_project_resource_param(project, resource_name="message resource")
        )
        viewer = await _resolve_private_resource_agent(
            ctx,
            project_obj,
            requested_agent=agent,
            action="resource://message/{message_id}",
            stateless_tool="search_messages",
        )
        message = await _get_visible_message(project_obj, viewer, int(message_id))
        sender = await _get_agent_any_project_by_id(message.sender_id)
        sender_project = await _get_project_by_id(sender.project_id)
        payload = _message_to_dict(message, include_body=True)
        _apply_sender_identity(
            payload,
            message_project_id=message.project_id,
            sender_name=sender.name,
            sender_project_id=sender_project.id,
            sender_project_human_key=sender_project.human_key,
            sender_project_slug=sender_project.slug,
        )
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://message/{message_id}",
            format_value=format_value,
        )

    @mcp.resource("resource://thread/{thread_id}{?project,agent,include_bodies,format}", mime_type="application/json")
    async def thread_resource(
        ctx: Context,
        thread_id: str,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        include_bodies: bool = False,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        List messages for a thread within a project.

        When to use
        -----------
        - Present a conversation view for a given ticket/thread key.
        - Export a thread for summarization or reporting.

        Parameters
        ----------
        thread_id : str
            Either a string thread key or a numeric message id to seed the thread.
        project : str
            Project slug or human key (required).
        include_bodies : bool
            Include message bodies if true (default false).

        Returns
        -------
        dict
            { project, thread_id, messages: [{...}] }

        Example
        -------
        The caller must already be authenticated as the viewer in this MCP
        session. The optional `agent` query parameter selects/asserts one of
        this session's existing bindings. Stateless callers must use the
        `summarize_thread` tool; registration tokens are never accepted in a
        resource URI.

        ```json
        {"jsonrpc":"2.0","id":"r6","method":"resources/read","params":{"uri":"resource://thread/TKT-123?project=/owner/backend&agent=codex-wsl-home-1&include_bodies=true"}}
        ```

        Numeric seed example (message id as thread seed):
        ```json
        {"jsonrpc":"2.0","id":"r6b","method":"resources/read","params":{"uri":"resource://thread/1234?project=/owner/backend&agent=codex-wsl-home-1"}}
        ```
        """
        # Robust query parsing: some FastMCP versions do not inject query args.
        # If the templating layer included the query string in the path segment,
        # extract it and fill missing parameters.
        format_value = format
        if "?" in thread_id:
            id_part, _, qs = thread_id.partition("?")
            thread_id = id_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and "project" in parsed and parsed["project"]:
                    project = parsed["project"][0]
                if agent is None and parsed.get("agent"):
                    agent = parsed["agent"][0]
                if parsed.get("include_bodies"):
                    val = parsed["include_bodies"][0].strip().lower()
                    include_bodies = val in ("1", "true", "t", "yes", "y")
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        project_obj = await _get_project_by_identifier(
            _require_project_resource_param(project, resource_name="thread resource")
        )
        viewer = await _resolve_private_resource_agent(
            ctx,
            project_obj,
            requested_agent=agent,
            action="resource://thread/{thread_id}",
            stateless_tool="summarize_thread",
        )

        if project_obj.id is None:
            raise ValueError("Project must have an id before listing threads.")
        await ensure_schema()
        try:
            message_id = int(thread_id)
        except ValueError:
            message_id = None
        sender_alias = aliased(Agent)
        sender_project_alias = aliased(Project)
        criteria = [Message.thread_id == thread_id]
        if message_id is not None:
            criteria.append(Message.id == message_id)
        async with get_session() as session:
            stmt = (
                select(
                    Message,
                    sender_alias.name,
                    sender_project_alias.id,
                    sender_project_alias.human_key,
                    sender_project_alias.slug,
                )
                .join(sender_alias, cast(Any, Message.sender_id == sender_alias.id))
                .join(sender_project_alias, cast(Any, sender_alias.project_id == sender_project_alias.id))
                .where(
                    cast(Any, Message.project_id == project_obj.id),
                    or_(*cast(Any, criteria)),
                    _message_visible_to_agent_clause(viewer.id or 0),
                )
                .order_by(asc(cast(Any, Message.created_ts)))
            )
            result = await session.execute(stmt)
            rows = result.all()
        messages = []
        for message, sender_name, sender_project_id, sender_project_human_key, sender_project_slug in rows:
            payload = _message_to_dict(message, include_body=include_bodies)
            _apply_sender_identity(
                payload,
                message_project_id=message.project_id,
                sender_name=sender_name,
                sender_project_id=sender_project_id,
                sender_project_human_key=sender_project_human_key,
                sender_project_slug=sender_project_slug,
            )
            messages.append(payload)
        payload = {"project": project_obj.human_key, "thread_id": thread_id, "messages": messages}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://thread/{thread_id}",
            format_value=format_value,
        )

    @mcp.resource(
        "resource://inbox/{agent}{?project,since_ts,urgent_only,include_bodies,limit,format}",
        mime_type="application/json",
    )
    async def inbox_resource(
        ctx: Context,
        agent: str,
        project: Optional[str] = None,
        since_ts: Optional[str] = None,
        urgent_only: bool = False,
        include_bodies: bool = False,
        limit: int = 20,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Read an agent's inbox for a project.

        Parameters
        ----------
        agent : str
            Agent name.
        project : str
            Project slug or human key (required).
        since_ts : Optional[str]
            ISO-8601 timestamp string; only messages newer than this are returned.
        urgent_only : bool
            If true, limits to importance in {high, urgent}.
        include_bodies : bool
            Include message bodies in results (default false).
        limit : int
            Maximum number of messages to return (default 20).

        Returns
        -------
        dict
            { project, agent, count, messages: [...] }

        Example
        -------
        The caller must already be authenticated as this agent in the current
        MCP session. Stateless callers must use the `fetch_inbox` tool;
        registration tokens are never accepted in a resource URI.

        ```json
        {"jsonrpc":"2.0","id":"r7","method":"resources/read","params":{"uri":"resource://inbox/codex-wsl-home-1?project=/owner/backend&limit=10&urgent_only=true"}}
        ```
        Incremental fetch example (using since_ts):
        ```json
        {"jsonrpc":"2.0","id":"r7b","method":"resources/read","params":{"uri":"resource://inbox/codex-wsl-home-1?project=/owner/backend&since_ts=2025-10-23T15:00:00Z"}}
        ```
        """
        # Robust query parsing: some FastMCP versions do not inject query args.
        # If the templating layer included the query string in the last path segment,
        # extract it and fill missing parameters.
        format_value = format
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            from urllib.parse import parse_qs
            parsed = parse_qs(qs, keep_blank_values=False)
            try:
                if project is None and "project" in parsed and parsed["project"]:
                    project = parsed["project"][0]
                if since_ts is None and "since_ts" in parsed and parsed["since_ts"]:
                    since_ts = parsed["since_ts"][0]
                if parsed.get("urgent_only"):
                    val = parsed["urgent_only"][0].strip().lower()
                    urgent_only = val in ("1", "true", "t", "yes", "y")
                if parsed.get("include_bodies"):
                    val = parsed["include_bodies"][0].strip().lower()
                    include_bodies = val in ("1", "true", "t", "yes", "y")
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass
            # Parse/validate limit OUTSIDE the suppressing block so an invalid
            # value raises (issue #178) instead of being silently ignored and
            # falling through to an unbounded DB query.
            limit = _parse_resource_limit(parsed, default=limit)

        # Guard the limit even when no query string was embedded in the path.
        limit = _validate_limit(limit)
        project_obj = await _get_project_by_identifier(
            _require_project_resource_param(project, resource_name="inbox resource")
        )
        agent_obj = await _resolve_private_resource_agent(
            ctx,
            project_obj,
            requested_agent=agent,
            action="resource://inbox/{agent}",
            stateless_tool="fetch_inbox",
        )
        messages = await _list_inbox(project_obj, agent_obj, limit, urgent_only, include_bodies, since_ts)
        # Enrich with commit info for canonical markdown files (best-effort)
        enriched: list[dict[str, Any]] = []
        for item in messages:
            try:
                msg_obj = await _get_message(project_obj, int(item["id"]))
                commit_info = await _commit_info_for_message(settings, project_obj, msg_obj)
                if commit_info:
                    item["commit"] = commit_info
            except Exception:
                pass
            enriched.append(item)
        payload = {
            "project": project_obj.human_key,
            "agent": agent_obj.name,
            "count": len(enriched),
            "messages": enriched,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://inbox/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://views/urgent-unread/{agent}{?project,limit,format}", mime_type="application/json")
    async def urgent_unread_view(
        ctx: Context,
        agent: str,
        project: Optional[str] = None,
        limit: int = 20,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Convenience view listing urgent and high-importance messages that are unread for an agent.

        Parameters
        ----------
        agent : str
            Agent name.
        project : str
            Project slug or human key (required).
        limit : int
            Max number of messages.
        """
        # Parse query embedded in agent path if present
        format_value = format
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                limit = _parse_resource_limit(parsed, default=limit)
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        project_obj = await _get_project_by_identifier(
            _require_project_resource_param(project, resource_name="urgent view")
        )
        agent_obj = await _resolve_private_resource_agent(
            ctx,
            project_obj,
            requested_agent=agent,
            action="resource://views/urgent-unread/{agent}",
            stateless_tool="fetch_inbox",
        )
        # Single SQL query: urgent + unread filter at the DB layer. Fixes a
        # prior N+1 (one read-state probe per row) and a limit-before-filter
        # correctness bug where the page of `limit` urgent messages would be
        # narrowed to unread *after* the LIMIT — so users frequently saw
        # fewer than `limit` unread items even when more existed.
        unread = await _list_inbox(
            project_obj,
            agent_obj,
            limit,
            urgent_only=True,
            include_bodies=False,
            since_ts=None,
            unread_only=True,
        )
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(unread), "messages": unread}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://views/urgent-unread/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://views/ack-required/{agent}{?project,limit,format}", mime_type="application/json")
    async def ack_required_view(
        ctx: Context,
        agent: str,
        project: Optional[str] = None,
        limit: int = 20,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Convenience view listing messages requiring acknowledgement for an agent where ack is pending.

        Parameters
        ----------
        agent : str
            Agent name.
        project : str
            Project slug or human key (required).
        limit : int
            Max number of messages.
        """
        # Parse query embedded in agent path if present
        format_value = format
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                limit = _parse_resource_limit(parsed, default=limit)
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        project_obj = await _get_project_by_identifier(
            _require_project_resource_param(project, resource_name="ack view")
        )
        agent_obj = await _resolve_private_resource_agent(
            ctx,
            project_obj,
            requested_agent=agent,
            action="resource://views/ack-required/{agent}",
            stateless_tool="fetch_inbox",
        )
        if project_obj.id is None or agent_obj.id is None:
            raise ValueError("Project/agent IDs must exist")
        await ensure_schema()
        out: list[dict[str, Any]] = []
        async with get_session() as session:
            rows = await session.execute(
                select(Message, MessageRecipient.kind)
                .join(MessageRecipient, cast(Any, MessageRecipient.message_id == Message.id))
                .where(
                    cast(Any, Message.project_id) == project_obj.id,
                    cast(Any, MessageRecipient.agent_id == agent_obj.id),
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                )
                .order_by(desc(cast(Any, Message.created_ts)))
                .limit(limit)
            )
            for msg, kind in rows.all():
                payload = _message_to_dict(msg, include_body=False)
                payload["kind"] = kind
                out.append(payload)
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(out), "messages": out}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://views/ack-required/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://views/acks-stale/{agent}{?project,ttl_seconds,limit,format}", mime_type="application/json")
    async def acks_stale_view(
        ctx: Context,
        agent: str,
        project: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        limit: int = 20,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        List ack-required messages older than a TTL where acknowledgement is still missing.

        Parameters
        ----------
        agent : str
            Agent name.
        project : str
            Project slug or human key (required).
        ttl_seconds : Optional[int]
            Minimum age in seconds to consider a message stale. Defaults to settings.ack_ttl_seconds.
        limit : int
            Max number of messages to return.
        """
        # Parse query embedded in agent path if present
        format_value = format
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                if parsed.get("ttl_seconds"):
                    with suppress(Exception):
                        ttl_seconds = int(parsed["ttl_seconds"][0])
                limit = _parse_resource_limit(parsed, default=limit)
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        project_obj = await _get_project_by_identifier(
            _require_project_resource_param(project, resource_name="stale acks view")
        )
        agent_obj = await _resolve_private_resource_agent(
            ctx,
            project_obj,
            requested_agent=agent,
            action="resource://views/acks-stale/{agent}",
            stateless_tool="fetch_inbox",
        )
        if project_obj.id is None or agent_obj.id is None:
            raise ValueError("Project/agent IDs must exist")
        await ensure_schema()
        ttl = int(ttl_seconds) if ttl_seconds is not None else get_settings().ack_ttl_seconds
        now = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        async with get_session() as session:
            rows = await session.execute(
                select(Message, MessageRecipient.kind, MessageRecipient.read_ts)
                .join(MessageRecipient, cast(Any, MessageRecipient.message_id == Message.id))
                .where(
                    cast(Any, Message.project_id) == project_obj.id,
                    cast(Any, MessageRecipient.agent_id == agent_obj.id),
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                )
                .order_by(asc(cast(Any, Message.created_ts)))
                .limit(limit * 5)
            )
            for msg, kind, read_ts in rows.all():
                # Coerce potential naive datetimes from SQLite to UTC for arithmetic
                created = msg.created_ts
                if getattr(created, "tzinfo", None) is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_s = int((now - created).total_seconds())
                if age_s >= ttl:
                    payload = _message_to_dict(msg, include_body=False)
                    payload["kind"] = kind
                    payload["read_at"] = _iso(read_ts) if read_ts else None
                    payload["age_seconds"] = age_s
                    out.append(payload)
                    if len(out) >= limit:
                        break
        payload = {
            "project": project_obj.human_key,
            "agent": agent_obj.name,
            "ttl_seconds": ttl,
            "count": len(out),
            "messages": out,
        }
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://views/acks-stale/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://views/ack-overdue/{agent}{?project,ttl_minutes,limit,format}", mime_type="application/json")
    async def ack_overdue_view(
        ctx: Context,
        agent: str,
        project: Optional[str] = None,
        ttl_minutes: int = 60,
        limit: int = 50,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """List messages requiring acknowledgement older than ttl_minutes without ack."""
        # Parse query embedded in agent path if present
        format_value = format
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                if parsed.get("ttl_minutes"):
                    with suppress(Exception):
                        ttl_minutes = int(parsed["ttl_minutes"][0])
                limit = _parse_resource_limit(parsed, default=limit)
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        project_obj = await _get_project_by_identifier(
            _require_project_resource_param(project, resource_name="ack-overdue view")
        )
        agent_obj = await _resolve_private_resource_agent(
            ctx,
            project_obj,
            requested_agent=agent,
            action="resource://views/ack-overdue/{agent}",
            stateless_tool="fetch_inbox",
        )
        if project_obj.id is None or agent_obj.id is None:
            raise ValueError("Project/agent IDs must exist")
        await ensure_schema()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, ttl_minutes))
        out: list[dict[str, Any]] = []
        async with get_session() as session:
            rows = await session.execute(
                select(Message, MessageRecipient.kind)
                .join(MessageRecipient, cast(Any, MessageRecipient.message_id == Message.id))
                .where(
                    cast(Any, Message.project_id) == project_obj.id,
                    cast(Any, MessageRecipient.agent_id == agent_obj.id),
                    cast(Any, Message.ack_required).is_(True),
                    cast(Any, MessageRecipient.ack_ts).is_(None),
                )
                .order_by(asc(cast(Any, Message.created_ts)))
                .limit(limit * 5)
            )
            for msg, kind in rows.all():
                created = msg.created_ts
                if getattr(created, "tzinfo", None) is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created <= cutoff:
                    payload = _message_to_dict(msg, include_body=False)
                    payload["kind"] = kind
                    out.append(payload)
                    if len(out) >= limit:
                        break
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(out), "messages": out}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://views/ack-overdue/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://mailbox/{agent}{?project,limit,format}", mime_type="application/json")
    async def mailbox_resource(
        ctx: Context,
        agent: str,
        project: Optional[str] = None,
        limit: int = 20,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        List recent messages in an agent's mailbox with lightweight Git commit context.

        Returns
        -------
        dict
            { project, agent, count, messages: [{ id, subject, from, created_ts, importance, ack_required, kind, commit: {hexsha, summary} | null }] }
        """
        # Parse query embedded in agent path if present
        format_value = format
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                limit = _parse_resource_limit(parsed, default=limit)
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        project_obj = await _get_project_by_identifier(
            _require_project_resource_param(project, resource_name="mailbox resource")
        )
        agent_obj = await _resolve_private_resource_agent(
            ctx,
            project_obj,
            requested_agent=agent,
            action="resource://mailbox/{agent}",
            stateless_tool="fetch_inbox",
        )
        items = await _list_inbox(project_obj, agent_obj, limit, urgent_only=False, include_bodies=False, since_ts=None)

        out: list[dict[str, Any]] = []
        for item in items:
            payload = dict(item)
            try:
                msg_obj = await _get_message(project_obj, int(item["id"]))
                commit_info = await _commit_info_for_message(settings, project_obj, msg_obj)
                if commit_info:
                    payload["commit"] = commit_info
            except Exception:
                pass
            out.append(payload)
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(out), "messages": out}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://mailbox/{agent}",
            format_value=format_value,
        )

    @mcp.resource(
        "resource://mailbox-with-commits/{agent}{?project,limit,format}",
        mime_type="application/json",
    )
    async def mailbox_with_commits_resource(
        ctx: Context,
        agent: str,
        project: Optional[str] = None,
        limit: int = 20,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """List recent messages in an agent's mailbox with commit metadata including diff summaries."""
        # Parse query embedded in agent path if present
        format_value = format
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                limit = _parse_resource_limit(parsed, default=limit)
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass
        project_obj = await _get_project_by_identifier(
            _require_project_resource_param(project, resource_name="mailbox-with-commits resource")
        )
        agent_obj = await _resolve_private_resource_agent(
            ctx,
            project_obj,
            requested_agent=agent,
            action="resource://mailbox-with-commits/{agent}",
            stateless_tool="fetch_inbox",
        )
        items = await _list_inbox(project_obj, agent_obj, limit, urgent_only=False, include_bodies=False, since_ts=None)

        enriched: list[dict[str, Any]] = []
        for item in items:
            try:
                msg_obj = await _get_message(project_obj, int(item["id"]))
                commit_info = await _commit_info_for_message(settings, project_obj, msg_obj)
                if commit_info:
                    item["commit"] = commit_info
            except Exception:
                pass
            enriched.append(item)
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(enriched), "messages": enriched}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://mailbox-with-commits/{agent}",
            format_value=format_value,
        )

    @mcp.resource("resource://outbox/{agent}{?project,limit,include_bodies,since_ts,format}", mime_type="application/json")
    async def outbox_resource(
        ctx: Context,
        agent: str,
        project: Optional[str] = None,
        limit: int = 20,
        include_bodies: bool = False,
        since_ts: Optional[str] = None,
        format: Optional[str] = None,
    ) -> dict[str, Any]:
        """List messages sent by the agent, enriched with commit metadata for canonical files."""
        # Support toolkits that incorrectly pass query in the template segment
        format_value = format
        if "?" in agent:
            name_part, _, qs = agent.partition("?")
            agent = name_part
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(qs, keep_blank_values=False)
                if project is None and parsed.get("project"):
                    project = parsed["project"][0]
                if parsed.get("limit"):
                    from contextlib import suppress
                    with suppress(Exception):
                        limit = int(parsed["limit"][0])
                if parsed.get("include_bodies"):
                    include_bodies = parsed["include_bodies"][0].lower() in {"1","true","t","yes","y"}
                if parsed.get("since_ts"):
                    since_ts = parsed["since_ts"][0]
                format_value = format_value or _extract_format_param(parsed)
            except Exception:
                pass

        project_obj = await _get_project_by_identifier(
            _require_project_resource_param(project, resource_name="outbox resource")
        )
        agent_obj = await _resolve_private_resource_agent(
            ctx,
            project_obj,
            requested_agent=agent,
            action="resource://outbox/{agent}",
            stateless_tool="search_messages",
        )
        items = await _list_outbox(project_obj, agent_obj, limit, include_bodies, since_ts)
        enriched: list[dict[str, Any]] = []
        for item in items:
            try:
                msg_obj = await _get_message(project_obj, int(item["id"]))
                commit_info = await _commit_info_for_message(settings, project_obj, msg_obj)
                if commit_info:
                    item["commit"] = commit_info
            except Exception:
                pass
            enriched.append(item)
        payload = {"project": project_obj.human_key, "agent": agent_obj.name, "count": len(enriched), "messages": enriched}
        return _apply_resource_output_format(
            payload,
            settings=settings,
            resource_name="resource://outbox/{agent}",
            format_value=format_value,
        )

    # No explicit output-schema transform; the tool returns ToolResult with {"result": ...}

    # -------------------------------------------------------------------------------------------------
    # Tool Filtering: Remove tools that shouldn't be exposed based on settings
    # -------------------------------------------------------------------------------------------------
    if settings.tool_filter.enabled:
        _apply_tool_filter(mcp, settings)

    return mcp


def _apply_tool_filter(mcp: FastMCP, settings: Settings) -> None:
    """Disable filtered tools through FastMCP's public visibility API.

    The transform belongs to this server instance, so building a filtered server
    cannot mutate global metadata or hide tools from a later unfiltered server.
    """
    to_disable: set[str] = set()
    for tool_name in TOOL_CLUSTER_MAP:
        if not _tool_visible_for_settings(tool_name, settings):
            to_disable.add(tool_name)

    if to_disable:
        mcp.disable(names=to_disable, components={"tool"})
        profile = settings.tool_filter.profile
        logger.info(
            "Tool filtering active (profile=%s): disabled %d tools",
            profile,
            len(to_disable),
        )
