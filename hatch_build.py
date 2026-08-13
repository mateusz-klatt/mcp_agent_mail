"""Build the Iris browser bundle into Python distributions.

The repository deliberately does not track generated ``ui/dist`` files.  A
source build therefore compiles the UI in an isolated, validated temporary
directory and force-includes the result in the distribution.  ``uv build``
creates the wheel from the generated sdist, so that second stage validates and
reuses the bundled assets without requiring Node again.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple
from zipfile import BadZipFile, ZipFile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_NODE_VERSION = "v22.22.2"
_NPM_VERSION = "10.9.7"
_STAGE_PREFIX = "mcp-agent-mail-ui-build-"
_STAGE_SENTINEL = ".mcp-agent-mail-ui-stage"
_BUILD_MANIFEST = ".hermes-ui-build.json"
_UI_DIST_SOURCE = Path("src/mcp_agent_mail/ui_dist")
_UI_DIST_WHEEL = "mcp_agent_mail/ui_dist"
_UI_DIST_SDIST = _UI_DIST_SOURCE.as_posix()
_DIST_HASHES_KEY = "files"
_VITE_MANIFEST = ".vite/manifest.json"
_FINGERPRINTED_ASSET_RE = re.compile(r"^assets/[A-Za-z0-9_.-]+-[A-Za-z0-9_-]{8,}\.(?:css|js)$")
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_ESCAPE_RE = re.compile(
    r"\\(?:(?P<hex>[0-9A-Fa-f]{1,6})(?:\r\n|[ \t\r\n\f])?"
    r"|(?P<newline>\r\n|[\r\n\f])|(?P<char>.))",
    re.DOTALL,
)
_CSS_URL_FUNCTION_RE = re.compile(r"url[ \t\r\n\f]*\(", re.IGNORECASE)
_EXPECTED_LOCALE_MANIFEST_KEYS = (
    "src/locales/ar.ts",
    "src/locales/bn.ts",
    "src/locales/bs.ts",
    "src/locales/cs.ts",
    "src/locales/da.ts",
    "src/locales/de.ts",
    "src/locales/el.ts",
    "src/locales/es.ts",
    "src/locales/fa.ts",
    "src/locales/fi.ts",
    "src/locales/fil.ts",
    "src/locales/fr.ts",
    "src/locales/ga.ts",
    "src/locales/he.ts",
    "src/locales/hi.ts",
    "src/locales/hr.ts",
    "src/locales/hu.ts",
    "src/locales/hy.ts",
    "src/locales/id.ts",
    "src/locales/is.ts",
    "src/locales/it.ts",
    "src/locales/ja.ts",
    "src/locales/ko.ts",
    "src/locales/lt.ts",
    "src/locales/lv.ts",
    "src/locales/ms.ts",
    "src/locales/my-MM.ts",
    "src/locales/nl.ts",
    "src/locales/no.ts",
    "src/locales/pl.ts",
    "src/locales/pt.ts",
    "src/locales/ro.ts",
    "src/locales/ru.ts",
    "src/locales/sk.ts",
    "src/locales/sq.ts",
    "src/locales/sr.ts",
    "src/locales/sv.ts",
    "src/locales/sw.ts",
    "src/locales/th.ts",
    "src/locales/tr.ts",
    "src/locales/uk.ts",
    "src/locales/vi.ts",
    "src/locales/zh-Hant.ts",
    "src/locales/zh.ts",
)
_WINDOWS_CHILD_ENVIRONMENT_KEYS = ("SystemRoot", "ComSpec", "PATHEXT")
_IGNORED_UI_DIRECTORIES = frozenset(
    {
        ".cache",
        "coverage",
        "dist",
        "node_modules",
        "playwright-report",
        "test-results",
    }
)


class HermesUiBuildError(RuntimeError):
    """The generated browser bundle is missing, unsafe, or not reproducible."""


class _StageDirectory(NamedTuple):
    """Identity facts proving that a cleanup target is the directory we made."""

    path: Path
    device: int
    inode: int
    sentinel: str


class _EntryPointReferenceCollector(HTMLParser):
    """Collect browser-active URLs from the generated Vite entry point."""

    def __init__(self) -> None:
        super().__init__()
        self.references: list[tuple[str, dict[str, str | None]]] = []
        self.duplicate_attributes: set[tuple[str, str]] = set()
        self.inline_active_content: set[str] = set()
        self._active_text_elements: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attribute_names = [name for name, _value in attrs]
        duplicate_names = {name for name in attribute_names if attribute_names.count(name) > 1}
        if duplicate_names:
            self.duplicate_attributes.update((tag, name) for name in duplicate_names)
            return
        attributes = dict(attrs)
        self.references.append((tag, attributes))
        if tag in {"script", "style"}:
            self._active_text_elements.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._active_text_elements and self._active_text_elements[-1] == tag:
            self._active_text_elements.pop()

    def handle_data(self, data: str) -> None:
        if self._active_text_elements and data.strip():
            self.inline_active_content.add(self._active_text_elements[-1])

    def handle_comment(self, data: str) -> None:
        if self._active_text_elements and data.strip():
            self.inline_active_content.add(self._active_text_elements[-1])


def _allowlisted_windows_environment(
    host_environment: Mapping[str, str],
    *,
    platform_name: str,
) -> dict[str, str]:
    """Return only the Windows process variables required to launch Node."""
    if platform_name != "nt":
        return {}
    return {name: host_environment[name] for name in _WINDOWS_CHILD_ENVIRONMENT_KEYS if name in host_environment}


def _iter_build_inputs(repository_root: Path) -> list[Path]:
    """Return every source file that can influence the two Vite builds."""
    sources: list[Path] = []
    ui_root = repository_root / "ui"
    templates_root = repository_root / "src" / "mcp_agent_mail" / "templates"
    viewer_index = repository_root / "src" / "mcp_agent_mail" / "viewer_assets" / "index.html"

    for search_root in (ui_root, templates_root):
        if not search_root.is_dir() or search_root.is_symlink():
            raise HermesUiBuildError(f"Required UI source directory is unavailable: {search_root}")
        for candidate in sorted(search_root.rglob("*")):
            relative_parts = candidate.relative_to(search_root).parts
            if search_root == ui_root and any(part in _IGNORED_UI_DIRECTORIES for part in relative_parts):
                continue
            if candidate.is_symlink():
                raise HermesUiBuildError(f"UI build inputs must not contain symlinks: {candidate}")
            if candidate.is_file():
                sources.append(candidate)

    if not viewer_index.is_file() or viewer_index.is_symlink():
        raise HermesUiBuildError(f"Required viewer source is unavailable: {viewer_index}")
    sources.append(viewer_index)
    return sorted(sources, key=lambda path: path.relative_to(repository_root).as_posix())


def _source_digest(repository_root: Path) -> str:
    """Hash build inputs with their repository-relative paths."""
    digest = hashlib.sha256()
    for source in _iter_build_inputs(repository_root):
        relative = source.relative_to(repository_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        contents = source.read_bytes()
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _create_stage_directory(repository_root: Path) -> _StageDirectory:
    """Create one owned stage directly beneath the operating-system temp root."""
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    stage_path = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX, dir=temp_root)).resolve(strict=True)
    if stage_path.parent != temp_root or not stage_path.name.startswith(_STAGE_PREFIX):
        raise HermesUiBuildError(f"Refusing unexpected UI staging path: {stage_path}")
    if stage_path == repository_root or stage_path.is_relative_to(repository_root):
        raise HermesUiBuildError(f"UI staging path must be outside the repository: {stage_path}")

    sentinel = os.urandom(32).hex()
    (stage_path / _STAGE_SENTINEL).write_text(sentinel, encoding="ascii")
    identity = stage_path.lstat()
    return _StageDirectory(
        path=stage_path,
        device=identity.st_dev,
        inode=identity.st_ino,
        sentinel=sentinel,
    )


def _validate_cleanup_target(stage: _StageDirectory, repository_root: Path) -> None:
    """Fail closed unless ``stage`` is still the exact temporary directory we made."""
    stage_path = stage.path
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    repository_root = repository_root.resolve(strict=True)
    if stage_path.is_symlink() or not stage_path.is_dir():
        raise HermesUiBuildError(f"Refusing to clean a missing or symlinked UI stage: {stage_path}")
    resolved_stage = stage_path.resolve(strict=True)
    if resolved_stage != stage_path or resolved_stage.parent != temp_root:
        raise HermesUiBuildError(f"Refusing UI stage outside the system temp root: {stage_path}")
    if not resolved_stage.name.startswith(_STAGE_PREFIX):
        raise HermesUiBuildError(f"Refusing UI stage with an unexpected name: {stage_path}")
    if resolved_stage == repository_root or resolved_stage.is_relative_to(repository_root):
        raise HermesUiBuildError(f"Refusing repository cleanup target: {stage_path}")

    identity = stage_path.lstat()
    if not stat.S_ISDIR(identity.st_mode) or (identity.st_dev, identity.st_ino) != (stage.device, stage.inode):
        raise HermesUiBuildError(f"UI stage identity changed before cleanup: {stage_path}")
    sentinel_path = stage_path / _STAGE_SENTINEL
    if sentinel_path.is_symlink() or sentinel_path.read_text(encoding="ascii") != stage.sentinel:
        raise HermesUiBuildError(f"UI stage ownership marker changed before cleanup: {stage_path}")


def _cleanup_stage_directory(stage: _StageDirectory, repository_root: Path) -> None:
    """Remove only the validated stage authorized by the repository owner."""
    if not stage.path.exists():
        return
    _validate_cleanup_target(stage, repository_root)
    print(
        "[hermes-ui-build] removing validated temporary stage "
        f"{stage.path} (device={stage.device}, inode={stage.inode})",
        file=sys.stderr,
    )
    shutil.rmtree(stage.path)


def _copy_build_inputs(repository_root: Path, stage: _StageDirectory) -> Path:
    """Copy source inputs into the owned stage without following symlinks."""
    _iter_build_inputs(repository_root)
    staged_ui = stage.path / "ui"

    def _ignore_ui_directory(_directory: str, names: list[str]) -> set[str]:
        return set(names).intersection(_IGNORED_UI_DIRECTORIES)

    shutil.copytree(
        repository_root / "ui",
        staged_ui,
        ignore=_ignore_ui_directory,
        symlinks=False,
    )
    staged_server_root = stage.path / "src" / "mcp_agent_mail"
    shutil.copytree(
        repository_root / "src" / "mcp_agent_mail" / "templates",
        staged_server_root / "templates",
        symlinks=False,
    )
    viewer_root = staged_server_root / "viewer_assets"
    viewer_root.mkdir(parents=True)
    shutil.copy2(
        repository_root / "src" / "mcp_agent_mail" / "viewer_assets" / "index.html",
        viewer_root / "index.html",
    )
    return staged_ui


def _run_checked(command: list[str], *, cwd: Path, environment: dict[str, str] | None = None) -> str:
    """Run one build command and retain bounded diagnostics on failure."""
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        diagnostics = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        raise HermesUiBuildError(
            f"UI build command failed ({completed.returncode}): {' '.join(command)}\n{diagnostics[-8000:]}"
        )
    return completed.stdout.strip()


def _dist_file_hashes(dist_root: Path) -> dict[str, str]:
    """Return a stable path-to-digest map for every generated bundle file."""
    hashes: dict[str, str] = {}
    for candidate in sorted(dist_root.rglob("*")):
        if not candidate.is_file() or candidate == dist_root / _BUILD_MANIFEST:
            continue
        relative = candidate.relative_to(dist_root).as_posix()
        hashes[relative] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return hashes


def _complete_dist_file_hashes(dist_root: Path) -> dict[str, str]:
    """Hash every browser artifact, including the manifest itself."""
    return {
        candidate.relative_to(dist_root).as_posix(): hashlib.sha256(candidate.read_bytes()).hexdigest()
        for candidate in sorted(dist_root.rglob("*"))
        if candidate.is_file()
    }


def _validate_artifact_ui(
    artifact_path: Path,
    *,
    target_name: str,
    dist_root: Path,
) -> None:
    """Require the completed archive to contain the exact validated UI bytes."""
    expected = _complete_dist_file_hashes(dist_root)
    actual: dict[str, str] = {}

    def _mentions_ui_dist(raw_name: str, marker: tuple[str, ...]) -> bool:
        parts = tuple(part for part in raw_name.replace("\\", "/").split("/") if part)
        return any(parts[index : index + len(marker)] == marker for index in range(len(parts) - len(marker) + 1))

    try:
        if target_name == "wheel":
            prefix = PurePosixPath(_UI_DIST_WHEEL)
            with ZipFile(artifact_path) as archive:
                for info in archive.infolist():
                    path = PurePosixPath(info.filename)
                    if path.is_absolute() or ".." in path.parts:
                        if _mentions_ui_dist(info.filename, prefix.parts):
                            raise HermesUiBuildError(f"Iris wheel contains an unsafe ui_dist member: {info.filename}")
                        continue
                    if path.parts[: len(prefix.parts)] != prefix.parts:
                        continue
                    relative_parts = path.parts[len(prefix.parts) :]
                    if not relative_parts:
                        if not info.is_dir():
                            raise HermesUiBuildError("Iris wheel contains an invalid ui_dist member.")
                        continue
                    if info.is_dir():
                        continue
                    if stat.S_ISLNK(info.external_attr >> 16):
                        raise HermesUiBuildError(f"Iris wheel contains a linked ui_dist member: {info.filename}")
                    relative = PurePosixPath(*relative_parts).as_posix()
                    if relative in actual:
                        raise HermesUiBuildError(f"Iris wheel contains a duplicate ui_dist member: {relative}")
                    actual[relative] = hashlib.sha256(archive.read(info)).hexdigest()
        elif target_name == "sdist":
            roots: set[str] = set()
            with tarfile.open(artifact_path, mode="r:*") as archive:
                for member in archive.getmembers():
                    path = PurePosixPath(member.name)
                    marker = ("src", "mcp_agent_mail", "ui_dist")
                    if path.is_absolute() or ".." in path.parts:
                        if _mentions_ui_dist(member.name, marker):
                            raise HermesUiBuildError(f"Iris sdist contains an unsafe ui_dist member: {member.name}")
                        continue
                    if len(path.parts) < 4 or path.parts[1:4] != marker:
                        continue
                    roots.add(path.parts[0])
                    relative_parts = path.parts[4:]
                    if not relative_parts:
                        if not member.isdir():
                            raise HermesUiBuildError("Iris sdist contains an invalid ui_dist member.")
                        continue
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise HermesUiBuildError(f"Iris sdist contains a linked ui_dist member: {member.name}")
                    relative = PurePosixPath(*relative_parts).as_posix()
                    if relative in actual:
                        raise HermesUiBuildError(f"Iris sdist contains a duplicate ui_dist member: {relative}")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise HermesUiBuildError(f"Iris sdist ui_dist member is unreadable: {member.name}")
                    actual[relative] = hashlib.sha256(stream.read()).hexdigest()
            if len(roots) != 1:
                raise HermesUiBuildError("Iris sdist must contain ui_dist beneath exactly one archive root.")
        else:
            raise HermesUiBuildError(f"Unsupported Iris UI artifact target: {target_name}")
    except (BadZipFile, OSError, tarfile.TarError) as exc:
        raise HermesUiBuildError(f"Iris {target_name} artifact is unavailable or invalid: {artifact_path}") from exc

    if actual != expected:
        raise HermesUiBuildError(f"Iris {target_name} UI members do not match the validated browser bundle.")


def _resolve_tool(name: str) -> str:
    """Resolve a build executable without invoking a shell."""
    candidates = [f"{name}.cmd", name] if os.name == "nt" else [name]
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable:
            return executable
    raise HermesUiBuildError(f"Required UI build executable is unavailable: {name}")


def _resolve_npm_cli(node: str) -> Path:
    """Resolve the npm CLI bundled beside the pinned Node executable."""
    node_directory = Path(node).resolve().parent
    candidates = (
        node_directory / "node_modules" / "npm" / "bin" / "npm-cli.js",
        node_directory.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise HermesUiBuildError(f"The npm CLI bundled with Node is unavailable beside: {node}")


def _verify_toolchain(
    repository_root: Path,
    *,
    node: str,
    npm_cli: Path,
    environment: dict[str, str],
) -> tuple[str, str]:
    """Require the release-pinned Node and npm pair."""
    node_version = _run_checked([node, "--version"], cwd=repository_root, environment=environment)
    npm_version = _run_checked(
        [node, str(npm_cli), "--version"],
        cwd=repository_root,
        environment=environment,
    )
    if node_version != _NODE_VERSION or npm_version != _NPM_VERSION:
        raise HermesUiBuildError(
            "Iris UI distributions require "
            f"Node {_NODE_VERSION} with npm {_NPM_VERSION}; found Node {node_version or '<empty>'} "
            f"and npm {npm_version or '<empty>'}."
        )
    return node_version, npm_version


def _write_build_manifest(
    dist_root: Path,
    *,
    repository_root: Path,
    node_version: str,
    npm_version: str,
    source_digest: str | None = None,
) -> None:
    """Bind generated assets to the exact source and toolchain that produced them."""
    manifest = {
        "schema": 1,
        "node_version": node_version,
        "npm_version": npm_version,
        "source_sha256": source_digest or _source_digest(repository_root),
        _DIST_HASHES_KEY: _dist_file_hashes(dist_root),
    }
    manifest_path = dist_root / _BUILD_MANIFEST
    try:
        with manifest_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    except FileExistsError as exc:
        raise HermesUiBuildError(f"Refusing a pre-existing UI build manifest: {manifest_path}") from exc


def _reachable_vite_assets(
    dist_root: Path,
) -> tuple[Path, Path, set[str], set[Path]]:
    """Return the exact Iris entry, locale chunks, and reachable stylesheets."""
    manifest_path = dist_root / _VITE_MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise HermesUiBuildError("Iris UI output has no regular Vite manifest.")
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HermesUiBuildError("Iris UI Vite manifest is invalid.") from exc
    if not isinstance(raw_manifest, dict) or not raw_manifest:
        raise HermesUiBuildError("Iris UI Vite manifest must be a non-empty object.")
    manifest: dict[str, dict[str, Any]] = {}
    for key, value in raw_manifest.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise HermesUiBuildError("Iris UI Vite manifest has an invalid entry.")
        manifest[key] = value

    entry_keys = [key for key, value in manifest.items() if value.get("isEntry") is True]
    if entry_keys != ["index.html"]:
        raise HermesUiBuildError("Iris UI Vite manifest must contain only the index.html entry point.")
    expected_locale_keys = frozenset(_EXPECTED_LOCALE_MANIFEST_KEYS)
    if set(manifest) != {"index.html", *expected_locale_keys}:
        raise HermesUiBuildError("Iris UI Vite manifest must contain the entry and exactly 44 locale chunks.")

    entry = manifest["index.html"]
    dynamic_imports = entry.get("dynamicImports")
    if (
        entry.get("src") != "index.html"
        or not isinstance(dynamic_imports, list)
        or len(dynamic_imports) != len(expected_locale_keys)
        or any(not isinstance(item, str) for item in dynamic_imports)
        or set(dynamic_imports) != expected_locale_keys
        or entry.get("imports", []) != []
        or entry.get("assets", []) != []
    ):
        raise HermesUiBuildError("Iris UI entry must dynamically import each of the 44 locale chunks exactly once.")
    for locale_key in _EXPECTED_LOCALE_MANIFEST_KEYS:
        locale_record = manifest[locale_key]
        if (
            locale_record.get("src") != locale_key
            or locale_record.get("isDynamicEntry") is not True
            or locale_record.get("imports", []) != []
            or locale_record.get("dynamicImports", []) != []
            or locale_record.get("assets", []) != []
        ):
            raise HermesUiBuildError("Iris UI locale catalogs must remain isolated dynamic chunks.")

    reachable_keys: set[str] = set()
    reachable_files: set[str] = set()
    reachable_stylesheets: set[Path] = set()
    chunk_files: dict[str, str] = {}
    pending = ["index.html"]
    while pending:
        key = pending.pop()
        if key in reachable_keys:
            continue
        record = manifest.get(key)
        if record is None:
            raise HermesUiBuildError("Iris UI Vite manifest references a missing chunk.")
        reachable_keys.add(key)
        file_name = record.get("file")
        expected_stem = "index" if key == "index.html" else key.removeprefix("src/locales/").removesuffix(".ts")
        if (
            not isinstance(file_name, str)
            or _FINGERPRINTED_ASSET_RE.fullmatch(file_name) is None
            or not file_name.startswith(f"assets/{expected_stem}-")
            or not file_name.endswith(".js")
        ):
            raise HermesUiBuildError("Iris UI Vite manifest references an unsafe output file.")
        previous_key = chunk_files.setdefault(file_name, key)
        if previous_key != key:
            raise HermesUiBuildError("Iris UI Vite manifest maps multiple chunks to one output file.")
        reachable_files.add(file_name)
        stylesheets = record.get("css", [])
        if not isinstance(stylesheets, list) or any(not isinstance(item, str) for item in stylesheets):
            raise HermesUiBuildError("Iris UI Vite manifest has an invalid stylesheet collection.")
        for stylesheet in stylesheets:
            if _FINGERPRINTED_ASSET_RE.fullmatch(stylesheet) is None or not stylesheet.endswith(".css"):
                raise HermesUiBuildError("Iris UI Vite manifest references an unsafe stylesheet.")
            reachable_files.add(stylesheet)
            reachable_stylesheets.add(dist_root / stylesheet)
        for relation_name in ("imports", "dynamicImports"):
            relations = record.get(relation_name, [])
            if not isinstance(relations, list) or any(not isinstance(item, str) for item in relations):
                raise HermesUiBuildError("Iris UI Vite manifest has an invalid chunk graph.")
            pending.extend(relations)

    if reachable_keys != set(manifest):
        raise HermesUiBuildError("Iris UI Vite manifest contains an unreachable output entry.")
    for relative in reachable_files:
        candidate = dist_root / relative
        if not candidate.is_file() or candidate.is_symlink() or candidate.stat().st_size == 0:
            raise HermesUiBuildError("Iris UI Vite manifest references a missing output file.")

    entry_file = entry.get("file")
    entry_css = entry.get("css", [])
    if (
        not isinstance(entry_file, str)
        or not entry_file.endswith(".js")
        or not isinstance(entry_css, list)
        or len(entry_css) != 1
        or not isinstance(entry_css[0], str)
        or not entry_css[0].endswith(".css")
    ):
        raise HermesUiBuildError("Iris UI entry must have one script and one stylesheet.")
    return (
        dist_root / entry_file,
        dist_root / entry_css[0],
        reachable_files,
        reachable_stylesheets,
    )


def _decode_css_escapes(css_text: str) -> str:
    """Decode CSS escapes before looking for browser-active URL syntax."""

    def replace_escape(match: re.Match[str]) -> str:
        hexadecimal = match.group("hex")
        if hexadecimal is not None:
            codepoint = int(hexadecimal, 16)
            if codepoint == 0 or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                return "\N{REPLACEMENT CHARACTER}"
            return chr(codepoint)
        if match.group("newline") is not None:
            return ""
        escaped_character = match.group("char")
        return escaped_character if escaped_character is not None else ""

    return _CSS_ESCAPE_RE.sub(replace_escape, css_text)


def _validate_stylesheet(stylesheet: Path) -> None:
    """Reject imports and non-inline URLs, including CSS-escaped spellings."""
    try:
        css_text = stylesheet.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HermesUiBuildError(f"Iris UI stylesheet is unreadable: {stylesheet.name}") from exc
    normalized_css = _CSS_COMMENT_RE.sub("", _decode_css_escapes(css_text))
    if "@import" in normalized_css.casefold():
        raise HermesUiBuildError(f"Iris UI stylesheet contains an active import: {stylesheet.name}")

    cursor = 0
    while match := _CSS_URL_FUNCTION_RE.search(normalized_css, cursor):
        end = normalized_css.find(")", match.end())
        if end == -1:
            raise HermesUiBuildError(f"Iris UI stylesheet contains an incomplete URL: {stylesheet.name}")
        raw_reference = normalized_css[match.end() : end].strip()
        if raw_reference[:1] in {'"', "'"}:
            quote = raw_reference[0]
            if len(raw_reference) < 2 or raw_reference[-1] != quote:
                raise HermesUiBuildError(f"Iris UI stylesheet contains an incomplete URL: {stylesheet.name}")
            reference = raw_reference[1:-1].strip()
        else:
            reference = raw_reference
        if not reference.casefold().startswith("data:image/"):
            raise HermesUiBuildError(f"Iris UI stylesheet contains a non-inline runtime URL: {stylesheet.name}")
        cursor = end + 1


def _validate_dist(dist_root: Path, *, repository_root: Path) -> None:
    """Validate the generated tree before Hatch reads any of its files."""
    if not dist_root.is_dir() or dist_root.is_symlink():
        raise HermesUiBuildError(f"Iris UI output directory is unavailable: {dist_root}")
    required = (
        dist_root / "index.html",
        dist_root / "assets" / "legacy.css",
        dist_root / _BUILD_MANIFEST,
    )
    if any(not path.is_file() or path.is_symlink() or path.stat().st_size == 0 for path in required):
        raise HermesUiBuildError("Iris UI output is missing index.html, login styles, or its build manifest.")

    (
        entry_javascript,
        entry_stylesheet,
        reachable_assets,
        reachable_stylesheets,
    ) = _reachable_vite_assets(dist_root)
    javascript = [entry_javascript]
    stylesheets = [entry_stylesheet]
    allowed_files = {
        "index.html",
        _BUILD_MANIFEST,
        _VITE_MANIFEST,
        "assets/legacy.css",
        *reachable_assets,
    }
    for candidate in dist_root.rglob("*"):
        if candidate.is_symlink() or (candidate.exists() and not (candidate.is_dir() or candidate.is_file())):
            raise HermesUiBuildError(f"Iris UI output contains an unsupported filesystem entry: {candidate}")
        if candidate.is_file() and candidate.stat().st_size == 0:
            raise HermesUiBuildError(f"Iris UI output contains an empty file: {candidate}")
        if candidate.is_file() and candidate.relative_to(dist_root).as_posix() not in allowed_files:
            raise HermesUiBuildError(f"Iris UI output contains an unexpected file: {candidate}")

    index_text = required[0].read_text(encoding="utf-8")
    collector = _EntryPointReferenceCollector()
    collector.feed(index_text)
    if collector.duplicate_attributes:
        duplicate_attributes = ", ".join(f"{tag}[{name}]" for tag, name in sorted(collector.duplicate_attributes))
        raise HermesUiBuildError(f"Iris UI entry point contains duplicate HTML attributes: {duplicate_attributes}")
    if collector.inline_active_content:
        active_tags = ", ".join(sorted(collector.inline_active_content))
        raise HermesUiBuildError(f"Iris UI entry point contains unsafe inline active content: {active_tags}")
    referenced_module_scripts: list[Path] = []
    referenced_stylesheets: list[Path] = []
    favicon_links = 0
    browser_url_attributes = frozenset(
        {"action", "background", "data", "formaction", "href", "ping", "poster", "src", "srcset", "xlink:href"}
    )
    for tag, attributes in collector.references:
        if (
            "style" in attributes
            or "srcdoc" in attributes
            or any(name.startswith("on") for name in attributes)
            or (attributes.get("http-equiv") or "").casefold() == "refresh"
        ):
            raise HermesUiBuildError(f"Iris UI entry point contains an unsafe active element: {tag}")
        if tag == "script":
            if (
                not {"type", "src"}.issubset(attributes)
                or not set(attributes).issubset({"type", "src", "crossorigin"})
                or attributes["type"] != "module"
                or attributes.get("crossorigin") not in {None, ""}
            ):
                raise HermesUiBuildError("Iris UI entry point contains an unexpected script element.")
        elif tag == "link":
            favicon_attributes = {
                "rel": "icon",
                "href": "/favicon.ico?v=iris",
                "type": "image/svg+xml",
                "sizes": "any",
            }
            if attributes == favicon_attributes:
                favicon_links += 1
                continue
            if (
                not {"rel", "href"}.issubset(attributes)
                or not set(attributes).issubset({"rel", "href", "crossorigin"})
                or attributes["rel"] != "stylesheet"
                or attributes.get("crossorigin") not in {None, ""}
            ):
                raise HermesUiBuildError("Iris UI entry point contains an unexpected link element.")
        active_attributes = browser_url_attributes.intersection(attributes)
        if not active_attributes:
            continue
        expected_attribute = "href" if tag == "link" else "src" if tag == "script" else None
        if expected_attribute is None or active_attributes != {expected_attribute}:
            raise HermesUiBuildError(f"Iris UI entry point contains an unsafe active element: {tag}")
        reference = attributes[expected_attribute]
        if reference is None:
            raise HermesUiBuildError(f"Iris UI entry point contains an empty runtime reference: {tag}")
        prefix = "/mail/assets/"
        if not reference.startswith(prefix) or any(character in reference for character in ("\\", ":", "?", "#", "%")):
            raise HermesUiBuildError(f"Iris UI entry point contains an unsafe runtime reference: {reference}")
        relative = Path(reference.removeprefix(prefix))
        if relative.is_absolute() or ".." in relative.parts:
            raise HermesUiBuildError(f"Iris UI entry point contains an unsafe runtime reference: {reference}")
        target = dist_root / "assets" / relative
        if not target.is_file() or target.is_symlink():
            raise HermesUiBuildError(f"Iris UI entry point references a missing build asset: {reference}")
        if tag == "script" and attributes.get("type") == "module":
            referenced_module_scripts.append(target)
        if tag == "link" and "stylesheet" in (attributes.get("rel") or "").split():
            referenced_stylesheets.append(target)
    if referenced_module_scripts != javascript or referenced_stylesheets != stylesheets or favicon_links != 1:
        raise HermesUiBuildError(
            "Iris UI entry point must contain exactly one built module script, "
            "one built stylesheet, and the exact favicon link."
        )

    for stylesheet in (
        *sorted(reachable_stylesheets),
        dist_root / "assets" / "legacy.css",
    ):
        _validate_stylesheet(stylesheet)

    try:
        manifest = json.loads((dist_root / _BUILD_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HermesUiBuildError("Iris UI build manifest is invalid.") from exc
    expected_manifest = {
        "schema": 1,
        "node_version": _NODE_VERSION,
        "npm_version": _NPM_VERSION,
        "source_sha256": _source_digest(repository_root),
        _DIST_HASHES_KEY: _dist_file_hashes(dist_root),
    }
    if manifest != expected_manifest:
        raise HermesUiBuildError("Iris UI assets do not match the current source tree and pinned toolchain.")


def _build_ui_in_stage(repository_root: Path) -> tuple[_StageDirectory, Path]:
    """Compile both UI bundles and leave the owned stage for Hatch to consume."""
    repository_root = repository_root.resolve(strict=True)
    stage = _create_stage_directory(repository_root)
    try:
        node = _resolve_tool("node")
        npm_cli = _resolve_npm_cli(node)
        cache_root = stage.path / ".npm-cache"
        process_temp = stage.path / ".tmp"
        xdg_cache = stage.path / ".cache"
        for directory in (cache_root, process_temp, xdg_cache):
            directory.mkdir()
        npm_config = stage.path / ".npmrc"
        npm_global_config = stage.path / ".npm-globalrc"
        npm_config.write_text("ignore-scripts=true\naudit=false\nfund=false\nupdate-notifier=false\n", encoding="ascii")
        npm_global_config.write_text("", encoding="ascii")
        environment = {
            "CI": "1",
            "HOME": str(stage.path),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": f"{Path(node).resolve().parent}{os.pathsep}{os.defpath}",
            "SOURCE_DATE_EPOCH": os.environ.get("SOURCE_DATE_EPOCH", "0"),
            "TEMP": str(process_temp),
            "TMP": str(process_temp),
            "TMPDIR": str(process_temp),
            "TZ": "UTC",
            "USERPROFILE": str(stage.path),
            "XDG_CACHE_HOME": str(xdg_cache),
            "npm_config_cache": str(cache_root),
            "npm_config_globalconfig": str(npm_global_config),
            "npm_config_userconfig": str(npm_config),
        }
        environment.update(_allowlisted_windows_environment(os.environ, platform_name=os.name))
        node_version, npm_version = _verify_toolchain(
            repository_root,
            node=node,
            npm_cli=npm_cli,
            environment=environment,
        )
        source_digest_before_copy = _source_digest(repository_root)
        staged_ui = _copy_build_inputs(repository_root, stage)
        staged_source_digest = _source_digest(stage.path)
        if staged_source_digest != source_digest_before_copy:
            raise HermesUiBuildError("UI sources changed while the isolated build snapshot was being copied.")
        _run_checked(
            [node, str(npm_cli), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=staged_ui,
            environment=environment,
        )
        _run_checked(
            [node, str(npm_cli), "run", "build"],
            cwd=staged_ui,
            environment=environment,
        )
        if _source_digest(repository_root) != staged_source_digest:
            raise HermesUiBuildError("UI sources changed while the isolated browser bundle was being built.")
        dist_root = staged_ui / "dist"
        _write_build_manifest(
            dist_root,
            repository_root=repository_root,
            node_version=node_version,
            npm_version=npm_version,
            source_digest=staged_source_digest,
        )
        _validate_dist(dist_root, repository_root=repository_root)
    except BaseException:
        _cleanup_stage_directory(stage, repository_root)
        raise
    return stage, dist_root


class CustomBuildHook(BuildHookInterface):
    """Attach one validated Iris UI tree to sdist and standard wheel builds."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._stages: list[tuple[_StageDirectory, str]] = []
        self._artifact_dist_roots: list[Path] = []
        atexit.register(self._cleanup_stages)

    def _cleanup_stages(self) -> None:
        repository_root = Path(self.root).resolve(strict=True)
        while self._stages:
            stage, _source_digest_at_build = self._stages.pop()
            _cleanup_stage_directory(stage, repository_root)

    def clean(self, versions: list[str]) -> None:
        del versions
        self._artifact_dist_roots.clear()
        self._cleanup_stages()

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        repository_root = Path(self.root).resolve(strict=True)
        if self.target_name == "wheel" and version == "editable":
            return
        if self.target_name not in {"sdist", "wheel"} or version != "standard":
            raise HermesUiBuildError(f"Unsupported Iris UI build target: {self.target_name}/{version}")

        packaged_dist = repository_root / _UI_DIST_SOURCE
        building_wheel_from_sdist = self.target_name == "wheel" and (repository_root / "PKG-INFO").is_file()
        if building_wheel_from_sdist and packaged_dist.exists():
            _validate_dist(packaged_dist, repository_root=repository_root)
            self._artifact_dist_roots.append(packaged_dist)
            # The generated tree is already inside the package copied from the
            # sdist.  Adding the same files through force_include would create
            # duplicate wheel members.
            return

        stage, dist_root = _build_ui_in_stage(repository_root)
        manifest = json.loads((dist_root / _BUILD_MANIFEST).read_text(encoding="utf-8"))
        source_digest_at_build = manifest.get("source_sha256")
        if not isinstance(source_digest_at_build, str) or len(source_digest_at_build) != 64:
            _cleanup_stage_directory(stage, repository_root)
            raise HermesUiBuildError("Iris UI build manifest has no valid source digest.")
        self._stages.append((stage, source_digest_at_build))
        self._artifact_dist_roots.append(dist_root)

        destination = _UI_DIST_SDIST if self.target_name == "sdist" else _UI_DIST_WHEEL
        build_data.setdefault("force_include", {})[str(dist_root)] = destination

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        del version, build_data
        repository_root = Path(self.root).resolve(strict=True)
        try:
            if any(_source_digest(repository_root) != expected for _stage, expected in self._stages):
                raise HermesUiBuildError("UI sources changed while Hatch was assembling the distribution artifact.")
            for dist_root in self._artifact_dist_roots:
                _validate_dist(dist_root, repository_root=repository_root)
                _validate_artifact_ui(
                    Path(artifact_path),
                    target_name=self.target_name,
                    dist_root=dist_root,
                )
        finally:
            self._artifact_dist_roots.clear()
            self._cleanup_stages()
