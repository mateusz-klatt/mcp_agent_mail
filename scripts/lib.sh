#!/usr/bin/env bash
# Shared helpers for setup/integration scripts
# - Colorized logging (best-effort)
# - Flags parsing: --yes, --dry-run, --quiet, --debug, --regenerate-token, --show-token, --project-dir
# - Dependency checks and traps
# - Atomic writes and JSON validation
# - Readiness polling and secure perms

set -euo pipefail

# Initialize colors if not already defined
init_colors() {
  if [[ -n "${NO_COLOR:-}" ]]; then
    _b=""; _dim=""; _red=""; _grn=""; _ylw=""; _blu=""; _mag=""; _cyn=""; _rst=""
    return
  fi
  if command -v tput >/dev/null 2>&1 && [[ -t 1 ]]; then
    _b=${_b:-$(tput bold)}; _dim=${_dim:-$(tput dim)}; _red=${_red:-$(tput setaf 1)}; _grn=${_grn:-$(tput setaf 2)}; _ylw=${_ylw:-$(tput setaf 3)}; _blu=${_blu:-$(tput setaf 4)}; _mag=${_mag:-$(tput setaf 5)}; _cyn=${_cyn:-$(tput setaf 6)}; _rst=${_rst:-$(tput sgr0)}
  else
    _b=""; _dim=""; _red=""; _grn=""; _ylw=""; _blu=""; _mag=""; _cyn=""; _rst=""
  fi
}

# Basic logging helpers (honor QUIET)
_print() { [[ "${QUIET:-0}" == "1" ]] && return 0; printf "%b\n" "$*"; }
log_step() { _print "${_b}${_cyn}==> ${1}${_rst}"; }
log_ok()   { _print "${_grn}${1}${_rst}"; }
log_warn() { _print "${_ylw}${1}${_rst}"; }
log_err()  { _print "${_red}${1}${_rst}"; }

# Parse common flags; sets globals: AUTO_YES, DRY_RUN, QUIET, DEBUG, REGENERATE_TOKEN, SHOW_TOKEN, PROJECT_DIR
parse_common_flags() {
  AUTO_YES=${AUTO_YES:-0}
  DRY_RUN=${DRY_RUN:-0}
  QUIET=${QUIET:-0}
  DEBUG=${DEBUG:-0}
  REGENERATE_TOKEN=${REGENERATE_TOKEN:-0}
  SHOW_TOKEN=${SHOW_TOKEN:-0}
  PROJECT_DIR=${PROJECT_DIR:-}
  local -a args=("$@");
  for ((i=0; i<${#args[@]}; i++)); do
    a="${args[$i]}"
    case "$a" in
      --yes) AUTO_YES=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --quiet) QUIET=1 ;;
      --debug) DEBUG=1 ;;
      --regenerate-token) REGENERATE_TOKEN=1 ;;
      --show-token) SHOW_TOKEN=1 ;;
      --project-dir) i=$((i+1)); PROJECT_DIR="${args[$i]:-}" ;;
      --project-dir=*) PROJECT_DIR="${a#*=}" ;;
    esac
  done
  export AUTO_YES DRY_RUN QUIET DEBUG REGENERATE_TOKEN SHOW_TOKEN PROJECT_DIR
  if [[ "${DEBUG}" == "1" ]]; then set -x; fi
}

# Traps and diagnostics
setup_traps() {
  if [[ "${DEBUG:-0}" == "1" ]]; then
    set -o errtrace
    trap 'last=$BASH_COMMAND; log_err "Error on: ${last}"' ERR
  fi
}

# Dependency checks
require_cmd() {
  local cmd="$1"; shift || true
  command -v "$cmd" >/dev/null 2>&1 || { log_err "Missing dependency: $cmd"; exit 1; }
}

# Read a single KEY=value line from a dotenv-shaped file. Last occurrence wins.
# Strips leading whitespace, the literal `KEY=` prefix, one pair of surrounding
# quotes (single or double), and any trailing CR (so CRLF dotenv files work).
# Returns the unquoted value on stdout, exit 0 if found, exit 1 otherwise.
_env_file_value() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 1
  local raw
  raw=$(grep -E "^[[:space:]]*${key}=" "$file" 2>/dev/null | tail -n 1) || return 1
  [[ -z "$raw" ]] && return 1
  # Strip leading whitespace (tabs/spaces).
  raw="${raw#${raw%%[![:space:]]*}}"
  # Strip the `KEY=` prefix.
  raw="${raw#${key}=}"
  # Strip a trailing CR (CRLF source files) BEFORE quote stripping, otherwise
  # the closing quote is no longer at the end of the string and we would leak
  # the inner `"`.
  raw="${raw%$'\r'}"
  # Strip one matching pair of surrounding quotes (double or single). We
  # require the open and close to be the same kind so we do not accidentally
  # eat a single trailing quote out of a value like KEY=foo"bar.
  if [[ ${#raw} -ge 2 && "${raw:0:1}" == '"' && "${raw: -1}" == '"' ]]; then
    raw="${raw:1:${#raw}-2}"
  elif [[ ${#raw} -ge 2 && "${raw:0:1}" == "'" && "${raw: -1}" == "'" ]]; then
    raw="${raw:1:${#raw}-2}"
  fi
  printf '%s\n' "$raw"
}

# Resolve the streamable-HTTP MCP endpoint used by coding clients.  Client
# installers deliberately do not inspect the repository's .env: a checkout can
# describe the server process running there, but it cannot describe which
# server a user-level client on this machine should connect to.
#
# Precedence:
#   1. INTEGRATION_MCP_URL (already a complete MCP endpoint)
#   2. AGENT_MAIL_URL (complete endpoint or server base URL)
#   3. AGENT_MAIL_URL from ~/.agent-mail.env
resolve_integration_mcp_url() {
  local url="${INTEGRATION_MCP_URL:-}" env_file
  if [[ -z "$url" ]]; then
    url="${AGENT_MAIL_URL:-}"
  fi
  if [[ -z "$url" ]]; then
    env_file="${AGENT_MAIL_ENV_FILE:-${HOME}/.agent-mail.env}"
    url="$(_env_file_value "$env_file" AGENT_MAIL_URL 2>/dev/null || true)"
  fi
  [[ -n "$url" ]] || return 1
  case "$url" in
    http://*|https://*) ;;
    *) log_err "Agent Mail MCP URL must start with http:// or https://: ${url}"; return 1 ;;
  esac
  case "$url" in
    *$'\n'*|*$'\r'*) log_err "Agent Mail MCP URL must be a single line"; return 1 ;;
  esac

  # INTEGRATION_MCP_URL is explicitly the final endpoint and is never changed.
  if [[ -n "${INTEGRATION_MCP_URL:-}" ]]; then
    printf '%s\n' "$url"
    return 0
  fi
  case "$url" in
    */mcp|*/mcp/) printf '%s/\n' "${url%/}" ;;
    */api/) printf '%s/mcp/\n' "${url%/api/}" ;;
    */api) printf '%s/mcp/\n' "${url%/api}" ;;
    *) printf '%s/mcp/\n' "${url%/}" ;;
  esac
}

