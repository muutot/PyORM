"""Immutable queries return complete models; projections return raw documents."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Self

from pymongo import IndexModel, ReturnDocument
from pymongo.results import DeleteResult, UpdateResult

from .exceptions import ValidationError
from .model import VERSION_FIELD, WRITE_TOKEN_FIELD, Document
from .serialization import encode_value, key_name, validate_bson
from .types import BSONDocument, MongoUpdate

if TYPE_CHECKING:
    from .connection import MongoORM

OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "nin", "exists", "all", "size"}
UPDATE_OPERATORS = {
    "$set",
    "$unset",
    "$inc",
    "$push",
    "$pull",
    "$addToSet",
    "$pop",
    "$min",
    "$max",
    "$mul",
    "$currentDate",
}


def field_path(model: type[Document[Any]], path: str) -> str:
    parts = path.replace("__", ".").split(".")
    if parts[0] == "id":
        parts[0] = "_id"
    elif parts[0] in model._fields:
        parts[0] = model._fields[parts[0]].db_field
    for part in parts:
        key_name(part)
    return ".".join(parts)


def build_filter(
    model: type[Document[Any]], query: Mapping[str, Any] | None = None, **filters
) -> Mapping[str, Any]:
    clauses: list[Mapping[str, Any]] = [deepcopy(query)] if query else []
    for name, value in filters.items():
        parts = name.split("__")
        operator = parts.pop() if len(parts) > 1 and parts[-1] in OPERATORS else "eq"
        path = field_path(model, ".".join(parts))
        value = encode_value(value)
        clauses.append({path: value if operator == "eq" else {f"${operator}": value}})
    result = {} if not clauses else clauses[0] if len(clauses) == 1 else {"$and": clauses}
    validate_bson(result)
    return result


def build_update(
    model: type[Document[Any]], update: MongoUpdate | None = None, **changes
) -> MongoUpdate:
    result: MongoUpdate = deepcopy(update) if update is not None else {}
    for name, value in changes.items():
        operator, separator, path = name.partition("__")
        if not separator:
            operator, path = "set", name
        result.setdefault(f"${operator}", {})[field_path(model, path)] = encode_value(value)
    if not result:
        raise ValidationError("An update must contain at least one operation")
    for operator, values in result.items():
        if operator not in UPDATE_OPERATORS or not isinstance(values, dict) or not values:
            raise ValidationError(f"Unsupported or empty update operator: {operator}")
        for path in values:
            if not isinstance(path, str) or path.split(".")[0] in (
                "_id",
                VERSION_FIELD,
                WRITE_TOKEN_FIELD,
            ):
                raise ValidationError("Updates must not change _id or _orm_version")
            for part in path.split("."):
                key_name(part)
    result.setdefault("$inc", {})[VERSION_FIELD] = 1
    validate_bson(result)
    return result


@dataclass(frozen=True)
class QuerySet[T: Document[Any]]:
    orm: MongoORM
    model: type[T]
    _filter: Mapping[str, Any]
    _sort: tuple[tuple[str, int], ...] = ()
    _limit: int = 0
    _skip: int = 0

    def filter(self, query: Mapping[str, Any] | None = None, **filters) -> Self:
        extra = build_filter(self.model, query, **filters)
        combined = (
            {"$and": [self._filter, extra]} if self._filter and extra else (extra or self._filter)
        )
        return replace(self, _filter=deepcopy(combined))

    def sort(self, *fields: str) -> Self:
        order: list[tuple[str, int]] = []
        for name in fields:
            descending = name.startswith("-")
            order.append(
                (field_path(self.model, name[1:] if descending else name), -1 if descending else 1)
            )
        return replace(self, _sort=tuple(order))

    def limit(self, value: int) -> Self:
        if type(value) is not int or value < 0:
            raise ValueError("limit must be a nonnegative integer")
        return replace(self, _limit=value)

    def skip(self, value: int) -> Self:
        if type(value) is not int or value < 0:
            raise ValueError("skip must be a nonnegative integer")
        return replace(self, _skip=value)

    def raw(
        self, projection: Mapping[str, Any] | Iterable[str] | None = None
    ) -> list[BSONDocument]:
        def perform() -> list[BSONDocument]:
            cursor = self.orm._collection(self.model).find(self._filter, projection)
            try:
                if self._sort:
                    cursor = cursor.sort(list(self._sort))
                if self._skip:
                    cursor = cursor.skip(self._skip)
                if self._limit:
                    cursor = cursor.limit(self._limit)
                return list(cursor)
            finally:
                cursor.close()

        return self.orm._run(perform)

    def all(self) -> list[T]:
        return [self.model.from_mongo(data, orm=self.orm) for data in self.raw()]

    def first(self) -> T | None:
        rows = self.limit(1).all()
        return rows[0] if rows else None

    def count(self) -> int:
        options = {}
        if self._skip:
            options["skip"] = self._skip
        if self._limit:
            options["limit"] = self._limit
        return self.orm._run(
            self.orm._collection(self.model).count_documents, self._filter, **options
        )

    def exists(self) -> bool:
        return bool(self.limit(1).raw({"_id": 1}))

    def _check_write(self) -> None:
        if self._sort or self._limit or self._skip:
            raise ValueError("Bulk writes require an unsorted, unpaginated query")

    def update(self, update: MongoUpdate | None = None, **changes) -> UpdateResult:
        self._check_write()
        update = build_update(self.model, update, **changes)
        return self.orm._run(self.orm._collection(self.model).update_many, self._filter, update)

    def delete(self) -> DeleteResult:
        self._check_write()
        return self.orm._run(self.orm._collection(self.model).delete_many, self._filter)

    def __iter__(self) -> Iterator[T]:
        return iter(self.all())


class Repository[T: Document[Any]]:
    def __init__(self, orm: MongoORM, model: type[T]) -> None:
        self.orm = orm
        self.model = model

    def new(self, **values) -> T:
        instance = self.model(**values)
        instance._orm = self.orm
        return instance

    def create(self, **values) -> T:
        return self.new(**values).save()

    def get(self, id) -> T | None:
        raw = self.orm._run(self.orm._collection(self.model).find_one, {"_id": id})
        return None if raw is None else self.model.from_mongo(raw, orm=self.orm)

    def get_many[ID](self, ids: Iterable[ID]) -> dict[ID, T]:
        rows = self.filter({"_id": {"$in": list(ids)}}).all()
        return {row.id: row for row in rows}

    def filter(self, query: Mapping[str, Any] | None = None, **filters) -> QuerySet[T]:
        return QuerySet(self.orm, self.model, build_filter(self.model, query, **filters))

    def all(self) -> list[T]:
        return self.filter().all()

    def update_one(
        self,
        query: Mapping[str, Any],
        update: MongoUpdate | None = None,
        *,
        upsert: bool = False,
        **changes,
    ) -> T | None:
        update = build_update(self.model, update, **changes)
        raw = self.orm._run(
            self.orm._collection(self.model).find_one_and_update,
            deepcopy(query),
            update,
            upsert=upsert,
            return_document=ReturnDocument.AFTER,
        )
        return None if raw is None else self.model.from_mongo(raw, orm=self.orm)

    def aggregate(self, pipeline: Sequence[Mapping[str, Any]], **options) -> list[BSONDocument]:
        def perform() -> list[BSONDocument]:
            cursor = self.orm._collection(self.model).aggregate(deepcopy(pipeline), **options)
            try:
                return list(cursor)
            finally:
                cursor.close()

        return self.orm._run(perform)

    def ensure_indexes(self) -> list[str]:
        indexes = self.model.__indexes__
        if not all(isinstance(index, IndexModel) for index in indexes):
            raise TypeError("__indexes__ must contain pymongo.IndexModel objects")
        return (
            []
            if not indexes
            else self.orm._run(
                self.orm._collection(self.model).create_indexes,
                list(indexes),
            )
        )
