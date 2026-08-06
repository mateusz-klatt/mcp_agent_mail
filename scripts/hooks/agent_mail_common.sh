# Shared configuration and MCP plumbing for the Agent Mail hooks.
# Sourced, never executed. Every function here is written so that a failure
# degrades to "do nothing" rather than to a broken editing session.
#
# Config resolution order: environment, then the project's .env, then a default.
#   AGENT_MAIL_URL          server base URL (default http://127.0.0.1:8765)
#   AGENT_MAIL_TOKEN        bearer token (default HTTP_BEARER_TOKEN from .env)
#   AGENT_MAIL_PROJECT_KEY  the canonical project key — MUST be byte-identical
#                           on every host, see docs/multi-host-project-identity.md
#   AGENT_MAIL_AGENT        this host's stable agent identity
#   AGENT_MAIL_PROJECT_DIR  local checkout, used to find .env and to relativise paths
#
# Calls go to the STATELESS mount, not /mcp. A one-shot JSON-RPC POST needs no
# initialize/notifications handshake and returns in ~40ms; the stateful mount
# would cost three round trips per hook and leak a session per invocation.

AM_TIMEOUT="${AGENT_MAIL_HOOK_TIMEOUT:-3}"
AM_BASE_URL="${AGENT_MAIL_URL:-http://127.0.0.1:8765}"
AM_PROJECT_DIR="${AGENT_MAIL_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}"

# Credentials are keyed by (project, agent) and are DURABLE, not per-session:
# re-registering an existing identity requires the registration token that the
# first registration returned, so losing this file means losing the identity.
AM_STATE_DIR="${AGENT_MAIL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-mail}"
AM_CRED_FILE="${AM_STATE_DIR}/credentials.json"

am_bearer() {
    if [ -n "${AGENT_MAIL_TOKEN:-}" ]; then
        printf '%s' "$AGENT_MAIL_TOKEN"
        return
    fi
    [ -n "$AM_PROJECT_DIR" ] && [ -r "${AM_PROJECT_DIR}/.env" ] \
        && grep -m1 '^HTTP_BEARER_TOKEN=' "${AM_PROJECT_DIR}/.env" 2>/dev/null | cut -d= -f2-
}

am_project_key() {
    printf '%s' "${AGENT_MAIL_PROJECT_KEY:-}"
}

# Default identity is derived from the hostname so a given machine keeps the same
# mailbox across sessions. Two constraints the server enforces silently:
#   * the name must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}, and
#   * it must not LOOK like a program or model name — `_looks_like_program_name`
#     rejects e.g. "claude-<host>", and the default enforcement mode is "coerce",
#     which quietly substitutes a random name instead of raising. A host prefix
#     with a numeric suffix avoids both traps.
am_agent_name() {
    if [ -n "${AGENT_MAIL_AGENT:-}" ]; then
        printf '%s' "$AGENT_MAIL_AGENT"
        return
    fi
    local h
    h="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo host)"
    h="$(printf '%s' "$h" | tr -cd '[:alnum:]._-' | cut -c1-100)"
    [ -z "$h" ] && h="host"
    printf '%s-1' "$h"
}

# One-shot MCP tool call against the stateless mount.
# Usage: am_call <tool_name> <arguments_json>
# Prints the tool's text payload on success, nothing on any failure. Never
# returns non-zero in a way that could propagate out of a hook.
am_call() {
    local tool="$1" args="$2" bearer body
    bearer="$(am_bearer)"
    [ -z "$bearer" ] && return 0
    body="$(jq -nc --arg t "$tool" --argjson a "$args" \
        '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:$t,arguments:$a}}' 2>/dev/null)" || return 0
    curl -s --max-time "$AM_TIMEOUT" -X POST "${AM_BASE_URL}/api/" \
        -H "Authorization: Bearer ${bearer}" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        --data "$body" 2>/dev/null \
        | jq -r '.result.content[0].text // empty' 2>/dev/null
}

am_cred_get() {
    local project="$1" agent="$2"
    [ -r "$AM_CRED_FILE" ] || return 0
    jq -r --arg p "$project" --arg a "$agent" \
        '.[$p][$a] // empty' "$AM_CRED_FILE" 2>/dev/null
}

am_cred_put() {
    local project="$1" agent="$2" token="$3" tmp
    [ -z "$token" ] && return 0
    mkdir -p "$AM_STATE_DIR" 2>/dev/null || return 0
    chmod 700 "$AM_STATE_DIR" 2>/dev/null
    tmp="${AM_CRED_FILE}.$$.tmp"
    # Read-modify-write through a temp file plus rename: two hooks firing at once
    # must not leave a truncated credential store, which would orphan the agent
    # identity permanently (the token cannot be re-read from the server).
    if [ -r "$AM_CRED_FILE" ]; then
        jq --arg p "$project" --arg a "$agent" --arg t "$token" \
            '.[$p] = ((.[$p] // {}) | .[$a] = $t)' "$AM_CRED_FILE" >"$tmp" 2>/dev/null || { rm -f "$tmp"; return 0; }
    else
        jq -n --arg p "$project" --arg a "$agent" --arg t "$token" \
            '{($p): {($a): $t}}' >"$tmp" 2>/dev/null || { rm -f "$tmp"; return 0; }
    fi
    chmod 600 "$tmp" 2>/dev/null
    mv -f "$tmp" "$AM_CRED_FILE" 2>/dev/null || rm -f "$tmp"
}

# Emit a Claude Code hook envelope. Plain stdout does not reach the model —
# additionalContext is the only channel that surfaces in its system reminder.
am_emit_context() {
    local event="$1" text="$2" escaped
    [ -z "$text" ] && return 0
    if escaped="$(printf '%s' "$text" | jq -Rs . 2>/dev/null)" && [ -n "$escaped" ]; then
        printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":%s}}\n' "$event" "$escaped"
    fi
}

# Path as the server records it: relative to the project checkout.
am_relpath() {
    local p="$1"
    [ -n "$AM_PROJECT_DIR" ] && p="${p#"${AM_PROJECT_DIR}"/}"
    printf '%s' "${p#./}"
}
