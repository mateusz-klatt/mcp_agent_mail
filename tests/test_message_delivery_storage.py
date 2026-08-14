"""Fault-injection tests for immutable single-delivery Git publication."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from git import Git, Repo

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.storage import (
    AsyncFileLock,
    MessageDeliveryPendingError,
    MessageDeliveryQuarantinedError,
    _commit,
    _commit_lock_path,
    _commit_message_delivery_sync,
    _fsync_message_delivery_directory_sync,
    _fsync_readonly_file_sync,
    _is_ephemeral_archive_path,
    _publish_message_delivery_sync,
    _write_message_delivery_attempt_sync,
    collect_lock_status,
    ensure_archive,
    heal_archive_locks,
    publish_message_delivery,
)

DELIVERY_ID = "65f7dc90-af0b-4f6b-8619-842ff2e5c06d"


def _document(delivery_id: str = DELIVERY_ID, *, body: str = "Hello") -> bytes:
    return (
        "---json\n"
        f'{{"delivery_id":"{delivery_id}","schema_version":1}}\n'
        "---\n\n"
        f"{body}\n"
    ).encode()


def _sha256(document: bytes) -> str:
    return hashlib.sha256(document).hexdigest()


async def _start_lock_holder(
    lock_path: Path,
    *,
    age_seconds: float = 0.0,
) -> asyncio.subprocess.Process:
    source = """
import asyncio
import json
import sys
import time
from pathlib import Path

from mcp_agent_mail.storage import AsyncFileLock

