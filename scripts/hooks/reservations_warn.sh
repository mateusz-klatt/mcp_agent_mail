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

# The only PreToolUse hook, and so the only one a person waits behind before
# every single edit. Elsewhere 8s buys reliability for free; here it would be
# eight seconds of nothing happening, per edit, whenever the network is a black
# hole. An explicit AGENT_MAIL_HOOK_TIMEOUT still wins.
AM_TIMEOUT="${AGENT_MAIL_HOOK_TIMEOUT:-3}"

am_read_payload
target="$(am_payload_field '.tool_input.file_path')"
[ -z "$target" ] && target="$(am_payload_field '.tool_input.notebook_path')"
[ -z "$target" ] && exit 0

# Project of the FILE, not of the working directory — see am_relpath.
PROJECT="$(am_project_key_for_file "$target")"
# The identity must come from the SAME project the credential is looked up
# under, and that is this file's project — not the session's. The reservation
# lives where the edited file lives, so the token that files it has to be the one
# issued there; a name resolved against the session's project produces a pair
# that matches only by accident.
#
# By accident is exactly what it is today: with no remembered name yet,
# am_agent_name falls through to derivation, which does not depend on the project
# at all — measured, three different project keys, one name. So this is
# preventive, not corrective: the mismatch appears at the first session restart,
# when granted/ starts holding per-project names, and would then be a silent
# exit rather than an error.
#
# Deferred earlier on the grounds that "session identity" and "file identity"
# were both defensible. They are not: home-wsl-1 measured that one of them files
# reservations and the other loses them.
export AM_PROJECT_FOR_NAME="$PROJECT"
[ -z "$PROJECT" ] && exit 0
SELF="$(am_agent_name)"

rel="$(am_relpath "$target")"
[ -z "$rel" ] && exit 0

body="$(am_get /mail/api/file-reservations \
    --data "project=$(am_urlencode "$PROJECT")" --data "path=$(am_urlencode "$rel")")"
rc=$?
# A server that did not answer is not a server that said "no conflicts", and
# staying quiet here states the second. During a deploy window every hook goes
# quiet at once: this one reports no conflict while autoreserve reports a hold
# filed, so two agents editing one file each get told they are alone. The
# coordination layer does not merely stop — for that minute it actively asserts
# a safety that is not there, which is worse than having no layer at all.
if [ "$rc" -ne 0 ]; then
    why="$(am_failure_reason "$rc" "$body")"
    am_emit_context "PreToolUse" \
        "Agent Mail: could not check reservations for ${rel} — ${why}. This is NOT 'no conflict': another agent may hold this file and you would not have been told. Check ${AGENT_MAIL_PUBLIC_URL:-$AM_BASE_URL}/mail before editing, or proceed knowing the guard is down."
    exit 0
fi
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
