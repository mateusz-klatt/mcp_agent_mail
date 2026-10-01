from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.http import SecurityAndRateLimitMiddleware, _decode_jwt_header_segment, build_http_app


@pytest.mark.asyncio
async def test_http_instances_keep_independent_bearer_and_cors_settings(isolated_env):
    settings = _config.get_settings()
    first_settings = replace(
        settings,
        http=replace(
            settings.http,
            bearer_token="first-instance-secret",
            allow_localhost_unauthenticated=False,
            jwt_enabled=False,
            rbac_enabled=False,
            rate_limit_enabled=False,
        ),
        cors=replace(settings.cors, enabled=True, origins=["https://first.example"]),
    )
    second_settings = replace(
        first_settings,
        http=replace(first_settings.http, bearer_token="second-instance-secret"),
        cors=replace(first_settings.cors, origins=["https://second.example"]),
    )
    first_app = build_http_app(first_settings)
    async with AsyncClient(transport=ASGITransport(app=first_app), base_url="http://test") as first:
        first_headers = {"Authorization": "Bearer first-instance-secret", "Origin": "https://first.example"}
        assert (await first.get("/docs", headers=first_headers)).status_code == 200

        second_app = build_http_app(second_settings)
        async with AsyncClient(transport=ASGITransport(app=second_app), base_url="http://test") as second:
            second_headers = {"Authorization": "Bearer second-instance-secret", "Origin": "https://second.example"}
            first_response, second_response = await asyncio.gather(
                first.get("/docs", headers=first_headers),
                second.get("/docs", headers=second_headers),
            )
            for response, origin in (
                (first_response, "https://first.example"),
                (second_response, "https://second.example"),
            ):
                assert response.status_code == 200
                assert response.headers["access-control-allow-origin"] == origin

            rejected = await asyncio.gather(
                first.get("/docs", headers=second_headers),
                second.get("/docs", headers=first_headers),
                first.get("/docs"),
                second.get("/docs"),
            )
            assert [response.status_code for response in rejected] == [401, 401, 401, 401]
            assert all("access-control-allow-origin" not in response.headers for response in rejected)


def test_decode_jwt_header_segment_variants():
    # Well-formed header
    import base64
    import json
    hdr = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode("utf-8")).rstrip(b"=")
    token = hdr.decode("ascii") + ".x.y"
    decoded = _decode_jwt_header_segment(token)
    assert decoded
    assert decoded.get("alg") == "HS256"
    # Malformed returns None
    assert _decode_jwt_header_segment("nope") is None


def test_rate_limits_for_branches(monkeypatch):
    _config.clear_settings_cache()
    settings = _config.get_settings()
    app = FastAPI()
    mw = SecurityAndRateLimitMiddleware(app, settings)
    assert mw._rate_limits_for("tools")[0] >= 1
    assert mw._rate_limits_for("resources")[0] >= 1
    assert mw._rate_limits_for("other")[0] >= 1
