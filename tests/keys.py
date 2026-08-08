"""Host-neutral opaque project keys used by server tests."""

from __future__ import annotations

from pathlib import PurePosixPath


def pkey(name: str) -> str:
    """Return the canonical POSIX-form project key for ``name``."""
    return str(PurePosixPath("/") / name)
