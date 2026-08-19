import asyncio
import contextlib
import json
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from git import Repo
from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from sqlalchemy import text

from mcp_agent_mail.app import (
    _public_runtime_descriptor,
    _redact_tool_log_value,
    build_mcp_server,
    expire_stale_agent_executions,
    get_project_sibling_data,
    refresh_project_sibling_suggestions,
    sweep_stale_agents,
    update_project_sibling_status,
)
from mcp_agent_mail.config import ConfigError, clear_settings_cache, get_settings
from mcp_agent_mail.db import get_session
from mcp_agent_mail.models import FileReservation
from tests.keys import pkey

_ROOT_EXECUTION_TOKEN = "1" * 64
_CHILD_EXECUTION_TOKEN = "2" * 64
_SIBLING_EXECUTION_TOKEN = "3" * 64
_EXECUTION_PROTOCOL_VERSION = 1
_BUILD_AGENT_NAME = "codex-wsl-buildhost-1"
_BUILD_PEER_NAME = "claude-linux-buildhost-1"


def test_public_runtime_descriptor_never_exposes_database_credentials(isolated_env):
    settings = get_settings()
    sentinel = "postgresql+asyncpg://iris:NEVER-LEAK-DB-PASSWORD@db.internal/iris"
    settings = replace(settings, database=replace(settings.database, url=sentinel))

    payload = _public_runtime_descriptor(settings)

    rendered = json.dumps(payload, sort_keys=True)
    assert "database" not in rendered.casefold()
    assert "NEVER-LEAK-DB-PASSWORD" not in rendered
    assert sentinel not in rendered


@pytest.mark.asyncio
async def test_public_health_and_environment_resource_omit_database_url(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        health = await client.call_tool("health_check", {})
        environment_blocks = await client.read_resource("resource://config/environment")

    environment_payload = json.loads(environment_blocks[0].text)
    assert "database_url" not in health.data
    assert "database_url" not in environment_payload
    assert "database" not in json.dumps(health.data).casefold()
    assert "database" not in json.dumps(environment_payload).casefold()


def test_tool_log_redaction_is_recursive_and_does_not_mutate_response():
    response = {
        "registration_token": "durable-secret",
        "nested": [
            {"executionToken": "execution-secret", "safe": "visible"},
            {"capability_hash": "capability-secret"},
        ],
        "secret_note": "private",
    }

    redacted = _redact_tool_log_value(response)

    assert redacted == {
        "registration_token": "***",
        "nested": [
            {"executionToken": "***", "safe": "visible"},
            {"capability_hash": "***"},
        ],
        "secret_note": "***",
    }
    assert response["registration_token"] == "durable-secret"
    assert response["nested"][0]["executionToken"] == "execution-secret"


async def _register_durable_test_agent(
    client: Client,
    project_key: str,
    name: str = "codex-wsl-testhost-1",
) -> dict[str, Any]:
    result = await client.call_tool(
        "register_agent",
        {
            "project_key": project_key,
            "program": "codex",
            "model": "gpt-5",
            "name": name,
        },
    )
    return result.data


async def _register_build_slot_test_agent(
    client: Client,
    *,
    name: str = _BUILD_AGENT_NAME,
    execution_token: str = _ROOT_EXECUTION_TOKEN,
) -> dict[str, Any]:
    agent = await _register_durable_test_agent(
        client,
        "Backend",
        name=name,
    )
    execution = await client.call_tool(
        "start_agent_execution",
        {
            "project_key": "Backend",
            "agent_name": name,
            "external_id": f"build-slot-session-{name}",
            "client_name": "pytest",
            "execution_token": execution_token,
            "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
        },
    )
    agent["execution_id"] = execution.data["id"]
    agent["execution_token"] = execution_token
    return agent


@pytest.mark.asyncio
async def test_agent_execution_lifecycle_scopes_sibling_claims(isolated_env):
    server = build_mcp_server()
    project_key = pkey("execution-lifecycle")
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_durable_test_agent(client, project_key)
        root = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "native-session-1",
                "client_name": "codex",
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        root_id = root.data["id"]
        resumed_root = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "native-session-1",
                "client_name": "codex",
                "execution_token": _ROOT_EXECUTION_TOKEN,
            },
        )
        assert resumed_root.data["reused"] is True
        assert resumed_root.data["lifecycle_protocol_version"] == 1
        assert resumed_root.data["warnings"]

        other_project_key = pkey("execution-lifecycle-other")
        await client.call_tool("ensure_project", {"human_key": other_project_key})
        other_agent = await _register_durable_test_agent(client, other_project_key)
        other_root = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": other_project_key,
                "agent_name": other_agent["name"],
                "external_id": "native-session-1",
                "client_name": "codex",
                "execution_token": "4" * 64,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        other_claim = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": other_project_key,
                "agent_name": other_agent["name"],
                "paths": ["src/other-project.py"],
            },
        )
        assert other_claim.data["granted"][0]["execution_id"] == other_root.data["id"]
        child = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "native-child-1",
                "client_name": "codex",
                "execution_token": _CHILD_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                "kind": "subagent",
                "parent_execution_id": root_id,
                "turn_id": "turn-1",
                "agent_type": "explorer",
            },
        )
        child_id = child.data["id"]
        assert child.data["ancestor_execution_ids"] == [root_id]

        heartbeat = await client.call_tool(
            "heartbeat_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "execution_id": child_id,
                "execution_token": _CHILD_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert heartbeat.data["lifecycle_protocol_version"] == 1
        assert "execution_token" not in heartbeat.data
        assert "execution_token_hash" not in heartbeat.data
        legacy_heartbeat = await client.call_tool(
            "heartbeat_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "execution_id": child_id,
                "execution_token": _CHILD_EXECUTION_TOKEN,
            },
        )
        assert legacy_heartbeat.data["lifecycle_protocol_version"] == 1
        assert legacy_heartbeat.data["warnings"]

        # Starting a child in the same MCP session must not replace the root's
        # implicit execution binding.
        root_claim = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "paths": ["src/shared.py"],
            },
        )
        assert root_claim.data["granted"][0]["execution_id"] == root_id

        child_claim = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "execution_id": child_id,
                "execution_token": _CHILD_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                "paths": ["src/shared.py"],
            },
        )
        assert child_claim.data["granted"][0]["execution_id"] == child_id
        assert child_claim.data["conflicts"] == []
        assert child_claim.data["ancestor_execution_ids"] == [root_id]

        sibling = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "native-child-2",
                "client_name": "codex",
                "execution_token": _SIBLING_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                "kind": "subagent",
                "parent_execution_id": root_id,
            },
        )
        sibling_claim = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "execution_id": sibling.data["id"],
                "execution_token": _SIBLING_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                "paths": ["src/shared.py"],
            },
        )
        sibling_conflicts = sibling_claim.data["conflicts"][0]["holders"]
        assert {holder["execution_id"] for holder in sibling_conflicts} == {child_id}

        ended = await client.call_tool(
            "end_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "execution_id": root_id,
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "status": "completed",
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert ended.data["execution"]["status"] == "completed"
        assert ended.data["descendants_ended"] == 2
        assert set(ended.data["descendant_execution_ids"]) == {
            child_id,
            sibling.data["id"],
        }
        assert ended.data["released_reservations"] == 3

        repeated = await client.call_tool(
            "end_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "execution_id": root_id,
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "status": "completed",
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert repeated.data["already_ended"] is True
        assert repeated.data["released_reservations"] == 0


@pytest.mark.asyncio
async def test_explicit_execution_claim_survives_end_and_owner_can_release(
    isolated_env,
):
    server = build_mcp_server()
    project_key = pkey("execution-explicit-claim")
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_durable_test_agent(client, project_key)
        execution = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "explicit-session",
                "client_name": "codex",
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        claims = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "paths": ["src/manual.py", "src/recover.py"],
                "origin": "explicit",
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        manual_id, recovery_id = [item["id"] for item in claims.data["granted"]]
        no_downgrade = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "paths": ["src/manual.py"],
                "origin": "auto",
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert no_downgrade.data["granted"][0]["origin"] == "explicit"

        automatic = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "paths": ["src/upgrade.py"],
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert automatic.data["granted"][0]["origin"] == "auto"
        upgraded = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "paths": ["src/upgrade.py"],
                "origin": "explicit",
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert upgraded.data["granted"][0]["origin"] == "explicit"

        child = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "explicit-child",
                "client_name": "codex",
                "execution_token": _CHILD_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                "kind": "subagent",
                "parent_execution_id": execution.data["id"],
            },
        )
        child_claim = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "execution_id": child.data["id"],
                "execution_token": _CHILD_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                "paths": ["src/child-recover.py"],
                "origin": "explicit",
            },
        )
        child_recovery_id = child_claim.data["granted"][0]["id"]
        exact_root_release = await client.call_tool(
            "release_file_reservations",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "execution_id": execution.data["id"],
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                "file_reservation_ids": [child_recovery_id],
            },
        )
        assert exact_root_release.data["released"] == 0

        ended = await client.call_tool(
            "end_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "execution_id": execution.data["id"],
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "status": "completed",
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert ended.data["released_reservations"] == 0

        released = await client.call_tool(
            "release_file_reservations",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "execution_id": execution.data["id"],
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                "file_reservation_ids": [manual_id],
            },
        )
        assert released.data["released"] == 1

        recovered = await client.call_tool(
            "force_release_file_reservation",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "file_reservation_id": recovery_id,
                "notify_previous": False,
            },
        )
        assert recovered.data["released"] == 1
        assert recovered.data["reservation"]["orphaned"] is True

        recovered_child = await client.call_tool(
            "force_release_file_reservation",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "file_reservation_id": child_recovery_id,
                "notify_previous": False,
            },
        )
        assert recovered_child.data["released"] == 1
        assert recovered_child.data["reservation"]["execution_status"] == "cancelled"


