# Shared plumbing for the Agent Mail hooks. Sourced, never executed.
#
# Installed ONCE PER MACHINE in ~/.claude/settings.json and works in every
# repository without per-project setup. Per-machine settings live in
# ~/.agent-mail.env, NOT in the hook commands: repeating four environment
# assignments across four commands is four places to forget one, and a forgotten
# one is silent.
#
# ~/.agent-mail.env (mode 0600), all optional except the token:
#   HTTP_BEARER_TOKEN=...                  server bearer
#   AGENT_MAIL_URL=https://host            server base URL
#   AGENT_MAIL_AGENT=name                  override the derived identity
#   AGENT_MAIL_STATE_DIR=/path             override the credential/state location
#
# Every function degrades to "do nothing" on failure. A hook that errors is a
# hook that blocks an edit.

# Git Bash rewrites any argument that looks like a POSIX path into a Windows one
# before handing it to a NATIVE binary — and jq.exe and curl.exe are native. A
# project key of "/owner/repo" would arrive as "C:/Program Files/Git/owner/repo",
# creating a phantom project that no other machine can ever join. Exported here
# so the hook commands do not each have to remember it.
case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) export MSYS_NO_PATHCONV=1 ;;
esac

# Per-machine configuration. Parsed rather than sourced: this file is read on
# every edit, and executing whatever it happens to contain is not a property
# worth having. Environment always wins, so a hook command can still override.
am_load_env() {
    [ -n "${AM_ENV_LOADED:-}" ] && return 0
    AM_ENV_LOADED=1
    local f="${AGENT_MAIL_ENV_FILE:-$HOME/.agent-mail.env}" line k v
    [ -r "$f" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        k="${line%%=*}"; v="${line#*=}"
        case "$k" in
            HTTP_BEARER_TOKEN|AGENT_MAIL_URL|AGENT_MAIL_AGENT|AGENT_MAIL_STATE_DIR|AGENT_MAIL_PROJECT_KEY)
                # shellcheck disable=SC2163
                [ -z "$(eval "printf '%s' \"\${$k:-}\"")" ] && export "$k=$v" ;;
        esac
    done < "$f"
    return 0
}
am_load_env

AM_TIMEOUT="${AGENT_MAIL_HOOK_TIMEOUT:-3}"
AM_BASE_URL="${AGENT_MAIL_URL:-http://127.0.0.1:8765}"

# On MSYS the credential store must be addressed the way jq.exe can open it.
# $HOME there is "/c/Users/you", which a native binary cannot resolve — and the
# failure is asymmetric and therefore vicious: the first write succeeds (bash
# redirection handles the path) while every later read fails (jq gets it as an
# argument). Registration works once, then the identity is unrecoverable.
am_default_state_dir() {
    local base="${XDG_STATE_HOME:-$HOME/.local/state}"
    case "$(uname -s 2>/dev/null)" in
        MINGW*|MSYS*|CYGWIN*)
            command -v cygpath >/dev/null 2>&1 && base="$(cygpath -m "$base" 2>/dev/null || printf '%s' "$base")" ;;
    esac
    printf '%s/agent-mail' "$base"
}
AM_STATE_DIR="${AGENT_MAIL_STATE_DIR:-$(am_default_state_dir)}"
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
    [ -n "${AGENT_MAIL_PROJECT_KEY:-}" ] && { printf '%s' "$AGENT_MAIL_PROJECT_KEY"; return 0; }
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
    [ -n "${AGENT_MAIL_PROJECT_KEY:-}" ] && { printf '%s' "$AGENT_MAIL_PROJECT_KEY"; return 0; }
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
# <host>-<platform>-1. The platform token comes from uname, which is the only
# thing that separates the three cases that otherwise collide: WSL and native
# Windows report the SAME hostname, and nothing else distinguishes a mac.
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
        *) printf '%s' "$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]' | cut -c1-8)" ;;
    esac
}

am_agent_name() {
    [ -n "${AGENT_MAIL_AGENT:-}" ] && { printf '%s' "$AGENT_MAIL_AGENT"; return 0; }
    local h p
    # macOS derives `hostname` from DHCP/reverse DNS, so a laptop that changes
    # network changes identity — and an identity change orphans the mailbox and
    # every reservation under the old name. LocalHostName is user-set and stays
    # put.
    h=""
    if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
        # macOS keeps three independent names and `hostname` reflects none of
        # them persistently: with HostName unset it takes kern.hostname from DHCP
        # option 12 or reverse DNS, falling back to LocalHostName off-network.
        # A laptop that changes network therefore changes identity — and the new
        # name has no entry in the credential store, so autoreserve and
        # session_end quietly start exiting 0 without doing anything. Observed:
        # the same machine reports a LAN-derived FQDN on one network and its
        # on-disk LocalHostName on another.
        #
        # HostName first because it is the one an operator can pin
        # (`sudo scutil --set HostName <name>`), LocalHostName second because it
        # is at least written to disk. Plain `hostname` is the last resort.
        h="$(scutil --get HostName 2>/dev/null || true)"
        [ -z "$h" ] && h="$(scutil --get LocalHostName 2>/dev/null || true)"
    fi
    # The `||` chain is load-bearing, not decoration: Git Bash ships GNU
    # coreutils' hostname, which has no -s at all (that flag belongs to
    # net-tools) and exits 1. Without the fallback there would be no name on
    # Windows whatsoever.
    [ -z "$h" ] && h="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo host)"
    # Lowercased deliberately. The server treats agent names as case-insensitively
    # unique, and $COMPUTERNAME on Windows reports HOME where `hostname` reports
    # home — two derivations of one machine that would collide as "already in
    # use", which in the default coerce mode is answered with a silent random
    # rename rather than an error.
    h="$(printf '%s' "$h" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]._-' | cut -c1-48)"
    [ -z "$h" ] && h="host"
    p="$(am_platform)"; [ -z "$p" ] && p="unknown"
    # The server silently replaces a name resembling a program or model with a
    # random one — `_looks_like_model_name` matches on substring — so keep the
    # shape boring and verify any new platform token before adding it.
    printf '%s-%s-1' "$h" "$p"
}

