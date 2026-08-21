"""Behaviour of the ``am-run`` build wrapper and the ``amctl env`` helper.

``am-run`` has two authorities for a build slot and they must never blend:

* the **server** owns the slot whenever the MCP endpoint answers, and
* a **local lease file** under ``<archive>/build_slots/<slot>/`` owns it only
  when the endpoint is unreachable.

Most of what is pinned below is therefore negative -- what must *not* happen.
A local lease written after the server has already spoken would hand the same
slot to two builders, so every server-side failure mode gets a test that says
"and no local lease appeared". The same applies to the child process: it must
not start when the slot was refused, and the argv it was given must not reach
the console or the server, because build commands carry credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, cast

import click
import httpx
import pytest
import typer
from sqlalchemy import select
from typer.testing import CliRunner

import mcp_agent_mail.cli as cli_module
from mcp_agent_mail.app import _resolve_project_identity
from mcp_agent_mail.cli import (
    _build_slot_renew_interval_seconds,
    _canonical_project_path,
    _effective_build_slot_ttl_seconds,
    _resolve_repo_worktree_root,
    _safe_build_path_component,
    am_run,
    app,
)
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Project

pytestmark = pytest.mark.usefixtures("isolated_env")

# Handed back by the stub endpoint as the id of the execution it opened. The
# CLI must quote this value -- not the uuid4 it generated locally -- in every
# later call and in the child's environment.
SERVER_EXECUTION_ID = "3d5b1f7a-9c04-4e2b-8a61-77bd0c9e4f12"

# Wire constants. These are contract with the server and with build scripts,
# so they are written out rather than derived.
PROTOCOL_VERSION = 1
SLOT_TOOLS = ("acquire_build_slot", "renew_build_slot", "release_build_slot")

FAR_FUTURE = "2099-01-01T00:00:00+00:00"
LONG_PAST = "2020-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------
# Stub HTTP endpoint
# --------------------------------------------------------------------------


class RpcResult:
    """A 200 response whose ``structuredContent`` is ``payload``."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": "stub",
            "result": {"structuredContent": self._payload},
        }


class RpcError:
    """A 200 response carrying a JSON-RPC ``error`` member."""

    def __init__(self, message: str) -> None:
        self._message = message

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": "stub", "error": {"message": self._message}}


class UndecodableBody:
    """A 200 response whose body is not JSON at all."""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        raise ValueError("body is not JSON")


class HttpFailure:
    """A non-2xx response. Reading its body would be a bug, so it explodes."""

    def __init__(self, status_code: int) -> None:
        self._request = httpx.Request("POST", "http://stub.invalid/mcp")
        self._response = httpx.Response(status_code, request=self._request)

    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError(
            f"status {self._response.status_code}",
            request=self._request,
            response=self._response,
        )

    def json(self) -> dict[str, Any]:
        raise AssertionError("the body must not be decoded after an HTTP status failure")


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


class StubEndpoint:
    """In-process stand-in for the Agent Mail JSON-RPC endpoint.

    Records every ``tools/call`` and answers with a default success unless a
    per-tool reaction has been registered. A reaction may return a response
    object or raise -- raising ``httpx.ConnectError`` is how a mid-run
    transport failure is expressed.
    """

    def __init__(self, execution_id: str = SERVER_EXECUTION_ID) -> None:
        self.calls: list[ToolCall] = []
        self.execution_id = execution_id
        self._reactions: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def react(self, tool: str, reaction: Callable[[dict[str, Any]], Any]) -> StubEndpoint:
        self._reactions[tool] = reaction
        return self

    def answer(self, tool: str, response: Any) -> StubEndpoint:
        return self.react(tool, lambda _arguments: response)

    def fail(self, tool: str, exception: BaseException) -> StubEndpoint:
        def _raise(_arguments: dict[str, Any]) -> Any:
            raise exception

        return self.react(tool, _raise)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> StubEndpoint:
        endpoint = self

        def post(_client: Any, _url: str, json: dict[str, Any] | None = None, headers: Any = None) -> Any:
            return endpoint._dispatch(json or {})

        monkeypatch.setattr("httpx.Client.post", post)
        return self

    # -- inspection -------------------------------------------------------

    @property
    def tool_names(self) -> list[str]:
        return [call.name for call in self.calls]

    def called(self, tool: str) -> bool:
        return tool in self.tool_names

    def arguments_for(self, tool: str) -> dict[str, Any]:
        matches = [call.arguments for call in self.calls if call.name == tool]
        assert matches, f"{tool} was never called; saw {self.tool_names}"
        return matches[-1]

    def every_argument(self, tool: str, key: str) -> list[Any]:
        return [call.arguments.get(key) for call in self.calls if call.name == tool]

    # -- internals --------------------------------------------------------

    def _dispatch(self, request: dict[str, Any]) -> Any:
        params = request.get("params") or {}
        name = str(params.get("name") or "")
        arguments = dict(params.get("arguments") or {})
        self.calls.append(ToolCall(name=name, arguments=arguments))
        reaction = self._reactions.get(name)
        if reaction is not None:
            answered = reaction(arguments)
            if answered is not None:
                return answered
        return RpcResult(self._default_payload(name))

    def _default_payload(self, name: str) -> dict[str, Any]:
        if name == "start_agent_execution":
            return {"id": self.execution_id, "status": "active"}
        if name == "end_agent_execution":
            return {"already_ended": False}
        return {}


