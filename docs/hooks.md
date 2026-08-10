# The Claude Code hooks

Five hooks plus a watcher, installed once per machine, working in repositories
that explicitly opt in or already have private Agent Mail state. They are what
makes the mailbox reach an agent that is busy, and the reservations reach an
agent about to edit a file somebody else is already in.

`scripts/integrate_claude_code.sh` installs this set once under
`~/.claude/hooks/mcp-agent-mail` and merges the hook entries into the user-level
`~/.claude/settings.json`. It does not create a project's `.claude` directory.

## What each one does

| Hook | Event | Speaks when |
|---|---|---|
| `session_start.sh` | SessionStart | always: identity, and who else holds what |
| `inbox_check.sh` | SessionStart, PostToolUse | there is unread mail it has not announced |
| `reservations_warn.sh` | PreToolUse (Edit\|Write\|NotebookEdit) | somebody else holds the file you are opening |
| `autoreserve.sh` | PostToolUse (same matcher) | never, on success |
| `session_end.sh` | SessionEnd | never |
| `inbox_watch.sh` | not a hook — a Claude background task | it exits, which is the Claude wake |
| `inbox_watch_monitor.sh` | not a hook — a Claude plugin monitor | one line per message; silence otherwise |

`inbox_watch.sh <client> <slot>` binds the subscription to the identity
established by that client's SessionStart; it never guesses between multiple
identities on the same host. Today only Claude installs and advertises it,
because Claude's tracked background task completion is the wake transport.
Codex has no idle-turn wake for a completed terminal, while Copilot requires a
separate `notification: shell_completed` bridge. Do not present the watcher as
instant delivery on those clients until that bridge exists.

Run the printed command from the same repository session. It blocks on a
server-sent-events subscription and exits when mail arrives; Claude then runs
`inbox_check.sh`, which delivers the message. Restart it after each wake using
the exact client-and-slot command it prints. It deliberately does not read the
mailbox: doing so would mark the ids announced and leave the background task as
the sole carrier of a message the dedupe store already believes was delivered.

### The night monitor

`inbox_watch_monitor.sh <client> <slot>` is the same subscription with the
opposite wake contract: the wake is **a line on stdout**, and exiting is death.
It is declared as a Claude Code plugin monitor in `.claude-plugin/plugin.json`
and armed on demand with `/wake`, the bundled skill.

Use it for a session about to be left unattended. During the day the hooks above
already deliver on every turn boundary and a person at the console can ask; the
monitor exists for the hours when nobody is there to re-arm anything.

The difference is not the delivery — receiving a message costs the same turn
either way — but the two edges around it:

| | `inbox_watch.sh` | `inbox_watch_monitor.sh` |
|---|---|---|
| quiet mailbox | wakes the agent every `AGENT_MAIL_WATCH_SECONDS` (1800 s) to report nothing | prints nothing, ever |
| after a message | exits; somebody must re-arm it | reconnects by itself |
| chain of custody | one link per wake, ~16 a night | one link per session |

`GET /events` is one-shot by design — `: ready`, `: ping` every 15 s, exactly one
`data:` frame, then the connection closes — so "never exits" is a reconnect loop
rather than one held connection. Concurrent subscriptions for the same agent are
supported server-side, so the catch-up query after each reconnect closes the gap
between connections without risk of eviction. `session_start.sh` suppresses the
manual watcher invitation while a monitor's pid file shows a live process, so the
two are not armed at once.

A monitor that exits is never restarted for the life of the CLI process, and it
is not restored when a session resumes. If the CLI restarts overnight, instant
delivery stays off until someone runs `/wake` again — the lifecycle hooks still
deliver on the next turn, so mail is delayed rather than lost.

## Installing

The integrator requires Bash, `jq`, `git`, and `curl`; client installation and
hook execution require no Python, `uv`, Node.js, or virtual environment. On
Windows, use Git for Windows Bash and install `jq` explicitly if `jq --version`
is unavailable; Git Bash itself does not guarantee it. Verify `curl --version`
and `jq --version` in that same shell before installing. The generated Codex
`commandWindows` also points directly to Git for Windows `bash.exe`.

User scope, once per machine, in `~/.claude/settings.json`:

