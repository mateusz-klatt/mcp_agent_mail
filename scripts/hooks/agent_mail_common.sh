# Shared plumbing for the Agent Mail hooks. Sourced, never executed.
#
# Installed once per machine in ~/.claude/settings.json and available only in
# repositories that carry an explicit opt-in marker or already have private
# Agent Mail state. Per-machine settings live in ~/.agent-mail.env, NOT in the
# hook commands: repeating four environment
# assignments across four commands is four places to forget one, and a forgotten
# one is silent.
#
# ~/.agent-mail.env (mode 0600), all optional except the token:
#   HTTP_BEARER_TOKEN=...                  server bearer
#   AGENT_MAIL_URL=https://host/mcp/       streamable-HTTP MCP endpoint
#   AGENT_MAIL_STATE_DIR=/path             shared credential/state location
#
# Client, slot, project, agent name and registration token are intentionally not
# accepted from this cross-client file.  A client-specific hook supplies its
# own client/slot; project and agent are derived at runtime; registration tokens
# live in credentials.json.
#
# Every function degrades to "do nothing" on failure. A hook that errors is a
# hook that blocks an edit.

# State contains durable-Agent credentials and per-execution capability tokens.
# Apply the private mode before the first redirect creates a temporary or final
# state file; chmod after creation would leave a short 0644 race under a caller's
# permissive umask.
umask 077

# Git Bash rewrites any argument that looks like a POSIX path into a Windows one
# before handing it to a NATIVE binary — and jq.exe and curl.exe are native. A
# project key of "/owner/repo" would arrive as "C:/Program Files/Git/owner/repo",
# creating a phantom project that no other machine can ever join. Exported here
# so the hook commands do not each have to remember it.
case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) export MSYS_NO_PATHCONV=1 ;;
esac

# A global hook can inherit a user path in either native Windows form
# (D:\Profiles\...) or the path dialect of the shell that launches it.  Keep
# one canonical form for the current execution substrate before Bash, jq and
# curl touch the same file.  In Git Bash the native jq/curl executables need a
# mixed Windows path; in WSL the Linux tools need /mnt/<drive>/....  A Windows
# path on a host that cannot translate it is a configuration error, never a
# relative filename in the repository currently being edited.
am_normalize_runtime_user_path() {
    local target="$1" system
    # Validate the caller-supplied dialect before asking cygpath to translate it.
    # `cygpath -m relative` helpfully anchors the value under $PWD, which would
    # turn a bad state setting into a seemingly absolute path inside whichever
    # repository happened to trigger the global hook.
    case "$target" in
        /*|[A-Za-z]:\\*|[A-Za-z]:/*) ;;
        *) return 1 ;;
    esac
    system="$(uname -s 2>/dev/null || printf unknown)"
    case "$system" in
        MINGW*|MSYS*|CYGWIN*)
            command -v cygpath >/dev/null 2>&1 || return 1
            cygpath -m "$target" 2>/dev/null
            return $? ;;
    esac
    case "$target" in
        [A-Za-z]:[\\/]*)
            command -v wslpath >/dev/null 2>&1 || return 1
            wslpath -u "$target" 2>/dev/null
            return $? ;;
        *) printf '%s' "$target" ;;
    esac
}

AM_PATH_CONFIGURATION_VALID=1

# Per-machine configuration. Parsed rather than sourced: this file is read on
# every edit, and executing whatever it happens to contain is not a property
# worth having. Environment always wins, so a hook command can still override.
am_load_env() {
    [ -n "${AM_ENV_LOADED:-}" ] && return 0
    AM_ENV_LOADED=1
    local configured_f="${AGENT_MAIL_ENV_FILE:-$HOME/.agent-mail.env}" f line k v
    if ! f="$(am_normalize_runtime_user_path "$configured_f")"; then
        AM_PATH_CONFIGURATION_VALID=0
        return 0
    fi
    case "$f" in
        /*|[A-Za-z]:/*) ;;
        *) AM_PATH_CONFIGURATION_VALID=0; return 0 ;;
    esac
    [ -r "$f" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        k="${line%%=*}"; v="${line#*=}"
        case "$k" in
            HTTP_BEARER_TOKEN|AGENT_MAIL_URL|AGENT_MAIL_STATE_DIR)
                # shellcheck disable=SC2163
                [ -z "$(eval "printf '%s' \"\${$k:-}\"")" ] && export "$k=$v" ;;
        esac
    done < "$f"
    return 0
}
am_load_env

# 3s was losing 2-5% of calls to nothing more than a slow round trip. The cost
# of raising it is paid only when packets vanish without an answer — a VPN, a
# firewall that drops instead of resetting, a dead CDN edge — because a refused
# connection returns in ~58ms whatever this is set to. Measured: a black hole
# costs exactly this many seconds, a dead server costs nothing.
#
# reservations_warn overrides it back down: it is the only PreToolUse hook, so
# it is the only one a person waits behind before every edit.
AM_TIMEOUT="${AGENT_MAIL_HOOK_TIMEOUT:-8}"
am_server_base_url() {
    local url="${AGENT_MAIL_URL:-http://127.0.0.1:8765}"
    url="${url%/}"
    case "$url" in
        */mcp) url="${url%/mcp}" ;;
        */api) url="${url%/api}" ;;
    esac
    printf '%s' "${url%/}"
}
AM_BASE_URL="$(am_server_base_url)"

# On MSYS the credential store must be addressed the way jq.exe can open it.
# $HOME there is "/c/Users/you", which a native binary cannot resolve — and the
# failure is asymmetric and therefore vicious: the first write succeeds (bash
# redirection handles the path) while every later read fails (jq gets it as an
# argument). Registration works once, then the identity is unrecoverable.
am_default_state_dir() {
    local base="${XDG_STATE_HOME:-$HOME/.local/state}"
    printf '%s/agent-mail' "$base"
}
_am_configured_state_dir="${AGENT_MAIL_STATE_DIR:-$(am_default_state_dir)}"
if ! AM_STATE_DIR="$(am_normalize_runtime_user_path "$_am_configured_state_dir")"; then
    AM_PATH_CONFIGURATION_VALID=0
    AM_STATE_DIR="/dev/null/agent-mail-invalid-state-dir"
fi
case "$AM_STATE_DIR" in
    /*|[A-Za-z]:/*) ;;
    *)
        AM_PATH_CONFIGURATION_VALID=0
        AM_STATE_DIR="/dev/null/agent-mail-invalid-state-dir" ;;
esac

# Capabilities and registration tokens must never be stageable. Refuse a state
# directory whose nearest existing ancestor is inside any Git worktree or Git
# directory. This also catches a not-yet-created `repo/.state/agent-mail`: the
# nearest existing ancestor is still the repository. The guard marker is a
# separate, non-secret file intentionally stored under Git metadata.
am_state_dir_is_inside_git() {
    local probe="$1" parent inside_worktree inside_git_dir
    [ -n "$probe" ] || return 1
    while [ ! -d "$probe" ]; do
        parent="$(dirname -- "$probe" 2>/dev/null)" || return 1
        [ -n "$parent" ] && [ "$parent" != "$probe" ] || return 1
        probe="$parent"
    done
    inside_worktree="$(git -C "$probe" rev-parse --is-inside-work-tree \
        2>/dev/null)"
    inside_git_dir="$(git -C "$probe" rev-parse --is-inside-git-dir \
        2>/dev/null)"
    [ "$inside_worktree" = "true" ] || [ "$inside_git_dir" = "true" ]
}
if [ "${AM_PATH_CONFIGURATION_VALID:-0}" = "1" ] \
    && am_state_dir_is_inside_git "$AM_STATE_DIR"; then
    AM_PATH_CONFIGURATION_VALID=0
    AM_STATE_DIR="/dev/null/agent-mail-state-dir-inside-git"
fi
unset _am_configured_state_dir
AM_CRED_FILE="${AM_STATE_DIR}/credentials.json"

# --- payload -----------------------------------------------------------------
# Claude Code delivers the invocation as JSON on stdin and closes the pipe.
# `read -d ''` consumes to EOF, bounded by -t: GNU `timeout` does not exist on a
# stock macOS, where `timeout 2 cat` would simply fail and the hook never run.
am_read_payload() {
    AM_PAYLOAD=""
    [ -t 0 ] && return 0
    IFS= read -r -d '' -t "$AM_TIMEOUT" AM_PAYLOAD 2>/dev/null || true
    return 0
}

am_payload_field() {
    printf '%s' "${AM_PAYLOAD:-}" | jq -r "$1 // empty" 2>/dev/null
}

# --- paths -------------------------------------------------------------------
# Claude Code does not normalise the paths a model supplies, so on Windows they
# arrive with backslashes. Everything downstream — git, the server, string
# comparison against another agent's reservation — expects forward slashes.
am_norm_path() {
    # Guarded on a drive letter: a backslash is a legal character in a POSIX
    # filename and must survive untouched there. Only a Windows-style absolute
    # path is rewritten.
    case "$1" in
        [A-Za-z]:[\\/]*) printf '%s' "${1//\\//}" ;;
        *)                 printf '%s' "$1" ;;
    esac
}

# Portable, bounded state-file component.  The readable prefix helps operators
# inspect state by hand; the digest preserves the full input so truncation and
# separator mapping cannot make two projects/sessions share a file.
am_sha256() {
    local digest
    if command -v sha256sum >/dev/null 2>&1; then
        digest="$(sha256sum 2>/dev/null | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        digest="$(shasum -a 256 2>/dev/null | awk '{print $1}')"
    elif command -v openssl >/dev/null 2>&1; then
        digest="$(openssl dgst -sha256 2>/dev/null | sed 's/^.*= //')"
    else
        # A different hash would make the shell hooks and Python migration CLI
        # address different state files.  Fail closed when no SHA-256 provider
        # exists instead of silently falling back to Git's repository hash.
        return 1
    fi
    case "$digest" in ''|*[!0-9A-Fa-f]*) return 1 ;; esac
    [ "${#digest}" -eq 64 ] || return 1
    printf '%s' "$(printf '%s' "$digest" | tr '[:upper:]' '[:lower:]' | cut -c1-32)"
}

am_state_component() {
    local raw="$1" prefix digest
    prefix="$(printf '%s' "$raw" \
        | LC_ALL=C tr -cs 'A-Za-z0-9._ -' '_' \
        | tr ' ' '_' \
        | cut -c1-47)"
    prefix="${prefix%_}"
    [ -n "$prefix" ] || prefix="state"
    digest="$(printf '%s' "$raw" | am_sha256)" || return 1
    printf '%s-%s' "$prefix" "$digest"
}

am_git() {
    git -C "$1" "${@:2}" 2>/dev/null
}

# --- project identity --------------------------------------------------------
# Derived from the repository's origin remote, normalised to "/owner/repo".
#
# ensure_project rejects any key that is not Path().is_absolute(), so a bare
# remote URL cannot be used; and the key must be byte-identical across hosts, so
# a checkout path cannot be used — /home/me/app, /Users/me/dev/app and C:\src\app
# are three projects with three mailboxes and nothing saying they were meant to
# be one.
am_normalize_remote() {
    printf '%s' "$1" \
        | sed -E 's#^[a-zA-Z]+://[^/]+/#/#; s#^[^@]+@[^:]+:#/#; s#\.git$##; s#/+$##' \
        | tr -d '\n'
}

am_project_key() {
    # A global client can inherit stale shell variables.  Project identity is
    # therefore always derived from the repository being opened; direct/API
    # callers that need an override must pass it outside this hook runtime.
    local url
    url="$(git rev-parse --is-inside-work-tree >/dev/null 2>&1 && git remote get-url origin 2>/dev/null)" || return 0
    [ -z "$url" ] && return 0
    am_normalize_remote "$url"
}

# For the edit hooks the project must come from the repository that OWNS THE
# FILE, not the working directory. Those differ more often than it looks — a
# scratch file under /tmp, a file opened from a second repository — and keying on
# the working directory files the edit under the wrong project, at an absolute
# path that can never match anyone else's reservation.
am_project_key_for_file() {
    # Likewise, an edit is owned by the file's repository, never by an ambient
    # project override inherited from the process that launched the client.
    local d url
    d="$(dirname "$(am_norm_path "$1")")"
    url="$(am_git "$d" rev-parse --is-inside-work-tree >/dev/null && am_git "$d" remote get-url origin)" || return 0
    [ -z "$url" ] && return 0
    am_normalize_remote "$url"
}

# Path relative to the git top-level of the file itself, so a worktree reserves
# something comparable with every other checkout. Yields nothing when the file
# lies outside a repository: an absolute reservation looks like protection and
# can never match.
am_relpath() {
    local p root
    p="$(am_norm_path "$1")"
    root="$(am_git "$(dirname "$p")" rev-parse --show-toplevel)" || return 0
    [ -z "$root" ] && return 0
    root="$(am_norm_path "$root")"
    p="${p#"${root}"/}"
    case "$p" in /*|?:/*) return 0 ;; esac   # still absolute -> outside that root
    printf '%s' "${p#./}"
}

# --- agent identity ----------------------------------------------------------
# Canonical names are <client>-<platform>-<host>-<slot>.  The platform token
# comes from uname, which is the only thing that separates the three cases that
# otherwise collide: WSL and native Windows report the SAME hostname, and
# nothing else distinguishes a mac.
am_platform() {
    case "$(uname -s 2>/dev/null)" in
        Darwin)               printf 'mac' ;;
        MINGW*|MSYS*|CYGWIN*) printf 'win' ;;
        Linux)
            if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/version 2>/dev/null; then
                printf 'wsl'
            else
                printf 'linux'
            fi ;;
        *) printf 'other' ;;
    esac
}

am_client() {
    local client="${1:-}"
    case "$client" in
        claude) printf 'claude' ;;
        codex) printf 'codex' ;;
        copilot) printf 'copilot' ;;
        gemini) printf 'gemini' ;;
        *)
            printf 'Unsupported Agent Mail client: %s\n' "$client" >&2
            return 1 ;;
    esac
}

# Short cc/cx/cp client segments were briefly used while the stable identity
# contract was being designed.  They are read only for a fail-closed migration
# check; canonical names and new state always use the program-family name.
am_transitional_client_alias() {
    case "$(am_client "${1:-}")" in
        claude) printf 'cc' ;;
        codex) printf 'cx' ;;
        copilot) printf 'cp' ;;
        *) return 1 ;;
    esac
}

am_slot() {
    local slot="${1:-1}"
    case "$slot" in
        ''|0|0*|*[!0-9]*)
            printf 'Agent Mail slot must be a positive integer: %s\n' "$slot" >&2
            return 1 ;;
        *) printf '%s' "$slot" ;;
    esac
}

# Stable machine/platform prefix shared by the legacy and client-scoped name
# formats.  Keeping one implementation is important during migration: a
# one-character disagreement would make the legacy detector miss the identity
# it is meant to protect.
am_hostname() {
    local h=""
    if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
        h="$(scutil --get HostName 2>/dev/null || true)"
        [ -z "$h" ] && h="$(scutil --get LocalHostName 2>/dev/null || true)"
    fi
    [ -z "$h" ] && h="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo host)"
    h="$(printf '%s' "$h" \
        | LC_ALL=C tr '[:upper:]' '[:lower:]' \
        | LC_ALL=C tr -cd '[:alnum:]._-' \
        | LC_ALL=C sed 's/^[^[:alnum:]]*//; s/[^[:alnum:]]*$//' \
        | cut -c1-48)"
    [ -z "$h" ] && h="host"
    printf '%s' "$h"
}

am_machine_stem() {
    local p
    p="$(am_platform)"; [ -z "$p" ] && p="unknown"
    printf '%s-%s' "$(am_hostname)" "$p"
}

am_legacy_agent_name() {
    local slot; slot="$(am_slot "${1:-1}")" || return 1
    printf '%s-%s' "$(am_machine_stem)" "$slot"
}

# Remember the name the server actually granted, keyed by project, client, and
# stable client slot.
#
# Only session_start.sh writes this, from register_agent's response. It is a
# separate file from credentials.json on purpose: that one is keyed by agent
# name, which is precisely the thing in question here.
am_granted_name_file() {
    local client slot project_component
    client="$(am_client "${2:-}")" || return 1
    slot="$(am_slot "${3:-1}")" || return 1
    project_component="$(am_state_component "$1")" || return 1
    printf '%s/granted/%s--%s-%s' "$AM_STATE_DIR" \
        "$project_component" \
        "$client" "$slot"
}

# Read-only filename used by the immediately previous installer generation.
# New writes always use the hashed form above.
am_previous_granted_name_file() {
    local client slot
    client="$(am_client "${2:-}")" || return 1
    slot="$(am_slot "${3:-1}")" || return 1
    printf '%s/granted/%s--%s-%s' "$AM_STATE_DIR" \
        "$(printf '%s' "$1" | tr '/' '_' | tr -cd '[:alnum:]._-' | cut -c1-96)" \
        "$client" "$slot"
}

am_transitional_granted_name_file() {
    local alias slot project_component
    alias="$(am_transitional_client_alias "${2:-}")" || return 1
    slot="$(am_slot "${3:-1}")" || return 1
    project_component="$(am_state_component "$1")" || return 1
    printf '%s/granted/%s--%s-%s' "$AM_STATE_DIR" \
        "$project_component" \
        "$alias" "$slot"
}

am_previous_transitional_granted_name_file() {
    local alias slot
    alias="$(am_transitional_client_alias "${2:-}")" || return 1
    slot="$(am_slot "${3:-1}")" || return 1
    printf '%s/granted/%s--%s-%s' "$AM_STATE_DIR" \
        "$(printf '%s' "$1" | tr '/' '_' | tr -cd '[:alnum:]._-' | cut -c1-96)" \
        "$alias" "$slot"
}

