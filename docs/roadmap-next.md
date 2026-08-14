# Next steps

Working notes for whoever picks this up. Ordered by what breaks first if ignored.

## Done

**Malformed request body answered 500, not 400.** A body whose bytes are not
valid UTF-8 made the stateless mount answer `500 Error handling POST request`,
which blames the server for the caller's bytes and names nothing actionable.
`Utf8BodyGuardMiddleware` now answers 400 with the failing byte offset. Plain
ASGI, POST-only, skips streamed and oversized bodies, so the SSE GET is
untouched. Tests in `tests/test_http_utf8_body_guard.py`.

It paid for itself immediately: the 400-with-offset is what let the Windows
machine isolate the real cause — the body crossing argv into native `curl.exe`,
where the console codepage re-encodes it. Fixed separately by sending the body
over stdin.

**Unread mail below the high-water mark was never delivered.** `fetch_inbox`
orders newest-first and *then* applies the limit, so an agent with more unread
than the page size saw only the newest page, stored the top id, and every older
unread message fell below the mark permanently. Verified live: fourteen unread,
ten announced, four never surfaced again even after the ten were read. The
scalar mark is now `{floor, ids}`, enumeration is split from body-fetching, and
what does not fit the render caps is named on one line or counted and held over
rather than dropped. A bare integer on disk migrates as a set of one, so the
first check after the change re-announces everything still unread.

## 1. Instant delivery

`inbox_check.sh` is polling: `SessionStart` always, then `PostToolUse` at most
once per 120s. An *idle* session is never woken, which is precisely the case
that matters — an agent waiting on another agent's answer, or a human saying
"stop".

Design settled (SSE, not WebSocket). WebSocket adds a proxy module and a server
flag for no directional benefit; the channel only ever carries server→client
hints, and SSE is already proven through this Apache/Cloudflare path with the
15s keepalive that also defeats the CDN's ~100s idle timeout.

  * **Route.** A new top-level `GET /events`, outside `/api`, `/mcp` and
    `/mail`, registered *before* the broad MCP mounts and the optional root
    static mount or it will be shadowed.
  * **Auth.** Both layers. The bearer is server-wide, not project-scoped, so
    without a second factor any bearer holder could subscribe as another agent
    and watch their message timing, ids and BCC arrivals. Take the recipient's
    registration token in a header — not a query parameter, which would land in
    proxy and CDN logs. Return the same 401 for unknown project, unknown agent
    and bad token, so the endpoint cannot be used to enumerate identities.
  * **Frames.** Thin: `{kind, project, agent, id}`, `kind: "message"` the only
    v1 kind. The woken agent pulls content through `fetch_inbox`; shipping
    payloads duplicates the mailbox's authorisation model for no gain. No SSE
    `id:` line — that would advertise `Last-Event-ID` replay this does not have.
  * **Fan-out.** One bounded queue *per connection*, keyed `(project, agent)`.
    A single shared queue per agent would load-balance the hint and wake only
    one of two sessions sharing an identity.
  * **Lifecycle.** `: ready` immediately, `: ping` every 15s, then one `data:`
    frame and close, so `curl -N` exits — and a background process that exits
    is already Claude Code's wake mechanism. No monitor runtime needed.
  * **Reconnect.** No replay, no sequence number, no cursor: the mailbox is the
    log. Order is subscribe-first, pull-second — register, send `: ready`, let
    the client catch up through `fetch_inbox` while subscribed, then wait. That
    closes the check-then-subscribe lost-wakeup window, and makes queue overflow
    and disconnected periods harmless.

Emit points: `_deliver_message` (covers sends, replies and the message
`request_contact` creates) and the overseer send path, which bypasses
`_deliver_message` and would otherwise be the one message type that never
notifies. Publish after the archive write succeeds, not merely after DB commit.

Two corrections against reusing the existing notification path: it excludes BCC
(blindness is between recipients, not between server and recipient), and it is
gated on `settings.notifications.enabled` plus a debounce, either of which would
silently swallow a wake.

Reservation grants are **not** an emit point. The grant path grants even when
conflicts exist and extends existing rows on every re-reservation, so
broadcasting it would wake everyone on routine edits, and there is no defined
recipient. Revisit only once a client has a reservation-state pull path;
`fetch_inbox` cannot reconcile one.

## 2. Two sessions on one host share one identity

Implemented: **keep the durable Agent identity and model concurrency as
AgentExecution**, not as more mailbox rows.

