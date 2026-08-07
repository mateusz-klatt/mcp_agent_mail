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

# 3s was losing 2-5% of calls to nothing more than a slow round trip. The cost
# of raising it is paid only when packets vanish without an answer — a VPN, a
# firewall that drops instead of resetting, a dead CDN edge — because a refused
# connection returns in ~58ms whatever this is set to. Measured: a black hole
# costs exactly this many seconds, a dead server costs nothing.
#
# reservations_warn overrides it back down: it is the only PreToolUse hook, so
# it is the only one a person waits behind before every edit.
AM_TIMEOUT="${AGENT_MAIL_HOOK_TIMEOUT:-8}"
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

# Remember the name the server actually granted, keyed by project.
#
# Only session_start.sh writes this, from register_agent's response. It is a
# separate file from credentials.json on purpose: that one is keyed by agent
# name, which is precisely the thing in question here.
am_granted_name_file() {
    # Separators are MAPPED, not deleted. `tr -cd` alone drops the slashes, and
    # then /p/x and /px both become "px" — two projects sharing one identity
    # file, which is a collision credentials.json does not have because jq keys
    # it by the whole string. Caught by a control that was itself aimed at the
    # wrong filename, which is how the naming came to be printed at all.
    printf '%s/granted/%s' "$AM_STATE_DIR" \
        "$(printf '%s' "$1" | tr '/' '_' | tr -cd '[:alnum:]._-' | cut -c1-96)"
}

am_granted_name_put() {
    local f; f="$(am_granted_name_file "$1")"
    [ -n "${2:-}" ] || return 0
    mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
    printf '%s' "$2" > "$f" 2>/dev/null || true
}

am_agent_name() {
    [ -n "${AGENT_MAIL_AGENT:-}" ] && { printf '%s' "$AGENT_MAIL_AGENT"; return 0; }
    # A previously granted name wins over re-deriving one.
    #
    # The server does not always give you the name you asked for: a name that is
    # taken, or that matches _looks_like_model_name on a SUBSTRING, is answered
    # in the default coerce mode with a silent random rename. Everything then
    # came apart, and measurably so — laptop-mac-1 ran three sessions under a
    # name containing "claude-" and got three different identities and three
    # entries in credentials.json, because:
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
    # so they all inherit the answer without being touched. AGENT_MAIL_AGENT
    # still wins: an explicit override is a statement about identity, and this
    # is only a memory of one.
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
    if [ -d "${AM_STATE_DIR}/granted" ]; then
      local _proj; _proj="${AM_PROJECT_FOR_NAME:-$(am_project_key)}"
      if [ -n "$_proj" ]; then
        local gf; gf="$(am_granted_name_file "$_proj")"
        if [ -r "$gf" ]; then
            local g; g="$(cat "$gf" 2>/dev/null)"
            [ -n "$g" ] && { printf '%s' "$g"; return 0; }
        fi
      fi
    fi
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
    cache="${AM_STATE_DIR}/model/$(printf '%s|%s' "$proj" "$agent" \
        | tr '/' '_' | tr -cd '[:alnum:]._|-' | tr '|' '_' | cut -c1-96)"
    have="$(cat "$cache" 2>/dev/null || true)"
    [ "$want" = "$have" ] && return 0
    mkdir -p "$(dirname "$cache")" 2>/dev/null || return 0
    if am_call register_agent "$(jq -nc --arg p "$proj" --arg n "$agent" \
            --arg t "$tok" --arg m "$want" --arg g "$prog" \
            '{project_key:$p,name:$n,registration_token:$t,program:$g,model:$m}')" \
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
