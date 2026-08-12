"""One-time, offline migration tests for stable client-scoped agent identities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from click import unstyle
from fastmcp import Client
from git import Git
from sqlalchemy import func, select as _sa_select, text
from sqlalchemy.engine import make_url
from sqlalchemy.sql import ColumnElement
from typer.testing import CliRunner

from mcp_agent_mail import cli as cli_module, storage as storage_module, utils as utils_module
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.cli import app
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import ensure_schema, get_session, reset_database_state
from mcp_agent_mail.models import (
    Agent,
    AgentLink,
    FileReservation,
    Message,
    MessageRecipient,
    Project,
    WindowIdentity,
)
from mcp_agent_mail.storage import (
    IDENTITY_RENAMES_FILENAME,
    IDENTITY_TOMBSTONES_DIRNAME,
    _commit,
    ensure_archive,
    get_historical_inbox_snapshot,
)
from mcp_agent_mail.utils import parse_client_platform_host_agent_id
from tests.keys import pkey

OLD_NAME = "home-wsl-1"
NEW_NAME = "codex-wsl-home-1"
REGISTRATION_TOKEN = "rename-test-registration-secret"
PROJECT_KEY = pkey("rename/project")


def select(*entities: Any, **kwargs: Any) -> Any:
    """Keep SQLModel descriptor typing out of migration behavior tests."""
    return _sa_select(*entities, **kwargs)


@dataclass(slots=True)
class SeededRename:
    project_id: int
    agent_id: int
    peer_id: int
    message_id: int
    reservation_id: int
    link_id: int
    archive_root: Path
    repo_root: Path
    seed_commit: str
    seed_timestamp: str


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _meaningful_git_status(repo_root: Path) -> list[str]:
    lines = _git(repo_root, "status", "--short", "--untracked-files=all").splitlines()
    return [
        line
        for line in lines
        if not line.endswith("server.lock")
        and not line.endswith("server.pid")
        and not line.endswith(".archive.lock")
        and not line.endswith(".archive.lock.owner.json")
    ]


def _tree_contents(root: Path) -> dict[str, str]:
    """Snapshot file bytes without letting metadata-only reads obscure mutations."""
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def _seed_rename_state(
    *,
    old_name: str = OLD_NAME,
    target_collision: str | None = None,
) -> SeededRename:
    await ensure_schema()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_session() as session:
        project = Project(slug="rename-project", human_key=PROJECT_KEY)
        session.add(project)
        await session.commit()
        await session.refresh(project)
        assert project.id is not None

        agent = Agent(
            project_id=project.id,
            name=old_name,
            program="claude",
            model="opus",
            registration_token=REGISTRATION_TOKEN,
        )
        peer = Agent(
            project_id=project.id,
            name="codex-linux-peer-1",
            program="codex",
            model="gpt",
            registration_token="peer-registration-secret",
        )
        session.add(agent)
        session.add(peer)
        await session.commit()
        await session.refresh(agent)
        await session.refresh(peer)
        assert agent.id is not None
        assert peer.id is not None

        if target_collision is not None:
            session.add(
                Agent(
                    project_id=project.id,
                    name=target_collision,
                    program="codex",
                    model="gpt",
                    registration_token="collision-secret",
                )
            )

        message = Message(
            project_id=project.id,
            sender_id=agent.id,
            subject="Preserve identity relationships",
            body_md="The foreign keys must not move.",
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)
        assert message.id is not None
        session.add(
            MessageRecipient(
                message_id=message.id,
                agent_id=agent.id,
                kind="to",
                read_ts=now,
                ack_ts=now,
            )
        )
        reservation = FileReservation(
            project_id=project.id,
            agent_id=agent.id,
            path_pattern="src/identity.py",
            exclusive=True,
            reason="rename regression",
            expires_ts=now + timedelta(hours=1),
        )
        session.add(reservation)
        link = AgentLink(
            a_project_id=project.id,
            a_agent_id=agent.id,
            b_project_id=project.id,
            b_agent_id=peer.id,
            status="approved",
        )
        session.add(link)
        session.add(
            WindowIdentity(
                project_id=project.id,
                window_uuid="11111111-1111-4111-8111-111111111111",
                display_name=old_name.upper(),
            )
        )
        session.add(
            WindowIdentity(
                project_id=project.id,
                window_uuid="22222222-2222-4222-8222-222222222222",
                display_name="unrelated-agent",
            )
        )
        await session.commit()
        await session.refresh(reservation)
        await session.refresh(link)
        assert reservation.id is not None
        assert link.id is not None

    archive = await ensure_archive(get_settings(), "rename-project")
    _git(
        archive.repo_root,
        "config",
        "--local",
        "user.name",
        archive.settings.storage.git_author_name,
    )
    _git(
        archive.repo_root,
        "config",
        "--local",
        "user.email",
        archive.settings.storage.git_author_email,
    )
    profile_path = archive.root / "agents" / old_name / "profile.json"
    inbox_path = (
        archive.root
        / "agents"
        / old_name
        / "inbox"
        / "2026"
        / "08"
        / f"2026-08-08T00-00-00Z__preserve-identity__{message.id}.md"
    )
    outbox_path = (
        archive.root
        / "agents"
        / old_name
        / "outbox"
        / "2026"
        / "08"
        / f"2026-08-08T00-00-00Z__preserve-identity__{message.id}.md"
    )
    reservation_dir = archive.root / "file_reservations"
    matching_reservation_path = reservation_dir / f"id-{reservation.id}.json"
    unrelated_reservation_path = reservation_dir / "id-unrelated.json"
    for path in (profile_path, inbox_path, outbox_path, matching_reservation_path, unrelated_reservation_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        json.dumps(
            {
                "id": agent.id,
                "project_id": project.id,
                "name": old_name,
                "program": "claude",
                "model": "opus",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    message_body = (
        "---json\n"
        + json.dumps(
            {
                "id": message.id,
                "from": "codex-linux-peer-1",
                "to": [old_name],
                "subject": "Preserve identity relationships",
                "importance": "normal",
            }
        )
        + "\n---\n\nThe foreign keys must not move.\n"
    )
    inbox_path.write_text(message_body, encoding="utf-8")
    outbox_path.write_text(message_body, encoding="utf-8")
    matching_reservation_path.write_text(
        json.dumps(
            {
                "id": reservation.id,
                "agent_id": agent.id,
                "agent": old_name,
                "path_pattern": reservation.path_pattern,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # The display name intentionally matches OLD, but the id does not. The
    # migration is keyed by the stable Agent.id and must leave this record alone.
    unrelated_reservation_path.write_text(
        json.dumps(
            {
                "id": "unrelated",
                "agent_id": peer.id,
                "agent": old_name,
                "path_pattern": "src/unrelated.py",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    rel_paths = [
        path.relative_to(archive.repo_root).as_posix()
        for path in (
            profile_path,
            inbox_path,
            outbox_path,
            matching_reservation_path,
            unrelated_reservation_path,
        )
    ]
    await _commit(archive.repo, archive.settings, "seed: legacy identity", rel_paths, use_queue=False)
    seed = archive.repo.head.commit
    return SeededRename(
        project_id=project.id,
        agent_id=agent.id,
        peer_id=peer.id,
        message_id=message.id,
        reservation_id=reservation.id,
        link_id=link.id,
        archive_root=archive.root,
        repo_root=archive.repo_root,
        seed_commit=seed.hexsha,
        seed_timestamp=seed.authored_datetime.isoformat(),
    )


def _seed(**kwargs: Any) -> SeededRename:
    seeded = asyncio.run(_seed_rename_state(**kwargs))
    reset_database_state()
    return seeded


@pytest.mark.asyncio
async def test_archive_commit_is_clean_with_windows_crlf_and_global_autocrlf(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    global_git_config = tmp_path / "global.gitconfig"
    global_git_config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    archive = await ensure_archive(get_settings(), "windows-eol-project")
    profile_path = archive.root / "agents" / OLD_NAME / "profile.json"
    message_path = archive.root / "agents" / OLD_NAME / "inbox" / "message.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    message_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_bytes(b'{"name": "home-wsl-1"}\r\n')
    message_path.write_bytes(b"# Windows checkout\r\n\r\nBody\r\n")
    rel_paths = [
        profile_path.relative_to(archive.repo_root).as_posix(),
        message_path.relative_to(archive.repo_root).as_posix(),
    ]

    await _commit(
        archive.repo,
        archive.settings,
        "test: commit Windows line endings",
        rel_paths,
        use_queue=False,
    )

    assert _git(archive.repo_root, "config", "--local", "--get", "core.autocrlf") == "false"
    assert profile_path.read_bytes() == b'{"name": "home-wsl-1"}\n'
    assert message_path.read_bytes() == b"# Windows checkout\n\nBody\n"
    assert _meaningful_git_status(archive.repo_root) == []


def test_rename_agent_defaults_to_dry_run_without_mutation(isolated_env) -> None:
    seeded = _seed()
    runner = CliRunner()
    # Model a real offline invocation: no live SQLAlchemy engine is keeping
    # SQLite WAL/SHM sidecars open when the operator runs the dry-run.
    reset_database_state()
    before_tree = _tree_contents(seeded.repo_root.parent)

    result = runner.invoke(app, ["rename-agent", PROJECT_KEY, OLD_NAME, NEW_NAME])

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in unstyle(result.output)
    assert "not lock-verified" in unstyle(result.output)
    assert REGISTRATION_TOKEN not in unstyle(result.output)
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == seeded.seed_commit
    assert (seeded.archive_root / "agents" / OLD_NAME / "profile.json").is_file()
    assert not (seeded.archive_root / "agents" / NEW_NAME).exists()
    assert not (seeded.archive_root / IDENTITY_RENAMES_FILENAME).exists()
    assert _tree_contents(seeded.repo_root.parent) == before_tree

    async def verify() -> tuple[str, str | None]:
        async with get_session() as session:
            agent = await session.get(Agent, seeded.agent_id)
            assert agent is not None
            return agent.name, agent.registration_token

    assert asyncio.run(verify()) == (OLD_NAME, REGISTRATION_TOKEN)


def test_rename_agent_dry_run_never_creates_missing_database_or_archive(isolated_env) -> None:
    settings = get_settings()
    database_url = make_url(settings.database.url)
    assert database_url.database is not None
    database_path = Path(database_url.database)
    storage_root = Path(settings.storage.root)
    before_parent = _tree_contents(storage_root.parent)

    result = CliRunner().invoke(
        app,
        ["rename-agent", PROJECT_KEY, OLD_NAME, NEW_NAME],
    )

    assert result.exit_code != 0
    assert "will not create" in unstyle(result.output)
    assert not database_path.exists()
    assert not storage_root.exists()
    assert _tree_contents(storage_root.parent) == before_parent


def test_rename_agent_dry_run_rejects_nonempty_wal_without_mutation(
    isolated_env,
) -> None:
    seeded = _seed()
    database_url = make_url(get_settings().database.url)
    assert database_url.database is not None
    database_path = Path(database_url.database)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "UPDATE agents SET task_description = ? WHERE id = ?",
            ("wal-probe", seeded.agent_id),
        )
        connection.commit()
        wal_path = database_path.with_name(f"{database_path.name}-wal")
        assert wal_path.stat().st_size > 0
        before = _tree_contents(seeded.repo_root.parent)

        result = CliRunner().invoke(
            app,
            ["rename-agent", PROJECT_KEY, OLD_NAME, NEW_NAME],
        )

        assert result.exit_code != 0
        assert "WAL" in unstyle(result.output)
        assert "non-empty" in unstyle(result.output)
        assert _tree_contents(seeded.repo_root.parent) == before
        assert _git(seeded.repo_root, "rev-parse", "HEAD") == seeded.seed_commit
    finally:
        connection.close()


def test_rename_agent_apply_preserves_ids_token_relations_and_git_history(isolated_env, tmp_path: Path) -> None:
    seeded = _seed()
    signals_root = tmp_path / "signals"
    signal_path = signals_root / "projects" / "rename-project" / "agents" / f"{OLD_NAME}.signal"
    signal_path.parent.mkdir(parents=True)
    signal_path.write_text("ephemeral\n", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
        env={"NOTIFICATIONS_SIGNALS_DIR": str(signals_root)},
    )

    assert result.exit_code == 0, result.output
    assert "APPLIED" in unstyle(result.output)
    assert "preserved" in unstyle(result.output)
    assert REGISTRATION_TOKEN not in unstyle(result.output)
    assert hashlib.sha256(REGISTRATION_TOKEN.encode()).hexdigest() not in unstyle(
        result.output
    )

    async def verify_database() -> dict[str, Any]:
        async with get_session() as session:
            agent = await session.get(Agent, seeded.agent_id)
            assert agent is not None
            names = (
                await session.execute(
                    select(Agent.name).where(
                        cast(ColumnElement[bool], Agent.project_id == seeded.project_id)
                    )
                )
            ).scalars().all()
            windows = (
                await session.execute(
                    select(WindowIdentity.display_name).where(
                        cast(ColumnElement[bool], WindowIdentity.project_id == seeded.project_id)
                    )
                )
            ).scalars().all()
            sender_id = (
                await session.execute(
                    select(Message.sender_id).where(
                        cast(ColumnElement[bool], Message.id == seeded.message_id)
                    )
                )
            ).scalar_one()
            recipient = (
                await session.execute(
                    select(MessageRecipient).where(
                        cast(ColumnElement[bool], MessageRecipient.message_id == seeded.message_id)
                    )
                )
            ).scalars().one()
            reservation = await session.get(FileReservation, seeded.reservation_id)
            link = await session.get(AgentLink, seeded.link_id)
            assert reservation is not None
            assert link is not None
            return {
                "agent": (agent.id, agent.name, agent.registration_token),
                "names": names,
                "windows": windows,
                "sender_id": sender_id,
                "recipient": (recipient.agent_id, recipient.read_ts, recipient.ack_ts),
                "reservation_agent_id": reservation.agent_id,
                "link_ids": (link.a_agent_id, link.b_agent_id),
            }

    database = asyncio.run(verify_database())
    assert database["agent"] == (seeded.agent_id, NEW_NAME, REGISTRATION_TOKEN)
    assert OLD_NAME not in database["names"]
    assert NEW_NAME in database["names"]
    assert NEW_NAME in database["windows"]
    assert "unrelated-agent" in database["windows"]
    assert database["sender_id"] == seeded.agent_id
    assert database["recipient"][0] == seeded.agent_id
    assert database["recipient"][1] is not None
    assert database["recipient"][2] is not None
    assert database["reservation_agent_id"] == seeded.agent_id
    assert database["link_ids"] == (seeded.agent_id, seeded.peer_id)

    old_dir = seeded.archive_root / "agents" / OLD_NAME
    new_dir = seeded.archive_root / "agents" / NEW_NAME
    assert not old_dir.exists()
    assert (new_dir / "inbox" / "2026" / "08").is_dir()
    assert (new_dir / "outbox" / "2026" / "08").is_dir()
    profile = json.loads((new_dir / "profile.json").read_text(encoding="utf-8"))
    assert profile["id"] == seeded.agent_id
    assert profile["name"] == NEW_NAME

    matching = json.loads(
        (seeded.archive_root / "file_reservations" / f"id-{seeded.reservation_id}.json").read_text(
            encoding="utf-8"
        )
    )
    unrelated = json.loads(
        (seeded.archive_root / "file_reservations" / "id-unrelated.json").read_text(encoding="utf-8")
    )
    assert matching["agent"] == NEW_NAME
    assert matching["agent_id"] == seeded.agent_id
    assert unrelated["agent"] == OLD_NAME
    assert unrelated["agent_id"] == seeded.peer_id

    ledger = json.loads((seeded.archive_root / IDENTITY_RENAMES_FILENAME).read_text(encoding="utf-8"))
    assert ledger["renames"][-1]["agent_id"] == seeded.agent_id
    assert ledger["renames"][-1]["old_name"] == OLD_NAME
    assert ledger["renames"][-1]["new_name"] == NEW_NAME
    tombstone = json.loads(
        (
            seeded.archive_root
            / IDENTITY_TOMBSTONES_DIRNAME
            / f"{OLD_NAME}.json"
        ).read_text(encoding="utf-8")
    )
    assert tombstone["new_name"] == NEW_NAME
    assert tombstone["agent_id"] == seeded.agent_id
    assert signal_path.read_text(encoding="utf-8") == "ephemeral\n"

    assert _git(seeded.repo_root, "rev-list", "--count", f"{seeded.seed_commit}..HEAD") == "1"
    assert _meaningful_git_status(seeded.repo_root) == []
    assert json.loads(
        _git(
            seeded.repo_root,
            "show",
            f"{seeded.seed_commit}:projects/rename-project/agents/{OLD_NAME}/profile.json",
        )
    )["name"] == OLD_NAME
    assert json.loads(
        _git(
            seeded.repo_root,
            "show",
            f"HEAD:projects/rename-project/agents/{NEW_NAME}/profile.json",
        )
    )["name"] == NEW_NAME
    historical_message = _git(
        seeded.repo_root,
        "show",
        (
            f"{seeded.seed_commit}:projects/rename-project/agents/{OLD_NAME}/"
            f"inbox/2026/08/2026-08-08T00-00-00Z__preserve-identity__{seeded.message_id}.md"
        ),
    )
    current_message = _git(
        seeded.repo_root,
        "show",
        (
            f"HEAD:projects/rename-project/agents/{NEW_NAME}/"
            f"inbox/2026/08/2026-08-08T00-00-00Z__preserve-identity__{seeded.message_id}.md"
        ),
    )
    assert current_message == historical_message
    assert OLD_NAME in current_message

    async def historical_snapshot() -> dict[str, Any]:
        archive = await ensure_archive(get_settings(), "rename-project")
        return await get_historical_inbox_snapshot(
            archive,
            NEW_NAME,
            seeded.seed_timestamp,
        )

    snapshot = asyncio.run(historical_snapshot())
    assert [message["id"] for message in snapshot["messages"]] == [str(seeded.message_id)]
    assert snapshot["resolved_agent_name"] == OLD_NAME


def test_rename_agent_is_scoped_when_two_projects_share_the_old_name(
    isolated_env,
) -> None:
    seeded = _seed()
    other_project_key = pkey("rename/project-b")
    other_token = "other-project-registration-secret"

    async def seed_other_project() -> tuple[int, int, Path]:
        await ensure_schema()
        async with get_session() as session:
            other_project = Project(
                slug="rename-project-b",
                human_key=other_project_key,
            )
            session.add(other_project)
            await session.commit()
            await session.refresh(other_project)
            assert other_project.id is not None

            other_agent = Agent(
                project_id=other_project.id,
                name=OLD_NAME,
                program="codex",
                model="gpt",
                registration_token=other_token,
            )
            session.add(other_agent)
            await session.commit()
            await session.refresh(other_agent)
            assert other_agent.id is not None

        archive = await ensure_archive(get_settings(), other_project.slug)
        profile_path = archive.root / "agents" / OLD_NAME / "profile.json"
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(
            json.dumps(
                {
                    "id": other_agent.id,
                    "project_id": other_project.id,
                    "name": OLD_NAME,
                    "program": "codex",
                    "model": "gpt",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        await _commit(
            archive.repo,
            archive.settings,
            "seed: same legacy identity in second project",
            [profile_path.relative_to(archive.repo_root).as_posix()],
            use_queue=False,
        )
        return other_project.id, other_agent.id, archive.root

    other_project_id, other_agent_id, other_archive_root = asyncio.run(
        seed_other_project()
    )
    reset_database_state()
    other_archive_before = _tree_contents(other_archive_root)
    head_before = _git(seeded.repo_root, "rev-parse", "HEAD")

    result = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )

    assert result.exit_code == 0, result.output
    assert other_token not in unstyle(result.output)

    async def project_names_and_tokens() -> tuple[tuple[str, str | None], tuple[str, str | None]]:
        async with get_session() as session:
            selected = await session.get(Agent, seeded.agent_id)
            other = await session.get(Agent, other_agent_id)
            assert selected is not None
            assert other is not None
            assert other.project_id == other_project_id
            return (
                (selected.name, selected.registration_token),
                (other.name, other.registration_token),
            )

    selected_identity, other_identity = asyncio.run(project_names_and_tokens())
    assert selected_identity == (NEW_NAME, REGISTRATION_TOKEN)
    assert other_identity == (OLD_NAME, other_token)

    assert _tree_contents(other_archive_root) == other_archive_before
    assert (other_archive_root / "agents" / OLD_NAME / "profile.json").is_file()
    assert not (other_archive_root / "agents" / NEW_NAME).exists()
    assert not (other_archive_root / IDENTITY_RENAMES_FILENAME).exists()
    assert not (
        other_archive_root
        / IDENTITY_TOMBSTONES_DIRNAME
        / f"{OLD_NAME}.json"
    ).exists()
    assert _git(seeded.repo_root, "rev-list", "--count", f"{head_before}..HEAD") == "1"
    changed_paths = _git(
        seeded.repo_root,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "HEAD",
    ).splitlines()
    assert changed_paths
    assert all(path.startswith("projects/rename-project/") for path in changed_paths)


def test_rename_agent_tombstone_blocks_stale_registration(isolated_env) -> None:
    seeded = _seed()
    result = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )
    assert result.exit_code == 0, result.output

    async def attempt_registration() -> str:
        server = build_mcp_server()
        async with Client(server) as client:
            with pytest.raises(Exception) as exc_info:
                await client.call_tool(
                    "register_agent",
                    {
                        "project_key": PROJECT_KEY,
                        "program": "claude",
                        "model": "opus",
                        "name": OLD_NAME,
                    },
                )
            return str(exc_info.value)

    error = asyncio.run(attempt_registration())
    assert "IDENTITY_RENAMED" in error
    assert NEW_NAME in error

    async def count_old_rows() -> int:
        async with get_session() as session:
            return int(
                (
                    await session.execute(
                        select(func.count(Agent.id)).where(
                            cast(ColumnElement[bool], Agent.project_id == seeded.project_id),
                            func.lower(Agent.name) == OLD_NAME.lower(),
                        )
                    )
                ).scalar_one()
            )

    assert asyncio.run(count_old_rows()) == 0


def test_historical_snapshot_uses_the_rename_commit_as_exact_boundary(
    isolated_env,
) -> None:
    seeded = _seed()
    result = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )
    assert result.exit_code == 0, result.output
    ledger = json.loads(
        (seeded.archive_root / IDENTITY_RENAMES_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    boundary = datetime.fromisoformat(ledger["renames"][-1]["renamed_at"])

    async def snapshots() -> tuple[dict[str, Any], dict[str, Any]]:
        archive = await ensure_archive(get_settings(), "rename-project")
        before = await get_historical_inbox_snapshot(
            archive,
            NEW_NAME,
            (boundary - timedelta(microseconds=1)).isoformat(),
        )
        exact = await get_historical_inbox_snapshot(
            archive,
            NEW_NAME,
            boundary.isoformat(),
        )
        return before, exact

    before, exact = asyncio.run(snapshots())
    assert before["resolved_agent_name"] == OLD_NAME
    assert exact["resolved_agent_name"] == NEW_NAME
    assert [item["id"] for item in before["messages"]] == [
        str(seeded.message_id)
    ]
    assert [item["id"] for item in exact["messages"]] == [
        str(seeded.message_id)
    ]
    assert exact["commit_sha"] == _git(seeded.repo_root, "rev-parse", "HEAD")


@pytest.mark.parametrize("enforcement_mode", ["coerce", "always_auto"])
def test_tombstone_blocks_raw_old_name_before_enforcement_mode(
    isolated_env,
    monkeypatch,
    enforcement_mode: str,
) -> None:
    old_name = "home-wsl-claude-1"
    new_name = "claude-wsl-home-1"
    seeded = _seed(old_name=old_name)
    applied = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            old_name,
            new_name,
            "--apply",
            "--confirm",
            f"{old_name}=>{new_name}",
        ],
    )
    assert applied.exit_code == 0, applied.output
    monkeypatch.setenv("AGENT_NAME_ENFORCEMENT_MODE", enforcement_mode)
    clear_settings_cache()

    async def attempt_registration() -> str:
        server = build_mcp_server()
        async with Client(server) as client:
            with pytest.raises(Exception) as exc_info:
                await client.call_tool(
                    "register_agent",
                    {
                        "project_key": PROJECT_KEY,
                        "program": "claude",
                        "model": "opus",
                        "name": old_name,
                    },
                )
            return str(exc_info.value)

    error = asyncio.run(attempt_registration())
    assert "IDENTITY_RENAMED" in error
    assert new_name in error

    async def count_project_agents() -> int:
        async with get_session() as session:
            return int(
                (
                    await session.execute(
                        select(func.count(Agent.id)).where(
                            cast(
                                ColumnElement[bool],
                                Agent.project_id == seeded.project_id,
                            )
                        )
                    )
                ).scalar_one()
            )

    assert asyncio.run(count_project_agents()) == 2


@pytest.mark.parametrize(
    ("old_name", "new_name", "target_collision", "expected"),
    [
        (OLD_NAME, "HOME-WSL-1", None, "case-only"),
        (OLD_NAME, "home-wsl-2", None, "canonical"),
        (OLD_NAME, NEW_NAME, NEW_NAME.upper(), "collision"),
    ],
)
def test_rename_agent_rejects_case_invalid_target_and_collision(
    isolated_env,
    old_name: str,
    new_name: str,
    target_collision: str | None,
    expected: str,
) -> None:
    seeded = _seed(old_name=old_name, target_collision=target_collision)

    result = CliRunner().invoke(app, ["rename-agent", PROJECT_KEY, old_name, new_name])

    assert result.exit_code != 0
    assert expected in unstyle(result.output).lower()
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == seeded.seed_commit


def test_rename_agent_leaves_pre_platform_identity_out_of_scope(isolated_env) -> None:
    seeded = _seed(old_name="holzera-1")

    result = CliRunner().invoke(
        app,
        ["rename-agent", PROJECT_KEY, "holzera-1", "codex-linux-holzera-1"],
    )

    assert result.exit_code != 0
    assert "pre-platform" in unstyle(result.output)
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == seeded.seed_commit


@pytest.mark.parametrize(
    "old_name",
    ["home-wsl-codex-1", "home-wsl-cx-1", "MaroonPuma"],
)
def test_rename_agent_accepts_evidenced_transitional_and_server_coerced_sources(
    isolated_env,
    old_name: str,
) -> None:
    seeded = _seed(old_name=old_name)

    result = CliRunner().invoke(
        app,
        ["rename-agent", PROJECT_KEY, old_name, NEW_NAME],
    )

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in unstyle(result.output)
    assert f"agent_id={seeded.agent_id}" in unstyle(result.output)


def test_shared_identity_parser_preserves_a_hyphenated_host() -> None:
    assert parse_client_platform_host_agent_id(
        "claude-mac-macbook-pro-mateusza-12"
    ) == ("claude", "mac", "macbook-pro-mateusza", "12")
    assert parse_client_platform_host_agent_id("claude-code-mac-home-1") is None


def test_rename_agent_accepts_hyphenated_legacy_host(isolated_env) -> None:
    old_name = "macbook-pro-mateusza-mac-1"
    new_name = "claude-mac-macbook-pro-mateusza-1"
    seeded = _seed(old_name=old_name)

    result = CliRunner().invoke(
        app,
        ["rename-agent", PROJECT_KEY, old_name, new_name],
    )

    assert result.exit_code == 0, result.output
    assert f"agent_id={seeded.agent_id}" in unstyle(result.output)


@pytest.mark.parametrize(
    "new_name",
    [
        "codex-linux-home-1",
        "codex-wsl-other-1",
        "codex-wsl-home-2",
    ],
)
def test_rename_agent_rejects_structural_identity_drift(
    isolated_env,
    new_name: str,
) -> None:
    seeded = _seed()

    result = CliRunner().invoke(
        app,
        ["rename-agent", PROJECT_KEY, OLD_NAME, new_name],
    )

    assert result.exit_code != 0
    assert "same host, OS and slot" in unstyle(result.output)
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == seeded.seed_commit


@pytest.mark.parametrize(
    ("old_name", "new_name"),
    [
        ("codex-home-wsl-1", "codex-wsl-home-1"),
        ("codex-wsl-home-1", "claude-wsl-home-1"),
    ],
)
def test_rename_agent_rejects_undeployed_client_first_source_shapes(
    isolated_env,
    old_name: str,
    new_name: str,
) -> None:
    seeded = _seed(old_name=old_name)

    result = CliRunner().invoke(
        app,
        ["rename-agent", PROJECT_KEY, old_name, new_name],
    )

    assert result.exit_code != 0
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == seeded.seed_commit


def test_rename_agent_retries_after_archive_ahead_failure(isolated_env, monkeypatch) -> None:
    seeded = _seed()
    original_apply = cli_module._apply_agent_rename_database

    async def fail_after_archive(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("injected database interruption")

    monkeypatch.setattr(cli_module, "_apply_agent_rename_database", fail_after_archive)
    first = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )
    assert first.exit_code != 0
    assert "injected database interruption" in first.output
    assert not (seeded.archive_root / "agents" / OLD_NAME).exists()
    assert (seeded.archive_root / "agents" / NEW_NAME).is_dir()

    async def database_name() -> str:
        async with get_session() as session:
            agent = await session.get(Agent, seeded.agent_id)
            assert agent is not None
            return agent.name

    assert asyncio.run(database_name()) == OLD_NAME
    archive_ahead_head = _git(seeded.repo_root, "rev-parse", "HEAD")

    monkeypatch.setattr(cli_module, "_apply_agent_rename_database", original_apply)
    second = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )
    assert second.exit_code == 0, second.output
    assert asyncio.run(database_name()) == NEW_NAME
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == archive_ahead_head
    assert _meaningful_git_status(seeded.repo_root) == []


@pytest.mark.parametrize(
    "failure_phase",
    [
        "after_move",
        "after_profile",
        "after_reservation",
        "after_ledger",
        "after_tombstone",
    ],
)
def test_rename_agent_retries_after_each_archive_filesystem_phase(
    isolated_env,
    monkeypatch,
    failure_phase: str,
) -> None:
    seeded = _seed()
    original_write = storage_module._write_json_atomic_sync
    interrupted = False

    def interrupted_write(path: Path, payload: Any) -> None:
        nonlocal interrupted
        is_profile = path.name == "profile.json" and NEW_NAME in path.parts
        is_reservation = path.parent.name == "file_reservations"
        is_ledger = path.name == IDENTITY_RENAMES_FILENAME
        is_tombstone = path.parent.name == IDENTITY_TOMBSTONES_DIRNAME
        before_phase = {
            "after_move": is_profile,
            "after_profile": is_reservation,
            "after_reservation": is_ledger,
            "after_ledger": is_tombstone,
        }
        if not interrupted and before_phase.get(failure_phase, False):
            interrupted = True
            raise RuntimeError(f"injected {failure_phase}")
        original_write(path, payload)
        if (
            not interrupted
            and failure_phase == "after_tombstone"
            and is_tombstone
        ):
            interrupted = True
            raise RuntimeError(f"injected {failure_phase}")

    monkeypatch.setattr(
        storage_module,
        "_write_json_atomic_sync",
        interrupted_write,
    )
    first = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )
    assert first.exit_code != 0
    assert interrupted
    assert f"injected {failure_phase}" in first.output
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == seeded.seed_commit

    async def database_name() -> str:
        async with get_session() as session:
            agent = await session.get(Agent, seeded.agent_id)
            assert agent is not None
            return agent.name

    assert asyncio.run(database_name()) == OLD_NAME
    monkeypatch.setattr(
        storage_module,
        "_write_json_atomic_sync",
        original_write,
    )
    retry = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )
    assert retry.exit_code == 0, retry.output
    assert asyncio.run(database_name()) == NEW_NAME
    assert _git(
        seeded.repo_root,
        "rev-list",
        "--count",
        f"{seeded.seed_commit}..HEAD",
    ) == "1"
    assert _meaningful_git_status(seeded.repo_root) == []


def test_rename_agent_retries_after_index_was_staged(
    isolated_env,
    monkeypatch,
) -> None:
    seeded = _seed()
    original_call_process = Git._call_process
    interrupted = False

    def fail_before_commit(
        git: Git,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal interrupted
        if method == "commit":
            interrupted = True
            raise RuntimeError("injected after staging")
        return original_call_process(git, method, *args, **kwargs)

    monkeypatch.setattr(Git, "_call_process", fail_before_commit)
    first = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )
    assert first.exit_code != 0
    assert interrupted
    assert "injected after staging" in first.output
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == seeded.seed_commit

    monkeypatch.setattr(Git, "_call_process", original_call_process)
    retry = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )
    assert retry.exit_code == 0, retry.output
    assert _git(
        seeded.repo_root,
        "rev-list",
        "--count",
        f"{seeded.seed_commit}..HEAD",
    ) == "1"
    assert _meaningful_git_status(seeded.repo_root) == []


@pytest.mark.parametrize("artifact", ["matching", "unrelated"])
def test_malformed_reservation_blocks_before_archive_mutation(
    isolated_env,
    artifact: str,
) -> None:
    seeded = _seed()
    reservation_path = (
        seeded.archive_root
        / "file_reservations"
        / (
            f"id-{seeded.reservation_id}.json"
            if artifact == "matching"
            else "id-malformed-unrelated.json"
        )
    )
    reservation_path.write_text("{not-json\n", encoding="utf-8")
    relative_path = reservation_path.relative_to(seeded.repo_root).as_posix()
    _git(seeded.repo_root, "add", "--", relative_path)
    _git(seeded.repo_root, "commit", "-m", f"seed: malformed {artifact} reservation")
    before_head = _git(seeded.repo_root, "rev-parse", "HEAD")

    result = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )

    assert result.exit_code != 0
    assert "reservation artifact is unreadable" in unstyle(result.output)
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == before_head
    assert (seeded.archive_root / "agents" / OLD_NAME).is_dir()
    assert not (seeded.archive_root / "agents" / NEW_NAME).exists()

    async def database_name() -> str:
        async with get_session() as session:
            agent = await session.get(Agent, seeded.agent_id)
            assert agent is not None
            return agent.name

    assert asyncio.run(database_name()) == OLD_NAME


def test_staged_unrelated_lock_blocks_dedicated_rename_commit(isolated_env) -> None:
    seeded = _seed()
    staged_lock = seeded.archive_root / "data.lock"
    staged_lock.write_text("operator-owned\n", encoding="utf-8")
    relative_path = staged_lock.relative_to(seeded.repo_root).as_posix()
    _git(seeded.repo_root, "add", "--", relative_path)

    result = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )

    assert result.exit_code != 0
    assert "clean" in unstyle(result.output).lower()
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == seeded.seed_commit
    assert relative_path in _git(
        seeded.repo_root,
        "diff",
        "--cached",
        "--name-only",
    ).splitlines()


def test_untracked_root_sqlite_runtime_files_do_not_block_rename(isolated_env) -> None:
    seeded = _seed()
    runtime_names = (
        "storage.sqlite3",
        "storage.sqlite3-journal",
        "storage.sqlite3-shm",
        "storage.sqlite3-wal",
    )
    for name in runtime_names:
        (seeded.repo_root / name).write_bytes(b"runtime-only")

    result = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )

    assert result.exit_code == 0, result.output
    for name in runtime_names:
        assert (seeded.repo_root / name).read_bytes() == b"runtime-only"
        assert _git(seeded.repo_root, "ls-files", "--", name) == ""


@pytest.mark.parametrize("artifact_state", ["staged", "tracked-modified"])
def test_root_sqlite_artifact_changes_still_block_rename(
    isolated_env,
    artifact_state: str,
) -> None:
    seeded = _seed()
    database_path = seeded.repo_root / "storage.sqlite3"
    database_path.write_bytes(b"operator-owned")
    _git(seeded.repo_root, "add", "--", database_path.name)
    if artifact_state == "tracked-modified":
        _git(seeded.repo_root, "commit", "-m", "seed: tracked archive database")
        database_path.write_bytes(b"operator-modified")
    head_before = _git(seeded.repo_root, "rev-parse", "HEAD")

    result = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )

    assert result.exit_code != 0
    assert "clean" in unstyle(result.output).lower()
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == head_before


def test_rename_agent_recovers_db_ahead_when_profile_proves_same_agent(isolated_env) -> None:
    seeded = _seed()

    async def rename_database_first() -> None:
        async with get_session() as session:
            agent = await session.get(Agent, seeded.agent_id)
            assert agent is not None
            agent.name = NEW_NAME
            session.add(agent)
            await session.commit()

    asyncio.run(rename_database_first())
    result = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "database_already_applied" in unstyle(result.output)
    assert not (seeded.archive_root / "agents" / OLD_NAME).exists()
    assert (seeded.archive_root / "agents" / NEW_NAME / "profile.json").is_file()
    assert _meaningful_git_status(seeded.repo_root) == []


def test_migrate_agent_state_is_atomic_backed_up_and_secret_safe(isolated_env, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    granted_dir = state_dir / "granted"
    granted_dir.mkdir(parents=True)
    credential_path = state_dir / "credentials.json"
    credential_path.write_text(
        json.dumps({PROJECT_KEY: {OLD_NAME: REGISTRATION_TOKEN, "peer": "peer-secret"}}) + "\n",
        encoding="utf-8",
    )
    safe_project = PROJECT_KEY.replace("/", "_")
    legacy_granted = granted_dir / safe_project
    legacy_granted.write_text(OLD_NAME, encoding="utf-8")
    transitional_granted = granted_dir / f"{safe_project}--cx-1"
    transitional_granted.write_text(OLD_NAME, encoding="utf-8")
    runner = CliRunner()

    dry_run = runner.invoke(
        app,
        [
            "migrate-agent-state",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--client",
            "codex",
            "--slot",
            "1",
            "--state-dir",
            str(state_dir),
        ],
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert "DRY RUN" in dry_run.output
    assert REGISTRATION_TOKEN not in dry_run.output
    assert json.loads(credential_path.read_text(encoding="utf-8"))[PROJECT_KEY][OLD_NAME] == REGISTRATION_TOKEN

    applied = runner.invoke(
        app,
        [
            "migrate-agent-state",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--client",
            "codex",
            "--slot",
            "1",
            "--state-dir",
            str(state_dir),
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert REGISTRATION_TOKEN not in applied.output
    assert hashlib.sha256(REGISTRATION_TOKEN.encode()).hexdigest() not in applied.output
    credentials = json.loads(credential_path.read_text(encoding="utf-8"))
    assert OLD_NAME not in credentials[PROJECT_KEY]
    assert credentials[PROJECT_KEY][NEW_NAME] == REGISTRATION_TOKEN
    assert credentials[PROJECT_KEY]["peer"] == "peer-secret"
    assert list((state_dir / "backups").glob("credentials.json.*.bak"))
    assert not legacy_granted.exists()
    assert not transitional_granted.exists()
    assert list((state_dir / "backups").glob(f"{transitional_granted.name}.*.bak"))
    translated = re.sub(r"[^A-Za-z0-9._ -]", "_", PROJECT_KEY)
    squeezed = re.sub(r"_+", "_", translated)
    prefix = squeezed.replace(" ", "_")[:47]
    prefix = prefix[:-1] if prefix.endswith("_") else prefix
    prefix = prefix or "state"
    digest = hashlib.sha256(PROJECT_KEY.encode("utf-8")).hexdigest()[:32]
    current_component = f"{prefix}-{digest}"
    assert (
        granted_dir / f"{current_component}--codex-1"
    ).read_text(encoding="utf-8") == NEW_NAME

    retry = runner.invoke(
        app,
        [
            "migrate-agent-state",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--client",
            "codex",
            "--slot",
            "1",
            "--state-dir",
            str(state_dir),
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )
    assert retry.exit_code == 0, retry.output
    assert "already_migrated" in retry.output
    assert REGISTRATION_TOKEN not in retry.output


def test_migrate_agent_state_rejects_ambiguous_lossy_project_component(
    isolated_env,
    tmp_path: Path,
) -> None:
    project = "p" * 96 + "-alpha"
    colliding_project = "p" * 96 + "-beta"
    assert cli_module._agent_state_previous_component(project) == (
        cli_module._agent_state_previous_component(colliding_project)
    )
    assert cli_module._agent_state_component(project) != (
        cli_module._agent_state_component(colliding_project)
    )
    state_dir = tmp_path / "state"
    granted_dir = state_dir / "granted"
    granted_dir.mkdir(parents=True)
    credential_path = state_dir / "credentials.json"
    credential_path.write_text(
        json.dumps(
            {
                project: {OLD_NAME: REGISTRATION_TOKEN},
                colliding_project: {"peer": "foreign-secret"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    legacy_granted = (
        granted_dir / cli_module._agent_state_previous_component(project)
    )
    legacy_granted.write_text(OLD_NAME, encoding="utf-8")
    before = _tree_contents(state_dir)

    result = CliRunner().invoke(
        app,
        [
            "migrate-agent-state",
            project,
            OLD_NAME,
            NEW_NAME,
            "--client",
            "codex",
            "--slot",
            "1",
            "--state-dir",
            str(state_dir),
        ],
    )

    assert result.exit_code != 0
    assert "ambiguous" in unstyle(result.output)
    assert REGISTRATION_TOKEN not in unstyle(result.output)
    assert _tree_contents(state_dir) == before


def test_agent_state_component_exactly_mirrors_shell_squeeze_order() -> None:
    value = "a___b  c/ü"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    assert cli_module._agent_state_component(value) == f"a_b__c-{digest}"


@pytest.mark.parametrize(
    ("exit_code", "open_winerror", "query_error", "expected"),
    [
        pytest.param(259, None, False, True, id="running"),
        pytest.param(0, None, False, False, id="exited"),
        pytest.param(None, 87, False, False, id="missing"),
        pytest.param(None, 5, False, True, id="open-denied"),
        pytest.param(None, None, True, True, id="query-error"),
    ],
)
def test_pid_is_alive_uses_non_signalling_windows_process_query(
    exit_code: int | None,
    open_winerror: int | None,
    query_error: bool,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = 4242
    handle = 31337
    open_calls: list[tuple[int, bool, int]] = []
    query_calls: list[int] = []
    close_calls: list[int] = []
    kill_calls: list[tuple[int, int]] = []

    class FakeWinAPI:
        STILL_ACTIVE = 259

        def OpenProcess(
            self,
            desired_access: int,
            inherit_handle: bool,
            process_id: int,
        ) -> int:
            open_calls.append((desired_access, inherit_handle, process_id))
            if open_winerror is not None:
                error = OSError(f"OpenProcess failed with {open_winerror}")
                cast(Any, error).winerror = open_winerror
                raise error
            return handle

        def GetExitCodeProcess(self, process_handle: int) -> int:
            query_calls.append(process_handle)
            if query_error:
                raise OSError("GetExitCodeProcess failed")
            assert exit_code is not None
            return exit_code

        def CloseHandle(self, process_handle: int) -> None:
            close_calls.append(process_handle)

    fake_winapi = FakeWinAPI()

    def fake_import_module(name: str) -> Any:
        assert name == "_winapi"
        return fake_winapi

    def record_kill(process_id: int, signal_number: int) -> None:
        kill_calls.append((process_id, signal_number))

    monkeypatch.setattr(utils_module.os, "name", "nt")
    monkeypatch.setattr(utils_module.os, "kill", record_kill)
    monkeypatch.setattr(utils_module.importlib, "import_module", fake_import_module)

    # Exercised through every entry point that ever had its own copy of this
    # probe. Calling only one of them is what let the two drift apart: the CLI
    # copy was covered by this test and correct, while the storage copy went
    # uncovered and read access-denied as death.
    for probe in (
        utils_module.pid_is_alive,
        cli_module._pid_is_alive,
        storage_module.AsyncFileLock._pid_alive,
    ):
        open_calls.clear()
        query_calls.clear()
        close_calls.clear()
        kill_calls.clear()

        assert probe(pid) is expected
        assert open_calls == [(0x1000, False, pid)]
        assert kill_calls == []
        if open_winerror is not None:
            assert query_calls == []
            assert close_calls == []
        else:
            assert query_calls == [handle]
            assert close_calls == [handle]


def test_pid_liveness_has_exactly_one_implementation() -> None:
    """Pin the property that the parametrised test above can only sample.

    ``open-denied`` proves the *current* callers agree today. This proves they
    cannot disagree tomorrow without someone deliberately reintroducing a second
    body, which is exactly how the first divergence happened: the knowledge that
    ``OpenProcess`` fails identically for "denied" and "absent" lived in one file
    and the second copy was written without it.
    """
    assert cli_module._pid_is_alive is utils_module.pid_is_alive
    # The storage entry point keeps its own name for its callers, so it cannot be
    # the same object. What it must not do is carry a second body: forwarding
    # shows up as a call to ``pid_is_alive`` and nothing platform-specific.
    storage_probe = storage_module.AsyncFileLock._pid_alive
    assert "pid_is_alive" in storage_probe.__code__.co_names
    assert "OpenProcess" not in storage_probe.__code__.co_names


def test_migrate_agent_state_interoperable_lock_preserves_concurrent_insert(
    isolated_env,
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "state"
    granted_dir = state_dir / "granted"
    granted_dir.mkdir(parents=True)
    credential_path = state_dir / "credentials.json"
    credential_path.write_text(
        json.dumps({PROJECT_KEY: {OLD_NAME: REGISTRATION_TOKEN}}) + "\n",
        encoding="utf-8",
    )
    legacy_granted = granted_dir / cli_module._agent_state_previous_component(
        PROJECT_KEY
    )
    legacy_granted.write_text(OLD_NAME, encoding="utf-8")
    reached_backup = threading.Event()
    continue_migration = threading.Event()
    writer_finished = threading.Event()
    original_backup = cli_module._agent_state_backup

    def blocking_backup(path: Path, root: Path) -> Path:
        if path == credential_path and not reached_backup.is_set():
            reached_backup.set()
            assert continue_migration.wait(timeout=10)
        return original_backup(path, root)

    monkeypatch.setattr(cli_module, "_agent_state_backup", blocking_backup)
    migration_results: list[Any] = []

    def migrate() -> None:
        migration_results.append(
            CliRunner().invoke(
                app,
                [
                    "migrate-agent-state",
                    PROJECT_KEY,
                    OLD_NAME,
                    NEW_NAME,
                    "--client",
                    "codex",
                    "--slot",
                    "1",
                    "--state-dir",
                    str(state_dir),
                    "--apply",
                    "--confirm",
                    f"{OLD_NAME}=>{NEW_NAME}",
                ],
            )
        )

    def insert_foreign_credential() -> None:
        with cli_module._portable_agent_state_lock(credential_path):
            payload = json.loads(credential_path.read_text(encoding="utf-8"))
            payload[PROJECT_KEY]["foreign"] = "foreign-secret"
            credential_path.write_text(
                json.dumps(payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        writer_finished.set()

    migration_thread = threading.Thread(target=migrate)
    migration_thread.start()
    assert reached_backup.wait(timeout=10)
    writer_thread = threading.Thread(target=insert_foreign_credential)
    writer_thread.start()
    assert not writer_finished.wait(timeout=0.1)
    continue_migration.set()
    migration_thread.join(timeout=10)
    writer_thread.join(timeout=10)
    assert not migration_thread.is_alive()
    assert not writer_thread.is_alive()
    assert migration_results
    migration_result = migration_results[0]
    assert migration_result.exit_code == 0, migration_result.output
    assert REGISTRATION_TOKEN not in unstyle(migration_result.output)
    assert "foreign-secret" not in unstyle(migration_result.output)
    credentials = json.loads(credential_path.read_text(encoding="utf-8"))
    assert OLD_NAME not in credentials[PROJECT_KEY]
    assert credentials[PROJECT_KEY][NEW_NAME] == REGISTRATION_TOKEN
    assert credentials[PROJECT_KEY]["foreign"] == "foreign-secret"


def test_rename_agent_requires_exact_confirmation_and_stopped_server(isolated_env, tmp_path: Path) -> None:
    _seed()
    wrong_confirmation = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            "yes",
        ],
    )
    assert wrong_confirmation.exit_code != 0
    assert f"{OLD_NAME}=>{NEW_NAME}" in unstyle(wrong_confirmation.output)

    from filelock import FileLock

    lock = FileLock(str(Path(get_settings().storage.root) / "server.lock"))
    lock.acquire(timeout=0)
    try:
        active_server = CliRunner().invoke(
            app,
            [
                "rename-agent",
                PROJECT_KEY,
                OLD_NAME,
                NEW_NAME,
                "--apply",
                "--confirm",
                f"{OLD_NAME}=>{NEW_NAME}",
            ],
        )
    finally:
        lock.release()
    assert active_server.exit_code != 0
    assert "operator-stopped" in unstyle(active_server.output)


def test_rename_agent_requires_persisted_token_and_clean_archive(isolated_env) -> None:
    seeded = _seed()

    async def clear_token() -> None:
        async with get_session() as session:
            agent = await session.get(Agent, seeded.agent_id)
            assert agent is not None
            agent.registration_token = None
            session.add(agent)
            await session.commit()

    asyncio.run(clear_token())
    reset_database_state()
    missing_token = CliRunner().invoke(app, ["rename-agent", PROJECT_KEY, OLD_NAME, NEW_NAME])
    assert missing_token.exit_code != 0
    assert "persisted registration token" in unstyle(missing_token.output).lower()

    async def restore_token() -> None:
        async with get_session() as session:
            agent = await session.get(Agent, seeded.agent_id)
            assert agent is not None
            agent.registration_token = REGISTRATION_TOKEN
            session.add(agent)
            await session.commit()

    asyncio.run(restore_token())
    reset_database_state()
    dirty_path = seeded.archive_root / "operator-note.txt"
    dirty_path.write_text("uncommitted\n", encoding="utf-8")
    dirty = CliRunner().invoke(app, ["rename-agent", PROJECT_KEY, OLD_NAME, NEW_NAME])
    assert dirty.exit_code != 0
    assert "clean" in unstyle(dirty.output).lower()


def test_db_ahead_collision_is_rejected_without_matching_legacy_profile_id(isolated_env) -> None:
    seeded = _seed(target_collision=NEW_NAME)

    async def remove_source_row_only() -> None:
        async with get_session() as session:
            source = await session.get(Agent, seeded.agent_id)
            assert source is not None
            await session.delete(source)
            await session.commit()

    # SQLite may enforce the existing foreign keys depending on the test
    # connection's pragma, so use a direct name change to an unrelated identity
    # instead of deleting relationship-bearing evidence.
    async def rename_source_elsewhere() -> None:
        async with get_session() as session:
            source = await session.get(Agent, seeded.agent_id)
            assert source is not None
            source.name = "claude-linux-different-1"
            session.add(source)
            await session.commit()

    del remove_source_row_only
    asyncio.run(rename_source_elsewhere())
    reset_database_state()
    result = CliRunner().invoke(app, ["rename-agent", PROJECT_KEY, OLD_NAME, NEW_NAME])
    assert result.exit_code != 0
    assert "collision" in unstyle(result.output).lower()
    assert _git(seeded.repo_root, "rev-parse", "HEAD") == seeded.seed_commit


def test_relationship_ids_are_stable_across_rename_at_sql_level(isolated_env) -> None:
    seeded = _seed()
    before: dict[str, list[tuple[Any, ...]]] = {}

    async def snapshot_relationships(target: dict[str, list[tuple[Any, ...]]]) -> None:
        statements = {
            "messages": "SELECT id, sender_id FROM messages ORDER BY id",
            "recipients": "SELECT message_id, agent_id, read_ts, ack_ts FROM message_recipients ORDER BY message_id, agent_id",
            "reservations": "SELECT id, agent_id FROM file_reservations ORDER BY id",
            "links": "SELECT id, a_agent_id, b_agent_id FROM agent_links ORDER BY id",
        }
        async with get_session() as session:
            for key, statement in statements.items():
                target[key] = [tuple(row) for row in (await session.execute(text(statement))).all()]

    asyncio.run(snapshot_relationships(before))
    result = CliRunner().invoke(
        app,
        [
            "rename-agent",
            PROJECT_KEY,
            OLD_NAME,
            NEW_NAME,
            "--apply",
            "--confirm",
            f"{OLD_NAME}=>{NEW_NAME}",
        ],
    )
    assert result.exit_code == 0, result.output
    after: dict[str, list[tuple[Any, ...]]] = {}
    asyncio.run(snapshot_relationships(after))
    assert after == before
    assert seeded.agent_id in {row[1] for row in after["messages"]}
