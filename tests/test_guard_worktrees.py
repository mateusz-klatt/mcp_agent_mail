"""Tests for guard hook functionality in git worktree scenarios.

Tests guard installation, hook generation, and conflict detection in various
git configurations including worktrees, custom hooksPath, and hook preservation.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.guard import (
    install_guard,
    install_prepush_guard,
    render_precommit_script,
    render_prepush_script,
    uninstall_guard,
)
from mcp_agent_mail.storage import ensure_archive, write_file_reservation_record


def _init_git_repo(repo_path: Path) -> None:
    """Initialize a git repository."""
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_path), check=True)


def _create_initial_commit(repo_path: Path) -> None:
    """Create an initial commit in the repo."""
    readme = repo_path / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_path), check=True, capture_output=True)


def _create_worktree(main_repo: Path, worktree_path: Path, branch_name: str) -> None:
    """Create a git worktree."""
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch_name],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
    )


def _run_hook(hook_path: Path, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    """Run a hook script."""
    full_env = os.environ.copy()
    full_env.update(env)
    # sys.executable, not "python" — see test_precommit_enforcement.py: the bare
    # name does not exist on macOS or on distributions that ship only python3.
    return subprocess.run(
        [sys.executable, str(hook_path)],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
    )


def _git_add(repo_path: Path, file_path: str) -> None:
    """Stage a file in a git repository."""
    subprocess.run(["git", "add", file_path], cwd=str(repo_path), check=True)


def _git_config(repo_path: Path, key: str, value: str) -> None:
    """Set a git config value."""
    subprocess.run(["git", "config", key, value], cwd=str(repo_path), check=True)


# =============================================================================
# Basic Worktree Installation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_install_in_worktree(isolated_env, tmp_path: Path):
    """Test guard installation in a git worktree."""
    settings = get_settings()

    # Create main repo with initial commit
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir(parents=True)
    _init_git_repo(main_repo)
    _create_initial_commit(main_repo)

    # Create worktree
    worktree = tmp_path / "worktree"
    _create_worktree(main_repo, worktree, "feature-branch")

    # Install guard in worktree
    await ensure_archive(settings, "worktree-test")
    hook_path = await install_guard(settings, "worktree-test", worktree)

    # Hook should be installed in the worktree's git dir
    assert hook_path.exists()
    assert "pre-commit" in hook_path.name


@pytest.mark.asyncio
async def test_guard_conflict_detection_in_worktree(isolated_env, tmp_path: Path):
    """Test that guard detects conflicts in worktree context."""
    settings = get_settings()

    # Create main repo
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir(parents=True)
    _init_git_repo(main_repo)
    _create_initial_commit(main_repo)

    # Create worktree
    worktree = tmp_path / "worktree"
    _create_worktree(main_repo, worktree, "feature-branch")

    # Create archive with file reservation
    archive = await ensure_archive(settings, "worktree-test")
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "src/*.py",
            "exclusive": True,
        },
    )

    # Render and write the guard script
    script = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script, encoding="utf-8")

    # Stage a conflicting file
    src_dir = worktree / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "app.py").write_text("print('hello')", encoding="utf-8")
    _git_add(worktree, "src/app.py")

    # Run the guard script with WORKTREES_ENABLED
    result = _run_hook(
        script_path,
        worktree,
        {"AGENT_NAME": "MyAgent", "WORKTREES_ENABLED": "1"},
    )

    # Should detect conflict
    assert result.returncode == 1
    assert "conflict" in result.stderr.lower() or "file_reservation" in result.stderr.lower()


# =============================================================================
# Custom core.hooksPath Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_install_custom_hookspath(isolated_env, tmp_path: Path):
    """Test guard installation with custom core.hooksPath."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Set custom hooksPath
    custom_hooks = tmp_path / "custom-hooks"
    custom_hooks.mkdir(parents=True)
    _git_config(repo, "core.hooksPath", str(custom_hooks))

    # Install guard
    hook_path = await install_guard(settings, "hookspath-test", repo)

    # Hook should be in custom hooks directory
    assert hook_path.parent == custom_hooks or str(custom_hooks) in str(hook_path)


