"""Model state is per-instance; constructors never change class methods."""

from __future__ import annotations

import inspect
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from copy import deepcopy
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING, Any, ClassVar, Self, overload

from bson import ObjectId
from pymongo import IndexModel

from .exceptions import ConcurrentModificationError, ValidationError
from .fields import MISSING, Field
from .serialization import diff_documents, validate_bson
from .types import BSONDocument, CacheKey, ChangeNotifier, MongoUpdate

if TYPE_CHECKING:
    from .connection import MongoORM
    from .manager import Manager

VERSION_FIELD = "_orm_version"
WRITE_TOKEN_FIELD = "_orm_write_token"


def _synchronize_method(method):
    @wraps(method)
    def synchronized(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return synchronized


class ModelMeta(type):
    _fields: dict[str, Field[Any]]

    def __new__(
        mcls: type[ModelMeta], name: str, bases: tuple[type, ...], namespace: dict[str, Any]
    ) -> ModelMeta:
        fields: dict[str, Field[Any]] = {}
        for base in bases:
            fields.update(getattr(base, "_fields", {}))
        # Retain the source project's field-list declaration as a migration aid.
        for field_name in namespace.get("_autosave", ()):
            if field_name not in fields and not isinstance(namespace.get(field_name), Field):
                default = namespace.get(field_name, MISSING)
                # BSON uses arrays for lists, tuples and sets; retain the declared
                # container type so a fresh load restores the business API.
                container_type = type(default)
                if container_type not in (dict, list, tuple, set, frozenset):
                    container_type = None
                namespace[field_name] = Field(container_type, default=default)
        for attr, value in namespace.items():
            if attr in fields and not isinstance(value, Field):
                raise TypeError(f"Inherited field {attr!r} must be overridden with Field")
            if isinstance(value, Field):
                if attr.startswith("_") or attr == "id" or any(hasattr(b, attr) for b in bases):
                    if attr not in fields:
                        raise TypeError(f"Reserved model attribute: {attr!r}")
                fields[attr] = value
        # Business methods are synchronized once at class creation, never by changing
        # class methods temporarily during instance construction. Manager I/O is separate.
        if namespace.get("__module__") != __name__:
            for attr, value in list(namespace.items()):
                if (
                    not attr.startswith("__")
                    and inspect.isfunction(value)
                    and not inspect.iscoroutinefunction(value)
                    and not inspect.isgeneratorfunction(value)
                ):
                    namespace[attr] = _synchronize_method(value)
        cls = super().__new__(mcls, name, bases, namespace)
        db_names = [field.db_field for field in fields.values()]
        if len(set(db_names)) != len(db_names) or {
            "_id",
            VERSION_FIELD,
            WRITE_TOKEN_FIELD,
        } & set(db_names):
            raise TypeError("Duplicate or reserved BSON field name")
        cls._fields = fields
        return cls


class EmbeddedDocument(metaclass=ModelMeta):
    _fields: ClassVar[dict[str, Field[Any]]]
    _autosave: ClassVar[Sequence[str]] = ()

    def __init__(self, **values) -> None:
        self._lock = threading.RLock()
        self._values: BSONDocument = {}
        self._extra: BSONDocument = {}
        self._on_change: ChangeNotifier | None = None
        unknown = values.keys() - self._fields.keys()
        if unknown:
            raise ValidationError(f"Unknown fields: {sorted(unknown)}")
        for name, field in self._fields.items():
            value = values[name] if name in values else field.get_default()
            if value is not MISSING:
                setattr(self, name, value)

    @contextmanager
    def locked(self) -> Iterator[Self]:
        """Hold the root document lock around shared nested-container mutations."""
        with self._lock:
            yield self

    def _field_hook(self, name: str) -> ChangeNotifier | None:
        return self._on_change

    def _bind_changes(self, hook: ChangeNotifier, lock: threading.RLock) -> None:
        from .tracking import track

        if self._on_change is not None and self._on_change != hook:
            raise ValueError("An embedded object cannot belong to two managed fields")
        self._lock = lock
        self._on_change = hook
        self._values = {name: track(value, hook, lock) for name, value in self._values.items()}

    def to_mongo(self) -> BSONDocument:
        with self._lock:
            data = deepcopy(self._extra)
            for name, field in self._fields.items():
                if name in self._values:
                    data[field.db_field] = field.to_mongo(self._values[name])
                elif field.required:
                    raise ValidationError(f"Field {name!r} is required")
            validate_bson(data)
            return deepcopy(data)

    @classmethod
    def from_mongo(cls, data: BSONDocument) -> Self:
        # Bypass custom constructors; loading does not run business initialization.
        instance = cls.__new__(cls)
        EmbeddedDocument.__init__(instance)
        names = {field.db_field for field in cls._fields.values()}
        instance._extra = deepcopy({k: v for k, v in data.items() if k not in names})
        for name, field in cls._fields.items():
            if field.db_field in data:
                setattr(instance, name, field.from_mongo(deepcopy(data[field.db_field])))
        return instance

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._values!r})"


