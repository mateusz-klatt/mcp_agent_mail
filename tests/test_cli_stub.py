"""Packaging and installer contract for the real ``mcp-agent-mail`` CLI."""
from __future__ import annotations

import importlib.metadata
import os
import shlex
import shutil
import subprocess
import sysconfig
import tomllib
from pathlib import Path

import pytest

from mcp_agent_mail.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"


def _bash_executable() -> str:
    discovered = shutil.which("bash")
    if os.name != "nt":
        return discovered or "bash"
    git = shutil.which("git")
    if git:
        git_root = Path(git).resolve().parent.parent
        for candidate in (
            git_root / "bin" / "bash.exe",
            git_root / "usr" / "bin" / "bash.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return discovered or "bash"


BASH = _bash_executable()


def _git_bash_path(path: Path) -> str:
    value = str(path)
    if os.name != "nt":
        return value
    normalized = value.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        return f"/{normalized[0].lower()}{normalized[2:]}"
    return normalized


def _shell_path_key(value: str) -> str:
    """Normalize native and MSYS spellings of the same working directory."""
    normalized = value.replace("\\", "/").rstrip("/")
    if (
        len(normalized) >= 3
        and normalized[0] == "/"
        and normalized[1].isalpha()
        and normalized[2] == "/"
    ):
        normalized = f"{normalized[1]}:{normalized[2:]}"
    if len(normalized) >= 3 and normalized[0].isalpha() and normalized[1:3] == ":/":
        return normalized.casefold()
    return normalized


def _bash(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _install_fake_uv(fake_bin: Path) -> Path:
    fake_bin.mkdir(parents=True, exist_ok=True)
    executable = fake_bin / "uv"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%s|%s\\n' \"$PWD\" \"$*\" >> \"$UV_CALL_LOG\"\n"
        "if [[ -n ${UV_FAIL_ON:-} && $* == *\"$UV_FAIL_ON\"* ]]; then\n"
        "  exit 42\n"
        "fi\n",
        encoding="utf-8",
        newline="\n",
    )
    executable.chmod(0o700)
    return executable


def _installer_env(tmp_path: Path, *, fail_on: str = "") -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    call_log = tmp_path / "uv-calls.log"
    home.mkdir()
    _install_fake_uv(fake_bin)
    path_entries = [
        _git_bash_path(fake_bin),
        *(
            _git_bash_path(Path(entry))
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry
        ),
    ]
    env = {
        **os.environ,
        "BASH_ENV": "",
        "HOME": _git_bash_path(home),
        "PATH": ":".join(path_entries),
        "UV_CALL_LOG": _git_bash_path(call_log),
        "UV_FAIL_ON": fail_on,
    }
    return env, home, call_log


def _install_cli_script(*, uname_value: str | None = None) -> str:
    simulated_uname = ""
    if uname_value is not None:
        simulated_uname = f"uname() {{ printf '%s\\n' {shlex.quote(uname_value)}; }}\n"
    return (
        simulated_uname
        + f"source {shlex.quote(_git_bash_path(INSTALLER))}\n"
        + f"REPO_DIR={shlex.quote(_git_bash_path(ROOT))}\n"
        + "install_cli\n"
    )


def _run_install_cli(
    tmp_path: Path,
    *,
    fail_on: str = "",
    uname_value: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    env, home, call_log = _installer_env(tmp_path, fail_on=fail_on)
    script = _install_cli_script(uname_value=uname_value)
    return _bash(script, cwd=ROOT, env=env), home, call_log


def test_shell_path_key_equates_native_and_msys_drive_spellings() -> None:
    assert _shell_path_key("D:/a/mcp_agent_mail/") == _shell_path_key(
        "/d/a/mcp_agent_mail"
    )


def test_project_defines_one_canonical_cli_entrypoint() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["scripts"] == {"mcp-agent-mail": "mcp_agent_mail.__main__:main"}


def test_editable_distribution_exposes_the_canonical_cli_entrypoint() -> None:
    distribution = importlib.metadata.distribution("mcp-agent-mail")
    console_scripts = [
        entry_point
        for entry_point in distribution.entry_points
        if entry_point.group == "console_scripts"
    ]

    assert [(entry.name, entry.value) for entry in console_scripts] == [
        ("mcp-agent-mail", "mcp_agent_mail.__main__:main")
    ]
    assert console_scripts[0].load() is main


def test_native_cli_launcher_runs_real_typer_help() -> None:
    executable_name = "mcp-agent-mail.exe" if os.name == "nt" else "mcp-agent-mail"
    launcher = Path(sysconfig.get_path("scripts")) / executable_name

    assert launcher.is_file(), f"uv sync did not install {launcher}"
    result = subprocess.run(
        [launcher, "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Developer utilities for Iris" in output
    assert "doctor" in output


def test_installer_delegates_global_cli_installation_and_path_setup_to_uv(tmp_path: Path) -> None:
    result, _home, call_log = _run_install_cli(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = [
        line.rsplit("|", maxsplit=1)
        for line in call_log.read_text(encoding="utf-8").splitlines()
    ]
    assert [command for _cwd, command in calls] == [
        "tool install --editable --python 3.14 .",
        "tool update-shell",
        "tool run --offline mcp-agent-mail --help",
    ]
    assert {_shell_path_key(cwd) for cwd, _command in calls} == {
        _shell_path_key(str(ROOT))
    }
    assert "Installed the mcp-agent-mail CLI" in result.stdout


@pytest.mark.parametrize(
    ("fail_on", "message"),
    [
        ("tool install", "Failed to install the mcp-agent-mail CLI with uv"),
        ("tool update-shell", "could not add its executable directory to PATH"),
        ("tool run", "did not pass its help smoke test"),
    ],
)
def test_installer_propagates_uv_cli_failures(
    tmp_path: Path,
    fail_on: str,
    message: str,
) -> None:
    result, _home, _call_log = _run_install_cli(tmp_path, fail_on=fail_on)

    assert result.returncode != 0
    assert message in result.stdout + result.stderr


def test_installer_refuses_to_overwrite_its_legacy_rejecting_stub(tmp_path: Path) -> None:
    env, home, call_log = _installer_env(tmp_path)
    legacy_stub = home / ".local" / "bin" / "mcp-agent-mail"
    legacy_stub.parent.mkdir(parents=True)
    original = "#!/usr/bin/env bash\nMCP Agent Mail is NOT a CLI tool\nsentinel\n"
    legacy_stub.write_text(original, encoding="utf-8", newline="\n")
    result = _bash(_install_cli_script(), cwd=ROOT, env=env)

    assert result.returncode != 0
    assert _git_bash_path(legacy_stub) in result.stdout + result.stderr
    assert "explicit review" in result.stdout + result.stderr
    assert legacy_stub.read_text(encoding="utf-8") == original
    assert not call_log.exists()


@pytest.mark.parametrize("shadow_kind", ["file", "broken_symlink"])
def test_installer_refuses_any_extensionless_windows_shadow(
    tmp_path: Path,
    shadow_kind: str,
) -> None:
    env, home, call_log = _installer_env(tmp_path)
    shadow = home / ".local" / "bin" / "mcp-agent-mail"
    shadow.parent.mkdir(parents=True)
    if shadow_kind == "file":
        original = "custom command that must remain untouched\n"
        shadow.write_text(original, encoding="utf-8", newline="\n")
    else:
        original = "missing-cli-target"
        try:
            shadow.symlink_to(original)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable on this platform: {exc}")

    result = _bash(
        _install_cli_script(uname_value="MINGW64_NT-10.0"),
        cwd=ROOT,
        env=env,
    )

    assert result.returncode != 0
    assert _git_bash_path(shadow) in result.stdout + result.stderr
    assert "shadows the Windows CLI" in result.stdout + result.stderr
    if shadow_kind == "file":
        assert shadow.read_text(encoding="utf-8") == original
    else:
        assert shadow.is_symlink()
        assert str(shadow.readlink()) == original
    assert not call_log.exists()


def test_installer_has_no_cli_stub_or_alias_shims_and_respects_start_only() -> None:
    content = INSTALLER.read_text(encoding="utf-8")
    start_only = content[content.index('if [[ "${START_ONLY}" -eq 1 ]]'):]
    start_only = start_only[: start_only.index("  ensure_uv")]
    normal_setup = content[content.index("  ensure_uv"):]

    assert "install_cli_stub" not in content
    assert 'for variant in "mcp_agent_mail"' not in content
    assert "install_cli" not in start_only
    assert normal_setup.index("  ensure_repo") < normal_setup.index("  sync_deps")
    assert normal_setup.index("  sync_deps") < normal_setup.index("  install_cli")