@pytest.mark.asyncio
async def test_agent_execution_start_is_race_idempotent(isolated_env):
    server = build_mcp_server()
    project_key = pkey("execution-race")
    async with Client(server) as setup_client:
        await setup_client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_durable_test_agent(setup_client, project_key)

    async def start_once() -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool(
                "start_agent_execution",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "registration_token": agent["registration_token"],
                    "external_id": "native-race-session",
                    "client_name": "codex",
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                    "task_description": "same lifetime",
                },
            )
            return result.data

    first, second = await asyncio.gather(start_once(), start_once())
    assert first["id"] == second["id"]
    assert {first["reused"], second["reused"]} == {False, True}
    assert "execution_token" not in first
    assert "execution_token_hash" not in first

    async with Client(server) as client:
        with pytest.raises(ToolError, match="already assigned"):
            await client.call_tool(
                "start_agent_execution",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "registration_token": agent["registration_token"],
                    "external_id": "different-native-lifetime",
                    "client_name": "codex",
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )

        with pytest.raises(ToolError, match="original execution_token"):
            await client.call_tool(
                "start_agent_execution",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "registration_token": agent["registration_token"],
                    "external_id": "native-race-session",
                    "client_name": "codex",
                    "execution_token": _CHILD_EXECUTION_TOKEN,
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )

    execution_id = first["id"]

    async def heartbeat_once() -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool(
                "heartbeat_agent_execution",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "registration_token": agent["registration_token"],
                    "execution_id": execution_id,
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )
            return result.data

    async def end_once() -> dict[str, Any]:
        async with Client(server) as client:
            result = await client.call_tool(
                "end_agent_execution",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "registration_token": agent["registration_token"],
                    "execution_id": execution_id,
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                    "status": "completed",
                },
            )
            return result.data

    heartbeat_result, first_end_result, second_end_result = await asyncio.gather(
        heartbeat_once(),
        end_once(),
        end_once(),
        return_exceptions=True,
    )
    assert isinstance(first_end_result, dict)
    assert isinstance(second_end_result, dict)
    assert first_end_result["execution"]["status"] == "completed"
    assert second_end_result["execution"]["status"] == "completed"
    assert {
        first_end_result["already_ended"],
        second_end_result["already_ended"],
    } == {False, True}
    assert isinstance(heartbeat_result, (dict, ToolError))
    if isinstance(heartbeat_result, dict):
        assert heartbeat_result["status"] == "active"
    else:
        assert "not active" in str(heartbeat_result)

    async with Client(server) as client:
        with pytest.raises(ToolError, match="cannot be reactivated"):
            await client.call_tool(
                "start_agent_execution",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "registration_token": agent["registration_token"],
                    "external_id": "native-race-session",
                    "client_name": "codex",
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )
    async with get_session() as session:
        status = (
            await session.execute(
                text("SELECT status FROM agent_executions WHERE id=:execution_id"),
                {"execution_id": execution_id},
            )
        ).scalar_one()
    assert status == "completed"


@pytest.mark.asyncio
async def test_active_execution_end_requires_capability_but_terminal_retry_allows_owner_recovery(
    isolated_env,
):
    server = build_mcp_server()
    project_key = pkey("execution-end-capability")
    async with Client(server) as owner_client:
        await owner_client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_durable_test_agent(owner_client, project_key)
        execution = await owner_client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "native-end-capability",
                "client_name": "codex",
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )

        with pytest.raises(ToolError, match="Invalid execution_token"):
            await owner_client.call_tool(
                "end_agent_execution",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "execution_id": execution.data["id"],
                    "status": "completed",
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )

        async with Client(server) as recovery_client:
            with pytest.raises(ToolError, match="Invalid execution_token"):
                await recovery_client.call_tool(
                    "end_agent_execution",
                    {
                        "project_key": project_key,
                        "agent_name": agent["name"],
                        "registration_token": agent["registration_token"],
                        "execution_id": execution.data["id"],
                        "status": "completed",
                        "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                    },
                )

            ended = await owner_client.call_tool(
                "end_agent_execution",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "execution_id": execution.data["id"],
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                    "status": "completed",
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )
            assert ended.data["already_ended"] is False

            recovered = await recovery_client.call_tool(
                "end_agent_execution",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "registration_token": agent["registration_token"],
                    "execution_id": execution.data["id"],
                    "status": "completed",
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )
            assert recovered.data["already_ended"] is True
            assert recovered.data["execution"]["status"] == "completed"


@pytest.mark.asyncio
async def test_retired_agent_cannot_start_execution(isolated_env):
    server = build_mcp_server()
    project_key = pkey("execution-retired-agent")
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_durable_test_agent(client, project_key)
        await client.call_tool(
            "retire_agent",
            {"project_key": project_key, "agent_name": agent["name"]},
        )

        with pytest.raises(ToolError, match="is retired"):
            await client.call_tool(
                "start_agent_execution",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "external_id": "retired-agent-session",
                    "client_name": "codex",
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )

    async with get_session() as session:
        count = (
            await session.execute(text("SELECT COUNT(*) FROM agent_executions"))
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_execution_required_and_name_required_are_fail_closed(
    isolated_env, monkeypatch
):
    monkeypatch.setenv("AGENT_EXECUTION_ENFORCEMENT_MODE", "enforce")
    clear_settings_cache()
    server = build_mcp_server()
    project_key = pkey("execution-required")
    async with Client(server) as client:
        project = await client.call_tool("ensure_project", {"human_key": project_key})
        with pytest.raises(ToolError, match="Missing required argument"):
            await client.call_tool(
                "register_agent",
                {"project_key": project_key, "program": "codex", "model": "gpt-5"},
            )
        with pytest.raises(ToolError, match="must match client-os-host-slot"):
            await client.call_tool(
                "register_agent",
                {
                    "project_key": project_key,
                    "program": "codex",
                    "model": "gpt-5",
                    "name": "BlueLake",
                },
            )
        async with get_session() as session:
            count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM agents WHERE project_id = :project_id"),
                    {"project_id": project.data["id"]},
                )
            ).scalar_one()
        assert count == 0

        agent = await _register_durable_test_agent(client, project_key)
        with pytest.raises(ToolError, match="requires execution_id"):
            await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "paths": ["src/no-execution.py"],
                },
            )


@pytest.mark.asyncio
async def test_existing_legacy_agent_can_authenticate_but_not_be_created(isolated_env):
    server = build_mcp_server()
    project_key = pkey("legacy-agent-auth")
    async with Client(server) as client:
        project = await client.call_tool("ensure_project", {"human_key": project_key})
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO agents "
                    "(project_id,name,program,model,task_description,inception_ts,last_active_ts,"
                    "attachments_policy,contact_policy,registration_token) "
                    "VALUES (:project_id,'BlueLake','legacy','legacy','',CURRENT_TIMESTAMP,"
                    "CURRENT_TIMESTAMP,'auto','auto','legacy-token')"
                ),
                {"project_id": project.data["id"]},
            )
            await session.commit()
        resumed = await client.call_tool(
            "register_agent",
            {
                "project_key": project_key,
                "program": "codex",
                "model": "gpt-5",
                "name": "BlueLake",
                "registration_token": "legacy-token",
            },
        )
        assert resumed.data["name"] == "BlueLake"
        assert resumed.data["program"] == "codex"


@pytest.mark.asyncio
async def test_stale_execution_waits_for_recent_descendant(isolated_env):
    server = build_mcp_server()
    project_key = pkey("execution-stale-tree")
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_durable_test_agent(client, project_key)
        root = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "stale-root",
                "client_name": "codex",
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        child = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "recent-child",
                "client_name": "codex",
                "execution_token": _CHILD_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                "kind": "subagent",
                "parent_execution_id": root.data["id"],
            },
        )
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=2)
        async with get_session() as session:
            await session.execute(
                text("UPDATE agents SET last_active_ts=:old WHERE id=:agent_id"),
                {
                    "old": old.replace(tzinfo=None).strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    ),
                    "agent_id": agent["id"],
                },
            )
            await session.execute(
                text(
                    "UPDATE agent_executions SET started_ts=:old, last_active_ts=:old "
                    "WHERE id=:execution_id"
                ),
                {
                    "old": old.replace(tzinfo=None).strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    ),
                    "execution_id": root.data["id"],
                },
            )
            await session.commit()

        # Durable Agent retirement remains independent from process liveness:
        # an active execution protects an otherwise old mailbox identity.
        assert await sweep_stale_agents(threshold_seconds=3600, now=now) == []

        kept = await expire_stale_agent_executions(3600, now=now)
        assert kept["expired"] == 0

        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE agent_executions SET started_ts=:old, last_active_ts=:old "
                    "WHERE id=:execution_id"
                ),
                {
                    "old": old.replace(tzinfo=None).strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    ),
                    "execution_id": child.data["id"],
                },
            )
            await session.commit()
        expired = await expire_stale_agent_executions(3600, now=now)
        assert expired["expired"] == 2


