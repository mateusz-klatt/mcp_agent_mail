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
  session-start|subagent-start|subagent-stop|heartbeat|stop|session-end) ;;
  *) exit 0 ;;
esac

# shellcheck source=/dev/null
. "$(dirname "$0")/agent_mail_common.sh" 2>/dev/null || {
  [[ "$EVENT" == "stop" || "$EVENT" == "subagent-stop" ]] && printf '{}\n'
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
  [[ "$EVENT" == "stop" || "$EVENT" == "subagent-stop" ]] && printf '{}\n'
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

# PostToolUse warnings must reach both the human-facing hook UI and the model.
# Codex ignores plain stdout for model context, so keep the documented
# hookSpecificOutput envelope alongside systemMessage.  This is deliberately
# separate from Stop output, whose decision contract must remain unchanged.
hook_post_tool_context() {
  if [[ "$HOOK_SURFACE" == "codex" ]]; then
    jq -nc --arg context "$1" \
      '{systemMessage:$context,
        hookSpecificOutput:{hookEventName:"PostToolUse",
                            additionalContext:$context}}'
  else
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

hook_subagent_start_context() {
  if [[ "$HOOK_SURFACE" == "copilot" ]]; then
    jq -nc --arg context "$1" '{additionalContext:$context}'
  else
    am_emit_context "SubagentStart" "$1"
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
  if [[ -n "$retired" ]]; then
    HOOK_ERROR="identity ${HOOK_AGENT} is retired and cannot receive new mail; this hook will not restore a manually decommissioned identity, so stop this session and have an operator explicitly restore it before restarting"
    return 1
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

# End events must not depend on the payload cwd still being a Git checkout, nor
# on the repository remaining opted in. SessionEnd persists its lifecycle
# tombstone before scanning every exact private execution; SubagentStop likewise
# resolves the native child entirely from private state.
if [[ "$EVENT" == "session-end" || "$EVENT" == "subagent-stop" ]]; then
  if [[ "$EVENT" == "subagent-stop" ]]; then
    am_subagent_executions_request_stop_all_for_payload "$HOOK_CLIENT" \
      >/dev/null 2>&1 || true
  else
    am_root_executions_end_all_for_payload "$HOOK_CLIENT" completed \
      >/dev/null 2>&1 || true
  fi
  hook_empty_stop_output
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
  elif [[ "$EVENT" == "subagent-start" ]]; then
    hook_subagent_start_context "$(am_project_activation_message "$PROJECT")"
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
  elif [[ "$EVENT" == "subagent-start" ]]; then
    hook_subagent_start_context "$MIGRATION_MESSAGE"
  elif [[ "$EVENT" == "stop" ]]; then
    hook_nonblocking_message "$MIGRATION_MESSAGE"
  else
    hook_empty_stop_output
  fi
  exit 0
fi

if [[ "$EVENT" == "session-start" ]]; then
  SESSION_SOURCE="$(am_payload_field '.source')"
  if ! am_session_run_begin "$HOOK_CLIENT" "$SESSION_SOURCE" >/dev/null; then
    hook_start_context \
      "Agent Mail: this ${HOOK_SURFACE} lifecycle is already ended and could not be resumed safely. The durable mailbox remains available, but execution-owned reservations are unavailable."
    exit 0
  fi
fi

# SubagentStart receives the parent session id plus a native agent_id. Reuse the
# durable mailbox credential and create a child execution beneath the active
# root execution; never call register_agent from this path.
if [[ "$EVENT" == "subagent-start" ]]; then
  HOOK_AGENT="$(am_agent_name "$HOOK_CLIENT" "$HOOK_SLOT")"
  HOOK_TOKEN="$(am_cred_get "$PROJECT" "$HOOK_AGENT")"
  if [[ -z "$HOOK_TOKEN" ]]; then
    hook_subagent_start_context \
      "Agent Mail: this subagent has no active durable parent identity. Do not call register_agent or create_agent_identity; report through the parent session. Execution-owned reservations are unavailable."
    exit 0
  fi
  if EXECUTION_ID="$(am_subagent_execution_start \
      "$PROJECT" "$HOOK_AGENT" "$HOOK_TOKEN" "$HOOK_CLIENT")"; then
    hook_subagent_start_context \
      "Agent Mail: execute as ${HOOK_AGENT} using subagent execution ${EXECUTION_ID}. Do not register or create another Agent. Reservations belong to this execution; the Git worktree is execution context, not identity."
  else
    hook_subagent_start_context \
      "Agent Mail: could not start the subagent execution for ${HOOK_AGENT}. Do not register or create another Agent; report through the parent. Treat reservation coordination as unavailable."
  fi
  exit 0
fi

if [[ "$EVENT" == "heartbeat" ]]; then
  HOOK_AGENT="$(am_agent_name "$HOOK_CLIENT" "$HOOK_SLOT")"
  HOOK_TOKEN="$(am_cred_get "$PROJECT" "$HOOK_AGENT")"
  # Codex documents cwd as the session working directory, not as a per-tool
  # target. If it names another opted-in repository on this event, establish the
  # same durable mailbox and lifecycle generation there before heartbeating it.
  # Never infer a second repository from command text.
  # A prior root state elsewhere proves SessionStart ran; without that proof a
  # global PostToolUse hook must not silently replace a missed lifecycle start.
  if [[ -n "$HOOK_AGENT" ]] \
    && ! am_session_end_intent_exists "$HOOK_CLIENT" \
    && am_session_has_root_execution_state "$HOOK_CLIENT" \
    && am_project_is_active "$PROJECT" "$HOOK_CLIENT" "$HOOK_SLOT" . \
    && ! am_identity_migration_pair "$PROJECT" "$HOOK_CLIENT" \
      "$HOOK_SLOT" >/dev/null; then
    if [[ -z "$HOOK_TOKEN" ]]; then
      HOOK_TOKEN="$(am_ensure_agent_credential \
        "$PROJECT" "$HOOK_AGENT" "$HOOK_CLIENT" "$HOOK_SLOT" \
        "$HOOK_PROGRAM" "$(hook_model_id)")"
    fi
    if [[ -n "$HOOK_TOKEN" ]]; then
      am_execution_ensure_for_payload "$PROJECT" "$HOOK_AGENT" \
        "$HOOK_TOKEN" "$HOOK_CLIENT" >/dev/null 2>&1 || true
    fi
  fi
  if [[ -n "$HOOK_AGENT" && -n "$HOOK_TOKEN" ]]; then
    am_execution_reconcile_for_payload "$PROJECT" "$HOOK_AGENT" \
      "$HOOK_TOKEN" "$HOOK_CLIENT" >/dev/null 2>&1 || true
    am_execution_heartbeat "$PROJECT" "$HOOK_AGENT" "$HOOK_TOKEN" \
      "$HOOK_CLIENT" >/dev/null 2>&1 || true
  fi
  # A PostToolUse payload can name a file owned by a repository other than the
  # session cwd. The target project never saw SessionStart, so establish the
  # same deterministic durable mailbox and native execution chain there before
  # touching liveness. This is lifecycle attribution only: Codex still has no
  # automatic reservation hook, and no claim is filed here.
  TARGET_PATH="$(am_payload_field '.tool_input.file_path')"
  [[ -n "$TARGET_PATH" ]] \
    || TARGET_PATH="$(am_payload_field '.tool_input.notebook_path')"
  TARGET_PROJECT=""
  [[ -z "$TARGET_PATH" ]] \
    || TARGET_PROJECT="$(am_project_key_for_file "$TARGET_PATH")"
  if [[ -n "$TARGET_PROJECT" && "$TARGET_PROJECT" != "$PROJECT" ]]; then
    TARGET_DIR="$(dirname "$(am_norm_path "$TARGET_PATH")")"
    # am_git, never `git -C`: under this hook's MSYS_NO_PATHCONV=1 export a
    # POSIX-form TARGET_DIR handed to native git.exe as an argument is not
    # translated, `git -C` exits 128, TARGET_REPO comes back empty, and the
    # whole cross-project enrollment below is silently skipped.
    TARGET_REPO="$(am_git "$TARGET_DIR" rev-parse --show-toplevel)"
    if [[ -n "$TARGET_REPO" ]] \
      && ! am_session_end_intent_exists "$HOOK_CLIENT" \
      && am_project_is_active "$TARGET_PROJECT" "$HOOK_CLIENT" "$HOOK_SLOT" \
        "$TARGET_DIR" \
      && ! am_identity_migration_pair "$TARGET_PROJECT" "$HOOK_CLIENT" \
        "$HOOK_SLOT" >/dev/null; then
      export AM_PROJECT_FOR_NAME="$TARGET_PROJECT"
      TARGET_AGENT="$(am_agent_name "$HOOK_CLIENT" "$HOOK_SLOT")"
      TARGET_TOKEN="$(am_cred_get "$TARGET_PROJECT" "$TARGET_AGENT")"
      if [[ -z "$TARGET_TOKEN" ]]; then
        TARGET_TOKEN="$(am_ensure_agent_credential \
          "$TARGET_PROJECT" "$TARGET_AGENT" "$HOOK_CLIENT" "$HOOK_SLOT" \
          "$HOOK_PROGRAM" "$(hook_model_id)")"
      fi
      if [[ -n "$TARGET_AGENT" && -n "$TARGET_TOKEN" ]] \
        && ! am_session_end_intent_exists "$HOOK_CLIENT"; then
        if AM_EXECUTION_CWD_OVERRIDE="$TARGET_REPO" \
          am_execution_ensure_for_payload "$TARGET_PROJECT" "$TARGET_AGENT" \
            "$TARGET_TOKEN" "$HOOK_CLIENT" >/dev/null; then
          AM_EXECUTION_CWD_OVERRIDE="$TARGET_REPO" \
            am_execution_reconcile_for_payload "$TARGET_PROJECT" \
              "$TARGET_AGENT" "$TARGET_TOKEN" "$HOOK_CLIENT" \
              >/dev/null 2>&1 || true
          AM_EXECUTION_CWD_OVERRIDE="$TARGET_REPO" \
            am_execution_heartbeat "$TARGET_PROJECT" "$TARGET_AGENT" \
              "$TARGET_TOKEN" "$HOOK_CLIENT" >/dev/null 2>&1 || true
        fi
      fi
    fi
  fi

  # Keep every root chain for this exact native session generation alive. The
  # current cwd and any explicit file target were enrolled above, so they are
  # included in the same exact-state scan without guessing paths from command
  # text.
  am_root_executions_heartbeat_all_for_payload "$HOOK_CLIENT" \
    >/dev/null 2>&1 || true

  TOOL_NAME="$(am_payload_field '.tool_name')"
  if [[ "$TOOL_NAME" == "apply_patch" || "$TOOL_NAME" == "Bash" ]] \
    && [[ -z "$(am_payload_field '.tool_input.file_path')" ]] \
    && [[ -z "$(am_payload_field '.tool_input.notebook_path')" ]]; then
    WARN_GENERATION="$(am_session_lifecycle_generation "$HOOK_CLIENT")"
    WARN_KEY="$(am_state_component \
      "${HOOK_CLIENT}|$(am_payload_field '.session_id')|${WARN_GENERATION}|${TOOL_NAME}|unattributable-target" \
      2>/dev/null || true)"
    WARN_FILE="${AM_STATE_DIR}/rate/${WARN_KEY}.warned"
    WARN_LOCK="${WARN_FILE}.lock"
    if [[ -n "$WARN_KEY" ]] && am_lock_acquire "$WARN_LOCK"; then
      if [[ ! -f "$WARN_FILE" ]]; then
        printf '%s\n' "$(date +%s)" > "$WARN_FILE" 2>/dev/null || true
        am_lock_release "$WARN_LOCK"
        hook_post_tool_context \
          "Agent Mail lifecycle scope: ${TOOL_NAME} was attributed only to the Git project containing the hook cwd. Its command payload is intentionally not parsed for paths, so writes into another repository cannot be enrolled or guarded automatically; run the tool from that repository or coordinate/reserve it explicitly."
      else
        am_lock_release "$WARN_LOCK"
      fi
    fi
  fi
  exit 0
fi

if [[ "$EVENT" == "session-start" ]]; then
  if ! hook_ensure_agent 1; then
    hook_start_context \
      "Agent Mail: could not register ${PROJECT} — ${HOOK_ERROR}. No inbox or reservation coordination is active for this ${HOOK_SURFACE} session."
    exit 0
  fi
  if EXECUTION_ID="$(am_root_execution_start \
      "$PROJECT" "$HOOK_AGENT" "$HOOK_TOKEN" "$HOOK_CLIENT")"; then
    EXECUTION_CONTEXT=" Root execution: ${EXECUTION_ID}."
  else
    EXECUTION_CONTEXT=" The durable mailbox is connected, but its root execution could not be started; execution-owned reservations are unavailable."
  fi
  if hook_fetch_inbox; then
    COUNTS="$(printf '%s' "$HOOK_INBOX" | jq -r \
      '[length, (map(select(.importance == "urgent" or .importance == "high")) | length)] | @tsv')"
    MESSAGE_COUNT="${COUNTS%%$'\t'*}"
    URGENT_COUNT="${COUNTS#*$'\t'}"
    START_CONTEXT="Agent Mail: you are ${HOOK_AGENT} on ${PROJECT}.${EXECUTION_CONTEXT} Unread inbox: ${MESSAGE_COUNT} message(s), ${URGENT_COUNT} high/urgent. Use fetch_inbox before proceeding when mail is pending."
  else
    START_CONTEXT="Agent Mail: you are ${HOOK_AGENT} on ${PROJECT}.${EXECUTION_CONTEXT} The inbox check failed — ${HOOK_ERROR}."
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
STOP_TOKEN="$(am_cred_get "$PROJECT" "$AGENT")"
if [[ -n "$STOP_TOKEN" ]]; then
  am_execution_reconcile_for_payload "$PROJECT" "$AGENT" "$STOP_TOKEN" \
    "$HOOK_CLIENT" >/dev/null 2>&1 || true
fi
INTERVAL="${AGENT_MAIL_INTERVAL:-120}"
case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=120 ;; esac

# Identity and repository derivation are local.  The rate gate sits after them
# but before ensure/register/fetch so an ordinary turn cannot update last_active
# or otherwise touch the server inside the interval.
ROOT_EXECUTION_ID="$(am_execution_state_value \
  "$PROJECT" "$AGENT" "$HOOK_CLIENT" session \
  "$(am_payload_field '.session_id')" \
  'select(.status == "active") | .execution_id')"
RATE_SCOPE="${ROOT_EXECUTION_ID:-session:$(am_payload_field '.session_id')}"
RATE_KEY="$(am_state_component \
  "${PROJECT}|${AGENT}|${RATE_SCOPE}" 2>/dev/null || true)"
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
am_execution_reconcile_for_payload "$PROJECT" "$HOOK_AGENT" "$HOOK_TOKEN" \
  "$HOOK_CLIENT" >/dev/null 2>&1 || true
am_execution_heartbeat "$PROJECT" "$HOOK_AGENT" "$HOOK_TOKEN" \
  "$HOOK_CLIENT" >/dev/null 2>&1 || true
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
