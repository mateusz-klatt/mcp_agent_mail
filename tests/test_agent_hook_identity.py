"""Cross-client identity contract for installation scripts and runtime hooks."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

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
BASH = shutil.which("bash") or "bash"


def _git_bash_path(path: str | Path) -> str:
    """Return a host path in the path dialect understood by Git Bash."""
    value = str(path)
    if os.name != "nt":
        return value
    normalized = value.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        return f"/{normalized[0].lower()}{normalized[2:]}"
    return normalized


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
        "codex": ("jq", "curl", "git", "uv"),
        "copilot": ("jq", "curl", "git"),
    }
    for client, commands in expected.items():
        script = INTEGRATORS[client].read_text(encoding="utf-8")
        for command in commands:
            assert f"require_cmd {command}" in script


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


def _integration_env(home: Path, fake_bin: Path) -> dict[str, str]:
    return {
        "HOME": _git_bash_path(home),
        "CODEX_HOME": _git_bash_path(home / ".codex"),
        "COPILOT_HOME": _git_bash_path(home / ".copilot"),
        "XDG_STATE_HOME": _git_bash_path(home / ".state"),
        "XDG_CONFIG_HOME": _git_bash_path(home / ".config"),
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
        "TMPDIR": _git_bash_path(home.parent),
        "TEMP": _git_bash_path(home.parent),
        "TMP": _git_bash_path(home.parent),
    }


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
    env_file = home / ".agent-mail.env"
    env_file.write_text(
        "AGENT_MAIL_URL=https://hermes.example/mcp/\nHTTP_BEARER_TOKEN=test-bearer\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        **os.environ,
        "HOME": _git_bash_path(home),
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
        "TMPDIR": _git_bash_path(home.parent),
        "TEMP": _git_bash_path(home.parent),
        "TMP": _git_bash_path(home.parent),
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
    payload = {
        "cwd": str(repo),
        "session_id": "identity-migration",
        "hook_event_name": "PostToolUse",
        "tool_input": {"file_path": str(target)},
    }
    return subprocess.run(
        [BASH, _git_bash_path(ROOT / "scripts" / "hooks" / script_name)],
        cwd=repo,
        env=env,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )


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
                                    "command": "bash /work/repo/.claude/hooks/session_start.sh || true",
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
    assert commands.count("bash /work/repo/.claude/hooks/session_start.sh || true") == 1
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
        assert managed[0]["commandWindows"].startswith(
            '"C:\\Program Files\\Git\\bin\\bash.exe"'
        )
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
        assert "C:\\Program Files\\Git\\bin\\bash.exe" in handler["powershell"]
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

    for forbidden in (
        project / ".claude",
        project / ".codex",
        project / ".vscode",
        project / "codex.mcp.json",
        project / ".mcp.json",
        project / "scripts" / "run_server_with_token.sh",
    ):
        assert not forbidden.exists()


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
        ("codex", "codex-profile/config.toml", "invalid = [\n"),
        (
            "codex",
            "codex-profile/config.toml",
            '[mcp_servers.mcp_agent_mail]\nhttp_headers = 7\n',
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
    before = _tree_snapshot(tmp_path)
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(home / "codex-profile"),
    }

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
    before = _tree_snapshot(tmp_path)
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "CODEX_HOME": _git_bash_path(codex_dir),
    }

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


@pytest.mark.parametrize(
    "existing_toml",
    [
        (
            'notify = ["/usr/local/bin/foreign-notify"]\n'
            'model = "keep-model"\n\n'
            '[mcp_servers . foreign]\ncommand = "keep-foreign"\n\n'
            '[mcp_servers . "claude-delegator"]\ncommand = "keep-delegator"\n\n'
            '[mcp_servers . mcp_agent_mail]\nurl = "https://old.example/mcp/"\n\n'
            '[mcp_servers . mcp_agent_mail . http_headers]\n'
            'Authorization = "Bearer old"\nX-Tenant = "keep-tenant"\n'
        ),
        (
            'notify = ["/usr/local/bin/foreign-notify"]\n'
            'mcp_servers = { foreign = { command = "keep-foreign" }, '
            '"claude-delegator" = { command = "keep-delegator" }, '
            'mcp_agent_mail = { url = "https://old.example/mcp/", '
            'http_headers = { Authorization = "Bearer old", '
            'X-Tenant = "keep-tenant" } } }\n'
        ),
        (
            'notify = ["/usr/local/bin/foreign-notify"]\n\n'
            '[mcp_servers.foreign]\ncommand = "keep-foreign"\n\n'
            '[mcp_servers."claude-delegator"]\ncommand = "keep-delegator"\n\n'
            '[mcp_servers."mcp-agent-mail"]\nurl = "https://old.example/mcp/"\n\n'
            '[mcp_servers."mcp-agent-mail".http_headers]\n'
            'Authorization = "Bearer old"\nX-Tenant = "keep-tenant"\n'
        ),
        (
            'notify = ["/usr/local/bin/foreign-notify", '
            '"/old/.codex/hooks/mcp-agent-mail/notify_wrapper.sh"]\n\n'
            '[mcp_servers.foreign]\ncommand = "keep-foreign"\n\n'
            '[mcp_servers.claude-delegator]\ncommand = "keep-delegator"\n\n'
            '[mcp_servers.mcp_agent_mail]\nurl = "https://old.example/mcp/"\n\n'
            '[mcp_servers.mcp_agent_mail.http_headers]\n'
            'Authorization = "Bearer old"\nX-Tenant = "keep-tenant"\n'
        ),
    ],
)
def test_codex_integrator_semantically_merges_all_supported_toml_shapes(
    tmp_path: Path,
    existing_toml: str,
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
    merged = tomllib.loads(config_path.read_text(encoding="utf-8"))
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
    assert merged["mcp_servers"]["foreign"] == {"command": "keep-foreign"}
    assert merged["mcp_servers"]["claude-delegator"] == {
        "command": "keep-delegator"
    }
    assert merged["notify"] == ["/usr/local/bin/foreign-notify"]


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
        parsed = shlex.split(command.removesuffix(" || true"))
        assert parsed[0] == "AGENT_MAIL_CLAUDE_SLOT=1"
        assert parsed[1] == "bash"
        assert Path(parsed[2]).is_relative_to(home / ".claude" / "hooks")


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
    codex_dir = home / "codex-profile"
    agent_mail_env = home / "agent-mail.env"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    portable_bash = fake_bin / "portable-git-bash"
    portable_bash.write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    portable_bash.chmod(0o700)
    fake_uname = fake_bin / "uname"
    fake_uname.write_text(
        "#!/usr/bin/env bash\nprintf 'MINGW64_NT-10.0\\n'\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_uname.chmod(0o700)
    fake_cygpath = fake_bin / "cygpath"
    fake_cygpath.write_text(
        r"""#!/usr/bin/env bash