@pytest.mark.asyncio
async def test_execution_reaper_batches_large_history_and_lineage_is_local(
    isolated_env,
    monkeypatch,
):
    """Audit history cannot inflate one reaper/outbox batch or lineage load."""
    import mcp_agent_mail.app as app_module

    server = build_mcp_server()
    project_key = pkey("execution-bounded-history")
    async with Client(server) as client:
        project = await client.call_tool(
            "ensure_project",
            {"human_key": project_key},
        )
        agent = await _register_durable_test_agent(client, project_key)
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=2)
        started = old - timedelta(minutes=1)
        history_ids = [str(uuid.uuid4()) for _ in range(300)]
        rows = [
            {
                "id": execution_id,
                "project_id": project.data["id"],
                "agent_id": agent["id"],
                "external_id": f"bounded-history-{index}",
                "token_hash": f"{index + 1000:064x}",
                "started_ts": started.replace(tzinfo=None).strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                ),
                "last_active_ts": old.replace(tzinfo=None).strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                ),
            }
            for index, execution_id in enumerate(history_ids)
        ]
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO agent_executions "
                    "(id, project_id, agent_id, external_id, client_name, "
                    "execution_token_hash, lifecycle_protocol_version, kind, "
                    "status, task_description, started_ts, last_active_ts) "
                    "VALUES (:id, :project_id, :agent_id, :external_id, "
                    "'pytest', :token_hash, 1, 'session', 'active', '', "
                    ":started_ts, :last_active_ts)"
                ),
                rows,
            )
            await session.commit()

        first = await expire_stale_agent_executions(3600, now=now)
        assert first["expired"] == 300
        async with get_session() as session:
            pending_after_first = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) "
                        "FROM build_slot_artifact_projections "
                        "WHERE project_id=:project_id "
                        "AND reconciled_ts IS NULL"
                    ),
                    {"project_id": project.data["id"]},
                )
            ).scalar_one()
        assert pending_after_first == 44

        second = await expire_stale_agent_executions(3600, now=now)
        assert second["expired"] == 0
        async with get_session() as session:
            pending_after_second = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) "
                        "FROM build_slot_artifact_projections "
                        "WHERE project_id=:project_id "
                        "AND reconciled_ts IS NULL"
                    ),
                    {"project_id": project.data["id"]},
                )
            ).scalar_one()
        assert pending_after_second == 0

        root = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "bounded-current-root",
                "client_name": "pytest",
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        child = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "bounded-current-child",
                "client_name": "pytest",
                "kind": "subagent",
                "parent_execution_id": root.data["id"],
                "execution_token": _CHILD_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        actual_ancestor_ids = app_module._execution_ancestor_ids
        lineage_sizes: list[int] = []

        def record_lineage_size(executions, execution):
            lineage_sizes.append(len(executions))
            return actual_ancestor_ids(executions, execution)

        monkeypatch.setattr(
            app_module,
            "_execution_ancestor_ids",
            record_lineage_size,
        )
        listed = await client.call_tool(
            "list_agent_executions",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "limit": 2,
            },
        )
        listed_rows = listed.structured_content.get("result")
        assert isinstance(listed_rows, list)
        child_row = next(row for row in listed_rows if row["id"] == child.data["id"])
        assert child_row["ancestor_execution_ids"] == [root.data["id"]]
        assert lineage_sizes
        assert max(lineage_sizes) == 2


@pytest.mark.asyncio
async def test_execution_reaper_retries_terminal_build_slot_artifact_failure(
    isolated_env,
    monkeypatch,
):
    import mcp_agent_mail.app as app_module

    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()
    project_key = pkey("execution-reaper-build-slot-retry")
    async with Client(server) as client:
        project = await client.call_tool(
            "ensure_project",
            {"human_key": project_key},
        )
        agent = await _register_durable_test_agent(client, project_key)
        execution = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "stale-build-retry",
                "client_name": "pytest",
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "slot": "reaper-retry",
                "execution_id": execution.data["id"],
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=2)
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE agent_executions SET started_ts=:old, last_active_ts=:old "
                    "WHERE id=:execution_id"
                ),
                {
                    "old": old.replace(tzinfo=None).strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    ),
                    "execution_id": execution.data["id"],
                },
            )
            await session.commit()

        lease_path = (
            Path(get_settings().storage.root).expanduser().resolve()
            / "projects"
            / project.data["slug"]
            / "build_slots"
            / "reaper-retry"
            / f"{execution.data['id']}.json"
        )
        async with get_session() as session:
            registered_path = (
                await session.execute(
                    text(
                        "SELECT slot_name, slot_path_component "
                        "FROM build_slot_artifact_paths "
                        "WHERE execution_id=:execution_id"
                    ),
                    {"execution_id": execution.data["id"]},
                )
            ).one()
        assert tuple(registered_path) == ("reaper-retry", "reaper-retry")
        actual_release = app_module._release_build_slot_artifacts_for_executions
        attempts = 0

        async def fail_once(*args: Any, **kwargs: Any) -> int:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated reaper build-slot publication failure")
            return await actual_release(*args, **kwargs)

        monkeypatch.setattr(
            app_module,
            "_release_build_slot_artifacts_for_executions",
            fail_once,
        )
        first = await expire_stale_agent_executions(3600, now=now)
        assert first["expired"] == 1
        assert "simulated reaper build-slot publication failure" in " ".join(
            first["archive_warnings"]
        )
        assert json.loads(lease_path.read_text(encoding="utf-8")).get(
            "released_ts"
        ) is None
        async with get_session() as session:
            pending_projection = (
                await session.execute(
                    text(
                        "SELECT reconciled_ts "
                        "FROM build_slot_artifact_projections "
                        "WHERE execution_id=:execution_id"
                    ),
                    {"execution_id": execution.data["id"]},
                )
            ).scalar_one()
        assert pending_projection is None

        second = await expire_stale_agent_executions(3600, now=now)
        assert second["expired"] == 0
        assert second["released_build_slots"] == 1
        assert json.loads(lease_path.read_text(encoding="utf-8"))[
            "released_ts"
        ] is not None
        async with get_session() as session:
            reconciled_projection = (
                await session.execute(
                    text(
                        "SELECT reconciled_ts "
                        "FROM build_slot_artifact_projections "
                        "WHERE execution_id=:execution_id"
                    ),
                    {"execution_id": execution.data["id"]},
                )
            ).scalar_one()
        assert reconciled_projection is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("enforcement_mode", ["observe", "enforce"])
async def test_execution_reaper_runs_in_server_lifespan(
    isolated_env,
    monkeypatch,
    enforcement_mode,
):
    monkeypatch.setenv("AGENT_EXECUTION_ENFORCEMENT_MODE", enforcement_mode)
    monkeypatch.setenv("AGENT_EXECUTION_REAPER_ENABLED", "true")
    monkeypatch.setenv("AGENT_EXECUTION_REAPER_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("AGENT_EXECUTION_REAPER_THRESHOLD_SECONDS", "17")
    clear_settings_cache()
    called = asyncio.Event()
    thresholds: list[int] = []

    async def fake_expire(threshold_seconds: int, **_kwargs: Any) -> dict[str, Any]:
        thresholds.append(threshold_seconds)
        called.set()
        return {
            "expired": 0,
            "execution_ids": [],
            "released_reservations": 0,
            "released_build_slots": 0,
            "expired_at": datetime.now(timezone.utc).isoformat(),
            "archive_warnings": [],
        }

    monkeypatch.setattr(
        "mcp_agent_mail.app.expire_stale_agent_executions",
        fake_expire,
    )
    server = build_mcp_server()
    async with Client(server):
        await asyncio.wait_for(called.wait(), timeout=2)

    assert thresholds == [17]


def test_production_disallows_unauthenticated_localhost_writer(
    isolated_env,
    monkeypatch,
):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "")
    clear_settings_cache()
    assert get_settings().http.allow_localhost_unauthenticated is False

    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "true")
    clear_settings_cache()
    with pytest.raises(ConfigError, match="cannot be enabled"):
        get_settings()