@pytest.mark.asyncio
async def test_guard_install_relative_hookspath(isolated_env, tmp_path: Path):
    """Test guard installation with relative core.hooksPath."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Set relative hooksPath
    (repo / "my-hooks").mkdir(parents=True)
    _git_config(repo, "core.hooksPath", "my-hooks")

    # Install guard
    hook_path = await install_guard(settings, "rel-hookspath-test", repo)

    # Hook should be resolved relative to repo root
    assert hook_path.exists()


# =============================================================================
# Husky v9 hooksPath Tests
# =============================================================================


def _write_husky_v9_layout(
    repo: Path,
    hook_name: str,
    tracked_body: str,
) -> tuple[Path, Path]:
    """Create the essential Husky v9 runtime stub, resolver, and tracked hook."""
    husky_dir = repo / ".husky"
    runtime_dir = husky_dir / "_"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    resolver = runtime_dir / "h"
    resolver.write_text(
        "#!/usr/bin/env sh\n"
        'hook_name="${0##*/}"\n'
        'tracked="${0%/*/*}/$hook_name"\n'
        '[ ! -f "$tracked" ] && exit 0\n'
        'sh -e "$tracked" "$@"\n'
        "exit $?\n",
        encoding="utf-8",
    )
    resolver.chmod(0o755)

    stub = runtime_dir / hook_name
    stub.write_text('#!/usr/bin/env sh\n. "$(dirname "$0")/h"\n', encoding="utf-8")
    stub.chmod(0o755)

    tracked = husky_dir / hook_name
    tracked.write_text(tracked_body, encoding="utf-8")
    tracked.chmod(0o755)
    return runtime_dir, tracked


@pytest.mark.asyncio
async def test_guard_install_husky_v9_runs_tracked_hook(isolated_env, tmp_path: Path):
    """Renaming the Husky stub to .orig must not change its logical hook name."""
    settings = get_settings()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    runtime_dir, _tracked = _write_husky_v9_layout(
        repo,
        "pre-commit",
        "#!/usr/bin/env sh\necho HUSKY_TRACKED_RAN\n",
    )
    _git_config(repo, "core.hooksPath", ".husky/_")

    hook_path = await install_guard(settings, "husky-v9-test", repo)

    assert (runtime_dir / "pre-commit.orig").exists()
    result = _run_hook(
        hook_path,
        repo,
        {"WORKTREES_ENABLED": "0", "GIT_IDENTITY_ENABLED": "0"},
    )
    assert result.returncode == 0, result.stderr
    assert "HUSKY_TRACKED_RAN" in result.stdout


@pytest.mark.asyncio
async def test_guard_install_husky_v9_propagates_failure(isolated_env, tmp_path: Path):
    """A failing tracked Husky hook must fail the Agent Mail chain-runner."""
    settings = get_settings()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    _write_husky_v9_layout(
        repo,
        "pre-commit",
        "#!/usr/bin/env sh\necho HUSKY_TRACKED_FAILED\nexit 23\n",
    )
    _git_config(repo, "core.hooksPath", ".husky/_")

    hook_path = await install_guard(settings, "husky-v9-failure-test", repo)
    result = _run_hook(
        hook_path,
        repo,
        {"WORKTREES_ENABLED": "0", "GIT_IDENTITY_ENABLED": "0"},
    )

    assert "HUSKY_TRACKED_FAILED" in result.stdout
    assert result.returncode == 23


@pytest.mark.asyncio
async def test_prepush_guard_husky_v9_forwards_argv_and_stdin(isolated_env, tmp_path: Path):
    """Husky pre-push receives Git's arguments and a fresh copy of its stdin."""
    settings = get_settings()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    _write_husky_v9_layout(
        repo,
        "pre-push",
        "#!/usr/bin/env sh\n"
        'printf "HUSKY_ARGS=%s|%s\\n" "$1" "$2"\n'
        "cat\n",
    )
    _git_config(repo, "core.hooksPath", ".husky/_")
    hook_path = await install_prepush_guard(settings, "husky-v9-prepush-test", repo)
    payload = "refs/heads/main 111 refs/heads/main 000\n"
    env = os.environ.copy()
    env.update({"WORKTREES_ENABLED": "0", "GIT_IDENTITY_ENABLED": "0"})

    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(hook_path), "origin", "ssh://example.invalid/repo.git"],
        cwd=repo,
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert isinstance(result.stdout, str)
    assert "HUSKY_ARGS=origin|ssh://example.invalid/repo.git" in result.stdout
    assert payload in result.stdout


