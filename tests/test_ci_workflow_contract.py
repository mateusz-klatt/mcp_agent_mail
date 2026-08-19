"""Supply-chain and runtime-boundary contracts for GitHub Actions workflows."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
REPOSITORY_ROOT = WORKFLOWS_DIR.parents[1]
USES_LINE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<target>[^#\s]+)\s*(?:#\s*(?P<comment>.*))?$"
)
IMMUTABLE_ACTION_REF = re.compile(r"[0-9a-f]{40}")
VERSION_COMMENT = re.compile(r"v\d+(?:\.\d+){1,2}")
BOUNDED_JOBS = (
    ("ci.yml", "build-and-test", 120),
    ("ci.yml", "portability", 180),
    ("sonarcloud.yml", "scan", 15),
    ("release.yml", "validate-release", 15),
    ("release.yml", "build-candidates", 120),
    ("release.yml", "container-smoke", 90),
    ("release.yml", "publish", 120),
)


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


def _job_permissions(workflow: str, job_name: str) -> dict[str, str]:
    block = _job_block(workflow, job_name)
    try:
        start = block.index("    permissions:")
    except ValueError:
        return {}

    permissions: dict[str, str] = {}
    for line in block[start + 1 :]:
        match = re.fullmatch(r"      ([a-z-]+):\s*(read|write|none)\s*", line)
        if match is None:
            break
        permissions[match.group(1)] = match.group(2)
    return permissions


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
    for workflow_name, job_name, expected_timeout in BOUNDED_JOBS:
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


def test_sonar_uses_ubuntu_gate_while_portability_remains_required() -> None:
    workflow = _read_workflow(WORKFLOWS_DIR / "ci.yml")
    ubuntu = _job_block(workflow, "build-and-test")
    portability = _job_block(workflow, "portability")
    sonar = _job_block(workflow, "sonar")

    windows = _job_block(workflow, "portability-windows")
    windows_summary = _job_block(workflow, "portability-windows-summary")

    visible_check_name = "    name: Lint, Type Check, Test (${{ matrix.os }}, py${{ matrix.python-version }})"
    assert visible_check_name in ubuntu
    assert visible_check_name in portability
    assert "        os: [ubuntu-latest]" in ubuntu
    assert "        os: [macos-latest]" in portability
    assert "      fail-fast: false" in portability
    assert not any(line.startswith("    continue-on-error:") for line in portability)

    # Windows runs as eight parallel chunks. The full leg needed ~4.5 h and was
    # killed by the ceiling with no output at all, so a failure anywhere was
    # invisible; the split is what makes failures reportable. fail-fast must
    # stay off or one failing chunk would hide the other seven.
    assert (
        "    name: Lint, Type Check, Test (windows-latest, py${{ matrix.python-version }}, chunk ${{ matrix.group }}/8)"
        in windows
    )
    assert "        group: [1, 2, 3, 4, 5, 6, 7, 8]" in windows
    assert "      fail-fast: false" in windows
    assert not any(line.startswith("    continue-on-error:") for line in windows)
    assert any(
        line.strip() == "--durations=50 --splits 8 --group ${{ matrix.group }}"
        for line in windows
    )

    # The summary job deliberately carries the name the single Windows job used
    # to have, so branch protection requiring that exact check keeps working.
    assert "    name: Lint, Type Check, Test (windows-latest, py3.14)" in windows_summary
    assert "    needs: [portability-windows]" in windows_summary
    assert not any(
        line.startswith("    continue-on-error:") for line in windows_summary
    )

    common_commands = (
        "uv sync --dev --frozen",
        "uv run ruff check",
        'uvx --from "ty==${TY_VERSION}" ty check --python-version 3.14 --python-platform all',
    )
    for command in common_commands:
        assert any(line.strip() == command for line in ubuntu)
        assert any(line.strip() == command for line in portability)

    assert any(
        line.strip()
        == "uv run -m pytest -q --cov-report=xml:coverage.xml --timeout=300 --timeout-method=thread --durations=50"
        for line in ubuntu
    )
    assert any(
        line.strip()
        == "uv run -m pytest -q --no-cov --timeout=300 --timeout-method=thread --durations=50"
        for line in portability
    )
    assert not any("--cov-report" in line for line in portability)
    assert any("tests/benchmarks/bench_*.py" in line for line in ubuntu)
    assert any("uv run python scripts/integration_showcase.py" in line for line in ubuntu)
    assert "          name: coverage-${{ github.sha }}" in ubuntu
    assert not any("coverage-${{ github.sha }}" in line for line in portability)
    assert "    needs: [build-and-test, frontend, frontend-webkit]" in sonar
    assert not any("portability" in line for line in sonar)
    assert "        needs.build-and-test.result == 'success' &&" in sonar
    assert "        needs.frontend.result == 'success' &&" in sonar
    assert "        needs.frontend-webkit.result == 'success' &&" in sonar


def test_reusable_sonar_caller_grants_requested_permissions() -> None:
    caller = _read_workflow(WORKFLOWS_DIR / "ci.yml")
    called = _read_workflow(WORKFLOWS_DIR / "sonarcloud.yml")
    caller_permissions = _job_permissions(caller, "sonar")
    called_permissions = _job_permissions(called, "scan")

    assert 'git ls-remote origin "$REMOTE_REF" "${REMOTE_REF}^{}"' in called

    for permission, requested_access in called_permissions.items():
        if requested_access == "none":
            continue
        granted_access = caller_permissions.get(permission, "none")
        assert granted_access == "write" or granted_access == requested_access, (
            f"reusable Sonar job requests {permission}: {requested_access}, but "
            f"the caller grants {granted_access}"
        )


def test_ty_version_is_explicit_and_shared_by_ci_and_makefile() -> None:
    workflow = _read_workflow(WORKFLOWS_DIR / "ci.yml")
    makefile = (REPOSITORY_ROOT / "Makefile").read_text()

    assert '  TY_VERSION: "0.0.71"' in workflow
    assert "TY_VERSION=0.0.71" in makefile
    assert 'uvx --from "ty==$(TY_VERSION)" ty check --python-version 3.14 --python-platform all' in makefile


def test_release_reuses_exact_sha_gates_before_publication() -> None:
    workflow = _read_workflow(WORKFLOWS_DIR / "release.yml")
    quality_gate = _job_block(workflow, "quality-gate")
    secret_gate = _job_block(workflow, "secret-gate")
    validation = _job_block(workflow, "validate-release")
    candidates = _job_block(workflow, "build-candidates")
    smoke = _job_block(workflow, "container-smoke")
    publish = _job_block(workflow, "publish")

    assert "    uses: ./.github/workflows/ci.yml" in quality_gate
    assert "    secrets: inherit" in quality_gate
    assert "    uses: ./.github/workflows/secret-scan.yml" in secret_gate
    assert "    needs: [quality-gate, secret-gate]" in validation
    assert "          ref: ${{ github.sha }}" in validation
    assert "        run: uv sync --dev --frozen" in validation
    assert any("git rev-parse HEAD" in line for line in validation)
    assert any("pyproject.toml version" in line for line in validation)
    assert any("Ensure the Docker Hub repository is public" in line for line in validation)
    assert any("https://hub.docker.com/v2/auth/token" in line for line in validation)
    assert any(
        "https://hub.docker.com/v2/namespaces/klattm/repositories/iris" in line
        for line in validation
    )
    assert sum('--oauth2-bearer "$hub_token"' in line for line in validation) == 2
    assert any('"$status" == "404"' in line for line in validation)
    assert any("is_private: false" in line for line in validation)

    assert "    needs: validate-release" in candidates
    assert "      packages: write" in candidates
    assert "      amd64_digest: ${{ steps.build-amd64.outputs.digest }}" in candidates
    assert "      arm64_digest: ${{ steps.build-arm64.outputs.digest }}" in candidates
    assert sum("docker/build-push-action@" in line for line in candidates) == 2
    assert "          platforms: linux/amd64" in candidates
    assert "          platforms: linux/arm64" in candidates
    assert sum("push-by-digest=true" in line for line in candidates) == 2
    assert sum("name-canonical=true" in line for line in candidates) == 2
    assert sum("provenance: false" in line for line in candidates) == 2
    assert sum("sbom: false" in line for line in candidates) == 2
    assert not any(re.fullmatch(r"\s+tags:\s+.+", line) for line in candidates)

    assert "    needs: [validate-release, build-candidates]" in smoke
    assert "      packages: read" in smoke
    assert any('IMAGE: ${{ needs.build-candidates.outputs.registry_image }}' in line for line in smoke)
    assert any('AMD64_DIGEST: ${{ needs.build-candidates.outputs.amd64_digest }}' in line for line in smoke)
    assert any('ARM64_DIGEST: ${{ needs.build-candidates.outputs.arm64_digest }}' in line for line in smoke)
    assert any('docker pull --quiet --platform "$platform" "$candidate"' in line for line in smoke)
    assert any('pulled_repo_digests="$(docker image inspect' in line for line in smoke)
    assert any('--arg expected "$candidate"' in line for line in smoke)
    assert any("index($expected) != null" in line for line in smoke)
    assert any("smoke_candidate linux/amd64" in line for line in smoke)
    assert any("smoke_candidate linux/arm64" in line for line in smoke)
    assert any("mcp_agent_mail.cli migrate" in line for line in smoke)
    assert any("/health/liveness" in line for line in smoke)
    assert any("/health/readiness" in line for line in smoke)
    assert not any("docker/build-push-action@" in line for line in smoke)

    assert "    needs: [validate-release, build-candidates, container-smoke]" in publish
    assert any("docker/login-action" in line for line in publish)
    assert any("type=sha,format=long,prefix=sha-" in line for line in publish)
    assert any("docker buildx imagetools create" in line for line in publish)
    assert any('"$IMAGE@$AMD64_DIGEST"' in line for line in publish)
    assert any('"$IMAGE@$ARM64_DIGEST"' in line for line in publish)
    assert any("Candidate output is not an exact sha256 digest" in line for line in publish)
    assert any(".platform.architecture == \"amd64\"" in line for line in publish)
    assert any(".platform.architecture == \"arm64\"" in line for line in publish)
    assert any(".manifests | length" in line for line in publish)
    assert any('"$tag_digest" != "$manifest_digest"' in line for line in publish)
    assert any("steps.promote.outputs.digest" in line for line in publish)
    assert any("Verify release tags are anonymously pullable" in line for line in publish)
    assert any('printf \'{"auths":{}}\\n\'' in line for line in publish)
    assert any('DOCKER_CONFIG="$anonymous_docker_config"' in line for line in publish)
    assert any("New GHCR packages are private by default" in line for line in publish)
    assert any('"$tag_digest" != "$PROMOTED_DIGEST"' in line for line in publish)
    assert not any("docker/build-push-action@" in line for line in publish)

    all_release_builds = [
        line for line in workflow.splitlines() if "docker/build-push-action@" in line
    ]
    assert len(all_release_builds) == 2
    assert sum("docker/metadata-action@" in line for line in workflow.splitlines()) == 1


def test_secret_scan_pins_binary_and_gates_current_tree() -> None:
    workflow = _read_workflow(WORKFLOWS_DIR / "secret-scan.yml")

    assert "  workflow_call:" in workflow
    assert 'version="8.18.4"' in workflow
    assert "ba6dbb656933921c775ee5a2d1c13a91046e7952e9d919f9bac4cec61d628e7d" in workflow
    assert "Gitleaks failed to reject its synthetic GitHub-token canary" in workflow
    current_tree = workflow.index("gitleaks detect --no-git --source .")
    report_only = workflow.index("continue-on-error: true")
    assert current_tree < report_only
    assert "gitleaks detect --source ." in workflow