The durable address remains `<client>-<os>-<host>-<slot>`. `SessionStart`
creates a root execution under that Agent. Native `SubagentStart` creates a
child using the client's stable `agent_id`; `SubagentStop` records a provisional
stop because another provider hook may continue the child, the next child tool
cancels it, and a later parent event finalizes it. `SessionEnd` ends the root
and descendants. Codex explicitly reports the parent `session_id` on
subagent hooks, so the child lookup is deterministic rather than inferred from
names. Claude Code additionally carries `agent_id` on edit-tool hooks inside
the child.

The local start transaction is also ordered against `SessionEnd`: an end that
arrives while `start_agent_execution` is in flight records `end_requested`.
When the server UUID returns, the hook uses it only to authenticate the matching
end call and never publishes an active execution or guard marker for the ended
native lifecycle. SessionEnd first persists a client/session tombstone before
enumerating projects. Concurrent first-touch enrollment checks that barrier
before local state and after the start RPC, so a project absent from the scan
cannot outlive the native session.

Reservations now carry `execution_id`. Conflict self-suppression and release
therefore mean "this execution", not "anything using this host mailbox".
Ending an execution releases only its automatic claims atomically; explicit
claims retain their TTL and require explicit release. The earlier per-session
path-log proposal is no longer the ownership model.

The per-worktree guard marker follows the execution that most recently proved
activity in that checkout. `SubagentStart` alone does not displace the root;
the first successful child heartbeat/automatic claim publishes the child plus
ancestor chain, and the next parent event restores the root. Invalid or stale
markers warn and continue during observe/warn rollout, but fail closed in
execution-enforce mode.

Cross-project attribution follows tool-defined file metadata. Claude starts the
target execution chain before autoreserving the edited path. Codex PostToolUse
does the same lazy start and heartbeat when `file_path` or `notebook_path` is
present, without filing a claim and without guessing from `turn_id`. SessionEnd
fans out exact per-project ends concurrently and reconciles each successful
result independently, keeping the multi-project path within the provider's
bounded hook deadline.

Cross-project edits establish the same deterministic durable host-slot Agent
and a separate execution chain in the target project. Session teardown ends all
exact roots matching the native client/session pair across touched projects.

Lifecycle hooks record `cwd`, repository root, Git common directory, worktree
path, branch, and HEAD as execution context. A worktree is not a Project or an
Agent, and this change deliberately does not turn on `WORKTREES_ENABLED` or
allocate a worktree for every read-only subagent. Orchestration may create an
isolated worktree for concurrent writers when their edit surfaces require it.

## 3. Display names

Keep the derived `<host>-<platform>-N` as the immutable key; add an alias for
display only. `Agent.display_name` (nullable, 128) plus one line in the
idempotent migration list in `db.py`.

**The alias must never be accepted in `to:`.** Name resolution is not one
function — it is a dozen `func.lower(Agent.name) == ...` sites in the hottest
part of `app.py`, and a partial fallback makes behaviour differ per call path.
It would also make a mutable field load-bearing, which is the exact thing
keeping the derived key immutable was meant to prevent. And `_looks_like_model_name`
is a substring test, so plausible aliases ("Opus Box", "Gemini-Rig") would be
silently mangled by name validation that display-only aliases never touch.

Reject at set time an alias equal to any agent's key in the project or to
another agent's alias. Render `alias (key)` everywhere, never the alias alone —
the key is what must be typed into `to:`.

Do **not** build this on `WindowIdentity`. It looks like a fit, but its uuid
comes from the *server process's* own environment at config load, so on a shared
remote server every caller resolves to one window identity or none. It is a
local-stdio window-history feature and is never accepted as authentication
proof. New sessions authenticate with a registration token; only the current
session's existing Agent binding may omit it.

Smallest shape: model field + migration line, `_agent_to_dict` emits it (its ten
call sites cover register, whois, contacts, the agents resource and the archive
profile), a new `set_agent_display_name` tool authenticated by the registration
token the agent already holds — a new tool rather than a kwarg on
`register_agent`, which upstream changes often — and `a.display_name` added to
the reservations endpoint's SELECT.

Worth saying plainly: `AGENT_MAIL_AGENT` in `~/.agent-mail.env` already buys a
human-readable name today for zero server cost. The catch is that it *is* the
key, so changing it later orphans the mailbox, the credential and every
reservation. The column earns its keep only for renaming after the fact, and for
names that cannot be keys.

## Still open, server side

`_authenticate_agent` never refreshes `last_active_ts`, so an agent that only
files reservations looks dead after `FILE_RESERVATION_INACTIVITY_SECONDS` and
has its holds swept. `register_agent` does not clear `retired_at` when an
existing agent re-registers with a valid token.

## Sequencing

1 is the user's stated priority and its identity dependency is now resolved by
2's decision to change nothing. The client half of 2 is a single hook file and
can land any time. 3 is additive and touches nothing the others depend on.