def unreachable_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every HTTP attempt fail the way a stopped server does."""

    def post(*_args: Any, **_kwargs: Any) -> Any:
        raise httpx.ConnectError("nothing is listening")

    monkeypatch.setattr("httpx.Client.post", post)


# --------------------------------------------------------------------------
# Stub child process
# --------------------------------------------------------------------------


@dataclass
class ChildRun:
    argv: list[str]
    env: dict[str, str]


class ChildStub:
    """Replaces ``subprocess.run`` and remembers how the child was invoked."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        while_running: Callable[[ChildRun], None] | None = None,
    ) -> None:
        self.runs: list[ChildRun] = []
        self.returncode = returncode
        self._while_running = while_running

    def install(self, monkeypatch: pytest.MonkeyPatch) -> ChildStub:
        def run(cmd: Any, env: Any = None, check: bool = False, **_kwargs: Any) -> Any:
            record = ChildRun(argv=[str(item) for item in cmd], env=dict(env or {}))
            self.runs.append(record)
            if self._while_running is not None:
                self._while_running(record)
            return SimpleNamespace(returncode=self.returncode)

        monkeypatch.setattr("subprocess.run", run)
        return self

    @property
    def started(self) -> bool:
        return bool(self.runs)

    @property
    def env(self) -> dict[str, str]:
        assert self.runs, "the child never started"
        return self.runs[-1].env


def forbid_child(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    """Install a child that fails the test if it is ever started."""

    def run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(reason)

    monkeypatch.setattr("subprocess.run", run)


# --------------------------------------------------------------------------
# Environment fixture
# --------------------------------------------------------------------------


@dataclass
class BuildSetup:
    """The directories and paths one ``am-run`` invocation will touch."""

    workdir: Path
    _storage: Path

    @property
    def storage_root(self) -> Path:
        return Path(get_settings().storage.root).expanduser().resolve()

    @property
    def project_key(self) -> str:
        """The human key ``am-run`` will resolve this working directory to."""
        return str(_resolve_repo_worktree_root(_canonical_project_path(self.workdir)))

    def archive_root(self, slug: str | None = None) -> Path:
        resolved = slug or _resolve_project_identity(self.project_key)["slug"]
        return self.storage_root / "projects" / resolved

    def leases(self, slot: str | None = None) -> list[Path]:
        pattern = f"projects/*/build_slots/{slot or '*'}/*.json"
        return sorted(self.storage_root.glob(pattern))

    def every_lease(self) -> list[Path]:
        return sorted(self.storage_root.glob("projects/*/build_slots/**/*.json"))


@pytest.fixture
def build_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., BuildSetup]:
    """Configure ``am-run``'s environment and hand back the paths it will use."""

    def configure(
        *,
        worktrees: bool = True,
        guard_mode: str = "block",
        agent_name: str = "Builder",
        workdir: Path | None = None,
    ) -> BuildSetup:
        storage = tmp_path / "am-store"
        monkeypatch.setenv("STORAGE_ROOT", str(storage))
        monkeypatch.setenv("WORKTREES_ENABLED", "1" if worktrees else "0")
        monkeypatch.setenv("AGENT_MAIL_GUARD_MODE", guard_mode)
        monkeypatch.setenv("AGENT_NAME", agent_name)
        # An ambient capability would short-circuit the database lookup these
        # tests are about, and the developer running them may well have one.
        monkeypatch.delenv("AGENT_MAIL_REGISTRATION_TOKEN", raising=False)
        get_settings.cache_clear()
        target = workdir if workdir is not None else tmp_path / "workdir"
        target.mkdir(parents=True, exist_ok=True)
        return BuildSetup(workdir=target, _storage=storage)

    return configure


def register_local_agent(setup: BuildSetup, *, name: str, token: str) -> None:
    """Give the CLI a locally registered agent whose token it can resolve."""
    human_key = setup.project_key
    slug = "p" + hashlib.blake2s(human_key.encode("utf-8"), digest_size=8).hexdigest()

    async def _write() -> None:
        await ensure_schema()
        async with get_session() as session:
            found = await session.execute(select(Project).where(cast(Any, Project.human_key == human_key)))
            project = found.scalars().first()
            if project is None:
                project = Project(slug=slug, human_key=human_key)
                session.add(project)
                await session.commit()
                await session.refresh(project)
            session.add(
                Agent(
                    project_id=cast(int, project.id),
                    name=name,
                    program="pytest",
                    model="stub",
                    task_description="",
                    registration_token=token,
                )
            )
            await session.commit()

    asyncio.run(_write())


def write_lease(path: Path, **fields: Any) -> None:
    """Plant a lease file the way a previous run would have left it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def make_git_repo(path: Path) -> Path:
    """Create a real (empty) repository so identity resolution finds a root."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "builder@example.invalid"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Builder"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    return path


