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
require_cmd uv
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

CODEX_DIR="$(_normalize_codex_user_path "${CODEX_HOME:-${HOME}/.codex}")" || exit 1
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

# Validate all source and destination inputs before the first write.  This is
# what makes both --dry-run and a refused merge genuinely side-effect free.
for _source in \
  "${ROOT_DIR}/scripts/hooks/codex_notify.sh" \
  "${ROOT_DIR}/scripts/hooks/agent_mail_common.sh"; do
  if [[ ! -f "${_source}" ]]; then
    log_err "Missing required hook script: ${_source}"
    exit 1
  fi
  if ! bash -n "${_source}"; then
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

printf -v _HOOK_RUNTIME_Q '%q' "$HOOK_RUNTIME"
CODEX_WRAPPER_CONTENT="$(cat <<SH
#!/usr/bin/env bash
export AGENT_MAIL_CODEX_SLOT='${_CODEX_SLOT}'
export AGENT_MAIL_HOOK_CLIENT='codex'
export AGENT_MAIL_HOOK_SLOT='${_CODEX_SLOT}'
export AGENT_MAIL_INTERVAL='120'
case "\${1:-}" in
  session-end) export AGENT_MAIL_HOOK_TIMEOUT='2' ;;
  *) export AGENT_MAIL_HOOK_TIMEOUT='6' ;;
esac
exec bash ${_HOOK_RUNTIME_Q} "\$@"
SH
)"
if ! bash -n <<<"$CODEX_WRAPPER_CONTENT"; then
  log_err "Generated invalid Codex hook wrapper; no user configuration was changed."
  exit 1
fi

printf -v _HOOK_WRAPPER_Q '%q' "$HOOK_WRAPPER"
_posix_hook_command() {
  printf 'bash %s %s' "$_HOOK_WRAPPER_Q" "$1"
}

# commandWindows is explicit even when the file is generated on a POSIX host,
# so a synced user config has a canonical Windows runner.  On Windows itself,
# resolve the actual Git for Windows installation and the actual hook path.
_WINDOWS_BASH='C:\Program Files\Git\bin\bash.exe'
_WINDOWS_WRAPPER='%USERPROFILE%\.codex\hooks\mcp-agent-mail\hook_wrapper.sh'
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
      log_err "Could not translate the Codex hook wrapper path for Windows."
      exit 1
    }
    ;;
esac
_windows_hook_command() {
  printf '"%s" "%s" %s' "$_WINDOWS_BASH" "$_WINDOWS_WRAPPER" "$1"
}

SESSION_START_GROUP="$(jq -nc \
  --arg command "$(_posix_hook_command session-start)" \
  --arg command_windows "$(_windows_hook_command session-start)" \
  '{matcher:"startup|resume|clear|compact",hooks:[{
    type:"command",command:$command,commandWindows:$command_windows,
    timeout:20,statusMessage:"Connecting Agent Mail",additionalContextLimit:2500
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

# Parse and re-emit TOML semantically rather than using table-header regexes.
# This accepts valid spaced/quoted/dotted headers, inline mcp_servers tables and
# nested http_headers tables.  Foreign values survive the round trip; only the
# managed URL, Authorization header, legacy auth keys and managed notify entry
# are changed.  uv is pinned to this repository and an isolated Python 3.14, so
# a caller's pyproject, environment and lockfile are never discovered or synced.
_UV_PYTHON=(
  uv run --directory "$ROOT_DIR" --project "$ROOT_DIR"
  --isolated --no-cache --no-sync --no-env-file --python 3.14 python
)
if ! UPDATED_USER_TOML="$(
  AGENT_MAIL_INSTALL_URL="$_URL" \
  AGENT_MAIL_INSTALL_AUTHORIZATION="Bearer $_TOKEN" \
  "${_UV_PYTHON[@]}" - "$USER_TOML" 2>/dev/null <<'PY'
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

path = Path(sys.argv[1])
url = os.environ.get("AGENT_MAIL_INSTALL_URL", "")
authorization = os.environ.get("AGENT_MAIL_INSTALL_AUTHORIZATION", "")
if not url or not authorization:
    raise SystemExit("missing managed MCP settings")

try:
    text = path.read_text(encoding="utf-8")
except FileNotFoundError:
    text = ""
data = tomllib.loads(text)
if not isinstance(data, dict):
    raise SystemExit("TOML root must be a table")

managed_hook_names = (
    "codex_notify.sh",
    "notify_wrapper.sh",
    "notify_inbox.sh",
    "hook_wrapper.sh",
    "agent_mail_hook.sh",
)