am_granted_name_put() {
    local f lock_dir tmp rc=1
    f="$(am_granted_name_file "$1" "${3:-}" "${4:-1}")" || return 1
    [ -n "${2:-}" ] || return 0
    mkdir -p "$(dirname "$f")" 2>/dev/null || return 1
    lock_dir="${f}.lock"
    am_lock_acquire "$lock_dir" || return 1
    tmp="${f}.${BASHPID:-$$}.tmp"
    if printf '%s' "$2" > "$tmp" 2>/dev/null; then
        chmod 600 "$tmp" 2>/dev/null || true
        mv -f "$tmp" "$f" 2>/dev/null && rc=0
    fi
    [ "$rc" -eq 0 ] || rm -f "$tmp" 2>/dev/null || true
    am_lock_release "$lock_dir"
    return "$rc"
}

# The previous hook generation keyed one granted-name file only by project and
# derived identities as <host>-<platform>-<slot>.  Returning the old filename is
# read-only: migration must be an explicit operator action after the server-side
# Agent row has been renamed in place.
am_legacy_granted_name_file() {
    printf '%s/granted/%s' "$AM_STATE_DIR" \
        "$(printf '%s' "$1" | tr '/' '_' | tr -cd '[:alnum:]._-' | cut -c1-96)"
}

# A user-level hook is present in every repository the client opens, but Agent
# Mail is not.  Treat a repository as active only when local private state
# already proves it was onboarded, or the repository carries an explicit
# opt-in marker.  This gate is deliberately local-only: asking the server
# whether a project exists would itself leak every arbitrary checkout into the
# global hook's network traffic and could reintroduce implicit registration.
am_project_has_local_state() {
    local project="$1" client slot granted previous_granted
    local transitional_granted previous_transitional_granted legacy_granted
    client="$(am_client "${2:-}")" || return 1
    slot="$(am_slot "${3:-1}")" || return 1

    if [ -r "$AM_CRED_FILE" ] && jq -e --arg project "$project" '
        type == "object"
        and ((.[$project]? // null) | type == "object")
        and any(.[$project][]?; type == "string" and length > 0)
    ' "$AM_CRED_FILE" >/dev/null 2>&1; then
        return 0
    fi

    # A granted-name file can survive an interrupted credential write.  Check
    # the exact current client/slot file and the one legacy project-only shape;
    # do not glob the lossy filename encoding and risk activating a colliding
    # project.
    granted="$(am_granted_name_file "$project" "$client" "$slot")" || return 1
    [ -s "$granted" ] && return 0
    previous_granted="$(am_previous_granted_name_file "$project" "$client" "$slot")" || return 1
    [ -s "$previous_granted" ] && return 0
    transitional_granted="$(am_transitional_granted_name_file "$project" "$client" "$slot" 2>/dev/null || true)"
    [ -n "$transitional_granted" ] && [ -s "$transitional_granted" ] && return 0
    previous_transitional_granted="$(am_previous_transitional_granted_name_file "$project" "$client" "$slot" 2>/dev/null || true)"
    [ -n "$previous_transitional_granted" ] && [ -s "$previous_transitional_granted" ] && return 0
    legacy_granted="$(am_legacy_granted_name_file "$project")"
    [ -s "$legacy_granted" ] && return 0
    return 1
}

am_repository_opted_in() {
    local hint="${1:-.}" root marker discovery
    root="$(git -C "$hint" rev-parse --show-toplevel 2>/dev/null)" || return 1
    [ -n "$root" ] || return 1

    marker="${root}/.agent-mail-project-id"
    # The committed identity marker is meaningful only when it contains an id.
    [ -r "$marker" ] && grep -Eq '[^[:space:]]' "$marker" 2>/dev/null && return 0

    discovery="${root}/.agent-mail.yaml"
    # Discovery YAML is an opt-in only when it declares the documented project
    # identity field.  Mere presence of an empty or unrelated YAML file must not
    # turn on a global hook.
    [ -r "$discovery" ] && awk '
        /^project_uid[[:space:]]*:/ {
            value = $0
            sub(/^project_uid[[:space:]]*:[[:space:]]*/, "", value)
            sub(/[[:space:]]+#.*/, "", value)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            lowered = tolower(value)
            if (value != "" && value != "\"\"" && value != "\047\047" &&
                value != "|" && value != ">" && value != "~" && lowered != "null") {
                found = 1
            }
        }
        END { exit(found ? 0 : 1) }
    ' "$discovery" 2>/dev/null && return 0
    return 1
}

am_project_is_active() {
    local project="$1" client="${2:-}" slot="${3:-1}" hint="${4:-.}"
    [ "${AM_PATH_CONFIGURATION_VALID:-0}" = "1" ] || return 1
    [ -n "$project" ] || return 1
    am_project_has_local_state "$project" "$client" "$slot" && return 0
    am_repository_opted_in "$hint"
}

am_project_activation_message() {
    local project="$1"
    if [ "${AM_PATH_CONFIGURATION_VALID:-0}" != "1" ]; then
        printf '%s' \
            "Agent Mail is disabled because AGENT_MAIL_ENV_FILE or AGENT_MAIL_STATE_DIR is not an absolute path usable by this execution environment. No Agent Mail network request was made and no Agent was registered. Use a POSIX absolute path under Linux/macOS/WSL or a Windows absolute path under Git Bash/WSL."
        return 0
    fi
    printf '%s' \
        "Agent Mail is not activated for ${project}. No Agent Mail network request was made and no Agent was registered. Explicitly onboard this repository first, or add a non-empty .agent-mail-project-id (or .agent-mail.yaml with project_uid) at its Git root. The global hook will not create projects or identities merely because a client opened a repository."
}

# Print "old<TAB>new" and return 0 when registering the client-scoped name could
# fork an existing legacy identity.  Return 1 when local state contains no such
# evidence or already contains a usable credential for the resolved new name.
# No network call and no state mutation happen here.
am_identity_migration_pair() {
    local project="$1" client slot stem new resolved gf previous_gf
    local short_client short_gf previous_short_gf old_gf candidate_file
    local legacy_name old_order_name short_order_name old=""
    client="$(am_client "${2:-}")" || return 1
    slot="$(am_slot "${3:-1}")" || return 1
    stem="$(am_machine_stem)"
    new="${client}-$(am_platform)-$(am_hostname)-${slot}"
    legacy_name="${stem}-${slot}"
    old_order_name="${stem}-${client}-${slot}"
    short_client="$(am_transitional_client_alias "$client" 2>/dev/null || true)"
    short_order_name="${stem}-${short_client}-${slot}"

    # A token under the final name proves that the server and local credential
    # cutover already completed.  A remembered OLD value without that token is
    # handled below and remains fail-closed.
    [ -n "$(am_cred_get "$project" "$new")" ] && return 1

    gf="$(am_granted_name_file "$project" "$client" "$slot")" || return 1
    previous_gf="$(am_previous_granted_name_file "$project" "$client" "$slot")" || return 1
    short_gf="$(am_transitional_granted_name_file "$project" "$client" "$slot" 2>/dev/null || true)"
    previous_short_gf="$(am_previous_transitional_granted_name_file "$project" "$client" "$slot" 2>/dev/null || true)"

    # Exact project/client/slot state may contain any of the three predecessor
    # formats, or a random adjective+noun granted by an older server.  An entry
    # for another numeric slot is ignored rather than reassigned to this slot.
    set -- "$gf" "$previous_gf"
    [ -n "$short_gf" ] && set -- "$@" "$short_gf"
    [ -n "$previous_short_gf" ] && set -- "$@" "$previous_short_gf"
    for candidate_file in "$@"; do
        [ -r "$candidate_file" ] || continue
        resolved="$(tr -d '\r\n' < "$candidate_file" 2>/dev/null)"
        [ -n "$resolved" ] || continue
        [ "$resolved" = "$new" ] && continue
        case "$resolved" in
            "$legacy_name"|"$old_order_name"|"$short_order_name") old="$resolved" ;;
            "${stem}-"[1-9]* )
                case "${resolved#${stem}-}" in *[!0-9]*) old="$resolved" ;; esac ;;
            "${stem}-${client}-"[1-9]*|"${stem}-${short_client}-"[1-9]*)
                # The exact current-slot forms matched above.  Do not let an
                # old slot 2 marker migrate slot 1.
                ;;
            *) old="$resolved" ;;
        esac
        [ -n "$old" ] && break
    done

    old_gf="$(am_legacy_granted_name_file "$project")"
    if [ -z "$old" ] && [ -r "$old_gf" ]; then
        resolved="$(tr -d '\r\n' < "$old_gf" 2>/dev/null)"
        case "$resolved" in
            "$legacy_name"|"$old_order_name"|"$short_order_name") old="$resolved" ;;
            "${stem}-"[1-9]* )
                case "${resolved#${stem}-}" in *[!0-9]*) old="$resolved" ;; esac ;;
            ?*) old="$resolved" ;;
        esac
    fi
    if [ -z "$old" ] && [ -r "$AM_CRED_FILE" ]; then
        for resolved in "$legacy_name" "$old_order_name" "$short_order_name"; do
            [ -n "$(am_cred_get "$project" "$resolved")" ] && { old="$resolved"; break; }
        done
    fi
    [ -n "$old" ] || return 1
    [ "$old" != "$new" ] || return 1
    printf '%s\t%s' "$old" "$new"
    return 0
}

am_identity_migration_message() {
    local old="$1" new="$2"
    printf '%s' \
        "Agent Mail: MIGRATION IN PROGRESS — registration is suspended (${old} -> ${new}). This session has NO Agent Mail identity: mail will not be delivered, reservations will not be filed, and conflict warnings are unavailable. No Agent Mail network request was made and no new Agent was registered. Safe order: (1) rename the existing server Agent row in place from ${old} to ${new}, preserving Agent.id and its registration_token; (2) migrate the local credentials.json key and the client/slot granted-name entry to ${new}; (3) restart this client. Do not register ${new} before steps 1 and 2, and do not copy the token to a second identity."
}

am_agent_name() {
    local client slot canonical
    client="$(am_client "${1:-}")" || return 1
    slot="$(am_slot "${2:-1}")" || return 1
    canonical="${client}-$(am_platform)-$(am_hostname)-${slot}"
    # A usable canonical credential wins first; otherwise a previously granted
    # name wins over re-deriving one.
    #
    # The server does not always give you the name you asked for.  Collisions
    # and older server policy can return a random name; before hotfix b43c156,
    # the model-name heuristic also coerced valid explicit IDs containing a
    # model substring.  Everything then came apart, and measurably so — one host
    # ran three sessions and got three identities and three credentials entries:
    #
    #   session_start:22   am_cred_get  … "$AGENT"      <- the DERIVED name
    #   session_start:75   am_cred_put  … "$got_name"   <- the GRANTED name
    #
    # written under one key, read back under another. The next session finds no
    # token, registers afresh, is renamed afresh, and the loop has no fixed
    # point. autoreserve.sh and inbox_check.sh look up by the derived name too,
    # so a renamed agent silently loses reservations, conflict warnings and mail
    # — every one of them exiting 0.
    #
    # Fixed here rather than in each hook because they all call this function,
    # so they all inherit the answer without being touched.  Canonical address
    # identity is deliberately not overridable; personality belongs in the
    # server-side display/preferred name instead.
    # The project is resolved here rather than passed in, so that every existing
    # caller — autoreserve.sh, inbox_check.sh, session_end.sh — inherits this
    # without being edited.
    #
    # The directory test comes FIRST, and that ordering is the whole point. The
    # original wrote "they already call am_project_key themselves; this is one
    # more read of the same git config, not a new kind of work" — true, and
    # answering the wrong question. It is a SECOND read, in a fresh subprocess,
    # and home-win-1 measured it at 122 ms on Windows against 5 ms here. The
    # comment justified the absence of a new KIND of work where the question was
    # about AMOUNT.
    #
    # Memoising am_project_key cannot help: every caller invokes it as
    # `$(am_project_key)`, so a cache assigned inside dies with the subshell —
    # verified with a minimal case before writing this.
    #
    # What does help is not asking. This lookup only has an answer once a session
    # has recorded a granted name, so until then the project key is bought and
    # thrown away on every hook invocation. `[ -d ]` costs ~0.25 ms.
    if [ -r "$AM_CRED_FILE" ] || [ -d "${AM_STATE_DIR}/granted" ]; then
      local _proj; _proj="${AM_PROJECT_FOR_NAME:-$(am_project_key)}"
      if [ -n "$_proj" ]; then
        # A credential under the canonical address proves that the server-side
        # cutover completed.  It must beat any stale predecessor marker left by
        # an interrupted local migration; otherwise SessionStart resolves OLD,
        # misses the canonical token and can recreate the retired identity.
        if [ -n "$(am_cred_get "$_proj" "$canonical")" ]; then
            printf '%s' "$canonical"
            return 0
        fi
        [ -d "${AM_STATE_DIR}/granted" ] || { printf '%s' "$canonical"; return 0; }
        local gf; gf="$(am_granted_name_file "$_proj" "$client" "$slot")"
        if [ ! -r "$gf" ]; then
            local previous_gf
            previous_gf="$(am_previous_granted_name_file "$_proj" "$client" "$slot")"
            [ -r "$previous_gf" ] && gf="$previous_gf"
        fi
        if [ -r "$gf" ]; then
            local g stem; g="$(cat "$gf" 2>/dev/null)"; stem="$(am_machine_stem)"
            case "$g" in
                "${stem}-"[1-9]* )
                    case "${g#${stem}-}" in
                        *[!0-9]*) ;;
                        *) [ "$g" = "${stem}-${slot}" ] || g="" ;;
                    esac ;;
            esac
            [ -n "$g" ] && { printf '%s' "$g"; return 0; }
        fi
      fi
    fi
    printf '%s' "$canonical"
}

# The model this session is actually running, for the agent's profile.
#
# This used to be the literal "opus-5" written into session_start.sh, which made
# the field wrong on every host not running that model — not stale after a
# switch, but wrong from the first second, and wrong with the same confidence as
# a correct value. A profile field that lies is worse than one left blank,
# because nothing downstream can tell the two apart.
#
# The transcript is the only source that carries this today. home-win-1 captured
# a live PostToolUse payload and enumerated all twelve top-level keys — cwd,
# duration_ms, effort, hook_event_name, permission_mode, prompt_id, session_id,
# tool_input, tool_name, tool_response, tool_use_id, transcript_path — and none
# of them is the model. The two payload probes below are therefore dead today,
# and are kept only because the hook contract is not ours to fix: if `model` is
# ever added, in either shape, this starts working with no change here. They are
# not evidence that the payload works.
#
# The consequence is worth stating plainly: at SessionStart on a FRESH session
# there is no source at all — no payload field, and no assistant turn in the
# transcript yet — so registration records "unknown". am_sync_model is what
# replaces it with a real id after the first turn. That is not a refinement of
# this function; it is the only path by which a new session ever gets a true
# value.
#
# Every assistant turn records `message.model`, and reading the LAST one is what
# makes a mid-session `/model` switch visible.
#
# Never returns empty: the server rejects an empty model (EMPTY_MODEL), so a
# blank here would fail registration outright and cost the session all
# coordination. "unknown" is the honest answer when there is nothing to read,
# and it is distinguishable from a real id, which "opus-5" was not.
am_model_id() {
    local m tp
    m="$(am_payload_field '(.model|strings)')"
    [ -z "$m" ] && m="$(am_payload_field '.model.id')"
    if [ -z "$m" ]; then
        tp="$(am_payload_field '.transcript_path')"
        if [ -n "$tp" ] && [ -f "$tp" ]; then
            # The tail first, and this is not premature: transcripts reach tens
            # of megabytes in a long session, where a full jq scan measured
            # ~85 ms against ~3 ms for the last 200 lines. That gap does not
            # matter once at SessionStart; it matters a great deal to
            # am_sync_model, which runs after every tool call.
            #
            # Last match, not first: the newest turn is the current model.
            m="$(tail -n 200 "$tp" 2>/dev/null \
                 | jq -r 'select(.message.model != null) | .message.model' 2>/dev/null | tail -n 1)"
            # A long run of tool results carries no assistant turn, so the tail
            # can legitimately hold no model at all. Falling back to the whole
            # file keeps the cheap path from turning a slow answer into a wrong
            # one.
            [ -z "$m" ] && m="$(jq -r 'select(.message.model != null) | .message.model' \
                                "$tp" 2>/dev/null | tail -n 1)"
        fi
    fi
    m="$(printf '%s' "$m" | tr -cd '[:alnum:]._-' | cut -c1-128)"
    [ -z "$m" ] && m="unknown"
    printf '%s' "$m"
}

