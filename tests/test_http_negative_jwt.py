from __future__ import annotations

import contextlib
from typing import Any

import pytest
from authlib.jose import JsonWebKey, jwt
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app


def _rpc(method: str, params: dict) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


@pytest.mark.asyncio
async def test_http_jwt_bad_kid_rejected(isolated_env, monkeypatch):
    monkeypatch.setenv("HTTP_JWT_ENABLED", "true")
    monkeypatch.setenv("HTTP_JWT_ALGORITHMS", "RS256")
    monkeypatch.setenv("HTTP_JWT_JWKS_URL", "https://jwks.local/keys")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()

    # JWKS contains kid 'abc', but token will use 'xyz'
    private_jwk = JsonWebKey.generate_key("RSA", 2048, is_private=True).as_dict(is_private=True)
    public_jwk = JsonWebKey.import_key(private_jwk).as_dict(is_private=False)
    public_jwk["kid"] = "abc"
    jwks_payload = {"keys": [public_jwk]}

    async def fake_get(self, url: str):
        class _Resp:
            status_code = 200
            def raise_for_status(self) -> None:
                """A 200 raises nothing; present so the double matches a real Response."""
            def json(self) -> dict[str, Any]:
                return jwks_payload
        return _Resp()

    token = jwt.encode({"alg": "RS256", "kid": "xyz"}, {"sub": "u1", settings.http.jwt_role_claim: "reader"}, private_jwk).decode("utf-8")

    server = build_mcp_server()
    app = build_http_app(settings, server)
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get, raising=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_http_jwt_wrong_alg_rejected(isolated_env, monkeypatch):
    monkeypatch.setenv("HTTP_JWT_ENABLED", "true")
    monkeypatch.setenv("HTTP_JWT_ALGORITHMS", "HS256")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()

    # Build RS256 token while server expects HS256
    private_jwk = JsonWebKey.generate_key("RSA", 2048, is_private=True).as_dict(is_private=True)
    token = jwt.encode({"alg": "RS256"}, {"sub": "u1", settings.http.jwt_role_claim: "reader"}, private_jwk).decode("utf-8")

    server = build_mcp_server()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        # Should be 401 due to bad algorithm
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_http_jwt_missing_aud_iss_rejected_when_configured(isolated_env, monkeypatch):
    monkeypatch.setenv("HTTP_JWT_ENABLED", "true")
    monkeypatch.setenv("HTTP_JWT_ALGORITHMS", "HS256")
    monkeypatch.setenv("HTTP_JWT_SECRET", "secret")
    monkeypatch.setenv("HTTP_JWT_AUDIENCE", "api://me")
    monkeypatch.setenv("HTTP_JWT_ISSUER", "https://issuer")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    # Build token without aud/iss
    token = jwt.encode({"alg": "HS256"}, {"sub": "u1", settings.http.jwt_role_claim: "reader"}, settings.http.jwt_secret).decode("utf-8")
    server = build_mcp_server()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_http_jwt_malformed_token(isolated_env, monkeypatch):
    monkeypatch.setenv("HTTP_JWT_ENABLED", "true")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer not.a.jwt"}
        r = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r.status_code == 401



@pytest.mark.asyncio
async def test_jwks_error_status_body_is_not_trusted(monkeypatch):
    """A JWKS endpoint answering 5xx must not have its body installed as keys.

    Fetching only checked that the body parsed, never that the server said the
    body was an answer. So anything able to make that URL return an error page
    carrying a ``keys`` array -- a caching proxy, a captive portal, an origin
    under someone else's control -- got its own key trusted for token
    verification. The positive control below serves the identical body under
    200, so a failure here can only mean the status was ignored; it cannot mean
    the document was unusable.
    """
    import httpx

    from mcp_agent_mail import http as http_mod

    attacker_jwk = JsonWebKey.generate_key("RSA", 2048, is_private=True).as_dict(is_private=False)
    attacker_jwk["kid"] = "attacker"
    served = {"keys": [attacker_jwk]}
    status = {"code": 503}

    async def fake_get(self, url: str):
        # A real Response, so raise_for_status() and json() are the real ones.
        return httpx.Response(status["code"], json=served, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get, raising=False)

    http_mod.clear_jwks_cache()
    assert await http_mod._fetch_jwks("https://jwks.local/keys") is None, (
        "a 503 body was imported as a key set"
    )

    # Positive control: same bytes, status 200 -- this must succeed.
    status["code"] = 200
    http_mod.clear_jwks_cache()
    assert await http_mod._fetch_jwks("https://jwks.local/keys") is not None, (
        "the control failed, so the test above proves nothing about status"
    )
    http_mod.clear_jwks_cache()
