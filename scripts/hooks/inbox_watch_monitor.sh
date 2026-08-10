#!/usr/bin/env bash
# Long-lived inbox monitor. Prints one short line per new message and KEEPS
# RUNNING, for the whole life of the process that started it.
#
#     ./scripts/hooks/inbox_watch_monitor.sh claude 1
#
# This is the sibling of inbox_watch.sh, not a replacement for it. They differ
# in the one thing that matters — what "wake" means:
#
#     inbox_watch.sh            the wake IS this process exiting
#     inbox_watch_monitor.sh    the wake is a LINE ON STDOUT; exiting is death
#
# That inversion drives every other difference below, and it is why this is a
# separate file rather than a flag. inbox_watch.sh is copied onto disk by the
# Claude integrator and is the documented manual path; changing its control flow
# to serve a second contract would put both behaviours in one set of exit paths,
# where the tests covering the first cannot tell you that you broke it.
#
# Intended caller: a Claude Code plugin monitor (see .claude-plugin/plugin.json),
# armed once per session by the bundled `wake` skill. A plugin monitor that exits
# is NEVER restarted for the remaining life of the CLI process — there is no
# respawn — so "exit 0 and let the next run sort it out", which is correct
# everywhere else in this hook directory, is the one thing this script must never
# do. Nobody is coming to start it again, and its death is silent.
#
# Consequences of that, each of which reads like a bug until you know the above:
#
#   * A missing credential does not end the run. inbox_watch.sh exits, because
#     SessionStart will register the identity and the next manual arm will work.
#     Here there is no next arm: the monitor can legitimately start BEFORE
#     SessionStart has registered, so it waits and re-checks instead.
#   * A failed subscription does not end the run. It backs off and retries.
#   * The first message does not end the run.
#
# Silence policy, deliberately asymmetric: a quiet mailbox prints NOTHING, ever.
# Every line printed here is a full agent turn — the message text is a rounding
# error next to the cost of the wake it causes — so the no-mail case must be
# free. The one exception is a sustained outage: "no new mail" and "I could not
# ask" must not look identical, so a failure streak past FAIL_ANNOUNCE_SECONDS
# prints exactly ONE line and then goes quiet again until it recovers.
#
# Cannot fail loudly: a monitor that dies is worse than a late message, so every
# unexpected condition backs off rather than aborting.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

# Same two-argument contract as inbox_watch.sh, and required for the same
# reason: this script is client-neutral, and guessing "claude" here is what made
# the watcher unusable from Codex and Copilot before a2d5b4f made it explicit.
if [ "$#" -ne 2 ]; then
    printf 'Agent Mail: inbox_watch_monitor.sh requires an explicit client and slot.\n' >&2
    exit 0
fi
CLIENT="$(am_client "$1")" || exit 0
SLOT="$(am_slot "$2")" || exit 0

# How long ONE subscription is held before reconnecting. Kept under the server's
# own cap (AGENT_MAIL_EVENTS_MAX_SECONDS, 3600 by default, which ends the stream
# with `: bye`) so the client decides when to cycle rather than discovering it
# from a closed socket. Longer than inbox_watch.sh's window on purpose: there,
# the window ending wakes the agent for nothing, so it is kept short to notice a
# dead subscription; here, cycling is free and invisible, so the only cost of a
# short window is a pointless catch-up query per cycle.
CONNECT="${AGENT_MAIL_MONITOR_CONNECT_SECONDS:-3300}"
# How long to wait for `: ready` before treating the subscription as failed.
READY_WAIT="${AGENT_MAIL_WATCH_READY_SECONDS:-15}"
# Backoff bounds for the retry loop. Starts small so a transient blip costs a
# few seconds of blindness, and caps well under the connection window so a long
# outage does not turn into an unbounded silence.
BACKOFF_MIN="${AGENT_MAIL_MONITOR_BACKOFF_MIN:-5}"
BACKOFF_MAX="${AGENT_MAIL_MONITOR_BACKOFF_MAX:-300}"
# How long a failure streak must last before it is worth one line of context.
FAIL_ANNOUNCE="${AGENT_MAIL_MONITOR_FAIL_SECONDS:-300}"

SELF_PATH="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)/$(basename -- "$0")" || exit 0

