#!/usr/bin/env bash
# PostToolUse: file a short advisory reservation for the path just edited.
#
# Reservations are opt-in at the model's discretion — the tool description asks
# politely and nothing else happens. Two agents that both neglect to reserve
# collide exactly as they would with no coordination layer, and the PreToolUse
# warning only helps when the OTHER agent happened to be disciplined. Filing
# automatically makes what a session is touching become what it holds.
#
# Exactly ONE path per call, deliberately. file_reservation_paths appends to the
# agent's set and extends the expiry of anything re-sent, so re-sending the whole
# session's history on every edit would keep a file touched once in the first
# minute alive for as many hours as the session runs.
#
# Cannot fail: this runs after the edit, so an error here achieves nothing except
# breaking the session.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

TTL="${AGENT_MAIL_AUTORESERVE_TTL:-900}"

am_read_payload
AM_SESSION_ID="$(am_payload_field '.session_id')"
target="$(am_payload_field '.tool_input.file_path')"
[ -z "$target" ] && target="$(am_payload_field '.tool_input.notebook_path')"
[ -z "$target" ] && exit 0

PROJECT="$(am_project_key)"
[ -z "$PROJECT" ] && exit 0
AGENT="$(am_agent_name)"

rel="$(am_relpath "$target")"
[ -z "$rel" ] && exit 0

token="$(am_cred_get "$PROJECT" "$AGENT")"
# No credential means SessionStart never ran. Registering here would work, but
# doing it from a per-edit hook races every sibling session on the same host for
# the same name; leave identity establishment to SessionStart.
[ -z "$token" ] && exit 0

resp="$(am_call file_reservation_paths "$(jq -nc \
    --arg p "$PROJECT" --arg a "$AGENT" --arg t "$token" --arg path "$rel" --argjson ttl "$TTL" \
    '{project_key:$p,agent_name:$a,registration_token:$t,paths:[$path],ttl_seconds:$ttl,reason:"auto: edited in session"}')")"
[ -z "$resp" ] && exit 0

# Remember it so SessionEnd can release exactly this session's paths and leave a
# concurrent session's holds on the same identity untouched.
am_session_log_add "$PROJECT" "$rel"

# Speak only on conflict. Announcing every successful self-reservation would put
# a line in the model's context on each edit, which is how a signal becomes noise.
conflicts="$(printf '%s' "$resp" | jq -r \
    '[.conflicts[]? | "\(.path_pattern // .path) held by \(.agent // .agent_name // "another agent")"] | join("; ")' 2>/dev/null)"
if [ -n "$conflicts" ] && [ "$conflicts" != "null" ]; then
    am_emit_context "PostToolUse" \
        "Agent Mail: you just edited a file reserved by someone else — ${conflicts}. Coordinate before continuing."
fi
exit 0
