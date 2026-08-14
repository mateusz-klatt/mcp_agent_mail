#!/usr/bin/env bash
set -euo pipefail

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
case "$-" in *x*) set +x; log_warn "Command tracing disabled while handling credentials." ;; esac
require_cmd jq
require_cmd curl
require_cmd git

TARGET_DIR="${PROJECT_DIR:-${ROOT_DIR}}"
if [[ ! -d "$TARGET_DIR" ]]; then
  log_err "Project directory does not exist: ${TARGET_DIR}"
  exit 1
fi
TARGET_DIR=$(cd "$TARGET_DIR" && pwd -P)
PROJECT_KEY="$(integration_project_key "$TARGET_DIR")" || exit 1
USER_CONFIG="${HOME}/.gemini/settings.json"
SHARED_ENV_FILE="${AGENT_MAIL_ENV_FILE:-${HOME}/.agent-mail.env}"
AGENT_MAIL_ENV_FILE="$SHARED_ENV_FILE"

log_step "Gemini CLI Agent Mail integration (user scope)"
_print "Gemini config: ${USER_CONFIG}"
_print "Project identity: ${PROJECT_KEY}"
_print "No file will be created in ${TARGET_DIR}."
if ! confirm "Proceed?"; then log_warn "Aborted."; exit 1; fi

_URL="$(resolve_integration_mcp_url)" || {
  log_err "Set INTEGRATION_MCP_URL or AGENT_MAIL_URL to the MCP endpoint."
  exit 1
}
_TOKEN="$(resolve_global_integration_bearer_token)" || {
  log_err "Set INTEGRATION_BEARER_TOKEN or HTTP_BEARER_TOKEN in the user environment."
  exit 1
}
AGENT_MAIL_URL="$_URL"
HTTP_BEARER_TOKEN="$_TOKEN"

# Validate the private credential destination before changing any user file.
# shellcheck disable=SC1090
. "${ROOT_DIR}/scripts/hooks/agent_mail_common.sh"
if [[ "${AM_PATH_CONFIGURATION_VALID:-0}" != "1" ]]; then
  log_err "Agent Mail credential state must be an absolute user path outside Git."
  exit 1
fi

if [[ -L "$SHARED_ENV_FILE" || ( -e "$SHARED_ENV_FILE" && ! -f "$SHARED_ENV_FILE" ) ]]; then
  log_err "Shared Agent Mail environment target must be a regular, non-symlink file: ${SHARED_ENV_FILE}"
  exit 1
fi
_ENV_PROBE=$(dirname "$SHARED_ENV_FILE")
while [[ ! -e "$_ENV_PROBE" ]]; do
  _NEXT=$(dirname "$_ENV_PROBE")
  [[ "$_NEXT" != "$_ENV_PROBE" ]] || break
  _ENV_PROBE="$_NEXT"
done
if [[ -L "$_ENV_PROBE" ]] \
    || [[ "$(git -C "$_ENV_PROBE" rev-parse --is-inside-work-tree 2>/dev/null || true)" == "true" ]] \
    || [[ "$(git -C "$_ENV_PROBE" rev-parse --is-inside-git-dir 2>/dev/null || true)" == "true" ]]; then
  log_err "Shared Agent Mail environment must live outside every Git worktree: ${SHARED_ENV_FILE}"
  exit 1
fi

if [[ -L "$USER_CONFIG" || ( -e "$USER_CONFIG" && ! -f "$USER_CONFIG" ) ]]; then
  log_err "Gemini user settings target must be a regular, non-symlink file: ${USER_CONFIG}"
  exit 1
fi
_PROBE=$(dirname "$USER_CONFIG")
while [[ ! -e "$_PROBE" ]]; do
  _NEXT=$(dirname "$_PROBE")
  [[ "$_NEXT" != "$_PROBE" ]] || break
  _PROBE="$_NEXT"
done
if [[ -L "$_PROBE" ]] \
    || [[ "$(git -C "$_PROBE" rev-parse --is-inside-work-tree 2>/dev/null || true)" == "true" ]] \
    || [[ "$(git -C "$_PROBE" rev-parse --is-inside-git-dir 2>/dev/null || true)" == "true" ]]; then
  log_err "Gemini user settings must live outside every Git worktree: ${USER_CONFIG}"
  exit 1
fi

