"""Cross-client identity contract for installation scripts and runtime hooks."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import time
import tomllib
from pathlib import Path, PureWindowsPath

import pytest

from mcp_agent_mail.cli import _agent_state_component

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
HOOK_COMMON = ROOT / "scripts" / "hooks" / "agent_mail_common.sh"
LEGACY_INBOX_HOOK = ROOT / "scripts" / "hooks" / "check_inbox.sh"
ENDPOINT_CANARY = ROOT / "scripts" / "test_endpoints.sh"
TLDR_INSTALLER = ROOT / "scripts" / "install.sh"
AUTO_INSTALLER = (
    ROOT
    / "scripts"
    / "automatically_detect_all_installed_coding_agents_and_install_mcp_agent_mail_in_all.sh"
)
INTEGRATORS = {
    "claude": ROOT / "scripts" / "integrate_claude_code.sh",
    "codex": ROOT / "scripts" / "integrate_codex_cli.sh",
    "copilot": ROOT / "scripts" / "integrate_github_copilot.sh",
}
USER_SCOPE_BOOTSTRAP_INTEGRATORS = {
    "gemini": ROOT / "scripts" / "integrate_gemini_cli.sh",
    "factory": ROOT / "scripts" / "integrate_factory_droid.sh",
    "cursor": ROOT / "scripts" / "integrate_cursor.sh",
    "cline": ROOT / "scripts" / "integrate_cline.sh",
    "windsurf": ROOT / "scripts" / "integrate_windsurf.sh",
    "opencode": ROOT / "scripts" / "integrate_opencode.sh",
}
FORBIDDEN_CLIENT_TOOLCHAIN_COMMANDS = (
    "python",
    "python3",
    "uv",
    "uvx",
    "node",
    "npx",
)
INSTALLED_HOOK_SOURCES = (
    HOOK_COMMON,
    ROOT / "scripts" / "hooks" / "session_start.sh",
    ROOT / "scripts" / "hooks" / "inbox_check.sh",
    ROOT / "scripts" / "hooks" / "reservations_warn.sh",
    ROOT / "scripts" / "hooks" / "autoreserve.sh",
    ROOT / "scripts" / "hooks" / "session_end.sh",
    ROOT / "scripts" / "hooks" / "inbox_watch.sh",
    ROOT / "scripts" / "hooks" / "codex_notify.sh",
)


def _posix_tool_dirs() -> tuple[str, ...]:
    """Return PATH entries that keep git and curl visible to a raw Git Bash.

    A native Windows process commonly resolves ``git`` through
    ``Git/cmd/git.exe`` or ``Git/bin/git.exe`` while Git's ``curl.exe`` lives in
    ``Git/mingw64/bin``. Prefer a verified Git runtime containing both tools,
    then retain the directories of the individually discovered executables.
    """
    resolved_tools: dict[str, Path] = {}
    for command in ("git", "curl"):
        executable = shutil.which(command)
        if executable:
            resolved_tools[command] = Path(executable).resolve()

    candidates: list[Path] = []
    git = resolved_tools.get("git")
    if git is not None:
        for root in git.parents:
            runtime_found = False
            for runtime_name in ("mingw64", "mingw32"):
                runtime = root / runtime_name / "bin"
                if all(
                    any((runtime / name).is_file() for name in (tool, f"{tool}.exe"))
                    for tool in ("git", "curl")
                ):
                    candidates.append(runtime)
                    runtime_found = True
                    break
            if runtime_found:
                break

    candidates.extend(path.parent for path in resolved_tools.values())
    entries: list[str] = []
    for candidate in candidates:
        entry = _git_bash_path(candidate)
        if entry not in entries:
            entries.append(entry)
    return tuple(entries) or ("/usr/bin",)


def test_posix_tool_dirs_include_windows_runtime_for_git_cmd_shim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_root = tmp_path / "Git"
    cmd_git = git_root / "cmd" / "git.exe"
    runtime = git_root / "mingw64" / "bin"
    runtime_git = runtime / "git.exe"
    runtime_curl = runtime / "curl.exe"
    system_curl = tmp_path / "Windows" / "System32" / "curl.exe"
    for executable in (cmd_git, runtime_git, runtime_curl, system_curl):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"")

    def fake_which(command: str) -> str | None:
        return {
            "git": str(cmd_git),
            "curl": str(system_curl),
        }.get(command)

    monkeypatch.setattr(shutil, "which", fake_which)

    entries = _posix_tool_dirs()

    assert entries[0] == _git_bash_path(runtime)
    assert _git_bash_path(cmd_git.parent) in entries
    assert _git_bash_path(system_curl.parent) in entries


def _bash_executable() -> str:
    """Locate the Git for Windows shell that runs an integrator under test.

    Git for Windows ships two different binaries with this name and this helper
    wants the one the *installers* must not use, which reads as a contradiction
    until you see what each does to the environment:

    ``Git\\bin\\bash.exe`` is a 47 KB launcher. It re-initialises MSYS — sets
    ``MSYSTEM=MINGW64`` and **rebuilds PATH**, putting ``/mingw64/bin`` and
    ``/usr/bin`` in front of whatever it inherited. That is exactly right for a
    hook command, which Claude hands to ``cmd.exe`` with no MSYS environment at
    all, and exactly wrong here. Measured, same PATH handed to each:

        PATH=<fixture>/Portable Git/mingw64/bin:/usr/bin:/bin

        Git\\bin\\bash.exe        PATH[0]=/mingw64/bin   git=/mingw64/bin/git
        Git\\usr\\bin\\bash.exe    PATH[0]=<fixture>...   git=<fixture>.../git

    Every Windows fixture in this module injects its fakes — ``git``, ``cygpath``,
    ``uname``, ``jq`` — by putting a directory first on PATH. Under the launcher
    that ordering is discarded before the script starts, so the installer sees the
    real Git and the real cygpath, and the assertions compare against a simulation
    that never ran. The failure is silent and looks like a resolver bug, because
    the value that comes back is a genuine working path — just not the fixture's.

    So: the installers pick the launcher (a hook must bring its own environment),
    and the harness picks the raw shell (a test must keep the one it built).
    ``usr/bin`` is therefore preferred here, and it is preferred per root before
    falling back, since the raw shell is what makes the run reproducible.
    """
    discovered = shutil.which("bash")
    if os.name != "nt":
        return discovered or "bash"
    git = shutil.which("git")
    if git:
        roots = list(Path(git).resolve().parents)
        for suffix in (("usr", "bin", "bash.exe"), ("bin", "bash.exe")):
            for git_root in roots:
                candidate = git_root.joinpath(*suffix)
                if candidate.is_file():
                    return str(candidate)
    return discovered or "bash"


BASH = _bash_executable()


@pytest.mark.skipif(os.name != "nt", reason="Windows Git layout")
def test_bash_executable_finds_git_root_above_mingw64(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_root = tmp_path / "Git"
    git = git_root / "mingw64" / "bin" / "git.exe"
    bash = git_root / "bin" / "bash.exe"
    git.parent.mkdir(parents=True)
    bash.parent.mkdir(parents=True)
    git.write_bytes(b"")
    bash.write_bytes(b"")

    def fake_which(executable: str) -> str | None:
        if executable == "git":
            return str(git)
        if executable == "bash":
            return str(tmp_path / "Windows" / "System32" / "bash.exe")
        return None

    monkeypatch.setattr(shutil, "which", fake_which)

    assert _bash_executable() == str(bash)


@pytest.mark.skipif(os.name != "nt", reason="Windows Git layout")
def test_bash_executable_prefers_raw_shell_when_both_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness needs the shell that leaves its environment alone.

    Both binaries exist in a real Git for Windows tree, and a helper that only
    checks ``.name == "bash.exe"`` cannot tell the two answers apart — which is
    how this preference got inverted once already without any test noticing.

    ``Git/bin/bash.exe`` re-initialises MSYS and rebuilds PATH, discarding the
    ordering every fixture in this module uses to inject its fakes. The
    installers want exactly that (a hook starts from ``cmd.exe`` with no MSYS
    environment); a test driving those installers wants the opposite, because
    the PATH it constructed is the thing under test.
    """
    git_root = tmp_path / "Git"
    git = git_root / "usr" / "bin" / "git.exe"
    launcher = git_root / "bin" / "bash.exe"
    raw_shell = git_root / "usr" / "bin" / "bash.exe"
    git.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    git.write_bytes(b"")
    launcher.write_bytes(b"")
    raw_shell.write_bytes(b"")

    def fake_which(executable: str) -> str | None:
        if executable == "git":
            return str(git)
        if executable == "bash":
            return str(raw_shell)
        return None

    monkeypatch.setattr(shutil, "which", fake_which)

    assert _bash_executable() == str(raw_shell)
    assert _bash_executable() != str(launcher)


def _git_bash_path(path: str | Path) -> str:
    """Return a host path in the path dialect understood by Git Bash."""
    value = str(path)
    if os.name != "nt":
        return value
    normalized = value.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        return f"/{normalized[0].lower()}{normalized[2:]}"
    return normalized