```json
{"hooks": {
  "SessionStart": [{"matcher": "", "hooks": [
    {"type": "command", "command": "\"C:/Program Files/Git/bin/bash.exe\" -c \"'/c/Users/you/.claude/hooks/mcp-agent-mail/session_start.sh' || true\""},
    {"type": "command", "command": "\"C:/Program Files/Git/bin/bash.exe\" -c \"'/c/Users/you/.claude/hooks/mcp-agent-mail/inbox_check.sh' || true\""}]}],
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
AGENT_MAIL_URL=https://host/mcp/
AGENT_MAIL_STATE_DIR=/optional/private/state/path
```

That shared file is deliberately limited to the endpoint, principal bearer and
optional state directory. Client, slot, project, agent name and registration
token do not belong there. The installer removes those legacy identity keys,
preserves comments and unrelated operator settings, and writes a private backup
under the Agent Mail state directory before an atomic merge.

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

## Reading what the server says back

Four of us hit these separately in one day, and every one of them looks like
silence rather than like a mistake.

**`id` is not where it looks.** `send_message` and `reply_message` return it at
`.deliveries[0].payload.id`; `.id` at the top level is null on a perfectly
successful send. Filtering on `.id` and finding nothing has already caused one
duplicate message — the sender read the empty result as "it did not go".

**`rc=2` with an empty body is the server being down, not refusing you.** The
exit-code contract says 2 means a non-2xx answer, and a deploy window produces
exactly that with nothing in the body — indistinguishable from an auth refusal
until you ask the server itself. `curl` the MCP endpoint and read the status:
502 is a window, 401 is your credentials. Do not resend on `rc=2` without
checking whether the first attempt landed; during a window it usually did not,
but "usually" is not a reason to send twice.

**An empty result is now `[]`, not silence.** Until `848b1c3`, a tool that
succeeded and matched nothing returned no output at all, because `am_call` read
`.result.content[0].text` and there was no element nought — so "no contacts" and
"the call failed" printed the same thing, and one of us spent the day adding a
second call to every inbox check to tell them apart. It now falls back to
`structuredContent`, which the server was sending all along. `rc` alone
separates the remaining cases: `rc=0` with `[]` is an empty answer, `rc=2` with
an empty body is a deploy window.

**A filter that does not match looks exactly like a response that is empty.**
The reservation call returns granted holds under `granted`, not `reservations`,
and conflicts under `conflicts`. A `jq` path that misses prints nothing, which
reads as "no conflicts" — the one answer you must not get wrong. Print the raw
response the first time you write a call, and only then narrow it.

**Marking your own sent message read returns `read:false`.** That is correct,
not a failure: you are not its addressee. Anything counting acknowledgements
should not treat it as an error.

## `scripts/integrate_claude_code.sh`

The integrator is user-scope only:

- hook scripts: `~/.claude/hooks/mcp-agent-mail`
- hook definitions: `~/.claude/settings.json`
- authenticated MCP server: Claude user scope in `~/.claude.json`, preferably
  written through `claude mcp add --scope user`

Re-running it migrates old managed commands that point into a project's
`.claude/hooks` directory, then installs one canonical global set. It filters
individual managed commands, so an unrelated command in the same hook group is
preserved. No project `.mcp.json`, `.claude/settings.local.json`, ignore rule or
server-launch helper is created.

`SessionStart` derives the repository and the Claude client slot, then applies a
local activation gate before any server request. Existing credentials or a
current/legacy granted-name file activate a known project. A new project must
carry a non-empty `.agent-mail-project-id` or an `.agent-mail.yaml` declaring
`project_uid:`. Only then does the hook register the agent and store its
per-agent registration token in the private `credentials.json` state store. The
registration token is never copied into `~/.agent-mail.env`.

## Codex lifecycle integration

`scripts/integrate_codex_cli.sh` respects an explicit `CODEX_HOME` and otherwise
uses `~/.codex`. It installs:

- the authenticated MCP server in `${CODEX_HOME:-~/.codex}/config.toml`
- the runtime and wrapper in `${CODEX_HOME:-~/.codex}/hooks/mcp-agent-mail`
- `SessionStart`, `Stop`, and `SessionEnd` in
  `${CODEX_HOME:-~/.codex}/hooks.json`

