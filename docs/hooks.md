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
| `session_start.sh` | SessionStart, SubagentStart | identity/execution context |
| `inbox_check.sh` | SessionStart, PostToolUse | there is unread mail it has not announced |
| `reservations_warn.sh` | PreToolUse (Edit\|Write\|NotebookEdit) | somebody else holds the file you are opening |
| `autoreserve.sh` | PostToolUse (same matcher) | never, on success |
| `session_end.sh` | SubagentStop, SessionEnd | never |
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

**The plugin runs a copy, not your working tree.** Installing from a local
directory leaves a full snapshot under
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` — a real directory
with no `.git`, measured on macOS and WSL alike. The commit that added
`.claude-plugin/marketplace.json` claims a local install "points at the working
tree itself"; that is wrong, and the drift it says has nowhere to happen simply
moved: `integrate_claude_code.sh --yes` refreshes the copies under
`~/.claude/hooks/`; the plugin cache needs `plugin uninstall` followed by
`plugin install`. `plugin update` will not do it — at an unchanged `version` it
answers "already at the latest version" and leaves the snapshot pinned to the
commit it was installed from, measured on Windows and WSL. Reinstalling does
not need a `version` bump.

Which copy actually executes has differed between machines — on WSL both the
skill and the monitor ran from the repository path while a stale cache copy sat
beside them — so read the path out of `pgrep -af inbox_watch_monitor` instead
of assuming either.

The bare `.sh` in the manifest needs no interpreter of its own: the host
resolves one, including on Windows, where it starts the command through Git for
Windows' bash exactly as the installer does for every hook —

```
"C:\Program Files\Git\bin\bash.exe" -c "… <plugin root>/scripts/hooks/inbox_watch_monitor.sh claude 1"
```

— measured with the monitor running there. No file association is involved. An
earlier revision of this file claimed the opposite; that came from probing the
path through `cmd /c`, which is not how the host starts it.

**Freshness is decided at arming time and never rechecked.** The running
process holds the inode it started with, so a `git pull` that rewrites the
script leaves the monitor executing the old code with nothing to show for it —
a cache copy at least records the commit it came from, a live process records
nothing. Re-arm after pulling.

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

Installed Git guards are deliberately standalone programs. They read the
short-lived process signals `AGENT_NAME`, `AGENT_EXECUTION_ID`,
`AGENT_EXECUTION_ANCESTOR_IDS`, `AGENT_EXECUTION_MARKER_MAX_AGE_SECONDS`, and
`AGENT_EXECUTION_ENFORCEMENT_MODE` directly from the committing shell because
they must run without importing the server, Python environment, or `.env`.
Server configuration remains exclusively owned by `python-decouple`; these
guard-only runtime signals are the explicit exception.

User scope, once per machine, in `~/.claude/settings.json`:

```json
{"hooks": {
  "SessionStart": [{"matcher": "", "hooks": [
    {"type": "command", "command": "\"C:/Program Files/Git/bin/bash.exe\" -c \"'/c/Users/you/.claude/hooks/mcp-agent-mail/session_start.sh' || true\""},
    {"type": "command", "command": "\"C:/Program Files/Git/bin/bash.exe\" -c \"'/c/Users/you/.claude/hooks/mcp-agent-mail/inbox_check.sh' || true\""}]}],
  "SubagentStart": [{"matcher": "", "hooks": [
    {"type": "command", "command": "…/session_start.sh || true"}]}],
  "SubagentStop": [{"matcher": "", "hooks": [
    {"type": "command", "command": "…/session_end.sh || true"}]}],
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
| `session_end.sh` | root automatic reservations were released, or a child stop was recorded provisionally |

Failures now announce themselves — "could not check the inbox … this is NOT 'no
new messages'" and so on — because a server that is down, refusing, or
answering in an unexpected shape used to produce exactly the same silence as a
quiet morning. Two agents editing one file during a deploy window were each
told nothing, and each read that as "no conflict".

Two silences still mean something you cannot see:

- `inbox_check.sh` announces each id once per root/child execution. A child's
  `.seen` state cannot consume the root's announcement. A message one execution
  has been told about and has not acted on is not repeated there; only the count
  of older unread is.
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

When an origin exists, the global hook derives and sends its canonical
synthetic `/owner/repo` key; it does not pass a checkout path through the local
marker-precedence resolver. Marker/discovery opt-in is therefore the activation
gate for a new checkout, while path-based CLI/MCP inspection is the surface
that resolves marker precedence and persists `project_uid`.

The durable Agent is not the execution identity. After registration,
`SessionStart` calls `start_agent_execution` and stores the returned UUID in a
private, per-project/per-session state record under `AGENT_MAIL_STATE_DIR`.
Repeated start events for an active native session reuse that record. After
`SessionEnd`, a fresh provider id starts generation one of another lifecycle;
Codex alone can genuinely resume the same id and then advances its private run
generation without creating a new Agent. The first root execution
`external_id` is the native `session_id`; a resumed Codex run uses the bounded
`<session_id>#run-N` form. A child uses its native `agent_id`; hooks never
synthesize an Agent name.
`SessionEnd` calls `end_agent_execution`, whose server transaction ends the
execution and releases only its automatic reservations. Explicit reservations
keep their TTL and require an explicit release.

The current [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
documents `agent_id` and `agent_type` on `SubagentStart`, and those fields plus
`stop_hook_active`, `agent_transcript_path`, and `last_assistant_message` on
`SubagentStop`. It also includes `agent_id` and `agent_type` on tool hooks
executed inside a subagent. The start hook therefore creates a child execution
beneath the root;
autoreserve sends that child `execution_id`. Claude explicitly permits another
matching `SubagentStop` hook to return `decision: "block"` and keep the child
running, so this hook records only a provisional stop. Any later child tool
event cancels it; the first parent event after the child really returns ends the
execution. A
subagent never registers a mailbox. The recorded `cwd`, repository root, Git
common directory, worktree path, branch, and HEAD describe where that execution
ran; none participates in Agent identity. The integration does not enable
`WORKTREES_ENABLED` or create worktrees by itself.

Physical placement is likewise not identity. A linked worktree beside the
checkout, under `/tmp`, or below a nested directory resolves to the same
Project and the same repository-relative reservation paths. Prefer a durable
sibling such as `~/worktrees/<repo>/<task>` for ordinary work and `/tmp` for
disposable gates. Git permits `repo/.worktrees/<task>`, but the parent checkout
then sees `.worktrees/` as untracked and a broad `git add` can stage an embedded
gitlink unless the directory is explicitly ignored; nested worktrees are
therefore supported by the lifecycle code but are not the recommended default.

Before calling the server, the hook generates a 256-bit capability and writes
it directly to a final `*.json.token` file beside the private metadata state,
with mode `0600`. A retry after a
process/network failure reuses exactly that token; the server stores only its
hash. The token is never copied to hook output, a temporary file, or Git
metadata. Every lifecycle call declares `lifecycle_protocol_version: 1`.
This capability prevents accidental cross-execution mutation; it is not an
adversarial same-user boundary. Codex and Claude intentionally share the WSL
user's private state, and any process running as that user can read it. No
per-client Windows ACL isolation is installed.
`AGENT_MAIL_STATE_DIR` must be an absolute path outside every Git worktree and
Git directory. Hooks reject even a not-yet-created state path whose nearest
existing ancestor is inside Git, so registration and execution capabilities
cannot become stageable. A lock left empty by a crash between directory
creation and PID publication is reclaimed after a bounded one-second grace.
The start RPC runs without holding the state lock. If `SessionEnd` arrives while
that RPC is in flight, it atomically changes local `starting` state to
`end_requested`. The start response may then persist its returned UUID only for
the authenticated `end_agent_execution` call: it never publishes `active` or an
active guard marker. A failed cleanup remains `end_requested` with the same
capability so an exact SessionEnd retry can finish it; it cannot be replayed as
a new native lifecycle. Before enumerating its exact lifecycle manifest,
`SessionEnd` also writes a private tombstone keyed by the client and native
`session_id`. Manifest enrollment and that barrier share the tombstone lock, so
a concurrent first-touch hook is either included in the exact
client/session/generation manifest or rejected before it can create state.
Hooks never scan unrelated execution records. Starts check the tombstone again
after the unlocked RPC; a raced server response is ended with its original
capability and is never published to the guard marker.

After a terminal server result is confirmed, the hook destroys the raw
execution token and heartbeat throttle stamp immediately, then rewrites the
execution state as a compact non-secret terminal record. That record, its exact
manifest, and the lifecycle tombstone remain resumable/auditable for
`AGENT_MAIL_EXECUTION_RESUME_HORIZON_SECONDS` (30 days by default, with a
one-day minimum). Date-bucketed retention work removes expired records only
after revalidating their exact generation under the existing locks and handles
at most `AGENT_MAIL_EXECUTION_RETENTION_GC_BATCH` markers per hook invocation
(64 by default, capped at 256). A resumed newer generation always preserves its
shared tombstone.

The root start also writes a private guard handoff at the path returned by
`git rev-parse --path-format=absolute --git-path agent-mail/execution-id`. Its JSON shape is
`{"execution_id":"<uuid>","status":"active","kind":"session|subagent","worktree_path":"...","heartbeat_ts":"<UTC-ISO-8601>","ancestor_execution_ids":[]}`.
This marker is a short-lived guard hint, never durable identity: recreating a
linked worktree can change its physical Git-dir path, and every start still
revalidates the native execution id and capability with the server.
A child overwrites that marker at start only when it runs in a different
worktree; a same-checkout `SubagentStart` leaves the root marker intact. The
child's first successful heartbeat or automatic reservation then hands the
marker to the exact child execution (with its root ancestry); the next parent
event restores the root marker. Thus a read-only child does not perturb commit
context, while a writer's own child claim is never misreported as a sibling
conflict. A confirmed end retains the
marker as terminal audit/handoff state; the server has released automatic
claims, while explicit claims retain their TTL. A provisional child stop keeps
the marker active until a parent event confirms the return. Failed server
validation marks it `unverified`; a later successful heartbeat or claim can
restore it. The next start overwrites it atomically. Successful heartbeats
refresh `heartbeat_ts`; a confirmed end changes `status` to the terminal
execution state. Guards
prefer `AGENT_EXECUTION_ID` when explicitly exported and may take its lineage
from comma-separated `AGENT_EXECUTION_ANCESTOR_IDS`; otherwise they read this
marker. An active marker is accepted only while its heartbeat age is at most
`AGENT_EXECUTION_MARKER_MAX_AGE_SECONDS` (default `1800`; deliberately stricter
than the server's 24-hour expiry threshold). Old plain-UUID markers have no
freshness proof and are not safe for self-suppression. Timestamps more than five
minutes in the future also fail closed for self-suppression, so clock corruption
cannot make a marker immortal. During observe/warn rollout an invalid marker is
reported and the Git operation continues;
`AGENT_EXECUTION_ENFORCEMENT_MODE=enforce` blocks until an active marker is
restored.

## Codex lifecycle integration

`scripts/integrate_codex_cli.sh` respects an explicit `CODEX_HOME` and otherwise
uses `~/.codex`. It installs:

- the authenticated MCP server in `${CODEX_HOME:-~/.codex}/config.toml`
- the runtime and wrapper in `${CODEX_HOME:-~/.codex}/hooks/mcp-agent-mail`
- `SessionStart`, `SubagentStart`, `SubagentStop`, `PostToolUse`, `Stop`, and `SessionEnd` in
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
Agent row. It also starts the root execution. Codex may emit `SessionEnd` after
an open conversation has been idle and later resume the same `session_id`; the
private lifecycle barrier therefore retains an integer generation. A genuine
`SessionStart(source=resume)` advances an ended generation and starts a distinct
server execution (`<session_id>#run-N`) without removing the prior end barrier.
An old in-flight start is generation-checked after its RPC and cannot publish or
end the resumed generation. Codex documents that subagent
hooks carry the parent `session_id` plus a native `agent_id` and `agent_type`;
`SubagentStart` creates a child execution using those fields, `turn_id`, the
active model and permission mode, plus the Git metadata captured locally, and
`SubagentStop` records the same provisional two-phase stop used for Claude,
because Codex also permits another matching stop hook to continue the child.
Both stop events emit valid JSON, and neither path calls `register_agent`. A
broad `PostToolUse` handler reconciles provisional children and performs a
locally rate-limited asynchronous execution heartbeat (at most once per 60 seconds), resolving the
child when the payload provides `agent_id` and otherwise the root. When that
tool's input provides `file_path` or `notebook_path` in another opted-in Git
project, the hook lazily establishes the same durable Codex mailbox and native
root/child execution chain in that target project, then heartbeats it. This is
lifecycle attribution only: it does not infer ownership from `turn_id` and does
not file an automatic reservation. Codex documents `cwd` as the session working
directory, not a per-tool target. If a hook event actually arrives with another
opted-in repository as `cwd`, that repository is enrolled; otherwise
`apply_patch` and `Bash` expose only an opaque `command`, which is deliberately
not parsed for paths. The first such ambiguous event in each lifecycle emits a
warning: cross-repository writes must run with that repository as the session
working directory or be coordinated and reserved explicitly. `Stop` also
heartbeats the root on its existing polling cadence before checking mail. This
keeps long read-only sessions alive without tying liveness to reservations.
`Stop` checks unread mail no more than once
per 120 seconds, always emits valid JSON, and creates a continuation only for
message ids that are both newly observed and high/urgent. A repeated urgent
message or ordinary unread mail is a UI `systemMessage`, avoiding continuation
loops. `SessionEnd` resolves private client/session state before Git project
discovery or opt-in checks, so a non-Git payload cwd or removed opt-in marker
cannot skip cleanup. It has the documented three-second ceiling and ends the root
execution for that lifecycle; the server atomically cascades the end through
its descendants. Ending an execution atomically releases its own automatic
reservations and cannot strip a sibling execution's holds;
explicit reservations retain their TTL until explicitly released. The Codex
integration does not install autoreserve. Its hook calls use the stateless
`/api/` mount, while ordinary MCP tool calls use a separate session; therefore a
manual Codex reservation cannot inherit the hook's execution binding or private
capability. Until a secure handoff or broker is implemented, automatic
execution-owned claims are supported by the Claude edit hook, not by Codex's
ordinary MCP session.

Claude's existing broad `PostToolUse` inbox hook performs the same bounded
heartbeat after its rate gate. Its edit autoreserve carries `execution_id`, and
a successful reserve/renew also refreshes execution liveness server-side. If a
heartbeat fails, hooks fail open and retry after the local interval. If an
execution expires inside the same active lifecycle generation, ordinary tool
events do not replay it as a new execution. A genuinely resumed Codex
`SessionStart(source=resume)` advances the retained generation and uses
`<session_id>#run-N`; otherwise the provider must emit a new `session_id` or
`agent_id`, producing a new immutable audit lifetime.

An edit can target a file in another Git project. Claude autoreserve uses the
file's canonical project, deterministically establishes the same durable
host-slot mailbox there, and starts the root/child execution chain before
claiming the path. Codex's broad PostToolUse hook performs the same lazy
execution setup and heartbeat when the tool input exposes the target file, but
does not claim it because the Codex integration has no autoreserve hook.
`SessionEnd` scans private lifecycle state and ends every exact root execution
for that native `session_id` and client, across all touched projects; it cannot
end another client's execution. Those independent authenticated end calls run
in parallel and are joined once, so two slow project endpoints remain inside
Codex's three-second SessionEnd ceiling; each successful result reconciles only
its own project's descendant state.

The server reaper runs from the FastMCP lifespan for stdio and HTTP transports.
`AGENT_EXECUTION_REAPER_ENABLED=true` scans every
`AGENT_EXECUTION_REAPER_INTERVAL_SECONDS=60` and expires executions whose last
activity is older than `AGENT_EXECUTION_REAPER_THRESHOLD_SECONDS=86400` (24
hours). Hook heartbeat attempts are locally rate-limited to at most once per 60
seconds but remain event-driven; a shorter server threshold is safe only for a
client that guarantees timer-driven heartbeats. The local 30-minute marker is
stricter so guards fail closed after a workstation sleep long before the server
reaper cleans up the execution.

If the private execution state (and therefore its capability) is lost, hooks do
not invent a replacement execution for the same provider id. Stop the affected
provider lifecycle and start a new one. Any remaining explicit reservation
keeps its TTL and can be inspected and, once the server's inactivity heuristics
consider it abandoned, cleared with `force_release_file_reservation`; automatic
claims are released by normal end/expiry handling.

Every hook has a POSIX Bash command and a `commandWindows` beginning with a
concrete Git for Windows `bash.exe`. When `CODEX_HOME` is shared from WSL under
`/mnt/<drive>`, the installer translates the wrapper path back to Windows. This
keeps the Windows desktop out of the WSL launcher while allowing the wrapper,
once inside Git Bash, to invoke ordinary `bash`.

Codex currently exposes lifecycle hooks, not Claude-style managed monitor
processes. The integration therefore does not auto-spawn a daemon. Mail is
delivered at the next lifecycle boundary; a completed background terminal does
not start a new Codex turn by itself.

All shipped bootstrap integrators request deterministic
`<client>-<os>-<host>-<slot>` mailbox names, including Gemini, Factory Droid,
Cursor, Cline, Windsurf, and OpenCode. None relies on strict-mode-incompatible
adjective+noun generation or the local `$USER` name.

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
forces another turn. `SessionEnd` ends the root execution and releases only its
automatic reservations; it is normally a local release no-op because this
integration does not install autoreserve.

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
