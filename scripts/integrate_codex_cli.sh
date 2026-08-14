#!/usr/bin/env bash
set -euo pipefail

# Source shared helpers
ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
if [[ -f "${ROOT_DIR}/scripts/lib.sh" ]]; then
  # shellcheck disable=SC1090
  . "${ROOT_DIR}/scripts/lib.sh"
else
  echo "FATAL: scripts/lib.sh not found" >&2
  exit 1
fi
init_colors
setup_traps
parse_common_flags "$@"
# Disable xtrace whether it came from --debug or an external `bash -x`; later
# environment assignments carry the principal bearer and must never be logged.
case "$-" in
  *x*)
    set +x
    log_warn "Command tracing is disabled while the integration handles credentials."
    ;;
esac
require_cmd jq
require_cmd curl
require_cmd git

_normalize_codex_user_path() {
  local target="$1"
  case "$target" in
    [a-zA-Z]:\\*|[a-zA-Z]:/*)
      case "$(uname -s 2>/dev/null || printf unknown)" in
        MINGW*|MSYS*|CYGWIN*)
          command -v cygpath >/dev/null 2>&1 || {
            log_err "cygpath is required to normalize the Windows user path: ${target}" >&2
            return 1
          }
          cygpath -u "$target" 2>/dev/null || {
            log_err "Could not normalize the Windows user path: ${target}" >&2
            return 1
          } ;;
        *)
          command -v wslpath >/dev/null 2>&1 || {
            log_err "wslpath is required to normalize the Windows user path: ${target}" >&2
            return 1
          }
          wslpath -u "$target" 2>/dev/null || {
            log_err "Could not normalize the Windows user path: ${target}" >&2
            return 1
          } ;;
      esac ;;
    *) printf '%s\n' "$target" ;;
  esac
}

_CODEX_HOME_INPUT="${CODEX_HOME:-${HOME}/.codex}"
CODEX_DIR="$(_normalize_codex_user_path "$_CODEX_HOME_INPUT")" || exit 1
SHARED_ENV_FILE="$(_normalize_codex_user_path \
  "${AGENT_MAIL_ENV_FILE:-${HOME}/.agent-mail.env}")" || exit 1
AGENT_MAIL_ENV_FILE="$SHARED_ENV_FILE"
HOOKS_DIR="${CODEX_DIR}/hooks/mcp-agent-mail"
HOOK_RUNTIME="${HOOKS_DIR}/agent_mail_hook.sh"
HOOK_WRAPPER="${HOOKS_DIR}/hook_wrapper.sh"
USER_HOOKS="${CODEX_DIR}/hooks.json"
USER_TOML="${CODEX_DIR}/config.toml"

for _user_target in "$CODEX_DIR" "$SHARED_ENV_FILE"; do
  case "$_user_target" in
    /*) ;;
    *)
      log_err "User integration target must be an absolute path: ${_user_target}"
      exit 1 ;;
  esac
done

_validate_managed_file_target() {
  local target="$1"
  if [[ -L "$target" || ( -e "$target" && ! -f "$target" ) ]]; then
    log_err "Managed user file target must be a regular, non-symlink file: ${target}"
    return 1
  fi
}

_validate_managed_directory_target() {
  local target="$1"
  if [[ -L "$target" || ( -e "$target" && ! -d "$target" ) ]]; then
    log_err "Managed user directory target must be a real directory: ${target}"
    return 1
  fi
}

# Refuse ambiguous destinations before reading configuration or constructing
# backups. In particular, write_atomic must never receive a directory or a
# symlink where this installer owns a regular file.
for _managed_dir in "$CODEX_DIR" "${CODEX_DIR}/hooks" "$HOOKS_DIR"; do
  _validate_managed_directory_target "$_managed_dir" || {
    log_err "No user configuration was changed."
    exit 1
  }
done
for _managed_file in \
  "$SHARED_ENV_FILE" \
  "$USER_HOOKS" \
  "$USER_TOML" \
  "$HOOK_RUNTIME" \
  "$HOOK_WRAPPER" \
  "${HOOKS_DIR}/agent_mail_common.sh"; do
  _validate_managed_file_target "$_managed_file" || {
    log_err "No user configuration was changed."
    exit 1
  }
done

log_step "OpenAI Codex CLI Integration (user scope)"
echo
echo "This installs MCP Agent Mail once for the current user:"
echo "  - lifecycle scripts: ${CODEX_DIR}/hooks/mcp-agent-mail"
echo "  - lifecycle config: ${CODEX_DIR}/hooks.json"
echo "  - authenticated MCP server: ${CODEX_DIR}/config.toml"
echo "No repository configuration or server credential is created."
echo
_CODEX_SLOT="$(integration_slot "${AGENT_MAIL_CODEX_SLOT:-1}")"
_AGENT="$(integration_agent_name codex "${_CODEX_SLOT}")"
if ! confirm "Proceed?"; then log_warn "Aborted."; exit 1; fi

_URL="$(resolve_integration_mcp_url)" || {
  log_err "Missing MCP endpoint. Set INTEGRATION_MCP_URL or AGENT_MAIL_URL (for example https://hermes.example/mcp/)."
  exit 1
}
_TOKEN="$(resolve_global_integration_bearer_token)" || {
  log_err "Missing bearer token. Set INTEGRATION_BEARER_TOKEN or HTTP_BEARER_TOKEN."
  exit 1
}
log_ok "Using MCP endpoint: ${_URL}"
log_ok "Codex identity template: ${_AGENT}"

# Git may materialize tracked shell sources with CRLF in a native Windows
# checkout.  The installed copies are executed by Git Bash, so publish a
# canonical LF stream instead of copying checkout bytes verbatim.  Strip only
# the CR that terminates a CRLF line; an embedded carriage return remains data.
_normalized_hook_source() {
  local source="$1" line
  while IFS= read -r line || [[ -n "$line" ]]; do
    printf '%s\n' "${line%$'\r'}"
  done < "$source"
}

# Validate all source and destination inputs before the first write.  This is
# what makes both --dry-run and a refused merge genuinely side-effect free.
for _source in \
  "${ROOT_DIR}/scripts/hooks/codex_notify.sh" \
  "${ROOT_DIR}/scripts/hooks/agent_mail_common.sh"; do
  if [[ ! -f "${_source}" ]]; then
    log_err "Missing required hook script: ${_source}"
    exit 1
  fi
  if ! _normalized_hook_source "${_source}" | bash -n; then
    log_err "Required hook script has invalid shell syntax: ${_source}"
    log_err "No user configuration was changed."
    exit 1
  fi
done
EXISTING_HOOKS='{}'
if [[ -f "$USER_HOOKS" ]]; then
  if ! jq -e '
      def valid_handler:
        type == "object" and
        ((has("command") | not) or (.command | type == "string")) and
        ((has("commandWindows") | not) or (.commandWindows | type == "string")) and
        ((has("command_windows") | not) or (.command_windows | type == "string"));
      def valid_group:
        type == "object" and
        (.hooks | type == "array") and
        all(.hooks[]; valid_handler);
      type == "object" and
      ((has("hooks") | not) or (.hooks | type == "object")) and
      (if has("hooks") then
        all(.hooks[]; type == "array" and all(.[]; valid_group))
      else true end)
    ' "$USER_HOOKS" >/dev/null 2>&1; then
    log_err "Existing ${USER_HOOKS} has an invalid nested hook shape; refusing to overwrite it."
    exit 1
  fi
  EXISTING_HOOKS="$(cat "$USER_HOOKS")"
fi

# The heredoc is parsed inside $().  Parenthesized case labels keep macOS's
# Bash 3.2 parser from consuming their ')' as the command-substitution close.
CODEX_WRAPPER_CONTENT="$(cat <<SH
#!/usr/bin/env bash
export AGENT_MAIL_CODEX_SLOT='${_CODEX_SLOT}'
export AGENT_MAIL_HOOK_CLIENT='codex'
export AGENT_MAIL_HOOK_SLOT='${_CODEX_SLOT}'
export AGENT_MAIL_INTERVAL='120'
case "\${1:-}" in
  (session-end) export AGENT_MAIL_HOOK_TIMEOUT='2' ;;
  (*) export AGENT_MAIL_HOOK_TIMEOUT='6' ;;
esac
_HOOK_DIR="\$(CDPATH= cd "\$(dirname "\$0")" && pwd -P)" || exit 1
exec bash "\${_HOOK_DIR}/agent_mail_hook.sh" "\$@"
SH
)"
if ! bash -n <<<"$CODEX_WRAPPER_CONTENT"; then
  log_err "Generated invalid Codex hook wrapper; no user configuration was changed."
  exit 1
fi

# Resolve the POSIX hook path in the Codex runtime, not in this installer.
# That distinction lets one Windows-hosted profile serve all supported modes:
# native Windows selects commandWindows below, native WSL uses its Linux HOME,
# and Codex Desktop in WSL uses its /mnt/... CODEX_HOME.  The hook's actual
# process environment remains the sole source of the win/wsl identity segment.
_posix_hook_command() {
  printf 'bash "${CODEX_HOME:-${HOME}/.codex}/hooks/mcp-agent-mail/hook_wrapper.sh" %s' "$1"
}

# commandWindows is explicit even when the file is generated on a POSIX host,
# so a synced user config has a canonical Windows runner.  On Windows itself,
# resolve the actual Git for Windows installation and the actual hook path.
_WINDOWS_BASH='C:\Program Files\Git\bin\bash.exe'
_WINDOWS_WRAPPER='%USERPROFILE%\.codex\hooks\mcp-agent-mail\hook_wrapper.sh'
case "$_CODEX_HOME_INPUT" in
  [a-zA-Z]:\\*|[a-zA-Z]:/*)
    _WINDOWS_CODEX_HOME="${_CODEX_HOME_INPUT//\//\\}"
    _WINDOWS_CODEX_HOME="${_WINDOWS_CODEX_HOME%\\}"
    _WINDOWS_WRAPPER="${_WINDOWS_CODEX_HOME}\\hooks\\mcp-agent-mail\\hook_wrapper.sh"
    ;;
esac
# A Windows Codex desktop can share CODEX_HOME with this installer through a
# /mnt/<drive> WSL path.  commandWindows must point back to that exact profile,
# not assume it lives below the WSL user's unrelated HOME.
if [[ "$HOOK_WRAPPER" == /mnt/[a-zA-Z]/* ]]; then
  if ! command -v wslpath >/dev/null 2>&1; then
    log_err "CODEX_HOME is on a Windows drive, but wslpath is unavailable."
    exit 1
  fi
  _WINDOWS_BASH=''
  if [[ -n "${AGENT_MAIL_GIT_BASH_PATH:-}" ]]; then
    case "$AGENT_MAIL_GIT_BASH_PATH" in
      [a-zA-Z]:\\*|[a-zA-Z]:/*)
        _candidate="$(wslpath -u "$AGENT_MAIL_GIT_BASH_PATH" 2>/dev/null)" || {
          log_err "Could not normalize AGENT_MAIL_GIT_BASH_PATH."
          exit 1
        } ;;
      /*) _candidate="$AGENT_MAIL_GIT_BASH_PATH" ;;
      *)
        log_err "AGENT_MAIL_GIT_BASH_PATH must be an absolute Windows or WSL path."
        exit 1 ;;
    esac
    if [[ ! -x "$_candidate" ]]; then
      log_err "AGENT_MAIL_GIT_BASH_PATH is not an executable Git Bash: ${AGENT_MAIL_GIT_BASH_PATH}"
      exit 1
    fi
    _WINDOWS_BASH="$(wslpath -w "$_candidate" 2>/dev/null)" || {
      log_err "Could not translate AGENT_MAIL_GIT_BASH_PATH for Windows."
      exit 1
    }
  else
    for _candidate in "/mnt/c/Program Files/Git/bin/bash.exe" "/mnt/c/Program Files (x86)/Git/bin/bash.exe"; do
      if [[ -x "$_candidate" ]]; then
        _WINDOWS_BASH="$(wslpath -w "$_candidate" 2>/dev/null)" || {
          log_err "Could not translate Git Bash for Windows."
          exit 1
        }
        break
      fi
    done
  fi
  if [[ -z "$_WINDOWS_BASH" ]]; then
    log_err "CODEX_HOME is shared with Windows, but Git for Windows bash.exe was not found."
    log_err "For a per-user, non-C: or custom install, set AGENT_MAIL_GIT_BASH_PATH explicitly."
    exit 1
  fi
  _WINDOWS_WRAPPER="$(wslpath -w "$HOOK_WRAPPER" 2>/dev/null)" || {
    log_err "Could not translate the Codex hook wrapper path for Windows."
    exit 1
  }
fi
case "$(uname -s 2>/dev/null || printf unknown)" in
  MINGW*|MSYS*|CYGWIN*)
    command -v cygpath >/dev/null 2>&1 || {
      log_err "Windows detected but cygpath is unavailable."
      exit 1
    }
    _WINDOWS_BASH=''
    if [[ -n "${AGENT_MAIL_GIT_BASH_PATH:-}" ]]; then
      case "$AGENT_MAIL_GIT_BASH_PATH" in
        [a-zA-Z]:\\*|[a-zA-Z]:/*)
          _candidate="$(cygpath -u "$AGENT_MAIL_GIT_BASH_PATH" 2>/dev/null)" || {
            log_err "Could not normalize AGENT_MAIL_GIT_BASH_PATH."
            exit 1
          }
          # Preserve the explicitly named Windows executable.  A Git Bash
          # cygpath round trip is not identity-preserving here: Git/bin maps to
          # /bin, which maps back to the different Git/usr/bin raw shell.
          _WINDOWS_BASH="${AGENT_MAIL_GIT_BASH_PATH//\\//}" ;;
        /*)
          _candidate="$AGENT_MAIL_GIT_BASH_PATH"
          _WINDOWS_BASH="$(cygpath -w "$_candidate" 2>/dev/null)" || {
            log_err "Could not translate AGENT_MAIL_GIT_BASH_PATH for Windows."
            exit 1
          } ;;
        *)
          log_err "AGENT_MAIL_GIT_BASH_PATH must be an absolute Windows or Git Bash path."
          exit 1 ;;
      esac
      if [[ ! -x "$_candidate" ]]; then
        log_err "AGENT_MAIL_GIT_BASH_PATH is not an executable Git Bash: ${AGENT_MAIL_GIT_BASH_PATH}"
        exit 1
      fi
    else
      # `command -v bash` inside Git Bash normally returns /usr/bin/bash: the
      # raw MSYS shell.  Codex launches commandWindows outside that environment,
      # where the raw shell does not initialise MSYSTEM or Git's runtime PATH.
      # Locate the Git installation from git.exe and deliberately choose its
      # bin/bash.exe launcher.  Requiring both shipped shells identifies the
      # install root and avoids mistaking Git/usr for it.
      _git_bin="$(command -v git 2>/dev/null || true)"
      _git_root=""
      _git_roots=()
      if [[ -n "$_git_bin" ]]; then
        _git_root="$(cygpath -m "$_git_bin" 2>/dev/null || true)"
      fi
      while [[ -n "$_git_root" && "$_git_root" == */* ]]; do
        _git_root="${_git_root%/*}"
        _git_roots+=("$_git_root")
      done
      for _candidate_root in ${_git_roots[@]+"${_git_roots[@]}"}; do
        if [[ -x "${_candidate_root}/bin/bash.exe" && \
              -x "${_candidate_root}/usr/bin/bash.exe" ]]; then
          _candidate="${_candidate_root}/bin/bash.exe"
          _WINDOWS_BASH="$(cygpath -w "$_candidate" 2>/dev/null)" || {
            log_err "Could not translate Git Bash for Windows."
            exit 1
          }
          break
        fi
      done
      if [[ -z "$_WINDOWS_BASH" ]]; then
        for _candidate in \
          "/c/Program Files/Git/bin/bash.exe" \
          "/c/Program Files (x86)/Git/bin/bash.exe"; do
          if [[ -x "$_candidate" ]]; then
            _WINDOWS_BASH="$(cygpath -w "$_candidate" 2>/dev/null)" || {
              log_err "Could not translate Git Bash for Windows."
              exit 1
            }
            break
          fi
        done
      fi
    fi
    if [[ -z "$_WINDOWS_BASH" ]]; then
      log_err "Windows detected but Git for Windows bash.exe was not found."
      log_err "For a per-user or custom install, set AGENT_MAIL_GIT_BASH_PATH explicitly."
      exit 1
    fi
    _WINDOWS_WRAPPER="$(cygpath -w "$HOOK_WRAPPER" 2>/dev/null)" || {
      log_err "Could not translate the Codex hook wrapper path for Windows."
      exit 1
    }
    ;;
