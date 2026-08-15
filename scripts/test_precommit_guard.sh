#!/usr/bin/env bash
# Exercise .githooks/pre-commit against a throwaway repository.
#
#     scripts/test_precommit_guard.sh
#
# This never touches the repository you run it from. It builds its own git repo
# under `mktemp -d`, copies the hook in, runs every case there, and removes the
# directory on exit — including on failure. That isolation is the point: a probe
# for a secret guard is itself a thing that stages files, and the obvious
# ad-hoc version ("cd somewhere, git add -A -f, see what happens") stages the
# caller's working tree if the `cd` silently fails. A guard test must not be
# able to commit the secret it is testing for.
#
# Cases are paired. Positive samples must be refused; negative controls must be
# allowed. Without the controls, a hook that refuses everything would look
# perfect, and that hook is useless — it gets disabled within a day.
set -uo pipefail

HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.githooks/pre-commit"
[ -f "${HOOK}" ] || { echo "no hook at ${HOOK}" >&2; exit 2; }

# Every step below is checked, and the reason is this script's own header.
# `set -u` does NOT catch the case that matters: if `mktemp -d` fails, WORK is
# set-and-empty rather than unset, `cd ""` returns 1, and without `set -e` the
# script keeps going — in the caller's repository, where it would then point
# core.hooksPath at a scratch directory and run `git clean -fd`. Reported by
# claude-linux-holzera-1, who measured it landing in the project root.
WORK="$(mktemp -d)" || { echo "mktemp -d failed" >&2; exit 2; }
case "${WORK}" in
    /*) ;;
    *) echo "mktemp -d returned something that is not an absolute path" >&2; exit 2 ;;
esac
[ -d "${WORK}" ] || { echo "mktemp -d returned ${WORK}, which is not a directory" >&2; exit 2; }

# Guarded because a variable in an `rm -rf` path is how directories disappear.
cleanup() {
    case "${WORK:-}" in
        /tmp/*|/var/folders/*) rm -rf "${WORK}" ;;
        *) echo "refusing to remove ${WORK:-<empty>}" >&2 ;;
    esac
}
trap cleanup EXIT

cd "${WORK}" || { echo "cannot cd into ${WORK}" >&2; exit 2; }
# Belt and braces: prove we actually moved before anything destructive runs.
[ "${PWD}" = "${WORK}" ] || { echo "cd reported success but PWD is ${PWD}" >&2; exit 2; }

git init -q .
git config user.email guard-test@example.invalid
git config user.name "Guard Test"
mkdir -p .githooks
cp "${HOOK}" .githooks/pre-commit
chmod +x .githooks/pre-commit
git config core.hooksPath .githooks

# Key headers are BUILT here, never written literally, and that is load-bearing
# rather than fussy. The hook refuses any file containing a private-key header,
# correctly and by design -- which means a suite that spells one out cannot be
# committed in a clone where the hook is enabled. Measured: with the headers
# inline, committing this very file is refused with "contains a private key
# header". A test suite nobody can commit is a test suite that stops being
# maintained, so the fixtures assemble the exact same bytes at run time.
pem() { printf -- '-----BEGIN %s-----' "$1"; }

pass=0
fail=0

# Canary. If the hook is not wired up, every positive sample below would be
# "allowed" and the run would look like a clean bill of health for a guard
# that never executed. Prove it fires before trusting anything else.
printf '%s\n' "$(pem 'OPENSSH PRIVATE KEY')" > .canary.txt
git add .canary.txt
if git commit -q -m canary >/dev/null 2>&1; then
    echo "canary committed: the hook is not running, so no result here means anything" >&2
    exit 2
fi
git reset -q
git clean -qfd -e .githooks
echo "canary: the guard is live"
echo

# want=allow | want=refuse
check() {
    local want="$1" label="$2"
    local rc=0
    # Re-checked every case, not just at startup: this function runs `git reset`
    # and `git clean`, and a stray `cd` anywhere above would aim both at
    # whatever directory we ended up in.
    [ "${PWD}" = "${WORK}" ] || { echo "PWD drifted to ${PWD}" >&2; exit 2; }
    git commit -q -m "${label}" >/dev/null 2>&1 || rc=$?
    local got="allow"
    [ "${rc}" -ne 0 ] && got="refuse"
    if [ "${got}" = "${want}" ]; then
        printf '  ok    %-8s %s\n' "${want}" "${label}"
        pass=$((pass + 1))
    else
        printf '  FAIL  wanted %s, got %s: %s\n' "${want}" "${got}" "${label}"
        fail=$((fail + 1))
    fi
    # Leave the index clean whichever way it went. `.githooks` is excluded
    # deliberately: it is untracked here, so a bare `git clean -fd` deletes
    # the hook under test and every later case then passes for the worst
    # possible reason — no guard at all.
    git reset -q
    git clean -qfd -e .githooks
}

echo "negative controls (must be allowed):"
echo "hello" > readme.md; git add readme.md
check allow "an ordinary file"

echo "ssh-ed25519 AAAAC3Nz" > server.pub; git add server.pub
check allow "a public key, .pub"

echo "ssh-ed25519 AAAAC3Nz" > signing-abc.pub.key; git add signing-abc.pub.key
check allow "a public key whose name ends .pub.key"

printf 'BEGIN of a sentence\naGVsbG8gd29ybGQgYmFzZTY0\n' > notes.md; git add notes.md
check allow "prose containing BEGIN and base64"

echo "prose" > big-ok.txt
head -c 200000 /dev/zero | tr '\0' 'A' | fold -w 100 >> big-ok.txt
git add big-ok.txt
check allow "a 200 KB file with no key in it"

echo
echo "positive samples (must be refused):"
echo "irrelevant" > secret.key; git add -f secret.key
check refuse "a .key file staged with git add -f"

printf '%s\nb3BlbnNz\n' "$(pem 'OPENSSH PRIVATE KEY')" > innocuous.txt; git add innocuous.txt
check refuse "an OpenSSH private key inside a .txt"

printf '%s\nMIIEpAIB\n' "$(pem 'RSA PRIVATE KEY')" > config.yml; git add config.yml
check refuse "an RSA private key inside a .yml"

printf '%s\nlQOYBF\n' "$(pem 'PGP PRIVATE KEY BLOCK')" > k.asc; git add -f k.asc
check refuse "a PGP private key block"

# The case that a small-file-only suite misses entirely. `head -c` closes the
# pipe on a large blob, git show dies of SIGPIPE, and under pipefail that used
# to read as "no key found" — so the bigger the file, the safer it looked.
{
    printf '%s\n' "$(pem 'OPENSSH PRIVATE KEY')"
    head -c 200000 /dev/zero | tr '\0' 'A' | fold -w 100
} > big-key.txt
git add big-key.txt
check refuse "a 200 KB file whose first line is a key header"

# --- provider tokens ---------------------------------------------------------
# These ten cases existed and passed while the guard caught no provider token at
# all, because not one of them presented one. That is what let the scan vanish in
# a rewrite and stay gone: the suite was measuring the guard it had, not the
# guard it needed. The negative controls below matter as much as the positives --
# a token rule that refuses ordinary prose gets switched off, and a switched-off
# guard protects nothing.

echo
echo "provider tokens, negative controls (must be allowed):"

printf 'see docs on ghp_ and sk- prefixes, and AKIA ids\n' > tokens-doc.md; git add tokens-doc.md
check allow "prose naming the prefixes without a token"

printf 'sk-short\nghp_tooshort\nAKIA123\n' > shortish.txt; git add shortish.txt
check allow "prefixes too short to be tokens"

printf 'https://example.test/xoxb-not-a-token\n' > link.txt; git add link.txt
check allow "a url containing xoxb- but no token body"

echo
echo "provider tokens, positive samples (must be refused):"

printf 'GITHUB_TOKEN=ghp_%s\n' "$(printf 'a%.0s' $(seq 1 36))" > env-ghp.txt
git add env-ghp.txt
check refuse "a GitHub personal access token"

printf 'OPENAI_API_KEY=sk-%s\n' "$(printf 'b%.0s' $(seq 1 32))" > env-sk.txt
git add env-sk.txt
check refuse "an OpenAI-style secret key"

printf 'aws_access_key_id = AKIA%s\n' "ABCDEFGHIJKLMNOP" > env-aws.txt
git add env-aws.txt
check refuse "an AWS access key id"

printf 'SLACK=xoxb-%s\n' "$(printf 'c%.0s' $(seq 1 24))" > env-slack.txt
git add env-slack.txt
check refuse "a Slack bot token"

printf 'PAT=github_pat_%s\n' "$(printf 'd%.0s' $(seq 1 36))" > env-pat.txt
git add env-pat.txt
check refuse "a fine-grained GitHub PAT"

# A token in a test fixture is still a token. This is the shape that actually
# leaked here: a credential pasted into a file nobody thought of as secret.
mkdir -p tests
printf 'FIXTURE = "ghp_%s"\n' "$(printf 'e%.0s' $(seq 1 36))" > tests/fixture.py
git add tests/fixture.py
check refuse "a token pasted into a test fixture"

# Known and deliberate limit, asserted so nobody rediscovers it as a surprise:
# the guard reads the first 64 KB of a blob, so a token past that byte is missed.
# Written as `allow` because that IS today's behaviour -- if someone widens the
# bound, this case fails and they get to decide knowingly.
{
    head -c 70000 /dev/zero | tr '\0' 'A' | fold -w 100
    printf 'TOKEN=ghp_%s\n' "$(printf 'f%.0s' $(seq 1 36))"
} > deep-token.txt
git add deep-token.txt
check allow "a token past the 64 KB read bound (known limit)"

# --- filename conventions ----------------------------------------------------
# Every file below holds the same innocuous byte. That is the point: if one of
# these is refused, it was refused for its NAME, because there is nothing in the
# content to find. The name layer is the only one that can catch a raw Ed25519
# seed -- 32 random bytes carry no PEM header and no token prefix -- and
# `signing-<hex>` is exactly the shape that leaked here once already.

echo
echo "filename conventions, negative controls (must be allowed):"

echo x > signing-abc123.pub; git add signing-abc123.pub
check allow "a signing key's paired .pub"

echo x > revoked-signing-77c6e768.pub; git add revoked-signing-77c6e768.pub
check allow "a revoked-signing marker, .pub (this repo has one)"

echo x > secretary-notes.md; git add secretary-notes.md
check allow "a filename containing 'secret' inside a word"

echo
echo "filename conventions, positive samples (must be refused):"

echo x > deploy.priv; git add -f deploy.priv
check refuse "a .priv file"

echo x > signing-77c6e768; git add -f signing-77c6e768
check refuse "a raw Ed25519 seed named signing-<hex>, no extension"

echo x > secrets.json; git add -f secrets.json
check refuse "a secrets.* bundle"

echo x > db-secret; git add -f db-secret
check refuse "a *-secret file"

echo x > app.secret.yaml; git add -f app.secret.yaml
check refuse "a *.secret.* file"

# --- the guard must not become the leak --------------------------------------
# The hook reads staged blobs in order to refuse them. Under `bash -x` every
# expansion is echoed, so without `set +x` the guard prints the very bytes it
# exists to stop -- and it does so during a commit, which is where CI logs are
# collected. A rewrite of scripts/hooks/check_inbox.sh dropped that line and
# spat two credentials into a transcript, so this is a regression test for a
# thing that has already happened once.
echo
echo "the guard's own output:"
printf '%s\nCANARY-MUST-NOT-APPEAR-IN-TRACE\n' "$(pem 'OPENSSH PRIVATE KEY')" > traced.txt
git add traced.txt
traced_output="$(bash -x .githooks/pre-commit 2>&1 || true)"
if printf '%s' "${traced_output}" | grep -q 'CANARY-MUST-NOT-APPEAR-IN-TRACE'; then
    printf '  FAIL  the hook echoed staged content while running under bash -x\n'
    fail=$((fail + 1))
else
    printf '  ok    refuse   staged content stays out of the hook trace under bash -x\n'
    pass=$((pass + 1))
fi
# Control: the run above must actually have refused, or the absence of the
# canary would only mean the hook exited before reading anything.
if printf '%s' "${traced_output}" | grep -q 'refusing this commit'; then
    printf '  ok    refuse   and that traced run did refuse, so it read the blob\n'
    pass=$((pass + 1))
else
    printf '  FAIL  the traced run did not refuse, so the check above proves nothing\n'
    fail=$((fail + 1))
fi
git reset -q
git clean -qfd -e .githooks

echo
if [ "${fail}" -eq 0 ]; then
    echo "all ${pass} cases behaved as intended"
    exit 0
fi
echo "${fail} of $((pass + fail)) cases were wrong"
exit 1
