---
name: wake
description: Arm the Agent Mail night monitor for THIS session only
disable-model-invocation: true
---

Invoking this skill arms the bundled inbox monitor for this session. The plugin
declares it with `when: "on-skill-invoke:mcp-agent-mail:wake"`, so the host
starts it in response to this very invocation.

Arm it when the session is about to be left unattended — overnight, or any
stretch with nobody at the console. During the day the lifecycle hooks already
deliver mail on every turn boundary, and a human who is present can simply say
"check the mail", so a monitor buys nothing and costs a held connection.

**`disable-model-invocation: true` is load-bearing, not a preference.** The
host matches `when:` by raw string equality against the identifier emitted on
dispatch, and the Skill-tool path emits the *unresolved* argument — invoking
this as a tool emits `wake`, which never matches `mcp-agent-mail:wake`. Only the
slash-command path emits the canonical name. Disabling model invocation forces
`/wake`, which is the form that works. That also matches when it should be used:
arming is a deliberate act by whoever is leaving.

Confirm it actually started with the exact-mailbox diagnostic, then stop:

```bash
"${HOME}/.claude/hooks/mcp-agent-mail/agent_mail_setup.sh" \
  doctor claude "${AGENT_MAIL_CLAUDE_SLOT:-1}"
```

Do not use a global `ps`, `pgrep`, or process count. Several CLIs can correctly
hold monitors for different projects on the same machine. The monitor itself
enforces one stream per exact `(project_key, agent_name)` and publishes those
non-secret labels in its argv and private metadata; doctor checks that exact
record, its parent process, and its source checksum.

If doctor says the monitor is absent, the host did not honour the trigger. Arm
the integrator-managed script through the **Monitor tool**, persistent and with no
timeout — not a backgrounded shell command:

```bash
${HOME}/.claude/hooks/mcp-agent-mail/inbox_watch_monitor.sh claude 1
```

Pass the actual slot when it is not 1, then run doctor again. A healthy monitor
prints nothing. If credentials are not present yet, leave the existing monitor
alone: it re-reads private state and starts delivering automatically after
`/mcp-agent-mail:onboard` succeeds.

If doctor reports a different running script snapshot, stop/re-arm it from the
integrator-managed path above. If it reports user-hook drift, rerun
`scripts/integrate_claude_code.sh`. A cached plugin copy does not track the
marketplace source. For Git-SHA versioning, update the marketplace and then the
plugin. For same-explicit-version file drift, refresh with uninstall followed by
install; `plugin update` at an unchanged explicit version does not refresh the
cache.

Once armed, each new message wakes this session with a single line naming the
message id. The hooks deliver the content as usual; the monitor only ends the
waiting. A quiet mailbox costs nothing at all — no line, no wake, no tokens —
which is the whole difference from the manual `inbox_watch.sh` path, where the
watch window elapsing wakes the agent every 30 minutes to report that nothing
happened.

Repeated `/wake` for the same project and Agent is idempotent: the later monitor
exits before subscribing. Monitors for different projects are independent and
valid. The process watches its original CLI parent **where that parent is
observable** and exits with it, which is what keeps an orphan from surviving into
the next CLI run. Where the probe has no resolving power — a native Windows
parent is not in the MSYS process table, so `kill -0` answers "absent" about a
healthy CLI — the monitor deliberately outlives its session rather than believe
that answer, and stopping it is then a manual act. Both cases occur on one
machine: arming through the plugin host gives an observable shell parent, while
`subprocess.Popen` does not. Do not infer which one you have — `doctor` reads
the verdict the monitor recorded at birth and names it.

The monitor is **not** restored when a session resumes, and a monitor that exits
is never restarted for the life of the CLI process. If the CLI restarts
overnight, instant delivery stays off until someone runs `/wake` again — the
lifecycle hooks still deliver on the next turn, so mail is delayed, never lost.
