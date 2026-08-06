#!/usr/bin/env bash
# SessionEnd hook: release this session's advisory reservations.
#
# Without this, every reservation lingers until its TTL expires. That is not
# merely untidy: the whole point of the system is that a held path means
# "someone is working here right now", and stale holds train the other agents to
# treat the warning as noise. The TTL is the backstop for a crash, not the
# normal path.
#
# The identity is durable and its credential deliberately survives — only the
# reservations are dropped, so the next session on this host reclaims the same
# mailbox rather than appearing as a new agent.
#
# Cannot fail: nothing useful happens after this hook, and an error here would
# only surface as a spurious failure at the end of otherwise good work.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

PROJECT="$(am_project_key)"
AGENT="$(am_agent_name)"
[ -z "$PROJECT" ] && exit 0

token="$(am_cred_get "$PROJECT" "$AGENT")"
[ -z "$token" ] && exit 0

am_call release_file_reservations "$(jq -nc \
    --arg p "$PROJECT" --arg a "$AGENT" --arg t "$token" \
    '{project_key:$p,agent_name:$a,registration_token:$t}')" >/dev/null 2>&1
exit 0