def test_oauth_configuration_fails_closed_in_production(
    isolated_env,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    monkeypatch.setenv("HTTP_OAUTH_ENABLED", "true")
    clear_settings_cache()
    with pytest.raises(
        ConfigError,
        match="HTTP_OAUTH_ENABLED requires non-empty values",
    ):
        get_settings()

    monkeypatch.setenv("HTTP_OAUTH_BASE_URL", "http://iris.example")
    monkeypatch.setenv("HTTP_OAUTH_GITHUB_CLIENT_ID", "github-client")
    monkeypatch.setenv(
        "HTTP_OAUTH_GITHUB_CLIENT_SECRET",
        "test-client-secret-" + ("s" * 32),
    )
    monkeypatch.setenv(
        "HTTP_OAUTH_GITHUB_ALLOWED_IDENTITIES",
        "login:allowed-user",
    )
    monkeypatch.setenv(
        "HTTP_OAUTH_JWT_SIGNING_KEY",
        "test-signing-key-" + ("k" * 32),
    )
    monkeypatch.setenv("HTTP_OAUTH_STORAGE_PATH", "relative/oauth")
    clear_settings_cache()
    with pytest.raises(ConfigError, match="must use https"):
        get_settings()

    monkeypatch.setenv("HTTP_OAUTH_BASE_URL", "https://iris.example")
    clear_settings_cache()
    with pytest.raises(ConfigError, match="must be absolute"):
        get_settings()

    monkeypatch.setenv("HTTP_OAUTH_STORAGE_PATH", str(tmp_path / "oauth"))
    monkeypatch.setenv(
        "HTTP_OAUTH_GITHUB_ALLOWED_IDENTITIES",
        "id:not-a-number",
    )
    clear_settings_cache()
    with pytest.raises(ConfigError, match="id: entries"):
        get_settings()

    monkeypatch.setenv(
        "HTTP_OAUTH_GITHUB_ALLOWED_IDENTITIES",
        "id:12345",
    )
    monkeypatch.setenv("HTTP_OAUTH_ACCESS_TOKEN_TTL_SECONDS", "60")
    clear_settings_cache()
    with pytest.raises(ConfigError, match="must be between 300"):
        get_settings()

    monkeypatch.setenv("HTTP_OAUTH_ACCESS_TOKEN_TTL_SECONDS", "2592000")
    monkeypatch.setenv(
        "HTTP_OAUTH_ALLOWED_CLIENT_REDIRECT_URIS",
        "https://*.example.com/*",
    )
    clear_settings_cache()
    with pytest.raises(ConfigError, match="wildcards only"):
        get_settings()

    monkeypatch.setenv(
        "HTTP_OAUTH_ALLOWED_CLIENT_REDIRECT_URIS",
        "http://127.0.0.1:*,https://vscode.dev/redirect",
    )
    monkeypatch.setenv("HTTP_OAUTH_RBAC_ROLE", "unknown-role")
    clear_settings_cache()
    with pytest.raises(ConfigError, match="HTTP_OAUTH_RBAC_ROLE"):
        get_settings()

    monkeypatch.setenv("HTTP_OAUTH_RBAC_ROLE", "writer")
    clear_settings_cache()
    settings = get_settings()
    assert settings.http.oauth_enabled is True
    assert settings.http.oauth_base_url == "https://iris.example"


def test_execution_reaper_default_tolerates_long_event_driven_tools(
    isolated_env,
    monkeypatch,
):
    monkeypatch.delenv("AGENT_EXECUTION_REAPER_THRESHOLD_SECONDS", raising=False)
    clear_settings_cache()
    settings = get_settings()
    assert settings.agent_execution_reaper_enabled is True
    assert settings.agent_execution_reaper_threshold_seconds == 24 * 60 * 60


@pytest.mark.asyncio
async def test_messaging_flow(isolated_env):
    server = build_mcp_server()
    agent_name = "codex-wsl-mailhost-1"

    async with Client(server) as client:
        health = await client.call_tool("health_check", {})
        assert health.data["status"] == "ok"
        assert health.data["environment"] == "test"

        project = await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        assert project.data["slug"] == "backend"

        agent = await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": agent_name,
                "task_description": "testing",
            },
        )
        assert agent.data["name"] == agent_name

        message = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": agent_name,
                "to": [agent_name],
                "subject": "Test",
                "body_md": "hello",
                "idempotency_key": "server-messaging-flow",
            },
        )
        # New response shape: deliveries list
        deliveries = message.data.get("deliveries") or []
        assert isinstance(deliveries, list)
        assert deliveries and deliveries[0]["delivery"]["status"] == "published"
        assert deliveries[0]["message"]["subject"] == "Test"
        assert deliveries[0]["delivery"]["message_id"] == deliveries[0]["message"]["id"]

        inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "Backend",
                "agent_name": agent_name,
            },
        )
        inbox_items = inbox.structured_content.get("result")
        assert isinstance(inbox_items, list)
        assert len(inbox_items) == 1
        assert inbox_items[0]["subject"] == "Test"

        resource_blocks = await client.read_resource("resource://project/backend")
        assert resource_blocks
        text_payload = resource_blocks[0].text
        assert agent_name in text_payload

        storage_root = Path(get_settings().storage.root).expanduser().resolve()
        profile = (
            storage_root
            / "projects"
            / "backend"
            / "agents"
            / agent_name
            / "profile.json"
        )
        assert profile.exists()
        delivery_id = deliveries[0]["delivery"]["id"]
        message_file = (
            storage_root
            / "projects"
            / "backend"
            / "message_deliveries"
            / f"{delivery_id}.md"
        )
        assert message_file.exists()
        assert "Test" in message_file.read_text()
        repo = Repo(str(storage_root))
        commit_message = str(repo.head.commit.message)
        header = commit_message.splitlines()[0]
        assert header == f"mail-delivery: publish {delivery_id}"
        assert f"Delivery-ID: {delivery_id}" in commit_message
        assert repo.head.commit.hexsha == deliveries[0]["delivery"]["commit_sha"]


@pytest.mark.asyncio
async def test_file_reservation_conflicts_and_release(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        alpha_identity = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name_hint": "codex-wsl-alpha-1",
            },
        )
        beta_identity = await client.call_tool(
            "create_agent_identity",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name_hint": "claude-linux-beta-1",
            },
        )
        alpha_name = alpha_identity.data["name"]
        beta_name = beta_identity.data["name"]

        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": alpha_name,
                "paths": ["src/app.py"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        assert result.data["granted"][0]["path_pattern"] == "src/app.py"

        conflict = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": beta_name,
                "paths": ["src/app.py"],
            },
        )
        # Advisory model: conflicts are reported but reservation is still granted
        assert conflict.data["conflicts"]
        assert conflict.data["granted"]
        assert conflict.data["granted"][0]["path_pattern"] == "src/app.py"

        # Both Alpha and Beta should have active reservations now (advisory model)
        active_only_resource = await client.read_resource("resource://file_reservations/backend?active_only=true")
        active_only_payload = json.loads(active_only_resource[0].text)
        assert len(active_only_payload) == 2
        assert all(entry["path_pattern"] == "src/app.py" for entry in active_only_payload)
        assert {entry["agent"] for entry in active_only_payload} == {alpha_name, beta_name}

        release = await client.call_tool(
            "release_file_reservations",
            {
                "project_key": "Backend",
                "agent_name": alpha_name,
                "paths": ["src/app.py"],
            },
        )
        assert release.data["released"] == 1

        file_reservations_resource = await client.read_resource("resource://file_reservations/backend?active_only=false")
        payload = json.loads(file_reservations_resource[0].text)
        assert any(entry["path_pattern"] == "src/app.py" and entry["released_ts"] is not None for entry in payload)

        # After Alpha releases, Beta's reservation should still be active (advisory model)
        active_only_after_release = await client.read_resource("resource://file_reservations/backend?active_only=true")
        active_reservations = json.loads(active_only_after_release[0].text)
        assert len(active_reservations) == 1
        assert active_reservations[0]["agent"] == beta_name
        assert active_reservations[0]["path_pattern"] == "src/app.py"
        assert active_reservations[0]["released_ts"] is None


