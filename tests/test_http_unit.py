from __future__ import annotations

import asyncio
import itertools
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from starlette.requests import Request

import mcp_agent_mail.http as http_module
from mcp_agent_mail import config as _config
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import SecurityAndRateLimitMiddleware, _decode_jwt_header_segment, build_http_app
from mcp_agent_mail.models import Agent, Message, Project


def test_legacy_fts_tokenizer_matches_bounded_reference_grammar() -> None:
    # Every generated input is shorter than the fixed repetition bound. This
    # independently preserves the previous alternative order without adding
    # an unbounded backtracking expression to the regression test itself.
    reference = re.compile(r'\w{1,64}:"[^"]{1,64}"|"[^"]{1,64}"|\S{1,64}')
    fragments = ('a', '_', ':', '"', ' ', '\t', 'λ', '²', '\u0301', '\u2003')
    for size in range(5):
        for combination in itertools.product(fragments, repeat=size):
            raw = "".join(combination)
            assert http_module._MailUiRoutes.Rendering._fts_query_parts(raw) == reference.findall(raw), repr(raw)


@pytest.mark.parametrize(
    ("raw", "scope", "fts", "pattern", "tokens"),
    [
        (" \t ", "body", "", "", []),
        ('subject:"two words" body:delta', None,
         'subject:"two words" AND body:"delta"', '%two words%delta%',
         [{"field": "subject", "value": "two words"}, {"field": "body", "value": "delta"}]),
        ('"a""b"', "body", 'body:"a" AND body:"b"', '%a%b%',
         [{"field": "body", "value": "a"}, {"field": "body", "value": "b"}]),
        ('body:"unterminated phrase', "subject", 'body:"""unterminated" AND subject:"phrase"',
         '%"unterminated%phrase%',
         [{"field": "body", "value": '"unterminated'}, {"field": "subject", "value": "phrase"}]),
        ('unknown:"two words"', "body", 'body:"unknown:""two words"""', '%unknown:"two words"%',
         [{"field": "body", "value": 'unknown:"two words"'}]),
        ('subject:"" 100%_!', "invalid", 'subject:"" AND (subject:"100%_!" OR body:"100%_!")',
         '%%100!%!_!!%', [{"field": "subject", "value": ""}, {"field": "both", "value": "100%_!"}]),
    ],
)
def test_legacy_fts_query_preserves_scope_quotes_and_like_escaping(
    raw: str, scope: str | None, fts: str, pattern: str, tokens: list[dict[str, str]],
) -> None:
    actual_fts, actual_pattern, actual_scope, actual_tokens = http_module._MailUiRoutes.Rendering._parse_fts_query(raw, scope)
    assert actual_fts == fts
    assert actual_pattern == pattern
    assert actual_tokens == tokens
    assert actual_scope == (scope if raw.strip() and scope in {"subject", "body"} else "both")


