from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from authlib.jose import JsonWebKey, jwt
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import ClientCode
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import _AllowlistedGitHubTokenVerifier, build_http_app


def _rpc(method: str, params: dict) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


def _configure_oauth(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HTTP_OAUTH_ENABLED", "true")
    monkeypatch.setenv("HTTP_OAUTH_BASE_URL", "https://iris.example")
    monkeypatch.setenv("HTTP_OAUTH_GITHUB_CLIENT_ID", "test-github-client")
    monkeypatch.setenv(
        "HTTP_OAUTH_GITHUB_CLIENT_SECRET",
        "test-github-client-secret-" + ("s" * 32),
    )
    monkeypatch.setenv(
        "HTTP_OAUTH_GITHUB_ALLOWED_IDENTITIES",
        "id:12345,login:allowed-user",
    )
    monkeypatch.setenv(
        "HTTP_OAUTH_JWT_SIGNING_KEY",
        "test-oauth-signing-key-" + ("k" * 32),
    )
    monkeypatch.setenv("HTTP_OAUTH_STORAGE_PATH", str(tmp_path / "oauth"))
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    _config.clear_settings_cache()


@pytest.mark.asyncio
async def test_github_oauth_verifier_enforces_identity_allowlist(monkeypatch):
    verifier = _AllowlistedGitHubTokenVerifier(
        ["id:12345", "login:Allowed-User"]
    )

    async def fake_verify(token: str) -> AccessToken:
        identities = {
            "by-id": ("12345", "renamed-user"),
            "by-login": ("67890", "allowed-user"),
            "denied": ("99999", "attacker"),
        }
        github_id, github_login = identities[token]
        return AccessToken(
            token=token,
            client_id=github_id,
            scopes=[],
            expires_at=None,
            claims={"sub": github_id, "login": github_login},
        )

    monkeypatch.setattr(verifier._delegate, "verify_token", fake_verify)

    assert await verifier.verify_token("by-id") is not None
    assert await verifier.verify_token("by-login") is not None
    assert await verifier.verify_token("denied") is None


@pytest.mark.asyncio
async def test_http_oauth_discovery_and_challenge_are_public(
    isolated_env,
    monkeypatch,
    tmp_path,
):
    _configure_oauth(monkeypatch, tmp_path)
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://iris.example",
    ) as client:
        resource_metadata = await client.get(
            "/.well-known/oauth-protected-resource/mcp"
        )
        assert resource_metadata.status_code == 200
        assert resource_metadata.json() == {
            "resource": "https://iris.example/mcp",
            "authorization_servers": ["https://iris.example/"],
            "scopes_supported": [],
            "bearer_methods_supported": ["header"],
        }

        authorization_metadata = await client.get(
            "/.well-known/oauth-authorization-server"
        )
        assert authorization_metadata.status_code == 200
        metadata = authorization_metadata.json()
        assert metadata["issuer"] == "https://iris.example/"
        assert metadata["authorization_endpoint"] == "https://iris.example/authorize"
        assert metadata["token_endpoint"] == "https://iris.example/token"
        assert metadata["registration_endpoint"] == "https://iris.example/register"
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert metadata["token_endpoint_auth_methods_supported"] == ["none"]

        unauthorized = await client.post("/mcp/", json={})
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"] == (
            'Bearer resource_metadata="'
            'https://iris.example/.well-known/oauth-protected-resource/mcp"'
        )


