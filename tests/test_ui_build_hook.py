from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any, cast
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.plugin.utils import load_plugin_from_script

import hatch_build

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _build_hook(root: Path, output: Path, target_name: str) -> hatch_build.CustomBuildHook:
    return hatch_build.CustomBuildHook(
        str(root),
        {},
        cast(Any, None),
        cast(Any, None),
        str(output),
        target_name,
    )


def _write_vite_output(
    dist_root: Path,
    *,
    include_country_flag_font: bool = True,
) -> None:
    assets = dist_root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (dist_root / "index.html").write_text(
        '<!doctype html><link rel="icon" href="/favicon.ico?v=iris" type="image/svg+xml" sizes="any">'
        '<script type="module" src="/mail/assets/index-testhash.js"></script>'
        '<link rel="stylesheet" href="/mail/assets/index-testhash.css">',
        encoding="utf-8",
    )
    (assets / "index-testhash.js").write_text(
        "/* English-only Iris entry */\n",
        encoding="utf-8",
    )
    (assets / "index-testhash.css").write_text(
        "/* index-testhash.css */\n",
        encoding="utf-8",
    )
    (assets / "legacy.css").write_text(
        '@font-face { src: url("/mail/assets/TwemojiCountryFlags.woff2?v=9f04f144") '
        'format("woff2"); }\n',
        encoding="utf-8",
    )
    if include_country_flag_font:
        (dist_root / hatch_build._COUNTRY_FLAG_FONT).write_bytes(
            (REPOSITORY_ROOT / "ui" / "public" / hatch_build._COUNTRY_FLAG_FONT).read_bytes()
        )

    locale_records: dict[str, dict[str, object]] = {}
    for locale_key in hatch_build._EXPECTED_LOCALE_MANIFEST_KEYS:
        locale = locale_key.removeprefix("src/locales/").removesuffix(".ts")
        locale_file = f"assets/{locale}-localehash.js"
        catalog_marker = "Język" if locale == "pl" else locale
        (dist_root / locale_file).write_text(
            f"export default {{ catalog: {catalog_marker!r} }};\n",
            encoding="utf-8",
        )
        locale_records[locale_key] = {
            "file": locale_file,
            "name": locale,
            "src": locale_key,
            "isDynamicEntry": True,
        }

    vite_manifest = dist_root / ".vite" / "manifest.json"
    vite_manifest.parent.mkdir()
    vite_manifest.write_text(
        json.dumps(
            {
                "index.html": {
                    "file": "assets/index-testhash.js",
                    "name": "index",
                    "src": "index.html",
                    "isEntry": True,
                    "dynamicImports": list(hatch_build._EXPECTED_LOCALE_MANIFEST_KEYS),
                    "css": ["assets/index-testhash.css"],
                },
                **locale_records,
            }
        ),
        encoding="utf-8",
    )


def _write_valid_dist(
    dist_root: Path,
    *,
    repository_root: Path,
    include_country_flag_font: bool = True,
) -> None:
    _write_vite_output(
        dist_root,
        include_country_flag_font=include_country_flag_font,
    )
    (dist_root / hatch_build._BUILD_MANIFEST).write_text(
        json.dumps(
            {
                "schema": 1,
                "node_version": hatch_build._NODE_VERSION,
                "npm_version": hatch_build._NPM_VERSION,
                "source_sha256": hatch_build._source_digest(repository_root),
                "files": hatch_build._dist_file_hashes(dist_root),
            }
        ),
        encoding="utf-8",
    )


def _refresh_build_manifest(dist_root: Path) -> None:
    manifest_path = dist_root / hatch_build._BUILD_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = hatch_build._dist_file_hashes(dist_root)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _write_ui_archive(
    artifact_path: Path,
    *,
    dist_root: Path,
    target_name: str,
    mutate_relative: str | None = None,
) -> None:
    files = [candidate for candidate in sorted(dist_root.rglob("*")) if candidate.is_file()]
    if target_name == "wheel":
        with ZipFile(artifact_path, mode="x", compression=ZIP_DEFLATED) as archive:
            for candidate in files:
                relative = candidate.relative_to(dist_root).as_posix()
                contents = candidate.read_bytes()
                if relative == mutate_relative:
                    contents += b"tampered"
                archive.writestr(f"mcp_agent_mail/ui_dist/{relative}", contents)
        return

    with tarfile.open(artifact_path, mode="x:gz") as archive:
        for candidate in files:
            relative = candidate.relative_to(dist_root).as_posix()
            contents = candidate.read_bytes()
            if relative == mutate_relative:
                contents += b"tampered"
            info = tarfile.TarInfo(f"mcp_agent_mail-test/src/mcp_agent_mail/ui_dist/{relative}")
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))


