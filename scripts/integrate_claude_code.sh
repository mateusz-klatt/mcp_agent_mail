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
# This installer handles bearer-bearing config writes. Disable xtrace whether
# it came from --debug or an external `bash -x`; otherwise later environment
# assignments would copy the bearer into stderr and shell transcripts.
case "$-" in
  *x*)
    set +x
    log_warn "Command tracing is disabled while the integration handles credentials."
    ;;
esac
require_cmd curl
require_cmd jq  # Required for safe JSON merging (avoids quote injection vulnerabilities)
require_cmd git

_normalize_claude_shared_path() {
  local target="$1"
  case "$target" in
    [a-zA-Z]:\\*|[a-zA-Z]:/*)
      case "$(uname -s 2>/dev/null || printf unknown)" in
        MINGW*|MSYS*|CYGWIN*)
          command -v cygpath >/dev/null 2>&1 || {
            log_err "cygpath is required to normalize the Windows shared env path: ${target}" >&2
            return 1
          }
          cygpath -u "$target" 2>/dev/null || {
            log_err "Could not normalize the Windows shared env path: ${target}" >&2
            return 1
          } ;;
        *)
          command -v wslpath >/dev/null 2>&1 || {
            log_err "wslpath is required to normalize the Windows shared env path: ${target}" >&2
            return 1
          }
          wslpath -u "$target" 2>/dev/null || {
            log_err "Could not normalize the Windows shared env path: ${target}" >&2
            return 1
          } ;;
      esac ;;
    *) printf '%s\n' "$target" ;;
  esac
}

SHARED_ENV_FILE="$(_normalize_claude_shared_path \
  "${AGENT_MAIL_ENV_FILE:-${HOME}/.agent-mail.env}")" || exit 1
case "$SHARED_ENV_FILE" in
  /*) ;;
  *)
    log_err "Shared Agent Mail env target must be an absolute path: ${SHARED_ENV_FILE}"
    exit 1 ;;
esac
AGENT_MAIL_ENV_FILE="$SHARED_ENV_FILE"

log_step "Claude Code Integration (user scope)"
echo
echo "This installs MCP Agent Mail once for the current user:"
echo "  - hooks: ~/.claude/hooks/mcp-agent-mail"
echo "  - hook settings: ~/.claude/settings.json"
echo "  - MCP server: Claude user scope (~/.claude.json)"
echo "No repository configuration or server credential is created."
echo
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

log_step "Preparing user-level Claude settings"
CLAUDE_DIR="${HOME}/.claude"
SETTINGS_PATH="${CLAUDE_DIR}/settings.json"
CLAUDE_USER_JSON="${HOME}/.claude.json"
HOOKS_DIR="${CLAUDE_DIR}/hooks/mcp-agent-mail"

# Preflight every input before creating a directory, backup, or partial hook
# installation.  A dry run and a refused merge must leave HOME byte-for-byte
# untouched.
AGENT_MAIL_HOOKS=(
  agent_mail_common.sh
  session_start.sh
  inbox_check.sh
  reservations_warn.sh
  autoreserve.sh
  session_end.sh
  inbox_watch.sh
)
_hooks_missing=()
for _h in "${AGENT_MAIL_HOOKS[@]}"; do
  if [[ ! -f "${ROOT_DIR}/scripts/hooks/${_h}" ]]; then
    _hooks_missing+=("${_h}")
  elif ! bash -n "${ROOT_DIR}/scripts/hooks/${_h}"; then
    log_err "Required hook script has invalid shell syntax: ${_h}"
    log_err "No user configuration was changed."
    exit 1
  fi
done
if [[ ${#_hooks_missing[@]} -gt 0 ]]; then
  log_err "Missing required hook scripts: ${_hooks_missing[*]}"
  log_err "No user configuration was changed."
  exit 1
fi

EXISTING_SETTINGS="{}"
EXISTING_CLAUDE_USER="{}"
if [[ -f "$SETTINGS_PATH" ]]; then
  if ! jq -e '
      def valid_handler:
        type == "object" and
        ((has("command") | not) or (.command | type == "string"));
      def valid_group:
        type == "object" and
        (.hooks | type == "array") and
        all(.hooks[]; valid_handler);
      type == "object" and
      ((has("hooks") | not) or (.hooks | type == "object")) and
      (if has("hooks") then
        all(.hooks[]; type == "array" and all(.[]; valid_group))
      else true end)
    ' "$SETTINGS_PATH" >/dev/null 2>&1; then
    log_err "Existing ${SETTINGS_PATH} has an invalid nested hook shape; refusing to overwrite it."
    exit 1
  fi
  EXISTING_SETTINGS="$(cat "$SETTINGS_PATH")"
fi
if [[ -f "$CLAUDE_USER_JSON" ]]; then
  if ! jq -e '
      type == "object" and
      ((has("mcpServers") | not) or (.mcpServers | type == "object"))
    ' "$CLAUDE_USER_JSON" >/dev/null 2>&1; then
    log_err "Existing ${CLAUDE_USER_JSON} has an invalid mcpServers shape; refusing to overwrite it."
    exit 1
  fi
  EXISTING_CLAUDE_USER="$(cat "$CLAUDE_USER_JSON")"
fi

# Client/platform/host/slot identity is stable across restarts and distinct from
# every other coding client on the same machine.
_CLAUDE_SLOT="$(integration_slot "${AGENT_MAIL_CLAUDE_SLOT:-1}")"
_AGENT="$(integration_agent_name claude "${_CLAUDE_SLOT}")"
log_ok "Claude identity template: ${_AGENT}"

if [[ -z "${_AGENT}" ]]; then
  log_err "Could not derive a stable Claude agent identity."
  exit 1
fi

# The hook commands carry only the Claude-specific slot, never shared settings
# or secrets. The client itself is a literal argument at each hook call site.
#
# Every value the hooks need they derive or read for themselves: the project key
# from the repo's origin remote, the identity from the hostname, the rest from
# ${ENV_FILE}. That lets one installation serve every explicitly activated
# repository on the machine rather than binding it to the directory from which
# the installer happened to run.
#
# This block used to pin AGENT_MAIL_PROJECT and AGENT_MAIL_AGENT into each
# command. Both are the wrong shape and the second is actively dangerous: a
# pinned identity or project key follows the machine into other checkouts and
# creates a project no other agent can join, with every hook still reporting
# success. Deriving them costs nothing and cannot drift.
#
# `|| true` on every command is load-bearing, not defensive habit: a PreToolUse
# hook that exits non-zero BLOCKS the tool call it was only meant to comment on.
# Three branches, not four. WSL is Linux for everything this script does —
# POSIX $HOME, `/` separators, an ordinary filesystem — and would need its own
# branch only to write Windows paths under /mnt/c, which this script neither
# does nor should (measured on WSL2 by home-wsl-1). `uname -s` cannot spot WSL
# on its own: it reports "Linux" there like any other Linux. /proc/version is
# the reliable discriminator and does not depend on $WSL_* being exported, which
# a hook launched by Claude Code cannot count on.
_OS=posix _WSL=0
case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*) _OS=windows ;;
esac
if [[ "${_OS}" == "posix" ]] && [[ -r /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null; then
  _WSL=1  # informational only; the install is identical to Linux
fi

# On Windows the command has to name a bash to run the hook with, and this is
# not a formality. Claude Code hands the string to cmd.exe, which cannot execute
# a .sh and has no `true`. Measured with the hook script present in every case:
#
#   /c/.../session_start.sh || true    rc=0  "path not found", "'true' is not…"
#   C:/.../session_start.sh            rc=0  silent — Windows opens a
#                                            file-association dialog instead
#   bash.exe -c "'/c/…' || true"       rc=0  the hook runs
#
# All three exit 0, so every one of them is reported healthy while two never
# executed a line. `|| true` belongs INSIDE the wrapper, where it absorbs a
# server outage without blocking the user's edit; outside it, it absorbs the
# hook not being runnable at all.
_BASH_WRAP=""
# Declared before the branch, not inside it: only the else-branch fills them, but
# the diagnostic below reads them on both paths. An "only assigned on one branch"
# variable read on the other is how this installer used to abort under `set -u`
# after writing the operator's credentials and before installing any hook.
_roots=()
_fallback_roots=()
if [[ "${_OS}" == "windows" ]]; then
  command -v cygpath >/dev/null 2>&1 || {
    log_err "Windows detected but cygpath is unavailable."
    exit 1
  }
  if [[ -n "${AGENT_MAIL_GIT_BASH_PATH:-}" ]]; then
    # A Windows path is kept in the form it was given. It used to be sent
    # through cygpath -u and back through cygpath -m, and that round trip does
    # not return what went in: MSYS mounts the Git install root at /, so
    #
    #   C:/Program Files/Git/bin/bash.exe  --cygpath -u-->  /bin/bash.exe
    #   /bin/bash.exe                      --cygpath -m-->  C:/Program Files/Git/usr/bin/bash.exe
    #
    # and those are different binaries — 47 KB against 2.4 MB, the launcher that
    # sets MSYSTEM=MINGW64 against the raw shell that does not. The POSIX view
    # cannot express Git\bin at all, so it answers with the other one. The
    # effect was that naming the correct shell explicitly got it silently
    # swapped for the wrong one, in the escape hatch that exists for when
    # detection fails. Bash tests C:/ paths directly, so nothing needs
    # converting; only backslashes are normalised, for the JSON we emit.
    case "$AGENT_MAIL_GIT_BASH_PATH" in
      [a-zA-Z]:\\*|[a-zA-Z]:/*)
        _c="${AGENT_MAIL_GIT_BASH_PATH//\\//}" ;;
      /*) _c="$AGENT_MAIL_GIT_BASH_PATH" ;;
      *)
        log_err "AGENT_MAIL_GIT_BASH_PATH must be an absolute Windows or Git Bash path."
        exit 1 ;;
    esac
    if [[ ! -x "$_c" ]]; then
      log_err "AGENT_MAIL_GIT_BASH_PATH is not an executable Git Bash: ${AGENT_MAIL_GIT_BASH_PATH}"
      exit 1
    fi
    case "$_c" in
      [a-zA-Z]:/*) _BASH_WRAP="$_c" ;;
      *)
        # A POSIX path has no Windows form to preserve, so converting is the
        # only option here — including its ambiguity about /bin.
        _BASH_WRAP="$(cygpath -m "$_c" 2>/dev/null)" || {
          log_err "Could not translate AGENT_MAIL_GIT_BASH_PATH for Windows."
          exit 1
        } ;;
    esac
  else
    # Git for Windows ships TWO bash.exe and they are not interchangeable when
    # the caller is Claude (cmd.exe), which is the only way these hooks ever
    # run. Measured on this machine, same probe through each:
    #
    #   Git\bin\bash.exe        MSYSTEM=MINGW64  git=/mingw64/bin/git
    #                                            curl=/mingw64/bin/curl
    #   Git\usr\bin\bash.exe    MSYSTEM=         git=/cmd/git
    #                                            curl=/c/WINDOWS/system32/curl
    #
    # `Git\bin\bash.exe` is the launcher that initialises the MSYS environment;
    # `usr\bin\bash.exe` is the raw shell, and started from outside it comes up
    # with no MSYSTEM and without /mingw64/bin on PATH. The hooks then resolve
    # whatever Windows happens to have — system32's curl instead of Git's — so
    # the failure is not "hook did not run" but "hook ran against different
    # tools than it was written for", which is the harder kind to see.
    #
    # This matters because `command -v bash` picks the wrong one: run from
    # inside Git Bash it answers /usr/bin/bash, so an installer that trusts it
    # writes the raw shell into every hook command. Deriving from `git` instead
    # walks the install root and finds bin/ before usr/bin/. `git` is a hard
    # requirement of this script (require_cmd git above), so it is always there,
    # and it locates Git for Windows wherever it was installed — scoop,
    # chocolatey, portable, another drive — which the literals below cannot.
    _git_bin="$(command -v git 2>/dev/null || true)"
    _git_root=""
    if [[ -n "$_git_bin" ]]; then
      _git_root="$(cygpath -m "$_git_bin" 2>/dev/null || true)"
    fi
    # Ancestors of git.exe, nearest first.
    while [[ -n "$_git_root" && "$_git_root" == */* ]]; do
      _git_root="${_git_root%/*}"
      _roots+=("$_git_root")
    done
    # The literals are a LAST resort and are kept in their own list, not appended
    # to the discovered ones. Sharing a list makes them compete on equal terms:
    # the default install satisfies any test the discovered root does, so a
    # single ordered sweep can answer "C:\Program Files\Git" on a machine that
    # runs a portable Git somewhere else entirely — silently, because that answer
    # is a real working bash. Discovered roots are exhausted first, both passes,
    # before a guess is allowed to win.
    _fallback_roots=("/c/Program Files/Git" "/c/Program Files (x86)/Git")
    # ${a[@]+"${a[@]}"} rather than "${a[@]}": an empty array is an unbound
    # variable under `set -u` before Bash 4.4.
    # Existence is tested in the form the candidate was built in — bash accepts
    # C:/ paths directly — but the answer always goes through `cygpath -m`.
    #
    # Skipping that call for paths that already look like C:/... is tempting and
    # wrong. It is a no-op on a mixed-form path, so it buys nothing, and it is
    # the one place where the platform gets to say what this path is called on
    # Windows. Removing it makes the installer assert a translation instead of
    # asking for one, which is unobservable here and breaks anywhere the mapping
    # is not identity — a portable Git reached through a subst'd or mapped
    # drive, and the test harness that simulates exactly that.
    #
    # The `cygpath -u` round trip that this branch used to do IS still wrong and
    # is not coming back: on a path under the Git install root, -u answers
    # relative to the MSYS mount table (C:/Program Files/Git/bin comes back as
    # plain /bin) and converting that back lands on Git\usr\bin — a different
    # binary. Asking once, in one direction, is the whole fix.
    #
    # Two passes, and the first one is the point. Git for Windows ships BOTH
    # bin/bash.exe (the 47 KB launcher that sets MSYSTEM=MINGW64) and
    # usr/bin/bash.exe (the 2.4 MB shell, which comes up with neither MSYSTEM
    # nor /mingw64/bin on PATH). Only the install root has both, so requiring
    # both IDENTIFIES the root instead of guessing at it:
    #
    #   Git            bin/bash.exe yes   usr/bin/bash.exe yes   -> the root
    #   Git/usr        bin/bash.exe yes   usr/bin/bash.exe no    -> the raw shell
    #   some/ancestor  bin/bash.exe yes   usr/bin/bash.exe no    -> unrelated bash
    #
    # Both of those false positives are real. `Git/usr` appears whenever the
    # caller's PATH did not go through /etc/profile, because `git` then resolves
    # to Git/usr/bin/git rather than Git/mingw64/bin/git. The unrelated one
    # appears whenever any ancestor directory of git.exe happens to contain
    # bin/bash.exe — which is what ordering by depth alone gets wrong, in both
    # directions: nearest-first walks into Git/usr, drive-first walks into the
    # unrelated ancestor.
    #
    # The loose pass keeps an unusual install working (a tree with only one of
    # the two), and runs nearest-first so it prefers the Git we actually found.
    for _rootset in discovered fallback; do
      if [[ "$_rootset" == discovered ]]; then
        _candidate_roots=(${_roots[@]+"${_roots[@]}"})
      else
        _candidate_roots=(${_fallback_roots[@]+"${_fallback_roots[@]}"})
      fi
      for _pass in strict loose; do
        for _root in ${_candidate_roots[@]+"${_candidate_roots[@]}"}; do
          if [[ "$_pass" == strict ]]; then
            [[ -x "${_root}/bin/bash.exe" && -x "${_root}/usr/bin/bash.exe" ]] || continue
          fi
          for _suffix in bin/bash.exe usr/bin/bash.exe; do
            _c="${_root}/${_suffix}"
            [[ -x "$_c" ]] || continue
            _BASH_WRAP="$(cygpath -m "$_c" 2>/dev/null)" || {
              log_err "Could not translate Git Bash for Windows."
              exit 1
            }
            break 4
          done
        done
      done
    done
  fi
  # Diagnostic for the one decision on this platform that is invisible once
  # made: which of the several bash.exe on the machine ends up in every hook
  # command. Set AGENT_MAIL_DEBUG_BASH_RESOLUTION to 1 for stderr, or to an
  # absolute path to append there instead — stderr is captured and truncated
  # when the installer runs under a test harness, which is exactly when the
  # answer matters most.
  if [[ -n "${AGENT_MAIL_DEBUG_BASH_RESOLUTION:-}" ]]; then
    _dbg() {
      case "${AGENT_MAIL_DEBUG_BASH_RESOLUTION}" in
        /*|[a-zA-Z]:/*) printf '%s\n' "$1" >> "${AGENT_MAIL_DEBUG_BASH_RESOLUTION}" ;;
        *) printf '%s\n' "$1" >&2 ;;
      esac
    }
    _dbg "DEBUG git=${_git_bin:-<none>} roots=${#_roots[@]} wrap=${_BASH_WRAP:-<none>}"
    for _r in ${_roots[@]+"${_roots[@]}"} ${_fallback_roots[@]+"${_fallback_roots[@]}"}; do
      _dbg "DEBUG root=$_r bin=$([[ -x "$_r/bin/bash.exe" ]] && echo yes || echo no) usrbin=$([[ -x "$_r/usr/bin/bash.exe" ]] && echo yes || echo no)"
    done
  fi
  if [[ -z "${_BASH_WRAP}" ]]; then
    log_err "Windows detected but no Git for Windows bash.exe found."
    log_err "Hook commands here exit 0 without running, so the hooks would look installed and do nothing."
    log_err "Install Git for Windows or set AGENT_MAIL_GIT_BASH_PATH, then re-run this script."
    exit 1
  fi
fi

# `bash <file>` rather than executing the path, on both branches, because the
# execute bit is not reliable anywhere it matters. On macOS and Linux a copy can
# arrive without it and the bare path then fails with "Permission denied"
# (measured by laptop-mac-1). Naming the interpreter removes the dependency.
#
# Inside the Windows wrapper the inner `bash` is safe, which is not obvious: on
# the PATH cmd.exe sees, `bash` is the WSL launcher, and that is exactly what
# made `bash <file>` wrong as a top-level command here. Inside the wrapper the
# shell is already Git Bash, so its own PATH answers — measured: `command -v
# bash` -> /usr/bin/bash, `uname -o` -> Msys. Both forms run the hook.
#
# The execute bit cannot in fact be lost on Windows, for the same reason chmod
# does nothing there: with `noacl` the mode is inferred, and a .sh stays 755
# through `chmod 644` and `chmod -x`. So this guard is inert here and load
# bearing elsewhere, which is the right way round.
_shell_quote() {
  # Bash's printf %q is available in the macOS Bash 3.2 baseline and avoids
  # that release's different parsing of quote-heavy // pattern substitution.
  printf '%q' "$1"
}

_hook_cmd() {
  local hook_path="${HOOKS_DIR}/$1" hook_path_q
  hook_path_q="$(_shell_quote "$hook_path")"
  if [[ -n "${_BASH_WRAP}" ]]; then
    # Inner command shell-escaped for bash, whole thing double-quoted for cmd.exe.
    printf '"%s" -c "AGENT_MAIL_CLAUDE_SLOT=%s bash %s || true"' \
      "${_BASH_WRAP}" "${_CLAUDE_SLOT}" "$hook_path_q"
  else
    printf 'AGENT_MAIL_CLAUDE_SLOT=%s bash %s || true' \
      "${_CLAUDE_SLOT}" "$hook_path_q"
  fi
}

# User settings contain hook definitions only.  MCP credentials are installed
# separately at Claude's user MCP scope.  EXISTING_SETTINGS was validated in
# the no-mutation preflight above.

# Migrate only hook commands previously managed by this installer.  Filter at
# the inner command level so an operator's custom command sharing the same hook
# group survives.  This removes the old direct-checkout scripts/hooks paths,
# project-local .claude/hooks paths and the current user-level marker before the
# canonical set is appended below.
EXISTING_SETTINGS="$(printf '%s' "$EXISTING_SETTINGS" | jq '
  def agent_mail_command:
    ((.command? // "") | ascii_downcase | gsub("\\\\"; "/")) as $command |
    ($command |
      contains("mcp-agent-mail/") or
      contains("mcp_agent_mail/scripts/hooks/")) and
    ($command | test(
      "(?:session_start|inbox_check|reservations_warn|autoreserve|session_end)[.]sh"
    ));
  if (.hooks | type) == "object" then
    .hooks |= with_entries(
      if (.value | type) == "array" then
        .value |= map(
          if (.hooks | type) == "array" then
            .hooks |= map(select(agent_mail_command | not))
          else . end
        ) | .value |= map(select((.hooks | type) != "array" or (.hooks | length) > 0))
      else . end
    )
  else . end
')" || {
  log_err "Could not clean managed hooks in ${SETTINGS_PATH}; no user configuration was changed."
  exit 1
}

# Build hook configs as JSON
# The four events, in the shape that is running on the fleet today. Matchers
# included: PreToolUse and PostToolUse fire on file-writing tools, not on Bash —
# a reservation warning is only useful immediately before an edit, and firing it
# on every shell command is how a warning becomes noise nobody reads.
SESSION_START_HOOK=$(jq -nc --arg a "$(_hook_cmd session_start.sh)" --arg b "$(_hook_cmd inbox_check.sh)" \
  '{matcher:"",hooks:[{type:"command",command:$a},{type:"command",command:$b}]}')
PRE_TOOL_USE_HOOK=$(jq -nc --arg a "$(_hook_cmd reservations_warn.sh)" \
  '{matcher:"Edit|Write|NotebookEdit",hooks:[{type:"command",command:$a}]}')
# PostToolUse is split in two, because the two hooks under it want opposite
# matchers.
#
# autoreserve.sh must stay narrow: it files a reservation for the file that was
# just written, so firing it after a shell command would reserve nothing and
# say so, on every command.
#
# inbox_check.sh must be wide, and this was measured the hard way. It is the
# only caller of am_sync_model, which is what keeps the agent's `model` field
# true. Under the narrow matcher an agent that never edits a file never
# refreshes it: the SessionStart run happens before the transcript has an
# assistant turn to read, so it records "unknown", and PostToolUse never fires
# at all. laptop-mac-1 spent a day at maximum activity with a `model` that had
# been wrong since morning — last_active_ts fresh to the second, model stale
# since the fix landed. The two fields look like they answer the same question
# and do not, so the agent that looks most alive can be the one whose model is
# least trustworthy, which is exactly backwards for routing work by model.
#
# The cost of the wide matcher is bounded and was checked before widening:
# am_sync_model sits BELOW the rate gate in inbox_check.sh, so the hook exits
# early within the interval and syncs at most once per 120s regardless of how
# many commands run.
POST_TOOL_USE_HOOK=$(jq -nc --arg a "$(_hook_cmd autoreserve.sh)" \
  '{matcher:"Edit|Write|NotebookEdit",hooks:[{type:"command",command:$a}]}')
POST_TOOL_USE_INBOX_HOOK=$(jq -nc --arg a "$(_hook_cmd inbox_check.sh)" \
  '{matcher:"",hooks:[{type:"command",command:$a}]}')
SESSION_END_HOOK=$(jq -nc --arg a "$(_hook_cmd session_end.sh)" \
  '{matcher:"",hooks:[{type:"command",command:$a}]}')

# Merge hooks into existing config (preserves permissions, plugins, other hooks).
# The last argument is the de-duplication marker: it must appear in a command
# this script writes, or a re-run appends a second copy of every hook.
MERGED_SETTINGS="$EXISTING_SETTINGS"
MERGED_SETTINGS=$(json_append_hook "$MERGED_SETTINGS" "SessionStart" "$SESSION_START_HOOK" "mcp-agent-mail/session_start.sh")
MERGED_SETTINGS=$(json_append_hook "$MERGED_SETTINGS" "PreToolUse" "$PRE_TOOL_USE_HOOK" "mcp-agent-mail/reservations_warn.sh")
MERGED_SETTINGS=$(json_append_hook "$MERGED_SETTINGS" "PostToolUse" "$POST_TOOL_USE_HOOK" "mcp-agent-mail/autoreserve.sh")
# Its own marker keeps the two PostToolUse entries distinct.  The managed-hook
# migration above removes earlier Agent Mail commands before these canonical
# entries are appended, so re-running also widens a previously narrow inbox
# check without duplicating or disturbing foreign commands.
MERGED_SETTINGS=$(json_append_hook "$MERGED_SETTINGS" "PostToolUse" "$POST_TOOL_USE_INBOX_HOOK" "mcp-agent-mail/inbox_check.sh")
MERGED_SETTINGS=$(json_append_hook "$MERGED_SETTINGS" "SessionEnd" "$SESSION_END_HOOK" "mcp-agent-mail/session_end.sh")

if ! printf '%s' "$MERGED_SETTINGS" | jq -e '
    def valid_handler:
      type == "object" and
      ((has("command") | not) or (.command | type == "string"));
    def valid_group:
      type == "object" and
      (.hooks | type == "array") and
      all(.hooks[]; valid_handler);
    type == "object" and
    (.hooks | type == "object") and
    all(.hooks[]; type == "array" and all(.[]; valid_group))
  ' >/dev/null; then
  log_err "Generated invalid nested hook JSON for ${SETTINGS_PATH}; no user configuration was changed."
  exit 1
fi

# Claude officially stores user-scoped MCP servers in ~/.claude.json.  The
# bearer is passed to jq through its environment: unlike /dev/fd/3 this works
# with native jq.exe in Git Bash, and unlike --arg it never appears in argv.
MERGED_CLAUDE_USER="$(
  AGENT_MAIL_INSTALL_URL="${_URL}" \
  AGENT_MAIL_INSTALL_AUTHORIZATION="Bearer ${_TOKEN}" \
  jq '
    .mcpServers = (.mcpServers // {}) |
    .mcpServers["mcp-agent-mail"] = {
      type:"http",
      url:env.AGENT_MAIL_INSTALL_URL,
      headers:{Authorization:env.AGENT_MAIL_INSTALL_AUTHORIZATION}
    }
  ' <<<"$EXISTING_CLAUDE_USER"
)" || {
  log_err "Could not merge ${CLAUDE_USER_JSON}; no user configuration was changed."
  exit 1
}
if ! printf '%s' "$MERGED_CLAUDE_USER" | jq -e '
    type == "object" and
    (.mcpServers | type == "object") and
    (.mcpServers["mcp-agent-mail"] | type == "object")
  ' >/dev/null; then
  log_err "Generated invalid MCP JSON for ${CLAUDE_USER_JSON}; no user configuration was changed."
  exit 1
fi

# All input validation and output construction above are read-only.  A dry run
# exits before backup creation, directory creation, chmod, or config writes.
if [[ "$DRY_RUN" == "1" ]]; then
  _print "[dry-run] write shared Agent Mail URL/bearer"
  _print "[dry-run] install Claude hooks under ${HOOKS_DIR}"
  _print "[dry-run] merge Claude hooks into ${SETTINGS_PATH}"
  _print "[dry-run] merge Claude MCP into ${CLAUDE_USER_JSON}"
  _print "[dry-run] no files or directories were changed"
  exit 0
fi

for _config in "$SETTINGS_PATH" "$CLAUDE_USER_JSON"; do
  backup_user_file "$_config" || exit 1
done

log_step "Writing shared server settings"
write_shared_agent_mail_env "${_URL}" "${_TOKEN}" || {
  log_err "Could not write ${SHARED_ENV_FILE}"
  exit 1
}

log_step "Installing the hook scripts"
_hooks_installed=0
for _h in "${AGENT_MAIL_HOOKS[@]}"; do
  write_atomic "${HOOKS_DIR}/${_h}" < "${ROOT_DIR}/scripts/hooks/${_h}"
  if [[ "${_h}" == "agent_mail_common.sh" ]]; then
    set_secure_file "${HOOKS_DIR}/${_h}" || exit 1
  else
    set_secure_exec "${HOOKS_DIR}/${_h}" || exit 1
  fi
  _hooks_installed=$((_hooks_installed + 1))
done
log_ok "Installed ${_hooks_installed}/${#AGENT_MAIL_HOOKS[@]} hook scripts to ${HOOKS_DIR}"

write_atomic "$SETTINGS_PATH" <<<"$MERGED_SETTINGS"
set_secure_file "$SETTINGS_PATH" || true
log_ok "Merged hooks into ${SETTINGS_PATH} (existing config preserved)"

log_step "Registering MCP server in Claude user config"
write_atomic "$CLAUDE_USER_JSON" <<<"$MERGED_CLAUDE_USER"
set_secure_file "$CLAUDE_USER_JSON" || true

log_ok "==> Claude Code user integration complete."
_print "Hook settings: ${SETTINGS_PATH}"
_print "Hook scripts: ${HOOKS_DIR}"
_print "MCP server: user scope in ${CLAUDE_USER_JSON}"
_print "SessionStart will register ${_AGENT} only in an explicitly activated project and keep its token in credentials.json."
