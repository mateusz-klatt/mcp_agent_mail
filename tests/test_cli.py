import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zipfile import ZipFile

import pytest
from git.cmd import Git
from sqlalchemy import select
from sqlalchemy.sql import ColumnElement
from typer.testing import CliRunner

from mcp_agent_mail import cli as cli_module, share as share_module
from mcp_agent_mail.cli import app
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, FileReservation, MessageDelivery, Project
from mcp_agent_mail.storage import (
    _commit as _archive_commit,
    commit_archive_subtree_deletion,
    ensure_archive,
)
from tests.keys import pkey

LIB_SH = Path(__file__).resolve().parents[1] / "scripts" / "lib.sh"


def _bash_executable() -> str:
    discovered = shutil.which("bash")
    if os.name != "nt":
        return discovered or "bash"
    git = shutil.which("git")
    if git:
        for git_root in Path(git).resolve().parents:
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


def _git_bash_path(path: str | Path) -> str:
    value = str(path)
    if os.name != "nt":
        return value
    normalized = value.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        return f"/{normalized[0].lower()}{normalized[2:]}"
    return normalized


def _seed_cli_agent_state(
    state_dir: Path,
    *,
    project: str,
    old_name: str,
    registration_token: str,
) -> None:
    granted_dir = state_dir / "granted"
    granted_dir.mkdir(parents=True)
    (state_dir / "credentials.json").write_text(
        json.dumps({project: {old_name: registration_token}}) + "\n",
        encoding="utf-8",
    )
    legacy_granted = granted_dir / cli_module._agent_state_previous_component(project)
    legacy_granted.write_text(old_name, encoding="utf-8")


def _path_tree(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    paths = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*")))
    contents = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    return paths, contents


def test_copy_bundle_contents_rejects_owned_source_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    viewer = source / "viewer"
    viewer.mkdir(parents=True)
    destination.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"PRIVATE_SYMLINK_CANARY")
    link = viewer / "index.html"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    old_manifest = b'{"old": true}\n'
    (destination / "manifest.json").write_bytes(old_manifest)

    with pytest.raises(
        cli_module.ShareExportError,
        match="Refusing to follow source bundle link",
    ):
        cli_module._copy_bundle_contents(source, destination)

    assert (destination / "manifest.json").read_bytes() == old_manifest
    assert not (destination / "viewer" / "index.html").exists()


def test_copy_bundle_contents_rejects_owned_destination_symlink(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source_viewer = source / "viewer"
    source_viewer.mkdir(parents=True)
    (source_viewer / "index.html").write_bytes(b"fresh viewer")
    destination.mkdir()
    external_viewer = tmp_path / "external-viewer"
    external_viewer.mkdir()
    canary = external_viewer / "index.html"
    canary.write_bytes(b"PRIVATE_DESTINATION_LINK_CANARY")
    destination_viewer = destination / "viewer"
    try:
        destination_viewer.symlink_to(external_viewer, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(
        cli_module.ShareExportError,
        match="Refusing to follow destination bundle link",
    ):
        cli_module._copy_bundle_contents(source, destination)

    assert canary.read_bytes() == b"PRIVATE_DESTINATION_LINK_CANARY"


def test_copy_bundle_contents_rejects_destination_root_symlink(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_bytes(b'{"fresh": true}\n')
    destination_target = tmp_path / "destination-target"
    destination_target.mkdir()
    canary = destination_target / "custom.txt"
    canary.write_bytes(b"PRIVATE_ROOT_LINK_CANARY")
    destination = tmp_path / "destination"
    try:
        destination.symlink_to(destination_target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(
        cli_module.ShareExportError,
        match="Destination bundle root must be a real directory",
    ):
        cli_module._copy_bundle_contents(source, destination)

    assert canary.read_bytes() == b"PRIVATE_ROOT_LINK_CANARY"
    assert not (destination_target / "manifest.json").exists()


def test_copy_bundle_contents_publishes_manifest_after_assets_and_pruning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / "viewer").mkdir(parents=True)
    (destination / "chunks").mkdir(parents=True)
    (source / "viewer" / "index.html").write_bytes(b"fresh viewer")
    (source / "manifest.json").write_bytes(b'{"fresh": true}\n')
    stale_chunk = destination / "chunks" / "stale.bin"
    stale_chunk.write_bytes(b"stale")
    (destination / "manifest.json").write_bytes(b'{"old": true}\n')

    events: list[str] = []
    original_copy2 = shutil.copy2
    original_unlink = Path.unlink

    def observed_copy2(source_path: Path, destination_path: Path) -> str:
        events.append(f"copy:{Path(destination_path).relative_to(destination).as_posix()}")
        return str(original_copy2(source_path, destination_path))

    def observed_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.is_relative_to(destination):
            events.append(f"unlink:{path.relative_to(destination).as_posix()}")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(shutil, "copy2", observed_copy2)
    monkeypatch.setattr(Path, "unlink", observed_unlink)

    cli_module._copy_bundle_contents(source, destination)

    assert events == [
        "copy:viewer/index.html",
        "unlink:chunks/stale.bin",
        "copy:manifest.json",
    ]


def test_write_directory_to_zip_rejects_storage_symlink(
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    (storage_root / "safe.txt").write_bytes(b"safe")
    external_secret = tmp_path / "external-secret.txt"
    external_secret.write_bytes(b"PRIVATE_ARCHIVE_SYMLINK_CANARY")
    link = storage_root / "linked-secret.txt"
    try:
        link.symlink_to(external_secret)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    archive_path = tmp_path / "archive.zip"

    with ZipFile(archive_path, "w") as archive, pytest.raises(
        cli_module.ShareExportError,
        match="Recovery archive refuses storage link",
    ):
        cli_module._write_directory_to_zip(
            archive,
            storage_root,
            Path("storage_repo"),
        )

    with ZipFile(archive_path) as archive:
        archived_payload = b"".join(archive.read(name) for name in archive.namelist())
    assert b"PRIVATE_ARCHIVE_SYMLINK_CANARY" not in archived_payload


def test_share_update_preserves_host_repo_and_zips_only_fresh_owned_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    manifest = {
        "export_config": {
            "projects": ["demo"],
            "inline_threshold": 64,
            "detach_threshold": 1024,
            "chunk_threshold": 2048,
            "chunk_size": 1024,
            "scrub_preset": "standard",
        },
        "project_scope": {"requested": ["demo"]},
        "attachments": {
            "config": {"inline_threshold": 64, "detach_threshold": 1024}
        },
        "scrub": {"preset": "standard"},
        "database": {},
    }
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    preserved = {
        ".git/config": b"https://operator:PRIVATE_GIT_TOKEN_CANARY@example.invalid/repo\n",
        ".github/workflows/pages.yml": b"name: PRIVATE_WORKFLOW_CANARY\n",
        "CNAME": b"mail.example.invalid\n",
        "custom.txt": b"PRIVATE_CUSTOM_CANARY\n",
    }
    for relative, content in preserved.items():
        target = bundle_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    stale_owned = (
        bundle_dir / "chunks" / "stale.bin",
        bundle_dir / "attachments" / "stale.bin",
        bundle_dir / "viewer" / "stale.js",
    )
    for path in stale_owned:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stale")

    database_path = tmp_path / "mailbox.sqlite3"
    database_path.write_bytes(b"source database")

    def fake_create_snapshot_context(
        *,
        snapshot_path: Path,
        **_kwargs: object,
    ) -> SimpleNamespace:
        snapshot_path.write_bytes(b"fresh snapshot")
        return SimpleNamespace(
            snapshot_path=snapshot_path,
            scope=SimpleNamespace(
                projects=[SimpleNamespace(slug="demo")],
                removed_count=0,
            ),
            scrub_summary=SimpleNamespace(
                preset="standard",
                agents_total=1,
                agents_pseudonymized=0,
                secrets_replaced=0,
                bodies_redacted=0,
            ),
            fts_enabled=False,
        )

    def fake_build_bundle_assets(
        _snapshot_path: Path,
        output_dir: Path,
        **_kwargs: object,
    ) -> SimpleNamespace:
        fresh_manifest = dict(manifest)
        fresh_manifest["database"] = {
            "chunk_manifest": {"chunk_count": 1, "chunk_size": 1024}
        }
        (output_dir / "manifest.json").write_text(
            json.dumps(fresh_manifest),
            encoding="utf-8",
        )
        for relative, content in {
            "viewer/index.html": b"fresh viewer",
            "chunks/00000.bin": b"fresh chunk",
            "attachments/aa/fresh.bin": b"fresh attachment",
            "README.md": b"fresh readme\n",
            "unexpected-private.tmp": b"PRIVATE_TEMP_CANARY",
        }.items():
            target = output_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return SimpleNamespace(
            attachments_manifest={
                "stats": {
                    "inline": 0,
                    "copied": 1,
                    "externalized": 0,
                    "missing": 0,
                }
            },
            chunk_manifest={"chunk_count": 1, "chunk_size": 1024},
            viewer_data=None,
        )

    monkeypatch.setattr(
        cli_module,
        "resolve_sqlite_database_path",
        lambda: database_path,
    )
    monkeypatch.setattr(
        cli_module,
        "create_snapshot_context",
        fake_create_snapshot_context,
    )
    monkeypatch.setattr(
        cli_module,
        "build_bundle_assets",
        fake_build_bundle_assets,
    )
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: SimpleNamespace(storage=SimpleNamespace(root=str(tmp_path / "storage"))),
    )
    monkeypatch.setattr(cli_module, "detect_hosting_hints", lambda _path: [])

    result = CliRunner().invoke(
        cli_module.app,
        ["share", "update", str(bundle_dir), "--zip"],
    )

    assert result.exit_code == 0, result.output
    for relative, content in preserved.items():
        assert (bundle_dir / relative).read_bytes() == content
    assert all(not path.exists() for path in stale_owned)
    assert (bundle_dir / "chunks" / "00000.bin").read_bytes() == b"fresh chunk"
    assert not (bundle_dir / "unexpected-private.tmp").exists()

    archive_path = bundle_dir.with_suffix(".zip")
    with ZipFile(archive_path) as archive:
        archived_names = set(archive.namelist())
        archived_payload = b"".join(archive.read(name) for name in archive.namelist())
    assert "manifest.json" in archived_names
    assert "viewer/index.html" in archived_names
    assert "chunks/00000.bin" in archived_names
    assert "attachments/aa/fresh.bin" in archived_names
    assert not any(name.startswith(".git/") for name in archived_names)
    assert not any(name.startswith(".github/") for name in archived_names)
    assert "CNAME" not in archived_names
    assert "custom.txt" not in archived_names
    assert "unexpected-private.tmp" not in archived_names
    for canary in (
        b"PRIVATE_GIT_TOKEN_CANARY",
        b"PRIVATE_WORKFLOW_CANARY",
        b"PRIVATE_CUSTOM_CANARY",
        b"PRIVATE_TEMP_CANARY",
    ):
        assert canary not in archived_payload


def _init_projects_adopt_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "adopt-repo"
    source_worktree = repo_root / "legacy-worktree"
    target_worktree = repo_root / "canonical-worktree"
    source_worktree.mkdir(parents=True, exist_ok=True)
    target_worktree.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_root), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_root), check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(repo_root), check=True)
    (repo_root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_root), check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo_root),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return source_worktree, target_worktree