esac
_powershell_single_quote() {
  printf '%s' "$1" | sed "s/'/''/g"
}
_windows_hook_command() {
  local event="$1" bash_q wrapper_q
  bash_q="$(_powershell_single_quote "$_WINDOWS_BASH")"
  wrapper_q="$(_powershell_single_quote "$_WINDOWS_WRAPPER")"
  printf "& '%s' '%s' '%s'" "$bash_q" "$wrapper_q" "$event"
}

SESSION_START_GROUP="$(jq -nc \
  --arg command "$(_posix_hook_command session-start)" \
  --arg command_windows "$(_windows_hook_command session-start)" \
  '{matcher:"startup|resume|clear|compact",hooks:[{
    type:"command",command:$command,commandWindows:$command_windows,
    timeout:20,statusMessage:"Connecting Agent Mail",additionalContextLimit:2500
  }]}')"
SUBAGENT_START_GROUP="$(jq -nc \
  --arg command "$(_posix_hook_command subagent-start)" \
  --arg command_windows "$(_windows_hook_command subagent-start)" \
  '{hooks:[{
    type:"command",command:$command,commandWindows:$command_windows,
    timeout:20,statusMessage:"Starting Agent Mail execution",additionalContextLimit:2500
  }]}')"
SUBAGENT_STOP_GROUP="$(jq -nc \
  --arg command "$(_posix_hook_command subagent-stop)" \
  --arg command_windows "$(_windows_hook_command subagent-stop)" \
  '{hooks:[{
    type:"command",command:$command,commandWindows:$command_windows,
    timeout:20,statusMessage:"Closing Agent Mail execution"
  }]}')"
