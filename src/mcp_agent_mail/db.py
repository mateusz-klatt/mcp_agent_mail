"""Async database engine and session management utilities.

This module provides robust SQLite handling for high-concurrency multi-agent workloads:

Concurrency Architecture:
- WAL mode with optimized checkpoint strategy (passive checkpoints to avoid blocking)
- Connection pooling with conservative limits to prevent file descriptor exhaustion
- Exponential backoff with jitter on lock contention (prevents thundering herd)
- Circuit breaker pattern to fail fast during prolonged database issues

Key invariants:
- One writer at a time (SQLite constraint), concurrent readers allowed
- Connections recycled after 1 hour to prevent stale handle accumulation
- Pool timeout of 30s fails fast with clear error vs hanging indefinitely
- busy_timeout of 60s gives writers time to complete during checkpoint
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Final, TypeVar, cast

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from sqlalchemy import CheckConstraint, MetaData, inspect as sa_inspect
from sqlalchemy.exc import OperationalError, TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql.schema import Table
from sqlmodel import SQLModel

from .config import DatabaseSettings, Settings, clear_settings_cache, get_settings
from .models import (
    MAIL_UI_LOCALE_VALUES,
    Agent,
    AgentExecution,
    MessageDelivery,
    MessageDeliveryRecipient,
    Project,
    UiUser,
)

T = TypeVar("T")
_logger = logging.getLogger(__name__)

# Backoff jitter source; SystemRandom keeps the uniform distribution while
# avoiding the seedable module-level Mersenne Twister state.
_jitter_rng = secrets.SystemRandom()

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_schema_ready = False
_schema_lock: asyncio.Lock | None = None

# Circuit breaker state for database operations
_circuit_breaker_failures: int = 0
_circuit_breaker_last_failure: float = 0.0
_circuit_breaker_open_until: float = 0.0
_CIRCUIT_BREAKER_THRESHOLD: int = 5  # Failures before opening circuit
_CIRCUIT_BREAKER_RESET_SECONDS: float = 30.0  # Time before half-open state
_CIRCUIT_BREAKER_LOCK: asyncio.Lock | None = None


class CircuitState(Enum):
    """Circuit breaker states for database operations."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing fast, not attempting operations
    HALF_OPEN = "half_open"  # Testing if service recovered


def _get_circuit_breaker_lock() -> asyncio.Lock:
    """Get or create circuit breaker lock (must be called from async context)."""
    global _CIRCUIT_BREAKER_LOCK
    if _CIRCUIT_BREAKER_LOCK is None:
        _CIRCUIT_BREAKER_LOCK = asyncio.Lock()
    return _CIRCUIT_BREAKER_LOCK


def get_circuit_state() -> CircuitState:
    """Get current circuit breaker state (non-blocking check)."""
    global _circuit_breaker_open_until, _circuit_breaker_failures
    now = time.monotonic()
    if _circuit_breaker_open_until > now:
        return CircuitState.OPEN
    if _circuit_breaker_failures >= _CIRCUIT_BREAKER_THRESHOLD:
        # Circuit was open but timeout passed - now half-open
        return CircuitState.HALF_OPEN
    return CircuitState.CLOSED


async def _record_circuit_success() -> None:
    """Record successful operation - reset circuit breaker."""
    global _circuit_breaker_failures, _circuit_breaker_open_until
    async with _get_circuit_breaker_lock():
        _circuit_breaker_failures = 0
        _circuit_breaker_open_until = 0.0


async def _record_circuit_failure() -> None:
    """Record failed operation - potentially open circuit breaker."""
    global _circuit_breaker_failures, _circuit_breaker_last_failure, _circuit_breaker_open_until
    async with _get_circuit_breaker_lock():
        now = time.monotonic()
        _circuit_breaker_failures += 1
        _circuit_breaker_last_failure = now
        if _circuit_breaker_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            _circuit_breaker_open_until = now + _CIRCUIT_BREAKER_RESET_SECONDS
            _logger.warning(
                "circuit_breaker.opened",
                extra={
                    "failures": _circuit_breaker_failures,
                    "reset_seconds": _CIRCUIT_BREAKER_RESET_SECONDS,
                },
            )


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open and operation should not proceed."""
    pass

_QUERY_TRACKER: contextvars.ContextVar["QueryTracker | None"] = contextvars.ContextVar("query_tracker", default=None)
_QUERY_HOOKS_INSTALLED = False
_SLOW_QUERY_LIMIT = 50
_MAIL_UI_LOCALE_SQL = ", ".join(repr(value) for value in MAIL_UI_LOCALE_VALUES)
_SQL_TABLE_RE = re.compile(r"\bfrom\s+([\w\.\"`\[\]]+)", re.IGNORECASE)
_SQL_UPDATE_RE = re.compile(r"\bupdate\s+([\w\.\"`\[\]]+)", re.IGNORECASE)
_SQL_INSERT_RE = re.compile(r"\binsert\s+into\s+([\w\.\"`\[\]]+)", re.IGNORECASE)


@dataclass(slots=True)
class QueryTracker:
    total: int = 0
    total_time_ms: float = 0.0
    per_table: dict[str, int] = field(default_factory=dict)
    slow_query_ms: float | None = None
    slow_queries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, statement: str, duration_ms: float) -> None:
        self.total += 1
        self.total_time_ms += duration_ms
        table = _extract_table_name(statement)
        if table:
            self.per_table[table] = self.per_table.get(table, 0) + 1
        if (
            self.slow_query_ms is not None
            and duration_ms >= self.slow_query_ms
            and len(self.slow_queries) < _SLOW_QUERY_LIMIT
        ):
            self.slow_queries.append(
                {
                    "table": table,
                    "duration_ms": round(duration_ms, 2),
                }
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "total_time_ms": round(self.total_time_ms, 2),
            "per_table": dict(sorted(self.per_table.items(), key=lambda item: (-item[1], item[0]))),
            "slow_query_ms": self.slow_query_ms,
            "slow_queries": list(self.slow_queries),
        }


def _clean_table_name(raw: str) -> str:
    cleaned = raw.strip()
    if "." in cleaned:
        cleaned = cleaned.split(".")[-1]
    return cleaned.strip("`\"[]")


def _extract_table_name(statement: str) -> str | None:
    for pattern in (_SQL_INSERT_RE, _SQL_UPDATE_RE, _SQL_TABLE_RE):
        match = pattern.search(statement)
        if match:
            return _clean_table_name(match.group(1))
    return None


def get_query_tracker() -> QueryTracker | None:
    return _QUERY_TRACKER.get()


def start_query_tracking(*, slow_ms: float | None = None) -> tuple[QueryTracker, contextvars.Token]:
    tracker = QueryTracker(slow_query_ms=slow_ms)
    token = _QUERY_TRACKER.set(tracker)
    return tracker, token


def stop_query_tracking(token: contextvars.Token) -> None:
    _QUERY_TRACKER.reset(token)


@contextmanager
def track_queries(*, slow_ms: float | None = None) -> Iterator[QueryTracker]:
    tracker, token = start_query_tracking(slow_ms=slow_ms)
    try:
        yield tracker
    finally:
        stop_query_tracking(token)


def _is_lock_error(error_msg: str) -> bool:
    """Check if error message indicates a database lock error."""
    lower_msg = error_msg.lower()
    return any(
        phrase in lower_msg
        for phrase in [
            "database is locked",
            "database is busy",
            "locked",
            "unable to open database",  # Can happen during checkpoint
            "disk i/o error",  # Sometimes masks lock issues
        ]
    )


def _is_pool_exhausted_error(exc: Exception) -> bool:
    """Check if exception indicates connection pool exhaustion."""
    if isinstance(exc, SATimeoutError):
        return True
    error_msg = str(exc).lower()
    return "pool" in error_msg and ("timeout" in error_msg or "exhausted" in error_msg)


def retry_on_db_lock(
    max_retries: int = 7,
    base_delay: float = 0.05,
    max_delay: float = 8.0,
    use_circuit_breaker: bool = True,
) -> Callable[..., Any]:
    """Decorator to retry async functions on SQLite database lock errors with exponential backoff + jitter.

    Args:
        max_retries: Maximum number of retry attempts (default: 7 for ~12.7s total backoff)
        base_delay: Initial delay in seconds (default: 0.05s for faster initial retry)
        max_delay: Maximum delay between retries in seconds
        use_circuit_breaker: Whether to integrate with circuit breaker (default: True)

    This handles transient "database is locked" errors from SQLite by:
    1. Checking circuit breaker state before attempting operation
    2. Catching OperationalError with lock-related messages
    3. Waiting with exponential backoff: base_delay * (2 ** attempt)
    4. Adding jitter to prevent thundering herd: random ±25% of delay
    5. Recording success/failure for circuit breaker state management
    6. Giving up after max_retries and re-raising the error

    Backoff schedule with defaults (0.05s base, 7 retries):
        Attempt 1: 0.05s, Attempt 2: 0.1s, Attempt 3: 0.2s, Attempt 4: 0.4s,
        Attempt 5: 0.8s, Attempt 6: 1.6s, Attempt 7: 3.2s
        Total max wait: ~6.35s (plus jitter)
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None
            func_name = getattr(func, "__name__", getattr(func, "__qualname__", "<callable>"))

            # Check circuit breaker state
            if use_circuit_breaker:
                state = get_circuit_state()
                if state == CircuitState.OPEN:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker is open for database operations. "
                        f"Function {func_name} will not be attempted. "
                        f"This typically indicates sustained database lock contention."
                    )

            for attempt in range(max_retries + 1):
                try:
                    result = await func(*args, **kwargs)
                    # Success - reset circuit breaker if it had any accumulated failures,
                    # even on the first attempt (allows recovery from HALF_OPEN state).
                    if use_circuit_breaker and _circuit_breaker_failures > 0:
                        await _record_circuit_success()
                    return result

                except (OperationalError, SATimeoutError) as e:
                    error_msg = str(e)
                    is_lock = _is_lock_error(error_msg)
                    is_pool = _is_pool_exhausted_error(e)

                    if not (is_lock or is_pool) or attempt >= max_retries:
                        # Not a retryable error, or we've exhausted retries
                        if use_circuit_breaker:
                            await _record_circuit_failure()
                        raise

                    last_exception = e

                    # Calculate exponential backoff with jitter
                    delay = min(base_delay * (2**attempt), max_delay)
                    # Add ±25% jitter to prevent thundering herd
                    jitter = delay * 0.25 * (2 * _jitter_rng.random() - 1)
                    total_delay = max(0.01, delay + jitter)  # Ensure positive delay

                    error_type = "pool_exhausted" if is_pool else "db_locked"
                    _logger.warning(
                        f"db.{error_type}",
                        extra={
                            "function": func_name,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "delay_seconds": round(total_delay, 3),
                            "error": error_msg[:200],
                        },
                    )

                    await asyncio.sleep(total_delay)

            # Should never reach here, but just in case
            if use_circuit_breaker:
                await _record_circuit_failure()
            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected retry loop exit")

        return wrapper

    return decorator


