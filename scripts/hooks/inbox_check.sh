#!/usr/bin/env bash
# Deliver unread mail into the agent's context.
#
# This is the hook the rest of the system exists to serve. Reservations stop two
# agents editing one file; only this lets them say anything to each other, or
# lets a human say anything to any of them.
#
# Wired to both SessionStart and PostToolUse:
#   * SessionStart, so a message waiting when a session opens is seen at once
#     rather than after the session's first edit — which for a session that only
#     reads would be never;
#   * PostToolUse, so a message arriving mid-task is picked up within one tool
#     call rather than at the end of the turn.
#
# Deliberately NOT marking anything read. Read state belongs to the agent, which
# marks it after acting (mark_message_read / acknowledge_message); marking on
# delivery would make an unacted-on message indistinguishable from a handled one.
# Repetition is instead avoided by remembering which ids were already announced.
#
# Cannot fail: an inbox check that breaks a session is worse than a late message.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

INTERVAL="${AGENT_MAIL_INBOX_INTERVAL:-120}"
MAX_BODY="${AGENT_MAIL_INBOX_BODY_CHARS:-1200}"
# How many unread messages to enumerate per check. Bodies are NOT fetched for
# these — see the two-call split below — so this is a cheap ceiling, and it must
# stay well above any believable backlog: anything past it is invisible to this
# hook until the newer messages are read.
INVENTORY="${AGENT_MAIL_INBOX_INVENTORY:-200}"
# How many NEW messages to reproduce in full. The rest are named but not quoted.
RENDER_FULL="${AGENT_MAIL_INBOX_RENDER_FULL:-10}"
# How many further new messages to name on one line each.
SUMMARY_MAX="${AGENT_MAIL_INBOX_SUMMARY_MAX:-40}"
# Announced ids retained before the low end is folded into a floor.
KEEP_IDS="${AGENT_MAIL_INBOX_KEEP_IDS:-500}"

am_read_payload
EVENT="$(am_payload_field '.hook_event_name')"
[ -z "$EVENT" ] && EVENT="PostToolUse"

PROJECT="$(am_project_key)"
# am_agent_name resolves the project itself when AM_PROJECT_FOR_NAME is unset,
# which means a second `git config` and `git rev-parse` for a value this hook
# has already computed one line above. The override exists for exactly this and
# nothing set it. Measured by home-win-1: am_project_key costs ~122 ms per call
# on Windows, so this is a doubled cost on every tool invocation there.
export AM_PROJECT_FOR_NAME="$PROJECT"
[ -z "$PROJECT" ] && exit 0
am_project_is_active "$PROJECT" claude "${AGENT_MAIL_CLAUDE_SLOT:-1}" . || exit 0
if migration_pair="$(am_identity_migration_pair "$PROJECT" claude "${AGENT_MAIL_CLAUDE_SLOT:-1}")"; then
    legacy_agent="${migration_pair%%$'\t'*}"
    client_agent="${migration_pair#*$'\t'}"
    am_emit_context "$EVENT" \
        "$(am_identity_migration_message "$legacy_agent" "$client_agent")"
    exit 0
fi
AGENT="$(am_agent_name claude "${AGENT_MAIL_CLAUDE_SLOT:-1}")"

token="$(am_cred_get "$PROJECT" "$AGENT")"
[ -z "$token" ] && exit 0   # SessionStart has not run; nothing to authenticate as

# Separators MAPPED, not deleted — third instance of a defect fixed twice
# already (am_granted_name_file, then am_sync_model). `tr -cd` alone drops the
# slashes rather than replacing them, so `/a/b` and `/ab` collapse onto one key.
#
# This one is the worst of the three. The other two guard a name and a cached
# model string; these guard `.seen`, the high-water mark of what this agent has
# already been shown. Two projects sharing it means mail from one is recorded as
# seen by the other and never surfaces — a silently lost message, which is the
# single failure this hook exists to prevent.
slug="$(am_state_component "${PROJECT}|${AGENT}")" || exit 0
stamp="${AM_STATE_DIR}/inbox/${slug}.stamp"
seen="${AM_STATE_DIR}/inbox/${slug}.seen"
mkdir -p "$(dirname "$stamp")" 2>/dev/null || exit 0

