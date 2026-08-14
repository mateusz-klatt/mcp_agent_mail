from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.guard import render_precommit_script
from mcp_agent_mail.storage import ensure_archive, write_file_reservation_record


def _init_git_repo(repo_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Configure dummy user to avoid git warnings
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_path), check=True)


def _stage_file(repo_path: Path, rel_path: str, content: str = "x") -> None:
    target = repo_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel_path], cwd=str(repo_path), check=True)


def _run_precommit(
    script_path: Path,
    repo_path: Path,
    agent_name: str,
    execution_id: str | None = None,
    ancestor_execution_ids: tuple[str, ...] = (),
    execution_enforcement_mode: str = "observe",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AGENT_NAME"] = agent_name
    if execution_id is None:
        env.pop("AGENT_EXECUTION_ID", None)
    else:
        env["AGENT_EXECUTION_ID"] = execution_id
    if ancestor_execution_ids:
        env["AGENT_EXECUTION_ANCESTOR_IDS"] = ",".join(ancestor_execution_ids)
    else:
        env.pop("AGENT_EXECUTION_ANCESTOR_IDS", None)
    # WORKTREES_ENABLED=1 is required for the guard to actually run (not exit early)
    env["WORKTREES_ENABLED"] = "1"
    env["AGENT_EXECUTION_ENFORCEMENT_MODE"] = execution_enforcement_mode
    # sys.executable, not "python" — see test_precommit_enforcement.py: the bare
    # name does not exist on macOS or on distributions that ship only python3.
    return subprocess.run([sys.executable, str(script_path)], cwd=str(repo_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.asyncio
async def test_precommit_no_conflict(isolated_env, tmp_path: Path):
    settings = get_settings()
    # Prepare project archive and render guard script
    archive = await ensure_archive(settings, "backend")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Create a separate code repo and stage a file
    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    _init_git_repo(code_repo)
    _stage_file(code_repo, "src/app.py")

    # No file reservations present -> should pass
    proc = _run_precommit(script_path, code_repo, agent_name="Alpha")
    assert proc.returncode == 0, proc.stderr


@pytest.mark.asyncio
async def test_precommit_conflict_detected(isolated_env, tmp_path: Path):
    settings = get_settings()
    # Prepare project archive and render guard script
    archive = await ensure_archive(settings, "backend")
    script_text = render_precommit_script(archive)
    script_path = tmp_path / "precommit.py"
    script_path.write_text(script_text, encoding="utf-8")

    # Write an active file reservation held by another agent
    await write_file_reservation_record(
        archive,
        {
            "agent": "Beta",
            "path_pattern": "src/app.py",
            # no expires_ts means treated as active by the guard script
        },
    )

    # Create a separate code repo and stage a file matching the reservation
    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True, exist_ok=True)
    _init_git_repo(code_repo)
    _stage_file(code_repo, "src/app.py")

    # AGENT_NAME is Alpha; reservation is held by Beta -> should block
    proc = _run_precommit(script_path, code_repo, agent_name="Alpha")
    assert proc.returncode == 1
    assert "Exclusive file_reservation conflicts detected" in (proc.stderr or "")


@pytest.mark.asyncio
async def test_precommit_ignores_exact_and_ancestor_claims_but_blocks_siblings(
    isolated_env,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    archive = await ensure_archive(settings, "backend")
    script_path = tmp_path / "precommit.py"
    script_path.write_text(render_precommit_script(archive), encoding="utf-8")
    owner_execution = "11111111-1111-4111-8111-111111111111"
    sibling_execution = "22222222-2222-4222-8222-222222222222"
    await write_file_reservation_record(
        archive,
        {
            "agent": "codex-wsl-home-1",
            "execution_id": owner_execution,
            "path_pattern": "src/app.py",
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True)
    _init_git_repo(code_repo)
    _stage_file(code_repo, "src/app.py")

    owner = _run_precommit(
        script_path,
        code_repo,
        agent_name="codex-wsl-home-1",
        execution_id=owner_execution,
    )
    assert owner.returncode == 0, owner.stderr

    child_execution = "77777777-7777-4777-8777-777777777777"
    child = _run_precommit(
        script_path,
        code_repo,
        agent_name="codex-wsl-home-1",
        execution_id=child_execution,
        ancestor_execution_ids=(owner_execution,),
    )
    assert child.returncode == 0, child.stderr

    sibling = _run_precommit(
        script_path,
        code_repo,
        agent_name="codex-wsl-home-1",
        execution_id=sibling_execution,
    )
    assert sibling.returncode == 1
    assert owner_execution in sibling.stderr


@pytest.mark.asyncio
async def test_precommit_resolves_execution_marker_and_env_takes_precedence(
    isolated_env,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    archive = await ensure_archive(settings, "backend")
    script_path = tmp_path / "precommit.py"
    script_path.write_text(render_precommit_script(archive), encoding="utf-8")
    owner_execution = "33333333-3333-4333-8333-333333333333"
    sibling_execution = "44444444-4444-4444-8444-444444444444"
    await write_file_reservation_record(
        archive,
        {
            "agent": "codex-wsl-home-1",
            "execution_id": owner_execution,
            "path_pattern": "src/app.py",
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True)
    _init_git_repo(code_repo)
    marker_result = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "agent-mail/execution-id",
        cwd=code_repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    marker_stdout, marker_stderr = await marker_result.communicate()
    assert marker_result.returncode == 0, marker_stderr.decode()
    marker = Path(marker_stdout.decode().strip())
    assert marker.is_absolute()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "execution_id": owner_execution,
                "status": "active",
                "kind": "session",
                "worktree_path": str(code_repo),
                "heartbeat_ts": datetime.now(timezone.utc).isoformat(),
                "ancestor_execution_ids": [],
            }
        ),
        encoding="utf-8",
    )
    _stage_file(code_repo, "src/app.py")

    marker_owner = _run_precommit(
        script_path,
        code_repo,
        agent_name="codex-wsl-home-1",
    )
    assert marker_owner.returncode == 0, marker_owner.stderr

    explicit_sibling = _run_precommit(
        script_path,
        code_repo,
        agent_name="codex-wsl-home-1",
        execution_id=sibling_execution,
    )
    assert explicit_sibling.returncode == 1


@pytest.mark.asyncio
async def test_precommit_stale_marker_warns_in_observe_and_blocks_in_enforce(
    isolated_env,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    archive = await ensure_archive(settings, "backend")
    script_path = tmp_path / "precommit.py"
    script_path.write_text(render_precommit_script(archive), encoding="utf-8")

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True)
    _init_git_repo(code_repo)
    marker_process = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "agent-mail/execution-id",
        cwd=str(code_repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    marker_stdout, marker_stderr = await marker_process.communicate()
    assert marker_process.returncode == 0, marker_stderr.decode()
    marker = Path(marker_stdout.decode().strip())
    assert marker.is_absolute()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "execution_id": "88888888-8888-4888-8888-888888888888",
                "status": "active",
                "kind": "session",
                "worktree_path": str(code_repo),
                "heartbeat_ts": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                "ancestor_execution_ids": [],
            }
        ),
        encoding="utf-8",
    )
    _stage_file(code_repo, "src/app.py")

    result = _run_precommit(
        script_path,
        code_repo,
        agent_name="codex-wsl-home-1",
    )
    assert result.returncode == 0
    assert "observe:" in result.stderr
    assert "heartbeat is stale" in result.stderr

    enforced = _run_precommit(
        script_path,
        code_repo,
        agent_name="codex-wsl-home-1",
        execution_enforcement_mode="enforce",
    )
    assert enforced.returncode == 1
    assert "heartbeat is stale" in enforced.stderr


@pytest.mark.asyncio
async def test_precommit_reports_legacy_same_agent_claim_in_observe_and_blocks_in_enforce(
    isolated_env,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    archive = await ensure_archive(settings, "backend")
    script_path = tmp_path / "precommit.py"
    script_path.write_text(render_precommit_script(archive), encoding="utf-8")
    await write_file_reservation_record(
        archive,
        {
            "agent": "codex-wsl-home-1",
            "path_pattern": "src/app.py",
            "exclusive": True,
        },
    )

    code_repo = tmp_path / "code"
    code_repo.mkdir(parents=True)
    _init_git_repo(code_repo)
    _stage_file(code_repo, "src/app.py")

    observe_result = _run_precommit(
        script_path,
        code_repo,
        agent_name="codex-wsl-home-1",
    )
    assert observe_result.returncode == 0
    assert "active legacy_unscoped claim(s) owned by this Agent" in observe_result.stderr
    assert "legacy claim src/app.py expires" in observe_result.stderr

    enforce_result = _run_precommit(
        script_path,
        code_repo,
        agent_name="codex-wsl-home-1",
        execution_id="77777777-7777-4777-8777-777777777777",
        execution_enforcement_mode="enforce",
    )
    assert enforce_result.returncode == 1
    assert "<legacy-unscoped>" in enforce_result.stderr
