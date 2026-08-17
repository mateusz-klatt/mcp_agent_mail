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

import contextlib
import json
import os
import signal
import subprocess
import sys
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
SETUP = ROOT / "scripts" / "hooks" / "agent_mail_setup.sh"


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


def _install_setup_server(fake_bin: Path) -> None:
    _install_fake_curl(
        fake_bin,
        r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_ARGV_LOG"
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name')"
case "$tool" in
  ensure_project)
    result='{"id":1,"slug":"owner-repo","human_key":"/owner/repo","project_uid":"private-project-uid"}'
    ;;
  register_agent)
    name="$(printf '%s' "$body" | jq -r '.params.arguments.name')"
    token="$(printf '%s' "$body" | jq -r '.params.arguments.registration_token // empty')"
    if [[ -z "$token" ]]; then
      result="$(jq -nc --arg name "$name" '{id:7,name:$name,display_name:"BlueCastle",notify_sound:"chime",registration_token:"one-time-secret"}')"
    else
      [[ "$token" == "one-time-secret" ]] || exit 91
      result="$(jq -nc --arg name "$name" '{id:7,name:$name,display_name:"BlueCastle",notify_sound:"chime"}')"
    fi
    ;;
  fetch_inbox)
    token="$(printf '%s' "$body" | jq -r '.params.arguments.registration_token // empty')"
    [[ "$token" == "one-time-secret" ]] || exit 92
    result='[]'
    ;;
  *) exit 93 ;;
esac
jq -nc --argjson value "$result" '{result:{content:[{type:"text",text:($value|tojson)}],isError:false}}'
printf '200\n'
''',
    )


def _install_rotation_server(fake_bin: Path) -> None:
    """Install a stateful fake for onboard plus journaled token rotation."""
    _install_fake_curl(
        fake_bin,
        r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_ARGV_LOG"
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name')"
printf '%s\n' "$tool" >> "$FAKE_TOOL_LOG"
current="$(cat "$FAKE_SERVER_TOKEN_FILE" 2>/dev/null)"
error=""
case "$tool" in
  ensure_project)
    result='{"id":1,"slug":"owner-repo","human_key":"/owner/repo","project_uid":"private-project-uid"}'
    ;;
  register_agent)
    name="$(printf '%s' "$body" | jq -r '.params.arguments.name')"
    token="$(printf '%s' "$body" | jq -r '.params.arguments.registration_token // empty')"
    if [[ -z "$token" ]]; then
      current="one-time-secret"
      printf '%s' "$current" > "$FAKE_SERVER_TOKEN_FILE"
      result="$(jq -nc --arg name "$name" --arg token "$current" \
        '{id:7,name:$name,registration_token:$token}')"
    elif [[ "$token" == "$current" ]]; then
      result="$(jq -nc --arg name "$name" '{id:7,name:$name}')"
    else
      error="authentication failed"
    fi
    ;;
  fetch_inbox)
    token="$(printf '%s' "$body" | jq -r '.params.arguments.registration_token // empty')"
    if [[ "$token" == "$current" ]]; then result='[]'; else error="authentication failed"; fi
    ;;
  rotate_registration_token)
    old="$(printf '%s' "$body" | jq -r '.params.arguments.registration_token // empty')"
    new="$(printf '%s' "$body" | jq -r '.params.arguments.new_registration_token // empty')"
    if [[ "${FAKE_ROTATE_REJECT:-0}" == 1 ]]; then
      error="rotation refused"
    elif [[ "$new" == "$current" ]]; then
      result="$(jq -nc --arg agent "$(printf '%s' "$body" | jq -r '.params.arguments.agent_name')" \
        '{agent:$agent,project:"/owner/repo",rotated:false,already_current:true}')"
    elif [[ "$old" == "$current" ]]; then
      if [[ "${FAKE_ROTATE_DELAY:-0}" != 0 ]]; then sleep "$FAKE_ROTATE_DELAY"; fi
      printf '%s' "$new" > "$FAKE_SERVER_TOKEN_FILE"
      count="$(cat "$FAKE_ROTATE_COUNT_FILE" 2>/dev/null)"
      case "$count" in ''|*[!0-9]*) count=0 ;; esac
      printf '%s' "$((count + 1))" > "$FAKE_ROTATE_COUNT_FILE"
      if [[ "${FAKE_LOSE_ROTATION_RESPONSE_ONCE:-0}" == 1 \
          && ! -e "$FAKE_LOST_RESPONSE_MARKER" ]]; then
        : > "$FAKE_LOST_RESPONSE_MARKER"
        exit 97
      fi
      result="$(jq -nc --arg agent "$(printf '%s' "$body" | jq -r '.params.arguments.agent_name')" \
        '{agent:$agent,project:"/owner/repo",rotated:true,already_current:false}')"
    else
      error="authentication failed"
    fi
    ;;
  whois)
    token="$(printf '%s' "$body" | jq -r '.params.arguments.registration_token // empty')"
    agent="$(printf '%s' "$body" | jq -r '.params.arguments.agent_name')"
    if [[ "$token" == "$current" ]]; then
      result="$(jq -nc --arg agent "$agent" '{id:7,name:$agent,recent_commits:[]}')"
    else
      error="authentication failed"
    fi
    ;;
  *) exit 93 ;;
esac
if [[ -n "$error" ]]; then
  jq -nc --arg message "$error" \
    '{result:{content:[{type:"text",text:$message}],isError:true}}'
else
  jq -nc --argjson value "$result" \
    '{result:{content:[{type:"text",text:($value|tojson)}],isError:false}}'
fi
printf '200\n'
''',
    )


