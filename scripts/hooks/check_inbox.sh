#!/usr/bin/env bash
# Fast inbox check hook for Claude Code / Codex-cli
#
# Features:
# - Rate limited (checks at most once per INTERVAL seconds)
# - Silent when no mail (saves tokens)
# - Uses curl directly (avoids Python import overhead)
# - Supports both plain-text (default) and Claude-Code JSON envelope output
#
# Usage in .claude/settings.json:
#   "PostToolUse": [
#     { "matcher": "Bash", "hooks": [{ "type": "command", "command": "/path/to/check_inbox.sh" }] }
#   ]
#
# Environment variables:
#   AGENT_MAIL_PROJECT             - Project key (absolute path)
#   AGENT_MAIL_AGENT               - Agent name
#   AGENT_MAIL_URL                 - Server URL (default: http://127.0.0.1:8765/api/)
#
# The principal bearer is read from ~/.agent-mail.env (mode 0600), not from the
# hook command. A hook command is stored in settings.json, which the installer
# chmods to 644 and which lives inside the repository — so a token placed there
# is handed to every account on the machine and sits one .gitignore edit away
# from a public remote.
#
#   AGENT_MAIL_TOKEN               - Principal bearer token (HTTP Authorization header).
#                                    HTTP_BEARER_TOKEN is accepted under the same meaning,
#                                    because that is the name the server and every other
#                                    hook already use for this value.
#   AGENT_MAIL_REGISTRATION_TOKEN  - Optional explicit per-agent registration
#                                    credential. Normally it is loaded from the
#                                    private shared credential store.
#   AGENT_MAIL_INTERVAL            - Minimum seconds between checks (default: 120)
#   AGENT_MAIL_HOOK_FORMAT         - Output format: "text" (default) or "json".
#                                    "json" emits a Claude-Code hookSpecificOutput envelope
#                                    so the inbox reminder is injected into the agent's
#                                    reasoning context as a system reminder. Plain stdout
#                                    from a PostToolUse hook is shown to the human in the
#                                    terminal but does NOT reach the agent — only this
#                                    envelope does. Set to "json" for Claude Code.

# Don't use set -e because grep returns 1 when no match
set -uo pipefail
case "$-" in *x*) set +x ;; esac

# Use the same private environment parser, credential store and curl transport
# as the lifecycle hooks. In particular, neither credential crosses process
# argv: the bearer is supplied through a mode-0600 curl config and the JSON-RPC
# body (including registration_token) through stdin.
ROOT_DIR=$(cd "$(dirname "$0")/../.." && pwd)
# shellcheck disable=SC1090
. "${ROOT_DIR}/scripts/hooks/agent_mail_common.sh"
[[ "${AM_PATH_CONFIGURATION_VALID:-0}" == "1" ]] || exit 0

# Configuration with defaults
PROJECT="${AGENT_MAIL_PROJECT:-}"
AGENT="${AGENT_MAIL_AGENT:-}"
INTERVAL="${AGENT_MAIL_INTERVAL:-120}"
HOOK_FORMAT="${AGENT_MAIL_HOOK_FORMAT:-text}"

# Require project and agent
if [[ -z "${PROJECT}" || -z "${AGENT}" ]]; then
  # Silent exit if not configured - don't spam errors
  exit 0
fi

# Detect placeholder values (indicates unconfigured settings)
# Must match patterns used by install scripts and server-side validation
if [[ "${PROJECT}" == *"YOUR_"* || "${PROJECT}" == *"PLACEHOLDER"* || "${PROJECT}" == "<"*">" ]]; then
  # Silent exit - configuration not complete
  exit 0
fi
if [[ "${AGENT}" == *"YOUR_"* || "${AGENT}" == *"PLACEHOLDER"* || "${AGENT}" == "<"*">" ]]; then
  exit 0
fi

# Rate limiting using temp file
RATE_FILE="/tmp/mcp-mail-check-${AGENT//[^a-zA-Z0-9]/_}"
NOW=$(date +%s)

