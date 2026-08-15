"""One storage root, one server: the CLI's ownership lock (issue #123).

Ownership here is an OS-level lock on ``server.lock``, never a marker file and
never a liveness guess, so every test below plants a *real* competing lock
rather than a stale artefact. ``server.pid`` is diagnostics: it names the owner
in the refusal text and nothing keys off it.

The two transports are exercised through the same three properties -- the lock
is held for the whole run, it is surrendered however the run ends, and a root
already owned is refused without the transport ever starting -- so they are
parametrised rather than written twice.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from os import getpid
from pathlib import Path
from typing import Any

import pytest
from filelock import FileLock, Timeout
from typer.testing import CliRunner

from mcp_agent_mail.cli import _SERVER_LOCK_FILENAME, _acquire_server_lock, app
from mcp_agent_mail.config import clear_settings_cache

PID_FILENAME = "server.pid"

# Called with the keyword arguments the CLI handed the transport it started.
TransportBody = Callable[[dict[str, Any]], None]


def _private_storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """Point the process-wide settings at a root of this test's own, and return it.

    The directory is deliberately left absent: creating the root is part of what
    acquisition promises, and a pre-made directory would hide a regression there.
    """
    root = tmp_path / name
    monkeypatch.setenv("STORAGE_ROOT", str(root))
    clear_settings_cache()
    return root


@contextmanager
def _root_owned_elsewhere(root: Path, owner_pid: str) -> Iterator[Path]:
    """Hold ``root`` exactly as a server in another process would, and yield the lock path."""
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / _SERVER_LOCK_FILENAME
    incumbent = FileLock(str(lock_path))
    incumbent.acquire(timeout=0)
    (root / PID_FILENAME).write_text(owner_pid, encoding="utf-8")
    try:
        yield lock_path
    finally:
        incumbent.release()


def _assert_lock_is_available(lock_path: Path) -> None:
    """Prove that no prior command frame still owns the OS-level lock."""
    probe = FileLock(str(lock_path))
    try:
        probe.acquire(timeout=0)
    finally:
        probe.release()


def _assert_lock_is_held(lock_path: Path) -> None:
    """Prove the lock is still owned by somebody -- the negative of the above."""
    probe = FileLock(str(lock_path))
    with pytest.raises(Timeout):
        probe.acquire(timeout=0)


def _stub_http_transport(monkeypatch: pytest.MonkeyPatch, body: TransportBody) -> None:
    """Replace the uvicorn server ``serve-http`` would run with ``body``."""

    def stand_in_for_uvicorn(asgi_app: Any, **kwargs: Any) -> None:
        body(kwargs)

    monkeypatch.setattr("uvicorn.run", stand_in_for_uvicorn)


def _stub_stdio_transport(monkeypatch: pytest.MonkeyPatch, body: TransportBody) -> None:
    """Replace the FastMCP run loop ``serve-stdio`` would enter with ``body``."""
    from fastmcp import FastMCP

    def fake_run(self: Any, transport: str = "stdio", **kwargs: Any) -> None:
        body({"transport": transport, **kwargs})

    monkeypatch.setattr(FastMCP, "run", fake_run)


TRANSPORTS = [
    pytest.param("serve-http", _stub_http_transport, id="http"),
    pytest.param("serve-stdio", _stub_stdio_transport, id="stdio"),
]


def test_acquisition_creates_the_root_the_lockfile_and_the_owner_pid(isolated_env, tmp_path, monkeypatch):
    """A first acquisition builds the root, takes the lock, and records this PID."""
    root = _private_storage_root(tmp_path, monkeypatch, "fresh")
    assert not root.exists()

    lock = _acquire_server_lock()
    try:
        assert (root / _SERVER_LOCK_FILENAME).exists()
        recorded = (root / PID_FILENAME).read_text(encoding="utf-8").strip()
        assert recorded == str(getpid())
    finally:
        lock.release()


def test_acquisition_refuses_an_owned_root_and_leaves_the_owner_holding_it(isolated_env, tmp_path, monkeypatch):
    """Refusal is SystemExit(1), and it must not disturb the incumbent's lock."""
    root = _private_storage_root(tmp_path, monkeypatch, "owned")

    with _root_owned_elsewhere(root, "99999") as lock_path:
        with pytest.raises(SystemExit) as refusal:
            _acquire_server_lock()
        assert refusal.value.code == 1
        _assert_lock_is_held(lock_path)


