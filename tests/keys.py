"""Project keys that are absolute on the platform running the test.

``ensure_project`` requires ``Path(human_key).is_absolute()``. A literal like
``"/backend"`` satisfies that on POSIX and fails it on Windows, where an absolute
path needs a drive letter. The validation is right — the key is meant to be an
agent's working directory, and its own error message spells out the Windows form
— so the literals in the fixtures are what is wrong.

The cost of leaving them is not a red suite but a blind one: the call raises
inside ``ensure_project``, so a test that starts this way dies before touching
its subject, identically on every commit. A regression underneath it is then
invisible to a HEAD-vs-base diff, because a test that already fails for one
reason fails the same way afterwards. Measured on Windows: all 25 failures in
test_server.py and all 17 across the files converted here had this single cause
and no other.
"""

from __future__ import annotations

import os
from pathlib import Path


def pkey(name: str) -> str:
    """Return an absolute project key for ``name`` ("backend", "test/names").

    Named ``pkey``, not ``project_key``, because the tests pass the same string
    under two dict keys — ``human_key`` when creating the project and
    ``project_key`` when addressing it afterwards. A helper sharing a name with
    one of them invites converting only that one, and the second call would then
    address a project the first never created.
    """
    if os.name != "nt":
        return f"/{name}"
    return str(Path(os.environ.get("SystemDrive", "C:") + "/") / Path(name))