_EXISTING='{}'
if [[ -f "$USER_CONFIG" ]]; then
  if ! jq -e 'type == "object" and ((.mcpServers // {}) | type == "object")' \
      "$USER_CONFIG" >/dev/null 2>&1; then
    log_err "Existing Gemini settings are not a mergeable JSON object: ${USER_CONFIG}"
    exit 1
  fi
  _EXISTING=$(<"$USER_CONFIG")
fi
_MERGED=$(AGENT_MAIL_JQ_AUTHORIZATION="Bearer ${_TOKEN}" jq \
  --arg url "$_URL" '
    .mcpServers = (.mcpServers // {}) |
    .mcpServers["mcp-agent-mail"] = {
      "httpUrl": $url,
      "headers": {"Authorization": env.AGENT_MAIL_JQ_AUTHORIZATION}
    }
  ' <<<"$_EXISTING") || {
  log_err "Could not merge Gemini user settings."
  exit 1
}

if [[ "$DRY_RUN" == "1" ]]; then
  DRY_RUN=1 write_shared_agent_mail_env "$_URL" "$_TOKEN" >/dev/null
  _print "[dry-run] merge ${USER_CONFIG}"
  _print "[dry-run] bootstrap ${PROJECT_KEY} without exposing credentials"
  exit 0
fi

write_shared_agent_mail_env "$_URL" "$_TOKEN" || exit 1
backup_user_file "$USER_CONFIG" || exit 1
write_atomic "$USER_CONFIG" <<<"$_MERGED"
set_secure_file "$USER_CONFIG" || exit 1

_SLOT="$(integration_slot "${AGENT_MAIL_GEMINI_SLOT:-1}")" || exit 1
_AGENT="$(integration_agent_name gemini "$_SLOT")" || exit 1
_REG_TOKEN="$(am_cred_get "$PROJECT_KEY" "$_AGENT")"
_ENSURE_ARGS=$(jq -nc --arg key "$PROJECT_KEY" '{human_key:$key}')
if ! am_call ensure_project "$_ENSURE_ARGS" >/dev/null 2>&1; then
  log_warn "Gemini config installed; server bootstrap is deferred because the endpoint is unavailable."
  exit 0
fi
if [[ -n "$_REG_TOKEN" ]]; then
  _REGISTER_ARGS=$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$_REG_TOKEN" jq -nc \
    --arg key "$PROJECT_KEY" --arg name "$_AGENT" \
    '{project_key:$key,program:"gemini-cli",model:"gemini",name:$name,
      task_description:"user integration",registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN}')
else
  _REGISTER_ARGS=$(jq -nc --arg key "$PROJECT_KEY" --arg name "$_AGENT" \
    '{project_key:$key,program:"gemini-cli",model:"gemini",name:$name,
      task_description:"user integration"}')
fi
_REGISTER_DATA="$(am_call register_agent "$_REGISTER_ARGS")" || {
  log_warn "Gemini config installed; durable-agent bootstrap will retry on the next run."
  exit 0
}
if ! jq -e 'type == "object"' <<<"$_REGISTER_DATA" >/dev/null 2>&1; then
  log_err "register_agent returned an unreadable response; no credential state was changed."
  exit 1
fi
_REGISTERED_AGENT=$(jq -r '.name // empty' <<<"$_REGISTER_DATA")
_RETURNED_TOKEN=$(jq -r '.registration_token // empty' <<<"$_REGISTER_DATA")
if [[ "$_REGISTERED_AGENT" != "$_AGENT" ]]; then
  log_err "Server did not confirm the requested durable Gemini identity."
  exit 1
fi
if [[ -n "$_REG_TOKEN" && -n "$_RETURNED_TOKEN" && "$_REG_TOKEN" != "$_RETURNED_TOKEN" ]]; then
  log_err "Server returned a different credential for the existing Gemini identity."
  exit 1
fi
if [[ -z "$_REG_TOKEN" ]]; then
  if [[ -z "$_RETURNED_TOKEN" ]] || ! am_cred_put "$PROJECT_KEY" "$_AGENT" "$_RETURNED_TOKEN"; then
    log_err "Gemini identity was registered but its credential could not be persisted safely."
    exit 1
  fi
fi

log_ok "Gemini CLI user integration complete."
_print "Durable Agent: ${_AGENT}"
_print "Registration credential: stored privately in ${AM_CRED_FILE}"