def test_refusal_names_the_recorded_owner_on_stderr(isolated_env, tmp_path, monkeypatch, capsys):
    """The refusal text carries the PID from server.pid so an operator can act on it."""
    root = _private_storage_root(tmp_path, monkeypatch, "diagnosable")

    with _root_owned_elsewhere(root, "42"):
        with pytest.raises(SystemExit):
            _acquire_server_lock()
        complaint = capsys.readouterr().err

    assert "42" in complaint
    assert "Another Agent Mail server is already running" in complaint


def test_a_released_root_can_be_owned_again(isolated_env, tmp_path, monkeypatch):
    """Releasing hands the root to the next starter; the lockfile itself blocks nothing."""
    root = _private_storage_root(tmp_path, monkeypatch, "recycled")

    first = _acquire_server_lock()
    first.release()

    # No cache clearing between the two: the root has not moved, so this is the
    # same settings object taking the same lock a second time.
    second = _acquire_server_lock()
    try:
        assert (root / _SERVER_LOCK_FILENAME).exists()
    finally:
        second.release()


@pytest.mark.parametrize("command,install_transport", TRANSPORTS)
def test_serve_owns_the_root_for_the_whole_run_and_frees_it_after(
    command, install_transport, isolated_env, tmp_path, monkeypatch
):
    """The lock is already taken when the transport starts and gone when the command returns."""
    root = _private_storage_root(tmp_path, monkeypatch, f"{command}-running")
    started: list[dict[str, Any]] = []

    def observe_while_serving(kwargs: dict[str, Any]) -> None:
        assert (root / _SERVER_LOCK_FILENAME).exists(), "server.lock must exist while the server is running"
        started.append(kwargs)

    install_transport(monkeypatch, observe_while_serving)

    result = CliRunner().invoke(app, [command])

    assert result.exit_code == 0, result.output
    assert started, f"{command} returned success without ever starting a transport"
    _assert_lock_is_available(root / _SERVER_LOCK_FILENAME)


def test_serve_stdio_starts_the_stdio_transport(isolated_env, tmp_path, monkeypatch):
    """serve-stdio must select stdio explicitly; the run loop's default is not the contract."""
    _private_storage_root(tmp_path, monkeypatch, "stdio-transport")
    started: list[dict[str, Any]] = []

    _stub_stdio_transport(monkeypatch, started.append)
    result = CliRunner().invoke(app, ["serve-stdio"])

    assert result.exit_code == 0, result.output
    assert [call["transport"] for call in started] == ["stdio"]


@pytest.mark.parametrize("command,install_transport", TRANSPORTS)
def test_serve_frees_the_root_when_the_transport_raises(
    command, install_transport, isolated_env, tmp_path, monkeypatch
):
    """A crashing server surrenders the root; it does not leave it owned by a corpse."""
    root = _private_storage_root(tmp_path, monkeypatch, f"{command}-crashed")

    def explode(kwargs: dict[str, Any]) -> None:
        raise RuntimeError(f"simulated {command} failure")

    install_transport(monkeypatch, explode)

    result = CliRunner().invoke(app, [command])

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    _assert_lock_is_available(root / _SERVER_LOCK_FILENAME)


@pytest.mark.parametrize("command,install_transport", TRANSPORTS)
def test_serve_refuses_an_owned_root_without_starting_a_transport(
    command, install_transport, isolated_env, tmp_path, monkeypatch
):
    """Exit 1 before any transport binds anything -- the refusal must come first."""
    root = _private_storage_root(tmp_path, monkeypatch, f"{command}-blocked")
    started: list[dict[str, Any]] = []
    install_transport(monkeypatch, started.append)

    with _root_owned_elsewhere(root, "12345"):
        result = CliRunner().invoke(app, [command])

    assert result.exit_code == 1
    assert started == [], f"{command} started a transport on a root owned by another process"