def _install_bash_command_forwarder(
    fake_bin: Path,
    command: str,
    target: str | Path,
) -> Path:
    forwarder = fake_bin / command
    forwarder.write_text(
        "#!/usr/bin/env bash\n"
        f"exec {shlex.quote(_git_bash_path(target))} \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    forwarder.chmod(0o700)
    return forwarder


def _install_simulated_windows_target_bash(
    executable: Path,
    target_tools: Path,
) -> None:
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/bin/sh\n"
        "if [ \"${5:-}\" = agent-mail-runtime-discovery ]; then\n"
        "  candidate=\"$FAKE_TARGET_TOOL_DIR/${7:-}\"\n"
        "  [ -x \"$candidate\" ] || exit 71\n"
        "  printf '%s\\n' \"$candidate\"\n"
        "  exit 0\n"
        "fi\n"
        "case \"${1:-}\" in\n"
        "  'D:\\Profiles\\Copilot\\hooks\\mcp-agent-mail\\hook_wrapper.sh')\n"
        "    shift\n"
        "    set -- \"$FAKE_COPILOT_POSIX/hooks/mcp-agent-mail/hook_wrapper.sh\" \"$@\"\n"
        "    ;;\n"
        "esac\n"
        f"exec {shlex.quote(_git_bash_path(BASH))} \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    executable.chmod(0o700)
    assert target_tools.is_dir()


def _install_simulated_wslpath(fake_bin: Path) -> None:
    fake_wslpath = fake_bin / "wslpath"
    fake_wslpath.write_text(
        r"""#!/usr/bin/env bash
case "$1" in
  -u)
    case "$2" in
      'D:\Profiles\Copilot') printf '%s\n' "$FAKE_COPILOT_POSIX" ;;
      'D:\Profiles\AppData\Roaming') printf '%s\n' '/mnt/d/Profiles/AppData/Roaming' ;;
      'D:\Portable Git\bin\bash.exe') printf '%s\n' "$FAKE_TARGET_BASH_POSIX" ;;
      *) printf '%s\n' "$2" ;;
    esac ;;
  -w|-m)
    case "$2" in
      "$FAKE_TARGET_BASH_POSIX") printf '%s\n' 'D:\Portable Git\bin\bash.exe' ;;
      *) printf '%s\n' 'D:\Profiles\Copilot\hooks\mcp-agent-mail\hook_wrapper.sh' ;;
    esac ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_wslpath.chmod(0o700)


def _simulated_wsl_windows_copilot_env(
    tmp_path: Path,
    *,
    include_target_jq: bool,
) -> tuple[dict[str, str], Path, Path, Path]:
    home = tmp_path / "home"
    outer_tools = tmp_path / "wsl tools"
    target_tools = tmp_path / "Windows target tools"
    target_bash = tmp_path / "target Git" / "bin" / "bash.exe"
    copilot_dir = home / "copilot-profile"
    for directory in (home, outer_tools, target_tools):
        directory.mkdir(parents=True)
    tool_paths = {tool: shutil.which(tool) for tool in ("git", "curl", "jq")}
    assert all(tool_paths.values())
    for tool, tool_path in tool_paths.items():
        assert tool_path is not None
        _install_bash_command_forwarder(outer_tools, tool, tool_path)
        if tool != "jq" or include_target_jq:
            _install_bash_command_forwarder(target_tools, tool, tool_path)
    fake_uname = outer_tools / "uname"
    fake_uname.write_text(
        "#!/usr/bin/env bash\nprintf 'Linux\\n'\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_uname.chmod(0o700)
    _install_simulated_wslpath(outer_tools)
    _install_simulated_windows_target_bash(target_bash, target_tools)
    env = {
        **os.environ,
        **_integration_env(home, outer_tools),
        "AGENT_MAIL_ENV_FILE": _git_bash_path(home / "agent-mail.env"),
        "AGENT_MAIL_GIT_BASH_PATH": r"D:\Portable Git\bin\bash.exe",
        "COPILOT_HOME": r"D:\Profiles\Copilot",
        "FAKE_COPILOT_POSIX": _git_bash_path(copilot_dir),
        "FAKE_TARGET_BASH_POSIX": _git_bash_path(target_bash),
        "FAKE_TARGET_TOOL_DIR": _git_bash_path(target_tools),
        "VSCODE_MCP_CONFIG_PATH": _git_bash_path(home / "vscode" / "mcp.json"),
        "WSL_DISTRO_NAME": "Ubuntu",
    }
    return env, copilot_dir, outer_tools, target_tools


def _install_forbidden_client_toolchain_guards(fake_bin: Path) -> None:
    fake_bin.mkdir(parents=True, exist_ok=True)
    for command in FORBIDDEN_CLIENT_TOOLCHAIN_COMMANDS:
        guard = fake_bin / command
        guard.write_text(
            "#!/usr/bin/env bash\n"
            f"printf 'forbidden client dependency invoked: {command}\\n' >&2\n"
            "exit 97\n",
            encoding="utf-8",
            newline="\n",
        )
        guard.chmod(0o700)


def test_bash_command_forwarder_preserves_the_original_tool_location(
    tmp_path: Path,
) -> None:
    tool_dir = tmp_path / "Chocolatey" / "bin"
    fake_bin = tmp_path / "fake bin"
    tool_dir.mkdir(parents=True)
    fake_bin.mkdir()
    payload = tool_dir / "jq.payload"
    payload.write_text("original-tool-location\n", encoding="utf-8", newline="\n")
    target = tool_dir / "jq-target"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'cat "$(dirname "$0")/jq.payload"\n'
        "printf '|%s' \"$1\"\n",
        encoding="utf-8",
        newline="\n",
    )
    target.chmod(0o700)
    forwarder = _install_bash_command_forwarder(fake_bin, "jq", target)

    result = subprocess.run(
        [BASH, _git_bash_path(forwarder), "argument with spaces"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "original-tool-location\n|argument with spaces"
    assert not forwarder.is_symlink()


def test_project_claude_template_is_an_inert_installer_pointer() -> None:
    """The tracked template must never recreate project-scoped MCP or hooks."""
    template_path = ROOT / ".claude" / "settings.json.template"
    template_text = template_path.read_text(encoding="utf-8")
    template = json.loads(template_text)

    assert set(template) == {"_DEPRECATED_TEMPLATE"}
    assert "integrate_claude_code.sh" in template_text
    assert "mcpServers" not in template
    assert "hooks" not in template
    assert "YOUR_BEARER_TOKEN" not in template_text


def test_mac_bash_32_case_labels_inside_command_substitutions_are_balanced() -> None:
    """Keep case labels parseable by the Bash 3.2 shipped with macOS."""
    shared_lib = LIB.read_text(encoding="utf-8")
    codex_integrator = INTEGRATORS["codex"].read_text(encoding="utf-8")

    assert "\n          (AGENT_MAIL_URL|" in shared_lib
    assert "|AGENT_MAIL_*_SLOT)\n" in shared_lib
    assert "\n  (session-end) export AGENT_MAIL_HOOK_TIMEOUT='2' ;;" in codex_integrator
    assert "\n  (*) export AGENT_MAIL_HOOK_TIMEOUT='6' ;;" in codex_integrator


def test_copilot_runtime_path_dedupe_is_bash_32_nounset_safe() -> None:
    copilot_integrator = INTEGRATORS["copilot"].read_text(encoding="utf-8")

    assert (
        'for existing in ${_COPILOT_RUNTIME_PATH_DIRS[@]+"'
        '${_COPILOT_RUNTIME_PATH_DIRS[@]}"}; do'
    ) in copilot_integrator


def _bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    # The hooks honour "environment wins" over the shared env file, so any
    # Agent Mail value inherited from the HOST leaks straight into an
    # assertion: on a machine whose repo `.env` holds a live bearer (leaked
    # into this process by an import-time load_dotenv), the token printed by a
    # fixture is the production one, not the fixture's. Green on CI, red on
    # every operator machine — and the diff prints the real secret. Neutralize
    # here; a test that wants one of these sets it explicitly via `env`.
    for leaked in (
        "HTTP_BEARER_TOKEN",
        "AGENT_MAIL_TOKEN",
        "AGENT_MAIL_URL",
        "AGENT_MAIL_STATE_DIR",
        "AGENT_MAIL_ENV_FILE",
    ):
        process_env.pop(leaked, None)
    process_env.update(env or {})
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        env=process_env,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "raw",
    [
        "/owner/repo",
        "/owner/répô/with spaces",
        f"/owner/{'long-segment-' * 20}",
    ],
)
def test_shell_and_python_state_components_are_identical(raw: str) -> None:
    result = _bash(
        f"source {shlex.quote(_git_bash_path(HOOK_COMMON))}; am_state_component {shlex.quote(raw)}"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == _agent_state_component(raw)


def test_state_digest_fails_without_a_sha256_provider() -> None:
    result = _bash(
        f"""
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        command() {{
          if [ "$1" = -v ]; then return 1; fi
          builtin command "$@"
        }}
        if am_sha256 <<<'state'; then exit 9; fi
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("uname_value", "extra_setup", "client", "slot", "expected"),
    [
        ("Linux", "grep() { return 1; }", "claude", "1", "claude-linux-labbox-1"),
        (
            "Linux",
            "export WSL_DISTRO_NAME=Ubuntu",
            "codex",
            "2",
            "codex-wsl-labbox-2",
        ),
        ("MINGW64_NT-10.0", "", "copilot", "1", "copilot-win-labbox-1"),
        (
            "Darwin",
            "scutil() { if [ \"$2\" = HostName ]; then printf 'Stable Mac'; else return 1; fi; }",
            "gemini",
            "3",
            "gemini-mac-stablemac-3",
        ),
        ("Linux", "grep() { return 1; }", "factory", "1", "factory-linux-labbox-1"),
        ("Linux", "grep() { return 1; }", "cursor", "2", "cursor-linux-labbox-2"),
        ("Linux", "grep() { return 1; }", "cline", "1", "cline-linux-labbox-1"),
        ("Linux", "grep() { return 1; }", "windsurf", "1", "windsurf-linux-labbox-1"),
        ("Linux", "grep() { return 1; }", "opencode", "4", "opencode-linux-labbox-4"),
    ],
)
def test_integration_agent_name_is_cross_platform_and_client_scoped(
    uname_value: str,
    extra_setup: str,
    client: str,
    slot: str,
    expected: str,
) -> None:
    result = _bash(
        f"""
        source {shlex.quote(_git_bash_path(LIB))}
        uname() {{ printf '%s' '{uname_value}'; }}
        hostname() {{ printf 'Lab Box!'; }}
        {extra_setup}
        integration_agent_name '{client}' '{slot}'
        """,
        env={"AGENT_NAME": "", "WSL_DISTRO_NAME": ""},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    ("script_name", "client", "slot_variable"),
    [
        ("integrate_gemini_cli.sh", "gemini", "AGENT_MAIL_GEMINI_SLOT"),
        ("integrate_factory_droid.sh", "factory", "AGENT_MAIL_FACTORY_SLOT"),
        ("integrate_cursor.sh", "cursor", "AGENT_MAIL_CURSOR_SLOT"),
        ("integrate_cline.sh", "cline", "AGENT_MAIL_CLINE_SLOT"),
        ("integrate_windsurf.sh", "windsurf", "AGENT_MAIL_WINDSURF_SLOT"),
        ("integrate_opencode.sh", "opencode", "AGENT_MAIL_OPENCODE_SLOT"),
    ],
)
def test_shipped_bootstrap_integrators_use_durable_client_host_slot_identity(
    script_name: str,
    client: str,
    slot_variable: str,
) -> None:
    script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")

    assert f"integration_agent_name {client}" in script
    assert slot_variable in script
    assert "integration_project_key \"$TARGET_DIR\"" in script
    assert "--arg name \"$_AGENT\"" in script
    assert "registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN" in script
    assert "am_cred_get" in script
    assert "am_cred_put" in script
    assert f'${{USER:-{client}}}' not in script
    assert "auto-generate adjective+noun" not in script


def test_hook_identity_cannot_be_replaced_by_an_ambient_agent_override(
    tmp_path: Path,
) -> None:
    result = _bash(
        f"""
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        uname() {{ printf Linux; }}
        hostname() {{ printf 'Lab-Box'; }}
        grep() {{ return 1; }}
        am_agent_name codex 1
        """,
        env={
            "AGENT_MAIL_AGENT": "manual-noncanonical-name",
            "AGENT_MAIL_ENV_FILE": _git_bash_path(tmp_path / "missing.env"),
            "AGENT_MAIL_STATE_DIR": _git_bash_path(tmp_path / "state"),
            "WSL_DISTRO_NAME": "",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "codex-linux-lab-box-1"


def test_integration_project_key_uses_origin_identity(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:owner/repo.git"],
        check=True,
        capture_output=True,
        text=True,
    )
    result = _bash(
        f"source {shlex.quote(_git_bash_path(LIB))}; "
        f"integration_project_key {shlex.quote(_git_bash_path(repo))}"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "/owner/repo"


def test_hook_does_not_load_client_or_slot_from_shared_env(tmp_path: Path) -> None:
    env_file = tmp_path / "agent-mail.env"
    env_file.write_text(
        "AGENT_MAIL_CLIENT=codex\n"
        "AGENT_MAIL_SLOT=9\n"
        "AGENT_MAIL_AGENT=wrong-agent\n"
        "AGENT_MAIL_PROJECT_KEY=/wrong/project\n"
        "AGENT_MAIL_REGISTRATION_TOKEN=wrong-token\n",
        encoding="utf-8",
        newline="\n",
    )

    result = _bash(
        f"""
        export AGENT_MAIL_ENV_FILE={shlex.quote(_git_bash_path(env_file))}
        unset AGENT_MAIL_CLIENT AGENT_MAIL_SLOT AGENT_MAIL_AGENT
        unset AGENT_MAIL_PROJECT_KEY AGENT_MAIL_REGISTRATION_TOKEN
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        printf '%s|%s|%s|%s|%s' \
          "${{AGENT_MAIL_CLIENT-unset}}" "${{AGENT_MAIL_SLOT-unset}}" \
          "${{AGENT_MAIL_AGENT-unset}}" "${{AGENT_MAIL_PROJECT_KEY-unset}}" \
          "${{AGENT_MAIL_REGISTRATION_TOKEN-unset}}"
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "unset|unset|unset|unset|unset"


def test_integrators_keep_client_and_slot_out_of_shared_env() -> None:
    for client, path in INTEGRATORS.items():
        script = path.read_text(encoding="utf-8")
        slot_name = f"AGENT_MAIL_{client.upper()}_SLOT"

        assert "integration_agent_name" in script
        assert slot_name in script
        assert "env_file_put AGENT_MAIL_CLIENT" not in script
        assert "env_file_put AGENT_MAIL_SLOT" not in script


def test_integrators_fail_fast_for_their_actual_runtime_dependencies() -> None:
    expected = {
        "claude": ("jq", "curl", "git"),
        "codex": ("jq", "curl", "git"),
        "copilot": ("jq", "curl", "git"),
    }
    for client, commands in expected.items():
        script = INTEGRATORS[client].read_text(encoding="utf-8")
        for command in commands:
            assert f"require_cmd {command}" in script
    for path in USER_SCOPE_BOOTSTRAP_INTEGRATORS.values():
        script = path.read_text(encoding="utf-8")
        for command in ("jq", "curl", "git"):
            assert f"require_cmd {command}" in script


def _forbidden_client_dependency_matches(text: str) -> list[str]:
    command = "(?:" + "|".join(FORBIDDEN_CLIENT_TOOLCHAIN_COMMANDS) + ")"
    patterns = (
        rf"(?m)^[ \t]*(?:exec[ \t]+)?{command}(?=[ \t]|$)",
        rf"\brequire_cmd[ \t]+{command}\b",
        rf"\bcommand[ \t]+-v[ \t]+{command}\b",
        rf"\benv[ \t]+{command}(?=[ \t]|$)",
        rf"(?:&&|\|\||[|;(])[ \t]*{command}(?=[ \t]|$)",
        r"\b(?:uv|uvx)[ \t]+run\b",
        r"\b(?:python|python3)[ \t]+(?:-[cm]\b|<<)",
        rf'["\']command["\'][ \t]*[:=][ \t]*["\']{command}\b',
        r"(?:/|\\)\.venv(?:/|\\)[^\n]*\bpython3?\b",
    )
    return [match.group(0) for pattern in patterns for match in re.finditer(pattern, text)]


@pytest.mark.parametrize(
    "path",
    [
        AUTO_INSTALLER,
        *INTEGRATORS.values(),
        *USER_SCOPE_BOOTSTRAP_INTEGRATORS.values(),
        *INSTALLED_HOOK_SOURCES,
    ],
    ids=lambda path: Path(path).name,
)
def test_client_installation_sources_do_not_invoke_language_toolchains(
    path: Path,
) -> None:
    matches = _forbidden_client_dependency_matches(path.read_text(encoding="utf-8"))

    assert matches == [], f"{path.relative_to(ROOT)} invokes {matches}"


def test_shared_env_merge_preserves_unmanaged_lines_and_removes_identity(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    old_state = tmp_path / "old-state"
    home.mkdir()
    old_state.mkdir()
    env_file = home / ".agent-mail.env"
    env_file.write_text(
        "# operator note\n"
        "UNRELATED_SETTING=keep-me\n"
        "AGENT_MAIL_URL=http://old/mcp/\n"
        "HTTP_BEARER_TOKEN=old\n"
        f"AGENT_MAIL_STATE_DIR={_git_bash_path(old_state)}\n"
        "AGENT_MAIL_AGENT=old-agent\n"
        "AGENT_MAIL_PROJECT_KEY=/old/project\n"
        "AGENT_MAIL_CLIENT=claude\n"
        "AGENT_MAIL_SLOT=9\n"
        "AGENT_MAIL_CODEX_SLOT=2\n"
        "AGENT_MAIL_REGISTRATION_TOKEN=registration-secret\n",
        encoding="utf-8",
        newline="\n",
    )

    result = _bash(
        f"""
        export HOME={shlex.quote(_git_bash_path(home))}
        export XDG_STATE_HOME={shlex.quote(_git_bash_path(state))}
        export AGENT_MAIL_ENV_FILE={shlex.quote(_git_bash_path(env_file))}
        export DRY_RUN=0
        source {shlex.quote(_git_bash_path(LIB))}
        write_shared_agent_mail_env https://hermes.example/mcp/ bearer-123
        """
    )

    assert result.returncode == 0, result.stderr
    contents = env_file.read_text(encoding="utf-8")
    assert "# operator note" in contents
    assert "UNRELATED_SETTING=keep-me" in contents
    assert "AGENT_MAIL_URL=https://hermes.example/mcp/" in contents
    assert "HTTP_BEARER_TOKEN=bearer-123" in contents
    assert f"AGENT_MAIL_STATE_DIR={_git_bash_path(old_state)}" in contents
    for forbidden in (
        "AGENT_MAIL_AGENT=",
        "AGENT_MAIL_PROJECT_KEY=",
        "AGENT_MAIL_CLIENT=",
        "AGENT_MAIL_SLOT=",
        "AGENT_MAIL_CODEX_SLOT=",
        "AGENT_MAIL_REGISTRATION_TOKEN=",
    ):
        assert forbidden not in contents
    assert list((old_state / "backups").glob("*.bak"))


def test_user_config_backups_do_not_overwrite_with_same_timestamp(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    config = home / "config.json"
    config.write_text('{"version": 1}\n', encoding="utf-8")

    result = _bash(
        f"""
        export HOME={shlex.quote(_git_bash_path(home))}
        export XDG_STATE_HOME={shlex.quote(_git_bash_path(state))}
        export DRY_RUN=0
        source {shlex.quote(_git_bash_path(LIB))}
        date() {{ printf '20260808_120000'; }}
        backup_user_file {shlex.quote(_git_bash_path(config))}
        backup_user_file {shlex.quote(_git_bash_path(config))}
        """
    )

    assert result.returncode == 0, result.stderr
    backups = list((state / "agent-mail" / "backups").glob("*.bak"))
    assert len(backups) == 2
    assert {path.read_text(encoding="utf-8") for path in backups} == {'{"version": 1}\n'}


@pytest.mark.parametrize(
    ("uname_value", "environment", "expected"),
    [
        (
            "Linux",
            {"HOME": "/home/test", "XDG_CONFIG_HOME": "/xdg"},
            "/xdg/Code/User/mcp.json",
        ),
        (
            "Darwin",
            {"HOME": "/Users/test", "XDG_CONFIG_HOME": ""},
            "/Users/test/Library/Application Support/Code/User/mcp.json",
        ),
        (
            "MINGW64_NT-10.0",
            {"HOME": "/c/Users/test", "APPDATA": "/c/Users/test/AppData/Roaming"},
            "/c/Users/test/AppData/Roaming/Code/User/mcp.json",
        ),
    ],
)
def test_vscode_user_mcp_path_is_platform_global(
    uname_value: str,
    environment: dict[str, str],
    expected: str,
) -> None:
    result = _bash(
        f"source {shlex.quote(_git_bash_path(LIB))}; "
        f"uname() {{ printf '%s' '{uname_value}'; }}; integration_vscode_user_mcp_path",
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_integrators_contain_no_project_local_client_writes() -> None:
    forbidden = (
        "${TARGET_DIR}/.claude",
        "${TARGET_DIR}/.codex",
        "${TARGET_DIR}/.factory",
        "${TARGET_DIR}/.gemini",
        "codex.mcp.json",
        "factory.mcp.json",
        "gemini.mcp.json",
        "cursor.mcp.json",
        "cline.mcp.json",
        "windsurf.mcp.json",
        '${TARGET_DIR}/opencode.json',
        ".vscode/mcp.json",
        'ensure_gitignore_entry "${TARGET_DIR}',
        "run_server_with_token.sh",
        "update_env_var \"HTTP_BEARER_TOKEN\"",
        "generate_bearer_token",
    )
    for path in (*INTEGRATORS.values(), *USER_SCOPE_BOOTSTRAP_INTEGRATORS.values()):
        script = path.read_text(encoding="utf-8")
        for fragment in forbidden:
            assert fragment not in script, f"{path.name} still contains {fragment}"


def test_user_scope_bootstrap_integrators_keep_credentials_off_output_and_argv() -> None:
    for path in USER_SCOPE_BOOTSTRAP_INTEGRATORS.values():
        script = path.read_text(encoding="utf-8")

        assert "resolve_global_integration_bearer_token" in script
        assert "write_shared_agent_mail_env" in script
        assert "backup_user_file" in script
        assert "backup_file" not in script
        assert "am_call ensure_project" in script
        assert "am_call register_agent" in script
        assert "_rpc_call" not in script
        assert "--argjson arguments" not in script
        assert "<(printf" not in script
        assert 'Authorization: Bearer ${_TOKEN}' not in script
        assert '--arg token "$_TOKEN"' not in script
        assert "-d \"{\\\"jsonrpc" not in script
        assert "Registration credential: stored privately" in script


def test_legacy_inbox_hook_uses_private_common_transport() -> None:
    script = LEGACY_INBOX_HOOK.read_text(encoding="utf-8")

    assert 'agent_mail_common.sh"' in script
    assert 'am_call fetch_inbox "$ARGS_JSON"' in script
    assert "AGENT_MAIL_JQ_REGISTRATION_TOKEN" in script
    assert "CURL_ARGS" not in script
    assert 'Authorization: Bearer ${TOKEN}' not in script
    assert "REG_TOKEN_JSON" not in script


def test_endpoint_canary_requires_preprovisioned_identity_and_redacts_output() -> None:
    script = ENDPOINT_CANARY.read_text(encoding="utf-8")

    assert "AGENT_MAIL_PROJECT" in script
    assert "AGENT_MAIL_AGENT" in script
    assert "AGENT_MAIL_REGISTRATION_TOKEN" in script
    assert "register_agent" not in script
    assert "am_call start_agent_execution" in script
    assert "am_call end_agent_execution" in script
    assert "registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN" in script
    assert "execution_token:env.AGENT_MAIL_JQ_EXECUTION_TOKEN" in script
    assert 'lifecycle_protocol_version:1' in script
    assert "Authorization: Bearer" not in script
    assert "curl " not in script


@pytest.mark.parametrize(
    "arguments",
    [
        ["--token", "installer-argv-secret"],
        ["--token=installer-argv-secret"],
    ],
)
def test_tldr_installer_rejects_token_argv_without_echoing_secret(
    arguments: list[str],
) -> None:
    script = TLDR_INSTALLER.read_text(encoding="utf-8")

    assert "INTEGRATION_TOKEN" not in script
    assert "--token" not in script
    result = subprocess.run(
        [BASH, "-x", _git_bash_path(TLDR_INSTALLER), *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "Unknown option" in output
    assert "installer-argv-secret" not in output


def test_hook_derives_stateless_base_from_streamable_mcp_url(tmp_path: Path) -> None:
    env_file = tmp_path / "agent-mail.env"
    env_file.write_text(
        "AGENT_MAIL_URL=https://hermes.example/mcp/\nHTTP_BEARER_TOKEN=test\n",
        encoding="utf-8",
        newline="\n",
    )
    result = _bash(
        f"""
        export AGENT_MAIL_ENV_FILE={shlex.quote(_git_bash_path(env_file))}
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        printf '%s' "$AM_BASE_URL"
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "https://hermes.example"


def _install_bash_env(home: Path, fake_bin: Path) -> tuple[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    bash_tmp = home / ".agent-mail-test-tmp"
    bash_tmp.mkdir(exist_ok=True)
    bash_tmp_path = _git_bash_path(bash_tmp)
    bash_env = home / ".agent-mail-test-bash-env"
    bash_env.write_text(
        f"export PATH={shlex.quote(_git_bash_path(fake_bin))}:\"$PATH\"\n"
        f"export TMPDIR={shlex.quote(bash_tmp_path)}\n"
        f"export TEMP={shlex.quote(bash_tmp_path)}\n"
        f"export TMP={shlex.quote(bash_tmp_path)}\n"
        "command() {\n"
        "  if [[ ${1:-} == -v ]]; then\n"
        "    case ${2:-} in\n"
        "      python|python3|uv|uvx|node|npx) return 1 ;;\n"
        "    esac\n"
        "  fi\n"
        "  builtin command \"$@\"\n"
        "}\n",
        encoding="utf-8",
        newline="\n",
    )
    return _git_bash_path(bash_env), str(bash_tmp)


def _integration_env(home: Path, fake_bin: Path) -> dict[str, str]:
    _install_forbidden_client_toolchain_guards(fake_bin)
    bash_env, bash_tmp = _install_bash_env(home, fake_bin)
    return {
        "HOME": _git_bash_path(home),
        "BASH_ENV": bash_env,
        "CODEX_HOME": _git_bash_path(home / ".codex"),
        "COPILOT_HOME": _git_bash_path(home / ".copilot"),
        "XDG_STATE_HOME": _git_bash_path(home / ".state"),
        "XDG_CONFIG_HOME": _git_bash_path(home / ".config"),
        # VS Code's user mcp.json is located through APPDATA on Windows, and this
        # helper used to leave it inherited. On a fresh runner that is harmless
        # because nothing lives there; on a developer's Windows machine the
        # installer reached the real %APPDATA%\Code\User\mcp.json, refused to
        # overwrite it — correctly — and failed the integration, and the test
        # with it. The refusal is the safety gate working; the hole is that the
        # test let the installer see the file at all. LOCALAPPDATA is pinned
        # alongside it so the pair cannot drift.
        "APPDATA": _git_bash_path(home / "appdata"),
        "LOCALAPPDATA": _git_bash_path(home / "localappdata"),
        "INTEGRATION_MCP_URL": "https://hermes.example/mcp/",
        "INTEGRATION_BEARER_TOKEN": "test-bearer",
        "PATH": ":".join(
            [
                _git_bash_path(fake_bin),
                *(
                    _git_bash_path(entry)
                    for entry in os.environ["PATH"].split(os.pathsep)
                    if entry
                ),
            ]
        ),
        "TMPDIR": bash_tmp,
        "TEMP": bash_tmp,
        "TMP": bash_tmp,
    }


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_integrators_install_lf_only_hooks_from_crlf_sources(
    tmp_path: Path,
    client: str,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    source_root = tmp_path / f"{client}-crlf-source"
    source_scripts = source_root / "scripts"
    source_hooks = source_scripts / "hooks"
    for directory in (home, project, fake_bin, source_hooks):
        directory.mkdir(parents=True)

    integrator = source_scripts / INTEGRATORS[client].name
    # Normalize the staged copies: under core.autocrlf the working tree may be
    # CRLF even though the blob is LF, and a CRLF integrator breaks under bash
    # before the behaviour under test is ever reached.
    integrator.write_bytes(
        INTEGRATORS[client].read_bytes().replace(b"\r\n", b"\n")
    )
    (source_scripts / "lib.sh").write_bytes(
        LIB.read_bytes().replace(b"\r\n", b"\n")
    )
    hook_names = (
        (
            "agent_mail_common.sh",
            "session_start.sh",
            "inbox_check.sh",
            "reservations_warn.sh",
            "autoreserve.sh",
            "session_end.sh",
            "inbox_watch.sh",
            "inbox_watch_monitor.sh",
        )
        if client == "claude"
        else ("codex_notify.sh", "agent_mail_common.sh")
    )
    for hook_name in hook_names:
        source = ROOT / "scripts" / "hooks" / hook_name
        # The contract under test is that the integrator PUBLISHES LF-only
        # hooks from CRLF sources; the checkout's own line endings (CRLF under
        # core.autocrlf) are not part of it, so normalize before re-CRLFing.
        source_bytes = source.read_bytes().replace(b"\r\n", b"\n")
        crlf_bytes = source_bytes.replace(b"\n", b"\r\n")
        assert b"\r\n" in crlf_bytes
        (source_hooks / hook_name).write_bytes(crlf_bytes)

    env = {**os.environ, **_integration_env(home, fake_bin)}
    if os.name == "nt":
        # Keep this regression focused on line-ending publication.  Native
        # launcher discovery has a separate simulated-Windows contract below.
        env["AGENT_MAIL_GIT_BASH_PATH"] = _git_bash_path(BASH)
    result = subprocess.run(
        [
            BASH,
            _git_bash_path(integrator),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=source_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    installed_dir = (
        home / ".claude" / "hooks" / "mcp-agent-mail"
        if client == "claude"
        else home / ".codex" / "hooks" / "mcp-agent-mail"
    )
    installed_hooks = sorted(installed_dir.glob("*.sh"))
    expected_installed_names = (
        set(hook_names)
        if client == "claude"
        else {"agent_mail_common.sh", "agent_mail_hook.sh", "hook_wrapper.sh"}
    )
    assert {path.name for path in installed_hooks} == expected_installed_names
    for installed_hook in installed_hooks:
        assert b"\r" not in installed_hook.read_bytes(), installed_hook.name
        syntax = subprocess.run(
            [BASH, "-n", _git_bash_path(installed_hook)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert syntax.returncode == 0, f"{installed_hook.name}: {syntax.stderr}"


def test_client_integration_environment_hides_language_toolchains(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    environment = {**os.environ, **_integration_env(home, fake_bin)}
    result = subprocess.run(
        [
            BASH,
            "--noprofile",
            "--norc",
            "-c",
            """
            for tool in python python3 uv uvx node npx; do
              if command -v "$tool" >/dev/null 2>&1; then
                printf 'unexpected dependency: %s\n' "$tool" >&2
                exit 90
              fi
            done
            """,
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _init_git_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:owner/repo.git"],
        check=True,
        capture_output=True,
        text=True,
    )


def _hook_env(home: Path, state: Path, fake_bin: Path) -> dict[str, str]:
    home.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    _install_forbidden_client_toolchain_guards(fake_bin)
    env_file = home / ".agent-mail.env"
    env_file.write_text(
        "AGENT_MAIL_URL=https://hermes.example/mcp/\nHTTP_BEARER_TOKEN=test-bearer\n",
        encoding="utf-8",
        newline="\n",
    )
    bash_env, bash_tmp = _install_bash_env(home, fake_bin)
    return {
        **os.environ,
        "HOME": _git_bash_path(home),
        "BASH_ENV": bash_env,
        "AGENT_MAIL_STATE_DIR": _git_bash_path(state),
        "AGENT_MAIL_ENV_FILE": _git_bash_path(env_file),
        "AGENT_MAIL_AGENT": "",
        "AGENT_MAIL_PROJECT_KEY": "",
        "PATH": ":".join(
            [
                _git_bash_path(fake_bin),
                *(
                    _git_bash_path(entry)
                    for entry in os.environ["PATH"].split(os.pathsep)
                    if entry
                ),
            ]
        ),
        "TMPDIR": bash_tmp,
        "TEMP": bash_tmp,
        "TMP": bash_tmp,
    }


def _hook_names(env: dict[str, str]) -> tuple[str, str, str]:
    result = _bash(
        f"""
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        printf '%s\n' "$(am_legacy_agent_name 1)"
        printf '%s\n' "$(am_agent_name claude 1)"
        printf '%s\n' "$(am_agent_name codex 1)"
        """,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    legacy, claude, codex = result.stdout.splitlines()
    return legacy, claude, codex


def _put_credential(state: Path, agent: str, token: str = "registration-token") -> None:
    (state / "credentials.json").write_text(
        json.dumps({"/owner/repo": {agent: token}}),
        encoding="utf-8",
    )


def _install_fake_curl(fake_bin: Path, response_script: str) -> Path:
    fake_bin.mkdir(exist_ok=True)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(response_script, encoding="utf-8", newline="\n")
    fake_curl.chmod(0o700)
    return fake_curl


def _assert_hook_private_mode(path: Path) -> None:
    """Assert 0600 where the backing filesystem exposes POSIX mode bits.

    Native Windows and default WSL DrvFS mounts report synthetic mode bits even
    after a successful chmod. The hook source assertions in the lifecycle test
    still prove the pre-create umask and explicit chmod contract on those hosts.
    """

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode == 0o600:
        return
    assert os.name == "nt" or str(path).startswith("/mnt/"), (
        f"private hook state {path} has mode {mode:o}"
    )


@pytest.mark.parametrize(
    ("client", "relative_config"),
    [
        ("gemini", ".gemini/settings.json"),
        ("factory", ".factory/mcp.json"),
        ("cursor", ".cursor/mcp.json"),
        ("cline", ".cline/data/settings/cline_mcp_settings.json"),
        ("windsurf", ".codeium/windsurf/mcp_config.json"),
        ("opencode", ".config/opencode/opencode.json"),
    ],
)
def test_user_scope_integrator_persists_and_resumes_registration_credential(
    tmp_path: Path,
    client: str,
    relative_config: str,
) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "checkout"
    _init_git_repo(repo)
    env = {**os.environ, **_integration_env(home, fake_bin)}
    env["CLINE_CONFIG_DIR"] = ""
    env["OPENCODE_CONFIG"] = ""
    request_log = tmp_path / "requests.jsonl"
    curl_argv_log = tmp_path / "curl-argv.log"
    env["FAKE_REQUEST_LOG"] = _git_bash_path(request_log)
    env["FAKE_CURL_ARGV_LOG"] = _git_bash_path(curl_argv_log)
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    *test-bearer*|*registration-secret*) exit 91 ;;
  esac
done
printf '%s\n' "$@" >> "$FAKE_CURL_ARGV_LOG"
body="$(cat)"
printf '%s\n' "$body" >> "$FAKE_REQUEST_LOG"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
case "$tool" in
  ensure_project) result='{"slug":"owner-repo"}' ;;
  register_agent)
    name="$(printf '%s' "$body" | jq -r '.params.arguments.name')"
    token="$(printf '%s' "$body" | jq -r '.params.arguments.registration_token // empty')"
    [[ -n "$token" ]] || token='registration-secret'
    result="$(AGENT_MAIL_FAKE_TOKEN="$token" jq -nc --arg name "$name" \
      '{name:$name,registration_token:env.AGENT_MAIL_FAKE_TOKEN,retired_at:null}')"
    ;;
  *) exit 92 ;;
esac
envelope="$(AGENT_MAIL_FAKE_RESULT="$result" jq -nc \
  '{result:{content:[{type:"text",text:env.AGENT_MAIL_FAKE_RESULT}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    real_jq = shutil.which("jq")
    assert real_jq is not None
    (fake_bin / "jq").write_text(
        "#!/usr/bin/env bash\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    *test-bearer*|*registration-secret*) exit 94 ;;\n"
        "  esac\n"
        "done\n"
        f"exec {shlex.quote(_git_bash_path(real_jq))} \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    (fake_bin / "jq").chmod(0o700)
    user_config = home / relative_config
    user_config.parent.mkdir(parents=True, exist_ok=True)
    server_key = "mcp" if client == "opencode" else "mcpServers"
    user_config.write_text(
        json.dumps({server_key: {"foreign": {"command": "keep"}}, "operator": "keep"})
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    before_repo_files = {
        path.relative_to(repo)
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }
    command = [
        BASH,
        _git_bash_path(USER_SCOPE_BOOTSTRAP_INTEGRATORS[client]),
        "--yes",
        "--debug",
        "--project-dir",
        _git_bash_path(repo),
    ]

    first = subprocess.run(
        command,
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        command,
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    combined_output = first.stdout + first.stderr + second.stdout + second.stderr
    assert "test-bearer" not in combined_output
    assert "registration-secret" not in combined_output
    merged = json.loads(user_config.read_text(encoding="utf-8"))
    assert merged["operator"] == "keep"
    assert merged[server_key]["foreign"] == {"command": "keep"}
    managed_server = merged[server_key]["mcp-agent-mail"]
    assert managed_server["headers"] == {
        "Authorization": "Bearer test-bearer"
    }
    if client == "cline":
        assert managed_server["type"] == "streamableHttp"
        assert managed_server["disabled"] is False
        assert managed_server["autoApprove"] == []
    if client == "opencode":
        assert merged["$schema"] == "https://opencode.ai/config.json"
        assert managed_server["type"] == "remote"
        assert managed_server["enabled"] is True
        assert managed_server["oauth"] is False
    credentials_path = home / ".state" / "agent-mail" / "credentials.json"
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    assert list(credentials) == ["/owner/repo"]
    assert list(credentials["/owner/repo"].values()) == ["registration-secret"]
    requests = [
        json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()
    ]
    registrations = [
        request["params"]["arguments"]
        for request in requests
        if request["params"]["name"] == "register_agent"
    ]
    assert len(registrations) == 2
    assert registrations[0].get("registration_token") is None
    assert registrations[1]["registration_token"] == "registration-secret"
    curl_argv = curl_argv_log.read_text(encoding="utf-8")
    assert "test-bearer" not in curl_argv
    assert "registration-secret" not in curl_argv
    assert "/dev/fd" not in curl_argv
    assert "curl-headers.conf" in curl_argv
    after_repo_files = {
        path.relative_to(repo)
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }
    assert after_repo_files == before_repo_files
    _assert_hook_private_mode(user_config)
    _assert_hook_private_mode(credentials_path)
    _assert_hook_private_mode(home / ".state" / "agent-mail" / "curl-headers.conf")


@pytest.mark.parametrize("client", USER_SCOPE_BOOTSTRAP_INTEGRATORS)
def test_user_scope_integrator_rejects_shared_environment_inside_git(
    tmp_path: Path,
    client: str,
) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "checkout"
    _init_git_repo(repo)
    env = {**os.environ, **_integration_env(home, fake_bin)}
    unsafe_env_file = repo / ".agent-mail.env"
    env["AGENT_MAIL_ENV_FILE"] = _git_bash_path(unsafe_env_file)
    before_repo_files = {
        path.relative_to(repo)
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(USER_SCOPE_BOOTSTRAP_INTEGRATORS[client]),
            "--yes",
            "--dry-run",
            "--project-dir",
            _git_bash_path(repo),
        ],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "must live outside every Git worktree" in output
    assert "test-bearer" not in output
    assert not unsafe_env_file.exists()
    after_repo_files = {
        path.relative_to(repo)
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }
    assert after_repo_files == before_repo_files


def _install_dash_c_hostile_git(fake_bin: Path) -> None:
    """Break `git -C <path>` exactly the way native git.exe breaks under Git Bash.

    The hook library exports MSYS_NO_PATHCONV=1 at file scope, and six
    installers *source* that library, so they inherit it.  From then on a POSIX
    path handed to a native git.exe is no longer translated: `git -C /c/...`
    cannot chdir and exits 128.  Everything else keeps working, which is what
    made the resulting hole so quiet — the worktree guard swallowed the failure
    with `2>/dev/null || true`, compared "" against "true", stayed silent, and
    let a bearer token be written inside the checkout.

    Shimming the same failure keeps this a regression test on *every* platform
    rather than only on the one Windows host, so the gate survives that host
    being unavailable.
    """
    real_git = shutil.which("git")
    assert real_git is not None, "git is required to build this shim"
    shim = fake_bin / "git"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-C" ]; then\n'
        "  printf 'fatal: cannot change to %s: No such file or directory\\n' \"$2\" >&2\n"
        "  exit 128\n"
        "fi\n"
        f'exec "{_git_bash_path(Path(real_git))}" "$@"\n',
        encoding="utf-8",
        # Windows would translate the shebang's LF into CRLF and `env` would
        # then look for a program literally named "bash\r".
        newline="\n",
    )
    shim.chmod(0o755)


@pytest.mark.parametrize("client", USER_SCOPE_BOOTSTRAP_INTEGRATORS)
def test_user_scope_integrator_guard_survives_a_probe_that_cannot_answer(
    tmp_path: Path,
    client: str,
) -> None:
    """The worktree guard must not fall silent when its own probe fails.

    This is the security half of the MSYS_NO_PATHCONV leak: with `git -C`
    broken, the old guard produced "" instead of "true" and therefore did not
    fire, so the installer wrote `.agent-mail.env` — containing the bearer
    token — inside a Git worktree, which is precisely what the guard exists to
    prevent.  A guard that treats "I could not find out" as "safe" is wrong
    regardless of what broke the probe: permissions, a corrupt `.git`, git
    missing from PATH, or a path dialect the binary cannot read.
    """
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "checkout"
    _init_git_repo(repo)
    env = {**os.environ, **_integration_env(home, fake_bin)}
    # After _integration_env, so fake_bin exists and is already on PATH.
    _install_dash_c_hostile_git(fake_bin)
    unsafe_env_file = repo / ".agent-mail.env"
    env["AGENT_MAIL_ENV_FILE"] = _git_bash_path(unsafe_env_file)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(USER_SCOPE_BOOTSTRAP_INTEGRATORS[client]),
            "--yes",
            "--dry-run",
            "--project-dir",
            _git_bash_path(repo),
        ],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "must live outside every Git worktree" in output, output
    # The token must never reach a file inside the checkout, nor the transcript.
    assert "test-bearer" not in output
    assert not unsafe_env_file.exists()


def test_legacy_inbox_hook_keeps_credentials_off_process_argv(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    agent = f"check-inbox-{tmp_path.name}"
    argv_log = tmp_path / "curl-argv.log"
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    *test-bearer*|*registration-secret*) exit 91 ;;
  esac
done
printf '%s\n' "$@" > "$FAKE_CURL_ARGV_LOG"
body="$(cat)"
[[ "$(printf '%s' "$body" | jq -r '.params.name')" == "fetch_inbox" ]] || exit 92
[[ "$(printf '%s' "$body" | jq -r '.params.arguments.registration_token')" == "registration-secret" ]] || exit 93
result='[{"id":1,"importance":"urgent","subject":"private argv canary"}]'
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    env.update(
        {
            "AGENT_MAIL_PROJECT": "/owner/repo",
            "AGENT_MAIL_AGENT": agent,
            "AGENT_MAIL_INTERVAL": "0",
            "FAKE_CURL_ARGV_LOG": _git_bash_path(argv_log),
        }
    )
    _put_credential(state, agent, "registration-secret")

    result = subprocess.run(
        [BASH, "-x", _git_bash_path(LEGACY_INBOX_HOOK)],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 message(s)" in output
    assert "1 urgent/high priority" in output
    assert "test-bearer" not in output
    assert "registration-secret" not in output
    argv = argv_log.read_text(encoding="utf-8")
    assert "test-bearer" not in argv
    assert "registration-secret" not in argv


def test_endpoint_canary_keeps_capabilities_off_argv_and_output(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    calls_log = tmp_path / "endpoint-calls.jsonl"
    curl_argv_log = tmp_path / "curl-argv.log"
    jq_argv_log = tmp_path / "jq-argv.log"
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    *test-bearer*|*registration-secret*) exit 91 ;;
  esac
done
printf '%s\n' "$@" >> "$FAKE_CURL_ARGV_LOG"
body="$(cat)"
printf '%s\n' "$body" >> "$FAKE_ENDPOINT_CALLS_LOG"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
case "$tool" in
  health_check) result='{"status":"ok","environment":"test"}' ;;
  ensure_project) result='{"id":1,"slug":"owner-repo","human_key":"/owner/repo","project_uid":"project-uid"}' ;;
  whois) result='{"id":2,"name":"codex-linux-home-1","program":"codex","model":"gpt","retired_at":null}' ;;
  start_agent_execution) result='{"id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","kind":"session","status":"active","lifecycle_protocol_version":1}' ;;
  end_agent_execution) result='{"id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","kind":"session","status":"completed","lifecycle_protocol_version":1}' ;;
  *) exit 92 ;;
esac
envelope="$(AGENT_MAIL_FAKE_RESULT="$result" jq -nc \
  '{result:{content:[{type:"text",text:env.AGENT_MAIL_FAKE_RESULT}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    real_jq = shutil.which("jq")
    assert real_jq is not None
    (fake_bin / "jq").write_text(
        "#!/usr/bin/env bash\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    *test-bearer*|*registration-secret*) exit 94 ;;\n"
        "  esac\n"
        "done\n"
        "printf '%s\\n' \"$@\" >> \"$FAKE_JQ_ARGV_LOG\"\n"
        f"exec {shlex.quote(_git_bash_path(real_jq))} \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    (fake_bin / "jq").chmod(0o700)
    env = _hook_env(home, state, fake_bin)
    env.update(
        {
            "AGENT_MAIL_PROJECT": "/owner/repo",
            "AGENT_MAIL_AGENT": "codex-linux-home-1",
            "AGENT_MAIL_REGISTRATION_TOKEN": "registration-secret",
            "FAKE_ENDPOINT_CALLS_LOG": _git_bash_path(calls_log),
            "FAKE_CURL_ARGV_LOG": _git_bash_path(curl_argv_log),
            "FAKE_JQ_ARGV_LOG": _git_bash_path(jq_argv_log),
        }
    )

    result = subprocess.run(
        [BASH, "-x", _git_bash_path(ENDPOINT_CANARY)],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    assert [call["params"]["name"] for call in calls] == [
        "health_check",
        "ensure_project",
        "whois",
        "start_agent_execution",
        "end_agent_execution",
    ]
    start_arguments = calls[3]["params"]["arguments"]
    end_arguments = calls[4]["params"]["arguments"]
    execution_token = start_arguments["execution_token"]
    assert len(execution_token) == 64
    assert end_arguments["execution_token"] == execution_token
    assert start_arguments["lifecycle_protocol_version"] == 1
    assert end_arguments["lifecycle_protocol_version"] == 1
    for secret in ("test-bearer", "registration-secret", execution_token):
        assert secret not in output
        assert secret not in curl_argv_log.read_text(encoding="utf-8")
        assert secret not in jq_argv_log.read_text(encoding="utf-8")
    assert "/dev/fd" not in curl_argv_log.read_text(encoding="utf-8")
    assert "curl-headers.conf" in curl_argv_log.read_text(encoding="utf-8")


def _run_identity_sensitive_hook(
    script_name: str,
    repo: Path,
    target: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    arguments = ["claude", "1"] if script_name == "inbox_watch.sh" else []
    payload = {
        "cwd": str(repo),
        "session_id": "identity-migration",
        "hook_event_name": "PostToolUse",
        "tool_input": {"file_path": str(target)},
    }
    return subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / script_name),
            *arguments,
        ],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["codex"],
        ["unsupported", "1"],
        ["codex", "0"],
        ["codex", "1", "extra"],
    ],
)
def test_inbox_watch_requires_explicit_valid_client_and_slot_without_network(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _install_fake_curl(
        fake_bin,
        "#!/usr/bin/env bash\nprintf called >> \"$FAKE_CURL_LOG\"\nexit 97\n",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env["FAKE_CURL_LOG"] = _git_bash_path(curl_log)
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / "inbox_watch.sh"),
            *arguments,
        ],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert not curl_log.exists()
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("mode", "expected_message"),
    [
        ("pending", "mail is already waiting"),
        ("event", "new mail for {agent} (id 4242)"),
        ("no-ready", "could not subscribe"),
        ("cut", "event stream closed"),
        ("invalid-watch", "event stream closed"),
        ("overflow-watch", "event stream closed"),
        ("overflow-ready", "event stream closed"),
        ("timeout", "watch window elapsed"),
    ],
)
def test_inbox_watch_uses_explicit_granted_identity_and_exact_rearm_command(
    tmp_path: Path,
    mode: str,
    expected_message: str,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text(
        "project-id\n",
        encoding="utf-8",
    )
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
if [[ " $* " == *"/events?"* ]]; then
  config="$(cat)"
  printf '%s\n' "$config" > "$FAKE_STREAM_AUTH_LOG"
  case "$FAKE_WATCH_MODE" in
    no-ready) exit 0 ;;
    cut)
      while [[ ! -f "$FAKE_READINESS_RELEASE" ]]; do
        /usr/bin/sleep 0.01
      done
      printf ': ready\n\n'
      exit 0
      ;;
    invalid-watch) printf ': ready\n\n'; exit 0 ;;
    overflow-watch) printf ': ready\n\n'; exit 0 ;;
    overflow-ready) /usr/bin/sleep 1.2; printf ': ready\n\n'; exit 0 ;;
    timeout) printf ': ready\n\n'; sleep 1; exit 0 ;;
    pending) printf ': ready\n\n'; sleep 30; exit 0 ;;
    event) printf ': ready\n\ndata: {"id":4242}\n\n'; exit 0 ;;
    *) exit 97 ;;
  esac
fi
body="$(cat)"
printf '%s\n' "$body" > "$FAKE_REQUEST_LOG"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
case "$tool:$FAKE_WATCH_MODE" in
  fetch_inbox:pending) result='[{"id":9001}]' ;;
  fetch_inbox:*) result='[]' ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    fake_date = fake_bin / "date"
    fake_date.write_text(
        """#!/usr/bin/env bash
if [[ ${FAKE_WATCH_MODE:-} == cut && ${1:-} == +%s ]]; then
  calls=0
  [[ -f $FAKE_DATE_CALL_LOG ]] && read -r calls < "$FAKE_DATE_CALL_LOG"
  calls=$(( calls + 1 ))
  printf '%s\n' "$calls" > "$FAKE_DATE_CALL_LOG"
  if [[ $calls -eq 1 ]]; then
    printf '100\n'
  else
    printf '101\n'
  fi
  exit 0
fi
exec /usr/bin/date "$@"
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_date.chmod(0o700)
    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text(
        """#!/usr/bin/env bash
if [[ ${FAKE_WATCH_MODE:-} == cut && ${1:-} == 0.2 ]]; then
  : > "$FAKE_READINESS_RELEASE"
fi
exec /usr/bin/sleep "$@"
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_sleep.chmod(0o700)
    hook_dir = tmp_path / "hooks with space"
    hook_dir.mkdir()
    watcher = hook_dir / "inbox_watch.sh"
    shutil.copy2(ROOT / "scripts" / "hooks" / "inbox_watch.sh", watcher)
    shutil.copy2(HOOK_COMMON, hook_dir / "agent_mail_common.sh")
    env = _hook_env(home, state, fake_bin)
    grant = _bash(
        f"""
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        agent="$(am_agent_name codex 2)"
        am_granted_name_put /owner/repo "$agent" codex 2
        printf '%s' "$agent"
        """,
        env=env,
    )
    assert grant.returncode == 0, grant.stderr
    agent_name = grant.stdout
    (state / "credentials.json").write_text(
        json.dumps(
            {
                "/owner/repo": {
                    "claude-wsl-home-1": "claude-token",
                    agent_name: "codex-token",
                }
            }
        ),
        encoding="utf-8",
    )
    stream_auth_log = tmp_path / "stream-auth.log"
    request_log = tmp_path / "request.log"
    date_call_log = tmp_path / "date-calls.log"
    readiness_release = tmp_path / "readiness-release"
    env.update(
        {
            "AGENT_MAIL_WATCH_SECONDS": (
                "1"
                if mode == "timeout"
                else "1+2"
                if mode == "invalid-watch"
                else "18446744073709551617"
                if mode == "overflow-watch"
                else "10"
            ),
            "AGENT_MAIL_WATCH_READY_SECONDS": (
                "abc"
                if mode == "no-ready"
                else "18446744073709551617"
                if mode == "overflow-ready"
                else "1"
            ),
            "FAKE_REQUEST_LOG": _git_bash_path(request_log),
            "FAKE_STREAM_AUTH_LOG": _git_bash_path(stream_auth_log),
            "FAKE_DATE_CALL_LOG": _git_bash_path(date_call_log),
            "FAKE_READINESS_RELEASE": _git_bash_path(readiness_release),
            "FAKE_WATCH_MODE": mode,
        }
    )

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(watcher),
            "codex",
            "2",
        ],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        # Git-for-Windows starts several MSYS/native processes here (bash,
        # git, curl, jq and the fixture helpers).  Five seconds was tight
        # enough for a loaded hosted runner to kill the immediate ``event``
        # case even though all seven slower parameters in the same job passed.
        # Keep a bounded deadline without turning Windows process start-up into
        # a production watcher failure.
        timeout=30 if os.name == "nt" else 10,
    )

    assert result.returncode == 0, result.stderr
    assert expected_message.format(agent=agent_name) in result.stdout
    rearm = shlex.split(result.stdout.splitlines()[-1].strip())
    assert rearm[0] == _git_bash_path(watcher.resolve())
    assert rearm[1:] == ["codex", "2"]
    stream_auth = stream_auth_log.read_text(encoding="utf-8")
    assert "codex-token" in stream_auth
    assert "claude-token" not in stream_auth
    if mode == "no-ready":
        assert not request_log.exists()
    else:
        request = json.loads(request_log.read_text(encoding="utf-8"))
        assert request["params"]["arguments"]["agent_name"] == agent_name
        assert request["params"]["arguments"]["registration_token"] == "codex-token"
    if mode == "cut":
        assert readiness_release.is_file()
        assert date_call_log.read_text(encoding="utf-8").strip() == "2"


