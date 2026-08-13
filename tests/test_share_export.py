from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import threading
import urllib.request
import warnings
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from typer.testing import CliRunner

import mcp_agent_mail.share as share
from mcp_agent_mail import cli as cli_module
from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.share import (
    SCRUB_PRESETS,
    ShareExportError,
    build_materialized_views,
    bundle_attachments,
    create_performance_indexes,
    finalize_snapshot_for_export,
    maybe_chunk_database,
    scrub_snapshot,
    summarize_snapshot,
)

warnings.filterwarnings("ignore", category=ResourceWarning)

pytestmark = pytest.mark.filterwarnings("ignore:.*ResourceWarning")


class _ViewerAssetReferenceCollector(HTMLParser):
    """Collect browser-active asset references from the standalone viewer."""

    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []
        self.csp = ""

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag in {"audio", "embed", "iframe", "img", "script", "source", "video"}:
            source = attributes.get("src")
            if source is not None:
                self.references.append(source)
        if tag == "link":
            href = attributes.get("href")
            if href is not None:
                self.references.append(href)
        http_equiv = attributes.get("http-equiv")
        if (
            tag == "meta"
            and http_equiv is not None
            and http_equiv.lower() == "content-security-policy"
        ):
            self.csp = attributes.get("content") or ""


def _build_snapshot(tmp_path: Path) -> Path:
    snapshot = tmp_path / "snapshot.sqlite3"
    conn = sqlite3.connect(snapshot)
    try:
        conn.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                contact_policy TEXT DEFAULT 'auto'
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                sender_id INTEGER,
                thread_id TEXT,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                ack_required INTEGER,
                created_ts TEXT,
                attachments TEXT
            );
            CREATE TABLE message_recipients (
                message_id INTEGER,
                agent_id INTEGER,
                kind TEXT,
                read_ts TEXT,
                ack_ts TEXT
            );
            CREATE TABLE file_reservations (id INTEGER PRIMARY KEY, project_id INTEGER);
            CREATE TABLE agent_links (
                id INTEGER PRIMARY KEY,
                a_project_id INTEGER,
                b_project_id INTEGER
            );
            CREATE TABLE project_sibling_suggestions (
                id INTEGER PRIMARY KEY,
                project_a_id INTEGER,
                project_b_id INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO projects (id, slug, human_key) VALUES (1, 'demo', 'demo-human')"
        )
        conn.execute(
            "INSERT INTO agents (id, project_id, name) VALUES (1, 1, 'Alice Agent')"
        )
        attachments = [
            {
                "type": "file",
                "path": "attachments/raw/secret.txt",
                "media_type": "text/plain",
                "download_url": "https://example.com/private?token=ghp_secret",
                "authorization": "Bearer " + "C" * 24,
            }
        ]
        conn.execute(
            """
            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (1, 1, 1, 'thread-1', ?, ?, 'normal', 1, '2025-01-01T00:00:00Z', ?)
            """,
            (
                "Token sk-" + "A" * 24,
                "Body bearer " + "B" * 24,
                json.dumps(attachments),
            ),
        )
        conn.execute(
            "INSERT INTO message_recipients (message_id, agent_id, kind, read_ts, ack_ts) VALUES (1, 1, 'to', '2025-01-01', '2025-01-02')"
        )
        conn.execute(
            "INSERT INTO file_reservations (id, project_id) VALUES (1, 1)"
        )
        conn.execute(
            "INSERT INTO agent_links (id, a_project_id, b_project_id) VALUES (1, 1, 1)"
        )
        conn.commit()
    finally:
        conn.close()
    return snapshot


def _build_multi_project_snapshot(tmp_path: Path) -> Path:
    snapshot = tmp_path / "multi.sqlite3"
    conn = sqlite3.connect(snapshot)
    try:
        conn.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                contact_policy TEXT DEFAULT 'auto'
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                sender_id INTEGER,
                thread_id TEXT,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                ack_required INTEGER,
                created_ts TEXT,
                attachments TEXT
            );
            CREATE TABLE message_recipients (
                message_id INTEGER,
                agent_id INTEGER,
                kind TEXT,
                read_ts TEXT,
                ack_ts TEXT
            );
            CREATE TABLE file_reservations (id INTEGER PRIMARY KEY, project_id INTEGER);
            CREATE TABLE agent_links (
                id INTEGER PRIMARY KEY,
                a_project_id INTEGER,
                b_project_id INTEGER
            );
            CREATE TABLE project_sibling_suggestions (
                id INTEGER PRIMARY KEY,
                project_a_id INTEGER,
                project_b_id INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO projects (id, slug, human_key) VALUES (1, 'alpha', '/repo/alpha')"
        )
        conn.execute(
            "INSERT INTO projects (id, slug, human_key) VALUES (2, 'beta', '/repo/beta')"
        )
        conn.execute(
            "INSERT INTO agents (id, project_id, name) VALUES (1, 1, 'Alpha Agent')"
        )
        conn.execute(
            "INSERT INTO agents (id, project_id, name) VALUES (2, 2, 'Beta Agent')"
        )
        conn.execute(
            """
            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (1, 1, 1, 'alpha-thread', 'Alpha', 'Alpha body', 'normal', 0, '2025-01-01T00:00:00Z', '[]')
            """
        )
        conn.execute(
            """
            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (2, 2, 2, 'beta-thread', 'Beta', 'Beta body', 'normal', 0, '2025-01-02T00:00:00Z', '[]')
            """
        )
        conn.execute(
            "INSERT INTO message_recipients (message_id, agent_id, kind, read_ts, ack_ts) VALUES (1, 1, 'to', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO message_recipients (message_id, agent_id, kind, read_ts, ack_ts) VALUES (2, 2, 'to', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO file_reservations (id, project_id) VALUES (1, 1)"
        )
        conn.execute(
            "INSERT INTO file_reservations (id, project_id) VALUES (2, 2)"
        )
        conn.execute(
            "INSERT INTO agent_links (id, a_project_id, b_project_id) VALUES (1, 1, 2)"
        )
        conn.execute(
            "INSERT INTO project_sibling_suggestions (id, project_a_id, project_b_id) VALUES (1, 1, 2)"
        )
        conn.commit()
    finally:
        conn.close()
    return snapshot


