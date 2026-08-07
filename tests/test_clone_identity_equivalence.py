import subprocess
from pathlib import Path

from mcp_agent_mail.app import _resolve_project_identity
from mcp_agent_mail.config import get_settings


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return cp.stdout.strip()


SHARED_REMOTE = "git@github.com:example-org/shared-repo.git"
OTHER_REMOTE = "git@github.com:example-org/other-repo.git"


def _clone_with_remote(tmp_path: Path, name: str, remote_url: str | None) -> Path:
    """Clone the local bare repo, optionally pointing origin at a real URL.

    The clone must be local — there is no network here — but the remote the
    identity is derived from must not be. `_normalize_git_remote` produces a
    privacy-safe ``host/owner/repo`` and returns None for anything lacking
    those parts, so a filesystem path normalizes to nothing at all. Passing
    None here keeps the path remote, which is what the second test needs.
    """
    _git(tmp_path, "clone", str(tmp_path / "remote.git"), str(tmp_path / name))
    clone = tmp_path / name
    if remote_url is not None:
        _git(clone, "remote", "set-url", "origin", remote_url)
    return clone


def test_clones_share_same_project_uid_via_remote(tmp_path: Path, monkeypatch) -> None:
    """Two clones of one remote resolve to one project; a third does not.

    This used to clone a local bare repo and compare the two identities
    directly. It could not pass: the remote was a filesystem path, so
    ``normalized_remote`` was None on both sides and the uid fell back to
    something per-directory. The equality in front of it — comparing the two
    ``normalized_remote`` values — passed on ``None == None``, so the test
    reported that it had read the remote while proving only that it had not.

    Hence the explicit non-None assertion below. Every equality here is
    satisfied by two clones whose remote could not be resolved at all, which
    is exactly the state this test was in.
    """
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    get_settings.cache_clear()

    _git(tmp_path, "init", "--bare", str(tmp_path / "remote.git"))
    c1 = _clone_with_remote(tmp_path, "clone1", SHARED_REMOTE)
    c2 = _clone_with_remote(tmp_path, "clone2", SHARED_REMOTE)
    c3 = _clone_with_remote(tmp_path, "clone3", OTHER_REMOTE)

    id1 = _resolve_project_identity(str(c1))
    id2 = _resolve_project_identity(str(c2))
    id3 = _resolve_project_identity(str(c3))

    assert id1["normalized_remote"] == "github.com/example-org/shared-repo"

    assert id1["normalized_remote"] == id2["normalized_remote"]
    assert id1["project_uid"] == id2["project_uid"]

    # Different remote, different project — otherwise "same uid" would also be
    # satisfied by a uid that ignores the remote entirely, which is precisely
    # what the path-remote version was measuring.
    assert id3["normalized_remote"] != id1["normalized_remote"]
    assert id3["project_uid"] != id1["project_uid"]


def test_path_remote_yields_no_shared_identity(tmp_path: Path, monkeypatch) -> None:
    """A remote with no host/owner/repo shares nothing, and that is deliberate.

    Pinned because it is the state the test above was accidentally in, and the
    natural "fix" for a future failure is to point it back at the bare repo.

    Measured both ways before writing this: identity_mode makes no difference
    to either outcome, so the shape of the remote is the whole cause.

        remote                mode=git-remote   mode=dir
        git@github.com:o/r     same uid          same uid
        /tmp/remote.git        different         different
    """
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    get_settings.cache_clear()

    _git(tmp_path, "init", "--bare", str(tmp_path / "remote.git"))
    c1 = _clone_with_remote(tmp_path, "clone1", None)
    c2 = _clone_with_remote(tmp_path, "clone2", None)

    id1 = _resolve_project_identity(str(c1))
    id2 = _resolve_project_identity(str(c2))

    assert id1["normalized_remote"] is None
    assert id1["project_uid"] != id2["project_uid"]