am_bearer() {
    printf '%s' "${AGENT_MAIL_TOKEN:-${HTTP_BEARER_TOKEN:-}}"
}

# --- server calls ------------------------------------------------------------
# The STATELESS mount: a one-shot JSON-RPC POST needs no initialize handshake and
# returns in ~40ms, where the stateful /mcp mount would cost three round trips
# and leak a session per hook invocation.
am_call() {
    local tool="$1" args="$2" bearer body
    bearer="$(am_bearer)"
    [ -z "$bearer" ] && return 0
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
    body="$(jq -nac --arg t "$tool" --argjson a "$args" \
        '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:$t,arguments:$a}}' 2>/dev/null)" || return 0
    printf '%s' "$body" | curl -s --max-time "$AM_TIMEOUT" -X POST "${AM_BASE_URL}/api/" \
        -H "Authorization: Bearer ${bearer}" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        --data @- 2>/dev/null \
        | jq -r '.result.content[0].text // empty' 2>/dev/null
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
am_get() {
    local path="$1" bearer
    bearer="$(am_bearer)"
    [ -z "$bearer" ] && return 0
    curl -s --max-time "$AM_TIMEOUT" -G -H "Authorization: Bearer ${bearer}" \
        "${@:2}" "${AM_BASE_URL}${path}" 2>/dev/null
}

# --- credential store --------------------------------------------------------
# Re-registering an existing name REQUIRES the token the first registration
# returned; the server will not reissue it. Losing this file orphans the identity
# permanently, hence the atomic write.
am_cred_get() {
    [ -r "$AM_CRED_FILE" ] || return 0
    jq -r --arg p "$1" --arg a "$2" '.[$p][$a] // empty' "$AM_CRED_FILE" 2>/dev/null
}

am_cred_put() {
    local project="$1" agent="$2" token="$3" tmp
    [ -z "$token" ] && return 0
    mkdir -p "$AM_STATE_DIR" 2>/dev/null || return 0
    chmod 700 "$AM_STATE_DIR" 2>/dev/null
    tmp="${AM_CRED_FILE}.$$.tmp"
    if [ -r "$AM_CRED_FILE" ]; then
        jq --arg p "$project" --arg a "$agent" --arg t "$token" \
            '.[$p] = ((.[$p] // {}) | .[$a] = $t)' "$AM_CRED_FILE" >"$tmp" 2>/dev/null \
            || { rm -f "$tmp"; return 0; }
    else
        jq -n --arg p "$project" --arg a "$agent" --arg t "$token" \
            '{($p): {($a): $t}}' >"$tmp" 2>/dev/null || { rm -f "$tmp"; return 0; }
    fi
    chmod 600 "$tmp" 2>/dev/null
    mv -f "$tmp" "$AM_CRED_FILE" 2>/dev/null || rm -f "$tmp"
}

# --- per-session path log ----------------------------------------------------
# Records what THIS session reserved, so SessionEnd releases exactly those paths
# and leaves a sibling session's holds alone. Keyed by session AND project: a
# session can edit files in more than one repository, and SessionEnd must find
# every log it wrote — looking under only the working directory's project would
# silently strand the rest.
am_session_slug() {
    printf '%s' "$1" | tr -cd '[:alnum:]._-' | cut -c1-64
}

am_session_log() {
    printf '%s/sessions/%s__%s.list' "$AM_STATE_DIR" \
        "$(am_session_slug "${AM_SESSION_ID:-nosession}")" "$(am_session_slug "$1")"
}

am_session_log_add() {
    local f; f="$(am_session_log "$1")"
    mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
    printf '%s\n' "$2" >>"$f" 2>/dev/null
    return 0
}

# Every log this session wrote, across all repositories it touched.
am_session_logs() {
    local dir="${AM_STATE_DIR}/sessions"
    [ -d "$dir" ] || return 0
    find "$dir" -maxdepth 1 -type f \
        -name "$(am_session_slug "${AM_SESSION_ID:-nosession}")__*.list" 2>/dev/null
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