def _seed_projects_adopt_state(source_worktree: Path, target_worktree: Path) -> tuple[Path, Path, Path, int]:
    async def _seed() -> tuple[Path, Path, Path, int]:
        await ensure_schema()
        async with get_session() as session:
            source_project = Project(slug="legacy", human_key=str(source_worktree))
            target_project = Project(slug="canonical", human_key=str(target_worktree))
            session.add(source_project)
            session.add(target_project)
            await session.commit()
            await session.refresh(source_project)
            await session.refresh(target_project)
            assert source_project.id is not None
            assert target_project.id is not None
            session.add(
                Agent(
                    project_id=source_project.id,
                    name="BlueLake",
                    program="codex",
                    model="gpt-5",
                    task_description="legacy agent",
                )
            )
            await session.commit()

        settings = get_settings()
        source_archive = await ensure_archive(settings, "legacy")
        target_archive = await ensure_archive(settings, "canonical")
        source_artifact = source_archive.root / "messages" / "legacy-note.md"
        source_artifact.parent.mkdir(parents=True, exist_ok=True)
        source_artifact.write_text("legacy artifact\n", encoding="utf-8")
        await _archive_commit(
            source_archive.repo,
            settings,
            "seed: legacy artifact",
            [source_artifact.relative_to(source_archive.repo_root).as_posix()],
        )
        return source_archive.root, target_archive.root, source_archive.repo_root, target_project.id

    return asyncio.run(_seed())


def _seed_hard_delete_cli_state(
    *,
    project_key: str,
    project_slug: str,
    agent_name: str,
    registration_token: str,
    agent_id: int | None = None,
) -> tuple[Path, Path]:
    async def _seed() -> tuple[Path, Path]:
        await ensure_schema()
        async with get_session() as session:
            project = Project(slug=project_slug, human_key=project_key)
            session.add(project)
            await session.commit()
            await session.refresh(project)
            assert project.id is not None
            session.add(
                Agent(
                    id=agent_id,
                    project_id=project.id,
                    name=agent_name,
                    program="codex",
                    model="gpt-5",
                    task_description="hard-delete test",
                    registration_token=registration_token,
                )
            )
            await session.commit()

        archive = await ensure_archive(get_settings(), project_slug)
        profile = archive.root / "agents" / agent_name / "profile.json"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text('{"name": "BlueLake"}\n', encoding="utf-8")
        await _archive_commit(
            archive.repo,
            archive.settings,
            "seed: hard-delete CLI agent",
            [profile.relative_to(archive.repo_root).as_posix()],
            use_queue=False,
        )
        return archive.repo_root, archive.root

    return asyncio.run(_seed())


def _git_output(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_commit_archive_subtree_deletion_rejects_resolved_escape(isolated_env) -> None:
    async def reject_escape() -> None:
        archive = await ensure_archive(get_settings(), "containment-project")
        escaped_target = archive.root / "agents" / ".." / ".." / "outside"
        with pytest.raises(ValueError, match="stay within"):
            await commit_archive_subtree_deletion(
                archive,
                escaped_target,
                "test: reject escaped deletion target",
            )

    asyncio.run(reject_escape())


def test_cli_lint(monkeypatch):
    runner = CliRunner()
    captured: list[list[str]] = []

    def fake_run(command: list[str]) -> None:
        captured.append(command)

    monkeypatch.setattr("mcp_agent_mail.cli._run_command", fake_run)
    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 0
    assert captured == [["ruff", "check", "--fix", "--unsafe-fixes"]]


def test_cli_typecheck(monkeypatch):
    runner = CliRunner()
    captured: list[list[str]] = []

    def fake_run(command: list[str]) -> None:
        captured.append(command)

    monkeypatch.setattr("mcp_agent_mail.cli._run_command", fake_run)
    result = runner.invoke(app, ["typecheck"])
    assert result.exit_code == 0
    assert captured == [["uvx", "ty", "check"]]


def test_shared_env_writer_rejects_non_absolute_state_without_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".agent-mail.env"
    original = "FOREIGN_SETTING=preserved\nHTTP_BEARER_TOKEN=old-secret\n"
    env_file.write_text(original, encoding="utf-8", newline="\n")
    original_bytes = env_file.read_bytes()
    monkeypatch.setenv("AGENT_MAIL_ENV_FILE", str(env_file))
    monkeypatch.setenv("AGENT_MAIL_URL", "https://mail.example/mcp/")
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "writer-regression-secret")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("AGENT_MAIL_TEST_LIB", _git_bash_path(LIB_SH))
    shell = r"""
. "$AGENT_MAIL_TEST_LIB"
init_colors
cygpath() { printf '%s\n' "$1"; }
wslpath() { printf '%s\n' "$1"; }
write_shared_agent_mail_env "$AGENT_MAIL_URL" "$HTTP_BEARER_TOKEN"
"""

    for state_dir in ("relative/state", r"D:\Profiles\agent-mail-state"):
        monkeypatch.setenv("AGENT_MAIL_STATE_DIR", state_dir)
        result = subprocess.run(
            [BASH, "--noprofile", "--norc", "-c", shell],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert "absolute path" in result.stderr
        assert "writer-regression-secret" not in result.stdout + result.stderr
        assert env_file.read_text(encoding="utf-8") == original
        assert _path_tree(tmp_path) == (
            (".agent-mail.env",),
            {".agent-mail.env": original_bytes},
        )


def test_shared_env_writer_translates_windows_state_before_persisting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".agent-mail.env"
    env_file.write_text("FOREIGN_SETTING=preserved\n", encoding="utf-8", newline="\n")
    normalized_state = tmp_path / "private-state"
    normalized_state_for_shell = _git_bash_path(normalized_state)
    monkeypatch.setenv("AGENT_MAIL_ENV_FILE", str(env_file))
    monkeypatch.setenv("AGENT_MAIL_STATE_DIR", r"D:\Profiles\agent-mail-state")
    monkeypatch.setenv("AGENT_MAIL_URL", "https://mail.example/mcp/")
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "translated-writer-secret")
    monkeypatch.setenv("AGENT_MAIL_TEST_NORMALIZED_STATE", normalized_state_for_shell)
    monkeypatch.setenv("AGENT_MAIL_TEST_LIB", _git_bash_path(LIB_SH))
    monkeypatch.setenv("NO_COLOR", "1")
    shell = r"""
. "$AGENT_MAIL_TEST_LIB"
init_colors
cygpath() { printf '%s\n' "$AGENT_MAIL_TEST_NORMALIZED_STATE"; }
wslpath() { printf '%s\n' "$AGENT_MAIL_TEST_NORMALIZED_STATE"; }
write_shared_agent_mail_env "$AGENT_MAIL_URL" "$HTTP_BEARER_TOKEN"
"""

    result = subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", shell],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    persisted = env_file.read_text(encoding="utf-8")
    assert "FOREIGN_SETTING=preserved\n" in persisted
    assert f"AGENT_MAIL_STATE_DIR={normalized_state_for_shell}\n" in persisted
    assert "D:\\Profiles" not in persisted
    assert "translated-writer-secret" not in result.stdout + result.stderr
    assert list((normalized_state / "backups").glob("*.bak"))


