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
    local installed_monitor running_hash installed_hash stream delivery_status="unconfirmed"
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
    stream="${AM_STATE_DIR}/watch/monitor-${slug}-${pid}.stream"
    # The owner and source checks prove that the intended monitor process is
    # running, but not that its long-lived delivery request is authenticated.
    # Inspect only the exact stream owned by the validated pid. Never echo its
    # contents: an error response can contain operational details that do not
    # belong in a diagnostic transcript.
    if [ -r "$stream" ] && grep -Fq 'Unauthorized' "$stream" 2>/dev/null; then
        delivery_status="unauthorized"
        printf 'monitor delivery: unauthorized; the running monitor may hold a stale bearer\n'
    elif [ -r "$stream" ] && grep -q '^: ready' "$stream" 2>/dev/null; then
        delivery_status="authenticated"
        printf 'monitor delivery: authenticated (: ready observed)\n'
    elif [ ! -s "$stream" ]; then
        printf 'monitor delivery: unconfirmed; stream is empty or between reconnects\n'
    else
        printf 'monitor delivery: unconfirmed; stream response is transient or not recognized\n'
    fi
    if [ -n "$running_hash" ] && [ "$running_hash" = "$installed_hash" ]; then
        if [ "$delivery_status" = "unauthorized" ]; then
            printf 'monitor: running pid %s; source is current, but delivery authentication failed\n' \
                "$pid"
            return 1
        fi
        printf 'monitor: healthy pid %s; source is current\n' "$pid"
        return 0
    fi
    printf 'monitor: running pid %s from a different script snapshot; re-arm it\n' "$pid"
    return 1
}

# Claude's plugin cache and its user-scoped lifecycle hooks are two independent
# installations. A cached `/doctor` used to inspect files relative to
# CLAUDE_PLUGIN_ROOT, which made an old cache compare itself with itself and
# report "current" while the marketplace source and the hooks actually in use
# had moved on. Keep this inventory explicit: it is the same runtime surface
# declared by the plugin plus the exact nine scripts copied by
# integrate_claude_code.sh.
CLAUDE_AGENT_MAIL_PLUGIN_ID="mcp-agent-mail@mateusz-klatt-mcp-agent-mail"
CLAUDE_AGENT_MAIL_MARKETPLACE="mateusz-klatt-mcp-agent-mail"
CLAUDE_AGENT_MAIL_PLUGIN_FILES=(
    .claude-plugin/plugin.json
    skills/doctor/SKILL.md
    skills/onboard/SKILL.md
    skills/wake/SKILL.md
    scripts/hooks/inbox_watch_monitor.sh
    scripts/hooks/agent_mail_common.sh
    scripts/hooks/agent_mail_setup.sh
)
CLAUDE_AGENT_MAIL_HOOKS=(
    agent_mail_common.sh
    session_start.sh
    inbox_check.sh
    reservations_warn.sh
    autoreserve.sh
    session_end.sh
    inbox_watch.sh
    inbox_watch_monitor.sh
    agent_mail_setup.sh
)

normalized_file_sha256() {
    local path="$1" line
    [ -r "$path" ] || return 1
    # The Claude integrator deliberately writes LF copies even when a native
    # Windows checkout has CRLF. Hash the same canonical stream so line-ending
    # policy is not mistaken for stale code. No file contents or digest is ever
    # printed by the diagnostic.
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "${line%$'\r'}"
    done < "$path" | am_sha256_full
}

claude_inventory_path() {
    local raw="$1"
    case "$raw" in
        ''|*$'\n'*|*$'\r'*) return 1 ;;
    esac
    am_normalize_runtime_user_path "$raw"
}

claude_inventory_command() (
    # Plugin inventory has no reason to inherit any Agent Mail credential.
    # Apart from being unnecessary, forwarding one to a third-party CLI makes a
    # read-only freshness check a new secret-bearing subprocess. Use a subshell
    # so the diagnostic's own authenticated server probe keeps its environment.
    unset INTEGRATION_BEARER_TOKEN HTTP_BEARER_TOKEN MCP_AGENT_MAIL_TOKEN
    unset AGENT_MAIL_TOKEN AGENT_MAIL_REGISTRATION_TOKEN _TOKEN
    command claude "$@"
)

