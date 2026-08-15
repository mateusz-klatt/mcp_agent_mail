"""The backup manifest version field must be an integer, and `bool` is not one.

`isinstance(True, int)` is true in Python, so a manifest carrying
`"version": true` passed the integer check and then compared equal to 1 -- a
payload that never named a version restored as though it had named version 1.
The range test cannot catch it either, because `True < 1` is false.

Each test carries the control that makes it a measurement: a real integer must
still be accepted, or a guard that rejected everything would read as a pass.
"""

from __future__ import annotations

import pytest

from mcp_agent_mail.storage import _parse_backup_manifest

_COMPLETE = {
    "version": 1,
    "created_at": "2026-08-16T00:00:00Z",
    "reason": "test",
    "database_path": "storage.sqlite3",
    "project_bundles": [],
    "storage_root": "/tmp/backup-root",
    "restore_instructions": "restore by hand",
}


def _manifest(**overrides: object) -> dict[str, object]:
    return {**_COMPLETE, **overrides}


def test_a_boolean_version_is_not_an_integer_version() -> None:
    with pytest.raises(ValueError, match="integer version"):
        _parse_backup_manifest(_manifest(version=True))


def test_the_same_manifest_with_a_real_integer_is_accepted() -> None:
    # Positive control for the test above: the rejection has to be about the
    # bool, not about some other field in the payload.
    assert _parse_backup_manifest(_manifest(version=1)).version == 1


def test_false_is_rejected_like_the_zero_it_equals() -> None:
    with pytest.raises(ValueError, match="integer version"):
        _parse_backup_manifest(_manifest(version=False))


@pytest.mark.parametrize("version", ["1", 0, -1, None, 1.0])
def test_non_integer_and_out_of_range_versions_stay_rejected(version: object) -> None:
    with pytest.raises(ValueError, match="integer version"):
        _parse_backup_manifest(_manifest(version=version))
