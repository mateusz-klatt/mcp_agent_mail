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
# Repetition is instead avoided by remembering the highest id already announced.
#
# Cannot fail: an inbox check that breaks a session is worse than a late message.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

INTERVAL="${AGENT_MAIL_INBOX_INTERVAL:-120}"
MAX_BODY="${AGENT_MAIL_INBOX_BODY_CHARS:-1200}"

am_read_payload
EVENT="$(am_payload_field '.hook_event_name')"
[ -z "$EVENT" ] && EVENT="PostToolUse"

PROJECT="$(am_project_key)"
[ -z "$PROJECT" ] && exit 0
AGENT="$(am_agent_name)"

token="$(am_cred_get "$PROJECT" "$AGENT")"
[ -z "$token" ] && exit 0   # SessionStart has not run; nothing to authenticate as

slug="$(printf '%s|%s' "$PROJECT" "$AGENT" | tr -cd '[:alnum:]._|-' | tr '|' '_')"
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
date +%s > "$stamp" 2>/dev/null

resp="$(am_call fetch_inbox "$(jq -nc --arg p "$PROJECT" --arg a "$AGENT" --arg t "$token" \
    '{project_key:$p,agent_name:$a,registration_token:$t,unread_only:true,limit:10,include_bodies:true}')")"
[ -z "$resp" ] && exit 0
printf '%s' "$resp" | jq -e 'type == "array"' >/dev/null 2>&1 || exit 0

last=0
[ -r "$seen" ] && last="$(cat "$seen" 2>/dev/null)"
case "$last" in ''|*[!0-9]*) last=0 ;; esac

# Fresh = arrived since the last announcement. Older unread messages are counted
# but not repeated verbatim: a message the agent has chosen not to act on should
# not re-enter its context in full on every check, or the channel becomes noise
# and gets ignored — the exact failure mode of the poll this replaces.
fresh="$(printf '%s' "$resp" | jq -c --argjson last "$last" '[.[] | select(.id > $last)]' 2>/dev/null)"
count_fresh="$(printf '%s' "$fresh" | jq 'length' 2>/dev/null || echo 0)"
count_all="$(printf '%s' "$resp" | jq 'length' 2>/dev/null || echo 0)"
[ "$count_fresh" -eq 0 ] 2>/dev/null && exit 0

highest="$(printf '%s' "$fresh" | jq '[.[].id] | max' 2>/dev/null)"
[ -n "$highest" ] && [ "$highest" != "null" ] && printf '%s' "$highest" > "$seen" 2>/dev/null

text="$(printf '%s' "$fresh" | jq -r --argjson maxb "$MAX_BODY" '
    [ .[] |
      "── from \(.from // .sender_name // "?")"
      + (if (.importance // "normal") != "normal" then "  [\(.importance)]" else "" end)
      + (if (.ack_required // false) then "  [ACK REQUIRED]" else "" end)
      + "\nsubject: \(.subject // "(no subject)")\n"
      + ((.body_md // "") | if length > $maxb then .[0:$maxb] + "\n…(truncated)" else . end)
    ] | join("\n\n")' 2>/dev/null)"
[ -z "$text" ] && exit 0

header="Agent Mail: ${count_fresh} new message(s) for ${AGENT}."
older=$((count_all - count_fresh))
[ "$older" -gt 0 ] 2>/dev/null && header="${header} ${older} older unread not repeated here."

am_emit_context "$EVENT" "${header}

${text}

Reply with send_message/reply_message, and mark handled with mark_message_read (or acknowledge_message when ACK is required) — otherwise it stays unread for everyone reviewing the mailbox."
exit 0
