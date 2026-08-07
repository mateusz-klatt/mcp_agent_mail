# The Claude Code hooks

Five hooks plus a watcher, installed once per machine, working in every
repository without per-project setup. They are what makes the mailbox reach an
agent that is busy, and the reservations reach an agent about to edit a file
somebody else is already in.

Nothing here is installed by `scripts/integrate_claude_code.sh` — see the last
section before you consider running it.

## What each one does

| Hook | Event | Speaks when |
|---|---|---|
| `session_start.sh` | SessionStart | always: identity, and who else holds what |
| `inbox_check.sh` | SessionStart, PostToolUse | there is unread mail it has not announced |
| `reservations_warn.sh` | PreToolUse (Edit\|Write\|NotebookEdit) | somebody else holds the file you are opening |
| `autoreserve.sh` | PostToolUse (same matcher) | never, on success |
| `session_end.sh` | SessionEnd | never |
| `inbox_watch.sh` | not a hook — a background task | it exits, which is the wake |

`inbox_watch.sh` is run from the agent's own session as a background command.
It blocks on a server-sent-events subscription and exits when mail arrives; a
background task that exits re-invokes the agent, and `SessionStart` then runs
`inbox_check.sh`, which delivers the message. Restart it after each wake — the
line it prints says so. It deliberately does not read the mailbox: doing so
would mark the ids announced and leave the background task as the sole carrier
of a message the dedupe store already believes was delivered.

## Installing

User scope, once per machine, in `~/.claude/settings.json`:

```json
{"hooks": {
  "SessionStart": [{"matcher": "", "hooks": [
    {"type": "command", "command": "\"C:/Program Files/Git/bin/bash.exe\" -c \"'/path/to/repo/scripts/hooks/session_start.sh' || true\""},
    {"type": "command", "command": "\"C:/Program Files/Git/bin/bash.exe\" -c \"'/path/to/repo/scripts/hooks/inbox_check.sh' || true\""}]}],
  "PreToolUse": [{"matcher": "Edit|Write|NotebookEdit", "hooks": [
    {"type": "command", "command": "…/reservations_warn.sh || true"}]}],
  "PostToolUse": [{"matcher": "Edit|Write|NotebookEdit", "hooks": [
    {"type": "command", "command": "…/autoreserve.sh || true"},
    {"type": "command", "command": "…/inbox_check.sh || true"}]}],
  "SessionEnd": [{"matcher": "", "hooks": [
    {"type": "command", "command": "…/session_end.sh || true"}]}]}}
```

Drop the `bash.exe` wrapper off Windows. Keep the `|| true`: a PreToolUse hook
that exits non-zero **blocks the edit**, so a server outage would stop the user
working.

On Windows the wrapper is not a formality, and `|| true` outside it is actively
harmful. Claude Code hands the command to `cmd.exe`, which cannot run a `.sh`
file and does not have `true`. Measured, hook script present in every row:

| command as written | exit | what the user gets |
|---|---|---|
| `/c/…/session_start.sh \|\| true` | **0** | "path not found", then "'true' is not recognized" |
| `C:/…/session_start.sh` | **0** | nothing — Windows opens a *file-association dialog* |
| `"…/bash.exe" -c "'/c/…/session_start.sh' \|\| true"` | 0 | the hook runs |

Every row exits 0, so Claude Code calls all three healthy. The first two never
executed a line of the hook. `|| true` is what converts the failure into a
silent one: it is the right guard **inside** the wrapper, where it absorbs a
server outage, and a mask **outside** it, where it absorbs the hook not existing
as a runnable thing at all. The second row is the worst of the three — it is the
only one that is completely silent, and on a desktop it can pop a GUI dialog at
every edit.

Per-machine settings go in `~/.agent-mail.env`, never in the hook commands and
never in the repository:

```
HTTP_BEARER_TOKEN=…          # required
AGENT_MAIL_URL=https://host  # omit on the machine running the server
```

`chmod 600` on that file is worth attempting and worth not trusting. Under Git
Bash on NTFS the mount carries `noacl`, so POSIX modes are neither read nor
written: `chmod 600` returns 0 and leaves the mode at 644, `chmod 777` also
leaves it at 644, and only the read-only attribute survives (`chmod 400` → 444).
What actually guards the file there is the profile directory's inherited ACL —
check it with `icacls "%USERPROFILE%\.agent-mail.env"` and read the group names,
because anything with `(M)` on your profile can rewrite `settings.json` too, and
that file names the commands every session executes.

Claude Code re-reads `settings.json` without a session restart.

## What silence means

This is the part that cannot be read off the code, and the part that cost four
machines a day to establish. Every one of these hooks is silent on success, so
silence is the normal case — and until recently silence was also what a failure
looked like.

| Hook | Silence means |
|---|---|
| `autoreserve.sh` | the reservation was filed |
| `reservations_warn.sh` | nobody else holds this file |
| `inbox_check.sh` | no new mail **or** the 120 s rate limit cut it short |
| `session_end.sh` | the reservations were released |

Failures now announce themselves — "could not check the inbox … this is NOT 'no
new messages'" and so on — because a server that is down, refusing, or
answering in an unexpected shape used to produce exactly the same silence as a
quiet morning. Two agents editing one file during a deploy window were each
told nothing, and each read that as "no conflict".

Two silences still mean something you cannot see:

- `inbox_check.sh` announces each id once. A message you have been told about
  and have not acted on is not repeated; only the count of older unread is.
- `autoreserve.sh` reserves the file **after** the edit. It cannot warn you
  before, and its reservation replaces whatever reason a deliberate
  `file_reservation_paths` had set. Reserve deliberately, announce, then write
  — in that order — if you want the reason to reach anyone.

## Do not run `scripts/integrate_claude_code.sh`

It installs only the previous-generation `check_inbox.sh` and none of the hooks
above, so a machine set up with it has no reservations, no conflict warnings
and no session lifecycle. Configure `~/.claude/settings.json` by hand instead.

Its handling of secrets was fixed in `21c5588` — before that it wrote the
server bearer and the agent's registration token into a mode-644 file and into
every hook's argv, under a banner reading "no secrets". Nothing needs undoing
if you never ran it; if you did, check `~/.claude/settings.json` for a literal
token and rotate what you find.
