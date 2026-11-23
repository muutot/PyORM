"""Bounded, key-partitioned workers with explicit completion and exception reporting."""

import hashlib
import threading
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from typing import Any

from bson import BSON

from .exceptions import ConnectionClosedError, QueueFullError


class OrderedExecutor:
    def __init__(self, workers: int = 4, max_pending: int = 1024) -> None:
        if type(workers) is not int or workers < 1:
            raise ValueError("workers must be a positive integer")
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        self._lanes = [
            ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"pyorm-{i}")
            for i in range(workers)
        ]
        self._slots = threading.BoundedSemaphore(max_pending)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._local = threading.local()
        self._closed = False
        self._next_lane = 0
        self._futures: set[Future[Any]] = set()

    @property
    def in_worker(self) -> bool:
        return getattr(self._local, "in_worker", False)

    def _check_worker(self) -> None:
        if self.in_worker:
            raise RuntimeError("Workers must not submit, flush, or close their own executor")

    def submit[R](self, func: Callable[..., R], *args, key=None, **kwargs) -> Future[R]:
        self._check_worker()
        with self._lock:
            if self._closed:
                raise ConnectionClosedError("Executor is closed")
            if key is None:
                lane = self._next_lane % len(self._lanes)
                self._next_lane += 1
            else:
                digest = hashlib.sha256(BSON.encode({"key": key})).digest()
                lane = int.from_bytes(digest[:8], "big") % len(self._lanes)
            if not self._slots.acquire(blocking=False):
                raise QueueFullError("Pending-operation limit reached; wait for submitted futures")

            def run() -> R:
                self._local.in_worker = True
                # Keep the marker for Future callbacks too: these also run on the worker.
                return func(*args, **kwargs)

            try:
                future = self._lanes[lane].submit(run)
            except BaseException:
                self._slots.release()
                raise
            self._futures.add(future)

            def done(completed: Future[R]) -> None:
                self._slots.release()
                with self._lock:
                    # Keep failed jobs until flush() so fire-and-forget errors are visible.
                    if completed.cancelled() or completed.exception() is None:
                        self._futures.discard(completed)
                    self._condition.notify_all()

            future.add_done_callback(done)
            return future

    def flush(self, timeout: float | None = None) -> None:
        """Wait for the submission snapshot; raise the first retained task failure."""
        self._check_worker()
        with self._condition:
            futures = set(self._futures)
            # Future.cancel() may not notify concurrent.futures.wait() when shutdown
            # removes the queued work item. Our condition also observes cancelled jobs.
            if not self._condition.wait_for(lambda: all(f.done() for f in futures), timeout):
                unfinished = sum(not f.done() for f in futures)
                raise TimeoutError(f"{unfinished} MongoDB operations are still running")
        error = None
        for future in futures:
            try:
                future.result()
            except CancelledError:
                pass
            except BaseException as exc:
                if error is None:
                    error = exc
        with self._lock:
            self._futures.difference_update(futures)
        if error is not None:
            raise error

    def shutdown(self, *, cancel_pending: bool = False) -> None:
        self._check_worker()
        with self._lock:
            self._closed = True
        for lane in self._lanes:
            lane.shutdown(wait=False, cancel_futures=cancel_pending)
        for lane in self._lanes:
            lane.shutdown(wait=True)
        self.flush()
