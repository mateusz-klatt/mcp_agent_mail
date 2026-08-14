"""Tests for image processing edge cases in MCP Agent Mail.

Tests edge cases including:
- Malformed/corrupt image files
- Various image modes (palette, LA, RGBA, etc.)
- Invalid data URIs
- Truncated images
- Zero-byte files
- Very large images
- Unusual file extensions
"""

from __future__ import annotations

import base64
import contextlib
import io
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from PIL import Image

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from tests.keys import pkey

IMAGE_EDGE_AGENT = "codex-wsl-image-edge-1"


@pytest.fixture(autouse=True)
def _allow_absolute_attachment_paths(monkeypatch):
    """Every image here is written to ``tmp_path``, so every path is absolute.

    ``ab670c6`` began refusing absolute attachment paths unless this is set.
    These tests predate that gate by two months and were never revisited, so
    the refusal happened before any image was read: a corrupt file raised no
    decoding error, and a body naming three images stored none. The failures
    read as broken image handling and were the path policy every time.

    Scoped to this module rather than to ``isolated_env``, which 102 test
    files share — relaxing the default there would switch the gate off for the
    whole suite, and the one place it is deliberately exercised would keep
    passing with nothing behind it.
    """
    monkeypatch.setenv("ALLOW_ABSOLUTE_ATTACHMENT_PATHS", "true")
    _config.clear_settings_cache()
    yield
    _config.clear_settings_cache()


# =============================================================================
# Malformed Image Tests
# =============================================================================


@pytest.mark.asyncio
async def test_corrupt_image_file_gracefully_fails(isolated_env):
    """Corrupt image paths remain opaque Markdown until normalization exists."""

    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    corrupt_path = storage_root.parent / "corrupt.png"
    corrupt_path.write_bytes(b"this is not a valid image file at all")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": IMAGE_EDGE_AGENT,
            },
        )
        body_md = f"![img]({corrupt_path})"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Corrupt Image",
                "body_md": body_md,
                "idempotency_key": "image-edge-corrupt",
            },
        )
        message = (res.data.get("deliveries") or [{}])[0].get("message", {})
        assert message.get("body_md") == body_md
        assert message.get("attachments") == []
    corrupt_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_zero_byte_image_file(isolated_env):
    """Zero-byte image paths remain opaque Markdown until normalization exists."""

    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    empty_path = storage_root.parent / "empty.png"
    empty_path.write_bytes(b"")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        body_md = f"![img]({empty_path})"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Empty Image",
                "body_md": body_md,
                "idempotency_key": "image-edge-empty",
            },
        )
        message = (res.data.get("deliveries") or [{}])[0].get("message", {})
        assert message.get("body_md") == body_md
        assert message.get("attachments") == []
    empty_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_truncated_png_header_only(isolated_env):
    """Truncated image paths remain opaque Markdown until normalization exists."""

    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    truncated_path = storage_root.parent / "truncated.png"
    # PNG magic header only
    truncated_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        body_md = f"![img]({truncated_path})"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Truncated PNG",
                "body_md": body_md,
                "idempotency_key": "image-edge-truncated",
            },
        )
        message = (res.data.get("deliveries") or [{}])[0].get("message", {})
        assert message.get("body_md") == body_md
        assert message.get("attachments") == []
    truncated_path.unlink(missing_ok=True)


# =============================================================================
# Image Mode Tests
# =============================================================================


