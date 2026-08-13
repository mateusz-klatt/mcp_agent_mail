from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import warnings
from pathlib import Path
from zipfile import ZipFile

import pytest
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from typer.testing import CliRunner

from mcp_agent_mail import cli as cli_module
from mcp_agent_mail.config import clear_settings_cache, get_settings

warnings.filterwarnings("ignore", category=ResourceWarning)

console = Console()


def _seed_mailbox(
    db_path: Path,
    storage_root: Path,
    *,
    message_count: int = 1,
) -> None:
    storage_root.mkdir(parents=True, exist_ok=True)
    attachments_dir = storage_root / "attachments" / "raw"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    (attachments_dir / "inline.txt").write_text("inline bytes", encoding="utf-8")
    (attachments_dir / "bundle.bin").write_bytes(b"B" * 256)
    (attachments_dir / "huge.dat").write_bytes(b"H" * 1024 * 32)

    conn = sqlite3.connect(db_path)
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
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'primary', 'Primary Mail')")
        conn.execute("INSERT INTO agents (id, project_id, name) VALUES (1, 1, 'Integration Bot')")

        attachments = [
            {
                "type": "file",
                "media_type": "text/plain",
                "path": "attachments/raw/inline.txt",
            },
            {
                "type": "file",
                "media_type": "application/octet-stream",
                "path": "attachments/raw/bundle.bin",
            },
            {
                "type": "file",
                "media_type": "application/octet-stream",
                "path": "attachments/raw/huge.dat",
            },
        ]

        conn.execute(
            """
            INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts, attachments)
            VALUES (
                1,
                1,
                1,
                'integration-thread',
                'Integration Test',
                'Body with bearer TOKEN [Docs](https://docs.example.invalid/uat) <img src="https://viewer-probe.invalid/raw.png"> ![probe](https://viewer-probe.invalid/markdown.png) <script>window._xss=1</script>',
                'normal',
                1,
                '2025-01-01T00:00:00Z',
                ?
            )
            """,
            (json.dumps(attachments),),
        )
        if message_count > 1:
            conn.executemany(
                """
                INSERT INTO messages (
                    id, project_id, sender_id, thread_id, subject, body_md,
                    importance, ack_required, created_ts, attachments
                )
                VALUES (?, 1, 1, 'bulk-thread', ?, ?, 'normal', 0,
                        '2024-01-01T00:00:00Z', '[]')
                """,
                (
                    (message_id, f"Bulk Message {message_id}", f"Bulk body {message_id}")
                    for message_id in range(2, message_count + 1)
                ),
            )
        conn.execute(
            """
            INSERT INTO message_recipients (message_id, agent_id, kind, read_ts, ack_ts)
            VALUES (1, 1, 'to', '2025-01-02T00:00:00Z', '2025-01-03T00:00:00Z')
            """
        )
        conn.execute("INSERT INTO file_reservations (id, project_id) VALUES (1, 1)")
        conn.execute(
            "INSERT INTO agent_links (id, a_project_id, b_project_id) VALUES (1, 1, 1)"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.usefixtures("isolated_env")
def test_share_export_end_to_end(monkeypatch, tmp_path: Path) -> None:
    settings = get_settings()
    db_path = Path(settings.database.url.replace("sqlite+aiosqlite:///", ""))
    storage_root = Path(settings.storage.root)
    _seed_mailbox(db_path, storage_root)

    output_dir = tmp_path / "bundle"
    runner = CliRunner()

    console.print(Panel.fit("🚀 Starting mailbox share export integration test"))

    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/incubator")

    table = Table(title="Export Configuration")
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("Database", str(db_path))
    table.add_row("Storage root", str(storage_root))
    table.add_row("Output Dir", str(output_dir))
    table.add_row("Inline Threshold", "64 bytes")
    table.add_row("Detach Threshold", "10240 bytes")
    console.print(table)

    result = runner.invoke(
        cli_module.app,
        [
            "share",
            "export",
            "--output",
            str(output_dir),
            "--project",
            "primary",
            "--inline-threshold",
            "64",
            "--detach-threshold",
            "10240",
        ],
    )
    console.print(Syntax(result.output, "text", theme="ansi_light"))
    assert result.exit_code == 0

    manifest_path = output_dir / "manifest.json"
    assert manifest_path.is_file()
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)

    console.print(
        Panel(
            Syntax(json.dumps(manifest, indent=2), "json", theme="ansi_light"),
            title="Manifest Snapshot",
            border_style="cyan",
        )
    )

    stats = manifest["attachments"]["stats"]
    assert stats["inline"] == 0
    assert stats["copied"] == 0
    assert stats["externalized"] == 0
    assert stats["missing"] == 0
    assert manifest["attachments"]["items"] == []
    assert manifest["scrub"]["preset"] == "standard"
    assert manifest["scrub"]["attachments_cleared"] == 1

    hosting_detected = {entry["id"] for entry in manifest.get("hosting", {}).get("detected", [])}
    assert "github_pages" in hosting_detected

    redirect_content = (output_dir / "index.html").read_text(encoding="utf-8")
    assert "<title>Iris · Agent Mail Viewer</title>" in redirect_content
    assert '<h1><span aria-hidden="true">🌈</span> Iris</h1>' in redirect_content
    assert "%26%23x1F308%3B" in redirect_content

    viewer_dir = output_dir / "viewer"
    assert (viewer_dir / "index.html").is_file()
    assert (viewer_dir / "styles.css").is_file()
    assert (viewer_dir / "viewer.js").is_file()
    assert (viewer_dir / "THIRD_PARTY_LICENSES.txt").is_file()
    assert (viewer_dir / "vendor" / "alpine.min.js").is_file()
    assert (viewer_dir / "vendor" / "lucide.min.js").is_file()
    assert (viewer_dir / "vendor" / "tailwind.min.css").is_file()
    index_content = (viewer_dir / "index.html").read_text(encoding="utf-8")
    # The legacy "Static Viewer" compat marker was removed (#224); assert the
    # stable page title instead, which still proves index.html was exported.
    assert "Iris · Agent Mail Viewer" in index_content
    assert '<span aria-hidden="true">🌈</span> Iris' in index_content
    assert "https://cdn." not in index_content
    assert "https://unpkg.com" not in index_content
    assert "https://fonts." not in index_content

    zip_path = output_dir.with_suffix(".zip")
    assert zip_path.is_file()
    with ZipFile(zip_path) as archive:
        names = archive.namelist()
    console.print(
        Panel.fit(
            "\n".join(names),
            title="ZIP Contents",
            border_style="magenta",
        )
    )
    assert "manifest.json" in names
    assert "mailbox.sqlite3" in names
    assert "viewer/index.html" in names

    readme_text = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "Detected hosting targets" in readme_text

    deployment_text = (output_dir / "HOW_TO_DEPLOY.md").read_text(encoding="utf-8")
    assert "## GitHub Pages (detected)" in deployment_text


