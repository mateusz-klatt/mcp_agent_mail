#!/usr/bin/env bash
# User-level Codex/Copilot CLI lifecycle hook for MCP Agent Mail.
#
# The client sends one JSON object on stdin.  The installed wrapper supplies
# one explicit event argument (session-start, stop, or session-end), a closed
# client token and a slot.  No project, agent name or registration token is
# placed in shared configuration.

# Do not use set -e: a quiet grep/jq query is an ordinary hook outcome.
set -uo pipefail

EVENT="${1:-}"
case "$EVENT" in
  session-start|stop|session-end) ;;
  *) exit 0 ;;
esac

# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || {
  [[ "$EVENT" == "stop" ]] && printf '{}\n'
  exit 0
}

am_read_payload
HOOK_CWD="$(am_payload_field '.cwd')"
if [[ -n "$HOOK_CWD" && -d "$HOOK_CWD" ]]; then
  cd "$HOOK_CWD" 2>/dev/null || {
    [[ "$EVENT" == "stop" ]] && printf '{}\n'
    exit 0
  }
fi

HOOK_CLIENT="${AGENT_MAIL_HOOK_CLIENT:-codex}"
HOOK_SLOT="${AGENT_MAIL_HOOK_SLOT:-${AGENT_MAIL_CODEX_SLOT:-1}}"
case "$HOOK_CLIENT" in
  codex)
    HOOK_SURFACE="codex"
    HOOK_PROGRAM="codex-cli"
    ;;
  copilot)
    HOOK_SURFACE="copilot"
    HOOK_PROGRAM="github-copilot-cli"
    ;;
  *) exit 0 ;;
esac

hook_empty_stop_output() {
  [[ "$EVENT" == "stop" ]] && printf '{}\n'
}

hook_nonblocking_message() {
  if [[ "$HOOK_SURFACE" == "codex" ]]; then
    jq -nc --arg message "$1" '{systemMessage:$message}'
  else
    # Copilot's Stop output accepts only allow/block decisions.  Do not turn
    # ordinary unread mail into a forced continuation; SessionStart reports
    # the count and only newly observed high/urgent mail blocks below.
    printf '{}\n'
  fi
}

hook_start_context() {
  if [[ "$HOOK_SURFACE" == "copilot" ]]; then
    jq -nc --arg context "$1" '{additionalContext:$context}'
  else
    am_emit_context "SessionStart" "$1"
  fi
}

hook_model_id() {
  local model
  model="$(am_payload_field '(.model | strings)')"
  model="$(printf '%s' "$model" | tr -cd '[:alnum:]._-' | cut -c1-128)"
  [[ -n "$model" ]] || model="unknown"
  printf '%s' "$model"
}

