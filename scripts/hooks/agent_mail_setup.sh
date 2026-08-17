#!/usr/bin/env bash
# Durable mailbox onboarding, credential rotation, and local diagnosis.
#
# This script is intentionally a thin client of agent_mail_common.sh.  The
# common layer already owns canonical project/Agent identity, private state,
# credential locking, and secret-safe HTTP transport. Keeping those rules in
# one place is what makes `/onboard`, `/rotate-token`, lifecycle hooks, and
# `/doctor` agree.

set -uo pipefail
# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || {
    printf 'Agent Mail: cannot load agent_mail_common.sh.\n' >&2
    exit 1
}

usage() {
    cat >&2 <<'EOF'
Usage:
  agent_mail_setup.sh onboard <client> <slot> [options]
  agent_mail_setup.sh doctor  <client> <slot> [options]
  agent_mail_setup.sh rotate-token <client> <slot> [--project-key KEY]

Options:
  --project-key KEY   Use this canonical project key instead of deriving origin.
  --program NAME      Profile program (default derived from client).
  --model NAME        Profile model (default derived from current hook context).
  --local-marker      Also write .agent-mail-project-id and hide it locally via
                      .git/info/exclude. Never commits it or edits .gitignore.
EOF
}

safe_program() {
    case "$1" in
        claude)  printf 'claude-code' ;;
        codex)   printf 'codex-cli' ;;
        copilot) printf 'github-copilot-cli' ;;
        gemini)  printf 'gemini-cli' ;;
        *) return 1 ;;
    esac
}