@pytest.mark.asyncio
async def test_palette_mode_image(isolated_env):
    """Test conversion of palette (P) mode image."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    palette_path = storage_root.parent / "palette.png"

    # Create palette mode image
    img = Image.new("P", (4, 4))
    img.putpalette(list(range(256)) * 3)  # Simple grayscale palette
    img.save(palette_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Palette Image",
                "body_md": f"![img]({palette_path})",
                "idempotency_key": "image-edge-palette",
            },
        )
        assert res.data.get("deliveries")
    palette_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_la_mode_image(isolated_env):
    """Test conversion of LA (luminance + alpha) mode image."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    la_path = storage_root.parent / "la_image.png"

    # Create LA mode image
    img = Image.new("LA", (4, 4), color=(128, 200))
    img.save(la_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "LA Image",
                "body_md": f"![img]({la_path})",
                "idempotency_key": "image-edge-la",
            },
        )
        assert res.data.get("deliveries")
        message = (res.data.get("deliveries") or [{}])[0].get("message", {})
        assert message.get("attachments") == []
    la_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_rgba_mode_image_preserves_alpha(isolated_env):
    """Test conversion of RGBA mode image preserves transparency."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    rgba_path = storage_root.parent / "rgba_image.png"

    # Create RGBA mode image with transparency
    img = Image.new("RGBA", (4, 4), color=(255, 0, 0, 128))  # Semi-transparent red
    img.save(rgba_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "RGBA Image",
                "body_md": f"![img]({rgba_path})",
                "idempotency_key": "image-edge-rgba",
            },
        )
        assert res.data.get("deliveries")
    rgba_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_grayscale_mode_image(isolated_env):
    """Test conversion of grayscale (L) mode image."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    gray_path = storage_root.parent / "gray_image.png"

    # Create L (grayscale) mode image
    img = Image.new("L", (4, 4), color=128)
    img.save(gray_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Grayscale Image",
                "body_md": f"![img]({gray_path})",
                "idempotency_key": "image-edge-grayscale",
            },
        )
        assert res.data.get("deliveries")
    gray_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_1bit_mode_image(isolated_env):
    """Test conversion of 1-bit (black and white) mode image."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    bw_path = storage_root.parent / "bw_image.png"

    # Create 1-bit mode image
    img = Image.new("1", (4, 4), color=1)
    img.save(bw_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "1-bit Image",
                "body_md": f"![img]({bw_path})",
                "idempotency_key": "image-edge-1bit",
            },
        )
        assert res.data.get("deliveries")
    bw_path.unlink(missing_ok=True)


# =============================================================================
# Data URI Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_malformed_data_uri_missing_comma(isolated_env, monkeypatch):
    """Test handling of malformed data URI without comma separator."""
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        # Malformed: no comma after base64
        body = "![img](data:image/pngbase64ABC123)"
        with pytest.raises(ToolError, match="bounded canonical inline representation"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": IMAGE_EDGE_AGENT,
                    "to": [IMAGE_EDGE_AGENT],
                    "subject": "Malformed URI",
                    "body_md": body,
                    "convert_images": False,
                    "idempotency_key": "image-edge-malformed-uri",
                },
            )


@pytest.mark.asyncio
async def test_data_uri_empty_base64(isolated_env, monkeypatch):
    """Test handling of data URI with empty base64 content."""
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        body = "![img](data:image/png;base64,)"
        with pytest.raises(ToolError, match="bounded canonical inline representation"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": IMAGE_EDGE_AGENT,
                    "to": [IMAGE_EDGE_AGENT],
                    "subject": "Empty Base64",
                    "body_md": body,
                    "convert_images": False,
                    "idempotency_key": "image-edge-empty-base64",
                },
            )


@pytest.mark.asyncio
async def test_data_uri_invalid_base64(isolated_env, monkeypatch):
    """Test handling of data URI with invalid base64 characters."""
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        body = "![img](data:image/png;base64,!!!not-valid-base64!!!)"
        with pytest.raises(ToolError, match="bounded canonical inline representation"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": IMAGE_EDGE_AGENT,
                    "to": [IMAGE_EDGE_AGENT],
                    "subject": "Invalid Base64",
                    "body_md": body,
                    "convert_images": False,
                    "idempotency_key": "image-edge-invalid-base64",
                },
            )


@pytest.mark.asyncio
async def test_data_uri_unusual_media_type(isolated_env, monkeypatch):
    """Test handling of data URI with unusual media type."""
    monkeypatch.setenv("CONVERT_IMAGES", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        payload = base64.b64encode(b"fake").decode()
        body = f"![img](data:image/x-custom-format;base64,{payload})"
        with pytest.raises(ToolError, match="bounded canonical inline representation"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": IMAGE_EDGE_AGENT,
                    "to": [IMAGE_EDGE_AGENT],
                    "subject": "Unusual Media Type",
                    "body_md": body,
                    "convert_images": False,
                    "idempotency_key": "image-edge-unusual-media",
                },
            )


# =============================================================================
# File Extension Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_image_without_extension(isolated_env):
    """Test handling of image file without extension."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    no_ext_path = storage_root.parent / "image_no_extension"

    # Create a valid PNG without file extension
    img = Image.new("RGB", (4, 4), color=(0, 255, 0))
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    no_ext_path.write_bytes(buffer.getvalue())

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "No Extension",
                "body_md": f"![img]({no_ext_path})",
                "idempotency_key": "image-edge-no-extension",
            },
        )
        assert res.data.get("deliveries")
    no_ext_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_image_wrong_extension(isolated_env):
    """Test handling of image with wrong extension (PNG saved as .jpg)."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    wrong_ext_path = storage_root.parent / "actually_png.jpg"

    # Create a PNG but save with .jpg extension
    img = Image.new("RGB", (4, 4), color=(0, 0, 255))
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    wrong_ext_path.write_bytes(buffer.getvalue())

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Wrong Extension",
                "body_md": f"![img]({wrong_ext_path})",
                "idempotency_key": "image-edge-wrong-extension",
            },
        )
        # Pillow should detect the actual format
        assert res.data.get("deliveries")
    wrong_ext_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_image_uppercase_extension(isolated_env):
    """Test handling of image with uppercase extension."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    upper_path = storage_root.parent / "IMAGE.PNG"

    img = Image.new("RGB", (4, 4), color=(255, 255, 0))
    img.save(upper_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Uppercase Extension",
                "body_md": f"![img]({upper_path})",
                "idempotency_key": "image-edge-uppercase-extension",
            },
        )
        assert res.data.get("deliveries")
    upper_path.unlink(missing_ok=True)