def _build_private_runtime_snapshot(tmp_path: Path) -> Path:
    snapshot = tmp_path / "private-runtime.sqlite3"
    conn = sqlite3.connect(snapshot)
    try:
        conn.executescript(
            """
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                slug TEXT,
                human_key TEXT,
                project_generation TEXT
            );
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                registration_token TEXT,
                agent_generation TEXT,
                task_description TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                sender_id INTEGER,
                thread_id TEXT,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                ack_required INTEGER,
                created_ts TEXT,
                attachments TEXT,
                delivery_id TEXT
            );
            CREATE TABLE message_recipients (
                message_id INTEGER,
                agent_id INTEGER,
                kind TEXT,
                read_ts TEXT,
                ack_ts TEXT
            );
            CREATE TABLE ui_users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                password_hash TEXT,
                session_generation TEXT
            );
            CREATE TABLE message_deliveries (
                id TEXT PRIMARY KEY,
                body_md TEXT,
                archive_document TEXT,
                attachments TEXT,
                idempotency_key TEXT,
                last_error TEXT
            );
            CREATE TABLE future_secret_table (secret_value TEXT);
            """
        )
        conn.executemany(
            "INSERT INTO projects (id, slug, human_key, project_generation) VALUES (?, ?, ?, ?)",
            [
                (1, "alpha", "/private/PRIVATE_ALPHA_PATH_CANARY", "a" * 64),
                (2, "beta", "/private/PRIVATE_BETA_PATH_CANARY", "b" * 64),
            ],
        )
        conn.executemany(
            """
            INSERT INTO agents (
                id, project_id, name, registration_token, agent_generation, task_description
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    1,
                    "ExternalSender",
                    "AGENT_TOKEN_CANARY",
                    "c" * 64,
                    "PRIVATE_TASK_CANARY",
                ),
                (2, 2, "BetaRecipient", "SECOND_TOKEN_CANARY", "d" * 64, "Safe task"),
            ],
        )
        conn.execute(
            """
            INSERT INTO messages (
                id, project_id, sender_id, thread_id, subject, body_md,
                importance, ack_required, created_ts, attachments, delivery_id
            ) VALUES (10, 2, 1, ?, 'Viewer subject', 'Viewer body',
                      'normal', 1, '2026-08-13T00:00:00Z', '[]', 'delivery-private')
            """,
            ("thread-sk-" + "Z" * 24,),
        )
        conn.execute(
            """
            INSERT INTO message_recipients (message_id, agent_id, kind, read_ts, ack_ts)
            VALUES (10, 2, 'to', 'PRIVATE_READ_CANARY', 'PRIVATE_ACK_CANARY')
            """
        )
        conn.execute(
            """
            INSERT INTO ui_users (id, username, password_hash, session_generation)
            VALUES (1, 'operator', 'PASSWORD_HASH_CANARY', 'SESSION_GENERATION_CANARY')
            """
        )
        conn.execute(
            """
            INSERT INTO message_deliveries (
                id, body_md, archive_document, attachments, idempotency_key, last_error
            ) VALUES (
                'delivery-private', 'DELIVERY_BODY_CANARY', 'ARCHIVE_DOCUMENT_CANARY',
                '[{"secret":"DELIVERY_ATTACHMENT_CANARY"}]',
                'IDEMPOTENCY_CANARY', 'DELIVERY_ERROR_CANARY'
            )
            """
        )
        conn.execute(
            "INSERT INTO future_secret_table (secret_value) VALUES ('FUTURE_SCHEMA_CANARY')"
        )
        conn.commit()
    finally:
        conn.close()
    return snapshot


def _read_message(snapshot: Path) -> tuple[str, str, list[dict[str, object]]]:
    conn = sqlite3.connect(snapshot)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT subject, body_md, attachments FROM messages WHERE id = 1").fetchone()
        attachments_raw = row["attachments"]
        attachments = json.loads(attachments_raw) if attachments_raw else []
        return row["subject"], row["body_md"], attachments
    finally:
        conn.close()


def test_apply_project_scope_dedup_and_removes(tmp_path: Path) -> None:
    snapshot = _build_multi_project_snapshot(tmp_path)

    result = share.apply_project_scope(snapshot, ["ALPHA", " alpha ", "ALPHA"])

    assert len(result.projects) == 1
    assert result.projects[0].slug == "alpha"
    assert result.removed_count == 1

    conn = sqlite3.connect(snapshot)
    try:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM message_recipients").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM file_reservations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM project_sibling_suggestions").fetchone()[0] == 0
    finally:
        conn.close()


def test_viewer_snapshot_is_a_fresh_allowlist_without_private_runtime_data(
    tmp_path: Path,
) -> None:
    source = _build_private_runtime_snapshot(tmp_path)
    snapshot = tmp_path / "public-viewer.sqlite3"

    context = share.create_snapshot_context(
        source_database=source,
        snapshot_path=snapshot,
        project_filters=["beta"],
        scrub_preset="standard",
        purpose="viewer_export",
    )

    assert [(project.slug, project.human_key) for project in context.scope.projects] == [
        ("beta", "beta")
    ]
    assert context.scope.removed_count == 1

    conn = sqlite3.connect(snapshot)
    try:
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "ui_users" not in table_names
        assert "message_deliveries" not in table_names
        assert "future_secret_table" not in table_names
        assert {
            "projects",
            "agents",
            "messages",
            "message_recipients",
            "message_overview_mv",
            "attachments_by_message_mv",
        }.issubset(table_names)
        assert {
            row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
        } == {"id", "slug", "human_key"}
        assert {
            row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()
        } == {"id", "project_id", "name", "origin_project_slug"}
        assert conn.execute(
            "SELECT slug, human_key FROM projects"
        ).fetchall() == [("beta", "beta")]
        assert conn.execute(
            "SELECT id, project_id, name, origin_project_slug FROM agents ORDER BY id"
        ).fetchall() == [
            (1, None, "ExternalSender", "alpha"),
            (2, 2, "BetaRecipient", None),
        ]
        assert conn.execute(
            "SELECT sender_id, subject, body_md FROM messages"
        ).fetchall() == [(1, "Viewer subject", "Viewer body")]
        assert conn.execute(
            "SELECT message_id, agent_id FROM message_recipients"
        ).fetchall() == [(10, 2)]
        assert conn.execute(
            """
            SELECT sender_display, sender_project_slug, sender_project_name, sender_address
            FROM message_overview_mv
            """
        ).fetchall() == [
            (
                "ExternalSender@alpha",
                "alpha",
                "alpha",
                "project:alpha#ExternalSender",
            )
        ]
    finally:
        conn.close()

    snapshot_bytes = snapshot.read_bytes()
    for canary in (
        b"PRIVATE_ALPHA_PATH_CANARY",
        b"PRIVATE_BETA_PATH_CANARY",
        b"AGENT_TOKEN_CANARY",
        b"SECOND_TOKEN_CANARY",
        b"PRIVATE_TASK_CANARY",
        b"PRIVATE_READ_CANARY",
        b"PRIVATE_ACK_CANARY",
        b"PASSWORD_HASH_CANARY",
        b"SESSION_GENERATION_CANARY",
        b"DELIVERY_BODY_CANARY",
        b"ARCHIVE_DOCUMENT_CANARY",
        b"DELIVERY_ATTACHMENT_CANARY",
        b"IDEMPOTENCY_CANARY",
        b"DELIVERY_ERROR_CANARY",
        b"FUTURE_SCHEMA_CANARY",
        b"sk-" + b"Z" * 24,
    ):
        assert canary not in snapshot_bytes


def test_recovery_snapshot_is_explicit_lossless_and_unfiltered(tmp_path: Path) -> None:
    source = _build_private_runtime_snapshot(tmp_path)
    snapshot = tmp_path / "private-recovery.sqlite3"
    source_conn = sqlite3.connect(source)
    try:
        source_schema = source_conn.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        source_dump = list(source_conn.iterdump())
    finally:
        source_conn.close()

    context = share.create_snapshot_context(
        source_database=source,
        snapshot_path=snapshot,
        project_filters=(),
        scrub_preset="archive",
        purpose="recovery_archive",
    )

    assert {project.slug for project in context.scope.projects} == {"alpha", "beta"}
    conn = sqlite3.connect(snapshot)
    try:
        assert conn.execute(
            "SELECT registration_token FROM agents WHERE id = 1"
        ).fetchone() == ("AGENT_TOKEN_CANARY",)
        assert conn.execute(
            "SELECT password_hash, session_generation FROM ui_users"
        ).fetchone() == ("PASSWORD_HASH_CANARY", "SESSION_GENERATION_CANARY")
        assert conn.execute(
            "SELECT body_md, archive_document FROM message_deliveries"
        ).fetchone() == ("DELIVERY_BODY_CANARY", "ARCHIVE_DOCUMENT_CANARY")
        assert conn.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall() == source_schema
        assert list(conn.iterdump()) == source_dump
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'fts_messages'"
        ).fetchone() is None
    finally:
        conn.close()

    with pytest.raises(ShareExportError, match="cannot be used for a public viewer"):
        share.create_snapshot_context(
            source_database=source,
            snapshot_path=tmp_path / "invalid-public.sqlite3",
            project_filters=(),
            scrub_preset="archive",
            purpose="viewer_export",
        )
    with pytest.raises(ShareExportError, match="must include the complete database"):
        share.create_snapshot_context(
            source_database=source,
            snapshot_path=tmp_path / "invalid-filtered-recovery.sqlite3",
            project_filters=("beta",),
            scrub_preset="archive",
            purpose="recovery_archive",
        )


def test_complete_public_bundle_and_zip_exclude_private_canaries(tmp_path: Path) -> None:
    source = _build_private_runtime_snapshot(tmp_path)
    storage_root = tmp_path / "PRIVATE_STORAGE_ROOT_CANARY"
    attachment_path = storage_root / "attachments" / "PRIVATE_FILE_PATH_CANARY.txt"
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_text("PRIVATE_ATTACHMENT_BYTES_CANARY", encoding="utf-8")
    conn = sqlite3.connect(source)
    try:
        conn.execute(
            "UPDATE messages SET attachments = ? WHERE id = 10",
            (
                json.dumps(
                    [
                        {
                            "type": "file",
                            "path": str(attachment_path),
                            "media_type": "text/plain",
                        }
                    ]
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    repo_root = tmp_path / "public-repo"
    bundle_root = repo_root / "docs" / "bundle"
    bundle_root.mkdir(parents=True)
    git_dir = repo_root / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        '[remote "origin"]\n'
        "    url = https://PRIVATE_REMOTE_USER_CANARY:PRIVATE_REMOTE_TOKEN_CANARY@"
        "github.com/PRIVATE_REMOTE_OWNER_CANARY/PRIVATE_REMOTE_REPO_CANARY.git\n",
        encoding="utf-8",
    )

    snapshot = bundle_root / "mailbox.sqlite3"
    context = share.create_snapshot_context(
        source_database=source,
        snapshot_path=snapshot,
        project_filters=["/private/PRIVATE_BETA_PATH_CANARY"],
        scrub_preset="standard",
        purpose="viewer_export",
    )
    hints = share.detect_hosting_hints(bundle_root)
    share.build_bundle_assets(
        snapshot,
        bundle_root,
        storage_root=storage_root,
        inline_threshold=32,
        detach_threshold=512,
        chunk_threshold=1 << 30,
        chunk_size=1024,
        scope=context.scope,
        project_filters=["/private/PRIVATE_BETA_PATH_CANARY"],
        scrub_summary=context.scrub_summary,
        hosting_hints=hints,
        fts_enabled=context.fts_enabled,
        export_config={
            "projects": ["/private/PRIVATE_BETA_PATH_CANARY"],
            "scrub_preset": "standard",
        },
    )
    archive_path = share.package_directory_as_zip(bundle_root, tmp_path / "public-bundle.zip")

    canaries = (
        b"PRIVATE_ALPHA_PATH_CANARY",
        b"PRIVATE_BETA_PATH_CANARY",
        b"AGENT_TOKEN_CANARY",
        b"PASSWORD_HASH_CANARY",
        b"DELIVERY_BODY_CANARY",
        b"ARCHIVE_DOCUMENT_CANARY",
        b"FUTURE_SCHEMA_CANARY",
        b"PRIVATE_STORAGE_ROOT_CANARY",
        b"PRIVATE_FILE_PATH_CANARY",
        b"PRIVATE_ATTACHMENT_BYTES_CANARY",
        b"PRIVATE_REMOTE_USER_CANARY",
        b"PRIVATE_REMOTE_TOKEN_CANARY",
        b"PRIVATE_REMOTE_OWNER_CANARY",
        b"PRIVATE_REMOTE_REPO_CANARY",
    )
    for public_file in (path for path in bundle_root.rglob("*") if path.is_file()):
        public_bytes = public_file.read_bytes()
        for canary in canaries:
            assert canary not in public_bytes, public_file
    with ZipFile(archive_path) as archive:
        assert all(not name.startswith((".git/", ".github/")) for name in archive.namelist())
        for name in archive.namelist():
            archived_bytes = archive.read(name)
            for canary in canaries:
                assert canary not in archived_bytes, name


def test_detect_hosting_hints_sort_order(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(share, "_find_repo_root", lambda _start: None)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("CF_PAGES", "1")
    monkeypatch.setenv("NETLIFY", "true")
    monkeypatch.setenv("AWS_S3_BUCKET", "bucket-name")

    hints = share.detect_hosting_hints(tmp_path)
    assert [hint.key for hint in hints] == [
        "github_pages",
        "cloudflare_pages",
        "netlify",
        "s3",
    ]


@pytest.mark.parametrize("source_mode", ["live", "package"])
def test_copy_viewer_assets_excludes_python_runtime_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_mode: str,
) -> None:
    """Neither live nor packaged asset traversal may export Python bytecode."""
    package_dir = tmp_path / "live-package"
    synthetic_assets = (
        package_dir / "viewer_assets"
        if source_mode == "live"
        else tmp_path / "synthetic-assets"
    )
    asset_contents = {
        "index.html": "viewer",
        "helpers.py": "# package marker",
        "scripts/app.js": "console.log('viewer')",
        "vendor/clusterize.min.css": "historical GPL source",
        "vendor/clusterize.min.js": "historical GPL source",
        "orphan.pyc": "compiled",
        "orphan.PYO": "optimized",
        "__pycache__/helpers.cpython-314.pyc": "cached",
        "scripts/__pycache__/app.cpython-314.pyc": "nested cache",
    }
    for relative, contents in asset_contents.items():
        asset_path = synthetic_assets / relative
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_text(contents, encoding="utf-8")

    monkeypatch.setattr(
        share,
        "_verify_viewer_vendor_assets",
        lambda *_args, **_kwargs: None,
    )
    if source_mode == "live":
        monkeypatch.setattr(share, "__file__", str(package_dir / "share.py"))
    else:
        monkeypatch.setattr(
            share,
            "__file__",
            str(tmp_path / "installed-package" / "share.py"),
        )
        monkeypatch.setattr(share.resources, "files", lambda _package: synthetic_assets)

    output_dir = tmp_path / "bundle"
    share.copy_viewer_assets(output_dir)

    viewer_root = output_dir / "viewer"
    exported = {
        path.relative_to(viewer_root).as_posix()
        for path in viewer_root.rglob("*")
        if path.is_file()
    }
    assert exported == {"helpers.py", "index.html", "scripts/app.js"}
    assert not (viewer_root / "__pycache__").exists()
    assert not (viewer_root / "scripts" / "__pycache__").exists()


@pytest.mark.parametrize("source_mode", ["live", "package"])
def test_copy_viewer_assets_is_self_contained_and_air_gapped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_mode: str,
) -> None:
    """Source-tree and installed-package exports use only bundled resources."""
    source_tree = Path(share.__file__).parent / "viewer_assets"
    if source_mode == "package":
        monkeypatch.setattr(
            share,
            "__file__",
            str(tmp_path / "installed-package" / "share.py"),
        )
        monkeypatch.setattr(share.resources, "files", lambda _package: source_tree)

    output_dir = tmp_path / "bundle"
    share.copy_viewer_assets(output_dir)

    viewer_root = output_dir / "viewer"
    index_text = (viewer_root / "index.html").read_text(encoding="utf-8")
    collector = _ViewerAssetReferenceCollector()
    collector.feed(index_text)

    assert "<title>Iris · Agent Mail Viewer</title>" in index_text
    assert '<span aria-hidden="true">🌈</span> Iris' in index_text
    assert "%26%23x1F308%3B" in index_text
    assert collector.csp
    assert "https:" not in collector.csp
    assert "http:" not in collector.csp
    assert all(
        not reference.startswith(("http://", "https://", "//"))
        for reference in collector.references
    )
    assert "./preview-reload.js" not in collector.references
    assert all("clusterize" not in reference.lower() for reference in collector.references)
    assert not (viewer_root / "vendor" / "clusterize.min.css").exists()
    assert not (viewer_root / "vendor" / "clusterize.min.js").exists()

    expected_vendor_hashes = {
        "alpine.min.js": "358d9afbb1ab5befa2f48061a30776e5bcd7707f410a606ba985f98bc3b1c034",
        "lucide.min.js": "e47754dcfb8e1d354d7da3dbd2ddc2d4ae3ef4065e34582fbced11737e29bea1",
        "tailwind.min.css": "d832a38b699b0ced1ccb9ad1598036601a5f5c32c24d7cbf78084fedeac3d482",
    }
    for filename, expected_digest in expected_vendor_hashes.items():
        asset = viewer_root / "vendor" / filename
        assert asset.is_file()
        assert hashlib.sha256(asset.read_bytes()).hexdigest() == expected_digest

    license_text = (viewer_root / "THIRD_PARTY_LICENSES.txt").read_text(
        encoding="utf-8"
    )
    assert "Alpine.js 3.14.1" in license_text
    assert "Lucide 0.474.0" in license_text
    assert "Tailwind CSS 3.4.17" in license_text
    assert "sql.js 1.10.1" in license_text
    assert "Marked 11.0.0" in license_text
    assert "DOMPurify 3.0.8" in license_text
    assert "Clusterize.js 0.18.0 - repository-only historical source" in license_text
    assert "Distribution status: excluded from the standalone viewer" in license_text
    assert "coi-serviceworker 0.1.7" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert "Mozilla Public License Version 2.0" in license_text
    assert "GNU GENERAL PUBLIC LICENSE\n                       Version 3" in license_text

    vendor_manifest = json.loads(
        (viewer_root / "vendor_manifest.json").read_text(encoding="utf-8")
    )
    expected_licenses = {
        "sql_js": "MIT",
        "marked": "MIT AND BSD-3-Clause",
        "dompurify": "Apache-2.0 OR MPL-2.0",
        "alpinejs": "MIT",
        "lucide": "ISC",
        "tailwindcss": "MIT",
        "coi_serviceworker": "MIT",
    }
    for component, expected_license in expected_licenses.items():
        assert vendor_manifest[component]["license"] == expected_license
        assert vendor_manifest[component]["license_file"] == (
            "THIRD_PARTY_LICENSES.txt"
        )
    assert "clusterize" not in vendor_manifest
    coi_asset = viewer_root / vendor_manifest["coi_serviceworker"]["asset"]["path"]
    assert hashlib.sha256(coi_asset.read_bytes()).hexdigest() == (
        vendor_manifest["coi_serviceworker"]["asset"]["sha256"]
    )

    assert "./vendor/alpine.min.js" in collector.references
    assert "./vendor/lucide.min.js" in collector.references
    assert "./vendor/tailwind.min.css" in collector.references
    assert all(
        b"sourceMappingURL" not in asset.read_bytes()
        for asset in (viewer_root / "vendor").glob("*.js")
    )


def test_checksum_verified_viewer_assets_are_not_rewritten_by_git() -> None:
    """Windows checkout must preserve the exact bytes pinned in the manifest."""
    repository_root = Path(__file__).resolve().parents[1]
    asset_paths = [
        "src/mcp_agent_mail/viewer_assets/coi-serviceworker.js",
        "src/mcp_agent_mail/viewer_assets/vendor/alpine.min.js",
        "src/mcp_agent_mail/viewer_assets/vendor/lucide.min.js",
    ]

    result = subprocess.run(
        ["git", "check-attr", "text", "--", *asset_paths],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        f"{asset_path}: text: unset" for asset_path in asset_paths
    ]


def test_wheel_and_sdist_exclude_repository_only_clusterize(tmp_path: Path) -> None:
    """Built Python distributions must never contain the retained GPL sources."""
    repository_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "uv",
            "build",
            "--no-progress",
            "--no-create-gitignore",
            "--out-dir",
            str(tmp_path),
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheel = next(tmp_path.glob("*.whl"))
    sdist = next(tmp_path.glob("*.tar.gz"))
    with ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
    with tarfile.open(sdist, mode="r:gz") as archive:
        sdist_names = archive.getnames()

    for artifact_names in (wheel_names, sdist_names):
        normalized = [name.lower() for name in artifact_names]
        assert not any("clusterize" in name for name in normalized)
        assert any(name.endswith("viewer_assets/index.html") for name in normalized)
        assert any(name.endswith("viewer_assets/viewer.js") for name in normalized)
        assert any(name.endswith("viewer_assets/vendor_manifest.json") for name in normalized)
        assert any(
            name.endswith("viewer_assets/third_party_licenses.txt")
            for name in normalized
        )
        assert any(name.endswith("ui_dist/index.html") for name in normalized)
        assert any(name.endswith("ui_dist/.hermes-ui-build.json") for name in normalized)

    no_node_path = tmp_path / "no-node-path"
    no_node_path.mkdir()
    wheel_from_sdist = tmp_path / "wheel-from-sdist"
    clean_environment = os.environ.copy()
    clean_environment["PATH"] = str(no_node_path)
    result = subprocess.run(
        [
            str(Path(shutil.which("uv") or "uv")),
            "build",
            "--wheel",
            "--no-progress",
            "--no-create-gitignore",
            "--out-dir",
            str(wheel_from_sdist),
            str(sdist),
        ],
        cwd=repository_root,
        env=clean_environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    node_free_wheel = next(wheel_from_sdist.glob("*.whl"))
    assert node_free_wheel.is_file()
    with ZipFile(node_free_wheel) as archive:
        node_free_names = [name.lower() for name in archive.namelist()]
    assert any(name.endswith("ui_dist/index.html") for name in node_free_names)
    assert any(
        name.endswith("ui_dist/.hermes-ui-build.json") for name in node_free_names
    )


def test_detect_hosting_hints_uses_output_dir_repo_when_cwd_elsewhere(
    monkeypatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "bundle-repo"
    output_dir = repo_root / "docs" / "mailbox"
    output_dir.mkdir(parents=True)
    git_dir = repo_root / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        '\n'.join(
            [
                '[remote "origin"]',
                "    url = git@github.com:example/shared-mailbox.git",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    workflows_dir = repo_root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "pages.yml").write_text("name: github-pages\n", encoding="utf-8")

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    monkeypatch.chdir(outside_dir)
    for key in (
        "GITHUB_REPOSITORY",
        "CF_PAGES",
        "CF_ACCOUNT_ID",
        "NETLIFY",
        "NETLIFY_SITE_ID",
        "AWS_S3_BUCKET",
        "AWS_BUCKET",
    ):
        monkeypatch.delenv(key, raising=False)

    hints = share.detect_hosting_hints(output_dir)

    github_hint = next(hint for hint in hints if hint.key == "github_pages")
    assert "GitHub remote detected" in github_hint.signals
    assert "Workflow pages.yml references Pages" in github_hint.signals
    assert "Export path inside docs/ directory" in github_hint.signals


def test_detect_hosting_hints_reads_remotes_from_gitfile_worktree(
    monkeypatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "worktree-bundle"
    repo_root.mkdir()
    output_dir = repo_root / "bundle"
    output_dir.mkdir()

    git_dir = repo_root / ".git-data"
    worktree_git_dir = git_dir / "worktrees" / "bundle"
    worktree_git_dir.mkdir(parents=True)
    (repo_root / ".git").write_text("gitdir: .git-data/worktrees/bundle\n", encoding="utf-8")
    (worktree_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (git_dir / "config").write_text(
        '\n'.join(
            [
                '[remote "origin"]',
                "    url = https://PRIVATE_USER_CANARY:PRIVATE_TOKEN_CANARY@github.com/PRIVATE_OWNER_CANARY/PRIVATE_REPO_CANARY.git?secret=PRIVATE_QUERY_CANARY",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    outside_dir = tmp_path / "outside-worktree"
    outside_dir.mkdir()
    monkeypatch.chdir(outside_dir)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    hints = share.detect_hosting_hints(output_dir)

    github_hint = next(hint for hint in hints if hint.key == "github_pages")
    assert "GitHub remote detected" in github_hint.signals
    serialized_hints = json.dumps([hint.signals for hint in hints])
    for canary in (
        "PRIVATE_USER_CANARY",
        "PRIVATE_TOKEN_CANARY",
        "PRIVATE_OWNER_CANARY",
        "PRIVATE_REPO_CANARY",
        "PRIVATE_QUERY_CANARY",
    ):
        assert canary not in serialized_hints


def test_load_bundle_export_config_preserves_explicit_zero_thresholds(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    manifest = {
        "export_config": {
            "projects": ["demo"],
            "inline_threshold": 0,
            "detach_threshold": 0,
            "chunk_threshold": 1024,
            "chunk_size": 65536,
            "scrub_preset": "strict",
        },
        "project_scope": {"requested": ["fallback"]},
        "attachments": {"config": {}},
        "scrub": {"preset": "standard"},
        "database": {},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    config = cli_module._load_bundle_export_config(bundle_dir)

    assert config.projects == ["demo"]
    assert config.inline_threshold == 0
    assert config.detach_threshold == 0
    assert config.chunk_threshold == 1024
    assert config.chunk_size == 65536
    assert config.scrub_preset == "strict"


def test_share_update_removes_stale_signature_and_reports_it(
    monkeypatch, tmp_path: Path
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    manifest = {
        "export_config": {
            "projects": ["demo"],
            "inline_threshold": 64,
            "detach_threshold": 1024,
            "chunk_threshold": 20 * 1024 * 1024,
            "chunk_size": 4 * 1024 * 1024,
            "scrub_preset": "standard",
        },
        "project_scope": {"requested": ["demo"]},
        "attachments": {"config": {"inline_threshold": 64, "detach_threshold": 1024}},
        "scrub": {"preset": "standard"},
        "database": {},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (bundle_dir / "manifest.sig.json").write_text('{"stale": true}\n', encoding="utf-8")

    database_path = tmp_path / "mailbox.sqlite3"
    database_path.write_bytes(b"db")

    def fake_create_snapshot_context(*, snapshot_path: Path, **_kwargs: object) -> share.SnapshotContext:
        snapshot_path.write_bytes(b"snapshot")
        return share.SnapshotContext(
            snapshot_path=snapshot_path,
            scope=share.ProjectScopeResult(projects=[share.ProjectRecord(1, "demo", "/repo/demo")], removed_count=0),
            scrub_summary=share.ScrubSummary(
                preset="standard",
                pseudonym_salt="standard",
                agents_total=1,
                agents_pseudonymized=0,
                ack_flags_cleared=1,
                recipients_cleared=1,
                file_reservations_removed=0,
                agent_links_removed=0,
                secrets_replaced=0,
                attachments_sanitized=0,
                bodies_redacted=0,
                attachments_cleared=0,
            ),
            fts_enabled=False,
        )

    def fake_build_bundle_assets(
        _snapshot_path: Path,
        output_dir: Path,
        **_kwargs: object,
    ) -> share.BundleArtifacts:
        refreshed_manifest = {
            "export_config": manifest["export_config"],
            "project_scope": manifest["project_scope"],
            "attachments": {"config": {"inline_threshold": 64, "detach_threshold": 1024}},
            "scrub": {"preset": "standard"},
            "database": {},
        }
        (output_dir / "manifest.json").write_text(json.dumps(refreshed_manifest), encoding="utf-8")
        return share.BundleArtifacts(
            attachments_manifest={"stats": {"inline": 0, "copied": 0, "externalized": 0, "missing": 0}},
            chunk_manifest=None,
            viewer_data=None,
        )

    monkeypatch.setattr(cli_module, "resolve_sqlite_database_path", lambda: database_path)
    monkeypatch.setattr(cli_module, "create_snapshot_context", fake_create_snapshot_context)
    monkeypatch.setattr(cli_module, "build_bundle_assets", fake_build_bundle_assets)
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: SimpleNamespace(storage=SimpleNamespace(root=str(tmp_path / "storage"))),
    )
    monkeypatch.setattr(cli_module, "detect_hosting_hints", lambda _path: [])

    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["share", "update", str(bundle_dir), "--no-zip"])

    assert result.exit_code == 0, result.output
    assert not (bundle_dir / "manifest.sig.json").exists()
    assert "Removed stale manifest.sig.json during update" in result.output


def test_share_update_prunes_stale_chunk_files_and_reports_it(
    monkeypatch, tmp_path: Path
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
        "attachments": {"config": {"inline_threshold": 64, "detach_threshold": 1024}},
        "scrub": {"preset": "standard"},
        "database": {},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    chunks_dir = bundle_dir / "chunks"
    chunks_dir.mkdir()
    stale_chunk = chunks_dir / "mailbox.sqlite3.part-0002"
    stale_chunk.write_bytes(b"stale-chunk")

    database_path = tmp_path / "mailbox.sqlite3"
    database_path.write_bytes(b"db")

    def fake_create_snapshot_context(*, snapshot_path: Path, **_kwargs: object) -> share.SnapshotContext:
        snapshot_path.write_bytes(b"snapshot")
        return share.SnapshotContext(
            snapshot_path=snapshot_path,
            scope=share.ProjectScopeResult(projects=[share.ProjectRecord(1, "demo", "/repo/demo")], removed_count=0),
            scrub_summary=share.ScrubSummary(
                preset="standard",
                pseudonym_salt="standard",
                agents_total=1,
                agents_pseudonymized=0,
                ack_flags_cleared=1,
                recipients_cleared=1,
                file_reservations_removed=0,
                agent_links_removed=0,
                secrets_replaced=0,
                attachments_sanitized=0,
                bodies_redacted=0,
                attachments_cleared=0,
            ),
            fts_enabled=False,
        )

    def fake_build_bundle_assets(
        _snapshot_path: Path,
        output_dir: Path,
        **_kwargs: object,
    ) -> share.BundleArtifacts:
        refreshed_manifest = {
            "export_config": manifest["export_config"],
            "project_scope": manifest["project_scope"],
            "attachments": {"config": {"inline_threshold": 64, "detach_threshold": 1024}},
            "scrub": {"preset": "standard"},
            "database": {
                "chunk_manifest": {
                    "chunk_count": 1,
                    "chunk_size": 1024,
                    "threshold_bytes": 2048,
                }
            },
        }
        (output_dir / "manifest.json").write_text(json.dumps(refreshed_manifest), encoding="utf-8")
        fresh_chunks = output_dir / "chunks"
        fresh_chunks.mkdir()
        (fresh_chunks / "mailbox.sqlite3.part-0001").write_bytes(b"fresh-chunk")
        (output_dir / "mailbox.sqlite3.config.json").write_text(
            json.dumps({"chunk_count": 1, "chunk_size": 1024, "threshold_bytes": 2048}),
            encoding="utf-8",
        )
        return share.BundleArtifacts(
            attachments_manifest={"stats": {"inline": 0, "copied": 0, "externalized": 0, "missing": 0}},
            chunk_manifest={"chunk_count": 1, "chunk_size": 1024},
            viewer_data=None,
        )

    monkeypatch.setattr(cli_module, "resolve_sqlite_database_path", lambda: database_path)
    monkeypatch.setattr(cli_module, "create_snapshot_context", fake_create_snapshot_context)
    monkeypatch.setattr(cli_module, "build_bundle_assets", fake_build_bundle_assets)
    monkeypatch.setattr(
        cli_module,
        "get_settings",
        lambda: SimpleNamespace(storage=SimpleNamespace(root=str(tmp_path / "storage"))),
    )
    monkeypatch.setattr(cli_module, "detect_hosting_hints", lambda _path: [])

    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["share", "update", str(bundle_dir), "--no-zip"])

    assert result.exit_code == 0, result.output
    assert not stale_chunk.exists()
    assert (chunks_dir / "mailbox.sqlite3.part-0001").exists()
    assert "Pruned 1 stale chunk file" in result.output
    assert "remain on disk" not in result.output


def test_scrub_snapshot_pseudonymizes_and_clears(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)

    summary = scrub_snapshot(snapshot, export_salt=b"unit-test-salt")

    assert summary.preset == "standard"
    assert summary.agents_total == 1
    assert summary.agents_pseudonymized == 0
    assert summary.ack_flags_cleared == 1
    assert summary.file_reservations_removed == 1
    assert summary.agent_links_removed == 1
    assert summary.secrets_replaced >= 2  # subject + body tokens
    assert summary.bodies_redacted == 0
    assert summary.attachments_cleared == 1

    conn = sqlite3.connect(snapshot)
    try:
        agent_name = conn.execute("SELECT name FROM agents WHERE id = 1").fetchone()[0]
        assert agent_name == "Alice Agent"
        ack_required = conn.execute("SELECT ack_required FROM messages WHERE id = 1").fetchone()[0]
        assert ack_required == 0
        read_ack = conn.execute(
            "SELECT read_ts, ack_ts FROM message_recipients WHERE message_id = 1"
        ).fetchone()
        assert read_ack == (None, None)

    finally:
        conn.close()

    subject, body, attachments = _read_message(snapshot)
    assert "sk-" not in subject
    assert "bearer" not in body.lower()
    assert attachments == []


def test_scrub_snapshot_strict_preset(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)

    summary = scrub_snapshot(snapshot, preset="strict", export_salt=b"strict-mode")

    assert summary.preset == "strict"
    assert summary.bodies_redacted == 1
    assert summary.attachments_cleared == 1

    conn = sqlite3.connect(snapshot)
    try:
        body = conn.execute("SELECT body_md FROM messages WHERE id = 1").fetchone()[0]
        attachments_raw = conn.execute("SELECT attachments FROM messages WHERE id = 1").fetchone()[0]
        assert body == "[Message body redacted]"
        assert attachments_raw == "[]"
    finally:
        conn.close()


def test_scrub_snapshot_archive_preset_preserves_runtime_state(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)

    summary = scrub_snapshot(snapshot, preset="archive", export_salt=b"archive-mode")

    assert summary.preset == "archive"
    assert summary.ack_flags_cleared == 0
    assert summary.recipients_cleared == 0
    assert summary.file_reservations_removed == 0
    assert summary.agent_links_removed == 0
    assert summary.secrets_replaced == 0
    assert summary.attachments_sanitized == 0

    conn = sqlite3.connect(snapshot)
    try:
        conn.row_factory = sqlite3.Row
        ack_required = conn.execute("SELECT ack_required FROM messages WHERE id = 1").fetchone()[0]
        assert ack_required == 1
        recipient_row = conn.execute(
            "SELECT read_ts, ack_ts FROM message_recipients WHERE message_id = 1"
        ).fetchone()
        assert recipient_row[0] == "2025-01-01"
        assert recipient_row[1] == "2025-01-02"
        file_reservation_count = conn.execute("SELECT COUNT(*) FROM file_reservations").fetchone()[0]
        assert file_reservation_count == 1
        agent_links_count = conn.execute("SELECT COUNT(*) FROM agent_links").fetchone()[0]
        assert agent_links_count == 1
    finally:
        conn.close()

    subject, body, attachments = _read_message(snapshot)
    assert "sk-" in subject
    assert "bearer" in body.lower()
    assert attachments and "download_url" in attachments[0]


def test_scrub_snapshot_invalid_attachments_json(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)

    conn = sqlite3.connect(snapshot)
    try:
        conn.execute("UPDATE messages SET attachments = ? WHERE id = 1", ("{not json}",))
        conn.commit()
    finally:
        conn.close()

    scrub_snapshot(snapshot, export_salt=b"invalid-json")

    conn = sqlite3.connect(snapshot)
    try:
        attachments_raw = conn.execute("SELECT attachments FROM messages WHERE id = 1").fetchone()[0]
        assert attachments_raw == "[]"
    finally:
        conn.close()


def test_bundle_attachments_handles_modes(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "PRIVATE_SOURCE_PATH_CANARY"
    base_assets = storage_root / "attachments" / "raw"
    base_assets.mkdir(parents=True, exist_ok=True)

    small = base_assets / "small.txt"
    small.write_bytes(b"tiny data")

    medium = base_assets / "medium.txt"
    medium.write_bytes(b"m" * 256)

    large = base_assets / "large.txt"
    large.write_bytes(b"L" * 512)

    payload = [
        {"type": "file", "path": str(small), "media_type": "text/plain"},
        {"type": "file", "path": str(medium), "media_type": "text/plain"},
        {"type": "file", "path": str(large), "media_type": "text/plain"},
        {
            "type": "file",
            "path": str(base_assets / "missing.txt"),
            "media_type": "text/plain",
        },
    ]

    conn = sqlite3.connect(snapshot)
    try:
        conn.execute(
            "UPDATE messages SET attachments = ? WHERE id = 1",
            (json.dumps(payload),),
        )
        conn.commit()
    finally:
        conn.close()

    manifest = bundle_attachments(
        snapshot,
        tmp_path / "out",
        storage_root=storage_root,
        inline_threshold=32,
        detach_threshold=400,
    )

    stats = manifest["stats"]
    assert stats == {
        "inline": 1,
        "copied": 1,
        "externalized": 1,
        "missing": 1,
        "bytes_copied": 256,
    }

    _subject, _body, attachments = _read_message(snapshot)
    assert attachments[0]["type"] == "inline"
    assert attachments[1]["type"] == "file"
    path_value = attachments[1]["path"]
    assert isinstance(path_value, str)
    assert path_value.startswith("attachments/")
    assert (tmp_path / "out" / path_value).is_file()
    assert attachments[2]["type"] == "external"
    assert "note" in attachments[2]
    assert attachments[3]["type"] == "missing"

    inline_data = attachments[0]["data_uri"]
    assert isinstance(inline_data, str)
    assert inline_data.startswith("data:text/plain;base64,")
    decoded = base64.b64decode(inline_data.split(",", 1)[1])
    assert decoded == b"tiny data"

    items = manifest["items"]
    assert len(items) == 4
    modes = {item["mode"] for item in items}
    assert modes == {"inline", "file", "external", "missing"}
    public_attachment_json = json.dumps(
        {"manifest": manifest, "attachments": attachments},
        sort_keys=True,
    )
    assert "PRIVATE_SOURCE_PATH_CANARY" not in public_attachment_json
    assert str(storage_root) not in public_attachment_json
    assert "original_path" not in public_attachment_json


def test_bundle_attachments_rejects_noncanonical_sha256_before_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "storage"
    source_path = storage_root / "attachments" / "raw" / "payload.bin"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"attachment data")

    conn = sqlite3.connect(snapshot)
    try:
        conn.execute(
            "UPDATE messages SET attachments = ? WHERE id = 1",
            (
                json.dumps(
                    [
                        {
                            "type": "file",
                            "path": str(source_path.relative_to(storage_root)),
                            "media_type": "application/octet-stream",
                        }
                    ]
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    malicious_digest = "../escaped-attachment"
    monkeypatch.setattr(
        share.hashlib,
        "sha256",
        lambda _data: SimpleNamespace(hexdigest=lambda: malicious_digest),
    )
    output_dir = tmp_path / "bundle"

    with pytest.raises(ShareExportError, match="64 lowercase hexadecimal"):
        bundle_attachments(
            snapshot,
            output_dir,
            storage_root=storage_root,
            inline_threshold=0,
            detach_threshold=1024,
        )

    assert list(tmp_path.glob("escaped-attachment*")) == []
    assert list((output_dir / "attachments").rglob("*")) == []


def test_bundle_attachments_rejects_attachment_symlink_outside_output(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "storage"
    output_dir = tmp_path / "bundle"
    outside_dir = tmp_path / "outside"
    output_dir.mkdir()
    outside_dir.mkdir()
    try:
        (output_dir / "attachments").symlink_to(outside_dir, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("Directory symlinks are unavailable on this platform")

    with pytest.raises(ShareExportError, match="stay within the bundle output directory"):
        bundle_attachments(
            snapshot,
            output_dir,
            storage_root=storage_root,
        )

    assert list(outside_dir.iterdir()) == []


@pytest.mark.parametrize("source_kind", ["absolute", "parent-traversal"])
def test_bundle_attachments_rejects_source_outside_storage(
    source_kind: str,
    tmp_path: Path,
) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "storage" / "root"
    storage_root.mkdir(parents=True)
    outside_source = tmp_path / "outside-secret.txt"
    secret = b"must never enter the share bundle"
    outside_source.write_bytes(secret)
    source_value = (
        str(outside_source.resolve())
        if source_kind == "absolute"
        else "../../outside-secret.txt"
    )

    conn = sqlite3.connect(snapshot)
    try:
        conn.execute(
            "UPDATE messages SET attachments = ? WHERE id = 1",
            (
                json.dumps(
                    [
                        {
                            "type": "file",
                            "path": source_value,
                            "media_type": "text/plain",
                        }
                    ]
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    output_dir = tmp_path / f"bundle-{source_kind}"
    with pytest.raises(
        ShareExportError,
        match="source path must stay within the configured storage directory",
    ) as exc_info:
        bundle_attachments(
            snapshot,
            output_dir,
            storage_root=storage_root,
            inline_threshold=0,
            detach_threshold=1024,
        )

    assert str(outside_source) not in str(exc_info.value)
    assert not any(
        path.is_file() and path.read_bytes() == secret
        for path in output_dir.rglob("*")
    )


def test_bundle_attachments_rejects_source_symlink_outside_storage(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "storage"
    source_dir = storage_root / "attachments" / "raw"
    source_dir.mkdir(parents=True)
    outside_source = tmp_path / "outside-secret.txt"
    secret = b"must never be followed through a symlink"
    outside_source.write_bytes(secret)
    source_link = source_dir / "linked-secret.txt"
    try:
        source_link.symlink_to(outside_source)
    except (NotImplementedError, OSError):
        pytest.skip("File symlinks are unavailable on this platform")

    conn = sqlite3.connect(snapshot)
    try:
        conn.execute(
            "UPDATE messages SET attachments = ? WHERE id = 1",
            (
                json.dumps(
                    [
                        {
                            "type": "file",
                            "path": str(source_link.relative_to(storage_root)),
                            "media_type": "text/plain",
                        }
                    ]
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    output_dir = tmp_path / "bundle-source-symlink"
    with pytest.raises(
        ShareExportError,
        match="source path must stay within the configured storage directory",
    ) as exc_info:
        bundle_attachments(
            snapshot,
            output_dir,
            storage_root=storage_root,
            inline_threshold=0,
            detach_threshold=1024,
        )

    assert str(outside_source) not in str(exc_info.value)
    assert not any(
        path.is_file() and path.read_bytes() == secret
        for path in output_dir.rglob("*")
    )


def test_summarize_snapshot(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "storage"
    attachments_dir = storage_root / "attachments" / "raw"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    (attachments_dir / "inline.txt").write_bytes(b"inline")
    (attachments_dir / "large.bin").write_bytes(b"L" * 1024)

    attachments = [
        {"type": "file", "path": "attachments/raw/inline.txt", "media_type": "text/plain"},
        {"type": "file", "path": "attachments/raw/large.bin", "media_type": "application/octet-stream"},
        {"type": "file", "path": "attachments/raw/missing.bin", "media_type": "application/octet-stream"},
    ]

    conn = sqlite3.connect(snapshot)
    try:
        conn.execute(
            "UPDATE messages SET attachments = ? WHERE id = 1",
            (json.dumps(attachments),),
        )
        conn.commit()
    finally:
        conn.close()

    summary = summarize_snapshot(
        snapshot,
        storage_root=storage_root,
        inline_threshold=64,
        detach_threshold=512,
    )

    assert summary["messages"] == 1
    assert summary["threads"] == 1
    assert summary["projects"]
    stats = summary["attachments"]
    assert stats["total"] == 3
    assert stats["inline_candidates"] == 1
    assert stats["external_candidates"] == 1
    assert stats["missing"] == 1


def test_manifest_snapshot_structure(monkeypatch, tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "env" / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    attachments_dir = storage_root / "attachments" / "raw"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    (attachments_dir / "binary.bin").write_bytes(b"binary data")

    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{snapshot}")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8123")
    monkeypatch.setenv("HTTP_PATH", "/mcp/")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")

    output_dir = tmp_path / "bundle"
    runner = CliRunner()
    clear_settings_cache()
    try:
        result = runner.invoke(
            cli_module.app,
            [
                "share",
                "export",
                "--output",
                str(output_dir),
                "--inline-threshold",
                "64",
                "--detach-threshold",
                "1024",
            ],
        )
        assert result.exit_code == 0, result.output

        manifest_path = output_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())

        assert manifest["schema_version"] == "0.1.0"
        assert manifest["scrub"]["preset"] == "standard"
        assert manifest["scrub"]["agents_total"] == 1
        assert manifest["scrub"]["agents_pseudonymized"] == 0
        assert manifest["scrub"]["ack_flags_cleared"] == 1
        assert manifest["scrub"]["recipients_cleared"] == 0
        assert manifest["scrub"]["file_reservations_removed"] == 0
        assert manifest["scrub"]["agent_links_removed"] == 0
        assert manifest["scrub"]["bodies_redacted"] == 0
        assert manifest["scrub"]["attachments_cleared"] == 1
        assert manifest["scrub"]["attachments_sanitized"] == 1
        assert manifest["scrub"]["secrets_replaced"] >= 2
        assert manifest["project_scope"]["included"] == [
            {"slug": "demo", "human_key": "demo"}
        ]
        assert manifest["project_scope"]["removed_count"] == 0
        assert manifest["database"]["chunked"] is False
        assert isinstance(manifest["database"].get("fts_enabled"), bool)
        detected_hosts = manifest["hosting"].get("detected", [])
        assert isinstance(detected_hosts, list)
        for host_entry in detected_hosts:
            assert {"id", "title", "summary", "signals"}.issubset(host_entry.keys())

        assert set(SCRUB_PRESETS) >= {"standard", "strict"}
    finally:
        clear_settings_cache()


def test_run_share_export_wizard(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "wizard.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT)")
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'demo', 'Demo Human')")
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (2, 'ops', 'Operations Vault')")
        conn.commit()
    finally:
        conn.close()

    responses = iter(["demo,ops", "2048", "65536", "1048576", "131072", "strict"])
    monkeypatch.setattr(cli_module.typer, "prompt", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(cli_module.typer, "confirm", lambda *_args, **_kwargs: False)

    result = cli_module._run_share_export_wizard(db, 1024, 32768, 1_048_576, 131_072, "standard")

    assert result["projects"] == ["demo", "ops"]
    assert result["inline_threshold"] == 2048
    assert result["detach_threshold"] == 65536
    assert result["chunk_threshold"] == 1_048_576
    assert result["chunk_size"] == 131_072
    assert result["zip_bundle"] is False
    assert result["scrub_preset"] == "strict"


def test_share_export_dry_run(monkeypatch, tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "env" / "storage"
    attachments_dir = storage_root / "attachments" / "raw"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    (attachments_dir / "inline.txt").write_bytes(b"data")

    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{snapshot}")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8765")
    monkeypatch.setenv("HTTP_PATH", "/mcp/")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")

    runner = CliRunner()
    clear_settings_cache()
    output_placeholder = tmp_path / "dry-run-out"
    result = runner.invoke(
        cli_module.app,
        [
            "share",
            "export",
            "--output",
            str(output_placeholder),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Dry-Run Summary" in result.output
    assert "Security Checklist" in result.output
    clear_settings_cache()


def test_start_preview_server_serves_content(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "index.html").write_text("hello preview", encoding="utf-8")
    viewer = bundle / "viewer"
    viewer.mkdir()
    standalone_html = "<!doctype html><body>standalone viewer</body>"
    viewer_index = viewer / "index.html"
    viewer_index.write_text(standalone_html, encoding="utf-8")
    (viewer / "preview-reload.js").write_text("// preview reload", encoding="utf-8")

    server = cli_module._start_preview_server(bundle, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=2) as response:
            body = response.read().decode("utf-8")
        assert "hello preview" in body
        with urllib.request.urlopen(
            f"http://{host}:{port}/viewer/", timeout=2
        ) as response:
            preview_body = response.read().decode("utf-8")
        assert (
            '<script type="module" src="./preview-reload.js" '
            "data-preview-only></script>"
        ) in preview_body
        assert viewer_index.read_text(encoding="utf-8") == standalone_html
        with urllib.request.urlopen(f"http://{host}:{port}/__preview__/status", timeout=2) as response:
            status_payload = json.loads(response.read().decode("utf-8"))
        assert "signature" in status_payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_collect_preview_status_is_independent_of_directory_entry_order(tmp_path: Path, monkeypatch) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for name in ("z.txt", "a.txt", "nested/m.txt"):
        path = first / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    for name in ("nested/m.txt", "a.txt", "z.txt"):
        path = second / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")

    fixed_ns = 1_725_000_000_000_000_000
    for root in (first, second):
        for path in root.rglob("*"):
            if path.is_file():
                os.utime(path, ns=(fixed_ns, fixed_ns))

    entries_by_root = {
        first: [first / "z.txt", first / "nested", first / "a.txt", first / "nested/m.txt"],
        second: [second / "nested/m.txt", second / "a.txt", second / "nested", second / "z.txt"],
    }
    monkeypatch.setattr(Path, "rglob", lambda root, _pattern: iter(entries_by_root[root]))

    first_status = cli_module._collect_preview_status(first)
    second_status = cli_module._collect_preview_status(second)

    assert first_status == second_status
    (first / "a.txt").write_text("expanded", encoding="utf-8")
    os.utime(first / "a.txt", ns=(fixed_ns, fixed_ns))
    assert cli_module._collect_preview_status(first)["signature"] != first_status["signature"]


def test_share_export_chunking_and_viewer_data(monkeypatch, tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    storage_root = tmp_path / "env" / "storage"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{snapshot}")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8765")
    monkeypatch.setenv("HTTP_PATH", "/mcp/")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")

    output_dir = tmp_path / "bundle"
    runner = CliRunner()
    clear_settings_cache()
    result = runner.invoke(
        cli_module.app,
        [
            "share",
            "export",
            "--output",
            str(output_dir),
            "--inline-threshold",
            "32",
            "--detach-threshold",
            "128",
            "--chunk-threshold",
            "1",
            "--chunk-size",
            "2048",
        ],
    )
    assert result.exit_code == 0, result.output

    chunk_config_path = output_dir / "mailbox.sqlite3.config.json"
    assert chunk_config_path.is_file()
    chunk_config = json.loads(chunk_config_path.read_text())
    assert chunk_config["chunk_count"] > 0

    chunks_dir = output_dir / "chunks"
    assert any(chunks_dir.iterdir())

    checksum_path = output_dir / "chunks.sha256"
    assert checksum_path.is_file()
    checksum_lines = checksum_path.read_text().strip().splitlines()
    assert len(checksum_lines) == chunk_config["chunk_count"]
    assert checksum_lines[0].count(" ") >= 1

    viewer_data_dir = output_dir / "viewer" / "data"
    messages_json = viewer_data_dir / "messages.json"
    assert messages_json.is_file()
    messages = json.loads(messages_json.read_text())
    assert messages and messages[0]["subject"]

    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert manifest["database"]["chunked"] is True
    assert "viewer" in manifest
    assert manifest["scrub"]["preset"] == "standard"
    assert isinstance(manifest["database"].get("fts_enabled"), bool)
    clear_settings_cache()


def test_verify_viewer_vendor_assets():
    # Should not raise when bundled vendor assets match recorded checksums.
    share._verify_viewer_vendor_assets()


def test_maybe_chunk_database_rejects_zero_chunk_size(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    output_dir = tmp_path / "bundle"
    output_dir.mkdir()
    with pytest.raises(ShareExportError):
        maybe_chunk_database(
            snapshot,
            output_dir,
            threshold_bytes=1,
            chunk_bytes=0,
        )


def test_sign_and_verify_manifest(tmp_path: Path) -> None:
    """Test Ed25519 manifest signing and verification flow."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    # Create a test manifest
    manifest_path = tmp_path / "manifest.json"
    manifest_data = {"version": "1.0", "test": "data"}
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    # Generate signing key (32-byte seed)
    signing_key_path = tmp_path / "signing.key"
    signing_key_path.write_bytes(b"A" * 32)

    # Sign the manifest
    signature_info = share.sign_manifest(
        manifest_path,
        signing_key_path,
        tmp_path,
    )

    assert signature_info["algorithm"] == "ed25519"
    assert "signature" in signature_info
    assert "public_key" in signature_info
    assert "manifest_sha256" in signature_info

    # Verify signature file was created
    sig_path = tmp_path / "manifest.sig.json"
    assert sig_path.exists()

    # Verify the bundle (should pass)
    result = share.verify_bundle(tmp_path)
    assert result["signature_checked"] is True
    assert result["signature_verified"] is True

    # Verify with explicit public key (should pass)
    result = share.verify_bundle(tmp_path, public_key=signature_info["public_key"])
    assert result["signature_verified"] is True

    # Tamper with manifest and verify (should fail)
    manifest_path.write_text(json.dumps({"tampered": True}), encoding="utf-8")
    with pytest.raises(ShareExportError, match="signature verification failed"):
        share.verify_bundle(tmp_path)


def test_verify_bundle_without_signature(tmp_path: Path) -> None:
    """Test bundle verification when no signature is present."""
    # Create minimal manifest
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

    # Verify should succeed but report no signature
    result = share.verify_bundle(tmp_path)
    assert result["signature_checked"] is False
    assert result["signature_verified"] is False


def test_verify_bundle_with_sri(tmp_path: Path) -> None:
    """Test SRI hash verification in bundle."""
    # Create manifest with SRI entries
    viewer_dir = tmp_path / "viewer"
    viewer_dir.mkdir()

    js_file = viewer_dir / "test.js"
    js_file.write_text("console.log('test');", encoding="utf-8")

    # Compute SRI hash
    sri_hash = share._compute_sri(js_file)

    manifest_data = {
        "version": "1.0",
        "viewer": {
            "sri": {
                "viewer/test.js": sri_hash
            }
        }
    }

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    # Verification should pass
    result = share.verify_bundle(tmp_path)
    assert result["sri_checked"] is True

    # Tamper with JS file
    js_file.write_text("console.log('hacked');", encoding="utf-8")

    # Verification should fail
    with pytest.raises(ShareExportError, match="SRI mismatch"):
        share.verify_bundle(tmp_path)


def test_verify_bundle_missing_sri_asset(tmp_path: Path) -> None:
    """Test verification fails when SRI asset is missing."""
    manifest_data = {
        "version": "1.0",
        "viewer": {
            "sri": {
                "viewer/missing.js": "sha256-abc123"
            }
        }
    }

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(ShareExportError, match="Missing asset for SRI entry"):
        share.verify_bundle(tmp_path)


def test_verify_bundle_rejects_sri_asset_outside_bundle_root(tmp_path: Path) -> None:
    """SRI entries must not resolve outside the verified bundle directory."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()

    outside_asset = tmp_path / "outside.js"
    outside_asset.write_text("console.log('outside');", encoding="utf-8")

    manifest_data = {
        "version": "1.0",
        "viewer": {
            "sri": {
                "../outside.js": share._compute_sri(outside_asset),
            }
        },
    }

    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(ShareExportError, match="must stay within the bundle root"):
        share.verify_bundle(bundle_dir)


def test_decrypt_with_age_requires_age_binary(tmp_path: Path, monkeypatch) -> None:
    """Test that decrypt_with_age fails gracefully when age is not installed."""
    monkeypatch.setattr(share.shutil, "which", lambda x: None)

    encrypted = tmp_path / "bundle.age"
    encrypted.write_bytes(b"encrypted data")
    output = tmp_path / "decrypted"

    with pytest.raises(ShareExportError, match="`age` CLI not found"):
        share.decrypt_with_age(encrypted, output, identity=tmp_path / "key.txt")


def test_decrypt_with_age_validation(tmp_path: Path, monkeypatch) -> None:
    """Test decrypt_with_age input validation."""
    # Mock age binary as available for validation tests
    monkeypatch.setattr(share.shutil, "which", lambda x: "/usr/bin/age" if x == "age" else None)

    encrypted = tmp_path / "bundle.age"
    encrypted.write_bytes(b"data")
    output = tmp_path / "out"
    identity = tmp_path / "identity.txt"
    identity.write_text("AGE-SECRET-KEY-1...", encoding="utf-8")

    # Can't provide both identity and passphrase
    with pytest.raises(ShareExportError, match="either an identity file or a passphrase"):
        share.decrypt_with_age(encrypted, output, identity=identity, passphrase="secret")

    # Must provide at least one
    with pytest.raises(ShareExportError, match="requires --identity or --passphrase"):
        share.decrypt_with_age(encrypted, output)

    # Identity file must exist
    missing_identity = tmp_path / "nonexistent.txt"
    with pytest.raises(ShareExportError, match="Identity file not found"):
        share.decrypt_with_age(encrypted, output, identity=missing_identity)


def test_decrypt_with_age_rejects_directory_input(tmp_path: Path, monkeypatch) -> None:
    """Decryption should reject directory inputs before invoking age."""
    monkeypatch.setattr(share.shutil, "which", lambda x: "/usr/bin/age" if x == "age" else None)

    encrypted_dir = tmp_path / "encrypted_dir"
    encrypted_dir.mkdir()
    identity = tmp_path / "identity.txt"
    identity.write_text("AGE-SECRET-KEY-1...", encoding="utf-8")

    with pytest.raises(ShareExportError, match="Encrypted path must be a file"):
        share.decrypt_with_age(encrypted_dir, tmp_path / "out", identity=identity)


def test_decrypt_with_age_refuses_existing_output(tmp_path: Path, monkeypatch) -> None:
    """Decryption should not overwrite an existing destination file."""
    monkeypatch.setattr(share.shutil, "which", lambda x: "/usr/bin/age" if x == "age" else None)

    encrypted = tmp_path / "bundle.age"
    encrypted.write_bytes(b"encrypted data")
    output = tmp_path / "bundle"
    output.write_bytes(b"existing data")
    identity = tmp_path / "identity.txt"
    identity.write_text("AGE-SECRET-KEY-1...", encoding="utf-8")

    monkeypatch.setattr(
        share.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("age should not be invoked when output already exists"),
    )

    with pytest.raises(ShareExportError, match="Refusing to overwrite existing output file"):
        share.decrypt_with_age(encrypted, output, identity=identity)


def test_decrypt_with_age_reports_exit_code_when_age_emits_no_output(
    tmp_path: Path, monkeypatch
) -> None:
    """Decryption failures should remain actionable even if age prints nothing."""
    monkeypatch.setattr(share.shutil, "which", lambda x: "/usr/bin/age" if x == "age" else None)

    encrypted = tmp_path / "bundle.age"
    encrypted.write_bytes(b"encrypted data")
    identity = tmp_path / "identity.txt"
    identity.write_text("AGE-SECRET-KEY-1...", encoding="utf-8")

    class Result:
        returncode = 7
        stderr = ""
        stdout = ""

    monkeypatch.setattr(share.subprocess, "run", lambda *args, **kwargs: Result())

    with pytest.raises(ShareExportError, match=r"status 7"):
        share.decrypt_with_age(encrypted, tmp_path / "out", identity=identity)


def test_sri_computation(tmp_path: Path) -> None:
    """Test SRI hash computation."""
    test_file = tmp_path / "test.js"
    test_file.write_text("test content", encoding="utf-8")

    sri = share._compute_sri(test_file)

    # Should start with sha256-
    assert sri.startswith("sha256-")

    # Should be base64 encoded (typically 44+ chars including prefix)
    assert len(sri) > 40

    # Should be deterministic
    sri2 = share._compute_sri(test_file)
    assert sri == sri2


def test_build_viewer_sri(tmp_path: Path) -> None:
    """Test building SRI map for viewer assets."""
    viewer_dir = tmp_path / "viewer"
    vendor_dir = viewer_dir / "vendor"
    vendor_dir.mkdir(parents=True)

    # Create test assets
    (viewer_dir / "viewer.js").write_text("js code", encoding="utf-8")
    (viewer_dir / "styles.css").write_text("css code", encoding="utf-8")
    (vendor_dir / "lib.wasm").write_bytes(b"wasm binary")
    (viewer_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (viewer_dir / "README.txt").write_text("readme", encoding="utf-8")

    sri_map = share._build_viewer_sri(tmp_path)

    # Should include .js, .css, .wasm files
    assert "viewer/viewer.js" in sri_map
    assert "viewer/styles.css" in sri_map
    assert "viewer/vendor/lib.wasm" in sri_map

    # Should NOT include .html or .txt
    assert "viewer/index.html" not in sri_map
    assert "viewer/README.txt" not in sri_map

    # All values should be SRI hashes
    for _path, sri in sri_map.items():
        assert sri.startswith("sha256-")


def test_cli_verify_command(tmp_path: Path) -> None:
    """Test the CLI verify command."""
    # Create a bundle with manifest
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = bundle / "manifest.json"
    manifest.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["share", "verify", str(bundle)],
    )

    assert result.exit_code == 0
    assert "Bundle verification passed" in result.output
    assert "SRI checked: False" in result.output
    assert "Signature checked: False" in result.output


def test_cli_verify_command_missing_bundle(tmp_path: Path) -> None:
    """Test verify command with non-existent bundle."""
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["share", "verify", str(tmp_path / "nonexistent")],
    )

    assert result.exit_code == 1
    assert "Bundle directory not found" in result.output


def test_cli_verify_command_not_directory(tmp_path: Path) -> None:
    """Test verify command with file instead of directory."""
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("test", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["share", "verify", str(not_a_dir)],
    )

    assert result.exit_code == 1
    assert "must be a directory" in result.output


def test_cli_decrypt_command_auto_output(tmp_path: Path) -> None:
    """Test decrypt command with auto-generated output path."""
    encrypted = tmp_path / "bundle.zip.age"
    encrypted.write_bytes(b"fake encrypted data")

    runner = CliRunner()
    # Should fail because age is not installed, but validates CLI parameter handling
    result = runner.invoke(
        cli_module.app,
        ["share", "decrypt", str(encrypted), "--identity", str(tmp_path / "key.txt")],
    )

    # Will fail due to missing age binary, but should not fail due to missing --output
    assert result.exit_code == 1
    # Error should be about age, not about missing output parameter
    assert "age" in result.output.lower() or "CLI not found" in result.output


def test_cli_decrypt_command_not_file(tmp_path: Path) -> None:
    """Test decrypt command with directory instead of file."""
    not_a_file = tmp_path / "directory"
    not_a_file.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["share", "decrypt", str(not_a_file), "--identity", str(tmp_path / "key.txt")],
    )

    assert result.exit_code == 1
    assert "must be a file" in result.output


def test_encrypt_bundle_requires_age_binary(tmp_path: Path, monkeypatch) -> None:
    """Test that encrypt_bundle fails gracefully when age is not installed."""
    monkeypatch.setattr(share.shutil, "which", lambda x: None)

    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"test data")

    with pytest.raises(ShareExportError, match="`age` CLI not found"):
        share.encrypt_bundle(bundle, ["age1recipient..."])


def test_encrypt_bundle_with_invalid_recipient(tmp_path: Path, monkeypatch) -> None:
    """Test encryption failure with invalid recipient format."""
    # Mock age binary to test actual age failures

    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"test data")

    def mock_run(*args, **kwargs):
        # Simulate age returning error for invalid recipient
        class Result:
            returncode = 1
            stderr = "Error: invalid recipient format: notavalidrecipient"

        return Result()

    monkeypatch.setattr(share.subprocess, "run", mock_run)
    monkeypatch.setattr(share.shutil, "which", lambda x: "/usr/bin/age" if x == "age" else None)

    with pytest.raises(ShareExportError, match=r"age encryption failed.*invalid recipient"):
        share.encrypt_bundle(bundle, ["notavalidrecipient"])


def test_encrypt_bundle_refuses_existing_output(tmp_path: Path, monkeypatch) -> None:
    """Encryption should not overwrite an existing .age artifact."""
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"test data")
    encrypted = tmp_path / "bundle.zip.age"
    encrypted.write_bytes(b"existing encrypted data")

    monkeypatch.setattr(share.shutil, "which", lambda x: "/usr/bin/age" if x == "age" else None)
    monkeypatch.setattr(
        share.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("age should not be invoked when output already exists"),
    )

    with pytest.raises(ShareExportError, match="Encrypted output path already exists"):
        share.encrypt_bundle(bundle, ["age1recipient..."])


def test_encrypt_bundle_reports_stdout_error_when_stderr_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """Encryption failures should surface stdout if stderr is empty."""
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"test data")

    class Result:
        returncode = 1
        stderr = ""
        stdout = "recipient rejected"

    monkeypatch.setattr(share.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(share.shutil, "which", lambda x: "/usr/bin/age" if x == "age" else None)

    with pytest.raises(ShareExportError, match=r"recipient rejected"):
        share.encrypt_bundle(bundle, ["age1recipient..."])


def test_encrypt_bundle_returns_none_for_empty_recipients(tmp_path: Path) -> None:
    """Test that encrypt_bundle returns None when no recipients provided."""
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"test data")

    result = share.encrypt_bundle(bundle, [])
    assert result is None


def test_verify_bundle_with_tampered_signature(tmp_path: Path) -> None:
    """Test signature verification fails when signature doesn't match."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    # Create a valid signed bundle
    manifest_path = tmp_path / "manifest.json"
    manifest_data: dict[str, str | bool] = {"version": "1.0", "test": "data"}
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    signing_key_path = tmp_path / "signing.key"
    signing_key_path.write_bytes(b"A" * 32)

    share.sign_manifest(manifest_path, signing_key_path, tmp_path)

    # Tamper with the manifest after signing (this invalidates the signature)
    manifest_data["tampered"] = True
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    # Verification should fail because manifest changed
    with pytest.raises(ShareExportError, match="signature verification failed"):
        share.verify_bundle(tmp_path)


def test_verify_bundle_with_missing_signature_file(tmp_path: Path) -> None:
    """Test verification when signature file is present in manifest but missing from disk."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    # Create manifest with signature claim
    manifest_path = tmp_path / "manifest.json"
    manifest_data = {"version": "1.0", "signed": True}
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    # Create signature file
    sig_path = tmp_path / "manifest.sig.json"
    sig_data = {
        "algorithm": "ed25519",
        "signature": "dGVzdA==",  # base64 "test"
        "public_key": "dGVzdA==",
        "manifest_sha256": "abc123",
    }
    sig_path.write_text(json.dumps(sig_data), encoding="utf-8")

    # Delete signature file after manifest references it
    sig_path.unlink()

    # Verification should handle missing signature gracefully
    result = share.verify_bundle(tmp_path)
    assert result["signature_checked"] is False


def test_verify_bundle_with_corrupted_signature_json(tmp_path: Path) -> None:
    """Test verification handles corrupted signature JSON."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

    sig_path = tmp_path / "manifest.sig.json"
    sig_path.write_text("{ invalid json", encoding="utf-8")

    # Should handle gracefully
    with pytest.raises(ShareExportError, match="not valid JSON"):
        share.verify_bundle(tmp_path)


def test_decrypt_encrypted_file_not_exist(tmp_path: Path, monkeypatch) -> None:
    """Test decrypt handles non-existent encrypted file."""
    monkeypatch.setattr(share.shutil, "which", lambda x: "/usr/bin/age" if x == "age" else None)

    encrypted = tmp_path / "nonexistent.age"
    output = tmp_path / "out"
    identity = tmp_path / "key.txt"
    identity.write_text("AGE-SECRET-KEY-1...", encoding="utf-8")

    with pytest.raises(ShareExportError, match="Encrypted file not found"):
        share.decrypt_with_age(encrypted, output, identity=identity)


def test_sign_manifest_with_invalid_key_length(tmp_path: Path) -> None:
    """Test signing fails with key that's not 32 or 64 bytes."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

    signing_key_path = tmp_path / "bad_key.key"
    signing_key_path.write_bytes(b"A" * 16)  # Invalid: only 16 bytes

    with pytest.raises(ShareExportError, match="32-byte seed or 64-byte expanded"):
        share.sign_manifest(manifest_path, signing_key_path, tmp_path)


def test_sign_manifest_with_directory_instead_of_file(tmp_path: Path) -> None:
    """Test signing fails when manifest path is a directory."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    manifest_dir = tmp_path / "manifest_dir"
    manifest_dir.mkdir()

    signing_key_path = tmp_path / "key.key"
    signing_key_path.write_bytes(b"A" * 32)

    with pytest.raises(ShareExportError, match="Manifest path must be a file"):
        share.sign_manifest(manifest_dir, signing_key_path, tmp_path)


def test_sign_manifest_with_missing_manifest(tmp_path: Path) -> None:
    """Test signing fails when manifest doesn't exist."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    manifest_path = tmp_path / "missing.json"
    signing_key_path = tmp_path / "key.key"
    signing_key_path.write_bytes(b"A" * 32)

    with pytest.raises(ShareExportError, match="Manifest file not found"):
        share.sign_manifest(manifest_path, signing_key_path, tmp_path)


def test_sign_manifest_overwrites_existing_public_key_when_requested(tmp_path: Path) -> None:
    """overwrite=True should refresh the exported public key file for share update flows."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

    signing_key_path = tmp_path / "key.key"
    signing_key_path.write_bytes(b"A" * 32)

    public_out = tmp_path / "signing.pub"
    public_out.write_text("stale-key", encoding="utf-8")

    signature_info = share.sign_manifest(
        manifest_path,
        signing_key_path,
        tmp_path,
        public_out=public_out,
        overwrite=True,
    )

    assert public_out.read_text(encoding="utf-8") == signature_info["public_key"]


def test_verify_bundle_with_sri_and_signature_both_valid(tmp_path: Path) -> None:
    """Test verification succeeds when both SRI and signature are valid."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    # Create viewer file
    viewer_dir = tmp_path / "viewer"
    viewer_dir.mkdir()
    js_file = viewer_dir / "test.js"
    js_file.write_text("console.log('test');", encoding="utf-8")

    # Compute SRI
    sri_hash = share._compute_sri(js_file)

    # Create manifest with SRI
    manifest_data = {
        "version": "1.0",
        "viewer": {"sri": {"viewer/test.js": sri_hash}},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    # Sign manifest
    signing_key_path = tmp_path / "key.key"
    signing_key_path.write_bytes(b"A" * 32)
    share.sign_manifest(manifest_path, signing_key_path, tmp_path)

    # Verification should pass
    result = share.verify_bundle(tmp_path)
    assert result["sri_checked"] is True
    assert result["signature_checked"] is True
    assert result["signature_verified"] is True


def test_verify_bundle_rejects_manifest_sha256_mismatch(tmp_path: Path) -> None:
    """Verification should fail if manifest.sig.json records the wrong manifest hash."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

    signing_key_path = tmp_path / "key.key"
    signing_key_path.write_bytes(b"A" * 32)
    share.sign_manifest(manifest_path, signing_key_path, tmp_path)

    sig_path = tmp_path / "manifest.sig.json"
    sig_payload = json.loads(sig_path.read_text(encoding="utf-8"))
    sig_payload["manifest_sha256"] = "0" * 64
    sig_path.write_text(json.dumps(sig_payload), encoding="utf-8")

    with pytest.raises(ShareExportError, match="manifest_sha256"):
        share.verify_bundle(tmp_path)


def test_verify_bundle_rejects_unsupported_signature_algorithm(tmp_path: Path) -> None:
    """Verification should fail if manifest.sig.json advertises a different algorithm."""
    pytest.importorskip("nacl", reason="PyNaCl required for signing tests")

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")

    signing_key_path = tmp_path / "key.key"
    signing_key_path.write_bytes(b"A" * 32)
    share.sign_manifest(manifest_path, signing_key_path, tmp_path)

    sig_path = tmp_path / "manifest.sig.json"
    sig_payload = json.loads(sig_path.read_text(encoding="utf-8"))
    sig_payload["algorithm"] = "rsa"
    sig_path.write_text(json.dumps(sig_payload), encoding="utf-8")

    with pytest.raises(ShareExportError, match="Unsupported signature algorithm"):
        share.verify_bundle(tmp_path)


def test_create_performance_indexes(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)

    # Ensure base schema has no indexes initially
    conn = sqlite3.connect(snapshot)
    try:
        indexes_before = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'"
            )
        }
    finally:
        conn.close()
    assert not indexes_before

    create_performance_indexes(snapshot)

    conn = sqlite3.connect(snapshot)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        assert "subject_lower" in columns
        assert "sender_lower" in columns

        index_rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='messages'"
        ).fetchall()
        index_map = {row[0]: row[1] for row in index_rows}

        sample = conn.execute(
            "SELECT subject_lower, sender_lower FROM messages ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    assert "idx_messages_created_ts" in index_map
    assert "idx_messages_thread" in index_map
    assert "idx_messages_sender" in index_map
    assert "idx_messages_subject_lower" in index_map
    assert "idx_messages_sender_lower" in index_map
    for name in (
        "idx_messages_created_ts",
        "idx_messages_thread",
        "idx_messages_sender",
        "idx_messages_subject_lower",
        "idx_messages_sender_lower",
    ):
        assert index_map[name], f"Expected SQL definition for index {name}"

    assert sample is not None
    assert isinstance(sample[0], str)
    assert isinstance(sample[1], str)


def test_finalize_snapshot_sql_hygiene(tmp_path: Path) -> None:
    """Test SQL hygiene optimizations from finalize_snapshot_for_export."""
    # Create a test database with some data
    snapshot = tmp_path / "snapshot.sqlite3"
    conn = sqlite3.connect(snapshot)
    try:
        # Create tables with data
        conn.executescript(
            """
            CREATE TABLE test_data (id INTEGER PRIMARY KEY, data TEXT);
            INSERT INTO test_data (data) VALUES ('test1'), ('test2'), ('test3');
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Get initial file size
    initial_size = snapshot.stat().st_size

    # Verify WAL mode might exist (default for some SQLite versions)
    conn = sqlite3.connect(snapshot)
    try:
        # Create some operations that might leave WAL files
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO test_data (data) VALUES ('wal-test')")
        conn.commit()
    finally:
        conn.close()

    # Apply SQL hygiene optimizations
    finalize_snapshot_for_export(snapshot)

    # Verify journal mode is DELETE
    conn = sqlite3.connect(snapshot)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.lower() == "delete", f"Expected DELETE mode, got {journal_mode}"

        # Verify page size is 1024
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        assert page_size == 1024, f"Expected page size 1024, got {page_size}"

        # Verify data integrity (VACUUM shouldn't lose data)
        row_count = conn.execute("SELECT COUNT(*) FROM test_data").fetchone()[0]
        assert row_count == 4, f"Expected 4 rows after finalization, got {row_count}"
    finally:
        conn.close()

    # Verify no WAL or SHM files exist
    wal_file = Path(f"{snapshot}-wal")
    shm_file = Path(f"{snapshot}-shm")
    assert not wal_file.exists(), "WAL file should not exist after finalization"
    assert not shm_file.exists(), "SHM file should not exist after finalization"

    # Note: File size may increase or decrease depending on initial page size
    # and fragmentation, so we just verify it's reasonable (not empty, not corrupted)
    final_size = snapshot.stat().st_size
    assert final_size > 0, "Snapshot should not be empty after finalization"
    assert final_size < initial_size * 2, "Snapshot size should be reasonable"


def test_build_materialized_views(tmp_path: Path) -> None:
    """Test materialized view creation for httpvfs performance optimization."""
    # Create a test database with messages and attachments
    snapshot = tmp_path / "snapshot.sqlite3"
    conn = sqlite3.connect(snapshot)
    try:
        conn.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                contact_policy TEXT DEFAULT 'auto'
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                sender_id INTEGER,
                thread_id TEXT,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                ack_required INTEGER,
                created_ts TEXT,
                attachments TEXT
            );

            INSERT INTO projects (id, slug, human_key) VALUES (1, 'demo', 'demo-key');
            INSERT INTO agents (id, project_id, name) VALUES (1, 1, 'AgentAlice');
            INSERT INTO agents (id, project_id, name) VALUES (2, 1, 'AgentBob');

            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (
                1, 1, 1, 'thread-1', 'Test Message 1', 'Body 1', 'high', 1, '2025-01-01T00:00:00Z',
                '[{"type":"file","path":"test.txt","bytes":100,"media_type":"text/plain"}]'
            );

            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (
                2, 1, 2, 'thread-1', 'Test Message 2', 'Body 2', 'normal', 0, '2025-01-02T00:00:00Z',
                '[{"type":"inline","data_uri":"data:text/plain;base64,dGVzdA=="},{"type":"file","path":"doc.pdf","bytes":500,"media_type":"application/pdf"}]'
            );

            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (
                3, 1, 1, 'thread-2', 'Test Message 3', 'Body 3', 'normal', 0, '2025-01-03T00:00:00Z', '[]'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Build materialized views
    build_materialized_views(snapshot)

    # Verify message_overview_mv was created
    conn = sqlite3.connect(snapshot)
    try:
        # Check table exists
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='message_overview_mv'"
        ).fetchall()
        assert len(tables) == 1, "message_overview_mv should exist"

        # Check data is populated
        rows = conn.execute("SELECT * FROM message_overview_mv ORDER BY id").fetchall()
        assert len(rows) == 3, f"Expected 3 rows in message_overview_mv, got {len(rows)}"

        # Verify columns
        row = conn.execute("SELECT * FROM message_overview_mv WHERE id = 1").fetchone()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM message_overview_mv WHERE id = 1").fetchone()
        assert row["sender_name"] == "AgentAlice"
        assert row["sender_display"] == "AgentAlice"
        assert row["sender_project_slug"] is None
        assert row["sender_address"] is None
        assert row["subject"] == "Test Message 1"
        assert row["importance"] == "high"
        assert row["attachment_count"] == 1

        # Verify indexes exist
        indexes = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='index' AND tbl_name='message_overview_mv'
            AND name LIKE 'idx_msg_overview_%'
            """
        ).fetchall()
        assert len(indexes) >= 4, f"Expected at least 4 covering indexes, got {len(indexes)}"

        # Check attachments_by_message_mv was created
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='attachments_by_message_mv'"
        ).fetchall()
        assert len(tables) == 1, "attachments_by_message_mv should exist"

        # Check attachment data is flattened
        attach_rows = conn.execute("SELECT * FROM attachments_by_message_mv ORDER BY message_id").fetchall()
        # Message 1: 1 attachment, Message 2: 2 attachments, Message 3: 0 attachments
        assert len(attach_rows) == 3, f"Expected 3 flattened attachment rows, got {len(attach_rows)}"

        # Verify attachment details
        attach_row = conn.execute(
            "SELECT * FROM attachments_by_message_mv WHERE message_id = 1"
        ).fetchone()
        conn.row_factory = sqlite3.Row
        attach_row = conn.execute(
            "SELECT * FROM attachments_by_message_mv WHERE message_id = 1"
        ).fetchone()
        assert attach_row["attachment_type"] == "file"
        assert attach_row["media_type"] == "text/plain"
        assert attach_row["path"] == "test.txt"
        assert attach_row["size_bytes"] == 100

        # Verify attachment indexes exist
        attach_indexes = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='index' AND tbl_name='attachments_by_message_mv'
            AND name LIKE 'idx_attach_%'
            """
        ).fetchall()
        assert len(attach_indexes) >= 3, f"Expected at least 3 attachment indexes, got {len(attach_indexes)}"
    finally:
        conn.close()


def test_build_materialized_views_preserves_external_sender_origin(tmp_path: Path) -> None:
    snapshot = tmp_path / "cross_project_sender.sqlite3"
    conn = sqlite3.connect(snapshot)
    try:
        conn.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                contact_policy TEXT DEFAULT 'auto'
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                sender_id INTEGER,
                thread_id TEXT,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                ack_required INTEGER,
                created_ts TEXT,
                attachments TEXT
            );
            CREATE TABLE message_recipients (
                message_id INTEGER,
                agent_id INTEGER,
                kind TEXT,
                read_ts TEXT,
                ack_ts TEXT
            );
            """
        )
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'target', '/repo/target')")
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (2, 'source', '/repo/source')")
        conn.execute("INSERT INTO agents (id, project_id, name) VALUES (1, 1, 'BlueLake')")
        conn.execute("INSERT INTO agents (id, project_id, name) VALUES (2, 2, 'BlueLake')")
        conn.execute(
            """
            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (1, 1, 2, 'cross-thread', 'Cross Project', 'Body', 'high', 0, '2025-01-01T00:00:00Z', '[]')
            """
        )
        conn.execute(
            "INSERT INTO message_recipients (message_id, agent_id, kind, read_ts, ack_ts) VALUES (1, 1, 'to', NULL, NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    build_materialized_views(snapshot)

    conn = sqlite3.connect(snapshot)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT sender_name, sender_display, sender_project_id, sender_project_slug,
                   sender_project_name, sender_address
            FROM message_overview_mv
            WHERE id = 1
            """
        ).fetchone()
        assert row is not None
        assert row["sender_name"] == "BlueLake"
        assert row["sender_display"] == "BlueLake@source"
        assert row["sender_project_id"] == 2
        assert row["sender_project_slug"] == "source"
        assert row["sender_project_name"] == "/repo/source"
        assert row["sender_address"] == "project:source#BlueLake"
    finally:
        conn.close()


def test_build_materialized_views_supports_legacy_fts_without_sender_id(tmp_path: Path) -> None:
    snapshot = tmp_path / "legacy_fts.sqlite3"
    conn = sqlite3.connect(snapshot)
    try:
        conn.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT);
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                name TEXT,
                contact_policy TEXT DEFAULT 'auto'
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                thread_id TEXT,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                ack_required INTEGER,
                created_ts TEXT,
                attachments TEXT
            );
            CREATE TABLE message_recipients (
                message_id INTEGER,
                agent_id INTEGER,
                kind TEXT,
                read_ts TEXT,
                ack_ts TEXT
            );
            CREATE VIRTUAL TABLE fts_messages USING fts5(subject, body_md);
            """
        )
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'legacy', '/repo/legacy')")
        conn.execute("INSERT INTO agents (id, project_id, name) VALUES (1, 1, 'BlueLake')")
        conn.execute(
            """
            INSERT INTO messages (id, project_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (1, 1, 'legacy-thread', 'Legacy', 'Legacy body', 'normal', 0, '2025-01-01T00:00:00Z', '[]')
            """
        )
        conn.execute("INSERT INTO fts_messages (subject, body_md) VALUES ('Legacy', 'Legacy body')")
        conn.commit()
    finally:
        conn.close()

    build_materialized_views(snapshot)

    conn = sqlite3.connect(snapshot)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT sender_name, sender_display, sender_project_slug, sender_address
            FROM fts_search_overview_mv
            WHERE message_id = 1
            """
        ).fetchone()
        assert row is not None
        assert row["sender_name"] == ""
        assert row["sender_display"] == ""
        assert row["sender_project_slug"] is None
        assert row["sender_address"] is None
    finally:
        conn.close()