class Document[ID = ObjectId](EmbeddedDocument):
    __collection__: ClassVar[str | Enum | None] = None
    __indexes__: ClassVar[Sequence[IndexModel]] = ()
    _cache_key: CacheKey
    _change_seq: int
    _acked_seq: int
    _dirty_fields: dict[str, int]
    _hooks: dict[str, ChangeNotifier]

    @classmethod
    def load(cls, id: ID) -> Self:
        """Load/reuse this model through the Manager configured at application startup."""
        from .runtime import get_manager

        return get_manager().get(cls, id)

    @overload
    def __init__(self: Document[ObjectId], *, id: None = None, **values) -> None: ...

    @overload
    def __init__(self, *, id: ID, **values) -> None: ...

    def __init__(self, *, id=None, **values) -> None:
        super().__init__(**values)
        # Constructor overloads describe when a default ObjectId is generated.
        self._id: ID = ObjectId() if id is None else id  # pyright: ignore[reportAttributeAccessIssue]
        self._version = 0
        self._persisted = False
        self._deleted = False
        self._snapshot: BSONDocument = {}
        self._orm: MongoORM | None = None
        self._manager: Manager | None = None

    @property
    def id(self) -> ID:
        return self._id

    @property
    def version(self) -> int:
        return self._version

    @property
    def is_persisted(self) -> bool:
        return self._persisted and not self._deleted

    @property
    def is_dirty(self) -> bool:
        if self._manager is not None:
            with self._lock:
                return not self._persisted or bool(self._dirty_fields)
        return not self._persisted or bool(self.pending_update())

    def _field_hook(self, name: str) -> ChangeNotifier | None:
        return self._hooks[name] if self._manager is not None else self._on_change

    def to_mongo(self) -> BSONDocument:
        data = super().to_mongo()
        data["_id"] = self.id
        data[VERSION_FIELD] = self.version
        validate_bson(data)
        return data

    def _data(self) -> BSONDocument:
        return super().to_mongo()

    @classmethod
    def from_mongo(cls, data: BSONDocument, *, orm: MongoORM | None = None) -> Self:
        version = data.get(VERSION_FIELD, 0)
        if type(version) is not int or version < 0:
            raise ValidationError(f"Invalid stored {VERSION_FIELD}: {version!r}")
        payload = {
            k: v for k, v in data.items() if k not in ("_id", VERSION_FIELD, WRITE_TOKEN_FIELD)
        }
        instance = super().from_mongo(payload)
        instance._id = data["_id"]
        instance._version = version
        instance._persisted = True
        instance._deleted = False
        instance._snapshot = deepcopy(payload)
        instance._orm = orm
        instance._manager = None
        return instance

    def pending_update(self) -> MongoUpdate:
        with self._lock:
            return diff_documents(self._snapshot, self._data())

    def _version_filter(self) -> BSONDocument:
        query: BSONDocument = {"_id": self.id, VERSION_FIELD: self.version}
        if self.version == 0:
            query.pop(VERSION_FIELD)
            query["$or"] = [{VERSION_FIELD: 0}, {VERSION_FIELD: {"$exists": False}}]
        return query

    def _engine(self, orm: MongoORM | None = None) -> MongoORM:
        engine = orm if orm is not None else self._orm
        if engine is None:
            raise RuntimeError("Use orm.repository(Model).new() or pass orm= to save()")
        if self._orm is not None and engine is not self._orm:
            raise RuntimeError("A document cannot be moved between ORM connections")
        return engine

    def save(self, *, orm: MongoORM | None = None) -> Self:
        if self._manager is not None:
            raise RuntimeError("Managed documents are persisted automatically by their Manager")
        engine = self._engine(orm)

        def perform() -> Self:
            with self._lock:
                if self._deleted:
                    raise RuntimeError("Deleted documents cannot be saved again")
                collection = engine._collection(type(self))
                data = self._data()
                if not self._persisted:
                    collection.insert_one({**data, "_id": self.id, VERSION_FIELD: 1})
                    self._version = 1
                    self._persisted = True
                else:
                    update = diff_documents(self._snapshot, data)
                    if not update:
                        return self
                    update["$inc"] = {VERSION_FIELD: 1}
                    result = collection.update_one(self._version_filter(), update)
                    if result.matched_count != 1:
                        raise ConcurrentModificationError(
                            f"{type(self).__name__} {self.id!r} changed at version {self.version}"
                        )
                    self._version += 1
                # Only acknowledge the snapshot after an acknowledged database write.
                self._snapshot = deepcopy(data)
                self._orm = engine
                return self

        return engine._run(perform)

    def save_async(self, *, orm: MongoORM | None = None) -> Future[Self]:
        if self._manager is not None:
            raise RuntimeError("Managed documents are persisted automatically by their Manager")
        engine = self._engine(orm)
        return engine.submit(
            self.save, orm=engine, key=(engine.collection_name(type(self)), self.id)
        )

    def reload(self) -> Self:
        if self._manager is not None:
            raise RuntimeError("Managed cache is authoritative and must not be replaced by reload")
        engine = self._engine()

        def perform() -> Self:
            with self._lock:
                raw = engine._collection(type(self)).find_one({"_id": self.id})
                if raw is None:
                    raise ConcurrentModificationError(f"Document {self.id!r} no longer exists")
                loaded = type(self).from_mongo(raw, orm=engine)
                self._values = loaded._values
                self._extra = loaded._extra
                self._snapshot = loaded._snapshot
                self._version = loaded._version
                self._persisted = True
                self._deleted = False
                return self

        return engine._run(perform)

    def delete(self) -> bool:
        if self._manager is not None:
            raise RuntimeError("Direct deletion of a managed cache document is not supported")
        engine = self._engine()

        def perform() -> bool:
            with self._lock:
                if not self.is_persisted:
                    return False
                result = engine._collection(type(self)).delete_one(self._version_filter())
                if result.deleted_count != 1:
                    raise ConcurrentModificationError(
                        f"Document {self.id!r} changed before deletion"
                    )
                self._deleted = True
                return True

        return engine._run(perform)
