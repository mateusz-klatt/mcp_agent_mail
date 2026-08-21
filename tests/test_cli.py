import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zipfile import ZipFile

import pytest
from click.testing import Result
from git import Repo
from git.cmd import Git
from sqlalchemy import select
from sqlalchemy.sql import ColumnElement
from typer.testing import CliRunner

from mcp_agent_mail import cli as cli_module, share as share_module, storage as storage_module
from mcp_agent_mail.cli import app
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import (
    Agent,
    FileReservation,
    MessageDelivery,
    Project,
)
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


# ---------- shared scaffolding ----------

ADOPTED_AGENT = "BlueLake"


def _run_cli(*args: str) -> Result:
    """Invoke the CLI with a console wide enough that Rich never folds a line.

    Many assertions here quote a message the command is contractually required
    to print. Rich falls back to 80 columns when stdout is not a terminal,
    which is narrower than several of those messages, so a substring check run
    at the default width can pass or fail on where a line wrap happened to land
    instead of on whether the message was printed at all.
    """
    return CliRunner().invoke(app, list(args), env={"COLUMNS": "400"})


async def _insert_project(session: Any, *, slug: str, human_key: str) -> int:
    """Insert one project and return the id the database assigned it."""
    project = Project(slug=slug, human_key=human_key)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    project_id = project.id
    assert project_id is not None, f"project {slug!r} was inserted without an id"
    return project_id


