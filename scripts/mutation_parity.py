#!/usr/bin/env python3
"""Check that a rewritten test file is no weaker than the one it replaced.

    scripts/mutation_parity.py --base <ref> --mutations muts.json tests/test_x.py

A rewritten test that passes proves nothing. Making it pass is the easy half, and
a test rewritten until green can be arbitrarily weaker than the one it replaced
while looking identical from the outside.

Asking the author to break the source and watch their test go red is better, but
not enough, and claude-win-home-1 said why: the author picks the mutation AFTER
writing the test, so they naturally pick one their test catches. It rules out a
tautology. It does not rule out a test that is radically weaker.

The old test still exists in git. That is the whole idea here: run both against
the same broken source and compare. Four outcomes, and each was contributed by
somebody who spotted a hole in the previous three:

    semantic mutation, old RED,   new RED     parity held
    semantic mutation, old RED,   new GREEN   REGRESSION -- this blocks
    semantic mutation, old GREEN, new GREEN   a gap BOTH share; reported, not fatal
    equivalent mutation, new RED              OVER-SPECIFIED -- this blocks too

The third row is claude-win-home-1's, and it is where `set +x` lived: the guard
suite passed 10 of 10 while catching no token at all, so a parity run on that file
would have come out clean with the new test exactly as blind as the old one.
Parity is a relative criterion and cannot see a shared blind spot; it can only
report it.

The fourth is claude-mac-laptop-1's, and it is the upper bound the first three
lack. A test that goes red on every change, including changes that preserve
behaviour, satisfies parity perfectly -- and gets deleted or weakened within the
month because it blocks legitimate refactoring. A guard that provokes its own
removal is worse than no guard.

Mutations must come from somebody who did not write the tests. That is not
ceremony: the failure this whole tool exists to prevent is a probe chosen to fit
its own result, and only the test's author can commit it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Same three-way convention as scripts/relicense_debt.py, and for the same
# reason. `1` has to mean "a real finding" and nothing else, or a CI job cannot
# tell a broken instrument from a broken test. The first draft of this file
# exited 1 on a malformed mutation set -- indistinguishable from "the new test
# is weaker" -- which is the identical mistake made in the debt gate two hours
# earlier and caught there the same way: by running it, not by reading it.
OK = 0
FINDING = 1
NO_MEASUREMENT = 2


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd or REPO, capture_output=True, text=True)


def pytest_verdict(python: str, test_file: str, cwd: Path) -> bool | None:
    """True green, False red, None when pytest never got as far as an assertion.

    Reading "not zero" as red is the loosest assertion available -- it asserts
    only that the process was unhappy -- and it is the same defect this file
    exists to catch, one level up, in the tool doing the catching. pytest has
    six exit codes and only two of them are verdicts:

        0 OK                 the file passed
        1 TESTS_FAILED       the file failed -- a real red
        2 INTERRUPTED        collection blew up, or someone hit ^C
        3 INTERNAL_ERROR
        4 USAGE_ERROR
        5 NO_TESTS_COLLECTED

    Codes 2-5 mean no test ran. The dangerous one is routine rather than
    exotic: a mutation that deletes or renames a name breaks the IMPORT of the
    module under test, both the old and the new test file fail to collect, both
    are recorded red, and the table prints "parity held" for a comparison in
    which not one assertion was ever evaluated. The verdict would be produced
    by a run that could not have produced any other.
    """
    proc = run(python, "-m", "pytest", test_file, "-q", "-x",
               "-p", "no:cacheprovider", "--no-cov", cwd=cwd)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    print(f"  pytest exited {proc.returncode} on {test_file}: no test ran, so this "
          f"is not a red.\n{proc.stdout[-1500:]}", file=sys.stderr)
    return None


def apply_mutation(cwd: Path, mutation: dict) -> str:
    """Apply one mutation, returning the original text so it can be restored.

    Refuses unless the anchor appears exactly once. A mutation that matches
    twice silently changes something nobody inspected, and a mutation that
    matches zero times is reported as applied while doing nothing at all --
    which would score as "both tests survived" and be filed as a shared gap
    that does not exist.
    """
    path = cwd / mutation["path"]
    original = path.read_text(encoding="utf-8")
    hits = original.count(mutation["find"])
    if hits != 1:
        print(
            f"mutation {mutation['id']}: anchor appears {hits} times in "
            f"{mutation['path']}, expected exactly 1. Refusing to guess.",
            file=sys.stderr,
        )
        raise SystemExit(NO_MEASUREMENT)
    path.write_text(original.replace(mutation["find"], mutation["replace"]),
                    encoding="utf-8")
    return original


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("test_file")
    ap.add_argument("--base", required=True,
                    help="ref holding the ORIGINAL test file, e.g. b16949b")
    ap.add_argument("--mutations", required=True, help="JSON list from the mutation author")
    ap.add_argument("--worktree", default=str(REPO), help="tree holding the REWRITTEN test")
    ap.add_argument("--python", default=str(REPO / ".venv/bin/python"))
    ap.add_argument("--min-measured", type=int, default=None,
                    help="refuse a verdict below this many measured semantic "
                         "mutations. Declare it BEFORE the run; there is no default.")
    args = ap.parse_args()

    cwd = Path(args.worktree)
    test_path = cwd / args.test_file
    mutations = json.loads(Path(args.mutations).read_text(encoding="utf-8"))

    # A set with no semantic mutation makes every verdict below vacuous: the only
    # remaining rule is "the new test must stay green", which every test
    # satisfies, including one that asserts nothing. A tool built to catch
    # measurements that cannot fail would then produce a measurement that cannot
    # fail. Found by claude-linux-holzera-1 in the generator, by negative control
    # rather than by reading, and the same hole was open here.
    semantic = [m for m in mutations if m.get("kind", "semantic") == "semantic"]
    if not semantic:
        print(
            f"{args.mutations}: {len(mutations)} mutations and not one is semantic. "
            "Parity would pass anything, including a test that asserts nothing. "
            "Refusing to report a verdict.",
            file=sys.stderr,
        )
        raise SystemExit(NO_MEASUREMENT)

    new_text = test_path.read_text(encoding="utf-8")
    old_text = run("git", "show", f"{args.base}:{args.test_file}").stdout
    if not old_text:
        print(f"no {args.test_file} at {args.base}; nothing to compare against", file=sys.stderr)
        raise SystemExit(NO_MEASUREMENT)

    # Upper bound, checked before anything is broken: a test that is red on a
    # clean tree satisfies every parity row below for the wrong reason.
    if pytest_verdict(args.python, args.test_file, cwd) is not True:
        print(f"{args.test_file}: not green on unmutated source. Parity is "
              "meaningless until this passes.", file=sys.stderr)
        return NO_MEASUREMENT

    rows, regressions, shared_gaps, overspecified, unmeasured = [], [], [], [], []
    try:
        for mutation in mutations:
            source_original = apply_mutation(cwd, mutation)
            try:
                test_path.write_text(old_text, encoding="utf-8")
                old_green = pytest_verdict(args.python, args.test_file, cwd)
                test_path.write_text(new_text, encoding="utf-8")
                new_green = pytest_verdict(args.python, args.test_file, cwd)
            finally:
                (cwd / mutation["path"]).write_text(source_original, encoding="utf-8")

            kind = mutation.get("kind", "semantic")
            # Before any comparison: a mutation neither test could run against
            # is not a row in this table. Printed, never scored -- silently
            # dropping it would shrink the mutation set to whatever happened to
            # be runnable and call the remainder a parity result.
            if old_green is None or new_green is None:
                unmeasured.append(mutation["id"])
                rows.append((mutation["id"], kind,
                             "n/a" if old_green is None else ("green" if old_green else "RED"),
                             "n/a" if new_green is None else ("green" if new_green else "RED"),
                             "NO MEASUREMENT (pytest never ran a test)"))
                continue
            if kind == "equivalent":
                verdict = "OVER-SPECIFIED" if not new_green else "ok"
                if not new_green:
                    overspecified.append(mutation["id"])
            elif not old_green and new_green:
                verdict = "REGRESSION"
                regressions.append(mutation["id"])
            elif old_green and new_green:
                verdict = "SHARED GAP"
                shared_gaps.append(mutation["id"])
            elif old_green and not new_green:
                verdict = "ok (new is wider)"
            else:
                verdict = "ok"
            rows.append((mutation["id"], kind,
                         "green" if old_green else "RED",
                         "green" if new_green else "RED", verdict))
    finally:
        test_path.write_text(new_text, encoding="utf-8")

    print(f"\n{args.test_file}  vs {args.base}\n")
    print(f"  {'id':<10} {'kind':<11} {'old':<6} {'new':<6} verdict")
    for row in rows:
        print(f"  {row[0]:<10} {row[1]:<11} {row[2]:<6} {row[3]:<6} {row[4]}")

    # The DENOMINATOR, on the same line as the verdict and never off it.
    # claude-win-home-1 caught the first fix stopping one floor short: routing
    # 2-5 to NO MEASUREMENT fixed the misclassification but left the aggregate
    # blind, because "all semantic mutations unmeasured" is the loosest threshold
    # there is. Nine unmeasured out of ten is not "all", so the run exited 0 and
    # printed the same sentence a full ten-for-ten run prints. A verdict whose
    # wording is invariant to its own sample size is the shape we spent the day
    # removing -- one "PASSED" at 1% and at 49%.
    semantic_unmeasured = [m["id"] for m in semantic if m["id"] in set(unmeasured)]
    measured = len(semantic) - len(semantic_unmeasured)
    scale = f"{measured}/{len(semantic)} semantic mutations measured"

    print()
    if regressions:
        print(f"  BLOCKS: the new test is weaker on {regressions}   [{scale}]")
    if overspecified:
        print(f"  BLOCKS: the new test fails on behaviour-preserving change "
              f"{overspecified}   [{scale}]")
    if shared_gaps:
        print(f"  gaps BOTH tests share, not caused by this rewrite: {shared_gaps}")
    if unmeasured:
        print(f"  NO VERDICT on {unmeasured}: pytest never ran a test under those "
              "mutations, so they say nothing either way")
    if not (regressions or overspecified):
        print(f"  PARITY HELD   [{scale}]"
              + ("" if not semantic_unmeasured else
                 f"  -- on the {measured} that ran; the other "
                 f"{len(semantic_unmeasured)} were never compared"))

    if regressions or overspecified:
        return FINDING
    # A floor may be set, but only BEFORE the run. Choosing it after seeing how
    # many happened to run is picking the probe to fit its own result, which is
    # the failure this whole file exists to prevent. So there is no default:
    # without --min-measured the tool reports the denominator and judges nothing.
    if args.min_measured is not None and measured < args.min_measured:
        print(f"\n  below the floor declared before this run "
              f"(--min-measured {args.min_measured}); no verdict", file=sys.stderr)
        return NO_MEASUREMENT
    if measured == 0:
        return NO_MEASUREMENT
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