# Set HOOK_AGENT, HOOK_TOKEN and HOOK_ERROR.  SessionStart refreshes the
# existing registration once at the lifecycle boundary; Stop registers only
# when no credential exists.  Consequently ordinary turns never touch
# last_active merely because the Stop hook fired.
hook_ensure_agent() {
  local refresh="$1" ensure_response ensure_rc args response response_rc
  local granted_agent granted_token model retired

  HOOK_ERROR=""
  HOOK_AGENT="$(am_agent_name "$HOOK_CLIENT" "$HOOK_SLOT")" || {
    HOOK_ERROR="could not derive the ${HOOK_SURFACE} identity"
    return 1
  }
  HOOK_TOKEN="$(am_cred_get "$PROJECT" "$HOOK_AGENT")"
  if [[ -n "$HOOK_TOKEN" && "$refresh" != "1" ]]; then
    return 0
  fi

  if [[ -z "$HOOK_TOKEN" ]]; then
    ensure_response="$(am_call ensure_project "$(jq -nc --arg k "$PROJECT" '{human_key:$k}')")"
    ensure_rc=$?
    if [[ $ensure_rc -ne 0 ]]; then
      HOOK_ERROR="$(am_failure_reason "$ensure_rc" "$ensure_response")"
      return 1
    fi
  fi

  model="$(hook_model_id)"
  if [[ -n "$HOOK_TOKEN" ]]; then
    args="$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$HOOK_TOKEN" \
      jq -nc --arg p "$PROJECT" --arg n "$HOOK_AGENT" \
      --arg m "$model" --arg program "$HOOK_PROGRAM" \
      '{project_key:$p,name:$n,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,program:$program,model:$m}')"
  else
    args="$(jq -nc --arg p "$PROJECT" --arg n "$HOOK_AGENT" --arg m "$model" \
      --arg program "$HOOK_PROGRAM" \
      '{project_key:$p,name:$n,program:$program,model:$m}')"
  fi

  response="$(am_call register_agent "$args")"
  response_rc=$?
  if [[ $response_rc -ne 0 ]]; then
    HOOK_ERROR="$(am_failure_reason "$response_rc" "$response")"
    return 1
  fi

  granted_agent="$(printf '%s' "$response" | jq -r '.name // empty' 2>/dev/null)"
  granted_token="$(printf '%s' "$response" | jq -r '.registration_token // empty' 2>/dev/null)"
  if [[ -z "$granted_agent" || ( -z "$granted_token" && -z "$HOOK_TOKEN" ) ]]; then
    HOOK_ERROR="the registration response did not contain a usable name and token"
    return 1
  fi

  [[ -n "$granted_token" ]] || granted_token="$HOOK_TOKEN"
  HOOK_AGENT="$granted_agent"
  HOOK_TOKEN="$granted_token"
  # Crash-safe order: remember the server-granted name first.  If credential
  # persistence then fails, the next start resolves this exact name and fails
  # authentication instead of deriving and registering another identity.
  if ! am_granted_name_put "$PROJECT" "$HOOK_AGENT" "$HOOK_CLIENT" "$HOOK_SLOT"; then
    HOOK_ERROR="could not persist the server-granted agent name"
    return 1
  fi
  if ! am_cred_put "$PROJECT" "$HOOK_AGENT" "$HOOK_TOKEN"; then
    HOOK_ERROR="could not persist the registration credential"
    return 1
  fi

  retired="$(printf '%s' "$response" | jq -r '.retired_at // empty' 2>/dev/null)"
  if [[ "$refresh" == "1" && -n "$retired" ]]; then
    am_call unretire_agent "$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$HOOK_TOKEN" \
      jq -nc --arg p "$PROJECT" --arg n "$HOOK_AGENT" \
      '{project_key:$p,agent_name:$n,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN}')" \
      >/dev/null 2>&1
  fi
  return 0
}

# Set HOOK_INBOX to the current unread message array.
hook_fetch_inbox() {
  local args response response_rc
  args="$(AGENT_MAIL_JQ_REGISTRATION_TOKEN="$HOOK_TOKEN" \
    jq -nc --arg p "$PROJECT" --arg n "$HOOK_AGENT" \
    '{project_key:$p,agent_name:$n,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,limit:20,include_bodies:false,unread_only:true}')"
  response="$(am_call fetch_inbox "$args")"
  response_rc=$?
  [[ $response_rc -eq 0 ]] || {
    HOOK_ERROR="$(am_failure_reason "$response_rc" "$response")"
    return 1
  }
  printf '%s' "$response" | jq -e 'type == "array"' >/dev/null 2>&1 || {
    HOOK_ERROR="the inbox response was not a message array"
    return 1
  }
  HOOK_INBOX="$response"
  return 0
}

# These clients do not install an autoreserve hook, so SessionEnd must not release
# every reservation held by the shared client slot.  If a session-scoped log
# exists (for example after a future explicit integration), release only its
# recorded paths.  With today's Codex hook set this is a local no-op.
hook_release_session_paths() {
  local log slug project paths agent token response response_rc migration
  AM_SESSION_ID="$(am_payload_field '.session_id')"
  [[ -n "$AM_SESSION_ID" ]] || return 0
  while IFS= read -r log; do
    [[ -r "$log" ]] || continue
    slug="${log##*__}"
    slug="${slug%.list}"
    [[ -n "$slug" ]] || continue
    paths="$(sort -u "$log" 2>/dev/null | jq -Rsc 'split("\n") | map(select(length > 0))' 2>/dev/null)"
    [[ -n "$paths" && "$paths" != "[]" ]] || continue
    project="$(jq -r --arg s "$slug" \
      'keys[] | select((. | gsub("[^A-Za-z0-9._-]"; "")) == $s)' \
      "$AM_CRED_FILE" 2>/dev/null | head -n 1)"
    [[ -n "$project" ]] || continue
    export AM_PROJECT_FOR_NAME="$project"
    if migration="$(am_identity_migration_pair "$project" "$HOOK_CLIENT" "$HOOK_SLOT")"; then
      continue
    fi
    agent="$(am_agent_name "$HOOK_CLIENT" "$HOOK_SLOT")" || continue
    token="$(am_cred_get "$project" "$agent")"
    [[ -n "$token" ]] || continue
    response="$(am_call release_file_reservations "$(
      AGENT_MAIL_JQ_REGISTRATION_TOKEN="$token" \
      jq -nc --arg p "$project" --arg a "$agent" --argjson paths "$paths" \
      '{project_key:$p,agent_name:$a,registration_token:env.AGENT_MAIL_JQ_REGISTRATION_TOKEN,paths:$paths}'
    )")"
    response_rc=$?
    if [[ $response_rc -eq 0 ]]; then
      # Retain the state file itself; an empty log is enough to prevent a
      # repeated release and avoids deleting user state from a lifecycle hook.
      printf '' > "$log" 2>/dev/null || true
    fi
  done <<EOF
$(am_session_logs)
EOF
}

