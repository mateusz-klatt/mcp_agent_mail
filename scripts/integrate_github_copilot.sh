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

_COPILOT_ID_CLIENT="copilot"
_normalize_installer_user_path() {
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

COPILOT_DIR="$(_normalize_installer_user_path "${COPILOT_HOME:-${HOME}/.copilot}")" || exit 1
SHARED_ENV_FILE="$(_normalize_installer_user_path \
  "${AGENT_MAIL_ENV_FILE:-${HOME}/.agent-mail.env}")" || exit 1
# Shared helpers resolve both existing URL/bearer values and the eventual write
# through AGENT_MAIL_ENV_FILE.  Keep that single path normalized consistently.
AGENT_MAIL_ENV_FILE="$SHARED_ENV_FILE"

log_step "GitHub Copilot CLI / VS Code Integration (user scope)"
echo
echo "This installs MCP Agent Mail once for the current user:"
echo "  - Copilot CLI MCP: ${COPILOT_DIR}/mcp-config.json"
echo "  - Copilot CLI hooks: ${COPILOT_DIR}/hooks/mcp-agent-mail.json"
echo "  - VS Code MCP: the platform's user mcp.json"
echo "It never creates or modifies repository/workspace client configuration."
echo
_COPILOT_SLOT="$(integration_slot "${AGENT_MAIL_COPILOT_SLOT:-1}")"
_AGENT="$(integration_agent_name "${_COPILOT_ID_CLIENT}" "${_COPILOT_SLOT}")"
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
log_ok "Copilot identity template: ${_AGENT}"

# Resolve every target and validate every existing JSON document before the
# first write.  In particular, malformed user configuration is never replaced
# with an empty object and --dry-run does not even create a parent directory.
COPILOT_MCP_JSON="${COPILOT_DIR}/mcp-config.json"
COPILOT_HOOKS_JSON="${COPILOT_DIR}/hooks/mcp-agent-mail.json"
VSCODE_MCP_JSON="$(integration_vscode_user_mcp_path)" || exit 1
VSCODE_MCP_JSON="$(_normalize_installer_user_path "$VSCODE_MCP_JSON")" || exit 1
HOOKS_DIR="${COPILOT_DIR}/hooks/mcp-agent-mail"
HOOK_RUNTIME="${HOOKS_DIR}/agent_mail_hook.sh"
HOOK_COMMON="${HOOKS_DIR}/agent_mail_common.sh"
HOOK_WRAPPER="${HOOKS_DIR}/hook_wrapper.sh"

for _user_target in "$COPILOT_DIR" "$VSCODE_MCP_JSON" "$SHARED_ENV_FILE"; do
  case "$_user_target" in
    /*) ;;
    *)
      log_err "User integration target must be an absolute path: ${_user_target}"
      exit 1
      ;;
  esac
done

for _required_hook in \
  "${ROOT_DIR}/scripts/hooks/codex_notify.sh" \
  "${ROOT_DIR}/scripts/hooks/agent_mail_common.sh"; do
  if [[ ! -f "$_required_hook" ]]; then
    log_err "Required hook runtime is missing: ${_required_hook}"
    exit 1
  fi
done

_json_object_or_default() {
  local path="$1" default_json="$2"
  if [[ ! -s "$path" ]]; then
    printf '%s' "$default_json"
    return 0
  fi
  if ! jq -e 'type == "object"' "$path" >/dev/null 2>&1; then
    log_err "Existing ${path} is not a valid JSON object; refusing to overwrite it." >&2
    return 1
  fi
  cat "$path"
}

EXISTING_COPILOT_MCP="$(_json_object_or_default "$COPILOT_MCP_JSON" '{}')" || exit 1
EXISTING_VSCODE_MCP="$(_json_object_or_default "$VSCODE_MCP_JSON" '{}')" || exit 1
EXISTING_COPILOT_HOOKS="$(_json_object_or_default \
  "$COPILOT_HOOKS_JSON" '{"version":1,"hooks":{}}')" || exit 1

if ! printf '%s' "$EXISTING_COPILOT_MCP" | jq -e \
    '(.mcpServers? // {} | type) == "object"' >/dev/null; then
  log_err "Existing ${COPILOT_MCP_JSON} has a non-object mcpServers field; refusing to overwrite it."
  exit 1
fi
if ! printf '%s' "$EXISTING_VSCODE_MCP" | jq -e \
    '(.servers? // {} | type) == "object"' >/dev/null; then
  log_err "Existing ${VSCODE_MCP_JSON} has a non-object servers field; refusing to overwrite it."
  exit 1
fi
if ! printf '%s' "$EXISTING_COPILOT_HOOKS" | jq -e '
    ((.version? // 1) == 1) and
    ((.hooks? // {}) | type == "object") and
    ([((.hooks? // {}) | to_entries[]).value | type == "array"] | all)
  ' >/dev/null; then
  log_err "Existing ${COPILOT_HOOKS_JSON} is not a valid version 1 hook object; refusing to overwrite it."
  exit 1
fi

# The principal bearer is supplied to jq through its environment, never as an
# argv value.  Debug tracing was disabled above, and no log line prints it.
MERGED_COPILOT_MCP="$(
  AGENT_MAIL_INSTALL_URL="${_URL}" \
  AGENT_MAIL_INSTALL_AUTHORIZATION="Bearer ${_TOKEN}" \
  jq '
    .mcpServers = (.mcpServers // {}) |
    .mcpServers["mcp-agent-mail"] = {
      type: "http",
      url: env.AGENT_MAIL_INSTALL_URL,
      tools: ["*"],
      headers: {Authorization: env.AGENT_MAIL_INSTALL_AUTHORIZATION}
    }
  ' <<<"$EXISTING_COPILOT_MCP"
)" || {
  log_err "Could not merge ${COPILOT_MCP_JSON}; existing configuration was left unchanged."
  exit 1
}
MERGED_VSCODE_MCP="$(
  AGENT_MAIL_INSTALL_URL="${_URL}" \
  AGENT_MAIL_INSTALL_AUTHORIZATION="Bearer ${_TOKEN}" \
  jq '
    .servers = (.servers // {}) |
    .servers["mcp-agent-mail"] = {
      type: "http",
      url: env.AGENT_MAIL_INSTALL_URL,
      headers: {Authorization: env.AGENT_MAIL_INSTALL_AUTHORIZATION}
    }
  ' <<<"$EXISTING_VSCODE_MCP"
)" || {
  log_err "Could not merge ${VSCODE_MCP_JSON}; existing configuration was left unchanged."
  exit 1
}

printf -v _HOOK_WRAPPER_Q '%q' "$HOOK_WRAPPER"
printf -v _HOOK_RUNTIME_Q '%q' "$HOOK_RUNTIME"
_bash_hook_command() {
  printf 'bash %s %s' "$_HOOK_WRAPPER_Q" "$1"
}

# Copilot selects `powershell` on Windows.  That command invokes one concrete
# Git for Windows bash.exe and passes the wrapper as a separate argument, so
# PowerShell never has to interpret Bash quoting or the bearer token.
_WINDOWS_BASH='C:\Program Files\Git\bin\bash.exe'
_WINDOWS_WRAPPER=''
if [[ "$HOOK_WRAPPER" == /mnt/[a-zA-Z]/* ]]; then
  if ! command -v wslpath >/dev/null 2>&1; then
    log_err "COPILOT_HOME is on a Windows drive, but wslpath is unavailable."
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
    log_err "COPILOT_HOME is shared with Windows, but Git for Windows bash.exe was not found."
    log_err "For a per-user, non-C: or custom install, set AGENT_MAIL_GIT_BASH_PATH explicitly."
    exit 1
  fi
  _WINDOWS_WRAPPER="$(wslpath -w "$HOOK_WRAPPER" 2>/dev/null)" || {
    log_err "Could not translate the Copilot hook wrapper path for Windows."
    exit 1
  }
fi
case "$(uname -s 2>/dev/null || printf unknown)" in
  MINGW*|MSYS*|CYGWIN*)
    _WINDOWS_BASH=''
    if [[ -n "${AGENT_MAIL_GIT_BASH_PATH:-}" ]]; then
      case "$AGENT_MAIL_GIT_BASH_PATH" in
        [a-zA-Z]:\\*|[a-zA-Z]:/*)
          _candidate="$(cygpath -u "$AGENT_MAIL_GIT_BASH_PATH" 2>/dev/null)" || {
            log_err "Could not normalize AGENT_MAIL_GIT_BASH_PATH."
            exit 1
          } ;;
        /*) _candidate="$AGENT_MAIL_GIT_BASH_PATH" ;;
        *)
          log_err "AGENT_MAIL_GIT_BASH_PATH must be an absolute Windows or Git Bash path."
          exit 1 ;;
      esac
      if [[ ! -x "$_candidate" ]]; then
        log_err "AGENT_MAIL_GIT_BASH_PATH is not an executable Git Bash: ${AGENT_MAIL_GIT_BASH_PATH}"
        exit 1
      fi
      _WINDOWS_BASH="$(cygpath -w "$_candidate" 2>/dev/null)" || {
        log_err "Could not translate AGENT_MAIL_GIT_BASH_PATH for Windows."
        exit 1
      }
    else
      _current_bash="$(command -v bash 2>/dev/null || true)"
      for _candidate in "$_current_bash" "/c/Program Files/Git/bin/bash.exe" "/c/Program Files (x86)/Git/bin/bash.exe"; do
        if [[ -n "$_candidate" && -x "$_candidate" ]]; then
          _WINDOWS_BASH="$(cygpath -w "$_candidate" 2>/dev/null)" || {
            log_err "Could not translate Git Bash for Windows."
            exit 1
          }
          break
        fi
      done
    fi
    if [[ -z "$_WINDOWS_BASH" ]]; then
      log_err "Windows detected but Git for Windows bash.exe was not found."
      log_err "For a per-user or custom install, set AGENT_MAIL_GIT_BASH_PATH explicitly."
      exit 1
    fi
    _WINDOWS_WRAPPER="$(cygpath -w "$HOOK_WRAPPER" 2>/dev/null)" || {
      log_err "Could not translate the Copilot hook wrapper path for Windows."
      exit 1
    }
    ;;
esac

_powershell_single_quote() {
  printf '%s' "$1" | sed "s/'/''/g"
}
_powershell_hook_command() {
  local event="$1" bash_q wrapper_q
  bash_q="$(_powershell_single_quote "$_WINDOWS_BASH")"
  if [[ -n "$_WINDOWS_WRAPPER" ]]; then
    wrapper_q="$(_powershell_single_quote "$_WINDOWS_WRAPPER")"
    printf "& '%s' '%s' '%s'" "$bash_q" "$wrapper_q" "$event"
  else
    printf "\$amHome = if (\$env:COPILOT_HOME) { \$env:COPILOT_HOME } else { Join-Path \$env:USERPROFILE '.copilot' }; & '%s' (Join-Path \$amHome 'hooks\\mcp-agent-mail\\hook_wrapper.sh') '%s'" \
      "$bash_q" "$event"
  fi
}

MERGED_COPILOT_HOOKS="$(printf '%s' "$EXISTING_COPILOT_HOOKS" | jq \
  --arg start_bash "$(_bash_hook_command session-start)" \
  --arg start_powershell "$(_powershell_hook_command session-start)" \
  --arg stop_bash "$(_bash_hook_command stop)" \
  --arg stop_powershell "$(_powershell_hook_command stop)" \
  --arg end_bash "$(_bash_hook_command session-end)" \
  --arg end_powershell "$(_powershell_hook_command session-end)" '
  def managed_agent_mail_hook:
    (((.bash? // "") + "\n" + (.powershell? // "") + "\n" + (.command? // ""))
      | ascii_downcase | gsub("\\\\"; "/")) as $command |
    ($command | contains("mcp-agent-mail/")) and
    ($command | contains("hook_wrapper.sh"));
  def without_managed:
    map(select(managed_agent_mail_hook | not));
  .version = 1 |
  .hooks = (.hooks // {}) |
  .hooks |= with_entries(.value |= without_managed) |
  .hooks.SessionStart = ((.hooks.SessionStart // []) + [{
    type:"command", bash:$start_bash, powershell:$start_powershell,
    timeoutSec:20
  }]) |
  .hooks.Stop = ((.hooks.Stop // []) + [{
    type:"command", bash:$stop_bash, powershell:$stop_powershell,
    timeoutSec:20
  }]) |
  .hooks.SessionEnd = ((.hooks.SessionEnd // []) + [{
    type:"command", bash:$end_bash, powershell:$end_powershell,
    timeoutSec:3
  }])
')" || {
  log_err "Could not merge ${COPILOT_HOOKS_JSON}; existing configuration was left unchanged."
  exit 1
}

if [[ "$DRY_RUN" == "1" ]]; then
  _print "[dry-run] write shared Agent Mail URL/bearer to ${SHARED_ENV_FILE}"
  _print "[dry-run] install Copilot CLI runtime under ${HOOKS_DIR}"
  _print "[dry-run] merge Copilot CLI MCP into ${COPILOT_MCP_JSON}"
  _print "[dry-run] merge Copilot CLI hooks into ${COPILOT_HOOKS_JSON}"
  _print "[dry-run] merge VS Code MCP into ${VSCODE_MCP_JSON}"
  _print "[dry-run] no files or directories were changed"
  exit 0
fi

write_shared_agent_mail_env "${_URL}" "${_TOKEN}" || exit 1

log_step "Installing Copilot CLI lifecycle runtime"
write_atomic "$HOOK_RUNTIME" < "${ROOT_DIR}/scripts/hooks/codex_notify.sh"
write_atomic "$HOOK_COMMON" < "${ROOT_DIR}/scripts/hooks/agent_mail_common.sh"
write_atomic "$HOOK_WRAPPER" <<SH
#!/usr/bin/env bash
export AGENT_MAIL_HOOK_CLIENT='${_COPILOT_ID_CLIENT}'
export AGENT_MAIL_HOOK_SLOT='${_COPILOT_SLOT}'
export AGENT_MAIL_INTERVAL='120'
case "\${1:-}" in
  session-end) export AGENT_MAIL_HOOK_TIMEOUT='2' ;;
  *) export AGENT_MAIL_HOOK_TIMEOUT='6' ;;
esac
_AM_HOOK_BASH="\$(command -p -v bash)" || exit 1
exec "\$_AM_HOOK_BASH" ${_HOOK_RUNTIME_Q} "\$@"
SH
set_secure_exec "$HOOK_RUNTIME" || exit 1
set_secure_file "$HOOK_COMMON" || exit 1
set_secure_exec "$HOOK_WRAPPER" || exit 1

log_step "Writing Copilot CLI and VS Code user configuration"
for _config in "$COPILOT_MCP_JSON" "$COPILOT_HOOKS_JSON" "$VSCODE_MCP_JSON"; do
  backup_user_file "$_config" || exit 1
done
printf '%s\n' "$MERGED_COPILOT_MCP" | write_atomic "$COPILOT_MCP_JSON"
printf '%s\n' "$MERGED_COPILOT_HOOKS" | write_atomic "$COPILOT_HOOKS_JSON"
printf '%s\n' "$MERGED_VSCODE_MCP" | write_atomic "$VSCODE_MCP_JSON"
for _config in "$COPILOT_MCP_JSON" "$COPILOT_HOOKS_JSON" "$VSCODE_MCP_JSON"; do
  json_validate "$_config" || exit 1
  set_secure_file "$_config" || true
done

echo
log_ok "==> GitHub Copilot CLI / VS Code user integration complete."
_print "Copilot CLI MCP config: ${COPILOT_MCP_JSON}"
_print "Copilot CLI hook config: ${COPILOT_HOOKS_JSON}"
_print "VS Code user MCP config: ${VSCODE_MCP_JSON}"
_print "Authenticated server: ${_URL}"
_print "Identity requested by Copilot CLI: ${_AGENT}"
_print "Restart Copilot CLI: user hook files are loaded only when the CLI starts."
_print "Documentation: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-hooks"