async def _insert_agent(session: Any, project_id: int, name: str) -> int:
    """Insert one agent into ``project_id`` and return its assigned id."""
    agent = Agent(
        project_id=project_id,
        name=name,
        program="codex",
        model="gpt-5",
        task_description="",
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    agent_id = agent.id
    assert agent_id is not None, f"agent {name!r} was inserted without an id"
    return agent_id


def _must_not_be_called(name: str) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Return an async stand-in that fails loudly if the CLI ever reaches it.

    Used for the "this path must not happen" half of a contract. A plain
    recording spy cannot express that: it only proves what did happen, and a
    test that asserts a call count of zero still passes when the call is made
    somewhere the spy was never installed.
    """

    async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(f"{name} must not run on this path")

    return _refuse


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


# ---------- projects adopt ----------
#
# Adoption folds a legacy per-worktree project into a canonical one. Three
# things have to move together -- the archive files, the archive git index, and
# the database rows -- and the command refuses outright unless both project
# keys resolve to the same repository, so the fixtures below build exactly that
# shape and nothing more.

ADOPTED_ARTIFACT = "messages/legacy-note.md"
ADOPTED_ARTIFACT_BODY = "legacy artifact\n"
LEGACY_SLUG = "legacy"
CANONICAL_SLUG = "canonical"
# Quoted from cli.projects_adopt: the message it gives the archive move commit.
MOVE_COMMIT_MESSAGE = f"adopt: move {LEGACY_SLUG} into {CANONICAL_SLUG}"


def _one_repo_two_worktrees(tmp_path: Path) -> tuple[Path, Path]:
    """Return two project keys that share a single git repository.

    ``projects adopt`` compares ``git rev-parse --git-common-dir`` for the two
    keys and declines the pair when they differ, so both directories are placed
    inside one initialised repository rather than being separate repositories.
    """
    repo_root = tmp_path / "shared-repo"
    legacy_key = repo_root / "worktree-legacy"
    canonical_key = repo_root / "worktree-canonical"
    legacy_key.mkdir(parents=True)
    canonical_key.mkdir(parents=True)

    repo = Repo.init(repo_root)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Adopt Fixture")
        writer.set_value("user", "email", "adopt@example.invalid")
        writer.set_value("commit", "gpgsign", "false")
    (repo_root / "README.md").write_text("seed\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("seed the shared repository")
    return legacy_key, canonical_key


def _seed_adoptable_pair(legacy_key: Path, canonical_key: Path) -> SimpleNamespace:
    """Create both projects, one agent in the legacy one, one committed artifact.

    Returns the handles the adoption tests need to observe the outcome: both
    archive roots, the shared archive repository, the two archive lock paths,
    and the canonical project id the legacy agent is supposed to end up under.
    """

    async def _seed() -> SimpleNamespace:
        await ensure_schema()
        async with get_session() as session:
            legacy_id = await _insert_project(
                session, slug=LEGACY_SLUG, human_key=str(legacy_key)
            )
            canonical_id = await _insert_project(
                session, slug=CANONICAL_SLUG, human_key=str(canonical_key)
            )
            await _insert_agent(session, legacy_id, ADOPTED_AGENT)

        settings = get_settings()
        legacy_archive = await ensure_archive(settings, LEGACY_SLUG)
        canonical_archive = await ensure_archive(settings, CANONICAL_SLUG)
        artifact = legacy_archive.root / ADOPTED_ARTIFACT
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(ADOPTED_ARTIFACT_BODY, encoding="utf-8")
        await _archive_commit(
            legacy_archive.repo,
            settings,
            "seed: legacy artifact",
            [artifact.relative_to(legacy_archive.repo_root).as_posix()],
        )
        return SimpleNamespace(
            legacy_root=legacy_archive.root,
            canonical_root=canonical_archive.root,
            archive_repo=legacy_archive.repo,
            archive_repo_root=legacy_archive.repo_root,
            lock_paths={
                LEGACY_SLUG: Path(legacy_archive.lock_path),
                CANONICAL_SLUG: Path(canonical_archive.lock_path),
            },
            canonical_id=canonical_id,
        )

    return asyncio.run(_seed())


def _agent_project_id(name: str) -> int:
    """Return the project the named agent currently belongs to."""

    async def _read() -> int:
        async with get_session() as session:
            agent = (
                await session.execute(
                    select(Agent).where(cast(ColumnElement[bool], Agent.name == name))
                )
            ).scalars().one()
            return agent.project_id

    return asyncio.run(_read())


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


def test_adopt_apply_relocates_files_index_and_rows_leaving_no_uncommitted_work(
    isolated_env,
    tmp_path,
) -> None:
    """One --apply must land all four of adoption's effects, and land them fully.

    The archive move is only half-done if the file appears in the new tree
    while git still tracks it under the old path, so the index is inspected
    from both directions and the repository is required to come out clean.
    """
    legacy_key, canonical_key = _one_repo_two_worktrees(tmp_path)
    seeded = _seed_adoptable_pair(legacy_key, canonical_key)

    result = _run_cli("projects", "adopt", LEGACY_SLUG, CANONICAL_SLUG, "--apply")

    assert result.exit_code == 0, result.output
    assert "Adoption apply completed." in result.stdout

    # 1. the artifact is in the canonical tree and gone from the legacy one
    assert not (seeded.legacy_root / ADOPTED_ARTIFACT).exists()
    assert (seeded.canonical_root / ADOPTED_ARTIFACT).read_text(
        encoding="utf-8"
    ) == ADOPTED_ARTIFACT_BODY

    # 2. git agrees: tracked under the new path, untracked under the old one
    tracked = set(
        seeded.archive_repo.git.ls_files(
            "--", f"projects/{LEGACY_SLUG}", f"projects/{CANONICAL_SLUG}"
        ).splitlines()
    )
    assert f"projects/{CANONICAL_SLUG}/{ADOPTED_ARTIFACT}" in tracked
    assert f"projects/{LEGACY_SLUG}/{ADOPTED_ARTIFACT}" not in tracked

    # 3. nothing is left staged or dangling for a later run to trip over
    assert seeded.archive_repo.git.status("--short") == ""

    # 4. the old slug stays resolvable through the alias, and the agent row now
    #    points at the canonical project
    aliases = json.loads(
        (seeded.canonical_root / "aliases.json").read_text(encoding="utf-8")
    )
    assert aliases["former_slugs"] == [LEGACY_SLUG]
    assert _agent_project_id(ADOPTED_AGENT) == seeded.canonical_id


def test_adopt_holds_both_archive_locks_while_the_move_is_committed(
    isolated_env,
    tmp_path,
    monkeypatch,
) -> None:
    """Neither archive may be open to another writer while the move commits.

    Observed through the lock files themselves rather than through a wrapper
    around ``archive_write_lock``: the on-disk ``.archive.lock`` is what a
    second process actually contends on, so this remains a statement about
    mutual exclusion even if the way the command acquires locks is rewritten.
    """
    legacy_key, canonical_key = _one_repo_two_worktrees(tmp_path)
    seeded = _seed_adoptable_pair(legacy_key, canonical_key)
    locks_at_commit: list[set[str]] = []
    unpatched_call_process = Git._call_process

    def record_locks_then_run(self, method, *args, **kwargs):
        if method == "commit" and MOVE_COMMIT_MESSAGE in args:
            locks_at_commit.append(
                {slug for slug, path in seeded.lock_paths.items() if path.exists()}
            )
        return unpatched_call_process(self, method, *args, **kwargs)

    monkeypatch.setattr(Git, "_call_process", record_locks_then_run)

    result = _run_cli("projects", "adopt", LEGACY_SLUG, CANONICAL_SLUG, "--apply")

    assert result.exit_code == 0, result.output
    assert locks_at_commit == [{LEGACY_SLUG, CANONICAL_SLUG}]


def test_projects_adopt_refuses_immutable_delivery_history_before_mutation(
    isolated_env,
    tmp_path,
) -> None:
    legacy_key, canonical_key = _one_repo_two_worktrees(tmp_path)
    seeded = _seed_adoptable_pair(legacy_key, canonical_key)
    source_root = seeded.legacy_root
    target_root = seeded.canonical_root

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

    result = _run_cli("projects", "adopt", LEGACY_SLUG, CANONICAL_SLUG, "--apply")

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
        access_log=True,
        forwarded_allow_ips="127.0.0.1",
    ):
        call_args["app"] = app
        call_args["host"] = host
        call_args["port"] = port
        call_args["log_level"] = log_level
        call_args["access_log"] = access_log
        call_args["forwarded_allow_ips"] = forwarded_allow_ips

    monkeypatch.setenv("HTTP_FORWARDED_ALLOW_IPS", "172.19.0.1")
    clear_settings_cache()
    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    result = runner.invoke(app, ["serve-http"])
    assert result.exit_code == 0
    assert call_args["host"] == "127.0.0.1"
    assert call_args["port"] == 8765
    assert call_args["access_log"] is False
    assert call_args["forwarded_allow_ips"] == "172.19.0.1"


def test_config_set_port_is_visible_to_the_very_next_read(tmp_path, monkeypatch) -> None:
    """Rewriting .env is not enough: the cached Settings must be invalidated too.

    ``show-port`` reads through ``get_settings()``, which memoises. A ``set-port``
    that writes the file without clearing that cache leaves the running process
    reporting the old port -- the file and the answer disagree, and only a
    restart reconciles them.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HTTP_HOST=127.0.0.1\nHTTP_PORT=4101\nHTTP_PATH=/api/\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    for variable in ("HTTP_HOST", "HTTP_PORT", "HTTP_PATH"):
        monkeypatch.delenv(variable, raising=False)
    clear_settings_cache()

    before = _run_cli("config", "show-port")
    assert before.exit_code == 0, before.output
    assert "4101" in before.stdout

    changed = _run_cli("config", "set-port", "4202")
    assert changed.exit_code == 0, changed.output

    # the file is edited in place: the port line is replaced, the rest survives
    persisted = env_file.read_text(encoding="utf-8")
    assert "HTTP_PORT=4202" in persisted
    assert "HTTP_PORT=4101" not in persisted
    assert "HTTP_HOST=127.0.0.1" in persisted
    assert "HTTP_PATH=/api/" in persisted

    after = _run_cli("config", "show-port")
    assert after.exit_code == 0, after.output
    assert "4202" in after.stdout


def test_cli_serve_stdio(isolated_env, monkeypatch):
    """Test that serve-stdio invokes FastMCP.run with stdio transport."""
    runner = CliRunner()
    call_args: dict[str, Any] = {}
    root_logger = logging.getLogger()
    previous_handlers = tuple(root_logger.handlers)
    previous_level = root_logger.level

    def fake_run(self, transport="stdio", **kwargs):
        call_args["transport"] = transport
        call_args["kwargs"] = kwargs

    # Patch FastMCP.run on the class before build_mcp_server returns an instance
    from fastmcp import FastMCP

    monkeypatch.setattr(FastMCP, "run", fake_run)
    result = runner.invoke(app, ["serve-stdio"])
    assert result.exit_code == 0
    assert call_args["transport"] == "stdio"
    assert tuple(root_logger.handlers) == previous_handlers
    assert root_logger.level == previous_level


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
    async def seed() -> None:
        await ensure_schema()
        async with get_session() as session:
            project_id = await _insert_project(session, slug="demo", human_key="Demo")
            await _insert_agent(session, project_id, "BlueLake")

    asyncio.run(seed())
    result = _run_cli("list-projects", "--include-agents")
    assert result.exit_code == 0
    assert "demo" in result.stdout
    assert "BlueLake" not in result.stdout


@pytest.mark.parametrize(
    ("command", "failing_callable", "message"),
    [
        pytest.param(
            ("list-projects", "--json"),
            "ensure_schema",
            "projects exploded",
            id="list-projects",
        ),
        pytest.param(
            ("doctor", "backups", "--json"),
            "list_backups",
            "backup listing exploded",
            id="doctor-backups",
        ),
    ],
)
def test_json_mode_reports_a_failure_as_a_parseable_error_object(
    monkeypatch,
    command: tuple[str, ...],
    failing_callable: str,
    message: str,
) -> None:
    """--json output stays machine-readable on the failure path too.

    A caller that pipes these commands into a parser gets a traceback on stderr
    and a truncated document on stdout if the error escapes uncaught, so both
    the exit status and the shape of stdout are part of the contract. The key
    name ``error`` is what such a caller reads, so it is quoted exactly.
    """
    owner = cli_module if failing_callable == "ensure_schema" else storage_module

    async def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(message)

    monkeypatch.setattr(owner, failing_callable, explode)

    result = _run_cli(*command)

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": message}


@pytest.mark.parametrize("command", ["hard-delete-agent", "hard-delete-project"])
def test_cli_does_not_expose_irreversible_hard_delete_commands(command: str) -> None:
    result = CliRunner().invoke(app, [command])

    assert result.exit_code == 2
    assert "No such command" in result.output


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


# ---------- doctor: scaffolding ----------
#
# Each helper below builds only the *state* a doctor subcommand is supposed to
# notice. None of them assert anything: every expectation is written out in the
# test that holds it, so no helper can quietly soften a check for its callers.

RESERVED_PATH = "src/{slug}.py"


def _reaped_child_pid() -> int:
    """A pid that belonged to a real process and no longer belongs to any.

    Staleness is decided by asking ``pid_is_alive`` about the pid recorded in a
    lock's owner sidecar, so the fixture has to supply a pid that is genuinely
    gone. A large invented number is only *probably* absent -- it is a bet on
    the platform's pid ceiling -- whereas a child this process spawned and
    reaped provably ran and provably stopped. Computed at each call rather than
    cached, to keep the window before the pid is used as short as possible.
    """
    with subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as child:
        child.wait()
    return child.pid


def _abandon_archive_lock(
    storage_root: str | Path,
    slug: str,
    *,
    owner: str = "dead-owner",
) -> Path:
    """Leave an abandoned ``.archive.lock`` for ``slug``; return its resolved path.

    ``AsyncFileLock`` has two independent grounds for calling a lock stale and
    ``owner`` selects which one is on trial:

    ``dead-owner``  the sidecar names a process that has exited. Stale on the
                    owner alone, so ``created_ts`` is deliberately *fresh* --
                    age must not be what rescues the assertion.
    ``aged-out``    the sidecar names no process at all, leaving age as the only
                    available evidence, so the lock is older than the 180-second
                    stale timeout.
    """
    root = Path(storage_root).expanduser().resolve()
    lock_path = root / "projects" / slug / ".archive.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")
    now = datetime.now(UTC)
    if owner == "dead-owner":
        sidecar: dict[str, Any] = {
            "pid": _reaped_child_pid(),
            "created_ts": now.timestamp(),
        }
    else:
        sidecar = {"created_ts": (now - timedelta(days=1)).timestamp()}
    lock_path.with_name(f"{lock_path.name}.owner.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )
    return lock_path


def _seed_expired_reservations(*slugs: str) -> None:
    """Give each named project one agent holding one already-expired reservation."""

    async def _seed() -> None:
        await ensure_schema()
        # FileReservation stores naive UTC, so the comparison value must match.
        expired_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        async with get_session() as session:
            for slug in slugs:
                project_id = await _insert_project(
                    session, slug=slug, human_key=pkey(slug)
                )
                agent_id = await _insert_agent(
                    session, project_id, f"{slug.capitalize()}Holder"
                )
                session.add(
                    FileReservation(
                        project_id=project_id,
                        agent_id=agent_id,
                        path_pattern=RESERVED_PATH.format(slug=slug),
                        expires_ts=expired_at,
                    )
                )
            await session.commit()

    asyncio.run(_seed())


def _released_reservation_paths() -> set[str]:
    """Path patterns of every reservation the database now marks as released."""

    async def _read() -> set[str]:
        async with get_session() as session:
            rows = (await session.execute(select(FileReservation))).scalars().all()
            return {row.path_pattern for row in rows if row.released_ts is not None}

    return asyncio.run(_read())


def _diagnostic(stdout: str, name: str) -> dict[str, Any]:
    """Pull exactly one named entry out of ``doctor check --json`` output.

    Insisting on exactly one matters: a scoping bug that emitted a diagnostic
    per project instead of one aggregate would still satisfy a "find the first
    match" lookup, and the count is the only place that shows.
    """
    payload = json.loads(stdout)
    matches = [item for item in payload["diagnostics"] if item["name"] == name]
    assert len(matches) == 1, f"expected one {name!r} diagnostic, got {len(matches)}"
    return matches[0]


MANIFEST_DEFAULTS: dict[str, Any] = {
    "version": 1,
    "created_at": "2026-08-15T09:00:00+00:00",
    "reason": "cli-test",
    "restore_instructions": "am doctor restore <path>",
}


def _write_backup_dir(
    directory: Path,
    *,
    database_path: str | None = "database.sqlite3",
    materialise_database: bool = True,
    project_bundles: Sequence[str] = (),
    storage_root: str = "/srv/agent-mail-archive",
    **overrides: Any,
) -> Path:
    """Lay out a backup directory and the manifest describing it.

    Defaults produce a backup ``doctor restore`` accepts; each keyword removes
    one property so a test can put exactly one thing wrong at a time.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if database_path is not None and materialise_database:
        artifact = directory / database_path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"SQLite format 3\x00")
    manifest: dict[str, Any] = {
        **MANIFEST_DEFAULTS,
        "database_path": database_path,
        "project_bundles": list(project_bundles),
        "storage_root": storage_root,
        **overrides,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _completed_restore(**extra: Any) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    """A ``restore_from_backup`` stand-in reporting a clean, successful restore."""

    async def _restore(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "database_restored": True,
            "bundles_restored": [],
            "errors": [],
            **extra,
        }

    return _restore


# ---------- doctor check ----------


@pytest.mark.parametrize("owner", ["dead-owner", "aged-out"])
def test_doctor_check_reports_an_abandoned_archive_lock_and_names_it(
    isolated_env,
    owner: str,
) -> None:
    """A lock nobody is holding has to be surfaced, not silently tolerated.

    Both of the lock's independent staleness grounds are exercised, because a
    guard that only recognises one of them looks healthy from the other side.
    The path is asserted too: a warning that does not say *which* lock is stale
    cannot be acted on, and it is also what ``doctor repair`` goes on to remove.
    """
    lock_path = _abandon_archive_lock(
        get_settings().storage.root, "backend", owner=owner
    )

    result = _run_cli("doctor", "check", "--json")

    assert result.exit_code == 0, result.output
    locks = _diagnostic(result.stdout, "Locks")
    assert locks["status"] == "warning"
    assert "stale" in locks["message"].lower()
    assert locks["details"] == [str(lock_path)]
    assert locks["repair_available"] is True


def test_doctor_check_reports_no_stale_locks_when_the_lock_is_held(isolated_env) -> None:
    """The control for the test above: a live owner must not be reported stale.

    Without this, a check that answered "stale" unconditionally would pass the
    abandoned-lock test and hand every future operator a false alarm -- and the
    repair path would then delete a lock a running process still holds.
    """
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    lock_path = storage_root / "projects" / "backend" / ".archive.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")
    lock_path.with_name(f"{lock_path.name}.owner.json").write_text(
        json.dumps({"pid": os.getpid(), "created_ts": datetime.now(UTC).timestamp()}),
        encoding="utf-8",
    )

    result = _run_cli("doctor", "check", "--json")

    assert result.exit_code == 0, result.output
    locks = _diagnostic(result.stdout, "Locks")
    assert locks["status"] == "ok"
    assert locks["repair_available"] is False


def test_doctor_check_reports_sqlite_sidecar_files_as_information(
    isolated_env,
    tmp_path,
    monkeypatch,
) -> None:
    """A ``-wal`` companion is expected during operation, so it is info, not a fault.

    The check reports presence and names the files; it deliberately does not
    read them, so this plants a sidecar with contents no SQLite ever wrote and
    still expects it listed. Anything stronger would be asserting a validation
    the command does not perform.
    """
    database = tmp_path / "mail.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    clear_settings_cache()
    sqlite3.connect(database).close()
    wal = database.with_name(f"{database.name}-wal")
    wal.write_bytes(b"not anything a write-ahead log would contain")

    result = _run_cli("doctor", "check", "--json")

    assert result.exit_code == 0, result.output
    sidecars = _diagnostic(result.stdout, "WAL Files")
    assert sidecars["status"] == "info"
    assert "wal/shm file" in sidecars["message"].lower()
    assert str(wal) in sidecars["details"]


def test_doctor_check_counts_only_the_named_project(isolated_env) -> None:
    """Naming a project must narrow the findings, and narrow them to the right one.

    Run twice on purpose. The unscoped run is the control: without it, a command
    that had lost the filter entirely -- or one that reported a fixed count of
    one -- would still satisfy the scoped assertions, so the scoped numbers only
    mean something next to the numbers they are supposed to differ from.

    ``Backend`` is passed in a case the database does not hold, because the
    identifier is slugified before lookup and that is part of the interface.
    """
    _seed_expired_reservations("backend", "frontend")
    storage_root = get_settings().storage.root
    for slug in ("backend", "frontend"):
        _abandon_archive_lock(storage_root, slug)

    everything = _run_cli("doctor", "check", "--json")
    backend_only = _run_cli("doctor", "check", "Backend", "--json")

    assert everything.exit_code == 0, everything.output
    assert backend_only.exit_code == 0, backend_only.output

    assert "2 stale lock" in _diagnostic(everything.stdout, "Locks")["message"]
    assert "2 expired reservation" in _diagnostic(
        everything.stdout, "File Reservations"
    )["message"]

    assert "1 stale lock" in _diagnostic(backend_only.stdout, "Locks")["message"]
    assert "1 expired reservation" in _diagnostic(
        backend_only.stdout, "File Reservations"
    )["message"]


# ---------- doctor repair ----------


def test_doctor_repair_touches_only_the_named_project(
    isolated_env,
    tmp_path,
    monkeypatch,
) -> None:
    """Repair is a mutation, so the blast radius is the property under test.

    Both projects are in the same repairable state and only one is named; the
    other one is here purely as the thing that must come out untouched.
    """
    _seed_expired_reservations("backend", "frontend")
    storage_root = get_settings().storage.root
    backend_lock = _abandon_archive_lock(storage_root, "backend")
    frontend_lock = _abandon_archive_lock(storage_root, "frontend")

    async def _snapshot(*_args: Any, **_kwargs: Any) -> Path:
        return tmp_path / "diagnostic-snapshot"

    monkeypatch.setattr(storage_module, "create_diagnostic_backup", _snapshot)

    result = _run_cli("doctor", "repair", "Backend", "--yes")

    assert result.exit_code == 0, result.output
    assert _released_reservation_paths() == {RESERVED_PATH.format(slug="backend")}
    assert not backend_lock.exists()
    assert frontend_lock.exists()
    assert "Healed 1 stale lock(s)" in result.stdout
    assert "No stale locks to heal" not in result.stdout


def test_doctor_repair_reports_lock_and_metadata_counts(
    isolated_env,
    tmp_path,
    monkeypatch,
) -> None:
    """The CLI must report both collections returned by the lock healer."""

    async def _snapshot(*_args: Any, **_kwargs: Any) -> Path:
        return tmp_path / "diagnostic-snapshot"

    async def _healed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "locks_scanned": 2,
            "locks_removed": ["stale.lock"],
            "metadata_removed": ["orphan.lock.owner.json"],
        }

    monkeypatch.setattr(storage_module, "create_diagnostic_backup", _snapshot)
    monkeypatch.setattr(storage_module, "heal_archive_locks", _healed)

    result = _run_cli("doctor", "repair", "--yes")

    assert result.exit_code == 0, result.output
    assert "Healed 1 stale lock(s)" in result.stdout
    assert "Removed 1 orphaned lock metadata file(s)" in result.stdout
    assert "No stale locks to heal" not in result.stdout


def test_doctor_repair_changes_nothing_when_the_backup_cannot_be_taken(
    isolated_env,
    monkeypatch,
) -> None:
    """No backup, no repair. The repairable state must survive the refusal intact.

    Asserting only the exit code would leave the dangerous case uncovered: a
    version that took the backup failure as a warning and repaired anyway also
    exits non-zero, and the operator would then have mutated state with no
    snapshot to go back to.
    """
    _seed_expired_reservations("backend")
    backend_lock = _abandon_archive_lock(get_settings().storage.root, "backend")

    async def _backup_fails(*_args: Any, **_kwargs: Any) -> Path:
        raise RuntimeError("backup volume offline")

    monkeypatch.setattr(storage_module, "create_diagnostic_backup", _backup_fails)

    result = _run_cli("doctor", "repair", "Backend", "--yes")

    assert result.exit_code == 1
    assert "Backup failed" in result.stdout
    assert "backup volume offline" in result.stdout
    assert _released_reservation_paths() == set()
    assert backend_lock.exists()


def test_doctor_repair_exits_nonzero_when_a_repair_step_reports_an_error(
    isolated_env,
    tmp_path,
    monkeypatch,
) -> None:
    """A step that failed must reach the exit status, not just the transcript.

    Lock healing is caught and recorded rather than raised, so the command runs
    to completion; the error count is the only thing that carries the failure
    out to a caller, and it must not be printed and then discarded.
    """

    async def _snapshot(*_args: Any, **_kwargs: Any) -> Path:
        return tmp_path / "diagnostic-snapshot"

    async def _healing_fails(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("archive lock cleanup exploded")

    monkeypatch.setattr(storage_module, "create_diagnostic_backup", _snapshot)
    monkeypatch.setattr(storage_module, "heal_archive_locks", _healing_fails)

    result = _run_cli("doctor", "repair", "--yes")

    assert result.exit_code == 1
    assert "Lock healing failed" in result.stdout
    assert "archive lock cleanup exploded" in result.stdout
    assert "Errors: 1" in result.stdout


# ---------- doctor restore: manifests that must be refused ----------


def _backup_with_unparseable_manifest(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "manifest.json").write_text('{"version": 1,', encoding="utf-8")
    return root


def _backup_with_manifest_that_is_not_an_object(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "manifest.json").write_text('["version", 1]', encoding="utf-8")
    return root


def _backup_with_nothing_to_restore(root: Path) -> Path:
    return _write_backup_dir(root, database_path=None, project_bundles=())


def _backup_pointing_outside_itself(root: Path) -> Path:
    outside = root.parent / "elsewhere.bundle"
    outside.write_text("bundle", encoding="utf-8")
    return _write_backup_dir(
        root, database_path=None, project_bundles=[str(outside)]
    )


def _backup_whose_database_is_absent(root: Path) -> Path:
    return _write_backup_dir(
        root, database_path="payload/db-copy.sqlite3", materialise_database=False
    )


def _backup_whose_database_is_a_directory(root: Path) -> Path:
    backup = _write_backup_dir(
        root, database_path="payload/db-copy.sqlite3", materialise_database=False
    )
    (backup / "payload" / "db-copy.sqlite3").mkdir(parents=True)
    return backup


@pytest.mark.parametrize(
    ("build_backup", "expected_reason"),
    [
        pytest.param(
            _backup_with_unparseable_manifest, None, id="manifest-is-not-json"
        ),
        pytest.param(
            _backup_with_manifest_that_is_not_an_object,
            "must contain a JSON object",
            id="manifest-is-not-an-object",
        ),
        pytest.param(
            _backup_with_nothing_to_restore,
            "must include a database backup or at least one archive bundle",
            id="manifest-describes-no-payload",
        ),
        pytest.param(
            _backup_pointing_outside_itself,
            "escapes backup directory",
            id="artifact-outside-the-backup",
        ),
        pytest.param(
            _backup_whose_database_is_absent,
            "references missing artifact",
            id="artifact-does-not-exist",
        ),
        pytest.param(
            _backup_whose_database_is_a_directory,
            "artifact is not a file",
            id="artifact-is-a-directory",
        ),
    ],
)
def test_doctor_restore_refuses_a_manifest_it_cannot_vouch_for(
    tmp_path,
    monkeypatch,
    build_backup: Callable[[Path], Path],
    expected_reason: str | None,
) -> None:
    """Validation happens before anything is touched, and says which thing was wrong.

    Two properties in one, and the second is the one a refusal test usually
    loses: the refusal has to be a *pre*-condition. Both the restore and the
    pre-restore snapshot are replaced with stand-ins that fail on contact, so
    an implementation that validated halfway through the work would be caught
    rather than credited with a correct exit code.

    ``expected_reason`` is ``None`` only for the unparseable-JSON case, whose
    wording belongs to CPython's decoder rather than to this project.
    """
    monkeypatch.setattr(
        storage_module, "restore_from_backup", _must_not_be_called("restore_from_backup")
    )
    monkeypatch.setattr(
        storage_module,
        "create_diagnostic_backup",
        _must_not_be_called("create_diagnostic_backup"),
    )
    backup = build_backup(tmp_path / "backup")

    result = _run_cli("doctor", "restore", str(backup), "--yes")

    assert result.exit_code == 1
    assert "Invalid backup manifest" in result.stdout
    if expected_reason is not None:
        assert expected_reason in result.stdout


# ---------- doctor restore: the restore itself ----------


def test_doctor_restore_snapshots_the_live_state_before_overwriting_it(
    tmp_path,
    monkeypatch,
) -> None:
    """The pre-restore snapshot is worthless unless it is taken *first*.

    So the order is what is asserted, not merely that both calls happened: a
    snapshot taken after the restore has already overwritten the database
    captures the new state and there is no way back to the old one.
    """
    live_archive = tmp_path / "live-archive"
    (live_archive / ".git").mkdir(parents=True)
    monkeypatch.setenv("STORAGE_ROOT", str(live_archive))
    clear_settings_cache()
    backup = _write_backup_dir(tmp_path / "backup", storage_root=str(live_archive))
    sequence: list[str] = []

    async def _snapshot(_settings: Any, *_args: Any, **kwargs: Any) -> Path:
        sequence.append(f"snapshot(reason={kwargs.get('reason')})")
        return tmp_path / "pre-restore-snapshot"

    async def _restore(
        _settings: Any, path: Path, *, dry_run: bool
    ) -> dict[str, Any]:
        sequence.append(f"restore(path={path}, dry_run={dry_run})")
        return {"database_restored": True, "bundles_restored": [], "errors": []}

    monkeypatch.setattr(storage_module, "create_diagnostic_backup", _snapshot)
    monkeypatch.setattr(storage_module, "restore_from_backup", _restore)

    result = _run_cli("doctor", "restore", str(backup), "--yes")

    assert result.exit_code == 0, result.output
    assert sequence == [
        "snapshot(reason=pre-restore)",
        f"restore(path={backup}, dry_run=False)",
    ]
    assert "Pre-restore backup:" in result.stdout


def test_doctor_restore_stops_when_the_pre_restore_snapshot_fails(
    tmp_path,
    monkeypatch,
) -> None:
    """A failed snapshot must abort the restore, not merely be reported.

    The restore stand-in fails on contact, so proceeding without a snapshot is
    a test failure rather than something the exit code happens to hide.
    """
    live_archive = tmp_path / "live-archive"
    (live_archive / ".git").mkdir(parents=True)
    monkeypatch.setenv("STORAGE_ROOT", str(live_archive))
    clear_settings_cache()
    backup = _write_backup_dir(tmp_path / "backup", storage_root=str(live_archive))

    async def _snapshot_fails(*_args: Any, **_kwargs: Any) -> Path:
        raise RuntimeError("snapshot volume offline")

    monkeypatch.setattr(storage_module, "create_diagnostic_backup", _snapshot_fails)
    monkeypatch.setattr(
        storage_module, "restore_from_backup", _must_not_be_called("restore_from_backup")
    )

    result = _run_cli("doctor", "restore", str(backup), "--yes")

    assert result.exit_code == 1
    assert "Restore failed" in result.stdout
    assert "snapshot volume offline" in result.stdout


def test_doctor_restore_skips_the_snapshot_when_there_is_nothing_to_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    """No live database and no live archive means the snapshot is skipped and said so.

    The skip must be visible in the output. A restore that silently omits the
    safety net looks exactly like one that took it, and the operator finds out
    only when they go looking for the snapshot.
    """
    empty_archive = tmp_path / "empty-archive"
    empty_archive.mkdir()
    absent_database = tmp_path / "nowhere" / "mail.sqlite3"
    monkeypatch.setenv("STORAGE_ROOT", str(empty_archive))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{absent_database}")
    clear_settings_cache()
    backup = _write_backup_dir(tmp_path / "backup", storage_root=str(empty_archive))

    monkeypatch.setattr(
        storage_module,
        "create_diagnostic_backup",
        _must_not_be_called("create_diagnostic_backup"),
    )
    monkeypatch.setattr(storage_module, "restore_from_backup", _completed_restore())

    result = _run_cli("doctor", "restore", str(backup), "--yes")

    assert result.exit_code == 0, result.output
    assert "Pre-restore backup skipped" in result.stdout
    assert "Database restored" in result.stdout


def test_doctor_restore_exits_nonzero_when_the_restore_reports_errors(
    tmp_path,
    monkeypatch,
) -> None:
    """Partial success is failure: the errors reach both the output and the status."""
    backup = _write_backup_dir(
        tmp_path / "backup", database_path="payload/db-copy.sqlite3"
    )

    async def _snapshot(*_args: Any, **_kwargs: Any) -> Path:
        return tmp_path / "pre-restore-snapshot"

    async def _partial_restore(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "database_restored": False,
            "bundles_restored": [],
            "errors": ["bundle for project demo could not be applied"],
        }

    monkeypatch.setattr(storage_module, "create_diagnostic_backup", _snapshot)
    monkeypatch.setattr(storage_module, "restore_from_backup", _partial_restore)

    result = _run_cli("doctor", "restore", str(backup), "--yes")

    assert result.exit_code == 1
    assert "Restore completed with errors" in result.stdout
    assert "bundle for project demo could not be applied" in result.stdout


# ---------- doctor restore: --dry-run ----------


def test_doctor_restore_dry_run_previews_without_snapshotting_or_confirming(
    tmp_path,
    monkeypatch,
) -> None:
    """A preview must write nothing -- including the snapshot that precedes a write.

    ``--yes`` is deliberately absent: a preview that stopped to ask for
    confirmation would hang a non-interactive caller, so the run completing at
    all is part of what is being asserted.
    """
    backup = _write_backup_dir(tmp_path / "backup")
    dry_run_flags: list[bool] = []

    async def _preview(_settings: Any, _path: Path, *, dry_run: bool) -> dict[str, Any]:
        dry_run_flags.append(dry_run)
        return {
            "database_restored": False,
            "bundles_restored": [],
            "errors": [],
            "would_restore_database": True,
            "would_restore_bundles": [],
        }

    monkeypatch.setattr(
        storage_module,
        "create_diagnostic_backup",
        _must_not_be_called("create_diagnostic_backup"),
    )
    monkeypatch.setattr(storage_module, "restore_from_backup", _preview)

    result = _run_cli("doctor", "restore", str(backup), "--dry-run")

    assert result.exit_code == 0, result.output
    assert dry_run_flags == [True]
    assert "Would restore" in result.stdout


def test_doctor_restore_dry_run_fails_when_the_target_cannot_take_the_payload(
    tmp_path,
    monkeypatch,
) -> None:
    """A preview that finds blockers must exit non-zero and name them.

    Nothing is stubbed here on purpose: the real ``restore_from_backup`` decides
    that a manifest carrying a database payload cannot be applied to a
    non-SQLite target, and a preview that reported the blocker while exiting 0
    would be read by any script as permission to proceed.
    """
    backup = _write_backup_dir(tmp_path / "backup", reason="postgres-target")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://agent:mail@localhost:5432/mcp"
    )
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    clear_settings_cache()

    result = _run_cli("doctor", "restore", str(backup), "--dry-run")

    assert result.exit_code == 1
    assert "Dry run found restore blockers" in result.stdout
    assert "does not use a SQLite database file" in result.stdout
