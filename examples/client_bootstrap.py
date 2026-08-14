"""Minimal client bootstrap that honours the tooling directory guidance.

This script is intentionally verbose so MCP client implementers can copy/paste
the parts they need. It demonstrates:

1. Fetching `resource://tooling/directory` and selecting an active cluster.
2. Optionally polling `resource://tooling/metrics` for dashboards.
3. Falling back to workflow macros when the selected model is "small".

The code uses the same `fastmcp.Client` class that the test-suite relies on,
so it can run directly against a locally launched `serve-http` instance.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from typing import Any

from decouple import Config as DecoupleConfig, RepositoryEnv
from fastmcp import Client

decouple_config = DecoupleConfig(RepositoryEnv(".env"))


def _select_cluster(directory_payload: dict[str, Any], cluster_name: str) -> dict[str, Any]:
    for cluster in directory_payload.get("clusters", []):
        if cluster.get("name") == cluster_name:
            return cluster
    raise RuntimeError(f"Cluster '{cluster_name}' not found in directory payload")


async def main() -> None:
    endpoint = decouple_config("AGENT_MAIL_URL", default="http://127.0.0.1:8765/api/")
    project_key = decouple_config("AGENT_MAIL_PROJECT", default="/owner/backend")
    agent_name = decouple_config("AGENT_MAIL_AGENT", default="codex-linux-bootstrap-1")
    registration_token = decouple_config("AGENT_MAIL_REGISTRATION_TOKEN", default="")
    if not registration_token:
        raise RuntimeError(
            "Provision the durable Agent first, then set "
            "AGENT_MAIL_REGISTRATION_TOKEN in a private environment/state store."
        )

    execution_token = secrets.token_hex(32)
    execution_id: str | None = None
    async with Client(endpoint) as client:
        directory_blocks = await client.read_resource("resource://tooling/directory")
        directory_payload = json.loads(getattr(directory_blocks[0], "text", "{}"))
        print("==> Loaded tooling directory; clusters available:")
        for cluster in directory_payload.get("clusters", []):
            print(f" - {cluster['name']} ({len(cluster['tools'])} tools)")

        # Pick a workflow for this session
        active_cluster = _select_cluster(directory_payload, "Messaging Lifecycle")

        # Determine which tools to activate. The simple heuristic below hides
        # high-complexity tools unless the underlying model is considered
        # "large". Replace this with your own routing logic.
        model_size = "small"
        enabled_tools: list[str] = []
        for tool in active_cluster["tools"]:
            complexity = tool.get("complexity", "medium")
            if model_size == "small" and complexity == "high":
                continue
            enabled_tools.append(tool["name"])

        # Always bolt on the workflow macros when using smaller models.
        if model_size == "small":
            for macro_name in ("macro_start_session", "macro_prepare_thread"):
                if macro_name not in enabled_tools:
                    enabled_tools.append(macro_name)

        print("==> Enable the following tools in the agent runtime:")
        for name in enabled_tools:
            print(f"   - {name}")

        # Optionally read metrics for dashboards
        metrics_blocks = await client.read_resource("resource://tooling/metrics")
        print("==> Current tool metrics snapshot:")
        print(getattr(metrics_blocks[0], "text", "{}"))

        # Finally run whatever workflow you need. As an example, call the macro
        # to bootstrap a session.
        try:
            response = await client.call_tool(
                "macro_start_session",
                {
                    "human_key": project_key,
                    "program": "codex",
                    "model": "gpt-5-small",
                    "agent_name": agent_name,
                    "external_id": f"bootstrap-{uuid.uuid4()}",
                    "client_name": "codex",
                    "execution_token": execution_token,
                    "registration_token": registration_token,
                    "file_reservation_paths": ["src/app.py"],
                },
            )
            result = response.data
            execution_id = str(result["execution"]["id"])
            print("==> Session ready (credentials intentionally omitted):")
            print(
                json.dumps(
                    {
                        "project": result["project"],
                        "agent": result["agent"],
                        "execution": result["execution"],
                        "file_reservations": result["file_reservations"],
                        "inbox_count": len(result["inbox"]),
                    },
                    indent=2,
                )
            )
        finally:
            if execution_id is not None:
                await client.call_tool(
                    "end_agent_execution",
                    {
                        "project_key": project_key,
                        "agent_name": agent_name,
                        "execution_id": execution_id,
                        "execution_token": execution_token,
                        "lifecycle_protocol_version": 1,
                        "status": "completed",
                        "registration_token": registration_token,
                    },
                )


if __name__ == "__main__":
    asyncio.run(main())