if [[ "$EVENT" == "session-end" ]]; then
  hook_release_session_paths
  exit 0
fi

PROJECT="$(am_project_key)"
if [[ -z "$PROJECT" ]]; then
  hook_empty_stop_output
  exit 0
fi
export AM_PROJECT_FOR_NAME="$PROJECT"

# hooks.json is global, so merely opening a repository is not permission to
# create an Agent Mail project or identity.  Keep this local gate before the
# migration check, rate files, and every network call.
if ! am_project_is_active "$PROJECT" "$HOOK_CLIENT" "$HOOK_SLOT" .; then
  if [[ "$EVENT" == "session-start" ]]; then
    hook_start_context "$(am_project_activation_message "$PROJECT")"
  else
    hook_empty_stop_output
  fi
  exit 0
fi

# This check is deliberately before the rate file and before every network
# call.  Agent.id owns the existing history; blindly registering the new name
# would fork that history into a second row.
if MIGRATION_PAIR="$(am_identity_migration_pair "$PROJECT" "$HOOK_CLIENT" "$HOOK_SLOT")"; then
  LEGACY_AGENT="${MIGRATION_PAIR%%$'\t'*}"
  CLIENT_AGENT="${MIGRATION_PAIR#*$'\t'}"
  MIGRATION_MESSAGE="$(am_identity_migration_message "$LEGACY_AGENT" "$CLIENT_AGENT")"
  if [[ "$EVENT" == "session-start" ]]; then
    hook_start_context "$MIGRATION_MESSAGE"
  else
    hook_nonblocking_message "$MIGRATION_MESSAGE"
  fi
  exit 0
fi

if [[ "$EVENT" == "session-start" ]]; then
  if ! hook_ensure_agent 1; then
    hook_start_context \
      "Agent Mail: could not register ${PROJECT} — ${HOOK_ERROR}. No inbox or reservation coordination is active for this ${HOOK_SURFACE} session."
    exit 0
  fi
  if hook_fetch_inbox; then
    COUNTS="$(printf '%s' "$HOOK_INBOX" | jq -r \
      '[length, (map(select(.importance == "urgent" or .importance == "high")) | length)] | @tsv')"
    MESSAGE_COUNT="${COUNTS%%$'\t'*}"
    URGENT_COUNT="${COUNTS#*$'\t'}"
    START_CONTEXT="Agent Mail: you are ${HOOK_AGENT} on ${PROJECT}. Unread inbox: ${MESSAGE_COUNT} message(s), ${URGENT_COUNT} high/urgent. Use fetch_inbox before proceeding when mail is pending."
  else
    START_CONTEXT="Agent Mail: you are ${HOOK_AGENT} on ${PROJECT}, but the inbox check failed — ${HOOK_ERROR}."
  fi
  hook_start_context "$START_CONTEXT"
  exit 0
fi

# Stop must always emit JSON.  A continuation generated by this hook receives a
# second Stop event; do not poll or block that continuation again.
if [[ "$(am_payload_field '.stop_hook_active')" == "true" ]]; then
  printf '{}\n'
  exit 0
fi

AGENT="$(am_agent_name "$HOOK_CLIENT" "$HOOK_SLOT")" || {
  printf '{}\n'
  exit 0
}
INTERVAL="${AGENT_MAIL_INTERVAL:-120}"
case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=120 ;; esac