# =============================================================================
# Multiple Images Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_multiple_images_in_body(isolated_env):
    """Test handling of multiple images in a single message body."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    img_paths = []

    for i in range(3):
        path = storage_root.parent / f"multi_img_{i}.png"
        img = Image.new("RGB", (4, 4), color=(i * 80, i * 80, i * 80))
        img.save(path, "PNG")
        img_paths.append(path)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        body = "\n".join([f"![img{i}]({p})" for i, p in enumerate(img_paths)])
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Multiple Images",
                "body_md": body,
                "idempotency_key": "image-edge-multiple",
            },
        )
        assert res.data.get("deliveries")
        message = (res.data.get("deliveries") or [{}])[0].get("message", {})
        assert message.get("body_md") == body
        assert message.get("attachments") == []

    for p in img_paths:
        p.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_mixed_valid_and_invalid_images(isolated_env):
    """Mixed image paths remain opaque Markdown until normalization exists."""

    storage_root = Path(get_settings().storage.root).expanduser().resolve()

    valid_path = storage_root.parent / "valid_img.png"
    img = Image.new("RGB", (4, 4), color=(100, 100, 100))
    img.save(valid_path, "PNG")

    invalid_path = storage_root.parent / "invalid_img.png"
    invalid_path.write_bytes(b"not an image")

    missing_path = storage_root.parent / "totally_missing.png"

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        body = f"![valid]({valid_path})\n![invalid]({invalid_path})\n![missing]({missing_path})"
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Mixed Images",
                "body_md": body,
                "idempotency_key": "image-edge-mixed",
            },
        )
        message = (res.data.get("deliveries") or [{}])[0].get("message", {})
        assert message.get("body_md") == body
        assert message.get("attachments") == []

    valid_path.unlink(missing_ok=True)
    invalid_path.unlink(missing_ok=True)


# =============================================================================
# Attachment Path Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_attachment_path_with_spaces(isolated_env):
    """Test attachment path containing spaces."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    spaced_path = storage_root.parent / "path with spaces" / "image file.png"
    spaced_path.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", (4, 4), color=(200, 100, 50))
    img.save(spaced_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        with pytest.raises(ToolError, match="bounded canonical inline representation"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": IMAGE_EDGE_AGENT,
                    "to": [IMAGE_EDGE_AGENT],
                    "subject": "Spaced Path",
                    "body_md": "check attachment",
                    "attachment_paths": [str(spaced_path)],
                    "idempotency_key": "image-edge-spaced-attachment",
                },
            )

    spaced_path.unlink(missing_ok=True)
    spaced_path.parent.rmdir()


