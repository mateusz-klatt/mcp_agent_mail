#!/usr/bin/env bash
# SessionStart: establish this host's Agent Mail identity for whatever repository
# the session opened in, and report what other agents are currently holding.
#
# No per-repository setup: the project key comes from the repo's origin remote.
# A directory that is not a git repo, or has no origin, produces nothing at all
# rather than a guessed key that would quietly create a phantom project.
#
# Cannot fail — a SessionStart hook that errors is a session that does not start.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

am_read_payload
PROJECT="$(am_project_key)"
[ -z "$PROJECT" ] && exit 0
AGENT="$(am_agent_name)"

am_call ensure_project "$(jq -nc --arg k "$PROJECT" '{human_key:$k}')" >/dev/null 2>&1

token="$(am_cred_get "$PROJECT" "$AGENT")"
if [ -n "$token" ]; then
    # `name`, not `agent_name` — the latter is an unexpected-keyword validation
    # error that am_call swallows, which once made a re-registration test pass
    # while doing nothing at all.
    args="$(jq -nc --arg p "$PROJECT" --arg n "$AGENT" --arg t "$token" \
        '{project_key:$p,name:$n,registration_token:$t,program:"claude-code",model:"opus-5"}')"
else
    args="$(jq -nc --arg p "$PROJECT" --arg n "$AGENT" \
        '{project_key:$p,name:$n,program:"claude-code",model:"opus-5"}')"
fi

resp="$(am_call register_agent "$args")"
[ -z "$resp" ] && exit 0
got_name="$(printf '%s' "$resp" | jq -r '.name // empty' 2>/dev/null)"
got_token="$(printf '%s' "$resp" | jq -r '.registration_token // empty' 2>/dev/null)"
[ -z "$got_name" ] && exit 0

# Persist the name the server GRANTED, not the one requested: a name resembling
# a program or model is silently replaced, and recording the request would leave
# every later call authenticating as an agent that does not exist.
[ -n "$got_token" ] && am_cred_put "$PROJECT" "$got_name" "$got_token"

# An agent idle for a day is auto-retired, and re-registering does NOT clear
# that flag — the session would look fine while every message sent to it failed.
if [ "$(printf '%s' "$resp" | jq -r '.retired_at // empty' 2>/dev/null)" != "" ]; then
    am_call unretire_agent "$(jq -nc --arg p "$PROJECT" --arg n "$got_name" --arg t "${got_token:-$token}" \
        '{project_key:$p,agent_name:$n,registration_token:$t}')" >/dev/null 2>&1
fi

summary="Agent Mail: you are ${got_name} on ${PROJECT}."
res="$(am_get /mail/api/file-reservations --data "project=$(am_urlencode "$PROJECT")")"
held="$(printf '%s' "$res" | jq -r --arg me "$got_name" \
    '[.reservations[]? | select(.agent != $me) | "\(.path_pattern) (\(.agent))"] | join(", ")' 2>/dev/null)"
[ -n "$held" ] && summary="${summary} Reserved by others right now: ${held}."

am_emit_context "SessionStart" "$summary"
exit 0
