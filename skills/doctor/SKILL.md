---
name: doctor
description: Diagnose this repository's exact Agent Mail identity, private state, credential validity, and night monitor without exposing secrets or confusing monitors from other projects. Use after onboarding, after restart or pull, when mail is missing, or when the operator invokes /mcp-agent-mail:doctor.
---

# Diagnose Agent Mail

Run the read-only deterministic diagnostic from the project being checked:

```bash
"${CLAUDE_CONFIG_DIR-${HOME}/.claude}/hooks/mcp-agent-mail/agent_mail_setup.sh" \
  doctor claude "${AGENT_MAIL_CLAUDE_SLOT:-1}"
```

The command checks the canonical project and Agent, local granted-name state,
the presence of the exact credential, a stateless authenticated inbox probe,
and the monitor marker for that exact `(project_key, agent_name)` pair. It also
checks that the monitor's original parent is alive and that the running script
matches the currently installed copy. For Claude it also compares the selected
plugin cache with the configured marketplace source and all nine user-scoped
hook copies with that source. Versions must match exactly; normalized hashes
catch same-version drift without treating CRLF and LF as different code.

Do not read or print the credential, and do not call registration as a repair.
The diagnostic does not mutate local or server state. Its authenticated probe
validates the stored credential; it does not claim that the current MCP session
was already bound before the probe.

When version fields are absent, Claude keys the plugin cache by the marketplace
Git commit. Repair a Git-SHA mismatch with `claude plugin marketplace update
mateusz-klatt-mcp-agent-mail`, then `claude plugin update
mcp-agent-mail@mateusz-klatt-mcp-agent-mail`. For an explicit version mismatch,
use `plugin update`; for same-explicit-version file drift, refresh with uninstall
followed by install because update does not replace that cache. Treat any
user-hook mismatch as a reason to rerun `scripts/integrate_claude_code.sh` from
the marketplace source, then restart/resume Claude so new lifecycle processes
use those copies. Doctor only reports these states; it performs no repair.

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