def _build_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Build async SQLAlchemy engine with SQLite-optimized settings for high-concurrency multi-agent workloads.

    SQLite Concurrency Tuning:
    - WAL mode: Allows concurrent readers + one writer (vs default rollback journal)
    - NORMAL sync: 10x faster than FULL, still durable (WAL provides crash safety)
    - busy_timeout=60s: Extended timeout during checkpoint operations
    - wal_autocheckpoint=1000: Checkpoint every 1000 pages (~4MB) to prevent WAL bloat
    - cache_size=-32768: 32MB page cache for better read performance

    Pool Tuning:
    - Higher default pool size for bursty multi-agent workloads (50 base for SQLite)
    - 45s pool timeout - long enough for checkpoint but not indefinite
    - pool_pre_ping: Detect and recycle stale connections
    """
    from sqlalchemy import event
    from sqlalchemy.engine import make_url

    # For SQLite, enable WAL mode and set timeout for better concurrent access
    connect_args = {}
    is_sqlite = "sqlite" in settings.url.lower()

    if is_sqlite:
        # Ensure parent directory exists for file-backed SQLite URLs.
        # SQLite returns "unable to open database file" when the directory is missing.
        try:
            parsed = make_url(settings.url)
            if parsed.database and parsed.database != ":memory:":
                Path(parsed.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Register datetime adapters ONCE globally for Python 3.12+ compatibility
        # These are module-level registrations, not per-connection
        import datetime as dt_module
        import sqlite3

        def adapt_datetime_iso(val: Any) -> str:
            """Adapt datetime.datetime to ISO 8601 date."""
            return str(val.isoformat())

        def convert_datetime(val: bytes | str) -> dt_module.datetime | None:
            """Convert ISO 8601 datetime to datetime.datetime object.

            Returns None for any conversion errors (invalid format, wrong type,
            corrupted data, etc.) to allow graceful degradation rather than crashing.
            """
            try:
                # Handle both bytes and str (SQLite can return either)
                if isinstance(val, bytes):
                    val = val.decode('utf-8')
                return dt_module.datetime.fromisoformat(val)
            except (ValueError, AttributeError, TypeError, UnicodeDecodeError, OverflowError):
                # Return None for any conversion failure:
                # - ValueError: invalid ISO format string
                # - TypeError: unexpected type (shouldn't happen but defensive)
                # - AttributeError: val has no expected attributes (defensive)
                # - UnicodeDecodeError: corrupted bytes (extreme edge case)
                # - OverflowError: datetime value out of valid range (year outside 1-9999)
                return None

        # Register adapters globally (safe to call multiple times - last registration wins)
        sqlite3.register_adapter(dt_module.datetime, adapt_datetime_iso)
        sqlite3.register_converter("timestamp", convert_datetime)

        connect_args = {
            "timeout": 60.0,  # Extended timeout (60s) to handle checkpoint stalls
            "check_same_thread": False,  # Required for async SQLite
        }

    # SQLite concurrency tuning:
    # - Larger pool to support high-concurrency multi-agent workloads (50 base + 4 overflow = 54 max connections)
    # - Longer timeout to handle WAL checkpoint blocking
    # For non-SQLite (PostgreSQL, etc.), keep existing defaults unless overridden
    pool_size = settings.pool_size if settings.pool_size is not None else (50 if is_sqlite else 25)
    max_overflow = settings.max_overflow if settings.max_overflow is not None else (4 if is_sqlite else 25)
    pool_timeout = settings.pool_timeout if settings.pool_timeout is not None else (45 if is_sqlite else 30)

    engine = create_async_engine(
        settings.url,
        echo=settings.echo,
        future=True,
        pool_pre_ping=True,  # Detect and recycle stale connections
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,  # Extended timeout for SQLite checkpoint scenarios
        pool_recycle=1800,  # Recycle connections every 30 minutes (was 1 hour)
        pool_reset_on_return="rollback",  # Ensure uncommitted transactions are rolled back on return
        connect_args=connect_args,
    )

    # For SQLite: Set up event listener to configure each connection with optimized PRAGMAs
    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_conn: Any, connection_record: Any) -> None:
            """Set SQLite PRAGMAs for high-concurrency multi-agent performance.

            These settings are optimized for scenarios with many concurrent agents
            reading and writing to the same database:

            - journal_mode=WAL: Write-Ahead Logging for concurrent reads during writes
            - synchronous=FULL: durable commits across process, OS, and power failures
            - busy_timeout=60000: 60s wait for locks (handles checkpoint stalls)
            - wal_autocheckpoint=1000: Checkpoint every ~4MB to prevent WAL bloat
            - cache_size=-32768: 32MB page cache (negative = KB, positive = pages)
            - temp_store=MEMORY: Temp tables in memory for faster operations
            - mmap_size=268435456: 256MB memory-mapped I/O for faster reads
            """
            cursor = dbapi_conn.cursor()
            try:
                # SQLite leaves foreign-key enforcement disabled on every new
                # connection unless the application enables it explicitly.
                # This must be connection-local: enabling it on the one
                # connection used by a schema migration does not protect later
                # pool checkouts.
                cursor.execute("PRAGMA foreign_keys=ON")

                # Enable WAL mode for concurrent reads/writes
                # This is persistent - only needs to be set once per database file
                cursor.execute("PRAGMA journal_mode=WAL")

                # Delivery receipts cross the SQLite/Git durability boundary. FULL
                # ensures a successful DB commit remains durable after an OS crash or
                # power loss; NORMAL only guarantees application-crash durability.
                cursor.execute("PRAGMA synchronous=FULL")

                # Extended busy timeout (60 seconds) to handle:
                # - WAL checkpoint blocking (can take seconds with large WAL)
                # - Concurrent write contention from multiple agents
                cursor.execute("PRAGMA busy_timeout=60000")

                # WAL autocheckpoint: checkpoint every 1000 pages (~4MB)
                # Prevents WAL file from growing unbounded while not checkpointing too often
                # Default is 1000, but setting explicitly for documentation
                cursor.execute("PRAGMA wal_autocheckpoint=1000")

                # Larger page cache (32MB) for better read performance
                # Negative value = KB, positive = pages
                cursor.execute("PRAGMA cache_size=-32768")

                # Keep temp tables in memory for faster operations
                cursor.execute("PRAGMA temp_store=MEMORY")

                # Enable memory-mapped I/O for faster reads (256MB limit)
                # This is particularly helpful for read-heavy workloads
                cursor.execute("PRAGMA mmap_size=268435456")

                # REPLACE normally suppresses its implicit DELETE triggers when
                # recursion is disabled. Audit rows also have a BEFORE INSERT
                # collision guard, but enabling recursion closes that SQLite
                # escape hatch for every trigger-backed invariant.
                cursor.execute("PRAGMA recursive_triggers=ON")

            finally:
                cursor.close()

        @event.listens_for(engine.sync_engine, "checkin")
        def on_checkin(dbapi_conn: Any, connection_record: Any) -> None:
            """Perform passive WAL checkpoint when connection returns to pool.

            PASSIVE checkpoint doesn't block writers - it only checkpoints pages
            that can be checkpointed without waiting. This helps keep WAL size
            manageable without causing lock contention.
            """
            try:
                cursor = dbapi_conn.cursor()
                try:
                    # PASSIVE mode: checkpoint what we can without blocking
                    # Returns (blocked, wal_pages, checkpointed_pages)
                    cursor.execute("PRAGMA wal_checkpoint(PASSIVE)")
                finally:
                    cursor.close()
            except Exception:
                # Ignore checkpoint errors - they're non-critical
                pass

    return engine


def install_query_hooks(engine: AsyncEngine) -> None:
    """Install lightweight query counting hooks on the engine (idempotent)."""
    global _QUERY_HOOKS_INSTALLED
    if _QUERY_HOOKS_INSTALLED:
        return
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def before_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        tracker = _QUERY_TRACKER.get()
        if tracker is None:
            return
        timings = conn.info.setdefault("query_start_time", [])
        timings.append(time.perf_counter())

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def after_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        tracker = _QUERY_TRACKER.get()
        if tracker is None:
            return
        timings = conn.info.get("query_start_time")
        if not timings:
            return
        start_time = timings.pop()
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        tracker.record(statement, duration_ms)

    _QUERY_HOOKS_INSTALLED = True


class UnsupportedDatabaseBackendError(RuntimeError):
    """Raised at startup when DATABASE_URL points at an unsupported backend.

    As of this version, mcp-agent-mail only supports SQLite as the backing
    store. Large portions of the application (FTS5 full-text search, many
    SQLite-specific PRAGMAs, ``ALTER TABLE ADD COLUMN`` idempotency) assume
    SQLite. PostgreSQL / MySQL / etc. will silently mis-behave or fail at
    schema init with cryptic errors (see issue #142 for the historical
    ``CREATE VIRTUAL TABLE`` failure).

    We fail fast with a clear, actionable error instead.
    """


_SUPPORTED_BACKENDS: Final = frozenset({"sqlite"})


def _assert_supported_backend(database_url: str) -> None:
    """Reject DATABASE_URLs that target backends we don't actually support.

    Accepts empty / unparseable URLs silently — those will produce their own
    errors downstream in :func:`_build_engine`. Only raises for URLs that
    parse cleanly but point at a known non-SQLite backend (e.g.
    ``postgresql+asyncpg://...``).
    """
    if not database_url:
        return
    try:
        from sqlalchemy.engine import make_url

        backend = make_url(database_url).get_backend_name().lower()
    except Exception:
        return
    if backend in _SUPPORTED_BACKENDS:
        return
    raise UnsupportedDatabaseBackendError(
        "DATABASE_URL points at an unsupported backend "
        f"({backend!r}). mcp-agent-mail currently only supports SQLite "
        "(e.g. 'sqlite+aiosqlite:////data/mailbox/storage.sqlite3'). "
        "PostgreSQL / MySQL / etc. are not yet implemented — core features "
        "(full-text search via FTS5, schema migrations, PRAGMA-based tuning) "
        "assume SQLite. Track support in "
        "https://github.com/Dicklesworthstone/mcp_agent_mail/issues/142."
    )


def init_engine(settings: Settings | None = None) -> None:
    """Initialise global engine and session factory once."""
    global _engine, _session_factory
    if _engine is not None and _session_factory is not None:
        return
    resolved_settings = settings or get_settings()
    _assert_supported_backend(resolved_settings.database.url)
    engine = _build_engine(resolved_settings.database)
    install_query_hooks(engine)
    _engine = engine
    _session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


async def await_database_cleanup_task(task: asyncio.Task[T]) -> T:
    """Finish one database cleanup task, then propagate caller cancellation."""
    cancellation: asyncio.CancelledError | None = None
    current_task = asyncio.current_task()
    while not task.done():
        try:
            # asyncio.wait does not cancel its member task when this waiter is
            # cancelled, and it does not surface the member's own exception.
            # That lets cleanup finish without Python 3.14's cancelled-shield
            # wrapper reporting "exception in shielded future".
            await asyncio.wait((task,))
        except asyncio.CancelledError as exc:
            if current_task is None or current_task.cancelling() == 0:
                raise
            if cancellation is None:
                cancellation = exc
            while current_task.cancelling():
                current_task.uncancel()
    if cancellation is not None:
        # Observe any cleanup failure, but caller cancellation is authoritative.
        # Otherwise task.result() can raise first and silently consume the
        # cancellation that this helper deliberately drained and preserved.
        with suppress(BaseException):
            task.result()
        raise cancellation
    return task.result()


async def _close_session(session: AsyncSession) -> None:
    """Close one session completely while preserving caller cancellation."""
    await await_database_cleanup_task(asyncio.create_task(session.close()))


@asynccontextmanager
async def get_session(*, check_circuit_breaker: bool = False) -> AsyncIterator[AsyncSession]:
    """Provide an async database session with guaranteed cleanup.

    This context manager ensures the session is always closed, even under task
    cancellation. Uses asyncio.shield() to prevent cancellation from interrupting
    the close operation.

    Args:
        check_circuit_breaker: If True, check circuit breaker state before yielding session.
            Raises CircuitBreakerOpenError if circuit is open. Default False for backwards
            compatibility - most callers use retry_on_db_lock which handles this.

    Note: We do NOT call session.rollback() here because that would expire all
    loaded objects, causing DetachedInstanceError when code tries to access
    attributes after the session closes. The pool_reset_on_return='rollback'
    setting handles uncommitted transactions at the pool level instead.
    """
    if check_circuit_breaker:
        state = get_circuit_state()
        if state == CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                "Circuit breaker is open for database operations. "
                "This typically indicates sustained database lock contention."
            )

    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        await _close_session(session)


@asynccontextmanager
async def get_immediate_session(*, check_circuit_breaker: bool = False) -> AsyncIterator[AsyncSession]:
    """Provide an async database session that begins with ``BEGIN IMMEDIATE``.

    This forces SQLite to acquire a *reserved lock* at the start of the
    transaction, which in WAL mode guarantees that all subsequent reads see
    the latest committed state (a fresh WAL snapshot).  Without this, a
    pooled connection may re-use a stale read snapshot, causing:

    - Phantom conflicts after a release (#130 / Rust Bug #85)
    - Missed conflicts before an insert (#129 / Rust Bug #86)

    The session is otherwise identical to :func:`get_session` — callers
    should ``await session.commit()`` on the success path; the finally
    block rolls back any uncommitted changes and closes the session.

    **Only use this for reservation operations** (or other paths that
    require serialised read-then-write consistency).  Regular reads should
    continue using :func:`get_session` to avoid unnecessary write-lock
    contention.
    """
    if check_circuit_breaker:
        state = get_circuit_state()
        if state == CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                "Circuit breaker is open for database operations. "
                "This typically indicates sustained database lock contention."
            )

    factory = get_session_factory()
    session = factory()
    try:
        # Obtain the underlying connection and issue BEGIN IMMEDIATE *before*
        # SQLAlchemy's autobegin can issue a plain BEGIN.
        conn = await session.connection()
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        yield session
    except BaseException:
        # Roll back on any error so the IMMEDIATE lock is released.
        with suppress(BaseException):
            await session.rollback()
        raise
    finally:
        await _close_session(session)


def get_db_health_status() -> dict[str, Any]:
    """Return database health status including circuit breaker state and pool info.

    Returns:
        Dict with circuit_state, pool stats (if available), and recommendations.
    """
    state = get_circuit_state()
    status: dict[str, Any] = {
        "circuit_state": state.value,
        "circuit_failures": _circuit_breaker_failures,
    }

    if _engine is not None:
        pool = cast(Any, _engine.pool)
        # Pool attributes are available at runtime but not in type stubs
        status["pool"] = {
            "size": pool.size(),
            "checked_in": pool.checkedin(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
        }

    if state == CircuitState.OPEN:
        status["recommendation"] = (
            "Circuit breaker is OPEN. Database is experiencing sustained lock contention. "
            "Consider: (1) reducing concurrent operations, (2) increasing busy_timeout, "
            "(3) checking for long-running transactions, (4) running PRAGMA wal_checkpoint(TRUNCATE)."
        )
    elif state == CircuitState.HALF_OPEN:
        status["recommendation"] = (
            "Circuit breaker is HALF_OPEN. Testing if database has recovered. "
            "Next successful operation will reset the circuit."
        )

    return status


_RELEASED_MESSAGE_DELIVERY_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "state",
        "project_id",
        "project_slug_snapshot",
        "project_generation_snapshot",
        "sender_id",
        "sender_name_snapshot",
        "sender_generation_snapshot",
        "actor_kind",
        "actor_agent_id",
        "actor_ui_user_id",
        "actor_name_snapshot",
        "actor_generation_snapshot",
        "actor_session_epoch_snapshot",
        "idempotency_scope",
        "idempotency_key",
        "request_sha256",
        "thread_id",
        "reply_to_message_id",
        "topic",
        "subject",
        "body_md",
        "importance",
        "ack_required",
        "attachments",
        "archive_document",
        "archive_document_sha256",
        "created_ts",
        "lease_token",
        "lease_fence",
        "lease_expires_ts",
        "attempt_count",
        "next_attempt_ts",
        "last_attempt_ts",
        "last_error",
        "archive_commit_sha",
        "archive_receipt_path",
        "receipt_sha256",
        "published_message_id",
        "published_ts",
        "quarantined_ts",
        "quarantine_reason",
    }
)
_RELEASED_MESSAGE_DELIVERY_RECIPIENT_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "delivery_id",
        "position",
        "kind",
        "agent_id",
        "agent_name_snapshot",
        "agent_generation_snapshot",
    }
)


def _sqlite_existing_table_columns(
    connection: Any,
    table_name: str,
) -> frozenset[str] | None:
    table_row = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if table_row is None:
        return None
    return frozenset(
        str(row[1])
        for row in connection.exec_driver_sql(
            f'PRAGMA table_info("{table_name}")'
        ).fetchall()
    )


def _message_delivery_schema_needs_rebuild(connection: Any) -> bool:
    """Recognize only the released delivery schema or the current schema.

    An unknown intermediate shape is not safe to guess at: delivery rows are
    immutable audit data, so startup must stop without changing the database
    instead of silently dropping an unrecognized column.
    """
    delivery_columns = _sqlite_existing_table_columns(
        connection,
        "message_deliveries",
    )
    recipient_columns = _sqlite_existing_table_columns(
        connection,
        "message_delivery_recipients",
    )
    if delivery_columns is None or recipient_columns is None:
        return False

    delivery_table = cast(Table, getattr(MessageDelivery, "__table__"))  # noqa: B009
    recipient_table = cast(
        Table,
        getattr(MessageDeliveryRecipient, "__table__"),  # noqa: B009
    )
    canonical_delivery_columns = frozenset(
        column.name for column in delivery_table.columns
    )
    canonical_recipient_columns = frozenset(
        column.name for column in recipient_table.columns
    )
    if (
        delivery_columns == canonical_delivery_columns
        and recipient_columns == canonical_recipient_columns
    ):
        return False
    if (
        delivery_columns == _RELEASED_MESSAGE_DELIVERY_COLUMNS
        and recipient_columns == _RELEASED_MESSAGE_DELIVERY_RECIPIENT_COLUMNS
    ):
        return True

    delivery_missing = sorted(canonical_delivery_columns - delivery_columns)
    delivery_unexpected = sorted(delivery_columns - canonical_delivery_columns)
    recipient_missing = sorted(canonical_recipient_columns - recipient_columns)
    recipient_unexpected = sorted(recipient_columns - canonical_recipient_columns)
    raise RuntimeError(
        "MessageDelivery schema migration found an unrecognized table shape; "
        f"delivery missing={delivery_missing}, unexpected={delivery_unexpected}; "
        f"recipients missing={recipient_missing}, unexpected={recipient_unexpected}"
    )


_RELEASED_MESSAGE_DELIVERY_PROJECTION_SQL: Final[str] = """
    SELECT
        legacy.id,
        legacy.state,
        CASE
            WHEN legacy.reply_to_message_id IS NOT NULL THEN 'reply'
            ELSE 'message'
        END,
        legacy.project_id,
        legacy.project_slug_snapshot,
        legacy.project_generation_snapshot,
        sender_project.id,
        CASE
            WHEN sender_project.id = legacy.project_id
                THEN legacy.project_slug_snapshot
            ELSE sender_project.slug
        END,
        CASE
            WHEN sender_project.id = legacy.project_id
                THEN legacy.project_generation_snapshot
            ELSE sender_project.project_generation
        END,
        legacy.sender_id,
        legacy.sender_name_snapshot,
        legacy.sender_generation_snapshot,
        legacy.actor_kind,
        CASE legacy.actor_kind
            WHEN 'agent' THEN legacy.actor_agent_id
            WHEN 'ui_user' THEN legacy.actor_ui_user_id
            ELSE 0
        END,
        legacy.actor_name_snapshot,
        sender_project.id,
        CASE
            WHEN sender_project.id = legacy.project_id
                THEN legacy.project_slug_snapshot
            ELSE sender_project.slug
        END,
        CASE
            WHEN sender_project.id = legacy.project_id
                THEN legacy.project_generation_snapshot
            ELSE sender_project.project_generation
        END,
        legacy.actor_generation_snapshot,
        legacy.actor_session_epoch_snapshot,
        legacy.idempotency_key,
        legacy.request_sha256,
        legacy.thread_id,
        legacy.reply_to_message_id,
        legacy.topic,
        legacy.subject,
        legacy.body_md,
        legacy.importance,
        legacy.ack_required,
        legacy.attachments,
        legacy.archive_document,
        legacy.archive_document_sha256,
        legacy.created_ts,
        legacy.lease_token,
        legacy.lease_fence,
        legacy.lease_expires_ts,
        legacy.attempt_count,
        0,
        legacy.next_attempt_ts,
        legacy.last_attempt_ts,
        legacy.last_error,
        legacy.archive_receipt_path,
        legacy.receipt_sha256,
        legacy.archive_commit_sha,
        legacy.published_message_id,
        legacy.published_ts,
        legacy.quarantined_ts,
        legacy.quarantine_reason
    FROM message_deliveries_schema_v1 AS legacy
    JOIN agents AS sender ON sender.id = legacy.sender_id
    JOIN projects AS sender_project ON sender_project.id = sender.project_id
"""

_CANONICAL_MESSAGE_DELIVERY_COLUMN_NAMES: Final[tuple[str, ...]] = (
    "id",
    "state",
    "delivery_kind",
    "project_id",
    "project_slug_snapshot",
    "project_generation_snapshot",
    "sender_project_id_snapshot",
    "sender_project_slug_snapshot",
    "sender_project_generation_snapshot",
    "sender_id",
    "sender_name_snapshot",
    "sender_generation_snapshot",
    "actor_kind",
    "actor_id",
    "actor_name_snapshot",
    "actor_project_id_snapshot",
    "actor_project_slug_snapshot",
    "actor_project_generation_snapshot",
    "actor_generation_snapshot",
    "actor_epoch_snapshot",
    "idempotency_key",
    "request_sha256",
    "thread_id",
    "reply_to_message_id",
    "topic",
    "subject",
    "body_md",
    "importance",
    "ack_required",
    "attachments",
    "archive_document",
    "document_sha256",
    "created_ts",
    "lease_token",
    "lease_fence",
    "lease_expires_ts",
    "attempt_count",
    "backoff_seconds",
    "next_attempt_ts",
    "last_attempt_ts",
    "last_error",
    "archive_relative_path",
    "archive_blob_sha",
    "archive_commit_sha",
    "message_id",
    "published_ts",
    "quarantined_ts",
    "quarantine_reason",
)

_CANONICAL_MESSAGE_DELIVERY_RECIPIENT_COLUMN_NAMES: Final[tuple[str, ...]] = (
    "delivery_id",
    "ordinal",
    "kind",
    "agent_id",
    "agent_name_snapshot",
    "agent_generation_snapshot",
    "project_id_snapshot",
)


def _quoted_column_list(column_names: tuple[str, ...]) -> str:
    return ", ".join(f'"{name}"' for name in column_names)


def _rebuild_message_delivery_schema(connection: Any) -> None:
    """Rebuild both released delivery tables as the canonical immutable ledger."""
    if not _message_delivery_schema_needs_rebuild(connection):
        return

    delivery_table = cast(Table, getattr(MessageDelivery, "__table__"))  # noqa: B009
    recipient_table = cast(
        Table,
        getattr(MessageDeliveryRecipient, "__table__"),  # noqa: B009
    )
    delivery_columns = _quoted_column_list(
        _CANONICAL_MESSAGE_DELIVERY_COLUMN_NAMES
    )
    recipient_columns = _quoted_column_list(
        _CANONICAL_MESSAGE_DELIVERY_RECIPIENT_COLUMN_NAMES
    )

    connection.exec_driver_sql(
        "ALTER TABLE message_delivery_recipients "
        "RENAME TO message_delivery_recipients_schema_v1"
    )
    connection.exec_driver_sql(
        "ALTER TABLE message_deliveries RENAME TO message_deliveries_schema_v1"
    )
    connection.execute(CreateTable(delivery_table))
    connection.execute(CreateTable(recipient_table))

    connection.exec_driver_sql(
        f"INSERT INTO message_deliveries ({delivery_columns}) "
        f"{_RELEASED_MESSAGE_DELIVERY_PROJECTION_SQL}"
    )
    recipient_projection_sql = """
        SELECT
            legacy.delivery_id,
            legacy.position,
            legacy.kind,
            legacy.agent_id,
            legacy.agent_name_snapshot,
            legacy.agent_generation_snapshot,
            delivery.project_id
        FROM message_delivery_recipients_schema_v1 AS legacy
        JOIN message_deliveries_schema_v1 AS delivery
          ON delivery.id = legacy.delivery_id
    """
    connection.exec_driver_sql(
        f"INSERT INTO message_delivery_recipients ({recipient_columns}) "
        f"{recipient_projection_sql}"
    )

    source_delivery_count = int(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM message_deliveries_schema_v1"
        ).scalar_one()
    )
    target_delivery_count = int(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM message_deliveries"
        ).scalar_one()
    )
    source_recipient_count = int(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM message_delivery_recipients_schema_v1"
        ).scalar_one()
    )
    target_recipient_count = int(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM message_delivery_recipients"
        ).scalar_one()
    )
    delivery_difference = connection.exec_driver_sql(
        f"SELECT 1 FROM ({_RELEASED_MESSAGE_DELIVERY_PROJECTION_SQL} "
        f"EXCEPT SELECT {delivery_columns} FROM message_deliveries) LIMIT 1"
    ).fetchone()
    reverse_delivery_difference = connection.exec_driver_sql(
        f"SELECT 1 FROM (SELECT {delivery_columns} FROM message_deliveries "
        f"EXCEPT {_RELEASED_MESSAGE_DELIVERY_PROJECTION_SQL}) LIMIT 1"
    ).fetchone()
    recipient_difference = connection.exec_driver_sql(
        f"SELECT 1 FROM ({recipient_projection_sql} "
        f"EXCEPT SELECT {recipient_columns} "
        "FROM message_delivery_recipients) LIMIT 1"
    ).fetchone()
    reverse_recipient_difference = connection.exec_driver_sql(
        f"SELECT 1 FROM (SELECT {recipient_columns} "
        "FROM message_delivery_recipients "
        f"EXCEPT {recipient_projection_sql}) LIMIT 1"
    ).fetchone()
    if (
        source_delivery_count != target_delivery_count
        or source_recipient_count != target_recipient_count
        or delivery_difference is not None
        or reverse_delivery_difference is not None
        or recipient_difference is not None
        or reverse_recipient_difference is not None
    ):
        raise RuntimeError(
            "MessageDelivery schema migration did not preserve every immutable row"
        )

    connection.exec_driver_sql("DROP TABLE message_delivery_recipients_schema_v1")
    connection.exec_driver_sql("DROP TABLE message_deliveries_schema_v1")
    for table in (delivery_table, recipient_table):
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            connection.execute(CreateIndex(index))

    canonical_delivery_columns = _sqlite_existing_table_columns(
        connection,
        "message_deliveries",
    )
    canonical_recipient_columns = _sqlite_existing_table_columns(
        connection,
        "message_delivery_recipients",
    )
    if canonical_delivery_columns != frozenset(
        _CANONICAL_MESSAGE_DELIVERY_COLUMN_NAMES
    ) or canonical_recipient_columns != frozenset(
        _CANONICAL_MESSAGE_DELIVERY_RECIPIENT_COLUMN_NAMES
    ):
        raise RuntimeError(
            "MessageDelivery schema migration did not install the canonical columns"
        )

    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            "MessageDelivery schema migration failed foreign-key validation"
        )


async def _migrate_message_delivery_schema(engine: AsyncEngine) -> None:
    """Atomically upgrade the released SQLite delivery ledger in place."""
    async with engine.connect() as connection:
        if connection.dialect.name != "sqlite":
            return
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await connection.commit()
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await connection.run_sync(_rebuild_message_delivery_schema)
            await connection.commit()
        except BaseException:
            with suppress(BaseException):
                await connection.rollback()
            raise
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            foreign_keys_enabled = int(
                (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
            )
            if foreign_keys_enabled != 1:
                raise RuntimeError("SQLite foreign-key enforcement was not restored")
            await connection.commit()


def _ui_users_locale_schema_needs_rebuild(connection: Any) -> bool:
    """Return whether an existing UI-account table still has the two-locale schema."""
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ui_users'"
    ).fetchone()
    if row is None:
        return False
    create_sql = re.sub(r"\s+", " ", str(row[0])).casefold()
    return (
        "preferred_ui_locale varchar(16)" not in create_sql
        or "preferred_correspondence_locale varchar(16)" not in create_sql
        or any(repr(value).casefold() not in create_sql for value in MAIL_UI_LOCALE_VALUES)
    )


def _rebuild_ui_users_locale_schema(connection: Any) -> None:
    """Rebuild ``ui_users`` with the canonical locale CHECK inside one transaction."""
    if not _ui_users_locale_schema_needs_rebuild(connection):
        return

    dependent_schema_objects = connection.exec_driver_sql(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE sql IS NOT NULL
          AND (
              (type = 'index' AND tbl_name = 'ui_users')
              OR (
                  type IN ('trigger', 'view')
                  AND (tbl_name = 'ui_users' OR instr(lower(sql), 'ui_users') > 0)
              )
          )
        ORDER BY type, name
        """
    ).fetchall()
    ui_users_table = cast(Table, getattr(UiUser, "__table__"))  # noqa: B009
    existing_columns = {
        str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(ui_users)")
    }
    expected_columns = [column.name for column in ui_users_table.columns]
    missing_columns = set(expected_columns) - existing_columns
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise RuntimeError(f"ui_users locale migration is missing columns: {missing}")

    temporary_metadata = MetaData()
    temporary_table = ui_users_table.to_metadata(
        temporary_metadata,
        name="ui_users_locale_v2",
    )
    connection.execute(CreateTable(temporary_table))
    quoted_columns = ", ".join(f'"{name}"' for name in expected_columns)
    connection.exec_driver_sql(
        f'INSERT INTO "ui_users_locale_v2" ({quoted_columns}) '
        f'SELECT {quoted_columns} FROM "ui_users"'
    )

    source_count = int(
        connection.exec_driver_sql("SELECT COUNT(*) FROM ui_users").scalar_one()
    )
    target_count = int(
        connection.exec_driver_sql("SELECT COUNT(*) FROM ui_users_locale_v2").scalar_one()
    )
    forward_difference = connection.exec_driver_sql(
        f"SELECT 1 FROM (SELECT {quoted_columns} FROM ui_users "
        f"EXCEPT SELECT {quoted_columns} FROM ui_users_locale_v2) LIMIT 1"
    ).fetchone()
    reverse_difference = connection.exec_driver_sql(
        f"SELECT 1 FROM (SELECT {quoted_columns} FROM ui_users_locale_v2 "
        f"EXCEPT SELECT {quoted_columns} FROM ui_users) LIMIT 1"
    ).fetchone()
    if (
        source_count != target_count
        or forward_difference is not None
        or reverse_difference is not None
    ):
        raise RuntimeError("ui_users locale migration did not preserve every account row")

    identifier_preparer = connection.dialect.identifier_preparer
    for object_type, name, _sql in dependent_schema_objects:
        if object_type not in {"trigger", "view"}:
            continue
        quoted_name = identifier_preparer.quote(str(name))
        connection.exec_driver_sql(f"DROP {str(object_type).upper()} {quoted_name}")

    connection.exec_driver_sql("DROP TABLE ui_users")
    connection.exec_driver_sql("ALTER TABLE ui_users_locale_v2 RENAME TO ui_users")

    replaced_locale_triggers = {"ui_users_locale_guard_bi", "ui_users_locale_guard_bu"}
    for object_type in ("view", "index", "trigger"):
        for stored_type, name, sql in dependent_schema_objects:
            if stored_type != object_type or name in replaced_locale_triggers:
                continue
            connection.exec_driver_sql(str(sql))

    existing_indexes = {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'ui_users'"
        )
    }
    for index in sorted(ui_users_table.indexes, key=lambda item: item.name or ""):
        if index.name not in existing_indexes:
            connection.execute(CreateIndex(index))

    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError("ui_users locale migration failed foreign-key validation")


async def _migrate_ui_users_locale_schema(engine: AsyncEngine) -> None:
    """Run the SQLite table rebuild with FK enforcement paused only for the transaction."""
    async with engine.connect() as connection:
        if connection.dialect.name != "sqlite":
            return
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await connection.commit()
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await connection.run_sync(_rebuild_ui_users_locale_schema)
            await connection.commit()
        except BaseException:
            with suppress(BaseException):
                await connection.rollback()
            raise
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            foreign_keys_enabled = int(
                (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
            )
            if foreign_keys_enabled != 1:
                raise RuntimeError("SQLite foreign-key enforcement was not restored")
            await connection.commit()


_BASELINE_REVISION: Final[str] = "0001baseline"


def _alembic_config(connection: Any) -> AlembicConfig:
    """Build an Alembic config bound to an already-open connection.

    The connection is handed through ``attributes`` rather than letting Alembic
    open its own: migrations must run on the same engine, inside the same
    transaction, as the schema work that precedes them.
    """
    config = AlembicConfig()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parent / "migrations")
    )
    config.attributes["connection"] = connection
    return config


