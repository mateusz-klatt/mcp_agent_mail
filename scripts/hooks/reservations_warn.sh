#!/usr/bin/env bash
# Warn when the file about to be edited is reserved by another agent.
# Claude Code PreToolUse hook for Edit / Write / NotebookEdit.
#
# CRITICAL PROPERTY: this script can never fail and can never hang. A PreToolUse
# hook that exits non-zero BLOCKS the tool call, so an advisory hook would
# otherwise make the repository uneditable the moment the server is down, the
# token is wrong, or the network stalls. Every path here ends in `exit 0`, and
# every network call carries a short timeout.
#
# It queries the running server over HTTP rather than shelling out to the CLI.
# That is not a style preference: `mcp_agent_mail.cli` imports the entire
# application on startup and measured ~24 SECONDS per invocation in this
# deployment — unusable on a per-edit hook. The HTTP round trip is ~40ms.
#
# It asks about the SPECIFIC path being edited, not "are there any reservations
# at all". A hook that fires on every unrelated reservation is noise, and noise
# gets ignored, which defeats the purpose.
#
# Config (environment, else the project's .env):
#   AGENT_MAIL_URL          server base URL      (default http://127.0.0.1:8765)
#   AGENT_MAIL_TOKEN        bearer token         (default HTTP_BEARER_TOKEN from .env)
#   AGENT_MAIL_PROJECT_KEY  project human_key    (default the project directory)

set -uo pipefail

# No hard-coded default: this repository is public and must not name a checkout.
# Note the nested :- on CLAUDE_PROJECT_DIR. Under `set -u` a bare
# ${A:-$B} still aborts when B is unset, and an aborting PreToolUse hook BLOCKS
# every edit — the precise failure this script exists to avoid.
PROJECT_DIR="${AGENT_MAIL_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}"
[ -z "$PROJECT_DIR" ] && exit 0
PROJECT_KEY="${AGENT_MAIL_PROJECT_KEY:-$PROJECT_DIR}"
BASE_URL="${AGENT_MAIL_URL:-http://127.0.0.1:8765}"
TIMEOUT="${AGENT_MAIL_HOOK_TIMEOUT:-3}"

# Claude Code feeds the hook the tool invocation as JSON on stdin. Read it
# non-blockingly: if nothing is piped in, -t 0 keeps us from hanging forever
# waiting on a terminal.
payload=""
if [ ! -t 0 ]; then
    payload="$(timeout 2 cat 2>/dev/null || true)"
fi

# Pull out the target path without requiring jq (a missing optional dependency
# must not turn into a broken hook). Accepts file_path or notebook_path.
target="$(printf '%s' "$payload" \
    | grep -oE '"(file_path|notebook_path)"[[:space:]]*:[[:space:]]*"[^"]+"' \
    | head -1 | sed -E 's/.*:[[:space:]]*"([^"]+)".*/\1/')"
[ -z "$target" ] && exit 0

# Reservations are recorded relative to the project root.
rel="${target#"${PROJECT_DIR}"/}"
rel="${rel#./}"

token="${AGENT_MAIL_TOKEN:-}"
if [ -z "$token" ] && [ -r "${PROJECT_DIR}/.env" ]; then
    token="$(grep -m1 '^HTTP_BEARER_TOKEN=' "${PROJECT_DIR}/.env" 2>/dev/null | cut -d= -f2-)"
fi
[ -z "$token" ] && exit 0   # not configured -> silently do nothing

urlenc() { printf '%s' "$1" | sed -e 's/%/%25/g' -e 's|/|%2F|g' -e 's/ /%20/g' -e 's/&/%26/g' -e 's/?/%3F/g' -e 's/=/%3D/g' -e 's/+/%2B/g'; }

body="$(curl -s --max-time "$TIMEOUT" -H "Authorization: Bearer ${token}" \
        "${BASE_URL}/mail/api/file-reservations?project=$(urlenc "$PROJECT_KEY")&path=$(urlenc "$rel")" \
        2>/dev/null)" || exit 0
[ -z "$body" ] && exit 0

active="$(printf '%s' "$body" | grep -oE '"active"[[:space:]]*:[[:space:]]*[0-9]+' \
          | grep -oE '[0-9]+$' | head -1)"
[ -z "$active" ] && exit 0
[ "$active" -eq 0 ] 2>/dev/null && exit 0

holder="$(printf '%s' "$body" | grep -oE '"agent"[[:space:]]*:[[:space:]]*"[^"]*"' \
          | head -1 | sed -E 's/.*"([^"]*)"$/\1/')"
reason="$(printf '%s' "$body" | grep -oE '"reason"[[:space:]]*:[[:space:]]*"[^"]*"' \
          | head -1 | sed -E 's/.*"([^"]*)"$/\1/')"

warning="$(printf 'Agent Mail: %s is reserved by %s%s. Coordinate before editing — see %s/mail' \
    "$rel" "${holder:-another agent}" "${reason:+ (${reason})}" \
    "${AGENT_MAIL_PUBLIC_URL:-$BASE_URL}")"

# The warning MUST leave through the hookSpecificOutput envelope. Plain stdout
# from a hook does not reach the model — it lands in the human's terminal (or,
# for PreToolUse, only the debug log), which means a warning printed the obvious
# way is delivered to nobody. `additionalContext` is the one channel that
# surfaces in the agent's system reminder. Upstream's own scripts/hooks/
# check_inbox.sh documents the same trap in its header.
#
# Note this is advisory, not `permissionDecision: "deny"`. Blocking would turn a
# server outage into an uneditable repository, and the reservations are advisory
# by design. Denial is worth revisiting only if agents are observed ignoring the
# warning, and then only for confirmed-active exclusive conflicts.
if escaped="$(printf '%s' "$warning" \
        | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null)" \
   && [ -n "$escaped" ]; then
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":%s}}\n' "$escaped"
else
    # No python3, or escaping failed. Emitting unescaped text here would produce
    # malformed JSON, which is worse than silence: stay quiet rather than risk
    # the harness rejecting the hook output.
    printf '%s\n' "$warning"
fi
exit 0