# =============================================================================
# Hook Preservation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_preserves_existing_hook(isolated_env, tmp_path: Path):
    """Test that guard preserves existing pre-commit hook as .orig."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Create existing pre-commit hook
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "pre-commit"
    existing_hook.write_text("#!/bin/bash\necho 'existing hook'\n", encoding="utf-8")
    existing_hook.chmod(0o755)

    # Install guard
    await install_guard(settings, "preserve-test", repo)

    # Original hook should be preserved as .orig
    orig_hook = hooks_dir / "pre-commit.orig"
    assert orig_hook.exists()
    assert "existing hook" in orig_hook.read_text()


@pytest.mark.asyncio
async def test_guard_doesnt_overwrite_own_orig(isolated_env, tmp_path: Path):
    """Test that reinstalling guard doesn't overwrite .orig file."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Create existing pre-commit hook
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "pre-commit"
    existing_hook.write_text("#!/bin/bash\necho 'original'\n", encoding="utf-8")
    existing_hook.chmod(0o755)

    # Install guard first time
    await install_guard(settings, "preserve-test", repo)

    # Verify .orig was created
    orig_hook = hooks_dir / "pre-commit.orig"
    assert orig_hook.exists()
    original_content = orig_hook.read_text()

    # Install guard second time
    await install_guard(settings, "preserve-test", repo)

    # .orig should still have original content
    assert orig_hook.read_text() == original_content


# =============================================================================
# Gate Variations Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_gate_worktrees_enabled_true(isolated_env, tmp_path: Path):
    """Test guard runs when WORKTREES_ENABLED=1."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")
    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with staged file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")

    # Run with WORKTREES_ENABLED=1
    result = _run_hook(script_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})

    # Should run (no conflicts, so exit 0)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_guard_gate_worktrees_enabled_false(isolated_env, tmp_path: Path):
    """Test guard exits early when WORKTREES_ENABLED=0."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")

    # Add a conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "*.txt",
            "exclusive": True,
        },
    )

    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with staged file that would conflict
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")

    # Run with WORKTREES_ENABLED=0 (disabled)
    result = _run_hook(script_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "0"})

    # Should exit early with 0 (no conflict check)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_guard_gate_git_identity_enabled(isolated_env, tmp_path: Path):
    """Test guard enforces conflicts when only GIT_IDENTITY_ENABLED=1 is set."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")
    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with staged file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "file.txt",
            "exclusive": True,
        },
    )

    # Run with GIT_IDENTITY_ENABLED=1 (alternative gate) and WORKTREES_ENABLED explicitly off.
    result = _run_hook(
        script_path,
        repo,
        {
            "AGENT_NAME": "TestAgent",
            "WORKTREES_ENABLED": "0",
            "GIT_IDENTITY_ENABLED": "1",
        },
    )

    assert result.returncode == 1
    assert "conflict" in result.stderr.lower()


@pytest.mark.asyncio
async def test_guard_gate_various_true_values(isolated_env, tmp_path: Path):
    """Test guard recognizes various truthy values for gate."""
    settings = get_settings()
    archive = await ensure_archive(settings, "gate-test")
    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "file.txt").write_text("content", encoding="utf-8")
    _git_add(repo, "file.txt")

    # Test various truthy values
    for value in ["1", "true", "True", "TRUE", "yes", "Yes", "t", "T", "y", "Y"]:
        result = _run_hook(script_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": value})
        # All should run (return 0 for no conflicts)
        assert result.returncode == 0, f"Gate value '{value}' should be truthy"


# =============================================================================
# Advisory Mode Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_advisory_mode_warn(isolated_env, tmp_path: Path):
    """Test guard in advisory/warn mode doesn't block on conflicts."""
    settings = get_settings()
    archive = await ensure_archive(settings, "advisory-test")

    # Add conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "*.py",
            "exclusive": True,
        },
    )

    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with conflicting file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "app.py").write_text("print('hello')", encoding="utf-8")
    _git_add(repo, "app.py")

    # Run in advisory mode
    result = _run_hook(
        script_path,
        repo,
        {
            "AGENT_NAME": "TestAgent",
            "WORKTREES_ENABLED": "1",
            "AGENT_MAIL_GUARD_MODE": "warn",
        },
    )

    # Should exit 0 in advisory mode (warn but don't block)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_guard_bypass_flag(isolated_env, tmp_path: Path):
    """Test AGENT_MAIL_BYPASS=1 bypasses all checks."""
    settings = get_settings()
    archive = await ensure_archive(settings, "bypass-test")

    # Add conflicting reservation
    await write_file_reservation_record(
        archive,
        {
            "agent": "OtherAgent",
            "path_pattern": "*.py",
            "exclusive": True,
        },
    )

    script = render_precommit_script(archive)
    script_path = tmp_path / "guard.py"
    script_path.write_text(script, encoding="utf-8")

    # Create repo with conflicting file
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "app.py").write_text("print('hello')", encoding="utf-8")
    _git_add(repo, "app.py")

    # Run with bypass enabled
    result = _run_hook(
        script_path,
        repo,
        {
            "AGENT_NAME": "TestAgent",
            "WORKTREES_ENABLED": "1",
            "AGENT_MAIL_BYPASS": "1",
        },
    )

    # Should bypass all checks
    assert result.returncode == 0
    assert "bypass" in result.stderr.lower()