# Rate limit. PostToolUse fires after every tool call, and an HTTP round trip per
# call would be both wasteful and — through a proxy — slow enough to notice.
# SessionStart always checks: that is the one moment where waiting is pointless.
if [ "$EVENT" != "SessionStart" ] && [ -f "$stamp" ]; then
    now=$(date +%s 2>/dev/null || echo 0)
    then_=$(cat "$stamp" 2>/dev/null || echo 0)
    case "$then_" in ''|*[!0-9]*) then_=0 ;; esac
    [ $((now - then_)) -lt "$INTERVAL" ] && exit 0
fi
# Stamped AFTER the call, not before — see the failure branch. Writing it here
# would mean a failed check silences its own outage warning for the full
# interval, so an agent would be told once that the channel is down and then
# nothing for two minutes while it stayed down.

# Keep the profile's model field true while the session runs. Deliberately BELOW
# the rate limit above, so it inherits that cadence and adds nothing to the hot
# path: PostToolUse fires after every tool call, and reading the transcript there
# would cost a few milliseconds every time to notice something that changes once
# a session, if ever. Within one inbox interval is soon enough.
#
# This is not a refinement of what SessionStart recorded — on a fresh session the
# model is genuinely unknowable at that moment (no assistant turn exists yet, and
# the hook payload carries no model at all), so this is the only path by which
# the field ever becomes true. It cannot fail the hook: am_sync_model returns 0
# unconditionally and reaches the network only when the value actually changed.
am_sync_model "$PROJECT" "$AGENT" "$token"

# ── what has already been announced ──────────────────────────────────────────
#
# {floor, ids}: every id at or below `floor` is settled, and `ids` lists the
# announced ones above it.
#
# A high-water mark ALONE is why this needed changing. fetch_inbox orders newest
# first and then applies the limit (app.py, `.order_by(desc(created_ts)).limit`),
# so with more unread than the limit the hook saw only the newest page, stored
# the top id, and every older unread message fell below the mark for good —
# invisible from then on even once the newer ones were read. Verified against a
# live server: 14 unread, 10 shown, the remaining 4 never announced again.
#
# A bare integer is the pre-existing format. It records the highest id announced
# and says NOTHING about the ids below it, so reading it as a floor would
# preserve on disk the exact loss this change removes. Treat it instead as a set
# of one: on the first check after an upgrade every still-unread message is
# announced again, including any that were stranded. Messages that are unread
# are by definition not yet acted on, so repeating them once is the safe
# direction, and it happens once per agent.
state='{"floor":0,"ids":[]}'
if [ -r "$seen" ]; then
    raw="$(cat "$seen" 2>/dev/null)"
    parsed="$(printf '%s' "$raw" | jq -c '
        if type == "number" then {floor: 0, ids: [.]}
        elif type == "object" and (.floor | type) == "number" and (.ids | type) == "array"
            then {floor: .floor, ids: .ids}
        else {floor: 0, ids: []} end' 2>/dev/null)"
    [ -n "$parsed" ] && state="$parsed"
fi

# ── call 1: enumerate, without bodies ────────────────────────────────────────
#
# Split into two calls so the common case — nothing new — costs one small
# response instead of dragging every unread body across the wire every 120s.
inv="$(am_call fetch_inbox "$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" \
    jq -nc --arg p "$PROJECT" --arg a "$AGENT" --argjson n "$INVENTORY" \
    '{project_key:$p,agent_name:$a,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,unread_only:true,limit:$n,include_bodies:false}')")"