# Re-register only when the running model has actually changed.
#
# SessionStart alone cannot answer this: it fires once, and on a fresh session it
# fires BEFORE the first assistant turn exists, so the value it records is the
# best guess available at the least informative moment. A `/model` switch
# afterwards leaves the profile confidently wrong for the rest of the session.
#
# The cached value is what keeps this off the network. Unchanged is the
# overwhelmingly common case and costs one file read; the round trip happens only
# on an actual change.
#
# "unknown" is skipped rather than sent. At SessionStart there is no choice —
# the server rejects an empty model — but here a previously recorded real id
# already exists, and replacing it with "unknown" because a tail read came up
# short would destroy information rather than correct it.
#
# The cache is written only after the call succeeds, so a failed sync retries on
# the next hook instead of recording a change that never reached the server.
# Returns 0 always: this runs inside hooks, and bookkeeping must never fail the
# session it is only supposed to describe.
# $4 is the program, defaulting to claude-code. That default is correct today and
# will not stay correct by itself: `session_start.sh` is copied ONLY by
# integrate_claude_code.sh (measured — codex installs codex_notify.sh, gemini and
# factory-droid install check_inbox.sh, and neither of those calls register_agent
# at all), so every present caller of this really is Claude Code.
#
# It is a parameter rather than another literal because this function lives in
# the file every hook sources, including the two that ship with other CLIs. The
# first non-Claude hook to call it would otherwise assert "claude-code" about
# itself — silently, and with a value indistinguishable from a measured one.
# That is the same defect that made `model` wrong for a year, and putting it back
# one line away from where it was just fixed would be hard to defend.
am_sync_model() {
    local proj="$1" agent="$2" tok="$3" prog="${4:-claude-code}" want have cache
    want="$(am_model_id)"
    case "$want" in ''|unknown) return 0 ;; esac
    # Separators MAPPED, not deleted — the same rule as am_granted_name_file, and
    # missing here because that fix was made two functions away and this path was
    # never looked at. `tr -cd` alone drops slashes rather than replacing them, so
    # `/a/b` and `/ab` collapse onto one cache file.
    #
    # Milder than the identity collision that motivated the other fix: the worst
    # case is a skipped or redundant model refresh, which the next real change
    # repairs by itself. Fixed anyway, because two derivations of the same kind of
    # key disagreeing is how the next reader learns the wrong rule.
    cache="${AM_STATE_DIR}/model/$(am_state_component "${proj}|${agent}")" || return 0
    have="$(cat "$cache" 2>/dev/null || true)"
    [ "$want" = "$have" ] && return 0
    mkdir -p "$(dirname "$cache")" 2>/dev/null || return 0
    if am_call register_agent "$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$tok" \
            jq -nc --arg p "$proj" --arg n "$agent" \
            --arg m "$want" --arg g "$prog" \
            '{project_key:$p,name:$n,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,program:$g,model:$m}')" \
            >/dev/null 2>&1; then
        printf '%s' "$want" > "$cache" 2>/dev/null || true
    fi
    return 0
}

am_bearer() {
    printf '%s' "${AGENT_MAIL_TOKEN:-${HTTP_BEARER_TOKEN:-}}"
}

# --- server calls ------------------------------------------------------------
# The STATELESS mount: a one-shot JSON-RPC POST needs no initialize handshake and
# returns in ~40ms, where the stateful /mcp mount would cost three round trips
# and leak a session per hook invocation.
# The bearer as a curl config file, so it never appears in argv. am_get can
# take the config on stdin; am_call cannot, because stdin carries the body.
# A file is the remaining way in: curl on Windows rejects /dev/fd/N and named
# pipes ("error encountered when reading a file"), accepting only a real file.
#
# Written once per session, not per call — the token does not change mid-session,
# so this is one write, not a temp file in the critical path. It lands in
# AM_STATE_DIR beside credentials.json, which already holds registration tokens
# under the same permissions; note this does put the bearer in a second place on
# disk, where before it lived only in ~/.agent-mail.env.
#
# Returns nothing if the file cannot be written; am_call then swaps which of the
# two stdin users gives way, rather than degrading to -H. A hook that cannot
# hide a token would still be better than a hook that fails, but it turns out
# not to be a choice we have to make.
am_hdr_conf() {
    local f="${AM_STATE_DIR}/curl-headers.conf" want cur tmp
    want="$(printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\nheader = "Accept: application/json, text/event-stream"' "$1")"
    cur="$(cat "$f" 2>/dev/null)"
    if [ "$cur" != "$want" ]; then
        mkdir -p "$AM_STATE_DIR" 2>/dev/null || return 0
        tmp="${f}.$$"
        printf '%s\n' "$want" > "$tmp" 2>/dev/null || return 0
        chmod 600 "$tmp" 2>/dev/null
        mv -f "$tmp" "$f" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 0; }
    fi
    printf '%s' "$f"
}

# Outcome of am_call / am_get, reported through the exit status:
#
#   0  the server answered 2xx; stdout is the result, and empty means empty
#   1  no answer at all — DNS, refused, timeout, a proxy that hung up
#   2  the server answered, with a status that is not 2xx
#
# Deliberately NOT a variable. Every call site is `x="$(am_call …)"`, which runs
# the function in a subshell, so anything assigned inside is gone by the time
# the caller looks — a status variable would read as empty at exactly the
# moment it mattered and would be believed. The exit status is the one channel
# that survives command substitution.
#
# 1 and 2 are kept apart because they call for different responses: 1 is worth
# retrying, 2 usually means the credential or the request is wrong and retrying
# will fail the same way.
am_http_status() {
    local code
    code="$(printf '%s' "$1" | tail -n1)"
    case "$code" in
        2??) return 0 ;;
        ''|*[!0-9]*|000) return 1 ;;
        *) return 2 ;;
    esac
}

# Escape a value for a curl config file. ORDER IS LOad-BEARING: backslashes
# first, quotes second. Reversed, the second pass doubles the backslashes the
# first pass just introduced (`"` -> `\"` -> `\\"`), curl reads a literal
# backslash followed by the end of the value, and the request dies as a JSON
# parse error at the server. Measured on Linux and Windows; the failure looks
# like a server problem and is not one.
am_conf_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

am_call() {
    local tool="$1" args="$2" bearer body hdr raw
    [ "${AM_PATH_CONFIGURATION_VALID:-0}" = "1" ] || return 1
    bearer="$(am_bearer)"
    # No bearer is NOT a reason to stay quiet. Send the request without the
    # header instead: a server that requires one answers a loud 401 (rc=2 via
    # am_http_status), and a deployment that deliberately runs unauthenticated
    # keeps working. Returning success with no output — as this did — made an
    # unconfigured machine indistinguishable from a healthy quiet one, which is
    # the failure this whole layer exists to remove.
    :
    # Two encoding defences, on purpose. The server accepts UTF-8 fine — the
    # corruption was ours: on Windows the argv crossing into native curl.exe
    # re-encodes text through the console codepage, turning valid UTF-8 into
    # legacy-codepage bytes the server rightly refuses (400). Verified
    # byte-for-byte on the affected machine: the identical body fails as
    # --data ARG and passes via stdin. So the body goes via stdin, which
    # removes that boundary — and -a escapes non-ASCII to \uXXXX anyway,
    # because ASCII cannot be mis-encoded even by a boundary nobody has
    # found yet.
    #
    # Worth doing even though the hooks themselves only ever send file paths and
    # agent names: the refusal arrives as an empty result below, indistinguishable
    # from "nothing to report", so an agent sending a non-English message loses
    # it with no error anywhere.
    # $args arrives on stdin, not as --argjson. Arguments can carry a whole
    # message body, and an argument is part of the command line: Windows caps
    # an entire command line at 32767 bytes, so past that jq cannot be started
    # and the call fails before a single byte is sent. Measured at 44 kB for
    # ten ordinary messages, so this is not a theoretical ceiling. Reading the
    # document from stdin removes it on every platform.
    #
    # The ceiling still binds wherever a CALLER builds $args with `--arg`, which
    # is fine for hooks (their largest argument is a 43-byte token) but not for
    # anything sending long prose.
    body="$(printf '%s' "$args" | jq -ac --arg t "$tool" \
        '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:$t,arguments:.}}' 2>/dev/null)" || return 0
    [ -z "$body" ] && return 1
    # The body stays on stdin whenever there is a header file to read the
    # credentials from, which keeps it clear of both argv and config escaping.
    hdr="$(am_hdr_conf "$bearer")"
    if [ -n "$hdr" ]; then
        raw="$(printf '%s' "$body" | curl -s --max-time "$AM_TIMEOUT" -X POST "${AM_BASE_URL}/api/" \
            -K "$hdr" --data @- --write-out '\n%{http_code}' 2>/dev/null)"
    else
        # No writable state dir. The body cannot go to argv here: every call
        # this module makes carries `registration_token` in its arguments, so
        # moving the body out of stdin would take the bearer off the process
        # table and put an agent credential on it instead — a swap, not a fix.
        # Both therefore travel in the config on stdin.
        #
        # That means escaping the body for the config format, and the order in
        # am_conf_escape is what makes it survive quotes and backslashes
        # together. Only this branch pays that cost, and only when the state
        # directory is unwritable.
        raw="$(printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\nheader = "Accept: application/json, text/event-stream"\ndata = "%s"\n' \
            "$bearer" "$(am_conf_escape "$body")" \
            | curl -s --max-time "$AM_TIMEOUT" -X POST "${AM_BASE_URL}/api/" \
                -K - --write-out '\n%{http_code}' 2>/dev/null)"
    fi
    # Separate "the server said there is nothing" from "nothing came back".
    # Until now both arrived as an empty string, so a deploy window or a dropped
    # link was indistinguishable from a quiet morning — and worse than quiet:
    # autoreserve reads silence as "reservation filed" while reservations_warn
    # reads it as "no conflict", so two agents editing one file are each assured
    # they are alone. The status goes on its own LAST line and is cut before jq,
    # because folding it into the body would break the parse this exists to
    # protect.
    am_http_status "$raw" || return $?
    # A refused tool call is an HTTP 200. The JSON-RPC envelope succeeded; the
    # tool inside it did not, and says so in .result.isError. Reading only the
    # status therefore lets an invalid registration_token through as a normal
    # answer — the caller gets an English error sentence where it expected a
    # JSON array, fails its own type check, and exits quietly. A wrong
    # credential is currently the most silent failure of the lot.
    raw="$(printf '%s' "$raw" | sed '$d')"
    # A JSON-RPC envelope error sits BESIDE .result, not inside it, so a check
    # that only reads .result.isError sees `.result` absent, `// empty`, and
    # hands the caller an empty string — a protocol failure arriving as
    # "answered 2xx, nothing to report".
    if printf '%s' "$raw" | jq -e 'has("error") and (.error | type) == "object"' >/dev/null 2>&1; then
        printf '%s' "$raw" | jq -r '.error.message // "the server rejected the request"' 2>/dev/null
        return 2
    fi
    if printf '%s' "$raw" | jq -e '.result.isError // false' >/dev/null 2>&1; then
        # The reason goes to stdout so the caller can quote it. Server text like
        # "Invalid registration_token for agent X" is the single most actionable
        # thing this layer ever produces, and dropping it would leave the agent
        # knowing only that something failed.
        printf '%s' "$raw" | jq -r '.result.content[0].text // "the server reported an error"' 2>/dev/null
        return 2
    fi
    # `content` is an EMPTY ARRAY when a tool succeeds and matches nothing — an
    # inbox with no unread, a topic filter with no hits, an agent with no
    # contacts. `.content[0].text` has nothing to take, so this used to print
    # nothing and return 0: a successful call rendered byte-identical to a failed
    # one. Measured against the live server, which answers correctly:
    #
    #     {"result":{"content":[],"structuredContent":{"result":[]},"isError":false}}
    #
    # so the loss was ours, not the server's. `structuredContent.result` carries
    # the real answer — `[]` — and emitting it lets a caller tell "asked, nothing
    # matched" from "could not ask", which is the distinction every hook here
    # exists to preserve. Falls back in that order and stays silent only when the
    # server truly sent neither.
    printf '%s' "$raw" | jq -r '
        if (.result.content[0].text? // null) != null then .result.content[0].text
        elif (.result.structuredContent.result? // null) != null then
            (.result.structuredContent.result | tojson)
        else empty end' 2>/dev/null
}

# Turn an am_call/am_get outcome into a sentence a caller can put in front of
# the agent. Kept here so the four hooks phrase it identically: an agent that
# sees the same wording twice learns it once.
am_failure_reason() {
    case "$1" in
        1) printf 'the server did not answer' ;;
        2) if [ -n "${2:-}" ]; then printf 'the server refused: %s' "$2"
           else printf 'the server refused the request'; fi ;;
        *) printf 'the call failed' ;;
    esac
}

# Percent-encode a query value so it can cross argv into native curl.exe.
# The same boundary that mangles am_call's body mangles --data-urlencode's
# VALUE: Windows re-encodes argv through the ANSI codepage, so a reserved
# path with any non-ASCII in it queries as legacy bytes, never matches, and
# the conflict warning silently does not come. Percent-encoded output is
# pure ASCII, which no codepage can damage. The value reaches jq via stdin,
# never argv.
am_urlencode() {
    printf '%s' "$1" | jq -sRr '@uri' 2>/dev/null
}

# Callers must pass query strings pre-encoded with am_urlencode
# (--data "k=$(am_urlencode "$v")"), NOT --data-urlencode "k=$v" — the raw
# value would be mis-encoded on argv before curl ever sees it.
#
# The bearer goes in a config read from stdin, not -H in argv, because the
# process table publishes argv to anyone who can run ps: on Linux
# /proc/PID/cmdline is world-readable, so any other account on the host reads
# the token live; on macOS it is owner-only, which still defeats the 0600 on
# ~/.agent-mail.env, since a routine `ps aux` during a session pulls the
# bearer straight into the model's context and the transcript. At two GETs
# per edit that window is not rare. Config on stdin is also the one form that
# crosses no argv boundary at all, so it needs no encoding defence.
am_get() {
    local path="$1" bearer raw
    [ "${AM_PATH_CONFIGURATION_VALID:-0}" = "1" ] || return 1
    bearer="$(am_bearer)"
    # No bearer is NOT a reason to stay quiet. Send the request without the
    # header instead: a server that requires one answers a loud 401 (rc=2 via
    # am_http_status), and a deployment that deliberately runs unauthenticated
    # keeps working. Returning success with no output — as this did — made an
    # unconfigured machine indistinguishable from a healthy quiet one, which is
    # the failure this whole layer exists to remove.
    :
    raw="$(printf 'header = "Authorization: Bearer %s"\n' "$bearer" \
      | curl -s --max-time "$AM_TIMEOUT" -G -K - \
        "${@:2}" "${AM_BASE_URL}${path}" --write-out '\n%{http_code}' 2>/dev/null)"
    # See am_call: an unanswered request must not be reported as an empty
    # answer. This one feeds the reservation warning, where "no rows" and "no
    # reply" would otherwise both render as "nobody holds this file".
    am_http_status "$raw" || return $?
    printf '%s' "$raw" | sed '$d'
}

# --- credential store --------------------------------------------------------
# Re-registering an existing name REQUIRES the token the first registration
# returned; the server will not reissue it. Losing this file orphans the identity
# permanently, hence the atomic write.
am_cred_get() {
    [ -r "$AM_CRED_FILE" ] || return 0
    jq -r --arg p "$1" --arg a "$2" '.[$p][$a] // empty' "$AM_CRED_FILE" 2>/dev/null
}

am_lock_has_live_publisher() {
    local lock_dir="$1" claim_prefix claim_file claim_owner
    claim_prefix="${lock_dir}.claim."
    for claim_file in "${claim_prefix}"*; do
        [ -e "$claim_file" ] || continue
        claim_owner="${claim_file#"$claim_prefix"}"
        case "$claim_owner" in ''|*[!0-9]*) continue ;; esac
        kill -0 "$claim_owner" 2>/dev/null && return 0
    done
    return 1
}

am_lock_atomic_claim() {
    local lock_dir="$1" owner="$2"
    mkdir -p "$(dirname "$lock_dir")" 2>/dev/null || return 1
    # The directory is only a container. Some cross-platform mkdir
    # implementations report success for a concurrent EEXIST, so the
    # noclobber-created pid file is the sole acquisition linearization point.
    mkdir "$lock_dir" 2>/dev/null || true
    [ -d "$lock_dir" ] || return 1
    (
        set -o noclobber
        printf '%s' "$owner" > "$lock_dir/pid"
    ) 2>/dev/null
}

am_lock_acquire() {
    local lock_dir="$1" self_owner observed_owner current_owner
    local tries=0 empty_tries=0
    local recovery_dir="${1}.recovery" claim_file
    self_owner="${BASHPID:-$$}"
    claim_file="${lock_dir}.claim.${self_owner}"
    mkdir -p "$(dirname "$lock_dir")" 2>/dev/null || return 1
    while :; do
        # Publish intent before mkdir. The PID is encoded in the directory entry
        # itself, so another process can observe the publisher even if this one
        # is descheduled between mkdir(lock) and writing lock/pid. This closes
        # the empty-lock ABA window without making the recovery mutex itself a
        # permanent gate for every healthy acquisition.
        : > "$claim_file" 2>/dev/null || return 1
        if am_lock_atomic_claim "$lock_dir" "$self_owner"; then
            rm -f "$claim_file" 2>/dev/null || true
            return 0
        fi
        rm -f "$claim_file" 2>/dev/null || true

        # Recover a lock whose recorded owner no longer exists. An empty lock can
        # be the tiny window between mkdir and writing pid, so give it one second
        # before treating it as the residue of a crash in that window. A live
        # publisher claim always wins over that timeout.
        if [ -r "$lock_dir/pid" ]; then
            observed_owner="$(tr -cd '0-9' < "$lock_dir/pid" 2>/dev/null)"
            if [ -n "$observed_owner" ] \
                && ! kill -0 "$observed_owner" 2>/dev/null; then
                # Serialize stale-owner recovery independently of the lock
                # being repaired. Without this persistent mutex, two healers
                # can both observe the dead pid; after one removes the lock and
                # a successor acquires it, the other can delete that successor's
                # pid (an ABA race). Never auto-heal this recovery mutex: doing
                # so would recursively recreate the same race one level down.
                if am_lock_atomic_claim "$recovery_dir" "$self_owner"; then
                    current_owner="$(tr -cd '0-9' \
                        < "$lock_dir/pid" 2>/dev/null)"
                    if [ "$current_owner" = "$observed_owner" ] \
                        && ! kill -0 "$current_owner" 2>/dev/null \
                        && ! am_lock_has_live_publisher "$lock_dir"; then
                        rm -f "$lock_dir/pid" 2>/dev/null || true
                        rmdir "$lock_dir" 2>/dev/null || true
                    fi
                    am_lock_release "$recovery_dir"
                    continue
                fi
            fi
            if [ -n "$observed_owner" ]; then
                empty_tries=0
            else
                empty_tries=$((empty_tries + 1))
            fi
        else
            empty_tries=$((empty_tries + 1))
        fi
        if [ "$empty_tries" -ge 20 ]; then
            if am_lock_atomic_claim "$recovery_dir" "$self_owner"; then
                # Re-read beneath the recovery mutex. A successor may have
                # populated the pid or may still be publishing it after this
                # waiter first saw an empty directory; either must remain.
                current_owner="$(tr -cd '0-9' < "$lock_dir/pid" 2>/dev/null)"
                if [ -z "$current_owner" ] \
                    && ! am_lock_has_live_publisher "$lock_dir"; then
                    rm -f "$lock_dir/pid" 2>/dev/null || true
                    rmdir "$lock_dir" 2>/dev/null || true
                fi
                am_lock_release "$recovery_dir"
            fi
            empty_tries=0
            continue
        fi
        tries=$((tries + 1))
        [ "$tries" -lt 200 ] || return 1
        sleep 0.05
    done
}

