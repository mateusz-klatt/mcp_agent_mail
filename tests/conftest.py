import contextlib
from pathlib import Path

import psutil
import pytest

from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.db import reset_database_state
from mcp_agent_mail.http import clear_jwks_cache
from mcp_agent_mail.storage import clear_repo_cache

# CPU overload threshold - skip benchmark tests if ALL cores are at this level
CPU_OVERLOAD_THRESHOLD = 95.0


def is_cpu_overloaded() -> bool:
    """Check if all CPU cores are at 95%+ utilization.

    Returns True only when the system is under extreme load (all cores saturated),
    which would make timing-based benchmark tests unreliable.
    """
    # Sample CPU usage over 200ms per-core
    per_cpu = psutil.cpu_percent(interval=0.2, percpu=True)
    if not per_cpu:
        return False

    overloaded = sum(1 for usage in per_cpu if usage >= CPU_OVERLOAD_THRESHOLD)
    return overloaded == len(per_cpu)


def skip_if_cpu_overloaded() -> None:
    """Skip the current test if all CPU cores are at 95%+ utilization.

    Use this at the start of any test that asserts on wall-clock time.
    Prevents flaky benchmark tests when the system is under extreme load.
    """
    if is_cpu_overloaded():
        cores = psutil.cpu_count()
        pytest.skip(
            f"Skipping benchmark: system under extreme CPU load "
            f"(all {cores} cores at {CPU_OVERLOAD_THRESHOLD}%+ utilization)"
        )


@pytest.fixture
def open_mail_ui_gate(isolated_env, monkeypatch):
    """Stand the ``/mail`` password gate aside for tests that assert view *content*.

    Deliberately a named fixture rather than a line in :func:`isolated_env`, and
    deliberately not a bearer header.

    The gate refuses every ``/mail`` route with 503 when ``MAIL_UI_AUTH_ENABLED``
    is on and no session secret is configured — which is the default, so tests
    written before the gate existed all fail on their first request. Three ways
    out, and only this one is honest:

    - a server bearer makes the routes answer 200, because the gate hands API
      clients to the bearer middleware untouched. The tests would pass while
      *bypassing* the thing they appear to authenticate against.
    - setting only the session secret moves the failure from 503 to 401. The
      count does not change, so it reads as "no progress" when in fact the gate
      has started working.
    - switching the gate off says so in the configuration, where a reader can
      see it.

    Global would have been shorter and is the wrong shape: ``_build`` in
    tests/test_mail_ui_auth.py sets ``MAIL_UI_SESSION_SECRET`` per case but does
    not pin ``MAIL_UI_AUTH_ENABLED``, so putting this in ``isolated_env`` would
    reach under the tests that exist to prove the gate works. Measured, running
    that suite with the variable forced off:

        4 failed, 4 passed

    So it would have broken loudly rather than quietly — which is the opposite
    of what this comment first claimed, and better news than it assumed. The
    four that still pass are the ones that would have been hollowed out: they
    assert the same thing whether the gate is on or off. Keeping this fixture
    named and scoped costs nothing and removes the question.

    Depends on ``isolated_env`` so it runs after it: that fixture clears the
    settings cache, and a value set before the clear would not survive it.
    """
    monkeypatch.setenv("MAIL_UI_AUTH_ENABLED", "false")
    clear_settings_cache()
    yield


