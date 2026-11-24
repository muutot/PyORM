"""Authoritative identity cache with generation-aware automatic incremental persistence."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from itertools import islice
from types import TracebackType
from typing import Any, Literal, Self

from bson import encode as encode_bson
from pymongo import timeout as mongo_timeout
from pymongo.errors import ConnectionFailure

from .connection import MongoORM
from .exceptions import (
    ConcurrentModificationError,
    ConnectionClosedError,
    DocumentInactiveError,
    PersistenceError,
)
from .model import VERSION_FIELD, WRITE_TOKEN_FIELD, Document
from .serialization import collect_set_paths, copy_bson, diff_documents, validate_bson
from .tracking import track
from .types import BSONDocument, CacheKey, MongoUpdate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChangeHook:
    document: Document[Any]
    field: str

    def _owner(self) -> Manager:
        manager = self.document._manager
        if manager is None:
            raise DocumentInactiveError(
                f"Document {self.document.id!r} was {self.document._retired}"
            )
        return manager

    def before(self) -> None:
        self._owner()._check_mutable(self.document)

    def __call__(self) -> None:
        doc = self.document
        doc._change_seq += 1
        doc._dirty_fields[self.field] = doc._change_seq
        self._owner()._mark(doc)


@dataclass
class Retirement:
    flush: bool
    completed: Future[None]


@contextmanager
def _retirement_lock(doc: Document[Any], deadline: float | None) -> Iterator[None]:
    if deadline is None:
        acquired = doc._lock.acquire()
    else:
        acquired = doc._lock.acquire(timeout=max(0, deadline - time.monotonic()))
    if not acquired:
        raise TimeoutError("Document is still executing a synchronized operation")
    try:
        yield
    finally:
        doc._lock.release()


@dataclass
class WritePacket:
    values: BSONDocument
    removed: tuple[str, ...]
    generations: dict[str, int]
    revision: int
    version: int
    token: str
    update: MongoUpdate
    new: bool


class Manager:
    """Business code loads once and assigns attributes; this owner handles persistence."""

    def __init__(
        self,
        uri: str = "mongodb://localhost:27017",
        database: str = "pyorm",
        *,
        flush_interval: float = 1.0,
        batch_size: int = 100,
        workers: int = 4,
        max_pending: int = 1024,
        retry_interval: float = 1.0,
        on_error: Callable[[Document[Any], Exception], None] | None = None,
        **connection_options,
    ) -> None:
        if flush_interval <= 0 or retry_interval <= 0:
            raise ValueError("flush_interval and retry_interval must be positive")
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self._orm = MongoORM(
            uri, database, workers=workers, max_pending=max_pending, **connection_options
        )
        self.flush_interval = flush_interval
        self.batch_size = batch_size
        self.retry_interval = retry_interval
        self._capacity = max_pending
        self._on_error = on_error
        self._condition = threading.Condition(threading.RLock())
        self._close_lock = threading.Lock()
        self._cache: dict[CacheKey, Document[Any]] = {}
        self._loads: dict[CacheKey, Future[Document[Any]]] = {}
        self._retiring: dict[CacheKey, Retirement] = {}
        self._models: dict[str, type[Document[Any]]] = {}
        self._pending: dict[CacheKey, float] = {}
        self._inflight: set[CacheKey] = set()
        self._packets: dict[CacheKey, WritePacket] = {}
        self._retry_at: dict[CacheKey, float] = {}
        self._errors: dict[CacheKey, Exception] = {}
        self._blocked: set[CacheKey] = set()
        self._closing = False
        self._closed = False
        self._stop = False
        self._forcing = 0
        self._scheduled_batch_size = batch_size
        self._scheduled_flush_interval = flush_interval
        self._thread = threading.Thread(target=self._dispatch, name="pyorm-manager", daemon=True)
        self._thread.start()

    def _check_open(self) -> None:
        with self._condition:
            if self._closing or self._closed:
                raise ConnectionClosedError("Manager is closing or closed")

    def _key(self, model: type[Document[Any]], id) -> CacheKey:
        return self._orm.collection_name(model), encode_bson({"_id": id})

    def _check_mutable(self, doc: Document[Any]) -> None:
        with self._condition:
            if self._closing or self._closed:
                raise ConnectionClosedError("Manager is closing or closed")
            if doc._cache_key in self._retiring:
                raise DocumentInactiveError(f"Document {doc.id!r} is being released or deleted")

    def get[T: Document[Any]](self, model: type[T], id) -> T:
        """Return the one cached instance; a missing document is initialized in memory."""
        key = self._key(model, id)
        while True:
            with self._condition:
                if self._closing or self._closed:
                    raise ConnectionClosedError("Manager is closing or closed")
                registered = self._models.setdefault(key[0], model)
                if registered is not model:
                    raise TypeError("One collection must use one model type within a Manager")
                retiring = self._retiring.get(key)
                if retiring is not None:
                    cached = self._cache.get(key)
                    if cached is not None and cached._lock._is_owned():  # pyright: ignore[reportAttributeAccessIssue]
                        raise DocumentInactiveError(
                            "Cannot wait for document retirement while holding its lock"
                        )
                    if threading.current_thread() is self._thread or self._orm._executor.in_worker:
                        raise RuntimeError("A Manager worker cannot wait for document retirement")
                if retiring is None:
                    if key in self._cache:
                        # Collection registration guarantees the cached model type.
                        return self._cache[key]  # pyright: ignore[reportReturnType]
                    future = self._loads.get(key)
                    owner = future is None
                    if future is None:
                        future = self._loads[key] = Future()
                    break
            # Wait outside scheduling locks, then join the ordinary shared load.
            retiring.completed.result()
        if not owner:
            # The same registered model type also owns the shared loading Future.
            return future.result()  # pyright: ignore[reportReturnType]
        try:
            raw = self._orm._run(self._orm._collection(model).find_one, {"_id": id})
            doc = model(id=id) if raw is None else model.from_mongo(raw, orm=self._orm)
            self._bind(doc, key)
            with self._condition:
                self._cache[key] = doc
                if not doc._persisted or doc._dirty_fields:
                    self._pending[key] = time.monotonic()
                self._loads.pop(key)
                future.set_result(doc)
                self._condition.notify_all()
            return doc
        except BaseException as exc:
            with self._condition:
                self._loads.pop(key, None)
                future.set_exception(exc)
                self._condition.notify_all()
            raise

    def _bind(self, doc: Document[Any], key: CacheKey) -> None:
        if doc._manager is not None:
            raise ValueError("Document is already managed")
        doc._manager = self
        doc._orm = self._orm
        doc._cache_key = key
        doc._change_seq = 0
        doc._acked_seq = 0
        doc._dirty_fields = {}
        doc._hooks = {name: ChangeHook(doc, name) for name in doc._fields}
        doc._on_change = ChangeHook(doc, "")
        for name, field in doc._fields.items():
            if name in doc._values:
                doc._values[name] = track(doc._values[name], doc._hooks[name], doc._lock)
                if not doc._persisted or field.db_field not in doc._snapshot:
                    doc._dirty_fields[name] = 0

    def release[ID](
        self,
        document: Document[ID],
        *,
        discard: bool = False,
        timeout: float | None = 10.0,
    ) -> bool:
        """Flush and retire one cached instance; discard=True explicitly cancels pending edits."""
        return self._retire(document, delete=False, discard=discard, timeout=timeout)

    def delete[ID](self, document: Document[ID], *, timeout: float | None = 10.0) -> bool:
        """Version-check deletion, then retire the instance and cancel its unsent edits."""
        return self._retire(document, delete=True, discard=True, timeout=timeout)

    def _retire(
        self,
        doc: Document[Any],
        *,
        delete: bool,
        discard: bool,
        timeout: float | None,
    ) -> bool:
        state = "deleted" if delete else "released"
        if doc._retired == state:
            return False
        doc._check_active()
        if threading.current_thread() is self._thread or self._orm._executor.in_worker:
            raise RuntimeError("A Manager worker cannot retire documents")
        if doc._lock._is_owned():  # pyright: ignore[reportAttributeAccessIssue]
            raise RuntimeError("Release/delete must be called outside a held document lock")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be nonnegative or None")
        deadline = None if timeout is None else time.monotonic() + timeout
        retirement = Retirement(flush=not discard, completed=Future())
        with _retirement_lock(doc, deadline), self._condition:
            if doc._retired == state:
                return False
            doc._check_active()
            self._check_open()
            if doc._manager is not self or self._cache.get(doc._cache_key) is not doc:
                raise ValueError("Document is not an active cached instance of this Manager")
            key = doc._cache_key
            if key in self._retiring:
                raise DocumentInactiveError(f"Document {doc.id!r} is already being retired")
            self._retiring[key] = retirement
            self._condition.notify_all()
        try:
            with self._condition:
                # An uncertain existing packet must finish even for discard/delete.
                # New captures are suppressed in these modes; release drains all edits.
                while (
                    key in self._inflight
                    or key in self._packets
                    or (retirement.flush and key in self._pending)
                ):
                    if key in self._blocked:
                        raise PersistenceError(
                            f"Cached document {key[0]} could not be persisted before release"
                        ) from self._errors[key]
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError("Document still has unacknowledged cached changes")
                    self._condition.wait(remaining)
            deleted = False
            if delete:
                with _retirement_lock(doc, deadline):
                    persisted = doc._persisted
                    query = doc._version_filter()
                    # The receipt also distinguishes a recreated id at the same version.
                    query[WRITE_TOKEN_FIELD] = doc._write_token
                if persisted:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError("Document deletion did not start before timeout")
                    collection = self._orm._collection(type(doc))
                    with mongo_timeout(remaining) if remaining is not None else nullcontext():
                        result = self._orm._run(collection.delete_one, query)
                        deleted = result.deleted_count == 1
                        if (
                            not deleted
                            and self._orm._run(collection.find_one, {"_id": doc.id}, {"_id": 1})
                            is not None
                        ):
                            raise ConcurrentModificationError(
                                f"Document {doc.id!r} changed before deletion; cache retained"
                            )
            # Once deletion is acknowledged, completing local retirement cannot
            # be cancelled by a late timeout and reactivate the deleted document.
            with _retirement_lock(doc, None if delete else deadline), self._condition:
                self._cache.pop(key)
                self._pending.pop(key, None)
                self._packets.pop(key, None)
                self._retry_at.pop(key, None)
                self._errors.pop(key, None)
                self._blocked.discard(key)
                doc._retired = state
                doc._deleted = delete
                doc._dirty_fields.clear()
                doc._hooks.clear()
                doc._snapshot = {}
                doc._manager = None
                doc._orm = None
                # Collection/model registration is a Manager-lifetime contract.
            return deleted if delete else True
        finally:
            with self._condition:
                self._retiring.pop(key)
                retirement.completed.set_result(None)
                self._condition.notify_all()

    def _mark(self, doc: Document[Any]) -> None:
        with self._condition:
            key = doc._cache_key
            new = key not in self._pending
            wake = (
                new
                or self.batch_size != self._scheduled_batch_size
                or self.flush_interval != self._scheduled_flush_interval
            )
            if new:
                self._pending[key] = time.monotonic()
            if (self._blocked or self._errors) and key not in self._inflight:
                wake = wake or key in self._blocked
                self._blocked.discard(key)
                self._errors.pop(key, None)
            # Repeated mutations retain the first deadline and queue position.
            # Completion wakes the dispatcher for changes made during an active write.
            if wake:
                self._condition.notify_all()

    def _capture(self, doc: Document[Any]) -> WritePacket:
        with doc._lock:
            new = not doc._persisted
            names = list(doc._fields) if new else list(doc._dirty_fields)
            generations = dict(doc._dirty_fields)
            values: BSONDocument = {}
            before: BSONDocument = {}
            removed: list[str] = []
            set_paths: set[str] = set()
            for name in names:
                field = doc._fields[name]
                if field.db_field in doc._snapshot:
                    before[field.db_field] = doc._snapshot[field.db_field]
                if name in doc._values:
                    set_paths.update(collect_set_paths(doc._values[name], field.db_field))
                    values[field.db_field] = field.to_mongo(doc._values[name])
                else:
                    if field.required:
                        field.validate(None)
                    removed.append(field.db_field)
            validate_bson(values)
            # One detached tree owns both packet values and operator payloads.
            # Even mutable BSON leaves returned by custom fields cannot follow cache edits.
            values = copy_bson(values)
            update = diff_documents(before, values, incremental_arrays=True, set_paths=set_paths)
            return WritePacket(
                values,
                tuple(removed),
                generations,
                doc._change_seq,
                doc._version + 1,
                uuid.uuid4().hex,
                update,
                new,
            )

    def _persist(self, doc: Document[Any], packet: WritePacket) -> Exception | None:
        """Retries reuse an idempotent packet; no database read can replace the cache."""
        try:
            if not packet.new and not packet.update:
                return None
            update = copy_bson(packet.update)
            update.setdefault("$set", {}).update(
                {
                    VERSION_FIELD: packet.version,
                    WRITE_TOKEN_FIELD: packet.token,
                }
            )
            if packet.new:
                query = {
                    "_id": doc.id,
                    VERSION_FIELD: packet.version,
                    WRITE_TOKEN_FIELD: packet.token,
                }
            else:
                expected = doc._version_filter()
                expected.pop("_id")
                receipt = {VERSION_FIELD: packet.version, WRITE_TOKEN_FIELD: packet.token}
                # $push is not idempotent: never replay it against an already
                # acknowledged target version. Check the receipt separately.
                if "$push" in packet.update:
                    query = {"_id": doc.id, **expected}
                else:
                    query = {"_id": doc.id, "$or": [expected, receipt]}
            collection = self._orm._collection(type(doc))
            result = self._orm._run(
                collection.update_one,
                query,
                update,
                upsert=packet.new,
            )
            if not packet.new and "$push" in packet.update and result.matched_count == 0:
                # A lost acknowledgement may mean the first attempt succeeded.
                # This metadata-only write confirms it without repeating additions
                # or reading/replacing any business data in the cache.
                result = self._orm._run(
                    collection.update_one,
                    {"_id": doc.id, **receipt},
                    {"$set": receipt},
                )
            if result.matched_count == 0 and result.upserted_id is None:
                raise ConcurrentModificationError(
                    f"{type(doc).__name__} {doc.id!r} changed or was deleted externally; "
                    "cache retained"
                )
            return None
        except Exception as exc:
            return exc

    def _finish(
        self,
        key: CacheKey,
        doc: Document[Any],
        packet,
        error: Exception | None,
        attempt_revision: int,
    ) -> None:
        # Lock order is always document -> manager, matching an attribute assignment.
        with doc._lock, self._condition:
            self._inflight.discard(key)
            if error is None:
                # A successful attempt always supplies a WritePacket; capture failures
                # supply None together with an error. Keep this boundary runtime-free.
                if packet.new or packet.update:
                    doc._version = packet.version
                    doc._persisted = True
                    doc._write_token = packet.token
                    # The completed packet is retired here. Transfer its detached
                    # values to the acknowledged snapshot without another full-tree copy.
                    doc._snapshot.update(packet.values)
                    for name in packet.removed:
                        doc._snapshot.pop(name, None)
                doc._acked_seq = max(doc._acked_seq, packet.revision)
                for name, generation in packet.generations.items():
                    if doc._dirty_fields.get(name) == generation:
                        doc._dirty_fields.pop(name)
                self._packets.pop(key, None)
                self._retry_at.pop(key, None)
                self._errors.pop(key, None)
                if doc._dirty_fields or not doc._persisted:
                    self._pending.setdefault(key, time.monotonic())
                else:
                    self._pending.pop(key, None)
            else:
                self._errors[key] = error
                if isinstance(error, ConnectionFailure):
                    self._retry_at[key] = time.monotonic() + self.retry_interval
                else:
                    self._packets.pop(key, None)
                    if doc._change_seq > attempt_revision and not isinstance(
                        error,
                        ConcurrentModificationError,
                    ):
                        self._blocked.discard(key)
                    else:
                        self._blocked.add(key)
            self._condition.notify_all()
        if error is not None:
            if self._on_error is not None:
                try:
                    self._on_error(doc, error)
                except Exception:
                    logger.exception("Manager on_error callback failed")
            else:
                logger.warning(
                    "Persistence failed for %s %r: %s", type(doc).__name__, doc.id, error
                )

    def _dispatch(self) -> None:
        while True:
            with self._condition:
                if self._stop:
                    return
                self._scheduled_batch_size = self.batch_size
                self._scheduled_flush_interval = self.flush_interval
                slots = self._capacity - len(self._inflight)
                if slots <= 0:
                    self._condition.wait()
                    continue
                now = time.monotonic()
                candidates = (
                    key
                    for key in self._pending
                    if key not in self._inflight
                    and key not in self._blocked
                    and (
                        key not in self._retiring
                        or self._retiring[key].flush
                        or key in self._packets
                    )
                )
                eligible = (key for key in candidates if self._retry_at.get(key, 0) <= now)
                if self._forcing:
                    # A drain needs only enough work to fill the available capacity.
                    ready = list(islice(eligible, min(slots, len(self._pending))))
                else:
                    # Stop counting once the batch threshold is known to be met.
                    batch = list(islice(eligible, min(self.batch_size, len(self._pending))))
                    if len(batch) == self.batch_size:
                        ready = batch[:slots]
                        if len(ready) < slots:
                            ready.extend(
                                islice(eligible, min(slots - len(ready), len(self._pending)))
                            )
                    else:
                        ready = [
                            key
                            for key in batch
                            if key in self._retry_at
                            or key in self._retiring
                            or self._pending[key] + self.flush_interval <= now
                        ][:slots]
                if not ready:
                    deadlines = [
                        self._retry_at.get(
                            key,
                            now
                            if self._forcing or key in self._retiring
                            else self._pending[key] + self.flush_interval,
                        )
                        for key in self._pending
                        if key not in self._inflight
                        and key not in self._blocked
                        and (
                            key not in self._retiring
                            or self._retiring[key].flush
                            or key in self._packets
                        )
                    ]
                    timeout = max(0.001, min(deadlines) - now) if deadlines and slots else None
                    self._condition.wait(timeout)
                    continue
                self._inflight.update(ready)
            for key in ready:
                doc = self._cache[key]
                with doc._lock:
                    attempt_revision = doc._change_seq
                try:
                    packet = self._packets.get(key)
                    if packet is None:
                        packet = self._capture(doc)
                        self._packets[key] = packet
                    future = self._orm.submit(self._persist, doc, packet, key=(key[0], doc.id))

                    def completed(
                        job: Future[Exception | None],
                        key: CacheKey = key,
                        doc: Document[Any] = doc,
                        packet: WritePacket = packet,
                        attempt_revision: int = attempt_revision,
                    ) -> None:
                        try:
                            error = job.result()
                        except Exception as exc:
                            error = exc
                        self._finish(key, doc, packet, error, attempt_revision)

                    future.add_done_callback(completed)
                except Exception as exc:
                    self._finish(key, doc, None, exc, attempt_revision)

    @property
    def stats(self) -> dict[str, int]:
        with self._condition:
            return {
                "cached": len(self._cache),
                "pending": len(self._pending),
                "inflight": len(self._inflight),
                "errors": len(self._errors),
                "loading": len(self._loads),
                "retiring": len(self._retiring),
            }

    @property
    def errors(self) -> dict[CacheKey, Exception]:
        with self._condition:
            return dict(self._errors)

    def wait_idle(self, timeout: float | None = None) -> None:
        """Lifecycle/testing barrier; normal business code never needs to call it."""
        if threading.current_thread() is self._thread or self._orm._executor.in_worker:
            raise RuntimeError("A Manager worker cannot wait for itself")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            self._forcing += 1
            self._condition.notify_all()
            try:
                while self._pending or self._inflight or self._loads or self._retiring:
                    blocked = next(
                        (key for key in self._blocked if key not in self._retiring), None
                    )
                    if blocked is not None:
                        key = blocked
                        raise PersistenceError(
                            f"Cached document {key[0]} could not be persisted"
                        ) from self._errors[key]
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError("Manager still has unacknowledged cached changes")
                    self._condition.wait(remaining)
            finally:
                self._forcing -= 1

    def close(self, timeout: float | None = 10.0) -> None:
        if threading.current_thread() is self._thread or self._orm._executor.in_worker:
            raise RuntimeError("A Manager worker cannot close itself")
        with self._close_lock:
            with self._condition:
                if self._closed:
                    return
                self._closing = True
                self._condition.notify_all()
                if not self._condition.wait_for(
                    lambda: not self._loads and not self._retiring, timeout
                ):
                    self._closing = False
                    self._condition.notify_all()
                    raise TimeoutError("Manager is still loading or retiring documents")
                documents = list(self._cache.values())
            # Set closing first, then let attribute assignments already holding a lock finish.
            for doc in documents:
                with doc._lock:
                    pass
            try:
                self.wait_idle(timeout)
            except BaseException:
                with self._condition:
                    self._closing = False
                    self._condition.notify_all()
                raise
            with self._condition:
                self._stop = True
                self._condition.notify_all()
            self._thread.join()
            self._orm.close()
            with self._condition:
                self._closed = True

    def __enter__(self) -> Self:
        self._check_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        self.close()
        return False