def _align_alembic_version(connection: Any, *, was_fresh: bool) -> None:
    """Bring ``alembic_version`` in line with what ``ensure_schema`` just built.

    The two cases are not interchangeable and getting them the wrong way round
    is the classic failure of introducing Alembic into a live project:

    * **fresh database** -- ``create_all`` built it from *current* models, so it
      already contains every column any existing revision would add. It is
      stamped at **head**; replaying revisions would fail on columns that are
      already there.
    * **pre-existing database** -- it predates Alembic and matches the baseline,
      so it is stamped at the **baseline** and every later revision applies to
      it in order.

    Once stamped, both run ``upgrade head``, which is a no-op for the fresh case
    and the actual migration path for the old one.
    """
    context = MigrationContext.configure(connection)
    already_tracked = context.get_current_revision() is not None
    config = _alembic_config(connection)
    if not already_tracked:
        alembic_command.stamp(config, "head" if was_fresh else _BASELINE_REVISION)
    alembic_command.upgrade(config, "head")


@retry_on_db_lock(max_retries=7, base_delay=0.1, max_delay=8.0, use_circuit_breaker=False)
async def ensure_schema(settings: Settings | None = None) -> None:
    """Ensure database schema exists (creates tables from SQLModel definitions).

    This is the pure SQLModel approach:
    - Models define the schema
    - create_all() creates tables that don't exist yet
    - For schema changes: delete the DB and regenerate (dev) or use Alembic (prod)

    Also enables SQLite WAL mode for better concurrent access.

    Note: Circuit breaker is disabled for schema operations since they're
    typically run at startup before the circuit breaker should be active.
    """
    global _schema_ready, _schema_lock
    if _schema_ready:
        return
    if _schema_lock is None:
        _schema_lock = asyncio.Lock()
    async with _schema_lock:
        if _schema_ready:
            return
        init_engine(settings)
        engine = get_engine()
        async with engine.begin() as conn:
            # Observed BEFORE create_all, because afterwards every database
            # looks alike. It decides where Alembic is stamped at the end, and
            # that decision cannot be recovered later.
            was_fresh = not await conn.run_sync(
                lambda sync_conn: sa_inspect(sync_conn).has_table("agents")
            )
            # Pure SQLModel: create tables from metadata
            # (WAL mode is set automatically via event listener in _build_engine)
            await conn.run_sync(SQLModel.metadata.create_all)
        # The released delivery ledger predates the canonical actor/project
        # snapshots. Its idempotency index references columns that no longer
        # exist, so rebuild both immutable tables before any index/trigger DDL.
        await _migrate_message_delivery_schema(engine)
        async with engine.begin() as conn:
            # Additive migrations and backfills must run before SQLite can
            # rebuild an intermediate AgentExecution table with physical
            # NOT NULL/CHECK/FK guarantees.
            await conn.run_sync(
                lambda sync_conn: _setup_fts(
                    sync_conn,
                    validate_execution_schema=False,
                )
            )
        await _migrate_agent_executions_schema(engine)
        # SQLite cannot widen a table-level CHECK in place. Rebuild the one
        # account table atomically after additive legacy migrations have made
        # every current column available, then recreate its dropped triggers.
        await _migrate_ui_users_locale_schema(engine)
        async with engine.begin() as conn:
            await conn.run_sync(_setup_fts)
        # Last, so that a revision can rely on everything above having run.
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: _align_alembic_version(sync_conn, was_fresh=was_fresh)
            )
        _schema_ready = True


def reset_database_state() -> None:
    """Test helper to reset global engine/session state."""
    global _engine, _session_factory, _schema_ready, _schema_lock, _QUERY_HOOKS_INSTALLED
    # Dispose any existing engine/pool first to avoid leaking file descriptors across tests.
    if _engine is not None:
        engine = _engine
        try:
            dispose_engine_blocking(engine)
        except Exception:
            # Last resort: sync pool disposal.
            with suppress(Exception):
                engine.sync_engine.dispose()
    _engine = None
    _session_factory = None
    _schema_ready = False
    _schema_lock = None
    # Query hooks bind to the disposed engine; reset the one-shot flag so the
    # rebuilt engine gets re-instrumented on the next init_engine() call.
    _QUERY_HOOKS_INSTALLED = False
    # Tests frequently mutate env vars; keep settings cache in sync with DB resets.
    clear_settings_cache()


def dispose_engine_blocking(engine: AsyncEngine, timeout_seconds: float = 5.0) -> None:
    """Dispose an async engine in a helper thread so shutdown survives active event loops/cancellation."""
    # The helper thread hands its failure back through this shared cell;
    # Thread.join() below establishes the happens-before for the read.
    dispose_errors: list[BaseException] = []

    def _dispose_in_thread() -> None:
        try:
            asyncio.run(engine.dispose())
        except BaseException as exc:  # pragma: no cover - best-effort fallback path
            dispose_errors.append(exc)

    dispose_thread = threading.Thread(target=_dispose_in_thread, name="db-dispose", daemon=True)
    dispose_thread.start()
    dispose_thread.join(timeout=timeout_seconds)
    if dispose_thread.is_alive():
        raise TimeoutError("Timed out waiting for async engine disposal in helper thread.")
    if dispose_errors:
        raise dispose_errors[0]


def _is_sqlite_connection(connection: Any) -> bool:
    """Best-effort check that a SQLAlchemy sync Connection is backed by SQLite.

    Used to gate SQLite-only DDL (FTS5 virtual tables, SQLite-idiom ALTERs)
    so the schema initializer still works for other backends that may be
    added in the future. As of this version, non-SQLite backends are not
    supported at runtime (see ``_assert_supported_backend``).
    """
    try:
        dialect = getattr(getattr(connection, "engine", None), "dialect", None)
        name = getattr(dialect, "name", "") or ""
        return name.lower() == "sqlite"
    except Exception:
        return False


