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

## Five axes a measurement has to declare

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

The sharpest instance of this axis is about writing this file. Three machines
claimed it within thirty-eight seconds, none having seen the other two. One of
them — the author of this paragraph — checked first and reported `docs/` free,
zero reservations on the prefix. The query was correct and the answer was true.
It was also the answer to a different question: **reservations record who is
editing, not who intends to.** Two others already intended, and neither could
possibly have appeared, because nobody had touched a file yet.

Worse, the project has a mechanism for exactly this and none of the three used
it. `autoreserve` files a reservation *after* an edit, by design; claiming an
intent requires calling `file_reservation_paths` deliberately, before writing.
Announcing in a shared channel feels like claiming a resource. It is not one:
messages cross, and the reader's copy of "who is doing what" is always older
than the sender's.

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

A detector can also see too narrowly rather than not at all. Someone grepping a
docstring pulled out `- Ignore conflicts; resolve them by coordinating with
holders` and was one keystroke from reporting that the tool tells its callers
to ignore conflicts. The line was real and the match was correct. Four lines
above it stood `Don't:`. When quoting from a list, quote the header of the list
— a fixed `-A`/`-B` window does not guarantee you have it.

It is tempting to conclude that a match is more dangerous than a miss, because
silence looks suspicious and agreement does not. That is wrong, and the
counter-example arrived within the hour: someone counted session logs, got
zero, and the zero fitted their standing hypothesis that the hook was sloppy.
They were counting in the wrong directory; the logs were one level down. A
false report of a non-existent bug was one keystroke away, from a zero.

So the direction of the result is not the discriminator. **Agreement with what
you expected is.** A zero from the wrong scope and a match without its frame
are the same error wearing two faces, and both pass unchallenged for the same
reason. What protects you is not distrusting silence — it is checking that you
looked where the thing would have been.

**When was it read, and when am I acting on it?**

A fifth axis, and the only one where the question was right and the answer was
right and the statement was still false. Someone measured whether anything had
been pushed, spent six minutes on another task, and reported the result as
current. It had been wrong for four and a half minutes by the time it was sent.

This one is not really about measurements. The same person walked into two more
instances the same afternoon, and only one involved a number: they marked
messages read from a list fetched before the last arrivals, and they announced
they were taking a task from a queue of free work quoted forty minutes earlier
— the task had been finished for forty-five. Nobody would call reading a task
list a measurement, which is exactly why nobody applies a rule about
measurements to it. **Any read followed by an action is exposed; measuring is
just the common case.**

Everything here moves in seconds: unread counts, reservations, session
bindings, who is editing what, what is still unclaimed. Twice in one afternoon
an empty inbox was followed by a newer message in the same thread — once from a
mis-marked read, once because it arrived between two queries. Identical
symptom, unrelated causes, neither distinguishable from a genuinely quiet
mailbox.

A read of fast-moving state carries an expiry. If it is not acted on promptly
it has to be re-taken rather than quoted, and when a decision hangs on it the
read belongs immediately before the decision — not before the writing-up.

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

**The control has to be built independently of the thing under test.** A
pre-push gate searched for the literal credential values. Because a bearer read
from a file carries no trailing newline, the file it searched held one line —
bearer immediately followed by token — so the gate was looking for a
concatenation that occurs nowhere. Its positive control was produced by copying
that same file, matched trivially, and certified the blindness. A control
derived from the subject always passes; that is what makes it worthless.

## Techniques

**A condition you cannot produce is not a condition that does not exist.**

Two machines tried to make `mv` refuse a rename — `chmod 500` on the directory,
then a read-only target file on Windows. Both writes went through anyway, and
both results looked like refutations of the claim under test. They were
evidence that the test never created the state it was testing.

**In a security scan, know which `grep` you are running — and check it where
it matters.** On three of the four machines here `grep` is a shell function
dispatching to `ugrep --ignore-files`, installed from the agent harness's shell
snapshot; on the fourth it is plain GNU grep. Same command quoted in the same
thread, two different programs, and no result shows which one ran. What the
wrapper skips — ignored paths, `.git`, binaries — is exactly where a leaked
credential lands.

Worse, the *same* wrapper is blind or not depending on the search root, because
`--ignore-files` looks for the ignore file relative to where the search starts.
A canary dropped in an ignored directory and searched for from that directory
is found; searched for from the repository root, it is not. So the natural way
to check whether you have this problem — put a canary next to itself and scan
there — returns wrapper and GNU in agreement and certifies that all is well.
The condition was reachable and the act of measuring removed it.

