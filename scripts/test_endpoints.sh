#!/usr/bin/env bash
set -euo pipefail
case "$-" in *x*) set +x ;; esac

# Scripted HTTP canary for one already-provisioned durable Agent. The shared
# hook transport keeps both the bearer and JSON body out of process argv.
# Required private inputs:
#   AGENT_MAIL_PROJECT, AGENT_MAIL_AGENT, AGENT_MAIL_REGISTRATION_TOKEN
# Optional: AGENT_MAIL_URL / HTTP_BEARER_TOKEN / AGENT_MAIL_STATE_DIR.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/hooks/agent_mail_common.sh
source "${SCRIPT_DIR}/hooks/agent_mail_common.sh"

PROJECT="${AGENT_MAIL_PROJECT:-}"
AGENT="${AGENT_MAIL_AGENT:-}"
REGISTRATION_TOKEN="${AGENT_MAIL_REGISTRATION_TOKEN:-}"
if [[ -z "$PROJECT" || -z "$AGENT" || -z "$REGISTRATION_TOKEN" ]]; then
  printf '%s\n' \
    'Set AGENT_MAIL_PROJECT, AGENT_MAIL_AGENT, and AGENT_MAIL_REGISTRATION_TOKEN' \
    'for an already-provisioned durable Agent before running this canary.' >&2
  exit 2
fi
command -v jq >/dev/null 2>&1 || {
  printf '%s\n' 'jq is required.' >&2
  exit 2
}

EXECUTION_ID=""
EXECUTION_TOKEN="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
EXTERNAL_ID="endpoint-canary-$(date +%s)-$$"

end_execution() {
  [[ -n "$EXECUTION_ID" ]] || return 0
  local args
  args="$({
    AGENT_MAIL_JQ_REGISTRATION_TOKEN="$REGISTRATION_TOKEN" \
    AGENT_MAIL_JQ_EXECUTION_TOKEN="$EXECUTION_TOKEN" \
      jq -nc \
        --arg project "$PROJECT" \
        --arg agent "$AGENT" \
        --arg execution_id "$EXECUTION_ID" \
        '{project_key:$project,agent_name:$agent,execution_id:$execution_id,execution_token:env.AGENT_MAIL_JQ_EXECUTION_TOKEN,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,lifecycle_protocol_version:1,status:"completed"}'
  })" || return 0
  am_call end_agent_execution "$args" >/dev/null 2>&1 || true
  EXECUTION_ID=""
}
trap end_execution EXIT INT TERM

echo "[1/4] Health check"
am_call health_check '{}' | jq '{status, environment}'

echo "[2/4] Ensure durable project identity"
ensure_args="$(jq -nc --arg project "$PROJECT" '{human_key:$project}')"
am_call ensure_project "$ensure_args" | jq '{id, slug, human_key, project_uid}'

echo "[3/4] Authenticate durable Agent"
whois_args="$({
  AGENT_MAIL_JQ_REGISTRATION_TOKEN="$REGISTRATION_TOKEN" \
    jq -nc --arg project "$PROJECT" --arg agent "$AGENT" \
      '{project_key:$project,agent_name:$agent,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN}'
})"
am_call whois "$whois_args" | jq '{id, name, program, model, retired_at}'

echo "[4/4] Start and end AgentExecution protocol v1"
start_args="$({
  AGENT_MAIL_JQ_REGISTRATION_TOKEN="$REGISTRATION_TOKEN" \
  AGENT_MAIL_JQ_EXECUTION_TOKEN="$EXECUTION_TOKEN" \
    jq -nc \
      --arg project "$PROJECT" \
      --arg agent "$AGENT" \
      --arg external_id "$EXTERNAL_ID" \
      '{project_key:$project,agent_name:$agent,external_id:$external_id,client_name:"endpoint-canary",execution_token:env.AGENT_MAIL_JQ_EXECUTION_TOKEN,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,lifecycle_protocol_version:1,kind:"session",task_description:"HTTP endpoint lifecycle canary"}'
})"
start_result="$(am_call start_agent_execution "$start_args")"
EXECUTION_ID="$(printf '%s' "$start_result" | jq -er '.id')"
printf '%s' "$start_result" | jq '{id, kind, status, lifecycle_protocol_version}'
end_execution
trap - EXIT INT TERM