CURL_PID=""
STREAM=""
MARKER=""
cleanup() {
    [ -n "$CURL_PID" ] && kill "$CURL_PID" 2>/dev/null
    [ -n "$STREAM" ] && rm -f "$STREAM" 2>/dev/null
    # The marker is what tells SessionStart not to ask the agent to arm a second
    # watcher alongside this one. Removing it on the way out is what makes that
    # suppression self-healing: if this process dies, the very next SessionStart
    # goes back to advertising the manual path.
    [ -n "$MARKER" ] && rm -f "$MARKER" 2>/dev/null
}
trap cleanup EXIT
# The signal handlers must EXIT, not merely clean up.
#
# `trap cleanup EXIT INT TERM` is the idiom every other script in this directory
# uses, and it is wrong here for the same reason everything else in this file is
# different: a handler that does not exit RETURNS, and bash resumes the loop
# where it left off. In a script built never to end, that means SIGTERM tears
# down the live subscription and then immediately opens a new one — the process
# survives its own stop signal. Measured: after `kill`, the monitor was still
# running 135s later with a fresh curl child, so the plugin host, TaskStop and
# session teardown could all fail to stop it, and the following `kill -9` then
# orphaned the curl because no trap runs on SIGKILL. Exiting here lets the EXIT
# trap above do the cleaning exactly once, on every path.
trap 'exit 0' INT TERM HUP

# Highest message id already announced on stdout.
#
# Needed only because this script reconnects. After a wake, the mail stays
# unread until the agent gets to it, so the catch-up query that runs after every
# reconnect finds the same id again — and without this guard would announce it
# on every cycle, turning one message into an unbounded stream of wakes. Kept in
# memory rather than on disk: the value is meaningful only for the life of this
# process, and a stale file would suppress a real message after a restart.
last_id=0
backoff="$BACKOFF_MIN"
fail_since=0
fail_announced=0
migration_announced=0

# Print one line, as short as the information allows.
#
# The SSE frame carries {kind, project, id, agent} and no sender or subject, so
# naming who wrote is not possible without a second round trip — and it is not
# worth one. This line only has to say "there is mail"; inbox_check.sh delivers
# the content when the agent wakes, and it is the component that owns dedupe and
# rate limiting for actual delivery.
announce() {
    printf 'Agent Mail: new mail (id %s) for %s\n' "$1" "$2"
}

# One sleep helper so an interrupted sleep cannot be mistaken for elapsed time.
nap() { sleep "$1" 2>/dev/null || true; }

