"""Supply-chain and runtime-boundary contracts for GitHub Actions workflows."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
USES_LINE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<target>[^#\s]+)\s*(?:#\s*(?P<comment>.*))?$"
)
IMMUTABLE_ACTION_REF = re.compile(r"[0-9a-f]{40}")
VERSION_COMMENT = re.compile(r"v\d+(?:\.\d+){1,2}")
BOUNDED_JOBS = {
    "ci.yml": ("build-and-test", 30),
    "sonarcloud.yml": ("scan", 15),
}


def _read_workflow(path: Path) -> str:
    raw = path.read_bytes()
    assert b"\x00" not in raw, f"{path.name} contains a NUL byte"
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _workflow_paths() -> list[Path]:
    paths = sorted(WORKFLOWS_DIR.glob("*.yml"))
    assert paths, "no GitHub Actions workflows found"
    return paths


def _job_block(workflow: str, job_name: str) -> list[str]:
    lines = workflow.splitlines()
    try:
        jobs_index = lines.index("jobs:")
        start = lines.index(f"  {job_name}:", jobs_index + 1)
    except ValueError as exc:
        raise AssertionError(f"job {job_name!r} was not found") from exc

    block: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            break
        block.append(line)
    return block


def test_external_actions_use_immutable_commits() -> None:
    failures: list[str] = []
    for path in _workflow_paths():
        for line_number, line in enumerate(_read_workflow(path).splitlines(), start=1):
            match = USES_LINE.fullmatch(line)
            if match is None:
                continue
            target = match.group("target")
            if target.startswith("./"):
                continue
            action, separator, ref = target.rpartition("@")
            location = f"{path.name}:{line_number}"
            if not action or not separator or IMMUTABLE_ACTION_REF.fullmatch(ref) is None:
                failures.append(f"{location}: {target!r} is not pinned to a lowercase 40-hex commit")
                continue
            comment = (match.group("comment") or "").strip()
            if VERSION_COMMENT.fullmatch(comment) is None:
                failures.append(f"{location}: pinned action is missing a version comment")

    assert not failures, "\n".join(failures)


def test_long_running_jobs_have_finite_timeouts() -> None:
    for workflow_name, (job_name, expected_timeout) in BOUNDED_JOBS.items():
        workflow = _read_workflow(WORKFLOWS_DIR / workflow_name)
        block = _job_block(workflow, job_name)
        timeouts = [
            int(match.group(1))
            for line in block
            if (match := re.fullmatch(r"    timeout-minutes:\s*([1-9]\d*)\s*", line))
        ]
        assert timeouts == [expected_timeout], (
            f"{workflow_name} job {job_name!r} must have one finite "
            f"timeout-minutes value of {expected_timeout}"
        )