@pytest.mark.usefixtures("isolated_env")
def test_viewer_playwright_smoke(monkeypatch, tmp_path: Path) -> None:
    playwright_sync = pytest.importorskip("playwright.sync_api")

    db_path = tmp_path / "playwright.sqlite3"
    storage_root = tmp_path / "storage"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    clear_settings_cache()
    get_settings()  # prime settings with new env values

    if db_path.exists():
        db_path.unlink()
    if storage_root.exists():
        shutil.rmtree(storage_root)

    _seed_mailbox(db_path, storage_root, message_count=1_001)

    output_dir = tmp_path / "bundle_playwright"
    runner = CliRunner()
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
            "10240",
        ],
    )
    assert result.exit_code == 0, result.output

    server = cli_module._start_preview_server(output_dir, "127.0.0.1", 0)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.25)

    try:
        with playwright_sync.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:  # pragma: no cover - local browser availability
                if os.environ.get("AGENT_MAIL_BROWSER_UAT_REQUIRED") == "1":
                    pytest.fail(f"Chromium must launch in CI after the install step: {exc}")
                pytest.skip(f"Chromium not installed for local Playwright UAT: {exc}")
            context = browser.new_context(viewport={"width": 390, "height": 844})
            page = context.new_page()
            server_host = host or "127.0.0.1"
            origin = f"http://{server_host}:{port}"
            request_urls: list[str] = []
            console_messages: list[tuple[str, str]] = []
            page_errors: list[str] = []
            page.on("request", lambda request: request_urls.append(request.url))
            page.on(
                "console",
                lambda message: console_messages.append((message.type, message.text)),
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(f"{origin}/viewer/index.html", wait_until="networkidle")

            def assert_mobile_touch_targets() -> None:
                page.wait_for_timeout(175)
                layout = page.evaluate(
                    """
                    () => {
                      const viewportWidth = document.documentElement.clientWidth;
                      const visibleRect = (element) => {
                        const style = getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return {
                          rect,
                          visible: style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && Number(style.opacity) > 0
                            && rect.width > 0
                            && rect.height > 0,
                        };
                      };
                      const undersized = Array.from(document.querySelectorAll(
                        'button, select, input[type="search"], input[type="checkbox"], a[href], [role="button"]'
                      )).flatMap((element) => {
                        const { rect, visible } = visibleRect(element);
                        const active = visible
                          && getComputedStyle(element).pointerEvents !== 'none'
                          && !element.matches(':disabled')
                          && element.getAttribute('aria-hidden') !== 'true'
                          && rect.right > 0
                          && rect.bottom > 0
                          && rect.left < viewportWidth
                          && rect.top < innerHeight;
                        if (!active || (rect.width >= 43.5 && rect.height >= 43.5)) return [];
                        return [{
                          tag: element.tagName.toLowerCase(),
                          label: element.getAttribute('aria-label')
                            || element.textContent.trim().replace(/\\s+/g, ' ').slice(0, 80),
                          width: Math.round(rect.width * 10) / 10,
                          height: Math.round(rect.height * 10) / 10,
                        }];
                      });
                      const outOfViewport = Array.from(document.body.querySelectorAll('*')).flatMap((element) => {
                        const { rect, visible } = visibleRect(element);
                        if (!visible || (rect.left >= -0.5 && rect.right <= viewportWidth + 0.5)) return [];
                        return [{
                          tag: element.tagName.toLowerCase(),
                          id: element.id,
                          className: typeof element.className === 'string'
                            ? element.className.slice(0, 120)
                            : '',
                          left: Math.round(rect.left * 10) / 10,
                          right: Math.round(rect.right * 10) / 10,
                        }];
                      });
                      return {
                        undersized,
                        outOfViewport,
                        viewportWidth,
                        bodyScrollWidth: document.body.scrollWidth,
                        documentScrollWidth: document.documentElement.scrollWidth,
                        scrollX,
                      };
                    }
                    """
                )
                assert layout["undersized"] == []
                assert layout["outOfViewport"] == []
                assert layout["bodyScrollWidth"] <= layout["viewportWidth"]
                assert layout["documentScrollWidth"] <= layout["viewportWidth"]
                assert layout["scrollX"] == 0

            # The legacy #message-list shim was removed (#224); the live viewer
            # renders rows as `.message-row` divs inside the virtual list.
            page.wait_for_selector("#virtual-message-list .message-row")
            first_entry = page.inner_text("#virtual-message-list .message-row")
            assert "Integration Test" in (first_entry or "")
            assert page.evaluate("typeof window.Alpine") == "object"
            assert page.evaluate("typeof window.Clusterize") == "undefined"
            assert page.locator("svg.lucide").count() > 0
            initial_window_ids = page.locator(
                "#virtual-message-list .message-row"
            ).evaluate_all(
                "rows => rows.map(row => row.getAttribute('data-message-id'))"
            )
            assert 0 < len(initial_window_ids) < 50
            assert page.locator("#virtual-message-list .virtual-spacer").count() == 2
            assert page.evaluate(
                """() => {
                    const list = document.getElementById('virtual-message-list');
                    return list.scrollHeight > list.clientHeight * 10;
                }"""
            )
            page.evaluate(
                """() => {
                    const list = document.getElementById('virtual-message-list');
                    list.scrollTop = list.scrollHeight;
                }"""
            )
            page.wait_for_function(
                """() => {
                    const row = document.querySelector(
                        '#virtual-message-list .message-row'
                    );
                    return row && row.getAttribute('data-message-id') !== '1';
                }"""
            )
            tail_window_ids = page.locator(
                "#virtual-message-list .message-row"
            ).evaluate_all(
                "rows => rows.map(row => row.getAttribute('data-message-id'))"
            )
            assert 0 < len(tail_window_ids) < 50
            assert set(initial_window_ids).isdisjoint(tail_window_ids)
            page.evaluate(
                "document.getElementById('virtual-message-list').scrollTop = 0"
            )
            page.wait_for_function(
                """() => document.querySelector(
                    '#virtual-message-list .message-row'
                )?.getAttribute('data-message-id') === '1'"""
            )
            assert_mobile_touch_targets()

            page.locator('[data-select-message-id="1"]').click()
            page.wait_for_function(
                "document.querySelector('[data-select-message-id=\"1\"]')?.getAttribute('aria-pressed') === 'true'"
            )
            assert_mobile_touch_targets()

            page.get_by_role("button", name="Sort messages").click()
            page.get_by_role("button", name="Newest First").wait_for(state="visible")
            assert_mobile_touch_targets()
            page.get_by_role("button", name="Newest First").click()

            page.get_by_role("button", name="Toggle message filters").first.click()
            page.locator("select").first.wait_for(state="visible")
            assert_mobile_touch_targets()
            page.get_by_role("button", name="Toggle message filters").first.click()

            page.fill("#unified-search", "does-not-exist")
            assert_mobile_touch_targets()
            page.wait_for_function(
                "document.querySelectorAll('#virtual-message-list .message-row').length === 0"
            )
            page.fill("#unified-search", "Integration")
            page.wait_for_function(
                "document.querySelectorAll('#virtual-message-list .message-row').length === 1"
            )
            page.locator("#virtual-message-list .message-row").click(
                position={"x": 120, "y": 40}
            )
            page.get_by_role("button", name="Close message view").wait_for(
                state="visible"
            )
            assert page.locator("img[src^='https://viewer-probe.invalid']").count() == 0
            body_link = page.get_by_role("link", name="Docs")
            assert body_link.count() == 1
            body_link_box = body_link.bounding_box()
            assert body_link_box is not None
            assert body_link_box["width"] >= 43.5
            assert body_link_box["height"] >= 43.5
            assert_mobile_touch_targets()
            page.get_by_role("button", name="Close message view").click()

            page.get_by_role("button", name="Threads", exact=True).click()
            page.locator("#thread-search-input").wait_for(state="visible")
            assert_mobile_touch_targets()
            page.locator("[data-thread-id]").first.click()
            assert_mobile_touch_targets()
            external_requests = [
                url
                for url in request_urls
                if not url.startswith((origin, "blob:", "data:"))
            ]
            assert external_requests == []
            assert any(
                url.startswith(f"{origin}/__preview__/status") for url in request_urls
            )
            assert page_errors == []
            assert [
                entry
                for entry in console_messages
                if entry[0] in {"error", "warning"}
            ] == []
            # Ensure sanitization removed inline script execution.
            xss_value = page.evaluate("window._xss || null")
            assert xss_value is None
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        clear_settings_cache()