# Strict bearer resolver for user-level client installations.  Unlike the
# legacy resolver below, it never reads a repository .env, a server credential
# file, or a generated run helper.
resolve_global_integration_bearer_token() {
  local token env_file
  for token in "${INTEGRATION_BEARER_TOKEN:-}" "${HTTP_BEARER_TOKEN:-}"; do
    if [[ -n "$token" ]]; then
      case "$token" in *$'\n'*|*$'\r'*) log_err "Bearer token must be a single line"; return 1 ;; esac
      printf '%s\n' "$token"
      return 0
    fi
  done
  env_file="${AGENT_MAIL_ENV_FILE:-${HOME}/.agent-mail.env}"
  token="$(_env_file_value "$env_file" HTTP_BEARER_TOKEN 2>/dev/null || true)"
  [[ -n "$token" ]] || return 1
  case "$token" in *$'\n'*|*$'\r'*) log_err "Bearer token must be a single line"; return 1 ;; esac
  printf '%s\n' "$token"
}

# Normalize a private Agent Mail state directory into the path dialect used by
# the current Bash process.  A native Windows path is never allowed to fall
# through as a relative POSIX filename: that would create artifacts such as
# ``D:\Profiles\...`` in whichever repository happened to be current.
normalize_agent_mail_state_dir() {
  local target="$1" normalized system
  case "$target" in
    *$'\n'*|*$'\r'*)
      log_err "Agent Mail state directory must be a single-line path." >&2
      return 1 ;;
  esac
  case "$target" in
    [a-zA-Z]:\\*|[a-zA-Z]:/*)
      system="$(uname -s 2>/dev/null || printf unknown)"
      case "$system" in
        MINGW*|MSYS*|CYGWIN*)
          command -v cygpath >/dev/null 2>&1 || {
            log_err "cygpath is required to normalize the Agent Mail state directory." >&2
            return 1
          }
          normalized="$(cygpath -u "$target" 2>/dev/null)" || {
            log_err "Could not normalize the Agent Mail state directory." >&2
            return 1
          } ;;
        *)
          command -v wslpath >/dev/null 2>&1 || {
            log_err "wslpath is required to normalize the Agent Mail state directory." >&2
            return 1
          }
          normalized="$(wslpath -u "$target" 2>/dev/null)" || {
            log_err "Could not normalize the Agent Mail state directory." >&2
            return 1
          } ;;
      esac ;;
    *) normalized="$target" ;;
  esac
  case "$normalized" in
    *$'\n'*|*$'\r'*)
      log_err "Normalized Agent Mail state directory must be a single-line path." >&2
      return 1 ;;
  esac
  case "$normalized" in
    /*) ;;
    *)
      log_err "Agent Mail state directory must be an absolute path." >&2
      return 1 ;;
  esac
  printf '%s\n' "$normalized"
}

# Source the hook library for its path-validation side effects WITHOUT
# inheriting its process-global MSYS_NO_PATHCONV.
#
# That export is correct for hooks and wrong for us, and the two needs are in
# direct conflict inside a single command — measured on native Windows:
#
#   jq -nc --arg k "/owner/repo" ...        without: "C:/Program Files/Git/owner/repo"
#                                           with:    "/owner/repo"          <- hooks need this
#   jq -r ... "$SOME_FILE"                  without: opens the file
#                                           with:    "Could not open file"  <- we need this
#
# Hooks pass a project key of "/owner/repo" to a native jq.exe and must not let
# Git Bash rewrite it into a phantom project no other machine can join; their
# own file arguments go through am_normalize_runtime_user_path, so they are
# safe. Installers pass real file paths and no project key, so for them the
# variable is pure damage: it made jq unable to read the client config, which
# surfaced as "Existing settings are not a mergeable JSON object".
#
# Saving and restoring around the source keeps each side with the setting it
# actually needs, and keeps the decision in one place rather than six.
source_hook_library_without_pathconv() {
  local lib="$1" _had_pathconv="${MSYS_NO_PATHCONV+set}" _old_pathconv="${MSYS_NO_PATHCONV:-}"
  # shellcheck disable=SC1090
  . "$lib"
  if [[ "$_had_pathconv" == "set" ]]; then
    export MSYS_NO_PATHCONV="$_old_pathconv"
  else
    unset MSYS_NO_PATHCONV
  fi
}

# Answer "is this directory inside a Git worktree or gitdir?", failing CLOSED.
#
# Exit 0 means REFUSE: it either is inside Git, or we could not find out.
# Exit 1 means a definite no.
#
# Two things here are deliberate and neither is stylistic.
#
# 1. It `cd`s into the directory and runs `git rev-parse` with NO `-C`.  Passing
#    a path to a NATIVE git.exe is what broke this guard on Windows: the hook
#    library exports MSYS_NO_PATHCONV=1 at file scope and six installers source
#    it, after which `git -C /c/...` can no longer chdir and exits 128.  Letting
#    git inherit the cwd instead makes the answer identical with and without
#    that variable — measured on native Windows — so the guard stops depending
#    on path translation rather than working around it.
#
# 2. It does NOT branch on git's exit code, because that code does not
#    discriminate: `rc=128` is git's ordinary answer for "not a repository",
#    which is the very case a healthy install must be allowed through.  Nor does
#    it match on git's message, which is localised and would be disarmed by a
#    user running with LANG set to anything but English.
#
# What it does not catch, stated so the gap is a decision and not an oversight:
# a corrupt `.git` that still lets `cd` succeed reads as "not a worktree".
path_is_inside_git() {
  local dir="$1" verdict

  if ! command -v git >/dev/null 2>&1; then
    log_err "Cannot verify ${dir} lies outside Git: git is not on PATH. Refusing."
    return 0
  fi
  if ! ( cd -- "$dir" ) >/dev/null 2>&1; then
    log_err "Cannot verify ${dir} lies outside Git: directory is unreachable. Refusing."
    return 0
  fi

  verdict="$( cd -- "$dir" >/dev/null 2>&1 && git rev-parse --is-inside-work-tree 2>/dev/null )"
  [[ "$verdict" == "true" ]] && return 0
  verdict="$( cd -- "$dir" >/dev/null 2>&1 && git rev-parse --is-inside-git-dir 2>/dev/null )"
  [[ "$verdict" == "true" ]] && return 0
  return 1
}

# Keep the shared hook environment intentionally small.  Client, slot, project,
# agent name and per-agent registration tokens belong to a client wrapper or to
# credentials.json, never to this cross-client file.
write_shared_agent_mail_env() {
  local url="$1" token="$2" env_file state existing_state line stripped key merged current
  env_file="${AGENT_MAIL_ENV_FILE:-${HOME}/.agent-mail.env}"
  state="${AGENT_MAIL_STATE_DIR:-}"
  existing_state="$(_env_file_value "$env_file" AGENT_MAIL_STATE_DIR 2>/dev/null || true)"
  [[ -n "$state" ]] || state="$existing_state"
  case "$url$token$state" in
    *$'\n'*|*$'\r'*) log_err "Shared Agent Mail settings must be single-line values"; return 1 ;;
  esac
  if [[ -n "$state" ]]; then
    state="$(normalize_agent_mail_state_dir "$state")" || return 1
  fi
  local DRY_RUN="${DRY_RUN:-0}"
  # Bash 3.2 (the system shell on macOS) can mistake an unbalanced case-label
  # ')' for the end of the surrounding command substitution.  The optional
  # opening '(' is valid case syntax and keeps that older parser balanced.
  merged="$({
    if [[ -f "$env_file" ]]; then
      while IFS= read -r line || [[ -n "$line" ]]; do
        stripped="${line#${line%%[![:space:]]*}}"
        stripped="${stripped#export }"
        key="${stripped%%=*}"
        case "$key" in
          (AGENT_MAIL_URL|HTTP_BEARER_TOKEN|AGENT_MAIL_STATE_DIR|AGENT_MAIL_AGENT|AGENT_MAIL_PROJECT|AGENT_MAIL_PROJECT_KEY|AGENT_MAIL_CLIENT|AGENT_MAIL_SLOT|AGENT_MAIL_REGISTRATION_TOKEN|AGENT_MAIL_TOKEN|AGENT_MAIL_*_SLOT)
            continue ;;
        esac
        printf '%s\n' "$line"
      done < "$env_file"
    fi
    printf 'AGENT_MAIL_URL=%s\n' "$url"
    printf 'HTTP_BEARER_TOKEN=%s\n' "$token"
    [[ -n "$state" ]] && printf 'AGENT_MAIL_STATE_DIR=%s\n' "$state"
  })"
  current="$(cat "$env_file" 2>/dev/null || true)"
  if [[ "$current" == "$merged" ]]; then
    return 0
  fi
  if [[ -f "$env_file" ]]; then
    if [[ -n "$state" ]]; then
      AGENT_MAIL_STATE_DIR="$state" backup_user_file "$env_file"
    else
      backup_user_file "$env_file"
    fi
  fi
  printf '%s\n' "$merged" | write_atomic "$env_file"
  [[ "$DRY_RUN" == "1" ]] || chmod 600 "$env_file" 2>/dev/null || true
}

# VS Code stores user MCP servers beside user settings, not below a workspace.
# The explicit overrides also make the path testable and support portable or
# custom --user-data-dir installations.
integration_vscode_user_mcp_path() {
  if [[ -n "${VSCODE_MCP_CONFIG_PATH:-}" ]]; then
    printf '%s\n' "$VSCODE_MCP_CONFIG_PATH"
    return 0
  fi
  if [[ -n "${VSCODE_USER_DATA_DIR:-}" ]]; then
    printf '%s/User/mcp.json\n' "${VSCODE_USER_DATA_DIR%/}"
    return 0
  fi
  case "$(uname -s 2>/dev/null)" in
    Darwin)
      printf '%s/Library/Application Support/Code/User/mcp.json\n' "$HOME" ;;
    MINGW*|MSYS*|CYGWIN*)
      local appdata="${APPDATA:-}"
      [[ -n "$appdata" ]] || { log_err "APPDATA is required to locate VS Code user configuration"; return 1; }
      if command -v cygpath >/dev/null 2>&1; then
        appdata="$(cygpath -u "$appdata" 2>/dev/null || printf '%s' "$appdata")"
      fi
      printf '%s/Code/User/mcp.json\n' "${appdata%/}" ;;
    *)
      printf '%s/Code/User/mcp.json\n' "${XDG_CONFIG_HOME:-${HOME}/.config}" ;;
  esac
}

# Resolve the active integration bearer token via a layered cascade.
# Order (first non-empty wins):
#   1. INTEGRATION_BEARER_TOKEN  (orchestrator-supplied session override)
#   2. HTTP_BEARER_TOKEN          (already-running server env)
#   3. MCP_AGENT_MAIL_TOKEN       (project-specific override)
#   4. /run/credentials/mcp-agent-mail.service/agent-mail-token  (systemd LoadCredential)
#   5. <root_dir>/.env            (project-local dotenv with HTTP_BEARER_TOKEN=)
#   6. ${MCP_AGENT_MAIL_ENV_FILE} (operator-pinned env file)
#   7. ${HOME}/.config/mcp-agent-mail/config.env  (user-default)
#   8. legacy: scripts/run_server_with_token.sh  (older installs)
# Always exits 0; emits empty line when nothing matched so callers can branch
# on the result without distinguishing exit codes.
resolve_integration_bearer_token() {
  local root_dir="${1:-$PWD}"
  root_dir="${root_dir%/}"
  local cred_file="/run/credentials/mcp-agent-mail.service/agent-mail-token"
  local token

  for token in \
    "${INTEGRATION_BEARER_TOKEN:-}" \
    "${HTTP_BEARER_TOKEN:-}" \
    "${MCP_AGENT_MAIL_TOKEN:-}"; do
    if [[ -n "$token" ]]; then
      printf '%s\n' "$token"
      return 0
    fi
  done

  if [[ -r "$cred_file" ]]; then
    token=$(tr -d '\r\n' < "$cred_file" 2>/dev/null) || token=""
    if [[ -n "$token" ]]; then
      printf '%s\n' "$token"
      return 0
    fi
  fi

  local f
  for f in \
    "${root_dir}/.env" \
    "${MCP_AGENT_MAIL_ENV_FILE:-}" \
    "${HOME}/.config/mcp-agent-mail/config.env"; do
    [[ -z "$f" ]] && continue
    [[ -f "$f" ]] || continue
    if token=$(_env_file_value "$f" "HTTP_BEARER_TOKEN") && [[ -n "$token" ]]; then
      printf '%s\n' "$token"
      return 0
    fi
  done

  local legacy="${root_dir}/scripts/run_server_with_token.sh"
  if [[ -f "$legacy" ]]; then
    token=$(grep -E '^[[:space:]]*export[[:space:]]+HTTP_BEARER_TOKEN="' "$legacy" 2>/dev/null \
      | tail -n 1 \
      | sed -E 's/.*HTTP_BEARER_TOKEN="([^"]+)".*/\1/') || token=""
    if [[ -n "$token" ]]; then
      printf '%s\n' "$token"
      return 0
    fi
  fi

  printf '\n'
  return 0
}