`command grep` reaches the real binary, but only where a shell interprets it.
`find … | xargs command grep …` runs `execvp("command")`, which does not exist,
so xargs exits 127 having searched nothing. In a pipeline ending in `wc -l` or
a command substitution, that 127 is discarded and the zero reads as clean —
verified here, canary in place, zero returned. A security gate always ends in a
count, so this is the shape it takes every time.

The same split runs through git: `git grep` searches tracked files, `git grep
--no-index` searches the working tree, and `git ls-files` answers about the
local index and nothing else. Three questions, one habit of speech.

**A reference that looks checkable is a reference nobody checks.** Every
`file.py:NNN` in one deployment file had drifted onto unrelated code — one of
them 84 lines from the behaviour it claimed to document. A line number reads as
evidence that somebody verified it, so the reader arrives already convinced and
interprets whatever is there as a match. The same shape as a pattern that
cannot match: both are silent, and both look precise. Symbol names survive
edits and are just as greppable.

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

The clearest example is one that was not sent. A call that had been used a
dozen times as a probe suddenly failed, and the obvious reading was that the
probe had been worthless all along — a severe piece of self-criticism, on a day
when self-criticism was the currency, and it would have invalidated a dozen of
the author's own measurements and everybody else's readings that depended on
them. Nobody would have challenged it. They measured it instead, in both
states, and the probe turned out to discriminate correctly. Note what makes
this hard: the retraction felt like rigour, and rigour is exactly what nobody
audits.

**False confirmation is harder to catch than false refutation.** A measurement
that contradicts the group gets scrutinised. One that agrees is accepted. So
when a result matches expectation, validity checks matter *more*, not less —
the opposite of what the instinct suggests.

**Rejecting an option by preference is not rejecting it by measurement, and the
record looks the same either way.** A proposed fix was set aside as "the less
elegant of two" and stayed on the list as a viable alternative. Measured later,
it made the bug worse. Whoever read the thread a month on would have seen two
options and picked the simpler one.

## A borrowed number arrives without its question

Every axis above is about a measurement you took. There is a sixth failure that
only happens to numbers you did not take, and it is the easiest of all to miss.

Someone counted the reservations in the project and reported that five of six
carried the generic reason `auto: edited in session` — offered as an
observation, correctly. Two other people then used that count as evidence that
nobody writes deliberate reasons, and built recommendations on it. The count
was right. It measures who has *edited a file since reserving it*, which is
nearly everyone, because editing is what reserving is for. A tool overwrote the
deliberate reasons on first edit; the number was recording the overwrite, not
the habit.

The people who repeated it had spent that day demanding the question behind
every number they were shown. They did not ask it of one they had adopted. A
measurement carries its scope in the head of whoever ran it, and almost never
in the sentence that travels: **when you use someone else's number, restate
what it counted before you build on it** — and if you cannot, that is the
finding.

## A subsystem with a defect history is cheaper to accuse

One afternoon, against one layer of this project — file reservations, which had
attracted the most real findings and was therefore the right place to check:

```
CONFIRMED BY MEASUREMENT              NEARLY SENT, ALL FALSE
 1. a hook overwrites a deliberate     A. "the docstring says to ignore
    reservation reason                    conflicts"  (a line under `Don't:`)
 2. the conflict payload omits that    B. "the hook writes no session log"
    reason entirely                       (counted in the wrong directory)
 3. a warning about configuration      C. "the server granted two holds and
    printed beside "granted"              reported none"  (they were first
 4. expires_ts cannot carry a             by thirteen minutes)
    freshness signal                   D. "the server reported no conflict"
                                          (their own jq dropped the field)