def test_cleanup_removes_only_the_owned_validated_temp_stage() -> None:
    stage = hatch_build._create_stage_directory(REPOSITORY_ROOT)
    assert stage.path.parent == Path(hatch_build.tempfile.gettempdir()).resolve(strict=True)
    assert stage.path.name.startswith(hatch_build._STAGE_PREFIX)
    assert not stage.path.is_relative_to(REPOSITORY_ROOT)

    hatch_build._cleanup_stage_directory(stage, REPOSITORY_ROOT)

    assert not stage.path.exists()


def test_cleanup_fails_closed_when_the_ownership_marker_changes() -> None:
    stage = hatch_build._create_stage_directory(REPOSITORY_ROOT)
    sentinel = stage.path / hatch_build._STAGE_SENTINEL
    sentinel.write_text("tampered", encoding="ascii")

    with pytest.raises(hatch_build.HermesUiBuildError, match="ownership marker changed"):
        hatch_build._cleanup_stage_directory(stage, REPOSITORY_ROOT)
    assert stage.path.is_dir()

    sentinel.write_text(stage.sentinel, encoding="ascii")
    hatch_build._cleanup_stage_directory(stage, REPOSITORY_ROOT)


def test_cleanup_fails_closed_for_an_unexpected_stage_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    target = temp_root / "unexpected-stage-name"
    target.mkdir()
    sentinel = "owned-but-wrong-name"
    (target / hatch_build._STAGE_SENTINEL).write_text(sentinel, encoding="ascii")
    identity = target.lstat()
    stage = hatch_build._StageDirectory(target, identity.st_dev, identity.st_ino, sentinel)
    monkeypatch.setattr(hatch_build.tempfile, "gettempdir", lambda: str(temp_root))

    with pytest.raises(hatch_build.HermesUiBuildError, match="unexpected name"):
        hatch_build._cleanup_stage_directory(stage, REPOSITORY_ROOT)

    assert target.is_dir()
    assert (target / hatch_build._STAGE_SENTINEL).read_text(encoding="ascii") == sentinel


def test_cleanup_fails_closed_for_a_stage_outside_the_system_temp_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    target = tmp_path / "outside" / f"{hatch_build._STAGE_PREFIX}outside"
    target.mkdir(parents=True)
    sentinel = "owned-but-outside"
    (target / hatch_build._STAGE_SENTINEL).write_text(sentinel, encoding="ascii")
    identity = target.lstat()
    stage = hatch_build._StageDirectory(target, identity.st_dev, identity.st_ino, sentinel)
    monkeypatch.setattr(hatch_build.tempfile, "gettempdir", lambda: str(temp_root))

    with pytest.raises(hatch_build.HermesUiBuildError, match="outside the system temp root"):
        hatch_build._cleanup_stage_directory(stage, REPOSITORY_ROOT)

    assert target.is_dir()
    assert (target / hatch_build._STAGE_SENTINEL).read_text(encoding="ascii") == sentinel


def test_cleanup_fails_closed_for_a_symlinked_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = "owned-but-symlinked"
    (outside / hatch_build._STAGE_SENTINEL).write_text(sentinel, encoding="ascii")
    target = temp_root / f"{hatch_build._STAGE_PREFIX}symlink"
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    identity = target.lstat()
    stage = hatch_build._StageDirectory(target, identity.st_dev, identity.st_ino, sentinel)
    monkeypatch.setattr(hatch_build.tempfile, "gettempdir", lambda: str(temp_root))

    with pytest.raises(hatch_build.HermesUiBuildError, match="missing or symlinked"):
        hatch_build._cleanup_stage_directory(stage, REPOSITORY_ROOT)

    assert target.is_symlink()
    assert (outside / hatch_build._STAGE_SENTINEL).read_text(encoding="ascii") == sentinel