@pytest.mark.asyncio
async def test_build_slot_observe_mode_fences_generationless_legacy_lease(
    isolated_env,
    monkeypatch,
):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_EXECUTION_ENFORCEMENT_MODE", "observe")
    clear_settings_cache()
    server = build_mcp_server()
    project_key = pkey("build-slot-observe")
    agent_name = "codex-wsl-buildlegacy-1"

    async with Client(server) as bootstrap_client:
        project = await bootstrap_client.call_tool(
            "ensure_project",
            {"human_key": project_key},
        )
        agent = await _register_durable_test_agent(
            bootstrap_client,
            project_key,
            name=agent_name,
        )

    lease_path = (
        Path(get_settings().storage.root).expanduser().resolve()
        / "projects"
        / project.data["slug"]
        / "build_slots"
        / "legacy-build"
        / f"{agent_name}__legacy-main.json"
    )
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_acquired_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    lease_path.write_text(
        json.dumps(
            {
                "slot": "legacy-build",
                "agent": agent_name,
                "branch": "legacy-main",
                "exclusive": True,
                "acquired_ts": legacy_acquired_ts,
                "expires_ts": (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    common_args = {
        "project_key": project_key,
        "agent_name": agent_name,
        "slot": "legacy-build",
        "branch": "legacy-main",
        "registration_token": agent["registration_token"],
    }
    async with Client(server) as legacy_client:
        acquired = await legacy_client.call_tool(
            "acquire_build_slot",
            common_args,
        )
        assert acquired.data["execution_id"] is None
        assert acquired.data["legacy_unscoped"] is True
        assert acquired.data["lifecycle_protocol_version"] == 0
        assert acquired.data["granted"]["legacy_unscoped"] is True
        assert acquired.data["granted"]["acquired_ts"] != legacy_acquired_ts
        assert any(
            conflict.get("acquired_ts") == legacy_acquired_ts
            for conflict in acquired.data["conflicts"]
        )
        assert any(
            warning.startswith("execution_required_after_rollout:")
            for warning in acquired.data["warnings"]
        )
        assert any(
            warning.startswith("execution_protocol_upgrade_required:")
            for warning in acquired.data["warnings"]
        )

        renewed = await legacy_client.call_tool(
            "renew_build_slot",
            {**common_args, "extend_seconds": 120},
        )
        assert renewed.data["renewed"] is True
        assert renewed.data["legacy_unscoped"] is True
        assert any(
            warning.startswith("execution_required_after_rollout:")
            for warning in renewed.data["warnings"]
        )

        released = await legacy_client.call_tool(
            "release_build_slot",
            common_args,
        )
        assert released.data["released"] is True
        assert released.data["legacy_unscoped"] is True
        assert any(
            warning.startswith("execution_required_after_rollout:")
            for warning in released.data["warnings"]
        )

    legacy_lease = json.loads(lease_path.read_text(encoding="utf-8"))
    assert legacy_lease["acquired_ts"] == legacy_acquired_ts
    assert "released_ts" not in legacy_lease

    current_lease_paths = [
        path for path in lease_path.parent.glob("*.json") if path != lease_path
    ]
    assert len(current_lease_paths) == 1
    current_lease_path = current_lease_paths[0]
    lease = json.loads(current_lease_path.read_text(encoding="utf-8"))
    assert lease["execution_id"] is None
    assert lease["legacy_unscoped"] is True
    assert lease["released_ts"] is not None


@pytest.mark.asyncio
async def test_build_slot_enforce_mode_requires_execution_capability_and_protocol(
    isolated_env,
    monkeypatch,
):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_EXECUTION_ENFORCEMENT_MODE", "enforce")
    clear_settings_cache()
    server = build_mcp_server()
    project_key = pkey("build-slot-enforce")
    agent_name = "codex-wsl-buildstrict-1"

    async with Client(server) as bootstrap_client:
        project = await bootstrap_client.call_tool(
            "ensure_project",
            {"human_key": project_key},
        )
        agent = await _register_durable_test_agent(
            bootstrap_client,
            project_key,
            name=agent_name,
        )
        execution = await bootstrap_client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent_name,
                "external_id": "strict-build-session",
                "client_name": "pytest",
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )

    lease_path = (
        Path(get_settings().storage.root).expanduser().resolve()
        / "projects"
        / project.data["slug"]
        / "build_slots"
        / "strict-build"
        / f"{execution.data['id']}.json"
    )
    base_args = {
        "project_key": project_key,
        "agent_name": agent_name,
        "slot": "strict-build",
        "registration_token": agent["registration_token"],
    }
    execution_args = {
        **base_args,
        "execution_id": execution.data["id"],
        "execution_token": _ROOT_EXECUTION_TOKEN,
    }

    async with Client(server) as stateless_client:
        with pytest.raises(ToolError, match="requires execution_id"):
            await stateless_client.call_tool(
                "acquire_build_slot",
                {
                    **base_args,
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )
        with pytest.raises(ToolError, match="Invalid execution_token"):
            await stateless_client.call_tool(
                "acquire_build_slot",
                {
                    **base_args,
                    "execution_id": execution.data["id"],
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )
        with pytest.raises(ToolError, match="execution_protocol_upgrade_required"):
            await stateless_client.call_tool(
                "acquire_build_slot",
                execution_args,
            )
        assert not lease_path.exists()

        acquired = await stateless_client.call_tool(
            "acquire_build_slot",
            {
                **execution_args,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert acquired.data["execution_id"] == execution.data["id"]
        assert acquired.data["legacy_unscoped"] is False
        assert acquired.data["lifecycle_protocol_version"] == 1
        assert "warnings" not in acquired.data
        before_invalid_renew = json.loads(lease_path.read_text(encoding="utf-8"))

        with pytest.raises(ToolError, match="execution_protocol_upgrade_required"):
            await stateless_client.call_tool(
                "renew_build_slot",
                execution_args,
            )
        assert json.loads(lease_path.read_text(encoding="utf-8")) == before_invalid_renew

        renewed = await stateless_client.call_tool(
            "renew_build_slot",
            {
                **execution_args,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert renewed.data["renewed"] is True
        assert renewed.data["lifecycle_protocol_version"] == 1

        with pytest.raises(ToolError, match="execution_protocol_upgrade_required"):
            await stateless_client.call_tool(
                "release_build_slot",
                execution_args,
            )
        assert json.loads(lease_path.read_text(encoding="utf-8")).get(
            "released_ts"
        ) is None

        released = await stateless_client.call_tool(
            "release_build_slot",
            {
                **execution_args,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert released.data["released"] is True
        assert released.data["lifecycle_protocol_version"] == 1


@pytest.mark.asyncio
async def test_terminal_end_retry_reconciles_root_and_descendant_build_slots(
    isolated_env,
    monkeypatch,
):
    import mcp_agent_mail.app as app_module

    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("AGENT_EXECUTION_ENFORCEMENT_MODE", "enforce")
    clear_settings_cache()
    server = build_mcp_server()
    project_key = pkey("build-slot-end-retry")
    agent_name = "codex-wsl-buildretry-1"

    async with Client(server) as client:
        project = await client.call_tool(
            "ensure_project",
            {"human_key": project_key},
        )
        await _register_durable_test_agent(
            client,
            project_key,
            name=agent_name,
        )
        root = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent_name,
                "external_id": "build-retry-root",
                "client_name": "pytest",
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        child = await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent_name,
                "external_id": "build-retry-child",
                "client_name": "pytest",
                "kind": "subagent",
                "parent_execution_id": root.data["id"],
                "execution_token": _CHILD_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        for execution, token in (
            (root, _ROOT_EXECUTION_TOKEN),
            (child, _CHILD_EXECUTION_TOKEN),
        ):
            await client.call_tool(
                "acquire_build_slot",
                {
                    "project_key": project_key,
                    "agent_name": agent_name,
                    "slot": "retry-build",
                    "exclusive": False,
                    "execution_id": execution.data["id"],
                    "execution_token": token,
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )

        archive_root = (
            Path(get_settings().storage.root).expanduser().resolve()
            / "projects"
            / project.data["slug"]
            / "build_slots"
            / "retry-build"
        )
        lease_paths = {
            execution_id: archive_root / f"{execution_id}.json"
            for execution_id in (root.data["id"], child.data["id"])
        }
        assert all(
            json.loads(path.read_text(encoding="utf-8")).get("released_ts")
            is None
            for path in lease_paths.values()
        )

        actual_release = app_module._release_build_slot_artifacts_for_executions
        attempts = 0

        async def fail_once(*args: Any, **kwargs: Any) -> int:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated post-commit build-slot failure")
            return await actual_release(*args, **kwargs)

        monkeypatch.setattr(
            app_module,
            "_release_build_slot_artifacts_for_executions",
            fail_once,
        )
        first = await client.call_tool(
            "end_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent_name,
                "execution_id": root.data["id"],
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "status": "completed",
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert first.data["already_ended"] is False
        assert "simulated post-commit build-slot failure" in first.data[
            "archive_warning"
        ]
        assert all(
            json.loads(path.read_text(encoding="utf-8")).get("released_ts")
            is None
            for path in lease_paths.values()
        )

        retried = await client.call_tool(
            "end_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent_name,
                "execution_id": root.data["id"],
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "status": "completed",
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )
        assert retried.data["already_ended"] is True
        assert retried.data["released_build_slots"] == 2
        assert retried.data["descendant_execution_ids"] == [child.data["id"]]
        assert all(
            json.loads(path.read_text(encoding="utf-8"))["released_ts"]
            is not None
            for path in lease_paths.values()
        )


@pytest.mark.asyncio
async def test_build_slot_tools_offload_git_and_slot_file_io(isolated_env, monkeypatch):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    main_thread = threading.main_thread()
    path_type = type(Path("/"))
    original_exists = path_type.exists
    original_glob = path_type.glob
    original_read_text = path_type.read_text
    slot_io_events: set[str] = set()

    def _guard_slot_path(path: Path, event: str) -> None:
        if "build_slots" not in path.parts:
            return
        slot_io_events.add(event)
        assert threading.current_thread() is not main_thread

    def checked_exists(self: Path) -> bool:
        _guard_slot_path(self, "exists")
        return original_exists(self)

    def checked_glob(self: Path, pattern: str, *args, **kwargs):
        _guard_slot_path(self, "glob")
        return original_glob(self, pattern, *args, **kwargs)

    def checked_read_text(self: Path, *args, **kwargs) -> str:
        _guard_slot_path(self, "read_text")
        return original_read_text(self, *args, **kwargs)

    @contextlib.contextmanager
    def fake_git_repo(*args, **kwargs):
        assert threading.current_thread() is not main_thread

        class _Branch:
            name = "main"

        class _Repo:
            active_branch = _Branch()

        yield _Repo()

    monkeypatch.setattr("mcp_agent_mail.app._git_repo", fake_git_repo)
    monkeypatch.setattr(path_type, "exists", checked_exists)
    monkeypatch.setattr(path_type, "glob", checked_glob)
    monkeypatch.setattr(path_type, "read_text", checked_read_text)

    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        agent = await _register_build_slot_test_agent(client)
        token = agent["registration_token"]

        acquired = await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        assert acquired.data["granted"]["branch"] == "main"

        renewed = await client.call_tool(
            "renew_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        assert renewed.data["renewed"] is True

        released = await client.call_tool(
            "release_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        assert released.data["released"] is True

    assert slot_io_events >= {"exists", "glob", "read_text"}


@pytest.mark.asyncio
async def test_build_slot_acquire_does_not_write_after_execution_ends(
    isolated_env,
    monkeypatch,
):
    import mcp_agent_mail.app as app_module

    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()
    project_key = pkey("build-slot-end-race")

    async with Client(server) as setup_client:
        ensured = await setup_client.call_tool(
            "ensure_project",
            {"human_key": project_key},
        )
        agent = await _register_durable_test_agent(setup_client, project_key)
        execution = await setup_client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "build-slot-race-session",
                "client_name": "codex",
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )

    archive = await app_module.ensure_archive(
        get_settings(),
        ensured.data["slug"],
    )
    async with Client(server) as acquire_client, Client(server) as end_client:
        async with app_module._archive_write_lock(archive):
            acquire_task = asyncio.create_task(
                acquire_client.call_tool(
                    "acquire_build_slot",
                    {
                        "project_key": project_key,
                        "agent_name": agent["name"],
                        "registration_token": agent["registration_token"],
                        "execution_id": execution.data["id"],
                        "execution_token": _ROOT_EXECUTION_TOKEN,
                        "slot": "race-slot",
                    },
                )
            )
            await asyncio.sleep(0)
            end_task = asyncio.create_task(
                end_client.call_tool(
                    "end_agent_execution",
                    {
                        "project_key": project_key,
                        "agent_name": agent["name"],
                        "registration_token": agent["registration_token"],
                        "execution_id": execution.data["id"],
                        "execution_token": _ROOT_EXECUTION_TOKEN,
                        "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                        "status": "completed",
                    },
                )
            )
            for _ in range(200):
                async with get_session() as session:
                    status = (
                        await session.execute(
                            text(
                                "SELECT status FROM agent_executions WHERE id=:execution_id"
                            ),
                            {"execution_id": execution.data["id"]},
                        )
                    ).scalar_one()
                if status == "completed":
                    break
                await asyncio.sleep(0.01)
            assert status == "completed"

        ended = await end_task
        assert ended.data["execution"]["status"] == "completed"
        with pytest.raises(ToolError, match="not active"):
            await acquire_task

    lease_path = (
        archive.root
        / "build_slots"
        / "race-slot"
        / f"{execution.data['id']}.json"
    )
    assert not lease_path.exists()


@pytest.mark.asyncio
async def test_build_slot_acquire_surfaces_archive_write_failure(
    isolated_env,
    monkeypatch,
):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()
    project_key = pkey("build-slot-write-failure")

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        agent = await _register_durable_test_agent(client, project_key)
        await client.call_tool(
            "start_agent_execution",
            {
                "project_key": project_key,
                "agent_name": agent["name"],
                "external_id": "build-slot-write-session",
                "client_name": "codex",
                "execution_token": _ROOT_EXECUTION_TOKEN,
                "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
            },
        )

        path_type = type(Path("/"))
        original_write_text = path_type.write_text

        def failing_write_text(self: Path, *args: Any, **kwargs: Any) -> int:
            if "build_slots" in self.parts:
                raise PermissionError("simulated build-slot write denial")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(path_type, "write_text", failing_write_text)
        with pytest.raises(ToolError, match="simulated build-slot write denial"):
            await client.call_tool(
                "acquire_build_slot",
                {
                    "project_key": project_key,
                    "agent_name": agent["name"],
                    "slot": "write-failure-slot",
                },
            )


@pytest.mark.asyncio
async def test_build_slot_tools_hold_archive_lock_during_slot_io(isolated_env, monkeypatch):
    import mcp_agent_mail.app as app_module

    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()
    path_type = type(Path("/"))
    original_glob = path_type.glob
    original_read_text = path_type.read_text
    original_write_text = path_type.write_text
    original_archive_write_lock = app_module._archive_write_lock
    lock_depth = 0
    slot_io_depths: list[int] = []

    def _record_slot_io(path: Path) -> None:
        if "build_slots" in path.parts:
            slot_io_depths.append(lock_depth)

    def checked_glob(self: Path, pattern: str, *args, **kwargs):
        _record_slot_io(self)
        return original_glob(self, pattern, *args, **kwargs)

    def checked_read_text(self: Path, *args, **kwargs) -> str:
        _record_slot_io(self)
        return original_read_text(self, *args, **kwargs)

    def checked_write_text(self: Path, *args, **kwargs) -> int:
        _record_slot_io(self)
        return original_write_text(self, *args, **kwargs)

    @contextlib.asynccontextmanager
    async def tracking_archive_write_lock(*args: Any, **kwargs: Any):
        nonlocal lock_depth
        lock_depth += 1
        try:
            async with original_archive_write_lock(*args, **kwargs):
                yield
        finally:
            lock_depth -= 1

    monkeypatch.setattr(path_type, "glob", checked_glob)
    monkeypatch.setattr(path_type, "read_text", checked_read_text)
    monkeypatch.setattr(path_type, "write_text", checked_write_text)
    monkeypatch.setattr(app_module, "_archive_write_lock", tracking_archive_write_lock)

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        agent = await _register_build_slot_test_agent(client)
        token = agent["registration_token"]

        await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        await client.call_tool(
            "renew_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        await client.call_tool(
            "release_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )

    assert slot_io_depths
    assert all(depth > 0 for depth in slot_io_depths)


@pytest.mark.asyncio
async def test_build_slot_renew_missing_lease_is_noop(isolated_env, monkeypatch):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        agent = await _register_build_slot_test_agent(client)
        token = agent["registration_token"]

        renewed = await client.call_tool(
            "renew_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        assert renewed.data["renewed"] is False
        assert renewed.data["expires_ts"] is None

    slot_dir = (
        Path(get_settings().storage.root).expanduser().resolve()
        / "projects"
        / "backend"
        / "build_slots"
        / "frontend-build"
    )
    assert list(slot_dir.glob("*.json")) == []


@pytest.mark.asyncio
async def test_build_slot_release_missing_lease_is_noop(isolated_env, monkeypatch):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        agent = await _register_build_slot_test_agent(client)
        token = agent["registration_token"]

        released = await client.call_tool(
            "release_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        assert released.data["released"] is False

    slot_dir = (
        Path(get_settings().storage.root).expanduser().resolve()
        / "projects"
        / "backend"
        / "build_slots"
        / "frontend-build"
    )
    assert list(slot_dir.glob("*.json")) == []


@pytest.mark.asyncio
async def test_build_slot_renew_released_lease_is_noop(isolated_env, monkeypatch):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        agent = await _register_build_slot_test_agent(client)
        token = agent["registration_token"]

        await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        await client.call_tool(
            "release_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )

        renewed = await client.call_tool(
            "renew_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        assert renewed.data["renewed"] is False
        assert renewed.data["expires_ts"] is None


@pytest.mark.asyncio
async def test_build_slot_release_already_released_lease_is_noop(isolated_env, monkeypatch):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        agent = await _register_build_slot_test_agent(client)
        token = agent["registration_token"]

        first_release = await client.call_tool(
            "release_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        assert first_release.data["released"] is False

        await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        released = await client.call_tool(
            "release_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        assert released.data["released"] is True

        released_again = await client.call_tool(
            "release_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "registration_token": token,
            },
        )
        assert released_again.data["released"] is False


@pytest.mark.asyncio
async def test_build_slot_renew_does_not_shorten_active_lease(isolated_env, monkeypatch):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        agent = await _register_build_slot_test_agent(client)
        token = agent["registration_token"]

        acquired = await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "ttl_seconds": 3600,
                "registration_token": token,
            },
        )
        original_exp = datetime.fromisoformat(acquired.data["granted"]["expires_ts"])

        renewed = await client.call_tool(
            "renew_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "extend_seconds": 60,
                "registration_token": token,
            },
        )
        assert renewed.data["renewed"] is True
        renewed_exp = datetime.fromisoformat(renewed.data["expires_ts"])
        assert renewed_exp >= original_exp


@pytest.mark.asyncio
async def test_build_slot_reacquire_same_holder_does_not_shorten_active_lease(isolated_env, monkeypatch):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        agent = await _register_build_slot_test_agent(client)
        token = agent["registration_token"]

        acquired = await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "ttl_seconds": 3600,
                "registration_token": token,
            },
        )
        original_exp = datetime.fromisoformat(acquired.data["granted"]["expires_ts"])
        original_acquired = acquired.data["granted"]["acquired_ts"]

        reacquired = await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "ttl_seconds": 60,
                "registration_token": token,
            },
        )
        reacquired_exp = datetime.fromisoformat(reacquired.data["granted"]["expires_ts"])
        assert reacquired_exp >= original_exp
        assert reacquired.data["granted"]["acquired_ts"] == original_acquired


@pytest.mark.asyncio
async def test_build_slot_renew_and_release_honor_explicit_branch_when_repo_branch_changes(isolated_env, monkeypatch):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()

    @contextlib.contextmanager
    def fake_git_repo(path, search_parent_directories=True):
        current_branch = "feature"

        class _Git:
            def rev_parse(self, *args):
                return current_branch

        class _ActiveBranch:
            name = current_branch

        class _Repo:
            active_branch = _ActiveBranch()
            git = _Git()

            def close(self):
                return None

        yield _Repo()

    monkeypatch.setattr("mcp_agent_mail.app._git_repo", fake_git_repo)
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        agent = await _register_build_slot_test_agent(client)
        token = agent["registration_token"]

        acquired = await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "branch": "main",
                "registration_token": token,
            },
        )
        assert acquired.data["granted"]["branch"] == "main"

        renewed = await client.call_tool(
            "renew_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "branch": "main",
                "registration_token": token,
            },
        )
        assert renewed.data["renewed"] is True

        released = await client.call_tool(
            "release_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "frontend-build",
                "branch": "main",
                "registration_token": token,
            },
        )
        assert released.data["released"] is True


@pytest.mark.asyncio
async def test_build_slot_conflicts_respect_both_requester_and_holder_exclusivity(isolated_env, monkeypatch):
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        blue = await _register_build_slot_test_agent(
            client,
            name=_BUILD_AGENT_NAME,
            execution_token=_ROOT_EXECUTION_TOKEN,
        )
        green = await _register_build_slot_test_agent(
            client,
            name=_BUILD_PEER_NAME,
            execution_token=_CHILD_EXECUTION_TOKEN,
        )

        blue_token = blue["registration_token"]
        green_token = green["registration_token"]

        await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "shared-first",
                "exclusive": False,
                "execution_id": blue["execution_id"],
                "execution_token": blue["execution_token"],
                "registration_token": blue_token,
            },
        )
        exclusive_second = await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_PEER_NAME,
                "slot": "shared-first",
                "exclusive": True,
                "execution_id": green["execution_id"],
                "execution_token": green["execution_token"],
                "registration_token": green_token,
            },
        )
        assert [entry["agent"] for entry in exclusive_second.data["conflicts"]] == [
            _BUILD_AGENT_NAME
        ]

        await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_AGENT_NAME,
                "slot": "exclusive-first",
                "exclusive": True,
                "execution_id": blue["execution_id"],
                "execution_token": blue["execution_token"],
                "registration_token": blue_token,
            },
        )
        shared_second = await client.call_tool(
            "acquire_build_slot",
            {
                "project_key": "Backend",
                "agent_name": _BUILD_PEER_NAME,
                "slot": "exclusive-first",
                "exclusive": False,
                "execution_id": green["execution_id"],
                "execution_token": green["execution_token"],
                "registration_token": green_token,
            },
        )
        assert [entry["agent"] for entry in shared_second.data["conflicts"]] == [
            _BUILD_AGENT_NAME
        ]


@pytest.mark.asyncio
async def test_legacy_mailbox_reservation_does_not_block_atomic_delivery(isolated_env):
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-sender-1",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "claude-linux-holder-1",
            },
        )

        # Legacy mailbox paths are no longer message publication targets.
        reservation = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": "claude-linux-holder-1",
                "paths": ["agents/codex-wsl-sender-1/inbox/*/*/*.md"],
                "ttl_seconds": 1800,
                "exclusive": True,
            },
        )
        assert reservation.data["granted"]

        # The immutable publisher writes one message_deliveries/<id>.md receipt,
        # so a stale reservation over the removed inbox bundle cannot gate it.
        resp = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "codex-wsl-sender-1",
                "to": ["codex-wsl-sender-1"],
                "subject": "Atomic delivery",
                "body_md": "hello",
                "idempotency_key": "legacy-mailbox-reservation-active",
            },
        )
        delivery = resp.data["deliveries"][0]
        assert delivery["delivery"]["status"] == "published"
        assert delivery["message"]["subject"] == "Atomic delivery"