while :; do
    # ── identity, re-resolved every cycle ────────────────────────────────────
    #
    # Re-resolved rather than computed once, because the failure modes it covers
    # are all recoverable: a project activated after the monitor started, a
    # credential written by a SessionStart that had not run yet, an identity
    # migration completed by a human. Computing this once at startup would turn
    # every one of those into a permanently dead monitor. The cost is a couple
    # of git calls per cycle, and cycles are hourly.
    PROJECT="$(am_project_key)"
    if [ -z "$PROJECT" ]; then nap "$BACKOFF_MAX"; continue; fi
    export AM_PROJECT_FOR_NAME="$PROJECT"

    if ! am_project_is_active "$PROJECT" "$CLIENT" "$SLOT" .; then
        nap "$BACKOFF_MAX"
        continue
    fi

    if migration_pair="$(am_identity_migration_pair "$PROJECT" "$CLIENT" "$SLOT")"; then
        # Announced once, not once per cycle: the situation needs a human, and
        # repeating it hourly would wake the agent all night to say the same
        # thing it can do nothing about.
        if [ "$migration_announced" -eq 0 ]; then
            printf '%s\n' "$(am_identity_migration_message \
                "${migration_pair%%$'\t'*}" "${migration_pair#*$'\t'}")"
            migration_announced=1
        fi
        nap "$BACKOFF_MAX"
        continue
    fi
    migration_announced=0

    AGENT="$(am_agent_name "$CLIENT" "$SLOT")"
    token="$(am_cred_get "$PROJECT" "$AGENT")"
    bearer="$(am_bearer)"
    if [ -z "$AGENT" ] || [ -z "$token" ] || [ -z "$bearer" ]; then
        # No identity yet. inbox_watch.sh exits here; this one cannot.
        nap "$BACKOFF_MAX"
        continue
    fi

    # ── liveness marker, refreshed once per cycle ────────────────────────────
    #
    # Holds this process's pid so SessionStart can distinguish "a monitor is
    # running" from "a monitor ran once and died", which an mtime alone cannot:
    # this script blocks inside curl for up to CONNECT seconds, so freshness has
    # to tolerate an hour of no writes, and an hour is long enough for a dead
    # monitor to keep suppressing the manual hint.
    slug="$(am_state_component "${PROJECT}|${AGENT}")" || slug=""
    if [ -n "$slug" ]; then
        MARKER="${AM_STATE_DIR}/watch/monitor-${slug}.pid"
        mkdir -p "$(dirname "$MARKER")" 2>/dev/null
        printf '%s\n' "$$" > "$MARKER" 2>/dev/null
    fi

    # ── subscribe ────────────────────────────────────────────────────────────
    STREAM="${AM_STATE_DIR}/watch/monitor-${slug:-$$}.stream"
    : > "$STREAM" 2>/dev/null || { nap "$backoff"; continue; }
    url="${AM_BASE_URL}/events?project=$(am_urlencode "$PROJECT")&agent=$(am_urlencode "$AGENT")"
    started="$(date +%s 2>/dev/null || echo 0)"
    # Both secrets on stdin via -K -, never in argv: an argument is readable from
    # the process table by anything on the machine, and this process is alive for
    # hours rather than seconds, so the exposure window is the whole night.
    printf 'header = "Authorization: Bearer %s"\nheader = "X-Agent-Mail-Registration-Token: %s"\nheader = "Accept: text/event-stream"\n' \
            "$bearer" "$token" \
        | curl -sN --no-buffer --max-time "$CONNECT" -K - "$url" >>"$STREAM" 2>/dev/null &
    CURL_PID=$!

    ready=0
    ready_deadline=$(( started + READY_WAIT ))
    while :; do
        grep -q '^: ready' "$STREAM" 2>/dev/null && { ready=1; break; }
        kill -0 "$CURL_PID" 2>/dev/null || break
        [ "$(date +%s 2>/dev/null || echo 0)" -ge "$ready_deadline" ] 2>/dev/null && break
        sleep 0.2
    done

    if [ "$ready" -ne 1 ]; then
        kill "$CURL_PID" 2>/dev/null
        wait "$CURL_PID" 2>/dev/null
        CURL_PID=""
        now="$(date +%s 2>/dev/null || echo 0)"
        [ "$fail_since" -eq 0 ] && fail_since="$now"
        if [ "$fail_announced" -eq 0 ] \
           && [ $(( now - fail_since )) -ge "$FAIL_ANNOUNCE" ] 2>/dev/null; then
            # Exactly one line per outage. Not silence, because a channel that is
            # down looks identical to a quiet morning and this is the only way
            # the agent hears about anything; not a line per retry, because that
            # would wake the agent every few seconds for the length of the outage.
            printf 'Agent Mail: instant delivery has been down for %ss for %s — mail may be waiting. Check the inbox directly.\n' \
                "$(( now - fail_since ))" "$AGENT"
            fail_announced=1
        fi
        nap "$backoff"
        backoff=$(( backoff * 2 ))
        [ "$backoff" -gt "$BACKOFF_MAX" ] && backoff="$BACKOFF_MAX"
        continue
    fi

    # Subscribed. Any earlier failure is over.
    if [ "$fail_announced" -eq 1 ]; then
        printf 'Agent Mail: instant delivery restored for %s.\n' "$AGENT"
    fi
    fail_since=0
    fail_announced=0
    backoff="$BACKOFF_MIN"

    # ── catch up, under the protection of a live subscription ────────────────
    #
    # Ordering is the whole reason this is not one curl: subscribe, wait for
    # `: ready`, and only THEN read the mailbox. Reading first leaves a window in
    # which a message can arrive after the read and before the subscription
    # exists, producing no hint and waking nobody. Doing it on every reconnect is
    # what closes the same window across the gap between two connections.
    #
    # DETECTION ONLY. Running inbox_check.sh here would deliver the content and
    # mark those ids announced, making this background process the sole carrier
    # of a message the dedupe store then believes was delivered.
    pending="$(am_call fetch_inbox "$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" \
        jq -nc --arg p "$PROJECT" --arg a "$AGENT" \
        '{project_key:$p,agent_name:$a,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,unread_only:true,limit:1,include_bodies:false}')")"
    top="$(printf '%s' "$pending" | jq -r 'if type == "array" and length > 0 then (.[0].id // empty) else empty end' 2>/dev/null)"
    case "$top" in
        ''|*[!0-9]*) ;;
        *) if [ "$top" -gt "$last_id" ] 2>/dev/null; then
               announce "$top" "$AGENT"
               last_id="$top"
           fi ;;
    esac

    # ── hold the subscription until it yields its one frame ──────────────────
    #
    # `GET /events` is one-shot by design: it emits `: ready`, then `: ping`
    # every 15s, then exactly ONE `data:` frame and returns. So "a monitor that
    # never exits" cannot be one held connection — it is this loop. Concurrent
    # subscriptions for the same agent are explicitly supported server-side
    # (both are woken, neither evicts the other), so reconnecting immediately is
    # safe even if an old connection is still winding down.
    wait "$CURL_PID" 2>/dev/null
    CURL_PID=""
    out="$(cat "$STREAM" 2>/dev/null)"
    rm -f "$STREAM" 2>/dev/null

    if printf '%s' "$out" | grep -q '^data: '; then
        id="$(printf '%s' "$out" | sed -n 's/^data: .*"id":\([0-9]*\).*/\1/p' | head -1)"
        case "$id" in
            ''|*[!0-9]*) id="" ;;
        esac
        if [ -n "$id" ] && [ "$id" -gt "$last_id" ] 2>/dev/null; then
            announce "$id" "$AGENT"
            last_id="$id"
        fi
    fi
    # No frame means the window closed on its own — `: bye`, a cut, or the
    # client cap. All three are ordinary here and none of them is news, so the
    # loop simply goes round again without printing. This is the case that used
    # to cost a wake every 1800s, and making it free is the point of the file.
done
