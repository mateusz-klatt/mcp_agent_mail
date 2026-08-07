# Measurement discipline

Four machines spent a day fixing failures in this project that shared one
shape: something was wrong and nothing said so. Silence meant "no conflict",
"no new mail", "reservation filed", "cleanup done" — and meant them falsely.

The fixes are in the code. This file is about the other half of that day: we
were wrong about our own measurements at least a dozen times, and the wrong
measurements were harder to catch than the wrong code. A false measurement
arrives with the authority of a number, and nobody audits arithmetic that
agrees with them.

Each item below is a real mistake from that day, not a hypothetical. The names
are left out because every one of the four made every one of these at least
once.

## Four axes a measurement has to declare

**What am I measuring — is it the thing I am asking about?**

Verifying that a token rotation succeeded, over the right transport, with
`health_check`. It returned success against a machine whose credential file did
not exist at all: `health_check` authenticates with the server bearer and does
not care about the agent's token. The call was real, the transport was right,
and it answered a different question — "is the server alive" rather than "did
the rotation land". The canonical verifier is a call that needs the thing under
test: `fetch_inbox` with `limit:1`, or simply running `session_start.sh`, which
reads the file and authenticates with its contents.

**Who introduced it — is this our problem?**

Six dead credentials in git history, reported in a context that implied we had
leaked them. Attribution was never checked. All of them came from upstream
commits, which changes the remedy completely: rewriting our history would not
touch a public repository we do not control, and the one live-tree occurrence
needed a single commit rather than a rewrite. The count was correct. The axis
was missing.

**Where is it — tree or history?**

The same six credentials: in history, the question is whether to rewrite a
public repository. In the working tree, it is one line. Conflating them turns a
one-line fix into a proposal that breaks every clone and fork.

**What shape is it — can the detector see this at all?**

Three independent scans searched for `[0-9a-f]{64}`, the shape of the server
bearer, and each reported the repository clean. Agent registration tokens are
`secrets.token_urlsafe(32)`: 43 characters of base64url. No amount of hex
searching will ever match one. The installer script writes *both* kinds into
one file, so the scans covered half of what they claimed to cover. A pre-push
secret gate on one machine had the same hole, and had reported "clean" on
thirty commits.

The narrower question is also the reliable one: *"did MY secret leak"* is
answered with `grep -F` on the literal value and needs no hypothesis about
shape. *"did ANY secret leak"* needs a tool that knows the context —
`gitleaks`, `trufflehog` — because a hand-written pattern fails silently when
the guess about the alphabet is wrong.

## Controls

**Positive control — can the detector see anything?**

Checking whether the bearer reaches the process table on Windows, three runs
came back "not found". That is indistinguishable from a blind detector until
the old `-H` form is run through the same probe and comes back "found". Without
that row, all three results were worthless.

**Negative control — is the detector seeing itself?**

A scan for secrets in the process table found one: its own `grep`, which had
been handed the pattern as an argument. The tool for detecting secrets in argv
had put a secret in argv.

**Every variant, not just one.** A detector that passes its positive control on
one shape says nothing about the others. The secret gate above was tested with
a bearer and never with a registration token.

## Techniques

**A condition you cannot produce is not a condition that does not exist.**

Two machines tried to make `mv` refuse a rename — `chmod 500` on the directory,
then a read-only target file on Windows. Both writes went through anyway, and
both results looked like refutations of the claim under test. They were
evidence that the test never created the state it was testing.

**When you cannot produce the condition, shadow the call that has to fail.**
Overriding `mv` with a shell function returning 1 exercises the branch
deterministically on any platform. This separates two questions that get
conflated: *is this condition reachable here* and *what does the code do when
it happens*. The second needs no operating system, and it is usually the one
being argued about.

**Measure both surfaces when a property might depend on one.** Three machines
measured whether their MCP session was bound and got three different answers.
The discriminator was neither platform nor elapsed time: it was which mount the
agent authenticates through. The measurement that settled it ran both surfaces
one minute apart under the same identity — the control was the *other surface*,
not a second attempt.

**Say what a null result excludes.** "I did not reproduce it" is a conclusion
only after checking that the test's construction permits reproduction. A
simulation of the state-fold bug ran a single round and reported "not
confirmed" — but one round can announce at most `INVENTORY` ids, so the fold
under test can never fire. Sent as-is, it would have refuted a correct report.

## Two failure modes of correction

**Over-correction inherits authority.** A correction arrives right after
someone has been proven wrong, and nobody measures it as carefully as the thing
it corrects. In one afternoon the same claim was narrowed three times — each
narrowing better than the last, each still incomplete. A correction is a
hypothesis of the same rank as the claim it overturns; it only looks like a
conclusion because it follows a demonstration of error.

**False confirmation is harder to catch than false refutation.** A measurement
that contradicts the group gets scrutinised. One that agrees is accepted. So
when a result matches expectation, validity checks matter *more*, not less —
the opposite of what the instinct suggests.

**Rejecting an option by preference is not rejecting it by measurement, and the
record looks the same either way.** A proposed fix was set aside as "the less
elegant of two" and stayed on the list as a viable alternative. Measured later,
it made the bug worse. Whoever read the thread a month on would have seen two
options and picked the simpler one.

## Where this came from

Every example above is a real event, and each has a commit or a thread entry
behind it. If one of them stops matching the code, the example is stale and
should be replaced rather than deleted — the mechanism outlives the instance.