# =============================================================================
# Pre-push Guard Tests
# =============================================================================


@pytest.mark.asyncio
async def test_prepush_guard_install(isolated_env, tmp_path: Path):
    """Test pre-push guard installation."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Install pre-push guard
    hook_path = await install_prepush_guard(settings, "prepush-test", repo)

    assert hook_path.exists()
    assert "pre-push" in hook_path.name


@pytest.mark.asyncio
async def test_prepush_script_generation(isolated_env, tmp_path: Path):
    """Test pre-push script includes STDIN handling."""
    settings = get_settings()
    archive = await ensure_archive(settings, "prepush-test")

    script = render_prepush_script(archive)

    # Should have pre-push specific handling
    assert "pre-push" in script
    assert "stdin" in script.lower() or "STDIN" in script


# =============================================================================
# Uninstall Tests
# =============================================================================


@pytest.mark.asyncio
async def test_guard_uninstall(isolated_env, tmp_path: Path):
    """Test guard uninstall removes hooks properly."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Install guard
    await install_guard(settings, "uninstall-test", repo)

    # Uninstall
    removed = await uninstall_guard(repo)

    assert removed is True


@pytest.mark.asyncio
async def test_guard_uninstall_nonexistent(isolated_env, tmp_path: Path):
    """Test uninstall on repo without guard returns False."""
    # Create repo without guard
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Uninstall (nothing to remove)
    removed = await uninstall_guard(repo)

    assert removed is False


# =============================================================================
# Chain Runner Tests
# =============================================================================