def managed_notify(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.lower().replace("\\", "/")
    return "mcp-agent-mail/" in normalized and any(
        name in normalized for name in managed_hook_names
    )


notify = data.get("notify")
if isinstance(notify, list):
    kept_notify = [value for value in notify if not managed_notify(value)]
    if kept_notify:
        data["notify"] = kept_notify
    else:
        data.pop("notify", None)
elif managed_notify(notify):
    data.pop("notify", None)

servers = data.get("mcp_servers")
if servers is None:
    servers = {}
elif not isinstance(servers, dict):
    raise SystemExit("mcp_servers must be a table")
else:
    servers = dict(servers)

managed_keys = [
    key for key in ("mcp_agent_mail", "mcp-agent-mail") if key in servers
]
if len(managed_keys) > 1:
    raise SystemExit("multiple managed MCP server aliases")
managed_key = managed_keys[0] if managed_keys else "mcp_agent_mail"
managed = servers.get(managed_key, {})
if not isinstance(managed, dict):
    raise SystemExit("managed MCP server must be a table")
managed = dict(managed)

headers = managed.get("http_headers", {})
if not isinstance(headers, dict):
    raise SystemExit("managed http_headers must be a table")
headers = dict(headers)
headers["Authorization"] = authorization
managed["url"] = url
managed["http_headers"] = headers
managed.pop("bearer_token_env_var", None)
managed.pop("env_http_headers", None)
servers[managed_key] = managed
data["mcp_servers"] = servers

bare_key_re = re.compile(r"^[A-Za-z0-9_-]+$")


def format_key(key: str) -> str:
    if bare_key_re.fullmatch(key):
        return key
    return json.dumps(key, ensure_ascii=False)


def format_path(parts: tuple[str, ...]) -> str:
    return ".".join(format_key(part) for part in parts)


def is_array_of_tables(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, dict) for item in value)
    )


def format_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "-inf" if value < 0 else "inf"
        return repr(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(format_value(item) for item in value) + "]"
    if isinstance(value, dict):
        entries = ", ".join(
            f"{format_key(str(key))} = {format_value(item)}"
            for key, item in value.items()
        )
        return "{ " + entries + " }"
    raise TypeError(f"unsupported TOML value type: {type(value).__name__}")


output: list[str] = []


def blank_line() -> None:
    if output and output[-1] != "":
        output.append("")


def split_items(table: dict[str, Any]) -> tuple[
    list[tuple[str, Any]],
    list[tuple[str, dict[str, Any]]],
    list[tuple[str, list[dict[str, Any]]]],
]:
    scalar_items: list[tuple[str, Any]] = []
    child_tables: list[tuple[str, dict[str, Any]]] = []
    array_tables: list[tuple[str, list[dict[str, Any]]]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            child_tables.append((key, value))
        elif is_array_of_tables(value):
            array_tables.append((key, value))
        else:
            scalar_items.append((key, value))
    return scalar_items, child_tables, array_tables


def emit_table(table: dict[str, Any], path_parts: tuple[str, ...]) -> None:
    scalar_items, child_tables, array_tables = split_items(table)
    blank_line()
    output.append(f"[{format_path(path_parts)}]")
    output.extend(
        f"{format_key(key)} = {format_value(value)}"
        for key, value in scalar_items
    )
    for key, child in child_tables:
        emit_table(child, (*path_parts, key))
    for key, items in array_tables:
        emit_array_table(items, (*path_parts, key))


def emit_array_table(
    items: list[dict[str, Any]], path_parts: tuple[str, ...]
) -> None:
    for item in items:
        scalar_items, child_tables, array_tables = split_items(item)
        blank_line()
        output.append(f"[[{format_path(path_parts)}]]")
        output.extend(
            f"{format_key(key)} = {format_value(value)}"
            for key, value in scalar_items
        )
        for key, child in child_tables:
            emit_table(child, (*path_parts, key))
        for key, nested_items in array_tables:
            emit_array_table(nested_items, (*path_parts, key))


root_scalars, root_tables, root_array_tables = split_items(data)
output.extend(
    f"{format_key(key)} = {format_value(value)}"
    for key, value in root_scalars
)
for key, table in root_tables:
    emit_table(table, (key,))
for key, items in root_array_tables:
    emit_array_table(items, (key,))

updated = "\n".join(output).rstrip() + "\n"
round_trip = tomllib.loads(updated)
round_trip_server = round_trip["mcp_servers"][managed_key]
if round_trip_server["url"] != url:
    raise SystemExit("managed URL did not round-trip")
if round_trip_server["http_headers"]["Authorization"] != authorization:
    raise SystemExit("managed Authorization did not round-trip")
sys.stdout.write(updated)
PY
)"; then
  log_err "Existing ${USER_TOML} is invalid or could not be merged; no user configuration was changed."
  exit 1
fi

# Every prospective output has now been parsed or syntax-checked in memory.
# A dry run stops before backups, mkdir, chmod, or any file publication.
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
write_atomic "$HOOK_RUNTIME" < "${ROOT_DIR}/scripts/hooks/codex_notify.sh"
set_secure_exec "$HOOK_RUNTIME" || exit 1
write_atomic "${HOOKS_DIR}/agent_mail_common.sh" \
  < "${ROOT_DIR}/scripts/hooks/agent_mail_common.sh"
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