TRIVIAL_CHILD = [sys.executable, "-c", "raise SystemExit(0)"]


def run_build(
    setup: BuildSetup,
    *,
    project_path: Path | None = None,
    slot: str = "unittest-slot",
    cmd: list[str] | None = None,
    agent: str = "Builder",
    ttl: int = 120,
    shared: bool = False,
    block: bool = False,
) -> None:
    """Invoke ``am-run`` with the defaults every test in this file shares.

    A call site then spells out only what its own test is about; repeating the
    other five arguments everywhere would bury that one difference.
    """
    am_run(
        slot=slot,
        cmd=TRIVIAL_CHILD if cmd is None else cmd,
        project_path=setup.workdir if project_path is None else project_path,
        agent=agent,
        ttl_seconds=ttl,
        shared=shared,
        block_on_conflicts=block,
    )


# ==========================================================================
# Path components: the archive must survive hostile slot/agent/branch names
# ==========================================================================


@pytest.mark.parametrize("device", ["CON", "CON.txt", "NUL.txt", "LPT1.log", "COM3", "PRN", "aux"])
def test_windows_device_names_never_survive_as_a_bare_component(device: str) -> None:
    component = _safe_build_path_component(device)

    stem = component.partition(".")[0].upper()
    assert stem not in {"CON", "PRN", "AUX", "NUL", "COM3", "LPT1"}
    assert component != device
    assert len(component.encode("utf-8")) <= 80
    assert _safe_build_path_component(device) == component
    # Defusing "CON" to something fixed would fuse it with every other name
    # that defuses the same way, so the digest of the original comes along.
    assert component.endswith(hashlib.sha256(device.encode("utf-8")).hexdigest()[:32])


@pytest.mark.parametrize("value", ["x" * 400, "ż" * 100, "a" * 79 + "bc"])
def test_overlong_names_are_cut_to_the_portable_byte_budget(value: str) -> None:
    component = _safe_build_path_component(value)

    assert len(component.encode("utf-8")) <= 80
    # Truncation alone would fuse distinct long names; the digest is what
    # keeps them apart, so it has to be present and it has to be of the
    # *original* value rather than of the truncated prefix.
    assert component.endswith(hashlib.sha256(value.encode("utf-8")).hexdigest()[:32])


def test_a_component_that_is_already_portable_is_passed_through_verbatim() -> None:
    # This is what makes the disambiguation below observable: an untouched
    # name carries no digest, so a digest means "this name was rewritten".
    for value in ("feature_foo", "Builder", "frontend-build", "v1.2.3"):
        assert _safe_build_path_component(value) == value


def test_rewriting_two_different_names_never_produces_one_directory() -> None:
    collision_bait = ["a/b", "a_b", "a b", ".", "..", "", "   ", "ż" * 100, "ż" * 101]

    components = [_safe_build_path_component(value) for value in collision_bait]

    assert len(set(components)) == len(collision_bait)
    assert not set(components) & {"", ".", ".."}
    assert all(len(component.encode("utf-8")) <= 80 for component in components)