@pytest.mark.asyncio
async def test_attachment_path_unicode(isolated_env):
    """Test attachment path containing unicode characters."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    unicode_path = storage_root.parent / "image_\u4e2d\u6587_\u65e5\u672c\u8a9e.png"

    img = Image.new("RGB", (4, 4), color=(50, 150, 200))
    img.save(unicode_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        with pytest.raises(ToolError, match="bounded canonical inline representation"):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": IMAGE_EDGE_AGENT,
                    "to": [IMAGE_EDGE_AGENT],
                    "subject": "Unicode Path",
                    "body_md": "check attachment",
                    "attachment_paths": [str(unicode_path)],
                    "idempotency_key": "image-edge-unicode-attachment",
                },
            )

    unicode_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_attachment_symlink(isolated_env, tmp_path):
    """Test attachment via symlink."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    real_path = storage_root.parent / "real_image.png"
    link_path = storage_root.parent / "symlink_image.png"

    img = Image.new("RGB", (4, 4), color=(10, 20, 30))
    img.save(real_path, "PNG")

    # Create symlink
    try:
        link_path.symlink_to(real_path)
    except OSError:
        pytest.skip("Cannot create symlinks on this system")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Symlink Attachment",
                "body_md": f"![img]({link_path})",
                "idempotency_key": "image-edge-symlink-markdown",
            },
        )
        assert res.data.get("deliveries")

    link_path.unlink(missing_ok=True)
    real_path.unlink(missing_ok=True)


# =============================================================================
# Image Format Tests
# =============================================================================


@pytest.mark.asyncio
async def test_gif_image_conversion(isolated_env):
    """Test conversion of GIF image to WebP."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    gif_path = storage_root.parent / "image.gif"

    img = Image.new("RGB", (4, 4), color=(255, 0, 255))
    img.save(gif_path, "GIF")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "GIF Image",
                "body_md": f"![img]({gif_path})",
                "idempotency_key": "image-edge-gif",
            },
        )
        assert res.data.get("deliveries")
        message = (res.data.get("deliveries") or [{}])[0].get("message", {})
        assert message.get("attachments") == []

    gif_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_bmp_image_conversion(isolated_env):
    """Test conversion of BMP image to WebP."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    bmp_path = storage_root.parent / "image.bmp"

    img = Image.new("RGB", (4, 4), color=(0, 128, 255))
    img.save(bmp_path, "BMP")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "BMP Image",
                "body_md": f"![img]({bmp_path})",
                "idempotency_key": "image-edge-bmp",
            },
        )
        assert res.data.get("deliveries")

    bmp_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_jpeg_image_conversion(isolated_env):
    """Test conversion of JPEG image to WebP."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    jpeg_path = storage_root.parent / "image.jpeg"

    img = Image.new("RGB", (4, 4), color=(255, 200, 100))
    img.save(jpeg_path, "JPEG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "JPEG Image",
                "body_md": f"![img]({jpeg_path})",
                "idempotency_key": "image-edge-jpeg",
            },
        )
        assert res.data.get("deliveries")

    jpeg_path.unlink(missing_ok=True)


# =============================================================================
# Size and Memory Edge Cases
# =============================================================================


@pytest.mark.asyncio
async def test_single_pixel_image(isolated_env):
    """Test handling of 1x1 pixel image."""
    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    tiny_path = storage_root.parent / "tiny.png"

    img = Image.new("RGB", (1, 1), color=(128, 128, 128))
    img.save(tiny_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Tiny Image",
                "body_md": f"![img]({tiny_path})",
                "idempotency_key": "image-edge-single-pixel",
            },
        )
        assert res.data.get("deliveries")
        message = (res.data.get("deliveries") or [{}])[0].get("message", {})
        assert message.get("attachments") == []

    tiny_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_moderately_large_image(isolated_env, monkeypatch):
    """Test handling of moderately large image (triggers file mode)."""
    # Small inline threshold to force file mode
    monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "100")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    storage_root = Path(get_settings().storage.root).expanduser().resolve()
    large_path = storage_root.parent / "large_image.png"

    # Create a larger image (100x100 should exceed 100 bytes threshold)
    img = Image.new("RGB", (100, 100), color=(64, 128, 192))
    img.save(large_path, "PNG")

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {"project_key": "Backend", "program": "codex", "model": "gpt-5", "name": IMAGE_EDGE_AGENT},
        )
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": IMAGE_EDGE_AGENT,
                "to": [IMAGE_EDGE_AGENT],
                "subject": "Large Image",
                "body_md": f"![img]({large_path})",
                "idempotency_key": "image-edge-large",
            },
        )
        assert res.data.get("deliveries")
        message = (res.data.get("deliveries") or [{}])[0].get("message", {})
        assert message.get("attachments") == []

    large_path.unlink(missing_ok=True)
