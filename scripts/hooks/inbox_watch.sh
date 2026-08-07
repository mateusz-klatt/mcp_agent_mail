#!/usr/bin/env bash
# Block until mail arrives for this agent, then exit.
#
# Not a hook. Run this as a BACKGROUND command from the agent's own session —
# in Claude Code, a background Bash task that exits re-invokes the agent, so
# this script exiting *is* the wake. Nothing else needs to poll, and no monitor
# runtime is required.
#
#     ./scripts/hooks/inbox_watch.sh        (run in background)
#
# Order matters and is the whole reason this is not one curl in a command
# substitution: subscribe, wait for `: ready`, and only THEN read the mailbox.
# Reading first leaves a window — a message arriving after the read and before
# the subscription exists produces no hint and wakes nobody, so it waits until
# something else happens to arrive. Buffering curl's whole output, as this
# script used to, cannot see `: ready` until the connection is over, which is
# too late to catch up under its protection.
#
# Two secrets cross into curl here — the server bearer and this agent's
# registration token — so both go through `-K -` on stdin. Passing either as
# `-H` would put it in argv, where any process on the machine can read it from
# the process table for the life of the call.
#
# Cannot fail: a watcher that breaks a session is worse than a late message.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

# How long to wait before giving up and exiting anyway. Exiting wakes the agent
# for nothing, so this trades a cheap no-op turn against noticing sooner that
# the subscription died in a way TCP never reported. Keep it under the server's
# own cap (AGENT_MAIL_EVENTS_MAX_SECONDS, 3600 by default) so the client is the
# one that decides when to reconnect.
WATCH="${AGENT_MAIL_WATCH_SECONDS:-1800}"
# The exact command to run again, carried in every message this script prints.
#
# Re-arming is the whole reason instant delivery went unused: the polling loop
# asks nothing of anybody, while this asks the agent to remember a command at
# the precise moment it has just been handed mail and wants to act on it. It
# cannot re-arm itself — the wake IS this process exiting, and a successor it
# spawned would not be a task the agent's runtime tracks, so that successor's
# exit would wake nobody. What can be removed is the recall: print the command,
# not a reminder that one exists.
# A bare path, deliberately, and NOT `bash <path>`.
#
# `bash <path>` looked like the portable choice: no per-platform branch, works
# on Linux and macOS. It is wrong on Windows, where `bash` on the PATH is the
# WSL launcher rather than Git Bash — so it starts a different operating system,
# which cannot see the drive the path refers to, and answers "No such file or
# directory". That is BYTE-IDENTICAL to what a genuinely missing hook prints,
# so it turns a working installation into what reads as a broken one.
#
# A bare path is executable wherever this is meant to be run from — the exec bit
# and shebang cover Linux, macOS and Git Bash alike. It fails only if handed to
# cmd.exe, which is not the shell any of this is invoked from.
#
# The lesson is not about bash. It is that "one form for every platform" was
# chosen to avoid duplicating platform knowledge that `am_platform` already
# holds — and avoiding a second copy of that knowledge is not the same as being
# allowed to ignore it.
SELF_CMD="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
# How long to wait for `: ready` before giving up on the subscription.
READY_WAIT="${AGENT_MAIL_WATCH_READY_SECONDS:-15}"

PROJECT="$(am_project_key)"
# am_agent_name resolves the project itself when AM_PROJECT_FOR_NAME is unset,
# which means a second `git config` and `git rev-parse` for a value this hook
# has already computed one line above. The override exists for exactly this and
# nothing set it. Measured by home-win-1: am_project_key costs ~122 ms per call
# on Windows, so this is a doubled cost on every tool invocation there.
export AM_PROJECT_FOR_NAME="$PROJECT"
[ -z "$PROJECT" ] && exit 0
AGENT="$(am_agent_name)"

token="$(am_cred_get "$PROJECT" "$AGENT")"
# No credential means SessionStart never ran, so there is no identity to
# subscribe as. Silence is right: the next SessionStart registers and the mail
# is picked up there.
[ -z "$token" ] && exit 0
bearer="$(am_bearer)"
[ -z "$bearer" ] && exit 0

# Keyed by PROCESS as well as identity. Two sessions on one host share an agent
# name, so a slug of (project, agent) alone means a second watcher truncates the
# first one's stream mid-read and its exit trap deletes a file the first is
# still using.
# Same mapping as inbox_check.sh — see the note there. Milder here because the
# pid keeps concurrent watchers apart regardless, but two projects would still
# share a directory of stream state, and "milder" is how the other three
# instances survived this long.
slug="$(printf '%s|%s|%s' "$PROJECT" "$AGENT" "$$" | tr '/' '_' | tr -cd '[:alnum:]._|-' | tr '|' '_' | cut -c1-96)"
stream="${AM_STATE_DIR}/watch/${slug}.stream"
mkdir -p "$(dirname "$stream")" 2>/dev/null || exit 0

