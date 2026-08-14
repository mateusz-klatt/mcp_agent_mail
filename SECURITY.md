# Security

## Reporting a vulnerability

Report privately through GitHub's [security advisory
form](https://github.com/mateusz-klatt/mcp_agent_mail/security/advisories/new).
Do not open a public issue for a vulnerability.

Include what an attacker gains, the smallest reproduction you have, and the
commit you tested. A report that says which commit it applies to is worth
several that do not.

This is a personal project, not a funded one. Expect an acknowledgement within
a week and no bug bounty.

## What this software assumes

Iris is designed for a single operator running one server for their own agents.
Those assumptions are load-bearing, and a deployment that breaks them is
outside what the code defends against:

- **The HTTP endpoint is not public.** Every MCP call requires a bearer token
  that is shared by all agents; it identifies the deployment, not the caller.
- **Agent identity is not an authentication boundary against a hostile agent.**
  A durable agent proves itself with a registration token, which is enough to
  keep two cooperating agents apart and is not enough to contain one that is
  actively malicious. Anything holding a valid token can act as that agent.
- **File reservations are advisory.** They announce intent. Nothing enforces
  them at the filesystem level, and an agent that ignores a claim will succeed.
- **The human web interface is the authenticated surface.** It uses signed
  sessions, per-project roles, and server-side authorization; hiding a control
  in the browser is never treated as a boundary.

## Handling credentials

Two separate secrets exist and are often confused:

- the **transport bearer**, which admits a caller to the MCP endpoint, and
- a per-agent **registration token**, which proves a durable mailbox identity.

Neither belongs in a repository, a prompt, or a message body. Note that tool
validation errors quote the rejected argument, so passing a token to a tool
that does not declare that parameter returns the token in the error. Send a
call once without credentials to check the parameter names, then repeat it with
them.