_AGENT_EXECUTION_INDEX_SQL: Final[dict[str, str]] = {
    "idx_agent_executions_active": (
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_active "
        "ON agent_executions (project_id, agent_id, last_active_ts) "
        "WHERE status = 'active'"
    ),
    "idx_agent_executions_active_stale": (
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_active_stale "
        "ON agent_executions (last_active_ts, project_id, id) "
        "WHERE status = 'active'"
    ),
    "idx_agent_executions_project_active_stale": (
        "CREATE INDEX IF NOT EXISTS idx_agent_executions_project_active_stale "
        "ON agent_executions (project_id, last_active_ts, id) "
        "WHERE status = 'active'"
    ),
    "idx_build_slot_artifact_projections_pending": (
        "CREATE INDEX IF NOT EXISTS "
        "idx_build_slot_artifact_projections_pending "
        "ON build_slot_artifact_projections (project_id, execution_id) "
        "WHERE reconciled_ts IS NULL"
    ),
    "idx_build_slot_artifact_paths_project_execution": (
        "CREATE INDEX IF NOT EXISTS "
        "idx_build_slot_artifact_paths_project_execution "
        "ON build_slot_artifact_paths "
        "(project_id, execution_id, slot_path_component)"
    ),
    "uq_agent_executions_session_external": (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_executions_session_external "
        "ON agent_executions (agent_id, client_name, external_id) "
        "WHERE kind = 'session'"
    ),
    "uq_agent_executions_subagent_external": (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_executions_subagent_external "
        "ON agent_executions (parent_execution_id, client_name, external_id) "
        "WHERE kind = 'subagent'"
    ),
    "idx_file_reservations_execution": (
        "CREATE INDEX IF NOT EXISTS idx_file_reservations_execution "
        "ON file_reservations (execution_id)"
    ),
    "idx_file_reservations_archive_pending": (
        "CREATE INDEX IF NOT EXISTS idx_file_reservations_archive_pending "
        "ON file_reservations (project_id, id) "
        "WHERE archive_synced_revision < archive_revision"
    ),
    "uq_agent_executions_token_hash": (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_executions_token_hash "
        "ON agent_executions (execution_token_hash)"
    ),
}


_AGENT_EXECUTION_TRIGGER_SQL: Final[dict[str, str]] = {
    "file_reservations_origin_guard_bi": """
        CREATE TRIGGER IF NOT EXISTS file_reservations_origin_guard_bi
        BEFORE INSERT ON file_reservations
        BEGIN
            SELECT RAISE(ABORT, 'invalid reservation origin')
            WHERE new.origin IS NULL
               OR new.origin NOT IN ('auto', 'explicit');
        END
    """,
    "file_reservations_origin_guard_bu": """
        CREATE TRIGGER IF NOT EXISTS file_reservations_origin_guard_bu
        BEFORE UPDATE OF origin ON file_reservations
        BEGIN
            SELECT RAISE(ABORT, 'invalid reservation origin')
            WHERE new.origin IS NULL
               OR new.origin NOT IN ('auto', 'explicit');
            SELECT RAISE(ABORT, 'reservation origin cannot downgrade to auto')
            WHERE old.origin = 'explicit' AND new.origin = 'auto';
        END
    """,
    "file_reservations_archive_version_guard_bi": """
        CREATE TRIGGER IF NOT EXISTS file_reservations_archive_version_guard_bi
        BEFORE INSERT ON file_reservations
        BEGIN
            SELECT RAISE(ABORT, 'invalid reservation archive version')
            WHERE new.archive_revision IS NULL
               OR new.archive_revision < 1
               OR new.archive_synced_revision IS NULL
               OR new.archive_synced_revision < 0
               OR new.archive_synced_revision > new.archive_revision;
        END
    """,
    "file_reservations_archive_version_guard_bu": """
        CREATE TRIGGER IF NOT EXISTS file_reservations_archive_version_guard_bu
        BEFORE UPDATE ON file_reservations
        BEGIN
            SELECT RAISE(ABORT, 'invalid reservation archive version')
            WHERE new.archive_revision IS NULL
               OR new.archive_revision < old.archive_revision
               OR new.archive_revision > old.archive_revision + 1
               OR new.archive_synced_revision IS NULL
               OR new.archive_synced_revision < old.archive_synced_revision
               OR new.archive_synced_revision < 0
               OR new.archive_synced_revision > new.archive_revision;
            SELECT RAISE(ABORT, 'reservation archive version is storage-managed')
            WHERE (
                    old.project_id IS NOT new.project_id
                    OR old.agent_id IS NOT new.agent_id
                    OR old.execution_id IS NOT new.execution_id
                    OR old.origin IS NOT new.origin
                    OR old.path_pattern IS NOT new.path_pattern
                    OR old.exclusive IS NOT new.exclusive
                    OR old.reason IS NOT new.reason
                    OR old.created_ts IS NOT new.created_ts
                    OR old.expires_ts IS NOT new.expires_ts
                    OR old.released_ts IS NOT new.released_ts
                  )
              AND (
                    new.archive_revision IS NOT old.archive_revision
                    OR new.archive_synced_revision IS NOT old.archive_synced_revision
                  );
            SELECT RAISE(ABORT, 'reservation archive version is storage-managed')
            WHERE old.project_id IS new.project_id
              AND old.agent_id IS new.agent_id
              AND old.execution_id IS new.execution_id
              AND old.origin IS new.origin
              AND old.path_pattern IS new.path_pattern
              AND old.exclusive IS new.exclusive
              AND old.reason IS new.reason
              AND old.created_ts IS new.created_ts
              AND old.expires_ts IS new.expires_ts
              AND old.released_ts IS new.released_ts
              AND new.archive_revision = old.archive_revision + 1
              AND new.archive_synced_revision IS NOT old.archive_synced_revision;
        END
    """,
    "file_reservations_archive_revision_au": """
        CREATE TRIGGER IF NOT EXISTS file_reservations_archive_revision_au
        AFTER UPDATE OF project_id, agent_id, execution_id, origin, path_pattern,
            exclusive, reason, created_ts, expires_ts, released_ts
        ON file_reservations
        WHEN old.project_id IS NOT new.project_id
          OR old.agent_id IS NOT new.agent_id
          OR old.execution_id IS NOT new.execution_id
          OR old.origin IS NOT new.origin
          OR old.path_pattern IS NOT new.path_pattern
          OR old.exclusive IS NOT new.exclusive
          OR old.reason IS NOT new.reason
          OR old.created_ts IS NOT new.created_ts
          OR old.expires_ts IS NOT new.expires_ts
          OR old.released_ts IS NOT new.released_ts
        BEGIN
            UPDATE file_reservations
            SET archive_revision = old.archive_revision + 1
            WHERE id = new.id;
        END
    """,
    "agent_executions_project_agent_guard_bi": """
        CREATE TRIGGER IF NOT EXISTS agent_executions_project_agent_guard_bi
        BEFORE INSERT ON agent_executions
        BEGIN
            SELECT RAISE(ABORT, 'agent execution owner is missing, mismatched, or retired')
            WHERE NOT EXISTS (
                SELECT 1
                FROM agents
                WHERE id IS new.agent_id
                  AND project_id IS new.project_id
                  AND retired_at IS NULL
            );
        END
    """,
    "agent_executions_capability_guard_bi": """
        CREATE TRIGGER IF NOT EXISTS agent_executions_capability_guard_bi
        BEFORE INSERT ON agent_executions
        BEGIN
            SELECT RAISE(ABORT, 'invalid agent execution capability')
            WHERE new.execution_token_hash IS NULL
               OR length(new.execution_token_hash) != 64
               OR lower(new.execution_token_hash) IS NOT new.execution_token_hash
               OR new.execution_token_hash GLOB '*[^0-9a-f]*'
               OR new.lifecycle_protocol_version IS NULL
               OR new.lifecycle_protocol_version < 0;
        END
    """,
    "agent_executions_capability_guard_bu": """
        CREATE TRIGGER IF NOT EXISTS agent_executions_capability_guard_bu
        BEFORE UPDATE OF execution_token_hash, lifecycle_protocol_version
        ON agent_executions
        BEGIN
            SELECT RAISE(ABORT, 'agent execution capability is immutable')
            WHERE new.execution_token_hash IS NOT old.execution_token_hash;
            SELECT RAISE(ABORT, 'invalid agent execution capability')
            WHERE new.execution_token_hash IS NULL
               OR length(new.execution_token_hash) != 64
               OR lower(new.execution_token_hash) IS NOT new.execution_token_hash
               OR new.execution_token_hash GLOB '*[^0-9a-f]*'
               OR new.lifecycle_protocol_version IS NULL
               OR new.lifecycle_protocol_version < old.lifecycle_protocol_version;
        END
    """,
    "agent_executions_project_agent_guard_bu": """
        CREATE TRIGGER IF NOT EXISTS agent_executions_project_agent_guard_bu
        BEFORE UPDATE OF project_id, agent_id ON agent_executions
        BEGIN
            SELECT RAISE(ABORT, 'agent execution owner is immutable')
            WHERE new.project_id IS NOT old.project_id
               OR new.agent_id IS NOT old.agent_id;
            SELECT RAISE(ABORT, 'agent execution owner is missing, mismatched, or retired')
            WHERE NOT EXISTS (
                SELECT 1
                FROM agents
                WHERE id IS new.agent_id
                  AND project_id IS new.project_id
            );
            SELECT RAISE(ABORT, 'agent execution reservation binding mismatch')
            WHERE EXISTS (
                SELECT 1
                FROM file_reservations
                WHERE execution_id IS old.id
                  AND (project_id IS NOT new.project_id OR agent_id IS NOT new.agent_id)
            );
            SELECT RAISE(ABORT, 'agent execution child binding mismatch')
            WHERE EXISTS (
                SELECT 1
                FROM agent_executions
                WHERE parent_execution_id IS old.id
                  AND (project_id IS NOT new.project_id OR agent_id IS NOT new.agent_id)
            );
        END
    """,
    "agents_execution_project_guard_bu": """
        CREATE TRIGGER IF NOT EXISTS agents_execution_project_guard_bu
        BEFORE UPDATE OF id, project_id ON agents
        BEGIN
            SELECT RAISE(ABORT, 'agent has project-bound executions')
            WHERE EXISTS (
                SELECT 1
                FROM agent_executions
                WHERE agent_id IS old.id
                  AND (agent_id IS NOT new.id OR project_id IS NOT new.project_id)
            );
        END
    """,
    "agent_executions_parent_guard_bi": """
        CREATE TRIGGER IF NOT EXISTS agent_executions_parent_guard_bi
        BEFORE INSERT ON agent_executions
        WHEN new.parent_execution_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'agent execution parent mismatch or inactive')
            WHERE NOT EXISTS (
                SELECT 1
                FROM agent_executions
                WHERE id IS new.parent_execution_id
                  AND project_id IS new.project_id
                  AND agent_id IS new.agent_id
                  AND status = 'active'
            );
        END
    """,
    "agent_executions_parent_guard_bu": """
        CREATE TRIGGER IF NOT EXISTS agent_executions_parent_guard_bu
        BEFORE UPDATE OF parent_execution_id, kind, project_id, agent_id
        ON agent_executions
        WHEN new.parent_execution_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'agent execution parent mismatch or inactive')
            WHERE NOT EXISTS (
                SELECT 1
                FROM agent_executions
                WHERE id IS new.parent_execution_id
                  AND project_id IS new.project_id
                  AND agent_id IS new.agent_id
                  AND status = 'active'
            );
        END
    """,
    "agent_executions_terminal_guard_bu": """
        CREATE TRIGGER IF NOT EXISTS agent_executions_terminal_guard_bu
        BEFORE UPDATE ON agent_executions
        BEGIN
            SELECT RAISE(ABORT, 'terminal agent execution is immutable')
            WHERE old.status != 'active';
            SELECT RAISE(ABORT, 'agent execution has active children')
            WHERE new.status IS NOT 'active'
              AND EXISTS (
                  SELECT 1
                  FROM agent_executions
                  WHERE parent_execution_id IS old.id AND status = 'active'
              );
            SELECT RAISE(ABORT, 'agent execution has active reservations')
            WHERE new.status IS NOT 'active'
              AND EXISTS (
                  SELECT 1
                  FROM file_reservations
                  WHERE execution_id IS old.id
                    AND origin = 'auto'
                    AND released_ts IS NULL
                    AND expires_ts > CURRENT_TIMESTAMP
              );
        END
    """,
    "agent_executions_build_slot_projection_ai": """
        CREATE TRIGGER IF NOT EXISTS agent_executions_build_slot_projection_ai
        AFTER INSERT ON agent_executions
        WHEN new.status != 'active'
        BEGIN
            INSERT OR IGNORE INTO build_slot_artifact_projections
                (execution_id, project_id, created_ts, reconciled_ts)
            VALUES (
                new.id,
                new.project_id,
                COALESCE(new.ended_ts, new.last_active_ts),
                NULL
            );
        END
    """,
    "agent_executions_build_slot_projection_au": """
        CREATE TRIGGER IF NOT EXISTS agent_executions_build_slot_projection_au
        AFTER UPDATE OF status ON agent_executions
        WHEN old.status = 'active' AND new.status != 'active'
        BEGIN
            INSERT OR IGNORE INTO build_slot_artifact_projections
                (execution_id, project_id, created_ts, reconciled_ts)
            VALUES (
                new.id,
                new.project_id,
                COALESCE(new.ended_ts, new.last_active_ts),
                NULL
            );
        END
    """,
    "build_slot_artifact_paths_active_execution_guard_bi": """
        CREATE TRIGGER IF NOT EXISTS
        build_slot_artifact_paths_active_execution_guard_bi
        BEFORE INSERT ON build_slot_artifact_paths
        BEGIN
            SELECT RAISE(
                ABORT,
                'build-slot artifact path execution mismatch or inactive'
            )
            WHERE NOT EXISTS (
                SELECT 1
                FROM agent_executions
                WHERE id IS new.execution_id
                  AND project_id IS new.project_id
                  AND status = 'active'
            );
        END
    """,
    "build_slot_artifact_paths_immutable_bu": """
        CREATE TRIGGER IF NOT EXISTS build_slot_artifact_paths_immutable_bu
        BEFORE UPDATE ON build_slot_artifact_paths
        BEGIN
            SELECT RAISE(ABORT, 'build-slot artifact path is immutable');
        END
    """,
    "build_slot_artifact_paths_immutable_bd": """
        CREATE TRIGGER IF NOT EXISTS build_slot_artifact_paths_immutable_bd
        BEFORE DELETE ON build_slot_artifact_paths
        BEGIN
            SELECT RAISE(ABORT, 'build-slot artifact path is immutable');
        END
    """,
    "file_reservations_execution_guard_bi": """
        CREATE TRIGGER IF NOT EXISTS file_reservations_execution_guard_bi
        BEFORE INSERT ON file_reservations
        WHEN new.execution_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'reservation execution binding mismatch')
            WHERE NOT EXISTS (
                SELECT 1
                FROM agent_executions
                WHERE id IS new.execution_id
                  AND project_id IS new.project_id
                  AND agent_id IS new.agent_id
                  AND status = 'active'
            );
        END
    """,
    "file_reservations_execution_guard_bu": """
        CREATE TRIGGER IF NOT EXISTS file_reservations_execution_guard_bu
        BEFORE UPDATE ON file_reservations
        BEGIN
            SELECT RAISE(ABORT, 'reservation execution binding is immutable')
            WHERE new.execution_id IS NOT old.execution_id;
            SELECT RAISE(ABORT, 'reservation execution owner is immutable')
            WHERE old.execution_id IS NOT NULL
              AND (
                    new.project_id IS NOT old.project_id
                    OR new.agent_id IS NOT old.agent_id
              );
            SELECT RAISE(ABORT, 'reservation execution binding mismatch')
            WHERE new.execution_id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1
                FROM agent_executions
                WHERE id IS new.execution_id
                  AND project_id IS new.project_id
                  AND agent_id IS new.agent_id
                  AND status = 'active'
            )
            AND NOT (
                old.project_id IS new.project_id
                AND old.agent_id IS new.agent_id
                AND old.execution_id IS new.execution_id
                AND old.origin IS new.origin
                AND old.path_pattern IS new.path_pattern
                AND old.exclusive IS new.exclusive
                AND old.reason IS new.reason
                AND old.created_ts IS new.created_ts
                AND old.expires_ts IS new.expires_ts
                AND old.released_ts IS new.released_ts
            )
            AND NOT (
                old.project_id IS new.project_id
                AND old.agent_id IS new.agent_id
                AND old.execution_id IS new.execution_id
                AND old.origin IS new.origin
                AND old.path_pattern IS new.path_pattern
                AND old.exclusive IS new.exclusive
                AND old.reason IS new.reason
                AND old.created_ts IS new.created_ts
                AND new.expires_ts <= old.expires_ts
                AND old.released_ts IS NULL
                AND new.released_ts IS NOT NULL
            );
        END
    """,
}


