#!/usr/bin/env bash
# SessionStart hook: establish this host's Agent Mail identity and report the
# coordination state the session is starting into.
#
# Identity is DURABLE, not per-session. Registering a name the first time
# returns a registration_token; every later registration of that same name
# REQUIRES that token back ("register_agent for an existing identity requires
# registration_token"). The token is therefore cached under
# $XDG_STATE_HOME/agent-mail/credentials.json — lose it and the identity is
# orphaned, because the server will not re-issue it.
#
# Cannot fail: a SessionStart hook that errors is a session that does not start.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

PROJECT="$(am_project_key)"
AGENT="$(am_agent_name)"
[ -z "$PROJECT" ] && exit 0   # unconfigured -> stay silent rather than guess a key

# The project row must exist before an agent can join it.
am_call ensure_project "$(jq -nc --arg k "$PROJECT" '{human_key:$k}')" >/dev/null 2>&1

token="$(am_cred_get "$PROJECT" "$AGENT")"
if [ -n "$token" ]; then
    args="$(jq -nc --arg p "$PROJECT" --arg n "$AGENT" --arg t "$token" \
        '{project_key:$p,agent_name:$n,name:$n,registration_token:$t,program:"claude-code",model:"opus-5"}')"
else
    args="$(jq -nc --arg p "$PROJECT" --arg n "$AGENT" \
        '{project_key:$p,name:$n,program:"claude-code",model:"opus-5"}')"
fi

resp="$(am_call register_agent "$args")"
[ -z "$resp" ] && exit 0

got_name="$(printf '%s' "$resp" | jq -r '.name // empty' 2>/dev/null)"
got_token="$(printf '%s' "$resp" | jq -r '.registration_token // empty' 2>/dev/null)"
[ -z "$got_name" ] && exit 0   # an error string, not a profile — nothing to record

# Persist under the name the server actually assigned. Asking for "foo" can
# silently yield something else: the default enforcement mode is "coerce", so a
# name that trips the program/model-lookalike check is replaced rather than
# rejected. Recording the requested name instead of the granted one would leave
# every later call authenticating as an agent that does not exist.
[ -n "$got_token" ] && am_cred_put "$PROJECT" "$got_name" "$got_token"

# Report what else is holding resources right now, so the session plans around
# it instead of discovering the conflict at its first edit.
bearer="$(am_bearer)"
summary="Agent Mail: registered as ${got_name} on project ${PROJECT}."
if [ -n "$bearer" ]; then
    res="$(curl -s --max-time "$AM_TIMEOUT" -G \
        -H "Authorization: Bearer ${bearer}" \
        --data-urlencode "project=${PROJECT}" \
        "${AM_BASE_URL}/mail/api/file-reservations" 2>/dev/null)"
    active="$(printf '%s' "$res" | jq -r '.active // 0' 2>/dev/null)"
    if [ -n "$active" ] && [ "$active" -gt 0 ] 2>/dev/null; then
        held="$(printf '%s' "$res" | jq -r \
            '[.reservations[] | select(.agent != "'"$got_name"'") | "\(.path_pattern) (\(.agent))"] | join(", ")' 2>/dev/null)"
        [ -n "$held" ] && summary="${summary} Files currently reserved by others: ${held}."
    fi
fi

am_emit_context "SessionStart" "$summary"
exit 0
