from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import respx
from authlib.jose import JsonWebKey, jwt
from fastmcp.server.auth import AccessToken
from httpx import ASGITransport, AsyncClient, Response as HttpxResponse

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import _AllowlistedGitHubTokenVerifier, build_http_app


def _rpc(method: str, params: dict) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


@contextlib.asynccontextmanager
async def _oauth_client(app: Any) -> AsyncIterator[AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://iris.example",
        ) as client,
    ):
        yield client


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
    monkeypatch.setenv("HTTP_OAUTH_DCR_RATE_LIMIT_PER_MINUTE", "10")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    monkeypatch.setenv("HTTP_PATH", "/api/")
    _config.clear_settings_cache()


def _vscode_registration() -> dict[str, Any]:
    return {
        "client_name": "Visual Studio Code",
        "redirect_uris": [
            "https://insiders.vscode.dev/redirect",
            "https://vscode.dev/redirect",
            "http://127.0.0.1/",
            "http://127.0.0.1:33418/",
        ],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "native",
    }


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
    provider = app.state.oauth_provider

    async with _oauth_client(app) as client:
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
        assert "client_id_metadata_document_supported" not in metadata

        for resource_path in ("/mcp", "/mcp/"):
            unauthorized = await client.post(resource_path, json={})
            assert unauthorized.status_code == 401
            assert unauthorized.headers["www-authenticate"] == (
                'Bearer resource_metadata="'
                'https://iris.example/.well-known/oauth-protected-resource/mcp"'
            )
    assert provider._github_http_client.is_closed is True
    oauth_dirs = (
        tmp_path / "oauth",
        tmp_path / "oauth" / "protected",
        tmp_path / "oauth" / "registrations",
    )
    # The layout claim holds everywhere; the permission claim does not.
    for oauth_dir in oauth_dirs:
        assert oauth_dir.is_dir()
    if os.name == "nt":
        # Windows has no POSIX mode bits: `mkdir(mode=0o700)` and `chmod(0o700)`
        # in _oauth_storage_path() are accepted and then ignored, so st_mode
        # comes back 0o777. Asserting 0o700 here would only ever be a claim
        # about the platform, never about our code. The directories are
        # therefore NOT owner-private on Windows -- production runs Linux, so
        # this is a gap in local dev, not in the deployment.
        for oauth_dir in oauth_dirs:
            assert oauth_dir.stat().st_mode & 0o777 == 0o777
    else:
        for oauth_dir in oauth_dirs:
            assert oauth_dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_oauth_disabled_metadata_probes_remain_public_with_bearer(
    isolated_env,
    monkeypatch,
):
    monkeypatch.setenv("HTTP_OAUTH_ENABLED", "false")
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "existing-agent-bearer")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    monkeypatch.setenv("HTTP_PATH", "/api/")
    _config.clear_settings_cache()
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://iris.example",
    ) as client:
        for path in (
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource/mcp",
            "/api/.well-known/oauth-authorization-server",
            "/.well-known/oauth-authorization-server/mcp",
        ):
            response = await client.get(path)
            assert response.status_code == 404
            assert response.json() == {"mcp_oauth": False}