def test_claude_session_start_prints_slot_pinned_watcher_command(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text(
        "project-id\n",
        encoding="utf-8",
    )
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
if [[ " $* " == *"/mail/api/file-reservations"* ]]; then
  cat >/dev/null
  printf '{"reservations":[]}\n200'
  exit 0
fi
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
case "$tool" in
  register_agent)
    name="$(printf '%s' "$body" | jq -r '.params.arguments.name')"
    result="$(jq -nc --arg name "$name" '{name:$name,retired_at:null}')"
    ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    identity = _bash(
        f"""
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        am_agent_name claude 2
        """,
        env=env,
    )
    assert identity.returncode == 0, identity.stderr
    agent_name = identity.stdout
    _put_credential(state, agent_name, "claude-slot-two-token")
    env["AGENT_MAIL_CLAUDE_SLOT"] = "2"
    payload = {
        "cwd": str(repo),
        "session_id": "claude-watcher-slot-two",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }

    result = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_start.sh")],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    command_line = next(
        line.strip() for line in context.splitlines() if "inbox_watch.sh" in line
    )
    watcher_command = shlex.split(command_line)
    assert Path(watcher_command[0]).name == "inbox_watch.sh"
    assert watcher_command[1:] == ["claude", "2"]
    assert f"you are {agent_name} on /owner/repo" in context


def test_claude_session_start_resumes_ended_lifecycle_generation(
    tmp_path: Path,
) -> None:
    """A resumed CLI session reuses its session_id, so the SessionEnd tombstone
    from the previous process still stands. SessionStart must advance the
    lifecycle generation past it — exactly as the codex path does — or the
    resumed session fails the end-intent barrier and runs without a root
    execution for its whole lifetime."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
if [[ " $* " == *"/mail/api/file-reservations"* ]]; then
  cat >/dev/null
  printf '{"reservations":[]}\n200'
  exit 0
fi
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
arguments="$(printf '%s' "$body" | jq -c '.params.arguments // {}')"
jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
  '{tool:$tool,arguments:$arguments}' >> "$FAKE_CALLS_LOG"
case "$tool" in
  register_agent)
    name="$(printf '%s' "$arguments" | jq -r '.name')"
    result="$(jq -nc --arg name "$name" '{name:$name,retired_at:null}')"
    ;;
  start_agent_execution)
    result='{"id":"11111111-1111-4111-8111-111111111111","status":"active","ancestor_execution_ids":[],"reused":false}'
    ;;
  end_agent_execution)
    result='{"execution":{"status":"completed"},"already_ended":false,"released_reservations":0}'
    ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    _, agent, _ = _hook_names(env)
    _put_credential(state, agent)
    calls_log = tmp_path / "calls.jsonl"
    env["FAKE_CALLS_LOG"] = _git_bash_path(calls_log)
    payload = {
        "cwd": str(repo),
        "session_id": "claude-resumed-root",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    start_command = [
        BASH,
        _git_bash_path(ROOT / "scripts" / "hooks" / "session_start.sh"),
    ]

    first_start = subprocess.run(
        start_command,
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert first_start.returncode == 0, first_start.stderr
    first_context = json.loads(first_start.stdout)["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "Root execution: 11111111-1111-4111-8111-111111111111" in first_context

    session_end = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_end.sh")],
        cwd=repo,
        env=env,
        input=json.dumps(
            {**payload, "hook_event_name": "SessionEnd", "reason": "other"}
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert session_end.returncode == 0, session_end.stderr
    intent_path = next((state / "session-end-intents").glob("*.json"))
    ended_intent = json.loads(intent_path.read_text(encoding="utf-8"))
    assert ended_intent["status"] == "ended"
    assert ended_intent["generation"] == 1

    resumed_start = subprocess.run(
        start_command,
        cwd=repo,
        env=env,
        input=json.dumps({**payload, "source": "resume"}),
        check=False,
        capture_output=True,
        text=True,
    )
    assert resumed_start.returncode == 0, resumed_start.stderr
    resumed_context = json.loads(resumed_start.stdout)["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "Root execution: 11111111-1111-4111-8111-111111111111" in resumed_context
    assert "could not be started" not in resumed_context
    assert "could not be resumed" not in resumed_context

    resumed_intent = json.loads(intent_path.read_text(encoding="utf-8"))
    assert resumed_intent["status"] == "active"
    assert resumed_intent["generation"] == 2
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    root_starts = [
        item["arguments"]
        for item in calls
        if item["tool"] == "start_agent_execution"
        and item["arguments"]["kind"] == "session"
    ]
    assert [item["external_id"] for item in root_starts] == [
        "claude-resumed-root",
        "claude-resumed-root#run-2",
    ]
    resumed_state = next(
        item
        for item in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in (state / "executions").glob("*.json")
        )
        if item.get("lifecycle_generation") == 2
    )
    assert resumed_state["status"] == "active"


def test_claude_fresh_registration_without_token_persists_no_identity_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
printf '%s\n' "$tool" >> "$FAKE_CURL_LOG"
case "$tool" in
  ensure_project) result='{"human_key":"/owner/repo"}' ;;
  register_agent)
    name="$(printf '%s' "$body" | jq -r '.params.arguments.name')"
    result="$(jq -nc --arg name "$name" '{name:$name,retired_at:null}')"
    ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env["FAKE_CURL_LOG"] = _git_bash_path(curl_log)
    payload = {
        "cwd": str(repo),
        "session_id": "claude-name-without-token",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }

    result = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_start.sh")],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "fresh registration returned" in context
    assert "no registration token" in context
    assert "Agent Mail: you are" not in context
    assert curl_log.read_text(encoding="utf-8").splitlines() == [
        "ensure_project",
        "register_agent",
    ]
    assert not (state / "credentials.json").exists()
    assert not (state / "granted").exists()


def test_claude_integrator_migrates_only_managed_hooks_in_temp_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    settings_dir = home / ".claude"
    settings_dir.mkdir(parents=True)
    project.mkdir()
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\nexit 1\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_claude.chmod(0o700)
    settings = settings_dir / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash /work/repo/.claude/hooks/mcp-agent-mail/session_start.sh || true",
                                },
                                {
                                    "type": "command",
                                    "command": "/home/klattm/projects/mcp_agent_mail/scripts/hooks/session_start.sh || true",
                                },
                                {
                                    "type": "command",
                                    "command": "bash /work/repo/.claude/hooks/session_start.sh || true",
                                },
                                {
                                    "type": "command",
                                    "command": "bash /work/foreign/scripts/hooks/session_start.sh || true",
                                },
                                {"type": "command", "command": "echo keep-foreign-hook"},
                            ],
                        }
                    ],
                    "PostToolUse": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash ~/.claude/hooks/mcp-agent-mail/inbox_check.sh || true",
                                },
                                {
                                    "type": "command",
                                    "command": "bash /mnt/d/projects/mcp_agent_mail/scripts/hooks/inbox_check.sh || true",
                                },
                                {
                                    "type": "command",
                                    "command": "bash C:\\projects\\mcp_agent_mail\\scripts\\hooks\\autoreserve.sh || true",
                                },
                                {
                                    "type": "command",
                                    "command": "bash /work/mcp_agent_mail/scripts/hooks/custom_hook.sh || true",
                                }
                            ],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "operatorMetadata": {"keep": True},
                "mcpServers": {
                    "claude-delegator": {
                        "type": "stdio",
                        "command": "keep-delegator",
                        "args": ["--keep"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["claude"]),
            "--yes",
            "--debug",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env={**os.environ, **_integration_env(home, fake_bin)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "test-bearer" not in result.stdout + result.stderr
    merged_text = settings.read_text(encoding="utf-8")
    merged = json.loads(merged_text)
    commands = [
        hook["command"]
        for groups in merged["hooks"].values()
        for group in groups
        for hook in group.get("hooks", [])
        if "command" in hook
    ]
    assert commands.count("echo keep-foreign-hook") == 1
    assert "/work/repo/.claude/hooks/mcp-agent-mail/session_start.sh" not in merged_text
    assert "/home/klattm/projects/mcp_agent_mail/scripts/hooks/session_start.sh" not in merged_text
    assert "/mnt/d/projects/mcp_agent_mail/scripts/hooks/inbox_check.sh" not in merged_text
    assert "C:\\projects\\mcp_agent_mail\\scripts\\hooks\\autoreserve.sh" not in merged_text
    assert commands.count("bash /work/repo/.claude/hooks/session_start.sh || true") == 1
    assert commands.count("bash /work/foreign/scripts/hooks/session_start.sh || true") == 1
    assert commands.count("bash /work/mcp_agent_mail/scripts/hooks/custom_hook.sh || true") == 1
    assert "~/.claude/hooks/mcp-agent-mail/inbox_check.sh" not in merged_text
    assert sum("mcp-agent-mail/session_start.sh" in command for command in commands) == 2
    assert len(merged["hooks"]["SubagentStart"]) == 1
    assert len(merged["hooks"]["SubagentStop"]) == 1
    assert merged["hooks"]["SubagentStart"][0]["matcher"] == ""
    assert merged["hooks"]["SubagentStop"][0]["matcher"] == ""
    assert "mcp-agent-mail/session_start.sh" in (
        merged["hooks"]["SubagentStart"][0]["hooks"][0]["command"]
    )
    assert "mcp-agent-mail/session_end.sh" in (
        merged["hooks"]["SubagentStop"][0]["hooks"][0]["command"]
    )
    assert (home / ".claude" / "hooks" / "mcp-agent-mail" / "session_start.sh").is_file()
    claude_mcp = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    server = claude_mcp["mcpServers"]["mcp-agent-mail"]
    assert server["url"] == "https://hermes.example/mcp/"
    assert server["headers"]["Authorization"] == "Bearer test-bearer"
    assert claude_mcp["operatorMetadata"] == {"keep": True}
    assert claude_mcp["mcpServers"]["claude-delegator"] == {
        "type": "stdio",
        "command": "keep-delegator",
        "args": ["--keep"],
    }
    generated_files = {
        home / ".agent-mail.env",
        home / ".claude.json",
        *(path for path in (home / ".claude").rglob("*") if path.is_file()),
    }
    for generated_file in sorted(generated_files, key=str):
        matches = _forbidden_client_dependency_matches(
            generated_file.read_text(encoding="utf-8")
        )
        assert matches == [], f"{generated_file} invokes {matches}"
    assert not (project / ".claude").exists()


def test_codex_and_copilot_integrators_write_only_temp_user_config(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    codex_dir = home / "custom-codex-profile"
    codex_dir.mkdir()
    copilot_dir = home / "custom-copilot-profile"
    copilot_hooks_dir = copilot_dir / "hooks"
    copilot_hooks_dir.mkdir(parents=True)
    copilot_mcp = copilot_dir / "mcp-config.json"
    copilot_mcp.write_text(
        json.dumps(
            {
                "operatorMetadata": {"keep": True},
                "mcpServers": {
                    "foreign": {
                        "type": "http",
                        "url": "https://foreign.example/mcp",
                        "tools": ["foreign-tool"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    copilot_hooks = copilot_hooks_dir / "mcp-agent-mail.json"
    copilot_hooks.write_text(
        json.dumps(
            {
                "version": 1,
                "operatorMetadata": {"keep": True},
                "hooks": {
                    "SessionStart": [
                        {
                            "type": "command",
                            "bash": "echo keep-copilot-foreign",
                            "powershell": "Write-Output keep-copilot-foreign",
                            "timeoutSec": 7,
                        },
                        {
                            "type": "command",
                            "bash": "bash /old/.copilot/hooks/mcp-agent-mail/hook_wrapper.sh session-start",
                            "powershell": "old mcp-agent-mail\\hook_wrapper.sh session-start",
                            "timeoutSec": 20,
                        },
                    ],
                    "ErrorOccurred": [
                        {"type": "command", "bash": "echo keep-error-hook"}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    vscode_config_path = home / ".config" / "Code" / "User" / "mcp.json"
    vscode_config_path.parent.mkdir(parents=True)
    vscode_config_path.write_text(
        json.dumps(
            {
                "operatorMetadata": {"keep": True},
                "servers": {
                    "foreign": {
                        "type": "http",
                        "url": "https://foreign.example/mcp",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    codex_hooks = codex_dir / "hooks.json"
    codex_hooks.write_text(
        json.dumps(
            {
                "description": "keep operator description",
                "operatorMetadata": {"keep": True},
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash /old/.codex/hooks/mcp-agent-mail/notify_wrapper.sh",
                                },
                                {"type": "command", "command": "echo keep-codex-foreign"},
                            ],
                        }
                    ],
                    "PostToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo keep-post-tool"}],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (codex_dir / "config.toml").write_text(
        "# MCP Agent Mail inbox reminder (managed by integrate_codex_cli.sh)\n"
        'notify = ["/old/.codex/hooks/mcp-agent-mail/notify_wrapper.sh"]\n\n'
        'model = "keep-model"\n\n'
        '[mcp_servers.foreign]\ncommand = "keep-command"\nargs = ["keep-arg"]\n\n'
        '[mcp_servers.claude-delegator]\ncommand = "keep-delegator"\nargs = ["--keep"]\n\n'
        '[mcp_servers.mcp_agent_mail]\nurl = "https://old.example/mcp/"\n\n'
        '[mcp_servers.mcp_agent_mail.http_headers]\n'
        'Authorization = "Bearer old"\nX-Tenant = "keep-tenant"\n',
        encoding="utf-8",
    )
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(codex_dir),
        "COPILOT_HOME": _git_bash_path(copilot_dir),
        "VSCODE_MCP_CONFIG_PATH": _git_bash_path(vscode_config_path),
    }

    codex_result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["codex"]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert codex_result.returncode == 0, codex_result.stderr
    assert "test-bearer" not in codex_result.stdout + codex_result.stderr
    codex_config = tomllib.loads((codex_dir / "config.toml").read_text(encoding="utf-8"))
    codex_server = codex_config["mcp_servers"]["mcp_agent_mail"]
    assert codex_server["url"] == "https://hermes.example/mcp/"
    assert codex_server["http_headers"]["Authorization"] == "Bearer test-bearer"
    assert "notify" not in codex_config
    assert codex_config["model"] == "keep-model"
    assert codex_config["mcp_servers"]["foreign"] == {
        "command": "keep-command",
        "args": ["keep-arg"],
    }
    assert codex_config["mcp_servers"]["claude-delegator"] == {
        "command": "keep-delegator",
        "args": ["--keep"],
    }
    assert codex_server["http_headers"]["X-Tenant"] == "keep-tenant"

    merged_hooks = json.loads(codex_hooks.read_text(encoding="utf-8"))
    assert merged_hooks["description"] == "keep operator description"
    assert merged_hooks["operatorMetadata"] == {"keep": True}
    all_handlers = [
        handler
        for groups in merged_hooks["hooks"].values()
        for group in groups
        for handler in group.get("hooks", [])
    ]
    commands = [handler.get("command", "") for handler in all_handlers]
    assert commands.count("echo keep-codex-foreign") == 1
    assert commands.count("echo keep-post-tool") == 1
    assert not any("/old/.codex/hooks/mcp-agent-mail" in command for command in commands)
    for event, event_arg in (
        ("SessionStart", "session-start"),
        ("SubagentStart", "subagent-start"),
        ("SubagentStop", "subagent-stop"),
        ("PostToolUse", "heartbeat"),
        ("Stop", "stop"),
        ("SessionEnd", "session-end"),
    ):
        managed = [
            handler
            for group in merged_hooks["hooks"][event]
            for handler in group.get("hooks", [])
            if "mcp-agent-mail" in handler.get("command", "")
        ]
        assert len(managed) == 1
        assert managed[0]["command"].endswith(event_arg)
        assert "commandWindows" in managed[0]
        windows_argv = shlex.split(managed[0]["commandWindows"], posix=False)
        assert windows_argv[0] == "&"
        bash_executable = windows_argv[1].strip("'")
        assert PureWindowsPath(bash_executable).name.casefold() == "bash.exe"
        assert PureWindowsPath(windows_argv[2].strip("'")).name == "hook_wrapper.sh"
        assert windows_argv[3].strip("'") == event_arg
        if os.name == "nt":
            assert Path(bash_executable).is_file()
        if event == "PostToolUse":
            managed_group = next(
                group
                for group in merged_hooks["hooks"][event]
                if managed[0] in group.get("hooks", [])
            )
            assert managed_group["matcher"] == "*"
            assert managed[0]["async"] is True
    assert merged_hooks["hooks"]["SessionEnd"][-1]["hooks"][0]["timeout"] == 3

    wrapper = codex_dir / "hooks" / "mcp-agent-mail" / "hook_wrapper.sh"
    wrapper_text = wrapper.read_text(encoding="utf-8")
    assert "AGENT_MAIL_CODEX_SLOT='1'" in wrapper_text
    assert "AGENT_MAIL_HOOK_CLIENT='codex'" in wrapper_text
    assert "AGENT_MAIL_HOOK_SLOT='1'" in wrapper_text
    assert "AGENT_MAIL_PROJECT" not in wrapper_text
    assert "AGENT_MAIL_AGENT" not in wrapper_text
    assert "AGENT_MAIL_REGISTRATION_TOKEN" not in wrapper_text

    # The installed wrapper owns the Codex identity. Ambient variables from a
    # parent shell must not redirect this client into another client's state.
    runtime = codex_dir / "hooks" / "mcp-agent-mail" / "agent_mail_hook.sh"
    runtime.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s|%s|%s' \"$AGENT_MAIL_HOOK_CLIENT\" "
        "\"$AGENT_MAIL_HOOK_SLOT\" \"$AGENT_MAIL_CODEX_SLOT\"\n",
        encoding="utf-8",
    )
    poisoned = subprocess.run(
        [BASH, _git_bash_path(wrapper), "stop"],
        env={
            **env,
            "AGENT_MAIL_HOOK_CLIENT": "copilot",
            "AGENT_MAIL_HOOK_SLOT": "9",
            "AGENT_MAIL_CODEX_SLOT": "8",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert poisoned.returncode == 0, poisoned.stderr
    assert poisoned.stdout == "codex|1|1"

    # A second installation replaces its own groups instead of appending a
    # duplicate, while the foreign hooks remain untouched.
    existing_config = (codex_dir / "config.toml").read_text(encoding="utf-8")
    (codex_dir / "config.toml").write_text(
        'notify = ["/usr/local/bin/foreign-notify"]\n' + existing_config,
        encoding="utf-8",
    )
    codex_rerun = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["codex"]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert codex_rerun.returncode == 0, codex_rerun.stderr
    rerun_hooks = json.loads(codex_hooks.read_text(encoding="utf-8"))
    rerun_commands = [
        handler.get("command", "")
        for groups in rerun_hooks["hooks"].values()
        for group in groups
        for handler in group.get("hooks", [])
    ]
    assert rerun_commands.count("echo keep-codex-foreign") == 1
    assert sum("mcp-agent-mail" in command for command in rerun_commands) == 6
    rerun_config = tomllib.loads((codex_dir / "config.toml").read_text(encoding="utf-8"))
    assert rerun_config["notify"] == ["/usr/local/bin/foreign-notify"]
    assert rerun_config["mcp_servers"]["mcp_agent_mail"]["http_headers"][
        "X-Tenant"
    ] == "keep-tenant"
    assert rerun_config["mcp_servers"]["claude-delegator"]["command"] == (
        "keep-delegator"
    )
    assert not (home / ".codex").exists()

    copilot_result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert copilot_result.returncode == 0, copilot_result.stderr
    assert "test-bearer" not in copilot_result.stdout + copilot_result.stderr
    vscode_config = json.loads(vscode_config_path.read_text(encoding="utf-8"))
    assert vscode_config["operatorMetadata"] == {"keep": True}
    assert vscode_config["servers"]["foreign"]["url"] == "https://foreign.example/mcp"
    vscode_server = vscode_config["servers"]["mcp-agent-mail"]
    assert vscode_server["url"] == "https://hermes.example/mcp/"
    assert vscode_server["headers"]["Authorization"] == "Bearer test-bearer"

    copilot_config = json.loads(copilot_mcp.read_text(encoding="utf-8"))
    assert copilot_config["operatorMetadata"] == {"keep": True}
    assert copilot_config["mcpServers"]["foreign"]["tools"] == ["foreign-tool"]
    copilot_server = copilot_config["mcpServers"]["mcp-agent-mail"]
    assert copilot_server == {
        "type": "http",
        "url": "https://hermes.example/mcp/",
        "tools": ["*"],
        "headers": {"Authorization": "Bearer test-bearer"},
    }

    copilot_hook_config = json.loads(copilot_hooks.read_text(encoding="utf-8"))
    assert copilot_hook_config["version"] == 1
    assert copilot_hook_config["operatorMetadata"] == {"keep": True}
    assert copilot_hook_config["hooks"]["ErrorOccurred"] == [
        {"type": "command", "bash": "echo keep-error-hook"}
    ]
    all_copilot_handlers = [
        handler
        for handlers in copilot_hook_config["hooks"].values()
        for handler in handlers
    ]
    assert sum(
        handler.get("bash") == "echo keep-copilot-foreign"
        for handler in all_copilot_handlers
    ) == 1
    managed_copilot_handlers = [
        handler
        for handler in all_copilot_handlers
        if "mcp-agent-mail" in handler.get("bash", "")
    ]
    assert len(managed_copilot_handlers) == 3
    assert {handler["timeoutSec"] for handler in managed_copilot_handlers} == {3, 20}
    for handler in managed_copilot_handlers:
        assert "powershell" in handler
        if os.name == "nt":
            powershell_argv = shlex.split(handler["powershell"])
            assert powershell_argv[0] == "&"
            assert PureWindowsPath(powershell_argv[1]).name.casefold() == "bash.exe"
            assert PureWindowsPath(powershell_argv[2]).name == "hook_wrapper.sh"
        else:
            assert "bash.exe" in handler["powershell"]
            assert "hook_wrapper.sh" in handler["powershell"]
        assert "??" not in handler["powershell"]

    copilot_wrapper = copilot_dir / "hooks" / "mcp-agent-mail" / "hook_wrapper.sh"
    copilot_wrapper_text = copilot_wrapper.read_text(encoding="utf-8")
    assert "AGENT_MAIL_HOOK_CLIENT='copilot'" in copilot_wrapper_text
    assert "AGENT_MAIL_HOOK_SLOT='1'" in copilot_wrapper_text
    assert "AGENT_MAIL_PROJECT" not in copilot_wrapper_text
    assert "AGENT_MAIL_REGISTRATION_TOKEN" not in copilot_wrapper_text

    # Reinstalling replaces exactly the three managed hook entries.
    copilot_rerun = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert copilot_rerun.returncode == 0, copilot_rerun.stderr
    rerun_copilot_hooks = json.loads(copilot_hooks.read_text(encoding="utf-8"))
    rerun_handlers = [
        handler
        for handlers in rerun_copilot_hooks["hooks"].values()
        for handler in handlers
    ]
    assert sum("mcp-agent-mail" in handler.get("bash", "") for handler in rerun_handlers) == 3
    assert sum(handler.get("bash") == "echo keep-copilot-foreign" for handler in rerun_handlers) == 1

    generated_files = {home / ".agent-mail.env", vscode_config_path}
    for generated_root in (codex_dir, copilot_dir):
        generated_files.update(path for path in generated_root.rglob("*") if path.is_file())
    for generated_file in sorted(generated_files, key=str):
        matches = _forbidden_client_dependency_matches(
            generated_file.read_text(encoding="utf-8")
        )
        assert matches == [], f"{generated_file} invokes {matches}"

    for forbidden in (
        project / ".claude",
        project / ".codex",
        project / ".vscode",
        project / "codex.mcp.json",
        project / ".mcp.json",
        project / "scripts" / "run_server_with_token.sh",
    ):
        assert not forbidden.exists()


def test_codex_wrapper_resolves_runtime_from_its_git_bash_view(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    codex_dir = home / "WSL mounted Codex profile"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()

    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(codex_dir),
    }
    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["codex"]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    installed_hooks = codex_dir / "hooks" / "mcp-agent-mail"
    installed_wrapper = installed_hooks / "hook_wrapper.sh"
    installed_runtime = installed_hooks / "agent_mail_hook.sh"
    wrapper_text = installed_wrapper.read_text(encoding="utf-8")
    assert _git_bash_path(installed_runtime) not in wrapper_text
    assert "/mnt/c/" not in wrapper_text
    assert 'exec bash "${_HOOK_DIR}/agent_mail_hook.sh" "$@"' in wrapper_text

    installed_runtime.write_text(
        "#!/usr/bin/env bash\nprintf 'wrong-runtime'\n",
        encoding="utf-8",
        newline="\n",
    )
    git_bash_hooks = (
        tmp_path
        / "Git Bash view"
        / "c"
        / "Users"
        / "mateu"
        / ".codex"
        / "hooks"
        / "mcp-agent-mail"
    )
    git_bash_hooks.mkdir(parents=True)
    git_bash_wrapper = git_bash_hooks / "hook_wrapper.sh"
    git_bash_wrapper.write_text(wrapper_text, encoding="utf-8", newline="\n")
    git_bash_wrapper.chmod(0o700)
    (git_bash_hooks / "agent_mail_hook.sh").write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s|%s|%s' \"$AGENT_MAIL_HOOK_CLIENT\" "
        "\"$AGENT_MAIL_HOOK_SLOT\" \"$AGENT_MAIL_CODEX_SLOT\"\n",
        encoding="utf-8",
        newline="\n",
    )

    relocated = subprocess.run(
        [BASH, _git_bash_path(git_bash_wrapper), "stop"],
        env={
            **env,
            "AGENT_MAIL_HOOK_CLIENT": "copilot",
            "AGENT_MAIL_HOOK_SLOT": "9",
            "AGENT_MAIL_CODEX_SLOT": "8",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert relocated.returncode == 0, relocated.stderr
    assert relocated.stdout == "codex|1|1"


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"))
    }


@pytest.mark.parametrize(
    ("client", "relative_path", "invalid_contents"),
    [
        ("claude", ".claude/settings.json", '{"hooks":{"SessionStart":"bad"}}'),
        ("claude", ".claude/settings.json", '{"hooks":null}'),
        ("claude", ".claude.json", '{"mcpServers":[]}'),
        ("claude", ".claude.json", "not-json"),
        ("codex", "codex-profile/hooks.json", '{"hooks":{"Stop":[{"hooks":{}}]}}'),
        ("codex", "codex-profile/hooks.json", ""),
        ("codex", "codex-profile/config.toml", "mcp_servers = 7\n"),
        (
            "codex",
            "codex-profile/config.toml",
            '[[mcp_servers]]\nname = "foreign"\n',
        ),
        ("codex", "codex-profile/config.toml", "invalid = [\n"),
        (
            "codex",
            "codex-profile/config.toml",
            '[mcp_servers.mcp_agent_mail]\nhttp_headers = 7\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            'mcp_servers = { foreign = { command = "keep-foreign" }, '
            'mcp_agent_mail = { url = "https://old.example/mcp/" } }\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers.mcp_agent_mail]\n"
            'url = "https://first.example/mcp/"\n'
            '[mcp_servers."mcp-agent-mail"]\n'
            'url = "https://second.example/mcp/"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers.mcp_agent_mail]\n"
            'url = "https://first.example/mcp/"\n'
            "[mcp_servers.mcp_agent_mail]\n"
            'url = "https://second.example/mcp/"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers.mcp_agent_mail\nurl = \"https://old.example/mcp/\"\n",
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers..mcp_agent_mail]\n"
            'url = "https://old.example/mcp/"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            '[mcp_servers."mcp\\u005fagent_mail"]\n'
            'url = "https://old.example/mcp/"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            '["mcp\\u005fservers".foreign]\ncommand = "keep"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            '[mcp_servers."fore\\u0069gn"]\ncommand = "keep"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers.mcp_agent_mail]\n"
            '"u\\u0072l" = "https://old.example/mcp/"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers.mcp_agent_mail]\n"
            'url.host = "https://old.example/mcp/"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers.mcp_agent_mail]\n"
            'url = "https://old.example/mcp/"\n'
            "[mcp_servers.mcp_agent_mail.http_headers]\n"
            'Authorization.scheme = "Bearer old"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers.mcp_agent_mail]\n"
            'url = "https://first.example/mcp/"\n'
            'url = "https://second.example/mcp/"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers.mcp_agent_mail]\n"
            'url = "https://old.example/mcp/"\n'
            "[mcp_servers.mcp_agent_mail.http_headers]\n"
            'Authorization = "Bearer first"\n'
            'Authorization = "Bearer second"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            'model = "first"\nmodel = "second"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[foreign]\nvalue = 1\n\"value\" = 2\n",
        ),
        (
            "codex",
            "codex-profile/config.toml",
            'foreign = "scalar"\n[foreign]\nvalue = "table"\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[[mcp_servers.mcp_agent_mail]]\nurl = \"https://old.example/mcp/\"\n",
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers]\n"
            'mcp_agent_mail = { url = "https://old.example/mcp/" }\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers]\n"
            '"mcp-agent-mail" = { url = "https://old.example/mcp/" }\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers.mcp_agent_mail]\n"
            "url = [\n\"https://old.example/mcp/\",\n]\n",
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "[mcp_servers.mcp_agent_mail]\n"
            'url = "https://old.example/mcp/"\n'
            "[mcp_servers.mcp_agent_mail.http_headers]\n"
            "Authorization = [\n\"Bearer old\",\n]\n",
        ),
        (
            "codex",
            "codex-profile/config.toml",
            'description = """\n'
            "[mcp_servers.mcp_agent_mail]\n"
            'url = "https://fake.example/mcp/"\n'
            '"""\n',
        ),
        (
            "codex",
            "codex-profile/config.toml",
            "description = '''\n"
            "[mcp_servers.mcp_agent_mail]\n"
            'url = "https://fake.example/mcp/"\n'
            "'''\n",
        ),
        (
            "codex",
            "codex-profile/config.toml",
            'notify = ["/old/.codex/hooks/mcp\\u002dagent\\u002dmail/'
            'hook_wrapper.sh"]\n',
        ),
    ],
)
def test_claude_and_codex_integrators_reject_invalid_nested_config_before_writes(
    tmp_path: Path,
    client: str,
    relative_path: str,
    invalid_contents: str,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    target = home / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(invalid_contents, encoding="utf-8")
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(home / "codex-profile"),
    }
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS[client]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    output = (result.stdout + result.stderr).lower()
    assert "refusing to overwrite" in output or "no user configuration was changed" in output
    assert "test-bearer" not in result.stdout + result.stderr
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_claude_and_codex_integrator_dry_run_has_zero_filesystem_mutations(
    tmp_path: Path,
    client: str,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    codex_dir = home / "codex-profile"
    if client == "claude":
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"SessionStart": []}}),
            encoding="utf-8",
        )
        (home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"claude-delegator": {"command": "keep"}}}),
            encoding="utf-8",
        )
    else:
        codex_dir.mkdir()
        (codex_dir / "hooks.json").write_text(
            json.dumps({"hooks": {"Stop": []}}),
            encoding="utf-8",
        )
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.claude-delegator]\ncommand = "keep"\n',
            encoding="utf-8",
        )
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(codex_dir),
    }
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS[client]),
            "--yes",
            "--dry-run",
            "--debug",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "test-bearer" not in result.stdout + result.stderr
    assert "no files or directories were changed" in result.stdout
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_codex_rejects_invalid_shared_state_without_mutation(
    tmp_path: Path,
    dry_run: bool,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    codex_dir = home / "codex-profile"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.foreign]\ncommand = "keep"\n',
        encoding="utf-8",
    )
    (home / ".agent-mail.env").write_text(
        "AGENT_MAIL_STATE_DIR=relative/state\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(codex_dir),
    }
    before = _tree_snapshot(tmp_path)

    arguments = [BASH, _git_bash_path(INTEGRATORS["codex"]), "--yes"]
    if dry_run:
        arguments.append("--dry-run")
    arguments.extend(["--project-dir", _git_bash_path(project)])
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "shared agent mail environment is invalid" in (
        result.stdout + result.stderr
    ).lower()
    assert "test-bearer" not in result.stdout + result.stderr
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("relative_path", ["hooks.json", "config.toml"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_codex_rejects_directory_config_destinations_without_mutation(
    tmp_path: Path,
    relative_path: str,
    dry_run: bool,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    codex_dir = home / "codex-profile"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    codex_dir.mkdir()
    (codex_dir / relative_path).mkdir()
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(codex_dir),
    }
    before = _tree_snapshot(tmp_path)

    arguments = [BASH, _git_bash_path(INTEGRATORS["codex"]), "--yes"]
    if dry_run:
        arguments.append("--dry-run")
    arguments.extend(["--project-dir", _git_bash_path(project)])
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "regular, non-symlink file" in (result.stdout + result.stderr).lower()
    assert "test-bearer" not in result.stdout + result.stderr
    assert _tree_snapshot(tmp_path) == before


def test_codex_dry_run_accepts_escaped_foreign_project_headers_without_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    codex_dir = home / "codex-profile"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    codex_dir.mkdir()
    config_path = codex_dir / "config.toml"
    config_path.write_text(
        '[projects."C:\\\\Users\\\\mateu"]\ntrust_level = "trusted"\n'
        '[projects."d:\\\\projects\\\\hestia"]\ntrust_level = "trusted"\n'
        '[mcp_servers.mcp_agent_mail]\nurl = "https://old.example/mcp/"\n'
        '[mcp_servers.mcp_agent_mail.http_headers]\n'
        'Authorization = "Bearer old"\n',
        encoding="utf-8",
    )
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(codex_dir),
    }
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["codex"]),
            "--yes",
            "--dry-run",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "test-bearer" not in result.stdout + result.stderr
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("existing_toml", "expected_notify"),
    [
        (
            (
                'notify = ["/usr/local/bin/foreign-notify"]\n'
                'model = "keep-model"\n\n'
                '[mcp_servers . foreign]\ncommand = "keep-foreign"\n\n'
                '[mcp_servers . "claude-delegator"]\ncommand = "keep-delegator"\n\n'
                '[mcp_servers . mcp_agent_mail]\nurl = "https://old.example/mcp/"\n\n'
                '[mcp_servers . mcp_agent_mail . http_headers]\n'
                'Authorization = "Bearer old"\nX-Tenant = "keep-tenant"\n'
            ),
            ["/usr/local/bin/foreign-notify"],
        ),
        (
            (
                'notify = ["/usr/local/bin/foreign-notify"]\n\n'
                '[mcp_servers.foreign]\ncommand = "keep-foreign"\n\n'
                '[mcp_servers."claude-delegator"]\ncommand = "keep-delegator"\n\n'
                '[mcp_servers."mcp-agent-mail"]\nurl = "https://old.example/mcp/"\n\n'
                '[mcp_servers."mcp-agent-mail".http_headers]\n'
                'Authorization = "Bearer old"\nX-Tenant = "keep-tenant"\n'
            ),
            ["/usr/local/bin/foreign-notify"],
        ),
        pytest.param(
            (
                'notify = ["/usr/local/bin/foreign-notify", '
                '"/old/.codex/hooks/mcp-agent-mail/notify_wrapper.sh"]\n\n'
                '[mcp_servers.foreign]\ncommand = "keep-foreign"\n\n'
                '[mcp_servers.claude-delegator]\ncommand = "keep-delegator"\n\n'
                '[mcp_servers.mcp_agent_mail]\nurl = "https://old.example/mcp/"\n\n'
                '[mcp_servers.mcp_agent_mail.http_headers]\n'
                'Authorization = "Bearer old"\nX-Tenant = "keep-tenant"\n'
            ),
            None,
            id="managed-notify-removes-whole-argv",
        ),
        pytest.param(
            (
                'notify = ["/old/.codex/hooks/mcp_agent_mail/hook_wrapper.sh", '
                '"stop"]\n\n'
                '[mcp_servers.mcp_agent_mail]\n'
                'url = "https://old.example/mcp/"\n\n'
                '[mcp_servers.mcp_agent_mail.http_headers]\n'
                'Authorization = "Bearer old"\nX-Tenant = "keep-tenant"\n'
            ),
            None,
            id="managed-underscore-notify-removes-whole-argv",
        ),
        pytest.param(
            (
                'notify = ["/usr/local/bin/foreign-notify"]\n\n'
                '[projects."C:\\\\Users\\\\mateu"]\ntrust_level = "trusted"\n'
                '[projects."d:\\\\projects\\\\hestia"]\ntrust_level = "trusted"\n\n'
                '[mcp_servers.mcp_agent_mail]\nurl = "https://old.example/mcp/"\n\n'
                '[mcp_servers.mcp_agent_mail.http_headers]\n'
                'authorization = "Bearer lowercase"\n'
                '"AuThOrIzAtIoN" = "Bearer mixed"\n'
                'X-Tenant = "keep-tenant"\n'
                '\n[[foreign_agents]]\nname = "first"\n'
                '[foreign_agents.details]\nrank = 1\n'
                '\n[[foreign_agents]]\nname = "second"\n'
                '[foreign_agents.details]\nrank = 2\n'
                '\n[foreign_settings]\nfeature.enabled = true\n'
            ),
            ["/usr/local/bin/foreign-notify"],
            id="foreign-dotted-aot-and-case-insensitive-authorization",
        ),
    ],
)
def test_codex_integrator_semantically_merges_all_supported_toml_shapes(
    tmp_path: Path,
    existing_toml: str,
    expected_notify: list[str] | None,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    codex_dir = home / "codex-profile"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    codex_dir.mkdir()
    config_path = codex_dir / "config.toml"
    config_path.write_text(existing_toml, encoding="utf-8")
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(codex_dir),
    }

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["codex"]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "test-bearer" not in result.stdout + result.stderr
    rendered_config = config_path.read_text(encoding="utf-8")
    merged = tomllib.loads(rendered_config)
    managed_key = (
        "mcp-agent-mail"
        if "mcp-agent-mail" in merged["mcp_servers"]
        else "mcp_agent_mail"
    )
    managed = merged["mcp_servers"][managed_key]
    assert managed["url"] == "https://hermes.example/mcp/"
    assert managed["http_headers"] == {
        "Authorization": "Bearer test-bearer",
        "X-Tenant": "keep-tenant",
    }
    if "foreign" in merged["mcp_servers"]:
        assert merged["mcp_servers"]["foreign"] == {"command": "keep-foreign"}
    if "claude-delegator" in merged["mcp_servers"]:
        assert merged["mcp_servers"]["claude-delegator"] == {
            "command": "keep-delegator"
        }
    if expected_notify is None:
        assert "notify" not in merged
    else:
        assert merged["notify"] == expected_notify
    if "foreign_agents" in merged:
        assert merged["foreign_agents"] == [
            {"name": "first", "details": {"rank": 1}},
            {"name": "second", "details": {"rank": 2}},
        ]
        assert (
            '\n[[foreign_agents]]\nname = "first"\n'
            '[foreign_agents.details]\nrank = 1\n'
            '\n[[foreign_agents]]\nname = "second"\n'
            '[foreign_agents.details]\nrank = 2\n'
        ) in rendered_config
        assert merged["foreign_settings"] == {"feature": {"enabled": True}}
    if "projects" in merged:
        assert merged["projects"] == {
            r"C:\Users\mateu": {"trust_level": "trusted"},
            r"d:\projects\hestia": {"trust_level": "trusted"},
        }


