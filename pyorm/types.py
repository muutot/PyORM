"""Shared types for dynamic BSON boundaries and change tracking."""

from typing import Any, Protocol

type BSONDocument = dict[str, Any]
type MongoUpdate = dict[str, BSONDocument]
type CacheKey = tuple[str, bytes]


class ChangeNotifier(Protocol):
    def before(self) -> None: ...

    def __call__(self) -> None: ...