async def main() -> None:
    lock_path = Path(sys.argv[1])
    age_seconds = float(sys.argv[2])
    lock = AsyncFileLock(lock_path, timeout_seconds=5.0)
    await lock.__aenter__()
    if age_seconds:
        metadata_path = lock_path.parent / f"{lock_path.name}.owner.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["created_ts"] = time.time() - age_seconds
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    print("ready", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    await lock.__aexit__(None, None, None)

asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        source,
        str(lock_path),
        str(age_seconds),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    ready = await asyncio.wait_for(process.stdout.readline(), timeout=10.0)
    if ready.strip() != b"ready":
        assert process.stderr is not None
        error = await process.stderr.read()
        raise AssertionError(
            f"lock holder failed before readiness: {error.decode(errors='replace')}"
        )
    return process


async def _stop_lock_holder(process: asyncio.subprocess.Process) -> None:
    assert process.stdin is not None
    process.stdin.write(b"\n")
    await process.stdin.drain()
    exit_code = await asyncio.wait_for(process.wait(), timeout=10.0)
    if exit_code != 0:
        assert process.stderr is not None
        error = await process.stderr.read()
        raise AssertionError(
            f"lock holder exited with {exit_code}: {error.decode(errors='replace')}"
        )


async def _start_raw_lock_holder(
    lock_path: Path,
    *,
    corrupt_metadata: bool,
) -> asyncio.subprocess.Process:
    source = """
import sys
import time
from pathlib import Path

from filelock import SoftFileLock

lock_path = Path(sys.argv[1])
lock = SoftFileLock(str(lock_path), thread_local=False)
lock.acquire()
old = time.time() - 3600.0
lock_path.touch()
lock_path.chmod(0o600)
import os
os.utime(lock_path, (old, old))
if sys.argv[2] == "corrupt":
    lock_path.with_name(f"{lock_path.name}.owner.json").write_text("{corrupt")
print("ready", flush=True)
sys.stdin.readline()
lock.release()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        source,
        str(lock_path),
        "corrupt" if corrupt_metadata else "missing",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    ready = await asyncio.wait_for(process.stdout.readline(), timeout=10.0)
    if ready.strip() != b"ready":
        assert process.stderr is not None
        error = await process.stderr.read()
        raise AssertionError(
            f"raw lock holder failed before readiness: {error.decode(errors='replace')}"
        )
    return process


@pytest.mark.asyncio
async def test_cancelled_lock_acquisition_drains_late_worker_before_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / ".commit.lock"
    delayed = AsyncFileLock(lock_path, timeout_seconds=5.0)
    original_acquire = delayed._lock.acquire
    worker_entered = threading.Event()
    release_worker = threading.Event()

    def delayed_acquire(*args: Any, **kwargs: Any) -> Any:
        worker_entered.set()
        assert release_worker.wait(timeout=10.0)
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(delayed._lock, "acquire", delayed_acquire)
    entering = asyncio.create_task(delayed.__aenter__())
    assert await asyncio.wait_for(
        asyncio.to_thread(worker_entered.wait, 10.0),
        timeout=11.0,
    )
    entering.cancel()
    await asyncio.sleep(0.05)
    assert not entering.done()
    entering.cancel()
    await asyncio.sleep(0.05)
    assert not entering.done()
    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await entering

    assert not delayed._held
    assert not delayed._lock.is_locked
    assert not lock_path.exists()
    assert not lock_path.with_name(".commit.lock.owner.json").exists()
    successor = AsyncFileLock(lock_path, timeout_seconds=1.0)
    await successor.__aenter__()
    await successor.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_repeated_cancellation_during_lock_release_blocks_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / ".commit.lock"
    first = AsyncFileLock(lock_path, timeout_seconds=1.0)
    await first.__aenter__()
    original_release = first._release_strict
    worker_entered = threading.Event()
    release_worker = threading.Event()

    def delayed_release() -> bool:
        worker_entered.set()
        assert release_worker.wait(timeout=10.0)
        return original_release()

    monkeypatch.setattr(first, "_release_strict", delayed_release)
    exiting = asyncio.create_task(first.__aexit__(None, None, None))
    assert await asyncio.wait_for(
        asyncio.to_thread(worker_entered.wait, 10.0),
        timeout=11.0,
    )
    exiting.cancel()
    await asyncio.sleep(0.05)
    assert not exiting.done()
    exiting.cancel()
    await asyncio.sleep(0.05)
    assert not exiting.done()

    successor = AsyncFileLock(lock_path, timeout_seconds=1.0)
    successor_entering = asyncio.create_task(successor.__aenter__())
    await asyncio.sleep(0.05)
    assert not successor_entering.done()
    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await exiting
    await successor_entering
    await successor.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_cancelled_generic_git_worker_keeps_global_lock_until_completion(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_agent_mail import storage as storage_module

    archive = await ensure_archive(get_settings(), "cancel-generic-worker")
    first_path = archive.root / "first.json"
    second_path = archive.root / "second.json"
    first_path.write_text('{"first":true}\n', encoding="utf-8")
    second_path.write_text('{"second":true}\n', encoding="utf-8")
    first_relative = first_path.relative_to(archive.repo_root).as_posix()
    second_relative = second_path.relative_to(archive.repo_root).as_posix()
    original_normalize = storage_module._normalize_archive_text_line_endings
    worker_entered = threading.Event()
    release_worker = threading.Event()

    def block_first_worker(repo_root: Path, paths: list[str]) -> None:
        if paths == [first_relative]:
            worker_entered.set()
            assert release_worker.wait(timeout=10.0)
        original_normalize(repo_root, paths)

    monkeypatch.setattr(
        storage_module,
        "_normalize_archive_text_line_endings",
        block_first_worker,
    )
    first = asyncio.create_task(
        _commit(
            archive.repo,
            archive.settings,
            "test: first generic worker",
            [first_relative],
            use_queue=False,
        )
    )
    assert await asyncio.wait_for(
        asyncio.to_thread(worker_entered.wait, 10.0),
        timeout=11.0,
    )
    first.cancel()
    successor = asyncio.create_task(
        _commit(
            archive.repo,
            archive.settings,
            "test: successor generic worker",
            [second_relative],
            use_queue=False,
        )
    )
    await asyncio.sleep(0.05)
    assert not first.done()
    assert not successor.done()
    first.cancel()
    await asyncio.sleep(0.05)
    assert not first.done()
    assert not successor.done()
    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await successor
    with Repo(str(archive.repo_root)) as repo:
        assert (repo.head.commit.tree / second_relative).data_stream.read() == b'{"second":true}\n'


@pytest.mark.asyncio
async def test_publish_delivery_is_one_verified_immutable_commit(isolated_env: Any) -> None:
    archive = await ensure_archive(get_settings(), "delivery-storage")
    document = _document()

    publication = await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=1,
    )

    expected_path = f"projects/{archive.slug}/message_deliveries/{DELIVERY_ID}.md"
    assert publication.relative_path == expected_path
    assert publication.document_sha256 == _sha256(document)
    assert publication.recovered is False
    with Repo(str(archive.repo_root)) as repo:
        commit = repo.commit(publication.commit_sha)
        blob = commit.tree / expected_path
        assert blob.data_stream.read() == document
        assert publication.blob_sha == blob.hexsha
        changed = repo.git.diff_tree(
            "--root",
            "--no-commit-id",
            "--name-status",
            "--no-renames",
            "-r",
            "-z",
            publication.commit_sha,
        )
        assert [part for part in changed.split("\0") if part] == ["A", expected_path]

    retried = await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=2,
    )
    assert retried.recovered is True
    assert retried.commit_sha == publication.commit_sha
    assert list((archive.root / "message_deliveries").glob("*.md")) == [
        archive.root / "message_deliveries" / f"{DELIVERY_ID}.md"
    ]


@pytest.mark.asyncio
async def test_delivery_git_writes_force_command_local_power_loss_durability(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = await ensure_archive(get_settings(), "delivery-git-durability")
    document = _document()
    original_call_process = Git._call_process
    durability_options: dict[str, tuple[str, ...]] = {}

    def capture_delivery_git_options(
        git: Git,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if method in {"add", "commit"} and any(
            DELIVERY_ID in str(argument) for argument in args
        ):
            durability_options[method] = tuple(git._git_options)
        return original_call_process(git, method, *args, **kwargs)

    monkeypatch.setattr(Git, "_call_process", capture_delivery_git_options)
    await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=1,
    )

    expected_options = (
        "-c",
        "core.longpaths=true",
        "-c",
        "core.fsync=all",
        "-c",
        "core.fsyncMethod=fsync",
    )
    assert durability_options == {
        "add": expected_options,
        "commit": expected_options,
    }
    with Repo(str(archive.repo_root)) as repo:
        assert not repo.config_reader("repository").has_option("core", "fsync")
        assert not repo.config_reader("repository").has_option("core", "fsyncMethod")


@pytest.mark.asyncio
async def test_first_archive_and_delivery_persist_every_power_loss_boundary(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_agent_mail import storage as storage_module

    settings = get_settings()
    repo_root = Path(settings.storage.root).expanduser().resolve()
    original_fsync_directory = _fsync_message_delivery_directory_sync
    original_call_process = Git._call_process
    fsynced_directories: list[Path] = []
    durable_git_commands: list[tuple[str, tuple[str, ...]]] = []

    def record_fsync_directory(directory: Path) -> None:
        fsynced_directories.append(directory.resolve())
        original_fsync_directory(directory)

    def record_git_options(
        git: Git,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if method in {"add", "commit"} and any(
            value in str(argument)
            for value in (".gitattributes", DELIVERY_ID)
            for argument in args
        ):
            durable_git_commands.append((method, tuple(git._git_options)))
        return original_call_process(git, method, *args, **kwargs)

    monkeypatch.setattr(
        storage_module,
        "_fsync_message_delivery_directory_sync",
        record_fsync_directory,
    )
    monkeypatch.setattr(Git, "_call_process", record_git_options)
    archive = await ensure_archive(settings, "first-durable-delivery")
    document = _document()
    await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=1,
    )

    expected_options = (
        "-c",
        "core.longpaths=true",
        "-c",
        "core.fsync=all",
        "-c",
        "core.fsyncMethod=fsync",
    )
    assert durable_git_commands == [
        ("add", expected_options),
        ("commit", expected_options),
        ("add", expected_options),
        ("commit", expected_options),
    ]
    assert repo_root.parent in fsynced_directories
    assert repo_root in fsynced_directories
    assert (repo_root / ".git") in fsynced_directories
    assert (repo_root / "projects") in fsynced_directories
    assert archive.root in fsynced_directories
    assert (archive.root / "message_deliveries") in fsynced_directories


def test_windows_directory_boundary_is_explicit_best_effort_without_native_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_agent_mail import storage as storage_module

    monkeypatch.setattr(storage_module.os, "name", "nt")
    monkeypatch.setattr(
        storage_module.os,
        "open",
        lambda *args, **kwargs: pytest.fail("Windows must not fake POSIX directory fsync"),
    )
    _fsync_message_delivery_directory_sync(tmp_path)


def test_windows_readonly_file_boundary_skips_unsupported_crt_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_agent_mail import storage as storage_module

    existing_file = tmp_path / "existing"
    existing_file.write_bytes(b"already durable")
    monkeypatch.setattr(storage_module.os, "name", "nt")
    monkeypatch.setattr(
        storage_module.os,
        "open",
        lambda *args, **kwargs: pytest.fail(
            "Windows must not fsync an O_RDONLY CRT descriptor"
        ),
    )

    _fsync_readonly_file_sync(existing_file)


def test_posix_readonly_file_boundary_flushes_and_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_agent_mail import storage as storage_module

    existing_file = tmp_path / "existing"
    existing_file.write_bytes(b"already durable")
    # Record the raw filesystem path rather than a Path. Patching os.name below
    # also switches pathlib's flavour, so a Path built inside the patch is a
    # PosixPath while tmp_path stayed a WindowsPath, and the two never compare
    # equal on Windows even for an identical path.
    opened: list[tuple[str, int]] = []
    flushed: list[int] = []
    closed: list[int] = []

    monkeypatch.setattr(storage_module.os, "name", "posix")
    monkeypatch.setattr(
        storage_module.os,
        "open",
        lambda path, flags: opened.append((os.fspath(path), flags)) or 47,
    )
    monkeypatch.setattr(storage_module.os, "fsync", flushed.append)
    monkeypatch.setattr(storage_module.os, "close", closed.append)

    _fsync_readonly_file_sync(existing_file)

    assert opened == [(os.fspath(existing_file), os.O_RDONLY | getattr(os, "O_BINARY", 0))]
    assert flushed == [47]
    assert closed == [47]


@pytest.mark.asyncio
@pytest.mark.parametrize("symlink_component", ["projects", "project"])
async def test_internal_archive_ancestor_symlink_is_rejected_before_mutation(
    isolated_env: Any,
    symlink_component: str,
) -> None:
    settings = get_settings()
    repo_root = Path(settings.storage.root).expanduser().resolve()
    repo_root.mkdir(parents=True)
    initialized = Repo.init(repo_root)
    initialized.close()
    evil_root = repo_root / ".git" / "evil"
    evil_root.mkdir()
    projects_root = repo_root / "projects"
    if symlink_component == "projects":
        projects_root.symlink_to(Path(".git") / "evil", target_is_directory=True)
    else:
        projects_root.mkdir()
        (projects_root / "symlink-project").symlink_to(
            Path("..") / ".git" / "evil",
            target_is_directory=True,
        )

    with pytest.raises(ValueError, match="not a regular directory"):
        await ensure_archive(settings, "symlink-project")
    assert not (evil_root / "message_deliveries").exists()
    assert not (repo_root / ".gitattributes").exists()
    with Repo(repo_root) as repo:
        assert not repo.head.is_valid()


@pytest.mark.asyncio
async def test_partial_attempt_is_invisible_and_retryable(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = await ensure_archive(get_settings(), "delivery-partial-write")
    document = _document()
    original_write = _write_message_delivery_attempt_sync
    calls = 0

    def partial_write(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(descriptor, content[:7])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            raise OSError("injected killed writer")
        original_write(path, content)

    monkeypatch.setattr(
        "mcp_agent_mail.storage._write_message_delivery_attempt_sync",
        partial_write,
    )
    with pytest.raises(MessageDeliveryPendingError, match="attempt write failed"):
        await publish_message_delivery(
            archive,
            DELIVERY_ID,
            document,
            _sha256(document),
            lease_fence=7,
        )

    delivery_dir = archive.root / "message_deliveries"
    pending_after_failure = list(delivery_dir.glob("*.pending"))
    assert len(pending_after_failure) == 1
    relative_pending = pending_after_failure[0].relative_to(archive.repo_root).as_posix()
    assert _is_ephemeral_archive_path(relative_pending)
    assert not (delivery_dir / f"{DELIVERY_ID}.md").exists()

    publication = await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=7,
    )
    assert publication.recovered is False
    pending_after_retry = list(delivery_dir.glob("*.pending"))
    assert len(pending_after_retry) == 2
    assert len({path.name for path in pending_after_retry}) == 2


@pytest.mark.asyncio
async def test_unsupported_hard_link_leaves_only_retryable_pending_attempt(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = await ensure_archive(get_settings(), "delivery-link-unsupported")
    document = _document()
    original_link = os.link

    def unsupported_link(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("injected hard-link unsupported")

    monkeypatch.setattr("mcp_agent_mail.storage.os.link", unsupported_link)
    with pytest.raises(MessageDeliveryPendingError, match="hard-link publication is unavailable"):
        await publish_message_delivery(
            archive,
            DELIVERY_ID,
            document,
            _sha256(document),
            lease_fence=1,
        )

    delivery_dir = archive.root / "message_deliveries"
    assert len(list(delivery_dir.glob("*.pending"))) == 1
    assert not (delivery_dir / f"{DELIVERY_ID}.md").exists()

    monkeypatch.setattr("mcp_agent_mail.storage.os.link", original_link)
    publication = await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=2,
    )
    assert publication.recovered is False


@pytest.mark.asyncio
async def test_commit_return_loss_recovers_committed_blob(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = await ensure_archive(get_settings(), "delivery-commit-loss")
    document = _document()
    original_commit = _commit_message_delivery_sync

    def commit_then_lose_return(*args: Any, **kwargs: Any) -> None:
        original_commit(*args, **kwargs)
        raise OSError("injected commit return loss")

    monkeypatch.setattr(
        "mcp_agent_mail.storage._commit_message_delivery_sync",
        commit_then_lose_return,
    )
    publication = await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=1,
    )

    assert publication.recovered is True
    with Repo(str(archive.repo_root)) as repo:
        touching = list(repo.iter_commits(paths=[publication.relative_path]))
    assert [commit.hexsha for commit in touching] == [publication.commit_sha]


@pytest.mark.asyncio
async def test_cancellation_waits_for_publisher_then_retry_recovers(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = await ensure_archive(get_settings(), "delivery-cancelled-publisher")
    document = _document()
    worker_entered = threading.Event()
    release_worker = threading.Event()
    original_publish = _publish_message_delivery_sync

    def blocked_publish(*args: Any, **kwargs: Any) -> Any:
        worker_entered.set()
        assert release_worker.wait(timeout=10.0)
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(
        "mcp_agent_mail.storage._publish_message_delivery_sync",
        blocked_publish,
    )
    publication_task = asyncio.create_task(
        publish_message_delivery(
            archive,
            DELIVERY_ID,
            document,
            _sha256(document),
            lease_fence=1,
        )
    )
    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(worker_entered.wait, 10.0),
            timeout=11.0,
        )
        publication_task.cancel()
        await asyncio.sleep(0.05)
        assert not publication_task.done()
        publication_task.cancel()
        await asyncio.sleep(0.05)
        assert not publication_task.done()
    finally:
        release_worker.set()

    with pytest.raises(asyncio.CancelledError):
        await publication_task
    monkeypatch.setattr(
        "mcp_agent_mail.storage._publish_message_delivery_sync",
        original_publish,
    )
    recovered = await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=2,
    )
    assert recovered.recovered is True
    with Repo(str(archive.repo_root)) as repo:
        assert len(
            list(repo.iter_commits(paths=[f":(literal){recovered.relative_path}"]))
        ) == 1


@pytest.mark.asyncio
async def test_concurrent_same_delivery_has_one_document_and_commit(isolated_env: Any) -> None:
    archive = await ensure_archive(get_settings(), "delivery-concurrent")
    document = _document()

    first, second = await asyncio.gather(
        publish_message_delivery(
            archive,
            DELIVERY_ID,
            document,
            _sha256(document),
            lease_fence=1,
        ),
        publish_message_delivery(
            archive,
            DELIVERY_ID,
            document,
            _sha256(document),
            lease_fence=2,
        ),
    )

    assert first.commit_sha == second.commit_sha
    assert {first.recovered, second.recovered} == {False, True}
    assert len(list((archive.root / "message_deliveries").glob("*.md"))) == 1
    with Repo(str(archive.repo_root)) as repo:
        assert len(list(repo.iter_commits(paths=[first.relative_path]))) == 1


@pytest.mark.asyncio
async def test_hash_and_existing_path_mismatches_are_rejected(
    isolated_env: Any,
) -> None:
    archive = await ensure_archive(get_settings(), "delivery-mismatch")
    document = _document()
    wrong_document = _document(body="Different")

    with pytest.raises(ValueError, match="document SHA-256 mismatch"):
        await publish_message_delivery(
            archive,
            DELIVERY_ID,
            document,
            _sha256(wrong_document),
            lease_fence=1,
        )
    assert not (archive.root / "message_deliveries").exists()

    await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=1,
    )
    with pytest.raises(MessageDeliveryQuarantinedError) as captured:
        await publish_message_delivery(
            archive,
            DELIVERY_ID,
            wrong_document,
            _sha256(wrong_document),
            lease_fence=2,
        )
    assert captured.value.expected_sha256 == _sha256(wrong_document)
    assert captured.value.actual_sha256 == _sha256(document)


@pytest.mark.parametrize(
    "slug",
    [
        "",
        ".",
        "../escape",
        "two/parts",
        "two\\parts",
        "/absolute",
        "Uppercase",
        "with.dot",
        "with[meta]",
        "a" * 256,
    ],
)
@pytest.mark.asyncio
async def test_ensure_archive_rejects_noncanonical_slug_before_mkdir(
    isolated_env: Any,
    slug: str,
) -> None:
    settings = get_settings()
    storage_root = Path(settings.storage.root)

    with pytest.raises(ValueError, match="project slug must be a canonical"):
        await ensure_archive(settings, slug)

    assert not storage_root.exists()


@pytest.mark.asyncio
async def test_new_archive_directories_are_parent_fsynced(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    repo_root = Path(settings.storage.root).resolve()
    calls: list[Path] = []

    def record_fsync(directory: Path) -> None:
        calls.append(directory.resolve())

    monkeypatch.setattr(
        "mcp_agent_mail.storage._fsync_message_delivery_directory_sync",
        record_fsync,
    )
    archive = await ensure_archive(settings, "durable-project-directory")

    assert archive.root == repo_root / "projects" / "durable-project-directory"
    assert calls[-1] == repo_root / "projects"
    assert repo_root in calls
    assert archive.root.is_dir()
    assert repo_root.parent in calls


@pytest.mark.asyncio
async def test_publisher_revalidates_archive_slug_and_root(isolated_env: Any) -> None:
    archive = await ensure_archive(get_settings(), "delivery-safe-path")
    document = _document()
    original_root = archive.root

    archive.slug = "delivery[meta]"
    with pytest.raises(ValueError, match="project slug must be a canonical"):
        await publish_message_delivery(
            archive,
            DELIVERY_ID,
            document,
            _sha256(document),
            lease_fence=1,
        )

    archive.slug = "delivery-safe-path"
    archive.root = archive.repo_root / "projects" / "different-path"
    with pytest.raises(ValueError, match="root does not match"):
        await publish_message_delivery(
            archive,
            DELIVERY_ID,
            document,
            _sha256(document),
            lease_fence=2,
        )
    assert not (original_root / "message_deliveries").exists()


@pytest.mark.asyncio
async def test_foreign_staged_unstaged_and_untracked_status_is_preserved(
    isolated_env: Any,
) -> None:
    archive = await ensure_archive(get_settings(), "delivery-foreign-state")
    staged_path = archive.root / "staged.md"
    staged_path.write_text("staged\n", encoding="utf-8", newline="\n")
    unstaged_path = archive.repo_root / ".gitattributes"
    unstaged_path.write_text("*.md text\nforeign dirty\n", encoding="utf-8", newline="\n")
    untracked_path = archive.root / "untracked.md"
    untracked_path.write_text("untracked\n", encoding="utf-8", newline="\n")
    relative_staged = staged_path.relative_to(archive.repo_root).as_posix()
    with Repo(str(archive.repo_root)) as repo:
        repo.index.add([relative_staged])
        status_before = repo.git.status("--porcelain=v1", "-z")
    document = _document()

    publication = await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=1,
    )

    with Repo(str(archive.repo_root)) as repo:
        status_after = repo.git.status("--porcelain=v1", "-z")
        assert (repo.head.commit.tree / publication.relative_path).data_stream.read() == document
        assert repo.index.entries[(publication.relative_path, 0)].binsha.hex() == (
            repo.head.commit.tree / publication.relative_path
        ).hexsha
    assert status_after == status_before


@pytest.mark.asyncio
async def test_two_projects_share_global_commit_lock_and_both_publish(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_archive = await ensure_archive(get_settings(), "delivery-project-one")
    second_archive = await ensure_archive(get_settings(), "delivery-project-two")
    first_id = DELIVERY_ID
    second_id = "749852bd-9e40-4d16-b006-78649a0b013c"
    first_document = _document(first_id, body="One")
    second_document = _document(second_id, body="Two")
    assert _commit_lock_path(first_archive.repo_root, ["projects/one/a.md"]) == (
        first_archive.lock_path.parent / ".commit.lock"
    )
    assert _commit_lock_path(second_archive.repo_root, ["projects/two/b.md"]) == (
        first_archive.lock_path.parent / ".commit.lock"
    )
    original_commit = _commit_message_delivery_sync
    active_commits = 0
    maximum_active_commits = 0
    calls_lock = threading.Lock()

    def measured_commit(*args: Any, **kwargs: Any) -> None:
        nonlocal active_commits, maximum_active_commits
        with calls_lock:
            active_commits += 1
            maximum_active_commits = max(maximum_active_commits, active_commits)
        try:
            time.sleep(0.05)
            original_commit(*args, **kwargs)
        finally:
            with calls_lock:
                active_commits -= 1

    monkeypatch.setattr(
        "mcp_agent_mail.storage._commit_message_delivery_sync",
        measured_commit,
    )
    first, second = await asyncio.gather(
        publish_message_delivery(
            first_archive,
            first_id,
            first_document,
            _sha256(first_document),
            lease_fence=1,
        ),
        publish_message_delivery(
            second_archive,
            second_id,
            second_document,
            _sha256(second_document),
            lease_fence=1,
        ),
    )

    assert first.commit_sha != second.commit_sha
    assert maximum_active_commits == 1
    with Repo(str(first_archive.repo_root)) as repo:
        assert (repo.head.commit.tree / first.relative_path).data_stream.read() == first_document
        assert (repo.head.commit.tree / second.relative_path).data_stream.read() == second_document
        assert len(list(repo.iter_commits(paths=[first.relative_path]))) == 1
        assert len(list(repo.iter_commits(paths=[second.relative_path]))) == 1


@pytest.mark.asyncio
async def test_subsequent_legacy_index_commit_retains_delivery(isolated_env: Any) -> None:
    archive = await ensure_archive(get_settings(), "delivery-then-legacy")
    document = _document()
    publication = await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=1,
    )
    legacy_path = archive.root / "legacy-record.md"
    legacy_path.write_text("legacy\n", encoding="utf-8", newline="\n")
    legacy_relative_path = legacy_path.relative_to(archive.repo_root).as_posix()

    await _commit(
        archive.repo,
        archive.settings,
        "legacy: follow delivery",
        [legacy_relative_path],
        use_queue=False,
    )

    with Repo(str(archive.repo_root)) as repo:
        assert (repo.head.commit.tree / publication.relative_path).data_stream.read() == document
        assert (repo.head.commit.tree / legacy_relative_path).data_stream.read() == b"legacy\n"
        assert len(list(repo.iter_commits(paths=[publication.relative_path]))) == 1


@pytest.mark.asyncio
async def test_failed_delivery_stage_survives_exact_legacy_commit_then_retries(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = await ensure_archive(get_settings(), "delivery-failed-stage")
    document = _document()
    original_commit = _commit_message_delivery_sync

    def fail_before_commit(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("injected commit failure")

    monkeypatch.setattr(
        "mcp_agent_mail.storage._commit_message_delivery_sync",
        fail_before_commit,
    )
    with pytest.raises(MessageDeliveryPendingError, match="Git commit failed"):
        await publish_message_delivery(
            archive,
            DELIVERY_ID,
            document,
            _sha256(document),
            lease_fence=1,
        )

    delivery_relative_path = (
        f"projects/{archive.slug}/message_deliveries/{DELIVERY_ID}.md"
    )
    legacy_path = archive.root / "legacy-after-failure.md"
    legacy_path.write_text("legacy\n", encoding="utf-8", newline="\n")
    legacy_relative_path = legacy_path.relative_to(archive.repo_root).as_posix()
    await _commit(
        archive.repo,
        archive.settings,
        "legacy: exact path after failed delivery",
        [legacy_relative_path],
        use_queue=False,
    )

    with Repo(str(archive.repo_root)) as repo:
        legacy_commit = repo.head.commit
        changed = repo.git.diff_tree(
            "--root",
            "--no-commit-id",
            "--name-status",
            "--no-renames",
            "-r",
            "-z",
            legacy_commit.hexsha,
        )
        assert [part for part in changed.split("\0") if part] == [
            "A",
            legacy_relative_path,
        ]
        assert delivery_relative_path not in legacy_commit.tree
        assert (delivery_relative_path, 0) in repo.index.entries

    monkeypatch.setattr(
        "mcp_agent_mail.storage._commit_message_delivery_sync",
        original_commit,
    )
    publication = await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=2,
    )
    with Repo(str(archive.repo_root)) as repo:
        assert (repo.head.commit.tree / publication.relative_path).data_stream.read() == document
        assert len(list(repo.iter_commits(paths=[f":(literal){publication.relative_path}"]))) == 1


@pytest.mark.asyncio
async def test_aged_global_commit_lock_with_live_owner_is_not_broken(
    isolated_env: Any,
) -> None:
    lock_path = Path(get_settings().storage.root) / ".commit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = await _start_lock_holder(lock_path, age_seconds=3600.0)
    try:
        waiter = AsyncFileLock(
            lock_path,
            timeout_seconds=0.25,
            stale_timeout_seconds=0.01,
            max_retries=0,
        )
        with pytest.raises(TimeoutError):
            await waiter.__aenter__()
        assert holder.returncode is None
        assert lock_path.exists()
    finally:
        await _stop_lock_holder(holder)


@pytest.mark.parametrize("corrupt_metadata", [False, True])
@pytest.mark.asyncio
async def test_global_commit_lock_with_unknown_owner_fails_closed(
    isolated_env: Any,
    corrupt_metadata: bool,
) -> None:
    settings = get_settings()
    lock_path = Path(settings.storage.root) / ".commit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = await _start_raw_lock_holder(
        lock_path,
        corrupt_metadata=corrupt_metadata,
    )
    try:
        waiter = AsyncFileLock(
            lock_path,
            timeout_seconds=0.25,
            stale_timeout_seconds=0.01,
            max_retries=0,
        )
        with pytest.raises(TimeoutError):
            await waiter.__aenter__()
        status = collect_lock_status(settings)
        commit_locks = [
            row for row in status["locks"] if row["path"] == str(lock_path)
        ]
        assert len(commit_locks) == 1
        assert commit_locks[0]["stale_suspected"] is False
        healed = await heal_archive_locks(settings)
        assert str(lock_path) not in healed["locks_removed"]
        assert holder.returncode is None
        assert lock_path.exists()
    finally:
        await _stop_lock_holder(holder)


@pytest.mark.asyncio
async def test_dead_global_commit_lock_resists_two_healers_and_successor(
    isolated_env: Any,
) -> None:
    settings = get_settings()
    lock_path = Path(settings.storage.root) / ".commit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")
    old = time.time() - 3600.0
    os.utime(lock_path, (old, old))
    metadata_path = lock_path.with_name(f"{lock_path.name}.owner.json")
    metadata_path.write_text(
        '{"pid":999999999,"created_ts":0}',
        encoding="utf-8",
    )
    first_healer = AsyncFileLock(lock_path, stale_timeout_seconds=0.01)
    second_healer = AsyncFileLock(lock_path, stale_timeout_seconds=0.01)

    healed = await asyncio.gather(
        asyncio.to_thread(first_healer._cleanup_if_stale),
        asyncio.to_thread(second_healer._cleanup_if_stale),
    )

    assert healed == [False, False]
    assert lock_path.exists()
    assert metadata_path.exists()
    successor = AsyncFileLock(
        lock_path,
        timeout_seconds=0.25,
        stale_timeout_seconds=0.01,
        max_retries=0,
    )
    with pytest.raises(TimeoutError):
        await successor.__aenter__()
    status = collect_lock_status(settings)
    commit_lock = next(
        row for row in status["locks"] if row["path"] == str(lock_path)
    )
    assert commit_lock["stale_suspected"] is True
    result = await heal_archive_locks(settings)
    assert str(lock_path) not in result["locks_removed"]
    assert lock_path.exists()
    assert metadata_path.exists()


@pytest.mark.asyncio
async def test_global_lock_handoff_never_unlinks_successor(
    isolated_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = Path(get_settings().storage.root) / ".commit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    first = AsyncFileLock(lock_path, timeout_seconds=1.0)
    await first.__aenter__()
    original_release = first._release_strict
    first_released = threading.Event()
    allow_first_to_return = threading.Event()

    def pause_after_release() -> bool:
        released = original_release()
        first_released.set()
        assert allow_first_to_return.wait(timeout=10.0)
        return released

    monkeypatch.setattr(first, "_release_strict", pause_after_release)
    first_exit = asyncio.create_task(first.__aexit__(None, None, None))
    successor: asyncio.subprocess.Process | None = None
    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(first_released.wait, 10.0),
            timeout=11.0,
        )
        successor = await _start_lock_holder(lock_path)
        allow_first_to_return.set()
        await first_exit

        third = AsyncFileLock(
            lock_path,
            timeout_seconds=0.25,
            stale_timeout_seconds=3600.0,
            max_retries=0,
        )
        with pytest.raises(TimeoutError):
            await third.__aenter__()
        assert successor.returncode is None
        assert lock_path.exists()
    finally:
        allow_first_to_return.set()
        if not first_exit.done():
            await first_exit
        if successor is not None:
            await _stop_lock_holder(successor)


@pytest.mark.asyncio
async def test_pending_alias_and_final_document_are_read_only(isolated_env: Any) -> None:
    archive = await ensure_archive(get_settings(), "delivery-read-only")
    document = _document()
    await publish_message_delivery(
        archive,
        DELIVERY_ID,
        document,
        _sha256(document),
        lease_fence=1,
    )

    delivery_dir = archive.root / "message_deliveries"
    final_path = delivery_dir / f"{DELIVERY_ID}.md"
    pending_path = next(delivery_dir.glob("*.pending"))
    final_mode = stat.S_IMODE(final_path.stat().st_mode)
    pending_mode = stat.S_IMODE(pending_path.stat().st_mode)
    assert final_mode & 0o222 == 0
    assert pending_mode & 0o222 == 0
    if os.name == "nt" or os.geteuid() != 0:
        with pytest.raises(PermissionError):
            pending_path.write_bytes(b"mutated")
    else:
        attempt = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'x')",
                str(pending_path),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            user=65534,
        )
        assert attempt.returncode != 0
    assert final_path.read_bytes() == document


def test_attempt_permissions_are_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_create_modes: list[int] = []
    requested_final_modes: list[int] = []
    original_open = os.open
    original_fchmod = os.fchmod

    def recording_open(path: Path, flags: int, mode: int = 0o777) -> int:
        requested_create_modes.append(mode)
        return original_open(path, flags, mode)

    def recording_fchmod(file_descriptor: int, mode: int) -> None:
        requested_final_modes.append(mode)
        original_fchmod(file_descriptor, mode)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fchmod", recording_fchmod)

    _write_message_delivery_attempt_sync(tmp_path / "attempt.pending", _document())

    assert requested_create_modes == [stat.S_IRUSR | stat.S_IWUSR]
    assert requested_final_modes == [stat.S_IRUSR]