@pytest.mark.asyncio
async def test_http_oauth_dcr_accepts_vscode_and_rejects_other_redirects(
    isolated_env,
    monkeypatch,
    tmp_path,
):
    _configure_oauth(monkeypatch, tmp_path)
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    registration = {
        "client_name": "Visual Studio Code",
        "redirect_uris": [
            "http://127.0.0.1:33418",
            "https://vscode.dev/redirect",
        ],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    code_verifier = "vscode-public-client-verifier-" + ("v" * 32)
    code_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        )
        .decode()
        .rstrip("=")
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://iris.example",
    ) as client:
        accepted = await client.post("/register", json=registration)
        assert accepted.status_code == 201
        accepted_body = accepted.json()
        assert accepted_body["client_id"]
        assert accepted_body.get("client_secret") is None
        assert accepted_body["token_endpoint_auth_method"] == "none"

        defaulted_public_client = await client.post(
            "/register",
            json={
                key: value
                for key, value in registration.items()
                if key != "token_endpoint_auth_method"
            },
        )
        assert defaulted_public_client.status_code == 201
        assert defaulted_public_client.json().get("client_secret") is None
        assert (
            defaulted_public_client.json()["token_endpoint_auth_method"]
            == "none"
        )

        mismatched_resource = await client.get(
            "/authorize",
            params={
                "client_id": accepted_body["client_id"],
                "response_type": "code",
                "redirect_uri": "http://127.0.0.1:33418",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": "wrong-resource-state",
                "resource": "https://attacker.example/mcp",
            },
        )
        assert mismatched_resource.status_code == 302
        mismatched_params = parse_qs(
            urlsplit(mismatched_resource.headers["location"]).query
        )
        assert mismatched_params["error"] == ["invalid_request"]
        assert mismatched_params["state"] == ["wrong-resource-state"]

        authorization = await client.get(
            "/authorize",
            params={
                "client_id": accepted_body["client_id"],
                "response_type": "code",
                "redirect_uri": "http://127.0.0.1:33418",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": "vscode-state",
                "resource": "https://iris.example/mcp",
            },
        )
        assert authorization.status_code == 302
        assert authorization.headers["location"].startswith(
            "https://iris.example/consent?txn_id="
        )
        consent = await client.get(authorization.headers["location"])
        assert consent.status_code == 200
        assert "Visual Studio Code" in consent.text
        assert "http://127.0.0.1:33418" in consent.text
        provider = app.state.oauth_provider
        txn_id = parse_qs(urlsplit(authorization.headers["location"]).query)[
            "txn_id"
        ][0]
        transaction = await provider._transaction_store.get(key=txn_id)
        assert transaction is not None
        consent_approval = await client.post(
            "/consent/submit",
            data={
                "txn_id": txn_id,
                "action": "approve",
                "csrf_token": transaction.csrf_token,
            },
        )
        assert consent_approval.status_code == 302
        upstream_authorization = urlsplit(
            consent_approval.headers["location"]
        )
        assert (
            upstream_authorization.scheme,
            upstream_authorization.netloc,
            upstream_authorization.path,
        ) == ("https", "github.com", "/login/oauth/authorize")
        upstream_params = parse_qs(upstream_authorization.query)
        assert upstream_params["client_id"] == ["test-github-client"]
        assert upstream_params["redirect_uri"] == [
            "https://iris.example/auth/callback"
        ]
        assert upstream_params["code_challenge_method"] == ["S256"]
        assert "resource" not in upstream_params

        rejected = await client.post(
            "/register",
            json={
                **registration,
                "redirect_uris": ["https://attacker.example/callback"],
            },
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"] == "invalid_redirect_uri"

        disguised_external_redirect = await client.post(
            "/register",
            json={
                **registration,
                "redirect_uris": [
                    "http://127.0.0.1:33418@attacker.example/callback"
                ],
            },
        )
        assert disguised_external_redirect.status_code == 400
        assert disguised_external_redirect.json()["error"] == "invalid_redirect_uri"

        async def accept_github_token(token: str) -> AccessToken | None:
            if token != "upstream-github-token":
                return None
            return AccessToken(
                token=token,
                client_id="12345",
                scopes=[],
                expires_at=None,
                claims={"sub": "12345", "login": "allowed-user"},
            )

        monkeypatch.setattr(
            provider._token_validator,
            "verify_token",
            accept_github_token,
        )
        client_code = "test-client-authorization-code"
        now = time.time()
        await provider._code_store.put(
            key=client_code,
            value=ClientCode(
                code=client_code,
                client_id=accepted_body["client_id"],
                redirect_uri="http://127.0.0.1:33418",
                code_challenge=code_challenge,
                code_challenge_method="S256",
                scopes=[],
                idp_tokens={"access_token": "upstream-github-token"},
                expires_at=now + 300,
                created_at=now,
            ),
            ttl=300,
        )
        token_response = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": accepted_body["client_id"],
                "code": client_code,
                "redirect_uri": "http://127.0.0.1:33418",
                "code_verifier": code_verifier,
                "resource": "https://iris.example/mcp",
            },
        )
        assert token_response.status_code == 200
        token_body = token_response.json()
        assert token_body["access_token"]
        assert token_body["expires_in"] == 30 * 24 * 60 * 60
        oauth_request = await client.post(
            settings.http.path,
            headers={"Authorization": f"Bearer {token_body['access_token']}"},
            json=_rpc(
                "tools/call",
                {"name": "health_check", "arguments": {}},
            ),
        )
        assert oauth_request.status_code == 200


