"""Scan for coverage and lint exclusion comments.

Detects pragma: no cover, noqa, type: ignore, and similar bypass comments
that may hide untested code or suppress legitimate linting warnings.
"""

import re
import sys
from pathlib import Path

PYTHON_PATTERNS: dict[str, re.Pattern[str]] = {
    "pragma: no cover": re.compile(r"#\s*pragma:\s*no\s*cover", re.IGNORECASE),
    "noqa": re.compile(r"#\s*noqa(?::\s*\w+)?"),
    "type: ignore": re.compile(r"#\s*type:\s*ignore(?:\[[\w,\s-]+\])?"),
    "pylint: disable": re.compile(r"#\s*pylint:\s*disable"),
    "mypy: ignore": re.compile(r"#\s*mypy:\s*ignore"),
    "coverage: exclude": re.compile(r"#\s*coverage:\s*exclude"),
    "fmt: off/skip": re.compile(r"#\s*fmt:\s*(off|skip)"),
    "isort: skip": re.compile(r"#\s*isort:\s*skip"),
    "ruff: noqa": re.compile(r"#\s*ruff:\s*noqa"),
}
TS_PATTERNS: dict[str, re.Pattern[str]] = {
    "istanbul ignore": re.compile(r"/\*\s*istanbul\s+ignore\s+(next|else|file)", re.IGNORECASE),
    "c8 ignore": re.compile(r"/\*\s*c8\s+ignore\s+(start|stop|next)", re.IGNORECASE),
    "v8 ignore": re.compile(r"/\*\s*v8\s+ignore\s+(start|stop|next)", re.IGNORECASE),
    "eslint-disable": re.compile(r"//\s*eslint-disable(?:-next-line|-line)?"),
    "eslint-disable block": re.compile(r"/\*\s*eslint-disable"),
    "@ts-ignore": re.compile(r"//\s*@ts-ignore"),
    "@ts-expect-error": re.compile(r"//\s*@ts-expect-error"),
    "@ts-nocheck": re.compile(r"//\s*@ts-nocheck"),
    "prettier-ignore": re.compile(r"//\s*prettier-ignore"),
    "biome-ignore": re.compile(r"//\s*biome-ignore"),
}
SKIP_DIRS = {
    ".venv",
    "node_modules",
    "__pycache__",
    ".git",
    "dist",
    "build",
    "coverage",
    ".pytest_cache",
}
SKIP_FILES = {
    "test_check_coverage_exclusions.py",
}


def find_exclusions_in_file(
    filepath: Path,
    patterns: dict[str, re.Pattern[str]],
) -> list[tuple[int, str, str]]:
    """Find exclusion comments in a file matching given patterns.

    Args:
        filepath: Path to the file to scan.
        patterns: Dictionary mapping pattern names to compiled regex patterns.

    Returns:
        List of tuples containing line number, pattern name, and line content.
    """
    findings: list[tuple[int, str, str]] = []
    try:
        content = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return findings
    for line_num, line in enumerate(content.splitlines(), start=1):
        for pattern_name, pattern in patterns.items():
            if pattern.search(line):
                findings.append((line_num, pattern_name, line.strip()))
                break
    return findings


def scan_python_files(root: Path) -> dict[Path, list[tuple[int, str, str]]]:
    """Scan Python files for exclusion patterns.

    Args:
        root: Root directory to scan recursively.

    Returns:
        Dictionary mapping file paths to their list of findings.
    """
    results: dict[Path, list[tuple[int, str, str]]] = {}
    for py_file in root.rglob("*.py"):
        if any(skip in py_file.parts for skip in SKIP_DIRS):
            continue
        if py_file.name in SKIP_FILES:
            continue
        findings = find_exclusions_in_file(py_file, PYTHON_PATTERNS)
        if findings:
            results[py_file] = findings
    return results


def scan_typescript_files(root: Path) -> dict[Path, list[tuple[int, str, str]]]:
    """Scan TypeScript/JavaScript files for exclusion patterns.

    Args:
        root: Root directory to scan recursively.

    Returns:
        Dictionary mapping file paths to their list of findings.
    """
    results: dict[Path, list[tuple[int, str, str]]] = {}
    for pattern in ("*.ts", "*.tsx", "*.js", "*.jsx"):
        for ts_file in root.rglob(pattern):
            if any(skip in ts_file.parts for skip in SKIP_DIRS):
                continue
            if ".generated." in ts_file.name:
                continue
            findings = find_exclusions_in_file(ts_file, TS_PATTERNS)
            if findings:
                results[ts_file] = findings
    return results


def print_results(
    results: dict[Path, list[tuple[int, str, str]]],
    root: Path,
    file_type: str,
) -> int:
    """Print scan results and return total count of findings.

    Args:
        results: Dictionary mapping file paths to their findings.
        root: Root directory for computing relative paths.
        file_type: Type of files being reported (for display purposes).

    Returns:
        Total count of exclusion findings.
    """
    if not results:
        print(f"  No exclusions found in {file_type} files")
        return 0
    total = 0
    for filepath, findings in sorted(results.items()):
        rel_path = filepath.relative_to(root).as_posix()
        print(f"\n  {rel_path}")
        for line_num, pattern_name, line_content in findings:
            print(f"     L{line_num}: [{pattern_name}]")
            display_line = line_content[:80] + "..." if len(line_content) > 80 else line_content
            print(f"        {display_line}")
            total += 1
    return total


def run_scan(
    root: Path,
    strict_mode: bool = False,
) -> int:
    """Run the full exclusion scan and return exit code.

    Args:
        root: Root directory of the project to scan.
        strict_mode: If True, return exit code 1 when exclusions are found.

    Returns:
        Exit code (0 for success, 1 for failure in strict mode with findings).
    """
    print("=" * 70)
    print("Coverage/Lint Exclusion Scanner")
    print("=" * 70)
    print(f"\nScanning: {root}")
    print(f"Mode: {'STRICT (will fail on findings)' if strict_mode else 'Report only'}")
    print("\n" + "-" * 70)
    print("PYTHON FILES (.py)")
    print("-" * 70)
    py_results = scan_python_files(root / "src")
    py_results.update(scan_python_files(root / "tests"))
    py_results.update(scan_python_files(root / "scripts"))
    if (root / "proprietary" / "src").exists():
        py_results.update(scan_python_files(root / "proprietary" / "src"))
    if (root / "proprietary" / "tests").exists():
        py_results.update(scan_python_files(root / "proprietary" / "tests"))
    py_count = print_results(py_results, root, "Python")
    print("\n" + "-" * 70)
    print("TYPESCRIPT FILES (.ts/.tsx)")
    print("-" * 70)
    ts_results = scan_typescript_files(root / "frontend")
    ts_count = print_results(ts_results, root, "TypeScript")
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total = py_count + ts_count
    print(f"\n  Python exclusions:     {py_count}")
    print(f"  TypeScript exclusions: {ts_count}")
    print(f"  Total:                 {total}")
    if total > 0:
        print("\nFound exclusion comments that bypass coverage/lint checks.")
        print("   Review these to ensure they are justified in a TDD project.")
        if strict_mode:
            print("\nSTRICT MODE: Failing due to exclusions found.")
            return 1
    else:
        print("\nNo coverage/lint exclusions found. Clean TDD codebase!")
    return 0


def main() -> int:
    """Entry point for check_coverage_exclusions script.

    Returns:
        Exit code from the scan operation.
    """
    strict_mode = "--strict" in sys.argv
    script_dir = Path(__file__).parent
    root = script_dir.parent
    return run_scan(root, strict_mode)


if __name__ == "__main__":
    raise SystemExit(main())
