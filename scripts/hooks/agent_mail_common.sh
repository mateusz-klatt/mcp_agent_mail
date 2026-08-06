# Shared plumbing for the Agent Mail hooks. Sourced, never executed.
#
# Designed to be installed ONCE PER MACHINE in ~/.claude/settings.json and work
# in every repository without per-project setup. Nothing here is configured per
# checkout: the project key is derived from the repository itself, so the same
# repo yields the same key on Linux, WSL, native Windows and macOS.
#
# Optional environment overrides:
#   AGENT_MAIL_URL          server base URL (default http://127.0.0.1:8765)
#   AGENT_MAIL_TOKEN        bearer token (default: HTTP_BEARER_TOKEN from ~/.agent-mail.env)
#   AGENT_MAIL_PROJECT_KEY  force a project key instead of deriving one
#   AGENT_MAIL_AGENT        force an identity instead of deriving one
#
# Every function degrades to "do nothing" on failure. A hook that errors is a
# hook that blocks an edit.

AM_TIMEOUT="${AGENT_MAIL_HOOK_TIMEOUT:-3}"
AM_BASE_URL="${AGENT_MAIL_URL:-http://127.0.0.1:8765}"
AM_STATE_DIR="${AGENT_MAIL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-mail}"
AM_CRED_FILE="${AM_STATE_DIR}/credentials.json"

# --- payload -----------------------------------------------------------------
# Claude Code delivers the hook invocation as JSON on stdin and closes the pipe.
# `read -d ''` consumes to EOF and is bounded by -t, which matters because GNU
# `timeout` does not exist on a stock macOS and `timeout 2 cat` would simply
# fail there — a hook that silently never runs on one of the machines.
am_read_payload() {
    AM_PAYLOAD=""
    [ -t 0 ] && return 0
    IFS= read -r -d '' -t "$AM_TIMEOUT" AM_PAYLOAD 2>/dev/null || true
    return 0
}

am_payload_field() {
    printf '%s' "${AM_PAYLOAD:-}" | jq -r "$1 // empty" 2>/dev/null
}

# --- project identity --------------------------------------------------------
# The key is derived from the repository's origin remote, normalised to an
# absolute-looking "/owner/repo".
#
# Two constraints force exactly this shape:
#   * ensure_project rejects anything Path().is_absolute() says no to, so a bare
#     remote URL cannot be used;
#   * the key must be byte-identical across hosts, so a checkout path cannot be
#     used — /home/me/app, /Users/me/dev/app and C:\src\app are three projects
#     with three mailboxes and no warning that they were ever meant to be one.
#
# The server has this same normalisation (PROJECT_IDENTITY_MODE=git-remote) but
# resolves the repo on the SERVER, which a container does not have — so it
# silently falls back to the checkout path. Doing it client-side is the same
# idea placed on the side of the network that can actually see the repository.
am_project_key() {
    if [ -n "${AGENT_MAIL_PROJECT_KEY:-}" ]; then
        printf '%s' "$AGENT_MAIL_PROJECT_KEY"
        return 0
    fi
    local url
    url="$(git rev-parse --is-inside-work-tree >/dev/null 2>&1 && git remote get-url origin 2>/dev/null)" || return 0
    [ -z "$url" ] && return 0   # no remote -> stay silent rather than invent a key
    printf '%s' "$url" \
        | sed -E 's#^[a-zA-Z]+://[^/]+/#/#; s#^[^@]+@[^:]+:#/#; s#\.git$##; s#/+$##' \
        | tr -d '\n'
}

# Repo root of the file being edited, so a worktree reserves paths comparable
# with every other checkout. Relativising against a fixed project directory
# would make a worktree outside it reserve an ABSOLUTE path that can never
# string-match anyone else's reservation — conflict detection would silently
# never fire in exactly the fan-out case it exists for.
am_relpath() {
    local p="$1" root
    root="$(git -C "$(dirname "$p")" rev-parse --show-toplevel 2>/dev/null)" || root=""
    [ -n "$root" ] && p="${p#"${root}"/}"
    printf '%s' "${p#./}"
}