POST_TOOL_HEARTBEAT_GROUP="$(jq -nc \
  --arg command "$(_posix_hook_command heartbeat)" \
  --arg command_windows "$(_windows_hook_command heartbeat)" \
  '{matcher:"*",hooks:[{
    type:"command",command:$command,commandWindows:$command_windows,
    timeout:20,async:true,statusMessage:"Refreshing Agent Mail execution"
  }]}')"
STOP_GROUP="$(jq -nc \
  --arg command "$(_posix_hook_command stop)" \
  --arg command_windows "$(_windows_hook_command stop)" \
  '{hooks:[{
    type:"command",command:$command,commandWindows:$command_windows,
    timeout:20,statusMessage:"Checking Agent Mail inbox"
  }]}')"
SESSION_END_GROUP="$(jq -nc \
  --arg command "$(_posix_hook_command session-end)" \
  --arg command_windows "$(_windows_hook_command session-end)" \
  '{matcher:"other",hooks:[{
    type:"command",command:$command,commandWindows:$command_windows,
    timeout:3,statusMessage:"Closing Agent Mail session"
  }]}')"

# Remove only commands managed by this integration, then append one canonical
# group for each lifecycle event.  Foreign handlers in the same matcher group
# survive, and re-running the installer cannot duplicate the managed set.
MERGED_HOOKS="$(printf '%s' "$EXISTING_HOOKS" | jq \
  --argjson session_start "$SESSION_START_GROUP" \
  --argjson subagent_start "$SUBAGENT_START_GROUP" \
  --argjson subagent_stop "$SUBAGENT_STOP_GROUP" \
  --argjson post_tool_heartbeat "$POST_TOOL_HEARTBEAT_GROUP" \
  --argjson stop "$STOP_GROUP" \
  --argjson session_end "$SESSION_END_GROUP" '
  def agent_mail_handler:
    (((.command? // "") + "\n" + (.commandWindows? // .command_windows? // ""))
      | ascii_downcase | gsub("\\\\"; "/")) as $command
    | ($command | contains("mcp-agent-mail/")) and
      ($command | test("(codex_notify|notify_wrapper|notify_inbox|hook_wrapper|agent_mail_hook)[.]sh"));
  def clean_groups:
    map(
      if (.hooks | type) == "array" then
        .hooks |= map(select(agent_mail_handler | not))
      else . end
    )
    | map(select((.hooks | type) != "array" or (.hooks | length) > 0));
  if type != "object" then error("hooks.json root must be an object") else . end
  | if has("hooks") and (.hooks | type) != "object" then
      error("hooks must be an object")
    else .hooks = (.hooks // {}) end
  | .hooks |= with_entries(
      if (.value | type) == "array" then .value |= clean_groups else . end
  )
  | .hooks.SessionStart = ((.hooks.SessionStart // []) + [$session_start])
  | .hooks.SubagentStart = ((.hooks.SubagentStart // []) + [$subagent_start])
  | .hooks.SubagentStop = ((.hooks.SubagentStop // []) + [$subagent_stop])
  | .hooks.PostToolUse = ((.hooks.PostToolUse // []) + [$post_tool_heartbeat])
  | .hooks.Stop = ((.hooks.Stop // []) + [$stop])
  | .hooks.SessionEnd = ((.hooks.SessionEnd // []) + [$session_end])
  | if has("description") then . else
      .description = "User-level MCP Agent Mail lifecycle hooks"
    end
')" || {
  log_err "Could not merge ${USER_HOOKS}; existing hooks were left unchanged."
  exit 1
}
if ! printf '%s' "$MERGED_HOOKS" | jq -e '
    def valid_handler:
      type == "object" and
      ((has("command") | not) or (.command | type == "string")) and
      ((has("commandWindows") | not) or (.commandWindows | type == "string")) and
      ((has("command_windows") | not) or (.command_windows | type == "string"));
    def valid_group:
      type == "object" and
      (.hooks | type == "array") and
      all(.hooks[]; valid_handler);
    type == "object" and
    (.hooks | type == "object") and
    all(.hooks[]; type == "array" and all(.[]; valid_group))
  ' >/dev/null; then
  log_err "Generated invalid nested hook JSON for ${USER_HOOKS}; no user configuration was changed."
  exit 1
fi

# Codex accepts static HTTP headers in config.toml.  This editor deliberately
# handles only table-form MCP configuration: it recognizes bare, quoted and
# whitespace-separated dotted table headers, while leaving foreign lines in
# their original order. Inline/dotted MCP definitions, escaped key names and
# multiline TOML strings are rejected because a bounded line editor cannot
# disambiguate those shapes without risking unrelated user configuration.
# Foreign dotted assignments and arrays of tables retain their original lines.
if ! _URL_TOML="$(AGENT_MAIL_JQ_VALUE="$_URL" jq -Rn 'env.AGENT_MAIL_JQ_VALUE')"; then
  log_err "Could not encode the MCP endpoint; no user configuration was changed."
  exit 1
fi
if ! _AUTHORIZATION_TOML="$(
  AGENT_MAIL_JQ_VALUE="Bearer $_TOKEN" jq -Rn 'env.AGENT_MAIL_JQ_VALUE'
)"; then
  log_err "Could not encode MCP authorization; no user configuration was changed."
  exit 1
fi

_TOML_INPUT="$USER_TOML"
if [[ ! -f "$_TOML_INPUT" ]]; then
  _TOML_INPUT="/dev/null"
fi
if ! UPDATED_USER_TOML="$(
  AGENT_MAIL_TOML_URL="$_URL_TOML" \
  AGENT_MAIL_TOML_AUTHORIZATION="$_AUTHORIZATION_TOML" \
  awk '
    function trim(value) {
      sub(/^[ \t\r\n]+/, "", value)
      sub(/[ \t\r\n]+$/, "", value)
      return value
    }

    function fail(message) {
      if (failure == "") {
        failure = message
      }
    }

    function is_managed_alias(value) {
      return value == "mcp_agent_mail" || value == "mcp-agent-mail"
    }

    function parse_key_path(value, parts,    length_value, index_value, quote,
                            escaped, component, component_had_escape,
                            character, count) {
      split("", parts)
      split("", key_path_component_escaped)
      key_path_had_escape = 0
      value = trim(value)
      length_value = length(value)
      index_value = 1
      count = 0
      while (index_value <= length_value) {
        while (index_value <= length_value &&
               substr(value, index_value, 1) ~ /[ \t]/) {
          index_value++
        }
        if (index_value > length_value) {
          return 0
        }
        quote = substr(value, index_value, 1)
        component = ""
        component_had_escape = 0
        if (quote == "\"" || quote == "\047") {
          index_value++
          escaped = 0
          while (index_value <= length_value) {
            character = substr(value, index_value, 1)
            if (quote == "\"" && escaped) {
              component = component "\\" character
              escaped = 0
            } else if (quote == "\"" && character == "\\") {
              key_path_had_escape = 1
              component_had_escape = 1
              escaped = 1
            } else if (character == quote) {
              break
            } else {
              component = component character
            }
            index_value++
          }
          if (index_value > length_value || escaped) {
            return 0
          }
          index_value++
        } else {
          while (index_value <= length_value) {
            character = substr(value, index_value, 1)
            if (character == "." || character ~ /[ \t]/) {
              break
            }
            component = component character
            index_value++
          }
          if (component !~ /^[A-Za-z0-9_-]+$/) {
            return 0
          }
        }
        count++
        parts[count] = component
        key_path_component_escaped[count] = component_had_escape
        while (index_value <= length_value &&
               substr(value, index_value, 1) ~ /[ \t]/) {
          index_value++
        }
        if (index_value > length_value) {
          return count
        }
        if (substr(value, index_value, 1) != ".") {
          return 0
        }
        index_value++
      }
      return 0
    }

    function path_id(parts, count,    item_number, identifier) {
      identifier = count
      for (item_number = 1; item_number <= count; item_number++) {
        identifier = identifier ":" length(parts[item_number]) ":" parts[item_number]
      }
      return identifier
    }

    function combined_path_id(table_parts, table_count, key_parts, key_count,
                              prefix_count,    item_number, identifier) {
      identifier = table_count + prefix_count
      for (item_number = 1; item_number <= table_count; item_number++) {
        identifier = identifier ":" length(table_parts[item_number]) ":" table_parts[item_number]
      }
      for (item_number = 1; item_number <= prefix_count; item_number++) {
        identifier = identifier ":" length(key_parts[item_number]) ":" key_parts[item_number]
      }
      return identifier
    }

    function set_current_table(parts, count,    item_number) {
      split("", current_table_parts)
      split("", current_table_component_escaped)
      current_table_count = count
      for (item_number = 1; item_number <= count; item_number++) {
        current_table_parts[item_number] = parts[item_number]
        current_table_component_escaped[item_number] = key_path_component_escaped[item_number]
      }
    }

    function escaped_table_path_is_foreign(parts) {
      return !key_path_component_escaped[1] && parts[1] != "mcp_servers"
    }

    function escaped_assignment_path_is_foreign(parts) {
      if (current_table_count > 0) {
        return !current_table_component_escaped[1] &&
               current_table_parts[1] != "mcp_servers"
      }
      return !key_path_component_escaped[1] && parts[1] != "mcp_servers"
    }

    function find_active_array_parent(parts, count,    prefix_count,
                                      identifier) {
      active_parent_scope = ""
      active_parent_count = 0
      for (prefix_count = count - 1; prefix_count >= 1; prefix_count--) {
        identifier = path_id(parts, prefix_count)
        if (active_array_instance[identifier] != "") {
          active_parent_scope = active_array_instance[identifier]
          active_parent_count = prefix_count
          return prefix_count
        }
      }
      return 0
    }

    function header_path(line, parts,    candidate, array_header, inner, count) {
      candidate = trim(line)
      sub(/\r$/, "", candidate)
      if (candidate ~ /^\[\[[^]]+\]\][ \t]*(#.*)?$/) {
        array_header = 1
      } else if (candidate ~ /^\[[^]]+\][ \t]*(#.*)?$/) {
        array_header = 0
      } else {
        return 0
      }
      inner = candidate
      sub(/^[ \t]*\[\[?/, "", inner)
      if (array_header) {
        sub(/\]\][ \t]*(#.*)?$/, "", inner)
      } else {
        sub(/\][ \t]*(#.*)?$/, "", inner)
      }
      count = parse_key_path(inner, parts)
      if (!count) {
        return 0
      }
      parsed_array_header = array_header
      return count
    }

    function assignment_path(line, parts,    length_line, index_line, quote,
                             escaped, character, equals_at, left) {
      length_line = length(line)
      quote = ""
      escaped = 0
      equals_at = 0
      for (index_line = 1; index_line <= length_line; index_line++) {
        character = substr(line, index_line, 1)
        if (quote != "") {
          if (quote == "\"" && escaped) {
            escaped = 0
          } else if (quote == "\"" && character == "\\") {
            escaped = 1
          } else if (character == quote) {
            quote = ""
          }
        } else if (character == "\"" || character == "\047") {
          quote = character
        } else if (character == "#") {
          break
        } else if (character == "=") {
          equals_at = index_line
          break
        }
      }
      assignment_equals_at = equals_at
      if (!equals_at) {
        return 0
      }
      left = substr(line, 1, equals_at - 1)
      return parse_key_path(left, parts)
    }

    function scan_balance(line,    index_line, character) {
      depth_before[NR] = square_depth + brace_depth
      quote_before[NR] = scan_quote
      for (index_line = 1; index_line <= length(line); index_line++) {
        character = substr(line, index_line, 1)
        if (scan_quote != "") {
          if (scan_quote == "\"" && scan_escaped) {
            scan_escaped = 0
          } else if (scan_quote == "\"" && character == "\\") {
            scan_escaped = 1
          } else if (character == scan_quote) {
            scan_quote = ""
          }
        } else if (substr(line, index_line, 3) == "\"\"\"" ||
                   substr(line, index_line, 3) == "\047\047\047") {
          fail("TOML multiline strings are unsupported")
          break
        } else if (character == "\"" || character == "\047") {
          scan_quote = character
          scan_escaped = 0
        } else if (character == "#") {
          break
        } else if (character == "[") {
          square_depth++
        } else if (character == "]") {
          square_depth--
        } else if (character == "{") {
          brace_depth++
        } else if (character == "}") {
          brace_depth--
        }
        if (square_depth < 0 || brace_depth < 0) {
          fail("unbalanced TOML delimiters")
          return
        }
      }
      depth_after[NR] = square_depth + brace_depth
      quote_after[NR] = scan_quote
    }

    function managed_notify_item(item,    normalized) {
      normalized = tolower(item)
      gsub(/\\/, "/", normalized)
      if (normalized !~ /mcp[-_]agent[-_]mail/) {
        return 0
      }
      if (normalized ~ /(codex_notify|notify_wrapper|notify_inbox|hook_wrapper|agent_mail_hook)[.]sh/) {
        return 1
      }
      return 0
    }

    function parse_notify(line, items,    equals_at, right, index_right,
                          character, quote, escaped, token, count, comment_at,
                          inner) {
      split("", items)
      assignment_path(line, notify_key_parts)
      equals_at = assignment_equals_at
      right = trim(substr(line, equals_at + 1))
      quote = ""
      escaped = 0
      comment_at = 0
      for (index_right = 1; index_right <= length(right); index_right++) {
        character = substr(right, index_right, 1)
        if (quote != "") {
          if (quote == "\"" && escaped) {
            escaped = 0
          } else if (quote == "\"" && character == "\\") {
            escaped = 1
          } else if (character == quote) {
            quote = ""
          }
        } else if (character == "\"" || character == "\047") {
          quote = character
        } else if (character == "#") {
          comment_at = index_right
          break
        }
      }
      if (comment_at) {
        notify_comment = trim(substr(right, comment_at))
        right = trim(substr(right, 1, comment_at - 1))
      } else {
        notify_comment = ""
      }
      if (substr(right, 1, 1) != "[" ||
          substr(right, length(right), 1) != "]") {
        return -1
      }
      inner = substr(right, 2, length(right) - 2)
      quote = ""
      escaped = 0
      token = ""
      count = 0
      for (index_right = 1; index_right <= length(inner); index_right++) {
        character = substr(inner, index_right, 1)
        if (quote != "") {
          token = token character
          if (quote == "\"" && escaped) {
            if (!(character == "\"" || character == "\\" ||
                  character == "b" || character == "t" ||
                  character == "n" || character == "f" ||
                  character == "r")) {
              return -1
            }
            escaped = 0
          } else if (quote == "\"" && character == "\\") {
            escaped = 1
          } else if (character == quote) {
            quote = ""
          }
        } else if (character == "\"" || character == "\047") {
          quote = character
          token = token character
        } else if (character == ",") {
          token = trim(token)
          if (token == "") {
            return -1
          }
          count++
          items[count] = token
          token = ""
        } else {
          token = token character
        }
      }
      if (quote != "" || escaped) {
        return -1
      }
      token = trim(token)
      if (token != "") {
        count++
        items[count] = token
      } else if (count > 0 && trim(inner) ~ /,$/) {
        return -1
      }
      for (index_right = 1; index_right <= count; index_right++) {
        token = items[index_right]
        if (!((substr(token, 1, 1) == "\"" &&
               substr(token, length(token), 1) == "\"") ||
              (substr(token, 1, 1) == "\047" &&
               substr(token, length(token), 1) == "\047"))) {
          return -1
        }
      }
      return count
    }

    function section_kind(parts, count) {
      if (count >= 2 && parts[1] == "mcp_servers" &&
          is_managed_alias(parts[2])) {
        if (count == 2) {
          return "managed-root"
        }
        if (count == 3 && parts[3] == "http_headers") {
          return "managed-headers"
        }
        return "managed-child"
      }
      return "foreign"
    }

    function managed_replacement_key(section, key) {
      if (section == "managed-root") {
        return key == "url" || key == "bearer_token_env_var" ||
               key == "env_http_headers"
      }
      return section == "managed-headers" && tolower(key) == "authorization"
    }

    function validate(    line_number, line, stripped, count, kind,
                         key_count, notify_count, identifier, prefix_count,
                         absolute_identifier, relative_identifier,
                         scope_identifier, registry_identifier,
                         header_semantic_scope, parent_scope) {
      split("", explicit_table_seen)
      split("", array_table_seen)
      split("", table_prefix_seen)
      split("", value_path_seen)
      split("", assignment_seen)
      split("", scope_table_prefix_seen)
      split("", scope_value_path_seen)
      split("", active_array_instance)
      split("", current_table_parts)
      split("", current_table_component_escaped)
      current_table_count = 0
      current_table_is_array = 0
      current_scope = "root"
      current_semantic_scope = "global"
      current_section = "root"
      for (line_number = 1; line_number <= total_lines; line_number++) {
        line = source_lines[line_number]
        stripped = trim(line)
        sub(/\r$/, "", stripped)
        if (depth_before[line_number] == 0 && substr(stripped, 1, 1) == "[") {
          count = header_path(line, header_parts)
          if (!count) {
            fail("unsupported or malformed TOML table header")
            continue
          }
          if (key_path_had_escape && !escaped_table_path_is_foreign(header_parts)) {
            fail("escaped TOML table keys require an unescaped foreign root")
            continue
          }
          identifier = path_id(header_parts, count)
          kind = section_kind(header_parts, count)
          find_active_array_parent(header_parts, count)
          parent_scope = active_parent_scope
          registry_identifier = identifier
          header_semantic_scope = "global"
          if (parent_scope != "") {
            registry_identifier = parent_scope SUBSEP identifier
            header_semantic_scope = parent_scope
          }
          if (parsed_array_header) {
            if (count == 1 && header_parts[1] == "mcp_servers") {
              fail("mcp_servers arrays of tables are unsupported")
              continue
            }
            if (kind ~ /^managed-/) {
              fail("managed MCP arrays of tables are unsupported")
              continue
            }
            if (explicit_table_seen[registry_identifier]) {
              fail("TOML table and array-of-tables paths conflict")
            }
            array_table_seen[registry_identifier]++
            current_table_is_array = 1
            current_scope = "array:" registry_identifier ":" array_table_seen[registry_identifier]
            current_semantic_scope = current_scope
            active_array_instance[identifier] = current_scope
          } else {
            if (explicit_table_seen[registry_identifier]) {
              fail("duplicate TOML table")
            }
            if (array_table_seen[registry_identifier]) {
              fail("TOML table and array-of-tables paths conflict")
            }
            explicit_table_seen[registry_identifier] = 1
            current_table_is_array = (parent_scope != "")
            current_scope = "table:" registry_identifier
            current_semantic_scope = header_semantic_scope
          }
          for (prefix_count = 1; prefix_count <= count; prefix_count++) {
            absolute_identifier = header_semantic_scope SUBSEP path_id(header_parts, prefix_count)
            if (value_path_seen[absolute_identifier]) {
              fail("TOML scalar and table paths conflict")
            }
            table_prefix_seen[absolute_identifier] = 1
          }
          set_current_table(header_parts, count)
          current_section = kind
          if (kind ~ /^managed-/) {
            if (managed_alias != "" && managed_alias != header_parts[2]) {
              fail("multiple managed MCP server aliases")
            }
            managed_alias = header_parts[2]
            if (kind == "managed-root") {
              managed_root_count++
              if (managed_root_count > 1) {
                fail("duplicate managed MCP server table")
              }
            } else if (kind == "managed-headers") {
              managed_headers_count++
              if (managed_headers_count > 1) {
                fail("duplicate managed MCP http_headers table")
              }
            }
          }
          continue
        }
        if (stripped == "" || substr(stripped, 1, 1) == "#" ||
            depth_before[line_number] > 0) {
          continue
        }
        key_count = assignment_path(line, key_parts)
        if (!key_count) {
          fail("unsupported or malformed TOML assignment")
          continue
        }
        if (key_path_had_escape && !escaped_assignment_path_is_foreign(key_parts)) {
          fail("escaped TOML assignment keys require an unescaped foreign root")
          continue
        }
        if (current_section == "root" && key_parts[1] == "mcp_servers") {
          fail("inline or dotted mcp_servers configuration is unsupported")
        }
        if (current_table_count == 1 &&
            current_table_parts[1] == "mcp_servers" &&
            is_managed_alias(key_parts[1])) {
          fail("inline managed MCP server aliases are unsupported")
        }
        if ((current_section == "managed-root" ||
             current_section == "managed-headers") && key_count != 1) {
          fail("dotted assignments in managed MCP tables are unsupported")
        }
        relative_identifier = path_id(key_parts, key_count)
        scope_identifier = current_scope SUBSEP relative_identifier
        if (assignment_seen[scope_identifier]) {
          fail("duplicate TOML assignment key")
        }
        assignment_seen[scope_identifier] = 1
        for (prefix_count = 1; prefix_count < key_count; prefix_count++) {
          identifier = current_scope SUBSEP path_id(key_parts, prefix_count)
          if (scope_value_path_seen[identifier]) {
            fail("TOML scalar and dotted-key paths conflict")
          }
          scope_table_prefix_seen[identifier] = 1
        }
        identifier = current_scope SUBSEP relative_identifier
        if (scope_table_prefix_seen[identifier]) {
          fail("TOML scalar and dotted-key paths conflict")
        }
        scope_value_path_seen[identifier] = 1
        for (prefix_count = 1; prefix_count < key_count; prefix_count++) {
          absolute_identifier = current_semantic_scope SUBSEP combined_path_id(current_table_parts, current_table_count, key_parts, key_count, prefix_count)
          if (value_path_seen[absolute_identifier]) {
            fail("TOML scalar and dotted-key paths conflict")
          }
          table_prefix_seen[absolute_identifier] = 1
        }
        absolute_identifier = current_semantic_scope SUBSEP combined_path_id(current_table_parts, current_table_count, key_parts, key_count, key_count)
        if (value_path_seen[absolute_identifier]) {
          fail("duplicate TOML assignment key")
        }
        if (table_prefix_seen[absolute_identifier]) {
          fail("TOML scalar and table paths conflict")
        }
        value_path_seen[absolute_identifier] = 1
        if (managed_replacement_key(current_section, key_parts[1]) &&
            (depth_after[line_number] != depth_before[line_number] ||
             quote_after[line_number] != quote_before[line_number])) {
          fail("multiline managed MCP values are unsupported")
        }
        if (current_section == "root" && key_count == 1 &&
            key_parts[1] == "notify") {
          notify_count = parse_notify(line, notify_items)
          if (notify_count < 0) {
            fail("notify must be a one-line array of strings")
          }
        }
        if (current_section == "managed-root" && key_count == 1 &&
            key_parts[1] == "http_headers") {
          fail("managed http_headers must use a nested table")
        }
      }
      if (scan_quote != "" || square_depth != 0 || brace_depth != 0) {
        fail("unbalanced TOML value")
      }
      if (managed_alias != "" && managed_root_count == 0) {
        fail("managed MCP child table has no server table")
      }
    }

    function output_notify(line,    count, item_number, combined) {
      count = parse_notify(line, output_notify_items)
      combined = ""
      for (item_number = 1; item_number <= count; item_number++) {
        combined = combined " " output_notify_items[item_number]
        if (managed_notify_item(output_notify_items[item_number])) {
          return
        }
      }
      if (managed_notify_item(combined)) {
        return
      }
      print line
    }

    function render(    line_number, line, stripped, count, kind, key_count,
                        alias_header) {
      current_section = "root"
      for (line_number = 1; line_number <= total_lines; line_number++) {
        line = source_lines[line_number]
        stripped = trim(line)
        sub(/\r$/, "", stripped)
        if (depth_before[line_number] == 0 && substr(stripped, 1, 1) == "[") {
          count = header_path(line, header_parts)
          kind = section_kind(header_parts, count)
          current_section = kind
          print line
          if (kind == "managed-root") {
            print "url = " ENVIRON["AGENT_MAIL_TOML_URL"]
          } else if (kind == "managed-headers") {
            print "Authorization = " ENVIRON["AGENT_MAIL_TOML_AUTHORIZATION"]
          }
          continue
        }
        if (stripped == "" || substr(stripped, 1, 1) == "#" ||
            depth_before[line_number] > 0) {
          print line
          continue
        }
        key_count = assignment_path(line, key_parts)
        if (current_section == "root" && key_count == 1 &&
            key_parts[1] == "notify") {
          output_notify(line)
          continue
        }
        if (current_section == "managed-root" && key_count == 1 &&
            (key_parts[1] == "url" ||
             key_parts[1] == "bearer_token_env_var" ||
             key_parts[1] == "env_http_headers")) {
          continue
        }
        if (current_section == "managed-headers" && key_count == 1 &&
            tolower(key_parts[1]) == "authorization") {
          continue
        }
        print line
      }

      if (managed_alias == "") {
        if (total_lines > 0 && source_lines[total_lines] != "") {
          print ""
        }
        print "[mcp_servers.mcp_agent_mail]"
        print "url = " ENVIRON["AGENT_MAIL_TOML_URL"]
        print ""
        print "[mcp_servers.mcp_agent_mail.http_headers]"
        print "Authorization = " ENVIRON["AGENT_MAIL_TOML_AUTHORIZATION"]
      } else if (managed_headers_count == 0) {
        alias_header = managed_alias
        if (managed_alias == "mcp-agent-mail") {
          alias_header = "\"mcp-agent-mail\""
        }
        if (total_lines > 0 && source_lines[total_lines] != "") {
          print ""
        }
        print "[mcp_servers." alias_header ".http_headers]"
        print "Authorization = " ENVIRON["AGENT_MAIL_TOML_AUTHORIZATION"]
      }
    }

    {
      source_lines[NR] = $0
      total_lines = NR
      scan_balance($0)
    }

    END {
      validate()
      if (failure != "") {
        print "Codex TOML merge refused: " failure > "/dev/stderr"
        exit 1
      }
      if (ENVIRON["AGENT_MAIL_TOML_URL"] == "" ||
          ENVIRON["AGENT_MAIL_TOML_AUTHORIZATION"] == "") {
        print "Codex TOML merge refused: missing managed MCP settings" > "/dev/stderr"
        exit 1
      }
      render()
    }
  ' "$_TOML_INPUT"
)"; then
  log_err "Existing ${USER_TOML} is invalid or could not be merged; no user configuration was changed."
  exit 1
fi

# Validate the shared state path through the same writer used by a real install.
# The temporary DRY_RUN value is dynamically visible to its helper calls, so
# this performs normalization and merge checks without backups or publication.
if ! DRY_RUN=1 write_shared_agent_mail_env "${_URL}" "${_TOKEN}" >/dev/null; then
  log_err "Shared Agent Mail environment is invalid; no user configuration was changed."
  exit 1
fi

# Every prospective output has now been validated in memory.  A dry run stops
# before backups, mkdir, chmod, or any file publication.
if [[ "$DRY_RUN" == "1" ]]; then
  _print "[dry-run] write shared Agent Mail URL/bearer"
  _print "[dry-run] install Codex lifecycle scripts under ${HOOKS_DIR}"
  _print "[dry-run] merge Codex hooks into ${USER_HOOKS}"
  _print "[dry-run] merge Codex MCP into ${USER_TOML}"
  _print "[dry-run] no files or directories were changed"
  exit 0
fi

for _config in "$USER_HOOKS" "$USER_TOML"; do
  backup_user_file "$_config" || exit 1
done

log_step "Writing shared server settings"
write_shared_agent_mail_env "${_URL}" "${_TOKEN}" || {
  log_err "Could not write ${SHARED_ENV_FILE}"
  exit 1
}

log_step "Installing Codex lifecycle scripts"
_normalized_hook_source "${ROOT_DIR}/scripts/hooks/codex_notify.sh" | \
  write_atomic "$HOOK_RUNTIME"
set_secure_exec "$HOOK_RUNTIME" || exit 1
_normalized_hook_source "${ROOT_DIR}/scripts/hooks/agent_mail_common.sh" | \
  write_atomic "${HOOKS_DIR}/agent_mail_common.sh"
set_secure_file "${HOOKS_DIR}/agent_mail_common.sh" || exit 1
write_atomic "$HOOK_WRAPPER" <<<"$CODEX_WRAPPER_CONTENT"
set_secure_exec "$HOOK_WRAPPER" || exit 1

write_atomic "$USER_HOOKS" <<<"$MERGED_HOOKS"
set_secure_file "$USER_HOOKS" || true
write_atomic "$USER_TOML" <<<"$UPDATED_USER_TOML"
set_secure_file "$USER_TOML" || true

log_ok "==> Codex user integration complete."
_print "Codex config: ${USER_TOML}"
_print "Codex hooks: ${USER_HOOKS}"
_print "Lifecycle scripts: ${HOOKS_DIR}"
_print "Identity template: ${_AGENT}; registration is activation-gated and migration-safe."
_print "Open /hooks in Codex and trust the new or changed user hook definitions before use."