@pytest.mark.asyncio
async def test_http_static_bearer_still_works_with_oauth_enabled(
    isolated_env,
    monkeypatch,
    tmp_path,
):
    _configure_oauth(monkeypatch, tmp_path)
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "existing-agent-bearer")
    _config.clear_settings_cache()
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://iris.example",
    ) as client:
        response = await client.post(
            settings.http.path,
            headers={"Authorization": "Bearer existing-agent-bearer"},
            json=_rpc(
                "tools/call",
                {"name": "health_check", "arguments": {}},
            ),
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_http_oauth_token_coexists_with_legacy_jwt_validation(
    isolated_env,
    monkeypatch,
    tmp_path,
):
    _configure_oauth(monkeypatch, tmp_path)
    monkeypatch.setenv("HTTP_JWT_ENABLED", "true")
    _config.clear_settings_cache()
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())

    async def accept_oauth_token(token: str) -> AccessToken | None:
        if token != "oauth-access-token":
            return None
        return AccessToken(
            token=token,
            client_id="vscode",
            scopes=[],
            expires_at=None,
            claims={"sub": "12345", "login": "allowed-user"},
        )

    monkeypatch.setattr(
        app.state.oauth_provider,
        "verify_token",
        accept_oauth_token,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://iris.example",
    ) as client:
        response = await client.post(
            settings.http.path,
            headers={"Authorization": "Bearer oauth-access-token"},
            json=_rpc(
                "tools/call",
                {"name": "health_check", "arguments": {}},
            ),
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_http_bearer_and_cors_preflight(isolated_env, monkeypatch):
    # Enable Bearer and CORS
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "token123")
    monkeypatch.setenv("HTTP_CORS_ENABLED", "true")
    monkeypatch.setenv("HTTP_CORS_ORIGINS", "http://example.com")
    # Disable localhost auto-authentication to properly test bearer auth
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Preflight OPTIONS
        r0 = await client.options(settings.http.path, headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "POST",
        })
        assert r0.status_code in (200, 204)
        # No bearer -> 401
        r1 = await client.post(settings.http.path, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r1.status_code == 401
        # With bearer
        r2 = await client.post(
            settings.http.path,
            headers={"Authorization": "Bearer token123", "Origin": "http://example.com"},
            json=_rpc("tools/call", {"name": "health_check", "arguments": {}}),
        )
        assert r2.status_code == 200
        # CORS header present on response
        assert r2.headers.get("access-control-allow-origin") in ("*", "http://example.com")


@pytest.mark.asyncio
async def test_http_jwks_validation_and_resource_rate_limit(isolated_env, monkeypatch):
    # Configure JWT with JWKS and strict resource rate limit
    monkeypatch.setenv("HTTP_JWT_ENABLED", "true")
    monkeypatch.setenv("HTTP_JWT_ALGORITHMS", "RS256")
    monkeypatch.setenv("HTTP_RBAC_ENABLED", "true")
    monkeypatch.setenv("HTTP_RBAC_READER_ROLES", "reader")
    monkeypatch.setenv("HTTP_RBAC_WRITER_ROLES", "writer")
    monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("HTTP_RATE_LIMIT_RESOURCES_PER_MINUTE", "1")
    monkeypatch.setenv("HTTP_RATE_LIMIT_TOOLS_PER_MINUTE", "10")
    # Provide a JWKS URL (dummy) and monkeypatch HTTP call
    monkeypatch.setenv("HTTP_JWT_JWKS_URL", "https://jwks.local/keys")
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    settings = _config.get_settings()

    # Generate RSA key + JWKS using Authlib utilities
    private_jwk = JsonWebKey.generate_key("RSA", 2048, is_private=True).as_dict(is_private=True)
    private_jwk["kid"] = "abc"
    public_jwk = JsonWebKey.import_key(private_jwk).as_dict(is_private=False)
    jwks_payload = {"keys": [public_jwk]}

    async def fake_get(self, url: str):
        class _Resp:
            status_code = 200
            def raise_for_status(self) -> None:
                """A 200 raises nothing; present so the double matches a real Response."""
            def json(self) -> dict[str, Any]:
                return jwks_payload
        return _Resp()

    # Build token with RS256
    token = (
        jwt.encode(
            {"alg": "RS256", "kid": "abc"},
            {"sub": "u1", settings.http.jwt_role_claim: "reader"},
            private_jwk,
        ).decode("utf-8")
    )

    server = build_mcp_server()
    app = build_http_app(settings, server)

    # Patch httpx.AsyncClient.get used in JWKS fetch path
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get, raising=False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {token}"}
        # Reader can call read-only tool
        r = await client.post(settings.http.path, headers=headers, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r.status_code == 200
        # Resource rate limit 1 rpm -> second call 429
        r1 = await client.post(settings.http.path, headers=headers, json=_rpc("resources/read", {"uri": "resource://tooling/projects"}))
        assert r1.status_code in (200, 429)
        r2 = await client.post(settings.http.path, headers=headers, json=_rpc("resources/read", {"uri": "resource://tooling/projects"}))
        assert r2.status_code == 429


@pytest.mark.asyncio
async def test_http_path_mount_trailing_and_no_slash(isolated_env):
    server = build_mcp_server()
    settings = _config.get_settings()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        base = settings.http.path.rstrip("/")
        r1 = await client.post(base, json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r1.status_code in (200, 401, 403)
        r2 = await client.post(base + "/", json=_rpc("tools/call", {"name": "health_check", "arguments": {}}))
        assert r2.status_code in (200, 401, 403)


@pytest.mark.asyncio
async def test_http_readiness_endpoint(isolated_env):
    server = build_mcp_server()
    settings = _config.get_settings()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health/readiness")
        assert r.status_code in (200, 503)


@pytest.mark.asyncio
async def test_retired_http_lock_status_endpoint_is_not_exposed(isolated_env, open_mail_ui_gate):
    # The legacy lock-inspection API is no longer part of the single React UI.
    server = build_mcp_server()
    settings = _config.get_settings()
    app = build_http_app(settings, server)

    storage_root = Path(settings.storage.root).expanduser().resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / ".archive.lock"
    lock_path.touch()
    metadata_path = storage_root / ".archive.lock.owner.json"
    metadata_path.write_text(json.dumps({"pid": 999_999, "created_ts": time.time() - 400}), encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/mail/api/locks")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}