def _rotation_setup(
    tmp_path: Path,
) -> tuple[dict[str, str], Path, str, str]:
    env, repo, _ = _monitor_env(tmp_path)
    _install_rotation_server(tmp_path / "bin")
    env["FAKE_ARGV_LOG"] = _git_bash_path(tmp_path / "argv.log")
    env["FAKE_SERVER_TOKEN_FILE"] = _git_bash_path(tmp_path / "server-token")
    env["FAKE_ROTATE_COUNT_FILE"] = _git_bash_path(tmp_path / "rotation-count")
    env["FAKE_LOST_RESPONSE_MARKER"] = _git_bash_path(tmp_path / "lost-response")
    env["FAKE_TOOL_LOG"] = _git_bash_path(tmp_path / "tool.log")
    onboard = subprocess.run(
        [BASH, _git_bash_path(SETUP), "onboard", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert onboard.returncode == 0, onboard.stdout + onboard.stderr
    credentials = json.loads(
        (tmp_path / "state" / "credentials.json").read_text(encoding="utf-8")
    )
    [(agent, original)] = credentials["/owner/repo"].items()
    return env, repo, agent, original


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
    proc, out_path, err_path = _spawn_monitor(env, repo, tmp, "canary")
    time.sleep(3)
    alive = proc.poll() is None
    _stdout, stderr = _stop_and_read(proc, out_path, err_path)
    # This fixture builds its own directory, so the per-test sweep never sees it.
    _sweep_monitors(tmp)

    if not alive:
        pytest.fail(
            "the monitor exited immediately with valid arguments, so nothing in "
            "this module is testing what it says it tests. Usually PATH: the "
            "script needs coreutils (`dirname`) from Git's usr/bin, which a "
            "native Windows shell does not put on PATH but Git Bash does. "
            f"stderr was: {stderr!r}"
        )


@pytest.fixture(autouse=True)
def _no_monitor_outlives_its_test(tmp_path: Path):
    """Kill every monitor this test started, by the pid the monitor itself wrote.

    `claude-win-home-1` measured 18 live monitors left behind by a few runs of
    this module, each holding a connection and each named after a fixture's
    throwaway project. Two correct behaviours compose into that: where the owner
    cannot be observed the monitor is deliberately immortal, and Windows offers
    Python no stop that runs the EXIT trap -- `Popen.terminate()` is
    TerminateProcess exactly as `taskkill /F` is.

    So the sweep reads the record rather than the handle. `Popen.pid` is the
    process Python started, and MSYS `exec` replaces that with another Windows
    process while keeping the shell pid, so the handle can name something that
    is already gone. The metadata carries `$$` from after the re-exec, which is
    the live one -- and it is killed through the monitor's own bash, because on
    Windows that pid means something only inside MSYS.

    Scoped to this test's own state directory, so a monitor another project or
    another agent is legitimately holding is never touched.
    """
    yield
    _sweep_monitors(tmp_path)


def _sweep_monitors(state_root: Path) -> None:
    watch_dir = state_root / "state" / "watch"
    pids: set[int] = set()
    for record in watch_dir.glob("monitor-*.json"):
        with contextlib.suppress(OSError, ValueError, KeyError):
            pids.add(int(json.loads(record.read_text(encoding="utf-8"))["pid"]))
    for marker in watch_dir.glob("monitor-*.pid"):
        with contextlib.suppress(OSError, ValueError):
            pids.add(int(marker.read_text(encoding="utf-8").strip()))
    if pids:
        subprocess.run(
            [BASH, "-c", "kill -9 " + " ".join(str(pid) for pid in sorted(pids))],
            check=False,
            capture_output=True,
        )


def _spawn_monitor(
    env: dict[str, str], repo: Path, log_dir: Path, tag: str
) -> tuple[subprocess.Popen[bytes], Path, Path]:
    """Start the monitor with its output going to FILES, never to pipes.

    A pipe reaches EOF only once every holder of the write end is gone, and the
    monitor is a bash script that spawns children. So `communicate()` waits on
    the whole descendant tree, not on the monitor -- and one orphan that
    outlives its parent (on Windows `taskkill /T` walks the tree by parent pid,
    so a re-parented grandchild is invisible to it) is enough to hold the pipe
    open for ever. That cost the Windows leg twice: first as a hang that took
    chunk 5 down on the 300s per-test ceiling, then, once the wait was bounded,
    as a red test whose message was about descriptors rather than about the
    monitor.

    Files remove the dependency instead of bounding it. Nothing has to close
    anything for the text to be readable, a straggler still writing is
    harmless, and what the assertions get back is what the monitor actually
    said. Our own handles are closed as soon as `Popen` has duplicated them,
    so the test process is never one of the holders.
    """
    out_path = log_dir / f"{tag}.out"
    err_path = log_dir / f"{tag}.err"
    with out_path.open("wb") as out, err_path.open("wb") as err:
        proc = subprocess.Popen(
            [BASH, _git_bash_path(MONITOR), "claude", "1"],
            cwd=repo,
            env=env,
            stdout=out,
            stderr=err,
        )
    return proc, out_path, err_path


def _read_output(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _wait_until(predicate, *, budget: float = 10.0) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def _bash_can_observe_pid(pid: int) -> bool:
    """Ask the monitor's own process namespace whether ``pid`` is still live."""
    return (
        subprocess.run(
            [BASH, "-c", f"kill -0 {pid} 2>/dev/null"],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def _recorded_monitor_pid(path: Path) -> int | None:
    """Read a monitor pid across the brief remove/replace window."""
    with contextlib.suppress(OSError, ValueError, KeyError):
        return int(json.loads(path.read_text(encoding="utf-8"))["pid"])
    return None


def _stop_and_read(
    proc: subprocess.Popen[bytes],
    out_path: Path,
    err_path: Path,
    *,
    budget: float = 15.0,
) -> tuple[str, str]:
    """Stop the monitor and read what it said, without ever blocking forever.

    Every wait here is on the direct child and nothing else, which is the only
    process this helper actually knows how to stop. Reading is unconditional:
    even if the child somehow outlives both attempts, the caller still gets the
    real text and fails -- or passes -- on its own terms.
    """
    if os.name == "nt":
        # No process groups to signal here; taskkill walks the tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            check=False,
            capture_output=True,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=budget)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=budget)
    return _read_output(out_path), _read_output(err_path)


def _spawn_resolved_monitor(
    env: dict[str, str],
    repo: Path,
    log_dir: Path,
    tag: str,
    *,
    project_key: str,
    agent_name: str,
    parent_pid: int,
) -> tuple[subprocess.Popen[bytes], Path, Path]:
    """Start the monitor at its internal argv, which is where the owner pid enters.

    The public two-argument form reads the owner from `$PPID`, so it can only
    ever be handed a pid the harness cannot choose. Naming it explicitly is what
    makes the "owner cannot be observed" condition reproducible anywhere rather
    than only on the host that happens to produce it.
    """
    out_path = log_dir / f"{tag}.out"
    err_path = log_dir / f"{tag}.err"
    with out_path.open("wb") as out, err_path.open("wb") as err:
        proc = subprocess.Popen(
            [
                BASH,
                _git_bash_path(MONITOR),
                "--resolved",
                "claude",
                "1",
                project_key,
                agent_name,
                str(parent_pid),
            ],
            cwd=repo,
            env=env,
            stdout=out,
            stderr=err,
        )
    return proc, out_path, err_path


def _bash_sees_its_parent() -> bool:
    """Whether the monitor's own bash can observe the process that started it.

    Spawned exactly the way the monitor is -- straight from Python -- because
    that is the whole question. Under Git Bash the answer is no: `$PPID` comes
    back as 1 and pid 1 is not a signallable entity there, so a probe that
    reports "absent" is describing itself, not the CLI. Measured rather than
    keyed off `os.name`, so a Git Bash that one day reports a real parent moves
    these assertions back on its own.
    """
    return (
        subprocess.run(
            [BASH, "-c", "kill -0 $PPID 2>/dev/null"],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def _force_kill(pid: int) -> None:
    """Stop a monitor the harness cannot reach through its Popen handle."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False,
            capture_output=True,
        )
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def _pid_bash_cannot_see() -> int:
    """A numeric pid that `kill -0` reports as absent, from the monitor's own bash.

    Asked of that bash rather than of Python on purpose: the whole point is what
    the script's own probe answers, and the two do not have to agree -- under
    Git Bash they demonstrably do not for a native parent.
    """
    reaped = subprocess.Popen([sys.executable, "-c", ""])
    reaped.wait(timeout=60)
    probe = subprocess.run(
        [BASH, "-c", f"kill -0 {reaped.pid} 2>/dev/null"],
        check=False,
        capture_output=True,
    )
    assert probe.returncode != 0, (
        f"pid {reaped.pid} was reused between reaping and probing, so this test "
        "would have proved nothing"
    )
    return reaped.pid


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

    proc, out_path, err_path = _spawn_monitor(env, repo, tmp_path, "alive")
    try:
        # Long enough to cross several iterations of the one-second backoff, so
        # this proves the loop continues rather than that one sleep is running.
        time.sleep(5)
        assert proc.poll() is None, "monitor exited without an identity"
    finally:
        stdout, _stderr = _stop_and_read(proc, out_path, err_path)

    assert stdout == "", f"monitor spoke when it had nothing to report: {stdout!r}"


def test_monitor_keeps_running_when_its_owner_cannot_be_observed(
    tmp_path: Path,
) -> None:
    """An owner the probe cannot see is not an owner that died.

    Every wait in the monitor begins by asking `kill -0 $PARENT_PID` whether the
    CLI is still there, and a negative answer ends the run. That is right when
    the answer means "gone" and catastrophic when it means "this probe cannot
    tell": a monitor is never restarted for the life of the CLI, so one wrong
    reading costs the whole session its instant delivery -- silently, since a
    healthy monitor prints nothing either.

    The condition is reproduced by naming an owner pid that the monitor's own
    bash reports as absent, which is exactly what a Git Bash monitor gets when
    its parent is a native Windows process outside the MSYS process table.
    Reproducing it through argv rather than through a host keeps the property
    testable on every platform instead of only on the one that exhibits it.
    """
    env, repo, _ = _monitor_env(tmp_path)
    watch_dir = tmp_path / "state" / "watch"

    # Identity comes from a real run rather than from a literal, so this test
    # cannot drift away from whatever the resolver actually produces here.
    probe, probe_out, probe_err = _spawn_monitor(env, repo, tmp_path, "resolve")
    try:
        assert _wait_until(lambda: bool(list(watch_dir.glob("monitor-*.json"))))
        record_path = next(iter(watch_dir.glob("monitor-*.json")))
        record = json.loads(
            record_path.read_text(encoding="utf-8")
        )
    finally:
        _stop_and_read(probe, probe_out, probe_err)
        # On Windows the Popen handle can name the bootstrap process that MSYS
        # replaced, while the live monitor recorded its own different pid. Stop
        # that process too or it keeps the project+Agent singleton and makes the
        # real subject below exit successfully as a duplicate.
        _sweep_monitors(tmp_path)

    probe_pid = int(record["pid"])
    # SIGKILL cannot run the EXIT trap, so the files remain. The singleton is
    # recoverable once its recorded owner is no longer observable; this is the
    # exact predicate `am_lock_acquire` uses before reclaiming the stale lock.
    assert _wait_until(lambda: not _bash_can_observe_pid(probe_pid)), (
        f"identity probe monitor {probe_pid} survived its metadata-directed stop"
    )
    proc, out_path, err_path = _spawn_resolved_monitor(
        env,
        repo,
        tmp_path,
        "unobservable",
        project_key=record["project_key"],
        agent_name=record["agent_name"],
        parent_pid=_pid_bash_cannot_see(),
    )
    try:
        assert _wait_until(
            lambda: (subject_pid := _recorded_monitor_pid(record_path)) is not None
            and subject_pid != probe_pid
        ), "the subject monitor never acquired the singleton"
        time.sleep(5)
        assert proc.poll() is None, (
            "the monitor read an unobservable owner as a dead one and exited"
        )
    finally:
        stdout, stderr = _stop_and_read(proc, out_path, err_path)

    assert stdout == "", f"monitor spoke when it had nothing to report: {stdout!r}"
    assert stderr == "", f"monitor complained about its own owner: {stderr!r}"


def test_onboard_persists_the_one_time_token_and_doctor_never_prints_it(
    tmp_path: Path,
) -> None:
    env, repo, _ = _monitor_env(tmp_path)
    fake_bin = tmp_path / "bin"
    _install_setup_server(fake_bin)
    argv_log = tmp_path / "argv.log"
    env["FAKE_ARGV_LOG"] = _git_bash_path(argv_log)

    first = subprocess.run(
        [BASH, _git_bash_path(SETUP), "onboard", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert first.returncode == 0, first.stderr
    assert "Agent Mail onboarding complete" in first.stdout
    assert "display name: BlueCastle" in first.stdout
    assert "notification sound: chime" in first.stdout
    assert "one-time-secret" not in first.stdout + first.stderr
    assert "one-time-secret" not in argv_log.read_text(encoding="utf-8")
    assert not (repo / ".agent-mail-project-id").exists()

    credentials = json.loads(
        (tmp_path / "state" / "credentials.json").read_text(encoding="utf-8")
    )
    [(agent_name, stored_token)] = credentials["/owner/repo"].items()
    assert agent_name.startswith("claude-") and agent_name.endswith("-1")
    assert stored_token == "one-time-secret"
    assert list((tmp_path / "state" / "granted").iterdir())

    again = subprocess.run(
        [BASH, _git_bash_path(SETUP), "onboard", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert again.returncode == 0, again.stderr
    assert "one-time-secret" not in again.stdout + again.stderr

    doctor = subprocess.run(
        [BASH, _git_bash_path(SETUP), "doctor", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert doctor.returncode == 0, "an optional night monitor is not mailbox damage"
    assert "server authentication: valid" in doctor.stdout
    assert "local marker: absent (optional" in doctor.stdout
    assert "monitor: not armed for this project and Agent (optional" in doctor.stdout
    assert "result: healthy" in doctor.stdout
    assert "one-time-secret" not in doctor.stdout + doctor.stderr


def test_rotate_token_persists_verifies_and_redacts_the_replacement(
    tmp_path: Path,
) -> None:
    env, repo, agent, original = _rotation_setup(tmp_path)
    rotated = subprocess.run(
        [BASH, _git_bash_path(SETUP), "rotate-token", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert rotated.returncode == 0, rotated.stdout + rotated.stderr
    credentials = json.loads(
        (tmp_path / "state" / "credentials.json").read_text(encoding="utf-8")
    )
    replacement = credentials["/owner/repo"][agent]
    assert replacement != original
    assert len(replacement) == 64
    assert set(replacement) <= set("0123456789abcdef")
    assert (tmp_path / "server-token").read_text(encoding="utf-8") == replacement
    journals = list((tmp_path / "state" / "credential-rotations").glob("*.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal == {
        "version": 1,
        "status": "idle",
        "project": "/owner/repo",
        "agent": agent,
    }
    exposed = rotated.stdout + rotated.stderr + (tmp_path / "argv.log").read_text(
        encoding="utf-8"
    )
    assert original not in exposed
    assert replacement not in exposed
    assert "value not displayed" in rotated.stdout
    assert (tmp_path / "rotation-count").read_text(encoding="utf-8") == "1"


def test_rotate_token_recovers_a_committed_response_loss_from_its_journal(
    tmp_path: Path,
) -> None:
    env, repo, agent, original = _rotation_setup(tmp_path)
    env["FAKE_LOSE_ROTATION_RESPONSE_ONCE"] = "1"
    interrupted = subprocess.run(
        [BASH, _git_bash_path(SETUP), "rotate-token", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert interrupted.returncode == 1
    credentials_path = tmp_path / "state" / "credentials.json"
    assert json.loads(credentials_path.read_text(encoding="utf-8"))["/owner/repo"][agent] == original
    [journal_path] = list(
        (tmp_path / "state" / "credential-rotations").glob("*.json")
    )
    pending = json.loads(journal_path.read_text(encoding="utf-8"))
    candidate = pending["new_registration_token"]
    assert pending["status"] == "pending"
    assert (tmp_path / "server-token").read_text(encoding="utf-8") == candidate
    assert original not in interrupted.stdout + interrupted.stderr
    assert candidate not in interrupted.stdout + interrupted.stderr
    assert candidate not in (tmp_path / "argv.log").read_text(encoding="utf-8")

    recovered = subprocess.run(
        [BASH, _git_bash_path(SETUP), "rotate-token", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    assert credentials["/owner/repo"][agent] == candidate
    completed = json.loads(journal_path.read_text(encoding="utf-8"))
    assert completed["status"] == "idle"
    assert "new_registration_token" not in completed
    assert (tmp_path / "rotation-count").read_text(encoding="utf-8") == "1"
    assert candidate not in recovered.stdout + recovered.stderr


def test_rotate_token_recovers_when_private_persist_failed_after_server_commit(
    tmp_path: Path,
) -> None:
    env, repo, agent, original = _rotation_setup(tmp_path)
    real_mv = subprocess.run(
        [BASH, "-lc", "command -v mv"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    env["REAL_MV"] = real_mv
    env["FAIL_CREDENTIAL_WRITE"] = "1"
    fake_mv = tmp_path / "bin" / "mv"
    fake_mv.write_text(
        """#!/usr/bin/env bash
last="${!#}"
if [[ "${FAIL_CREDENTIAL_WRITE:-0}" == 1 && "$last" == */credentials.json ]]; then
  exit 71
fi
exec "$REAL_MV" "$@"
""",
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)

    interrupted = subprocess.run(
        [BASH, _git_bash_path(SETUP), "rotate-token", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert interrupted.returncode == 1
    credentials_path = tmp_path / "state" / "credentials.json"
    assert json.loads(credentials_path.read_text(encoding="utf-8"))["/owner/repo"][agent] == original
    [journal_path] = list(
        (tmp_path / "state" / "credential-rotations").glob("*.json")
    )
    pending = json.loads(journal_path.read_text(encoding="utf-8"))
    candidate = pending["new_registration_token"]
    assert (tmp_path / "server-token").read_text(encoding="utf-8") == candidate
    assert candidate not in interrupted.stdout + interrupted.stderr

    repaired_env = {
        key: value for key, value in env.items() if key != "FAIL_CREDENTIAL_WRITE"
    }
    recovered = subprocess.run(
        [BASH, _git_bash_path(SETUP), "rotate-token", "claude", "1"],
        cwd=repo,
        env=repaired_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    assert credentials["/owner/repo"][agent] == candidate
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == "idle"
    assert (tmp_path / "rotation-count").read_text(encoding="utf-8") == "1"


def test_rotate_token_preserves_old_state_on_refusal_and_fails_closed_if_stale(
    tmp_path: Path,
) -> None:
    env, repo, agent, original = _rotation_setup(tmp_path)
    env["FAKE_ROTATE_REJECT"] = "1"
    refused = subprocess.run(
        [BASH, _git_bash_path(SETUP), "rotate-token", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refused.returncode == 1
    credentials_path = tmp_path / "state" / "credentials.json"
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    assert credentials["/owner/repo"][agent] == original
    assert (tmp_path / "server-token").read_text(encoding="utf-8") == original
    [journal_path] = list(
        (tmp_path / "state" / "credential-rotations").glob("*.json")
    )
    pending = json.loads(journal_path.read_text(encoding="utf-8"))
    candidate = pending["new_registration_token"]
    assert pending["status"] == "pending"
    assert original not in refused.stdout + refused.stderr
    assert candidate not in refused.stdout + refused.stderr
    assert (tmp_path / "tool.log").read_text(encoding="utf-8").splitlines().count(
        "rotate_registration_token"
    ) == 1

    # A credential change outside this journal is not something recovery may
    # guess through. It must stop before another rotation RPC or local overwrite.
    unrelated = "9" * 64
    credentials["/owner/repo"][agent] = unrelated
    credentials_path.write_text(json.dumps(credentials), encoding="utf-8")
    retry_env = {key: value for key, value in env.items() if key != "FAKE_ROTATE_REJECT"}
    stale = subprocess.run(
        [BASH, _git_bash_path(SETUP), "rotate-token", "claude", "1"],
        cwd=repo,
        env=retry_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert stale.returncode == 1
    assert json.loads(credentials_path.read_text(encoding="utf-8"))["/owner/repo"][agent] == unrelated
    assert json.loads(journal_path.read_text(encoding="utf-8")) == pending
    assert (tmp_path / "server-token").read_text(encoding="utf-8") == original
    assert (tmp_path / "tool.log").read_text(encoding="utf-8").splitlines().count(
        "rotate_registration_token"
    ) == 1
    assert unrelated not in stale.stdout + stale.stderr
    assert candidate not in stale.stdout + stale.stderr


def test_concurrent_local_rotate_commands_coalesce_under_the_identity_lock(
    tmp_path: Path,
) -> None:
    env, repo, agent, original = _rotation_setup(tmp_path)
    env["FAKE_ROTATE_DELAY"] = "1"
    command = [BASH, _git_bash_path(SETUP), "rotate-token", "claude", "1"]
    first = subprocess.Popen(
        command,
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        command,
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_stdout, first_stderr = first.communicate(timeout=30)
    second_stdout, second_stderr = second.communicate(timeout=30)

    assert first.returncode == 0, first_stdout + first_stderr
    assert second.returncode == 0, second_stdout + second_stderr
    credentials = json.loads(
        (tmp_path / "state" / "credentials.json").read_text(encoding="utf-8")
    )
    replacement = credentials["/owner/repo"][agent]
    assert replacement != original
    assert (tmp_path / "server-token").read_text(encoding="utf-8") == replacement
    assert (tmp_path / "rotation-count").read_text(encoding="utf-8") == "1"
    exposed = first_stdout + first_stderr + second_stdout + second_stderr
    assert original not in exposed
    assert replacement not in exposed


def test_onboard_persists_the_non_reissuable_token_before_repairable_name_marker(
    tmp_path: Path,
) -> None:
    env, repo, _ = _monitor_env(tmp_path)
    fake_bin = tmp_path / "bin"
    _install_setup_server(fake_bin)
    env["FAKE_ARGV_LOG"] = _git_bash_path(tmp_path / "argv.log")
    real_mv = subprocess.run(
        [BASH, "-lc", "command -v mv"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    env["REAL_MV"] = real_mv
    env["FAIL_GRANTED_WRITE"] = "1"
    fake_mv = fake_bin / "mv"
    fake_mv.write_text(
        """#!/usr/bin/env bash
if [[ "${FAIL_GRANTED_WRITE:-}" == 1 ]]; then
  for argument in "$@"; do
    [[ "$argument" == */granted/* ]] && exit 71
  done
fi
exec "$REAL_MV" "$@"
""",
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)

    interrupted = subprocess.run(
        [BASH, _git_bash_path(SETUP), "onboard", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert interrupted.returncode == 1
    credentials = json.loads(
        (tmp_path / "state" / "credentials.json").read_text(encoding="utf-8")
    )
    assert list(credentials["/owner/repo"].values()) == ["one-time-secret"]
    assert "one-time-secret" not in interrupted.stdout + interrupted.stderr

    repaired_env = {key: value for key, value in env.items() if key != "FAIL_GRANTED_WRITE"}
    repaired = subprocess.run(
        [BASH, _git_bash_path(SETUP), "onboard", "claude", "1"],
        cwd=repo,
        env=repaired_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert repaired.returncode == 0, repaired.stderr
    assert list((tmp_path / "state" / "granted").iterdir())
    assert "one-time-secret" not in repaired.stdout + repaired.stderr


def test_onboard_local_marker_is_explicit_and_hidden_only_locally(
    tmp_path: Path,
) -> None:
    env, repo, _ = _monitor_env(tmp_path)
    _install_setup_server(tmp_path / "bin")
    env["FAKE_ARGV_LOG"] = _git_bash_path(tmp_path / "argv.log")

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(SETUP),
            "onboard",
            "claude",
            "1",
            "--local-marker",
        ],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert (repo / ".agent-mail-project-id").read_text(encoding="utf-8") == (
        "private-project-uid\n"
    )
    exclude = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    exclude_path = Path(exclude)
    if not exclude_path.is_absolute():
        exclude_path = repo / exclude_path
    assert ".agent-mail-project-id" in exclude_path.read_text(encoding="utf-8")
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""

    doctor = subprocess.run(
        [BASH, _git_bash_path(SETUP), "doctor", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "local marker: present and hidden only in .git/info/exclude" in doctor.stdout

    (repo / ".gitignore").write_text(
        ".agent-mail-project-id\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", ".agent-mail-project-id"],
        check=True,
        capture_output=True,
        text=True,
    )
    unsafe = subprocess.run(
        [BASH, _git_bash_path(SETUP), "doctor", "claude", "1"],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert unsafe.returncode == 1
    assert "invalid public .gitignore entry" in unsafe.stdout
    assert "invalid tracked .agent-mail-project-id" in unsafe.stdout


def test_monitor_is_singleton_per_project_and_agent_with_diagnostic_argv(
    tmp_path: Path,
) -> None:
    """Repeated `/wake` is free; another project remains independently valid."""
    env, repo, _ = _monitor_env(tmp_path)
    first, first_out, first_err = _spawn_monitor(env, repo, tmp_path, "first")
    duplicate: subprocess.Popen[bytes] | None = None
    other: subprocess.Popen[bytes] | None = None
    duplicate_out = duplicate_err = other_out = other_err = tmp_path / "unused"
    try:
        watch_dir = tmp_path / "state" / "watch"
        assert _wait_until(lambda: len(list(watch_dir.glob("monitor-*.json"))) == 1)
        duplicate, duplicate_out, duplicate_err = _spawn_monitor(
            env,
            repo,
            tmp_path,
            "duplicate",
        )
        assert _wait_until(lambda: duplicate.poll() is not None)
        assert first.poll() is None
        assert len(list(watch_dir.glob("monitor-*.json"))) == 1

        other_repo = tmp_path / "other-repo"
        _init_git_repo(other_repo)
        subprocess.run(
            [
                "git",
                "-C",
                str(other_repo),
                "remote",
                "set-url",
                "origin",
                "git@github.com:owner/other.git",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        other, other_out, other_err = _spawn_monitor(
            env,
            other_repo,
            tmp_path,
            "other",
        )
        assert _wait_until(lambda: len(list(watch_dir.glob("monitor-*.json"))) == 2)
        assert first.poll() is None and other.poll() is None

        metadata = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in watch_dir.glob("monitor-*.json")
        ]
        assert {item["project_key"] for item in metadata} == {
            "/owner/repo",
            "/owner/other",
        }
        agent_names = {item["agent_name"] for item in metadata}
        assert len(agent_names) == 1
        resolved_agent = agent_names.pop()
        assert resolved_agent.startswith("claude-")
        assert resolved_agent.endswith("-1")
        if Path(f"/proc/{first.pid}/cmdline").is_file():
            argv = Path(f"/proc/{first.pid}/cmdline").read_bytes().replace(
                b"\0", b" "
            )
            assert b"--resolved" in argv
            assert b"/owner/repo" in argv
            assert resolved_agent.encode() in argv
    finally:
        if duplicate is not None:
            _stop_and_read(duplicate, duplicate_out, duplicate_err)
        if other is not None:
            _stop_and_read(other, other_out, other_err)
        _stop_and_read(first, first_out, first_err)


def test_monitor_exits_and_cleans_its_exact_record_when_cli_parent_exits(
    tmp_path: Path,
) -> None:
    """An orphan must not survive into the next CLI run -- where it can be seen.

    Ownership is only enforceable on a host whose shell can observe the process
    that started it, and Git Bash cannot: it reports `PPID=1`, and pid 1 is not
    signallable there. So this pins both halves of one contract rather than one
    half and a skip -- the orphan dies where death is detectable, and where it is
    not, the monitor deliberately outlives its CLI until the connection window
    ends. Which half runs is decided by measuring the shell, not by naming the
    platform.
    """
    env, repo, _ = _monitor_env(tmp_path)
    watch_dir = tmp_path / "state" / "watch"
    env.update(
        {
            "TEST_BASH": BASH,
            "TEST_MONITOR": _git_bash_path(MONITOR),
            "TEST_REPO": str(repo),
            "TEST_WATCH_DIR": str(watch_dir),
        }
    )
    helper = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import glob
import os
import subprocess
import time

child = subprocess.Popen(
    [os.environ["TEST_BASH"], os.environ["TEST_MONITOR"], "claude", "1"],
    cwd=os.environ["TEST_REPO"],
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    if glob.glob(os.path.join(os.environ["TEST_WATCH_DIR"], "monitor-*.json")):
        print(child.pid, flush=True)
        raise SystemExit(0)
    time.sleep(0.05)
raise SystemExit(2)
""",
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert helper.returncode == 0, helper.stderr
    assert helper.stdout.strip().isdigit()
    monitor_pid = int(helper.stdout.strip())

    def _record_gone() -> bool:
        return not list(watch_dir.glob("monitor-*.json")) and not list(
            watch_dir.glob("monitor-*.pid")
        )

    try:
        if _bash_sees_its_parent():
            assert _wait_until(_record_gone, budget=10), (
                "monitor retained its ownership record after the original CLI "
                "parent exited"
            )
        else:
            # The other half of the same contract, and the reason this is a
            # branch rather than a skip: where the owner cannot be observed the
            # monitor deliberately stops supervising it, so surviving the parent
            # is the specified behaviour and its cost -- the record clears when
            # the connection window ends, not when the CLI dies. Asserting it
            # keeps this platform covered instead of silently uncovered, and
            # turns red the day the probe starts working, which is exactly when
            # the branch above should take over.
            assert not _wait_until(_record_gone, budget=5), (
                "the monitor cleaned up after a parent it cannot observe, so "
                "either the latch is gone or this host now reports a real "
                "parent -- in both cases this test is measuring the wrong branch"
            )
    finally:
        # Nothing else will: on such a host the monitor is, correctly, immortal.
        _force_kill(monitor_pid)


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

    proc, _out_path, _err_path = _spawn_monitor(env, repo, tmp_path, "sigterm")
    time.sleep(3)
    assert proc.poll() is None, "monitor exited before the signal could be sent"

    sent = time.monotonic()
    proc.terminate()
    try:
        # `wait`, not `communicate`: the event under test is the monitor
        # exiting, and pipes reaching EOF is a different event that an orphaned
        # curl child can delay indefinitely. Waiting on the wrong one would
        # report a surviving grandchild as "monitor ignored SIGTERM" -- the
        # regression this test exists to catch, blamed on the wrong process.
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - failure path
        # Bounded on purpose: a test that fails must fail, not hang the run.
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=15)
        pytest.fail("monitor ignored SIGTERM")

    assert time.monotonic() - sent < 15
