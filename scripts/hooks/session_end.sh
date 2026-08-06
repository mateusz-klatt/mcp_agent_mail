#!/usr/bin/env bash
# SessionEnd: release the reservations THIS session filed.
#
# Path-scoped, not wholesale. release_file_reservations with no paths drops
# everything the agent holds — and the identity is shared by every CLI on this
# host for this project, so a bare release would strip a concurrent session of
# protection the moment an unrelated terminal was closed.
#
# The identity and its credential survive; only the holds go. The next session on
# this host reclaims the same mailbox instead of appearing as a new agent.
#
# Cannot fail: nothing useful happens after this hook, so an error here would
# only surface as a spurious failure at the end of otherwise good work.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

am_read_payload
AM_SESSION_ID="$(am_payload_field '.session_id')"

AGENT="$(am_agent_name)"

# Release across EVERY repository this session touched, not just the one its
# working directory happens to sit in. autoreserve keys each log by the project
# of the edited FILE, so a session that edited a second repository wrote a log
# this hook would otherwise never look for — and those reservations would then
# sit until their TTL, which is exactly the stale-hold problem the hook exists
# to prevent.
found=0
while IFS= read -r log; do
    [ -r "$log" ] || continue
    # Recover the project from the log's own name: "<session>__<project>.list".
    slug="${log##*__}"; slug="${slug%.list}"
    [ -z "$slug" ] && continue
    paths="$(sort -u "$log" 2>/dev/null | jq -Rsc 'split("\n") | map(select(length>0))' 2>/dev/null)"
    [ -z "$paths" ] || [ "$paths" = "[]" ] && { rm -f "$log" 2>/dev/null; continue; }

    # The slug is sanitised, so recover the real key from the credential store
    # by matching on it rather than trying to reverse the sanitisation.
    project="$(jq -r --arg s "$slug" 'keys[] | select((. | gsub("[^A-Za-z0-9._-]";"")) == $s)' \
        "$AM_CRED_FILE" 2>/dev/null | head -1)"
    [ -z "$project" ] && { rm -f "$log" 2>/dev/null; continue; }

    token="$(am_cred_get "$project" "$AGENT")"
    [ -z "$token" ] && { rm -f "$log" 2>/dev/null; continue; }

    am_call release_file_reservations "$(jq -nc \
        --arg p "$project" --arg a "$AGENT" --arg t "$token" --argjson paths "$paths" \
        '{project_key:$p,agent_name:$a,registration_token:$t,paths:$paths}')" >/dev/null 2>&1
    rm -f "$log" 2>/dev/null
    found=$((found+1))
done <<EOF
$(am_session_logs)
EOF
exit 0
