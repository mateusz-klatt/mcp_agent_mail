#!/usr/bin/env bash
#
# Start the Iris HTTP server locally, with a bearer token resolved for you.
#
# This is the from-a-checkout path. If you only want a running server, the
# published image needs none of this:
#
#   docker run -d --name iris -p 8765:8765 -v iris-data:/data \
#     -e MAIL_UI_SESSION_SECRET="$(openssl rand -hex 32)" klattm/iris
#
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# Read one key out of .env. Deliberately small: the value is trimmed and
# unwrapped from a single layer of matching quotes, which covers how the
# installers write this file. Anything more exotic belongs in the environment.
read_env_key() {
  local key="$1" value
  [ -f .env ] || return 0

  value="$(sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*(.*)\$/\2/p" .env | tail -1)"
  [ -n "$value" ] || return 0

  value="${value%"${value##*[![:space:]]}"}"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  printf '%s' "$value"
}

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to run from a checkout: https://docs.astral.sh/uv/" >&2
  echo "Or run the published image instead: docker run -p 8765:8765 klattm/iris" >&2
  exit 1
fi

generated_token=false
if [ -z "${HTTP_BEARER_TOKEN:-}" ]; then
  HTTP_BEARER_TOKEN="$(read_env_key HTTP_BEARER_TOKEN)"
fi
if [ -z "${HTTP_BEARER_TOKEN:-}" ]; then
  HTTP_BEARER_TOKEN="$(uv run python -c 'import secrets; print(secrets.token_hex(32))')"
  generated_token=true
fi
export HTTP_BEARER_TOKEN

if [ -z "${MAIL_UI_SESSION_SECRET:-}" ]; then
  MAIL_UI_SESSION_SECRET="$(read_env_key MAIL_UI_SESSION_SECRET)"
  [ -n "$MAIL_UI_SESSION_SECRET" ] && export MAIL_UI_SESSION_SECRET
fi

if [ "$generated_token" = true ]; then
  echo "HTTP_BEARER_TOKEN (generated for this run): $HTTP_BEARER_TOKEN"
  echo "Set it in .env to keep the same token across restarts."
fi

# The server starts happily without a session secret, but /mail then answers
# "Mail UI authentication is unconfigured" on every request -- an easy trap,
# because nothing fails until you open the browser.
if [ -z "${MAIL_UI_SESSION_SECRET:-}" ]; then
  echo
  echo "warning: MAIL_UI_SESSION_SECRET is not set, so /mail will refuse to serve." >&2
  echo "         Generate one and put it in .env to use the web interface:" >&2
  echo "           echo \"MAIL_UI_SESSION_SECRET=\$(openssl rand -hex 32)\" >> .env" >&2
  echo "         MCP traffic on the bearer token is unaffected." >&2
  echo
fi

exec uv run python -m mcp_agent_mail.cli serve-http "$@"