@pytest.mark.asyncio
async def test_force_release_file_reservation_stale(isolated_env, monkeypatch):
    monkeypatch.setenv("FILE_RESERVATION_INACTIVITY_SECONDS", "5")
    monkeypatch.setenv("FILE_RESERVATION_ACTIVITY_GRACE_SECONDS", "1")
    clear_settings_cache()
    try:
        server = build_mcp_server()
        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": pkey("backend")})
            holder = await client.call_tool(
                "register_agent",
                {
                    "project_key": "Backend",
                    "program": "codex",
                    "model": "gpt-5",
                    "name": "codex-wsl-holder-1",
                },
            )
            execution = await client.call_tool(
                "start_agent_execution",
                {
                    "project_key": "Backend",
                    "agent_name": "codex-wsl-holder-1",
                    "external_id": "force-release-stale",
                    "client_name": "pytest",
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )
            reservation = await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "Backend",
                    "agent_name": "codex-wsl-holder-1",
                    "paths": ["src/app.py"],
                    "ttl_seconds": 3600,
                    "origin": "explicit",
                    "execution_id": execution.data["id"],
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                },
            )
            reservation_id = reservation.data["granted"][0]["id"]

            async with get_session() as session:
                project_row = await session.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": "backend"})
                project_id = project_row.scalar_one()
                stale_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
                await session.execute(
                    text(
                        "UPDATE agents SET last_active_ts = :ts WHERE project_id = :pid AND lower(name) = :name"
                    ),
                    {
                        "ts": stale_cutoff,
                        "pid": project_id,
                        "name": "codex-wsl-holder-1",
                    },
                )
                await session.commit()

            await client.call_tool(
                "end_agent_execution",
                {
                    "project_key": "Backend",
                    "agent_name": "codex-wsl-holder-1",
                    "execution_id": execution.data["id"],
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                    "status": "failed",
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )
            force = await client.call_tool(
                "force_release_file_reservation",
                {
                    "project_key": "Backend",
                    "agent_name": "codex-wsl-holder-1",
                    "registration_token": holder.data["registration_token"],
                    "file_reservation_id": reservation_id,
                },
            )
            assert force.data["released"] == 1
            assert force.data["reservation"]["notified"] is False
            resource = await client.read_resource("resource://file_reservations/backend?active_only=false")
            payload = json.loads(resource[0].text)
            released = next(item for item in payload if item["id"] == reservation_id)
            assert released["released_ts"] is not None
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_force_release_file_reservation_expired_is_noop(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        holder = await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-holder-1",
            },
        )
        reservation = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                "agent_name": "codex-wsl-holder-1",
                "registration_token": holder.data["registration_token"],
                "paths": ["src/app.py"],
                "ttl_seconds": 3600,
            },
        )
        reservation_id = reservation.data["granted"][0]["id"]

        async with get_session() as session:
            expired = (
                (datetime.now(timezone.utc) - timedelta(minutes=10))
                .replace(tzinfo=None)
                .strftime("%Y-%m-%d %H:%M:%S.%f")
            )
            await session.execute(
                text("UPDATE file_reservations SET expires_ts = :ts WHERE id = :id"),
                {"ts": expired, "id": reservation_id},
            )
            await session.commit()

        force = await client.call_tool(
            "force_release_file_reservation",
            {
                "project_key": "Backend",
                "agent_name": "codex-wsl-holder-1",
                "registration_token": holder.data["registration_token"],
                "file_reservation_id": reservation_id,
            },
        )
        assert force.data["released"] == 0
        assert force.data["already_released"] is True
        assert force.data["expired"] is True
        assert force.data["released_at"] is not None

        async with get_session() as session:
            result = await session.execute(
                text("SELECT released_ts FROM file_reservations WHERE id = :id"),
                {"id": reservation_id},
            )
            released_ts = result.scalar_one()
            assert released_ts is not None

        resource = await client.read_resource("resource://file_reservations/backend?active_only=false")
        payload = json.loads(resource[0].text)
        released = next(item for item in payload if item["id"] == reservation_id)
        assert released["released_ts"] is not None