am_lock_release() {
    local lock_dir="$1"
    # pid is the lock. If a successor claims it between unlink and rmdir, the
    # non-empty directory makes rmdir fail without touching that successor.
    rm -f "$lock_dir/pid" 2>/dev/null || true
    rmdir "$lock_dir" 2>/dev/null || true
}

am_cred_put() {
    local project="$1" agent="$2" token="$3" tmp lock_dir rc=1
    [ -z "$token" ] && return 0
    mkdir -p "$AM_STATE_DIR" 2>/dev/null || return 1
    chmod 700 "$AM_STATE_DIR" 2>/dev/null
    lock_dir="${AM_CRED_FILE}.lock"
    if ! am_lock_acquire "$lock_dir"; then
        printf 'Agent Mail could not lock credential store: %s\n' "$AM_CRED_FILE" >&2
        return 1
    fi
    tmp="${AM_CRED_FILE}.${BASHPID:-$$}.tmp"
    if [ -r "$AM_CRED_FILE" ]; then
        if ! jq -e 'type == "object"' "$AM_CRED_FILE" >/dev/null 2>&1; then
            printf 'Agent Mail credential store is invalid JSON; refusing to overwrite: %s\n' "$AM_CRED_FILE" >&2
        elif AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" \
            jq --arg p "$project" --arg a "$agent" \
            '.[$p] = ((.[$p] // {}) | .[$a] = env.AGENT_MAIL_JQ_REGISTRATION_TOKEN)' \
            "$AM_CRED_FILE" >"$tmp" 2>/dev/null; then
            rc=0
        fi
    else
        if AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" \
            jq -n --arg p "$project" --arg a "$agent" \
            '{($p): {($a): env.AGENT_MAIL_JQ_REGISTRATION_TOKEN}}' \
            >"$tmp" 2>/dev/null; then
            rc=0
        fi
    fi
    if [ "$rc" -eq 0 ]; then
        chmod 600 "$tmp" 2>/dev/null
        if ! mv -f "$tmp" "$AM_CRED_FILE" 2>/dev/null; then
            rc=1
        fi
    fi
    [ "$rc" -eq 0 ] || rm -f "$tmp" 2>/dev/null || true
    am_lock_release "$lock_dir"
    return "$rc"
}

# Establish one deterministic durable mailbox in a project from a lifecycle
# hook. The per-identity lock closes the fresh-registration race: concurrent
# cross-project edit hooks re-check credentials after acquiring it instead of
# asking the server to issue the same one-time registration token twice.
am_ensure_agent_credential() {
    local project="$1" agent="$2" client="$3" slot="$4" program="$5" model="$6"
    local key lock_dir token args response rc got_name got_token retired
    [ -n "$project" ] && [ -n "$agent" ] && [ -n "$client" ] \
        && [ -n "$slot" ] && [ -n "$program" ] && [ -n "$model" ] || return 1
    token="$(am_cred_get "$project" "$agent")"
    if [ -n "$token" ]; then
        printf '%s' "$token"
        return 0
    fi
    key="$(am_state_component "${project}|${agent}")" || return 1
    lock_dir="${AM_STATE_DIR}/registration/${key}.lock"
    am_lock_acquire "$lock_dir" || return 1
    token="$(am_cred_get "$project" "$agent")"
    if [ -n "$token" ]; then
        am_lock_release "$lock_dir"
        printf '%s' "$token"
        return 0
    fi
    am_call ensure_project \
        "$(jq -nc --arg key "$project" '{human_key:$key}')" >/dev/null
    rc=$?
    if [ "$rc" -ne 0 ]; then
        am_lock_release "$lock_dir"
        return "$rc"
    fi
    args="$(jq -nc --arg project "$project" --arg name "$agent" \
        --arg program "$program" --arg model "$model" \
        '{project_key:$project,name:$name,program:$program,model:$model}')"
    response="$(am_call register_agent "$args")"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        am_lock_release "$lock_dir"
        return "$rc"
    fi
    got_name="$(printf '%s' "$response" | jq -r '.name // empty' 2>/dev/null)"
    got_token="$(printf '%s' "$response" | jq -r \
        '.registration_token // empty' 2>/dev/null)"
    retired="$(printf '%s' "$response" | jq -r '.retired_at // empty' 2>/dev/null)"
    if [ "$got_name" != "$agent" ] || [ -z "$got_token" ] || [ -n "$retired" ] \
        || ! am_granted_name_put "$project" "$got_name" "$client" "$slot" \
        || ! am_cred_put "$project" "$got_name" "$got_token"; then
        am_lock_release "$lock_dir"
        return 1
    fi
    am_lock_release "$lock_dir"
    printf '%s' "$got_token"
}

# --- execution lifecycle -----------------------------------------------------
# Agent is the durable mailbox.  A CLI session or native subagent is an
# AgentExecution beneath that mailbox, and reservations belong to that
# execution.  Keeping the native lifecycle id in private state lets repeated
# SessionStart events (resume/compact) reuse one active execution without
# turning every terminal or subagent into another permanent Agent row.
am_execution_state_file() {
    local project="$1" agent="$2" client="$3" session_id="$4"
    local kind="$5" native_id="$6" generation="${7:-}" component
    if [ -z "$generation" ]; then
        generation="$(am_session_lifecycle_generation "$client" "$session_id")" \
            || return 1
    fi
    if [ "$generation" -eq 1 ]; then
        component="$(am_state_component \
            "${project}|${agent}|${client}|${session_id}|${kind}|${native_id}")" \
            || return 1
    else
        component="$(am_state_component \
            "${project}|${agent}|${client}|${session_id}|generation:${generation}|${kind}|${native_id}")" \
            || return 1
    fi
    printf '%s/executions/%s.json' "$AM_STATE_DIR" "$component"
}

am_execution_token_file() {
    local state_file
    state_file="$(am_execution_state_file "$@")" || return 1
    printf '%s.token' "$state_file"
}

# One lifecycle manifest is the only enumeration surface for execution state.
# Native hooks may touch several projects and spawn many children, but a hook
# for one client/session/generation must never walk another lifecycle's files.
am_execution_manifest_file() {
    local client="$1" session_id="$2" generation="$3" component
    [ -n "$client" ] && [ -n "$session_id" ] || return 1
    case "$generation" in ''|*[!0-9]*) return 1 ;; esac
    component="$(am_state_component \
        "${client}|${session_id}|generation:${generation}")" || return 1
    printf '%s/execution-manifests/%s.json' "$AM_STATE_DIR" "$component"
}

am_execution_manifest_add() {
    local client="$1" session_id="$2" generation="$3" state_file="$4"
    local manifest_file state_name lock_dir tmp rc=1
    local intent_file intent_lock intent_state
    manifest_file="$(am_execution_manifest_file \
        "$client" "$session_id" "$generation")" || return 1
    case "$state_file" in
        "${AM_STATE_DIR}/executions/"*.json) ;;
        *) return 1 ;;
    esac
    state_name="${state_file##*/}"
    case "$state_name" in ''|*/*|*\\*|*.json.json) return 1 ;; esac
    case "$state_name" in *.json) ;; *) return 1 ;; esac
    mkdir -p "$(dirname "$manifest_file")" 2>/dev/null || return 1
    chmod 700 "$(dirname "$manifest_file")" 2>/dev/null
    intent_file="$(am_session_end_intent_file "$client" "$session_id")" \
        || return 1
    mkdir -p "$(dirname "$intent_file")" 2>/dev/null || return 1
    chmod 700 "$(dirname "$intent_file")" 2>/dev/null

    # Enrollment and SessionEnd serialize on the lifecycle tombstone. If this
    # registration wins, SessionEnd sees the manifest entry; if SessionEnd
    # wins, no late active manifest can be left behind after its enumeration.
    intent_lock="${intent_file}.lock"
    am_lock_acquire "$intent_lock" || return 1
    intent_state="$(cat "$intent_file" 2>/dev/null)"
    if [ -n "$intent_state" ] \
        && ! printf '%s' "$intent_state" | jq -e \
            --arg client "$client" --arg session_id "$session_id" \
            --argjson generation "$generation" '
              .version == 2 and .client == $client and
              .session_id == $session_id and
              .generation == $generation and .status == "active"
            ' >/dev/null 2>&1; then
        am_lock_release "$intent_lock"
        return 1
    fi
    lock_dir="${manifest_file}.lock"
    if ! am_lock_acquire "$lock_dir"; then
        am_lock_release "$intent_lock"
        return 1
    fi
    tmp="${manifest_file}.${BASHPID:-$$}.tmp"
    if [ -r "$manifest_file" ]; then
        if jq -e --arg client "$client" --arg session_id "$session_id" \
            --argjson generation "$generation" '
              .version == 1 and .client == $client and
              .session_id == $session_id and
              .lifecycle_generation == $generation and
              (.state_files | type == "array")
            ' "$manifest_file" >/dev/null 2>&1 \
            && jq --arg state_file "$state_name" '
              .state_files = ((.state_files + [$state_file]) | unique) |
              .updated_at = (now | todateiso8601)
            ' "$manifest_file" > "$tmp" 2>/dev/null; then
            rc=0
        fi
    elif jq -nc --arg client "$client" --arg session_id "$session_id" \
        --argjson generation "$generation" --arg state_file "$state_name" '
          {version:1,client:$client,session_id:$session_id,
           lifecycle_generation:$generation,status:"active",
           state_files:[$state_file],updated_at:(now | todateiso8601)}
        ' > "$tmp" 2>/dev/null; then
        rc=0
    fi
    if [ "$rc" -eq 0 ]; then
        if ! chmod 600 "$tmp" 2>/dev/null \
            || ! mv -f "$tmp" "$manifest_file" 2>/dev/null; then
            rc=1
        fi
    fi
    [ "$rc" -eq 0 ] || rm -f "$tmp" 2>/dev/null || true
    am_lock_release "$lock_dir"
    am_lock_release "$intent_lock"
    return "$rc"
}

am_execution_manifest_state_files() {
    local client="$1" session_id="$2" generation="$3" manifest_file name
    manifest_file="$(am_execution_manifest_file \
        "$client" "$session_id" "$generation")" || return 1
    [ -r "$manifest_file" ] || return 0
    while IFS= read -r name; do
        case "$name" in ''|*/*|*\\*) continue ;; esac
        case "$name" in *.json) ;; *) continue ;; esac
        printf '%s/executions/%s\n' "$AM_STATE_DIR" "$name"
    done < <(
        jq -r --arg client "$client" --arg session_id "$session_id" \
            --argjson generation "$generation" '
              select(.version == 1 and .client == $client and
                     .session_id == $session_id and
                     .lifecycle_generation == $generation and
                     (.state_files | type == "array")) |
              .state_files[] | select(type == "string")
            ' "$manifest_file" 2>/dev/null
    )
}

am_execution_heartbeat_stamp_file() {
    local project="$1" agent="$2" execution_id="$3" component
    [ -n "$project" ] && [ -n "$agent" ] && [ -n "$execution_id" ] \
        || return 1
    component="$(am_state_component \
        "${project}|${agent}|${execution_id}")" || return 1
    printf '%s/executions/heartbeat-%s.stamp' "$AM_STATE_DIR" "$component"
}

# A provider SessionEnd is a lifecycle-wide barrier, not merely a snapshot of
# execution files that happen to exist at that instant.  PostToolUse hooks run
# concurrently with SessionEnd, so a first touch in another project can still
# be enrolling while the end hook scans private state.  Persist one tombstone
# for the exact client/native session before that scan; every later start checks
# it before creating state and again after the unlocked server RPC.
am_session_end_intent_file() {
    local client="$1" session_id="${2:-}" component
    [ -n "$session_id" ] || session_id="$(am_payload_field '.session_id')"
    [ -n "$client" ] && [ -n "$session_id" ] || return 1
    component="$(am_state_component "${client}|${session_id}")" || return 1
    printf '%s/session-end-intents/%s.json' "$AM_STATE_DIR" "$component"
}

# Return the current local lifecycle generation for one provider session. A
# Codex conversation can emit SessionEnd after it has been idle and later emit
# SessionStart(source=resume) with the same session_id. The provider id therefore
# identifies the durable conversation, not one bounded server execution.
am_session_lifecycle_generation() {
    local client="$1" session_id="${2:-}" intent_file generation
    [ -n "$session_id" ] || session_id="$(am_payload_field '.session_id')"
    intent_file="$(am_session_end_intent_file "$client" "$session_id")" \
        || return 1
    generation="$(jq -r '
        if .version == 2 and (.generation | type) == "number" and
           .generation >= 1 and (.generation | floor) == .generation
        then .generation
        elif .version == 1 and .status == "ended" then 1
        else empty end
      ' "$intent_file" 2>/dev/null)"
    [ -n "$generation" ] || generation=1
    printf '%s' "$generation"
}

# Begin or resume the exact local lifecycle generation. This never removes the
# SessionEnd barrier: a genuine resume advances it atomically and marks the new
# generation active, so an in-flight start from the prior generation observes
# that it has been superseded and cannot publish an active marker afterwards.
am_session_run_begin() {
    local client="$1" source="${2:-}" session_id intent_file lock_dir state
    local generation status tmp rc=1
    session_id="$(am_payload_field '.session_id')"
    [ -n "$client" ] && [ -n "$session_id" ] || return 1
    intent_file="$(am_session_end_intent_file "$client" "$session_id")" \
        || return 1
    mkdir -p "$(dirname "$intent_file")" 2>/dev/null || return 1
    chmod 700 "$(dirname "$intent_file")" 2>/dev/null
    lock_dir="${intent_file}.lock"
    am_lock_acquire "$lock_dir" || return 1
    state="$(cat "$intent_file" 2>/dev/null)"
    generation="$(printf '%s' "$state" | jq -r '
        if .version == 2 and (.generation | type) == "number" and
           .generation >= 1 and (.generation | floor) == .generation
        then .generation
        elif .version == 1 and .status == "ended" then 1
        else empty end
      ' 2>/dev/null)"
    status="$(printf '%s' "$state" | jq -r '.status // empty' 2>/dev/null)"
    [ -n "$generation" ] || generation=1
    if [ "$status" = "ended" ]; then
        if [ "$source" != "resume" ]; then
            am_lock_release "$lock_dir"
            return 1
        fi
        generation=$((generation + 1))
    elif [ "$status" = "active" ]; then
        am_lock_release "$lock_dir"
        printf '%s' "$generation"
        return 0
    elif [ -n "$state" ]; then
        am_lock_release "$lock_dir"
        return 1
    fi
    tmp="${intent_file}.${BASHPID:-$$}.tmp"
    if jq -nc --arg client "$client" --arg session_id "$session_id" \
        --argjson generation "$generation" \
        '{version:2,client:$client,session_id:$session_id,
          generation:$generation,status:"active",
          started_at:(now | todateiso8601)}' > "$tmp" 2>/dev/null \
        && chmod 600 "$tmp" 2>/dev/null \
        && mv -f "$tmp" "$intent_file" 2>/dev/null; then
        rc=0
    fi
    am_lock_release "$lock_dir"
    [ "$rc" -eq 0 ] || return "$rc"
    printf '%s' "$generation"
}

am_session_end_intent_mark() {
    local client="$1" session_id intent_file lock_dir tmp state generation
    session_id="$(am_payload_field '.session_id')"
    [ -n "$session_id" ] || return 1
    intent_file="$(am_session_end_intent_file "$client" "$session_id")" \
        || return 1
    mkdir -p "$(dirname "$intent_file")" 2>/dev/null || return 1
    chmod 700 "$(dirname "$intent_file")" 2>/dev/null
    lock_dir="${intent_file}.lock"
    am_lock_acquire "$lock_dir" || return 1
    state="$(cat "$intent_file" 2>/dev/null)"
    generation="$(printf '%s' "$state" | jq -r '
        if .version == 2 and (.generation | type) == "number" and
           .generation >= 1 and (.generation | floor) == .generation
        then .generation
        elif .version == 1 and .status == "ended" then 1
        else empty end
      ' 2>/dev/null)"
    [ -n "$generation" ] || generation=1
    tmp="${intent_file}.${BASHPID:-$$}.tmp"
    if ! jq -nc --arg client "$client" --arg session_id "$session_id" \
        --argjson generation "$generation" \
        '{version:2,client:$client,session_id:$session_id,
          generation:$generation,status:"ended",
          ended_at:(now | todateiso8601)}' > "$tmp" 2>/dev/null \
        || ! chmod 600 "$tmp" 2>/dev/null \
        || ! mv -f "$tmp" "$intent_file" 2>/dev/null; then
        am_lock_release "$lock_dir"
        return 1
    fi
    am_lock_release "$lock_dir"
    printf '%s' "$generation"
    return 0
}