def test_codex_parser_is_isolated_from_the_callers_python_project(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    foreign_project = tmp_path / "foreign-project"
    fake_bin = tmp_path / "bin"
    codex_dir = home / "codex-profile"
    home.mkdir()
    foreign_project.mkdir()
    fake_bin.mkdir()
    codex_dir.mkdir()
    (foreign_project / "pyproject.toml").write_text(
        '[project]\nname = "must-not-be-loaded"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    (foreign_project / "uv.lock").write_text("must-stay-byte-identical\n", encoding="utf-8")
    (foreign_project / ".venv").mkdir()
    (foreign_project / ".venv" / "sentinel").write_text("keep\n", encoding="utf-8")
    before = _tree_snapshot(foreign_project)
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(codex_dir),
    }

    result = subprocess.run(
        [BASH, _git_bash_path(INTEGRATORS["codex"]), "--yes"],
        cwd=foreign_project,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert _tree_snapshot(foreign_project) == before


def test_claude_posix_hook_commands_quote_spaces_and_apostrophes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home with space" / "operator's-profile"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    home.mkdir(parents=True)
    project.mkdir()
    fake_bin.mkdir()

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["claude"]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env={**os.environ, **_integration_env(home, fake_bin)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    managed_commands = [
        handler["command"]
        for groups in settings["hooks"].values()
        for group in groups
        for handler in group["hooks"]
        if "mcp-agent-mail" in handler.get("command", "")
    ]
    assert len(managed_commands) == 8
    for command in managed_commands:
        if os.name == "nt":
            outer = shlex.split(command)
            assert Path(outer[0]).name.casefold() == "bash.exe"
            assert outer[1] == "-c"
            parsed = shlex.split(outer[2].removesuffix(" || true"))
        else:
            parsed = shlex.split(command.removesuffix(" || true"))
        assert parsed[0] == "AGENT_MAIL_CLAUDE_SLOT=1"
        assert parsed[1] == "bash"
        assert parsed[2].startswith(_git_bash_path(home / ".claude" / "hooks"))


@pytest.mark.parametrize(
    ("client", "use_override", "debug_resolution"),
    [
        ("claude", False, False),
        ("claude", True, True),
        ("codex", False, False),
        ("codex", True, False),
    ],
)
def test_claude_and_codex_windows_hooks_support_current_and_explicit_git_bash(
    tmp_path: Path,
    client: str,
    use_override: bool,
    debug_resolution: bool,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    git_root = tmp_path / "Portable Git"
    git_bin = git_root / "mingw64" / "bin"
    detected_bash = git_root / "bin" / "bash.exe"
    raw_bash = git_root / "usr" / "bin" / "bash.exe"
    codex_dir = home / "codex-profile"
    agent_mail_env = home / "agent-mail.env"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    git_bin.mkdir(parents=True)
    detected_bash.parent.mkdir(parents=True)
    raw_bash.parent.mkdir(parents=True)
    git_path = shutil.which("git")
    assert git_path is not None
    fake_git = _install_bash_command_forwarder(git_bin, "git", git_path)
    detected_bash.write_text(
        "#!/bin/sh\nexit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    detected_bash.chmod(0o700)
    raw_bash.write_text(
        "#!/bin/sh\nexit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    raw_bash.chmod(0o700)
    portable_bash = fake_bin / "portable-git-bash"
    portable_bash.write_text(
        "#!/bin/sh\nexit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    portable_bash.chmod(0o700)
    fake_uname = fake_bin / "uname"
    fake_uname.write_text(
        "#!/bin/sh\nprintf 'MINGW64_NT-10.0\\n'\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_uname.chmod(0o700)
    fake_cygpath = fake_bin / "cygpath"
    fake_cygpath.write_text(
        r"""#!/bin/sh
case "$1" in
  -u)
    case "$2" in
      'D:\Profiles\Codex') printf '%s\n' "$FAKE_CODEX_POSIX" ;;
      'Q:\AgentMail\shared.env') printf '%s\n' "$FAKE_AGENT_MAIL_ENV_POSIX" ;;
      'D:\Portable Git\bin\bash.exe'|'D:/Portable Git/bin/bash.exe')
        printf '%s\n' "$FAKE_OVERRIDE_BASH" ;;
      *) printf '%s\n' "$2" ;;
    esac ;;
  -m)
    case "$2" in
      "$FAKE_GIT_BIN_POSIX") printf '%s\n' "$FAKE_GIT_BIN_MIXED" ;;
      "$FAKE_DETECTED_BASH") printf '%s\n' 'D:\Portable Git\bin\bash.exe' ;;
      */bash|*/bash.exe|*/portable-git-bash)
        printf '%s\n' 'D:\Portable Git\usr\bin\bash.exe' ;;
      *) printf '%s\n' 'D:\Profiles\Codex\hooks\mcp-agent-mail\hook_wrapper.sh' ;;
    esac ;;
  -w)
    case "$2" in
      "$FAKE_DETECTED_BASH") printf '%s\n' 'D:\Portable Git\bin\bash.exe' ;;
      */bash|*/bash.exe|*/portable-git-bash)
        printf '%s\n' 'D:\Portable Git\usr\bin\bash.exe' ;;
      *) printf '%s\n' 'D:\Profiles\Codex\hooks\mcp-agent-mail\hook_wrapper.sh' ;;
    esac ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_cygpath.chmod(0o700)
    integration_env = _integration_env(home, fake_bin)
    fake_git_posix = _git_bash_path(fake_git)
    git_root_native = str(git_root).replace("\\", "/")
    fake_git_mixed = (
        f"{git_root_native}/mingw64/bin/git" if os.name == "nt" else fake_git_posix
    )
    detected_bash_path = (
        f"{git_root_native}/bin/bash.exe"
        if os.name == "nt"
        else _git_bash_path(detected_bash)
    )
    env = {
        **os.environ,
        **integration_env,
        "AGENT_MAIL_ENV_FILE": r"Q:\AgentMail\shared.env",
        "FAKE_AGENT_MAIL_ENV_POSIX": _git_bash_path(agent_mail_env),
        "FAKE_CODEX_POSIX": _git_bash_path(codex_dir),
        "FAKE_DETECTED_BASH": detected_bash_path,
        "FAKE_OVERRIDE_BASH": _git_bash_path(portable_bash),
        "FAKE_GIT_BIN_MIXED": fake_git_mixed,
        "FAKE_GIT_BIN_POSIX": fake_git_posix,
        "PATH": f"{_git_bash_path(git_bin)}:{integration_env['PATH']}",
    }
    if client == "codex":
        env.update(
            {
                "CODEX_HOME": r"D:\Profiles\Codex",
            }
        )
    if use_override:
        env["AGENT_MAIL_GIT_BASH_PATH"] = (
            r"D:\Portable Git\bin\bash.exe"
            if client == "codex"
            else _git_bash_path(portable_bash)
        )
    if debug_resolution:
        env["AGENT_MAIL_DEBUG_BASH_RESOLUTION"] = "1"

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS[client]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    if debug_resolution:
        assert (
            "DEBUG git=<none> roots=0 "
            "wrap=D:\\Portable Git\\usr\\bin\\bash.exe"
        ) in result.stderr
    assert agent_mail_env.is_file()
    assert not (ROOT / r"Q:\AgentMail\shared.env").exists()
    if client == "claude":
        settings = json.loads((home / ".claude" / "settings.json").read_text())
        managed_commands = [
            handler["command"]
            for groups in settings["hooks"].values()
            for group in groups
            for handler in group["hooks"]
            if "mcp-agent-mail" in handler.get("command", "")
        ]
        assert len(managed_commands) == 8
        expected_bash = (
            "D:\\Portable Git\\usr\\bin\\bash.exe"
            if use_override
            else "D:\\Portable Git\\bin\\bash.exe"
        )
        assert all(
            f'"{expected_bash}"' in command
            for command in managed_commands
        )
    else:
        hooks = json.loads((codex_dir / "hooks.json").read_text())
        managed_handlers = {
            event: handler
            for event, groups in hooks["hooks"].items()
            for group in groups
            for handler in group["hooks"]
            if "mcp-agent-mail" in handler.get("command", "")
        }
        assert len(managed_handlers) == 6
        expected_bash = (
            "D:/Portable Git/bin/bash.exe"
            if use_override
            else r"D:\Portable Git\bin\bash.exe"
        )
        assert all(
            expected_bash in handler["commandWindows"]
            for handler in managed_handlers.values()
        )
        assert all(
            r"D:\Portable Git\usr\bin\bash.exe" not in handler["commandWindows"]
            and "D:/Portable Git/usr/bin/bash.exe" not in handler["commandWindows"]
            for handler in managed_handlers.values()
        )
        event_arguments = {
            "SessionStart": "session-start",
            "SubagentStart": "subagent-start",
            "SubagentStop": "subagent-stop",
            "PostToolUse": "heartbeat",
            "Stop": "stop",
            "SessionEnd": "session-end",
        }
        for event, handler in managed_handlers.items():
            assert handler["command"] == (
                'bash "${CODEX_HOME:-${HOME}/.codex}/hooks/'
                f'mcp-agent-mail/hook_wrapper.sh" {event_arguments[event]}'
            )


@pytest.mark.parametrize("client", ["claude", "codex", "copilot"])
def test_integrators_hide_bearer_under_external_xtrace(
    tmp_path: Path,
    client: str,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    token = f"xtrace-secret-{client}-alpha-omega"
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(home / "codex-profile"),
        "INTEGRATION_BEARER_TOKEN": token,
    }
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            "-x",
            _git_bash_path(INTEGRATORS[client]),
            "--yes",
            "--dry-run",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert token not in output
    assert "xtrace-secret" not in output
    assert "alpha-omega" not in output
    assert _tree_snapshot(tmp_path) == before


def test_claude_and_codex_installers_keep_bearers_off_argv_and_pseudofiles() -> None:
    claude = INTEGRATORS["claude"].read_text(encoding="utf-8")
    codex = INTEGRATORS["codex"].read_text(encoding="utf-8")

    assert "--rawfile /dev/fd" not in claude
    assert "--rawfile" not in claude
    assert "--arg token" not in claude
    assert "--arg token" not in codex
    assert "AGENT_MAIL_INSTALL_AUTHORIZATION" in claude
    assert "AGENT_MAIL_JQ_VALUE" in codex
    assert "AGENT_MAIL_TOML_AUTHORIZATION" in codex


def test_execution_capabilities_stay_out_of_jq_argv() -> None:
    common = (ROOT / "scripts" / "hooks" / "agent_mail_common.sh").read_text(
        encoding="utf-8"
    )
    autoreserve = (ROOT / "scripts" / "hooks" / "autoreserve.sh").read_text(
        encoding="utf-8"
    )

    assert "--arg execution_token" not in common
    assert "--arg execution_token" not in autoreserve
    assert "AGENT_MAIL_JQ_EXECUTION_TOKEN" in common
    assert "AGENT_MAIL_JQ_EXECUTION_TOKEN" in autoreserve


def test_copilot_integrator_dry_run_has_zero_filesystem_mutations(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    copilot_dir = home / "copilot-profile"
    hooks_dir = copilot_dir / "hooks"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    hooks_dir.mkdir(parents=True)
    (copilot_dir / "mcp-config.json").write_text(
        json.dumps({"mcpServers": {"foreign": {"type": "http"}}}),
        encoding="utf-8",
    )
    (hooks_dir / "mcp-agent-mail.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "SessionStart": [
                        {"type": "command", "bash": "echo keep-foreign"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "COPILOT_HOME": _git_bash_path(copilot_dir),
    }
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--dry-run",
            "--debug",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "test-bearer" not in result.stdout + result.stderr
    assert "no files or directories were changed" in result.stdout
    assert _tree_snapshot(tmp_path) == before


def test_copilot_integrator_rejects_invalid_user_json_before_any_write(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    copilot_dir = home / "copilot-profile"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    copilot_dir.mkdir()
    invalid_config = copilot_dir / "mcp-config.json"
    invalid_config.write_text("not-json\n", encoding="utf-8")
    shared_env = home / ".agent-mail.env"
    shared_env.write_text("UNRELATED_SETTING=keep\n", encoding="utf-8")
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "COPILOT_HOME": _git_bash_path(copilot_dir),
    }
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "refusing to overwrite" in result.stdout + result.stderr
    assert "test-bearer" not in result.stdout + result.stderr
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("use_override", [False, True])
def test_copilot_windows_hooks_use_git_bash_launcher_with_poisoned_path(
    tmp_path: Path,
    use_override: bool,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    git_root = tmp_path / "Portable Git"
    git_bin = git_root / "mingw64" / "bin"
    git_bash = git_root / "bin" / "bash.exe"
    copilot_dir = home / "copilot-profile"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    git_bin.mkdir(parents=True)
    git_bash.parent.mkdir(parents=True)
    git_path = shutil.which("git")
    assert git_path is not None
    _install_bash_command_forwarder(git_bin, "git", git_path)
    git_bash.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(_git_bash_path(BASH))} \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    git_bash.chmod(0o700)
    fake_uname = fake_bin / "uname"
    fake_uname.write_text(
        "#!/bin/sh\nprintf 'MINGW64_NT-10.0\\n'\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_uname.chmod(0o700)
    fake_cygpath = fake_bin / "cygpath"
    fake_cygpath.write_text(
        r"""#!/bin/sh
case "$1" in
  -u)
    case "$2" in
      'D:\Profiles\Copilot') printf '%s\n' "$FAKE_COPILOT_POSIX" ;;
      'D:\Profiles\agent-mail.env') printf '%s\n' "$FAKE_AGENT_MAIL_ENV_POSIX" ;;
      *) printf '%s\n' "$2" ;;
    esac ;;
  -m)
    case "$2" in
      "$FAKE_GIT_BIN_POSIX") printf '%s\n' "$FAKE_GIT_BIN_POSIX" ;;
      *) printf '%s\n' "$2" ;;
    esac ;;
  -w)
    case "$2" in
      "$FAKE_GIT_BASH_POSIX") printf '%s\n' 'D:\Portable Git\bin\bash.exe' ;;
      *) printf '%s\n' 'D:\Profiles\Copilot\hooks\mcp-agent-mail\hook_wrapper.sh' ;;
    esac ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_cygpath.chmod(0o700)
    poison_log = tmp_path / "poisoned-bash.log"
    poisoned_bash = fake_bin / "bash"
    poisoned_bash.write_text(
        "#!/bin/sh\n"
        "printf invoked >\"$POISON_LOG\"\n"
        "exit 97\n",
        encoding="utf-8",
        newline="\n",
    )
    poisoned_bash.chmod(0o700)
    integration_env = _integration_env(home, fake_bin)
    env = {
        **os.environ,
        **integration_env,
        "AGENT_MAIL_ENV_FILE": r"D:\Profiles\agent-mail.env",
        "APPDATA": _git_bash_path(home / "appdata"),
        "COPILOT_HOME": r"D:\Profiles\Copilot",
        "FAKE_AGENT_MAIL_ENV_POSIX": _git_bash_path(home / "agent-mail.env"),
        "FAKE_COPILOT_POSIX": _git_bash_path(copilot_dir),
        "FAKE_GIT_BASH_POSIX": _git_bash_path(git_bash),
        "FAKE_GIT_BIN_POSIX": _git_bash_path(git_bin / "git"),
        "PATH": f"{_git_bash_path(git_bin)}:{integration_env['PATH']}",
        "POISON_LOG": _git_bash_path(poison_log),
    }
    if use_override:
        env["AGENT_MAIL_GIT_BASH_PATH"] = (
            str(git_bash) if os.name == "nt" else _git_bash_path(git_bash)
        )

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    hooks = json.loads(
        (copilot_dir / "hooks" / "mcp-agent-mail.json").read_text(encoding="utf-8")
    )
    managed = [handler for handlers in hooks["hooks"].values() for handler in handlers]
    assert len(managed) == 3
    assert (home / "agent-mail.env").is_file()
    assert not poison_log.exists()
    expected_bash = (
        str(git_bash).replace("\\", "/")
        if use_override and os.name == "nt"
        else "D:/Portable Git/bin/bash.exe"
    )
    for handler in managed:
        powershell = handler["powershell"].replace("\\", "/")
        assert expected_bash in powershell
        assert "usr/bin/bash.exe" not in powershell
        assert "System32" not in powershell


def test_copilot_native_windows_override_preserves_launcher_path() -> None:
    integrator = INTEGRATORS["copilot"].read_text(encoding="utf-8")

    assert '_candidate="${AGENT_MAIL_GIT_BASH_PATH//\\\\//}"' in integrator
    assert '[a-zA-Z]:/*) _WINDOWS_BASH="$_candidate" ;;' in integrator
    assert '_candidate="$(cygpath -u "$AGENT_MAIL_GIT_BASH_PATH"' not in integrator


def test_copilot_wsl_windows_profile_defaults_vscode_to_windows_appdata_without_writes(
    tmp_path: Path,
) -> None:
    env, _, _, _ = _simulated_wsl_windows_copilot_env(
        tmp_path,
        include_target_jq=True,
    )
    env.pop("VSCODE_MCP_CONFIG_PATH")
    env.pop("VSCODE_USER_DATA_DIR", None)
    env["APPDATA"] = r"D:\Profiles\AppData\Roaming"
    project = tmp_path / "project"
    project.mkdir()
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--dry-run",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "/mnt/d/Profiles/AppData/Roaming/Code/User/mcp.json" in output
    assert _git_bash_path(tmp_path / "home" / ".config" / "Code") not in output
    assert "Copilot identity resolved by Windows hook runtime (client=copilot, slot=1)" in output
    assert "copilot-wsl-" not in output
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("appdata", "expected_error"),
    [
        (None, "appdata is unavailable"),
        ("/home/operator/.config", "appdata does not resolve to a windows drive"),
    ],
)
def test_copilot_wsl_windows_profile_invalid_appdata_fails_before_mutation(
    tmp_path: Path,
    appdata: str | None,
    expected_error: str,
) -> None:
    env, _, _, _ = _simulated_wsl_windows_copilot_env(
        tmp_path,
        include_target_jq=True,
    )
    env.pop("VSCODE_MCP_CONFIG_PATH")
    env.pop("VSCODE_USER_DATA_DIR", None)
    if appdata is None:
        env.pop("APPDATA", None)
    else:
        env["APPDATA"] = appdata
    project = tmp_path / "project"
    project.mkdir()
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--dry-run",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert expected_error in output.lower()
    assert "VSCODE_MCP_CONFIG_PATH" in output
    assert "copilot-wsl-" not in output
    assert _tree_snapshot(tmp_path) == before


def test_copilot_wsl_windows_profile_explicit_vscode_path_wins_without_appdata(
    tmp_path: Path,
) -> None:
    env, _, _, _ = _simulated_wsl_windows_copilot_env(
        tmp_path,
        include_target_jq=True,
    )
    env.pop("VSCODE_USER_DATA_DIR", None)
    env.pop("APPDATA", None)
    explicit_vscode = env["VSCODE_MCP_CONFIG_PATH"]
    project = tmp_path / "project"
    project.mkdir()
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--dry-run",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert explicit_vscode in output
    assert "appdata is unavailable" not in output.lower()
    assert "Copilot identity resolved by Windows hook runtime (client=copilot, slot=1)" in output
    assert _tree_snapshot(tmp_path) == before


def test_copilot_wsl_windows_profile_embeds_only_target_git_bash_tools(
    tmp_path: Path,
) -> None:
    env, copilot_dir, outer_tools, target_tools = _simulated_wsl_windows_copilot_env(
        tmp_path,
        include_target_jq=True,
    )
    project = tmp_path / "project"
    _init_git_repo(project)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    wrapper = copilot_dir / "hooks" / "mcp-agent-mail" / "hook_wrapper.sh"
    wrapper_text = wrapper.read_text(encoding="utf-8")
    target_dir_q = _git_bash_path(target_tools).replace(" ", r"\ ")
    outer_dir_q = _git_bash_path(outer_tools).replace(" ", r"\ ")
    assert f"export PATH={target_dir_q}:" in wrapper_text
    assert outer_dir_q not in wrapper_text
    assert "/usr/local/bin:/usr/bin:/bin:/mingw64/bin:/mingw32/bin" in wrapper_text
    assert _git_bash_path(copilot_dir) not in wrapper_text
    assert (
        '_AM_HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" '
        '&& pwd -P)" || exit 1'
    ) in wrapper_text
    assert (
        'exec "$_AM_HOOK_BASH" "${_AM_HOOK_DIR}/agent_mail_hook.sh" "$@"'
        in wrapper_text
    )
    hooks = json.loads(
        (copilot_dir / "hooks" / "mcp-agent-mail.json").read_text(encoding="utf-8")
    )
    handlers = [handler for group in hooks["hooks"].values() for handler in group]
    assert all(
        "D:\\Portable Git\\bin\\bash.exe" in handler["powershell"]
        for handler in handlers
    )
    assert all("usr\\bin\\bash.exe" not in handler["powershell"] for handler in handlers)
    system32 = tmp_path / "Windows" / "System32"
    system32.mkdir(parents=True)
    poison_log = tmp_path / "wsl-target-poisoned-bash.log"
    poisoned_bash = system32 / "bash"
    poisoned_bash.write_text(
        "#!/bin/sh\n"
        "printf invoked >\"$POISON_LOG\"\n"
        "exit 97\n",
        encoding="utf-8",
        newline="\n",
    )
    poisoned_bash.chmod(0o700)
    payload = {
        "cwd": _git_bash_path(project),
        "session_id": "wsl-windows-target-wrapper",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }

    execution = subprocess.run(
        [
            # Run the simulated target shell *through* a real one. It is a
            # shebang script that happens to be named bash.exe, which the POSIX
            # loader honours and CreateProcess does not: naming it as argv[0] on
            # Windows gets WinError 2 for the /c/... spelling and WinError 216
            # ("not a valid Win32 application") for the native one, because the
            # file is text. Passing it as an argument keeps $1.. identical to
            # direct execution, so nothing about the simulation changes.
            BASH,
            env["FAKE_TARGET_BASH_POSIX"],
            r"D:\Profiles\Copilot\hooks\mcp-agent-mail\hook_wrapper.sh",
            "session-start",
        ],
        cwd=ROOT,
        env={
            **env,
            "BASH_ENV": "",
            "CDPATH": _git_bash_path(tmp_path / "poisoned-cdpath"),
            "PATH": _git_bash_path(system32),
            "POISON_LOG": _git_bash_path(poison_log),
        },
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert execution.returncode == 0, execution.stderr
    assert execution.stderr == ""
    assert not poison_log.exists()
    output = json.loads(execution.stdout)
    assert "Agent Mail is not activated for /owner/repo" in output["additionalContext"]


def test_copilot_wsl_windows_profile_missing_target_jq_fails_before_mutation(
    tmp_path: Path,
) -> None:
    env, _, _, _ = _simulated_wsl_windows_copilot_env(
        tmp_path,
        include_target_jq=False,
    )
    project = tmp_path / "project"
    project.mkdir()
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--dry-run",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "cannot resolve required runtime command: jq" in (
        result.stdout + result.stderr
    ).lower()
    assert _tree_snapshot(tmp_path) == before