_MESSAGE_DELIVERIES_PROJECT_GUARD_BD_SQL: Final[str] = """
    CREATE TRIGGER IF NOT EXISTS message_deliveries_project_guard_bd
    BEFORE DELETE ON projects
    BEGIN
        SELECT RAISE(ABORT, 'project has pending message delivery')
        WHERE EXISTS (
            SELECT 1
            FROM message_deliveries
            WHERE state = 'pending'
              AND (
                    project_id IS old.id
                    OR sender_project_id_snapshot IS old.id
                    OR (
                        actor_kind = 'agent'
                        AND actor_project_id_snapshot IS old.id
                    )
                )
        );
    END
"""


def _sqlite_table_columns(connection: Any, table_name: str) -> set[str]:
    """Return exact SQLite column names for one trusted internal table."""
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_sqlite_column(
    connection: Any,
    *,
    table_name: str,
    column_name: str,
    alter_sql: str,
) -> None:
    """Apply a required additive migration without swallowing DDL failures."""
    if column_name not in _sqlite_table_columns(connection, table_name):
        connection.exec_driver_sql(alter_sql)
    if column_name not in _sqlite_table_columns(connection, table_name):
        raise RuntimeError(
            f"Required schema column {table_name}.{column_name} is unavailable"
        )


def _normalize_schema_sql(sql: str) -> str:
    """Normalize harmless SQLite DDL formatting without erasing semantics."""
    normalized = re.sub(r"\bIF\s+NOT\s+EXISTS\b", "", sql, flags=re.IGNORECASE)
    normalized = normalized.replace('"', "").replace("`", "")
    return re.sub(r"\s+", " ", normalized).strip().removesuffix(";").casefold()


def _agent_execution_table_mismatches(connection: Any) -> list[str]:
    """Return physical AgentExecution table drift from the SQLModel contract."""
    table = cast(Table, getattr(AgentExecution, "__table__"))  # noqa: B009
    actual_rows = connection.exec_driver_sql(
        "PRAGMA table_info(agent_executions)"
    ).fetchall()
    actual_columns = {
        str(row[1]): (
            str(row[2]).upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
        )
        for row in actual_rows
    }
    expected_columns = {
        column.name: (
            str(column.type.compile(dialect=connection.dialect)).upper(),
            int(not column.nullable),
            None,
            int(column.primary_key),
        )
        for column in table.columns
    }
    mismatches: list[str] = []
    if actual_columns != expected_columns:
        mismatches.append("columns")

    table_sql_row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'agent_executions'"
    ).fetchone()
    if table_sql_row is None or table_sql_row[0] is None:
        return [*mismatches, "table"]
    actual_table_sql = _normalize_schema_sql(str(table_sql_row[0]))
    for constraint in table.constraints:
        if not isinstance(constraint, CheckConstraint):
            continue
        constraint_sql = str(
            constraint.sqltext.compile(dialect=connection.dialect)
        )
        expected_clause = _normalize_schema_sql(
            f"CONSTRAINT {constraint.name} CHECK ({constraint_sql})"
        )
        if expected_clause not in actual_table_sql:
            mismatches.append(f"constraint:{constraint.name}")
    if "unique (execution_token_hash)" not in actual_table_sql:
        mismatches.append("unique:execution_token_hash")

    actual_foreign_keys = {
        tuple(str(value).casefold() for value in row[2:8])
        for row in connection.exec_driver_sql(
            "PRAGMA foreign_key_list(agent_executions)"
        ).fetchall()
    }
    expected_foreign_keys = {
        ("projects", "project_id", "id", "no action", "no action", "none"),
        ("agents", "agent_id", "id", "no action", "no action", "none"),
        (
            "agent_executions",
            "parent_execution_id",
            "id",
            "no action",
            "no action",
            "none",
        ),
    }
    if actual_foreign_keys != expected_foreign_keys:
        mismatches.append("foreign_keys")
    return mismatches


def _rebuild_agent_executions_schema(connection: Any) -> None:
    """Atomically rebuild the execution table to its canonical physical shape."""
    mismatches = _agent_execution_table_mismatches(connection)
    if not mismatches:
        return

    table = cast(Table, getattr(AgentExecution, "__table__"))  # noqa: B009
    canonical_index_names = {
        index.name for index in table.indexes if index.name is not None
    } | set(_AGENT_EXECUTION_INDEX_SQL)
    canonical_trigger_names = set(_AGENT_EXECUTION_TRIGGER_SQL)
    dependent_schema_objects = connection.exec_driver_sql(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE tbl_name = 'agent_executions'
          AND type IN ('index', 'trigger')
          AND sql IS NOT NULL
        ORDER BY type, name
        """
    ).fetchall()
    custom_schema_sql = [
        str(sql)
        for object_type, name, sql in dependent_schema_objects
        if (
            (object_type == "index" and name not in canonical_index_names)
            or (object_type == "trigger" and name not in canonical_trigger_names)
        )
    ]
    referencing_triggers = connection.exec_driver_sql(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'trigger'
          AND tbl_name != 'agent_executions'
          AND sql IS NOT NULL
          AND instr(lower(sql), 'agent_executions') > 0
        ORDER BY name
        """
    ).fetchall()
    for trigger_name, _trigger_sql in referencing_triggers:
        quoted_name = connection.dialect.identifier_preparer.quote(str(trigger_name))
        connection.exec_driver_sql(f"DROP TRIGGER {quoted_name}")

    temporary_metadata = MetaData()
    cast(Table, getattr(Project, "__table__")).to_metadata(temporary_metadata)  # noqa: B009
    cast(Table, getattr(Agent, "__table__")).to_metadata(temporary_metadata)  # noqa: B009
    temporary_table = table.to_metadata(
        temporary_metadata,
        name="agent_executions_schema_v2",
    )
    connection.execute(CreateTable(temporary_table))

    column_names = [column.name for column in table.columns]
    quoted_columns = ", ".join(f'"{name}"' for name in column_names)
    connection.exec_driver_sql(
        f'INSERT INTO "agent_executions_schema_v2" ({quoted_columns}) '
        f'SELECT {quoted_columns} FROM "agent_executions"'
    )
    source_count = int(
        connection.exec_driver_sql("SELECT COUNT(*) FROM agent_executions").scalar_one()
    )
    target_count = int(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM agent_executions_schema_v2"
        ).scalar_one()
    )
    forward_difference = connection.exec_driver_sql(
        f"SELECT 1 FROM (SELECT {quoted_columns} FROM agent_executions "
        f"EXCEPT SELECT {quoted_columns} FROM agent_executions_schema_v2) LIMIT 1"
    ).fetchone()
    reverse_difference = connection.exec_driver_sql(
        f"SELECT 1 FROM (SELECT {quoted_columns} FROM agent_executions_schema_v2 "
        f"EXCEPT SELECT {quoted_columns} FROM agent_executions) LIMIT 1"
    ).fetchone()
    if (
        source_count != target_count
        or forward_difference is not None
        or reverse_difference is not None
    ):
        raise RuntimeError(
            "AgentExecution schema migration did not preserve every execution row"
        )

    connection.exec_driver_sql("DROP TABLE agent_executions")
    connection.exec_driver_sql(
        "ALTER TABLE agent_executions_schema_v2 RENAME TO agent_executions"
    )
    for index in sorted(table.indexes, key=lambda item: item.name or ""):
        connection.execute(CreateIndex(index))
    for schema_sql in custom_schema_sql:
        connection.exec_driver_sql(schema_sql)
    for _trigger_name, trigger_sql in referencing_triggers:
        connection.exec_driver_sql(str(trigger_sql))

    remaining_mismatches = _agent_execution_table_mismatches(connection)
    if remaining_mismatches:
        raise RuntimeError(
            "AgentExecution schema migration left physical drift: "
            f"{remaining_mismatches}"
        )


async def _migrate_agent_executions_schema(engine: AsyncEngine) -> None:
    """Rebuild an intermediate nullable execution schema with FK safety."""
    async with engine.connect() as connection:
        mismatches = await connection.run_sync(_agent_execution_table_mismatches)
        await connection.commit()
        if not mismatches:
            return

        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await connection.commit()
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await connection.run_sync(_rebuild_agent_executions_schema)
            await connection.commit()
        except BaseException:
            with suppress(BaseException):
                await connection.rollback()
            raise
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            foreign_keys_enabled = int(
                (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
            )
            if foreign_keys_enabled != 1:
                raise RuntimeError("SQLite foreign-key enforcement was not restored")
            violations = (
                await connection.exec_driver_sql("PRAGMA foreign_key_check")
            ).fetchall()
            if violations:
                raise RuntimeError(
                    "AgentExecution schema migration failed foreign-key validation"
                )
            await connection.commit()


def _validate_agent_execution_schema(connection: Any) -> None:
    """Fail closed on semantic drift in execution capability DDL."""
    table_mismatches = _agent_execution_table_mismatches(connection)
    if table_mismatches:
        raise RuntimeError(
            "AgentExecution schema validation failed; table drift: "
            f"{table_mismatches}"
        )

    reservation_columns = {
        str(row[1]): (str(row[2]).upper(), int(row[3]))
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(file_reservations)"
        ).fetchall()
    }
    required_reservation_columns = {
        "execution_id": ("VARCHAR(36)", 0),
        "origin": ("VARCHAR(16)", 1),
        "archive_revision": ("INTEGER", 1),
        "archive_synced_revision": ("INTEGER", 1),
    }
    if any(
        reservation_columns.get(name) != definition
        for name, definition in required_reservation_columns.items()
    ):
        raise RuntimeError(
            "AgentExecution schema validation failed; reservation column drift"
        )
    reservation_foreign_keys = {
        tuple(str(value).casefold() for value in row[2:8])
        for row in connection.exec_driver_sql(
            "PRAGMA foreign_key_list(file_reservations)"
        ).fetchall()
    }
    if (
        "agent_executions",
        "execution_id",
        "id",
        "no action",
        "no action",
        "none",
    ) not in reservation_foreign_keys:
        raise RuntimeError(
            "AgentExecution schema validation failed; reservation FK drift"
        )

    invalid_capabilities = int(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM agent_executions "
            "WHERE execution_token_hash IS NULL "
            "OR length(execution_token_hash) != 64 "
            "OR lower(execution_token_hash) != execution_token_hash "
            "OR execution_token_hash GLOB '*[^0-9a-f]*' "
            "OR lifecycle_protocol_version IS NULL "
            "OR lifecycle_protocol_version < 0"
        ).scalar_one()
    )
    if invalid_capabilities:
        raise RuntimeError(
            "AgentExecution schema validation found invalid capability rows"
        )

    invalid_reservation_origins = int(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM file_reservations "
            "WHERE origin IS NULL OR origin NOT IN ('auto', 'explicit')"
        ).scalar_one()
    )
    if invalid_reservation_origins:
        raise RuntimeError(
            "AgentExecution schema validation found invalid reservation origins"
        )

    invalid_reservation_archive_versions = int(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM file_reservations "
            "WHERE archive_revision IS NULL OR archive_revision < 1 "
            "OR archive_synced_revision IS NULL OR archive_synced_revision < 0 "
            "OR archive_synced_revision > archive_revision"
        ).scalar_one()
    )
    if invalid_reservation_archive_versions:
        raise RuntimeError(
            "AgentExecution schema validation found invalid reservation archive versions"
        )

    actual_indexes = {
        str(row[0]): _normalize_schema_sql(str(row[1]))
        for row in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND sql IS NOT NULL"
        ).fetchall()
    }
    index_mismatches = sorted(
        name
        for name, expected_sql in _AGENT_EXECUTION_INDEX_SQL.items()
        if actual_indexes.get(name) != _normalize_schema_sql(expected_sql)
    )
    if index_mismatches:
        raise RuntimeError(
            "AgentExecution schema validation failed; index definition drift: "
            f"{index_mismatches}"
        )

    expected_triggers = {
        **_AGENT_EXECUTION_TRIGGER_SQL,
        "message_deliveries_project_guard_bd": (
            _MESSAGE_DELIVERIES_PROJECT_GUARD_BD_SQL
        ),
    }
    actual_triggers = {
        str(row[0]): _normalize_schema_sql(str(row[1]))
        for row in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND sql IS NOT NULL"
        ).fetchall()
    }
    trigger_mismatches = sorted(
        name
        for name, expected_sql in expected_triggers.items()
        if actual_triggers.get(name) != _normalize_schema_sql(expected_sql)
    )
    if trigger_mismatches:
        raise RuntimeError(
            "AgentExecution schema validation failed; trigger definition drift: "
            f"{trigger_mismatches}"
        )


