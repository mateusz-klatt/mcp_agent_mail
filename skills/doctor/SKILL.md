---
name: doctor
description: Diagnose this repository's exact Agent Mail identity, private state, credential validity, and night monitor without exposing secrets or confusing monitors from other projects. Use after onboarding, after restart or pull, when mail is missing, or when the operator invokes /mcp-agent-mail:doctor.
---

# Diagnose Agent Mail

Run the read-only deterministic diagnostic from the project being checked:

```bash
"${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel)}/scripts/hooks/agent_mail_setup.sh" \
  doctor claude "${AGENT_MAIL_CLAUDE_SLOT:-1}"
```

The command checks the canonical project and Agent, local granted-name state,
the presence of the exact credential, a stateless authenticated inbox probe,
and the monitor marker for that exact `(project_key, agent_name)` pair. It also
checks that the monitor's original parent is alive and that the running script
matches the currently installed copy.

Do not read or print the credential, and do not call registration as a repair.
The diagnostic does not mutate local or server state. Its authenticated probe
validates the stored credential; it does not claim that the current MCP session
was already bound before the probe.

## Interpret monitor results

Do not count `inbox_watch_monitor` processes globally. Several CLIs on one
machine may correctly monitor different repositories. The implemented
singleton is per canonical project and Agent, and the process argv carries both
non-secret labels for human inspection.

`monitor: not armed` is not lost mail: lifecycle hooks still deliver at turn
boundaries. Use `/mcp-agent-mail:wake` only for an unattended session. A live
monitor with missing credentials will recover automatically after onboarding;
do not arm a second copy.

If the running source is stale after a pull, stop/re-arm that monitor through
the host. If core identity or authentication fails, recommend `/onboard`; never
create a replacement Agent.
