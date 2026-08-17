"""Tests for the Git repository LRU cache (file handle management).

These tests verify the _LRURepoCache class behavior to prevent EMFILE errors
under heavy load. Addresses GitHub issue #59.

Reference: mcp_agent_mail-jto (Bug: File handle exhaustion)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import threading
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from git import Repo

from mcp_agent_mail import storage as storage_module
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.storage import (
    _ensure_repo,
    _GlobalAsyncCapacityLimiter,
    _LRURepoCache,
    clear_repo_cache,
    get_repo_cache_stats,
)


class TestLRURepoCacheBasics:
    """Test basic LRU cache operations."""

    def test_cache_default_maxsize_is_16(self):
        """Default maxsize should be 16 (increased from 8 for better concurrency)."""
        cache = _LRURepoCache()
        assert cache._maxsize == 16

    def test_cache_custom_maxsize(self):
        """Custom maxsize should be respected."""
        cache = _LRURepoCache(maxsize=4)
        assert cache._maxsize == 4

    def test_cache_minimum_maxsize_is_1(self):
        """Maxsize should be at least 1."""
        cache = _LRURepoCache(maxsize=0)
        assert cache._maxsize == 1
        cache = _LRURepoCache(maxsize=-5)
        assert cache._maxsize == 1

    def test_put_and_get(self):
        """Basic put and get operations should work."""
        cache = _LRURepoCache(maxsize=4)
        mock_repo = MagicMock()

        cache.put("/path/to/repo", mock_repo)
        assert cache.get("/path/to/repo") is mock_repo
        assert len(cache) == 1

    def test_peek_does_not_update_lru_order(self):
        """Peek should not update LRU order."""
        cache = _LRURepoCache(maxsize=4)
        repo1 = MagicMock()
        repo2 = MagicMock()

        cache.put("repo1", repo1)
        cache.put("repo2", repo2)

        # Peek at repo1 - should NOT move it to end
        assert cache.peek("repo1") is repo1

        # Order should still be [repo1, repo2] (oldest first)
        assert cache._order == ["repo1", "repo2"]

    def test_get_updates_lru_order(self):
        """Get should update LRU order (move to most recently used)."""
        cache = _LRURepoCache(maxsize=4)
        repo1 = MagicMock()
        repo2 = MagicMock()

        cache.put("repo1", repo1)
        cache.put("repo2", repo2)

        # Get repo1 - should move it to end
        assert cache.get("repo1") is repo1

        # Order should now be [repo2, repo1]
        assert cache._order == ["repo2", "repo1"]

    def test_contains(self):
        """Contains check should work."""
        cache = _LRURepoCache(maxsize=4)
        mock_repo = MagicMock()

        cache.put("repo1", mock_repo)
        assert "repo1" in cache
        assert "repo2" not in cache


class TestLRURepoCacheEviction:
    """Test LRU eviction behavior."""

    def test_eviction_at_capacity(self):
        """Oldest repos should be evicted when at capacity."""
        cache = _LRURepoCache(maxsize=2)
        repo1 = MagicMock()
        repo2 = MagicMock()
        repo3 = MagicMock()

        cache.put("repo1", repo1)
        cache.put("repo2", repo2)

        # Verify repo1 is in cache before eviction
        assert "repo1" in cache
        assert len(cache) == 2

        cache.put("repo3", repo3)  # This should evict repo1

        assert len(cache) == 2
        assert "repo1" not in cache  # Evicted from cache
        assert "repo2" in cache
        assert "repo3" in cache
        # repo1 was evicted - it's either in _evicted list or was cleaned up
        # (depending on refcount at cleanup time). Key assertion is it's no longer in cache.

    def test_evicted_repos_added_to_evicted_list(self):
        """Evicted repos should be tracked for later cleanup with timestamps."""
        cache = _LRURepoCache(maxsize=1)
        repo1 = MagicMock()
        repo2 = MagicMock()

        cache.put("repo1", repo1)

        # Mock cleanup to prevent immediate cleanup and verify eviction mechanism
        evicted_during_put: list = []
        original_cleanup = cache._cleanup_evicted
        def tracking_cleanup(**kwargs: Any) -> int:
            # Record what's in evicted list before cleanup runs
            evicted_during_put.extend(cache._evicted)
            return original_cleanup(**kwargs)
        cache_any = cast(Any, cache)
        cache_any._cleanup_evicted = tracking_cleanup

        cache.put("repo2", repo2)  # Evicts repo1

        assert len(cache) == 1
        assert "repo2" in cache
        # Verify repo1 was added to evicted list as (repo, timestamp) tuple
        evicted_repos = [r for r, _ts in evicted_during_put]
        assert repo1 in evicted_repos

    def test_duplicate_put_updates_lru_order(self):
        """Putting same key again should update LRU order without eviction."""
        cache = _LRURepoCache(maxsize=2)
        repo1 = MagicMock()
        repo2 = MagicMock()

        cache.put("repo1", repo1)
        cache.put("repo2", repo2)
        cache.put("repo1", repo1)  # Update LRU order, don't evict

        assert len(cache) == 2
        assert cache._order == ["repo2", "repo1"]


class TestLRURepoCacheCleanup:
    """Test cleanup behavior for evicted repos."""

    def test_cleanup_evicted_returns_count(self):
        """_cleanup_evicted should return count of closed repos after grace period."""
        cache = _LRURepoCache(maxsize=1)

        # Add a mock repo to evicted list with a timestamp far in the past
        mock_repo = MagicMock()
        cache._evicted.append((mock_repo, time.monotonic() - cache.EVICTION_GRACE_SECONDS - 10))

        closed = cache._cleanup_evicted()

        assert closed == 1
        mock_repo.close.assert_called_once()
        assert len(cache._evicted) == 0

    def test_cleanup_keeps_recently_evicted_repos(self):
        """Repos still within their grace period should not be closed."""
        cache = _LRURepoCache(maxsize=1)

        mock_repo = MagicMock()
        # Evicted just now -- well within the grace period
        cache._evicted.append((mock_repo, time.monotonic()))

        closed = cache._cleanup_evicted()

        assert closed == 0
        mock_repo.close.assert_not_called()
        evicted_repos = [r for r, _ts in cache._evicted]
        assert mock_repo in evicted_repos

    def test_force_cleanup_ignores_grace_period(self):
        """force=True should close repos regardless of grace period."""
        cache = _LRURepoCache(maxsize=1)

        mock_repo = MagicMock()
        # Evicted just now -- still in grace period
        cache._evicted.append((mock_repo, time.monotonic()))

        closed = cache._cleanup_evicted(force=True)

        assert closed == 1
        mock_repo.close.assert_called_once()
        assert len(cache._evicted) == 0

    def test_clear_closes_all_repos(self):
        """Clear should close all cached and evicted repos."""
        cache = _LRURepoCache(maxsize=4)
        repo1 = MagicMock()
        repo2 = MagicMock()
        evicted_repo = MagicMock()

        cache.put("repo1", repo1)
        cache.put("repo2", repo2)
        cache._evicted.append((evicted_repo, time.monotonic()))

        count = cache.clear()

        assert count == 3
        repo1.close.assert_called_once()
        repo2.close.assert_called_once()
        evicted_repo.close.assert_called_once()
        assert len(cache) == 0
        assert len(cache._evicted) == 0


class TestLRURepoCacheStats:
    """Test statistics and monitoring."""

    def test_evicted_count_property(self):
        """evicted_count should return number of evicted repos."""
        cache = _LRURepoCache(maxsize=1)
        assert cache.evicted_count == 0

        cache._evicted.append((MagicMock(), time.monotonic()))
        cache._evicted.append((MagicMock(), time.monotonic()))
        assert cache.evicted_count == 2

    def test_stats_property(self):
        """stats should return cache statistics."""
        cache = _LRURepoCache(maxsize=8)
        cache.put("repo1", MagicMock())
        cache._evicted.append((MagicMock(), time.monotonic()))

        stats = cache.stats
        assert stats == {"cached": 1, "evicted": 1, "maxsize": 8}


class TestLRURepoCacheOpportunisticCleanup:
    """Test opportunistic cleanup on get operations."""

    def test_cleanup_triggered_every_4th_get(self):
        """Cleanup should run every 4th get operation."""
        cache = _LRURepoCache(maxsize=4)
        repo = MagicMock()
        cache.put("repo", repo)

        # Track cleanup calls
        cleanup_calls = 0
        original_cleanup = cache._cleanup_evicted
        def tracking_cleanup():
            nonlocal cleanup_calls
            cleanup_calls += 1
            return original_cleanup()
        cache_any = cast(Any, cache)
        cache_any._cleanup_evicted = tracking_cleanup

        # 3 gets - no cleanup yet
        cache.get("repo")
        cache.get("repo")
        cache.get("repo")
        assert cleanup_calls == 0

        # 4th get triggers cleanup
        cache.get("repo")
        assert cleanup_calls == 1

        # Next 4 gets trigger another cleanup
        cache.get("repo")
        cache.get("repo")
        cache.get("repo")
        cache.get("repo")
        assert cleanup_calls == 2


class TestModuleLevelFunctions:
    """Test module-level cache functions."""

    def test_ensure_repo_cache_hit_refreshes_lru_order(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The production fast path must protect a recently used repo from eviction."""
        cache = _LRURepoCache(maxsize=2)
        monkeypatch.setattr(storage_module, "_REPO_CACHE", cache)
        first_root = tmp_path / "first"
        second_root = tmp_path / "second"
        third_root = tmp_path / "third"
        first_repo = MagicMock(spec=Repo)
        second_repo = MagicMock(spec=Repo)
        third_repo = MagicMock(spec=Repo)
        first_key = str(first_root.resolve())
        second_key = str(second_root.resolve())
        third_key = str(third_root.resolve())
        cache.put(first_key, first_repo)
        cache.put(second_key, second_repo)

        try:
            resolved = asyncio.run(_ensure_repo(first_root, get_settings()))
            cache.put(third_key, third_repo)

            assert resolved is first_repo
            assert first_key in cache
            assert second_key not in cache
            assert third_key in cache
            assert cache._order == [first_key, third_key]
        finally:
            cache.clear()

    def test_repo_single_flight_spans_concurrent_event_loops(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two loop threads must open and cache a repository exactly once."""
        repo_root = tmp_path / "shared-repo"
        repo_root.mkdir()
        initialized = Repo.init(repo_root)
        initialized.close()
        clear_repo_cache()

        real_repo = storage_module.Repo
        constructor_started = threading.Event()
        second_constructor_started = threading.Event()
        release_constructor = threading.Event()
        constructor_guard = threading.Lock()
        worker_barrier = threading.Barrier(2)
        constructor_calls = 0

        def slow_repo(path: str) -> Repo:
            nonlocal constructor_calls
            with constructor_guard:
                constructor_calls += 1
                if constructor_calls > 1:
                    second_constructor_started.set()
            constructor_started.set()
            if not release_constructor.wait(timeout=5):
                raise TimeoutError("test did not release the repository constructor")
            return real_repo(path)

        monkeypatch.setattr(storage_module, "Repo", slow_repo)
        settings = get_settings()

        def open_on_private_loop() -> Repo:
            worker_barrier.wait(timeout=5)
            return asyncio.run(_ensure_repo(repo_root, settings))

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                try:
                    results = [executor.submit(open_on_private_loop) for _ in range(2)]
                    assert constructor_started.wait(timeout=5)
                    assert not second_constructor_started.wait(timeout=0.25)
                    release_constructor.set()
                    first, second = (future.result(timeout=10) for future in results)
                finally:
                    release_constructor.set()

            assert constructor_calls == 1
            assert first is second
            assert get_repo_cache_stats()["cached"] == 1
        finally:
            release_constructor.set()
            clear_repo_cache()

    @pytest.mark.asyncio
    async def test_cancelled_repo_waiter_consumes_late_shared_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cancelled waiter must not leak a later shared-flight exception."""
        repo_root = tmp_path / "cancelled-waiter"
        flight: concurrent.futures.Future[Repo] = concurrent.futures.Future()
        both_waiting = asyncio.Event()
        waiter_count = 0

        def shared_flight(
            root: Path,
            settings: Any,
            cache_key: str,
        ) -> concurrent.futures.Future[Repo]:
            nonlocal waiter_count
            del root, settings, cache_key
            waiter_count += 1
            if waiter_count == 2:
                both_waiting.set()
            return flight

        monkeypatch.setattr(
            storage_module,
            "_get_or_start_repo_single_flight",
            shared_flight,
        )
        loop = asyncio.get_running_loop()
        previous_exception_handler = loop.get_exception_handler()
        loop_errors: list[dict[str, Any]] = []

        def capture_loop_error(
            _loop: asyncio.AbstractEventLoop,
            context: dict[str, Any],
        ) -> None:
            loop_errors.append(context)

        loop.set_exception_handler(capture_loop_error)
        settings = get_settings()
        first = asyncio.create_task(_ensure_repo(repo_root, settings))
        second = asyncio.create_task(_ensure_repo(repo_root, settings))
        try:
            await asyncio.wait_for(both_waiting.wait(), timeout=5.0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            shared_error = RuntimeError(
                f"Repository initialization was cancelled for {repo_root.resolve()}"
            )
            flight.set_exception(shared_error)
            with pytest.raises(RuntimeError, match="Repository initialization was cancelled") as exc:
                await second
            assert exc.value is shared_error
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert loop_errors == []
        finally:
            for waiter in (first, second):
                if not waiter.done():
                    waiter.cancel()
            if not flight.done():
                flight.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
            await asyncio.sleep(0)
            loop.set_exception_handler(previous_exception_handler)

    def test_cancelled_repo_waiter_consumes_done_failure_before_loop_close(self) -> None:
        """A same-tick failure must be observed before its loop can close."""
        flight: concurrent.futures.Future[Repo] = concurrent.futures.Future()
        shared_error = RuntimeError("late shared repository failure")
        loop = asyncio.new_event_loop()
        loop_errors: list[dict[str, Any]] = []

        def capture_loop_error(
            _loop: asyncio.AbstractEventLoop,
            context: dict[str, Any],
        ) -> None:
            loop_errors.append(context)

        async def cancel_after_shared_failure(
            shared_flight: concurrent.futures.Future[Repo],
        ) -> None:
            current = asyncio.current_task()
            assert current is not None
            loop.call_soon(shared_flight.set_exception, shared_error)
            loop.call_soon(current.cancel)
            try:
                await storage_module._await_repo_single_flight(shared_flight)
            except asyncio.CancelledError:
                loop.stop()
                raise

        loop.set_exception_handler(capture_loop_error)
        waiter = loop.create_task(cancel_after_shared_failure(flight))
        try:
            with pytest.raises(asyncio.CancelledError):
                loop.run_until_complete(waiter)
        finally:
            loop.close()
        del waiter
        del flight
        gc.collect()
        assert loop_errors == []

    def test_global_capacity_limiter_spans_event_loops(self) -> None:
        """Capacity one must serialize holders running on different loops."""
        limiter = _GlobalAsyncCapacityLimiter(1)
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        async def hold_first() -> None:
            async with limiter.slot():
                first_entered.set()
                await asyncio.to_thread(release_first.wait)

        async def enter_second() -> None:
            async with limiter.slot():
                second_entered.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            try:
                first = executor.submit(asyncio.run, hold_first())
                assert first_entered.wait(timeout=5)
                second = executor.submit(asyncio.run, enter_second())
                assert not second_entered.wait(timeout=0.1)
                release_first.set()
                first.result(timeout=5)
                second.result(timeout=5)
            finally:
                release_first.set()

        assert second_entered.is_set()

    def test_global_capacity_limiter_cancelled_waiter_does_not_leak(self) -> None:
        """Cancelling a blocked acquire must leave the full capacity available."""

        async def exercise() -> None:
            limiter = _GlobalAsyncCapacityLimiter(1)
            await limiter.acquire()
            blocked = asyncio.create_task(limiter.acquire())
            await asyncio.sleep(0.05)
            blocked.cancel()

            with pytest.raises(asyncio.CancelledError):
                await blocked

            # The original holder still owns the sole permit. Cancellation of
            # the queued waiter must not manufacture another one.
            assert limiter._available == 0
            limiter.release()
            assert limiter._available == 1
            await asyncio.wait_for(limiter.acquire(), timeout=1)
            limiter.release()

        asyncio.run(exercise())

    def test_global_capacity_limiter_cancellation_after_grant_returns_permit(self) -> None:
        """A granted permit must be returned if cancellation wins before resume."""

        async def exercise() -> None:
            limiter = _GlobalAsyncCapacityLimiter(1)
            await limiter.acquire()
            blocked = asyncio.create_task(limiter.acquire())
            await asyncio.sleep(0)

            limiter.release()
            blocked.cancel()

            with pytest.raises(asyncio.CancelledError):
                await blocked

            await asyncio.wait_for(limiter.acquire(), timeout=1)
            limiter.release()

        asyncio.run(exercise())

    def test_global_capacity_limiter_many_waiters_and_cancellations(self) -> None:
        """Cancelled entries must not stall a larger cross-loop-style queue."""

        async def exercise() -> None:
            limiter = _GlobalAsyncCapacityLimiter(2)
            await limiter.acquire()
            await limiter.acquire()
            acquired: list[int] = []

            async def contender(index: int) -> None:
                await limiter.acquire()
                try:
                    acquired.append(index)
                    await asyncio.sleep(0)
                finally:
                    limiter.release()

            tasks = [asyncio.create_task(contender(index)) for index in range(12)]
            cancelled = {1, 3, 6, 10}
            await asyncio.sleep(0)
            for index in cancelled:
                tasks[index].cancel()
            await asyncio.sleep(0)

            limiter.release()
            limiter.release()
            results = await asyncio.gather(*tasks, return_exceptions=True)

            assert set(acquired) == set(range(12)) - cancelled
            assert all(isinstance(results[index], asyncio.CancelledError) for index in cancelled)

            # Every permit is back after the queue drains.
            await asyncio.wait_for(limiter.acquire(), timeout=1)
            await asyncio.wait_for(limiter.acquire(), timeout=1)
            blocked = asyncio.create_task(limiter.acquire())
            await asyncio.sleep(0)
            assert not blocked.done()
            blocked.cancel()
            limiter.release()
            limiter.release()
            with pytest.raises(asyncio.CancelledError):
                await blocked

        asyncio.run(exercise())

    def test_clear_repo_cache_returns_count(self):
        """clear_repo_cache should return count of closed repos."""
        # This uses the global cache - just verify it doesn't crash
        count = clear_repo_cache()
        assert isinstance(count, int)
        assert count >= 0

    def test_get_repo_cache_stats_returns_dict(self):
        """get_repo_cache_stats should return statistics dict."""
        stats = get_repo_cache_stats()
        assert isinstance(stats, dict)
        assert "cached" in stats
        assert "evicted" in stats
        assert "maxsize" in stats
        assert stats["maxsize"] == 16  # Default is now 16


class TestLRURepoCacheWarningLogging:
    """Test warning logs when evicted list grows large."""

    def test_warning_logged_when_evicted_exceeds_maxsize(self):
        """Warning should be logged when evicted list exceeds maxsize."""
        cache = _LRURepoCache(maxsize=2)

        # Add many repos to evicted list, all recently evicted (within grace period)
        now = time.monotonic()
        for _ in range(5):
            mock_repo = MagicMock()
            cache._evicted.append((mock_repo, now))

        # Repos are within grace period so they won't be cleaned up,
        # and the warning should fire because len(still_pending) > maxsize
        with patch("mcp_agent_mail.storage._logger") as mock_logger:
            cache._cleanup_evicted()

            # Warning should have been logged
            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args
            assert call_args[0][0] == "repo_cache.evicted_backlog"