def test_legacy_fts_tokenizer_handles_long_unquoted_and_unclosed_terms() -> None:
    word = "a" * 100_000
    fragments = [word, 'body:"' + word, '"' + word, 'unknown:"two words"', '""']
    raw = " ".join(fragments)
    # The opening quote after body closes at the following term's quote;
    # preserving that legacy grammar matters even for malformed input.
    parts = http_module._MailUiRoutes.Rendering._fts_query_parts(raw)
    assert parts == [word, 'body:"' + word + ' "', word, 'unknown:"two words"', '""']
    assert http_module._MailUiRoutes.Rendering._fts_query_parts(word + ':"' + word) == [word + ':"' + word]


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("scope", ["subject", "body", "both"])
async def test_legacy_project_search_keeps_scope_project_and_safe_snippets(
    isolated_env: object, fallback: bool, scope: str,
) -> None:
    await ensure_schema()
    routes = http_module._MailUiRoutes(FastAPI(), _config.get_settings())
    async with get_session() as session:
        project = Project(slug="search-unit", human_key="/test/search-unit")
        other = Project(slug="search-other", human_key="/test/search-other")
        session.add_all([project, other])
        await session.flush()
        assert project.id is not None
        assert other.id is not None
        sender = Agent(project_id=project.id, name="codex-linux-search-1", program="test", model="test")
        session.add(sender)
        await session.flush()
        assert sender.id is not None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        messages = [
            Message(project_id=project.id, sender_id=sender.id, subject="needle subject", body_md="unrelated", created_ts=now),
            Message(project_id=project.id, sender_id=sender.id, subject="body match", body_md="<script>needle</script>", created_ts=now + timedelta(seconds=1)),
            Message(project_id=other.id, sender_id=sender.id, subject="needle foreign", body_md="needle", created_ts=now),
        ]
        session.add_all(messages)
        await session.commit()
        if fallback:
            # Exercise the real SQL fallback when the optional FTS table is
            # unavailable, without mocking the search implementation.
            await session.execute(text("ALTER TABLE fts_messages RENAME TO unavailable_fts"))
        matches, tokens = await routes.legacy._project_matches(session, project.id, "needle", scope, "time", 1)
        expected = {"subject": ["needle subject"], "body": ["body match"], "both": ["body match", "needle subject"]}
        assert [match["subject"] for match in matches] == expected[scope]
        assert tokens == [{"field": scope, "value": "needle"}]
        for match in matches:
            assert "<script>" not in str(match["snippet"])
            if fallback:
                assert match["snippet"] == ""
                assert match["hits"] == 0
        if not fallback and scope != "subject":
            assert "<mark>needle</mark>" in str(matches[0]["snippet"])
            assert "&lt;script&gt;" in str(matches[0]["snippet"])
            assert matches[0]["hits"] == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('[{"name":"safe"},4,null]', [{"name": "safe"}]),
        ([{"name": "safe"}, "invalid"], [{"name": "safe"}]),
        ('{"not":"a list"}', []),
        ("invalid-json", []),
        (None, []),
    ],
)
def test_legacy_attachment_metadata_ignores_malformed_entries(raw: object, expected: list[dict[str, str]]) -> None:
    row = cast(RowMapping, {"attachments": raw})
    assert http_module._MailUiRoutes.Legacy._message_attachments(row) == expected


def test_legacy_attachment_metadata_tolerates_missing_column() -> None:
    assert http_module._MailUiRoutes.Legacy._message_attachments(cast(RowMapping, {})) == []


@pytest.mark.parametrize(
    ("current", "limit", "level", "event", "cleanup_threshold"),
    [
        (-1, 100, None, None, None),
        (1, 0, None, None, None),
        (50, 100, None, None, None),
        (75, 100, "warning", "fd_health.warning", None),
        (85, 100, "warning", "fd_health.low", 25),
        (95, 100, "error", "fd_health.critical", 100),
    ],
)
def test_fd_health_reports_pressure_and_uses_appropriate_cleanup(
    monkeypatch: pytest.MonkeyPatch, current: int, limit: int,
    level: str | None, event: str | None, cleanup_threshold: int | None,
) -> None:
    logger = Mock()
    cleanup = Mock(return_value=3)
    monkeypatch.setattr(http_module, "get_fd_usage", lambda: (current, limit))
    monkeypatch.setattr(http_module, "get_repo_cache_stats", lambda: {})
    monkeypatch.setattr(http_module, "get_lock_telemetry", lambda: {})
    monkeypatch.setattr(http_module, "get_fd_headroom", lambda: 40)
    monkeypatch.setattr(http_module, "proactive_fd_cleanup", cleanup)
    http_module._HttpLifecycle.Workers._check_fd_health(logger)
    if level is None:
        assert logger.mock_calls == []
    else:
        call = getattr(logger, level).call_args_list[0]
        assert call.args == (event,)
        assert call.kwargs["current_fds"] == current
    if cleanup_threshold is None:
        cleanup.assert_not_called()
    else:
        cleanup.assert_called_once_with(threshold=cleanup_threshold)
        recovery_event = "fd_health.emergency_cleanup" if level == "error" else "fd_health.proactive_cleanup"
        assert any(call.args == (recovery_event,) and call.kwargs["freed"] == 3 for call in logger.mock_calls)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [(timedelta(seconds=-1), "Just now"), (timedelta(seconds=30), "Just now"),
     (timedelta(minutes=2), "2m ago"), (timedelta(hours=2), "2h ago"),
     (timedelta(days=2), "2d ago"), (timedelta(days=62), "2mo ago"),
     (timedelta(days=732), "2y ago")],
)
def test_unified_inbox_relative_time_labels(delta: timedelta, expected: str) -> None:
    assert http_module._MailUiRoutes.Operations._relative_message_time(delta) == expected