am_session_end_intent_exists() {
    local client="$1" session_id="${2:-}" expected_generation="${3:-}"
    local intent_file
    [ -n "$session_id" ] || session_id="$(am_payload_field '.session_id')"
    intent_file="$(am_session_end_intent_file "$client" "$session_id")" \
        || return 1
    jq -e --arg client "$client" --arg session_id "$session_id" \
        --arg expected_generation "$expected_generation" '
        .client == $client and .session_id == $session_id and
        ((if .version == 2 then .generation
          elif .version == 1 and .status == "ended" then 1
          else null end) as $generation |
         $generation != null and
         (if $expected_generation == "" then .status == "ended"
          else ($generation != ($expected_generation | tonumber)) or
               .status == "ended" end))
      ' "$intent_file" >/dev/null 2>&1
}

# Rewrite a confirmed terminal state to the small non-secret resume/audit
# record, then destroy the raw execution capability and heartbeat throttle.
# The caller holds the exact state-file lock. Heartbeat takes that lock before
# its final stamp write, so it cannot recreate a stamp after this cleanup.
am_execution_terminalize_local_locked() {
    local state_file="$1" end_status="$2" end_source="${3:-server}"
    local project agent execution_id stamp token_file tmp
    case "$end_status" in completed|failed|cancelled|expired) ;;
        *) return 1 ;;
    esac
    [ -r "$state_file" ] || return 1
    project="$(jq -r '.project // empty' "$state_file" 2>/dev/null)"
    agent="$(jq -r '.agent // empty' "$state_file" 2>/dev/null)"
    execution_id="$(jq -r '.execution_id // empty' "$state_file" 2>/dev/null)"
    stamp="$(am_execution_heartbeat_stamp_file \
        "$project" "$agent" "$execution_id" 2>/dev/null)"
    token_file="${state_file}.token"
    tmp="${state_file}.${BASHPID:-$$}.tmp"
    if ! jq --arg status "$end_status" --arg end_source "$end_source" '
          {version:(.version // 1),project,agent,client,session_id,
           lifecycle_generation:(.lifecycle_generation // 1),kind,native_id,
           external_id,parent_execution_id,execution_id,
           status:$status,end_source:$end_source,
           ended_locally_at:(now | todateiso8601)} |
          with_entries(select(.value != null and .value != ""))
        ' "$state_file" > "$tmp" 2>/dev/null \
        || ! chmod 600 "$tmp" 2>/dev/null \
        || ! mv -f "$tmp" "$state_file" 2>/dev/null; then
        rm -f "$tmp" 2>/dev/null || true
        return 1
    fi
    rm -f "$token_file" 2>/dev/null || true
    [ -z "$stamp" ] || rm -f "$stamp" 2>/dev/null || true
    return 0
}

am_execution_resume_horizon_seconds() {
    local horizon="${AGENT_MAIL_EXECUTION_RESUME_HORIZON_SECONDS:-2592000}"
    case "$horizon" in ''|*[!0-9]*) horizon=2592000 ;; esac
    # A sub-day horizon is too short for delayed provider events and suspended
    # laptops. Operators may lengthen this but cannot accidentally erase the
    # race barrier immediately.
    [ "$horizon" -ge 86400 ] || horizon=86400
    printf '%s' "$horizon"
}

am_execution_manifest_mark_terminal() {
    local client="$1" session_id="$2" generation="$3" state_file status
    local manifest_file intent_file manifest_name intent_name lock_dir tmp
    local now horizon retain_until bucket marker_dir marker_file marker_tmp
    while IFS= read -r state_file; do
        [ -r "$state_file" ] || continue
        status="$(jq -r '.status // empty' "$state_file" 2>/dev/null)"
        case "$status" in completed|failed|cancelled|expired) ;;
            *) return 1 ;;
        esac
    done < <(am_execution_manifest_state_files \
        "$client" "$session_id" "$generation")

    manifest_file="$(am_execution_manifest_file \
        "$client" "$session_id" "$generation")" || return 1
    [ -r "$manifest_file" ] || return 0
    intent_file="$(am_session_end_intent_file "$client" "$session_id")" \
        || return 1
    now="$(date +%s 2>/dev/null)"
    case "$now" in ''|*[!0-9]*) return 1 ;; esac
    horizon="$(am_execution_resume_horizon_seconds)" || return 1
    retain_until=$((now + horizon))
    bucket=$((retain_until / 86400))
    manifest_name="${manifest_file##*/}"
    intent_name="${intent_file##*/}"

    lock_dir="${manifest_file}.lock"
    am_lock_acquire "$lock_dir" || return 1
    tmp="${manifest_file}.${BASHPID:-$$}.tmp"
    if ! jq --argjson retain_until "$retain_until" '
          .status = "terminal" |
          .ended_at = (now | todateiso8601) |
          .retain_until_epoch = $retain_until
        ' "$manifest_file" > "$tmp" 2>/dev/null \
        || ! chmod 600 "$tmp" 2>/dev/null \
        || ! mv -f "$tmp" "$manifest_file" 2>/dev/null; then
        rm -f "$tmp" 2>/dev/null || true
        am_lock_release "$lock_dir"
        return 1
    fi

    marker_dir="${AM_STATE_DIR}/execution-retention/${bucket}"
    marker_file="${marker_dir}/${manifest_name}"
    if ! mkdir -p "$marker_dir" 2>/dev/null; then
        am_lock_release "$lock_dir"
        return 1
    fi
    chmod 700 "$(dirname "$marker_dir")" "$marker_dir" 2>/dev/null
    marker_tmp="${marker_file}.${BASHPID:-$$}.tmp"
    if ! jq -nc --arg client "$client" --arg session_id "$session_id" \
        --argjson generation "$generation" --arg manifest "$manifest_name" \
        --arg intent "$intent_name" --argjson retain_until "$retain_until" '
          {version:1,client:$client,session_id:$session_id,
           lifecycle_generation:$generation,manifest_file:$manifest,
           intent_file:$intent,retain_until_epoch:$retain_until}
        ' > "$marker_tmp" 2>/dev/null \
        || ! chmod 600 "$marker_tmp" 2>/dev/null \
        || ! mv -f "$marker_tmp" "$marker_file" 2>/dev/null; then
        rm -f "$marker_tmp" 2>/dev/null || true
        am_lock_release "$lock_dir"
        return 1
    fi
    am_lock_release "$lock_dir"
    return 0
}

am_execution_retention_prune_marker() {
    local marker_file="$1" now="$2" client session_id generation retain_until
    local manifest_name intent_name manifest_file intent_file state_file state_lock
    local state_status state_generation project agent execution_id stamp manifest_lock
    client="$(jq -r '.client // empty' "$marker_file" 2>/dev/null)"
    session_id="$(jq -r '.session_id // empty' "$marker_file" 2>/dev/null)"
    generation="$(jq -r '.lifecycle_generation // empty' "$marker_file" 2>/dev/null)"
    retain_until="$(jq -r '.retain_until_epoch // empty' "$marker_file" 2>/dev/null)"
    manifest_name="$(jq -r '.manifest_file // empty' "$marker_file" 2>/dev/null)"
    intent_name="$(jq -r '.intent_file // empty' "$marker_file" 2>/dev/null)"
    case "$generation" in ''|*[!0-9]*) return 1 ;; esac
    case "$retain_until" in ''|*[!0-9]*) return 1 ;; esac
    [ "$retain_until" -le "$now" ] || return 1
    case "$manifest_name" in ''|*/*|*\\*) return 1 ;; esac
    case "$intent_name" in ''|*/*|*\\*) return 1 ;; esac
    manifest_file="${AM_STATE_DIR}/execution-manifests/${manifest_name}"
    intent_file="${AM_STATE_DIR}/session-end-intents/${intent_name}"
    [ -r "$manifest_file" ] || {
        rm -f "$marker_file" 2>/dev/null || true
        return 0
    }
    manifest_lock="${manifest_file}.lock"
    am_lock_acquire "$manifest_lock" || return 1
    if ! jq -e --arg client "$client" --arg session_id "$session_id" \
        --argjson generation "$generation" --argjson retain_until "$retain_until" '
          .version == 1 and .client == $client and
          .session_id == $session_id and
          .lifecycle_generation == $generation and .status == "terminal" and
          .retain_until_epoch == $retain_until
        ' "$manifest_file" >/dev/null 2>&1; then
        am_lock_release "$manifest_lock"
        return 1
    fi

    while IFS= read -r state_file; do
        [ -r "$state_file" ] || continue
        state_lock="${state_file}.lock"
        if ! am_lock_acquire "$state_lock"; then
            am_lock_release "$manifest_lock"
            return 1
        fi
        state_status="$(jq -r '.status // empty' "$state_file" 2>/dev/null)"
        state_generation="$(jq -r '.lifecycle_generation // 1' \
            "$state_file" 2>/dev/null)"
        if [ "$state_generation" != "$generation" ]; then
            am_lock_release "$state_lock"
            am_lock_release "$manifest_lock"
            return 1
        fi
        case "$state_status" in completed|failed|cancelled|expired) ;;
            *)
                am_lock_release "$state_lock"
                am_lock_release "$manifest_lock"
                return 1
                ;;
        esac
        project="$(jq -r '.project // empty' "$state_file" 2>/dev/null)"
        agent="$(jq -r '.agent // empty' "$state_file" 2>/dev/null)"
        execution_id="$(jq -r '.execution_id // empty' "$state_file" 2>/dev/null)"
        stamp="$(am_execution_heartbeat_stamp_file \
            "$project" "$agent" "$execution_id" 2>/dev/null)"
        rm -f "${state_file}.token" "$state_file" 2>/dev/null || true
        [ -z "$stamp" ] || rm -f "$stamp" 2>/dev/null || true
        am_lock_release "$state_lock"
    done < <(am_execution_manifest_state_files \
        "$client" "$session_id" "$generation")

    if jq -e --argjson generation "$generation" \
        --argjson retain_until "$retain_until" '
          .lifecycle_generation == $generation and .status == "terminal" and
          .retain_until_epoch == $retain_until
        ' "$manifest_file" >/dev/null 2>&1; then
        rm -f "$manifest_file" 2>/dev/null || true
    else
        am_lock_release "$manifest_lock"
        return 1
    fi
    am_lock_release "$manifest_lock"

    # The tombstone is shared by resumed generations. Delete it only when it
    # still describes this exact ended generation; an active/newer run wins.
    if [ -r "$intent_file" ]; then
        state_lock="${intent_file}.lock"
        if am_lock_acquire "$state_lock"; then
            if jq -e --arg client "$client" --arg session_id "$session_id" \
                --argjson generation "$generation" '
                  .client == $client and .session_id == $session_id and
                  .generation == $generation and .status == "ended"
                ' "$intent_file" >/dev/null 2>&1; then
                rm -f "$intent_file" 2>/dev/null || true
            fi
            am_lock_release "$state_lock"
        fi
    fi
    rm -f "$marker_file" 2>/dev/null || true
    return 0
}

# Retention work is independently bounded. The date buckets keep directory
# fanout small; each hook processes at most one configured batch of due markers.
am_execution_retention_prune() {
    local root="${AM_STATE_DIR}/execution-retention" now today batch processed=0
    local bucket bucket_name remaining marker
    [ -d "$root" ] || return 0
    now="$(date +%s 2>/dev/null)"
    case "$now" in ''|*[!0-9]*) return 0 ;; esac
    today=$((now / 86400))
    batch="${AGENT_MAIL_EXECUTION_RETENTION_GC_BATCH:-64}"
    case "$batch" in ''|*[!0-9]*) batch=64 ;; esac
    [ "$batch" -ge 1 ] || batch=1
    [ "$batch" -le 256 ] || batch=256
    while IFS= read -r bucket; do
        bucket_name="${bucket##*/}"
        case "$bucket_name" in ''|*[!0-9]*) continue ;; esac
        [ "$bucket_name" -le "$today" ] || continue
        remaining=$((batch - processed))
        [ "$remaining" -gt 0 ] || break
        while IFS= read -r marker; do
            [ -r "$marker" ] || continue
            am_execution_retention_prune_marker "$marker" "$now" \
                >/dev/null 2>&1 || true
            processed=$((processed + 1))
            [ "$processed" -lt "$batch" ] || break
        done < <(find "$bucket" -maxdepth 1 -type f -name '*.json' \
            -print 2>/dev/null | head -n "$remaining")
        rmdir "$bucket" 2>/dev/null || true
        [ "$processed" -lt "$batch" ] || break
    done < <(find "$root" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null)
    rmdir "$root" 2>/dev/null || true
    return 0
}

am_session_external_id() {
    local native_id="$1" generation="$2" candidate digest
    if [ "$generation" -eq 1 ]; then
        printf '%s' "$native_id"
        return 0
    fi
    candidate="${native_id}#run-${generation}"
    if [ "${#candidate}" -le 255 ]; then
        printf '%s' "$candidate"
        return 0
    fi
    digest="$(printf '%s' "$native_id" | am_sha256)" || return 1
    printf 'session:%s:%s' "$generation" "$digest"
}

# Prove that SessionStart established this provider lifecycle somewhere before a
# later PostToolUse is allowed to lazily enroll another opted-in project. This
# prevents a missed/failed root start from being silently replaced by an edit
# hook registration in the same checkout.
am_session_has_root_execution_state() {
    local client="$1" session_id generation state_file
    session_id="$(am_payload_field '.session_id')"
    [ -n "$client" ] && [ -n "$session_id" ] || return 1
    generation="$(am_session_lifecycle_generation "$client" "$session_id")" \
        || return 1
    while IFS= read -r state_file; do
        [ -r "$state_file" ] || continue
        if jq -e --arg client "$client" --arg session_id "$session_id" \
            --argjson generation "$generation" '
              .client == $client and .session_id == $session_id and
              (.lifecycle_generation // 1) == $generation and
              .kind == "session" and
              (.status == "starting" or .status == "active" or
               .status == "stopping" or .status == "end_requested")
            ' "$state_file" >/dev/null 2>&1; then
            return 0
        fi
    done < <(am_execution_manifest_state_files \
        "$client" "$session_id" "$generation")
    return 1
}

am_execution_git_metadata() {
    local cwd="$1" repo_root="" git_common_dir="" worktree_path=""
    local branch="" head_sha="" common_raw=""
    if [ -n "$cwd" ] && [ -d "$cwd" ] \
        && git -C "$cwd" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        repo_root="$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)"
        worktree_path="$repo_root"
        common_raw="$(git -C "$cwd" rev-parse --git-common-dir 2>/dev/null)"
        if [ -n "$common_raw" ]; then
            case "$common_raw" in
                /*|[A-Za-z]:[\\/]*) git_common_dir="$common_raw" ;;
                *)
                    git_common_dir="$(CDPATH= cd -- "$cwd/$common_raw" 2>/dev/null \
                        && pwd -P)" ;;
            esac
        fi
        branch="$(git -C "$cwd" symbolic-ref --quiet --short HEAD 2>/dev/null)"
        head_sha="$(git -C "$cwd" rev-parse --verify HEAD 2>/dev/null \
            | tr '[:upper:]' '[:lower:]')"
        case "$head_sha" in
            ''|*[!0-9a-f]*) head_sha="" ;;
            *) [ "${#head_sha}" -eq 40 ] || head_sha="" ;;
        esac
    fi
    jq -nc \
        --arg cwd "$cwd" \
        --arg repo_root "$repo_root" \
        --arg git_common_dir "$git_common_dir" \
        --arg worktree_path "$worktree_path" \
        --arg branch "$branch" \
        --arg head_sha "$head_sha" \
        '{cwd:$cwd,repo_root:$repo_root,git_common_dir:$git_common_dir,
          worktree_path:$worktree_path,branch:$branch,head_sha:$head_sha}'
}

# Private, per-worktree handoff for git guards. `git rev-parse --git-path`
# resolves into the worktree's own git dir for linked worktrees, so siblings do
# not overwrite one another. The marker is deliberately retained on stop as
# terminal audit state. A confirmed end has released that execution's automatic
# claims; explicit claims keep their TTL, and the next start atomically
# overwrites the marker without destructive cleanup in a lifecycle hook.
am_execution_marker_path() {
    local cwd="$1" git_path
    [ -n "$cwd" ] && [ -d "$cwd" ] || return 1
    git_path="$(git -C "$cwd" rev-parse --path-format=absolute --git-path \
        agent-mail/execution-id 2>/dev/null)" || return 1
    case "$git_path" in
        /*/agent-mail/execution-id|[A-Za-z]:[\\/]*agent-mail/execution-id) ;;
        *) return 1 ;;
    esac
    printf '%s' "$git_path"
}

am_execution_marker_write() {
    local cwd="$1" execution_id="$2" kind="$3" marker tmp worktree_path now
    local ancestor_execution_ids="${4:-[]}"
    local lock_dir rc=1
    printf '%s' "$execution_id" | grep -Eq \
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' \
        || return 1
    marker="$(am_execution_marker_path "$cwd")" || return 1
    worktree_path="$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)" \
        || return 1
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
    printf '%s' "$now" | grep -Eq \
        '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$' \
        || return 1
    printf '%s' "$ancestor_execution_ids" | jq -e \
        'type == "array" and all(.[]; type == "string")' \
        >/dev/null 2>&1 || ancestor_execution_ids='[]'
    mkdir -p "$(dirname "$marker")" 2>/dev/null || return 1
    chmod 700 "$(dirname "$marker")" 2>/dev/null
    lock_dir="${marker}.lock"
    am_lock_acquire "$lock_dir" || return 1
    tmp="${marker}.${BASHPID:-$$}.tmp"
    if jq -nc --arg execution_id "$execution_id" --arg kind "$kind" \
        --arg worktree_path "$worktree_path" --arg heartbeat_ts "$now" \
        --argjson ancestor_execution_ids "$ancestor_execution_ids" \
        '{execution_id:$execution_id,status:"active",kind:$kind,
          worktree_path:$worktree_path,heartbeat_ts:$heartbeat_ts,
          ancestor_execution_ids:$ancestor_execution_ids}' \
        > "$tmp" 2>/dev/null \
        && chmod 600 "$tmp" 2>/dev/null \
        && mv -f "$tmp" "$marker" 2>/dev/null; then
        rc=0
    fi
    am_lock_release "$lock_dir"
    return "$rc"
}