if [[ -f "${RATE_FILE}" ]]; then
  LAST_CHECK=$(cat "${RATE_FILE}" 2>/dev/null || echo 0)
  ELAPSED=$((NOW - LAST_CHECK))
  if [[ ${ELAPSED} -lt ${INTERVAL} ]]; then
    # Too soon, skip check
    exit 0
  fi
fi

# Update last check time
echo "${NOW}" > "${RATE_FILE}"

# Build fetch_inbox arguments without putting the registration credential in
# jq argv. An explicit environment credential remains supported for callers
# that pre-provision it; otherwise resume the durable identity from the private
# credential store shared by the installers and lifecycle hooks.
REG_TOKEN="${AGENT_MAIL_REGISTRATION_TOKEN:-$(am_cred_get "$PROJECT" "$AGENT")}"
if [[ -n "${REG_TOKEN}" ]]; then
  ARGS_JSON=$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$REG_TOKEN" jq -nc \
    --arg project "$PROJECT" --arg agent "$AGENT" \
    '{project_key:$project,agent_name:$agent,
      registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,
      limit:10,include_bodies:false,unread_only:true}') || exit 0
else
  ARGS_JSON=$(jq -nc --arg project "$PROJECT" --arg agent "$AGENT" \
    '{project_key:$project,agent_name:$agent,
      limit:10,include_bodies:false,unread_only:true}') || exit 0
fi

# Fetch inbox via the stateless MCP endpoint. am_call unwraps a successful tool
# response to the returned message array and stays fail-open for this legacy
# reminder hook.
RESPONSE=$(am_call fetch_inbox "$ARGS_JSON" 2>/dev/null || true)

# Check if we got a valid response with messages
if [[ -z "${RESPONSE}" ]]; then
  exit 0
fi

# Count total + urgent messages by parsing JSON. Robust against single-line
# responses (where `grep -c '"subject"'` returned 1 regardless of message
# count) and against importance values that share substrings.
#
# Tolerates both modern (`result.structuredContent.result`) and legacy
# (`result.content[0].text` JSON-encoded string) tool-result shapes.
COUNTS=$(printf '%s' "${RESPONSE}" | jq -r '
  (if type == "array" then .
   elif type == "object" then
     (.result.structuredContent.result //
      ((.result.content[0].text // "[]") | try fromjson catch []))
   else [] end) |
  if type != "array" then "0 0"
  else "\(length) \(map(select(
    type == "object" and (.importance == "urgent" or .importance == "high")
  )) | length)"
  end
' 2>/dev/null || echo "0 0")

MSG_COUNT="${COUNTS%% *}"
URGENT_COUNT="${COUNTS##* }"
MSG_COUNT="${MSG_COUNT:-0}"
URGENT_COUNT="${URGENT_COUNT:-0}"

if [[ "${MSG_COUNT}" -gt 0 ]]; then
  if [[ ${URGENT_COUNT} -gt 0 ]]; then
    MSG_TEXT="You have ${MSG_COUNT} message(s) in your inbox (${URGENT_COUNT} urgent/high priority). Use fetch_inbox to check your messages."
  else
    MSG_TEXT="You have ${MSG_COUNT} recent message(s) in your inbox. Consider checking with fetch_inbox if you haven't lately."
  fi

  if [[ "${HOOK_FORMAT}" == "json" ]]; then
    # Claude-Code hookSpecificOutput envelope. additionalContext is the only
    # PostToolUse channel that surfaces in the agent's next-turn system
    # reminder; plain stdout is shown only in the human's terminal.
    printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":%s}}\n' \
      "$(jq -Rn --arg text "${MSG_TEXT}" '$text')"
  else
    echo ""
    echo "📬 === INBOX REMINDER ==="
    if [[ ${URGENT_COUNT} -gt 0 ]]; then
      echo "⚠️  ${MSG_TEXT}"
    else
      echo "   ${MSG_TEXT}"
    fi
    echo "========================="
    echo ""
  fi
fi

exit 0
