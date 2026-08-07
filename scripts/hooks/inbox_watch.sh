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
# What it does not do: read the mailbox. The server's hint carries only an id,
# and this script prints one line saying mail is waiting. Reading is
# `inbox_check.sh`'s job, and it runs on SessionStart — which is exactly when
# the agent comes back after this exits. Fetching here would duplicate that,
# and would put the message body in a background task's output rather than in
# the channel the agent already reads.
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

PROJECT="$(am_project_key)"
[ -z "$PROJECT" ] && exit 0
AGENT="$(am_agent_name)"

token="$(am_cred_get "$PROJECT" "$AGENT")"
# No credential means SessionStart never ran, so there is no identity to
# subscribe as. Silence is right: the next SessionStart registers and the mail
# is picked up there.
[ -z "$token" ] && exit 0
bearer="$(am_bearer)"
[ -z "$bearer" ] && exit 0

url="${AM_BASE_URL}/events?project=$(am_urlencode "$PROJECT")&agent=$(am_urlencode "$AGENT")"

# --no-buffer, or curl holds the frame in its own output buffer and the wake
# arrives whenever the buffer happens to flush rather than when the mail lands.
out="$(printf 'header = "Authorization: Bearer %s"\nheader = "X-Agent-Mail-Registration-Token: %s"\nheader = "Accept: text/event-stream"\n' \
        "$bearer" "$token" \
    | curl -sN --no-buffer --max-time "$WATCH" -K - "$url" 2>/dev/null)"

# Distinguish the two reasons for waking, because they call for different next
# moves and the agent cannot tell them apart from an empty exit.
if printf '%s' "$out" | grep -q '^data: '; then
    id="$(printf '%s' "$out" | sed -n 's/^data: .*"id":\([0-9]*\).*/\1/p' | head -1)"
    printf 'Agent Mail: new mail for %s (id %s). Read it, then start this watcher again.\n' \
        "$AGENT" "${id:-?}"
elif printf '%s' "$out" | grep -q '^: ready'; then
    printf 'Agent Mail: watch window elapsed with no new mail for %s. Start this watcher again.\n' "$AGENT"
else
    # Never subscribed — bad credential, server down, proxy in the way. Say so
    # rather than reporting "no mail", which would be indistinguishable from a
    # healthy quiet period and could hide an outage for as long as it lasts.
    printf 'Agent Mail: could not subscribe for %s (no stream opened). Check the server and credentials before relying on instant delivery.\n' "$AGENT"
fi
exit 0
