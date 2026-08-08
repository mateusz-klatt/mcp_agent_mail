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
# The resolved bearer is forwarded to each child integration. Bash xtrace would
# expose it, and installers must never print the full credential even when
# --debug or the legacy --show-token flag is requested.
case "$-" in
  *x*)
    set +x
    log_warn "Command tracing is disabled while the integration handles credentials."
    ;;
esac
require_cmd jq

log_step "MCP Agent Mail: install user-level client integrations"
echo
echo "This detects Claude Code, Codex CLI and GitHub Copilot CLI, then configures"
echo "their user profiles plus VS Code's user MCP file. It never writes client"
echo "configuration to a repository."
echo
if ! confirm "Proceed?"; then log_warn "Aborted."; exit 1; fi

HAS_CLAUDE=0
command -v claude >/dev/null 2>&1 && HAS_CLAUDE=1
[[ -d "${HOME}/.claude" ]] && HAS_CLAUDE=1
HAS_CODEX=0
command -v codex >/dev/null 2>&1 && HAS_CODEX=1
[[ -d "${CODEX_HOME:-${HOME}/.codex}" ]] && HAS_CODEX=1
HAS_COPILOT=0
command -v copilot >/dev/null 2>&1 && HAS_COPILOT=1
[[ -d "${COPILOT_HOME:-${HOME}/.copilot}" ]] && HAS_COPILOT=1
_print "Found: claude=${HAS_CLAUDE} codex=${HAS_CODEX} copilot=${HAS_COPILOT}; Copilot CLI and VS Code user config are safe to preinstall"

# Every installed lifecycle runtime uses curl and git.  Copilot CLI/VS Code is
# intentionally preinstalled even before either client is detected, so these
# checks are unconditional and happen before any child can write configuration.
require_cmd curl
require_cmd git
if [[ $HAS_CODEX -eq 1 ]]; then
  require_cmd uv
fi

INTEGRATION_MCP_URL="$(resolve_integration_mcp_url)" || {
  log_err "Missing MCP endpoint. Set INTEGRATION_MCP_URL or AGENT_MAIL_URL."
  exit 1
}
INTEGRATION_BEARER_TOKEN="$(resolve_global_integration_bearer_token)" || {
  log_err "Missing bearer token. Set INTEGRATION_BEARER_TOKEN or HTTP_BEARER_TOKEN."
  exit 1
}
export INTEGRATION_MCP_URL INTEGRATION_BEARER_TOKEN

log_step "Applying detected user-level integrations"

INTEGRATION_FAILURES=()
if [[ $HAS_CLAUDE -eq 1 ]]; then
  echo "-- Integrating Claude Code..."
  if ! bash "${ROOT_DIR}/scripts/integrate_claude_code.sh" --yes "$@"; then
    log_warn "Claude Code integration failed; continuing to the remaining clients."
    INTEGRATION_FAILURES+=("Claude Code")
  fi
fi

if [[ $HAS_CODEX -eq 1 ]]; then
  echo "-- Integrating Codex CLI..."
  if ! bash "${ROOT_DIR}/scripts/integrate_codex_cli.sh" --yes "$@"; then
    log_warn "Codex CLI integration failed; continuing to the remaining clients."
    INTEGRATION_FAILURES+=("Codex CLI")
  fi
fi

# Copilot CLI and VS Code use independent user-level server lists.  Preinstalling
# both is harmless before either client is installed and avoids workspace files.
echo "-- Integrating GitHub Copilot CLI and VS Code in user scope..."
if ! bash "${ROOT_DIR}/scripts/integrate_github_copilot.sh" --yes "$@"; then
  log_warn "GitHub Copilot integration failed."
  INTEGRATION_FAILURES+=("GitHub Copilot")
fi

echo
log_step "Summary"
_print "Bearer token: configured (value hidden)"
[[ "${SHOW_TOKEN}" == "1" ]] && log_warn "--show-token is ignored by user integration installers."
_print "MCP endpoint: ${INTEGRATION_MCP_URL}"
_print "Client configuration writes were limited to the user profile; the repository was not modified."
if [[ ${#INTEGRATION_FAILURES[@]} -gt 0 ]]; then
  log_err "Failed integrations: ${INTEGRATION_FAILURES[*]}"
  exit 1
fi
log_ok "All detected client integrations completed successfully."