@pytest.mark.asyncio
async def test_force_release_audit_ignores_legacy_mailbox_reservation(isolated_env, monkeypatch):
    monkeypatch.setenv("FILE_RESERVATION_INACTIVITY_SECONDS", "3600")
    monkeypatch.setenv("FILE_RESERVATION_ACTIVITY_GRACE_SECONDS", "120")
    clear_settings_cache()
    try:
        server = build_mcp_server()
        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": pkey("backend")})
            holder = await client.call_tool(
                "register_agent",
                {
                    "project_key": "Backend",
                    "program": "codex",
                    "model": "gpt-5",
                    "name": "codex-wsl-holder-1",
                },
            )
            execution = await client.call_tool(
                "start_agent_execution",
                {
                    "project_key": "Backend",
                    "agent_name": "codex-wsl-holder-1",
                    "external_id": "force-release-audit",
                    "client_name": "pytest",
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )
            await client.call_tool(
                "register_agent",
                {
                    "project_key": "Backend",
                    "program": "codex",
                    "model": "gpt-5",
                    "name": "codex-linux-blocker-1",
                },
            )
            reservation = await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "Backend",
                    "agent_name": "codex-wsl-holder-1",
                    "paths": ["src/app.py"],
                    "ttl_seconds": 3600,
                    "origin": "explicit",
                    "execution_id": execution.data["id"],
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                },
            )
            reservation_id = reservation.data["granted"][0]["id"]

            blocker = await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "Backend",
                    "agent_name": "codex-linux-blocker-1",
                    "paths": ["agents/codex-wsl-holder-1/inbox/*/*/*.md"],
                    "ttl_seconds": 1800,
                    "exclusive": True,
                },
            )
            assert blocker.data["granted"]

            async with get_session() as session:
                project_row = await session.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": "backend"})
                project_id = project_row.scalar_one()
                stale_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
                await session.execute(
                    text(
                        "UPDATE agents SET last_active_ts = :ts WHERE project_id = :pid AND lower(name) = :name"
                    ),
                    {
                        "ts": stale_cutoff,
                        "pid": project_id,
                        "name": "codex-wsl-holder-1",
                    },
                )
                await session.commit()

            await client.call_tool(
                "end_agent_execution",
                {
                    "project_key": "Backend",
                    "agent_name": "codex-wsl-holder-1",
                    "execution_id": execution.data["id"],
                    "execution_token": _ROOT_EXECUTION_TOKEN,
                    "status": "failed",
                    "lifecycle_protocol_version": _EXECUTION_PROTOCOL_VERSION,
                },
            )
            force = await client.call_tool(
                "force_release_file_reservation",
                {
                    "project_key": "Backend",
                    "agent_name": "codex-wsl-holder-1",
                    "registration_token": holder.data["registration_token"],
                    "file_reservation_id": reservation_id,
                },
            )
            assert force.data["released"] == 1
            assert force.data["reservation"]["notified"] is False
            assert force.data["reservation"].get("notification_error") is None
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_force_release_rejects_recent_activity(isolated_env, monkeypatch):
    monkeypatch.setenv("FILE_RESERVATION_INACTIVITY_SECONDS", "300")
    monkeypatch.setenv("FILE_RESERVATION_ACTIVITY_GRACE_SECONDS", "120")
    clear_settings_cache()
    try:
        server = build_mcp_server()
        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": pkey("backend")})
            await client.call_tool(
                "register_agent",
                {
                    "project_key": "Backend",
                    "program": "codex",
                    "model": "gpt-5",
                    "name": "codex-wsl-holder-1",
                },
            )
            reservation = await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "Backend",
                    "agent_name": "codex-wsl-holder-1",
                    "paths": ["src/app.py"],
                    "ttl_seconds": 3600,
                },
            )
            reservation_id = reservation.data["granted"][0]["id"]
            with pytest.raises(ToolError):
                await client.call_tool(
                    "force_release_file_reservation",
                    {
                        "project_key": "Backend",
                        "agent_name": "codex-wsl-holder-1",
                        "file_reservation_id": reservation_id,
                    },
                )

            resource = await client.read_resource("resource://file_reservations/backend?active_only=true")
            entries = json.loads(resource[0].text)
            assert entries and entries[0]["id"] == reservation_id
            assert entries[0]["released_ts"] is None
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_file_reservation_integration_logging(isolated_env, monkeypatch):
    monkeypatch.setenv("FILE_RESERVATION_INACTIVITY_SECONDS", "5")
    monkeypatch.setenv("FILE_RESERVATION_ACTIVITY_GRACE_SECONDS", "2")
    clear_settings_cache()

    console = Console(record=True, force_terminal=True)
    holder_name = "codex-wsl-holder-1"
    peer_name = "claude-linux-peer-1"

    def _log(title: str, description: str, data: object | None = None) -> None:
        renderables: list[Text | Syntax] = [Text(description)]
        if data is not None:
            rendered = json.dumps(data, indent=2, default=str)
            renderables.append(Syntax(rendered, "json", theme="monokai", word_wrap=True))
        console.print(Panel(Group(*renderables), title=title, border_style="cyan"))

    try:
        server = build_mcp_server()
        async with Client(server) as client:
            await client.call_tool("ensure_project", {"human_key": pkey("backend")})
            _log("Project", "Ensured project '/backend'")

            for name in (holder_name, peer_name):
                await client.call_tool(
                    "register_agent",
                    {
                        "project_key": "Backend",
                        "program": "codex",
                        "model": "gpt-5",
                        "name": name,
                    },
                )
                _log("Agent Registered", f"Registered agent {name}")

            reservation = await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": "Backend",
                    "agent_name": holder_name,
                    "paths": ["src/app.py"],
                    "ttl_seconds": 3600,
                },
            )
            reservation_id = reservation.data["granted"][0]["id"]
            _log(
                "Reservation Granted",
                f"{holder_name} reserved src/app.py",
                reservation.data,
            )

            resource_initial = await client.read_resource("resource://file_reservations/backend?active_only=false")
            payload_initial = json.loads(resource_initial[0].text)
            _log("Initial Reservations", "Reservation state immediately after grant", payload_initial)

            async with get_session() as session:
                project_row = await session.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": "backend"})
                project_id = project_row.scalar_one()
                stale_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
                await session.execute(
                    text("UPDATE agents SET last_active_ts = :ts WHERE project_id = :pid AND lower(name) = :name"),
                    {"ts": stale_cutoff, "pid": project_id, "name": holder_name},
                )
                await session.commit()
            _log(
                "Agent Last Active Adjusted",
                f"Artificially aged {holder_name} last_active_ts to simulate inactivity.",
            )

            resource_after = await client.read_resource("resource://file_reservations/backend?active_only=false")
            payload_after = json.loads(resource_after[0].text)
            _log("Post Sweep", "Reservation state after inactivity sweep", payload_after)

            released_entry = next(item for item in payload_after if item["id"] == reservation_id)
            assert released_entry["released_ts"] is not None
            assert released_entry["stale"] is False
            assert any("agent_inactive" in reason for reason in released_entry["stale_reasons"])

            active_only = await client.read_resource("resource://file_reservations/backend?active_only=true")
            payload_active_only = json.loads(active_only[0].text)
            _log("Active Reservations", "Active-only listing should omit released entries", payload_active_only)
            assert payload_active_only == []

            notebook = console.export_text(clear=False)
            assert f"{holder_name} reserved" in notebook
            assert "agent_inactive" in notebook
    finally:
        clear_settings_cache()