write_local_marker() {
    local project_uid="$1" root marker git_exclude existing
    [ -n "$project_uid" ] || {
        printf 'Agent Mail: server response omitted project_uid; local marker not written.\n' >&2
        return 1
    }
    root="$(am_git . rev-parse --show-toplevel)" || return 1
    [ -n "$root" ] || return 1
    marker="${root}/.agent-mail-project-id"
    if [ -e "$marker" ]; then
        existing="$(tr -d '\r\n[:space:]' < "$marker" 2>/dev/null)"
        if [ "$existing" != "$project_uid" ]; then
            printf 'Agent Mail: refusing to replace a different local marker at %s.\n' "$marker" >&2
            return 1
        fi
    fi
    git_exclude="$(am_git . rev-parse --git-path info/exclude)" || return 1
    [ -n "$git_exclude" ] || return 1
    case "$git_exclude" in
        /*|[A-Za-z]:/*) ;;
        *) git_exclude="${root}/${git_exclude}" ;;
    esac
    mkdir -p "$(dirname "$git_exclude")" 2>/dev/null || return 1
    if ! grep -Fxq '.agent-mail-project-id' "$git_exclude" 2>/dev/null; then
        printf '%s\n' '.agent-mail-project-id' >> "$git_exclude" || return 1
    fi
    if [ ! -e "$marker" ]; then
        printf '%s\n' "$project_uid" > "$marker" || return 1
    fi
    printf 'local marker: %s (hidden only in .git/info/exclude)\n' "$marker"
}

local_marker_status() {
    local root marker git_exclude marker_tracked=0 problem=0
    root="$(am_git . rev-parse --show-toplevel)" || return 1
    [ -n "$root" ] || return 1
    marker="${root}/.agent-mail-project-id"
    git_exclude="$(am_git . rev-parse --git-path info/exclude)" || return 1
    case "$git_exclude" in
        /*|[A-Za-z]:/*) ;;
        *) git_exclude="${root}/${git_exclude}" ;;
    esac

    if [ -r "${root}/.gitignore" ] \
        && grep -Fxq '.agent-mail-project-id' "${root}/.gitignore" 2>/dev/null; then
        printf 'local marker policy: invalid public .gitignore entry; use only .git/info/exclude\n'
        problem=1
    fi
    if am_git "$root" ls-files --error-unmatch -- '.agent-mail-project-id' \
        >/dev/null 2>&1; then
        marker_tracked=1
        printf 'local marker policy: invalid tracked .agent-mail-project-id\n'
        problem=1
    fi
    if [ ! -e "$marker" ]; then
        printf 'local marker: absent (optional; private onboarding state is sufficient)\n'
        [ "$problem" -eq 0 ]
        return
    fi
    if ! grep -Eq '[^[:space:]]' "$marker" 2>/dev/null; then
        printf 'local marker: invalid empty file\n'
        return 1
    fi
    if [ "$marker_tracked" -eq 0 ] \
        && grep -Fxq '.agent-mail-project-id' "$git_exclude" 2>/dev/null; then
        printf 'local marker: present and hidden only in .git/info/exclude\n'
    else
        printf 'local marker: present but not safely isolated in .git/info/exclude\n'
        problem=1
    fi
    [ "$problem" -eq 0 ]
}

monitor_status() {
    local project="$1" agent="$2" slug marker meta pid meta_pid parent_pid supervise_parent
    local installed_monitor running_hash installed_hash
    slug="$(am_state_component "${project}|${agent}")" || return 1
    marker="${AM_STATE_DIR}/watch/monitor-${slug}.pid"
    meta="${AM_STATE_DIR}/watch/monitor-${slug}.json"
    if [ ! -r "$marker" ]; then
        if [ -e "$meta" ]; then
            printf 'monitor: stale metadata without an ownership marker\n'
            return 1
        fi
        printf 'monitor: not armed for this project and Agent (optional; use /wake only when unattended)\n'
        return 2
    fi
    pid="$(tr -cd '0-9' < "$marker" 2>/dev/null)"
    case "$pid" in ''|*[!0-9]*)
        printf 'monitor: stale marker (invalid pid)\n'
        return 1 ;;
    esac
    if ! kill -0 "$pid" 2>/dev/null; then
        printf 'monitor: stale marker for exited pid %s\n' "$pid"
        return 1
    fi
    if [ ! -r "$meta" ]; then
        printf 'monitor: running pid %s, but metadata is missing (old monitor)\n' "$pid"
        return 1
    fi
    meta_pid="$(jq -r '.pid // empty' < "$meta" 2>/dev/null)"
    if [ "$meta_pid" != "$pid" ] \
        || ! jq -e --arg project "$project" --arg agent "$agent" \
            '.project_key == $project and .agent_name == $agent' \
            < "$meta" >/dev/null 2>&1; then
        printf 'monitor: metadata does not belong to this exact project and Agent\n'
        return 1
    fi
    # `kill -0` on the owner answers a different question depending on whether
    # the monitor is supervising at all, so read its own latch rather than
    # re-deriving one here. The monitor probes once at birth, while the owner is
    # certainly alive, and records the verdict; a failure it saw then means the
    # probe has no resolving power on this host, not that anything died. Windows
    # hits that whenever the owner is a native process outside the MSYS table --
    # `parent_pid` comes back as 1 -- and reporting "shutting down" there called
    # a healthy armed monitor dying, in the one check the wake skill tells
    # operators to trust instead of `ps`.
    parent_pid="$(jq -r '.parent_pid // empty' < "$meta" 2>/dev/null)"
    supervise_parent="$(jq -r '.supervise_parent // empty' < "$meta" 2>/dev/null)"
    case "$parent_pid" in
        ''|*[!0-9]*) printf 'monitor parent: unknown\n' ;;
        *)
            if kill -0 "$parent_pid" 2>/dev/null; then
                printf 'monitor parent: live pid %s\n' "$parent_pid"
            elif [ "$supervise_parent" = "0" ]; then
                printf 'monitor parent: pid %s not observable here; the monitor disabled owner supervision at startup and will not exit for this\n' \
                    "$parent_pid"
            elif [ -z "$supervise_parent" ]; then
                # Armed before the latch was published. Both explanations remain
                # open, so say so rather than pick one: a false "issue" against a
                # healthy monitor is the direction that costs an operator a
                # needless re-arm, and this case disappears on the next arm.
                printf 'monitor parent: pid %s not signalable, and this monitor predates the supervision record, so whether it is exiting cannot be told from here\n' \
                    "$parent_pid"
            else
                printf 'monitor parent: exited pid %s (monitor is shutting down)\n' "$parent_pid"
                return 1
            fi ;;
    esac
    installed_monitor="$(dirname "$0")/inbox_watch_monitor.sh"
    running_hash="$(jq -r '.source_sha256 // empty' < "$meta" 2>/dev/null)"
    installed_hash="$(am_sha256 < "$installed_monitor" 2>/dev/null || true)"
    if [ -n "$running_hash" ] && [ "$running_hash" = "$installed_hash" ]; then
        printf 'monitor: healthy pid %s; source is current\n' "$pid"
        return 0
    fi
    printf 'monitor: running pid %s from a different script snapshot; re-arm it\n' "$pid"
    return 1
}

[ "$#" -ge 3 ] || { usage; exit 2; }
MODE="$1"
CLIENT="$(am_client "$2")" || exit 2
SLOT="$(am_slot "$3")" || exit 2
shift 3

PROJECT_OVERRIDE=""
PROGRAM=""
MODEL=""
LOCAL_MARKER=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --project-key)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            PROJECT_OVERRIDE="$2"
            shift 2 ;;
        --program)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            PROGRAM="$2"
            shift 2 ;;
        --model)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            MODEL="$2"
            shift 2 ;;
        --local-marker)
            LOCAL_MARKER=1
            shift ;;
        *)
            printf 'Agent Mail: unknown option %s.\n' "$1" >&2
            usage
            exit 2 ;;
    esac
done
case "$MODE" in onboard|doctor|rotate-token) ;; *) usage; exit 2 ;; esac
if [ "$MODE" = "doctor" ] && [ "$LOCAL_MARKER" -eq 1 ]; then
    printf 'Agent Mail: --local-marker is an onboarding mutation, not a doctor option.\n' >&2
    exit 2
fi
if [ "$MODE" = "rotate-token" ] \
    && { [ -n "$PROGRAM" ] || [ -n "$MODEL" ] || [ "$LOCAL_MARKER" -eq 1 ]; }; then
    printf 'Agent Mail: rotate-token accepts only --project-key; profile and marker options belong to onboarding.\n' >&2
    exit 2
fi
if [ "${AM_PATH_CONFIGURATION_VALID:-0}" != "1" ]; then
    printf 'Agent Mail: private state path is invalid or lies inside Git.\n' >&2
    exit 1
fi

PROJECT="${PROJECT_OVERRIDE:-$(am_project_key)}"
if [ -z "$PROJECT" ]; then
    printf 'Agent Mail: cannot derive a canonical project key; pass --project-key explicitly.\n' >&2
    exit 1
fi
export AM_PROJECT_FOR_NAME="$PROJECT"
AGENT="$(am_agent_name "$CLIENT" "$SLOT")" || exit 1
if migration_pair="$(am_identity_migration_pair "$PROJECT" "$CLIENT" "$SLOT")"; then
    printf '%s\n' "$(am_identity_migration_message \
        "${migration_pair%%$'\t'*}" "${migration_pair#*$'\t'}")" >&2
    exit 1
fi

if [ "$MODE" = "rotate-token" ]; then
    if ! am_registration_token_rotate_and_persist "$PROJECT" "$AGENT"; then
        exit 1
    fi
    printf 'Agent Mail registration credential rotated and verified.\n'
    printf 'project: %s\nagent: %s\n' "$PROJECT" "$AGENT"
    printf 'credential: stored privately at %s (value not displayed)\n' "$AM_CRED_FILE"
    printf 'next: restart/resume the CLI so it opens a fresh MCP session with the replacement credential.\n'
    exit 0
fi

if [ "$MODE" = "doctor" ]; then
    problems=0
    printf 'project: %s\nagent: %s\nclient/slot: %s/%s\n' \
        "$PROJECT" "$AGENT" "$CLIENT" "$SLOT"
    if am_project_has_local_state "$PROJECT" "$CLIENT" "$SLOT"; then
        printf 'local state: present at %s\n' "$AM_STATE_DIR"
    else
        printf 'local state: missing; run /mcp-agent-mail:onboard\n'
        problems=$((problems + 1))
    fi
    local_marker_status || problems=$((problems + 1))
    token="$(am_cred_get "$PROJECT" "$AGENT")"
    if [ -z "$token" ]; then
        printf 'credential: missing for this exact project and Agent\n'
        problems=$((problems + 1))
    else
        printf 'credential: present in private store (value not displayed)\n'
        auth_args="$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" jq -nc \
            --arg p "$PROJECT" --arg a "$AGENT" \
            '{project_key:$p,agent_name:$a,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,limit:1,include_bodies:false}')"
        if am_call fetch_inbox "$auth_args" >/dev/null; then
            printf 'server authentication: valid (stateless probe)\n'
        else
            rc=$?
            printf 'server authentication: failed (%s)\n' "$(am_failure_reason "$rc" "")"
            problems=$((problems + 1))
        fi
    fi
    monitor_status "$PROJECT" "$AGENT"
    monitor_rc=$?
    if [ "$monitor_rc" -eq 1 ]; then
        problems=$((problems + 1))
    fi
    if [ "$problems" -eq 0 ]; then
        printf 'result: healthy\n'
        exit 0
    fi
    printf 'result: %s issue(s); no state was changed\n' "$problems"
    exit 1
fi

PROGRAM="${PROGRAM:-$(safe_program "$CLIENT")}" || exit 1
MODEL="${MODEL:-$(am_model_id)}"
ensure_args="$(jq -nc --arg key "$PROJECT" '{human_key:$key}')"
ensure_response="$(am_call ensure_project "$ensure_args")"
rc=$?
if [ "$rc" -ne 0 ]; then
    printf 'Agent Mail: ensure_project failed: %s.\n' \
        "$(am_failure_reason "$rc" "$ensure_response")" >&2
    exit 1
fi
project_uid="$(printf '%s' "$ensure_response" | jq -r '.project_uid // empty' 2>/dev/null)"

# The helper performs registration and both private writes while the one-time
# token exists only in this command substitution.  Never echo this variable.
token="$(am_ensure_agent_credential \
    "$PROJECT" "$AGENT" "$CLIENT" "$SLOT" "$PROGRAM" "$MODEL")"
rc=$?
if [ "$rc" -ne 0 ] || [ -z "$token" ]; then
    printf 'Agent Mail: onboarding could not obtain and persist the registration credential.\n' >&2
    printf 'If this Agent already exists, recover its original private credential; the server never reissues it.\n' >&2
    exit 1
fi

# Existing credentials take the helper's fast path. Authenticate and refresh
# the profile explicitly so onboarding is a real end-to-end verification, not
# merely proof that a string exists in credentials.json.
register_args="$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" jq -nc \
    --arg p "$PROJECT" --arg n "$AGENT" --arg program "$PROGRAM" --arg model "$MODEL" \
    '{project_key:$p,name:$n,program:$program,model:$model,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN}')"
register_response="$(am_call register_agent "$register_args")"
rc=$?
if [ "$rc" -ne 0 ]; then
    printf 'Agent Mail: persisted credential failed authentication: %s.\n' \
        "$(am_failure_reason "$rc" "$register_response")" >&2
    exit 1
fi
got_name="$(printf '%s' "$register_response" | jq -r '.name // empty' 2>/dev/null)"
if [ "$got_name" != "$AGENT" ] \
    || ! am_granted_name_put "$PROJECT" "$AGENT" "$CLIENT" "$SLOT"; then
    printf 'Agent Mail: server/local identity mismatch; refusing partial onboarding.\n' >&2
    exit 1
fi

auth_args="$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" jq -nc \
    --arg p "$PROJECT" --arg a "$AGENT" \
    '{project_key:$p,agent_name:$a,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,limit:1,include_bodies:false}')"
if ! am_call fetch_inbox "$auth_args" >/dev/null; then
    printf 'Agent Mail: registration succeeded, but authenticated inbox verification failed.\n' >&2
    exit 1
fi

if [ "$LOCAL_MARKER" -eq 1 ]; then
    write_local_marker "$project_uid" || exit 1
fi
display_name="$(printf '%s' "$register_response" | jq -r '.display_name // empty' 2>/dev/null)"
notify_sound="$(printf '%s' "$register_response" | jq -r '.notify_sound // empty' 2>/dev/null)"
printf 'Agent Mail onboarding complete.\n'
printf 'project: %s\nagent: %s\n' "$PROJECT" "$AGENT"
[ -n "$display_name" ] && printf 'display name: %s\n' "$display_name"
[ -n "$notify_sound" ] && printf 'notification sound: %s\n' "$notify_sound"
printf 'credential: stored privately at %s (value not displayed)\n' "$AM_CRED_FILE"
printf 'authenticated inbox: verified\n'
printf 'next: restart/resume the CLI if lifecycle hooks predated onboarding; use /mcp-agent-mail:wake only when leaving it unattended.\n'
