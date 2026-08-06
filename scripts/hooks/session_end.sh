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

PROJECT="$(am_project_key)"
[ -z "$PROJECT" ] && exit 0
AGENT="$(am_agent_name)"

token="$(am_cred_get "$PROJECT" "$AGENT")"
[ -z "$token" ] && exit 0

log="$(am_session_log "$PROJECT")"
# Nothing recorded means this session reserved nothing. Falling back to a bare
# release here would be exactly the sibling-clobbering this hook exists to avoid.
[ -r "$log" ] || exit 0

paths="$(sort -u "$log" 2>/dev/null | jq -Rsc 'split("\n") | map(select(length>0))' 2>/dev/null)"
[ -z "$paths" ] || [ "$paths" = "[]" ] && exit 0

am_call release_file_reservations "$(jq -nc \
    --arg p "$PROJECT" --arg a "$AGENT" --arg t "$token" --argjson paths "$paths" \
    '{project_key:$p,agent_name:$a,registration_token:$t,paths:$paths}')" >/dev/null 2>&1

rm -f "$log" 2>/dev/null
exit 0