CURL_PID=""
cleanup() {
    [ -n "$CURL_PID" ] && kill "$CURL_PID" 2>/dev/null
    rm -f "$stream" 2>/dev/null
}
trap cleanup EXIT INT TERM

url="${AM_BASE_URL}/events?project=$(am_urlencode "$PROJECT")&agent=$(am_urlencode "$AGENT")"

# Streamed to a file rather than captured, so `: ready` is readable while the
# connection is still open. --no-buffer, or curl holds the frame in its own
# output buffer and the wake arrives whenever that happens to flush.
: > "$stream"
started="$(date +%s 2>/dev/null || echo 0)"
printf 'header = "Authorization: Bearer %s"\nheader = "X-Agent-Mail-Registration-Token: %s"\nheader = "Accept: text/event-stream"\n' \
        "$bearer" "$token" \
    | curl -sN --no-buffer --max-time "$WATCH" -K - "$url" >>"$stream" 2>/dev/null &
CURL_PID=$!

# ── wait for the subscription to be live ─────────────────────────────────────
ready=0
ready_deadline=$(( started + READY_WAIT ))
while :; do
    grep -q '^: ready' "$stream" 2>/dev/null && { ready=1; break; }
    kill -0 "$CURL_PID" 2>/dev/null || break      # curl died before subscribing
    [ "$(date +%s 2>/dev/null || echo 0)" -ge "$ready_deadline" ] 2>/dev/null && break
    sleep 0.2
done

if [ "$ready" -ne 1 ]; then
    # Kill first, THEN reap. A connection that opened but never said `: ready`
    # leaves curl alive until its own --max-time, so waiting on it here would
    # hold this message back for the entire watch window — half an hour of
    # silence to report that the first fifteen seconds failed.
    kill "$CURL_PID" 2>/dev/null
    wait "$CURL_PID" 2>/dev/null
    CURL_PID=""
    printf 'Agent Mail: could not subscribe for %s (no stream opened). Check the server and credentials before relying on instant delivery.\n' "$AGENT"
    exit 0
fi

# ── catch up, under the protection of a live subscription ────────────────────
#
# Anything already waiting is read here. Anything arriving from now on has a
# subscriber, so it produces a hint. Between the two there is no gap, which is
# the entire point of doing this after `: ready` rather than before connecting.
# DETECTION ONLY. Running inbox_check.sh here would deliver the content — and
# in doing so mark those ids announced and stamp the rate limiter, making this
# background task the sole carrier of a message the dedupe store now believes
# was delivered. Lose this stdout and the mail is gone, because the SessionStart
# check that runs when the agent wakes will skip ids it has already recorded.
# Ask whether anything is waiting; let the established channel say what.
pending="$(am_call fetch_inbox "$(jq -nc --arg p "$PROJECT" --arg a "$AGENT" --arg t "$token" \
    '{project_key:$p,agent_name:$a,registration_token:$t,unread_only:true,limit:1,include_bodies:false}')")"
if printf '%s' "$pending" | jq -e 'type == "array" and length > 0' >/dev/null 2>&1; then
    # Something is already waiting. Exit now rather than sitting on an open
    # subscription: the agent has work it can do, and holding the connection
    # would only delay what SessionStart is about to hand it anyway.
    printf 'Agent Mail: mail is already waiting for %s. Read it, then re-arm instant delivery by running this in the BACKGROUND:\n    %s\n' \
        "$AGENT" "$SELF_CMD"
    exit 0
fi

# ── wait for a hint ──────────────────────────────────────────────────────────
wait "$CURL_PID" 2>/dev/null
CURL_PID=""
ended="$(date +%s 2>/dev/null || echo 0)"
elapsed=$(( ended - started ))
out="$(cat "$stream" 2>/dev/null)"

if printf '%s' "$out" | grep -q '^data: '; then
    id="$(printf '%s' "$out" | sed -n 's/^data: .*"id":\([0-9]*\).*/\1/p' | head -1)"
    printf 'Agent Mail: new mail for %s (id %s). Read it, then re-arm instant delivery by running this in the BACKGROUND:\n    %s\n' \
        "$AGENT" "${id:-?}" "$SELF_CMD"
elif [ "$elapsed" -lt $(( WATCH - 5 )) ] 2>/dev/null; then
    # A subscription that ends early ended because something cut it, not
    # because nothing happened. Without this a proxy or CDN dropping the
    # connection at its idle limit reports "no new mail" — a quiet period that
    # never occurred, and the exact shape of failure this layer keeps
    # producing.
    printf 'Agent Mail: the event stream closed after %ss of a %ss window for %s — it was cut, not idle. This is NOT "no new mail": mail sent after the cut was not delivered here. Start the watcher again, and check the inbox directly if this keeps happening.\n' \
        "$elapsed" "$WATCH" "$AGENT"
else
    printf 'Agent Mail: watch window elapsed (%ss) with no new mail for %s. Start this watcher again in the background.\n' \
        "$elapsed" "$AGENT"
fi
exit 0