def _setup_fts(
    connection: Any,
    *,
    validate_execution_schema: bool = True,
) -> None:
    # FTS5 + the ``ALTER TABLE`` idioms below are SQLite-only. Skip them
    # entirely on other backends so schema init does not blow up with
    # ``CREATE VIRTUAL TABLE`` against Postgres et al. Runtime search paths
    # also short-circuit to LIKE fallbacks when FTS is unavailable.
    if not _is_sqlite_connection(connection):
        engine = getattr(connection, "engine", None)
        dialect_name = getattr(getattr(engine, "dialect", None), "name", "unknown")
        _logger.info(
            "db.fts.skipped_non_sqlite",
            extra={"dialect": dialect_name},
        )
        return
    connection.exec_driver_sql(
        "CREATE VIRTUAL TABLE IF NOT EXISTS fts_messages USING fts5(message_id UNINDEXED, subject, body)"
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS fts_messages_ai
        AFTER INSERT ON messages
        BEGIN
            INSERT INTO fts_messages(rowid, message_id, subject, body)
            VALUES (new.id, new.id, new.subject, new.body_md);
        END;
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS fts_messages_ad
        AFTER DELETE ON messages
        BEGIN
            DELETE FROM fts_messages WHERE rowid = old.id;
        END;
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS fts_messages_au
        AFTER UPDATE ON messages
        BEGIN
            DELETE FROM fts_messages WHERE rowid = old.id;
            INSERT INTO fts_messages(rowid, message_id, subject, body)
            VALUES (new.id, new.id, new.subject, new.body_md);
        END;
        """
    )
    # Additional performance indexes for common access patterns
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_created_ts ON messages(created_ts)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_thread_id ON messages(thread_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_importance ON messages(importance)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_sender_created ON messages(sender_id, created_ts DESC)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_project_created_desc "
        "ON messages(project_id, created_ts DESC)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_file_reservations_expires_ts ON file_reservations(expires_ts)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_message_recipients_agent ON message_recipients(agent_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_message_recipients_agent_message "
        "ON message_recipients(agent_id, message_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_messages_project_sender_created_desc "
        "ON messages(project_id, sender_id, created_ts DESC)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_file_reservations_project_released_expires "
        "ON file_reservations(project_id, released_ts, expires_ts)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_file_reservations_project_agent_released "
        "ON file_reservations(project_id, agent_id, released_ts)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_product_project "
        "ON product_project_links(product_id, project_id)"
    )
    # AgentLink indexes for efficient contact lookups
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_links_a_project "
        "ON agent_links(a_project_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_links_b_project "
        "ON agent_links(b_project_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_links_b_project_agent "
        "ON agent_links(b_project_id, b_agent_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_links_status "
        "ON agent_links(status)"
    )
    # Schema migrations: add columns that may be missing on older databases.
    # SQLite ALTER TABLE ADD COLUMN is idempotent-safe via try/except.
    # These columns form the execution capability/ownership boundary. Unlike
    # older best-effort additive migrations below, failures must abort startup:
    # enforce mode cannot safely run against a partially upgraded schema.
    _ensure_sqlite_column(
        connection,
        table_name="file_reservations",
        column_name="execution_id",
        alter_sql=(
            "ALTER TABLE file_reservations ADD COLUMN execution_id VARCHAR(36) "
            "DEFAULT NULL REFERENCES agent_executions(id)"
        ),
    )
    _ensure_sqlite_column(
        connection,
        table_name="file_reservations",
        column_name="origin",
        alter_sql=(
            "ALTER TABLE file_reservations ADD COLUMN origin VARCHAR(16) "
            "NOT NULL DEFAULT 'explicit'"
        ),
    )
    _ensure_sqlite_column(
        connection,
        table_name="file_reservations",
        column_name="archive_revision",
        alter_sql=(
            "ALTER TABLE file_reservations ADD COLUMN archive_revision INTEGER "
            "NOT NULL DEFAULT 1"
        ),
    )
    _ensure_sqlite_column(
        connection,
        table_name="file_reservations",
        column_name="archive_synced_revision",
        alter_sql=(
            "ALTER TABLE file_reservations ADD COLUMN archive_synced_revision "
            "INTEGER NOT NULL DEFAULT 0"
        ),
    )
    _ensure_sqlite_column(
        connection,
        table_name="agent_executions",
        column_name="execution_token_hash",
        alter_sql=(
            "ALTER TABLE agent_executions ADD COLUMN execution_token_hash "
            "VARCHAR(64) DEFAULT NULL"
        ),
    )
    _ensure_sqlite_column(
        connection,
        table_name="agent_executions",
        column_name="lifecycle_protocol_version",
        alter_sql=(
            "ALTER TABLE agent_executions ADD COLUMN lifecycle_protocol_version "
            "INTEGER NOT NULL DEFAULT 0"
        ),
    )
    _ensure_sqlite_column(
        connection,
        table_name="projects",
        column_name="project_uid",
        alter_sql=(
            "ALTER TABLE projects ADD COLUMN project_uid VARCHAR(255) DEFAULT NULL"
        ),
    )
    _ensure_sqlite_column(
        connection,
        table_name="agents",
        column_name="provisioning_state",
        alter_sql=(
            "ALTER TABLE agents ADD COLUMN provisioning_state VARCHAR(16) "
            "NOT NULL DEFAULT 'active'"
        ),
    )

    for migration_sql in [
        "ALTER TABLE agents ADD COLUMN retired_at DATETIME DEFAULT NULL",
        "ALTER TABLE agents ADD COLUMN agent_generation VARCHAR(64) DEFAULT NULL",
        "ALTER TABLE projects ADD COLUMN project_generation VARCHAR(64) DEFAULT NULL",
        "ALTER TABLE projects ADD COLUMN archived_at DATETIME DEFAULT NULL",
        "ALTER TABLE agents ADD COLUMN registration_token VARCHAR(64) DEFAULT NULL",
        "ALTER TABLE messages ADD COLUMN topic VARCHAR(64) DEFAULT NULL",
        # #188: persist the direct parent→child reply edge so replies survive a
        # round-trip through the DB (previously reply_to lived only in the
        # response payload and was lost on read).
        "ALTER TABLE messages ADD COLUMN reply_to INTEGER DEFAULT NULL",
        "ALTER TABLE messages ADD COLUMN delivery_id VARCHAR(36) DEFAULT NULL",
        # Display alias. Additive and nullable, so an older database keeps
        # working and every agent simply has no alias until it sets one.
        "ALTER TABLE agents ADD COLUMN display_name VARCHAR(128) DEFAULT NULL",
        "ALTER TABLE agents ADD COLUMN notify_sound VARCHAR(32) DEFAULT NULL",
        "ALTER TABLE ui_users ADD COLUMN session_generation VARCHAR(64) DEFAULT NULL",
        "ALTER TABLE ui_users ADD COLUMN display_name VARCHAR(128) DEFAULT NULL",
        "ALTER TABLE ui_users ADD COLUMN profile_revision INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE ui_users ADD COLUMN preferred_ui_locale VARCHAR(16) NOT NULL DEFAULT 'en'",
        "ALTER TABLE ui_users ADD COLUMN preferred_correspondence_locale VARCHAR(16) DEFAULT NULL",
        "ALTER TABLE ui_access_audit_events ADD COLUMN "
        "actor_account_generation_snapshot VARCHAR(64) DEFAULT NULL",
        "ALTER TABLE ui_access_audit_events ADD COLUMN "
        "actor_session_epoch_snapshot INTEGER DEFAULT NULL",
        "ALTER TABLE ui_access_audit_events ADD COLUMN "
        "project_generation_snapshot VARCHAR(64) DEFAULT NULL",
    ]:
        with suppress(Exception):  # Column already exists — safe to ignore
            connection.exec_driver_sql(migration_sql)

    # This execution surface is still unreleased; existing development rows
    # receive irrecoverable random capabilities so no caller can accidentally
    # authenticate them after the capability boundary is introduced.
    connection.exec_driver_sql(
        "UPDATE agent_executions SET execution_token_hash = lower(hex(randomblob(32))) "
        "WHERE execution_token_hash IS NULL"
    )

    # The delivery idempotency scope changed before this unshipped surface was
    # enabled: a UI actor's mailbox-project lifetime is now part of its scope.
    # Recreate the development index instead of preserving the obsolete shape.
    connection.exec_driver_sql("DROP INDEX IF EXISTS uq_message_deliveries_idempotency")

    # Index migrations for newly added columns.
    # CREATE INDEX IF NOT EXISTS is natively idempotent in SQLite.
    for index_sql in [
        # Multiple NULLs deliberately preserve unclaimed legacy rows.  Every
        # newly created or lazily claimed Project receives a durable UID, and
        # SQLite's UNIQUE semantics reject any accidental merge.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_project_uid "
        "ON projects (project_uid)",
        "CREATE INDEX IF NOT EXISTS ix_agents_registration_token ON agents (registration_token)",
        "CREATE INDEX IF NOT EXISTS idx_messages_project_topic ON messages (project_id, topic)",
        "CREATE INDEX IF NOT EXISTS ix_messages_reply_to ON messages (reply_to)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_delivery "
        "ON messages (delivery_id) WHERE delivery_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_ui_access_audit_target_created "
        "ON ui_access_audit_events (target_user_id, created_ts)",
        "CREATE INDEX IF NOT EXISTS idx_ui_access_audit_project_created "
        "ON ui_access_audit_events (project_id, created_ts)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_message_deliveries_idempotency "
        "ON message_deliveries ("
        "project_id, project_generation_snapshot, actor_kind, actor_id, "
        "coalesce(actor_generation_snapshot, ''), "
        "coalesce(actor_project_generation_snapshot, ''), idempotency_key)",
        "CREATE INDEX IF NOT EXISTS idx_message_deliveries_due "
        "ON message_deliveries (next_attempt_ts, lease_expires_ts) "
        "WHERE state = 'pending'",
        "CREATE INDEX IF NOT EXISTS idx_message_deliveries_project_created "
        "ON message_deliveries (project_id, created_ts)",
        "CREATE INDEX IF NOT EXISTS idx_message_deliveries_reply_pending "
        "ON message_deliveries (reply_to_message_id, state)",
        "CREATE INDEX IF NOT EXISTS idx_message_delivery_recipients_agent "
        "ON message_delivery_recipients (agent_id, delivery_id)",
        *_AGENT_EXECUTION_INDEX_SQL.values(),
    ]:
        connection.exec_driver_sql(index_sql)

    # These definitions are dropped and recreated on every startup. Merely
    # checking that a trigger name exists is unsafe: an older or tampered body
    # could otherwise retain weaker NULL/equality or terminal-state semantics.
    for trigger_name in _AGENT_EXECUTION_TRIGGER_SQL:
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger_name}")
    for trigger_sql in _AGENT_EXECUTION_TRIGGER_SQL.values():
        connection.exec_driver_sql(trigger_sql)

    # Seed the outbox for terminal rows created before this projection table
    # existed. INSERT OR IGNORE preserves any already acknowledged row.
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO build_slot_artifact_projections "
        "(execution_id, project_id, created_ts, reconciled_ts) "
        "SELECT id, project_id, COALESCE(ended_ts, last_active_ts), NULL "
        "FROM agent_executions WHERE status != 'active'"
    )

    connection.exec_driver_sql(
        """
        UPDATE ui_users
        SET role = 'member', session_epoch = session_epoch + 1
        WHERE role = 'viewer'
        """
    )
    connection.exec_driver_sql(
        f"""
        UPDATE ui_users
        SET preferred_ui_locale = CASE
            WHEN trim(COALESCE(preferred_ui_locale, '')) COLLATE NOCASE
                 IN ({_MAIL_UI_LOCALE_SQL})
                THEN CASE lower(trim(preferred_ui_locale))
                    {" ".join(f"WHEN {value.casefold()!r} THEN {value!r}" for value in MAIL_UI_LOCALE_VALUES)}
                END
            ELSE 'en'
        END,
        preferred_correspondence_locale = CASE
            WHEN preferred_correspondence_locale IS NULL THEN NULL
            WHEN trim(preferred_correspondence_locale) COLLATE NOCASE
                 IN ({_MAIL_UI_LOCALE_SQL})
                THEN CASE lower(trim(preferred_correspondence_locale))
                    {" ".join(f"WHEN {value.casefold()!r} THEN {value!r}" for value in MAIL_UI_LOCALE_VALUES)}
                END
            ELSE NULL
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_users_identity_collision_guard_bi
        BEFORE INSERT ON ui_users
        BEGIN
            SELECT RAISE(ABORT, 'ui_users identity collision')
            WHERE EXISTS (
                SELECT 1
                FROM ui_users
                WHERE id = new.id OR username = new.username
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_users_identity_collision_guard_bu
        BEFORE UPDATE OF id, username ON ui_users
        BEGIN
            SELECT RAISE(ABORT, 'ui_users identity collision')
            WHERE EXISTS (
                SELECT 1
                FROM ui_users
                WHERE id != old.id
                  AND (id = new.id OR username = new.username)
            );
        END
        """
    )
    connection.exec_driver_sql("DROP TRIGGER IF EXISTS ui_users_locale_guard_bi")
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER IF NOT EXISTS ui_users_locale_guard_bi
        BEFORE INSERT ON ui_users
        BEGIN
            SELECT RAISE(ABORT, 'invalid preferred_ui_locale')
            WHERE new.preferred_ui_locale IS NULL
               OR new.preferred_ui_locale NOT IN ({_MAIL_UI_LOCALE_SQL});
            SELECT RAISE(ABORT, 'invalid preferred_correspondence_locale')
            WHERE new.preferred_correspondence_locale IS NOT NULL
              AND new.preferred_correspondence_locale NOT IN ({_MAIL_UI_LOCALE_SQL});
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_users_profile_guard_bi
        BEFORE INSERT ON ui_users
        BEGIN
            SELECT RAISE(ABORT, 'invalid display_name')
            WHERE new.display_name IS NOT NULL
              AND (length(trim(new.display_name)) = 0 OR length(new.display_name) > 128);
            SELECT RAISE(ABORT, 'invalid profile_revision')
            WHERE new.profile_revision < 1;
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_users_profile_guard_bu
        BEFORE UPDATE OF display_name, profile_revision ON ui_users
        BEGIN
            SELECT RAISE(ABORT, 'invalid display_name')
            WHERE new.display_name IS NOT NULL
              AND (length(trim(new.display_name)) = 0 OR length(new.display_name) > 128);
            SELECT RAISE(ABORT, 'invalid profile_revision')
            WHERE new.profile_revision < 1;
        END
        """
    )
    connection.exec_driver_sql("DROP TRIGGER IF EXISTS ui_users_locale_guard_bu")
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER IF NOT EXISTS ui_users_locale_guard_bu
        BEFORE UPDATE OF preferred_ui_locale, preferred_correspondence_locale ON ui_users
        BEGIN
            SELECT RAISE(ABORT, 'invalid preferred_ui_locale')
            WHERE new.preferred_ui_locale IS NULL
               OR new.preferred_ui_locale NOT IN ({_MAIL_UI_LOCALE_SQL});
            SELECT RAISE(ABORT, 'invalid preferred_correspondence_locale')
            WHERE new.preferred_correspondence_locale IS NOT NULL
              AND new.preferred_correspondence_locale NOT IN ({_MAIL_UI_LOCALE_SQL});
        END
        """
    )
    generation_rows = connection.exec_driver_sql(
        """
        SELECT id
        FROM ui_users
        WHERE session_generation IS NULL OR trim(session_generation) = ''
        """
    ).fetchall()
    for generation_row in generation_rows:
        connection.exec_driver_sql(
            "UPDATE ui_users SET session_generation = ? WHERE id = ?",
            (secrets.token_hex(32), int(generation_row[0])),
        )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_users_generation_guard_bi
        BEFORE INSERT ON ui_users
        BEGIN
            SELECT RAISE(ABORT, 'invalid session_generation')
            WHERE new.session_generation IS NULL
               OR length(new.session_generation) != 64
               OR new.session_generation GLOB '*[^0-9a-f]*';
            SELECT RAISE(ABORT, 'session_generation lifetime was already used')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries
                WHERE actor_kind = 'ui_user'
                  AND actor_id = new.id
                  AND actor_generation_snapshot = new.session_generation
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_users_generation_guard_bu
        BEFORE UPDATE OF session_generation ON ui_users
        WHEN old.session_generation IS NOT new.session_generation
        BEGIN
            SELECT RAISE(ABORT, 'session_generation is immutable');
        END
        """
    )
    project_generation_rows = connection.exec_driver_sql(
        """
        SELECT id
        FROM projects
        WHERE project_generation IS NULL OR trim(project_generation) = ''
        """
    ).fetchall()
    for project_generation_row in project_generation_rows:
        connection.exec_driver_sql(
            "UPDATE projects SET project_generation = ? WHERE id = ?",
            (secrets.token_hex(32), int(project_generation_row[0])),
        )
    agent_generation_rows = connection.exec_driver_sql(
        """
        SELECT id
        FROM agents
        WHERE agent_generation IS NULL OR trim(agent_generation) = ''
        """
    ).fetchall()
    for agent_generation_row in agent_generation_rows:
        connection.exec_driver_sql(
            "UPDATE agents SET agent_generation = ? WHERE id = ?",
            (secrets.token_hex(32), int(agent_generation_row[0])),
        )
    connection.exec_driver_sql(
        "UPDATE agents SET provisioning_state = 'active' "
        "WHERE provisioning_state IS NULL OR trim(provisioning_state) = ''"
    )
    invalid_provisioning_state = connection.exec_driver_sql(
        "SELECT id, provisioning_state FROM agents "
        "WHERE provisioning_state NOT IN ('provisioning', 'active') LIMIT 1"
    ).fetchone()
    if invalid_provisioning_state is not None:
        raise RuntimeError(
            "Invalid agents.provisioning_state for Agent id "
            f"{invalid_provisioning_state[0]}: {invalid_provisioning_state[1]!r}"
        )
    for trigger_name in (
        "agents_provisioning_state_guard_bi",
        "agents_provisioning_state_guard_bu",
        "agent_executions_active_agent_guard_bi",
        "file_reservations_active_agent_guard_bi",
        "agent_links_active_agents_guard_bi",
        "messages_active_sender_guard_bi",
        "message_recipients_active_agent_guard_bi",
        "message_deliveries_active_agents_guard_bi",
        "message_delivery_recipients_active_agent_guard_bi",
    ):
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger_name}")
    connection.exec_driver_sql(
        """
        CREATE TRIGGER agents_provisioning_state_guard_bi
        BEFORE INSERT ON agents
        BEGIN
            SELECT RAISE(ABORT, 'invalid agent provisioning_state')
            WHERE new.provisioning_state NOT IN ('provisioning', 'active');
            SELECT RAISE(ABORT, 'provisioning agent requires registration token')
            WHERE new.provisioning_state = 'provisioning'
              AND (new.registration_token IS NULL OR trim(new.registration_token) = '');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER agents_provisioning_state_guard_bu
        BEFORE UPDATE OF provisioning_state ON agents
        WHEN old.provisioning_state IS NOT new.provisioning_state
        BEGIN
            SELECT RAISE(ABORT, 'invalid agent provisioning_state transition')
            WHERE old.provisioning_state != 'provisioning'
               OR new.provisioning_state != 'active';
            SELECT RAISE(ABORT, 'active agent requires registration token')
            WHERE new.registration_token IS NULL OR trim(new.registration_token) = '';
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER agent_executions_active_agent_guard_bi
        BEFORE INSERT ON agent_executions
        BEGIN
            SELECT RAISE(ABORT, 'agent execution requires active Agent')
            WHERE NOT EXISTS (
                SELECT 1 FROM agents
                WHERE id = new.agent_id AND provisioning_state = 'active'
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER file_reservations_active_agent_guard_bi
        BEFORE INSERT ON file_reservations
        WHEN new.agent_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'file reservation requires active Agent')
            WHERE NOT EXISTS (
                SELECT 1 FROM agents
                WHERE id = new.agent_id AND provisioning_state = 'active'
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER agent_links_active_agents_guard_bi
        BEFORE INSERT ON agent_links
        BEGIN
            SELECT RAISE(ABORT, 'agent link requires active Agents')
            WHERE NOT EXISTS (
                SELECT 1 FROM agents
                WHERE id = new.a_agent_id AND provisioning_state = 'active'
            ) OR NOT EXISTS (
                SELECT 1 FROM agents
                WHERE id = new.b_agent_id AND provisioning_state = 'active'
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER messages_active_sender_guard_bi
        BEFORE INSERT ON messages
        BEGIN
            SELECT RAISE(ABORT, 'message requires active sender Agent')
            WHERE NOT EXISTS (
                SELECT 1 FROM agents
                WHERE id = new.sender_id AND provisioning_state = 'active'
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER message_recipients_active_agent_guard_bi
        BEFORE INSERT ON message_recipients
        BEGIN
            SELECT RAISE(ABORT, 'message recipient requires active Agent')
            WHERE NOT EXISTS (
                SELECT 1 FROM agents
                WHERE id = new.agent_id AND provisioning_state = 'active'
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER message_deliveries_active_agents_guard_bi
        BEFORE INSERT ON message_deliveries
        BEGIN
            SELECT RAISE(ABORT, 'delivery requires active sender Agent')
            WHERE NOT EXISTS (
                SELECT 1 FROM agents
                WHERE id = new.sender_id AND provisioning_state = 'active'
            );
            SELECT RAISE(ABORT, 'delivery requires active actor Agent')
            WHERE new.actor_kind = 'agent' AND NOT EXISTS (
                SELECT 1 FROM agents
                WHERE id = new.actor_id AND provisioning_state = 'active'
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER message_delivery_recipients_active_agent_guard_bi
        BEFORE INSERT ON message_delivery_recipients
        BEGIN
            SELECT RAISE(ABORT, 'delivery recipient requires active Agent')
            WHERE NOT EXISTS (
                SELECT 1 FROM agents
                WHERE id = new.agent_id AND provisioning_state = 'active'
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS agents_identity_collision_guard_bi
        BEFORE INSERT ON agents
        BEGIN
            SELECT RAISE(ABORT, 'agents identity collision')
            WHERE EXISTS (
                SELECT 1
                FROM agents
                WHERE id = new.id
                   OR (project_id = new.project_id AND name = new.name)
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS agents_identity_collision_guard_bu
        BEFORE UPDATE OF id, project_id, name ON agents
        BEGIN
            SELECT RAISE(ABORT, 'agents identity collision')
            WHERE EXISTS (
                SELECT 1
                FROM agents
                WHERE id != old.id
                  AND (
                      id = new.id
                      OR (project_id = new.project_id AND name = new.name)
                  )
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS agents_generation_guard_bi
        BEFORE INSERT ON agents
        BEGIN
            SELECT RAISE(ABORT, 'invalid agent_generation')
            WHERE new.agent_generation IS NOT NULL
              AND trim(new.agent_generation) != ''
              AND (
                  length(new.agent_generation) != 64
                  OR new.agent_generation GLOB '*[^0-9a-f]*'
              );
            SELECT RAISE(ABORT, 'agent_generation lifetime was already used')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries
                WHERE (
                    sender_id = new.id
                    AND sender_generation_snapshot = new.agent_generation
                )
                   OR (
                    actor_kind = 'agent'
                    AND actor_id = new.id
                    AND actor_generation_snapshot = new.agent_generation
                )
                   OR EXISTS (
                    SELECT 1
                    FROM message_delivery_recipients
                    WHERE delivery_id = message_deliveries.id
                      AND agent_id = new.id
                      AND agent_generation_snapshot = new.agent_generation
                )
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS agents_generation_guard_bu
        BEFORE UPDATE OF agent_generation ON agents
        WHEN old.agent_generation IS NOT new.agent_generation
        BEGIN
            SELECT RAISE(ABORT, 'agent_generation is immutable')
            WHERE old.agent_generation IS NOT NULL
              AND trim(old.agent_generation) != '';
            SELECT RAISE(ABORT, 'invalid agent_generation')
            WHERE new.agent_generation IS NULL
               OR length(new.agent_generation) != 64
               OR new.agent_generation GLOB '*[^0-9a-f]*';
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS agents_generation_ai
        AFTER INSERT ON agents
        WHEN new.agent_generation IS NULL OR trim(new.agent_generation) = ''
        BEGIN
            UPDATE agents
            SET agent_generation = lower(hex(randomblob(32)))
            WHERE id = new.id;
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS projects_slug_guard_bi
        BEFORE INSERT ON projects
        BEGIN
            SELECT RAISE(ABORT, 'invalid canonical project slug')
            WHERE length(new.slug) < 1
               OR length(new.slug) > 255
               OR lower(new.slug) != new.slug
               OR new.slug GLOB '*[^a-z0-9-]*'
               OR substr(new.slug, 1, 1) NOT GLOB '[a-z0-9]'
               OR substr(new.slug, -1, 1) NOT GLOB '[a-z0-9]';
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS projects_slug_guard_bu
        BEFORE UPDATE OF slug ON projects
        BEGIN
            SELECT RAISE(ABORT, 'invalid canonical project slug')
            WHERE length(new.slug) < 1
               OR length(new.slug) > 255
               OR lower(new.slug) != new.slug
               OR new.slug GLOB '*[^a-z0-9-]*'
               OR substr(new.slug, 1, 1) NOT GLOB '[a-z0-9]'
               OR substr(new.slug, -1, 1) NOT GLOB '[a-z0-9]';
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS projects_identity_collision_guard_bi
        BEFORE INSERT ON projects
        BEGIN
            SELECT RAISE(ABORT, 'projects identity collision')
            WHERE EXISTS (
                SELECT 1
                FROM projects
                WHERE id = new.id OR slug = new.slug
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS projects_identity_collision_guard_bu
        BEFORE UPDATE OF id, slug ON projects
        BEGIN
            SELECT RAISE(ABORT, 'projects identity collision')
            WHERE EXISTS (
                SELECT 1
                FROM projects
                WHERE id != old.id AND (id = new.id OR slug = new.slug)
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS projects_generation_guard_bi
        BEFORE INSERT ON projects
        BEGIN
            SELECT RAISE(ABORT, 'invalid project_generation')
            WHERE new.project_generation IS NOT NULL
              AND trim(new.project_generation) != ''
              AND (
                  length(new.project_generation) != 64
                  OR new.project_generation GLOB '*[^0-9a-f]*'
              );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS projects_generation_guard_bu
        BEFORE UPDATE OF project_generation ON projects
        WHEN old.project_generation IS NOT new.project_generation
        BEGIN
            SELECT RAISE(ABORT, 'project_generation is immutable')
            WHERE old.project_generation IS NOT NULL
              AND trim(old.project_generation) != '';
            SELECT RAISE(ABORT, 'invalid project_generation')
            WHERE new.project_generation IS NULL
               OR length(new.project_generation) != 64
               OR new.project_generation GLOB '*[^0-9a-f]*';
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS projects_generation_ai
        AFTER INSERT ON projects
        WHEN new.project_generation IS NULL OR trim(new.project_generation) = ''
        BEGIN
            UPDATE projects
            SET project_generation = lower(hex(randomblob(32)))
            WHERE id = new.id;
        END
        """
    )
    # Recreate the delivery triggers so an existing development database
    # receives the same final invariants as a database created from scratch.
    for delivery_trigger in (
        "messages_identity_collision_guard_bi",
        "messages_identity_collision_guard_bu",
        # Recreated below with CREATE TRIGGER IF NOT EXISTS, which is a no-op
        # against a database that already has the previous definition. Any
        # change to its authorization clause reaches an existing deployment
        # only because the name is dropped here first.
        "message_deliveries_guard_bi",
        "message_deliveries_snapshots_bu",
        "message_deliveries_receipt_bu",
        "message_deliveries_lease_fence_bu",
        "message_deliveries_terminal_bu",
        "message_deliveries_transition_bu",
        "message_deliveries_immutable_bd",
        "message_delivery_recipients_guard_bi",
        "message_delivery_recipients_immutable_bu",
        "message_delivery_recipients_immutable_bd",
        "message_deliveries_project_guard_bd",
        "message_deliveries_project_guard_bu",
        "message_deliveries_agent_pending_bd",
        "message_deliveries_agent_pending_bu",
        "message_deliveries_ui_user_pending_bd",
        "message_deliveries_ui_user_pending_bu",
        "message_deliveries_reply_target_pending_bd",
        "message_deliveries_reply_target_pending_bu",
        "message_deliveries_reply_target_pending_bi",
    ):
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {delivery_trigger}")
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS messages_identity_collision_guard_bi
        BEFORE INSERT ON messages
        BEGIN
            SELECT RAISE(ABORT, 'messages identity collision')
            WHERE EXISTS (
                SELECT 1
                FROM messages
                WHERE id = new.id
                   OR (
                       new.delivery_id IS NOT NULL
                       AND delivery_id = new.delivery_id
                   )
            );
            SELECT RAISE(ABORT, 'message delivery binding mismatch')
            WHERE new.delivery_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM message_deliveries
                  WHERE id = new.delivery_id
                    AND state = 'pending'
                    AND message_id IS NULL
                    AND project_id = new.project_id
                    AND sender_id = new.sender_id
              );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS messages_identity_collision_guard_bu
        BEFORE UPDATE OF id, delivery_id ON messages
        BEGIN
            SELECT RAISE(ABORT, 'messages identity collision')
            WHERE EXISTS (
                SELECT 1
                FROM messages
                WHERE id != old.id
                  AND (
                      id = new.id
                      OR (
                          new.delivery_id IS NOT NULL
                          AND delivery_id = new.delivery_id
                      )
                  )
            );
            SELECT RAISE(ABORT, 'message delivery binding mismatch')
            WHERE old.delivery_id IS NOT new.delivery_id
              AND new.delivery_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM message_deliveries
                  WHERE id = new.delivery_id
                    AND state = 'pending'
                    AND message_id IS NULL
                    AND project_id = new.project_id
                    AND sender_id = new.sender_id
              );
            SELECT RAISE(ABORT, 'message delivery binding is immutable')
            WHERE old.delivery_id IS NOT NULL
              AND old.delivery_id IS NOT new.delivery_id;
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_guard_bi
        BEFORE INSERT ON message_deliveries
        BEGIN
            SELECT RAISE(ABORT, 'message delivery identity collision')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries
                WHERE id = new.id
                   OR (
                       project_id = new.project_id
                       AND project_generation_snapshot = new.project_generation_snapshot
                       AND actor_kind = new.actor_kind
                       AND actor_id = new.actor_id
                       AND actor_generation_snapshot IS new.actor_generation_snapshot
                       AND actor_project_generation_snapshot
                           IS new.actor_project_generation_snapshot
                       AND idempotency_key = new.idempotency_key
                   )
            );
            SELECT RAISE(ABORT, 'message delivery must start pending')
            WHERE new.state != 'pending';
            SELECT RAISE(ABORT, 'message delivery project snapshot mismatch')
            WHERE NOT EXISTS (
                SELECT 1
                FROM projects
                WHERE id = new.project_id
                  AND slug = new.project_slug_snapshot
                  AND project_generation = new.project_generation_snapshot
                  AND archived_at IS NULL
            );
            SELECT RAISE(ABORT, 'message delivery sender snapshot mismatch')
            WHERE NOT EXISTS (
                SELECT 1
                FROM agents
                WHERE id = new.sender_id
                  AND project_id = new.sender_project_id_snapshot
                  AND name = new.sender_name_snapshot
                  AND agent_generation = new.sender_generation_snapshot
                  AND retired_at IS NULL
            );
            SELECT RAISE(ABORT, 'message delivery sender project snapshot mismatch')
            WHERE NOT EXISTS (
                SELECT 1
                FROM projects
                WHERE id = new.sender_project_id_snapshot
                  AND slug = new.sender_project_slug_snapshot
                  AND project_generation = new.sender_project_generation_snapshot
                  AND archived_at IS NULL
            );
            SELECT RAISE(ABORT, 'message delivery agent actor snapshot mismatch')
            WHERE new.actor_kind = 'agent'
              AND NOT EXISTS (
                  SELECT 1
                  FROM agents
                  WHERE id = new.actor_id
                    AND project_id = new.actor_project_id_snapshot
                    AND name = new.actor_name_snapshot
                    AND agent_generation = new.actor_generation_snapshot
                    AND retired_at IS NULL
              );
            SELECT RAISE(ABORT, 'message delivery agent actor project snapshot mismatch')
            WHERE new.actor_kind = 'agent'
              AND NOT EXISTS (
                  SELECT 1
                  FROM projects
                  WHERE id = new.actor_project_id_snapshot
                    AND slug = new.actor_project_slug_snapshot
                    AND project_generation = new.actor_project_generation_snapshot
                    AND archived_at IS NULL
              );
            SELECT RAISE(ABORT, 'message delivery user actor project snapshot mismatch')
            WHERE new.actor_kind = 'ui_user'
              AND NOT EXISTS (
                  SELECT 1
                  FROM projects
                  WHERE id = new.actor_project_id_snapshot
                    AND slug = new.actor_project_slug_snapshot
                    AND project_generation = new.actor_project_generation_snapshot
                    AND archived_at IS NULL
              );
            SELECT RAISE(ABORT, 'message delivery user actor snapshot mismatch')
            WHERE new.actor_kind = 'ui_user'
              AND NOT EXISTS (
                  SELECT 1
                  FROM ui_users
                  WHERE id = new.actor_id
                    AND username = new.actor_name_snapshot
                    AND session_generation = new.actor_generation_snapshot
                    AND session_epoch = new.actor_epoch_snapshot
                    AND disabled = 0
              );
            SELECT RAISE(ABORT, 'message delivery user actor is not authorized')
            WHERE new.actor_kind = 'ui_user'
              AND (
                  new.delivery_kind = 'contact_request'
                  OR NOT EXISTS (
                      SELECT 1
                      FROM ui_users AS actor
                      WHERE actor.id = new.actor_id
                        AND actor.disabled = 0
                        AND new.delivery_kind IN ('message', 'reply')
                        AND (
                            actor.role = 'admin'
                            OR (
                                actor.role = 'member'
                                AND EXISTS (
                                    SELECT 1
                                    FROM ui_project_assignments AS assignment
                                    WHERE assignment.user_id = actor.id
                                      AND assignment.project_id =
                                          new.actor_project_id_snapshot
                                      AND assignment.role = 'operator'
                                )
                            )
                        )
                  )
              );
            SELECT RAISE(ABORT, 'message delivery reply target mismatch')
            WHERE new.reply_to_message_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM messages
                  WHERE id = new.reply_to_message_id
                    AND project_id = new.project_id
              );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_snapshots_bu
        BEFORE UPDATE ON message_deliveries
        WHEN old.id IS NOT new.id
          OR old.delivery_kind IS NOT new.delivery_kind
          OR old.project_id IS NOT new.project_id
          OR old.project_slug_snapshot IS NOT new.project_slug_snapshot
          OR old.project_generation_snapshot IS NOT new.project_generation_snapshot
          OR old.sender_project_id_snapshot IS NOT new.sender_project_id_snapshot
          OR old.sender_project_slug_snapshot IS NOT new.sender_project_slug_snapshot
          OR old.sender_project_generation_snapshot IS NOT new.sender_project_generation_snapshot
          OR old.sender_id IS NOT new.sender_id
          OR old.sender_name_snapshot IS NOT new.sender_name_snapshot
          OR old.sender_generation_snapshot IS NOT new.sender_generation_snapshot
          OR old.actor_kind IS NOT new.actor_kind
          OR old.actor_id IS NOT new.actor_id
          OR old.actor_name_snapshot IS NOT new.actor_name_snapshot
          OR old.actor_project_id_snapshot IS NOT new.actor_project_id_snapshot
          OR old.actor_project_slug_snapshot IS NOT new.actor_project_slug_snapshot
          OR old.actor_project_generation_snapshot IS NOT new.actor_project_generation_snapshot
          OR old.actor_generation_snapshot IS NOT new.actor_generation_snapshot
          OR old.actor_epoch_snapshot IS NOT new.actor_epoch_snapshot
          OR old.idempotency_key IS NOT new.idempotency_key
          OR old.request_sha256 IS NOT new.request_sha256
          OR old.thread_id IS NOT new.thread_id
          OR old.reply_to_message_id IS NOT new.reply_to_message_id
          OR old.topic IS NOT new.topic
          OR old.subject IS NOT new.subject
          OR old.body_md IS NOT new.body_md
          OR old.importance IS NOT new.importance
          OR old.ack_required IS NOT new.ack_required
          OR old.attachments IS NOT new.attachments
          OR old.archive_document IS NOT new.archive_document
          OR old.document_sha256 IS NOT new.document_sha256
          OR old.created_ts IS NOT new.created_ts
        BEGIN
            SELECT RAISE(ABORT, 'message delivery snapshots are immutable');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_receipt_bu
        BEFORE UPDATE OF archive_relative_path, archive_blob_sha, archive_commit_sha
        ON message_deliveries
        WHEN old.archive_relative_path IS NOT NULL
          AND (
              old.archive_relative_path IS NOT new.archive_relative_path
              OR old.archive_blob_sha IS NOT new.archive_blob_sha
              OR old.archive_commit_sha IS NOT new.archive_commit_sha
          )
        BEGIN
            SELECT RAISE(ABORT, 'message delivery receipt is immutable');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_lease_fence_bu
        BEFORE UPDATE ON message_deliveries
        BEGIN
            SELECT RAISE(ABORT, 'message delivery lease fence must advance on acquisition')
            WHERE old.lease_token IS NOT new.lease_token
              AND new.lease_token IS NOT NULL
              AND new.lease_fence != old.lease_fence + 1;
            SELECT RAISE(ABORT, 'message delivery lease fence changed without acquisition')
            WHERE old.lease_token IS new.lease_token
              AND new.lease_fence != old.lease_fence;
            SELECT RAISE(ABORT, 'message delivery lease fence changed on release')
            WHERE old.lease_token IS NOT NULL
              AND new.lease_token IS NULL
              AND new.lease_fence != old.lease_fence;
            SELECT RAISE(ABORT, 'message delivery attempt count is not monotonic')
            WHERE new.attempt_count < old.attempt_count
               OR new.attempt_count > old.attempt_count + 1;
            SELECT RAISE(ABORT, 'message delivery attempt must advance on acquisition')
            WHERE old.lease_token IS NOT new.lease_token
              AND new.lease_token IS NOT NULL
              AND new.attempt_count != old.attempt_count + 1;
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_terminal_bu
        BEFORE UPDATE ON message_deliveries
        WHEN old.state IN ('published', 'quarantined')
        BEGIN
            SELECT RAISE(ABORT, 'terminal message delivery is immutable');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_transition_bu
        BEFORE UPDATE OF state ON message_deliveries
        WHEN old.state = 'pending'
        BEGIN
            SELECT RAISE(ABORT, 'illegal message delivery state transition')
            WHERE new.state NOT IN ('pending', 'published', 'quarantined');
            SELECT RAISE(ABORT, 'published delivery requires ordered recipients')
            WHERE new.state = 'published'
              AND (
                  NOT EXISTS (
                      SELECT 1
                      FROM message_delivery_recipients
                      WHERE delivery_id = old.id
                  )
                  OR (
                      SELECT min(ordinal)
                      FROM message_delivery_recipients
                      WHERE delivery_id = old.id
                  ) != 0
                  OR (
                      SELECT max(ordinal)
                      FROM message_delivery_recipients
                      WHERE delivery_id = old.id
                  ) + 1 != (
                      SELECT count(*)
                      FROM message_delivery_recipients
                      WHERE delivery_id = old.id
                  )
              );
            SELECT RAISE(ABORT, 'published delivery message snapshot mismatch')
            WHERE new.state = 'published'
              AND NOT EXISTS (
                  SELECT 1
                  FROM messages
                  WHERE id = new.message_id
                    AND delivery_id = old.id
                    AND project_id = old.project_id
                    AND sender_id = old.sender_id
                    AND thread_id IS old.thread_id
                    AND reply_to IS old.reply_to_message_id
                    AND topic IS old.topic
                    AND subject = old.subject
                    AND body_md = old.body_md
                    AND importance = old.importance
                    AND ack_required = old.ack_required
                    AND attachments = old.attachments
                    AND created_ts = old.created_ts
              );
            SELECT RAISE(ABORT, 'published delivery recipient snapshot mismatch')
            WHERE new.state = 'published'
              AND (
                  (
                      SELECT count(*)
                      FROM message_recipients
                      WHERE message_id = new.message_id
                  ) != (
                      SELECT count(*)
                      FROM message_delivery_recipients
                      WHERE delivery_id = old.id
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM message_delivery_recipients AS delivery_recipient
                      WHERE delivery_recipient.delivery_id = old.id
                        AND NOT EXISTS (
                            SELECT 1
                            FROM message_recipients AS message_recipient
                            WHERE message_recipient.message_id = new.message_id
                              AND message_recipient.agent_id = delivery_recipient.agent_id
                              AND message_recipient.kind = delivery_recipient.kind
                        )
                  )
              );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_immutable_bd
        BEFORE DELETE ON message_deliveries
        BEGIN
            SELECT RAISE(ABORT, 'message deliveries are immutable');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_delivery_recipients_guard_bi
        BEFORE INSERT ON message_delivery_recipients
        BEGIN
            SELECT RAISE(ABORT, 'message delivery recipient identity collision')
            WHERE EXISTS (
                SELECT 1
                FROM message_delivery_recipients
                WHERE delivery_id = new.delivery_id
                  AND (ordinal = new.ordinal OR agent_id = new.agent_id)
            );
            SELECT RAISE(ABORT, 'message delivery recipient parent is not pending')
            WHERE NOT EXISTS (
                SELECT 1
                FROM message_deliveries
                WHERE id = new.delivery_id
                  AND state = 'pending'
                  AND project_id = new.project_id_snapshot
            );
            SELECT RAISE(ABORT, 'message delivery recipient snapshot mismatch')
            WHERE NOT EXISTS (
                SELECT 1
                FROM agents
                WHERE id = new.agent_id
                  AND project_id = new.project_id_snapshot
                  AND name = new.agent_name_snapshot
                  AND agent_generation = new.agent_generation_snapshot
                  AND retired_at IS NULL
                  AND contact_policy != 'block_all'
            );
            SELECT RAISE(ABORT, 'contact request requires exactly one to recipient')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries AS delivery
                WHERE delivery.id = new.delivery_id
                  AND delivery.delivery_kind = 'contact_request'
                  AND (
                      new.kind != 'to'
                      OR EXISTS (
                          SELECT 1
                          FROM message_delivery_recipients AS existing_recipient
                          WHERE existing_recipient.delivery_id = delivery.id
                      )
                  )
            );
            SELECT RAISE(ABORT, 'message delivery recipient route is not approved')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries AS delivery
                WHERE delivery.id = new.delivery_id
                  AND delivery.delivery_kind = 'message'
                  AND delivery.sender_project_id_snapshot != new.project_id_snapshot
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_links AS link
                      WHERE link.a_project_id = delivery.sender_project_id_snapshot
                        AND link.a_agent_id = delivery.sender_id
                        AND link.b_project_id = new.project_id_snapshot
                        AND link.b_agent_id = new.agent_id
                        AND link.status = 'approved'
                        AND (link.expires_ts IS NULL OR link.expires_ts > CURRENT_TIMESTAMP)
                  )
            );
            SELECT RAISE(ABORT, 'contact request recipient route is not pending')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries AS delivery
                WHERE delivery.id = new.delivery_id
                  AND delivery.delivery_kind = 'contact_request'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_links AS link
                      WHERE link.a_project_id = delivery.sender_project_id_snapshot
                        AND link.a_agent_id = delivery.sender_id
                        AND link.b_project_id = new.project_id_snapshot
                        AND link.b_agent_id = new.agent_id
                        AND link.status = 'pending'
                        AND (link.expires_ts IS NULL OR link.expires_ts > CURRENT_TIMESTAMP)
                  )
            );
            SELECT RAISE(ABORT, 'reply recipient route is not approved')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries AS delivery
                WHERE delivery.id = new.delivery_id
                  AND delivery.delivery_kind = 'reply'
                  AND delivery.sender_project_id_snapshot != new.project_id_snapshot
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_links AS link
                      WHERE link.a_project_id = delivery.sender_project_id_snapshot
                        AND link.a_agent_id = delivery.sender_id
                        AND link.b_project_id = new.project_id_snapshot
                        AND link.b_agent_id = new.agent_id
                        AND link.status = 'approved'
                        AND (link.expires_ts IS NULL OR link.expires_ts > CURRENT_TIMESTAMP)
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM messages AS inbound
                      LEFT JOIN message_recipients AS inbound_recipient
                        ON inbound_recipient.message_id = inbound.id
                      WHERE inbound.project_id = delivery.sender_project_id_snapshot
                        AND inbound.sender_id = new.agent_id
                        AND (
                            delivery.actor_kind = 'ui_user'
                            OR inbound_recipient.agent_id = delivery.sender_id
                        )
                        AND (
                            inbound.thread_id = delivery.thread_id
                            OR (
                                inbound.thread_id IS NULL
                                AND CAST(inbound.id AS TEXT) = delivery.thread_id
                            )
                        )
                  )
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_delivery_recipients_immutable_bu
        BEFORE UPDATE ON message_delivery_recipients
        BEGIN
            SELECT RAISE(ABORT, 'message delivery recipients are immutable');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_delivery_recipients_immutable_bd
        BEFORE DELETE ON message_delivery_recipients
        BEGIN
            SELECT RAISE(ABORT, 'message delivery recipients are immutable');
        END
        """
    )
    connection.exec_driver_sql(_MESSAGE_DELIVERIES_PROJECT_GUARD_BD_SQL)
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_project_guard_bu
        BEFORE UPDATE OF id, slug ON projects
        WHEN old.id IS NOT new.id OR old.slug IS NOT new.slug
        BEGIN
            SELECT RAISE(ABORT, 'project has immutable message delivery history')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries
                WHERE (
                      project_id = old.id
                      OR sender_project_id_snapshot = old.id
                      OR (
                          actor_kind = 'agent'
                          AND actor_project_id_snapshot = old.id
                      )
                  )
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_agent_pending_bd
        BEFORE DELETE ON agents
        BEGIN
            SELECT RAISE(ABORT, 'agent has pending message delivery')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries
                WHERE state = 'pending'
                  AND (
                      sender_id = old.id
                      OR (actor_kind = 'agent' AND actor_id = old.id)
                      OR EXISTS (
                          SELECT 1
                          FROM message_delivery_recipients
                          WHERE delivery_id = message_deliveries.id
                            AND agent_id = old.id
                      )
                  )
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_agent_pending_bu
        BEFORE UPDATE OF id, project_id ON agents
        WHEN old.id IS NOT new.id OR old.project_id IS NOT new.project_id
        BEGIN
            SELECT RAISE(ABORT, 'agent has pending message delivery')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries
                WHERE state = 'pending'
                  AND (
                      sender_id = old.id
                      OR (actor_kind = 'agent' AND actor_id = old.id)
                      OR EXISTS (
                          SELECT 1
                          FROM message_delivery_recipients
                          WHERE delivery_id = message_deliveries.id
                            AND agent_id = old.id
                      )
                  )
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_ui_user_pending_bd
        BEFORE DELETE ON ui_users
        BEGIN
            SELECT RAISE(ABORT, 'user has pending message delivery')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries
                WHERE state = 'pending'
                  AND actor_kind = 'ui_user'
                  AND actor_id = old.id
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_ui_user_pending_bu
        BEFORE UPDATE OF id ON ui_users
        WHEN old.id IS NOT new.id
        BEGIN
            SELECT RAISE(ABORT, 'user has pending message delivery')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries
                WHERE state = 'pending'
                  AND actor_kind = 'ui_user'
                  AND actor_id = old.id
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_reply_target_pending_bd
        BEFORE DELETE ON messages
        BEGIN
            SELECT RAISE(ABORT, 'message is a pending delivery reply target')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries
                WHERE state = 'pending'
                  AND reply_to_message_id = old.id
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_reply_target_pending_bu
        BEFORE UPDATE OF id, project_id ON messages
        WHEN old.id IS NOT new.id OR old.project_id IS NOT new.project_id
        BEGIN
            SELECT RAISE(ABORT, 'message is a pending delivery reply target')
            WHERE EXISTS (
                SELECT 1
                FROM message_deliveries
                WHERE state = 'pending'
                  AND reply_to_message_id = old.id
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS message_deliveries_reply_target_pending_bi
        BEFORE INSERT ON messages
        BEGIN
            SELECT RAISE(ABORT, 'message is a pending delivery reply target')
            WHERE EXISTS (
                SELECT 1
                FROM messages
                JOIN message_deliveries
                  ON message_deliveries.reply_to_message_id = messages.id
                WHERE messages.id = new.id
                  AND message_deliveries.state = 'pending'
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_project_assignments_user_ad
        AFTER DELETE ON ui_users
        BEGIN
            DELETE FROM ui_project_assignments WHERE user_id = old.id;
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_project_assignments_parent_bi
        BEFORE INSERT ON ui_project_assignments
        BEGIN
            SELECT RAISE(ABORT, 'ui_project_assignments user does not exist')
            WHERE NOT EXISTS (SELECT 1 FROM ui_users WHERE id = new.user_id);
            SELECT RAISE(ABORT, 'ui_project_assignments project does not exist')
            WHERE NOT EXISTS (SELECT 1 FROM projects WHERE id = new.project_id);
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_project_assignments_parent_bu
        BEFORE UPDATE OF user_id, project_id ON ui_project_assignments
        BEGIN
            SELECT RAISE(ABORT, 'ui_project_assignments user does not exist')
            WHERE NOT EXISTS (SELECT 1 FROM ui_users WHERE id = new.user_id);
            SELECT RAISE(ABORT, 'ui_project_assignments project does not exist')
            WHERE NOT EXISTS (SELECT 1 FROM projects WHERE id = new.project_id);
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_project_assignments_project_ad
        AFTER DELETE ON projects
        BEGIN
            DELETE FROM ui_project_assignments WHERE project_id = old.id;
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_access_audit_events_guard_bi
        BEFORE INSERT ON ui_access_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'invalid old project role')
            WHERE new.old_role IS NOT NULL
              AND new.old_role NOT IN ('viewer', 'operator');
            SELECT RAISE(ABORT, 'invalid new project role')
            WHERE new.new_role IS NOT NULL
              AND new.new_role NOT IN ('viewer', 'operator');
            SELECT RAISE(ABORT, 'access audit requires an effective change')
            WHERE new.old_role IS new.new_role;
            SELECT RAISE(ABORT, 'invalid access epoch step')
            WHERE new.target_epoch_after != new.target_epoch_before + 1;
            SELECT RAISE(ABORT, 'invalid target account generation snapshot')
            WHERE length(new.target_account_generation) != 64;
            SELECT RAISE(ABORT, 'invalid project generation snapshot')
            WHERE length(new.project_generation_snapshot) != 64;
            SELECT RAISE(ABORT, 'invalid actor provenance snapshot')
            WHERE NOT (
                (
                    new.actor_user_id IS NULL
                    AND new.actor_account_generation_snapshot IS NULL
                    AND new.actor_session_epoch_snapshot IS NULL
                )
                OR (
                    new.actor_user_id IS NOT NULL
                    AND length(new.actor_account_generation_snapshot) = 64
                    AND new.actor_session_epoch_snapshot >= 1
                )
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_access_audit_events_immutable_bi
        BEFORE INSERT ON ui_access_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'ui_access_audit_events id collision is immutable')
            WHERE EXISTS (
                SELECT 1
                FROM ui_access_audit_events
                WHERE id = new.id
            );
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_access_audit_events_immutable_bu
        BEFORE UPDATE ON ui_access_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'ui_access_audit_events are immutable');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER IF NOT EXISTS ui_access_audit_events_immutable_bd
        BEFORE DELETE ON ui_access_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'ui_access_audit_events are immutable');
        END
        """
    )
    if validate_execution_schema:
        _validate_agent_execution_schema(connection)


