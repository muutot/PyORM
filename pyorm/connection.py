"""A single thread-safe MongoClient pool and a drainable operation lifecycle."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Self

from pymongo import MongoClient
from pymongo.collection import Collection

from .exceptions import ConnectionClosedError
from .executor import OrderedExecutor
from .model import Document
from .types import BSONDocument

if TYPE_CHECKING:
    from .query import Repository


class MongoORM:
    def __init__(
        self,
        uri: str = "mongodb://localhost:27017",
        database: str = "pyorm",
        *,
        workers: int = 4,
        max_pending: int = 1024,
        max_pool_size: int = 100,
        min_pool_size: int = 0,
        server_selection_timeout_ms: int = 5000,
        connect_timeout_ms: int = 5000,
        socket_timeout_ms: int = 10000,
        wait_queue_timeout_ms: int = 5000,
        client: MongoClient[BSONDocument] | None = None,
        owns_client: bool | None = None,
        **client_options,
    ) -> None:
        if not isinstance(database, str) or not database:
            raise ValueError("database must be a nonempty name")
        if type(max_pool_size) is not int or max_pool_size < 1:
            raise ValueError("max_pool_size must be positive")
        if type(min_pool_size) is not int or not 0 <= min_pool_size <= max_pool_size:
            raise ValueError("min_pool_size must be between 0 and max_pool_size")
        self._executor = OrderedExecutor(workers, max_pending)
        self._condition = threading.Condition(threading.RLock())
        self._close_lock = threading.Lock()
        self._local = threading.local()
        self._closing = False
        self._closed = False
        self._active = 0
        self._owns_client = (client is None) if owns_client is None else owns_client
        self._client: MongoClient[BSONDocument] = (
            client
            if client is not None
            else MongoClient(
                uri,
                maxPoolSize=max_pool_size,
                minPoolSize=min_pool_size,
                serverSelectionTimeoutMS=server_selection_timeout_ms,
                connectTimeoutMS=connect_timeout_ms,
                socketTimeoutMS=socket_timeout_ms,
                waitQueueTimeoutMS=wait_queue_timeout_ms,
                retryReads=True,
                retryWrites=True,
                **client_options,
            )
        )
        self._database = self._client[database]
        if not self._database.write_concern.acknowledged:
            if self._owns_client:
                self._client.close()
            raise ValueError("ORM saves require acknowledged writes (w must not be 0)")

    @property
    def client(self) -> MongoClient[BSONDocument]:
        """The shared PyMongo client, for advanced driver access."""
        return self._client

    @property
    def is_closed(self) -> bool:
        with self._condition:
            return self._closed

    @staticmethod
    def collection_name(model: type[Document[Any]]) -> str:
        if not isinstance(model, type) or not issubclass(model, Document):
            raise TypeError("model must be a Document subclass")
        name = model.__collection__ or getattr(model, "_collection", None)
        name = getattr(name, "value", name) or model.__name__.lower()
        if not isinstance(name, str) or not name or "\0" in name or "$" in name:
            raise ValueError("Invalid collection name")
        return name

    def _collection(self, model: type[Document[Any]]) -> Collection[BSONDocument]:
        return self._database[self.collection_name(model)]

    @contextmanager
    def _operation(self) -> Iterator[None]:
        depth = getattr(self._local, "depth", 0)
        with self._condition:
            if self._closed or (self._closing and not depth and not self._executor.in_worker):
                raise ConnectionClosedError("MongoORM is closing or closed")
            self._active += 1
            self._local.depth = depth + 1
        try:
            yield
        finally:
            with self._condition:
                self._local.depth = depth
                self._active -= 1
                self._condition.notify_all()

    def _run[**P, R](self, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        with self._operation():
            return func(*args, **kwargs)

    def ping(self) -> BSONDocument:
        return self._run(self._client.admin.command, "ping")

    def repository[T: Document[Any]](self, model: type[T]) -> Repository[T]:
        from .query import Repository

        self.collection_name(model)
        return Repository(self, model)

    def __getitem__[T: Document[Any]](self, model: type[T]) -> Repository[T]:
        return self.repository(model)

    def submit[R](self, func: Callable[..., R], *args, key=None, **kwargs) -> Future[R]:
        with self._condition:
            if self._closed or self._closing:
                raise ConnectionClosedError("MongoORM is closing or closed")
            return self._executor.submit(self._run, func, *args, key=key, **kwargs)

    def flush(self, timeout: float | None = None) -> None:
        return self._executor.flush(timeout)

    def close(self, *, cancel_pending: bool = False) -> None:
        if self._executor.in_worker or getattr(self._local, "depth", 0):
            raise RuntimeError("An active operation cannot close its own MongoORM")
        with self._close_lock:
            with self._condition:
                if self._closed:
                    return
                self._closing = True
                self._condition.notify_all()
            try:
                self._executor.shutdown(cancel_pending=cancel_pending)
            finally:
                with self._condition:
                    self._condition.wait_for(lambda: self._active == 0)
                    try:
                        if self._owns_client:
                            self._client.close()
                    finally:
                        self._closed = True
                        self._condition.notify_all()

    def __enter__(self) -> Self:
        with self._condition:
            if self._closing or self._closed:
                raise ConnectionClosedError("MongoORM is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        try:
            self.close()
        except BaseException:
            if exc_type is None:
                raise
        return False