am_execution_marker_touch() {
    local cwd="$1" execution_id="$2" compatible_ids="${3:-[]}"
    local marker current_id tmp now lock_dir rc=1
    marker="$(am_execution_marker_path "$cwd")" || return 1
    [ -r "$marker" ] || return 1
    lock_dir="${marker}.lock"
    am_lock_acquire "$lock_dir" || return 1
    current_id="$(jq -r '.execution_id // empty' "$marker" 2>/dev/null)"
    if [ "$current_id" != "$execution_id" ] \
        && ! printf '%s' "$compatible_ids" | jq -e --arg id "$current_id" \
            'type == "array" and index($id) != null' >/dev/null 2>&1; then
        am_lock_release "$lock_dir"
        return 1
    fi
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
    if ! printf '%s' "$now" | grep -Eq \
        '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'; then
        am_lock_release "$lock_dir"
        return 1
    fi
    tmp="${marker}.${BASHPID:-$$}.tmp"
    if jq --arg heartbeat_ts "$now" \
        '.status = "active" | .heartbeat_ts = $heartbeat_ts' \
        "$marker" > "$tmp" 2>/dev/null \
        && chmod 600 "$tmp" 2>/dev/null \
        && mv -f "$tmp" "$marker" 2>/dev/null; then
        rc=0
    fi
    am_lock_release "$lock_dir"
    return "$rc"
}

am_execution_marker_end() {
    local cwd="$1" execution_id="$2" end_status="$3" marker current_id tmp
    local lock_dir rc=1
    marker="$(am_execution_marker_path "$cwd")" || return 1
    [ -r "$marker" ] || return 1
    lock_dir="${marker}.lock"
    am_lock_acquire "$lock_dir" || return 1
    current_id="$(jq -r '.execution_id // empty' "$marker" 2>/dev/null)"
    if [ "$current_id" != "$execution_id" ]; then
        am_lock_release "$lock_dir"
        return 1
    fi
    tmp="${marker}.${BASHPID:-$$}.tmp"
    if jq --arg status "$end_status" \
        'if $status == "unverified" and
            (.status == "completed" or .status == "failed" or
             .status == "cancelled" or .status == "expired")
         then . else .status = $status end' \
        "$marker" > "$tmp" 2>/dev/null \
        && chmod 600 "$tmp" 2>/dev/null \
        && mv -f "$tmp" "$marker" 2>/dev/null; then
        rc=0
    fi
    am_lock_release "$lock_dir"
    return "$rc"
}

# Publish an active marker while holding the matching lifecycle state lock.
# SessionEnd writes its tombstone independently and then waits on this same
# state lock, so either publication sees the barrier and is skipped, or the end
# path necessarily follows it and leaves the final marker terminal.
am_execution_marker_publish_active() {
    local project="$1" agent="$2" client="$3" kind="$4" native_id="$5"
    local cwd="$6" execution_id="$7" ancestors="${8:-[]}"
    local session_id state_file lock_dir rc=1 generation
    session_id="$(am_payload_field '.session_id')"
    generation="$(am_session_lifecycle_generation "$client" "$session_id")" \
        || return 1
    state_file="$(am_execution_state_file "$project" "$agent" "$client" \
        "$session_id" "$kind" "$native_id" "$generation")" || return 1
    [ -r "$state_file" ] || return 1
    lock_dir="${state_file}.lock"
    am_lock_acquire "$lock_dir" || return 1
    if ! am_session_end_intent_exists "$client" "$session_id" "$generation" \
        && jq -e --arg id "$execution_id" \
            '.status == "active" and .execution_id == $id' \
            "$state_file" >/dev/null 2>&1 \
        && am_execution_marker_write "$cwd" "$execution_id" "$kind" \
            "$ancestors"; then
        rc=0
    fi
    am_lock_release "$lock_dir"
    return "$rc"
}

am_execution_token_generate() {
    local token=""
    [ -r /dev/urandom ] || return 1
    token="$(LC_ALL=C od -An -N32 -tx1 /dev/urandom 2>/dev/null \
        | tr -d ' \r\n')" || return 1
    case "$token" in
        *[!0-9a-f]*|'') return 1 ;;
    esac
    [ "${#token}" -eq 64 ] || return 1
    printf '%s' "$token"
}

# Start or recover one execution and print its UUID.  A "starting" state is
# deliberately durable: if the hook dies after the server commit but before the
# local update, the next event retries the same external_id and the server's
# idempotency returns the same execution instead of creating a duplicate.
am_execution_start() {
    local project="$1" agent="$2" token="$3" client="$4" kind="$5"
    local native_id="$6" parent_execution_id="${7:-}"
    local parent_execution_token="${8:-}" task_description="${9:-}"
    local session_id cwd state_file token_file lock_dir state external_id metadata tmp
    local turn_id agent_type model permission_mode execution_token
    local args response rc execution_id status ancestors response_status reused
    local requested_end_status state_was_new=0 generation
    local existing_execution_id="" existing_marker_cwd=""
    session_id="$(am_payload_field '.session_id')"
    cwd="${AM_EXECUTION_CWD_OVERRIDE:-$(am_payload_field '.cwd')}"
    turn_id="$(am_payload_field '.turn_id')"
    agent_type="$(am_payload_field '.agent_type')"
    model="$(am_payload_field '.model')"
    permission_mode="$(am_payload_field '.permission_mode')"
    turn_id="$(printf '%s' "$turn_id" | cut -c1-255)"
    agent_type="$(printf '%s' "$agent_type" | cut -c1-128)"
    model="$(printf '%s' "$model" | cut -c1-128)"
    permission_mode="$(printf '%s' "$permission_mode" | cut -c1-64)"
    [ -n "$project" ] && [ -n "$agent" ] && [ -n "$token" ] \
        && [ -n "$client" ] && [ -n "$native_id" ] || return 1
    generation="$(am_session_lifecycle_generation "$client" "$session_id")" \
        || return 1
    state_file="$(am_execution_state_file "$project" "$agent" "$client" \
        "$session_id" "$kind" "$native_id" "$generation")" || return 1
    # Register the path before any state or server execution can exist. A
    # concurrent SessionEnd may then enumerate this exact lifecycle without a
    # global scan; if it observes the path before creation, the tombstone checks
    # below still prevent publication after the end barrier.
    am_execution_manifest_add "$client" "$session_id" "$generation" \
        "$state_file" || return 1
    # A completed native lifecycle cannot enroll a new project after its end
    # hook already took the state-file snapshot. Existing state is allowed
    # through so a crash-ambiguous start can recover its UUID and end exactly.
    if am_session_end_intent_exists "$client" "$session_id" "$generation" \
        && [ ! -r "$state_file" ]; then
        return 1
    fi
    token_file="${state_file}.token"
    mkdir -p "$(dirname "$state_file")" 2>/dev/null || return 1
    chmod 700 "$(dirname "$state_file")" 2>/dev/null
    lock_dir="${state_file}.lock"
    am_lock_acquire "$lock_dir" || return 1

    state="$(cat "$state_file" 2>/dev/null)"
    if printf '%s' "$state" | jq -e \
        'type == "object" and
         (.status == "active" or .status == "stopping") and
         (.execution_id | type == "string" and length > 0)' \
        >/dev/null 2>&1 \
        && grep -Eq '^[0-9a-f]{64}$' "$token_file" 2>/dev/null; then
        existing_execution_id="$(printf '%s' "$state" | jq -r '.execution_id')"
        existing_marker_cwd="$(printf '%s' "$state" | jq -r \
            '.worktree_path // .cwd // empty')"
    fi

    # Provider lifecycle ids are immutable audit identities. Replaying a start
    # after the matching stop must not manufacture a second execution for the
    # same native session/subagent id.
    if printf '%s' "$state" | jq -e \
        'type == "object" and
         (.status == "completed" or .status == "failed" or
          .status == "cancelled" or .status == "expired")' \
        >/dev/null 2>&1; then
        am_lock_release "$lock_dir"
        return 1
    fi

    # Any persisted lifecycle state without its original capability is not
    # recoverable by minting a replacement token: the server intentionally
    # rejects token rotation. This includes `starting` and `end_requested`,
    # because either may already refer to a committed server execution whose
    # response was lost. Fail closed for guard self-suppression and require a
    # fresh provider lifecycle instead.
    if printf '%s' "$state" | jq -e \
        'type == "object" and
         (.status == "starting" or .status == "active" or
          .status == "stopping" or .status == "end_requested")' \
        >/dev/null 2>&1 \
        && ! grep -Eq '^[0-9a-f]{64}$' "$token_file" 2>/dev/null; then
        execution_id="$(printf '%s' "$state" | jq -r '.execution_id // empty')"
        existing_marker_cwd="$(printf '%s' "$state" | jq -r \
            '.worktree_path // .cwd // empty')"
        am_lock_release "$lock_dir"
        am_execution_marker_end "$existing_marker_cwd" "$execution_id" \
            unverified >/dev/null 2>&1 || true
        return 1
    fi

    # Active/stopping state also requires the UUID returned by the server.
    if [ -z "$existing_execution_id" ] \
        && printf '%s' "$state" | jq -e \
            '(.status == "active" or .status == "stopping")' \
            >/dev/null 2>&1; then
        execution_id="$(printf '%s' "$state" | jq -r '.execution_id // empty')"
        existing_marker_cwd="$(printf '%s' "$state" | jq -r \
            '.worktree_path // .cwd // empty')"
        am_lock_release "$lock_dir"
        am_execution_marker_end "$existing_marker_cwd" "$execution_id" \
            unverified >/dev/null 2>&1 || true
        return 1
    fi

    if ! printf '%s' "$state" | jq -e \
        'type == "object" and
         (.status == "starting" or .status == "active" or
          .status == "stopping" or .status == "end_requested") and
         (.external_id | type == "string" and length > 0)' \
        >/dev/null 2>&1 \
        || ! grep -Eq '^[0-9a-f]{64}$' "$token_file" 2>/dev/null; then
        if [ "$kind" = "session" ]; then
            external_id="$(am_session_external_id "$native_id" "$generation")" \
                || {
                    am_lock_release "$lock_dir"
                    return 1
                }
        else
            external_id="$native_id"
        fi
        execution_token="$(am_execution_token_generate)" || {
            am_lock_release "$lock_dir"
            return 1
        }
        # The raw capability is written directly to its final private path.
        # State JSON updates use temporary files, so keeping the secret separate
        # guarantees it never appears in a marker, response, or *.tmp file.
        if ! printf '%s\n' "$execution_token" > "$token_file" 2>/dev/null \
            || ! chmod 600 "$token_file" 2>/dev/null; then
            am_lock_release "$lock_dir"
            return 1
        fi
        metadata="$(am_execution_git_metadata "$cwd")" || metadata='{}'
        state="$(jq -nc \
            --arg project "$project" --arg agent "$agent" --arg client "$client" \
            --arg session_id "$session_id" --arg kind "$kind" \
            --argjson lifecycle_generation "$generation" \
            --arg native_id "$native_id" --arg external_id "$external_id" \
            --arg parent_execution_id "$parent_execution_id" \
            --arg turn_id "$turn_id" --arg agent_type "$agent_type" \
            --arg model "$model" --arg permission_mode "$permission_mode" \
            --arg task_description "$task_description" --argjson git "$metadata" \
            '{version:1,project:$project,agent:$agent,client:$client,
              session_id:$session_id,lifecycle_generation:$lifecycle_generation,
              kind:$kind,native_id:$native_id,
              external_id:$external_id,
              parent_execution_id:$parent_execution_id,
              turn_id:$turn_id,agent_type:$agent_type,model:$model,
              permission_mode:$permission_mode,
              task_description:$task_description,status:"starting"} + $git')" || {
            am_lock_release "$lock_dir"
            return 1
        }
        tmp="${state_file}.${BASHPID:-$$}.tmp"
        if ! printf '%s\n' "$state" > "$tmp" 2>/dev/null \
            || ! chmod 600 "$tmp" 2>/dev/null \
            || ! mv -f "$tmp" "$state_file" 2>/dev/null; then
            am_lock_release "$lock_dir"
            return 1
        fi
        state_was_new=1
    fi
    execution_token="$(cat "$token_file" 2>/dev/null)"
    if ! printf '%s' "$execution_token" | grep -Eq '^[0-9a-f]{64}$'; then
        am_lock_release "$lock_dir"
        return 1
    fi
    if am_session_end_intent_exists "$client" "$session_id" "$generation"; then
        if [ "$state_was_new" -eq 1 ]; then
            # No start RPC has run for this new state. Close it locally and do
            # not manufacture a server execution after SessionEnd.
            if ! am_execution_terminalize_local_locked \
                "$state_file" completed session_end_intent; then
                am_lock_release "$lock_dir"
                return 1
            fi
            am_lock_release "$lock_dir"
            am_execution_manifest_mark_terminal \
                "$client" "$session_id" "$generation" >/dev/null 2>&1 || true
            am_execution_retention_prune >/dev/null 2>&1 || true
            return 1
        fi
        # An older `starting` record may already have committed server-side.
        # Preserve its capability and replay start only to recover the UUID;
        # active records can go directly through the exact end path below.
        tmp="${state_file}.${BASHPID:-$$}.tmp"
        if ! jq '.status = "end_requested" |
                .requested_end_status = "completed" |
                .end_requested_at = (now | todateiso8601)' \
            "$state_file" > "$tmp" 2>/dev/null \
            || ! chmod 600 "$tmp" 2>/dev/null \
            || ! mv -f "$tmp" "$state_file" 2>/dev/null; then
            am_lock_release "$lock_dir"
            return 1
        fi
        state="$(cat "$state_file" 2>/dev/null)"
    fi
    if printf '%s' "$state" | jq -e \
        '.status == "end_requested" and
         (.execution_id | type == "string" and length > 0)' \
        >/dev/null 2>&1; then
        requested_end_status="$(printf '%s' "$state" | jq -r \
            '.requested_end_status // "completed"' 2>/dev/null)"
        am_lock_release "$lock_dir"
        if am_execution_end "$project" "$agent" "$token" "$client" "$kind" \
            "$native_id" "$requested_end_status" "$generation" \
            >/dev/null 2>&1; then
            am_execution_manifest_mark_terminal \
                "$client" "$session_id" "$generation" >/dev/null 2>&1 || true
            am_execution_retention_prune >/dev/null 2>&1 || true
        fi
        return 1
    fi
    am_lock_release "$lock_dir"

    args="$(printf '%s' "$state" | \
        AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" \
        AGENT_MAIL_JQ_EXECUTION_TOKEN="$execution_token" \
        AGENT_MAIL_JQ_PARENT_EXECUTION_TOKEN="$parent_execution_token" jq -c '
          {project_key:.project,agent_name:.agent,
           registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,
           external_id:.external_id,client_name:.client,kind:.kind,
           execution_token:env.AGENT_MAIL_JQ_EXECUTION_TOKEN,
           lifecycle_protocol_version:1,
           turn_id:.turn_id,agent_type:.agent_type,model:.model,
           permission_mode:.permission_mode,
           task_description:.task_description,cwd:.cwd,repo_root:.repo_root,
           git_common_dir:.git_common_dir,worktree_path:.worktree_path,
           branch:.branch,head_sha:.head_sha}
          + (if .parent_execution_id == "" then {}
             else {parent_execution_id:.parent_execution_id,
                   parent_execution_token:env.AGENT_MAIL_JQ_PARENT_EXECUTION_TOKEN} end)
          | with_entries(select(.value != ""))
        ' 2>/dev/null)" || return 1
    [ -n "$args" ] || return 1
    response="$(am_call start_agent_execution "$args")"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        if [ -n "$existing_execution_id" ]; then
            am_execution_marker_end "$existing_marker_cwd" \
                "$existing_execution_id" unverified >/dev/null 2>&1 || true
        fi
        return "$rc"
    fi
    execution_id="$(printf '%s' "$response" | jq -r '.id // empty' 2>/dev/null)"
    response_status="$(printf '%s' "$response" | jq -r \
        '.status // empty' 2>/dev/null)"
    reused="$(printf '%s' "$response" | jq -r '.reused // false' 2>/dev/null)"
    if ! printf '%s' "$execution_id" | grep -Eq \
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' \
        || [ "$response_status" != "active" ] \
        || { [ -n "$existing_execution_id" ] \
             && { [ "$execution_id" != "$existing_execution_id" ] \
                  || [ "$reused" != "true" ]; }; }; then
        if [ -n "$existing_execution_id" ]; then
            am_execution_marker_end "$existing_marker_cwd" \
                "$existing_execution_id" unverified >/dev/null 2>&1 || true
        fi
        return 1
    fi

    if printf '%s' "$response" | jq -e \
        '(.ancestor_execution_ids // []) | type == "array"' \
        >/dev/null 2>&1; then
        ancestors="$(printf '%s' "$response" | jq -c \
            '.ancestor_execution_ids // []' 2>/dev/null)"
    else
        ancestors='[]'
    fi
    if [ "$kind" = "subagent" ] && [ "$ancestors" = "[]" ] \
        && [ -n "$parent_execution_id" ]; then
        ancestors="$(jq -nc --arg parent "$parent_execution_id" '[$parent]')"
    fi
    am_lock_acquire "$lock_dir" || return 1
    status="$(jq -r '.status // empty' "$state_file" 2>/dev/null)"
    if [ "$status" = "end_requested" ] \
        || am_session_end_intent_exists "$client" "$session_id" "$generation"; then
        # SessionEnd can race the unlocked start RPC. Persist the exact server
        # UUID solely so the pending end can authenticate; never publish this
        # execution as active or write an active guard marker after the native
        # lifecycle has ended. The end call happens after releasing this lock.
        tmp="${state_file}.${BASHPID:-$$}.tmp"
        if ! jq --arg id "$execution_id" --argjson ancestors "$ancestors" \
            '.execution_id = $id | .ancestor_execution_ids = $ancestors |
             .status = "end_requested" |
             .requested_end_status = (.requested_end_status // "completed") |
             .end_requested_at = (.end_requested_at // (now | todateiso8601))' \
            "$state_file" > "$tmp" 2>/dev/null \
            || ! chmod 600 "$tmp" 2>/dev/null \
            || ! mv -f "$tmp" "$state_file" 2>/dev/null; then
            am_lock_release "$lock_dir"
            return 1
        fi
        requested_end_status="$(jq -r \
            '.requested_end_status // "completed"' "$state_file" 2>/dev/null)"
        am_lock_release "$lock_dir"
        if am_execution_end "$project" "$agent" "$token" "$client" "$kind" \
            "$native_id" "$requested_end_status" "$generation" \
            >/dev/null 2>&1; then
            am_execution_manifest_mark_terminal \
                "$client" "$session_id" "$generation" >/dev/null 2>&1 || true
            am_execution_retention_prune >/dev/null 2>&1 || true
        fi
        return 1
    fi
    if [ "$status" != "starting" ] && [ "$status" != "active" ] \
        && [ "$status" != "stopping" ]; then
        # A concurrent SessionEnd completed the server end while the start RPC
        # was in flight. Never republish that terminal lifecycle as active.
        am_lock_release "$lock_dir"
        return 1
    fi
    tmp="${state_file}.${BASHPID:-$$}.tmp"
    if jq --arg id "$execution_id" --argjson ancestors "$ancestors" \
        '.execution_id = $id | .ancestor_execution_ids = $ancestors |
         .status = "active" | del(.stop_requested_at)' \
        "$state_file" > "$tmp" 2>/dev/null \
        && chmod 600 "$tmp" 2>/dev/null \
        && mv -f "$tmp" "$state_file" 2>/dev/null; then
        :
    else
        am_lock_release "$lock_dir"
        return 1
    fi
    am_lock_release "$lock_dir"
    printf '%s' "$execution_id"
}

am_execution_state_value() {
    local project="$1" agent="$2" client="$3" kind="$4" native_id="$5"
    local query="$6" session_id state_file
    session_id="$(am_payload_field '.session_id')"
    state_file="$(am_execution_state_file "$project" "$agent" "$client" \
        "$session_id" "$kind" "$native_id")" || return 1
    [ -r "$state_file" ] || return 1
    jq -r "$query // empty" "$state_file" 2>/dev/null
}

am_execution_state_token() {
    local project="$1" agent="$2" client="$3" kind="$4" native_id="$5"
    local generation="${6:-}" session_id token_file token
    session_id="$(am_payload_field '.session_id')"
    if [ -z "$generation" ]; then
        generation="$(am_session_lifecycle_generation "$client" "$session_id")" \
            || return 1
    fi
    token_file="$(am_execution_token_file "$project" "$agent" "$client" \
        "$session_id" "$kind" "$native_id" "$generation")" || return 1
    token="$(cat "$token_file" 2>/dev/null)"
    printf '%s' "$token" | grep -Eq '^[0-9a-f]{64}$' || return 1
    printf '%s' "$token"
}

# Resolve the execution that owns the current hook call.  Claude includes
# agent_id on tool hooks fired inside a subagent; Codex currently documents it
# only on SubagentStart/SubagentStop, so an absent field correctly resolves to
# the root session instead of inventing an undocumented identifier.
am_execution_id_for_payload() {
    local project="$1" agent="$2" client="$3" native_id kind
    native_id="$(am_payload_field '.agent_id')"
    if [ -n "$native_id" ]; then kind="subagent"; else
        kind="session"
        native_id="$(am_payload_field '.session_id')"
    fi
    am_execution_state_value "$project" "$agent" "$client" "$kind" \
        "$native_id" 'select(.status == "active") | .execution_id'
}

am_execution_lineage_ids_for_payload() {
    local project="$1" agent="$2" client="$3" execution_id native_id kind
    local state_ancestors compatible_ids
    execution_id="$(am_execution_id_for_payload "$project" "$agent" "$client")"
    [ -n "$execution_id" ] || return 1
    native_id="$(am_payload_field '.agent_id')"
    if [ -n "$native_id" ]; then kind="subagent"; else
        kind="session"
        native_id="$(am_payload_field '.session_id')"
    fi
    state_ancestors="$(am_execution_state_value \
        "$project" "$agent" "$client" "$kind" "$native_id" \
        '.ancestor_execution_ids | select(type == "array")')"
    [ -n "$state_ancestors" ] || state_ancestors='[]'

    compatible_ids="$(jq -nc --arg execution_id "$execution_id" \
        --argjson ancestors "$state_ancestors" \
        '[$execution_id] + $ancestors | unique')" || return 1
    printf '%s' "$compatible_ids"
}

am_execution_compatible_ids_for_payload() {
    local project="$1" agent="$2" client="$3" marker cwd marker_id
    local compatible_ids max_age
    compatible_ids="$(am_execution_lineage_ids_for_payload \
        "$project" "$agent" "$client")" || return 1

    # A local active state can survive sleep, server expiry, or a lost stop
    # event. Self-suppression therefore also requires a fresh marker for this
    # execution or one of its ancestors. Old/plain/terminal markers fail closed.
    cwd="${AM_EXECUTION_CWD_OVERRIDE:-$(am_payload_field '.cwd')}"
    marker="$(am_execution_marker_path "$cwd")" || marker=""
    [ -n "$marker" ] && [ -r "$marker" ] || return 1
    max_age="${AGENT_EXECUTION_MARKER_MAX_AGE_SECONDS:-1800}"
    case "$max_age" in ''|*[!0-9]*) max_age=1800 ;; esac
    jq -e --argjson max_age "$max_age" '
        .status == "active" and
        (.execution_id | type == "string" and length > 0) and
        (.heartbeat_ts | type == "string") and
        ((.heartbeat_ts | fromdateiso8601) as $heartbeat |
          $heartbeat >= (now - $max_age) and $heartbeat <= (now + 300))
      ' "$marker" >/dev/null 2>&1 || return 1
    marker_id="$(jq -r '.execution_id' "$marker" 2>/dev/null)"
    printf '%s' "$compatible_ids" | jq -e --arg id "$marker_id" \
        'index($id) != null' >/dev/null 2>&1 || return 1
    # State lineage came from the authenticated start response. The marker is
    # only a freshness/handoff hint, so never widen the trusted set with ids
    # found solely in that file.
    printf '%s' "$compatible_ids"
}

