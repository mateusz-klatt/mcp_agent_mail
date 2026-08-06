#!/usr/bin/env bash
# PostToolUse hook: file a short advisory reservation for a path this session
# just edited.
#
# Why this exists. The PreToolUse warning only helps when the OTHER agent was
# disciplined enough to reserve first. Two agents that both forget to reserve
# collide exactly as they would without any coordination layer at all, because
# reservations are opt-in at the LLM's discretion — the tool description asks
# politely and nothing enforces it. Auto-filing on edit converts reservations
# from a convention someone must remember into ambient truth: whatever a session
# is actually touching is, by construction, what it holds.
#
# The TTL is deliberately short. A reservation is a statement about current
# activity, not a claim on the future, and a session that crashes must not
# park a lock on a file for an hour. Repeat edits renew it.
#
# Cannot fail and cannot block: this runs after the edit has already happened,
# so the only thing an error here could achieve is breaking the session.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

TTL="${AGENT_MAIL_AUTORESERVE_TTL:-900}"

PROJECT="$(am_project_key)"
AGENT="$(am_agent_name)"
[ -z "$PROJECT" ] && exit 0

payload=""
if [ ! -t 0 ]; then
    payload="$(timeout 2 cat 2>/dev/null || true)"
fi
target="$(printf '%s' "$payload" \
    | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' 2>/dev/null)"
[ -z "$target" ] && exit 0

rel="$(am_relpath "$target")"
[ -z "$rel" ] && exit 0

token="$(am_cred_get "$PROJECT" "$AGENT")"
# No credential means SessionStart never ran or its identity was not recorded.
# Registering here would mint a SECOND identity for this host and split the
# mailbox, so do nothing instead.
[ -z "$token" ] && exit 0

resp="$(am_call file_reservation_paths "$(jq -nc \
    --arg p "$PROJECT" --arg a "$AGENT" --arg t "$token" --arg path "$rel" --argjson ttl "$TTL" \
    '{project_key:$p,agent_name:$a,registration_token:$t,paths:[$path],ttl_seconds:$ttl,reason:"auto: edited in session"}')")"
[ -z "$resp" ] && exit 0

# Only speak up if someone else already held it. Announcing every successful
# self-reservation would put a line in the model's context on every single edit,
# which is how a signal becomes noise and gets ignored.
conflicts="$(printf '%s' "$resp" | jq -r \
    '[.conflicts[]? | "\(.path_pattern // .path) held by \(.agent // .agent_name // "another agent")"] | join("; ")' 2>/dev/null)"
if [ -n "$conflicts" ] && [ "$conflicts" != "null" ]; then
    am_emit_context "PostToolUse" \
        "Agent Mail: you just edited a file reserved by someone else — ${conflicts}. Coordinate before continuing."
fi
exit 0