rc=$?
# "No new mail" and "I could not ask" are the same silence today, and this hook
# is the only channel through which anyone reaches this agent. A deploy window
# or a dropped link therefore looks exactly like a quiet morning, for as long
# as it lasts — including to a human trying to say stop.
if [ "$rc" -ne 0 ]; then
    # Back off, but far less than the healthy interval: retry soon enough that
    # the agent learns when the channel returns, rarely enough not to hammer a
    # server that is already struggling.
    now_fail=$(date +%s 2>/dev/null || echo 0)
    printf '%s' "$(( now_fail - INTERVAL + ${AGENT_MAIL_INBOX_RETRY:-30} ))" > "$stamp" 2>/dev/null
    why="$(am_failure_reason "$rc" "$inv")"
    am_emit_context "$EVENT" \
        "Agent Mail: could not check the inbox — ${why}. This is NOT 'no new messages': mail may be waiting, including from the human overseer. Retry before assuming the channel is quiet."
    exit 0
fi
date +%s > "$stamp" 2>/dev/null
[ -z "$inv" ] && exit 0
printf '%s' "$inv" | jq -e 'type == "array"' >/dev/null 2>&1 || exit 0

fresh="$(printf '%s' "$inv" | jq -c --argjson st "$state" \
    '[.[] | select(.id > $st.floor and ((.id) as $i | $st.ids | index($i) | not))]' 2>/dev/null)"
[ -z "$fresh" ] && exit 0
count_fresh="$(printf '%s' "$fresh" | jq 'length' 2>/dev/null || echo 0)"
[ "$count_fresh" -eq 0 ] 2>/dev/null && exit 0

# ── call 2: bodies, for the few that get quoted in full ──────────────────────
#
# fetch_inbox has no id filter and no offset, so ask for a prefix long enough to
# cover the newest RENDER_FULL fresh items and then select by id. The slack
# absorbs messages arriving between the two calls, which would otherwise push
# the last wanted item off the end.
cutoff="$(printf '%s' "$inv" | jq --argjson st "$state" --argjson n "$RENDER_FULL" '
    [ to_entries[]
      | select(.value.id > $st.floor and ((.value.id) as $i | $st.ids | index($i) | not))
      | .key ]
    | (.[$n - 1] // .[-1] // -1) + 1 + 10' 2>/dev/null)"
case "$cutoff" in ''|*[!0-9]*) cutoff="$RENDER_FULL" ;; esac

want="$(printf '%s' "$fresh" | jq -c --argjson n "$RENDER_FULL" '[.[0:$n][].id]' 2>/dev/null)"
[ -z "$want" ] && want='[]'

bodies="$(am_call fetch_inbox "$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" \
    jq -nc --arg p "$PROJECT" --arg a "$AGENT" --argjson n "$cutoff" \
    '{project_key:$p,agent_name:$a,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,unread_only:true,limit:$n,include_bodies:true}')")"
printf '%s' "$bodies" | jq -e 'type == "array"' >/dev/null 2>&1 || bodies='[]'

full="$(printf '%s' "$bodies" | jq -c --argjson want "$want" \
    '[.[] | select(.id as $i | $want | index($i))]' 2>/dev/null)"
[ -z "$full" ] && full='[]'

# Whatever call 2 failed to return is named on one line rather than dropped: a
# message the agent is never told about is the failure this hook exists to
# prevent, and a subject line still says who to go and ask.
#
# Both arrays arrive on stdin, NOT through --argjson. $full holds whole message
# bodies, and handing those to jq as an argument puts them on the command line:
# measured at 44 kB for ten messages of ordinary length, against a hard 32 kB
# limit for an entire command line on Windows. Past that, jq cannot be launched
# at all — and the failure is silent in the worst way, because an empty $rest
# becomes an empty $announced and the hook exits without a word. Mail simply
# stops being delivered, on the busiest mailboxes first, and only on one
# platform. jq -s slurps the stream into an array, so nothing large is ever an
# argument and the ceiling stops existing on every platform at once.
rest="$({ printf '%s\n' "$fresh"; printf '%s\n' "$full"; } | jq -sc --argjson n "$SUMMARY_MAX" '
    .[0] as $fresh | .[1] as $full
    | [$full[].id] as $shown
    | [$fresh[] | select(.id as $i | $shown | index($i) | not)] | .[0:$n]' 2>/dev/null)"
[ -z "$rest" ] && rest='[]'

announced="$({ printf '%s\n' "$full"; printf '%s\n' "$rest"; } | jq -sc '[.[0][].id] + [.[1][].id]' 2>/dev/null)"
[ -z "$announced" ] && exit 0
printf '%s' "$announced" | jq -e 'length > 0' >/dev/null 2>&1 || exit 0