am_execution_token_for_payload() {
    local project="$1" agent="$2" client="$3" native_id kind
    native_id="$(am_payload_field '.agent_id')"
    if [ -n "$native_id" ]; then kind="subagent"; else
        kind="session"
        native_id="$(am_payload_field '.session_id')"
    fi
    [ -n "$(am_execution_state_value "$project" "$agent" "$client" "$kind" \
        "$native_id" 'select(.status == "active") | .execution_id')" ] || return 1
    am_execution_state_token "$project" "$agent" "$client" "$kind" "$native_id"
}

# SubagentStop can be blocked by another matching provider hook. Ending the
# server execution inside SubagentStop would therefore release claims while the
# child continues. Record a provisional local stop instead; a child tool event
# cancels it, while the first later parent event proves control returned and
# finalizes it.
am_subagent_execution_request_stop() {
    local project="$1" agent="$2" client="$3" native_id session_id state_file
    local lock_dir status tmp now
    native_id="$(am_payload_field '.agent_id')"
    session_id="$(am_payload_field '.session_id')"
    [ -n "$native_id" ] && [ -n "$session_id" ] || return 0
    state_file="$(am_execution_state_file "$project" "$agent" "$client" \
        "$session_id" subagent "$native_id")" || return 1
    [ -r "$state_file" ] || return 0
    lock_dir="${state_file}.lock"
    am_lock_acquire "$lock_dir" || return 1
    status="$(jq -r '.status // empty' "$state_file" 2>/dev/null)"
    if [ "$status" != "active" ] && [ "$status" != "stopping" ]; then
        am_lock_release "$lock_dir"
        return 0
    fi
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
    tmp="${state_file}.${BASHPID:-$$}.tmp"
    if jq --arg now "$now" \
        '.status = "stopping" | .stop_requested_at = $now' \
        "$state_file" > "$tmp" 2>/dev/null \
        && chmod 600 "$tmp" 2>/dev/null \
        && mv -f "$tmp" "$state_file" 2>/dev/null; then
        am_lock_release "$lock_dir"
        return 0
    fi
    am_lock_release "$lock_dir"
    return 1
}

am_execution_resume_for_payload() {
    local project="$1" agent="$2" client="$3" native_id session_id state_file
    local lock_dir status tmp
    native_id="$(am_payload_field '.agent_id')"
    [ -n "$native_id" ] || return 0
    session_id="$(am_payload_field '.session_id')"
    state_file="$(am_execution_state_file "$project" "$agent" "$client" \
        "$session_id" subagent "$native_id")" || return 1
    [ -r "$state_file" ] || return 0
    lock_dir="${state_file}.lock"
    am_lock_acquire "$lock_dir" || return 1
    status="$(jq -r '.status // empty' "$state_file" 2>/dev/null)"
    if [ "$status" != "stopping" ]; then
        am_lock_release "$lock_dir"
        return 0
    fi
    tmp="${state_file}.${BASHPID:-$$}.tmp"
    if jq '.status = "active" | del(.stop_requested_at)' \
        "$state_file" > "$tmp" 2>/dev/null \
        && chmod 600 "$tmp" 2>/dev/null \
        && mv -f "$tmp" "$state_file" 2>/dev/null; then
        am_lock_release "$lock_dir"
        return 0
    fi
    am_lock_release "$lock_dir"
    return 1
}

am_execution_marker_refresh_for_payload() {
    local project="$1" agent="$2" client="$3" native_id kind execution_id
    local marker_cwd ancestors
    native_id="$(am_payload_field '.agent_id')"
    if [ -n "$native_id" ]; then kind="subagent"; else
        kind="session"
        native_id="$(am_payload_field '.session_id')"
    fi
    execution_id="$(am_execution_state_value "$project" "$agent" "$client" \
        "$kind" "$native_id" 'select(.status == "active") | .execution_id')"
    [ -n "$execution_id" ] || return 1
    marker_cwd="$(am_execution_state_value "$project" "$agent" "$client" \
        "$kind" "$native_id" '.worktree_path // .cwd')"
    ancestors="$(am_execution_state_value "$project" "$agent" "$client" \
        "$kind" "$native_id" \
        '.ancestor_execution_ids | select(type == "array")')"
    [ -n "$ancestors" ] || ancestors='[]'
    am_execution_marker_publish_active "$project" "$agent" "$client" "$kind" \
        "$native_id" "$marker_cwd" "$execution_id" "$ancestors"
}

am_execution_reconcile_for_payload() {
    local project="$1" agent="$2" token="$3" client="$4" native_id
    native_id="$(am_payload_field '.agent_id')"
    if [ -n "$native_id" ]; then
        am_execution_resume_for_payload "$project" "$agent" "$client"
    else
        am_execution_finalize_stopping_children_all_for_payload "$client"
        am_execution_marker_refresh_for_payload "$project" "$agent" "$client" \
            >/dev/null 2>&1 || true
    fi
}

# Touch one exact execution at most once per local interval. Keeping kind and
# native id explicit lets a PostToolUse event refresh every active root chain
# even when that payload belongs to a native subagent.
am_execution_heartbeat_exact() {
    local project="$1" agent="$2" token="$3" client="$4"
    local kind="$5" native_id="$6" publish_marker="${7:-1}"
    local execution_id execution_token interval stamp lock_dir now then_
    local marker_cwd ancestors state_file state_lock current_execution
    execution_id="$(am_execution_state_value "$project" "$agent" "$client" \
        "$kind" "$native_id" 'select(.status == "active") | .execution_id')"
    [ -n "$execution_id" ] || return 0
    execution_token="$(am_execution_state_token "$project" "$agent" "$client" \
        "$kind" "$native_id")"
    [ -n "$execution_token" ] || return 0
    interval="${AGENT_MAIL_EXECUTION_HEARTBEAT_INTERVAL:-60}"
    case "$interval" in ''|*[!0-9]*) interval=60 ;; esac
    stamp="$(am_execution_heartbeat_stamp_file \
        "$project" "$agent" "$execution_id")" || return 0
    lock_dir="${stamp}.lock"
    am_lock_acquire "$lock_dir" || return 0
    now="$(date +%s 2>/dev/null || printf '0')"
    then_="$(cat "$stamp" 2>/dev/null || printf '0')"
    case "$then_" in ''|*[!0-9]*) then_=0 ;; esac
    if [ $((now - then_)) -lt "$interval" ]; then
        am_lock_release "$lock_dir"
        return 0
    fi

    if ! am_call heartbeat_agent_execution "$(
        AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" \
        AGENT_MAIL_JQ_EXECUTION_TOKEN="$execution_token" jq -nc \
            --arg p "$project" --arg a "$agent" --arg id "$execution_id" \
            '{project_key:$p,agent_name:$a,
              registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,
              execution_id:$id,
              execution_token:env.AGENT_MAIL_JQ_EXECUTION_TOKEN,
              lifecycle_protocol_version:1}'
        )" >/dev/null; then
        if [ "$publish_marker" -eq 1 ]; then
            marker_cwd="$(am_execution_state_value \
                "$project" "$agent" "$client" "$kind" "$native_id" \
                '.worktree_path // .cwd')"
            am_execution_marker_end "$marker_cwd" "$execution_id" unverified \
                >/dev/null 2>&1 || true
        fi
        am_lock_release "$lock_dir"
        return 0
    fi
    state_file="$(am_execution_state_file "$project" "$agent" "$client" \
        "$(am_payload_field '.session_id')" "$kind" "$native_id")" || {
        am_lock_release "$lock_dir"
        return 0
    }
    state_lock="${state_file}.lock"
    if ! am_lock_acquire "$state_lock"; then
        am_lock_release "$lock_dir"
        return 0
    fi
    current_execution="$(jq -r \
        'select(.status == "active") | .execution_id // empty' \
        "$state_file" 2>/dev/null)"
    if [ "$current_execution" != "$execution_id" ]; then
        am_lock_release "$state_lock"
        am_lock_release "$lock_dir"
        return 0
    fi
    mkdir -p "$(dirname "$stamp")" 2>/dev/null || {
        am_lock_release "$state_lock"
        am_lock_release "$lock_dir"
        return 0
    }
    printf '%s' "$now" > "$stamp" 2>/dev/null || true
    am_lock_release "$state_lock"
    if [ "$publish_marker" -eq 1 ]; then
        marker_cwd="$(am_execution_state_value "$project" "$agent" "$client" \
            "$kind" "$native_id" '.worktree_path // .cwd')"
        ancestors="$(am_execution_state_value "$project" "$agent" "$client" \
            "$kind" "$native_id" \
            '.ancestor_execution_ids | select(type == "array")')"
        [ -n "$ancestors" ] || ancestors='[]'
        # The publisher reacquires and revalidates the exact state under its
        # lock, so a concurrent terminal transition cannot be overwritten.
        am_execution_marker_publish_active "$project" "$agent" "$client" \
            "$kind" "$native_id" "$marker_cwd" "$execution_id" \
            "$ancestors" >/dev/null 2>&1 || true
    fi
    am_lock_release "$lock_dir"
    return 0
}

# Touch the execution corresponding to the current hook payload. This is
# intentionally independent of inbox polling: read-only agents and subagents
# can be active for hours without creating a reservation.
am_execution_heartbeat() {
    local project="$1" agent="$2" token="$3" client="$4" native_id kind
    native_id="$(am_payload_field '.agent_id')"
    if [ -n "$native_id" ]; then
        kind="subagent"
    else
        kind="session"
        native_id="$(am_payload_field '.session_id')"
    fi
    [ -n "$native_id" ] || return 0
    am_execution_heartbeat_exact "$project" "$agent" "$token" "$client" \
        "$kind" "$native_id"
}