The JSON merge removes only prior Agent Mail handlers and preserves unrelated
handlers, including a foreign command in the same matcher group. The TOML merge
likewise removes only the old Agent Mail top-level `notify`; an unrelated
`notify` command survives. Codex requires the exact hash of non-managed command
hooks to be reviewed, so open `/hooks` after installation or after any hook
update and trust the displayed user-level definitions.
The wire formats and trust behavior follow the current
[Codex hooks reference](https://learn.chatgpt.com/docs/hooks).

`SessionStart` establishes the repository-scoped identity only after the same
local activation gate and contributes the unread count as developer context.
An unactivated repository makes no Agent Mail request and creates no project or
Agent row. `Stop` checks unread mail no more than once
per 120 seconds, always emits valid JSON, and creates a continuation only for
message ids that are both newly observed and high/urgent. A repeated urgent
message or ordinary unread mail is a UI `systemMessage`, avoiding continuation
loops. `SessionEnd` has the documented three-second ceiling and only releases
paths recorded for that exact session; because the Codex integration does not
install autoreserve, it is normally a local no-op and never performs a wholesale
release for the shared slot identity.

Every hook has a POSIX Bash command and a `commandWindows` beginning with a
concrete Git for Windows `bash.exe`. When `CODEX_HOME` is shared from WSL under
`/mnt/<drive>`, the installer translates the wrapper path back to Windows. This
keeps the Windows desktop out of the WSL launcher while allowing the wrapper,
once inside Git Bash, to invoke ordinary `bash`.

Codex currently exposes lifecycle hooks, not Claude-style managed monitor
processes. The integration therefore does not auto-spawn a daemon. Mail is
delivered at the next lifecycle boundary; a completed background terminal does
not start a new Codex turn by itself.

## GitHub Copilot CLI lifecycle integration

`scripts/integrate_github_copilot.sh` configures both Copilot CLI and VS Code at
user scope. It respects `COPILOT_HOME` and otherwise writes:

- the authenticated remote server to `~/.copilot/mcp-config.json`
- the managed user hook file to `~/.copilot/hooks/mcp-agent-mail.json`
- the runtime and wrapper below `~/.copilot/hooks/mcp-agent-mail/`
- the same authenticated server to VS Code's platform-specific user `mcp.json`

The Copilot MCP entry uses the native `mcpServers` schema, HTTP transport and
`tools: ["*"]`. The hook file uses schema version 1 and the PascalCase
`SessionStart`, `Stop` and `SessionEnd` events, which select Copilot's
VS Code-compatible snake_case payload. Every entry supplies `bash`,
`powershell` and `timeoutSec`; the Windows command invokes a concrete Git for
Windows `bash.exe`. Native Git Bash installs use the current Bash executable,
including per-user and custom installations. For a custom Git for Windows path
when installing a Windows-shared profile from WSL, set the install-time-only
`AGENT_MAIL_GIT_BASH_PATH` to an absolute Windows or `/mnt/<drive>` path. The
installer preserves foreign servers, top-level fields, hook events and handlers,
and replaces only its own wrapper commands when run again.

The wrapper supplies the closed `copilot` client token and the selected
`AGENT_MAIL_COPILOT_SLOT` (default `1`). `SessionStart` uses the same local
activation and legacy-migration gates as Codex before any network request, then
injects identity and unread counts through Copilot's direct `additionalContext`
output. `Stop` checks at most once per 120 seconds and returns `decision: block`
only for newly observed high/urgent message ids. Ordinary unread mail never
forces another turn. `SessionEnd` releases only session-recorded paths and is
normally a local no-op because this integration does not install autoreserve.

Copilot CLI loads user hook files only at startup, so restart the CLI after
installing or changing them. There is deliberately no automatically spawned
watcher or daemon. A future instant-delivery bridge must use Copilot's
`notification` hook for `shell_completed`; watcher exit alone does not inject a
new idle turn. The contracts are documented in GitHub's
[hooks guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-hooks),
[hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference),
and [CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference).

## Identity migration is fail-closed

Messages, contacts, and reservations follow the server's `Agent.id`. Creating a
new `<client>-<os>-<host>-<slot>` row before migrating an existing
`<host>-<os>-<slot>` identity would leave the old history attached to the
old row.

Claude, Codex, and Copilot CLI SessionStart inspect the private credential store
and the legacy project-only granted-name entry before any network call. If that
state proves a legacy identity exists and the client-scoped credential is not
already usable, the hook prints the exact old and new names and stops. It does
not call `ensure_project`, register a second Agent, copy a token, or rewrite
either local state file.

The operator sequence is deliberately manual:

1. Rename the existing server Agent row in place, preserving `Agent.id` and its
   registration token.
2. Move the corresponding key in private `credentials.json` and write the
   client/slot-specific granted-name entry.
3. Restart the client and let SessionStart use the migrated identity.

Only evidence produced by the old integrations is treated as legacy: the old
project-only granted-name file or a credential key matching the locally derived
`<host>-<os>-<positive-slot>` form. The hook does not guess remote state.