# Generate a fresh bearer token using whatever entropy source is available.
# Probes: openssl > python3 > python > uv-run python > /dev/urandom > timestamp.
# Output: 64 hex chars on every path except the last-resort timestamp fallback,
# which emits "<unix-ns>_<hostname>" only when no real entropy source exists.
#
# Each candidate is captured into a local variable and length-validated before
# being accepted. This prevents partial stdout from a failed candidate (e.g.
# python printing diagnostic text to stdout before crashing) from leaking into
# the result, and rejects short reads from /dev/urandom that occur in pre-seed
# / restricted environments.
generate_bearer_token() {
  local token=""
  if command -v openssl >/dev/null 2>&1; then
    token=$(openssl rand -hex 32 2>/dev/null) || token=""
    [[ ${#token} -eq 64 ]] || token=""
  fi
  if [[ -z "$token" ]] && command -v python3 >/dev/null 2>&1; then
    token=$(python3 -c 'import secrets;print(secrets.token_hex(32))' 2>/dev/null) || token=""
    [[ ${#token} -eq 64 ]] || token=""
  fi
  if [[ -z "$token" ]] && command -v python >/dev/null 2>&1; then
    token=$(python -c 'import secrets;print(secrets.token_hex(32))' 2>/dev/null) || token=""
    [[ ${#token} -eq 64 ]] || token=""
  fi
  if [[ -z "$token" ]] && command -v uv >/dev/null 2>&1; then
    token=$(uv run --no-project python -c 'import secrets;print(secrets.token_hex(32))' 2>/dev/null) || token=""
    [[ ${#token} -eq 64 ]] || token=""
  fi
  if [[ -z "$token" ]] && [[ -r /dev/urandom ]]; then
    token=$(head -c 32 /dev/urandom 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n') || token=""
    [[ ${#token} -eq 64 ]] || token=""
  fi
  if [[ -z "$token" ]]; then
    token="$(date +%s%N 2>/dev/null || date +%s)_$(hostname 2>/dev/null || echo host)"
  fi
  printf '%s\n' "$token"
}

# Write the standard run-helper script at the given path. The helper resolves
# HTTP_BEARER_TOKEN via the lib.sh cascade at runtime (preferred) or falls back
# to its own minimal env-file resolution when lib.sh is missing.
write_run_helper_script() {
  local target="$1"
  write_atomic "$target" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${ROOT_DIR}/scripts/lib.sh" ]]; then
  # shellcheck disable=SC1090
  . "${ROOT_DIR}/scripts/lib.sh"
fi

if [[ -z "${HTTP_BEARER_TOKEN:-}" ]] && declare -F resolve_integration_bearer_token >/dev/null 2>&1; then
  HTTP_BEARER_TOKEN="$(resolve_integration_bearer_token "${ROOT_DIR}")"
fi
if [[ -z "${HTTP_BEARER_TOKEN:-}" ]]; then
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    HTTP_BEARER_TOKEN=$(grep -E '^HTTP_BEARER_TOKEN=' "${ROOT_DIR}/.env" 2>/dev/null | tail -n 1 | sed -E 's/^HTTP_BEARER_TOKEN=//') || true
  fi
fi
if [[ -z "${HTTP_BEARER_TOKEN:-}" ]] && declare -F generate_bearer_token >/dev/null 2>&1; then
  HTTP_BEARER_TOKEN="$(generate_bearer_token)"
fi
if [[ -z "${HTTP_BEARER_TOKEN:-}" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    HTTP_BEARER_TOKEN="$(openssl rand -hex 32)"
  elif command -v uv >/dev/null 2>&1; then
    HTTP_BEARER_TOKEN="$(uv run python -c 'import secrets;print(secrets.token_hex(32))')"
  else
    HTTP_BEARER_TOKEN="$(date +%s)_$(hostname 2>/dev/null || echo host)"
  fi
fi
export HTTP_BEARER_TOKEN

uv run python -m mcp_agent_mail.cli serve-http "$@"
SH
  set_secure_exec "$target" || true
}

# Atomic write: read content from stdin and atomically move to target
write_atomic() {
  local target="$1"; shift || true
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    _print "[dry-run] write ${target}"
    cat >/dev/null # consume stdin
    return 0
  fi

  local dir; dir=$(dirname "$target")

  # Create directory with error checking
  if ! mkdir -p "$dir"; then
    echo "ERROR: Failed to create directory ${dir}" >&2
    return 1
  fi

  local tmp
  tmp="${target}.tmp.$$"

  # Set up cleanup trap for temp file (double quotes = expand now, not at trap execution)
  trap "rm -f \"$tmp\" 2>/dev/null" EXIT INT TERM

  # Create temp file with secure permissions (600)
  umask 077
  if ! cat >"$tmp"; then
    echo "ERROR: Failed to write temp file ${tmp}" >&2
    rm -f "$tmp" 2>/dev/null
    trap - EXIT INT TERM
    return 1
  fi

  # Atomic move
  if ! mv "$tmp" "$target"; then
    echo "ERROR: Failed to move ${tmp} to ${target}" >&2
    rm -f "$tmp" 2>/dev/null
    trap - EXIT INT TERM
    return 1
  fi

  # Clear trap after successful completion
  trap - EXIT INT TERM
}

# JSON validate via jq or Python
json_validate() {
  local file="$1"
  if command -v jq >/dev/null 2>&1; then
    jq empty "$file" >/dev/null 2>&1 || { log_err "Invalid JSON: $file"; return 1; }
  else
    if command -v python >/dev/null 2>&1; then
      python -c 'import json,sys; json.load(open(sys.argv[1],"r",encoding="utf-8"))' "$file" >/dev/null 2>&1 || { log_err "Invalid JSON: $file"; return 1; }
    else
      uv run python -c 'import json,sys; json.load(open(sys.argv[1],"r",encoding="utf-8"))' "$file" >/dev/null 2>&1 || { log_err "Invalid JSON: $file"; return 1; }
    fi
  fi
}

# Escape a string for safe embedding in JSON
# Usage: escaped=$(json_escape_string "$raw_string") || exit 1
# Returns: JSON-escaped string WITH quotes (e.g., "value")
# Exits with error if escaping fails
json_escape_string() {
  local raw="$1"
  local result

  if command -v jq >/dev/null 2>&1; then
    # Use jq for proper JSON escaping
    if ! result=$(jq -n --arg str "$raw" '$str' 2>&1); then
      echo "ERROR: jq failed to escape JSON string" >&2
      return 1
    fi
  elif command -v python >/dev/null 2>&1; then
    if ! result=$(python -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$raw" 2>&1); then
      echo "ERROR: python failed to escape JSON string" >&2
      return 1
    fi
  elif command -v uv >/dev/null 2>&1; then
    if ! result=$(uv run python -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$raw" 2>&1); then
      echo "ERROR: uv python failed to escape JSON string" >&2
      return 1
    fi
  else
    echo "ERROR: No JSON escaping tool available (need jq, python, or uv)" >&2
    return 1
  fi

  # Validate result is non-empty
  if [[ -z "$result" ]]; then
    echo "ERROR: JSON escaping produced empty result" >&2
    return 1
  fi

  echo "$result"
}

# Merge MCP server config into existing settings JSON
# Usage: json_merge_mcp_server <existing_json> <server_name> <server_config_json>
# Returns: merged JSON preserving all existing keys
# Requires: jq (for safe JSON handling without quote injection vulnerabilities)
json_merge_mcp_server() {
  local existing="$1"
  local server_name="$2"
  local server_config="$3"

  if ! command -v jq >/dev/null 2>&1; then
    log_err "jq is required for JSON merge. Install: brew install jq (macOS) or apt install jq (Linux)"
    return 1
  fi

  echo "$existing" | jq --arg name "$server_name" --argjson config "$server_config" \
    '.mcpServers = (.mcpServers // {}) | .mcpServers[$name] = $config'
}

# Return the Morph grep-only tool allowlist unless explicitly overridden.
default_morph_enabled_tools() {
  printf '%s\n' "${MORPH_ENABLED_TOOLS:-warpgrep_codebase_search,warpgrep_github_search}"
}

# Best-effort Morph API key discovery for CLI installers.
# Priority:
#   1) MORPH_API_KEY env var
#   2) ~/.codex/config.toml
#   3) ~/.gemini/settings.json
#   4) ~/.claude/settings.json
#   5) legacy ~/.claude.json
resolve_morph_api_key() {
  local key=""

  if [[ -n "${MORPH_API_KEY:-}" ]]; then
    printf '%s\n' "${MORPH_API_KEY}"
    return 0
  fi

  if [[ -f "${HOME}/.codex/config.toml" ]]; then
    key=$(sed -n 's/^MORPH_API_KEY = "\(.*\)"$/\1/p' "${HOME}/.codex/config.toml" | head -n 1)
    if [[ -n "${key}" ]]; then
      printf '%s\n' "${key}"
      return 0
    fi
  fi

  if command -v jq >/dev/null 2>&1; then
    if [[ -f "${HOME}/.gemini/settings.json" ]]; then
      key=$(jq -r '.mcpServers["morph-mcp"].env.MORPH_API_KEY // empty' "${HOME}/.gemini/settings.json" 2>/dev/null || true)
      if [[ -n "${key}" ]]; then
        printf '%s\n' "${key}"
        return 0
      fi
    fi

    if [[ -f "${HOME}/.claude/settings.json" ]]; then
      key=$(jq -r '.mcpServers["morph-mcp"].env.MORPH_API_KEY // empty' "${HOME}/.claude/settings.json" 2>/dev/null || true)
      if [[ -n "${key}" ]]; then
        printf '%s\n' "${key}"
        return 0
      fi
    fi

    if [[ -f "${HOME}/.claude.json" ]]; then
      key=$(jq -r '.mcpServers["morph-mcp"].env.MORPH_API_KEY // empty' "${HOME}/.claude.json" 2>/dev/null || true)
      if [[ -n "${key}" ]]; then
        printf '%s\n' "${key}"
        return 0
      fi
    fi
  fi

  printf '\n'
}

# Append hook to existing hooks array without duplicating
# Usage: json_append_hook <existing_json> <hook_type> <hook_json> <identifier>
# hook_type: SessionStart, PreToolUse, PostToolUse
# identifier: string to check for duplicates (e.g., "mcp-agent-mail")
# Requires: jq (for safe JSON handling without quote injection vulnerabilities)
json_append_hook() {
  local existing="$1"
  local hook_type="$2"
  local hook_json="$3"
  local identifier="$4"

  if ! command -v jq >/dev/null 2>&1; then
    log_err "jq is required for JSON merge. Install: brew install jq (macOS) or apt install jq (Linux)"
    return 1
  fi

  # Check if already exists (use --arg to safely pass identifier).
  #
  # `any`, not a bare stream: `jq -e` sets its exit status from the LAST value
  # the filter produces, so a stream of true,false answered "absent" whenever
  # the marker matched anything other than the final command. An entry with two
  # commands was therefore appended again on every run — and the previous
  # markers here happened to match the last command, which is why nobody saw it.
  if echo "$existing" | jq -e --arg id "$identifier" \
      "any(.hooks.${hook_type}[]? | .hooks[]? | .command; contains(\$id))" >/dev/null 2>&1; then
    # Already exists, return unchanged
    echo "$existing"
    return
  fi
  # Append new hook
  echo "$existing" | jq --argjson hook "$hook_json" \
    ".hooks = (.hooks // {}) | .hooks.${hook_type} = ((.hooks.${hook_type} // []) + [\$hook])"
}

# Ensure settings.local.json is in .gitignore
# Usage: ensure_gitignore_entry <gitignore_path> <pattern>
ensure_gitignore_entry() {
  local gitignore="$1"
  local pattern="$2"

  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] ensure ${pattern} in ${gitignore}"
    return 0
  fi

  if [[ -f "$gitignore" ]]; then
    if ! grep -qF "$pattern" "$gitignore" 2>/dev/null; then
      echo "" >> "$gitignore"
      echo "# Claude Code local settings (contains secrets)" >> "$gitignore"
      echo "$pattern" >> "$gitignore"
      log_ok "Added ${pattern} to .gitignore"
    fi
  else
    cat > "$gitignore" <<EOF
# Claude Code local settings (contains secrets)
${pattern}
EOF
    log_ok "Created .gitignore with ${pattern}"
  fi
}

# Set file permissions to 600 with error checking
set_secure_file() {
  local file="$1"
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] chmod 600 ${file}"
    return 0
  fi
  if [[ ! -e "$file" ]]; then
    log_warn "Cannot chmod: file does not exist: ${file}"
    return 1
  fi
  if ! chmod 600 "$file" 2>/dev/null; then
    log_warn "Failed to chmod 600 ${file} (permissions/readonly filesystem?)"
    return 1
  fi
}

# Set file permissions to 700 (executable) with error checking
set_secure_exec() {
  local file="$1"
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] chmod 700 ${file}"
    return 0
  fi
  if [[ ! -e "$file" ]]; then
    log_warn "Cannot chmod: file does not exist: ${file}"
    return 1
  fi
  if ! chmod 700 "$file" 2>/dev/null; then
    log_warn "Failed to chmod 700 ${file} (permissions/readonly filesystem?)"
    return 1
  fi
}

# Set directory permissions to 700 with error checking
set_secure_dir() {
  local dir="$1"
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] chmod 700 ${dir}"
    return 0
  fi
  if [[ ! -d "$dir" ]]; then
    log_warn "Cannot chmod: directory does not exist: ${dir}"
    return 1
  fi
  if ! chmod 700 "$dir" 2>/dev/null; then
    log_warn "Failed to chmod 700 ${dir} (permissions/readonly filesystem?)"
    return 1
  fi
}

# Readiness polling: host, port, path, tries, delay_seconds
readiness_poll() {
  local host="$1"; local port="$2"; local path="$3"; local tries="$4"; local delay="$5"
  local url="http://${host}:${port}${path}"
  local n
  for ((n=0; n<tries; n++)); do
    if curl -fsS --connect-timeout 1 --max-time 2 --retry 0 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

# Run command honoring DRY_RUN
run_cmd() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] $*"
    return 0
  fi
  "$@"
}

# Back up user-profile configuration without writing into the current
# repository.  These files can contain bearer tokens, so both directory and
# copy are private.  Backups are timestamped and never pruned automatically.
backup_user_file() {
  local file="$1" state_dir backup_dir rel safe stamp target counter
  [[ -f "$file" ]] || return 0
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    _print "[dry-run] backup ${file}"
    return 0
  fi
  state_dir="${AGENT_MAIL_STATE_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/agent-mail}"
  state_dir="$(normalize_agent_mail_state_dir "$state_dir")" || return 1
  backup_dir="${state_dir}/backups"
  mkdir -p "$backup_dir" || return 1
  chmod 700 "$state_dir" "$backup_dir" 2>/dev/null || true
  rel="$file"
  [[ -n "${HOME:-}" && "$file" == "${HOME}/"* ]] && rel="${file#${HOME}/}"
  safe="$(printf '%s' "$rel" | tr '/\\ ' '___' | tr -cd '[:alnum:]_.-' | cut -c1-160)"
  [[ -n "$safe" ]] || safe="config"
  # BSD date (macOS) does not implement GNU's %N and prints it literally.
  # PID plus a collision counter keeps repeated and concurrent installs from
  # overwriting an earlier backup on every supported platform.
  stamp="$(date +%Y%m%d_%H%M%S 2>/dev/null || date +%s)"
  target="${backup_dir}/${safe}.${stamp}.$$.bak"
  counter=0
  while [[ -e "$target" ]]; do
    counter=$((counter + 1))
    target="${backup_dir}/${safe}.${stamp}.$$.${counter}.bak"
  done
  cp "$file" "$target" || return 1
  chmod 600 "$target" 2>/dev/null || true
  _print "Backed up ${file} to ${target}"
}

# Backup a file to backup_config_files/ with timestamp before .bak extension
# Usage: backup_file "/path/to/file"
#
# Creates distinguishable backup names for files from different locations:
#   - HOME files: home_.claude_settings.json.TIMESTAMP.bak
#   - Project files: local_claude_settings.json.TIMESTAMP.bak
backup_file() {
  local file="$1"

  # Validate input
  if [[ -z "$file" ]]; then
    echo "ERROR: backup_file called with empty file path" >&2
    return 1
  fi

  if [[ ! -f "$file" ]]; then
    return 0  # Nothing to backup
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] backup ${file}"
    return 0
  fi

  # Create backup directory at project root
  local backup_dir="backup_config_files"
  if ! mkdir -p "$backup_dir"; then
    echo "ERROR: Failed to create backup directory ${backup_dir}" >&2
    return 1
  fi

  # Create unique backup name that encodes path information
  # Sanitize glob metacharacters to prevent find pattern matching issues
  local backup_name
  # Check if file is under HOME (require trailing slash to avoid false prefix matches)
  # Also validate HOME is non-empty to avoid matching everything
  if [[ -n "$HOME" && "$file" == "$HOME/"* ]]; then
    # HOME directory file - use relative path from HOME
    local rel_path="${file#$HOME/}"
    rel_path="${rel_path//\//_}"  # Replace / with _
    # Sanitize glob metacharacters: * ? [ ] { }
    rel_path="${rel_path//\*/STAR}"
    rel_path="${rel_path//\?/QMARK}"
    rel_path="${rel_path//\[/LBRACK}"
    rel_path="${rel_path//\]/RBRACK}"
    rel_path="${rel_path//\{/LBRACE}"
    rel_path="${rel_path//\}/RBRACE}"
    backup_name="home_${rel_path}"
  else
    # Non-HOME path (project-local or absolute)
    local sanitized="${file//\//_}"  # Replace / with _
    # Sanitize glob metacharacters: * ? [ ] { }
    sanitized="${sanitized//\*/STAR}"
    sanitized="${sanitized//\?/QMARK}"
    sanitized="${sanitized//\[/LBRACK}"
    sanitized="${sanitized//\]/RBRACK}"
    sanitized="${sanitized//\{/LBRACE}"
    sanitized="${sanitized//\}/RBRACE}"
    # Remove leading dots and underscores
    while [[ "$sanitized" == .* ]] || [[ "$sanitized" == _* ]]; do
      sanitized="${sanitized#.}"
      sanitized="${sanitized#_}"
    done
    backup_name="local_${sanitized}"
  fi

  # Create backup with timestamp (nanoseconds for uniqueness) BEFORE .bak extension
  local timestamp
  timestamp=$(date +%Y%m%d_%H%M%S_%N)
  local backup_path="${backup_dir}/${backup_name}.${timestamp}.bak"

  # Copy with error handling
  if ! cp "$file" "$backup_path"; then
    echo "ERROR: Failed to backup ${file}" >&2
    return 1
  fi

  _print "Backed up ${file} to ${backup_path}"

  # Cleanup old backups (keep last 10 for this file pattern)
  cleanup_old_backups "$backup_dir" "$backup_name" 10
}

# Cleanup old backup files, keeping only the most recent N
# Usage: cleanup_old_backups <backup_dir> <backup_pattern> <keep_count>
cleanup_old_backups() {
  local backup_dir="$1"
  local backup_pattern="$2"
  local keep_count="${3:-10}"

  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] cleanup old backups matching ${backup_pattern}"
    return 0
  fi

  # Find all backups matching this pattern, sort by timestamp (newest first), delete old ones
  # Pattern: ${backup_pattern}.TIMESTAMP.bak
  local old_backups
  old_backups=$(find "$backup_dir" -maxdepth 1 -name "${backup_pattern}.*.bak" -type f 2>/dev/null | sort -r | tail -n +$((keep_count + 1)))

  if [[ -n "$old_backups" ]]; then
    while IFS= read -r old_backup; do
      rm -f "$old_backup" 2>/dev/null && _print "Removed old backup: ${old_backup}"
    done <<< "$old_backups"
  fi
}

# Update or append env var in .env atomically (backup first)
update_env_var() {
  local key="$1"; local value="$2"; local env_file=".env"
  if [[ "${DRY_RUN}" == "1" ]]; then _print "[dry-run] set ${key} in .env"; return 0; fi

  local tmp="${env_file}.tmp.$$"
  # Double quotes = expand now, not at trap execution
  trap "rm -f \"$tmp\" 2>/dev/null" EXIT INT TERM

  if [[ -f "$env_file" ]]; then
    backup_file "$env_file"

    # Use atomic write: read old file, modify, write to temp, move
    if grep -q "^${key}=" "$env_file"; then
      # Replace existing key
      umask 077
      if ! sed -E "s/^${key}=.*/${key}=${value}/" "$env_file" > "$tmp"; then
        rm -f "$tmp" 2>/dev/null
        trap - EXIT INT TERM
        echo "ERROR: Failed to update ${key} in ${env_file}" >&2
        return 1
      fi
    else
      # Append new key
      umask 077
      if ! { cat "$env_file"; echo "${key}=${value}"; } > "$tmp"; then
        rm -f "$tmp" 2>/dev/null
        trap - EXIT INT TERM
        echo "ERROR: Failed to append ${key} to ${env_file}" >&2
        return 1
      fi
    fi

    # Atomic move
    if ! mv "$tmp" "$env_file"; then
      rm -f "$tmp" 2>/dev/null
      trap - EXIT INT TERM
      echo "ERROR: Failed to move temp file to ${env_file}" >&2
      return 1
    fi
  else
    # Create new file
    umask 077
    if ! echo "${key}=${value}" > "$tmp"; then
      rm -f "$tmp" 2>/dev/null
      trap - EXIT INT TERM
      echo "ERROR: Failed to create ${env_file}" >&2
      return 1
    fi

    if ! mv "$tmp" "$env_file"; then
      rm -f "$tmp" 2>/dev/null
      trap - EXIT INT TERM
      echo "ERROR: Failed to move temp file to ${env_file}" >&2
      return 1
    fi
  fi

  trap - EXIT INT TERM
  # Bug #5 fix: set_secure_file logs its own warning, no need to duplicate
  set_secure_file "$env_file" || true
}

# Confirmation prompt honoring AUTO_YES and TTY; usage: confirm "Message?" || exit 1
confirm() {
  local msg="$1"
  if [[ "${AUTO_YES}" == "1" ]]; then return 0; fi
  if [[ ! -t 0 ]]; then return 1; fi
  read -r -p "${msg} [y/N] " _ans || return 1
  [[ "${_ans}" == "y" || "${_ans}" == "Y" ]]
}

# Return a space-separated list of PIDs listening on a TCP port (best-effort)
find_listening_pids_for_port() {
  local port="$1"
  local pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids=$(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ')
  elif command -v fuser >/dev/null 2>&1; then
    # fuser prints like: 8765/tcp: 1234 2345
    pids=$(fuser -n tcp "${port}" 2>/dev/null | sed -E 's/.*: *//' | tr ' ' '\n' | tr '\n' ' ')
  elif command -v ss >/dev/null 2>&1; then
    # ss -ltnp output includes users:(("python",pid=1234,fd=...))
    pids=$(ss -ltnp 2>/dev/null | awk -v p=":${port}" '$4 ~ p {print $0}' | sed -nE 's/.*pid=([0-9]+).*/\1/p' | tr '\n' ' ')
  fi
  echo "${pids}" | xargs -n1 echo | awk 'NF' | sort -u | tr '\n' ' '
}

# Gracefully kill a list of PIDs owned by current user; escalate to KILL after timeout
kill_pids_graceful() {
  local timeout_s="${1:-5}"; shift || true
  local pids=("$@")
  [[ ${#pids[@]} -eq 0 ]] && return 0
  local me; me=$(id -un)
  local to_kill=()
  local pid
  for pid in "${pids[@]}"; do
    [[ -z "$pid" ]] && continue
    local owner
    owner=$(ps -o user= -p "$pid" 2>/dev/null | awk '{print $1}')
    if [[ "$owner" == "$me" ]]; then
      to_kill+=("$pid")
    else
      log_warn "Skipping PID $pid owned by $owner"
    fi
  done
  [[ ${#to_kill[@]} -eq 0 ]] && return 0
  if [[ "${DRY_RUN}" == "1" ]]; then _print "[dry-run] kill -TERM ${to_kill[*]}"; return 0; fi
  kill -TERM "${to_kill[@]}" 2>/dev/null || true
  local end=$(( $(date +%s) + timeout_s ))
  while :; do
    local alive=()
    for pid in "${to_kill[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then alive+=("$pid"); fi
    done
    [[ ${#alive[@]} -eq 0 ]] && break
    if (( $(date +%s) >= end )); then
      log_warn "Escalating to SIGKILL for: ${alive[*]}"
      kill -KILL "${alive[@]}" 2>/dev/null || true
      break
    fi
    sleep 0.2
  done
}

# Start server in background using run helper; log to logs directory
start_server_background() {
  local helper="scripts/run_server_with_token.sh"
  local stamp
  stamp=$(date +%Y%m%d_%H%M%S)
  mkdir -p logs
  local log_file="logs/server_${stamp}.log"
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] ${helper} > ${log_file} 2>&1 &"
    return 0
  fi
  if [[ -x "$helper" ]]; then
    nohup "$helper" >"$log_file" 2>&1 &
  else
    nohup uv run python -m mcp_agent_mail.cli serve-http >"$log_file" 2>&1 &
  fi
  # Export PID so caller can kill it later
  export _BACKGROUND_SERVER_PID=$!
  _print "Server starting (PID: ${_BACKGROUND_SERVER_PID}, logs: ${log_file})"
}

# Stop background server started by start_server_background
stop_background_server() {
  if [[ -n "${_BACKGROUND_SERVER_PID:-}" ]]; then
    if kill -0 "${_BACKGROUND_SERVER_PID}" 2>/dev/null; then
      _print "Stopping background server (PID: ${_BACKGROUND_SERVER_PID})"
      kill -TERM "${_BACKGROUND_SERVER_PID}" 2>/dev/null || true
      # Wait briefly for graceful shutdown
      local waited=0
      while kill -0 "${_BACKGROUND_SERVER_PID}" 2>/dev/null && [[ $waited -lt 5 ]]; do
        sleep 0.5
        waited=$((waited + 1))
      done
      # Force kill if still running
      if kill -0 "${_BACKGROUND_SERVER_PID}" 2>/dev/null; then
        kill -9 "${_BACKGROUND_SERVER_PID}" 2>/dev/null || true
      fi
    fi
    unset _BACKGROUND_SERVER_PID
  fi
}

# Cross-platform helper to kill processes on a TCP port (works on Linux and macOS)
# Usage: kill_port_processes <port>
# Only kills processes owned by current user for safety
# shellcheck disable=SC2015  # A && B || C pattern is intentional here
kill_port_processes() {
  local port="$1"
  local current_user
  current_user=$(id -un)
  local killed=0

  # Try lsof first (available on macOS and most Linux)
  if command -v lsof >/dev/null 2>&1; then
    local pids
    pids=$(lsof -t -i :"${port}" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
      for pid in $pids; do
        local owner
        owner=$(ps -o user= -p "$pid" 2>/dev/null | awk '{print $1}') || owner=""
        if [[ "$owner" == "$current_user" ]]; then
          # First try SIGTERM for graceful shutdown
          kill -TERM "$pid" 2>/dev/null && killed=1 || true
        elif [[ -n "$owner" ]]; then
          log_warn "Port ${port} in use by PID $pid owned by $owner; skipping"
        fi
        # If owner is empty, process likely already exited
      done
    fi
  # Fall back to fuser (typically Linux)
  elif command -v fuser >/dev/null 2>&1; then
    local pids
    pids=$(fuser -n tcp "${port}" 2>/dev/null | sed -E 's/.*: *//' | tr ' ' '\n' | awk 'NF') || pids=""
    if [[ -n "$pids" ]]; then
      for pid in $pids; do
        local owner
        owner=$(ps -o user= -p "$pid" 2>/dev/null | awk '{print $1}') || owner=""
        if [[ "$owner" == "$current_user" ]]; then
          kill -TERM "$pid" 2>/dev/null && killed=1 || true
        elif [[ -n "$owner" ]]; then
          log_warn "Port ${port} in use by PID $pid owned by $owner; skipping"
        fi
      done
    fi
  fi

  # Give processes time to exit cleanly
  if [[ $killed -eq 1 ]]; then
    sleep 0.5
    # Check if any are still running and force-kill
    if command -v lsof >/dev/null 2>&1; then
      local remaining
      remaining=$(lsof -t -i :"${port}" 2>/dev/null || true)
      if [[ -n "$remaining" ]]; then
        for pid in $remaining; do
          local owner
          owner=$(ps -o user= -p "$pid" 2>/dev/null | awk '{print $1}') || owner=""
          if [[ "$owner" == "$current_user" ]]; then
            kill -9 "$pid" 2>/dev/null || true
          fi
        done
        sleep 0.3
      fi
    fi
  fi
}

# ---------------------------------------------------------------------------
# Explicit identity helpers (#140)
# ---------------------------------------------------------------------------

integration_project_key() {
  local target_dir="$1" remote=""
  if [[ -n "${AGENT_MAIL_PROJECT_KEY:-}" ]]; then
    printf '%s' "${AGENT_MAIL_PROJECT_KEY}"
    return 0
  fi
  # Enter the directory instead of `git -C <path>`: the same probe-dialect
  # hazard as path_is_inside_git above.  With `-C`, a git that cannot chdir
  # (path dialect, permissions, a hostile PATH) silently yields remote="" and
  # the installer dies before printing anything at all.
  remote=$( cd -- "$target_dir" >/dev/null 2>&1 \
    && git remote get-url origin 2>/dev/null ) || remote=""
  if [[ -n "$remote" ]]; then
    printf '%s' "$remote" \
      | sed -E 's#^[a-zA-Z]+://[^/]+/#/#; s#^[^@]+@[^:]+:#/#; s#\.git$##; s#/+$##' \
      | tr -d '\n'
    return 0
  fi
  log_err "Cannot derive a shared Agent Mail project key: '${target_dir}' has no origin remote"
  return 1
}

integration_platform() {
  case "$(uname -s 2>/dev/null)" in
    Darwin) printf 'mac' ;;
    MINGW*|MSYS*|CYGWIN*) printf 'win' ;;
    Linux)
      if [[ -n "${WSL_DISTRO_NAME:-}" ]] || grep -qi microsoft /proc/version 2>/dev/null; then
        printf 'wsl'
      else
        printf 'linux'
      fi ;;
    *) printf 'other' ;;
  esac
}

integration_client() {
  local client="$1"
  case "$client" in
    agy) printf 'agy' ;;
    claude) printf 'claude' ;;
    codex) printf 'codex' ;;
    copilot) printf 'copilot' ;;
    gemini) printf 'gemini' ;;
    factory) printf 'factory' ;;
    cursor) printf 'cursor' ;;
    cline) printf 'cline' ;;
    windsurf) printf 'windsurf' ;;
    opencode) printf 'opencode' ;;
    grok) printf 'grok' ;;
    kimi) printf 'kimi' ;;
    *)
      log_err "Unsupported agent client token: ${client}"
      return 1 ;;
  esac
}

integration_slot() {
  local slot="${1:-1}"
  case "$slot" in
    ''|0|0*|*[!0-9]*)
      log_err "Agent Mail slot must be a positive integer, got '${slot}'"
      return 1 ;;
    *) printf '%s' "$slot" ;;
  esac
}

integration_hostname() {
  local host=""
  if [[ "$(uname -s 2>/dev/null)" == "Darwin" ]]; then
    host=$(scutil --get HostName 2>/dev/null || true)
    [[ -n "$host" ]] || host=$(scutil --get LocalHostName 2>/dev/null || true)
  fi
  [[ -n "$host" ]] || host=$(hostname -s 2>/dev/null || hostname 2>/dev/null || printf 'host')
  host=$(printf '%s' "$host" \
    | LC_ALL=C tr '[:upper:]' '[:lower:]' \
    | LC_ALL=C tr -cd '[:alnum:]._-' \
    | LC_ALL=C sed 's/^[^[:alnum:]]*//; s/[^[:alnum:]]*$//' \
    | cut -c1-48)
  [[ -n "$host" ]] || host="host"
  printf '%s' "$host"
}

integration_agent_name() {
  local client slot host platform
  client=$(integration_client "$1") || return 1
  slot=$(integration_slot "${2:-1}") || return 1
  host=$(integration_hostname)
  platform=$(integration_platform)
  printf '%s-%s-%s-%s' "$client" "$platform" "$host" "$slot"
}

# Return the caller-requested agent name override, if set.
# Agents can export AGENT_NAME=alpha-one before bootstrapping to claim
# a stable identity across relaunches.
requested_agent_name_override() {
  local name="${AGENT_NAME:-}"
  if [[ -z "$name" ]]; then
    echo ""
    return
  fi
  # Validate against the safe-ID pattern (mirrors _THREAD_ID_RE in utils.py)
  if [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
    echo "$name"
  else
    log_warn "AGENT_NAME='$name' does not match safe-ID pattern; ignoring"
    echo ""
  fi
}

# Build the JSON arguments fragment for register_agent with the required durable
# client/OS/host/slot mailbox name.
# Usage: _ARGS=$(build_register_agent_arguments_json "$human_key" "program" "model" "task" "$agent_name")
build_register_agent_arguments_json() {
  local human_key="$1" program="$2" model="$3" task="$4" agent_name="${5:-}"
  if [[ -z "$agent_name" ]]; then
    log_err "register_agent requires a durable client-os-host-slot name"
    return 1
  fi
  local program_esc model_esc task_esc
  program_esc=$(json_escape_string "$program") || return 1
  model_esc=$(json_escape_string "$model") || return 1
  task_esc=$(json_escape_string "$task") || return 1
  local args="\"project_key\":${human_key},\"program\":${program_esc},\"model\":${model_esc},\"task_description\":${task_esc}"
  local name_esc
  name_esc=$(json_escape_string "$agent_name") || return 1
  args="${args},\"name\":${name_esc}"
  echo "$args"
}

# Extract the registered agent name from a JSON-RPC response.
extract_registered_agent_name() {
  local response="$1"
  if [[ -z "$response" ]]; then
    echo ""
    return
  fi
  local name=""
  if command -v jq >/dev/null 2>&1; then
    name=$(echo "$response" | jq -r '.result.content[0].text // empty' 2>/dev/null | jq -r '.name // empty' 2>/dev/null || echo "")
  else
    name=$(echo "$response" | uv run python -c 'import sys,json; r=json.load(sys.stdin); c=r.get("result",{}).get("content",[]); print(json.loads(c[0]["text"])["name"] if c else "")' 2>/dev/null || echo "")
  fi
  echo "$name"
}

# Extract the per-agent registration_token from a register_agent JSON-RPC response.
#
# Hook scripts (e.g. scripts/hooks/check_inbox.sh) invoke fetch_inbox via direct
# curl POSTs that bypass any persistent MCP-session state, so the principal
# bearer token alone is insufficient — the server requires the per-agent
# registration_token in the JSON-RPC arguments. This helper lets installers
# capture the token at registration time and propagate it into the hook env.
extract_registered_registration_token() {
  local response="$1"
  if [[ -z "$response" ]]; then
    echo ""
    return
  fi
  local token=""
  if command -v jq >/dev/null 2>&1; then
    token=$(echo "$response" | jq -r '.result.content[0].text // empty' 2>/dev/null | jq -r '.registration_token // empty' 2>/dev/null || echo "")
  else
    token=$(echo "$response" | uv run python -c 'import sys,json; r=json.load(sys.stdin); c=r.get("result",{}).get("content",[]); print(json.loads(c[0]["text"]).get("registration_token","") if c else "")' 2>/dev/null || echo "")
  fi
  echo "$token"
}

# Write the agent identity to a discoverable file so shell tooling can find it.
persist_agent_identity_file() {
  local root_dir="$1" agent_name="$2" target_dir="${3:-$1}"
  local identity_dir="${target_dir}/.agent-mail"
  mkdir -p "$identity_dir" 2>/dev/null || true
  echo "$agent_name" > "${identity_dir}/identity"
}