is_full_lower_git_sha() {
    local value="$1"
    case "$value" in ''|*[!0-9a-f]*) return 1 ;; esac
    [ "${#value}" -eq 40 ] || [ "${#value}" -eq 64 ]
}

claude_freshness_status() {
    local plugins_json="" marketplaces_json="" plugin_count="" marketplace_count=""
    local installed_version="" installed_root_raw="" installed_root="" installed_enabled=""
    local marketplace_root_raw="" marketplace_root="" marketplace_source=""
    local source_version="" source_mode="" catalog_source=""
    local manifest_version="" catalog_version="" source_git_sha=""
    local installed_manifest_version="" installed_manifest_valid=0
    local source_ready=0 installed_ready=0
    local versions_match=1 rel source_hash installed_hash plugin_drift=0 hook_drift=0
    local installed_hooks_root="${HOME}/.claude/hooks/mcp-agent-mail"
    CLAUDE_FRESHNESS_PROBLEMS=0

    if ! command -v claude >/dev/null 2>&1; then
        printf 'Claude plugin inventory: unavailable; claude CLI is not on PATH\n'
        CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
        return
    fi

    if plugins_json="$(claude_inventory_command plugin list --json 2>/dev/null)" \
        && printf '%s' "$plugins_json" | jq -e '
            if type == "array" then .
            elif type == "object" and (.plugins | type == "array") then .plugins
            else error("unsupported plugin inventory") end
            | all(.[]; type == "object")
        ' >/dev/null 2>&1; then
        plugin_count="$(printf '%s' "$plugins_json" | jq -r \
            --arg id "$CLAUDE_AGENT_MAIL_PLUGIN_ID" '
                if type == "array" then . else .plugins end
                | [.[] | select(.id == $id)] | length
            ')"
        if [ "$plugin_count" = "1" ]; then
            installed_version="$(printf '%s' "$plugins_json" | jq -r \
                --arg id "$CLAUDE_AGENT_MAIL_PLUGIN_ID" '
                    if type == "array" then . else .plugins end
                    | [.[] | select(.id == $id)][0].version
                    | strings | select(length > 0)
                ' 2>/dev/null)"
            installed_root_raw="$(printf '%s' "$plugins_json" | jq -r \
                --arg id "$CLAUDE_AGENT_MAIL_PLUGIN_ID" '
                    if type == "array" then . else .plugins end
                    | [.[] | select(.id == $id)][0].installPath
                    | strings | select(length > 0)
                ' 2>/dev/null)"
            installed_enabled="$(printf '%s' "$plugins_json" | jq -r \
                --arg id "$CLAUDE_AGENT_MAIL_PLUGIN_ID" '
                    if type == "array" then . else .plugins end
                    | [.[] | select(.id == $id)][0].enabled
                    | if . == false then "false" else "true" end
                ' 2>/dev/null)"
            if [ -n "$installed_version" ] \
                && installed_root="$(claude_inventory_path "$installed_root_raw")" \
                && [ -n "$installed_root" ]; then
                installed_ready=1
                if [ "$installed_enabled" = "false" ]; then
                    printf 'Claude plugin: installed at version %s but disabled\n' \
                        "$installed_version"
                    CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                fi
            else
                printf 'Claude plugin inventory: Agent Mail entry has invalid version or installPath\n'
                CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
            fi
        elif [ "$plugin_count" = "0" ]; then
            printf 'Claude plugin: mcp-agent-mail is not installed\n'
            CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
        else
            printf 'Claude plugin inventory: %s Agent Mail entries are ambiguous\n' \
                "$plugin_count"
            CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
        fi
    else
        printf 'Claude plugin inventory: claude plugin list --json failed or returned an unsupported shape\n'
        CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
    fi

    if marketplaces_json="$(claude_inventory_command plugin marketplace list --json 2>/dev/null)" \
        && printf '%s' "$marketplaces_json" | jq -e '
            if type == "array" then .
            elif type == "object" and (.marketplaces | type == "array") then .marketplaces
            else error("unsupported marketplace inventory") end
            | all(.[]; type == "object")
        ' >/dev/null 2>&1; then
        marketplace_count="$(printf '%s' "$marketplaces_json" | jq -r \
            --arg name "$CLAUDE_AGENT_MAIL_MARKETPLACE" '
                if type == "array" then . else .marketplaces end
                | [.[] | select(.name == $name)] | length
            ')"
        if [ "$marketplace_count" = "1" ]; then
            marketplace_root_raw="$(printf '%s' "$marketplaces_json" | jq -r \
                --arg name "$CLAUDE_AGENT_MAIL_MARKETPLACE" '
                    if type == "array" then . else .marketplaces end
                    | [.[] | select(.name == $name)][0].installLocation
                    | strings | select(length > 0)
                ' 2>/dev/null)"
            marketplace_source="$(printf '%s' "$marketplaces_json" | jq -r \
                --arg name "$CLAUDE_AGENT_MAIL_MARKETPLACE" '
                    if type == "array" then . else .marketplaces end
                    | [.[] | select(.name == $name)][0].source
                    | strings | select(length > 0)
                ' 2>/dev/null)"
            if marketplace_root="$(claude_inventory_path "$marketplace_root_raw")" \
                && [ -n "$marketplace_root" ] && [ -n "$marketplace_source" ]; then
                if ! jq -e '
                        type == "object" and .name == "mcp-agent-mail" and
                        ((has("version") | not) or
                         (.version | type == "string" and length > 0))
                    ' "${marketplace_root}/.claude-plugin/plugin.json" \
                        >/dev/null 2>&1; then
                    printf 'Claude plugin source: plugin manifest is invalid or missing\n'
                    CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                elif ! jq -e '
                        type == "object" and (.plugins | type == "array") and
                        ([.plugins[] | select(.name == "mcp-agent-mail")] | length) == 1 and
                        ([.plugins[] | select(.name == "mcp-agent-mail")][0] |
                         ((has("version") | not) or
                          (.version | type == "string" and length > 0)))
                    ' "${marketplace_root}/.claude-plugin/marketplace.json" \
                        >/dev/null 2>&1; then
                    printf 'Claude plugin source: marketplace manifest is invalid or ambiguous\n'
                    CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                else
                    manifest_version="$(jq -r '.version // empty' \
                        "${marketplace_root}/.claude-plugin/plugin.json" 2>/dev/null)"
                    catalog_version="$(jq -r '
                        [.plugins[] | select(.name == "mcp-agent-mail")][0].version // empty
                    ' "${marketplace_root}/.claude-plugin/marketplace.json" 2>/dev/null)"
                    catalog_source="$(jq -r '
                        [.plugins[] | select(.name == "mcp-agent-mail")][0].source
                        | strings | select(. == "./")
                    ' "${marketplace_root}/.claude-plugin/marketplace.json" 2>/dev/null)"
                    if [ -n "$manifest_version" ]; then
                        source_version="$manifest_version"
                        source_mode="explicit"
                    elif [ -n "$catalog_version" ]; then
                        source_version="$catalog_version"
                        source_mode="explicit"
                    else
                        case "$marketplace_source" in
                            github|git|url|directory)
                                if [ -z "$catalog_source" ]; then
                                    printf 'Claude plugin source: Git-SHA mode requires the root-relative ./ plugin in the Git-backed marketplace\n'
                                    CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                                else
                                    source_git_sha="$(am_git "$marketplace_root" rev-parse HEAD)"
                                    if is_full_lower_git_sha "$source_git_sha"; then
                                        source_version="$source_git_sha"
                                        if [ "$marketplace_source" = "directory" ]; then
                                            source_mode="directory"
                                        else
                                            source_mode="git"
                                        fi
                                    elif [ "$marketplace_source" = "directory" ] \
                                        && [ "$(am_git "$marketplace_root" rev-parse --is-inside-work-tree)" = "true" ]; then
                                        printf 'Claude plugin source: live marketplace directory is a Git worktree, but its exact HEAD is unavailable or invalid\n'
                                        CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                                        source_version="live-directory"
                                        source_mode="directory"
                                    else
                                        printf 'Claude plugin source: marketplace %s cannot be verified as a Git worktree with an exact HEAD\n' \
                                            "$marketplace_source"
                                        CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                                        if [ "$marketplace_source" = "directory" ]; then
                                            source_version="live-directory"
                                            source_mode="directory"
                                        fi
                                    fi
                                fi ;;
                            *)
                                printf 'Claude plugin source: version fields are absent but marketplace source %s is not Git-backed\n' \
                                    "$marketplace_source"
                                CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1)) ;;
                        esac
                    fi
                    if [ -n "$manifest_version" ] && [ -n "$catalog_version" ] \
                        && [ "$manifest_version" != "$catalog_version" ]; then
                        printf 'Claude plugin source: manifest version %s differs from marketplace version %s\n' \
                            "$manifest_version" "$catalog_version"
                        CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                        versions_match=0
                    fi
                    if [ -n "$source_version" ]; then
                        if [ "$versions_match" -eq 1 ] \
                            || [ "$marketplace_source" = "directory" ]; then
                            source_ready=1
                        fi
                    fi
                    if [ "$source_ready" -eq 1 ] \
                        && [ "$marketplace_source" = "directory" ]; then
                        source_mode="directory"
                        if [ -z "$source_git_sha" ]; then
                            source_git_sha="$(am_git "$marketplace_root" rev-parse HEAD)"
                        fi
                        if is_full_lower_git_sha "$source_git_sha"; then
                            printf 'Claude plugin source: live Git-backed directory at %s\n' \
                                "$source_git_sha"
                        else
                            printf 'Claude plugin source: live directory has no verifiable exact Git HEAD\n'
                        fi
                        printf 'Claude plugin source: legacy directory mode is not an immutable tracked-file snapshot\n'
                        CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                    fi
                fi
            else
                printf 'Claude marketplace inventory: Agent Mail source or installLocation is invalid\n'
                CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
            fi
        elif [ "$marketplace_count" = "0" ]; then
            printf 'Claude marketplace: %s is not configured\n' \
                "$CLAUDE_AGENT_MAIL_MARKETPLACE"
            CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
        else
            printf 'Claude marketplace inventory: %s Agent Mail entries are ambiguous\n' \
                "$marketplace_count"
            CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
        fi
    else
        printf 'Claude marketplace inventory: claude plugin marketplace list --json failed or returned an unsupported shape\n'
        CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
    fi

    if [ "$installed_ready" -eq 1 ] && [ "$source_mode" != "directory" ]; then
        if ! jq -e '
                type == "object" and .name == "mcp-agent-mail" and
                ((has("version") | not) or
                 (.version | type == "string" and length > 0))
            ' "${installed_root}/.claude-plugin/plugin.json" \
                >/dev/null 2>&1; then
            printf 'Claude plugin cache: installed manifest is invalid or missing\n'
            CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
            versions_match=0
        else
            installed_manifest_valid=1
            installed_manifest_version="$(jq -r '.version // empty' \
                "${installed_root}/.claude-plugin/plugin.json" 2>/dev/null)"
        fi
        if [ "$installed_manifest_valid" -eq 1 ]; then
            if [ -n "$installed_manifest_version" ] \
                && [ "$installed_manifest_version" != "$installed_version" ]; then
                printf 'Claude plugin cache: inventory version %s differs from cached manifest version %s\n' \
                    "$installed_version" "$installed_manifest_version"
                CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                versions_match=0
            elif [ -z "$installed_manifest_version" ] && [ "$source_mode" = "explicit" ]; then
                printf 'Claude plugin cache: explicit-version source has no cached manifest version\n'
                CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                versions_match=0
            elif [ -z "$installed_manifest_version" ] \
                && ! is_full_lower_git_sha "$installed_version"; then
                printf 'Claude plugin cache: versionless manifest requires a full lowercase Git SHA inventory version\n'
                CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                versions_match=0
            fi
        fi
    fi

    if [ "$source_ready" -eq 1 ] && [ "$installed_ready" -eq 1 ]; then
        if [ "$source_mode" = "directory" ]; then
            printf 'Claude plugin version: cached inventory %s is not a freshness signal for a live directory source\n' \
                "$installed_version"
            printf 'Claude plugin files: live from marketplace directory; cache comparison is not applicable\n'
        elif [ "$installed_version" != "$source_version" ]; then
            # Exact identity is intentional. Ordering version strings would
            # misclassify 0.10 versus 0.9 and still would not answer whether
            # the selected cache is the source snapshot Claude will execute.
            if [ "$source_mode" = "git" ]; then
                printf 'Claude plugin version: installed %s differs from source Git HEAD %s\n' \
                    "$installed_version" "$source_version"
            else
                printf 'Claude plugin version: installed %s differs from source %s\n' \
                    "$installed_version" "$source_version"
            fi
            CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
            versions_match=0
        elif [ "$versions_match" -eq 1 ]; then
            if [ "$source_mode" = "git" ]; then
                printf 'Claude plugin version: current (%s exact Git SHA match)\n' \
                    "$source_version"
            else
                printf 'Claude plugin version: current (%s exact match)\n' "$source_version"
            fi
        fi

        if [ "$source_mode" != "directory" ] && [ "$versions_match" -eq 1 ]; then
            for rel in "${CLAUDE_AGENT_MAIL_PLUGIN_FILES[@]}"; do
                if ! source_hash="$(normalized_file_sha256 "${marketplace_root}/${rel}")"; then
                    printf 'Claude plugin file: source missing or unreadable: %s\n' "$rel"
                    CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                    plugin_drift=$((plugin_drift + 1))
                elif ! installed_hash="$(normalized_file_sha256 "${installed_root}/${rel}")"; then
                    printf 'Claude plugin file: installed copy missing or unreadable: %s\n' "$rel"
                    CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                    plugin_drift=$((plugin_drift + 1))
                elif [ "$source_hash" != "$installed_hash" ]; then
                    printf 'Claude plugin file: same-version drift: %s\n' "$rel"
                    CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                    plugin_drift=$((plugin_drift + 1))
                fi
            done
            if [ "$plugin_drift" -eq 0 ]; then
                printf 'Claude plugin files: current (%s/%s normalized hashes match)\n' \
                    "${#CLAUDE_AGENT_MAIL_PLUGIN_FILES[@]}" \
                    "${#CLAUDE_AGENT_MAIL_PLUGIN_FILES[@]}"
            fi
        elif [ "$source_mode" != "directory" ]; then
            printf 'Claude plugin files: hash comparison deferred until version metadata agrees\n'
        fi
    fi

    if [ "$source_ready" -eq 1 ]; then
        for rel in "${CLAUDE_AGENT_MAIL_HOOKS[@]}"; do
            if ! source_hash="$(normalized_file_sha256 "${marketplace_root}/scripts/hooks/${rel}")"; then
                printf 'Claude hook: source missing or unreadable: %s\n' "$rel"
                CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                hook_drift=$((hook_drift + 1))
            elif ! installed_hash="$(normalized_file_sha256 "${installed_hooks_root}/${rel}")"; then
                printf 'Claude hook: installed copy missing or unreadable: %s\n' "$rel"
                CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                hook_drift=$((hook_drift + 1))
            elif [ "$source_hash" != "$installed_hash" ]; then
                printf 'Claude hook: installed copy drift: %s\n' "$rel"
                CLAUDE_FRESHNESS_PROBLEMS=$((CLAUDE_FRESHNESS_PROBLEMS + 1))
                hook_drift=$((hook_drift + 1))
            fi
        done
        if [ "$hook_drift" -eq 0 ]; then
            printf 'Claude hooks: current (%s/%s normalized hashes match)\n' \
                "${#CLAUDE_AGENT_MAIL_HOOKS[@]}" \
                "${#CLAUDE_AGENT_MAIL_HOOKS[@]}"
        fi
    fi
    return 0
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
    if [ "$CLIENT" = "claude" ]; then
        claude_freshness_status
        problems=$((problems + CLAUDE_FRESHNESS_PROBLEMS))
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
