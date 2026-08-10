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

Confirm it actually started, then stop:

1. Check that a monitor process is now running:

   ```
   pgrep -af "inbox_watch_monitor"
   ```

   A task list does not show background monitors, so check the processes. One
   match means this session is armed — report that and STOP.

2. If there is **no** match after a few seconds, the host did not honour the
   trigger. Arm it yourself instead, through the **Monitor tool**, persistent and
   with no timeout — NOT a backgrounded shell command, which reports only when
   the process exits, and this script is built never to exit:

   ```
   ${CLAUDE_PLUGIN_ROOT}/scripts/hooks/inbox_watch_monitor.sh claude 1
   ```

   Pass the slot your identity actually uses if it is not 1.

3. Either way, before reporting success make sure it survived startup. A healthy
   monitor prints **nothing** — silence here is the correct outcome, not a
   failure — so verify by process, never by output. If it exited within a few
   seconds, the identity is not registered yet: let SessionStart run first.

Once armed, each new message wakes this session with a single line naming the
message id. The hooks deliver the content as usual; the monitor only ends the
waiting. A quiet mailbox costs nothing at all — no line, no wake, no tokens —
which is the whole difference from the manual `inbox_watch.sh` path, where the
watch window elapsing wakes the agent every 30 minutes to report that nothing
happened.

**Arm one session at a time.** Every armed session subscribes independently and
every one of them is woken by the same message; the server supports the
concurrency, but the duplicate wakes are pure cost.

The monitor is **not** restored when a session resumes, and a monitor that exits
is never restarted for the life of the CLI process. If the CLI restarts
overnight, instant delivery stays off until someone runs `/wake` again — the
lifecycle hooks still deliver on the next turn, so mail is delayed, never lost.