# Identity and repository derivation are local.  The rate gate sits after them
# but before ensure/register/fetch so an ordinary turn cannot update last_active
# or otherwise touch the server inside the interval.
RATE_KEY="$(am_state_component "${PROJECT}|${AGENT}" 2>/dev/null || true)"
RATE_FILE=""
if [[ -n "$RATE_KEY" ]]; then
  mkdir -p "${AM_STATE_DIR}/rate" 2>/dev/null || true
  RATE_FILE="${AM_STATE_DIR}/rate/${HOOK_CLIENT}-${RATE_KEY}"
fi
NOW="$(date +%s)"
if [[ -n "$RATE_FILE" && -f "$RATE_FILE" ]]; then
  LAST_CHECK="$(grep -E '^[0-9]+$' "$RATE_FILE" 2>/dev/null || printf '0')"
  ELAPSED=$((NOW - LAST_CHECK))
  if [[ $ELAPSED -lt $INTERVAL ]]; then
    printf '{}\n'
    exit 0
  fi
fi
[[ -z "$RATE_FILE" ]] || printf '%s\n' "$NOW" > "$RATE_FILE" 2>/dev/null || true

if ! hook_ensure_agent 0; then
  hook_nonblocking_message \
    "Agent Mail inbox check failed for ${PROJECT}: ${HOOK_ERROR}."
  exit 0
fi
if ! hook_fetch_inbox; then
  hook_nonblocking_message \
    "Agent Mail inbox check failed for ${HOOK_AGENT}: ${HOOK_ERROR}."
  exit 0
fi

COUNTS="$(printf '%s' "$HOOK_INBOX" | jq -r \
  '[length, (map(select(.importance == "urgent" or .importance == "high")) | length)] | @tsv')"
MESSAGE_COUNT="${COUNTS%%$'\t'*}"
URGENT_COUNT="${COUNTS#*$'\t'}"

# Only unseen high/urgent message ids may continue a stopped turn.  The cache
# is updated after a successful inbox read and before the decision is emitted,
# so the same message cannot create a continuation loop.
SEEN_FILE=""
[[ -z "$RATE_KEY" ]] || SEEN_FILE="${AM_STATE_DIR}/rate/${HOOK_CLIENT}-urgent-${RATE_KEY}.json"
CURRENT_URGENT_IDS="$(printf '%s' "$HOOK_INBOX" | jq -c \
  '[.[] | select(.importance == "urgent" or .importance == "high") | (.id | tostring)]')"
PREVIOUS_URGENT_IDS="[]"
if [[ -n "$SEEN_FILE" && -r "$SEEN_FILE" ]]; then
  CACHED_IDS="$(cat "$SEEN_FILE" 2>/dev/null)"
  if printf '%s' "$CACHED_IDS" | jq -e 'type == "array"' >/dev/null 2>&1; then
    PREVIOUS_URGENT_IDS="$CACHED_IDS"
  fi
fi
NEW_URGENT_IDS="$(jq -nc --argjson current "$CURRENT_URGENT_IDS" \
  --argjson previous "$PREVIOUS_URGENT_IDS" '$current - $previous')"
NEW_URGENT_COUNT="$(printf '%s' "$NEW_URGENT_IDS" | jq 'length')"
if [[ -n "$SEEN_FILE" ]]; then
  SEEN_TMP="${SEEN_FILE}.$$.tmp"
  printf '%s\n' "$CURRENT_URGENT_IDS" > "$SEEN_TMP" 2>/dev/null \
    && mv -f "$SEEN_TMP" "$SEEN_FILE" 2>/dev/null || true
fi

if [[ $NEW_URGENT_COUNT -gt 0 ]]; then
  NEW_DETAILS="$(printf '%s' "$HOOK_INBOX" | jq -r --argjson ids "$NEW_URGENT_IDS" '
    [.[]
      | select((.id | tostring) as $id | ($ids | index($id)) != null)
      | "#\(.id) from \(.from // "unknown"): \(.subject // "(no subject)")"]
    | join("; ")
  ')"
  jq -nc --arg reason \
    "New high/urgent Agent Mail (${NEW_URGENT_COUNT}): ${NEW_DETAILS}. Call fetch_inbox now, handle or acknowledge it, then finish the turn." \
    '{decision:"block",reason:$reason}'
elif [[ $MESSAGE_COUNT -gt 0 ]]; then
  hook_nonblocking_message \
    "Agent Mail: ${MESSAGE_COUNT} unread message(s) for ${HOOK_AGENT}; ${URGENT_COUNT} are high/urgent. Use fetch_inbox when appropriate."
else
  printf '{}\n'
fi

exit 0