@pytest.mark.asyncio
async def test_search_and_summarize(isolated_env):
    server = build_mcp_server()
    agent_name = "codex-wsl-searchhost-1"

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": agent_name,
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": agent_name,
                "to": [agent_name],
                "subject": "Plan",
                "body_md": "- TODO: implement FTS\n- ACTION: review file reservations",
                "idempotency_key": "search-and-summarize-plan",
            },
        )
        search = await client.call_tool(
            "search_messages",
            {"project_key": "Backend", "query": "FTS", "limit": 5},
        )
        def _get_subject(x):
            if isinstance(x, dict):
                return x.get("subject")
            return getattr(x, "subject", None)
        assert sum(1 for _ in search.data) >= 1

        summary = await client.call_tool(
            "summarize_thread",
            {"project_key": "Backend", "thread_id": "1", "include_examples": True},
        )
        summary_data = summary.data["summary"]
        assert "TODO" in " ".join(summary_data["key_points"])
        assert summary.data["examples"]


@pytest.mark.asyncio
async def test_attachment_paths_fail_closed_before_delivery_intent(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-attachment-1",
            },
        )
        with pytest.raises(
            Exception,
            match="attachment_paths and convert_images are disabled",
        ):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "codex-wsl-attachment-1",
                    "to": ["codex-wsl-attachment-1"],
                    "subject": "Reserved attachment surface",
                    "body_md": "The request must fail before reading this path.",
                    "attachment_paths": ["missing.png"],
                    "idempotency_key": "attachment-paths-rejected",
                },
            )

    async with get_session() as session:
        count = await session.scalar(text("SELECT COUNT(*) FROM message_deliveries"))
    assert count == 0


@pytest.mark.asyncio
async def test_attachment_rejection_precedes_path_resolution(isolated_env, monkeypatch):
    from mcp_agent_mail import storage as _storage

    attachment_path = Path("/reserved/never-resolve.png")
    original_resolve = _storage._expanduser_resolve_path
    seen_resolve = 0

    def forbidden_resolve(path: Path) -> Path:
        nonlocal seen_resolve
        if path == attachment_path:
            seen_resolve += 1
            raise AssertionError(f"reserved attachment path was resolved: {path}")
        return original_resolve(path)

    monkeypatch.setattr(_storage, "_expanduser_resolve_path", forbidden_resolve)

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-pathguard-1",
            },
        )
        with pytest.raises(
            Exception,
            match="attachment_paths and convert_images are disabled",
        ):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "Backend",
                    "sender_name": "codex-wsl-pathguard-1",
                    "to": ["codex-wsl-pathguard-1"],
                    "subject": "No path resolution",
                    "body_md": "Reserved attachment input.",
                    "attachment_paths": [str(attachment_path)],
                    "idempotency_key": "attachment-resolution-rejected",
                },
            )

    assert seen_resolve == 0


@pytest.mark.asyncio
async def test_rich_logger_does_not_throw(isolated_env, monkeypatch):
    # Enable rich logging flags
    from mcp_agent_mail import config as _config
    monkeypatch.setenv("LOG_RICH_ENABLED", "true")
    monkeypatch.setenv("LOG_INCLUDE_TRACE", "true")
    # Rebuild settings cache
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    server = build_mcp_server()
    # Start a client and hit a couple of endpoints to produce logs
    async with Client(server) as client:
        res = await client.call_tool("health_check", {})
        assert res.data["status"] == "ok"
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-richlogger-1",
            },
        )
        await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "codex-wsl-richlogger-1",
                "to": ["codex-wsl-richlogger-1"],
                "subject": "Rich",
                "body_md": "hello",
                "idempotency_key": "rich-logger-message",
            },
        )


@pytest.mark.asyncio
async def test_server_level_attachment_policy_does_not_mutate_markdown(isolated_env, monkeypatch):
    # The legacy server switch cannot re-enable the reserved attachment API.
    monkeypatch.setenv("CONVERT_IMAGES", "true")
    from mcp_agent_mail import config as _config
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-markdown-1",
                # leave attachments_policy default (auto)
            },
        )
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "codex-wsl-markdown-1",
                "to": ["codex-wsl-markdown-1"],
                "subject": "Markdown remains source",
                "body_md": "Here ![pic](local-reference.png)",
                "idempotency_key": "server-attachment-policy-ignored",
            },
        )
        published = result.data["deliveries"][0]
        assert published["delivery"]["status"] == "published"
        assert published["message"]["attachments"] == []
        assert published["message"]["body_md"] == "Here ![pic](local-reference.png)"


@pytest.mark.asyncio
async def test_atomic_replay_is_stable_across_legacy_reservation_expiry(isolated_env, monkeypatch):
    # Even the legacy enforcement switch cannot make removed mailbox paths
    # part of the immutable delivery write set.
    monkeypatch.setenv("FILE_RESERVATIONS_ENFORCEMENT_ENABLED", "true")
    from mcp_agent_mail import config as _config
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()

    server = build_mcp_server()
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("backend")})
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "codex-wsl-replay-1",
            },
        )
        await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "claude-linux-holder-1",
            },
        )
        # Beta reserves the removed per-agent inbox surface.
        reservation = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "Backend",
                    "agent_name": "claude-linux-holder-1",
                    "paths": ["agents/codex-wsl-replay-1/inbox/*/*/*.md"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )
        assert reservation.data["granted"]

        # Publication succeeds while the obsolete reservation is active.
        resp = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "codex-wsl-replay-1",
                "to": ["codex-wsl-replay-1"],
                "subject": "Stable replay",
                "body_md": "hello",
                "idempotency_key": "legacy-mailbox-reservation-replay",
            },
        )
        first = resp.data["deliveries"][0]
        assert first["delivery"]["status"] == "published"

        reservation_id = reservation.data["granted"][0]["id"]
        async with get_session() as session:
            expired = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=60)
            reservation_row = await session.get(FileReservation, reservation_id)
            assert reservation_row is not None
            reservation_row.expires_ts = expired
            session.add(reservation_row)
            await session.commit()

        # Replaying the same operation after the reservation expires reuses the
        # exact delivery and visible Message row.
        resp2 = await client.call_tool(
            "send_message",
            {
                "project_key": "Backend",
                "sender_name": "codex-wsl-replay-1",
                "to": ["codex-wsl-replay-1"],
                "subject": "Stable replay",
                "body_md": "hello",
                "idempotency_key": "legacy-mailbox-reservation-replay",
            },
        )
        deliveries = resp2.data.get("deliveries") or []
        assert deliveries, resp2.data
        assert deliveries[0]["delivery"]["id"] == first["delivery"]["id"]
        assert deliveries[0]["delivery"]["reused"] is True
        assert deliveries[0]["message"]["id"] == first["message"]["id"]


@pytest.mark.asyncio
async def test_project_sibling_suggestions_backend(isolated_env, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")
    server = build_mcp_server()

    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": pkey("data/projects/backend_core")})
        await client.call_tool("ensure_project", {"human_key": pkey("data/projects/backend_core_ui")})

    await refresh_project_sibling_suggestions(max_pairs=5)
    data = await get_project_sibling_data()

    async with get_session() as session:
        rows = await session.execute(text("SELECT id FROM projects ORDER BY slug"))
        project_ids = [int(row[0]) for row in rows.fetchall()]

    assert len(project_ids) == 2
    first_id, second_id = project_ids
    assert first_id in data and second_id in data
    assert any(entry["peer"]["id"] == second_id for entry in data[first_id]["suggested"])

    confirmation = await update_project_sibling_status(first_id, second_id, "confirmed")
    assert confirmation["status"] == "confirmed"

    updated_map = await get_project_sibling_data()
    assert any(entry["peer"]["id"] == second_id for entry in updated_map[first_id]["confirmed"])
    assert not any(entry["peer"]["id"] == second_id for entry in updated_map[first_id]["suggested"])
    assert any(entry["peer"]["id"] == first_id for entry in updated_map[second_id]["confirmed"])