case "$1" in
  -u)
    case "$2" in
      'D:\Profiles\Codex') printf '%s\n' "$FAKE_CODEX_POSIX" ;;
      'Q:\AgentMail\shared.env') printf '%s\n' "$FAKE_AGENT_MAIL_ENV_POSIX" ;;
      *) printf '%s\n' "$2" ;;
    esac ;;
  -w|-m)
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
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "AGENT_MAIL_ENV_FILE": r"Q:\AgentMail\shared.env",
        "FAKE_AGENT_MAIL_ENV_POSIX": _git_bash_path(agent_mail_env),
        "FAKE_CODEX_POSIX": _git_bash_path(codex_dir),
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
        assert all(
            '"D:\\Portable Git\\usr\\bin\\bash.exe"' in command
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
    assert "AGENT_MAIL_INSTALL_AUTHORIZATION" in codex


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
    before = _tree_snapshot(tmp_path)
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "COPILOT_HOME": _git_bash_path(copilot_dir),
    }

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
    before = _tree_snapshot(tmp_path)
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "COPILOT_HOME": _git_bash_path(copilot_dir),
    }

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


def test_copilot_windows_hooks_use_the_current_custom_git_bash(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    fake_bin = tmp_path / "bin"
    copilot_dir = home / "copilot-profile"
    home.mkdir()
    project.mkdir()
    fake_bin.mkdir()
    fake_uname = fake_bin / "uname"
    fake_uname.write_text(
        "#!/usr/bin/env bash\nprintf 'MINGW64_NT-10.0\\n'\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_uname.chmod(0o700)
    fake_cygpath = fake_bin / "cygpath"
    fake_cygpath.write_text(
        r"""#!/usr/bin/env bash
case "$1" in
  -u)
    case "$2" in
      'D:\Profiles\Copilot') printf '%s\n' "$FAKE_COPILOT_POSIX" ;;
      'D:\Profiles\agent-mail.env') printf '%s\n' "$FAKE_AGENT_MAIL_ENV_POSIX" ;;
      *) printf '%s\n' "$2" ;;
    esac ;;
  -w|-m)
    case "$2" in
      */bash|*/bash.exe) printf '%s\n' 'D:\Portable Git\usr\bin\bash.exe' ;;
      *) printf '%s\n' 'D:\Profiles\Copilot\hooks\mcp-agent-mail\hook_wrapper.sh' ;;
    esac ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_cygpath.chmod(0o700)
    env = {
        **os.environ,
        **_integration_env(home, fake_bin),
        "AGENT_MAIL_ENV_FILE": r"D:\Profiles\agent-mail.env",
        "APPDATA": _git_bash_path(home / "appdata"),
        "COPILOT_HOME": r"D:\Profiles\Copilot",
        "FAKE_AGENT_MAIL_ENV_POSIX": _git_bash_path(home / "agent-mail.env"),
        "FAKE_COPILOT_POSIX": _git_bash_path(copilot_dir),
    }

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
    assert all(
        "D:\\Portable Git\\usr\\bin\\bash.exe" in handler["powershell"]
        for handler in managed
    )


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
    uv_path = shutil.which("uv")
    assert uv_path is not None
    (fake_bin / "uv").symlink_to(uv_path)
    env = {
        **os.environ,
        "HOME": _git_bash_path(home),
        "CODEX_HOME": _git_bash_path(codex_dir),
        "XDG_STATE_HOME": _git_bash_path(home / ".state"),
        "XDG_CONFIG_HOME": _git_bash_path(home / ".config"),
        "VSCODE_MCP_CONFIG_PATH": _git_bash_path(vscode_mcp),
        "INTEGRATION_MCP_URL": "https://hermes.example/mcp/",
        "INTEGRATION_BEARER_TOKEN": "never-log-prefix-123456-never-log-suffix",
        # Exclude real Claude/Codex binaries while retaining ordinary POSIX
        # tools. The explicit CODEX_HOME above still detects Codex.
        "PATH": f"{_git_bash_path(fake_bin)}:/usr/bin:/bin",
        "TMPDIR": _git_bash_path(home.parent),
        "TEMP": _git_bash_path(home.parent),
        "TMP": _git_bash_path(home.parent),
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
        """
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