def get_database_path(settings: Settings | None = None) -> Path | None:
    """Extract the filesystem path to the SQLite database file from settings.

    Args:
        settings: Application settings, or None to use global settings

    Returns:
        Path to the database file, or None if not using SQLite or path cannot be determined
    """
    resolved = settings or get_settings()
    url_raw = resolved.database.url

    try:
        from sqlalchemy.engine import make_url

        parsed = make_url(url_raw)
    except Exception:
        return None

    if parsed.get_backend_name() != "sqlite":
        return None

    db_path = parsed.database
    if not db_path or db_path == ":memory:":
        return None

    return Path(db_path)


def connect_sqlite_readonly(
    db_path: Path,
    *,
    busy_timeout_ms: int = 60_000,
) -> sqlite3.Connection:
    """Open a database that another process may be writing, without writing to it.

    Every tool that reads the live database from a SECOND process has to use
    this. A read-write connection there is what completes the corruption chain
    measured on 2026-08-14: it can checkpoint the WAL and unlink `-wal`/`-shm`
    when it closes, and if the server has meanwhile lost its own POSIX locks it
    will do exactly that underneath a running server.

    `mode=ro` and not `immutable=1`: immutable promises SQLite the file cannot
    change and it then skips locking entirely, which is true of a snapshot and
    false of a live database -- SQLite's own documentation says incorrect
    results and SQLITE_CORRUPT may follow if the file does change. `query_only`
    is belt and braces: it turns an accidental write into an error here rather
    than a surprise for whoever is writing.
    """
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        connection.execute("PRAGMA query_only=ON")
    except Exception:
        connection.close()
        raise
    return connection


def get_sqlite_sidecar_paths(db_path: Path) -> tuple[Path, Path]:
    """Return the WAL and SHM sidecar paths for a SQLite database file."""
    return (
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    )


def get_sqlite_pre_restore_path(db_path: Path) -> Path:
    """Return the safety-backup path used before overwriting a SQLite database."""
    return db_path.with_name(f"{db_path.name}.pre-restore")