# --- agent identity ----------------------------------------------------------
# Durable per (host, project). Concurrent sessions on one host share it, which
# is safe: file_reservation_paths APPENDS to an agent's set rather than
# replacing it, so siblings accumulate rather than erase. SessionEnd releases by
# path (not wholesale) so one session's exit cannot drop another's holds.
am_agent_name() {
    if [ -n "${AGENT_MAIL_AGENT:-}" ]; then
        printf '%s' "$AGENT_MAIL_AGENT"
        return 0
    fi
    local h
    h="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo host)"
    h="$(printf '%s' "$h" | tr -cd '[:alnum:]._-' | cut -c1-64)"
    [ -z "$h" ] && h="host"
    # WSL reports the Windows machine name, so a WSL and a native-Windows CLI on
    # one box would otherwise claim the same identity and the same mailbox.
    if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/version 2>/dev/null; then
        h="${h}wsl"
    fi
    # The server rejects names resembling a program or model — and does it
    # SILENTLY, substituting a random one (enforcement mode defaults to
    # "coerce"). `_looks_like_model_name` matches on substring, so a hostname
    # containing e.g. "opus" or "gemini-" would be coerced without a word.
    printf '%s-1' "$h"
}

am_bearer() {
    if [ -n "${AGENT_MAIL_TOKEN:-}" ]; then
        printf '%s' "$AGENT_MAIL_TOKEN"
        return 0
    fi
    [ -r "$HOME/.agent-mail.env" ] \
        && grep -m1 '^HTTP_BEARER_TOKEN=' "$HOME/.agent-mail.env" 2>/dev/null | cut -d= -f2-
}

# --- server calls ------------------------------------------------------------
# The STATELESS mount: a one-shot JSON-RPC POST needs no initialize handshake
# and returns in ~40ms, where the stateful /mcp mount would cost three round
# trips and leak a session per hook invocation.
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

am_get() {
    local path="$1" bearer
    bearer="$(am_bearer)"
    [ -z "$bearer" ] && return 0
    curl -s --max-time "$AM_TIMEOUT" -G -H "Authorization: Bearer ${bearer}" \
        "${@:2}" "${AM_BASE_URL}${path}" 2>/dev/null
}

# --- credential store --------------------------------------------------------
# Re-registering an existing name REQUIRES the token the first registration
# returned; the server will not reissue it. Losing this file orphans the
# identity permanently, hence the atomic write.
am_cred_get() {
    [ -r "$AM_CRED_FILE" ] || return 0
    jq -r --arg p "$1" --arg a "$2" '.[$p][$a] // empty' "$AM_CRED_FILE" 2>/dev/null
}

am_cred_put() {
    local project="$1" agent="$2" token="$3" tmp
    [ -z "$token" ] && return 0
    mkdir -p "$AM_STATE_DIR" 2>/dev/null || return 0
    chmod 700 "$AM_STATE_DIR" 2>/dev/null
    tmp="${AM_CRED_FILE}.$$.tmp"
    if [ -r "$AM_CRED_FILE" ]; then
        jq --arg p "$project" --arg a "$agent" --arg t "$token" \
            '.[$p] = ((.[$p] // {}) | .[$a] = $t)' "$AM_CRED_FILE" >"$tmp" 2>/dev/null \
            || { rm -f "$tmp"; return 0; }
    else
        jq -n --arg p "$project" --arg a "$agent" --arg t "$token" \
            '{($p): {($a): $t}}' >"$tmp" 2>/dev/null || { rm -f "$tmp"; return 0; }
    fi
    chmod 600 "$tmp" 2>/dev/null
    mv -f "$tmp" "$AM_CRED_FILE" 2>/dev/null || rm -f "$tmp"
}

# --- per-session path log ----------------------------------------------------
# Records what THIS session reserved, so SessionEnd can release exactly those
# paths and leave a sibling session's holds alone. Keyed by session_id, so a new
# session starts empty without anything having to reset it — which matters
# because SessionStart re-fires on resume and compaction for a session that is
# still very much alive.
am_session_log() {
    local key
    key="$(printf '%s|%s' "$1" "${AM_SESSION_ID:-nosession}" | tr -cd '[:alnum:]._|-' | tr '|' '_')"
    printf '%s/sessions/%s.list' "$AM_STATE_DIR" "$key"
}

am_session_log_add() {
    local f; f="$(am_session_log "$1")"
    mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
    printf '%s\n' "$2" >>"$f" 2>/dev/null
    return 0
}

# --- output ------------------------------------------------------------------
# Plain stdout from a hook does not reach the model; additionalContext is the
# only channel that surfaces in its system reminder.
am_emit_context() {
    local event="$1" text="$2" escaped
    [ -z "$text" ] && return 0
    if escaped="$(printf '%s' "$text" | jq -Rs . 2>/dev/null)" && [ -n "$escaped" ]; then
        printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":%s}}\n' "$event" "$escaped"
    fi
}