@pytest.fixture(autouse=True)
def deterministic_git_environment(monkeypatch):
    """Keep test commits independent of the developer's global Git config."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test-agent")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "test-agent")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Provide isolated database settings for tests and reset caches."""
    db_path: Path = tmp_path / "test.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8765")
    monkeypatch.setenv("HTTP_PATH", "/mcp/")
    # Empty values shadow both ambient process configuration and the real .env;
    # delenv() would let python-decouple fall through to that file. OAuth stays
    # disabled below, while every related value and limiter knob starts from
    # its compile-time default regardless of the machine running the suite.
    for variable in (
        "HTTP_OAUTH_ACCESS_TOKEN_TTL_SECONDS",
        "HTTP_OAUTH_ALLOWED_CLIENT_REDIRECT_URIS",
        "HTTP_OAUTH_BASE_URL",
        "HTTP_OAUTH_DCR_RATE_LIMIT_PER_MINUTE",
        "HTTP_OAUTH_GITHUB_ALLOWED_IDENTITIES",
        "HTTP_OAUTH_GITHUB_CLIENT_ID",
        "HTTP_OAUTH_GITHUB_CLIENT_SECRET",
        "HTTP_OAUTH_GITHUB_TOKEN_CACHE_TTL_SECONDS",
        "HTTP_OAUTH_JWT_SIGNING_KEY",
        "HTTP_OAUTH_RBAC_ROLE",
        "HTTP_OAUTH_STORAGE_PATH",
        "HTTP_RATE_LIMIT_BACKEND",
        "HTTP_RATE_LIMIT_ENABLED",
        "HTTP_RATE_LIMIT_PER_MINUTE",
        "HTTP_RATE_LIMIT_REDIS_URL",
        "HTTP_RATE_LIMIT_RESOURCES_BURST",
        "HTTP_RATE_LIMIT_RESOURCES_PER_MINUTE",
        "HTTP_RATE_LIMIT_TOOLS_BURST",
        "HTTP_RATE_LIMIT_TOOLS_PER_MINUTE",
    ):
        monkeypatch.setenv(variable, "")
    monkeypatch.setenv("HTTP_OAUTH_ENABLED", "false")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    # An empty process value deliberately shadows any deploy provenance in the
    # repository's real .env. delenv() would let python-decouple fall through
    # to that file and make generic tests depend on the local production build.
    monkeypatch.setenv("MCP_AGENT_MAIL_BUILD_COMMIT", "")
    storage_root = tmp_path / "storage"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "128")
    clear_settings_cache()
    reset_database_state()
    # Clear process-wide caches before the test so reused URLs/paths cannot
    # inherit resources created by an earlier test.
    clear_repo_cache()
    clear_jwks_cache()
    try:
        yield
    finally:
        # Every process-wide resource has an explicit close/reset operation.
        # Do not scan the entire GC heap: besides being expensive, that used to
        # close objects still owned by unrelated fixtures.
        clear_repo_cache()
        clear_jwks_cache()
        clear_settings_cache()

        if db_path.exists():
            # Windows refuses to unlink a file another handle still holds, and
            # raises out of teardown — turning a test's own result into an
            # error and losing it. POSIX unlinks the entry regardless, which is
            # why this was invisible until the Windows suite got far enough to
            # open a database at all. tmp_path is pytest's to clean up either
            # way, so failing to remove it here costs nothing; failing loudly
            # costs the result of the test that just ran.
            with contextlib.suppress(PermissionError):
                db_path.unlink()
        storage_root = tmp_path / "storage"
        if storage_root.exists():
            # Git object files can be read-only on Windows. This eager cleanup is
            # only an optimization; ``tmp_path`` owns eventual removal, so a
            # platform-level refusal must not replace the test's actual result.
            with contextlib.suppress(OSError):
                for path in storage_root.rglob("*"):
                    if path.is_file():
                        path.unlink()
                for path in sorted(storage_root.rglob("*"), reverse=True):
                    if path.is_dir():
                        path.rmdir()
                if storage_root.exists():
                    storage_root.rmdir()


def _clear_process_resources() -> None:
    """Release cached repositories, database pools, and configuration state."""
    with contextlib.suppress(Exception):
        clear_repo_cache()
    with contextlib.suppress(Exception):
        reset_database_state()
    with contextlib.suppress(Exception):
        clear_settings_cache()
    with contextlib.suppress(Exception):
        clear_jwks_cache()


def pytest_runtest_setup() -> None:
    """Release resources left by the fully torn-down preceding test."""
    _clear_process_resources()


def pytest_sessionfinish() -> None:
    """Release resources after the final test and its asyncio runner finish."""
    _clear_process_resources()
