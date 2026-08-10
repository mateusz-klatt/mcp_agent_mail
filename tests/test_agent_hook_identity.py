"""Cross-client identity contract for installation scripts and runtime hooks."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path, PureWindowsPath

import pytest

from mcp_agent_mail.cli import _agent_state_component

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
HOOK_COMMON = ROOT / "scripts" / "hooks" / "agent_mail_common.sh"
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


def _bash_executable() -> str:
    """Locate the Git for Windows launcher, preferring ``bin`` over ``usr/bin``.

    Git for Windows ships two different binaries with this name: ``Git\\bin\\bash.exe``
    is a 47 KB launcher that sets ``MSYSTEM=MINGW64`` and puts ``/mingw64/bin`` on
    PATH, and ``Git\\usr\\bin\\bash.exe`` is the 2.4 MB shell itself, which comes up
    with neither. Starting the second one from outside MSYS gets a shell that
    resolves ``curl`` to ``C:\\Windows\\System32\\curl.exe`` instead of Git's, so it
    is not a slower path to the same place.

    Ancestors are walked **shallowest first**, which is what actually separates
    the two. Ordering by suffix does not, because the suffix is relative to the
    root: with ``git`` at ``Git/usr/bin/git.exe`` the ancestor ``Git/usr`` plus
    ``bin/bash.exe`` *is* the raw shell, so "prefer bin over usr/bin" still picks
    it. Only depth distinguishes them — the launcher lives one level up, in the
    install root, and the install root is the shallower ancestor.

    Which layout a machine presents depends on whether the caller's PATH went
    through ``/etc/profile``: a login shell resolves ``git`` to
    ``Git/mingw64/bin/git.exe``, a plain ``bash script.sh`` to
    ``Git/usr/bin/git.exe``. An order that only works for one of them is a
    preference that flips on how the installer was invoked.
    """
    discovered = shutil.which("bash")
    if os.name != "nt":
        return discovered or "bash"
    git = shutil.which("git")
    if git:
        for git_root in reversed(Path(git).resolve().parents):
            for candidate in (
                git_root / "bin" / "bash.exe",
                git_root / "usr" / "bin" / "bash.exe",
            ):
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
def test_bash_executable_prefers_launcher_when_git_sits_in_usr_bin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The layout that a per-root search order gets wrong.

    ``git`` resolves to ``Git/usr/bin/git.exe`` whenever the caller's PATH did not
    go through ``/etc/profile`` — a plain ``bash script.sh`` rather than a login
    shell, which is how an installer is normally invoked. Searching both suffixes
    per root then reaches ``Git/usr/bin/bash.exe`` one level before ``Git/bin``
    and returns the raw shell. Both files exist here, so a helper that only
    checks ``.name == "bash.exe"`` cannot tell the two answers apart.
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

    assert _bash_executable() == str(launcher)


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
    [AUTO_INSTALLER, *INTEGRATORS.values(), *INSTALLED_HOOK_SOURCES],
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
        "codex.mcp.json",
        ".vscode/mcp.json",
        'ensure_gitignore_entry "${TARGET_DIR}',
        "run_server_with_token.sh",
        "update_env_var \"HTTP_BEARER_TOKEN\"",
        "generate_bearer_token",
    )
    for path in INTEGRATORS.values():
        script = path.read_text(encoding="utf-8")
        for fragment in forbidden:
            assert fragment not in script, f"{path.name} still contains {fragment}"


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
    cut) printf ': ready\n\n'; exit 0 ;;
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
    env.update(
        {
            "AGENT_MAIL_WATCH_SECONDS": "1" if mode == "timeout" else "10",
            "AGENT_MAIL_WATCH_READY_SECONDS": "1",
            "FAKE_REQUEST_LOG": _git_bash_path(request_log),
            "FAKE_STREAM_AUTH_LOG": _git_bash_path(stream_auth_log),
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
        timeout=5,
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
    assert sum("mcp-agent-mail/session_start.sh" in command for command in commands) == 1
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
        bash_executable = windows_argv[0].strip('"')
        assert PureWindowsPath(bash_executable).name.casefold() == "bash.exe"
        assert PureWindowsPath(windows_argv[1].strip('"')).name == "hook_wrapper.sh"
        assert windows_argv[2] == event_arg
        if os.name == "nt":
            assert Path(bash_executable).is_file()
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
    assert sum("mcp-agent-mail" in command for command in rerun_commands) == 3
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
    assert len(managed_commands) == 6
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


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("use_override", [False, True])
def test_claude_and_codex_windows_hooks_support_current_and_explicit_git_bash(
    tmp_path: Path,
    client: str,
    use_override: bool,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    git_root = tmp_path / "Portable Git"
    git_bin = git_root / "mingw64" / "bin"
    detected_bash = git_root / "bin" / "bash.exe"
    codex_dir = home / "codex-profile"
    agent_mail_env = home / "agent-mail.env"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    git_bin.mkdir(parents=True)
    detected_bash.parent.mkdir(parents=True)
    git_path = shutil.which("git")
    assert git_path is not None
    fake_git = _install_bash_command_forwarder(git_bin, "git", git_path)
    detected_bash.write_text(
        "#!/bin/sh\nexit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    detected_bash.chmod(0o700)
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
        env["AGENT_MAIL_GIT_BASH_PATH"] = _git_bash_path(portable_bash)

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
        assert len(managed_commands) == 6
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
        managed_handlers = [
            handler
            for groups in hooks["hooks"].values()
            for group in groups
            for handler in group["hooks"]
            if "mcp-agent-mail" in handler.get("command", "")
        ]
        assert len(managed_handlers) == 3
        assert all(
            "D:\\Portable Git\\usr\\bin\\bash.exe" in handler["commandWindows"]
            for handler in managed_handlers
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
        "PATH": f"{_git_bash_path(fake_bin)}:/usr/bin:/bin",
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
    assert len(windows_commands) == 3
    assert len(native_commands) == 3
    assert all(_git_bash_path(windows_profile) in item for item in windows_commands)
    assert all(_git_bash_path(native_profile) in item for item in native_commands)
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
    assert len(_managed_codex_commands(windows_profile)) == 3
    assert len(_managed_codex_commands(native_profile)) == 3

    before_dry_run = _tree_snapshot(tmp_path)
    dry_run = _run_auto_installer(env, project, "--dry-run")

    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    assert "no files or directories were changed" in dry_run.stdout
    assert _tree_snapshot(tmp_path) == before_dry_run


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
