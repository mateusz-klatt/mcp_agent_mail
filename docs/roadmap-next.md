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

Decided: **keep the identity model as it is.** Not a session discriminator.

A session-suffixed identity (`<host>-<platform>-<hex of session_id>`) covers
nothing actually run here — same-host different-project is already isolated end
to end by `(project_id, name)` uniqueness — and does not cover the case that
does collide, ultracode fan-out, because subagents share one `session_id`.
Against that it would break the property the fleet most depends on: identity
outliving the process, which is the only reason mail sent to an absent machine
is picked up at its next `SessionStart`. It would also commit one agent
directory and one archive commit per terminal, forever.

The real defect is narrower and has a client-side fix.
`reservations_warn.sh` suppresses on *"is the holder me?"* when it means
*"did this session reserve it?"* — and the per-session path log already on disk
answers the second question. Warn when the holder is another agent, or when it
is our own name with no matching entry in this session's log, wording that case
as "reserved under this host's identity by another session". `session_start.sh`
carries the same self-filter and wants the same treatment as a separate line.

Then, for ultracode: key the session log `<session_id>.<agent_id>` when the
payload carries one and widen the `am_session_logs` glob to `"${slug}*__*.list"`
so `SessionEnd`, which only ever sees `session_id`, still finds every subagent's
log. Backward compatible. **Verify first** that `agent_id` is stable per
subagent rather than per tool call — if it churns, the log fragments and every
second edit warns spuriously.

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
local-stdio single-user feature.

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
