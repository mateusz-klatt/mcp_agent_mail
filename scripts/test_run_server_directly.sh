#!/usr/bin/env bash
# Test running server directly vs via script to see if Rich output differs

set -euo pipefail

# Read from the environment, never hardcoded. A literal token here is exported
# into the shell of everyone who runs this file and into every process it spawns
# — and this one lived in a tracked file on a public remote. Dead against our
# server, but a credential handed to every reader all the same.
: "${HTTP_BEARER_TOKEN:?set HTTP_BEARER_TOKEN first, e.g. from ~/.agent-mail.env}"
export HTTP_BEARER_TOKEN

echo "========================================"
echo "Running server with direct Python call"
echo "========================================"
echo ""
echo "Command: python -m mcp_agent_mail.cli serve-http"
echo ""

cd /data/projects/mcp_agent_mail
python -m mcp_agent_mail.cli serve-http --host 127.0.0.1 --port 13701
