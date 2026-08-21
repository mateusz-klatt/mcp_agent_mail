---
name: onboard
description: Provision this repository's durable Agent Mail mailbox in one secret-safe operation. Use when a new clone, host, or project has not yet been onboarded, when lifecycle hooks report that Agent Mail is inactive, or when the operator explicitly invokes /mcp-agent-mail:onboard.
---

# Onboard Agent Mail

Run the repository's deterministic onboarding command. It ensures the canonical
project, registers the stable `client-os-host-slot` Agent, captures the token
that is issued only once, atomically stores it under the granted Agent name,
then writes the repairable granted-name marker and verifies an authenticated
inbox read. Credential-first ordering means an interrupted second write cannot
orphan the server identity.

## Run

From the project being onboarded:

```bash
"${HOME}/.claude/hooks/mcp-agent-mail/agent_mail_setup.sh" \
  onboard claude "${AGENT_MAIL_CLAUDE_SLOT:-1}"
```

Treat this command's exit status and safe summary as authoritative. Never call
`register_agent` separately, print the returned token, inspect raw
`credentials.json`, or place a credential in argv, a prompt, a message, or Git.
On failure, report its diagnostic without attempting a second identity.

The command is idempotent. If private credentials already exist, it
authenticates the same durable Agent and refreshes its profile; it never creates
a subagent identity.

## Optional local marker

Do not create a marker unless the operator explicitly asks for one. When asked,
rerun the same command with `--local-marker`. It writes the exact `project_uid`
returned by the server to `.agent-mail-project-id` and adds only that filename
to `.git/info/exclude`.

Never commit `.agent-mail-project-id`, never add it to `.gitignore`, and never
use `projects mark-identity` for this flow: with directory identity mode that
command can derive a different project on every host.

## Finish

Report the canonical Agent, generated display name, automatic sound, private
state location, and verification result from the safe summary. A session whose
`SessionStart` ran before onboarding cannot retroactively gain its root
execution; tell the operator to restart or resume the CLI. Do not arm `/wake`
unless the operator is actually leaving the session unattended.
