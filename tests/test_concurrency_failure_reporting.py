"""The concurrency stress tests must say WHY they failed, not just how often.

`test_high_concurrency_no_corruption` gathers with `return_exceptions=True` and
then asserts on a count. Counting the exceptions and dropping them is how ten
copies of `sqlite3.DatabaseError: database disk image is malformed` reached CI
run 33413410950 (c58ade5, windows-latest, chunk 3/8) rendered as one line —
"Too many agent failures: 0/10 completed" — which names neither the error nor
its kind and reads like an overloaded runner.

These tests pin the two properties that stop it happening again: the failure
text carries the exceptions, and a damaged database file is not absorbed by the
success-rate floor that exists for slow runners.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from tests.test_concurrency_agents import (
    describe_gather_failures,
    fail_on_database_corruption,
)

_CI_SIGNATURE = "database disk image is malformed"


def _raised(exc: BaseException) -> BaseException:
    """Return `exc` carrying a real __traceback__, as gather would hand it back."""
    try:
        raise exc
    except BaseException as caught:
        return caught


def test_ten_identical_failures_are_grouped_and_named() -> None:
    """The exact CI shape: ten of one fault, reported as one line and a count."""
    results = [_raised(sqlite3.DatabaseError(_CI_SIGNATURE)) for _ in range(10)]

    described = describe_gather_failures(results)

    assert "10x DatabaseError" in described, described
    assert _CI_SIGNATURE in described, described
    # A place in the code, not only a string. This is what the old message could
    # not give, and it is why the CI failure needed a captured stdout to diagnose.
    assert "Traceback" in described, described


def test_successes_are_not_reported_as_failures() -> None:
    """Control: a clean gather result must not manufacture a failure report."""
    assert describe_gather_failures([["a"], ["b"], []]) == "no exceptions were raised"


def test_distinct_failures_are_listed_separately() -> None:
    """Two different faults must not collapse into one another's count."""
    results = [
        _raised(sqlite3.DatabaseError(_CI_SIGNATURE)),
        _raised(TimeoutError("send timed out")),
        _raised(TimeoutError("send timed out")),
    ]

    described = describe_gather_failures(results)

    assert "1x DatabaseError" in described, described
    assert "2x TimeoutError" in described, described
    assert "3 of 3 tasks raised" in described, described


def test_corruption_fails_the_run_whatever_the_success_rate() -> None:
    """One damaged-file error is fatal even among nine successes.

    The success floor exists so a loaded runner can still pass. Corruption is
    not the thing that floor was lowered for.
    """
    results: list[object] = [["ok"] for _ in range(9)]
    results.append(_raised(sqlite3.DatabaseError(_CI_SIGNATURE)))

    with pytest.raises(pytest.fail.Exception) as excinfo:
        fail_on_database_corruption(results)

    message = str(excinfo.value)
    assert "damaged database file" in message, message
    assert _CI_SIGNATURE in message, message


@pytest.mark.parametrize(
    "wording",
    [
        "database disk image is malformed",
        "file is not a database",
        "malformed database schema",
        "DATABASE DISK IMAGE IS MALFORMED",
    ],
)
def test_each_corruption_wording_is_caught(wording: str) -> None:
    """SQLite has several phrasings for a damaged file; none may slip through."""
    with pytest.raises(pytest.fail.Exception):
        fail_on_database_corruption([_raised(sqlite3.DatabaseError(wording))])


@pytest.mark.parametrize(
    "transient",
    [
        asyncio.CancelledError("cancelled under load"),
        TimeoutError("operation timed out"),
        sqlite3.OperationalError("database is locked"),
    ],
)
def test_transient_failures_do_not_trip_the_corruption_gate(
    transient: BaseException,
) -> None:
    """Control, and the load-bearing half.

    Without it this file would also pass against a gate that fails on every
    exception, which would make the stress tests' tolerance for a busy runner
    useless. `database is locked` is contention, not damage, and is listed here
    on purpose because it is the near miss.
    """
    fail_on_database_corruption([_raised(transient)])