@pytest.mark.asyncio
async def test_oauth_callback_redacts_idp_failures(
    isolated_env,
    monkeypatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
):
    _configure_oauth(monkeypatch, tmp_path)
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    callback_uri = "http://127.0.0.1:45123/"
    code_verifier = "oauth-callback-redaction-verifier-" + ("v" * 32)
    code_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        )
        .decode()
        .rstrip("=")
    )
    sentinel = "SUPERSECRET-IDP-ERROR-DESCRIPTION-IRIS"
    oauth_logger = logging.getLogger(
        "fastmcp.server.auth.oauth_proxy.proxy"
    )
    previous_level = oauth_logger.level
    oauth_logger.setLevel(logging.DEBUG)
    oauth_logger.addHandler(caplog.handler)
    try:
        async with _oauth_client(app) as client:
            invalid_state = await client.get(
                "/auth/callback",
                params={"code": "rejected-idp-code", "state": sentinel},
            )
            assert invalid_state.status_code == 400
            assert sentinel not in invalid_state.text
            assert invalid_state.headers["cache-control"] == "no-store"

            registration = await client.post(
                "/register",
                json=_vscode_registration(),
            )
            client_id = registration.json()["client_id"]
            authorization = await client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "response_type": "code",
                    "redirect_uri": callback_uri,
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                    "state": "callback-redaction-state",
                    "resource": "https://iris.example/mcp",
                },
            )
            txn_id = parse_qs(
                urlsplit(authorization.headers["location"]).query
            )["txn_id"][0]
            consent = await client.get(authorization.headers["location"])
            csrf_match = re.search(
                r'name="csrf_token" value="([^"]+)"',
                consent.text,
            )
            assert csrf_match is not None
            approval = await client.post(
                "/consent",
                data={
                    "txn_id": txn_id,
                    "action": "approve",
                    "csrf_token": csrf_match.group(1),
                },
            )
            assert approval.status_code == 302

            idp_error = await client.get(
                "/auth/callback",
                params={
                    "error": "provider_specific_failure",
                    "error_description": sentinel,
                    "state": txn_id,
                },
            )
            assert idp_error.status_code == 302
            safe_error_query = parse_qs(
                urlsplit(idp_error.headers["location"]).query
            )
            assert safe_error_query["error"] == ["server_error"]
            assert "error_description" not in safe_error_query
            assert sentinel not in idp_error.headers["location"]
            assert idp_error.headers["cache-control"] == "no-store"

            with respx.mock(assert_all_called=True) as github:
                github.post(
                    "https://github.com/login/oauth/access_token"
                ).mock(
                    return_value=HttpxResponse(
                        400,
                        json={
                            "error": "invalid_grant",
                            "error_description": sentinel,
                        },
                    )
                )
                exchange_failure = await client.get(
                    "/auth/callback",
                    params={"code": "rejected-idp-code", "state": txn_id},
                )
            assert exchange_failure.status_code == 500
            assert sentinel not in exchange_failure.text
            assert "restart the sign-in flow" in exchange_failure.text
            assert exchange_failure.headers["cache-control"] == "no-store"
    finally:
        oauth_logger.removeHandler(caplog.handler)
        oauth_logger.setLevel(previous_level)

    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    raw_records = repr([(record.msg, record.args) for record in caplog.records])
    assert sentinel not in rendered_logs
    assert sentinel not in raw_records
    assert "details redacted" in rendered_logs


