"""Contract tests for `scripts/hooks/inbox_watch_monitor.sh`.

The monitor is the one hook script built never to exit, so the properties worth
pinning here are the ones that are *inverted* relative to every sibling in that
directory. Both were wrong in the first draft, and neither is visible to a test
that only checks argument validation:

  * a missing identity must NOT end the run — `inbox_watch.sh` exits there and is
    right to, because the next manual arm picks it up; a plugin monitor that
    exits is never restarted for the life of the CLI process, so exiting is death
  * SIGTERM must actually end the process — `trap cleanup EXIT INT TERM`, the
    idiom the other hooks use, runs the handler and then RESUMES the loop, so the
    monitor survived its own stop signal and orphaned its curl child

These live in their own module rather than in test_agent_hook_identity.py so the
twelve existing assertions about inbox_watch.sh keep a file to themselves.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import skip_if_cpu_overloaded
from tests.test_agent_hook_identity import (
    BASH,
    ROOT,
    _git_bash_path,
    _hook_env,
    _init_git_repo,
    _install_fake_curl,
    _tree_snapshot,
)

MONITOR = ROOT / "scripts" / "hooks" / "inbox_watch_monitor.sh"


def _monitor_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _install_fake_curl(
        fake_bin,
        '#!/usr/bin/env bash\nprintf called >> "$FAKE_CURL_LOG"\nexit 97\n',
    )
    env = _hook_env(home, state, fake_bin)
    env["FAKE_CURL_LOG"] = _git_bash_path(tmp_path / "curl.log")
    # Without this the no-identity path sleeps for five minutes and the test
    # would be measuring the sleep rather than the loop.
    env["AGENT_MAIL_MONITOR_BACKOFF_MAX"] = "1"
    env["AGENT_MAIL_MONITOR_BACKOFF_MIN"] = "1"
    return env, repo, tmp_path / "curl.log"


@pytest.fixture(scope="module", autouse=True)
def _harness_can_actually_run_the_script(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Fail this module outright if the script dies before it reaches its own code.

    Every refusal assertion below is of the form "exit 0 and touch nothing", and
    a script that never starts satisfies all of them. `claude-win-home-1`
    measured exactly that: run from a native Windows shell, `PATH` carries
    `Git/cmd` but not `Git/usr/bin`, so `dirname` on line 47 is not found, the
    `. agent_mail_common.sh` that follows fails, and the script exits 0 from the
    top of the file. Seven refusal tests then went green while the two-argument
    contract they claim to cover was never executed — deleting the validation
    entirely would not have shown up.

    So the module gets a precondition rather than better wording: start the
    script with valid arguments and require it to still be alive. It cannot be,
    unless sourcing worked and the loop was reached.
    """
    tmp = tmp_path_factory.mktemp("harness-canary")
    env, repo, _ = _monitor_env(tmp)
    proc = subprocess.Popen(
        [BASH, _git_bash_path(MONITOR), "claude", "1"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(3)
    alive = proc.poll() is None
    proc.terminate()
    try:
        _stdout, stderr = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - failure path
        proc.kill()
        _stdout, stderr = proc.communicate()

    if not alive:
        pytest.fail(
            "the monitor exited immediately with valid arguments, so nothing in "
            "this module is testing what it says it tests. Usually PATH: the "
            "script needs coreutils (`dirname`) from Git's usr/bin, which a "
            "native Windows shell does not put on PATH but Git Bash does. "
            f"stderr was: {stderr!r}"
        )


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["claude"],
        ["claude", "1", "extra"],
    ],
)
def test_monitor_refuses_a_wrong_number_of_arguments(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    """Refusal has to be rc 0, touch nothing, and SAY so.

    The message is asserted, not just the exit code: rc 0 alone is what a script
    that crashed on its second line also produces, and that is how this same
    assertion passed on Windows while testing nothing.

    rc 0 rather than a diagnostic code because the caller is a plugin host that
    reads a non-zero exit as a broken plugin — the operator would be told the
    integration is faulty when the only fault is a typo in a slot.
    """
    env, repo, curl_log = _monitor_env(tmp_path)
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [BASH, _git_bash_path(MONITOR), *arguments],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "requires an explicit client and slot" in result.stderr
    assert result.stdout == ""
    assert not curl_log.exists()
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "arguments",
    [
        ["bogus", "1"],
        ["claude", "0"],
        ["claude", "01"],
        ["claude", "x"],
    ],
)
def test_monitor_refuses_an_invalid_client_or_slot(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    """Rejected by am_client/am_slot, which refuse silently by design.

    There is nothing in the output to assert on here — which is why the module
    canary exists. Without it, "silent and harmless" is indistinguishable from
    "never ran", and this is the shape that went green on Windows for the wrong
    reason.
    """
    env, repo, curl_log = _monitor_env(tmp_path)
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [BASH, _git_bash_path(MONITOR), *arguments],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    # The arity guard is upstream of these, so seeing its message would mean the
    # arguments never reached the client/slot validation this test is about.
    assert "requires an explicit client and slot" not in result.stderr
    assert not curl_log.exists()
    assert _tree_snapshot(tmp_path) == before


def test_monitor_stays_alive_and_silent_without_an_identity(tmp_path: Path) -> None:
    """The inversion that matters: no credential is a reason to wait, not to exit.

    A monitor can legitimately start before SessionStart has registered the
    agent. `inbox_watch.sh` exits in that situation; if this one did, arming it
    at the wrong moment would leave the session with no instant delivery for the
    rest of its life, and nothing would say so.

    Silence is asserted in the same breath because it is the other half of the
    contract: a quiet mailbox must cost nothing, since every line printed here
    becomes a full agent turn.
    """
    env, repo, _ = _monitor_env(tmp_path)

    proc = subprocess.Popen(
        [BASH, _git_bash_path(MONITOR), "claude", "1"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Long enough to cross several iterations of the one-second backoff, so
        # this proves the loop continues rather than that one sleep is running.
        time.sleep(5)
        assert proc.poll() is None, "monitor exited without an identity"
    finally:
        proc.terminate()
        try:
            stdout, _stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - failure path
            proc.kill()
            stdout, _stderr = proc.communicate()

    assert stdout == "", f"monitor spoke when it had nothing to report: {stdout!r}"


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "Windows has no SIGTERM: Popen.terminate() calls TerminateProcess, which "
        "runs no trap, so this asserts nothing about the handler being tested."
    ),
)
def test_monitor_exits_promptly_on_sigterm(tmp_path: Path) -> None:
    """Pins the regression that made the monitor unstoppable.

    `trap cleanup EXIT INT TERM` cleaned up and then returned into the loop, so
    the process survived SIGTERM and opened a fresh subscription. Measured before
    the fix: still running 135 s after `kill`, with a new curl child. Anything
    that stops a monitor — the plugin host, TaskStop, session teardown — depends
    on this, and the next `kill -9` orphans the connection because SIGKILL runs
    no trap at all.
    """
    skip_if_cpu_overloaded()
    env, repo, _ = _monitor_env(tmp_path)

    proc = subprocess.Popen(
        [BASH, _git_bash_path(MONITOR), "claude", "1"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(3)
    assert proc.poll() is None, "monitor exited before the signal could be sent"

    sent = time.monotonic()
    proc.terminate()
    try:
        proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - failure path
        proc.kill()
        proc.communicate()
        pytest.fail("monitor ignored SIGTERM")

    assert time.monotonic() - sent < 15
