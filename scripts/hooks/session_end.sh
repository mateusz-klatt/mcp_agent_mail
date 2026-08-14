#!/usr/bin/env bash
# SessionEnd ends the root AgentExecution. SubagentStop records a provisional
# stop because another matching provider hook can block it and continue the
# child; a later child tool cancels it, while the first parent event finalizes
# it. The server releases only automatic reservations owned by an execution;
# explicit reservations keep their TTL and require explicit release.
#
# The identity and its credential survive. Automatic holds go at confirmed end;
# explicit holds retain their TTL. The next session on this host reclaims the
# same mailbox instead of appearing as a new agent.
#
# Cannot fail: nothing useful happens after this hook, so an error here would
# only surface as a spurious failure at the end of otherwise good work.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

am_read_payload
AM_SESSION_ID="$(am_payload_field '.session_id')"
HOOK_EVENT="$(am_payload_field '.hook_event_name')"
if [ "$HOOK_EVENT" = "SubagentStop" ]; then
    am_subagent_executions_request_stop_all_for_payload claude \
        >/dev/null 2>&1 || true
else
    am_root_executions_end_all_for_payload claude completed \
        >/dev/null 2>&1 || true
fi

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
[ "$HOOK_EVENT" = "SubagentStop" ] && printf '{}\n'
exit 0