@pytest.mark.parametrize("created", ["2025-01-02T12:00:00Z", datetime(2025, 1, 2, 12), datetime(2025, 1, 2, 14, tzinfo=timezone(timedelta(hours=2)))])
@pytest.mark.parametrize(("role", "can_reply"), [("viewer", False), ("operator", True)])
def test_unified_inbox_payload_normalizes_time_excerpt_and_project_permissions(
    isolated_env: object, created: str | datetime, role: str, can_reply: bool,
) -> None:
    settings = _config.get_settings()
    settings = replace(settings, mail_ui=replace(settings.mail_ui, enabled=True))
    routes = http_module._MailUiRoutes(FastAPI(), settings)
    request = Request({"type": "http", "headers": []})
    row: dict[str, Any] = {
        "id": 12, "subject": "", "body_md": "#*`" + "x" * 160,
        "body_length": None, "created_ts": created, "importance": "",
        "thread_id": "thread", "message_project_id": 1,
        "sender_name": "codex-linux-other-1", "sender_project_id": 2,
        "sender_project_name": "/other", "sender_project_slug": "other",
        "project_slug": "local", "project_name": "/local", "recipients": " first, , second ",
    }
    payload = routes.operations._unified_message_payload(cast(RowMapping, row), request, {1: role})
    assert payload["subject"] == "(No subject)"
    assert payload["importance"] == "normal"
    assert payload["body_length"] == 163
    assert payload["excerpt"] == "x" * 147 + "..."
    assert payload["created_full"] == "January 02, 2025 at 12:00 PM"
    assert payload["recipients"] == "first, second"
    assert payload["sender_address"] == "project:other#codex-linux-other-1"
    assert payload["can_reply"] is can_reply
    assert payload["read"] is False


def test_quota_warnings_include_only_projects_at_or_over_each_limit(
    isolated_env: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(_config.get_settings(), quota_attachments_limit_bytes=100, quota_inbox_limit_count=3)
    logger = Mock()
    monkeypatch.setattr(http_module.structlog, "get_logger", lambda _name: logger)
    workers = http_module._HttpLifecycle.Workers(settings)
    workers._report_quota_limits({
        "per_project_attach": {"below": 99, "equal": 100, "above": 101},
        "per_project_inbox_counts": {"below": 2, "equal": 3, "above": 4},
    })
    warnings = [(call.args[0], call.kwargs["project"]) for call in logger.warning.call_args_list]
    assert warnings == [
        ("quota_attachments_exceeded", "equal"), ("quota_attachments_exceeded", "above"),
        ("quota_inbox_exceeded", "equal"), ("quota_inbox_exceeded", "above"),
    ]


@pytest.mark.parametrize("target", [
    "https://outside.invalid/mail", "//outside.invalid/mail", "/mail/\\outside",
    "/mail/\x00", "/mail/project#fragment", "/mail/%ZZ", "/mail/retired/route",
    "/mail/project?a=1&b=2&c=3&d=4&e=5",
])
def test_login_next_refuses_noncanonical_and_external_destinations(target: str) -> None:
    assert http_module._MailUiRoutes.Sessions._safe_next(target) == "/mail"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"not-json", b"\xff"])
async def test_mcp_response_capture_preserves_status_headers_on_invalid_json(body: bytes) -> None:
    capture = http_module._MCPResponseCapture()
    await capture.send({"type": "http.response.start", "status": 503, "headers": [(b"x-result", b"unavailable")]})
    await capture.send({"type": "http.response.body", "body": body})
    assert capture.status_code == 503
    assert capture.headers == {"x-result": "unavailable"}
    assert capture.body == {}


@pytest.mark.asyncio
async def test_mcp_response_capture_accepts_json_response() -> None:
    capture = http_module._MCPResponseCapture()
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    await capture.send({"type": "http.response.body", "body": json.dumps(payload).encode()})
    assert capture.body == payload


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
