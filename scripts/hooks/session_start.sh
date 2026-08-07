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
rc=$?
# Registration is what every later hook authenticates with, so a session that
# fails here has no working coordination at all — no reservations filed, no
# conflicts seen, no mail delivered. Today that session starts in silence and
# looks identical to a healthy one. Say it once, at the only moment the agent
# can act on it.
if [ "$rc" -ne 0 ]; then
    why="$(am_failure_reason "$rc" "$resp")"
    am_emit_context "SessionStart" \
        "Agent Mail: could not register on ${PROJECT} — ${why}. For this session there is no coordination: reservations will not be filed, conflicts will not be reported, and mail will not be delivered. Nothing else will mention it again."
    exit 0
fi
[ -z "$resp" ] && exit 0
got_name="$(printf '%s' "$resp" | jq -r '.name // empty' 2>/dev/null)"
got_token="$(printf '%s' "$resp" | jq -r '.registration_token // empty' 2>/dev/null)"
# A response with no name in it is not an empty response — that case exited
# above — and it is not a failure either: the status was 2xx, the envelope was
# well formed, the tool did not refuse. The server answered something other
# than what register_agent returns, which is what `MCP_AGENT_MAIL_OUTPUT_FORMAT`
# or `TOON_DEFAULT_FORMAT` does: every field is replaced by {format, data,
# meta}. Exiting quietly here leaves a machine that looks configured, registers
# "successfully" every session, and has no coordination at all.
#
# The exit-code contract cannot catch this. It answers "did the server reply",
# and here the server replied; the question is whether the reply contains what
# this hook reads.
if [ -z "$got_name" ]; then
    am_emit_context "SessionStart" \
        "Agent Mail: could not read an agent name from the registration response. The server answered, but not with what register_agent returns — check MCP_AGENT_MAIL_OUTPUT_FORMAT and TOON_DEFAULT_FORMAT on the server, which replace every field with a {format, data, meta} envelope. Until this is fixed there is no coordination in this session: no reservations, no conflict warnings, no mail. The registration itself may well have succeeded: the identity can now exist on the server while no token exists here, and fixing the setting does not undo that — re-registering the same name will then be refused for want of the token it never returned."
    exit 0
fi

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
if [ $? -ne 0 ]; then
    # Registration worked, so this is a blip rather than an outage — but an
    # empty holdings list reads as "nobody is holding anything", which is a
    # claim, not an absence of one.
    summary="${summary} Could not read what others hold right now — assume nothing about who is editing what."
else
    held="$(printf '%s' "$res" | jq -r --arg me "$got_name" \
        '[.reservations[]? | select(.agent != $me) | "\(.path_pattern) (\(.agent))"] | join(", ")' 2>/dev/null)"
    [ -n "$held" ] && summary="${summary} Reserved by others right now: ${held}."
fi

# Ask for the watcher HERE, not after a wake.
#
# Measured across four machines: one runs the watcher, two run a polling loop,
# one is driven entirely by a person typing "check your mail". What decides is
# not which mechanism works — nobody reported a fault in any of them — but how
# much each asks the agent to remember. The watcher asks for a re-arm after
# every wake, and that request arrives at the exact moment the agent has just
# been handed mail and wants to answer it, so it loses to the thing it
# interrupts.
#
# Session start is the one moment the request competes with nothing: there is no
# task in flight yet, and this hook is already speaking. It cannot start the
# watcher itself — a process spawned from a hook is not a task the agent's
# runtime tracks, so its exit would wake nobody, and the exit IS the wake.
watcher="$(dirname "$0")/inbox_watch.sh"
if [ -x "$watcher" ]; then
    summary="${summary}

For instant delivery instead of polling, run this ONCE in the background now — it
exits when mail arrives, and a background task that exits wakes you:
    bash $(cd "$(dirname "$watcher")" && pwd)/$(basename "$watcher")"
fi

am_emit_context "SessionStart" "$summary"
exit 0