@pytest.mark.asyncio
async def test_chain_runner_executes_plugins(isolated_env, tmp_path: Path):
    """Test chain runner executes plugins in hooks.d directory."""
    settings = get_settings()

    # Create repo
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _init_git_repo(repo)

    # Install guard (creates chain runner)
    hook_path = await install_guard(settings, "chain-test", repo)

    # Create additional plugin in hooks.d
    hooks_d = hook_path.parent / "hooks.d" / "pre-commit"
    hooks_d.mkdir(parents=True, exist_ok=True)

    # Plugin that creates a marker file
    plugin = hooks_d / "99-test-plugin.py"
    marker_file = tmp_path / "plugin_ran.txt"
    plugin.write_text(
        f"#!/usr/bin/env python3\n"
        f"from pathlib import Path\n"
        f"Path({str(marker_file)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
        newline="\n",
    )
    plugin.chmod(0o755)

    # Stage a file
    (repo / "test.txt").write_text("test", encoding="utf-8")
    _git_add(repo, "test.txt")

    # Run chain runner
    _run_hook(hook_path, repo, {"AGENT_NAME": "TestAgent", "WORKTREES_ENABLED": "1"})

    # Plugin should have run
    assert marker_file.exists()
    assert marker_file.read_text(encoding="utf-8") == "ran"


# =============================================================================
# Windows chain-runner dispatch
# =============================================================================


