#!/usr/bin/env bash
# PreToolUse (Edit|Write|NotebookEdit): warn when the file about to be edited is
# reserved by a different agent.
#
# This is the only cross-machine guard that exists. The shipped pre-commit guard
# reads reservations from the server's archive directory on the LOCAL filesystem,
# so on any machine that does not host the container it sees zero reservations,
# always — and warning at commit time is too late regardless.
#
# CANNOT FAIL and cannot hang: a PreToolUse hook that exits non-zero BLOCKS the
# tool call, so an advisory warning would otherwise make the repository
# uneditable the moment the server is unreachable. Every path ends in exit 0.
#
# Advisory only, never permissionDecision:"deny". Reservations are advisory by
# design; denial would convert a server outage into a work stoppage. Revisit only
# if agents are observed ignoring the warning, and then only for confirmed active
# exclusive conflicts.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || exit 0

am_read_payload
target="$(am_payload_field '.tool_input.file_path')"
[ -z "$target" ] && target="$(am_payload_field '.tool_input.notebook_path')"
[ -z "$target" ] && exit 0

# Project of the FILE, not of the working directory — see am_relpath.
PROJECT="$(am_project_key_for_file "$target")"
[ -z "$PROJECT" ] && exit 0
SELF="$(am_agent_name)"

rel="$(am_relpath "$target")"
[ -z "$rel" ] && exit 0

body="$(am_get /mail/api/file-reservations \
    --data-urlencode "project=${PROJECT}" --data-urlencode "path=${rel}")"
[ -z "$body" ] && exit 0

# Ignore our own holds: at one path per edit, re-editing a file we already
# reserved is the common case, and a warning that is usually wrong gets ignored.
conflict="$(printf '%s' "$body" | jq -r --arg me "$SELF" \
    '[.reservations[]? | select(.agent != $me)][0] // empty
     | "\(.path_pattern) is reserved by \(.agent)\(if (.reason // "") == "" then "" else " (" + .reason + ")" end)"' 2>/dev/null)"
[ -z "$conflict" ] && exit 0

am_emit_context "PreToolUse" \
    "Agent Mail: ${conflict}. Coordinate before editing — see ${AGENT_MAIL_PUBLIC_URL:-$AM_BASE_URL}/mail"
exit 0
