from __future__ import annotations

import asyncio
import os
import shutil
from asyncio.subprocess import PIPE
from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.guard import (
    install_guard,
    install_prepush_guard,
    render_precommit_script,
    uninstall_guard,
)
from mcp_agent_mail.storage import ensure_archive


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


def _legacy_cmd_fixture(hook_name: str, line_ending: bytes) -> bytes:
    body = (
        "@echo off\n"
        "setlocal\n"
        'set "DIR=%~dp0"\n'
        f'python "%DIR%{hook_name}" %*\n'
        "exit /b %ERRORLEVEL%\n"
    ).encode()
    return body.replace(b"\n", line_ending)


def _retired_cmd_fixture(line_ending: bytes) -> bytes:
    body = (
        "@echo off\n"
        "REM mcp-agent-mail disabled legacy cmd shim v1\n"
        "1>&2 echo [mcp-agent-mail] Legacy .cmd shim is disabled; use Git or the sibling PowerShell shim.\n"
        "exit /b 126\n"
    ).encode()
    return body.replace(b"\n", line_ending)


def _git_bash_path(path: Path) -> str:
    value = str(path)
    if os.name != "nt":
        return value
    normalized = value.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        return f"/{normalized[0].lower()}{normalized[2:]}"
    return normalized


@pytest.mark.asyncio
async def test_guard_render_and_conflict_message(isolated_env, tmp_path: Path):
    settings = get_settings()
    archive = await ensure_archive(settings, "backend")
    script = render_precommit_script(archive)
    assert "FILE_RESERVATIONS_DIR" in script and "AGENT_NAME" in script

    # Initialize dummy repo and write a file_reservation artifact that conflicts with the staged file
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    proc_init = await asyncio.create_subprocess_exec("git", "init", cwd=str(repo_dir))
    assert (await proc_init.wait()) == 0
    # Create a file and stage it
    f = repo_dir / "agents" / "Blue" / "inbox" / "2025" / "10" / "note.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x", encoding="utf-8")
    proc_add = await asyncio.create_subprocess_exec(
        "git",
        "add",
        f.relative_to(repo_dir).as_posix(),
        cwd=str(repo_dir),
    )
    assert (await proc_add.wait()) == 0

    # Write a conflicting file reservation in archive
    reservations_dir = archive.root / "file_reservations"
    reservations_dir.mkdir(parents=True, exist_ok=True)
    (reservations_dir / "c.json").write_text(
        '{"agent":"Other","path_pattern":"agents/*/inbox/*/*/*.md","expires_ts":"2999-01-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )

    # Install the guard and run it with AGENT_NAME set to Blue
    hook_path = await install_guard(settings, "backend", repo_dir)
    assert hook_path.exists()
    # WORKTREES_ENABLED=1 is required for the guard to actually run (not exit early)
    env = os.environ.copy()
    env.update(AGENT_NAME="Blue", WORKTREES_ENABLED="1")
    proc_hook = await asyncio.create_subprocess_exec(
        BASH,
        "-c",
        'exec "$1"',
        "mcp-agent-mail-hook",
        _git_bash_path(hook_path),
        cwd=str(repo_dir),
        env=env,
        stdout=PIPE,
        stderr=PIPE,
    )
    _stdout_bytes, stderr_bytes = await proc_hook.communicate()
    # Expect non-zero due to conflict and helpful message
    assert proc_hook.returncode != 0
    stderr_text = (stderr_bytes.decode("utf-8", "ignore") if stderr_bytes else "")
    assert "file_reservation" in stderr_text.lower() or "exclusive" in stderr_text.lower()

    # Uninstall guard path returns True and removes file
    removed = await uninstall_guard(repo_dir)
    assert removed is True


@pytest.mark.asyncio
async def test_uninstall_guard_removes_agent_mail_windows_shims(isolated_env, tmp_path: Path):
    settings = get_settings()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    proc_init = await asyncio.create_subprocess_exec("git", "init", cwd=str(repo_dir))
    assert (await proc_init.wait()) == 0

    await install_guard(settings, "backend", repo_dir)
    await install_prepush_guard(settings, "backend", repo_dir)

    hooks_dir = repo_dir / ".git" / "hooks"
    assert not (hooks_dir / "pre-commit.cmd").exists()
    assert (hooks_dir / "pre-commit.ps1").exists()
    assert not (hooks_dir / "pre-push.cmd").exists()
    assert (hooks_dir / "pre-push.ps1").exists()

    removed = await uninstall_guard(repo_dir)

    assert removed is True
    assert not (hooks_dir / "pre-commit").exists()
    assert not (hooks_dir / "pre-commit.cmd").exists()
    assert not (hooks_dir / "pre-commit.ps1").exists()
    assert not (hooks_dir / "pre-push").exists()
    assert not (hooks_dir / "pre-push.cmd").exists()
    assert not (hooks_dir / "pre-push.ps1").exists()


@pytest.mark.parametrize(
    ("hook_name", "installer"),
    (("pre-commit", install_guard), ("pre-push", install_prepush_guard)),
)
@pytest.mark.parametrize("line_ending", (b"\n", b"\r\n", b"\r\r\n"))
@pytest.mark.asyncio
async def test_guard_retires_only_exact_legacy_cmd_shims_in_place(
    isolated_env,
    tmp_path: Path,
    hook_name: str,
    installer,
    line_ending: bytes,
):
    settings = get_settings()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    proc_init = await asyncio.create_subprocess_exec("git", "init", cwd=str(repo_dir))
    assert (await proc_init.wait()) == 0

    cmd_path = repo_dir / ".git" / "hooks" / f"{hook_name}.cmd"
    cmd_path.write_bytes(_legacy_cmd_fixture(hook_name, line_ending))

    await installer(settings, "backend", repo_dir)

    retired = _retired_cmd_fixture(b"\r\n")
    assert cmd_path.read_bytes() == retired
    assert b"%*" not in retired
    assert b"python" not in retired.lower()

    await installer(settings, "backend", repo_dir)
    assert cmd_path.read_bytes() == retired


@pytest.mark.parametrize(
    ("hook_name", "installer"),
    (("pre-commit", install_guard), ("pre-push", install_prepush_guard)),
)
@pytest.mark.parametrize(
    "foreign_body",
    (
        b"@echo off\r\necho foreign hook\r\n",
        b"x" * 10_000,
        _legacy_cmd_fixture("pre-commit", b"\r\n") + b"echo foreign\r\n",
        _retired_cmd_fixture(b"\r\n") + b"echo foreign\r\n",
    ),
)
@pytest.mark.asyncio
async def test_guard_preserves_foreign_cmd_shims_byte_for_byte(
    isolated_env,
    tmp_path: Path,
    hook_name: str,
    installer,
    foreign_body: bytes,
):
    settings = get_settings()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    proc_init = await asyncio.create_subprocess_exec("git", "init", cwd=str(repo_dir))
    assert (await proc_init.wait()) == 0

    cmd_path = repo_dir / ".git" / "hooks" / f"{hook_name}.cmd"
    cmd_path.write_bytes(foreign_body)
    await installer(settings, "backend", repo_dir)
    assert cmd_path.read_bytes() == foreign_body

    assert await uninstall_guard(repo_dir) is True
    assert cmd_path.read_bytes() == foreign_body


@pytest.mark.parametrize("hook_name", ("pre-commit", "pre-push"))
@pytest.mark.parametrize("line_ending", (b"\n", b"\r\n", b"\r\r\n"))
@pytest.mark.parametrize("body_kind", ("legacy", "retired"))
@pytest.mark.asyncio
async def test_uninstall_guard_removes_only_exact_owned_cmd_variants(
    isolated_env,
    tmp_path: Path,
    hook_name: str,
    line_ending: bytes,
    body_kind: str,
):
    settings = get_settings()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    proc_init = await asyncio.create_subprocess_exec("git", "init", cwd=str(repo_dir))
    assert (await proc_init.wait()) == 0

    installer = install_guard if hook_name == "pre-commit" else install_prepush_guard
    await installer(settings, "backend", repo_dir)
    cmd_path = repo_dir / ".git" / "hooks" / f"{hook_name}.cmd"
    body = _legacy_cmd_fixture(hook_name, line_ending) if body_kind == "legacy" else _retired_cmd_fixture(line_ending)
    cmd_path.write_bytes(body)

    assert await uninstall_guard(repo_dir) is True
    assert not cmd_path.exists()


@pytest.mark.parametrize(
    ("hook_name", "installer"),
    (("pre-commit", install_guard), ("pre-push", install_prepush_guard)),
)
@pytest.mark.asyncio
async def test_uninstall_guard_preserves_modified_powershell_shim(
    isolated_env,
    tmp_path: Path,
    hook_name: str,
    installer,
):
    settings = get_settings()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    proc_init = await asyncio.create_subprocess_exec("git", "init", cwd=str(repo_dir))
    assert (await proc_init.wait()) == 0

    ps1_path = repo_dir / ".git" / "hooks" / f"{hook_name}.ps1"
    foreign_body = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$hook = Join-Path $PSScriptRoot '{hook_name}'\n"
        "python $hook @args\n"
        "exit $LASTEXITCODE\n"
        "Write-Output 'foreign'\n"
    ).encode()
    ps1_path.write_bytes(foreign_body)
    await installer(settings, "backend", repo_dir)
    assert await uninstall_guard(repo_dir) is True
    assert ps1_path.read_bytes() == foreign_body