am_execution_end() {
    local project="$1" agent="$2" token="$3" client="$4" kind="$5"
    local native_id="$6" end_status="${7:-completed}" execution_id execution_token
    local state_file session_id rc lock_dir tmp current_status marker_cwd
    local effective_end_status generation="${8:-}"
    session_id="$(am_payload_field '.session_id')"
    if [ -z "$generation" ]; then
        generation="$(am_session_lifecycle_generation "$client" "$session_id")" \
            || return 1
    fi
    state_file="$(am_execution_state_file "$project" "$agent" "$client" \
        "$session_id" "$kind" "$native_id" "$generation")" || return 1
    [ -r "$state_file" ] || return 0
    lock_dir="${state_file}.lock"
    am_lock_acquire "$lock_dir" || return 1
    current_status="$(jq -r '.status // empty' "$state_file" 2>/dev/null)"
    if [ "$current_status" = "starting" ]; then
        # The start RPC deliberately runs without this lock. Record the native
        # SessionEnd intent durably so its eventual response cannot publish an
        # active execution after the session has ended. There is no server UUID
        # to end yet; am_execution_start will persist the returned UUID and call
        # back into this function after releasing the lock.
        tmp="${state_file}.${BASHPID:-$$}.tmp"
        if ! jq --arg status "$end_status" \
            '.status = "end_requested" |
             .requested_end_status = $status |
             .end_requested_at = (now | todateiso8601)' \
            "$state_file" > "$tmp" 2>/dev/null \
            || ! chmod 600 "$tmp" 2>/dev/null \
            || ! mv -f "$tmp" "$state_file" 2>/dev/null; then
            am_lock_release "$lock_dir"
            return 1
        fi
        am_lock_release "$lock_dir"
        return 0
    fi
    if [ "$current_status" != "active" ] \
        && [ "$current_status" != "stopping" ] \
        && [ "$current_status" != "end_requested" ]; then
        case "$current_status" in
            completed|failed|cancelled|expired)
                am_execution_terminalize_local_locked \
                    "$state_file" "$current_status" server_retry \
                    >/dev/null 2>&1 || true
                ;;
        esac
        am_lock_release "$lock_dir"
        return 0
    fi
    execution_id="$(jq -r '.execution_id // empty' "$state_file" 2>/dev/null)"
    if [ -z "$execution_id" ]; then
        am_lock_release "$lock_dir"
        return 0
    fi
    effective_end_status="$end_status"
    if [ "$current_status" = "end_requested" ]; then
        effective_end_status="$(jq -r --arg fallback "$end_status" \
            '.requested_end_status // $fallback' "$state_file" 2>/dev/null)"
    fi
    execution_token="$(am_execution_state_token \
        "$project" "$agent" "$client" "$kind" "$native_id" "$generation")"
    if [ -z "$execution_token" ]; then
        am_lock_release "$lock_dir"
        return 1
    fi
    marker_cwd="$(jq -r '.worktree_path // .cwd // empty' \
        "$state_file" 2>/dev/null)"
    am_call end_agent_execution "$(
        AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" \
        AGENT_MAIL_JQ_EXECUTION_TOKEN="$execution_token" jq -nc \
            --arg p "$project" --arg a "$agent" --arg id "$execution_id" \
            --arg status "$effective_end_status" \
            '{project_key:$p,agent_name:$a,
              registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,
              execution_id:$id,
              execution_token:env.AGENT_MAIL_JQ_EXECUTION_TOKEN,status:$status,
              lifecycle_protocol_version:1}'
    )" >/dev/null
    rc=$?
    if [ "$rc" -ne 0 ]; then
        am_lock_release "$lock_dir"
        return "$rc"
    fi

    if ! am_execution_terminalize_local_locked \
        "$state_file" "$effective_end_status" server; then
        am_lock_release "$lock_dir"
        return 1
    fi
    am_lock_release "$lock_dir"
    am_execution_marker_end "$marker_cwd" "$execution_id" "$effective_end_status" \
        >/dev/null 2>&1 || true
    return 0
}

am_execution_finalize_stopping_children() {
    local project="$1" agent="$2" token="$3" client="$4" session_id state_file
    local native_id generation
    session_id="$(am_payload_field '.session_id')"
    [ -n "$session_id" ] || return 0
    generation="$(am_session_lifecycle_generation "$client" "$session_id")" \
        || return 0
    while IFS= read -r state_file; do
        [ -r "$state_file" ] || continue
        native_id="$(jq -r --arg project "$project" --arg agent "$agent" \
            --arg client "$client" --arg session_id "$session_id" \
            --argjson generation "$generation" '
              select(.project == $project and .agent == $agent and
                     .client == $client and .session_id == $session_id and
                     (.lifecycle_generation // 1) == $generation and
                     .kind == "subagent" and .status == "stopping") |
              .native_id // empty
            ' "$state_file" 2>/dev/null)"
        [ -n "$native_id" ] || continue
        am_execution_end "$project" "$agent" "$token" "$client" \
            subagent "$native_id" completed "$generation" \
            >/dev/null 2>&1 || true
    done < <(am_execution_manifest_state_files \
        "$client" "$session_id" "$generation")
    return 0
}

am_execution_finalize_stopping_children_all_for_payload() {
    local client="$1" session_id state_file project agent token native_id generation
    session_id="$(am_payload_field '.session_id')"
    [ -n "$session_id" ] || return 0
    generation="$(am_session_lifecycle_generation "$client" "$session_id")" \
        || return 0
    while IFS= read -r state_file; do
        [ -r "$state_file" ] || continue
        native_id="$(jq -r --arg client "$client" --arg session_id "$session_id" \
            --argjson generation "$generation" '
              select(.client == $client and .session_id == $session_id and
                     (.lifecycle_generation // 1) == $generation and
                     .kind == "subagent" and .status == "stopping") |
              .native_id // empty
            ' "$state_file" 2>/dev/null)"
        [ -n "$native_id" ] || continue
        project="$(jq -r '.project // empty' "$state_file" 2>/dev/null)"
        agent="$(jq -r '.agent // empty' "$state_file" 2>/dev/null)"
        [ -n "$project" ] && [ -n "$agent" ] || continue
        token="$(am_cred_get "$project" "$agent")"
        [ -n "$token" ] || continue
        am_execution_end "$project" "$agent" "$token" "$client" \
            subagent "$native_id" completed "$generation" \
            >/dev/null 2>&1 || true
    done < <(am_execution_manifest_state_files \
        "$client" "$session_id" "$generation")
    return 0
}

am_subagent_executions_request_stop_all_for_payload() {
    local client="$1" session_id native_id state_file project agent generation
    session_id="$(am_payload_field '.session_id')"
    native_id="$(am_payload_field '.agent_id')"
    [ -n "$session_id" ] && [ -n "$native_id" ] || return 0
    generation="$(am_session_lifecycle_generation "$client" "$session_id")" \
        || return 0
    while IFS= read -r state_file; do
        [ -r "$state_file" ] || continue
        if ! jq -e --arg client "$client" --arg session_id "$session_id" \
            --arg native_id "$native_id" --argjson generation "$generation" '
              .client == $client and .session_id == $session_id and
              (.lifecycle_generation // 1) == $generation and
              .kind == "subagent" and .native_id == $native_id and
              (.status == "active" or .status == "stopping")
            ' "$state_file" >/dev/null 2>&1; then
            continue
        fi
        project="$(jq -r '.project // empty' "$state_file" 2>/dev/null)"
        agent="$(jq -r '.agent // empty' "$state_file" 2>/dev/null)"
        [ -n "$project" ] && [ -n "$agent" ] || continue
        am_subagent_execution_request_stop "$project" "$agent" "$client" \
            >/dev/null 2>&1 || true
    done < <(am_execution_manifest_state_files \
        "$client" "$session_id" "$generation")
    return 0
}

# A native session can own a root execution in more than one project. Every
# PostToolUse keeps all exact roots for this client/session generation alive,
# rather than letting a prior project expire merely because the hook cwd moved
# to another repository. New target enrollment happens before this scan.
am_root_executions_heartbeat_all_for_payload() {
    local client="$1" session_id generation state_file project agent token
    session_id="$(am_payload_field '.session_id')"
    [ -n "$client" ] && [ -n "$session_id" ] || return 0
    generation="$(am_session_lifecycle_generation "$client" "$session_id")" \
        || return 0
    while IFS= read -r state_file; do
        [ -r "$state_file" ] || continue
        if ! jq -e --arg client "$client" --arg session_id "$session_id" \
            --argjson generation "$generation" '
              .client == $client and .session_id == $session_id and
              (.lifecycle_generation // 1) == $generation and
              .kind == "session" and .native_id == $session_id and
              .status == "active"
            ' "$state_file" >/dev/null 2>&1; then
            continue
        fi
        project="$(jq -r '.project // empty' "$state_file" 2>/dev/null)"
        agent="$(jq -r '.agent // empty' "$state_file" 2>/dev/null)"
        [ -n "$project" ] && [ -n "$agent" ] || continue
        token="$(am_cred_get "$project" "$agent")"
        [ -n "$token" ] || continue
        am_execution_heartbeat_exact "$project" "$agent" "$token" "$client" \
            session "$session_id" 0 >/dev/null 2>&1 || true
    done < <(am_execution_manifest_state_files \
        "$client" "$session_id" "$generation")
    return 0
}

am_root_executions_end_all_for_payload() {
    local client="$1" end_status="${2:-completed}" session_id state_file
    local project agent token spawned=0 generation
    session_id="$(am_payload_field '.session_id')"
    [ -n "$session_id" ] || return 0
    # This durable barrier must precede enumeration. Concurrent first-touch
    # enrollment in another project will then refuse a new execution even when
    # no state file existed when this loop began.
    generation="$(am_session_end_intent_mark "$client" 2>/dev/null)" \
        || return 0
    case "$generation" in ''|*[!0-9]*) return 0 ;; esac
    while IFS= read -r state_file; do
        [ -r "$state_file" ] || continue
        if ! jq -e --arg client "$client" --arg session_id "$session_id" \
            --argjson generation "$generation" '
              .client == $client and .session_id == $session_id and
              (.lifecycle_generation // 1) == $generation and
              .kind == "session" and .native_id == $session_id and
              (.status == "starting" or .status == "active" or
               .status == "stopping" or .status == "end_requested")
            ' "$state_file" >/dev/null 2>&1; then
            continue
        fi
        project="$(jq -r '.project // empty' "$state_file" 2>/dev/null)"
        agent="$(jq -r '.agent // empty' "$state_file" 2>/dev/null)"
        [ -n "$project" ] && [ -n "$agent" ] || continue
        token="$(am_cred_get "$project" "$agent")"
        [ -n "$token" ] || continue
        # SessionEnd has a three-second provider ceiling, while one HTTP call
        # may consume two seconds. End independent project executions in
        # parallel; every subshell retains only its exact credential/capability
        # in shell memory, never argv or output. Reconcile descendant state only
        # after that project's authenticated end succeeds.
        (
            if am_execution_end "$project" "$agent" "$token" "$client" \
                session "$session_id" "$end_status" "$generation" \
                >/dev/null 2>&1; then
                am_execution_mark_session_children_terminal_local \
                    "$project" "$agent" "$client" "$end_status" "$generation"
            fi
        ) >/dev/null 2>&1 &
        spawned=1
    done < <(am_execution_manifest_state_files \
        "$client" "$session_id" "$generation")
    [ "$spawned" -eq 0 ] || wait || true
    am_execution_manifest_mark_terminal \
        "$client" "$session_id" "$generation" >/dev/null 2>&1 || true
    am_execution_retention_prune >/dev/null 2>&1 || true
    return 0
}

am_execution_mark_session_children_terminal_local() {
    local project="$1" agent="$2" client="$3" end_status="$4" session_id
    local state_file native_id execution_id marker_cwd lock_dir current
    local generation="${5:-}"
    session_id="$(am_payload_field '.session_id')"
    [ -n "$session_id" ] || return 0
    if [ -z "$generation" ]; then
        generation="$(am_session_lifecycle_generation "$client" "$session_id")" \
            || return 0
    fi
    while IFS= read -r state_file; do
        [ -r "$state_file" ] || continue
        native_id="$(jq -r --arg project "$project" --arg agent "$agent" \
            --arg client "$client" --arg session_id "$session_id" \
            --argjson generation "$generation" '
              select(.project == $project and .agent == $agent and
                     .client == $client and .session_id == $session_id and
                     (.lifecycle_generation // 1) == $generation and
                     .kind == "subagent" and
                     (.status == "active" or .status == "stopping")) |
              .native_id // empty
            ' "$state_file" 2>/dev/null)"
        [ -n "$native_id" ] || continue
        lock_dir="${state_file}.lock"
        am_lock_acquire "$lock_dir" || continue
        current="$(jq -r '.status // empty' "$state_file" 2>/dev/null)"
        if [ "$current" != "active" ] && [ "$current" != "stopping" ]; then
            am_lock_release "$lock_dir"
            continue
        fi
        execution_id="$(jq -r '.execution_id // empty' "$state_file" 2>/dev/null)"
        marker_cwd="$(jq -r '.worktree_path // .cwd // empty' \
            "$state_file" 2>/dev/null)"
        if am_execution_terminalize_local_locked \
            "$state_file" "$end_status" parent_end; then
            am_lock_release "$lock_dir"
            am_execution_marker_end "$marker_cwd" "$execution_id" \
                "$end_status" >/dev/null 2>&1 || true
        else
            am_lock_release "$lock_dir"
        fi
    done < <(am_execution_manifest_state_files \
        "$client" "$session_id" "$generation")
    return 0
}

am_root_execution_start() {
    local project="$1" agent="$2" token="$3" client="$4" session_id cwd id
    session_id="$(am_payload_field '.session_id')"
    [ -n "$session_id" ] || return 1
    am_execution_retention_prune >/dev/null 2>&1 || true
    id="$(am_execution_start "$project" "$agent" "$token" "$client" \
        session "$session_id" "" "" "")" || return 1
    am_execution_finalize_stopping_children_all_for_payload "$client" \
        >/dev/null 2>&1 || true
    cwd="${AM_EXECUTION_CWD_OVERRIDE:-$(am_payload_field '.cwd')}"
    am_execution_marker_publish_active "$project" "$agent" "$client" session \
        "$session_id" "$cwd" "$id" '[]' || true
    printf '%s' "$id"
}

# Ensure the execution corresponding to the current payload exists in one
# project. This is used when a tool edits a file in a repository other than the
# hook payload's original cwd: the same native session receives a separate root
# (and, for a child tool, child) execution under that project's durable Agent.
am_execution_ensure_for_payload() {
    local project="$1" agent="$2" token="$3" client="$4"
    local session_id native_id execution_id
    session_id="$(am_payload_field '.session_id')"
    native_id="$(am_payload_field '.agent_id')"
    [ -n "$session_id" ] || return 1
    execution_id="$(am_execution_state_value "$project" "$agent" "$client" \
        session "$session_id" 'select(.status == "active") | .execution_id')"
    if [ -z "$execution_id" ]; then
        am_root_execution_start "$project" "$agent" "$token" "$client" \
            >/dev/null || return 1
    fi
    if [ -n "$native_id" ]; then
        execution_id="$(am_execution_state_value "$project" "$agent" "$client" \
            subagent "$native_id" 'select(.status == "active") | .execution_id')"
        if [ -z "$execution_id" ]; then
            am_subagent_execution_start "$project" "$agent" "$token" "$client" \
                >/dev/null || return 1
        fi
    fi
    am_execution_id_for_payload "$project" "$agent" "$client"
}

am_subagent_execution_start() {
    local project="$1" agent="$2" token="$3" client="$4"
    local session_id native_id agent_type parent_id parent_token parent_worktree
    local current_worktree cwd task id ancestors
    session_id="$(am_payload_field '.session_id')"
    native_id="$(am_payload_field '.agent_id')"
    agent_type="$(am_payload_field '.agent_type')"
    [ -n "$session_id" ] && [ -n "$native_id" ] || return 1
    parent_id="$(am_execution_state_value "$project" "$agent" "$client" \
        session "$session_id" 'select(.status == "active") | .execution_id')"
    [ -n "$parent_id" ] || return 1
    parent_token="$(am_execution_state_token \
        "$project" "$agent" "$client" session "$session_id")"
    [ -n "$parent_token" ] || return 1
    parent_worktree="$(am_execution_state_value "$project" "$agent" "$client" \
        session "$session_id" 'select(.status == "active") | .worktree_path')"
    agent_type="$(printf '%s' "$agent_type" | cut -c1-128)"
    task="${client} subagent"
    [ -z "$agent_type" ] || task="${task}: ${agent_type}"
    id="$(am_execution_start "$project" "$agent" "$token" "$client" \
        subagent "$native_id" "$parent_id" "$parent_token" "$task")" \
        || return 1
    ancestors="$(am_execution_state_value "$project" "$agent" "$client" \
        subagent "$native_id" \
        '.ancestor_execution_ids | select(type == "array")')"
    [ -n "$ancestors" ] || ancestors="$(jq -nc --arg parent "$parent_id" '[$parent]')"
    cwd="${AM_EXECUTION_CWD_OVERRIDE:-$(am_payload_field '.cwd')}"
    current_worktree="$(am_execution_git_metadata "$cwd" \
        | jq -r '.worktree_path // empty' 2>/dev/null)"
    if [ -n "$current_worktree" ] && [ "$current_worktree" != "$parent_worktree" ]; then
        am_execution_marker_publish_active "$project" "$agent" "$client" \
            subagent "$native_id" "$cwd" "$id" "$ancestors" || true
    fi
    printf '%s' "$id"
}

am_root_execution_end() {
    local project="$1" agent="$2" token="$3" client="$4" session_id rc
    session_id="$(am_payload_field '.session_id')"
    [ -n "$session_id" ] || return 0
    am_execution_end "$project" "$agent" "$token" "$client" \
        session "$session_id" completed
    rc=$?
    if [ "$rc" -eq 0 ]; then
        # Root end atomically cascades through descendants on the server. Mirror
        # that terminal state locally so a separate child worktree cannot retain
        # a fresh active marker after an abrupt parent shutdown.
        am_execution_mark_session_children_terminal_local \
            "$project" "$agent" "$client" completed
    fi
    return "$rc"
}

# --- output ------------------------------------------------------------------
# Plain stdout from a hook does not reach the model; additionalContext is the
# only channel that surfaces in its system reminder.
am_emit_context() {
    local event="$1" text="$2" escaped
    [ -z "$text" ] && return 0
    if escaped="$(printf '%s' "$text" | jq -Rs . 2>/dev/null)" && [ -n "$escaped" ]; then
        printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":%s}}\n' "$event" "$escaped"
    fi
}
