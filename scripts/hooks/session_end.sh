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

# Release across EVERY repository this session touched, not just the one its
# working directory happens to sit in. autoreserve keys each log by the project
# of the edited FILE, so a session that edited a second repository wrote a log
# this hook would otherwise never look for — and those reservations would then
# sit until their TTL, which is exactly the stale-hold problem the hook exists
# to prevent.
found=0
while IFS= read -r log; do
    [ -r "$log" ] || continue
    paths="$(am_session_paths "$log")"
    [ -n "$paths" ] && [ "$paths" != "[]" ] || { printf '' > "$log" 2>/dev/null || true; continue; }

    # New logs carry the exact project key in a JSON header.  Legacy logs are
    # accepted only when their old lossy slug resolves to exactly one key.
    project="$(am_session_project "$log")" || continue
    [ -n "$project" ] || continue
    export AM_PROJECT_FOR_NAME="$project"
    if am_identity_migration_pair "$project" claude "${AGENT_MAIL_CLAUDE_SLOT:-1}" >/dev/null; then
        continue
    fi
    agent="$(am_agent_name claude "${AGENT_MAIL_CLAUDE_SLOT:-1}")" || continue
    token="$(am_cred_get "$project" "$agent")"
    [ -n "$token" ] || continue

    response="$(am_call release_file_reservations "$(
        AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" \
        jq -nc --arg p "$project" --arg a "$agent" --argjson paths "$paths" \
        '{project_key:$p,agent_name:$a,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,paths:$paths}'
    )")"
    if [ $? -eq 0 ]; then
        # Keep an empty state file rather than deleting evidence after a failed
        # release.  am_session_log_add recreates its exact project header before
        # the next path is appended.
        printf '' > "$log" 2>/dev/null || true
        found=$((found+1))
    fi
done <<EOF
$(am_session_logs)
EOF

# Drop the derived bearer copy am_call writes for curl. It is a cache, not
# configuration, so its lifetime should be the session's — and unlike
# ~/.agent-mail.env, which the operator created deliberately and would think to
# remove when decommissioning a machine, this file appears on its own and nobody
# knows it is there.
#
# Safe against a concurrent session on the same host: am_hdr_conf rewrites the
# file whenever its contents do not match the current bearer, and a missing file
# reads as empty, so the worst case is one extra write in the other session.
rm -f "${AM_STATE_DIR}/curl-headers.conf" 2>/dev/null
exit 0