@pytest.mark.parametrize(("device_delta", "inode_delta"), [(1, 0), (0, 1)])
def test_cleanup_fails_closed_when_the_stage_identity_changes(
    device_delta: int,
    inode_delta: int,
) -> None:
    stage = hatch_build._create_stage_directory(REPOSITORY_ROOT)
    stale_identity = hatch_build._StageDirectory(
        stage.path,
        stage.device + device_delta,
        stage.inode + inode_delta,
        stage.sentinel,
    )

    with pytest.raises(hatch_build.HermesUiBuildError, match="identity changed"):
        hatch_build._cleanup_stage_directory(stale_identity, REPOSITORY_ROOT)
    assert stage.path.is_dir()

    hatch_build._cleanup_stage_directory(stage, REPOSITORY_ROOT)


def test_editable_wheel_does_not_build_or_package_the_ui(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail_if_called(_repository_root: Path) -> tuple[hatch_build._StageDirectory, Path]:
        pytest.fail("editable installs must not invoke Node or create a UI stage")

    monkeypatch.setattr(hatch_build, "_build_ui_in_stage", fail_if_called)
    hook = _build_hook(REPOSITORY_ROOT, tmp_path, "wheel")
    build_data: dict[str, Any] = {}

    hook.initialize("editable", build_data)

    assert build_data == {}


def test_sdist_force_includes_generated_ui_and_finalize_cleans_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stage = hatch_build._create_stage_directory(REPOSITORY_ROOT)
    dist_root = stage.path / "ui" / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    monkeypatch.setattr(hatch_build, "_build_ui_in_stage", lambda _root: (stage, dist_root))
    hook = _build_hook(REPOSITORY_ROOT, tmp_path, "sdist")
    build_data: dict[str, Any] = {}

    hook.initialize("standard", build_data)

    assert build_data["force_include"] == {
        str(dist_root): "src/mcp_agent_mail/ui_dist",
    }
    assert stage.path.is_dir()

    artifact_path = tmp_path / "artifact.tar.gz"
    _write_ui_archive(artifact_path, dist_root=dist_root, target_name="sdist")
    hook.finalize("standard", build_data, str(artifact_path))

    assert not stage.path.exists()


def test_finalize_rejects_a_source_change_during_artifact_assembly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stage = hatch_build._create_stage_directory(REPOSITORY_ROOT)
    dist_root = stage.path / "ui" / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    monkeypatch.setattr(hatch_build, "_build_ui_in_stage", lambda _root: (stage, dist_root))
    hook = _build_hook(REPOSITORY_ROOT, tmp_path, "sdist")
    build_data: dict[str, Any] = {}
    hook.initialize("standard", build_data)
    monkeypatch.setattr(hatch_build, "_source_digest", lambda _root: "f" * 64)

    with pytest.raises(hatch_build.HermesUiBuildError, match="assembling the distribution"):
        hook.finalize("standard", build_data, str(tmp_path / "artifact.tar.gz"))

    assert not stage.path.exists()


def test_finalize_revalidates_staged_ui_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stage = hatch_build._create_stage_directory(REPOSITORY_ROOT)
    dist_root = stage.path / "ui" / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    monkeypatch.setattr(hatch_build, "_build_ui_in_stage", lambda _root: (stage, dist_root))
    hook = _build_hook(REPOSITORY_ROOT, tmp_path, "wheel")
    build_data: dict[str, Any] = {}
    hook.initialize("standard", build_data)
    artifact_path = tmp_path / "artifact.whl"
    _write_ui_archive(artifact_path, dist_root=dist_root, target_name="wheel")
    (dist_root / "assets" / "legacy.css").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(hatch_build.HermesUiBuildError, match="do not match"):
        hook.finalize("standard", build_data, str(artifact_path))

    assert not stage.path.exists()


@pytest.mark.parametrize(
    ("target_name", "artifact_name"),
    [("wheel", "artifact.whl"), ("sdist", "artifact.tar.gz")],
)
def test_completed_artifact_must_match_every_validated_ui_byte(
    tmp_path: Path,
    target_name: str,
    artifact_name: str,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    artifact_path = tmp_path / artifact_name
    _write_ui_archive(artifact_path, dist_root=dist_root, target_name=target_name)

    hatch_build._validate_artifact_ui(
        artifact_path,
        target_name=target_name,
        dist_root=dist_root,
    )

    mutated_path = tmp_path / f"mutated-{artifact_name}"
    _write_ui_archive(
        mutated_path,
        dist_root=dist_root,
        target_name=target_name,
        mutate_relative="assets/legacy.css",
    )
    with pytest.raises(hatch_build.HermesUiBuildError, match="do not match"):
        hatch_build._validate_artifact_ui(
            mutated_path,
            target_name=target_name,
            dist_root=dist_root,
        )


@pytest.mark.parametrize(
    ("target_name", "artifact_name", "malicious_member"),
    [
        ("wheel", "unsafe.whl", "mcp_agent_mail/ui_dist/../escape.js"),
        (
            "sdist",
            "unsafe.tar.gz",
            "/mcp_agent_mail-test/src/mcp_agent_mail/ui_dist/escape.js",
        ),
    ],
)
def test_completed_artifact_rejects_unsafe_ui_member_paths(
    tmp_path: Path,
    target_name: str,
    artifact_name: str,
    malicious_member: str,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    artifact_path = tmp_path / artifact_name
    if target_name == "wheel":
        with ZipFile(artifact_path, mode="x") as archive:
            archive.writestr(malicious_member, b"malicious")
    else:
        with tarfile.open(artifact_path, mode="x:gz") as archive:
            contents = b"malicious"
            info = tarfile.TarInfo(malicious_member)
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))

    with pytest.raises(hatch_build.HermesUiBuildError, match="unsafe ui_dist member"):
        hatch_build._validate_artifact_ui(
            artifact_path,
            target_name=target_name,
            dist_root=dist_root,
        )


def test_wheel_from_sdist_reuses_bundled_ui_without_node(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    packaged_dist = source_root / "src" / "mcp_agent_mail" / "ui_dist"
    packaged_dist.mkdir(parents=True)
    (source_root / "PKG-INFO").write_text("Metadata-Version: 2.4\n", encoding="utf-8")
    validated: list[tuple[Path, Path]] = []

    def record_validation(dist_root: Path, *, repository_root: Path) -> None:
        validated.append((dist_root, repository_root))

    def fail_if_called(_repository_root: Path) -> tuple[hatch_build._StageDirectory, Path]:
        pytest.fail("wheel-from-sdist must reuse the generated bundle without Node")

    monkeypatch.setattr(hatch_build, "_validate_dist", record_validation)
    monkeypatch.setattr(hatch_build, "_build_ui_in_stage", fail_if_called)
    hook = _build_hook(source_root, tmp_path, "wheel")
    build_data: dict[str, Any] = {}

    hook.initialize("standard", build_data)

    assert validated == [(packaged_dist, source_root.resolve(strict=True))]
    assert build_data == {}


def test_wheel_source_tree_without_git_still_builds_fresh_ui(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "docker-context"
    (source_root / "ui").mkdir(parents=True)
    (source_root / "ui" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    templates_root = source_root / "src" / "mcp_agent_mail" / "templates"
    templates_root.mkdir(parents=True)
    (templates_root / "base.html").write_text("<!doctype html>", encoding="utf-8")
    viewer_root = source_root / "src" / "mcp_agent_mail" / "viewer_assets"
    viewer_root.mkdir(parents=True)
    (viewer_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    packaged_dist = source_root / "src" / "mcp_agent_mail" / "ui_dist"
    packaged_dist.mkdir(parents=True)
    stage = hatch_build._create_stage_directory(REPOSITORY_ROOT)
    fresh_dist = stage.path / "ui" / "dist"
    _write_valid_dist(fresh_dist, repository_root=source_root)
    build_calls: list[Path] = []

    def build_fresh(repository_root: Path) -> tuple[hatch_build._StageDirectory, Path]:
        build_calls.append(repository_root)
        return stage, fresh_dist

    monkeypatch.setattr(hatch_build, "_build_ui_in_stage", build_fresh)
    hook = _build_hook(source_root, tmp_path, "wheel")
    build_data: dict[str, Any] = {}

    hook.initialize("standard", build_data)

    assert build_calls == [source_root.resolve(strict=True)]
    assert build_data["force_include"] == {
        str(fresh_dist): "mcp_agent_mail/ui_dist",
    }
    artifact_path = tmp_path / "artifact.whl"
    _write_ui_archive(artifact_path, dist_root=fresh_dist, target_name="wheel")
    hook.finalize("standard", build_data, str(artifact_path))
    assert not stage.path.exists()


def test_generated_dist_rejects_external_runtime_references(tmp_path: Path) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)
    (dist_root / "index.html").write_text(
        '<script type="module" src="https://cdn.invalid/app.js"></script>',
        encoding="utf-8",
    )

    with pytest.raises(hatch_build.HermesUiBuildError, match="unsafe runtime reference"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


def test_generated_dist_rejects_missing_index_asset(tmp_path: Path) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    (dist_root / "index.html").write_text(
        '<script type="module" src="/mail/assets/missing.js"></script>'
        '<link rel="stylesheet" href="/mail/assets/index-testhash.css">',
        encoding="utf-8",
    )

    with pytest.raises(hatch_build.HermesUiBuildError, match="missing build asset"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


@pytest.mark.parametrize(
    "unexpected_name",
    [
        "orphan-localehash.js",
        "orphan-stylehash.css",
        "legacy.js",
        "UnexpectedCountryFlags.woff2",
    ],
)
def test_generated_dist_rejects_unreferenced_or_legacy_runtime_files(
    tmp_path: Path,
    unexpected_name: str,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    (dist_root / "assets" / unexpected_name).write_text(
        "/* unexpected runtime asset */\n",
        encoding="utf-8",
    )

    with pytest.raises(hatch_build.HermesUiBuildError, match="unexpected file"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


def test_generated_dist_requires_the_pinned_country_flag_font(tmp_path: Path) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(
        dist_root,
        repository_root=REPOSITORY_ROOT,
        include_country_flag_font=False,
    )

    with pytest.raises(hatch_build.HermesUiBuildError, match="country flag font"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


def test_pinned_country_flag_font_is_not_rewritten_by_git() -> None:
    font_path = "ui/public/assets/TwemojiCountryFlags.woff2"
    result = subprocess.run(
        ["git", "check-attr", "text", "--", font_path],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == f"{font_path}: text: unset"


def test_generated_dist_rejects_a_rehashed_but_tampered_country_flag_font(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    font = dist_root / hatch_build._COUNTRY_FLAG_FONT
    font.write_bytes(font.read_bytes() + b"tampered")
    _refresh_build_manifest(dist_root)

    with pytest.raises(hatch_build.HermesUiBuildError, match="pinned asset"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


def test_generated_dist_rejects_a_duplicate_nested_country_flag_font(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    nested_font = dist_root / "assets" / hatch_build._COUNTRY_FLAG_FONT
    nested_font.parent.mkdir(parents=True)
    nested_font.write_bytes((dist_root / hatch_build._COUNTRY_FLAG_FONT).read_bytes())
    _refresh_build_manifest(dist_root)

    with pytest.raises(hatch_build.HermesUiBuildError, match="unexpected file"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


def test_generated_dist_requires_exact_lazy_locale_chunk_contract(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    vite_manifest_path = dist_root / hatch_build._VITE_MANIFEST
    vite_manifest = json.loads(vite_manifest_path.read_text(encoding="utf-8"))
    entry = vite_manifest["index.html"]
    locale_keys = set(hatch_build._EXPECTED_LOCALE_MANIFEST_KEYS)
    chunk_files = {record["file"] for record in vite_manifest.values()}

    assert len(locale_keys) == 44
    assert len(vite_manifest) == 45
    assert len(entry["dynamicImports"]) == 44
    assert set(entry["dynamicImports"]) == locale_keys
    assert len(chunk_files) == 45
    assert vite_manifest["src/locales/pl.ts"]["isDynamicEntry"] is True
    assert vite_manifest["src/locales/pl.ts"]["file"] != entry["file"]
    assert "Język" not in (dist_root / entry["file"]).read_text(encoding="utf-8")
    assert "Język" in (dist_root / vite_manifest["src/locales/pl.ts"]["file"]).read_text(encoding="utf-8")

    hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)

    entry["dynamicImports"].remove("src/locales/pl.ts")
    vite_manifest_path.write_text(json.dumps(vite_manifest), encoding="utf-8")
    _refresh_build_manifest(dist_root)
    with pytest.raises(hatch_build.HermesUiBuildError, match="44 locale chunks"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


def test_generated_dist_rejects_duplicate_lazy_chunk_output_files(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    vite_manifest_path = dist_root / hatch_build._VITE_MANIFEST
    vite_manifest = json.loads(vite_manifest_path.read_text(encoding="utf-8"))
    vite_manifest["src/locales/fr.ts"]["file"] = vite_manifest["src/locales/pl.ts"]["file"]
    vite_manifest_path.write_text(json.dumps(vite_manifest), encoding="utf-8")
    _refresh_build_manifest(dist_root)

    with pytest.raises(
        hatch_build.HermesUiBuildError,
        match=r"unsafe output file|multiple chunks",
    ):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


def test_generated_dist_rejects_duplicate_html_attributes_before_resolution(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    index = dist_root / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            'src="/mail/assets/index-testhash.js"',
            'src="https://cdn.invalid/app.js" src="/mail/assets/index-testhash.js"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(hatch_build.HermesUiBuildError, match="duplicate HTML attributes"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


@pytest.mark.parametrize(
    "unexpected_markup",
    [
        '<link rel="preload" href="/mail/assets/index-testhash.js" as="script">',
        '<link rel="icon" href="/favicon.ico">',
        '<script type="module" src="/mail/assets/index-testhash.js"></script>',
        '<script src="/mail/assets/legacy.js"></script>',
    ],
)
def test_generated_dist_rejects_extraneous_script_and_link_elements(
    tmp_path: Path,
    unexpected_markup: str,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    index = dist_root / "index.html"
    index.write_text(index.read_text(encoding="utf-8") + unexpected_markup, encoding="utf-8")

    with pytest.raises(
        hatch_build.HermesUiBuildError,
        match=r"unexpected (?:script|link) element|exactly one built module script",
    ):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


@pytest.mark.parametrize(
    "active_markup",
    [
        '<meta http-equiv="refresh" content="0;url=https://cdn.invalid">',
        '<video poster="/mail/logout"></video>',
        '<source srcset="https://cdn.invalid/image.png">',
        '<div style="background-image:url(https://cdn.invalid/image.png)"></div>',
        "<iframe srcdoc=\"&lt;script&gt;fetch('https://cdn.invalid')&lt;/script&gt;\"></iframe>",
        "<main onload=\"fetch('https://cdn.invalid')\"></main>",
    ],
)
def test_generated_dist_rejects_alternative_active_html(
    tmp_path: Path,
    active_markup: str,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    index = dist_root / "index.html"
    index.write_text(index.read_text(encoding="utf-8") + active_markup, encoding="utf-8")

    with pytest.raises(hatch_build.HermesUiBuildError, match="unsafe active element"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


@pytest.mark.parametrize(
    "active_markup",
    [
        "<script>fetch('https://cdn.invalid')</script>",
        "<style>@import url('https://cdn.invalid/theme.css');</style>",
        "<svg><style>image { fill: url('https://cdn.invalid/paint'); }</style></svg>",
    ],
)
def test_generated_dist_rejects_inline_active_content(
    tmp_path: Path,
    active_markup: str,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    index = dist_root / "index.html"
    index.write_text(index.read_text(encoding="utf-8") + active_markup, encoding="utf-8")

    with pytest.raises(hatch_build.HermesUiBuildError, match="unsafe inline active content"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


def test_generated_dist_rejects_external_stylesheet_url(tmp_path: Path) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    (dist_root / "assets" / "index-testhash.css").write_text(
        ".tracker { background-image: url(https://cdn.invalid/pixel.png); }\n",
        encoding="utf-8",
    )
    manifest_path = dist_root / hatch_build._BUILD_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = hatch_build._dist_file_hashes(dist_root)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(hatch_build.HermesUiBuildError, match="non-inline runtime URL"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


@pytest.mark.parametrize(
    "font_url",
    [
        "/mail/assets/UnexpectedCountryFlags.woff2",
        "/mail/assets/TwemojiCountryFlags.woff2?v=1",
        "TwemojiCountryFlags.woff2",
    ],
)
def test_generated_dist_rejects_non_allowlisted_local_font_urls(
    tmp_path: Path,
    font_url: str,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    (dist_root / "assets" / "legacy.css").write_text(
        f'@font-face {{ src: url("{font_url}") format("woff2"); }}\n',
        encoding="utf-8",
    )
    _refresh_build_manifest(dist_root)

    with pytest.raises(hatch_build.HermesUiBuildError, match="non-inline runtime URL"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


@pytest.mark.parametrize(
    ("css_text", "expected_error"),
    [
        (
            "@import 'https://cdn.invalid/theme.css';\n",
            "active import",
        ),
        (
            r"@\69 mport 'https\3a //cdn.invalid/theme.css';",
            "active import",
        ),
        (
            r".tracker { background-image: u\72l(https\3a //cdn.invalid/pixel.png); }",
            "non-inline runtime URL",
        ),
        (
            r".tracker { background-image: u\/**\/\72l(https://cdn.invalid/pixel.png); }",
            "non-inline runtime URL",
        ),
    ],
)
def test_generated_dist_rejects_external_references_in_lazy_stylesheets(
    tmp_path: Path,
    css_text: str,
    expected_error: str,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    lazy_stylesheet = dist_root / "assets" / "pl-styleshash.css"
    lazy_stylesheet.write_text(css_text, encoding="utf-8")
    vite_manifest_path = dist_root / hatch_build._VITE_MANIFEST
    vite_manifest = json.loads(vite_manifest_path.read_text(encoding="utf-8"))
    vite_manifest["src/locales/pl.ts"]["css"] = ["assets/pl-styleshash.css"]
    vite_manifest_path.write_text(json.dumps(vite_manifest), encoding="utf-8")
    _refresh_build_manifest(dist_root)

    with pytest.raises(hatch_build.HermesUiBuildError, match=expected_error):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


def test_generated_dist_accepts_inline_image_in_lazy_stylesheet(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    lazy_stylesheet = dist_root / "assets" / "pl-styleshash.css"
    lazy_stylesheet.write_text(
        ".flag { background-image: url('data:image/png;base64,iVBORw0KGgo='); }\n",
        encoding="utf-8",
    )
    vite_manifest_path = dist_root / hatch_build._VITE_MANIFEST
    vite_manifest = json.loads(vite_manifest_path.read_text(encoding="utf-8"))
    vite_manifest["src/locales/pl.ts"]["css"] = ["assets/pl-styleshash.css"]
    vite_manifest_path.write_text(json.dumps(vite_manifest), encoding="utf-8")
    _refresh_build_manifest(dist_root)

    hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


def test_generated_dist_rejects_asset_tampering(tmp_path: Path) -> None:
    dist_root = tmp_path / "dist"
    _write_valid_dist(dist_root, repository_root=REPOSITORY_ROOT)
    hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)
    (dist_root / "assets" / "index-testhash.js").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(hatch_build.HermesUiBuildError, match="do not match"):
        hatch_build._validate_dist(dist_root, repository_root=REPOSITORY_ROOT)


def test_manifest_writer_never_follows_a_preexisting_symlink(tmp_path: Path) -> None:
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged", encoding="utf-8")
    try:
        (dist_root / hatch_build._BUILD_MANIFEST).symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(hatch_build.HermesUiBuildError, match="pre-existing UI build manifest"):
        hatch_build._write_build_manifest(
            dist_root,
            repository_root=REPOSITORY_ROOT,
            node_version=hatch_build._NODE_VERSION,
            npm_version=hatch_build._NPM_VERSION,
        )

    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_windows_build_environment_preserves_only_required_process_variables() -> None:
    host_environment = {
        "SystemRoot": r"C:\Windows",
        "ComSpec": r"C:\Windows\System32\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "NODE_OPTIONS": "--require=C:\\host\\inject.js",
        "VITE_HOST_SECRET": "host-secret",
    }

    assert hatch_build._allowlisted_windows_environment(
        host_environment,
        platform_name="nt",
    ) == {
        "SystemRoot": r"C:\Windows",
        "ComSpec": r"C:\Windows\System32\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
    }
    assert (
        hatch_build._allowlisted_windows_environment(
            host_environment,
            platform_name="posix",
        )
        == {}
    )


def test_docker_build_is_locked_and_rejects_unvalidated_ui_sources() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert dockerfile.count("COPY pyproject.toml uv.lock README.md hatch_build.py ./") == 2
    assert "RUN uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert "RUN uv sync --frozen --no-dev" in dockerfile
    assertion = "RUN test ! -e ./src/mcp_agent_mail/ui_dist"
    validated_copy = "COPY --from=ui-builder /ui/dist ./src/mcp_agent_mail/ui_dist"
    assert dockerfile.index(assertion) < dockerfile.index(validated_copy)
    assert {
        ".playwright-mcp/",
        "src/mcp_agent_mail/ui_dist/",
        "ui/playwright-report/",
        "ui/test-results/",
    }.issubset(dockerignore)


def test_node_build_environment_drops_host_injection_variables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_environments: list[dict[str, str]] = []
    stage = hatch_build._create_stage_directory(REPOSITORY_ROOT)

    monkeypatch.setenv("NODE_OPTIONS", "--require=/host/inject.js")
    monkeypatch.setenv("NODE_PATH", "/host/modules")
    monkeypatch.setenv("VITE_HOST_SECRET", "host-secret")
    monkeypatch.setenv("NODE_ENV", "development")
    monkeypatch.setattr(hatch_build, "_create_stage_directory", lambda _root: stage)
    monkeypatch.setattr(hatch_build, "_resolve_tool", lambda _name: "/fake/node")
    monkeypatch.setattr(hatch_build, "_resolve_npm_cli", lambda _node: Path("/fake/npm-cli.js"))
    monkeypatch.setattr(hatch_build, "_copy_build_inputs", lambda _root, _stage: stage.path / "ui")
    monkeypatch.setattr(hatch_build, "_source_digest", lambda _root: "stable-source")

    def capture_run(
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str] | None = None,
    ) -> str:
        del cwd
        assert environment is not None
        captured_environments.append(environment.copy())
        if command[-1] == "--version":
            return (
                hatch_build._NPM_VERSION
                if any(part.endswith("npm-cli.js") for part in command)
                else hatch_build._NODE_VERSION
            )
        if command[-1] == "build":
            dist_root = stage.path / "ui" / "dist"
            _write_vite_output(dist_root)
        return ""

    monkeypatch.setattr(hatch_build, "_run_checked", capture_run)

    built_stage, _dist_root = hatch_build._build_ui_in_stage(REPOSITORY_ROOT)
    try:
        assert len(captured_environments) == 4
        for captured_environment in captured_environments:
            for hostile_name in ("NODE_OPTIONS", "NODE_PATH", "NODE_ENV", "VITE_HOST_SECRET"):
                assert hostile_name not in captured_environment
            assert captured_environment["npm_config_globalconfig"] == str(stage.path / ".npm-globalrc")
            assert captured_environment["HOME"] == str(stage.path)
    finally:
        hatch_build._cleanup_stage_directory(built_stage, REPOSITORY_ROOT)


def test_isolated_build_rejects_a_live_source_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = hatch_build._create_stage_directory(REPOSITORY_ROOT)
    digest_calls = 0

    def changing_digest(_root: Path) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return "snapshot-a" if digest_calls <= 2 else "snapshot-b"

    monkeypatch.setattr(hatch_build, "_create_stage_directory", lambda _root: stage)
    monkeypatch.setattr(hatch_build, "_resolve_tool", lambda _name: "/fake/node")
    monkeypatch.setattr(hatch_build, "_resolve_npm_cli", lambda _node: Path("/fake/npm-cli.js"))
    monkeypatch.setattr(hatch_build, "_source_digest", changing_digest)
    monkeypatch.setattr(hatch_build, "_copy_build_inputs", lambda _root, _stage: stage.path / "ui")
    monkeypatch.setattr(
        hatch_build,
        "_run_checked",
        lambda command, **_kwargs: (
            hatch_build._NPM_VERSION
            if command[-1] == "--version" and any(part.endswith("npm-cli.js") for part in command)
            else hatch_build._NODE_VERSION
            if command[-1] == "--version"
            else ""
        ),
    )

    with pytest.raises(hatch_build.HermesUiBuildError, match=r"changed while.*being built"):
        hatch_build._build_ui_in_stage(REPOSITORY_ROOT)

    assert digest_calls == 3
    assert not stage.path.exists()


def test_hatch_custom_loader_imports_the_build_hook() -> None:
    loaded = load_plugin_from_script(
        str(REPOSITORY_ROOT / "hatch_build.py"),
        "hatch_build_custom_test",
        BuildHookInterface,
        "build_hook",
    )

    assert loaded.__name__ == "CustomBuildHook"
    assert issubclass(loaded, BuildHookInterface)


@pytest.mark.parametrize(
    ("target_name", "version"),
    [("wheel", "editable-v2"), ("sdist", "editable"), ("custom", "standard")],
)
def test_build_hook_rejects_unsupported_target_modes(
    target_name: str,
    version: str,
    tmp_path: Path,
) -> None:
    hook = _build_hook(REPOSITORY_ROOT, tmp_path, target_name)

    with pytest.raises(hatch_build.HermesUiBuildError, match="Unsupported Iris UI build target"):
        hook.initialize(version, {})