def test_migrate_agent_state_uses_global_hook_env_not_repo_env(
    isolated_env,
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = pkey("cli-state/global")
    old_name = "home-wsl-1"
    new_name = "codex-wsl-home-1"
    registration_token = "global-state-registration-secret"
    state_dir = tmp_path / "custom-global-state"
    _seed_cli_agent_state(
        state_dir,
        project=project,
        old_name=old_name,
        registration_token=registration_token,
    )
    global_env = tmp_path / "home" / ".agent-mail.env"
    global_env.parent.mkdir()
    global_env.write_text(
        f"AGENT_MAIL_STATE_DIR={state_dir}\n",
        encoding="utf-8",
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".env").write_text(
        f"AGENT_MAIL_STATE_DIR={tmp_path / 'wrong-repo-state'}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("HOME", str(global_env.parent))
    monkeypatch.setenv("USERPROFILE", str(global_env.parent))
    monkeypatch.delenv("AGENT_MAIL_STATE_DIR", raising=False)
    monkeypatch.delenv("AGENT_MAIL_ENV_FILE", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "migrate-agent-state",
            project,
            old_name,
            new_name,
            "--client",
            "codex",
            "--slot",
            "1",
        ],
        terminal_width=240,
    )

    assert result.exit_code == 0, result.output
    assert "credential_state=pending" in result.output
    assert registration_token not in result.output
    assert not (tmp_path / "wrong-repo-state").exists()


def test_migrate_agent_state_rejects_relative_global_state_before_mutation(
    isolated_env,
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = pkey("cli-state/relative")
    old_name = "home-wsl-1"
    new_name = "codex-wsl-home-1"
    registration_token = "relative-state-registration-secret"
    explicit_state = tmp_path / "explicit-state"
    _seed_cli_agent_state(
        explicit_state,
        project=project,
        old_name=old_name,
        registration_token=registration_token,
    )
    global_env = tmp_path / ".agent-mail.env"
    global_env.write_text(
        "AGENT_MAIL_STATE_DIR=relative-state\n",
        encoding="utf-8",
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("AGENT_MAIL_STATE_DIR", raising=False)
    monkeypatch.delenv("AGENT_MAIL_ENV_FILE", raising=False)
    before = _path_tree(tmp_path)

    rejected = CliRunner().invoke(
        app,
        [
            "migrate-agent-state",
            project,
            old_name,
            new_name,
            "--client",
            "codex",
            "--slot",
            "1",
            "--apply",
            "--confirm",
            f"{old_name}=>{new_name}",
        ],
    )

    assert rejected.exit_code != 0
    assert "AGENT_MAIL_STATE_DIR must be an absolute path" in rejected.output
    assert registration_token not in rejected.output
    assert _path_tree(tmp_path) == before
    assert not (checkout / "relative-state").exists()

    explicit = CliRunner().invoke(
        app,
        [
            "migrate-agent-state",
            project,
            old_name,
            new_name,
            "--client",
            "codex",
            "--slot",
            "1",
            "--state-dir",
            str(explicit_state),
        ],
    )
    assert explicit.exit_code == 0, explicit.output
    assert registration_token not in explicit.output


def test_projects_adopt_apply_moves_archive_state_and_keeps_archive_git_clean(isolated_env, tmp_path):
    runner = CliRunner()
    source_worktree, target_worktree = _init_projects_adopt_repo(tmp_path)
    source_root, target_root, archive_repo_root, target_project_id = _seed_projects_adopt_state(source_worktree, target_worktree)

    result = runner.invoke(app, ["projects", "adopt", "legacy", "canonical", "--apply"])

    assert result.exit_code == 0
    assert "Adoption apply completed." in result.stdout
    assert not (source_root / "messages" / "legacy-note.md").exists()
    assert (target_root / "messages" / "legacy-note.md").exists()
    aliases = json.loads((target_root / "aliases.json").read_text(encoding="utf-8"))
    assert aliases["former_slugs"] == ["legacy"]

    async def _verify() -> int:
        async with get_session() as session:
            agent = (
                await session.execute(
                    select(Agent).where(cast(ColumnElement[bool], Agent.name == "BlueLake"))
                )
            ).scalars().one()
            return agent.project_id

    assert asyncio.run(_verify()) == target_project_id

    archive_status = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(archive_repo_root),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert archive_status == ""

    source_ls = subprocess.run(
        ["git", "ls-files", "--", "projects/legacy/messages/legacy-note.md"],
        cwd=str(archive_repo_root),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    target_ls = subprocess.run(
        ["git", "ls-files", "--", "projects/canonical/messages/legacy-note.md"],
        cwd=str(archive_repo_root),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert source_ls == ""
    assert target_ls == "projects/canonical/messages/legacy-note.md"


def test_projects_adopt_move_commit_holds_both_archive_locks(isolated_env, tmp_path, monkeypatch):
    runner = CliRunner()
    source_worktree, target_worktree = _init_projects_adopt_repo(tmp_path)
    _seed_projects_adopt_state(source_worktree, target_worktree)

    active_locks: set[str] = set()
    observed_lock_sets: list[set[str]] = []
    original_archive_write_lock = cli_module.archive_write_lock
    original_git_call_process = Git._call_process

    @asynccontextmanager
    async def tracking_archive_write_lock(archive, *args, **kwargs):
        async with original_archive_write_lock(archive, *args, **kwargs):
            active_locks.add(archive.slug)
            try:
                yield
            finally:
                active_locks.remove(archive.slug)

    def tracking_git_call_process(self, method, *args, **kwargs):
        if method == "commit" and "adopt: move legacy into canonical" in args:
            observed_lock_sets.append(set(active_locks))
        return original_git_call_process(self, method, *args, **kwargs)

    monkeypatch.setattr("mcp_agent_mail.cli.archive_write_lock", tracking_archive_write_lock)
    monkeypatch.setattr(Git, "_call_process", tracking_git_call_process)

    result = runner.invoke(app, ["projects", "adopt", "legacy", "canonical", "--apply"])

    assert result.exit_code == 0
    assert observed_lock_sets == [{"legacy", "canonical"}]


def test_projects_adopt_refuses_immutable_delivery_history_before_mutation(
    isolated_env,
    tmp_path,
) -> None:
    runner = CliRunner()
    source_worktree, target_worktree = _init_projects_adopt_repo(tmp_path)
    source_root, target_root, _, _ = _seed_projects_adopt_state(
        source_worktree,
        target_worktree,
    )

    async def _seed_delivery() -> tuple[str, int]:
        async with get_session() as session:
            source_project = (
                await session.execute(
                    select(Project).where(
                        cast(ColumnElement[bool], Project.slug == "legacy")
                    )
                )
            ).scalars().one()
            sender = (
                await session.execute(
                    select(Agent).where(
                        cast(
                            ColumnElement[bool],
                            Agent.project_id == source_project.id,
                        ),
                        cast(ColumnElement[bool], Agent.name == "BlueLake"),
                    )
                )
            ).scalars().one()
            assert source_project.id is not None
            assert sender.id is not None
            document = "immutable delivery\n"
            delivery = MessageDelivery(
                project_id=source_project.id,
                project_slug_snapshot=source_project.slug,
                project_generation_snapshot=source_project.project_generation,
                sender_project_id_snapshot=source_project.id,
                sender_project_slug_snapshot=source_project.slug,
                sender_project_generation_snapshot=source_project.project_generation,
                sender_id=sender.id,
                sender_name_snapshot=sender.name,
                sender_generation_snapshot=sender.agent_generation,
                actor_kind="system",
                actor_id=0,
                actor_name_snapshot="system",
                idempotency_key="adopt-refusal",
                request_sha256="1" * 64,
                subject="Immutable",
                body_md="Immutable",
                archive_document=document,
                document_sha256=hashlib.sha256(document.encode()).hexdigest(),
            )
            session.add(delivery)
            await session.commit()
            return delivery.id, source_project.id

    delivery_id, source_project_id = asyncio.run(_seed_delivery())
    pending_path = source_root / "message_deliveries" / ".pending" / delivery_id
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text("attempt\n", encoding="utf-8")

    result = runner.invoke(app, ["projects", "adopt", "legacy", "canonical", "--apply"])

    assert result.exit_code != 0
    assert "immutable message delivery history" in result.output
    assert (source_root / "messages" / "legacy-note.md").read_text(
        encoding="utf-8"
    ) == "legacy artifact\n"
    assert not (target_root / "messages" / "legacy-note.md").exists()
    assert pending_path.read_text(encoding="utf-8") == "attempt\n"

    async def _verify_unchanged() -> tuple[int, int]:
        async with get_session() as session:
            delivery_count = (
                await session.execute(
                    select(MessageDelivery).where(
                        cast(ColumnElement[bool], MessageDelivery.id == delivery_id)
                    )
                )
            ).scalars().all()
            sender = (
                await session.execute(
                    select(Agent).where(
                        cast(ColumnElement[bool], Agent.name == "BlueLake")
                    )
                )
            ).scalars().one()
            return len(delivery_count), sender.project_id

    assert asyncio.run(_verify_unchanged()) == (1, source_project_id)


def test_cli_serve_http_uses_settings(isolated_env, monkeypatch):
    runner = CliRunner()
    call_args: dict[str, Any] = {}

    def fake_uvicorn_run(
        app,
        host,
        port,
        log_level="info",
        forwarded_allow_ips="127.0.0.1",
    ):
        call_args["app"] = app
        call_args["host"] = host
        call_args["port"] = port
        call_args["log_level"] = log_level
        call_args["forwarded_allow_ips"] = forwarded_allow_ips

    monkeypatch.setenv("HTTP_FORWARDED_ALLOW_IPS", "172.19.0.1")
    clear_settings_cache()
    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    result = runner.invoke(app, ["serve-http"])
    assert result.exit_code == 0
    assert call_args["host"] == "127.0.0.1"
    assert call_args["port"] == 8765
    assert call_args["forwarded_allow_ips"] == "172.19.0.1"


def test_cli_config_set_port_clears_cached_settings(tmp_path, monkeypatch):
    runner = CliRunner()
    env_path = tmp_path / ".env"
    env_path.write_text("HTTP_HOST=127.0.0.1\nHTTP_PORT=1111\nHTTP_PATH=/api/\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HTTP_HOST", raising=False)
    monkeypatch.delenv("HTTP_PORT", raising=False)
    monkeypatch.delenv("HTTP_PATH", raising=False)
    clear_settings_cache()

    show_before = runner.invoke(app, ["config", "show-port"])
    assert show_before.exit_code == 0
    assert "1111" in show_before.stdout

    set_result = runner.invoke(app, ["config", "set-port", "2222"])
    assert set_result.exit_code == 0

    show_after = runner.invoke(app, ["config", "show-port"])
    assert show_after.exit_code == 0
    assert "2222" in show_after.stdout


def test_cli_serve_stdio(isolated_env, monkeypatch):
    """Test that serve-stdio invokes FastMCP.run with stdio transport."""
    runner = CliRunner()
    call_args: dict[str, Any] = {}

    def fake_run(self, transport="stdio", **kwargs):
        call_args["transport"] = transport
        call_args["kwargs"] = kwargs

    # Patch FastMCP.run on the class before build_mcp_server returns an instance
    from fastmcp import FastMCP

    monkeypatch.setattr(FastMCP, "run", fake_run)
    result = runner.invoke(app, ["serve-stdio"])
    assert result.exit_code == 0
    assert call_args["transport"] == "stdio"


def test_cli_migrate(monkeypatch):
    runner = CliRunner()
    invoked: dict[str, bool] = {"called": False}

    async def fake_migrate(settings):
        invoked["called"] = True

    monkeypatch.setattr("mcp_agent_mail.cli.ensure_schema", fake_migrate)
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0
    assert invoked["called"] is True


def test_cli_list_projects(isolated_env):
    runner = CliRunner()

    async def seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            project = Project(slug="demo", human_key="Demo")
            session.add(project)
            await session.commit()
            await session.refresh(project)
            assert project.id is not None
            session.add(
                Agent(
                    project_id=project.id,
                    name="BlueLake",
                    program="codex",
                    model="gpt-5",
                    task_description="",
                )
            )
            await session.commit()

    asyncio.run(seed())
    result = runner.invoke(app, ["list-projects", "--include-agents"])
    assert result.exit_code == 0
    assert "demo" in result.stdout
    assert "BlueLake" not in result.stdout


def test_cli_list_projects_json_returns_structured_error_on_failure(monkeypatch):
    runner = CliRunner()

    async def failing_ensure_schema(_settings=None) -> None:
        raise RuntimeError("projects exploded")

    monkeypatch.setattr("mcp_agent_mail.cli.ensure_schema", failing_ensure_schema)

    result = runner.invoke(app, ["list-projects", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": "projects exploded"}


def test_cli_hard_delete_agent_commits_archive_deletion(isolated_env):
    project_key = pkey("cli/delete-agent")
    project_slug = "cli-delete-agent"
    agent_name = "BlueLake"
    registration_token = "agent-delete-token"
    repo_root, project_root = _seed_hard_delete_cli_state(
        project_key=project_key,
        project_slug=project_slug,
        agent_name=agent_name,
        registration_token=registration_token,
        agent_id=17,
    )
    agent_relpath = f"projects/{project_slug}/agents/{agent_name}"

    async def seed_reservations() -> tuple[
        int,
        int,
        list[Path],
        list[Path],
        list[Path],
    ]:
        async with get_session() as session:
            project_record = (
                await session.execute(
                    select(Project).where(
                        cast(ColumnElement[bool], Project.human_key == project_key)
                    )
                )
            ).scalars().one()
            target_agent = (
                await session.execute(
                    select(Agent).where(
                        cast(ColumnElement[bool], Agent.project_id == project_record.id),
                        cast(ColumnElement[bool], Agent.name == agent_name),
                    )
                )
            ).scalars().one()
            assert project_record.id is not None
            assert target_agent.id is not None
            unrelated_agent = Agent(
                project_id=project_record.id,
                name="GreenCastle",
                program="codex",
                model="gpt-5",
                task_description="unrelated hard-delete reservation",
                registration_token="unrelated-token",
            )
            session.add(unrelated_agent)
            await session.commit()
            await session.refresh(unrelated_agent)
            assert unrelated_agent.id is not None
            expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
            reservations = [
                FileReservation(
                    project_id=project_record.id,
                    agent_id=target_agent.id,
                    path_pattern="src/target-a.py",
                    expires_ts=expires_at,
                ),
                FileReservation(
                    project_id=project_record.id,
                    agent_id=target_agent.id,
                    path_pattern="src/target-b.py",
                    expires_ts=expires_at,
                ),
                FileReservation(
                    project_id=project_record.id,
                    agent_id=unrelated_agent.id,
                    path_pattern="src/unrelated.py",
                    expires_ts=expires_at,
                ),
            ]
            session.add_all(reservations)
            await session.commit()
            for reservation in reservations:
                await session.refresh(reservation)
                assert reservation.id is not None

        archive = await ensure_archive(get_settings(), project_slug)
        reservations_dir = archive.root / "file_reservations"
        reservations_dir.mkdir(parents=True, exist_ok=True)
        target_artifacts: list[Path] = []
        unrelated_artifacts: list[Path] = []
        mismatched_artifacts: list[Path] = []
        rel_paths: list[str] = []
        for reservation in reservations:
            payload = {
                "id": reservation.id,
                "agent": (
                    agent_name
                    if reservation.agent_id == target_agent.id
                    else unrelated_agent.name
                ),
                "agent_id": reservation.agent_id,
                "path_pattern": reservation.path_pattern,
            }
            digest = hashlib.sha1(
                reservation.path_pattern.encode("utf-8"),
                usedforsecurity=False,
            ).hexdigest()
            artifact_paths = [
                reservations_dir / f"{digest}.json",
                reservations_dir / f"id-{reservation.id}.json",
            ]
            for artifact_path in artifact_paths:
                artifact_path.write_text(
                    json.dumps(payload, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                rel_paths.append(artifact_path.relative_to(repo_root).as_posix())
            if reservation.agent_id == target_agent.id:
                target_artifacts.extend(artifact_paths)
            else:
                unrelated_artifacts.extend(artifact_paths)
        legacy_target_artifact = reservations_dir / "legacy-target.json"
        legacy_target_artifact.write_text(
            json.dumps(
                {
                    "agent": agent_name,
                    "path_pattern": "src/legacy-target.py",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        target_artifacts.append(legacy_target_artifact)
        rel_paths.append(legacy_target_artifact.relative_to(repo_root).as_posix())
        for index, invalid_agent_id in enumerate((17.9, "17", True), start=1):
            mismatched_artifact = reservations_dir / f"mismatched-agent-id-{index}.json"
            mismatched_artifact.write_text(
                json.dumps(
                    {
                        "agent": agent_name,
                        "agent_id": invalid_agent_id,
                        "path_pattern": f"src/mismatched-{index}.py",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            mismatched_artifacts.append(mismatched_artifact)
            rel_paths.append(mismatched_artifact.relative_to(repo_root).as_posix())
        await _archive_commit(
            archive.repo,
            archive.settings,
            "seed: hard-delete reservation artifacts",
            rel_paths,
            use_queue=False,
        )
        return (
            target_agent.id,
            unrelated_agent.id,
            target_artifacts,
            unrelated_artifacts,
            mismatched_artifacts,
        )

    (
        target_agent_id,
        unrelated_agent_id,
        target_artifacts,
        unrelated_artifacts,
        mismatched_artifacts,
    ) = asyncio.run(seed_reservations())
    assert target_agent_id == 17
    foreign_path = repo_root / "operator-note.txt"
    foreign_path.write_text("keep staged\n", encoding="utf-8")
    _git_output(repo_root, "add", "--", foreign_path.name)

    result = CliRunner().invoke(
        app,
        [
            "hard-delete-agent",
            project_key,
            agent_name,
            "--confirm",
            "I UNDERSTAND",
            "--token",
            registration_token,
        ],
    )

    async def load_reservations() -> tuple[
        Agent | None,
        list[FileReservation],
        list[FileReservation],
    ]:
        async with get_session() as session:
            deleted_agent = await session.get(Agent, target_agent_id)
            target_rows = (
                await session.execute(
                    select(FileReservation).where(
                        cast(
                            ColumnElement[bool],
                            FileReservation.agent_id == target_agent_id,
                        )
                    )
                )
            ).scalars().all()
            unrelated_rows = (
                await session.execute(
                    select(FileReservation).where(
                        cast(
                            ColumnElement[bool],
                            FileReservation.agent_id == unrelated_agent_id,
                        )
                    )
                )
            ).scalars().all()
            return deleted_agent, list(target_rows), list(unrelated_rows)

    deleted_agent, target_rows, unrelated_rows = asyncio.run(load_reservations())
    committed_paths = _git_output(
        repo_root,
        "show",
        "--format=",
        "--name-only",
        "HEAD",
    ).splitlines()

    assert result.exit_code == 0, result.output
    assert deleted_agent is None
    assert target_rows == []
    assert len(unrelated_rows) == 1
    assert not (project_root / "agents" / agent_name).exists()
    assert all(not path.exists() for path in target_artifacts)
    assert all(path.is_file() for path in unrelated_artifacts)
    assert all(path.is_file() for path in mismatched_artifacts)
    assert _git_output(repo_root, "ls-files", "--", agent_relpath) == ""
    assert foreign_path.name not in committed_paths
    assert all(
        path.relative_to(repo_root).as_posix() in committed_paths
        for path in target_artifacts
    )
    assert all(
        path.relative_to(repo_root).as_posix() not in committed_paths
        for path in unrelated_artifacts
    )
    assert all(
        path.relative_to(repo_root).as_posix() not in committed_paths
        for path in mismatched_artifacts
    )
    assert _git_output(repo_root, "diff", "--cached", "--name-only") == foreign_path.name
    assert _git_output(repo_root, "status", "--porcelain").splitlines() == [
        f"A  {foreign_path.name}"
    ]


def test_cli_hard_delete_project_commits_archive_deletion(isolated_env):
    project_key = pkey("cli/delete-project")
    registration_token = "project-delete-token"
    repo_root, project_root = _seed_hard_delete_cli_state(
        project_key=project_key,
        project_slug="cli-delete-project",
        agent_name="BlueLake",
        registration_token=registration_token,
    )
    project_relpath = "projects/cli-delete-project"

    result = CliRunner().invoke(
        app,
        [
            "hard-delete-project",
            project_key,
            "--confirm",
            "I UNDERSTAND",
            "--token",
            registration_token,
        ],
    )

    assert result.exit_code == 0, result.output
    assert not project_root.exists()
    assert _git_output(repo_root, "ls-files", "--", project_relpath) == ""
    assert _git_output(repo_root, "status", "--porcelain") == ""


def test_cli_hard_delete_project_refuses_immutable_delivery_before_mutation(
    isolated_env,
) -> None:
    project_key = pkey("cli/delete-project-with-delivery")
    registration_token = "project-delete-token"
    _, project_root = _seed_hard_delete_cli_state(
        project_key=project_key,
        project_slug="cli-delete-project-with-delivery",
        agent_name="BlueLake",
        registration_token=registration_token,
    )

    async def _seed_delivery() -> str:
        async with get_session() as session:
            project = (
                await session.execute(
                    select(Project).where(
                        cast(ColumnElement[bool], Project.human_key == project_key)
                    )
                )
            ).scalars().one()
            sender = (
                await session.execute(
                    select(Agent).where(
                        cast(ColumnElement[bool], Agent.project_id == project.id),
                        cast(ColumnElement[bool], Agent.name == "BlueLake"),
                    )
                )
            ).scalars().one()
            assert project.id is not None
            assert sender.id is not None
            document = "immutable CLI delivery\n"
            delivery = MessageDelivery(
                project_id=project.id,
                project_slug_snapshot=project.slug,
                project_generation_snapshot=project.project_generation,
                sender_project_id_snapshot=project.id,
                sender_project_slug_snapshot=project.slug,
                sender_project_generation_snapshot=project.project_generation,
                sender_id=sender.id,
                sender_name_snapshot=sender.name,
                sender_generation_snapshot=sender.agent_generation,
                actor_kind="system",
                actor_id=0,
                actor_name_snapshot="system",
                idempotency_key="cli-hard-delete-refusal",
                request_sha256="1" * 64,
                subject="Immutable",
                body_md="Immutable",
                archive_document=document,
                document_sha256=hashlib.sha256(document.encode()).hexdigest(),
            )
            session.add(delivery)
            await session.commit()
            return delivery.id

    delivery_id = asyncio.run(_seed_delivery())
    profile = project_root / "agents" / "BlueLake" / "profile.json"
    profile_before = profile.read_bytes()

    result = CliRunner().invoke(
        app,
        [
            "hard-delete-project",
            project_key,
            "--confirm",
            "I UNDERSTAND",
            "--token",
            registration_token,
        ],
    )

    assert result.exit_code != 0
    assert "immutable message delivery history" in result.output
    assert profile.read_bytes() == profile_before

    async def _verify() -> tuple[str, int]:
        async with get_session() as session:
            delivery = await session.get(MessageDelivery, delivery_id)
            project = (
                await session.execute(
                    select(Project).where(
                        cast(ColumnElement[bool], Project.human_key == project_key)
                    )
                )
            ).scalars().one()
            assert delivery is not None
            assert project.id is not None
            return delivery.state, project.id

    state, project_id = asyncio.run(_verify())
    assert state == "pending"
    assert project_id > 0


def test_cli_hard_delete_project_reports_archive_setup_failure_after_db_delete(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_key = pkey("cli/delete-project-archive-failure")
    registration_token = "project-delete-token"
    _seed_hard_delete_cli_state(
        project_key=project_key,
        project_slug="cli-delete-project-archive-failure",
        agent_name="BlueLake",
        registration_token=registration_token,
    )

    async def fail_archive_setup(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("archive setup failed")

    monkeypatch.setattr(cli_module, "ensure_archive", fail_archive_setup)
    result = CliRunner().invoke(
        app,
        [
            "hard-delete-project",
            project_key,
            "--confirm",
            "I UNDERSTAND",
            "--token",
            registration_token,
        ],
    )

    async def project_was_deleted() -> bool:
        async with get_session() as session:
            project = (
                await session.execute(
                    select(Project).where(
                        cast(ColumnElement[bool], Project.human_key == project_key)
                    )
                )
            ).scalars().first()
            return project is None

    assert result.exit_code == 0, result.output
    assert "Filesystem warning" in result.output
    assert "archive setup failed" in result.output
    assert asyncio.run(project_was_deleted())


def test_cli_hard_delete_project_preserves_foreign_staged_changes(isolated_env) -> None:
    project_key = pkey("cli/delete-project-staged")
    registration_token = "project-delete-token"
    repo_root, project_root = _seed_hard_delete_cli_state(
        project_key=project_key,
        project_slug="cli-delete-project-staged",
        agent_name="BlueLake",
        registration_token=registration_token,
    )
    foreign_path = repo_root / "operator-note.txt"
    foreign_path.write_text("keep staged\n", encoding="utf-8")
    _git_output(repo_root, "add", "--", foreign_path.name)

    result = CliRunner().invoke(
        app,
        [
            "hard-delete-project",
            project_key,
            "--confirm",
            "I UNDERSTAND",
            "--token",
            registration_token,
        ],
    )

    committed_paths = _git_output(
        repo_root,
        "show",
        "--format=",
        "--name-only",
        "HEAD",
    ).splitlines()
    assert result.exit_code == 0, result.output
    assert not project_root.exists()
    assert foreign_path.name not in committed_paths
    assert any(path.startswith("projects/cli-delete-project-staged/") for path in committed_paths)
    assert _git_output(repo_root, "diff", "--cached", "--name-only") == foreign_path.name


@pytest.mark.parametrize("delete_kind", ["agent", "project"])
def test_cli_hard_delete_holds_archive_lock_through_subtree_commit(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
    delete_kind: str,
) -> None:
    project_key = pkey(f"cli/delete-{delete_kind}-locked")
    project_slug = f"cli-delete-{delete_kind}-locked"
    registration_token = "delete-token"
    _repo_root, _project_root = _seed_hard_delete_cli_state(
        project_key=project_key,
        project_slug=project_slug,
        agent_name="BlueLake",
        registration_token=registration_token,
    )
    if delete_kind == "agent":
        original_commit = cli_module.commit_archive_path_deletions
        commit_attribute = "commit_archive_path_deletions"
    else:
        original_commit = cli_module.commit_archive_subtree_deletion
        commit_attribute = "commit_archive_subtree_deletion"
    observed_lock_paths: list[Path] = []

    async def observe_lock(archive, paths, message):
        assert archive.lock_path.is_file()
        observed_lock_paths.append(archive.lock_path)
        return await original_commit(archive, paths, message)

    monkeypatch.setattr(cli_module, commit_attribute, observe_lock)
    command = [
        f"hard-delete-{delete_kind}",
        project_key,
    ]
    if delete_kind == "agent":
        command.append("BlueLake")
    command.extend(
        [
            "--confirm",
            "I UNDERSTAND",
            "--token",
            registration_token,
        ]
    )

    result = CliRunner().invoke(app, command)

    assert result.exit_code == 0, result.output
    assert len(observed_lock_paths) == 1


def test_cli_hard_delete_project_preserves_artifact_created_after_lock_release(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_key = pkey("cli/delete-project-late-artifact")
    project_slug = "cli-delete-project-late-artifact"
    registration_token = "project-delete-token"
    _repo_root, project_root = _seed_hard_delete_cli_state(
        project_key=project_key,
        project_slug=project_slug,
        agent_name="BlueLake",
        registration_token=registration_token,
    )
    original_lock = cli_module.archive_write_lock
    late_artifact = project_root / "late-writer.txt"

    @asynccontextmanager
    async def inject_after_release(archive, *args, **kwargs):
        async with original_lock(archive, *args, **kwargs):
            yield
        late_artifact.write_text("preserve me\n", encoding="utf-8")

    monkeypatch.setattr(cli_module, "archive_write_lock", inject_after_release)
    result = CliRunner().invoke(
        app,
        [
            "hard-delete-project",
            project_key,
            "--confirm",
            "I UNDERSTAND",
            "--token",
            registration_token,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Filesystem warning" in result.output
    assert late_artifact.read_text(encoding="utf-8") == "preserve me\n"


def test_archive_save_defaults_to_archive_preset(tmp_path, isolated_env, monkeypatch):
    runner = CliRunner()
    archive_path = tmp_path / "state.zip"
    archive_path.write_bytes(b"zip")
    captured: dict[str, Any] = {}

    def fake_archive(**kwargs):
        captured.update(kwargs)
        metadata = {"scrub_preset": kwargs["scrub_preset"], "projects_requested": list(kwargs["project_filters"])}
        return archive_path, metadata

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", fake_archive)
    result = runner.invoke(app, ["archive", "save"])
    assert result.exit_code == 0
    assert captured["scrub_preset"] == "archive"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_create_mailbox_archive_uses_private_posix_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    archive_dir = project_root / cli_module.ARCHIVE_DIR_NAME
    archive_dir.mkdir(mode=0o777)
    archive_dir.chmod(0o777)
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    (storage_root / "secret.txt").write_text(
        "PRIVATE_ARCHIVE_CANARY\n",
        encoding="utf-8",
    )
    database_path = tmp_path / "mailbox.sqlite3"
    database_path.write_bytes(b"database")

    scrub_summary = share_module.ScrubSummary(
        preset="archive",
        pseudonym_salt="archive",
        agents_total=0,
        agents_pseudonymized=0,
        ack_flags_cleared=0,
        recipients_cleared=0,
        file_reservations_removed=0,
        agent_links_removed=0,
        secrets_replaced=0,
        attachments_sanitized=0,
        bodies_redacted=0,
        attachments_cleared=0,
    )

    def fake_snapshot_context(
        *,
        snapshot_path: Path,
        **_kwargs: object,
    ) -> share_module.SnapshotContext:
        snapshot_path.write_bytes(b"lossless snapshot")
        return share_module.SnapshotContext(
            snapshot_path=snapshot_path,
            scope=share_module.ProjectScopeResult(projects=[], removed_count=0),
            scrub_summary=scrub_summary,
            fts_enabled=False,
        )

    monkeypatch.setattr(cli_module, "_detect_project_root", lambda: project_root)
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: SimpleNamespace(
            database=SimpleNamespace(url=f"sqlite+aiosqlite:///{database_path}"),
            storage=SimpleNamespace(root=str(storage_root)),
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "resolve_sqlite_database_path",
        lambda _url: database_path,
    )
    monkeypatch.setattr(
        cli_module,
        "create_snapshot_context",
        fake_snapshot_context,
    )

    archive_path, _metadata = cli_module._create_mailbox_archive(
        project_filters=(),
        scrub_preset="archive",
        label="permissions",
        status_message="",
    )

    assert archive_dir.stat().st_mode & 0o777 == 0o700
    assert archive_path.stat().st_mode & 0o777 == 0o600
    with ZipFile(archive_path) as archive:
        assert archive.read("storage_repo/secret.txt") == b"PRIVATE_ARCHIVE_CANARY\n"


def test_clear_and_reset_skips_archive_when_disabled(isolated_env, monkeypatch):
    runner = CliRunner()

    def _should_not_run(**_kwargs):  # pragma: no cover - defensive
        raise AssertionError("archive should not be invoked when --no-archive is supplied")

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", _should_not_run)
    result = runner.invoke(app, ["clear-and-reset-everything", "--force", "--no-archive"])
    assert result.exit_code == 0


def test_doctor_check_reports_stale_locks(isolated_env):
    runner = CliRunner()
    settings = get_settings()
    lock_path = Path(settings.storage.root) / "projects" / "backend" / ".archive.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")
    metadata_path = lock_path.parent / ".archive.lock.owner.json"
    metadata_path.write_text(
        json.dumps({"pid": 999999, "created_ts": time.time() - 3600}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "check", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    locks_diag = next(item for item in payload["diagnostics"] if item["name"] == "Locks")
    assert locks_diag["status"] == "warning"
    assert "stale" in locks_diag["message"].lower()


def test_doctor_check_detects_non_sqlite3_wal_files(tmp_path, monkeypatch):
    runner = CliRunner()
    db_path = tmp_path / "mail.db"
    wal_path = tmp_path / "mail.db-wal"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    clear_settings_cache()

    sqlite3.connect(db_path).close()
    wal_path.write_text("wal", encoding="utf-8")

    result = runner.invoke(app, ["doctor", "check", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    wal_diag = next(item for item in payload["diagnostics"] if item["name"] == "WAL Files")
    assert wal_diag["status"] == "info"
    assert "wal/shm file" in wal_diag["message"].lower()


def test_doctor_check_scopes_project_specific_findings(isolated_env):
    runner = CliRunner()

    async def seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            backend = Project(slug="backend", human_key=pkey("backend"))
            frontend = Project(slug="frontend", human_key=pkey("frontend"))
            session.add(backend)
            session.add(frontend)
            await session.commit()
            await session.refresh(backend)
            await session.refresh(frontend)
            assert backend.id is not None
            assert frontend.id is not None

            backend_agent = Agent(project_id=backend.id, name="BlueLake", program="codex", model="gpt-5", task_description="")
            frontend_agent = Agent(project_id=frontend.id, name="GreenCastle", program="codex", model="gpt-5", task_description="")
            session.add(backend_agent)
            session.add(frontend_agent)
            await session.commit()
            await session.refresh(backend_agent)
            await session.refresh(frontend_agent)
            assert backend_agent.id is not None
            assert frontend_agent.id is not None

            expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
            session.add(
                FileReservation(
                    project_id=backend.id,
                    agent_id=backend_agent.id,
                    path_pattern="src/backend.py",
                    expires_ts=expired_at,
                )
            )
            session.add(
                FileReservation(
                    project_id=frontend.id,
                    agent_id=frontend_agent.id,
                    path_pattern="src/frontend.py",
                    expires_ts=expired_at,
                )
            )
            await session.commit()

    asyncio.run(seed())

    settings = get_settings()
    for slug in ("backend", "frontend"):
        lock_path = Path(settings.storage.root) / "projects" / slug / ".archive.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("", encoding="utf-8")
        (lock_path.parent / ".archive.lock.owner.json").write_text(
            json.dumps({"pid": 999999, "created_ts": time.time() - 3600}),
            encoding="utf-8",
        )

    result = runner.invoke(app, ["doctor", "check", "Backend", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    locks_diag = next(item for item in payload["diagnostics"] if item["name"] == "Locks")
    reservations_diag = next(item for item in payload["diagnostics"] if item["name"] == "File Reservations")
    assert "1 stale lock" in locks_diag["message"].lower()
    assert "1 expired reservation" in reservations_diag["message"].lower()


def test_doctor_backups_json_returns_structured_error_on_failure(monkeypatch):
    runner = CliRunner()

    async def failing_list_backups(_settings) -> list[dict[str, Any]]:
        raise RuntimeError("backup listing exploded")

    monkeypatch.setattr("mcp_agent_mail.storage.list_backups", failing_list_backups)

    result = runner.invoke(app, ["doctor", "backups", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": "backup listing exploded"}


def test_doctor_repair_scopes_project_specific_repairs(isolated_env, monkeypatch):
    runner = CliRunner()

    async def seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            backend = Project(slug="backend", human_key=pkey("backend"))
            frontend = Project(slug="frontend", human_key=pkey("frontend"))
            session.add(backend)
            session.add(frontend)
            await session.commit()
            await session.refresh(backend)
            await session.refresh(frontend)
            assert backend.id is not None
            assert frontend.id is not None

            backend_agent = Agent(project_id=backend.id, name="BlueLake", program="codex", model="gpt-5", task_description="")
            frontend_agent = Agent(project_id=frontend.id, name="GreenCastle", program="codex", model="gpt-5", task_description="")
            session.add(backend_agent)
            session.add(frontend_agent)
            await session.commit()
            await session.refresh(backend_agent)
            await session.refresh(frontend_agent)
            assert backend_agent.id is not None
            assert frontend_agent.id is not None

            expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
            session.add(
                FileReservation(
                    project_id=backend.id,
                    agent_id=backend_agent.id,
                    path_pattern="src/backend.py",
                    expires_ts=expired_at,
                )
            )
            session.add(
                FileReservation(
                    project_id=frontend.id,
                    agent_id=frontend_agent.id,
                    path_pattern="src/frontend.py",
                    expires_ts=expired_at,
                )
            )
            await session.commit()

    async def fake_backup(*args, **kwargs):
        return Path("/tmp/fake-doctor-backup")

    asyncio.run(seed())
    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", fake_backup)

    settings = get_settings()
    backend_lock = Path(settings.storage.root) / "projects" / "backend" / ".archive.lock"
    backend_lock.parent.mkdir(parents=True, exist_ok=True)
    backend_lock.write_text("", encoding="utf-8")
    (backend_lock.parent / ".archive.lock.owner.json").write_text(
        json.dumps({"pid": 999999, "created_ts": time.time() - 3600}),
        encoding="utf-8",
    )
    frontend_lock = Path(settings.storage.root) / "projects" / "frontend" / ".archive.lock"
    frontend_lock.parent.mkdir(parents=True, exist_ok=True)
    frontend_lock.write_text("", encoding="utf-8")
    (frontend_lock.parent / ".archive.lock.owner.json").write_text(
        json.dumps({"pid": 999999, "created_ts": time.time() - 3600}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "repair", "Backend", "--yes"])
    assert result.exit_code == 0

    async def verify() -> tuple[list[FileReservation], list[FileReservation]]:
        async with get_session() as session:
            backend_rows = (
                await session.execute(
                    select(FileReservation)
                    .join(Project, cast(ColumnElement[bool], FileReservation.project_id == Project.id))
                    .where(cast(ColumnElement[bool], Project.slug == "backend"))
                )
            ).scalars().all()
            frontend_rows = (
                await session.execute(
                    select(FileReservation)
                    .join(Project, cast(ColumnElement[bool], FileReservation.project_id == Project.id))
                    .where(cast(ColumnElement[bool], Project.slug == "frontend"))
                )
            ).scalars().all()
            return list(backend_rows), list(frontend_rows)

    backend_rows, frontend_rows = asyncio.run(verify())
    assert backend_rows[0].released_ts is not None
    assert frontend_rows[0].released_ts is None
    assert backend_lock.exists() is False
    assert frontend_lock.exists() is True


def test_doctor_restore_creates_pre_restore_backup(tmp_path, monkeypatch):
    runner = CliRunner()
    current_archive = tmp_path / "current-archive"
    (current_archive / ".git").mkdir(parents=True)
    monkeypatch.setenv("STORAGE_ROOT", str(current_archive))
    clear_settings_cache()

    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "database.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "test",
            "database_path": "database.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    calls: dict[str, Any] = {}

    async def fake_create_backup(*args: Any, **kwargs: Any) -> Path:
        calls["reason"] = kwargs.get("reason")
        return tmp_path / "pre-restore-snapshot"

    async def fake_restore(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["restore_backup_path"] = args[1]
        calls["restore_dry_run"] = kwargs.get("dry_run")
        return {"database_restored": True, "bundles_restored": [], "errors": []}

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", fake_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.restore_from_backup", fake_restore)

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 0
    assert calls["reason"] == "pre-restore"
    assert calls["restore_backup_path"] == backup_path
    assert calls["restore_dry_run"] is False
    assert "Pre-restore backup:" in result.stdout


def test_doctor_restore_aborts_when_pre_restore_backup_fails(tmp_path, monkeypatch):
    runner = CliRunner()
    current_archive = tmp_path / "current-archive"
    (current_archive / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STORAGE_ROOT", str(current_archive))
    clear_settings_cache()

    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "database.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "test",
            "database_path": "database.sqlite3",
            "project_bundles": [],
            "storage_root": str(current_archive),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    async def failing_create_backup(*args: Any, **kwargs: Any) -> Path:
        raise RuntimeError("archive bundle failed")

    def should_not_restore(*args: Any, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - defensive
        raise AssertionError("restore should not proceed when pre-restore backup fails")

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", failing_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.restore_from_backup", should_not_restore)

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Restore failed" in result.stdout
    assert "archive bundle failed" in result.stdout


def test_doctor_restore_dry_run_skips_pre_restore_backup(tmp_path, monkeypatch):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "database.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "test",
            "database_path": "database.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    create_calls = 0
    restore_calls: list[bool | None] = []

    async def fake_create_backup(*args: Any, **kwargs: Any) -> Path:
        nonlocal create_calls
        create_calls += 1
        return tmp_path / "pre-restore-snapshot"

    async def fake_restore(*args: Any, **kwargs: Any) -> dict[str, Any]:
        restore_calls.append(kwargs.get("dry_run"))
        return {
            "database_restored": False,
            "bundles_restored": [],
            "errors": [],
            "would_restore_database": False,
            "would_restore_bundles": [],
        }

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", fake_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.restore_from_backup", fake_restore)

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--dry-run"])
    assert result.exit_code == 0
    assert create_calls == 0
    assert restore_calls == [True]


def test_doctor_restore_dry_run_exits_nonzero_when_preview_reports_errors(tmp_path, monkeypatch):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "database.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "dry-run-error",
            "database_path": "database.sqlite3",
            "project_bundles": [],
            "storage_root": str(tmp_path / "archive"),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://agent:mail@localhost:5432/mcp")
    clear_settings_cache()

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--dry-run"])

    assert result.exit_code == 1
    assert "Dry run found restore blockers" in result.stdout
    assert "does not use a SQLite database file" in result.stdout


def test_doctor_repair_aborts_when_backup_creation_fails(isolated_env, monkeypatch):
    runner = CliRunner()

    async def seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            backend = Project(slug="backend", human_key=pkey("backend"))
            session.add(backend)
            await session.commit()
            await session.refresh(backend)
            assert backend.id is not None

            backend_agent = Agent(
                project_id=backend.id,
                name="BlueLake",
                program="codex",
                model="gpt-5",
                task_description="",
            )
            session.add(backend_agent)
            await session.commit()
            await session.refresh(backend_agent)
            assert backend_agent.id is not None

            expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
            session.add(
                FileReservation(
                    project_id=backend.id,
                    agent_id=backend_agent.id,
                    path_pattern="src/backend.py",
                    expires_ts=expired_at,
                )
            )
            await session.commit()

    async def failing_backup(*args: Any, **kwargs: Any) -> Path:
        raise RuntimeError("backup disk offline")

    asyncio.run(seed())
    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", failing_backup)

    settings = get_settings()
    backend_lock = Path(settings.storage.root) / "projects" / "backend" / ".archive.lock"
    backend_lock.parent.mkdir(parents=True, exist_ok=True)
    backend_lock.write_text("", encoding="utf-8")
    (backend_lock.parent / ".archive.lock.owner.json").write_text(
        json.dumps({"pid": 999999, "created_ts": time.time() - 3600}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "repair", "Backend", "--yes"])
    assert result.exit_code == 1
    assert "Backup failed" in result.stdout

    async def verify() -> list[FileReservation]:
        async with get_session() as session:
            backend_rows = (
                await session.execute(
                    select(FileReservation)
                    .join(Project, cast(ColumnElement[bool], FileReservation.project_id == Project.id))
                    .where(cast(ColumnElement[bool], Project.slug == "backend"))
                )
            ).scalars().all()
            return list(backend_rows)

    backend_rows = asyncio.run(verify())
    assert backend_rows[0].released_ts is None
    assert backend_lock.exists() is True


def test_doctor_repair_exits_nonzero_when_repair_reports_errors(isolated_env, monkeypatch, tmp_path):
    runner = CliRunner()

    async def fake_create_backup(*args: Any, **kwargs: Any) -> Path:
        return tmp_path / "fake-doctor-backup"

    async def failing_heal_locks(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("archive lock cleanup exploded")

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", fake_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.heal_archive_locks", failing_heal_locks)

    result = runner.invoke(app, ["doctor", "repair", "--yes"])

    assert result.exit_code == 1
    assert "Lock healing failed" in result.stdout
    assert "Errors: 1" in result.stdout


def test_doctor_restore_rejects_malformed_manifest(tmp_path):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "manifest.json").write_text("{not-json", encoding="utf-8")

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Invalid backup manifest" in result.stdout


def test_doctor_restore_rejects_manifest_without_restore_payload(tmp_path):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "empty",
            "database_path": None,
            "project_bundles": [],
            "storage_root": "/tmp/archive",
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Invalid backup manifest" in result.stdout


def test_doctor_restore_rejects_manifest_artifact_outside_backup(tmp_path):
    runner = CliRunner()
    external_bundle = tmp_path / "external.bundle"
    external_bundle.write_text("bundle", encoding="utf-8")

    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "bad-paths",
            "database_path": None,
            "project_bundles": [str(external_bundle)],
            "storage_root": "/tmp/archive",
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Invalid backup manifest" in result.stdout


def test_doctor_restore_exits_nonzero_when_restore_reports_errors(tmp_path, monkeypatch):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    payload_dir = backup_path / "payload"
    payload_dir.mkdir()
    (payload_dir / "db-copy.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "restore-error",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": "/tmp/archive",
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    async def fake_create_backup(*args: Any, **kwargs: Any) -> Path:
        return tmp_path / "pre-restore-snapshot"

    async def fake_restore(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "database_restored": False,
            "bundles_restored": [],
            "errors": ["simulated restore failure"],
        }

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", fake_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.restore_from_backup", fake_restore)

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Restore completed with errors" in result.stdout
    assert "simulated restore failure" in result.stdout


def test_doctor_restore_skips_pre_restore_backup_on_empty_current_state(tmp_path, monkeypatch):
    runner = CliRunner()
    current_archive = tmp_path / "current-archive"
    current_archive.mkdir()
    current_db = tmp_path / "current-state" / "mail.db"
    monkeypatch.setenv("STORAGE_ROOT", str(current_archive))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{current_db}")
    clear_settings_cache()

    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "database.sqlite3").write_text("db", encoding="utf-8")
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "test",
            "database_path": "database.sqlite3",
            "project_bundles": [],
            "storage_root": str(current_archive),
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    def should_not_create_backup(*args: Any, **kwargs: Any) -> Path:  # pragma: no cover - defensive
        raise AssertionError("pre-restore backup should be skipped for empty current state")

    async def fake_restore(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"database_restored": True, "bundles_restored": [], "errors": []}

    monkeypatch.setattr("mcp_agent_mail.storage.create_diagnostic_backup", should_not_create_backup)
    monkeypatch.setattr("mcp_agent_mail.storage.restore_from_backup", fake_restore)

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 0
    assert "Pre-restore backup skipped" in result.stdout
    assert "Database restored" in result.stdout


def test_doctor_restore_rejects_manifest_with_missing_artifact(tmp_path):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "missing-db",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": "/tmp/archive",
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Invalid backup manifest" in result.stdout
    assert "references missing artifact" in result.stdout


def test_doctor_restore_rejects_manifest_directory_artifact(tmp_path):
    runner = CliRunner()
    backup_path = tmp_path / "restore-backup"
    backup_path.mkdir()
    (backup_path / "payload" / "db-copy.sqlite3").mkdir(parents=True, exist_ok=True)
    (backup_path / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "created_at": "2026-04-10T00:00:00+00:00",
            "reason": "dir-db",
            "database_path": "payload/db-copy.sqlite3",
            "project_bundles": [],
            "storage_root": "/tmp/archive",
            "restore_instructions": "test",
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "restore", str(backup_path), "--yes"])
    assert result.exit_code == 1
    assert "Invalid backup manifest" in result.stdout
    assert "artifact is not a file" in result.stdout