# ── render ───────────────────────────────────────────────────────────────────
text="$(printf '%s' "$full" | jq -r --argjson maxb "$MAX_BODY" '
    [ .[] |
      "── from \(.from // .sender_name // "?")"
      + (if (.importance // "normal") != "normal" then "  [\(.importance)]" else "" end)
      + (if (.ack_required // false) then "  [ACK REQUIRED]" else "" end)
      + "\nsubject: \(.subject // "(no subject)")\n"
      + ((.body_md // "") | if length > $maxb then .[0:$maxb] + "\n…(truncated)" else . end)
    ] | join("\n\n")' 2>/dev/null)"

listed="$(printf '%s' "$rest" | jq -r '
    [ .[] | "  #\(.id)  \(.from // .sender_name // "?"): \(.subject // "(no subject)")"
      + (if (.importance // "normal") != "normal" then "  [\(.importance)]" else "" end)
      + (if (.ack_required // false) then "  [ACK REQUIRED]" else "" end)
    ] | join("\n")' 2>/dev/null)"

count_all="$(printf '%s' "$inv" | jq 'length' 2>/dev/null || echo 0)"
total="$count_all"
[ "$count_all" -ge "$INVENTORY" ] 2>/dev/null && total="${INVENTORY}+"
header="Agent Mail: ${count_fresh} new message(s) for ${AGENT}; ${total} unread in total."

body=""
[ -n "$text" ] && body="${text}"
if [ -n "$listed" ]; then
    [ -n "$body" ] && body="${body}

"
    body="${body}Also new, not quoted here — fetch_inbox by id to read:
${listed}"
fi
# Only the ids actually named above may be recorded as announced.
beyond=$((count_fresh - $(printf '%s' "$announced" | jq 'length' 2>/dev/null || echo 0)))
if [ "$beyond" -gt 0 ] 2>/dev/null; then
    body="${body}
  …and ${beyond} more new, held over to the next check."
fi
[ -z "$body" ] && exit 0

# Record only what was named, so anything held over is announced next time
# rather than skipped. Trimming drops the oldest ids and folds them into the
# floor, which keeps this file bounded without ever stepping over an id that
# was never shown.
# Folding old ids into `floor` asserts that everything below them was already
# shown. That assertion is only true when the inventory was EXHAUSTIVE — and it
# is capped at INVENTORY and ordered newest-first, so with a backlog above the
# cap the oldest unread never entered it at all and would drop below the new
# floor permanently. Reproduced: after announcing 1000..501 then 500..451, ids
# 450..1 become unreachable forever.
#
# `count_all < INVENTORY` is exactly the proof that the response was complete:
# the server returned fewer rows than we asked for, so there is nothing else to
# return. Only then may the low end be folded. Otherwise the set keeps growing —
# a state file that grows during a backlog is a far smaller problem than a
# mailbox that silently loses its oldest messages, and it shrinks again on the
# next complete inventory.
exhaustive=0
[ "$count_all" -lt "$INVENTORY" ] 2>/dev/null && exhaustive=1
new_state="$(jq -nc --argjson st "$state" --argjson add "$announced" --argjson keep "$KEEP_IDS" \
    --argjson exhaustive "$exhaustive" '
    (($st.ids + $add) | unique | sort | reverse) as $all
    | if ($all | length) > $keep and $exhaustive == 1
        then ($all[0:$keep]) as $kept | {floor: (($kept | min) - 1), ids: $kept}
        else {floor: $st.floor, ids: $all} end' 2>/dev/null)"
if [ -n "$new_state" ]; then
    printf '%s' "$new_state" > "${seen}.tmp" 2>/dev/null && mv -f "${seen}.tmp" "$seen" 2>/dev/null
fi

am_emit_context "$EVENT" "${header}

${body}

Reply with send_message/reply_message, and mark handled with mark_message_read (or acknowledge_message when ACK is required) — otherwise it stays unread for everyone reviewing the mailbox."
exit 0