def test_copilot_wsl_windows_profile_failing_target_jq_fails_before_mutation(
    tmp_path: Path,
) -> None:
    env, _, _, target_tools = _simulated_wsl_windows_copilot_env(
        tmp_path,
        include_target_jq=True,
    )
    target_jq = target_tools / "jq"
    target_jq.write_text(
        "#!/bin/sh\nexit 73\n",
        encoding="utf-8",
        newline="\n",
    )
    target_jq.chmod(0o700)
    project = tmp_path / "project"
    project.mkdir()
    before = _tree_snapshot(tmp_path)

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--dry-run",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "failed the dirname/git/curl/jq runtime preflight" in (
        result.stdout + result.stderr
    ).lower()
    assert _tree_snapshot(tmp_path) == before


def test_copilot_installed_wrapper_ignores_path_poisoned_system32_bash(
    tmp_path: Path,
) -> None:
    env, copilot_dir, _, fake_bin = _simulated_wsl_windows_copilot_env(
        tmp_path,
        include_target_jq=True,
    )
    home = tmp_path / "home"
    project = tmp_path / "project"
    system32 = tmp_path / "Windows" / "System32"
    system32.mkdir(parents=True)
    _init_git_repo(project)
    (project / ".agent-mail-project-id").write_text(
        "project-id\n",
        encoding="utf-8",
        newline="\n",
    )
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
if [[ ${1:-} == --version ]]; then printf 'test curl\n'; exit 0; fi
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
printf '%s\n' "$PATH" > "$FAKE_PATH_LOG"
printf '%s\n' "$tool" >> "$FAKE_CURL_LOG"
case "$tool" in
  ensure_project) result='{"human_key":"/owner/repo"}' ;;
  register_agent)
    name="$(printf '%s' "$body" | jq -r '.params.arguments.name')"
    printf '%s\n' "$name" > "$FAKE_AGENT_LOG"
    result="$(jq -nc --arg name "$name" \
      '{name:$name,registration_token:"registration-token",retired_at:null}')"
    ;;
  start_agent_execution)
    result='{"id":"11111111-1111-4111-8111-111111111111","status":"active","reused":false}'
    ;;
  fetch_inbox) result='[]' ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    install = subprocess.run(
        [
            BASH,
            _git_bash_path(INTEGRATORS["copilot"]),
            "--yes",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert install.returncode == 0, install.stderr
    installed_hooks = copilot_dir / "hooks" / "mcp-agent-mail"
    installed_wrapper = installed_hooks / "hook_wrapper.sh"
    wrapper_text = installed_wrapper.read_text(encoding="utf-8")
    assert f"{_git_bash_path(fake_bin).replace(' ', r'\ ')}:/usr/local/bin" in wrapper_text
    assert "/usr/local/bin:/usr/bin:/bin" in wrapper_text
    assert '"${PATH:+:${PATH}}"' in wrapper_text
    assert '_AM_HOOK_BASH="$(command -p -v bash)" || exit 1' in wrapper_text
    assert 'exec "$_AM_HOOK_BASH" ' in wrapper_text
    assert "exec bash " not in wrapper_text
    assert 'exec "$BASH" ' not in wrapper_text
    poison_log = tmp_path / "poisoned-system32-bash.log"
    poisoned_bash = system32 / "bash"
    poisoned_bash.write_text(
        "#!/bin/sh\n"
        "printf invoked >\"$POISON_LOG\"\n"
        "exit 97\n",
        encoding="utf-8",
        newline="\n",
    )
    poisoned_bash.chmod(0o700)
    curl_log = tmp_path / "curl.log"
    agent_log = tmp_path / "agent.log"
    path_log = tmp_path / "path.log"
    runtime_env = {
        **env,
        "BASH_ENV": "",
        "CDPATH": _git_bash_path(tmp_path / "poisoned-cdpath"),
        "PATH": _git_bash_path(system32),
        "FAKE_AGENT_LOG": _git_bash_path(agent_log),
        "FAKE_CURL_LOG": _git_bash_path(curl_log),
        "FAKE_PATH_LOG": _git_bash_path(path_log),
        "POISON_LOG": _git_bash_path(poison_log),
    }
    payload = {
        "cwd": _git_bash_path(project),
        "session_id": "copilot-system32-path",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "test-model",
    }

    execution = subprocess.run(
        [
            # Run the simulated target shell *through* a real one. It is a
            # shebang script that happens to be named bash.exe, which the POSIX
            # loader honours and CreateProcess does not: naming it as argv[0] on
            # Windows gets WinError 2 for the /c/... spelling and WinError 216
            # ("not a valid Win32 application") for the native one, because the
            # file is text. Passing it as an argument keeps $1.. identical to
            # direct execution, so nothing about the simulation changes.
            BASH,
            env["FAKE_TARGET_BASH_POSIX"],
            r"D:\Profiles\Copilot\hooks\mcp-agent-mail\hook_wrapper.sh",
            "session-start",
        ],
        cwd=ROOT,
        env=runtime_env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert execution.returncode == 0, execution.stderr
    assert execution.stderr == ""
    assert not poison_log.exists()
    registered_agent = agent_log.read_text(encoding="utf-8").strip()
    assert re.fullmatch(
        r"copilot-(?:linux|mac|win|wsl|other)-[a-z0-9._-]+-1",
        registered_agent,
    )
    assert curl_log.read_text(encoding="utf-8").splitlines() == [
        "ensure_project",
        "register_agent",
        "start_agent_execution",
        "fetch_inbox",
    ]
    runtime_path = path_log.read_text(encoding="utf-8").strip().split(":")
    assert runtime_path[:4] == [
        _git_bash_path(fake_bin),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    assert runtime_path[-1] == _git_bash_path(system32)
    assert json.loads(execution.stdout) == {
        "additionalContext": (
            f"Agent Mail: you are {registered_agent} on /owner/repo. "
            "Root execution: 11111111-1111-4111-8111-111111111111. "
            "Unread inbox: 0 message(s), 0 high/urgent. Use fetch_inbox before "
            "proceeding when mail is pending."
        )
    }
    state = home / ".state" / "agent-mail"
    assert json.loads((state / "credentials.json").read_text(encoding="utf-8")) == {
        "/owner/repo": {registered_agent: "registration-token"}
    }
    granted_files = list((state / "granted").iterdir())
    assert len(granted_files) == 1
    assert granted_files[0].read_text(encoding="utf-8") == registered_agent


def test_copilot_session_start_uses_direct_context_and_no_unactivated_network(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _install_fake_curl(
        fake_bin,
        "#!/usr/bin/env bash\nprintf called >> \"$FAKE_CURL_LOG\"\nexit 97\n",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env.update(
        {
            "AGENT_MAIL_HOOK_CLIENT": "copilot",
            "AGENT_MAIL_HOOK_SLOT": "1",
            "FAKE_CURL_LOG": _git_bash_path(curl_log),
        }
    )
    payload = {
        "cwd": str(repo),
        "session_id": "copilot-unactivated",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
            "session-start",
        ],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert set(output) == {"additionalContext"}
    assert "Agent Mail is not activated for /owner/repo" in output["additionalContext"]
    assert not curl_log.exists()


def test_copilot_registration_stops_when_credential_cannot_be_persisted(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    state.mkdir()
    credentials = state / "credentials.json"
    credentials.write_text("not-json\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
printf '%s\n' "$tool" >> "$FAKE_CURL_LOG"
case "$tool" in
  ensure_project) result='{"human_key":"/owner/repo"}' ;;
  register_agent)
    name="$(printf '%s' "$body" | jq -r '.params.arguments.name')"
    result="$(jq -nc --arg name "$name" '{name:$name,registration_token:"registration-token",retired_at:null}')"
    ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env.update(
        {
            "AGENT_MAIL_HOOK_CLIENT": "copilot",
            "AGENT_MAIL_HOOK_SLOT": "1",
            "FAKE_CURL_LOG": _git_bash_path(curl_log),
        }
    )
    payload = {
        "cwd": str(repo),
        "session_id": "copilot-invalid-credentials",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
            "session-start",
        ],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "could not persist the registration credential" in json.loads(result.stdout)[
        "additionalContext"
    ]
    assert curl_log.read_text(encoding="utf-8").splitlines() == [
        "ensure_project",
        "register_agent",
    ]
    assert credentials.read_text(encoding="utf-8") == "not-json\n"
    granted_files = list((state / "granted").iterdir())
    assert len(granted_files) == 1
    assert granted_files[0].read_text(encoding="utf-8")


def test_copilot_runtime_uses_bounded_sha256_rate_keys_for_long_projects(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    long_project = f"owner/{'segment' * 50}"
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", f"git@github.com:{long_project}.git"],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
case "$tool" in
  ensure_project) result='{"human_key":"ok"}' ;;
  register_agent)
    name="$(printf '%s' "$body" | jq -r '.params.arguments.name')"
    result="$(jq -nc --arg name "$name" '{name:$name,registration_token:"registration-token",retired_at:null}')"
    ;;
  fetch_inbox) result='[{"id":77,"importance":"urgent","from":"sender","subject":"act"}]' ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    env.update({"AGENT_MAIL_HOOK_CLIENT": "copilot", "AGENT_MAIL_HOOK_SLOT": "1"})
    payload = json.dumps(
        {
            "cwd": str(repo),
            "session_id": "copilot-long-project",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
    )

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
            "stop",
        ],
        cwd=repo,
        env=env,
        input=payload,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
    rate_files = list((state / "rate").iterdir())
    assert len(rate_files) == 2
    assert all(len(path.name.encode()) <= 255 for path in rate_files)
    assert all(len(path.name.rsplit("-", 1)[-1].split(".", 1)[0]) == 32 for path in rate_files)


def test_copilot_install_and_runtime_keep_credentials_off_process_argv() -> None:
    integrator = INTEGRATORS["copilot"].read_text(encoding="utf-8")
    runtime = (ROOT / "scripts" / "hooks" / "codex_notify.sh").read_text(
        encoding="utf-8"
    )

    assert "--arg token" not in integrator
    assert "copilot mcp add" not in integrator
    assert "--arg t" not in runtime
    assert "AGENT_MAIL_JQ_REGISTRATION_TOKEN" in runtime


def test_auto_installer_continues_after_failure_then_exits_nonzero(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    codex_dir = home / "codex-profile"
    vscode_mcp = home / "vscode-mcp.json"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    codex_dir.mkdir()

    # Detection is driven only by this explicit profile.  Invalid managed JSON
    # makes the Codex child fail without touching any real user configuration.
    (codex_dir / "hooks.json").write_text("not-json\n", encoding="utf-8")
    _install_forbidden_client_toolchain_guards(fake_bin)
    jq_path = shutil.which("jq")
    assert jq_path is not None
    _install_bash_command_forwarder(fake_bin, "jq", jq_path)
    bash_env, bash_tmp = _install_bash_env(home, fake_bin)
    env = {
        **os.environ,
        "HOME": _git_bash_path(home),
        "BASH_ENV": bash_env,
        "CODEX_HOME": _git_bash_path(codex_dir),
        "XDG_STATE_HOME": _git_bash_path(home / ".state"),
        "XDG_CONFIG_HOME": _git_bash_path(home / ".config"),
        "VSCODE_MCP_CONFIG_PATH": _git_bash_path(vscode_mcp),
        "INTEGRATION_MCP_URL": "https://hermes.example/mcp/",
        "INTEGRATION_BEARER_TOKEN": "never-log-prefix-123456-never-log-suffix",
        # Exclude real Claude/Codex binaries while retaining ordinary POSIX
        # tools. The explicit CODEX_HOME above still detects Codex.
        #
        # _posix_tool_dirs() is what makes "ordinary POSIX tools" true on Windows
        # as well: Git for Windows keeps git and curl in /mingw64/bin, not
        # /usr/bin, so the two literals below are a complete toolchain on Linux
        # and a partial one here. That went unnoticed while the harness started
        # scripts through Git\bin\bash.exe, which quietly prepends /mingw64/bin
        # during MSYS setup — the test was relying on the shell to repair a PATH
        # it had declared complete.
        "PATH": ":".join(
            dict.fromkeys(
                (
                    _git_bash_path(fake_bin),
                    *_posix_tool_dirs(),
                    "/usr/bin",
                    "/bin",
                )
            )
        ),
        "TMPDIR": bash_tmp,
        "TEMP": bash_tmp,
        "TMP": bash_tmp,
    }

    result = subprocess.run(
        [
            BASH,
            "-x",
            _git_bash_path(AUTO_INSTALLER),
            "--yes",
            "--debug",
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "never-log-prefix" not in combined
    assert "never-log-suffix" not in combined
    assert "Bearer token: configured (value hidden)" in combined
    assert "Summary" in combined
    assert "Failed integrations: Codex CLI" in combined
    # A failed client does not prevent the remaining integrations from running.
    assert vscode_mcp.is_file()


def _install_wslpath_profile_mapper(
    fake_bin: Path,
    windows_codex_home: Path,
) -> None:
    fake_uname = fake_bin / "uname"
    fake_uname.write_text(
        "if [[ ${1:-} == -r ]]; then\n"
        "  printf '%s\\n' '6.6.0-microsoft-standard-WSL2'\n"
        "else\n"
        "  printf '%s\\n' 'Linux'\n"
        "fi\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_uname.chmod(0o700)
    mapper = fake_bin / "wslpath"
    mapper.write_text(
        "case $1:$2 in\n"
        "  '-u:D:\\Profiles\\Codex')\n"
        "    printf '%s\\n' \"$FAKE_WINDOWS_CODEX_HOME\" ;;\n"
        "  -w:*bash.exe)\n"
        "    printf '%s\\n' 'D:\\Portable Git\\bin\\bash.exe' ;;\n"
        "  -w:*hook_wrapper.sh)\n"
        "    printf '%s\\n' 'D:\\Profiles\\Codex\\hooks\\mcp-agent-mail\\hook_wrapper.sh' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    mapper.chmod(0o700)
    assert windows_codex_home.is_absolute()


def _install_fake_codex(executable: Path) -> None:
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    executable.chmod(0o700)


def _install_isolated_shell_toolchain(fake_bin: Path) -> None:
    commands = (
        "awk",
        "bash",
        "cat",
        "chmod",
        "cp",
        "curl",
        "cut",
        "date",
        "dirname",
        "git",
        "grep",
        "head",
        "hostname",
        "jq",
        "mkdir",
        "mktemp",
        "mv",
        "pwd",
        "sed",
        "sort",
        "tail",
        "tr",
        "uname",
        "wc",
    )
    for command in commands:
        target = shutil.which(command)
        assert target is not None, command
        forwarder = fake_bin / command
        forwarder.write_text(
            f"exec {shlex.quote(_git_bash_path(target))} \"$@\"\n",
            encoding="utf-8",
            newline="\n",
        )
        forwarder.chmod(0o700)


def _run_auto_installer(
    env: dict[str, str],
    project: Path,
    *extra_arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            BASH,
            _git_bash_path(AUTO_INSTALLER),
            "--yes",
            *extra_arguments,
            "--project-dir",
            _git_bash_path(project),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _managed_codex_commands(profile: Path) -> list[str]:
    hooks = json.loads((profile / "hooks.json").read_text(encoding="utf-8"))
    return [
        handler["command"]
        for groups in hooks["hooks"].values()
        for group in groups
        for handler in group["hooks"]
        if "mcp-agent-mail" in handler.get("command", "")
    ]


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "Simulates a WSL host: fakes uname as Linux, sets WSL_DISTRO_NAME and "
        "resolves Windows profiles through a wslpath shim. Native Windows/MSYS "
        "is not a coherent emulator of that — the real shell is MSYS, /mnt/c "
        "does not exist, and the Copilot runtime probe has no consistent answer "
        "for which hook Bash was selected. The WSL contract these pin still runs "
        "on the Ubuntu and macOS legs, which is where it is meaningful."
    ),
)
def test_auto_installer_isolates_windows_desktop_and_native_wsl_codex_profiles(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    windows_profile = tmp_path / "windows-profile"
    native_profile = home / ".codex"
    native_bin = tmp_path / "native-bin"
    for directory in (home, project, fake_bin, windows_profile, native_profile):
        directory.mkdir(parents=True)

    (windows_profile / "config.toml").write_text(
        'model_reasoning_effort = "ultra"\n',
        encoding="utf-8",
    )
    (native_profile / "config.toml").write_text(
        'model_reasoning_effort = "xhigh"\n',
        encoding="utf-8",
    )
    windows_state = windows_profile / "state_5.sqlite"
    native_state = native_profile / "state_5.sqlite"
    windows_state.write_bytes(b"desktop-state")
    native_state.write_bytes(b"native-state")
    bundled_codex = windows_profile / "bin" / "wsl" / "build" / "codex"
    native_codex = native_bin / "codex"
    _install_fake_codex(bundled_codex)
    _install_fake_codex(native_codex)
    _install_wslpath_profile_mapper(fake_bin, windows_profile)
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": r"D:\Profiles\Codex",
        "FAKE_WINDOWS_CODEX_HOME": _git_bash_path(windows_profile),
        "MSYS_NO_PATHCONV": "1",
        "WSL_DISTRO_NAME": "Ubuntu",
    }
    env["PATH"] = ":".join(
        (
            _git_bash_path(bundled_codex.parent),
            _git_bash_path(native_bin),
            env["PATH"],
        )
    )

    first = _run_auto_installer(env, project)

    assert first.returncode == 0, first.stdout + first.stderr
    assert "Codex profiles: Windows/Desktop=" in first.stdout
    assert first.stdout.count("-- Integrating Codex CLI (") == 2
    windows_config = tomllib.loads(
        (windows_profile / "config.toml").read_text(encoding="utf-8")
    )
    native_config = tomllib.loads(
        (native_profile / "config.toml").read_text(encoding="utf-8")
    )
    assert windows_config["model_reasoning_effort"] == "ultra"
    assert native_config["model_reasoning_effort"] == "xhigh"
    for config in (windows_config, native_config):
        assert config["mcp_servers"]["mcp_agent_mail"]["url"] == (
            "https://hermes.example/mcp/"
        )
    windows_commands = _managed_codex_commands(windows_profile)
    native_commands = _managed_codex_commands(native_profile)
    assert len(windows_commands) == 6
    assert len(native_commands) == 6
    expected_runtime_commands = {
        'bash "${CODEX_HOME:-${HOME}/.codex}/hooks/'
        f'mcp-agent-mail/hook_wrapper.sh" {event}'
        for event in (
            "session-start",
            "subagent-start",
            "subagent-stop",
            "heartbeat",
            "stop",
            "session-end",
        )
    }
    assert set(windows_commands) == expected_runtime_commands
    assert set(native_commands) == expected_runtime_commands
    assert windows_state.read_bytes() == b"desktop-state"
    assert native_state.read_bytes() == b"native-state"

    before_rerun = {
        "windows": _tree_snapshot(windows_profile),
        "native": _tree_snapshot(native_profile),
    }
    second = _run_auto_installer(env, project)

    assert second.returncode == 0, second.stdout + second.stderr
    assert _tree_snapshot(windows_profile) == before_rerun["windows"]
    assert _tree_snapshot(native_profile) == before_rerun["native"]
    assert len(_managed_codex_commands(windows_profile)) == 6
    assert len(_managed_codex_commands(native_profile)) == 6

    before_dry_run = _tree_snapshot(tmp_path)
    dry_run = _run_auto_installer(env, project, "--dry-run")

    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    assert "no files or directories were changed" in dry_run.stdout
    assert _tree_snapshot(tmp_path) == before_dry_run

    # The same installed command must resolve the profile at hook runtime.
    # This is the three-mode boundary: native Windows selects commandWindows,
    # while both WSL CLI and Desktop-in-WSL execute this POSIX command and let
    # the actual Linux process identify itself as WSL.
    for label, profile in (
        ("desktop-wsl", windows_profile),
        ("native-wsl", native_profile),
    ):
        runtime = profile / "hooks" / "mcp-agent-mail" / "agent_mail_hook.sh"
        runtime.write_text(
            "#!/usr/bin/env bash\n"
            '. "$(dirname "$0")/agent_mail_common.sh"\n'
            f"printf '{label}|%s' \"$(am_platform)\"\n",
            encoding="utf-8",
            newline="\n",
        )
        runtime.chmod(0o700)
        probe = subprocess.run(
            [BASH, "-c", windows_commands[0]],
            cwd=project,
            env={**env, "CODEX_HOME": _git_bash_path(profile)},
            check=False,
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout == f"{label}|wsl"


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "Simulates a WSL host: fakes uname as Linux, sets WSL_DISTRO_NAME and "
        "resolves Windows profiles through a wslpath shim. Native Windows/MSYS "
        "is not a coherent emulator of that — the real shell is MSYS, /mnt/c "
        "does not exist, and the Copilot runtime probe has no consistent answer "
        "for which hook Bash was selected. The WSL contract these pin still runs "
        "on the Ubuntu and macOS legs, which is where it is meaningful."
    ),
)
def test_auto_installer_keeps_custom_native_codex_home_single_target(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    custom_profile = Path("/home/agent-mail-test-custom-codex")
    for directory in (home, project, fake_bin):
        directory.mkdir(parents=True)
    _install_fake_codex(fake_bin / "codex")
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": str(custom_profile),
        "WSL_DISTRO_NAME": "Ubuntu",
    }
    before = _tree_snapshot(tmp_path)

    result = _run_auto_installer(env, project, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("-- Integrating Codex CLI...") == 1
    assert "Codex profiles: Windows/Desktop=" not in result.stdout
    assert "merge Codex hooks into /home/agent-mail-test-custom-codex/hooks.json" in (
        result.stdout
    )
    assert not (home / ".codex").exists()
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "Simulates a WSL host: fakes uname as Linux, sets WSL_DISTRO_NAME and "
        "resolves Windows profiles through a wslpath shim. Native Windows/MSYS "
        "is not a coherent emulator of that — the real shell is MSYS, /mnt/c "
        "does not exist, and the Copilot runtime probe has no consistent answer "
        "for which hook Bash was selected. The WSL contract these pin still runs "
        "on the Ubuntu and macOS legs, which is where it is meaningful."
    ),
)
def test_auto_installer_keeps_windows_profile_single_without_native_wsl_codex(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    windows_profile = tmp_path / "windows-profile"
    for directory in (home, project, fake_bin, windows_profile):
        directory.mkdir(parents=True)
    (windows_profile / "config.toml").write_text(
        'model_reasoning_effort = "ultra"\n',
        encoding="utf-8",
    )
    bundled_codex = windows_profile / "bin" / "wsl" / "build" / "codex"
    _install_fake_codex(bundled_codex)
    base_env = _integration_env(home, fake_bin)
    _install_isolated_shell_toolchain(fake_bin)
    _install_wslpath_profile_mapper(fake_bin, windows_profile)
    env = {
        **os.environ,
        **base_env,
        "CODEX_HOME": r"D:\Profiles\Codex",
        "FAKE_WINDOWS_CODEX_HOME": _git_bash_path(windows_profile),
        "MSYS_NO_PATHCONV": "1",
        "WSL_DISTRO_NAME": "Ubuntu",
        "PATH": ":".join(
            (_git_bash_path(bundled_codex.parent), _git_bash_path(fake_bin))
        ),
    }
    before = _tree_snapshot(tmp_path)

    result = _run_auto_installer(env, project, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("-- Integrating Codex CLI...") == 1
    assert "Codex profiles: Windows/Desktop=" not in result.stdout
    assert _git_bash_path(windows_profile / "hooks.json") in result.stdout
    assert _git_bash_path(home / ".codex" / "hooks.json") not in result.stdout
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "evidence",
    ["credentials", "granted"],
)
def test_legacy_identity_evidence_requires_explicit_migration(
    tmp_path: Path,
    evidence: str,
) -> None:
    result = _bash(
        f"""
        export AGENT_MAIL_STATE_DIR={shlex.quote(_git_bash_path(tmp_path))}
        export AGENT_MAIL_ENV_FILE=/dev/null
        unset AGENT_MAIL_AGENT
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        legacy="$(am_legacy_agent_name 1)"
        if [[ {shlex.quote(evidence)} == credentials ]]; then
          am_cred_put /owner/repo "$legacy" legacy-token
        elif [[ {shlex.quote(evidence)} == granted ]]; then
          granted="$(am_legacy_granted_name_file /owner/repo)"
          mkdir -p "$(dirname "$granted")"
          printf '%s' "$legacy" > "$granted"
        fi
        am_identity_migration_pair /owner/repo codex 1
        """
    )

    assert result.returncode == 0, result.stderr
    legacy, canonical = result.stdout.split("\t")
    assert legacy.endswith("-1")
    canonical_parts = canonical.split("-")
    assert canonical_parts[0] == "codex"
    assert canonical_parts[1] in {"linux", "wsl", "win", "mac", "other"}
    assert canonical_parts[-1] == "1"


def test_final_credential_overrides_a_stale_legacy_granted_marker(
    tmp_path: Path,
) -> None:
    result = _bash(
        f"""
        uname() {{ printf Linux; }}
        hostname() {{ printf home; }}
        export WSL_DISTRO_NAME=Ubuntu
        export AGENT_MAIL_STATE_DIR={shlex.quote(_git_bash_path(tmp_path))}
        export AGENT_MAIL_ENV_FILE=/dev/null
        unset AGENT_MAIL_AGENT
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        canonical="$(am_agent_name codex 1)"
        legacy="$(am_legacy_agent_name 1)"
        am_cred_put /owner/repo "$canonical" canonical-token
        granted="$(am_granted_name_file /owner/repo codex 1)"
        mkdir -p "$(dirname "$granted")"
        printf '%s' "$legacy" > "$granted"
        if am_identity_migration_pair /owner/repo codex 1; then
          printf 'blocked\n'
        else
          printf 'safe\n'
        fi
        export AM_PROJECT_FOR_NAME=/owner/repo
        am_agent_name codex 1
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["safe", "codex-wsl-home-1"]


def test_existing_client_scoped_credential_is_not_misclassified_as_legacy(
    tmp_path: Path,
) -> None:
    result = _bash(
        f"""
        uname() {{ printf Linux; }}
        hostname() {{ printf home; }}
        export WSL_DISTRO_NAME=Ubuntu
        export AGENT_MAIL_STATE_DIR={shlex.quote(_git_bash_path(tmp_path))}
        export AGENT_MAIL_ENV_FILE=/dev/null
        unset AGENT_MAIL_AGENT
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        am_cred_put /owner/repo codex-wsl-home-1 canonical-token
        if am_identity_migration_pair /owner/repo codex 1; then
          printf blocked
        else
          printf safe
        fi
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "safe"


@pytest.mark.parametrize(
    ("evidence", "expected_old"),
    [
        ("credential", "home-wsl-codex-1"),
        ("granted", "home-wsl-codex-1"),
        ("coerced-granted", "MaroonPuma"),
    ],
)
def test_old_order_client_state_requires_final_identity_migration(
    tmp_path: Path,
    evidence: str,
    expected_old: str,
) -> None:
    result = _bash(
        f"""
        uname() {{ printf Linux; }}
        hostname() {{ printf home; }}
        export WSL_DISTRO_NAME=Ubuntu
        export AGENT_MAIL_STATE_DIR={shlex.quote(_git_bash_path(tmp_path))}
        export AGENT_MAIL_ENV_FILE=/dev/null
        unset AGENT_MAIL_AGENT
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        transitional=home-wsl-codex-1
        if [[ {shlex.quote(evidence)} == credential ]]; then
          am_cred_put /owner/repo "$transitional" transitional-token
        else
          granted="$(am_transitional_granted_name_file /owner/repo codex 1)"
          mkdir -p "$(dirname "$granted")"
          if [[ {shlex.quote(evidence)} == coerced-granted ]]; then
            printf '%s' MaroonPuma > "$granted"
            am_cred_put /owner/repo MaroonPuma transitional-token
          else
            printf '%s' "$transitional" > "$granted"
          fi
        fi
        am_project_is_active /owner/repo codex 1 . || exit 91
        am_identity_migration_pair /owner/repo codex 1
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected_old}\tcodex-wsl-home-1"


def test_preplatform_orphan_does_not_hide_exact_legacy_migration(
    tmp_path: Path,
) -> None:
    result = _bash(
        f"""
        uname() {{ printf Linux; }}
        hostname() {{ printf holzera; }}
        grep() {{ return 1; }}
        unset WSL_DISTRO_NAME AGENT_MAIL_AGENT
        export AGENT_MAIL_STATE_DIR={shlex.quote(_git_bash_path(tmp_path))}
        export AGENT_MAIL_ENV_FILE=/dev/null
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}

        am_cred_put /owner/repo holzera-1 orphan-token
        if am_identity_migration_pair /owner/repo claude 1; then
          printf 'orphan-blocked\n'
        else
          printf 'orphan-ignored\n'
        fi

        am_cred_put /owner/repo holzera-linux-1 legacy-token
        am_identity_migration_pair /owner/repo claude 1
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "orphan-ignored",
        "holzera-linux-1\tclaude-linux-holzera-1",
    ]


@pytest.mark.parametrize(
    ("script_name", "arguments"),
    [
        ("session_start.sh", []),
        ("codex_notify.sh", ["session-start"]),
    ],
)
def test_session_start_makes_no_request_for_unactivated_repository(
    tmp_path: Path,
    script_name: str,
    arguments: list[str],
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _install_fake_curl(
        fake_bin,
        "#!/usr/bin/env bash\nprintf called >> \"$FAKE_CURL_LOG\"\nexit 97\n",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env["FAKE_CURL_LOG"] = _git_bash_path(curl_log)
    payload = {
        "cwd": str(repo),
        "session_id": "session-unactivated",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
    }

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / script_name),
            *arguments,
        ],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "not activated for /owner/repo" in context
    assert "No Agent Mail network request was made" in context
    assert not curl_log.exists()
    assert not (state / "credentials.json").exists()


@pytest.mark.parametrize(
    ("script_name", "arguments", "client"),
    [
        ("session_start.sh", [], "claude"),
        ("codex_notify.sh", ["session-start"], "codex"),
    ],
)
def test_session_start_never_restores_retired_identity(
    tmp_path: Path,
    script_name: str,
    arguments: list[str],
    client: str,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
printf '%s\n' "$tool" >> "$FAKE_CURL_LOG"
case "$tool" in
  ensure_project) result='{"human_key":"/owner/repo"}' ;;
  register_agent)
    name="$(printf '%s' "$body" | jq -r '.params.arguments.name')"
    result="$(jq -nc --arg name "$name" \
      '{name:$name,registration_token:"registration-token",retired_at:"2026-08-11T08:00:00Z"}')"
    ;;
  unretire_agent) result='{"status":"active"}' ;;
  fetch_inbox) result='[]' ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env["FAKE_CURL_LOG"] = _git_bash_path(curl_log)
    _, claude_name, codex_name = _hook_names(env)
    agent_name = claude_name if client == "claude" else codex_name
    _put_credential(state, agent_name)
    payload = {
        "cwd": str(repo),
        "session_id": "retired-session",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
    }

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / script_name),
            *arguments,
        ],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert f"identity {agent_name}" in context
    assert "is retired and cannot receive new mail" in context
    assert "will not restore a manually decommissioned identity" in context
    assert "explicitly restore" in context
    calls = curl_log.read_text(encoding="utf-8").splitlines()
    assert calls[-1] == "register_agent"
    assert "unretire_agent" not in calls
    assert "fetch_inbox" not in calls


def test_inherited_project_override_cannot_activate_a_different_repository(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo-b"
    _init_git_repo(repo)
    _install_fake_curl(
        fake_bin,
        "#!/usr/bin/env bash\nprintf called >> \"$FAKE_CURL_LOG\"\nexit 97\n",
    )
    env = _hook_env(home, state, fake_bin)
    _, _, codex_name = _hook_names(env)
    (state / "credentials.json").write_text(
        json.dumps({"/owner/project-a": {codex_name: "project-a-token"}}),
        encoding="utf-8",
    )
    env["AGENT_MAIL_PROJECT_KEY"] = "/owner/project-a"
    curl_log = tmp_path / "curl.log"
    env["FAKE_CURL_LOG"] = _git_bash_path(curl_log)
    payload = {
        "cwd": str(repo),
        "session_id": "poisoned-parent-project",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
    }

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
            "session-start",
        ],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "not activated for /owner/repo" in context
    assert "/owner/project-a" not in context
    assert "No Agent Mail network request was made" in context
    assert not curl_log.exists()


@pytest.mark.parametrize(
    ("marker", "contents"),
    [
        (".agent-mail-project-id", "project-id\n"),
        (".agent-mail.yaml", "project_uid: product-project-id\n"),
    ],
)
def test_explicit_repository_marker_activates_global_hook_locally(
    tmp_path: Path,
    marker: str,
    contents: str,
) -> None:
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    _init_git_repo(repo)
    state.mkdir()
    (repo / marker).write_text(contents, encoding="utf-8")

    result = _bash(
        f"""
        cd {shlex.quote(_git_bash_path(repo))}
        export AGENT_MAIL_STATE_DIR={shlex.quote(_git_bash_path(state))}
        export AGENT_MAIL_ENV_FILE=/dev/null
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        am_project_is_active /owner/repo codex 1 .
        """
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "contents",
    [
        "metadata:\n  project_uid: nested-value\n",
        "description: |\n  project_uid: literal-text-only\n",
        "project_uid:\n",
        "project_uid: null\n",
        "project_uid: ''\n",
    ],
)
def test_non_top_level_or_empty_project_uid_does_not_activate_or_call_server(
    tmp_path: Path,
    contents: str,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail.yaml").write_text(contents, encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        "#!/usr/bin/env bash\nprintf called >> \"$FAKE_CURL_LOG\"\nexit 97\n",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env["FAKE_CURL_LOG"] = _git_bash_path(curl_log)
    payload = {
        "cwd": str(repo),
        "session_id": "invalid-discovery-opt-in",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
    }

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
            "session-start",
        ],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "not activated for /owner/repo" in context
    assert "No Agent Mail network request was made" in context
    assert not curl_log.exists()
    assert not (state / "credentials.json").exists()


def test_relative_state_directory_fails_closed_without_repo_write_or_network(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        "#!/usr/bin/env bash\nprintf called >> \"$FAKE_CURL_LOG\"\nexit 97\n",
    )
    env = _hook_env(home, state, fake_bin)
    env["AGENT_MAIL_STATE_DIR"] = "relative-agent-mail-state"
    curl_log = tmp_path / "curl.log"
    env["FAKE_CURL_LOG"] = _git_bash_path(curl_log)
    payload = {
        "cwd": str(repo),
        "session_id": "relative-state-dir",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
    }

    result = subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
            "session-start",
        ],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "disabled because AGENT_MAIL_ENV_FILE or AGENT_MAIL_STATE_DIR" in context
    assert "No Agent Mail network request was made" in context
    assert not curl_log.exists()
    assert not (repo / "relative-agent-mail-state").exists()
    assert not (repo / "credentials.json").exists()


def test_windows_user_paths_are_normalized_for_mocked_git_bash(tmp_path: Path) -> None:
    env_file = tmp_path / "git-bash-agent-mail.env"
    state = tmp_path / "git-bash-state"
    env_file.write_text(
        "AGENT_MAIL_URL=https://hermes.example/mcp/\n"
        "HTTP_BEARER_TOKEN=git-bash-bearer\n",
        encoding="utf-8",
        newline="\n",
    )
    result = _bash(
        f"""
        uname() {{ printf MINGW64_NT-10.0; }}
        cygpath() {{
          [ "$1" = -m ] || return 2
          case "$2" in
            'Q:\\AgentMail\\shared.env') printf '%s' {shlex.quote(_git_bash_path(env_file))} ;;
            'Q:\\AgentMail\\state') printf '%s' {shlex.quote(_git_bash_path(state))} ;;
            *) return 3 ;;
          esac
        }}
        export AGENT_MAIL_ENV_FILE='Q:\\AgentMail\\shared.env'
        export AGENT_MAIL_STATE_DIR='Q:\\AgentMail\\state'
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        printf '%s\n' "$AM_PATH_CONFIGURATION_VALID" "$AM_STATE_DIR" "$HTTP_BEARER_TOKEN"
        if am_normalize_runtime_user_path relative-state >/dev/null; then exit 91; fi
        am_cred_put /owner/repo codex-win-build-box-1 registration-token
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["1", _git_bash_path(state), "git-bash-bearer"]
    credentials = json.loads((state / "credentials.json").read_text(encoding="utf-8"))
    assert credentials["/owner/repo"]["codex-win-build-box-1"] == "registration-token"


def test_windows_user_paths_are_normalized_for_mocked_wsl(tmp_path: Path) -> None:
    env_file = tmp_path / "wsl-agent-mail.env"
    state = tmp_path / "wsl-state"
    env_file.write_text(
        "AGENT_MAIL_URL=https://hermes.example/mcp/\n"
        "HTTP_BEARER_TOKEN=wsl-bearer\n",
        encoding="utf-8",
        newline="\n",
    )
    result = _bash(
        f"""
        uname() {{ printf Linux; }}
        wslpath() {{
          [ "$1" = -u ] || return 2
          case "$2" in
            'Q:\\AgentMail\\shared.env') printf '%s' {shlex.quote(_git_bash_path(env_file))} ;;
            'Q:\\AgentMail\\state') printf '%s' {shlex.quote(_git_bash_path(state))} ;;
            *) return 3 ;;
          esac
        }}
        export WSL_DISTRO_NAME=Ubuntu
        export AGENT_MAIL_ENV_FILE='Q:\\AgentMail\\shared.env'
        export AGENT_MAIL_STATE_DIR='Q:\\AgentMail\\state'
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        printf '%s\n' "$AM_PATH_CONFIGURATION_VALID" "$AM_STATE_DIR" "$HTTP_BEARER_TOKEN"
        am_cred_put /owner/repo codex-wsl-build-box-1 registration-token
        """,
        env={"MSYS_NO_PATHCONV": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["1", _git_bash_path(state), "wsl-bearer"]
    credentials = json.loads((state / "credentials.json").read_text(encoding="utf-8"))
    assert credentials["/owner/repo"]["codex-wsl-build-box-1"] == "registration-token"


def test_reservation_guard_makes_no_request_for_unactivated_repository(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    target = repo / "module.py"
    target.write_text("pass\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        "#!/usr/bin/env bash\nprintf called >> \"$FAKE_CURL_LOG\"\nexit 97\n",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env["FAKE_CURL_LOG"] = _git_bash_path(curl_log)
    payload = {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "tool_input": {"file_path": str(target)},
    }

    result = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "reservations_warn.sh")],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not curl_log.exists()


@pytest.mark.parametrize(
    ("script_name", "arguments", "client"),
    [
        ("session_start.sh", [], "claude"),
        ("codex_notify.sh", ["session-start"], "codex"),
    ],
)
def test_session_start_fails_closed_without_network_for_legacy_identity(
    tmp_path: Path,
    script_name: str,
    arguments: list[str],
    client: str,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _install_fake_curl(
        fake_bin,
        "#!/usr/bin/env bash\nprintf called >> \"$FAKE_CURL_LOG\"\nexit 97\n",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env["FAKE_CURL_LOG"] = _git_bash_path(curl_log)
    legacy, claude_name, codex_name = _hook_names(env)
    expected = claude_name if client == "claude" else codex_name
    _put_credential(state, legacy, "legacy-token")

    payload = {
        "cwd": str(repo),
        "session_id": "session-legacy",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
    }
    result = subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / script_name),
            *arguments,
        ],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert legacy in context
    assert expected in context
    assert "No Agent Mail network request was made" in context
    assert "preserving Agent.id" in context
    assert not curl_log.exists()
    credentials = json.loads((state / "credentials.json").read_text(encoding="utf-8"))
    assert credentials == {"/owner/repo": {legacy: "legacy-token"}}


@pytest.mark.parametrize(
    "script_name",
    ["inbox_check.sh", "inbox_watch.sh", "autoreserve.sh"],
)
def test_identity_sensitive_hooks_report_legacy_migration_without_network(
    tmp_path: Path,
    script_name: str,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    target = repo / "module.py"
    target.write_text("pass\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        "#!/usr/bin/env bash\nprintf called >> \"$FAKE_CURL_LOG\"\nexit 97\n",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env["FAKE_CURL_LOG"] = _git_bash_path(curl_log)
    legacy, canonical, _ = _hook_names(env)
    _put_credential(state, legacy, "legacy-token")
    expected = _bash(
        f"""
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        am_identity_migration_message \
          {shlex.quote(legacy)} {shlex.quote(canonical)}
        """,
        env=env,
    )
    assert expected.returncode == 0, expected.stderr

    result = _run_identity_sensitive_hook(script_name, repo, target, env)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    if script_name == "inbox_watch.sh":
        assert result.stdout == f"{expected.stdout}\n"
    else:
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"] == {
            "hookEventName": "PostToolUse",
            "additionalContext": expected.stdout,
        }
    assert "legacy-token" not in result.stdout
    assert "test-bearer" not in result.stdout
    assert not curl_log.exists()
    credentials = json.loads((state / "credentials.json").read_text(encoding="utf-8"))
    assert credentials == {"/owner/repo": {legacy: "legacy-token"}}


@pytest.mark.parametrize(
    "script_name",
    ["inbox_check.sh", "inbox_watch.sh", "autoreserve.sh"],
)
@pytest.mark.parametrize("repository_active", [False, True])
def test_identity_sensitive_hooks_do_not_report_migration_without_legacy_state(
    tmp_path: Path,
    script_name: str,
    repository_active: bool,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    target = repo / "module.py"
    target.write_text("pass\n", encoding="utf-8")
    if repository_active:
        (repo / ".agent-mail-project-id").write_text(
            "project-id\n",
            encoding="utf-8",
        )
    _install_fake_curl(
        fake_bin,
        "#!/usr/bin/env bash\nprintf called >> \"$FAKE_CURL_LOG\"\nexit 97\n",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env["FAKE_CURL_LOG"] = _git_bash_path(curl_log)

    result = _run_identity_sensitive_hook(script_name, repo, target, env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert not curl_log.exists()


def test_codex_stop_registers_first_call_then_rate_limits_before_network(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
printf '%s\n' "$tool" >> "$FAKE_CURL_LOG"
case "$tool" in
  ensure_project)
    result='{"human_key":"/owner/repo"}'
    ;;
  register_agent)
    name="$(printf '%s' "$body" | jq -r '.params.arguments.name')"
    result="$(jq -nc --arg name "$name" \
      '{name:$name,registration_token:"registration-token",retired_at:null}')"
    ;;
  fetch_inbox)
    result="${FAKE_INBOX_JSON:-[]}"
    ;;
  *)
    result='{}'
    ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    curl_log = tmp_path / "curl.log"
    env.update(
        {"FAKE_CURL_LOG": _git_bash_path(curl_log), "FAKE_INBOX_JSON": "[]"}
    )
    payload = json.dumps(
        {
            "cwd": str(repo),
            "session_id": "session-stop",
            "hook_event_name": "Stop",
            "turn_id": "turn-1",
            "stop_hook_active": False,
            "model": "gpt-5.6",
        }
    )
    command = [
        BASH,
        _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
        "stop",
    ]

    first = subprocess.run(
        command,
        cwd=repo,
        env=env,
        input=payload,
        check=False,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {}
    assert curl_log.read_text(encoding="utf-8").splitlines() == [
        "ensure_project",
        "register_agent",
        "fetch_inbox",
    ]

    second = subprocess.run(
        command,
        cwd=repo,
        env=env,
        input=payload,
        check=False,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == {}
    assert curl_log.read_text(encoding="utf-8").splitlines() == [
        "ensure_project",
        "register_agent",
        "fetch_inbox",
    ]

    urgent_env = {
        **env,
        "AGENT_MAIL_INTERVAL": "0",
        "FAKE_INBOX_JSON": json.dumps(
            [
                {
                    "id": 839,
                    "from": "operator",
                    "subject": "preserve Agent.id",
                    "importance": "urgent",
                }
            ]
        ),
    }
    urgent_first = subprocess.run(
        command,
        cwd=repo,
        env=urgent_env,
        input=payload,
        check=False,
        capture_output=True,
        text=True,
    )
    urgent_first_json = json.loads(urgent_first.stdout)
    assert urgent_first_json["decision"] == "block"
    assert "#839" in urgent_first_json["reason"]

    urgent_second = subprocess.run(
        command,
        cwd=repo,
        env=urgent_env,
        input=payload,
        check=False,
        capture_output=True,
        text=True,
    )
    urgent_second_json = json.loads(urgent_second.stdout)
    assert "decision" not in urgent_second_json
    assert "systemMessage" in urgent_second_json

    other_session_payload = json.dumps(
        {
            "cwd": str(repo),
            "session_id": "session-stop-parallel",
            "hook_event_name": "Stop",
            "turn_id": "turn-parallel",
            "stop_hook_active": False,
            "model": "gpt-5.6",
        }
    )
    other_session = subprocess.run(
        command,
        cwd=repo,
        env=urgent_env,
        input=other_session_payload,
        check=False,
        capture_output=True,
        text=True,
    )
    other_session_json = json.loads(other_session.stdout)
    assert other_session_json["decision"] == "block"
    assert "#839" in other_session_json["reason"]


def test_hook_remembers_granted_names_per_client_and_slot(tmp_path: Path) -> None:
    result = _bash(
        f"""
        export AGENT_MAIL_STATE_DIR={shlex.quote(_git_bash_path(tmp_path))}
        export AGENT_MAIL_ENV_FILE=/dev/null
        export AGENT_MAIL_PROJECT_KEY=/owner/repo
        export AM_PROJECT_FOR_NAME=/owner/repo
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}

        am_granted_name_put /owner/repo server-claude claude 1
        am_granted_name_put /owner/repo server-codex codex 1
        am_granted_name_put /owner/repo server-codex-slot-2 codex 2

        printf '%s\n' "$(am_agent_name claude 1)"
        printf '%s\n' "$(am_agent_name codex 1)"
        printf '%s\n' "$(am_agent_name codex 2)"
        """,
        env={"AGENT_MAIL_AGENT": ""},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["server-claude", "server-codex", "server-codex-slot-2"]


@pytest.mark.parametrize(
    ("client", "slot", "error_fragment"),
    [
        ("unknown", "1", "Unsupported Agent Mail client"),
        ("claude", "0", "Agent Mail slot must be a positive integer"),
    ],
)
def test_hook_rejects_invalid_client_or_slot(
    tmp_path: Path,
    client: str,
    slot: str,
    error_fragment: str,
) -> None:
    result = _bash(
        f"""
        export AGENT_MAIL_STATE_DIR={shlex.quote(_git_bash_path(tmp_path))}
        export AGENT_MAIL_ENV_FILE=/dev/null
        export AGENT_MAIL_PROJECT_KEY=/owner/repo
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        am_agent_name '{client}' '{slot}'
        """,
        env={"AGENT_MAIL_AGENT": ""},
    )

    assert result.returncode != 0
    assert error_fragment in result.stderr


def test_hook_mail_ui_bearer_surface_is_limited_to_file_reservations() -> None:
    mail_paths = {
        match.group(0)
        for path in INSTALLED_HOOK_SOURCES
        for match in re.finditer(r"/mail/[A-Za-z0-9_/-]+", path.read_text(encoding="utf-8"))
    }

    assert mail_paths == {"/mail/api/file-reservations"}


def test_codex_execution_hooks_preserve_native_lifecycle_and_private_capability(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Hook Test",
            "-c",
            "user.email=hook@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
arguments="$(printf '%s' "$body" | jq -c '.params.arguments // {}')"
jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
  '{tool:$tool,arguments:$arguments}' >> "$FAKE_CALLS_LOG"
case "$tool" in
  register_agent)
    name="$(printf '%s' "$arguments" | jq -r '.name')"
    result="$(jq -nc --arg name "$name" \
      '{name:$name,registration_token:"registration-token",retired_at:null}')"
    ;;
  start_agent_execution)
    kind="$(printf '%s' "$arguments" | jq -r '.kind')"
    if [[ $kind == session && ! -f $FAKE_ROOT_START_ATTEMPT ]]; then
      : > "$FAKE_ROOT_START_ATTEMPT"
      exit 7
    fi
    if [[ $kind == subagent ]]; then
      result='{"id":"22222222-2222-4222-8222-222222222222","status":"active","ancestor_execution_ids":["11111111-1111-4111-8111-111111111111"],"reused":false}'
    else
      result='{"id":"11111111-1111-4111-8111-111111111111","status":"active","ancestor_execution_ids":[],"reused":true}'
    fi
    ;;
  heartbeat_agent_execution)
    result='{"id":"11111111-1111-4111-8111-111111111111","status":"active"}'
    ;;
  end_agent_execution)
    result='{"execution":{"status":"completed"},"already_ended":false,"released_reservations":0}'
    ;;
  fetch_inbox) result='[]' ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    _, _, agent = _hook_names(env)
    _put_credential(state, agent)
    calls_log = tmp_path / "calls.jsonl"
    root_attempt = tmp_path / "root-start-attempt"
    env.update(
        {
            "FAKE_CALLS_LOG": _git_bash_path(calls_log),
            "FAKE_ROOT_START_ATTEMPT": _git_bash_path(root_attempt),
            "AGENT_MAIL_EXECUTION_HEARTBEAT_INTERVAL": "60",
        }
    )
    root_payload = {
        "cwd": str(repo),
        "session_id": "thr-native-root-123",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
        "permission_mode": "default",
    }
    command = [
        BASH,
        _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
        "session-start",
    ]

    failed_start = subprocess.run(
        command,
        cwd=repo,
        env=env,
        input=json.dumps(root_payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert failed_start.returncode == 0, failed_start.stderr
    failed_context = json.loads(failed_start.stdout)["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "root execution could not be started" in failed_context
    root_state_path = next((state / "executions").glob("*.json"))
    starting_state = json.loads(root_state_path.read_text(encoding="utf-8"))
    assert starting_state["status"] == "starting"
    assert "execution_token" not in starting_state
    root_token_path = Path(f"{root_state_path}.token")
    root_token = root_token_path.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{64}", root_token)
    _assert_hook_private_mode(root_state_path)
    _assert_hook_private_mode(root_token_path)

    recovered_start = subprocess.run(
        command,
        cwd=repo,
        env=env,
        input=json.dumps(root_payload),
        check=False,
        capture_output=True,
        text=True,
    )

    assert recovered_start.returncode == 0, recovered_start.stderr
    recovered_context = json.loads(recovered_start.stdout)["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "Root execution: 11111111-1111-4111-8111-111111111111" in recovered_context
    active_root_state = json.loads(root_state_path.read_text(encoding="utf-8"))
    assert "execution_token" not in active_root_state
    assert root_token_path.read_text(encoding="utf-8").strip() == root_token
    assert active_root_state["status"] == "active"
    assert active_root_state["ancestor_execution_ids"] == []
    _assert_hook_private_mode(root_state_path)
    _assert_hook_private_mode(root_token_path)
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    root_starts = [
        item["arguments"]
        for item in calls
        if item["tool"] == "start_agent_execution"
        and item["arguments"]["kind"] == "session"
    ]
    assert len(root_starts) == 2
    assert {item["execution_token"] for item in root_starts} == {root_token}
    assert {item["external_id"] for item in root_starts} == {"thr-native-root-123"}
    assert all(item["lifecycle_protocol_version"] == 1 for item in root_starts)
    assert all(item["model"] == "gpt-5.6" for item in root_starts)
    assert all(item["permission_mode"] == "default" for item in root_starts)
    # Compare places, not spellings: git reports C:/... on Windows while
    # str(WindowsPath) spells C:\..., and the two name the same directory.
    assert all(Path(item["repo_root"]) == repo for item in root_starts)
    assert all(Path(item["worktree_path"]) == repo for item in root_starts)
    assert all(re.fullmatch(r"[0-9a-f]{40}", item["head_sha"]) for item in root_starts)

    marker_rel = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "agent-mail/execution-id",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    marker_path = Path(marker_rel)
    if not marker_path.is_absolute():
        marker_path = repo / marker_path
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    # Compare the place, not the spelling: git reports C:/... on Windows while
    # str(WindowsPath) spells C:\..., and both name the same worktree.
    assert Path(marker["worktree_path"]) == repo
    assert {k: v for k, v in marker.items() if k != "worktree_path"} == {
        "execution_id": "11111111-1111-4111-8111-111111111111",
        "status": "active",
        "kind": "session",
        "heartbeat_ts": marker["heartbeat_ts"],
        "ancestor_execution_ids": [],
    }
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", marker["heartbeat_ts"]
    )
    assert root_token not in marker_path.read_text(encoding="utf-8")
    assert root_token not in failed_start.stdout + recovered_start.stdout
    _assert_hook_private_mode(marker_path)
    common_source = HOOK_COMMON.read_text(encoding="utf-8")
    assert re.search(r"(?m)^umask 077$", common_source)
    assert 'chmod 600 "$token_file"' in common_source
    assert 'chmod 600 "$tmp"' in common_source
    assert not list((state / "executions").glob("*.tmp"))

    compatible_command = f"""
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        am_read_payload
        am_execution_compatible_ids_for_payload /owner/repo \
          {shlex.quote(agent)} codex
    """
    compatible = subprocess.run(
        [BASH, "-c", compatible_command],
        cwd=repo,
        env=env,
        input=json.dumps(root_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert compatible.returncode == 0, compatible.stderr
    assert json.loads(compatible.stdout) == [
        "11111111-1111-4111-8111-111111111111"
    ]
    future_marker = {**marker, "heartbeat_ts": "2999-01-01T00:00:00Z"}
    marker_path.write_text(json.dumps(future_marker), encoding="utf-8")
    future_compatible = subprocess.run(
        [BASH, "-c", compatible_command],
        cwd=repo,
        env=env,
        input=json.dumps(root_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert future_compatible.returncode != 0
    assert future_compatible.stdout == ""

    active_replay = subprocess.run(
        command,
        cwd=repo,
        env=env,
        input=json.dumps({**root_payload, "source": "compact"}),
        check=False,
        capture_output=True,
        text=True,
    )
    assert active_replay.returncode == 0, active_replay.stderr
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    assert sum(item["tool"] == "start_agent_execution" for item in calls) == 3
    refreshed_marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert refreshed_marker["status"] == "active"
    assert refreshed_marker["heartbeat_ts"] != "2999-01-01T00:00:00Z"

    child_payload = {
        "cwd": str(repo),
        "session_id": "thr-native-root-123",
        "turn_id": "turn-native-789",
        "hook_event_name": "SubagentStart",
        "agent_id": "agent-native-child-456",
        "agent_type": "explorer",
        "model": "gpt-5.6",
        "permission_mode": "default",
    }
    child_start = subprocess.run(
        [*command[:-1], "subagent-start"],
        cwd=repo,
        env=env,
        input=json.dumps(child_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert child_start.returncode == 0, child_start.stderr
    child_context = json.loads(child_start.stdout)["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "subagent execution 22222222-2222-4222-8222-222222222222" in child_context
    states = {
        path: json.loads(path.read_text(encoding="utf-8"))
        for path in (state / "executions").glob("*.json")
    }
    child_state_path, child_state = next(
        (path, item) for path, item in states.items() if item["kind"] == "subagent"
    )
    assert "execution_token" not in child_state
    child_token_path = Path(f"{child_state_path}.token")
    child_token = child_token_path.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{64}", child_token)
    _assert_hook_private_mode(child_state_path)
    _assert_hook_private_mode(child_token_path)
    assert child_state["external_id"] == "agent-native-child-456"
    assert child_state["ancestor_execution_ids"] == [
        "11111111-1111-4111-8111-111111111111"
    ]
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    child_args = next(
        item["arguments"]
        for item in calls
        if item["tool"] == "start_agent_execution"
        and item["arguments"]["kind"] == "subagent"
    )
    assert child_args["external_id"] == "agent-native-child-456"
    assert child_args["execution_token"] == child_token
    assert child_args["parent_execution_id"] == (
        "11111111-1111-4111-8111-111111111111"
    )
    assert child_args["parent_execution_token"] == root_token
    assert child_args["turn_id"] == "turn-native-789"
    assert child_args["agent_type"] == "explorer"
    assert child_args["lifecycle_protocol_version"] == 1
    assert json.loads(marker_path.read_text())["execution_id"] == (
        "11111111-1111-4111-8111-111111111111"
    )
    assert child_token not in child_start.stdout + marker_path.read_text()

    # SubagentStart itself stays read-only with respect to a same-checkout
    # marker. The first real child tool event publishes the exact child context
    # (including root lineage), and the next parent event restores the root.
    child_heartbeat_payload = {
        **child_payload,
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_use_id": "child-marker-handoff",
        "tool_input": {"file_path": str(repo / "module.py")},
        "tool_response": {"success": True},
    }
    child_heartbeat = subprocess.run(
        [*command[:-1], "heartbeat"],
        cwd=repo,
        env=env,
        input=json.dumps(child_heartbeat_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert child_heartbeat.returncode == 0, child_heartbeat.stderr
    child_marker = json.loads(marker_path.read_text())
    assert child_marker["execution_id"] == child_state["execution_id"]
    assert child_marker["ancestor_execution_ids"] == [
        active_root_state["execution_id"]
    ]
    parent_handoff = subprocess.run(
        [*command[:-1], "heartbeat"],
        cwd=repo,
        env=env,
        input=json.dumps(
            {
                **root_payload,
                "hook_event_name": "PostToolUse",
                "tool_name": "Read",
                "tool_use_id": "root-marker-restore",
                "tool_input": {"file_path": str(repo / "module.py")},
                "tool_response": {"success": True},
            }
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert parent_handoff.returncode == 0, parent_handoff.stderr
    assert json.loads(marker_path.read_text())["execution_id"] == (
        active_root_state["execution_id"]
    )

    heartbeat_payload = {
        **root_payload,
        "hook_event_name": "PostToolUse",
        "turn_id": "turn-heartbeat",
        "tool_name": "Bash",
        "tool_use_id": "tool-1",
        "tool_input": {"command": "git status"},
        "tool_response": {"output": "clean"},
    }
    heartbeat_command = [*command[:-1], "heartbeat"]
    heartbeat_outputs: list[str] = []
    for _ in range(2):
        heartbeat = subprocess.run(
            heartbeat_command,
            cwd=repo,
            env=env,
            input=json.dumps(heartbeat_payload),
            check=False,
            capture_output=True,
            text=True,
        )
        assert heartbeat.returncode == 0, heartbeat.stderr
        heartbeat_outputs.append(heartbeat.stdout)
    first_warning_output = json.loads(heartbeat_outputs[0])
    first_warning = first_warning_output["systemMessage"]
    assert "command payload is intentionally not parsed for paths" in first_warning
    assert first_warning_output["hookSpecificOutput"] == {
        "hookEventName": "PostToolUse",
        "additionalContext": first_warning,
    }
    assert heartbeat_outputs[1] == ""
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    heartbeat_args = [
        item["arguments"]
        for item in calls
        if item["tool"] == "heartbeat_agent_execution"
    ]
    assert {
        (item["execution_id"], item["execution_token"])
        for item in heartbeat_args
    } == {
        ("11111111-1111-4111-8111-111111111111", root_token),
        ("22222222-2222-4222-8222-222222222222", child_token),
    }
    assert all(item["project_key"] == "/owner/repo" for item in heartbeat_args)
    assert all(item["agent_name"] == agent for item in heartbeat_args)
    assert all(
        item["registration_token"] == "registration-token"
        for item in heartbeat_args
    )
    assert all(item["lifecycle_protocol_version"] == 1 for item in heartbeat_args)

    child_stop = subprocess.run(
        [*command[:-1], "subagent-stop"],
        cwd=repo,
        env=env,
        input=json.dumps(
            {
                **child_payload,
                "hook_event_name": "SubagentStop",
                "stop_hook_active": False,
                "agent_transcript_path": str(repo / "child.jsonl"),
                "last_assistant_message": "done",
            }
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert child_stop.returncode == 0, child_stop.stderr
    assert json.loads(child_stop.stdout) == {}
    provisional_calls = [
        json.loads(line) for line in calls_log.read_text().splitlines()
    ]
    assert not any(
        item["tool"] == "end_agent_execution"
        and item["arguments"]["execution_id"]
        == "22222222-2222-4222-8222-222222222222"
        for item in provisional_calls
    )
    continued_child = subprocess.run(
        heartbeat_command,
        cwd=repo,
        env=env,
        input=json.dumps(
            {
                **child_payload,
                "hook_event_name": "PostToolUse",
                "tool_name": "Read",
                "tool_use_id": "continued-child-tool",
                "tool_input": {"file_path": str(repo / "module.py")},
                "tool_response": {"success": True},
            }
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert continued_child.returncode == 0, continued_child.stderr
    assert json.loads(child_state_path.read_text())["status"] == "active"
    repeated_stop = subprocess.run(
        [*command[:-1], "subagent-stop"],
        cwd=repo,
        env=env,
        input=json.dumps(
            {
                **child_payload,
                "hook_event_name": "SubagentStop",
                "stop_hook_active": True,
                "agent_transcript_path": str(repo / "child.jsonl"),
                "last_assistant_message": "now done",
            }
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert repeated_stop.returncode == 0, repeated_stop.stderr
    assert json.loads(repeated_stop.stdout) == {}
    assert json.loads(child_state_path.read_text())["status"] == "stopping"
    parent_return = subprocess.run(
        heartbeat_command,
        cwd=repo,
        env=env,
        input=json.dumps({**heartbeat_payload, "tool_use_id": "parent-return"}),
        check=False,
        capture_output=True,
        text=True,
    )
    assert parent_return.returncode == 0, parent_return.stderr
    root_end = subprocess.run(
        [*command[:-1], "session-end"],
        cwd=repo,
        env=env,
        input=json.dumps(
            {**root_payload, "hook_event_name": "SessionEnd", "reason": "other"}
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert root_end.returncode == 0, root_end.stderr
    assert root_end.stdout == ""
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    end_args = [
        item["arguments"] for item in calls if item["tool"] == "end_agent_execution"
    ]
    assert [(item["execution_id"], item["execution_token"]) for item in end_args] == [
        ("22222222-2222-4222-8222-222222222222", child_token),
        ("11111111-1111-4111-8111-111111111111", root_token),
    ]
    assert all(item["lifecycle_protocol_version"] == 1 for item in end_args)
    assert json.loads(marker_path.read_text())["status"] == "completed"

    terminal_replay = subprocess.run(
        command,
        cwd=repo,
        env=env,
        input=json.dumps({**root_payload, "source": "resume"}),
        check=False,
        capture_output=True,
        text=True,
    )
    assert terminal_replay.returncode == 0, terminal_replay.stderr
    terminal_context = json.loads(terminal_replay.stdout)["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "Root execution: 11111111-1111-4111-8111-111111111111" in terminal_context
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    assert sum(item["tool"] == "start_agent_execution" for item in calls) == 6
    resumed_root = [
        item["arguments"]
        for item in calls
        if item["tool"] == "start_agent_execution"
        and item["arguments"]["kind"] == "session"
    ][-1]
    assert resumed_root["external_id"] == "thr-native-root-123#run-2"
    resumed_state = next(
        item
        for item in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in (state / "executions").glob("*.json")
        )
        if item.get("lifecycle_generation") == 2
    )
    assert resumed_state["status"] == "active"


def test_session_end_racing_start_rpc_ends_returned_execution_without_publication(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    start_entered = tmp_path / "start-entered"
    start_release = tmp_path / "start-release"
    calls_log = tmp_path / "calls.jsonl"
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
if [[ $* == *'/mail/api/file-reservations'* ]]; then
  cat >/dev/null
  printf '%s\n200' '{"active":0,"reservations":[]}'
  exit 0
fi
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
arguments="$(printf '%s' "$body" | jq -c '.params.arguments // {}')"
jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
  '{tool:$tool,arguments:$arguments}' >> "$FAKE_CALLS_LOG"
case "$tool" in
  ensure_project) result='{"human_key":"/owner/repo"}' ;;
  register_agent)
    name="$(printf '%s' "$arguments" | jq -r '.name')"
    result="$(jq -nc --arg name "$name" \
      '{name:$name,registration_token:"registration-token",retired_at:null}')"
    ;;
  start_agent_execution)
    : > "$FAKE_START_ENTERED"
    waited=0
    while [[ ! -f $FAKE_START_RELEASE && $waited -lt 1000 ]]; do
      sleep 0.01
      waited=$((waited + 1))
    done
    [[ -f $FAKE_START_RELEASE ]] || exit 8
    result='{"id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc","status":"active","ancestor_execution_ids":[],"reused":false}'
    ;;
  end_agent_execution)
    result='{"execution":{"status":"completed"},"already_ended":false,"released_reservations":0}'
    ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    _, agent, _ = _hook_names(env)
    _put_credential(state, agent)
    jq_audit = tmp_path / "jq-state-reads.log"
    real_jq = shutil.which("jq")
    assert real_jq is not None
    fake_jq = fake_bin / "jq"
    fake_jq.write_text(
        f"""#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    */executions/irrelevant-*.json)
      printf '%s\n' "$arg" >> "$FAKE_JQ_STATE_READS"
      ;;
  esac