class _RecordingRun:
    """Record subprocess argv and input instead of spawning a Windows child."""

    def __init__(self, git_exec_path: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._git_exec_path = git_exec_path

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        normalized = [str(value) for value in argv]
        if normalized[:2] == ["git", "--exec-path"]:
            return subprocess.CompletedProcess(
                normalized,
                0,
                stdout=self._git_exec_path,
                stderr="",
            )
        self.calls.append((normalized, kwargs))
        return subprocess.CompletedProcess(normalized, 0, stdout="", stderr="")


def _exec_chain_runner(hook_path: Path, script_text: str, *, os_name: str) -> None:
    """Execute a rendered runner with only its imported os.name simulated."""
    exec_globals: dict[str, Any] = {"__file__": str(hook_path), "__name__": "__main__"}
    os_shim = types.SimpleNamespace(name=os_name)
    real_import = builtins.__import__

    def _import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "os":
            return os_shim
        return real_import(name, *args, **kwargs)

    exec_globals["__builtins__"] = {**vars(builtins), "__import__": _import}
    with pytest.raises(SystemExit) as exc_info:
        exec(compile(script_text, str(hook_path), "exec"), exec_globals)
    assert exc_info.value.code in (0, None)


def _write_windows_dispatch_layout(tmp_path: Path) -> Path:
    """Create Windows runner children covering Python and shell dispatch."""
    hooks = tmp_path / "hooks"
    run_dir = hooks / "hooks.d" / "pre-commit"
    run_dir.mkdir(parents=True)
    (run_dir / "10-plugin.py").write_text("print('python')\n", encoding="utf-8")
    (run_dir / "20-python-script").write_text(
        "#!/usr/bin/env python3\nprint('python shebang')\n",
        encoding="utf-8",
    )
    (run_dir / "30-native.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    (hooks / "pre-commit.orig").write_text(
        "#!/usr/bin/env sh\necho original\n",
        encoding="utf-8",
    )
    return hooks


def test_chain_runner_windows_dispatches_shebang_children(monkeypatch, tmp_path: Path):
    """Windows must not pass shell or Python shebang scripts bare to CreateProcess."""
    from mcp_agent_mail.guard import _render_chain_runner_script

    hooks = _write_windows_dispatch_layout(tmp_path)
    hook_path = hooks / "pre-commit"
    recorder = _RecordingRun()
    monkeypatch.setattr(subprocess, "run", recorder)
    monkeypatch.setattr(shutil, "which", lambda _command: "C:/Git/usr/bin/sh.exe")
    monkeypatch.setattr(sys, "argv", [str(hook_path), "hook-arg"])

    _exec_chain_runner(hook_path, _render_chain_runner_script("pre-commit"), os_name="nt")

    calls = [argv for argv, _kwargs in recorder.calls]
    python_file = next(argv for argv in calls if "10-plugin.py" in argv[1])
    python_shebang = next(argv for argv in calls if "20-python-script" in argv[1])
    native_cmd = next(argv for argv in calls if "30-native.cmd" in argv[0])
    shell_orig = next(
        argv for argv in calls if any(value.endswith("pre-commit.orig") for value in argv)
    )
    assert python_file == [sys.executable, str(hooks / "hooks.d/pre-commit/10-plugin.py"), "hook-arg"]
    assert python_shebang == [
        sys.executable,
        str(hooks / "hooks.d/pre-commit/20-python-script"),
        "hook-arg",
    ]
    assert native_cmd == [str(hooks / "hooks.d/pre-commit/30-native.cmd"), "hook-arg"]
    shell_orig_path = str(hooks / "pre-commit.orig").replace("\\", "/")
    assert shell_orig == ["C:/Git/usr/bin/sh.exe", shell_orig_path, "hook-arg"]


def test_chain_runner_windows_resolves_git_bundled_sh(monkeypatch, tmp_path: Path):
    """The runner finds Git for Windows' sh.exe when PATH has no shell."""
    from mcp_agent_mail.guard import _render_chain_runner_script

    hooks = _write_windows_dispatch_layout(tmp_path)
    hook_path = hooks / "pre-commit"
    git_root = tmp_path / "Git"
    exec_path = git_root / "mingw64/libexec/git-core"
    exec_path.mkdir(parents=True)
    bundled_sh = git_root / "usr/bin/sh.exe"
    bundled_sh.parent.mkdir(parents=True)
    bundled_sh.write_text("", encoding="utf-8")
    recorder = _RecordingRun(git_exec_path=str(exec_path))
    monkeypatch.setattr(subprocess, "run", recorder)
    monkeypatch.setattr(shutil, "which", lambda _command: None)
    monkeypatch.setattr(sys, "argv", [str(hook_path)])

    _exec_chain_runner(hook_path, _render_chain_runner_script("pre-commit"), os_name="nt")

    orig_call = next(
        argv
        for argv, _kwargs in recorder.calls
        if any(value.endswith("pre-commit.orig") for value in argv)
    )
    assert orig_call[0] == str(bundled_sh)


@pytest.mark.parametrize(
    ("hook_name", "hook_args", "stdin_bytes"),
    [
        ("pre-commit", ["commit-arg"], None),
        ("pre-push", ["origin", "ssh://example.invalid/repo.git"], b"ref tuple\n"),
    ],
)
def test_chain_runner_windows_husky_uses_real_hook_name(
    monkeypatch,
    tmp_path: Path,
    hook_name: str,
    hook_args: list[str],
    stdin_bytes: bytes | None,
):
    """Windows Husky uses Git sh, a slash-safe argv0, and preserves hook I/O."""
    from mcp_agent_mail.guard import _render_chain_runner_script

    # WindowsPath supplies real backslashes. On POSIX, use one literal
    # backslash in a component so the simulated Windows branch still proves
    # that only sh-bound paths are normalized.
    hooks = (
        tmp_path / ".husky/_"
        if os.name == "nt"
        else tmp_path / "C:\\repo" / ".husky/_"
    )
    hooks.mkdir(parents=True)
    (hooks / "h").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    (hooks / f"{hook_name}.orig").write_text(
        '#!/usr/bin/env sh\n. "$(dirname "$0")/h"\n',
        encoding="utf-8",
    )
    hook_path = hooks / hook_name
    recorder = _RecordingRun()
    monkeypatch.setattr(subprocess, "run", recorder)
    monkeypatch.setattr(shutil, "which", lambda _command: "C:/Git/usr/bin/sh.exe")
    monkeypatch.setattr(sys, "argv", [str(hook_path), *hook_args])
    if stdin_bytes is not None:
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(stdin_bytes)))

    script = _render_chain_runner_script(hook_name)
    _exec_chain_runner(hook_path, script, os_name="nt")

    assert len(recorder.calls) == 1
    argv, kwargs = recorder.calls[0]
    assert argv[:3] == [
        "C:/Git/usr/bin/sh.exe",
        "-c",
        'husky_h="$1"; shift; . "$husky_h"',
    ]
    assert argv[3].endswith(f"/{hook_name}")
    assert not argv[3].endswith(".orig")
    assert argv[4].endswith("/h")
    assert "\\" not in argv[3]
    assert "\\" not in argv[4]
    assert argv[5:] == hook_args
    assert kwargs["input"] == stdin_bytes
