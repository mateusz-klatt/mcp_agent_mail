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

# Project of the FILE, not of the working directory — see am_relpath.
PROJECT="$(am_project_key_for_file "$target")"
# The identity must come from the SAME project the credential is looked up
# under, and that is this file's project — not the session's. The reservation
# lives where the edited file lives, so the token that files it has to be the one
# issued there; a name resolved against the session's project produces a pair
# that matches only by accident.
#
# By accident is exactly what it is today: with no remembered name yet,
# am_agent_name falls through to derivation, which does not depend on the project
# at all — measured, three different project keys, one name. So this is
# preventive, not corrective: the mismatch appears at the first session restart,
# when granted/ starts holding per-project names, and would then be a silent
# exit rather than an error.
#
# Deferred earlier on the grounds that "session identity" and "file identity"
# were both defensible. They are not: home-wsl-1 measured that one of them files
# reservations and the other loses them.
export AM_PROJECT_FOR_NAME="$PROJECT"
[ -z "$PROJECT" ] && exit 0
am_project_is_active "$PROJECT" claude "${AGENT_MAIL_CLAUDE_SLOT:-1}" \
    "$(dirname "$(am_norm_path "$target")")" || exit 0
if migration_pair="$(am_identity_migration_pair "$PROJECT" claude "${AGENT_MAIL_CLAUDE_SLOT:-1}")"; then
    legacy_agent="${migration_pair%%$'\t'*}"
    client_agent="${migration_pair#*$'\t'}"
    am_emit_context "PostToolUse" \
        "$(am_identity_migration_message "$legacy_agent" "$client_agent")"
    exit 0
fi
AGENT="$(am_agent_name claude "${AGENT_MAIL_CLAUDE_SLOT:-1}")"

rel="$(am_relpath "$target")"
[ -z "$rel" ] && exit 0

token="$(am_cred_get "$PROJECT" "$AGENT")"
# No credential means SessionStart never ran. Registering here would work, but
# doing it from a per-edit hook races every sibling session on the same host for
# the same name; leave identity establishment to SessionStart.
[ -z "$token" ] && exit 0

resp="$(am_call file_reservation_paths "$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" \
    jq -nc --arg p "$PROJECT" --arg a "$AGENT" --arg path "$rel" --argjson ttl "$TTL" \
    '{project_key:$p,agent_name:$a,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,paths:[$path],ttl_seconds:$ttl,reason:"auto: edited in session"}')")"
rc=$?
# Silence here reads as "reservation filed" — the hook only speaks on conflict,
# so saying nothing is how success looks. When the server is unreachable that
# makes this the more dangerous half of a pair: reservations_warn reports no
# conflict at the same moment, so two agents editing one file are each assured
# they are alone. Announce the failure even though the ordinary path stays
# quiet, because this is the one case where quiet is a false statement.
if [ "$rc" -ne 0 ]; then
    why="$(am_failure_reason "$rc" "$resp")"
    am_emit_context "PostToolUse" \
        "Agent Mail: NO reservation was filed for ${rel} — ${why}. Nobody else will be warned that you are editing it, and you were not warned about them. Treat this file as unguarded until coordination is back."
    exit 0
fi
[ -z "$resp" ] && exit 0

# Remember it so SessionEnd can release exactly this session's paths and leave a
# concurrent session's holds on the same identity untouched.
am_session_log_add "$PROJECT" "$rel"

# Speak only on conflict. Announcing every successful self-reservation would put
# a line in the model's context on each edit, which is how a signal becomes noise.
# The holder is nested: the server returns {path, holders:[{agent,…}]}
# (app.py:11264). Reading .agent at the top level yielded null and fell back to
# "another agent" — so in the one moment a conflict is real, the message did not
# say who to talk to, which is the only thing it exists to convey.
conflicts="$(printf '%s' "$resp" | jq -r \
    '[.conflicts[]? as $c | ($c.holders[]? // {}) | "\($c.path) held by \(.agent // "another agent")"] | unique | join("; ")' 2>/dev/null)"
if [ -n "$conflicts" ] && [ "$conflicts" != "null" ]; then
    am_emit_context "PostToolUse" \
        "Agent Mail: you just edited a file reserved by someone else — ${conflicts}. Coordinate before continuing."
fi
exit 0
