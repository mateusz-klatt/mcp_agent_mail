from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from PIL import Image

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from tests.keys import pkey


@pytest.fixture(autouse=True)
def _allow_absolute_attachment_paths(monkeypatch):
    """Both tests here build their images next to the storage root, absolutely.

    Same cause as test_image_processing_edge.py: ``ab670c6`` gated absolute
    attachment paths and these predate it, so ``send_message`` refused before
    an attachment was ever written and the failure named the manifest.

    Nothing in this module asserts the refusal — that lives in
    test_security_path_traversal.py, which this fixture deliberately does not
    reach. Module scope rather than ``isolated_env`` for exactly that reason.
    """
    monkeypatch.setenv("ALLOW_ABSOLUTE_ATTACHMENT_PATHS", "true")
    _config.clear_settings_cache()
    yield
    _config.clear_settings_cache()


@pytest.mark.asyncio
async def test_attachment_paths_fail_closed_without_artifacts(isolated_env, monkeypatch):
    monkeypatch.setenv("KEEP_ORIGINAL_IMAGES", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    img_path = storage_root.parent / "img_o.png"
    img = Image.new("RGB", (4, 4), color=(0, 0, 255))
    img.save(img_path)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-attachments-1",
            },
        )
        with pytest.raises(ToolError, match="bounded canonical inline representation"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "codex-wsl-attachments-1",
                    "to": ["codex-wsl-attachments-1"],
                    "subject": "Orig",
                    "body_md": "see",
                    "attachment_paths": [str(img_path)],
                    "idempotency_key": "attachments-originals-disabled",
                },
            )
        # Rejection happens before any attachment artifact is created.
        proj = storage_root / "projects" / "backend"
        manifests = list((proj / "attachments" / "_manifests").glob("*.json"))
        assert not manifests
        originals = list((proj / "attachments" / "originals").rglob("*.*"))
        assert not originals
    img_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_attachment_paths_fail_closed_independent_of_inline_threshold(isolated_env, monkeypatch):
    # Large threshold -> inline
    monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "1048576")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    img_path = storage_root.parent / "img_t.png"
    img = Image.new("RGB", (8, 8), color=(255, 0, 0))
    img.save(img_path)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-attachments-1",
            },
        )
        with pytest.raises(ToolError, match="bounded canonical inline representation"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "codex-wsl-attachments-1",
                    "to": ["codex-wsl-attachments-1"],
                    "subject": "Inline",
                    "body_md": "body",
                    "attachment_paths": [str(img_path)],
                    "idempotency_key": "attachments-large-inline-threshold",
                },
            )

        # Small threshold -> file
        monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "1")
        with contextlib.suppress(Exception):
            _config.clear_settings_cache()
        with pytest.raises(ToolError, match="bounded canonical inline representation"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "codex-wsl-attachments-1",
                    "to": ["codex-wsl-attachments-1"],
                    "subject": "File",
                    "body_md": "body",
                    "attachment_paths": [str(img_path)],
                    "idempotency_key": "attachments-small-inline-threshold",
                },
            )
    img_path.unlink(missing_ok=True)
