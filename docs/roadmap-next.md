# Next steps

Working notes for whoever picks this up. Ordered by what breaks first if ignored.

## 1. Malformed request body answers 500, not 400

Reproduced: a body whose bytes are not valid UTF-8 (a legacy codepage, which is
what Git Bash on a non-English Windows sends) makes the stateless mount answer
`500 Error handling POST request`. Valid UTF-8 and `\uXXXX`-escaped bodies both
answer 200 — the server decodes non-ASCII correctly, so this is not a
non-ASCII problem, it is an *invalid encoding* problem.

The message comes from `mcp/server/streamable_http.py:650` (the SDK), not from
this repo. The client-side mitigation is already in: `am_call` emits ASCII-only
via `jq -a`, which cannot be mis-encoded.

What is still worth doing here: a small middleware that rejects an undecodable
body with 400 and a message saying so. A 500 tells the caller "the server
broke" when the truth is "you sent bytes I cannot read", and in `am_call` it
surfaces as an empty result — indistinguishable from "nothing to report". That
class of silent failure has cost more time on this project than any other.

Also worth a regression test asserting `POST /api/` accepts a UTF-8 body.

## 2. Two sessions on one host share one identity

Verified: `AM_SESSION_ID` *is* assigned where it is needed (`autoreserve.sh`,
`session_end.sh` both read `.session_id` from the payload), and the per-session
path log is keyed correctly — a log written by session `A` is named for `A`.
So `SessionEnd` releasing another session's paths is NOT a live defect.

The real gap is elsewhere: `reservations_warn.sh` filters out reservations held
by *this agent name*, and two concurrent CLIs on one host share that name. So a
second terminal editing a file the first one holds is told nothing. The warning
is suppressed exactly when it would be useful.

Server-side, reservations belong to an agent, not a session, so this cannot be
fully fixed client-side by filtering alone.

Options, in increasing cost:
  a. leave it — accept that same-host sessions do not warn each other;
  b. include a session discriminator in the identity (`<host>-<platform>-<8 hex
     of session_id>`), making every terminal a separate addressable agent;
  c. keep the durable identity and add a session field server-side.

(b) also gives what was asked for separately: the ability to DM one specific
terminal. Its cost is agent sprawl, bounded by the existing 24h auto-retire.

## 3. Instant delivery

`inbox_check.sh` is polling, however well-behaved: `SessionStart` always, then
`PostToolUse` at most once per 120s. A session that is *idle* is never woken,
which is precisely the case that matters — an agent waiting on another agent's
answer, or a human saying "stop".

Two transports are possible and the choice is not obvious:

  * A dedicated SSE endpoint. The transport is proven end to end on this
    deployment (the MCP stream already survives the reverse proxy and CDN
    unbuffered, with a 15s keepalive that also defeats the CDN's ~100s idle
    timeout). Needs no new proxy module. Uvicorn currently starts with
    websockets disabled, so SSE is also the smaller change.
  * A WebSocket endpoint. Requires enabling websockets in the server start and
    an extra proxy module, for no directional benefit — the channel only ever
    carries server→client hints.

The client half already exists in Claude Code: a background process that exits
re-invokes the agent, so `curl -N` on the stream *is* the wake mechanism. No
separate monitor runtime is required.

Frames should be thin — `{kind, project, agent, id}` — with the woken agent
pulling the real content through `fetch_inbox`. Shipping payloads on the
channel duplicates the mailbox's authorisation model for no gain.

Emit points already identified: `_deliver_message` (alongside the existing
`emit_notification_signal` call), the reservation grant path, and separately
the overseer send path — which bypasses `_deliver_message` and would otherwise
be the one message type that never notifies.

## 4. Display names

Identity is currently `<host>-<platform>-1`, derived and stable. The request is
to let each participant pick a human-readable name.

Keep the derived value as the immutable key and add an alias for display, so a
rename cannot orphan a mailbox, a credential, or a reservation. The server
already stores agent rows; an alias column plus a rename tool is the smaller
half. The larger half is deciding where the alias is authoritative for
addressing — accepting an alias in `to:` means aliases must be unique per
project and reserved against collision with derived names.

## Sequencing

1 first: it is small, it is in this repo, and it converts a two-day mystery
into a legible error. Then 2(b) or 2(a) — that decision gates 3, because the
notification channel is per-recipient and the recipient is whatever identity
model wins. 4 last: it is additive and touches nothing the others depend on.