@pytest.mark.asyncio
async def test_http_oauth_dcr_accepts_vscode_and_rejects_other_redirects(
    isolated_env,
    monkeypatch,
    tmp_path,
):
    _configure_oauth(monkeypatch, tmp_path)
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    callback_uri = "http://127.0.0.1:45123/"
    registration = _vscode_registration()
    code_verifier = "vscode-public-client-verifier-" + ("v" * 32)
    code_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        )
        .decode()
        .rstrip("=")
    )

    async with _oauth_client(app) as client:
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

        missing_resource = await client.get(
            "/authorize",
            params={
                "client_id": accepted_body["client_id"],
                "response_type": "code",
                "redirect_uri": callback_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": "missing-resource-state",
            },
        )
        assert missing_resource.status_code == 302
        missing_resource_params = parse_qs(
            urlsplit(missing_resource.headers["location"]).query
        )
        assert missing_resource_params["error"] == ["invalid_request"]
        assert missing_resource_params["state"] == ["missing-resource-state"]

        mismatched_resource = await client.get(
            "/authorize",
            params={
                "client_id": accepted_body["client_id"],
                "response_type": "code",
                "redirect_uri": callback_uri,
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
                "redirect_uri": callback_uri,
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
        assert callback_uri in consent.text
        txn_id = parse_qs(urlsplit(authorization.headers["location"]).query)[
            "txn_id"
        ][0]
        csrf_match = re.search(
            r'name="csrf_token" value="([^"]+)"',
            consent.text,
        )
        assert csrf_match is not None
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://iris.example",
        ) as different_browser:
            forged_consent = await different_browser.post(
                "/consent",
                data={
                    "txn_id": txn_id,
                    "action": "approve",
                    "csrf_token": csrf_match.group(1),
                },
            )
        assert forged_consent.status_code == 403
        consent_approval = await client.post(
            "/consent",
            data={
                "txn_id": txn_id,
                "action": "approve",
                "csrf_token": csrf_match.group(1),
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

        async with AsyncClient(
            transport=ASGITransport(
                app=app,
                client=("198.51.100.24", 45123),
            ),
            base_url="https://iris.example",
        ) as second_source:
            disguised_external_redirect = await second_source.post(
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

        with respx.mock(assert_all_called=False) as github:
            upstream_token = github.post(
                "https://github.com/login/oauth/access_token"
            ).mock(
                side_effect=[
                    HttpxResponse(
                        200,
                        json={
                            "access_token": "upstream-github-token",
                            "token_type": "bearer",
                        },
                    ),
                    HttpxResponse(
                        200,
                        json={
                            "access_token": "denied-github-token",
                            "token_type": "bearer",
                        },
                    ),
                ]
            )
            github_user = github.get("https://api.github.com/user").mock(
                side_effect=[
                    HttpxResponse(
                        200,
                        json={
                            "id": 12345,
                            "login": "allowed-user",
                            "name": "Allowed User",
                        },
                    ),
                    HttpxResponse(
                        200,
                        json={
                            "id": 99999,
                            "login": "denied-user",
                            "name": "Denied User",
                        },
                    ),
                ]
            )
            github_scopes = github.get(
                "https://api.github.com/user/repos"
            ).mock(
                side_effect=[
                    HttpxResponse(
                        200,
                        json=[],
                        headers={"x-oauth-scopes": ""},
                    ),
                    HttpxResponse(
                        200,
                        json=[],
                        headers={"x-oauth-scopes": ""},
                    ),
                ]
            )

            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="https://iris.example",
            ) as different_browser:
                confused_deputy = await different_browser.get(
                    "/auth/callback",
                    params={"code": "attacker-code", "state": txn_id},
                )
            assert confused_deputy.status_code == 403
            assert upstream_token.call_count == 0

            callback = await client.get(
                "/auth/callback",
                params={"code": "github-code", "state": txn_id},
            )
            assert callback.status_code == 302
            callback_url = urlsplit(callback.headers["location"])
            assert (
                callback_url.scheme,
                callback_url.hostname,
                callback_url.port,
                callback_url.path,
            ) == ("http", "127.0.0.1", 45123, "/")
            callback_params = parse_qs(callback_url.query)
            assert callback_params["state"] == ["vscode-state"]
            client_code = callback_params["code"][0]

            assert upstream_token.call_count == 1
            upstream_token_form = parse_qs(
                upstream_token.calls[0].request.content.decode()
            )
            assert upstream_token_form["code"] == ["github-code"]
            assert upstream_token_form["redirect_uri"] == [
                "https://iris.example/auth/callback"
            ]
            assert upstream_token_form["code_verifier"]
            assert "resource" not in upstream_token_form

            token_form = {
                "grant_type": "authorization_code",
                "client_id": accepted_body["client_id"],
                "code": client_code,
                "redirect_uri": callback_uri,
                "code_verifier": code_verifier,
            }
            missing_token_resource = await client.post(
                "/token",
                data=token_form,
            )
            assert missing_token_resource.status_code == 400
            assert missing_token_resource.json()["error"] == "invalid_target"
            assert missing_token_resource.headers["cache-control"] == "no-store"
            assert missing_token_resource.headers["pragma"] == "no-cache"

            wrong_token_resource = await client.post(
                "/token",
                data={
                    **token_form,
                    "resource": "https://attacker.example/mcp",
                },
            )
            assert wrong_token_resource.status_code == 400
            assert wrong_token_resource.json()["error"] == "invalid_target"

            query_token_resource = await client.post(
                "/token",
                data={
                    **token_form,
                    "resource": "https://iris.example/mcp?tenant=attacker",
                },
            )
            assert query_token_resource.status_code == 400
            assert query_token_resource.json()["error"] == "invalid_target"

            duplicate_resource_body = urlencode(
                [
                    *token_form.items(),
                    ("resource", "https://iris.example/mcp"),
                    ("resource", "https://iris.example/mcp"),
                ]
            )
            duplicate_token_resource = await client.post(
                "/token",
                content=duplicate_resource_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert duplicate_token_resource.status_code == 400
            assert duplicate_token_resource.json()["error"] == "invalid_request"

            duplicate_client_body = urlencode(
                [
                    *token_form.items(),
                    ("client_id", accepted_body["client_id"]),
                    ("resource", "https://iris.example/mcp"),
                ]
            )
            duplicate_client = await client.post(
                "/token",
                content=duplicate_client_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert duplicate_client.status_code == 400
            assert duplicate_client.json()["error"] == "invalid_request"
            assert duplicate_client.headers["cache-control"] == "no-store"

            unknown_client = await client.post(
                "/token",
                data={
                    **token_form,
                    "client_id": "unknown-public-client",
                    "resource": "https://iris.example/mcp",
                },
            )
            assert unknown_client.status_code == 401
            assert unknown_client.json()["error"] == "invalid_client"
            assert unknown_client.headers["cache-control"] == "no-store"

            wrong_pkce = await client.post(
                "/token",
                data={
                    **token_form,
                    "code_verifier": "wrong-verifier-" + ("x" * 48),
                    "resource": "https://iris.example/mcp",
                },
            )
            assert wrong_pkce.status_code == 401
            assert wrong_pkce.json()["error"] == "invalid_grant"

            token_response = await client.post(
                "/token",
                data={
                    **token_form,
                    "resource": "https://iris.example/mcp",
                },
            )
            assert token_response.status_code == 200
            assert token_response.headers["cache-control"] == "no-store"
            assert token_response.headers["pragma"] == "no-cache"
            token_body = token_response.json()
            assert token_body["access_token"]
            assert token_body["access_token"] != "upstream-github-token"
            assert token_body["expires_in"] == 30 * 24 * 60 * 60
            assert token_body.get("refresh_token") is None

            replay = await client.post(
                "/token",
                data={
                    **token_form,
                    "resource": "https://iris.example/mcp",
                },
            )
            assert replay.status_code == 401
            assert replay.json()["error"] == "invalid_grant"

            authorization_header = {
                "Authorization": f"Bearer {token_body['access_token']}"
            }
            initialize = await client.post(
                "/mcp/",
                headers=authorization_header,
                json={
                    "jsonrpc": "2.0",
                    "id": "initialize-1",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "oauth-e2e-test",
                            "version": "1.0.0",
                        },
                    },
                },
            )
            assert initialize.status_code == 200, initialize.text
            session_id = initialize.headers.get("mcp-session-id")
            assert session_id
            session_headers = {
                **authorization_header,
                "mcp-session-id": session_id,
            }
            initialized = await client.post(
                "/mcp/",
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
            )
            assert initialized.status_code in (200, 202)
            oauth_request = await client.post(
                "/mcp/",
                headers=session_headers,
                json=_rpc(
                    "tools/call",
                    {"name": "health_check", "arguments": {}},
                ),
            )
            assert oauth_request.status_code == 200
            assert (
                oauth_request.json()["result"]["structuredContent"]["status"]
                == "ok"
            )

            denied_authorization = await client.get(
                "/authorize",
                params={
                    "client_id": accepted_body["client_id"],
                    "response_type": "code",
                    "redirect_uri": callback_uri,
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                    "state": "denied-user-state",
                    "resource": "https://iris.example/mcp",
                },
            )
            assert denied_authorization.status_code == 302
            denied_consent = await client.get(
                denied_authorization.headers["location"]
            )
            assert denied_consent.status_code == 200
            denied_txn_id = parse_qs(
                urlsplit(denied_authorization.headers["location"]).query
            )["txn_id"][0]
            denied_csrf_match = re.search(
                r'name="csrf_token" value="([^"]+)"',
                denied_consent.text,
            )
            assert denied_csrf_match is not None
            denied_approval = await client.post(
                "/consent",
                data={
                    "txn_id": denied_txn_id,
                    "action": "approve",
                    "csrf_token": denied_csrf_match.group(1),
                },
            )
            assert denied_approval.status_code == 302
            denied_callback = await client.get(
                "/auth/callback",
                params={
                    "code": "denied-github-code",
                    "state": denied_txn_id,
                },
            )
            assert denied_callback.status_code == 302
            denied_client_code = parse_qs(
                urlsplit(denied_callback.headers["location"]).query
            )["code"][0]
            denied_token_form = {
                **token_form,
                "code": denied_client_code,
                "resource": "https://iris.example/mcp",
            }
            denied_token = await client.post("/token", data=denied_token_form)
            assert denied_token.status_code == 401
            assert denied_token.json()["error"] == "invalid_grant"
            denied_replay = await client.post(
                "/token",
                data=denied_token_form,
            )
            assert denied_replay.status_code == 401
            assert denied_replay.json()["error"] == "invalid_grant"

            assert upstream_token.call_count == 2
            assert github_user.call_count == 2
            assert github_scopes.call_count == 2


@pytest.mark.asyncio
async def test_oauth_dcr_is_bounded_rate_limited_and_storage_isolated(
    isolated_env,
    monkeypatch,
    tmp_path,
):
    _configure_oauth(monkeypatch, tmp_path)
    monkeypatch.setenv("HTTP_OAUTH_DCR_RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("HTTP_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("HTTP_RBAC_ENABLED", "false")
    monkeypatch.setenv("HTTP_JWT_ENABLED", "false")
    _config.clear_settings_cache()
    settings = _config.get_settings()
    assert settings.http.rate_limit_enabled is False
    app = build_http_app(settings, build_mcp_server())
    provider = app.state.oauth_provider

    async def oversized_registration() -> AsyncIterator[bytes]:
        yield b'{"client_name":"' + (b"x" * (8 * 1024))
        yield b'"}'

    async def oversized_token_request() -> AsyncIterator[bytes]:
        yield b"grant_type=authorization_code&code="
        yield b"x" * (8 * 1024)

    async def oversized_authorization_request() -> AsyncIterator[bytes]:
        yield b"client_id=public-client&response_type=code&state="
        yield b"x" * (8 * 1024)

    async with _oauth_client(app) as client:
        oversized = await client.post(
            "/register",
            content=oversized_registration(),
            headers={"Content-Type": "application/json"},
        )
        assert oversized.status_code == 413
        assert oversized.headers["cache-control"] == "no-store"

        oversized_token = await client.post(
            "/token",
            content=oversized_token_request(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert oversized_token.status_code == 413
        assert oversized_token.headers["cache-control"] == "no-store"

        oversized_authorization = await client.post(
            "/authorize",
            content=oversized_authorization_request(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                # A hostile client can lie about Content-Length while still
                # streaming bytes. The receive-side cap remains authoritative.
                "Content-Length": "0",
            },
        )
        assert oversized_authorization.status_code == 413
        assert oversized_authorization.headers["cache-control"] == "no-store"

        malformed_token = await client.post(
            "/token",
            content=b"not-multipart",
            headers={"Content-Type": "multipart/form-data; boundary=x"},
        )
        assert malformed_token.status_code == 400
        assert malformed_token.json()["error"] == "invalid_request"
        assert malformed_token.headers["cache-control"] == "no-store"

        accepted = await client.post("/register", json=_vscode_registration())
        assert accepted.status_code == 201

        limited = await client.post("/register", json=_vscode_registration())
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Rate limit exceeded"}

        discovery = await client.get(
            "/.well-known/oauth-authorization-server"
        )
        assert discovery.status_code == 200

        protected_store, dcr_store = provider._oauth_disk_stores
        await provider._client_storage.put(
            key="protected-sentinel",
            value={"kind": "protected"},
            collection="mcp-jti-mappings",
        )
        await provider._client_storage.put(
            key="dcr-sentinel",
            value={"kind": "registration"},
            collection="mcp-oauth-proxy-clients",
        )
        assert (
            await protected_store.get(
                key="protected-sentinel",
                collection="mcp-jti-mappings",
            )
            is not None
        )
        assert (
            await dcr_store.get(
                key="protected-sentinel",
                collection="mcp-jti-mappings",
            )
            is None
        )
        assert (
            await dcr_store.get(
                key="dcr-sentinel",
                collection="mcp-oauth-proxy-clients",
            )
            is not None
        )
        assert (
            await protected_store.get(
                key="dcr-sentinel",
                collection="mcp-oauth-proxy-clients",
            )
            is None
        )


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

    async with _oauth_client(app) as client:
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

    async with _oauth_client(app) as client:
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