```

Four real, four false, one subsystem, one afternoon. Three of the false ones
were caught in the last check before sending; one went out and had to be
retracted.

The bilance alone proves nothing — four false alarms is unremarkable if that is
where everyone was looking. What makes it a property of the situation rather
than of a person is the **spread of authorship**. The four false reports came
from three different people; a fourth then listed six over-broad claims of
their own from the same afternoon, which makes it four out of four. Not "this
is not one person's habit" — there was nobody it did not happen to. Each new
accusation against that layer fitted a pattern the group had just established,
so it needed less evidence to be believed and got less. One of them noticed
afterwards that they had accepted three such reports unverified while checking
every claim that touched their own code — not from laziness, but because the
claims had stopped looking like hypotheses.

Suspect this most where you have been most right.

**The countermeasure, and it is not "be more careful."** Nobody here was
careless; every one of those false reports had a measurement behind it. What
distinguishes the ones caught from the one that went out is uniform, and it is
worth stating as a procedure:

> Send an accusation only after running a control that would have refuted it —
> not after re-reading your evidence and finding it still convincing.

Of five false alarms stopped before sending, every single one was stopped by a
control: a call that had to succeed, a path nobody held, an invented route that
returned the same code as the real one. Not one was stopped by reading the
draft again. Rereading confirms; only a control can contradict.

This composes with the rule two sections down about controls built from their
own subject. A control that would refute you is worth nothing if it is made out
of the thing it is testing — a canary placed in a directory that turns out not
to be ignored, a positive control copied from the file under test. Both of
those passed, and both meant nothing.

## Who catches these

Not one of the mistakes above was found by the person who made it. Every single
one was caught by somebody else, and always by the same question: *what is that
number actually an answer to?*

That is not a remark about attentiveness. It is structural. The author of a
measurement knows what they meant to measure, and reads the result as the
answer to that. A reader has only the result and the method, so the gap between
them is the first thing they see. Which means:

- **A finding is not finished when it is measured. It is finished when someone
  else has said what it excludes.** Four machines agreed three separate times
  today on a fix that was still wrong; agreement is not verification, and a
  proposal everybody likes is the one nobody re-checks.
- **Report the measurement, not just the conclusion** — the call, the flags, the
  controls. A conclusion cannot be audited; a method can. Half the corrections
  below started with someone noticing a missing flag in a command that was
  quoted in full.
- **Say plainly when a test failed to create its own condition.** Twice today
  that admission turned what looked like a refutation into a live finding.

## Some failures have no signature, and can only be prevented

Every technique above assumes the failure leaves a trace somebody could look
for. Some do not, and the mistake is to answer them with better reporting.

A hook command that `cmd.exe` cannot run exits **0**. So does a hook that ran
and had nothing to say — which is the designed behaviour of four of the five.
The exit code is identical, and so is the output: empty. A fallback that fires
on non-zero exit therefore cannot fire, and one that fires on empty output would
fire constantly on healthy machines. The observable trace of the failure is
byte-identical to the trace of its opposite.

When that is true, "detect and report it" is not a cheap version of the fix; it
is a different thing that does not work. Worse, shipping it removes the reason
to keep looking — everyone now believes this class announces itself.

Before choosing a reporting fix, ask what distinguishes the failure from the
healthy case in the data you can actually see. If the honest answer is
*nothing*, the only fix is to make the state unreachable: name the interpreter,
pin the path, remove the branch. Reserve reporting for failures that have a
signature.

The generalisation is uncomfortable because it cuts against the rest of this
document: not every problem is a measurement problem. Some are, and better
controls find them. Others cannot be measured at all from where you stand, and
the effort spent building an instrument is effort not spent removing the state.

## Your own probe comes back as somebody else's evidence

We measure against a shared server and, sometimes, a shared machine. A probe
that pokes at a live system produces effects other people can see — and they see
them as phenomena, not as your experiment.

Today a controlled call to `cmd.exe` on a `.sh` file put a file-association
dialog on the operator's screen. Within minutes three agents had attributed it
to a commit shipped a few minutes earlier, marked it urgent, and pushed a fix
whose justification cited the operator's window as field evidence. The commit
that accused it only ever *printed* a path, and printing cannot open a dialog —
one line of mechanism ruled it out, and nobody asked for it, because live human
testimony feels stronger than any measurement.

Two things make this hard to catch. The first is that it needs you to remember
what you were doing two minutes ago, at the moment everyone is looking at the
conclusion. The second is that the fix was **correct**: the bare path really
does fail there. A right action taken for a wrong reason is the worst case,
because the outcome vouches for the process, and nothing downstream ever
disturbs it.

Two habits help. Announce a probe that touches shared state *before* running it,
so the effect has a name when it surfaces. And when a symptom is attributed to a
suspect, ask the cheapest question first — **does the accused execute anything at
all?** — before the more interesting question of how it did it.

## Where this came from

Every example above is a real event, and each has a commit or a thread entry
behind it. If one of them stops matching the code, the example is stale and
should be replaced rather than deleted — the mechanism outlives the instance.
