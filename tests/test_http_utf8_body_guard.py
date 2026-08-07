"""Undecodable request bodies must answer 400, not 500.

The MCP SDK's POST handler raises on a body it cannot decode, and the reply
that reaches the caller is `500 Error handling POST request`. That answer is
wrong in both halves: it blames the server for the caller's bytes, and it names
nothing the caller could act on. In practice the bytes come from a shell on a
non-English Windows whose console codepage is not UTF-8 — and the hook clients
that swallow errors turn the 500 into an empty result, which is
indistinguishable from "nothing to report". A message can vanish with no trace.

These tests pin both directions: a legal body still gets through untouched
(that is the half most at risk from a guard that buffers), and an illegal one
gets a 400 that says why.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import Utf8BodyGuardMiddleware, build_http_app

TOKEN = "utf8-guard-token"


def _rpc(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tools/call",
        "params": {"name": "health_check", "arguments": arguments},
    }


def _build(monkeypatch) -> tuple[Any, Any]:
    monkeypatch.setenv("HTTP_BEARER_TOKEN", TOKEN)
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    return settings, build_http_app(settings, build_mcp_server())


HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.mark.asyncio
async def test_utf8_body_is_accepted(isolated_env, monkeypatch):
    """A body carrying non-ASCII as real UTF-8 reaches the tool.

    This is the case the guard must not break. Buffering a request body is
    where middleware most often goes wrong, so assert the happy path with
    bytes that actually exercise the decode.
    """
    settings, app = _build(monkeypatch)
    body = json.dumps(_rpc({}), ensure_ascii=False).encode("utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(settings.http.path, headers=HEADERS, content=body)

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_non_utf8_body_is_rejected_with_400(isolated_env, monkeypatch):
    """A body in a legacy codepage answers 400 and says what is wrong."""
    settings, app = _build(monkeypatch)
    # cp1250 is what Git Bash sends on a Polish Windows. 0x9C ("ś") is not a
    # legal UTF-8 lead byte, so this cannot be decoded by luck.
    body = json.dumps(_rpc({}), ensure_ascii=False)[:-1].encode("utf-8") + "ś".encode("cp1250") + b"}"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(settings.http.path, headers=HEADERS, content=body)

    assert response.status_code == 400
    detail = response.json()["detail"]
    # The offset is the one thing that lets a caller find the offending byte.
    assert "UTF-8" in detail and "offset" in detail


@pytest.mark.asyncio
async def test_ascii_escaped_body_is_accepted(isolated_env, monkeypatch):
    """`\\uXXXX` escapes carry non-ASCII with no encoding to get wrong.

    This is what `am_call` emits (`jq -a`), so it is the shape every hook on
    every platform actually sends.
    """
    settings, app = _build(monkeypatch)
    body = json.dumps(_rpc({}), ensure_ascii=True).encode("ascii")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(settings.http.path, headers=HEADERS, content=body)

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_is_never_intercepted():
    """Non-POST traffic passes through without the body ever being read.

    The long-lived SSE GET is the reason this matters: buffering it would hang
    the stream forever rather than fail visibly.
    """
    seen: dict[str, Any] = {}

    async def downstream(scope, receive, send):
        seen["reached"] = True
        # The guard must not have consumed anything on our behalf.
        seen["receive_is_original"] = receive is original_receive

    async def original_receive():  # pragma: no cover - must never be called
        raise AssertionError("body read on a GET")

    async def send(_message):  # pragma: no cover - nothing is sent here
        pass

    guard = Utf8BodyGuardMiddleware(downstream)
    await guard({"type": "http", "method": "GET", "headers": []}, original_receive, send)

    assert seen == {"reached": True, "receive_is_original": True}


@pytest.mark.asyncio
async def test_oversized_body_is_passed_through_unbuffered():
    """A body above the inspection cap is forwarded, not held in memory."""
    forwarded: dict[str, Any] = {}

    async def downstream(scope, receive, send):
        forwarded["receive_is_original"] = receive is original_receive

    async def original_receive():  # pragma: no cover - must never be called
        raise AssertionError("oversized body was buffered")

    async def send(_message):  # pragma: no cover
        pass

    too_big = str(Utf8BodyGuardMiddleware.MAX_INSPECT_BYTES + 1).encode()
    guard = Utf8BodyGuardMiddleware(downstream)
    await guard(
        {"type": "http", "method": "POST", "headers": [(b"content-length", too_big)]},
        original_receive,
        send,
    )

    assert forwarded == {"receive_is_original": True}