def test_a_real_child_process_receives_the_prepared_environment(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Every other test here replaces subprocess.run, which means none of them
    # would notice if the env mapping stopped being a usable environment or
    # the argv list stopped being passed through. This one spawns for real.
    setup = build_setup(guard_mode="warn")
    unreachable_endpoint(monkeypatch)
    reported = tmp_path / "child-env.json"
    program = (
        "import json, os, pathlib; "
        f"pathlib.Path({str(reported)!r}).write_text(json.dumps("
        "{key: os.environ.get(key) for key in ('AM_SLOT', 'AGENT', 'SLUG', 'ARTIFACT_DIR')}))"
    )

    run_build(setup, slot="real-child", cmd=[sys.executable, "-c", program])

    seen = json.loads(reported.read_text(encoding="utf-8"))
    assert seen["AM_SLOT"] == "real-child"
    assert seen["AGENT"] == "Builder"
    assert seen["SLUG"] == _resolve_project_identity(setup.project_key)["slug"]
    assert Path(seen["ARTIFACT_DIR"]).is_relative_to(setup.storage_root)


# ==========================================================================
# Offline: the local lease file is the authority
# ==========================================================================


def test_offline_run_leaves_exactly_one_local_lease_for_the_slot(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup(guard_mode="warn")
    unreachable_endpoint(monkeypatch)
    child = ChildStub().install(monkeypatch)

    run_build(setup)

    leases = setup.leases("unittest-slot")
    assert len(leases) == 1
    payload = json.loads(leases[0].read_text(encoding="utf-8"))
    assert payload["slot"] == "unittest-slot"
    assert payload["agent"] == "Builder"
    assert payload["exclusive"] is True
    # A reader has to be able to tell a fallback lease from a server one.
    assert payload["authority"] == "local"
    assert child.started


def test_a_dotdot_slot_name_cannot_place_its_lease_outside_build_slots(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup(guard_mode="warn")
    unreachable_endpoint(monkeypatch)
    ChildStub().install(monkeypatch)

    run_build(setup, slot="..")

    archive = setup.archive_root()
    build_slots_root = (archive / "build_slots").resolve()
    leases = sorted(build_slots_root.rglob("*.json"))
    assert len(leases) == 1
    assert leases[0].resolve().is_relative_to(build_slots_root)
    # ".." is not merely rejected, it is renamed to something inert.
    assert leases[0].parent.name.startswith("unknown-")
    assert sorted(archive.glob("*.json")) == []


def test_traversal_in_agent_and_branch_keeps_the_child_writing_inside_the_archive(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    setup = build_setup(guard_mode="warn")
    unreachable_endpoint(monkeypatch)
    monkeypatch.setattr(
        "mcp_agent_mail.app._resolve_project_identity",
        lambda _path: {
            "slug": "hostile-branch-project",
            "project_uid": "uid-hostile",
            "branch": "../../outside-branch\\..\\escaped",
        },
    )

    def write_a_marker(record: ChildRun) -> None:
        artifact_dir = Path(record.env["ARTIFACT_DIR"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "child-wrote-this.txt").write_text("ok", encoding="utf-8")

    child = ChildStub(while_running=write_a_marker).install(monkeypatch)

    run_build(setup, slot="safe-slot", cmd=["unused"], agent="../outside-agent\\..\\escaped")

    archive = setup.archive_root("hostile-branch-project").resolve()
    artifacts_root = (archive / "artifacts").resolve()
    artifact_dir = Path(child.env["ARTIFACT_DIR"]).resolve()
    assert artifact_dir.is_relative_to(artifacts_root)

    markers = sorted(tmp_path.rglob("child-wrote-this.txt"))
    assert markers == [artifact_dir / "child-wrote-this.txt"]
    assert markers[0].resolve().is_relative_to(archive)

    leases = sorted((archive / "build_slots").resolve().rglob("*.json"))
    assert len(leases) == 1
    assert leases[0].resolve().is_relative_to((archive / "build_slots").resolve())


def test_a_sanitised_branch_never_shares_an_artifact_directory_with_a_literal_one(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup(guard_mode="warn")
    unreachable_endpoint(monkeypatch)
    current_branch = {"value": "feature/foo"}
    monkeypatch.setattr(
        "mcp_agent_mail.app._resolve_project_identity",
        lambda _path: {
            "slug": "disambiguation-project",
            "project_uid": "uid-disambiguation",
            "branch": current_branch["value"],
        },
    )
    child = ChildStub().install(monkeypatch)

    for branch in ("feature/foo", "feature_foo"):
        current_branch["value"] = branch
        run_build(setup, slot="safe-slot", cmd=["unused"])

    slashed, underscored = (Path(run.env["ARTIFACT_DIR"]) for run in child.runs)
    assert slashed != underscored
    # "feature_foo" was writable as-is, so it is used unchanged; "feature/foo"
    # had to be rewritten, so it carries a digest and cannot land on top of it.
    assert underscored.name == "feature_foo"
    assert slashed.name == _safe_build_path_component("feature/foo")
    archive = setup.archive_root("disambiguation-project").resolve()
    assert all(path.resolve().is_relative_to(archive) for path in (slashed, underscored))

    # Two independent executions, two lease files, neither overwriting the other.
    leases = sorted((archive / "build_slots" / "safe-slot").glob("*.json"))
    assert len(leases) == 2
    assert len({path.name for path in leases}) == 2


def test_a_ttl_below_the_server_floor_is_raised_before_the_child_starts(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup(guard_mode="warn")
    unreachable_endpoint(monkeypatch)
    observed: list[float] = []

    def inspect_lease(_record: ChildRun) -> None:
        # Read it from inside the child: after the run the lease is released,
        # so a check afterwards could not tell a floored TTL from a released one.
        leases = setup.leases("unittest-slot")
        assert len(leases) == 1
        payload = json.loads(leases[0].read_text(encoding="utf-8"))
        acquired = datetime.fromisoformat(payload["acquired_ts"])
        expires = datetime.fromisoformat(payload["expires_ts"])
        observed.append((expires - acquired).total_seconds())

    ChildStub(while_running=inspect_lease).install(monkeypatch)

    run_build(setup, ttl=30)

    assert observed == [pytest.approx(60, abs=5)]


def test_reacquiring_our_own_lease_never_moves_its_expiry_backwards(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup(guard_mode="warn")
    unreachable_endpoint(monkeypatch)
    # Pin the execution id so the lease this run writes is the file planted
    # below -- that is the only way to exercise the same-holder branch.
    pinned = "0f0e0d0c-0b0a-4908-8706-050403020100"
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(pinned))

    lease_path = setup.archive_root() / "build_slots" / "unittest-slot" / f"{pinned}.json"
    write_lease(
        lease_path,
        slot="unittest-slot",
        agent="Builder",
        branch="unknown",
        exclusive=True,
        execution_id=pinned,
        acquired_ts=LONG_PAST,
        expires_ts=FAR_FUTURE,
    )
    seen: list[dict[str, Any]] = []

    def read_lease(_record: ChildRun) -> None:
        seen.append(json.loads(lease_path.read_text(encoding="utf-8")))

    ChildStub(while_running=read_lease).install(monkeypatch)

    run_build(setup, ttl=30)

    assert len(seen) == 1
    assert seen[0]["acquired_ts"] == LONG_PAST
    assert datetime.fromisoformat(seen[0]["expires_ts"]) >= datetime.fromisoformat(FAR_FUTURE)


def test_another_holders_active_lease_is_left_byte_for_byte_alone(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup(guard_mode="warn")
    unreachable_endpoint(monkeypatch)
    foreign = setup.archive_root() / "build_slots" / "unittest-slot" / "someone-else.json"
    write_lease(
        foreign,
        slot="unittest-slot",
        agent="OtherBuilder",
        branch="main",
        exclusive=True,
        execution_id="not-ours",
        acquired_ts=LONG_PAST,
        expires_ts=FAR_FUTURE,
    )
    before = foreign.read_bytes()
    ChildStub().install(monkeypatch)

    run_build(setup)

    # Neither the acquire nor the release path may touch a lease it does not own.
    assert foreign.read_bytes() == before


def test_every_lease_file_touch_happens_under_the_archive_write_lock(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup(guard_mode="warn")
    unreachable_endpoint(monkeypatch)

    # The counter has to be process-wide, not per-thread: the code holds the
    # lock on one thread and then performs the file IO on `asyncio.to_thread`
    # workers, so a thread-local count reads zero for every real access.
    # A process-wide count is only unambiguous while at most one lock section
    # can be open, so the renewer is deliberately left dormant here -- the
    # default interval for a 120s TTL is 60s and the child returns at once,
    # which makes the main thread the only actor for the whole measurement.
    depth = 0
    observed_depths: list[int] = []
    path_type = type(setup.workdir)
    originals = {name: getattr(path_type, name) for name in ("glob", "read_text", "write_text", "mkdir")}
    original_lock = cli_module.archive_write_lock

    def note(path: Path) -> None:
        if "build_slots" in path.parts:
            observed_depths.append(depth)

    def wrap(name: str) -> Callable[..., Any]:
        original = originals[name]

        def wrapper(self: Path, *args: Any, **kwargs: Any) -> Any:
            note(self)
            return original(self, *args, **kwargs)

        return wrapper

    @contextlib.asynccontextmanager
    async def counting_lock(*args: Any, **kwargs: Any) -> Any:
        nonlocal depth
        async with original_lock(*args, **kwargs):
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    for name in originals:
        monkeypatch.setattr(path_type, name, wrap(name))
    monkeypatch.setattr(cli_module, "archive_write_lock", counting_lock)
    child = ChildStub().install(monkeypatch)

    run_build(setup)

    assert child.started
    assert observed_depths, "no build_slots file access was observed at all"
    # Sections never nest or overlap, so "depth > 0" really does mean
    # "inside the one section that is open".
    assert set(observed_depths) == {1}


def test_asking_for_a_shared_lease_does_not_buy_past_a_local_exclusive_holder(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup(guard_mode="block")
    unreachable_endpoint(monkeypatch)
    write_lease(
        setup.archive_root() / "build_slots" / "unittest-slot" / "someone-else.json",
        slot="unittest-slot",
        agent="OtherBuilder",
        branch="main",
        exclusive=True,
        acquired_ts=LONG_PAST,
        expires_ts=FAR_FUTURE,
    )
    forbid_child(monkeypatch, "the child must not start while an exclusive holder is live")

    with pytest.raises(typer.Exit) as raised:
        run_build(setup, shared=True, block=True)

    assert raised.value.exit_code == 1


def test_a_shared_local_holder_does_not_block_another_shared_request(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The complement of the test above: without this one, blanket refusal
    # would pass just as well as reading the `exclusive` flag.
    setup = build_setup(guard_mode="block")
    unreachable_endpoint(monkeypatch)
    write_lease(
        setup.archive_root() / "build_slots" / "unittest-slot" / "someone-else.json",
        slot="unittest-slot",
        agent="OtherBuilder",
        branch="main",
        exclusive=False,
        acquired_ts=LONG_PAST,
        expires_ts=FAR_FUTURE,
    )
    child = ChildStub().install(monkeypatch)

    run_build(setup, shared=True, block=True)

    assert child.started


# ==========================================================================
# Server authority: lifecycle, tokens, and every way it can go wrong
# ==========================================================================


def test_a_server_run_opens_one_execution_and_threads_it_through_every_call(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    child = ChildStub().install(monkeypatch)

    run_build(setup, slot="frontend-build")

    assert endpoint.tool_names == [
        "ensure_project",
        "start_agent_execution",
        "acquire_build_slot",
        "release_build_slot",
        "end_agent_execution",
    ]

    start = endpoint.arguments_for("start_agent_execution")
    assert start["kind"] == "session"
    assert start["client_name"] == "am-run"
    assert start["lifecycle_protocol_version"] == PROTOCOL_VERSION
    assert start["registration_token"] == "secret-token"
    # The capability the server will check on every later call: 32 random bytes.
    assert re.fullmatch(r"[0-9a-f]{64}", start["execution_token"])

    for tool in ("acquire_build_slot", "release_build_slot", "end_agent_execution"):
        arguments = endpoint.arguments_for(tool)
        # The server's id, not the uuid4 am-run minted for the offline case.
        assert arguments["execution_id"] == SERVER_EXECUTION_ID
        assert arguments["execution_token"] == start["execution_token"]
        assert arguments["lifecycle_protocol_version"] == PROTOCOL_VERSION

    assert endpoint.arguments_for("end_agent_execution")["status"] == "completed"
    assert child.env["AGENT_EXECUTION_ID"] == SERVER_EXECUTION_ID


def test_a_failing_child_ends_the_execution_as_failed_and_propagates_its_code(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    ChildStub(returncode=42).install(monkeypatch)

    with pytest.raises(typer.Exit) as raised:
        run_build(setup)

    assert raised.value.exit_code == 42
    assert endpoint.arguments_for("end_agent_execution")["status"] == "failed"
    # A failed build still owes the server its slot back.
    assert endpoint.called("release_build_slot")


def test_the_registration_token_accompanies_every_build_slot_call(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    renew_attempted = threading.Event()

    def note_renewal(_arguments: dict[str, Any]) -> None:
        renew_attempted.set()

    def await_renewal(_record: ChildRun) -> None:
        assert renew_attempted.wait(timeout=2), "the renewer never called renew_build_slot"

    endpoint.react("renew_build_slot", note_renewal)
    monkeypatch.setattr("mcp_agent_mail.cli._build_slot_renew_interval_seconds", lambda _ttl: 0.01)
    ChildStub(while_running=await_renewal).install(monkeypatch)

    run_build(setup)

    for tool in SLOT_TOOLS:
        tokens = endpoint.every_argument(tool, "registration_token")
        assert tokens, f"{tool} was never called; saw {endpoint.tool_names}"
        assert set(tokens) == {"secret-token"}


def test_the_registration_token_is_found_whatever_the_case_of_the_agent_name(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup(agent_name="builder")
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    ChildStub().install(monkeypatch)

    run_build(setup, agent="builder")

    assert endpoint.arguments_for("acquire_build_slot")["registration_token"] == "secret-token"
    assert endpoint.arguments_for("release_build_slot")["registration_token"] == "secret-token"


def test_a_reachable_server_and_no_registration_token_refuses_the_run(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()  # deliberately no registered agent
    endpoint = StubEndpoint().install(monkeypatch)
    forbid_child(monkeypatch, "the child must not start without a slot")

    with pytest.raises(click.ClickException) as raised:
        run_build(setup)

    assert "registration token" in str(raised.value)
    # It fails before opening an execution, so there is nothing to clean up.
    assert not endpoint.called("start_agent_execution")
    assert not endpoint.called("acquire_build_slot")


def test_server_reported_conflicts_stop_the_build_and_write_no_local_lease(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    endpoint.answer(
        "acquire_build_slot",
        RpcResult(
            {
                "conflicts": [
                    {
                        "slot": "unittest-slot",
                        "agent": "OtherBuilder",
                        "branch": "main",
                        "expires_ts": "2026-04-10T03:00:00Z",
                    }
                ]
            }
        ),
    )
    forbid_child(monkeypatch, "the child must not start when the server reports a conflict")

    with pytest.raises(typer.Exit) as raised:
        run_build(setup, block=True)

    assert raised.value.exit_code == 1
    # Falling back locally here would hand the slot to two builders at once.
    assert setup.every_lease() == []


def test_asking_for_a_shared_lease_does_not_buy_past_a_server_side_exclusive_holder(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    endpoint.answer(
        "acquire_build_slot",
        RpcResult(
            {
                "conflicts": [
                    {
                        "slot": "unittest-slot",
                        "agent": "OtherBuilder",
                        "branch": "main",
                        "exclusive": True,
                        "expires_ts": "2026-04-10T03:00:00Z",
                    }
                ]
            }
        ),
    )
    forbid_child(monkeypatch, "a shared request must not run past an exclusive holder")

    with pytest.raises(typer.Exit) as raised:
        run_build(setup, shared=True, block=True)

    assert raised.value.exit_code == 1


def test_a_rejected_acquire_is_reported_verbatim_and_releases_nothing(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    endpoint.answer("acquire_build_slot", RpcError("server denied build slot"))
    forbid_child(monkeypatch, "the child must not start when acquisition was denied")

    with pytest.raises(click.ClickException) as raised:
        run_build(setup, block=True)

    assert "server denied build slot" in str(raised.value)
    # Releasing a slot we never held could evict whoever does hold it.
    assert not endpoint.called("release_build_slot")
    assert setup.every_lease() == []


def test_an_undecodable_body_is_named_as_such_rather_than_treated_as_success(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    StubEndpoint().install(monkeypatch).answer("ensure_project", UndecodableBody())
    forbid_child(monkeypatch, "the child must not start on an undecodable server reply")

    with pytest.raises(click.ClickException) as raised:
        run_build(setup, block=True)

    assert "invalid JSON response from server" in str(raised.value)
    assert setup.every_lease() == []


def test_an_http_status_failure_is_reported_with_its_code(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    # HttpFailure.json() raises if it is ever read, so this also pins that
    # the status is checked before the body.
    StubEndpoint().install(monkeypatch).answer("ensure_project", HttpFailure(401))
    forbid_child(monkeypatch, "the child must not start after an HTTP failure")

    with pytest.raises(click.ClickException) as raised:
        run_build(setup, block=True)

    assert "HTTP 401 from server" in str(raised.value)
    assert setup.every_lease() == []


def test_an_acquire_that_never_answered_fails_closed_and_still_ends_the_execution(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    endpoint.fail("acquire_build_slot", httpx.ConnectError("connection dropped mid-acquire"))
    forbid_child(monkeypatch, "the child must not start on an ambiguous acquire")

    with pytest.raises(click.ClickException, match="remote acquisition result is ambiguous"):
        run_build(setup)

    # The server may or may not have taken the slot, so a local lease would be
    # a guess. The execution is still closed rather than left dangling.
    assert endpoint.tool_names == [
        "ensure_project",
        "start_agent_execution",
        "acquire_build_slot",
        "end_agent_execution",
    ]
    assert setup.every_lease() == []


def test_a_failing_renewal_keeps_server_authority_and_writes_no_local_lease(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    renew_attempted = threading.Event()

    def drop_renewals(_arguments: dict[str, Any]) -> Any:
        renew_attempted.set()
        raise httpx.ConnectError("renew unavailable")

    endpoint.react("renew_build_slot", drop_renewals)
    monkeypatch.setattr("mcp_agent_mail.cli._build_slot_renew_interval_seconds", lambda _ttl: 0.01)
    def _await_renewal(_record: ChildRun) -> None:
        # A lambda here returns Event.wait()'s bool, and the callback contract
        # says None. Harmless at runtime, and a real disagreement about what the
        # hook is for: it exists to pause, not to report.
        renew_attempted.wait(timeout=2)

    child = ChildStub(while_running=_await_renewal).install(monkeypatch)

    run_build(setup)

    assert renew_attempted.is_set()
    # A lost renewal is a reason to retry, not a reason to change authority,
    # and certainly not a reason to kill a running build.
    assert child.started
    assert setup.every_lease() == []


def test_the_renewer_is_stopped_before_the_slot_is_handed_back(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    release_in_flight = threading.Event()
    renewed_during_release = threading.Event()

    def slow_release(_arguments: dict[str, Any]) -> Any:
        release_in_flight.set()
        time.sleep(0.05)
        return None

    def note_late_renewal(_arguments: dict[str, Any]) -> Any:
        if release_in_flight.is_set():
            renewed_during_release.set()
        return None

    endpoint.react("release_build_slot", slow_release)
    endpoint.react("renew_build_slot", note_late_renewal)
    monkeypatch.setattr("mcp_agent_mail.cli._build_slot_renew_interval_seconds", lambda _ttl: 0.01)
    ChildStub().install(monkeypatch)

    run_build(setup)

    assert release_in_flight.is_set()
    # A renewal racing the release would resurrect a slot nobody holds.
    assert not renewed_during_release.is_set()


def test_a_failing_release_does_not_fall_back_to_writing_a_local_lease(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    endpoint.fail("release_build_slot", httpx.ConnectError("release unavailable"))
    ChildStub().install(monkeypatch)

    run_build(setup)

    assert endpoint.called("acquire_build_slot")
    assert endpoint.called("release_build_slot")
    # Expiry on the server is the fallback, not a file this process invents.
    assert setup.every_lease() == []


def test_all_three_slot_calls_quote_the_same_branch_and_protocol_version(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    renew_attempted = threading.Event()

    def note_renewal(_arguments: dict[str, Any]) -> None:
        renew_attempted.set()

    def await_renewal(_record: ChildRun) -> None:
        assert renew_attempted.wait(timeout=2), "the renewer never called renew_build_slot"

    endpoint.react("renew_build_slot", note_renewal)
    monkeypatch.setattr("mcp_agent_mail.cli._build_slot_renew_interval_seconds", lambda _ttl: 0.01)
    ChildStub(while_running=await_renewal).install(monkeypatch)

    run_build(setup)

    for tool in SLOT_TOOLS:
        branches = endpoint.every_argument(tool, "branch")
        assert branches, f"{tool} was never called; saw {endpoint.tool_names}"
        # A workdir outside any repository still has to name one stable branch:
        # acquire, renew and release must agree or they address different rows.
        assert set(branches) == {"unknown"}
        assert set(endpoint.every_argument(tool, "lifecycle_protocol_version")) == {PROTOCOL_VERSION}


# ==========================================================================
# TTL arithmetic
# ==========================================================================


@pytest.mark.parametrize(
    ("requested", "effective"),
    [(0, 60), (1, 60), (30, 60), (59, 60), (60, 60), (61, 61), (3600, 3600)],
)
def test_the_effective_ttl_never_drops_below_the_servers_sixty_second_floor(
    requested: int, effective: int
) -> None:
    assert _effective_build_slot_ttl_seconds(requested) == effective


@pytest.mark.parametrize("requested", [0, 30, 60, 120, 3601])
def test_renewal_happens_halfway_through_the_effective_ttl(requested: int) -> None:
    effective = _effective_build_slot_ttl_seconds(requested)
    interval = _build_slot_renew_interval_seconds(requested)

    assert interval == effective // 2
    # Renewing on the boundary would leave a window with no live lease.
    assert 1 <= interval < effective


def test_the_server_is_asked_for_the_floored_ttl_not_the_requested_one(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup()
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    ChildStub().install(monkeypatch)

    run_build(setup, ttl=30)

    assert endpoint.every_argument("acquire_build_slot", "ttl_seconds") == [60]


# ==========================================================================
# The worktree gate, argv secrecy, and project identity
# ==========================================================================


def test_the_execution_lifecycle_runs_even_with_the_worktree_gate_closed(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup = build_setup(worktrees=False)
    register_local_agent(setup, name="Builder", token="secret-token")
    endpoint = StubEndpoint().install(monkeypatch)
    child = ChildStub().install(monkeypatch)

    run_build(setup, slot="no-slot-gate", cmd=[sys.executable, "--token", "TOP_SECRET_ARG"])

    # The gate governs build slots only; the execution record is unconditional.
    assert endpoint.tool_names == ["ensure_project", "start_agent_execution", "end_agent_execution"]
    start = endpoint.arguments_for("start_agent_execution")
    assert start["task_description"] == "am-run build slot: no-slot-gate"
    assert child.env["AGENT_EXECUTION_ID"] == SERVER_EXECUTION_ID

    # Build commands carry credentials in argv. Neither the server record nor
    # the console echo may contain them.
    assert "TOP_SECRET_ARG" not in json.dumps(start)
    printed = capsys.readouterr().out
    assert "TOP_SECRET_ARG" not in printed
    assert "redacted" in printed


def test_an_offline_run_hands_the_child_no_execution_id_at_all(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = build_setup(worktrees=False)  # no registered agent, no server
    unreachable_endpoint(monkeypatch)
    child = ChildStub().install(monkeypatch)

    run_build(setup, slot="offline-slot")

    # An id the server never issued would be quoted back at it later as if it
    # were real, so an unconfirmed execution exposes nothing.
    assert "AGENT_EXECUTION_ID" not in child.env


def test_a_child_started_in_a_subdirectory_gets_the_repository_roots_identity(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path / "repo")
    workdir = repo / "nested" / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    setup = build_setup(worktrees=False, workdir=workdir)

    root_identity = _resolve_project_identity(str(repo))
    subdir_identity = _resolve_project_identity(str(workdir))
    # Control: without the walk-up these two would be indistinguishable and
    # the assertions below would hold for the wrong reason.
    assert root_identity["slug"] != subdir_identity["slug"]

    child = ChildStub().install(monkeypatch)

    run_build(setup, project_path=workdir, slot="subdir-slot")

    env = child.env
    assert env["SLUG"] == root_identity["slug"]
    assert env["PROJECT_UID"] == root_identity["project_uid"]
    assert env["AGENT"] == "Builder"
    assert env["AM_SLOT"] == "subdir-slot"
    assert env["BRANCH"]
    assert env["CACHE_KEY"] == f"am-cache-{root_identity['project_uid']}-Builder-{env['BRANCH']}"
    assert env["ARTIFACT_DIR"] == str(
        setup.storage_root
        / "projects"
        / root_identity["slug"]
        / "artifacts"
        / _safe_build_path_component("Builder")
        / _safe_build_path_component(env["BRANCH"])
    )


def test_amctl_env_prints_one_unwrapped_line_per_variable(
    build_setup: Callable[..., BuildSetup],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path / "repo-with-a-deliberately-long-directory-name-for-env")
    workdir = repo / "nested" / "path" / "for" / "env"
    workdir.mkdir(parents=True, exist_ok=True)
    setup = build_setup(worktrees=False, workdir=workdir)
    # A narrow terminal is what would tempt a rich renderer into wrapping.
    monkeypatch.setenv("COLUMNS", "40")
    get_settings.cache_clear()
    root_identity = _resolve_project_identity(str(repo))

    result = CliRunner().invoke(app, ["amctl", "env", "--path", str(workdir), "--agent", "Builder"])

    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if "=" in line]
    env = dict(line.split("=", 1) for line in lines)

    assert env["SLUG"] == root_identity["slug"]
    assert env["PROJECT_UID"] == root_identity["project_uid"]
    assert env["AGENT"] == "Builder"
    assert env["BRANCH"]
    assert env["CACHE_KEY"] == f"am-cache-{root_identity['project_uid']}-Builder-{env['BRANCH']}"

    expected_artifact_dir = (
        setup.storage_root
        / "projects"
        / root_identity["slug"]
        / "artifacts"
        / _safe_build_path_component("Builder")
        / _safe_build_path_component(env["BRANCH"])
    )
    assert env["ARTIFACT_DIR"] == str(expected_artifact_dir)
    # The output is meant to be eval'd by a shell, so a value longer than the
    # terminal must still arrive on a single line.
    assert len(f"ARTIFACT_DIR={expected_artifact_dir}") > 40
    assert f"ARTIFACT_DIR={expected_artifact_dir}" in lines