done
exec {shlex.quote(_git_bash_path(Path(real_jq).resolve()))} "$@"
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_jq.chmod(0o700)
    env.update(
        {
            "FAKE_CALLS_LOG": _git_bash_path(calls_log),
            "FAKE_START_ENTERED": _git_bash_path(start_entered),
            "FAKE_START_RELEASE": _git_bash_path(start_release),
            "FAKE_JQ_STATE_READS": _git_bash_path(jq_audit),
        }
    )
    execution_dir = state / "executions"
    execution_dir.mkdir(parents=True)
    irrelevant_state = json.dumps(
        {
            "version": 1,
            "project": "/owner/irrelevant",
            "agent": "codex-linux-irrelevant-1",
            "client": "codex",
            "session_id": "irrelevant-session",
            "lifecycle_generation": 1,
            "kind": "session",
            "native_id": "irrelevant-session",
            "status": "active",
        }
    )
    # Stay below NTFS's per-file hard-link limit so Windows does not fall back
    # to thousands of full copies. These are still 10,000 distinct directory
    # entries that an unbounded execution-state scan would open through jq.
    for chunk in range(10):
        irrelevant_seed = state / f"irrelevant-state-fixture-{chunk}"
        irrelevant_seed.write_text(irrelevant_state, encoding="utf-8")
        for offset in range(1_000):
            index = chunk * 1_000 + offset
            irrelevant_path = execution_dir / f"irrelevant-{index:05d}.json"
            try:
                os.link(irrelevant_seed, irrelevant_path)
            except OSError:
                shutil.copyfile(irrelevant_seed, irrelevant_path)

    payload = {
        "cwd": str(repo),
        "session_id": "session-end-during-start",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "permission_mode": "default",
    }
    start_process = subprocess.Popen(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_start.sh")],
        cwd=repo,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert start_process.stdin is not None
    start_process.stdin.write(json.dumps(payload))
    start_process.stdin.close()

    # Wall-clock generosity, not laxness: reaching the start RPC costs a chain
    # of bash+jq spawns whose price is the machine's, not the hook's.  Measured
    # on the native Windows host: a full session-start takes 15.5 s while the
    # hook is perfectly healthy, so the old 10 s window failed on spawn cost
    # alone.  The loop exits the moment the marker appears, so a fast machine
    # pays nothing for the headroom.
    deadline = time.monotonic() + (60 if os.name == "nt" else 20)
    while not start_entered.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert start_entered.exists(), "start_agent_execution RPC was not reached"
    manifests = list((state / "execution-manifests").glob("*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert len(manifest["state_files"]) == 1
    root_state_path = execution_dir / manifest["state_files"][0]
    assert json.loads(root_state_path.read_text(encoding="utf-8"))["status"] == (
        "starting"
    )
    root_token_path = Path(f"{root_state_path}.token")
    root_token = root_token_path.read_text(encoding="utf-8").strip()
    assert manifest["state_files"] == [root_state_path.name]

    # Ten thousand unrelated execution records must not be opened by this
    # lifecycle's SessionEnd. The jq wrapper gives a deterministic I/O proof;
    # this is not a timing assertion that could pass on a fast machine.
    end = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_end.sh")],
        cwd=repo,
        env=env,
        input=json.dumps({**payload, "hook_event_name": "SessionEnd"}),
        check=False,
        capture_output=True,
        text=True,
    )
    assert end.returncode == 0, end.stderr
    pending_state = json.loads(root_state_path.read_text(encoding="utf-8"))
    assert pending_state["status"] == "end_requested"
    assert pending_state["requested_end_status"] == "completed"
    assert "execution_id" not in pending_state
    assert not jq_audit.exists() or jq_audit.read_text(encoding="utf-8") == ""
    calls_before_release = [
        json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["tool"] for item in calls_before_release].count(
        "end_agent_execution"
    ) == 0

    start_release.write_text("continue\n", encoding="utf-8")
    assert start_process.wait(timeout=15) == 0
    assert start_process.stdout is not None
    assert start_process.stderr is not None
    start_stdout = start_process.stdout.read()
    start_stderr = start_process.stderr.read()
    final_state = json.loads(root_state_path.read_text(encoding="utf-8"))
    assert final_state["status"] == "completed"
    assert final_state["execution_id"] == "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    assert "requested_end_status" not in final_state
    assert "end_requested_at" not in final_state
    calls = [
        json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()
    ]
    end_arguments = next(
        item["arguments"] for item in calls if item["tool"] == "end_agent_execution"
    )
    assert end_arguments == {
        "project_key": "/owner/repo",
        "agent_name": agent,
        "registration_token": "registration-token",
        "execution_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "execution_token": root_token,
        "status": "completed",
        "lifecycle_protocol_version": 1,
    }
    marker = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "agent-mail/execution-id",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert not Path(marker).exists()
    assert root_token not in start_stdout + start_stderr + end.stdout + end.stderr
    assert root_token not in root_state_path.read_text(encoding="utf-8")
    _assert_hook_private_mode(root_state_path)
    assert not root_token_path.exists()
    compact_keys = {
        "version",
        "project",
        "agent",
        "client",
        "session_id",
        "lifecycle_generation",
        "kind",
        "native_id",
        "external_id",
        "execution_id",
        "status",
        "end_source",
        "ended_locally_at",
    }
    assert set(final_state) <= compact_keys
    terminal_manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert terminal_manifest["client"] == "claude"
    assert terminal_manifest["status"] == "terminal"
    assert terminal_manifest["state_files"] == [root_state_path.name]
    assert terminal_manifest["retain_until_epoch"] > int(time.time())
    retention_markers = list((state / "execution-retention").glob("*/*.json"))
    assert len(retention_markers) == 1
    retention_marker = json.loads(retention_markers[0].read_text(encoding="utf-8"))
    assert retention_marker["manifest_file"] == manifests[0].name
    assert retention_marker["retain_until_epoch"] == terminal_manifest[
        "retain_until_epoch"
    ]
    _assert_hook_private_mode(manifests[0])
    _assert_hook_private_mode(retention_markers[0])
    assert not list((state / "executions").glob("heartbeat-*.stamp"))

    # Exercise the exact post-horizon deletion path without waiting a day.
    # Both sides of the manifest/marker pair must agree before pruning; the
    # 10,000 unrelated records remain untouched.
    prune_now = int(time.time())
    expired_at = prune_now - 1
    terminal_manifest["retain_until_epoch"] = expired_at
    manifests[0].write_text(json.dumps(terminal_manifest), encoding="utf-8")
    retention_marker["retain_until_epoch"] = expired_at
    retention_markers[0].write_text(json.dumps(retention_marker), encoding="utf-8")
    pruned = subprocess.run(
        [
            BASH,
            "-c",
            (
                f"source {shlex.quote(_git_bash_path(HOOK_COMMON))}; "
                "am_execution_retention_prune_marker "
                f"{shlex.quote(_git_bash_path(retention_markers[0]))} "
                f"{prune_now}"
            ),
        ],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert pruned.returncode == 0, pruned.stderr
    assert not root_state_path.exists()
    assert not manifests[0].exists()
    assert not retention_markers[0].exists()
    assert not list((state / "session-end-intents").glob("*.json"))
    assert sum(1 for _ in execution_dir.glob("irrelevant-*.json")) == 10_000
    assert not jq_audit.exists() or jq_audit.read_text(encoding="utf-8") == ""


def test_codex_old_session_end_cannot_end_resumed_generation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    calls_log = tmp_path / "calls.jsonl"
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
arguments="$(printf '%s' "$body" | jq -c '.params.arguments // {}')"
jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
  '{tool:$tool,arguments:$arguments}' >> "$FAKE_CALLS_LOG"
case "$tool" in
  register_agent)
    name="$(printf '%s' "$arguments" | jq -r '.name')"
    result="$(jq -nc --arg name "$name" \
      '{name:$name,registration_token:"registration-token",retired_at:null}')"
    ;;
  start_agent_execution)
    external_id="$(printf '%s' "$arguments" | jq -r '.external_id')"
    if [[ $external_id == *'#run-2' ]]; then
      execution_id=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb
    else
      execution_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa
    fi
    result="$(jq -nc --arg id "$execution_id" \
      '{id:$id,status:"active",ancestor_execution_ids:[],reused:false}')"
    ;;
  end_agent_execution)
    result='{"execution":{"status":"completed"},"already_ended":false,"released_reservations":0}'
    ;;
  fetch_inbox) result='[]' ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    _, _, agent = _hook_names(env)
    _put_credential(state, agent)
    env["FAKE_CALLS_LOG"] = _git_bash_path(calls_log)
    command = [
        BASH,
        _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
    ]
    payload = {
        "cwd": str(repo),
        "session_id": "codex-resumed-generation",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
        "permission_mode": "default",
    }
    started = subprocess.run(
        [*command, "session-start"],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert started.returncode == 0, started.stderr
    generation_one_state_path = next((state / "executions").glob("*.json"))
    generation_one = json.loads(
        generation_one_state_path.read_text(encoding="utf-8")
    )
    assert generation_one["lifecycle_generation"] == 1

    lock_path = Path(f"{generation_one_state_path}.lock")
    lock_holder = subprocess.Popen(
        [
            BASH,
            "-c",
            (
                f"source {shlex.quote(_git_bash_path(HOOK_COMMON))}; "
                f"am_lock_acquire {shlex.quote(_git_bash_path(lock_path))} || exit 1; "
                "printf 'ready\\n'; IFS= read -r _; "
                f"am_lock_release {shlex.quote(_git_bash_path(lock_path))}"
            ),
        ],
        cwd=repo,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert lock_holder.stdout is not None
    assert lock_holder.stdout.readline().strip() == "ready"

    end_process = subprocess.Popen(
        [*command, "session-end"],
        cwd=repo,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert end_process.stdin is not None
    end_process.stdin.write(json.dumps({**payload, "hook_event_name": "SessionEnd"}))
    end_process.stdin.close()
    end_process.stdin = None
    deadline = time.monotonic() + 5
    tombstone: dict[str, object] | None = None
    while time.monotonic() < deadline:
        intents = list((state / "session-end-intents").glob("*.json"))
        if intents:
            tombstone = json.loads(intents[0].read_text(encoding="utf-8"))
            if tombstone.get("status") == "ended":
                break
        time.sleep(0.01)
    assert tombstone is not None
    assert (tombstone["generation"], tombstone["status"]) == (1, "ended")

    resumed = subprocess.run(
        [*command, "session-start"],
        cwd=repo,
        env=env,
        input=json.dumps({**payload, "source": "resume"}),
        check=False,
        capture_output=True,
        text=True,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" in resumed.stdout

    assert lock_holder.stdin is not None
    lock_holder.stdin.write("release\n")
    lock_holder.stdin.flush()
    assert lock_holder.wait(timeout=5) == 0
    end_stdout, end_stderr = end_process.communicate(timeout=5)
    assert end_process.returncode == 0, end_stderr
    assert end_stdout == ""

    generations = {
        item["lifecycle_generation"]: item
        for path in (state / "executions").glob("*.json")
        if (item := json.loads(path.read_text(encoding="utf-8")))["kind"]
        == "session"
    }
    assert generations[1]["status"] == "completed"
    assert generations[2]["status"] == "active"
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    starts = [item["arguments"] for item in calls if item["tool"] == "start_agent_execution"]
    assert [item["external_id"] for item in starts] == [
        "codex-resumed-generation",
        "codex-resumed-generation#run-2",
    ]
    ends = [item["arguments"] for item in calls if item["tool"] == "end_agent_execution"]
    assert [item["execution_id"] for item in ends] == [
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    ]


def test_execution_marker_is_absolute_and_distinct_per_linked_worktree(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Hook Test",
            "-c",
            "user.email=hook@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    worktree_a = tmp_path / "worktree-a"
    worktree_b = tmp_path / "worktree-b"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "hook-wt-a", str(worktree_a)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "hook-wt-b", str(worktree_b)],
        check=True,
        capture_output=True,
        text=True,
    )

    marker_paths: list[Path] = []
    execution_ids = [
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    ]
    for worktree, execution_id in zip(
        (worktree_a, worktree_b), execution_ids, strict=True
    ):
        expected = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "agent-mail/execution-id",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        marker_result = _bash(
            f"""
            source {shlex.quote(_git_bash_path(HOOK_COMMON))}
            marker="$(am_execution_marker_path {shlex.quote(_git_bash_path(worktree))})"
            printf '%s\n' "$marker"
            am_execution_marker_write {shlex.quote(_git_bash_path(worktree))} \
              {shlex.quote(execution_id)} subagent '[]'
            """
        )
        assert marker_result.returncode == 0, marker_result.stderr
        actual = Path(marker_result.stdout.strip())
        assert actual.is_absolute()
        assert actual == Path(expected)
        marker_paths.append(actual)

    assert marker_paths[0] != marker_paths[1]
    assert json.loads(marker_paths[0].read_text())["execution_id"] == execution_ids[0]
    assert json.loads(marker_paths[1].read_text())["execution_id"] == execution_ids[1]


def test_execution_marker_does_not_downgrade_confirmed_terminal_status(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    execution_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    result = _bash(
        f"""
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        am_execution_marker_write {shlex.quote(_git_bash_path(repo))} \
          {shlex.quote(execution_id)} session '[]'
        am_execution_marker_end {shlex.quote(_git_bash_path(repo))} \
          {shlex.quote(execution_id)} completed
        am_execution_marker_end {shlex.quote(_git_bash_path(repo))} \
          {shlex.quote(execution_id)} unverified
        marker="$(am_execution_marker_path {shlex.quote(_git_bash_path(repo))})"
        jq -r '.status' "$marker"
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "completed"


def test_claude_native_subagent_payload_owns_auto_claim_and_ends_child(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    target = repo / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Hook Test",
            "-c",
            "user.email=hook@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
if [[ " $* " == *"/mail/api/file-reservations"* ]]; then
  cat >/dev/null
  printf '%s\n200' '{"active":0,"reservations":[]}'
  exit 0
fi
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
arguments="$(printf '%s' "$body" | jq -c '.params.arguments // {}')"
jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
  '{tool:$tool,arguments:$arguments}' >> "$FAKE_CALLS_LOG"
case "$tool" in
  ensure_project) result='{"human_key":"/owner/repo"}' ;;
  register_agent)
    name="$(printf '%s' "$arguments" | jq -r '.name')"
    result="$(jq -nc --arg name "$name" \
      '{name:$name,registration_token:"registration-token",retired_at:null}')"
    ;;
  start_agent_execution)
    if [[ $(printf '%s' "$arguments" | jq -r '.kind') == subagent ]]; then
      result='{"id":"44444444-4444-4444-8444-444444444444","status":"active","ancestor_execution_ids":["33333333-3333-4333-8333-333333333333"],"reused":false}'
    else
      result='{"id":"33333333-3333-4333-8333-333333333333","status":"active","ancestor_execution_ids":[],"reused":false}'
    fi
    ;;
  file_reservation_paths)
    result='{"granted":[{"path_pattern":"module.py"}],"conflicts":[],"warnings":[]}'
    ;;
  fetch_inbox)
    if [[ $(printf '%s' "$arguments" | jq -r '.include_bodies') == true ]]; then
      result='[{"id":901,"from":"HumanOverseer","subject":"Shared unread","body_md":"Read this in both contexts","importance":"high","ack_required":true}]'
    else
      result='[{"id":901,"from":"HumanOverseer","subject":"Shared unread","importance":"high","ack_required":true}]'
    fi
    ;;
  end_agent_execution)
    result='{"execution":{"status":"completed"},"already_ended":false,"released_reservations":1}'
    ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    _, claude_agent, _ = _hook_names(env)
    _put_credential(state, claude_agent)
    calls_log = tmp_path / "claude-calls.jsonl"
    env["FAKE_CALLS_LOG"] = _git_bash_path(calls_log)
    root_payload = {
        "session_id": "claude-session-native",
        "transcript_path": str(repo / "session.jsonl"),
        "cwd": str(repo),
        "permission_mode": "default",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }

    root_start = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_start.sh")],
        cwd=repo,
        env=env,
        input=json.dumps(root_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert root_start.returncode == 0, root_start.stderr
    assert "Root execution: 33333333-3333-4333-8333-333333333333" in json.loads(
        root_start.stdout
    )["hookSpecificOutput"]["additionalContext"]

    child_payload = {
        "session_id": "claude-session-native",
        "transcript_path": str(repo / "session.jsonl"),
        "cwd": str(repo),
        "permission_mode": "default",
        "hook_event_name": "SubagentStart",
        "agent_id": "claude-agent-native-child",
        "agent_type": "Explore",
    }
    child_start = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_start.sh")],
        cwd=repo,
        env=env,
        input=json.dumps(child_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert child_start.returncode == 0, child_start.stderr
    child_output = json.loads(child_start.stdout)
    assert child_output["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    assert "subagent execution 44444444-4444-4444-8444-444444444444" in (
        child_output["hookSpecificOutput"]["additionalContext"]
    )
    child_compatible = subprocess.run(
        [
            BASH,
            "-c",
            f"source {shlex.quote(_git_bash_path(HOOK_COMMON))}; "
            "am_read_payload; "
            f"am_execution_compatible_ids_for_payload /owner/repo {shlex.quote(claude_agent)} claude",
        ],
        cwd=repo,
        env=env,
        input=json.dumps(child_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert child_compatible.returncode == 0, child_compatible.stderr
    assert set(json.loads(child_compatible.stdout)) == {
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
    }

    child_mail_payload = {
        **child_payload,
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_use_id": "child-read",
        "tool_input": {"file_path": str(target)},
        "tool_response": {"success": True},
    }
    root_mail_payload = {
        **root_payload,
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_use_id": "root-read",
        "tool_input": {"file_path": str(target)},
        "tool_response": {"success": True},
    }
    for mail_payload in (child_mail_payload, root_mail_payload):
        mail = subprocess.run(
            [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "inbox_check.sh")],
            cwd=repo,
            env=env,
            input=json.dumps(mail_payload),
            check=False,
            capture_output=True,
            text=True,
        )
        assert mail.returncode == 0, mail.stderr
        mail_context = json.loads(mail.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        assert "Shared unread" in mail_context
        assert "Read this in both contexts" in mail_context
    assert len(list((state / "inbox").glob("*.seen"))) == 2

    edit_payload = {
        **child_payload,
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_use_id": "tool-claude-edit",
        "tool_input": {"file_path": str(target)},
        "tool_response": {"success": True},
    }
    autoreserve = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "autoreserve.sh")],
        cwd=repo,
        env=env,
        input=json.dumps(edit_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert autoreserve.returncode == 0, autoreserve.stderr
    assert autoreserve.stdout == ""

    states = {
        item["kind"]: (path, item)
        for path in (state / "executions").glob("*.json")
        if (item := json.loads(path.read_text(encoding="utf-8")))
    }
    root_state_path, root_state = states["session"]
    child_state_path, child_state = states["subagent"]
    root_token = Path(f"{root_state_path}.token").read_text().strip()
    child_token = Path(f"{child_state_path}.token").read_text().strip()
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    starts = [
        item["arguments"]
        for item in calls
        if item["tool"] == "start_agent_execution"
    ]
    assert [item["external_id"] for item in starts] == [
        "claude-session-native",
        "claude-agent-native-child",
    ]
    assert starts[1]["agent_type"] == "Explore"
    assert starts[1]["parent_execution_id"] == root_state["execution_id"]
    assert starts[1]["parent_execution_token"] == root_token
    claim = next(
        item["arguments"]
        for item in calls
        if item["tool"] == "file_reservation_paths"
    )
    assert claim["execution_id"] == child_state["execution_id"]
    assert claim["execution_token"] == child_token
    assert claim["origin"] == "auto"
    assert claim["lifecycle_protocol_version"] == 1

    child_stop_payload = {
        **child_payload,
        "hook_event_name": "SubagentStop",
        "stop_hook_active": False,
        "agent_transcript_path": str(repo / "child.jsonl"),
        "last_assistant_message": "done",
    }
    child_stop = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_end.sh")],
        cwd=repo,
        env=env,
        input=json.dumps(child_stop_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert child_stop.returncode == 0, child_stop.stderr
    assert json.loads(child_stop.stdout) == {}
    calls_after_stop = [
        json.loads(line) for line in calls_log.read_text().splitlines()
    ]
    assert not any(item["tool"] == "end_agent_execution" for item in calls_after_stop)

    # SubagentStop may be blocked by another hook. Only a later parent event
    # proves that control actually returned and finalizes the provisional stop.
    parent_reconcile = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "inbox_check.sh")],
        cwd=repo,
        env=env,
        input=json.dumps({**root_mail_payload, "tool_use_id": "root-after-child"}),
        check=False,
        capture_output=True,
        text=True,
    )
    assert parent_reconcile.returncode == 0, parent_reconcile.stderr
    root_stop = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_end.sh")],
        cwd=repo,
        env=env,
        input=json.dumps(
            {**root_payload, "hook_event_name": "SessionEnd", "reason": "other"}
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert root_stop.returncode == 0, root_stop.stderr
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    assert sum(item["tool"] == "register_agent" for item in calls) == 1
    end_calls = [
        item["arguments"] for item in calls if item["tool"] == "end_agent_execution"
    ]
    assert [(item["execution_id"], item["execution_token"]) for item in end_calls] == [
        (child_state["execution_id"], child_token),
        (root_state["execution_id"], root_token),
    ]
    combined_output = root_start.stdout + child_start.stdout + child_stop.stdout
    assert root_token not in combined_output
    assert child_token not in combined_output


def test_reservation_warning_distinguishes_legacy_sibling_and_orphaned_claims(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    target = repo / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
cat >/dev/null
printf '%s\n200' "$FAKE_RESERVATIONS_BODY"
""",
    )
    env = _hook_env(home, state, fake_bin)
    _, claude_agent, _ = _hook_names(env)
    _put_credential(state, claude_agent)
    payload = {
        "session_id": "warning-session",
        "transcript_path": str(repo / "session.jsonl"),
        "cwd": str(repo),
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_use_id": "warning-edit",
        "tool_input": {"file_path": str(target)},
    }
    expires = "2026-08-13T23:59:59Z"
    cases = [
        (
            {
                "agent": claude_agent,
                "execution_id": None,
                "execution_status": None,
                "legacy_unscoped": True,
                "orphaned": False,
            },
            ["legacy claim sprzed migracji", "not a sibling execution conflict"],
            False,
        ),
        (
            {
                "agent": claude_agent,
                "execution_id": "66666666-6666-4666-8666-666666666666",
                "execution_status": "active",
                "legacy_unscoped": False,
                "orphaned": False,
            },
            ["sibling execution 66666666-6666-4666-8666-666666666666"],
            True,
        ),
        (
            {
                "agent": "OtherAgent",
                "execution_id": None,
                "execution_status": None,
                "legacy_unscoped": True,
                "orphaned": False,
            },
            ["legacy claim sprzed migracji", "reserved by OtherAgent"],
            True,
        ),
        (
            {
                "agent": "OtherAgent",
                "execution_id": "77777777-7777-4777-8777-777777777777",
                "execution_status": "expired",
                "legacy_unscoped": False,
                "orphaned": True,
            },
            ["orphaned claim", "inactive execution expired"],
            True,
        ),
        (
            {
                "agent": "<orphaned>",
                "execution_id": "88888888-8888-4888-8888-888888888888",
                "execution_status": "active",
                "legacy_unscoped": False,
                "orphaned": True,
            },
            ["orphaned claim"],
            True,
        ),
    ]
    for claim, expected_fragments, coordinates in cases:
        env["FAKE_RESERVATIONS_BODY"] = json.dumps(
            {
                "active": 1,
                "reservations": [
                    {
                        **claim,
                        "path_pattern": "module.py",
                        "reason": "architecture",
                        "expires_ts": expires,
                    }
                ],
            }
        )
        warning = subprocess.run(
            [
                BASH,
                _git_bash_path(ROOT / "scripts" / "hooks" / "reservations_warn.sh"),
            ],
            cwd=repo,
            env=env,
            input=json.dumps(payload),
            check=False,
            capture_output=True,
            text=True,
        )
        assert warning.returncode == 0, warning.stderr
        context = json.loads(warning.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        assert f"expires {expires}" in context
        for fragment in expected_fragments:
            assert fragment in context
        assert ("Coordinate before editing" in context) is coordinates


@pytest.mark.parametrize("second_start", ["terminal", "error"])
def test_active_local_execution_requires_server_revalidation(
    tmp_path: Path,
    second_start: str,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
arguments="$(printf '%s' "$body" | jq -c '.params.arguments // {}')"
jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
  '{tool:$tool,arguments:$arguments}' >> "$FAKE_CALLS_LOG"
case "$tool" in
  ensure_project) result='{"human_key":"/owner/repo"}' ;;
  register_agent)
    name="$(printf '%s' "$arguments" | jq -r '.name')"
    result="$(jq -nc --arg name "$name" \
      '{name:$name,registration_token:"registration-token",retired_at:null}')"
    ;;
  start_agent_execution)
    if [[ ! -f $FAKE_START_SEEN ]]; then
      : > "$FAKE_START_SEEN"
      result='{"id":"55555555-5555-4555-8555-555555555555","status":"active","ancestor_execution_ids":[],"reused":false}'
    elif [[ $FAKE_SECOND_START == error ]]; then
      exit 9
    else
      result='{"id":"55555555-5555-4555-8555-555555555555","status":"completed","ancestor_execution_ids":[],"reused":true}'
    fi
    ;;
  fetch_inbox) result='[]' ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    _, _, agent = _hook_names(env)
    _put_credential(state, agent)
    calls_log = tmp_path / "revalidation-calls.jsonl"
    env.update(
        {
            "FAKE_CALLS_LOG": _git_bash_path(calls_log),
            "FAKE_START_SEEN": _git_bash_path(tmp_path / "start-seen"),
            "FAKE_SECOND_START": second_start,
        }
    )
    payload = {
        "cwd": str(repo),
        "session_id": "codex-revalidation-session",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
        "permission_mode": "default",
    }
    command = [
        BASH,
        _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
        "session-start",
    ]

    first = subprocess.run(
        command,
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    assert "Root execution: 55555555-5555-4555-8555-555555555555" in first.stdout
    state_path = next((state / "executions").glob("*.json"))
    token = Path(f"{state_path}.token").read_text().strip()
    marker_rel = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "agent-mail/execution-id",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    marker_path = Path(marker_rel)
    if not marker_path.is_absolute():
        marker_path = repo / marker_path
    assert json.loads(marker_path.read_text())["status"] == "active"

    second = subprocess.run(
        command,
        cwd=repo,
        env=env,
        input=json.dumps({**payload, "source": "compact"}),
        check=False,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr
    assert "root execution could not be started" in second.stdout
    assert json.loads(marker_path.read_text())["status"] == "unverified"
    local_state = json.loads(state_path.read_text())
    assert local_state["status"] == "active"
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    starts = [
        item["arguments"]
        for item in calls
        if item["tool"] == "start_agent_execution"
    ]
    assert len(starts) == 2
    assert {item["external_id"] for item in starts} == {
        "codex-revalidation-session"
    }
    assert {item["execution_token"] for item in starts} == {token}


def test_cross_client_session_end_cannot_release_other_execution(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "shared-state"
    fake_bin = tmp_path / "bin"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".agent-mail-project-id").write_text("project-id\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
if [[ " $* " == *"/mail/api/file-reservations"* ]]; then
  cat >/dev/null
  printf '%s\n200' '{"active":0,"reservations":[]}'
  exit 0
fi
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
arguments="$(printf '%s' "$body" | jq -c '.params.arguments // {}')"
jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
  '{tool:$tool,arguments:$arguments}' >> "$FAKE_CALLS_LOG"
case "$tool" in
  ensure_project) result='{"human_key":"/owner/repo"}' ;;
  register_agent)
    name="$(printf '%s' "$arguments" | jq -r '.name')"
    result="$(jq -nc --arg name "$name" \
      '{name:$name,registration_token:"registration-token",retired_at:null}')"
    ;;
  start_agent_execution)
    if [[ $(printf '%s' "$arguments" | jq -r '.client_name') == claude ]]; then
      result='{"id":"99999999-9999-4999-8999-999999999999","status":"active","ancestor_execution_ids":[],"reused":false}'
    else
      result='{"id":"aaaaaaaa-0000-4000-8000-000000000000","status":"active","ancestor_execution_ids":[],"reused":false}'
    fi
    ;;
  end_agent_execution)
    result='{"execution":{"status":"completed"},"already_ended":false,"released_reservations":1}'
    ;;
  fetch_inbox) result='[]' ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    _, claude_agent, codex_agent = _hook_names(env)
    (state / "credentials.json").write_text(
        json.dumps(
            {
                "/owner/repo": {
                    claude_agent: "registration-token",
                    codex_agent: "registration-token",
                }
            }
        ),
        encoding="utf-8",
    )
    calls_log = tmp_path / "cross-client-calls.jsonl"
    env["FAKE_CALLS_LOG"] = _git_bash_path(calls_log)
    session_id = "shared-native-provider-session"
    claude_payload = {
        "session_id": session_id,
        "transcript_path": str(repo / "claude.jsonl"),
        "cwd": str(repo),
        "permission_mode": "default",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    codex_payload = {
        "session_id": session_id,
        "transcript_path": str(repo / "codex.jsonl"),
        "cwd": str(repo),
        "permission_mode": "default",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
    }
    claude_start = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_start.sh")],
        cwd=repo,
        env=env,
        input=json.dumps(claude_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    codex_start = subprocess.run(
        [
            BASH,
            _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
            "session-start",
        ],
        cwd=repo,
        env=env,
        input=json.dumps(codex_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert claude_start.returncode == 0, claude_start.stderr
    assert codex_start.returncode == 0, codex_start.stderr

    states = {
        item["client"]: (path, item)
        for path in (state / "executions").glob("*.json")
        if (item := json.loads(path.read_text()))
    }
    claude_state_path, claude_state = states["claude"]
    codex_state_path, codex_state = states["codex"]
    claude_token = Path(f"{claude_state_path}.token").read_text().strip()
    codex_token = Path(f"{codex_state_path}.token").read_text().strip()
    assert claude_token != codex_token

    claude_end = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_end.sh")],
        cwd=repo,
        env=env,
        input=json.dumps(
            {**claude_payload, "hook_event_name": "SessionEnd", "reason": "other"}
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert claude_end.returncode == 0, claude_end.stderr
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    end_calls = [
        item["arguments"] for item in calls if item["tool"] == "end_agent_execution"
    ]
    assert end_calls == [
        {
            "project_key": "/owner/repo",
            "agent_name": claude_agent,
            "registration_token": "registration-token",
            "execution_id": claude_state["execution_id"],
            "execution_token": claude_token,
            "status": "completed",
            "lifecycle_protocol_version": 1,
        }
    ]
    assert codex_state["execution_id"] not in json.dumps(end_calls)
    assert codex_token not in json.dumps(end_calls)
    assert not any(item["tool"] == "release_file_reservations" for item in calls)
    assert json.loads(codex_state_path.read_text())["status"] == "active"
    assert not (state / "sessions").exists()


def test_codex_apply_patch_uses_reported_cwd_for_cross_project_execution(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    root_repo = tmp_path / "root-repo"
    edited_repo = tmp_path / "edited-repo"
    _init_git_repo(root_repo)
    _init_git_repo(edited_repo)
    subprocess.run(
        [
            "git",
            "-C",
            str(edited_repo),
            "remote",
            "set-url",
            "origin",
            "git@github.com:owner/other.git",
        ],
        check=True,
    )
    for repo in (root_repo, edited_repo):
        (repo / ".agent-mail-project-id").write_text(
            "project-id\n", encoding="utf-8"
        )
    target = edited_repo / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
arguments="$(printf '%s' "$body" | jq -c '.params.arguments // {}')"
jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
  '{tool:$tool,arguments:$arguments}' >> "$FAKE_CALLS_LOG"
project="$(printf '%s' "$arguments" | jq -r '.project_key // .human_key // empty')"
case "$tool" in
  ensure_project) result="$(jq -nc --arg key "$project" '{human_key:$key}')" ;;
  register_agent)
    name="$(printf '%s' "$arguments" | jq -r '.name')"
    if [[ $project == /owner/other ]]; then reg_token=other-registration-token; else reg_token=root-registration-token; fi
    result="$(jq -nc --arg name "$name" --arg token "$reg_token" \
      '{name:$name,registration_token:$token,retired_at:null}')"
    ;;
  start_agent_execution)
    if [[ $project == /owner/other ]]; then execution_id=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb; else execution_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa; fi
    result="$(jq -nc --arg id "$execution_id" '{id:$id,status:"active",ancestor_execution_ids:[],reused:false}')"
    ;;
  heartbeat_agent_execution)
    result="$(jq -nc --arg id "$(printf '%s' "$arguments" | jq -r '.execution_id')" '{id:$id,status:"active"}')"
    ;;
  end_agent_execution)
    sleep "${FAKE_END_DELAY:-0}"
    result='{"execution":{"status":"completed"},"already_ended":false,"released_reservations":0}'
    ;;
  fetch_inbox) result='[]' ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    _, _, agent = _hook_names(env)
    _put_credential(state, agent, "root-registration-token")
    calls_log = tmp_path / "codex-cross-project-calls.jsonl"
    env.update(
        {
            "FAKE_CALLS_LOG": _git_bash_path(calls_log),
            "FAKE_END_DELAY": "1.6",
            "AGENT_MAIL_EXECUTION_HEARTBEAT_INTERVAL": "0",
        }
    )
    root_payload = {
        "session_id": "codex-cross-project-session",
        "turn_id": "turn-start",
        "cwd": str(root_repo),
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
        "permission_mode": "default",
    }
    command = [
        BASH,
        _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
    ]
    start = subprocess.run(
        [*command, "session-start"],
        cwd=root_repo,
        env=env,
        input=json.dumps(root_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert start.returncode == 0, start.stderr

    # Wire-realistic Codex PostToolUse: apply_patch guarantees only an opaque
    # command. The common cwd is the session working directory, so it can enroll
    # this repository only when Codex actually reports that repository here;
    # command text itself is never parsed for paths.
    post_tool_payload = {
        "session_id": "codex-cross-project-session",
        "turn_id": "turn-tool",
        "cwd": str(edited_repo),
        "hook_event_name": "PostToolUse",
        "tool_name": "apply_patch",
        "tool_use_id": "tool-cross-project-apply-patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Update File: module.py\n"
                "@@\n-VALUE = 1\n+VALUE = 2\n"
                "*** End Patch"
            )
        },
        "tool_response": {"success": True},
        "model": "gpt-5.6",
        "permission_mode": "default",
    }
    heartbeat = subprocess.run(
        [*command, "heartbeat"],
        cwd=root_repo,
        env=env,
        input=json.dumps(post_tool_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert heartbeat.returncode == 0, heartbeat.stderr
    warning_output = json.loads(heartbeat.stdout)
    assert warning_output["hookSpecificOutput"] == {
        "hookEventName": "PostToolUse",
        "additionalContext": warning_output["systemMessage"],
    }
    assert "command payload is intentionally not parsed" in warning_output[
        "systemMessage"
    ]

    # This bound exists to catch a session-end that BLOCKS, not to police how
    # fast it is.  Three seconds never had the headroom for that: the step ends
    # executions in two projects and measured 2.295/2.297/2.325 s on an idle
    # M-series Mac, i.e. 23% of margin before a hosted runner adds any load at
    # all — so macos-latest failed it on 5828d1d while ubuntu passed.  The cause
    # is the budget, not a regression: the same test measured 8.12/8.00/8.13 s
    # end to end at cb14179 against 8.23/8.23/8.25 s with the pre-287decd hook
    # library restored, so the worktree-guard rewrite is marginally faster, not
    # slower.  Scale the deadline like the watcher case above does, keeping it
    # bounded so a genuine hang still fails.
    end_deadline = 30 if os.name == "nt" else 10
    end_started = time.monotonic()
    end = subprocess.run(
        [*command, "session-end"],
        cwd=root_repo,
        env=env,
        input=json.dumps({**root_payload, "hook_event_name": "SessionEnd"}),
        check=False,
        capture_output=True,
        text=True,
        timeout=end_deadline,
    )
    end_elapsed = time.monotonic() - end_started
    assert end.returncode == 0, end.stderr
    assert end_elapsed < end_deadline
    calls = [
        json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()
    ]
    starts = [
        item["arguments"]
        for item in calls
        if item["tool"] == "start_agent_execution"
    ]
    assert {(item["project_key"], item["external_id"]) for item in starts} == {
        ("/owner/repo", "codex-cross-project-session"),
        ("/owner/other", "codex-cross-project-session"),
    }
    target_start = next(
        item for item in starts if item["project_key"] == "/owner/other"
    )
    assert target_start["client_name"] == "codex"
    # Compare places, not spellings: the hook now derives these from
    # `git rev-parse` run inside the directory, which reports C:/... on
    # Windows, while str(WindowsPath) spells C:\... — same directory, two
    # renderings.  Third occurrence of this assertion shape; the other two
    # were converted in the execution-hooks test.
    assert Path(target_start["cwd"]) == edited_repo
    assert Path(target_start["repo_root"]) == edited_repo
    assert Path(target_start["worktree_path"]) == edited_repo
    heartbeats = [
        item["arguments"]
        for item in calls
        if item["tool"] == "heartbeat_agent_execution"
    ]
    assert {(item["project_key"], item["execution_id"]) for item in heartbeats} == {
        ("/owner/repo", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("/owner/other", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    }
    ends = [
        item["arguments"] for item in calls if item["tool"] == "end_agent_execution"
    ]
    assert {(item["project_key"], item["execution_id"]) for item in ends} == {
        ("/owner/repo", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("/owner/other", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    }
    assert not any(item["tool"] == "file_reservation_paths" for item in calls)


def test_codex_session_end_tombstone_blocks_raced_cross_project_enrollment(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    root_repo = tmp_path / "root-repo"
    edited_repo = tmp_path / "edited-repo"
    non_git_cwd = tmp_path / "ended-session-cwd"
    _init_git_repo(root_repo)
    _init_git_repo(edited_repo)
    non_git_cwd.mkdir()
    subprocess.run(
        [
            "git",
            "-C",
            str(edited_repo),
            "remote",
            "set-url",
            "origin",
            "git@github.com:owner/other.git",
        ],
        check=True,
    )
    for repo in (root_repo, edited_repo):
        (repo / ".agent-mail-project-id").write_text(
            "project-id\n", encoding="utf-8"
        )
    target = edited_repo / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
arguments="$(printf '%s' "$body" | jq -c '.params.arguments // {}')"
jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
  '{tool:$tool,arguments:$arguments}' >> "$FAKE_CALLS_LOG"
project="$(printf '%s' "$arguments" | jq -r '.project_key // .human_key // empty')"
case "$tool" in
  ensure_project) result="$(jq -nc --arg key "$project" '{human_key:$key}')" ;;
  register_agent)
    name="$(printf '%s' "$arguments" | jq -r '.name')"
    if [[ $project == /owner/other ]]; then
      : > "$FAKE_REGISTER_ENTERED"
      while [[ ! -e $FAKE_REGISTER_RELEASE ]]; do sleep 0.02; done
      reg_token=other-registration-token
    else
      reg_token=root-registration-token
    fi
    result="$(jq -nc --arg name "$name" --arg token "$reg_token" \
      '{name:$name,registration_token:$token,retired_at:null}')"
    ;;
  start_agent_execution)
    result='{"id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","status":"active","ancestor_execution_ids":[],"reused":false}'
    ;;
  heartbeat_agent_execution)
    result="$(jq -nc --arg id "$(printf '%s' "$arguments" | jq -r '.execution_id')" '{id:$id,status:"active"}')"
    ;;
  end_agent_execution)
    result='{"execution":{"status":"completed"},"already_ended":false,"released_reservations":0}'
    ;;
  fetch_inbox) result='[]' ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    _, _, agent = _hook_names(env)
    _put_credential(state, agent, "root-registration-token")
    calls_log = tmp_path / "codex-raced-enrollment-calls.jsonl"
    register_entered = tmp_path / "target-register-entered"
    register_release = tmp_path / "target-register-release"
    env.update(
        {
            "FAKE_CALLS_LOG": _git_bash_path(calls_log),
            "FAKE_REGISTER_ENTERED": _git_bash_path(register_entered),
            "FAKE_REGISTER_RELEASE": _git_bash_path(register_release),
            "AGENT_MAIL_EXECUTION_HEARTBEAT_INTERVAL": "0",
        }
    )
    session_id = "codex-raced-cross-project-session"
    command = [
        BASH,
        _git_bash_path(ROOT / "scripts" / "hooks" / "codex_notify.sh"),
    ]
    root_payload = {
        "session_id": session_id,
        "turn_id": "turn-start",
        "cwd": str(root_repo),
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
        "permission_mode": "default",
    }
    start = subprocess.run(
        [*command, "session-start"],
        cwd=root_repo,
        env=env,
        input=json.dumps(root_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert start.returncode == 0, start.stderr

    post_tool_payload = {
        "session_id": session_id,
        "turn_id": "turn-tool",
        "cwd": str(root_repo),
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__filesystem__write_file",
        "tool_use_id": "tool-raced-cross-project-file",
        "tool_input": {"file_path": str(target), "content": "VALUE = 2\n"},
        "tool_response": {"success": True},
        "model": "gpt-5.6",
        "permission_mode": "default",
    }
    enrollment = subprocess.Popen(
        [*command, "heartbeat"],
        cwd=root_repo,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert enrollment.stdin is not None
    enrollment.stdin.write(json.dumps(post_tool_payload))
    enrollment.stdin.close()
    enrollment.stdin = None
    # Same spawn-cost headroom as the racing test above: cross-project
    # enrollment sits behind register + ensure + execution bookkeeping, each a
    # separate bash+jq process, and on the native Windows host the healthy
    # path needs more than the 5 s this wait used to allow.  The loop returns
    # the instant the marker appears.
    deadline = time.monotonic() + (60 if os.name == "nt" else 20)
    while not register_entered.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert register_entered.exists(), "target enrollment never reached register_agent"

    # The payload cwd is intentionally a live non-Git directory: lifecycle
    # cleanup must be driven by private client/session state, not repository
    # discovery or the current opt-in marker.
    end = subprocess.run(
        [*command, "session-end"],
        cwd=non_git_cwd,
        env=env,
        input=json.dumps(
            {
                "session_id": session_id,
                "cwd": str(non_git_cwd),
                "hook_event_name": "SessionEnd",
                "reason": "other",
            }
        ),
        check=False,
        capture_output=True,
        text=True,
        # A kill this high catches a genuinely hung hook while letting a slow
        # machine finish: measured session-end on the native Windows host is
        # 6.8 s of pure spawn cost with the hook healthy, so the old 3 s kill
        # reported TimeoutExpired — an error, not a verdict — on speed alone.
        timeout=60 if os.name == "nt" else 20,
    )
    assert end.returncode == 0, end.stderr
    tombstones = list((state / "session-end-intents").glob("*.json"))
    assert len(tombstones) == 1
    tombstone = json.loads(tombstones[0].read_text(encoding="utf-8"))
    assert (tombstone["client"], tombstone["session_id"], tombstone["status"]) == (
        "codex",
        session_id,
        "ended",
    )
    _assert_hook_private_mode(tombstones[0])

    register_release.touch()
    stdout, stderr = enrollment.communicate(timeout=5)
    assert enrollment.returncode == 0, stderr
    assert stdout == ""
    late = subprocess.run(
        [*command, "heartbeat"],
        cwd=root_repo,
        env=env,
        input=json.dumps(post_tool_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert late.returncode == 0, late.stderr
    assert late.stdout == ""
    calls = [
        json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()
    ]
    target_starts = [
        item
        for item in calls
        if item["tool"] == "start_agent_execution"
        and item["arguments"]["project_key"] == "/owner/other"
    ]
    assert target_starts == []
    assert sum(
        item["tool"] == "register_agent"
        and item["arguments"].get("project_key") == "/owner/other"
        for item in calls
    ) == 1
    assert not any(
        item["tool"] in {"heartbeat_agent_execution", "file_reservation_paths"}
        and item["arguments"].get("project_key") == "/owner/other"
        for item in calls
    )
    root_ends = [
        item["arguments"] for item in calls if item["tool"] == "end_agent_execution"
    ]
    assert {(item["project_key"], item["execution_id"]) for item in root_ends} == {
        ("/owner/repo", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    }
    target_states = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (state / "executions").glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["project"] == "/owner/other"
    ]
    assert target_states == []


def test_claude_cross_project_edit_starts_and_ends_each_exact_execution(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    root_repo = tmp_path / "root-repo"
    edited_repo = tmp_path / "edited-repo"
    _init_git_repo(root_repo)
    _init_git_repo(edited_repo)
    subprocess.run(
        [
            "git",
            "-C",
            str(edited_repo),
            "remote",
            "set-url",
            "origin",
            "git@github.com:owner/other.git",
        ],
        check=True,
    )
    for repo in (root_repo, edited_repo):
        (repo / ".agent-mail-project-id").write_text(
            "project-id\n", encoding="utf-8"
        )
    target = edited_repo / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    _install_fake_curl(
        fake_bin,
        """#!/usr/bin/env bash
body="$(cat)"
tool="$(printf '%s' "$body" | jq -r '.params.name // empty')"
arguments="$(printf '%s' "$body" | jq -c '.params.arguments // {}')"
jq -nc --arg tool "$tool" --argjson arguments "$arguments" \
  '{tool:$tool,arguments:$arguments}' >> "$FAKE_CALLS_LOG"
project="$(printf '%s' "$arguments" | jq -r '.project_key // empty')"
case "$tool" in
  ensure_project) result="$(jq -nc --arg key "$(printf '%s' "$arguments" | jq -r '.human_key')" '{human_key:$key}')" ;;
  register_agent)
    name="$(printf '%s' "$arguments" | jq -r '.name')"
    if [[ $project == /owner/other ]]; then reg_token=other-registration-token; else reg_token=root-registration-token; fi
    result="$(jq -nc --arg name "$name" --arg token "$reg_token" \
      '{name:$name,registration_token:$token,retired_at:null}')"
    ;;
  start_agent_execution)
    if [[ $project == /owner/other ]]; then execution_id=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb; else execution_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa; fi
    result="$(jq -nc --arg id "$execution_id" '{id:$id,status:"active",ancestor_execution_ids:[],reused:false}')"
    ;;
  file_reservation_paths) result='{"granted":[{"path_pattern":"module.py"}],"conflicts":[],"warnings":[]}' ;;
  end_agent_execution) result='{"execution":{"status":"completed"},"already_ended":false,"released_reservations":1}' ;;
  fetch_inbox) result='[]' ;;
  *) result='{}' ;;
esac
envelope="$(jq -nc --arg text "$result" \
  '{result:{content:[{type:"text",text:$text}],isError:false}}')"
printf '%s\n200' "$envelope"
""",
    )
    env = _hook_env(home, state, fake_bin)
    _, agent, _ = _hook_names(env)
    _put_credential(state, agent, "root-registration-token")
    calls_log = tmp_path / "cross-project-calls.jsonl"
    env["FAKE_CALLS_LOG"] = _git_bash_path(calls_log)
    payload = {
        "session_id": "cross-project-session",
        "transcript_path": str(root_repo / "session.jsonl"),
        "cwd": str(root_repo),
        "permission_mode": "default",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    start = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_start.sh")],
        cwd=root_repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert start.returncode == 0, start.stderr

    edit = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "autoreserve.sh")],
        cwd=root_repo,
        env=env,
        input=json.dumps(
            {
                **payload,
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "tool_use_id": "cross-project-edit",
                "tool_input": {"file_path": str(target)},
                "tool_response": {"success": True},
            }
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert edit.returncode == 0, edit.stderr
    assert edit.stdout == ""

    end = subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / "session_end.sh")],
        cwd=root_repo,
        env=env,
        input=json.dumps({**payload, "hook_event_name": "SessionEnd"}),
        check=False,
        capture_output=True,
        text=True,
    )
    assert end.returncode == 0, end.stderr
    calls = [json.loads(line) for line in calls_log.read_text().splitlines()]
    starts = [
        item["arguments"]
        for item in calls
        if item["tool"] == "start_agent_execution"
    ]
    assert {(item["project_key"], item["external_id"]) for item in starts} == {
        ("/owner/repo", "cross-project-session"),
        ("/owner/other", "cross-project-session"),
    }
    claim = next(
        item["arguments"]
        for item in calls
        if item["tool"] == "file_reservation_paths"
    )
    assert claim["project_key"] == "/owner/other"
    assert claim["execution_id"] == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    ends = [
        item["arguments"] for item in calls if item["tool"] == "end_agent_execution"
    ]
    assert {(item["project_key"], item["execution_id"]) for item in ends} == {
        ("/owner/repo", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("/owner/other", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    }


def test_hook_refuses_secret_state_and_recovers_crash_locks_without_aba(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    unsafe_state = repo / ".private" / "agent-mail"
    refused = _bash(
        f"""
        export AGENT_MAIL_STATE_DIR={shlex.quote(_git_bash_path(unsafe_state))}
        export AGENT_MAIL_ENV_FILE=/dev/null
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        printf '%s|%s' "$AM_PATH_CONFIGURATION_VALID" "$AM_STATE_DIR"
        """
    )
    assert refused.returncode == 0, refused.stderr
    assert refused.stdout.startswith("0|/dev/null/agent-mail-state-dir-inside-git")

    safe_state = tmp_path / "state"
    empty_lock = safe_state / "recover.lock"
    empty_lock.mkdir(parents=True)
    recovered = _bash(
        f"""
        export AGENT_MAIL_STATE_DIR={shlex.quote(_git_bash_path(safe_state))}
        export AGENT_MAIL_ENV_FILE=/dev/null
        source {shlex.quote(_git_bash_path(HOOK_COMMON))}
        am_lock_acquire {shlex.quote(_git_bash_path(empty_lock))}
        test -s {shlex.quote(_git_bash_path(empty_lock / 'pid'))}
        am_lock_release {shlex.quote(_git_bash_path(empty_lock))}
        """
    )
    assert recovered.returncode == 0, recovered.stderr

    stale_lock = safe_state / "stale.lock"
    stale_lock.mkdir()
    dead_owner = subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", "printf '%s' $$"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (stale_lock / "pid").write_text(dead_owner, encoding="utf-8")
    recovery_lock = Path(f"{stale_lock}.recovery")
    recovery_holder = subprocess.Popen(
        [
            BASH,
            "--noprofile",
            "--norc",
            "-c",
            (
                f"source {shlex.quote(_git_bash_path(HOOK_COMMON))}; "
                f"mkdir {shlex.quote(_git_bash_path(recovery_lock))} || exit 1; "
                f"printf '%s' $$ > "
                f"{shlex.quote(_git_bash_path(recovery_lock / 'pid'))}; "
                "printf 'ready\\n'; IFS= read -r _; "
                f"am_lock_release {shlex.quote(_git_bash_path(recovery_lock))}"
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert recovery_holder.stdout is not None
    assert recovery_holder.stdout.readline().strip() == "ready"

    acquired_markers = [safe_state / "healer-one", safe_state / "healer-two"]
    contention_markers = [
        safe_state / "healer-one-contending",
        safe_state / "healer-two-contending",
    ]
    release_markers = [safe_state / "release-one", safe_state / "release-two"]
    healers: list[subprocess.Popen[str]] = []
    for marker, contention_marker, release_marker in zip(
        acquired_markers, contention_markers, release_markers, strict=True
    ):
        healer = subprocess.Popen(
            [
                BASH,
                "--noprofile",
                "--norc",
                "-c",
                (
                    # uutils coreutils 0.8.0 can report success after its
                    # concurrent mkdir syscall returned EEXIST. Reproduce that
                    # contract deliberately so pid publication, not a friendly
                    # mkdir exit code, must provide mutual exclusion.
                    "mkdir() { command mkdir \"$@\"; local rc=$? target; "
                    "target=\"${!#}\"; "
                    "if [[ $rc -ne 0 && -d $target ]]; then return 0; fi; "
                    "return \"$rc\"; }; "
                    "sleep() { "
                    f"printf 'wait\\n' >> {shlex.quote(_git_bash_path(contention_marker))}; "
                    "command sleep \"$@\"; }; "
                    f"source {shlex.quote(_git_bash_path(HOOK_COMMON))}; "
                    f"am_lock_acquire {shlex.quote(_git_bash_path(stale_lock))} "
                    "|| exit 1; "
                    f"printf '%s' $$ > {shlex.quote(_git_bash_path(marker))}; "
                    f"while [[ ! -f {shlex.quote(_git_bash_path(release_marker))} ]]; "
                    "do command sleep 0.01; done; "
                    f"am_lock_release {shlex.quote(_git_bash_path(stale_lock))}"
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        healers.append(healer)

    # The sleep shim is a deterministic barrier: each marker proves that its
    # contender failed to acquire the stale lock and reached the retry path
    # while the dedicated recovery mutex was held. Only then may repair begin.
    deadline = time.monotonic() + 5
    while (
        not all(path.exists() for path in contention_markers)
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert all(path.exists() for path in contention_markers)
    assert recovery_holder.stdin is not None
    recovery_holder.stdin.write("release\n")
    recovery_holder.stdin.flush()
    assert recovery_holder.wait(timeout=5) == 0
    deadline = time.monotonic() + 5
    while not any(path.exists() for path in acquired_markers) and time.monotonic() < deadline:
        time.sleep(0.01)
    acquired_indexes = [
        index for index, path in enumerate(acquired_markers) if path.exists()
    ]
    assert len(acquired_indexes) == 1
    first_index = acquired_indexes[0]
    second_index = 1 - first_index
    first_pid = acquired_markers[first_index].read_text(encoding="utf-8")
    assert (stale_lock / "pid").read_text(encoding="utf-8") == first_pid
    # Prove the loser retried after the live successor published its pid. The
    # test proceeds only after another observed wait (or a forbidden second
    # acquisition), so scheduler speed cannot turn this into a fixed-sleep race.
    second_contention = contention_markers[second_index]
    contention_size = second_contention.stat().st_size
    deadline = time.monotonic() + 5
    while (
        second_contention.stat().st_size == contention_size
        and not acquired_markers[second_index].exists()
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert not acquired_markers[second_index].exists()
    assert second_contention.stat().st_size > contention_size

    first_healer = healers[first_index]
    release_markers[first_index].write_text("release\n", encoding="utf-8")
    assert first_healer.wait(timeout=5) == 0
    deadline = time.monotonic() + 5
    while not acquired_markers[second_index].exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert acquired_markers[second_index].exists()
    second_pid = acquired_markers[second_index].read_text(encoding="utf-8")
    assert (stale_lock / "pid").read_text(encoding="utf-8") == second_pid
    second_healer = healers[second_index]
    release_markers[second_index].write_text("release\n", encoding="utf-8")
    assert second_healer.wait(timeout=5) == 0


def test_execution_manifest_enumeration_survives_crlf_from_jq(tmp_path: Path) -> None:
    """The manifest scan must not drop entries when jq writes CRLF.

    ``jq`` is the only producer in the hook library that feeds a ``read`` loop
    directly, and the Git Bash ``jq.exe`` opens stdout in text mode, so on
    native Windows every name arrives as ``<file>.json\r``. The CR survives
    ``read -r`` and defeats the ``*.json`` glob, which emptied this enumeration
    and silently turned all nine of its consumers — subagent stop, root
    heartbeat, root end — into no-ops. Nothing reported it because the whole
    path is deliberately failure-open.

    The shim is load-bearing: real ``jq`` emits LF on Linux and macOS, so
    without it this test passes with or without the fix on the two platforms
    that run it most, and would be pure decoration. ``SHIM_CRLF`` is asserted
    for the same reason — it fails the test if the shim ever stops reproducing
    the condition, rather than letting the assertion below go vacuous.
    """
    real_jq = shutil.which("jq")
    if real_jq is None:
        pytest.skip("jq is required to exercise the manifest enumeration")

    state_dir = tmp_path / "state"
    (state_dir / "executions").mkdir(parents=True)
    (state_dir / "execution-manifests").mkdir(parents=True)

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "jq"
    # Not `sed 's/$/\r/'`: MSYS sed strips the CR it is being asked to add,
    # so the shim silently degrades to a no-op on the very platform this test
    # exists for. Normalising to exactly one CR also keeps it deterministic
    # where the real jq already emits CRLF - a second CR would survive the
    # single-CR strip in the library and fail this test against correct code.
    shim.write_text(
        "#!/usr/bin/env bash\n"
        r'''"$REAL_JQ" "$@" | while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    printf '%s\r\n' "$line"
done
exit "${PIPESTATUS[0]}"
''',
        encoding="utf-8",
        # Windows would otherwise translate the shebang's LF to CRLF and
        # `env` would look for a program literally named "bash\r".
        newline="\n",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    script = r"""
set -uo pipefail
. scripts/hooks/agent_mail_common.sh || { echo "SOURCE_FAILED"; exit 1; }
sid=11111111-2222-3333-4444-555555555555
manifest="$(am_execution_manifest_file claude "$sid" 1)" || {
    echo "NO_MANIFEST_PATH"; exit 1; }
printf '{"kind":"subagent"}' > "$AM_STATE_DIR/executions/state-one.json"
printf '{"kind":"subagent"}' > "$AM_STATE_DIR/executions/state-two.json"
printf '%s' '{"version":1,"client":"claude","session_id":"'"$sid"'",
"lifecycle_generation":1,
"state_files":["state-one.json","state-two.json"]}' > "$manifest"
# Read the CR back through the exact mechanism under test. On MSYS, sed and
# grep silently normalise CRLF and command substitution strips the CR too, so
# all three report "no CR" on bytes that demonstrably contain one and would
# quietly disarm this control. `read -r` from a process substitution is what
# the library itself uses, and it is the one that preserves the byte.
probe=""
while IFS= read -r probe; do break; done \
    < <(jq -r '.state_files[]' "$manifest")
case "$probe" in
    *$'\r') echo "SHIM_CRLF=yes" ;;
    *) echo "SHIM_CRLF=no" ;;
esac
echo "COUNT=$(am_execution_manifest_state_files claude "$sid" 1 | grep -c .)"
"""

    result = _bash(
        script,
        env={
            "AGENT_MAIL_STATE_DIR": state_dir.as_posix(),
            "REAL_JQ": real_jq,
            "PATH": f"{shim_dir.as_posix()}{os.pathsep}{os.environ.get('PATH', '')}",
        },
    )

    assert "SOURCE_FAILED" not in result.stdout, result.stderr
    assert "SHIM_CRLF=yes" in result.stdout, (
        "the jq shim stopped emitting CRLF, so this test no longer reproduces "
        f"the Windows condition it exists for: {result.stdout!r} {result.stderr!r}"
    )
    assert "COUNT=2" in result.stdout, (
        f"manifest enumeration dropped CRLF-terminated names: "
        f"{result.stdout!r} {result.stderr!r}"
    )
